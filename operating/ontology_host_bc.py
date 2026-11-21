"""The BC capture units a projection store holds (``store_bc_units``), read by index
ranges of the BC capture handles; split out of ``ontology_host`` (module size cap)."""
from __future__ import annotations

from typing import Any

from .ontology_tools import StoreReader


# The versions whose handle starts ``bc://`` in any letter case (what ``LIKE 'bc://%'``
# matches), as index ranges: ``[p, p[:-1] + '0')`` holds exactly the strings starting with
# ``p`` (``0`` follows ``/``). A ``LIKE`` read every version's handle in the index
# (11.6 million on generation 7, ~40 s at every host start); the ranges read the BC ones.
_BC_HANDLE_RANGES = " OR ".join(
    f"(v.l1_handle >= '{p}' AND v.l1_handle < '{p[:-1]}0')"
    for p in ("bc://", "bC://", "Bc://", "BC://"))


def store_bc_units(reader: StoreReader) -> dict[str, str]:
    """BC capture units a store holds (``bc:<company>/<entitySet>``) -> the BC
    environment their capture handles name (``bc://<environment>/...``)."""
    units: dict[str, str] = {}
    # The BC audience keys from the kept key list (read by its index), named whole: a
    # ``LIKE 'bc:%'`` on the key read every object (Postgres: ILIKE, no index; 15-48 s
    # at every host start under load, 11-13).
    keys = [key for key in reader.audience_keys() if key[:3].lower() == "bc:"]
    if not keys:
        return units
    rows: list[Any] = []
    with reader.lock:
        for start in range(0, len(keys), 500):   # within any SQLite variable limit
            part = keys[start:start + 500]
            rows += reader.conn.execute(
                "SELECT o.audience_key, MIN(v.l1_handle) FROM object_versions v JOIN "
                "objects o ON o.object_id = v.object_id WHERE o.audience_key IN ("
                + ",".join("?" * len(part)) + f") AND ({_BC_HANDLE_RANGES}) "
                "GROUP BY o.audience_key", part).fetchall()
    for key, handle in rows:
        units[key] = handle[len("bc://"):].split("/", 1)[0]
    return units
