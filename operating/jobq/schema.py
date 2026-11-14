"""The job queue database's schema: ordered SQL migrations in ``operating/jobq/migrations``,
applied by the Postgres migration runner (``operating.pg.migrate``) and recorded in
``jobq.migrations`` with each file's sha256, so a later change is a new migration and an
edited applied one is refused as drift. Every step is ``IF NOT EXISTS``: a database the
earlier inline script made is recorded as current without a change."""
from __future__ import annotations

from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
LEDGER = "jobq"
LOCK_KEY = 0x504C58_4A51  # "PLXJQ": one schema apply at a time per queue database


def apply(conn: Any) -> list[str]:
    """Apply the pending migrations on an idle connection; return the names applied."""
    from operating.pg import migrate
    return migrate.migrate(conn, values={}, directory=MIGRATIONS_DIR, ledger=LEDGER,
                           lock_key=LOCK_KEY)


def status(conn: Any) -> dict[str, Any]:
    from operating.pg import migrate
    return migrate.status(conn, MIGRATIONS_DIR, ledger=LEDGER)
