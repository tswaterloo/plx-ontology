"""Declared schema of the cases store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS case_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL,
  case_seq INTEGER NOT NULL CHECK(case_seq >= 1),
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL UNIQUE,
  UNIQUE(case_id, case_seq)
);
CREATE INDEX IF NOT EXISTS idx_case_event_kind ON case_event(kind, seq);
CREATE INDEX IF NOT EXISTS idx_case_event_case_kind ON case_event(case_id, kind);
CREATE TRIGGER IF NOT EXISTS case_event_no_update BEFORE UPDATE ON case_event
BEGIN SELECT RAISE(ABORT, 'case_event_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS case_event_no_delete BEFORE DELETE ON case_event
BEGIN SELECT RAISE(ABORT, 'case_event_is_append_only'); END;
"""

_CURSOR_DDL = ("CREATE TABLE IF NOT EXISTS case_cursor (case_id TEXT NOT NULL, "
               "session_ref TEXT NOT NULL, case_seq INTEGER NOT NULL, "
               "PRIMARY KEY (case_id, session_ref))")

STORE = "cases"
STEPS = ((1, "initial", (_DDL,)),)
# The read cursors live in a second file, beside the case store.
CURSOR_STEPS = ((1, "initial", (_CURSOR_DDL,)),)
