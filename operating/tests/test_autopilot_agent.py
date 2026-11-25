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
from operating import autopilot_agent_io as io
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

    def __init__(self, drop=(), answer=None, fail=None, fixed="") -> None:
        self.drop, self.answer, self.fail, self.fixed = set(drop), answer, fail, fixed
        self.prompts: list[str] = []

    def __call__(self, argv, *, input, **_kw):
        self.prompts.append(input)
        if self.fail is not None:
            raise self.fail
        ids = re.findall(r"^\[([RNQOPTB]\d*)\]", input, re.M)
        out = self.answer if self.answer is not None else json.dumps(
            {"items": [{"id": i, "keep": "no" if i in self.drop else "yes", "why": "-",
                        **({"fixed": self.fixed} if i == "R" and i in self.drop else {})}
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
    def test_excluded_people_and_pay_never_reach_the_model(self):
        session = Session()
        cases = {
            "not_cleared": (GATE, People(barred={"dev@plx.example"})),
            "not_cleared ": ({**MAIL, "cleared": False}, People()),
            "pay_or_hr": ({**GATE, "text": PAYSTUB}, People()),
        }
        for why, (event, people) in cases.items():
            with self.subTest(why):
                self.assertEqual(run(assistant(session), event, people), (None, why.strip()))
        self.assertEqual(session.runs, [])
        # what the session read or wrote is screened too: a hit drops the whole decision
        for looked, why in ((["{'requested_by': 'cos@plx.example'}"], "excluded_in_thread"),):
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
        # a written reply is never swapped for a card: the question beside it is its note
        ask = {"question": "Can PO 77 take Nov 20?",
               "options": [{"label": "Yes", "reply": "Yes, Nov 20 is fine."},
                           {"label": "No", "reply": "Nov 20 is too late for us."},
                           {"label": "", "reply": "dropped: no label"}]}
        got, _ = run(assistant(Session(answer=final(reply="Sure! [yes or no]", ask=ask))))
        self.assertEqual((got.reply, got.ask, got.note),
                         ("Sure! [yes or no]", None, "Can PO 77 take Nov 20?"))
        # a question alone is the model's own card: kept with its answers
        got, _ = run(assistant(Session(answer=final(ask=ask))))
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


class Sort:
    """The reply sort's ``claude -p`` stand-in: answers ``answer`` (a JSON text) or raises
    ``fail``; keeps every prompt it was shown."""

    def __init__(self, answer='{"reply": false, "what": "a newsletter"}', fail=None) -> None:
        self.answer, self.fail = answer, fail
        self.prompts: list[str] = []
        self.models: list[str] = []

    def __call__(self, argv, *, input, **_kw):
        self.prompts.append(input)
        self.models.append(argv[argv.index("--model") + 1])
        if self.fail is not None:
            raise self.fail
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": self.answer}),
                                           stderr="")


def sorted_by(session, sort, model="haiku") -> ag.Assistant:
    return ag.Assistant("opus", lookup=session, binary=BIN, home=HOME, per_cycle=100,
                        checker=ck.Checker(binary=BIN, home=HOME, call=Check()),
                        reply_screen=model, call=sort)


class ReplySort(unittest.TestCase):
    """The small model's sort before the session (``Assistant.reply_screen``, the open
    replies' drafts): only a plain false keeps the session from running."""

    def test_a_plain_false_runs_no_session_anything_else_lets_the_session_decide(self):
        session, sort = Session(), Sort()
        self.assertEqual(run(sorted_by(session, sort)), (None, "no_reply_wanted"))
        self.assertEqual((session.runs, sort.models), ([], ["haiku"]))
        self.assertIn(MAIL["text"], sort.prompts[0])
        self.assertIn("me@plx.example", sort.prompts[0])
        # a card number in the message is masked before the sort reads it
        card = {**ASKED, "text": ASKED["text"] + " Card 4111 1111 1111 1111 on file."}
        sort = Sort()
        run(sorted_by(Session(), sort), card)
        self.assertNotIn("4111 1111 1111 1111", sort.prompts[0])
        self.assertIn("Card", sort.prompts[0])
        for answer, fail in (('{"reply": true}', None), ('{"reply": "false"}', None),
                             ('{"what": "x"}', None), ("not json", None),
                             ('{"reply": false}', subprocess.TimeoutExpired("claude", 60)),
                             ('{"reply": false}', OSError("no binary"))):
            with self.subTest(answer=answer, fail=fail):
                session = Session()
                got, why = run(sorted_by(session, Sort(answer, fail)))
                self.assertEqual((why, got.reply), ("", "Yes, Nov 20 works for PO 77."))
                self.assertEqual(len(session.runs), 1)

    def test_no_sort_by_default_and_never_before_the_people_screens(self):
        session, sort = Session(), Sort()
        quiet = ag.Assistant("opus", lookup=session, binary=BIN, home=HOME, per_cycle=100,
                             checker=ck.Checker(binary=BIN, home=HOME, call=Check()), call=sort)
        self.assertEqual(run(quiet)[1], "")
        self.assertEqual((len(session.runs), sort.prompts), (1, []))
        session = Session()
        for event, people, why in (
                (GATE, People(barred={"dev@plx.example"}), "not_cleared"),
                ({**MAIL, "cleared": False}, People(), "not_cleared"),
                ({**GATE, "text": PAYSTUB}, People(), "pay_or_hr")):
            with self.subTest(why):
                self.assertEqual(run(sorted_by(session, sort), event, people), (None, why))
        self.assertEqual((session.runs, sort.prompts), ([], []))
        # no session for the person: no sort either (nothing would be drafted)
        self.assertEqual(run(sorted_by(None, sort)), (None, "agent_no_session"))
        self.assertEqual(sort.prompts, [])

    def test_a_time_look_is_never_sorted(self):
        session, sort = Session(calls=[("plx.prepare_write", TASK)]), Sort()
        got, why = sorted_by(session, sort).look("me", "Ana", People(),
                                                 as_of="2026-04-07T13:00:00+00:00",
                                                 changes=PLAN_CH)
        self.assertEqual((why, len(got.proposals), sort.prompts), ("", 1, []))


class Reads(Session):
    """A session whose reads come back only when it may read (``reads``); ``answers``
    the final answer of each run in turn."""

    def __init__(self, looked, answers) -> None:
        super().__init__(looked=looked)
        self.finals = list(answers)

    def run(self, owner, prompt, *, tools=(), job=False, agent=False, reads=True):
        assert agent and not job
        self.runs.append((owner, prompt))
        self.reads.append(reads)
        return self.finals[min(len(self.runs), len(self.finals)) - 1], \
            (self.looked if reads else [])


class ScreenedReads(unittest.TestCase):
    """A message's session that read something the screens bar (people exclusion,
    Freightcom) runs once more with no record tool, from the screened message and thread
    alone; nothing of the screened session (its reads, its decision) reaches that run or
    the check, and the retry is screened like any session (dev 11-21: 4 of 20 replies were
    lost to a record a session looked up, none to the message)."""

    BARRED = People(barred={"cos@plx.example"})
    SEEN = "{'requested_by': 'cos@plx.example', 'note': 'secret plan B'}"

    def test_a_screened_read_retries_from_the_thread_alone(self):
        for looked, why in (([self.SEEN], "excluded_in_thread"),
                            ([PAYSTUB], "pay_or_hr")):
            with self.subTest(why):
                session = Reads(looked, [final(reply="Plan B ships Friday, cos agreed."),
                                         final(reply="Thanks, noted: Nov 20 works.")])
                check = Check()
                got, got_why = run(assistant(session, check), people=self.BARRED)
                self.assertEqual((got_why, got.reply, got.looked),
                                 ("", "Thanks, noted: Nov 20 works.", []))
                self.assertEqual(session.reads, [True, False])
                again = session.runs[1][1]
                self.assertIn(ag.THREAD_ONLY.format(person="me@plx.example"), again)
                self.assertIn(MAIL["text"], again)
                self.assertNotIn("Plan B", again)
                # the check sees only the retry: one call, none of the screened session
                self.assertEqual(len(check.prompts), 1)
                for gone in ("secret plan B", "cos@plx.example", "Plan B ships",
                             PAYSTUB[:40]):
                    self.assertNotIn(gone, check.prompts[0])

    def test_the_reads_that_pass_are_used_in_the_retry_and_the_barred_one_is_named_unread(self):
        from operating import session_trace
        fine = "PO 77 is due Nov 20 per the record"
        session = Reads([fine, self.SEEN], [final(reply="Plan B ships Friday."),
                                            final(reply="PO 77 is due Nov 20.")])
        session_trace.begin()
        got, why = run(assistant(session, Check()), people=self.BARRED)
        trace = session_trace.end()
        self.assertEqual((why, got.reply, got.looked), ("", "PO 77 is due Nov 20.", [fine]))
        again = session.runs[1][1]
        self.assertIn(fine, again)                          # the read that stands is used, not lost
        self.assertNotIn("cos@plx.example", again)          # the barred one never reaches the retry
        self.assertNotIn("secret plan B", again)
        self.assertIn("1 other read(s) could not be used (excluded_in_thread)", again)
        self.assertEqual([h["why"] for h in trace["withheld"]], ["excluded_in_thread"])

    def test_the_retry_is_screened_and_runs_once(self):
        # the retry's own text names the barred person: dropped, no third run
        session = Reads([self.SEEN], [final(reply="ok"),
                                      final(reply="I asked cos@plx.example for it.")])
        self.assertEqual(run(assistant(session), people=self.BARRED),
                         (None, "excluded_in_thread"))
        self.assertEqual(session.reads, [True, False])
        # a screened message never reaches any session
        session = Reads([], [final(reply="ok")])
        self.assertEqual(run(assistant(session), {**ASKED, "text": "From: cos@plx.example"},
                             self.BARRED), (None, "excluded_in_thread"))
        self.assertEqual(session.runs, [])

    def test_a_look_is_not_retried(self):
        session = Reads([self.SEEN], [final(todos=[{"system": "Portal", "record": "PO 77",
                                                    "what": "Enter the date"}])])
        got = ag.Assistant("opus", lookup=session, binary=BIN, home=HOME, per_cycle=100,
                           checker=ck.Checker(binary=BIN, home=HOME, call=Check())).look(
            "me", "me@plx.example", self.BARRED,
            changes=[{"record": "PO 77", "line": "PO 77: date due"}])
        self.assertEqual(got, (None, "excluded_in_thread"))
        self.assertEqual(session.reads, [True])

    def test_an_address_without_a_name_part_names_nobody(self):
        from operating.autopilot_drafter import thread_screen

        class Directory:
            # as the captured directory decides: an address of our domain it does not know
            # is undecidable (withheld); another domain's is nobody of ours
            def identity_clear(self, who):
                return who == "me@plx.example" or not who.endswith("@plx.example")

        query = "{'sql': \"SELECT 1 FROM emails WHERE sender LIKE '%' || '@plx.example'\"}"
        self.assertIsNone(thread_screen(Directory(), query))
        for text in ("From: ghost@plx.example", "{'to': 'ghost@plx.example'}"):
            self.assertEqual(thread_screen(Directory(), text), "excluded_in_thread")


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
        self.assertEqual((why, got.reply, got.ask, got.warning),
                         ("", "Yes, Nov 20 works; the trucks are booked.", None,
                          ck.WARN_UNSUPPORTED))
        # the output step: a reviewed draft (Assist) keeps it with the warning; unreviewed
        # (Autopilot) it is kept the same way: never an empty "What should the reply say?"
        # card for a draft a check flagged (the person reads every card; Auto never sends it)
        from operating import autopilot_agent_io as io
        for review, want in ((True, ("Yes, Nov 20 works; the trucks are booked.", None,
                                     ck.WARN_UNSUPPORTED)),
                             (False, ("Yes, Nov 20 works; the trucks are booked.", None,
                                      ck.WARN_UNSUPPORTED))):
            with self.subTest(review=review):
                got, _ = run(assistant(reply, Check(drop={"R"})))
                inside = {**MAIL, "side": "internal"}
                ours = [a for a in [inside.get("sender"), *(inside.get("to") or ()),
                                    *(inside.get("cc") or ())] if a]
                out, _held, _why = io.agent_output(got, inside, "employee:A", "Nov 20?", {},
                                                   {"employee:A": ours}, None, ours[0],
                                                   review=review)
                self.assertEqual((out.reply, out.ask, out.warning), want)
        # a turn keeps the written reply too (flagged)
        self.assertEqual(run(assistant(reply, Check(drop={"R"})), TURN)[0].warning,
                         ck.WARN_UNSUPPORTED)
        # copied: no reply due, so not the person's question either
        self.assertEqual(run(assistant(reply, Check(drop={"R"})), {**MAIL, "asks": True}),
                         (None, "nothing_to_do"))
        # the check's repair takes out what it could not support: the reviewed draft is the
        # repair, no warning left; a "repair" that adds a figure is no repair (warning kept);
        # unreviewed it is still the open question
        inside = {**MAIL, "side": "internal"}
        ours = [a for a in [inside.get("sender"), *(inside.get("to") or ()),
                            *(inside.get("cc") or ())] if a]
        # a repair written with raw line breaks still parses (the verdict is not lost)
        raw = '{"items": [{"id": "R", "keep": "no", "why": "-", "fixed": "Yes.\n[trucks]"}]}'
        got, why = run(assistant(reply, Check(answer=raw)))
        self.assertEqual((why, got.fixed), ("", "Yes.\n[trucks]"))
        self.assertEqual(ck.fixed_of('{"items": [{"id": "R", "fixed": "We have shipped PO 77 yet."}]}',
                                     "We have not shipped PO 77 yet."), "")   # a cut "not"
        self.assertEqual(ck.fixed_of('{"items": [{"id": "R", "fixed": "We have not shipped [PO]."}]}',
                                     "We have not shipped PO 77 yet."), "We have not shipped [PO].")
        for fixed, review, want in (
                ("Yes, Nov 20 works; [trucks].", True, ("Yes, Nov 20 works; [trucks].", "")),
                ("Yes, Nov 20 works; trucks at 9.", True,
                 ("Yes, Nov 20 works; the trucks are booked.", ck.WARN_UNSUPPORTED)),
                # a "repair" that says something new (no figure in it) is no repair either
                ("Yes, Nov 20 works; the credit hold is released and your order shipped.",
                 True, ("Yes, Nov 20 works; the trucks are booked.", ck.WARN_UNSUPPORTED)),
                ("Yes, Nov 20 works; [trucks].", False,
                 ("Yes, Nov 20 works; the trucks are booked.", ck.WARN_UNSUPPORTED))):
            with self.subTest(fixed=fixed, review=review):
                got, _ = run(assistant(reply, Check(drop={"R"}, fixed=fixed)))
                out, _held, _why = io.agent_output(got, inside, "employee:A", "Nov 20?", {},
                                                   {"employee:A": ours}, None, ours[0],
                                                   review=review)
                self.assertEqual((out.reply, out.warning), want)
        # an outside reader, reviewed: the outbound check's finding (here: it could not run)
        # stays beside the kept draft, never a clean draft
        outside = {**MAIL, "side": "external"}
        got, _ = run(assistant(reply, Check()))
        out, _held, _why = io.agent_output(got, outside, "employee:A", "Nov 20?", {},
                                           {"employee:A": ["me@plx.example"]}, None,
                                           "me@plx.example", review=True)
        self.assertEqual(out.reply, "Yes, Nov 20 works; the trucks are booked.")
        self.assertTrue(out.warning.startswith("Check before sending: "), out.warning)
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

    def test_the_note_is_checked_as_the_reply_and_a_contradiction_is_unsupported(self):
        note = {"question": "Do you hold Matt to 5,000?", "options": []}
        session = Session(answer=final(reply="Hi, [yes or no] on 1,000.", ask=note))
        check = Check()
        got, _ = run(assistant(session, check))
        prompt = check.prompts[0]
        self.assertIn("[N] Note shown to the person beside the reply", prompt)
        self.assertIn("Do you hold Matt to 5,000?", prompt)
        self.assertIn("CONTRADICT", prompt)
        self.assertEqual(got.note, "Do you hold Matt to 5,000?")
        # unsupported: its repair stands (only taken out or blanked), else no note; the
        # reply is judged on its own
        for fixed, want in (("Do you hold Matt to [quantity]?", "Do you hold Matt to [quantity]?"),
                            ("Do you hold Matt to 2,000?", ""), ("", "")):
            with self.subTest(fixed=fixed):
                answer = json.dumps({"items": [{"id": "R", "keep": "yes"},
                                               {"id": "N", "keep": "no", "fixed": fixed}]})
                got, why = run(assistant(session, Check(answer=answer)))
                self.assertEqual((why, got.reply, got.note, got.warning),
                                 ("", "Hi, [yes or no] on 1,000.", want, ""))
        # a note the check does not name does not stand (fail closed)
        got, _ = run(assistant(session, Check(answer='{"items": [{"id": "R", "keep": "yes"}]}')))
        self.assertEqual((got.reply, got.note), ("Hi, [yes or no] on 1,000.", ""))

    def test_an_unsupported_answer_is_repaired_with_placeholders_not_dropped(self):
        card = Session(answer=final(ask=self.ASK))
        for fixed, label, want in (
                # repaired: the card keeps both ready replies
                ("[day] at 3 works for us.", "Another day", [
                    ("Yes", "Yes, Nov 20 works."), ("Another day", "[day] at 3 works for us.")]),
                # a "repair" that adds a figure is no repair: dropped, one left -> none
                ("Saturday at 4 works for us.", "Friday", []),
                # a label naming a figure the repaired reply no longer carries: dropped
                ("[day] at [time] works for us.", "Friday at 3", []),
                # nor a word it no longer says (the button would claim what the reply dropped)
                ("[day] works for us.", "Friday", [])):
            with self.subTest(fixed=fixed, label=label):
                ask = {**self.ASK, "options": [self.ASK["options"][0],
                                               {**self.ASK["options"][1], "label": label}]}
                answer = json.dumps({"items": [{"id": "Q", "keep": "yes"},
                                               {"id": "O1", "keep": "yes"},
                                               {"id": "O2", "keep": "no", "fixed": fixed}]})
                got, why = run(assistant(Session(answer=final(ask=ask)), Check(answer=answer)))
                self.assertEqual(why, "")
                self.assertEqual([(o["label"], o["reply"]) for o in got.ask["options"]], want)
        self.assertIn("[O1]..", ck.PROMPT)

    def test_a_card_shows_each_ready_answer_once(self):
        def card(options):
            got, why = run(assistant(Session(answer=final(ask={
                "question": "Can PO 77 take Nov 20?", "options": options}))))
            return [(o["label"], o["reply"]) for o in got.ask["options"]]
        yes, no = ("Yes", "Yes, Nov 20 works."), ("No", "Nov 20 is too late for us.")
        both = [{"label": l, "reply": r} for l, r in (yes, no)]
        # an answer that is only placeholders has nothing ready to send: no button
        self.assertEqual(card([both[0], {"label": "Other", "reply": "[your answer]."}, both[1]]),
                         [yes, no])
        # the same reply twice, or two answers under one label: one button each
        self.assertEqual(card([both[0], {"label": "Sure", "reply": " yes, nov 20 WORKS. "},
                               both[1]]), [yes, no])
        self.assertEqual(card([both[0], {"label": "yes ", "reply": "Fine by us."}, both[1]]),
                         [yes, no])
        # what is left after that is one answer: a guess, so none
        self.assertEqual(card([both[0], {**both[0], "label": "Sure"}]), [])

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
        the written reply stands; only their turn: the written reply stands too (the person
        reviews it); not due: neither (the supported change stays)."""
        from operating.autopilot_check import ASK_PLAIN
        session = Session(calls=[("plx.prepare_write", PO_DATE)],
                          answer=final(reply="Yes, Nov 20 works.",
                                       todos=[{"system": "Sage", "record": "PO 77",
                                               "what": "Move the date"}]))
        copied = {**MAIL, "to_me": False, "cc": ["me@plx.example"], "asks": True}
        for name, event, due in (
                ("put to them, asks", ASKED, "reply"),
                ("put to them, they answer this sender", TURN, "reply"),
                ("put to them, ask undecided by the rules", {**TURN, "sender_history": [0, 0],
                                                             "ask_check": "model"}, "reply"),
                ("copied, they wrote in the thread", {**copied, "own_posts": [30, 2]},
                 "reply"),
                ("in a group, they usually answer this sender there",
                 {**copied, "answerer": [6, 4]}, "reply"),
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
        # on a turn as when asked, the person's call keeps the answers the check kept (a card
        # with no ready answers is theirs to write); one kept answer is still none
        offered = Session(answer=final(ask={"question": "Which date suits you?", "options": [
            {"label": "Nov 20", "reply": "Nov 20 works."},
            {"label": "Nov 27", "reply": "Nov 27 works."}]}))
        for event in (TURN, ASKED):
            with self.subTest(due=event is TURN and "turn" or "asked"):
                self.assertEqual(len(run(assistant(offered), event)[0].ask["options"]), 2)
        self.assertEqual(run(assistant(offered, Check(drop={"O2"})), TURN)[0].ask,
                         {"question": "Which date suits you?", "options": []})
        # not due at all: no card
        self.assertEqual(run(assistant(offered), copied), (None, "nothing_to_do"))
        # the check's own say on whether a reply is due is not read
        ignored = Check(answer=json.dumps({"needed": "no", "items": [
            {"id": i, "keep": "yes"} for i in ("R", "P1", "T1")]}))
        self.assertEqual(run(assistant(session, ignored), ASKED)[0].reply, "Yes, Nov 20 works.")
        # nothing due and nothing else: nothing at all
        only_reply = Session(answer=final(reply="Thanks!"))
        self.assertEqual(run(assistant(only_reply), copied), (None, "nothing_to_do"))

    def test_screened_material_never_reaches_the_check(self):
        for looked, people, why in (
                (["{'requested_by': 'cos@plx.example'}"], People(barred={"cos@plx.example"}),
                 "excluded_in_thread"),):
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

    def test_an_outside_sender_reads_as_the_person_with_only_their_own_lines_given(self):
        msg = self.message(84, "Where is my order SO-1?")
        session = Session(answer=final(reply="Your order SO-1 is open."))
        out, items = self.live([msg], assistant(session))
        self.assertEqual(session.reads, [True])
        self.assertNotIn("no record tools", session.runs[0][1])
        self.assertIn("never another customer or supplier", session.runs[0][1])
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

    def test_a_card_left_without_two_answers_offers_its_blank_reply_instead(self):
        """A question whose answers do not leave two ready to send is the person's to write:
        the reply with the answer as a [placeholder] stands in (checked as any reply); with
        two answers left the card stays and the blank is never shown; no blank, or one
        that names another customer: the card as before."""
        blank = "Thanks for asking. SO-1 [ships Friday or not]."
        yes = {"label": "Yes", "reply": "Yes, SO-1 ships Friday."}
        leak = {"label": "After", "reply": "After SO-9931 for Northwind Soapworks."}
        no = {"label": "No", "reply": "No, SO-1 ships Monday."}
        for n, (options, b, card, draft) in enumerate((
                ([yes, leak], blank, None, blank),
                ([yes, leak, no], blank, ["Yes", "No"], ""),
                ([yes, leak], "", [], ""),
                ([yes, leak], "After SO-9931 for Northwind Soapworks: [ships].", [], ""))):
            with self.subTest(n=n):
                self.setUp()
                ask = {"question": "Ship Friday?", "options": options,
                       **({"blank": b} if b else {})}
                out, items = self.live([self.message(95 + n, "Can SO-1 ship Friday?")],
                                       assistant(Session(answer=final(ask=ask))))
                body = self.body(items)
                self.assertEqual(body["draft"]["body"] if body.get("draft") else "", draft)
                got = body.get("your_call")
                self.assertEqual([o["label"] for o in got["options"]] if got else None, card)
        self.assertIn('"blank"', ag.task('P', 'now', 'x'))
        self.assertNotIn('"blank"', ag.task('P', 'now', 'x', prepared=True))    # a finished item has no blank
        self.assertIn("[B]", ck.PROMPT)

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

    def test_a_long_change_list_is_one_counted_item_with_the_newest_few_and_no_session(self):
        # dev replays D-G: 27 looks with more than 8 listed records, none right
        rows = [{"record": f"IN-{i}", "line": f"Order IN-{i}: shipped, AR invoice not "
                 f"entered yet", "stage": "ar_invoice"} for i in range(39)]
        for n, sessions in ((8, 1), (9, 0), (10, 0), (39, 0), (60, 0)):
            with self.subTest(rows=n):
                self.setUp()
                session = Session(calls=[("plx.prepare_write", PO_DATE)],
                                  answer=final(summary="ship", todos=[
                                      {"system": "sage", "record": "IN-0", "what": "Invoice"}]))
                listed = (rows * 2)[:n] if n > 39 else rows[:n]
                out, items = self.cycle(session, changes=listed)
                self.assertEqual((len(items), len(session.runs)), (1, sessions), out)
                body = json.loads(items[0]["body"])["prepared"]
                if sessions:
                    continue
                self.assertEqual(body.get("jobs") or [], [])     # never a change to make
                self.assertEqual(len(body["todos"]), 1)
                what = body["todos"][0]["what"]
                # the whole count, past the 20 rows a look carries
                self.assertTrue(what.startswith(f"{n} records moved"), what)
                self.assertEqual(re.findall(r"\[(IN-\d+)\]", what), ["IN-0", "IN-1", "IN-2"])
                self.assertIn("; the newest 3: ", what)
                self.assertNotIn("more", what)
                self.assertEqual(body["todos"][0]["record"], "IN-0")

    def test_a_long_list_for_a_person_not_cleared_or_a_screened_line_makes_nothing(self):
        session = Session()
        many = [{"record": f"IN-{i}", "line": f"shipment {i} shipped"} for i in range(12)]
        a = assistant(session)
        self.assertEqual(a.look("me", "Ana", People(barred={"me"}), changes=many),
                         (None, "not_cleared"))
        # a row whose line carries pay or HR matter: the screens a session's item passes
        pay = [{"record": "PR-1", "line": "Paystub attached, gross pay, deductions, net pay"}, *many]
        got, why = a.look("me", "Ana", People(), changes=pay)
        self.assertIsNone(got)
        self.assertEqual(why, "pay_or_hr")
        self.assertEqual(session.runs, [])
        event = {"event_type": io.LOOK_EVENT, "fields": {"look": "day start", "changes": many}}
        # an odd or smaller total never shrinks the count below the rows carried
        for total in (None, True, "40", 3, -1, 12.5):
            with self.subTest(total=total):
                got, why = a.run({**event, "fields": {**event["fields"], "changes_total": total}},
                                 "me", "", "Ana", People())
                self.assertTrue(got.todos[0]["what"].startswith("12 records moved"))
        got, _why = a.run({**event, "fields": {**event["fields"], "changes_total": 40}},
                          "me", "", "Ana", People())
        self.assertTrue(got.todos[0]["what"].startswith("40 records moved"))
        self.assertEqual(session.runs, [])

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


class PreparedItem(unittest.TestCase):
    """The prepared session's finished item (``operating.assist_prepared``): real facts and real choices."""

    def test_a_prepared_task_has_no_blank_and_asks_for_real_choices_with_their_evidence(self):
        live, done = ag.task("P", "now", "x"), ag.task("P", "now", "x", prepared=True)
        self.assertIn("[placeholder]", live)
        self.assertNotIn('"blank"', done)
        self.assertIn("never a [placeholder]", done)
        self.assertIn('Say "nothing" only when a record you read shows', done)

    def test_a_prepared_questions_facts_and_labels_are_not_cut_and_an_approvals_options_stay(self):
        question = "Fact. " * 200                    # 1,200 characters of stated facts
        label = "Refund $650.88: invoice IN1 bills 60 units and the PO shows 40 shipped " * 3
        final = json.dumps({"ask": {"question": question, "options": [
            {"label": label, "reply": "ok"}, {"label": "Decline", "reply": "no"}]}})
        live = ag.parsed(final, [], message=False)
        self.assertEqual(live.ask["options"], [])                         # the live cycle: unchanged
        self.assertEqual(ag.parsed(json.dumps({"reply": "Status: X"}), [], message=False).reply, "")
        self.assertEqual(ag.parsed(json.dumps({"reply": "Status: X"}), [], message=False, prepared=True).reply,
                         "Status: X")                                     # a task's new message is its draft
        got = ag.parsed(final, [], message=False, prepared=True)
        self.assertEqual(got.ask["question"], question.strip())
        self.assertEqual([o["label"] for o in got.ask["options"]], [label.strip(), "Decline"])

    def test_the_second_check_judges_a_prepared_answer_as_a_choice_open_to_the_person(self):
        got = ag.Decision(ask={"question": "q", "options": [{"label": "Decline", "reply": "no"}]})
        self.assertIn("the message itself offers", ck.prompt(got, "m", [], "P", False))
        said = ck.prompt(got, "m", [], "P", False, prepared=True)
        self.assertIn("a choice open to P given the message and the records", said)
        self.assertNotIn("the message itself offers", said)


class Answers(Session):
    """A session that answers from a list (the last one repeats) and takes a prepared session's lead and tools."""

    def __init__(self, answers) -> None:
        super().__init__()
        self.said, self.lead, self.more = list(answers), [], []

    def run(self, owner, prompt, *, tools=(), job=False, agent=False, reads=True, lead="", extra_tools=()):
        self.lead.append(lead)
        self.more.append(list(extra_tools))
        self.answer = self.said.pop(0) if len(self.said) > 1 else self.said[0]
        return super().run(owner, prompt, tools=tools, job=job, agent=agent, reads=reads)


class PreparedBlanks(unittest.TestCase):
    def prep(self, session):
        return ag.Assistant("opus", lookup=session, binary=BIN, home=HOME, per_cycle=100,
                            checker=ck.Checker(binary=BIN, home=HOME, call=Check())).run(
            ASKED, "me", aj.described(ASKED, ASKED.get("text", "")), "me@plx.example", People(),
            prepared=aj.Prepared(lead="THE PERSON'S BLOCK", tools=["T"]))

    def test_a_reply_with_a_blank_slot_is_asked_again_once_and_the_lead_and_tools_go_to_each_session(self):
        question = final(ask={"question": "Which day?", "options": [
            {"label": "Fri 20 Nov (PO 77 is due Nov 20)", "reply": "Friday the 20th works for PO 77."},
            {"label": "Mon 23 Nov", "reply": "Monday the 23rd works for PO 77."}]})
        session = Answers([final(reply="Yes, [ship date] works for PO 77."), question])
        got, why = self.prep(session)
        self.assertEqual(why, "")
        self.assertEqual(len(session.runs), 2)
        self.assertIn("LEFT BLANKS IN SQUARE BRACKETS: [ship date]", session.runs[1][1])
        self.assertEqual(session.lead, ["THE PERSON'S BLOCK"] * 2)
        self.assertEqual(session.more, [["T"]] * 2)
        self.assertEqual(got.reply, "")
        self.assertEqual(len(got.ask["options"]), 2)

    def test_a_finished_reply_runs_one_session_and_a_second_blank_is_kept_with_its_warning(self):
        one = Answers([final(reply="Yes, Nov 20 works for PO 77.")])
        got, _ = self.prep(one)
        self.assertEqual((len(one.runs), got.reply), (1, "Yes, Nov 20 works for PO 77."))
        two = Answers([final(reply="Yes, [ship date] works for PO 77.")])
        got, _ = self.prep(two)
        self.assertEqual(len(two.runs), 2)                    # once more, never a loop

    def test_a_nothing_names_its_record_on_the_trace_and_a_read_the_screen_bars_is_redone_for_a_task_too(self):
        from operating import session_trace
        said = "Stephen's email to Ron Pope sent 17 Nov (emails/x)"
        session = Answers([final(reply="", nothing=said)])
        session_trace.begin()
        got, why = self.prep(session)
        self.assertEqual((got, why), (None, "nothing_to_do"))
        self.assertEqual(session_trace.end()["nothing"], said)
        # a record with no message (an approval, a task): a read the screen bars sends it to the retry
        # from the reads that stand, as a message's does
        task = {**ASKED, "event_type": "task.assigned"}
        barred = Answers([final(ask={"question": "Which?", "options": [
            {"label": "A", "reply": "do a"}, {"label": "B", "reply": "do b"}]})])
        barred.looked = ["{'requested_by': 'cos@plx.example'}"]
        a = ag.Assistant("opus", lookup=barred, binary=BIN, home=HOME, per_cycle=100,
                         checker=ck.Checker(binary=BIN, home=HOME, call=Check()))
        a.run(task, "me", aj.described(task, task.get("text", "")), "me@plx.example",
              People(barred={"cos@plx.example"}), prepared=aj.Prepared(lead="L"))
        self.assertEqual(len(barred.runs), 2)

    def test_a_reviewed_answer_a_check_flags_stays_with_what_to_check_once(self):
        from operating import autopilot_agent_io as io
        class Held:
            pass
        options = [{"label": "Decline", "reply": "No."}, {"label": "Yes", "reply": "Thanks, it is 4,242 units."}]
        m = ag.Decision  # unused
        from operating import autopilot_drafter as ad
        material = ad.Material(reader="", channel="mail", sender="s", subject="x", text="no figures here",
                               address="me@plx.example", relation="", account=[], looked_up=[])
        kept = io._sendable(options, material, None, [], "", [], True, "Sender")
        self.assertEqual(len(kept), 2)
        self.assertIn("check before sending", kept[1]["label"])
        self.assertEqual(kept[1]["label"].count("check before sending"), 1)
        again = io._sendable(kept, material, None, [], "", [], True, "Sender")
        self.assertEqual(again[1]["label"], kept[1]["label"])
        self.assertEqual(len(io._sendable(options, material, None, [], "", []))
                         , 1)                              # the live cycle still drops it
