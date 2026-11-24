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
from operator_api.workspace import case_console
from operator_api.workspace import object_workspace as objects
from operator_api.tests import case_fixtures

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


    def ask(self, served, question, attachments=()):
        started = time.monotonic()
        seen = []
        body = {"question": question, **({"attachments": list(attachments)}
                                         if attachments else {})}
        with served.client.stream("POST", "/api/ask", json=body) as reply:
            self.assertEqual(reply.status_code, 200)
            buffer = ""
            for chunk in reply.iter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    seen.append((events(block)[0], time.monotonic() - started))
        return seen





    def setUp(self):
        # the work log keeps Asks only while the people exclusion decides (fail closed)
        from operator_api.tests.test_work_log import Guard
        self.guard = Guard()
        for served in (self.ops, self.outsider):
            store = served.app.state.work_log
            self.addCleanup(setattr, store, "guard", store.guard)
            store.guard = lambda: self.guard










    def test_progress_names_plain_steps_never_an_identifier_or_a_tool(self):
        """The one Ask path streams plain-words steps as the session reads (no tool name,
        id, handle or argument). (The gate-checked verdict and the refused-source reader of
        the old agent path are gone with it: the permission door now sits in the tools.)"""
        status = "Overdue"
        po = "PurchaseOrder:bc.purchase_orders:x-1"
        lines = [tool_use(1, "plx.object", {"object_id": "Receipt:bc.purchase_receipts:r-9"}),
                 tool_result(1, {"object_id": "Receipt:bc.purchase_receipts:r-9"}),
                 tool_use(2, "plx.documents", {"object_id": po,
                                               "query": "order confirmation"}),
                 tool_result(2, {"documents": [{"kind": "email", "citation": "x"}]}),
                 tool_use(3, "plx.sql", {"sql": "select 1"}),
                 tool_result(3, {"row_count": 1, "views": []})]
        prose = f"The order status is {status}.\n\n" + answer_block(self.handle, status)
        self.use(self.ops, lambda on_line: ScriptedSession(on_line, lines, prose, pause=0.4))
        got = self.ask(self.ops, "What did the supplier confirm for PO 90412?")
        for (name, data), _ in got:
            m.ASK_EVENTS[name].model_validate(data)
        steps = [data["message"] for (name, data), _ in got if name == "status"]
        self.assertIn("Working out which records answer it", steps)
        for step in steps:   # never a tool name, an id, a handle or an argument
            for internal in ("plx.", "plx_", "://", ":bc.", "x-1", "r-9", "order confirmation"):
                self.assertNotIn(internal, step)






    def test_only_the_unqualified_list_question_reads_as_the_late_list(self):
        # plx.find's home mode (``mcp_portfolio.late_list_words``): the one late-list
        # grammar left; the Ask's ready late list is routed by the front door's reading
        from operating.mcp_portfolio import late_list_words
        for asked in ("Which purchase orders are late?", "which POs are late",
                      "What's late?", "late purchase orders", "show me overdue POs",
                      "Are there any POs past due?", "Which POs are late right now?",
                      # chat spellings and typos (demo rehearsal 2026-10-31)
                      "wat r the late pos", "any late pos rn?", "late purchse ordres",
                      "whats overdue atm", "any late p.o.s?"):
            self.assertTrue(late_list_words(asked), asked)
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
            self.assertFalse(late_list_words(asked), asked)








    def test_original_reads_are_capped_and_a_failing_host_is_a_logged_500(self):
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
        # a host that failed is the server's failure, not a missing original
        # (followups-1115: only a refusal or RecordNotFound is a 404); nothing of it shown
        with self.assertLogs("operator_api", "ERROR"):
            got = self.ops.client.get("/api/source",
                                      params={"handle": "original://nr-0123456789ab#page=1"})
        self.assertEqual((got.status_code, got.json()["error"]["code"]), (500, "internal"))
        self.assertNotIn("host_session_ended", got.text)

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







class MailAttachmentSource(unittest.TestCase):
    """``read_source`` takes a byte-pinned mail attachment handle (the plx.read_document path
    for a mail attachment goes through ``Backend._host_opens`` -> ``read_source``), and only
    as the reader's own host reads it: a reader the capture's gate does not show the parent
    message gets nothing. Nothing here replaces ``read_source`` or ``_host_opens``."""
    PIN = "a" * 64
    HANDLE = "graph-mail://mbx/msg@ck#attachment=AAA&sha256=" + PIN

    class Host:
        def __init__(self, sees_parent):
            self.sees_parent, self.asked = sees_parent, []

        def call(self, tool, arguments):
            self.asked.append((tool, arguments))
            if not self.sees_parent:   # the capture's gate: the parent message is not theirs
                return {"error": {"code": "citation_unresolvable"}}
            return {"status": "resolved", "citation": arguments["citation"]}

    def ask_for(self, sees_parent):
        host = self.Host(sees_parent)
        return aa.AgentAsk(seconds=5, host_for=lambda session: host,
                           agent_for=lambda on_line: self.fail("no session")), host

    def test_a_reader_who_sees_the_parent_message_reads_it_and_one_who_does_not_is_refused(self):
        ask, host = self.ask_for(True)
        self.assertEqual(ask.read_source(object(), self.HANDLE)["status"], "resolved")
        self.assertEqual(host.asked, [("plx.read", {"citation": self.HANDLE})])
        ask, host = self.ask_for(False)
        from operator_api.errors import RecordNotFound
        with self.assertRaises(RecordNotFound):
            ask.read_source(object(), self.HANDLE)

    def test_only_a_byte_pinned_attachment_handle_is_taken_from_mail(self):
        ask, host = self.ask_for(True)
        for bad in ("graph-mail://mbx/msg@ck",                       # a message
                    "graph-mail://mbx/msg@ck#attachment=AAA",         # no pin
                    "graph-mail://mbx/msg@ck#attachment=AAA&sha256=" + "A" * 64,   # not hex
                    "graph-mail://mbx/msg@ck#attachment=AAA&sha256=" + "a" * 63,
                    "graph-mail://mbx/msg@ck#attachment=AAA&sha256=" + self.PIN + "&x=1"):
            with self.subTest(handle=bad), self.assertRaises(KeyError):
                ask.read_source(object(), bad)
        self.assertEqual(host.asked, [])


class ItemRecheck(unittest.TestCase):
    def test_only_a_captured_original_the_app_cannot_recheck_stays_with_the_host_link(self):
        from operator_api.workspace.object_workspace import ITEM_REFUSED, _withheld
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
        from operator_api.workspace.object_workspace import ITEM_REFUSED, NOT_GRANTED, _withheld
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
        from operator_api.workspace.object_workspace import ITEM_REFUSED, _withheld
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
        # why it cannot run, as a code (the server logs it and marks its answers)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "host.json"
            config.write_text("{}", encoding="utf-8")
            secret = {"PLX_WORKBENCH_SESSION_SECRET": "s" * 40}
            self.assertEqual(aa.AgentAsk.missing(None, secret), "no_host_config")
            self.assertEqual(aa.AgentAsk.missing(config, {}), "no_session_secret")
            self.assertEqual(aa.AgentAsk.missing(config, {**secret, "PATH": tmp}),
                             "no_claude_cli")
            self.assertIsNone(aa.AgentAsk.from_args(Path(tmp), config,
                                                    {**secret, "PATH": tmp}))
            cli = Path(tmp) / "claude"
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            cli.chmod(0o755)
            self.assertIsNone(aa.AgentAsk.missing(config, {**secret, "PATH": tmp}))

    def test_the_agent_and_prompt_are_the_scoreboards(self):
        from operating import agent_session
        from scripts.eval_case_cards import CARD_AGENT
        self.assertEqual(aa.AGENT, CARD_AGENT)
        question = "Which lot did we receive?"
        self.assertEqual(agent_session.ask_prompt(question),
                         runner.build_prompt(runner.UNPINNED_ITEM, question))
        self.assertNotIn(aa.APP_READER_RULE, runner.build_prompt(runner.UNPINNED_ITEM,
                                                                   question))
        from operating.prose_style import PROSE_STYLE_RULE
        self.assertNotIn(PROSE_STYLE_RULE, runner.build_prompt(runner.UNPINNED_ITEM, question))
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
