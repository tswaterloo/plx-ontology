"""Actual duty: broker/duty charges, linked only by recorded references, effective rate per PO
and per item, and the order of duty bases in landed cost.

Evidence label: isolated-fixture (synthetic rows, never live PLX data).
"""

from __future__ import annotations

from decimal import Decimal
import unittest

from operating import duty_actual as da
from operating import hs_tariff as hs
from operating import landed_cost as lc

D = Decimal
GL = [{"acctid": "4110101   ", "acctdesc": "Duties & Brokerage - Raw Materials"},
      {"acctid": "4113101", "acctdesc": "Duties & Brokerage - Packaging"},
      {"acctid": "5000001", "acctdesc": "Freight In"}]
VENDORS = [{"vendorid": "B1", "vendname": "Northgate Customs Brokers Ltd"},
           {"vendorid": "S1", "vendname": "Some Supplier Inc"}]


def line(vend, inv, acct, amount, **extra):
    return {"idvend": vend, "idinvc": inv, "idacct": acct, "amtextndhc": amount, **extra}


def rows(lines, ibh=(), obs=(), pjh=(), bc=()):
    return {"sage.gl_account": GL, "sage.ap_vendor": VENDORS, "sage.ap_pjd": list(lines),
            "sage.ap_ibh": list(ibh), "sage.ap_obs": list(obs), "sage.ap_pjh": list(pjh),
            "public.bc_purchase_invoice_snapshot": list(bc)}


POS = {"PO90000001": {}, "PO90000002": {}, "PO#7001": {}}
POS = {da.po_key(k): v for k, v in POS.items()}


class ChargeTest(unittest.TestCase):
    def test_only_duty_gl_lines_become_charges_and_account_formats_agree(self):
        out = da.build_charges(rows([
            line("B1", "I1", "41101-01", "100.10"), line("B1", "I1", "4110101", "50"),
            line("B1", "I1", "5000001", "999"),            # freight on the same invoice
            line("S1", "I2", "41131-01", "20")]))
        by = {c["invoice"]: c for c in out["charges"]}
        self.assertEqual(by["I1"]["amount"], D("150.10"))
        self.assertEqual(by["I1"]["lines"], 2)
        self.assertTrue(by["I1"]["broker_by_name"])
        self.assertFalse(by["I2"]["broker_by_name"])
        self.assertEqual(out["coverage"]["gl_accounts"], 2)
        self.assertEqual(out["coverage"]["sage_broker_vendors"], 1)

    def test_non_numeric_amount_is_excluded_and_counted_not_zero(self):
        out = da.build_charges(rows([line("B1", "I1", "4110101", "n/a"),
                                     line("B1", "I1", "4110101", "5")]))
        self.assertEqual(out["charges"][0]["amount"], D("5"))
        self.assertEqual(out["excluded"]["amount_not_numeric"], 1)

    def test_unknown_account_shape_never_matches(self):
        out = da.build_charges(rows([line("B1", "I1", "41101-0000-01", "5")]))
        self.assertEqual(out["charges"], [])

    def test_bc_invoice_counts_only_from_a_broker_named_vendor(self):
        bc = [{"vendor_number": "V1", "vendor_name": "Harbour Customs Broker", "number": "PI1",
               "amount_excluding_tax": "70.25", "currency_code": "CAD",
               "order_number": "PO90000001"},
              {"vendor_number": "V2", "vendor_name": "Box Maker", "number": "PI2",
               "amount_excluding_tax": "10", "currency_code": "CAD", "order_number": "PO90000001"}]
        out = da.build_charges(rows([], bc=bc))
        [c] = out["charges"]
        self.assertEqual((c["source"], c["amount"]), ("public.bc_purchase_invoice_snapshot",
                                                      D("70.25")))
        [link] = da.link_charges(out["charges"], POS)
        self.assertEqual((link["po"], link["linked_by"]), ("PO90000001", ["po_number"]))


class LinkTest(unittest.TestCase):
    def link(self, lines, **kw):
        built = da.build_charges(rows(lines, **kw))
        return da.link_charges(built["charges"], POS, kw.get("suppliers"))

    def test_structured_po_reference_links(self):
        [r] = self.link([line("B1", "I1", "4110101", "10")],
                        ibh=[{"idvend": "B1", "idinvc": "I1", "ponbr": "PO90000001  "}])
        self.assertEqual((r["linked"], r["po"], r["linked_by"]), (True, "PO90000001", ["po_number"]))

    def test_po_number_in_line_text_links(self):
        [r] = self.link([line("B1", "I1", "4110101", "10", glref="entry for po90000002 cleared")])
        self.assertEqual((r["po"], r["linked_by"]), ("PO90000002", ["po_text"]))

    def test_supplier_invoice_number_alone_is_a_lead_never_a_link(self):
        built = da.build_charges(rows([line("B1", "I1", "4110101", "10",
                                            gldesc="supplier inv SUP-5521 duty")]))
        [r] = da.link_charges(built["charges"], POS, {"SUP-5521": ["PO90000001"]})
        self.assertEqual((r["linked"], r["reason"], r["lead_pos"]),
                         (False, "SUPPLIER_INVOICE_LEAD_ONLY", ["PO90000001"]))
        rates = da.po_effective_rates([r], {"PO90000001": {"value": "100", "currency": "CAD"}})
        self.assertEqual(rates, {})                       # a lead never feeds actual duty

    def test_a_year_in_free_text_does_not_link_to_a_receipt_numbered_like_it(self):
        built = da.build_charges(rows([line("B1", "I1", "4110101", "10",
                                            gldesc="Customs entry Oct 2026")]))
        [r] = da.link_charges(built["charges"], POS, {"2026": ["PO90000001"]})
        self.assertFalse(r["linked"])

    def test_a_lead_corroborates_a_recorded_po_reference_to_the_same_po(self):
        built = da.build_charges(rows(
            [line("B1", "I1", "4110101", "10", gldesc="supplier inv SUP-5521")],
            ibh=[{"idvend": "B1", "idinvc": "I1", "ponbr": "PO90000001"}]))
        [r] = da.link_charges(built["charges"], POS, {"SUP-5521": ["PO90000001"]})
        self.assertEqual((r["po"], r["linked_by"]), ("PO90000001",
                                                     ["po_number", "supplier_invoice"]))

    def test_the_brokers_own_invoice_number_is_never_matched(self):
        bc = [{"vendor_number": "V1", "vendor_name": "Harbour Customs Broker", "number": "PI1",
               "amount_excluding_tax": "70", "currency_code": "CAD",
               "vendor_invoice_number": "BRK-9001"}]
        built = da.build_charges(rows([], bc=bc))
        [r] = da.link_charges(built["charges"], POS, {"BRK-9001": ["PO90000001"]})
        self.assertEqual((r["linked"], r["reason"]), (False, "NO_REFERENCE_RECORDED"))
        own = da.build_charges(rows([line("B1", "BRK-77", "4110101", "10", gldesc="BRK-77")]))
        [r] = da.link_charges(own["charges"], POS, {"BRK-77": ["PO90000001"]})
        self.assertFalse(r["linked"])

    def test_unlinked_charges_keep_a_reason(self):
        got = self.link([line("B1", "I1", "4110101", "10"),
                         line("B1", "I2", "4110101", "10", glref="PO99999999"),
                         line("B1", "I3", "4110101", "10", glref="PO90000001 PO90000002")])
        self.assertEqual([(r["linked"], r.get("reason")) for r in got],
                         [(False, "NO_REFERENCE_RECORDED"), (False, "REFERENCE_NOT_A_KNOWN_PO"),
                          (False, "MULTIPLE_POS_NO_SPLIT_RULE")])

    def test_amount_date_and_vendor_alone_never_link(self):
        # a PO of exactly this value exists; nothing but a reference may link
        [r] = self.link([line("S1", "I1", "4110101", "1234.00", dateinvc="20260101")])
        self.assertFalse(r["linked"])

    def test_bare_digits_do_not_link_to_a_po(self):
        # "7001" is a PO number's digits but a text token without a PO marker is not a reference
        [r] = self.link([line("B1", "I1", "4110101", "10", glref="invoice 7001")])
        self.assertFalse(r["linked"])

    def test_cardinality_no_charge_lost_or_duplicated(self):
        for n in (1, 10, 60):
            lines = [line("B1", f"I{i}", "4110101", "1", glref=f"PO9{i:07d}") for i in range(n)]
            pos = {da.po_key(f"PO9{i:07d}"): {} for i in range(n)}
            got = da.link_charges(da.build_charges(rows(lines))["charges"], pos)
            self.assertEqual(len(got), n)
            self.assertEqual(len({r["po"] for r in got}), n)


class RateTest(unittest.TestCase):
    def charge(self, po, amount, cur="CAD"):
        return {"linked": True, "po": po, "amount": D(amount), "currency": cur,
                "includes_brokerage": True, "linked_by": ["po_number"]}

    def test_cad_and_usd_pos_use_exact_decimals_and_the_recorded_rate(self):
        goods = {"A": {"value": "1000", "currency": "CAD"},
                 "B": {"value": "1000", "currency": "USD", "rate": "1.3"}}
        got = da.po_effective_rates([self.charge("A", "65"), self.charge("B", "91")], goods)
        self.assertEqual(got["A"]["rate"], D("0.065"))
        self.assertEqual(got["B"]["rate"], D("0.07"))          # 91 / (1000 x 1.3)
        self.assertTrue(got["A"]["includes_brokerage"])

    def test_several_charges_on_one_po_sum(self):
        got = da.po_effective_rates([self.charge("A", "30"), self.charge("A", "35")],
                                    {"A": {"value": "1000", "currency": "CAD"}})
        self.assertEqual((got["A"]["rate"], got["A"]["charges"]), (D("0.065"), 2))

    def test_gaps_fail_closed_with_a_reason(self):
        goods = {"A": {"value": "1000", "currency": "USD"},          # no exchange rate
                 "B": {"value": "0", "currency": "CAD"},             # no goods value
                 "C": {"value": "100", "currency": "CAD"}}           # rate above the ceiling
        got = da.po_effective_rates([self.charge("A", "5"), self.charge("B", "5"),
                                     self.charge("C", "500"), self.charge("D", "5"),
                                     self.charge("E", "5", "USD")],
                                    {**goods, "E": {"value": "10", "currency": "USD", "rate": "1"}})
        self.assertEqual({k: (v["rate"], v["gap"]) for k, v in got.items()}, {
            "A": (None, "EXCHANGE_RATE_NOT_RECORDED"), "B": (None, "GOODS_VALUE_NOT_RECORDED"),
            "C": (None, "RATE_NOT_PLAUSIBLE_CHARGE_MAY_COVER_MORE_THAN_THIS_PO"),
            "D": (None, "GOODS_VALUE_NOT_RECORDED"), "E": (None, "CHARGE_CURRENCY_NOT_HOME")})

    def test_rate_above_forty_percent_is_flagged_and_not_used(self):
        goods = {"A": {"value": "100", "currency": "CAD"}}
        for amount, usable in (("40", True), ("41", False)):
            got = da.po_effective_rates([self.charge("A", amount)], goods)["A"]
            self.assertEqual(got["rate"] is not None, usable, amount)
        self.assertEqual(got["gap"], "RATE_NOT_PLAUSIBLE_CHARGE_MAY_COVER_MORE_THAN_THIS_PO")

    def test_unlinked_charges_never_reach_a_rate(self):
        self.assertEqual(da.po_effective_rates(
            [{"linked": False, "amount": D(5), "reason": "NO_REFERENCE_RECORDED"}], {}), {})

    def test_item_rate_is_exact_only_on_a_single_line_po(self):
        rates = {"p1": {"rate": D("0.05"), "includes_brokerage": True},
                 "p2": {"rate": D("0.04"), "includes_brokerage": True},
                 "p3": {"rate": None, "gap": "X"}}
        got = da.item_rates(rates, {"p1": [{"item": "I1"}],
                                    "p2": [{"item": "I1"}, {"item": "I2"}],
                                    "p3": [{"item": "I3"}]})
        self.assertEqual([(x["po"], x["exact"]) for x in got["I1"]], [("p1", True), ("p2", False)])
        self.assertFalse(got["I2"][0]["exact"])
        self.assertIn("pro rata", got["I2"][0]["allocation"])
        self.assertNotIn("I3", got)

    def test_check_on_real_shaped_output_end_to_end(self):
        built = da.build_charges(rows(
            [line("B1", "I1", "4110101", "70", glref="PO90000001")]))
        links = da.link_charges(built["charges"], POS)
        rates = da.po_effective_rates(links, {"PO90000001": {"value": "1000", "currency": "CAD"}})
        self.assertEqual(da.actual_duty_for_po_line("PO90000001", rates)["rate"], "0.07")
        self.assertIsNone(da.actual_duty_for_po_line("PO90000002", rates))
        self.assertIsNone(da.actual_duty_for_po_line(None, rates))


class GoodsTest(unittest.TestCase):
    def test_goods_value_is_home_currency_and_never_a_partial_sum(self):
        got = da.sage_po_goods([
            {"ponumber": "PO90000001", "extended": "100", "currency": "USD", "rate": "1.25"},
            {"ponumber": "PO90000001", "extended": "50", "currency": "CAD", "rate": "1"},
            {"ponumber": "PO90000002", "extended": "100", "currency": "USD", "rate": ""},
            {"ponumber": "PO90000003", "extended": "9", "currency": "CAD", "multipor": "1"},
            {"ponumber": "PO90000004", "extended": "x", "currency": "CAD"},
            {"ponumber": "", "extended": "5", "currency": "CAD"}])
        self.assertEqual(got["PO90000001"]["value"], D("175"))
        for po in ("PO90000002", "PO90000003", "PO90000004"):
            self.assertIsNone(got[po]["value"], po)
        self.assertEqual(len(got), 4)

    def test_supplier_invoice_index_needs_a_digit_and_lists_every_po(self):
        got = da.supplier_invoice_index([
            {"invnumber": "inv-77", "ponumber": "PO90000001"},
            {"invnumber": "INV-77", "ponumber": "PO90000002"},
            {"invnumber": "PREPAID", "ponumber": "PO90000001"}])
        self.assertEqual(got, {"INV-77": ["PO90000001", "PO90000002"]})
        built = da.build_charges(rows([line("B1", "I1", "4110101", "10", gldesc="ref INV-77")]))
        [r] = da.link_charges(built["charges"], POS, got)
        self.assertEqual((r["linked"], r["reason"], r["lead_pos"]),
                         (False, "SUPPLIER_INVOICE_LEAD_ONLY", ["PO90000001", "PO90000002"]))


class BasisOrderTest(unittest.TestCase):
    def setUp(self):
        self.hs_t = hs.build_hs_category_table(
            [{"category": "bottle", "hs_code": "3923.30", "source": "h"},
             {"category": "shampoo", "hs_code": "3305.10", "source": "h"}])
        self.tar = hs.build_tariff_table(
            [{"hs_code": "3923.30", "origin": "CN", "rate": "2%", "source": "t"}])
        self.actual = {"rate": "0.031", "_source": "x", "includes_brokerage": True}

    def test_actual_beats_a_recorded_rate_a_rule_and_the_default(self):
        for cat, origin in (("bottle", "CN"), ("bottle", "US"), ("shampoo", "CN")):
            cited = hs.cite_duty(cat, origin, self.hs_t, self.tar)
            got = da.choose_duty(self.actual, cited)
            self.assertEqual((got["basis"], got["rate"]), ("actual_recorded", D("0.031")))

    def test_without_an_actual_the_basis_is_named_in_order(self):
        names = {("bottle", "CN"): "recorded_hs_origin_rate", ("bottle", "US"):
                 "provisional_origin_rule", ("shampoo", "CN"): "default_fallback"}
        for (cat, origin), basis in names.items():
            got = da.choose_duty(None, hs.cite_duty(cat, origin, self.hs_t, self.tar))
            self.assertEqual(got["basis"], basis)
        gap = da.choose_duty(None, hs.cite_duty("bottle", None, self.hs_t, self.tar))
        self.assertEqual((gap["basis"], gap["rate"], gap["gap"]),
                         (None, None, "ORIGIN_NOT_RECORDED"))


class LandedCostTest(unittest.TestCase):
    PO = {"_source": "api://po/1", "shippingCost": "0", "dutiesCost": "0"}
    LINES = [{"id": "a", "_source": "api://l/a", "unitPrice": "2", "quantity": "10",
              "quantityReceived": "10"},
             {"id": "b", "_source": "api://l/b", "unitPrice": "5", "quantity": "4",
              "quantityReceived": "4"}]
    ACTUAL = {"rate": "0.07", "_source": "broker charges (1)", "includes_brokerage": True}

    def test_actual_duty_is_rate_times_line_goods_and_labelled(self):
        out = lc.po_line_landed(self.PO, self.LINES, "a", actual_duty=self.ACTUAL)
        derived = out["duty"]["derived"]
        self.assertEqual(Decimal(derived["value"]), D("1.4"))          # 0.07 x (2 x 10)
        self.assertEqual(derived["basis"], "actual_recorded")
        self.assertIn("upper bound", derived["rule"])
        self.assertIn("pro rata", derived["rule"])
        total = sum(Decimal(lc.po_line_landed(self.PO, self.LINES, i, actual_duty=self.ACTUAL
                                              )["duty"]["derived"]["value"]) for i in "ab")
        self.assertEqual(total, D("0.07") * 40)                        # lines add up to the PO

    def test_receipt_actual_still_wins_and_no_actual_changes_nothing(self):
        single = self.LINES[:1]
        receipt = {"_source": "api://rc/1", "freightCost": "0", "dutiesCost": "9"}
        out = lc.po_line_landed(self.PO, single, "a", receipt=receipt, actual_duty=self.ACTUAL)
        self.assertEqual(out["duty"]["derived"]["value"], "9")
        base = lc.po_line_landed(self.PO, self.LINES, "a")
        self.assertEqual(base["duty"], lc.po_line_landed(self.PO, self.LINES, "a",
                                                         actual_duty=None)["duty"])
        self.assertIn("unknown", base["duty"])

    def test_ddp_never_overrides_a_recorded_actual(self):
        ddp = hs.resolve_incoterm("DDP", None)
        out = lc.po_line_landed(self.PO, self.LINES, "a", actual_duty=self.ACTUAL, incoterm=ddp)
        self.assertEqual(Decimal(out["duty"]["derived"]["value"]), D("1.4"))
        self.assertTrue(any("recorded actual wins" in p["status"] for p in out["provisional_rules"]))
        po = {**self.PO, "shippingCost": "50"}
        receipt = {"_source": "api://rc/1", "freightCost": "50", "dutiesCost": "9"}
        out = lc.po_line_landed(po, self.LINES[:1], "a", receipt=receipt, incoterm=ddp)
        self.assertEqual(out["duty"]["derived"]["value"], "9")
        self.assertNotEqual(out["freight"]["derived"]["value"], "0")
        nothing = lc.po_line_landed(self.PO, self.LINES, "a", incoterm=ddp)
        self.assertEqual(nothing["duty"]["derived"]["value"], "0")
        self.assertIn("provisional rule", nothing["duty"]["derived"]["rule"])

    def test_us_otherwise_zero_rule_gives_a_zero_duty_not_the_header_estimate(self):
        hs_t = hs.build_hs_category_table([{"category": "bottle", "hs_code": "3923.30",
                                            "source": "h"}])
        cited = hs.cite_duty("bottle", "US", hs_t, hs.build_tariff_table([]))
        po = {**self.PO, "dutiesCost": "300"}
        out = lc.po_line_landed(po, self.LINES[:1], "a", tariff=hs.tariff_for_po_line(cited))
        self.assertEqual(out["duty"]["derived"]["value"], "0")
        self.assertEqual(out["provisional_rules"][0]["status"], hs.PROVISIONAL)

    def test_unusable_actual_rate_falls_through_to_the_unknown(self):
        for bad in ({"rate": "abc", "_source": "x"}, {"rate": "-1", "_source": "x"},
                    {"rate": "NaN", "_source": "x"}):
            out = lc.po_line_landed(self.PO, self.LINES, "a", actual_duty=bad)
            self.assertIn("unknown", out["duty"], bad)


if __name__ == "__main__":
    unittest.main()
