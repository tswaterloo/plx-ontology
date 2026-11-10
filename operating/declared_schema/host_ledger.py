"""Declared schema of the host_ledger store (see ``operating.declared_schema``)."""

_LEDGER_DDL = (
    """CREATE TABLE IF NOT EXISTS source_state (
  source TEXT PRIMARY KEY, configured INTEGER NOT NULL, generation INTEGER NOT NULL,
  captured_at TEXT, served_from TEXT NOT NULL, complete INTEGER, attempted_at TEXT,
  stale_since TEXT, error TEXT, served_path TEXT)""",
    """CREATE TABLE IF NOT EXISTS cycle (
  cycle_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  state TEXT NOT NULL, report TEXT)""",
    """CREATE TABLE IF NOT EXISTS projection (
  generation INTEGER PRIMARY KEY, built_at TEXT NOT NULL, previous_generation INTEGER,
  content_digest TEXT, objects INTEGER, inputs TEXT NOT NULL)""",
    # The parts the source's latest read was refused or not answered on (``blocked_parts``:
    # codes and statuses only); no row while it answered.
    """CREATE TABLE IF NOT EXISTS source_blocked (
  source TEXT PRIMARY KEY, parts TEXT NOT NULL)""",
)

STORE = "host_ledger"
STEPS = ((1, "initial", (_LEDGER_DDL,)),)
