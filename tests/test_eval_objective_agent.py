"""The objective-investigation eval (scripts/eval_objective_agent.py,
live check ``eval.objective_agent``): the objective gate, the harness post-check's
metrics on scripted sessions and free-form objectives. Keyless: the fixture world and
``evals.runner.ScriptedAgent`` only (``injected-transport``, never the real agent)."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from evals import runner
from scripts import eval_objective_agent as oa

CATALOG = [{"name": "plx.find", "read_only": True},
           {"name": "plx.investigate", "read_only": True},
           {"name": "plx.case_open", "read_only": False},
           {"name": "plx.case_record_gap", "read_only": False},
           {"name": "plx.case_propose", "read_only": False},
           {"name": "plx.action_propose", "read_only": False},
           {"name": "plx.act", "read_only": False},
           {"name": "mc_create_task", "read_only": False}]

MO, SO = "app_manufacturing_order", "app_sales_order"
GAPS = (("absent_in_source", "AssemblyOrder.final_quantity",
         "Produced quantity of the assembly order",
         "The assembly order's produced quantity is empty in the source", MO),
        ("not_ingested", None, "Shipped quantity of the order",
         "No shipment rows are ingested for this order", SO))


def handles_of(result) -> dict:
    """Record identity -> one handle, of every citation in a tool result."""
    out: dict = {}
    for text in oa._strings(result):
        if "://" in text:
            identity = oa.handle_identity(text)
            if identity is not None:
                out.setdefault(identity, text)
    return out


def _text(value):
    """A case view's text field: the agent's view wraps it as untrusted text."""
    return value["untrusted_text"] if isinstance(value, dict) else value


def _root(objective) -> dict:
    return {"identifier": objective["root"]["identifier"],
            "identifier_kind": objective["root"]["identifier_kind"]}


def first_session(objective, as_known_at, *, gap_id=None, extra_fact=None, act=False,
                  tries=()):
    """A scripted first session: investigate, open a case, a cited fact per record read,
    the two gaps, one MC follow-up proposal per gap. ``gap_id`` overrides the proposals'
    gap; ``extra_fact`` adds a fact citing that handle; ``act`` also calls plx.act;
    ``tries``: families tried first (their results are read, never recorded)."""
    def script(prompt, call):
        for family in tries:
            call("plx.investigate", {"family": family, "root": _root(objective),
                                     "as_known_at": as_known_at})
        found = call("plx.investigate", {"family": "F1", "root": _root(objective),
                                         "as_known_at": as_known_at})
        handles = handles_of(found)
        case_id = call("plx.case_open", {"question": objective["goal"],
                                         "roots": [objective["root"]["identifier"]]})["case_id"]
        for handle in handles.values():
            call("plx.case_add_finding", {"case_id": case_id, "kind": "fact",
                                          "status": "supported",
                                          "statement": "The chain includes this record.",
                                          "citations": [handle]})
        if extra_fact is not None:
            call("plx.case_add_finding", {"case_id": case_id, "kind": "fact",
                                          "status": "supported",
                                          "statement": "A related order is also open.",
                                          "citations": [extra_fact]})
        for state, prop, missing, why, relation in GAPS:
            cited = [h for h in handles.values() if f"public.{relation}/" in h]
            if not cited:
                continue
            arguments = {"case_id": case_id, "state": state, "missing": missing, "why": why,
                         "who_can_answer": ["operations"], "citations": cited[:1]}
            if prop:
                arguments["property"] = prop
            gap = call("plx.case_record_gap", arguments)
            if "gap" not in gap:  # the case refused it (e.g. the property does not apply)
                continue
            call(oa.PROPOSAL_TOOL, {
                "case_id": case_id, "capability": oa.PROPOSAL_CAPABILITY,
                "payload": {"gap_id": gap_id or gap["gap"]["gap_id"], "title": missing,
                            "description": why, "priority": "medium"},
                "citations": cited[:1]})
        if act:
            call("plx.act", {"case_id": case_id, "proposal_id": "P1"})
        return "Case recorded."
    return script


def resume_session(*, duplicate=False):
    """A scripted resuming session: claim, read, (re-record a held gap), release."""
    def script(prompt, call):
        case_id = next(line.split(":", 1)[1].strip() for line in prompt.splitlines()
                       if line.startswith("Case:"))
        call("plx.case_open", {"case_id": case_id, "lease_seconds": oa.LEASE_SECONDS})
        view = call("plx.case_get", {"case_id": case_id, "section": "gaps"})
        if duplicate:
            held = view["items"][0]
            call("plx.case_record_gap", {
                "case_id": case_id, "state": held["state"],
                "missing": _text(held["missing"]).upper(),
                "why": "Still open.", "who_can_answer": ["operations"],
                "citations": oa._handles(held["citations"]),
                **({"property": _text(held["property"])} if held.get("property") else {})})
        call("plx.case_open", {"case_id": case_id, "lease_seconds": 0,
                               "handoff": "Nothing new; follow-ups proposed."})
        return "Resumed."
    return script


def prompt_objective(prompt) -> dict:
    """The objective a prompt states (its goal and root), as a session reads it."""
    lines = prompt.splitlines()
    found = re.search(r"^Root record: (.+) \(identifier kind: ([a-z_]+)\)\.$", prompt, re.M)
    goal = lines[lines.index("Objective (the employee's words):") + 1]
    return {"id": "from-prompt", "goal": goal,
            "root": {"identifier": found.group(1), "identifier_kind": found.group(2)}}


class PromptAgent:
    """The good scripted sessions for whatever objective the prompt states."""

    def run(self, prompt, host):
        if "\nCase: " in prompt:
            return runner.ScriptedAgent(resume_session()).run(prompt, host)
        return runner.ScriptedAgent(lambda p, call: first_session(
            prompt_objective(p), re.search(r"as known at (\S+) \(", p).group(1))(p, call)
            ).run(prompt, host)


class TwoSessions:
    """The first session's script, then the resuming one's."""

    def __init__(self, first, second):
        self.scripts, self.runs = [first, second], 0

    def run(self, prompt, host):
        script = self.scripts[min(self.runs, 1)]
        self.runs += 1
        return runner.ScriptedAgent(script).run(prompt, host)


class ObjectiveGateTest(unittest.TestCase):
    """(a) The runner's gate refuses plx.case_propose today; the objective gate admits
    it by its exact name while plx.act, other action tools and MC tools stay refused."""

    def test_runner_gate_refuses_the_proposal_tool_without_the_option(self):
        for options in ({}, {"case_tools": True}):
            allowed, refused = runner.allowed_tools(CATALOG, **options)
            self.assertNotIn("plx.case_propose", allowed)
            self.assertIn("plx.case_propose", refused)

    def test_objective_gate_admits_the_proposal_tool_only(self):
        allowed, refused = oa.objective_gate(CATALOG)
        self.assertIn("plx.case_propose", allowed)
        for name in ("plx.find", "plx.investigate", "plx.case_open", "plx.case_record_gap"):
            self.assertIn(name, allowed)
        for name in ("plx.act", "plx.action_propose", "mc_create_task"):
            self.assertIn(name, refused)
            self.assertNotIn(name, allowed)

    def test_claude_code_agent_carries_the_option(self):
        self.assertFalse(runner.ClaudeCodeAgent().proposals)
        self.assertTrue(runner.ClaudeCodeAgent(case_tools=True, proposals=True).proposals)


class ObjectiveFileTest(unittest.TestCase):
    """(c) An objective is a root and a goal only; anything else fails closed."""

    def test_fixture_objectives_load(self):
        objectives, as_known_at = oa.load_objectives()
        self.assertGreaterEqual(len(objectives), 5)
        self.assertEqual(as_known_at, "2099-01-02T04:00:00Z")

    def test_unknown_keys_are_refused(self):
        good = {"id": "obj-x", "root": {"identifier": "X1", "identifier_kind": "so_number"},
                "goal": "Close it."}
        self.assertEqual(oa.check_objective(good), good)
        for bad, code in (({**good, "family": "F4"}, "objective_keys"),
                          ({**good, "plan": ["plx.find"]}, "objective_keys"),
                          ({**good, "root": {**good["root"], "family": "F1"}}, "objective_root"),
                          ({**good, "goal": " "}, "objective_goal"),
                          ({**good, "id": "Bad Id"}, "objective_id"),
                          ("obj-x", "objective_keys")):
            with self.subTest(bad=bad), self.assertRaises(oa.ObjectiveError) as caught:
                oa.check_objective(bad)
            self.assertEqual(str(caught.exception), code)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "objectives.json"
            for data, code in (
                    ({"format": "other", "as_known_at": "t", "objectives": [good]},
                     "objectives_format"),
                    ({"format": oa.OBJECTIVES_FORMAT, "as_known_at": "t", "objectives": [good],
                      "expected": {}}, "objectives_keys"),
                    ({"format": oa.OBJECTIVES_FORMAT, "as_known_at": "t",
                      "objectives": [good, good]}, "objective_id_repeated")):
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(code=code), self.assertRaises(oa.ObjectiveError) as caught:
                    oa.load_objectives(path)
                self.assertEqual(str(caught.exception), code)


class FixtureSessionsTest(unittest.TestCase):
    """(b) and (c) on the fixture world: the post-check's metrics on scripted sessions."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        make_host, expectation = oa.fixture_world(Path(cls._tmp.name))
        cls.make_host, cls.expectation = staticmethod(make_host), staticmethod(expectation)
        objectives, cls.t = oa.load_objectives()
        cls.objectives = {item["id"]: item for item in objectives}
        cls.objective = cls.objectives["obj-fx-001"]
        cls.expected = cls.expectation(cls.objective)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_objective(self, objective, first, second=None, expected=None):
        host = self.make_host(objective)
        try:
            self.assertIsNone(oa.preflight(host))
            return oa.run_objective(objective, TwoSessions(first, second or resume_session()),
                                    host, expected or self.expected, self.t)
        finally:
            host.close()

    def test_oracle_expects_records_and_gaps(self):
        self.assertGreaterEqual(len(self.expected["objects"]), 5)
        self.assertEqual(sorted(gap["state"] for gap in self.expected["gaps"]),
                         ["absent_in_source", "not_ingested"])

    def test_good_session_scores_full_marks(self):
        row = self.run_objective(self.objective, first_session(self.objective, self.t))
        self.assertEqual(row["discovery_recall"], 1.0)
        self.assertEqual(row["grounded_proposals"], 1)
        self.assertEqual(row["proposals"], 2)
        self.assertEqual(row["grounded_facts"], 1)
        self.assertEqual(row["action_calls"], 0)
        self.assertEqual(row["action_free"], 1)
        self.assertEqual(row["mc_sent"], 0)
        self.assertEqual(row["false_gaps"], 0)
        self.assertTrue(row["chain_intact"])
        self.assertTrue(row["resume_claimed"])
        self.assertFalse(row["resume_case_reopened"])
        self.assertEqual(row["resume_duplicates"], 0)
        summary = oa.part_summary([row])
        self.assertEqual(summary["grounded_proposal_share"], 1.0)
        self.assertEqual(summary["confident_unsupported"], 0.0)
        self.assertEqual(summary["false_gap_rate"], 0.0)
        self.assertTrue(oa.meets_bar(summary))

    def test_proposal_from_an_unrecorded_gap_is_not_grounded(self):
        row = self.run_objective(self.objective,
                                 first_session(self.objective, self.t, gap_id="G9"))
        self.assertEqual(row["grounded_proposals"], 0)
        self.assertEqual(row["proposals_grounded"], 0)
        self.assertEqual(row["mc_sent"], 0)
        self.assertEqual(oa.part_summary([row])["grounded_proposal_share"], 0.0)

    def test_fact_citing_an_unread_row_is_unsupported(self):
        other = self.objectives["obj-fx-004"]
        # Another order's row, read by the harness, never by the session.
        found = self.make_host(other)
        try:
            unread = next(iter(handles_of(found.read("plx.investigate", {
                "family": "F1", "root": _root(other), "as_known_at": self.t})).values()))
        finally:
            found.close()
        row = self.run_objective(self.objective,
                                 first_session(self.objective, self.t, extra_fact=unread))
        self.assertEqual(row["grounded_facts"], 0)
        self.assertEqual(row["facts_unsupported"], 1)
        self.assertGreater(oa.part_summary([row])["confident_unsupported"], 0.02)
        self.assertEqual(row["grounded_proposals"], 1)

    def test_calling_plx_act_is_an_action_call_and_is_refused(self):
        row = self.run_objective(self.objective,
                                 first_session(self.objective, self.t, act=True))
        self.assertEqual(row["action_calls"], 1)
        self.assertEqual(row["action_free"], 0)
        self.assertEqual(row["action_tools"], ["plx.act"])
        self.assertGreaterEqual(row["refused_tool_calls"], 1)
        self.assertEqual(row["mc_sent"], 0)
        self.assertFalse(oa.meets_bar(oa.part_summary([row])))

    def test_resume_recording_a_held_gap_is_a_duplicate(self):
        row = self.run_objective(self.objective, first_session(self.objective, self.t),
                                 resume_session(duplicate=True))
        self.assertTrue(row["resume_claimed"])
        self.assertEqual(row["resume_duplicates"], 1)

    def test_objectives_naming_a_family_or_an_unplanned_root_run_unchanged(self):
        injected = oa.check_objective({
            "id": "obj-family-keyword", "root": dict(self.objective["root"]),
            "goal": "Run F3 and the three-way match family on this order."})
        for objective in (self.objectives["obj-fx-005"], injected):
            with self.subTest(objective=objective["id"]):
                prompt = oa.objective_prompt(objective, self.t)
                self.assertIn(objective["goal"], prompt)
                expected = self.expectation(objective)
                # a family the goal names, then one no plan covers, then F1
                row = self.run_objective(objective, first_session(
                    objective, self.t, tries=("three_way_match", "close_out")),
                    expected=expected)
                self.assertGreaterEqual(row["no_plan_calls"], 1)
                self.assertIsNotNone(row["discovery_recall"])
                self.assertEqual(row["action_calls"], 0)
                self.assertTrue(row["chain_intact"])


    def test_live_part_runs_each_sampled_root_on_the_reader_host(self):
        """The live part's plumbing with the reader's host played by the fixture case
        host and dev F1 items standing in for the oracle's live sample (their
        ``known_outcome`` is the expectation): one row per item, scored."""
        from scripts import eval_dev_baseline as edb
        from scripts import eval_real_agent as era
        items = [item for item in runner.load_items(edb.DEV_DIR, split="dev")
                 if item["family"] == "F1"
                 and item["root"]["identifier_kind"] == "so_number"][:2]
        self.assertEqual(len(items), 2)
        for item in items:  # read at the fixture world's now, as a live item reads now
            item["as_known_at"] = {**item["as_known_at"], "time": self.t}
        hosts = []

        class ReaderHost:  # LiveHost's contract: the agent's session, the harness read
            def __init__(self, fixture):
                self.agent_session, self.mcp_server = fixture._start, fixture._server_spec
                self.read, self.close = fixture.base.read, fixture.close

        def host_for(item):
            hosts.append(item["id"])
            return ReaderHost(self.make_host(None))
        live = ((items, host_for, None, None), [], {"reader": "named"})
        with tempfile.TemporaryDirectory() as tmp:
            objectives = Path(tmp) / "objectives.json"
            data = json.loads(oa.FIXTURE_OBJECTIVES.read_text(encoding="utf-8"))
            data["objectives"] = data["objectives"][:1]
            objectives.write_text(json.dumps(data), encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(era, "live_part", lambda *a, **k: live), \
                    contextlib.redirect_stdout(out):
                code = oa.main(["run", "--work-dir", str(Path(tmp) / "work"), "--objectives",
                                str(objectives), "--live"], agent=PromptAgent(), environ={})
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["evidence_label"], "live-captures")
        part = report["eval"][oa.LIVE_PART]
        self.assertEqual(part["objectives"], 2)
        self.assertEqual(sorted(hosts), sorted(item["id"] for item in items))
        self.assertEqual(len({row["objective_id"] for row in part["rows"]}), 2)
        for row, item in zip(part["rows"], items):
            self.assertEqual(row["root"], {k: item["root"][k]
                                           for k in ("identifier", "identifier_kind")})
            # the item's own known outcome is the expectation (its chain, root aside)
            self.assertEqual(row["records_expected"],
                             len({
                                 oa._identity(entry) for entry in
                                 item["known_outcome"]["chain"][1:]}))
            self.assertGreater(row["records_found"], 0)
            self.assertGreater(row["discovery_recall"], 0)
            self.assertTrue(row["chain_intact"])
            self.assertTrue(row["resume_claimed"])
            self.assertEqual((row["action_calls"], row["mc_sent"]), (0, 0))
        self.assertEqual(report["eval"]["total"]["objectives"], 3)


class ReportTest(unittest.TestCase):
    """The run's report: blocked parts are typed, never scored."""

    def run_main(self, argv, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = oa.main(argv, **kwargs)
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_missing_binary_blocks_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.run_main(["run", "--work-dir", tmp, "--claude-bin",
                                    str(Path(tmp) / "no-claude")], environ={})
        self.assertEqual(report["agent_unmeasured"], "binary_not_found")
        self.assertFalse(report["eval"]["complete"])
        self.assertFalse(report["eval"]["meets_bar"])
        self.assertIsNone(report["eval"]["total"]["discovery_recall"])

    def test_live_part_without_a_reader_is_blocked_before_any_read(self):
        def refuse(*args, **kwargs):
            raise AssertionError("a source was built without a reader")
        with tempfile.TemporaryDirectory() as tmp:
            objectives = Path(tmp) / "objectives.json"
            data = json.loads(oa.FIXTURE_OBJECTIVES.read_text(encoding="utf-8"))
            data["objectives"] = data["objectives"][:1]
            objectives.write_text(json.dumps(data), encoding="utf-8")
            objective = data["objectives"][0]
            agent = TwoSessions(first_session(objective, data["as_known_at"]), resume_session())
            report = self.run_main(["run", "--work-dir", str(Path(tmp) / "work"),
                                    "--objectives", str(objectives), "--live"],
                                   agent=agent, environ={}, live_sources=refuse,
                                   live_make_host=refuse)
        self.assertEqual(report["agent_evidence"], "injected-transport")
        self.assertEqual(report["evidence_label"], "isolated-fixture")
        self.assertIn({"part": oa.LIVE_PART, "error_code": "no_employee_identity"},
                      [{k: part.get(k) for k in ("part", "error_code")}
                       for part in report["blocked_upstream"]])
        self.assertNotIn(oa.LIVE_PART, report["eval"])
        self.assertEqual(report["eval"]["fixture"]["objectives"], 1)
        self.assertEqual(report["eval"]["fixture"]["discovery_recall"], 1.0)
        self.assertIsNone(report["agent_unmeasured"])
        # The live check on this report: the fixture part meets expect_blocked but for the
        # agent evidence (a scripted agent is never the real one); the pass expectation
        # needs the live part.
        from verify import live_verify as lv
        spec = json.loads((Path(oa.ROOT) / "verify" / "live-checks"
                           / "eval.objective_agent.json").read_text(encoding="utf-8"))
        self.assertEqual(lv.validate_spec(spec), [])
        self.assertEqual(lv.check_expectations(spec, 0, report, spec["expect_blocked"]),
                         ["agent_evidence != expected"])
        self.assertIn("missing eval.live_bc", lv.check_expectations(spec, 0, report))


if __name__ == "__main__":
    unittest.main()
