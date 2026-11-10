"""Declared schema of the action_path store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS action_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id TEXT NOT NULL,
  intent_seq INTEGER NOT NULL CHECK(intent_seq >= 1),
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL UNIQUE,
  UNIQUE(intent_id, intent_seq)
);
CREATE TRIGGER IF NOT EXISTS action_event_no_update BEFORE UPDATE ON action_event
BEGIN SELECT RAISE(ABORT, 'action_event_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS action_event_no_delete BEFORE DELETE ON action_event
BEGIN SELECT RAISE(ABORT, 'action_event_is_append_only'); END;
CREATE TABLE IF NOT EXISTS consumed_call_approval (
  approval_sha256 TEXT PRIMARY KEY,
  intent_id TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS consumed_call_approval_no_update BEFORE UPDATE
ON consumed_call_approval BEGIN SELECT RAISE(ABORT, 'consumed_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS consumed_call_approval_no_delete BEFORE DELETE
ON consumed_call_approval BEGIN SELECT RAISE(ABORT, 'consumed_is_append_only'); END;
CREATE TABLE IF NOT EXISTS internal_action (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('started', 'executed', 'refused')),
  case_id TEXT NOT NULL,
  action_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  identity_json TEXT NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS internal_action_no_update BEFORE UPDATE ON internal_action
BEGIN SELECT RAISE(ABORT, 'internal_action_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS internal_action_no_delete BEFORE DELETE ON internal_action
BEGIN SELECT RAISE(ABORT, 'internal_action_is_append_only'); END;
CREATE TABLE IF NOT EXISTS case_author (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('case.opened', 'proposal.recorded')),
  ref TEXT NOT NULL,
  original INTEGER NOT NULL CHECK(original IN (0, 1)),
  case_seq INTEGER,
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  identity_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_case_author_original ON case_author(case_id, kind, ref)
WHERE original = 1;
CREATE INDEX IF NOT EXISTS idx_case_author ON case_author(case_id, kind, ref);
CREATE TRIGGER IF NOT EXISTS case_author_no_update BEFORE UPDATE ON case_author
BEGIN SELECT RAISE(ABORT, 'case_author_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS case_author_no_delete BEFORE DELETE ON case_author
BEGIN SELECT RAISE(ABORT, 'case_author_is_append_only'); END;
CREATE TABLE IF NOT EXISTS case_request (
  actor TEXT NOT NULL,
  request_key TEXT NOT NULL,
  case_id TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY(actor, request_key)
);
CREATE TRIGGER IF NOT EXISTS case_request_no_update BEFORE UPDATE ON case_request
BEGIN SELECT RAISE(ABORT, 'case_request_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS case_request_no_delete BEFORE DELETE ON case_request
BEGIN SELECT RAISE(ABORT, 'case_request_is_append_only'); END;
"""

STORE = "action_path"
STEPS = ((1, "initial", (_DDL,)),)
