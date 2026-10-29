"""Landed cost on real inputs, at the engine boundary and through the live check's reading.

The FileMaker replica supplier row of the order's own supplier (``tariff_c``, ``hscode``,
``countryoforigin``) feeds the estimate's duty as a labelled assumption naming its source.
A Sage receipt stating its own currency is the receipt. The Portal rate-settings
``tariff_rate`` is the default when cited. FileMaker's own ``dlt_costlanded_c`` is compared
as counts only (agree, differ, missing). A zero receipt actual, an
unreadable currency and a purchase order past the 50-line read each name their typed gap,
and the live check's counts are disjoint, with the overlap reported. Evidence label:
isolated-fixture (synthetic rows, never live PLX data).
"""

from __future__ import annotations

from decimal import Decimal
import re
import unittest

from operating import landed_cost as lc
from scripts import eval_packaging_oracle_check as check

PO = "PurchaseOrder:1"
ARGS = {"roles": {r: r for r in lc.PLAN_ROLES}, "line_link": "PurchaseOrderLine.line_of",
        "item_link": "PurchaseOrderLine.item", "item_twin_link": "Product.same_record_as",
        "quote_link": "ForwarderQuote.for_purchase_order",
        "supplier_item_link": "SupplierItem.for_item", "supplier_link": "SupplierItem.offered_by",
        "po_supplier_link": "PurchaseOrder.ordered_from",
        "supplier_twin_link": "Supplier.same_record_as"}


def _entry(role, oid, value, **extra):
    return {"name": f"{role}_{oid.split(':')[-1]}", "object_id": oid, "value": value,
            "citation": {"l1_handle": f"fixture://{oid}/{role}", "field_path": role}, **extra}


def _text(value):
    return {"untrusted_text": value}


def plan(lines, *, freight="100", tariff=None, hs=None, origin=None, fm=None, currency="USD",
         extra=None, assumptions=None, complete=True, covers=True, rows_hidden=(),
         row_supplier="Supplier:fm", po_supplier=True, unread_lines=0, args=None):
    """One engine run over a purchase order whose ``lines`` are ``(price, ordered, received)``
    in USD per ea. ``tariff``/``hs``/``origin``/``fm``: the line index -> the value on that
    line's item's FileMaker supplier row (``fm``: ``(value, unit, currency)``); the row is
    offered by ``row_supplier``, the twin of the order's supplier (``po_supplier``).
    ``unread_lines``: lines linked to the order that the run never read."""
    evidence: dict = {}
    if currency is not None:
        evidence["currency"] = [_entry("currency", PO, _text(currency))]
    edges = []
    if po_supplier:
        edges += [{"link_type": "PurchaseOrder.ordered_from", "from": PO, "to": "Supplier:po"},
                  {"link_type": "Supplier.same_record_as", "from": "Supplier:po",
                   "to": "Supplier:fm"}]
    for i, (price, ordered, got) in enumerate(lines, start=1):
        oid, item, twin, row = (f"PurchaseOrderLine:{i}", f"Product:{i}", f"Product:fm-{i}",
                                f"SupplierItem:{i}")
        edges += [{"link_type": "PurchaseOrderLine.line_of", "from": oid, "to": PO},
                  {"link_type": "PurchaseOrderLine.item", "from": oid, "to": item},
                  {"link_type": "Product.same_record_as", "from": item, "to": twin},
                  {"link_type": "SupplierItem.for_item", "from": row, "to": twin},
                  {"link_type": "SupplierItem.offered_by", "from": row, "to": row_supplier}]
        evidence.setdefault("unit_price", []).append(
            _entry("unit_price", oid, price, unit="ea", currency="USD"))
        evidence.setdefault("quantity", []).append(_entry("quantity", oid, ordered, unit="ea"))
        evidence.setdefault("received", []).append(_entry("received", oid, got, unit="ea"))
        for role, found in (("supplier_tariff", tariff), ("supplier_hs_code", hs),
                            ("supplier_origin", origin)):
            if found and i in found:
                value = found[i]
                evidence.setdefault(role, []).append(
                    _entry(role, row, _text(value) if isinstance(value, str) else value))
        if fm and i in fm:
            # FileMaker's dlt_costlanded_c sits on the row's price tier (tierpricing); a
            # tier of minimum 1 ea, which every ordered quantity reaches.
            value, unit, cur = fm[i]
            tier = f"SupplierTier:{i}"
            edges.append({"link_type": "SupplierTier.tier_of", "from": tier, "to": row})
            evidence.setdefault("fm_landed_cost", []).append(
                _entry("fm_landed_cost", tier, value, unit=unit, currency=cur))
            evidence.setdefault("fm_tier_quantity", []).append(
                _entry("fm_tier_quantity", tier, "1", unit="ea"))
    for n in range(len(lines) + 1, len(lines) + 1 + unread_lines):
        edges.append({"link_type": "PurchaseOrderLine.line_of",
                      "from": f"PurchaseOrderLine:{n}", "to": PO})
    if freight is not None:
        evidence["freight_estimate"] = [_entry("freight_estimate", PO, freight, currency="USD")]
    for role, found in (extra or {}).items():
        evidence.setdefault(role, []).extend(found)
    args = dict(args or ARGS)
    if fm:
        args.setdefault("tier_link", "SupplierTier.tier_of")
    edges += args.pop("_extra_edges", [])
    rows: dict = {}
    for found in evidence.values():
        for e in found:
            rows[e["citation"]["l1_handle"]] = {e["citation"]["field_path"]: e["value"]}
    out = lc.plan_run(args, evidence, edges, items={}, name="landed_cost",
                      as_of="2099-01-02",
                      resolver=lambda h, actor: None if h in rows_hidden else rows.get(h),
                      actor=None, complete=complete, covers=lambda link: covers,
                      assumptions=assumptions)
    out["by_name"] = {e["name"]: e for e in out["entries"]}
    return out


def _gap(out, reason):
    return [g for g in out["gaps"] if g["reason"] == reason]


def _estimate_gap(out, reason):
    """The estimate's own gaps (the actual lines keep their duty gaps: no receipt or header
    duty is the actual, whatever the estimate assumes)."""
    return [g for g in _gap(out, reason) if g["subject"].startswith("estimate")]


def _reading(out):
    """The live check's reading of an engine answer (``estimate_reading``)."""
    return check.estimate_reading({"answer": out["entries"], "gaps": out["gaps"]})


TWO = [("1.00", "100", "0"), ("3.00", "100", "0")]


class SupplierRowInputsTest(unittest.TestCase):
    def test_supplier_row_tariff_hs_and_origin_feed_the_estimate_as_a_sourced_assumption(self):
        out = plan(TWO, tariff={1: "4%", 2: "4 %"}, hs={1: "3923.30", 2: "3923.30"},
                   origin={1: "CN", 2: "CN"})
        e = out["by_name"]
        # Freight 100 over ordered value 100 and 300: 25 and 75; duty 4% of goods.
        self.assertEqual(e["landed_cost_estimate_line_1"]["value"], "1.29")
        self.assertEqual(e["landed_cost_estimate_line_2"]["value"], "3.87")
        self.assertEqual(e["landed_cost_estimate"]["value"], "516")
        line = e["landed_cost_estimate_line_1"]
        for name in ("supplier_tariff_1", "supplier_hs_code_1", "supplier_origin_1"):
            self.assertIn(name, line["inputs"])
        self.assertIn("SupplierItem:1", line["basis"])
        listed = [s for s in e["landed_cost_estimate"]["steps"]
                  if s.startswith("assumption: duty at the FileMaker supplier row tariff")]
        self.assertEqual(len(listed), 1, e["landed_cost_estimate"]["steps"])
        for part in ("supplier_tariff_1", "tariff_c", "FileMaker replica supplier row",
                     "duty-rate rule", "pending"):
            self.assertIn(part, listed[0])
        self.assertTrue(any("origin: " in s and "supplier_origin_1" in s for s in line["steps"]),
                        line["steps"])
        # Steps name entries and rates, never the row's text.
        self.assertFalse(any("CN" in s or "3923" in s for s in line["steps"]))
        # A cited rate, its HS code cited: neither missing-input gap, no default range.
        self.assertFalse(_estimate_gap(out, "hs_code_not_cited")
                         + _estimate_gap(out, "duty_rate_source_absent"))
        self.assertFalse(any(s.startswith("range: ") for s in e["landed_cost_estimate"]["steps"]))
        self.assertEqual(_reading(out)["assumptions"], ["duty_supplier_row_tariff"])

    def test_a_line_without_a_row_rate_keeps_the_default_and_names_its_input(self):
        out = plan(TWO, tariff={1: "4%"}, hs={2: "3923.30"})
        e = out["by_name"]
        self.assertEqual(e["landed_cost_estimate_line_1"]["value"], "1.29")
        # line 2: (300 + 75 + 6.5% of 300) / 100
        self.assertEqual(e["landed_cost_estimate_line_2"]["value"], "3.945")
        # The row's HS code is a cited HS code: Finance gives the rate, never Procurement.
        [gap] = _estimate_gap(out, "duty_rate_source_absent")
        self.assertIn("landed_cost_estimate_line_2", gap["subject"])
        self.assertNotIn("landed_cost_estimate_line_1", gap["subject"])
        self.assertEqual(gap["who_can_answer"], ["FIN"])
        self.assertFalse(_estimate_gap(out, "hs_code_not_cited"))
        steps = e["landed_cost_estimate"]["steps"]
        # The range runs from no duty on the defaulted line to the default.
        self.assertIn("range: 504 USD (no duty on the 1 line(s) at the Portal default) to "
                      "523.5 USD (duty at the Portal default tariffRate)", steps)
        self.assertEqual(_reading(out)["assumptions"],
                         ["duty_portal_default", "duty_range_stated", "duty_supplier_row_tariff",
                          "tariff_rate_not_projected"])

    def test_an_unreadable_disagreeing_or_foreign_row_rate_is_never_applied(self):
        for tariff, reason in (({1: "6.5"}, "supplier_tariff_unreadable"),
                               ({1: "150%"}, "supplier_tariff_unreadable"),
                               ({1: "-1%"}, "supplier_tariff_unreadable"),
                               ({1: 0.04}, "supplier_tariff_unreadable"),
                               ({1: "four percent"}, "supplier_tariff_unreadable")):
            with self.subTest(tariff=tariff):
                out = plan([("1.00", "100", "0")], tariff=tariff)
                [gap] = _gap(out, reason)
                self.assertEqual(gap["who_can_answer"], ["FIN"])
                self.assertNotIn("6.5", gap["detail"].split(":", 1)[1])
                self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.065")
                self.assertTrue(_estimate_gap(out, "hs_code_not_cited"))
        # Two rows of the order's supplier for one item stating different rates: none chosen.
        extra = {"supplier_tariff": [_entry("supplier_tariff", "SupplierItem:1b", _text("5%"))]}
        out = plan([("1.00", "100", "0")], tariff={1: "4%"}, extra=extra,
                   args=dict(ARGS))
        # SupplierItem:1b is not linked: it is not this item's row, so it is ignored.
        self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.04")
        self.assertFalse(_gap(out, "supplier_tariff_records_disagree"))
        out = _two_rows("4%", "5%")
        self.assertTrue(_gap(out, "supplier_tariff_records_disagree"))
        self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.065")
        self.assertEqual(_two_rows("4%", "4.0%")["by_name"]["landed_cost_estimate_line_1"]
                         ["value"], "2.04")
        # A row of another supplier, or an order whose supplier is unread: not this order's.
        for kwargs in ({"row_supplier": "Supplier:other"}, {"po_supplier": False}):
            with self.subTest(**kwargs):
                out = plan([("1.00", "100", "0")], tariff={1: "4%"}, hs={1: "3923.30"},
                           **kwargs)
                self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.065")
                self.assertTrue(_estimate_gap(out, "hs_code_not_cited"))
                self.assertNotIn("supplier_tariff_1",
                                 out["by_name"]["landed_cost_estimate_line_1"]["inputs"])
        # A plan naming no supplier links never reads a row (the plan as it is today).
        bare = {k: v for k, v in ARGS.items() if not k.startswith(("supplier", "po_supplier"))}
        out = plan([("1.00", "100", "0")], tariff={1: "4%"}, args=bare)
        self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.065")

    def test_a_row_reached_only_by_a_lead_tier_link_or_entry_is_never_used(self):
        # The order's supplier joined to the row's supplier only by an unconfirmed twin.
        lead = {**ARGS, "_extra_edges": [{"link_type": "Supplier.same_record_as",
                                          "from": "Supplier:po", "to": "Supplier:lead",
                                          "tier": "lead"}]}
        out = plan([("1.00", "100", "0")], tariff={1: "4%"}, row_supplier="Supplier:lead",
                   po_supplier=True, args=lead)
        self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.065")
        # The same row joined at an evidence tier is used.
        strong = {**ARGS, "_extra_edges": [dict(lead["_extra_edges"][0], tier="corroborated")]}
        out = plan([("1.00", "100", "0")], tariff={1: "4%"}, row_supplier="Supplier:lead",
                   args=strong)
        self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.04")
        # A tariff entry read through a lead-tier link (lead_basis) is dropped.
        extra = {"supplier_tariff": [dict(_entry("supplier_tariff", "SupplierItem:1",
                                                 _text("4%")), lead_basis=True)]}
        out = plan([("1.00", "100", "0")], extra=extra)
        self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.065")
        self.assertNotIn("supplier_tariff_1", out["by_name"]["landed_cost_estimate_line_1"]
                         ["inputs"])

    def test_a_reader_who_cannot_read_the_row_rate_sees_no_amount(self):
        out = plan(TWO, tariff={1: "4%", 2: "4%"},
                   rows_hidden={"fixture://SupplierItem:1/supplier_tariff"})
        e = out["by_name"]
        self.assertIsNone(e["landed_cost_estimate_line_1"]["value"])
        self.assertIsNone(e["landed_cost_estimate"]["value"])
        self.assertEqual(e["landed_cost_estimate_line_2"]["value"], "3.87")

    def test_per_line_rates_at_1_10_and_60_lines_lose_and_duplicate_nothing(self):
        for n in (1, 10, 60):
            with self.subTest(lines=n):
                lines = [(f"{1 + i % 7}.13", str(10 + i), "0") for i in range(n)]
                rates = {i: f"{i % 5}%" for i in range(1, n + 1)}
                out = plan(lines, freight="1000.01", tariff=rates)
                e = out["by_name"]
                names = sorted(k for k in e if k.startswith("landed_cost_estimate_line"))
                self.assertEqual(names, sorted(f"landed_cost_estimate_line_{i}"
                                               for i in range(1, n + 1)))
                goods = [Decimal(p) * Decimal(q) for p, q, _ in lines]
                duty = sum(g * Decimal(i % 5) / 100 for i, g in enumerate(goods, start=1))
                self.assertEqual(Decimal(e["landed_cost_estimate"]["value"]),
                                 sum(goods) + Decimal("1000.01") + duty)
                self.assertFalse(_estimate_gap(out, "hs_code_not_cited"))
                for entry in out["entries"]:
                    self.assertLessEqual(len(entry.get("steps") or ()), 40)
                    self.assertLessEqual(len(entry.get("inputs") or ()), 50)


def _two_rows(first, second):
    """One line whose item has two rows of the order's supplier stating ``first``/``second``."""
    extra = {"supplier_tariff": [_entry("supplier_tariff", "SupplierItem:1b", _text(second))]}
    out = plan([("1.00", "100", "0")], tariff={1: first}, extra=extra,
               args={**ARGS, "_extra_edges": [
                   {"link_type": "SupplierItem.for_item", "from": "SupplierItem:1b",
                    "to": "Product:fm-1"},
                   {"link_type": "SupplierItem.offered_by", "from": "SupplierItem:1b",
                    "to": "Supplier:fm"}]})
    return out


class RateSettingsTest(unittest.TestCase):
    def test_a_cited_rate_setting_is_the_default_and_a_bad_one_fails_closed(self):
        setting = {"rate_setting_tariff": [_entry("rate_setting_tariff", "RateSettings:1",
                                                  "0.05")]}
        out = plan([("1.00", "100", "0")], extra=setting)
        e = out["by_name"]
        self.assertEqual(e["landed_cost_estimate_line_1"]["value"], "2.05")
        self.assertIn("rate_setting_tariff_1", e["landed_cost_estimate_line_1"]["inputs"])
        steps = e["landed_cost_estimate"]["steps"]
        self.assertTrue(any(s.startswith("assumption: duty at the Portal rate settings "
                                         "tariff_rate 0.05") for s in steps), steps)
        # A flat rate, not the item's: the missing per-item input is still named.
        self.assertTrue(_estimate_gap(out, "hs_code_not_cited"))
        self.assertIn("duty_portal_rate_setting", _reading(out)["assumptions"])
        self.assertNotIn("unclassified", _reading(out)["assumptions"])
        for bad in ("1.5", "-0.1", "6.5%", None):
            with self.subTest(bad=bad):
                setting = {"rate_setting_tariff": [_entry("rate_setting_tariff",
                                                          "RateSettings:1", bad)]}
                out = plan([("1.00", "100", "0")], extra=setting)
                if bad is not None:
                    self.assertTrue(_gap(out, "rate_setting_tariff_unreadable"))
                self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"],
                                 "2.065")
        two = {"rate_setting_tariff": [_entry("rate_setting_tariff", "RateSettings:1", "0.05"),
                                       _entry("rate_setting_tariff", "RateSettings:2", "0.07")]}
        out = plan([("1.00", "100", "0")], extra=two)
        self.assertTrue(_gap(out, "rate_setting_tariff_unreadable"))
        self.assertEqual(out["by_name"]["landed_cost_estimate_line_1"]["value"], "2.065")


class SageReceiptTest(unittest.TestCase):
    RECEIVED = [("1.00", "100", "100"), ("3.00", "100", "100")]

    def test_a_sage_receipt_stating_its_currency_is_the_receipt(self):
        receipt = {"receipt_freight": [_entry("receipt_freight", "Receipt:sage-1", "40")],
                   "receipt_currency": [_entry("receipt_currency", "Receipt:sage-1",
                                               _text("usd"))]}
        out = plan(self.RECEIVED, extra=receipt)
        self.assertFalse([n for n in out["by_name"] if "estimate" in n])
        line = out["by_name"]["landed_cost_line_1"]
        self.assertEqual(line["steps"][-1], lc.FREIGHT_LABELS["posted"])
        self.assertIn("receipt_currency_sage-1", line["inputs"])
        self.assertFalse(_gap(out, "receipt_actual_currency_unrecorded"))
        # Without its currency, or in another one, the actual is not usable.
        for currency, entries in (("none", []), ("EUR", [_entry(
                "receipt_currency", "Receipt:sage-1", _text("EUR"))])):
            with self.subTest(currency=currency):
                out = plan(self.RECEIVED, extra={"receipt_freight": receipt["receipt_freight"],
                                                 "receipt_currency": entries})
                self.assertTrue(_gap(out, "receipt_actual_currency_unrecorded"))
                self.assertNotEqual(out["by_name"]["landed_cost_line_1"]["steps"][-1],
                                    lc.FREIGHT_LABELS["posted"])
        # A receipt's currency never stands in for another receipt's actual.
        other = {"receipt_freight": receipt["receipt_freight"],
                 "receipt_currency": [_entry("receipt_currency", "Receipt:sage-2",
                                             _text("USD"))]}
        self.assertTrue(_gap(plan(self.RECEIVED, extra=other),
                             "receipt_actual_currency_unrecorded"))
        # A receipt whose records state two currencies: unusable, never a pick.
        two = {"receipt_freight": receipt["receipt_freight"],
               "receipt_currency": [_entry("receipt_currency", "Receipt:sage-1", _text("USD")),
                                    dict(_entry("receipt_currency", "Receipt:sage-1",
                                                _text("CAD")), name="receipt_currency_b")]}
        self.assertTrue(_gap(plan(self.RECEIVED, extra=two),
                             "receipt_actual_currency_unrecorded"))


class FileMakerAgreementTest(unittest.TestCase):
    def _agreement(self, out):
        entry = out["by_name"]["landed_cost_fm_agreement"]
        self.assertIsNone(entry["value"])
        counts = entry["steps"][-1]
        match = re.fullmatch(r"counts: agree (\d+), differ (\d+), missing (\d+) .*", counts)
        self.assertIsNotNone(match, counts)
        return tuple(int(x) for x in match.groups()), entry

    def test_agreement_is_reported_as_counts_never_amounts(self):
        # estimate per ea: 1.2317, 3.695 and 2.4633 (freight 100 over 600 of goods)
        fm = {1: ("1.24", "ea", "USD"), 2: ("4.50", "ea", "USD")}
        out = plan(TWO + [("2.00", "100", "0")], fm=fm)
        counts, entry = self._agreement(out)
        self.assertEqual(counts, (1, 1, 1))
        self.assertEqual(entry["steps"][:3], ["landed_cost_line_1: agree (our estimate against FileMaker's recorded "
                                              "dlt_costlanded_c fm_landed_cost_1)",
                                              "landed_cost_line_2: differ (our estimate against FileMaker's "
                                              "recorded dlt_costlanded_c fm_landed_cost_2)",
                                              "landed_cost_line_3: missing (no FileMaker "
                                              "tier price (tierpricing) on the supplier row)"])
        amounts = {"1.24", "4.50", "1.2317", "3.695", "1.23", "3.69"}
        for step in entry["steps"]:
            self.assertFalse([a for a in amounts if a in step], step)
        self.assertEqual(_reading(out)["fm_agreement"], {"agree": 1, "differ": 1, "missing": 1})
        # The actual landed cost (every line received, the receipt's posted freight and duty)
        # is compared: (100 + 100 + 6.5) / 100.
        posted = {role: [_entry(role, "Receipt:1", value, currency="USD")]
                  for role, value in (("receipt_freight", "100"), ("receipt_duty", "6.5"))}
        out = plan([("1.00", "100", "100")], fm={1: ("2.07", "ea", "USD")}, extra=posted)
        self.assertEqual(out["by_name"]["landed_cost_line_1"]["value"], "2.065")
        counts, entry = self._agreement(out)
        self.assertEqual((counts, entry["steps"][0]),
                         ((1, 0, 0), "landed_cost_line_1: agree (our landed cost against "
                                     "FileMaker's recorded dlt_costlanded_c fm_landed_cost_1)"))

    def test_an_incomparable_or_unreadable_figure_is_missing(self):
        for fm, why in (({1: ("1.32", "kg", "USD")}, "unit"), ({1: ("1.32", "ea", "CAD")}, "ccy"),
                        ({1: ("abc", "ea", "USD")}, "text"), ({1: ("0", "ea", "USD")}, "zero")):
            with self.subTest(why=why):
                counts, _ = self._agreement(plan([("1.00", "100", "0")], fm=fm))
                self.assertEqual(counts, (0, 0, 1))
        # A reader who cannot read the FileMaker figure, or our own amount: missing only.
        counts, _ = self._agreement(plan([("1.00", "100", "0")], fm={1: ("1.32", "ea", "USD")},
                                         rows_hidden={"fixture://SupplierTier:1/fm_landed_cost"}))
        self.assertEqual(counts, (0, 0, 1))
        counts, _ = self._agreement(plan([("1.00", "100", "0")], fm={1: ("2.065", "ea", "USD")},
                                         rows_hidden={"fixture://PurchaseOrder:1/currency"}))
        self.assertEqual(counts, (0, 0, 1))
        # No FileMaker figure reached, no entry: the run reports nothing.
        out = plan([("1.00", "100", "0")])
        self.assertNotIn("landed_cost_fm_agreement", out["by_name"])
        self.assertIsNone(_reading(out)["fm_agreement"])


class TypedGapsTest(unittest.TestCase):
    """Each of these names its typed gap, with who can give it."""

    RECEIVED = [("1.00", "100", "100"), ("3.00", "100", "100")]

    def test_a_zero_freight_or_duty_receipt_actual_names_its_gap(self):
        for role in ("receipt_freight", "receipt_duty"):
            with self.subTest(role=role):
                zero = {role: [_entry(role, "Receipt:1", "0", currency="USD")]}
                out = plan(self.RECEIVED, extra=zero)
                [gap] = _gap(out, "receipt_actual_zero")
                self.assertEqual(gap["who_can_answer"], ["WH"])
                entry = out["by_name"]["landed_cost_estimate"]
                self.assertTrue(entry["steps"][0].startswith("no estimate: RECEIPT_ACTUAL_ZERO "))
                reading = _reading(out)
                self.assertIn({"code": "receipt_actual_zero", "who_can_answer": ["WH"]},
                              reading["missing"])
                self.assertTrue(check.estimate_summary({"readings": [reading]})[
                    "with_named_missing_input"])
                self.assertIsNotNone(out["by_name"]["landed_cost_line_1"])

    def test_an_unreadable_currency_names_its_gap_with_an_estimate_entry(self):
        for currency, reason in ((None, "currency_uncited"), ("dollars", "currency_not_iso_4217")):
            for lines in (TWO, self.RECEIVED):
                with self.subTest(currency=currency, received=lines is self.RECEIVED):
                    out = plan(lines, currency=currency)
                    entry = out["by_name"]["landed_cost_estimate"]
                    self.assertIsNone(entry["value"])
                    self.assertTrue(entry["steps"][0].startswith(
                        f"no estimate: {reason.upper()} "), entry["steps"])
                    self.assertEqual(entry["steps"][-1], lc.ESTIMATE_LABEL)
                    [gap] = _gap(out, reason)
                    self.assertEqual(gap["who_can_answer"], ["PROC"])
                    reading = _reading(out)
                    self.assertIn({"code": reason, "who_can_answer": ["PROC"]},
                                  reading["missing"])
                    summary = check.estimate_summary({"readings": [reading]})
                    self.assertEqual(summary["without_estimate_or_named_input"], 0)
                    self.assertEqual(summary["without_estimate_entry"], 0)
        # Two currencies on the order's records: the same, with its own code.
        two = {"currency": [_entry("currency", "PurchaseOrder:twin", _text("CAD"))]}
        out = plan(TWO, extra=two)
        self.assertTrue(out["by_name"]["landed_cost_estimate"]["steps"][0].startswith(
            "no estimate: MIXED_CURRENCY_NO_CITED_RATE "))

    def test_a_po_past_the_50_line_read_cites_the_missing_lines(self):
        lines = [("1.00", "10", "10")] * 50
        for kwargs in ({"unread_lines": 10}, {"complete": False}):
            with self.subTest(**kwargs):
                out = plan(lines, **kwargs)
                entry = out["by_name"]["landed_cost_estimate"]
                self.assertIsNone(entry["value"])
                self.assertFalse(any("RECEIPT_ACTUAL_NOT_READ" in s for s in entry["steps"]))
                self.assertTrue(entry["steps"][0].startswith(
                    "no estimate: ESTIMATE_LINES_INCOMPLETE "), entry["steps"])
                [gap] = _gap(out, "estimate_lines_incomplete")
                self.assertEqual(gap["who_can_answer"], ["PROC"])
                if kwargs.get("unread_lines"):
                    self.assertIn("10 of 60 line(s) were read with no unit price", gap["detail"])
                else:
                    self.assertIn("lines past the read are not read", gap["detail"])
                self.assertIn({"code": "estimate_lines_incomplete", "who_can_answer": ["PROC"]},
                              _reading(out)["missing"])
        # Fifty lines read, the whole order, every one received: the receipt gap as before.
        out = plan(lines)
        self.assertTrue(out["by_name"]["landed_cost_estimate"]["steps"][0].startswith(
            "no estimate: RECEIPT_ACTUAL_NOT_READ "))


class SummaryCountsTest(unittest.TestCase):
    def test_counts_are_disjoint_and_the_overlap_is_reported(self):
        estimate_only = _reading(plan(TWO, tariff={1: "4%", 2: "4%"}, hs={1: "x", 2: "x"}))
        estimate_and_named = _reading(plan(TWO))            # default duty: HS_CODE_NOT_CITED
        named_only = _reading(plan(TWO, freight=None))      # no freight source
        nothing = {"estimate": False, "estimate_entry": True, "labelled": True,
                   "freight_source": "none", "assumptions": [], "missing": [],
                   "fm_agreement": None}
        self.assertTrue(estimate_only["estimate"] and not estimate_only["missing"])
        self.assertTrue(estimate_and_named["estimate"] and estimate_and_named["missing"])
        self.assertTrue(not named_only["estimate"] and named_only["missing"])
        readings = [estimate_only, estimate_and_named, estimate_and_named, named_only, nothing]
        summary = check.estimate_summary({"readings": readings, "listed": 5,
                                          "listing": "complete"})
        self.assertEqual((summary["with_estimate"], summary["with_named_missing_input"],
                          summary["without_estimate_or_named_input"],
                          summary["with_estimate_and_named_missing_input"]), (3, 1, 1, 2))
        self.assertEqual(summary["with_estimate"] + summary["with_named_missing_input"]
                         + summary["without_estimate_or_named_input"], summary["investigated"])
        for n in (1, 10, 60):
            with self.subTest(n=n):
                many = check.estimate_summary({"readings": [estimate_and_named] * n})
                self.assertEqual((many["with_estimate"], many["with_named_missing_input"],
                                  many["with_estimate_and_named_missing_input"]), (n, 0, n))

    def test_fm_agreement_counts_sum_over_the_readings(self):
        with_fm = _reading(plan(TWO, fm={1: ("1.315", "ea", "USD"), 2: ("9", "ea", "USD")}))
        summary = check.estimate_summary({"readings": [with_fm, with_fm, _reading(plan(TWO))]})
        self.assertEqual(summary["fm_landed_cost_agreement"],
                         {"agree": 2, "differ": 2, "missing": 0, "not_reported": 1})


class PlanCheckTest(unittest.TestCase):
    def test_the_new_roles_are_optional_and_an_unknown_one_is_refused(self):
        doc = {"root": {"object_types": ["PurchaseOrder"]}, "evidence": [{"name": r, "kind": "property", "object_type": "PurchaseOrderLine",
                             "properties": [r]} for r in lc.PLAN_ROLES]}
        required = {r: r for r in lc.PLAN_ROLES if r not in lc._OPTIONAL_ROLES}
        for role in ("supplier_tariff", "supplier_hs_code", "supplier_origin",
                     "rate_setting_tariff", "receipt_currency", "fm_landed_cost"):
            self.assertIn(role, lc._OPTIONAL_ROLES)
        problems = lc.plan_check({"roles": required, "line_link": "x"}, doc, len(doc["evidence"]),
                                 None)
        self.assertFalse([p for p in problems if "required" in p], problems)
        problems = lc.plan_check({"roles": {**required, "tariff_guess": "currency"}}, doc,
                                 len(doc["evidence"]), None)
        self.assertIn("unknown role tariff_guess", problems)
        # The supplier-row roles are read from one object type.
        doc["evidence"][lc.PLAN_ROLES.index("supplier_hs_code")]["object_type"] = "Product"
        problems = lc.plan_check({"roles": {r: r for r in lc.PLAN_ROLES}}, doc,
                                 len(doc["evidence"]), None)
        self.assertIn("the supplier row roles must be read from one object type", problems)


if __name__ == "__main__":
    unittest.main()
