"""Where the Ask's reads run: the hot standby when it answers as a replica, else the primary.

A heavy read is protected against by where it runs, never by a limit.
``plx.sql``, ``plx.search`` and ``plx.open`` read through this module's choice
(``operating.ontology_tools_sql._lent``). There is no lag threshold that refuses or cuts:
the standby is used whenever it is a reachable replica, and each result says whether it came
from the standby and as of when (the replay time of the very connection that read it); when
the standby cannot be used the primary is read and the result says so and why.

``PLX_WARM_STANDBY_DSN`` is the one setting naming the standby (the warm-up's, the Ask's):
``operator_api.standby_reads`` imports it from here."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Mapping, Optional

LOG = logging.getLogger("operating.standby_route")
DSN_ENV = "PLX_WARM_STANDBY_DSN"
# how long a probe's answer is trusted (a failed one: the primary is read for this long,
# then the standby is tried again)
PROBE_SECONDS = 10.0
_LOCK = threading.Lock()
_PROBED: dict[str, tuple[float, Optional[str]]] = {}   # standby dsn -> (at, reason it is not used)


def standby_dsn(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    value = str((os.environ if environ is None else environ).get(DSN_ENV, "")).strip()
    return value or None


def _probe(dsn: str) -> Optional[str]:
    """None when ``dsn`` answers as a replica, else why not (said in the result)."""
    import psycopg
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as conn:
            (recovering,) = conn.execute("SELECT pg_is_in_recovery()").fetchone()
        return None if recovering else "the standby is not in recovery (promoted)"
    except Exception as exc:  # noqa: BLE001 - said in the result, logged by type
        LOG.warning("standby unreachable: %s", type(exc).__name__)
        return f"the standby could not be reached ({type(exc).__name__})"


def route(primary: str, environ: Optional[Mapping[str, str]] = None,
          probe: Any = None, clock: Any = time.monotonic) -> tuple[str, Optional[str]]:
    """(the DSN to read, why it is the primary): the standby with None when it answers as a
    replica; else ``primary`` with the reason (no standby set, unreachable, promoted)."""
    standby = standby_dsn(environ)
    if not standby or standby == primary:
        return primary, "no standby is set for the Ask's reads"
    now = clock()
    with _LOCK:
        held = _PROBED.get(standby)
    if held is None or now - held[0] > PROBE_SECONDS:
        held = (now, (probe or _probe)(standby))
        with _LOCK:
            _PROBED[standby] = held
    return (standby, None) if held[1] is None else (primary, held[1])


def failed(standby: str, why: str, clock: Any = time.monotonic) -> None:
    """A read on the standby could not even start (its connection failed): the primary is
    used until the next probe."""
    with _LOCK:
        _PROBED[standby] = (clock(), f"the standby could not be reached ({why})")


def reset() -> None:
    with _LOCK:
        _PROBED.clear()
