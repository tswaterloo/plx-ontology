"""The AI watcher: what in each cycle of changes needs someone's attention, as a sharp
operations lead would notice it. It sits on top of the proactive engine's hand-written
detectors (``operating.proactive_engine``) and never replaces them.

Each run (``cycle``):

1. **Changes** (``discover``): the objects the row-delta layers (``ontology.delta``,
   ``<projection>/delta/CURRENT.json``) own that this watcher has not looked at yet, over
   the served generation the pointer names (another generation's pointer is ignored).
   Only ids, types, version digests and the values' own times are read here, never shown
   to anyone: a record whose current version has the digest last seen is unchanged; one
   whose properties changed only in the systems' write stamps (``delta.STAMP_PROPERTIES``)
   is a restamp; a new record whose own newest time is older than ``WINDOW_HOURS`` is a
   backlog capture, not news; a part of a message (an attachment, a handle with a
   ``#`` fragment) is read through its message; a message of a thread already queued
   folds into the thread's newest; another mailbox's copy of a message already seen (its
   ``internet_message_id``) is a ``copy``. A message, chat or document the people
   exclusion may cover is dropped before anything reads it (fail closed: no guard, no
   messages). Every colleague's message in the layers teaches who the company writes to
   (``correspondents``: digests of outside addresses and domains, never the text).
   Each is queued; a queued change older than ``WINDOW_HOURS`` ages out, counted.
2. **One batch** (``pick``): at most ``MAX_READS`` reads per run: changed business records
   first, then new messages and records, newest first; messages by tier (``_tier``: from
   someone the company writes to, naming business records, from a colleague, other,
   automated); a run of more than ``BULK_MIN`` records of one type that changed the same
   properties is one line (a bulk recompute) with a sample read, and so are more than
   ``MESSAGE_BULK_MIN`` messages of one automated or unknown sender (a notification
   stream). Nothing new: no model call (``nothing_new``). Every model session
   (batch or retry) is recorded as it starts; ``MAX_SESSIONS_PER_HOUR`` started in the
   last hour: none more (``rate_capped``); the queue waits. A run starts a session only
   when its time limit ends within ``RUN_SECONDS``; a killed run is resumed (``recover``).
3. **Opus reads them** through the existing headless agent path
   (``agent_session.ClaudeCodeAgent`` on ``agent_session.ReaderHost``): the read-only
   ``plx.*`` tools behind the tool gate, as one fixed reader (``PLX_WATCHER_READER``, an
   employee's identity claims; unset: the declared eval principal), so the host trims every
   read for that reader and applies the people exclusion. The batch's reads are made in the
   session itself before its first turn (the host's prefetch), so what it cites was
   returned to it. The task (``task``) is a short general instruction about what an
   operations lead at a manufacturer cares about; no question type, kind list or phrase
   list decides materiality. Every item it raises it submits to the session's own answer
   gate (``plx.case_get`` answer mode, ``operating.answer_gate``) with the item's title as
   the question.
4. **Gate, screen, truth check** (``proposals``, ``truth``): an item stands only on what
   the gate passed: its evidence groups are the passed claims' citations, its text and
   why are the submitted ones screened against those claims (``answer_gate.final_reply``
   with no asker-given identifiers; an item none of whose text or why sentences stand is
   ``statement_unsupported`` and gets its own gate pass), and it is checked by the
   cross-system truth check
   (``operating.truth_check``: test records, an order received or cancelled elsewhere, a
   lot used up, a hold that is a default, a quantity that is a format pair, an at-risk
   cause that is wrong). A contradicted item is refused, never shown.
5. **Dedup and fold** (``fold``): the open detector events and this watcher's own open
   items about the batch's records are in the task, so it does not raise them again; an
   item whose subject an open detector event already covers with the same evidence, or
   which repeats an open item of its own (same subject, evidence within it), is dropped
   or refreshed. Items stay open ``ITEM_DAYS`` past their last sight.

**Permissions** (``read_items``): each record a claim cites or is computed from (its
citations, a calculation's inputs, the record it was reached through) is stored with its
object and the markings a reader needs to read what the claim reads there (the version's
own and those of the values it reads, ``Places.markings_of``). A reader sees an item only
when every one of those records sits in an audience the reader is granted, at markings it
holds (in the served store or the delta layer serving the object now), the host's per-item
decision (``decide``, which applies the people exclusion) passes each, and the subject is
visible: every claim is shown or the item is not. The rest is never shown nor counted
(``briefing`` gives watcher items no hidden count).

**Never sends anything**: an item is a proposal with evidence; its next step is a draft
suggestion. The watcher writes only its own store (``<state>/ai-watcher/watcher.sqlite``,
or ``--out-dir``) and reads every source read-only.

**Switch** (``switch``): ``<state>/ai-watcher/switch.json`` ``off`` (default: the service
exits at once), ``dry`` (it runs and keeps items, the briefing shows none) or ``on`` (the
briefing lists open items as ``watcher_attention``). A file, so the service and the API
read the same switch without a restart.

Run: ``python3 -m operating.ai_watcher run --state-dir /data/plx-state --host-config
<host.json>``; ``switch on|dry|off``; ``status``; ``dryrun --out-dir DIR`` (the last
``--hours`` of deltas into a separate store and ``dryrun.md``; production untouched).
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from operating import declared_schema as _schemas
from operating.declared_schema import ai_watcher as _AI_WATCHER
from operating.prose_style import PROSE_STYLE_RULE

SCHEMA = "plx-ai-watcher/v1"
KIND = "watcher_attention"          # the gap kind its items carry (``briefing``)
DETECTOR = "ai_watcher"             # the check named in the briefing
AGENT = "plx.agent.investigate"     # the agent definition whose read-only tools it uses
READER_ENV = "PLX_WATCHER_READER"   # JSON claims of the employee it reads as
READER_CLAIMS = ("uid", "email", "entraTenantId", "entraObjectId")
MODEL_ENV = "PLX_WATCHER_MODEL"
MODEL = "opus"
SWITCH_STATES = ("off", "dry", "on")
WATCH_DIR = "ai-watcher"

WINDOW_HOURS = 24        # a new record older than this is backlog; a queued change ages out
MAX_READS = 36           # reads prefetched into one run's session
MAX_SESSIONS_PER_HOUR = 4   # model sessions (batch and retry) started in any hour
MAX_ATTEMPTS = 2         # sessions a queued change is sent to before it is abandoned
RUN_SECONDS = 1500       # one run's model sessions end within this (the unit allows 1800)
RETRY_BUDGET_SECONDS = 180   # a refused item's own pass: research budget
RETRY_TIMEOUT_SECONDS = 360  # and its process's own limit
BULK_MIN = 12            # same type, same changed properties, more than this: one bulk line
ITEM_DAYS = 7            # an open item expires this long after it was last seen
BUDGET_SECONDS = 420     # the session's research budget (the gate is always answered)
TIMEOUT_SECONDS = 720    # the session process's own limit
MAX_STEPS = 45           # tool calls besides the gate's
MAX_TURNS = 60
READ_CHARS = 1500        # a message or document read into the batch
MAX_ITEMS = 8            # items one run may raise
MAX_RETRIES = 3          # refused items one run decides again, each in its own gate pass
MAX_RETRY_CITES = 12     # records a retried item's pass is told it cited
PRIORITIES = ("high", "medium", "low")
# What an item's claim rests on, as the truth check tests it (never what makes it matter):
CLAIM_KINDS = ("order_open", "lot_near_expiry", "lot_on_hold", "quantity_conflict",
               "customer_order_at_risk", "other")
# Handles of messages, chats, meetings and documents: read one by one (never bulked) and
# cleared with the people exclusion first.
MESSAGE_SCHEMES = ("graph-mail", "teams-chat", "teams-channel", "sharepoint",
                   "sharepoint-list", "meeting-transcript", "calendar-event", "original")
# A message's thread (its newest message stands for the rest in one run).
THREAD_PROPERTIES = ("conversation_id", "chat_id")
# A message's own id across mailboxes (one copy per message is read), its sender and its
# recipients (who the company writes to: ``correspondents``).
MESSAGE_KEY_PROPERTIES = ("internet_message_id",)
SENDER_PROPERTIES = ("sender", "from_address")
RECIPIENT_PROPERTIES = ("to_addresses", "cc_addresses")
# More than this many queued messages of one sender who is automated or no correspondent:
# one bulk line with one sample read (a notification stream, a mailing).
MESSAGE_BULK_MIN = 4
# How the batch orders new messages (``_tier``): mail from someone the company writes to,
# then mail naming a business record, then colleagues, then the rest, automated last.
TIERS = ("correspondent", "names_records", "internal", "other", "automated")
TIME_TYPES = ("timestamp", "date")
# A batch's reads: messages, the schema's curated business records, its generated source
# mirrors (a share one leaves unused goes to the others).
SHARES = (("message", 0.5), ("curated", 0.4), ("mirror", 0.1))
_CHUNK = 500


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ro(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _chunks(items: Sequence[Any], size: int = _CHUNK) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _scheme(handle: str) -> str:
    return handle.partition("://")[0] if "://" in handle else ""


def is_message(handle: str) -> bool:
    return _scheme(handle) in MESSAGE_SCHEMES


def handle_root(handle: str) -> str:
    """A handle without its fragment and version: the record it names, at any version."""
    body = handle.split("#", 1)[0]
    scheme, sep, rest = body.partition("://")
    return scheme + sep + rest.rsplit("@", 1)[0] if sep and "@" in rest else body


# --------------------------------------------------------------------------- switch

def watch_dir(state_dir: Path) -> Path:
    return Path(state_dir) / WATCH_DIR


def switch_path(state_dir: Path) -> Path:
    return watch_dir(state_dir) / "switch.json"


def switch(state_dir: Path) -> str:
    """The switch as set (``off`` when unset or unreadable: never on by accident)."""
    try:
        value = json.loads(switch_path(state_dir).read_text(encoding="utf-8")).get("state")
    except (OSError, ValueError, AttributeError):
        return "off"
    return value if value in SWITCH_STATES else "off"


def set_switch(state_dir: Path, state: str, *, clock: Callable[[], dt.datetime] = _now
               ) -> None:
    if state not in SWITCH_STATES:
        raise ValueError("switch_state_unknown")
    path = switch_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"state": state, "at": _iso(clock())}), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- store



def store_path(directory: Path) -> Path:
    return Path(directory) / "watcher.sqlite"


def open_store(directory: Path) -> sqlite3.Connection:
    """The watcher's store, its owner's alone (0600: it keeps digests of addresses)."""
    path = store_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    for part in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            os.chmod(part, 0o600)
        except FileNotFoundError:
            pass
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    for column in ("attempts INTEGER NOT NULL DEFAULT 0", "claimed INTEGER", "traits TEXT"):
        try:   # a store written before sessions: its queue gains the columns
            conn.execute(f"ALTER TABLE queue ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass   # no queue yet (created below) or the column is there
    _schemas.apply(conn, _AI_WATCHER)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA,))
    elif row[0] != SCHEMA:
        conn.close()
        raise ValueError("watcher_schema_mismatch")
    if conn.execute("SELECT 1 FROM meta WHERE key = 'key_salt'").fetchone() is None:
        # this store's own random salt for the address and message digests; digests kept
        # before it (unsalted) are dropped, and queued messages rank as unknown senders
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM meta WHERE key = 'key_salt'").fetchone() is None:
                conn.execute("INSERT INTO meta VALUES ('key_salt', ?)",
                             (secrets.token_hex(32),))
                conn.execute("DELETE FROM correspondents")
                conn.execute("DELETE FROM learned")
                conn.execute("DELETE FROM message_keys")
                conn.execute("UPDATE queue SET traits = NULL WHERE traits IS NOT NULL")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return conn


def key_salt(ws: sqlite3.Connection) -> bytes:
    return bytes.fromhex(ws.execute("SELECT value FROM meta WHERE key = 'key_salt'"
                                    ).fetchone()[0])


@contextmanager
def _locked(directory: Path) -> Iterator[bool]:
    """One run at a time per store (a second one returns at once)."""
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".run.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- changes

def _pointer(store: Path) -> tuple[str | None, list[str]]:
    """(the served generation's content digest, the delta layers over it, newest first);
    no layers when the pointer is missing, unknown or of another generation."""
    from . import delta_overlay
    with closing(_ro(store)) as conn:
        served = dict(conn.execute("SELECT key, value FROM store_meta WHERE key IN "
                                   "('content_digest', 'schema_digest')").fetchall())
    digest = served.get("content_digest")
    fields = delta_overlay.read_pointer(delta_overlay.pointer_path(store))
    if fields is None or not digest or fields.get("base_digest") != digest:
        return digest, []
    layers = [str(layer) for layer in fields.get("layers") or ()]
    for layer in layers:
        # the overlay's own rule (``delta_overlay._open_layer``): a layer of another
        # generation or schema voids the whole pointer, as the tools would ignore it
        try:
            with closing(_ro(layer)) as conn:
                meta = dict(conn.execute("SELECT key, value FROM store_meta WHERE key IN "
                                         "('content_digest', 'schema_digest')").fetchall())
        except sqlite3.Error:
            return digest, []
        if meta.get("content_digest") != digest \
                or meta.get("schema_digest") != served.get("schema_digest"):
            return digest, []
    return digest, layers


# Postgres as the only store (``PLX_ONTOLOGY_PG=only``): the changes are the object
# versions Postgres numbered past the last ones looked at (each delta layer and migration
# numbers its versions from a sequence), one batch per run named ``postgres:LOW-HIGH``;
# a batch of more objects than this (a migration re-marking a type) is noted, not queued.
PG_BATCH_OBJECTS = 100_000


def _postgres_changes(ws: sqlite3.Connection, now_text: str
                      ) -> tuple[str, list[str], Any, Any] | None:
    """(the Postgres generation's digest as the layers' base, the batch of versions not
    looked at yet as one layer, how a layer is opened, its changed objects) when Postgres
    is the only store; None otherwise. The first look starts from the versions there are
    (what came before is the generation's)."""
    from operating.pg import ontology_reader
    if not ontology_reader.pg_only():
        return None
    import psycopg
    from operating.pg.sqlite_compat import PgCompatConnection
    target = ontology_reader.dsn()
    with psycopg.connect(target, connect_timeout=5) as pg:
        generation, top = pg.execute(
            "SELECT (SELECT value FROM ontology.store_meta WHERE key = 'content_digest'), "
            "(SELECT coalesce(max(ver), 0) FROM ontology.object_versions)").fetchone()
    digest = f"postgres:{generation}"
    marks = [int(r[0].rsplit("-", 1)[1]) for r in ws.execute(
        "SELECT store FROM layers WHERE base_digest = ? AND store LIKE 'postgres:%'",
        (digest,))]
    if not marks:
        ws.execute("INSERT OR REPLACE INTO layers VALUES (?, ?, ?)",
                   (f"postgres:{top}-{top}", digest, now_text))
        ws.execute("INSERT OR REPLACE INTO learned VALUES (?, ?, ?)",
                   (f"postgres:{top}-{top}", digest, now_text))
        return digest, [], None, None
    low = max(marks)
    layers = [f"postgres:{low}-{top}"] if top > low else []

    def open_layer(_layer: str) -> Any:
        return closing(PgCompatConnection(target))

    def owned(conn: Any, layer: str) -> list[str]:
        lo, hi = (int(x) for x in layer.split(":", 1)[1].split("-"))
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT object_id FROM object_versions WHERE ver > ? AND ver <= ? "
            "LIMIT ?", (lo, hi, PG_BATCH_OBJECTS + 1))]
        return [] if len(ids) > PG_BATCH_OBJECTS else ids
    return digest, layers, open_layer, owned


def _layer_owned(conn: sqlite3.Connection, _layer: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT object_id FROM delta_owned")]


_CURRENT = ("SELECT o.object_id, o.object_type, o.audience_key, v.l1_handle, v.digest, "
            "v.recorded_at, v.markings FROM objects o JOIN object_versions v ON "
            "v.object_id = o.object_id AND v.superseded_at IS NULL WHERE o.object_id IN "
            "(SELECT value FROM json_each(?))")


def _values(conn: sqlite3.Connection, ids: Sequence[str]
            ) -> dict[str, dict[str, list[Any]]]:
    """object id -> property -> its current values with their types (never shown)."""
    out: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for part in _chunks(list(ids)):
        for row in conn.execute(
                "SELECT object_id, property, value_type, value FROM object_values WHERE "
                "object_id IN (SELECT value FROM json_each(?)) AND superseded_at IS NULL",
                (json.dumps(list(part)),)):
            out[row[0]][row[1]].append((row[2], row[3]))
    return out


def _values_of(conn: sqlite3.Connection, ids: Sequence[str], props: Sequence[str]
               ) -> dict[str, dict[str, list[Any]]]:
    """``_values`` of only ``props`` (what learning who the company writes to reads)."""
    out: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for part in _chunks(list(ids)):
        for row in conn.execute(
                "SELECT object_id, property, value_type, value FROM object_values WHERE "
                "object_id IN (SELECT value FROM json_each(?)) AND superseded_at IS NULL "
                "AND property IN (SELECT value FROM json_each(?))",
                (json.dumps(list(part)), json.dumps(list(props)))):
            out[row[0]][row[1]].append((row[2], row[3]))
    return out


def _props(values: Mapping[str, Sequence[Any]]) -> dict[str, str]:
    """property -> a digest of its values (what a change compares)."""
    return {prop: hashlib.sha256(json.dumps(sorted(str(v) for _, v in vals))
                                 .encode("utf-8")).hexdigest()[:16]
            for prop, vals in values.items()}


def _event_at(values: Mapping[str, Sequence[Any]], recorded: str, now: str) -> str:
    """A record's own newest time not after now (its timestamps and dates), else when its
    version was recorded."""
    times = [str(v)[:19] for vals in values.values() for kind, v in vals
             if kind in TIME_TYPES and isinstance(v, str) and str(v)[:19] <= now[:19]
             and re.match(r"\d{4}-\d{2}-\d{2}", str(v))]
    return max(times) if times else recorded[:19]


def _thread(values: Mapping[str, Sequence[Any]]) -> str | None:
    for prop in THREAD_PROPERTIES:
        for _, value in values.get(prop) or ():
            if isinstance(value, str) and value:
                return f"{prop}:{value}"
    return None


_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _key(text: str, salt: bytes) -> str:
    """A lowered address, domain or message id as the watcher keeps it: a keyed digest
    under the store's own salt (``key_salt``), never the text."""
    return hmac.new(salt, text.strip().lower().encode("utf-8"), hashlib.sha256
                    ).hexdigest()[:32]


def _addresses(values: Mapping[str, Sequence[Any]], props: Sequence[str]) -> list[str]:
    return [a.lower() for prop in props for _, v in values.get(prop) or ()
            if isinstance(v, str) for a in _ADDRESS.findall(v)]


def _party_keys(address: str, salt: bytes) -> list[str]:
    """The keys a counterparty address is known by: itself, and its domain unless that
    is a shared mail provider's (one gmail writer says nothing of another)."""
    from ontology.mentions import shared_mail_domain
    domain = address.rsplit("@", 1)[1]
    return [_key(address, salt)] + ([] if shared_mail_domain(domain)
                                    else [_key("@" + domain, salt)])


def _message_key(values: Mapping[str, Sequence[Any]], salt: bytes) -> str | None:
    for prop in MESSAGE_KEY_PROPERTIES:
        for _, value in values.get(prop) or ():
            if isinstance(value, str) and value.strip():
                return _key(value, salt)
    return None


def _traits(values: Mapping[str, Sequence[Any]], internal: frozenset[str], linked: int,
            salt: bytes) -> dict[str, Any]:
    """What the batch knows of a message before reading it (never shown as text): its
    sender's keys, whether the sender is a colleague (an address of the tenant's own
    domains) or automated (``capture_mail.automated_sender``), how many business records
    it names (its mention links)."""
    from integrations.capture_mail import automated_sender
    senders = _addresses(values, SENDER_PROPERTIES)
    out: dict[str, Any] = {"linked": linked}
    if senders:
        sender = senders[0]
        out.update(sender=_key(sender, salt), parties=_party_keys(sender, salt),
                   internal=sender.rsplit("@", 1)[1] in internal,
                   automated=automated_sender(sender))
    return out


def learn_correspondents(ws: sqlite3.Connection, values: Mapping[str, Mapping[str, Any]],
                         internal: frozenset[str], *, now: str, salt: bytes,
                         sender_excluded: Callable[[str], bool]) -> int:
    """Count, by digest, every outside address and domain a colleague wrote to in these
    messages (``correspondents``): who the company answers is who matters to it. A
    message whose sender the people exclusion may cover teaches nothing (fail closed)."""
    counts: Counter = Counter()
    for vals in values.values():
        senders = _addresses(vals, SENDER_PROPERTIES)
        if not senders or senders[0].rsplit("@", 1)[1] not in internal \
                or sender_excluded(senders[0]):
            continue
        for address in set(_addresses(vals, RECIPIENT_PROPERTIES)):
            if address.rsplit("@", 1)[1] not in internal:
                counts.update(_party_keys(address, salt))
    for key, n in counts.items():
        ws.execute("INSERT INTO correspondents VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                   "SET n = n + excluded.n, at = excluded.at", (key, n, now))
    return sum(1 for _ in counts)


def _linked(conn: sqlite3.Connection, ids: Sequence[str]) -> Counter:
    """message id -> how many business records its current mention links name."""
    out: Counter = Counter()
    try:
        for part in _chunks(list(ids)):
            for (oid,) in conn.execute(
                    "SELECT from_object FROM links WHERE superseded_at IS NULL AND "
                    "link_type LIKE '%.mentions%' AND from_object IN (SELECT value FROM "
                    "json_each(?))", (json.dumps(list(part)),)):
                out[oid] += 1
    except sqlite3.Error:
        pass   # a layer without links: nothing named
    return out


def discover(ws: sqlite3.Connection, store: Path, *, now: dt.datetime,
             guard: Any = None, excluded: Callable[[Any, str], bool] | None = None,
             window_hours: float = WINDOW_HOURS,
             sender_excluded: Callable[[Any, str], bool] | None = None) -> dict[str, Any]:
    """Queue the changes of every delta layer this watcher has not looked at (module doc,
    step 1); counts only."""
    from ontology.delta import STAMP_PROPERTIES
    from . import truth_check

    excluded = excluded or (lambda g, h: truth_check.excluded_hit(g, {"handle": h}))
    sender_excluded = sender_excluded or (
        lambda g, a: truth_check.excluded_hit(g, {"header": a}))
    try:
        internal = frozenset(d.lower() for d in guard.known_domains()) \
            if guard is not None else frozenset()
    except Exception:  # noqa: BLE001 - a guard that cannot say: no colleague is known
        internal = frozenset()
    counts: Counter = Counter()
    postgres = _postgres_changes(ws, _iso(now))
    if postgres is not None:
        digest, layers, open_layer, owned_of = postgres
    else:
        (digest, layers), open_layer, owned_of = _pointer(store), _ro, _layer_owned
    fresh = [layer for layer in layers if not ws.execute(
        "SELECT 1 FROM layers WHERE store = ? AND base_digest = ?",
        (layer, digest)).fetchone()]
    unlearned = [layer for layer in layers if not ws.execute(
        "SELECT 1 FROM learned WHERE store = ? AND base_digest = ?",
        (layer, digest)).fetchone()]
    if not fresh and not (unlearned and guard is not None and internal):
        return {"layers": 0, **counts}
    since = _iso(now - dt.timedelta(hours=window_hours))
    newest: dict[str, tuple[str, sqlite3.Row]] = {}
    for layer in fresh:   # newest first: an object is the newest layer's that owns it
        with open_layer(layer) as conn:
            owned = owned_of(conn, layer)
            for part in _chunks(owned):
                for row in conn.execute(_CURRENT, (json.dumps(list(part)),)):
                    newest.setdefault(row["object_id"], (layer, row))
    counts["owned"] = len(newest)
    now_text = _iso(now)
    salt = key_salt(ws)
    # what the layers' messages teach of who the company writes to, learned in the same
    # transaction that marks the layers seen (a run that fails teaches nothing twice)
    # every current layer not learned from yet, whether or not it is new to the queue (a
    # store whose salt is new learns the layers it had already looked at)
    teach: dict[str, dict[str, list[Any]]] = {}
    if guard is not None and internal:
        for layer in unlearned:
            with open_layer(layer) as conn:
                owned = owned_of(conn, layer)
                mine = [row["object_id"] for part in _chunks(owned)
                        for row in conn.execute(_CURRENT, (json.dumps(list(part)),))
                        if is_message(row["l1_handle"]) and "#" not in row["l1_handle"]
                        and not excluded(guard, row["l1_handle"])]
                teach.update(_values_of(conn, mine,
                                        SENDER_PROPERTIES + RECIPIENT_PROPERTIES))
    held = {r["object_id"]: r for part in _chunks(sorted(newest)) for r in ws.execute(
        "SELECT object_id, digest, props FROM seen WHERE object_id IN (SELECT value FROM "
        "json_each(?))", (json.dumps(list(part)),))}
    served: dict[str, str] = {}
    # the served generation's digests and values (Postgres alone: nothing is known before
    # the first look; its generation's file is never opened, even while it is still
    # there, so the file can be retired: ``sqlite-retire`` step 3)
    with (closing(_ro(store)) if postgres is None
          else closing(sqlite3.connect(":memory:"))) as base:
        if postgres is not None:
            base.execute("CREATE TABLE object_versions (object_id, digest, superseded_at)")
            base.execute("CREATE TABLE object_values (object_id, property, value_type, value, "
                         "superseded_at)")
        for part in _chunks([o for o in newest if o not in held]):
            for row in base.execute(
                    "SELECT object_id, digest FROM object_versions WHERE object_id IN "
                    "(SELECT value FROM json_each(?)) AND superseded_at IS NULL",
                    (json.dumps(list(part)),)):
                served[row[0]] = row[1]
        wanted: dict[str, list[str]] = defaultdict(list)
        for oid, (layer, row) in newest.items():
            prior = held[oid]["digest"] if oid in held else served.get(oid)
            handle = row["l1_handle"]
            if prior == row["digest"]:
                counts["unchanged"] += 1
            elif "#" in handle:
                counts["part_of_message"] += 1
            elif is_message(handle) and (guard is None or excluded(guard, handle)):
                counts["people_withheld"] += 1
            elif row["recorded_at"] < since:   # captured before the window: not news
                counts["old_capture"] += 1
            else:
                wanted[layer].append(oid)
        changed_from_served = [oid for ids in wanted.values() for oid in ids
                               if oid not in held and oid in served]
        before = {oid: _props(vals) for oid, vals in _values(base, changed_from_served).items()}
    queued = 0
    threads: dict[str, tuple[str, str]] = {r["thread"]: (r["object_id"], r["event_at"])
                                           for r in ws.execute(
        "SELECT object_id, thread, event_at FROM queue WHERE thread IS NOT NULL")}
    ws.execute("BEGIN IMMEDIATE")
    try:
        if teach:
            counts["correspondents"] += learn_correspondents(
                ws, teach, internal, now=now_text, salt=salt,
                sender_excluded=lambda a: sender_excluded(guard, a))
        for layer, ids in wanted.items():
            with open_layer(layer) as conn:
                values = _values(conn, ids)
                linked = _linked(conn, [o for o in ids if is_message(newest[o][1]["l1_handle"])])
            for oid in ids:
                row = newest[oid][1]
                vals = values.get(oid, {})
                props = _props(vals)
                old = (json.loads(held[oid]["props"] or "{}") if oid in held
                       else before.get(oid))
                change = "new" if (oid not in held and oid not in served) else "changed"
                ws.execute("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?)",
                           (oid, row["digest"], json.dumps(props), now_text))
                changed: list[str] = []
                if change == "changed":
                    changed = sorted(p for p in set(props) | set(old or {})
                                     if (old or {}).get(p) != props.get(p)
                                     and p not in STAMP_PROPERTIES)
                    if old is not None and not changed:
                        counts["restamped"] += 1
                        continue
                event = _event_at(vals, row["recorded_at"], now_text)
                if change == "new" and event < since[:19]:
                    counts["old_news"] += 1
                    continue
                message = is_message(row["l1_handle"])
                key = _message_key(vals, salt) if message else None
                if key is not None:   # another mailbox's copy of a message already queued
                    held_by = ws.execute("SELECT object_id FROM message_keys WHERE key = ?",
                                         (key,)).fetchone()
                    if held_by is not None and held_by[0] != oid:
                        counts["copy"] += 1
                        continue
                    ws.execute("INSERT OR REPLACE INTO message_keys VALUES (?, ?, ?)",
                               (key, oid, now_text))
                thread = _thread(vals) if message else None
                if thread is not None and thread in threads:
                    other, at = threads[thread]
                    if at >= event:
                        counts["same_thread"] += 1
                        continue
                    ws.execute("DELETE FROM queue WHERE object_id = ?", (other,))
                    counts["same_thread"] += 1
                if thread is not None:
                    threads[thread] = (oid, event)
                ws.execute(
                    "INSERT OR REPLACE INTO queue (object_id, object_type, handle, change, "
                    "changed, event_at, since, thread, queued_at, traits) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (oid, row["object_type"], row["l1_handle"], change, json.dumps(changed),
                     event, None if change == "new" else since, thread, now_text,
                     json.dumps(_traits(vals, internal, linked.get(oid, 0), salt))
                     if message else None))
                queued += 1
        for layer in fresh:
            ws.execute("INSERT OR REPLACE INTO layers VALUES (?, ?, ?)",
                       (layer, digest, now_text))
        if postgres is not None and fresh:   # one mark per Postgres: the newest batch
            for table in ("layers", "learned"):
                ws.execute(f"DELETE FROM {table} WHERE base_digest = ? AND store LIKE "
                           "'postgres:%' AND store <> ?", (digest, fresh[0]))
        if guard is not None and internal:
            for layer in unlearned:
                ws.execute("INSERT OR REPLACE INTO learned VALUES (?, ?, ?)",
                           (layer, digest, now_text))
        ws.execute("DELETE FROM message_keys WHERE at < ?",
                   (_iso(now - dt.timedelta(days=ITEM_DAYS)),))
        ws.execute("COMMIT")
    except BaseException:
        ws.execute("ROLLBACK")
        raise
    return {"layers": len(fresh), "queued": queued, **counts}


# --------------------------------------------------------------------------- batch

def _reads(row: Mapping[str, Any]) -> int:
    return 2 if row["change"] == "changed" and not is_message(row["handle"]) else 1


def traits_of(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("traits") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def known_parties(ws: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """The party keys of these messages' senders the company has written to."""
    keys = sorted({k for r in rows for k in traits_of(r).get("parties") or ()})
    return {r[0] for part in _chunks(keys) for r in ws.execute(
        "SELECT key FROM correspondents WHERE key IN (SELECT value FROM json_each(?))",
        (json.dumps(list(part)),))}


def _tier(row: Mapping[str, Any], known: set[str]) -> str:
    """Where a message stands in the batch (``TIERS``): its sender is someone the company
    writes to, it names business records, it is a colleague's, another person's, or an
    automated sender's. Never a filter: it decides only what is read first."""
    traits = traits_of(row)
    if traits.get("automated"):
        return "automated"
    if not traits.get("internal") and known & set(traits.get("parties") or ()):
        return "correspondent"
    if traits.get("linked"):
        return "names_records"
    return "internal" if traits.get("internal") else "other"


def _balanced(rows: Sequence[dict[str, Any]], budget: int, origin: Mapping[str, str],
              known: set[str] | None = None) -> list[dict[str, Any]]:
    """At most ``budget`` reads of ``rows`` (newest first): ``SHARES`` of it to messages,
    to the schema's curated business records and to its generated source mirrors, a
    share one stream leaves unused going to the others; messages are taken by their tier
    (``_tier``), each tier the one that ages out first; records are taken round-robin
    over their types, the type with the fewest changes first (one change among thousands
    of a bulk recompute says more), each type's newest first."""
    streams: dict[str, list[dict[str, Any]]] = {"message": [], "curated": [], "mirror": []}
    for row in rows:
        stream = "message" if is_message(row["handle"]) else (
            "mirror" if origin.get(row["object_type"]) == "generated" else "curated")
        streams[stream].append(row)
    known = known or set()
    for row in streams["message"]:
        row["tier"] = _tier(row, known)
    # by tier, and within one the message that ages out first (a customer's mail of
    # yesterday is read before it is lost, today's still has its day)
    streams["message"].sort(key=lambda r: (TIERS.index(r["tier"]), r["event_at"]))
    for name in ("curated", "mirror"):
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in streams[name]:
            by_type[row["object_type"]].append(row)
        order = sorted(by_type, key=lambda t: (len(by_type[t]), t))
        mixed, depth = [], 0
        while len(mixed) < len(streams[name]):
            mixed += [by_type[t][depth] for t in order if depth < len(by_type[t])]
            depth += 1
        streams[name] = mixed
    taken: list[dict[str, Any]] = []
    left = budget
    for name, share in SHARES:
        quota = max(1, round(budget * share)) if streams[name] else 0
        while streams[name] and quota >= _reads(streams[name][0]) and left >= _reads(
                streams[name][0]):
            row = streams[name].pop(0)
            quota -= _reads(row)
            left -= _reads(row)
            taken.append(row)
    for name, _share in SHARES:   # what a stream left unused goes to the others
        while streams[name] and left >= _reads(streams[name][0]):
            row = streams[name].pop(0)
            left -= _reads(row)
            taken.append(row)
    return taken


def pick(ws: sqlite3.Connection, *, now: dt.datetime, max_reads: int = MAX_READS,
         window_hours: float = WINDOW_HOURS, bulk_min: int = BULK_MIN,
         origin: Mapping[str, str] | None = None,
         message_bulk_min: int = MESSAGE_BULK_MIN) -> dict[str, Any]:
    """This run's batch from the queue (module doc, step 2): ``entries`` read one by one
    (``_balanced``), ``bulk`` groups (one line, one sample read each) and the queue
    entries it takes; the others stay queued. Aged entries leave the queue
    (``aged_out``). ``origin``: object type -> ``curated`` or ``generated`` (the schema's
    ``origin_of_type``)."""
    since = _iso(now - dt.timedelta(hours=window_hours))[:19]
    aged = ws.execute("DELETE FROM queue WHERE event_at < ? AND change = 'new' AND "
                      "claimed IS NULL", (since,)).rowcount
    aged += ws.execute("DELETE FROM queue WHERE queued_at < ? AND claimed IS NULL",
                       (since,)).rowcount
    rows = [dict(r) for r in ws.execute("SELECT * FROM queue WHERE claimed IS NULL ORDER BY "
                                        "event_at DESC, object_id")]
    known = known_parties(ws, [r for r in rows if is_message(r["handle"])])
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if is_message(row["handle"]):
            traits = traits_of(row)
            # a sender's stream: automated, or no one the company writes to nor a colleague
            if traits.get("sender") and (traits.get("automated") or (
                    not traits.get("internal")
                    and not known & set(traits.get("parties") or ()))):
                groups[("message", traits["sender"])].append(row)
        elif row["change"] == "changed":
            groups[(row["object_type"], row["changed"])].append(row)
        else:
            groups[(row["object_type"], "new")].append(row)
    bulky = {key for key, members in groups.items()
             if len(members) > (message_bulk_min if key[0] == "message" else bulk_min)}

    def group_of(r: Mapping[str, Any]) -> tuple:
        if is_message(r["handle"]):
            return ("message", traits_of(r).get("sender"))
        return (r["object_type"], r["changed"] if r["change"] == "changed" else "new")
    single = [r for r in rows if group_of(r) not in bulky]
    bulk = [{"object_type": groups[key][0]["object_type"],
             "change": "new" if key[0] == "message" or key[1] == "new" else "changed",
             "changed": [] if key[0] == "message" or key[1] == "new" else json.loads(key[1]),
             "messages": key[0] == "message",
             "automated": bool(traits_of(groups[key][0]).get("automated")),
             "count": len(groups[key]), "sample": groups[key][0],
             "members": [m["object_id"] for m in groups[key]]}
            for key in sorted(bulky, key=lambda k: (-len(groups[k]), str(k)))]
    bulk = bulk[:max(0, max_reads // 6)]   # one sample read each
    entries = _balanced(single, max_reads - len(bulk), origin or {}, known)
    taken = [r["object_id"] for r in entries] + [m for g in bulk for m in g["members"]]
    return {"entries": entries, "bulk": bulk, "taken": taken, "aged_out": aged,
            "waiting": len(rows) - len(taken)}


def reads_for(batch: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The reads the session makes before its first turn, as the watcher's reader."""
    out: list[tuple[str, dict[str, Any]]] = []
    for row in batch["entries"]:
        if is_message(row["handle"]):
            out.append(("plx.read", {"citation": row["handle"], "max_chars": READ_CHARS}))
            continue
        changed = json.loads(row["changed"]) if isinstance(row["changed"], str) \
            else list(row["changed"])
        args: dict[str, Any] = {"object_id": row["object_id"], "response_format": "concise"}
        if changed:
            args["properties"] = changed[:50]
        out.append(("plx.object", args))
        if row["change"] == "changed" and changed and row.get("since"):
            out.append(("plx.object", {**args, "as_known_at": row["since"]}))
    for group in batch["bulk"]:
        sample = group["sample"]
        if is_message(sample["handle"]):
            out.append(("plx.read", {"citation": sample["handle"], "max_chars": READ_CHARS}))
            continue
        args = {"object_id": sample["object_id"], "response_format": "concise"}
        if group["changed"]:
            args["properties"] = group["changed"][:50]
        out.append(("plx.object", args))
    return out


def flagged(inbox: Path, ws: sqlite3.Connection, object_ids: Sequence[str]
            ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(open detector events about these records, this watcher's open items about them):
    kinds, subjects and titles only, for the task's dedup."""
    events: list[dict[str, str]] = []
    if inbox.is_file() and object_ids:
        with closing(_ro(inbox)) as conn:
            ids = json.dumps(sorted(set(object_ids)))
            for row in conn.execute(
                    "SELECT DISTINCT g.kind, g.object_id FROM gaps g WHERE g.state = 'open' "
                    "AND (g.object_id IN (SELECT value FROM json_each(?)) OR g.gap_id IN "
                    "(SELECT gap_id FROM gap_cites WHERE object_id IN (SELECT value FROM "
                    "json_each(?)))) ORDER BY g.object_id, g.kind LIMIT 200", (ids, ids)):
                events.append({"kind": row[0], "object_id": row[1]})
    # this watcher's open items about these records, then its other open items newest
    # first: one matter reaches the company through many records and mailboxes
    own: list[dict[str, str]] = []
    for r in ws.execute(
            "SELECT i.title, i.subject, max(i.subject IN (SELECT value FROM json_each(?)) OR "
            "c.object_id IN (SELECT value FROM json_each(?))) AS here FROM items i LEFT JOIN "
            "item_cites c ON c.item_id = i.item_id WHERE i.state = 'open' GROUP BY "
            "i.item_id ORDER BY here DESC, i.last_seen DESC LIMIT 40",
            (json.dumps(list(object_ids)), json.dumps(list(object_ids)))):
        own.append({"title": r["title"], "subject": r["subject"]})
    return events, own


# --------------------------------------------------------------------------- task

INSTRUCTION = (
    "You are the operations lead of a manufacturer reading what just changed in the "
    "company's systems: new mail and chat, new and changed records. You brief the chief "
    "executive. Raise only what genuinely needs a decision or action now: something that "
    "threatens or changes a customer commitment, supply, quality, cash or margin; a "
    "customer, supplier or other outside party waiting on an answer from us, above all one "
    "who is chasing; new business offered to us; money owed that is not being collected "
    "or paid; or two records that contradict each other in a way that changes what "
    "someone must do. Not items: routine traffic, marketing and cold pitches, automated "
    "notices that report no business event, IT and account notices, and the next normal "
    "step of work the person it is addressed to is already doing (a quote, invoice, "
    "confirmation or document sent to the colleague who asked for it), unless something "
    "about it is late, contradicts a record or changes money, quantities or dates. Raise "
    "one item per real-world matter: when several changes, mailboxes or one of your open "
    "items below already cover the same matter with the same counterparty, raise it at most "
    "once and only for what is new. Most batches hold nothing worth raising; raising "
    "nothing is a good answer.")

GATE_RULE = (
    "Each item is a proposal with evidence, never your own assertion. For each item, "
    "submit it to the Workbench's answer gate on its own: call plx.case_get with only "
    "{\"answer\": {\"question\": \"<the item's title>\", \"text\": \"<one or two plain "
    "sentences saying what happened>\", \"claims\": [...]}}. Every identifier, name, date, "
    "quantity and amount your text states must be a claim: {\"name\": \"<snake_case>\", "
    "\"citation\": \"<a handle a tool returned in this session>\", \"field\": "
    "\"row.<column>\"} (the gate reads the value from that field), or on a message's or "
    "file's text {\"name\", \"citation\", \"field\": <the text field plx.read returned it "
    "under: \"row.body_text\" for a mail, \"row.content_text\" for a file>, \"quote\": "
    "\"<the passage as written>\"} (\"text\" is "
    "only for a page citation plx.read named; a figure in a quote also gives \"value\": "
    "the figure as printed). Cite the records the item is about. The batch reads above are "
    "for spotting what matters: before you submit an item, read each record you will cite "
    "yourself (plx.read with only its citation for a message or document, plx.object with "
    "only its object_id for a record: not the calls made for you), since the "
    "gate accepts only citations your own reads in this session returned; the gate allows "
    "one repair in the whole session, so get each submission right. Write the text only "
    "from what your claims carry: every name of a person or company, figure, code and "
    "date in it must stand in a claimed line or quote, or its sentence is dropped, and an "
    "item none of whose sentences stand is not shown. A claim the gate refuses is not part of the item; outcome \"repair\" lists "
    "refused claims once: fix them and submit again. An item whose claims all fail is "
    "dropped: say nothing about it. Never send, propose or draft anything to anyone "
    "outside: the next step you suggest is for a colleague to decide.")

OUTPUT_RULE = (
    "After the gate has decided every item, end with one json block and nothing after "
    "it: {\"items\": [{\"title\": \"<exactly the question you submitted>\", \"subject\": "
    "\"<object_id of the record the item is about, one you read>\", \"why\": \"<one or "
    "two plain sentences: why it needs attention now; every name, number, code and date in "
    "them must stand in a line your passed claims quote or state, so claim the passage "
    "that carries them>\", "
    "\"next_step\": \"<one short suggestion for a colleague>\", \"departments\": "
    "[<department codes who should act: {codes}>], \"priority\": \"high|medium|low\", "
    "\"rests_on\": \"<one of {kinds}: what the item's claim rests on: order_open when it "
    "matters only while a purchase order is still open or undelivered, else the closest, "
    "else other>\"}]}}. At most {most} items; {{\"items\": []}} when nothing needs "
    "attention.")


def task(batch: Mapping[str, Any], events: Sequence[Mapping[str, str]],
         own: Sequence[Mapping[str, str]], *, now: dt.datetime, departments: Iterable[str]
         ) -> str:
    """The session's whole task for one batch (its reads follow, made in the session)."""
    lines = [INSTRUCTION, "",
             f"Now: {_iso(now)[:16]}Z. The changes of this batch, each read for you below "
             "as this session's reader (\"changed\" lists the properties that changed; a "
             "second read shows the values before, as known at the time given):"]
    for n, row in enumerate(batch["entries"], start=1):
        changed = json.loads(row["changed"]) if isinstance(row["changed"], str) \
            else row["changed"]
        what = row["change"] + (f" ({', '.join(changed[:12])})" if changed else "")
        lines.append(f"{n}. {row['object_type']} {row['object_id']}: {what}, "
                     f"{row['event_at'][:16]}{_about(row)}")
    for group in batch["bulk"]:
        if group.get("messages"):
            lines.append(f"- {group['count']} {group['object_type']} messages from one "
                         f"{'automated ' if group.get('automated') else ''}sender the "
                         "company does not write to (one stream; one sample read: "
                         f"{group['sample']['object_id']})")
            continue
        what = (f"changed {', '.join(group['changed'][:8])}" if group["changed"]
                else "new")
        lines.append(f"- {group['count']} {group['object_type']} records {what} together "
                     f"(one bulk run; one sample read: {group['sample']['object_id']})")
    if events:
        lines += ["", "Already flagged by the rule-based checks (do not raise these again "
                  "unless a change here makes them materially different):"]
        lines += [f"- {e['kind']} on {e['object_id']}" for e in events[:60]]
    if own:
        lines += ["", "Already raised by you earlier and still open (do not raise again):"]
        lines += [f"- {o['title']} ({o['subject']})" for o in own[:40]]
    lines += ["", "Read more (plx.read, plx.object, plx.around, plx.find) only where an "
              "item needs it: the thread a message continues, the order or customer it "
              "names, the other system's record it contradicts.", "", GATE_RULE, "",
              OUTPUT_RULE.replace("{codes}", ", ".join(sorted(departments)))
              .replace("{kinds}", ", ".join(CLAIM_KINDS)).replace("{most}", str(MAX_ITEMS))
              .replace("{{", "{").replace("}}", "}"), "", PROSE_STYLE_RULE]
    return "\n".join(lines)


_ABOUT = {"correspondent": "from someone the company writes to",
          "names_records": "names business records", "internal": "from a colleague",
          "other": "from someone the company has not written to",
          "automated": "from an automated sender"}


def _about(row: Mapping[str, Any]) -> str:
    """What the batch knows of a message's sender before reading it (``_tier``)."""
    tier = row.get("tier")
    if tier not in _ABOUT:
        return ""
    linked = traits_of(row).get("linked") or 0
    extra = (f"; names {linked} business record{'s' if linked != 1 else ''}"
             if linked and tier != "names_records" else "")
    return f" [{_ABOUT[tier]}{extra}]"


RETRY_RULE = (
    "An earlier review of new changes raised the item below, but the answer gate refused "
    "its claims ({refusals}), and that session had used its one repair. Decide it again on "
    "its own: read the records it rests on yourself, then submit it to the gate (one "
    "repair is allowed here), or drop it when the records do not hold it.")


def retry_task(title: str, retry: Mapping[str, Any], refusals: Sequence[str], *,
               departments: Iterable[str]) -> str:
    """The task of one refused item's own gate pass (``cycle``): the item as raised, the
    records it cited, the gate rule and the output rule for at most that one item."""
    lines = [INSTRUCTION, "", RETRY_RULE.replace("{refusals}", ", ".join(refusals)
                                                 or "no claim passed"), "",
             f"Title: {title}"]
    if "statement_unsupported" in refusals:
        lines[2] += (" statement_unsupported: claims passed, but no sentence of its text or "
                     "why stood on them (a name, figure or date in each was in no passed "
                     "claim): claim the passages that carry what it says.")
    for key, label in (("subject", "Subject record"), ("text", "Submitted text"),
                       ("why", "Why it was raised"), ("next_step", "Suggested next step")):
        if isinstance(retry.get(key), str) and retry[key].strip():
            lines.append(f"{label}: {retry[key].strip()[:1500]}")
    if retry.get("cited"):
        lines += ["Records it cited:", *(f"- {h}" for h in retry["cited"])]
    rule = (OUTPUT_RULE.replace("{codes}", ", ".join(sorted(departments)))
            .replace("{kinds}", ", ".join(CLAIM_KINDS)).replace("{most}", "1")
            .replace("{{", "{").replace("}}", "}"))
    return "\n".join(lines + ["", GATE_RULE.replace(
        "The batch reads above are for spotting what matters: before",
        "Before"), "", rule, "", PROSE_STYLE_RULE])


# --------------------------------------------------------------------------- proposals

def _plain(value: Any) -> Any:
    from . import answer_gate
    return answer_gate.plain(value)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def final_items(final_text: str) -> list[dict[str, Any]]:
    """The items of the session's final json block (none when it has none)."""
    found = _FENCE.findall(final_text or "")
    for raw in reversed(found):
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        items = value.get("items") if isinstance(value, dict) else None
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)][:MAX_ITEMS]
    return []


def cited_fields(claim: Mapping[str, Any]) -> dict[str, set[str | None]]:
    """Every record one claim cites or is computed from, with the fields it reads there:
    its citations, each value claim's citation and its inputs' (``of``), and the record it
    was reached through (``via``); a record cited without a field reads ``None``."""
    out: dict[str, set[str | None]] = {}

    def add(handle: Any, field: Any = None) -> None:
        if isinstance(handle, str) and handle:
            out.setdefault(handle, set()).add(field if isinstance(field, str) else None)
    for handle in claim.get("citations") or ():
        add(handle)
    add(claim.get("via"))
    for value in claim.get("value_claims") or ():
        if isinstance(value, Mapping):
            add(value.get("citation"), value.get("field"))
            add(value.get("via"))
            for given in value.get("of") or ():
                if isinstance(given, Mapping):
                    add(given.get("citation"), given.get("field"))
    return out


def _cited(claim: Mapping[str, Any]) -> list[str]:
    return list(cited_fields(claim))


def _refused_said(unsupported: Any) -> list[str]:
    from . import answer_gate
    return answer_gate.refused_said([u for u in unsupported or () if isinstance(u, Mapping)])


def submissions(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """question -> its last gate decision this session: ``{"outcome", "claims",
    "text", "unsupported"}`` (``claims``: the passed ones, each with its citations)."""
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        arguments = event.get("arguments")
        if event.get("type") != "tool_call" or event.get("name") != "plx.case_get" \
                or not isinstance(arguments, Mapping) \
                or not isinstance(arguments.get("answer"), Mapping):
            continue
        answer = arguments["answer"]
        question = answer.get("question")
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        if not isinstance(question, str) or not question.strip():
            continue
        claims = [c for c in result.get("claims") or () if isinstance(c, Mapping)]
        out[question.strip()] = {
            "outcome": result.get("outcome") if not event.get("is_error") else "refused",
            "claims": [{"id": c.get("id"), "text": c.get("text"), "citations": _cited(c),
                        "fields": {h: sorted(f for f in fs if f) or None
                                   for h, fs in cited_fields(c).items()},
                        **{k: c[k] for k in ("value_claims", "checked", "via") if k in c}}
                       for c in claims],
            "text": answer.get("text") if isinstance(answer.get("text"), str) else "",
            "unsupported": len(result.get("unsupported_claims") or ()),
            # why the gate refused what it refused (reason codes only)
            "refusals": sorted({str(u.get("reason")) for u in result.get(
                "unsupported_claims") or () if isinstance(u, Mapping)}),
            # what the refused claims said: never let back in reworded (the Ask's rule)
            "refused_said": _refused_said(result.get("unsupported_claims")),
            "submitted": len(answer.get("claims") or ())
            if isinstance(answer.get("claims"), list) else 0,
            # the records the submission cited (an item given its own gate pass reads them)
            "cited": list(dict.fromkeys(
                h for c in answer.get("claims") or () if isinstance(c, Mapping)
                for h in _cited({"citations": c.get("citations"),
                                 "value_claims": [c]}))) [:MAX_RETRY_CITES]}
    return out


# A sentence's names (``named``): capitalised words and addresses. Figures, dates, codes
# and identifiers are the Ask's own rule (``answer_gate.final_reply``, ``screened``).
_TOKEN = re.compile(r"[A-Za-z0-9][\w@.+'/&-]*")
_NEGATORS = re.compile(r"\b(?:not|no|never|none|nothing|without|cannot)\b|n't\b", re.I)
# Words a sentence may start with that state nothing (grammar, not a list of cases).
_PLAIN_STARTS = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "it", "its", "they", "their", "we",
    "our", "he", "she", "his", "her", "there", "if", "when", "once", "until", "unless",
    "so", "because", "as", "both", "each", "every", "any", "all", "someone", "nobody",
    "while", "since", "after", "before", "then", "also", "otherwise"})


def _cited_text(claims: Sequence[Mapping[str, Any]]) -> str:
    """Every line, value and quote of the passed claims, and the system and table each
    citation names (``api://sage.gl_perr/...``: "sage.gl_perr"), lower case: what they
    hold, so a reason may say which system a cited record is in."""
    parts: list[str] = []
    for claim in claims:
        parts.append(str(_plain(claim.get("text")) or ""))
        for handle in claim.get("citations") or ():
            if isinstance(handle, str) and "://" in handle:
                scheme, _, rest = handle.partition("://")
                parts.append(f"{scheme} {rest.split('/', 1)[0] if scheme == 'api' else ''}")
        for value in claim.get("value_claims") or ():
            if isinstance(value, Mapping):
                for key in ("value", "quote"):
                    if value.get(key) is not None:
                        parts.append(str(_plain(value[key])))
    return "\n".join(parts).lower()


def _names(sentence: str) -> list[str]:
    """The names a sentence states: every address, and every capitalised word but a plain
    sentence start (``_PLAIN_STARTS``) and but one with a digit (a figure or code, the
    Ask's rule judges it whole)."""
    out = []
    for n, match in enumerate(_TOKEN.finditer(sentence)):
        token = re.sub(r"'s$", "", match.group(0).rstrip(".,;:'/&-"))
        if not token or any(c.isdigit() for c in token):
            continue
        if "@" in token or (token[0].isupper()
                            and not (n == 0 and token.lower() in _PLAIN_STARTS)):
            out.append(token)
    return out


def named(sentence: str, cited: str) -> bool:
    """Whether every name ``sentence`` states (``_names``) stands in the passed claims'
    own lines, values or quotes (``cited``, lower case) as a whole word, and it negates
    nothing unless they do."""
    if _NEGATORS.search(sentence) and not _NEGATORS.search(cited):
        return False
    return all(re.search(r"(?<![\w])" + re.escape(token.lower()) + r"(?![\w])", cited)
               for token in _names(sentence))


_SHORT_WORD_END = re.compile(r"(?:^|\s)[A-Z][a-z]{0,3}\.$")


def _sentences(text: str) -> list[str]:
    """Sentences; a period after a short capitalised word followed by a figure ("Jan. 30th",
    "No. 5") ends none."""
    out: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+", text):
        part = part.strip()
        if not part:
            continue
        if out and part[0].isdigit() and _SHORT_WORD_END.search(out[-1]):
            out[-1] += " " + part
        else:
            out.append(part)
    return out


_PERCENT = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?%")


def quoted(claims: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The figures, dates and code numbers the passed claims' own lines and quotes print,
    each with the currency or unit printed beside it, as one passed claim the Ask's final
    screen reads (``answer_gate.final_reply``): a reason may say what a cited passage says,
    as the passage says it, by the very rule the Ask's reply is screened with (whole
    normalised numbers, the currency and unit beside them, dates by their day)."""
    from . import claim_checks as cc
    values: list[dict[str, Any]] = []
    for claim in claims:
        texts = [_plain(claim.get("text"))]
        for value in claim.get("value_claims") or ():
            if isinstance(value, Mapping):
                texts += [_plain(value.get("quote")), _plain(value.get("value"))]
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                continue
            money = {n: w for n, _s, _e, w in cc.currency_words(text)}
            units = {n: w for n, _s, _e, w in cc.unit_words(text)}
            for written, number in cc.prose_numbers(text):
                if written.rstrip().endswith("%"):
                    continue   # a percent: below, at percent scale
                values.append({"value": str(number),
                               **({"currency": money[number]} if number in money else {}),
                               **({"unit": units[number]} if number in units else {})})
            for match in _PERCENT.finditer(text):
                number = cc.to_decimal(match.group(1))
                if number is not None:
                    values.append({"value": str(number), "unit": "%"})
            for _written, moment, _flag in cc.written_dates(text):
                if moment and isinstance(moment[0], str):
                    values.append({"value": moment[0]})
            for label in cc.labels(text):
                values += [{"value": str(n)} for n in cc.label_numbers(label.group(0))]
    return {"id": "quoted", "check": {"status": "passed", "unit_basis": "quoted"},
            "value_claims": values}


def screened(text: Any, claims: Sequence[Mapping[str, Any]], *, fallback: bool = False,
             suggestion: bool = False, refused: Sequence[str] = ()) -> str:
    """``text`` screened against ``claims``, sentence by sentence, by the Ask's own final
    screen (``answer_gate.final_reply``, no asker-given identifier): every figure, date,
    code, identifier and currency or unit it writes must be one a passed claim states or
    its cited passage prints (``quoted``), it must deny no passed value, and no word of a
    claim the gate refused (``refused``, ``answer_gate.refused_said``) may come back
    reworded; a unit or currency nothing checked is dropped as the Ask drops it. A finding
    (not a ``suggestion``) must also name no one and nothing the cited lines do not
    (``named``). Any other sentence is dropped. Nothing left: the claims' own lines with
    ``fallback``, else nothing."""
    from . import answer_gate
    lines = [str(_plain(c.get("text"))) for c in claims
             if isinstance(_plain(c.get("text")), str)]
    if not isinstance(text, str) or not text.strip() or not claims:
        return "; ".join(lines) if fallback else ""
    cited = _cited_text(claims)
    backing = [*claims, quoted(claims)]
    kept = []
    for sentence in _sentences(text):
        reply = answer_gate.final_reply(sentence, backing, question="",
                                        refused=list(refused))
        if reply.get("removed_sentences") or not str(reply.get("text") or "").strip():
            continue
        if not suggestion and not named(sentence, cited):
            continue
        kept.append(str(reply["text"]).strip())
    if kept:
        return " ".join(kept)
    return "; ".join(lines) if fallback else ""


def proposals(run: Any, *, departments: Iterable[str]) -> list[dict[str, Any]]:
    """The items one session raised, each with its gate decision: ``gate`` ``passed``
    (answered with at least one claim) or why not; never an item the gate did not see."""
    decisions = submissions(run.events)
    codes = set(departments)
    items = final_items(run.final_text)
    named = {str(i.get("title") or "").strip() for i in items}
    # a submission the final block does not list (a session cut short, or an item the
    # gate refused that the agent then dropped) is still an item, decided as the gate did
    for question in decisions:
        if question not in named:
            items.append({"title": question})
    out = []
    for item in items[:MAX_ITEMS * 2]:
        title = str(item.get("title") or "").strip()[:300]
        decision = decisions.get(title)
        claims = decision["claims"] if decision else []
        if decision is None:
            gate = "not_submitted"
        elif decision["outcome"] != "answered" or not claims:
            gate = f"not_passed:{decision['outcome']}"
        else:
            gate = "passed"
        # one evidence group per claim: every record it cites or is computed from
        groups = [c["citations"] for c in claims if c["citations"]]
        fields = {h: f for c in claims for h, f in (c.get("fields") or {}).items()}
        # the item's own statement (its text or why) must stand on what the gate passed:
        # when the screen keeps no sentence of either, the passed claims are fragments
        # that do not carry what the item says (a refused claim held its point)
        text = screened(decision["text"], claims, refused=decision["refused_said"]) \
            if decision else ""
        why = screened(item.get("why"), claims, refused=decision["refused_said"]) \
            if claims else ""
        if gate == "passed" and not text and not why:
            gate = "not_passed:statement_unsupported"
        elif gate == "passed" and not text:
            text = screened(decision["text"], claims, fallback=True,
                            refused=decision["refused_said"])
        kind = item.get("rests_on") if item.get("rests_on") in CLAIM_KINDS else "other"
        priority = item.get("priority") if item.get("priority") in PRIORITIES else "medium"
        out.append({
            "title": title, "subject": item.get("subject") if isinstance(
                item.get("subject"), str) else None,
            "text": text,
            "why": why,
            "next_step": screened(item.get("next_step"), claims, suggestion=True,
                                  refused=decision["refused_said"]) if claims else "",
            "departments": [d for d in item.get("departments") or () if d in codes][:4],
            "priority": priority, "claim_kind": kind, "gate": gate,
            "claims": [{"id": c.get("id"), "text": _plain(c.get("text")),
                        "citations": c["citations"]} for c in claims],
            "groups": groups, "fields": fields,
            "refused_claims": decision["unsupported"] if decision else 0,
            "refusals": (decision["refusals"] + (
                ["statement_unsupported"] if gate.endswith("statement_unsupported") else [])
                if decision else []),
            # what an item the gate refused is decided again from, on its own pass
            **({"retry": {"text": decision["text"], "why": item.get("why"),
                          "next_step": item.get("next_step"), "cited": decision["cited"],
                          "subject": item.get("subject"),
                          "departments": item.get("departments"),
                          "priority": item.get("priority"),
                          "rests_on": item.get("rests_on")}}
               if decision and gate != "passed" else {})})
    return out


# --------------------------------------------------------------------------- placement

# Each delta layer's owned object ids by its file identity (path, inode, size, change
# time, WAL size: ``delta_overlay._layer_identity``), so a briefing read does not read
# every layer's list again; a rewritten layer has another identity.
_OWNED: dict[tuple, frozenset[str]] = {}
_OWNED_LOCK = threading.Lock()
_OWNED_KEPT = 256


class Places:
    """Where a cited handle's object is served now, with its audience and markings: the
    newest delta layer owning it, else the served store (``place``)."""

    def __init__(self, store: Path) -> None:
        self.store = Path(store)
        _digest, self.layers = _pointer(self.store)
        self._conns: dict[str, sqlite3.Connection] = {}
        self._owner: list[tuple[str, frozenset[str]]] | None = None   # newest first
        self._objects: dict[str, str | None] = {}   # handle -> the object it cites

    def _conn(self, path: str) -> sqlite3.Connection:
        if path not in self._conns:
            self._conns[path] = _ro(path)
        return self._conns[path]

    def _where(self, object_id: str) -> str:
        if self._owner is None:
            # Every layer's owned ids read once (not one query per layer for every cited
            # record), and kept across reads while the layer file is the same one.
            self._owner = [(layer, self._owned(layer)) for layer in self.layers]
        for layer, owned in self._owner:   # newest first: the newest owner wins
            if object_id in owned:
                return layer
        return str(self.store)

    def _owned(self, layer: str) -> frozenset[str]:
        from .delta_overlay import _layer_identity
        identity = _layer_identity(layer)
        with _OWNED_LOCK:
            kept = _OWNED.get(identity) if identity is not None else None
        if kept is not None:
            return kept
        owned = frozenset(row[0] for row in self._conn(layer).execute(
            "SELECT object_id FROM delta_owned"))
        if identity is not None and identity == _layer_identity(layer):
            with _OWNED_LOCK:
                if len(_OWNED) >= _OWNED_KEPT:
                    _OWNED.clear()
                _OWNED[identity] = owned
        return owned

    def object_of(self, handle: str) -> str | None:
        """The object a handle cites (exact, then without a page or field fragment)."""
        if handle in self._objects:
            return self._objects[handle]
        tries = [handle, handle.split("#page=", 1)[0], handle.split("#", 1)[0]]
        found = None
        for path in [*(layer for layer in self.layers), str(self.store)]:
            conn = self._conn(path)
            for candidate in dict.fromkeys(tries):
                row = conn.execute("SELECT object_id FROM object_versions WHERE l1_handle = ? "
                                   "LIMIT 1", (candidate,)).fetchone()
                if row is not None:
                    found = row[0]
                    break
            if found is not None:
                break
        self._objects[handle] = found
        return found

    def markings_of(self, handle: str, object_id: str, fields: Sequence[str] | None
                    ) -> str:
        """The markings a reader must hold to read what a claim reads of ``handle``: its
        version's own and those of the values it reads there (``fields``; none named, or
        none matched: every value of that version, fail closed), canonical; unreadable:
        a value no reader holds."""
        from ontology import store as ostore
        conn = self._conn(self._where(object_id))
        held: set[str] = set()
        texts = [r[0] for r in conn.execute(
            "SELECT markings FROM object_versions WHERE l1_handle = ? AND object_id = ?",
            (handle.split("#", 1)[0], object_id))]
        version = handle.split("#", 1)[0]
        if not texts:   # cited at a revision this store does not hold: its current one
            row = conn.execute("SELECT l1_handle, markings FROM object_versions WHERE "
                               "object_id = ? AND superseded_at IS NULL",
                               (object_id,)).fetchone()
            if row is None:
                return '["unreadable"]'
            version, texts = row[0], [row[1]]
        rows = []
        if fields:
            rows = [r[0] for r in conn.execute(
                "SELECT DISTINCT markings FROM object_values WHERE l1_handle = ? AND "
                "field_path IN (SELECT value FROM json_each(?))",
                (version, json.dumps(list(fields))))]
        if not rows:
            rows = [r[0] for r in conn.execute(
                "SELECT DISTINCT markings FROM object_values WHERE l1_handle = ?",
                (version,))]
        try:
            for text in [*texts, *rows]:
                held.update(json.loads(text or "[]"))
            return ostore.markings_text(held)
        except (ValueError, TypeError, ostore.StoreError):
            return '["unreadable"]'

    def place(self, object_id: str) -> dict[str, Any] | None:
        """``{"object_type", "audience_key", "markings"}`` of its current version where it
        is served now, or None (not served: never shown)."""
        row = self._conn(self._where(object_id)).execute(
            "SELECT o.object_type, o.audience_key, v.markings FROM objects o JOIN "
            "object_versions v ON v.object_id = o.object_id AND v.superseded_at IS NULL "
            "WHERE o.object_id = ?", (object_id,)).fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()


# --------------------------------------------------------------------------- truth

def _relation(object_id: str) -> str | None:
    """The mirror relation a Portal object id's record sits in (``truth_check._row_of``)."""
    from . import truth_check
    _type, binding, _key = truth_check.parse_object(object_id)
    name = binding.split(".")[1] if binding.startswith("portal.") and "." in binding else None
    if not name:
        return None
    return "public." + (name[3:] if name.startswith("fm_") else name)


def load_sources(state_dir: Path, object_ids: Iterable[str], *,
                 as_of: dt.date) -> Any:
    """The truth check's sources (``truth_check.Sources``): its own relations plus those of
    the records the items name; read-only."""
    from . import truth_check as tc
    relations = list(dict.fromkeys([*tc.RELATIONS, *filter(None, map(_relation, object_ids))]))
    return tc.Sources(portal=tc.mirror_rows(Path(state_dir) / tc.MIRROR, relations),
                      bc=tc.bc_records(Path(state_dir) / tc.BC_CAPTURE, tc.BC_SETS),
                      as_of=as_of)


def _test_marked(src: Any, object_id: str) -> list[dict[str, Any]]:
    from . import truth_check as tc
    item = tc._row_of(src, object_id)
    if item is None:
        return []
    texts = [str(v) for v in item["row"].values() if isinstance(v, (str, int))]
    hits = sorted({m.group(0) for text in texts for m in tc.TEST_MARK.finditer(text)})
    return [{"rule": "T1", "text": "the record carries test/UAT/demo marks: "
             + "; ".join(hits)[:200], "citations": [item["citation"]]}] if hits else []


def truth(src: Any, item: Mapping[str, Any], objects: Sequence[str]) -> dict[str, Any]:
    """The truth check of one item over the records it names (module doc, step 4):
    ``{"verdict", "contradicted", "reasons"}``; ``not_checkable`` when no rule of the
    check applies (it then passes: nothing contradicts it)."""
    from . import truth_check as tc
    subject = item["subject"]
    src.index()   # built whole first (a row lookup alone would leave it partial)
    reasons: list[dict[str, Any]] = []
    verdicts: list[str] = []
    contradicted = False
    for oid in objects:
        marks = _test_marked(src, oid)
        if marks:
            reasons += marks
            verdicts.append("test_record")
            contradicted = True
    for oid in [o for o in objects if o.startswith("PurchaseOrder:")]:
        verdict = tc.po_verdict(src, oid)
        reasons += verdict["reasons"]
        verdicts.append(verdict["verdict"])
        if verdict["verdict"] == "test_record" or (
                item["claim_kind"] == "order_open" and verdict["verdict"] in (
                    "received_elsewhere", "cancelled_elsewhere", "draft_twin")):
            contradicted = True
    claim = {"object_id": subject, "summary": item.get("text") or ""}
    rule = {"lot_near_expiry": tc.lot_verdict, "lot_on_hold": tc.batch_verdict,
            "quantity_conflict": tc.quantity_verdict}.get(item["claim_kind"])
    if rule is not None:
        verdict = rule(src, claim)
    elif item["claim_kind"] == "customer_order_at_risk" and any(
            o.startswith("PurchaseOrder:") for o in objects):
        # the cause it names (a purchase order) must still be open: none cited, no check
        verdict = tc.at_risk_verdict(src, claim, [{"object_id": o} for o in objects])
    else:
        verdict = None
    if verdict is not None:
        reasons += verdict.get("reasons") or []
        verdicts.append(verdict["verdict"])
        contradicted = contradicted or bool(verdict.get("contradicted"))
    if contradicted:
        name = "contradicted"
    elif any(v == "supported" for v in verdicts):
        name = "supported"
    elif verdicts:
        name = "unknown"
    else:
        name = "not_checkable"
    return {"verdict": name, "contradicted": contradicted, "checks": verdicts,
            "reasons": [{"rule": r.get("rule"), "text": str(r.get("text"))[:300],
                         "citations": list(r.get("citations") or ())[:4]}
                        for r in reasons][:8]}


# --------------------------------------------------------------------------- fold

def fingerprint(subject: str, groups: Sequence[Sequence[str]]) -> str:
    roots = sorted({handle_root(h) for group in groups for h in group})
    return hashlib.sha256(json.dumps([subject, roots]).encode("utf-8")).hexdigest()[:20]


def detector_covers(inbox: Path, subject: str, groups: Sequence[Sequence[str]]) -> bool:
    """An open detector event about ``subject`` already cites every record this item cites."""
    if not inbox.is_file():
        return False
    roots = {handle_root(h) for group in groups for h in group}
    with closing(_ro(inbox)) as conn:
        # a person's own mail (``proactive_commitments.OWNER_KINDS``) is shown to its owner
        # only: it covers nothing every other reader would see
        for (gap_id,) in conn.execute("SELECT gap_id FROM gaps WHERE state = 'open' AND "
                                      "object_id = ? AND kind <> 'needs_my_reply'",
                                      (subject,)):
            cited = {handle_root(r[0]) for r in conn.execute(
                "SELECT handle FROM gap_cites WHERE gap_id = ?", (gap_id,))}
            if roots and roots <= cited:
                return True
    return False


def fold(ws: sqlite3.Connection, items: Sequence[Mapping[str, Any]], *, run_id: int,
         now: dt.datetime) -> Counter:
    """Write this run's items: a passed, uncontradicted item is ``open`` (one already open
    with the same fingerprint, or covering its evidence, is refreshed instead); the others
    are kept ``refused`` or ``duplicate`` with their reason (never read by a reader).
    Open items not seen for ``ITEM_DAYS`` expire."""
    at = _iso(now)
    counts: Counter = Counter()
    ws.execute("BEGIN IMMEDIATE")
    try:
        for item in items:
            state, reason = item["state"], item.get("reason")
            fp = fingerprint(item["subject"] or "", item["groups"])
            if state == "open":
                same = ws.execute("SELECT item_id FROM items WHERE state = 'open' AND "
                                  "fingerprint = ?", (fp,)).fetchone()
                if same is None:
                    roots = {handle_root(h) for g in item["groups"] for h in g}
                    for row in ws.execute("SELECT item_id FROM items WHERE state = 'open' "
                                          "AND subject = ?", (item["subject"],)).fetchall():
                        held = {handle_root(r[0]) for r in ws.execute(
                            "SELECT handle FROM item_cites WHERE item_id = ?", (row[0],))}
                        if roots and roots <= held:
                            same = row
                            break
                if same is not None:
                    ws.execute("UPDATE items SET last_seen = ? WHERE item_id = ?",
                               (at, same[0]))
                    counts["refreshed"] += 1
                    continue
            item_id = "aw-" + hashlib.sha256(f"{fp}|{state}|{at}".encode()).hexdigest()[:16]
            ws.execute(
                "INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (item_id, fp, item["subject"] or "", item.get("subject_type") or "",
                 item["title"], item["text"], item["why"], item["next_step"],
                 json.dumps(item["departments"]), item["priority"], item["claim_kind"],
                 json.dumps({"verdict": item["gate"], "claims": item["claims"],
                             "refused_claims": item["refused_claims"]}),
                 json.dumps(item.get("truth") or {}), state, reason, at, at, run_id))
            ws.executemany("INSERT OR IGNORE INTO item_cites VALUES (?, ?, ?, ?, ?)",
                           [(item_id, grp, handle, (item.get("objects") or {}).get(handle),
                             (item.get("markings") or {}).get(handle, '["unreadable"]'))
                            for grp, group in enumerate(item["groups"]) for handle in group])
            counts[state] += 1
        expired = (now - dt.timedelta(days=ITEM_DAYS))
        counts["expired"] = ws.execute(
            "UPDATE items SET state = 'expired' WHERE state = 'open' AND last_seen < ?",
            (_iso(expired),)).rowcount
        ws.execute("COMMIT")
    except BaseException:
        ws.execute("ROLLBACK")
        raise
    return counts


def judge(items: Sequence[dict[str, Any]], *, places: Places, inbox: Path,
          sources: Callable[[Sequence[str]], Any], guard: Any,
          excluded: Callable[[Any, str], bool] | None = None) -> list[dict[str, Any]]:
    """Each proposal decided (module doc, steps 4-5): placed (every cited object served
    now, at its markings), cleared of the people exclusion, truth-checked and deduplicated
    against the detectors; ``state`` and ``reason`` set."""
    from . import truth_check
    excluded = excluded or (lambda g, h: truth_check.excluded_hit(g, {"handle": h}))
    out = []
    src = None
    for item in items:
        item = dict(item)
        item["state"], item["reason"] = "refused", None
        if item["gate"] != "passed":
            item["reason"] = item["gate"]
            out.append(item)
            continue
        handles = [h for g in item["groups"] for h in g]
        objects = {h: places.object_of(h) for h in handles}
        placed = {oid: places.place(oid) for oid in set(objects.values()) if oid}
        if not all(objects.values()) or not all(placed.values()):
            item["reason"] = "cite_not_served"
            out.append(item)
            continue
        item["objects"] = objects
        # what a reader must hold to read each record as its claims read it: the version's
        # markings and those of the very values the claims read there (a finance cost on
        # an unmarked order needs finance)
        fields = item.get("fields") or {}
        item["markings"] = {h: places.markings_of(h, objects[h], fields.get(h))
                            for h in handles}
        if any(is_message(h) and (guard is None or excluded(guard, h)) for h in handles):
            item["reason"] = "people_withheld"
            out.append(item)
            continue
        cited_objects = list(dict.fromkeys(objects.values()))
        if item["subject"] not in cited_objects:
            item["subject"] = cited_objects[0]   # the subject is a record the item cites
        item["subject_type"] = placed[item["subject"]]["object_type"]
        if src is None:
            src = sources([o for i in items for o in [i.get("subject") or ""]] + cited_objects)
        item["truth"] = truth(src, item, cited_objects)
        if item["truth"]["contradicted"]:
            item["reason"] = "truth_contradicted"
        elif detector_covers(inbox, item["subject"], item["groups"]):
            item["state"], item["reason"] = "duplicate", "detector_event"
        else:
            item["state"] = "open"
        out.append(item)
    return out


# --------------------------------------------------------------------------- reading

def _reader_states(conn: sqlite3.Connection, reader: str) -> dict[str, sqlite3.Row]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                        "'item_states'").fetchone():
        return {}   # a store written before reader states: none set
    return {row["item_id"]: row for row in conn.execute(
        "SELECT * FROM item_states WHERE reader = ?", (reader,))}


def set_aside(state: Mapping[str, Any] | None, today: dt.date) -> bool:
    """Whether a reader's state keeps an item out of their briefing: dismissed, or snoozed
    through a day not yet past (an ended snooze counts no more, as a card's does)."""
    if state is None:
        return False
    if state["state"] == "dismissed":
        return True
    return state["state"] == "snoozed" and str(state["until"] or "") >= today.isoformat()


def read_items(directory: Path, store: Path, *, may_read: Callable[[str], bool],
               held: Iterable[str], decide: Callable[[Sequence[str]], Mapping[str, Any]],
               limit: int = 200, reader: str | None = None,
               today: dt.date | None = None) -> list[dict[str, Any]]:
    """The open items one reader may see (module doc, permissions), as the briefing reads a
    gap: kind ``watcher_attention``, ``detected_by`` ``watcher``. With ``reader`` (the
    briefing's reader key), an item that reader dismissed, or snoozed through a day not
    past ``today``, is left out (``set_item_state``). Read only."""
    from ontology import store as ostore
    path = store_path(directory)
    if not path.is_file():
        return []
    allowed = set(ostore.allowed_marking_sets(held))
    granted: dict[str, bool] = {}

    def may(key: str) -> bool:
        if key not in granted:
            try:
                granted[key] = may_read(key) is True
            except Exception:  # noqa: BLE001 - a failing grant check denies
                granted[key] = False
        return granted[key]

    places = Places(store)
    try:
        with closing(_ro(path)) as conn:
            rows = conn.execute("SELECT * FROM items WHERE state = 'open' ORDER BY "
                                "last_seen DESC, item_id LIMIT ?", (limit,)).fetchall()
            cites = defaultdict(list)
            for row in conn.execute(
                    "SELECT c.* FROM item_cites c JOIN items i ON i.item_id = c.item_id "
                    "WHERE i.state = 'open'"):
                cites[row["item_id"]].append(dict(row))
            if reader is not None:
                states = _reader_states(conn, reader)
                day = today or _now().date()
                rows = [r for r in rows if not set_aside(states.get(r["item_id"]), day)]
            # people's feedback on an item (not useful, wrong): suppressed for whom it covers
            from . import feedback_signals
            hide = feedback_signals.suppressor(_state_of(store))
            rows = [r for r in rows if hide(r["item_id"], reader) is None]
            since_run = _code_since(conn)
        seen: dict[str, bool] = {}

        def visible(object_id: str | None) -> bool:
            if not object_id:
                return False
            if object_id not in seen:
                where = places.place(object_id)
                seen[object_id] = bool(where) and may(where["audience_key"]) \
                    and where["markings"] in allowed
            return seen[object_id]

        shown_cites = {}
        for row in rows:
            shown_cites[row["item_id"]] = [c for c in cites[row["item_id"]]
                                           if c["markings"] in allowed
                                           and visible(c["object_id"])]
        handles = sorted({c["handle"] for found in shown_cites.values() for c in found})
        refused = {h for h, why in decide(handles).items() if why is not None} \
            if handles else set()
        out = []
        for row in rows:
            # every claim is shown or none: a claim only when the reader may open every
            # record it cites or is computed from, at the markings of what it reads there;
            # an item only when every claim is (its text is written from all of them)
            every = cites[row["item_id"]]
            open_ = [c for c in shown_cites[row["item_id"]] if c["handle"] not in refused]
            if not every or len(open_) != len(every) or not visible(row["subject"]):
                continue
            out.append({
                "id": row["item_id"], "kind": KIND, "detector": DETECTOR,
                "object_id": row["subject"], "object_type": row["subject_type"],
                "summary": row["text"], "citations": list(dict.fromkeys(
                    c["handle"] for c in open_)),
                "detected_by": "watcher", "state": "open",
                # made by an older watcher code or model than the deployed one: labelled
                "older_code": since_run is None or row["run_id"] is None
                or row["run_id"] < since_run,
                "first_seen": row["first_seen"], "last_seen": row["last_seen"],
                "follow_up": {"object_id": row["subject"], "why": row["why"],
                              "next_step": row["next_step"],
                              "who_can_answer": json.loads(row["departments"]),
                              "priority": row["priority"], "state": "unknown"}})
        return out
    finally:
        places.close()


def _code_since(conn: sqlite3.Connection) -> int | None:
    """The first run of the deployed watcher code and model; None: none yet (every item
    is older code's)."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'derived_code'").fetchone()
        held = json.loads(row[0]) if row else None
        from . import derived_status
        if not isinstance(held, dict) or held.get("code") != derived_status.version(
                "ai-watcher") or held.get("model") != (os.environ.get(MODEL_ENV) or MODEL):
            return None
        return int(held["since_run"])
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return None


SNOOZE_DAYS = 7
STATE_ACTIONS = ("dismissed", "snoozed", "open")   # ``open``: undo the reader's state


def set_item_state(store: Path, item_id: str, *, reader: str, state: str,
                   may_read: Callable[[str], bool], held: Iterable[str],
                   decide: Callable[[Sequence[str]], Mapping[str, Any]],
                   until: str | None = None, reason: str | None = None,
                   directory: Path | None = None,
                   clock: Callable[[], dt.datetime] = _now) -> dict[str, Any]:
    """One reader dismisses or snoozes a watcher item of their briefing (``open`` undoes
    it), per reader as the briefing's own acknowledgements and visits are kept: only
    their own briefing changes, the item stays for everyone else. An item this reader may
    not see (or none) is refused alike (``KeyError``), so nothing is learnt of it; a bad
    state or snooze day is ``ValueError``. A snooze lasts through ``until`` (an ISO day
    after today; default ``SNOOZE_DAYS`` from today)."""
    if state not in STATE_ACTIONS:
        raise ValueError("item_state_unknown")
    if directory is None:
        state_dir = _state_of(store)
        if state_dir is None or switch(state_dir) != "on":
            raise KeyError("item_not_found")   # no briefing lists it
        directory = watch_dir(state_dir)
    now = clock()
    today = now.astimezone(dt.timezone.utc).date()
    if state == "snoozed":
        try:
            day = dt.date.fromisoformat(until) if until else today + dt.timedelta(
                days=SNOOZE_DAYS)
        except (TypeError, ValueError):
            raise ValueError("snooze_until_invalid") from None
        if day <= today or day > today + dt.timedelta(days=366):
            raise ValueError("snooze_until_invalid")
        until = day.isoformat()
    else:
        until = None
    visible = {i["id"] for i in read_items(directory, store, may_read=may_read, held=held,
                                            decide=decide, limit=10_000)}
    if item_id not in visible:
        raise KeyError("item_not_found")
    with closing(open_store(directory)) as ws:
        if state == "open":
            ws.execute("DELETE FROM item_states WHERE item_id = ? AND reader = ?",
                       (item_id, reader))
        else:
            ws.execute("INSERT OR REPLACE INTO item_states VALUES (?, ?, ?, ?, ?, ?)",
                       (item_id, reader, state, until,
                        (reason or "").strip()[:500] or None, _iso(now)))
    return {"item_id": item_id, "state": state, "until": until, "at": _iso(now)}


def stamp(store: Path) -> tuple | None:
    """The watcher's state beside a served store now (its store, write-ahead log and
    switch: change time and size), for a page cache's key: a run, a dismissal or a switch
    reads the briefing again."""
    state_dir = _state_of(store)
    if state_dir is None:
        return None
    directory = watch_dir(state_dir)
    out = []
    for file in (store_path(directory), directory / "watcher.sqlite-wal",
                 switch_path(state_dir)):
        try:
            found = os.stat(file)
            out.append((found.st_mtime_ns, found.st_size))
        except OSError:
            out.append(None)
    from . import feedback_signals
    out.append(feedback_signals.stamp(state_dir))   # an item marked by feedback
    return tuple(out)


def merge_briefing(found: Mapping[str, Any], store: Path, *, may_read: Callable[[str], bool],
                   held: Iterable[str], decide: Callable[[Sequence[str]], Mapping[str, Any]],
                   reader: str | None = None) -> dict[str, Any]:
    """The briefing's inbox read with this reader's watcher items added, when the switch
    is ``on`` (else as read), less those ``reader`` dismissed or snoozed. The watcher's
    last run is its check's state."""
    state_dir = _state_of(store)
    if found.get("status") != "read" or state_dir is None or switch(state_dir) != "on":
        return dict(found)
    directory = watch_dir(state_dir)
    try:
        items = read_items(directory, store, may_read=may_read, held=held, decide=decide,
                           reader=reader)
        status = last_status(directory)
    except Exception:  # noqa: BLE001 - the watcher never breaks the briefing
        return {**found, "detectors": {**(found.get("detectors") or {}),
                                       DETECTOR: "failed"}}
    out = dict(found)
    out["gaps"] = list(found.get("gaps") or ()) + items
    out["detectors"] = {**(found.get("detectors") or {}), DETECTOR: status}
    if items:
        out["kind_history"] = {**(found.get("kind_history") or {}),
                               KIND: min(i["first_seen"] for i in items)}
    return out


def _state_of(store: Path) -> Path | None:
    """The state directory a served store sits in (``<state>/projection/<file>``)."""
    parent = Path(store).resolve().parent if Path(store).exists() else Path(store).parent
    if parent.name == "projection":
        return parent.parent
    if parent.parent.name == "generations":
        return parent.parent.parent.parent
    return None


def last_status(directory: Path) -> str:
    """The briefing's word for the watcher's last run: ``complete`` (a run within the hour
    reviewed its batch or found nothing new), ``truncated`` (changes still wait), else
    ``not_run``."""
    path = store_path(directory)
    if not path.is_file():
        return "not_run"
    with closing(_ro(path)) as conn:
        row = conn.execute("SELECT run_at, status, summary FROM runs ORDER BY run_id DESC "
                           "LIMIT 1").fetchone()
    if row is None:
        return "not_run"
    try:
        summary = json.loads(row["summary"])
    except ValueError:
        summary = {}
    if row["status"] == "failed":
        return "failed"
    return "truncated" if summary.get("waiting") else "complete"


# --------------------------------------------------------------------------- the run

# ``isolated_state``: files up to this size are copied (whatever writes them writes the
# copy), larger ones are linked read-only; a store's journal, lock and temp files are left
# to the store.
ISOLATE_COPY_BYTES = 64 * 1024 * 1024
ISOLATE_COPY_SMALL = 1024 * 1024   # any other file up to this size is copied too
ISOLATE_DIR_ENTRIES = 1000   # a subtree holding more is a content store: linked whole
_SIDECAR = re.compile(r"(?:-wal|-shm|-journal|\.lock|\.tmp)$")


def _rollback_store(path: Path) -> bool:
    """A SQLite file in rollback-journal mode (its header's write version 1): readers write
    nothing of it. A WAL store is read where it is (its live log is beside it)."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return header[:16] == b"SQLite format 3\x00" and header[18] == 1


def isolated_state(real: Path, scratch: Path, *, copy_bytes: int = ISOLATE_COPY_BYTES
                   ) -> Path:
    """A state directory for a dry run that writes nothing of ``real``: its directories
    made anew under ``scratch``, every sqlite store up to ``copy_bytes`` and every other
    file up to ``ISOLATE_COPY_SMALL`` copied (a store by its backup, so the copy is
    whole), a large rollback-mode store (the served projection) hard linked (what the
    tools keep beside a store lands beside the link), every other larger file or
    subtree of more than ``ISOLATE_DIR_ENTRIES`` entries (the capture stores, blobs) a
    symbolic link to the original, read only, and its symbolic links kept. Whatever the
    run creates or rewrites lands in ``scratch``; this watcher's own directory is not
    carried (the dry run keeps its own store). Returns ``scratch``."""
    real, scratch = Path(real).resolve(), Path(scratch)
    if scratch.resolve() == real or real in scratch.resolve().parents:
        raise ValueError("isolated_state_inside_real")
    scratch.mkdir(parents=True, exist_ok=True)
    held: Counter = Counter()   # directory -> entries in its whole subtree
    for here, dirs, files in os.walk(real, topdown=False):
        held[here] += len(dirs) + len(files) + sum(held[os.path.join(here, d)]
                                                    for d in dirs)
    for here, dirs, files in os.walk(real):
        rel = Path(here).relative_to(real)
        if rel.parts[:1] == (WATCH_DIR,):
            dirs[:] = []
            continue
        if rel.parts and held[here] > ISOLATE_DIR_ENTRIES:
            # a content store (blobs by digest): read where it is, never walked
            if not (scratch / rel).is_symlink():
                (scratch / rel).symlink_to(here)
            dirs[:] = []
            continue
        (scratch / rel).mkdir(parents=True, exist_ok=True)
        os.chmod(scratch / rel, os.stat(here).st_mode & 0o777)
        for name in [d for d in dirs if (Path(here) / d).is_symlink()] + files:
            source, target = Path(here) / name, scratch / rel / name
            if target.exists() or target.is_symlink() or _SIDECAR.search(name):
                continue
            if source.is_symlink():
                target.symlink_to(os.path.realpath(source))
            elif not source.is_file():
                continue
            elif _rollback_store(source) and source.stat().st_size > copy_bytes:
                # a large store in rollback mode (the served projection): hard linked, as
                # a run pins a generation, so what the tools keep beside the file they
                # read (``PRAGMA database_list`` names the path, a symbolic link would
                # name the original's directory) is kept beside the link; another
                # filesystem: refused, never a link whose neighbours are the original's
                if os.stat(source).st_dev != os.stat(scratch).st_dev:
                    raise ValueError("isolated_state_other_filesystem")
                os.link(source, target)
            elif name.endswith((".sqlite", ".db")) and source.stat().st_size <= copy_bytes:
                with closing(_ro(source)) as src, closing(sqlite3.connect(target)) as dst:
                    src.backup(dst)
                os.chmod(target, source.stat().st_mode & 0o777)
            elif source.stat().st_size <= ISOLATE_COPY_SMALL:
                import shutil
                shutil.copy2(source, target)
            else:   # a capture's content or a large store: read where it is
                target.symlink_to(source)
        dirs[:] = [d for d in dirs if not (Path(here) / d).is_symlink()]
    return scratch


def reader_identity(environ: Mapping[str, str]) -> tuple[dict[str, str] | None, str | None]:
    """(the watcher reader's identity claims, None), (None, None) for the declared eval
    principal (none named) or (None, why) for a named reader that is not valid. Never
    printed."""
    raw = environ.get(READER_ENV) or ""
    if not raw.strip():
        return None, None
    try:
        claims = json.loads(raw)
    except ValueError:
        return None, "reader_identity_invalid"
    if not isinstance(claims, dict) or not all(isinstance(claims.get(k), str)
                                               and claims[k].strip() for k in READER_CLAIMS):
        return None, "reader_identity_invalid"
    return {k: claims[k] for k in READER_CLAIMS}, None


def reader_host(state_dir: Path, host_config: Path, environ: Mapping[str, str]) -> Any:
    """The persistent host as the watcher's one fixed reader (``agent_session.ReaderHost``,
    the Ask's): ``ValueError`` (a code) when it cannot be started."""
    from . import agent_session
    from . import eval_principal
    from .session_identity import session_secret
    secret = session_secret(environ)
    if not secret:
        raise ValueError("no_session_secret")
    config = json.loads(Path(host_config).read_text(encoding="utf-8"))
    identity, problem = reader_identity(environ)
    if problem:
        raise ValueError(problem)
    extra = ({n: environ[n] for n in agent_session.GRAPH_ENV if environ.get(n)}
             if config.get("entra_directory") else {})
    if identity is None:
        return agent_session.ReaderHost(
            state_dir, host_config, eval_principal.ticket_claims(Path(host_config)), secret,
            tenant=str(config.get("tenant") or "plx"), extra_env=extra,
            eval_principal=True, agent=AGENT)
    return agent_session.ReaderHost(state_dir, host_config, identity, secret,
                                    tenant=str(config.get("tenant") or "plx"),
                                    extra_env=extra, agent=AGENT)


def claude_agent(model: str, *, budget: float = BUDGET_SECONDS,
                 timeout: int = TIMEOUT_SECONDS) -> Any:
    from . import agent_session
    return agent_session.ClaudeCodeAgent(
        model=model, timeout=timeout, max_turns=MAX_TURNS,
        budget_seconds=budget, max_steps=MAX_STEPS, end_at_gate=False,
        # one host serves every call: measured, a mail read a helper host served did not
        # count as this session's evidence at the gate, and the watcher waits for no one
        helpers=0)


# --------------------------------------------------------------------------- sessions

def sessions_last_hour(ws: sqlite3.Connection, now: dt.datetime) -> int:
    """Model sessions started in the hour before ``now``, batch and retry alike, whether
    they ended or were killed: what the cap (``MAX_SESSIONS_PER_HOUR``) counts."""
    return ws.execute("SELECT count(*) FROM sessions WHERE started_at >= ?",
                      (_iso(now - dt.timedelta(hours=1)),)).fetchone()[0]


def _start_session(ws: sqlite3.Connection, kind: str, entries: Sequence[str],
                   now: dt.datetime, parent: int | None = None) -> int:
    """Record a session as it starts (before any model call), claiming the queue entries
    it reads: a run killed in it leaves the record (``recover``)."""
    ws.execute("BEGIN IMMEDIATE")
    try:
        sid = int(ws.execute(
            "INSERT INTO sessions (kind, parent, started_at, state, entries) VALUES "
            "(?, ?, ?, 'running', ?)", (kind, parent, _iso(now),
                                        json.dumps(list(entries)))).lastrowid)
        ws.executemany("UPDATE queue SET claimed = ? WHERE object_id = ?",
                       [(sid, oid) for oid in entries])
        ws.execute("COMMIT")
    except BaseException:
        ws.execute("ROLLBACK")
        raise
    return sid


def _end_session(ws: sqlite3.Connection, sid: int, state: str, *, now: dt.datetime,
                 result: Any = None, summary: Any = None) -> None:
    ws.execute("UPDATE sessions SET state = ?, ended_at = ?, result = coalesce(?, result), "
               "summary = coalesce(?, summary) WHERE session_id = ?",
               (state, _iso(now), None if result is None else json.dumps(result, default=str),
                None if summary is None else json.dumps(summary, default=str), sid))


def _release(ws: sqlite3.Connection, sid: int) -> Counter:
    """The queue entries of a session that gave no result, back in the queue for one more
    attempt; one already tried ``MAX_ATTEMPTS`` times leaves it (``abandoned``)."""
    counts: Counter = Counter()
    for (oid, attempts) in ws.execute("SELECT object_id, attempts FROM queue WHERE "
                                      "claimed = ?", (sid,)).fetchall():
        if attempts + 1 >= MAX_ATTEMPTS:
            ws.execute("DELETE FROM queue WHERE object_id = ?", (oid,))
            counts["abandoned"] += 1
        else:
            ws.execute("UPDATE queue SET claimed = NULL, attempts = attempts + 1 WHERE "
                       "object_id = ?", (oid,))
            counts["requeued"] += 1
    return counts


def recover(ws: sqlite3.Connection, now: dt.datetime) -> tuple[Counter, list[int]]:
    """A run killed mid-way (a timeout, a restart): a session that started and never ended
    is ``interrupted`` and its entries go back for one more attempt, never more
    (``_release``); a batch session whose result was saved but never folded is returned
    to be folded from that result, with no new model call. Counts and the sessions."""
    counts: Counter = Counter()
    for (sid,) in ws.execute("SELECT session_id FROM sessions WHERE state = 'running'"
                             ).fetchall():
        counts["interrupted"] += 1
        counts.update(_release(ws, sid))
        _end_session(ws, sid, "interrupted", now=now)
    pending = [r[0] for r in ws.execute(
        "SELECT session_id FROM sessions WHERE kind = 'batch' AND state = 'answered' "
        "ORDER BY session_id")]
    return counts, pending


def _fold_session(ws: sqlite3.Connection, sid: int, raised: list[dict[str, Any]], *,
                  now: dt.datetime, store: Path, inbox: Path,
                  sources: Callable[[Sequence[str]], Any], guard: Any,
                  excluded: Callable[[Any, str], bool] | None,
                  summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Judge and fold one batch session's saved items, take its entries off the queue and
    mark it folded: the end of a run, or of a killed one resumed (``recover``)."""
    places = Places(store)
    try:
        judged = judge(raised, places=places, inbox=inbox, sources=sources, guard=guard,
                       excluded=excluded)
    finally:
        places.close()
    run_id = _record(ws, now, summary, model_run=True)
    ws.execute("DELETE FROM queue WHERE claimed = ?", (sid,))
    folded = fold(ws, judged, run_id=run_id, now=now)
    summary.update(raised=len(raised), folded=dict(folded),
                   reasons=dict(Counter(i["reason"] for i in judged if i["reason"])))
    ws.execute("UPDATE runs SET summary = ? WHERE run_id = ?",
               (json.dumps(summary, sort_keys=True, default=str), run_id))
    _end_session(ws, sid, "folded", now=now)
    return judged


def cycle(state_dir: Path, *, directory: Path | None = None, store: Path | None = None,
          host: Any = None, agent: Any = None, retry_agent: Any = None,
          now: dt.datetime | None = None,
          guard: Any = ..., sources: Callable[[Sequence[str]], Any] | None = None,
          max_sessions_per_hour: int = MAX_SESSIONS_PER_HOUR, max_reads: int = MAX_READS,
          window_hours: float = WINDOW_HOURS, discover_changes: bool = True,
          excluded: Callable[[Any, str], bool] | None = None,
          transcripts: Path | None = None, run_seconds: float = RUN_SECONDS,
          monotonic: Callable[[], float] = time.monotonic,
          log: Callable[[str], None] = lambda text: None) -> dict[str, Any]:
    """One watcher run (module doc); its summary (counts and enums only). ``host``,
    ``agent``, ``retry_agent``: the reader's host, the batch session's agent and a refused
    item's own pass's (``reader_host``, ``claude_agent``; a given ``agent`` serves both);
    ``guard``: the people exclusion (default: the truth check's).

    Cost: every model session, batch or retry, is recorded as it starts and counted
    against ``max_sessions_per_hour`` (a killed one included); a session is started only
    when its own time limit still ends within ``run_seconds`` of the run's start, so the
    run ends inside its unit's timeout. Recovery: a killed run's saved result is folded by
    the next run with no model call; a session killed before its result gives its entries
    one more attempt, never a whole batch replayed again and again (``recover``)."""
    from . import answerers, truth_check
    from ontology.delta import served_store
    now = now or _now()
    begun = monotonic()
    directory = Path(directory) if directory is not None else watch_dir(state_dir)
    store = Path(store) if store is not None else served_store(Path(state_dir))
    inbox = store.with_name(store.name + ".gap-inbox.sqlite")
    if guard is ...:
        guard, _why = truth_check.people_guard(Path(state_dir), os.environ)
    if sources is None:
        def sources(objects: Sequence[str]) -> Any:
            return load_sources(Path(state_dir), objects, as_of=now.date())
    model = os.environ.get(MODEL_ENV) or MODEL
    if retry_agent is None:
        retry_agent = agent if agent is not None else claude_agent(
            model, budget=RETRY_BUDGET_SECONDS, timeout=RETRY_TIMEOUT_SECONDS)
    agent = agent if agent is not None else claude_agent(model)
    fold_args = {"now": now, "store": store, "inbox": inbox, "sources": sources,
                 "guard": guard, "excluded": excluded}

    def at() -> dt.datetime:
        """The moment now within the run (its start plus the time spent): a session's
        start and end as they happened."""
        return now + dt.timedelta(seconds=max(0.0, monotonic() - begun))
    with _locked(directory) as mine:
        if not mine:
            return {"status": "busy"}
        with closing(open_store(directory)) as ws:
            recovered, pending = recover(ws, now)
            resumed = []
            for sid in pending:   # a killed run's saved result: folded, no model call
                row = ws.execute("SELECT result, summary FROM sessions WHERE session_id = ?",
                                 (sid,)).fetchone()
                try:
                    raised = json.loads(row["result"] or "[]")
                    old = json.loads(row["summary"] or "{}")
                except ValueError:
                    raised, old = [], {}
                old = {**old, "status": "resumed", "resumed_session": sid}
                resumed += _fold_session(ws, sid, raised, summary=old, **fold_args)
            found = discover(ws, store, now=now, guard=guard, excluded=excluded,
                             window_hours=window_hours) \
                if discover_changes else {"layers": 0}
            summary: dict[str, Any] = {"schema": SCHEMA, "changes": found,
                                       "recovered": dict(recovered),
                                       "resumed": len(pending), "sessions": 0}

            def room(timeout: float) -> bool:
                """Whether one more session of this limit fits the cap and the run."""
                return sessions_last_hour(ws, now) < max_sessions_per_hour \
                    and monotonic() - begun + timeout <= run_seconds

            if not room(getattr(agent, "timeout", TIMEOUT_SECONDS) or 0):
                summary.update(status="rate_capped", waiting=ws.execute(
                    "SELECT count(*) FROM queue WHERE claimed IS NULL").fetchone()[0])
                _record(ws, now, summary, model_run=False)
                summary["items"] = resumed
                return summary
            batch = pick(ws, now=now, max_reads=max_reads, window_hours=window_hours,
                         origin=_origins())
            summary.update(aged_out=batch["aged_out"], waiting=batch["waiting"],
                           reviewed=len(batch["entries"]),
                           bulk=sum(g["count"] for g in batch["bulk"]))
            if not batch["entries"] and not batch["bulk"]:
                summary["status"] = "nothing_new"
                _record(ws, now, summary, model_run=False)
                summary["items"] = resumed
                return summary
            ids = [r["object_id"] for r in batch["entries"]] + [
                g["sample"]["object_id"] for g in batch["bulk"]]
            events, own = flagged(inbox, ws, ids)
            codes = answerers.departments()
            prompt = task(batch, events, own, now=now, departments=codes)
            agent.first_reads = reads_for(batch)
            agent.follow_reads = None
            started = time.monotonic()
            log(f"session: {len(batch['entries'])} records, {len(batch['bulk'])} bulk")
            sid = _start_session(ws, "batch", batch["taken"], at())
            summary["sessions"] += 1
            try:
                run = agent.run(prompt, host)
            except Exception as error:  # noqa: BLE001 - a failed session: one more try
                summary.update(status="failed", error=type(error).__name__,
                               released=dict(_release(ws, sid)))
                _end_session(ws, sid, "failed", now=at())
                _record(ws, now, summary, model_run=True)
                return summary
            summary["session"] = {
                "seconds": round(time.monotonic() - started, 1),
                "subtype": run.usage.get("subtype"), "turns": run.usage.get("num_turns"),
                "tool_calls": sum(1 for e in run.events if e.get("type") == "tool_call"),
                "gate": (run.agent.get("answer_gate") or {}).get("outcomes"),
                # the batch's reads as the session got them (counts only)
                "prefetched": dict(Counter(
                    f"{e.get('name')}:{_read_state(e)}" for e in run.events
                    if e.get("prefetched"))),
                "calls": dict(Counter(f"{e.get('name')}:{_read_state(e)}" for e in run.events
                                      if e.get("type") == "tool_call"
                                      and not e.get("prefetched"))),
                "cost_usd": run.usage.get("total_cost_usd")}
            if transcripts is not None:   # a dry run's private debugging copy
                transcripts.mkdir(parents=True, exist_ok=True)
                (transcripts / f"run-{_iso(now)}-{int(time.time())}.json").write_text(
                    json.dumps({"task": prompt, "events": run.events,
                                "final_text": run.final_text}, default=str),
                    encoding="utf-8")
            raised = proposals(run, departments=codes)
            # the result is saved before anything else: a run killed from here on is
            # folded by the next one from it, with no model call
            _end_session(ws, sid, "answered", now=at(), result=raised,
                         summary={**summary, "status": "ran"})
            # each item the gate refused once the session's one repair was spent gets its
            # own small gate pass (its own repair), while the cap and the run's time allow
            retried = recovered_items = deferred = 0
            for n, item in enumerate(raised):
                if item["gate"] == "passed" or not item.get("retry"):
                    continue
                if retried >= MAX_RETRIES or not room(
                        getattr(retry_agent, "timeout", RETRY_TIMEOUT_SECONDS) or 0):
                    deferred += 1
                    continue
                retried += 1
                rid = _start_session(ws, "retry", [], at(), parent=sid)
                summary["sessions"] += 1
                again = _retry(retry_agent, host, item, codes, transcripts=transcripts,
                               now=now)
                _end_session(ws, rid, "answered" if again is not None else "failed",
                             now=at())
                if again is not None:
                    raised[n] = again
                    recovered_items += 1
                    ws.execute("UPDATE sessions SET result = ? WHERE session_id = ?",
                               (json.dumps(raised, default=str), sid))
            summary.update(retried=retried, recovered=recovered_items,
                           retries_deferred=deferred, status="ran")
            judged = _fold_session(ws, sid, raised, summary=summary, **fold_args)
            summary["items"] = resumed + judged
            summary["prompt_records"] = ids
            return summary


def _retry(agent: Any, host: Any, item: Mapping[str, Any], codes: Iterable[str], *,
           transcripts: Path | None, now: dt.datetime) -> dict[str, Any] | None:
    """One refused item decided again in its own session (``retry_task``): the item its
    gate passed, or None (refused again, dropped, or the session failed)."""
    retry = item["retry"]
    prompt = retry_task(item["title"], retry, item.get("refusals") or (), departments=codes)
    agent.first_reads, agent.follow_reads = [], None
    try:
        run = agent.run(prompt, host)
    except Exception:  # noqa: BLE001 - the item stays refused
        return None
    if transcripts is not None:
        transcripts.mkdir(parents=True, exist_ok=True)
        (transcripts / f"retry-{_iso(now)}-{time.time_ns()}.json").write_text(
            json.dumps({"task": prompt, "events": run.events, "final_text": run.final_text},
                       default=str), encoding="utf-8")
    found = [p for p in proposals(run, departments=codes) if p["gate"] == "passed"]
    if not found:
        return None
    best = next((p for p in found if p["title"] == item["title"]), found[0])
    return {**best, "retried": True}


def _read_state(event: Mapping[str, Any]) -> str:
    result = event.get("result")
    if event.get("is_error") or (isinstance(result, Mapping) and "error" in result):
        error = result.get("error") if isinstance(result, Mapping) else None
        code = error.get("code") if isinstance(error, Mapping) else error
        return f"error:{code}" if isinstance(code, str) else "error"
    return "ok"


def _origins() -> dict[str, str]:
    try:
        from ontology.schema import load_schema
        return dict(load_schema().origin_of_type)
    except Exception:  # noqa: BLE001 - unknown origins: every record is one stream
        return {}


def _record(ws: sqlite3.Connection, now: dt.datetime, summary: Mapping[str, Any], *,
            model_run: bool) -> int:
    cursor = ws.execute("INSERT INTO runs (run_at, status, model_run, summary) VALUES "
                        "(?, ?, ?, ?)", (_iso(now), str(summary.get("status") or "ran"),
                                          int(model_run), json.dumps(summary, sort_keys=True,
                                                                     default=str)))
    run_id = int(cursor.lastrowid)
    if model_run:
        _note_code(ws, run_id, now)
    return run_id


def _note_code(ws: sqlite3.Connection, run_id: int, now: dt.datetime) -> None:
    """Record the code and model of a model run (``operating.derived_status``): an item
    last made by a run before the first of this code is of older code, labelled
    (``read_items``'s ``older_code``) and never rerun outside the watcher's schedule."""
    try:
        from . import derived_status
        code = derived_status.version("ai-watcher")
    except Exception:  # noqa: BLE001 - unknown code: every item is older code's
        code = None
    model = os.environ.get(MODEL_ENV) or MODEL
    row = ws.execute("SELECT value FROM meta WHERE key = 'derived_code'").fetchone()
    try:
        held = json.loads(row[0]) if row else None
    except ValueError:
        held = None
    if isinstance(held, dict) and held.get("code") == code and held.get("model") == model:
        return
    ws.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('derived_code', ?)",
               (json.dumps({"scheme": "plx-derived-code/v1", "artifact": "ai-watcher",
                            "code": code, "model": model, "at": _iso(now),
                            "since_run": run_id}, sort_keys=True),))


# --------------------------------------------------------------------------- report

def report(runs: Sequence[Mapping[str, Any]], *, hours: float, now: dt.datetime) -> str:
    """The dry run's report: per run what it reviewed, and per raised item what it is, why
    it matters, its citations and whether the gate and the truth check passed it."""
    lines = [f"# AI watcher dry run ({_iso(now)[:16]}Z, last {hours:g} h of deltas)", "",
             "Read-only: the watcher read the served projection and its delta layers, made "
             "its reads as its one reader through the host's tools, and wrote only its own "
             "store in this folder. Nothing was sent.", ""]
    totals: Counter = Counter()
    for n, run in enumerate(runs, start=1):
        session = run.get("session") or {}
        lines += [f"## Run {n}: {run.get('status')}", "",
                  f"- changes: `{json.dumps(run.get('changes'), sort_keys=True)}`",
                  f"- reviewed one by one: {run.get('reviewed', 0)}; in bulk lines: "
                  f"{run.get('bulk', 0)}; still waiting: {run.get('waiting', 0)}; aged out: "
                  f"{run.get('aged_out', 0)}",
                  f"- session: {session.get('seconds')} s, {session.get('tool_calls')} tool "
                  f"calls, gate outcomes {session.get('gate')}, ended {session.get('subtype')}",
                  f"- refused items given their own gate pass: {run.get('retried', 0)}, "
                  f"passed on it: {run.get('recovered', 0)}",
                  f"- batch reads made before its first turn: "
                  f"`{json.dumps(session.get('prefetched'), sort_keys=True)}`; its own calls: "
                  f"`{json.dumps(session.get('calls'), sort_keys=True)}`",
                  ""]
        for item in run.get("items") or ():
            totals[item["state"]] += 1
            truth_v = (item.get("truth") or {}).get("verdict", "not run")
            lines += [f"### {item['title'] or '(untitled)'}", "",
                      f"- **Outcome:** {item['state']}"
                      + (f" ({item['reason']})" if item.get("reason") else ""),
                      f"- **Gate:** {item['gate']}"
                      + (" on its own pass" if item.get("retried") else "")
                      + f" ({len(item['claims'])} claims passed, "
                      f"{item['refused_claims']} refused"
                      + (f": {', '.join(item.get('refusals') or ())}"
                         if item.get("refusals") else "") + ")",
                      f"- **Truth check:** {truth_v}"
                      + (f" ({', '.join(sorted(set((item.get('truth') or {}).get('checks') or ())))})"
                         if (item.get("truth") or {}).get("checks") else ""),
                      f"- **Subject:** `{item.get('subject')}`",
                      f"- **What:** {item['text'] or '(nothing the gate passed)'}",
                      f"- **Reason:** {item['why'] or '(nothing the gate passed)'}",
                      f"- **Suggested next step (draft, not sent):** "
                      f"{item['next_step'] or '-'}",
                      f"- **Routed to:** {', '.join(item['departments']) or '-'}; priority "
                      f"{item['priority']}; rests on {item['claim_kind']}",
                      "- **Citations:**"]
            for claim in item["claims"]:
                lines.append(f"  - {claim.get('text') or claim.get('id')}: "
                             + ", ".join(f"`{h}`" for h in claim["citations"]))
            for reason in (item.get("truth") or {}).get("reasons") or ():
                lines.append(f"  - truth {reason['rule']}: {reason['text']}")
            lines.append("")
        if not run.get("items"):
            lines += ["No item raised.", ""]
    lines.insert(4, "**Items:** " + (", ".join(f"{k} {v}" for k, v in sorted(totals.items()))
                                     or "none") + "\n")
    return "\n".join(lines)


def _public(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in summary.items() if k not in ("items", "prompt_records")}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.ai_watcher",
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one watcher run (nothing when the switch is off)")
    dry = sub.add_parser("dryrun", help="read-only runs over the last hours of deltas")
    for p in (run, dry):
        p.add_argument("--state-dir", type=Path, required=True)
        p.add_argument("--host-config", type=Path, required=True)
        p.add_argument("--max-reads", type=int, default=MAX_READS)
    dry.add_argument("--out-dir", type=Path, required=True)
    dry.add_argument("--hours", type=float, default=24.0)
    dry.add_argument("--runs", type=int, default=2)
    dry.add_argument("--transcripts", type=Path, default=None,
                     help="keep each session's transcript here (debugging only)")
    dry.add_argument("--isolate", type=Path, default=None,
                     help="run against an isolated copy of --state-dir built here "
                          "(isolated_state): nothing of the real state is written")
    sw = sub.add_parser("switch", help="turn the watcher on, dry or off")
    sw.add_argument("state", choices=SWITCH_STATES)
    sw.add_argument("--state-dir", type=Path, required=True)
    st = sub.add_parser("status", help="the switch and the last runs (counts only)")
    st.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "switch":
        set_switch(args.state_dir, args.state)
        print(json.dumps({"switch": args.state}))
        return 0
    if args.cmd == "status":
        directory = watch_dir(args.state_dir)
        out: dict[str, Any] = {"switch": switch(args.state_dir),
                               "last": last_status(directory)}
        if store_path(directory).is_file():
            with closing(_ro(store_path(directory))) as conn:
                out["runs"] = [{"run_at": r[0], "status": r[1]} for r in conn.execute(
                    "SELECT run_at, status FROM runs ORDER BY run_id DESC LIMIT 5")]
                out["items"] = dict(conn.execute("SELECT state, count(*) FROM items "
                                                 "GROUP BY state").fetchall())
        print(json.dumps(out, indent=1))
        return 0
    if args.cmd == "run" and switch(args.state_dir) == "off":
        print(json.dumps({"status": "switched_off"}))
        return 0
    if args.cmd == "dryrun" and args.isolate is not None:
        args.state_dir = isolated_state(args.state_dir, args.isolate)
        os.environ["PLX_WORKBENCH_STATE_DIR"] = str(args.state_dir)
    host = reader_host(args.state_dir, args.host_config, os.environ)
    if args.cmd == "run":
        summary = cycle(args.state_dir, host=host, max_reads=args.max_reads,
                        log=lambda text: print(text, file=sys.stderr))
        print(json.dumps(_public(summary), sort_keys=True, default=str))
        return 1 if summary.get("status") == "failed" else 0
    # dryrun: its own store in --out-dir, the last --hours of deltas, at most --runs runs
    now = _now()
    runs = []
    for n in range(max(1, args.runs)):
        summary = cycle(args.state_dir, directory=args.out_dir, host=host, now=now,
                        max_reads=args.max_reads, window_hours=args.hours,
                        max_sessions_per_hour=4 * max(1, args.runs),
                        discover_changes=n == 0,
                        transcripts=args.transcripts,
                        log=lambda text: print(text, file=sys.stderr))
        runs.append(summary)
        print(json.dumps(_public(summary), sort_keys=True, default=str), file=sys.stderr)
        if summary.get("status") != "ran":
            break
    (args.out_dir / "dryrun.md").write_text(report(runs, hours=args.hours, now=now),
                                            encoding="utf-8")
    (args.out_dir / "dryrun.json").write_text(json.dumps(runs, indent=1, default=str),
                                              encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
