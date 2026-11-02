"""plx.investigate plan ``sage_order_margin`` (F31) at the tool boundary, and the customer
profitability reader choosing it for a Sage order.

Synthetic Sage O/E rows (the reporting API's column names) are projected by the one
projector; ``plx.investigate`` runs its real plan and engine, every citation is re-read
through a per-reader row resolver, and every answer is validated against the tool contract.
Evidence label: isolated-fixture (synthetic rows, never live PLX data).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from ontology import identifiers, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import build_store
from ontology.types import Actor
from operating import customer_profit as cp
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt
from tests import test_investigate_people_graph as base

CONTRACTS = ot.load_contracts()
OUTPUT = CONTRACTS["tools"]["plx.investigate"]["output"]
NOW = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)
FULL = Actor("plx", frozenset({"staff:ops", "staff:prices"}), "restricted")
NO_FINANCE = Actor("plx", frozenset({"staff:ops"}), "restricted")
HDR, DTL = "sage.oe_order_header", "sage.oe_order_detail"
IHDR, IDTL = "sage.oe_invoice_header", "sage.oe_invoice_detail"


def _pad(text: str, width: int = 22) -> str:
    return text.ljust(width)             # Sage pads its text columns


class Rows:
    """Sage O/E rows of synthetic orders, each order with one invoice."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []
        self.serial = 0

    def order(self, number: str, lines: list[dict], *, kind: str = "1", currency="CAD",
              rate="1", invoice=True, invlines: int | None = None, discount="0",
              charges: tuple[str, ...] = (), billed: tuple[dict, ...] = ()) -> None:
        """``lines``: dicts with item, qty, ext, disc, cost and optional conv/unitconv,
        outstanding, shipped (default qty) and original (default qty + outstanding);
        ``billed``: invoice-only item lines (the same keys) on none of the order's lines."""
        self.serial += 1
        uniq = str(1000 + self.serial)
        self.rows.append((HDR, {"orduniq": uniq, "ordnumber": _pad(number), "type": kind,
                                "customer": _pad("CUST01", 12), "orddate": "20250301",
                                "orsourcurr": currency}))
        for n, line in enumerate(lines, start=1):
            self.rows.append((DTL, {
                "_sage_pk": f"{uniq}|{n}", "orduniq": uniq, "linenum": str(32 * n),
                "detailnum": str(n), "linetype": "1", "item": _pad(line["item"], 24),
                "ordunit": "EA", "qtyordered": line.get("outstanding", "0"),
                "origqty": line.get("original", str(Decimal(line["qty"]) + Decimal(
                    line.get("outstanding", "0")))),
                "qtybackord": "0", "qtyshptodt": line.get("shipped", line["qty"])}))
        if not invoice:
            return
        inv = str(5000 + self.serial)
        details = [{"linetype": "1", "item": _pad(line["item"], 24), "invunit": "EA",
                    "qtyshipped": line["qty"], "extinvmisc": line["ext"],
                    "invdisc": line.get("disc", "0"), "cosuntcst": line["cost"],
                    "cosuntconv": line.get("conv", "1"),
                    "unitconv": line.get("unitconv", "1")} for line in [*lines, *billed]]
        details += [{"linetype": "2", "item": _pad("", 24), "invunit": "", "qtyshipped": "0",
                     "extinvmisc": amount, "invdisc": "0", "cosuntcst": "0",
                     "cosuntconv": "0", "unitconv": "0"} for amount in charges]
        self.rows.append((IHDR, {"invuniq": inv, "invnumber": _pad(f"IN{number}"),
                                 "ordnumber": _pad(number), "customer": _pad("CUST01", 12),
                                 "invdate": "20250310", "insourcurr": currency,
                                 "inhomecurr": "CAD", "inrate": rate,
                                 "invlines": str(len(details) if invlines is None
                                                 else invlines),
                                 "invdiscamt": discount}))
        for n, detail in enumerate(details, start=1):
            self.rows.append((IDTL, {"_sage_pk": f"{inv}|{n}", "invuniq": inv,
                                     "ordnumber": _pad(number), "detailnum": str(n),
                                     "orddtlnum": str(n), **detail}))

    def records(self):
        out, self.handles = [], {}
        keys = {HDR: "orduniq", IHDR: "invuniq"}
        for n, (rel, row) in enumerate(self.rows, start=1):
            key = {keys.get(rel, "_sage_pk"): row[keys.get(rel, "_sage_pk")]}
            handle = f"api://{rel}/{n}@g-1"
            self.handles[handle] = (rel, row)
            out.append(base.record("portal_api", rel, json.dumps(key), row, handle,
                                   f"{n:064x}", f"api:{rel}"))
        return out


class World:
    def __init__(self, rows: Rows) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ontology.sqlite"
        self.compiled = schema_loader.load_schema()
        records = rows.records()
        self.handles = rows.handles
        build_store(self.path, self.compiled,
                    {"portal_api": base.Static("portal_api", records)},
                    normalizer=identifiers.normalize, clock=lambda: NOW)

    def close(self) -> None:
        self.tmp.cleanup()

    def rows(self, handle: str, actor: Actor):
        found = self.handles.get(handle)
        return None if found is None else {"citation": handle, "relation": found[0],
                                           "key": {}, "row": dict(found[1])}

    def table(self) -> dict:
        policy = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"staff:ops"}},
                               markings={m: {"staff:prices"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        tools = ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(
            self.compiled), grammar=None, contracts=CONTRACTS, clock=lambda: NOW)
        table: dict = {"_reader": reader}
        ot.register(table, tools)
        tt.register(table, tt.TraverseTools(tools))
        oi.register(table, oi.InvestigateTools(table, rows=self.rows,
                                               scope=oi.read_scope(tools), clock=lambda: NOW))
        return table


def order_id(number: str) -> str:
    # The projector's object id of a Sage order header (key orduniq).
    return f'SalesOrder:portal.sage_oe_order_header:{{"orduniq":"{number}"}}'


class SageOrderMarginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        rows = Rows()
        # 1001: CAD, two items (one costed in another unit), a discount and a freight charge.
        rows.order("OR1", [{"item": "ITEM-A", "qty": "10", "ext": "100", "disc": "4",
                            "cost": "6"},
                           {"item": "ITEM-B", "qty": "4", "ext": "40", "disc": "1",
                            "cost": "2.5", "conv": "12", "unitconv": "24"}],
                   discount="5", charges=("15",))
        # 1002: a USD invoice at its posted rate.
        rows.order("OR2", [{"item": "ITEM-A", "qty": "2", "ext": "50", "cost": "20"}],
                   currency="USD", rate="1.25")
        # 1003: a zero unit cost.  1004: an invoice with a line not served.
        rows.order("OR3", [{"item": "ITEM-A", "qty": "1", "ext": "10", "cost": "0"}])
        rows.order("OR4", [{"item": "ITEM-A", "qty": "1", "ext": "10", "cost": "3"}],
                   invlines=2)
        # 1005: a quote, never invoiced.  1006: still a quantity outstanding.
        rows.order("OR5", [{"item": "ITEM-A", "qty": "1", "ext": "10", "cost": "3",
                            "outstanding": "1"}], kind="4", invoice=False)
        rows.order("OR6", [{"item": "ITEM-A", "qty": "1", "ext": "10", "cost": "3",
                            "outstanding": "2"}])
        # 1007: a CAD invoice whose rate is not 1.
        rows.order("OR7", [{"item": "ITEM-A", "qty": "1", "ext": "10", "cost": "3"}],
                   rate="1.3")
        # 1008-1011: 1, 10, 50 and 60 item lines.
        for count in (1, 10, 50, 60):
            rows.order(f"ORN{count}", [{"item": f"ITEM-{n:02d}", "qty": "1", "ext": "10",
                                        "cost": "4"} for n in range(count)])
        # 1012: an item billed on the order's invoice that is on none of its lines.
        rows.order("ORX", [{"item": "ITEM-A", "qty": "1", "ext": "10", "cost": "3"}],
                   billed=({"item": "ITEM-Z", "qty": "1", "ext": "5", "cost": "1"},))
        cls.world = World(rows)

    @classmethod
    def tearDownClass(cls):
        cls.world.close()

    def ask(self, uniq: str, actor: Actor = FULL) -> dict:
        table = self.world.table()
        self.addCleanup(table["_reader"].conn.close)
        arguments, pages = {"family": "F31", "root": order_id(uniq)}, []
        while True:
            out = table["plx.investigate"]["handler"](arguments, actor)
            self.assertEqual(validate(out, OUTPUT, root=CONTRACTS), [], json.dumps(out)[:1500])
            self.assertNotIn("error", out, json.dumps(out)[:1500])
            pages.append(out)
            cursor = out["completeness"].get("next_cursor")
            if not cursor:
                break
            arguments = {**arguments, "cursor": cursor}
        merged = dict(pages[0])
        merged["answer"] = list({e["name"]: e for p in pages for e in p["answer"]}.values())
        return merged

    @staticmethod
    def named(out: dict) -> dict:
        return {e["name"]: e for e in out["answer"]}

    @staticmethod
    def codes(out: dict) -> set:
        return {str(g.get("reason")).upper() for g in out.get("gaps") or ()}

    def totals(self, out: dict) -> tuple:
        got = self.named(out)
        return tuple(got[n]["value"] for n in ("order_margin_revenue",
                                                "order_margin_booked_cost", "order_margin",
                                                "order_margin_margin_pct"))

    def test_an_invoiced_order_gets_a_cad_margin_from_cited_sage_cost_and_revenue(self):
        out = self.ask("1001")
        # A: (100 - 4) x 1 = 96 against 10 x 1 / 1 x 6 = 60; B: (40 - 1) = 39 against
        # 4 x 24 / 12 x 2.5 = 20. The freight charge is not goods.
        self.assertEqual(self.totals(out), ("135", "80", "55", "40.74"))
        got = self.named(out)
        self.assertEqual(got["order_margin"]["currency"], "CAD")
        self.assertEqual({(got[f"order_margin_line_{i}_revenue"]["value"],
                           got[f"order_margin_line_{i}_booked_cost"]["value"])
                          for i in (1, 2)}, {("96", "60"), ("39", "20")})
        self.assertIn("SAGE_CREDIT_NOTES_NOT_SERVED", self.codes(out))
        self.assertIn("SAGE_CHARGES_NOT_IN_MARGIN", self.codes(out))
        self.assertTrue(out["claim_checks"]["passed"], out["claim_checks"])
        # Every input is a cited Sage value of the same answer.
        for i in (1, 2):
            for name in got[f"order_margin_line_{i}_booked_cost"]["inputs"]:
                self.assertTrue(got[name]["citation"]["l1_handle"].startswith(
                    "api://sage.oe_"), name)

    def test_a_usd_invoice_is_converted_only_at_its_own_posted_rate(self):
        out = self.ask("1002")
        self.assertEqual(self.totals(out), ("62.5", "40", "22.5", "36"))
        steps = " ".join(self.named(out)["order_margin_line_1_revenue"]["steps"])
        self.assertIn("USD x posted rate 1.25", steps)

    def test_an_unproven_part_leaves_no_total_with_its_typed_gap(self):
        for uniq, code in (("1003", "SAGE_COST_NOT_VALUED"),
                           ("1004", "SAGE_INVOICE_LINES_INCOMPLETE"),
                           ("1006", "LINE_PARTIALLY_INVOICED"),
                           ("1007", "SAGE_RATE_UNPROVEN"),
                           ("1005", "LINE_NOT_INVOICED")):
            with self.subTest(order=uniq):
                out = self.ask(uniq)
                self.assertEqual(self.totals(out), (None,) * 4)
                self.assertIn(code, self.codes(out))

    def test_a_reader_without_finance_sees_no_figure(self):
        out = self.ask("1001", NO_FINANCE)
        self.assertEqual(self.totals(out), (None,) * 4)
        dumped = json.dumps(out)
        for figure in ('"135"', '"80"', '"96"', '"2.5"'):
            self.assertNotIn(figure, dumped)

    def test_1_10_50_60_lines_none_lost_or_partially_summed(self):
        # Reproduced (2026-11-02, live mirror): an order of some 40 item lines answered
        # result_too_large (every item's margin repeated on every page). Up to the plan's 50
        # objects per step every line is summed, over as many pages as it takes; past them
        # (60) a gap, never a sum of part of the lines.
        for uniq, count in (("1008", 1), ("1009", 10), ("1010", 50), ("1011", 60)):
            with self.subTest(lines=count):
                started = time.monotonic()
                out = self.ask(uniq)
                if count <= 50:
                    self.assertEqual(self.totals(out)[:3], (str(10 * count), str(4 * count),
                                                            str(6 * count)))
                    self.assertEqual(len([e for e in out["answer"] if e["name"].endswith(
                        "_booked_cost") and e["name"].startswith("order_margin_line_")
                        and e["value"] == "4"]), count)
                else:
                    self.assertEqual(self.totals(out), (None,) * 4)
                    self.assertIn("ORDER_LINES_INCOMPLETE", self.codes(out))
                self.assertLess(time.monotonic() - started, 120)

    def test_an_item_billed_on_no_order_line_keeps_the_output_contract(self):
        # Reproduced (2026-11-02, live mirror): such an item's entries named the item code
        # as their basis, not an object id, and the answer failed the output contract.
        out = self.ask("1012")             # ask() validates every page against it
        self.assertEqual(self.totals(out), (None,) * 4)
        self.assertIn("INVOICE_LINE_UNATTRIBUTED", self.codes(out))
        for entry in out["answer"]:
            for oid in entry.get("basis") or ():
                self.assertTrue(oid.startswith(("SalesOrderLine:", "SalesInvoiceLine:")), oid)

    def test_sage_ordered_quantity_is_the_original_order_quantity(self):
        # qtyordered is what is still outstanding; origqty what was ordered (BC's quantity).
        table = self.world.table()
        self.addCleanup(table["_reader"].conn.close)
        line = 'SalesOrderLine:portal.sage_oe_order_detail:{"_sage_pk":"1006|1"}'
        out = table["plx.object"]["handler"]({"object_id": line, "properties": [
            "ordered_quantity", "outstanding_quantity"]}, FULL)
        got = {v["property"]: v["value"] for v in out["values"]}
        self.assertEqual((Decimal(str(base.text(got["ordered_quantity"]))),
                          Decimal(str(base.text(got["outstanding_quantity"])))),
                         (Decimal(3), Decimal(2)))

    def test_customer_profitability_reads_a_sage_order_through_the_sage_plan(self):
        table = self.world.table()
        self.addCleanup(table["_reader"].conn.close)

        def call(name, arguments):
            return table[name]["handler"](arguments, FULL)
        order = cp.read_order(call, cp.Order(order_id("1001"), "OR1", []))
        self.assertEqual((order.revenue, order.cost, order.margin),
                         (Decimal("135"), Decimal("80"), Decimal("55")))
        self.assertEqual(order.currency, "CAD")
        self.assertIn("SAGE_CREDIT_NOTES_NOT_SERVED", {c for c, _d in order.gaps})
        # The why speaks of Sage's posted cost of sales, never a BC item ledger.
        breakdown = cp._breakdown(call, order, {})
        self.assertEqual({s["kind"] for s in breakdown["sources"]}, {"sage_cost_of_sales"})
        self.assertEqual(sum((s["amount"] for s in breakdown["sources"]), Decimal(0)),
                         Decimal("80"))
        words = json.dumps([breakdown["sources"], breakdown["gaps"]], default=str).lower()
        self.assertIn("cost of sales", words)
        for bc in ("ledger", "business central", "bc "):
            self.assertNotIn(bc, words)

    def test_customer_profitability_states_a_large_sage_order_past_its_page_reads(self):
        # Reproduced (2026-11-02): the reader read at most 8 more answer pages, so a Sage
        # order of more than about 7 item lines had no margin. Its figure is the sum the
        # first page states; the lines not read are said so.
        table = self.world.table()
        self.addCleanup(table["_reader"].conn.close)

        def call(name, arguments):
            return table[name]["handler"](arguments, FULL)
        order = cp.read_order(call, cp.Order(order_id("1010"), "ORN50", []))
        self.assertEqual((order.revenue, order.cost, order.margin, order.margin_pct),
                         (Decimal("500"), Decimal("200"), Decimal("300"), Decimal("60")))
        codes = {c for c, _d in order.gaps}
        self.assertIn("ORDER_LINES_NOT_ALL_SHOWN", codes)
        self.assertNotIn("ANSWER_PAGES_UNREAD", codes)
        shown = [ln for ln in order.lines if ln.margin is not None]
        self.assertTrue(0 < len(shown) < 50)
        self.assertTrue(all(ln.revenue - ln.cost == ln.margin for ln in shown))


if __name__ == "__main__":
    unittest.main()
