"""Fix 1 of ASSIST specs 8.5 (no model): the owner's act moves the matter; the plan queue runs
newest activity first and none is dropped or cut by age; a plan is made again only when the
matter's activity moves past what the plan saw; each plan is written the moment it is made, so a
run that is stopped keeps every plan it made; a matter nothing has happened on since its due time
is shown as "still open?" and stays listed and planned (last).
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import unittest
from unittest import mock

from operating import assist_activity as aa
from operating import assist_matters as am
from operating import assist_plan as plan
from operating.tests import test_assist_matters as base

NOW, ANN, BOB, OUT = base.NOW, base.ANN, base.BOB, base.OUT
ANSWER = json.dumps({"acts": [{"act": "reply"}], "why": "w"})


def nearest(ms):
    return {m["matter_id"]: ([], 0) for m in ms}


class Activity(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail
    read = base.Matters.read
    obj = base.Matters.obj
    cycle = base.Matters.cycle
    matters = base.Matters.matters

    def ask(self, thread, owner=ANN, at=None):
        at = at or NOW - dt.timedelta(hours=5)
        h = self.mail(thread, OUT, [f"{owner[2:]}@ours.example"], at=at, box=owner)
        self.read(h, thread=thread, actor=owner, at=at)
        return at

    def later(self, thread, sender_person, at, box=BOB, needs=False):
        """A later message of the thread, as another mailbox's copy holds it."""
        h = self.mail(thread, f"{sender_person[2:]}@ours.example", [OUT], at=at, box=box)
        self.read(h, thread=thread, actor=None, at=at, needs=needs, basis="")
        self.pg.execute("UPDATE assist.readings SET sender_person = %s WHERE message_key = %s",
                        (sender_person, h))

    def plan_run(self, call=None, **kw):
        calls = []

        def default(prompt):
            calls.append(prompt)
            return ANSWER
        out = plan.run_plans(self.pg, call or default, lambda m: m["title"], nearest,
                             model="stand-in", **kw)
        return out, calls

    # -------------------------------------------------- the owner's act moves the matter

    def test_the_owners_message_in_another_mailbox_copy_closes_the_matter(self):
        at = self.ask("t1")
        self.cycle()
        self.assertEqual(self.matters("conversation")[0][3], "open")
        self.later("t1", ANN, at + dt.timedelta(hours=1))      # stored only in Bob's mailbox
        out = self.cycle()
        self.assertEqual([(r[3], r[4]) for r in self.matters("conversation")], [("done", "owner_acted")])
        self.assertEqual(self.log("closed"), 1)

    def test_a_logged_act_of_the_owner_on_the_thread_closes_it_and_another_persons_does_not(self):
        at = self.ask("t1")
        self.ask("t2", owner=BOB)
        self.cycle()
        act = ("INSERT INTO assist.actions (action_key, person, at, system, kind, thread_key, "
               "attributed_by) VALUES (%s, %s, %s, 'mail', 'mail_sent', %s, 'sender')")
        self.pg.execute(act, ("a1", BOB, at + dt.timedelta(hours=1), "t1"))     # not the owner
        self.pg.execute(act, ("a2", ANN, at - dt.timedelta(hours=1), "t1"))     # before the basis
        self.pg.execute(act, ("a3", BOB, at + dt.timedelta(hours=1), "t2"))     # Bob, on his own
        self.cycle()
        by = {r[2]: r[3] for r in self.matters("conversation")}
        self.assertEqual(by, {"t1": "open", "t2": "done"})
        self.pg.execute(act, ("a4", ANN, at + dt.timedelta(hours=2), "t1"))
        self.pg.execute("UPDATE assist.actions SET not_human = 'bulk' WHERE action_key = 'a4'")
        self.cycle()
        self.assertEqual({r[2]: r[3] for r in self.matters("conversation")}["t1"], "open")  # not a person's act
        self.pg.execute("UPDATE assist.actions SET not_human = '' WHERE action_key = 'a4'")
        self.cycle()
        self.assertEqual({r[2]: r[3] for r in self.matters("conversation")}["t1"], "done")

    # -------------------------------------------------- replan only when activity moves

    def test_a_later_message_the_plan_never_saw_queues_the_matter_again_and_nothing_else_does(self):
        at = self.ask("t1")
        self.ask("t2")
        self.cycle()
        out, calls = self.plan_run()
        self.assertEqual((out["planned"], len(calls)), (2, 2))
        seen = self.pg.execute("SELECT count(*) FROM assist.matters WHERE plan_seen_at IS NOT NULL"
                               ).fetchone()[0]
        self.assertEqual(seen, 2)
        self.cycle()                                   # nothing moved: nothing queued, nothing to plan
        self.assertEqual(self.plan_run()[1], [])
        self.later("t1", "p-outside", at + dt.timedelta(hours=2), needs=False)   # a colleague's own note
        out = self.cycle()
        self.assertEqual(out["activity"]["requeued"], 1)
        self.assertEqual(self.log("basis_moved"), 1)
        again, calls = self.plan_run()
        self.assertEqual((again["matters"], len(calls)), (1, 1))
        self.cycle()
        self.assertEqual(self.plan_run()[1], [])            # planned again once, from what it now saw

    def test_a_plan_made_while_a_message_arrives_is_queued_again_never_kept_as_current(self):
        at = self.ask("t1")
        self.cycle()
        out, _ = self.plan_run(call=lambda p: (self.later("t1", "p-outside", at + dt.timedelta(hours=1)),
                                           self.cycle(), ANSWER)[2])
        self.assertEqual(out["planned"], 1)
        self.cycle()
        self.assertEqual(self.pg.execute("SELECT plan_status FROM assist.matters").fetchone()[0], "none")

    # -------------------------------------------------- the queue

    def test_the_queue_runs_newest_activity_first_shown_people_first_and_drops_nothing(self):
        for i, hours in enumerate((100, 3, 50, 1, 200)):
            self.ask(f"t{i}", at=NOW - dt.timedelta(hours=hours))
        self.ask("tb", owner=BOB, at=NOW - dt.timedelta(hours=400))
        self.cycle()
        out, calls = self.plan_run(shown=[BOB])
        self.assertEqual((out["matters"], out["waiting"], out["planned"]), (6, 6, 6))
        ages = self.pg.execute("SELECT subject_key FROM assist.worker_runs w JOIN assist.matters m "
                               "USING (matter_id) ORDER BY w.seq").fetchall()
        self.assertEqual([r[0] for r in ages], ["tb", "t3", "t1", "t2", "t0", "t4"])

    def test_a_task_nothing_has_happened_on_comes_last_and_is_counted_not_dropped(self):
        self.pg.execute("CREATE TABLE ontology.object_versions (ver bigint, object_id text, "
                        "recorded_at text)")
        for oid, created in (("PT-old", "2026-05-01T10:00:00Z"), ("PT-new", "2026-11-22T10:00:00Z")):
            self.obj(oid, "PlannerTask", "plan:P", title=oid, assignees=json.dumps([ANN]),
                     created_at=created, due_at="2026-05-10T10:00:00Z" if oid == "PT-old"
                     else "2026-11-30T10:00:00Z")
            self.pg.execute("INSERT INTO ontology.object_versions VALUES (1, %s, '2026-11-01T00:00:00Z')", (oid,))
        self.ask("t1", at=NOW - dt.timedelta(days=30))
        self.cycle()
        out, _ = self.plan_run(now=NOW)
        order = [r[0] for r in self.pg.execute(
            "SELECT m.subject_key FROM assist.worker_runs w JOIN assist.matters m USING (matter_id) "
            "ORDER BY w.seq").fetchall()]
        self.assertEqual(order, ["PT-new", "t1", "PT-old"])      # the capture's first version is not a touch
        self.assertEqual((out["waiting"], out["planned"], out["still_open"]), (3, 3, 2))
        # a later captured version of the old task is a touch: it moves up, and is not "still open"
        self.pg.execute("INSERT INTO ontology.object_versions VALUES (2, 'PT-old', '2026-11-20T00:00:00Z')")
        self.cycle()
        self.assertEqual(self.pg.execute("SELECT touched_at FROM assist.matters WHERE subject_key = "
                                         "'PT-old'").fetchone()[0], dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc))

    def test_still_open_is_asked_for_a_due_matter_with_nothing_since_and_the_list_keeps_it(self):
        self.obj("PT1", "PlannerTask", "plan:P", title="Old task", assignees=json.dumps([ANN]),
                 created_at="2026-05-01T10:00:00Z", due_at="2026-05-10T10:00:00Z")
        self.obj("PT2", "PlannerTask", "plan:P", title="Due later", assignees=json.dumps([ANN]),
                 created_at="2026-11-20T10:00:00Z", due_at="2026-12-10T10:00:00Z")
        self.cycle()
        got = am.as_items(self.pg, [ANN], shown=True, door=base.Matters.everything, now=NOW, all_ages=True)
        by = {i["summary"]: i for i in got["items"]}
        self.assertTrue(by["Old task"]["still_open"])
        self.assertTrue(by["Old task"]["note"].startswith("Still open? Nothing has happened on it since "
                                                          "it was due 2026-05-10."))
        self.assertFalse(by["Due later"]["still_open"])
        self.assertEqual(got["total"], 2)
        aged = am.as_items(self.pg, [ANN], shown=True, door=base.Matters.everything, now=NOW)
        self.assertEqual((len(aged["items"]), aged["older"], aged["total"]), (1, 1, 2))   # counted, reachable

    # -------------------------------------------------- a stopped run keeps its plans

    def test_a_run_killed_in_the_middle_keeps_every_plan_it_made(self):
        for i in range(5):
            self.ask(f"t{i}", at=NOW - dt.timedelta(hours=i + 1))
        self.cycle()
        n = []

        def call(prompt):
            n.append(1)
            if len(n) == 4:
                raise KeyboardInterrupt        # the run is stopped during the fourth call
            return ANSWER
        with self.assertRaises(KeyboardInterrupt):
            self.plan_run(call=call)
        self.assertEqual(self.pg.execute("SELECT count(*) FROM assist.matters WHERE plan_status = "
                                         "'planned'").fetchone()[0], 3)
        self.assertEqual(self.pg.execute("SELECT count(*) FROM assist.worker_runs").fetchone()[0], 3)
        out, calls = self.plan_run()                      # the next run plans the two that are left only
        self.assertEqual((out["matters"], len(calls)), (2, 2))

    def test_every_plan_is_one_write_and_the_reads_do_not_grow_with_the_count(self):
        counts = {}
        for n in (1, 10, 60):
            self.pg.execute("TRUNCATE assist.matters, assist.matter_log, assist.worker_runs, "
                            "biz.emails, assist.readings")
            for i in range(n):
                self.ask(f"s{n}-{i}", at=NOW - dt.timedelta(minutes=i + 1))
            self.cycle()
            counting = base.Counting(self.pg)
            out = plan.run_plans(counting, lambda p: ANSWER, lambda m: m["title"], nearest, model="x")
            self.assertEqual(out["planned"], n)
            counts[n] = counting.n - n               # one statement per plan written, the rest are reads
        self.assertEqual(len(set(counts.values())), 1, counts)

    def test_the_pilot_shows_the_queue_order_it_ran(self):
        for i in range(4):
            self.ask(f"t{i}", at=NOW - dt.timedelta(hours=10 * (i + 1)))
        self.cycle()
        out, _ = self.plan_run(pilot=3)
        self.assertEqual([o["touched_at"] for o in out["order"]],
                         sorted((o["touched_at"] for o in out["order"]), reverse=True))
        self.assertEqual((out["pilot"], out["matters"], out["waiting"]), (3, 3, 4))

    def test_the_prompt_shows_the_messages_after_the_asking_one(self):
        from operating import assist_readings as ar
        at = self.ask("t1")
        self.cycle()
        mid, handle = self.pg.execute("SELECT m.matter_id, r.handles[1] FROM assist.matters m JOIN "
                                      "assist.readings r ON r.thread_key = 't1'").fetchone()
        oid = self.pg.execute("SELECT object_ids[1] FROM biz.emails WHERE handle = %s", (handle,)).fetchone()[0]
        rows = {("emails", "t1"): [("k0", at, "x", handle, oid),
                                   ("k1", at + dt.timedelta(hours=2), "bob@ours.example", "h1", "o1")]}
        words = {(handle, oid): ("Please send the COA", ""), ("h1", "o1"): ("Done, sent it", "")}
        with mock.patch.object(ar, "_covered", lambda pg, env: 7), \
                mock.patch.object(ar, "thread_rows", lambda pg, s, page, covered: rows), \
                mock.patch.object(ar, "message_texts", lambda pg, pairs: words):
            text = plan.matter_inputs(self.pg, [{"matter_id": mid}])[mid]["text"]
        self.assertIn("after the asking message, oldest first:", text)
        self.assertIn("p-bob: Done, sent it", text)
        with mock.patch.object(plan, "WINDOW_CHARS", 20), mock.patch.object(ar, "_covered", lambda pg, env: 7), \
                mock.patch.object(ar, "thread_rows", lambda pg, s, page, covered: rows), \
                mock.patch.object(ar, "message_texts", lambda pg, pairs: words):
            tight = plan.matter_inputs(self.pg, [{"matter_id": mid}])[mid]["text"]
        self.assertIn("messages after the asking one are not shown here", tight)   # counted, not silent

    def log(self, what):
        return base.Matters.log(self, what)

    def test_still_open_is_one_definition(self):
        t = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        due = t + dt.timedelta(days=1)
        self.assertTrue(aa.still_open(due, None, t, NOW))
        self.assertTrue(aa.still_open(due, due, t, NOW))                       # touched at the due time only
        self.assertFalse(aa.still_open(due, due + dt.timedelta(hours=1), t, NOW))
        late = NOW - dt.timedelta(days=5)             # made after its due time, nothing since
        self.assertTrue(aa.still_open(due, late, late, NOW))
        self.assertFalse(aa.still_open(due, late + dt.timedelta(hours=1), late, NOW))
        self.assertFalse(aa.still_open(None, None, t, NOW))                    # no due time: nothing to be silent since
        self.assertFalse(aa.still_open(NOW + dt.timedelta(days=1), None, t, NOW))


if __name__ == "__main__":
    unittest.main()
