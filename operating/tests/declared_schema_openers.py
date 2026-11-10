"""Open every declared SQLite store through its real constructor in a fresh directory and
return the schema of the file it made. Used by ``test_declared_schema`` and by the script
that recorded the pinned dumps from the code before the schemas were declared (the same
functions run unchanged on both)."""
from __future__ import annotations

import sqlite3
import types
from pathlib import Path
from typing import Callable


def dump(path: Path) -> list[list[str]]:
    """Every object of the file's schema (type, name, table, sql), sorted."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted([t, n, tb, sql or ""] for t, n, tb, sql in db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE "
            "'sqlite_%'"))
    finally:
        db.close()


def _mail(d: Path) -> Path:
    from integrations.capture_mail import MailCaptureStore
    s = MailCaptureStore(d)
    s.db.close()
    return s.path


def _sharepoint(d: Path) -> Path:
    from integrations.capture_sharepoint import SharePointCaptureStore
    s = SharePointCaptureStore(d)
    s.db.close()
    return s.path


def _capture_store(d: Path) -> Path:
    from integrations.capture_store import CaptureStore
    s = CaptureStore(d / "c.sqlite", types.SimpleNamespace(tenant="t", allows=lambda *a, **k: True))
    s._db.close()
    return d / "c.sqlite"


def _sheet_readings(d: Path) -> Path:
    from integrations.sheet_readings import open_readings
    open_readings(d / "s.sqlite").close()
    return d / "s.sqlite"


def _cases(d: Path) -> Path:
    from operating.cases import CaseStore
    CaseStore(d / "k.sqlite")
    return d / "k.sqlite"


def _playbook(d: Path) -> Path:
    from operating.playbook_reports import PlaybookReportStore
    PlaybookReportStore(d / "p.sqlite")
    return d / "p.sqlite"


def _action_path(d: Path) -> Path:
    from operating.action_path import ActionPath
    from operating.cases import CaseStore
    ActionPath(d / "a.sqlite", CaseStore(d / "ac.sqlite"))
    return d / "a.sqlite"


def _ai_watcher(d: Path) -> Path:
    from operating import ai_watcher
    ai_watcher.open_store(d).close()
    return ai_watcher.store_path(d)


def _originals(d: Path) -> Path:
    from operating.case_originals import OriginalStore
    s = OriginalStore(d / "o")
    return next((d / "o").rglob("*.sqlite"), None) or next(
        p for p in (d / "o").rglob("*") if p.is_file() and p.suffix in (".db", ".sqlite"))


def _owner(d: Path) -> Path:
    from operating.goal2_exec_owner import IsolatedOwnerSystem
    IsolatedOwnerSystem(d / "w.sqlite")
    return d / "w.sqlite"


def _ingest(d: Path) -> Path:
    from operating.ingestion import IngestStore
    s = IngestStore(d)
    s.db.close()
    return s.path


def _labour(d: Path) -> Path:
    from operating.labour_hours import LabourStore
    LabourStore(d / "l.sqlite").db.close()
    return d / "l.sqlite"


def _inbox(d: Path) -> Path:
    from operating.proactive_engine import _open_inbox
    _open_inbox(d / "i.sqlite").close()
    return d / "i.sqlite"


def _inbox_changes(d: Path) -> Path:
    from operating.proactive_incremental import _open
    _open(d / "ic.sqlite").close()
    return d / "ic.sqlite"


def _watchlists(d: Path) -> Path:
    from operating.watchlists import _connect
    _connect(d / "w.sqlite").close()
    return d / "w.sqlite"


def _feedback(d: Path) -> Path:
    from operator_api.feedback import FeedbackStore
    s = FeedbackStore(d / "fb")
    s._conn.close()
    return d / "fb" / "feedback.sqlite"


def _page_store(d: Path) -> Path:
    from operator_api.page_store import PageStore
    s = PageStore(d, "code")
    s._conn().close()
    return s.path


def _page_counts(d: Path) -> Path:
    from integrations.page_counts import PageCounts
    p = PageCounts(d / "pc.sqlite")
    p._conn().close()
    return d / "pc.sqlite"


def _captured_text(d: Path) -> Path:
    from integrations.captured_text import KeptReadings
    s = KeptReadings(d / "ct.sqlite", "code")
    s._db.close()
    return d / "ct.sqlite"


def _kept_members(d: Path) -> Path:
    from integrations.kept_members import KeptMembers
    s = KeptMembers(d / "km.sqlite", "code")
    s._db.close()
    return d / "km.sqlite"


def _kept_fields(d: Path) -> Path:
    from ontology.document_fields import KeptFields
    s = KeptFields(d / "kf.sqlite", "code")
    s._db.close()
    return d / "kf.sqlite"


def _memo(d: Path) -> Path:
    from ontology.mention_memo import DocumentMemo
    s = DocumentMemo(d / "mm.sqlite", None, "t", 1)
    s._db.close()
    return d / "mm.sqlite"


def _briefs(d: Path) -> Path:
    from operating.briefs import WeeklyArchive
    a = WeeklyArchive(d / "b.sqlite")
    a._conn().close()
    return d / "b.sqlite"


def _replay_progress(d: Path) -> Path:
    from operating.autopilot_replay import Progress
    Progress(d / "r.sqlite").conn.close()
    return d / "r.sqlite"


def _autopilot(d: Path) -> Path:
    from operating.autopilot import Queue
    Queue(d / "q.sqlite").close()
    return d / "q.sqlite"


def _delta(d: Path) -> Path:
    from ontology.delta import State
    State(d / "ds.sqlite").conn.close()
    return d / "ds.sqlite"


def _project_objects(d: Path) -> Path:
    from ontology.project_incremental import _Objects
    o = _Objects(d / "po.sqlite")
    o.conn.commit()
    o.conn.close()
    return d / "po.sqlite"


def _replay_guard(d: Path) -> Path:
    from operating.session_identity import TicketReplayGuard
    TicketReplayGuard(d / "t.sqlite")
    return d / "t.sqlite"


def _journal(d: Path) -> Path:
    from operator_api.workspace.session_journal import SessionJournal
    SessionJournal(d / "j.sqlite")
    return d / "j.sqlite"


def _confirmation_forms(d: Path) -> Path:
    from operating.confirmation_forms import ConfirmationForms
    ConfirmationForms(d / "cf.sqlite")
    return d / "cf.sqlite"


def _kind_confirmation(d: Path) -> Path:
    from operating.kind_confirmation import KindConfirmations
    KindConfirmations(d / "kc.sqlite")
    return d / "kc.sqlite"


OPENERS: dict[str, Callable[[Path], Path]] = {
    "capture_mail": _mail, "capture_sharepoint": _sharepoint, "capture_store": _capture_store,
    "sheet_readings": _sheet_readings, "cases": _cases, "playbook_reports": _playbook,
    "action_path": _action_path, "ai_watcher": _ai_watcher, "case_originals": _originals,
    "goal2_exec_owner": _owner, "ingestion": _ingest, "labour_hours": _labour,
    "proactive_inbox": _inbox, "proactive_incremental": _inbox_changes,
    "watchlists": _watchlists, "feedback": _feedback, "page_store": _page_store,
    "page_counts": _page_counts, "captured_text": _captured_text,
    "kept_members": _kept_members, "document_fields": _kept_fields,
    "mention_memo": _memo, "briefs": _briefs, "autopilot_replay": _replay_progress,
    "autopilot_queue": _autopilot, "delta_state": _delta, "project_objects": _project_objects,
    "session_replay": _replay_guard, "session_journal": _journal,
    "confirmation_forms": _confirmation_forms, "kind_confirmation": _kind_confirmation,
}


# Declared DDL whose store has no cheap constructor, or that is applied later than open:
# (module, name), read as the module's attribute (the same on both sides of the move).
CONSTANTS: list[tuple[str, str]] = [
    ("integrations.source_mirror", "_DDL"), ("integrations.source_mirror", "_CHANGE_DDL"),
    ("operating.host", "_LEDGER_DDL"), ("operating.cases", "_DDL"),
    ("operating.cases", "_CURSOR_DDL"), ("operating.playbook_reports", "_DDL"),
    ("operating.ontology_tools_changes", "_INDEX_DDL"),
    ("operating.ontology_tools_changes", "_INDEX_INDEXES"),
    ("integrations.capture_mail", "_FOLDER_LISTING_DDL"),
    ("integrations.capture_mail", "_PRIORITY_DDL"),
    ("operating.proactive_engine", "_VISITS_DDL"),
]


def constants_digest() -> dict[str, str]:
    import hashlib
    import importlib
    out = {}
    for module, name in CONSTANTS:
        value = getattr(importlib.import_module(module), name)
        text = value if isinstance(value, str) else "\n--\n".join(value)
        out[f"{module}.{name}"] = hashlib.sha256(text.encode()).hexdigest()
    return out
