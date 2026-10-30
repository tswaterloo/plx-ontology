"""The ``tools.*`` live checks (scripts/live_employee_tools.py) over a host-built projection.

Each spec's own argv runs in-process through ``operating.host`` cycles on a BC-only host
configuration over the BC fake of ``integrations/tests/test_bc_capture.py`` (synthetic
records), and its output is judged by ``live_verify.check_expectations``: the check passes
on the fixed product and fails on a reproduction of the defect it guards.
Evidence label: injected-transport.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from verify import live_verify
from integrations.tests import test_bc_capture as tb
from integrations.tests.test_live_source_adapters import ENV
from ontology.tests.test_project_bc import MAN, gid, purchase_world, record
from operating import case_runtime
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from scripts import live_employee_tools as le

SPECS = live_verify.ROOT / live_verify.SPECS_DIR_NAME
UNAVAILABLE = (503, {}, b'{"error":{"code":"Unavailable"}}')
TOO_MANY = (429, {"Retry-After": "1"}, b'{"error":{"code":"TooManyRequests"}}')
CHECKS = ("tools.schema_paging", "tools.find_unknown_property", "tools.investigate_bc_claims",
          "tools.investigate_inline_plans")
# The ~23 min investigate check runs in the daily set.
CADENCE = {"tools.schema_paging": "hourly", "tools.find_unknown_property": "hourly",
           "tools.investigate_bc_claims": "daily", "tools.investigate_inline_plans": "daily"}


class Undeclared(BaseException):
    """The guard's stop: ``live_verify.ENV_GUARD`` ends the command at that read."""


class GuardedEnv(dict):
    """``os.environ`` of a spec command under ``live_verify.ENV_GUARD``: the child env the
    verifier built, and any read of a hidden (set but undeclared) credential stops."""

    def __init__(self, env, hidden) -> None:
        super().__init__(env)
        self.hidden = hidden

    def __getitem__(self, key):
        if key in self.hidden:
            raise Undeclared(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        if key in self.hidden:
            self[key]
        return super().__contains__(key)

    def get(self, key, default=None):
        return self[key] if key in self else default


def world() -> tb.FakeBc:
    """One PO -> receipt -> invoice per company, a deposit (vendor payment journal line)
    and a G/L entry on the first company's invoice, plus a sales order carrying a customer
    PO and its shipment (BC API v2.0 field names)."""
    fake = purchase_world()
    journal = gid(1, 40)
    fake.data[(MAN, "vendorPaymentJournals")] = [record(journal, code="PAYMENT")]
    fake.lines[(MAN, "vendorPaymentJournals", journal)] = [record(
        gid(1, 41), journalId=journal, journalDisplayName="PAYMENT", vendorId=gid(1, 5),
        vendorNumber="V-1", postingDate="2026-09-10", documentNumber="DEP-1",
        amount="#300.00", appliesToInvoiceId=gid(1, 4), appliesToInvoiceNumber="PI-1")]
    fake.data[(MAN, "generalLedgerEntries")] = [record(
        gid(1, 42), entryNumber=7, documentNumber="PI-1", documentType="Invoice",
        accountNumber="5000", description="Widget", debitAmount="#1200.00",
        creditAmount="#0", postingDate="2026-09-21")]
    fake.data[(MAN, "salesOrders")] = [record(
        gid(1, 21), number="SO-1", externalDocumentNumber="CPO-77", customerNumber="C-1",
        status="Open", orderDate="2026-09-01")]
    fake.data[(MAN, "salesShipments")] = [record(
        gid(1, 22), number="SS-1", orderNumber="SO-1", customerNumber="C-1",
        shipmentDate="2026-09-25", postingDate="2026-09-25")]
    return fake


class LiveEmployeeToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        def refuse(*args, **kwargs):
            raise AssertionError("network access attempted in an offline test")
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = mock.patch(target, refuse)
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory(prefix="plx-live-employee-tools-")
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        cls.books = cls.root / "books.json"
        cls.books.write_text(json.dumps({"schema": "plx-bc-company-books/v1",
                                         "environments": tb.BOOKS}), "utf-8")
        cls.runs = 0

    def run_spec(self, spec_id: str, fake: tb.FakeBc | None = None,
                 daily: Path | None = None) -> tuple[dict, int, dict]:
        """The spec's argv as the verifier expands it (``daily``: the previous daily
        report, else its ``--previous-report`` is dropped), run in-process."""
        spec = json.loads((SPECS / f"{spec_id}.json").read_text("utf-8"))
        self.assertEqual(live_verify.validate_spec(spec, stem=spec_id), [])
        self.assertEqual(spec["cadence"], CADENCE[spec_id])
        type(self).runs += 1
        tmp = self.root / f"run-{self.runs}"
        tmp.mkdir()
        argv = live_verify._expand(spec["command"][3:], str(tmp), daily)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = le.main(argv + ["--bc-books", str(self.books)], environ=ENV,
                           sender=fake or world(), sleep=lambda seconds: None)
        document = json.loads(out.getvalue())
        self.assertNotIn("error", document)
        self.assert_no_values(out.getvalue())
        return spec, code, document

    def verdict(self, spec: dict, code: int, document: dict) -> dict:
        """The verifier's own result entry for this output (``live_verify.run_spec`` over
        a finished process that printed it)."""
        class Finished:
            returncode = code

            def communicate(self, timeout=None):
                return json.dumps(document).encode(), b""
        with mock.patch.object(live_verify.subprocess, "Popen", lambda *a, **k: Finished()):
            return live_verify.run_spec(spec, environ=ENV)

    def run_guarded(self, spec_id: str, fake: tb.FakeBc | None = None) -> dict:
        """``live_verify.run_spec`` over the full test env: the verifier builds the child
        env and its hidden names itself; the command then runs in-process (over the BC
        fake) under that env with every hidden read guarded as ``ENV_GUARD`` does."""
        spec = json.loads((SPECS / f"{spec_id}.json").read_text("utf-8"))
        books, sender = self.books, fake or world()

        class InProcess:
            def __init__(self, argv, env, **kwargs):
                self.assert_guarded(argv)
                hidden = frozenset(filter(None, argv[3].split(",")))
                out, self.stderr = io.StringIO(), b""
                try:
                    with mock.patch.object(os, "environ", GuardedEnv(env, hidden)), \
                            contextlib.redirect_stdout(out):
                        self.returncode = le.main([*argv[5:], "--bc-books", str(books)],
                                                  sender=sender, sleep=lambda seconds: None)
                except Undeclared as read:
                    self.returncode = live_verify.UNDECLARED_ENV_EXIT
                    self.stderr = json.dumps({"error": "UndeclaredEnv",
                                              "name": read.args[0]}).encode()
                self.stdout = out.getvalue().encode()

            @staticmethod
            def assert_guarded(argv):
                assert argv[1:3] == ["-c", live_verify.ENV_GUARD], argv[:3]
                assert argv[4] == "scripts.live_employee_tools", argv[4]

            def communicate(self, timeout=None):
                return self.stdout, self.stderr
        with mock.patch.object(live_verify.subprocess, "Popen", InProcess):
            return live_verify.run_spec(spec, environ=ENV)

    def assert_no_values(self, text: str) -> None:
        """No record value, number, name or citation reaches the output."""
        for value in ("PO-1001", "CPO-77", "SO-1", "ITEM-A", "Widget", "V-1", "bc://",
                      gid(1, 1)):
            self.assertNotIn(value, text)

    def test_each_check_reaches_its_body_with_only_its_declared_env(self):
        """Live b5332d0: all three errored undeclared_env MICROSOFT_GRAPH_CLIENT_SECRET (the
        served tools built Graph readers on a host configuring no Graph source). Through
        the verifier's own env filter, with the Graph credentials set but undeclared, each
        reaches its body and passes on the fixture."""
        self.assertIn("MICROSOFT_GRAPH_CLIENT_SECRET", ENV)
        for spec_id in CHECKS:
            with self.subTest(spec_id):
                entry = self.run_guarded(spec_id)
                self.assertNotIn("undeclared_env", entry["detail"])
                self.assertEqual(entry["result"], "pass", entry["detail"])
        # The guard is live: a host reading an undeclared credential is still an error.
        from operating import host as hm
        served = hm.Host.served_tools

        def reads_graph(host, actor):
            host.environ.get("MICROSOFT_GRAPH_CLIENT_SECRET")
            return served(host, actor)
        with mock.patch.object(hm.Host, "served_tools", reads_graph):
            entry = self.run_guarded("tools.schema_paging")
        self.assertEqual((entry["result"], entry["detail"]),
                         ("error", {"undeclared_env": "MICROSOFT_GRAPH_CLIENT_SECRET"}))

    def test_schema_paging_reaches_every_type_and_fails_a_page_without_cursor(self):
        spec, code, doc = self.run_spec("tools.schema_paging")
        self.assertEqual(live_verify.check_expectations(spec, code, doc), [])
        self.assertGreater(doc["sections"]["object_types"]["declared"], 100)
        original = ot.OntologyTools._schema_tool

        def cursorless(tools, arguments, actor):  # the defect: truncated, next_cursor null
            out = original(tools, arguments, actor)
            out["completeness"]["next_cursor"] = None
            return out
        with mock.patch.object(ot.OntologyTools, "_schema_tool", cursorless):
            spec, code, doc = self.run_spec("tools.schema_paging")
        failures = live_verify.check_expectations(spec, code, doc)
        self.assertEqual(code, 1)
        for path in ("truncated_without_cursor", "missing_total",
                     "sections.object_types.listed"):
            self.assertTrue(any(path in f for f in failures), (path, failures))

    def test_find_names_valid_properties_and_no_portal_freshness_gap(self):
        spec, code, doc = self.run_spec("tools.find_unknown_property")
        self.assertEqual(live_verify.check_expectations(spec, code, doc), [])
        self.assertEqual(doc["freshness_gaps"], {"source_not_configured":
                                                 doc["portal_not_configured_gaps"]})
        # (b) a filter on an undeclared property answered as anything but unknown_property,
        # or with a hint that names no declared property, fails the check.
        original = ot.OntologyTools._find_store

        def silent_miss(tools, arguments, actor):
            try:
                return original(tools, arguments, actor)
            except ot.ToolError as exc:
                if exc.code != "unknown_property":
                    raise
                raise ot.ToolError("not_found", "no visible match") from None
        with mock.patch.object(ot.OntologyTools, "_find_store", silent_miss):
            spec, code, doc = self.run_spec("tools.find_unknown_property")
        self.assertEqual(doc["missed_outcomes"], {"not_found": doc["types_probed"]})
        self.assertIn("unknown_property_missed != expected",
                      " ".join(live_verify.check_expectations(spec, code, doc)))
        # A hint naming one declared property in passing does not list the declared set.
        with mock.patch.object(ot, "_valid_properties",
                               lambda t, names: f"filter on {names[0]}, see plx.schema"):
            spec, code, doc = self.run_spec("tools.find_unknown_property")
        self.assertLess(doc["hint_names_valid"], doc["unknown_property"])
        self.assertTrue(any("hint_names_valid" in f for f in
                            live_verify.check_expectations(spec, code, doc)))
        # (c) a host whose Portal reads as configured though nothing was read from it
        # (the defect) carries portal_api freshness_unknown on every find.
        init = case_runtime.CaseRuntime.__init__

        def configured(runtime, *args, **kwargs):
            init(runtime, *args, **kwargs)
            runtime.client_configured = True
        with mock.patch.object(case_runtime.CaseRuntime, "__init__", configured):
            spec, code, doc = self.run_spec("tools.find_unknown_property")
        failures = " ".join(live_verify.check_expectations(spec, code, doc))
        self.assertEqual(code, 1)
        self.assertIn("portal_freshness_gaps", failures)
        self.assertIn("portal_not_configured_gaps", failures)

    def test_prose_words_in_a_hint_are_not_listed_names(self):
        """Review of employee-tools-followups: a declared property named like a word of the
        hint's prose ('filter', 'schema', 'lists') counted as listed and false-failed a
        hint that lists the rest with a correct (+N more). Only the listed names count."""
        long = [f"declared_property_{n:03d}" for n in range(60)]
        types = {"t.long": long + ["filter", "schema", "lists", "plx"],
                 "t.short": ["one", "filter", "properties"], "t.none": []}
        schema = mock.Mock(object_types={t: {"properties": [{"name": n} for n in names]}
                                         for t, names in types.items()})
        hints = {t: ot._valid_properties(t, names) for t, names in types.items()}
        hints["t.wrong"] = "filter on one of t.wrong's properties: a, b, c"
        schema.object_types["t.wrong"] = {"properties": [{"name": n} for n in "abd"]}
        self.assertIn("(+", hints["t.long"])

        def call(tool, arguments):
            return {"error": {"code": "unknown_property",
                              "hint": hints[arguments["object_type"]]}}
        report = le.find_check(call, schema, [])
        # Every real hint lists its type's names; the one naming an undeclared one does not.
        self.assertEqual((report["unknown_property"], report["hint_names_valid"]), (4, 3))

    def test_investigate_claims_pass_for_the_manager_and_fail_when_uncited(self):
        spec, code, doc = self.run_spec("tools.investigate_bc_claims")
        self.assertEqual(live_verify.check_expectations(spec, code, doc), [])
        families = doc["families"]
        self.assertEqual(set(families), set(doc["families_run"]))
        self.assertTrue({"three_way_match", "customer_po_status", "landed_cost"} <= set(families))
        # LOG-09: the landed-cost walk on each sampled BC purchase order records its stages
        # and typed gaps. The engine runs (never engine_failed). This reader holds neither
        # finance nor purchasing_prices, so the line price is named withheld, never read as
        # an unpriced line; the finance-marked deposit and G/L stages are withheld from this
        # reader, named by step, never counted.
        landed = families["landed_cost"]
        self.assertEqual((landed["roots"], landed["errors"]), (2, 0))
        self.assertEqual(landed["stages"], {"stage_bc_receipt": 2, "stage_purchase_invoice": 2})
        self.assertEqual(landed["stages_reached"], landed["stages"])
        self.assertNotIn("engine_failed", landed["gap_codes"])
        self.assertTrue({"unit_price_withheld", "receipt_actual_withheld"}
                        <= set(landed["gap_codes"]))
        self.assertNotIn("unpriced_line", landed["gap_codes"])
        self.assertTrue({"payments:marking_withheld", "gl_entries:marking_withheld"}
                        <= set(landed["stage_gaps"]))
        recorded = self.verdict(spec, code, doc)["detail"]
        self.assertNotIn("fields_truncated", recorded)
        self.assertEqual(recorded["fields"]["families.landed_cost"]["stage_gaps"],
                         landed["stage_gaps"])
        self.assertEqual(recorded["fields"]["families_run"], doc["families_run"])
        self.assertEqual(families["customer_po_status"]["value_checks"], {"passed": 3})
        self.assertGreaterEqual(families["three_way_match"]["value_checks"]["passed"], 1)
        # Both companies' POs are roots; the outsider is refused every one of them.
        self.assertEqual(families["three_way_match"]["roots"], 2)
        self.assertEqual(doc["outsider_states"], {"blocked": doc["outsider_probed"]})
        self.assertEqual(doc["clearance_source"],
                         "examples/employee_permission_policy.example.json")
        self.assertEqual(self.verdict(spec, code, doc)["detail"]["fields"]["clearance_source"],
                         doc["clearance_source"])
        # The defect: the claim gate cannot re-read what the answer cites.
        with mock.patch.object(oi, "citation_rows", lambda runtime: lambda handle, actor: None):
            spec, code, doc = self.run_spec("tools.investigate_bc_claims")
        failures = " ".join(live_verify.check_expectations(spec, code, doc))
        self.assertEqual(code, 1)
        self.assertGreater(doc["claim_uncited"], 0)
        self.assertIn("claim_uncited", failures)
        self.assertIn("roots_failed", failures)

    def test_inline_plans_answer_with_citations_or_refuse_with_their_typed_reason(self):
        spec, code, doc = self.run_spec("tools.investigate_inline_plans")
        self.assertEqual(live_verify.check_expectations(spec, code, doc), [])
        examples = json.loads(oi.INLINE_EXAMPLES.read_text("utf-8"))["examples"]
        self.assertEqual(doc["plans_run"], [e["plan"]["id"] for e in examples])
        outcomes = {plan: stats["outcomes"] for plan, stats in doc["plans"].items()}
        # Both companies' POs and vendors are roots; the one sales shipment is.
        self.assertEqual(outcomes, {
            "supplier_orders_past_due": {"answered_cited": 2},
            "order_lines_not_received": {"answered_uncited": 2},  # every line received
            "shipment_documents": {"answered_cited": 1},
            "order_carrier": {"refused:plan_undeclared_link": 2},
            "order_due_date": {"refused:plan_unknown_property": 2}})
        self.assertEqual(doc["plans"]["order_lines_not_received"]["gap_codes"]["no_match"], 2)
        self.assertEqual(doc["outsider_states"], {"blocked": doc["outsider_probed"]})
        recorded = self.verdict(spec, code, doc)
        self.assertEqual(recorded["result"], "pass", recorded["detail"])
        self.assertNotIn("fields_truncated", recorded["detail"])
        # The defect: an inline plan runs unchecked. The undeclared link is then a tool
        # error of the walk, never the typed refusal the planner's hint repairs.
        with mock.patch.object(oi, "plan_problems", lambda *args: []):
            spec, code, doc = self.run_spec("tools.investigate_inline_plans")
        self.assertEqual(code, 1)
        self.assertGreater(doc["plans_unexpected"], 0)
        self.assertIn("plans_unexpected", " ".join(
            live_verify.check_expectations(spec, code, doc)))

    def test_families_are_the_plans_rooted_on_what_the_capture_holds(self):
        """A plan added to ontology/plans runs with no script edit when the capture holds
        its root type, and one rooted on a type no cycle captures does not run; a broken
        plan fails the run closed."""
        plans = self.root / "plans"
        plans.mkdir()
        for path in oi.PLAN_DIR.glob("*.json"):
            (plans / path.name).write_text(path.read_text("utf-8"), "utf-8")
        added = json.loads((oi.PLAN_DIR / "three_way_match.json").read_text("utf-8"))
        added.update(id="probe_added", families=["F98"])
        supplier = {**added, "id": "probe_supplier", "families": ["F99"],
                  "root": {"object_types": ["Supplier"]}, "step_budget": 4,
                  "steps": [{"id": "root_read", "tool": "plx.object", "from": "root",
                             "bind": "object_id"}],
                  "evidence": [{"name": "supplier_code", "kind": "property",
                                "object_type": "Supplier", "properties": ["code"]}]}
        for doc in (added, supplier):
            (plans / f"{doc['id']}.json").write_text(json.dumps(doc), "utf-8")
        with mock.patch.object(oi.load_plans, "__defaults__", (plans,)):
            spec, code, doc = self.run_spec("tools.investigate_bc_claims")
            self.assertEqual(live_verify.check_expectations(spec, code, doc), [])
            self.assertIn("probe_added", doc["families_run"])
            self.assertNotIn("probe_supplier", doc["families_run"])
            self.assertEqual(doc["families"]["probe_added"]["roots"], 2)
            self.assertGreaterEqual(doc["families"]["probe_added"]["claims"], 1)
            (plans / "probe_supplier.json").write_text("{", "utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = le.main(["investigate", "--state-dir", str(self.root / "broken" / "s"),
                                "--cycle", "purchaseOrders", "--bc-books", str(self.books)],
                               environ=ENV, sender=world(), sleep=lambda seconds: None)
        self.assertEqual((code, json.loads(out.getvalue())["error"]), (1, "PlanError"))

    def test_families_rotate_oldest_evidence_first_and_a_cut_keeps_what_ran(self):
        """f8e4e5ed5f28 (2): families ran in sorted order, so a budget stop always cut
        three_way_match. They now run oldest evidence first (families_age from the
        previous daily report), a cut keeps the families that ran, and the verifier's
        recorded families_age orders the next run."""
        previous = self.root / "previous-daily.json"
        previous.write_text(json.dumps({"checks": [{
            "id": "tools.investigate_bc_claims", "detail": {"fields": {"families_age": {
                "three_way_match": 5, "landed_cost": 1, "chain": 0, "commercial_chain": 0,
                "customer_po_status": 0, "decision_evidence": 0, "impact_chain": 0,
                "item_lineage": 0, "late_po": 0, "order_margin": 0, "people_on_record": 0,
                "order_quantities": "x"}}}}]}),
            "utf-8")
        call = le.Caller.__call__

        def cut_after_six(caller, name, arguments):
            if caller.report["calls"] >= 6:  # the budget stop, mid-run
                raise le.BudgetExhausted()
            return call(caller, name, arguments)
        with mock.patch.object(le.Caller, "__call__", cut_after_six):
            spec, code, doc = self.run_spec("tools.investigate_bc_claims", daily=previous)
        # An age that is no run count is dropped: that family runs first, as never aged.
        # people_on_record (8e63046f6b) is one more family, aged 0 like the others.
        self.assertEqual(doc["families_run"], [
            "order_quantities", "three_way_match", "landed_cost", "chain", "commercial_chain",
            "customer_po_status", "decision_evidence", "impact_chain", "item_lineage",
            "late_po", "order_margin", "people_on_record"])
        self.assertEqual((code, doc["budget_exhausted"]), (1, True))
        self.assertEqual(set(doc["families"]),
                         {"order_quantities", "three_way_match", "landed_cost"})
        self.assertEqual(doc["families"]["three_way_match"]["roots"], 2)
        self.assertGreaterEqual(doc["families"]["three_way_match"]["claims"], 1)
        self.assertEqual(doc["families_age"], {
            "order_quantities": 0, "three_way_match": 0, "landed_cost": 2, "chain": 1,
            "commercial_chain": 1, "customer_po_status": 1, "decision_evidence": 1,
            "impact_chain": 1, "item_lineage": 1, "late_po": 1, "order_margin": 1,
            "people_on_record": 1})
        entry = self.verdict(spec, code, doc)
        self.assertEqual(entry["result"], "fail")
        self.assertEqual(entry["detail"]["fields"]["families_age"], doc["families_age"])
        recorded = self.root / "recorded-daily.json"
        recorded.write_text(json.dumps({"checks": [{"id": spec["id"], **entry}]}), "utf-8")
        spec, code, doc = self.run_spec("tools.investigate_bc_claims", daily=recorded)
        self.assertEqual(live_verify.check_expectations(spec, code, doc), [])
        self.assertEqual(doc["families_run"], [
            "landed_cost", "chain", "commercial_chain", "customer_po_status",
            "decision_evidence", "impact_chain", "item_lineage", "late_po", "order_margin",
            "people_on_record", "order_quantities", "three_way_match"])
        self.assertEqual(set(doc["families_age"].values()), {0})

    def test_a_family_bc_holds_no_root_of_is_blocked_and_a_dropped_one_fails(self):
        """f8e4e5ed5f28 (3): a plan rooted on a captured type with no projected record
        failed the run. When BC answered every set binding its root types in full, in
        every pinned company, and holds none, it is blocked (no_root_records); when a
        pinned company was left unread, or the capture holds them but the projection does
        not, it still fails."""
        def no_sales_orders():
            fake = world()
            for company in tb.CO:
                fake.data[(company, "salesOrders")] = []
            return fake
        spec, code, doc = self.run_spec("tools.investigate_bc_claims", no_sales_orders())
        empty = {f for f, stats in doc["families"].items() if stats.get("root_source_empty")}
        # Customer POs bind only the sales orders; sales orders also bind the shipments.
        self.assertEqual(empty, {"customer_po_status"})
        self.assertEqual(doc["families"]["customer_po_status"]["roots"], 0)
        self.assertEqual((code, doc["sources_blocked"]), (0, "no_root_records"))
        self.assertEqual(doc["bc_companies_unread"], [])
        entry = self.verdict(spec, code, doc)
        self.assertEqual(entry["result"], "blocked")
        self.assertEqual(live_verify.check_expectations(
            {**spec, "expect": spec["expect_blocked"]}, code, doc), [])
        # A budget stop in the second company's shipments left the
        # third company's sales orders unread (no run), and the two read in full were
        # taken for every company. Unread, no set is empty: the stop fails the run.
        from integrations import bc_read
        read_set = bc_read.BcCapture._capture_set

        def budget_stop(capture, actor, company_id, entity_set, *args, **kwargs):
            if (company_id, entity_set) == (tb.CO[1], "salesShipments"):
                capture._deadline = capture._monotonic()  # spent: the unit pauses, stops
            return read_set(capture, actor, company_id, entity_set, *args, **kwargs)
        with mock.patch.object(bc_read.BcCapture, "_capture_set", budget_stop):
            spec, code, doc = self.run_spec("tools.investigate_bc_claims", no_sales_orders())
        self.assertEqual(doc["bc_capture"]["reasons"], ["budget"])
        self.assertEqual(doc["bc_companies_unread"], [tb.BOOKS["production"][tb.CO[2]]])
        self.assertNotIn("root_source_empty", doc["families"]["customer_po_status"])
        self.assertEqual(code, 1)
        self.assertNotIn("sources_blocked", doc)
        entry = self.verdict(spec, code, doc)
        self.assertEqual(entry["result"], "fail")
        self.assertEqual(entry["detail"]["fields"]["bc_companies_unread"], ["consol"])
        # The same reach with an uncited answer is wrong, never blocked.
        with mock.patch.object(oi, "citation_rows", lambda runtime: lambda handle, actor: None):
            spec, code, doc = self.run_spec("tools.investigate_bc_claims", no_sales_orders())
        self.assertEqual(code, 1)
        self.assertNotIn("sources_blocked", doc)
        # The defect: BC holds the sales orders but the projection lost their customer POs.
        cycles = le.run_cycles

        def projection_drops_customer_pos(state, *args, **kwargs):
            out = cycles(state, *args, **kwargs)
            from operating import host as hm
            with sqlite3.connect(hm.projection_path(state)) as conn:
                conn.execute("DELETE FROM objects WHERE object_type = 'CustomerPurchaseOrder'")
            return out
        with mock.patch.object(le, "run_cycles", projection_drops_customer_pos):
            spec, code, doc = self.run_spec("tools.investigate_bc_claims")
        self.assertEqual(doc["families"]["customer_po_status"]["roots"], 0)
        self.assertNotIn("root_source_empty", doc["families"]["customer_po_status"])
        self.assertEqual(code, 1)
        self.assertNotIn("sources_blocked", doc)
        self.assertEqual(self.verdict(spec, code, doc)["result"], "fail")

    def test_many_long_gap_codes_stay_under_the_detail_cap(self):
        """f8e4e5ed5f28 (4): live gap codes could push the recorded detail past the
        verifier's 4000-char cap (fields_truncated). Forty distinct 62-char gap and
        reason codes per answer (each map under the verifier's 50-key summary) fold
        into ``other``, every gap still counted."""
        call = le.Caller.__call__
        extra = [f"stage_{chr(97 + n // 26)}{chr(97 + n % 26)}_" + "_".join(["withheld"] * 6)
                 for n in range(40)]  # no digit: the scrubber redacts long codes with one

        def many_codes(caller, name, arguments):
            out = call(caller, name, arguments)
            if name == "plx.investigate" and "answerability" in out:
                out["gaps"] = [*out.get("gaps", ()),
                               *({"subject": "x", "reason": r} for r in extra)]
                out["answerability"]["reasons"] = [*out["answerability"].get("reasons", ()),
                                                   *extra]
            return out
        with mock.patch.object(le.Caller, "__call__", many_codes):
            spec, code, doc = self.run_spec("tools.investigate_bc_claims")
        self.assertEqual(live_verify.check_expectations(spec, code, doc), [])
        for family, stats in doc["families"].items():
            self.assertLessEqual(len(json.dumps(stats, sort_keys=True)), le.FAMILY_CHARS)
            self.assertGreater(stats["codes_folded"], 0, family)
            self.assertGreaterEqual(sum(stats["gap_codes"].values()), 40 * stats["roots"])
        entry = self.verdict(spec, code, doc)
        self.assertEqual(entry["result"], "pass")
        self.assertNotIn("fields_truncated", entry["detail"])
        self.assertLessEqual(len(json.dumps(entry["detail"], sort_keys=True)),
                             live_verify.MAX_DETAIL_CHARS)
        self.assertEqual(entry["detail"]["fields"]["families.landed_cost"]["codes_folded"],
                         doc["families"]["landed_cost"]["codes_folded"])

    def test_a_throttle_blocks_only_what_it_left_unread(self):
        # 503 on the sales shipments (cycle 1), 429 on the purchase orders (cycle 2): no
        # purchase order is projected, so three_way_match reaches no root, and only the
        # throttle explains it.
        fake = world()
        fake.fail(r"/salesShipments(\?|$)", *[UNAVAILABLE] * 40)
        fake.fail(r"/purchaseOrders(\?|$)", *[TOO_MANY] * 40)
        spec, code, doc = self.run_spec("tools.investigate_bc_claims", fake)
        self.assertEqual((code, doc["sources_blocked"]), (0, "upstream_throttled"))
        self.assertEqual(doc["families"]["three_way_match"]["roots"], 0)
        self.assertEqual(doc["upstream_throttled"]["http_statuses"], [429, 503])
        self.assertTrue({"salesShipments", "purchaseOrders", "items"}
                        <= set(doc["upstream_throttled"]["entity_sets"]))
        self.assertEqual({(p["error_code"], p["http_status"], p["cause"])
                          for p in live_verify.blocked_parts(doc)},
                         {("upstream_throttled", 429, "upstream_outage"),
                          ("upstream_throttled", 503, "upstream_outage")})
        self.assertEqual(self.verdict(spec, code, doc)["result"], "blocked")
        # A throttle beside another upstream failure (a 500 on the shipments) does not
        # explain the unreached roots alone: it fails.
        fake = world()
        fake.fail(r"/salesShipments(\?|$)", *[(500, {}, b'{"error":{"code":"Internal"}}')] * 40)
        fake.fail(r"/purchaseOrders(\?|$)", *[TOO_MANY] * 40)
        spec, code, doc = self.run_spec("tools.investigate_bc_claims", fake)
        self.assertEqual(code, 1)
        self.assertNotIn("sources_blocked", doc)
        self.assertEqual(self.verdict(spec, code, doc)["result"], "fail")
        # A capture throttled part-way that still reaches every family is blocked, never
        # a pass; with an uncited answer it fails. (The G/L entries root no family alone;
        # items would: they project the Products item_lineage starts from.)
        for uncited, result in ((False, "blocked"), (True, "fail")):
            fake = world()
            fake.fail(r"/generalLedgerEntries(\?|$)", *[TOO_MANY] * 40)
            with mock.patch.object(oi, "citation_rows",
                                   (lambda runtime: lambda handle, actor: None) if uncited
                                   else oi.citation_rows):
                spec, code, doc = self.run_spec("tools.investigate_bc_claims", fake)
            self.assertEqual(doc["upstream_throttled"]["http_statuses"], [429])
            self.assertNotIn("sources_blocked", doc)
            self.assertEqual((code, doc["claim_uncited"] > 0), (int(uncited), uncited))
            self.assertEqual(self.verdict(spec, code, doc)["result"], result)

    def test_untraced_reads_only_the_pinned_companies_a_listing_left_out(self):
        """The throttle-only rule (``run_cycles`` ``untraced``) over the company listing: an
        unpinned company is never captured by design and an incomplete listing that still
        shows every pinned company left nothing unread, so the throttle alone explains the
        unreached roots (blocked); a pinned company the listing did not show is untraced
        (fail)."""
        def throttled(listing):
            fake = world()
            fake.fail(r"/salesShipments(\?|$)", *[UNAVAILABLE] * 40)
            fake.fail(r"/purchaseOrders(\?|$)", *[TOO_MANY] * 40)
            fake.fail(r"/api/v2\.0/companies(\?|$)", *listing * 3)  # one listing per cycle
            return fake

        # A company row carries @odata.etag as every BC API v2.0 entity does (as the BC
        # fake's own listing): the capture keeps each company record as a ``companies``
        # unit, and without it the unthrottled third cycle ends that unit partial
        # (company_etag_missing), an untraced read, so the run would fail for that and
        # not test the listing.
        def page(companies, token):
            return 200, {}, json.dumps({"value": [{"id": c, "name": "X", "@odata.etag": 'W/"c"'}
                                                  for c in companies],
                                        "@odata.nextLink": f"{tb.HOST}{tb.BASE}/companies"
                                                           f"?$skiptoken={token}"}).encode()
        unpinned = [tb.UNPINNED, "c0000000-0000-4000-8000-0000000000fe"]
        incomplete = (page([*tb.CO, unpinned[0]], 4), page([unpinned[1]], 5))
        fake = throttled(incomplete)
        spec, code, doc = self.run_spec("tools.investigate_bc_claims", fake)
        self.assertEqual(len(fake.bc_gets(r"companies\?\$skiptoken=4$")), 3)  # per cycle
        self.assertEqual((code, doc["sources_blocked"]), (0, "upstream_throttled"))
        self.assertEqual(self.verdict(spec, code, doc)["result"], "blocked")
        missing = (page(tb.CO[:2], 2), page([], 3))
        fake = throttled(missing)
        spec, code, doc = self.run_spec("tools.investigate_bc_claims", fake)
        self.assertFalse(fake.bc_gets(tb.CO[2]))
        self.assertEqual(code, 1)
        self.assertNotIn("sources_blocked", doc)
        self.assertEqual(self.verdict(spec, code, doc)["result"], "fail")

    def test_bc_down_is_blocked_upstream_and_an_unknown_set_fails_closed(self):
        fake = world()
        fake.fail(r"/companies(\?|$)", *[UNAVAILABLE] * 40)
        spec, code, doc = self.run_spec("tools.investigate_bc_claims", fake)
        self.assertEqual((code, doc["sources_blocked"], doc["objects"]),
                         (0, "upstream_throttled", 0))
        self.assertEqual({(p["source"], p.get("http_status")) for p in doc["blocked_upstream"]},
                         {("business-central", 503)})
        self.assertTrue(live_verify.blocked_parts(doc))
        fake = world()  # not a throttle: blocked, as no record source answered
        fake.fail(r"/companies(\?|$)", *[(500, {}, b'{"error":{"code":"Internal"}}')] * 40)
        spec, code, doc = self.run_spec("tools.investigate_bc_claims", fake)
        self.assertEqual((code, doc["sources_blocked"]), (0, "no_record_source"))
        fake = world()
        fake.fail(r"/companies(\?|$)", *[UNAVAILABLE] * 40)
        spec, code, doc = self.run_spec("tools.find_unknown_property", fake)
        self.assertTrue(live_verify.check_expectations(spec, code, doc))  # full expect misses
        self.assertEqual(live_verify.check_expectations(
            {**spec, "expect": spec["expect_blocked"]}, code, doc), [])
        # A cycle naming no BC entity set is the product's own failure, never blocked.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = le.main(["investigate", "--state-dir", str(self.root / "unknown" / "state"),
                            "--cycle", "noSuchSet", "--bc-books", str(self.books)],
                           environ=ENV, sender=world(), sleep=lambda seconds: None)
        doc = json.loads(out.getvalue())
        self.assertEqual((code, doc["capture_errors"], doc["blocked_upstream"]), (1, 1, []))
        self.assertNotIn("sources_blocked", doc)


if __name__ == "__main__":
    unittest.main()
