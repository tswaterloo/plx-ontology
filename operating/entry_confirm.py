"""Held entry-assist entries and the values a person enters or confirms on them.

Entry assist (``operating.entry_assist``) holds a BC order or PO whose party, or a line's
item, quantity or price, is a check: nothing cited it, so nothing may put it on the write
desk. The person who prepares the entry settles each of those checks here: they pick the
customer, supplier or item record (from the suggestions or a search), or enter the
quantity, or the price with its currency. Each value is kept as ``status: "confirmed"``
with who confirmed it and when (``source.confirmed_by``, ``confirmed_at``), never as a
cited one; ``entry_assist.bc_header`` takes a confirmed value like a cited one, and the
reason it writes on the desk says how many values a person confirmed and who.

Nothing here writes to BC or approves anything: once every check is settled the entry goes
to the write desk exactly as a fully cited one does (prepared by that person, approved by
another grant holder there, a dry run until every switch is set).

The store is a JSON-lines file in the server's private state directory
(``<state_dir>/entry/holds.jsonl``, 0600; in memory without one): a ``held`` row (the
pre-fill, its kind, who prepared it), one ``confirmed`` row per value, and a ``prepared``
row with the write desk entry once it went there. Every read and write holds an ``flock``
on ``holds.lock`` (more than one API process is safe), waiting at most ``LOCK_WAIT``
seconds (then ``Busy``: try again); an unreadable line is skipped, logged and noted in
``holds-unreadable.json`` for the ``entry_holds_unreadable`` ops alarm. The folder is
0750 and that note 0640 in the state directory's group (the alarm pass runs as the jobs'
user, not the API's); the store and its lock stay 0600. An entry held more than
``HOLD_DAYS`` days ago is cut to an ``expired`` row on the next write (its preparer is
told to ask again, never "no such entry"), and that row is dropped after
``EXPIRED_DAYS`` days (``EntryHolds``).

A value must be readable as written: a number takes commas only as thousands separators
(``number``), a currency only as its ISO code (``currency``)."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional

# the party field of each kind, and the line fields a BC entry needs settled
PARTY = {"order": "customer", "purchase_order": "supplier"}
LINE_FIELDS = ("item", "quantity", "unit_price")
# the record type a picked value must be, per field
RECORD_TYPE = {"customer": "Customer", "supplier": "Supplier", "item": "Product"}
HOLD_DAYS = 7               # a hold older than this is not confirmed on: ask again
EXPIRED_DAYS = 90           # an expired hold still answers "ask again" this long
LOCK_WAIT = 10.0            # seconds a request waits for the store, then ``Busy``
MAX_LINES = 200             # a pre-fill with more lines is not held (never cut short)
_KEY = re.compile(r"^(?:(customer|supplier)|line:([1-9][0-9]{0,2}):(item|quantity|unit_price))$")
_ID = re.compile(r"^eh_[0-9a-f]{24}$")
# a plain number, or one with every comma between groups of three digits (ASCII only)
_NUMBER = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,6})?|\.[0-9]{1,6}",
                     re.ASCII)
UNREADABLE_FILE = "holds-unreadable.json"   # read by ``ops_alarms.entry_holds_checks``
_log = logging.getLogger(__name__)


class Refused(Exception):
    """A confirmation that cannot be taken: ``code``, words for the person, HTTP status."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Busy(Refused):
    """The store stayed locked past ``LOCK_WAIT`` (another request holds it): try again."""

    def __init__(self) -> None:
        super().__init__("busy", "Held entries are busy right now: try again in a moment.",
                         503)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def held_keys(prefill: Mapping[str, Any], kind: str) -> list[tuple[str, str, Optional[int]]]:
    """``(key, field name, line number or None)`` of every field the BC entry needs
    settled: the party, then each line's item, quantity and price."""
    out: list[tuple[str, str, Optional[int]]] = [(PARTY[kind], PARTY[kind], None)]
    for n, _ln in enumerate(prefill.get("lines") or (), 1):
        out += [(f"line:{n}:{f}", f, n) for f in LINE_FIELDS]
    return out


def field_at(prefill: Mapping[str, Any], key: str) -> Optional[Mapping[str, Any]]:
    """The pre-fill's field at ``key`` (None: no such field)."""
    found = _KEY.fullmatch(key or "")
    if not found:
        return None
    if found.group(1):
        return (prefill.get("header") or {}).get(found.group(1))
    lines = prefill.get("lines") or ()
    n = int(found.group(2))
    if n > len(lines):
        return None
    return lines[n - 1].get(found.group(3)) or {"value": None, "status": "check",
                                                 "reason": "not read on the line"}


def parse_key(kind: str, prefill: Mapping[str, Any], key: str) -> tuple[str, Optional[int]]:
    """``(field name, line number or None)`` of a key this entry holds (else ``Refused``)."""
    found = _KEY.fullmatch(key or "")
    if not found or kind not in PARTY:
        raise Refused("unknown_field", "That is not a value of this entry.", 422)
    if found.group(1):
        if found.group(1) != PARTY[kind]:
            raise Refused("unknown_field", "That is not a value of this entry.", 422)
        return found.group(1), None
    n = int(found.group(2))
    if n > len(prefill.get("lines") or ()):
        raise Refused("unknown_field", "That is not a value of this entry.", 422)
    return found.group(3), n


def number(raw: Any, *, positive: bool) -> str:
    """A quantity (``positive``) or a price (zero or more) as a plain decimal string;
    anything else (empty, words, NaN, Infinity, a sign it may not have, more than 15
    digits before or 6 after the point) is ``Refused``. A comma is taken only as a
    thousands separator in its place (``1,000``, ``12,345.67``); any other comma
    (``1,0,0``, ``1,00``, ``1000,000``) is refused, never dropped: the person meant
    something the number does not say."""
    text = str(raw if raw is not None else "").strip()
    if not _NUMBER.fullmatch(text):
        raise Refused("bad_value", "Enter a number: digits, one decimal point, and commas "
                      "only between groups of three digits (1,000.50).", 422)
    text = text.replace(",", "")
    if len(text.split(".")[0]) > 15:
        raise Refused("bad_value", "Enter a number of at most 15 digits before the point.",
                      422)
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise Refused("bad_value", "Enter a number (digits and one decimal point).",
                      422) from None
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise Refused("bad_value", "A quantity is more than zero." if positive else
                      "A price is zero or more.", 422)
    return format(value.normalize() if value == value.to_integral() else value, "f")


def currency(raw: Any) -> str:
    """The price's currency as its ISO code (else ``Refused``: never guessed)."""
    from ontology.units import currency_code
    code = currency_code(str(raw or ""))[0] if isinstance(raw, str) else None
    if not code:
        raise Refused("bad_currency", "Say the price's currency as its three-letter code "
                      "(CAD, USD).", 422)
    return code


def confirmed(value: Any, *, by: Mapping[str, str], at: datetime,
              **source: Any) -> dict[str, Any]:
    """A field a person entered or confirmed (module doc)."""
    return {"value": value, "status": "confirmed",
            "source": {"basis": "entered or confirmed by the person preparing the entry",
                       **{k: v for k, v in source.items() if v not in (None, "", [])},
                       "confirmed_by": by.get("name") or by.get("email") or "the person",
                       "confirmed_by_email": by.get("email") or None,
                       "confirmed_by_oid": by.get("oid") or None,
                       "confirmed_at": _stamp(at)}}


def apply(prefill: Mapping[str, Any], confirmations: list[Mapping[str, Any]]
          ) -> dict[str, Any]:
    """The pre-fill with each key's latest confirmation in place of its field."""
    out = copy.deepcopy(dict(prefill))
    out["header"] = dict(out.get("header") or {})
    out["lines"] = [dict(ln) for ln in out.get("lines") or ()]
    for c in confirmations:
        found = _KEY.fullmatch(str(c.get("key") or ""))
        if not found:
            continue
        if found.group(1):
            out["header"][found.group(1)] = copy.deepcopy(c["field"])
        elif int(found.group(2)) <= len(out["lines"]):
            out["lines"][int(found.group(2)) - 1][found.group(3)] = copy.deepcopy(c["field"])
    return out


def _digest(kind: str, prefill: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps([kind, prefill], sort_keys=True, default=str)
                          .encode()).hexdigest()


# the fields every row of each event needs, else the row is unreadable (skipped)
_ROW_FIELDS = {"held": ("preparer", "kind", "digest", "prefill"),
               "confirmed": ("preparer", "key", "field"),
               "prepared": ("preparer", "intent_id"),
               # what is left of an entry held more than HOLD_DAYS ago (``_append``)
               "expired": ("preparer", "kind")}


def _readable(row: Any) -> bool:
    if not isinstance(row, dict) or row.get("event") not in _ROW_FIELDS:
        return False
    if not isinstance(row.get("entry_id"), str) or not isinstance(row.get("at"), str):
        return False
    need = _ROW_FIELDS[row["event"]]
    if any(row.get(k) in (None, "") for k in need):
        return False
    if row["event"] == "held":
        return row["kind"] in PARTY and isinstance(row["prefill"], dict)
    if row["event"] == "expired":
        return row["kind"] in PARTY
    if row["event"] == "confirmed":
        return isinstance(row["field"], dict) and isinstance(row["key"], str)
    return True


class _StoreLock:
    """One read-modify-write of the store at a time, across threads (an ``RLock``) and
    across API processes (``flock`` on ``holds.lock`` next to the store, held while the
    outermost ``with`` lasts; reentrant in the thread that holds it). The outermost entry
    waits at most ``LOCK_WAIT`` seconds in all, then raises ``Busy``: one stuck request
    never holds every other entry request."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._thread = threading.RLock()
        self._depth = 0
        self._fd: Optional[int] = None

    def __enter__(self) -> "_StoreLock":
        deadline = time.monotonic() + LOCK_WAIT
        if not self._thread.acquire(timeout=LOCK_WAIT):
            raise Busy()
        try:
            if self._depth == 0 and self.path is not None:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise Busy() from None
                            time.sleep(0.05)
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._thread.release()
            raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        try:
            self._depth -= 1
            if self._depth == 0 and self._fd is not None:
                fd, self._fd = self._fd, None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            self._thread.release()


class EntryHolds:
    """Held entries and their confirmations (module doc).

    Every read and write takes ``lock`` (threads and API processes alike), so a reader
    never sees a half-written row and two final confirms never both prepare. A row that
    cannot be read (a torn or corrupt line) is skipped, logged by count only (never its
    content) and noted in ``holds-unreadable.json`` for the ops alarm; the next write
    rewrites the file without it. Each write also drops every row of an entry held more
    than ``HOLD_DAYS`` days ago (it can no longer be confirmed), so the file stays
    bounded by a week of holds plus a small ``expired`` row per entry for
    ``EXPIRED_DAYS`` days. The folder and the note are readable by the state directory's
    group (the ops alarm reads the note as the jobs' user); the store is 0600."""

    def __init__(self, directory: Optional[Path]) -> None:
        self.dir = directory
        self._mem: list[dict[str, Any]] = []
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            # group read and search (the setgid bit a shared state directory gave it stays)
            os.chmod(directory, 0o750 | (directory.stat().st_mode & stat.S_ISGID))
            _share_group(directory, directory.parent)
        self.lock = _StoreLock(directory / "holds.lock" if directory is not None else None)

    @classmethod
    def for_state_dir(cls, state_dir: Any) -> "EntryHolds":
        return cls(Path(state_dir) / "entry" if state_dir else None)

    @property
    def _path(self) -> Path:
        assert self.dir is not None
        return self.dir / "holds.jsonl"

    def _read(self) -> tuple[list[dict[str, Any]], bool]:
        """The store's readable rows, and whether the file needs rewriting before the next
        append: a line was unreadable, or the last one has no newline (a torn write an
        append would run into). The caller holds ``lock``."""
        if self.dir is None:
            return copy.deepcopy(self._mem), False
        try:
            text = self._path.read_bytes().decode("utf-8", "replace")
        except FileNotFoundError:
            return [], False
        rows: list[dict[str, Any]] = []
        bad = 0
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if _readable(row):
                rows.append(row)
            else:
                bad += 1
        if bad:
            self._note_unreadable(bad)
        return rows, bool(bad) or not text.endswith("\n") and bool(text)

    def _note_unreadable(self, bad: int) -> None:
        _log.warning("entry holds: %d unreadable line(s) in %s skipped", bad, self._path)
        marker = self.dir / UNREADABLE_FILE  # type: ignore[operator]
        try:
            seen = json.loads(marker.read_text("utf-8"))
            first = str(seen.get("first_at") or "") if isinstance(seen, dict) else ""
        except (OSError, ValueError):
            first = ""
        now = _stamp(_now())
        self._write_atomic(marker, json.dumps(
            {"lines": bad, "first_at": first or now, "last_at": now}, sort_keys=True),
            mode=0o640)

    def _write_atomic(self, path: Path, text: str, mode: int = 0o600) -> None:
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, mode)
        if mode & 0o040:
            _share_group(tmp, path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _rows(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._read()[0]

    def _append(self, row: Mapping[str, Any]) -> None:
        """Add one row; drop unreadable lines and expired entries if there are any."""
        with self.lock:
            rows, dirty = self._read()
            keep = _pruned(rows)
            if self.dir is None:
                self._mem = keep + [copy.deepcopy(dict(row))]
                return
            if dirty or keep != rows:
                self._write_atomic(self._path, "".join(
                    json.dumps(r, sort_keys=True) + "\n" for r in keep + [dict(row)]))
                return
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def hold(self, preparer: str, kind: str, prefill: Mapping[str, Any]) -> str:
        """The entry id of this pre-fill held for ``preparer`` (their object id): the same
        pre-fill still held for them and not yet on the desk is the same entry, with the
        values they already confirmed."""
        if kind not in PARTY or not preparer:
            raise ValueError("an order or PO held for a named person")
        if len(prefill.get("lines") or ()) > MAX_LINES:
            raise ValueError(f"more than {MAX_LINES} lines")  # guard-ok: explicit stated refusal of the whole pre-fill, never a silent cut
        keep = {"header": dict(prefill.get("header") or {}),
                "lines": list(prefill.get("lines") or ())}
        digest = _digest(kind, keep)
        with self.lock:
            rows = self._rows()
            done = {r["entry_id"] for r in rows if r["event"] == "prepared"}
            limit = _stamp(_now() - timedelta(days=HOLD_DAYS))
            for r in reversed(rows):
                if r["event"] == "held" and r["preparer"] == preparer and \
                        r["digest"] == digest and r["entry_id"] not in done and r["at"] >= limit:
                    return str(r["entry_id"])
            entry_id = "eh_" + secrets.token_hex(12)
            self._append({"event": "held", "entry_id": entry_id, "preparer": preparer,
                          "kind": kind, "digest": digest, "prefill": keep,
                          "at": _stamp(_now())})
        return entry_id

    def get(self, preparer: str, entry_id: str) -> Optional[dict[str, Any]]:
        """The preparer's own held entry: ``{"entry_id", "kind", "prefill"`` (with every
        confirmation applied), ``"confirmations", "intent_id", "at", "expired"}``; None for
        anyone else's, an unknown id or a malformed one (never told apart)."""
        if not _ID.fullmatch(entry_id or "") or not preparer:
            return None
        rows = [r for r in self._rows() if r.get("entry_id") == entry_id]
        first = next((r for r in rows if r["event"] == "held"), None)
        if first is None:
            gone = next((r for r in rows if r["event"] == "expired"), None)
            if gone is None or gone["preparer"] != preparer:
                return None
            # cut on a write after HOLD_DAYS: its preparer is told to ask again
            return {"entry_id": entry_id, "kind": gone["kind"], "at": gone["at"],
                    "prefill": {"header": {}, "lines": []}, "confirmations": [],
                    "intent_id": gone.get("intent_id"), "expired": True, "pruned": True}
        if first["preparer"] != preparer:
            return None
        confirmations = [r for r in rows if r["event"] == "confirmed"
                         and r["preparer"] == preparer]
        prepared = next((r for r in rows if r["event"] == "prepared"), None)
        return {"entry_id": entry_id, "kind": first["kind"], "at": first["at"],
                "prefill": apply(first["prefill"], confirmations),
                "confirmations": confirmations,
                "intent_id": prepared["intent_id"] if prepared else None,
                "expired": first["at"] < _stamp(_now() - timedelta(days=HOLD_DAYS))}

    def confirm(self, preparer: str, entry_id: str, key: str,
                field: Mapping[str, Any]) -> None:
        """Keep one confirmed value (the caller holds ``lock`` and checked the entry)."""
        self._append({"event": "confirmed", "entry_id": entry_id, "preparer": preparer,
                      "key": key, "field": dict(field), "at": _stamp(_now())})

    def prepared(self, preparer: str, entry_id: str, intent_id: str) -> None:
        self._append({"event": "prepared", "entry_id": entry_id, "preparer": preparer,
                      "intent_id": intent_id, "at": _stamp(_now())})


def _share_group(path: Path, holder: Path) -> None:
    """``path`` in ``holder``'s group when it is not already (a group this process cannot
    give leaves the path's own: the ops alarm then reports the note as unknown)."""
    try:
        gid = holder.stat().st_gid
        if path.stat().st_gid != gid:
            os.chown(path, -1, gid)
    except OSError:
        pass


def _pruned(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` with every entry held more than ``HOLD_DAYS`` days ago cut to one
    ``expired`` row (who, which kind, when held, its desk entry if any), and every
    ``expired`` row older than ``EXPIRED_DAYS`` days dropped."""
    now = _now()
    limit = _stamp(now - timedelta(days=HOLD_DAYS))
    gone_limit = _stamp(now - timedelta(days=EXPIRED_DAYS))
    held = {r["entry_id"]: r for r in rows if r["event"] == "held"}
    live = {e for e, r in held.items() if r["at"] >= limit}
    keep: list[dict[str, Any]] = []
    cut: set[str] = set()
    desk = {r["entry_id"]: r["intent_id"] for r in rows if r["event"] == "prepared"}
    for r in rows:
        e = r["entry_id"]
        if e in live:
            keep.append(r)
        elif e in cut:
            continue
        elif r["event"] == "held" or (r["event"] == "expired" and e not in held):
            cut.add(e)
            if r["at"] < gone_limit:
                continue
            row = {"event": "expired", "entry_id": e, "preparer": r["preparer"],
                   "kind": r["kind"], "at": r["at"]}
            intent = desk.get(e) or r.get("intent_id")
            if intent:
                row["intent_id"] = intent
            keep.append(row)
    return keep


def unreadable(state_dir: Path) -> Optional[dict[str, Any]]:
    """What ``EntryHolds`` last noted about unreadable store lines: None when it never
    noted any; ``{"unreadable_note": True}`` when the note is not JSON; ``{"cannot_open":
    "<error class>"}`` when this user cannot open it (the folder is the API user's and
    not shared with this one). Never raises: the ops alarm pass reads it."""
    marker = Path(state_dir) / "entry" / UNREADABLE_FILE
    try:
        text = marker.read_text("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        return {"cannot_open": type(error).__name__}
    try:
        seen = json.loads(text)
    except ValueError:
        return {"unreadable_note": True}
    return seen if isinstance(seen, dict) else {"unreadable_note": True}


__all__ = ["EntryHolds", "Refused", "Busy", "held_keys", "field_at", "parse_key", "number",
           "currency", "confirmed", "apply", "unreadable", "PARTY", "RECORD_TYPE"]
