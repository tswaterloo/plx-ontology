"""Declared schema of the session_journal store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS request (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, session_ref TEXT, employee TEXT,
  method TEXT NOT NULL, route TEXT NOT NULL, status INTEGER NOT NULL, refusal TEXT,
  object_type TEXT, panels TEXT, proof TEXT, access TEXT)
"""

STORE = "session_journal"
STEPS = ((1, "initial", (_DDL,)),)
