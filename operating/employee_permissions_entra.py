"""Entra directory provider for the per-employee resolver, with an expiring cache.

``EmployeePermissionResolver(..., directory=EntraDirectoryProvider(reader))``
makes the Entra directory a hard precondition of every employee entitlement
(``operating.employee_permissions``): the user must be this enabled object and
its transitive membership must have been read completely (or, while the
membership read is refused for consent, derived from the user's joined Teams),
through the GET-only ``integrations.graph_directory`` reader. The resolver runs
on every case read (``EmployeeActorGate``), so the reads are cached per employee:

* Only a complete ``read`` is cached, for ``ttl_seconds`` from when it was
  read. At expiry the next resolve reads the directory again, so a revoked
  membership, a disabled account or a deleted user takes effect then, never
  later. A clock that runs backwards also expires the entry. A consent granted
  meanwhile is used the same way: the next read after expiry is transitive.
* A failed read is never cached: it denies now and is retried on the next
  resolve (a failure cannot be remembered as a grant, and an outage does not
  pin a denial). A read whose groups could be neither read nor derived
  (``group_basis`` ``none``) is not cached either.
* The cache is bounded (oldest entry evicted) and lives in memory only.

Evidence: injected-transport tests; live-unverified
(verify/live-checks/graph.directory_entitlements.json).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from integrations.graph_directory import GraphDirectoryReader

from .employee_permissions import GROUP_BASIS_NONE, EmployeePrincipal, EntraDirectoryRead

DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 3600


class EntraDirectoryProvider:
    """``DirectoryProvider``: ``EmployeePrincipal -> EntraDirectoryRead``, cached until expiry."""

    def __init__(self, reader: GraphDirectoryReader, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_entries: int = 1024, clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(reader, GraphDirectoryReader):
            raise TypeError("GraphDirectoryReader required")
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be 1..{MAX_TTL_SECONDS}")
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self._reader = reader
        self.ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, EntraDirectoryRead]] = {}

    def __call__(self, principal: EmployeePrincipal) -> EntraDirectoryRead:
        if not isinstance(principal, EmployeePrincipal) or principal.kind != "employee":
            raise TypeError("employee principal required")
        key = (principal.tenant_id, principal.object_id)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                read_at, read = cached
                if read_at <= now < read_at + self.ttl_seconds:
                    return read
                del self._cache[key]
        read = self._reader.read(principal.object_id)
        if (read.status == "read" and read.object_id == principal.object_id
                and read.group_basis != GROUP_BASIS_NONE):
            with self._lock:
                self._cache[key] = (now, read)
                while len(self._cache) > self._max_entries:
                    del self._cache[next(iter(self._cache))]
        return read


__all__ = ["DEFAULT_TTL_SECONDS", "EntraDirectoryProvider", "MAX_TTL_SECONDS"]
