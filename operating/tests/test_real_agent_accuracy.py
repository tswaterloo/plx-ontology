"""Real-agent accuracy: the product fixes behind the causes the
classification of the real agent's fixture answers found (``evals/bench/real_agent_failures.json``).

Evidence label: ``isolated-fixture``. Every test drives the product boundary an MCP client
reaches (the session server's tools, the initialize primer, the eval prompt), over the
case console's synthetic fixture world and fixture employees; no model runs here and no
live source is read.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evals import runner
from ontology.json_contract import validate
from operating.ontology_tools import load_contracts
from operating.tests.test_agent_loop import QUESTION, AgentLoopTests, _plain, model
from operating.tests.test_session_identity import OID_A, OID_B
from operating import agent_loop


CONTRACTS = load_contracts()


class AnswerTextCheckTests(AgentLoopTests):
    """Cause: the agent's prose stated numbers (identifier digits, counts) that no claim it
    submitted states, so the scorer's close-outcome check refused answers whose every value
    passed (claim_unstructured). plx.case_get answer mode now checks the prose it is given
    with the same check, and tells the client which numbers are uncovered."""

    def _session_with_read(self, oid=OID_A):
        """A session of ``oid`` that read the purchase order an OID_A Ask cited (OID_B may
        not read it: its read is refused), and that record's handle."""
        seen = agent_loop.run(QUESTION, self.server(OID_A), model([]))
        handle = next(c for claim in seen["claims"] for c in claim["citations"]
                      if c.startswith("api://public.app_purchase_order"))
        session = agent_loop._Session(self.server(oid))
        session.call("plx.read", {"citation": handle})
        return session, handle

    def claim(self, handle, value):
        return {"text": f"Its PO id is {value}.", "citations": [handle],
                "value_claims": [{"value": value, "citation": handle,
                                  "field": "row.custom_fields.fmGeneratedPoId"}]}

    def submit(self, session, answer):
        result, is_error = session.call("plx.case_get", {"answer": answer})
        self.assertFalse(is_error, result)
        return _plain(result)

    def test_prose_numbers_no_passing_claim_states_are_listed_uncovered(self):
        session, handle = self._session_with_read()
        covered = self.submit(session, {"claims": [self.claim(handle, 5447)],
                                        "text": "Its PO id is 5447."})
        self.assertEqual(covered["outcome"], "answered")
        self.assertEqual((covered["text_check"]["passed"], covered["text_check"]["uncovered"],
                          covered["text_check"]["instruction"]), (True, [], None))
        # An identifier of capitals then digits alone, a count in words and a stray number
        # are claims in prose too: each is listed with the gate's code, never passed.
        loose = self.submit(session, {"claims": [self.claim(handle, 5447)],
                                      "text": "PO id 5447 on QRX-082, twelve lines, 9999 units."})
        check = loose["text_check"]
        self.assertFalse(check["passed"])
        self.assertEqual(sorted(entry["value"] for entry in check["uncovered"]),
                         sorted(["082", "9999", "twelve"]))
        self.assertTrue(all(entry["code"] == "claim_unstructured"
                            for entry in check["uncovered"]))
        self.assertIn("cited value claim", check["instruction"])
        # The claims are still the gate's: the text never changes which claims pass.
        self.assertEqual(loose["claims"], covered["claims"])

    def test_a_refused_claim_covers_nothing_and_nothing_withheld_is_shown(self):
        """Fail closed: a claim the gate refuses (a wrong value, or a citation this session
        was never returned: the reader without access) covers no number of the text, and
        the check shows no handle or value of the record."""
        session, handle = self._session_with_read()
        wrong = self.submit(session, {"claims": [self.claim(handle, 5448)],
                                      "text": "Its PO id is 5448."})
        self.assertEqual([e["value"] for e in wrong["text_check"]["uncovered"]], ["5448"])
        other, _ = self._session_with_read(OID_B)
        result = self.submit(other, {"claims": [self.claim(handle, 5447)],
                                     "text": "Its PO id is 5447."})
        self.assertEqual(result["claims"], [])
        self.assertEqual([e["value"] for e in result["text_check"]["uncovered"]], ["5447"])
        self.assertNotIn(handle, json.dumps(result))

    def test_the_text_passes_the_one_prose_screen_the_scorer_runs(self):
        """The scorer screens the answer text with ``claim_checks.screen_prose`` beside the
        close-outcome check; the gate's text check runs the same screen. A passing value
        written negated denies what it states (``claim_value_unstated``: blocking); a state
        word no claim of its sentence is about (``late``, no lateness claim) is listed
        ``unchecked`` (``claim_prose_unchecked``: unverified, the answer still passes)."""
        session, handle = self._session_with_read()
        denied = self.submit(session, {"claims": [self.claim(handle, 5447)],
                                       "text": "Its PO id is not 5447."})["text_check"]
        self.assertFalse(denied["passed"])
        self.assertEqual([(e["code"], e["value"]) for e in denied["uncovered"]],
                         [("claim_value_unstated", "5447")])
        self.assertIsNotNone(denied["instruction"])
        late = self.submit(session, {"claims": [self.claim(handle, 5447)],
                                     "text": "The order is late and its PO id is "
                                             "5447."})["text_check"]
        self.assertEqual((late["passed"], late["uncovered"]), (True, []))
        self.assertEqual([e["code"] for e in late["unchecked"]], ["claim_prose_unchecked"])
        self.assertIsNotNone(late["instruction"])
        clean = self.submit(session, {"claims": [self.claim(handle, 5447)],
                                      "text": "Its PO id is 5447."})["text_check"]
        self.assertEqual((clean["passed"], clean["unchecked"], clean["instruction"]),
                         (True, [], None))

    def test_without_text_the_answer_mode_is_the_asks_gate_unchanged(self):
        session, handle = self._session_with_read()
        result = self.submit(session, {"claims": [self.claim(handle, 5447)]})
        self.assertNotIn("text_check", result)
        for bad in ({"claims": [self.claim(handle, 5447)], "text": 5447},
                    {"claims": [self.claim(handle, 5447)], "text": ""},
                    {"text": "PO id 5447"}):
            with self.subTest(answer=bad):
                result, is_error = session.call("plx.case_get", {"answer": bad})
                self.assertTrue(is_error, result)

    def test_the_eval_prompt_asks_for_the_text_and_names_identifier_digits(self):
        self.assertIn('"text"', runner.ANSWER_GATE_RULE)
        self.assertIn("text_check", runner.ANSWER_GATE_RULE)


class FailureClassificationTests(unittest.TestCase):
    """The committed classification is whole: every item one known class, the summary its
    count, the live part labelled unverified with its blocker."""

    def test_every_item_has_one_known_class_and_the_summary_counts_them(self):
        path = Path(__file__).resolve().parents[2] / "evals/bench/real_agent_failures.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {"wrong_tool", "tool_returned_nothing_useful", "answer_cut",
                 "scorer_mismatch", "lead_only_evidence", "data_truly_absent",
                 "answer_prose_uncovered"}
        classes = [item["class"] for item in data["items"]]
        self.assertTrue(set(classes) <= known, classes)
        self.assertEqual(len({item["item"] for item in data["items"]}), len(data["items"]))
        for name in known:
            self.assertEqual(data["summary"].get(name, 0), classes.count(name), name)
        # The latest run's failing items too, each with its cause.
        remaining = data["after"]["remaining"]
        self.assertTrue(all(item["class"] in known and item["cause"] for item in remaining))
        for name in known:
            self.assertEqual(data["remaining_summary"].get(name, 0),
                             [item["class"] for item in remaining].count(name), name)
        self.assertIs(data["live"]["verified"], False)
        self.assertTrue(data["live"]["blocker"])


class FixtureCaptureHostTests(unittest.TestCase):
    """Cause: the fixture host the real agent ran on had no BC, so every item whose answer
    is a BC record (a purchase invoice, an invoiced quantity) was data-absent for the agent
    while the scripted baseline reads it. ``--fixture-capture-audience`` gates the fixture
    state's own BC capture as a live audience would, building no reader and reading no
    credential; it is refused anywhere but a fixture session."""

    MESSAGES = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}]

    @classmethod
    def setUpClass(cls):
        from integrations.capture_store import CaptureStore
        from integrations.live_source_guard import LiveSourceAudience
        from ontology.core import Actor
        from operating.host import BC_DIR, BC_FILE
        from scripts import eval_dev_baseline as edb, eval_oracle_dev as dev
        cls._temp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._temp.name)
        cls.state = cls.dir / "state"
        (cls.state / BC_DIR).mkdir(parents=True)
        capture, environment = edb.fixture_bc_capture(dev.eval_world(), host_tenant="plx")
        capture(cls.state / BC_DIR / BC_FILE, None)
        cls.marking = f"bc:{environment.lower()}:*"
        cls.audience = cls.dir / "capture-audience.json"
        cls.audience.write_text(json.dumps({
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "audiences": {cls.marking: ["staff:finance"]},
            "default_classification": "internal"}), encoding="utf-8")
        reader = Actor("plx", frozenset({"staff:finance"}), "restricted")
        store = CaptureStore.open_read_only(cls.state / BC_DIR / BC_FILE, LiveSourceAudience(
            {cls.marking: ["staff:finance"]}, tenant="plx", default_classification="internal"))
        try:
            unit = next(u for u in store.units("bc") if u.endswith("/purchaseInvoices"))
            cls.handle = store.records(reader, "bc", unit, limit=1)["items"][0]["citation"]
        finally:
            store.close()

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def host(self, *arguments, calls=(), env=None):
        from operating.tests.test_session_identity import AUDIENCE, ROOT, _env
        messages = [*self.MESSAGES, *({"jsonrpc": "2.0", "id": 10 + n, "method": "tools/call",
                                       "params": {"name": name, "arguments": args}}
                                      for n, (name, args) in enumerate(calls))]
        return subprocess.run(
            [sys.executable, "-m", "operating.mcp_server", "--state-dir", str(self.state),
             "--source-mirror-audience", str(AUDIENCE), *arguments], cwd=ROOT,
            env={**_env(), **(env or {})}, capture_output=True, text=True, timeout=120,
            input="".join(json.dumps(m) + "\n" for m in messages))

    def read(self, principal, env=None):
        done = self.host("--fixture", "--principal", principal, "--fixture-capture-audience",
                         str(self.audience), calls=[("plx.read", {"citation": self.handle})],
                         env=env)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        return json.loads(done.stdout.splitlines()[1])["result"]

    def test_the_granted_reader_reads_the_capture_and_no_other_reader_sees_it(self):
        # Credential-shaped BC variables in the environment are never read: no reader is
        # built, so the read is the capture's alone (it would fail closed, not go live).
        bait = {"BC_TENANT_ID": "t", "BC_CLIENT_ID": "c", "BC_CLIENT_SECRET": "s",
                "BC_ENVIRONMENT": "Production"}
        granted = self.read("staff:finance", env=bait)
        self.assertFalse(granted["isError"], granted)
        self.assertEqual(granted["structuredContent"]["citation"], self.handle)
        self.assertNotEqual(granted["structuredContent"].get("status"), "withheld", granted)
        self.assertIn("number", json.dumps(granted["structuredContent"]))
        # A reader the capture audience does not grant: withheld, no field of the record.
        refused = self.read("staff:operations")["structuredContent"]
        self.assertEqual((refused["status"], refused["completeness"]["returned"]),
                         ("withheld", 0), refused)
        self.assertEqual(set(refused) - {"citation", "gaps", "meta", "status", "completeness"},
                         set(), refused)

    def test_it_is_refused_anywhere_but_a_fixture_session(self):
        refused = {
            ("--principal", "staff:finance", "--fixture-capture-audience", "a"):
                "--fixture-capture-audience needs a --fixture session",
            ("--fixture", "--principal", "staff:finance", "--fixture-capture-audience", "a",
             "--live-source-audience", "a"):
                "--fixture-capture-audience is refused with --live-source-audience",
            ("--fixture", "--principal", "staff:finance", "--fixture-capture-audience",
             str(self.dir / "missing.json")): "fixture capture audience is invalid",
        }
        for arguments, message in refused.items():
            with self.subTest(arguments=arguments):
                done = self.host(*arguments)
                self.assertEqual(done.returncode, 2, done.stderr[-300:])
                self.assertIn(message, done.stderr)
                self.assertEqual(done.stdout, "")
        other = self.dir / "other-tenant.json"
        other.write_text(json.dumps({"schema": "plx-live-source-audience/v1",
                                     "tenant": "elsewhere",
                                     "audiences": {self.marking: ["staff:finance"]}}),
                         encoding="utf-8")
        done = self.host("--fixture", "--principal", "staff:finance",
                         "--fixture-capture-audience", str(other))
        self.assertEqual(done.returncode, 2)
        self.assertIn("tenant must equal --tenant", done.stderr)
        # The harness's host refuses it the same way off a fixture host.
        with self.assertRaises(runner.RunnerError):
            runner.CaseHost(self.state, self.audience, "plx", ["staff:finance"],
                            capture_audience=self.audience)


class HomeRuleCoverageTests(unittest.TestCase):
    """Cause (a): plx.find's home mode dropped the rules' not_evaluated counts, so an
    agent read "no late purchase order" when the rule had judged none (no need date, an
    unsettled confirmation). Home now names, per rule, how many records the reader may read
    it left unjudged, and is incomplete (rule_not_evaluated) while any is."""

    def setUp(self):
        from operating.tests import test_connector_home as home
        self.home = home
        self.base = home.Base("setUp")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    def run_all_rules(self, world, coverage):
        from datetime import datetime, timezone
        from operating import proactive_engine as pe
        world.build(self.base.store)

        def card(_tool, arguments):
            return {"object": {"object_id": arguments["object_id"]}, "leads": [],
                    "conflicts": []}
        return pe.run(self.base.store, pe.inbox_path(self.base.store), self.home.AS_OF,
                      coverage=coverage, qc_link_types=(self.home.QC_LINK,), call=card,
                      clock=lambda: datetime(2026, 10, 19, 6, tzinfo=timezone.utc))

    def rules(self, host):
        found = self.base.find(host, {"query": "What needs my attention?", "limit": 50})
        return found, {rule["rule"]: rule for rule in found["home"]["rules"]}

    def test_each_rule_says_what_it_left_unjudged_of_what_the_reader_may_read(self):
        world, ids = self.home.seeded(receipt_markings='["quality_controlled"]')
        summary = self.run_all_rules(world, ids["coverage"])
        found, rules = self.rules(self.home.Host(self.base.store))
        self.assertEqual(set(rules), set(summary["detectors"]))
        for name, rule in rules.items():
            skipped = sum(summary["not_evaluated"].get(name, {}).values())
            if name == "order_without_customer_po":   # an order never projected: no record
                self.assertIsNone(rule["not_evaluated"])
            else:
                self.assertEqual(rule["not_evaluated"] == 0, skipped == 0, (name, rule))
            self.assertEqual(rule["status"], summary["detectors"][name])
        # records, not reasons: the disputed need date sits on two records of one order;
        # the Sage-only, status-less and unmapped-word POs are unknown and counted
        self.assertEqual(rules["late_or_unconfirmed_po"]["not_evaluated"], 8)
        # Every rule ran to the end, yet records were left unjudged: never a clean result.
        self.assertEqual((found["home"]["complete"], found["completeness"]["complete"],
                          found["completeness"]["reason"]), (False, False, "rule_not_evaluated"))
        # A narrower reader is told only of the records it may read; one with no grant 0.
        _, narrow = self.rules(self.home.Host(self.base.store, units=("api:a",
                                                                      self.home.UNIT), held=()))
        for name, rule in narrow.items():
            if rule["not_evaluated"] is not None:
                self.assertLessEqual(rule["not_evaluated"], rules[name]["not_evaluated"], name)
        self.assertLess(narrow["po_receipt_quantity_mismatch"]["not_evaluated"],
                        rules["po_receipt_quantity_mismatch"]["not_evaluated"])
        _, none = self.rules(self.home.Host(self.base.store, units=(), held=()))
        self.assertEqual({name: rule["not_evaluated"] for name, rule in none.items()
                          if rule["not_evaluated"] is not None},
                         {name: 0 for name in none if name != "order_without_customer_po"})

    def test_a_clean_store_is_complete_and_an_unknown_rules_shape_is_not(self):
        from unittest import mock
        from operating import proactive_engine as pe
        world = self.home.World()
        self.home.po(world, "Pending", "2026-10-01")
        self.run_all_rules(world, {"orders": [], "completeness": {"complete": True}})
        found, rules = self.rules(self.home.Host(self.base.store))
        self.assertEqual({rule["not_evaluated"] for rule in rules.values()}, {0})
        self.assertTrue(found["completeness"]["complete"], found["home"]["rules"])
        real = pe.read_inbox
        for odd in ([{"rule": "late_or_unconfirmed_po", "status": "complete",
                      "not_evaluated": -1}], "x", None):
            def bent(*args, odd=odd, **kwargs):
                return {**real(*args, **kwargs), "rules": odd}
            with self.subTest(rules=odd), mock.patch.object(pe, "read_inbox", bent):
                found, rules = self.rules(self.home.Host(self.base.store))
                self.assertIsNone(rules["late_or_unconfirmed_po"]["not_evaluated"])
                self.assertFalse(found["completeness"]["complete"])

    def test_a_skip_a_hidden_record_caused_is_not_counted_so_it_hints_at_nothing(self):
        # A skip over an identity cluster was counted against every
        # member, so a reader of one member learnt that a record it may not read (a twin
        # disputing the need date, a lead from a hidden confirmation, a value at a marking
        # it does not hold) made the rule skip. Each hidden part below is the whole cause
        # of its skip; the narrow reader's count must be what it is without those parts.
        def world(hidden):
            w = self.home.World()
            po = self.home.po
            twin = po(w, "Pending", "2026-10-03")
            # not yet due: only the (lead) confirmation could make it at risk (Vince)
            lead = po(w, "Pending", "2026-11-02")
            marked = po(w, "Pending", "2026-10-04")
            po(w, "Pending")   # no need date: its own skip
            if hidden:
                w.link("PurchaseOrder.same_record_as", twin,
                       po(w, "Pending", "2026-10-07", audience="api:b"))
                w.link("SupplierConfirmation.confirms", w.obj(
                    "SupplierConfirmation", {"delivery_date": "2026-10-01"},
                    audience="api:b"), lead, "lead")
                w.link("PurchaseOrder.same_record_as", marked,
                       po(w, "Pending", "2026-10-09", markings='["finance"]'))
            return w
        narrow = {"units": ("api:a", self.home.UNIT), "held": ()}
        counts = {}
        for hidden in (True, False):
            self.run_all_rules(world(hidden), {"orders": [], "completeness": {"complete": True}})
            _, full = self.rules(self.home.Host(self.base.store))
            found, rules = self.rules(self.home.Host(self.base.store, **narrow))
            counts[hidden] = rules["late_or_unconfirmed_po"]["not_evaluated"]
            # a reader of everything is told of every record the rule left unjudged
            self.assertEqual(full["late_or_unconfirmed_po"]["not_evaluated"],
                             6 if hidden else 1, hidden)
            self.assertEqual(found["completeness"]["reason"], "rule_not_evaluated")
            self.assertNotIn("api:b", json.dumps(found["home"]))
        # the narrow reader counts only the skip it may read the cause of, either way
        self.assertEqual(counts, {True: 1, False: 1})
        for count in (1, 10, 60):
            with self.subTest(count=count):
                world = self.home.World()
                for _ in range(count):   # open orders with no need date: not judged
                    self.home.po(world, "Pending")
                self.run_all_rules(world, {"orders": [], "completeness": {"complete": True}})
                _, rules = self.rules(self.home.Host(self.base.store))
                self.assertEqual(rules["late_or_unconfirmed_po"]["not_evaluated"], count)


class FreeTextQuestionTests(unittest.TestCase):
    """Cause (b): the primer sent "Which purchase orders are late?" to three_way_match,
    plain business questions ("what should I worry about?") were not home questions, and a
    question with no identifier came back as a wall of "live carrier read(s) not
    configured" gaps: noise, since no record number was there to look up."""

    def test_the_primer_and_home_phrases_route_business_questions(self):
        from operating import mcp_server
        starters = {question: family for question, _tools, family
                    in mcp_server.PRIMER_STARTERS}
        self.assertEqual(starters["Which purchase orders are late?"], "late_po")
        for question in ("What should I worry about?", "What should I worry about today?",
                         "What do I need to worry about?", "Are there any problems?",
                         "Any issues today?", "What's late?", "What is behind?"):
            self.assertEqual(mcp_server.home_kinds(question), (), question)
        for question in ("Are there any problems with my purchase orders?",
                         "Which purchase orders should I worry about?"):
            self.assertIn("late_po", mcp_server.home_kinds(question) or (), question)
        # A question naming a record is never home mode, however it is worded.
        for question in ("problems with PO 5447", "What is late on SO100201?",
                         "issues 5447", "worry"):
            self.assertIsNone(mcp_server.home_kinds(question), question)


def _resolve_case():
    from ontology.tests import test_resolve as tr

    class NoBcResolve(tr.ResolveTestCase):
        """The resolve fixture with the BC lookup not configured (Portal mirror only)."""

        def runTest(self):  # pragma: no cover - driven by CarrierGapTests
            pass

        def tools(self, **kwargs):
            tools = super().tools(**kwargs)
            from ontology import resolve
            tools.sources = resolve.SourceResolver(
                self.view, [resolve.MirrorLookup(self.mirror, clock=self.clock)],
                normalizer=tr.identifiers.normalize, workdir=self.root / "work-nobc",
                clock=self.clock)
            return tools
    return tr, NoBcResolve


class CarrierGapTests(unittest.TestCase):
    """Cause (b): carriers no source is configured for are named only when the query can
    name an identifier; a digit-free question still never reads as absence."""

    def setUp(self):
        self.tr, case = _resolve_case()
        self.case = case()
        self.case.setUpClass()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def gaps(self, query):
        out = self.case.tools().call("plx.find", {"query": query}, self.tr.SALES)
        return out, [g for g in out["gaps"] if g["reason"] == "source_not_configured"]

    def test_unconfigured_carrier_gaps_only_for_a_query_that_names_an_identifier(self):
        out, gaps = self.gaps("SO999999")
        self.assertEqual(out["status"], "not_found")
        self.assertTrue(gaps, out["gaps"])
        self.assertTrue(all(g["subject"].startswith("bc:") for g in gaps), gaps)
        out, gaps = self.gaps("what should i chase with the supplier")
        self.assertEqual(out["status"], "not_found")
        self.assertEqual(gaps, [])
        # The no-match is still not a claim of absence.
        self.assertNotIn("absent_in_source", {g["state"] for g in out["gaps"]})


class LatePoUndecidedTests(unittest.TestCase):
    """Cause (c): a PO with no planned date on any record the reader can read was told
    "a due date of this stage is withheld" (asserting a hidden date) whenever its grant did
    not cover a carrier source, while the proactive engine called it undated; and late_po's
    undecided verdict said "see its gaps" without carrying them."""

    @classmethod
    def setUpClass(cls):
        from ontology.tests import test_po_lateness as pl
        pl.setUpModule()
        cls.pl = pl

    def store(self, bc_date):
        pl = self.pl
        s = pl.tp.Store()
        po = pl.portal_po(s)                        # no planned date on the Portal PO
        twin = pl.bc_po(s, expected=bc_date)        # BC: outside the SALES grant
        s.link("PurchaseOrder.same_record_as", po, twin, kind="same_record")
        return s, po

    def lateness(self, store, po, actor):
        result = self.pl.tools_at(store).process(
            {"from": po, "process": self.pl.PROCESS}, actor).result
        self.assertEqual(validate(result, CONTRACTS["tools"]["plx.process"]["output"],
                                  root=CONTRACTS), [])
        (entry,) = result["lateness"]
        return result, entry

    def test_no_visible_date_is_not_a_withheld_date_and_hides_what_bc_holds(self):
        pl = self.pl
        texts = []
        for bc_date in ("2099-01-05", None):
            store, po = self.store(bc_date)
            result, entry = self.lateness(store, po, pl.SALES)
            self.assertIsNone(entry["late"])
            self.assertEqual((entry["gap"]["state"], entry["gap"]["reason"]),
                             ("unknown", "no_visible_due_date"), entry["gap"])
            self.assertNotIn("withheld", json.dumps(entry["gap"]))
            texts.append(json.dumps(result).replace(po, "PO"))
            staff_result, staff = self.lateness(store, po, pl.STAFF)
            if bc_date:
                self.assertEqual(staff["governing_date"], bc_date)
            else:   # the engine's no_need_date: no planned date in any source
                self.assertEqual(staff["gap"]["reason"], "no_due_date")
        # The restricted reader's answer is the same whether BC holds a date or not.
        self.assertEqual(texts[0], texts[1])
        # A date the reader can read, beside a source it may not: still withheld.
        store, po = self.store("2099-01-05")
        store.event("PurchaseOrder.expected", po, "2099-01-25")
        _, entry = self.lateness(store, po, pl.SALES)
        self.assertEqual(entry["gap"]["reason"], "withheld_by_permission")

    def test_late_po_carries_the_gaps_of_an_undecided_verdict(self):
        pl = self.pl
        store, po = self.store("2099-01-05")
        out = pl.LatePoPlanTests("test_the_stored_plan_validates").investigate(
            pl.LatePoPlanTests("test_the_stored_plan_validates").table(store), po, pl.SALES)
        entries = {e["name"]: e for e in out["answer"]}
        self.assertIsNone(entries["late_1"]["value"])
        about = [g for g in out["gaps"] if g.get("concerns") == "late_1"]
        self.assertIn("no_visible_due_date", {g["reason"] for g in about}, out["gaps"])
        (undecided,) = [g for g in about if g["reason"] == "not_decided"]
        self.assertNotIn("see its gaps", undecided["detail"])
        self.assertNotIn("2099-01-05", json.dumps(out))


class StagingRootGapTests(unittest.TestCase):
    """Cause (d): the root_lineage_staging gap's detail carried the binding's code paths
    and was cut mid-word at 600 characters. The detail is one plain sentence; the
    references go in ``basis``, cut at a word when long."""

    def test_detail_is_plain_and_a_long_basis_ends_on_a_whole_word(self):
        import copy
        from unittest import mock
        from operating import ontology_tools_process as op
        from tests import test_ontology_tools_process as base
        from tests import test_process_three_way as tw
        base.setUpModule()
        real = op.ProcessTools._binding

        def long_rules(self, bid):
            found = real(self, bid)
            if found is None or bid != "portal.app_purchase_order":
                return found
            tid, binding = found
            binding = copy.deepcopy(binding)
            rules = binding.get("lineage", {}).get("rules", [])
            binding.setdefault("lineage", {})["rules"] = rules * 12
            return tid, binding
        case = tw.GoldPurchaseOriginTests("run")
        seen, cut = [], op._cut_words

        def watched(text, limit):
            seen.append(text)
            return cut(text, limit)
        with mock.patch.object(op.ProcessTools, "_binding", long_rules), \
                mock.patch.object(op, "_cut_words", watched):
            answer, stages, _ = case.gold_po("gold_native_staging")
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "root_lineage_staging"]
        (full,) = set(seen)
        self.assertGreater(len(full), 600)
        self.assertNotIn("@", gap["detail"])
        self.assertTrue(gap["detail"].endswith("."), gap["detail"])
        basis = gap["basis"]
        self.assertLessEqual(len(basis), 600)
        self.assertTrue(basis.endswith("\u2026"), basis[-80:])
        kept = basis[:-1]
        self.assertTrue(full.startswith(kept))
        self.assertIn(full[len(kept)], " ;,", full[len(kept) - 20:len(kept) + 20])
        missing = stages["purchase_order"]["missing"]
        self.assertEqual(missing["basis"], basis)
        self.assertNotIn("@", missing["detail"])


class ChainPartyNameTests(unittest.TestCase):
    """Cause (e): the chain answer counted suppliers (supplier_count 1) but named none, so
    an agent could not say who supplied the order. It now names each supplier and
    customer the chain reached, cited, only where the reader may read the record."""

    @classmethod
    def setUpClass(cls):
        from ontology.tests import test_po_lateness as pl
        pl.setUpModule()
        cls.pl = pl

    def investigate(self, store, root, policy, actor):
        from operating import ontology_tools as ot
        from operating import ontology_tools_investigate as oi
        from operating import ontology_tools_process as op
        from operating import ontology_tools_traverse as tt
        tp = self.pl.tp
        table: dict = {}
        base = ot.OntologyTools(ot.StoreReader(store.conn), policy, schema=tp.SCHEMA,
                                contracts=CONTRACTS, clock=lambda: tp.NOW)
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        op.register(table, op.ProcessTools(base, tp.GRAPHS))
        oi.register(table, oi.InvestigateTools(table, schema=tp.SCHEMA, rows=None))
        out = table["plx.investigate"]["handler"]({"family": "chain", "root": root}, actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.investigate"]["output"],
                                  root=CONTRACTS), [], json.dumps(out)[:1500])
        return out

    def store(self, suppliers):
        s = self.pl.tp.Store()
        po = self.pl.portal_po(s, expected="2099-01-25")
        for index, (name, code) in enumerate(suppliers):
            sup = s.obj(f"Supplier:portal.app_supplier:sup-{index}")
            if name:
                s.val(sup, "name", "text", name)
            s.val(sup, "code", "identifier", code)
            s.link("PurchaseOrder.ordered_from", po, sup)
        return s, po

    def names(self, out):
        self.assertIn("answer", out, json.dumps(out)[:1500])

        def text(value):
            return value.get("untrusted_text") if isinstance(value, dict) else value
        return {e["name"]: (e["property"], text(e["value"]), e["citation"]["l1_handle"])
                for e in out["answer"] if e["name"].startswith("supplier_")
                and e["name"] != "supplier_count"}

    def test_the_chain_names_its_supplier_only_to_a_reader_of_it(self):
        from operating import ontology_tools as ot
        from ontology import store_ddl
        tp = self.pl.tp
        s, po = self.store([("Synthetic Supplies Co", "SYN-SUP-1")])
        out = self.investigate(s, po, tp.POLICY, tp.STAFF)
        (prop, value, handle), = self.names(out).values()
        self.assertEqual((prop, value), ("name", "Synthetic Supplies Co"))
        self.assertTrue(handle.startswith("api://public.app_supplier/"))
        counts = {e["name"]: e["value"] for e in out["answer"]}
        self.assertEqual(counts["supplier_count"], 1)
        # A reader whose grant leaves out the supplier relation: no name, no code.
        narrow = ot.ReadPolicy("plx", {"api:*": frozenset({"staff", "sales"}),
                                       "api:public.app_supplier": frozenset({"staff"})},
                               {m: frozenset({"staff"}) for m in store_ddl.MARKINGS})
        out = self.investigate(s, po, narrow, tp.SALES)
        self.assertEqual(self.names(out), {})
        self.assertNotIn("Synthetic Supplies", json.dumps(out))
        self.assertNotIn("SYN-SUP-1", json.dumps(out))

    def test_cardinality_and_a_supplier_with_no_name_is_named_by_its_code(self):
        tp = self.pl.tp
        for count in (1, 10, 60):
            with self.subTest(count=count):
                s, po = self.store([(None if i % 3 == 0 else f"Synthetic Vendor {i:02d}",
                                     f"SYN-{i:02d}") for i in range(count)])
                out = self.investigate(s, po, tp.POLICY, tp.STAFF)
                named = self.names(out)
                reached = {e["name"]: e["value"] for e in out["answer"]}["supplier_count"]
                # Names are read for at most the step's max_each (8) suppliers: every name
                # entry is on every answer page, so an uncapped read turned a fitting answer
                # into result_too_large. The rest are counted and said unread, never dropped
                # silently.
                self.assertEqual(len(named), min(reached, 8))
                self.assertEqual(len({h for _, _, h in named.values()}), len(named))
                for prop, value, _ in named.values():
                    self.assertEqual(prop == "code", value.startswith("SYN-"), value)
                unread = [g for g in out["gaps"] if g["subject"] == "supplier_read"
                          and g["reason"] == "page_cap"]
                self.assertEqual(len(unread), int(reached > 8), out["gaps"])
                if reached > 8:
                    self.assertIn(f"{reached - 8} of {reached} starting objects",
                                  unread[0]["detail"])
                    self.assertFalse(out["completeness"]["complete"])



class ObservationBoundTests(unittest.TestCase):
    """Cause (f): the console Ask showed the planner 6,000 characters of each result, so a
    plan's answer (11-22k) arrived cut, its later entries and gaps unseen, and each
    citation spent ~80 characters on a digest the planner never uses (evidence and the
    claim gate read the whole result). A plx.investigate result is shown up to 12,000
    characters; every citation the planner is shown keeps its handle, field and lineage."""

    @staticmethod
    def server(tool, n, pad):
        from operating.tests.test_ask_answers import BigResultServer
        server = BigResultServer(0)
        server.tools = [{**server.tools[0], "name": tool}]
        server.result = {
            "answerability": {"state": "answerable"},
            "answer": [{"name": f"e{i}", "value": str(i), "note": "n" * pad,
                        "citation": {"l1_handle": f"api://public.t/{i}", "field_path":
                                     "row.v", "digest": "d" * 64, "lineage": "native"},
                        "records": [{"citation": {"l1_handle": f"api://public.r/{i}",
                                                  "field_path": "row.id", "digest": "e" * 64,
                                                  "lineage": "native"}}]}
                       for i in range(n)]}
        return server

    def test_an_investigate_result_up_to_12k_is_shown_whole_without_digests(self):
        from operating.tests.test_ask_answers import merged, pager
        for n in (1, 10, 30):   # 60 entries are over 12k even bare: the next test
            with self.subTest(n=n):
                pad = 9_000 // n - 330   # ~9k of JSON whatever n: over 6k, under 12k
                server, pages = self.server("plx.investigate", n, pad), []
                agent_loop.run("What happened to X:y:{}?", server,
                               pager(pages, "plx.investigate"),
                               agent_loop.Bounds(max_steps=32))
                text = json.dumps(server.result, separators=(",", ":"))
                self.assertGreater(len(text), 6_000)
                first = pages[0]["result"]
                self.assertNotIn("omitted", first)
                self.assertNotIn("digest", json.dumps(first))
                expected = json.loads(text.replace(',"digest":"' + "d" * 64 + '"', "")
                                      .replace(',"digest":"' + "e" * 64 + '"', ""))
                self.assertEqual(merged(pages), expected)
                self.assertEqual(server.calls, 1)

    def test_the_bounds_hold_over_them(self):
        from operating.tests.test_ask_answers import merged, pager
        for tool, limit in (("plx.investigate", 12_000), ("plx.timeline", 6_000)):
            with self.subTest(tool=tool):
                server, pages = self.server(tool, 60, 400), []
                agent_loop.run("What happened to X:y:{}?", server, pager(pages, tool),
                               agent_loop.Bounds(max_steps=32))
                self.assertIn("omitted", pages[0]["result"])
                self.assertGreater(len(json.dumps(pages[0]["result"])), limit * 0.8)
                for page in pages:
                    self.assertLessEqual(len(json.dumps(page["result"], ensure_ascii=False,
                                                        separators=(",", ":"))), limit)
                self.assertEqual([e["name"] for e in merged(pages)["answer"]],
                                 [f"e{i}" for i in range(60)])
        with self.assertRaises(ValueError):
            agent_loop.Bounds(max_investigate_chars=0)
        with self.assertRaises(ValueError):
            agent_loop.Bounds(max_investigate_chars=40_001)



class GapConcernsTests(unittest.TestCase):
    """Observed after (a)-(g): an answer whose gap said "concerns": null (it concerns no
    value) was refused whole as answer_malformed:gaps, every value lost (scorer mismatch);
    and an agent gave a step gap with no concerns the concerns of the item's own gap, so
    one value carried two states (gap_contradictory). null reads as no concerns; any other
    non-string still refuses the answer; the instructions say to copy concerns as given."""

    def answer(self, concerns):
        return {"format": "plx-eval-answer/v1", "answerability": "partial", "text": "t",
                "values": [{"name": "shipped", "value": None}],
                "gaps": [{"state": "unknown", "subject": "Shipment", "reason": "no_match",
                          "concerns": concerns}]}

    def test_null_concerns_concern_nothing_and_other_shapes_fail_closed(self):
        from evals import score
        fence = "```json\n{}\n```"
        value, error = score.parse_answer(fence.format(json.dumps(self.answer(None))))
        self.assertIsNone(error)
        self.assertIsNone(value["gaps"][0]["concerns"])
        for bad in (3, ["shipped"], {"name": "shipped"}, True):
            with self.subTest(bad=bad):
                self.assertEqual(score.parse_answer(fence.format(json.dumps(
                    self.answer(bad))))[1], "answer_malformed:gaps")
        # A null-concerns gap beside a gap on the value is no contradiction.
        two = self.answer(None)
        two["gaps"].append({"state": "not_ingested", "subject": "shipped quantity",
                            "reason": "no_object_reached", "concerns": "shipped"})
        self.assertFalse(score._contradictory(two["gaps"], two["values"]))

    def test_the_instructions_say_to_copy_concerns_as_given(self):
        from operating import mcp_server
        prompt = runner.build_prompt({"as_known_at": {"time": "2099-01-02T03:04:05Z",
                                                      "l1_generations": {}}},
                                     "Q?")
        for text in (mcp_server.PRIMER, " ".join(prompt.split())):
            self.assertIn("a gap with no concerns concerns no value", text)


class HarnessBcEvidenceTests(unittest.TestCase):
    """Cause: in the real-agent fixture part the harness re-read a cited BC record through
    the bare case host, which answers a bc:// citation with metadata only, so a value an
    answer drew from BC (a purchase or sales invoice count) scored
    calculation_input_unresolved however it was cited. The sample host now re-reads it from
    the capture store the product projected, as the item's principals, as the scripted
    baseline does; a reader the store does not grant, or a handle it does not hold, gets no
    row."""

    @classmethod
    def setUpClass(cls):
        from integrations.capture_store import CaptureStore
        from integrations.live_source_guard import LiveSourceAudience
        from ontology.core import Actor
        from operating.host import BC_DIR, BC_FILE
        from scripts import eval_dev_baseline as edb, eval_real_agent as era
        cls._temp = tempfile.TemporaryDirectory()
        work = Path(cls._temp.name) / "work"
        cls.items, host_for, _ = era.fixture_parts(work)["fixture_sample"]
        cls.host_for = staticmethod(host_for)
        db = work / "fixture" / "state" / BC_DIR / BC_FILE
        store = CaptureStore.open_read_only(db, LiveSourceAudience(
            {"bc:*": [edb.PRINCIPAL]}, tenant=edb.TENANT, default_classification="internal"))
        reader = Actor(edb.TENANT, frozenset({edb.PRINCIPAL}), "restricted")
        try:
            cls.handles = [store.records(reader, "bc", unit, limit=1)["items"][0]["citation"]
                           for unit in store.units("bc")
                           if unit.endswith(("/purchaseInvoices", "/salesInvoices"))]
        finally:
            store.close()

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def evidence(self, item, handles):
        host = self.host_for(item)
        try:
            return runner.harness_evidence(
                {"values": [{"citations": [{"l1_handle": h} for h in handles]}]}, host)
        finally:
            host.close()

    def test_a_cited_bc_record_is_re_read_with_its_row_as_the_items_reader(self):
        self.assertGreaterEqual(len(self.handles), 2)  # each company's purchase and sales
        item = next(i for i in self.items if not (i.get("actor") or {}).get("principals"))
        read = self.evidence(item, self.handles)
        for handle in self.handles:
            self.assertIs(read[handle]["ok"], True, handle)
            self.assertIsInstance(read[handle]["row"], dict)
            self.assertTrue(read[handle]["row"])
            self.assertEqual(read[handle]["provenance"]["source"], "capture-store")

    def test_an_ungranted_reader_or_an_unheld_handle_gets_no_row(self):
        item = dict(self.items[0], actor={"principals": ["staff:outsider"]})
        for handle, read in self.evidence(item, self.handles).items():
            self.assertNotIn("row", read, handle)
        mine = next(i for i in self.items if not (i.get("actor") or {}).get("principals"))
        unheld = self.handles[0].replace("@", "x@", 1)
        self.assertNotIn("row", self.evidence(mine, [unheld])[unheld])


def load_tests(loader, tests, pattern):
    """Only this module's own tests (the AgentLoopTests base is imported for its fixture)."""
    suite = unittest.TestSuite()
    for case in (AnswerTextCheckTests, FailureClassificationTests, FixtureCaptureHostTests,
                 HomeRuleCoverageTests, FreeTextQuestionTests, CarrierGapTests,
                 LatePoUndecidedTests, StagingRootGapTests, ChainPartyNameTests,
                 ObservationBoundTests, GapConcernsTests, HarnessBcEvidenceTests):
        names = [name for name in loader.getTestCaseNames(case)
                 if name in case.__dict__]
        suite.addTests(case(name) for name in names)
    return suite


if __name__ == "__main__":
    unittest.main()
