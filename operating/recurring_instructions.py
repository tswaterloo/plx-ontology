"""A person's saved recurring instructions ("every Monday at 8, chase the open POs"): the
trigger ``operating.instruct.recurring`` read from the line, the hour it runs (US Eastern),
and its state (``active`` or ``paused``; stopping one deletes it). Kept per owner in one
private SQLite file; every read and change names the owner and touches only their rows.

``next_due`` is pure: the first run after a moment for a ``schedule.daily`` (working days
only when ``weekdays_only``), ``schedule.weekly`` (``weekday``) or ``schedule.monthly``
(the first working day) trigger; an event trigger (``message.received``) has none here.
Nothing here reads a business system, runs an instruction or sends anything.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

FILE = "recurring-instructions.sqlite"
EASTERN = ZoneInfo("America/New_York")
KINDS = ("schedule.daily", "schedule.weekly", "schedule.monthly")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
STATES = ("active", "paused")
MOST_PER_OWNER = 20
DEFAULT_HOUR = 8


class RecurringRefused(ValueError):
    """A trigger, hour, weekday or state this store does not keep."""


def next_due(kind: str, params: dict[str, Any], hour: int, after: datetime) -> Optional[datetime]:
    """The first run strictly after ``after`` (aware), in UTC; None for a trigger without
    a schedule."""
    if kind not in KINDS:
        return None
    local = after.astimezone(EASTERN)
    day = local.date()
    for _ in range(400):
        at = datetime(day.year, day.month, day.day, hour, tzinfo=EASTERN)
        if at > local and _matches(kind, params, day):
            return at.astimezone(timezone.utc)
        day += timedelta(days=1)
    return None


def _matches(kind: str, params: dict[str, Any], day: Any) -> bool:
    if kind == "schedule.daily":
        return not params.get("weekdays_only") or day.weekday() < 5
    if kind == "schedule.weekly":
        return WEEKDAYS[day.weekday()] == params.get("weekday", "monday")
    first = day.replace(day=1)
    while first.weekday() >= 5:
        first += timedelta(days=1)
    return day == first


def _check(kind: str, params: dict[str, Any], hour: int) -> None:
    if kind == "message.received":
        from operating.recurring_events import rule_ok
        if not rule_ok(params) or not params.get("email"):
            raise RecurringRefused("A \"whenever\" instruction names who the mail is from "
                                   "or words in its subject.")
        return
    if kind not in KINDS:
        raise RecurringRefused("Only a daily, weekly or monthly instruction is kept here.")
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        raise RecurringRefused("The hour is 0 to 23.")
    if kind == "schedule.weekly" and params.get("weekday") not in WEEKDAYS:
        raise RecurringRefused("A weekly instruction names its weekday.")


class RecurringStore:
    def __init__(self, data_dir: Any) -> None:
        self.path = Path(data_dir) / FILE

    def _db(self) -> sqlite3.Connection:
        fresh = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        from operating import declared_schema
        from operating.declared_schema import recurring_instructions as declared
        declared_schema.apply(conn, declared)
        if fresh:
            from operating.file_modes import keep_private
            keep_private(self.path, 0o600)
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["params"] = json.loads(out["params"])
        return out

    def add(self, owner: str, text: str, label: str, trigger: dict[str, Any],
            hour: int = DEFAULT_HOUR, now: Optional[float] = None) -> dict[str, Any]:
        kind, params = trigger.get("kind", ""), dict(trigger.get("params") or {})
        _check(kind, params, hour)
        now = time.time() if now is None else now
        due = next_due(kind, params, hour, datetime.fromtimestamp(now, timezone.utc))
        with self._db() as conn:
            if conn.execute("SELECT count(*) FROM recurring WHERE owner = ?",
                            (owner,)).fetchone()[0] >= MOST_PER_OWNER:
                raise RecurringRefused(f"Keep at most {MOST_PER_OWNER} recurring instructions.")
            rid = secrets.token_hex(8)
            conn.execute("INSERT INTO recurring VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, "
                         "NULL, ?, NULL)", (rid, owner, text, label[:120], kind,
                                            json.dumps(params), str(trigger.get("words") or ""),
                                            hour, now, due.timestamp() if due else None))
        return self.get(owner, rid)

    def list(self, owner: str) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM recurring WHERE owner = ? ORDER BY created",
                                (owner,)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, owner: str, rid: str) -> Optional[dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM recurring WHERE owner = ? AND id = ?",
                               (owner, rid)).fetchone()
        return self._row(row) if row else None

    def change(self, owner: str, rid: str, *, hour: Optional[int] = None,
               weekday: Optional[str] = None, state: Optional[str] = None,
               label: Optional[str] = None, now: Optional[float] = None
               ) -> Optional[dict[str, Any]]:
        held = self.get(owner, rid)
        if held is None:
            return None
        params = dict(held["params"])
        if weekday is not None:
            if held["kind"] != "schedule.weekly":
                raise RecurringRefused("Only a weekly instruction has a weekday.")
            params["weekday"] = weekday
        hour = held["hour"] if hour is None else hour
        state = held["state"] if state is None else state
        if state not in STATES:
            raise RecurringRefused("An instruction is active or paused.")
        _check(held["kind"], params, hour)
        now = time.time() if now is None else now
        due = next_due(held["kind"], params, hour, datetime.fromtimestamp(now, timezone.utc))
        with self._db() as conn:
            conn.execute("UPDATE recurring SET params = ?, hour = ?, state = ?, label = ?, "
                         "next_run = ? WHERE owner = ? AND id = ?",
                         (json.dumps(params), hour, state,
                          (label if label is not None else held["label"])[:120],
                          due.timestamp() if due else None, owner, rid))
        return self.get(owner, rid)

    def stop(self, owner: str, rid: str) -> bool:
        with self._db() as conn:
            return conn.execute("DELETE FROM recurring WHERE owner = ? AND id = ?",
                                (owner, rid)).rowcount > 0

    def due(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        if not self.path.exists():
            return []
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM recurring WHERE state = 'active' AND next_run "
                                "IS NOT NULL AND next_run <= ? ORDER BY next_run",
                                (now,)).fetchall()
        return [self._row(r) for r in rows]

    def claim(self, row: dict[str, Any], now: Optional[float] = None) -> bool:
        """Take a due row for one run: its next run moves on only if no other process moved
        it first (two servers overlapping in a deploy never both run it)."""
        now = time.time() if now is None else now
        due = next_due(row["kind"], row["params"], row["hour"],
                       datetime.fromtimestamp(now, timezone.utc))
        with self._db() as conn:
            return conn.execute(
                "UPDATE recurring SET last_run = ?, last_result = 'Running', next_run = ? "
                "WHERE owner = ? AND id = ? AND state = 'active' AND next_run = ?",
                (now, due.timestamp() if due else None, row["owner"], row["id"],
                 row["next_run"])).rowcount == 1

    def ran(self, owner: str, rid: str, result: str, now: Optional[float] = None) -> None:
        """Record a run (or a run that could not start) and move to the next one: a run
        missed while the server was down runs once, never once per missed day."""
        held = self.get(owner, rid)
        if held is None:
            return
        now = time.time() if now is None else now
        due = next_due(held["kind"], held["params"], held["hour"],
                       datetime.fromtimestamp(now, timezone.utc))
        with self._db() as conn:
            conn.execute("UPDATE recurring SET last_run = ?, last_result = ?, next_run = ? "
                         "WHERE owner = ? AND id = ?",
                         (now, result[:200], due.timestamp() if due else None, owner, rid))

    def result(self, owner: str, rid: str, result: str) -> None:
        """The words of the run under way once it ends (``ran`` already moved it on)."""
        with self._db() as conn:
            conn.execute("UPDATE recurring SET last_result = ? WHERE owner = ? AND id = ?",
                         (result[:200], owner, rid))

    def events(self) -> list[dict[str, Any]]:
        """Every owner's active event rules (mirrored for the autopilot's fast feed)."""
        if not self.path.exists():
            return []
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM recurring WHERE kind = 'message.received' AND "
                                "state = 'active'").fetchall()
        return [self._row(r) for r in rows]
