"""The oracle's own order margin of a Business Central sales order (family F10, held-out lane).

Computed from raw BC records only (the GET-only reads of ``evals.oracle.source.BcReads``
over an injected ``BcPort``: live BC through ``integrations.bc_read``, a BC capture store, or
the synthetic ``fixture_world`` below). It never imports ``ontology/`` or ``operating/``: it
shares no code with the projection, ``plx.investigate`` or ``operating/order_margin.py``, so
it can score them (10-DECISION decision 8, independent oracle lane).

Rules, read from BC API v2.0 semantics and the Portal where it has one:

* the order: exactly one ``salesOrders`` record with that ``number`` in the company, or
  none (BC deletes an order once it is shipped and invoiced) when that read was complete and
  a ``salesInvoices`` or ``salesShipments`` record of the company names it (``orderNumber``);
* its item lines: ``salesOrderLines`` of ``lineType`` Item; other line types with a quantity
  hold revenue no item line takes (the total is then undecided), comment lines are skipped.
  An order BC no longer serves has one line per item of its posted invoice lines (lineType
  Item) and its shipped Sale ledger entries, keyed by the ``items`` record id; it is decided
  only when every read was complete, the item's base unit is the invoice unit and the
  invoiced quantity equals the shipped ledger quantity (there is no ordered quantity), and
  with a credit memo read of its company not complete it is not read at all (``blocked``:
  undecided, so a caveated product figure is neither right nor wrong);
* revenue of an item line: the ``amountExcludingTax`` of the invoice lines (lineType Item, the
  same ``lineObjectNumber``) of the ``salesInvoices`` whose ``orderNumber`` is the order's
  number and whose ``status`` is Open or Paid (Draft and In Review are not posted; Canceled
  and Corrective are undecided), less the ``amountExcludingTax`` of the Item lines (same
  ``lineObjectNumber``) of the posted (Open or Paid) ``salesCreditMemos`` whose ``invoiceId``
  is one of those invoices, or (``invoiceId`` blank or nil) whose ``invoiceNumber`` names
  exactly one invoice of this company and that invoice is one of them: a credit memo is
  subtracted because it is a credit memo, never
  because of a sign. A credit memo of another status applied to the order leaves its items
  (and the total) undecided; its Draft and In Review ones are not posted;
* the line is decided only when its invoiced quantity (same unit code) equals its ordered
  quantity, the credited lines are in that unit too, in one currency over the invoices and
  credit memos: ``currencyCode``, blank meaning the company's local currency CAD on the
  manufacturing and distribution books (Portal ``bcTransactionCurrency``);
* booked cost: the shipped cost less the returned cost. Shipped cost: minus the
  ``costAmountActual`` of the ``itemLedgerEntries`` with ``entryType`` Sale and
  ``documentType`` Sales Shipment whose ``documentNumber`` is a ``salesShipments`` number of
  the order, same ``itemNumber``. Returned cost: the ``costAmountActual`` of the entries with
  ``entryType`` Sale and ``documentType`` Sales Credit Memo whose ``documentNumber`` is the
  number of a netted credit memo, same item, matched memo by memo: a memo crediting a
  quantity of the item with no such entry of its own (a return through a return receipt, or
  none posted) leaves the cost undecided, never covered by another memo's entry. None, or
  any zero/empty actual cost, leaves it undecided; the ledger quantities (base unit, from
  ``items.baseUnitOfMeasureCode``) must equal the invoiced quantity and, memo by memo, the
  credited quantity when the base unit is the line unit (nothing is converted), else every
  read must have been complete. Ledger amounts are the
  company's local currency (CAD on those two books): another invoice currency leaves the
  margin undecided;
* two item lines with the same item are undecided (the documents cannot be split);
* the total only when every item line is decided, every posted invoice line and every
  credit memo line with an amount (a Charge or Account credit is never goods) of the order
  is taken by an item line and one currency holds; margin % = margin / revenue x 100.

Caveats (``caveats``): ``credit_memos_not_read`` when a credit memo read (by invoice, by the
order's customer, or a memo's lines) was not complete or the order names no customer, so a
credit may be unseen; ``credit_memo_unattributed`` when a posted credit memo to the order's
``customerNumber`` cannot be attributed to an invoice: BC states no ``invoiceId`` (blank or
nil) and its ``invoiceNumber`` is blank or names no single invoice of the company (none, or
two). Nothing says which order such a memo credits, so it is never netted and the figures
are net only of the attributable memos. The runner scores a product figure stated without the
matching gap as wrong.

``reading`` returns per line (keyed by the BC line id) and for the total the decided values
(decimal text) or None with a reason, plus the BC documents read (``sources``) for partition
hygiene. ``partition`` keeps related documents and near-duplicates in one partition.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
import hashlib
from typing import Any, Iterable, Mapping, Sequence

from .source import BcReads, ReadFailed, decimal_of, decimal_text, key_text

FAMILY = "F10"
FIGURES = ("revenue", "booked_cost", "margin")
POSTED = frozenset({"open", "paid"})
UNPOSTED = frozenset({"draft", "in review"})
LOCAL_CURRENCY = {"manufacturing": "CAD", "distribution": "CAD"}
PARTITIONS = ("dev", "held_out")
SALT = "plx-eval-split/v1:order_margin"
CREDIT_CAVEAT = "credit_memos_not_read"
UNATTRIBUTED_CAVEAT = "credit_memo_unattributed"
NIL_ID = "00000000-0000-0000-0000-000000000000"  # BC's blank Guid (invoiceId of no invoice)
MANY_MEMOS = 12  # fixture customer C-5's credit memos on other invoices (a repeat customer)
LOOSE_MEMOS = 60  # fixture customer C-9's posted credit memos that state no invoice at all
NUMBERED_MEMOS = 20  # and C-9's memos applied by invoiceNumber alone to invoices of no order
FIXTURE_SETS = ("salesOrders", "salesInvoices", "salesShipments", "salesCreditMemos",
                "customers", "items", "itemLedgerEntries")


def _word(value: Any) -> str | None:
    text = key_text(value)
    return " ".join(text.replace("_x0020_", " ").split()).casefold() if text else None


def _code(value: Any) -> str:
    return (key_text(value) or "").strip().upper()


def _nil(value: Any) -> bool:
    """Whether BC states no record here: blank, or the nil Guid."""
    return (key_text(value) or "").strip(" ") in ("", NIL_ID)


def reading(reads: BcReads, number: str, *, book: str | None) -> dict[str, Any]:
    """The oracle's order margin of one company's sales order (``reads`` is that company's).
    ``{"blocked": reason}`` when the order cannot be read exactly once, or BC no longer
    serves it and a credit memo read was not complete."""
    complete = True
    credits_read = True
    memos_whole = True   # every credit memo read (find and lines) was complete
    sources: set[str] = set()

    def find(entity_set: str, field: str, value: Any, *,
             cite: bool = True) -> list[dict[str, Any]]:
        nonlocal complete, credits_read, memos_whole
        records, done = reads.find(entity_set, field, value)
        complete = complete and done
        if entity_set == "salesCreditMemos":
            credits_read = credits_read and done
            memos_whole = memos_whole and done
        if cite and entity_set not in ("items", "customers"):  # master data relates no orders
            sources.update(f"bc:{entity_set}:{r.get('id')}" for r in records)
        return records

    def lines(record: Mapping[str, Any]) -> list[dict[str, Any]]:
        nonlocal complete, credits_read, memos_whole
        records, done = reads.lines(record)
        complete = complete and done
        if record.get("entity_set") == "salesCreditMemos":
            credits_read = credits_read and done
            memos_whole = memos_whole and done
        return records

    def currency(fields: Mapping[str, Any]) -> str | None:
        return _code(fields.get("currencyCode")) or LOCAL_CURRENCY.get(book or "")

    try:
        orders = find("salesOrders", "number", number)
        if len(orders) > 1:
            return {"blocked": "order_ambiguous"}
        unserved = not orders
        if unserved and not complete:   # an order an incomplete read missed may be served
            return {"blocked": "order_read_incomplete"}
        order_lines = lines(orders[0]) if orders else []
        invoices = find("salesInvoices", "orderNumber", number)
        shipments = find("salesShipments", "orderNumber", number)
        if unserved and not invoices and not shipments:
            return {"blocked": "order_not_found"}
        posted: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
        doubtful: set[str] = set()
        currencies: dict[str, str | None] = {}
        credits: list[tuple[str, str | None, dict[str, Any]]] = []  # (memo no., currency, line)
        unsettled_credit = False
        for invoice in invoices:
            status = _word(invoice["fields"].get("status"))
            if status in UNPOSTED:
                continue
            for line in lines(invoice):
                fields = line["fields"]
                if status not in POSTED:
                    doubtful.add(_code(fields.get("lineObjectNumber")))
                    continue
                posted.append((invoice, fields))
            currencies[str(invoice["id"])] = currency(invoice["fields"])
            applied = find("salesCreditMemos", "invoiceId", invoice["id"])
            # BC leaves invoiceId blank or nil on some posted memos and states invoiceNumber
            # only: such a memo is applied to the invoice that number names, and only where
            # it names exactly one invoice of this company (never a guess).
            invoice_number = key_text(invoice["fields"].get("number"))
            if invoice_number and len(find("salesInvoices", "number",
                                           invoice_number, cite=False)) == 1:
                applied += [m for m in find("salesCreditMemos", "invoiceNumber",
                                            invoice_number)
                            if _nil(m["fields"].get("invoiceId"))]
            for memo in applied:
                memo_status = _word(memo["fields"].get("status"))
                if memo_status in UNPOSTED:
                    continue
                for line in lines(memo):
                    fields = line["fields"]
                    if memo_status not in POSTED:
                        unsettled_credit = True
                        if _word(fields.get("lineType")) == "item":
                            doubtful.add(_code(fields.get("lineObjectNumber")))
                        continue
                    credits.append((key_text(memo["fields"].get("number")) or "",
                                    currency(memo["fields"]), fields))
        ledger: list[dict[str, Any]] = []
        for shipment in shipments:
            number_of = key_text(shipment["fields"].get("number"))
            for entry in find("itemLedgerEntries", "documentNumber", number_of):
                fields = entry["fields"]
                if (_word(fields.get("entryType")) == "sale"
                        and _word(fields.get("documentType")) == "sales shipment"):
                    ledger.append(fields)
        returns: dict[str, list[dict[str, Any]]] = {}
        for memo_number in {m for m, _, _ in credits} - {""}:
            for entry in find("itemLedgerEntries", "documentNumber", memo_number):
                fields = entry["fields"]
                if (_word(fields.get("entryType")) == "sale"
                        and _word(fields.get("documentType")) == "sales credit memo"):
                    returns.setdefault(memo_number, []).append(fields)
        unattributed = False
        if orders:
            customer = key_text(orders[0]["fields"].get("customerNumber"))
        else:   # the documents of an order BC no longer serves name its customer
            named = {key_text(r["fields"].get("customerNumber")) for r in invoices} or {
                key_text(r["fields"].get("customerNumber")) for r in shipments}
            customer = next(iter(named)) if len(named) == 1 else None
        if customer is None:
            credits_read = False
        for memo in (find("salesCreditMemos", "customerNumber", customer, cite=False)
                     if customer else ()):
            fields = memo["fields"]
            if _word(fields.get("status")) not in POSTED:
                continue
            # What the memo applies to is decided only by invoiceId, or (none stated) by an
            # invoiceNumber that names exactly one invoice of this company. Neither: nothing
            # says which order it credits, so no figure of this customer's orders is a
            # complete net margin.
            if _nil(fields.get("invoiceId")):
                applies_to = (key_text(fields.get("invoiceNumber")) or "").strip(" ")
                if not applies_to or len(find("salesInvoices", "number", applies_to,
                                              cite=False)) != 1:
                    unattributed = True
                    sources.add(f"bc:salesCreditMemos:{memo.get('id')}")
        if orders:
            codes = {_code(line["fields"].get("lineObjectNumber")) for line in order_lines}
        else:
            codes = ({_code(f.get("lineObjectNumber")) for _, f in posted
                      if _word(f.get("lineType")) == "item"}
                     | {_code(e.get("itemNumber")) for e in ledger} | doubtful)
        base_units: dict[str, str] = {}
        item_ids: dict[str, str] = {}
        for item in sorted(codes - {""}):
            found = find("items", "number", item)
            if len(found) == 1:
                base_units[item] = _code(found[0]["fields"].get("baseUnitOfMeasureCode"))
                item_ids[item] = str(found[0]["id"])
    except ReadFailed as exc:
        return {"blocked": f"read_failed:{exc.reason}"[:80]}
    if unserved and not memos_whole:
        # Only complete reads prove an order BC no longer serves whole, and an unseen memo
        # may be what credits it: undecided (excluded), never a figure to score against.
        return {"blocked": "credit_memos_not_read", "sources": sorted(sources)}

    if orders:
        item_lines = [(str(line["id"]), _code(line["fields"].get("lineObjectNumber")),
                       line["fields"]) for line in order_lines
                      if _word(line["fields"].get("lineType")) == "item"]
    else:   # one line per item, keyed by its items record (the product's line basis)
        item_lines = [(item_ids.get(code, f"item:{code}"), code, None)
                      for code in sorted(codes - {""})]
    other = [line for line in order_lines if _word(line["fields"].get("lineType")) != "item"
             and (decimal_of(line["fields"].get("quantity")) or 0) != 0]
    per_item: dict[str, int] = {}
    for _, code, _ in item_lines:
        per_item[code] = per_item.get(code, 0) + 1
    out_lines: dict[str, dict[str, Any]] = {}
    decided: list[tuple[Decimal, Decimal, str | None]] = []
    for key, item, fields in item_lines:
        figures = _line(item, fields, per_item, posted, doubtful, currencies, credits, ledger,
                        returns, base_units, complete)
        out_lines[key] = figures
        if figures["margin"] is not None:
            decided.append((Decimal(figures["revenue"]), Decimal(figures["booked_cost"]),
                            figures["currency"]))
    taken = set(per_item)
    stray = [f for _, f in posted if _word(f.get("lineType")) != "item"
             or _code(f.get("lineObjectNumber")) not in taken]
    stray += [f for _, _, f in credits
              if (_word(f.get("lineType")) != "item"
                  and (decimal_of(f.get("amountExcludingTax")) or 0) != 0)
              or (_word(f.get("lineType")) == "item"
                  and _code(f.get("lineObjectNumber")) not in taken)]
    total: dict[str, Any] = {k: None for k in FIGURES + ("margin_pct",)}
    if not item_lines:
        total["reason"] = "no_item_lines"
    elif len(decided) != len(item_lines):
        total["reason"] = "line_undecided"
    elif stray or other or unsettled_credit:
        total["reason"] = "revenue_not_taken_by_an_item_line"
    elif len({c for _, _, c in decided}) != 1:
        total["reason"] = "several_currencies"
    else:
        revenue = sum((r for r, _, _ in decided), Decimal(0))
        cost = sum((c for _, c, _ in decided), Decimal(0))
        total.update(revenue=decimal_text(revenue), booked_cost=decimal_text(cost),
                     margin=decimal_text(revenue - cost), reason="decided",
                     currency=decided[0][2])
        if revenue:
            total["margin_pct"] = decimal_text(((revenue - cost) * 100 / revenue).quantize(
                Decimal("0.01"), ROUND_HALF_EVEN))
    caveats = ([CREDIT_CAVEAT] if not credits_read else []) + (
        [UNATTRIBUTED_CAVEAT] if unattributed else [])
    return {"lines": out_lines, "total": total, "complete": complete,
            "caveats": caveats, "sources": sorted(sources)}


def _line(item: str, fields: Mapping[str, Any] | None, per_item: Mapping[str, int],
          posted: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], doubtful: set[str],
          currencies: Mapping[str, str | None],
          credits: Sequence[tuple[str, str | None, Mapping[str, Any]]],
          ledger: Sequence[Mapping[str, Any]], returns: Mapping[str, Sequence[Mapping[str, Any]]],
          base_units: Mapping[str, str], complete: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"revenue": None, "booked_cost": None, "margin": None,
                           "currency": None}

    def undecided(reason: str) -> dict[str, Any]:
        out["reason"] = reason
        return out

    if not item or per_item.get(item, 0) > 1:
        return undecided("item_unknown_or_repeated")
    if item in doubtful:
        return undecided("invoice_not_settled")
    mine = [(inv, f) for inv, f in posted if _word(f.get("lineType")) == "item"
            and _code(f.get("lineObjectNumber")) == item]
    # ``fields`` None: BC no longer serves the order, so there is no ordered quantity.
    ordered = decimal_of(fields.get("quantity")) if fields is not None else None
    if not mine or (fields is not None and ordered is None):
        return undecided("not_invoiced")
    unit = _code((fields if fields is not None else mine[0][1]).get("unitOfMeasureCode"))
    revenue, invoiced, used = Decimal(0), Decimal(0), set()
    for invoice, line in mine:
        amount, quantity = decimal_of(line.get("amountExcludingTax")), decimal_of(
            line.get("quantity"))
        if amount is None or quantity is None or _code(line.get("unitOfMeasureCode")) != unit:
            return undecided("invoice_line_unreadable")
        revenue += amount
        invoiced += quantity
        used.add(currencies.get(str(invoice["id"])))
    entries = [e for e in ledger if _code(e.get("itemNumber")) == item]
    if fields is None:   # whole when the invoiced quantity is the shipped ledger quantity
        if not complete:
            return undecided("reads_incomplete")
        if base_units.get(item) != unit:
            return undecided("ledger_whole_unproven")
        shipped = [decimal_of(e.get("quantity")) for e in entries]
        if None in shipped:
            return undecided("ledger_quantity_unreadable")
        ordered = -sum(shipped, Decimal(0))  # type: ignore[arg-type]
    if invoiced != ordered:
        return undecided("not_wholly_invoiced")
    credited = Decimal(0)
    per_memo: dict[str, Decimal] = {}   # memo number -> quantity it credits of this item
    for memo_number, memo_currency, line in credits:
        if _word(line.get("lineType")) != "item" or _code(line.get("lineObjectNumber")) != item:
            continue
        amount, quantity = decimal_of(line.get("amountExcludingTax")), decimal_of(
            line.get("quantity"))
        if amount is None or quantity is None or _code(line.get("unitOfMeasureCode")) != unit:
            return undecided("credit_line_unreadable")
        credited += amount
        used.add(memo_currency)
        per_memo[memo_number] = per_memo.get(memo_number, Decimal(0)) + quantity
    if len(used) != 1 or None in used:
        return undecided("invoice_currency")
    currency = next(iter(used))
    revenue -= credited
    out.update(revenue=decimal_text(revenue), currency=currency)
    # Each memo's return is its own ledger entries (same item), never another memo's.
    back_of = {m: [e for e in returns.get(m, ()) if _code(e.get("itemNumber")) == item]
               for m in per_memo}
    back = [e for m in sorted(back_of) for e in back_of[m]]
    costs = [decimal_of(e.get("costAmountActual")) for e in entries]
    back_costs = [decimal_of(e.get("costAmountActual")) for e in back]
    if not entries or any(c is None or c == 0 for c in costs + back_costs):
        return undecided("cost_not_booked")
    if any(q != 0 and not back_of[m] for m, q in per_memo.items()):
        return undecided("return_not_in_ledger")
    if currency != "CAD":
        return undecided("cost_currency_differs")
    shipped = -sum((decimal_of(e.get("quantity")) or Decimal(0) for e in entries), Decimal(0))
    if base_units.get(item) == unit:   # same unit: no conversion, quantities compare per memo
        if shipped != invoiced or any(
                sum((decimal_of(e.get("quantity")) or Decimal(0) for e in back_of[m]),
                    Decimal(0)) != q for m, q in per_memo.items()):
            return undecided("ledger_quantity_differs")
    elif not complete:
        return undecided("ledger_whole_unproven")
    cost = -sum(costs, Decimal(0)) - sum(back_costs, Decimal(0))  # type: ignore[arg-type]
    out.update(booked_cost=decimal_text(cost), margin=decimal_text(revenue - cost),
               reason="decided")
    return out


# ----------------------------------------------------------------- partitions

def partition(items: Sequence[Mapping[str, Any]], *, salt: str = SALT) -> list[str]:
    """``dev`` or ``held_out`` per item, by group: items that read one BC document (an
    invoice, shipment, ledger entry) or name the same order number in any company (a
    near-duplicate) form one group, and a group is wholly in one partition (a third dev)."""
    parent = list(range(len(items)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for index, item in enumerate(items):
        for key in group_keys(item):
            if key in owner:
                parent[root(index)] = root(owner[key])
            else:
                owner[key] = index
    names: dict[int, str] = {}
    for index, item in enumerate(items):
        names[root(index)] = min(names.get(root(index), "~"), min(group_keys(item)))
    out = []
    for index in range(len(items)):
        bucket = int(hashlib.sha256(f"{salt}\x1f{names[root(index)]}".encode()).hexdigest(), 16)
        out.append(PARTITIONS[0] if bucket % 3 == 0 else PARTITIONS[1])
    return out


def group_keys(item: Mapping[str, Any]) -> set[str]:
    number = "".join(ch for ch in str(item.get("number") or "").casefold() if ch.isalnum())
    return {f"number:{number}", *(f"doc:{s}" for s in item.get("sources") or ())}


def violations(items: Sequence[Mapping[str, Any]], partitions: Sequence[str]) -> int:
    """Pairs of items in different partitions that share a group key (0 when hygienic)."""
    seen: dict[str, str] = {}
    bad = 0
    for item, part in zip(items, partitions):
        for key in group_keys(item):
            if seen.setdefault(key, part) != part:
                bad += 1
    return bad


# ----------------------------------------------------------------- fixture world

class RecordPort:
    """``BcPort`` over one company's raw BC records (``{"company_id", "entity_set", "id",
    "fields", "citation", "document_set"?, "document_id"?}``): exact field equality, lines by
    their document id. ``complete`` says whether the records are a complete read: one flag
    for every set, or per entity set (a set it does not name is incomplete)."""

    def __init__(self, records: Iterable[Mapping[str, Any]], company_id: str, *,
                 complete: bool | Mapping[str, bool] = True) -> None:
        self.records = [dict(r) for r in records
                        if str(r.get("company_id")).lower() == company_id.lower()]
        self.complete = complete

    def _complete(self, entity_set: str | None) -> bool:
        if isinstance(self.complete, Mapping):
            return self.complete.get(entity_set) is True  # type: ignore[arg-type]
        return bool(self.complete)

    def find(self, entity_set: str, field: str, value: str) -> dict[str, Any]:
        return {"records": [r for r in self.records if r["entity_set"] == entity_set
                            and key_text(r["fields"].get(field)) == value],
                "complete": self._complete(entity_set)}

    def lines(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return {"records": [r for r in self.records if r.get("document_set")
                            == record["entity_set"] and r.get("document_id") == record["id"]],
                "complete": self._complete(_LINE_SETS.get(record["entity_set"]))}

    def sample(self, entity_set: str, limit: int) -> dict[str, Any]:
        return {"records": [r for r in self.records if r["entity_set"] == entity_set][:limit],
                "complete": self._complete(entity_set), "source": [entity_set]}


_LINE_SETS = {"salesOrders": "salesOrderLines", "salesInvoices": "salesInvoiceLines",
              "salesCreditMemos": "salesCreditMemoLines"}


def fixture_world(companies: Sequence[str]) -> list[dict[str, Any]]:
    """Synthetic BC sales documents (not PLX data) in two companies that reuse every number:
    SO-1 (one line with booked cost, one without), SO-2 (a USD invoice against CAD cost),
    SO-3 (two invoices and two shipments, wholly booked), SO-4 (partly invoiced), all to
    customer C-1 with no credit memo; SO-5 (an applied credit memo returning 2 of 10, its
    return on the item ledger, and a comment line) and SO-6 (an applied credit memo whose
    only line is a Charge naming the item's number) to C-2; SO-7 to C-3, who also has a
    posted credit memo that names no invoice; SO-8 to C-4, sold in BOX (base unit PCS), with two
    applied credit memos of one box each, one returned on its own Sales Credit Memo ledger
    entry and one through a return receipt (no entry of its own: its return cost is unknown);
    SO-9 to C-5, whose ``MANY_MEMOS`` other posted credit memos all apply to invoices that
    bill no order. Three orders whose customer has a credit memo BC states no ``invoiceId``
    for: SO-10 to C-6, whose memo names an invoice no company holds, SO-11 to C-7, whose
    memo's number names two invoices of the company, and SO-12 to C-8, whose memo names its
    own invoice and is netted (the ``invoiceNumber`` fallback, its return on the ledger).
    SO-13 to C-9, a repeat customer with ``LOOSE_MEMOS`` standalone posted credit memos
    (no invoice stated) and ``NUMBERED_MEMOS`` more applied by number alone to invoices of no
    order: the answer must count the former, cite a few and stay one response. Orders BC no
    longer serves (no ``salesOrders`` record, as after BC deletes a shipped and invoiced one):
    SO-20 to C-1, two invoices and one shipment over FG-1 and FG-2, wholly invoiced, and the
    other company's SO-20 with other amounts; SO-21 to C-1, shipped 10 and invoiced 4."""
    out: list[dict[str, Any]] = []
    counter = [0]

    def gid(company: int, kind: int) -> str:
        counter[0] += 1
        return f"{company + 1:08x}-{kind:04x}-4000-8000-{counter[0]:012x}"

    def add(c: int, entity_set: str, fields: dict[str, Any], *,
            parent: dict[str, Any] | None = None) -> dict[str, Any]:
        kind = (*FIXTURE_SETS, *_LINE_SETS.values()).index(entity_set) + 1
        record = {"company_id": companies[c], "entity_set": entity_set, "id": gid(c, kind),
                  "fields": fields}
        record["fields"] = {"id": record["id"], **fields}
        record["citation"] = f"bc://fixture/{companies[c]}/{entity_set}/{record['id']}@0-1"
        if parent is not None:
            record.update(document_set=parent["entity_set"], document_id=parent["id"])
            record["fields"]["documentId"] = parent["id"]
        out.append(record)
        return record

    def order(c: int, number: str, lines: Sequence[tuple[str | None, int]],
              customer: str = "C-1", unit: str = "PCS") -> None:
        head = add(c, "salesOrders", {"number": number, "currencyCode": "",
                                      "orderDate": "2026-09-01", "customerNumber": customer})
        for seq, (item, qty) in enumerate(lines, start=1):
            add(c, "salesOrderLines", {
                "sequence": 10000 * seq, "lineType": "Item" if item else "Comment",
                "lineObjectNumber": item or "", "quantity": qty,
                "unitOfMeasureCode": unit if item else "", "unitPrice": 5.0},
                parent=head)

    def invoice(c: int, number: str, order_no: str, lines: Sequence[tuple[str, int, float]],
                currency: str = "", status: str = "Open", customer: str = "C-1",
                unit: str = "PCS") -> dict[str, Any]:
        head = add(c, "salesInvoices", {"number": number, "orderNumber": order_no,
                                        "currencyCode": currency, "status": status,
                                        "customerNumber": customer,
                                        "postingDate": "2026-09-05"})
        for seq, (item, qty, amount) in enumerate(lines, start=1):
            add(c, "salesInvoiceLines", {
                "sequence": 10000 * seq, "lineType": "Item", "lineObjectNumber": item,
                "quantity": qty, "unitOfMeasureCode": unit, "unitPrice": amount / qty,
                "amountExcludingTax": amount}, parent=head)
        return head

    def credit_memo(c: int, number: str, applied: dict[str, Any] | None, customer: str,
                    lines: Sequence[tuple[str, str, int, float]], unit: str = "PCS",
                    *, by_number: bool = False, invoice_number: str = "") -> None:
        """``by_number``: BC states no invoiceId for the memo, only the applied invoice's
        number. ``invoice_number`` (with no applied invoice): the number BC states names no
        single invoice of the company, so the memo is applied to none of them."""
        head = add(c, "salesCreditMemos", {
            "number": number, "customerNumber": customer, "currencyCode": "",
            "status": "Open", "postingDate": "2026-09-10",
            "invoiceId": NIL_ID if applied is None or by_number else applied["id"],
            "invoiceNumber": applied["fields"]["number"] if applied else invoice_number})
        for seq, (line_type, code, qty, amount) in enumerate(lines, start=1):
            add(c, "salesCreditMemoLines", {
                "sequence": 10000 * seq, "lineType": line_type, "lineObjectNumber": code,
                "quantity": qty, "unitOfMeasureCode": unit if code else "",
                "unitPrice": amount / qty if qty else 0.0, "amountExcludingTax": amount},
                parent=head)

    def ledger(c: int, document_type: str, number: str,
               entries: Sequence[tuple[str, int, float]]) -> None:
        for item, qty, cost in entries:
            add(c, "itemLedgerEntries", {
                "entryNumber": counter[0], "itemNumber": item, "entryType": "Sale",
                "documentType": document_type, "documentNumber": number,
                "quantity": qty, "costAmountActual": cost, "salesAmountActual": 0})

    def shipment(c: int, number: str, order_no: str,
                 entries: Sequence[tuple[str, int, float]]) -> None:
        add(c, "salesShipments", {"number": number, "orderNumber": order_no,
                                  "postingDate": "2026-09-04"})
        ledger(c, "Sales_x0020_Shipment", number, entries)

    for c in range(len(companies[:2])):
        for item in ("FG-1", "FG-2"):
            add(c, "items", {"number": item, "displayName": "Synthetic good",
                             "baseUnitOfMeasureCode": "PCS", "type": "Inventory"})
        for customer in ("C-1", "C-2", "C-3", "C-4", "C-5", "C-6", "C-7", "C-8",
                         "C-9"):
            add(c, "customers", {"number": customer, "displayName": "Synthetic customer",
                                 "type": "Company", "currencyCode": ""})
    order(0, "SO-1", [("FG-1", 10), ("FG-2", 4), (None, 0)])
    invoice(0, "SI-1", "SO-1", [("FG-1", 10, 50.0), ("FG-2", 4, 40.0)])
    shipment(0, "SS-1", "SO-1", [("FG-1", -10, -21.4)])
    order(0, "SO-2", [("FG-1", 5)])
    invoice(0, "SI-2", "SO-2", [("FG-1", 5, 30.0)], currency="USD", status="Paid")
    shipment(0, "SS-2", "SO-2", [("FG-1", -5, -10.7)])
    order(0, "SO-3", [("FG-1", 2)])
    invoice(0, "SI-3A", "SO-3", [("FG-1", 1, 10.0)])
    invoice(0, "SI-3B", "SO-3", [("FG-1", 1, 10.0)])
    shipment(0, "SS-3A", "SO-3", [("FG-1", -1, -4.0)])
    shipment(0, "SS-3B", "SO-3", [("FG-1", -1, -4.0)])
    order(0, "SO-4", [("FG-1", 10)])
    invoice(0, "SI-4", "SO-4", [("FG-1", 4, 20.0)])
    shipment(0, "SS-4", "SO-4", [("FG-1", -4, -8.56)])
    order(0, "SO-5", [("FG-1", 10)], customer="C-2")
    billed = invoice(0, "SI-5", "SO-5", [("FG-1", 10, 50.0)], customer="C-2")
    shipment(0, "SS-5", "SO-5", [("FG-1", -10, -21.4)])
    credit_memo(0, "CM-5", billed, "C-2", [("Item", "FG-1", 2, 10.0), ("Comment", "", 0, 0.0)])
    ledger(0, "Sales_x0020_Credit_x0020_Memo", "CM-5", [("FG-1", 2, 4.28)])
    order(0, "SO-6", [("FG-2", 4)], customer="C-2")
    billed = invoice(0, "SI-6", "SO-6", [("FG-2", 4, 40.0)], customer="C-2")
    shipment(0, "SS-6", "SO-6", [("FG-2", -4, -8.0)])
    credit_memo(0, "CM-6", billed, "C-2", [("Charge", "FG-2", 1, 5.0)])
    order(0, "SO-7", [("FG-1", 3)], customer="C-3")
    invoice(0, "SI-7", "SO-7", [("FG-1", 3, 15.0)], customer="C-3")
    shipment(0, "SS-7", "SO-7", [("FG-1", -3, -6.42)])
    credit_memo(0, "CM-7", None, "C-3", [("Item", "FG-1", 1, 5.0)])
    order(0, "SO-8", [("FG-1", 10)], customer="C-4", unit="BOX")
    billed = invoice(0, "SI-8", "SO-8", [("FG-1", 10, 100.0)], customer="C-4", unit="BOX")
    shipment(0, "SS-8", "SO-8", [("FG-1", -60, -60.0)])
    credit_memo(0, "CM-8A", billed, "C-4", [("Item", "FG-1", 1, 10.0)], unit="BOX")
    ledger(0, "Sales_x0020_Credit_x0020_Memo", "CM-8A", [("FG-1", 6, 6.0)])
    credit_memo(0, "CM-8B", billed, "C-4", [("Item", "FG-1", 1, 10.0)], unit="BOX")
    ledger(0, "Sales_x0020_Return_x0020_Receipt", "RR-8B", [("FG-1", 6, 6.0)])
    order(0, "SO-9", [("FG-1", 2)], customer="C-5")
    invoice(0, "SI-9", "SO-9", [("FG-1", 2, 20.0)], customer="C-5")
    shipment(0, "SS-9", "SO-9", [("FG-1", -2, -8.0)])
    for n in range(MANY_MEMOS):   # C-5's credits of invoices that bill no order
        billed = invoice(0, f"SI-9X{n}", "", [("FG-2", 1, 10.0)], customer="C-5")
        credit_memo(0, f"CM-9X{n}", billed, "C-5", [("Item", "FG-2", 1, 10.0)])
    order(0, "SO-10", [("FG-1", 3)], customer="C-6")
    invoice(0, "SI-10", "SO-10", [("FG-1", 3, 15.0)], customer="C-6")
    shipment(0, "SS-10", "SO-10", [("FG-1", -3, -6.42)])
    credit_memo(0, "CM-10", None, "C-6", [("Item", "FG-1", 1, 5.0)], invoice_number="SI-NONE")
    order(0, "SO-11", [("FG-1", 3)], customer="C-7")
    invoice(0, "SI-11", "SO-11", [("FG-1", 3, 15.0)], customer="C-7")
    shipment(0, "SS-11", "SO-11", [("FG-1", -3, -6.42)])
    for _ in range(2):            # one number, two invoices: the memo names neither
        invoice(0, "SI-TWICE", "", [("FG-2", 1, 10.0)], customer="C-7")
    credit_memo(0, "CM-11", None, "C-7", [("Item", "FG-1", 1, 5.0)], invoice_number="SI-TWICE")
    order(0, "SO-12", [("FG-1", 10)], customer="C-8")
    billed = invoice(0, "SI-12", "SO-12", [("FG-1", 10, 50.0)], customer="C-8")
    shipment(0, "SS-12", "SO-12", [("FG-1", -10, -21.4)])
    credit_memo(0, "CM-12", billed, "C-8", [("Item", "FG-1", 2, 10.0)], by_number=True)
    ledger(0, "Sales_x0020_Credit_x0020_Memo", "CM-12", [("FG-1", 2, 4.28)])
    order(0, "SO-13", [("FG-1", 3)], customer="C-9")
    invoice(0, "SI-13", "SO-13", [("FG-1", 3, 15.0)], customer="C-9")
    shipment(0, "SS-13", "SO-13", [("FG-1", -3, -6.42)])
    for n in range(LOOSE_MEMOS):   # C-9's standalone credits: no invoice stated at all
        credit_memo(0, f"CM-13S{n}", None, "C-9", [("Item", "FG-1", 1, 5.0)])
    for n in range(NUMBERED_MEMOS):   # C-9's credits applied by number to invoices of no order
        billed = invoice(0, f"SI-13X{n}", "", [("FG-2", 1, 10.0)], customer="C-9")
        credit_memo(0, f"CM-13X{n}", billed, "C-9", [("Item", "FG-2", 1, 10.0)],
                    by_number=True)
    invoice(0, "SI-20A", "SO-20", [("FG-1", 2, 20.0)])   # BC deleted SO-20 and SO-21
    invoice(0, "SI-20B", "SO-20", [("FG-2", 1, 12.0)])
    shipment(0, "SS-20", "SO-20", [("FG-1", -2, -8.0), ("FG-2", -1, -5.0)])
    shipment(0, "SS-21", "SO-21", [("FG-1", -10, -21.4)])
    invoice(0, "SI-21", "SO-21", [("FG-1", 4, 20.0)])
    if len(companies) > 1:   # the other company reuses SO-1 and SO-20 with other amounts
        order(1, "SO-1", [("FG-1", 3)])
        invoice(1, "SI-1", "SO-1", [("FG-1", 3, 99.0)])
        shipment(1, "SS-1", "SO-1", [("FG-1", -3, -1.0)])
        invoice(1, "SI-20", "SO-20", [("FG-1", 1, 7.0)])
        shipment(1, "SS-20", "SO-20", [("FG-1", -1, -3.0)])
    return out


__all__ = ["CREDIT_CAVEAT", "FAMILY", "FIGURES", "FIXTURE_SETS", "LOOSE_MEMOS", "PARTITIONS",
           "RecordPort", "UNATTRIBUTED_CAVEAT", "fixture_world", "group_keys", "partition",
           "reading", "violations"]
