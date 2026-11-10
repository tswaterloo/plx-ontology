"""Declared schema of the page_store store (see ``operating.declared_schema``)."""

STORE = "page_store"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, "
                         "read_at REAL NOT NULL, expires REAL NOT NULL, value BLOB NOT NULL)",
)),)
