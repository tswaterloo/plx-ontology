"""The landed-cost estimate wired to its real inputs (directive eff2eb4c2451).

The ontology projects the FileMaker supplier row's ``tariff_c`` (``SupplierItem.tariff_rate``)
and the Sage receipt header's ``currency`` (``Receipt.currency``); the ``landed_cost`` plan
walks from the purchase order to its own supplier's row for the line's item, so an open
order's estimate states its duty at that row's rate as a labelled assumption. FileMaker's
``dlt_costlanded_c`` lives per tier (``tierpricing``): the tier is the one with the highest
minimum quantity at or below the line's ordered quantity, and the agreement is counts only,
labelled as FileMaker's recorded figure. An empty ``tariff_c`` is absent, not a gap. A
reader who cannot read a row rate sees no step stating it. The live check's pass condition
compares its counts with a tally read independently from the raw answers.

Evidence label: isolated-fixture (synthetic rows, never live PLX data); the projected test
runs the real projector over an injected-transport fixture world.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
import re
import tempfile
import unittest

from operating import landed_cost as lc
from operating.tests.test_landed_cost_real_inputs import (
    ARGS, PO, TWO, _entry, _estimate_gap, _gap, _reading, _text, plan)
from scripts import eval_packaging_oracle_check as check

TIER_LINK = "SupplierTier.tier_of"
WIRED = {**ARGS, "tier_link": TIER_LINK}
COUNTS = re.compile(r"counts: agree (\d+), differ (\d+), missing (\d+) .*")


def order(lines, *, tiers=None, tier_edges=None, args=None, extra=None, **kwargs):
    """``plan`` over ``lines`` with FileMaker tiers: ``tiers`` maps a line index to its row's
    tiers ``(min quantity, unit, dlt_costlanded_c)``, each ``SupplierTier:<i>-<k>`` linked to
    ``SupplierItem:<i>`` by ``tier_of`` (``tier_edges``: extra fields on those edges)."""
    extra = {role: list(found) for role, found in (extra or {}).items()}
    edges = list((args or WIRED).get("_extra_edges", ()))
    for i, found in (tiers or {}).items():
        for k, (least, unit, figure) in enumerate(found, start=1):
            tier = f"SupplierTier:{i}-{k}"
            edges.append({"link_type": TIER_LINK, "from": tier, "to": f"SupplierItem:{i}",
                          **(tier_edges or {})})
            extra.setdefault("fm_tier_quantity", []).append(
                _entry("fm_tier_quantity", tier, least, unit=unit))
            if figure is not None:
                extra.setdefault("fm_landed_cost", []).append(
                    _entry("fm_landed_cost", tier, figure, unit="ea", currency="USD"))
    return plan(lines, extra=extra, args={**(args or WIRED), "_extra_edges": edges}, **kwargs)


def agreement(out):
    entry = out["by_name"]["landed_cost_fm_agreement"]
    match = COUNTS.fullmatch(entry["steps"][-1])
    assert match, entry["steps"]
    return tuple(int(x) for x in match.groups()), entry


TIERS = [("1", "ea", "9.99"), ("200", "ea", "1.44"), ("500", "ea", "0.50")]


class OpenOrderEstimateTest(unittest.TestCase):
    def test_an_open_po_gets_a_labelled_estimate_and_the_tier_its_quantity_reaches(self):
        # 250 ea at 1.00, freight 100, the row's tariff_c 4%: (250 + 100 + 10) / 250 = 1.44.
        out = order([("1.00", "250", "0")], tariff={1: "4%"}, hs={1: "3923.30"},
                    origin={1: "CN"}, tiers={1: TIERS})
        e = out["by_name"]
        self.assertEqual(e["landed_cost_estimate_line_1"]["value"], "1.44")
        self.assertEqual(e["landed_cost_estimate"]["value"], "360")
        reading = _reading(out)
        self.assertTrue(reading["estimate"] and reading["labelled"])
        self.assertEqual(reading["assumptions"], ["duty_supplier_row_tariff"])
        for name in ("supplier_tariff_1", "supplier_hs_code_1", "supplier_origin_1"):
            self.assertIn(name, e["landed_cost_estimate_line_1"]["inputs"])
        counts, entry = agreement(out)
        self.assertEqual(counts, (1, 0, 0))
        self.assertEqual(reading["fm_agreement"], {"agree": 1, "differ": 0, "missing": 0})
        self.assertIsNone(entry["value"])
        self.assertTrue(entry["steps"][0].startswith("landed_cost_line_1: agree "), entry)
        self.assertIn("fm_landed_cost_1-2", entry["steps"][0])
        self.assertIn("recorded", entry["steps"][0])
        self.assertIn("fm_landed_cost_1-2", entry["inputs"])
        self.assertIn("SupplierTier:1-2", entry["basis"])
        for step in entry["steps"]:
            self.assertNotIn("(actual)", step)
            self.assertFalse([a for a in ("9.99", "1.44", "0.50", "360") if a in step], step)
        # The tier is chosen by the ordered quantity: the highest minimum at or below it.
        for ordered, tier in (("100", "1-1"), ("199", "1-1"), ("200", "1-2"), ("499", "1-2"),
                              ("500", "1-3"), ("600", "1-3")):
            with self.subTest(ordered=ordered):
                _, entry = agreement(order([("1.00", ordered, "0")], tariff={1: "4%"},
                                           tiers={1: TIERS}))
                self.assertIn(f"fm_landed_cost_{tier}", entry["steps"][0])
                self.assertEqual([n for n in entry["inputs"] if n.startswith("fm_landed")],
                                 [f"fm_landed_cost_{tier}"])

    def test_a_received_order_compares_its_landed_cost_never_labelled_actual(self):
        posted = {role: [_entry(role, "Receipt:1", value, currency="USD")]
                  for role, value in (("receipt_freight", "100"), ("receipt_duty", "6.5"))}
        out = order([("1.00", "100", "100")], tiers={1: [("1", "ea", "2.07")]}, extra=posted)
        self.assertEqual(out["by_name"]["landed_cost_line_1"]["value"], "2.065")
        counts, entry = agreement(out)
        self.assertEqual(counts, (1, 0, 0))
        self.assertIn("recorded", entry["steps"][0])
        self.assertFalse(any("(actual)" in s for s in entry["steps"]), entry["steps"])

    def test_a_tier_that_cannot_be_chosen_for_certain_is_missing(self):
        one = [("1.00", "250", "0")]
        cases = {
            "tier quantity in another unit": dict(tiers={1: [("1", "kg", "1.44")]}),
            "tiers in mixed units": dict(tiers={1: [("1", "ea", "1.44"),
                                                    ("100", "kg", "1.44")]}),
            "same minimum, different figures": dict(tiers={1: [("200", "ea", "1.44"),
                                                               ("200", "ea", "1.50")]}),
            "no tier at or below the quantity": dict(tiers={1: [("300", "ea", "1.44")]}),
            "a tier quantity that is text": dict(tiers={1: [(_text("two hundred"), "ea",
                                                             "1.44")]}),
            "the chosen tier records no figure": dict(tiers={1: [("1", "ea", "1.44"),
                                                                 ("200", "ea", None)]}),
            "a tier quantity hidden from the reader": dict(
                tiers={1: TIERS}, rows_hidden={"fixture://SupplierTier:1-1/fm_tier_quantity"}),
            "the chosen figure hidden from the reader": dict(
                tiers={1: TIERS}, rows_hidden={"fixture://SupplierTier:1-2/fm_landed_cost"}),
            "a tier joined only by a lead-tier link": dict(tiers={1: TIERS},
                                                           tier_edges={"tier": "lead"}),
            "another supplier's row": dict(tiers={1: TIERS}, row_supplier="Supplier:other"),
            "an order whose supplier is unread": dict(tiers={1: TIERS}, po_supplier=False),
        }
        for why, kwargs in cases.items():
            with self.subTest(why):
                out = order(one, tariff={1: "4%"}, **kwargs)
                counts, entry = agreement(out)
                self.assertEqual(counts, (0, 0, 1), entry["steps"])
                self.assertTrue(entry["steps"][0].startswith("landed_cost_line_1: missing ("))
                self.assertFalse([a for a in ("1.44", "1.50", "9.99") if a in
                                  " ".join(entry["steps"])])
        # A plan naming no tier link reaches no tier: missing, never the row read as a tier.
        out = order(one, tariff={1: "4%"}, tiers={1: TIERS}, args=ARGS)
        self.assertEqual(agreement(out)[0], (0, 0, 1))

    def test_agreement_counts_at_1_10_and_60_lines_lose_and_duplicate_nothing(self):
        for n in (1, 10, 60):
            with self.subTest(lines=n):
                lines = [("1.00", "100", "0")] * n
                # freight 100 * n over n equal lines: (100 + 100 + 6.5) / 100 = 2.065 each.
                tiers = {i: [("1", "ea", "2.065" if i % 3 else "9")] for i in range(1, n + 1)}
                if n > 1:
                    tiers[n] = [("101", "ea", "2.065")]      # none at or below: missing
                out = order(lines, freight=str(100 * n), tiers=tiers)
                counts, entry = agreement(out)
                differ = sum(1 for i in range(1, n) if i % 3 == 0) + (n == 1 and 0)
                missing = 1 if n > 1 else 0
                self.assertEqual(sum(counts), n)
                self.assertEqual(counts, (n - differ - missing, differ, missing))
                self.assertEqual(_reading(out)["fm_agreement"],
                                 dict(zip(("agree", "differ", "missing"), counts)))
                self.assertLessEqual(len(entry["steps"]), 40)
                self.assertLessEqual(len(entry["inputs"]), 50)


class EmptyTariffTest(unittest.TestCase):
    def test_an_empty_tariff_c_is_absent_not_a_gap(self):
        for value in ("", "   ", _text(""), _text("  ")):
            with self.subTest(value=value):
                out = plan([("1.00", "100", "0")], tariff={1: value}, hs={1: "3923.30"})
                self.assertFalse(_gap(out, "supplier_tariff_unreadable"))
                line = out["by_name"]["landed_cost_estimate_line_1"]
                self.assertEqual(line["value"], "2.065")
                self.assertNotIn("supplier_tariff_1", line["inputs"])
                # The row states no rate: Finance gives one, as for any HS code without a rate.
                [gap] = _estimate_gap(out, "duty_rate_source_absent")
                self.assertEqual(gap["who_can_answer"], ["FIN"])
        # An empty row beside a readable one of the same supplier: the readable rate stands.
        extra = {"supplier_tariff": [_entry("supplier_tariff", "SupplierItem:1b", _text(""))]}
        out = plan([("1.00", "100", "0")], tariff={1: "4%"}, extra=extra,
                   args={**ARGS, "_extra_edges": [
                       {"link_type": "SupplierItem.for_item", "from": "SupplierItem:1b",
                        "to": "Product:fm-1"},
                       {"link_type": "SupplierItem.offered_by", "from": "SupplierItem:1b",
                        "to": "Supplier:fm"}]})
        self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.04")
        self.assertFalse(_gap(out, "supplier_tariff_records_disagree"))
        # Not empty, still not a rate: unreadable as before.
        out = plan([("1.00", "100", "0")], tariff={1: "n/a"})
        self.assertTrue(_gap(out, "supplier_tariff_unreadable"))


class RestrictedReaderTest(unittest.TestCase):
    def _no_step_states(self, out, *texts):
        for entry in out["entries"]:
            for step in entry.get("steps") or ():
                for text in texts:
                    self.assertNotIn(text, step, entry["name"])

    def test_an_unreadable_row_rate_is_never_stated_in_any_step(self):
        out = plan(TWO, tariff={1: "4%", 2: "4%"},
                   rows_hidden={"fixture://SupplierItem:1/supplier_tariff"})
        e = out["by_name"]
        self.assertIsNone(e["landed_cost_estimate_line_1"]["value"])
        self.assertIsNone(e["landed_cost_estimate"]["value"])
        self.assertEqual(e["landed_cost_estimate_line_2"]["value"], "3.87")
        self._no_step_states(out, "supplier_tariff_1 0.04", "supplier_tariff_1 4")
        line_1 = " ".join(e["landed_cost_estimate_line_1"]["steps"])
        self.assertNotIn("0.04", line_1)
        self.assertNotIn("duty: 4 ", line_1)
        self.assertNotIn("supplier_tariff_1", e["landed_cost_estimate_line_1"]["inputs"])
        [gap] = _gap(out, "supplier_tariff_unread")
        self.assertIn("supplier_tariff_1", gap["detail"])
        self.assertNotIn("0.04", gap["detail"])
        self.assertNotIn("4%", gap["detail"])
        self.assertEqual(gap["who_can_answer"], ["FIN"])
        self.assertIn("landed_cost_estimate_line_1", gap["subject"])
        # The readable line keeps its own rate, and the listed assumption names only it.
        listed = [s for s in e["landed_cost_estimate_line_2"]["steps"]
                  if s.startswith("assumption: duty at the FileMaker supplier row tariff")]
        self.assertEqual(len(listed), 1)
        self.assertIn("supplier_tariff_2 0.04", listed[0])
        self.assertIn("on 1 of 2 line(s)", listed[0])

    def test_an_unreadable_rate_setting_is_never_stated_in_any_step(self):
        setting = {"rate_setting_tariff": [_entry("rate_setting_tariff", "RateSettings:1",
                                                  "0.05")]}
        out = plan([("1.00", "100", "0")], extra=setting,
                   rows_hidden={"fixture://RateSettings:1/rate_setting_tariff"})
        self.assertIsNone(out["by_name"]["landed_cost_estimate_line_1"]["value"])
        self._no_step_states(out, "0.05")
        for gap in out["gaps"]:
            self.assertNotIn("0.05", gap["detail"])
        [gap] = _gap(out, "rate_setting_tariff_unread")
        self.assertEqual(gap["who_can_answer"], ["FIN"])


class OwnOrderOnlyTest(unittest.TestCase):
    def test_a_row_of_another_orders_supplier_is_never_this_lines(self):
        # Line 1 is also linked to purchase order 2, whose supplier offers a 9% row for the
        # item: purchase order 2 is not this order, so its supplier's row is never read.
        for tier in ("lead", "verified"):
            with self.subTest(tier=tier):
                args = {**ARGS, "_extra_edges": [
                    {"link_type": "PurchaseOrderLine.line_of", "from": "PurchaseOrderLine:1",
                     "to": "PurchaseOrder:2", "tier": tier},
                    {"link_type": "PurchaseOrder.ordered_from", "from": "PurchaseOrder:2",
                     "to": "Supplier:x"},
                    {"link_type": "SupplierItem.for_item", "from": "SupplierItem:x",
                     "to": "Product:fm-1"},
                    {"link_type": "SupplierItem.offered_by", "from": "SupplierItem:x",
                     "to": "Supplier:x"}]}
                extra = {"supplier_tariff": [_entry("supplier_tariff", "SupplierItem:x",
                                                    _text("9%"))]}
                out = plan([("1.00", "100", "0")], po_supplier=False, extra=extra, args=args)
                line = out["by_name"]["landed_cost_estimate_line_1"]
                self.assertEqual(line["value"], "2.065")
                self.assertNotIn("supplier_tariff_x", line["inputs"])
                self.assertNotIn("SupplierItem:x", line["basis"])
        # The order's own supplier's row still applies.
        out = plan([("1.00", "100", "0")], tariff={1: "4%"})
        self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.04")


class LiveCheckPassConditionTest(unittest.TestCase):
    """``estimate_ok``: the live check's pass condition compares its partition with a tally
    read independently from the raw answers, so it can fail."""

    @staticmethod
    def results():
        return [{"answer": copy.deepcopy(out["entries"]), "gaps": copy.deepcopy(out["gaps"])}
                for out in (plan(TWO, tariff={1: "4%", 2: "4%"}, hs={1: "x", 2: "x"}),
                            plan(TWO), plan(TWO, freight=None))]

    @staticmethod
    def run_of(readings, results):
        return {"readings": readings, "listed": 3, "listing": "complete", "errors": [],
                "tally": check.answer_tally(results)}

    def test_the_sum_check_fails_when_an_input_is_changed(self):
        results = self.results()
        readings = [check.estimate_reading(r) for r in results]
        run = self.run_of(readings, results)
        summary = check.estimate_summary(run)
        self.assertEqual((summary["with_estimate"], summary["with_named_missing_input"]), (2, 1))
        self.assertTrue(check.estimate_ok(summary, run))
        # A reading that lost its estimate: the partition still adds up, the tally does not.
        flipped = [dict(readings[0], estimate=False)] + readings[1:]
        run = self.run_of(flipped, results)
        self.assertFalse(check.estimate_ok(check.estimate_summary(run), run))
        # A raw answer changed under the same readings: its estimate is no amount.
        for value in (None, "not a number", "NaN"):
            with self.subTest(value=value):
                changed = copy.deepcopy(results)
                total = next(e for e in changed[0]["answer"]
                             if e["name"] == "landed_cost_estimate")
                total["value"] = value
                run = self.run_of(readings, changed)
                self.assertFalse(check.estimate_ok(check.estimate_summary(run), run))
        # A reading dropped, or no tally at all: never a pass.
        run = self.run_of(readings[:2], results)
        self.assertFalse(check.estimate_ok(check.estimate_summary(run), run))
        run = {k: v for k, v in self.run_of(readings, results).items() if k != "tally"}
        self.assertFalse(check.estimate_ok(check.estimate_summary(run), run))

    def test_inputs_read_from_served_rows_are_counted_per_order(self):
        results = self.results()
        stated = [{"name": "supplier_tariff", "kind": "property", "value": {"untrusted_text": "4%"},
                   "object_type": "SupplierItem", "property": "tariff_rate"},
                  {"name": "receipt_currency", "kind": "property",
                   "value": {"untrusted_text": "USD"}, "object_type": "Receipt",
                   "property": "currency"},
                  {"name": "receipt_currency_2", "kind": "property", "value": None,
                   "object_type": "Receipt", "property": "currency"}]
        results[0]["answer"] += stated[:1]
        results[1]["answer"] += stated[1:]
        results[2]["answer"] += stated[2:]
        readings = [check.estimate_reading(r) for r in results]
        self.assertEqual([r["inputs_stated"] for r in readings],
                         [["supplier_tariff_rate"], ["receipt_currency"], []])
        summary = check.estimate_summary(self.run_of(readings, results))
        self.assertEqual(summary["inputs_stated"],
                         {"receipt_currency": 1, "supplier_tariff_rate": 1})


class PlanWiringTest(unittest.TestCase):
    def test_the_plan_reads_the_row_tariff_and_the_receipt_currency_it_is_checked_on(self):
        from operating import ontology_tools as ot
        from operating import ontology_tools_investigate as oi
        import json
        schema = ot.default_schema()
        plans = oi.load_plans(schema=schema)
        doc = plans["landed_cost"].doc if hasattr(plans["landed_cost"], "doc") \
            else plans["landed_cost"]
        contracts = json.loads((Path(__file__).resolve().parents[2]
                                / "operating/ontology_tool_contracts.json").read_text("utf-8"))
        self.assertEqual(oi.check_plan(doc, schema, contracts), [])
        items = {e["name"]: e for e in doc["evidence"]}
        compute = items["landed_cost"]["engine_args"]
        roles = compute["roles"]
        for role, (type_id, prop) in (("supplier_tariff", ("SupplierItem", "tariff_rate")),
                                      ("supplier_origin", ("SupplierItem", "country_of_origin")),
                                      ("receipt_currency", ("Receipt", "currency"))):
            item = items[roles[role]]
            self.assertEqual((item["object_type"], item["properties"]), (type_id, [prop]))
            self.assertTrue(item.get("omit_empty"), role)
        for arg, link in (("supplier_item_link", "SupplierItem.for_item"),
                          ("supplier_link", "SupplierItem.offered_by"),
                          ("po_supplier_link", "PurchaseOrder.ordered_from"),
                          ("supplier_twin_link", "Supplier.same_record_as")):
            self.assertEqual(compute[arg], link)
        # FileMaker's hscode, countryoforigin and tierpricing are not served: typed, unread.
        supplier_item = schema.object_types["SupplierItem"]
        props = {p["name"]: p for p in supplier_item["properties"]} \
            if isinstance(supplier_item, dict) else None
        if props is not None:
            self.assertEqual(props["hs_code"].get("availability"), "not_served")
            self.assertNotIn("supplier_hs_code", roles)
        self.assertNotIn("fm_landed_cost", roles)


def _projected_relations() -> dict:
    # ETL copies (external_id fm-supp-{id}, fm-raw-{id}): their FileMaker twins are stored-key
    # links an amount may rest on; a natively created app row's twins stay leads.
    at = "2099-01-01T12:00:00Z"

    def po(pid, number, supplier):
        return {"id": pid, "po_number": number, "status": "SENT", "supplier_id": supplier,
                "currency": "USD", "shipping_cost": "100.0", "duties_cost": None,
                "incoterms": "FOB", "custom_fields": {}, "updated_at": at}

    def line(lid, pid, product, qty):
        return {"id": lid, "purchase_order_id": pid, "product_id": product, "quantity": qty,
                "quantity_received": "0.0", "unit_price": "1.0", "uom": "ea", "updated_at": at}
    return {
        "public.app_supplier": [{"id": "fx-s1", "code": "FXS1", "name": "FX Supplier",
                                 "external_id": "fm-supp-71", "currency": "USD", "updated_at": at}],
        "public.supplier": [{"id": 71, "name": "FX Supplier", "audittimestampmodified": at}],
        "public.app_product": [
            {"id": "fx-p-1", "code": "FX-RAW1", "external_id": "fm-raw-5", "hs_code": "3923.30", "updated_at": at},
            {"id": "fx-p-2", "code": "FX-RAW2", "external_id": "fm-raw-6", "hs_code": "3923.30", "updated_at": at}],
        "public.raw_material": [{"id": 5, "rawcode": "FX-RAW1", "audittimestampmodified": at},
                                {"id": 6, "rawcode": "FX-RAW2", "audittimestampmodified": at}],
        "public.raw_material_supplier": [
            {"id": 9, "idraw": 5, "idsupplier": 71, "tariff_c": "4%", "currency_c": "USD",
             "itemnumber": "FX-9", "audittimestampmodified": at},
            {"id": 10, "idraw": 6, "idsupplier": 71, "tariff_c": "", "currency_c": "USD",
             "itemnumber": "FX-10", "audittimestampmodified": at}],
        "public.app_purchase_order": [po("fx-po-1", "PO100961", "fx-s1"),
                                      po("fx-po-2", "PO100962", "fx-s1")],
        "public.app_purchase_order_line": [line("fx-pol-1", "fx-po-1", "fx-p-1", "250.0"),
                                           line("fx-pol-2", "fx-po-2", "fx-p-2", "100.0")],
    }


class ProjectedWiringTest(unittest.TestCase):
    """The committed plan over the real projector: an open order of a supplier whose
    FileMaker row states ``tariff_c`` gets its estimate at that rate."""
    PRINCIPAL = "eval:dev-baseline"

    @classmethod
    def setUpClass(cls) -> None:
        from evals.oracle.fixture_world import World
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from operating.case_runtime import CaseRuntime
        from scripts import eval_dev_baseline as script
        from scripts.eval_unfamiliar_sample import _fixture_fetch
        relations = _projected_relations()
        cls.tmp = tempfile.TemporaryDirectory(prefix="plx-landed-wiring-")
        client = WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                        fetch=_fixture_fetch(World(relations)))
        cls.product = script.build_product(client, Path(cls.tmp.name) / "w", sorted(relations),
                                           budget_seconds=None, clock=script.fixture_clock)
        cls.runtime = CaseRuntime(cls.product.state_dir, client=None,
                                  audience=cls.product.audience, tenant=script.TENANT)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.runtime.close()
        cls.tmp.cleanup()

    def investigate(self, number: str, *, markings=None) -> dict:
        from ontology import store_ddl
        from ontology.types import Actor
        from operating import ontology_tools as ot
        from operating import ontology_tools_investigate as oi
        from operating import ontology_tools_traverse as tt
        reader = ot.StoreReader.open(self.product.store)
        self.addCleanup(reader.conn.close)
        who = {self.PRINCIPAL}
        marks = store_ddl.MARKINGS if markings is None else markings
        policy = ot.ReadPolicy(tenant="plx", audiences={k: set(who)
                                                        for k in reader.audience_keys()},
                               markings={m: set(who) for m in marks})
        base = ot.OntologyTools(reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(
            table, rows=oi.citation_rows(self.runtime), scope=oi.read_scope(base)))
        return table["plx.investigate"]["handler"](
            {"family": "landed_cost", "root": {"identifier": number},
             "as_known_at": "2099-01-02T04:00:00Z"}, Actor("plx", frozenset(who)))

    def test_the_row_tariff_reaches_the_estimate_and_an_empty_one_is_absent(self):
        result = self.investigate("PO100961")
        props = [e for e in result["answer"] if e["kind"] == "property"
                 and e.get("object_type") == "SupplierItem" and e.get("property") == "tariff_rate"]
        self.assertEqual(len(props), 1, [e["name"] for e in result["answer"]])
        computed = {e["name"]: e for e in result["answer"] if e["kind"] == "computed"}
        line = computed["landed_cost_estimate_line_1"]
        # (250 + 100 + 4% of 250) / 250
        self.assertEqual(line["value"], "1.44")
        self.assertIn(props[0]["name"], line["inputs"])
        reading = check.estimate_reading(result)
        self.assertTrue(reading["estimate"] and reading["labelled"])
        self.assertIn("duty_supplier_row_tariff", reading["assumptions"])
        self.assertEqual(reading["inputs_stated"], ["supplier_tariff_rate"])
        # The empty tariff_c: no entry, no unreadable gap, the default duty with its HS gap.
        result = self.investigate("PO100962")
        self.assertFalse([e for e in result["answer"] if e.get("property") == "tariff_rate"])
        self.assertNotIn("supplier_tariff_unreadable", {g["reason"] for g in result["gaps"]})
        computed = {e["name"]: e for e in result["answer"] if e["kind"] == "computed"}
        self.assertEqual(computed["landed_cost_estimate_line_1"]["value"], "2.065")
        self.assertIn("duty_rate_source_absent", {g["reason"] for g in result["gaps"]})

    def test_a_reader_without_the_finance_marking_reads_no_row_rate(self):
        from ontology import store_ddl
        result = self.investigate("PO100961", markings=[m for m in store_ddl.MARKINGS
                                                        if m != "finance"])
        self.assertFalse([e for e in result["answer"] if e.get("property") == "tariff_rate"])
        text = repr(result)
        self.assertNotIn("4%", text)
        self.assertNotIn("0.04", text)


if __name__ == "__main__":
    unittest.main()
