"""Independent evaluation oracle (L11).

Computes ground truth for question families F1-F4 directly from raw source reads
(Portal reporting API rows, BC documents) with joins re-derived from the Portal context
pack. It never imports ``ontology`` or ``operating``
(nor, inside this package, ``integrations``): transports are injected by the caller, so
the oracle cannot share a projection, resolver or case script with the agent it grades.
``scripts/check_oracle_imports.py`` enforces the ban.

Interface ``plx-eval-oracle/v1``: ``make_oracle(family, sources)(root, as_known_at)``
returns an ``oracle_result`` (``evals/item.schema.json``). ``generator.generate`` samples
closed transactions by run id into ``plx-eval-item/v1`` items.
"""

from .families import INTERFACE, ORACLES, Sources, make_oracle, run
from .source import BcReads, PortalReads

__all__ = ["BcReads", "INTERFACE", "ORACLES", "PortalReads", "Sources", "make_oracle", "run"]
