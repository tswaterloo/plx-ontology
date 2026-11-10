"""Declared schema of the confirmation_forms store (see ``operating.declared_schema``)."""

STORE = "confirmation_forms"
STEPS = ((1, "initial", (
    """CREATE TABLE IF NOT EXISTS confirmation_form (
                token_sha256 TEXT PRIMARY KEY, purpose TEXT NOT NULL, tenant TEXT NOT NULL,
                subject TEXT NOT NULL, requested_by TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
                outcome TEXT)""",
)),)
