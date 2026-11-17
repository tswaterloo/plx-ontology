"""Ops mail: the alarms reach a person without anyone looking at the app.

The in-app alarms (``operating.ops_alarms``) and the host's own alarm
(``deploy/ops/host/unit_alarm.py``) hand their open incidents here. Each incident key gets
one mail when it opens, one when its level rises (escalates), one again every
``resend_hours`` (4) while it stays open at level ``high`` or ``critical``, and one when it
recovers. A ``warning`` is mailed once when it opens and then listed in the daily mail; an
``info`` is never mailed on its own. When one pass has more than ``max_mails_per_pass``
mails they go as one combined mail, so a hundred timers stopping at once is one mail.

Once a day, at ``heartbeat_hour_et`` (8 AM ET), the in-app pass sends the daily mail: "all
clear" when nothing at warning or above is open, else every open incident with its age
(those open three days or more are marked: fix them or pause them), what is paused and
until when, mails that could not be sent, and the lines the caller adds (disk forecast,
dependency drift). The daily mail coming is the proof the alarms run: when it does not
come, they do not. The host alarm (system python, root disk) mails on its own when the
in-app pass has stopped or /data is gone.

Config: ``/etc/plx/ops-notify.json`` (``PLX_OPS_NOTIFY_CONFIG``), on the root disk:

    {"enabled": false, "sender": "", "owner_address": "", "recipients": ["owner"],
     "credentials_file": "~/.config/plx/plx.env", "credentials_prefix": "AZURE",
     "heartbeat_hour_et": 8, "resend_hours": 4, "max_mails_per_pass": 4,
     "state_file": "/var/lib/plx-ops/notify.json", "ops_url": ""}

Nothing is sent unless ``enabled`` is true and a ``sender`` (one fixed ops mailbox) and at
least one recipient are set; until then every mail is recorded in the state file's log as
not sent, with why. ``"owner"`` in ``recipients`` is ``owner_address``. Alarm mail goes to
the owner only: a recipient other than the owner's mailbox is refused,
never mailed (``refused_recipients``; the status page counts them), and the transport itself
refuses a mail to anyone else before it asks for a token. Transport: Graph
``POST /users/{sender}/sendMail`` with the client-credentials token of the app whose
``<prefix>_TENANT_ID/_CLIENT_ID/_CLIENT_SECRET`` are in ``credentials_file`` (read at send
time; the alarm services keep secrets out of their environment). A failed send is kept and
retried on the next pass (at most 20, for 24 h). Mails hold alarm lines only (unit, check
and source names, counts, times): never a business record, a person's address other than
the recipients, or a secret.

Stdlib only and no package imports: the host alarm loads this file from
``/usr/local/lib/plx-ops`` when /data and the venv are gone.

State (``state_file``, under an exclusive lock): ``keys`` (each incident mailed: level,
when it opened, when last mailed, who opened it), ``heartbeat_on`` (the ET date of the last
daily mail), ``pending`` (unsent mails) and ``log`` (the last 200 mails, sent or not).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote, urlencode

SCHEMA = "plx-ops-notify/v1"
CONFIG_ENV = "PLX_OPS_NOTIFY_CONFIG"
CONFIG_PATH = Path("/etc/plx/ops-notify.json")
FALLBACK_STATE = "notify.json"   # beside the alarm state when the root-disk dir is not ours
LEVELS = {"info": 1, "warning": 2, "high": 3, "critical": 4}
NAMES = {v: k for k, v in LEVELS.items()}
KEEP_LOG = 200
KEEP_PENDING = 20
PENDING_HOURS = 24
STALE_OPEN_DAYS = 3
HOST_GRACE_MINUTES = 15   # a unit failure the host mailed that the in-app pass never saw
SUBJECT_MAX = 160
DEFAULTS: dict[str, Any] = {
    "enabled": False, "sender": "", "owner_address": "", "recipients": ["owner"],
    "credentials_file": "~/.config/plx/plx.env", "credentials_prefix": "AZURE",
    "heartbeat_hour_et": 8, "resend_hours": 4, "max_mails_per_pass": 4,
    "state_file": "/var/lib/plx-ops/notify.json", "ops_url": "",
}

# Levels by check key (the first matching prefix wins); a check may carry its own "level".
CRITICAL_UNITS = ("plx-api.service", "plx-app.service", "plx-caddy.service",
                  "plx-ingest.service", "postgresql@17-main.service", "plx-ops-health.service")
KEY_LEVELS: tuple[tuple[str, str], ...] = (
    ("memory_pressure", "info"),
    ("host:", "critical"), ("ingest_service", "critical"),
    ("exclusion_unavailable:", "critical"),
    ("capture:mail", "critical"), ("capture:teams", "critical"),
    ("capture:portal", "critical"),
    ("capture_failing:mail", "critical"), ("capture_failing:teams", "critical"),
    ("capture_failing:portal", "critical"),
    ("disk_free", "critical"), ("memory_available", "critical"),
    ("backup", "critical"), ("restore_test", "critical"), ("pg_connections", "critical"),
    ("capture:", "high"), ("capture_failing:", "high"), ("ingest_start", "high"),
    ("delta_", "high"), ("pg_follow", "high"), ("proactive_last_pass", "high"),
    ("search_follow_last_pass", "high"), ("credential_expiry", "high"),
    ("disk_forecast:", "high"), ("pg_wraparound", "high"), ("load_high", "high"),
)


def level_for(key: str, check: Optional[Mapping[str, Any]] = None) -> int:
    """The level (``LEVELS`` value) of an incident key; a check's own ``level`` wins."""
    own = (check or {}).get("level")
    if isinstance(own, str) and own in LEVELS:
        return LEVELS[own]
    if key.startswith(("unit:", "unit:user:")):
        unit = key.split(":")[-1]
        return LEVELS["critical" if unit in CRITICAL_UNITS else
                      "high" if unit.endswith(".service") else "warning"]
    for prefix, name in KEY_LEVELS:
        if key.startswith(prefix):
            return LEVELS[name]
    return LEVELS["warning"]


# ---- config -----------------------------------------------------------------------------

def load_config(path: Optional[Path] = None,
                environ: Mapping[str, str] = os.environ) -> dict[str, Any]:
    """The config file merged over ``DEFAULTS``; a missing or unreadable file is the
    defaults (disabled), with ``config_error`` naming why."""
    where = Path(path or environ.get(CONFIG_ENV) or CONFIG_PATH)
    cfg = dict(DEFAULTS, config_path=str(where))
    try:
        held = json.loads(where.read_text())
        if not isinstance(held, dict):
            raise ValueError("not an object")
        cfg.update({k: v for k, v in held.items() if k in DEFAULTS})
    except FileNotFoundError:
        cfg["config_error"] = "no config file"
    except (OSError, ValueError) as exc:
        cfg["config_error"] = f"config unreadable ({type(exc).__name__})"
    return cfg


def owner_mailbox(cfg: Mapping[str, Any]) -> Optional[str]:
    """The owner's mailbox (``owner_address``), or None when it is not set."""
    address = cfg.get("owner_address")
    if isinstance(address, str) and "@" in address:
        return address.strip()
    return None


def _is_owner(address: Any, cfg: Mapping[str, Any]) -> bool:
    owner = owner_mailbox(cfg)
    return (owner is not None and isinstance(address, str)
            and address.strip().lower() == owner.lower())


def recipients(cfg: Mapping[str, Any]) -> list[str]:
    """Who the alarm mail goes to: the owner's mailbox when ``recipients`` names it (as
    ``"owner"`` or the address itself), else no one. Alarm mail is owner-only: every other
    entry is refused (``refused_recipients``), never mailed."""
    owner = owner_mailbox(cfg)
    named = any(r == "owner" or _is_owner(r, cfg) for r in cfg.get("recipients") or ())
    return [owner] if owner is not None and named else []


def refused_recipients(cfg: Mapping[str, Any]) -> int:
    """How many ``recipients`` entries are not the owner's mailbox (refused; the count only,
    never an address)."""
    return sum(1 for r in cfg.get("recipients") or ()
               if r != "owner" and not _is_owner(r, cfg))


def why_off(cfg: Mapping[str, Any]) -> Optional[str]:
    """Why nothing is sent, or None when mail goes out."""
    if not cfg.get("enabled"):
        return cfg.get("config_error") or "disabled in the config"
    if not (isinstance(cfg.get("sender"), str) and "@" in cfg["sender"]):
        return "no sender set"
    if not recipients(cfg):
        return "no recipient set"
    return None


# ---- transport --------------------------------------------------------------------------

class SendError(Exception):
    """A mail not sent; the message names the step and an HTTP status, never a value."""


def read_env_file(path: Path, names: Iterable[str]) -> dict[str, str]:
    wanted, out = set(names), {}
    for raw in path.read_text().splitlines():
        row = raw.strip()
        if row.startswith("export "):
            row = row[7:].strip()
        if not row or row.startswith("#") or "=" not in row:
            continue
        name, value = row.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name in wanted:
            out[name] = value
    return out


Post = Callable[[str, Mapping[str, str], bytes], tuple[int, bytes]]


REQUEST_SECONDS = 10


def _sender() -> Any:
    """``ops_mail_sender`` (the one pinned POST): the repo's, or the copy installed beside
    this file in /usr/local/lib/plx-ops."""
    try:
        from operating import ops_mail_sender
    except ImportError:
        import ops_mail_sender  # type: ignore[import-not-found,no-redef]
    return ops_mail_sender


def _post(url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
    sender = _sender()
    try:
        return sender.post_ops_mail(url, headers, body, REQUEST_SECONDS)
    except sender.SenderRefused as exc:
        raise SendError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - no connection, timeout: named, never a value
        raise SendError(f"request failed ({type(exc).__name__})") from None


class GraphMail:
    """Graph ``sendMail`` from the fixed ops sender (client credentials). ``post`` is
    injected in tests."""

    LOGIN = "https://login.microsoftonline.com"
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, cfg: Mapping[str, Any], post: Post = _post) -> None:
        self.cfg, self.post = cfg, post

    def _token(self) -> str:
        prefix = str(self.cfg.get("credentials_prefix") or "AZURE")
        names = [f"{prefix}_TENANT_ID", f"{prefix}_CLIENT_ID", f"{prefix}_CLIENT_SECRET"]
        try:
            creds = read_env_file(Path(str(self.cfg["credentials_file"])).expanduser(),
                                  names)
        except OSError as exc:
            raise SendError(f"credentials file unreadable ({type(exc).__name__})") from None
        if any(not creds.get(n) for n in names):
            raise SendError(f"{prefix} credentials incomplete")
        status, body = self.post(
            f"{self.LOGIN}/{quote(creds[names[0]], safe='')}/oauth2/v2.0/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            urlencode({"grant_type": "client_credentials", "client_id": creds[names[1]],
                       "client_secret": creds[names[2]],
                       "scope": "https://graph.microsoft.com/.default"}).encode())
        try:
            token = json.loads(body or b"{}").get("access_token") if status == 200 else None
        except ValueError:
            token = None
        if not isinstance(token, str) or not token:
            raise SendError(f"token refused (HTTP {status})")
        return token

    def __call__(self, mail: Mapping[str, Any]) -> None:
        to = list(mail.get("to") or ())
        if not to or not all(_is_owner(a, self.cfg) for a in to):
            raise SendError("recipient refused: alarm mail goes to the owner's mailbox only")
        sender = str(self.cfg["sender"])
        # an alarm quoting a log or a record never mails a card number or a credential
        from operating.card_text import mask_served
        message = {"message": {
            "subject": mask_served(str(mail["subject"])),
            "body": {"contentType": "Text", "content": mask_served(str(mail["body"]))},
            "toRecipients": [{"emailAddress": {"address": a}} for a in mail["to"]]},
            "saveToSentItems": False}
        status, _ = self.post(
            f"{self.GRAPH}/users/{quote(sender, safe='@')}/sendMail",
            {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
            json.dumps(message).encode())
        if status != 202:
            raise SendError(f"sendMail refused (HTTP {status})")


# ---- time -------------------------------------------------------------------------------

def _eastern():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - no tz database: UTC, shown as such
        return timezone.utc


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: Any) -> Optional[datetime]:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def et(when: Optional[datetime]) -> str:
    if when is None:
        return "unknown"
    local = when.astimezone(_eastern())
    return local.strftime("%a %b %-d %-I:%M %p ET")


def span(delta: timedelta) -> str:
    minutes = max(0, delta.total_seconds()) / 60
    if minutes < 120:
        return f"{round(minutes)} min"
    if minutes < 48 * 60:
        return f"{round(minutes / 60)} h"
    return f"{round(minutes / 1440)} days"


# ---- state ------------------------------------------------------------------------------

class _Held:
    """The notify state under an exclusive lock (the in-app pass and the host alarm)."""

    def __init__(self, path: Path, wait: bool = True) -> None:
        self.path, self.wait, self.held = path, wait, None

    def __enter__(self) -> Optional[dict[str, Any]]:
        """The state, or None (``wait`` False) when another pass holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(self.path.with_name(self.path.name + ".lock"), "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            return None
        try:
            held = json.loads(self.path.read_text())
        except (OSError, ValueError):
            held = None
        self.held = held if isinstance(held, dict) and held.get("schema") == SCHEMA \
            else {"schema": SCHEMA, "keys": {}, "pending": [], "log": []}
        return self.held

    def __exit__(self, *exc: Any) -> None:
        if self.lock is None:
            return
        try:
            if exc[0] is None and self.held is not None:
                tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as handle:
                    json.dump(self.held, handle, indent=1, sort_keys=True)
                os.replace(tmp, self.path)
        finally:
            self.lock.close()


def state_path(cfg: Mapping[str, Any], fallback_dir: Optional[Path] = None) -> Path:
    """The configured state file when its directory is writable, else ``fallback_dir``'s
    (the in-app alarm state) so a host without /var/lib/plx-ops still deduplicates."""
    path = Path(str(cfg.get("state_file") or DEFAULTS["state_file"]))
    if fallback_dir is not None and not os.access(path.parent, os.W_OK):
        return fallback_dir / FALLBACK_STATE
    return path


# ---- mails ------------------------------------------------------------------------------

def _host() -> str:
    try:
        return os.uname().nodename or "host"
    except (AttributeError, OSError):
        return "host"


def _footer(cfg: Mapping[str, Any]) -> list[str]:
    tail = ["", f"Host: {_host()}"]
    if cfg.get("ops_url"):
        tail.append(f"Status page: {cfg['ops_url']}")
    return tail


KIND_WORDS = {"open": "OPEN", "escalate": "WORSE", "resend": "STILL OPEN",
              "recover": "RECOVERED"}


def event_mail(event: Mapping[str, Any], cfg: Mapping[str, Any], now: datetime
               ) -> dict[str, Any]:
    level = NAMES[event["level"]]
    subject = f"[PLX ops] {KIND_WORDS[event['kind']]} {level}: {event['line']}"
    opened = _parse(event.get("opened_at"))
    body = [event["line"], ""]
    if event["kind"] == "recover":
        body.append(f"Recovered at {et(now)}; it was open "
                    f"{span(now - opened) if opened else 'for an unknown time'}.")
    else:
        body.append(f"Opened {et(opened)}" + (f" ({span(now - opened)} ago)" if opened
                                               and now - opened > timedelta(minutes=10)
                                               else "") + f". Level: {level}.")
        if event["kind"] == "escalate":
            body.append(f"It was {NAMES.get(event.get('was'), 'lower')} before.")
        if event["level"] >= LEVELS["high"]:
            body.append(f"Mailed again every {cfg['resend_hours']:g} h while it stays open.")
    body.append(f"Key: {event['key']}")
    return {"subject": subject[:SUBJECT_MAX], "body": "\n".join(body + _footer(cfg)),
            "keys": [event["key"]], "kind": event["kind"]}


def combined_mail(events: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any],
                  now: datetime) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    worst = max((e["level"] for e in events if e["kind"] != "recover"), default=None)
    said = ", ".join(f"{n} {KIND_WORDS[k].lower()}" for k, n in
                     sorted(counts.items(), key=lambda kv: list(KIND_WORDS).index(kv[0])))
    subject = f"[PLX ops] {len(events)} changes" + (f", worst {NAMES[worst]}" if worst
                                                    else "") + f": {said}"
    body = []
    for kind in KIND_WORDS:
        group = sorted((e for e in events if e["kind"] == kind),
                       key=lambda e: (-e["level"], e["key"]))
        if group:
            body += ["", f"{KIND_WORDS[kind]} ({len(group)}):"]
            body += [f"- [{NAMES[e['level']]}] {e['line']}" for e in group]
    return {"subject": subject[:SUBJECT_MAX], "body": "\n".join(body[1:] + _footer(cfg)),
            "keys": [e["key"] for e in events], "kind": "combined"}


def heartbeat_mail(held: Mapping[str, Any], cfg: Mapping[str, Any], now: datetime,
                   paused: Sequence[Mapping[str, Any]], extra: Sequence[str]
                   ) -> dict[str, Any]:
    keys = held.get("keys") or {}
    shown = sorted(((k, v) for k, v in keys.items()
                    if int(v.get("level") or 0) >= LEVELS["warning"]),
                   key=lambda kv: (-int(kv[1].get("level") or 0), kv[1].get("opened_at", "")))
    worst = max((int(v.get("level") or 0) for _, v in shown), default=0)
    day = now.astimezone(_eastern()).strftime("%a %b %-d")
    if not shown:
        subject = f"[PLX ops] Daily {day}: all clear"
        body = ["All clear: nothing is open at warning or above."]
    else:
        subject = (f"[PLX ops] Daily {day}: {len(shown)} open, worst {NAMES[worst]}")
        body = [f"Open ({len(shown)}):"]
        for key, v in shown:
            opened = _parse(v.get("opened_at"))
            age = now - opened if opened else None
            stale = age is not None and age >= timedelta(days=STALE_OPEN_DAYS)
            body.append(f"- [{NAMES.get(int(v.get('level') or 0), '?')}] {v.get('line')}"
                        f" (open {span(age) if age is not None else '?'}"
                        + ("; open 3+ days: fix it or pause it" if stale else "") + ")")
    infos = [v for v in keys.values() if int(v.get("level") or 0) < LEVELS["warning"]]
    if infos:
        body += ["", f"Notes ({len(infos)}):"] + [f"- {v.get('line')}" for v in infos]
    if paused:
        body += ["", f"Paused on purpose ({len(paused)}):"]
        body += [f"- {p.get('line') or p.get('key')} (until {et(_parse(p.get('until')))}: "
                 f"{p.get('why') or 'no reason given'})" for p in paused]
    pending = held.get("pending") or []
    if pending:
        body += ["", f"Mails not sent yet ({len(pending)}), retried each pass:"]
        body += [f"- {m.get('subject')} ({m.get('error')})" for m in pending]
    if extra:
        body += [""] + list(extra)
    body += ["", f"This mail comes every day at {int(cfg['heartbeat_hour_et'])} AM ET. "
             "If it does not come, the alarms are not running: check the host."]
    return {"subject": subject[:SUBJECT_MAX], "body": "\n".join(body + _footer(cfg)),
            "keys": [], "kind": "heartbeat"}


# ---- the pass ---------------------------------------------------------------------------

def _events(held: dict[str, Any], current: Mapping[str, Mapping[str, Any]],
            owned: Callable[[str, Mapping[str, Any]], bool], now: datetime,
            cfg: Mapping[str, Any], by: str) -> list[dict[str, Any]]:
    """What to mail this pass. While mail is off nothing counts as mailed, so turning it
    on mails every incident then open (one combined mail when there are several)."""
    keys: dict[str, dict[str, Any]] = held.setdefault("keys", {})
    resend = timedelta(hours=float(cfg.get("resend_hours") or 4))
    live = why_off(cfg) is None
    stamp, out = _iso(now), []
    for key, inc in current.items():
        level = int(inc.get("level") or level_for(key, inc))
        line = str(inc.get("line") or key)
        entry = keys.get(key)
        if entry is None:
            entry = keys[key] = {"level": level, "mailed_level": 0, "line": line, "by": by,
                                 "opened_at": inc.get("opened_at") or stamp,
                                 "last_mailed_at": None}
        entry.update(level=level, line=line, by=by)
        mailed = int(entry.get("mailed_level") or 0)
        last = _parse(entry.get("last_mailed_at"))
        if level > mailed and level >= LEVELS["warning"]:
            kind = "open" if mailed < LEVELS["warning"] else "escalate"
        elif level >= LEVELS["high"] and (last is None or now - last >= resend):
            kind = "resend"
        else:
            if level < mailed:
                entry["mailed_level"] = level   # better: a later rise mails again
            continue
        out.append({"kind": kind, "key": key, "level": level, "line": line, "was": mailed,
                    "opened_at": entry.get("opened_at")})
        if live:
            entry.update(mailed_level=level, last_mailed_at=stamp)
    for key, entry in list(keys.items()):
        if key in current or not owned(key, entry):
            continue
        keys.pop(key)
        if int(entry.get("mailed_level") or 0) >= LEVELS["warning"]:
            out.append({"kind": "recover", "key": key, "line": str(entry.get("line")),
                        "level": int(entry.get("mailed_level") or 0),
                        "opened_at": entry.get("opened_at")})
    return out


SEND_BUDGET_SECONDS = 25   # all sends of one pass; the rest wait for the next pass


def _deliver(held: dict[str, Any], mails: list[dict[str, Any]], cfg: Mapping[str, Any],
             now: datetime, transport: Optional[Callable[[Mapping[str, Any]], None]], *,
             retry: bool = True, clock: Callable[[], float] = time.monotonic
             ) -> list[dict[str, Any]]:
    """Sends this pass's mails, the pending ones first (``retry``). Stops at the first
    failure and once ``SEND_BUDGET_SECONDS`` are spent: what is left waits, so an
    unreachable Graph costs a pass one request. While mail is off only the daily mail is
    logged (as not sent, with why)."""
    off = why_off(cfg)
    to = recipients(cfg)
    stamp = _iso(now)
    log = held.setdefault("log", [])
    if off is not None:
        out = []
        for mail in mails:
            record = {"at": stamp, "kind": mail.get("kind"), "subject": mail["subject"],
                      "keys": list(mail.get("keys") or []), "sent": False, "why": off}
            if mail.get("kind") == "heartbeat":
                log.insert(0, record)
            out.append(record)
        del log[KEEP_LOG:]
        return out
    keep = [m for m in held.get("pending") or []
            if (_parse(m.get("first_at")) or now) > now - timedelta(hours=PENDING_HOURS)]
    queue = (keep if retry else []) + mails
    held["pending"] = [] if retry else keep
    send = transport if transport is not None else GraphMail(cfg)
    started, failed, out = clock(), None, []
    for mail in queue:
        record = {"at": stamp, "kind": mail.get("kind"), "subject": mail["subject"],
                  "keys": list(mail.get("keys") or [])}
        if failed is None and clock() - started > SEND_BUDGET_SECONDS:
            failed = "send time used up this pass"
        if failed is not None:
            why = failed
        else:
            try:
                send({"subject": mail["subject"], "body": mail["body"], "to": to})
                record.update(sent=True, to=len(to))
                why = None
            except SendError as exc:
                why = failed = str(exc)
            except Exception as exc:  # noqa: BLE001 - a transport bug never stops the alarm
                why = failed = f"send crashed ({type(exc).__name__})"
        if why is not None:
            record.update(sent=False, why=why)
            held["pending"].append({k: v for k, v in mail.items() if k != "retry"}
                                   | {"first_at": mail.get("first_at") or stamp,
                                      "error": why})
        log.insert(0, record)
        out.append(record)
    held["pending"] = held["pending"][-KEEP_PENDING:]
    del log[KEEP_LOG:]
    return out


def _mails(events: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any], now: datetime
           ) -> list[dict[str, Any]]:
    if not events:
        return []
    if len(events) > int(cfg.get("max_mails_per_pass") or 4):
        return [combined_mail(events, cfg, now)]
    return [event_mail(e, cfg, now) for e in events]


def heartbeat_due(held: Mapping[str, Any], cfg: Mapping[str, Any], now: datetime) -> bool:
    local = now.astimezone(_eastern())
    return (local.hour >= int(cfg.get("heartbeat_hour_et") or 8)
            and held.get("heartbeat_on") != local.date().isoformat())


BUSY = {"mails": [], "off": None, "busy": True}


def run_alarms(incidents: Mapping[str, Mapping[str, Any]], now: datetime, *,
               cfg: Optional[Mapping[str, Any]] = None,
               transport: Optional[Callable[[Mapping[str, Any]], None]] = None,
               paused: Sequence[Mapping[str, Any]] = (), extra: Sequence[str] = (),
               fallback_dir: Optional[Path] = None, heartbeat: bool = True,
               retry: bool = True) -> dict[str, Any]:
    """The in-app pass: ``incidents`` (key -> ``line``, ``opened_at``, ``level``) are every
    incident open and not paused. Mails what changed and, when due, the daily mail. A
    paused key is neither open nor recovered (its mail state waits for the pause to end).
    A key the host alarm opened (a unit failure) that this pass never reports recovers
    after ``HOST_GRACE_MINUTES``; ``host:`` keys are the host alarm's own. Another pass
    mailing right now: this one skips (``busy``) and the next catches up."""
    cfg = cfg or load_config()
    held_back = {p.get("key") for p in paused}

    def owned(key: str, entry: Mapping[str, Any]) -> bool:
        if key.startswith("host:") or key in held_back:
            return False
        if entry.get("by") == "host":
            opened = _parse(entry.get("opened_at"))
            return opened is None or now - opened >= timedelta(minutes=HOST_GRACE_MINUTES)
        return True
    with _Held(state_path(cfg, fallback_dir), wait=False) as held:
        if held is None:
            return dict(BUSY)
        mails = _mails(_events(held, incidents, owned, now, cfg, "alarms"), cfg, now)
        if heartbeat and heartbeat_due(held, cfg, now):
            mails.append(heartbeat_mail(held, cfg, now, paused, extra))
            held["heartbeat_on"] = now.astimezone(_eastern()).date().isoformat()
        sent = _deliver(held, mails, cfg, now, transport, retry=retry)
        return {"mails": sent, "off": why_off(cfg),
                "open_keys": len(held.get("keys") or {}),
                "pending": len(held.get("pending") or [])}


def run_host(conditions: Optional[Mapping[str, Mapping[str, Any]]], now: datetime, *,
             cfg: Optional[Mapping[str, Any]] = None,
             transport: Optional[Callable[[Mapping[str, Any]], None]] = None,
             notices: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """The host alarm: ``conditions`` are every ``host:`` key now true (absent ones
    recover; None leaves them as they are); ``notices`` open keys the in-app pass owns
    from then on (a unit failure: ``unit:<name>``), mailed once unless already open."""
    cfg = cfg or load_config()
    with _Held(state_path(cfg)) as held:
        events = [] if conditions is None else _events(
            held, conditions, lambda k, e: k.startswith("host:"), now, cfg, "host")
        keys = held.setdefault("keys", {})
        fresh = {k: v for k, v in (notices or {}).items() if k not in keys}
        events += _events(held, fresh, lambda k, e: False, now, cfg, "host")
        sent = _deliver(held, _mails(events, cfg, now), cfg, now, transport)
        return {"mails": sent, "off": why_off(cfg)}


def describe(cfg: Optional[Mapping[str, Any]] = None,
             fallback_dir: Optional[Path] = None) -> dict[str, Any]:
    """For the status page: whether mail goes out (and why not), the last few mails."""
    cfg = cfg or load_config()
    path = state_path(cfg, fallback_dir)
    try:
        held = json.loads(path.read_text())
    except (OSError, ValueError):
        held = {}
    log = held.get("log") if isinstance(held, dict) else None
    return {"sending": why_off(cfg) is None, "why_not": why_off(cfg),
            "recipients": len(recipients(cfg)),
            "refused_recipients": refused_recipients(cfg),
            "heartbeat_on": held.get("heartbeat_on") if isinstance(held, dict) else None,
            "pending": len(held.get("pending") or []) if isinstance(held, dict) else 0,
            "last": [{k: m.get(k) for k in ("at", "kind", "subject", "sent", "why")}
                     for m in (log or [])[:5]]}
