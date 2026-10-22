"""Order margin: invoiced revenue and booked item-ledger cost per sales order line (P9).

The engine of the ``plx.investigate`` plan ``ontology/plans/order_margin.json`` (family F10,
evidence kind ``compute``). It computes nothing the plan did not reach and cite: every input
is a projected Business Central value with its ``bc://`` citation, re-read for the
investigating actor (``resolver``) before it is used. A value a person corrected
(``plx.review`` ``correct_value``; the engine takes reviewed inputs) is the person's, used
only when the value it corrects and every record the correction cites resolve for the actor.

Semantics (BC API v2.0, mirrored from the Portal where it has a rule):

* **Revenue** of a line: the ``amountExcludingTax`` of the posted invoice lines (invoice status
  Open or Paid; Draft / In Review are not posted) for the line's item, on invoices that bill
  the order, less the ``amountExcludingTax`` of the Item lines for that item of the posted
  sales credit memos applied to those invoices (``SalesCreditMemo.applies_to``). A credit
  memo is subtracted because of its document type, never because of a sign. Revenue is
  excluding tax (tax is added on the invoice header only).
* **Booked cost** of a line: the shipped cost less the returned cost. Shipped cost: minus the
  ``costAmountActual`` of the item ledger entries of entry type Sale and document type Sales
  Shipment, posted on the order's shipments, for the line's item. BC books the cost of the
  invoiced quantity there (``Cost Amount (Actual)``); the expected cost of a shipped,
  uninvoiced quantity is not served by API v2.0, so a zero or empty actual cost is a typed
  gap (``COST_NOT_YET_ACTUAL``), never a zero cost. Returned cost: the ``costAmountActual``
  of the entries of entry type Sale and document type Sales Credit Memo posted by a netted
  credit memo, for the item, matched memo by memo: a memo crediting goods with no such entry
  of its own (a return receipt, or none posted) is a typed gap (``RETURN_NOT_IN_LEDGER``),
  never covered by another memo's entry. In the base unit the returned quantity must equal
  each memo's credited quantity; in another unit nothing is converted and the capture must
  be complete (``COST_COMPLETENESS_UNPROVEN``).
* **Margin** = revenue - booked cost; margin % = margin / revenue x 100, margin on price, the
  Portal's house convention (plx-customer-portal@8e3a951 portal/src/lib/costing/
  margin-ladder.ts: price = cost / (1 - m)).
* **Currency.** Ledger amounts are in the company's local currency; the schema states CAD for
  the manufacturing and distribution books. An invoice's amounts are in its ``currencyCode``;
  a blank code means the company's local currency (Portal ``bcTransactionCurrency`` omits
  CAD, plx-customer-portal@b46934a portal/src/lib/business-central/serializers.ts:428-434),
  read from the cited invoice row and its capture lineage, and only for those two books.
  Revenue and cost in different currencies, or several invoice currencies, give no margin
  (``MIXED_CURRENCY_NO_CITED_RATE``): nothing is converted or added across currencies.
* **Completeness.** A line's figures need the whole line invoiced (invoiced quantity equal to
  the ordered quantity, in one unit) and its ledger entries proven whole: their quantity
  (base unit) equals the invoiced quantity when the item's base unit is the invoice unit, else
  the BC capture of the run must be complete. Otherwise the line is a typed gap and has no
  figure. The order total needs every item line computed, a line set proven complete (a
  complete run, a reader whose scope covers the order's lines, invoices and shipments) and
  no posted invoice revenue that no order line takes.
* **Credit memos.** Only a credit memo line of line type Item names goods: a Charge or Account
  credit applied to the order is never netted into a line, and the order total is then not
  stated (``CREDIT_MEMO_LINE_UNATTRIBUTED``), as for an invoice charge line. Only memos
  applied to the order's invoices are read. Posted memos to the order's customer that name no
  invoice are reached by a filtered ``plx.around`` (the memos BC states no invoiceId for,
  so a memo BC names an invoice for is never reached) and their ``applies_to`` resolved,
  never read one by one. Those that reach no invoice — BC states no invoiceId and the
  invoiceNumber names no single invoice of the company, so nothing says which order they
  credit — are counted in an entry citing at most ``CITED_MEMOS`` of them (``basis``) beside
  a typed gap (``CREDIT_MEMO_UNATTRIBUTED``) on figures net only of the attributable memos:
  never netted (we cannot know whose they are), never ignored, and the answer is then not a
  complete net margin; the answer stays the same size however many there are. A memo this
  reader may not read is never counted or cited: the capture gap below stands for it, so
  this gap never tells the reader whether one exists. Unless the
  customer's memos were reached, this reader may read every one of them and the BC capture
  is proven complete, a credit memo may be unseen: the typed gap ``CREDIT_MEMOS_NOT_CAPTURED``
  stays (figures net only of the credit memos read). The capture proof is the BC family's
  completeness (``meta.freshness``) or, per capture unit, a complete ``company=<id>`` claim
  of the order's company on both ``salesCreditMemos`` and ``salesCreditMemoLines``
  (``captured``, from the host; unknown falls back to the family): decision
  docs/decisions/2026-10-05-margin-net-credit-capture-units.md. Booked cost keeps its own
  completeness proof.
* **Orders BC no longer serves.** BC deletes a sales order once it is shipped and invoiced;
  the projection then roots the order at the posted documents that name it (a binding the
  plan lists in ``posted_bindings``, ``stands_for`` in the schema; decision
  docs/decisions/2026-10-07-p9-margin-completed-orders.md). Such an order has no lines or
  ordered quantity: its lines are its items, those of its posted invoice lines and of its
  shipped ledger entries, and a line is whole when its invoiced quantity equals its shipped
  item-ledger quantity in the item's base unit (the invoice unit; nothing converted), over
  invoices and shipments proven whole for this reader (a complete run, a read scope covering
  them and a complete BC capture of ``POSTED_UNITS``, per company claim or family). Else the
  line is a typed gap (``LINE_PARTIALLY_INVOICED``, ``INVOICED_EXCEEDS_SHIPPED``,
  ``POSTED_LINE_UNPROVEN``, ``ORDER_LINES_INCOMPLETE``). Revenue, cost and margin are then
  computed as for a served order.
* **Never mixed.** Only BC records count: a Portal or Sage twin is never added to a BC
  figure, and an invoice or ledger entry reached only
  through lead-tier links never enters a sum. The landed-cost estimate (family F7, plan
  ``landed_cost``, rooted at a purchase order) is an analytic estimate: freight and duty are
  expensed to the G/L (ERP-D-015), so booked cost excludes them, and this engine never adds
  the estimate to it; the answer says where to ask for it instead.

Gaps never quote source text: they name answer entries, codes and ISO 4217 currencies only.
Evidence label of fixture runs: ``isolated-fixture``; nothing here reads a source by itself.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Mapping, Sequence

from ontology.units import unit_token
from operating.landed_cost import (ISO_4217, MAX_STEP_CHARS, _canon, _dec_str, _sha,
                                   correction_sources, person_correction)
from operating.cases import _cited_float, _field

Resolver = Callable[[str, Any], Any]

ENGINE = "order_margin"
# Evidence roles (plan evidence names) and declared links (engine_args), see plan_check.
PROPERTY_ROLES = ("ordered", "base_uom", "invoice", "invoice_status", "invoiced", "revenue",
                  "entry_type", "document_type", "ledger_quantity", "cost", "credit_memo",
                  "credit_status", "credit_line_type", "returned", "credit")
SELECT_ROLES = ("ledger_capture", "credit_capture", "credit_customer",
                "credit_hidden", "credit_withheld", "order")
LINKS = ("line_link", "line_item", "invoice_link", "invoice_line_link", "invoice_item",
         "shipment_link", "ledger_link", "ledger_item", "credit_link", "credit_line_link",
         "credit_item", "return_link", "customer_link")
POSTED = frozenset({"open", "paid"})          # BC salesInvoice status of a posted invoice
UNPOSTED = frozenset({"draft", "in review"})
SALE = "sale"
SHIPMENT_DOCUMENT = "sales shipment"  # ledger entry documentType (API: Sales_x0020_Shipment)
CREDIT_DOCUMENT = "sales credit memo"  # (API: Sales_x0020_Credit_x0020_Memo)
ITEM_LINE = "item"                     # the only document line type that names goods
CITED_MEMOS = 3   # unattributed credit memos an answer cites; it counts every one
CREDIT_UNITS = ("salesCreditMemos", "salesCreditMemoLines")  # BC capture units of the memos
# BC capture units whose whole capture proves the lines of an order BC no longer serves.
POSTED_UNITS = ("salesInvoices", "salesInvoiceLines", "salesShipments", "itemLedgerEntries")
# Books whose local currency is CAD (the schema's ItemLedgerEntry money currency evidence).
LOCAL_CURRENCY = {"bc_manufacturing": "CAD", "bc_distribution": "CAD"}
CURRENCY_FIELD = "currencyCode"
_GAP_STATES = {"MIXED_CURRENCY_NO_CITED_RATE": "not_ingested",
               "COST_NOT_YET_ACTUAL": "not_ingested",
               "INVOICE_CORRECTED": "not_ingested",
               "INVOICE_CURRENCY_UNKNOWN": "not_ingested",
               "CREDIT_MEMO_CURRENCY_UNKNOWN": "not_ingested",
               "CREDIT_MEMOS_NOT_CAPTURED": "not_ingested",
               "RETURN_NOT_IN_LEDGER": "not_ingested"}
LANDED_NOTE = ("landed-cost estimate: not part of booked cost. BC booked cost is the item "
               "ledger Cost Amount (Actual); freight and duty are expensed to the G/L "
               "(ERP-D-015). The estimate is family F7 (plan landed_cost, rooted at a purchase "
               "order supplying the item) and is never added here.")


def _text(entry: Mapping[str, Any] | None) -> str | None:
    value = (entry or {}).get("value")
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    return " ".join(value.replace("_x0020_", " ").split()).casefold() \
        if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, float):
            return _cited_float(value, "cited")[0]
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _binding(object_id: str) -> str:
    parts = object_id.split(":", 2)
    return parts[1] if len(parts) > 1 else ""


def plan_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
               schema: Any) -> list[str]:
    """Problems with ``engine_args``: every role names a property (``ledger_capture``: a
    select) evidence item listed before the compute item, every link is declared and every
    ``posted_bindings`` entry is a binding that stands for another (``stands_for``)."""
    out: list[str] = []
    roles = args.get("roles") if isinstance(args, Mapping) else None
    if not isinstance(roles, Mapping):
        return ["engine_args.roles must map roles to evidence names"]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    for role in set(roles) - set(PROPERTY_ROLES) - set(SELECT_ROLES):
        out.append(f"unknown role {role}")
    for role in PROPERTY_ROLES + SELECT_ROLES:
        kind = "select" if role in SELECT_ROLES else "property"
        if items.get(roles.get(role) or "", {}).get("kind") != kind:
            out.append(f"role {role} must name a {kind} evidence item listed before it")
    declared = getattr(schema, "link_types", None)
    for link in LINKS:
        if not isinstance(args.get(link), str):
            out.append(f"{link} is required")
        elif declared is not None and args[link] not in declared:
            out.append(f"{link}: undeclared link type {args[link]}")
    posted = args.get("posted_bindings")
    if not isinstance(posted, list) or not all(isinstance(b, str) for b in posted):
        out.append("posted_bindings must list binding ids")
    else:
        types = getattr(schema, "object_types", None)
        if types is not None:
            standing = {b["id"] for t in types.values() for b in t.get("bindings", ())
                        if b.get("stands_for")}
            out += [f"posted_bindings: {b} stands for no binding" for b in posted
                    if b not in standing]
    return out


class _Run:
    """One engine call: entries by role, edges by link, typed gaps grouped by code."""

    def __init__(self, args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping]],
                 edges: Sequence[Mapping[str, Any]], items: Mapping[str, Mapping[str, Any]],
                 resolver: Resolver | None, actor: Any) -> None:
        self.args, self.items = args, items
        self.roles = args["roles"]
        self.got = {role: [e for e in evidence.get(self.roles[role], ())
                           if e.get("object_id")] for role in PROPERTY_ROLES}
        self.selected = {role: next((e.get("value") for e in evidence.get(
            self.roles[role], ())), None) for role in SELECT_ROLES}
        self.capture = self.selected["ledger_capture"]
        self.links: dict[str, list[tuple[str, str]]] = {}
        for edge in edges:
            for link in LINKS:
                if edge.get("link_type") == args[link]:
                    self.links.setdefault(link, []).append((edge["from"], edge["to"]))
        self.resolver, self.actor = resolver, actor
        self._views: dict[str, Any] = {}
        self.grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.reasons: list[str] = []

    # ---------------------------------------------------------------- plumbing

    def gap(self, code: str, subject: str, detail: str, *roles: str,
            line: str | None = None) -> None:
        detail = f"{code}: {detail}"[:600]
        gap = self.grouped.get((code, subject, detail))
        if gap is None:
            gap = self.grouped[(code, subject, detail)] = {
                "state": _GAP_STATES.get(code, "unknown"), "reason": code.lower()[:64],
                "subject": subject, "detail": detail, "lines": []}
            sources = [f"{self.items[self.roles[r]]['object_type']}."
                       f"{'|'.join(self.items[self.roles[r]]['properties'])}"[:64]
                       for r in roles if self.roles.get(r) in self.items]
            if sources:
                gap["searched"] = {"sources": sources[:8]}
        if line and line not in gap["lines"]:
            gap["lines"].append(line)
        if code.lower() not in self.reasons:
            self.reasons.append(code.lower()[:64])

    def gaps(self) -> list[dict[str, Any]]:
        out = []
        for gap in self.grouped.values():
            lines = gap.pop("lines")
            if lines:
                gap["subject"] = f"{gap['subject']} of {', '.join(lines)}"
            gap["subject"] = gap["subject"][:700]
            out.append(gap)
        return out

    def by_object(self, role: str) -> dict[str, Mapping[str, Any]]:
        out: dict[str, Mapping[str, Any]] = {}
        for entry in self.got[role]:
            out.setdefault(entry["object_id"], entry)
        return out

    def targets(self, link: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for source, target in self.links.get(link, ()):
            out.setdefault(source, set()).add(target)
        return out

    def view(self, handle: str) -> Any:
        if handle not in self._views:
            view = None
            if self.resolver is not None:
                try:
                    view = self.resolver(handle, self.actor)
                except Exception:  # noqa: BLE001 - any resolver failure fails closed
                    view = None
            self._views[handle] = view if isinstance(view, Mapping) else None
        return self._views[handle]

    def raw(self, entry: Mapping[str, Any], field_path: str | None = None) -> tuple[bool, Any]:
        """(resolved, raw value) of a cited field re-read for the actor."""
        citation = entry.get("citation") or {}
        view = self.view(str(citation.get("l1_handle")))
        if view is None:
            return False, None
        path = field_path or str(citation.get("field_path"))
        for candidate in (path, "record." + path.split(".", 1)[-1]):
            try:
                return True, _field(view, candidate)
            except Exception:  # noqa: BLE001 - a path the view does not carry
                continue
        return True, None

    def amount(self, entry: Mapping[str, Any]) -> Decimal | None:
        """The entry's number, only when the re-read cited field states the same number; a
        person's correction (a reviewed input), only when the value it corrects and every
        record it cites resolve for the actor."""
        value = _number(entry.get("value"))
        review = person_correction(entry)
        if review is not None:
            return value if value is not None and all(
                self.view(h) is not None for h in correction_sources(entry, review)) else None
        resolved, raw = self.raw(entry)
        return value if value is not None and resolved and _number(raw) == value else None

    def currency(self, entry: Mapping[str, Any]) -> tuple[str | None, str]:
        """(ISO currency, how it was read) of a BC document from its cited header row."""
        resolved, raw = self.raw(entry, "row." + CURRENCY_FIELD)
        if not resolved:
            return None, "the cited document did not resolve for this reader"
        if not isinstance(raw, str):
            return None, "the cited document states no currencyCode"
        code = raw.strip().upper()
        if code:
            return (code, "cited currencyCode") if code in ISO_4217 else (
                None, "the cited currencyCode is not an ISO 4217 code")
        book = (entry.get("citation") or {}).get("lineage")
        if book in LOCAL_CURRENCY:
            return LOCAL_CURRENCY[book], ("blank currencyCode = the company's local currency "
                                          f"{LOCAL_CURRENCY[book]} ({book})")
        return None, "blank currencyCode in a book whose local currency is not established"


def _corrected_in_another_currency(entry: Mapping[str, Any], currency: str) -> bool:
    """A person's correction of a document amount whose currency (``plx.review`` states one
    on every money correction) is not the cited document's: it is never labelled with the
    document's currency."""
    review = person_correction(entry)
    return review is not None and review.get("currency") != currency


def _money(value: Decimal, currency: str) -> str:
    return f"{_dec_str(value)} {currency}"


def plan_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Resolver | None, actor: Any,
             complete: bool, covers: Callable[[str], bool],
             captured: Callable[[str], bool | None] | None = None) -> dict[str, Any]:
    """Computed entries (per line revenue, booked cost and margin; order totals and margin %;
    the landed-estimate note), typed gaps and reason codes for one plan run (module doc)."""
    run = _Run(args, evidence, edges, items, resolver, actor)
    root = run.selected["order"]
    posted = isinstance(root, str) and _binding(root) in set(args["posted_bindings"])
    ordered, base_uom = run.by_object("ordered"), run.by_object("base_uom")
    line_item = run.targets("line_item")
    invoice_of = run.targets("invoice_line_link")
    billing = run.targets("invoice_link")
    item_of_invoice_line = run.targets("invoice_item")
    item_of_entry = run.targets("ledger_item")
    shipment_of_entry = run.targets("ledger_link")
    linked = {source for source, _ in run.links.get("line_link", ())}
    line_ids = sorted(linked | set(ordered))
    non_bc = sorted({e["object_id"] for role in PROPERTY_ROLES for e in run.got[role]
                     if not _binding(e["object_id"]).startswith("bc.")})
    if non_bc:
        run.gap("NON_BC_RECORD_EXCLUDED", "order margin",
                f"{len(non_bc)} record(s) outside Business Central were reached; BC figures "
                "never add a Portal or Sage twin, so they are left out")
    bc_lines = [oid for oid in line_ids if _binding(oid).startswith("bc.")]
    if line_ids and not bc_lines:
        run.gap("ORDER_NOT_BC_ROOTED", "order margin",
                "the order's lines are not Business Central records; order margin answers "
                "from BC invoices and the BC item ledger only", "ordered")

    # -------------------------------------------------- invoices: posted, BC, evidence tier
    statuses = {e["object_id"]: _text(e) for e in run.got["invoice_status"]}
    headers = run.by_object("invoice")
    currency_of: dict[str, tuple[str | None, str]] = {}
    for invoice, header in headers.items():
        currency_of[invoice] = run.currency(header)
    for memo, header in run.by_object("credit_memo").items():
        currency_of[memo] = run.currency(header)
    invoice_lines: dict[str, list[dict[str, Any]]] = {}   # item object -> posted lines
    unattributed: list[str] = []
    held_back: dict[str, set[str]] = {}                   # item object -> why lines are open
    qty_by_line = run.by_object("invoiced")
    for entry in run.got["revenue"]:
        oid = entry["object_id"]
        invoices = invoice_of.get(oid, set())
        invoice = next(iter(invoices)) if len(invoices) == 1 else None
        items_ = item_of_invoice_line.get(oid, set())
        item = next(iter(items_)) if len(items_) == 1 else None
        if not _binding(oid).startswith("bc.") or invoice is None or not billing.get(invoice):
            continue
        status = statuses.get(invoice)
        if status in UNPOSTED:
            continue                                      # not booked revenue
        if item is None:
            unattributed.append(oid)
            continue
        if entry.get("lead_basis"):
            held_back.setdefault(item, set()).add("LEAD_TIER_INVOICE_EXCLUDED")
            continue
        if status not in POSTED:
            held_back.setdefault(item, set()).add(
                "INVOICE_CORRECTED" if status in ("canceled", "cancelled", "corrective")
                else "INVOICE_STATUS_UNKNOWN")
            continue
        invoice_lines.setdefault(item, []).append(
            {"amount": entry, "quantity": qty_by_line.get(oid), "invoice": invoice})

    # -------------------------------------------------- credit memos applied to those invoices
    credits, stray_credit, unsettled = _credits(run, billing, held_back)

    # -------------------------------------------------- ledger entries: shipments and returns
    types = {e["object_id"]: _text(e) for e in run.got["entry_type"]}
    documents = {e["object_id"]: _text(e) for e in run.got["document_type"]}
    ledger_qty = run.by_object("ledger_quantity")
    memo_of_entry = run.targets("return_link")
    ledger: dict[str, list[dict[str, Any]]] = {}
    returns: dict[str, list[dict[str, Any]]] = {}
    for entry in run.got["cost"]:
        oid = entry["object_id"]
        items_ = item_of_entry.get(oid, set())
        if not _binding(oid).startswith("bc.") or len(items_) != 1 or types.get(oid) != SALE:
            continue
        row = {"cost": entry, "quantity": ledger_qty.get(oid), "lead": entry.get("lead_basis"),
               "memos": memo_of_entry.get(oid, set())}
        if shipment_of_entry.get(oid) and documents.get(oid) == SHIPMENT_DOCUMENT:
            ledger.setdefault(next(iter(items_)), []).append(row)
        elif row["memos"] and documents.get(oid) == CREDIT_DOCUMENT:
            returns.setdefault(next(iter(items_)), []).append(row)

    # -------------------------------------------------- lines
    # An order BC no longer serves (``posted``) has no lines: one per item of its posted
    # invoice lines and shipped ledger entries, whole when invoiced = shipped (_posted_whole).
    whole = posted and complete and all(
        covers(args[link]) for link in ("invoice_link", "shipment_link")) and (
        run.capture == "complete" or captured is not None
        and all(captured(unit) is True for unit in POSTED_UNITS))
    if posted:
        bc_lines = sorted(set(invoice_lines) | set(held_back) | set(ledger))
        line_item = {item: {item} for item in bc_lines}
    items_of_lines: dict[str, list[str]] = {}
    for oid in bc_lines:
        for item in line_item.get(oid, ()):
            items_of_lines.setdefault(item, []).append(oid)
    entries: list[dict[str, Any]] = []
    totals: list[tuple[str, Decimal, Decimal, str, list[str]]] = []
    goods_lines = 0
    for index, oid in enumerate(bc_lines, start=1):
        label = f"{name}_line_{index}"
        result = _line(run, label, oid, ordered.get(oid), line_item.get(oid, set()),
                       items_of_lines, invoice_lines, held_back, ledger, base_uom,
                       currency_of, complete, credits, returns,
                       posted=posted, whole=whole)
        if result is None:
            continue                                      # not an item line (no item, qty 0)
        goods_lines += 1
        for entry in result["entries"]:
            entry["inputs"] = list(dict.fromkeys(entry["inputs"]))[:50]
        entries.extend(result["entries"])
        if result.get("figures"):
            totals.append(result["figures"])

    # -------------------------------------------------- order totals
    lines_complete = whole if posted else (complete and bool(linked) and all(
        covers(args[link]) for link in ("line_link", "invoice_link", "shipment_link"))
        and all(oid in ordered for oid in linked))
    if not lines_complete:
        run.gap("ORDER_LINES_INCOMPLETE", "order lines",
                "the order's line set, invoices and shipments are not proven complete (a "
                "truncated run, a line not read, or a reader whose scope may hide one); no "
                "order total is stated", "ordered")
    attributed = {item for item in invoice_lines if item in items_of_lines}
    stray = unattributed + [e["amount"]["object_id"] for item, rows in invoice_lines.items()
                            if item not in attributed for e in rows]
    if stray:
        run.gap("INVOICE_LINE_UNATTRIBUTED", "order total",
                f"{len(stray)} posted invoice line(s) of the order match no item line of it "
                "(a charge, an account line or another item); the total would leave their "
                "revenue out", "revenue")
    stray_credit += [r["amount"]["object_id"] for item, rows in credits.items()
                     if item not in items_of_lines for r in rows]
    if stray_credit:
        run.gap("CREDIT_MEMO_LINE_UNATTRIBUTED", "order total",
                f"{len(stray_credit)} line(s) of posted credit memos applied to the order are "
                "not an Item line of an item the order carries (a charge or account credit is "
                "never goods); the total would leave their credit out", "credit_line_type",
                "credit")
    if unsettled:
        run.gap("CREDIT_MEMO_UNSETTLED", "order total",
                "a credit memo applied to the order has a status that is neither posted (Open, "
                "Paid) nor unposted (Draft, In Review); what it credits is not decided",
                "credit_status")
    unattributed = _credit_coverage(run, complete, captured, name)
    currencies = {figures[3] for figures in totals}
    total_names = {key: f"{name}_{key}" if key != "margin" else name
                   for key in ("revenue", "booked_cost", "margin", "margin_pct")}
    line_names = [e["name"] for e in entries]
    out_totals = [{"name": total_names[key][:64], "kind": "computed", "value": None,
                   "inputs": [n for n in line_names if n.endswith("_" + key)
                              or (key in ("margin", "margin_pct") and n.endswith("_margin"))][:50],
                   "steps": [], "check": "not_applicable"} for key in total_names]
    if (lines_complete and goods_lines and len(totals) == goods_lines and not stray
            and not stray_credit and not unsettled and len(currencies) == 1):
        currency = next(iter(currencies))
        revenue = sum((t[1] for t in totals), Decimal(0))
        cost = sum((t[2] for t in totals), Decimal(0))
        margin = revenue - cost
        steps = [f"{t[0]}: revenue {_money(t[1], currency)}, booked cost "
                 f"{_money(t[2], currency)}" for t in totals][:36]
        steps.append(f"order: revenue {_money(revenue, currency)} - booked cost "
                     f"{_money(cost, currency)} = margin {_money(margin, currency)}")
        values = {"revenue": revenue, "booked_cost": cost, "margin": margin}
        if revenue != 0:
            values["margin_pct"] = (margin * 100 / revenue).quantize(Decimal("0.01"),
                                                                      ROUND_HALF_EVEN)
            steps.append(f"margin % on price = margin / revenue x 100 = "
                         f"{_dec_str(values['margin_pct'])}")
        digest = _sha(_canon({"lines": [t[4] for t in totals], "steps": steps}))
        for entry, key in zip(out_totals, total_names):
            if key in values:
                entry.update(value=_dec_str(values[key]), steps=list(steps), digest=digest)
                if key == "margin_pct":
                    entry["unit"] = "%"
                else:
                    entry.update(unit=currency, currency=currency)
            else:
                entry["steps"] = ["revenue is zero: no margin % on price"]
    else:
        why = (f"{goods_lines - len(totals)} of {goods_lines} item line(s) have no margin"
               if len(totals) != goods_lines else
               "lines in several currencies" if len(currencies) > 1 else
               "the line set, the invoiced revenue or its credits are not proven whole "
               "(see the gaps)")
        for entry in out_totals:
            entry["steps"] = [f"no order total: {why}"[:MAX_STEP_CHARS]]
    landed = {"name": f"{name}_landed_estimate"[:64], "kind": "computed", "value": None,
              "inputs": [], "steps": [LANDED_NOTE], "check": "not_applicable"}
    return {"entries": out_totals + entries + unattributed + [landed], "gaps": run.gaps(),
            "reasons": run.reasons}


def _credits(run: _Run, billing: Mapping[str, set[str]], held_back: dict[str, set[str]]
             ) -> tuple[dict[str, list[dict[str, Any]]], list[str], bool]:
    """(item object -> the Item lines of posted BC credit memos applied to the order's
    invoices, credit lines no item takes, whether an applied memo is unsettled). A line
    counts as goods only when its line type reads Item, whatever its item link says."""
    applies = run.targets("credit_link")
    applied = {memo for memo, invoices in applies.items()
               if _binding(memo).startswith("bc.") and invoices & set(billing)}
    statuses = {e["object_id"]: _text(e) for e in run.got["credit_status"]}
    memo_of_line = run.targets("credit_line_link")
    item_of_line = run.targets("credit_item")
    line_types = {e["object_id"]: _text(e) for e in run.got["credit_line_type"]}
    returned = run.by_object("returned")
    credits: dict[str, list[dict[str, Any]]] = {}
    stray: list[str] = []
    unsettled = False
    for entry in run.got["credit"]:
        oid = entry["object_id"]
        memos = memo_of_line.get(oid, set()) & applied
        if not _binding(oid).startswith("bc.") or len(memos) != 1:
            continue
        memo = next(iter(memos))
        status = statuses.get(memo)
        if status in UNPOSTED:
            continue                                      # not a posted credit
        items_ = item_of_line.get(oid, set())
        item = next(iter(items_)) if len(items_) == 1 else None
        line_type = line_types.get(oid)
        if line_type != ITEM_LINE:
            if line_type is None and item is not None:
                held_back.setdefault(item, set()).add("CREDIT_LINE_TYPE_UNKNOWN")
            elif _number(entry.get("value")) != 0:
                stray.append(oid)                         # a charge or account credit
            unsettled = unsettled or status not in POSTED
            continue
        if status not in POSTED:
            unsettled = True
            if item is not None:
                held_back.setdefault(item, set()).add("CREDIT_MEMO_UNSETTLED")
            continue
        if item is None:
            stray.append(oid)
        elif entry.get("lead_basis"):
            held_back.setdefault(item, set()).add("LEAD_TIER_CREDIT_EXCLUDED")
        else:
            credits.setdefault(item, []).append(
                {"amount": entry, "quantity": returned.get(oid), "memo": memo})
    return credits, stray, unsettled


def _unattributed(run: _Run) -> tuple[int, list[str]]:
    """(how many, a few object ids of) the posted BC credit memos of the order's customer
    that are applied to no invoice at all. The plan reaches the customer's memos BC states
    no invoiceId for (their ``customer_link`` edges: what this reader may read, never read
    one by one) and resolves their ``applies_to``, which binds nothing when the
    invoiceNumber names no single invoice of the company. Which order such a memo credits
    is not decided, so it is counted and a bounded few cited, never netted."""
    loose = {memo for memo, _ in run.links.get("customer_link", ())
             if _binding(memo).startswith("bc.")}
    open_ = sorted(loose - set(run.targets("credit_link")))
    return len(open_), open_[:CITED_MEMOS]


def _credit_coverage(run: _Run, complete: bool,
                     captured: Callable[[str], bool | None] | None,
                     name: str) -> list[dict[str, Any]]:
    """Typed gaps for credit memos this answer may not net: posted memos of the order's
    customer that no invoice takes (``_unattributed``, counted in an entry whose ``basis``
    cites at most ``CITED_MEMOS`` of them), and credit memos this reader or this capture may
    not hold. The capture proof is the BC family's completeness, or both credit memo units
    claimed complete for the order's company (``captured``); an unknown claim falls back to
    the family. A memo this reader may not read is never counted or cited: the capture gap
    stands for it and never says whether one exists."""
    chosen = run.selected
    out: list[dict[str, Any]] = []
    count, cited = _unattributed(run)
    if count > 0:
        entry = {"name": f"{name}_unattributed_credits"[:64], "kind": "computed",
                 "value": str(count), "inputs": [], "basis": cited, "check": "not_applicable",
                 "steps": [f"{count} posted credit memo(s) of the order's customer applied to "
                           f"no invoice; basis: {len(cited)} of them"]}
        out.append(entry)
        run.gap("CREDIT_MEMO_UNATTRIBUTED", "order margin",
                f"{count} posted credit memo(s) of the order's customer are applied to no "
                "invoice (BC states no invoiceId and the invoiceNumber names no single invoice "
                "of the company): each may credit this order, so it is neither netted nor "
                "ignored; revenue and margin are net only of the credit memos applied to the "
                f"order's invoices. Answer entry {entry['name']} counts them; its basis cites "
                f"{len(cited)} by object id", "credit_memo", "credit_status")
    if chosen["credit_customer"] != 1:
        run.gap("CREDIT_MEMOS_NOT_CAPTURED", "order margin",
                "the order's BC customer and its credit memos were not reached, so credit "
                "memos that name no invoice cannot be checked; revenue and margin are net only "
                "of the credit memos read", "credit_memo")
    elif chosen["credit_hidden"] or chosen["credit_withheld"]:
        run.gap("CREDIT_MEMOS_NOT_CAPTURED", "order margin",
                "this reader may not read every BC credit memo of the order's customer, so a "
                "credit against this order may be unseen; revenue and margin are net only of "
                "the credit memos read", "credit_memo")
    elif not (complete and (chosen["credit_capture"] == "complete" or captured is not None
                            and all(captured(unit) is True for unit in CREDIT_UNITS))):
        run.gap("CREDIT_MEMOS_NOT_CAPTURED", "order margin",
                "the BC capture is not proven complete, so a credit memo against this order may "
                "be unseen; revenue and margin are net only of the credit memos read",
                "credit_memo")
    return out


def _line(run: _Run, label: str, oid: str, ordered: Mapping[str, Any] | None,
          items: set[str], items_of_lines: Mapping[str, list[str]],
          invoice_lines: Mapping[str, list[dict[str, Any]]],
          held_back: Mapping[str, set[str]], ledger: Mapping[str, list[dict[str, Any]]],
          base_uom: Mapping[str, Mapping[str, Any]],
          currency_of: Mapping[str, tuple[str | None, str]], complete: bool,
          credits: Mapping[str, list[dict[str, Any]]],
          returns: Mapping[str, list[dict[str, Any]]], *, posted: bool = False,
          whole: bool = False) -> dict[str, Any] | None:
    """One order line's revenue, booked cost and margin entries (null with typed gaps when
    any input is open; ``basis`` starts with the order line, ``posted``: the item); None for
    a line that is not an item line (no item, quantity 0)."""
    qty = run.amount(ordered) if ordered else None
    if not items and qty == 0:
        return None
    base = {"kind": "computed", "value": None, "check": "not_applicable"}
    revenue = dict(base, name=f"{label}_revenue"[:64], inputs=[], steps=[],
                   basis=[oid])
    cost = dict(base, name=f"{label}_booked_cost"[:64], inputs=[], steps=[], basis=[oid])
    margin = dict(base, name=f"{label}_margin"[:64], inputs=[revenue["name"], cost["name"]],
                  steps=[], basis=[oid])
    out = {"entries": [revenue, cost, margin]}

    def stop(code: str, detail: str, *roles: str) -> dict[str, Any]:
        run.gap(code, "order line", detail, *roles, line=label)
        for entry in (revenue, cost, margin):
            entry["steps"] = entry["steps"] or [f"{code}: see the gaps"]
        return out

    if ordered is not None:
        revenue["inputs"].append(ordered["name"])
    if len(items) != 1:
        return stop("LINE_ITEM_UNKNOWN", "the line is linked to no single BC item", "ordered")
    item = next(iter(items))
    if len(items_of_lines.get(item, ())) > 1:
        return stop("LINE_ATTRIBUTION_AMBIGUOUS", "several lines of the order carry this "
                    "item; invoice lines and ledger entries cannot be split between them")
    if not posted and (qty is None or ordered is None):
        return stop("LINE_QUANTITY_UNKNOWN", "no cited ordered quantity re-read for this "
                    "reader", "ordered")
    unit = unit_token(ordered.get("unit")) if ordered is not None else None
    rows = invoice_lines.get(item, [])
    credited_rows = credits.get(item, [])
    revenue["inputs"] += [r["amount"]["name"] for r in rows + credited_rows] + [
        r["quantity"]["name"] for r in rows + credited_rows if r["quantity"]]
    revenue["basis"] = [oid] + sorted({r["amount"]["object_id"]
                                       for r in rows + credited_rows})[:99]
    for code in sorted(held_back.get(item, ())):
        return stop(code, {"LEAD_TIER_INVOICE_EXCLUDED": "an invoice line of this item rests "
                           "on a lead-tier link and never enters a sum",
                           "LEAD_TIER_CREDIT_EXCLUDED": "a credit memo line of this item rests "
                           "on a lead-tier link and never enters a sum",
                           "INVOICE_CORRECTED": "an invoice of this item was cancelled or "
                           "corrected; what corrects it is not decided",
                           "INVOICE_STATUS_UNKNOWN": "an invoice of this item has a status "
                           "that is not a posted one",
                           "CREDIT_MEMO_UNSETTLED": "a credit memo applied to the order "
                           "credits this item with a status that is not a posted one",
                           "CREDIT_LINE_TYPE_UNKNOWN": "a credit memo line applied to the order "
                           "names this item but its line type was not read"}[code],
                    "invoice_status", "revenue", "credit_status", "credit_line_type")
    if not rows:
        return stop("LINE_NOT_INVOICED", "no posted invoice line of this item bills the "
                    "order", "revenue", "invoiced")
    if posted:   # no ordered quantity: the invoice lines' one unit
        unit = unit_token((rows[0]["quantity"] or {}).get("unit"))
    amounts, invoiced, currencies, how = [], Decimal(0), set(), set()
    for row in rows:
        amount = run.amount(row["amount"])
        quantity = run.amount(row["quantity"]) if row["quantity"] else None
        if amount is None or quantity is None:
            return stop("CITED_VALUE_UNRESOLVED", "an invoice line's cited amount or quantity "
                        "did not re-read to the projected value", "revenue", "invoiced")
        if unit is None or unit_token(row["quantity"].get("unit")) != unit:
            return stop("INVOICED_QUANTITY_UNVERIFIED", "the invoiced quantities are not in "
                        "one unit (the ordered quantity's); no conversion is assumed",
                        "invoiced")
        currency, reading = currency_of.get(row["invoice"], (None, "invoice not read"))
        if currency is None:
            return stop("INVOICE_CURRENCY_UNKNOWN", reading, "invoice")
        if _corrected_in_another_currency(row["amount"], currency):
            return stop("CORRECTION_CURRENCY_DIFFERS", "a person's correction of an invoice "
                        "line's amount states a currency other than the cited invoice's; "
                        "nothing is converted", "revenue", "invoice")
        amounts.append(amount)
        invoiced += quantity
        currencies.add(currency)
        how.add(reading)
    if posted:
        failed = _posted_whole(run, item, unit, invoiced, ledger, base_uom, whole)
        if failed is not None:
            return stop(*failed)
        of_what = f"{_dec_str(invoiced)} shipped (BC no longer serves the order)"
    elif invoiced != qty:
        return stop("LINE_PARTIALLY_INVOICED" if invoiced < qty else "INVOICED_EXCEEDS_ORDERED",
                    "the posted invoiced quantity differs from the ordered quantity; a margin "
                    "of part of a line is not stated", "invoiced", "ordered")
    else:
        of_what = f"{_dec_str(qty)} ordered"
    credited, returned = [], Decimal(0)
    per_memo: dict[str, Decimal] = {}     # memo object -> quantity it credits of this item
    for row in credited_rows:
        amount = run.amount(row["amount"])
        quantity = run.amount(row["quantity"]) if row["quantity"] else None
        if amount is None or quantity is None:
            return stop("CITED_VALUE_UNRESOLVED", "a credit memo line's cited amount or "
                        "quantity did not re-read to the projected value", "credit", "returned")
        if unit_token(row["quantity"].get("unit")) != unit:
            return stop("CREDITED_QUANTITY_UNVERIFIED", "the credited quantity is not in the "
                        "ordered quantity's unit; no conversion is assumed", "returned")
        currency, reading = currency_of.get(row["memo"], (None, "credit memo not read"))
        if currency is None:
            return stop("CREDIT_MEMO_CURRENCY_UNKNOWN", reading, "credit_memo")
        if _corrected_in_another_currency(row["amount"], currency):
            return stop("CORRECTION_CURRENCY_DIFFERS", "a person's correction of a credit "
                        "memo line's amount states a currency other than the cited memo's; "
                        "nothing is converted", "credit", "credit_memo")
        credited.append(amount)
        returned += quantity
        per_memo[row["memo"]] = per_memo.get(row["memo"], Decimal(0)) + quantity
        currencies.add(currency)
        how.add(reading)
    if len(currencies) != 1:
        return stop("MIXED_CURRENCY_NO_CITED_RATE", "the line's invoices and credit memos are "
                    "in " + " and ".join(sorted(currencies)) + "; nothing is converted",
                    "invoice", "credit_memo")
    currency = next(iter(currencies))
    invoiced_revenue = sum(amounts, Decimal(0))
    credit_total = sum(credited, Decimal(0))
    total_revenue = invoiced_revenue - credit_total
    revenue["steps"] = [f"invoiced = sum of {len(amounts)} posted invoice line amount(s) "
                        f"excluding tax = {_money(invoiced_revenue, currency)} for "
                        f"{_dec_str(invoiced)} {unit} invoiced of {of_what}"]
    if credited:
        revenue["steps"].append(
            f"credited = sum of {len(credited)} Item line amount(s) excluding tax of "
            f"{len(per_memo)} posted credit memo(s) applied to the order's invoices = "
            f"{_money(credit_total, currency)} for {_dec_str(returned)} {unit} credited")
    revenue["steps"] += [f"revenue = invoiced - credited = {_money(total_revenue, currency)}",
                         "currency: " + "; ".join(sorted(how))]
    revenue["steps"] = revenue["steps"][:40]
    revenue.update(value=_dec_str(total_revenue), unit=currency, currency=currency,
                   digest=_sha(_canon({"revenue": revenue["steps"],
                                       "inputs": revenue["inputs"]})))
    entries = ledger.get(item, [])
    # A memo's return is a ledger entry posted by that memo alone, never another memo's.
    back_of = {memo: [e for e in returns.get(item, []) if e["memos"] == {memo}]
               for memo in per_memo}
    back = [e for memo in sorted(back_of) for e in back_of[memo]]
    cost["inputs"] = [e["cost"]["name"] for e in entries + back] + [
        e["quantity"]["name"] for e in entries + back if e["quantity"]]
    cost["basis"] = [oid] + sorted({e["cost"]["object_id"] for e in entries + back})[:99]
    if not entries:
        return stop("COST_LINES_MISSING", "no Sale item ledger entry of this item is posted "
                    "on the order's shipments; a missing cost is never zero", "cost")
    if any(quantity != 0 and not back_of[memo] for memo, quantity in per_memo.items()):
        return stop("RETURN_NOT_IN_LEDGER", "a credit memo credits goods of this item whose "
                    "return is on no Sales Credit Memo item ledger entry of that memo (a return "
                    "receipt, or none posted); its returned goods' cost is unknown, never zero "
                    "and never another memo's", "cost")
    if any(e["lead"] for e in entries + back):
        return stop("LEAD_TIER_COST_EXCLUDED", "a ledger entry of this item rests on a "
                    "lead-tier link and never enters a sum", "cost")
    costs, shipped, back_costs = [], Decimal(0), []
    taken_back: dict[str, Decimal] = {memo: Decimal(0) for memo in per_memo}
    for entry, memo in [(e, None) for e in entries] + [
            (e, m) for m in sorted(back_of) for e in back_of[m]]:
        value = run.amount(entry["cost"])
        quantity = run.amount(entry["quantity"]) if entry["quantity"] else None
        if value is None or quantity is None:
            return stop("COST_NOT_YET_ACTUAL" if _number(entry["cost"].get("value")) in
                        (None, Decimal(0)) else "CITED_VALUE_UNRESOLVED",
                        "a ledger entry has no actual cost (its expected cost is not served by "
                        "the BC API) or did not re-read", "cost", "ledger_quantity")
        if value == 0:
            return stop("COST_NOT_YET_ACTUAL", "a ledger entry's actual cost is zero: its "
                        "cost is not yet invoiced (expected cost is not served by the BC API)",
                        "cost")
        cost_currency = entry["cost"].get("currency")
        if cost_currency != currency:
            return stop("MIXED_CURRENCY_NO_CITED_RATE", f"revenue is in {currency}, booked "
                        f"cost in {cost_currency or 'an unstated currency'}; nothing is "
                        "converted", "cost", "invoice")
        if memo is not None:                # by document type, never by sign
            back_costs.append(value)
            taken_back[memo] += quantity
        else:
            costs.append(value)
            shipped -= quantity
    base = unit_token(_text(base_uom.get(item)))
    if base == unit:
        if shipped != invoiced or taken_back != per_memo:   # per memo, nothing converted
            return stop("COST_QUANTITY_MISMATCH", "the ledger entries' quantity in the item's "
                        "base unit differs from the invoiced (or credited) quantity, so their "
                        "cost is not the line's cost", "ledger_quantity", "invoiced", "returned",
                        "base_uom")
        proof = f"ledger quantity {_dec_str(shipped)} {unit} = invoiced quantity"
        if back:
            proof += (f"; returned {_dec_str(sum(taken_back.values(), Decimal(0)))} {unit} = "
                      "credited quantity, memo by memo")
    elif run.capture == "complete" and complete:
        proof = "ledger quantity not comparable (base unit differs); the BC capture is complete"
    else:
        return stop("COST_COMPLETENESS_UNPROVEN", "the item's base unit is not the invoice "
                    "unit, so the ledger entries' quantity cannot prove them whole, and the BC "
                    "capture is not complete", "ledger_quantity", "base_uom")
    shipped_cost = -sum(costs, Decimal(0))
    returned_cost = sum(back_costs, Decimal(0))
    booked = shipped_cost - returned_cost
    cost["steps"] = [f"shipped cost = -(sum of {len(costs)} Sales Shipment ledger Cost Amount "
                     f"(Actual)) = {_money(shipped_cost, currency)}"]
    if back:
        cost["steps"].append(f"returned cost = sum of {len(back_costs)} Sales Credit Memo "
                             f"ledger Cost Amount (Actual) = {_money(returned_cost, currency)}")
    cost["steps"] += [f"booked cost = shipped cost - returned cost = "
                      f"{_money(booked, currency)}", proof]
    cost["steps"] = cost["steps"][:40]
    cost.update(value=_dec_str(booked), unit=currency, currency=currency,
                digest=_sha(_canon({"cost": cost["steps"], "inputs": cost["inputs"]})))
    value = total_revenue - booked
    margin["steps"] = [f"margin = revenue {_money(total_revenue, currency)} - booked cost "
                       f"{_money(booked, currency)} = {_money(value, currency)}"]
    if total_revenue != 0:
        pct = (value * 100 / total_revenue).quantize(Decimal("0.01"), ROUND_HALF_EVEN)
        margin["steps"].append(f"margin % on price = {_dec_str(pct)}")
    margin.update(value=_dec_str(value), unit=currency, currency=currency,
                  digest=_sha(_canon({"margin": margin["steps"], "revenue": revenue["digest"],
                                      "cost": cost["digest"]})))
    out["figures"] = (label, total_revenue, booked, currency,
                      [revenue["digest"], cost["digest"]])
    return out


def _posted_whole(run: _Run, item: str, unit: str | None, invoiced: Decimal,
                  ledger: Mapping[str, list[dict[str, Any]]],
                  base_uom: Mapping[str, Mapping[str, Any]],
                  whole: bool) -> tuple[str, ...] | None:
    """(gap code, detail, roles) when an item line of an order BC no longer serves is not
    proven whole, else None: its invoiced quantity must equal its shipped item-ledger
    quantity, in the item's base unit as the invoice unit (nothing converted), over invoices
    and shipments proven whole for this reader (``whole``)."""
    if not whole:
        return ("ORDER_LINES_INCOMPLETE", "BC no longer serves the order: its lines are its "
                "posted invoice lines and shipments, which are not proven whole (a truncated "
                "run, a reader whose scope may hide one, or an incomplete BC capture)",
                "invoiced", "ledger_quantity")
    if unit is None or unit_token(_text(base_uom.get(item))) != unit:
        return ("POSTED_LINE_UNPROVEN", "BC no longer serves the order, so a line is whole "
                "only when its invoiced quantity equals its shipped item-ledger quantity; the "
                "item's base unit is not the invoice unit, so they do not compare",
                "invoiced", "base_uom")
    entries = ledger.get(item, [])
    if any(e["lead"] for e in entries):
        return ("LEAD_TIER_COST_EXCLUDED", "a ledger entry of this item rests on a lead-tier "
                "link and never enters a sum", "cost")
    shipped = Decimal(0)
    for entry in entries:
        quantity = run.amount(entry["quantity"]) if entry["quantity"] else None
        if quantity is None:
            return ("CITED_VALUE_UNRESOLVED", "a ledger entry's cited quantity did not "
                    "re-read to the projected value", "ledger_quantity")
        shipped -= quantity
    if invoiced != shipped:
        return ("LINE_PARTIALLY_INVOICED" if invoiced < shipped else
                "INVOICED_EXCEEDS_SHIPPED", "the posted invoiced quantity differs from the "
                "shipped item-ledger quantity; a margin of part of a line is not stated",
                "invoiced", "ledger_quantity")
    return None


# ``reviewed_inputs``: a person's correction in force is computed from (``_Run.amount``).
PLAN_ENGINES = {ENGINE: {"check": plan_check, "run": plan_run, "reviewed_inputs": True}}

__all__ = ["ENGINE", "LANDED_NOTE", "PLAN_ENGINES", "plan_check", "plan_run"]
