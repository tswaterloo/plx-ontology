"""Quantity and quote rules of the proactive engine: receipt quantity mismatches, receipts
without QC, orders without a customer PO, expired quotes and conflicting quantities."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
import functools
import json
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from ontology import store as ostore
from ontology.units import unit_token

from . import po_status
from . import proactive_gaps as pg

from .proactive_engine_config import (
    CONFIRMING, NO_QC_SOURCE_BINDINGS, QC_TYPES, RECEIPT_LINKS, SCOPE_CHUNK, SETTLED,
)
from .proactive_engine_findings import (
    _cites, _clusters, _day, _gap, _identity_link_types, _markings, _parts, _result, _Skips,
)
from .proactive_engine_purchasing import _po_source
from .proactive_engine_sales import _inactive_orders, _quantity

if TYPE_CHECKING:
    from .proactive_engine_config import Cite
    from .proactive_engine_findings import Part
    from .proactive_engine import _Store


def _po_receipt_quantity_mismatch(st: _Store, as_of: date) -> dict[str, Any]:
    name = "po_receipt_quantity_mismatch"
    ordered = st.values(("PurchaseOrderLine",), ("ordered_quantity",))
    received = st.values(("ReceiptLine",), ("received_quantity",))
    src = _po_source(st)
    twins = st.links((RECEIPT_LINKS[0],), SETTLED)
    root = _clusters(((l["from_object"], l["to_object"]) for l in twins), received)
    identity: dict[str, list[str]] = defaultdict(list)
    for link in twins:
        if link["from_object"] in root:
            identity[root[link["from_object"]]].append(link["markings"])
    receives = st.links((RECEIPT_LINKS[1],))
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
        # blocked, never a clean "none": the receiving QC FileMaker keeps on each receiving
        # line (its QC lot and status, ``Lot.qc_status``) has no declared QC relation
        # yet; say also when no dedicated QC record (the
        # Portal's incoming QC and eQMS tables, not served) is in the store at all
        reasons = ["qc_link_not_declared"]
        if not any(st._type_count(t) for t in QC_TYPES):
            reasons.append("no_qc_record_captured")
        return _result([], status="blocked", reasons=reasons)
    receipts = st.values(("Receipt",), ("received_date", "receipt_number"))
    lines_of: dict[str, set[str]] = defaultdict(set)
    line_marks: dict[tuple[str, str], str] = {}
    for link in st.links((RECEIPT_LINKS[2],), CONFIRMING):
        lines_of[link["to_object"]].add(link["from_object"])
        line_marks[(link["to_object"], link["from_object"])] = link["markings"]
    qc: dict[str, set[str]] = defaultdict(set)
    qc_parts: dict[str, list[Part]] = defaultdict(list)   # end -> the QC link's other end
    for link in st.links(tuple(link_types)):
        for end, other in ((link["from_object"], link["to_object"]),
                           (link["to_object"], link["from_object"])):
            qc[end].add(link["tier"])
            qc_parts[end].append((other, link["markings"]))
    # a line's twin (the same row in another system) holds that system's QC decision too
    twins: dict[str, set[str]] = defaultdict(set)
    for link in st.links((RECEIPT_LINKS[0],), CONFIRMING):
        twins[link["from_object"]].add(link["to_object"])
        twins[link["to_object"]].add(link["from_object"])
    skipped = _Skips()
    gaps = []
    for receipt, entry in sorted(receipts.items()):
        dated = [r for r in entry["props"].get("received_date", [])
                 if (d := _day(r["value"])) is not None and d <= as_of]
        if not dated:
            continue
        lines = lines_of.get(receipt, set())
        held = {receipt, *lines, *(t for line in lines for t in twins.get(line, ()))}
        tiers = set().union(*(qc.get(o, set()) for o in held))
        if not tiers and po_status.binding_of(receipt) in NO_QC_SOURCE_BINDINGS and not any(
                po_status.binding_of(t) in po_status.FM_RECEIPT_BINDINGS
                for line in lines for t in twins.get(line, ())):
            # Sage and BC keep no QC decision: out of the rule's scope, neither a gap nor
            # "cannot check" (one scope note says so, ``unjudged_words.scope_notes``)
            continue
        if tiers & set(CONFIRMING):
            continue
        if tiers:
            # its cause: the receipt's lines and every QC link on them or on it
            cause = [(other, _markings(m)) for other, m in qc_parts.get(receipt, ())]
            for line in sorted(lines_of.get(receipt, ())):
                joined = line_marks.get((receipt, line))
                cause.append((line, _markings(joined)))
                for end in (line, *sorted(twins.get(line, ()))):
                    cause += [(other, _markings(m, joined)) for other, m in qc_parts.get(end, ())]
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


def _order_without_customer_po(st: _Store, coverage: Mapping[str, Any] | None,
                               as_of: date | None = None) -> dict[str, Any]:
    """A live sales order holding no customer PO. Only the active population is asked
    (``_inactive_orders``): a closed or historic order is counted apart (``closed_order``,
    ``historic_order``), never listed as work."""
    name = "order_without_customer_po"
    if coverage is None:
        return _result([], status="not_configured", reasons=["coverage_not_given"])
    inactive = _inactive_orders(st, as_of) if as_of is not None else {}
    excluded: Counter = Counter()
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
        if found["object_id"] in inactive:
            excluded[inactive[found["object_id"]] + "_order"] += 1   # judged: not live work
            continue
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
    result = _result(gaps, status=status, not_evaluated=skipped,
                     reasons=[] if status == "complete" else list(
                         completeness.get("reasons") or ["scan_incomplete"]))
    result["counts"] = {"found": len(result["gaps"]), "excluded": dict(sorted(excluded.items()))}
    return result


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
    result = _result(gaps)
    result["checked"] = len(found)   # every quote read is judged (none is left unjudged)
    return result


def _quantity_rows(st: _Store) -> list[dict[str, Any]]:
    """Every current quantity value of a record not removed: the ``object_values`` view's
    rows of ``value_type`` quantity, read from its tables quantity kind first (the
    layouts that hold a quantity, then their cells by layout). Through the view the
    planner scanned every value source of the store (51 s on 2.8M). The order is the
    view's scan order: value source, then cell, then the field's place in its layout."""
    if getattr(st.conn, "postgres", False):
        found = _pg_quantity_rows(st)
    elif st.scoped:
        found = _scoped_quantity_rows(st)
    else:
        found = st.conn.execute(_QUANTITY_SQL).fetchall()
    found = list(found)
    found.sort(key=lambda r: (r["src"], r["cell_field"] is not None, r["cell_field"] or 0,
                              r["pos"]))
    keep = ("object_id", "object_type", "property", "value", "unit", "quantity_role",
            "l1_handle", "markings")
    return [{key: row[key] for key in keep} for row in found]


_QUANTITY_SQL = (
    "SELECT s.src, c.field AS cell_field, lf.pos, v.object_id, o.object_type, "
    "f.property, CASE WHEN c.field IS NULL THEN json_extract(c.value, lf.path) "
    "ELSE c.value END AS value, k.unit, k.quantity_role, v.l1_handle, k.markings "
    "FROM value_kinds k CROSS JOIN value_layout_fields lf ON lf.kind = k.kind "
    "CROSS JOIN value_cells c ON c.layout = lf.layout "
    "JOIN value_sources s ON s.src = c.src JOIN object_versions v ON v.rowid = s.ver "
    "JOIN objects o ON o.object_id = v.object_id JOIN value_fields f ON f.field = lf.field "
    "WHERE k.value_type = 'quantity' AND s.superseded_at IS NULL AND NOT "
    + ostore.removed_at_sql("o.object_id"))


def _pg_quantity_rows(st: _Store) -> list[Any]:
    """``_quantity_rows``' rows on the Postgres ontology, which holds no packed layouts
    (``value_layout_fields``): one cell per field, each with its own value kind. A whole
    pass reads only the records of the types an identity link joins (``_identity_link_types``:
    a dispute is two copies of one record; a lone record holds one current source), by the
    type index, and of each only the cells of the fields the schema declares a quantity
    (kind checked as well): the kind has no index there, so a read by kind scanned every
    cell of the store (520M rows; 120 s statement timeout on every full run, 2026-11-11).
    An incremental pass reads its scope's records' own value sources."""
    base = ("SELECT s.src, c.field AS cell_field, 0 AS pos, v.object_id, o.object_type, "
            "f.property, c.value, k.unit, k.quantity_role, v.l1_handle, k.markings ")
    if not st.scoped:
        sql = (base + "FROM objects o JOIN object_versions v ON v.object_id = o.object_id "
               "JOIN value_sources s ON s.ver = v.rowid AND s.superseded_at IS NULL "
               "JOIN value_cells c ON c.src = s.src "
               "AND c.field IN (SELECT value FROM json_each(:fields)) "
               "JOIN value_kinds k ON k.kind = c.kind AND k.value_type = 'quantity' "
               "JOIN value_fields f ON f.field = c.field "
               "WHERE o.object_type = :type AND NOT " + ostore.removed_at_sql("o.object_id"))
        fields = json.dumps([row[0] for row in st.conn.execute(
            "SELECT field FROM value_fields WHERE property IN (SELECT value FROM "
            "json_each(:props))", {"props": json.dumps(_quantity_properties())})])
        found = []
        for object_type in sorted({t.split(".", 1)[0] for t in _identity_link_types()}):
            found.extend(st.conn.execute(sql, {"fields": fields, "type": object_type}))
        return found
    sql = (base + "FROM value_kinds k JOIN value_cells c ON c.kind = k.kind "
           "JOIN value_sources s ON s.src = c.src JOIN object_versions v ON v.rowid = s.ver "
           "JOIN objects o ON o.object_id = v.object_id JOIN value_fields f ON f.field = c.field "
           "WHERE k.value_type = 'quantity' AND s.superseded_at IS NULL AND NOT "
           + ostore.removed_at_sql("o.object_id")
           + " AND v.object_id IN (SELECT value FROM json_each(:chunk))")
    found: list[Any] = []
    ids = st._scope_ids
    for start in range(0, len(ids), SCOPE_CHUNK):
        found.extend(st.conn.execute(sql, {"chunk": json.dumps(ids[start:start + SCOPE_CHUNK])}))
    return found


@functools.lru_cache(maxsize=1)
def _quantity_properties() -> list[str]:
    """Every property name the schema declares a quantity on some type."""
    from ontology.schema import load_schema

    return sorted({p["name"] for spec in load_schema().object_types.values()
                   for p in spec.get("properties", ())
                   if (p.get("value_type") or p.get("type")) == "quantity"})


def _scoped_quantity_rows(st: _Store) -> list[Any]:
    """``_quantity_rows``' rows of the scope's records (an incremental pass), read from
    each record's own value sources (its version, never a scan of the quantity cells)."""
    found: list[Any] = []
    sql = ("SELECT s.src, c.field AS cell_field, lf.pos, v.object_id, o.object_type, "
           "f.property, CASE WHEN c.field IS NULL THEN json_extract(c.value, lf.path) "
           "ELSE c.value END AS value, k.unit, k.quantity_role, v.l1_handle, k.markings "
           "FROM object_versions v JOIN objects o ON o.object_id = v.object_id "
           "JOIN value_sources s ON s.ver = v.rowid JOIN value_cells c ON c.src = s.src "
           "JOIN value_layout_fields lf ON lf.layout = c.layout "
           "JOIN value_kinds k ON k.kind = lf.kind JOIN value_fields f ON f.field = lf.field "
           "WHERE v.object_id IN (SELECT value FROM json_each(:chunk)) "
           "AND k.value_type = 'quantity' AND s.superseded_at IS NULL AND NOT "
           + ostore.removed_at_sql("o.object_id"))
    ids = st._scope_ids
    for start in range(0, len(ids), SCOPE_CHUNK):
        found.extend(st.conn.execute(sql, {"chunk": json.dumps(ids[start:start + SCOPE_CHUNK])}))
    return found


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
    disputed = {top: {m["object_id"] for m in members}
                for (top, _t, _p), members in grouped.items()
                if len({m["l1_handle"] for m in members}) >= 2}
    closed = _closed_clusters(st, disputed, {row["object_type"] for row in rows})
    edited = _edit_times(st, disputed, {row["object_type"] for row in rows})
    gaps = []
    skipped: Counter[str] = Counter()
    for (top, object_type, prop), members in sorted(grouped.items()):
        if len({m["l1_handle"] for m in members}) < 2:
            continue
        if _superseded(members, edited):
            # the newest system of record wins: a value only an older record of the same
            # identity still holds is superseded, no risk (the record page shows both)
            skipped["superseded_by_newer_record"] += 1
            continue
        by_handle = {m["l1_handle"]: m for m in members}
        subject = f"{object_type}.{prop}"
        conflict = {"kind": "value_disagreement", "subject": subject, "values": [
            {"value_type": "quantity", "value": m["value"], "unit": unit_token(m["unit"]),
             "quantity_role": m["quantity_role"], "citation": {"l1_handle": m["l1_handle"]}}
            for m in members]}
        items = pg._conflicting_quantities([conflict], skipped)
        if items and top in closed:
            skipped["record_closed"] += len(items)
            continue   # a finished or cancelled record: nothing left to act on
        for item in items:
            gaps.append(_gap(
                item["kind"], "conflicting_quantity", top, object_type, item["key"],
                summary=item["summary"], missing=item["missing"], why=item["why"],
                priority=item["priority"], about=(object_type, prop),
                groups=[_cites((by_handle[h] for h in group), *marks.get(top, []))
                        for group in item["requires"]]))
    return _result(gaps, not_evaluated=skipped)


# A record's own last-edit time as its system stamps it (never when it was read).
EDIT_TIME_PROPS = ("portal_updated_at", "fm_modified_at", "bc_last_modified_at")


def _edit_times(st: _Store, disputed: Mapping[str, set[str]], types: Iterable[str]
                ) -> dict[str, Any]:
    """object id -> its newest own edit time (``EDIT_TIME_PROPS``), of the objects holding
    a disputed value; an object with none stated is absent."""
    from .answer_conflicts import parse_time
    objects = sorted({o for members in disputed.values() for o in members})
    if not objects:
        return {}
    out: dict[str, Any] = {}
    for oid, entry in st.values(sorted(types), EDIT_TIME_PROPS, objects=objects).items():
        times = [t for p in EDIT_TIME_PROPS for r in entry["props"].get(p, [])
                 for t in [parse_time(r["value"])] if t is not None]
        if times:
            out[oid] = max(times)
    return out


def _superseded(members: Sequence[Mapping[str, Any]], edited: Mapping[str, Any]) -> bool:
    """Whether one value of a disputed quantity is held by the newest edited record of the
    identity: the one conflict rule (``answer_conflicts.resolve``'s "newest", records
    dated by their own edit time) decides, never a value two records of one system hold
    alike. Each disagreeing record must state its edit time, and they must be different
    records (one record stating two values is no supersession)."""
    from .answer_conflicts import Reading, resolve
    if len({m["object_id"] for m in members}) < 2 \
            or any(m["object_id"] not in edited for m in members):
        return False
    by_value: dict[str, set[str]] = defaultdict(set)
    for m in members:
        by_value[str(pg._decimal(m["value"]))].add(m["object_id"])
    if any(a & b for x, a in by_value.items() for y, b in by_value.items() if x < y):
        return False
    found = resolve("quantity", [Reading(value=m["value"], handle=m["l1_handle"],
                                         as_of=edited[m["object_id"]]) for m in members])
    return bool(found) and found.get("rule") == "newest"


def _closed_clusters(st: _Store, disputed: Mapping[str, set[str]],
                     types: Iterable[str]) -> set[str]:
    """The clusters (of ``disputed``: root -> its objects holding the disputed values) any
    of whose records' own ``status`` says closed (``proactive_gaps.status_closed``)."""
    owner = {object_id: top for top, members in disputed.items() for object_id in members}
    if not owner:
        return set()
    found = st.values(sorted(types), ("status",), objects=sorted(owner))
    return {owner[object_id] for object_id, entry in found.items()
            if any(pg.status_closed(row["value"])
                   for row in entry["props"].get("status", []))}


def _card_of(object_id: str, object_type: str, props: Mapping[str, Any]) -> dict[str, Any]:
    """A ``plx.object``-shaped card of the store's current values, for the shared rules."""
    return {"object": {"object_id": object_id, "object_type": object_type},
            "values": [{"property": prop, "value": row["value"], "unit": row["unit"],
                        "citation": {"l1_handle": row["l1_handle"]}}
                       for prop, rows in props.items() for row in rows]}


def _unreceived_draft(st: _Store, order: str, order_lines: Sequence[str],
                      lines: Mapping[str, Mapping[str, Any]]) -> bool:
    """``order`` is a BC Draft (its ``bc_status`` read as not issued) none of whose lines
    records a received quantity above 0 (an unreadable one counts as none: a Draft's
    date never decides late on a guess)."""
    values = (st.values((order.split(":", 1)[0],), ("bc_status",), objects=[order])
              .get(order) or {"props": {}})["props"]
    if not po_status.bc_draft({"bc_status": [r["value"] for r in values.get("bc_status", [])]}):
        return False
    for line_id in order_lines:
        for row in (lines.get(line_id) or {"props": {}})["props"].get(pg.LINE_RECEIVED, ()):
            number = po_status._number(row["value"])
            if number is not None and number > po_status.EPS:
                return False
    return True
