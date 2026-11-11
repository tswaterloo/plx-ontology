"""Declared schema of the mention extraction cache (``ontology.mention_cache``,
``projection/mention-extract.sqlite``; see ``operating.declared_schema``)."""

STORE = "mention_cache"
STEPS = ((1, "initial", (
    'CREATE TABLE IF NOT EXISTS extracted (key TEXT PRIMARY KEY, xcode TEXT NOT NULL, value BLOB NOT NULL, used INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS texts (key TEXT PRIMARY KEY, code TEXT NOT NULL, value BLOB NOT NULL, used INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS groups (key TEXT PRIMARY KEY, value BLOB NOT NULL, used INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
)),)
