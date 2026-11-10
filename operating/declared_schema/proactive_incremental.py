"""Declared schema of the proactive_incremental store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prints (object_id TEXT PRIMARY KEY, print TEXT NOT NULL,
  neighbors TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS layers (layer TEXT PRIMARY KEY, base_digest TEXT NOT NULL,
  seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS passes (pass_id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, summary TEXT NOT NULL);
"""

STORE = "proactive_incremental"
STEPS = ((1, "initial", (_DDL,)),)
