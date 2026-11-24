"""Autopilot jobs (``operating.autopilot_jobs``): the system work an event calls for, proposed
by the model with the Ask's write tool contract, kept on the person's item, and prepared only
when the person reviews it (``/api/autopilot/items/{id}/jobs/{n}/prepare`` through the
prepared-writes desk, confirmed in two steps on ``/api/prepared``).

No test calls a model or a system: the triage call and the tool session are stand-ins, the
write transport a fake. SYNTHETIC people and records. Evidence label: injected-transport.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from fastapi.testclient import TestClient

from integrations.people_exclusion import PeopleExclusion
from operating import autopilot as ap
from operating import autopilot_jobs as aj
from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating import proactive_engine as pe
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests import test_autopilot as ta
from operating.tests.test_autopilot_write import Transport
from operating.tests.test_payroll_marking import PAYSTUB
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend

HOME = "/var/lib/plx-autopilot/claude-home"
BIN = "/usr/local/bin/claude"
PLAN = "plan-0001"
PO_DATE = {"system": "portal", "operation": "purchase-order.update", "target": {"id": "cmpo0001"},
           "change": {"expectedDate": "2026-11-20"},
           "reason": "The supplier confirmed delivery on 20 November."}
TASK = {"system": "planner", "operation": "task_create", "target": {},
        "change": {"planId": PLAN, "title": "Send the COA to the customer"},
        "reason": "Agreed in the meeting."}


class Triage:
    """The ``claude -p`` stand-in of the first call: answers ``work``, keeps the prompts."""

    def __init__(self, work: bool) -> None:
        self.work, self.prompts = work, []

    def __call__(self, argv, *, input, **_kw):
        self.prompts.append(input)
        out = {"result": json.dumps({"work": self.work, "what": ""})}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")


class Session:
    """The tool session stand-in: calls the given write tools with ``calls`` (tool name,
    arguments), returns ``looked`` as what the read tools returned."""

    def __init__(self, calls=(), looked=(), final='{"summary": "Move the PO date"}') -> None:
        self.calls, self.looked, self.final = list(calls), list(looked), final
        self.runs: list[tuple[str, str]] = []
        self.answers: list = []

    def run(self, owner, prompt, *, tools=(), job=False):
        assert job
        self.runs.append((owner, prompt))
        by_name = {t.spec["name"]: t for t in tools}
        for name, args in self.calls:
            self.answers.append(by_name[name].call(args))
        return self.final, self.looked


class People:
    def __init__(self, barred=()) -> None:
        self.barred = set(barred)

    def identity_clear(self, who: str) -> bool:
        return who not in self.barred


MAIL = {"event_type": "message.received", "object_id": "Email:1", "sender": "sup@vendor.example",
        "cleared": True, "channel": "mail", "subject": "PO 77 confirmation",
        "text": "Confirmed, we deliver PO 77 on Nov 20."}
GATE = {"event_type": "approval.requested", "object_id": "MCApproval:1", "status": "pending",
        "fields": {"requested_by": "dev@plx.example", "title": "Merge PR"}, "text": ""}


def jobber(session, triage=True):
    return aj.Jobber("opus", triage="haiku", lookup=session, binary=BIN, home=HOME,
                     per_cycle=100, call=Triage(triage))


class ProposalsAreShapedLikeTheWritePath(unittest.TestCase):
    def test_a_proposal_passes_only_the_write_paths_own_shape_checks(self):
        self.assertEqual(aj.check("plx.prepare_write", PO_DATE), [])
        self.assertEqual(aj.check("plx.prepare_write", TASK), [])
        # the record's etag and before-values are read by the desk at review: not asked here
        self.assertEqual(aj.check("plx.prepare_write", {
            "system": "bc", "operation": "purchase_order_line_update",
            "target": {"company_name": "PLX", "record_id": "00000000-0000-4000-8000-000000000002"},
            "change": {"expectedReceiptDate": "2026-11-20"}, "reason": "Supplier moved it."}), [])
        # an approval: the gate's scope is read from Mission Control at review, never proposed
        approve = {"system": "mc", "operation": "approval_decide",
                   "target": {"task_id": "TASK-1", "gate_id": "apg_1"},
                   "change": {"decision": "approve"}, "reason": "Checks pass."}
        self.assertEqual(aj.check("plx.prepare_write", approve), [])
        self.assertTrue(aj.check("plx.prepare_write", {**approve, "target": {"task_id": "TASK-1"}}))
        refused = {
            "a field the route does not take": {**PO_DATE, "change": {"expected_date": "2026-11-20"}},
            "a reply": {**PO_DATE, "system": "mail", "operation": "send_email"},
            "a Teams message": {**PO_DATE, "system": "teams"},
            "a delete": {"system": "bc", "operation": "sales_order_delete",
                         "target": {"company_name": "PLX", "document_id": "x"}, "change": {},
                         "reason": "r"},
            "FileMaker": {**PO_DATE, "system": "filemaker"},
            "Sage": {**PO_DATE, "system": "sage"},
            "an unknown system": {**PO_DATE, "system": "erp9"},
            "an unknown field": {**PO_DATE, "extra": 1},
            "no reason": {**PO_DATE, "reason": ""},
            "not an object": ["portal"],
        }
        for why, args in refused.items():
            with self.subTest(why):
                self.assertTrue(aj.check("plx.prepare_write", args))
        self.assertTrue(aj.check(aj.DOC_TOOL_NAME, {"citation": "graph-mail://x", "find": "a",
                                                    "replace": "b", "reason": "r"}))
        self.assertEqual(aj.check(aj.DOC_TOOL_NAME, {"citation": "sharepoint://d/i@e",
                                                     "find": "a", "replace": "b",
                                                     "reason": "r"}), [])

    def test_the_tools_keep_only_checked_proposals_and_at_most_a_few(self):
        kept: list = []
        tools = {t.spec["name"]: t for t in aj.proposal_tools(kept)}
        self.assertEqual(set(tools), {"plx.prepare_write", aj.DOC_TOOL_NAME})
        refused = tools["plx.prepare_write"].call({**PO_DATE, "system": "mail"})
        self.assertIn("refused", refused)
        for _ in range(aj.MOST + 2):
            tools["plx.prepare_write"].call(PO_DATE)
        self.assertEqual(len(kept), aj.MOST)
        self.assertIn("refused", tools["plx.prepare_write"].call(TASK))   # past the cap
        # a stored item is checked again when read back (a tampered row never shows)
        self.assertEqual(aj.kept([{"tool": "plx.prepare_write",
                                   "arguments": {**PO_DATE, "system": "mail"}},
                                  {"tool": "plx.shell", "arguments": PO_DATE},
                                  {"tool": "plx.prepare_write", "arguments": PO_DATE}]),
                         [{"tool": "plx.prepare_write", "arguments": PO_DATE}])

    def test_the_catalogue_is_read_from_the_adapters_and_offers_no_reply_or_delete(self):
        lines = aj.catalogue()
        self.assertTrue(any(line.startswith("portal purchase-order.update") and "expectedDate"
                            in line for line in lines), lines)
        self.assertTrue(any(line.startswith("planner task_create") for line in lines))
        self.assertTrue(any(line.startswith("calendar event_respond") and "accept" in line
                            for line in lines), lines)
        # messages only as new messages to someone other than the sender; never the
        # write path's own "graph" kind
        messages = [line for line in lines if line.split(" ", 1)[0] in ("mail", "teams")]
        self.assertEqual(len(messages), 2, messages)
        self.assertTrue(all("someone other than the event's sender" in m for m in messages))
        self.assertFalse([line for line in lines if line.split(" ", 1)[0] == "graph"])
        self.assertFalse([line for line in lines if "_delete" in line or "file_upload" in line])

    MESSAGE = {"system": "mail", "operation": "send_email", "target": {},
               "change": {"to": ["luis@plx.example"], "cc": ["stephen@plx.example"],
                          "subject": "Plant design", "body": "Hi Luis, could you share the "
                          "plant layout PDF with Stephen?"},
               "reason": "Stephen asked for the plant design."}
    RESPOND = {"system": "calendar", "operation": "event_respond",
               "target": {"event_id": "AAMkAGU3MWUx="},
               "change": {"response": "accept", "comment": ""}, "reason": "Free then."}

    def test_a_message_to_someone_else_and_an_invitations_answer_are_proposals(self):
        """Owner: Autopilot does the person's whole job: asking a colleague or a supplier,
        passing a message on, answering an invitation are proposals like a system change
        (the write path's own shapes); a message with no recipient or body, an answer that
        is not accept/decline/tentative or that sets anything else is refused."""
        self.assertEqual(aj.check("plx.prepare_write", self.MESSAGE), [])
        teams = {"system": "teams", "operation": "chat_message", "target": {},
                 "change": {"members": ["luis@plx.example"], "body": "Plant layout PDF?"},
                 "reason": "r"}
        self.assertEqual(aj.check("plx.prepare_write", teams), [])
        self.assertEqual(aj.check("plx.prepare_write", self.RESPOND), [])
        for response in ("decline", "tentative", "Accept"):
            self.assertEqual(aj.check("plx.prepare_write", {
                **self.RESPOND, "change": {"response": response}}), [], response)
        refused = {
            "no recipient": {**self.MESSAGE, "change": {"subject": "s", "body": "b"}},
            "no body": {**self.MESSAGE, "change": {"to": ["luis@plx.example"], "body": " "}},
            "a field the card does not show": {**self.MESSAGE, "change": {
                **self.MESSAGE["change"], "bcc": ["x@plx.example"]}},
            "Teams with no one": {**teams, "change": {"body": "hi"}},
            "no reason": {**self.MESSAGE, "reason": ""},
            "maybe": {**self.RESPOND, "change": {"response": "maybe"}},
            "an answer that edits": {**self.RESPOND, "change": {"response": "accept",
                                                               "subject": "Mine now"}},
            "no event": {**self.RESPOND, "target": {}},
        }
        for why, args in refused.items():
            with self.subTest(why):
                self.assertTrue(aj.check("plx.prepare_write", args))

    def test_an_invitations_answer_is_one_of_graphs_three_actions_and_has_no_undo(self):
        from integrations import live_source_guard as guard
        adapter = wa.default_adapters({})["calendar_write"]
        for response, verb in (("accept", "accept"), ("decline", "decline"),
                               ("tentative", "tentativelyAccept")):
            action = {"kind": "calendar_write", "system": "calendar",
                      "operation": "event_respond",
                      "target": {"user": "me@plx.example", "event_id": "AAMkAGU3MWUx="},
                      "payload": {"response": response, "comment": "See you then"}}
            request = adapter.request(action)
            self.assertEqual(request["method"], "POST")
            self.assertTrue(request["url"].endswith(f"/events/AAMkAGU3MWUx=/{verb}"),
                            request["url"])
            self.assertEqual(request["body"], {"comment": "See you then", "sendResponse": True})
            self.assertIsNone(adapter.undo_request(action, request))
            self.assertEqual(guard.write_shape("POST", request["url"]), "calendar")
        base = "https://graph.microsoft.com/v1.0/users/me@plx.example/events/AAMkAGU3MWUx="
        for verb in ("forward", "cancel", "accept/x", "snoozeReminder"):
            self.assertIsNone(guard.write_shape("POST", f"{base}/{verb}"), verb)


class JobberBoundary(unittest.TestCase):
    def test_a_message_to_the_sender_is_the_drafts_reply_never_a_job_proposal(self):
        """A message addressed to the event's sender, or posted into the event's own chat,
        is a reply: dropped from the job (the drafts prepare it); a message to someone else,
        even copying the sender, is kept."""
        to_sender = {"system": "mail", "operation": "send_email", "target": {},
                     "change": {"to": ["Sup@vendor.example"], "body": "Noted, thanks."},
                     "reason": "r"}
        onward = {"system": "mail", "operation": "send_email", "target": {},
                  "change": {"to": ["buyer@plx.example"], "cc": ["sup@vendor.example"],
                             "subject": "PO 77", "body": "PO 77 lands Nov 20: please "
                             "plan the receipt."}, "reason": "r"}
        same_chat = {"system": "teams", "operation": "chat_message",
                     "target": {"chat_id": "19:abc"}, "change": {"body": "On it"},
                     "reason": "r"}
        session = Session(calls=[("plx.prepare_write", to_sender),
                                 ("plx.prepare_write", onward),
                                 ("plx.prepare_write", same_chat)])
        event = {**MAIL, "conversation_id": "19:abc"}
        got, why = jobber(session).run(event, "me", aj.described(event, MAIL["text"]),
                                       "me@plx.example", People())
        self.assertEqual(why, "")
        self.assertEqual([p["arguments"] for p in got.proposals], [onward])
        only = Session(calls=[("plx.prepare_write", to_sender)])
        self.assertEqual(jobber(only).run(MAIL, "me", aj.described(MAIL, MAIL["text"]),
                                          "me@plx.example", People()),
                         (None, "no_system_work"))

    def test_a_message_the_small_model_finds_no_work_in_starts_no_session(self):
        session = Session(calls=[("plx.prepare_write", PO_DATE)])
        got, why = jobber(session, triage=False).run(MAIL, "me", aj.described(MAIL, ""),
                                                     "me@plx.example", People())
        self.assertEqual((got, why), (None, "no_system_work"))
        self.assertEqual(session.runs, [])

    def test_the_proposals_become_the_job_and_nothing_proposed_is_no_job(self):
        session = Session(calls=[("plx.prepare_write", PO_DATE),
                                 ("plx.prepare_write", {**PO_DATE, "system": "mail"})])
        got, why = jobber(session).run(MAIL, "me", aj.described(MAIL, MAIL["text"]),
                                       "me@plx.example", People())
        self.assertEqual(why, "")
        self.assertEqual(got.proposals, [{"tool": "plx.prepare_write", "arguments": PO_DATE}])
        self.assertIn("Confirmed, we deliver PO 77", session.runs[0][1])
        self.assertIn("purchase-order.update", session.runs[0][1])   # the catalogue
        got, why = jobber(Session()).run(MAIL, "me", aj.described(MAIL, ""), "me@plx.example",
                                         People())
        self.assertEqual((got, why), (None, "no_system_work"))

    def test_an_approval_goes_to_the_session_without_the_first_call(self):
        session = Session(calls=[("plx.prepare_write", TASK)])
        j = jobber(session, triage=False)
        got, why = j.run(GATE, "me", aj.described(GATE, ""), "me@plx.example", People())
        self.assertEqual(why, "")
        self.assertEqual(j.call.prompts, [])
        self.assertEqual(len(session.runs), 1)

    def test_a_brief_is_kept_only_for_an_event_that_asks_for_one(self):
        """A meeting the person runs gets a brief before it (``recurring_work``): the work
        is to read, not to change a system. Only an event whose fields ask for a brief
        keeps one; a message's brief is never kept (no item without a change); a brief
        naming an excluded person is no job."""
        prep = {"event_type": "record.due", "object_id": "Meeting:1", "status": "recurring",
                "fields": {"title": "Brief for Progress Huddle at 09:00", "brief": True}}
        final = '{"summary": "Huddle prep", "brief": "Open: PO 77 date (mail 11-18)."}'
        got, why = jobber(Session(final=final), triage=False).run(
            prep, "me", aj.described(prep, ""), "me@plx.example", People())
        self.assertEqual((why, got.proposals, got.brief),
                         ("", [], "Open: PO 77 date (mail 11-18)."))
        for event in ({**prep, "fields": {"title": "x"}}, MAIL):
            with self.subTest(event["event_type"]):
                got, why = jobber(Session(final=final), triage=True).run(
                    event, "me", aj.described(event, "text"), "me@plx.example", People())
                self.assertEqual((got, why), (None, "no_system_work"))
        barred = '{"summary": "x", "brief": "Ask barred@plx.example about it."}'
        got, why = jobber(Session(final=barred), triage=False).run(
            prep, "me", aj.described(prep, ""), "me@plx.example",
            People(barred={"barred@plx.example"}))
        self.assertIsNone(got)
        self.assertNotEqual(why, "")

    def test_a_due_teams_post_becomes_a_teams_draft_into_its_chat(self):
        """A scheduled Teams post (``recurring_work.POST_KINDS``): the brief the model wrote
        is offered as a Teams draft into the chat it is posted in (sent only on the
        person's confirm); a brief without a chat stays a brief; a change proposed is a
        job as before."""
        class Fake:
            busy = False

            def __init__(self, job):
                self.job = job

            def pending(self):
                return []

            def run(self, *a, **k):
                return self.job, ""
        post = {"event_type": "record.due", "object_id": "TeamsChatMessage:1",
                "fields": {"brief": True, "post_to": "19:c@thread.v2"}}

        def made(event, job):
            summary: dict = {}
            held = {"job_cycle": summary, "jobber": Fake(job)}
            return ap._model_job(None, held, summary, event, "me", {"kind": "job"}, None, {})
        out, why = made(post, aj.Job("Weekly post", [], "Shipped 4 lots this week."))
        self.assertEqual((why, out["kind"], out["teams_draft"]["conversation"],
                          out["teams_draft"]["body"]),
                         (None, "reply", "19:c@thread.v2", "Shipped 4 lots this week."))
        self.assertNotIn("jobs", out)
        out, _ = made({**post, "fields": {"brief": True}}, aj.Job("Prep", [], "Open: PO 7."))
        self.assertEqual((out["kind"], out["brief"]), ("job", "Open: PO 7."))
        out, _ = made(post, aj.Job("x", [{"tool": "plx.prepare_write", "arguments": TASK}],
                                   "text"))
        self.assertEqual(out["kind"], "job")

    def test_excluded_people_and_pay_never_reach_the_model(self):
        session = Session(calls=[("plx.prepare_write", TASK)])
        j = jobber(session)
        cases = {
            "not_cleared": (GATE, People(barred={"dev@plx.example"})),
            "not_cleared ": ({**MAIL, "cleared": False}, People()),
            "pay_or_hr": ({**GATE, "text": PAYSTUB}, People()),
        }
        for why, (event, people) in cases.items():
            with self.subTest(why):
                got, said = j.run(event, "me", aj.described(event, event.get("text", "")),
                                  "me@plx.example", people)
                self.assertEqual((got, said), (None, why.strip()))
        self.assertEqual(session.runs, [])
        # Freightcom is read like any other carrier (owner 11-23): no screen on it
        session = Session(calls=[("plx.prepare_write", PO_DATE)],
                          looked=["carrier: Freightcom, pickup 20 Nov"])
        got, why = jobber(session).run(MAIL, "me", aj.described(MAIL, ""), "me@plx.example",
                                       People())
        self.assertEqual(why, "")

    def test_tool_answers_quoting_a_colleagues_address_pass_and_an_excluded_one_never(self):
        """Fresh grade 11-18: 3 of 5 approvals the model prepared were withheld because the
        tool answers (Python text) quote addresses: 'cos@...' read as an unknown address."""
        quoted = ["{'requested_by': 'cos@plx.example', 'decided_by': None}"]
        session = Session(calls=[("plx.prepare_write", TASK)], looked=quoted)
        got, why = jobber(session).run(GATE, "me", aj.described(GATE, ""), "me@plx.example",
                                       People())
        self.assertEqual(why, "")
        barred = People(barred={"cos@plx.example"})
        session = Session(calls=[("plx.prepare_write", TASK)], looked=quoted)
        got, why = jobber(session).run(GATE, "me", aj.described(GATE, ""), "me@plx.example",
                                       barred)
        self.assertEqual((got, why), (None, "excluded_in_thread"))

    def test_the_pool_answers_pending_then_the_job(self):
        session = Session(calls=[("plx.prepare_write", PO_DATE)])
        j = jobber(session)
        self.addCleanup(j.close)
        got, why = j.run(MAIL, "me", aj.described(MAIL, ""), "me@plx.example", People(), key="k")
        self.assertEqual((got, why), (None, "job_pending"))
        j.wait(10)
        got, why = j.run(MAIL, "me", aj.described(MAIL, ""), "me@plx.example", People(), key="k")
        self.assertEqual(why, "")
        self.assertEqual(len(got.proposals), 1)

    def test_no_model_named_is_no_jobber(self):
        self.assertIsNone(aj.from_environ({}))
        self.assertIsNone(aj.from_environ({aj.MODEL_ENV: "off",
                                           "PLX_AUTOPILOT_DRAFT_MODEL": "opus"}))
        self.assertEqual(aj.from_environ({"PLX_AUTOPILOT_DRAFT_MODEL": "opus"}).model, "opus")


class LiveCycle(ta.Fixture):
    """In the live queue a job item holds the proposed changes or is not made at all; a
    replay never starts a job session."""

    def live(self, ids, j, name="s.sqlite", queue="autopilot"):
        path = self.dir / name
        if not path.exists():
            self.w.build(path)
        st = pe._Store(path, threads=1)
        q = ap.Queue(self.dir / queue / ap.QUEUE_FILE)
        q.__dict__.setdefault("_draft_context", {})["jobber"] = j
        try:
            out = ap.run_cycle(st, q, ids, people=ta.people(), registry=None,
                               environ=self.env, now=ta.NOW,
                               landed_at=ap._iso(ta.NOW - timedelta(minutes=2)))
            items = [dict(r) for r in q.conn.execute(
                "SELECT * FROM items WHERE autopilot = 'job_message'")]
            return out, items
        finally:
            st.close()
            q.close()

    def test_the_item_carries_the_proposed_changes_and_waits_for_the_person(self):
        msg = self.message(80, "Confirmed, we deliver PO 77 on Nov 20.")
        session = Session(calls=[("plx.prepare_write", PO_DATE)])
        out, items = self.live([msg], jobber(session))
        self.assertEqual(out["prepared"].get("job_message"), 1, out)
        body = json.loads(items[0]["body"])
        self.assertEqual(body["prepared"]["kind"], "job")
        self.assertEqual(body["prepared"]["jobs"],
                         [{"tool": "plx.prepare_write", "arguments": PO_DATE}])
        self.assertEqual(body["done_when"], "person_decided")
        self.assertNotIn("material", body["prepared"])
        # the item is the person's to review: nothing ran (no executor action of it)
        self.assertEqual(aw.actions_of({"body": body}), [])

    def test_no_change_proposed_or_no_model_makes_no_job_item(self):
        msg = self.message(81, "Thanks for the update!")
        out, items = self.live([msg], jobber(Session(), triage=False))
        self.assertEqual(items, [])
        self.assertEqual(out["skipped"].get("job_message:no_system_work"), 1)
        out, items = self.live([self.message(82, "Thanks!")], None, "t.sqlite")
        self.assertEqual(items, [])
        self.assertEqual(out["skipped"].get("job_message:job_model_off"), 1)

    def test_a_replay_queue_never_starts_a_job(self):
        msg = self.message(83, "Confirmed, we deliver PO 77 on Nov 20.")
        session = Session(calls=[("plx.prepare_write", PO_DATE)])
        out, items = self.live([msg], jobber(session), queue="replay")
        self.assertEqual((items, session.runs), ([], []))
        self.assertFalse([k for k in out["skipped"] if k.startswith("job_")])

    def test_a_proposed_message_carrying_another_partys_business_outside_is_held(self):
        """A job reads as the person; what it proposes to send outside the company is
        checked (``autopilot_outbound``): another customer named, or a price the readers'
        own material does not state, is held back and named on the item; a system change
        and a clean message stand; nothing left: no item."""
        self.w.obj("Customer", {"name": "Northwind Soapworks Inc", "code": "C200",
                                "erp_code": "C200", "email": "ap@northwind.example"})
        def message(body):
            return {"system": "mail", "operation": "send_email", "target": {},
                    "change": {"to": ["sales@vendor.example"], "subject": "Stock", "body": body},
                    "reason": "Ask the supplier."}
        name = message("Northwind Soapworks takes the next lot first.")
        price = message("We pay $4.85 per kg now.")
        clean = message("Can you confirm stock for next week?")
        msg = self.message(84, "Confirmed, we deliver PO 77 on Nov 20.")
        session = Session(calls=[("plx.prepare_write", PO_DATE), ("plx.prepare_write", name),
                                 ("plx.prepare_write", price), ("plx.prepare_write", clean)])
        out, items = self.live([msg], jobber(session))
        body = json.loads(items[0]["body"])["prepared"]
        self.assertEqual([p["arguments"] for p in body["jobs"]], [PO_DATE, clean])
        self.assertEqual([b["why"] for b in body["held_back"]],
                         ["another_partys_name", "price_not_on_their_records"])
        self.assertIn("held back", body["summary"])
        out, items = self.live([self.message(85, "Confirmed, we deliver PO 77 on Nov 20.")],
                               jobber(Session(calls=[("plx.prepare_write", name)])), "h.sqlite")
        self.assertEqual(len(items), 1)          # the first message's item only
        self.assertEqual(out["skipped"].get("job_message:outbound_held"), 1, out)

    def test_cardinality_jobs_never_spend_the_drafts_budget_and_none_is_lost(self):
        """N=1/10/60 messages each with a change: one job item per message up to the
        person's hourly cap of job items; the rest wait (deferred), none doubled; the
        reply drafts' own hourly budget is untouched by the jobs."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                msgs = [self.message(900 + i, f"Confirmed, we deliver PO 77 on Nov 20. {i}")
                        for i in range(n)]
                session = Session(calls=[("plx.prepare_write", PO_DATE)])
                out, items = self.live(msgs, jobber(session), f"n{n}.sqlite")
                made = min(n, 30)
                self.assertEqual(len(items), made, out["skipped"])
                self.assertEqual(len({i["event_id"] for i in items}), made)
                self.assertEqual(out["skipped"].get("job_message:rate_limited", 0), n - made)
                q = ap.Queue(self.dir / "autopilot" / ap.QUEUE_FILE)
                spent = q.conn.execute("SELECT owner, n FROM rate").fetchall()
                q.close()
                self.assertTrue(all(o.startswith("job:") for o, _n in spent
                                    if _n > 0 and not o.startswith(("33", "55"))), spent)


OWNER = ("ana@plx.example", "33333333-3333-4333-8333-333333333333")
COLLEAGUE = ("sam@plx.example", "55555555-5555-4555-8555-555555555555")


def job_item(item_id: str, owner: str, jobs: list) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"item_id": item_id, "event_id": "e-" + item_id, "autopilot": "invoice_entry",
            "owner": owner, "title": "System work from a meeting: Weekly ops",
            "body": {"happened": {"event_type": "meeting.ended", "object_id": "Meeting:1",
                                  "subject": "Weekly ops"},
                     "prepared": {"kind": "job", "summary": "Tasks agreed in the meeting",
                                  "jobs": jobs},
                     "done_when": "person_decided", "label": "System work from a meeting"},
            "audiences": ["mail:" + owner], "markings": [], "citations": ["calendar-event://m/1"],
            "due_at": now, "created_at": now}


class ApprovalJobSettles(unittest.TestCase):
    """A job item about an MC approval request leaves the queue once the approval is
    decided in MC (by anyone), whatever its own rule: 22 of 23 such items in one live
    queue were open on approvals MC already showed approved. A pending approval keeps
    its item (the person's own decision still closes it); a status that cannot be read
    keeps it too."""

    def test_decided_in_mc_closes_pending_stays_person_decided_still_closes(self):
        from unittest import mock
        from operating import autopilot_engine as ae
        with tempfile.TemporaryDirectory() as tmp:
            q = ap.Queue(Path(tmp) / "queue" / ap.QUEUE_FILE)
            try:
                status = {}
                with q.conn:
                    for n, st in enumerate(("approved", "rejected", "pending", "pending", "")):
                        item = job_item(f"ap_{n}", OWNER[1], [])
                        oid = f"MCApproval:mission_control.approvals:{{\"id\":\"apg_{n}\"}}"
                        item["autopilot"] = "invoice_entry"
                        item["body"]["happened"] = {"event_type": "approval.requested",
                                                    "object_id": oid}
                        q.add_item(item)
                        if st:
                            status[oid] = {"status": {"value": st}}
                    q.add_item(job_item("ap_meeting", OWNER[1], []))
                read = lambda _st, ids, _props: {i: status[i] for i in ids if i in status}  # noqa: E731
                with mock.patch.object(ae, "_read", read):
                    done = ae.settle(object(), q, {}, None, now=datetime.now(timezone.utc),
                                     decided={"ap_3": {"closed": "person_decided"}})
                self.assertEqual(dict(done), {"approval_decided": 2, "person_decided": 1})
                self.assertEqual({i["item_id"] for i in q.items()},
                                 {"ap_2", "ap_4", "ap_meeting"})
            finally:
                q.close()


class JobReview(unittest.TestCase):
    """The person reviews a proposed change: it is prepared through the desk as them (one
    prepared change per job, never a second), confirmed on ``/api/prepared``; another
    person's item is not found; a stored proposal the checks refuse is never offered."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        path = root / "queue" / ap.QUEUE_FILE
        q = ap.Queue(path)
        with q.conn:
            q.add_item(job_item("ap_job", OWNER[1], [{"tool": "plx.prepare_write",
                                                      "arguments": TASK}]))
            q.add_item(job_item("ap_bad", OWNER[1], [{"tool": "plx.prepare_write",
                                                      "arguments": {**TASK, "system": "mail"}}]))
            q.add_item(job_item("ap_sam", COLLEAGUE[1], [{"tool": "plx.prepare_write",
                                                          "arguments": TASK}]))
            brief = job_item("ap_brief", OWNER[1], [])
            brief["body"]["prepared"]["brief"] = "Open: the PO 77 date (mail of today)."
            q.add_item(brief)
            q.add_item(job_item("ap_empty", OWNER[1], []))
        q.close()
        self.fake = Transport()
        guard = PeopleExclusion(["99999999-9999-4999-8999-999999999999"])
        guard.attach_directory([(OWNER[1], OWNER[0], []), (COLLEAGUE[1], COLLEAGUE[0], [])])
        env = {aw.APPROVED_ENV: "planner_write"}
        b = self.backend = MockBackend()
        b.writes_environ = env
        b.autopilot_queue = path
        b.autopilot_write_state = root / "autopilot-write"
        b.autopilot_grant = (lambda aud: True, frozenset(), lambda hs: {h: None for h in hs})
        b.autopilot_owner = lambda reader: reader.session.subject_oid == OWNER[1]
        b.autopilot_guard = guard
        b.autopilot_adapters = {"planner_write": wa.PlannerAdapter(self.fake, environ=env)}

    def client(self, who) -> TestClient:
        s = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                            subject_email=who[0], subject_oid=who[1])
        client = TestClient(create_app(self.backend, DevAuth(lambda: s, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def drafts(self, client) -> dict:
        r = client.get("/api/autopilot/drafts")
        self.assertEqual(r.status_code, 200, r.text)
        return {i["item_id"]: i for i in r.json()["items"]}

    def test_review_prepares_once_as_the_person_and_confirm_is_two_steps(self):
        owner = self.client(OWNER)
        shown = self.drafts(owner)
        # not the refused one, not Sam's, not one with neither a change nor a brief; a
        # meeting brief shows as the item's own text
        self.assertEqual(set(shown), {"ap_job", "ap_brief"})
        self.assertEqual((shown["ap_brief"]["jobs"], shown["ap_brief"]["message"]),
                         ([], "Open: the PO 77 date (mail of today)."))
        job = shown["ap_job"]
        self.assertEqual((job["kind"], job["channel"]), ("job", "system"))
        self.assertEqual(job["jobs"][0]["system"], "planner")
        self.assertIsNone(job["jobs"][0]["prepared_id"])
        r = owner.post("/api/autopilot/items/ap_job/jobs/0/prepare")
        self.assertEqual(r.status_code, 200, r.text)
        card = r.json()
        self.assertEqual((card["state"], card["origin"]), ("prepared", "invoice_entry"))
        self.assertEqual(self.fake.calls, [])             # reviewing writes nothing
        again = owner.post("/api/autopilot/items/ap_job/jobs/0/prepare").json()
        self.assertEqual(again["prepared_id"], card["prepared_id"])
        self.assertEqual(self.drafts(owner)["ap_job"]["jobs"][0]["prepared_id"],
                         card["prepared_id"])
        preview = owner.get(f"/api/prepared/{card['prepared_id']}").json()
        done = owner.post(f"/api/prepared/{card['prepared_id']}/confirm",
                          json={"preview_hash": preview["preview_hash"], "confirm": True})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["state"], "confirmed")
        # every change of the item confirmed: the item leaves the list
        self.assertNotIn("ap_job", self.drafts(owner))
        # confirmed once: a second review answers the same change, never a new one
        self.assertEqual(owner.post("/api/autopilot/items/ap_job/jobs/0/prepare").json()[
            "prepared_id"], card["prepared_id"])

    def test_an_approval_job_card_carries_its_recommendation_reasons_and_mc_link(self):
        item = job_item("ap_gate", OWNER[1], [{"tool": "plx.prepare_write", "arguments": TASK}])
        item["autopilot"] = "invoice_entry"
        item["body"]["prepared"].update({
            "recommendation": "approve", "reasons": ["an earlier approval was granted"],
            "mc": {"link": "https://mc.plxcustomer.io/tasks/TASK-3"},
            "action": {"kind": "mc_approval", "note": "Decide in Mission Control."}})
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.add_item(item)
        q.close()
        card = self.drafts(self.client(OWNER))["ap_gate"]
        self.assertEqual((card["kind"], len(card["jobs"])), ("job", 1))
        self.assertEqual(card["facts"], ["Recommended: approve", "an earlier approval was granted"])
        self.assertEqual([(lk["label"], lk["href"]) for lk in card["links"]],
                         [("Decide in Mission Control", "https://mc.plxcustomer.io/tasks/TASK-3")])
        self.assertEqual(card["note"], "Decide in Mission Control.")
        # a plain job card carries none of it
        plain = self.drafts(self.client(OWNER))["ap_job"]
        self.assertEqual((plain["facts"], plain["links"], plain["note"]), ([], [], ""))

    def test_another_persons_item_a_refused_proposal_or_a_bad_index_is_not_found(self):
        owner = self.client(OWNER)
        for path in ("/api/autopilot/items/ap_sam/jobs/0/prepare",
                     "/api/autopilot/items/ap_bad/jobs/0/prepare",
                     "/api/autopilot/items/ap_job/jobs/3/prepare",
                     "/api/autopilot/items/ap_job/jobs/-1/prepare"):
            with self.subTest(path):
                self.assertEqual(owner.post(path).status_code, 404)
        sam = self.client(COLLEAGUE)
        self.assertEqual(sam.post("/api/autopilot/items/ap_job/jobs/0/prepare").status_code,
                         404)
        self.assertEqual(self.fake.calls, [])

    def test_a_dismissed_change_closes_the_item(self):
        owner = self.client(OWNER)
        pid = owner.post("/api/autopilot/items/ap_job/jobs/0/prepare").json()["prepared_id"]
        self.assertEqual(owner.post(f"/api/prepared/{pid}/dismiss").status_code, 200)
        self.assertNotIn("ap_job", self.drafts(owner))
        self.assertEqual(self.fake.calls, [])
        # the person decided every change: the item is closed as their decision (a reject:
        # nothing confirmed), and the engine's settle marks the queue item done
        from operating import autopilot_engine as ae
        from operating import autopilot_outcomes as ao
        decided = ao.read_decisions(self.backend.autopilot_write_state.parent
                                    / ao.DECISIONS_FILE)
        self.assertEqual({k: (v["decision"], v["closed"]) for k, v in decided.items()},
                         {"ap_job": ("reject", "person_decided")})
        shown = {i["item_id"]: i for i in owner.get("/api/autopilot").json()["items"]}
        self.assertEqual(shown["ap_job"]["state"], "rejected")
        q = ap.Queue(self.backend.autopilot_queue)
        try:
            done = ae.settle(None, q, {}, None, now=datetime.now(timezone.utc),
                             decided=decided)
            self.assertEqual(dict(done), {"person_decided": 1})
            self.assertEqual({i["item_id"] for i in q.items()}, {"ap_bad", "ap_sam", "ap_brief", "ap_empty"})
        finally:
            q.close()

    def test_an_expired_review_is_offered_again_as_a_new_change(self):
        from unittest import mock
        from operating import prepared_writes as pw
        owner = self.client(OWNER)
        first = owner.post("/api/autopilot/items/ap_job/jobs/0/prepare").json()["prepared_id"]
        with mock.patch.object(pw, "EXPIRES", timedelta(0)):
            job = self.drafts(owner)["ap_job"]["jobs"][0]
            self.assertEqual((job["prepared_id"], job["state"]), (None, "expired"))
            again = owner.post("/api/autopilot/items/ap_job/jobs/0/prepare").json()
        self.assertNotEqual(again["prepared_id"], first)
        job = self.drafts(owner)["ap_job"]["jobs"][0]
        self.assertEqual((job["prepared_id"], job["state"]), (again["prepared_id"], "prepared"))
        self.assertEqual(owner.post("/api/autopilot/items/ap_job/jobs/0/prepare").json()[
            "prepared_id"], again["prepared_id"])          # and then once, as before
        self.assertEqual(self.fake.calls, [])


class RegistryRules(unittest.TestCase):
    def test_a_job_autopilot_is_the_job_handler_and_the_job_draft_together(self):
        reg = ap.load_registry(None)
        jobs = [a for a in reg["autopilots"] if a.get("draft") == "job"]
        self.assertTrue(jobs)
        self.assertTrue(all(a["handler"] == "job" and a["done_when"] == "person_decided"
                            for a in jobs))
        base = dict(reg)
        bad = {**jobs[0], "id": "job_bad", "handler": "reply_prep"}
        with self.assertRaises(ap.RegistryInvalid):
            ap.validate_registry({**base, "autopilots": [bad]})
        bad = {**jobs[0], "id": "job_bad"}
        bad.pop("draft")
        with self.assertRaises(ap.RegistryInvalid):
            ap.validate_registry({**base, "autopilots": [bad]})
        # standing work: a rule-based handler, on a job autopilot only
        for wrong in ({"standing": "no_such_handler"}, {"standing": "job"},
                      {"standing": "approval_recommendation", "draft": "agent"}):
            with self.subTest(wrong=wrong), self.assertRaises(ap.RegistryInvalid):
                ap.validate_registry({**base, "autopilots": [{**jobs[0], "id": "job_bad",
                                                              **wrong}]})
        ap.validate_registry({**base, "autopilots": [
            {**jobs[0], "id": "job_ok", "standing": "approval_recommendation"}]})


if __name__ == "__main__":
    unittest.main()
