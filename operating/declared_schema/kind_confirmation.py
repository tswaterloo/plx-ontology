"""Declared schema of the kind_confirmation store (see ``operating.declared_schema``)."""

STORE = "kind_confirmation"
STEPS = ((1, "initial", (
    """CREATE TABLE IF NOT EXISTS confirmation (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                tenant TEXT NOT NULL, original_id TEXT NOT NULL, drive_id TEXT,
                item_id TEXT, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
                supersedes TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_confirmation_item
                ON confirmation(tenant, drive_id, item_id)""",
)),)
