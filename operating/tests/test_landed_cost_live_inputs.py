"""Live landed-cost inputs: recorded inbound freight beside recorded duty, the capture-built
artifact, and the plx.investigate landed_cost engine reading it per purchase order behind the
finance gate.

Freight charges are Sage AP lines on inbound-freight G/L accounts (found by account name),
linked to a purchase order only by a recorded reference, with the same lead-only rule for
free-text supplier invoice numbers and the same exclusion of the forwarder's own numbers as
duty. The engine uses a recorded actual first (after a receipt actual), then the header
estimate or a forwarder quote, then a named gap; a reader without the finance marking gets
none of it.

Evidence label: isolated-fixture (synthetic rows, never live PLX data).
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import unittest

from ontology import store_ddl
from ontology.types import Actor
from operating import duty_actual as da
from operating import landed_cost as lc
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt
from scripts import duty_actual_report as report
from tests import test_investigate_landed_cost as world

D = Decimal
GL = [{"acctid": "4110101", "acctdesc": "Duties & Brokerage - Raw Materials"},
      {"acctid": "4110201", "acctdesc": "Freight - Raw Materials CAD"},
      {"acctid": "41130-01", "acctdesc": "Freight - Packaging"},
      {"acctid": "4165501", "acctdesc": "Shipping Outbound to Distribution Customers"},
      {"acctid": "4165001", "acctdesc": "Shipping Supplies"},
      {"acctid": "4169001", "acctdesc": "Freight Charges Recovered CA"},
      {"acctid": "3190001", "acctdesc": "Sales - Shipping Charged Distribution"},
      {"acctid": "4110999", "acctdesc": "Freight, Duty & Customs combined"}]
VENDORS = [{"vendorid": "F1", "vendname": "Bluewater Freight Forwarding"},
           {"vendorid": "S1", "vendname": "Some Supplier Inc"}]
POS = {da.po_key(k): {} for k in ("PO90000001", "PO90000002", "PO90000003")}


def line(vend, inv, acct, amount, **extra):
    return {"idvend": vend, "idinvc": inv, "idacct": acct, "amtextndhc": amount, **extra}


def rows(lines, ibh=(), bc=()):
    return {"sage.gl_account": GL, "sage.ap_vendor": VENDORS, "sage.ap_pjd": list(lines),
            "sage.ap_ibh": list(ibh), "sage.ap_obs": [], "sage.ap_pjh": [],
            "public.bc_purchase_invoice_snapshot": list(bc)}


def freight_links(lines, ibh=(), suppliers=None):
    built = da.build_charges(rows(lines, ibh), kind="freight")
    return da.link_charges(built["charges"], POS, suppliers)


class FreightChargeTest(unittest.TestCase):
    def test_only_inbound_freight_accounts_are_freight_and_never_a_duty_account(self):
        lines = [line("F1", "I1", "4110201", "100"), line("F1", "I1", "4113001", "20.5"),
                 line("F1", "I1", "4165501", "999"), line("F1", "I1", "4165001", "999"),
                 line("F1", "I1", "4169001", "-50"), line("F1", "I1", "3190001", "999"),
                 line("F1", "I1", "4110101", "77"), line("F1", "I1", "4110999", "33")]
        freight = da.build_charges(rows(lines), kind="freight")
        [charge] = freight["charges"]
        self.assertEqual((charge["kind"], charge["amount"], charge["accounts"]),
                         ("freight", D("120.5"), ["4110201", "4113001"]))
        self.assertFalse(charge["includes_brokerage"])
        self.assertEqual(freight["coverage"]["gl_accounts"], 2)
        self.assertEqual(freight["coverage"]["sage_forwarder_vendors"], 1)
        # The combined account is duty's (upper bound), never counted twice as freight.
        [duty] = da.build_charges(rows(lines))["charges"]
        self.assertEqual((duty["amount"], duty["accounts"]), (D("110"), ["4110101", "4110999"]))

    def test_unknown_charge_kind_fails_closed(self):
        with self.assertRaises(ValueError):
            da.build_charges(rows([line("F1", "I1", "4110201", "1")]), kind="insurance")

    def test_freight_links_only_through_a_recorded_reference(self):
        receipts = {"INV-778812": ["PO90000002"], "4500": ["PO90000003"]}
        [by_po, lead, own, text] = freight_links(
            [line("F1", "A1", "4110201", "10"),
             line("F1", "A2", "4110201", "10", glcomment="supplier inv INV-778812"),
             line("F1", "4500", "4110201", "10", glcomment="our ref 4500"),
             line("F1", "A4", "4110201", "10", gldesc="freight PO90000003 + INV-778812")],
            ibh=[{"idvend": "F1", "idinvc": "A1", "ponbr": "PO90000001"}], suppliers=receipts)
        self.assertEqual((by_po["po"], by_po["linked_by"]), ("PO90000001", ["po_number"]))
        # A supplier invoice number in free text is a lead only, never a link.
        self.assertEqual((lead["linked"], lead["reason"], lead["lead_pos"]),
                         (False, "SUPPLIER_INVOICE_LEAD_ONLY", ["PO90000002"]))
        # The forwarder's own invoice number is never a candidate, even when a receipt
        # records the same number.
        self.assertEqual((own["linked"], own["reason"]), (False, "NO_REFERENCE_RECORDED"))
        # A recorded PO reference in the text links; a lead to another PO does not move it.
        self.assertEqual((text["po"], text["linked_by"]), ("PO90000003", ["po_text"]))

    def test_freight_rate_plausibility_flags(self):
        def rate(amount, goods="1000", currency="CAD", fx=None):
            links = freight_links([line("F1", "A1", "4110201", amount)],
                                  ibh=[{"idvend": "F1", "idinvc": "A1", "ponbr": "PO90000001"}])
            return da.po_effective_rates(links, {"PO90000001": {
                "value": goods, "currency": currency, "rate": fx}}, kind="freight")["PO90000001"]
        ok = rate("300")
        self.assertEqual((ok["rate"], ok["freight_paid"], ok["gap"] if "gap" in ok else None),
                         (D("0.3"), D("300"), None))
        # 0.6 is over freight's own ceiling (0.5): flagged, never used.
        self.assertEqual(rate("600")["gap"],
                         "RATE_NOT_PLAUSIBLE_CHARGE_MAY_COVER_MORE_THAN_THIS_PO")
        self.assertEqual(rate("-20")["gap"],
                         "RATE_NOT_PLAUSIBLE_CHARGE_MAY_COVER_MORE_THAN_THIS_PO")
        self.assertIsNone(rate("-20")["rate"])
        self.assertEqual(rate("10", goods=None)["gap"], "GOODS_VALUE_NOT_RECORDED")
        self.assertEqual(rate("10", currency="USD")["gap"], "EXCHANGE_RATE_NOT_RECORDED")
        usd = rate("135", currency="USD", fx="1.35")
        self.assertEqual(usd["rate"], D("0.1"))

    def test_bc_vendor_named_broker_and_forwarder_is_neither(self):
        bc = [{"vendor_number": "V1", "vendor_name": "Harbour Customs Broker & Freight",
               "number": "PI1", "amount_excluding_tax": "70", "currency_code": "CAD",
               "order_number": "PO90000001"},
              {"vendor_number": "V2", "vendor_name": "Ocean Cargo Lines", "number": "PI2",
               "amount_excluding_tax": "40", "currency_code": "CAD", "order_number": "PO90000001"}]
        freight = da.build_charges(rows([], bc=bc), kind="freight")
        duty = da.build_charges(rows([], bc=bc))
        self.assertEqual([c["invoice"] for c in freight["charges"]], ["PI2"])
        self.assertEqual(duty["charges"], [])
        self.assertEqual((freight["excluded"]["vendor_named_both"],
                          duty["excluded"]["vendor_named_both"]), (1, 1))

    def test_cardinality_no_freight_charge_lost_or_duplicated(self):
        for n in (1, 10, 60):
            lines = [line("F1", f"A{i}", "4110201", "1.10") for i in range(n)]
            ibh = [{"idvend": "F1", "idinvc": f"A{i}", "ponbr": f"PO9000000{1 + i % 3}"}
                   for i in range(n)]
            started = time.monotonic()
            links = freight_links(lines, ibh)
            rates = da.po_effective_rates(links, {k: {"value": "1000", "currency": "CAD"}
                                                  for k in POS}, kind="freight")
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(len(links), n)
            self.assertEqual(sum(r["charges"] for r in rates.values()), n)
            self.assertEqual(sum((r["freight_paid"] for r in rates.values()), D(0)),
                             D("1.10") * n)


class ArtifactTest(unittest.TestCase):
    def test_artifact_round_trip_and_fail_closed_reads(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reader = da.ActualsFile.under(tmp.name)
        self.assertEqual(reader(["PO90000001"]), {"unavailable": "LANDED_ACTUALS_NOT_CAPTURED"})
        path = Path(tmp.name) / da.ARTIFACT_DIR / da.ARTIFACT_FILE
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema": "something-else", "records": {}}), "utf-8")
        self.assertEqual(reader(["PO90000001"]), {"unavailable": "LANDED_ACTUALS_UNREADABLE"})
        path.write_text("{not json", "utf-8")
        self.assertEqual(reader(["PO90000001"]), {"unavailable": "LANDED_ACTUALS_UNREADABLE"})
        rows_ = {"public.app_purchase_order": [
                     {"id": "1", "po_number": "PO90000001", "supplier_id": "s1", "incoterms": ""},
                     {"id": "2", "po_number": "PO90000002", "supplier_id": "s1",
                      "incoterms": "DDP"}],
                 "public.app_supplier": [{"id": "s1", "incoterms": "FOB"}]}
        duty = {"po_rates": {"PO90000001": {"rate": D("0.05"), "charges": 2,
                                            "linked_by": ["po_number"],
                                            "includes_brokerage": True}},
                "known_pos": ["PO90000001", "PO90000003"]}
        freight = {"po_rates": {"PO90000001": {"rate": None, "gap": "GOODS_VALUE_NOT_RECORDED",
                                               "charges": 1, "linked_by": ["po_number"]}}}
        doc = report.landed_actuals(rows_, duty, freight, built_at="2099-01-01T00:00:00+00:00")
        report.write_artifact(doc, path.parent)
        got = reader(["PO90000001", "PO90000002", "PO99999999"])
        self.assertEqual(got["records"]["PO90000001"]["duty"]["rate"], "0.05")
        self.assertEqual(got["records"]["PO90000001"]["freight"]["gap"],
                         "GOODS_VALUE_NOT_RECORDED")
        # The PO's own incoterm overrides the supplier default; a blank PO term keeps it.
        self.assertEqual(got["records"]["PO90000001"]["incoterm"],
                         {"po": None, "vendor_default": "FOB"})
        self.assertEqual(got["records"]["PO90000002"]["incoterm"]["po"], "DDP")
        self.assertEqual(got["known"], ["PO90000001", "PO90000002"])
        self.assertEqual(report.artifact_summary(doc)["pos_with_duty_rate"], 1)
        # Rates, counts and codes only: no charge amount, vendor or invoice is written.
        text = path.read_text("utf-8")
        for word in ("paid", "vendor\"", "invoice"):
            self.assertNotIn(word, text)


GOLDEN_PO = "PO100901"


class InvestigateActualsTest(unittest.TestCase):
    """At the plx.investigate boundary over the projected fixture world."""

    TENANT = "plx"
    PRINCIPAL = "eval:dev-baseline"

    @classmethod
    def setUpClass(cls) -> None:
        world.FixtureWorld.build()
        cls.actor = Actor(cls.TENANT, frozenset({cls.PRINCIPAL}))

    def artifact(self, records, known=None) -> da.ActualsFile:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        doc = {"schema": da.ARTIFACT_SCHEMA, "built_at": "2099-01-01T00:00:00+00:00",
               "known_pos": sorted(known if known is not None else records), "records": records}
        report.write_artifact(doc, Path(tmp.name) / da.ARTIFACT_DIR)
        return da.ActualsFile.under(tmp.name)

    def table(self, provider, *, markings=None, held=True) -> dict:
        reader = ot.StoreReader.open(world.FixtureWorld.product.store)
        self.addCleanup(reader.conn.close)
        who = {self.PRINCIPAL}
        marks = store_ddl.MARKINGS if markings is None else markings
        policy = ot.ReadPolicy(tenant=self.TENANT,
                               audiences={k: set(who) for k in reader.audience_keys()},
                               markings={m: set(who) for m in marks})
        base = ot.OntologyTools(reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(
            table, rows=oi.citation_rows(world.FixtureWorld.runtime), scope=oi.read_scope(base),
            held_markings=(lambda actor: policy.held_markings(actor)) if held else None,
            landed_actuals=provider))
        return table

    def investigate(self, number: str, table: dict) -> dict:
        result = table["plx.investigate"]["handler"](
            {"family": "landed_cost", "root": {"identifier": number}, "as_known_at": world.AT},
            self.actor)
        world._ok(self, result)
        return result

    PAID = {GOLDEN_PO: {"duty": {"rate": "0.05", "gap": None, "charges": 2,
                                 "linked_by": ["po_number"], "includes_brokerage": True},
                        "freight": {"rate": "0.1", "gap": None, "charges": 1,
                                    "linked_by": ["po_number"], "includes_brokerage": False},
                        "incoterm": {"po": "FOB", "vendor_default": None}}}

    def test_recorded_duty_and_freight_complete_the_landed_cost(self):
        before = world._entries(self.investigate(GOLDEN_PO, self.table(None)), "computed")
        self.assertIsNone(before["landed_cost"]["value"])
        result = self.investigate(GOLDEN_PO, self.table(self.artifact(self.PAID)))
        computed = world._entries(result, "computed")
        reasons = world._gap_reasons(result)
        self.assertFalse({"duty_rate_source_absent", "hs_code_not_cited",
                          "receipt_actual_not_recorded", "incoterm_not_recorded"} & reasons,
                         reasons)
        self.assertIsNotNone(computed["landed_cost"]["value"])
        line_a = computed["landed_cost_line_1"]
        # duty and freight are the recorded rate times this line's own goods (Decimal exact)
        self.assertIn("goods total: goods * qty [goods=0.802 USD/ea, qty=12500 ea] = 10025 USD",
                      line_a["steps"])
        self.assertTrue(any("duty=501.25 USD" in s for s in line_a["steps"]), line_a["steps"])
        self.assertTrue(any("freight=1002.5 USD" in s for s in line_a["steps"]), line_a["steps"])
        self.assertEqual(line_a["steps"][-1], lc.FREIGHT_LABELS["actual"])
        per_line = [D(computed[f"landed_cost_line_{i}"]["value"]) for i in (1, 2, 3)]
        self.assertTrue(all(v > 0 for v in per_line))
        self.assertTrue(result["claim_checks"]["passed"], result["claim_checks"])

    def test_reader_without_the_finance_marking_gets_no_recorded_charge(self):
        for table in (self.table(self.artifact(self.PAID),
                                 markings=[m for m in store_ddl.MARKINGS if m != "finance"]),
                      self.table(self.artifact(self.PAID), held=False)):
            result = self.investigate(GOLDEN_PO, table)
            self.assertIn("actual_charges_withheld", world._gap_reasons(result))
            text = json.dumps(result)
            self.assertNotIn("actual recorded", text)
            self.assertNotIn("recorded actual", text)
            self.assertNotIn("duty=501.25", text)
            computed = world._entries(result, "computed")
            self.assertIsNone(computed["landed_cost"]["value"])

    def test_flagged_or_unreadable_actuals_are_named_and_never_used(self):
        flagged = {GOLDEN_PO: {"freight": {
            "rate": None, "gap": "RATE_NOT_PLAUSIBLE_CHARGE_MAY_COVER_MORE_THAN_THIS_PO",
            "charges": 3, "linked_by": ["po_number"]},
            "duty": {"rate": "7", "gap": None, "charges": 1, "linked_by": ["po_number"]}}}
        result = self.investigate(GOLDEN_PO, self.table(self.artifact(flagged)))
        reasons = world._gap_reasons(result)
        self.assertLessEqual({"actual_freight_unusable", "actual_duty_unusable",
                              "incoterm_not_recorded"}, reasons)
        line_a = world._entries(result, "computed")["landed_cost_line_1"]
        self.assertEqual(line_a["steps"][-1], lc.FREIGHT_LABELS["estimate"])
        self.assertNotIn("actual recorded", json.dumps(result))
        # No capture on the host, or a number the capture never linked against: named.
        missing = self.investigate(GOLDEN_PO, self.table(da.ActualsFile("/nonexistent/x.json")))
        self.assertIn("landed_actuals_not_captured", world._gap_reasons(missing))
        unknown = self.investigate(GOLDEN_PO, self.table(self.artifact({}, known=[])))
        self.assertIn("po_not_in_charge_capture", world._gap_reasons(unknown))
        broken = self.investigate(GOLDEN_PO, self.table(lambda keys: 1 / 0))
        self.assertIn("landed_actuals_unreadable", world._gap_reasons(broken))

    def test_a_withheld_receipt_actual_is_named_withheld_never_absent(self):
        """PO100904's receipt records freight 250 and duty 40; a reader without the finance
        marking must not be told the receipt records none (routed to the warehouse)."""
        no_finance = [m for m in store_ddl.MARKINGS if m != "finance"]
        result = self.investigate("PO100904", self.table(None, markings=no_finance))
        gaps = {g["reason"]: g for g in result["gaps"]}
        self.assertIn("receipt_actual_withheld", gaps, sorted(gaps))
        self.assertNotIn("receipt_actual_not_recorded", gaps)
        self.assertEqual(gaps["receipt_actual_withheld"]["who_can_answer"], ["FIN"])
        text = json.dumps(result)
        self.assertNotIn("record none", text)
        self.assertNotIn("none records a freight or duty actual", text)
        for amount in ("250", "40.0"):
            self.assertNotIn(f"={amount}", text)
        # A reader holding the marking still reads the receipt's actuals as before.
        full = self.investigate("PO100904", self.table(None))
        self.assertNotIn("receipt_actual_withheld", world._gap_reasons(full))

    def test_every_line_of_long_orders_carries_the_recorded_freight(self):
        for number, count in (("PO100912", 12), ("PO100950", 50)):
            paid = {number: {"freight": {"rate": "0.2", "gap": None, "charges": 1,
                                         "linked_by": ["po_number"]}}}
            table = self.table(self.artifact(paid))
            arguments = {"family": "landed_cost", "root": {"identifier": number},
                         "as_known_at": world.AT}
            started, lines = time.monotonic(), []
            for _ in range(40):
                result = table["plx.investigate"]["handler"](dict(arguments), self.actor)
                world._ok(self, result)
                lines += [e for name, e in world._entries(result, "computed").items()
                          if name.startswith("landed_cost_line_")]
                if result["completeness"]["next_cursor"] is None:
                    break
                arguments["cursor"] = result["completeness"]["next_cursor"]
            self.assertLess(time.monotonic() - started, 120)
            # every line once: none lost across pages, none answered twice
            self.assertEqual(len({e["name"] for e in lines}), count)
            self.assertEqual(len(lines), count)
            # 1.5 x 10 = 15 per line, freight 0.2 x 15 = 3 on each, none lost or doubled
            self.assertEqual({any("freight=3 USD" in s for s in e["steps"]) for e in lines},
                             {True}, number)


def _discounted_bc_world():
    """A BC order whose prices include tax: line 1 at 50% discount with its amount excluding
    tax, line 2 undiscounted with its amount, line 3 discounted with no amount read."""
    from evals.oracle.fixture_world import World, bc_record
    from tests import test_landed_cost_bc_root as bcr
    order = bc_record("purchaseOrders", 0, 830_101, number=bcr.NUMBER, vendorNumber="FXV001",
                      currencyCode="USD", orderDate="2098-12-01", fullyReceived=False,
                      pricesIncludeTax=True)
    specs = [("2.75", {"discountPercent": 50.0, "discountAmount": 137.5,
                       "amountExcludingTax": 137.5}),
             ("4.25", {"discountPercent": 0.0, "discountAmount": 0.0,
                       "amountExcludingTax": 425.0}),
             ("3.00", {"discountPercent": 10.0, "discountAmount": 30.0})]
    lines = [bc_record("purchaseOrderLines", 0, 8_301_010 + n, documentId=order["id"],
                       sequence=10000 * n, lineType="Item", lineObjectNumber=f"FX-{n}",
                       quantity=100.0, receivedQuantity=0.0, invoicedQuantity=0.0,
                       directUnitCost=float(price), unitOfMeasureCode="PCS", **extra)
             for n, (price, extra) in enumerate(specs, start=1)]
    relations = {"public.app_supplier": [], "public.app_purchase_order": [],
                 "public.app_purchase_order_line": [], "public.app_receipt": []}
    return World(relations, bc={"purchaseOrders": [order]}, bc_lines={order["id"]: lines})


class BcNetGoodsTest(unittest.TestCase):
    """A BC line's directUnitCost is gross: goods are its amount excluding tax, else a gap."""

    PRINCIPAL = "eval:dev-baseline"

    @classmethod
    def setUpClass(cls) -> None:
        from tests import test_landed_cost_bc_root as bcr
        original, bcr._world = bcr._world, _discounted_bc_world
        try:
            bcr.BcRootedLandedCost.setUpClass.__func__(cls)
        finally:
            bcr._world = original

    @classmethod
    def tearDownClass(cls) -> None:
        cls.runtime.close()
        cls.tmp.cleanup()

    def investigate(self, markings) -> dict:
        from tests import test_landed_cost_bc_root as bcr
        return bcr.BcRootedLandedCost.investigate(self, markings)

    def test_discounted_and_tax_inclusive_bc_lines_use_the_net_amount_or_a_named_gap(self):
        result = self.investigate(store_ddl.MARKINGS)
        computed = world._entries(result, "computed")
        steps = {n: computed[f"landed_cost_line_{n}"]["steps"] for n in (1, 2, 3)}
        self.assertTrue(any("= 137.5 USD" in s and s.startswith("goods total")
                            for s in steps[1]), steps[1])
        self.assertTrue(any("= 425 USD" in s and s.startswith("goods total")
                            for s in steps[2]), steps[2])
        self.assertFalse(any(s.startswith("goods total") for s in steps[3]), steps[3])
        [gap] = [g for g in result["gaps"] if g["reason"] == "bc_price_not_net"]
        self.assertEqual(gap["who_can_answer"], ["FIN"])
        self.assertIn("the line records a discount", gap["detail"])
        # The gross price never becomes a goods total, a weight or an estimate subtotal.
        text = json.dumps(result)
        for gross in ("= 275 USD", "= 300 USD", "goods: 700 USD", "goods: 1000 USD"):
            self.assertNotIn(gross, text)


if __name__ == "__main__":
    unittest.main()
