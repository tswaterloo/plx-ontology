"""The Autopilot's decision step as one general assistant (operating/autopilot_agent.py): every
message goes to one session as the person (no rule matching), which prepares the reply, the
question to the person, the system changes or nothing; the screens, the proposal checks, the
opt-in, the live-queue-only rule and the hourly budget stay. At the boundary: what reaches the
model, what an item holds, what the queue gets per message (N=1/10/60)."""

from __future__ import annotations

from datetime import timedelta
import json
import os
import re
import subprocess
import unittest
from unittest import mock

from operating import autopilot as ap
from operating import autopilot_agent as ag
from operating import autopilot_check as ck
from operating import autopilot_jobs as aj
from operating import autopilot_write as aw
from operating import proactive_engine as pe
from operating.tests import test_autopilot as ta
from operating.tests.test_autopilot_jobs import (BIN, GATE, HOME, MAIL, PLAN, PO_DATE, TASK,
                                                 People)
from operating.tests.test_payroll_marking import PAYSTUB

SAGE = {"system": "sage", "operation": "ic_adjustment", "target": {}, "change": {"qty": 1},
        "reason": "Count correction."}
TO_SENDER = {"system": "mail", "operation": "send_email", "target": {},
             "change": {"to": ["sup@vendor.example"], "subject": "RE: PO 77",
                        "body": "Thanks, noted."}, "reason": "Reply."}
TO_COLLEAGUE = {"system": "mail", "operation": "send_email", "target": {},
                "change": {"to": ["buyer@plx.example"], "subject": "PO 77 moves to Nov 20",
                           "body": "Please update the plan."}, "reason": "Pass it on."}


def final(**kw) -> str:
    return json.dumps({"summary": kw.pop("summary", "Answer the supplier"), **kw})


class Session:
    """The tool session stand-in: calls the write tools with ``calls``, returns ``looked``."""

    def __init__(self, calls=(), looked=(), answer=None) -> None:
        self.calls, self.looked = list(calls), list(looked)
        self.answer = final(reply="Yes, Nov 20 works for PO 77.") if answer is None else answer
        self.runs: list[tuple[str, str]] = []
        self.answers: list = []
        self.reads: list[bool] = []

    def run(self, owner, prompt, *, tools=(), job=False, agent=False, reads=True):
        assert agent and not job
        self.runs.append((owner, prompt))
        self.reads.append(reads)
        by_name = {t.spec["name"]: t for t in tools}
        for name, args in self.calls:
            self.answers.append(by_name[name].call(args))
        return self.answer, self.looked


class Check:
    """The second pass's ``claude -p`` stand-in: keeps every item it is shown but ``drop``;
    ``answer`` replaces its whole answer; ``fail`` raises."""

    def __init__(self, drop=(), answer=None, fail=None) -> None:
        self.drop, self.answer, self.fail = set(drop), answer, fail
        self.prompts: list[str] = []

    def __call__(self, argv, *, input, **_kw):
        self.prompts.append(input)
        if self.fail is not None:
            raise self.fail
        ids = re.findall(r"^\[([RQOPT]\d*)\]", input, re.M)
        out = self.answer if self.answer is not None else json.dumps(
            {"items": [{"id": i, "keep": "no" if i in self.drop else "yes", "why": "-"}
                       for i in ids]})
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": out}),
                                           stderr="")


def assistant(session, check=None) -> ag.Assistant:
    checker = ck.Checker(binary=BIN, home=HOME, call=check or Check())
    return ag.Assistant("opus", lookup=session, binary=BIN, home=HOME, per_cycle=100,
                        checker=checker)


# a mail put to the person that the ask rules read an ask in: a reply is due
# (``autopilot_agent_io.reply_due``)
ASKED = {**MAIL, "to_me": True, "asks": True}


def run(a, event=ASKED, people=None):
    return a.run(event, "me", aj.described(event, event.get("text", "")), "me@plx.example",
                 people or People())


class AssistantBoundary(unittest.TestCase):
    def test_excluded_people_pay_and_freightcom_never_reach_the_model(self):
        session = Session()
        cases = {
            "not_cleared": (GATE, People(barred={"dev@plx.example"})),
            "not_cleared ": ({**MAIL, "cleared": False}, People()),
            "pay_or_hr": ({**GATE, "text": PAYSTUB}, People()),
            "freightcom": ({**MAIL, "sender": "billing@freightcom.com"}, People()),
        }
        for why, (event, people) in cases.items():
            with self.subTest(why):
                self.assertEqual(run(assistant(session), event, people), (None, why.strip()))
        self.assertEqual(session.runs, [])
        # what the session read or wrote is screened too: a hit drops the whole decision
        for looked, why in ((["carrier: Freightcom, pickup 20 Nov"], "freightcom"),
                            (["{'requested_by': 'cos@plx.example'}"], "excluded_in_thread")):
            with self.subTest(why):
                got = run(assistant(Session(looked=looked)),
                          people=People(barred={"cos@plx.example"}))
                self.assertEqual(got, (None, why))

    def test_one_session_decides_reply_question_changes_or_nothing(self):
        # every message goes to the session: no first call, no rule
        got, why = run(assistant(Session(calls=[("plx.prepare_write", PO_DATE)])))
        self.assertEqual(why, "")
        self.assertEqual(got.reply, "Yes, Nov 20 works for PO 77.")
        self.assertEqual(got.proposals, [{"tool": "plx.prepare_write", "arguments": PO_DATE}])
        # the answer is the person's: the question stands, never a reply beside it
        ask = {"question": "Can PO 77 take Nov 20?",
               "options": [{"label": "Yes", "reply": "Yes, Nov 20 is fine."},
                           {"label": "No", "reply": "Nov 20 is too late for us."},
                           {"label": "", "reply": "dropped: no label"}]}
        got, _ = run(assistant(Session(answer=final(reply="Sure!", ask=ask))))
        self.assertEqual((got.reply, got.ask["question"]), ("", "Can PO 77 take Nov 20?"))
        self.assertEqual([o["label"] for o in got.ask["options"]], ["Yes", "No"])
        # nothing prepared, or an answer that is not the contract: no item, never a guess
        for answer in (final(nothing="A thank-you"), "I think you should reply yes.",
                       json.dumps({"reply": 42, "ask": "yes", "todos": "Sage"})):
            with self.subTest(answer=answer):
                self.assertEqual(run(assistant(Session(answer=answer))), (None, "nothing_to_do"))

    def test_proposals_pass_the_write_paths_checks_and_a_reply_is_never_a_second_message(self):
        session = Session(calls=[("plx.prepare_write", SAGE), ("plx.prepare_write", TO_SENDER),
                                 ("plx.prepare_write", TO_COLLEAGUE)],
                          answer=final(todos=[{"system": "Sage", "record": "IC adj 5",
                                               "what": "Post the count correction"}]))
        got, why = run(assistant(session))
        self.assertIn("refused", session.answers[0])          # Sage has no write path
        self.assertEqual([p["arguments"] for p in got.proposals], [TO_COLLEAGUE])
        self.assertEqual(got.todos[0]["system"], "Sage")
        self.assertEqual(got.reply, "")

    def test_a_time_look_reads_only_for_a_cleared_person_and_never_replies(self):
        session = Session(calls=[("plx.prepare_write", TASK)])
        self.assertEqual(assistant(session).look("me", "Ana", People(barred={"me"})),
                         (None, "not_cleared"))
        self.assertEqual(session.runs, [])
        # nothing computed as changed for them (none, or an unreadable list): no session
        for bad in ((), None, "PO cmpo0001", [{"record": "", "line": "x"}], [{"line": "x"}]):
            self.assertEqual(assistant(session).look(
                "me", "Ana", People(), as_of="2026-04-07T13:00:00+00:00", changes=bad),
                (None, "nothing_changed"))
        self.assertEqual(session.runs, [])
        got, why = assistant(session).look("me", "Ana", People(),
                                           as_of="2026-04-07T13:00:00+00:00", changes=PLAN_CH)
        self.assertEqual((why, got.reply), ("", ""))
        self.assertEqual(len(got.proposals), 1)
        prompt = session.runs[0][1]
        self.assertIn("Tuesday 2026-04-07 09:00 AM ET", prompt)
        self.assertIn("act as of then", prompt)
        self.assertIn("[" + PLAN + "] plan", prompt)


# put to the person, no certain ask, but they usually answer this sender: a turn
TURN = {**MAIL, "to_me": True, "asks": False, "sender_history": [10, 8]}


class SecondCheck(unittest.TestCase):
    """Only what the second pass (operating/autopilot_check.py) finds supported stands, and
    only where a reply is due by what is computed on the message; anything it cannot decide
    does not."""

    ASK = {"question": "Can PO 77 take Nov 20?",
           "options": [{"label": "Yes", "reply": "Yes, Nov 20 works."},
                       {"label": "Friday", "reply": "Friday at 3 works for us."}]}

    def test_the_check_sees_the_message_the_reads_and_every_item(self):
        check = Check()
        looked = ['{"title": {"untrusted_text": "PO 77 due Nov 18", "origin": '
                  '{"citation": "sql://abc"}}}']
        session = Session(calls=[("plx.prepare_write", PO_DATE)], looked=looked,
                          answer=final(reply="Yes, Nov 20 works.",
                                       todos=[{"system": "Sage", "record": "PO 77",
                                               "what": "Move the date"}]))
        got, why = run(assistant(session, check))
        self.assertEqual((why, got.reply, len(got.proposals), len(got.todos)),
                         ("", "Yes, Nov 20 works.", 1, 1))
        prompt = check.prompts[0]
        for part in (MAIL["text"], '"PO 77 due Nov 18"', "[R] Reply", "[P1] Proposed",
                     "[T1] To-do"):
            self.assertIn(part, prompt)
        self.assertNotIn('"needed"', prompt)          # whether to reply is not its call
        self.assertNotIn("sql://abc", prompt)        # citation wrappers are not material
        self.assertIn("--model", check_argv(check, session))

    def test_unsupported_items_are_dropped_and_a_reply_becomes_the_persons_question(self):
        reply = Session(answer=final(reply="Yes, Nov 20 works; the trucks are booked."))
        got, why = run(assistant(reply, Check(drop={"R"})))
        self.assertEqual((why, got.reply, got.ask), ("", "", {"question": ck.ASK_PLAIN,
                                                              "options": []}))
        # a turn the person's habit or the ask rules' open reading gives is theirs too
        self.assertEqual(run(assistant(reply, Check(drop={"R"})), TURN)[0].ask,
                         {"question": ck.ASK_PLAIN, "options": []})
        # copied: no reply due, so not the person's question either
        self.assertEqual(run(assistant(reply, Check(drop={"R"})), {**MAIL, "asks": True}),
                         (None, "nothing_to_do"))
        card = Session(answer=final(ask=self.ASK))
        three = Session(answer=final(ask={**self.ASK, "options": [
            *self.ASK["options"], {"label": "No", "reply": "Nov 20 is too late."}]}))
        got, _ = run(assistant(three, Check(drop={"O2"})))
        self.assertEqual([o["label"] for o in got.ask["options"]], ["Yes", "No"])
        # one answer left is a guess at the person's choice: they write it
        got, _ = run(assistant(card, Check(drop={"O2"})))
        self.assertEqual(got.ask, {"question": self.ASK["question"], "options": []})
        got, _ = run(assistant(card, Check(drop={"Q", "O1", "O2"})))
        self.assertEqual(got.ask, {"question": ck.ASK_PLAIN, "options": []})
        self.assertEqual(run(assistant(card, Check(drop={"O1", "O2"})), TURN)[0].ask,
                         {"question": self.ASK["question"], "options": []})
        work = Session(calls=[("plx.prepare_write", TO_COLLEAGUE), ("plx.prepare_write", PO_DATE)],
                       answer=final(todos=[{"system": "Sage", "record": "x", "what": "y"}]))
        got, _ = run(assistant(work, Check(drop={"P1", "T1"})))
        self.assertEqual(([p["arguments"] for p in got.proposals], got.todos), ([PO_DATE], []))

    def test_no_verdict_a_bad_one_or_an_unnamed_item_fails_closed(self):
        session = Session(answer=final(reply="Yes, Nov 20 works.", ask=None),
                          calls=[("plx.prepare_write", PO_DATE)])
        for check in (Check(answer="Looks fine to me."), Check(answer='{"items": "all"}'),
                      Check(answer='{"items": {"R": "yes"}}'),
                      Check(fail=subprocess.TimeoutExpired("claude", 1)),
                      Check(fail=OSError("gone"))):
            with self.subTest(answer=check.answer, fail=check.fail):
                self.assertEqual(run(assistant(session, check)), (None, "check_failed"))
        # named items only: the change it did not name does not stand
        named = Check(answer=json.dumps({"items": [{"id": "R", "keep": "yes"}]}))
        got, _ = run(assistant(session, named))
        self.assertEqual((got.reply, got.proposals), ("Yes, Nov 20 works.", []))

    def test_a_mails_exchange_kind_is_read_from_its_change_key(self):
        from operating.autopilot_events import item_kind
        box = "graph-mail://8fbb69ae-4c54-4615-aa30-38d0ba460681/AAkALgAAAAAAHYQ="
        for key, kind in (("CwAAABYAAABtc1vm", "meeting_request"), ("CQAAABYAAABtc1vm", "message"),
                          ("DAAAABYAAAB", "meeting_response"), ("DQAAABYAAAB", "meeting_cancel"),
                          ("HwAAABYAAAB", "auto_reply"), ("FwAAABYAAAB", "undeliverable"),
                          ("GAAAABYAAAB", ""), ("!!!!", ""), ("", "")):
            with self.subTest(key=key):
                self.assertEqual(item_kind([None, f"{box}@{key}"]), kind)
        self.assertEqual(item_kind([]), "")
        # the session and the check both read what the mail is
        from operating import autopilot_agent_io as io
        said = io.agent_input({**MAIL, "item_kind": "meeting_request"}, "", {}, {})[0]
        self.assertIn("meeting invitation to them", said)
        self.assertNotIn("meeting invitation", io.agent_input(MAIL, "", {}, {})[0])
        self.assertEqual(item_kind(["teams://chat/1@CwAAAB"]), "")

    def test_whether_a_reply_is_due_is_computed_never_the_models(self):
        """The same supported reply, change and to-do on messages that differ only in what
        is computed on them: addressing, the ask rules' reading, the person's habit. Asked:
        the written reply stands; only their turn: the open question instead (theirs to
        word); not due: neither (the supported change stays)."""
        from operating.autopilot_check import ASK_PLAIN
        session = Session(calls=[("plx.prepare_write", PO_DATE)],
                          answer=final(reply="Yes, Nov 20 works.",
                                       todos=[{"system": "Sage", "record": "PO 77",
                                               "what": "Move the date"}]))
        copied = {**MAIL, "to_me": False, "cc": ["me@plx.example"], "asks": True}
        for name, event, due in (
                ("put to them, asks", ASKED, "reply"),
                ("put to them, they answer this sender", TURN, "question"),
                ("put to them, ask undecided by the rules", {**TURN, "sender_history": [0, 0],
                                                             "ask_check": "model"}, "question"),
                ("copied, they wrote in the thread", {**copied, "own_posts": [30, 2]},
                 "question"),
                ("in a group, they usually answer this sender there",
                 {**copied, "answerer": [6, 4]}, "question"),
                ("put to them, rarely answer this sender", {**TURN, "sender_history": [10, 2]},
                 ""),
                ("copied", copied, ""),
                ("greets someone else", {**ASKED, "addressed_other": True}, ""),
                ("nothing computed on it", MAIL, ""),
                ("an invitation put to them", {**MAIL, "to_me": True,
                                               "item_kind": "meeting_request"}, "reply"),
                ("an invitation they are copied on", {**copied, "asks": False,
                                                      "item_kind": "meeting_request"}, ""),
                ("an automatic reply that reads as asking", {**ASKED, "item_kind": "auto_reply"},
                 ""),
                ("an invitation's answer in a thread of theirs",
                 {**TURN, "item_kind": "meeting_response"}, ""),
                ("a kind not known", {**ASKED, "item_kind": "something_new"}, "reply"),
                ("unreadable fields", {**MAIL, "to_me": "yes", "asks": "yes",
                                       "sender_history": "lots", "own_posts": [None, "x"]},
                 "")):
            with self.subTest(name):
                got, why = run(assistant(session), event)
                self.assertEqual((why, got.reply, got.ask, len(got.todos), len(got.proposals)),
                                 ("", "Yes, Nov 20 works." if due == "reply" else "",
                                  {"question": ASK_PLAIN, "options": []} if due == "question"
                                  else None, 1 if due else 0, 1))
        # on a turn, the model's own open question is kept, its offered answers are not
        offered = Session(answer=final(ask={"question": "Which date suits you?", "options": [
            {"label": "Nov 20", "reply": "Nov 20 works."},
            {"label": "Nov 27", "reply": "Nov 27 works."}]}))
        self.assertEqual(run(assistant(offered), TURN)[0].ask,
                         {"question": "Which date suits you?", "options": []})
        self.assertEqual(len(run(assistant(offered), ASKED)[0].ask["options"]), 2)
        # the check's own say on whether a reply is due is not read
        ignored = Check(answer=json.dumps({"needed": "no", "items": [
            {"id": i, "keep": "yes"} for i in ("R", "P1", "T1")]}))
        self.assertEqual(run(assistant(session, ignored), ASKED)[0].reply, "Yes, Nov 20 works.")
        # nothing due and nothing else: nothing at all
        only_reply = Session(answer=final(reply="Thanks!"))
        self.assertEqual(run(assistant(only_reply), copied), (None, "nothing_to_do"))

    def test_screened_material_never_reaches_the_check(self):
        for looked, people, why in (
                (["carrier: Freightcom, pickup 20 Nov"], People(), "freightcom"),
                (["{'requested_by': 'cos@plx.example'}"], People(barred={"cos@plx.example"}),
                 "excluded_in_thread")):
            with self.subTest(why):
                check = Check()
                self.assertEqual(run(assistant(Session(looked=looked), check), people=people),
                                 (None, why))
                self.assertEqual(check.prompts, [])

    def test_a_look_keeps_only_what_the_check_finds_new(self):
        todos = [{"system": "sage", "record": "IN-1", "what": "Invoice shipment 1"}]
        check = Check(drop={"P1"})
        session = Session(calls=[("plx.prepare_write", PO_DATE)],
                          answer=final(summary="Day start", todos=todos))
        got, why = assistant(session, check).look("me", "Ana", People(),
                                                  as_of="2026-04-07T13:00:00+00:00",
                                                  changes=[*PO_CH, *IN_CH])
        self.assertEqual((why, got.proposals, got.todos), ("", [], todos))
        # the check judges against the list the session was given, as of the look's time
        # (a replayed look reads today's records: a later state never counts against it)
        self.assertIn("computed from the records as of the look", check.prompts[0])
        for c in [*PO_CH, *IN_CH]:
            self.assertIn(f"[{c['record']}] {c['line']}", check.prompts[0])
        self.assertIn("It is Tuesday 2026-04-07 09:00 AM ET.", check.prompts[0])
        self.assertNotIn('"needed"', check.prompts[0])
        everything = Check(drop={"P1", "T1"})
        self.assertEqual(assistant(session, everything).look(
            "me", "Ana", People(), as_of="2026-04-07T13:00:00+00:00",
            changes=[*PO_CH, *IN_CH]), (None, "check_dropped"))


def check_argv(check, session) -> str:
    """The check's command line as the model process gets it (one call, no tools)."""
    a = assistant(Session(answer=session.answer))
    argv = []
    a.checker.call = lambda av, **kw: argv.extend(av) or check(av, **kw)
    run(a)
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    return " ".join(argv)


class LiveCycle(ta.Fixture):
    """In the live queue with the switch on, the assistant decides every message in place of
    the rule-matched autopilots; off, nothing changes; a replay never starts it."""

    def live(self, ids, a, name="s.sqlite", queue="autopilot", on=True):
        path = self.dir / name
        if not path.exists():
            self.w.build(path)
        st = pe._Store(path, threads=1)
        q = ap.Queue(self.dir / queue / ap.QUEUE_FILE)
        q.__dict__.setdefault("_draft_context", {})["assistant"] = a
        try:
            from operating import autopilot_agent_io as io
            looks = io.look_events
            with mock.patch.dict(os.environ, on if isinstance(on, dict) else {
                    ap.AGENT_ENV: "1" if on else "0", ap.AGENT_PEOPLE_ENV: ""}), \
                    mock.patch.object(io, "look_events", lambda o, now: looks(
                        o, now, changes=(lambda _o, _at: changes) if changes else None)):
                out = ap.run_cycle(st, q, ids, people=ta.people(), registry=None,
                                   environ=self.env, now=ta.NOW,
                                   landed_at=ap._iso(ta.NOW - timedelta(minutes=2)))
            items = [dict(r) for r in q.conn.execute("SELECT * FROM items")]
            return out, items
        finally:
            st.close()
            q.close()

    def test_a_reply_and_its_changes_are_two_items_for_the_person_to_confirm(self):
        msg = self.message(80, "Confirmed, we deliver PO 77 on Nov 20. Can you confirm?")
        out, items = self.live([msg], assistant(Session(calls=[("plx.prepare_write", PO_DATE)])))
        self.assertEqual({i["autopilot"] for i in items}, {"assistant"}, out)
        bodies = sorted((json.loads(i["body"])["prepared"] for i in items),
                        key=lambda b: b["kind"])
        self.assertEqual([b["kind"] for b in bodies], ["job", "reply"])
        self.assertEqual(bodies[0]["jobs"], [{"tool": "plx.prepare_write", "arguments": PO_DATE}])
        self.assertEqual(bodies[1]["draft"]["body"], "Yes, Nov 20 works for PO 77.")
        self.assertNotIn("also_jobs", bodies[1])
        # nothing ran: the job holds no executor action, the reply only its send for the
        # person's confirm, and nothing was sent or written by the cycle
        acts = {json.loads(i["body"])["prepared"]["kind"]:
                [a["kind"] for a in aw.actions_of({"body": json.loads(i["body"])})]
                for i in items}
        self.assertEqual(acts, {"job": [], "reply": ["send_mail"]})
        self.assertEqual((out["sent"], out["writes"]), (0, 0))

    def test_a_question_is_a_your_call_card_with_the_answers_replies(self):
        ask = {"question": "Ship Friday?", "options": [{"label": "Yes", "reply": "Yes, Friday."},
                                                       {"label": "No", "reply": "Not Friday."}]}
        msg = self.message(81, "Can it ship Friday?")
        out, items = self.live([msg], assistant(Session(answer=final(ask=ask))))
        prepared = json.loads(items[0]["body"])["prepared"]
        self.assertEqual(prepared["your_call"]["ask"], "Ship Friday?")
        self.assertEqual(prepared["your_call"]["options"][0]["reply"], "Yes, Friday.")
        self.assertEqual(prepared["draft"]["body"], "")

    def test_switch_off_or_a_replay_never_starts_the_assistant(self):
        msg = self.message(82, "Confirmed, we deliver PO 77 on Nov 20.")
        session = Session()
        out, items = self.live([msg], assistant(session), on=False)
        self.assertFalse([i for i in items if i["autopilot"] == "assistant"])
        out, items = self.live([msg], assistant(session), "r.sqlite", queue="replay")
        self.assertEqual(session.runs, [])
        self.assertFalse([i for i in items if i["autopilot"] == "assistant"])

    def test_the_people_list_switches_the_assistant_on_for_them_alone(self):
        msg = lambda i: self.message(84 + i, "Confirmed, we deliver PO 77 on Nov 20.")
        off = {ap.AGENT_ENV: "0", ap.AGENT_PEOPLE_ENV: ""}
        _, rules = self.live([msg(0)], assistant(Session()), on=off)
        for env, mine in (({ap.AGENT_ENV: "0", ap.AGENT_PEOPLE_ENV: ta.OURS.upper()}, True),
                          ({ap.AGENT_ENV: "1", ap.AGENT_PEOPLE_ENV: f"x@y.example, {ta.OURS_OID}"},
                           True),
                          ({ap.AGENT_ENV: "1", ap.AGENT_PEOPLE_ENV: "x@y.example"}, False)):
            with self.subTest(env=env):
                self.setUp()
                session = Session()
                out, items = self.live([msg(0)], assistant(session), on=env)
                got = {i["autopilot"] for i in items}
                if mine:
                    self.assertEqual(got, {"assistant"}, out)
                else:
                    # someone else listed: this person keeps the rules, as with the switch off
                    self.assertEqual((session.runs, got), ([], {i["autopilot"] for i in rules}))
        self.assertEqual(ap.agent_people({ap.AGENT_ENV: "1"}), None)
        self.assertEqual(ap.agent_people({ap.AGENT_ENV: "yes", ap.AGENT_PEOPLE_ENV: " ; ,"}),
                         frozenset())
        self.assertFalse(ap.agent_for("p", {}, {ap.AGENT_PEOPLE_ENV: ","}))

    def test_nothing_to_do_makes_no_item_and_the_rules_stay_out(self):
        msg = self.message(83, "Thanks for the update!")
        out, items = self.live([msg], assistant(Session(answer=final(nothing="thanks"))))
        self.assertEqual(items, [])
        self.assertEqual(out["skipped"].get("assistant:nothing_to_do"), 1, out)
        self.assertFalse([k for k in out["prepared"] if k != "assistant"])

    def test_cardinality_one_item_per_message_up_to_the_hourly_budget(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                msgs = [self.message(900 + i, f"Can you confirm PO 77 for Nov 20? {i}")
                        for i in range(n)]
                out, items = self.live(msgs, assistant(Session()), f"n{n}.sqlite")
                made = min(n, 30)
                self.assertEqual(len(items), made, out["skipped"])
                self.assertEqual(len({i["event_id"] for i in items}), made)
                self.assertEqual(out["skipped"].get("assistant:rate_limited", 0), n - made)


class _Every(set):
    def __contains__(self, item) -> bool:
        return True


class GroupTurns(ta.Fixture):
    """A group message not addressed to the person: the assistant decides it only when they
    are in that exchange (wrote there lately, or answer this sender there); otherwise it is
    skipped as before."""

    def turn(self, posts, answerer=(0, 0)):
        real = ap.discover

        def unaddressed(*a, **k):
            events, skipped = real(*a, **k)
            return [{**e, "unaddressed": True, "to_me": False} for e in events], skipped
        msg = self.message(95, "Can someone confirm PO 77 for Nov 20?", sender=ta.APPROVER,
                           customer=False)
        session = Session()
        with mock.patch.object(ap, "discover", unaddressed), \
                mock.patch.object(ap.Queue, "own_posts", lambda *a, **k: posts), \
                mock.patch.object(ap.Queue, "usual_answerer", lambda *a, **k: answerer), \
                mock.patch.object(ap.Queue, "writers", lambda *a, **k: _Every()):
            out, items = LiveCycle.live(self, [msg], assistant(session),
                                        f"g{posts[1]}{answerer[1]}.sqlite")
        return session, items

    def test_the_assistant_takes_a_group_turn_only_when_the_person_is_in_the_exchange(self):
        session, items = self.turn((None, 0))
        self.assertEqual((session.runs, items), ([], []))
        self.setUp()
        session, items = self.turn((12.0, 2))
        self.assertEqual(len(session.runs), 1)
        self.assertEqual({i["autopilot"] for i in items}, {"assistant"})
        self.setUp()
        session, items = self.turn((None, 0), answerer=(3, 2))
        self.assertEqual(len(session.runs), 1)


class OutsideReaders(ta.Fixture):
    """A conversation with a reader outside the company: the session gets no record read
    tool, only the outside party's own records' lines; a reply, an option or a proposed
    message that names another customer's order, another customer, or a price their own
    records do not state is never a draft to confirm: the reply becomes a question card that
    says why, a message is held back and named on the item."""

    def setUp(self) -> None:
        super().setUp()
        other = self.w.obj("Customer", {"name": "Northwind Soapworks Inc", "code": "C200",
                                        "erp_code": "C200", "email": "ap@northwind.example"})
        order = self.w.obj("SalesOrder", {"order_number": "SO-9931", "status": "Open",
                                          "order_date": "2026-10-20"})
        self.w.link("SalesOrder.ordered_by", order, other)
        self.identified.append(("order_number", "SO-9931", order, "order_number"))

    def live(self, ids, a, name="s.sqlite", queue="autopilot", on=True):
        # the store with its identifier rows (each order number names its record)
        if self.named_order and not any(o == self.named_order for _k, _n, o, _p in self.identified):
            self.identified.append(("order_number", "SO-1", self.named_order, "order_number"))
        if not (self.dir / name).exists():
            self.build()
        return LiveCycle.live(self, ids, a, name, queue, on)

    def body(self, items, kind="reply"):
        return next(json.loads(i["body"])["prepared"] for i in items
                    if json.loads(i["body"])["prepared"]["kind"] == kind)

    def test_an_outside_sender_gets_no_read_tools_and_only_their_own_lines(self):
        msg = self.message(84, "Where is my order SO-1?")
        session = Session(answer=final(reply="Your order SO-1 is open."))
        out, items = self.live([msg], assistant(session))
        self.assertEqual(session.reads, [False])
        self.assertIn("no record tools", session.runs[0][1])
        self.assertIn("Their own records", session.runs[0][1])
        self.assertNotIn("SO-9931", session.runs[0][1])
        self.assertEqual(self.body(items)["draft"]["body"], "Your order SO-1 is open.")
        self.assertNotIn("held_back", self.body(items))

    def test_a_reply_naming_another_customers_business_becomes_a_card_that_says_why(self):
        replies = {
            "another_partys_record": "SO-1 is open; SO-9931 ships first.",
            "another_partys_name": "We are busy with Northwind Soapworks this week.",
            "price_not_on_their_records": "The new price is $4.85 per kg.",
        }
        for i, (why, text) in enumerate(replies.items()):
            with self.subTest(why=why):
                self.setUp()
                msg = self.message(85 + i, "Where is my order SO-1?")
                out, items = self.live([msg], assistant(Session(answer=final(reply=text))))
                prepared = self.body(items)
                self.assertEqual(prepared["draft"]["body"], "", prepared)
                self.assertTrue(prepared["your_call"]["ask"].startswith("I held back the reply"))
                self.assertEqual(prepared["held_back"][0]["why"], why)
                self.assertNotIn(text, json.dumps(prepared["your_call"]))

    def test_an_option_naming_another_customer_is_dropped_and_their_own_stays(self):
        ask = {"question": "Ship Friday?", "options": [
            {"label": "Yes", "reply": "Yes, SO-1 ships Friday."},
            {"label": "After", "reply": "After SO-9931 for Northwind Soapworks."},
            {"label": "No", "reply": "No, SO-1 ships Monday."}]}
        out, items = self.live([self.message(88, "Can SO-1 ship Friday?")],
                               assistant(Session(answer=final(ask=ask))))
        self.assertEqual([o["label"] for o in self.body(items)["your_call"]["options"]],
                         ["Yes", "No"])

    def test_a_message_to_an_outside_reader_from_an_inside_thread_is_held_back(self):
        leak = {"system": "mail", "operation": "send_email", "target": {},
                "change": {"to": ["sales@vendor.example"], "subject": "Stock",
                           "body": "Northwind Soapworks needs SO-9931 filled first."},
                "reason": "Ask the supplier."}
        fine = {**leak, "change": {**leak["change"], "body": "Can you confirm stock for next week?"}}
        msg = self.message(89, "Please chase the supplier on stock.", sender=ta.APPROVER,
                           customer=False)
        session = Session(calls=[("plx.prepare_write", leak), ("plx.prepare_write", fine)],
                          answer=final(summary="Chase the supplier"))
        out, items = self.live([msg], assistant(session))
        self.assertEqual(session.reads, [True])             # every reader is ours: reads
        job = self.body(items, "job")
        self.assertEqual([p["arguments"]["change"]["body"] for p in job["jobs"]],
                         ["Can you confirm stock for next week?"])
        self.assertEqual(job["held_back"], [{"to": ["sales@vendor.example"],
                                             "why": "another_partys_record"}])
        self.assertIn("held back", job["summary"])

    def test_an_outside_text_that_cannot_be_checked_is_held(self):
        from operating import autopilot_outbound as ob
        self.assertEqual(ob.held(None, "Your order ships Friday.", ["x@outside.example"], ""),
                         "outbound_unchecked")
        self.assertEqual(ob.held(None, "Your order ships Friday.", ["chat:19:abc"], ""),
                         "outbound_unchecked")
        self.assertEqual(ob.held(None, "Your order ships Friday.", [], ""), "")

    def test_the_drafters_reply_checks_hold_for_the_assistant(self):
        cases = {"figure_not_in_material": "SO-1 ships on 2026-12-03.",
                 "nothing_useful": "Thanks!"}
        for i, (why, text) in enumerate(cases.items()):
            with self.subTest(why=why):
                self.setUp()
                out, items = self.live([self.message(90 + i, "Where is my order SO-1?")],
                                       assistant(Session(answer=final(reply=text))))
                self.assertEqual(items, [])
                self.assertEqual(out["skipped"].get(f"assistant:{why}"), 1, out)


class Looks(ta.Fixture):
    """The assistant's time-driven look: one per opted-in person per working hour (the day's
    start first), live and switched on only; it proposes changes and turns a question into a
    to-do (there is no message to answer)."""

    def test_one_look_per_person_per_working_hour_and_none_outside_them(self):
        from datetime import datetime, timezone
        from operating import autopilot_agent_io as io
        start = datetime(2026, 10, 20, 11, 5, tzinfo=timezone.utc)         # Tue 7 AM ET
        got = io.look_events(["a", "b", "a"], start)
        self.assertEqual([(e["owner_hint"], e["fields"]["look"]) for e in got],
                         [("a", "day start"), ("b", "day start")])
        self.assertEqual(got, io.look_events(["b", "a"], start.replace(minute=50)))
        later = io.look_events(["a"], start.replace(hour=15))
        self.assertEqual(later[0]["fields"]["look"], "hourly look")
        self.assertNotEqual(later[0]["event_id"], got[0]["event_id"])
        self.assertEqual(io.look_events(["a"], start.replace(hour=23)), [])     # 7 PM ET
        self.assertEqual(io.look_events(["a"], start.replace(day=24)), [])      # Saturday
        for n in (1, 10, 60):
            owners = [f"p{i}" for i in range(n)]
            self.assertEqual(len({e["event_id"] for e in io.look_events(owners, start)}), n)

    def cycle(self, session, on=True, gate=True, name="s.sqlite", changes=None):
        path = self.dir / name
        if not path.exists():
            self.w.build(path)
        st = pe._Store(path, threads=1)
        q = ap.Queue(self.dir / "autopilot" / ap.QUEUE_FILE)
        q.__dict__.setdefault("_draft_context", {})["assistant"] = assistant(session)
        g = ap.autopilot_engine.SwitchGate(None, {ta.OURS_OID: frozenset({"routine_update"})}) \
            if gate else None
        try:
            from operating import autopilot_agent_io as io
            looks = io.look_events
            with mock.patch.dict(os.environ, on if isinstance(on, dict) else {
                    ap.AGENT_ENV: "1" if on else "0", ap.AGENT_PEOPLE_ENV: ""}), \
                    mock.patch.object(io, "look_events", lambda o, now: looks(
                        o, now, changes=(lambda _o, _at: changes) if changes else None)):
                out = ap.run_cycle(st, q, [], people=ta.people(), environ=self.env,
                                   now=ta.NOW, gate=g)
            return out, [dict(r) for r in q.conn.execute(
                "SELECT * FROM items WHERE autopilot = 'assistant_look'")]
        finally:
            st.close()
            q.close()

    def test_looks_follow_the_people_list(self):
        session = Session(calls=[("plx.prepare_write", PO_DATE)], answer=final(summary="late"))
        for listed, n in (("x@y.example", 0), (ta.OURS, 1)):
            with self.subTest(listed=listed):
                self.setUp()
                out, items = self.cycle(session, changes=PO_CH, on={
                    ap.AGENT_ENV: "0", ap.AGENT_PEOPLE_ENV: listed})
                self.assertEqual(len(items), n, out)

    def test_a_look_proposes_changes_and_a_question_is_a_todo(self):
        ask = {"question": "Chase the late PO cmpo0001 with the supplier?", "options": []}
        session = Session(calls=[("plx.prepare_write", PO_DATE)],
                          answer=final(summary="PO 77 is late", ask=ask))
        out, items = self.cycle(session, changes=PO_CH)
        self.assertEqual(len(items), 1, out)
        self.assertTrue(session.runs[0][1].split("It is ", 1)[1].count("Nothing has just arrived"))
        body = json.loads(items[0]["body"])["prepared"]
        self.assertEqual(body["kind"], "job")
        self.assertEqual(body["jobs"], [{"tool": "plx.prepare_write", "arguments": PO_DATE}])
        self.assertEqual(body["todos"][-1]["what"], ask["question"])
        self.assertNotIn("your_call", body)
        self.assertEqual(aw.actions_of({"body": json.loads(items[0]["body"])}), [])
        # the same hour again: the look is kept once
        again, items = self.cycle(Session(calls=[("plx.prepare_write", PO_DATE)]),
                                  changes=PO_CH)
        self.assertEqual((again["events_new"], len(items)), (0, 1))

    def test_a_look_raises_at_most_two_things_changes_first(self):
        todos = [{"system": "sage", "record": f"IN-{i}", "what": f"Invoice shipment {i}"}
                 for i in range(5)]
        session = Session(calls=[("plx.prepare_write", PO_DATE)],
                          answer=final(summary="Day start", todos=todos))
        out, items = self.cycle(session, changes=[*PO_CH, *(
            {"record": f"IN-{i}", "line": f"shipment {i} shipped"} for i in range(5))])
        body = json.loads(items[0]["body"])["prepared"]
        self.assertEqual(len(body["jobs"]), 1)
        self.assertEqual([t["record"] for t in body["todos"]], ["IN-0"])
        self.assertIn("at most two things", session.runs[0][1])
        self.assertIn("Nothing on any other record", session.runs[0][1])

    def test_a_look_runs_no_session_without_a_computed_change_list(self):
        # no change list (the live cycle until one is computed): no session, no item
        session = Session(calls=[("plx.prepare_write", PO_DATE)])
        out, items = self.cycle(session)
        self.assertEqual((items, session.runs), ([], []))
        self.assertEqual(out["skipped"].get("assistant_look:nothing_changed"), 1)

    def test_a_look_keeps_only_items_naming_a_listed_record(self):
        # only items naming a listed record stand (a question naming one: a to-do)
        todos = [{"system": "sage", "record": "IN-1", "what": "Invoice shipment 1"},
                 {"system": "sage", "record": "IN-9", "what": "Invoice shipment 9"}]
        ask = {"question": "Chase PO cmpo0001 with the supplier?", "options": []}
        session = Session(calls=[("plx.prepare_write", TASK), ("plx.prepare_write", PO_DATE)],
                          answer=final(summary="Day start", todos=todos, ask=ask,
                                       reply="Thanks!"))
        out, items = self.cycle(session, changes=[*PO_CH, *IN_CH])
        body = json.loads(items[0]["body"])["prepared"]
        self.assertEqual(body["jobs"], [{"tool": "plx.prepare_write", "arguments": PO_DATE}])
        # IN-9 and TASK name no listed record; two things at most (LOOK_ITEMS), changes first
        self.assertEqual([t["what"] for t in body["todos"]], ["Invoice shipment 1"])
        self.assertNotIn("reply", {k for k, v in body.items() if v})

    def test_a_look_with_nothing_on_a_listed_record_is_nothing(self):
        from operating import autopilot_agent_io as io
        session = Session(calls=[("plx.prepare_write", TASK)])
        out, items = self.cycle(session, changes=IN_CH)
        self.assertEqual(items, [])
        self.assertEqual(out["skipped"].get("assistant_look:not_on_changes"), 1, out)
        # the list a look carries: readable rows only, at most LOOK_CHANGES, none repeated
        for n in (1, 10, 60):
            rows = [{"record": f"B-{i}", "line": f"batch {i} made"} for i in range(n)]
            got = io.look_changes(rows + [{"record": 5, "line": "x"}, "B-x"])
            self.assertEqual([r["record"] for r in got],
                             [f"B-{i}" for i in range(min(n, io.LOOK_CHANGES))])

    def test_no_look_switched_off_or_without_the_persons_opt_in(self):
        session = Session(calls=[("plx.prepare_write", PO_DATE)])
        out, items = self.cycle(session, on=False)
        self.assertEqual((items, session.runs), ([], []))
        out, items = self.cycle(session, gate=False, name="g.sqlite")
        self.assertEqual((items, session.runs), ([], []))


# computed change lists (``autopilot_agent_io.look_changes``) the look tests act on
PO_CH = [{"record": "cmpo0001", "line": "purchase order past its expected date"}]
IN_CH = [{"record": "IN-1", "line": "shipment 1 shipped, not invoiced"}]
PLAN_CH = [{"record": PLAN, "line": "plan has a new bucket of work for them"}]


class Invitations(ta.Fixture):
    def test_an_invitation_mail_names_the_calendar_event_to_answer_and_the_clash(self):
        from operating import autopilot_agent_io as io
        from operating.autopilot_handlers import Ctx
        box = ta.OURS_OID
        bind = 'meetings.calendar_events:{"id":"AAMkINV%d","mailbox":"%s"}'
        self.w.obj("CalendarEvent", {"subject": "Q3 review", "start": "2026-10-21T15:00:00Z",
                                     "end": "2026-10-21T16:00:00Z", "is_organizer": "false",
                                     "is_cancelled": "false"}, binding=bind % (1, box))
        self.w.obj("CalendarEvent", {"subject": "Plant walk", "start": "2026-10-21T15:30:00Z",
                                     "end": "2026-10-21T16:30:00Z", "is_organizer": "false",
                                     "is_cancelled": "false"}, binding=bind % (2, box))
        self.w.obj("CalendarEvent", {"subject": "Q3 review", "start": "2026-10-21T15:00:00Z",
                                     "end": "2026-10-21T16:00:00Z", "is_organizer": "false",
                                     "is_cancelled": "false"},
                   binding=bind % (3, "00000000-0000-4000-8000-0000000000ff"))
        path = self.dir / "s.sqlite"
        self.w.build(path)
        st = pe._Store(path, threads=1)
        self.addCleanup(st.close)
        ctx = Ctx(st, ta.NOW.date(), 10 ** 12, ["synth-tenant.example"])
        event = {"event_type": "message.received", "channel": "mail", "mailbox": box,
                 "subject": "Invitation: Q3 review", "at": "2026-10-19T15:00:00Z"}
        lines = io.invitation_lines(ctx, event)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("event_id AAMkINV1", lines[0])
        self.assertIn("Plant walk", lines[0])
        self.assertEqual(io.invitation_lines(ctx, {**event, "subject": "Other"}), [])
        self.assertEqual(io.invitation_lines(ctx, {**event, "at": "2026-10-22T15:00:00Z"}), [])


class RegistryRule(unittest.TestCase):
    def test_the_assistant_is_one_registry_entry_on_the_job_handler(self):
        reg = ap.load_registry(None)
        mine = [a for a in reg["autopilots"] if a.get("draft") == "agent"]
        self.assertEqual([(a["id"], a["handler"], a["on"]) for a in mine],
                         [("assistant", "job", "message.received"),
                          ("assistant_look", "job", "assistant.look")])
        bad = json.loads(json.dumps(reg))
        next(a for a in bad["autopilots"] if a["id"] == "assistant")["handler"] = "reply_prep"
        with self.assertRaises(ap.RegistryInvalid):
            ap.validate_registry(bad)
        self.assertFalse(ap.agent_mode({}))
        self.assertFalse(ap.agent_mode({ap.AGENT_ENV: "yes"}))
        self.assertTrue(ap.agent_mode({ap.AGENT_ENV: "1"}))


if __name__ == "__main__":
    unittest.main()
