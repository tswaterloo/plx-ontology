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
  whose later document promises a later delivery date (both cited);
* ``lot_attention``: a lot whose expiry date falls within ``EXPIRY_DAYS`` of the run's day
  (``lot_near_expiry``), and a batch on quality hold whose status is still active
  (``lot_on_hold``; a closed status is skipped, an unmapped one ``not_evaluated``);
* ``accepted_quote_no_order``: a quote accepted (its status word or its accepted time)
  that no sales order converts (no ``Quote.converted_to`` or
  ``SalesOrder.created_from_quote`` link, no converted order number). The absence is the
  run's verdict, so a reader sees it only with a grant on every sales-order audience;
* ``invoice_quantity_mismatch``: per purchase order and item, the quantity its posted
  supplier invoices bill (``PurchaseInvoice.invoices``) above what its lines received, in
  one unit (a unit not stated on both sides is ``not_evaluated``, never converted).

Tool-call detectors (the run principal's ``plx.investigate``, bounded to
``CALL_BOUND`` roots, most recent first; more candidates make the rule ``truncated``):

* ``vendor_no_reply`` (F28 ``quote_follow_up``): a request to a vendor unanswered for 7
  days or more, over the purchase orders mail named within ``MAIL_DAYS``. The absence of
  a reply is the run's verdict: a reader needs a grant on every audience the
  conversation's stored messages sit in;
* ``vendor_terms_unapplied`` (F27 ``vendor_replies``): a term a vendor's reply offers that
  differs from or fills the system's field, never applied;
* ``negative_margin`` (F10 ``order_margin`` through ``operating.customer_profit``): a
  sales order invoiced within ``MARGIN_DAYS`` before the newest posting date the store
  holds whose decided, checked margin is below zero; every figure it rests on is its own
  citation group.

Late purchase orders, a late PO hitting a customer order (``orders_at_risk``) and the
receipt quantity mismatch are the engine's own rules; ``operating.briefing`` maps every
kind to its event type. Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore
from ontology.units import unit_token

REVISION_DAYS = 14
EXPIRY_DAYS = 30
MAIL_DAYS = 30
MARGIN_DAYS = 30
CALL_BOUND = 40
CALL_BOUND_REASON = "event_bound"
APART_REASON = "read_apart"   # left to the separate vendor-mail pass
NO_REPLY_DAYS = 7    # F28's threshold: a request waiting this long is overdue
CACHE_DAYS = 14      # a vendor-mail reading is redone at least this often
CACHE_VERSION = 1    # bump when what a reading means changes: every root is read again

REVISED_DATES = {"PurchaseOrder": ("expected_date",),
                 "PurchaseOrderLine": ("bc_expected_receipt_date",)}
DATE_WORDS = {"expected_date": "requested-by date",
              "bc_expected_receipt_date": "expected receipt date"}
HOLD_WORDS = frozenset({"on-hold", "on hold", "hold", "onhold"})
CLOSED_BATCH = frozenset({"cancelled", "canceled", "realized", "closed"})
ACTIVE_BATCH = frozenset({"allocated", "committed", "generated", "scheduled", "planned",
                          "in progress", "released"})
ACCEPTED_WORDS = frozenset({"accepted"})
POSTED_INVOICE = frozenset({"open", "paid"})

QUOTE_ORDER_LINKS = ("Quote.converted_to", "SalesOrder.created_from_quote")


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
                settled: Sequence[str], confirming: Sequence[str]) -> dict[str, Any]:
    gaps = []
    skipped: Counter = Counter()
    since = (as_of - timedelta(days=REVISION_DAYS)).isoformat()
    lines_of: dict[str, tuple[str, str]] = {}
    for link in st.links(["PurchaseOrderLine.line_of"], settled):
        lines_of[link["from_object"]] = (link["to_object"], link["markings"])
    for object_type, props in REVISED_DATES.items():
        ids = [r[0] for r in st.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_type = ? AND NOT "
            + ostore.removed_at_sql("o.object_id") + " ORDER BY o.object_id", (object_type,))]
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
    if several:
        found = st.values(["SupplierConfirmation"], ("delivery_date", "document_date"),
                          objects=sorted({c for cs in several.values() for c, _ in cs}))
        for po, cs in sorted(several.items()):
            dated = []
            for conf, marks in cs:
                props = (found.get(conf) or {}).get("props") or {}
                promised, issued = _one(props.get("delivery_date")), _one(
                    props.get("document_date"))
                if promised is None or issued is None or _day(promised["value"]) is None \
                        or _day(issued["value"]) is None:
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
    return result(gaps, not_evaluated=skipped)


# --------------------------------------------------------------------------- lots

def lot_attention(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                  result: Callable) -> dict[str, Any]:
    gaps = []
    skipped: Counter = Counter()
    horizon = as_of + timedelta(days=EXPIRY_DAYS)
    for oid, entry in sorted(st.values(["Lot"], ("expiry_date", "lot_number")).items()):
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
        days = (day - as_of).days
        number = _one(props.get("lot_number"))
        gaps.append(gap(
            "lot_near_expiry", "lot_attention", oid, "Lot", f"expiry|{day}",
            summary=f"Expires on {day}, " + ("today" if days == 0 else
                                               f"in {days} day{'' if days == 1 else 's'}"),
            missing="Whether this lot is used, reworked or written off before it expires",
            why=f"The lot's recorded expiry date is {day}, within {EXPIRY_DAYS} days.",
            priority="high" if days <= 7 else "medium", about=("Lot", "expiry_date"),
            groups=[cites([expiry])], extra=cites([number]) if number else (),
            event={"days": days}))
    for oid, entry in sorted(st.values(["Batch"], ("quality_status", "status",
                                                   "lot_number")).items()):
        props = entry["props"]
        hold = _one(props.get("quality_status"))
        if hold is None or _word(hold) not in HOLD_WORDS:
            continue
        status = _one(props.get("status"))
        word = _word(status)
        if word in CLOSED_BATCH:
            continue
        if status is None or word not in ACTIVE_BATCH:
            skipped["status_unmapped"] += 1
            continue
        gaps.append(gap(
            "lot_on_hold", "lot_attention", oid, "Batch", "hold",
            summary="On quality hold while the batch is still active",
            missing="Why the batch is held and when Quality will decide its release",
            why="The batch's quality status is a hold and its status is not closed "
                "(both cited).",
            priority="high", about=("Batch", "quality_status"),
            groups=[cites([hold]), cites([status])]))
    return result(gaps, not_evaluated=skipped)


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
                              result: Callable, settled: Sequence[str]) -> dict[str, Any]:
    gaps = []
    skipped: Counter = Counter()
    links = st.links(["PurchaseInvoice.invoices", "PurchaseInvoiceLine.line_of",
                      "PurchaseInvoiceLine.for_item", "PurchaseOrderLine.line_of",
                      "PurchaseOrderLine.for_item"], settled)
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
        received = []
        for line in po_lines.get(po, ()):
            line_item = item_of.get(line["from_object"])
            if line_item is None or line_item["to_object"] != item:
                continue
            qty = _one(props(line["from_object"]).get("received_quantity"))
            if qty is None:
                received = None
                break
            received.append((qty, [line["markings"], line_item["markings"]]))
        if not received:
            skipped["receipt_quantity_unknown"] += 1
            continue
        units = {unit_token(r[0].get("unit")) for r in rows} | {
            unit_token(q.get("unit")) for q, _ in received}
        if len(units) != 1 or None in units or "" in units:
            skipped["unit_not_stated"] += 1
            continue
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
                "than its lines record received (each quantity cited).",
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
        for mail, order in st.conn.execute(
                "SELECT from_object, to_object FROM links WHERE link_type = "
                "'Email.mentions_purchase_order' AND superseded_at IS NULL AND tier <> "
                "'contradicted'"):
            if order in wanted:
                self.mail[order].add(mail)

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
        kind = next((("differs" if "(differs)" in s else "fills") for s in steps
                     if s.startswith("proposed update")), None)
        handles = sorted(set(_entry_handles(entry)))
        if kind is not None and handles:
            out.append({"name": name, "basis": basis, "handles": handles, "kind": kind})
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
    return gap(
        "vendor_terms_unapplied", detector, root, "PurchaseOrder",
        f"{finding['name']}|{finding['handles'][0]}",
        summary=f"A vendor reply offers a term that {word} system field, not applied",
        missing="Whether the offered term should be entered in the Portal (a "
                "person approves and enters it; nothing is written here)",
        why="A reply from the vendor states a term the system does not hold as "
            "stated (the reply and the record are cited).",
        priority="medium", about=("PurchaseOrder", "confirmation"),
        groups=[cites(reply)] + [cites([by_object[b]]) for b in basis])


def vendor_mail(st: Any, as_of: date, call: Callable | None, *, gap: Callable,
                cites: Callable, result: Callable, family: str, bound: int = CALL_BOUND,
                cache: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """F28 (``vendor_no_reply``) or F27 (``vendor_terms_unapplied``) over the purchase
    orders mail named recently, incrementally: a purchase order whose thread is unchanged
    since its last reading (``_cache_valid``) keeps that reading (no investigation; F28's
    days waiting counted on from it); only changed ones are investigated, newest mail
    first, at most ``bound`` per run. The result's ``cache`` holds every reading."""
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
    for root, fp, newest in stale[:bound]:
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
    out = _bounded(result, gaps, skipped, more=len(stale) > bound, spent=spent)
    if bound == 0 and stale:   # the build's run: the vendor-mail pass reads them apart
        out["reasons"] = [APART_REASON]
    out["cache"], out["roots"] = kept, list(roots)
    out["reused"] = len(roots) - len(stale)
    return out


def _bounded(result: Callable, gaps: list, skipped: Counter, *, more: bool, spent: bool
             ) -> dict[str, Any]:
    """A tool-call detector's result: ``truncated`` when roots were left over the bound
    (``event_bound``) or its time ran out (``time_budget``), what it found kept either way."""
    reasons = ([CALL_BOUND_REASON] if more else []) + (["time_budget"] if spent else [])
    return result(gaps, status="truncated" if reasons else "complete", reasons=reasons,
                  not_evaluated=skipped)


def _recent_invoiced_orders(st: Any, as_of: date) -> list[str]:
    """Orders whose posted invoices fall within ``MARGIN_DAYS`` before the newest posting
    date the store holds (not after ``as_of``): the data's own recency, so a capture whose
    invoices end earlier still yields its latest orders, most recently invoiced first."""
    newest = st.conn.execute(
        "SELECT max(v.value) FROM links l JOIN object_values v ON v.object_id = l.from_object "
        "AND v.property = 'posting_date' AND v.superseded_at IS NULL WHERE l.link_type = "
        "'SalesInvoice.bills' AND l.superseded_at IS NULL AND v.value <= :until",
        {"until": (as_of + timedelta(days=1)).isoformat()}).fetchone()[0]
    end = _day(newest)
    if end is None:
        return []
    since = (end - timedelta(days=MARGIN_DAYS)).isoformat()
    rows = st.conn.execute(
        "SELECT l.to_object, max(v.value) AS last FROM links l JOIN object_values v ON "
        "v.object_id = l.from_object AND v.property = 'posting_date' AND v.superseded_at IS "
        "NULL WHERE l.link_type = 'SalesInvoice.bills' AND l.superseded_at IS NULL AND "
        "l.tier IN ('verified', 'reviewed', 'corroborated') AND v.value >= :since AND "
        "v.value <= :until GROUP BY l.to_object ORDER BY last DESC, l.to_object",
        {"since": since, "until": (as_of + timedelta(days=1)).isoformat()}).fetchall()
    return [r[0] for r in rows]


def negative_margin(st: Any, as_of: date, call: Callable | None, *, gap: Callable,
                    cites: Callable, result: Callable, bound: int = CALL_BOUND
                    ) -> dict[str, Any]:
    if call is None:
        return result([], status="not_configured", reasons=["tool_caller_not_given"])
    from . import customer_profit as cp

    roots = _recent_invoiced_orders(st, as_of)
    gaps, skipped = [], Counter()
    spent = False
    for root in roots[:bound]:
        try:
            order = cp.read_order(call, cp.Order(root, root, []))
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
        handles = sorted({h for line in order.lines for h in (
            *line.invoice_handles, *line.credit_handles, *line.ledger_handles,
            *line.return_handles) if isinstance(h, str)})
        rows = {r["l1_handle"]: r for r in st.versions(handles=handles)}
        if not handles or set(handles) - set(rows):
            skipped["figures_not_stored"] += 1
            continue
        subject = st.versions(objects=[root])
        loss = -order.margin
        gaps.append(gap(
            "negative_margin", "negative_margin", root, "SalesOrder", "margin",
            summary=f"Lost {loss} {order.currency} on item cost only: revenue {order.revenue} "
                    f"{order.currency}, item cost {order.cost} {order.currency}",
            missing="Why the order's item cost exceeds its invoiced revenue",
            why="Margin on item cost only: invoiced revenue net of credit memos less the "
                "booked item-ledger cost (freight, duty and overhead are not in it), from "
                "the order margin engine's checked figures (each figure cited).",
            priority="high", about=("SalesOrder", "margin"),
            groups=[cites(subject)] + [cites([rows[h]]) for h in handles],
            event={"amount": {"value": str(loss), "currency": order.currency,
                              "basis": "loss"}}))
    return _bounded(result, gaps, skipped, more=len(roots) > bound, spent=spent)
