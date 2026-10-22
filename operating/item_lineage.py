"""Item lineage: an item's records in other systems, what uses and buys it, and the screening
job that turns it into another item, each as a recorded link or a typed gap.

The engine of the ``plx.investigate`` plan ``ontology/plans/item_lineage.json`` (family F13,
evidence kind ``compute``). It is the commercial chain engine (``operating.commercial_chain``,
whose module doc gives ``engine_args``) under its own name: the plan walks the item links the
schema binds (an item's twins in the Portal, FileMaker and Business Central, the finished
products and assembly orders that use it, its supplier offers, purchase, receipt and ledger
lines, an FG item's minted, reference and adopted products), and every link the screening and
procure-to-G/L questions need that no source the Workbench reads records (artwork version to
item, the item it replaced, FG revision, the screening job, its decision and good-unit count,
the conversion step, transfers to the screener, G/L posting lineage, the cost policy) is a
typed gap with the question for its owner. No link is inferred from a shared code prefix,
supplier, date or quantity, and no cost per good unit is computed without a recorded count.
"""

from __future__ import annotations

from .commercial_chain import plan_check, plan_run

ENGINE = "item_lineage"

PLAN_ENGINES = {ENGINE: {"check": plan_check, "run": plan_run}}

__all__ = ["ENGINE", "PLAN_ENGINES"]
