"""The inventory suite: a CFO's inventory and costing questions about one item or one
customer (``evals/live_ops/cards.json`` cards marked ``"suite": "inventory"``;
``evals.live_ops_keys`` merges ``FAMILIES``). Taken from a real request (an auditor's three
questions on a contract-manufactured deodorant and its customer), keyed for every item and
customer the records allow.

Ground truth is read from the raw rows: Sage OE invoice lines and headers (what was
shipped and invoiced, at what price and unit cost), FileMaker finished products (the
item's Sage code, ``codeerp``; its bill-of-materials cost, ``sumcost_c``), assembly orders
(realized runs of the item), their components (the packaging issued) and production tasks
(the units filled). Every figure is a cited count or amount, never an estimate:

* **reconciliation** (``item_reconciliation``): packaging issued to the item's runs, units
  filled, units shipped and invoiced since, and what must be on hand now (filled less
  shipped). Keyed only where the item's whole life is in the last year (every realized run
  and every invoice line after its first run) and no line is a $0 release, so nothing
  else can explain the stock;
* **costing** (``item_costing``): the Sage unit cost of the item's latest invoice against
  its FileMaker bill-of-materials cost, and the gap per unit;
* **bill and hold** (``bill_and_hold_item``, ``customer_zero_value_items``): an item a
  customer was billed for in advance and that ships later at $0: billed, released at $0,
  still held for them at zero value;
* **stock held for a customer** (``customer_held_stock``): every item held for the customer
  (reconciled stock and bill-and-hold stock) and the units in all, only where every item
  the customer bought in the last year is one of those.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
import re
from typing import Any, Callable, Mapping, Sequence

from evals.oracle.source import decimal_of, instant, key_text

SAGE_INVOICE = "sage.oe_invoice_header"
SAGE_INVOICE_LINE = "sage.oe_invoice_detail"
FM_FP = "public.finished_product"
FM_PRODUCTION = "public.production"
WINDOW_DAYS = 365
# A count is exact; "0.4" keeps it exact for whole units while a figure binds to its label
# in a sentence naming several (``evals.live_ops``: an exact count reads every figure of
# its clause, so a reconciliation written in one sentence would state none).
COUNT = "0.4"
LABELS = {
    "issued": ["issued", "packaging", "bottles", "containers", "components", "used"],
    "filled": ["filled", "produced", "made", "fill", "filling"],
    "shipped": ["shipped", "invoiced", "sold", "shipments"],
    "on_hand": ["on hand", "left", "remaining", "in stock", "should have", "expected"],
    "sage_cost": ["sage", "sage cost", "sage unit cost", "costed at", "carried at"],
    "bom_cost": ["bom", "bill of materials", "bom cost", "filemaker cost", "fm cost"],
    "gap": ["difference", "gap", "higher", "more", "extra", "above", "loading", "markup"],
    "billed": ["billed", "invoiced", "advance"],
    "released": ["released", "shipped", "pulled"],
    "held": ["holding", "held", "still", "remaining", "left"],
    "units": ["units", "total", "altogether", "in all"],
}


# The words of these figures every other figure is bound against (merged into
# ``evals.live_ops_keys.LABELS``): "1,200 shipped, leaving 3,795 on hand" gives the 3,795
# to "on hand", never to "shipped". Only words no older figure is written beside.
RIVALS = {"inventory_on_hand": ["on hand", "in stock"],
          "inventory_filled": ["filled", "produced"],
          "inventory_issued": ["issued", "bottles", "containers"],
          "inventory_billed": ["billed"], "inventory_released": ["released"],
          "inventory_held": ["held", "holding"],
          "inventory_bom": ["bom", "bill of materials"], "inventory_sage": ["sage"]}


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _sage_day(value: Any) -> date | None:
    text = _text(value)
    if not re.fullmatch(r"\d{8}", text) or text == "00000000":
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _plain(number: Decimal) -> str:
    return lk._more._plain(number)


def sage_lines(world: lk.World) -> dict[str, list[dict[str, Any]]]:
    """Sage item code -> its invoice item lines (linetype 1): the customer, the invoice
    day, the units shipped, the unit price and unit cost, and the line and header cited."""
    if "sage_lines" in world._cache:
        return world._cache["sage_lines"]
    headers = {_text(i["row"].get("invuniq")): i for i in world.rows(SAGE_INVOICE)}
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in world.rows(SAGE_INVOICE_LINE):
        row = item["row"]
        code, head = _text(row.get("item")), headers.get(_text(row.get("invuniq")))
        if _text(row.get("linetype")) != "1" or not code or head is None:
            continue
        day = _sage_day(head["row"].get("invdate"))
        qty = decimal_of(_text(row.get("qtyshipped")))
        if day is None or qty is None:
            continue
        out[code].append({"customer": _text(head["row"].get("customer")), "day": day,
                          "qty": qty, "price": decimal_of(_text(row.get("unitprice"))),
                          "cost": decimal_of(_text(row.get("cosuntcst"))),
                          "invoice": _text(head["row"].get("invnumber")),
                          "cites": [item["citation"], head["citation"]]})
    world._cache["sage_lines"] = out
    return out


def _products(world: lk.World) -> dict[str, Mapping[str, Any]]:
    """Sage code -> the one FileMaker finished product carrying it (``codeerp``)."""
    found: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in world.rows(FM_FP):
        code = _text(item["row"].get("codeerp"))
        if code:
            found[code].append(item)
    return {code: items[0] for code, items in found.items() if len(items) == 1}


def _runs(world: lk.World) -> dict[str, list[dict[str, Any]]]:
    """FileMaker finished product id -> its realized assembly orders, each with the units
    filled (``production`` Filling tasks), the packaging issued (per run, the largest
    issued quantity of a one-per-unit packaging component) and the rows read."""
    if "inventory_runs" in world._cache:
        return world._cache["inventory_runs"]
    filled: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in world.rows(FM_PRODUCTION):
        if _text(item["row"].get("type")).casefold() == "filling":
            filled[key_text(item["row"].get("idmo")) or ""].append(item)
    parts: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in world.rows(lk.FM_AO_ITEM):
        parts[key_text(item["row"].get("idassem")) or ""].append(item)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in world.rows(lk.FM_AO):
        row = item["row"]
        if _text(row.get("status")) != "Realized":
            continue
        ao = key_text(row.get("id")) or ""
        made = instant(row.get("datemanuf"))
        made = made.date() if made is not None else None
        units = [decimal_of(t["row"].get("qty")) for t in filled.get(ao, ())]
        packs = [(decimal_of(p["row"].get("actualamount")), p) for p in parts.get(ao, ())
                 if key_text(p["row"].get("idpack")) and
                 decimal_of(p["row"].get("qtybatch")) == 1]
        packs = [(n, p) for n, p in packs if n is not None]
        out[key_text(row.get("idfinprod")) or ""].append({
            "name": _text(row.get("nameamo_c")), "made": made,
            "filled": None if not units or None in units else sum(units, Decimal(0)),
            "issued": max((n for n, _p in packs), default=None),
            "cites": [item["citation"], *(t["citation"] for t in filled.get(ao, ())),
                      *(p["citation"] for _n, p in packs)]})
    world._cache["inventory_runs"] = out
    return out


def _item_slot(code: str, product: Mapping[str, Any]) -> str:
    name = re.sub(r"\s+", " ", _text(product["row"].get("name")))
    name = name if len(name) <= 40 else name[:40].rsplit(" ", 1)[0]
    return f"{code} ({name})" if name else code


def _count(fact_id: str, value: Decimal, records: Sequence[str], **extra: Any
           ) -> dict[str, Any]:
    return lk._fact("number", fact_id, records, ("api",), value=_plain(value),
                    label=LABELS[extra.pop("label", fact_id)], tolerance=COUNT, **extra)


def _reconciled(world: lk.World) -> dict[str, dict[str, Any]]:
    """Sage code -> the reconciliation of an item whose whole life is in the window (see
    the module doc): issued, filled, shipped, on hand, the records and the customers."""
    if "reconciled" in world._cache:
        return world._cache["reconciled"]
    lines, runs = sage_lines(world), _runs(world)
    out = {}
    for code, product in _products(world).items():
        mine = runs.get(key_text(product["row"].get("id")) or "", [])
        if not mine or any(r["made"] is None or r["filled"] is None or r["issued"] is None
                           for r in mine):
            continue
        first = min(r["made"] for r in mine)
        if (world.as_of - first).days > WINDOW_DAYS:
            continue
        sold = [l for l in lines.get(code, ()) if l["day"] <= world.as_of]
        if any(l["day"] < first or not l["price"] for l in sold):
            continue   # stock from before the first run, or a $0 release: not this rule
        filled = sum((r["filled"] for r in mine), Decimal(0))
        issued = sum((r["issued"] for r in mine), Decimal(0))
        shipped = sum((l["qty"] for l in sold), Decimal(0))
        if filled <= 0 or shipped > filled or filled > issued:
            continue   # more filled than containers issued: the run records disagree
        out[code] = {"product": product, "issued": issued,
                     "filled": filled, "shipped": shipped, "on_hand": filled - shipped,
                     "runs": [c for r in mine for c in r["cites"]] + [product["citation"]],
                     "sold": [c for l in sold for c in l["cites"]],
                     "customers": {l["customer"] for l in sold}}
    world._cache["reconciled"] = out
    return out


def item_reconciliation(world: lk.World) -> list[lk.Candidate]:
    out = []
    for code, entry in _reconciled(world).items():
        runs, sold = entry["runs"], entry["sold"]
        out.append(lk.Candidate(code, "Product", {"item": _item_slot(code, entry["product"])}, [
            _count("issued", entry["issued"], runs), _count("filled", entry["filled"], runs),
            _count("shipped", entry["shipped"], sold or runs),
            _count("on_hand", entry["on_hand"], runs + sold)],
            ["FileMaker finished_product.codeerp = the Sage item; its realized "
             "assembly_order runs: production Filling qty (filled), assembly_order_item "
             "actualamount of one-per-unit packaging (issued); Sage oe_invoice_detail "
             "qtyshipped of the item since the first run (shipped and invoiced)"],
            "on hand = units filled less units shipped and invoiced, for an item whose every "
            f"run and invoice is within {WINDOW_DAYS} days and none a $0 release"))
    return out


def item_costing(world: lk.World) -> list[lk.Candidate]:
    """The Sage unit cost of the item's latest invoice (one cost that day, within the
    window) against its FileMaker bill-of-materials cost, and the gap per unit."""
    out = []
    lines = sage_lines(world)
    for code, product in _products(world).items():
        bom = decimal_of(product["row"].get("sumcost_c"))
        dated = [l for l in lines.get(code, ()) if l["cost"] and l["cost"] > 0
                 and 0 <= (world.as_of - l["day"]).days <= WINDOW_DAYS]
        if not bom or bom <= 0 or not dated:
            continue
        day = max(l["day"] for l in dated)
        last = [l for l in dated if l["day"] == day]
        if len({l["cost"] for l in last}) != 1:
            continue
        cost = last[0]["cost"]
        cites = [c for l in last for c in l["cites"]]
        money = {"tolerance": "0.005", "tolerance_pct": lk.MONEY_PCT, "markings": ["finance"]}
        out.append(lk.Candidate(code, "Product", {"item": _item_slot(code, product)}, [
            lk._fact("number", "sage_cost", cites, ("api",), value=_plain(cost),
                     label=LABELS["sage_cost"], **money),
            lk._fact("number", "bom_cost", [product["citation"]], ("api",),
                     value=_plain(Decimal(str(round(bom, 4)))), label=LABELS["bom_cost"],
                     **money),
            lk._fact("number", "gap", cites + [product["citation"]], ("api",),
                     value=_plain(Decimal(str(round(cost - bom, 4)))), label=LABELS["gap"],
                     tolerance="0.01", tolerance_pct="2", markings=["finance"])],
            ["Sage oe_invoice_detail.cosuntcst of the item's latest invoice in the window; "
             "FileMaker finished_product.sumcost_c (bill-of-materials cost) of the product "
             "whose codeerp is the item"],
            "the Sage unit cost against the FileMaker BOM cost and their difference"))
    return out


def _customer_names(world: lk.World) -> dict[str, str]:
    return {_text(i["row"].get("idcust")): _text(i["row"].get("namecust"))
            for i in world.rows(lk._more.SAGE_CUSTOMER) if _text(i["row"].get("namecust"))}


def _held(world: lk.World) -> dict[tuple[str, str], dict[str, Any]]:
    """(Sage customer, item) -> billed in advance, released at $0, still held: the item
    billed to the customer on one priced invoice (every priced line of the pair is on it)
    and shipped to them at $0 after it, with no priced invoice after the first $0 release
    (the bill-and-hold rule alone explains it). A pair with ordinary priced sales beside
    the advance billing names no one billing and is not keyed."""
    if "held" in world._cache:
        return world._cache["held"]
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for code, lines in sage_lines(world).items():
        for line in lines:
            if line["day"] <= world.as_of and line["qty"] > 0 and line["price"] is not None:
                by_pair[(line["customer"], code)].append(line)
    out = {}
    for pair, lines in by_pair.items():
        zero = [l for l in lines if l["price"] == 0]
        if not zero:
            continue
        first = min(l["day"] for l in zero)
        billed = [l for l in lines if l["price"] > 0 and l["day"] <= first]
        if not billed or any(l["price"] > 0 and l["day"] > first for l in lines) \
                or len({l["invoice"] for l in billed}) != 1:
            continue
        if (world.as_of - first).days > 2 * WINDOW_DAYS:
            continue
        b = sum((l["qty"] for l in billed), Decimal(0))
        r = sum((l["qty"] for l in zero), Decimal(0))
        if r > b:
            continue
        out[pair] = {"billed": b, "released": r, "held": b - r,
                     "billed_cites": [c for l in billed for c in l["cites"]],
                     "released_cites": [c for l in zero for c in l["cites"]]}
    world._cache["held"] = out
    return out


def bill_and_hold_item(world: lk.World) -> list[lk.Candidate]:
    names, products = _customer_names(world), _products(world)
    out = []
    for (customer, code), entry in _held(world).items():
        name = names.get(customer)
        if not name:
            continue
        item = _item_slot(code, products[code]) if code in products else code
        both = entry["billed_cites"] + entry["released_cites"]
        out.append(lk.Candidate(f"{customer}/{code}", "Product",
                                {"customer": name, "item": item}, [
            _count("billed", entry["billed"], entry["billed_cites"]),
            _count("released", entry["released"], entry["released_cites"] or both),
            _count("held", entry["held"], both)],
            ["Sage oe_invoice_detail of the customer and item: priced lines on or before the "
             "first $0 line (billed in advance), the $0 lines (released)"],
            "held at zero value = billed in advance less released at $0"))
    return out


def customer_zero_value_items(world: lk.World) -> list[lk.Candidate]:
    names = _customer_names(world)
    by_customer: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for (customer, code), entry in _held(world).items():
        if entry["held"] > 0:
            by_customer[customer].append((code, entry))
    return [lk.Candidate(customer, "Customer", {"customer": names[customer]}, [
        lk._fact("set", "zero_value_items",
                 [c for _code, e in items for c in e["billed_cites"] + e["released_cites"]],
                 ("api",), members=[[code] for code, _e in sorted(items)])],
        ["Sage oe_invoice_detail: the customer's items billed in advance and released at $0, "
         "units still held"], "the customer's items held at zero value (bill and hold)")
        for customer, items in by_customer.items()
        if customer in names and 1 <= len(items) <= lk.LIST_MAX]


def customer_held_stock(world: lk.World) -> list[lk.Candidate]:
    """Everything held for a customer: its reconciled items' stock on hand and its
    bill-and-hold items' held units, when every item it bought in the window is one of
    those (else the total would be a part named as the whole)."""
    names, reconciled, held = _customer_names(world), _reconciled(world), _held(world)
    bought: dict[str, set[str]] = defaultdict(set)
    for code, lines in sage_lines(world).items():
        for line in lines:
            if 0 <= (world.as_of - line["day"]).days <= WINDOW_DAYS:
                bought[line["customer"]].add(code)
    out = []
    for customer, codes in bought.items():
        stock: dict[str, tuple[Decimal, list[str]]] = {}
        for code in codes:
            entry = reconciled.get(code)
            if entry is not None and entry["customers"] == {customer}:
                stock[code] = (entry["on_hand"], entry["runs"] + entry["sold"])
            elif (customer, code) in held:
                h = held[(customer, code)]
                stock[code] = (h["held"], h["billed_cites"] + h["released_cites"])
            else:
                break
        else:
            kept = {c: v for c, v in stock.items() if v[0] > 0}
            if customer not in names or not 1 <= len(kept) <= lk.LIST_MAX:
                continue
            records = [r for _n, cites in kept.values() for r in cites]
            out.append(lk.Candidate(customer, "Customer", {"customer": names[customer]}, [
                lk._fact("set", "held_items", records, ("api",),
                         members=[[c] for c in sorted(kept)]),
                _count("units", sum((n for n, _c in kept.values()), Decimal(0)), records,
                       label="units")],
                ["item_reconciliation and bill_and_hold_item over every item the customer "
                 f"was invoiced for in the last {WINDOW_DAYS} days"],
                "the customer's stock we hold: reconciled stock on hand plus bill-and-hold "
                "units, every item it bought accounted for"))
    return out


FAMILIES: dict[str, Callable[[lk.World], list[lk.Candidate]]] = {
    "item_reconciliation": item_reconciliation, "item_costing": item_costing,
    "bill_and_hold_item": bill_and_hold_item,
    "customer_zero_value_items": customer_zero_value_items,
    "customer_held_stock": customer_held_stock,
}
PORTAL_RELATIONS = (SAGE_INVOICE, SAGE_INVOICE_LINE, FM_FP, FM_PRODUCTION)
NARROW = {
    SAGE_INVOICE: ("invuniq", "invnumber", "invdate", "customer"),
    SAGE_INVOICE_LINE: ("invuniq", "linetype", "item", "qtyshipped", "unitprice",
                        "cosuntcst"),
    FM_FP: ("id", "codeerp", "name", "sumcost_c"),
    FM_PRODUCTION: ("id", "idmo", "type", "qty"),
}

from evals import live_ops_keys as lk  # noqa: E402 - merged into its FAMILIES when it loads
