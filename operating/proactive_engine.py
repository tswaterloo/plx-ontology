"""Proactive engine: scheduled, set-based gap detection over the whole projection store,
and a persisted per-employee gap inbox the console Home reads.

The detectors read the store with SQL over all its current records (never one object
read at a time), so ``scanned`` is the whole store; each says ``complete``, ``truncated``,
``blocked`` (a source the rule needs is not served), ``not_configured`` or ``failed``:

* ``late_or_unconfirmed_po``: a purchase order open by the one PO status definition
  (``operating.po_status``: each settled record by its own source, the Portal header word
  with its own lines' summed quantities, a BC order's fullyReceived; an unknown status,
  a Sage-only or FileMaker-only order included, is counted ``not_evaluated`` by its
  reason, never open) judged on its requested-by date (its ``expected_date``; none is
  ``no_need_date``, never a line's or the supplier's date in its place): past it while
  open is late (``late_po`` ``overdue``, or ``unconfirmed_po`` when no supplier
  confirmation is linked); not yet past it with a settled confirmation after it is at
  risk (``late_po`` ``confirmed_after_need_date``). A confirmation linked only as a lead
  (when not already late), two need dates or two statuses across one order's settled
  identities are counted ``not_evaluated``, never raised; the result's ``counts`` (and
  the run summary's ``po_lateness``) say how many were evaluated, late and at risk;
* ``systems_disagree`` (raised by ``late_or_unconfirmed_po``): two linked records of one
  purchase order disagree (``po_status.read``'s conflicts: receipt state, received
  quantity, requested-by date), ONE gap per pair and field naming both values and who
  governs; each side's citations are their own group, so a reader of one side never
  sees it. A BC side that governs receipt state also joins the late gap's evidence;
* ``po_receipt_quantity_mismatch``: the received quantity of a PO line (its receipt
  lines through settled ``receives_line`` links, one quantity per receipt line identity)
  above the ordered one, or below it on an order recorded as fully received (closed
  ``RECEIVED`` by the one PO status definition); a unit that
  differs, a disputed quantity or a lead-only link is ``not_evaluated``;
* ``receipt_without_qc``: a received receipt with no QC record linked to it or its
  lines. ``blocked`` while the schema declares no QC link (the QC relations are not
  served: upstream ask Q3) unless the caller names the link types;
* ``order_without_customer_po``: the orders ``operating.customer_po_coverage``'s full
  mirror scan classifies ``absent_in_this_source`` (no linked FM row holding one), mapped
  to their projected record; ``complete`` only when that scan was;
* ``late_order``: ``operating.proactive_gaps``' late-order rule over every purchase order
  line (expected receipt date before ``as_of``, received below ordered; one cited value of
  each, else the line is ``not_evaluated``), ONE gap per order through its settled
  ``line_of`` links, with the never-sent supplier draft (``follow_up.draft``,
  ``late_order_draft``): the order's number, its late lines and the governing date (the
  supplier's confirmed delivery date through a settled ``SupplierConfirmation.confirms``,
  else "our expected date"), each fact in a side list of citations. A reader who may not
  open every fact the draft cites gets the gap without the draft; the console words it
  again for the person who proposes it (``order_draft``) and records it on the case;
* ``expired_quote`` and ``conflicting_quantity``: ``operating.proactive_gaps``' rules over
  every quote and every quantity of each settled identity cluster;
* ``orders_at_risk``: ``operating.impact_chain``'s walk from every late or slipping
  purchase order (``plx.process``'s own verdict, asked through ``call``) over recorded
  links only to the customer orders it feeds: ONE gap per order at risk (days at risk
  above 0; days at risk is a lower bound, no assembly lead time is recorded), citing the PO's lateness
  dates (each its own group), every record of the chain (each its own group, so a reader
  who may not open one link of the chain never sees the gap) and the order's required
  date; never a value. The verdict is the run principal's: it also rests on what the
  receipt and promise sources do NOT hold, so the gap further requires (``gap_requires``)
  a grant on every source audience the ``po_delivery_window`` graph reads
  (``_lateness_audiences``): a reader whose own verdict would be withheld never sees it.
  The summary's ``top_finding`` says "N customer orders at risk from M late POs, worst
  first" (counts only). ``not_configured`` without a caller;
* ``card_rules``: ``missing_in_record`` and attested conflicts from the ``plx.object``
  cards of the documents that attest a record (bounded per-object reads, ``CARD_BOUND``).

A gap carries the citations its claim rests on, grouped (each group must stay cited for a
reader to see the gap), a stable id and a proposed ``follow_up``
(``operator_ui.object_workspace.FOLLOW_UP_FIELDS``) that only a person sends. ``run``
writes one inbox file next to the store: a gap found again keeps its id and first sight,
a gap a complete detector no longer finds is ``resolved`` (found again: reopened), and an
acknowledgement is per reader. ``read_inbox`` trims for one reader: a gap shows only when
its subject and at least one citation of every group sit in audiences the reader is
granted, at markings it holds, and pass the host's per-item decision (a gap of a
``REQUIRES_GRANT`` detector also needs its recorded audience requirements, every one
granted; none recorded: never shown); the counts it
returns are over what that reader may read.

``refresh`` is the full scheduled run (every detector: the customer PO coverage scan and
the tool caller's ``plx.process``/``plx.object`` reads as the run principal), shared by
``main`` and the host's post-build hook (``after_build(scheduled=)``), within a time
budget (``REFRESH_BUDGET_SECONDS``): a detector the budget stops is ``truncated`` with
reason ``time_budget``, never ``complete``, and leaves its gaps as they were.

``python3 -m operating.proactive_engine`` is the live check's command
(live check ``case.proactive_engine``): one host capture cycle, the engine
over its projection, then the inbox read per reader; counts and enums only. Evidence
label: that of the captures it reads (live-unverified until the verifier runs it).
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import functools
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore
from ontology.units import unit_token

from . import po_status
from . import proactive_gaps as pg

SCHEMA = "plx-proactive-engine/v1"
INBOX_SCHEMA = "plx-gap-inbox/v1"
MAX_SHOWN = 100
CARD_BOUND = 500
VALUES_CHUNK = 500   # object ids per value read (``_Store.values``)
KEEP_RUNS = 100
# The host cycle's full refresh (``after_build(scheduled=)``) starts no detector and makes
# no tool call after this many seconds: the cycle lock is not held unboundedly. Sized for a
# ~2.5M-object store (store-only detectors alone ~4 min there) next to a 1-4 h build.
REFRESH_BUDGET_SECONDS = 1500
TIME_BUDGET = "time_budget"
SETTLED = ("verified", "reviewed")
CONFIRMING = ("verified", "reviewed", "corroborated")
QC_TYPES = ("RelPublicAppIncomingQcRecord", "QualityRecord")
PRIORITY = ("high", "medium", "low")
DETECTORS = ("late_or_unconfirmed_po", "po_receipt_quantity_mismatch", "receipt_without_qc",
             "order_without_customer_po", "expired_quote", "conflicting_quantity",
             "late_order", "orders_at_risk", "card_rules")
SET_BASED = frozenset(DETECTORS) - {"card_rules", "orders_at_risk"}
STATES = ("open", "acknowledged", "resolved")
# Detectors whose claim rests on what sources do not hold (an absence), so on the run
# principal's grant: their gaps show only to a reader granted every recorded audience.
REQUIRES_GRANT = frozenset({"orders_at_risk"})
# Gap kinds whose wording sums over the whole run (money at stake): an open one is reworded
# only by a ``complete`` run of its detector.
WHOLE_ONLY = frozenset({"po_impact_margin"})
LATENESS_GRAPH = Path(__file__).resolve().parents[1] / "ontology" / "schema" / "process" \
    / "po_delivery_window.json"
UNREADABLE = '["unreadable"]'   # a markings value no reader holds (fail closed)
CONFIRMATION_TYPE = "SupplierConfirmation"
CONFIRMATION_LINK = po_status.CONFIRMS   # confirmation -> the order it confirms
CONFIRMED_DATE = "delivery_date"
DRAFT_LABEL = "Draft supplier message (not sent)"
DRAFT_STATUS = "draft_not_sent"
MAX_DRAFT_TEXT = 1500
MAX_DRAFT_LINES = 6
MAX_DRAFT_CITATIONS = 20   # the catalog's workbench.draft.record citation_list max_items

Cite = tuple  # (l1_handle, object_id, canonical markings)


# --------------------------------------------------------------------------- store reads

class _Store:
    """One store generation, read only, every read over current records not removed."""

    def __init__(self, path: Path) -> None:
        if not Path(path).is_file():
            raise ostore.StoreError("store_missing")
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # No query of a detector may build a transient index over a whole table: on a
        # store of millions of value cells that index is a multi-GB temp file and minutes.
        self.conn.execute("PRAGMA automatic_index = OFF")
        problem = ostore.format_problem(self.conn)
        if problem is not None:
            self.conn.close()
            raise ostore.StoreError("store_format_mismatch", problem)

    def close(self) -> None:
        self.conn.close()

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def values(self, types: Sequence[str], props: Sequence[str] | None = None, *,
               objects: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        """object_id -> {"type", "props": {property: [row, ...]}}; ``objects``: only these.

        Object first, in chunks of ``VALUES_CHUNK`` ids: the records of the types (not
        removed) are listed by the type index, then each chunk's values are read by object
        id (version, source, cell). One query over a whole type let the planner start from
        the property instead and index every value cell of the store on the fly (a
        multi-GB temp file; the home's engine run took 23 minutes on 2.5M objects)."""
        params: dict[str, Any] = {"types": json.dumps(list(types))}
        listed = ("SELECT o.object_id FROM objects o "
                  "WHERE o.object_type IN (SELECT value FROM json_each(:types)) AND NOT "
                  + ostore.removed_at_sql("o.object_id"))
        if objects is not None:
            listed += " AND o.object_id IN (SELECT value FROM json_each(:objects))"
            params["objects"] = json.dumps(list(objects))
        ids = [row[0] for row in self.conn.execute(listed + " ORDER BY o.object_id", params)]
        sql = ("SELECT o.object_id, o.object_type, v.property, v.value_type, v.value, v.unit, "
               "v.quantity_role, v.l1_handle, v.markings FROM objects o "
               "JOIN object_values v ON v.object_id = o.object_id "
               "WHERE o.object_id IN (SELECT value FROM json_each(:chunk)) "
               "AND v.superseded_at IS NULL")
        chunk_params: dict[str, Any] = {}
        if props is not None:
            sql += " AND v.property IN (SELECT value FROM json_each(:props))"
            chunk_params["props"] = json.dumps(list(props))
        sql += " ORDER BY o.object_id, v.property, v.l1_handle"
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), VALUES_CHUNK):
            chunk_params["chunk"] = json.dumps(ids[start:start + VALUES_CHUNK])
            for row in self.conn.execute(sql, chunk_params):
                entry = out.setdefault(row["object_id"],
                                       {"type": row["object_type"], "props": {}})
                entry["props"].setdefault(row["property"], []).append(dict(row))
        return out

    def links(self, types: Sequence[str], tiers: Sequence[str] | None = None
              ) -> list[dict[str, Any]]:
        """Current links of these types between records not removed; never contradicted."""
        sql = ("SELECT l.link_id, l.link_type, l.from_object, l.to_object, l.tier, l.markings "
               "FROM links l "
               "WHERE l.link_type IN (SELECT value FROM json_each(:types)) "
               "AND l.superseded_at IS NULL AND l.tier <> 'contradicted' "
               "AND NOT " + ostore.removed_at_sql("l.from_object")
               + " AND NOT " + ostore.removed_at_sql("l.to_object"))
        params: dict[str, Any] = {"types": json.dumps(list(types))}
        if tiers is not None:
            sql += " AND l.tier IN (SELECT value FROM json_each(:tiers))"
            params["tiers"] = json.dumps(list(tiers))
        return [dict(row) for row in self.conn.execute(sql, params)]

    def versions(self, object_type: str | None = None, handles: Iterable[str] | None = None,
                 objects: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Current versions (handle, object, markings) of a type, of these handles or of
        these objects."""
        sql = ("SELECT v.l1_handle, v.object_id, v.markings, o.object_type FROM object_versions v "
               "JOIN objects o ON o.object_id = v.object_id WHERE v.superseded_at IS NULL AND NOT "
               + ostore.removed_at_sql("o.object_id"))
        params: dict[str, Any] = {}
        if object_type is not None:
            sql += " AND o.object_type = :type"
            params["type"] = object_type
        if handles is not None:
            sql += " AND v.l1_handle IN (SELECT value FROM json_each(:handles))"
            params["handles"] = json.dumps(sorted(set(handles)))
        if objects is not None:
            sql += " AND v.object_id IN (SELECT value FROM json_each(:objects))"
            params["objects"] = json.dumps(sorted(set(objects)))
        return [dict(row) for row in self.conn.execute(sql, params)]

    def scope(self) -> list[tuple[str, str, int]]:
        """(audience key, current markings, objects): the whole store the run covered."""
        return [tuple(row) for row in self.conn.execute(
            "SELECT o.audience_key, v.markings, count(*) FROM objects o JOIN object_versions v "
            "ON v.object_id = o.object_id AND v.superseded_at IS NULL WHERE NOT "
            + ostore.removed_at_sql("o.object_id") + " GROUP BY o.audience_key, v.markings")]


def _markings(*texts: str | None) -> str:
    """The union of markings values, canonical; anything unreadable never shows."""
    held: set[str] = set()
    for text in texts:
        try:
            parsed = json.loads(text) if text else []
        except (TypeError, ValueError):
            return UNREADABLE
        if not isinstance(parsed, list) or not all(isinstance(m, str) for m in parsed):
            return UNREADABLE
        held.update(parsed)
    try:
        return ostore.markings_text(held)
    except ostore.StoreError:
        return UNREADABLE


def _cites(rows: Iterable[Mapping[str, Any]], *extra_markings: str | None) -> list[Cite]:
    out: dict[str, Cite] = {}
    for row in rows:
        handle = row.get("l1_handle")
        if isinstance(handle, str) and handle and handle not in out:
            out[handle] = (handle, row.get("object_id"),
                           _markings(row.get("markings"), *extra_markings))
    return list(out.values())


_clusters = po_status.clusters   # member -> its settled cluster's least object id


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _gap(kind: str, detector: str, object_id: str, object_type: str, key: str, *,
         summary: str, missing: str, why: str, priority: str, about: tuple[str, str],
         groups: Sequence[Sequence[Cite]], extra: Sequence[Cite] = (),
         days_late: int | None = None) -> dict[str, Any] | None:
    """``days_late``: the days a purchase order open by the one status definition is past its
    requested-by date (None: not late; ``late_order``'s line-level lateness is another gap
    kind and never carries it); the inbox lists every late purchase order first, longest late
    first, whatever its kind's priority."""
    groups = [list(group) for group in groups]
    if not groups or not all(groups):
        return None   # a claim with an uncited part is not raised
    handles = list(dict.fromkeys([c[0] for g in groups for c in g] + [c[0] for c in extra]))
    who = pg._who(object_type, {"about": about, "handles": handles})
    return {"id": "pe-" + hashlib.sha256(f"{kind}|{object_id}|{key}".encode("utf-8"))
            .hexdigest()[:16],
            "kind": kind, "detector": detector, "object_id": object_id,
            "object_type": object_type, "summary": pg._clip(summary),
            "groups": groups, "extra": list(extra),
            "follow_up": {"object_id": object_id, "missing": pg._clip(missing, 480),
                          "why": why, "who_can_answer": who, "state": "unknown",
                          "priority": priority,
                          **({"days_late": days_late} if days_late is not None else {})}}


Part = tuple  # (object_id, canonical markings): one record a skip's cause reads


def _parts(rows: Iterable[Mapping[str, Any]], *extra_markings: str | None) -> list[Part]:
    """The records ``rows`` were read from, each at its row's markings joined with
    ``extra_markings`` (the links that tie it to the rest of the cause)."""
    return [(row.get("object_id"), _markings(row.get("markings"), *extra_markings))
            for row in rows]


class _Skips(Counter):
    """A rule's records it did not judge: the count per reason (the run summary) and each
    skip with its subjects (the records it left unjudged) and its cause (every record and
    link, at its markings, the decision to skip read: ``skips``). A reader is told how many
    of the records it may read a rule left unjudged, counted only over skips whose whole
    cause it may read, the way a gap shows only when the reader may open all of its
    evidence: a skip caused by a record or a link it may not read (an identity twin
    disputing the need date, a lead from a hidden confirmation) is not counted for it, so
    no count names or hints at a hidden record. A skip no record stands for (an order the
    store never projected) or whose cause names a value without its record is
    ``unattributed``: unknown for every reader alike."""

    def __init__(self) -> None:
        super().__init__()
        self.skips: list[tuple[tuple[str, ...], tuple[Part, ...]]] = []
        self.unattributed = 0

    def note(self, reason: str, subjects: Sequence[str] = (),
             cause: Iterable[Part] = ()) -> None:
        self[reason] += 1
        parts = {(subject, "[]") for subject in subjects}
        for object_id, markings in cause:
            if not isinstance(object_id, str) or not object_id:
                parts = set()   # a cause no reader can be checked against: fail closed
                break
            parts.add((object_id, markings if isinstance(markings, str) else UNREADABLE))
        if not subjects or not parts:
            self.unattributed += 1
            return
        self.skips.append((tuple(sorted(set(subjects))), tuple(sorted(parts))))


def _result(gaps: Iterable[dict[str, Any] | None], *, status: str = "complete",
            reasons: Sequence[str] = (), not_evaluated: Counter | None = None
            ) -> dict[str, Any]:
    skips = not_evaluated if isinstance(not_evaluated, _Skips) else None
    counted = sum((not_evaluated or Counter()).values())
    return {"status": status, "reasons": list(reasons),
            "gaps": [gap for gap in gaps if gap is not None],
            "not_evaluated": dict(sorted((not_evaluated or Counter()).items())),
            "unjudged": [{"subjects": list(subjects), "cause": [list(p) for p in cause]}
                         for subjects, cause in skips.skips] if skips is not None else [],
            # a count without its records (a plain Counter) is attributed to nobody
            "unattributed": skips.unattributed if skips is not None else counted}


@functools.lru_cache(maxsize=1)
def _identity_link_types() -> tuple[str, ...]:
    from ontology.schema import load_schema
    from .ontology_tools import identity_link_type

    return tuple(sorted(lid for lid, link in load_schema().link_types.items()
                        if identity_link_type(link)))


@functools.lru_cache(maxsize=1)
def declared_qc_links() -> tuple[str, ...]:
    """Link types the schema declares between a QC record and anything (none today)."""
    from ontology.schema import load_schema

    return tuple(sorted(lid for lid, link in load_schema().link_types.items()
                        if link.get("from") in QC_TYPES or link.get("to") in QC_TYPES))


# --------------------------------------------------------------------------- detectors

PO_STATUS_PROPS = tuple(po_status.SOURCE_OF)
# the follow-up field naming the order an item is about (its key and its record's rank):
# never shown, read by ``read_inbox`` to list one item per order for each reader
_ORDER = "order"


def _late_or_unconfirmed_po(st: _Store, as_of: date) -> dict[str, Any]:
    """Each settled PO read by the one purchase-order reader (``po_status.read`` over
    ``po_status.StoreSource``: status, lines, FM QC log, ranked need date, promise and
    verdict); this rule adds only its counts, skips, citations and gaps."""
    name = "late_or_unconfirmed_po"
    src = po_status.StoreSource(st)
    skipped = _Skips()
    counts: Counter = Counter()
    classes: Counter = Counter()
    gaps = []
    disagreeing: set[tuple[str, frozenset]] = set()
    # one order across systems: a settled cluster and every record it is compared with
    # (``po_status.compared_twin``), joined transitively (one BC order, two Portal twins:
    # one order). Each record's late or unconfirmed item is kept with its order key and
    # rank; ``read_inbox`` shows one per order among those its reader may open (``_ORDER``)
    reads = [po_status.read(src, top, as_of) for top, _ in sorted(src.clusters.items())]
    joined = [(po.members[0], other) for po in reads
              for other in (*po.members[1:], *po.date_twins)]
    root = po_status.clusters(joined, [m for po in reads
                                       for m in (*po.members, *po.date_twins)])
    counted: dict[str, set[str]] = defaultdict(set)
    order_class: dict[str, tuple[list, str]] = {}
    for po in reads:
        members = po.members
        order = root.get(members[0], members[0])
        # BC's record governs; among others the least record id, so the choice is pinned
        rank = [0 if any(po_status.system_of(m) == po_status.RECEIPT_AUTHORITY
                         for m in members) else 1, min(members)]

        def order_gap(gap: Any, _order: str = order, _rank: list = rank) -> None:
            if gap is not None:
                gap["follow_up"][_ORDER] = {"key": _order, "rank": _rank}
                gaps.append(gap)

        def count(key: str, _order: str = order) -> int:
            if key in counted[_order]:
                return 0
            counted[_order].add(key)
            return 1
        marks = po.identity_marks
        state = po.status
        # one class per order: its governing record's (BC first, else the least id)
        if order not in order_class or rank < order_class[order][0]:
            order_class[order] = (rank, state.cls)
        for conflict in po.conflicts:
            records = frozenset(r for side in conflict["sides"] for r in side["records"])
            if not records or (conflict["field"], records) in disagreeing:
                continue   # the same disagreement read again from the other record's side
            disagreeing.add((conflict["field"], records))
            gaps.append(_systems_disagree(name, conflict, records, [*marks, *po.marks]))

        def rows(prop: str) -> list[dict[str, Any]]:
            return [r for m in members for r in src.props(m).get(prop, [])]
        # what every skip of this order read: its records and the identity links joining
        # them, their status, line quantity and date values (``skip``'s cause)
        read: list[Part] = [(m, _markings(*marks)) for m in members] + _parts(
            [*po.evidence, *rows("expected_date")], *marks, *po.marks)

        def skip(reason: str, *more: Part) -> None:
            skipped.note(reason, members, [*read, *more])
        if state.cls == po_status.UNKNOWN:
            skip(state.reason)   # neither open nor closed: counted, never guessed
            continue
        if state.cls != po_status.OPEN:
            continue
        rep = min(m for m in members if any(src.props(m).get(p) for p in PO_STATUS_PROPS))
        for _ in po.need.unparseable:
            skip("date_unparseable")
        verdict, why = po.verdict
        if why == "no_requested_date":
            # the requested-by date is the reference or it is missing: never a line's or
            # the supplier's date in its place
            skip("no_need_date")
            continue
        if why == "need_date_disputed":
            skip("need_date_disputed")
            continue
        need = po.need.hi
        need_cites = _cites(po.need.governing, *marks)
        # every status record and line quantity the verdict rests on is its own evidence:
        # a reader who may not open one of them sees no gap (none is an alternative)
        status_cites = [[cite] for cite in _cites(po.evidence, *marks, *po.marks)] or [[]]
        number = _cites(rows("po_number"), *marks) + _cites(
            [r for t in po.date_twins for r in src.props(t).get("po_number", [])],
            *marks, *po.marks)
        links = src.confirm_links(members)
        linked = [(link["from_object"], _markings(link["markings"], *marks)) for link in links]
        late_now = verdict == po_status.LATE
        if late_now or not links:   # decided without a confirmation (none is at risk)
            counts["evaluated"] += count("evaluated")
            counts["late"] += count("late") if late_now else 0
        if not links:
            if need <= as_of:
                counts["unconfirmed"] += count("unconfirmed")
                order_gap(_gap(
                    "unconfirmed_po", name, rep, "PurchaseOrder", "unconfirmed",
                    summary=f"An open purchase order needed by {need.isoformat()} has no "
                            "supplier confirmation",
                    missing=f"Has the supplier confirmed the purchase order needed by "
                            f"{need.isoformat()}?",
                    why="The order is open, its need date has come and no supplier "
                        "confirmation is linked to it in the store.",
                    priority="high", about=("PurchaseOrder", "expected_date"),
                    groups=[need_cites, *status_cites], extra=number,
                    days_late=(as_of - need).days if late_now else None))
            continue
        if late_now:   # late on the requested-by date alone, whatever was confirmed
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=f"An open purchase order is past its need date {need.isoformat()}",
                missing=f"When will the purchase order needed by {need.isoformat()} be "
                        "received?",
                why="The order is still open after its need date.",
                priority="medium", about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number,
                days_late=(as_of - need).days))
            continue
        if any(link["tier"] not in CONFIRMING for link in links):
            skip("confirmation_unsettled", *linked)
            continue   # a lead may be the confirmation: at risk or not is not known
        dated = {id(link) for _, link in po.promise.items}
        undated = any(id(link) not in dated for link in links)
        if verdict == po_status.AT_RISK:
            counts["evaluated"] += count("evaluated")
            counts["at_risk"] += count("at_risk")
            shown = po.promise.latest.isoformat()
            by_link: dict[int, tuple[dict[str, Any], list]] = {}
            for row, link in po.promise.items:
                if row["value"] is not None and po_status.day_of(row["value"]) \
                        == po.promise.latest:
                    by_link.setdefault(id(link), (link, []))[1].append(row)
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "confirmed_after_need_date",
                summary=f"The supplier confirmed delivery on {shown}, after the need date "
                        f"{need.isoformat()}",
                missing=f"Can the purchase order needed by {need.isoformat()} arrive in time, "
                        f"or must the plan move to {shown}?",
                why="A linked supplier confirmation states a delivery date later than the "
                    "order's need date.",
                priority="high", about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites,
                        *[_cites(found, link["markings"]) for link, found in by_link.values()]],
                extra=number))
        elif undated:
            skip("confirmation_undated", *linked)
        elif po.promise.reason == "promises_disagree":
            # the confirmations' dates disagree and no document date orders them
            skip("promises_disagree", *linked)
        else:
            counts["evaluated"] += count("evaluated")
    result = _result(gaps, not_evaluated=skipped)
    classes.update(cls for _rank, cls in order_class.values())
    result["counts"] = {"pos": len(order_class),
                        "classes": {cls: classes.get(cls, 0) for cls in po_status.CLASSES},
                        **{key: counts.get(key, 0) for key in (
                            "evaluated", "late", "at_risk", "unconfirmed")}}
    return result


_FIELD_WORDS = {"receipt_state": "whether it is fully received",
                "received_quantity": "the quantity received",
                "expected_date": "the requested-by date"}
_STATE_WORDS = {po_status.FULLY_RECEIVED: "fully received",
                po_status.OPEN_LINES: "open lines still outstanding"}


def _systems_disagree(name: str, conflict: Mapping[str, Any], records: Iterable[str],
                      marks: Sequence[str]) -> dict[str, Any] | None:
    """A ``systems_disagree`` gap: two linked records of one purchase order state different
    values (``po_status.read``'s conflict). Each side's cited records are their own
    evidence group, so a reader who may not open either side sees no gap; the summary
    names both values and who governs, never one picked silently."""
    field, governs = conflict["field"], conflict["governs"]
    sides = conflict["sides"]
    unit = f" {conflict['unit']}" if conflict.get("unit") else ""

    def said(side: Mapping[str, Any]) -> str:
        value = side["value"]
        return (_STATE_WORDS.get(value, value) if field == "receipt_state"
                else f"{value}{unit}")
    groups = [_cites(side["items"], *marks, *conflict.get("marks", ())) for side in sides]
    rule = ("BC lines govern receipt state" if field != "expected_date" and governs
            else "the declared date precedence governs" if governs else "no system governs")
    subject = next((side["records"][0] for side in sides
                    if side["system"] == governs and side["records"]), min(records))
    return _gap(
        "systems_disagree", name, subject, "PurchaseOrder",
        f"{field}|{'|'.join(sorted(records))}",
        summary=(f"Systems disagree on {_FIELD_WORDS[field]} of a purchase order: "
                 + "; ".join(f"{side['system']} says {said(side)}" for side in sides)
                 + f" ({rule})"),
        missing=f"Which record of this purchase order is right about {_FIELD_WORDS[field]}, "
                "and can the other one be corrected?",
        why="Two linked records of the same purchase order state different values; the "
            "answer cites both and names the one that governs.",
        priority="medium",
        about=("PurchaseOrder", "expected_date" if field == "expected_date" else "status"),
        groups=groups)


def _quantity(rows: Sequence[Mapping[str, Any]]) -> tuple[Decimal, str] | None | bool:
    """The one (number, unit) the rows state; None: none; False: they disagree."""
    found = set()
    for row in rows:
        number, unit = pg._decimal(row["value"]), unit_token(row["unit"])
        if number is None or unit is None:
            return False
        found.add((number, unit))
    if not found:
        return None
    return found.pop() if len(found) == 1 else False


def _po_receipt_quantity_mismatch(st: _Store, as_of: date) -> dict[str, Any]:
    name = "po_receipt_quantity_mismatch"
    ordered = st.values(("PurchaseOrderLine",), ("ordered_quantity",))
    received = st.values(("ReceiptLine",), ("received_quantity",))
    src = po_status.StoreSource(st)
    twins = st.links(("ReceiptLine.same_record_as",), SETTLED)
    root = _clusters(((l["from_object"], l["to_object"]) for l in twins), received)
    identity: dict[str, list[str]] = defaultdict(list)
    for link in twins:
        if link["from_object"] in root:
            identity[root[link["from_object"]]].append(link["markings"])
    receives = st.links(("ReceiptLine.receives_line",))
    targets: dict[str, set[str]] = defaultdict(set)
    link_marks: dict[str, list[str]] = defaultdict(list)
    unsettled: dict[str, list[Part]] = defaultdict(list)
    for link in receives:
        if link["from_object"] not in root:
            continue
        if link["tier"] not in CONFIRMING:
            unsettled[link["to_object"]].append((link["from_object"], link["markings"]))
            continue
        targets[root[link["from_object"]]].add(link["to_object"])
        link_marks[root[link["from_object"]]].append(link["markings"])
    members: dict[str, list[str]] = defaultdict(list)
    for object_id, top in root.items():
        members[top].append(object_id)
    skipped = _Skips()
    per_line: dict[str, list[tuple[tuple[Decimal, str], list[Cite]]]] = defaultdict(list)
    broken: set[str] = set(unsettled)
    for top, cluster in sorted(members.items()):
        lines = targets.get(top, set())
        if not lines:
            continue
        rows = [r for m in cluster for r in received[m]["props"].get("received_quantity", [])]
        # what a skip of this receipt read: its records and identity links, their
        # quantities, and the lines they receive (at the linking marks)
        marks = [*identity.get(top, []), *link_marks[top]]
        read = ([(m, _markings(*marks)) for m in cluster] + _parts(rows, *marks)
                + [(line, _markings(*marks)) for line in sorted(lines)])
        if len(lines) > 1:
            skipped.note("receipt_line_ambiguous", cluster, read)
            broken |= lines
            continue
        found = _quantity(rows)
        (line,) = lines
        if found is False:
            skipped.note("sources_unreconciled", cluster, read)
            broken.add(line)
            continue
        if found is not None:
            per_line[line].append((found, _cites(rows, *link_marks[top])))
    for line in sorted(set(unsettled) & set(ordered)):
        # a lead may be its receipt: not evaluated (the lead's receipt is its cause)
        skipped.note("receipt_link_unsettled", [line],
                     [(receipt, _markings(m)) for receipt, m in unsettled[line]])
    gaps = []
    for line, parts in sorted(per_line.items()):
        if line in broken or line not in ordered:
            continue
        rows = ordered[line]["props"].get("ordered_quantity", [])
        want = _quantity(rows)
        # the line's ordered quantities and every receipt quantity read against them
        read = _parts(rows) + [(c[1], c[2]) for _, cites in parts for c in cites]
        if not want:
            skipped.note("sources_unreconciled" if want is False else "no_ordered_quantity",
                         [line], read)
            continue
        if any(unit != want[1] for (_, unit), _ in parts):
            skipped.note("unit_differs", [line], read)
            continue
        got = sum((number for (number, _), _ in parts), Decimal(0))
        unit = want[1]
        shown = (format(got.normalize(), "f"), format(want[0].normalize(), "f"))
        groups = [_cites(rows), *[cites for _, cites in parts]]
        if got > want[0]:
            gaps.append(_gap(
                "po_receipt_quantity_mismatch", name, line, "PurchaseOrderLine", "over",
                summary=f"Received {shown[0]} {unit} against {shown[1]} {unit} ordered",
                missing=f"Why was more received ({shown[0]} {unit}) than ordered "
                        f"({shown[1]} {unit})?",
                why="The receipt lines linked to this PO line total more than its ordered "
                    "quantity, in the same unit.",
                priority="high", about=("PurchaseOrderLine", "ordered_quantity"),
                groups=groups))
            continue
        if got == want[0]:
            continue
        closed = []
        for po in src.po_of.get(line, []):
            if po not in src.pos:
                continue
            # the order by the one reader: its settled twins' status, lines and FM QC log
            found = po_status.read(src, po, dates=False)
            if found.status == po_status.PoStatus(po_status.CLOSED, po_status.RECEIVED):
                closed += [[cite] for cite in _cites(found.evidence, *found.identity_marks,
                                                     *found.marks)]
        if not closed:
            continue   # still receiving (or not known closed): not yet a gap
        gaps.append(_gap(
            "po_receipt_quantity_mismatch", name, line, "PurchaseOrderLine", "short",
            summary=f"Received {shown[0]} {unit} against {shown[1]} {unit} ordered on an "
                    "order recorded as fully received",
            missing=f"Is the shortfall of the PO line ({shown[0]} of {shown[1]} {unit}) "
                    "still coming, or should the order be corrected?",
            why="The order is recorded as received while its linked receipt lines total "
                "less than the ordered quantity.",
            priority="medium", about=("PurchaseOrderLine", "ordered_quantity"),
            groups=[*groups, *closed]))
    return _result(gaps, not_evaluated=skipped)


def _receipt_without_qc(st: _Store, as_of: date, link_types: Sequence[str]
                        ) -> dict[str, Any]:
    name = "receipt_without_qc"
    if not link_types:
        return _result([], status="blocked", reasons=["qc_link_not_declared"])
    receipts = st.values(("Receipt",), ("received_date", "receipt_number"))
    lines_of: dict[str, set[str]] = defaultdict(set)
    line_marks: dict[tuple[str, str], str] = {}
    for link in st.links(("ReceiptLine.line_of",), CONFIRMING):
        lines_of[link["to_object"]].add(link["from_object"])
        line_marks[(link["to_object"], link["from_object"])] = link["markings"]
    qc: dict[str, set[str]] = defaultdict(set)
    qc_parts: dict[str, list[Part]] = defaultdict(list)   # end -> the QC link's other end
    for link in st.links(tuple(link_types)):
        for end, other in ((link["from_object"], link["to_object"]),
                           (link["to_object"], link["from_object"])):
            qc[end].add(link["tier"])
            qc_parts[end].append((other, link["markings"]))
    skipped = _Skips()
    gaps = []
    for receipt, entry in sorted(receipts.items()):
        dated = [r for r in entry["props"].get("received_date", [])
                 if (d := _day(r["value"])) is not None and d <= as_of]
        if not dated:
            continue
        tiers = set().union(*(qc.get(o, set()) for o in {receipt, *lines_of.get(receipt, ())}))
        if tiers & set(CONFIRMING):
            continue
        if tiers:
            # its cause: the receipt's lines and every QC link on them or on it
            cause = [(other, _markings(m)) for other, m in qc_parts.get(receipt, ())]
            for line in sorted(lines_of.get(receipt, ())):
                joined = line_marks.get((receipt, line))
                cause.append((line, _markings(joined)))
                cause += [(other, _markings(m, joined)) for other, m in qc_parts.get(line, ())]
            skipped.note("qc_link_unsettled", [receipt], cause)
            continue
        gaps.append(_gap(
            "receipt_without_qc", name, receipt, "Receipt", "no_qc",
            summary="A received receipt has no QC record linked to it",
            missing="Was the received material inspected, and where is its QC record?",
            why="No QC record is linked to the receipt or its lines in the store.",
            priority="medium", about=("Receipt", "received_date"), groups=[_cites(dated)],
            extra=_cites(entry["props"].get("receipt_number", []))))
    return _result(gaps, not_evaluated=skipped)


def _order_without_customer_po(st: _Store, coverage: Mapping[str, Any] | None
                               ) -> dict[str, Any]:
    name = "order_without_customer_po"
    if coverage is None:
        return _result([], status="not_configured", reasons=["coverage_not_given"])
    completeness = coverage.get("completeness") or {}
    versions = st.versions("SalesOrder")
    exact = {v["l1_handle"]: v for v in versions}
    stem = {v["l1_handle"].split("@", 1)[0]: v for v in versions}
    held = st.values(("SalesOrder",), ("customer_po",))
    skipped = _Skips()
    gaps = []
    for order in coverage.get("orders") or []:
        if (not isinstance(order, Mapping) or order.get("status") != "absent_in_this_source"
                or order.get("fm_order_entry_holds_customer_po")):
            continue
        citation = order.get("citation")
        found = (exact.get(citation) or stem.get(citation.split("@", 1)[0])
                 if isinstance(citation, str) else None)
        if found is None:
            skipped.note("not_projected")   # no record stands for it
            continue
        if any(r["value"] not in (None, "") for r in
               (held.get(found["object_id"]) or {"props": {}})["props"].get("customer_po", [])):
            continue   # the projection holds one (another source): not absent
        gaps.append(_gap(
            "order_without_customer_po", name, found["object_id"], "SalesOrder",
            "customer_po",
            summary="A sales order holds no customer PO in any field the Portal row carries",
            missing="Which customer PO does this sales order answer to?",
            why="The full scan of the Portal's sales orders found no customer PO on this "
                "order and no linked FM order entry holding one.",
            priority="medium", about=("SalesOrder", "customer_po"),
            groups=[[(found["l1_handle"], found["object_id"], _markings(found["markings"]))]]))
    status = "complete" if completeness.get("complete") is True else "truncated"
    return _result(gaps, status=status, not_evaluated=skipped,
                   reasons=[] if status == "complete" else list(
                       completeness.get("reasons") or ["scan_incomplete"]))


def _expired_quote(st: _Store, as_of: date) -> dict[str, Any]:
    _documents, quotes, _records = pg._roster()
    found = st.values(quotes, ("status", "valid_until", *pg.QUOTE_OUTCOME_FIELDS))
    gaps = []
    for object_id, entry in sorted(found.items()):
        rows = [r for prop_rows in entry["props"].values() for r in prop_rows]
        by_handle = {r["l1_handle"]: r for r in rows}
        items = [{**r, "citation": {"l1_handle": r["l1_handle"]}} for r in rows]
        for item in pg._expired_quote(entry["type"], items, as_of):
            gaps.append(_gap(
                item["kind"], "expired_quote", object_id, entry["type"], item["key"],
                summary=item["summary"], missing=item["missing"], why=item["why"],
                priority=item["priority"], about=item["about"],
                groups=[_cites(by_handle[h] for h in group) for group in item["requires"]]))
    return _result(gaps)


def _quantity_rows(st: _Store) -> list[dict[str, Any]]:
    """Every current quantity value of a record not removed: the ``object_values`` view's
    rows of ``value_type`` quantity, read from its tables quantity kind first (the
    layouts that hold a quantity, then their cells by layout). Through the view the
    planner scanned every value source of the store (51 s on 2.8M). The order is the
    view's scan order: value source, then cell, then the field's place in its layout."""
    found = st.conn.execute(
        "SELECT s.src, c.field AS cell_field, lf.pos, v.object_id, o.object_type, "
        "f.property, CASE WHEN c.field IS NULL THEN json_extract(c.value, lf.path) "
        "ELSE c.value END AS value, k.unit, k.quantity_role, v.l1_handle, k.markings "
        "FROM value_kinds k CROSS JOIN value_layout_fields lf ON lf.kind = k.kind "
        "CROSS JOIN value_cells c ON c.layout = lf.layout "
        "JOIN value_sources s ON s.src = c.src JOIN object_versions v ON v.rowid = s.ver "
        "JOIN objects o ON o.object_id = v.object_id JOIN value_fields f ON f.field = lf.field "
        "WHERE k.value_type = 'quantity' AND s.superseded_at IS NULL AND NOT "
        + ostore.removed_at_sql("o.object_id")).fetchall()
    found.sort(key=lambda r: (r["src"], r["cell_field"] is not None, r["cell_field"] or 0,
                              r["pos"]))
    keep = ("object_id", "object_type", "property", "value", "unit", "quantity_role",
            "l1_handle", "markings")
    return [{key: row[key] for key in keep} for row in found]


def _conflicting_quantity(st: _Store, as_of: date) -> dict[str, Any]:
    rows = _quantity_rows(st)
    objects = {row["object_id"] for row in rows}
    identity = [link for link in st.links(_identity_link_types(), SETTLED)
                if link["from_object"] in objects and link["to_object"] in objects]
    root = _clusters(((l["from_object"], l["to_object"]) for l in identity), objects)
    marks: dict[str, list[str]] = defaultdict(list)
    for link in identity:
        marks[root[link["from_object"]]].append(link["markings"])
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(root[row["object_id"]], row["object_type"], row["property"])].append(dict(row))
    gaps = []
    for (top, object_type, prop), members in sorted(grouped.items()):
        if len({m["l1_handle"] for m in members}) < 2:
            continue
        by_handle = {m["l1_handle"]: m for m in members}
        subject = f"{object_type}.{prop}"
        conflict = {"kind": "value_disagreement", "subject": subject, "values": [
            {"value_type": "quantity", "value": m["value"], "unit": unit_token(m["unit"]),
             "quantity_role": m["quantity_role"], "citation": {"l1_handle": m["l1_handle"]}}
            for m in members]}
        for item in pg._conflicting_quantities([conflict]):
            gaps.append(_gap(
                item["kind"], "conflicting_quantity", top, object_type, item["key"],
                summary=item["summary"], missing=item["missing"], why=item["why"],
                priority=item["priority"], about=(object_type, prop),
                groups=[_cites((by_handle[h] for h in group), *marks.get(top, []))
                        for group in item["requires"]]))
    return _result(gaps)


def _card_of(object_id: str, object_type: str, props: Mapping[str, Any]) -> dict[str, Any]:
    """A ``plx.object``-shaped card of the store's current values, for the shared rules."""
    return {"object": {"object_id": object_id, "object_type": object_type},
            "values": [{"property": prop, "value": row["value"], "unit": row["unit"],
                        "citation": {"l1_handle": row["l1_handle"]}}
                       for prop, rows in props.items() for row in rows]}


def _late_orders(st: _Store, as_of: date, only: str | None = None
                 ) -> tuple[list[dict[str, Any]], "_Skips"] | None:
    """Each order with late lines (``pg._late_line``) and the facts its supplier draft
    states, every fact as ``(value, Cite)``: its number, its supplier's name, its
    confirmed delivery date and each late line. ``only``: that one order (the console's
    re-derivation). None when the schema does not declare the rule."""
    order_type = pg._late_target()
    if order_type is None:
        return None
    line_props = (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED, pg.LINE_ITEM)
    orders_of: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in st.links((pg.LINE_LINK,), CONFIRMING):
        orders_of[link["from_object"]].append((link["to_object"], link["markings"]))
    wanted = None if only is None else sorted(
        line for line, reached in orders_of.items() if any(o == only for o, _m in reached))
    lines = st.values((pg.LINE_TYPE,), line_props, objects=wanted)
    skipped = _Skips()
    late: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = defaultdict(list)
    for line_id, entry in sorted(lines.items()):
        found = pg._late_line(_card_of(line_id, pg.LINE_TYPE, entry["props"]), as_of)
        if found is None:
            if any(len(entry["props"].get(p, ())) > 1 for p in line_props[:3]):
                skipped.note("line_value_disputed", [line_id], _parts(
                    [r for p in line_props[:3] for r in entry["props"].get(p, ())]))
            continue
        reached = {order for order, _marks in orders_of.get(line_id, [])
                   if order.startswith(f"{order_type}:")}
        if len(reached) != 1:
            # unlinked: nothing beyond the line; ambiguous: each order and its link
            skipped.note("line_order_unlinked" if not reached else "line_order_ambiguous",
                         [line_id], [(order, _markings(m)) for order, m in
                                     orders_of.get(line_id, []) if order in reached])
            continue
        (order,) = reached
        if only is not None and order != only:
            continue
        marks = _markings(*[m for o, m in orders_of[line_id] if o == order])
        late[order].append((found, entry, marks))
    if not late:
        return [], skipped
    picked = sorted(late)
    numbers = st.values((order_type,), (pg.ORDER_NUMBER,), objects=picked)
    supplier_type = pg._supplier_target()
    suppliers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if supplier_type is not None:
        for link in st.links((pg.SUPPLIER_LINK,), CONFIRMING):
            if link["from_object"] in late:
                suppliers[link["from_object"]].append((link["to_object"], link["markings"]))
    names = st.values((supplier_type,), (pg.SUPPLIER_NAME,), objects=sorted(
        {t for found in suppliers.values() for t, _m in found})) if supplier_type else {}
    confirms: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    for link in st.links((CONFIRMATION_LINK,), CONFIRMING):
        if link["to_object"] in late:
            confirms[link["to_object"]].append((link["from_object"], link["markings"],
                                                link["tier"] in SETTLED))
    dated = st.values((CONFIRMATION_TYPE,), (CONFIRMED_DATE,), objects=sorted(
        {c for found in confirms.values() for c, _m, _s in found}))
    out = []
    for order in picked:
        facts: dict[str, Any] = {"order": order, "order_type": order_type, "number": None,
                                 "supplier": None, "confirmed": [], "corroborated": [],
                                 "unresolved": []}
        card = _card_of(order, order_type, (numbers.get(order) or {"props": {}})["props"])
        number = pg._one_text(card, pg.ORDER_NUMBER)
        if number is None:
            facts["unresolved"].append("order_number:not_in_record")
        else:
            facts["number"] = (number[0], _cites([r for r in numbers[order]["props"][
                pg.ORDER_NUMBER] if r["l1_handle"] == number[1]])[0])
        if supplier_type is None:
            facts["unresolved"].append("supplier:link_undeclared")
        else:
            reached = {t: m for t, m in suppliers.get(order, [])}
            if len(reached) != 1 or not next(iter(reached)).startswith(f"{supplier_type}:"):
                # none, two, or one that is not a supplier record: no one supplier
                facts["unresolved"].append("supplier:not_linked" if not reached
                                           else "supplier:ambiguous")
            else:
                ((one, link_marks),) = reached.items()
                card = _card_of(one, supplier_type, (names.get(one) or {"props": {}})["props"])
                name = pg._one_text(card, pg.SUPPLIER_NAME)
                if name is None:
                    facts["unresolved"].append("supplier:name_not_in_record")
                else:
                    facts["supplier"] = (name[0], _cites([
                        r for r in names[one]["props"][pg.SUPPLIER_NAME]
                        if r["l1_handle"] == name[1]], link_marks)[0])
        for confirmation, link_marks, settled in sorted(confirms.get(order, [])):
            rows = (dated.get(confirmation) or {"props": {}})["props"].get(CONFIRMED_DATE, [])
            days = {_day(r["value"]) for r in rows}
            if len(days) != 1 or None in days:
                facts["unresolved"].append("confirmed_date:unreadable")
                continue
            # only a settled confirmation is told to the supplier as theirs; a corroborated
            # one stays an internal note
            facts["confirmed" if settled else "corroborated"].append(
                (next(iter(days)).isoformat(), _cites(rows[:1], link_marks)[0]))
        facts["lines"] = []
        for line, entry, marks in late[order]:
            props = entry["props"]

            def cite(prop: str, handle: str | None) -> list[Cite]:
                return _cites([r for r in props.get(prop, []) if r["l1_handle"] == handle],
                              marks)
            facts["lines"].append({
                **line, "date_cite": cite(pg.LINE_DATE, line["date_handle"]),
                "quantity_cites": cite(pg.LINE_ORDERED, line["ordered"][2])
                + cite(pg.LINE_RECEIVED, line["received"][2]),
                "item_cite": cite(pg.LINE_ITEM, line["item"][1]) if line.get("item") else [],
                "evidence": {prop: _cites([r for r in props[prop]
                                           if r["l1_handle"] in line["handles"]], marks)
                             for prop in (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED)}})
        out.append(facts)
    return out, skipped


def _worded(facts: Mapping[str, Any], roles: Sequence[str],
            readable: Callable[[Sequence[Cite]], set[str]] | None = None
            ) -> dict[str, Any] | None:
    """The supplier draft of one order's facts as a reader may read them: a line is used
    only cited whole (its date and both quantities), a withheld item name is left out, a
    withheld number, supplier or confirmation is named in ``unresolved`` (a withheld
    confirmation drops the confirmed date: the reader cannot see whether it agrees). None
    when no late line is left."""
    every = [c for line in facts["lines"] for c in (*line["date_cite"],
                                                    *line["quantity_cites"],
                                                    *line["item_cite"])]
    every += [f[1] for f in (facts["number"], facts["supplier"]) if f is not None]
    every += [c for _day_text, c in (*facts["confirmed"], *facts["corroborated"])]
    shown = {c[0] for c in every} if readable is None else readable(every)
    opens = lambda cites: bool(cites) and all(c[0] in shown for c in cites)  # noqa: E731
    lines = []
    for line in facts["lines"]:
        if not opens(line["date_cite"] + line["quantity_cites"]):
            continue
        lines.append({**line, "item": line["item"] if opens(line["item_cite"]) else None})
    if not lines:
        return None
    unresolved = list(facts["unresolved"])
    told: dict[str, Any] = {}
    for name in ("number", "supplier"):
        fact = facts[name]
        if fact is not None and not opens([fact[1]]):
            unresolved.append(f"{'order_number' if name == 'number' else name}:withheld")
            fact = None
        told[name] = None if fact is None else (fact[0], fact[1][0])
    confirmed = facts["confirmed"]
    told["confirmed"] = None
    told["internal_notes"] = []
    if confirmed and not opens([c for _d, c in confirmed]):
        unresolved.append("confirmed_date:withheld")
    elif len({d for d, _c in confirmed}) > 1:
        unresolved.append("confirmed_date:conflict")
    elif confirmed and "confirmed_date:unreadable" not in unresolved:
        told["confirmed"] = (confirmed[0][0], confirmed[0][1][0])
    corroborated = facts["corroborated"]
    if (corroborated and told["confirmed"] is None and len({d for d, _c in corroborated}) == 1
            and opens([c for _d, c in corroborated])):
        told["internal_notes"] = [{"note": "confirmed_date:corroborated_only",
                                   "value": corroborated[0][0],
                                   "citation": corroborated[0][1][0]}]
    told["unresolved"] = unresolved
    return late_order_draft(lines, told, roles)


def _amount(number: Decimal, unit: str | None) -> str:
    shown = format(number.normalize(), "f")
    return f"{shown} {pg._clip(' '.join(unit.split()), 20)}" if unit and unit.strip() else shown


def late_order_draft(lines: Sequence[Mapping[str, Any]], facts: Mapping[str, Any],
                     roles: Sequence[str]) -> dict[str, Any]:
    """The supplier message asking it to confirm delivery of one order's late lines, worded
    from cited facts only, never sent (``DRAFT_STATUS``, ``sent`` False). It names the
    purchase order, each late line (its item, ordered and received quantities) and the
    governing date: the supplier's confirmed delivery date when exactly one is readable
    (``date_basis`` ``confirmed``), else each line's expected receipt date worded "our
    expected date" (``expected``). Its citations are a side list: each stated fact one
    ``facts`` entry with its handle, no marker in the text; at most ``MAX_DRAFT_LINES``
    lines and ``MAX_DRAFT_CITATIONS`` handles (the catalog's draft citation bound)."""
    citations: list[str] = []
    listed: list[dict[str, Any]] = []

    def fact(name: str, value: str, handle: str, **extra: Any) -> None:
        if handle not in citations:
            citations.append(handle)
        listed.append({"fact": name, "value": value, **extra, "citation": handle})

    number, supplier, confirmed = facts.get("number"), facts.get("supplier"), \
        facts.get("confirmed")
    if supplier is not None:
        to = {"kind": "supplier", "name": supplier[0], "citation": supplier[1]}
        to_text = supplier[0]
        fact("supplier", supplier[0], supplier[1])
    else:
        role = roles[0] if roles else None
        to = {"kind": "answerer_role", "role": role,
              "reason": "the order's supplier name is not readable here"}
        to_text = (f"{role} (answerer role; the order's supplier name is not readable here)"
                   if role else "(no supplier name or answerer role is known)")
    if number is not None:
        fact("order_number", number[0], number[1])
        order_text, subject = (f"purchase order {number[0]}",
                               f"Please confirm the delivery date for purchase order {number[0]}")
    else:
        order_text, subject = ("our purchase order (its number is not readable here)",
                               "Please confirm the delivery date for our purchase order")
    if confirmed is not None:
        fact("confirmed_delivery_date", confirmed[0], confirmed[1])
        basis = (f"Your confirmation gives the delivery date {confirmed[0]}, and these lines "
                 "are not fully received:\n")
    else:
        basis = "These lines are past our expected date and not fully received:\n"
    head = (f"{DRAFT_LABEL}\nTo: {to_text}\nSubject: {subject}\n\nHello,\n\n"
            f"Please confirm the delivery date for {order_text}. {basis}")
    tail = "\nPlease reply with the date each line will be delivered."
    ordered = sorted(lines, key=lambda line: (line["day"], str(line.get("line_id"))))
    body, shown = "", 0
    for line in ordered[:MAX_DRAFT_LINES]:
        mark = len(listed), list(citations)
        line_id = str(line.get("line_id") or "")
        item = line.get("item")
        if item:
            fact("line_item", item[0], item[1], line_id=line_id)
        name = item[0] if item else "a line (the record names no item)"
        when = ""
        if confirmed is None:
            day = line["day"].isoformat()
            fact("expected_receipt", day, line["date_handle"], line_id=line_id)
            when = f"our expected date {day}, "
        (q_o, u_o, h_o), (q_r, u_r, h_r) = line["ordered"], line["received"]
        fact("ordered_quantity", format(q_o.normalize(), "f"), h_o, line_id=line_id, unit=u_o)
        fact("received_quantity", format(q_r.normalize(), "f"), h_r, line_id=line_id, unit=u_r)
        row = (f"- {name}: {when}ordered {_amount(q_o, u_o)}, "
               f"received {_amount(q_r, u_r)}\n")
        if (len(head) + len(body) + len(row) + len(tail) + 60 > MAX_DRAFT_TEXT
                or len(citations) > MAX_DRAFT_CITATIONS):
            del listed[mark[0]:]   # this line does not fit: neither its text nor its facts
            citations[:] = mark[1]
            break
        body += row
        shown += 1
    if shown < len(ordered):
        body += f"- and {len(ordered) - shown} more late lines, not listed here\n"
    return {"label": DRAFT_LABEL, "status": DRAFT_STATUS, "sent": False, "to": to,
            "subject": pg._clip(subject, 200), "text": pg._clip(head + body + tail,
                                                                 MAX_DRAFT_TEXT),
            "date_basis": "confirmed" if confirmed is not None else "expected",
            "internal_notes": [dict(n) for n in facts.get("internal_notes") or ()],
            "facts": listed, "citations": citations, "lines_late": len(ordered),
            "lines_listed": shown, "unresolved": list(facts.get("unresolved") or ())}


def _late_order(st: _Store, as_of: date) -> dict[str, Any]:
    """``late_order``: late lines (``pg._late_line``) grouped by their one order, each gap
    with its supplier draft (``late_order_draft``) as the store holds the facts."""
    name = "late_order"
    found = _late_orders(st, as_of)
    if found is None:
        return _result([], status="not_configured", reasons=["rule_not_declared"])
    orders, skipped = found
    gaps = []
    for facts in orders:
        rows = facts["lines"]
        groups = [[c for line in rows for c in line["evidence"][prop]]
                  for prop in (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED)]
        extra = [c for line in rows for c in line["item_cite"]]
        extra += [f[1] for f in (facts["number"], facts["supplier"]) if f is not None]
        extra += [c for _d, c in facts["confirmed"]]
        count = len(rows)
        noun = "line" if count == 1 else "lines"
        latest = max(line["day"] for line in rows).isoformat()
        gap = _gap(
            "late_order", name, facts["order"], facts["order_type"], "late_lines",
            summary=f"{count} {noun} expected by {latest} not fully received",
            missing="Confirm a new delivery date for the lines past their expected receipt",
            why=pg._clip(f"{count} {noun} of the order had an expected receipt date before "
                         f"the check date (latest {latest}) and a received quantity below "
                         "the ordered quantity, as cited."),
            priority="high", about=(pg.LINE_TYPE, pg.LINE_DATE), groups=groups,
            extra=list({c[0]: c for c in extra}.values()))
        if gap is not None:
            draft = _worded(facts, gap["follow_up"]["who_can_answer"])
            if draft is not None:
                gap["follow_up"]["draft"] = draft
            gaps.append(gap)
    return _result(gaps, not_evaluated=skipped)


def order_draft(store_path: Any, order_id: str, as_of: date, *,
                may_read: Callable[[str], bool], held: Iterable[str],
                decide: Callable[[Sequence[str]], Mapping[str, Any]]) -> dict[str, Any] | None:
    """The supplier draft of one order (``late_order_draft``), derived again from the store
    for one reader: a fact is stated only when its record sits in an audience
    ``may_read`` admits, at markings the reader ``held`` covers, and ``decide`` (None per
    handle it may open) lets it open, as ``read_inbox`` shows a gap. The console words a
    proposed draft with this, never from the text or answerers a client sent: the
    answerer roles are the gap's own (``pg._who``, as ``_gap`` resolves them). None when
    the order has no late line the reader may open whole (or the rule is not declared)."""
    st = _Store(Path(store_path))
    try:
        found = _late_orders(st, as_of, only=order_id)
        if not found or not found[0]:
            return None
        (facts,) = found[0]
        allowed = set(ostore.allowed_marking_sets(held))

        def readable(cites: Sequence[Cite]) -> set[str]:
            ids = sorted({c[1] for c in cites if isinstance(c[1], str)})
            seen: dict[str, bool] = {}
            for row in st.conn.execute(
                    "SELECT o.object_id, o.audience_key, v.markings FROM objects o JOIN "
                    "object_versions v ON v.object_id = o.object_id AND v.superseded_at IS "
                    "NULL WHERE o.object_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(ids),)):
                try:
                    ok = may_read(row["audience_key"]) is True
                except Exception:  # noqa: BLE001 - a failing grant check denies
                    ok = False
                seen[row["object_id"]] = (seen.get(row["object_id"], True) and ok
                                          and row["markings"] in allowed)
            open_ = [c for c in cites if seen.get(c[1]) is True and c[2] in allowed]
            handles = sorted({c[0] for c in open_})
            refused = {h for h, why in decide(handles).items() if why is not None} \
                if handles else set()
            return {c[0] for c in open_ if c[0] not in refused}

        first = facts["lines"][0]["evidence"][pg.LINE_DATE][0][0]
        roles = pg._who(facts["order_type"], {"about": (pg.LINE_TYPE, pg.LINE_DATE),
                                              "handles": [first]})
        return _worded(facts, roles, readable)
    finally:
        st.close()


def _lateness_audiences(st: _Store) -> list[str]:
    """Every audience a reader needs for ``plx.process`` to decide a purchase order's
    lateness for it, as the tool decides a source is withheld (a Portal relation's
    ``api:`` key, a BC relation's stored ``bc:<company>/`` keys or, none stored, the
    family-wide ``bc:*``; else ``<family>:*`` and the family's stored keys;
    item-ACL sources are unproven for every reader alike): over each binding the
    ``po_delivery_window`` graph names and each binding of the link types it walks, plus
    the audience of every stored record of those bindings."""
    from ontology.schema import load_schema
    from operating.ontology_tools import _AUDIENCE_SOURCE, _ITEM_ACL_AUDIENCES

    schema = load_schema()
    strings: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            strings.add(node)
        elif isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(LATENESS_GRAPH.read_text(encoding="utf-8")))
    bindings = {s for s in strings if s in schema.bindings}
    for link in (schema.link_types[s] for s in strings if s in schema.link_types):
        for lb in link.get("bindings", ()):
            bindings |= {lb.get("from_binding"), lb.get("to_binding"),
                         (lb.get("via") or {}).get("binding")}
    bindings.discard(None)
    row = st.meta("audience_keys")
    stored = set(json.loads(row)) if row else set()
    stored |= {r[0] for r in st.conn.execute("SELECT DISTINCT audience_key FROM objects")}
    prefix_of = {source: prefix for prefix, source in _AUDIENCE_SOURCE.items()}
    need: set[str] = set()
    for bid in sorted(bindings):
        if bid not in schema.bindings:
            raise ValueError(f"lateness_binding_unknown:{bid}")   # fail closed
        tid, binding = schema.bindings[bid]
        source, relation = binding["source"], binding["relation"]
        prefix = prefix_of.get(source)
        if source == "portal_api":
            need.add(f"api:{relation}")
        elif source == "bc":
            # per company where the store holds that relation's companies; else the tool
            # asks whether the grant covers the whole family: only a family-wide pattern
            # grants the literal ``bc:*`` key
            company = {k for k in stored if k.startswith("bc:") and k.endswith(f"/{relation}")}
            need |= company or {"bc:*"}
        elif prefix is not None and prefix not in _ITEM_ACL_AUDIENCES:
            need |= {f"{prefix}:*"} | {k for k in stored if k.startswith(f"{prefix}:")}
        prefix_id = f"{tid}:{bid}:"
        need |= {r[0] for r in st.conn.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_type = ? AND "
            "substr(object_id, 1, ?) = ?", (tid, len(prefix_id), prefix_id))}
    return sorted(need)


def _orders_at_risk(st: _Store, as_of: date, call: Callable | None, bound: int
                    ) -> dict[str, Any]:
    """``orders_at_risk``: one gap per customer order a late PO puts at risk, and per late
    PO one ``po_impact`` gap (the orders at risk it feeds, never money) and, when every one
    of those orders has a stated margin in one currency, one ``po_impact_margin`` gap with
    the margin at stake whose every figure is its own finance-marked citation group (only a
    reader holding finance, who may open every figure, sees it)."""
    if call is None:
        return _result([], status="not_configured", reasons=["tool_caller_not_given"])
    from . import impact_chain as ic

    found = ic.scan(st, call, bound=bound, margin_values=ic.store_margin_values(st),
                    landed_values=ic.investigate_landed_values(call))
    risky = found["assessment"].at_risk
    requires = _lateness_audiences(st) if risky else []
    late = found["late"]
    handles = {v["citation"]["l1_handle"] for order in risky
               for v in late[order.worst].cites}
    handles |= {ref for order in risky for ref in order.required_from}
    records = {r for order in risky for chain in order.chains if chain.po == order.worst
               for r in chain.records}
    by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    impact_versions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for version in st.versions(objects=records | {r for order in risky for chain in order.chains
                                                  for r in chain.records}):
        impact_versions[version["object_id"]].append(version)
        if version["object_id"] in records:
            by_object[version["object_id"]].append(version)
    handles |= {v["citation"]["l1_handle"] for order in risky for chain in order.chains
                for v in late[chain.po].cites}
    handles |= {r for margin in found["margins"].values() for f in margin.figures
                for r in (f.refs or (f.ref,))}
    held = {v["l1_handle"]: v for v in st.versions(handles=handles)}

    def cite_value(fig: Any) -> Cite:
        """A margin figure's citation at its value's own markings (finance) joined with its
        record's: a reader who does not hold finance never sees the money claim."""
        version = held.get(fig.ref)   # a handle the store does not hold: shown to nobody
        return ((fig.ref, version["object_id"], _markings(version["markings"], fig.markings))
                if version else (fig.ref, None, UNREADABLE))

    def cite(handle: str) -> Cite:
        version = held.get(handle)   # a handle the store does not hold: shown to nobody
        return ((handle, version["object_id"], _markings(version["markings"])) if version
                else (handle, None, UNREADABLE))

    gaps = []
    skipped: Counter[str] = Counter()
    for order in risky:
        chain = next(c for c in order.chains if c.po == order.worst)
        into: dict[str, list[str]] = defaultdict(list)
        for edge in chain.edges:
            into[edge.frm].append(edge.markings)
            into[edge.to].append(edge.markings)
        # each lateness value its own group: a reader who may not open one of them never
        # sees the verdict that rests on it
        groups = [[cite(v["citation"]["l1_handle"])] for v in late[order.worst].cites]
        groups += [[(v["l1_handle"], record, _markings(v["markings"], *into[record]))
                    for v in by_object.get(record, ())] for record in chain.records]
        groups.append([cite(ref) for ref in order.required_from])
        pos = len({c.po for c in order.chains})
        noun = "purchase order" if pos == 1 else "purchase orders"
        gap = _gap(
            "order_at_risk", "orders_at_risk", order.key, "SalesOrder", "at_risk",
            summary=f"{order.days} days at risk from {pos} late {noun} (days at risk is a "
                    "lower bound: no assembly lead time is recorded)",
            missing="Confirm whether the order's required date still holds, or when the "
                    "late purchase order's material will arrive",
            why=pg._clip(f"The item of a late purchase order is consumed by an assembly "
                         f"order made or allocated for this order (recorded links, each "
                         f"cited): material {order.material.isoformat()} "
                         f"({order.material_how}) against the required date "
                         f"{order.required.isoformat()}."),
            priority="high" if order.days >= 7 else "medium",
            about=("SalesOrder", "required_date"), groups=groups)
        if gap is None:
            skipped["order_at_risk_uncited"] += 1
        else:
            gap["requires"] = requires
            gaps.append(gap)
    by_key = {order.key: order for order in risky}
    for impact in found["po_impacts"]:
        orders = [by_key[k] for k in impact["orders"]]
        if not orders:
            continue
        po = impact["po"]
        # the PO's lateness values, then every record of each chain from it, each its own
        # group: a reader who may not open one never sees the claim
        groups = [[cite(v["citation"]["l1_handle"])] for v in late[po].cites]
        for order in orders:
            chain = next(c for c in order.chains if c.po == po)
            into: dict[str, list[str]] = defaultdict(list)
            for edge in chain.edges:
                into[edge.frm].append(edge.markings)
                into[edge.to].append(edge.markings)
            groups += [[(v["l1_handle"], record, _markings(v["markings"], *into[record]))
                        for v in impact_versions.get(record, ())] for record in chain.records]
            groups.append([cite(ref) for ref in order.required_from])
        n = len(orders)
        noun = "sales order" if n == 1 else "sales orders"
        worst = max(o.days for o in orders)
        missing = ("Confirm when the late purchase order's material will arrive, or whether "
                   "the sales orders' required dates still hold")
        why = pg._clip(f"An item of this purchase order is consumed by assembly orders made or "
                       f"allocated for {n} {noun} at risk (recorded links, each cited); the "
                       f"worst is {worst} days at risk (a lower bound: no assembly lead time is "
                       "recorded).")
        priority = "high" if worst >= 7 else "medium"
        plain = _gap("po_impact", "orders_at_risk", po, "PurchaseOrder", "impact",
                     summary=f"affects {n} {noun}, worst {worst} days at risk",
                     missing=missing, why=why, priority=priority,
                     about=("PurchaseOrder", "lateness"), groups=groups)
        if plain is None:
            skipped["po_impact_uncited"] += 1
            continue
        plain["requires"] = requires
        gaps.append(plain)
        margin = impact["margin"]
        if margin is None or impact["orders_with_margin"] != n:
            continue   # the money claim needs every order's margin: never a part as the whole
        figures = [f for order in orders for f in found["margins"][order.key].figures]
        # a landed-cost engine figure: each cited value it was computed from is its own
        # finance-marked group (a reader who may not open one never sees the claim)
        value_groups = [[cite_value(f)] if not f.refs else [cite_value(ic.Figure(
            f.amount, f.currency, f.unit, ref, f.object_id, f.markings))] for f in figures
            for ref in (f.refs or (f.ref,))]
        money = _gap("po_impact_margin", "orders_at_risk", po, "PurchaseOrder", "margin",
                     summary=f"affects {n} {noun}, {margin['amount']} {margin['currency']} "
                             f"margin at stake ({margin['basis']}), worst {worst} days at "
                             "risk",
                     missing=missing, why=pg._clip(why + " " + ic.MARGIN_BASIS + "."),
                     priority=priority, about=("PurchaseOrder", "lateness"),
                     groups=groups + value_groups)
        if money is None:
            skipped["po_impact_margin_uncited"] += 1
        else:
            money["requires"] = requires
            gaps.append(money)
    if found["lateness_undetermined"]:
        skipped["lateness_undetermined"] = found["lateness_undetermined"]
    if found["pos_not_checked"]:
        skipped["po_not_checked"] = found["pos_not_checked"]
    result = _result(gaps, status="truncated" if found["truncated"] else "complete",
                     reasons=["impact_bound"] if found["pos_not_checked"] else [],
                     not_evaluated=skipped)
    result["finding"] = {"orders_at_risk": len(risky),
                         "late_pos": found["late_pos_with_orders_at_risk"],
                         "text": ic.headline(len(risky),
                                             found["late_pos_with_orders_at_risk"])}
    return result


def _card_rules(st: _Store, as_of: date, call: Callable | None, bound: int) -> dict[str, Any]:
    """``missing_in_record`` and attested conflicts on the attesting documents' cards."""
    if call is None:
        return _result([], status="not_configured", reasons=["tool_caller_not_given"])
    documents = [name for name, _field in pg._roster()[0]]
    ids = [row[0] for row in st.conn.execute(
        "SELECT o.object_id FROM objects o WHERE o.object_type IN (SELECT value FROM "
        "json_each(?)) AND NOT " + ostore.removed_at_sql("o.object_id")
        + " ORDER BY o.first_recorded_at DESC, o.object_id LIMIT ?",
        (json.dumps(documents), bound + 1))]
    unread: Counter[str] = Counter()
    skipped = _Skips()
    found: list[tuple[dict[str, Any], list[list[str]]]] = []
    for object_id in ids[:bound]:
        answer = call("plx.object", {"object_id": object_id, "response_format": "detailed"})
        error = answer.get("error") if isinstance(answer, Mapping) else {"code": "error"}
        if error:
            code = error.get("code") if isinstance(error, Mapping) else None
            unread[code if isinstance(code, str) else "error"] += 1
            skipped.note(f"unread_{code if isinstance(code, str) else 'error'}", [object_id])
            continue
        conflicts = answer.get("conflicts") if isinstance(answer.get("conflicts"), list) else []
        card = {"object": answer.get("object"), "leads": answer.get("leads"), "values": [],
                "conflicts": [c for c in conflicts if isinstance(c, Mapping) and c.get("tier")]}
        found.extend(pg._detect(card, as_of))
    held = {v["l1_handle"]: v for v in st.versions(handles={
        h for gap, requires in found for h in gap["citations"]})}

    def cite(handle: str) -> Cite:
        version = held.get(handle)   # a handle the store does not hold: shown to nobody
        return ((handle, version["object_id"], _markings(version["markings"])) if version
                else (handle, None, UNREADABLE))

    gaps = []
    for gap, requires in found:
        follow = gap["follow_up"]
        gaps.append(_gap(
            gap["kind"], "card_rules", gap["object_id"], gap["object_type"],
            gap["id"], summary=gap["summary"], missing=follow["missing"], why=follow["why"],
            priority=follow["priority"], about=(gap["object_type"], ""),
            groups=[[cite(h) for h in group] for group in requires],
            extra=[cite(h) for h in gap["citations"]]))
    status = "truncated" if len(ids) > bound or unread else "complete"
    return _result(gaps, status=status,
                   reasons=(["card_bound"] if len(ids) > bound else [])
                   + [f"unread:{code}" for code in sorted(unread)],
                   not_evaluated=skipped)


# --------------------------------------------------------------------------- the inbox

_INBOX_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
  as_of TEXT NOT NULL, store_digest TEXT, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scope (audience_key TEXT NOT NULL, markings TEXT NOT NULL,
  objects INTEGER NOT NULL, PRIMARY KEY (audience_key, markings));
CREATE TABLE IF NOT EXISTS detector_runs (detector TEXT PRIMARY KEY, status TEXT NOT NULL,
  reasons TEXT NOT NULL, found INTEGER NOT NULL, not_evaluated TEXT NOT NULL,
  run_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rule_coverage (detector TEXT PRIMARY KEY,
  unattributed INTEGER NOT NULL, run_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unjudged_causes (detector TEXT NOT NULL, skip INTEGER NOT NULL,
  object_id TEXT NOT NULL, markings TEXT NOT NULL, subject INTEGER NOT NULL,
  PRIMARY KEY (detector, skip, object_id, markings));
CREATE TABLE IF NOT EXISTS gaps (gap_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  detector TEXT NOT NULL, object_id TEXT NOT NULL, object_type TEXT NOT NULL,
  summary TEXT NOT NULL, follow_up TEXT NOT NULL, priority INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'resolved')), first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, resolved_at TEXT);
CREATE INDEX IF NOT EXISTS gaps_open ON gaps (state, priority, last_seen);
CREATE TABLE IF NOT EXISTS gap_cites (gap_id TEXT NOT NULL, grp INTEGER NOT NULL,
  handle TEXT NOT NULL, object_id TEXT, markings TEXT NOT NULL,
  PRIMARY KEY (gap_id, grp, handle));
CREATE TABLE IF NOT EXISTS gap_requires (gap_id TEXT NOT NULL, audience_key TEXT NOT NULL,
  PRIMARY KEY (gap_id, audience_key));
CREATE TABLE IF NOT EXISTS acks (gap_id TEXT NOT NULL, reader TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (gap_id, reader));
"""


def inbox_path(store_path: Any) -> Path:
    """The inbox file of a projection store: beside it, named after it."""
    store = Path(store_path)
    return store.with_name(store.name + ".gap-inbox.sqlite")


def reader_key(actor: Any) -> str:
    """The reader an acknowledgement belongs to: tenant and sorted principals."""
    return f"{actor.tenant}:" + ",".join(sorted(actor.principals))


def _open_inbox(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_INBOX_DDL)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)",
                     (INBOX_SCHEMA,))
    elif row[0] != INBOX_SCHEMA:
        conn.close()
        raise ValueError("gap_inbox_schema_mismatch")
    return conn


class TimeBudgetSpent(Exception):
    """A tool call refused because the run's time budget is spent."""


class Budget:
    """A run's time budget (``seconds`` from now on ``monotonic``): ``guard`` wraps a tool
    caller so that once it is spent every call raises ``TimeBudgetSpent`` and is noted
    (``refused``), so a detector that swallows a failing call is still not complete."""

    def __init__(self, seconds: float, monotonic: Callable[[], float] = time.monotonic
                 ) -> None:
        self.monotonic = monotonic
        self.deadline = monotonic() + seconds
        self.refused = 0

    def spent(self) -> bool:
        return self.monotonic() >= self.deadline

    def guard(self, call: Callable | None) -> Callable | None:
        if call is None:
            return None

        def guarded(tool: str, arguments: dict) -> Any:
            if self.spent():
                self.refused += 1
                raise TimeBudgetSpent(tool)
            return call(tool, arguments)
        return guarded


def run(store_path: Any, inbox: Any, as_of: date | None = None, *,
        coverage: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
        call: Callable | None = None, card_bound: int = CARD_BOUND,
        qc_link_types: Sequence[str] | None = None, budget: Budget | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict[str, Any]:
    """Run every detector over the store and fold the gaps into the inbox; the summary
    (counts and enums only). A detector that fails is ``failed`` and leaves its gaps as
    they were; only a ``complete`` detector resolves the gaps it no longer finds. The
    run's day (``as_of``, what late and days late are counted to) defaults to the
    business day (ET) of ``clock()`` (``po_status.business_today``), never its UTC day.
    ``coverage`` may be a callable, scanned at its detector's turn (its failure fails that
    detector alone). With a ``budget``, a detector not started before it is spent, or one
    whose tool call it refused, is ``truncated`` (``time_budget``)."""
    moment = clock()
    as_of = po_status.business_today(moment) if as_of is None else as_of
    if type(as_of) is not date:   # a datetime or text is no day: refused, never coerced
        raise TypeError("as_of_must_be_a_date")
    run_at = moment.isoformat()
    call = call if budget is None else budget.guard(call)
    st = _Store(Path(store_path))
    try:
        steps: dict[str, Callable[[], dict[str, Any]]] = {
            "late_or_unconfirmed_po": lambda: _late_or_unconfirmed_po(st, as_of),
            "po_receipt_quantity_mismatch": lambda: _po_receipt_quantity_mismatch(st, as_of),
            "receipt_without_qc": lambda: _receipt_without_qc(
                st, as_of, declared_qc_links() if qc_link_types is None else qc_link_types),
            "order_without_customer_po": lambda: _order_without_customer_po(
                st, coverage() if callable(coverage) else coverage),
            "expired_quote": lambda: _expired_quote(st, as_of),
            "conflicting_quantity": lambda: _conflicting_quantity(st, as_of),
            "late_order": lambda: _late_order(st, as_of),
            "orders_at_risk": lambda: _orders_at_risk(st, as_of, call, card_bound),
            "card_rules": lambda: _card_rules(st, as_of, call, card_bound)}
        results: dict[str, dict[str, Any]] = {}
        for name in DETECTORS:
            if budget is not None and budget.spent():
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
                continue
            refused = budget.refused if budget is not None else 0
            try:
                results[name] = steps[name]()
            except TimeBudgetSpent:
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
            except Exception as error:  # noqa: BLE001 - one detector fails alone, typed
                results[name] = _result([], status="failed", reasons=[type(error).__name__])
            if budget is not None and budget.refused > refused \
                    and results[name]["status"] != "failed":
                # a call it made was refused: what it found stands, never all it would find
                results[name]["status"] = "truncated"
                if TIME_BUDGET not in results[name]["reasons"]:
                    results[name]["reasons"].append(TIME_BUDGET)
        scope = st.scope()
        digest = st.meta("content_digest")
    finally:
        st.close()
    gaps: dict[str, dict[str, Any]] = {}
    for result in results.values():
        for gap in result["gaps"]:
            gaps.setdefault(gap["id"], gap)
    summary = _summary(results, gaps, scope)
    with closing(_open_inbox(Path(inbox))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _fold(conn, results, gaps, scope, run_at, as_of, digest, summary)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return summary


def _summary(results: Mapping[str, Mapping[str, Any]], gaps: Mapping[str, Mapping[str, Any]],
             scope: Sequence[tuple[str, str, int]]) -> dict[str, Any]:
    cited = [g for g in gaps.values() if all(g["groups"])]
    return {"scanned": sum(n for _, _, n in scope),
            "scope_truncated": any(results[d]["status"] in ("truncated", "failed")
                                   for d in SET_BASED),
            "truncated": any(r["status"] in ("truncated", "failed") for r in results.values()),
            "gaps_found": len(gaps), "gaps_cited": len(cited),
            "gaps_uncited": len(gaps) - len(cited),
            "with_follow_up": sum(1 for g in gaps.values() if g["follow_up"]["who_can_answer"]),
            "gaps_by_kind": dict(sorted(Counter(g["kind"] for g in gaps.values()).items())),
            "detectors": {name: results[name]["status"] for name in DETECTORS},
            "detector_reasons": {name: results[name]["reasons"] for name in DETECTORS
                                 if results[name]["reasons"]},
            "not_evaluated": {name: results[name]["not_evaluated"] for name in DETECTORS
                              if results[name]["not_evaluated"]},
            "po_lateness": results["late_or_unconfirmed_po"].get("counts"),
            "top_finding": results["orders_at_risk"].get("finding")}


def _fold(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
          gaps: Mapping[str, Mapping[str, Any]], scope: Sequence[tuple[str, str, int]],
          run_at: str, as_of: date, digest: str | None, summary: Mapping[str, Any]) -> None:
    conn.execute("INSERT INTO runs (run_at, as_of, store_digest, summary) VALUES (?, ?, ?, ?)",
                 (run_at, as_of.isoformat(), digest, json.dumps(summary, sort_keys=True)))
    conn.execute("DELETE FROM runs WHERE run_id NOT IN (SELECT run_id FROM runs "
                 "ORDER BY run_id DESC LIMIT ?)", (KEEP_RUNS,))
    conn.execute("DELETE FROM scope")
    conn.executemany("INSERT INTO scope VALUES (?, ?, ?)", scope)
    for name, result in results.items():
        if result["status"] == "not_configured" and conn.execute(
                "SELECT 1 FROM detector_runs WHERE detector = ?", (name,)).fetchone():
            continue   # a run without this detector's input says nothing about its gaps
        conn.execute(
            "INSERT INTO detector_runs VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (detector) DO "
            "UPDATE SET status = excluded.status, reasons = excluded.reasons, found = "
            "excluded.found, not_evaluated = excluded.not_evaluated, run_at = excluded.run_at",
            (name, result["status"], json.dumps(result["reasons"]), len(result["gaps"]),
             json.dumps(result["not_evaluated"], sort_keys=True), run_at))
        # The records this run's rule left unjudged with each skip's cause, replacing the
        # last run's: a reader is told the count of those it may read whose whole cause it
        # may read (``read_inbox``), never of the others.
        conn.execute("DELETE FROM unjudged_causes WHERE detector = ?", (name,))
        conn.executemany(
            "INSERT OR IGNORE INTO unjudged_causes VALUES (?, ?, ?, ?, ?)",
            [(name, n, object_id, markings, int(object_id in skip["subjects"]))
             for n, skip in enumerate(result.get("unjudged") or ())
             for object_id, markings in skip["cause"]])
        conn.execute(
            "INSERT INTO rule_coverage VALUES (?, ?, ?) ON CONFLICT (detector) DO UPDATE SET "
            "unattributed = excluded.unattributed, run_at = excluded.run_at",
            (name, int(result.get("unattributed") or 0), run_at))
    for gap in gaps.values():
        before = conn.execute("SELECT state FROM gaps WHERE gap_id = ?", (gap["id"],)).fetchone()
        if before is not None and before["state"] == "resolved":
            conn.execute("DELETE FROM acks WHERE gap_id = ?", (gap["id"],))   # reopened
        elif before is not None and gap["kind"] in WHOLE_ONLY \
                and results[gap["detector"]]["status"] != "complete":
            # a money claim summed over a run that did not finish may be a part: the open
            # gap keeps the wording and citations of the last run that did
            conn.execute("UPDATE gaps SET last_seen = ? WHERE gap_id = ?", (run_at, gap["id"]))
            continue
        conn.execute(
            "INSERT INTO gaps VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL) "
            "ON CONFLICT (gap_id) DO UPDATE SET summary = excluded.summary, follow_up = "
            "excluded.follow_up, priority = excluded.priority, state = 'open', last_seen = "
            "excluded.last_seen, resolved_at = NULL",
            (gap["id"], gap["kind"], gap["detector"], gap["object_id"], gap["object_type"],
             gap["summary"], json.dumps(gap["follow_up"], sort_keys=True),
             PRIORITY.index(gap["follow_up"]["priority"]), run_at, run_at))
        conn.execute("DELETE FROM gap_cites WHERE gap_id = ?", (gap["id"],))
        rows = {(grp, c[0]): (gap["id"], grp, c[0], c[1], c[2])
                for grp, group in enumerate(gap["groups"]) for c in group}
        for c in gap["extra"]:
            rows.setdefault((-1, c[0]), (gap["id"], -1, c[0], c[1], c[2]))
        conn.executemany("INSERT INTO gap_cites VALUES (?, ?, ?, ?, ?)", rows.values())
        conn.execute("DELETE FROM gap_requires WHERE gap_id = ?", (gap["id"],))
        conn.executemany("INSERT INTO gap_requires VALUES (?, ?)",
                         [(gap["id"], key) for key in sorted(set(gap.get("requires", ())))])
    complete = [name for name, result in results.items() if result["status"] == "complete"]
    conn.execute(
        "UPDATE gaps SET state = 'resolved', resolved_at = ? WHERE state = 'open' "
        "AND detector IN (SELECT value FROM json_each(?)) "
        "AND gap_id NOT IN (SELECT value FROM json_each(?))",
        (run_at, json.dumps(complete), json.dumps(sorted(gaps))))


def _reader_view(conn: sqlite3.Connection, may_read: Callable[[str], bool],
                 held: Iterable[str]) -> tuple[str, str]:
    """(visible audience keys, allowed markings values) as JSON, from the store's keys."""
    row = conn.execute("SELECT value FROM st.store_meta WHERE key = 'audience_keys'").fetchone()
    keys = json.loads(row[0]) if row is not None else None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        keys = [r[0] for r in conn.execute("SELECT DISTINCT audience_key FROM st.objects")]
    if _has_requires(conn):   # a gap's required audiences, held by no record maybe
        keys = list(dict.fromkeys([*keys, *(r[0] for r in conn.execute(
            "SELECT DISTINCT audience_key FROM gap_requires ORDER BY audience_key"))]))
    visible = []
    for key in keys:
        try:
            if may_read(key) is True:
                visible.append(key)
        except Exception:  # noqa: BLE001 - a failing grant check denies
            continue
    return json.dumps(visible), json.dumps(ostore.allowed_marking_sets(held))


def _has_requires(conn: sqlite3.Connection) -> bool:
    """An inbox written before ``gap_requires`` has none: its ``REQUIRES_GRANT`` gaps
    then show to nobody until the next run records their requirements."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                        "'gap_requires'").fetchone() is not None


_OBJECT_VISIBLE = (
    "EXISTS (SELECT 1 FROM st.objects so JOIN st.object_versions sv ON sv.object_id = "
    "so.object_id AND sv.superseded_at IS NULL WHERE so.object_id = {o} "
    "AND so.audience_key IN (SELECT value FROM json_each(:aud)) "
    "AND sv.markings IN (SELECT value FROM json_each(:marks)))")


def _visible(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], params: Mapping[str, str],
             decide: Callable[[Sequence[str]], Mapping[str, Any]]
             ) -> list[tuple[sqlite3.Row, list[str]]]:
    """Each gap the reader may see, with the citations it may open."""
    ids = json.dumps([row["gap_id"] for row in rows])
    cites: dict[str, list[tuple[int, str]]] = defaultdict(list)
    groups: dict[str, set[int]] = defaultdict(set)
    for cite in conn.execute(
            "SELECT gc.gap_id, gc.grp, gc.handle, gc.object_id IS NOT NULL AND gc.markings IN "
            "(SELECT value FROM json_each(:marks)) AND "
            + _OBJECT_VISIBLE.format(o="gc.object_id")
            + " AS shown FROM gap_cites gc WHERE gc.gap_id IN (SELECT value FROM "
            "json_each(:ids)) ORDER BY gc.gap_id, gc.grp, gc.handle", {**params, "ids": ids}):
        if cite["grp"] >= 0:
            groups[cite["gap_id"]].add(cite["grp"])
        if cite["shown"]:
            cites[cite["gap_id"]].append((cite["grp"], cite["handle"]))
    requires: dict[str, set[str]] = defaultdict(set)
    if _has_requires(conn):
        for req in conn.execute(
                "SELECT gap_id, audience_key FROM gap_requires WHERE gap_id IN (SELECT value "
                "FROM json_each(:ids))", {"ids": ids}):
            requires[req["gap_id"]].add(req["audience_key"])
    granted = set(json.loads(params["aud"]))
    handles = sorted({h for found in cites.values() for _, h in found})
    refused = {h for h, why in decide(handles).items() if why is not None} if handles else set()
    out = []
    for row in rows:
        open_ = [(grp, h) for grp, h in cites.get(row["gap_id"], []) if h not in refused]
        need = groups.get(row["gap_id"], set())
        required = requires.get(row["gap_id"], set())
        if row["detector"] in REQUIRES_GRANT and not (required and required <= granted):
            continue   # a verdict on what a source does not hold: the run's grant only
        if need and need <= {grp for grp, _ in open_}:
            out.append((row, list(dict.fromkeys(h for _, h in open_))))
    return out


def _attach(path: Path, store: Path, *, write: bool = False) -> sqlite3.Connection:
    mode = "rw" if write else "ro"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, isolation_level=None,
                           timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if not write:
            conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != INBOX_SCHEMA:
            raise ValueError("gap_inbox_schema_mismatch")
        if not Path(store).is_file():
            raise ostore.StoreError("store_missing")
        conn.execute("ATTACH DATABASE ? AS st", (f"file:{store}?mode=ro",))
        row = conn.execute("SELECT value FROM st.store_meta WHERE key = 'format'").fetchone()
        if row is None or row[0] != ostore.FORMAT:
            raise ostore.StoreError("store_format_mismatch")
    except BaseException:
        conn.close()
        raise
    return conn


def _rule_coverage(conn: sqlite3.Connection, params: Mapping[str, str],
                   detectors: Mapping[str, str]) -> list[dict[str, Any]]:
    """Per rule, its last run's status and how many records this reader may read it left
    unjudged (``not_evaluated``): counted over the records the reader's grant shows, of
    the skips whose whole cause (every record at its markings) the reader may read, never
    the store's: a skip a hidden record or link caused is not counted, so the count never
    hints at it. None (unknown, never 0) when the rule left a skip no record stands for,
    or the inbox predates per-skip causes while its run counted skips."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    recorded: dict[str, int] = {}
    visible: dict[str, int] = {}
    if {"rule_coverage", "unjudged_causes"} <= tables:
        recorded = {row[0]: row[1] for row in conn.execute(
            "SELECT detector, unattributed FROM rule_coverage")}
        visible = {row[0]: row[1] for row in conn.execute(
            "SELECT u.detector, count(DISTINCT u.object_id) FROM unjudged_causes u "
            "WHERE u.subject = 1 AND NOT EXISTS (SELECT 1 FROM unjudged_causes c WHERE "
            "c.detector = u.detector AND c.skip = u.skip AND NOT (c.markings IN (SELECT "
            "value FROM json_each(:marks)) AND " + _OBJECT_VISIBLE.format(o="c.object_id")
            + ")) GROUP BY u.detector", params)}
    counted = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, not_evaluated FROM detector_runs")}
    said = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, reasons FROM detector_runs")}
    out = []
    for name in DETECTORS:
        try:
            skips = sum(int(v) for v in json.loads(counted.get(name) or "{}").values())
        except (ValueError, TypeError, AttributeError):
            skips = 1   # unreadable: never read as nothing skipped
        if name not in recorded:
            shown = None if skips else 0
        else:
            shown = None if recorded[name] else visible.get(name, 0)
        try:
            given = json.loads(said.get(name) or "[]")
            reasons = [r for r in given if isinstance(r, str)] if isinstance(given, list) else []
        except (ValueError, TypeError):
            reasons = []    # unreadable: no reason is said, never a made-up one
        out.append({"rule": name, "status": detectors.get(name, "not_run"),
                    "not_evaluated": shown, "reasons": reasons})
    return out


def read_inbox(inbox: Any, store: Any, *, reader: str, may_read: Callable[[str], bool],
               held: Iterable[str], decide: Callable[[Sequence[str]], Mapping[str, Any]],
               states: Sequence[str] = ("open", "acknowledged"),
               limit: int = MAX_SHOWN) -> dict[str, Any]:
    """The gaps one reader may see, trimmed: every late order first (longest late first), then
    by priority; what the last run
    covered of what this reader may read. ``may_read(audience_key)``, ``held`` (the
    markings the reader holds) and ``decide(handles)`` (None per handle it may open)
    are the reader's grant; anything they refuse is never shown nor counted. ``complete``
    is true only when every rule ran to the end; ``not_run`` names each rule that did not
    (not configured on this schedule, blocked, or never run), so no reader takes an
    inbox some rules never looked at for a clean result."""
    path = Path(inbox)
    if not path.is_file():
        return {"status": "not_scheduled", "gaps": [], "scanned": 0, "truncated": False,
                "max_objects": None, "unscanned": None, "unread": {}, "complete": False,
                "not_run": {name: "not_run" for name in DETECTORS}}
    unknown = set(states) - set(STATES)
    if unknown:
        raise ValueError("gap_state_unknown")
    with closing(_attach(path, Path(store))) as conn:
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks}
        last = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        digest = conn.execute(
            "SELECT value FROM st.store_meta WHERE key = 'content_digest'").fetchone()
        scanned = conn.execute(
            "SELECT COALESCE(SUM(objects), 0) FROM scope WHERE audience_key IN (SELECT value "
            "FROM json_each(:aud)) AND markings IN (SELECT value FROM json_each(:marks))",
            params).fetchone()[0]
        detectors = {row["detector"]: row["status"] for row in conn.execute(
            "SELECT detector, status FROM detector_runs ORDER BY detector")}
        stored = {"open"} if set(states) & {"open", "acknowledged"} else set()
        stored |= {"resolved"} & set(states)
        global_states = json.dumps(sorted(stored))
        shown: list[dict[str, Any]] = []
        more = False
        orders: dict[str, tuple[Any, dict[str, Any]]] = {}   # order key -> (rank, entry)
        offset, page = 0, max(limit + 1, 200)
        while not more:
            rows = conn.execute(
                "SELECT g.*, (SELECT at FROM acks a WHERE a.gap_id = g.gap_id AND a.reader = "
                ":reader) AS acked FROM gaps g WHERE g.state IN (SELECT value FROM "
                "json_each(:states)) AND " + _OBJECT_VISIBLE.format(o="g.object_id")
                + " ORDER BY json_extract(g.follow_up, '$.days_late') IS NULL, "
                "json_extract(g.follow_up, '$.days_late') DESC, "
                "g.priority, g.last_seen DESC, g.gap_id LIMIT :page OFFSET :offset",
                {**params, "reader": reader, "states": global_states, "page": page,
                 "offset": offset}).fetchall()
            if not rows:
                break
            offset += len(rows)
            for row, citations in _visible(conn, rows, params, decide):
                state = ("resolved" if row["state"] == "resolved"
                         else "acknowledged" if row["acked"] else "open")
                if state not in states:
                    continue
                follow_up = json.loads(row["follow_up"])
                order = follow_up.pop(_ORDER, None)
                held = orders.get(order["key"]) if isinstance(order, dict) else None
                if held is not None:
                    # this order is listed: its governing record's item replaces a twin's,
                    # never a second item (each was decided visible to this reader)
                    if order["rank"] < held[0]:
                        index = next(i for i, g in enumerate(shown) if g is held[1])
                        shown[index] = entry = _shown(row, citations, state, follow_up)
                        orders[order["key"]] = (order["rank"], entry)
                    continue
                if len(shown) >= limit:
                    more = True
                    break
                entry = _shown(row, citations, state, follow_up)
                shown.append(entry)
                if isinstance(order, dict):
                    orders[order["key"]] = (order["rank"], entry)
        rules = _rule_coverage(conn, params, detectors)
    failed = sorted(name for name, status in detectors.items() if status == "failed")
    truncated = any(status == "truncated" for status in detectors.values())
    # A rule that did not run (not configured on this schedule, blocked on a source, or
    # with no row at all) looked for nothing: the read is never complete while one remains.
    not_run = {name: detectors.get(name, "not_run") for name in DETECTORS
               if detectors.get(name) not in ("complete", "truncated", "failed")}
    return {"status": "read", "as_of": last["as_of"] if last else None,
            "run_at": last["run_at"] if last else None,
            "stale": bool(last) and (digest[0] if digest else None) != last["store_digest"],
            "scanned": scanned, "truncated": truncated or bool(failed),
            "max_objects": None, "unscanned": None,
            "unread": {f"{name}_failed": 1 for name in failed},
            "detectors": detectors, "not_run": not_run, "rules": rules,
            "complete": not (truncated or failed or not_run),
            "gaps": shown, "more": more, "limit": limit}


def _shown(row: Mapping[str, Any], citations: Sequence[str], state: str,
           follow_up: dict[str, Any]) -> dict[str, Any]:
    """One inbox row as its reader sees it (``follow_up`` without the order field)."""
    days_late = follow_up.pop("days_late", None)
    draft = follow_up.get("draft")
    if isinstance(draft, dict) and not set(draft.get("citations") or ()) <= set(citations):
        follow_up.pop("draft")   # a fact it states is not this reader's to open
    return {"id": row["gap_id"], "kind": row["kind"],
            "object_id": row["object_id"], "object_type": row["object_type"],
            "summary": row["summary"], "citations": citations,
            "detected_by": "rule", "state": state,
            "first_seen": row["first_seen"], "last_seen": row["last_seen"],
            **({"days_late": days_late} if days_late is not None else {}),
            "follow_up": follow_up}


def acknowledge(inbox: Any, store: Any, gap_id: str, *, reader: str,
                may_read: Callable[[str], bool], held: Iterable[str],
                decide: Callable[[Sequence[str]], Mapping[str, Any]],
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
                ) -> dict[str, Any]:
    """Record that this reader saw an open gap; a gap it may not see is refused alike
    whether it exists or not."""
    path = Path(inbox)
    if not isinstance(gap_id, str) or not path.is_file():
        raise PermissionError("gap_not_visible")
    with closing(_attach(path, Path(store), write=True)) as conn:
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks}
        rows = conn.execute(
            "SELECT g.* FROM gaps g WHERE g.gap_id = :id AND g.state = 'open' AND "
            + _OBJECT_VISIBLE.format(o="g.object_id"), {**params, "id": gap_id}).fetchall()
        if not _visible(conn, rows, params, decide):
            raise PermissionError("gap_not_visible")
        at = clock().isoformat()
        conn.execute("INSERT OR IGNORE INTO acks VALUES (?, ?, ?)", (gap_id, reader, at))
    return {"id": gap_id, "state": "acknowledged"}


def refresh(runtime: Any, investigator: Any, audience: Any, projection: Path, as_of: date,
            *, captures: Path | None, scan_coverage: bool = True,
            budget: Budget | None = None,
            clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
            ) -> dict[str, Any]:
    """The full scheduled run over ``projection`` into its inbox, as ``investigator``: the
    customer PO coverage scan of the runtime's mirror (``scan_coverage``; its order
    statuses ``absent_in_this_source``) and a case session over the projection and the
    state directory's captures as the tool caller (``orders_at_risk``, ``card_rules``).
    The inbox stores each gap with its citations' audiences; ``read_inbox`` trims it per
    reader. Shared by ``main`` and the host's ``after_build(scheduled=)``."""
    from . import case_workspace_live as live
    from . import customer_po_coverage

    def coverage() -> Mapping[str, Any]:
        return customer_po_coverage.coverage(runtime.mirror, investigator,
                                             statuses=("absent_in_this_source",))

    session = live._Session(runtime, investigator, audience, projection, captures=captures)
    try:
        return run(projection, inbox_path(projection), as_of,
                   coverage=coverage if scan_coverage else None, call=session,
                   budget=budget, clock=clock)
    finally:
        session.close()


def after_build(store_path: Any, *, as_of: date | None = None,
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                scheduled: Callable[[date, Budget], Mapping[str, Any]] | None = None,
                budget_seconds: float | None = None,
                monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The host's hook after a projection build, never raising; counts and enums only.
    ``scheduled(as_of, budget)``: the full run (``refresh``) within a ``budget_seconds``
    budget; without it the store-only detectors (no mirror scan, no cards). Judged on
    ``as_of``, else the business day (ET) of ``clock()``."""
    try:
        if scheduled is None:
            found = run(store_path, inbox_path(store_path), as_of, clock=clock)
        else:
            found = scheduled(po_status.business_today(clock()) if as_of is None else as_of,
                              Budget(REFRESH_BUDGET_SECONDS if budget_seconds is None
                                     else budget_seconds, monotonic))
    except Exception as error:  # noqa: BLE001 - detection never fails a capture cycle
        return {"state": "failed", "error": type(error).__name__}
    return {"state": "ran", "gaps_found": found["gaps_found"], "detectors": found["detectors"],
            **({"detector_reasons": found["detector_reasons"]}
               if found.get("detector_reasons") else {})}


# --------------------------------------------------------------------------- live command

def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None, graph_sender: Any = None) -> int:
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor
    from operator_ui.object_workspace import HostAccess

    from . import case_workspace_live as live

    parser = argparse.ArgumentParser(prog="python3 -m operating.proactive_engine",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True,
                        help="the investigator's principals (repeat)")
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--reader", action="append", default=[], metavar="P1,P2",
                        help="another reader whose inbox is counted (repeat)")
    parser.add_argument("--host-config", type=Path, default=None,
                        help="run one host capture cycle into --state-dir first")
    parser.add_argument("--live-source-audience", type=Path, default=None)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        audience = AudienceMap.from_file(args.mirror_audience)
        readers = [live._reader(text, args.tenant, args.clearance) for text in args.reader]
    except (OSError, ValueError) as error:
        parser.error(f"invalid input: {type(error).__name__}")
    if audience.tenant != args.tenant:
        parser.error("mirror audience tenant must equal --tenant")
    as_of = args.as_of or po_status.business_today()
    summary: dict[str, Any] = {"schema": SCHEMA, "evidence": live.MIRROR_EVIDENCE["label"],
                               "as_of": as_of.isoformat(), "capture": None}
    if args.host_config is not None:
        code, summary["capture"] = live._host_cycle(args, env, host_options or {})
        if code != 0:
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    investigator = Actor(args.tenant, frozenset(args.principal), args.clearance)
    readers_live = None
    if args.live_source_audience is not None:
        from .case_runtime import build_live_readers
        from .ingestion import IngestError, load_live_audience
        try:
            readers_live = build_live_readers(
                load_live_audience(args.live_source_audience, env), env,
                graph_sender=graph_sender, bc_sender=(host_options or {}).get("sender"))
        except IngestError as error:
            parser.error(f"live source audience is invalid: {error.code}")
    runtime = live.offline_runtime(args.state_dir, args.mirror_audience, args.tenant,
                                   live=readers_live, environ=env)
    projection = live.projection_file(args.state_dir, args.tenant)
    inbox = inbox_path(projection)
    rendered = []
    try:
        live.project_state(args.state_dir, projection, tenant=args.tenant)
        summary["blocked_upstream"] = live.upstream_parts(live.portal_upstream(args.state_dir))
        summary.update(refresh(runtime, investigator, audience, projection, as_of,
                               captures=args.state_dir))
        for actor in (investigator, *readers):
            session = live._Session(runtime, actor, audience, projection,
                                    captures=args.state_dir)
            try:
                access = HostAccess(session.host, runtime, actor)
                with access.request():
                    may_read, held = access.grant()
                    found = read_inbox(inbox, projection, reader=reader_key(actor),
                                       may_read=may_read, held=held, decide=access.decide)
            finally:
                session.close()
            rendered.append({"reader": sorted(actor.principals),
                             "investigator": actor is investigator,
                             "scanned": found["scanned"], "shown": len(found["gaps"]),
                             "gaps_by_kind": dict(sorted(Counter(
                                 g["kind"] for g in found["gaps"]).items()))})
    finally:
        runtime.close()
    summary["readers"] = rendered
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 1 if "failed" in summary.get("detectors", {}).values() else 0


if __name__ == "__main__":
    sys.exit(main())
