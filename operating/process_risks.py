"""Schedule risks of ``plx.process`` (RSN-01, risks part): a prerequisite that is due to
be met after the stage waiting on it is due.

A stage's prerequisite may declare a ``schedule`` (``ontology/schema/process``): the stage
of its own graph whose ``due`` date the prerequisite is needed by (``needed_by``: an
assembly order's materials by its planned filling start) and the stage of another graph
whose ``due`` date says when a prerequisite record is expected to meet it
(``available_by``: a purchase order's planned arrival, its requested-by date: FM
datearrive, Sage exparrival, Portal ``expected_date`` and the BC header
``requestedReceiptDate``; a BC line's ``expectedReceiptDate`` follows the supplier's
promise and is never read in its place,
plx-customer-portal:portal/prisma/schema.prisma@5cb95e1 PurchaseOrder.expectedDate :3513,
ManufacturingOrder.scheduledStartDate :2588; BC API v2.0 purchaseOrderLine
.expectedReceiptDate and purchaseOrder.requestedReceiptDate, both Edm.Date, as
OntologyProject:ontology/schema/generated/meta/bc_metadata.json@6e58f98 pins them and
``integrations/live_source_grammar.py`` BC_QUERY_SPECS reads them). Both dates are read
as ``ProcessTools._due_date`` reads a due date: declared planned date columns only, one
proven date or a gap, never a computed, default or chosen date.

For each blocker entry that names a record (``ProcessTools._blockers``: a live record
not proven to have reached its prerequisite stage, at any level, ``via`` saying which
record waits on it), the record's ``available_by`` date is compared with the waiting
record's ``needed_by`` date. Later is a ``schedule_risk`` citing both dates; on time or
the same day is none; a side without one proven date (undated, withheld, unproven,
disagreeing, contradicted by or unattested against a captured document, or read from a
staging record, which is never authority) is a ``schedule_undecided`` gap naming that
side and its cause (``CAUSES``: undated, withheld, unproven, disagreeing or staging_only,
which ``undecided_cause`` reads back for a count), never a guess: one per waiting record when its own ``needed_by`` date is the
missing side (no record it waits on can then be compared, so a gap per record would only
repeat it), else one per record. A risk is ``proven`` only on a proven blocker (pegged
and proven not reached); a candidate (a PO linked by item only, an undecided record) is
a possible risk. Nothing here names a process, stage, type or date column: the graphs declare them.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from operating.attested_conflicts import conflicts as attested_conflicts
from operating.ontology_tools import _gap
from operating.ontology_tools_process import NEVER_AUTHORITY, Dated, ProcessTools, _Ctx
from operating.ontology_tools_traverse import WITHHELD

SCHEDULE_RISK = "schedule_risk"
UNDECIDED = "schedule_undecided"
MAX_DATES = 12  # cited dates listed per side (the contract's cap)
SIDES = ("needed_by", "available_by")
# why a side gives no date to compare, by ``ProcessTools._due_date``'s gap reason; any
# other reason is unproven (a date not proven), never a date
CAUSES = {"no_due_date": "undated", WITHHELD: "withheld", "due_date_unproven": "unproven",
          "due_date_attestation_unproven": "unproven", "due_dates_disagree": "disagreeing",
          "due_date_conflict": "disagreeing"}
STAGING_ONLY = "staging_only"
_CAUSE = re.compile(r"is not established: (" + "|".join(SIDES) + r") ("
                    + "|".join(sorted({*CAUSES.values(), STAGING_ONLY})) + r") \(")


def schedule_risks(tools: ProcessTools, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                   stage: Mapping[str, Any], blockers: Sequence[Mapping[str, Any]]
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(risks, gaps) of ``stage`` of root ``oid`` over its blocker entries (module doc).
    Each record is compared once per waiting record; entries without a record, or whose
    prerequisite declares no schedule, give nothing (the blocker entry says why)."""
    dates: dict[tuple[str, str, str], Dated] = {}
    found: dict[str, list[dict[str, Any]]] = {}

    def due(other: Mapping[str, Any], sid: str, root: str) -> tuple[Dated, str | None]:
        """A stage's due date for ``root`` as ``other`` reads it, and why it is no
        authority (a staging record) when it is not."""
        key = (other["id"], sid, root)
        if key not in dates:
            if root not in found:
                found[root] = attested_conflicts(tools.base, ctx.scope, root,
                                                  other["root_type"])[0]
            target = next(s for s in other["stages"] if s["id"] == sid)
            dates[key] = tools._due_date(ctx, other, root, target, found[root])
        lineage = ctx.visible([root])[root]["lineage"]
        return dates[key], (lineage if lineage in NEVER_AUTHORITY else None)

    risks: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    # waiting side without a needed_by date -> [withheld, why, records it leaves undecided]
    unneeded: dict[tuple[str, str, str], list[Any]] = {}

    def problem(side: str, side_graph: Mapping[str, Any], side_stage: str, root: str
                ) -> tuple[tuple[Dated, str | None], str | None, bool]:
        """A side's read, why it gives no date to compare (None: it gives one) and
        whether that is because the date is withheld from this reader."""
        dated, staging = read = due(side_graph, side_stage, root)
        where = f"({side_graph['id']}.{side_stage} of {root})"
        why = dated.gap or dated.unattested
        if why is not None:
            withheld = why["state"] == "inaccessible"
            cause = "withheld" if withheld else CAUSES.get(why["reason"], "unproven")
            return read, (f"{side} {cause} {where}: {why['reason']}"
                          + (f", {why['detail']}" if why.get("detail") else "")), withheld
        if staging is not None:
            return read, (f"{side} {STAGING_ONLY} {where}: read from a {staging} record, a "
                          "lead and never authority"), False
        return read, None, False

    for entry in blockers:
        record = entry.get("object")
        if record is None:
            continue
        if entry.get("via"):   # "<process>.<stage> <record>": that record waits on it
            name, waiting = entry["via"].split(" ", 1)
            process, sid = name.split(".", 1)
            waits_graph = tools.graphs[process]
            waits = next(s for s in waits_graph["stages"] if s["id"] == sid)
        else:
            waits_graph, waits, waiting = graph, stage, oid
        for pre in waits.get("prerequisites", ()):
            if "schedule" not in pre or (pre["process"], pre["stage"]) != (
                    entry["process"], entry["stage"]):
                continue
            key = (waits_graph["id"], waits["id"], waiting, pre["process"], pre["stage"], record)
            if key in seen:
                continue
            seen.add(key)
            schedule = pre["schedule"]
            source = schedule["available_by"]
            need, why, withheld = problem("needed_by", waits_graph, schedule["needed_by"],
                                          waiting)
            if why is not None:   # nothing it waits on compares: said once, for it
                said = unneeded.setdefault(
                    (waits_graph["id"], schedule["needed_by"], waiting), [withheld, why, 0])
                said[2] += 1
                continue
            subject = f"{pre['process']}.{pre['stage']} {record}"[:700]
            other = tools.graphs.get(source["process"])
            if other is None:
                why = f"available_by unproven (process {source['process']} is not loaded)"
            else:
                have, why, withheld = problem("available_by", other, source["stage"], record)
            if why is not None:
                gaps.append(_gap("inaccessible" if withheld else "unknown", UNDECIDED, subject,
                                 ("whether it is due after it is needed is not established: "
                                  + why)[:600]))
                continue
            needed, available = need[0], have[0]
            if available.date <= needed.date:
                continue
            risk = {"kind": SCHEDULE_RISK, "process": pre["process"], "stage": pre["stage"],
                    "object": record,
                    "proven": entry["missing"]["state"] == "absent_in_source",
                    "days_late": (available.date - needed.date).days,
                    "needed_by": _side(waits_graph["id"], schedule["needed_by"], waiting,
                                       needed),
                    "available_by": _side(source["process"], source["stage"], record,
                                          available)}
            if entry.get("via"):
                risk["via"] = entry["via"]
            risks.append(risk)
    # proven risks first, then the latest: the cap drops the least certain and least late
    risks.sort(key=lambda r: (not r["proven"], -r["days_late"]))
    return risks, [_gap("inaccessible" if withheld else "unknown", UNDECIDED,
                        f"{process}.{sid} {waiting}"[:700],
                        (f"whether any of the {count} prerequisite record(s) it waits on is due "
                         f"after it is needed is not established: {why}")[:600])
                   for (process, sid, waiting), (withheld, why, count) in unneeded.items()
                   ] + gaps


def undecided_cause(gap: Mapping[str, Any]) -> tuple[str, str] | None:
    """(side, cause) of a ``schedule_undecided`` gap this module wrote; None for any other
    gap or a detail it does not read as one (counted apart, never guessed)."""
    found = gap.get("reason") == UNDECIDED and _CAUSE.search(gap.get("detail") or "")
    return (found.group(1), found.group(2)) if found else None


def _side(process: str, stage: str, record: str, dated: Dated) -> dict[str, Any]:
    return {"process": process, "stage": stage, "object": record,
            "date": dated.date.isoformat(), "dates": dated.values[:MAX_DATES]}
