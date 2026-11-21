"""The BC capture units a projection store holds (``store_bc_units``), read by index
ranges of the BC capture handles; split out of ``ontology_host`` (module size cap)."""
from __future__ import annotations

import re
from typing import Any

from .ontology_tools import StoreReader


# The versions whose handle starts ``bc://`` in any letter case (what ``LIKE 'bc://%'``
# matches), as index ranges: ``[p, p[:-1] + '0')`` holds exactly the strings starting with
# ``p`` (``0`` follows ``/``). A ``LIKE`` read every version's handle in the index
# (11.6 million on generation 7, ~40 s at every host start); the ranges read the BC ones.
_BC_HANDLE_RANGES = " OR ".join(
    f"(v.l1_handle >= '{p}' AND v.l1_handle < '{p[:-1]}0')"
    for p in ("bc://", "bC://", "Bc://", "BC://"))


# a BC unit key as ``ontology.project_bc`` makes it: bc:<company>/<entitySet>
_UNIT = re.compile(r"bc:[^/:]+/[^/:]+")


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
    if getattr(reader.conn, "postgres", False):
        # a key named as the BC projection names it (bc:<company>/<entitySet>) by the
        # handle index's prefixes; any other key by the grouped read below
        units.update(_pg_bc_units(reader, [k for k in keys if _UNIT.fullmatch(k)]))
        # every key the prefixes did not find (another shape, or BC versions only under
        # another unit's prefix) is read as before: the same map as the grouped read
        keys = [k for k in keys if k not in units]
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


# Postgres: the distinct ``bc://<environment>/<company>/<entitySet>/`` prefixes of the
# versions' handles by a skip scan of the handle index (one probe a prefix: ~70 live, ~15 ms),
# where the grouped read above read every BC version (2.3 s at every host start, 11-21).
_PG_PREFIXES = """WITH RECURSIVE p(h) AS (
  SELECT (SELECT min(l1_handle) FROM object_versions WHERE l1_handle >= ? AND l1_handle < ?)
  UNION ALL
  SELECT (SELECT min(l1_handle) FROM object_versions
          WHERE l1_handle >= split_part(p.h, '/', 1) || '//' || split_part(p.h, '/', 3) || '/'
                || split_part(p.h, '/', 4) || '/' || split_part(p.h, '/', 5) || '0'
            AND l1_handle < ?)
  FROM p WHERE p.h IS NOT NULL)
SELECT h FROM p WHERE h IS NOT NULL"""
# a prefix counts for its unit only when a version under it is of an object holding that
# very audience key (as the grouped read joins them); the least such handle, read in index
# order up to the first match
_PG_FIRST = ("SELECT v.l1_handle FROM object_versions v JOIN objects o ON o.object_id = "
             "v.object_id WHERE v.l1_handle >= ? AND v.l1_handle < ? AND o.audience_key = ? "
             "ORDER BY v.l1_handle LIMIT 1")


def _pg_bc_units(reader: StoreReader, keys: list[str]) -> dict[str, str]:
    """Unit -> the environment of its least BC handle under its own
    ``bc://<env>/<company>/<entitySet>/`` prefixes, for each key whose prefixes hold a
    version of an object with that key. A unit with BC versions outside its own prefixes
    would get its environment from those alone; a key found here is so only when it has
    versions under its own prefix, so ``store_bc_units`` reads every key not found here by
    the grouped read. Entity sets and environments are taken to be letters and digits (the
    skip past a prefix steps over names that extend it with a character below ``0``)."""
    wanted = set(keys)
    found: dict[str, tuple[str, str]] = {}
    with reader.lock:
        for scheme in ("bc://", "bC://", "Bc://", "BC://"):
            hi = scheme[:-1] + "0"
            for (handle,) in reader.conn.execute(_PG_PREFIXES, (scheme, hi, hi)).fetchall():
                parts = str(handle).split("/")
                if len(parts) < 6:
                    continue
                key = f"bc:{parts[3]}/{parts[4]}"
                if key not in wanted:
                    continue
                prefix = "/".join(parts[:5]) + "/"
                first = reader.conn.execute(_PG_FIRST, (prefix, prefix[:-1] + "0",
                                                        key)).fetchone()
                if first is not None and (key not in found or first[0] < found[key][0]):
                    found[key] = (first[0], parts[2])
    return {key: env for key, (_least, env) in found.items()}


class ListingAsked:
    """A BC lookup as ``case_host_policy`` uses it, noting whether the decision used it
    (any attribute but ``reader`` counts as the company listing asked)."""

    def __init__(self, lookup: Any) -> None:
        self._lookup, self.asked = lookup, False

    @property
    def reader(self) -> Any:
        return self._lookup.reader

    def __getattr__(self, name: str) -> Any:
        self.asked = True
        return getattr(self._lookup, name)
