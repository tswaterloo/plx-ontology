"""Declared schema of the "whenever" fires store (``operating.recurring_events.Fires``)."""

STORE = "recurring_fires"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS fires (rid TEXT NOT NULL, owner TEXT NOT NULL, object_id TEXT NOT NULL, at REAL NOT NULL, taken INTEGER NOT NULL DEFAULT 0, UNIQUE (rid, object_id))",
)),)
