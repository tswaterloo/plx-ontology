"""Declared schema of the reply drafts store (see ``operating.declared_schema``)."""

_DDL = """CREATE TABLE IF NOT EXISTS drafts (
  owner TEXT NOT NULL, object_id TEXT NOT NULL, state TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
  ask TEXT NOT NULL DEFAULT '', options TEXT NOT NULL DEFAULT '[]',
  why TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, seconds REAL,
  PRIMARY KEY (owner, object_id))"""

STORE = "reply_drafts"
STEPS = ((1, "initial", (_DDL,)),)
