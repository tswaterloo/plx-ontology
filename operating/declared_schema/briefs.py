"""Declared schema of the briefs store (see ``operating.declared_schema``)."""

STORE = "briefs"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS weeks (reader TEXT NOT NULL, week TEXT NOT "
                     "NULL, grant_key TEXT NOT NULL, stored_at TEXT NOT NULL, body TEXT NOT "
                     "NULL, PRIMARY KEY (reader, week))",
)),)
