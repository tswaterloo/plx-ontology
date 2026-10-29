import copy
import unittest
from decimal import Decimal

from ontology.core import ValidationError
from operating.cases import UnitMismatch
from operating import landed_cost
from operating.landed_cost import LandedCostError, compute_landed_cost

QTY = {"literal": "1000", "unit": "EA"}


def base_costs():
    return [
        {"name": "goods", "category": "goods", "literal": "2.50", "unit": "USD/EA"},
        {"name": "freight", "category": "freight", "literal": "400", "unit": "USD"},
        {"name": "duty", "category": "duty", "literal": "0.10", "unit": "USD/EA", "currency": "USD"},
        {"name": "other", "category": "other", "literal": "100", "unit": "USD"},
    ]


def run(costs=None, **kw):
    kw.setdefault("as_of", "2026-09-30")
    return compute_landed_cost(base_costs() if costs is None else costs, kw.pop("quantity", QTY),
                               kw.pop("target", "USD"), **kw)


class Resolver:
    def __init__(self):
        self.docs = {"quote://1": {"price": "2.50", "fp": 2.5, "until": "2026-12-31",
                                   "old": "2026-01-01"}}

    def __call__(self, handle, actor):
        return self.docs[handle]  # KeyError for unknown handles


class LandedCostTests(unittest.TestCase):
    def test_happy_path_single_currency(self):
        r = run()
        self.assertEqual(r["status"], "computed")
        self.assertEqual(r["total"], {"value": "3100", "unit": "USD"})
        self.assertEqual(r["per_unit"], {"value": "3.1", "unit": "USD/EA"})
        self.assertEqual(r["unknowns"], [])
        self.assertTrue(any("goods total" in s and "2500 USD" in s for s in r["steps"]))
        self.assertEqual(len(r["digest"]), 64)

    def test_fx_path(self):
        costs = base_costs()
        costs[0] = {"name": "goods", "category": "goods", "literal": "18", "unit": "CNY/EA"}
        fx = [{"from": "CNY", "to": "USD", "rate": "0.14", "unit": "USD/CNY", "as_of": "2026-09-29"}]
        r = run(costs, fx=fx)
        self.assertEqual(r["status"], "computed")
        self.assertEqual(r["total"]["value"], "3120")  # 18000*0.14=2520 +400+100+100
        self.assertEqual(r["per_unit"]["value"], "3.12")
        self.assertEqual(r["fx"][0]["as_of"], "2026-09-29")

    def test_fx_validation_matrix(self):
        """Review c202c2f: a zero, future-dated or stale rate was used silently."""
        costs = base_costs()
        costs[0] = {"name": "goods", "category": "goods", "literal": "18", "unit": "CNY/EA"}
        def fx(rate="0.14", as_of="2026-09-29"):
            return [{"from": "CNY", "to": "USD", "rate": rate, "unit": "USD/CNY", "as_of": as_of}]
        for label, entry, status in (
                ("zero rate", fx(rate="0"), "unknown"),
                ("negative rate", fx(rate="-0.14"), "unknown"),
                ("dated after as_of", fx(as_of="2026-10-05"), "unknown"),
                ("older than the limit", fx(as_of="2020-01-01"), "unknown"),
                ("within the limit", fx(as_of="2026-09-24"), "computed")):
            with self.subTest(label):
                r = run(costs, fx=entry)
                self.assertEqual(r["status"], status)
                if status == "unknown":
                    self.assertIsNone(r["total"])
        old = run(costs, fx=fx(as_of="2020-01-01"))
        self.assertTrue(old["fx"][0]["stale"])
        accepted = run(costs, fx=fx(as_of="2020-01-01"), accept_stale="rate confirmed by finance")
        self.assertEqual(accepted["status"], "computed")
        self.assertEqual(accepted["stale"][0]["accepted_with_reason"], "rate confirmed by finance")

    def test_non_positive_goods_is_unknown_and_negative_other_costs_need_an_explicit_credit(self):
        for price in ("0", "-1"):
            costs = base_costs()
            costs[0] = {**costs[0], "literal": price}
            r = run(costs)
            self.assertEqual(r["status"], "unknown", price)
            self.assertEqual(r["unknowns"][0]["reason"], "goods_cost_not_positive")
        costs = base_costs()
        costs.append({"name": "rebate", "category": "other", "literal": "-50", "unit": "USD"})
        r = run(costs)
        self.assertEqual(r["status"], "unknown")
        self.assertIsNone(r["total"])
        self.assertEqual(r["unknowns"][0]["reason"], "negative_cost_requires_explicit_credit")
        self.assertEqual(r["unknowns"][0]["input"], "rebate")
        costs[-1] = {**costs[-1], "credit": True, "credit_reason": "supplier rebate memo 114"}
        r = run(costs)
        self.assertEqual(r["status"], "computed")
        self.assertEqual(r["total"]["value"], "3050")
        rebate = next(x for x in r["inputs"] if x["name"] == "rebate")
        self.assertTrue(rebate["negative"])
        self.assertEqual(rebate["credit"], {"reason": "supplier rebate memo 114"})
        positive = base_costs()
        positive.append({"name": "fee", "category": "other", "literal": "50", "unit": "USD",
                         "credit": True, "credit_reason": "not a credit"})
        with self.assertRaises(LandedCostError):
            run(positive)
        for bad in ({"credit": True}, {"credit": True, "credit_reason": " "},
                    {"credit": "yes", "credit_reason": "x"}):
            costs[-1] = {"name": "rebate", "category": "other", "literal": "-50", "unit": "USD",
                         **bad}
            with self.assertRaises(ValidationError):
                run(costs)
        # A credit flag never makes a negative goods price acceptable.
        goods = base_costs()
        goods[0] = {**goods[0], "literal": "-1", "credit": True, "credit_reason": "x"}
        r = run(goods)
        self.assertEqual(r["unknowns"][0]["reason"], "goods_cost_not_positive")

    def test_missing_fx_is_unknown_with_gap(self):
        costs = base_costs()
        costs[0] = {"name": "goods", "category": "goods", "literal": "18", "unit": "CNY/EA"}
        r = run(costs)
        self.assertEqual(r["status"], "unknown")
        self.assertIsNone(r["total"])
        self.assertIsNone(r["per_unit"])
        self.assertEqual(r["gaps"][0]["missing"], "fx_CNY_to_USD")
        self.assertEqual(r["known_subtotal"]["value"], "600")
        self.assertIn("partial", r["known_subtotal"]["label"])

    def test_fx_wrong_direction_or_unit_refused(self):
        costs = base_costs()
        costs[0] = {"name": "goods", "category": "goods", "literal": "18", "unit": "CNY/EA"}
        fx = [{"from": "CNY", "to": "USD", "rate": "7.1", "unit": "CNY/USD", "as_of": "2026-09-29"}]
        with self.assertRaises(UnitMismatch):
            run(costs, fx=fx)

    def test_unknown_tariff(self):
        costs = base_costs()
        costs[2] = {"name": "duty", "category": "duty", "unknown": "tariff treatment not established",
                    "who_can_answer": "customs broker"}
        r = run(costs)
        self.assertEqual(r["status"], "unknown")
        self.assertIsNone(r["per_unit"])
        self.assertEqual(r["unknowns"][0]["reason"], "tariff treatment not established")
        self.assertEqual(r["unknowns"][0]["who_can_answer"], "customs broker")
        self.assertEqual(r["known_subtotal"]["value"], "3000")

    def test_unknown_requires_who_can_answer(self):
        costs = base_costs()
        costs[2] = {"name": "duty", "category": "duty", "unknown": "x"}
        with self.assertRaises(ValidationError):
            run(costs)

    def test_stale_quote_unknown_without_accept(self):
        costs = base_costs()
        costs[0]["valid_until"] = "2026-09-01"
        r = run(costs)
        self.assertEqual(r["status"], "unknown")
        self.assertTrue(r["stale"][0]["accepted_with_reason"] is None)
        self.assertTrue(r["unknowns"][0]["reason"].startswith("stale_quote"))
        self.assertTrue(r["inputs"][0]["stale"])

    def test_stale_quote_accepted_with_reason_recorded(self):
        costs = base_costs()
        costs[0]["valid_until"] = "2026-09-01"
        r = run(costs, accept_stale="supplier confirmed price by email")
        self.assertEqual(r["status"], "computed")
        # A plain string with exactly one stale item is bound to that item.
        self.assertEqual(r["accept_stale"], {"goods": "supplier confirmed price by email"})
        self.assertEqual(r["stale"][0]["accepted_with_reason"], "supplier confirmed price by email")
        self.assertNotEqual(r["digest"], run()["digest"])

    def test_accept_stale_names_what_it_accepts(self):
        costs = base_costs()
        costs[0]["valid_until"] = "2026-09-01"
        costs[1]["valid_until"] = "2026-09-02"
        r = run(costs, accept_stale={"goods": "emailed", "freight": "forwarder confirmed"})
        self.assertEqual(r["status"], "computed")
        self.assertEqual(r["accept_stale"], {"freight": "forwarder confirmed", "goods": "emailed"})
        # Naming only one leaves the other stale and unknown.
        r = run(costs, accept_stale={"goods": "emailed"})
        self.assertEqual(r["status"], "unknown")
        self.assertEqual(r["unknowns"][0]["input"], "freight")
        # A string cannot cover several stale items.
        with self.assertRaises(ValidationError) as ctx:
            run(costs, accept_stale="all fine")
        self.assertIn("accept_stale_string_ambiguous", str(ctx.exception))
        # Naming something that is not stale is refused.
        with self.assertRaises(ValidationError) as ctx:
            run(costs, accept_stale={"duty": "x", "goods": "y"})
        self.assertIn("accept_stale_names_no_stale_item", str(ctx.exception))
        with self.assertRaises(ValidationError):
            run(base_costs(), accept_stale="nothing is stale")
        with self.assertRaises(ValidationError):
            run(costs, accept_stale={"goods": " "})

    def test_accept_stale_can_name_an_fx_entry_by_index(self):
        costs = base_costs()
        costs[0] = {"name": "goods", "category": "goods", "literal": "18", "unit": "CNY/EA"}
        costs[1]["valid_until"] = "2026-09-01"
        fx = [{"from": "CNY", "to": "USD", "rate": "0.14", "unit": "USD/CNY",
               "as_of": "2020-01-01"}]
        r = run(costs, fx=fx, accept_stale={0: "finance rate", "freight": "email"})
        self.assertEqual(r["status"], "computed")
        self.assertEqual(r["fx"][0]["stale_accepted"], "finance rate")
        r = run(costs, fx=fx, accept_stale={"freight": "email"})
        self.assertEqual(r["status"], "unknown")
        with self.assertRaises(ValidationError):
            run(costs, fx=fx, accept_stale="one reason for two items")

    def test_unused_fx_is_reported_not_stale_and_does_not_affect_the_result(self):
        fx = [{"from": "CNY", "to": "USD", "rate": "0.14", "unit": "USD/CNY",
               "as_of": "2020-01-01"}]
        plain = run()
        r = run(fx=fx)
        self.assertEqual(r["status"], "computed")
        self.assertEqual(r["stale"], [])
        self.assertEqual(r["unused_fx"], [{"input": "fx[0]", "from": "CNY", "to": "USD"}])
        self.assertEqual(r["total"], plain["total"])
        self.assertNotIn("stale", r["fx"][0])
        # A string acceptance cannot be spent on an unused rate.
        with self.assertRaises(ValidationError):
            run(fx=fx, accept_stale="rate was fine")
        self.assertEqual(plain["unused_fx"], [])

    def test_valid_quote_not_stale(self):
        costs = base_costs()
        costs[0]["valid_until"] = "2026-09-30"
        self.assertEqual(run(costs)["stale"], [])

    def test_unit_mismatch_refused(self):
        costs = base_costs()
        costs[0]["unit"] = "USD/kg"
        with self.assertRaises(UnitMismatch):
            run(costs)
        costs = base_costs()
        costs[1]["currency"] = "EUR"
        with self.assertRaises(UnitMismatch):
            run(costs)

    def test_float_refused(self):
        costs = base_costs()
        costs[0]["literal"] = 2.5
        with self.assertRaises(ValidationError):
            run(costs)
        with self.assertRaises(ValidationError):
            run(quantity={"literal": 1000.0, "unit": "EA"})

    def test_digest_reproducible_and_sensitive(self):
        a, b = run(), run()
        self.assertEqual(a["digest"], b["digest"])
        mutations = [
            lambda c, k: c[0].update(literal="2.51"),
            lambda c, k: c[1].update(unit="USD/EA"),
            lambda c, k: c[3].update(name="misc"),
            lambda c, k: k.update(as_of="2026-10-01"),
            lambda c, k: k.update(quantity={"literal": "2000", "unit": "EA"}),
        ]
        for mutate in mutations:
            costs, kw = copy.deepcopy(base_costs()), {}
            mutate(costs, kw)
            try:
                other = run(costs, **kw)
            except ValidationError:
                continue
            self.assertNotEqual(other["digest"], a["digest"])

    def test_cited_inputs_via_resolver(self):
        costs = base_costs()
        costs[0] = {"name": "goods", "category": "goods", "citation": "quote://1",
                    "field": "price", "unit": "USD/EA", "valid_until_field": "until"}
        r = run(costs, resolver=Resolver())
        self.assertEqual(r["status"], "computed")
        self.assertEqual(r["inputs"][0]["citation"]["handle"], "quote://1")
        self.assertEqual(len(r["inputs"][0]["citation"]["digest"]), 64)
        self.assertEqual(r["inputs"][0]["unit_basis"], "caller_declared")
        costs[0]["valid_until_field"] = "old"
        self.assertEqual(run(costs, resolver=Resolver())["status"], "unknown")
        costs[0]["valid_until_field"] = "until"
        costs[0]["field"] = "fp"
        r = run(costs, resolver=Resolver())
        self.assertEqual(r["inputs"][0]["source_number"]["raw"], "2.5")
        self.assertEqual(r["per_unit"]["value"], "3.1")

    def test_unresolvable_citation_fails_closed(self):
        costs = base_costs()
        costs[0] = {"name": "goods", "category": "goods", "citation": "quote://missing",
                    "field": "price", "unit": "USD/EA"}
        for resolver in (Resolver(), None, lambda h, a: "not a mapping"):
            r = run(costs, resolver=resolver)
            self.assertEqual(r["status"], "unknown")
            self.assertIsNone(r["per_unit"])
            self.assertEqual(r["unknowns"][0]["reason"], "citation_unresolvable")

    def test_cited_field_missing_fails_closed(self):
        costs = base_costs()
        costs[0] = {"name": "goods", "category": "goods", "citation": "quote://1",
                    "field": "nope", "unit": "USD/EA"}
        self.assertEqual(run(costs, resolver=Resolver())["unknowns"][0]["reason"],
                         "cited_field_missing_or_null")

    def test_cited_fx(self):
        costs = base_costs()
        costs[0] = {"name": "goods", "category": "goods", "literal": "18", "unit": "CNY/EA"}
        resolver = lambda h, a: {"rate": "0.14"}
        fx = [{"from": "CNY", "to": "USD", "citation": "fx://1", "field": "rate",
               "unit": "USD/CNY", "as_of": "2026-09-29"}]
        self.assertEqual(run(costs, fx=fx, resolver=resolver)["per_unit"]["value"], "3.12")
        self.assertEqual(run(costs, fx=fx)["status"], "unknown")

    def test_quantity_must_be_positive_and_plain_unit(self):
        with self.assertRaises(ValidationError):
            run(quantity={"literal": "0", "unit": "EA"})
        with self.assertRaises(UnitMismatch):
            run(quantity={"literal": "5", "unit": "USD"})


def _contracts(unit="CNY/EA"):
    from operating.source_units import SourceUnitContracts, SCHEMA
    return SourceUnitContracts({"schema": SCHEMA, "contracts": [{
        "id": "purchase-price", "relation": "public.purchase_lines", "column": "price",
        "unit": unit, "meaning": "m", "evidence": ["e"], "verification": "v"}]})


class ApiResolver:
    def __call__(self, handle, actor):
        return {"relation": "public.purchase_lines", "key": {"id": "p1"},
                "row": {"price": "2.50", "currency": "CNY", "uom": "EA"}}


class InputBindingAndUnitTests(unittest.TestCase):
    def test_uom_family_is_quantity_not_currency(self):
        for uom in ("EA", "PCS", "BOX", "CS", "PLT", "KG", "L", "GAL", "CTN", "SET"):
            with self.subTest(uom=uom):
                costs = [{"name": "goods", "category": "goods", "literal": "2.50",
                          "unit": f"USD/{uom}"}]
                r = run(costs, quantity={"literal": "1000", "unit": uom})
                self.assertEqual(r["status"], "computed")
                self.assertEqual(r["total"], {"value": "2500", "unit": "USD"})
                self.assertEqual(r["per_unit"]["unit"], f"USD/{uom}")

    def test_declared_currency_must_be_iso(self):
        costs = [{"name": "goods", "category": "goods", "literal": "2", "unit": "BOX",
                  "currency": "BOX"}]
        with self.assertRaises(UnitMismatch):
            run(costs)
        with self.assertRaises(ValidationError):
            run(target="BOX")

    def test_contract_contradiction_refused(self):
        costs = [{"name": "goods", "category": "goods", "citation": "api://x",
                  "field": "row.price", "unit": "USD/EA"}]
        with self.assertRaises(UnitMismatch) as cm:
            run(costs, resolver=ApiResolver(), unit_contracts=_contracts())
        self.assertIn("unit_contradicts_source_contract", str(cm.exception))

    def test_contract_bound_unit_may_be_omitted_and_is_recorded(self):
        costs = [{"name": "goods", "category": "goods", "citation": "api://x",
                  "field": "row.price"}]
        fx = [{"from": "CNY", "to": "USD", "rate": "0.14", "unit": "USD/CNY", "as_of": "2026-09-29"}]
        r = run(costs, resolver=ApiResolver(), unit_contracts=_contracts(), fx=fx)
        self.assertEqual(r["status"], "computed")
        rec = r["inputs"][0]
        self.assertEqual(rec["unit"], "CNY/EA")
        self.assertEqual(rec["unit_basis"], "source_contract")
        self.assertEqual(rec["unit_contract"], "purchase-price")
        self.assertEqual(r["total"]["value"], "350")  # 2.5*1000*0.14

    def test_matching_declared_unit_accepted_and_literal_is_caller_declared(self):
        costs = [{"name": "goods", "category": "goods", "citation": "api://x",
                  "field": "row.price", "unit": "CNY/EA"}]
        r = run(costs, resolver=ApiResolver(), unit_contracts=_contracts())
        self.assertEqual(r["inputs"][0]["unit_basis"], "source_contract")
        self.assertEqual(run()["inputs"][0]["unit_basis"], "caller_declared")

    def test_no_contract_is_caller_declared_and_mixed_currency_unknown(self):
        costs = [{"name": "goods", "category": "goods", "citation": "api://x",
                  "field": "row.price", "unit": "CNY/EA"}]
        r = run(costs, resolver=ApiResolver(), unit_contracts=None)
        self.assertEqual(r["inputs"][0]["unit_basis"], "caller_declared")
        self.assertEqual(r["status"], "unknown")
        self.assertEqual(r["gaps"][0]["missing"], "fx_CNY_to_USD")
        r = run(costs, resolver=ApiResolver(), unit_contracts=_contracts())  # builtin-like, no FX
        self.assertEqual(r["status"], "unknown")

    def test_builtin_default_leaves_uncontracted_citation_declared(self):
        r = run([{"name": "goods", "category": "goods", "citation": "quote://1",
                  "field": "price", "unit": "USD/EA"}], resolver=Resolver())
        self.assertEqual(r["inputs"][0]["unit_basis"], "caller_declared")


class CostTreatmentAndPortalQuoteMirrorTests(unittest.TestCase):
    """ERP-D-015 and the Portal quote landed cost (Portal staging 5724575, landed-cost.ts
    unchanged since 8da33e4, portal/src/lib/costing/landed-cost.ts:landedComponentCost;
    devloop/CONTEXT/03-PORTAL.md sections 4.1 and 9). The full engine port is golden-tested in
    test_portal_landed_engine.py against the Portal's own TypeScript."""

    def test_every_record_is_labelled_an_estimate_not_item_cost(self):
        for record in (run(), run([{"name": "goods", "category": "goods",
                                    "unknown": "no PI", "who_can_answer": "Purchasing"}])):
            treatment = record["cost_treatment"]
            self.assertEqual(treatment["kind"], "analytic_estimate_not_item_cost")
            self.assertIn("expense (ERP-D-015, Approved 2026-09-10)",
                          treatment["current_portal_decision"])
            self.assertIn("Vince's step 18 asks to reopen ERP-D-015", treatment["open_question"])
            self.assertIn("@5724575", treatment["source"])

    def test_portal_quote_mirror_matches_the_portal_function(self):
        """Expected values computed by running the Portal's landedComponentCost body (num,
        round4 = Math.round(n * 1e4) / 1e4, defaults 0.065 and 1.38) in Node 22."""
        vectors = [((0.802, 0), (0.802, 0.0521, 0.0, 1.1787)),
                   ((0.552, 0.0342), (0.552, 0.0359, 0.0342, 0.8455)),
                   ((1.13, 0.05), (1.13, 0.0735, 0.05, 1.7108)),
                   ((0.0425, 0), (0.0425, 0.0028, 0.0, 0.0625)),
                   ((12.3456, 1.2), (12.3456, 0.8025, 1.2, 19.3444)),
                   ((0.000769, 0.00005), (0.0008, 0.0, 0.0001, 0.0011)),
                   ((0, 0.3), (0.0, 0.0, 0.3, 0.3)),
                   ((-1, 0.1), (0.0, 0.0, 0.1, 0.1)),  # num(): not above zero -> 0
                   ((0.5, 0), (0.5, 0.0325, 0.0, 0.7348))]
        for (tier, freight), expected in vectors:
            got = landed_cost.portal_quote_landed_component(tier, freight)
            self.assertEqual((got["tierPrice"], got["duty"], got["freight"], got["landedUnitCost"]),
                             expected, (tier, freight))

    def test_portal_quote_mirror_uses_the_hard_constants_without_fx(self):
        got = landed_cost.portal_quote_landed_component(1.0, 0.1)
        self.assertEqual((got["dutyRate"], got["multiplier"]), (0.065, 1.38))
        self.assertIsNone(got["currency_conversion"])
        self.assertFalse(got["rate_settings_applied"])
        self.assertIn("not item cost", got["label"])
        self.assertEqual(got["cost_treatment"]["kind"], "analytic_estimate_not_item_cost")
        # RateSettings values cannot be passed: the Portal's callers never pass them.
        with self.assertRaises(TypeError):
            landed_cost.portal_quote_landed_component(1.0, 0.1, dutyRate=0.1)  # type: ignore
        # Bools and non-numbers read as 0, like the Portal's num().
        self.assertEqual(landed_cost.portal_quote_landed_component(True, "3")["landedUnitCost"], 0.0)


class QuoteInputTests(unittest.TestCase):
    """The ``landed_cost`` plan engine over synthetic forwarder-quote answer entries
    (DECISIONS/2026-10-07-freight-quote-basis.md). An unexpected or unread field fails closed:
    the quote is never total freight and the reason is a typed gap; a quote whose own figures
    disagree is a typed conflict. Evidence label: isolated-fixture."""
    FULL = {"quote_rate": "9800", "quote_basis": "per 40HQ", "quote_basis_count": "2 container",
            "quote_scope": "door to door",
            "quote_excludes": "duties and taxes", "quote_valid_until": "2099-01-31"}
    DOOR = {k: v for k, v in FULL.items() if k.startswith(("quote_scope", "quote_excludes",
                                                            "quote_valid"))}
    TEXT = ("quote_basis", "quote_scope", "quote_includes", "quote_excludes")
    MEASURED = ("quote_basis_count", "quote_volume", "quote_weight",
                "quote_pallets")  # "<value> <unit>"

    def plan(self, fields: dict, currency="USD", estimate=None) -> dict:
        def entry(role, oid, value, **extra):
            return {"name": role, "object_id": oid, "value": value,
                    "citation": {"l1_handle": f"sharepoint://{oid}", "field_path": role},
                    **extra}
        evidence: dict = {"currency": [{"name": "po_currency", "object_id": "PO:1",
                                        "value": {"untrusted_text": "USD"}}]}
        edges = []
        if estimate is not None:  # one complete line, so the header estimate is allocated
            edges = [{"link_type": "PurchaseOrderLine.line_of", "from": "L:1", "to": "PO:1"}]
            evidence.update(
                unit_price=[entry("unit_price", "L:1", "0.80", unit="ea", currency="USD")],
                quantity=[entry("quantity", "L:1", "1200", unit="ea")],
                received=[entry("received", "L:1", "1200", unit="ea")],
                freight_estimate=[entry("freight_estimate", "PO:1", estimate, currency="USD")])
        for role, value in fields.items():
            extra = {"currency": currency} if role in ("quote_amount", "quote_rate") else {}
            if extra and " " in value:   # "<amount> <currency>"
                value, extra["currency"] = value.split()
            elif role in self.MEASURED:
                value, extra["unit"] = value.split()
            elif role in self.TEXT:
                value = {"untrusted_text": value}
            evidence[role] = [entry(role, "ForwarderQuote:1", value, **extra)]
        roles = {r: r for r in landed_cost.PLAN_ROLES}
        rows: dict = {}
        for found in evidence.values():
            for e in found:
                if "citation" in e:
                    rows.setdefault(e["citation"]["l1_handle"], {})[
                        e["citation"]["field_path"]] = e["value"]
        return landed_cost.plan_run(
            {"roles": roles, "line_link": "PurchaseOrderLine.line_of",
             "quote_link": "ForwarderQuote.for_purchase_order"}, evidence, edges, items={},
            name="landed_cost", as_of="2099-01-02", resolver=lambda h, actor: rows[h],
            actor=None, complete=True, covers=lambda link: True)

    def run_quote(self, fields: dict, currency="USD") -> tuple:
        out = self.plan(fields, currency)
        [quote] = [e for e in out["entries"] if e["name"] == "landed_cost_freight_quote_1"]
        self.conflicts = out["conflicts"]
        return quote["value"], {g["reason"] for g in out["gaps"]} - {"po_lines_incomplete"}

    def test_a_total_is_never_multiplied_and_a_rate_only_by_a_cited_count(self):
        # A per-container rate times a cited count in containers is the total freight.
        self.assertEqual(self.run_quote(self.FULL), ("19600", set()))
        # The same figure under a total label is never x 2, and not the total either: a
        # per-40HQ amount is total only for 1 container or a cited rate x count equal to it.
        total = {k: v for k, v in self.FULL.items() if k != "quote_rate"}
        for fields, expected in ((dict(total, quote_amount="9800"), (None, {"basis_not_total"})),
                                 (dict(self.FULL, quote_amount="19600.00"), ("19600", set()))):
            with self.subTest(fields=fields):
                self.assertEqual(self.run_quote(fields), expected)
                self.assertEqual(self.conflicts, [])
        # A per-CBM rate with only a container count, or a 40HQ rate with a count in cbm,
        # is a gap, never a number.
        for fields in (dict(self.FULL, quote_basis="per CBM"),
                       dict(self.FULL, quote_basis_count="2 cbm")):
            with self.subTest(fields=fields):
                value, reasons = self.run_quote(fields)
                self.assertIsNone(value)
                self.assertIn("freight_basis_count_unknown", reasons)
                self.assertIn("pack_spec_not_served", reasons)

    def test_a_disagreeing_cited_rate_is_a_conflict_and_one_unit_is_one_whole_unit(self):
        """A count of 1 makes a stated amount the total only for one container or pallet (a
        whole unit-shipment), never 1 kg or 1 CBM, and never against a cited rate."""
        for fields, expected in (
                # 1 kg, 4.20 per kg: the 85 stated is neither the total nor chosen over it.
                ({"quote_amount": "85", "quote_rate": "4.20", "quote_basis": "per kg",
                  "quote_weight": "1 kg"}, (None, {"freight_rate_count_conflict"})),
                # rate 4,900 x 1 x 40HQ is not the 9,800 stated (the reviewer's probe).
                ({"quote_amount": "9800", "quote_rate": "4900", "quote_basis": "1 x 40HQ"},
                 (None, {"freight_rate_count_conflict"})),
                ({"quote_amount": "9800", "quote_rate": "9800", "quote_basis": "per 40HQ",
                  "quote_basis_count": "2 container"}, (None, {"freight_rate_count_conflict"}))):
            with self.subTest(fields=fields):
                self.assertEqual(self.run_quote(dict(self.DOOR, **fields)), expected)
                [conflict] = self.conflicts
                self.assertEqual((conflict["kind"], conflict["tier"]),
                                 ("value_disagreement", "lead"))
                self.assertEqual(conflict["inputs"][:2], ["quote_amount", "quote_rate"])
                self.assertEqual(len(conflict["inputs"]), 3)  # and the count it was read from
                self.assertIn("neither is chosen", conflict["note"])
        for fields, expected, currency in (
                ({"quote_amount": "85", "quote_basis": "per CBM", "quote_volume": "1 cbm"},
                 (None, {"basis_not_total"}), "USD"),
                ({"quote_amount": "85", "quote_basis": "per kg", "quote_weight": "1 kg"},
                 (None, {"basis_not_total"}), "USD"),
                ({"quote_amount": "4700", "quote_basis": "per 40HQ",
                  "quote_basis_count": "1 container"}, ("4700", set()), "USD"),
                ({"quote_amount": "300", "quote_basis": "per pallet", "quote_pallets": "1 pallet"},
                 ("300", set()), "USD"),
                # a rate in another currency is not compared; one container stays the total
                ({"quote_amount": "4700", "quote_rate": "4300 EUR", "quote_basis": "1 x 40HQ"},
                 ("4700", set()), "USD")):
            with self.subTest(fields=fields):
                self.assertEqual(self.run_quote(dict(self.DOOR, **fields), currency), expected)
                self.assertEqual(self.conflicts, [])

    def test_a_count_of_the_basis_unit_is_read_from_every_typed_count_field(self):
        for fields, value in (
                ({"quote_rate": "85", "quote_basis": "per CBM", "quote_volume": "12.5 cbm"},
                 "1062.5"),
                ({"quote_amount": "1062.50", "quote_rate": "85", "quote_basis": "per CBM",
                  "quote_volume": "12.5 cbm"}, "1062.5"),
                ({"quote_amount": "4850", "quote_basis": "1 x 40HQ"}, "4850"),
                ({"quote_rate": "4900", "quote_basis": "per 40HQ x 2"}, "9800"),
                ({"quote_rate": "75", "quote_basis": "per pallet", "quote_pallets": "4 pallet"},
                 "300"),
                ({"quote_rate": "75", "quote_basis": "4 pallets"}, "300"),
                ({"quote_amount": "300", "quote_rate": "75", "quote_basis": "per pallet",
                  "quote_pallets": "4 plts"}, "300"),
                ({"quote_rate": "4.20", "quote_basis": "per kg", "quote_weight": "1200 kg"},
                 "5040")):
            with self.subTest(fields=fields):
                self.assertEqual(self.run_quote(dict(self.DOOR, **fields)), (value, set()))
        # A count in another unit, a fraction of a pallet, a basis line with more than a count
        # on it, or two different counts: never a total.
        for fields, reason in (
                ({"quote_rate": "4.20", "quote_basis": "per kg", "quote_weight": "1200 lb"},
                 "freight_basis_count_unknown"),
                ({"quote_rate": "4.20", "quote_basis": "per kg, min 100 kg"},
                 "freight_basis_terms_unread"),
                ({"quote_rate": "4900", "quote_basis": "-2 x 40HQ"}, "freight_basis_terms_unread"),
                ({"quote_rate": "75", "quote_basis": "per pallet", "quote_pallets": "2.5 pallet"},
                 "freight_basis_count_unknown"),
                ({"quote_rate": "75", "quote_basis": "per pallet", "quote_volume": "4 cbm"},
                 "freight_basis_count_unknown"),
                ({"quote_rate": "4900", "quote_basis": "3 x 40HQ",
                  "quote_basis_count": "2 container"}, "freight_basis_counts_conflict"),
                ({"quote_amount": "9800", "quote_basis": "per CBM", "quote_volume": "12.5 cbm",
                  "quote_basis_count": "10 cbm"},
                 "freight_basis_counts_conflict")):
            with self.subTest(fields=fields):
                value, reasons = self.run_quote(dict(self.DOOR, **fields))
                self.assertIsNone(value)
                self.assertIn(reason, reasons)
                self.assertEqual(len(self.conflicts), reason.endswith("_conflict"))

    def test_a_minimum_or_second_figure_on_the_basis_line_is_never_applied(self):
        """A minimum charge is not read, so rate x a count below it (50 kg at 4.20 with a
        100 kg minimum: at least 420, not 210) is never total freight, and a stated amount
        equal to the minimum is no false rate/count conflict: the basis line is the gap."""
        for fields in (
                {"quote_rate": "4.20", "quote_basis": "per kg, min 100 kg", "quote_weight": "50 kg"},
                {"quote_amount": "420", "quote_rate": "4.20", "quote_basis": "per kg, min 100 kg",
                 "quote_weight": "50 kg"},
                {"quote_rate": "85", "quote_basis": "per CBM, min 1 CBM", "quote_volume": "0.5 cbm"},
                {"quote_rate": "85", "quote_basis": "per CBM (minimum charge)",
                 "quote_volume": "12.5 cbm"},
                {"quote_rate": "4900", "quote_basis": "per 40HQ, max 2",
                 "quote_basis_count": "2 container"},
                {"quote_amount": "1000", "quote_basis": "USD 280 per CBM",
                 "quote_volume": "1 cbm"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.run_quote(dict(self.DOOR, **fields)),
                                 (None, {"freight_basis_terms_unread"}))
                self.assertEqual(self.conflicts, [])
        out = self.plan(dict(self.DOOR, quote_rate="4.20", quote_basis="per kg, min 100 kg"))
        [gap] = [g for g in out["gaps"] if g["reason"] == "freight_basis_terms_unread"]
        self.assertIn("basis line (quote_basis) states a minimum", gap["detail"])
        self.assertNotIn("100", gap["detail"])  # names the entry; quotes no source text
        # A container size is no second figure; a count stated as the whole line is read.
        self.assertEqual(self.run_quote(dict(self.DOOR, quote_rate="4900",
                                             quote_basis="per 40' HQ x 2")), ("9800", set()))

    def test_unread_or_unexpected_quote_fields_are_never_total_freight(self):
        for fields, currency, reason in (
                (dict(self.FULL, quote_basis="per 20GP or 40HQ"), "USD", "freight_basis_unread"),
                (dict(self.FULL, quote_basis="per pallet"), "USD", "freight_basis_count_unknown"),
                (dict(self.FULL, quote_basis="per pallet", quote_amount="9800"), "USD",
                 "basis_not_total"),
                (dict(self.FULL, quote_excludes="insurance"), "USD", "freight_exclusions_unread"),
                (dict(self.FULL, quote_basis_count="1.5 container"), "USD", "freight_basis_count_unknown"),
                (self.FULL, None, "freight_quote_amount_unread"),
                (dict(self.FULL, quote_rate="0"), "USD", "freight_quote_amount_unread"),
                (dict(self.FULL, quote_valid_until="31/01/2099"), "USD",
                 "freight_quote_validity_unknown"),
                (dict(self.FULL, quote_includes="destination charges", quote_excludes="delivery"),
                 "USD", "freight_scope_contradicts")):
            with self.subTest(fields=fields, currency=currency):
                value, reasons = self.run_quote(fields, currency)
                self.assertIsNone(value)
                self.assertIn(reason, reasons)

    def test_an_estimate_is_a_partial_quote_only_within_the_stated_tolerance(self):
        """A header estimate within ESTIMATE_QUOTE_TOLERANCE (0.5%) of a destination-only
        quote is that quote's figure, never allocated as freight; further off it stays the
        labelled header estimate."""
        partial = {"quote_amount": "1710", "quote_basis": "per shipment",
                   "quote_includes": "destination charges", "quote_valid_until": "2099-01-31"}
        self.assertEqual(landed_cost.ESTIMATE_QUOTE_TOLERANCE, Decimal("0.005"))
        for estimate, withheld in (("1710.0", True), ("1712", True), ("1718.55", True),
                                   ("1701.45", True), ("1718.56", False), ("1800", False)):
            with self.subTest(estimate=estimate):
                out = self.plan(partial, estimate=estimate)
                [line] = [e for e in out["entries"] if e["name"] == "landed_cost_line_1"]
                self.assertEqual("freight_estimate_is_partial_quote" in out["reasons"], withheld)
                self.assertEqual(line["steps"][-1], landed_cost.FREIGHT_LABELS[
                    "none" if withheld else "estimate"])


if __name__ == "__main__":
    unittest.main()
