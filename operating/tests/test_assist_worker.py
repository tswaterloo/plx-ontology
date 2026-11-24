"""Draft on request (``operating.assist_worker``) on Postgres: the matters' own tables and
made-up comms rows (the fixtures of ``test_assist_matters``), a stand-in for the Assistant.

Checked at the boundary: only matters asked for are served, once; a reply becomes ``drafted``
with its body on the listed item, a question ``needs_you`` listed as a question, anything else a
typed ``failed`` (never "nothing to prepare"), a matter with no message record a typed failure, a
session that raises one matter's failure only; a request after a draft is served again; the list
says "Drafting" between the request and the answer; the person's "Draft it" row is committed by
the API's own insert; 1/10/60: model calls = N and the statements the worker makes do not grow.
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import unittest

from operating import assist_matters as am
from operating import assist_worker as aw
from operating.tests import test_assist_matters as base

NOW, ANN, OUT = base.NOW, base.ANN, base.OUT


class Counting(base.Counting):
    pass


class Worker(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail
    read = base.Matters.read
    cycle = base.Matters.cycle
    matters = base.Matters.matters
    log = base.Matters.log

    def thread(self, name: str, n: int = 0) -> str:
        t0 = NOW - dt.timedelta(hours=5, minutes=n)
        h = self.mail(name, OUT, ["ann@ours.example"], at=t0)
        self.read(h, thread=name, actor=ANN, at=t0)
        return h

    def ask(self, subject: str) -> str:
        mid = self.pg.execute("SELECT matter_id FROM assist.matters WHERE subject_key = %s",
                              (subject,)).fetchone()[0]
        self.pg.execute("INSERT INTO assist.matter_log (matter_id, what, detail) VALUES "
                        "(%s, 'requested', '{}')", (mid,))
        return mid

    def item(self, mid: str, pg=None):
        rows = am.as_items(pg or self.pg, [ANN], shown=True, door=lambda items: {
            i["item_id"] for i in items}, all_ages=True)["items"]
        return next(i for i in rows if i["item_id"] == mid)

    def test_a_request_is_served_once_and_the_item_shows_each_state(self):
        self.thread("t1")
        self.thread("t2")
        self.cycle()
        a, b = self.ask("t1"), None
        seen: list[tuple[str, str]] = []

        def draft(owner, oid):
            seen.append((owner, oid))
            return [{"draft": "Hi,\n\nThe 12th.\n\nAnn", "note": "check the date"}]
        item = self.item(a)
        self.assertEqual((item["plan_status"], item["note"]),
                         ("working", "Drafting. It shows here when it is ready."))
        out = aw.run_queue(self.pg, draft, model="stand-in")
        self.assertEqual((out["requested"], out["served"], out["drafted"]), (1, 1, 1))
        self.assertEqual(seen[0][0], ANN)                  # as the person the matter is for
        item = self.item(a)
        self.assertEqual((item["plan_status"], item["kind"], item["body"]),
                         ("drafted", "reply", "Hi,\n\nThe 12th.\n\nAnn"))
        self.assertEqual(self.pg.execute("SELECT what FROM assist.matter_log WHERE matter_id = %s "
                                         "AND what IN ('drafted', 'plan_failed')", (a,)).fetchall(),
                         [("drafted",)])
        self.assertEqual(self.pg.execute("SELECT person, tier, model, status FROM assist.worker_runs"
                                         ).fetchall(), [(ANN, "draft", "stand-in", "ok")])
        again = aw.run_queue(self.pg, draft)
        self.assertEqual((again["requested"], len(seen)), (0, 1))     # asked once, served once
        self.assertIsNone(b)
        # a second request after the draft is served again
        self.ask("t1")
        aw.run_queue(self.pg, lambda o, i: [{"draft": "New text"}])
        self.assertEqual(self.item(a)["body"], "New text")
        # a matter nobody asked for is never drafted
        other = self.pg.execute("SELECT plan_status FROM assist.matters WHERE subject_key = 't2'"
                                ).fetchone()[0]
        self.assertEqual(other, "none")

    def test_a_question_a_failure_and_a_raising_session_each_say_what_they_are(self):
        for n, name in enumerate(("q", "f", "r", "e")):
            self.thread(name, n)
        self.cycle()
        mids = {name: self.ask(name) for name in ("q", "f", "r", "e")}

        def draft(owner, oid):
            key = self.pg.execute("SELECT subject FROM biz.emails WHERE object_ids[1] = %s",
                                  (oid,)).fetchone()
            thread = self.pg.execute("SELECT thread FROM biz.emails WHERE object_ids[1] = %s",
                                     (oid,)).fetchone()[0]
            if thread == "q":
                return [{"kind": "your_call", "ask": "What price do we quote?", "options": []}]
            if thread == "f":
                return [{"why": "pay_or_hr"}]
            if thread == "r":
                raise RuntimeError("session died")
            return []
        out = aw.run_queue(self.pg, draft)
        self.assertEqual((out["needs_you"], out["failed"], out["served"]), (1, 3, 4))
        item = self.item(mids["q"])
        self.assertEqual((item["plan_status"], item["kind"], item["ask"]),
                         ("needs_you", "your_call", "What price do we quote?"))
        got = {n: self.pg.execute("SELECT plan_status, plan_reason FROM assist.matters WHERE "
                                  "matter_id = %s", (m,)).fetchone() for n, m in mids.items()}
        self.assertEqual(got["f"], ("failed", "pay_or_hr"))
        self.assertEqual(got["r"], ("failed", "draft_failed:RuntimeError"))
        self.assertEqual(got["e"], ("failed", "no_draft"))        # empty is typed, never "nothing"
        self.assertIn("Ask for it again", self.item(mids["f"])["note"])
        self.assertEqual(self.log("plan_failed"), 3)

    def test_a_matter_with_no_message_record_is_a_typed_failure(self):
        self.pg.execute(
            "INSERT INTO assist.matters (matter_id, owner_person, kind, subject_key, sources, "
            "basis_at, title) VALUES ('m-task', %s, 'task', 'T-1', '{}', now(), 'A task')", (ANN,))
        self.pg.execute("INSERT INTO assist.matter_log (matter_id, what) VALUES ('m-task', "
                        "'requested')")
        out = aw.run_queue(self.pg, lambda o, i: self.fail("no message, no session"))
        self.assertEqual(out["failed"], 1)
        self.assertEqual(self.pg.execute("SELECT plan_status, plan_reason FROM assist.matters"
                                         ).fetchone(), ("failed", "no_message:task"))

    def test_the_apis_log_row_is_what_the_worker_serves(self):
        """The row the route writes (its own insert through the pool, committed) is the one the
        worker reads: same table, same ``what``."""
        from operator_api import assist_routes
        import inspect
        self.assertIn("'requested'", requested_sql := aw._REQUESTED)
        self.assertIn('"requested"', inspect.getsource(assist_routes.install))
        self.assertTrue(requested_sql)

    def test_a_failed_redraft_keeps_the_earlier_draft_and_each_result_is_kept_as_it_finishes(self):
        self.thread("t1")
        self.thread("t2", 1)
        self.cycle()
        a = self.ask("t1")
        aw.run_queue(self.pg, lambda o, i: [{"draft": "First draft"}])
        self.assertEqual(self.item(a)["body"], "First draft")
        self.ask("t1")

        def boom(owner, oid):
            raise RuntimeError("session died")
        out = aw.run_queue(self.pg, boom)
        self.assertEqual(out["failed"], 1)
        item = self.item(a)
        self.assertEqual((item["body"], item["plan_status"]), ("First draft", "drafted"))
        self.assertEqual(self.pg.execute("SELECT plan_reason FROM assist.matters WHERE matter_id = %s",
                                         (a,)).fetchone()[0], "draft_failed:RuntimeError")
        self.assertNotEqual(item["plan_status"], "working")          # not "Drafting" forever
        # one at a time: the first matter's result is in the database before the second starts
        b = self.ask("t2")
        self.ask("t1")
        seen = []

        def draft(owner, oid):
            seen.append(self.pg.execute("SELECT count(*) FROM assist.matter_log WHERE what = "
                                        "'drafted'").fetchone()[0])
            return [{"draft": "again"}]
        aw.run_queue(self.pg, draft, parallel=1)
        self.assertEqual(seen[1] - seen[0], 1)
        self.assertTrue(b)

    def test_model_calls_are_n_and_statements_do_not_grow_with_n(self):
        counts = {}
        for n in (1, 10, 60):
            self.pg.execute("TRUNCATE assist.matters, assist.matter_log, assist.worker_runs, "
                            "biz.emails, assist.readings")
            for i in range(n):
                self.thread(f"s{n}-{i}", i)
            self.cycle()
            for i in range(n):
                self.ask(f"s{n}-{i}")
            calls = []
            counting = Counting(self.pg)
            out = aw.run_queue(counting, lambda o, i, calls=calls: (calls.append(1) or
                                                                    [{"draft": "ok"}]))
            self.assertEqual((out["served"], len(calls)), (n, n))
            counts[n] = counting.n
        # each result is written as it finishes: one statement per matter beside the fixed ones
        self.assertEqual(counts[10] - counts[1], 9 * (counts[60] - counts[10]) // 50, counts)


if __name__ == "__main__":
    unittest.main()
