"""Promises and waits (``operating.assist_waits``) on Postgres: a sent message whose reading holds
a ``waiting_on`` or a ``promise`` opens a matter for the sender; with no date stated the matter is
due after THEIR measured lag (no fixed number of days), a person with no history is asked at
once; a wait is out of the list until due; a wait closes when someone else writes in the thread,
a promise when the person does; a moved basis opens it again.
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import unittest

from operating import assist_matters as am
from operating.tests import test_assist_matters as base

NOW, ANN, BOB, OUT = base.NOW, base.ANN, base.BOB, base.OUT
REAL = dt.datetime.now(dt.timezone.utc)


class Waits(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail
    cycle = base.Matters.cycle

    def sent(self, thread, at, *, wait="", promise="", counterpart="Croda", due=None):
        h = self.mail(thread, "ann@ours.example", [OUT], at=at, box=ANN)
        self.pg.execute(
            "INSERT INTO assist.readings (source, message_key, handles, thread_key, said_at, read_ver, "
            "needs_act, act, act_class, summary, quote, status, sender_person, actor_basis, waiting_on, "
            "promise, counterpart, due) VALUES ('emails', %s, %s, %s, %s, 2, false, '', 'reply', 's', 'q', "
            "'ok', %s, '', %s, %s, %s, %s)", (h, [h], thread, at, ANN, wait, promise, counterpart, due))
        return h

    def reply(self, thread, sender, at, box):
        self.mail(thread, sender, ["ann@ours.example"], at=at, box=box)

    def row(self, kind):
        return self.pg.execute("SELECT state, done_reason, next_act, due_at, title, detail FROM "
                               "assist.matters WHERE kind = %s", (kind,)).fetchone()

    def listed(self, kind):
        got = am.as_items(self.pg, [ANN], shown=True, door=lambda items: {i["item_id"] for i in items},
                          all_ages=True)["items"]
        return [i for i in got if i["kind_of_matter"] == kind]

    def test_no_history_asks_the_person_at_once_instead_of_a_default(self):
        self.sent("t1", REAL - dt.timedelta(hours=2), wait="the COA from Croda")
        self.cycle()
        state, _why, act, due, title, detail = self.row("wait")
        self.assertEqual((state, act, due), ("open", "ask_person", None))
        self.assertEqual(title, "Waiting for the COA from Croda")
        self.assertIn("when should it be looked for", detail)
        self.assertEqual(len(self.listed("wait")), 1)               # shown now, not after a guess

    def test_the_due_time_is_the_persons_own_measured_lag_and_a_wait_hides_until_then(self):
        for i in range(3):                    # three earlier waits, each answered after 2 days
            t = REAL - dt.timedelta(days=30 + i * 3)
            self.sent(f"old{i}", t, wait="a quote")
            self.reply(f"old{i}", OUT, t + dt.timedelta(days=2), ANN)
        self.sent("new", REAL - dt.timedelta(hours=1), wait="the COA")
        self.cycle()
        rows = self.pg.execute("SELECT subject_key, state, due_at, next_act FROM assist.matters "
                               "WHERE kind = 'wait' AND subject_key = 'new'").fetchall()
        [(_, state, due, act)] = rows
        self.assertEqual((state, act), ("open", "new_message"))
        gap = (due - (REAL - dt.timedelta(hours=1))).total_seconds()
        self.assertAlmostEqual(gap, 2 * 86400, delta=60)             # the measured 2 days, no constant
        self.assertEqual(self.listed("wait"), [])                    # not due: out of the list
        self.pg.execute("UPDATE assist.matters SET due_at = now() - interval '1 minute' "
                        "WHERE subject_key = 'new'")                 # the day comes
        self.assertEqual([i["summary"] for i in self.listed("wait")], ["Waiting for the COA from Croda"])

    def test_a_stated_date_wins_and_an_answer_closes_the_wait_and_a_kept_promise_closes(self):
        self.sent("w", REAL - dt.timedelta(days=3), wait="the COA", due=(REAL - dt.timedelta(days=1)).date())
        self.sent("p", REAL - dt.timedelta(days=3), promise="send the specs tomorrow")
        self.cycle()
        self.assertEqual(self.row("wait")[0], "open")
        self.assertEqual(self.row("promise")[0], "open")
        self.reply("w", OUT, REAL - dt.timedelta(days=1), ANN)                   # they answered
        self.reply("p", "ann@ours.example", REAL - dt.timedelta(days=2), ANN)    # Ann wrote again
        self.cycle()
        self.assertEqual(self.row("wait")[:2], ("done", "answered"))
        self.assertEqual(self.row("promise")[:2], ("done", "kept"))
        self.assertEqual(self.listed("wait"), [])

    def test_self_wait_and_agent_messages_are_skipped_and_titles_never_double_from(self):
        from operating import assist_waits as aw
        # Title deduplication and trailing generic clause cleanup
        self.assertEqual(
            aw.build_wait_title("PO and deposit from Josh at KBM Resorts", "KBM Resorts (Josh)"),
            "Waiting for PO and deposit from Josh at KBM Resorts",
        )
        self.assertEqual(
            aw.build_wait_title("the COA, from them", "Croda"),
            "Waiting for the COA from Croda",
        )
        self.assertEqual(
            aw.build_wait_title("pricing update, from our side", ""),
            "Waiting for pricing update",
        )
        # Self-wait and agent self-notifications must never open a wait matter
        self.sent("self-w", REAL - dt.timedelta(hours=2), wait="signed quote", counterpart="Ann")
        h_agent = self.mail("agent-w", "ann@ours.example", [OUT], at=REAL - dt.timedelta(hours=2), box=ANN)
        self.pg.execute(
            "INSERT INTO assist.readings (source, message_key, handles, thread_key, said_at, read_ver, "
            "needs_act, act, act_class, summary, quote, status, sender_person, actor_basis, waiting_on, "
            "promise, counterpart, due) VALUES ('emails', %s, %s, 'agent-w', %s, 2, false, '', 'reply', "
            "'Agent reports sync complete', 'Workbench can now run', 'ok', %s, '', 'follow up', '', 'Croda', NULL)",
            (h_agent, [h_agent], REAL - dt.timedelta(hours=2), ANN),
        )
        self.cycle()
        self.assertIsNone(self.row("wait"))


if __name__ == "__main__":
    unittest.main()
