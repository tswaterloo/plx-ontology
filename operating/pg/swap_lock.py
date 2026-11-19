"""The table swap's lock-taking steps, run so a reader never waits on them for long.

A swap renames a built table over the served one (and drops and re-makes the guarded views
over it): ``ALTER TABLE ... RENAME`` and ``DROP VIEW`` need an exclusive lock, and while one
waits behind a long reader (a ``plx.sql`` cursor) every NEW reader queues behind it; once
it has the lock, every statement the swap runs before it commits keeps the readers out
too. Sampled live, readers waited 4-10 s behind the rename and then 4-12 s behind the
lineage insert and the exclusion scan that followed it in the same transaction.

So: the swap's heavy work (lineage, counts, exclusion, state) is done first, none of it
locking a served table, and ``swap_in`` runs the lock-taking statements in a savepoint
under a short ``lock_timeout``, trying again (a pause between tries) until the readers
let go. A reader waits at most ``LOCK_MS`` behind a try."""
from __future__ import annotations

import random
import time
from typing import Any, Callable

LOCK_MS = 400          # a try's longest lock wait: what a new reader can queue behind it
PAUSE_SECONDS = (0.2, 0.8)
GIVE_UP_SECONDS = 900  # then the swap fails (the built table is dropped; the next refresh builds again)


def swap_in(write: Any, steps: Callable[[], None], *, give_up: float = GIVE_UP_SECONDS,
            lock_ms: int = LOCK_MS, sleep: Callable[[float], None] = time.sleep) -> int:
    """Run ``steps()`` (the lock-taking statements, nothing heavy) inside the caller's open
    transaction as a savepoint under ``lock_timeout``; a lock it could not get in time or a
    deadlock rolls the savepoint back and tries again. Returns the number of tries. It must
    be the last thing the transaction does (the lock timeout stays set until it ends)."""
    import psycopg
    deadline = time.monotonic() + give_up
    tries = 0
    while True:
        tries += 1
        try:
            with write.transaction():
                write.execute(f"SET LOCAL lock_timeout = {int(lock_ms)}")
                steps()
            return tries
        except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected):
            if time.monotonic() >= deadline:
                raise
            sleep(random.uniform(*PAUSE_SECONDS))
