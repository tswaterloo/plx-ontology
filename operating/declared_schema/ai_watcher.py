"""Declared schema of the ai_watcher store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS layers (store TEXT PRIMARY KEY, base_digest TEXT NOT NULL,
  seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen (object_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
  props TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue (object_id TEXT PRIMARY KEY, object_type TEXT NOT NULL,
  handle TEXT NOT NULL, change TEXT NOT NULL, changed TEXT NOT NULL, event_at TEXT NOT NULL,
  since TEXT, thread TEXT, queued_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  claimed INTEGER, traits TEXT);
CREATE TABLE IF NOT EXISTS sessions (session_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('batch', 'retry')), parent INTEGER,
  started_at TEXT NOT NULL, ended_at TEXT, state TEXT NOT NULL CHECK (state IN
  ('running', 'answered', 'folded', 'interrupted', 'failed')), entries TEXT NOT NULL,
  result TEXT, summary TEXT);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions (started_at);
CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
  status TEXT NOT NULL, model_run INTEGER NOT NULL, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (item_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
  subject TEXT NOT NULL, subject_type TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
  why TEXT NOT NULL, next_step TEXT NOT NULL, departments TEXT NOT NULL,
  priority TEXT NOT NULL, claim_kind TEXT NOT NULL, gate TEXT NOT NULL, truth TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'expired', 'refused', 'duplicate')),
  reason TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, run_id INTEGER);
CREATE TABLE IF NOT EXISTS item_cites (item_id TEXT NOT NULL, grp INTEGER NOT NULL,
  handle TEXT NOT NULL, object_id TEXT, markings TEXT NOT NULL,
  PRIMARY KEY (item_id, grp, handle));
CREATE INDEX IF NOT EXISTS items_open ON items (state, last_seen);
CREATE TABLE IF NOT EXISTS message_keys (key TEXT PRIMARY KEY, object_id TEXT NOT NULL,
  at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS correspondents (key TEXT PRIMARY KEY, n INTEGER NOT NULL,
  at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learned (store TEXT NOT NULL, base_digest TEXT NOT NULL,
  at TEXT NOT NULL, PRIMARY KEY (store, base_digest));
CREATE TABLE IF NOT EXISTS item_states (item_id TEXT NOT NULL, reader TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('dismissed', 'snoozed')), until TEXT, reason TEXT,
  at TEXT NOT NULL, PRIMARY KEY (item_id, reader));
"""

STORE = "ai_watcher"
STEPS = ((1, "initial", (_DDL,)),)
