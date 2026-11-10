"""Declared schema of the document_fields store (see ``operating.declared_schema``)."""

STORE = "document_fields"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS fields (key TEXT PRIMARY KEY, "
                             "code TEXT NOT NULL, value TEXT NOT NULL, used INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, "
                             "value TEXT NOT NULL)",
)),)
