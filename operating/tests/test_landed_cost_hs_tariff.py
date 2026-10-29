"""HS category table, HS x origin tariff table, provisional business rules and the incoterm rule.

Evidence label: isolated-fixture (synthetic rows, never live PLX data).
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from operating import hs_tariff as hs
from operating import landed_cost as lc

PROV = hs.PROVISIONAL


def _tables(hs_obs, cats=(), rate_obs=()):
    return hs.build_hs_category_table(hs_obs, cats), hs.build_tariff_table(rate_obs)


class CategoryTableTest(unittest.TestCase):
    def test_mapped_conflict_and_gap_are_named(self):
        table = hs.build_hs_category_table(
            [{"category": "Shampoo ", "hs_code": "3305.10", "source": "s1"},
             {"category": "shampoo", "hs_code": "330510", "source": "s2"},
             {"category": "bottle", "hs_code": "3923.30", "source": "s1"},
             {"category": "bottle", "hs_code": "7010.90", "source": "s3"},
             {"category": "label", "hs_code": "  ", "source": "s1"},
             {"category": "cap", "hs_code": "not a code", "source": "s1"}],
            ["label", "cap", "wipes"])
        cats = table["categories"]
        self.assertEqual(cats["shampoo"]["status"], "mapped")
        self.assertEqual(cats["shampoo"]["codes"][0]["sources"], ["s1", "s2"])
        self.assertEqual(cats["bottle"]["status"], "conflict")
        self.assertEqual(len(cats["bottle"]["codes"]), 2)
        for gap in ("label", "cap", "wipes"):
            self.assertEqual(cats[gap]["status"], "gap")
        self.assertEqual(table["coverage"], {"categories": 5, "mapped": 1, "conflict": 1,
                                             "gap": 3})

    def test_rows_to_observations_use_recorded_values_only(self):
        rows = {"public.app_product": [
                    {"id": "1", "category_id": None, "sage_category": "BOTTLE", "hs_code": None},
                    {"id": "2", "category_id": None, "sage_category": "WIPES",
                     "hs_code": "5603.91"}],
                "sage.ic_item": [{"itemno": "x", "category": "BOTTLE  ", "tariffcode": " " * 20}],
                "public.raw_material_supplier": [
                    {"tariff_c": "6%", "hscode": "3305.10", "countryoforigin": "China"},
                    {"tariff_c": "6%", "hscode": None, "countryoforigin": "China"}]}
        hs_obs, cats, rate_obs = hs.observations_from_rows(rows)
        table = hs.build_hs_category_table(hs_obs, cats)["categories"]
        self.assertEqual(table["bottle"]["status"], "gap")
        self.assertEqual(table["wipes"]["status"], "mapped")
        self.assertEqual(len(rate_obs), 1)


class TariffTableTest(unittest.TestCase):
    def test_only_percent_written_rates_with_known_origin_count(self):
        t = hs.build_tariff_table([
            {"hs_code": "3305.10", "origin": "China", "rate": "6.5%", "source": "a"},
            {"hs_code": "3305.10", "origin": "China", "rate": "6.5 %", "source": "b"},
            {"hs_code": "3923.30", "origin": "CN", "rate": "5%", "source": "a"},
            {"hs_code": "3923.30", "origin": "CN", "rate": "7%", "source": "b"},
            {"hs_code": "3923.30", "origin": "Atlantis", "rate": "5%", "source": "a"},
            {"hs_code": "3923.30", "origin": "CN", "rate": "5", "source": "a"}])
        by = {r["hs_code"]: r for r in t["recorded"]}
        self.assertEqual(by["330510"]["status"], "recorded")
        self.assertEqual(by["392330"]["status"], "conflict")
        self.assertEqual(t["coverage"]["unusable_observations"], 2)
        self.assertIn(PROV, json.dumps(t["owner_rules"]))
        self.assertEqual(t["default"]["rate"], "0.065")


class CiteDutyTest(unittest.TestCase):
    def setUp(self):
        self.hs_t, self.tar = _tables(
            [{"category": "shampoo", "hs_code": "3305.10", "source": "hsrc"},
             {"category": "hair conditioner", "hs_code": "3305.90", "source": "hsrc"},
             {"category": "bottle", "hs_code": "3923.30", "source": "hsrc"},
             {"category": "bottle", "hs_code": "7010.90", "source": "hsrc2"}],
            ["label"],
            [{"hs_code": "3305.10", "origin": "CN", "rate": "4%", "source": "tariff_c"},
             {"hs_code": "3305.10", "origin": "US", "rate": "2%", "source": "tariff_c"}])

    def test_recorded_chain_cites_every_step(self):
        c = hs.cite_duty("Shampoo", "China", self.hs_t, self.tar)
        self.assertEqual((c["status"], c["rate"], c["hs_code"]), ("cited", Decimal("0.04"),
                                                                 "330510"))
        self.assertEqual([s["step"] for s in c["chain"]], ["category_hs", "hs_origin_rate"])
        self.assertEqual(c["provisional_rules"], [])
        tariff = hs.tariff_for_po_line(c)
        self.assertEqual(tariff["duty_rate"], "0.04")
        self.assertIn("tariff_c", tariff["_source"])

    def test_recorded_rate_beats_the_us_rule(self):
        c = hs.cite_duty("shampoo", "US", self.hs_t, self.tar)
        self.assertEqual(c["rate"], Decimal("0.02"))
        self.assertEqual(c["provisional_rules"], [])

    def test_us_origin_zero_rule_is_labelled_provisional(self):
        c = hs.cite_duty("bottle", "USA", self.hs_t, self.tar)
        self.assertEqual((c["status"], c["rate"]), ("cited", Decimal(0)))
        self.assertEqual(c["provisional_rules"][0]["status"], PROV)
        self.assertEqual(c["chain"][0]["status"], PROV)

    def test_us_hair_conditioner_falls_to_default_not_zero(self):
        c = hs.cite_duty("hair conditioner", "US", self.hs_t, self.tar)
        self.assertEqual(c["status"], "default")
        self.assertEqual(c["rate"], Decimal("0.065"))
        self.assertEqual(c["provisional_rules"][0]["id"], "default_tariff")
        self.assertIsNone(hs.tariff_for_po_line(c))

    def test_gaps_fail_closed(self):
        cases = [(("shampoo", None), "ORIGIN_NOT_RECORDED"),
                 (("shampoo", "Atlantis"), "ORIGIN_NOT_RECORDED"),
                 (("label", "CN"), "HS_CODE_NOT_RECORDED_FOR_CATEGORY"),
                 (("bottle", "CN"), "HS_CODE_CONFLICT"),
                 (("unknown", "CN"), "CATEGORY_NOT_RECORDED"),
                 ((None, "US"), "CATEGORY_NOT_RECORDED")]
        for args, gap in cases:
            c = hs.cite_duty(*args, self.hs_t, self.tar)
            self.assertEqual((c["status"], c["gap"], c["rate"]), ("gap", gap, None), args)
            self.assertIsNone(hs.tariff_for_po_line(c))

    def test_rules_are_editable_config_without_code_change(self):
        rules = hs.load_rules()
        rules["origin_duty_rules"][0]["dutiable_categories"].append("bottle")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rules.json"
            path.write_text(json.dumps(rules))
            edited = hs.load_rules(path)
        c = hs.cite_duty("bottle", "US", self.hs_t, self.tar, edited)
        self.assertEqual(c["status"], "gap")  # now dutiable, mapping conflicts, no rule zero
        self.assertEqual(hs.load_rules()["status"], PROV)

    def test_cardinality(self):
        for n in (1, 10, 60):
            obs = [{"category": f"c{i}", "hs_code": f"33{i:02d}.10", "source": "s"}
                   for i in range(n)]
            table = hs.build_hs_category_table(obs)
            self.assertEqual(table["coverage"]["mapped"], n)
            self.assertEqual(len(table["categories"]), n)


class IncotermTest(unittest.TestCase):
    PO = {"_source": "api://po/1", "shippingCost": "1000", "dutiesCost": "300"}
    LINES = [{"id": "a", "_source": "api://l/a", "unitPrice": "2", "quantity": "10",
              "quantityReceived": "10"}]

    def test_po_overrides_vendor_default(self):
        r = hs.resolve_incoterm("ddp", "EXW")
        self.assertEqual((r["incoterm"], r["set_by"]), ("DDP", "po"))
        r = hs.resolve_incoterm(None, "FOB")
        self.assertEqual((r["incoterm"], r["set_by"]), ("FOB", "vendor_default"))
        self.assertEqual(r["buyer_bears"], {"freight": True, "duty": True, "brokerage": True})
        self.assertEqual(r["status"], PROV)

    def test_unknown_or_missing_term_is_a_gap(self):
        self.assertEqual(hs.resolve_incoterm("CIF", None)["gap"], "INCOTERM_RULE_NOT_RECORDED")
        self.assertEqual(hs.resolve_incoterm(None, " ")["gap"], "INCOTERM_NOT_RECORDED")

    def test_ddp_makes_freight_and_duty_cited_zero_and_labelled(self):
        out = lc.po_line_landed(self.PO, self.LINES, "a",
                                incoterm=hs.resolve_incoterm("DDP", None))
        for k in ("freight", "duty"):
            self.assertEqual(out[k]["derived"]["value"], "0")
            self.assertIn(PROV, out[k]["derived"]["rule"])
        self.assertEqual(out["provisional_rules"][0]["status"], PROV)

    def test_exw_leaves_buyer_costs_unchanged(self):
        base = lc.po_line_landed(self.PO, self.LINES, "a")
        exw = lc.po_line_landed(self.PO, self.LINES, "a",
                                incoterm=hs.resolve_incoterm("EXW", None))
        self.assertEqual(base["freight"], exw["freight"])
        self.assertEqual(base["duty"], exw["duty"])
        self.assertEqual(base["provisional_rules"], [])
        self.assertEqual(exw["provisional_rules"][0]["status"], PROV)

    def test_cited_tariff_flows_into_po_line_with_provisional_label(self):
        hs_t, tar = _tables([{"category": "bottle", "hs_code": "3923.30", "source": "h"}])
        cited = hs.cite_duty("bottle", "US", hs_t, tar)
        out = lc.po_line_landed(self.PO, self.LINES, "a", tariff=hs.tariff_for_po_line(cited))
        self.assertIn("derived", out["duty"])
        self.assertEqual(out["provisional_rules"][0]["status"], PROV)


if __name__ == "__main__":
    unittest.main()
