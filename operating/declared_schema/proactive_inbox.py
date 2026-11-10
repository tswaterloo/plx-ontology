"""Declared schema of the proactive_inbox store (see ``operating.declared_schema``)."""

_INBOX_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
  as_of TEXT NOT NULL, store_digest TEXT, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scope (audience_key TEXT NOT NULL, markings TEXT NOT NULL,
  objects INTEGER NOT NULL, PRIMARY KEY (audience_key, markings));
CREATE TABLE IF NOT EXISTS detector_runs (detector TEXT PRIMARY KEY, status TEXT NOT NULL,
  reasons TEXT NOT NULL, found INTEGER NOT NULL, not_evaluated TEXT NOT NULL,
  run_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rule_coverage (detector TEXT PRIMARY KEY,
  unattributed INTEGER NOT NULL, run_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unjudged_causes (detector TEXT NOT NULL, skip INTEGER NOT NULL,
  object_id TEXT NOT NULL, markings TEXT NOT NULL, subject INTEGER NOT NULL,
  PRIMARY KEY (detector, skip, object_id, markings));
CREATE TABLE IF NOT EXISTS unjudged_reasons (detector TEXT NOT NULL, skip INTEGER NOT NULL,
  reason TEXT NOT NULL, PRIMARY KEY (detector, skip));
CREATE TABLE IF NOT EXISTS gaps (gap_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  detector TEXT NOT NULL, object_id TEXT NOT NULL, object_type TEXT NOT NULL,
  summary TEXT NOT NULL, follow_up TEXT NOT NULL, priority INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'resolved')), first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, resolved_at TEXT);
CREATE INDEX IF NOT EXISTS gaps_open ON gaps (state, priority, last_seen);
CREATE TABLE IF NOT EXISTS gap_cites (gap_id TEXT NOT NULL, grp INTEGER NOT NULL,
  handle TEXT NOT NULL, object_id TEXT, markings TEXT NOT NULL,
  PRIMARY KEY (gap_id, grp, handle));
CREATE TABLE IF NOT EXISTS gap_requires (gap_id TEXT NOT NULL, audience_key TEXT NOT NULL,
  PRIMARY KEY (gap_id, audience_key));
CREATE TABLE IF NOT EXISTS acks (gap_id TEXT NOT NULL, reader TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (gap_id, reader));
CREATE TABLE IF NOT EXISTS visits (reader TEXT PRIMARY KEY, at TEXT NOT NULL, previous TEXT);
CREATE TABLE IF NOT EXISTS event_cache (detector TEXT NOT NULL, root TEXT NOT NULL,
  reading TEXT NOT NULL, PRIMARY KEY (detector, root));
CREATE TABLE IF NOT EXISTS detector_history (detector TEXT NOT NULL, run_at TEXT NOT NULL,
  status TEXT NOT NULL, found INTEGER NOT NULL, PRIMARY KEY (detector, run_at));
CREATE TABLE IF NOT EXISTS kind_history (kind TEXT NOT NULL, run_at TEXT NOT NULL,
  found INTEGER NOT NULL, PRIMARY KEY (kind, run_at));
"""

STORE = "proactive_inbox"
STEPS = ((1, "initial", (_INBOX_DDL,)),)


# Created when a reader first marks its briefing seen (``record_visit``), not at open.
_VISITS_DDL = ("CREATE TABLE IF NOT EXISTS visits (reader TEXT PRIMARY KEY, at TEXT NOT NULL, "
               "previous TEXT)")
