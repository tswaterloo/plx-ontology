"""Event triggers for recurring instructions ("whenever a mail from acme.com about a ship
date arrives, ..."): the rules the API mirrors for the autopilot's fast feed, the pure match
of a ``message.received`` event, and the fires the feed notes for the API to run.

A rule matches only on what the person set (model-free, never guessed): ``from`` a
substring of the sender (an address or a domain) and/or ``subject`` words all present,
case-insensitive, and only mail that reached the owner's own mailbox. A rule with neither
is refused (it would be every mail). Files are private (0600) under the autopilot state.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

RULES_FILE = "recurring-rules.json"
FIRES_FILE = "recurring-fires.sqlite"
EVENT = "message.received"


def rule_ok(params: Mapping[str, Any]) -> bool:
    return bool(str(params.get("from") or "").strip() or str(params.get("subject") or "").strip())


def matches(rule: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """``event`` (a ``message.received``) is one ``rule`` fires on."""
    if event.get("event_type") != EVENT or event.get("unaddressed") or not rule_ok(rule):
        return False
    if str(event.get("mailbox") or "").lower() != str(rule.get("email") or "").lower() \
            or not rule.get("email"):
        return False
    sender = str(event.get("sender") or "").lower()
    if str(rule.get("from") or "").strip() and str(rule["from"]).strip().lower() not in sender:
        return False
    subject = str(event.get("subject") or "").lower()
    words = re.findall(r"\w+", str(rule.get("subject") or "").lower())
    return all(w in subject for w in words)


def write_rules(dirpath: Path, rules: Iterable[Mapping[str, Any]]) -> None:
    """The active event rules (rid, owner, email, from, subject), replaced atomically."""
    dirpath.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = dirpath / f".{RULES_FILE}.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump([dict(r) for r in rules], out)
    os.replace(tmp, dirpath / RULES_FILE)


def read_rules(dirpath: Path) -> list[dict[str, Any]]:
    try:
        rules = json.loads((dirpath / RULES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


class Fires:
    def __init__(self, dirpath: Path) -> None:
        self.path = Path(dirpath) / FIRES_FILE

    def _db(self) -> sqlite3.Connection:
        fresh = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        from operating import declared_schema
        from operating.declared_schema import recurring_fires as declared
        declared_schema.apply(conn, declared)
        if fresh:
            os.chmod(self.path, 0o600)
        return conn

    def note(self, rules: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]],
             now: float | None = None) -> int:
        """A fire per (rule, matching event), once each; how many were new."""
        now = time.time() if now is None else now
        rows = {(str(r["rid"]), str(r["owner"]), str(e["object_id"]))
                for e in events if e.get("object_id") for r in rules if matches(r, e)}
        if not rows:
            return 0
        with self._db() as conn:
            return conn.executemany("INSERT OR IGNORE INTO fires (rid, owner, object_id, at) "
                                    "VALUES (?, ?, ?, ?)",
                                    [(*row, now) for row in sorted(rows)]).rowcount

    def take(self, limit: int = 20) -> list[dict[str, Any]]:
        """Fires not yet taken, each taken by exactly one caller."""
        if not self.path.exists():
            return []
        out = []
        with self._db() as conn:
            for row in conn.execute("SELECT rowid, * FROM fires WHERE taken = 0 ORDER BY at "
                                    "LIMIT ?", (limit,)).fetchall():
                if conn.execute("UPDATE fires SET taken = 1 WHERE rowid = ? AND taken = 0",
                                (row["rowid"],)).rowcount == 1:
                    out.append({k: row[k] for k in ("rid", "owner", "object_id", "at")})
        return out
