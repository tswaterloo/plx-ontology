"""Declared schema of the capture_store store (see ``operating.declared_schema``)."""

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, unit TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('in_progress','complete','partial','failed')),
        started_at TEXT NOT NULL, finished_at TEXT, updated_at TEXT NOT NULL,
        resources TEXT NOT NULL, cursor TEXT, pages INTEGER NOT NULL DEFAULT 0, records INTEGER NOT NULL DEFAULT 0,
        new_versions INTEGER NOT NULL DEFAULT 0, withheld INTEGER NOT NULL DEFAULT 0,
        dropped INTEGER NOT NULL DEFAULT 0, restarts INTEGER NOT NULL DEFAULT 0,
        reason TEXT, error TEXT, retry_not_before TEXT, detail TEXT)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS runs_open ON runs(kind, unit) "
    "WHERE state = 'in_progress'",
    "CREATE INDEX IF NOT EXISTS runs_by_unit ON runs(kind, unit, run_id)",
    """CREATE TABLE IF NOT EXISTS versions (
        capture_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, path TEXT NOT NULL, unit TEXT NOT NULL,
        source_version TEXT NOT NULL, digest TEXT NOT NULL, lineage TEXT NOT NULL,
        payload TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES runs(run_id),
        recorded_at TEXT NOT NULL, superseded_at TEXT, superseded_by INTEGER,
        last_seen_run INTEGER NOT NULL, last_seen_at TEXT NOT NULL,
        missing_from_source_since TEXT,
        CHECK (superseded_at IS NULL OR superseded_at >= recorded_at))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS versions_current ON versions(kind, path) "
    "WHERE superseded_at IS NULL",
    "CREATE INDEX IF NOT EXISTS versions_by_path ON versions(kind, path, capture_id)",
    "CREATE INDEX IF NOT EXISTS versions_by_unit ON versions(kind, unit, path)",
    # Steps a run finished that are not pages (e.g. the header versions whose lines a
    # document-lines run read), committed with that step's records.
    """CREATE TABLE IF NOT EXISTS run_marks (
        run_id INTEGER NOT NULL REFERENCES runs(run_id), mark TEXT NOT NULL,
        PRIMARY KEY (run_id, mark))""",
    # Superseded versions dropped by the retention bound (their handles report it).
    """CREATE TABLE IF NOT EXISTS pruned (
        capture_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
        unit TEXT NOT NULL, pruned_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS pruned_by_unit ON pruned(kind, unit)",
    # Closed stretches a record was missing from its source (the open one is its current
    # version's missing_from_source_since), kept when the record is seen again.
    """CREATE TABLE IF NOT EXISTS removals (
        kind TEXT NOT NULL, path TEXT NOT NULL, since TEXT NOT NULL, until TEXT NOT NULL,
        CHECK (until >= since))""",
    "CREATE INDEX IF NOT EXISTS removals_by_path ON removals(kind, path)",
    # The latest source ACL of a unit whose kind has ``unit_acl`` (gates all its versions).
    """CREATE TABLE IF NOT EXISTS unit_acl (
        kind TEXT NOT NULL, unit TEXT NOT NULL, acl TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY (kind, unit))""",
)

STORE = "capture_store"
STEPS = ((1, "initial", (_SCHEMA,)),)
