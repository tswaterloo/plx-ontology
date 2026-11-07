"""One host CPU budget for the capture children's text extraction and OCR.

The ingest host runs every source's capture CLI at once. Two of them extract text on
worker pools that fork poppler and tesseract (mail: ``extract_workers`` and
``ocr_workers``; SharePoint: ``EXTRACT_WORKERS`` threads), each sized for an idle host on
its own, so together they ran 20 workers beside Postgres, the delta and the API on 16
cores (audit 2026-11-06: load 30-90, CPU pressure 62%). More workers than cores adds
context switches, not throughput.

The host (``operating.ingest_forever.child_env``) hands each extracting child its share
of one budget in :data:`BUDGET_ENV`: the configured ``ingest.cpu_budget`` (cores), else
:func:`host_budget` read as the child starts (every core but two on a quiet host, half
the cores when the 1-minute load is past the core count). A child caps its pools at its
share (:func:`cap`); a child started by hand, without the variable, keeps its own sizing.
OMP_THREAD_LIMIT=1 for tesseract is set where it runs
(``integrations.document_understanding._tool_env``).
"""

from __future__ import annotations

import os
from typing import Mapping

BUDGET_ENV = "PLX_INGEST_CPU_BUDGET"
# The whole budget the host was configured with (``ingest.cpu_budget``), as the host
# hands it to the runner that starts the children.
TOTAL_ENV = "PLX_INGEST_CPU_TOTAL"
# The capture children that extract text (their shares split the budget).
EXTRACTING = ("mail", "sharepoint")
# A host is busy when its 1-minute load is past this many runnable tasks per core.
BUSY_LOAD_PER_CORE = 1.0
MAX_BUDGET = 256


def host_budget(cpus: int | None = None, load: float | None = None) -> int:
    """Cores the ingest's text extraction may use now: every core but two, or half the
    cores when the host is busy (8 on a 16-core host)."""
    cpus = cpus or os.cpu_count() or 2
    if load is None:
        try:
            load = os.getloadavg()[0]
        except (OSError, AttributeError):
            load = float(cpus)   # unknown: as if busy
    if load >= cpus * BUSY_LOAD_PER_CORE:
        return max(1, cpus // 2)
    return max(1, cpus - 2)


def share(total: int, children: int = len(EXTRACTING)) -> int:
    """One extracting child's share of ``total`` cores."""
    return max(1, total // max(1, children))


def child_share(environ: Mapping[str, str]) -> int:
    """An extracting child's share as its runner starts it: of the configured total
    (:data:`TOTAL_ENV`), else of :func:`host_budget` now."""
    total = from_env(environ, TOTAL_ENV)
    return share(total if total is not None else host_budget())


def from_env(environ: Mapping[str, str] | None = None, name: str = BUDGET_ENV
             ) -> int | None:
    """The share this child was given (None: none, it sizes its own pools). A value
    that is not a whole number of cores from 1 to :data:`MAX_BUDGET` reads as one core:
    a malformed budget never widens the pools."""
    environ = os.environ if environ is None else environ
    value = environ.get(name)
    if value is None:
        return None
    try:
        number = int(value.strip())
    except (ValueError, AttributeError):
        return 1
    return number if 1 <= number <= MAX_BUDGET else 1


def cap(workers: int, environ: Mapping[str, str] | None = None, *, part: float = 1.0
        ) -> int:
    """``workers``, at most ``part`` of this child's share (at least one)."""
    budget = from_env(environ)
    if budget is None:
        return workers
    return max(1, min(workers, int(budget * part) or 1))
