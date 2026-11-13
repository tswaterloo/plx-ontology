"""Declared schema of the delta's held-files store (see ``operating.declared_schema``).

``<delta dir>/held.sqlite``: the files each held record waits on (``ontology.delta``),
one row per (record, file digest). A file of its own beside ``state.sqlite``: the state
store keeps its version, so code from before this store still opens it."""

STORE = "delta_held"
STEPS = ((1, "initial", (
            "CREATE TABLE IF NOT EXISTS files (source TEXT NOT NULL, key TEXT NOT NULL, "
            "digest TEXT NOT NULL, PRIMARY KEY (source, key, digest));"
            "CREATE INDEX IF NOT EXISTS files_digest ON files (digest);"
)),)
