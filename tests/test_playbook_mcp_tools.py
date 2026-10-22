"""The playbook adoption path's MCP tools (operating.playbook_tools): RSN-08, ENG-13.

Evidence label: ``isolated-fixture`` / ``injected-transport``. Cases are opened and read through
the real ``MCPServer`` / ``CaseRuntime`` over the in-process ``FakeWorkbench``; proposals are
scored by the registered ``playbook_eval.case_runner`` (unreplaced) on those cases and kept in a
real ``PlaybookReportStore``; the adoption target is the overlay of the test's temporary state
directory, and the shipped ``operating/playbooks`` (read, never written) is asserted
byte-identical after an adoption. The MCP client is simulated: the test plays the steward by
answering the ``elicitation/create`` form the tool sent. No network, no credential.
"""

from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from ontology.core import Actor
from ontology.json_contract import validate
from operating import confirmation_forms as cf
from operating import mcp_server
from operating import playbook_tools as pt
from operating import semantic_proposals as sp
from operating.ontology_tools import load_contracts
from operating.playbook_eval import proposal_store
from operating.playbook_reports import PlaybookReportStore
from operating.playbooks import PLAYBOOK_DIR, PlaybookRegistry, overlay_dir
from operating.tests.firewall_view import client_result
from operating.tests.test_mcp_case_tools import FINANCE, OPS, OPS_FINANCE, CaseToolsTestCase
from operator_ui.case_console import REPORTS_FILE, report_store_for

CONTRACTS = load_contracts()
PROPOSER = Actor("plx", frozenset({"service:proposer", *FINANCE}))
EVALUATOR = Actor("plx", frozenset({"service:playbook-evaluator", "staff:operations"}))
STEWARD = Actor("plx", frozenset({"employee:steward-1"}))
ELICITING = {"elicitation": {}}
AGENT = "plx.agent.playbook"
NEW_FILE = "customer_po_enrichment_v4.json"


class PlaybookToolsTest(CaseToolsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.rt = self.runtime()
        self.overlay = overlay_dir(self.state)
        self.shipped = self.shipped_bytes()
        self.edit = json.loads((PLAYBOOK_DIR / "customer_po_enrichment_v3.json").read_text())
        self.edit["version"] = 4
        self.edit["question_pattern"] += " (reworded)"
        self.evidence_case = self.open_case(PROPOSER.principals)
        self.configure([self.open_case(OPS) for _ in range(2)])

    def open_case(self, principals) -> str:
        return self.ok(self.server(principals, self.rt), "plx.case_open",
                       {"question": "Does the order carry a customer PO?"})["case_id"]

    def configure(self, frozen, stewards=("employee:steward-1",)) -> None:
        path = self.root / "playbook-proposals.json"
        path.write_text(json.dumps({"schema": pt.CONFIG_SCHEMA, "stewards": list(stewards),
                                    "frozen_cases": frozen}))
        self.frozen, self.config = frozen, pt.load_config(path)

    @staticmethod
    def shipped_bytes() -> dict:
        return {path.name: path.read_bytes() for path in sorted(PLAYBOOK_DIR.iterdir())}

    def session(self, actor: Actor, caps=None, agent=AGENT, tools=None,
                client="steward-client") -> mcp_server.MCPServer:
        tools = tools or mcp_server.playbook_session_tools(self.rt, actor, self.state, self.config)
        server = mcp_server.MCPServer(case_runtime=self.rt, actor=actor, agent=agent,
                                      playbook_proposals=tools)
        server.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": caps or {},
                                  "clientInfo": {"name": client, "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def tool(self, server, name, arguments) -> dict:
        """A tools/call as a client reads it; a result satisfies the tool's contract."""
        self.request_id += 1
        result = client_result(name, server.handle({
            "jsonrpc": "2.0", "id": self.request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})["result"])
        if not result["isError"]:
            self.assertEqual(validate(result["structuredContent"],
                                      CONTRACTS["tools"][name]["output"], root=CONTRACTS), [])
        return result

    def done(self, server, name, arguments) -> dict:
        result = self.tool(server, name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def refusal(self, server, name, arguments) -> str:
        result = self.tool(server, name, arguments)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    def propose(self, actor=PROPOSER, **changes) -> dict:
        arguments = {"file": NEW_FILE, "content": self.edit, "rationale": "reads clearer",
                     "evidence": [self.evidence_case], **changes}
        return self.done(self.session(actor), pt.PROPOSE, arguments)

    def records(self, state=None) -> list[dict]:
        path = (state or self.state) / "playbook-reports.sqlite"
        with PlaybookReportStore(path).changes() as (events, _):
            return list(events)

    def answer(self, server, request, action="accept", content=None) -> None:
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "id": request["id"], "result": {
            "action": action, "content": {"confirm": True} if content is None else content}}))

    def status(self, server, pid) -> tuple:
        out = self.done(server, pt.ADOPT, {"proposal_id": pid, "mode": "status"})
        return out["proposal_status"], out.get("last_answer")

    def evaluated(self) -> str:
        pid = self.propose()["proposal_id"]
        self.done(self.session(EVALUATOR), pt.EVALUATE, {"proposal_id": pid})
        return pid

    # ------------------------------------------------------------------ tests

    def test_tools_are_registered_with_their_contracts_for_the_playbook_agent_only(self):
        listed = self.session(PROPOSER).handle({"jsonrpc": "2.0", "id": 2,
                                                "method": "tools/list"})["result"]["tools"]
        self.assertEqual({tool["name"] for tool in listed}, {"plx.case_get", *pt.TOOL_NAMES})
        for tool in listed:
            if tool["name"] in pt.TOOL_NAMES:
                self.assertEqual(tool["inputSchema"], CONTRACTS["tools"][tool["name"]]["input"])
                self.assertFalse(tool["annotations"]["readOnlyHint"])
        # The case agents' listing keeps its budget; they cannot call the tools.
        case_agent = self.session(PROPOSER, agent=None)
        self.assertFalse(set(pt.TOOL_NAMES) & {tool["name"] for tool in case_agent.tools})
        self.assertIn("tool_not_allowed_for_agent", self.refusal(
            case_agent, pt.PROPOSE, {"file": NEW_FILE, "content": self.edit,
                                     "rationale": "r", "evidence": [self.evidence_case]}))
        # Without the host configuration nothing is served (default deny).
        bare = mcp_server.MCPServer(case_runtime=self.rt, actor=PROPOSER, agent=AGENT)
        bare.handle({"jsonrpc": "2.0", "id": "i", "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "c"}}})
        bare.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIn("playbook_proposals_not_configured",
                      self.refusal(bare, pt.EVALUATE, {"proposal_id": "sp-" + "0" * 24}))

    def test_a_proposal_needs_evidence_its_proposer_can_read(self):
        server = self.session(PROPOSER)
        base = {"file": NEW_FILE, "content": self.edit, "rationale": "reads clearer"}
        for arguments, code in (
                (base, "arguments_invalid:evidence"),
                ({**base, "evidence": []}, "arguments_invalid:evidence"),
                # a frozen case of the operations staff and a row finance may not read
                ({**base, "evidence": [self.frozen[0]]}, "evidence_not_readable:0"),
                ({**base, "evidence": [self.evidence_case, "api://portal/public.x/1@g1"]},
                 "evidence_not_readable:1"),
                ({**base, "evidence": ["a sentence, not a citation"]},
                 "evidence_not_readable:0")):
            with self.subTest(code=code):
                self.assertEqual(self.refusal(server, pt.PROPOSE, arguments), code)
        self.assertEqual(self.records(), [])
        proposed = self.propose()
        self.assertEqual((proposed["proposal_status"], proposed["version"]), ("proposed", 4))
        # the proposer is told nothing about the frozen cases
        self.assertFalse(set(self.frozen) & set(json.dumps(proposed).split('"')))
        # a case whose change chain was tampered with is no evidence
        db = sqlite3.connect(self.rt.cases.path)
        try:
            db.execute("DROP TRIGGER case_event_no_update")
            db.execute("UPDATE case_event SET payload_json=replace(payload_json, 'customer PO', "
                       "'forged PO') WHERE case_id=?", (self.evidence_case,))
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.refusal(server, pt.PROPOSE, {**base, "rationale": "again",
                                                           "evidence": [self.evidence_case]}),
                         "chain_broken:0")

    def test_propose_evaluate_and_a_steward_adopts_through_the_form(self):
        pid = self.propose()["proposal_id"]
        # The proposer cannot evaluate its own proposal; an agent session never evaluates.
        self.assertEqual(self.refusal(self.session(PROPOSER), pt.EVALUATE,
                                      {"proposal_id": pid}), "evaluator_is_proposer")
        agent = Actor("plx", frozenset({"agent:claude", "staff:operations"}))
        self.assertEqual(self.refusal(self.session(agent), pt.EVALUATE, {"proposal_id": pid}),
                         "evaluator_must_be_a_service_or_employee")
        evaluated = self.done(self.session(EVALUATOR), pt.EVALUATE, {"proposal_id": pid})
        self.assertEqual((evaluated["proposal_status"], evaluated["regression"],
                          evaluated["verdict"], [case["case_id"] for case in evaluated["cases"]]),
                         ("evaluated", False, "non_regressing", sorted(self.frozen)))
        # The contract requires the verdict: an answer without one is no evaluate output.
        self.assertTrue(validate({k: v for k, v in evaluated.items() if k != "verdict"},
                                 CONTRACTS["tools"][pt.EVALUATE]["output"], root=CONTRACTS))
        # Only one human steward is asked, by a form; nobody else is.
        for actor, caps, code in (
                (Actor("plx", frozenset({"employee:someone"})), ELICITING, "not_a_steward"),
                (Actor("plx", frozenset({"employee:steward-1", "agent:claude"})), ELICITING,
                 "steward_must_be_one_human_employee"),
                (EVALUATOR, ELICITING, "steward_must_be_one_human_employee"),
                (STEWARD, {}, "human_confirmation_surface_unavailable")):
            with self.subTest(code=code):
                server = self.session(actor, caps)
                self.assertEqual(self.refusal(server, pt.ADOPT, {"proposal_id": pid}), code)
                self.assertEqual(server.outbound, [])
        # A host store without a state directory adopts nothing: refused before any form.
        reports = report_store_for(self.rt, self.state / REPORTS_FILE, recorders=[STEWARD])
        stateless = pt.PlaybookTools(proposal_store(
            reports, frozen_cases=self.frozen, stewards=["employee:steward-1"]), CONTRACTS,
            cf.ConfirmationForms(self.root / "forms.sqlite"))
        server = self.session(STEWARD, ELICITING, tools=stateless)
        self.assertEqual(self.refusal(server, pt.ADOPT, {"proposal_id": pid}),
                         "adoption_directory_not_configured")
        self.assertEqual(server.outbound, [])
        server = self.session(STEWARD, ELICITING)
        asked = self.done(server, pt.ADOPT, {"proposal_id": pid})
        self.assertEqual((asked["confirmation"], asked["version"]),
                         ("awaiting_steward_confirmation", 4))
        [form] = server.outbound
        self.assertEqual(form["method"], "elicitation/create")
        self.assertNotIn(form["id"], json.dumps(asked))
        self.assertFalse((self.overlay / NEW_FILE).exists())   # asked, nothing adopted yet
        self.answer(server, form, content={"confirm": False})
        self.assertEqual(self.done(server, pt.ADOPT, {"proposal_id": pid, "mode": "status"}),
                         {"proposal_id": pid, "proposal_status": "evaluated",
                          "last_answer": "not_confirmed_by_the_steward",
                          "business_system_writes": 0})
        self.answer(server, form)   # a form is answerable once
        self.assertFalse((self.overlay / NEW_FILE).exists())
        server.outbound.clear()
        self.done(server, pt.ADOPT, {"proposal_id": pid})
        [form] = server.outbound
        self.answer(server, form)
        self.assertEqual(self.status(server, pid), ("adopted", "adopted"))
        # Applied through ProposalStore.adopt: decision, move, confirmation by the steward,
        # each naming the form answered and the client the session declared.
        confirmation = {"via": "mcp_elicitation", "form_id": form["id"],
                        "client_info": {"name": "steward-client", "version": "1"}}
        self.assertEqual([(e["schema"], e.get("steward") or e.get("by"), e["confirmation"])
                          for e in self.records()[-2:]],
                         [(sp.ADOPTION, "employee:steward-1", confirmation),
                          (sp.APPLIED, "employee:steward-1", confirmation)])
        # Written to the state directory's overlay only; the next load of the layers serves it,
        # and the shipped directory is byte-identical and alone still serves version 3.
        self.assertEqual([path.name for path in self.overlay.iterdir()], [NEW_FILE])
        self.assertEqual(self.shipped_bytes(), self.shipped)
        adopted = PlaybookRegistry().load_layers(overlay=self.overlay).get(self.edit["id"])
        self.assertEqual((adopted.version, adopted.definition["question_pattern"]),
                         (4, self.edit["question_pattern"]))
        self.assertEqual(PlaybookRegistry().load_layers().get(self.edit["id"]).version, 3)

    def test_a_form_is_answered_once_by_its_steward_before_expiry_even_after_a_restart(self):
        self.configure(self.frozen, stewards=("employee:steward-1", "employee:steward-2"))
        pid = self.evaluated()
        server = self.session(STEWARD, ELICITING)
        self.done(server, pt.ADOPT, {"proposal_id": pid})
        [form] = server.outbound
        # Another steward cannot answer the form that asked this one; trying spends it.
        other = self.session(Actor("plx", frozenset({"employee:steward-2"})), ELICITING)
        self.answer(other, form)
        self.answer(server, form)
        self.assertEqual(self.status(server, pid),
                         ("evaluated", "answerer_is_not_the_requester"))
        self.assertEqual(self.status(other, pid), ("evaluated", None))
        # The comparison the form named was replaced before the answer: nothing is adopted.
        self.done(server, pt.ADOPT, {"proposal_id": pid})
        second = Actor("plx", frozenset({"service:second-evaluator", *OPS}))
        self.done(self.session(second), pt.EVALUATE, {"proposal_id": pid})
        self.answer(server, server.outbound[-1])
        self.assertEqual(self.status(server, pid),
                         ("evaluated", "proposal_changed_since_request"))
        # An answer after the form expired adopts nothing either.
        self.done(server, pt.ADOPT, {"proposal_id": pid})
        later = datetime.now(timezone.utc) + timedelta(seconds=cf.DEFAULT_TTL_SECONDS + 1)
        with mock.patch.object(cf, "_now", lambda: later):
            self.answer(server, server.outbound[-1])
        self.assertEqual(self.status(server, pid), ("evaluated", "confirmation_request_expired"))
        self.assertFalse(self.overlay.exists())
        # A pending form survives a host restart: the steward's session on the restarted host
        # (new tools over the same state directory) answers it once; a replay is refused.
        self.done(server, pt.ADOPT, {"proposal_id": pid})
        form = server.outbound[-1]
        restarted = self.session(STEWARD, ELICITING, client="restarted-client")
        self.answer(restarted, form)
        self.assertEqual(self.status(restarted, pid), ("adopted", "adopted"))
        replay = {"jsonrpc": "2.0", "id": form["id"],
                  "result": {"action": "accept", "content": {"confirm": True}}}
        self.assertEqual(restarted.playbook_proposals.answer(replay, STEWARD),
                         "confirmation_request_confirmed")
        self.assertEqual([e["schema"] for e in self.records()].count(sp.ADOPTION), 1)
        self.assertEqual(self.records()[-1]["confirmation"]["form_id"], form["id"])
        self.assertEqual(json.loads((self.overlay / NEW_FILE).read_text()), self.edit)
        # The host's other forms share the store: a document-fields form is not an adoption
        # answer and stays answerable.
        forms = restarted.playbook_proposals.forms
        token, _ = forms.open("document_fields", "plx", "orig-1", "employee:steward-1", {})
        self.assertEqual(restarted.playbook_proposals.answer(dict(replay, id=token), STEWARD),
                         cf.UNKNOWN)
        self.assertEqual(forms.pending("document_fields", "plx", "orig-1"), 1)

    def test_a_move_that_fails_stays_pending_and_the_next_steward_form_completes_it(self):
        pid = self.evaluated()
        server = self.session(STEWARD, ELICITING)
        # The overlay folder cannot be created: nothing is recorded, the proposal stays
        # evaluated and the answer says so (a new request retries).
        real_mkdir = Path.mkdir

        def no_overlay(path, *args, **kwargs):
            if path == self.overlay:
                raise OSError("no space left on the state volume")
            return real_mkdir(path, *args, **kwargs)
        self.done(server, pt.ADOPT, {"proposal_id": pid})
        with mock.patch.object(Path, "mkdir", no_overlay):
            self.answer(server, server.outbound[-1])
        self.assertEqual(self.status(server, pid), ("evaluated", "adoption_not_recorded"))
        self.assertNotIn(sp.ADOPTION, [e["schema"] for e in self.records()])
        self.assertFalse(self.overlay.exists())
        self.done(server, pt.ADOPT, {"proposal_id": pid})

        def crash(src, dst):
            raise OSError("crash at the move")
        with mock.patch.object(sp.os, "replace", crash):
            self.answer(server, server.outbound[-1])
        self.assertEqual(self.status(server, pid), ("adoption_pending", "adoption_not_completed"))
        self.assertEqual(list(self.overlay.iterdir()), [])   # no file, no temp file
        # The session ends; the steward's next session completes the recorded adoption.
        again = self.session(STEWARD, ELICITING, client="restarted-client")
        asked = self.done(again, pt.ADOPT, {"proposal_id": pid})
        self.assertEqual((asked["proposal_status"], "version" in asked),
                         ("adoption_pending", False))
        self.answer(again, again.outbound[-1])
        self.assertEqual(self.status(again, pid), ("adopted", "adopted"))
        self.assertEqual([(e["schema"], e["confirmation"]["client_info"]["name"])
                          for e in self.records()[-2:]],
                         [(sp.ADOPTION, "steward-client"), (sp.APPLIED, "restarted-client")])
        self.assertEqual(json.loads((self.overlay / NEW_FILE).read_text()), self.edit)
        self.assertEqual(self.shipped_bytes(), self.shipped)

    def test_a_frozen_case_the_evaluator_reads_in_part_is_unscored_and_blocks_adoption(self):
        """Visible, never a silent pass: an evaluator from whom a cited finding is withheld
        scores nothing on that case, and only a reader of the whole case unblocks adoption."""
        case_id, _citation = self.open_case_with_fact(self.server(OPS_FINANCE, self.rt))
        self.configure([case_id])
        quality = frozenset({"staff:quality"})   # a proposer sharing no evaluator's principal
        pid = self.propose(Actor("plx", frozenset({"service:proposer", *quality})),
                           evidence=[self.open_case(quality)])["proposal_id"]
        partial = Actor("plx", frozenset({"service:finance-evaluator", *FINANCE}))
        evaluated = self.done(self.session(partial), pt.EVALUATE, {"proposal_id": pid})
        self.assertEqual((evaluated["regression"], evaluated["verdict"], evaluated["cases"]),
                         (False, "unscored", [{"case_id": case_id, "before_outcome": "unscored",
                                               "after_outcome": "unscored", "regressed": False}]))
        steward = self.session(STEWARD, ELICITING)
        self.assertEqual(self.refusal(steward, pt.ADOPT, {"proposal_id": pid}),
                         "comparison_unscored")
        self.assertEqual(steward.outbound, [])
        # The same incomplete read recorded before the full-case reader rule
        # (the old runner stored pass/fail and no unscored items) is never trusted: adoption
        # refuses it, before any form, until the edit is evaluated again.
        [partial_run] = [e for e in self.records() if e["schema"] == sp.COMPARISON]
        legacy = {key: value for key, value in partial_run.items()
                  if key not in ("unscored_items", "unscorable")}
        legacy.update(before={case_id: "fail"}, after={case_id: "fail"})
        with PlaybookReportStore(self.state / REPORTS_FILE).changes() as (_events, append):
            append(legacy)
        self.assertEqual(self.refusal(steward, pt.ADOPT, {"proposal_id": pid}),
                         "comparison_predates_full_read_rule")
        self.assertEqual(steward.outbound, [])
        full = self.done(self.session(EVALUATOR), pt.EVALUATE, {"proposal_id": pid})
        self.assertEqual((full["verdict"], full["cases"][0]["before_outcome"] != "unscored"),
                         ("non_regressing", True))
        self.done(steward, pt.ADOPT, {"proposal_id": pid})
        self.assertEqual([form["method"] for form in steward.outbound], ["elicitation/create"])

    def test_a_case_no_reader_can_read_is_unscorable_and_left_out_of_the_comparison(self):
        """A frozen case whose cited row no reader can resolve (the mirror was rebuilt) is
        unscorable for everyone, with its reason: adoption refuses only when fewer than half of
        the frozen cases are left to score, never because of that one case. (A case withheld
        from this evaluator only stays unscored: test_a_frozen_case_the_evaluator_reads_in_...)"""
        hidden, _citation = self.open_case_with_fact(self.server(OPS_FINANCE, self.rt))
        hidden_too, _citation = self.open_case_with_fact(self.server(OPS_FINANCE, self.rt))
        scored = self.open_case(OPS)
        for frozen, verdict, refusal in (([hidden], "unscorable", "no_scorable_case"),
                                         ([hidden, hidden_too, scored], "unscorable",
                                          "too_few_scored_cases"),
                                         ([hidden, scored], "non_regressing", None)):
            with self.subTest(frozen=len(frozen)):
                self.configure(frozen)
                pid = self.propose()["proposal_id"]
                # every citation minted before the rebuild resolves for nobody
                with mock.patch.object(self.rt.mirror, "generation", "f" * 16):
                    evaluated = self.done(self.session(EVALUATOR), pt.EVALUATE,
                                          {"proposal_id": pid})
                cases = {case["case_id"]: case for case in evaluated["cases"]}
                self.assertEqual((evaluated["regression"], evaluated["verdict"], cases[hidden]),
                                 (False, verdict, {
                                     "case_id": hidden, "before_outcome": "unscorable",
                                     "after_outcome": "unscorable", "regressed": False,
                                     "unscorable_reasons": ["mirror_rebuilt"]}))
                steward = self.session(STEWARD, ELICITING)
                if refusal:
                    self.assertEqual(self.refusal(steward, pt.ADOPT, {"proposal_id": pid}),
                                     refusal)
                    self.assertEqual(steward.outbound, [])
                    continue
                # the other case is scored (empty, it fails on both sides) and compared
                self.assertEqual((cases[scored]["before_outcome"],
                                  cases[scored]["after_outcome"]), ("fail", "fail"))
                self.done(steward, pt.ADOPT, {"proposal_id": pid})
                # the steward is told what was scored, never that every case ran
                message = steward.outbound[-1]["params"]["message"]
                self.assertIn("scored 1 of 2 frozen cases", message)
                self.assertIn("1 left out, unscorable for every reader (mirror_rebuilt)", message)
                self.assertNotIn("ran every frozen case", message)
                self.answer(steward, steward.outbound[-1])
                self.assertEqual(self.status(steward, pid), ("adopted", "adopted"))

    def test_the_host_command_line_serves_the_tools_over_its_state_directory(self):
        state = self.root / "fixture-host"   # a fixture host never opens this live state
        argv = ["--state-dir", str(state), "--tenant", "plx", "--fixture",
                "--principal", "staff:operations", "--source-mirror-audience",
                str(self.audience_path), "--agent", AGENT]
        host, server = mcp_server.session_server(
            [*argv, "--playbook-proposals", str(self.root / "playbook-proposals.json")])
        self.addCleanup(host.case_runtime.close)
        self.assertIsInstance(server.playbook_proposals, pt.PlaybookTools)
        server.handle({"jsonrpc": "2.0", "id": "i", "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "c"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        evidence = host.case_runtime.cases.open(Actor("plx", OPS), "Is the PO carried?")
        proposed = self.done(server, pt.PROPOSE, {
            "file": NEW_FILE, "content": self.edit, "rationale": "reads clearer",
            "evidence": [evidence]})
        self.assertEqual([e["id"] for e in self.records(state)], [proposed["proposal_id"]])
        broken = self.root / "broken.json"
        broken.write_text(json.dumps({"schema": pt.CONFIG_SCHEMA, "stewards": ["agent:x"],
                                      "frozen_cases": self.frozen}))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mcp_server.session_server([*argv, "--playbook-proposals", str(broken)])

    def test_an_evaluator_outside_a_frozen_case_learns_nothing_and_nothing_is_compared(self):
        pid = self.propose()["proposal_id"]
        outsider = Actor("plx", frozenset({"service:playbook-evaluator"}))
        self.assertEqual(self.refusal(self.session(outsider), pt.EVALUATE,
                                      {"proposal_id": pid}), "evaluation_incomplete")
        self.assertNotIn(sp.COMPARISON, [e["schema"] for e in self.records()])
        steward = self.session(STEWARD, ELICITING)
        self.assertEqual(self.refusal(steward, pt.ADOPT, {"proposal_id": pid}),
                         "no_comparison")
        self.assertEqual(steward.outbound, [])

    def test_a_regressing_edit_is_recorded_and_never_adopted(self):
        regressing = copy.deepcopy(self.edit)
        regressing["success_checks"] = regressing["success_checks"][1:]   # weakens a check
        pid = self.propose(content=regressing)["proposal_id"]
        evaluated = self.done(self.session(EVALUATOR), pt.EVALUATE, {"proposal_id": pid})
        self.assertTrue(evaluated["regression"])
        self.assertTrue(evaluated["playbook_reasons"])
        server = self.session(STEWARD, ELICITING)
        self.assertEqual(self.refusal(server, pt.ADOPT, {"proposal_id": pid}),
                         "comparison_regresses")
        self.assertEqual(server.outbound, [])

    @mock.patch.object(mcp_server, "CALLS_PER_MINUTE", 7)
    def test_every_frozen_case_is_compared_once_at_each_cardinality(self):
        # The reads wait out the rate limit on their one server (never a fresh one that would
        # reset it): a fake clock that each wait advances.
        clock, waits, servers = [0.0], [], []
        real_init = mcp_server.MCPServer.__init__

        def wait(seconds):
            waits.append(seconds)
            clock[0] += seconds

        def counted(server, *args, **kwargs):
            servers.append(server)
            real_init(server, *args, **kwargs)
        for count in (1, 10, 60):
            with self.subTest(frozen=count):
                self.configure([self.open_case(OPS) for _ in range(count)])
                self.edit["version"] = 4 + count   # a fresh edit per cardinality
                pid = self.propose(file=f"customer_po_enrichment_v{4 + count}.json")["proposal_id"]
                evaluator = self.session(EVALUATOR)
                servers.clear()
                waits.clear()
                with mock.patch.object(mcp_server, "monotonic", lambda: clock[0]), \
                        mock.patch.object(mcp_server, "sleep", wait), \
                        mock.patch.object(mcp_server.MCPServer, "__init__", counted):
                    evaluated = self.done(evaluator, pt.EVALUATE, {"proposal_id": pid})
                self.assertEqual(([case["case_id"] for case in evaluated["cases"]],
                                  evaluated["verdict"]), (sorted(self.frozen), "non_regressing"))
                self.assertEqual(len(servers), 1)
                self.assertEqual(bool(waits), count > 1)
                self.assertTrue(all(0 < seconds <= 60 for seconds in waits))

    def test_host_configuration_fails_closed(self):
        path = self.root / "config.json"
        good = {"schema": pt.CONFIG_SCHEMA, "stewards": ["employee:steward-1"],
                "frozen_cases": [self.frozen[0]]}
        for change in ({"stewards": ["agent:claude"]}, {"stewards": ["service:x"]},
                       {"stewards": []}, {"frozen_cases": ["AO-1"]}, {"frozen_cases": []},
                       {"extra": 1}, {"schema": "other/v1"}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**good, **change}))
                with self.assertRaises(pt.PlaybookConfigError):
                    pt.load_config(path)


if __name__ == "__main__":
    unittest.main()
