"""Proactive events: the analyst's watch list, run after every build beside the gap rules.

The scheduled engine (``operating.proactive_engine``) runs these detectors with its own
rules, over the same store generation, and folds what they find into the same inbox: an
event is a gap of an event kind, with its citations grouped (each group must stay cited
for a reader to see it), a stable id, its first and last sight (so "new since your last
visit" and "new this generation" are read from the inbox, never re-decided) and, where the
claim is a verdict on what a source does NOT hold, the source audiences a reader must be
granted (``requires``). Every figure an event states is a cited value or a count of days
between two cited dates; nothing is estimated, converted or summed across currencies.

Set-based detectors (one pass over the store):

* ``eta_slipped``: a date someone promised moved later. A purchase order's requested-by
  date (``expected_date``) or a line's expected receipt date (BC ``expectedReceiptDate``,
  which follows the supplier's promise) whose value superseded within ``REVISION_DAYS``
  of the run's day was earlier than the value now held by the same source field (both
  versions cited, each its own group); or two settled supplier confirmations of one order
  whose later document promises a later delivery date (both cited); or, per order line,
  two confirmations each promising the line's own item a date (``line_promises``, the
  line matched on firm evidence by ``operating.line_promises``) whose later document
  promises that line a later arrival (both confirmations, their dates and the line's
  matched value cited, the line and its match basis in the event). A confirmation's
  event tells a reader who may not read every source a promise sits in that a later
  one it cannot see may exist (``caveat``);
* ``lot_attention``: a lot whose expiry date falls within ``EXPIRY_DAYS`` of the run's day
  and whose QC status says it still holds stock (``lot_near_expiry``; a used-up, rejected
  or expired lot is skipped, a lot with no, a disputed or an unmapped status is
  ``not_evaluated``, never claimed), and a batch on quality hold (``lot_on_hold``): a
  hold is claimed only where it is not the default of the batch's lifecycle state in its
  own system (``stock_state.batch_hold`` over every batch of that system, by status; a
  state too small there read over every system's), so a made batch never released is a
  hold and an unmade one carrying the pre-release default is not; a cancelled batch is
  never claimed, an unmapped status or an unmade batch in a state too small to profile
  is ``not_evaluated``. A copy of a batch (``Batch.same_record_as``: the migrated record a
  source batch was copied into) is decided by its origin and counted once, on it;
* ``accepted_quote_no_order``: a quote accepted (its status word or its accepted time)
  that no sales order converts (no ``Quote.converted_to`` or
  ``SalesOrder.created_from_quote`` link, no converted order number). The absence is the
  run's verdict, so a reader sees it only with a grant on every sales-order audience;
* ``invoice_quantity_mismatch``: per purchase order and item, the quantity its posted
  supplier invoices bill (``PurchaseInvoice.invoices``) above what its lines received, in
  one unit (a unit not stated on both sides is ``not_evaluated``, never converted).

* ``vendor_no_reply`` (F28's question over the whole store, ``vendor_no_reply``): every
  open purchase order whose mail shows our request to the outside unanswered for 7 days
  or more, people cleared first. The absence of a reply is the run's verdict: a reader
  needs a grant on every audience the conversation's cleared copies sit in.

Tool-call detectors (the run principal's ``plx.investigate``, bounded to ``CALL_BOUND``
roots, most recent first, or every root on the engine's process pool; more candidates
make the rule ``truncated``):

* ``vendor_terms_unapplied`` (F27 ``vendor_replies``): a term a vendor's reply offers that
  differs from or fills the system's field, never applied;
* ``negative_margin`` (F10 ``order_margin`` through ``operating.customer_profit``): a
  sales order invoiced within ``MARGIN_DAYS`` before the newest invoice date its own
  system holds (BC's and Sage's each count back from their own) whose decided, checked
  margin is below zero; every figure it rests on is its own citation group. An order whose
  invoiced revenue net of credits is zero is a ``no_charge_order`` (its item cost stated,
  low priority), never a loss.

Late purchase orders, a late PO hitting a customer order (``orders_at_risk``) and the
receipt quantity mismatch are the engine's own rules; ``operating.briefing`` maps every
kind to its event type. Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore
from ontology.units import unit_token

from . import stock_state

REVISION_DAYS = 14
EXPIRY_DAYS = 30
MAIL_DAYS = 30
MARGIN_DAYS = 30
CALL_BOUND = 40
CALL_BOUND_REASON = "event_bound"
APART_REASON = "read_apart"   # left to the separate vendor-mail pass
NO_REPLY_DAYS = 7    # F28's threshold: a request waiting this long is overdue
CACHE_DAYS = 14      # a vendor-mail reading is redone at least this often
CACHE_VERSION = 2    # bump when what a reading means changes: every root is read again

REVISED_DATES = {"PurchaseOrder": ("expected_date",),
                 "PurchaseOrderLine": ("bc_expected_receipt_date",)}
DATE_WORDS = {"expected_date": "requested-by date",
              "bc_expected_receipt_date": "expected receipt date"}
ACCEPTED_WORDS = frozenset({"accepted"})
POSTED_INVOICE = frozenset({"open", "paid"})

QUOTE_ORDER_LINKS = ("Quote.converted_to", "SalesOrder.created_from_quote")
MAIL_PO_LINK = "Email.mentions_purchase_order"
INVOICE_LINK = "SalesInvoice.bills"
INVOICE_LINKS = ("PurchaseInvoice.invoices", "PurchaseInvoiceLine.line_of",
                 "PurchaseInvoiceLine.for_item", "PurchaseOrderLine.line_of",
                 "PurchaseOrderLine.for_item")
# Every link type these rules read (the engine reads them in one scan, ``_Store.links``).
LINK_TYPES = ("PurchaseOrderLine.line_of", "SupplierConfirmation.confirms",
              *QUOTE_ORDER_LINKS, *INVOICE_LINKS, MAIL_PO_LINK, INVOICE_LINK,
              stock_state.BATCH_TWIN_LINK)


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _decimal(raw: Any) -> Decimal | None:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _one(rows: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    """The one value of a property, or None when it is missing or its sources disagree."""
    rows = [r for r in rows or () if r.get("value") not in (None, "")]
    if not rows or len({str(r["value"]).strip() for r in rows}) != 1:
        return None
    return rows[0]


def _word(row: Mapping[str, Any] | None) -> str:
    return " ".join(str((row or {}).get("value") or "").replace("_", " ").split()).casefold()


# --------------------------------------------------------------------------- eta_slipped

def eta_slipped(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                settled: Sequence[str], confirming: Sequence[str],
                caveat: Callable[[Any], None] = lambda _gap: None,
                orders: Any = None) -> dict[str, Any]:
    """``caveat(gap)``: records on a gap resting on supplier promises that a reader who
    may not read every promise is told a later one may exist (the engine's). ``orders``:
    the one PO reader over the store (``po_status.StoreSource``): an order it calls closed
    or a draft never slips (nothing is due), and a revised date either side of which is a
    default (a bulk share of its property's records, a sync's re-write, or one of the
    order's own days: ``po_status.is_default_date``) is no slip of a promise."""
    gaps = []
    skipped: Counter = Counter()
    settled_state = _OrderState(orders)
    since = (as_of - timedelta(days=REVISION_DAYS)).isoformat()
    lines_of: dict[str, tuple[str, str]] = {}
    for link in st.links(["PurchaseOrderLine.line_of"], settled):
        lines_of[link["from_object"]] = (link["to_object"], link["markings"])
    for object_type, props in REVISED_DATES.items():
        ids = [r[0] for r in st.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_type = ? AND NOT "
            + ostore.removed_at_sql("o.object_id") + " ORDER BY o.object_id", (object_type,))]
        if getattr(st, "scoped", False):
            ids = [oid for oid in ids if oid in st.limit]   # an incremental pass's records
        for start in range(0, len(ids), 500):
            chunk = json.dumps(ids[start:start + 500])
            rows = st.conn.execute(
                "SELECT v.object_id, v.property, v.value, v.binding_id, v.field_path, "
                "v.l1_handle, v.markings, v.recorded_at, v.superseded_at FROM object_values v "
                "WHERE v.object_id IN (SELECT value FROM json_each(:chunk)) AND v.property IN "
                "(SELECT value FROM json_each(:props))",
                {"chunk": chunk, "props": json.dumps(list(props))}).fetchall()
            by_field: dict[tuple, list[Mapping[str, Any]]] = defaultdict(list)
            for row in rows:
                by_field[(row["object_id"], row["property"], row["binding_id"],
                          row["field_path"])].append(dict(row))
            for (oid, prop, _b, _f), versions in sorted(by_field.items()):
                current = [v for v in versions if v["superseded_at"] is None]
                if len(current) != 1:
                    continue
                now, new_day = current[0], _day(current[0]["value"])
                old = [v for v in versions if v["superseded_at"] is not None
                       and v["superseded_at"] >= since and _day(v["value"]) is not None]
                if new_day is None or not old:
                    continue
                prev = max(old, key=lambda v: v["superseded_at"])
                old_day = _day(prev["value"])
                if old_day is None or new_day <= old_day:
                    continue
                subject, extra_marks = oid, None
                if object_type == "PurchaseOrderLine":
                    if oid not in lines_of:
                        skipped["line_without_order"] += 1
                        continue
                    subject, extra_marks = lines_of[oid]
                why_not = settled_state.no_slip(subject, [(now, new_day), (prev, old_day)])
                if why_not is not None:
                    skipped[why_not] += 1
                    continue
                days = (new_day - old_day).days
                words = DATE_WORDS.get(prop, prop)
                made = gap(
                    "eta_slipped", "eta_slipped", subject, "PurchaseOrder",
                    f"{oid}|{prop}|{old_day}|{new_day}",
                    summary=f"The {words} moved later by {days} days, from {old_day} to "
                            f"{new_day}",
                    missing="Whether the new date still meets the orders this purchase "
                            "order supplies",
                    why=f"The source changed the {words} between two builds: {old_day} "
                        f"before, {new_day} now (both versions cited).",
                    priority="high" if days >= 7 else "medium",
                    about=("PurchaseOrder", "lateness"),
                    groups=[cites([prev], extra_marks), cites([now], extra_marks)],
                    event={"days": days, "date_was": old_day.isoformat(),
                           "date_now": new_day.isoformat()})
                gaps.append(made)
    # two settled confirmations of one order, the later one promising later
    confirms: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in st.links(["SupplierConfirmation.confirms"], confirming):
        confirms[link["to_object"]].append((link["from_object"], link["markings"]))
    several = {po: c for po, c in confirms.items() if len(c) > 1}
    for po in sorted(several):
        why_not = settled_state.no_slip(po, [])   # a closed order's promise moves nothing
        if why_not is not None:
            skipped[why_not] += 1
            del several[po]
    revised = len(gaps)   # the source field revisions above rest on no promise read
    if several:
        found = st.values(["SupplierConfirmation"],
                          ("delivery_date", "document_date", LINE_PROMISES),
                          objects=sorted({c for cs in several.values() for c, _ in cs}))
        gaps += _line_slips(st, several, found, settled, gap=gap, cites=cites,
                            skipped=skipped)
        for po, cs in sorted(several.items()):
            dated = []
            for conf, marks in cs:
                props = (found.get(conf) or {}).get("props") or {}
                promised, issued = _one(props.get("delivery_date")), _one(
                    props.get("document_date"))
                if promised is None or issued is None or _day(promised["value"]) is None \
                        or _day(issued["value"]) is None:
                    if not props.get(LINE_PROMISES):   # per item: read by line above
                        skipped["confirmation_undated"] += 1
                    continue
                dated.append((_day(issued["value"]), _day(promised["value"]), conf,
                              [promised, issued], marks))
            dated.sort(key=lambda d: (d[0], d[2]))
            for earlier, later in zip(dated, dated[1:]):
                if later[0] <= earlier[0] or later[1] <= earlier[1]:
                    continue
                days = (later[1] - earlier[1]).days
                gaps.append(gap(
                    "eta_slipped", "eta_slipped", po, "PurchaseOrder",
                    f"{earlier[2]}|{later[2]}",
                    summary=f"The supplier's promised delivery moved later by {days} days, "
                            f"from {earlier[1]} to {later[1]}",
                    missing="Whether the new promise still meets the orders this purchase "
                            "order supplies",
                    why=f"A confirmation of {later[0]} promises {later[1]}; the one of "
                        f"{earlier[0]} promised {earlier[1]} (both cited).",
                    priority="high" if days >= 7 else "medium",
                    about=("PurchaseOrder", "lateness"),
                    groups=[cites(earlier[3], earlier[4]), cites(later[3], later[4])],
                    event={"days": days, "date_was": earlier[1].isoformat(),
                           "date_now": later[1].isoformat()}))
    for made in gaps[revised:]:
        caveat(made)
    return result(gaps, not_evaluated=skipped)


class _OrderState:
    """The one PO reader's verdict on an order for a slip (``po_status``): closed or a
    draft by the shared status (a receipt in any copy closes it) is never a slip, nor is a
    date moved to or from a default. Without a reader (``orders`` None) nothing is
    skipped."""

    def __init__(self, orders: Any) -> None:
        self.src = orders
        self.read: dict[str, Any] = {}

    def no_slip(self, order: str, dated: Sequence[tuple[Mapping[str, Any], date]]
                ) -> str | None:
        if self.src is None:
            return None
        from . import po_status as ps
        if order not in self.read:
            try:
                status = ps.read(self.src, order, dates=False).status
            except Exception:  # noqa: BLE001 - an order the reader does not hold
                status = None
            copies = ps.copies(self.src, order)[0] if status is not None else [order]
            own: set[date] = set()
            for record in copies:
                own |= self.src.own_days(record)
            self.read[order] = (status, own)
        status, own = self.read[order]
        if status is not None and status.cls in (ps.CLOSED, ps.DRAFT):
            return "order_not_open"
        defaults = self.src.date_defaults()
        if any(ps.is_default_date(defaults, row, day, own) for row, day in dated):
            return "default_date"
        return None


LINE_PROMISES = "line_promises"
LINE_OWN = ("description", "item_code", "line_type")


def _line_slips(st: Any, several: Mapping[str, Sequence[tuple[str, str]]],
                found: Mapping[str, Any], settled: Sequence[str], *, gap: Callable,
                cites: Callable, skipped: Counter) -> list[dict[str, Any] | None]:
    """Per order line: two confirmations of one order, each promising the line's own item
    a date (``SupplierConfirmation.line_promises``, the line matched to the item on firm
    evidence only, ``operating.line_promises.match``), the later document promising that
    line a later arrival. Each event cites both confirmations' per-item promise and
    document date, and the line's own value it was matched by (each its own group)."""
    from operating import line_promises as lp

    lines: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in st.links(["PurchaseOrderLine.line_of"], settled):
        if link["to_object"] in several:
            lines[link["to_object"]].append((link["from_object"], link["markings"]))
    if not lines:
        return []
    own = st.values(["PurchaseOrderLine"], LINE_OWN,
                    objects=sorted({ln for ls in lines.values() for ln, _ in ls}))
    out: list[dict[str, Any] | None] = []
    for po, cs in sorted(several.items()):
        if not lines.get(po):
            continue
        props_of = {ln: (own.get(ln) or {}).get("props") or {} for ln, _ in lines[po]}
        line_marks = dict(lines[po])
        descriptors = {ln: lp.descriptor(lambda name, _p=props: [
            str(r["value"]) for r in _p.get(name, ()) if r.get("value") not in (None, "")])
            for ln, props in props_of.items()}
        # line -> [(issued, promised, confirmation, cited rows, link markings, basis)]
        promised: dict[str, list[tuple[date, date, str, list, str, str]]] = defaultdict(list)
        for conf, marks in cs:
            props = (found.get(conf) or {}).get("props") or {}
            row, issued = _one(props.get(LINE_PROMISES)), _one(props.get("document_date"))
            if row is None:
                continue
            day_issued = _day(issued["value"]) if issued is not None else None
            blocks = lp.blocks_of(row["value"])
            if day_issued is None or not blocks:
                skipped["line_promises_undated"] += 1
                continue
            hits, _rest = lp.match(descriptors, blocks)
            for ln, (index, basis) in hits.items():
                arrival = blocks[index].get("delivery_date")
                day = _day(arrival.get("value")) if isinstance(arrival, Mapping) and \
                    isinstance(arrival.get("value"), str) else None
                if day is not None:
                    promised[ln].append((day_issued, day, conf, [row, issued], marks, basis))
        for ln, dated in sorted(promised.items()):
            dated.sort(key=lambda d: (d[0], d[2]))
            for earlier, later in zip(dated, dated[1:]):
                if later[0] <= earlier[0] or later[1] <= earlier[1]:
                    continue
                days = (later[1] - earlier[1]).days
                # the line's own value it was matched by: the evidence of the match
                matched = [r for name in (("item_code", "line_type")
                                          if later[5] == "item_code" else ("description",))
                           for r in props_of[ln].get(name, ())]
                basis = later[5].replace("_", " ")
                out.append(gap(
                    "eta_slipped", "eta_slipped", po, "PurchaseOrder",
                    f"{earlier[2]}|{later[2]}|{ln}",
                    summary=f"The supplier's promised delivery of one line moved later by "
                            f"{days} days, from {earlier[1]} to {later[1]}",
                    missing="Whether the new promise for this line still meets the orders "
                            "it supplies",
                    why=f"A confirmation of {later[0]} promises {later[1]} for the line "
                        f"(matched to its item by {basis}); the one of {earlier[0]} promised "
                        f"{earlier[1]} (both cited).",
                    priority="high" if days >= 7 else "medium",
                    about=("PurchaseOrder", "lateness"),
                    groups=[cites(earlier[3], earlier[4]), cites(later[3], later[4]),
                            cites(matched, line_marks[ln])],
                    event={"days": days, "date_was": earlier[1].isoformat(),
                           "date_now": later[1].isoformat(), "line": ln,
                           "matched_by": later[5], "confirmation_was": earlier[2],
                           "confirmation_now": later[2]}))
    return out


# --------------------------------------------------------------------------- lots

def lot_attention(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                  result: Callable) -> dict[str, Any]:
    gaps: list = []
    skipped: Counter = Counter()
    _lots_near_expiry(st, as_of, gaps, skipped, gap=gap, cites=cites)
    _batches_on_hold(st, gaps, skipped, gap=gap, cites=cites)
    return result(gaps, not_evaluated=skipped)


def _lots_near_expiry(st: Any, as_of: date, gaps: list, skipped: Counter, *,
                      gap: Callable, cites: Callable) -> None:
    """A lot expiring within ``EXPIRY_DAYS`` is claimed only while its QC status says it
    holds stock: a lot used up, rejected or expired has nothing left to expire, and a lot
    whose status is missing, disputed or unmapped is not judged (``lot_status_*``)."""
    horizon = as_of + timedelta(days=EXPIRY_DAYS)
    for oid, entry in sorted(st.values(["Lot"], ("expiry_date", "lot_number",
                                                 "qc_status")).items()):
        props = entry["props"]
        expiry = _one(props.get("expiry_date"))
        if expiry is None:
            if props.get("expiry_date") and any(r.get("value") not in (None, "")
                                                for r in props["expiry_date"]):
                skipped["expiry_disputed"] += 1
            continue
        day = _day(expiry["value"])
        if day is None:
            skipped["expiry_unreadable"] += 1
            continue
        if not as_of <= day <= horizon:
            continue
        status = _one(props.get("qc_status"))
        if status is None:
            stated = [r for r in props.get("qc_status") or () if r.get("value") not in (None, "")]
            skipped["lot_status_disputed" if stated else "lot_status_unknown"] += 1
            continue
        stock = stock_state.lot_holds_stock(status["value"])
        if stock is False:
            continue
        if stock is None:
            skipped["lot_status_unmapped"] += 1
            continue
        days = (day - as_of).days
        number = _one(props.get("lot_number"))
        gaps.append(gap(
            "lot_near_expiry", "lot_attention", oid, "Lot", f"expiry|{day}",
            summary=f"Expires on {day}, " + ("today" if days == 0 else
                                               f"in {days} day{'' if days == 1 else 's'}"),
            missing="Whether this lot is used, reworked or written off before it expires",
            why=f"The lot's recorded expiry date is {day}, within {EXPIRY_DAYS} days, and "
                f"its QC status ({status['value']}) says it still holds stock.",
            priority="high" if days <= 7 else "medium", about=("Lot", "expiry_date"),
            groups=[cites([expiry]), cites([status])],
            extra=cites([number]) if number else (), event={"days": days}))


def _batches_on_hold(st: Any, gaps: list, skipped: Counter, *, gap: Callable,
                     cites: Callable) -> None:
    """A batch really on quality hold (``stock_state.batch_hold``: a hold word that is not
    its lifecycle state's default over every batch of its own system); a copy of a batch
    (``stock_state.BATCH_TWIN_LINK``) is decided by its origin and counted once, on it,
    the copy's values cited beside it."""
    batches = st.values(["Batch"], ("quality_status", "status", "lot_number"))
    read: dict[str, tuple] = {}
    for oid, entry in batches.items():
        props = entry["props"]
        read[oid] = (_one(props.get("quality_status")), _one(props.get("status")))
        if read[oid][0] is None and any(r.get("value") not in (None, "")
                                        for r in props.get("quality_status") or ()):
            skipped["quality_status_disputed"] += 1
    scoped = getattr(st, "scoped", False)

    def hold_profiles() -> tuple[dict[str, Any], Any]:
        # what every batch in each state carries per system: a population (all batches),
        # read whole in an incremental pass (``proactive_engine._Store.population``)
        held = read if not scoped else {
            oid: (_one(entry["props"].get("quality_status")),
                  _one(entry["props"].get("status")))
            for oid, entry in st.values(["Batch"], ("quality_status", "status")).items()}
        systems: dict[str, list] = defaultdict(list)
        for oid, (hold, status) in held.items():
            if hold is not None and status is not None:
                systems[ostore.object_segment(oid)].append((status["value"], hold["value"]))
        profiles = {system: stock_state.hold_profile(pairs)
                    for system, pairs in systems.items()}
        return profiles, stock_state.pooled(*profiles.values())
    population = getattr(st, "population", None)
    profiles, every = (population("batch_hold_profiles", hold_profiles)
                       if population is not None else hold_profiles())
    origins: set[str] = set()
    copies: dict[str, set[str]] = defaultdict(set)    # origin -> its copies
    for link in st.links([stock_state.BATCH_TWIN_LINK]):
        origin, copy = link["from_object"], link["to_object"]
        if origin in read and copy in read and origin != copy:
            origins.add(copy)
            copies[origin].add(copy)
    for oid in sorted(read):
        hold, status = read[oid]
        if oid in origins or hold is None:
            continue   # a copy: its origin decides, and it is counted once there
        verdict = stock_state.batch_hold(status["value"] if status else None, hold["value"],
                                         profiles.get(ostore.object_segment(oid), {}), every)
        if verdict in (stock_state.UNMAPPED, stock_state.UNPROFILED):
            skipped[verdict] += 1
        if verdict != stock_state.HOLD:
            continue
        made = stock_state.made(status["value"])
        twins = sorted(copies.get(oid, ()))
        gaps.append(gap(
            "lot_on_hold", "lot_attention", oid, "Batch", "hold",
            summary=("Made but never released: Quality still holds the batch" if made
                     else "On quality hold while the batch is still active"),
            missing="Why the batch is held and when Quality will decide its release",
            why=("The batch's status says it was made and its quality status is still a "
                 "hold (both cited)" if made else
                 "The batch's quality status is a hold and its status is not closed "
                 "(both cited)")
                + "; a hold is claimed only where it is not what every batch in that "
                  "state carries in its system.",
            priority="high", about=("Batch", "quality_status"),
            groups=[cites([hold]), cites([status])],
            extra=cites([row for twin in twins for row in read[twin] if row is not None])))


# --------------------------------------------------------------------------- quotes

def accepted_quote_no_order(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                            result: Callable) -> dict[str, Any]:
    gaps = []
    found = st.values(["Quote"], ("status", "accepted_at", "converted_sales_order_number",
                                  "quote_number", "total_amount", "currency"))
    converted = set()
    for link in st.links(list(QUOTE_ORDER_LINKS)):
        converted.add(link["from_object"] if link["link_type"] == "Quote.converted_to"
                      else link["to_object"])
    requires = sorted({r[0] for r in st.conn.execute(
        "SELECT DISTINCT audience_key FROM objects WHERE object_type = 'SalesOrder'")}) or \
        ["sales_order:none_stored"]
    for oid, entry in sorted(found.items()):
        props = entry["props"]
        status, accepted = _one(props.get("status")), _one(props.get("accepted_at"))
        evidence = [r for r in (status if _word(status) in ACCEPTED_WORDS else None,
                                accepted) if r is not None]
        if not evidence or oid in converted or _one(props.get("converted_sales_order_number")):
            continue
        when = _day(accepted["value"]) if accepted else None
        made = gap(
            "quote_accepted_no_order", "accepted_quote_no_order", oid, "Quote", "no_order",
            summary="Accepted" + (f" on {when}" if when else "") + " with no sales order "
                    "converted from it",
            missing="Whether the customer's order is coming, or why the quote was not "
                    "converted",
            why="The quote is recorded accepted and no sales order is linked to it or "
                "names its conversion.",
            priority="high", about=("Quote", "status"),
            groups=[cites(evidence)],
            event={"days": (as_of - when).days} if when and when <= as_of else None)
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    return result(gaps)


# --------------------------------------------------------------------------- invoices

def invoice_quantity_mismatch(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                              result: Callable, settled: Sequence[str],
                              orders: Any = None) -> dict[str, Any]:
    """Per purchase order and item, billed above received. What the order received is
    read on every copy of it (``orders``: the one PO reader, ``po_status.copies``): the
    invoice's own order record and every other system's record of the same order, each
    copy's received quantity on its own (copies of one receipt are never added); billed
    above what every readable copy received, in one unit, is a mismatch, cited by the copy
    that received the most."""
    gaps = []
    skipped: Counter = Counter()
    links = st.links(list(INVOICE_LINKS), settled)
    by_type: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for link in links:
        by_type[link["link_type"]].append(link)
    invoice_po = defaultdict(list)
    for link in by_type["PurchaseInvoice.invoices"]:
        invoice_po[link["from_object"]].append(link)
    if not invoice_po:
        return result([])
    inv_lines = defaultdict(list)
    for link in by_type["PurchaseInvoiceLine.line_of"]:
        if link["to_object"] in invoice_po:
            inv_lines[link["to_object"]].append(link)
    item_of = {l["from_object"]: l for t in ("PurchaseInvoiceLine.for_item",
                                             "PurchaseOrderLine.for_item")
               for l in by_type[t]}
    pos = {l["to_object"] for ls in invoice_po.values() for l in ls}
    from . import po_status as ps
    copies_of = {po: (ps.copies(orders, po)[0] if orders is not None else [po])
                 for po in sorted(pos)}
    pos |= {c for found in copies_of.values() for c in found}
    po_lines = defaultdict(list)
    for link in by_type["PurchaseOrderLine.line_of"]:
        if link["to_object"] in pos:
            po_lines[link["to_object"]].append(link)
    values = st.values(["PurchaseInvoice", "PurchaseInvoiceLine", "PurchaseOrderLine"],
                       ("status", "invoiced_quantity", "received_quantity"),
                       objects=sorted(set(invoice_po) | {l["from_object"] for ls in
                                                         inv_lines.values() for l in ls}
                                      | {l["from_object"] for ls in po_lines.values()
                                         for l in ls}))

    def props(oid: str) -> Mapping[str, Any]:
        return (values.get(oid) or {}).get("props") or {}

    billed: dict[tuple[str, str], list] = defaultdict(list)    # (po, item) -> rows, marks
    for invoice, to_po in invoice_po.items():
        if len(to_po) != 1:
            skipped["invoice_of_several_orders"] += 1
            continue
        status = _one(props(invoice).get("status"))
        if _word(status) not in POSTED_INVOICE:
            continue
        for line in inv_lines.get(invoice, ()):
            item = item_of.get(line["from_object"])
            qty = _one(props(line["from_object"]).get("invoiced_quantity"))
            if item is None or qty is None:
                continue
            billed[(to_po[0]["to_object"], item["to_object"])].append(
                (qty, status, [to_po[0]["markings"], line["markings"], item["markings"]]))
    for (po, item), rows in sorted(billed.items()):
        # each copy's received quantity of the item, read whole in one unit, else None
        # (a copy whose quantity is not readable may hold the receipt: none is judged)
        per_copy: list | None = []
        for copy in copies_of.get(po, [po]):
            got: list | None = []
            for line in po_lines.get(copy, ()):
                line_item = item_of.get(line["from_object"])
                if line_item is None or line_item["to_object"] != item:
                    continue
                qty = _one(props(line["from_object"]).get("received_quantity"))
                if qty is None:
                    got = None
                    break
                got.append((qty, [line["markings"], line_item["markings"]]))
            if got is None:
                per_copy = None
                break
            if got:
                per_copy.append(got)
        if not per_copy:
            skipped["receipt_quantity_unknown"] += 1
            continue
        units = {unit_token(r[0].get("unit")) for r in rows} | {
            unit_token(q.get("unit")) for got in per_copy for q, _ in got}
        if len(units) != 1 or None in units or "" in units:
            skipped["unit_not_stated"] += 1
            continue
        if any(_decimal(q["value"]) is None for got in per_copy for q, _ in got):
            skipped["quantity_unreadable"] += 1
            continue
        received = max(per_copy, key=lambda got: sum(
            (_decimal(q["value"]) for q, _ in got), Decimal(0)))
        total_billed = sum((_decimal(q["value"]) or Decimal(0) for q, _, _ in rows),
                           Decimal(0))
        total_received = sum((_decimal(q["value"]) or Decimal(0) for q, _ in received),
                             Decimal(0))
        if any(_decimal(q["value"]) is None for q, _, _ in rows) or any(
                _decimal(q["value"]) is None for q, _ in received):
            skipped["quantity_unreadable"] += 1
            continue
        if total_billed <= total_received:
            continue
        unit = next(iter(units))
        gaps.append(gap(
            "invoice_quantity_mismatch", "invoice_quantity_mismatch", po, "PurchaseOrder",
            f"invoice|{item}",
            summary=f"Invoiced {total_billed} {unit} of an item, received {total_received} "
                    f"{unit}",
            missing="Whether the supplier billed goods not yet received",
            why="The posted supplier invoices of this purchase order bill more of the item "
                "than any record of the order records received (each quantity cited).",
            priority="high", about=("PurchaseOrder", "invoice"),
            groups=[cites([q, s], *m) for q, s, m in rows]
                   + [cites([q], *m) for q, m in received]))
    return result(gaps, not_evaluated=skipped)


# --------------------------------------------------------------------------- tool calls

def _recent_mail_orders(st: Any, as_of: date) -> list[str]:
    """Purchase orders named by mail sent within ``MAIL_DAYS``, most recently named first."""
    since = (as_of - timedelta(days=MAIL_DAYS)).isoformat()
    rows = st.conn.execute(
        "SELECT l.to_object, max(v.value) AS last FROM links l JOIN object_values v ON "
        "v.object_id = l.from_object AND v.property = 'sent_at' AND v.superseded_at IS NULL "
        "WHERE l.link_type = 'Email.mentions_purchase_order' AND l.superseded_at IS NULL "
        "AND l.tier <> 'contradicted' AND v.value >= :since AND v.value <= :until "
        "GROUP BY l.to_object ORDER BY last DESC, l.to_object",
        {"since": since, "until": (as_of + timedelta(days=1)).isoformat()}).fetchall()
    return [r[0] for r in rows]


def _entry_handles(entry: Mapping[str, Any]) -> list[str]:
    out = []
    value = entry.get("value")
    if isinstance(value, Mapping):
        origin = value.get("origin") or {}
        if isinstance(origin.get("citation"), str):
            out.append(origin["citation"])
    citation = entry.get("citation")
    if isinstance(citation, Mapping) and isinstance(citation.get("l1_handle"), str):
        out.append(citation["l1_handle"])
    return out


def _investigate(call: Callable, family: str, root: str) -> Mapping[str, Any] | None:
    try:
        found = call("plx.investigate", {"family": family, "root": root})
    except Exception as error:  # noqa: BLE001 - one root fails alone
        if type(error).__name__ == "TimeBudgetSpent":
            raise
        return None
    return found if isinstance(found, Mapping) and not found.get("error") else None


class _MailIndex:
    """The mail the vendor checks rest on, read once per run in three plain scans (each
    message's conversation and sent time, the mail naming each purchase order): a
    per-order self-join over the value cells took minutes per order on the live store."""

    def __init__(self, st: Any, roots: Sequence[str]) -> None:
        self.st = st
        self.conv: dict[str, str] = {}
        self.members: dict[str, set[str]] = defaultdict(set)
        for oid, value in st.conn.execute(
                "SELECT object_id, value FROM object_values WHERE property = "
                "'conversation_id' AND superseded_at IS NULL"):
            if value not in (None, ""):
                self.conv[oid] = str(value)
                self.members[str(value)].add(oid)
        self.sent = {oid: str(value) for oid, value in st.conn.execute(
            "SELECT object_id, value FROM object_values WHERE property = 'sent_at' AND "
            "superseded_at IS NULL") if value not in (None, "")}
        wanted = set(roots)
        self.mail: dict[str, set[str]] = defaultdict(set)
        for link in st.links([MAIL_PO_LINK], held=False):
            if link["to_object"] in wanted:
                self.mail[link["to_object"]].add(link["from_object"])

    def thread(self, mail: Iterable[str]) -> set[str]:
        """These messages and every stored message of their conversations."""
        out = set(mail)
        for oid in list(out):
            if oid in self.conv:
                out |= self.members[self.conv[oid]]
        return out

    def state(self, root: str) -> tuple[str, str | None]:
        """What the vendor-mail checks of one purchase order rest on, as a fingerprint:
        the current versions of the order, the messages naming it and every stored message
        of their conversations (an arriving reply changes it); and the newest message's
        sent time."""
        messages = self.thread(self.mail.get(root, ()))
        ids = sorted({root, *messages})
        rows = self.st.conn.execute(
            "SELECT object_id, l1_handle FROM object_versions WHERE superseded_at IS NULL AND "
            "object_id IN (SELECT value FROM json_each(?)) ORDER BY object_id, l1_handle",
            (json.dumps(ids),)).fetchall()
        newest = max((self.sent[m] for m in messages if m in self.sent), default=None)
        body = json.dumps([CACHE_VERSION, [list(r) for r in rows]])
        return hashlib.sha256(body.encode("utf-8")).hexdigest(), newest

    def audiences(self, mail: Sequence[str]) -> list[str]:
        """Every audience the stored messages of these messages' conversations sit in."""
        ids = sorted(self.thread(mail))
        rows = self.st.conn.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_id IN (SELECT value FROM "
            "json_each(?))", (json.dumps(ids),)).fetchall()
        return sorted(r[0] for r in rows)


def _cache_valid(entry: Any, fp: str, newest: str | None, as_of: date, family: str) -> bool:
    """A root's last reading still stands: same fingerprint, read within ``CACHE_DAYS``,
    and (F28) no message of the thread has crossed ``NO_REPLY_DAYS`` since it was read."""
    if not isinstance(entry, Mapping) or entry.get("fp") != fp:
        return False
    read_on = _day(entry.get("as_of"))
    if read_on is None or read_on > as_of or (as_of - read_on).days >= CACHE_DAYS:
        return False
    sent = _day(newest)
    if family == "F28" and sent is not None and \
            read_on < sent + timedelta(days=NO_REPLY_DAYS) <= as_of:
        return False   # a request waiting fewer days then may be overdue now
    return True


def _findings(run: Mapping[str, Any], family: str, skipped: Counter) -> list[dict[str, Any]]:
    """What one investigation found, as the inputs of a gap (cached as they are)."""
    out = []
    for entry in run.get("answer") or ():
        name = str(entry.get("name") or "")
        if entry.get("kind") == "computed" and not entry.get("basis") \
                and entry.get("value") is None and any(
                    "not decided" in str(step) for step in entry.get("steps") or ()):
            skipped["not_decided"] += 1    # e.g. no usable people exclusion: counted
            continue
        if entry.get("kind") != "computed" or name.endswith(("_draft", "_request")) \
                or not entry.get("basis") or entry.get("lead_basis"):
            continue
        basis = [b for b in entry["basis"] if isinstance(b, str)]
        if family == "F28":
            if isinstance(entry.get("value"), int):
                out.append({"basis": basis, "waited": entry["value"]})
            continue
        steps = [str(s.get("text") if isinstance(s, Mapping) else s)
                 for s in entry.get("steps") or ()]
        proposed = next((s for s in steps if s.startswith("proposed update")), None)
        kind = None if proposed is None else "differs" if "(differs)" in proposed else "fills"
        # the term's field (``vendor_terms._shown``): a price compares a marked system price
        price = proposed is not None and proposed.split(": ", 1)[-1].startswith("price")
        handles = sorted(set(_entry_handles(entry)))
        if kind is not None and handles:
            out.append({"name": name, "basis": basis, "handles": handles, "kind": kind,
                        "price": price})
    return out


def _mail_gap(st: Any, root: str, family: str, finding: Mapping[str, Any], *, gap: Callable,
              cites: Callable, skipped: Counter, index: "_MailIndex") -> dict[str, Any] | None:
    detector = "vendor_no_reply" if family == "F28" else "vendor_terms_unapplied"
    basis = list(finding["basis"])
    rows = st.versions(objects=basis)
    if {r["object_id"] for r in rows} != set(basis):
        skipped["basis_not_stored"] += 1
        return None
    by_object = {r["object_id"]: r for r in rows}
    if family == "F28":
        waited = int(finding["waited"])
        made = gap(
            "vendor_no_reply", detector, root, "PurchaseOrder", f"{basis[0]}",
            summary=f"A request to the vendor has had no reply for {waited} days",
            missing="Whether the vendor answered outside the mail read here; follow "
                    "up if not (a draft is in the investigation, nothing is sent)",
            why="Our last message asking the vendor for a quote, price or "
                f"confirmation is {waited} days old and no later message from "
                "outside is in its conversation (the request is cited).",
            priority="high" if waited >= 14 else "medium",
            about=("PurchaseOrder", "confirmation"),
            groups=[cites([by_object[basis[0]]])], extra=cites([
                by_object[b] for b in basis[1:]]),
            event={"days": waited})
        if made is not None:
            made["requires"] = index.audiences(basis[:1])
        return made
    reply = list(st.versions(handles=finding["handles"]))
    if not reply:
        skipped["reply_not_stored"] += 1
        return None
    word = "differs from" if finding["kind"] == "differs" else "fills an empty"
    # The verdict tells what the system's field holds: each system record is cited at its
    # values' markings too, and a price (an empty one included) at purchasing_prices.
    marks = _value_markings(st, basis[1:], key="object_id")
    price = ['["purchasing_prices"]'] if finding.get("price", True) else []
    return gap(
        "vendor_terms_unapplied", detector, root, "PurchaseOrder",
        f"{finding['name']}|{finding['handles'][0]}",
        summary=f"A vendor reply offers a term that {word} system field, not applied",
        missing="Whether the offered term should be entered in the Portal (a "
                "person approves and enters it; nothing is written here)",
        why="A reply from the vendor states a term the system does not hold as "
            "stated (the reply and the record are cited).",
        priority="medium", about=("PurchaseOrder", "confirmation"),
        groups=[cites(reply), cites([by_object[basis[0]]])] + [
            cites([by_object[b]], *marks.get(b, ()), *price) for b in basis[1:]])


def vendor_mail(st: Any, as_of: date, call: Callable | None, *, gap: Callable,
                cites: Callable, result: Callable, family: str, bound: int = CALL_BOUND,
                cache: Mapping[str, Any] | None = None,
                pmap: Callable[[Callable, Sequence[Any]], list[Any]] | None = None
                ) -> dict[str, Any]:
    """F28 (``vendor_no_reply``) or F27 (``vendor_terms_unapplied``) over the purchase
    orders mail named recently, incrementally: a purchase order whose thread is unchanged
    since its last reading (``_cache_valid``) keeps that reading (no investigation; F28's
    days waiting counted on from it); only changed ones are investigated, newest mail
    first, at most ``bound`` per run. The result's ``cache`` holds every reading.
    ``pmap``: the engine's process pool (``operating.proactive_pool``): every changed one
    is investigated on its workers (a positive ``bound`` no longer cuts; the budget does),
    one not reached in time is read on a later run (``time_budget``)."""
    if call is None:
        return result([], status="not_configured", reasons=["tool_caller_not_given"])
    roots = _recent_mail_orders(st, as_of)
    cache = cache or {}
    gaps, skipped, kept = [], Counter(), {}
    stale: list[tuple[str, str, str | None]] = []
    index = _MailIndex(st, roots)
    for root in roots:
        fp, newest = index.state(root)
        entry = cache.get(root)
        if not _cache_valid(entry, fp, newest, as_of, family):
            stale.append((root, fp, newest))
            continue
        kept[root] = entry
        aged = (as_of - _day(entry["as_of"])).days
        for finding in entry.get("findings") or ():
            if family == "F28":
                finding = {**finding, "waited": int(finding["waited"]) + aged}
            gaps.append(_mail_gap(st, root, family, finding, gap=gap, cites=cites,
                                  skipped=skipped, index=index))
        skipped.update(entry.get("skipped") or {})
    spent = False
    asked = stale if pmap is not None and bound > 0 else stale[:bound]
    pooled: list[Any] | None = None
    if pmap is not None and asked:
        from .proactive_pool import NOT_READ, tool_call
        pooled = pmap(tool_call, [("plx.investigate", {"family": family, "root": root})
                                  for root, _fp, _newest in asked])
    for n, (root, fp, newest) in enumerate(asked):
        if pooled is not None:
            answer = pooled[n]
            if answer is NOT_READ or (isinstance(answer, Mapping) and "time_budget_exhausted"
                                      in ((answer.get("answerability") or {}).get("reasons")
                                          or ())):
                spent = True   # read on a later run
                continue
            run = answer if isinstance(answer, Mapping) and not answer.get("error") else None
        else:
            try:
                run = _investigate(call, family, root)
            except Exception as error:  # noqa: BLE001 - only the budget stops the scan
                if type(error).__name__ != "TimeBudgetSpent":
                    raise
                spent = True    # what was found stands; the rest is read on a later run
                break
        if run is None:
            skipped["run_refused"] += 1
            continue
        mine = Counter()
        findings = _findings(run, family, mine)
        kept[root] = {"fp": fp, "as_of": as_of.isoformat(), "findings": findings,
                      "skipped": dict(mine)}
        skipped.update(mine)
        for finding in findings:
            gaps.append(_mail_gap(st, root, family, finding, gap=gap, cites=cites,
                                  skipped=skipped, index=index))
    out = _bounded(result, gaps, skipped, more=len(stale) > len(asked), spent=spent)
    out["coverage"] = {"unit": "purchase_orders_named_by_recent_mail", "total": len(roots),
                       "evaluated": len(kept), "not_evaluated": {k: v for k, v in (
                           ("not_read_this_run", len(roots) - len(kept)),) if v}}
    if bound == 0 and stale:   # the build's run: the vendor-mail pass reads them apart
        out["reasons"] = [APART_REASON]
    out["cache"], out["roots"] = kept, list(roots)
    out["reused"] = len(roots) - len(stale)
    return out


# --------------------------------------------------------------------------- vendor_no_reply

NO_REPLY_WINDOW_DAYS = 90   # our request is looked at when sent within this many days
MAX_NAMED_RECORDS = 4       # a message naming more purchase order records is a report
# What a message of ours asks a vendor beyond a quote or price: when it ships or arrives.
_ETA_WORDS = re.compile(r"\b(?:eta|etas|delivery|deliver(?:ed|ing)?|ship(?:s|ping|ment|ped)?|"
                        r"dispatch(?:ed)?|arriv(?:e|al|ing)|tracking|status|update)\b", re.I)


# Subject prefixes (stacked, any case, with "[External]" tags between): a reply, a forward.
_PREFIXES = r"^\s*(?:(?:\[[^\]]{1,30}\]|[A-Za-z]{2,4}(?:\[\d+\])?\s*[:：]|回复[:：]|答复[:：]|转发[:：])\s*)*"
_REPLY = re.compile(_PREFIXES[:-1] + r"?(?:RE|AW|SV|VS|R|RIF|ANTW|回复|答复)\s*[:：]", re.I)
_FORWARD = re.compile(_PREFIXES + r"(?:FW|FWD|WG|TR|RV|转发)\s*[:：]", re.I)
_FORWARDED = re.compile(r"forwarded message|begin forwarded message", re.I)
# One reply or forward tag ("RE:", "Fwd:", "AW:", "SV:", "Re[2]:", any case) and a run of them.
_TAG = r"(re|fwd?|aw|sv)\s*(?:\[\d+\])?\s*[:：]"
_TAGS = re.compile(rf"^\s*{_TAG}(?:\s*{_TAG})*\s*", re.I)


def reply_subject(subject: Any) -> str | None:
    """The subject a reply to a message of ``subject`` carries: one tag, never stacked. A
    subject with no reply or forward tag gets "Re: "; one that starts with tags keeps its
    first tag alone ("RE: RE: x" is "RE: x", "Re: FW: x" is "Re: x"). None or blank: None."""
    if not isinstance(subject, str) or not subject.strip():
        return None
    found = _TAGS.match(subject)
    if found is None:
        return f"Re: {subject.strip()}"
    rest = subject[found.end():].strip()
    return f"{found.group(1)}: {rest}" if rest else f"{found.group(1)}:"


def asks_vendor(text: Any) -> bool:
    """Whether a message's own text (quoted history removed) asks: a term or ETA word and
    a request word in one sentence (``operating.vendor_terms.asks_for_terms`` for the terms
    a vendor states, here also when it ships or arrives)."""
    from ontology.mentions import QuotedRanges

    from .vendor_terms import _REQUEST_WORDS, asks_for_terms

    if not isinstance(text, str) or not text.strip():
        return False
    if asks_for_terms(text):
        return True
    quoted = QuotedRanges(text)
    own = "".join(ch for i, ch in enumerate(text) if not quoted.contains(i))
    return any(_ETA_WORDS.search(sentence) and _REQUEST_WORDS.search(sentence)
               for sentence in re.split(r"(?<=[.!?\n])", own))


def _instant(raw: Any) -> datetime | None:
    try:
        text = str(raw).strip()
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def recipients(row: Mapping[str, Any]) -> list[str] | None | bool:
    """A message's To and Cc addresses (``to_addresses``, ``cc_addresses``: each one JSON
    list of addresses), lower-case: None when neither is projected (an older build), False
    when one is unreadable (not a list of addresses: its people cannot be cleared)."""
    found: list[str] = []
    held = False
    for prop in ("to_addresses", "cc_addresses"):
        raw = row.get(prop)
        if raw in (None, ""):
            continue
        held = True
        try:
            listed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return False
        if not isinstance(listed, list) or not all(
                isinstance(a, str) and "@" in a for a in listed):
            return False
        found.extend(a.strip().casefold() for a in listed)
    return found if held else None


def vendor_no_reply(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                    result: Callable, people: Any, orders: Mapping[str, tuple[str, str]],
                    order_key: str = "order",
                    colliding: Callable[[str], bool] = lambda _oid: False,
                    resolver: Any = None) -> dict[str, Any]:
    """F28 over the whole store, set-based: every OPEN purchase order (``orders``: record ->
    (its order across systems, that order's class), the late rule's join) whose mail shows
    a request of ours to the outside, within ``NO_REPLY_WINDOW_DAYS``, unanswered for
    ``NO_REPLY_DAYS`` or more.

    Read in three plain scans (each stored message's conversation, sent time, sender,
    subject and preview; the mailbox each copy sits in; the mail naming each purchase
    order, never contradicted, from messages naming at most ``MAX_NAMED_RECORDS`` order
    records). People first (``people``: ``operating.chunking.permissions.Exclusion``, the
    guard plus the mail capture's directory and tenant domains): a copy in a mailbox the
    exclusion does not clear is dropped unread (never a reply, never cited); a conversation
    where a person of ours the exclusion does not clear wrote is not judged
    (``participants_not_cleared``); with the guard unavailable or no tenant domain known
    the rule is ``blocked`` (fail closed). Ours: a sender in a tenant domain; outside: any
    other. Per conversation naming the order: our last message, if it asks (``asks_vendor``
    on its subject and preview) and the outside wrote in that conversation before it, with
    no later message from the outside, is a request waiting since it was sent. One gap per
    order (its longest wait), citing our message; it requires a grant on every audience the
    conversation's cleared copies sit in (the absence of a reply is the run's verdict). The
    rule's ``coverage``: open orders, judged (some cleared conversation names them) and not
    (by reason). ``colliding(record)``: the order's number is another order's too
    (``po_status.StoreSource.colliding``): a number-text mention of it (a lead link) may be
    the other order's, so it ties no message to this order, unless ``resolver``
    (``ontology.po_mention_resolve.Resolver``) picks the one order it is about from the
    message's cleared conversation (its outside senders' supplier, its date in the order's
    life): the pick stays a lead, and ``mention_resolution`` counts the links by reason."""
    if people is None or getattr(people, "unavailable", None):
        # the guard's own code (an enum, never an ID) says why: no list, a bad entry, ...
        cause = getattr(people, "unavailable", None) or "exclusion_not_loaded"
        return result([], status="blocked", reasons=["people_exclusion_unavailable", cause])
    domains = tuple(getattr(people, "internal", ()) or ())
    if not domains:
        return result([], status="blocked", reasons=["tenant_domains_unknown"])

    def internal(address: str) -> bool:
        domain = address.rsplit("@", 1)[1] if "@" in address else ""
        return bool(domain) and any(domain == d or domain.endswith("." + d) for d in domains)

    open_order: dict[str, str] = {m: order for m, (order, cls) in orders.items()
                                  if cls == "open"}
    open_orders = set(open_order.values())
    members: dict[str, list[str]] = defaultdict(list)
    for record, order in sorted(open_order.items()):
        members[order].append(record)
    # the mailboxes each message copy sits in, cleared first: a copy elsewhere is never read
    clear_units: dict[str, bool] = {}
    audience: dict[str, str] = {}
    for oid, key in st.conn.execute(
            "SELECT o.object_id, o.audience_key FROM objects o WHERE o.object_type = 'Email' "
            "AND NOT " + ostore.removed_at_sql("o.object_id")):
        if key not in clear_units:
            clear_units[key] = bool(people.unit_clear(key))
        if clear_units[key]:
            audience[oid] = key
    named: dict[str, set[str]] = defaultdict(set)
    lead: dict[tuple[str, str], bool] = {}
    reused_by: dict[str, set[str]] = defaultdict(set)
    for link in st.links([MAIL_PO_LINK], held=False):
        mail, po, tier = link["from_object"], link["to_object"], link["tier"]
        if tier not in ("verified", "reviewed", "corroborated") and colliding(po):
            if mail in audience:   # a reused number's mention: this order's only if resolved
                reused_by[mail].add(po)
            continue
        if mail in audience:
            named[mail].add(po)
            lead[(mail, po)] = lead.get((mail, po), True) and tier not in (
                "verified", "reviewed", "corroborated")
    values: dict[str, dict[str, str]] = defaultdict(dict)
    for oid, value in st.conn.execute(
            "SELECT object_id, value FROM object_values WHERE property = 'conversation_id' "
            "AND superseded_at IS NULL AND object_id LIKE 'Email:%'"):
        if oid in audience and value not in (None, ""):
            values[oid]["conversation_id"] = str(value)
    resolution: Counter = Counter()
    reused = _resolve_reused(st, reused_by, values, resolver, named, lead, resolution,
                             getattr(resolver, "trail", None))
    # the rest of a message is read only for the conversations a message naming an open
    # order (few enough records) sits in: every copy of each, cleared (each a scan of the
    # store's whole mail before, minutes on the live store)
    asked = {values.get(mail, {}).get("conversation_id") for mail, pos in named.items()
             if len(pos) <= MAX_NAMED_RECORDS and any(p in open_order for p in pos)}
    wanted = sorted(oid for oid, row in values.items() if row["conversation_id"] in asked)
    for start in range(0, len(wanted), 500):
        for oid, prop, value in st.conn.execute(
                "SELECT object_id, property, value FROM object_values WHERE superseded_at IS "
                "NULL AND property IN ('sent_at', 'sender', 'subject', 'body_preview', "
                "'to_addresses', 'cc_addresses') AND "
                "object_id IN (SELECT value FROM json_each(?))",
                (json.dumps(wanted[start:start + 500]),)):
            if value not in (None, ""):
                values[oid][prop] = str(value)
    threads: dict[str, list[str]] = defaultdict(list)
    copies: dict[str, list[str]] = defaultdict(list)   # every copy, dated or not
    for oid, row in values.items():
        if "conversation_id" in row:
            copies[row["conversation_id"]].append(oid)
            if _instant(row.get("sent_at")) is not None:
                threads[row["conversation_id"]].append(oid)
    since = as_of - timedelta(days=NO_REPLY_WINDOW_DAYS)
    skipped: Counter = Counter()
    if reused:
        skipped["mention_of_reused_number"] += reused
    judged: set[str] = set()
    uncleared: set[str] = set()
    outcomes: Counter = Counter()   # each judged (order, conversation) by its outcome

    def addressed_out(oid: str, found: list[str] | None, outside: list[str]) -> bool:
        if found:
            return any(not internal(a) for a in found)
        moment = _instant(values[oid]["sent_at"])
        return any(_instant(values[o]["sent_at"]) < moment for o in outside)

    waiting: dict[str, tuple[int, str, str, bool, list[str]]] = {}
    seen: set[tuple[str, str]] = set()
    for mail, pos in named.items():
        if len(pos) > MAX_NAMED_RECORDS:
            continue   # a report listing many orders is no request about one
        orders_named = {open_order[p] for p in pos if p in open_order}
        conversation = values.get(mail, {}).get("conversation_id")
        if not orders_named or conversation is None:
            continue
        for order in sorted(orders_named):
            if (order, conversation) in seen:
                continue
            seen.add((order, conversation))
            thread = sorted(threads.get(conversation, ()),
                            key=lambda o: (_instant(values[o]["sent_at"]), o))
            # every copy of the conversation, an undated one included, is cleared first:
            # each sender and each To/Cc recipient of ours
            senders = {o: (values[o].get("sender") or "").strip().casefold()
                       for o in copies.get(conversation, ())}
            told = {o: recipients(values[o]) for o in copies.get(conversation, ())}
            if any(not s or "@" not in s or (internal(s) and not people.identity_clear(s))
                   for s in senders.values()) or any(
                    found is False or any(internal(a) and not people.identity_clear(a)
                                          for a in found or ())
                    for found in told.values()):
                uncleared.add(order)   # someone of ours not cleared (or no sender): unjudged
                continue
            judged.add(order)
            ours = [o for o in thread if internal(senders[o])]
            outside = [o for o in thread if not internal(senders[o])]
            if not ours:
                outcomes["no_message_of_ours"] += 1
                continue
            # a message of ours is to the outside when an outside address is among its
            # recipients; with no recipients projected, when the outside wrote before it
            requests = [o for o in ours if addressed_out(o, told[o], outside)]
            if not requests:
                outcomes["addressed_internally" if any(told[o] for o in ours)
                         else "never_with_the_outside"] += 1
                continue
            last = requests[-1]
            sent = _instant(values[last]["sent_at"])
            if any(_instant(values[o]["sent_at"]) > sent for o in outside):
                outcomes["answered"] += 1
                continue
            waited = (as_of - sent.date()).days
            if waited < NO_REPLY_DAYS:
                outcomes["waiting_under_threshold"] += 1
                continue
            if sent.date() < since:
                outcomes["sent_before_window"] += 1
                continue
            row = values[last]
            subject = row.get("subject") or ""
            if not told[last] and (_FORWARD.match(subject)
                                   or _FORWARDED.search(row.get("body_preview") or "")):
                # who a forward went to is not projected (no To/Cc): an internal forward to
                # a colleague is never read as a request to the vendor
                skipped["forward_not_judged"] += 1
                continue
            # the message's own text asks; a reply keeps the thread's subject, so the
            # subject counts only on a message that is no reply
            if not (asks_vendor(row.get("body_preview"))
                    or (not _REPLY.match(subject) and asks_vendor(subject))):
                skipped["last_message_asks_nothing"] += 1
                continue
            outcomes["waiting"] += 1
            rep = members[order][0]
            # firm when some message of the conversation names one of the order's records
            # by a settled or corroborated link; else the order is named by a lead only
            is_lead = not any(not lead.get((m, r), True) for m in thread
                              for r in members[order])
            before = waiting.get(order)
            if before is None or waited > before[0]:
                waiting[order] = (waited, last, rep, is_lead, thread)
    gaps = []
    for order, (waited, last, rep, is_lead, thread) in sorted(waiting.items()):
        rows = st.versions(objects=[last])
        if not rows:
            skipped["basis_not_stored"] += 1
            continue
        sent = values[last]["sent_at"][:10]
        made = gap(
            "vendor_no_reply", "vendor_no_reply", rep, "PurchaseOrder", last,
            summary=("Lead, not firm: " if is_lead else "")
                    + f"Our request to the vendor of {sent} has had no reply for {waited} days",
            missing="Whether the vendor answered outside the mail read here, or when it "
                    "will confirm; follow up if not (nothing is sent)",
            why=f"Our last message to the outside about this order (an outside recipient, "
                f"or a conversation the outside wrote in before it), sent {sent}, asks for a quote, price, confirmation or delivery date, and "
                f"no later message from outside is in its conversation for {waited} days"
                + (" (the message names the order by a mention, not a confirmed link)"
                   if is_lead else "") + " (the request is cited).",
            priority=("high" if waited >= 14 else "medium") if not is_lead else "low",
            about=("PurchaseOrder", "confirmation"), groups=[cites(rows)],
            event={"days": waited, "sent": sent})
        if made is None:
            skipped["request_uncited"] += 1
            continue
        made["requires"] = sorted({audience[o] for o in thread if o in audience})
        made["follow_up"][order_key] = {"key": order, "rank": [0, rep]}
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    unjudged = open_orders - judged
    out["coverage"] = {"unit": "open_purchase_orders", "total": len(open_orders),
                       "evaluated": len(judged),
                       "not_evaluated": {k: v for k, v in sorted({
                           "participants_not_cleared": len(uncleared - judged),
                           "no_cleared_mail_names_it": len(unjudged - uncleared)}.items())
                           if v},
                       # each (order, conversation) judged, by its outcome: why no alert
                       "conversations": dict(sorted(outcomes.items()))}
    if resolution:
        out["mention_resolution"] = dict(sorted(resolution.items()))
    return out


def _resolve_reused(st: Any, reused_by: Mapping[str, set[str]],
                    values: dict[str, dict[str, str]], resolver: Any,
                    named: dict[str, set[str]], lead: dict[tuple[str, str], bool],
                    resolution: Counter, trail: list | None = None) -> int:
    """The lead links on reused numbers (``reused_by``: cleared message -> records) that
    ``resolver`` ties to one order, added to ``named`` as leads; returns how many links stay
    unresolved. Per message and number: its sent time and the senders of its cleared
    conversation (``values``: cleared messages only) are the evidence; ``resolution``
    counts links by reason (``resolved:<reason>``, ``ambiguous:<reason>``); ``trail``, when
    given, gets ``(message, record, reason, picked, number)`` per link (ids, reasons)."""
    if not reused_by:
        return 0
    if resolver is None:
        resolution["ambiguous:no_resolver"] += sum(len(p) for p in reused_by.values())
        return sum(len(p) for p in reused_by.values())
    conversations = {values.get(m, {}).get("conversation_id") for m in reused_by} - {None}
    peers = sorted({oid for oid, row in values.items()
                    if row.get("conversation_id") in conversations} | set(reused_by))
    for start in range(0, len(peers), 500):
        for oid, prop, value in st.conn.execute(
                "SELECT object_id, property, value FROM object_values WHERE superseded_at IS "
                "NULL AND property IN ('sent_at', 'sender') AND "
                "object_id IN (SELECT value FROM json_each(?))",
                (json.dumps(peers[start:start + 500]),)):
            if value not in (None, "") and oid in values:
                values[oid][prop] = str(value)
    in_thread: dict[str, list[str]] = defaultdict(list)
    for oid in peers:
        conversation = values.get(oid, {}).get("conversation_id")
        if conversation is not None:
            in_thread[conversation].append(oid)
    cited = _lead_numbers(st, reused_by)
    left = 0
    for mail, records in sorted(reused_by.items()):
        row = values.get(mail, {})
        thread = in_thread.get(row.get("conversation_id"), []) or [mail]
        found = resolver.mail(row.get("sent_at"), {o: values.get(o, {}).get("sender")
                                                    for o in {mail, *thread}})
        chosen: set[str] = set()
        # the numbers the message's own leads cite (each lead's mention), never a number
        # a linked record merely also holds
        by_number: dict[str, set[str]] = defaultdict(set)
        for record in records:
            for number in cited.get((mail, record), ()):
                by_number[number].add(record)
        for number, linked in sorted(by_number.items()):
            picked = resolver.resolve_linked(number, found, linked)
            mine = linked & set(picked.order.records) if picked.order is not None else set()
            chosen |= mine
            if mine:
                resolution["resolved:" + picked.reason] += len(mine)
            else:
                resolution["ambiguous:" + (picked.reason if picked.order is None
                                           else "pick_not_linked")] += len(linked)
            if trail is not None:
                trail.extend((mail, r, picked.reason, r in mine, number) for r in sorted(linked))
        for record in chosen:
            named[mail].add(record)
            lead[(mail, record)] = lead.get((mail, record), True)
        rest = records - chosen
        left += len(rest)
        untold = {r for r in records if not cited.get((mail, r))}
        if untold:
            resolution["ambiguous:number_not_told"] += len(untold)
    return left


def _lead_numbers(st: Any, reused_by: Mapping[str, set[str]]) -> dict[tuple[str, str], set[str]]:
    """(message, record) -> the order number keys its current lead links cite (their
    ``identifier_mention`` features' normalised values), for the messages of ``reused_by``."""
    from ontology.po_mention_resolve import number_key

    mails = sorted(reused_by)
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for start in range(0, len(mails), 500):
        for frm, to, raw in st.conn.execute(
                "SELECT from_object, to_object, reason FROM links WHERE superseded_at IS "
                "NULL AND tier = 'lead' AND link_type = ? AND from_object IN (SELECT value "
                "FROM json_each(?))", (MAIL_PO_LINK, json.dumps(mails[start:start + 500]))):
            if to not in reused_by.get(frm, ()):
                continue
            try:
                features = json.loads(raw or "[]")
            except ValueError:
                continue   # an unreadable reason cites no number: the link stays unresolved
            for feature in features if isinstance(features, list) else ():
                if isinstance(feature, dict) and feature.get("feature") == "identifier_mention" \
                        and number_key(feature.get("normalized")):
                    out[(frm, to)].add(number_key(feature.get("normalized")))
    return out


def _bounded(result: Callable, gaps: list, skipped: Counter, *, more: bool, spent: bool
             ) -> dict[str, Any]:
    """A tool-call detector's result: ``truncated`` when roots were left over the bound
    (``event_bound``) or its time ran out (``time_budget``), what it found kept either way."""
    reasons = ([CALL_BOUND_REASON] if more else []) + (["time_budget"] if spent else [])
    return result(gaps, status="truncated" if reasons else "complete", reasons=reasons,
                  not_evaluated=skipped)


INVOICE_DATES = ("posting_date", "invoice_date")   # BC's posting date, Sage's invoice date


def _recent_invoiced_orders(st: Any, as_of: date, *, days: int = MARGIN_DAYS,
                            since: int = -1) -> list[str]:
    """Orders whose invoices (a BC posting date or a Sage invoice date) fall within
    ``MARGIN_DAYS`` before the newest such date their own system holds (not after
    ``as_of``): each system's own recency (``po_status.system_of`` of the invoice), so a
    system whose invoices end earlier (BC's capture ends months before Sage's) still
    yields its latest orders (``days``/``since``: more than ``since`` and at most ``days``
    days before it; the margin erosion rule's earlier orders). The systems take turns, each most recently invoiced first
    (days before its newest, then the date, then the order), so one system's slow margin
    reads (a Sage order's run takes minutes) never keep another's orders unread."""
    from .po_status import system_of

    links = st.links([INVOICE_LINK], held=False)
    invoices = sorted({link["from_object"] for link in links})
    dated: dict[str, date] = {}   # invoice -> its latest invoice date, not after as_of
    for start in range(0, len(invoices), 500):
        for oid, raw in st.conn.execute(
                "SELECT object_id, value FROM object_values WHERE superseded_at IS NULL AND "
                "property IN (SELECT value FROM json_each(:props)) AND object_id IN "
                "(SELECT value FROM json_each(:chunk))",
                {"props": json.dumps(INVOICE_DATES),
                 "chunk": json.dumps(invoices[start:start + 500])}):
            day = _day(raw)
            if day is not None and day <= as_of and (oid not in dated or day > dated[oid]):
                dated[oid] = day
    newest: dict[str, date] = {}
    for oid, day in dated.items():
        system = system_of(oid)
        if system not in newest or day > newest[system]:
            newest[system] = day
    last: dict[str, tuple[int, date]] = {}   # order -> (days before its system's newest, day)
    last_invoice: dict[str, str] = {}        # order -> that invoice (its system)
    for link in links:
        if link["tier"] not in ("verified", "reviewed", "corroborated"):
            continue
        day = dated.get(link["from_object"])
        if day is None:
            continue
        top = newest[system_of(link["from_object"])]
        back = (top - day).days
        if back > days:
            continue
        order = link["to_object"]
        if order not in last or (back, -day.toordinal()) < (last[order][0],
                                                            -last[order][1].toordinal()):
            last[order] = (back, day)
            last_invoice[order] = link["from_object"]
    per_system: dict[str, list[str]] = defaultdict(list)
    for order in sorted(last, key=lambda o: (last[o][0], -last[o][1].toordinal(), o)):
        if last[order][0] <= since:
            continue   # its latest invoice is within ``since``: a recent order, not earlier
        per_system[system_of(last_invoice[order])].append(order)
    turns = [(rank, system, order) for system, orders in per_system.items()
             for rank, order in enumerate(orders)]
    return [order for _rank, _system, order in sorted(turns)]


def _value_markings(st: Any, objects: Iterable[str], *, key: str = "l1_handle"
                    ) -> dict[str, list[str]]:
    """l1 handle (``key="object_id"``: object) -> the markings of its current values: money
    is marked on its value (``finance``, ``purchasing_prices``), not on its record's
    version, so a citation carries both."""
    out: dict[str, list[str]] = defaultdict(list)
    column = {"l1_handle": "l1_handle", "object_id": "object_id"}[key]
    for found, markings in st.conn.execute(
            f"SELECT DISTINCT {column}, markings FROM object_values WHERE superseded_at IS "
            "NULL AND object_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(objects)),)):
        out[found].append(markings)
    return out


def read_margin(call: Callable, root: str) -> Any:
    """One order's checked margin (``customer_profit.read_order``), without its raw run: a
    pool task (``operating.proactive_pool``)."""
    from . import customer_profit as cp

    order = cp.read_order(call, cp.Order(root, root, []))
    order.run = None
    return order


def negative_margin(st: Any, as_of: date, call: Callable | None, *, gap: Callable,
                    cites: Callable, result: Callable, bound: int = CALL_BOUND,
                    pmap: Callable[[Callable, Sequence[Any]], list[Any]] | None = None,
                    roots: Sequence[str] | None = None,
                    read: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """``pmap``: the engine's process pool: every recently invoiced order is read on its
    workers (no ``bound``; one not reached in time is ``time_budget``). ``roots``: the
    recently invoiced orders (``_recent_invoiced_orders``) when already read. ``read``:
    one order's margin without a pool (default ``customer_profit.read_order``; the engine
    shares its readings with the margin erosion rule)."""
    if call is None:
        return result([], status="not_configured", reasons=["tool_caller_not_given"])
    from . import customer_profit as cp

    roots = _recent_invoiced_orders(st, as_of) if roots is None else list(roots)
    gaps, skipped = [], Counter()
    spent = False
    asked = roots if pmap is not None else roots[:bound]
    if pmap is not None:
        from .proactive_pool import NOT_READ
        pooled = pmap(read_margin, asked)
    for n, root in enumerate(asked):
        if pmap is not None:
            order = pooled[n]
            if order is NOT_READ:
                spent = True
                skipped["time_budget"] += 1
                continue
            if not isinstance(order, cp.Order):
                skipped["run_failed"] += 1
                continue
        else:
            try:
                order = read(root) if read is not None else \
                    cp.read_order(call, cp.Order(root, root, []))
            except Exception as error:  # noqa: BLE001 - one order fails alone
                if type(error).__name__ == "TimeBudgetSpent":
                    spent = True    # what was found stands; the rest is not looked at
                    break
                skipped["run_failed"] += 1
                continue
        if order.margin is None or order.currency is None:
            skipped["margin_not_decided"] += 1
            continue
        if order.margin >= 0:
            continue
        # nothing invoiced net of credits: a no-charge order (a sample, a replacement, a
        # fully credited invoice) costs its items but loses no sale; its own kind, no loss
        # A fully credited order whose goods were NOT returned stays a loss (a write-off):
        # only nothing invoiced, or credited and returned, is no-charge
        credited = any(line.credited or line.credit_amount for line in order.lines)
        # the return check is per line: only the credited lines whose goods did not come
        # back stay a loss (a write-off), valued at what was credited for them
        unreturned = sum((line.credit_amount for line in order.lines
                          if (line.credited or line.credit_amount)
                          and not line.return_handles), Decimal(0))
        returned = any(line.return_handles for line in order.lines)
        fully_credited = order.revenue is not None and order.revenue == 0 and credited
        no_charge = order.revenue is not None and order.revenue == 0 and (
            not credited or (returned and unreturned <= 0))
        handles = sorted({h for line in order.lines for h in (
            *line.invoice_handles, *line.credit_handles, *line.ledger_handles,
            *line.return_handles) if isinstance(h, str)})
        rows = {r["l1_handle"]: r for r in st.versions(handles=handles)}
        if not handles or set(handles) - set(rows):
            skipped["figures_not_stored"] += 1
            continue
        subject = st.versions(objects=[root])
        marks = _value_markings(st, {rows[h]["object_id"] for h in handles})
        loss = -order.margin
        if fully_credited and returned and unreturned > 0:
            loss = unreturned       # only the unreturned part is lost
        if no_charge:
            gaps.append(gap(
                "no_charge_order", "negative_margin", root, "SalesOrder", "no_charge",
                summary=f"No-charge order: nothing invoiced, item cost {order.cost} "
                        f"{order.currency}",
                missing="Whether this order was meant to ship at no charge (a sample, a "
                        "replacement or a credited invoice)",
                why="Its invoiced revenue net of credit memos is zero, so it is a no-charge "
                    "order, not a loss on a sale; its booked item cost is stated (each "
                    "figure cited).",
                priority="low", about=("SalesOrder", "margin"),
                groups=[cites(subject)] + [cites([rows[h]], *marks.get(h, ()))
                                           for h in handles]))
            continue
        gaps.append(gap(
            "negative_margin", "negative_margin", root, "SalesOrder", "margin",
            summary=f"Lost {loss} {order.currency} on item cost only: revenue {order.revenue} "
                    f"{order.currency}, item cost {order.cost} {order.currency}",
            missing="Why the order's item cost exceeds its invoiced revenue",
            why="Margin on item cost only: invoiced revenue net of credit memos less the "
                "booked item-ledger cost (freight, duty and overhead are not in it), from "
                "the order margin engine's checked figures (each figure cited).",
            priority="high", about=("SalesOrder", "margin"),
            groups=[cites(subject)] + [cites([rows[h]], *marks.get(h, ())) for h in handles],
            event={"amount": {"value": str(loss), "currency": order.currency,
                              "basis": "loss"}}))
    out = _bounded(result, gaps, skipped, more=len(roots) > len(asked), spent=spent)
    judged = len(asked) - sum(skipped.values())
    out["coverage"] = {"unit": "orders_invoiced_recently", "total": len(roots),
                       "evaluated": judged, "not_evaluated": {k: v for k, v in sorted({
                           **dict(skipped), "not_asked": len(roots) - len(asked)}.items())
                           if v}}
    return out
