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
        kw.setdefault("screen", lambda text: None)
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
        self.assertEqual(out["matters"], 1, self.matters())                  # Bob's mail only
        self.assertEqual(sorted(r[0] for r in self.pg.execute(
            "SELECT subject_key FROM assist.matters WHERE plan_status = 'planned'").fetchall()), ["t-bob"])
        # Ann's mail and her Planner task (a typed record of its own) both wait for the session
        self.assertEqual(sorted(r[0] for r in self.pg.execute(
            "SELECT subject_key FROM assist.matters WHERE owner_person = %s AND plan_status = 'none'",
            (ANN,)).fetchall()), ["PT1", "t-ann"])

    def test_a_precedent_that_fails_its_own_screen_is_left_out_and_said_never_the_matter_refused(self):
        self.ask("t1")
        self.cycle()
        cases = [{"at": f"2026-10-0{i}", "counterpart": "Croda", "situation": "asks", "act": "reply",
                  "act_to": [], "why": "own", "act_text": text}
                 for i, text in ((1, "fine words"), (2, "payroll for ann@excluded.example"), (3, "more fine words"))]
        seen = []

        def session(owner, oid, extra="", as_of=""):
            seen.append(extra)
            return REPLY
        out = prep.serve(self.pg, [ANN], session, inputs_of,
                         lambda ms, gs: {m["matter_id"]: (cases, 3) for m in ms}, model="m",
                         screen=lambda t: "excluded_in_thread" if "excluded.example" in t else None)
        self.assertEqual((out["served"], out["drafted"]), (1, 1))
        self.assertNotIn("excluded.example", seen[0])                        # never reaches the model
        self.assertIn("fine words", seen[0])
        self.assertIn("more fine words", seen[0])
        self.assertIn("(2 of 3 past cases", seen[0])
        self.assertIn("1 past cases are left out", seen[0])
        left = self.pg.execute("SELECT plan->'prepared'->'precedents_left_out' FROM assist.matters").fetchone()[0]
        self.assertEqual(left, [{"at": "2026-10-02", "why": "excluded_in_thread"}])

    def test_one_ten_and_sixty_precedents_none_lost_none_repeated_and_the_window_is_the_only_bound(self):
        matter = {"owner_person": ANN}
        for n in (1, 10, 60):
            cases = [{"at": f"2026-01-{i + 1:02d}", "counterpart": "C", "situation": "s", "act": "reply",
                      "act_to": [], "why": "own", "act_text": f"case-{i:03d} " + "w" * 50} for i in range(n)]
            bad = {f"case-{i:03d}" for i in range(0, n, 5)}
            asked = []

            def screen(text):
                asked.append(text)
                return "excluded_in_thread" if any(b in text for b in bad) else None
            text, left = prep.extra_of(matter, {"text": "m"}, cases, n, when="now", unread=[], screen=screen)
            shown = [c for c in range(n) if f"case-{c:03d}" in text]
            self.assertEqual(len(left), len(bad), n)                           # every failing one reported
            self.assertEqual(sorted(shown), [i for i in range(n) if f"case-{i:03d}" not in bad])
            self.assertEqual(len(asked), n)                                      # each screened once
            self.assertEqual(len(shown) + len(left), n)
        # a window that holds only some: the rest are counted, never silently dropped
        plan.WINDOW_CHARS, was = 1200, plan.WINDOW_CHARS
        try:
            text, left = prep.extra_of(matter, {"text": "m"}, cases, 60, when="now", unread=[],
                                       screen=lambda t: None)
        finally:
            plan.WINDOW_CHARS = was
        self.assertRegex(text, r"\d+ more past cases are not shown here")

    def test_the_persons_own_notes_reach_the_session_whole_and_a_replay_reads_none(self):
        """W1/W2 (owner 11-24): every note of theirs, newest first, in the session's input; a
        person with none gets no block; a replay as of the past (--as-of-basis) reads none
        (today's notes would leak the future)."""
        matter = {"owner_person": ANN}
        notes = [f"note {i:02d} " + "w" * 40 for i in range(60)]
        text, _ = prep.extra_of(matter, {"text": "m"}, [], 0, when="now", unread=[],
                                screen=lambda t: None, notes=notes)
        self.assertEqual([n for n in notes if n in text], notes)       # N=60: none lost
        self.assertIn("private to them", text)
        bare, _ = prep.extra_of(matter, {"text": "m"}, [], 0, when="now", unread=[],
                                screen=lambda t: None)
        self.assertNotIn("TOLD THE WORKBENCH", bare)

    def test_the_matters_own_words_failing_the_screen_is_that_matters_typed_failure_with_no_session(self):
        self.ask("t1")
        self.cycle()
        ran = []
        out = self.serve(lambda *a, **k: ran.append(1) or REPLY, screen=lambda t: "pay_or_hr")
        self.assertEqual((out["failed"], ran), (1, []))
        self.assertEqual(self.pg.execute("SELECT plan_reason FROM assist.matters").fetchone()[0], "pay_or_hr")

    def test_a_task_with_no_message_is_served_from_its_own_record_with_its_cost_and_tool_reads(self):
        from operating import session_trace
        self.obj("PT1", "PlannerTask", "plan:P", title="Book the truck", assignees=json.dumps([ANN]),
                 created_at="2026-11-22T10:00:00Z")
        self.cycle()
        seen = []

        def session(owner, oid, extra="", as_of=""):
            seen.append(oid)
            session_trace.note("0.1234", [{"tool": "plx.sql", "arguments": {"q": "select 1"}, "failed": False}])
            return [{"kind": "your_call", "ask": "Which truck?", "options": []}]
        out = self.serve(session)
        self.assertEqual((out["served"], out["needs_you"], seen), (1, 1, ["PT1"]))
        row = self.pg.execute("SELECT plan->'prepared'->'tool_reads', plan->'prepared'->>'reads_traced', "
                              "plan->'prepared'->>'usd' FROM assist.matters").fetchone()
        self.assertEqual(row[0], [{"tool": "plx.sql", "arguments": {"q": "select 1"}, "failed": False}])
        self.assertEqual((row[1], row[2]), ("true", None))                   # the cost is the run row's
        self.assertEqual(self.pg.execute("SELECT tier, usd, usd_basis FROM assist.worker_runs").fetchall(),
                         [("draft", __import__("decimal").Decimal("0.1234"), "reported")])

    def test_a_mission_control_question_is_placed_on_its_asking_event_and_an_unplaced_one_is_left_out(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        state = Path(tempfile.mkdtemp())
        (state / "mc-capture").mkdir()
        with sqlite3.connect(state / "mc-capture" / "capture.sqlite") as c:
            c.execute("CREATE TABLE versions (kind TEXT, path TEXT, payload TEXT, superseded_at TEXT)")
            for path, kind, q in (("live/task_events/T1~7", "question.asked", "q_1"),
                                  ("live/task_events/T1~8", "question.answered", "q_2")):
                c.execute("INSERT INTO versions VALUES ('mission_control', ?, ?, NULL)",
                          (path, json.dumps({"row": {"kind": kind, "payload": {"questionId": q}}})))
        key = '{"event_id":"T1~7","scope":"live"}'
        self.pg.execute("INSERT INTO ontology.objects VALUES (%s, 'MCTaskEvent', 'mc:x', "
                        "'2026-11-20T10:00:00Z', NULL, %s)", ("MCTaskEvent:mission_control.task_events:" + key, key))
        self.assertEqual(prep.question_oids(self.pg, state, ["q_1", "q_2", "q_9"]),
                         {"q_1": "MCTaskEvent:mission_control.task_events:" + key})
        self.assertEqual(prep.question_oids(self.pg, None, ["q_1"]), {})      # no capture to read: none placed

    def test_a_database_with_no_freshness_table_says_so_not_none_unread(self):
        self.assertEqual(prep.unread_sources(self.pg), ["every source: the freshness table is not there"])


if __name__ == "__main__":
    unittest.main()
