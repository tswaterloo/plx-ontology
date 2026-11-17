"""Purchase-order rules of the proactive engine: late or unconfirmed POs, receipt doubts and
reconciliation, and POs whose systems disagree."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from . import active_population as active
from . import po_status

from .proactive_engine_config import CONFIRMING, UNDATED_KIND
from .proactive_engine_findings import _cites, _gap, _markings, _parts, _result, _Skips

if TYPE_CHECKING:
    from .proactive_engine_config import Cite
    from .proactive_engine_findings import Part
    from .proactive_engine import _Store

PO_STATUS_PROPS = tuple(po_status.SOURCE_OF)
# the follow-up field naming the order an item is about (its key and its record's rank):
# never shown, read by ``read_inbox`` to list one item per order for each reader
_ORDER = "order"
# The order record a gap's evidence attests, on gaps that are not that order's own item
# (``missing_in_record``): read by the active-population and test-record filters only,
# never the inbox's one-item-per-order fold (``_ORDER``), so such a note never displaces
# or folds that order's late item.
_ATTESTS = "attests_order"


def _po_source(st: _Store) -> po_status.StoreSource:
    """The whole store's purchase orders as the one PO reader reads them, read once per
    run (the late rule and the receipt quantity rule both read every order)."""
    # read whole even in an incremental pass: its number collisions and default dates
    # are populations every order is judged against
    return st.population("po_source", lambda: po_status.StoreSource(st),
                         lambda src: (sorted(src.colliding), src.defaults.values))


def _mention_resolver(st: _Store, people: Any) -> Any:
    """The store's reused-number mention resolver (``ontology.po_mention_resolve``) with
    our own domains from the people exclusion; None without them (nothing resolves)."""
    from ontology import po_mention_resolve

    own = tuple(getattr(people, "internal", ()) or ())
    if people is None or getattr(people, "unavailable", None) or not own:
        return None
    try:
        with st.whole():
            return po_mention_resolve.Resolver(st.conn, own)
    except Exception:  # noqa: BLE001 - unreadable here: nothing resolves (counted), never a guess
        return None


def _late_or_unconfirmed_po(st: _Store, as_of: date) -> dict[str, Any]:
    """Each settled PO read by the one purchase-order reader (``po_status.read`` over
    ``po_status.StoreSource``: status, lines, FM QC log, ranked need date, promise and
    verdict); this rule adds only its counts, skips, citations and gaps."""
    name = "late_or_unconfirmed_po"
    src = _po_source(st)
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
    # orders open by any read; those any read dates; each undated read's pending item
    open_orders: set[str] = set()
    dated_orders: set[str] = set()
    undated_reads: dict[str, list[tuple[str, list, Any]]] = defaultdict(list)
    # orders a read raised a receipt reconciliation note for (another copy records them
    # received): never also "open, no due date" (11-13 judging: a March BC order still
    # Open beside the Portal's Complete copy with its receipts)
    reconciled_orders: set[str] = set()
    # orders another read of which is closed (the Portal's Complete copy of a migrated BC
    # order still Open, joined through the BC read's twin): never "open, no due date"
    closed_orders: set[str] = set()
    for po in reads:
        members = po.members
        order = root.get(members[0], members[0])
        # BC's record governs; among others the least record id, so the choice is pinned
        # the copy that holds the number the supplier knows (a FileMaker-era order's
        # FileMaker PO number) is shown first, then a BC copy, then the least record id
        rank = [0 if any(src.props(m).get("fm_po_number") for m in members) else
                1 if any(po_status.system_of(m) == po_status.POSTED_COPY for m in members)
                else 2, min(members)]

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
            # an open copy of an order another copy records received is a reconciliation
            # note (a receipt in any system closes the order), never a delivery risk
            if conflict.get("reconcile"):
                doubt = _receipt_doubt(conflict)
                if doubt is not None:
                    # the received side's own record does not stand behind "received"
                    # (11-13 judging: 12 of 17 rested on a B.Order header or no receipt)
                    counts[doubt] += 1
                    continue
                # another copy records the order received: it is this note, never also
                # "open, no due date" (decided once every read is done)
                reconciled_orders.add(order)
            gaps.append((_receipt_reconciliation if conflict.get("reconcile")
                         else _systems_disagree)(name, conflict, records,
                                                 [*marks, *po.marks]))

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
            if state.cls == po_status.CLOSED:
                closed_orders.add(order)
            continue
        open_orders.add(order)
        rep = min(m for m in members if any(src.props(m).get(p) for p in PO_STATUS_PROPS))
        for _ in po.need.unparseable:
            skip("date_unparseable")
        verdict, why = po.verdict
        if why == "no_requested_date":
            # the requested-by date is the reference or it is missing: never a line's or
            # the supplier's date in its place (nor a BC Draft twin's, decision
            # 2026-11-01-late-list-draft-twins): never late, listed apart as
            # "open, no due date recorded", cited by the status it is open by
            leads = [link for link in (src.confirm_links(
                [*members, *(t for t in po.date_twins if t not in members)]) or [])
                if link["tier"] not in CONFIRMING]
            if leads:
                # a supplier confirmation is linked only as a lead: it may state the date
                # (11-13 judging: one confirming delivery 2027-01-25), so "no supplier-
                # confirmed date" is not known
                skip("confirmation_unsettled", *[(l["from_object"], _markings(
                    l["markings"], *marks)) for l in leads])
                continue
            skip("no_need_date")
            # decided once every read of the order is done: an order another read dates
            # is never undated (one order, one verdict)
            status_cites = [[cite] for cite in _cites(po.evidence, *marks, *po.marks)]
            undated_reads[order].append((order, rank, _gap(
                UNDATED_KIND, name, rep, "PurchaseOrder", "undated",
                summary="An open purchase order has no due date recorded"
                        + (" (its only dates are system-computed or placeholder defaults)"
                           if po.need is not None and po.need.defaulted else ""),
                missing="What is the requested-by date of this open purchase order?",
                why="The order is open, but no requested-by date, supplier-confirmed "
                    "date or lead time is recorded on it or on any released system record "
                    "of it, so it is never counted late.",
                priority="low", about=("PurchaseOrder", "expected_date"),
                groups=status_cites,
                extra=_cites(rows("po_number"), *marks)) if status_cites else None))
            continue
        dated_orders.add(order)
        if why == "need_date_disputed":
            skip("need_date_disputed")
            continue
        need = po.need.hi
        need_cites = _cites(po.need.governing, *marks, *po.need.marks)
        basis = po.need.basis
        # every status record and line quantity the verdict rests on is its own evidence:
        # a reader who may not open one of them sees no gap (none is an alternative)
        status_cites = [[cite] for cite in _cites(po.evidence, *marks, *po.marks)] or [[]]
        number = _cites(rows("po_number") + rows("fm_po_number"), *marks) + _cites(
            [r for t in po.date_twins for p in ("po_number", "fm_po_number")
             for r in src.props(t).get(p, [])], *marks, *po.marks)
        # confirmations linked to ANY copy of the order (its twins in every system)
        links = src.confirm_links([*members, *(t for t in po.date_twins if t not in members)])
        linked = [(link["from_object"], _markings(link["markings"], *marks)) for link in links]
        settled = [link for link in links if link["tier"] in CONFIRMING]
        late_now = verdict == po_status.LATE
        if late_now or not links:   # decided without a confirmation (none is at risk)
            counts["evaluated"] += count("evaluated")
            counts["late"] += count("late") if late_now else 0
        if late_now and links and not settled:
            # a confirmation is linked, but only as an unverified lead: never "none"
            counts["confirmation_unverified"] += count("confirmation_unverified")
            found = _link_cites(src, links)
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=_late_summary(basis, need, po.need.doubt),
                missing=f"When will the purchase order due {need.isoformat()} be received?",
                why="The order is still open after its due date.",
                priority=_doubt_priority("medium", po.need.doubt), about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number + found,
                days_late=(as_of - need).days,
                # "found, unverified" only to a reader who may open one of the found
                # confirmations (``_shown``); never "none"
                confirmation="found_unverified", confirmation_cites=[c[0] for c in found],
                due_basis=basis))
            continue
        reused = [] if links else src.reused_links(
            [*members, *(t for t in po.date_twins if t not in members)])
        if late_now and reused:
            # only leads on a number another order also holds: evidence that cannot be
            # tied to this order, so "needs check", never "none found"
            found = _link_cites(src, reused)
            counts["confirmation_needs_check"] += count("confirmation_needs_check")
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=_late_summary(basis, need, po.need.doubt),
                missing=f"When will the purchase order due {need.isoformat()} be received?",
                why="The order is still open after its due date. A document that may be its "
                    "supplier's confirmation names a number another order also holds.",
                priority=_doubt_priority("medium", po.need.doubt), about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number + found,
                days_late=(as_of - need).days, confirmation=po_status.NEEDS_CHECK,
                confirmation_cites=[c[0] for c in found], due_basis=basis))
            continue
        if not links:
            if need <= as_of:
                counts["unconfirmed"] += count("unconfirmed")
                order_gap(_gap(
                    "unconfirmed_po", name, rep, "PurchaseOrder", "unconfirmed",
                    summary=_unconfirmed_summary(basis, need, po.need.doubt),
                    missing=f"Has the supplier confirmed the purchase order due "
                            f"{need.isoformat()}?",
                    why="The order is open, its due date has come and no supplier "
                        "confirmation is linked to any copy of it in the store.",
                    priority=_doubt_priority("high", po.need.doubt), about=("PurchaseOrder", "expected_date"),
                    groups=[need_cites, *status_cites], extra=number,
                    days_late=(as_of - need).days if late_now else None,
                    confirmation="none_found", due_basis=basis))
            continue
        if late_now:   # late on the requested-by date alone, whatever was confirmed
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=_late_summary(basis, need, po.need.doubt),
                missing=f"When will the purchase order due {need.isoformat()} be "
                        "received?",
                why="The order is still open after its due date.",
                priority=_doubt_priority("medium", po.need.doubt), about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number + _link_cites(src, settled),
                days_late=(as_of - need).days, confirmation="confirmed",
                confirmation_cites=[c[0] for c in _link_cites(src, settled)], due_basis=basis))
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
                        f"{need.isoformat()}" + _doubt_words(po.need.doubt),
                missing=f"Can the purchase order needed by {need.isoformat()} arrive in time, "
                        f"or must the plan move to {shown}?",
                why="A linked supplier confirmation states a delivery date later than the "
                    "order's need date.",
                priority=_doubt_priority("high", po.need.doubt), about=("PurchaseOrder", "expected_date"),
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
    stale = _historic_orders(st, as_of, {m: o for m, o in root.items()
                                         if o in undated_reads and o not in dated_orders})
    for order, pending in undated_reads.items():
        if order in dated_orders:
            continue   # another read of this order dates it: never undated
        if order in reconciled_orders or order in closed_orders:
            # another copy records it received or closed: a reconciliation (listed as its
            # note when one is raised), never an open order missing its date
            counts["closed_elsewhere"] += 1
            continue
        if order in stale:
            # open and undated, but nothing about it moved in the active window (a legacy
            # back-order): counted apart, never listed as current work
            counts["historic"] += 1
            continue
        counts["undated"] += 1   # counted whether or not a status value is citable
        counted[order].add("undated")
        for key, rank, gap in pending:
            if gap is not None:
                gap["follow_up"][_ORDER] = {"key": key, "rank": rank}
                gaps.append(gap)
    late_orders = {o for o, keys in counted.items() if "late" in keys}
    result = _result(gaps, not_evaluated=skipped)
    # per order, the counts it is in: a gap the active-population filter drops takes its
    # order out of them (``_drop_inactive_orders``), so the counts say what is listed
    result["counted"] = {o: sorted(keys) for o, keys in counted.items()}
    # each record's order (the cross-system join) and that order's class (open when any
    # read of it is open): the vendor-mail rule judges open orders only, one per order
    result["po_orders"] = {m: (order, po_status.OPEN if order in open_orders
                               else order_class.get(order, ([], po_status.UNKNOWN))[1])
                           for m, order in root.items()}
    classes.update(cls for _rank, cls in order_class.values())
    result["counts"] = {"pos": len(order_class),
                        "classes": {cls: classes.get(cls, 0) for cls in po_status.CLASSES},
                        **{key: counts.get(key, 0) for key in (
                            "evaluated", "late", "at_risk", "unconfirmed", "undated",
                            "historic", "closed_elsewhere", *RECEIPT_DOUBTS)},
                        # orders open by any of their reads (a Portal Pending order beside
                        # its BC Draft twin is classed by the BC record, yet open here):
                        # open_by_read = late + undated + historic + closed_elsewhere
                        # + open_other
                        "open_by_read": len(open_orders),
                        "open_other": len(open_orders - late_orders
                                          - (set(undated_reads) - dated_orders))}
    return result


# where an order's due date comes from (``po_status.Need.basis``), as a gap words it
_DUE_WORDS = {"requested": "its need date",
              "vendor_eta": "the supplier's confirmed date",
              "lead_time": "its lead-time due date"}


PO_ACTIVITY = ("order_date", "portal_created_at", "fm_created_at", *active.EDIT_TIME_PROPS)


def _historic_orders(st: _Store, as_of: date, order_of: Mapping[str, Any]) -> set[Any]:
    """The purchase orders (of ``order_of``: record -> its order) none of whose records
    holds a business date (order, creation or edit day; bulk import days left out) within
    ``active.ACTIVE_DAYS`` of ``as_of`` (``active.inactive_reason``)."""
    if not order_of:
        return set()

    def every_import_day() -> dict[Any, frozenset]:
        held = st.values(("PurchaseOrder",), PO_ACTIVITY)
        return active.import_days(((po_status.binding_of(oid), n), r.get("value"))
                                  for oid, entry in held.items() for n in PO_ACTIVITY
                                  for r in entry["props"].get(n, ()))
    imported = st.population("po_import_days", every_import_day)
    found = st.values(("PurchaseOrder",), PO_ACTIVITY, objects=sorted(order_of))
    dates: dict[Any, list[date]] = defaultdict(list)
    for oid, entry in found.items():
        dates[order_of[oid]] += active.business_days(
            ({"value": r.get("value"), "binding": po_status.binding_of(oid), "property": n}
             for n in PO_ACTIVITY for r in entry["props"].get(n, ())), imported)
    return {order for order in set(order_of.values()) if active.inactive_reason(
        po_status.OPEN, as_of=as_of, dates=dates.get(order, ())) == active.HISTORIC}


def _doubt_words(doubt: str | None) -> str:
    """What a doubted due date says (``po_status.Need.doubt``, BIZ-007): the date is the
    order's own day, kept and judged, but it may be a form default."""
    if doubt == po_status.OWN_DAY:
        return ("; that date is the day the order was placed, so it may be a form default "
                "rather than the requested date")
    return "; the due date is in doubt" if doubt else ""


def _doubt_priority(priority: str, doubt: str | None) -> str:
    return "low" if doubt else priority


def _late_summary(basis: str, need: date, doubt: str | None = None) -> str:
    return (f"An open purchase order is past {_DUE_WORDS[basis]} {need.isoformat()}"
            + _doubt_words(doubt))


def _unconfirmed_summary(basis: str, need: date, doubt: str | None = None) -> str:
    due = ("needed by" if basis == "requested" else "due by its lead time on"
           if basis == "lead_time" else "due by")
    return (f"An open purchase order {due} {need.isoformat()} has no supplier confirmation"
            + _doubt_words(doubt))


RECEIPT_TYPES = (po_status.RECEIPT_TYPE, "ReceiptLine", po_status.LOT_TYPE)   # goods in
RECEIPT_DOUBTS = ("received_header_open", "received_without_receipt")


def _receipt_doubt(conflict: Mapping[str, Any]) -> str | None:
    """Why a reconciliation note is not raised, else None. The side said to hold the goods
    received must stand behind it on its own record: its header word may not read open
    (B.Order, Pending, Generated or Revised: FileMaker's items rolled up over a header still
    saying back-ordered are no closing record a person can act on), and it must cite a
    recorded receipt (a ``Receipt``/``ReceiptLine`` record, or a BC side's own received
    line quantities); a status word alone is no receipt. Judged on the cited items only,
    so the verdict never reads more than the note shows."""
    if conflict.get("field") != "receipt_state":
        return None
    received = conflict.get("governs")
    side = next((s for s in conflict.get("sides") or () if s.get("system") == received), None)
    if side is None:
        return RECEIPT_DOUBTS[1]
    items = [i for i in side.get("items") or () if isinstance(i, Mapping)]
    open_words = po_status.FM_ITEM_PARTIAL | po_status.FM_HEADER_NOTHING
    if any(str(i.get("object_id") or "").startswith("PurchaseOrder:")
           and i.get("property") in ("status", "fm_status")
           and po_status.word(i.get("value")) in open_words for i in items):
        return RECEIPT_DOUBTS[0]
    if not any(str(i.get("object_id") or "").split(":", 1)[0] in RECEIPT_TYPES
               or i.get("property") == po_status.RECEIVED_QTY for i in items):
        return RECEIPT_DOUBTS[1]
    return None


def _receipt_reconciliation(name: str, conflict: Mapping[str, Any], records: Iterable[str],
                            marks: Sequence[str]) -> dict[str, Any] | None:
    """A ``receipt_reconciliation`` note: one copy of a purchase order records the goods
    received while another copy (a BC order, say) still shows them outstanding. A receipt
    in any system closes the order, so this is never a late or at-risk claim: the open
    copy needs reconciling. Each side's citations are their own group."""
    field, sides = conflict["field"], conflict["sides"]
    received = conflict.get("governs")
    groups = [_cites(side["items"], *marks, *conflict.get("marks", ())) for side in sides]
    open_side = next((side for side in sides if side["system"] != received), sides[-1])
    subject = (open_side["records"] or [min(records)])[0]
    unit = f" {conflict['unit']}" if conflict.get("unit") else ""

    def said(side: Mapping[str, Any]) -> str:
        value = side["value"]
        return (_STATE_WORDS.get(value, value) if field == "receipt_state"
                else f"{value}{unit}")
    return _gap(
        "receipt_reconciliation", name, subject, "PurchaseOrder",
        f"{field}|{'|'.join(sorted(records))}",
        summary=(f"A received purchase order still shows {_FIELD_WORDS[field]} differently "
                 "in another copy: " + "; ".join(
                     f"{side['system']} says {said(side)}" for side in sides)
                 + (f" ({received} records the goods received; the other copy needs "
                    "reconciling)" if received else "")),
        missing=f"Can the open copy of this received purchase order be closed or "
                f"corrected ({_FIELD_WORDS[field]})?",
        why="A receipt in any system closes the order; the copy that still shows it open "
            "is a bookkeeping difference, not a late delivery.",
        priority="low",
        about=("PurchaseOrder", "status"), groups=groups)


def _link_cites(src: po_status.StoreSource, links: Sequence[Mapping[str, Any]]
                ) -> list[Cite]:
    """The confirming documents' own records, each cited through its link's markings."""
    out: list[Cite] = []
    for link in links:
        out += _cites(src.documents.get(link["from_object"], []), link["markings"])
    return list({c[0]: c for c in out}.values())


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
    rule = ("the declared date precedence governs" if field == "expected_date" and governs
            else f"{governs} records the goods received" if governs
            else "no system governs")
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
