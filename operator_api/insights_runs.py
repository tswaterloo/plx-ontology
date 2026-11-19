"""Scheduled reports and monitors: a saved insight with a ``schedule`` runs by itself as
its owner, and what it found comes to them on Home (``news``), never to anyone else.

Who it runs as when they are signed out (decided 11-19; options were: their stored session
or token, the owner's "view as", the Autopilot's opted-in readers file, or an identity
resolved fresh at each run): **a read-ahead identity of the owner resolved at each run**
(``backend.identity_for``, ``session_identity.warm_session``: the identity the app already
reads ahead with for people with no session). It is resolved now exactly as their sign-in
would be (an inactive, unverified, ungranted or excluded employee is refused and nothing is
read), it reads through their own door (``insights.read_sql``: plx.sql as them, their rows
and cells), it can write nothing and serves nothing (``SessionIdentity`` refuses it every
intent and audit), and it expires minutes later. No token or session of theirs is kept; a
grant taken away stops the next run. An owner key that is not one employee (a demo or
fixture sign-in) never runs.

What is kept (``insights.run``, migration 0046): when it ran, ok or failed with a plain
reason, how many rows, a keyed digest of each row (to tell a new row from one seen before)
and whether it came to them. No business row, value or text: the person opens the insight
and it reads again, live, as them.

When it comes to them (``notify``): "rows" when it returns any row, "new_rows" when a row
appears that the last good run did not have (the first run sets what was there), "every_run"
each time, "never" not at all (the run is still recorded and shown on the tile).

When it runs (business time zone): "hourly" when the last run is an hour old; "daily" once
a day from ``RUN_HOUR``; "weekly" once a week from Monday ``RUN_HOUR``. One API process runs
a given insight at a time (a transaction-scoped advisory lock and the last run read again
under it), so several API workers never run it twice.

Evidence: isolated-fixture (``operator_api/tests/test_insights_runs.py``) and the live run in
MASTER."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import models as m

RUN_HOUR = 7              # daily and weekly runs start at 7 AM business time
TICK_SECONDS = 60.0
IDENTITY_SECONDS = 300    # the run's identity expires this long after it is made
MOST_KEYS = 500           # row digests kept per run (insights.run.keys)
NEWS_DAYS = 14            # Home lists runs that came to them this long
MOST_NEWS = 20
KEY_FILE = "insights-run.key"
LOCK_CLASS = 46046        # pg_try_advisory_xact_lock(LOCK_CLASS, insight id)
EMPLOYEE_PREFIX = "employee:"

REASONS = {
    "not_employee": "It runs only for a Microsoft sign-in.",
    "refused": "Your access could not be confirmed when it ran, so nothing was read.",
}
# A failed read is kept as a fixed sentence per refusal code, never the refusal's own text:
# a database error's message can carry a value of a row (``invalid input syntax ...
# "<value>"``), and ``insights.run`` keeps no business value. The person opens the insight
# to see the live reason, read again as them.
FAILED = {
    "insight_invalid_sql": "The statement could not be read: open it to see why.",
    "insight_chart": "Its chart could not be drawn from what it read.",
    "insight_not_built": "It needs a column the business tables do not have yet.",
    "insight_gone": "It reads a table or column the business tables no longer have.",
    "insight_not_ready": "The business tables were being rebuilt when it ran.",
    "insight_too_slow": "It took longer than the read limit, so nothing was checked.",
    "insight_withheld": "It reads something outside what you can see, so nothing was checked.",
    "insight_watch": "The column it watches is not in what it reads now.",
}
FAILED_OTHER = "It could not be read when it ran."

log = logging.getLogger("operator_api")


def business_now(now: Optional[dt.datetime] = None) -> dt.datetime:
    from zoneinfo import ZoneInfo
    from operating.pg import business_views as bv
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(ZoneInfo(bv.BUSINESS_TZ))


def due(schedule: str, last: Optional[dt.datetime], now: dt.datetime) -> bool:
    """Whether an insight on ``schedule`` that last ran at ``last`` (None: never) runs at
    ``now`` (timezone-aware)."""
    if schedule not in ("hourly", "daily", "weekly"):
        return False
    local = business_now(now)
    if schedule == "hourly":
        return last is None or now - last >= dt.timedelta(minutes=59)
    start = local.replace(hour=RUN_HOUR, minute=0, second=0, microsecond=0)
    if schedule == "weekly":
        start -= dt.timedelta(days=local.weekday())
    if local < start:
        start -= dt.timedelta(days=7 if schedule == "weekly" else 1)
    return last is None or last < start


def owner_oid(owner: str) -> Optional[str]:
    """The Entra object id of an owner key that is one employee (``auth.Reader.key``: their
    principals, sorted and joined), else None."""
    parts = [p for p in str(owner or "").split(",") if p]
    if len(parts) != 1 or not parts[0].startswith(EMPLOYEE_PREFIX):
        return None
    oid = parts[0][len(EMPLOYEE_PREFIX):].strip().lower()
    return oid or None


def row_keys(insight_id: int, salt: bytes, rows: Sequence[Sequence[Any]]) -> list[str]:
    """A keyed digest of each row (HMAC of the insight id and its cells): equal rows, equal
    digests; nothing of the row can be read back from it without the key."""
    out = []
    for row in rows[:MOST_KEYS]:
        text = json.dumps([insight_id, list(row)], default=str, separators=(",", ":"))
        out.append(hmac.new(salt, text.encode(), hashlib.sha256).hexdigest()[:24])
    return out


def past(line: Mapping[str, Any], columns: Sequence[str], rows: Sequence[Sequence[Any]]
         ) -> int:
    """How many rows hold a number in ``line``'s column past its value (above: greater,
    below: less; exact decimals); a cell that is no number is not past it."""
    from decimal import Decimal
    from .charts import number
    at, mark, n = list(columns).index(line["column"]), Decimal(repr(line["value"])), 0
    for row in rows:
        value = number(row[at]) if at < len(row) else None
        if value is None or not value.is_finite():
            continue
        if (line["op"] == "above" and value > mark) or (line["op"] == "below" and value < mark):
            n += 1
    return n


def fires(notify: str, row_count: int, new_rows: int) -> bool:
    return {"rows": row_count > 0, "new_rows": new_rows > 0,
            "every_run": True}.get(notify, False)


class Runner:
    """Runs the due saved insights of every owner (``tick``) and lists their news."""

    def __init__(self, backend: Any, connect: Any, salt: bytes, *,
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)
                 ) -> None:
        """``connect``: a connection factory, or (``ready``) a callable answering
        (connect, salt) once runs can run in this process, None until then (``tick``
        then does nothing)."""
        self.backend, self.connect, self.salt, self.clock = backend, connect, salt, clock
        self._ready: Optional[Callable[[], Any]] = None if salt else connect
        self._thread: Optional[threading.Thread] = None

    def ready(self) -> bool:
        if self._ready is None:
            return True
        got = self._ready()
        if got is None:
            return False
        self.connect, self.salt = got
        self._ready = None
        return True

    # ------------------------------------------------------------------ running

    def candidates(self) -> list[tuple[int, str, str, str, Optional[dt.datetime]]]:
        with self.connect() as pg:
            return [tuple(r) for r in pg.execute(
                "SELECT i.id, i.owner, i.schedule, i.notify, (SELECT max(r.ran_at) FROM "
                "insights.run r WHERE r.insight_id = i.id) FROM insights.insight i WHERE "
                "i.deleted_at IS NULL AND i.schedule <> '' ORDER BY i.id").fetchall()]

    def tick(self) -> list[dict[str, Any]]:
        """Run every insight due now (each at most once across processes); what ran."""
        if not self.ready():
            return []
        now = self.clock()
        out = []
        for iid, _owner, schedule, _notify, last in self.candidates():
            if due(schedule, last, now):
                got = self.run_one(iid)
                if got is not None:
                    out.append(got)
        return out

    def run_one(self, iid: int) -> Optional[dict[str, Any]]:
        """Run insight ``iid`` if this process holds its lock and it is still due under it;
        its run row (None: not run here)."""
        with self.connect() as pg, pg.transaction():
            if not pg.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                              (LOCK_CLASS, iid)).fetchone()[0]:
                return None
            row = pg.execute(
                "SELECT i.owner, i.sql, i.schedule, i.notify, (SELECT max(r.ran_at) FROM "
                "insights.run r WHERE r.insight_id = i.id), k.keys, i.watch, k.over_rows FROM "
                "insights.insight i LEFT JOIN LATERAL (SELECT r.keys, r.over_rows FROM "
                "insights.run r WHERE r.insight_id = i.id AND r.status = 'ok' ORDER BY "
                "r.ran_at DESC, r.id DESC LIMIT 1) k ON true WHERE i.id = %s AND "
                "i.deleted_at IS NULL", (iid,)).fetchone()
            if row is None:
                return None
            owner, sql, schedule, notify, last, seen, watch, was_over = row
            if not due(schedule, last, self.clock()):
                return None
            done = self._read(iid, owner, sql, notify, seen, watch, was_over)
            pg.execute("INSERT INTO insights.run (insight_id, ran_at, status, reason, "
                       "row_count, keys, new_rows, fired, over_rows) VALUES (%s, %s, %s, %s, "
                       "%s, %s, %s, %s, %s)",
                       (iid, self.clock(), done["status"], done["reason"][:200],
                        done["row_count"], done["keys"], done["new_rows"], done["fired"],
                        done.get("over_rows")))
            return {"insight_id": iid, **{k: v for k, v in done.items() if k != "keys"}}

    def _read(self, iid: int, owner: str, sql: str, notify: str,
              seen: Optional[Sequence[str]], watch: Any = None,
              was_over: Optional[int] = None) -> dict[str, Any]:
        """The statement read as its owner, now: counts and row digests only (and, watching
        a line, how many rows are past it)."""
        from .auth import Reader
        from .errors import UserFacing
        from .insights import read_sql

        def failed(reason: str) -> dict[str, Any]:
            return {"status": "failed", "reason": reason, "row_count": 0, "keys": [],
                    "new_rows": 0, "fired": False}
        oid = owner_oid(owner)
        identity_for = getattr(self.backend, "identity_for", None)
        if oid is None or not callable(identity_for):
            return failed(REASONS["not_employee"])
        try:
            session = identity_for(oid, int(time.time()) + IDENTITY_SECONDS)
        except Exception:  # noqa: BLE001 - refused (no access, excluded): nothing read
            return failed(REASONS["refused"])
        if ",".join(sorted(getattr(session, "principals", ()) or ())) != owner:
            return failed(REASONS["refused"])   # never anyone but the owner
        reader = Reader(session, getattr(self.backend, "reader_mode", "entra"), "", None)
        try:
            got = read_sql(self.backend, reader, sql)
        except UserFacing as why:
            return failed(FAILED.get(why.code, FAILED_OTHER))
        rows = [r.get("cells", []) for r in got.get("rows") or () if isinstance(r, Mapping)]
        keys = row_keys(iid, self.salt, rows)
        new = 0 if seen is None else len(set(keys) - set(seen))
        out = {"status": "ok", "reason": "only the first rows were checked"
               if got.get("truncated") else "", "row_count": len(rows), "keys": keys,
               "new_rows": new, "fired": fires(notify, len(rows), new)}
        if notify == "crosses":
            from .insights import watch_of
            line = watch_of(watch)
            columns = list(got.get("columns") or ())
            if not line or line["column"] not in columns:
                return failed(FAILED["insight_watch"])
            over = past(line, columns, rows)
            # it came to them when it crossed: past the line now, none past it the last
            # good run (the first run: past it at all)
            out.update(over_rows=over, fired=over > 0 and not was_over)
        return out

    def start(self) -> None:
        """The run loop on a daemon thread (once)."""
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                time.sleep(TICK_SECONDS)
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - next tick
                    log.info("insights runs: %s", type(exc).__name__)
        self._thread = threading.Thread(target=loop, name="insights-runs", daemon=True)
        self._thread.start()


def run_key(data_dir: Path) -> bytes:
    """The row digests' key: made once in the app data folder (readable by the API's user
    only), the same for every process of this server."""
    import os
    import secrets
    path = data_dir / KEY_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("the insights run key is not 32 bytes")
    return key


# ----------------------------------------------------------------------------- news

def news(connect: Callable[[], Any], owner: str, now: Optional[dt.datetime] = None
         ) -> list[m.InsightNews]:
    """The runs of ``owner``'s own insights that came to them in the last ``NEWS_DAYS`` and
    that they have not put away, newest first."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with connect() as pg:
        rows = pg.execute(
            "SELECT r.id, r.insight_id, i.title, r.ran_at, r.row_count, r.new_rows, i.notify, "
            "r.reason, r.over_rows, i.watch FROM insights.run r JOIN insights.insight i ON "
            "i.id = r.insight_id "
            "WHERE i.owner = %s AND i.deleted_at IS NULL AND r.fired AND r.seen_at IS NULL "
            "AND r.ran_at >= %s ORDER BY r.ran_at DESC, r.id DESC LIMIT %s",
            (owner, now - dt.timedelta(days=NEWS_DAYS), MOST_NEWS)).fetchall()
    return [m.InsightNews(run_id=f"r{rid}", insight_id=f"s{iid}", title=title,
                          ran_at=ran.isoformat(timespec="seconds"), row_count=n, new_rows=new,
                          said=said(notify, n, new, reason, over, watch))
            for rid, iid, title, ran, n, new, notify, reason, over, watch in rows]


def said(notify: str, rows: int, new: int, reason: str = "", over: Optional[int] = None,
         watch: Any = None) -> str:
    """The one line Home shows for a run that came to them."""
    many = lambda k, one: f"{k} {one}" + ("" if k == 1 else "s")   # noqa: E731
    from .insights import watch_of
    line_ = watch_of(watch)
    if notify == "crosses" and over and line_:
        line = (f"{many(over, 'row')} now {line_['op']} {line_['value']:g} in "
                f"{line_['column'].replace('_', ' ')} ({many(rows, 'row')} in all).")
    elif notify == "new_rows" and new:
        line = f"{many(new, 'new row')} since it last ran ({many(rows, 'row')} now)."
    else:
        line = f"{many(rows, 'row')} now." if rows else "No rows now."
    return line + (" Only the first rows were checked." if reason else "")


def put_away(connect: Callable[[], Any], owner: str, run_id: str) -> bool:
    """Mark one of ``owner``'s news as seen (it leaves Home); False when it is not theirs."""
    if not (run_id.startswith("r") and run_id[1:].isdigit() and len(run_id) < 20):
        return False
    with connect() as pg:
        got = pg.execute(
            "UPDATE insights.run r SET seen_at = now() FROM insights.insight i WHERE "
            "r.id = %s AND i.id = r.insight_id AND i.owner = %s AND r.seen_at IS NULL "
            "RETURNING r.id", (int(run_id[1:]), owner)).fetchone()
        pg.commit()
    return got is not None
