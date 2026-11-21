"""Declared schema of the recurring instructions store (``operating.recurring_instructions``)."""

STORE = "recurring_instructions"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS recurring (id TEXT PRIMARY KEY, owner TEXT NOT NULL, text TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL, params TEXT NOT NULL, words TEXT NOT NULL, hour INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, last_run REAL, next_run REAL, last_result TEXT)",
)),)
