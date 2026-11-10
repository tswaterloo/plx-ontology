"""Declared schema of the ingestion store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
  state TEXT NOT NULL, config_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_unit (
  run_id INTEGER NOT NULL, unit_id TEXT NOT NULL, source_id TEXT NOT NULL,
  kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
  reasons_json TEXT NOT NULL DEFAULT '[]', counts_json TEXT NOT NULL DEFAULT '{}',
  cursor_before_json TEXT, cursor_after_json TEXT, lag_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (run_id, unit_id));
CREATE TABLE IF NOT EXISTS unit_cursor (
  unit_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, kind TEXT NOT NULL,
  config_digest TEXT NOT NULL, cursor_json TEXT NOT NULL, updated_at TEXT NOT NULL,
  updated_run INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS item (
  unit_id TEXT NOT NULL, item_key TEXT NOT NULL, version TEXT NOT NULL,
  content_sha256 TEXT, citation TEXT, source_time TEXT, record_json TEXT NOT NULL,
  first_seen_run INTEGER NOT NULL, last_seen_run INTEGER NOT NULL,
  last_changed_run INTEGER NOT NULL, last_seen_pass INTEGER NOT NULL DEFAULT 0,
  absent_since_run INTEGER,
  PRIMARY KEY (unit_id, item_key));
"""

STORE = "ingestion"
STEPS = ((1, "initial", (_DDL,)),)
