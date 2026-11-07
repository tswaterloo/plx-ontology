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


class FailedSession:
    """A Claude Code session that ends as the CLI ends one whose login cannot run it: no
    tool call, an error result with the CLI's own words (``usage``: the run's usage)."""

    gates_final_reply = True

    def __init__(self, on_line, text, usage):
        self.on_line, self.text, self.usage = on_line, text, usage

    def run(self, prompt, host):
        line = {"type": "result", "subtype": "success", "is_error": True, "result": self.text}
        self.on_line(json.dumps(line) + "\n")
        run = runner.parse_stream_json([json.dumps(line)], ["plx.find"])
        return runner.AgentRun([], run.final_text, {**run.usage, **self.usage}, {})


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

    def ask_with(self, served, body):
        with served.client.stream("POST", "/api/ask", json=body) as reply:
            self.assertEqual(reply.status_code, 200)
            text = "".join(reply.iter_text())
        blocks = [events(b)[0] for b in text.split("\n\n") if b.strip()]
        return next(data for name, data in reversed(blocks) if name == "answer")

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
        self.assertIn("Checking the answer against the records", steps)
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

    def test_progress_names_plain_steps_and_the_verdict_comes_before_the_answer(self):
        """The reader watches the work in plain words (their own identifier, the sources
        their session was served, the check) and reads the gate-accepted first line before
        the full answer; a reader whose grant refuses the cited source sees neither."""
        status = "Overdue"
        po = "PurchaseOrder:bc.purchase_orders:x-1"
        lines = [tool_use(1, "plx.object", {"object_id": "Receipt:bc.purchase_receipts:r-9"}),
                 tool_result(1, {"object_id": "Receipt:bc.purchase_receipts:r-9"}),
                 tool_use(2, "plx.documents", {"object_id": po,
                                               "query": "order confirmation"}),
                 tool_result(2, {"documents": [{"kind": "email", "citation": "x"}]}),
                 tool_use(3, "plx.around", {"from": po}),
                 tool_result(3, {"error": "refused"}),
                 tool_use(4, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(4, decision(self.handle, status,
                                         text=f"The order status is {status}. Writing "
                                              "the rest takes a while."))]
        prose = f"The order status is {status}.\n\n" + answer_block(self.handle, status)
        self.use(self.ops, lambda on_line: ScriptedSession(on_line, lines, prose, pause=0.4))
        got = self.ask(self.ops, "What did the supplier confirm for PO 90412?")
        for (name, data), _ in got:
            m.ASK_EVENTS[name].model_validate(data)
        steps = [data["message"] for (name, data), _ in got if name == "status"]
        for step in ("Finding PO 90412", "Reading Business Central receipts",
                     "Checking supplier confirmations in mail",
                     "Checking the answer against the records"):
            self.assertIn(step, steps)
        self.assertLess(steps.index("Finding PO 90412"),
                        steps.index("Reading Business Central receipts"))
        for step in steps:   # never a tool name, an id, a handle or an argument
            for internal in ("plx.", "plx_", "://", ":bc.", "x-1", "r-9", "order confirmation"):
                self.assertNotIn(internal, step)
        self.assertNotIn("Reading Business Central purchase orders", steps)  # refused read
        verdicts = [(data, t) for (name, data), t in got if name == "verdict"]
        self.assertEqual([d["text"] for d, _ in verdicts], [f"The order status is {status}."])
        names = [name for (name, _), _ in got]
        self.assertLess(names.index("verdict"), names.index("token"))
        answer = got[-1][0][1]
        self.assertTrue(answer["direct_answer"].startswith(verdicts[0][0]["text"]))
        # the reader the cited source is withheld from: no verdict, no value, anywhere
        self.use(self.outsider, lambda on_line: ScriptedSession(on_line, lines, prose))
        other = self.ask(self.outsider, "What did the supplier confirm for PO 90412?")
        self.assertNotIn("verdict", [name for (name, _), _ in other])
        self.assertNotIn(status, json.dumps([data for (_, data), _ in other]))

    def test_a_one_record_question_answers_fast_through_the_gate_without_the_agent(self):
        started = []

        def no_agent(on_line):
            started.append(on_line)
            raise RuntimeError("the agent session is not this test's")
        for served in (self.ops, self.outsider):
            self.use(served, no_agent)
        question = "wheres 5447 at? is it late"
        got = self.ask(self.ops, question)
        for (name, data), _ in got:
            m.ASK_EVENTS[name].model_validate(data)
        self.assertLess(got[-1][1], 10.0)
        answer = got[-1][0][1]
        self.assertEqual(answer["outcome"], "partial")       # its lateness is unsettled
        self.assertIn("can't be settled", answer["direct_answer"])
        self.assertNotIn("```", answer["direct_answer"])
        fact = next(f for f in answer["facts"] if "REVISED" in f["text"])
        self.assertEqual(fact["check"], "passed")             # the gate's passed claim
        handle = fact["sources"][0]["handle"]
        self.assertEqual(self.ops.client.get("/api/source", params={"handle": handle})
                         .status_code, 200)
        self.assertEqual(started, [])                         # no agent session ran
        # a reader granted none of it learns nothing: no fast answer, the agent's turn
        other = self.ask(self.outsider, question)[-1][0][1]
        self.assertEqual(len(started), 1)
        self.assertNotIn("REVISED", json.dumps(other))
        self.assertNotIn(handle, json.dumps(other))

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
        # stopped with nothing passed: the assistant was unavailable (its time limit),
        # never "no records answer this"
        self.assertEqual(answer["outcome"], "abstained")
        self.assertTrue(answer["direct_answer"].startswith(
            "The assistant is unavailable right now (it reached its 45 s time limit)."))
        self.assertIn("narrow the question", answer["direct_answer"])
        self.assertNotIn("couldn't find", answer["direct_answer"])
        self.assertEqual(answer["assistant_unavailable"], "it reached its 45 s time limit")
        self.assertEqual(answer["facts"], [])

    def test_a_session_the_login_cannot_run_says_the_assistant_is_unavailable(self):
        """The CLI's weekly limit, a sign-in failure or a crash is the assistant being
        unavailable, said with its reason, never "no records you can see answer this";
        what the records read without it show stays (the late PO's trail and list fact)."""
        label = self.late[0]["label"]
        cases = [("You've hit your weekly limit \u00b7 resets Nov 2, 3pm (UTC)",
                  {"exit_code": 1}, "its usage limit has been reached"),
                 ("Claude AI usage limit reached|1793548800", {}, "its usage limit has been "
                                                                  "reached"),
                 ("Invalid API key \u00b7 Please run /login", {"exit_code": 1},
                  "it could not sign in"),
                 ("", {"exit_code": 137}, "it stopped with an error")]
        for text, usage, reason in cases:
            with self.subTest(reason=reason, text=text[:20]):
                self.use(self.ops, lambda on_line, t=text, u=usage: FailedSession(
                    on_line, t, u))
                answer = self.ask(self.ops, f"What is going on with PO {label}?")[-1][0][1]
                said = answer["direct_answer"]
                self.assertTrue(said.startswith(
                    f"The assistant is unavailable right now ({reason})."), said)
                self.assertNotIn("couldn't find", said)
                self.assertNotIn("weekly limit", said)   # the CLI's words stay out
                self.assertEqual(answer["assistant_unavailable"], reason)
                self.assertEqual(answer["outcome"], "partial")
                self.assertIn("Here is what the records show.", said)
                self.assertIn(label, said)
                self.assertTrue(answer["trail"] and answer["trail"]["trails"])

    def test_late_purchase_orders_answer_from_the_list_home_shows(self):
        self.use(self.ops, lambda on_line: self.fail("the list never runs a session"))
        answer = self.ask(self.ops, "Which purchase orders are late?")[-1][0][1]
        self.assertTrue(self.late)
        self.assertEqual(answer["outcome"], "answered")
        self.assertEqual([s["object_id"] for s in answer["subjects"]],
                         [p["object_id"] for p in self.late])
        self.assertTrue(answer["direct_answer"].startswith(f"{len(self.late)} purchase order"))
        self.assertIn("All of them are listed below", answer["direct_answer"])
        # each late order names its supplier as the reader reads it, on Home and here
        for po in self.late:
            self.assertTrue(po["supplier"], po)
            self.assertIn(f"{po['label']} from {po['supplier']}", answer["direct_answer"])
        # chat spellings answer from the same list, with the same count
        for asked in ("wat r the late pos", "any late pos rn?"):
            got = self.ask(self.ops, asked)[-1][0][1]
            self.assertEqual([s["object_id"] for s in got["subjects"]],
                             [p["object_id"] for p in self.late], asked)
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
                      "Are there any POs past due?", "Which POs are late right now?",
                      # chat spellings and typos (demo rehearsal 2026-10-31)
                      "wat r the late pos", "any late pos rn?", "late purchse ordres",
                      "whats overdue atm", "any late p.o.s?"):
            self.assertTrue(be.late_list_question(asked), asked)
        for asked in ("Why is our latest PO late?", "Which of Acme's POs are late?",
                      "Which POs from Thames Chemicals are late?",
                      "Show POs that are not late", "Which POs were late last quarter?",
                      "Which POs will be late next week?", "Which open POs are late?",
                      "Which purchase orders are late for PO5455?",
                      "Which purchase orders did we receive?",
                      "Which of Maria's purchase orders are overdue?",
                      f"is {case_fixtures.PO_NUMBER} late?", "Are POs 12 and 14 late?",
                      "which of our late purchase orders has the vendor confirmed a "
                      "delivery date for?", "older late pos", "any late pos from dsm rn?",
                      "wat r the late pos not confirmed"):
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

    def test_a_hedge_naming_the_po_gives_way_to_the_late_list(self):
        # demo rehearsal 2026-10-31: "PO 106093 is 111 days late ... I can't tell you
        # whether PO 106093 is late": the PO's own number is a checked value, so the
        # hedge naming it was kept as if it stated a checked fact
        po = self.late[0]
        lines = [tool_use(1, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(1, decision(self.handle, po["label"]))]
        prose = (f"I can't tell you whether PO {po['label']} is late, and I found no vendor "
                 f"confirmation.\n\nThe due date isn't proven, so I'm not calling it late."
                 f"\n\nThe order status is {po['label']}.\n\n"
                 + answer_block(self.handle, po["label"]))
        self.use(self.ops, lambda on_line: ScriptedSession(on_line, lines, prose))
        answer = self.ask(self.ops, f"is PO {po['label']} late, and did the vendor confirm "
                                    "the date?")[-1][0][1]
        text = answer["direct_answer"]
        self.assertTrue(text.startswith(f"{po['label']} is {po['days_late']} days late")
                        or text.startswith(f"PO {po['label']} is {po['days_late']} days late"),
                        text)
        self.assertNotIn("can't tell you whether", text)
        self.assertNotIn("not calling it late", text)
        self.assertIn(f"The order status is {po['label']}.", text)   # a checked fact stays

    def test_a_hedge_about_receipt_is_not_shown_as_a_contrary_source(self):
        po = self.late[0]
        lines = [tool_use(1, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(1, decision(self.handle, "Overdue"))]
        prose = ("I couldn't confirm whether it was fully received from a source you can "
                 "open.\n\nThe order status is Overdue.\n\n"
                 + answer_block(self.handle, "Overdue"))
        self.use(self.ops, lambda on_line: ScriptedSession(on_line, lines, prose))
        answer = self.ask(self.ops, f"whats the status on {po['label'].lower()}")[-1][0][1]
        first = answer["direct_answer"].splitlines()[0]
        self.assertTrue(first.startswith(f"{po['label']} is {po['days_late']} days late"),
                        first)
        self.assertNotIn("Not everything agrees", answer["direct_answer"])
        self.assertIn("The order status is Overdue.", answer["direct_answer"])

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
        self.assertIn("Not everything agrees: The order status is Complete.", first)
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
        # a SharePoint file the app's runtime cannot open goes the same way
        sp = "sharepoint://b%21DRIVE/01ITEM@%22%7BE%7D%2C1%22"
        calls.clear()
        self.assertEqual(self.ops.client.get("/api/source",
                                             params={"handle": sp}).status_code, 404)
        self.assertEqual(calls[-1][1:], ("plx.read", {"citation": sp}))
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

    def test_a_materials_question_gives_the_agent_the_walk_and_the_reply_agrees(self):
        """The materials walk is read before the session starts and handed to the agent
        in its task; the answer's table is that same walk (read once), and a reply saying
        no purchase order is linked gives way to the table's own count."""
        from operator_api import trail as tr
        ref = lambda oid, label: m.ObjectRef(object_id=oid, type=oid.split(":")[0],  # noqa
                                             label=label, open_url="/o")
        rows = [m.MaterialRow(assembly=ref("AssemblyOrder:x", "AO"),
                              item=ref(f"Product:i{k}", f"ITEM-{k}"),
                              po=ref(f"PurchaseOrder:p{k}", f"PO-{k}"),
                              state="undetermined" if k < 2 else None)
                 for k in range(5)]
        walked = m.EvidenceTrail(kind="materials", title=tr.materials_title(rows, 5),
                                 materials=rows, hidden_steps=0)
        calls = []

        def walk(mapper, view, starts, seconds=0):
            calls.append(starts)
            return walked
        lines = [tool_use(1, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(1, decision(self.handle, "Overdue"))]
        prose = (f"I couldn't find any purchase orders linked to {case_fixtures.AO_CODES[0]}, "
                 "so I can't say which are late. The order status is Overdue. Its "
                 "materials include ITEM-3.\n\n" + answer_block(self.handle, "Overdue"))
        seen = {}
        self.use(self.ops, lambda on_line: ScriptedSession(on_line, lines, prose, seen=seen))
        with mock.patch.object(tr, "materials_trail", side_effect=walk):
            got = self.ask(self.ops, f"which POs on {case_fixtures.AO_CODES[0]} are late?")
        answer = got[-1][0][1]
        self.assertEqual(len(calls), 1)                     # one walk, before the session
        self.assertEqual(calls[0][0][1], "AssemblyOrder")
        self.assertIn(walked.title, seen["prompt"])
        # the walk is a tool output in the task, never part of the question the reply is
        # screened against: a figure only it carries is not stated without a passed claim
        self.assertIn(aa.PREREAD_HEADING, seen["prompt"])
        self.assertTrue(seen["prompt"].startswith(aa.app_prompt(
            f"which POs on {case_fixtures.AO_CODES[0]} are late?")))
        self.assertNotIn("ITEM-3", answer["direct_answer"])
        self.assertIn("PO PO-0 (object id PurchaseOrder:p0) for item ITEM-0: lateness "
                      "undecided", seen["prompt"])
        # undetermined verdicts are not counted as checked
        self.assertEqual(walked.title, "From the recorded links: 5 purchase orders for this "
                                       "order's materials, 0 late of 0 with a lateness "
                                       "verdict; 5 undecided or not checked")
        self.assertTrue(answer["direct_answer"].startswith(walked.title + "."),
                        answer["direct_answer"])
        self.assertNotIn("couldn't find any purchase orders", answer["direct_answer"])
        self.assertIn("The order status is Overdue.", answer["direct_answer"])
        self.assertEqual(answer["trail"]["kind"], "materials")
        self.assertEqual(answer["trail"]["title"], walked.title)
        # a question naming no order walks nothing and the task carries no walk
        calls.clear()
        with mock.patch.object(tr, "materials_trail", side_effect=walk):
            self.ask(self.ops, "What is the status of the late order?")
        self.assertEqual(calls, [])
        self.assertNotIn("materials walk", seen["prompt"])

    def test_a_number_two_different_records_share_is_asked_back_and_the_pick_answers(self):
        """An identifier naming two different records (not one record's copies) gets a
        did-you-mean before any session; the pick runs the session on the earlier question
        about the picked record."""
        from operator_api import preread
        po = self.late[0]
        other = 'PurchaseOrder:bc.purchase_orders:{"id":"synthetic-other"}'
        real = preread.resolve

        def two(call, found, object_id=None, same=None, question=""):
            item = {"mention": po["label"], "kind": "petra_po", "candidates": [
                {"object_id": po["object_id"], "object_type": "PurchaseOrder"},
                {"object_id": other, "object_type": "PurchaseOrder"}]}
            return real(call, {**found, "ambiguous": [item]}, object_id,
                        same=lambda ids: False, question=question)
        lines = [tool_use(1, "plx.case_get", {"answer": {"claims": []}}),
                 tool_result(1, decision(self.handle, "Overdue"))]
        seen = {}
        self.use(self.ops, lambda on_line: ScriptedSession(
            on_line, lines, "The order status is Overdue.\n\n"
            + answer_block(self.handle, "Overdue"), seen=seen))
        question = f"why don't the invoice and receipt match on {po['label']}?"
        with mock.patch.object(preread, "resolve", side_effect=two):
            answer = self.ask(self.ops, question)[-1][0][1]
            self.assertEqual(seen, {})                       # no session ran
            self.assertTrue(answer["direct_answer"].endswith("Which did you mean?"))
            self.assertEqual([c["object_id"] for c in answer["did_you_mean"]],
                             [po["object_id"], other])
            self.assertEqual(answer["did_you_mean"][1]["label"],
                             f"{po['label']} in Business Central")
            pick = answer["did_you_mean"][0]
            with self.ops.client.stream("POST", "/api/ask", json={
                    "question": f"I mean {pick['label']}", "object_id": pick["object_id"],
                    "history": [{"role": "user", "text": question},
                                {"role": "assistant", "text": answer["direct_answer"]}]}
                    ) as reply:
                self.assertEqual(reply.status_code, 200)
                body = "".join(reply.iter_text())
        self.assertIn(question, seen["prompt"])          # the earlier question, answered
        self.assertIn(pick["object_id"], seen["prompt"])
        self.assertIn('"outcome"', body)
        # a pick of a record that settles neither asks the number back again
        seen.clear()
        with mock.patch.object(preread, "resolve", side_effect=two):
            again = self.ask_with(self.ops, {
                "question": "I mean the other one", "object_id": "Customer:x:1",
                "history": [{"role": "user", "text": question}]})
        self.assertEqual(seen, {})
        self.assertEqual([c["object_id"] for c in again["did_you_mean"]],
                         [po["object_id"], other])


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


    def test_a_sharepoint_file_the_readers_host_passed_shows_and_a_refused_one_is_counted(self):
        # Live 2026-10-30: every claim citing a SharePoint file was dropped (the app's item
        # re-check has no SharePoint gate), uncounted, and the answer said nothing.
        from operator_ui.object_workspace import ITEM_REFUSED, NOT_GRANTED, _withheld
        sp = "sharepoint://b%21DRIVE/01MAP@%22%7BE%7D%2C1%22"
        other = "sharepoint://b%21OTHER/01SECRET@%22%7BE%7D%2C1%22"

        class Access:
            def decide(self, handles):
                return {h: _withheld(NOT_GRANTED if h == other else ITEM_REFUSED)
                        for h in handles}

        view = SimpleNamespace(access=Access(), _can_open=lambda h: True,
                               _call=lambda name, args: {}, _base=lambda: {})

        def claim(handle, text, phrase):
            return {"id": phrase, "check": {"status": "passed"},
                    "citations": [envelope(handle)], "text": envelope(text),
                    "phrase": envelope(phrase), "value_claims": [{
                        "citation": handle, "field": "text", "value": envelope(phrase)}]}
        result = {"outcome": "answered",
                  "claims": [claim(sp, "Accepting a quote sets it to ACCEPTED.", "ACCEPTED"),
                             claim(other, "The tier is Zephyr-9.", "Zephyr-9")],
                  "unsupported_claims": [], "reply": {"text": envelope("x")}}
        events = [{"type": "tool_call", "name": runner.ANSWER_GATE_TOOL,
                   "arguments": {"answer": {}}, "result": result, "is_error": False}]
        prose = ("The map says accepting a quote sets its status to ACCEPTED.\n\n"
                 "The order carries the Zephyr-9 tier.")
        ran = aa.AskRun(final_text=prose, answer={"answerability": "answerable",
                                                  "text": "", "values": [], "gaps": []},
                        events=events, decision=None, stopped=False, seconds=1.0)
        backend = be.WorkspaceBackend(lambda s: None, data_label="test")
        with self.assertLogs("operator_api.ask", "INFO") as logged:
            answer = be.agent_answer(backend, view, m.AskRequest(question="q"), ran, {},
                                     "c", lambda: 1)
        self.assertEqual([f.text for f in answer.facts],
                         ["Accepting a quote sets it to ACCEPTED."])
        self.assertEqual(answer.facts[0].sources[0].open_url, be.open_url(sp))
        self.assertEqual(answer.outcome, "answered")
        # a grant refusal: only the shown facts are said (fail closed)
        self.assertEqual(answer.direct_answer, "Accepting a quote sets it to ACCEPTED.")
        self.assertEqual(answer.hidden.count, 1)                             # counted
        self.assertIn('"not_granted": 1', "\n".join(logged.output))
        self.assertNotIn("Zephyr", answer.model_dump_json())                 # never shown
        self.assertNotIn(other, answer.model_dump_json())


    def test_a_sentence_repeating_a_withheld_claims_words_goes(self):
        # Review probe: the re-screen kept a hidden claim's words verbatim.
        from operator_ui.object_workspace import ITEM_REFUSED, _withheld
        sp = "sharepoint://b%21DRIVE/01MAP@%22%7BE%7D%2C1%22"

        class Access:
            def decide(self, handles):
                return {h: _withheld(ITEM_REFUSED) for h in handles}

        view = SimpleNamespace(access=Access(), _can_open=lambda h: True,
                               _call=lambda name, args: {}, _base=lambda: {})
        shown = {"id": "m1", "check": {"status": "passed"}, "citations": [envelope(sp)],
                 "text": envelope("The quote was accepted."), "phrase": envelope("accepted"),
                 "value_claims": [{"citation": sp, "field": "text",
                                   "value": envelope("accepted")}]}
        hidden = {"id": "m2", "check": {"status": "passed"}, "citations": [],
                  "text": envelope("Acme agreed to cover the freight."),
                  "phrase": envelope("Acme agreed to cover the freight"),
                  "value_claims": [{"field": "text", "value": envelope("2026-11-14"),
                                    "quote": "ships on 2026-11-14"}]}
        result = {"outcome": "answered", "claims": [shown, hidden],
                  "unsupported_claims": [], "reply": {"text": envelope("x")}}
        events = [{"type": "tool_call", "name": runner.ANSWER_GATE_TOOL,
                   "arguments": {"answer": {}}, "result": result, "is_error": False}]
        prose = ("The quote was accepted. Acme agreed to cover the freight.\n\n"
                 "The order ships on November 14.")
        ran = aa.AskRun(final_text=prose, answer={"answerability": "answerable",
                                                  "text": "", "values": [], "gaps": []},
                        events=events, decision=None, stopped=False, seconds=1.0)
        backend = be.WorkspaceBackend(lambda s: None, data_label="test")
        with self.assertLogs("operator_api.ask", "INFO"):
            answer = be.agent_answer(backend, view, m.AskRequest(question="q"), ran, {},
                                     "c", lambda: 1)
        self.assertIn("The quote was accepted.", answer.direct_answer)   # the rest stays
        self.assertNotIn("Acme", answer.direct_answer)
        self.assertNotIn("November 14", answer.direct_answer)
        self.assertEqual(answer.hidden.count, 1)


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


class ProgressWords(unittest.TestCase):
    """``ask_progress``: an unknown, failed or empty read names no source; a verdict the
    reply screen would change is not sent."""

    def test_a_read_that_names_nothing_known_gives_no_source_step(self):
        from operator_api import ask_progress as p
        receipt = {"object_id": "Receipt:bc.purchase_receipts:r-1"}
        self.assertEqual(p.result_step("plx.object", receipt, json.dumps({"ok": 1})),
                         "Reading Business Central receipts")
        self.assertIsNone(p.result_step("plx.object", receipt, "{}", is_error=True))
        self.assertIsNone(p.result_step("plx.object", receipt, json.dumps({"error": "x"})))
        self.assertIsNone(p.result_step("plx.object", receipt, "not json"))
        self.assertIsNone(p.result_step("plx.object", {"object_id": "Secret:hr.pay:1"}, "{}"))
        self.assertIsNone(p.result_step("plx.documents", {}, json.dumps({"documents": []})))
        self.assertIsNone(p.result_step("plx.unknown", receipt, "{}"))
        self.assertEqual(p.call_step("plx.unknown"), "Reading records")
        self.assertEqual(p.result_step("plx.around", {"from": "PurchaseOrder:bc.po:1"},
                                       json.dumps({"objects": [
                                           {"object_id": "Receipt:portal.app_receipt:2"},
                                           {"object_id": "PurchaseOrder:bc.po:1"}]})),
                         "Reading Portal receipts")
        self.assertEqual(p.finding_steps("why is PO 90412 late"), ["Finding PO 90412"])
        self.assertEqual(p.finding_steps("is the soap order late"), [])

    def test_a_verdict_only_from_an_answered_decision_the_screen_keeps(self):
        from operator_api import ask_progress as p
        handle = "api://public.app_purchase_order/x@1-1"
        kept = decision(handle, "Overdue")
        self.assertEqual(p.verdict(kept, "status?"), "The order status is Overdue.")
        self.assertIsNone(p.verdict(decision(handle, "Overdue", outcome="repair"), "q"))
        self.assertIsNone(p.verdict(decision(handle, "Overdue", text="It shipped 999 "
                                             "totes."), "q"))
        self.assertIsNone(p.verdict({**kept, "reply": {"text": ""}}, "q"))
        self.assertEqual(p.citations(kept), [handle])
        uncited = {**kept, "claims": [{**kept["claims"][0], "citations": []}]}
        self.assertIsNone(p.citations(uncited))


class ReplyProseTests(unittest.TestCase):
    def test_the_research_stopped_line_leads_the_answer_never_replaces_it(self):
        """A reply rendered from the gate's decision is only its answer block; a session
        cut short puts the research-stopped line before it. The reader sees the line and
        then the answer's text (11-06: a not-found answer showed the line alone)."""
        from operating.deep_investigation import STOPPED_LINE
        said = "No purchase order for that product from that supplier was found."
        block = "```json\n" + json.dumps({"format": "plx-eval-answer/v1", "text": said}) \
            + "\n```"
        self.assertEqual(aa.reply_prose(STOPPED_LINE + "\n\n" + block, {"text": said}),
                         STOPPED_LINE + "\n\n" + said)
        # the agent's own prose around the block stays the reply
        own = STOPPED_LINE + "\n\nThe order shipped on Monday.\n\n" + block
        self.assertEqual(aa.reply_prose(own, {"text": said}),
                         STOPPED_LINE + "\n\nThe order shipped on Monday.")
        self.assertEqual(aa.reply_prose(block, {"text": said}), said)
        # the answer's text saying it stopped itself is said once
        own_stop = "I stopped researching at the time limit; the lots were not checked."
        self.assertEqual(aa.reply_prose(STOPPED_LINE + "\n\n" + block, {"text": own_stop}),
                         own_stop)
