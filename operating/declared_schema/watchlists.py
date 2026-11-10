"""Declared schema of the watchlists store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS follows (reader TEXT NOT NULL, object_id TEXT NOT NULL,
  object_type TEXT, label TEXT, at TEXT NOT NULL, baseline_at TEXT,
  PRIMARY KEY (reader, object_id));
CREATE INDEX IF NOT EXISTS follows_object ON follows (object_id);
CREATE TABLE IF NOT EXISTS rules (rule_id TEXT PRIMARY KEY, reader TEXT NOT NULL,
  text TEXT NOT NULL, spec TEXT NOT NULL, description TEXT NOT NULL, at TEXT NOT NULL,
  baseline_at TEXT, state TEXT NOT NULL DEFAULT 'active', note TEXT, cursor TEXT);
CREATE TABLE IF NOT EXISTS seen (object_id TEXT PRIMARY KEY, object_type TEXT,
  full INTEGER NOT NULL, props TEXT NOT NULL, neighbors TEXT, gate TEXT, label TEXT,
  at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (rule_id TEXT NOT NULL, grp TEXT NOT NULL,
  subject TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (rule_id, grp, subject, at));
CREATE TABLE IF NOT EXISTS alerts (alert_id TEXT PRIMARY KEY, reader TEXT NOT NULL,
  origin TEXT NOT NULL, origin_id TEXT NOT NULL, kind TEXT NOT NULL, about TEXT NOT NULL,
  object_id TEXT NOT NULL, body TEXT NOT NULL, gates TEXT NOT NULL, at TEXT NOT NULL,
  read_at TEXT);
CREATE INDEX IF NOT EXISTS alerts_reader ON alerts (reader, at);
CREATE TABLE IF NOT EXISTS signal_reads (reader TEXT NOT NULL, alert_id TEXT NOT NULL,
  at TEXT NOT NULL, PRIMARY KEY (reader, alert_id));
"""

STORE = "watchlists"
STEPS = ((1, "initial", (_DDL,)),)
