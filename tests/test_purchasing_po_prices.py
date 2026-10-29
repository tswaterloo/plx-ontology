"""Purchase-order prices for purchasing staff (owner decision, Tanush, 2026-10-27).

Purchasing staff may see PO line prices: the unit price, the line amount, the discount, the
PO total and the supplier's quoted price on purchase orders and their lines (BC, Portal,
FileMaker). Those columns and properties carry the marking ``purchasing_prices`` in place of
``finance`` (``ontology/schema/overrides/markings.json`` ``scoped_markings``); a finance reader
holds it too (``ontology/store_ddl.py`` ``MARKING_HELD_WITH``). Margins, GL, AP/AR, sales
prices, costing, PO header freight and duty estimates and receipt and Sage AP actuals stay
finance. Landed cost for a purchasing reader shows goods from PO line prices and the labelled
company-wide history estimate, never a recorded AP charge or a per-supplier rate.

Three readers at each boundary: purchasing (``purchasing_prices``), finance and a reader
holding neither. Evidence label: isolated-fixture / injected-transport (synthetic rows,
never live PLX data).
"""

from __future__ import annotations

from contextlib import closing
import copy
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest

from ontology import store as ontology_store
from ontology import store_ddl
from ontology.generator import overrides as ov
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt
from operating.tests import test_landed_cost_history as hist
from tests import test_investigate_landed_cost as world

ROOT = Path(__file__).resolve().parents[1]
MARKINGS_FILE = ROOT / "ontology/schema/overrides/markings.json"
COVERAGE = ROOT / "ontology/schema/generated/meta/coverage.json"
OTHERS = [m for m in store_ddl.MARKINGS if m not in ("finance", "purchasing_prices")]
READERS = {"purchasing": [*OTHERS, "purchasing_prices"], "finance": [*OTHERS, "finance"],
           "neither": OTHERS}
# Finance-only data a purchasing reader must not reach: margins and costing, GL, AP/AR,
# sales prices, purchase invoices, receipt actuals and PO header freight and duty estimates.
FINANCE_ONLY = [("SalesOrderLine", "unit_price"), ("SalesOrder", "total_amount"),
                ("SalesInvoiceLine", "unit_price"), ("QuoteLine", "unit_price"),
                ("QuoteLine", "std_cost"), ("GeneralLedgerEntry", "debit_amount"),
                ("AgedPayable", "balance_due"), ("AgedReceivable", "balance_due"),
                ("ItemLedgerEntry", "cost_amount_actual"), ("Product", "average_cost"),
                ("Product", "bc_unit_price"), ("PurchaseInvoiceLine", "unit_cost"),
                ("Receipt", "freight_cost_actual"), ("Receipt", "duties_cost_actual"),
                ("PurchaseOrder", "shipping_cost_estimate"),
                ("PurchaseOrder", "duties_cost_estimate"), ("SupplierItem", "tariff_rate"),
                ("ForwarderQuote", "amount"),
                # Sage AP/AR money named amt (review minor 3): unmarked until 2026-10-27.
                ("RelSageApIbh", "amtinvctot"), ("RelSageArOpenDocument", "amtinvchc")]


def _scoped() -> dict:
    [scoped] = json.loads(MARKINGS_FILE.read_text("utf-8"))["scoped_markings"]
    return scoped


def _markings(spec: dict, prop: dict) -> set:
    return set(spec.get("markings", ())) | set(prop.get("markings", ()))


class MarkingDeclarationTest(unittest.TestCase):
    """The declared override is the one source of which values carry purchasing_prices."""

    def test_exactly_the_declared_properties_carry_purchasing_prices(self):
        schema = ot.default_schema()
        scoped = _scoped()
        declared = {(p["object_type"], p["property"]) for p in scoped["properties"]}
        relations = {c["relation"] for c in scoped["columns"]}
        carrying = set()
        for tid, spec in schema.object_types.items():
            for prop in spec["properties"]:
                marks = _markings(spec, prop)
                if "purchasing_prices" not in marks:
                    continue
                self.assertNotIn("finance", marks, f"{tid}.{prop['name']}")
                if tid.startswith("Rel"):   # a generated type: one of the declared columns
                    self.assertTrue({b["relation"] for b in spec["bindings"]} & relations, tid)
                else:
                    carrying.add((tid, prop["name"]))
        self.assertEqual(carrying, declared)
        self.assertIn("2026-10-27", scoped["description"])
        self.assertIn("Tanush", scoped["description"])

    def test_every_declared_column_is_generated_with_purchasing_prices(self):
        report = json.loads(COVERAGE.read_text("utf-8"))
        text = json.dumps(report)
        for column in _scoped()["columns"]:
            key = f"{column['relation']}.{column['column']}"
            self.assertRegex(text, re.escape(f'"{key}": "purchasing_prices-marked'), key)

    def test_finance_only_values_keep_the_finance_marking(self):
        schema = ot.default_schema()
        for tid, name in FINANCE_ONLY:
            spec = schema.object_types[tid]
            [prop] = [p for p in spec["properties"] if p["name"] == name]
            marks = _markings(spec, prop)
            self.assertIn("finance", marks, f"{tid}.{name}")
            self.assertNotIn("purchasing_prices", marks, f"{tid}.{name}")

    def test_a_malformed_scoped_marking_fails_closed(self):
        base = json.loads(MARKINGS_FILE.read_text("utf-8"))
        breaks = {
            "held_with differs from MARKING_HELD_WITH": ("held_with", []),
            "an unknown marking": ("marking", "buyer_prices"),
            "replaces a marking no rule applies": ("replaces", "pii"),
            "an unknown key": ("grants", ["staff:purchasing"]),
        }
        for what, (key, value) in breaks.items():
            with self.subTest(what), tempfile.TemporaryDirectory() as tmp:
                for path in MARKINGS_FILE.parent.glob("*.json"):
                    (Path(tmp) / path.name).write_bytes(path.read_bytes())
                doc = copy.deepcopy(base)
                doc["scoped_markings"][0][key] = value
                (Path(tmp) / "markings.json").write_text(json.dumps(doc), "utf-8")
                with self.assertRaises(ov.OverrideError):
                    ov.load_overrides(Path(tmp))


class RawSagePoMoneyTest(unittest.TestCase):
    """Review minor 3: raw Sage PO money columns carried no marking. The generated raw types
    (ontology/schema/generated/, shadowed by the curated PurchaseOrder where one binds the
    relation) are the marking of every raw column."""

    @staticmethod
    def _raw_types() -> dict:
        types: dict = {}
        for path in sorted((ROOT / "ontology/schema/generated").glob("*.json")):
            got = json.loads(path.read_text("utf-8")).get("object_types") or {}
            types.update(got if isinstance(got, dict) else {t["id"]: t for t in got})
        return types

    def test_sage_po_totals_carry_purchasing_prices_and_the_rest_finance(self):
        types = self._raw_types()
        expected = {("RelSagePoHeader", "doctotal"): {"purchasing_prices"},
                    ("RelSagePoHeader", "extended"): {"purchasing_prices"},
                    ("RelSagePoDetail", "extended"): {"purchasing_prices"},
                    ("RelSagePoHeader", "txalloamt"): {"finance"},
                    ("RelSagePoHeader", "extreceive"): {"finance"},
                    ("RelSagePoDetail", "txrecvamt1"): {"finance"},
                    ("RelSagePoReceiptHeader", "doctotal"): {"finance"},
                    ("RelSagePoReceiptDetail", "extended"): {"finance"},
                    ("RelPublicGeneratedPurchaseOrder", "tax_c"): {"finance"}}
        for (tid, name), marks in expected.items():
            spec = types[tid]
            [prop] = [p for p in spec["properties"] if p["name"] == name]
            self.assertEqual(_markings(spec, prop), marks, f"{tid}.{name}")

    def test_no_raw_po_or_sage_money_column_is_unmarked(self):
        # Every raw property of a purchase-order or Sage relation whose name reads as Sage
        # money (amt, doctotal, extended amounts, tax bases, FM tax) carries a money marking,
        # unless the finance rule names it a code.
        money = re.compile(r"amt|^doctotal$|^ext(ended|cancel|receive)$|^taxbase\d$|^tax_c$")
        unless = ov.load_overrides().name_rule("finance").unless
        unmarked = []
        for tid, spec in self._raw_types().items():
            relations = {b["relation"] for b in spec.get("bindings", ())}
            if not any(r.startswith("sage.") or "purchase_order" in r for r in relations):
                continue
            for prop in spec["properties"]:
                if money.search(prop["name"]) and not unless.search(prop["name"]) and \
                        not _markings(spec, prop) & {"finance", "purchasing_prices"}:
                    unmarked.append(f"{tid}.{prop['name']}")
        self.assertEqual(unmarked, [])


class PurchasingGrantTest(unittest.TestCase):
    """The live host grants purchasing_prices to staff:purchasing only: Portal PROC members or
    an Entra job title naming purchas/procure/buyer/sourcing, never an excluded person."""

    HOST = ROOT / "examples/host.live-check.json"
    POLICY = ROOT / "examples/employee_permission_policy.example.json"
    SPEC = ROOT / "devloop/live-checks/permissions.purchasing_prices_grant.json"
    OIDS = [f"cccccccc-0000-4000-8000-{i:012d}" for i in range(70)]

    def setUp(self) -> None:
        from scripts import purchasing_grant_probe as probe
        self.probe = probe
        self.host = json.loads(self.HOST.read_text("utf-8"))
        self.policy = json.loads(self.POLICY.read_text("utf-8"))

    def test_the_host_grants_purchasing_prices_to_purchasing_staff_and_no_one_else(self):
        from operating.employee_permissions import EmployeePermissionPolicy
        from operating.ontology_host import check_marking_grants
        grants = check_marking_grants(self.host["marking_grants"])
        self.assertEqual(grants, {"purchasing_prices": frozenset({"staff:purchasing"})})
        policy = ot.ReadPolicy("plx", {}, grants)
        self.assertEqual(policy.held_markings(Actor("plx", frozenset({"staff:purchasing"}))),
                         {"purchasing_prices"})
        for other in ("staff:operations", "staff:finance", "staff:warehouse"):
            self.assertEqual(policy.held_markings(Actor("plx", frozenset({other}))),
                             frozenset(), other)
        self.assertEqual(self.probe.grant_basis(self.host, self.policy)["department_code"],
                         "PROC")
        rule = EmployeePermissionPolicy.from_json(self.policy).rules["staff:purchasing"]
        self.assertEqual(rule.entra_job_title_keywords, set(self.probe.KEYWORDS))
        self.assertEqual(rule.entra_departments, frozenset())

    def test_a_grant_not_resting_on_procurement_fails_closed(self):
        broken = {
            "grant_not_purchasing_only": [
                ({**self.host, "marking_grants": {"purchasing_prices": ["staff:operations"]}},
                 self.policy),
                ({**self.host, "marking_grants": {"purchasing_prices": [
                    "staff:purchasing", "staff:operations"]}}, self.policy),
                ({k: v for k, v in self.host.items() if k != "marking_grants"}, self.policy)],
            "principal_not_tied_to_procurement": [
                (self.host, self._policy(data_key="operations.sales")),
                (self.host, self._policy(department_ids=[])),
                (self.host, self._policy(entra_departments=["Procurement"]))],
            "job_title_keywords_differ": [
                (self.host, self._policy(entra_job_title_keywords=["purchas"])),
                (self.host, self._policy(entra_job_title_keywords=None))]}
        for code, cases in broken.items():
            for host, policy in cases:
                with self.subTest(code), self.assertRaises(ValueError) as caught:
                    self.probe.grant_basis(host, policy)
                self.assertEqual(str(caught.exception), code)

    def _policy(self, **change):
        policy = copy.deepcopy(self.policy)
        rule = policy["principals"]["staff:purchasing"]
        for key, value in change.items():
            rule.pop(key, None)
            if value is not None:
                rule[key] = value
        return policy

    def test_members_counted_once_across_n_1_10_60(self):
        depts = [{"id": "d1", "code": "PROC", "is_active": True},
                 {"id": "d2", "code": "PROC", "is_active": False},
                 {"id": "d3", "code": "FIN", "is_active": True}]
        for n in (1, 10, 60):
            rows = [{"department_id": "d1", "user_id": f"u{i}"} for i in range(n)]
            rows += rows[:1] + [{"department_id": "d2", "user_id": "x"},
                                {"department_id": "d3", "user_id": "y"}]
            self.assertEqual(self.probe.count_members(depts, rows)["members"], n)
        self.assertFalse(self.probe.count_members(depts[1:], [])["department_found"])

    def _graph(self, titles, *, complete=True):
        users = [{"id": oid, "accountEnabled": i % 7 != 6, "jobTitle": title}
                 for i, (oid, title) in enumerate(zip(self.OIDS, titles))]

        class Api:
            urls: list = []

            def get_pages(self, url, *, max_pages, max_items):
                self.urls.append(url)
                return users, {"complete": complete}
        return Api()

    def test_job_title_counts_and_exclusion_across_n_1_10_60(self):
        from integrations.graph_directory import count_job_titles
        from integrations.people_exclusion import PeopleExclusion
        from integrations.http import SourceUnavailable
        for n in (1, 10, 60):
            titles = [("Buyer", "Outsourcing Lead", None, "Purchasing Manager", "")[i % 5]
                      for i in range(n)]
            enabled = [i for i in range(n) if i % 7 != 6]
            matches = [i for i in enabled if i % 5 in (0, 3)]
            guard = PeopleExclusion([self.OIDS[matches[0]]])
            api = self._graph(titles)
            got = count_job_titles(api, self.probe.KEYWORDS, guard)
            self.assertEqual(got, {"enabled_users": len(enabled),
                                   "job_title_matches": len(matches), "job_title_excluded": 1,
                                   "job_title_members": len(matches) - 1}, n)
            self.assertIn("jobTitle", api.urls[-1])
        with self.assertRaises(SourceUnavailable):
            count_job_titles(self._graph(["Buyer"], complete=False), self.probe.KEYWORDS,
                             PeopleExclusion([self.OIDS[0]]))
        with self.assertRaises(Exception):   # no usable exclusion: nothing is counted
            count_job_titles(self._graph(["Buyer"]), self.probe.KEYWORDS, PeopleExclusion())

    def test_probe_output_meets_its_live_check_spec(self):
        import sys
        from integrations.people_exclusion import PeopleExclusion
        sys.path.insert(0, str(ROOT / "devloop"))
        import live_verify as lv
        spec = json.loads(self.SPEC.read_text("utf-8"))
        lv.validate_spec(spec)

        class Client:
            def __init__(self, members):
                self.members = members

            def read_relation(self, name, *, columns):
                if name == "public.app_department":
                    return {"rows": [{"id": "d1", "code": "PROC", "is_active": True}]}
                return {"rows": [{"department_id": "d1", "user_id": f"u{i}"}
                                 for i in range(self.members)]}
        guard = PeopleExclusion([self.OIDS[0]])
        # The supervisor's 2026-10-27 reading: PROC empty, two titled people.
        passing = self.probe.run({}, Client(0), self._graph(["Buyer", "x", "Sourcing Lead",
                                                             "Procurement"]), guard)
        self.assertEqual(lv.check_expectations(spec, 0, passing), [])
        self.assertEqual((passing["members"], passing["job_title_matches"],
                          passing["job_title_excluded"], passing["holders_at_least"]),
                         (0, 3, 1, 2))
        text = json.dumps(passing)
        for secret in ("u0", "Buyer", "Sourcing Lead", self.OIDS[1]):
            self.assertNotIn(secret, text)   # counts only
        blocked = {
            "portal_api_unset": self.probe.run({}, None, self._graph(["Buyer"]), guard),
            "graph_unset": self.probe.run({}, Client(3), None, guard),
            "excluded_people_oids_absent": self.probe.run({}, Client(3), self._graph(["Buyer"]),
                                                          PeopleExclusion()),
            "purchasing_holders_none": self.probe.run({}, Client(0), self._graph(["Clerk"]),
                                                      guard)}
        for code, doc in blocked.items():
            self.assertEqual(doc["blocked_reason"], code)
            self.assertTrue(lv.check_expectations(spec, 0, doc), code)


class HeldMarkingTest(unittest.TestCase):
    """Every read policy: finance implies purchasing_prices, never the reverse."""

    def test_finance_implies_purchasing_prices_and_purchasing_holds_no_finance(self):
        purchasing = Actor("plx", frozenset({"staff:purchasing"}))
        finance = Actor("plx", frozenset({"staff:finance"}))
        neither = Actor("plx", frozenset({"staff:sales"}))
        grants = {"purchasing_prices": {"staff:purchasing"}, "finance": {"staff:finance"}}
        for policy in (ot.ReadPolicy("plx", {}, grants),
                       ontology_store.ReadPolicy({}, marking_principals=grants)):
            self.assertEqual(policy.held_markings(purchasing), {"purchasing_prices"})
            self.assertEqual(policy.held_markings(finance), {"finance", "purchasing_prices"})
            self.assertEqual(policy.held_markings(neither), frozenset())


class FixtureReaders(unittest.TestCase):
    """plx.object, plx.around and plx.investigate over the projected fixture world."""

    PRINCIPAL = "eval:dev-baseline"   # the fixture runtime's audience grants it the citations

    @classmethod
    def setUpClass(cls) -> None:
        world.FixtureWorld.build()
        cls.actor = Actor("plx", frozenset({cls.PRINCIPAL}))
        reader = ot.StoreReader.open(world.FixtureWorld.product.store)
        with closing(reader.conn):
            cls.lines = [r[0] for r in reader.conn.execute(
                "SELECT object_id FROM objects WHERE object_id LIKE "
                "'PurchaseOrderLine:portal.app_purchase_order_line:%' ORDER BY object_id")]
            cls.orders = [r[0] for r in reader.conn.execute(
                "SELECT object_id FROM objects WHERE object_id LIKE "
                "'PurchaseOrder:portal.app_purchase_order:%' ORDER BY object_id")]
        cls.full = {oid: cls._values(cls.table(store_ddl.MARKINGS), oid)
                    for oid in cls.lines + cls.orders}

    @classmethod
    def table(cls, markings) -> dict:
        reader = ot.StoreReader.open(world.FixtureWorld.product.store)
        who = {cls.PRINCIPAL}
        policy = ot.ReadPolicy(tenant="plx",
                               audiences={k: set(who) for k in reader.audience_keys()},
                               markings={m: set(who) for m in markings})
        base = ot.OntologyTools(reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        table: dict = {"_conn": reader.conn}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(
            table, rows=oi.citation_rows(world.FixtureWorld.runtime), scope=oi.read_scope(base),
            held_markings=lambda actor: policy.held_markings(actor)))
        return table

    @classmethod
    def _values(cls, table: dict, oid: str) -> dict:
        out = table["plx.object"]["handler"]({"object_id": oid, "as_known_at": world.AT,
                                              "response_format": "detailed"}, cls.actor)
        assert "error" not in out, out
        values: dict = {}
        for v in out["values"]:
            values.setdefault(v["property"], []).append(json.dumps(v["value"]))
        return {name: json.dumps(sorted(v)) for name, v in values.items()}

    def _table(self, who: str) -> dict:
        table = self.table(READERS[who])
        self.addCleanup(table.pop("_conn").close)
        return table

    def test_plx_object_shows_po_prices_to_purchasing_and_finance_only(self):
        priced = {"unit_price", "line_total"}
        self.assertTrue(self.lines)
        for oid in self.lines:
            self.assertTrue(priced & {k for k, v in self.full[oid].items()
                                      if re.search(r"\d", v)}, (oid, self.full[oid]))
        for who in READERS:
            table = self._table(who)
            for oid in self.lines:
                seen = self._values(table, oid)
                for name in priced & set(self.full[oid]):
                    if who == "neither":
                        self.assertNotEqual(seen.get(name), self.full[oid][name], (who, name))
                    else:
                        self.assertEqual(seen.get(name), self.full[oid][name], (who, name))
            for oid in self.orders:   # the header freight estimate stays finance
                seen = self._values(table, oid)
                for name in ("shipping_cost_estimate", "duties_cost_estimate"):
                    if name in self.full[oid] and re.search(r"\d", self.full[oid][name]):
                        if who == "finance":
                            self.assertEqual(seen.get(name), self.full[oid][name])
                        else:
                            self.assertNotEqual(seen.get(name), self.full[oid][name], (who, name))

    def test_plx_around_reaches_every_line_for_each_reader_and_no_price(self):
        # A line is an object every reader reaches; only its price values are marked, so
        # the walk loses or adds no line for any reader and states no price value.
        reached = {}
        for who in READERS:
            out = self._table(who)["plx.around"]["handler"](
                {"from": self.orders[0], "as_known_at": world.AT, "response_format": "detailed",
                 "path": [{"link_type": "PurchaseOrderLine.line_of", "direction": "reverse"}]},
                self.actor)
            self.assertNotIn("error", out)
            reached[who] = sorted({e["from"] for e in out["edges"]})
            text = json.dumps(out)
            for oid in reached[who]:
                for value in json.loads(self.full.get(oid, {}).get("unit_price", "[]")):
                    self.assertNotIn(f'"value": {value}', text)
        self.assertTrue(reached["neither"])
        self.assertEqual(reached["purchasing"], reached["neither"])
        self.assertEqual(reached["finance"], reached["neither"])

    def test_landed_cost_goods_from_po_line_prices_for_purchasing(self):
        golden = "PO100901"
        answers = {}
        for who in READERS:
            result = self._table(who)["plx.investigate"]["handler"](
                {"family": "landed_cost", "root": {"identifier": golden},
                 "as_known_at": world.AT}, self.actor)
            world._ok(self, result)
            answers[who] = result
        def prices(result):
            return sorted(str(e["value"]) for e in result["answer"]
                          if e["kind"] == "property" and e.get("property") == "unit_price")
        self.assertEqual(prices(answers["purchasing"]), prices(answers["finance"]))
        self.assertTrue(prices(answers["purchasing"]))
        self.assertEqual(prices(answers["neither"]), [])
        purchasing_gaps = {g["reason"] for g in answers["purchasing"]["gaps"]}
        self.assertNotIn("unit_price_withheld", purchasing_gaps)
        [held] = [g for g in answers["neither"]["gaps"] if g["reason"] == "marking_withheld"
                  and g["subject"].startswith("PurchaseOrderLine.unit_price")]
        self.assertIn("purchasing_prices marking, which this reader does not hold",
                      held["detail"])
        # The receipt actuals and the header freight estimate stay finance: withheld from
        # purchasing and named so, never reported as not recorded.
        self.assertIn("receipt_actual_withheld", purchasing_gaps)
        self.assertIn("po_freight_estimate_withheld", purchasing_gaps)
        self.assertNotIn("po_freight_not_recorded", purchasing_gaps)
        self.assertNotIn("po_freight_not_recorded",
                         {g["reason"] for g in answers["finance"]["gaps"]})
        goods = [s for e in answers["purchasing"]["answer"] if e["kind"] == "computed"
                 for s in e.get("steps") or () if s.startswith("goods total:")]
        self.assertTrue(goods, "purchasing gets goods steps from the PO line prices")
        # The header freight estimate (finance) reaches finance only.
        shipping = [str(e["value"]) for e in answers["finance"]["answer"]
                    if e.get("property") == "shipping_cost_estimate"]
        self.assertTrue(shipping)
        for who in ("purchasing", "neither"):
            self.assertFalse([e for e in answers[who]["answer"]
                              if e.get("property") == "shipping_cost_estimate"], who)
        neither = json.dumps(answers["neither"])
        for price in prices(answers["finance"]):
            self.assertIsNone(re.search(rf"(?<![0-9.]){re.escape(price)}(?![0-9])", neither))


class PurchasingHistoryEstimateTest(unittest.TestCase):
    """InvestigateTools' actuals gate at the engine boundary: purchasing gets the labelled
    company-wide estimate, never a recorded charge or a per-supplier rate."""

    RECORDS = {"PO900001": {
        "duty": {"rate": "0.05", "gap": None, "charges": 2, "linked_by": ["po_number"],
                 "includes_brokerage": True},
        "estimate": {"freight": hist._hist("0.33", level="supplier", q3="0.4")},
        "incoterm": {"po": "FOB", "vendor_default": None}}}

    def reader(self, held, defaults=None) -> object:
        tools = SimpleNamespace(
            held_markings=lambda actor: set(held),
            landed_actuals=hist.provider(self.RECORDS, default=defaults))
        return oi.InvestigateTools._actuals_reader(tools, SimpleNamespace(actor=None))

    def test_purchasing_gets_the_company_wide_estimate_and_no_ap_charge(self):
        finance = hist.run(hist.EstimateFromHistoryTest.LINES,
                           landed_actuals=self.reader({"finance", "purchasing_prices"}))
        # Finance: the recorded duty (0.05) and the supplier freight rate (0.33) apply.
        self.assertIn("part duty: actual", hist._steps(finance))
        self.assertIn("rate 0.33", hist._part(finance, "freight"))
        purchasing = hist.run(hist.EstimateFromHistoryTest.LINES,
                              landed_actuals=self.reader({"purchasing_prices"}))
        e = purchasing["by_name"]
        # goods 400; freight 0.2 x 400 = 80 and duty 0.015 x 400 = 6, both company-wide.
        self.assertEqual(e["landed_cost_estimate"]["value"], "486")
        self.assertIn("all suppliers (company-wide)", hist._part(purchasing, "freight"))
        self.assertIn("all suppliers (company-wide)", hist._part(purchasing, "duty"))
        self.assertIsNone(e["landed_cost"]["value"])
        self.assertTrue(hist.est._gap(purchasing, "actual_charges_withheld"))
        text = json.dumps(purchasing)
        for leak in ("part duty: actual", "rate 0.05", "rate 0.33", "purchase order's supplier"):
            self.assertNotIn(leak, text)

    def test_a_supplier_level_default_is_never_given_to_purchasing(self):
        out = self.reader({"purchasing_prices"},
                          defaults={"duty": hist._hist("0.02", level="supplier"),
                                    "freight": hist.FREIGHT})("PO900001".split())
        self.assertEqual(set(out["default_estimates"]), {"freight"})
        self.assertEqual(out["records"], {})
        self.assertEqual(out["known"], [])

    def test_a_reader_with_neither_marking_gets_nothing(self):
        out = self.reader(set(OTHERS))(["PO900001"])
        self.assertEqual(out, {"withheld": "finance"})
        result = hist.run(hist.EstimateFromHistoryTest.LINES, landed_actuals=self.reader(OTHERS))
        self.assertNotIn("ESTIMATED FROM PLX HISTORY", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
