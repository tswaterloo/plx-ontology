"""The app's Ask on the scoreboard's agent path (``operator_api.agent_ask``) over the
console's fixture world, the session a scripted Claude Code stream (injected-transport):
the reader's own host and the card prompt, progress streamed as the session runs, the
answer only what the session's answer gate passed, sources opened only for this reader,
the hard cap answering partial or "still working", the late-PO list from the data Home
shows, the reader identity failing closed, and the source page showing the field that
ties a migrated purchase order to the number asked about."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from evals import runner
from operating import proactive_engine
from operator_api import agent_ask as aa
from operator_api import backend as be
from operator_api import models as m
from operator_api.tests.test_backend_fixture import OPS, Served, events
from operator_ui import case_console
from operator_ui import object_workspace as objects
from operator_ui.tests import case_fixtures

def tool_use(n, name, arguments):
    return {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": f"t{n}", "name": runner.mcp_tool_name(name),
        "input": arguments}]}}


def tool_result(n, result):
    return {"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": f"t{n}", "content": json.dumps(result)}]}}


def envelope(text):
    return {"origin": {"citation": "workbench://plx.case_get"}, "untrusted_text": text}


def decision(handle, value, *, outcome="answered", text=None):
    """A gate decision as ``plx.case_get`` answer mode returns it (envelopes and all)."""
    claim = {"id": "m1", "check": {"status": "passed", "failures": []},
             "citations": [envelope(handle)],
             "text": envelope(f"The order status is {value}."),
             "value_claims": [{"citation": handle, "field": "row.status", "kind": "text",
                               "value": {"origin": {"citation": handle},
                                         "untrusted_text": value}}]}
    return {"outcome": outcome, "claims": [claim], "unsupported_claims": [],
            "answerability": {"state": "answerable"},
            "reply": {"text": envelope(text or f"The order status is {value}.")}}


class ScriptedSession:
    """A Claude Code session's stream: each line handed to ``on_line`` with a pause, then
    the run ``parse_stream_json`` makes of it (as ``ClaudeCodeAgent`` returns it)."""

    gates_final_reply = True

    def __init__(self, on_line, lines, final_text, *, stopped=False, pause=0.05,
                 seen=None):
        self.on_line, self.lines, self.final_text = on_line, lines, final_text
        self.stopped, self.pause, self.seen = stopped, pause, seen

    def run(self, prompt, host):
        if self.seen is not None:
            self.seen.update(prompt=prompt, host=host)
        for line in self.lines:
            self.on_line(json.dumps(line) + "\n")
            time.sleep(self.pause)
        run = runner.parse_stream_json([json.dumps(line) for line in self.lines],
                                       ["plx.find", "plx.object", "plx.case_get"])
        events = [{**event, "type": "tool_call"} for event in run.events]
        usage = {"subtype": "timeout", "is_error": True} if self.stopped else {}
        return runner.AgentRun(events, "" if self.stopped else self.final_text, usage, {})


def answer_block(handle, value, extra_text=""):
    return ("```json\n" + json.dumps({
        "format": "plx-eval-answer/v1", "answerability": "answerable",
        "text": f"The order status is {value}.{extra_text}",
        "values": [{"name": "order_status", "value": value,
                    "citations": [{"l1_handle": handle, "field_path": "row.status"}]}],
        "gaps": [{"state": "unknown", "subject": "receipt_date",
                  "reason": "no_receipt_recorded"}]}) + "\n```")


class AgentPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="app-agent-ask-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, store)
        proactive_engine.after_build(store)
        cls.ops = Served(cls.state, OPS, cls.tmp / "ops-app")
        cls.outsider = Served(cls.state, ["staff:outsider"], cls.tmp / "outsider-app")
        home = cls.ops.client.get("/api/home").json()
        cls.late = home["needs_attention"]["late_pos"]
        cls.handle = cls.late[0]["sources"][0]["handle"]

    @classmethod
    def tearDownClass(cls):
        cls.ops.stop()
        cls.outsider.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def use(self, served, make_session, seconds=60):
        backend = served.app.state.backend
        hosts = []

        def host_for(session):
            hosts.append(session)
            return SimpleNamespace(close=lambda: None)
        backend.agent_ask = aa.AgentAsk(seconds=seconds, host_for=host_for,
                                        agent_for=make_session)
        self.addCleanup(setattr, backend, "agent_ask", None)
        return hosts

    def ask(self, served, question):
        started = time.monotonic()
        seen = []
        with served.client.stream("POST", "/api/ask", json={"question": question}) as reply:
            self.assertEqual(reply.status_code, 200)
            buffer = ""
            for chunk in reply.iter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    seen.append((events(block)[0], time.monotonic() - started))
        return seen

    def test_ask_runs_the_card_path_as_the_reader_and_shows_only_what_the_gate_passed(self):
        status = "Overdue"
        lines = [tool_use(1, "plx.find", {"query": "late order"}),
                 tool_result(1, {"status": "found", "candidates": []}),
                 tool_use(2, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(2, decision(self.handle, status))]
        prose = (f"The order status is {status}. It shipped 999 totes last week.\n\n"
                 + answer_block(self.handle, status))
        seen = {}
        hosts = self.use(self.ops, lambda on_line: ScriptedSession(
            on_line, lines, prose, pause=0.3, seen=seen))
        question = "Why is our latest purchase order behind?"
        got = self.ask(self.ops, question)
        names = [name for (name, _), _ in got]
        self.assertEqual(names[0], "status")
        self.assertLess(got[0][1], 3.0)                    # the first event comes at once
        for (name, data), _ in got:
            m.ASK_EVENTS[name].model_validate(data)
        steps = [data["message"] for (name, data), _ in got if name == "status"]
        self.assertIn("Looking up records", steps)         # each call streamed as it starts
        self.assertIn("Checking every figure against its source", steps)
        reading = next(t for (n, d), t in got if d.get("message") == "Looking up records")
        self.assertLess(reading, got[-1][1] - 0.5)         # ...before the session ended
        # the card runner's prompt, for this reader's own session, then the app's
        # business-reader wording rule (the scoreboard's prompt never carries it)
        self.assertEqual(seen["prompt"], aa.app_prompt(question))
        self.assertTrue(seen["prompt"].startswith(runner.ask_prompt(question)))
        self.assertEqual(seen["prompt"][len(runner.ask_prompt(question)):],
                         "\n\n" + aa.APP_READER_RULE)
        self.assertEqual(sorted(hosts[0].principals), sorted(OPS))
        answer = got[-1][0][1]
        self.assertEqual(answer["outcome"], "answered")
        self.assertIn(f"The order status is {status}.", answer["direct_answer"])
        self.assertNotIn("999", answer["direct_answer"])    # no passed claim backs it
        self.assertNotIn("```", answer["direct_answer"])
        self.assertNotIn("plx-eval-answer", answer["direct_answer"])
        fact = answer["facts"][0]
        self.assertEqual(fact["check"], "passed")
        by_handle = {s["handle"]: s for s in fact["sources"]}
        self.assertEqual(by_handle[self.handle]["fields"], {"status": status})
        self.assertTrue(by_handle[self.handle]["open_url"])
        self.assertIn("receipt date", [n["what"] for n in answer["not_found"]])
        opened = self.ops.client.get("/api/source", params={"handle": self.handle})
        self.assertEqual(opened.status_code, 200, opened.text)
        # the same citation never opens for a reader granted none of it
        self.use(self.outsider, lambda on_line: ScriptedSession(on_line, lines, prose))
        other = self.ask(self.outsider, question)[-1][0][1]
        self.assertEqual(other["facts"], [])
        self.assertEqual(other["hidden"]["count"], 1)
        self.assertNotIn(self.handle, json.dumps(other))
        self.assertNotIn(status, json.dumps(other))
        self.assertEqual(self.outsider.client.get(
            "/api/source", params={"handle": self.handle}).status_code, 404)

    def test_a_session_stopped_at_the_cap_answers_partial_or_still_working(self):
        lines = [tool_use(1, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(1, decision(self.handle, "Overdue"))]
        self.use(self.ops, lambda on_line: ScriptedSession(on_line, lines, "", stopped=True))
        answer = self.ask(self.ops, "What is the status of the late order?")[-1][0][1]
        self.assertEqual(answer["outcome"], "partial")
        self.assertTrue(answer["direct_answer"].startswith(be.PARTIAL_AT_LIMIT))
        self.assertIn("Overdue", answer["direct_answer"])
        self.assertTrue(answer["facts"])
        self.use(self.ops, lambda on_line: ScriptedSession(
            on_line, [tool_use(1, "plx.find", {"query": "x"})], "", stopped=True),
            seconds=45)
        answer = self.ask(self.ops, "What is the status of the late order?")[-1][0][1]
        self.assertEqual(answer["outcome"], "abstained")
        self.assertEqual(answer["direct_answer"], be.STILL_WORKING.format(seconds=45))
        self.assertEqual(answer["facts"], [])

    def test_late_purchase_orders_answer_from_the_list_home_shows(self):
        self.use(self.ops, lambda on_line: self.fail("the list never runs a session"))
        answer = self.ask(self.ops, "Which purchase orders are late?")[-1][0][1]
        self.assertTrue(self.late)
        self.assertEqual(answer["outcome"], "answered")
        self.assertEqual([s["object_id"] for s in answer["subjects"]],
                         [p["object_id"] for p in self.late])
        self.assertTrue(answer["direct_answer"].startswith(f"{len(self.late)} purchase order"))
        for fact, po in zip(answer["facts"], self.late):
            self.assertIn(f"{po['days_late']} days late", fact["text"])
            self.assertEqual([s["handle"] for s in fact["sources"]],
                             [s["handle"] for s in po["sources"]])
        # a reader granted none of those records sees none of them
        self.use(self.outsider, lambda on_line: self.fail("no session"))
        other = self.ask(self.outsider, "Which purchase orders are late?")[-1][0][1]
        text = json.dumps(other)
        for po in self.late:
            self.assertNotIn(po["object_id"], text)
        self.assertEqual(other["facts"], [])

    def test_only_the_unqualified_list_question_takes_the_late_list(self):
        for asked in ("Which purchase orders are late?", "which POs are late",
                      "What's late?", "late purchase orders", "show me overdue POs",
                      "Are there any POs past due?", "Which POs are late right now?"):
            self.assertTrue(be.late_list_question(asked), asked)
        for asked in ("Why is our latest PO late?", "Which of Acme's POs are late?",
                      "Which POs from Thames Chemicals are late?",
                      "Show POs that are not late", "Which POs were late last quarter?",
                      "Which POs will be late next week?", "Which open POs are late?",
                      "Which purchase orders are late for PO5455?",
                      "Which purchase orders did we receive?",
                      "Which of Maria's purchase orders are overdue?",
                      f"is {case_fixtures.PO_NUMBER} late?", "Are POs 12 and 14 late?"):
            self.assertFalse(be.late_list_question(asked), asked)

    def test_a_host_that_does_not_start_falls_back_to_the_quicker_answer(self):
        def broken(session):
            raise RuntimeError("host_list_timeout")
        backend = self.ops.app.state.backend
        backend.agent_ask = aa.AgentAsk(seconds=30, host_for=broken,
                                        agent_for=lambda on_line: self.fail("no session"))
        self.addCleanup(setattr, backend, "agent_ask", None)
        got = self.ask(self.ops, f"is {case_fixtures.PO_NUMBER} late?")
        self.assertEqual(got[-1][0][0], "answer")
        answer = got[-1][0][1]
        self.assertTrue(answer["direct_answer"].startswith(be.AGENT_FALLBACK))
        self.assertTrue(answer["facts"])

    def test_a_named_late_po_answers_with_the_lateness_home_shows(self):
        po = self.late[0]
        lines = [tool_use(1, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(1, decision(self.handle, "Overdue"))]
        prose = ("Its lateness can't be decided from what I can read. The system gave no "
                 "verdict.\n\n- The order status is Overdue (`row.status`).\n\n"
                 + answer_block(self.handle, "Overdue").replace(
                     '"subject": "receipt_date"', '"subject": "late_verdict"'))
        for served in (self.ops, self.outsider):
            self.use(served, lambda on_line: ScriptedSession(on_line, lines, prose))
        question = f"whats the status on {po['label'].lower()}"
        answer = self.ask(self.ops, question)[-1][0][1]
        first = answer["direct_answer"].splitlines()[0]
        self.assertTrue(first.startswith(f"{po['label']} is {po['days_late']} days late"),
                        first)
        # the agent's own unchecked hedge and its lateness gap give way to the late
        # list's fact; the other sentences stay
        self.assertNotIn("can't be decided", answer["direct_answer"])
        self.assertIn("The system gave no verdict.", answer["direct_answer"])
        self.assertNotIn("late verdict", json.dumps(answer["not_found"]))
        self.assertNotIn("row.status", answer["direct_answer"])
        self.assertIn("The order status is Overdue", answer["direct_answer"])
        late = answer["facts"][0]
        self.assertEqual(late["text"], f"{po['label']}: {po['days_late']} days late. "
                                       f"{po['reason']}")
        self.assertEqual([s["handle"] for s in late["sources"]],
                         [s["handle"] for s in po["sources"]])   # Home's own evidence
        self.assertEqual(late["check"], "computed_from_cited_records")
        # a PO the late list does not hold, or a question not about where it stands,
        # gets no lateness
        for other in (f"whats the status on {case_fixtures.PO_NUMBER}",
                      f"who is the buyer on {po['label']}"):
            got = self.ask(self.ops, other)[-1][0][1]
            self.assertNotIn("days late", json.dumps(got), other)
        # a reader who cannot see that PO never learns its lateness
        other = self.ask(self.outsider, question)[-1][0][1]
        self.assertNotIn("days late", json.dumps(other))
        self.assertNotIn(po["object_id"], json.dumps(other))

    def test_a_lateness_hedge_stating_a_checked_value_stays(self):
        po = self.late[0]
        lines = [tool_use(1, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(1, decision(self.handle, "Overdue"))]
        prose = ("I couldn't confirm it is late, though the order status is Overdue.\n\n"
                 + answer_block(self.handle, "Overdue"))
        self.use(self.ops, lambda on_line: ScriptedSession(on_line, lines, prose))
        answer = self.ask(self.ops, f"whats the status on {po['label'].lower()}")[-1][0][1]
        self.assertIn("though the order status is Overdue", answer["direct_answer"])

    def test_a_late_po_a_source_calls_complete_shows_both_side_by_side(self):
        po = self.late[0]
        lines = [tool_use(1, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(1, decision(self.handle, "Complete"))]
        prose = ("The order status is Complete.\n\nIt is marked complete in the "
                 "Portal.\n\n" + answer_block(self.handle, "Complete"))
        self.use(self.ops, lambda on_line: ScriptedSession(on_line, lines, prose))
        answer = self.ask(self.ops, f"is {po['label']} late?")[-1][0][1]
        first = answer["direct_answer"].splitlines()[0]
        self.assertTrue(first.startswith(f"Home lists {po['label']} as {po['days_late']} "
                                         "days late"), first)
        self.assertIn("A source says: The order status is Complete.", first)
        # moved up, not repeated; and nothing else the gate passed is dropped
        self.assertEqual(answer["direct_answer"].count("The order status is Complete."), 1)
        self.assertIn("It is marked complete in the Portal.", answer["direct_answer"])

    def test_original_reads_are_capped_and_a_failing_host_is_not_found(self):
        backend = self.ops.app.state.backend
        reader = SimpleNamespace(key="a")
        other = SimpleNamespace(key="b")
        with mock.patch.object(be, "ORIGINAL_READ_WAIT", 0.05):
            with backend._original_reads(reader), backend._original_reads(reader):
                with self.assertRaises(be.errors.UserFacing):   # a third for one reader
                    with backend._original_reads(reader):
                        pass
                with backend._original_reads(other), backend._original_reads(other):
                    with self.assertRaises(be.errors.UserFacing):   # five overall
                        with backend._original_reads(SimpleNamespace(key="c")):
                            pass
            with backend._original_reads(reader):   # released again
                pass

        def broken(session):
            def call(name, arguments):
                raise RuntimeError("host_session_ended")
            return SimpleNamespace(call=call, close=lambda: None)
        backend.agent_ask = aa.AgentAsk(seconds=30, host_for=broken,
                                        agent_for=lambda on_line: self.fail("no session"))
        self.addCleanup(setattr, backend, "agent_ask", None)
        got = self.ops.client.get("/api/source",
                                  params={"handle": "original://nr-0123456789ab#page=1"})
        self.assertEqual(got.status_code, 404)

    def test_a_captured_original_opens_only_through_the_readers_host(self):
        backend = self.ops.app.state.backend
        handle = "original://nr-0123456789ab#page=1"
        calls = []

        def host_for(session):
            def call(name, arguments):
                calls.append((session, name, dict(arguments)))
                if arguments["citation"] == handle:
                    return {"status": "resolved", "page": 1,
                            "row": {"filename": "Map.zip", "sha256": "ab" * 32},
                            "text": envelope("The screening process, end to end."),
                            "next_text_offset": 34}
                return {"status": "refused", "error": {"code": "original_not_visible"}}
            return SimpleNamespace(call=call, close=lambda: None)
        backend.agent_ask = aa.AgentAsk(seconds=30, host_for=host_for,
                                        agent_for=lambda on_line: self.fail("no session"))
        self.addCleanup(setattr, backend, "agent_ask", None)
        opened = self.ops.client.get("/api/source", params={"handle": handle})
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["text"], "The screening process, end to end.")
        # read as the page's text under the file's name, paged as the host pages it
        self.assertEqual((opened.json()["kind"], opened.json()["title"],
                          opened.json()["fields"], opened.json()["next_offset"]),
                         ("text", "Map.zip, page 1", None, 34))
        self.assertEqual(calls[-1][1:], ("plx.read", {"citation": handle}))
        self.assertTrue(set(OPS) <= set(calls[-1][0].principals))   # as this reader
        # the reader's host refusing it is not found, never the text
        refused = self.ops.client.get("/api/source",
                                      params={"handle": "original://nr-ffffffffffff"})
        self.assertEqual(refused.status_code, 404)
        self.assertNotIn("screening", refused.text)
        # any other family never reaches the host path
        before = len(calls)
        self.assertEqual(self.ops.client.get(
            "/api/source", params={"handle": "graph-mail://box/AAA@g-1"}).status_code, 404)
        self.assertEqual(len(calls), before)

    def test_the_agents_gap_words_pass_the_reply_screen(self):
        lines = [tool_use(1, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(1, decision(self.handle, "Overdue"))]
        block = answer_block(self.handle, "Overdue").replace(
            '"subject": "receipt_date"', '"subject": "receipt of 7342 totes on 2026-01-05"')
        self.use(self.ops, lambda on_line: ScriptedSession(
            on_line, lines, "The order status is Overdue.\n\n" + block))
        answer = self.ask(self.ops, "What is the status of the late order?")[-1][0][1]
        text = json.dumps(answer["not_found"])
        self.assertNotIn("7342", text)
        self.assertIn("A value the question asks for", text)


class ItemRecheck(unittest.TestCase):
    def test_only_a_captured_original_the_app_cannot_recheck_stays_with_the_host_link(self):
        from operator_ui.object_workspace import ITEM_REFUSED, _withheld
        original, mail = "original://nr-0123456789ab#page=1", "graph-mail://box/AAA@g-1"

        class Access:
            def decide(self, handles):
                return {h: _withheld(ITEM_REFUSED) for h in handles}

        view = SimpleNamespace(access=Access(), _can_open=lambda h: True,
                               _call=lambda name, args: {}, _base=lambda: {})

        def claim(handle, text):
            return {"id": text, "check": {"status": "passed"}, "citations": [envelope(handle)],
                    "text": envelope(text), "value_claims": [{
                        "citation": handle, "field": "text", "value": envelope(text)}]}
        result = {"outcome": "answered", "claims": [claim(original, "The map covers screening."),
                                                    claim(mail, "SECRET mail line.")],
                  "unsupported_claims": [], "reply": {"text": envelope("x")}}
        events = [{"type": "tool_call", "name": runner.ANSWER_GATE_TOOL,
                   "arguments": {"answer": {}}, "result": result, "is_error": False}]
        ran = aa.AskRun(final_text="The map covers screening. SECRET mail line.", answer=None,
                        events=events, decision=None, stopped=False, seconds=1.0)
        backend = be.WorkspaceBackend(lambda s: None, data_label="test")
        answer = be.agent_answer(backend, view, m.AskRequest(question="q"), ran, {}, "c",
                                 lambda: 1)
        self.assertEqual([f.text for f in answer.facts], ["The map covers screening."])
        # it links to the source page, which opens it only through the reader's own host
        # (AgentPath.test_a_captured_original_opens_only_through_the_readers_host)
        self.assertEqual(answer.facts[0].sources[0].open_url, be.open_url(original))
        self.assertEqual(answer.hidden.count, 1)
        self.assertNotIn("SECRET", answer.model_dump_json())


class ReaderIdentity(unittest.TestCase):
    TENANT = "11111111-1111-4111-8111-111111111111"
    OID = "22222222-2222-4222-8222-222222222222"

    def test_the_ticket_names_the_signed_in_employee_and_fails_closed_otherwise(self):
        employee = SimpleNamespace(kind="employee", tenant_id=self.TENANT, object_id=self.OID)
        session = SimpleNamespace(employee=employee, email="reader@example.com",
                                  portal_user_id="u-7", identities=frozenset())
        self.assertEqual(aa.ticket_claims(session), {
            "uid": "u-7", "email": "reader@example.com", "entraTenantId": self.TENANT,
            "entraObjectId": self.OID})
        for refused in (SimpleNamespace(employee=None, email="x@example.com"),
                        SimpleNamespace(employee=SimpleNamespace(kind="agent"), email="x@y"),
                        SimpleNamespace(employee=employee, email=None, identities=frozenset(
                            {self.OID}), portal_user_id=None)):
            with self.assertRaises(aa.ReaderRefused):
                aa.ticket_claims(refused)
        # no employee: the agent path is not this reader's (the objective box answers)
        self.assertFalse(aa.AgentAsk(state_dir=Path("."), host_config=Path("x"),
                                     secret="s" * 40).serves(SimpleNamespace(employee=None)))
        self.assertIsNone(aa.AgentAsk.from_args(Path("."), None, {}))

    def test_the_agent_and_prompt_are_the_scoreboards(self):
        from operating import agent_session
        from scripts.eval_case_cards import CARD_AGENT
        self.assertEqual(aa.AGENT, CARD_AGENT)
        question = "Which lot did we receive?"
        self.assertEqual(agent_session.ask_prompt(question),
                         runner.build_prompt(runner.UNPINNED_ITEM, question))
        self.assertNotIn(aa.APP_READER_RULE, runner.build_prompt(runner.UNPINNED_ITEM,
                                                                   question))
        defs = runner._schema()["$defs"]
        self.assertEqual(list(agent_session.QUANTITY_ROLES), defs["quantity_role"]["enum"])
        self.assertEqual(list(agent_session.GAP_STATES), defs["gap_state"]["enum"])


class Streaming(unittest.TestCase):
    def test_lines_arrive_while_the_session_runs_and_the_cap_kills_it(self):
        got = []
        spawn = runner.streaming_spawn(lambda line: got.append((line, time.monotonic())))
        script = ("import sys,time\nfor i in range(3):\n print(i, flush=True)\n"
                  " time.sleep(0.4)\n")
        started = time.monotonic()
        code, out = spawn([sys.executable, "-c", script], ".", {}, 30)
        self.assertEqual((code, out.split()), (0, ["0", "1", "2"]))
        self.assertLess(got[0][1] - started, got[-1][1] - started - 0.5)
        with self.assertRaises(subprocess.TimeoutExpired) as stopped:
            spawn([sys.executable, "-c", "import time\nprint('a', flush=True)\n"
                   "time.sleep(60)"], ".", {}, 1)
        self.assertEqual(stopped.exception.output, "a\n")


class SourcePage(unittest.TestCase):
    def test_a_nested_field_tying_the_record_to_the_number_asked_is_shown(self):
        # A Portal PO migrated from FileMaker: its own number is not the one asked about;
        # the FileMaker number it carries sits in a nested field the page used to drop.
        row = {"po_number": "PO-200", "status": "Complete",
               "custom_fields": {"fmPoNumber": "PO#17", "fmGeneratedPoId": 42},
               "lines": [{"sku": "x"}], "note": {"untrusted_text": "hello"}}
        fields = be.record_fields(row)
        self.assertEqual(fields["custom_fields.fmPoNumber"], "PO#17")
        self.assertEqual(fields["custom_fields.fmGeneratedPoId"], "42")
        self.assertEqual(fields["po_number"], "PO-200")
        self.assertEqual(fields["note"], "hello")
        self.assertNotIn("lines", fields)


if __name__ == "__main__":
    unittest.main()
