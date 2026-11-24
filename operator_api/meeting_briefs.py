"""Meeting briefs prepared ahead and kept (owner 11-24, W1): the brief of each of the
reader's meetings today and tomorrow is built whole (no time budget, no deadline: Guardrail
1) behind the Meetings and Home reads, kept per reader, and the page draws the kept one.

* ``MeetingBriefStore``: one JSON file per (reader, record), owner-only, in the app's data
  directory. A kept brief is served only under the same grant (policy fingerprint, markings,
  people-exclusion stamp) and only while every source it shows still opens for the reader
  (``WeeklyArchive``'s rule). Any file failure is a miss, never a failed read.
* A kept brief older than ``REFRESH_AFTER`` is served at once and prepared again behind the
  read, so the person never waits on a rebuild and never sees a day-old brief for long; the
  page says when it was kept (``stored_at``).
* The person's own notes about the meeting and its parties (``your_notes``, memory items
  naming those records) are added when the brief is served, never kept in it: a note added
  a second ago is in the next draw, and a note is never in another person's brief.
Evidence: isolated-fixture (``operator_api/tests/test_meeting_briefs.py``)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from operating import briefs as br

LOG = logging.getLogger(__name__)
DIR = "meeting-briefs"
REFRESH_AFTER = 30 * 60      # seconds a kept brief is served before it is prepared again
AHEAD_EVERY = 20 * 60        # seconds between one reader's prepare-ahead passes


def _name(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:40]


class MeetingBriefStore:
    def __init__(self, root: Any) -> None:
        self.root = Path(root)

    def _path(self, reader: str, object_id: str) -> Path:
        return self.root / _name(reader) / (_name(object_id) + ".json")

    def get(self, reader: str, object_id: str, grant: str, decide: Optional[Callable]
            ) -> Optional[dict[str, Any]]:
        try:
            doc = json.loads(self._path(reader, object_id).read_text("utf-8"))
            if doc.get("grant") != grant or doc.get("object_id") != object_id:
                return None
            raw = doc["body"]
            handles = br.WeeklyArchive.handles(raw)
            if handles:
                if decide is None:
                    return None
                decided = decide(handles)
                if any(h not in decided or decided[h] is not None for h in handles):
                    return None
            return dict(raw, stored_at=doc["stored_at"])
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001 - a miss, said in the log
            LOG.info("kept meeting brief not used: unreadable", exc_info=True)
            return None

    def put(self, reader: str, object_id: str, grant: str, raw: Mapping[str, Any]) -> bool:
        try:
            path = self._path(reader, object_id)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            body = json.dumps({"object_id": object_id, "grant": grant, "body": raw,
                               "stored_at": datetime.now(timezone.utc).isoformat(
                                   timespec="seconds")}, default=str)
            tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(tmp, path)
            return True
        except Exception:  # noqa: BLE001 - not kept, said in the log
            LOG.warning("meeting brief not kept", exc_info=True)
            return False


def age_seconds(stored_at: Optional[str]) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(stored_at))
                ).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def note_rows(items: list[Mapping[str, Any]], ids: set[str]) -> list[dict[str, Any]]:
    """Brief rows of the person's own notes naming one of ``ids``."""
    return [{"label": str(i["text"]), "who": "Your note", "date": str(i.get("at") or "")[:10] or None,
             "detail": None, "sources": []}
            for i in items if ids & set(i.get("records") or ())]


def notes_section(items: list[Mapping[str, Any]], ids: set[str]) -> dict[str, Any]:
    rows = note_rows(items, ids)
    return {"key": "your_notes", "title": "Your notes", "state": "shown" if rows else "empty",
            "rows": rows, "more": 0, "figures": [],
            "note": None if rows else "Nothing noted yet. Add context below: it is private "
                                      "to you and used in this brief, in Ask and in Assist."}
