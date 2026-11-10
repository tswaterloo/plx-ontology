"""Declared schema of the goal2_exec_owner store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS owner_record (
  relation TEXT NOT NULL, record_key TEXT NOT NULL, row_json TEXT NOT NULL,
  PRIMARY KEY (relation, record_key));
CREATE TABLE IF NOT EXISTS owner_operation (
  idempotency_key TEXT PRIMARY KEY,
  payload_sha256 TEXT, payload_json TEXT,
  relation TEXT, record_key TEXT,
  status TEXT NOT NULL CHECK(status IN ('applied','pending','rejected','fenced','partial')),
  receipt TEXT, reason TEXT, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS owner_relation (
  relation TEXT PRIMARY KEY, fields_json TEXT NOT NULL, version_field TEXT NOT NULL);
"""

STORE = "goal2_exec_owner"
STEPS = ((1, "initial", (_DDL,)),)
