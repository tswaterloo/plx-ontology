"""Sage order margin: invoiced revenue against the cost Sage booked, per item of an order.

The engine of the ``plx.investigate`` plan ``ontology/plans/sage_order_margin.json`` (family
F31). It computes nothing the plan did not reach and cite: every input is a projected Sage
O/E value re-read for the investigating actor (``resolver``) before it is used, as the BC
engine (``operating.order_margin``) does.

* **Revenue** of an invoice line: ``extended_amount`` (extinvmisc, excluding tax) less its
  ``invoice_discount_share`` (invdisc), in the invoice currency, times the invoice's own
  posted ``exchange_rate`` (inrate) into the home currency; 1 is required when the two
  currencies are the same. Rounded to the cent per line, the rate stated in the steps.
* **Cost** of an invoice line: ``invoiced_quantity x unit_conversion / cost_unit_conversion x
  unit_cost`` in the home currency, the amount Sage posted to cost of sales for it. A zero
  unit cost or conversion on a quantity is no cost value (``SAGE_COST_NOT_VALUED``), never a
  zero cost.
* **Per item** of the order (its item lines by Sage item code): revenue, cost and margin over
  every invoice line of that item; margin % on price.
* **Whole.** A total only when the order is a type-1 sales order with nothing outstanding
  (Sage's outstanding quantity includes backorders), the line set and invoices are read
  whole (a complete run whose scope covers the links), every invoice's lines are all read
  (``line_count``), its lines' discount shares sum to its ``invoice_discount``, per item the
  invoiced quantity equals the shipped-to-date quantity in the same unit (a line of another
  order on the invoice breaks it), and every item line is computed. Otherwise typed gaps
  and no figure. An order whose lines or an invoice are not read whole states no item
  figure either. The order's revenue and cost are the sums of its item figures and cite the
  summed invoices, not the item entries, and each item's margin names its two figures in
  its steps: no entry repeats per item on every answer page, so an order of up to the
  plan's 50 lines is answered over as many pages as it takes and its total stands on the
  first page (``operating.customer_profit`` reads it there).
* **Not goods.** Miscellaneous charge lines (line type 2) are left out of the item margin
  and counted (``SAGE_CHARGES_NOT_IN_MARGIN``), as BC leaves charge lines out.
* **Credit notes.** Sage serves no O/E credit note: every figure is before any credit note
  (``SAGE_CREDIT_NOTES_NOT_SERVED``, stated on every order with an invoice).

Gaps never quote source text: they name codes, counts and ISO 4217 currencies only.
Evidence label of fixture runs: ``isolated-fixture``; nothing here reads a source by itself.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any, Callable, Mapping, Sequence

from ontology.units import unit_token
from operating.landed_cost import ISO_4217, _canon, _dec_str, _sha
from operating.order_margin import _binding, _number, _Run as _BcRun

ENGINE = "sage_order_margin"
PROPERTY_ROLES = ("order_number", "order_type", "line_type", "line_item", "outstanding",
                  "shipped", "invoice", "invoice_currency", "home_currency",
                  "rate", "invoice_lines_count", "invoice_discount", "invoice_line_type",
                  "invoice_item", "invoiced", "extended",
                  "discount_share", "unit_cost", "cost_conversion", "unit_conversion")
SELECT_ROLES = ("order",)
LINKS = ("line_link", "invoice_link", "invoice_line_link")
COVERED = ("line_link", "invoice_link")   # the links that attach to the order itself
SALES_ORDER = "1"          # OEORDH.TYPE of an active order (3 standing, 4 quote)
ITEM, CHARGE = "1", "2"    # OE detail LINETYPE
CENT = Decimal("0.01")
TOTAL_STEP_LINES = 6    # item lines a total's steps list (the totals repeat on every page)
# Gaps that leave the whole order unread or unconvertible: no item figure is stated.
WHOLE_ORDER = frozenset({"ORDER_LINES_INCOMPLETE", "SAGE_INVOICE_LINES_INCOMPLETE",
                         "SAGE_DISCOUNT_UNALLOCATED", "SAGE_RATE_UNPROVEN",
                         "INVOICE_CURRENCY_UNKNOWN", "MIXED_CURRENCY_NO_CITED_RATE"})
_GAP_STATES = {"SAGE_CREDIT_NOTES_NOT_SERVED": "not_ingested",
               "SAGE_COST_NOT_VALUED": "unknown"}
CREDIT_NOTE = ("Sage serves no O/E credit note (no credit note relation; the GL shows they "
               "exist), so every figure is before any credit note on this order")
Resolver = Callable[[str, Any], Any]


def plan_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
               schema: Any) -> list[str]:
    """Problems with ``engine_args``: every role names a property (``order``: a select)
    evidence item listed before the compute item, every link is declared, ``bindings``
    lists the order bindings the engine computes."""
    out: list[str] = []
    roles = args.get("roles") if isinstance(args, Mapping) else None
    if not isinstance(roles, Mapping):
        return ["engine_args.roles must map roles to evidence names"]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    out += [f"unknown role {r}" for r in set(roles) - set(PROPERTY_ROLES) - set(SELECT_ROLES)]
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
    bindings = args.get("bindings")
    if not isinstance(bindings, list) or not bindings \
            or not all(isinstance(b, str) for b in bindings):
        out.append("bindings must list the order binding ids")
    return out


class _Run(_BcRun):
    """The BC engine's run plumbing (roles, links, re-read, gaps) over this plan's roles."""

    def __init__(self, args, evidence, edges, items, resolver, actor) -> None:
        self.args, self.items = args, items
        self.roles = args["roles"]
        self.got = {role: [e for e in evidence.get(self.roles[role], ()) if e.get("object_id")]
                    for role in PROPERTY_ROLES}
        self.selected = {role: next((e.get("value") for e in evidence.get(
            self.roles[role], ())), None) for role in SELECT_ROLES}
        self.links = {}
        for edge in edges:
            for link in LINKS:
                if edge.get("link_type") == args[link]:
                    self.links.setdefault(link, []).append((edge["from"], edge["to"]))
        self.resolver, self.actor = resolver, actor
        self._views, self.grouped, self.reasons = {}, {}, []

    def gap(self, code: str, subject: str, detail: str, *roles: str,
            line: str | None = None) -> None:
        super().gap(code, subject, detail, *roles, line=line)
        for gap in self.grouped.values():
            gap["state"] = _GAP_STATES.get(gap["reason"].upper(), gap["state"])

    def number(self, role: str, oid: str) -> tuple[Decimal | None, str | None]:
        """(the re-read number, its entry name) of an object's role; None when absent or
        not re-read equal for this reader."""
        entry = self.by_object(role).get(oid)
        return (self.amount(entry), entry["name"]) if entry else (None, None)

    def text(self, role: str, oid: str) -> tuple[str | None, str | None]:
        """(the re-read text, folded, and its entry name); None unless it re-reads equal."""
        entry = self.by_object(role).get(oid)
        value = (entry or {}).get("value")
        if isinstance(value, Mapping):
            value = value.get("untrusted_text")
        if entry is None or not isinstance(value, str):
            return None, None
        resolved, raw = self.raw(entry)
        fold = " ".join(value.split()).upper()
        if not resolved or not isinstance(raw, str) or " ".join(raw.split()).upper() != fold:
            return None, entry["name"]
        return fold, entry["name"]


def _money(value: Decimal, currency: str) -> str:
    return f"{_dec_str(value)} {currency}"


def plan_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Resolver | None, actor: Any,
             complete: bool, covers: Callable[[str], bool], **_: Any) -> dict[str, Any]:
    """Computed entries (per item revenue, cost and margin; order totals and margin %),
    typed gaps and reason codes for one plan run (module doc)."""
    run = _Run(args, evidence, edges, items, resolver, actor)
    root = run.selected["order"]
    total_names = {key: f"{name}_{key}" if key != "margin" else name
                   for key in ("revenue", "booked_cost", "margin", "margin_pct")}
    totals = [{"name": n[:64], "kind": "computed", "value": None, "inputs": [], "steps": [],
               "check": "not_applicable"} for n in total_names.values()]

    def done(why: str, entries: Sequence[dict] = ()) -> dict[str, Any]:
        for entry in totals:
            entry["steps"] = entry["steps"] or [f"no order total: {why}"[:300]]
        return {"entries": totals + list(entries), "gaps": run.gaps(), "reasons": run.reasons}

    if not isinstance(root, str) or _binding(root) not in set(args["bindings"]):
        run.gap("ORDER_NOT_SAGE_ROOTED", "order margin", "the order is not a Sage O/E order; "
                "this plan computes Sage orders only (BC orders: family F10)")
        return done("not a Sage order")
    number, number_name = run.text("order_number", root)
    kind, kind_name = run.text("order_type", root)
    invoice_of_line = run.targets("invoice_line_link")
    invoices = sorted({src for src, dst in run.links.get("invoice_link", ()) if dst == root})
    order_lines = sorted({src for src, dst in run.links.get("line_link", ()) if dst == root})
    if not invoices:
        run.gap("LINE_NOT_INVOICED", "order margin",
                "no invoice of the order is read; nothing of it is invoiced"
                + ("" if kind == SALES_ORDER else " (the record is not a type-1 sales order)"),
                "invoice")
        return done("nothing invoiced")
    run.gap("SAGE_CREDIT_NOTES_NOT_SERVED", "order margin", CREDIT_NOTE)
    if kind != SALES_ORDER:
        run.gap("SAGE_NOT_A_SALES_ORDER", "order", "the Sage order type is not 1 (an active "
                "order) or did not re-read for this reader", "order_type")
    if number is None:
        run.gap("CITED_VALUE_UNRESOLVED", "order", "the order number did not re-read to the "
                "projected value", "order_number")
    # An invoice's lines are proven whole by its own line count, below.
    whole = complete and all(covers(args[link]) for link in COVERED)
    if not whole:
        run.gap("ORDER_LINES_INCOMPLETE", "order lines", "the order's lines, invoices or "
                "invoice lines are not proven complete (a truncated run or a reader whose "
                "scope may hide one); no order total is stated", "line_type")

    # -------------------------------------------------- order lines: shipped, nothing open
    shipped: dict[tuple[str, str | None], Decimal] = {}
    basis: dict[str, str] = {}
    inputs: dict[str, list[str]] = {}
    open_lines = 0
    for oid in order_lines:
        line_type, type_name = run.text("line_type", oid)
        if line_type != ITEM:
            if line_type is None:
                run.gap("CITED_VALUE_UNRESOLVED", "order line", "an order line's type did not "
                        "re-read", "line_type")
            continue
        item, item_name = run.text("line_item", oid)
        values = {role: run.number(role, oid)
                  for role in ("outstanding", "shipped")}
        if item is None or any(v is None for v, _n in values.values()):
            run.gap("CITED_VALUE_UNRESOLVED", "order line", "an order line's item or "
                    "quantities did not re-read to the projected values",
                    "line_item", "shipped")
            continue
        if values["outstanding"][0] != 0:      # backorders are part of it
            open_lines += 1
        entry = run.by_object("shipped")[oid]
        key = (item, unit_token(entry.get("unit")))
        shipped[key] = shipped.get(key, Decimal(0)) + values["shipped"][0]
        basis.setdefault(item, oid)
        inputs.setdefault(item, []).extend(
            [type_name, item_name] + [n for _v, n in values.values()])
    if open_lines:
        run.gap("LINE_PARTIALLY_INVOICED", "order lines", f"{open_lines} item line(s) of the "
                "order still have a quantity outstanding or backordered; a margin of part "
                "of an order is not stated", "outstanding")

    # -------------------------------------------------- invoices and their lines
    figures: dict[str, dict[str, Any]] = {}       # item -> sums and steps
    homes: set[str] = set()
    heads: list[str] = []      # the summed invoices' header entries: what the totals cite
    charges = 0
    for invoice in invoices:
        label = run.text("invoice", invoice)[0] or "an invoice"
        label = label[:40]
        source, source_name = run.text("invoice_currency", invoice)
        home, home_name = run.text("home_currency", invoice)
        rate, rate_name = run.number("rate", invoice)
        count, count_name = run.number("invoice_lines_count", invoice)
        discount, discount_name = run.number("invoice_discount", invoice)
        if None in (source, home, rate, count, discount):
            run.gap("CITED_VALUE_UNRESOLVED", "invoice", "an invoice's currency, rate, line "
                    "count or discount did not re-read to the projected value", "rate",
                    line=label)
            continue
        if source not in ISO_4217 or home not in ISO_4217:
            run.gap("INVOICE_CURRENCY_UNKNOWN", "invoice", "the invoice's currency or home "
                    "currency is not an ISO 4217 code", "invoice_currency", line=label)
            continue
        if rate <= 0 or (source == home and rate != 1):
            run.gap("SAGE_RATE_UNPROVEN", "invoice", f"the invoice's posted rate does not "
                    f"convert {source} into {home} (it must be positive, and 1 within one "
                    "currency)", "rate", line=label)
            continue
        homes.add(home)
        lines = sorted(oid for oid, heads in invoice_of_line.items() if invoice in heads)
        if Decimal(len(lines)) != count:
            run.gap("SAGE_INVOICE_LINES_INCOMPLETE", "invoice", f"the invoice holds "
                    f"{_dec_str(count)} line(s) and {len(lines)} were read",
                    "invoice_lines_count", line=label)
            continue
        shares = Decimal(0)
        for oid in lines:
            share, _n = run.number("discount_share", oid)
            shares += share if share is not None else Decimal("NaN")
        if shares.is_nan() or shares != discount:
            run.gap("SAGE_DISCOUNT_UNALLOCATED", "invoice", "the invoice lines' discount "
                    "shares do not re-read or do not sum to the invoice discount, so a line's "
                    "net revenue is not proven", "discount_share", "invoice_discount",
                    line=label)
            continue
        head = [source_name, home_name, rate_name, count_name, discount_name]
        for oid in lines:
            line_type, type_name = run.text("invoice_line_type", oid)
            if line_type == CHARGE:
                charges += 1
                continue
            if line_type != ITEM:
                run.gap("INVOICE_LINE_UNATTRIBUTED", "order total", "an invoice line is "
                        "neither an item nor a charge line, or its type did not re-read",
                        "invoice_line_type", line=label)
                continue
            item, item_name = run.text("invoice_item", oid)
            if item is not None:            # an item no order line names: its invoice line
                figures.setdefault(item, {}).setdefault("line", oid)
            got = {role: run.number(role, oid) for role in (
                "invoiced", "extended", "discount_share", "unit_cost", "cost_conversion",
                "unit_conversion")}
            if item is None or any(v is None for v, _n in got.values()):
                run.gap("CITED_VALUE_UNRESOLVED", "invoice line", "an invoice line's item, "
                        "quantity, amount or cost did not re-read to the projected value",
                        "invoiced", "unit_cost", line=label)
                continue
            quantity, cost_unit = got["invoiced"][0], got["unit_cost"][0]
            if quantity != 0 and (cost_unit == 0 or got["cost_conversion"][0] == 0):
                run.gap("SAGE_COST_NOT_VALUED", "order line", "an invoiced item line has a zero "
                        "Sage unit cost or cost-unit conversion; its cost is not known, never "
                        "taken as zero", "unit_cost", line=item)
                figures.setdefault(item, {}).update(blocked=True)
                continue
            net = got["extended"][0] - got["discount_share"][0]
            revenue = (net * rate).quantize(CENT, ROUND_HALF_UP)
            cost = (quantity * got["unit_conversion"][0] / got["cost_conversion"][0]
                    * cost_unit).quantize(CENT, ROUND_HALF_UP) if quantity else Decimal(0)
            row = figures.setdefault(item, {})
            row.setdefault("revenue", Decimal(0))
            row["revenue"] += revenue
            row["cost"] = row.get("cost", Decimal(0)) + cost
            unit = unit_token(run.by_object("invoiced")[oid].get("unit"))
            row.setdefault("invoiced", {})
            row["invoiced"][unit] = row["invoiced"].get(unit, Decimal(0)) + quantity
            row.setdefault("home", home)
            row.setdefault("steps", []).append(
                f"{label}: revenue ({_dec_str(got['extended'][0])} - "
                f"{_dec_str(got['discount_share'][0])}) {source}"
                + (f" x posted rate {_dec_str(rate)}" if source != home else "")
                + f" = {_money(revenue, home)}; cost {_dec_str(quantity)} x "
                f"{_dec_str(got['unit_conversion'][0])} / {_dec_str(got['cost_conversion'][0])}"
                f" x {_dec_str(cost_unit)} = {_money(cost, home)}")
            row.setdefault("inputs", []).extend(
                head + [type_name, item_name] + [got[r][1] for r in (
                    "invoiced", "extended", "discount_share")])
            row.setdefault("cost_inputs", []).extend(
                got[r][1] for r in ("unit_cost", "cost_conversion", "unit_conversion"))
            heads += [n for n in head if n not in heads]
    if charges:
        run.gap("SAGE_CHARGES_NOT_IN_MARGIN", "order margin", f"{charges} miscellaneous charge "
                "line(s) (freight or other charges billed) are not goods: left out of the "
                "item margin")
    if len(homes) > 1:
        run.gap("MIXED_CURRENCY_NO_CITED_RATE", "order margin", "the invoices state several "
                "home currencies: " + ", ".join(sorted(homes)))

    # An order whose line set or an invoice is not read whole has no item figures either:
    # an item's lines may be among those not read (and the answer stays one page).
    unread = sorted(WHOLE_ORDER & {r.upper() for r in run.reasons})
    if unread:
        return done(", ".join(c.lower() for c in unread))

    # -------------------------------------------------- per item: invoiced = shipped
    entries: list[dict[str, Any]] = []
    computed: list[tuple[str, Decimal, Decimal, str]] = []
    goods = sorted(set(figures) | {item for item, _u in shipped})
    for index, item in enumerate(goods, start=1):
        label = f"{name}_line_{index}"
        row = figures.get(item, {})
        # The order line of the item, else the invoice line billing it: an object id.
        base = {"kind": "computed", "value": None, "check": "not_applicable",
                "basis": [basis.get(item) or row.get("line")][:1]}
        revenue = dict(base, name=f"{label}_revenue"[:64], steps=[],
                       inputs=list(dict.fromkeys(inputs.get(item, []) + row.get("inputs", [])
                                                 ))[:50])
        # Each input travels with the one figure it makes (answer pages); the quantity
        # makes both: the cost names the revenue's quantity through ``steps``.
        cost = dict(base, name=f"{label}_booked_cost"[:64], steps=[],
                    inputs=list(dict.fromkeys(row.get("cost_inputs", [])))[:50])
        # The margin is the two figures' difference, named in its steps: an entry that took
        # them as inputs would repeat on every answer page (one per item: an order of some
        # 40 items no longer fits a page), so it travels as its own unit.
        margin = {"name": f"{label}_margin"[:64], "kind": "computed", "value": None,
                  "check": "not_applicable", "steps": [], "inputs": [],
                  "basis": list(base["basis"])}
        entries += [revenue, cost, margin]
        invoiced = {u: q for u, q in row.get("invoiced", {}).items() if q}
        ordered = {u: q for (i, u), q in shipped.items() if i == item and q}
        why = None
        if row.get("blocked"):
            why = "an invoiced line of the item has no Sage cost value"
        elif item not in basis:
            why = "invoiced on the order's invoices but on none of its item lines"
            run.gap("INVOICE_LINE_UNATTRIBUTED", "order total", "an item is invoiced on the "
                    "order's invoices but is on none of its lines", "invoice_item", line=item)
        elif "revenue" not in row:
            why = "no invoice line of the item is computed"
            if not ordered:
                entries[-3:] = []
                continue                   # an item line nothing shipped of: not goods sold
            run.gap("LINE_PARTIALLY_INVOICED", "order line", "the item is shipped and no "
                    "computed invoice line bills it", "invoiced", line=item)
        elif invoiced != ordered:
            why = "the invoiced quantity differs from the shipped-to-date quantity"
            run.gap("LINE_PARTIALLY_INVOICED" if sum(invoiced.values(), Decimal(0))
                    < sum(ordered.values(), Decimal(0)) else "INVOICED_EXCEEDS_SHIPPED",
                    "order line", "the invoiced quantity of the item differs from its shipped-"
                    "to-date quantity (or is in another unit); a margin of part of a line is "
                    "not stated", "invoiced", "shipped", line=item)
        if why:
            for entry in (revenue, cost, margin):
                entry["steps"] = [f"no figure: {why}"]
            continue
        home = row["home"]
        value = row["revenue"] - row["cost"]
        revenue.update(value=_dec_str(row["revenue"]), unit=home, currency=home,
                       steps=row["steps"][:36] + [f"revenue = {_money(row['revenue'], home)}"])
        cost.update(value=_dec_str(row["cost"]), unit=home, currency=home,
                    steps=[f"cost Sage posted to cost of sales = {_money(row['cost'], home)}",
                           f"invoiced {_quantities(invoiced)} = shipped to date"])
        margin.update(value=_dec_str(value), unit=home, currency=home, steps=[
            f"margin = {revenue['name']} - {cost['name']} = {_money(row['revenue'], home)}"
            f" - {_money(row['cost'], home)} = {_money(value, home)}"[:300]])
        for entry in (revenue, cost):
            entry["digest"] = _sha(_canon({"steps": entry["steps"], "inputs": entry["inputs"]}))
        computed.append((label, row["revenue"], row["cost"], home))

    # -------------------------------------------------- order totals
    blocking = {"ORDER_NOT_SAGE_ROOTED", "SAGE_NOT_A_SALES_ORDER", "CITED_VALUE_UNRESOLVED",
                "ORDER_LINES_INCOMPLETE", "LINE_PARTIALLY_INVOICED", "INVOICE_CURRENCY_UNKNOWN",
                "SAGE_RATE_UNPROVEN", "SAGE_INVOICE_LINES_INCOMPLETE",
                "SAGE_DISCOUNT_UNALLOCATED", "INVOICE_LINE_UNATTRIBUTED",
                "SAGE_COST_NOT_VALUED",
                "INVOICED_EXCEEDS_SHIPPED", "MIXED_CURRENCY_NO_CITED_RATE"}
    stopped = sorted(blocking & {r.upper() for r in run.reasons})
    item_lines = len(entries) // 3
    # The order's revenue and cost are sums of the item lines' figures, computed here from
    # the same re-read values: they cite the summed invoices (not the item entries), so the
    # order figure stands on the answer's first page however many pages its items take.
    for entry, key in zip(totals, total_names):
        entry["inputs"] = {"margin_pct": [total_names["margin"], total_names["revenue"]],
                           "margin": [total_names["revenue"], total_names["booked_cost"]]
                           }.get(key, heads[:50])
    if stopped or not computed or len(computed) != item_lines:
        return done((", ".join(c.lower() for c in stopped) or
                     f"{item_lines - len(computed)} of {item_lines} item(s) have no margin"),
                    entries)
    home = computed[0][3]
    revenue = sum((c[1] for c in computed), Decimal(0))
    cost = sum((c[2] for c in computed), Decimal(0))
    values = {"revenue": revenue, "booked_cost": cost, "margin": revenue - cost}
    # The per-item figures are their own entries; the totals (repeated on every answer
    # page) name the first few and count the rest.
    steps = [f"{c[0]}: revenue {_money(c[1], home)}, cost {_money(c[2], home)}"
             for c in computed[:TOTAL_STEP_LINES]]
    if len(computed) > TOTAL_STEP_LINES:
        steps.append(f"and {len(computed) - TOTAL_STEP_LINES} more item(s), each its own "
                     "entry")
    steps.append(f"order: revenue {_money(revenue, home)} - cost {_money(cost, home)} = "
                 f"margin {_money(values['margin'], home)} (before any credit note; Sage "
                 "serves none)")
    if revenue != 0:
        values["margin_pct"] = (values["margin"] * 100 / revenue).quantize(CENT,
                                                                          ROUND_HALF_EVEN)
        steps.append(f"margin % on price = margin / revenue x 100 = "
                     f"{_dec_str(values['margin_pct'])}")
    digest = _sha(_canon({"steps": steps}))
    for entry, key in zip(totals, total_names):
        if key not in values:
            entry["steps"] = ["revenue is zero: no margin % on price"]
            continue
        entry.update(value=_dec_str(values[key]), steps=list(steps), digest=digest)
        entry.update(unit="%") if key == "margin_pct" else entry.update(unit=home,
                                                                        currency=home)
    return {"entries": totals + entries, "gaps": run.gaps(), "reasons": run.reasons}


def _quantities(by_unit: Mapping[str | None, Decimal]) -> str:
    return ", ".join(f"{_dec_str(q)}{' ' + u if u else ''}" for u, q in sorted(
        by_unit.items(), key=lambda kv: str(kv[0]))) or "0"


PLAN_ENGINES = {ENGINE: {"check": plan_check, "run": plan_run}}

__all__ = ["ENGINE", "PLAN_ENGINES", "plan_check", "plan_run"]
