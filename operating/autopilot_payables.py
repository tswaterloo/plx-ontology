"""Payables entries a person keys in the accounting system, prepared from the records
(dry run: nothing is written to Sage, BC or any system, nothing sent or approved; an item
carries no ``write`` spec, so no write desk ever sees it):

* ``ap_invoice_entry`` (a supplier invoice received): two things.

  1. The MATCH: the purchase order the invoice names (its PO number, the supplier on both
     sides), the PO's lines and receipts, and per invoice line the 3-way variances: the
     line's quantity against the PO line's ordered quantity and against what was received
     by the invoice date, what the supplier billed on that PO line before (already
     billed), and its price against the PO line's price where the PO states one.
  2. The CODED ENTRY the AP person keys: vendor, invoice number, invoice date, due date
     (the supplier's terms as its latest earlier invoice's due less invoice date), the
     currency, the tax as the invoice states it (the vendor's tax group as on its latest
     invoice), and the distribution lines, each on the G/L account the supplier's earlier
     invoices were coded to (``coding``), translated to the shared-chart BC account through
     the Sage -> BC G/L crosswalk (``operating.gl_crosswalk``) when the crosswalk states it
     for the account in the invoice's currency.

What the invoice states (the input): the vendor, its number, date, PO number, currency,
document total and tax, and its item lines (item, quantity, unit, amount). Everything the
person decides (the G/L coding, how a non-item remainder is distributed, the due date)
comes from the records entered before it, never from the entry being prepared.

Coding (no entry beats a wrong one; a line not told exactly leaves no entry):

* an item line: the account of this vendor's latest ``ITEM_HISTORY`` earlier lines for the
  same item, when all are on one account;
* what the item lines leave of the total before tax: one line on the account of this
  vendor's latest ``VENDOR_HISTORY`` earlier invoices in this currency, when each coded
  everything that was not an item line to one account and all to the same one.

An invoice whose number one of this vendor's earlier entries or open documents already
carries (``proactive_cash.similar_numbers``: the same document keyed twice) is flagged as
a duplicate and gets no entry. A credit or debit note, an invoice with tax included in its
amounts, or one whose lines exceed its total, gets none either (``skip``).

Money is ``Decimal`` with its currency; the item is finance-marked through the records it
reads (Sage AP rows carry ``finance``; a payroll account's rows ``finance`` and ``pii``),
so only a reader holding those sees it. No rule names a supplier, an item or a document.
Sources: Sage AP invoice entries (``RelSageApIbh`` header, ``RelSageApIbd`` lines), Sage
AP open documents, Sage and Portal purchase orders and receipts. ``isolated-fixture`` in
tests; scored against what people entered by ``operating.payables_score``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from . import autopilot as ap
from . import autopilot_records

CENT = Decimal("0.01")
HANDLER = "ap_invoice_entry"
HEAD_TYPE, HEAD_BINDING = "RelSageApIbh", "gen.sage.ap_ibh"
LINE_TYPE, LINE_BINDING = "RelSageApIbd", "gen.sage.ap_ibd"
DOC_TYPE, DOC_BINDING = "RelSageApOpenDocument", "gen.sage.ap_open_document"
HEAD = ("idvend", "idinvc", "dateinvc", "datedue", "codecurn", "ponbr", "amtinvctot",
        "amttaxtot", "texttrx", "cntbtch", "cntitem", "enteredby", "codetaxgrp",
        "swtaxincl1", "swtaxincl2", "swtaxincl3", "swtaxincl4", "swtaxincl5")
LINE = ("cntbtch", "cntitem", "cntline", "idglacct", "amtdist", "iditem", "qtyinvc",
        "unitmeas", "textdesc", "swtaxincl1", "swtaxincl2", "swtaxincl3", "swtaxincl4",
        "swtaxincl5")
DOC = ("idvend", "idinvc", "cntbtch", "cntitem", "txttrxtype", "dateinvc")
PO_HEAD = ("sage_po_number", "po_number", "currency", "order_date")
PO_LINE = ("ordered_quantity", "received_quantity", "unit_price", "item_code", "description")
RECEIPT_HEAD = ("received_date", "sage_receipt_number", "receipt_number")
INVOICE_KIND = "1"                 # Sage AP texttrx: 1 invoice (2 debit note, 3 credit note)
DOC_INVOICE_KINDS = frozenset({"", "1"})   # an open document of these kinds is an invoice
ITEM_HISTORY = 3                   # earlier lines of the vendor and item read for its account
VENDOR_HISTORY = 3                 # earlier invoices of the vendor read for its account
CROSSWALK_ENV = "PLX_GL_CROSSWALK"
CROSSWALK_DEFAULT = "/data/plx-state/reference/gl-crosswalk.json"


def _money(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _qty(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _norm(raw: Any) -> str:
    """A code or number compared once punctuation, case and leading zeros go."""
    text = re.sub(r"[^0-9A-Z]", "", str(raw or "").upper())
    return text.lstrip("0") or text


def _day(raw: Any) -> date | None:
    from .autopilot_receivables import _day as day
    return day(str(raw or ""))


def _spec(registry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next((a for a in registry["autopilots"] if a.get("on") == "record.due"
                 and a.get("handler") == HANDLER and a.get("enabled") is not False), None)


# ----------------------------------------------------------------------------- the records

@dataclass
class Entry:
    """One Sage AP invoice entry (header and lines) as read."""
    oid: str
    key: tuple[int, int]                  # (batch, entry): the order entries were keyed in
    head: dict[str, Any]                  # property -> its row (value, l1_handle)
    lines: list[dict[str, Any]] = field(default_factory=list)   # line oid, row, ...

    def v(self, prop: str) -> str:
        return autopilot_records._v(self.head, prop).strip()


def _first(props: Mapping[str, list]) -> dict[str, Any]:
    return {p: rows[0] for p, rows in props.items() if rows}


def _int(raw: str) -> int | None:
    try:
        return int(Decimal(raw))
    except (ArithmeticError, ValueError):
        return None


def _entries(st: Any) -> dict[str, Any]:
    """Every Sage AP invoice entry: header with its lines, by vendor; read once a run."""
    from .proactive_cash import read_records

    def load() -> dict[str, Any]:
        heads = read_records(st, HEAD_TYPE, HEAD_BINDING, HEAD)
        lines = read_records(st, LINE_TYPE, LINE_BINDING, LINE)
        by_key: dict[tuple[int, int], Entry] = {}
        twice: set[tuple[int, int]] = set()
        for oid, props in heads.items():
            head = _first(props)
            b, i = _int(autopilot_records._v(head, "cntbtch")), _int(autopilot_records._v(head, "cntitem"))
            if b is None or i is None:
                continue
            if (b, i) in by_key:
                twice.add((b, i))           # one entry key on two records: neither is told
            by_key[(b, i)] = Entry(oid, (b, i), head)
        for k in twice:
            by_key.pop(k, None)
        for oid, props in lines.items():
            row = _first(props)
            b, i = _int(autopilot_records._v(row, "cntbtch")), _int(autopilot_records._v(row, "cntitem"))
            entry = by_key.get((b, i)) if b is not None and i is not None else None
            if entry is not None:
                entry.lines.append({"oid": oid, "row": row,
                                    "n": _int(autopilot_records._v(row, "cntline")) or 0})
        by_vendor: dict[str, list[Entry]] = defaultdict(list)
        for entry in by_key.values():
            entry.lines.sort(key=lambda ln: (ln["n"], ln["oid"]))
            if entry.v("idvend"):
                by_vendor[_norm(entry.v("idvend"))].append(entry)
        for found in by_vendor.values():
            found.sort(key=lambda e: e.key)
        return {"by_key": by_key, "by_vendor": dict(by_vendor), "by_oid":
                {e.oid: e for e in by_key.values()}}
    memo = getattr(st, "memo", None)
    return memo("payables:entries", load) if memo else load()


def _open_docs(st: Any) -> dict[str, list[dict[str, Any]]]:
    """The Sage AP open documents by vendor (posted invoices: the duplicate check)."""
    from .proactive_cash import read_records

    def load() -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for oid, props in read_records(st, DOC_TYPE, DOC_BINDING, DOC).items():
            row = _first(props)
            vendor = _norm(autopilot_records._v(row, "idvend"))
            if vendor:
                out[vendor].append({"oid": oid, "row": row})
        return dict(out)
    memo = getattr(st, "memo", None)
    return memo("payables:open_docs", load) if memo else load()


def crosswalk(path: str | os.PathLike | None = None) -> dict[str, Any] | None:
    """The Sage -> BC G/L crosswalk file (``operating.gl_crosswalk``), by the Sage
    formatted account (normalized) -> its mappings: None when no file is loaded."""
    from . import gl_crosswalk as gx
    where = Path(path or os.environ.get(CROSSWALK_ENV) or CROSSWALK_DEFAULT)
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping) or data.get("format") != gx.FORMAT:
        return None
    return gx.by_sage_account(data)


# ----------------------------------------------------------------------------- the invoice

@dataclass
class Received:
    """A supplier invoice as it states itself (what the person keys from)."""
    oid: str                       # the record it was read from
    vendor: str
    number: str
    day: date
    currency: str
    po: str
    total: Decimal                 # the document total, tax included
    tax: Decimal
    items: list[dict[str, Any]]    # item, quantity, unit, amount, cite
    before: tuple[int, int] | None  # entries keyed before it are the history
    cites: list[dict[str, Any]]
    tax_included: bool = False
    kind: str = INVOICE_KIND


def received_from_entry(entry: Entry) -> Received | dict[str, str]:
    """The invoice a Sage AP entry was keyed from: its header facts and item lines (item,
    quantity, unit, amount as the supplier lists them); never its G/L coding."""
    total, tax = autopilot_records._decimal(entry.v("amtinvctot")), autopilot_records._decimal(entry.v("amttaxtot"))
    day = _day(entry.v("dateinvc"))
    if not entry.v("idvend") or not entry.v("idinvc") or day is None:
        return {"skip": "invoice_unread"}
    if total is None or tax is None or not entry.v("codecurn"):
        return {"skip": "amounts_unread"}
    flags = [entry.v(f"swtaxincl{n}") for n in range(1, 6)]
    items, included = [], any(f == "1" for f in flags)
    for ln in entry.lines:
        r = ln["row"]
        if any(autopilot_records._v(r, f"swtaxincl{n}").strip() == "1" for n in range(1, 6)):
            included = True
        item = autopilot_records._v(r, "iditem").strip()
        if not item:
            continue
        items.append({"item": item, "quantity": autopilot_records._decimal(autopilot_records._v(r, "qtyinvc")),
                      "unit": autopilot_records._v(r, "unitmeas").strip(),
                      "amount": autopilot_records._decimal(autopilot_records._v(r, "amtdist")), "row": r,
                      "oid": ln["oid"]})
    return Received(entry.oid, entry.v("idvend"), entry.v("idinvc"), day,
                    entry.v("codecurn").upper(), entry.v("ponbr"), total, tax, items,
                    entry.key, [entry.head], included, entry.v("texttrx"))


# ----------------------------------------------------------------------------- history

def _history(index: Mapping[str, Any], inv: Received) -> list[Entry]:
    """The vendor's invoices entered before this one in its currency, latest first."""
    found = index["by_vendor"].get(_norm(inv.vendor), [])
    return [e for e in reversed(found) if e.oid != inv.oid
            and (inv.before is None or e.key < inv.before)
            and e.v("texttrx") == INVOICE_KIND and e.v("codecurn").upper() == inv.currency]


def item_account(history: Sequence[Entry], item: str) -> tuple[str | None, list]:
    """The account this vendor's latest ``ITEM_HISTORY`` earlier lines of ``item`` were all
    coded to; None when there are none or they disagree."""
    lines = [ln for e in history for ln in e.lines
             if _norm(autopilot_records._v(ln["row"], "iditem")) == _norm(item)][:ITEM_HISTORY]
    accounts = {autopilot_records._v(ln["row"], "idglacct").strip() for ln in lines}
    if not lines or len(accounts) != 1 or "" in accounts:
        return None, lines
    return next(iter(accounts)), lines


def vendor_account(history: Sequence[Entry]) -> tuple[str | None, str, list[Entry]]:
    """The account the vendor's latest ``VENDOR_HISTORY`` earlier invoices coded all their
    non-item lines to (each to one account, all to the same); (None, why, read)."""
    read = [e for e in history if any(not autopilot_records._v(ln["row"], "iditem").strip()
                                      for ln in e.lines)][:VENDOR_HISTORY]
    if len(read) < VENDOR_HISTORY:
        return None, "history_short", read
    accounts = {autopilot_records._v(ln["row"], "idglacct").strip() for e in read for ln in e.lines
                if not autopilot_records._v(ln["row"], "iditem").strip()}
    if len(accounts) != 1 or "" in accounts:
        return None, "coding_varies", read
    return next(iter(accounts)), "", read


def terms(history: Sequence[Entry]) -> tuple[int | None, Entry | None]:
    """The vendor's terms in days: its latest earlier invoice's due less invoice date."""
    for e in history:
        issued, due = _day(e.v("dateinvc")), _day(e.v("datedue"))
        if issued and due and due >= issued:
            return (due - issued).days, e
    return None, None


def duplicates(st: Any, index: Mapping[str, Any], inv: Received) -> list[dict[str, Any]]:
    """This vendor's earlier entries and open documents carrying the invoice's number (the
    same document keyed twice), the invoice's own posting excluded."""
    from .proactive_cash import similar_numbers
    out = []
    for e in index["by_vendor"].get(_norm(inv.vendor), []):
        if e.oid == inv.oid or (inv.before is not None and e.key >= inv.before):
            continue
        if e.v("texttrx") == INVOICE_KIND and similar_numbers(e.v("idinvc"), inv.number):
            out.append({"oid": e.oid, "row": e.head, "kind": "entry"})
    for d in _open_docs(st).get(_norm(inv.vendor), []):
        r = d["row"]
        key = (_int(autopilot_records._v(r, "cntbtch")), _int(autopilot_records._v(r, "cntitem")))
        if None in key:
            continue
        if inv.before is not None and key >= inv.before:
            continue                  # this invoice's own posting, or a later one
        if autopilot_records._v(r, "txttrxtype").strip() not in DOC_INVOICE_KINDS:
            continue
        flagged = {index["by_oid"][o["oid"]].key for o in out if o["kind"] == "entry"}
        if similar_numbers(autopilot_records._v(r, "idinvc"), inv.number) and key not in flagged:
            out.append({"oid": d["oid"], "row": r, "kind": "open_document"})
    return out


# ----------------------------------------------------------------------------- the match

def _one_supplier(ctx: Any, oids: Sequence[str], link: str) -> set[str]:
    return {o for o in ctx.linked_many(oids, (link,)) if o.startswith("Supplier:")}


def _binding(oid: str) -> str:
    parts = oid.split(":", 2)
    return parts[1] if len(parts) > 2 else ""


def _quantity(row: Mapping[str, Any] | None, prop: str) -> tuple[Decimal | None, str]:
    cell = (row or {}).get(prop) or {}
    return autopilot_records._decimal(cell.get("value")), str(cell.get("unit") or "").strip().lower()


def match(ctx: Any, index: Mapping[str, Any], inv: Received,
          history: Sequence[Entry]) -> dict[str, Any]:
    """The purchase order the invoice bills and its 3-way variances (module doc)."""
    if not inv.po:
        return {"status": "no_po", "lines": [], "variances": []}
    found = sorted({*ctx.find("PurchaseOrder", "sage_po_number", inv.po),
                    *ctx.find("PurchaseOrder", "po_number", inv.po)})
    by_binding: dict[str, list[str]] = defaultdict(list)
    for oid in found:
        by_binding[_binding(oid)].append(oid)
    if not found:
        return {"status": "po_not_found", "po": inv.po, "lines": [], "variances": []}
    if any(len(v) > 1 for v in by_binding.values()):
        return {"status": "po_ambiguous", "po": inv.po, "lines": [], "variances": []}
    heads = ctx.read(found, PO_HEAD)
    for oid in found:
        ctx.cite(heads.get(oid), "sage_po_number", "po_number")
    # the supplier on both sides: the PO's and the Sage vendor's Portal supplier
    from .autopilot_handlers import FindUnavailable
    try:
        vendors = ctx.find("RelSageApVendor", "vendorid", inv.vendor)
    except FindUnavailable:
        vendors = []        # the Sage vendor not looked up: the supplier stays "not_told"
    ctx.objects.update(vendors)
    ours = _one_supplier(ctx, vendors, "RelSageApVendor.portal_supplier") if vendors else set()
    theirs = _one_supplier(ctx, found, "PurchaseOrder.ordered_from")
    variances: list[str] = []
    if ours and theirs and not ours & theirs:
        return {"status": "other_supplier", "po": inv.po, "lines": [],
                "variances": ["the PO is ordered from another supplier"]}
    supplier = "agrees" if ours & theirs else "not_told"
    currencies = {autopilot_records._v(heads.get(o), "currency").strip().upper() for o in found} - {""}
    if currencies and currencies != {inv.currency}:
        variances.append(f"PO currency {'/'.join(sorted(currencies))}, invoice {inv.currency}")
    po_lines = {o: r for o, r in ctx.read([o for o in ctx.linked_many(
        found, ("PurchaseOrderLine.line_of",)) if o.startswith("PurchaseOrderLine:")],
        PO_LINE).items()}
    receipts = [o for o in ctx.linked_many(found, ("Receipt.receives",))
                if o.startswith("Receipt:")]
    rheads = ctx.read(receipts, RECEIPT_HEAD)
    dated = {o for o in receipts
             if (d := _day(autopilot_records._v(rheads.get(o), "received_date"))) and d <= inv.day}
    rlines = [o for o in ctx.linked_many(sorted(dated), ("ReceiptLine.line_of",))
              if o.startswith("ReceiptLine:")]
    rrows = ctx.read(rlines, ("received_quantity",))
    products: dict[str, set[str]] = defaultdict(set)
    for _t, a, b in ctx.touching(list(po_lines) + rlines,
                                 ("PurchaseOrderLine.for_item", "ReceiptLine.for_item")):
        products[a].add(b)
    one_product = {k: next(iter(v)) for k, v in products.items() if len(v) == 1}
    received_of: dict[str, tuple[Decimal, str]] = {}
    for o in rlines:
        q, unit = _quantity(rrows.get(o), "received_quantity")
        prod = one_product.get(o)
        if q is None or prod is None:
            continue
        have = received_of.get(prod)
        if have is not None and have[1] != unit:
            received_of[prod] = (Decimal(-1), "mixed")
        else:
            received_of[prod] = ((have[0] if have else Decimal(0)) + q, unit)
    # already billed: this vendor's earlier invoices on the same PO, by item
    billed: Counter = Counter()
    for e in history:
        if _norm(e.v("ponbr")) != _norm(inv.po):
            continue
        for ln in e.lines:
            q = autopilot_records._decimal(autopilot_records._v(ln["row"], "qtyinvc"))
            item = _norm(autopilot_records._v(ln["row"], "iditem"))
            if item and q is not None:
                billed[item] += q
    lines = []
    for it in inv.items:
        code = _norm(it["item"])
        same = [o for o, r in po_lines.items() if _norm(autopilot_records._v(r, "item_code")) == code]
        out = {"item": it["item"], "quantity": _qty(it["quantity"])
               if it["quantity"] is not None else None, "unit": it["unit"] or None}
        if len(same) != 1:
            out["variance"] = ["no PO line of this item" if not same
                               else "several PO lines of this item"]
            variances.append(f"{it['item']}: {out['variance'][0]}")
            lines.append(out)
            continue
        pl = same[0]
        row = po_lines[pl]
        ctx.cite(row, "ordered_quantity", "item_code")
        ordered, unit = _quantity(row, "ordered_quantity")
        before = billed.get(code, Decimal(0))
        got = received_of.get(one_product.get(pl, ""))
        # received by the invoice date only: the PO line's own received total is today's,
        # never the day's (a replay reading it would see receipts made after the invoice)
        received, runit, basis = (got[0], got[1], "receipts dated by the invoice date") \
            if got and got[1] != "mixed" else (None, "", "no receipt line of this item "
                                               "dated by the invoice date ties to it")
        out.update({"po_line": pl, "ordered": _qty(ordered) if ordered is not None else None,
                    "received": _qty(received) if received is not None else None,
                    "received_basis": basis, "billed_before": _qty(before)})
        flags = []
        q = it["quantity"]
        if q is None:
            flags.append("quantity not on the invoice line")
        elif it["unit"] and unit and it["unit"].lower() != unit:
            flags.append(f"invoice unit {it['unit']}, PO unit {unit}: quantities not compared")
        else:
            if ordered is None:
                flags.append("PO line quantity unread")
            elif before >= ordered:
                flags.append("already billed in full")
            elif before + q > ordered:
                flags.append(f"bills {_qty(before + q - ordered)} more than ordered")
            if received is None or (runit and unit and runit != unit):
                flags.append("received quantity not told")
            elif before + q > received:
                flags.append(f"{_qty(before + q - received)} not yet received")
        price = autopilot_records._decimal(autopilot_records._v(row, "unit_price"))
        if price is not None and q and it["amount"] is not None:
            ctx.cite(row, "unit_price")
            expected = (q * price).quantize(CENT, rounding=ROUND_HALF_UP)
            out["po_unit_price"] = format(price.normalize(), "f")
            if expected != it["amount"].quantize(CENT, rounding=ROUND_HALF_UP):
                flags.append(f"price: {_money(it['amount'])} billed, {_money(expected)} at "
                             "the PO price")
        elif price is None:
            out["po_unit_price"] = None
        out["variance"] = flags
        variances.extend(f"{it['item']}: {f}" for f in flags)
        lines.append(out)
    return {"status": "variance" if variances else "matched", "po": inv.po,
            "purchase_orders": found, "supplier": supplier, "receipts": sorted(dated),
            "lines": lines, "variances": variances}


# ----------------------------------------------------------------------------- the entry

def keyer(index: Mapping[str, Any], history: Sequence[Entry], people: Any) -> str | None:
    """Who keys this vendor's invoices: the one cleared person whose accounting user code
    entered its latest earlier invoice (``day_coverage.user_code_person``); None when none
    does or the people exclusion is unavailable (never a whole role for one record)."""
    from .day_coverage import user_code_person
    if people is None or getattr(people, "unavailable", None):
        return None
    directory, _addresses, domains = ap._people_maps(people)
    for e in history[:5]:
        oid = user_code_person(e.v("enteredby"), directory, domains)
        if oid and people.identity_clear(oid):
            return oid
    return None


def _bc(xwalk: Mapping[str, Any] | None, account: str, currency: str
        ) -> tuple[dict[str, Any] | None, str | None]:
    if xwalk is None:
        return None, "the Sage -> BC G/L crosswalk is not loaded"
    found = [m for m in xwalk.get(_norm(account), ()) if m["currency"] == currency]
    if len(found) == 1:
        m = found[0]
        if not m.get("bc_acctid"):
            return None, f"account {account} is to be added to the shared chart"
        return {"account": m["bc_acctid"], "name": m.get("bc_name"),
                "crosswalk": m.get("evidence")}, None
    if not found and xwalk.get(_norm(account)):
        return None, f"account {account} is mapped in another currency only"
    return None, f"account {account} is not in the crosswalk" if not found \
        else f"account {account} is mapped twice in {currency}"


def prepare(ctx: Any, inv: Received, *, people: Any = None,
            xwalk: Mapping[str, Any] | None | bool = True) -> dict[str, Any]:
    """The match and coded entry for one received invoice, or ``{"skip": reason}``."""
    if inv.kind != INVOICE_KIND:
        return {"skip": "not_an_invoice"}
    if inv.tax_included:
        return {"skip": "tax_included"}
    index = _entries(ctx.st)
    history = _history(index, inv)
    for row in inv.cites:
        ctx.cite(row, "idvend", "idinvc", "dateinvc", "codecurn", "ponbr", "amtinvctot",
                 "amttaxtot")
    ctx.objects.add(inv.oid)
    dupes = duplicates(ctx.st, index, inv)
    found = match(ctx, index, inv, history)
    head = {"vendor": inv.vendor, "invoice_number": inv.number,
            "invoice_date": inv.day.isoformat(), "currency": inv.currency,
            "po": inv.po or None}
    links = [{"label": "Open the invoice", "href": f"/object/{inv.oid}"}] + \
        [{"label": "Open the purchase order", "href": f"/object/{o}"}
         for o in found.get("purchase_orders", [])[:2]]
    if dupes:
        for d in dupes:
            ctx.objects.add(d["oid"])
            ctx.cite(d["row"], "idinvc")
        return {"summary": f"Invoice {inv.number} looks already entered "
                           f"({len(dupes)} earlier record(s) carry its number): not entered.",
                "object": {"object_id": inv.oid, "number": inv.number,
                           "date": inv.day.isoformat()},
                "duplicate_of": [d["oid"] for d in dupes], "entry": None,
                "invoice": head, "match": found, "gaps": [], "dry_run": True,
                "links": links + [{"label": "Open the earlier record", "href": f"/object/{d['oid']}"}
                                  for d in dupes[:2]]}
    net = inv.total - inv.tax
    if any(it["amount"] is None for it in inv.items):
        return {"skip": "line_amount_unread"}
    if sum((it["amount"] for it in inv.items), Decimal(0)) > net:
        return {"skip": "lines_exceed_total"}
    lines, read = [], []
    item_sum = Decimal(0)
    for it in inv.items:
        if it["amount"] is None:
            return {"skip": "line_amount_unread"}
        account, seen = item_account(history, it["item"])
        if account is None:
            return {"skip": "item_account_unknown"}
        read.extend(seen)
        ctx.objects.add(it["oid"])          # its audience and markings carry onto the item
        ctx.cite(it["row"], "iditem", "qtyinvc", "amtdist")
        item_sum += it["amount"]
        lines.append({"sage_account": account, "amount": _money(it["amount"]),
                      "item": it["item"], "quantity": _qty(it["quantity"])
                      if it["quantity"] is not None else None, "basis": "invoice_line"})
    rest = net - item_sum
    if rest < 0:
        return {"skip": "lines_exceed_total"}
    if rest > 0 or not lines:
        account, why, entries = vendor_account(history)
        if account is None:
            return {"skip": why}
        for e in entries:
            ctx.objects.add(e.oid)
            for ln in e.lines:
                if not autopilot_records._v(ln["row"], "iditem").strip():
                    read.append(ln)
        lines.append({"sage_account": account, "amount": _money(rest), "item": None,
                      "quantity": None, "basis": "vendor_account"})
    for ln in read:
        ctx.objects.add(ln["oid"])
        ctx.cite(ln["row"], "idglacct")
    table = crosswalk() if xwalk is True else (xwalk or None)
    gaps = []
    for ln in lines:
        bc, gap = _bc(table, ln["sage_account"], inv.currency)
        ln["bc_account"] = bc
        if gap:
            gaps.append(gap)
    days, latest = terms(history)
    if latest is not None:
        ctx.objects.add(latest.oid)
        ctx.cite(latest.head, "dateinvc", "datedue", "codetaxgrp")
    else:
        gaps.append("terms not found on an earlier invoice")
    taxgroup = latest.v("codetaxgrp") if latest is not None else ""
    entry = {"kind": "ap_invoice", **head,
             "due_date": (inv.day + timedelta(days=days)).isoformat()
             if days is not None else None, "terms_days": days,
             "lines": lines, "subtotal": _money(net), "tax": _money(inv.tax),
             "tax_group": taxgroup or None, "total": _money(inv.total),
             "on_hold": found["status"] not in ("matched", "no_po"),
             "hold_reasons": found["variances"] if found["status"] == "variance"
             else ([found["status"]] if found["status"] not in ("matched", "no_po") else [])}
    if sum(Decimal(ln["amount"]) for ln in lines) != net.quantize(CENT, rounding=ROUND_HALF_UP):
        return {"skip": "lines_do_not_sum"}     # never an entry that does not balance
    return {"summary": f"Invoice {inv.number} ({len(lines)} line(s), {_money(net)} "
                       f"{inv.currency} before tax) is ready to enter"
                       + (f"; PO {inv.po}: {found['status'].replace('_', ' ')}"
                          if inv.po else "; no PO on the invoice") + ".",
            "object": {"object_id": inv.oid, "number": inv.number,
                       "date": inv.day.isoformat()},
            "entry": entry, "invoice": head, "match": found, "gaps": gaps,
            "dry_run": True, "owner_hint": keyer(index, history, people), "links": links}


# ----------------------------------------------------------------------------- the workflow

def entry_events(st: Any, registry: Mapping[str, Any], as_of: date,
                 people: Any = None, *, force: bool = False) -> list[dict[str, Any]]:
    """``record.due`` events: Sage AP invoice entries whose invoice is dated in the last
    ``window_days`` (the replay's invoices received; ``force``: the scorer, with the
    workflow off)."""
    spec = _spec(registry)
    if spec is None and not force:
        return []
    window = int(((spec or {}).get("params") or {}).get("window_days", 1))
    lo = as_of - timedelta(days=window - 1)
    index = _entries(st)
    out = []
    for entry in index["by_key"].values():
        d = _day(entry.v("dateinvc"))
        if d is None or not (lo <= d <= as_of) or entry.v("texttrx") != INVOICE_KIND:
            continue
        audience = autopilot_records._types(st, [entry.oid]).get(entry.oid, ("", ""))[1]
        handles = [c["l1_handle"] for c in entry.head.values()
                   if isinstance(c, Mapping) and c.get("l1_handle")]
        history = [e for e in reversed(index["by_vendor"].get(_norm(entry.v("idvend")), []))
                   if e.key < entry.key]
        out.append({"event_id": ap._digest("record.due", HANDLER, entry.oid),
                    "event_type": "record.due", "object_id": entry.oid,
                    "object_type": HEAD_TYPE, "at": d.isoformat(), "status": HANDLER,
                    "audience": audience, "handles": list(dict.fromkeys(handles))[:3],
                    "fields": {"invoice_number": entry.v("idinvc"),
                               "invoice_date": d.isoformat()},
                    "owner_hint": keyer(index, history, people)})
    return sorted(out, key=lambda e: (e["at"], e["object_id"]))


def h_ap_invoice_entry(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    entry = _entries(ctx.st)["by_oid"].get(event["object_id"])
    if entry is None:
        return {"skip": "invoice_unread"}
    inv = received_from_entry(entry)
    if isinstance(inv, dict):
        return inv
    return prepare(ctx, inv, people=getattr(ctx.st, "autopilot_people", None))


def due_events(st: Any, registry: Mapping[str, Any], as_of: date, people: Any
               ) -> list[dict[str, Any]]:
    return entry_events(st, registry, as_of, people)
