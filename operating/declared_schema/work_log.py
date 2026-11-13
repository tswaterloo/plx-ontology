"""Declared schema of the work_log store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
  reader TEXT NOT NULL, reader_name TEXT,
  kind TEXT NOT NULL, verb TEXT NOT NULL, title TEXT NOT NULL, detail TEXT,
  thread TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT,
  records TEXT NOT NULL DEFAULT '[]', people TEXT NOT NULL DEFAULT '[]',
  awaiting TEXT, once TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS event_reader ON event (reader, seq);
CREATE INDEX IF NOT EXISTS event_thread ON event (reader, thread, seq);
CREATE TABLE IF NOT EXISTS event_record (seq INTEGER NOT NULL, object_id TEXT NOT NULL,
  PRIMARY KEY (object_id, seq));
CREATE TRIGGER IF NOT EXISTS event_no_update BEFORE UPDATE ON event
BEGIN SELECT RAISE(ABORT, 'work_log_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS event_no_delete BEFORE DELETE ON event
BEGIN SELECT RAISE(ABORT, 'work_log_is_append_only'); END;
CREATE TABLE IF NOT EXISTS thread (reader TEXT NOT NULL, thread TEXT NOT NULL,
  name TEXT, pinned INTEGER NOT NULL DEFAULT 0, case_id TEXT, closed_at TEXT,
  at TEXT NOT NULL, PRIMARY KEY (reader, thread));
"""

STORE = "work_log"
# A row's ``detail`` (an Ask answer, up to 200 KB) sits before ``people`` and ``awaiting``,
# so reading those from the row walks the answer's overflow pages: the reads that check
# every matching row (a page's people re-check, the autopilot's answered-in-Workbench
# lookup) are answered from these indexes alone, never from the rows (AP-11).
_COVERING = """
CREATE INDEX IF NOT EXISTS event_light ON event (reader, seq, thread, kind, at, people);
CREATE INDEX IF NOT EXISTS event_awaiting ON event (kind, at, awaiting)
  WHERE awaiting IS NOT NULL;
"""

STEPS = ((1, "initial", (_DDL,)),
         (2, "covering_indexes", (_COVERING,)))
