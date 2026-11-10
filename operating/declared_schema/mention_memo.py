"""Declared schema of the mention memo store (see ``operating.declared_schema``)."""

STORE = "mention_memo"
STEPS = ((1, "initial", (
    'CREATE TABLE IF NOT EXISTS mentions (key TEXT PRIMARY KEY, code TEXT NOT NULL, valid_at TEXT, value BLOB NOT NULL, used INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS texts (key TEXT PRIMARY KEY, code TEXT NOT NULL, value BLOB NOT NULL, used INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS groups (key TEXT PRIMARY KEY, value BLOB NOT NULL, used INTEGER NOT NULL)',
)),)
