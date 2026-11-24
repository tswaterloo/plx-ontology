"""A person's own mail with no answer (``operating.autopilot_waits``, registry ``chase``):
due a working day after it was sent, read by the triage model as still owed, on the replay
fixture's mail. Evidence: isolated-fixture."""
from __future__ import annotations

import unittest
from datetime import timedelta

from operating import autopilot_replay as rp
from operating import proactive_engine as pe
from operating.tests import test_autopilot_replay as trp
from operating.tests.test_autopilot_replay import (DAY, OURS, OURS_OID, REP, SENT, mail,
                                                    people)
from operating.tests.test_autopilot_triage import StubTriage as _Stub

ASK = "Could you send the COA for lot 22 when you have it?"
NOTE = "Thanks, all received."


class Waits(trp.WaitingOnYou):
    def own(self, i: int, body: str, *, to=(REP,), hour: int = 9) -> str:
        return self.sent(mail(self.w, SENT, sender=OURS, to=to, body=body,
                              subject=f"Lot {i}", mailbox=OURS_OID, conversation=f"w-{i}",
                              n=90_000 + i, hour=hour), hour)

    def habit(self, k: int = 10, gap: timedelta = timedelta(hours=25), *, start: int = 12,
              answered: bool = False) -> None:
        """``k`` own mails before the window, each followed in its thread by the person's own
        next mail ``gap`` of working time later (``answered``: the recipient answered in
        between, so it was no chase)."""
        for j in range(k):
            t1 = DAY + timedelta(days=-start - 2 * j, hours=9)
            t2 = rp.answer_end(t1, gap)
            for n, (t, body) in enumerate(((t1, "Can you confirm the date?"),
                                           (t2, "Any update on this?"))):
                d = (t.date() - DAY.date()).days
                self.sent(mail(self.w, SENT + timedelta(days=d), sender=OURS, to=(REP,),
                               body=body, subject=f"Old {j}", mailbox=OURS_OID,
                               conversation=f"h-{j}", n=80_000 + 2 * j + n, hour=t.hour),
                          t.hour, d)
            if answered:
                d = (t1.date() - DAY.date()).days
                self.sent(mail(self.w, SENT + timedelta(days=d), sender=REP, to=(OURS,),
                               body="Working on it.", subject=f"Re: Old {j}",
                               mailbox=OURS_OID, conversation=f"h-{j}", n=85_000 + j,
                               hour=t1.hour + 1), t1.hour + 1, d)

    def answered(self, i: int, *, day: int, hour: int = 11) -> str:
        made = mail(self.w, SENT + timedelta(days=day), sender=REP, to=(OURS,), body="Attached.",
                    subject=f"Re: Lot {i}", mailbox=OURS_OID, conversation=f"w-{i}",
                    n=91_000 + i, hour=hour)
        return self.sent(made, hour, day)

    def replay_with(self, model, days: int = 3):
        from operating import chat_ask_model as cam
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        try:
            return rp.replay(st, people(), role=f"person:{OURS}", out=self.dir / "out",
                             since=DAY, until=DAY + timedelta(days=days), roles={},
                             config=rp.load_config(), environ={},
                             end=DAY + timedelta(days=10), probe=lambda: 64.0,
                             ask_judge=cam.Judge(model, batch=25) if model else None)
        finally:
            st.close()

    def chases(self):
        return [i for i in self.queue_file().items(None) if i["autopilot"] == "chase"]

    def test_cardinality_an_unanswered_ask_is_chased_once_an_answered_one_never(self):
        """Own mails asking something: those answered within the working day make nothing,
        the rest one item each, its draft to the recipient in the thread; a mail the model
        reads as owing nothing makes none. Each own mail read once. N=1/10/60."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.habit(gap=timedelta(hours=6))
                for i in range(n):
                    self.own(i, ASK if i % 3 != 2 else NOTE)
                    if i % 3 == 1:
                        self.answered(i, day=0)
                self.store()
                model = _Stub({ASK: ("reply", "the COA for lot 22"), NOTE: ("fyi", "")})
                out = self.replay_with(model)
                want = sum(1 for i in range(n) if i % 3 == 0)
                self.assertEqual(out["prepared_by_autopilot"].get("chase", 0), want)
                got = self.chases()
                self.assertEqual(len({i["event_id"] for i in got}), want)
                for item in got:
                    prepared = item["body"]["prepared"]
                    self.assertIn("the COA for lot 22", prepared["summary"])
                    self.assertEqual(prepared["draft"]["to"][0]["address"], REP)
                    self.assertTrue(prepared["draft"]["subject"].lower().startswith("re:"))
                own = [m for b in model.sent for m in b if m.get("own")]
                self.assertEqual(len(own), n - sum(1 for i in range(n) if i % 3 == 1))

    def test_no_model_an_excluded_recipient_or_an_answer_before_due_makes_none(self):
        from operating.tests.test_autopilot_replay import BOSS
        for case in ("no_model", "excluded", "answered_next_morning", "chased_himself"):
            with self.subTest(case=case):
                self.setUp()
                self.habit()
                if case == "excluded":
                    self.own(0, ASK, to=(BOSS,))
                else:
                    self.own(0, ASK, hour=15)
                if case == "answered_next_morning":
                    self.answered(0, day=1, hour=10)
                if case == "chased_himself":
                    self.sent(mail(self.w, SENT + timedelta(days=1), sender=OURS, to=(REP,),
                                   body="Any news?", subject="Re: Lot 0", mailbox=OURS_OID,
                                   conversation="w-0", n=92_000, hour=10), 10, 1)
                self.store()
                model = None if case == "no_model" else _Stub({ASK: ("reply", "the COA")})
                out = self.replay_with(model)
                self.assertEqual(out["prepared_by_autopilot"].get("chase", 0), 0, case)
                if model is not None and case == "excluded":
                    self.assertFalse(model.sent)

    def test_the_persons_own_delay_decides_and_no_habit_makes_none(self):
        """The same unanswered ask, answered two working days later: chased when the person
        chases after a working day, not when they chase after three, nor when their history
        holds too few chases (answered threads, or own lines minutes apart, are none)."""
        cases = {"chases_after_a_day": (dict(gap=timedelta(hours=25)), 1),
                 "chases_after_three_days": (dict(gap=timedelta(hours=72)), 0),
                 "too_few_chases": (dict(k=4), 0),
                 "answered_in_between": (dict(answered=True), 0),
                 "same_exchange": (dict(gap=timedelta(minutes=60)), 0),
                 "no_history": (dict(k=0), 0)}
        for case, (habit, want) in cases.items():
            with self.subTest(case=case):
                self.setUp()
                self.habit(**habit)
                self.own(0, ASK)
                self.answered(0, day=2, hour=16)
                self.store()
                out = self.replay_with(_Stub({ASK: ("reply", "the COA")}), days=4)
                self.assertEqual(out["prepared_by_autopilot"].get("chase", 0), want, case)

    def test_working_between_inverts_answer_end(self):
        from operating import autopilot_waits as aw
        for hours in (1, 6, 25, 72, 130):
            for d in range(7):
                a = DAY + timedelta(days=d, hours=14)
                b = rp.answer_end(a, timedelta(hours=hours))
                self.assertEqual(aw.working_between(a, b), timedelta(hours=hours), (hours, d))


class Switch(unittest.TestCase):
    def test_follow_ups_are_their_own_choice_off_for_everyone_until_turned_on(self):
        """A chase is prepared only for a person who turned follow-ups on themselves: its
        own workflow and choice, in no Draft file, and a person with every other workflow
        on (Draft and their own switches) still gets none."""
        import json as _json
        from operating import autopilot as ap, autopilot_engine as ae
        from operating import autopilot_write as aw, workflows as wf
        flow = wf.of_item("chase", {"subject": "PO 4410 order confirmation", "text": "x"})
        self.assertEqual(flow, "follow_ups")
        self.assertEqual(aw.draft_choice("follow_ups"), ("follow_ups",))
        others = {f for cid, _l, fl in aw.DRAFT_CHOICES if cid != "follow_ups" for f in fl}
        self.assertNotIn("follow_ups", others)
        draft = _json.loads((ap.REGISTRY_FILE.parent / "autopilot_draft.json").read_text())
        self.assertFalse(any("follow_ups" in v for v in draft["people"].values()))
        everything = frozenset(wf.names() - {"follow_ups"})
        gate = ae.SwitchGate(None, {"p1": everything})
        self.assertFalse(gate("p1", flow))
        self.assertTrue(gate("p1", "customer_order_intake"))


for _name in dir(trp.WaitingOnYou):
    if _name.startswith("test") and _name not in Waits.__dict__:
        setattr(Waits, _name, None)
del _name


if __name__ == "__main__":
    unittest.main()
