"""Digests a person may switch on: a morning briefing, and weekly digests of decisions, customer
feedback and requests still waiting. Each is a plain question the person's own Ask answers as
them (their door, their rows, people excluded) on a schedule they chose; the answer's words are
kept for them alone (``ReportStore``, one private SQLite file) and shown on their Digests list.

Off until the person turns one on: no row exists, so nothing runs. A switched-on digest is a
recurring row (``operating.recurring_instructions``: ``params = {"digest": <id>}``), so its
schedule, hour, pause and stop are the recurring store's. The questions say what to cover, in
words; no rule reads a message here, the model reading the person's own data does.

Evidence: isolated-fixture (``operator_api/tests/test_digests.py``); live-unverified.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

FILE = "digest-reports.sqlite"
MOST_CHARS = 8000

CATALOG: tuple[dict[str, Any], ...] = (
    {"id": "morning", "title": "Morning briefing", "when": "every weekday", "weekdays_only": True,
     "hour": 7, "question": (
         "Give me my morning briefing: what changed overnight that needs me, what is due or late "
         "today, who is waiting on a reply from me, and my meetings today. Keep it short and say "
         "where each item comes from.")},
    {"id": "decisions", "title": "Decisions this week", "when": "every Friday", "weekday": "friday",
     "hour": 16, "question": (
         "List the decisions made this past week in mail and Teams that I took part in or that "
         "affect my work: what was decided, by whom, when, and where to find it.")},
    {"id": "feedback", "title": "Customer feedback this week", "when": "every Friday",
     "weekday": "friday", "hour": 15, "question": (
         "Summarise the customer feedback and complaints that came in by mail or Teams this past "
         "week: who, what they said, and whether anyone has answered them.")},
    {"id": "requests", "title": "Requests still waiting", "when": "every Monday",
     "weekday": "monday", "hour": 8, "question": (
         "List the requests made of me in mail and Teams over the past two weeks that are still "
         "unanswered: who asked, for what, and since when.")},
)


def entry(digest_id: str) -> Optional[dict[str, Any]]:
    return next((d for d in CATALOG if d["id"] == digest_id), None)


def trigger(spec: dict[str, Any]) -> dict[str, Any]:
    """The recurring trigger of a catalog entry, carrying its digest id."""
    if spec.get("weekday"):
        return {"kind": "schedule.weekly",
                "params": {"weekday": spec["weekday"], "digest": spec["id"]},
                "words": spec["when"]}
    return {"kind": "schedule.daily",
            "params": {"weekdays_only": bool(spec.get("weekdays_only")), "digest": spec["id"]},
            "words": spec["when"]}


class ReportStore:
    """The latest report of each (owner, recurring row); every read and write names the owner."""

    def __init__(self, data_dir: Any) -> None:
        self.path = Path(data_dir) / FILE

    def _db(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fresh = not self.path.exists()
        conn = sqlite3.connect(self.path, timeout=10)
        from operating import declared_schema
        from operating.declared_schema import digest_reports as declared
        declared_schema.apply(conn, declared)
        if fresh:
            from operating.file_modes import keep_private
            keep_private(self.path, 0o600)
        return conn

    def put(self, owner: str, rid: str, text: str, now: Optional[float] = None) -> None:
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO report VALUES (?, ?, ?, ?)",
                         (owner, rid, time.time() if now is None else now, text[:MOST_CHARS]))

    def get(self, owner: str, rid: str) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        with self._db() as conn:
            row = conn.execute("SELECT ran_at, text FROM report WHERE owner = ? AND rid = ?",
                               (owner, rid)).fetchone()
        return {"ran_at": row[0], "text": row[1]} if row else None

    def drop(self, owner: str, rid: str) -> None:
        if self.path.exists():
            with self._db() as conn:
                conn.execute("DELETE FROM report WHERE owner = ? AND rid = ?", (owner, rid))
