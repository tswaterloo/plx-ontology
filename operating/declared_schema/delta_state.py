"""Declared schema of the delta state store (see ``operating.declared_schema``)."""

STORE = "delta_state"
STEPS = ((1, "initial", (
            "CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS changed (source TEXT NOT NULL, key TEXT NOT NULL, "
            "seen_at TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (source, key));"
            "CREATE TABLE IF NOT EXISTS context (source TEXT NOT NULL, key TEXT NOT NULL, "
            "PRIMARY KEY (source, key));"
            "CREATE TABLE IF NOT EXISTS owned (object_id TEXT PRIMARY KEY, source TEXT NOT NULL, "
            "key TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS slow (digest TEXT PRIMARY KEY, "
            "timeouts INTEGER NOT NULL);"
)),)
