"""Declared schema of the playbook_reports store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS playbook_report (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_sha256 TEXT NOT NULL UNIQUE,
  case_id TEXT NOT NULL,
  playbook_id TEXT NOT NULL,
  playbook_version TEXT NOT NULL,
  definition_sha256 TEXT NOT NULL,
  evaluator_version TEXT NOT NULL,
  case_boundary_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  passed_all INTEGER NOT NULL CHECK(passed_all IN (0,1)),
  passed_all_clean INTEGER NOT NULL CHECK(passed_all_clean IN (0,1)),
  recorded_at TEXT NOT NULL,
  recorded_by TEXT NOT NULL,
  transcript_source TEXT NOT NULL,
  report_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  row_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_playbook_report_case ON playbook_report(case_id, playbook_id, id);
CREATE TRIGGER IF NOT EXISTS playbook_report_no_update BEFORE UPDATE ON playbook_report
BEGIN SELECT RAISE(ABORT, 'playbook_report_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS playbook_report_no_delete BEFORE DELETE ON playbook_report
BEGIN SELECT RAISE(ABORT, 'playbook_report_is_append_only'); END;
CREATE TABLE IF NOT EXISTS playbook_change (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  row_hash TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS playbook_change_no_update BEFORE UPDATE ON playbook_change
BEGIN SELECT RAISE(ABORT, 'playbook_change_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS playbook_change_no_delete BEFORE DELETE ON playbook_change
BEGIN SELECT RAISE(ABORT, 'playbook_change_is_append_only'); END;
"""

STORE = "playbook_reports"
STEPS = ((1, "initial", (_DDL,)),)
