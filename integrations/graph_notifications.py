"""Graph change notifications through Event Hubs: a nudge that pulls mail and Teams sooner.

ARCH-REVIEW-1112 L2 / AUD-05. Graph posts a change notification for a subscribed resource
(a mailbox's messages, Teams chat and channel messages) to an Azure Event Hub; this
receiver reads that hub and, for each valid notification, writes a **wake** for the source
it concerns (``mail`` or ``teams``). The continuous ingest
(``operating.ingest_forever.Continuous``, ``ingest.graph_notifications`` true) starts that
source's own delta capture early, at most once per ``WAKE_MIN_GAP_SECONDS``. Nothing else
changes: the delta query stays the one reader (a notification's ids are never fetched,
stored or trusted), the people exclusion and every capture rule apply as before, and the
regular cadence remains the floor, so a lost or late notification costs freshness only, and a forged one at most one
extra pull per source per 30 s (``WAKE_MIN_GAP_SECONDS``), never a missed or a wrong row.
Known limit: whoever holds the client-state secret can keep that 30 s cadence going.

**Off unless configured.** With none of the ``PLX_GRAPH_NOTIFY_*`` settings the receiver
reports ``not_configured`` and exits 0, writes nothing, and the ingest reads no wakes unless
the host config sets ``ingest.graph_notifications``; until both are set capture behaves
exactly as without this module.

Validation, fail closed (a notification that fails any check wakes nothing; counted):

* the event body is JSON ``{"value": [notification, ...]}`` (Graph's batch shape);
* ``clientState`` equals ``PLX_GRAPH_NOTIFY_CLIENT_STATE_SECRET`` (compared in constant time);
* ``tenantId`` equals ``AZURE_TENANT_ID`` when that is set;
* ``subscriptionId`` is in ``PLX_GRAPH_NOTIFY_SUBSCRIPTIONS`` when that list is set;
* ``resource`` is a mailbox message (``users/{id}/messages/{id}``, with or without a mail
  folder) -> ``mail``, or a chat or channel message (``chats(...)/messages(...)``,
  ``teams(...)/channels(...)/messages(...)``, replies) -> ``teams``; anything else is
  ``ignored``.

Lifecycle notifications (``lifecycleEvent``: ``missed`` wakes its source;
``reauthorizationRequired`` and ``subscriptionRemoved`` wake nothing) and the latest
``subscriptionExpirationDateTime`` seen per source are kept in the status file
(``<state>/host/graph-notify.json``) so an expiring or removed subscription is visible in
ops before the nudges stop. Nothing written holds an id, an address, a resource path, a
client state or a key: source names, counts, codes and times only.

Event Hubs reading (``EventHubSource``) needs the optional ``azure-eventhub`` package (pure
Python AMQP; no stdlib client exists, and Event Hubs offers no REST receive) and a
listen-only connection string (``PLX_GRAPH_NOTIFY_EVENTHUB_KEY``, a secret never printed). It
only receives: offsets are checkpointed in a local file (``<state>/host/graph-notify-
offsets.json``), never in Azure, and a fresh start reads from the newest event (the
regular cadence covers what came before).

``python3 -m integrations.graph_notifications serve --state DIR`` runs the receiver;
``... probe [--state DIR]`` (the live check ``graph.change_notifications``) reads the hub's
partition ids (metadata only, no event) and the status file's counts and times;
``... status --state DIR`` prints the status file; ``... replay --state DIR FILE`` feeds a
fixture file (JSON lines of event bodies) through the same path (tests, a dry run).
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

SOURCES = ("mail", "teams")
WAKE_DIR = Path("host") / "notify-wake"
WEBHOOK_WAKE_FILE = Path("host") / "graph-notify.wake"
RENEW_WAKE_FILE = Path("host") / "graph-subscriptions.wake"
STATUS_FILE = Path("host") / "graph-notify.json"
OFFSETS_FILE = Path("host") / "graph-notify-offsets.json"
STATUS_SCHEMA = "plx-graph-notify/v1"
WAKE_MIN_GAP_SECONDS = 30.0      # the ingest starts a woken source at most this often
MAX_EVENT_BYTES = 1 << 20
WEBHOOK_MAX_BYTES = 256 * 1024
# outcomes that are a refusal (forged, foreign or malformed), not just "nothing to wake"
REFUSED = frozenset({"client_state_mismatch", "tenant_mismatch", "subscription_unknown",
                     "change_type_unknown", "lifecycle_unknown", "malformed",
                     "event_malformed"})        # Graph batches are small; a larger body is refused

ENV_NAMESPACE = "PLX_GRAPH_NOTIFY_EVENTHUB_KEY"            # listen-only connection string
ENV_HUB = "PLX_GRAPH_NOTIFY_HUB"
ENV_GROUP = "PLX_GRAPH_NOTIFY_CONSUMER_GROUP"
ENV_CLIENT_STATE = "PLX_GRAPH_NOTIFY_CLIENT_STATE_SECRET"
ENV_SUBSCRIPTIONS = "PLX_GRAPH_NOTIFY_SUBSCRIPTIONS"
ENV_TENANT = "AZURE_TENANT_ID"
ENV_GRAPH_TENANT = "MICROSOFT_GRAPH_TENANT_ID"
SETTING_NAMES = (ENV_NAMESPACE, ENV_HUB, ENV_GROUP, ENV_CLIENT_STATE, ENV_SUBSCRIPTIONS)

_ID = r"(?:\('[^'/]+'\)|/[^/()]+)"
_MAIL = re.compile(rf"^/?users{_ID}(?:/mailfolders{_ID})?/messages{_ID}$", re.I)
_TEAMS = re.compile(rf"^/?(?:chats{_ID}|teams{_ID}/channels{_ID})/messages{_ID}"
                    rf"(?:/replies{_ID})?$", re.I)
_MAIL_SUB = re.compile(rf"^/?users{_ID}(?:/mailfolders{_ID})?/messages$", re.I)
_TEAMS_SUB = re.compile(
    rf"^/?(?:chats{_ID}/messages|chats/getallmessages|users{_ID}/chats/getallmessages|"
    rf"teams{_ID}/channels{_ID}/messages|teams/getallmessages|"
    rf"teams{_ID}/channels/getallmessages)$", re.I)
_EXPIRY_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)")
_STATE_LOCK = threading.Lock()


class NotConfigured(Exception):
    """None of the receiver's settings is set: it does nothing."""


class SettingsInvalid(ValueError):
    """Some but not all required settings are set, or one is malformed (code only)."""


@dataclass(frozen=True)
class Settings:
    connection: str = field(repr=False)
    hub: str
    client_state: str = field(repr=False)
    consumer_group: str = "$Default"
    tenant: str | None = None
    subscriptions: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        present = {name for name in SETTING_NAMES if (environ.get(name) or "").strip()}
        if not present:
            raise NotConfigured("not_configured")
        missing = [n for n in (ENV_NAMESPACE, ENV_HUB, ENV_CLIENT_STATE) if n not in present]
        if missing:
            raise SettingsInvalid("settings_incomplete:" + ",".join(missing))
        state = environ[ENV_CLIENT_STATE].strip()
        if not 16 <= len(state) <= 128:   # Graph allows 128; a guessable one is no check
            raise SettingsInvalid("client_state_length")
        hub = environ[ENV_HUB].strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", hub):
            raise SettingsInvalid("hub_name_invalid")
        subs = frozenset(s.strip().lower() for s in
                         (environ.get(ENV_SUBSCRIPTIONS) or "").split(",") if s.strip())
        return cls(connection=environ[ENV_NAMESPACE].strip(), hub=hub, client_state=state,
                   consumer_group=(environ.get(ENV_GROUP) or "").strip() or "$Default",
                   tenant=(environ.get(ENV_TENANT) or "").strip().lower() or None,
                   subscriptions=subs)

    @classmethod
    def for_webhook(cls, environ: Mapping[str, str]) -> "Settings":
        """Settings for the HTTP webhook receiver (``POST /graph/notify``). Does not require
        Event Hubs settings. When ``PLX_GRAPH_NOTIFY_CLIENT_STATE_SECRET`` is unset or empty,
        ``client_state`` is empty and ``judge`` fails closed (drops every notification)."""
        state = (environ.get(ENV_CLIENT_STATE) or "").strip()
        tenant = ((environ.get(ENV_TENANT) or "").strip()
                  or (environ.get(ENV_GRAPH_TENANT) or "").strip()).lower() or None
        subs = frozenset(s.strip().lower() for s in
                         (environ.get(ENV_SUBSCRIPTIONS) or "").split(",") if s.strip())
        return cls(connection="", hub="webhook", client_state=state,
                   tenant=tenant, subscriptions=subs)


# --- one notification ----------------------------------------------------------------

def source_of(resource: Any) -> str | None:
    """``mail``, ``teams`` or None for the resource path of a notification."""
    if not isinstance(resource, str) or len(resource) > 2048:
        return None
    path = resource.strip().split("?", 1)[0]
    if _MAIL.match(path):
        return "mail"
    if _TEAMS.match(path):
        return "teams"
    return None


def lifecycle_source_of(resource: Any) -> str | None:
    """``mail``, ``teams`` or None for a lifecycle notification, which may name either a
    message resource or a subscription-level resource (e.g. ``chats/getAllMessages``)."""
    found = source_of(resource)
    if found is not None:
        return found
    if not isinstance(resource, str) or len(resource) > 2048:
        return None
    path = resource.strip().split("?", 1)[0]
    if _MAIL_SUB.match(path):
        return "mail"
    if _TEAMS_SUB.match(path):
        return "teams"
    return None


def judge(note: Any, settings: Settings) -> tuple[str | None, str]:
    """(source to wake or None, outcome code) of one notification."""
    if not isinstance(note, Mapping):
        return None, "malformed"
    given = note.get("clientState")
    if not settings.client_state or not isinstance(given, str) or not hmac.compare_digest(
            given.encode("utf-8"), settings.client_state.encode("utf-8")):
        return None, "client_state_mismatch"
    tenant = note.get("tenantId")
    if settings.tenant is not None and (not isinstance(tenant, str)
                                        or tenant.lower() != settings.tenant):
        return None, "tenant_mismatch"
    sub = note.get("subscriptionId")
    if settings.subscriptions and (not isinstance(sub, str)
                                   or sub.lower() not in settings.subscriptions):
        return None, "subscription_unknown"
    source = source_of(note.get("resource"))
    lifecycle = note.get("lifecycleEvent")
    if lifecycle is not None:
        if lifecycle == "missed":
            lsrc = source or lifecycle_source_of(note.get("resource"))
            return lsrc, "lifecycle_missed" if lsrc else "ignored"
        if lifecycle in ("reauthorizationRequired", "subscriptionRemoved"):
            return None, "lifecycle_" + lifecycle
        return None, "lifecycle_unknown"
    if source is None:
        return None, "ignored"
    if note.get("changeType") not in ("created", "updated", "deleted"):
        return None, "change_type_unknown"
    return source, "accepted"


def notifications(body: bytes, *, max_bytes: int = MAX_EVENT_BYTES) -> list[Any] | None:
    """The notifications of one event body (None: not Graph's batch shape)."""
    if len(body) > max_bytes:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    value = data.get("value") if isinstance(data, dict) else None
    return value if isinstance(value, list) else None


# --- wakes: the one hand-off to the ingest -------------------------------------------

def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class WebhookResult:
    """Evaluated outcome of one HTTP webhook batch (no secrets, IDs, or resource paths)."""
    accepted: int
    dropped: int
    counts: dict[str, int]
    woken_sources: tuple[str, ...]
    expiries: dict[str, str]
    source_lifecycles: dict[str, str]
    renewal_events: tuple[str, ...]


_ACCEPTED_OUTCOMES = frozenset({
    "accepted",
    "lifecycle_missed",
    "lifecycle_reauthorizationRequired",
    "lifecycle_subscriptionRemoved",
})
_RENEWAL_OUTCOMES = frozenset({
    "lifecycle_reauthorizationRequired",
    "lifecycle_subscriptionRemoved",
})


def evaluate_webhook_body(body: bytes, settings: Settings) -> WebhookResult:
    """Validate one HTTP webhook POST body in memory without touching disk."""
    batch = notifications(body, max_bytes=WEBHOOK_MAX_BYTES)
    if batch is None:
        return WebhookResult(
            accepted=0,
            dropped=1,
            counts={"event_malformed": 1},
            woken_sources=(),
            expiries={},
            source_lifecycles={},
            renewal_events=(),
        )
    counts: dict[str, int] = {}
    woken: set[str] = set()
    expiries: dict[str, str] = {}
    source_lifecycles: dict[str, str] = {}
    renewal_events: set[str] = set()
    accepted = 0
    dropped = 0
    for note in batch:
        source, outcome = judge(note, settings)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in _ACCEPTED_OUTCOMES:
            accepted += 1
        else:
            dropped += 1
        if outcome in _RENEWAL_OUTCOMES:
            renewal_events.add(outcome)
        if source is not None:
            woken.add(source)
            expiry = note.get("subscriptionExpirationDateTime") if isinstance(note, Mapping) else None
            if isinstance(expiry, str) and _EXPIRY_RE.fullmatch(expiry):
                expiries[source] = max(expiries.get(source) or "", expiry)
            if outcome.startswith("lifecycle_"):
                source_lifecycles[source] = outcome
    return WebhookResult(
        accepted=accepted,
        dropped=dropped,
        counts=counts,
        woken_sources=tuple(sorted(woken)),
        expiries=expiries,
        source_lifecycles=source_lifecycles,
        renewal_events=tuple(sorted(renewal_events)),
    )


def record_webhook_result(
    state: Path,
    result: WebhookResult,
    *,
    now: Callable[[], float] = time.time,
) -> None:
    """Atomically record a ``WebhookResult`` to ``<state>/host/graph-notify.json`` and
    write wake markers for ingest and subscription renewal. Never writes IDs or secrets."""
    state_path = Path(state)
    ts = now()
    with _STATE_LOCK:
        status_path = state_path / STATUS_FILE
        try:
            held = json.loads(status_path.read_text(encoding="utf-8"))
            if not isinstance(held, dict) or held.get("schema") != STATUS_SCHEMA:
                held = {"schema": STATUS_SCHEMA, "counts": {}, "sources": {}}
        except (OSError, ValueError):
            held = {"schema": STATUS_SCHEMA, "counts": {}, "sources": {}}
        held_counts = held.setdefault("counts", {})
        held_sources = held.setdefault("sources", {})
        for source in result.woken_sources:
            entry = held_sources.setdefault(source, {})
            entry["last_notified_at"] = ts
            expiry = result.expiries.get(source)
            if expiry:
                entry["subscription_expires"] = max(entry.get("subscription_expires") or "", expiry)
            lc = result.source_lifecycles.get(source)
            if lc:
                entry["last_lifecycle"] = lc
        for name, count in result.counts.items():
            held_counts[name] = held_counts.get(name, 0) + count
            if name.startswith("lifecycle_") and name != "lifecycle_missed":
                held["last_lifecycle"] = {"event": name, "at": ts}
        held["last_event_at"] = ts
        _write_json(status_path, held)
        for source in result.woken_sources:
            wake(state_path, source, now=lambda: ts)
        if result.woken_sources:
            _write_json(state_path / WEBHOOK_WAKE_FILE,
                        {"at": ts, "sources": list(result.woken_sources)})
        if result.renewal_events:
            _write_json(state_path / RENEW_WAKE_FILE,
                        {"at": ts, "events": list(result.renewal_events)})


def wake(state: Path, source: str, *, now: Callable[[], float] = time.time) -> None:
    """Ask the ingest to run ``source``'s delta capture soon (``<state>/host/notify-wake/
    <source>.json``: the time only)."""
    if source not in SOURCES:
        raise ValueError("source_unknown")
    _write_json(Path(state) / WAKE_DIR / f"{source}.json", {"at": now()})


class Wakes:
    """The ingest's side: the newest wake time per source, read from the wake files (only
    a file whose mtime changed is read again). A missing, unreadable or malformed file is
    no wake."""

    def __init__(self, state: Path) -> None:
        self.folder = Path(state) / WAKE_DIR
        self._seen: dict[str, tuple[int, float | None]] = {}

    def latest(self, names: Iterable[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for name in names:
            if name not in SOURCES:
                continue
            path = self.folder / f"{name}.json"
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            held = self._seen.get(name)
            if held is None or held[0] != mtime:
                try:
                    at = json.loads(path.read_text(encoding="utf-8")).get("at")
                except (OSError, ValueError, AttributeError):
                    at = None
                at = float(at) if isinstance(at, (int, float)) and not isinstance(
                    at, bool) else None
                held = (mtime, at)
                self._seen[name] = held
            if held[1] is not None:
                out[name] = held[1]
        return out


# --- the receiver --------------------------------------------------------------------

class Receiver:
    """Judges event bodies, writes wakes and keeps the status file. ``handle`` is called
    with each partition's batch; the caller checkpoints its offsets only after it
    returned (at-least-once: a repeated batch only repeats a wake)."""

    def __init__(self, state: Path, settings: Settings, *,
                 now: Callable[[], float] = time.time) -> None:
        self.state, self.settings, self.now = Path(state), settings, now
        self.status = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            held = json.loads((self.state / STATUS_FILE).read_text(encoding="utf-8"))
            if isinstance(held, dict) and held.get("schema") == STATUS_SCHEMA:
                return held
        except (OSError, ValueError):
            pass
        return {"schema": STATUS_SCHEMA, "counts": {}, "sources": {}}

    def handle(self, bodies: Sequence[bytes]) -> dict[str, int]:
        counts: dict[str, int] = {}
        woken: set[str] = set()
        for body in bodies:
            batch = notifications(body)
            if batch is None:
                counts["event_malformed"] = counts.get("event_malformed", 0) + 1
                continue
            for note in batch:
                source, outcome = judge(note, self.settings)
                counts[outcome] = counts.get(outcome, 0) + 1
                if source is None:
                    continue
                woken.add(source)
                entry = self.status["sources"].setdefault(source, {})
                entry["last_notified_at"] = self.now()
                expiry = note.get("subscriptionExpirationDateTime")
                if isinstance(expiry, str) and re.fullmatch(
                        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", expiry):
                    entry["subscription_expires"] = max(entry.get("subscription_expires")
                                                        or "", expiry)
                if outcome.startswith("lifecycle_"):
                    entry["last_lifecycle"] = outcome
        for source in sorted(woken):
            wake(self.state, source, now=self.now)
        for name, n in counts.items():
            self.status["counts"][name] = self.status["counts"].get(name, 0) + n
            if name.startswith("lifecycle_") and name != "lifecycle_missed":
                self.status["last_lifecycle"] = {"event": name, "at": self.now()}
        self.status["last_event_at"] = self.now() if bodies else self.status.get(
            "last_event_at")
        _write_json(self.state / STATUS_FILE, self.status)
        return counts


class Offsets:
    """Per-partition offsets of the receiver, in a local file (never in Azure)."""

    def __init__(self, state: Path) -> None:
        self.path = Path(state) / OFFSETS_FILE
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held = {}
        self.offsets: dict[str, str] = ({str(k): str(v) for k, v in held.items()}
                                        if isinstance(held, dict) else {})

    def get(self, partition: str) -> str | None:
        return self.offsets.get(partition)

    def put(self, partition: str, offset: str) -> None:
        self.offsets[partition] = offset
        _write_json(self.path, self.offsets)


def event_bytes(event: Any) -> bytes:
    """An event's data body as bytes; anything else (a value or sequence body) is empty,
    which the receiver counts as malformed."""
    try:
        body = event.body
        return bytes(body) if isinstance(body, (bytes, bytearray)) else b"".join(body)
    except Exception:  # noqa: BLE001 - not a data body: malformed, never a crash
        return b""


class EventHubSource:
    """Receives event batches from the hub (``azure-eventhub``, imported only here) and
    hands each to ``handle``; the offset is kept after ``handle`` returned."""

    def __init__(self, settings: Settings, offsets: Offsets) -> None:
        self.settings, self.offsets = settings, offsets

    def run(self, handle: Callable[[Sequence[bytes]], Any], stop: threading.Event) -> None:
        try:
            from azure.eventhub import EventHubConsumerClient  # type: ignore[import-not-found]
        except ImportError:
            raise SettingsInvalid("dependency_absent:azure-eventhub") from None
        client = EventHubConsumerClient.from_connection_string(
            self.settings.connection, consumer_group=self.settings.consumer_group,
            eventhub_name=self.settings.hub)

        def on_batch(context: Any, events: Sequence[Any]) -> None:
            if not events:
                return
            handle([event_bytes(e) for e in events])
            self.offsets.put(str(context.partition_id), str(events[-1].offset))

        starting = {p: o for p, o in self.offsets.offsets.items()}
        watcher = threading.Thread(target=lambda: (stop.wait(), client.close()),
                                   daemon=True, name="graph-notify-stop")
        watcher.start()
        with client:
            client.receive_batch(on_event_batch=on_batch, max_batch_size=100,
                                 max_wait_time=30,
                                 starting_position=starting or "@latest",
                                 starting_position_inclusive=False)


def probe(settings: Settings, state: Path | None, *,
          partitions: Callable[[Settings], Sequence[str]] | None = None,
          now: Callable[[], float] = time.time) -> dict[str, Any]:
    """The live check: the hub answers with its partitions (receive-only metadata), and how
    recent and how near expiry the notifications are (counts, ages, hours; never an id)."""
    if partitions is None:
        def partitions(given: Settings) -> Sequence[str]:
            try:
                from azure.eventhub import EventHubConsumerClient  # type: ignore[import-not-found]
            except ImportError:
                raise SettingsInvalid("dependency_absent:azure-eventhub") from None
            client = EventHubConsumerClient.from_connection_string(
                given.connection, consumer_group=given.consumer_group,
                eventhub_name=given.hub)
            with client:
                return list(client.get_partition_ids())
    report: dict[str, Any] = {"source": "graph_notifications",
                              "partitions": len(partitions(settings))}
    status: dict[str, Any] = {}
    if state is not None:
        try:
            status = json.loads((Path(state) / STATUS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
    counts = status.get("counts") or {}
    report["events_accepted"] = int(counts.get("accepted", 0))
    report["events_refused"] = int(sum(v for k, v in counts.items() if k in REFUSED))
    last = status.get("last_event_at")
    report["last_event_age_seconds"] = (round(now() - last) if isinstance(last, (int, float))
                                        else None)
    from datetime import datetime
    hours: dict[str, float] = {}
    for source, entry in (status.get("sources") or {}).items():
        expiry = (entry or {}).get("subscription_expires")
        if source in SOURCES and isinstance(expiry, str):
            try:
                at = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1",
                                                   expiry.replace("Z", "+00:00")))
            except ValueError:
                continue
            hours[source] = round((at.timestamp() - now()) / 3600, 1)
    report["expires_in_hours"] = hours
    report["ok"] = report["partitions"] > 0
    return report


def fixture_bodies(path: Path) -> Iterator[bytes]:
    """Event bodies of a fixture file: one JSON document per line."""
    with open(path, "rb") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield line


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None,
         source_factory: Callable[[Settings, Offsets], Any] = EventHubSource) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python3 -m integrations.graph_notifications")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "status", "replay", "probe"):
        p = sub.add_parser(name)
        p.add_argument("--state", required=name != "probe", type=Path,
                       default=Path(environ["PLX_INGEST_STATE"]) if name == "probe"
                       and environ.get("PLX_INGEST_STATE") else None)
        if name == "replay":
            p.add_argument("fixture", type=Path)
    args = parser.parse_args(argv)
    if args.command == "status":
        try:
            print((args.state / STATUS_FILE).read_text(encoding="utf-8").strip())
        except OSError:
            print(json.dumps({"status": "no_status"}))
        return 0
    try:
        settings = Settings.from_env(environ)
    except NotConfigured:
        print(json.dumps({"status": "not_configured"}))
        return 0
    except SettingsInvalid as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    if args.command == "probe":
        try:
            print(json.dumps(probe(settings, args.state), sort_keys=True))
        except SettingsInvalid as error:
            print(json.dumps({"status": "refused", "reason": str(error), "ok": False}))
            return 2
        except Exception as error:  # noqa: BLE001 - the class only: never a message
            print(json.dumps({"error": "hub_unreachable", "error_class":
                              type(error).__name__, "ok": False}))
            return 1
        return 0
    receiver = Receiver(args.state, settings)
    if args.command == "replay":
        print(json.dumps({"status": "replayed",
                          "counts": receiver.handle(list(fixture_bodies(args.fixture)))},
                         sort_keys=True))
        return 0
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        source_factory(settings, Offsets(args.state)).run(receiver.handle, stop)
    except SettingsInvalid as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(json.dumps({"status": "stopped", "counts": receiver.status.get("counts", {})},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
