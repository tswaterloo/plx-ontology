"""Why a margin moved: the change against a baseline split into price, material cost,
returns and credits, and mix (``plx.aggregate`` mode ``margin_variance``).

No margin is computed here. Every figure is the order margin engine's checked one
(``customer_profit.read_order``: F10 for a Business Central order, F31 for a Sage one;
invoiced revenue net of applied credit memos less booked item cost, every input a cited
value re-read for the reader), read through the reader's own tool calls, so a record the
reader may not read is never counted and an order whose margin it may not see is left out
(``complete: false``, never a count of what it may not see).

* **Subject.** A sales order (its own invoiced lines against its customer's orders first
  invoiced in the baseline window, by default the 365 days before it), a customer (its
  orders), a product (the lines of that item on any order), or none: every order first
  invoiced in the window (a period). An order counts in a window by its first posted
  invoice's posting date (``customer_profit``'s rule). Default windows: the 90 days to
  today against the 90 days before.
* **Per item, unit and currency** (only lines with a checked revenue, cost and margin and an
  invoiced quantity), per invoiced unit: gross price ``g`` (revenue plus credits), credits
  net of the cost those credit memos returned ``k``, shipped cost ``s`` (booked cost plus
  that returned cost), so margin ``m = g - k - s``. With ``Q`` the invoiced quantity,
  ``1`` the period and ``0`` the baseline: price ``(g1 - g0) Q1``, material cost
  ``-(s1 - s0) Q1``, returns and credits ``-(k1 - k0) Q1``, and mix ``(Q1 - Q0) m0`` plus
  the whole margin of items sold on one side only. The parts sum exactly to the margin
  change (mix takes the cent of rounding).
* **Freight.** Inbound freight and duty are expensed to the G/L, never to the item ledger
  (``customer_profit.FREIGHT_GAP``), so they are in no margin figure. Beside the identity,
  for the largest items the change in landed freight and duty per unit of each window's
  latest purchase order of the item (F7, ``customer_profit.materials``: landed less goods
  cost per unit, both cited) times ``Q1`` is an estimate, never added to the parts.
* **Currencies** are never converted nor summed: one block per currency. Money is
  ``Decimal``. Finance-marked: a reader that does not hold ``finance`` gets no figure.

Fixture runs are ``isolated-fixture``; live-unverified.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal
import json
import time
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from ontology.json_contract import validate
from ontology.units import unit_token

from . import customer_profit as cp
from . import injection_guard

TOOL_NAME = "plx.aggregate"   # served as its mode ``margin_variance``, never a listed tool
MODE = "margin_variance"
FORMAT = "plx-margin-variance/v1"
FINANCE = "finance"
WINDOW_DAYS = 90
ORDER_BASELINE_DAYS = 365
MAX_ORDERS = 40          # orders read per window; past it the answer is not complete
DRIVERS = 8              # items listed per currency, largest margin change first
FREIGHT_ITEMS = 3        # items whose landed freight is read for the estimate
BUDGET_SECONDS = 35.0    # plx.aggregate's call limit is 45 s
CENT = Decimal("0.01")
KEYS = frozenset({"subject", "from", "to", "baseline_from", "baseline_to"})
SUBJECTS = {"SalesOrder": "order", "Customer": "customer", "Product": "product"}
SO_LINE_OF = "SalesOrderLine.line_of"
INVOICE_DATES = ("posting_date", "invoice_date")
POSTED = frozenset({"open", "paid"})
COMPONENTS = ("price", "material_cost", "returns_credits", "mix")


def _contracts() -> Mapping[str, Any]:
    from .ontology_tools import load_contracts
    return load_contracts()


def _error(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message[:600],
                      **({"hint": hint[:600]} if hint else {})}}


def _money(n: Decimal) -> str:
    return format(n.quantize(CENT), "f")


def _date(raw: Any) -> dt.date | None:
    if not isinstance(raw, str):
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


@dataclass
class Cell:
    """One item, unit and currency on one side: quantity and the money per part."""
    quantity: Decimal = Decimal(0)
    gross: Decimal = Decimal(0)      # revenue + credit memo amounts
    credits: Decimal = Decimal(0)    # credit memo amounts less the cost they returned
    shipped: Decimal = Decimal(0)    # booked cost + the cost credit memos returned
    returned: Decimal = Decimal(0)   # that returned cost
    handles: list[str] = field(default_factory=list)

    @property
    def margin(self) -> Decimal:
        return self.gross - self.credits - self.shipped

    def per(self, amount: Decimal) -> Decimal:
        return amount / self.quantity


@dataclass
class Side:
    orders: list[cp.Order] = field(default_factory=list)
    without_margin: int = 0
    not_read: int = 0
    withheld: bool = False
    cells: dict[tuple, Cell] = field(default_factory=dict)
    lines_left_out: int = 0


def add_order(side: Side, order: cp.Order, item: str | None = None) -> None:
    """The order's checked lines into ``side`` (``item``: only that item's lines)."""
    side.orders.append(order)
    for line in order.lines:
        if item is not None and line.item != item:
            continue
        currency = line.currency or order.currency
        if (line.item is None or line.revenue is None or line.cost is None
                or line.margin is None or currency is None or line.invoiced <= 0):
            side.lines_left_out += 1
            continue
        key = (line.item, unit_token(line.unit) if line.unit else None, currency)
        cell = side.cells.setdefault(key, Cell())
        cell.quantity += line.invoiced
        cell.gross += line.revenue + line.credit_amount
        cell.credits += line.credit_amount - line.returned_cost
        cell.shipped += line.cost + line.returned_cost
        cell.returned += line.returned_cost
        for h in (*line.invoice_handles, *line.credit_handles, *line.ledger_handles,
                  *line.cost_handles, *line.return_handles):
            if isinstance(h, str) and h and h not in cell.handles:
                cell.handles.append(h)


def decompose(now: Side, before: Side) -> dict[str, dict[str, Any]]:
    """currency -> the two sides' figures, the parts of the margin change (exact, in
    cents: mix takes the rounding) and the items, largest change first."""
    out: dict[str, dict[str, Any]] = {}
    keys = sorted(set(now.cells) | set(before.cells), key=lambda k: (k[2], k[0], k[1] or ""))
    for key in keys:
        currency = key[2]
        block = out.setdefault(currency, {"parts": defaultdict(Decimal), "items": [],
                                          "now": Cell(), "before": Cell()})
        one, zero = now.cells.get(key), before.cells.get(key)
        parts = dict.fromkeys(COMPONENTS, Decimal(0))
        if one is not None and zero is not None:
            q1 = one.quantity
            parts["price"] = (one.per(one.gross) - zero.per(zero.gross)) * q1
            parts["material_cost"] = -(one.per(one.shipped) - zero.per(zero.shipped)) * q1
            parts["returns_credits"] = -(one.per(one.credits) - zero.per(zero.credits)) * q1
            parts["mix"] = (q1 - zero.quantity) * zero.per(zero.margin)
        elif one is not None:
            parts["mix"] = one.margin
        else:
            parts["mix"] = -zero.margin
        for side, cell in (("now", one), ("before", zero)):
            if cell is not None:
                total = block[side]
                total.gross += cell.gross
                total.credits += cell.credits
                total.shipped += cell.shipped
                total.returned += cell.returned
        for name, value in parts.items():
            block["parts"][name] += value
        block["items"].append({"key": key, "parts": parts,
                               "change": (one.margin if one else Decimal(0))
                               - (zero.margin if zero else Decimal(0)),
                               "now": one, "before": zero})
    for block in out.values():
        change = block["now"].margin - block["before"].margin
        rounded = {n: block["parts"][n].quantize(CENT) for n in COMPONENTS[:-1]}
        rounded["mix"] = change.quantize(CENT) - sum(rounded.values(), Decimal(0))
        block["rounded"], block["change"] = rounded, change
        block["items"].sort(key=lambda i: (-abs(i["change"]), i["key"][0], i["key"][1] or ""))
    return out


class Budget:
    def __init__(self, seconds: float | None, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.deadline = None if seconds is None else clock() + seconds

    def spent(self) -> bool:
        return self.deadline is not None and self.clock() >= self.deadline


class MarginVarianceTool:
    """``plx.aggregate`` mode ``margin_variance`` over a host's tool table and its
    ``OntologyTools`` (policy, clock, store)."""

    def __init__(self, table: Mapping[str, dict[str, Any]], base: Any, *,
                 budget_seconds: float | None = BUDGET_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.table, self.base = table, base
        self.budget_seconds, self.clock = budget_seconds, clock

    # ------------------------------------------------------------------ the call
    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict) or arguments.get("mode") != MODE:
            return self._checked(_error("invalid_input", "mode must be margin_variance"))
        held = frozenset(self.base.policy.held_markings(actor))
        if FINANCE not in held:
            return self._checked(_error(
                "finance_not_held", "a margin is made of finance-marked figures, and this "
                "reader does not hold finance", "ask someone who holds the finance marking"))
        asked = arguments.get("variance", {})
        if not isinstance(asked, dict) or set(asked) - KEYS \
                or any(not isinstance(v, str) for v in asked.values()):
            return self._checked(_error(
                "invalid_input", "variance takes text fields subject, from, to, "
                "baseline_from and baseline_to only",
                "e.g. {\"mode\": \"margin_variance\", \"variance\": {\"subject\": "
                "\"Customer:...\", \"from\": \"2026-08-01\", \"to\": \"2026-10-31\"}}"))
        subject = asked.get("subject")
        kind = "period" if subject is None else SUBJECTS.get(subject.split(":", 1)[0])
        if kind is None:
            return self._checked(_error("invalid_input", "subject must be the object id of "
                                        "a SalesOrder, a Customer or a Product"))
        dates = {k: _date(asked[k]) for k in KEYS - {"subject"} if k in asked}
        if None in dates.values():
            return self._checked(_error("invalid_input", "dates are ISO dates (YYYY-MM-DD)"))
        today = self.base._as_known_at({}).date()

        def call(name: str, args: dict) -> Any:
            entry = self.table.get(name)
            return None if entry is None else entry["handler"](dict(args), actor)

        budget = Budget(self.budget_seconds, self.clock)
        try:
            result = self._answer(call, kind, subject, dates, today, budget)
        except cp.ProfitError:
            result = _error("subject_not_readable", "the subject is not a record this "
                            "reader may read")
        return self._checked(result)

    # ------------------------------------------------------------------ windows
    @staticmethod
    def _windows(dates: Mapping[str, dt.date], today: dt.date, *, end: dt.date | None = None,
                 start: dt.date | None = None, baseline_days: int | None = None
                 ) -> tuple[dt.date, dt.date, dt.date, dt.date] | str:
        to = dates.get("to") or end or today
        since = dates.get("from") or start or to - dt.timedelta(days=WINDOW_DAYS - 1)
        span = (to - since).days + 1
        b_to = dates.get("baseline_to") or since - dt.timedelta(days=1)
        b_from = dates.get("baseline_from") or b_to - dt.timedelta(
            days=(baseline_days or span) - 1)
        if since > to or b_from > b_to:
            return "a window ends before it starts"
        if b_to >= since:
            return "the baseline must end before the period starts"
        return since, to, b_from, b_to

    def _answer(self, call: Callable, kind: str, subject: str | None,
                dates: Mapping[str, dt.date], today: dt.date, budget: Budget
                ) -> dict[str, Any]:
        gaps: list[dict[str, str]] = []
        complete = True
        item = subject if kind == "product" else None
        if kind == "order":
            if cp._safe(call, "plx.object", {"object_id": subject}) is None:
                raise cp.ProfitError("order_not_readable")
            own = sorted(cp._order_dates(call, subject))
            if not own:
                return _error("order_not_invoiced", "the order has no posted invoice, so "
                              "it has no margin yet")
            windows = self._windows(dates, today, start=own[0], end=max(own[-1], own[0]),
                                    baseline_days=ORDER_BASELINE_DAYS)
            if isinstance(windows, str):
                return _error("invalid_input", windows)
            customers, _e, _w, _c = cp._around(call, subject, [(cp.ORDER_LINK, "forward")], 2)
            candidates: list[tuple[str, str, list[dt.date]]] = [(subject, subject, own)]
            if len(customers) == 1:
                listed, trimmed, whole = self._customer_orders(
                    call, customers[0]["object_id"], windows, budget)
                candidates += [c for c in listed if c[0] != subject]
                complete = complete and whole and not trimmed
            else:
                gaps.append({"code": "CUSTOMER_NOT_ONE",
                             "detail": "the order is not tied to one customer this reader "
                                       "may read, so it has no baseline orders"})
        else:
            windows = self._windows(dates, today)
            if isinstance(windows, str):
                return _error("invalid_input", windows)
            if kind == "customer":
                if cp._safe(call, "plx.object", {"object_id": subject}) is None:
                    raise cp.ProfitError("customer_not_readable")
                candidates, trimmed, whole = self._customer_orders(call, subject, windows,
                                                                   budget)
            elif kind == "product":
                if cp._safe(call, "plx.object", {"object_id": subject}) is None:
                    raise cp.ProfitError("product_not_readable")
                candidates, trimmed, whole = self._product_orders(call, subject, windows,
                                                                  budget)
            else:
                candidates, trimmed, whole = self._period_orders(call, windows, budget)
            complete = complete and whole and not trimmed
        since, to, b_from, b_to = windows
        now, before = Side(), Side()
        for oid, number, days in sorted(candidates, key=lambda c: (-c[2][0].toordinal(),
                                                                   c[0])):
            first = days[0]
            if kind == "order":     # the order alone against its customer's earlier orders
                side = now if oid == subject else before if b_from <= first <= b_to else None
            else:
                side = now if since <= first <= to else before if b_from <= first <= b_to \
                    else None
            if side is None:
                continue
            if len(side.orders) + side.without_margin >= MAX_ORDERS or budget.spent():
                side.not_read += 1
                continue
            order = cp.read_order(call, cp.Order(oid, number, days))
            if order.withheld:
                side.withheld = True
                continue
            if not order.decided:
                # an order none of whose lines is invoiced yet has no margin to have
                if any(ln.invoice_handles or ln.invoiced for ln in order.lines):
                    side.without_margin += 1
                continue
            add_order(side, order, item)
        if now.not_read or before.not_read or now.withheld or before.withheld \
                or now.without_margin or before.without_margin:
            complete = False
        if now.without_margin or before.without_margin:
            gaps.append({"code": "ORDERS_WITHOUT_MARGIN",
                         "detail": "orders invoiced in a window with no checked margin (a "
                                   "cost not valued, a partial invoice, a failed check) are "
                                   "left out"})
        if now.not_read or before.not_read:
            gaps.append({"code": "ORDERS_NOT_ALL_READ",
                         "detail": f"past {MAX_ORDERS} orders a window or the time budget, "
                                   "orders were not read; the figures cover those read"})
        if now.lines_left_out or before.lines_left_out:
            gaps.append({"code": "LINES_WITHOUT_MARGIN",
                         "detail": "lines with no checked revenue, cost, item or invoiced "
                                   "quantity are left out of the split"})
        gaps.append({"code": cp.FREIGHT_GAP[0], "detail": cp.FREIGHT_GAP[1]})
        blocks = decompose(now, before)
        by_currency = []
        for currency, block in sorted(blocks.items()):
            by_currency.append({
                "currency": currency,
                "current": self._side_figures(block["now"], now, currency),
                "baseline": self._side_figures(block["before"], before, currency),
                "change": _money(block["change"]),
                "components": {n: _money(v) for n, v in block["rounded"].items()},
                "freight": self._freight(call, block, currency, to, b_to, budget),
                "drivers": [self._driver(i) for i in block["items"][:DRIVERS]]})
        if len(by_currency) > 1:
            gaps.append({"code": "MIXED_CURRENCY", "detail": "figures in more than one "
                         "currency are split per currency, never converted or summed"})
        return {
            "mode": MODE, "format": FORMAT,
            "subject": {"object_id": subject, "kind": kind},
            "period": {"from": since.isoformat(), "to": to.isoformat()},
            "baseline": {"from": b_from.isoformat(), "to": b_to.isoformat()},
            "by_currency": by_currency,
            "orders": {"current": len(now.orders), "baseline": len(before.orders),
                       "without_margin": now.without_margin + before.without_margin,
                       "not_read": now.not_read + before.not_read},
            "gaps": gaps[:12],
            "basis": ("The order margin engine's checked figures per item, unit and currency "
                      "(item cost only): price, material cost and returns/credits are each "
                      "change per invoiced unit times this period's quantity; mix is the "
                      "quantity change at the baseline unit margin plus items sold on one "
                      "side only. The parts sum to the change; freight is an estimate "
                      "beside it."),
            "complete": complete, "evidence": "tools"}

    @staticmethod
    def _side_figures(cell: Cell, side: Side, currency: str) -> dict[str, Any]:
        revenue = cell.gross - cell.credits - cell.returned   # net of credit memos
        cost = cell.shipped - cell.returned                    # booked cost
        margin = cell.margin
        orders = sum(1 for o in side.orders
                     if any((ln.currency or o.currency) == currency for ln in o.lines))
        return {"revenue": _money(revenue), "cost": _money(cost), "margin": _money(margin),
                "margin_pct": _money(margin / revenue * 100) if revenue > 0 else None,
                "orders": orders}

    @staticmethod
    def _driver(item: Mapping[str, Any]) -> dict[str, Any]:
        object_id, unit, _currency = item["key"]
        handles = [h for c in (item["now"], item["before"]) if c is not None
                   for h in c.handles]
        # a unit is typed only when it reads as one (``injection_guard.is_unit``): a line's
        # prose in its unit field leaves as null, never as text the reader sees bare
        return {"item": object_id, "unit": unit if injection_guard.is_unit(unit) else None,
                "margin_change": _money(item["change"]),
                **{n: _money(item["parts"][n]) for n in COMPONENTS},
                "sold": ("both" if item["now"] and item["before"]
                         else "period_only" if item["now"] else "baseline_only"),
                "citations": list(dict.fromkeys(handles))[:8]}

    def _freight(self, call: Callable, block: Mapping[str, Any], currency: str,
                 to: dt.date, b_to: dt.date, budget: Budget) -> dict[str, Any]:
        """The landed freight-and-duty estimate (never in the parts): per item sold on both
        sides, the change per unit of the latest purchase order's freight before each
        window's end, times this period's quantity."""
        total, read, handles = Decimal(0), 0, []
        both = [i for i in block["items"] if i["now"] is not None and i["before"] is not None]
        both.sort(key=lambda i: (-i["now"].gross, i["key"][0]))
        for entry in both[:FREIGHT_ITEMS]:
            if budget.spent():
                break
            object_id, unit, _c = entry["key"]
            per: list[tuple[Decimal, list[str]]] = []
            for end in (to, b_to):
                rows, _gaps = cp.materials(call, object_id, end, currency, unit)
                row = next((r for r in rows if r.get("comparable")
                            and r.get("per_unit") is not None
                            and r.get("goods_per_unit") is not None), None)
                if row is None:
                    break
                per.append((row["per_unit"] - row["goods_per_unit"], list(row["handles"])))
            if len(per) != 2:
                continue
            read += 1
            total += -(per[0][0] - per[1][0]) * entry["now"].quantity
            handles += [h for p in per for h in p[1]]
        return {"estimate": _money(total) if read else None, "items": read,
                "in_margin": False, "citations": list(dict.fromkeys(handles))[:8]}

    # ------------------------------------------------------------------ order lists
    @staticmethod
    def _customer_orders(call: Callable, customer: str, windows: tuple, budget: Budget
                         ) -> tuple[list[tuple[str, str, list[dt.date]]], bool, bool]:
        since, to, b_from, _b_to = windows
        period = cp.Period(b_from, to, "", True)
        orders, trimmed, whole, _out, _skip = cp.customer_orders(call, customer, period,
                                                                 late=budget.spent)
        return [(o.object_id, o.number, o.dates) for o in orders if o.dates], trimmed, \
            bool(whole)

    @staticmethod
    def _product_orders(call: Callable, item: str, windows: tuple, budget: Budget
                        ) -> tuple[list[tuple[str, str, list[dt.date]]], bool, bool]:
        _since, to, b_from, _b_to = windows
        found, _e, trimmed, whole = cp._around(
            call, item, [(cp.LINE_ITEM, "reverse"), (SO_LINE_OF, "forward")],
            cp.MAX_ORDERS * 4)
        out = []
        for ref in found:
            if budget.spent():
                return out, trimmed, False
            days = sorted(cp._order_dates(call, ref["object_id"]))
            if days and b_from <= days[0] <= to:
                out.append((ref["object_id"], cp._display(ref) or ref["object_id"], days))
        return out, trimmed, whole

    def _period_orders(self, call: Callable, windows: tuple, budget: Budget
                       ) -> tuple[list[tuple[str, str, list[dt.date]]], bool, bool]:
        """Orders billed by an invoice posted in either window, newest first (the store's
        invoice index; each order is then read through the reader's own tools, so an
        order it may not read is never counted)."""
        since, to, b_from, _b_to = windows
        conn = self.base.store.conn
        rows = conn.execute(
            "SELECT DISTINCT l.to_object, v.value FROM link_rows l JOIN object_values v ON "
            "v.object_id = l.from_object AND v.superseded_at IS NULL AND v.property IN "
            "(SELECT value FROM json_each(:props)) WHERE l.link_type = :link "
            "AND l.superseded_at IS NULL AND l.tier <> 'contradicted' "
            "AND substr(v.value, 1, 10) BETWEEN :start AND :end",
            {"props": json.dumps(list(INVOICE_DATES)), "link": cp.INVOICE_LINK,
             "start": b_from.isoformat(), "end": to.isoformat()}).fetchall()
        newest: dict[str, str] = {}
        for oid, day in rows:
            if oid not in newest or str(day) > newest[oid]:
                newest[oid] = str(day)
        out: list[tuple[str, str, list[dt.date]]] = []
        whole = True
        for oid in sorted(newest, key=lambda o: (newest[o], o), reverse=True):
            if budget.spent() or len(out) >= 2 * MAX_ORDERS:
                whole = False
                break
            if cp._safe(call, "plx.object", {"object_id": oid,
                                             "properties": ["order_date"]}) is None:
                whole = False      # an order this reader may not read: never counted
                continue
            days = sorted(cp._order_dates(call, oid))
            if days and b_from <= days[0] <= to:
                out.append((oid, oid, days))
        return out, False, whole

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        contracts = _contracts()
        schema = contracts["$defs"]["margin_variance_answer" if "error" not in result
                                    else "error_result"]
        problems = validate(result, schema, root=contracts)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + str(problems[0]))[:600])
        if len(json.dumps(result)) > int(contracts["limits"]["max_result_bytes"]) - 2000:
            return _error("result_too_large", "the margin variance exceeds the result budget")
        return result


def register(table: MutableMapping[str, dict[str, Any]], base: Any, *,
             calls: Mapping[str, dict[str, Any]] | None = None) -> MarginVarianceTool:
    """Serve ``plx.aggregate`` mode ``margin_variance`` in a tool table: a call naming the
    mode goes here, every other call to the handler before it (plx.aggregate, or a mode
    registered earlier). A table without plx.aggregate serves no mode of it. ``calls``:
    the table the mode reads through as the caller (plx.object/around/investigate;
    default ``table``)."""
    tool = MarginVarianceTool(table if calls is None else calls, base)
    entry = table.get(TOOL_NAME)
    if entry is None:
        return tool
    before = entry["handler"]

    def handler(arguments: Any, actor: Any) -> dict[str, Any]:
        if isinstance(arguments, dict) and arguments.get("mode") == MODE:
            return tool.call(arguments, actor)
        return before(arguments, actor)
    table[TOOL_NAME] = dict(entry, handler=handler)
    return tool


__all__ = ["FORMAT", "MODE", "TOOL_NAME", "MarginVarianceTool", "add_order", "decompose",
           "register"]
