"""The prepared session at arrival (``operating.assist_prepared``): only the named people's matters that
have a message are served, newest activity first, once; the session is given the matter as it stands
(later messages too), the person's precedents and the sources that cannot be read now; the result is
the matter's draft or question with the handles read, the unread sources and the as-of moment; the
plan tier no longer plans those matters (a tier removed) but still plans their tasks and everyone
else's; a session that raises is that matter's typed failure; each result is kept as it is made.
Evidence label: isolated-fixture (no session was run on a real matter)."""
from __future__ import annotations

import datetime as dt
import json
import unittest

from operating import assist_plan as plan
from operating import assist_prepared as prep
from operating import assist_worker as aw
from operating.tests import test_assist_matters as base

NOW, ANN, BOB, OUT = base.NOW, base.ANN, base.BOB, base.OUT
REPLY = [{"draft": "Hello, shipped Monday.", "note": "", "warning": ""}]


def inputs_of(ms):
    return {m["matter_id"]: {"text": f"MATTER {m['title']} later: Done", "situation": "s"} for m in ms}


def nearest(ms, gs):
    return {m["matter_id"]: ([{"at": "2026-10-01", "counterpart": "Croda", "situation": "asks",
                                "act": "reply", "act_to": [], "why": "own"}], 4) for m in ms}


class Prepared(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail
    read = base.Matters.read
    obj = base.Matters.obj
    cycle = base.Matters.cycle

    def ask(self, thread, owner=ANN, hours=5):
        at = NOW - dt.timedelta(hours=hours)
        h = self.mail(thread, OUT, [f"{owner[2:]}@ours.example"], at=at, box=owner)
        self.read(h, thread=thread, actor=owner, at=at)

    def matters(self):
        """Every matter's subject, owner and plan status, for an assertion message."""
        return self.pg.execute("SELECT subject_key, owner_person, plan_status FROM assist.matters "
                               "ORDER BY subject_key").fetchall()

    def serve(self, session, people=(ANN,), **kw):
        return prep.serve(self.pg, list(people), session, inputs_of, nearest, model="stand-in", **kw)

    def test_the_people_are_named_by_the_environment_and_nobody_by_default(self):
        self.assertEqual(prep.prepared_for({}), [])
        self.assertEqual(prep.prepared_for({prep.ENV: " p-b ,p-a,,"}, ["p-c", ""]), ["p-a", "p-b", "p-c"])

    def test_the_named_peoples_message_matters_are_served_newest_first_once_with_reads_and_unread(self):
        self.pg.execute("CREATE TABLE biz.view_state (view text, failing_since timestamptz, "
                        "quarantined_until timestamptz)")
        self.pg.execute("INSERT INTO biz.view_state VALUES ('emails', now(), NULL), ('stock', NULL, NULL)")
        self.ask("t-old", hours=30)
        self.ask("t-new", hours=2)
        self.ask("t-bob", owner=BOB)
        self.cycle()
        seen = []

        def session(owner, oid, extra="", as_of=""):
            seen.append((owner, oid, extra, as_of))
            return REPLY
        out = self.serve(session)
        self.assertEqual((out["requested"], out["served"], out["drafted"]), (2, 2, 2))
        self.assertEqual({s[0] for s in seen}, {ANN})                       # Bob is not named
        self.assertEqual([s[1] for s in seen], sorted([s[1] for s in seen], key=lambda o: o != seen[0][1]))
        first = seen[0][2]
        self.assertIn("PREPARED SESSION", first)
        self.assertIn("later: Done", first)                                  # the matter as it stands
        self.assertIn("(1 of 4 past cases", first)
        self.assertIn("3 more past cases are not shown here", first)       # counted, not silent
        self.assertIn("These sources could not be read just now: emails: failing since", first)
        self.assertNotIn("stock", first.split("These sources")[1].split("\n")[0])
        rows = self.pg.execute("SELECT subject_key, plan_status, plan->'prepared'->'unread', "
                               "plan->'prepared'->>'reads_traced', plan->'prepared'->'draft'->>'body' "
                               "FROM assist.matters WHERE owner_person = %s ORDER BY subject_key", (ANN,)).fetchall()
        self.assertEqual([r[:2] for r in rows], [("t-new", "drafted"), ("t-old", "drafted")])
        self.assertTrue(rows[0][2][0].startswith("emails: failing since"))
        self.assertEqual(rows[0][4], "Hello, shipped Monday.")
        self.assertEqual(self.pg.execute("SELECT plan_status FROM assist.matters WHERE owner_person = %s",
                                         (BOB,)).fetchone()[0], "none")
        self.assertEqual(self.serve(session)["requested"], 0)               # once

    def test_the_handles_the_door_was_asked_about_are_kept_and_an_empty_trace_says_so(self):
        from operating.ontology_tools_policy import note_opened
        self.ask("t1")
        self.ask("t2", hours=9)
        self.cycle()

        calls = []

        def session(owner, oid, extra="", as_of=""):
            calls.append(oid)
            if len(calls) == 1:                     # only the first session's tools ran in this process
                note_opened(["mail://a", "record://b", "mail://a"])
            return REPLY
        self.serve(session)
        got = {r[0]: (r[1], r[2]) for r in self.pg.execute(
            "SELECT subject_key, plan->'prepared'->'reads', plan->'prepared'->>'reads_traced' "
            "FROM assist.matters ORDER BY subject_key")}
        self.assertEqual(sorted(got.values(), key=lambda v: len(v[0])),
                         [([], "false"), (["mail://a", "record://b"], "true")])   # untraced says so

    def test_a_replay_pins_every_read_to_the_matters_basis(self):
        self.ask("t1", hours=7)
        self.cycle()
        seen = []
        self.serve(lambda o, oid, extra="", as_of="": (seen.append((extra, as_of)), REPLY)[1], as_of_basis=True)
        basis = self.pg.execute("SELECT basis_at FROM assist.matters").fetchone()[0]
        self.assertEqual(seen[0][1], basis.isoformat())
        self.assertIn("nothing later exists", seen[0][0])
        self.assertEqual(self.pg.execute("SELECT plan->'prepared'->>'as_of' FROM assist.matters").fetchone()[0],
                         basis.isoformat())

    def test_a_raising_session_is_that_matters_typed_failure_and_the_rest_are_kept(self):
        for i in range(3):
            self.ask(f"t{i}", hours=2 + i)
        self.cycle()
        n = []

        def session(owner, oid, extra="", as_of=""):
            n.append(1)
            if len(n) == 2:
                raise RuntimeError("boom")
            return REPLY
        out = self.serve(session)
        self.assertEqual((out["served"], out.get("drafted"), out.get("failed")), (3, 2, 1))
        self.assertEqual(self.pg.execute("SELECT plan_reason FROM assist.matters WHERE plan_status = 'failed'"
                                         ).fetchone()[0], "draft_failed:RuntimeError")

    def test_the_plan_tier_leaves_their_message_matters_to_the_session_and_plans_the_rest(self):
        self.ask("t-ann")
        self.ask("t-bob", owner=BOB)
        self.obj("PT1", "PlannerTask", "plan:P", title="Book the truck", assignees=json.dumps([ANN]),
                 created_at="2026-11-22T10:00:00Z")
        self.cycle()
        calls = []
        out = plan.run_plans(self.pg, lambda p: (calls.append(p), json.dumps(
            {"acts": [{"act": "reply"}], "why": "w"}))[1], lambda m: m["title"],
            lambda ms: {m["matter_id"]: ([], 0) for m in ms}, model="x", prepared=[ANN])
        self.assertEqual(out["matters"], 2, self.matters())                  # Bob's mail, Ann's task
        self.assertEqual(sorted(r[0] for r in self.pg.execute(
            "SELECT subject_key FROM assist.matters WHERE plan_status = 'planned'").fetchall()),
            ["PT1", "t-bob"])
        self.assertEqual(self.pg.execute("SELECT plan_status FROM assist.matters WHERE subject_key = 't-ann'"
                                         ).fetchone()[0], "none")

    def test_a_database_with_no_freshness_table_says_so_not_none_unread(self):
        self.assertEqual(prep.unread_sources(self.pg), ["every source: the freshness table is not there"])


if __name__ == "__main__":
    unittest.main()
