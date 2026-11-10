"""Declared schema of the kept_members store (see ``operating.declared_schema``)."""

STORE = "kept_members"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS members (key TEXT PRIMARY KEY, "
                             "code TEXT NOT NULL, value BLOB NOT NULL, used INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, "
                             "value TEXT NOT NULL)",
)),)
