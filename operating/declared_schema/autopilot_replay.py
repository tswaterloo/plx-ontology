"""Declared schema of the autopilot_replay store (see ``operating.declared_schema``)."""

STORE = "autopilot_replay"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, "
                          "finished_at TEXT, summary TEXT)",
)),)
