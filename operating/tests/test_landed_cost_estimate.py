"""Landed cost estimate mode (before receipt), at the engine boundary and through plx.investigate.

An open purchase order's landed cost is an estimate (ERP-D-015: the PO header is an estimate,
the receipt the actual): the header shipping estimate pro-rated over the ordered line value,
duty from the header duty estimate, a person's recorded duty-rate assumption or the Portal
default tariff rate as a labelled assumption beside a gap naming the missing input. Entries are
labelled estimate and kept apart from the actual landed cost; a header value read only through
an unconfirmed twin (``lead_basis``) never feeds a number; a reader without the finance marking
sees no amount. Evidence label: isolated-fixture (synthetic rows, never live PLX data).
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import re
import tempfile
import unittest

from ontology import store_ddl
from ontology.json_contract import validate
from ontology.types import Actor
from operating import landed_cost as lc
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
PO = "PurchaseOrder:1"
ARGS = {"roles": {r: r for r in lc.PLAN_ROLES}, "line_link": "PurchaseOrderLine.line_of",
        "item_link": "PurchaseOrderLine.item", "quote_link": "ForwarderQuote.for_purchase_order"}


def _entry(role, oid, value, **extra):
    return {"name": f"{role}_{oid.split(':')[-1]}", "object_id": oid, "value": value,
            "citation": {"l1_handle": f"fixture://{oid}", "field_path": role}, **extra}


def _assumption(**over):
    record = {"kind": "duty_rate", "rate": "0.04", "source": "broker email, HS 3923.30",
              "recorded_by": "person:finance-1", "recorded_at": "2099-01-01T09:00:00Z",
              "author_kind": "human", "case_id": "case-1", "record_id": "rec-1",
              "subject": PO}
    record.update(over)
    return record


def plan(lines, *, freight="100", freight_extra=None, duty=None, hs=(), currency="USD",
         assumptions=None, extra=None, complete=True, covers=True, rows_hidden=()):
    """One engine run over a purchase order whose ``lines`` are ``(price, ordered, received)``
    in USD per ea; ``hs``: the line indexes whose item cites an HS code."""
    evidence: dict = {"currency": [_entry("currency", PO, {"untrusted_text": currency})]}
    edges = []
    for i, (price, ordered, got) in enumerate(lines, start=1):
        oid = f"PurchaseOrderLine:{i}"
        edges.append({"link_type": "PurchaseOrderLine.line_of", "from": oid, "to": PO})
        edges.append({"link_type": "PurchaseOrderLine.item", "from": oid, "to": f"Item:{i}"})
        evidence.setdefault("unit_price", []).append(
            _entry("unit_price", oid, price, unit="ea", currency="USD"))
        evidence.setdefault("quantity", []).append(_entry("quantity", oid, ordered, unit="ea"))
        evidence.setdefault("received", []).append(_entry("received", oid, got, unit="ea"))
        if i in hs:
            evidence.setdefault("classification", []).append(
                _entry("classification", f"Item:{i}", {"untrusted_text": "3923.30"}))
    if freight is not None:
        evidence["freight_estimate"] = [_entry("freight_estimate", PO, freight, currency="USD",
                                               **(freight_extra or {}))]
    if duty is not None:
        evidence["duty_estimate"] = [_entry("duty_estimate", PO, duty, currency="USD")]
    for role, found in (extra or {}).items():
        evidence.setdefault(role, []).extend(found)
    rows: dict = {}
    for found in evidence.values():
        for e in found:
            rows.setdefault(e["citation"]["l1_handle"], {})[e["citation"]["field_path"]] = \
                e["value"]
    out = lc.plan_run(ARGS, evidence, edges, items={}, name="landed_cost", as_of="2099-01-02",
                      resolver=lambda h, actor: None if h in rows_hidden else rows[h],
                      actor=None, complete=complete, covers=lambda link: covers,
                      assumptions=assumptions)
    out["by_name"] = {e["name"]: e for e in out["entries"]}
    return out


def _gap(out, reason):
    return [g for g in out["gaps"] if g["reason"] == reason]


class EstimateEngineTests(unittest.TestCase):
    def test_an_unreceived_po_gets_a_per_line_estimate_from_the_header_freight(self):
        out = plan([("1.00", "100", "0"), ("3.00", "100", "0")])
        e = out["by_name"]
        # Freight 100 USD over ordered value 100 and 300: 25 and 75; duty at the Portal default
        # 6.5% of goods, labelled an assumption: (100 + 25 + 6.5) / 100 = 1.315 per ea.
        self.assertEqual(e["landed_cost_estimate_line_1"]["value"], "1.315")
        self.assertEqual(e["landed_cost_estimate_line_2"]["value"], "3.945")
        self.assertEqual(e["landed_cost_estimate_line_1"]["unit"], "USD/ea")
        self.assertEqual(e["landed_cost_estimate_subtotal"]["value"], "500")
        self.assertEqual(e["landed_cost_estimate"]["value"], "526")
        for name in ("landed_cost_estimate", "landed_cost_estimate_subtotal",
                     "landed_cost_estimate_line_1", "landed_cost_estimate_line_2"):
            self.assertEqual(e[name]["steps"][-1], lc.ESTIMATE_LABEL, name)
            self.assertEqual(e[name]["currency"], "USD")
        line = e["landed_cost_estimate_line_1"]
        self.assertTrue(any("freight_estimate_1" in s and "pro-rated by ordered line value" in s
                            for s in line["steps"]), line["steps"])
        self.assertIn("freight_estimate_1", line["inputs"])
        # Answer paging: a line entry the actual-cost lines also use (a price, a quantity) is
        # never an estimate input, else it is shared on every page and a long PO does not fit.
        actual_inputs = {n for entry in out["entries"] if "_estimate" not in entry["name"]
                         and entry["kind"] == "computed" for n in entry.get("inputs") or ()}
        for name in ("landed_cost_estimate_subtotal", "landed_cost_estimate_line_1",
                     "landed_cost_estimate_line_2"):
            self.assertFalse(set(e[name]["inputs"]) & actual_inputs - {"freight_estimate_1"},
                             e[name]["inputs"])
        self.assertTrue(any(s.startswith("goods: ") for s in line["steps"]), line["steps"])
        # Every assumption is listed and cited; the range runs from no duty to the default.
        total = e["landed_cost_estimate"]["steps"]
        self.assertTrue(any(s.startswith("assumption: duty at the Portal default tariffRate "
                                         "0.065 (portal/src/lib/costing/rate-masters.ts")
                            for s in total), total)
        self.assertIn("range: 500 USD (no duty) to 526 USD (duty at the Portal default "
                      "tariffRate)", total)
        # Kept apart from the actual landed cost, which stays unknown before receipt.
        self.assertIsNone(e["landed_cost"]["value"])
        self.assertIsNone(e["landed_cost_line_1"]["value"])
        self.assertNotIn(lc.ESTIMATE_LABEL, e["landed_cost_line_1"]["steps"])

    def test_with_no_duty_source_and_no_assumption_the_missing_input_is_named(self):
        out = plan([("1.00", "100", "0"), ("3.00", "100", "0")], hs=(1,))
        by = {(g["reason"], g["subject"]): g for g in out["gaps"]
              if g["subject"].startswith("estimate duty")}
        self.assertEqual(set(by), {
            ("duty_rate_source_absent", "estimate duty of landed_cost_estimate_line_1"),
            ("hs_code_not_cited", "estimate duty of landed_cost_estimate_line_2")})
        self.assertEqual(by[("duty_rate_source_absent",
                             "estimate duty of landed_cost_estimate_line_1")]["who_can_answer"],
                         ["FIN"])
        self.assertEqual(by[("hs_code_not_cited",
                             "estimate duty of landed_cost_estimate_line_2")]["who_can_answer"],
                         ["PROC"])
        self.assertIn("duty_rate_source_absent", out["reasons"])

    def test_a_recorded_duty_assumption_is_applied_and_listed(self):
        out = plan([("1.00", "100", "0"), ("3.00", "100", "0")], hs=(1, 2),
                   assumptions=[_assumption()])
        e = out["by_name"]
        # (100 + 25 + 4) / 100 and (300 + 75 + 12) / 100
        self.assertEqual(e["landed_cost_estimate_line_1"]["value"], "1.29")
        self.assertEqual(e["landed_cost_estimate_line_2"]["value"], "3.87")
        self.assertEqual(e["landed_cost_estimate"]["value"], "516")
        listed = [s for s in e["landed_cost_estimate"]["steps"] if s.startswith("assumption:")]
        self.assertEqual(len(listed), 1, listed)
        for part in ("duty rate 0.04", "person:finance-1", "2099-01-01T09:00:00Z", "case-1",
                     "rec-1", "broker email"):
            self.assertIn(part, listed[0])
        self.assertIn(listed[0], e["landed_cost_estimate_line_1"]["steps"])
        self.assertFalse([g for g in out["gaps"] if g["subject"].startswith("estimate duty ")])
        self.assertFalse(any("range:" in s for s in e["landed_cost_estimate"]["steps"]))

    def test_an_invalid_or_disagreeing_assumption_fails_closed(self):
        bad = [_assumption(author_kind="agent"), _assumption(rate="1.5"),
               _assumption(rate=0.04), _assumption(recorded_by=" "),
               _assumption(recorded_at="yesterday"), _assumption(kind="freight_rate"),
               _assumption(subject="PurchaseOrder:other"), _assumption(extra="x"), "x"]
        for record in bad:
            with self.subTest(record=record):
                if not isinstance(record, dict) or record["subject"] == PO:
                    with self.assertRaises(lc.LandedCostError):
                        lc.duty_assumption(record)
                out = plan([("1.00", "100", "0")], assumptions=[record])
                self.assertTrue(_gap(out, "assumption_record_invalid"))
                # not applied: the line carries the Portal default, the missing input named
                self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.065")
                self.assertTrue(_gap(out, "hs_code_not_cited"))
        two = plan([("1.00", "100", "0")],
                   assumptions=[_assumption(), _assumption(rate="0.05", record_id="rec-2")])
        self.assertTrue(_gap(two, "duty_assumptions_disagree"))
        self.assertEqual(two["by_name"]["landed_cost_estimate_line_1"]["value"], "2.065")

    def test_a_lead_basis_header_value_never_feeds_a_number(self):
        lead = {"lead_basis": True}
        out = plan([("1.00", "100", "0"), ("3.00", "100", "0")], freight_extra=lead)
        e = out["by_name"]
        self.assertEqual({e[n]["value"] for n in e if n.startswith("landed_cost")}, {None})
        [gap] = _gap(out, "po_header_on_unconfirmed_twin")
        self.assertEqual(gap["who_can_answer"], ["PROC"])
        self.assertTrue(_gap(out, "po_freight_not_recorded"))
        self.assertNotIn("freight_estimate_1", e["landed_cost_estimate_line_1"]["inputs"])
        # A currency read only on the twin: no target currency, so no amount anywhere.
        twin = {"currency": [dict(_entry("currency", "PurchaseOrder:twin",
                                         {"untrusted_text": "USD"}), lead_basis=True)]}
        out = plan([("1.00", "100", "0")], currency=None, extra=twin)
        self.assertEqual({e["value"] for e in out["entries"]}, {None})
        self.assertTrue(_gap(out, "po_header_on_unconfirmed_twin"))
        # Beside a confirmed value, a disagreeing twin is dropped, never a conflict or a pick.
        twin_freight = {"freight_estimate": [dict(_entry(
            "freight_estimate", "PurchaseOrder:twin", "900", currency="USD"), lead_basis=True)]}
        out = plan([("1.00", "100", "0"), ("3.00", "100", "0")], extra=twin_freight)
        self.assertEqual(out["by_name"]["landed_cost_estimate_subtotal"]["value"], "500")
        self.assertFalse(_gap(out, "po_estimate_records_disagree"))
        self.assertNotIn("po_header_on_unconfirmed_twin", out["reasons"])

    def test_a_received_po_has_no_estimate_and_the_actual_is_unchanged(self):
        # Every line received and a receipt actual read: the receipt is the actual, no estimate.
        receipt = {"receipt_freight": [_entry("receipt_freight", "Receipt:1", "40",
                                              currency="USD")]}
        out = plan([("1.00", "100", "100"), ("3.00", "100", "100")], extra=receipt)
        self.assertFalse([n for n in out["by_name"] if "estimate" in n])
        self.assertIsNotNone(out["by_name"]["landed_cost_line_1"])
        # No receipt read (the kept capture's fully received open orders): one null, labelled
        # estimate cites the receipt gap and who can give it, never no estimate entry at all.
        out = plan([("1.00", "100", "100"), ("3.00", "100", "100")])
        [name] = [n for n in out["by_name"] if "estimate" in n]
        entry = out["by_name"][name]
        self.assertEqual((name, entry["value"], entry["steps"][-1]),
                         ("landed_cost_estimate", None, lc.ESTIMATE_LABEL))
        self.assertTrue(entry["steps"][0].startswith("no estimate: RECEIPT_ACTUAL_NOT_READ "))
        [gap] = _gap(out, "receipt_actual_not_read")
        self.assertEqual(gap["who_can_answer"], ["WH"])
        self.assertIsNotNone(out["by_name"]["landed_cost_line_1"])

    def test_a_partial_line_set_or_unpriced_line_names_the_input(self):
        for kwargs, reason in (({"complete": False}, "estimate_lines_incomplete"),
                               ({"covers": False}, "estimate_read_scope_unproven")):
            with self.subTest(kwargs=kwargs):
                out = plan([("1.00", "100", "0")], **kwargs)
                self.assertEqual({e["value"] for e in out["entries"]}, {None})
                self.assertTrue(_gap(out, reason))
        # A reader whose scope is not proven lacks a grant, not an input: no person is named.
        [gap] = _gap(plan([("1.00", "100", "0")], covers=False), "estimate_read_scope_unproven")
        self.assertNotIn("who_can_answer", gap)
        # An open purchase order with no line reached still states (null) estimate entries;
        # they cite the run's one PO_LINES_INCOMPLETE gap (who can give the lines), never a
        # second gap for the same cause.
        out = plan([])
        self.assertIsNone(out["by_name"]["landed_cost_estimate"]["value"])
        self.assertEqual(out["by_name"]["landed_cost_estimate"]["steps"],
                         ["no estimate: PO_LINES_INCOMPLETE (no line of the purchase order was "
                          "reached; see the gaps)", lc.ESTIMATE_LABEL])
        self.assertEqual(_gap(out, "estimate_lines_incomplete"), [])
        [gap] = _gap(out, "po_lines_incomplete")
        self.assertIn("no line of the purchase order was reached", gap["detail"])
        out = plan([(None, "100", "0"), ("3.00", "100", "0")])
        [gap] = _gap(out, "estimate_lines_incomplete")
        self.assertIn("1 of 2 line(s) were read with no unit price", gap["detail"])
        self.assertEqual(gap["who_can_answer"], ["PROC"])
        out = plan([("0", "100", "0"), ("3.00", "100", "0")])
        self.assertTrue(_gap(out, "estimate_line_value_uncited"))
        out = plan([("1.00", "100", "0")], freight=None)
        self.assertIsNone(out["by_name"]["landed_cost_estimate_subtotal"]["value"])
        self.assertTrue([g for g in _gap(out, "po_freight_not_recorded")
                         if g["subject"] == "estimate freight"])
        # A cited input the reader cannot resolve leaves that line (and the PO) unknown.
        out = plan([("1.00", "100", "0"), ("3.00", "100", "0")],
                   rows_hidden={"fixture://PurchaseOrder:1"})
        self.assertEqual({e["value"] for e in out["entries"]}, {None})

    def test_a_usable_quote_total_is_the_freight_when_the_header_has_none(self):
        quote = {"quote_amount": [_entry("quote_amount", "ForwarderQuote:1", "200",
                                         currency="USD")],
                 "quote_basis": [_entry("quote_basis", "ForwarderQuote:1",
                                        {"untrusted_text": "per shipment"})],
                 "quote_scope": [_entry("quote_scope", "ForwarderQuote:1",
                                        {"untrusted_text": "door to door"})],
                 "quote_excludes": [_entry("quote_excludes", "ForwarderQuote:1",
                                           {"untrusted_text": "duties and taxes"})],
                 "quote_valid_until": [_entry("quote_valid_until", "ForwarderQuote:1",
                                              {"untrusted_text": "2099-02-01"})]}
        out = plan([("1.00", "100", "0"), ("3.00", "100", "0")], freight=None, extra=quote)
        quoted = out["by_name"]["landed_cost_freight_quote_1"]["value"]
        self.assertEqual(quoted, "200")
        self.assertEqual(out["by_name"]["landed_cost_estimate_subtotal"]["value"], "600")
        self.assertIn("quote_amount_1", out["by_name"]["landed_cost_estimate_line_1"]["inputs"])

    def test_shares_sum_to_the_header_at_1_10_and_60_lines(self):
        for n in (1, 10, 60):
            with self.subTest(lines=n):
                lines = [(f"{1 + i % 7}.13", str(10 + i), "0") for i in range(n)]
                out = plan(lines, freight="1000.01", assumptions=[_assumption(rate="0")])
                e = out["by_name"]
                names = [f"landed_cost_estimate_line_{i}" for i in range(1, n + 1)]
                self.assertEqual(sorted(k for k in e if k.startswith("landed_cost_estimate_line")),
                                 sorted(names))
                goods = sum(Decimal(p) * Decimal(q) for p, q, _ in lines)
                self.assertEqual(Decimal(e["landed_cost_estimate_subtotal"]["value"]),
                                 goods + Decimal("1000.01"))
                self.assertEqual(Decimal(e["landed_cost_estimate"]["value"]),
                                 goods + Decimal("1000.01"))
                for entry in out["entries"]:
                    self.assertLessEqual(len(entry.get("steps") or ()), 40)
                    self.assertLessEqual(len(entry.get("inputs") or ()), 50)
        self.assertEqual(lc.prorate(Decimal("100"), [Decimal(1)] * 3),
                         [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")])
        for bad in ([], [Decimal(0)], [Decimal(-1)]):
            with self.assertRaises(lc.LandedCostError):
                lc.prorate(Decimal("1"), bad)


# ---- through plx.investigate: money visibility -------------------------------------------

UPDATED = "2099-01-01T12:00:00Z"
AT = "2099-01-02T04:00:00Z"


def _relations() -> dict:
    def po(pid, number, shipping):
        return {"id": pid, "po_number": number, "status": "OPEN", "supplier_id": "fx-s1",
                "currency": "USD", "shipping_cost": shipping, "duties_cost": None,
                "incoterms": "FOB", "custom_fields": {}, "updated_at": UPDATED}

    def line(lid, pid, product, qty, received, price):
        return {"id": lid, "purchase_order_id": pid, "product_id": product, "quantity": qty,
                "quantity_received": received, "unit_price": price, "uom": "ea",
                "updated_at": UPDATED}
    return {
        "public.app_supplier": [{"id": "fx-s1", "code": "FXS1", "name": "FX Supplier",
                                 "currency": "USD", "updated_at": UPDATED}],
        "public.app_product": [
            {"id": "fx-p-1", "code": "FX-1", "hs_code": "3923.30", "updated_at": UPDATED},
            {"id": "fx-p-2", "code": "FX-2", "hs_code": None, "updated_at": UPDATED}],
        "public.app_purchase_order": [po("fx-po-o", "PO300001", "437.0")],
        "public.app_purchase_order_line": [
            line("fx-pol-o-1", "fx-po-o", "fx-p-1", "100.0", "0.0", "1.0"),
            line("fx-pol-o-2", "fx-po-o", "fx-p-2", "100.0", "0.0", "3.0")],
        "public.app_receipt": [],
    }


class InvestigateEstimateTests(unittest.TestCase):
    PRINCIPAL = "eval:dev-baseline"

    @classmethod
    def setUpClass(cls) -> None:
        from evals.oracle.fixture_world import World
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from operating.case_runtime import CaseRuntime
        from scripts import eval_dev_baseline as script
        from scripts.eval_unfamiliar_sample import _fixture_fetch
        relations = _relations()
        cls.tmp = tempfile.TemporaryDirectory(prefix="plx-estimate-test-")
        client = WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                        fetch=_fixture_fetch(World(relations)))
        cls.product = script.build_product(client, Path(cls.tmp.name) / "w", sorted(relations),
                                           budget_seconds=None, clock=script.fixture_clock)
        cls.runtime = CaseRuntime(cls.product.state_dir, client=None,
                                  audience=cls.product.audience, tenant=script.TENANT)
        cls.tenant = script.TENANT
        cls.actor = Actor(script.TENANT, frozenset({cls.PRINCIPAL}))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.runtime.close()
        cls.tmp.cleanup()

    def investigate(self, markings, assumptions=None) -> dict:
        reader = ot.StoreReader.open(self.product.store)
        self.addCleanup(reader.conn.close)
        who = {self.PRINCIPAL}
        policy = ot.ReadPolicy(tenant=self.tenant,
                               audiences={k: set(who) for k in reader.audience_keys()},
                               markings={m: set(who) for m in markings})
        base = ot.OntologyTools(reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(
            table, rows=oi.citation_rows(self.runtime), scope=oi.read_scope(base),
            assumptions=assumptions))
        result = table["plx.investigate"]["handler"](
            {"family": "landed_cost", "root": {"identifier": "PO300001"}, "as_known_at": AT},
            self.actor)
        self.assertEqual(validate(result, CONTRACTS["tools"]["plx.investigate"]["output"],
                                  root=CONTRACTS), [])
        return result

    def test_the_open_po_estimate_and_a_reader_without_money_visibility(self):
        full = self.investigate(store_ddl.MARKINGS)
        e = {x["name"]: x for x in full["answer"] if x["kind"] == "computed"}
        # 437 over ordered value 100 and 300: 109.25 and 327.75; Portal default duty 6.5%.
        self.assertEqual(e["landed_cost_estimate_subtotal"]["value"], "837")
        self.assertEqual(e["landed_cost_estimate_line_1"]["value"], "2.1575")
        self.assertEqual(e["landed_cost_estimate"]["value"], "863")
        self.assertIsNone(e["landed_cost"]["value"])
        self.assertTrue(full["claim_checks"]["passed"], full["claim_checks"])
        duty = {g["reason"]: g["who_can_answer"] for g in full["gaps"]
                if g["subject"].startswith("estimate duty")}
        self.assertEqual(duty, {"duty_rate_source_absent": ["FIN"],
                                "hs_code_not_cited": ["PROC"]})
        # Without the finance marking: no amount, in any entry, step or gap.
        blind = self.investigate(())
        text = json.dumps(blind)
        for amount in ("437", "109.25", "837", "863", "2.1575"):
            # as a number, never inside a digest or an id (hex can hold the digits)
            self.assertIsNone(re.search(rf"(?<![0-9A-Za-z.]){re.escape(amount)}(?![0-9A-Za-z])",
                                        text), amount)
        computed = [x for x in blind["answer"] if x["kind"] == "computed"]
        self.assertTrue(any(x["name"] == "landed_cost_estimate" for x in computed))
        self.assertEqual({x["value"] for x in computed}, {None})

    def test_a_recorded_assumption_reaches_the_engine_through_the_executor(self):
        root = {}

        def provider(root_id, actor):
            root["id"] = root_id
            return [_assumption(subject=root_id, rate="0.1")]
        result = self.investigate(store_ddl.MARKINGS, assumptions=provider)
        e = {x["name"]: x for x in result["answer"] if x["kind"] == "computed"}
        self.assertEqual(e["landed_cost_estimate"]["value"], "877")  # 837 + 10% of 400
        self.assertTrue(any("duty rate 0.1" in s for s in e["landed_cost_estimate"]["steps"]))

        def broken(root_id, actor):
            raise RuntimeError("store down")
        result = self.investigate(store_ddl.MARKINGS, assumptions=broken)
        self.assertIn("assumptions_unreadable", {g["reason"] for g in result["gaps"]})
        e = {x["name"]: x for x in result["answer"] if x["kind"] == "computed"}
        self.assertEqual(e["landed_cost_estimate"]["value"], "863")


class LongOpenOrderPagingTests(unittest.TestCase):
    """A long open order whose items carry no units per case (the shape a kept capture's
    ``result_too_large`` orders had: one empty cell per item, read by no line entry): the
    answer pages within the result cap, every entry on exactly one page, never refused. At
    1, 20 and 60 lines (past the plan's 50-line read, the estimate names the cut)."""

    PRINCIPAL = "eval:dev-baseline"

    def test_every_entry_reaches_the_reader_once_within_the_cap(self):
        for n in (1, 20, 60):
            with self.subTest(lines=n):
                self.check_long_order(n)

    def check_long_order(self, n: int) -> None:
        from evals.oracle.fixture_world import World
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from operating.case_runtime import CaseRuntime
        from scripts import eval_dev_baseline as script
        from scripts.eval_unfamiliar_sample import _fixture_fetch

        relations = _relations()
        relations["public.app_product"] = [
            {"id": f"fx-p-{i}", "code": f"FX-{i}", "hs_code": "3923.30", "units_per_box": None,
             "updated_at": UPDATED} for i in range(n)]
        relations["public.app_purchase_order_line"] = [
            {"id": f"fx-pol-o-{i}", "purchase_order_id": "fx-po-o", "product_id": f"fx-p-{i}",
             "quantity": "100.0", "quantity_received": "0.0", "unit_price": "1.0", "uom": "ea",
             "updated_at": UPDATED} for i in range(n)]
        tmp = tempfile.TemporaryDirectory(prefix="plx-estimate-long-")
        self.addCleanup(tmp.cleanup)
        client = WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                        fetch=_fixture_fetch(World(relations)))
        product = script.build_product(client, Path(tmp.name) / "w", sorted(relations),
                                       budget_seconds=None, clock=script.fixture_clock)
        runtime = CaseRuntime(product.state_dir, client=None, audience=product.audience,
                              tenant=script.TENANT)
        self.addCleanup(runtime.close)
        reader = ot.StoreReader.open(product.store)
        self.addCleanup(reader.conn.close)
        who = {self.PRINCIPAL}
        policy = ot.ReadPolicy(tenant=script.TENANT,
                               audiences={k: set(who) for k in reader.audience_keys()},
                               markings={m: set(who) for m in store_ddl.MARKINGS})
        base = ot.OntologyTools(reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(table, rows=oi.citation_rows(runtime),
                                               scope=oi.read_scope(base)))
        actor = Actor(script.TENANT, frozenset(who))
        args = {"family": "landed_cost", "root": {"identifier": "PO300001"}, "as_known_at": AT}
        seen: dict[str, int] = {}
        cursor, pages = None, 0
        while True:
            page = table["plx.investigate"]["handler"](
                {**args, **({"cursor": cursor} if cursor else {})}, actor)
            self.assertNotIn("error", page, (pages, page.get("error")))
            self.assertEqual(validate(page, CONTRACTS["tools"]["plx.investigate"]["output"],
                                      root=CONTRACTS), [])
            self.assertLessEqual(ot._envelope_bytes(page), base.limits["max_result_bytes"])
            pages += 1
            gaps = page["gaps"]
            for entry in page["answer"]:
                seen[entry["name"]] = seen.get(entry["name"], 0) + 1
            cursor = page["completeness"].get("next_cursor")
            if not cursor:
                break
            self.assertLess(pages, 200)
        self.assertEqual(pages > 1, n > 1, pages)
        read = min(n, 50)  # the plan reads 50 lines; past them the run is incomplete
        # Each line's estimate, its landed cost line and its item's empty units-per-case cell
        # on exactly one page; nothing lost or repeated.
        once = [f"landed_cost_estimate_line_{i}" for i in range(1, read + 1)] + \
            [f"landed_cost_line_{i}" for i in range(1, read + 1)]
        self.assertEqual({name: seen.get(name) for name in once}, dict.fromkeys(once, 1))
        cells = [name for name in seen if name.startswith("units_per_box")]
        self.assertEqual((len(cells), {seen[name] for name in cells}), (read, {1}), seen)
        for name in ("landed_cost_estimate", "landed_cost_estimate_subtotal", "landed_cost"):
            self.assertIn(name, seen)
        if n > 50:
            named = {g["reason"]: g.get("who_can_answer") for g in gaps}
            self.assertEqual(named.get("estimate_lines_incomplete"), ["PROC"], named)


class HeaderCurrencyFormTests(unittest.TestCase):
    """The Portal writes a purchase order's currency as an ISO code with its sign attached
    (the measured live shape ``AAA$``); the header reads it through ``currency_code``, the
    ontology's one rule, and states the rule. Synthetic values only."""

    def test_a_code_with_its_own_sign_attached_is_the_target_currency(self):
        for written in ("USD$", "$USD", "usd$", "US$"):
            with self.subTest(written=written):
                out = plan([("1.00", "100", "0"), ("3.00", "100", "0")], currency=written)
                self.assertFalse(_gap(out, "currency_not_iso_4217"), out["gaps"])
                e = out["by_name"]["landed_cost_estimate"]
                self.assertEqual(e["value"], "526")
                self.assertEqual(e["steps"][-1], lc.ESTIMATE_LABEL)
                rule = "sign_form" if written == "US$" else "sign_attached"
                self.assertTrue(any(s.startswith("currency: ") and rule in s and "USD" in s
                                    for s in e["steps"]), e["steps"])
                self.assertTrue(any(s.startswith("currency: ") for s in
                                    out["by_name"]["landed_cost"]["steps"]))

    def test_a_plain_code_states_no_rule(self):
        out = plan([("1.00", "100", "0")], currency="USD")
        self.assertFalse(any(s.startswith("currency: ") for e in out["entries"]
                             for s in e.get("steps") or ()))

    def test_one_currency_in_two_written_forms_is_one_currency(self):
        twin = {"currency": [_entry("currency", PO, {"untrusted_text": "USD"})]}
        out = plan([("1.00", "100", "0")], currency="USD$", extra=twin)
        self.assertFalse(_gap(out, "mixed_currency_no_cited_rate"), out["gaps"])
        self.assertIsNotNone(out["by_name"]["landed_cost_estimate"]["value"])

    def test_a_form_no_rule_reads_stays_unknown(self):
        for written in ("$", "XYZ$", "USD\u20ac", "dollars"):
            with self.subTest(written=written):
                out = plan([("1.00", "100", "0")], currency=written)
                self.assertTrue(_gap(out, "currency_not_iso_4217"), out["gaps"])
                self.assertIsNone(out["by_name"]["landed_cost_estimate"]["value"])


if __name__ == "__main__":
    unittest.main()
