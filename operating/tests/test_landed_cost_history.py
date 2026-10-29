"""Landed cost estimated from PLX history: duty and freight rates per supplier, else per
recorded supplier country, else company-wide, built at capture time and used by the landed-cost
estimate when nothing records the part.

PLX records almost no per-PO duty or freight link, so most orders had gaps and no number. The
capture now derives rates from history (median and IQR of the linked POs' effective rates; for
freight, which names no PO, a company-wide ratio of Sage AP freight charges over goods received
per quarter). The engine uses them after every recorded source and before the Portal default,
labels each part (actual / recorded / estimated_from_history / default) with its basis, marks
the total an estimate and never words a history figure as recorded. Amounts stay behind the
finance marking.

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
from operating import duty_actual as da
from operating import landed_cost as lc
from operating.tests import test_landed_cost_estimate as est
from operating.tests import test_landed_cost_live_inputs as live
from scripts import duty_actual_report as report
from tests import test_investigate_landed_cost as world

D = Decimal
PO = est.PO


def _hist(rate, *, level="all", method="median_of_po_rates", n=84, period="2017-10..2024-08",
          median=None, q1="0.006", q3="0.038", brokerage=True, **over):
    rec = {"rate": rate, "level": level, "method": method, "n": n,
           "unit": da.HISTORY_METHODS.get(method, "purchase_orders"), "period": period,
           "median": median or rate, "q1": q1, "q3": q3, "includes_brokerage": brokerage}
    rec.update(over)
    return rec


FREIGHT = _hist("0.2", method="pooled_period_ratio", n=27, period="2017-Q2..2026-Q4",
                median="0.19", q1="0.1", q3="0.33", brokerage=False)
DUTY = _hist("0.015", q1="0.007", q3="0.038")


def provider(records=None, default=None, known=("PO900001",)):
    """A landed_actuals provider as InvestigateTools passes it to a finance reader."""
    def read(keys):
        return {"records": {k: v for k, v in (records or {}).items() if k in keys},
                "known": [k for k in keys if k in known],
                "default_estimates": {"duty": DUTY, "freight": FREIGHT}
                if default is None else default,
                "digest": "0123456789abcdef", "built_at": "2099-01-01T00:00:00+00:00"}
    return read


def run(lines, *, landed_actuals=None, numbers=("PO900001",), freight=None, duty=None,
        incoterm=None, assumptions=None, receipts=0):
    """``est.plan`` with the order's number and a landed_actuals provider."""
    extra = {"po_number": [est._entry("po_number", PO, {"untrusted_text": n},
                                      name=f"po_number_{i}")
                           for i, n in enumerate(numbers, start=1)]}
    original = lc.plan_run

    def with_actuals(*a, **k):
        return original(*a, **k, landed_actuals=landed_actuals)
    lc.plan_run = with_actuals
    try:
        return est.plan(lines, freight=freight, duty=duty, extra=extra, assumptions=assumptions,
                        receipts=receipts)
    finally:
        lc.plan_run = original


def _steps(out, name="landed_cost_estimate"):
    return out["by_name"][name]["steps"]


def _part(out, part, name="landed_cost_estimate"):
    [step] = [s for s in _steps(out, name) if s.startswith(f"part {part}: ")]
    return step


class CaptureHistoryTest(unittest.TestCase):
    """The capture-side tables: per supplier, per country, company-wide, with their basis."""

    def parties(self):
        rows = {"sage.po_header": [
            {"ponumber": f"PO{i:06d}", "vdcode": "V1" if i <= 4 else "V2",
             "vdcountry": "China" if i <= 6 else "CN", "date": f"2024{i:02d}15"}
            for i in range(1, 9)],
            "public.app_supplier": [{"id": "s1", "country": "CHINA"}],
            "public.app_purchase_order": [
                {"po_number": "PO000001", "supplier_id": "s1", "date_generated": "2023-12-01"},
                {"po_number": "PO000008", "supplier_id": "s1", "created_at": "2024-01-02T00:00Z"}]}
        return da.po_parties(rows)

    def test_duty_history_by_supplier_country_and_all_with_sample_and_spread(self):
        parties = self.parties()
        # Two systems' supplier keys stay separate; a PO whose records name two countries
        # (PO000008: Sage 'CN', Portal 'CHINA') counts in no country.
        self.assertEqual(parties["PO000001"]["suppliers"], ["portal:s1", "sage:V1"])
        self.assertEqual(parties["PO000008"]["countries"], ["CHINA", "CN"])
        self.assertEqual(parties["PO000001"]["date"], "2023-12-01")
        rates = {f"PO{i:06d}": {"rate": D(f"0.0{i}")} for i in range(1, 9)}
        rates["PO000009"] = {"rate": None, "gap": "RATE_NOT_PLAUSIBLE_CHARGE_MAY_COVER_MORE"}
        rates["PO000010"] = {"rate": D("0.9")}            # above the duty ceiling 0.4
        hist = da.history_from_rates(rates, parties, "duty")
        v1 = hist["by_supplier"]["sage:V1"]
        self.assertEqual((v1["n"], v1["median"], v1["q1"], v1["q3"], v1["period"]),
                         (4, "0.025000", "0.017500", "0.032500", "2023-12..2024-04"))
        self.assertEqual(v1["method"], "median_of_po_rates")
        self.assertTrue(v1["includes_brokerage"])
        # Below min_pos (3) a supplier gets no estimate: portal:s1 has 2 POs.
        self.assertNotIn("portal:s1", hist["by_supplier"])
        self.assertEqual(sorted(hist["by_country"]), ["CHINA"])
        self.assertEqual(hist["by_country"]["CHINA"]["n"], 6)
        self.assertEqual((hist["all"]["n"], hist["all"]["median"]), (8, "0.045000"))
        self.assertEqual(hist["counts"]["excluded_rates"], 2)
        # Too few POs overall: no company-wide estimate, never one from a thin sample.
        thin = da.history_from_rates({k: rates[k] for k in ("PO000001", "PO000002")},
                                     parties, "duty")
        self.assertIsNone(thin["all"])

    def test_freight_aggregate_is_pooled_per_quarter_from_one_ledger(self):
        lines = [live.line("F1", f"A{m}", "4110201", "30") for m in range(1, 13)]
        # Sage leaves the line date 0; the invoice header holds it.
        for row in lines:
            row["dateinvc"] = "0"
        heads = [{"idvend": "F1", "idinvc": f"A{m}", "dateinvc": f"2024{m:02d}10"}
                 for m in range(1, 13)]
        rows = {**live.rows(lines, ibh=heads), "public.bc_purchase_invoice_snapshot": [
            {"vendor_number": "B1", "vendor_name": "Ocean Cargo", "number": "X",
             "amount_excluding_tax": "999999", "currency_code": "CAD",
             "invoice_date": "2024-02-01"}]}
        charges = da.build_charges(rows, kind="freight")["charges"]
        self.assertEqual({c["date"][:4] for c in charges}, {"2024"})
        receipts = [{"date": f"2024{m:02d}01", "extended": "100", "currency": "USD",
                     "rate": "1.5"} for m in range(1, 13)]
        receipts.append({"date": "20240301", "extended": "n/a", "currency": "CAD"})
        got = da.aggregate_ratio(charges, receipts, "freight")
        # 90 CAD freight / 450 CAD goods per quarter = 0.2; the BC invoice is not compared
        # with Sage receipts.
        self.assertEqual((got["all"]["rate"], got["all"]["n"], got["all"]["unit"],
                          got["all"]["period"]), ("0.200000", 4, "quarters", "2024-Q1..2024-Q4"))
        self.assertEqual(got["counts"]["receipts_unreadable"], 1)
        self.assertEqual(got["counts"]["charges_undated_or_not_home"], 1)
        # A quarter above the ceiling is excluded; under min_periods quarters, no estimate.
        heavy = [dict(c, amount=D("9000")) if c["date"] < "2024-04" else c for c in charges]
        self.assertIsNone(da.aggregate_ratio(heavy, receipts, "freight")["all"])

    def test_no_rules_file_lowers_the_minimum_sample_below_three(self):
        # Review minor 5: a rules file that allows a one-PO or one-quarter estimate is
        # overridden by duty_actual.HISTORY_MIN_SAMPLE (3 POs, 3 quarters).
        self.assertEqual(da.HISTORY_MIN_SAMPLE, {"purchase_orders": 3, "quarters": 3})
        lax = {**da.RULES, "history": {**da.RULES["history"], "min_pos": 1, "min_pos_all": 1,
                                       "min_periods": 1}}
        parties = {f"P{i}": {"suppliers": ["sage:V1"], "countries": ["CHINA"],
                             "date": "2024-01-01"} for i in range(1, 4)}
        rates = {f"P{i}": {"rate": D("0.02")} for i in range(1, 4)}
        two = da.history_from_rates({k: rates[k] for k in ("P1", "P2")}, parties, "duty", lax)
        self.assertIsNone(two["all"])
        self.assertEqual((two["by_supplier"], two["by_country"]), ({}, {}))
        three = da.history_from_rates(rates, parties, "duty", lax)
        self.assertEqual((three["all"]["n"], three["by_supplier"]["sage:V1"]["n"]), (3, 3))
        home = da.RULES["home_currency"]
        charges = [{"source": "sage.ap_pjd", "currency": home, "date": f"2024{m:02d}10",
                    "amount": D("10")} for m in (1, 4, 7)]
        receipts = [{"date": f"2024{m:02d}01", "extended": "100", "currency": home}
                    for m in (1, 4, 7)]
        self.assertIsNone(da.aggregate_ratio(charges[:2], receipts, "freight", lax)["all"])
        self.assertEqual(da.aggregate_ratio(charges, receipts, "freight", lax)["all"]["n"], 3)

    def test_estimates_resolve_supplier_then_country_then_company_wide(self):
        parties = {"PO1": {"suppliers": ["sage:V1"], "countries": ["CHINA"]},
                   "PO2": {"suppliers": ["sage:V9"], "countries": ["CHINA"]},
                   "PO3": {"suppliers": ["sage:V9"], "countries": ["CN", "CHINA"]},
                   "PO4": {"suppliers": ["sage:V1", "portal:s2"], "countries": []}}
        linked = {"by_supplier": {"sage:V1": _hist("0.02", level="supplier"),
                                  "portal:s2": _hist("0.05", level="supplier")},
                  "by_country": {"CHINA": _hist("0.03", level="country")}, "all": DUTY}
        per_po, default = da.resolve_estimates(
            parties, {"duty": {"linked": linked, "aggregate": {"all": None}},
                      "freight": {"linked": {"by_supplier": {}, "by_country": {}, "all": None},
                                  "aggregate": {"all": FREIGHT}}})
        self.assertEqual(per_po["PO1"]["duty"]["level"], "supplier")
        self.assertEqual(per_po["PO2"]["duty"]["level"], "country")
        # Two countries, or two suppliers with different estimates: the company-wide default.
        self.assertNotIn("PO3", per_po)
        self.assertNotIn("PO4", per_po)
        self.assertEqual((default["duty"], default["freight"]), (DUTY, FREIGHT))

    def test_artifact_carries_estimates_and_the_reader_returns_the_default(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        duty = {"po_rates": {}, "known_pos": ["PO900001", "PO900002"]}
        hist = {"estimates": {"PO900002": {"duty": _hist("0.02", level="supplier")}},
                "default": {"duty": DUTY, "freight": FREIGHT}}
        doc = report.landed_actuals({}, duty, {"po_rates": {}}, built_at="2099-01-01T00:00Z",
                                    history=hist)
        report.write_artifact(doc, Path(tmp.name) / da.ARTIFACT_DIR)
        got = da.ActualsFile.under(tmp.name)(["PO900001", "PO900002"])
        self.assertEqual(got["default_estimates"], {"duty": DUTY, "freight": FREIGHT})
        self.assertEqual(got["records"]["PO900002"]["estimate"]["duty"]["level"], "supplier")
        self.assertNotIn("PO900001", got["records"])


class EstimateFromHistoryTest(unittest.TestCase):
    """At the landed-cost engine boundary (plan_run)."""

    LINES = [("1.00", "100", "0"), ("3.00", "100", "0")]

    def test_an_open_po_with_no_freight_record_gets_a_labelled_estimate(self):
        before = run(self.LINES)
        self.assertIsNone(before["by_name"]["landed_cost_estimate"]["value"])
        out = run(self.LINES, landed_actuals=provider())
        e = out["by_name"]
        # goods 400; freight 0.2 x 400 = 80 (history); duty 0.015 x 400 = 6 (history).
        self.assertEqual(e["landed_cost_estimate"]["value"], "486")
        self.assertEqual(e["landed_cost_estimate_subtotal"]["value"], "480")
        self.assertEqual(e["landed_cost_estimate_line_1"]["value"], "1.215")
        self.assertEqual(_steps(out)[-1], lc.ESTIMATE_LABEL)
        self.assertEqual(_part(out, "goods"),
                         "part goods: recorded (cited unit price x ordered quantity of every "
                         "line)")
        freight = _part(out, "freight")
        self.assertTrue(freight.startswith("part freight: estimated_from_history: ESTIMATED "
                                           "FROM PLX HISTORY (freight), not recorded"), freight)
        for basis in ("all suppliers (company-wide)", "rate 0.2 over 27 quarters",
                      "2017-Q2..2026-Q4", "median 0.19, IQR 0.1 to 0.33",
                      "capture 0123456789abcdef"):
            self.assertIn(basis, freight)
        duty = _part(out, "duty")
        self.assertIn("part duty: estimated_from_history: ESTIMATED FROM PLX HISTORY (duty)", duty)
        self.assertIn("over 84 purchase orders, 2017-10..2024-08", duty)
        self.assertTrue(_part(out, "brokerage").startswith(
            "part brokerage: included in the duty part"))
        self.assertIn("ESTIMATE: at least one part is estimated from PLX history, not recorded "
                      "for this purchase order, so the total is an estimate", _steps(out))
        # The freight gap still names what is not recorded; the actual cost stays unknown.
        self.assertTrue(est._gap(out, "po_freight_not_recorded"))
        self.assertIsNone(e["landed_cost"]["value"])
        # Never worded as recorded: no estimate step calls a history figure recorded.
        text = json.dumps(out["entries"])
        self.assertNotIn("part freight: recorded", text)
        self.assertNotIn("part duty: recorded", text)

    def test_a_full_precision_basis_is_stated_whole(self):
        long = {"duty": _hist("0.014339", median="0.014339", q1="0.006803", q3="0.038409"),
                "freight": _hist("0.194504", method="pooled_period_ratio", n=27,
                                 period="2017-Q2..2026-Q4", median="0.187522", q1="0.095394",
                                 q3="0.327888", brokerage=False)}
        out = run(self.LINES, landed_actuals=provider(default=long))
        for part in ("freight", "duty"):
            step = _part(out, part)
            self.assertLess(len(step), lc.MAX_STEP_CHARS, step)
            self.assertTrue(step.endswith("built 2099-01-01T00:00:00+00:00)"), step)

    def test_recorded_sources_win_over_history_and_history_over_the_default(self):
        # The header freight estimate and a person's duty rate are recorded: history unused.
        out = run(self.LINES, landed_actuals=provider(), freight="100",
                  assumptions=[est._assumption()])
        self.assertTrue(_part(out, "freight").startswith("part freight: recorded (purchase order "
                                                         "header shipping estimate"))
        self.assertEqual(_part(out, "duty"), "part duty: recorded")
        self.assertNotIn("ESTIMATED FROM PLX HISTORY", json.dumps(out["entries"]))
        # A recorded actual of the order beats both.
        paid = {"PO900001": {"freight": {"rate": "0.1", "gap": None, "charges": 1,
                                         "linked_by": ["po_number"]}}}
        out = run(self.LINES, landed_actuals=provider(paid))
        self.assertTrue(_part(out, "freight").startswith("part freight: actual"))
        self.assertTrue(_part(out, "duty").startswith("part duty: estimated_from_history"))
        self.assertEqual(out["by_name"]["landed_cost_estimate"]["value"], "446")
        # No duty history: the Portal default, labelled default, with its range.
        out = run(self.LINES, landed_actuals=provider(default={"freight": FREIGHT}))
        self.assertEqual(_part(out, "duty"), "part duty: default")
        self.assertTrue(_part(out, "brokerage").startswith("part brokerage: not_applicable"))
        self.assertTrue(est._gap(out, "history_estimate_not_available"))
        # The supplier's own estimate wins over the company-wide one.
        own = {"PO900001": {"estimate": {"duty": _hist("0.05", level="supplier", n=4,
                                                       q1="0.04", q3="0.06")}}}
        out = run(self.LINES, landed_actuals=provider(own))
        self.assertIn("this purchase order's supplier", _part(out, "duty"))
        self.assertEqual(out["by_name"]["landed_cost_estimate"]["value"], "500")

    def test_a_seller_borne_part_under_the_recorded_incoterm_is_zero_not_history(self):
        ddp = {"PO900001": {"incoterm": {"po": "DDP", "vendor_default": None}}}
        out = run(self.LINES, landed_actuals=provider(ddp))
        self.assertEqual(out["by_name"]["landed_cost_estimate"]["value"], "400")
        self.assertTrue(_part(out, "freight").startswith("part freight: recorded (incoterm DDP"))
        self.assertNotIn("ESTIMATED FROM PLX HISTORY", json.dumps(out["entries"]))

    def test_an_unknown_or_malformed_estimate_fails_closed(self):
        bad = [_hist("0.2", method="pooled_period_ratio", n=27, extra="x"),
               _hist("0.9", method="pooled_period_ratio", n=27),       # above freight ceiling
               _hist("0.2", method="pooled_period_ratio", n=True),
               _hist("0.2", method="guess", n=27),
               _hist("0.2", method="pooled_period_ratio", n=27, level="region"),
               _hist("0.2", method="pooled_period_ratio", n=27, level="supplier"),  # as default
               _hist("0.2", method="pooled_period_ratio", n=27, period="always"),
               _hist("0.2", method="pooled_period_ratio", n=27, q1="0.5", q3="0.1"),
               {k: v for k, v in FREIGHT.items() if k != "q3"},
               "0.2"]
        for rec in bad:
            out = run(self.LINES, landed_actuals=provider(default={"freight": rec}))
            self.assertIsNone(out["by_name"]["landed_cost_estimate"]["value"], rec)
            self.assertTrue(est._gap(out, "history_estimate_unusable"), rec)
            self.assertNotIn("ESTIMATED FROM PLX HISTORY", json.dumps(out["entries"]))

    def test_an_estimate_on_fewer_than_three_pos_or_quarters_is_a_named_gap(self):
        # Review minor 5: n >= 3 POs (duty) and 3 quarters (freight), else no estimate.
        small = {"duty": _hist("0.015", n=2, q1="0.007", q3="0.038"),
                 "freight": _hist("0.2", method="pooled_period_ratio", n=2,
                                  period="2024-Q1..2024-Q2", median="0.19", q1="0.1",
                                  q3="0.33", brokerage=False)}
        withheld = {"actuals_withheld": True, "default_estimates": small,
                    "digest": "0123456789abcdef", "built_at": "2099-01-01T00:00:00+00:00"}
        for name, actuals in (("finance reader", provider(default=small)),
                              ("purchasing reader", lambda keys: withheld)):
            with self.subTest(name):
                out = run(self.LINES, landed_actuals=actuals)
                self.assertIsNone(out["by_name"]["landed_cost_estimate"]["value"])
                self.assertEqual(len(est._gap(out, "history_sample_too_small")), 2)
                self.assertFalse(est._gap(out, "history_estimate_unusable"))
                self.assertNotIn("ESTIMATED FROM PLX HISTORY", json.dumps(out["entries"]))
        # A supplier estimate on 2 POs is refused too; the company-wide one (84 POs) applies.
        own = {"PO900001": {"estimate": {"duty": _hist("0.05", level="supplier", n=2,
                                                       q1="0.04", q3="0.06")}}}
        out = run(self.LINES, landed_actuals=provider(own))
        self.assertTrue(est._gap(out, "history_sample_too_small"))
        self.assertIn("all suppliers (company-wide)", _part(out, "duty"))
        # Exactly 3 is enough.
        three = {"duty": dict(small["duty"], n=3), "freight": dict(small["freight"], n=3)}
        out = run(self.LINES, landed_actuals=provider(default=three))
        self.assertFalse(est._gap(out, "history_sample_too_small"))
        self.assertIn("over 3 purchase orders", _part(out, "duty"))
        self.assertIn("over 3 quarters", _part(out, "freight"))

    def test_numbers_of_one_order_with_different_estimates_use_the_company_wide_one(self):
        two = {"PO900001": {"estimate": {"duty": _hist("0.05", level="supplier", q3="0.1")}},
               "PO900002": {"estimate": {"duty": _hist("0.09", level="supplier", q3="0.1")}}}
        out = run(self.LINES, landed_actuals=provider(two, known=("PO900001", "PO900002")),
                  numbers=("PO900001", "PO900002"))
        self.assertTrue(est._gap(out, "history_estimates_disagree"))
        self.assertIn("all suppliers (company-wide)", _part(out, "duty"))

    def test_a_reader_without_the_finance_marking_gets_no_history_figure(self):
        out = run(self.LINES, landed_actuals=lambda keys: {"withheld": "finance"})
        self.assertTrue(est._gap(out, "actual_charges_withheld"))
        self.assertIsNone(out["by_name"]["landed_cost_estimate"]["value"])
        self.assertNotIn("ESTIMATED FROM PLX HISTORY", json.dumps(out))

    def test_a_received_po_whose_receipt_records_none_gets_a_received_estimate(self):
        lines = [("1.00", "100", "100"), ("3.00", "100", "100")]
        without = run(lines, receipts=1)
        self.assertIsNone(without["by_name"]["landed_cost_estimate"]["value"])
        out = run(lines, landed_actuals=provider(), receipts=1)
        self.assertEqual(out["by_name"]["landed_cost_estimate"]["value"], "486")
        self.assertEqual(_steps(out)[-1], lc.RECEIVED_ESTIMATE_LABEL)
        self.assertTrue(lc.RECEIVED_ESTIMATE_LABEL.startswith("ESTIMATE"))
        # An unread receipt may record an actual: no history estimate replaces it.
        unread = run(lines, landed_actuals=provider(), receipts=0)
        self.assertIsNone(unread["by_name"]["landed_cost_estimate"]["value"])

    def test_cardinality_1_10_60_lines_no_share_lost_or_doubled(self):
        for n in (1, 10, 60):
            lines = [("1.50", "10", "0")] * n
            started = time.monotonic()
            out = run(lines, landed_actuals=provider())
            self.assertLess(time.monotonic() - started, 10)
            e = out["by_name"]
            goods = D("15") * n
            self.assertEqual(D(e["landed_cost_estimate_subtotal"]["value"]),
                             goods + D("0.2") * goods)
            self.assertEqual(D(e["landed_cost_estimate"]["value"]),
                             goods * (1 + D("0.2") + D("0.015")))
            per_line = [D(e[f"landed_cost_estimate_line_{i}"]["value"]) for i in
                        range(1, n + 1)]
            self.assertEqual(len(per_line), n)
            self.assertEqual(sum(v * 10 for v in per_line), D(e["landed_cost_estimate"]["value"]))
            self.assertEqual(_steps(out)[-1], lc.ESTIMATE_LABEL)


class InvestigateHistoryTest(unittest.TestCase):
    """Through plx.investigate and InvestigateTools' finance gate, over the fixture world:
    PO100901 is fully received and its read receipt records no actual."""

    TENANT, PRINCIPAL = live.InvestigateActualsTest.TENANT, live.InvestigateActualsTest.PRINCIPAL
    setUpClass = classmethod(live.InvestigateActualsTest.setUpClass.__func__)
    table = live.InvestigateActualsTest.table
    investigate = live.InvestigateActualsTest.investigate

    def setUp(self) -> None:
        self.doc = {"PO100901": {"incoterm": {"po": "FOB", "vendor_default": None}}}

    def artifact_with_history(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        doc = {"schema": da.ARTIFACT_SCHEMA, "built_at": "2099-01-01T00:00:00+00:00",
               "known_pos": ["PO100901"], "records": self.doc,
               "history_default": {"duty": DUTY, "freight": FREIGHT}}
        report.write_artifact(doc, Path(tmp.name) / da.ARTIFACT_DIR)
        return da.ActualsFile.under(tmp.name)

    def test_history_reaches_a_finance_reader_only(self):
        result = self.investigate("PO100901", self.table(self.artifact_with_history()))
        computed = world._entries(result, "computed")
        estimate = computed["landed_cost_estimate"]
        self.assertIsNotNone(estimate["value"])
        self.assertEqual(estimate["steps"][-1], lc.RECEIVED_ESTIMATE_LABEL)
        self.assertTrue(any(s.startswith("part duty: estimated_from_history")
                            for s in estimate["steps"]), estimate["steps"])
        # The actual landed cost is not made from an estimate.
        self.assertIsNone(computed["landed_cost"]["value"])
        self.assertTrue(result["claim_checks"]["passed"], result["claim_checks"])
        no_finance = [m for m in store_ddl.MARKINGS if m != "finance"]
        for table in (self.table(self.artifact_with_history(), markings=no_finance),
                      self.table(self.artifact_with_history(), held=False)):
            hidden = self.investigate("PO100901", table)
            self.assertNotIn("ESTIMATED FROM PLX HISTORY", json.dumps(hidden))
            self.assertIsNone(world._entries(hidden, "computed")["landed_cost_estimate"]["value"])


if __name__ == "__main__":
    unittest.main()
