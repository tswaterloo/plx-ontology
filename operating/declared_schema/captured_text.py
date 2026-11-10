"""Declared schema of the captured_text store (see ``operating.declared_schema``)."""

STORE = "captured_text"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS readings (key TEXT PRIMARY KEY, "
                             "code TEXT NOT NULL, value BLOB NOT NULL, used INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, "
                             "value TEXT NOT NULL)",
)),)
