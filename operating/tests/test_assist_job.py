"""Hand Assist a whole job (``operating.assist_job``) on Postgres (the migration 0082 table) with a
stand-in for the sessions.

Checked at the boundary: a request, its plan and every piece are append-only rows folded into one state;
no piece is lost or run twice at 1, 10 and 60 pieces (and no piece count caps the job); a person's Stop
ends the job: no session starts after it, the model processes end, the pieces already prepared stay;
every failure is a typed row (a plan that is not JSON, an empty plan, a piece that raises: the others
stand); the table refuses UPDATE and DELETE; a message to someone outside is held when no outbound check
can run (fail closed); records a plan named that no read shows are listed.
Evidence label: isolated-fixture (a real Postgres, no model, no business system)."""
from __future__ import annotations

import json
import threading
import time
import unittest

import psycopg

from operating import assist_job as aj
from operating.pg.migrate import MIGRATIONS_DIR
from operating.tests import pg_testdb

ANN = "ann-oid"


class Fake:
    """A stand-in for ``Sessions``: ``plan`` answers the given plan, ``piece`` a prepared piece."""

    def __init__(self, pieces=3, plan=None, why="", delay=0.0, boom=()):
        self.n, self.why, self.delay, self.boom = pieces, why, delay, set(boom)
        self.plan_given = plan
        self.started: list[int] = []
        self.lock = threading.Lock()

    def plan(self, text):
        if self.why:
            return self.why, None, {}
        got = self.plan_given or {"summary": "do it", "checklist": [{"text": "call", "piece": 1}], "found": f"{self.n}",
                                  "not_found": "", "nothing": "",
                                  "pieces": [{"title": f"p{i}", "instruction": f"i{i}", "records": [f"R{i}"]}
                                             for i in range(1, self.n + 1)]}
        return "", got, {"calls": [{"tool": "plx.sql", "arguments": {"q": "x"}, "failed": False}],
                         "reads_list": ["h1"], "unread": ["emails: failing"], "fallbacks": [], "usd": "0.5"}

    def piece(self, n, total, piece, text):
        with self.lock:
            self.started.append(n)
        if n in self.boom:
            raise RuntimeError("boom")
        time.sleep(self.delay)
        return {"status": "prepared", "why": "", "summary": f"s{n}", "reply": "", "ask": None, "proposals": [],
                "todos": [], "reads": [f"R{n}"], "tool_reads": [], "reads_traced": True, "usd": "0.1",
                "records_unread": aj.unverified(piece["records"], [], [])}


class JobCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)

    def setUp(self):
        self.pg = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.pg.close)
        self.pg.execute("DROP SCHEMA IF EXISTS assist CASCADE; CREATE SCHEMA assist")
        self.pg.execute((MIGRATIONS_DIR / "0082_assist_job_log.sql").read_text(encoding="utf-8"))

    def run_job(self, fake, **kw):
        jid = aj.request(self.pg, ANN, "chase every late PO from DSM")
        job = aj.Job(self.pg, jid, ANN, fake, **kw)
        return jid, job, job.run()


class Folding(JobCase):
    def test_a_job_is_the_fold_of_its_rows(self):
        jid, _job, status = self.run_job(Fake(3))
        self.assertEqual(status, "finished")
        got = aj.fold(aj.rows_of(self.pg, jid))
        self.assertEqual((got["status"], got["planned"], [p["n"] for p in got["pieces"]]), ("finished", 3, [1, 2, 3]))
        self.assertEqual(got["text"], "chase every late PO from DSM")
        self.assertEqual(got["plan_reads"], ["h1"])
        self.assertEqual(got["unread"], ["emails: failing"])
        self.assertEqual(got["usd"], 0.8)            # plan 0.5 + 3 x 0.1
        self.assertEqual(got["checklist"], [{"text": "call", "piece": 1}])

    def test_the_log_is_append_only(self):
        jid, _j, _s = self.run_job(Fake(1))
        for sql in ("UPDATE assist.job_log SET what = 'failed'", "DELETE FROM assist.job_log"):
            with self.assertRaises(psycopg.Error):
                self.pg.execute(sql)
        with self.assertRaises(ValueError):
            aj.append(self.pg, jid, ANN, "sent", {})

    def test_decisions_prepared_ids_and_edits_fold_onto_their_piece(self):
        jid, _j, _s = self.run_job(Fake(2))
        aj.append(self.pg, jid, ANN, "decided", {"piece": 2, "decision": "edited", "text": "mine",
                                                "edits": {"0": {"body": "new"}}})
        aj.append(self.pg, jid, ANN, "prepared", {"piece": 2, "index": 0, "prepared_id": "pw_1"})
        got = aj.fold(aj.rows_of(self.pg, jid))
        p2 = got["pieces"][1]
        self.assertEqual((p2["decision"], p2["edited_text"], p2["edits"], p2["prepared_ids"]),
                         ("edited", "mine", {"0": {"body": "new"}}, {0: "pw_1"}))
        self.assertEqual(got["pieces"][0]["decision"], "")

    def test_another_persons_jobs_are_not_listed(self):
        jid, _j, _s = self.run_job(Fake(1))
        self.assertEqual([j["job_id"] for j in aj.jobs_of(self.pg, [ANN], recent=None)], [jid])
        self.assertEqual(aj.jobs_of(self.pg, ["bob"], recent=None), [])

    def test_a_card_number_in_the_request_is_masked_and_an_empty_one_refused(self):
        jid = aj.request(self.pg, ANN, "pay with card 4111 1111 1111 1111 now")
        self.assertNotIn("4111 1111 1111 1111", aj.fold(aj.rows_of(self.pg, jid))["text"])
        with self.assertRaises(ValueError):
            aj.request(self.pg, ANN, "   ")


class Cardinality(JobCase):
    def test_no_piece_is_lost_or_repeated_and_nothing_caps_the_count(self):
        for n in (1, 10, 60):
            fake = Fake(n, delay=0.01)
            started = time.monotonic()
            jid, _j, status = self.run_job(fake, parallel=8)
            got = aj.fold(aj.rows_of(self.pg, jid))
            self.assertEqual(status, "finished", n)
            self.assertEqual(sorted(fake.started), list(range(1, n + 1)), n)
            self.assertEqual([p["n"] for p in got["pieces"]], list(range(1, n + 1)), n)
            self.assertLess(time.monotonic() - started, 20, n)


class Failures(JobCase):
    def test_a_plan_that_is_not_usable_is_a_typed_failure_with_words(self):
        for why in ("plan_unreadable", "no_session", "job_screened", "plan_screened"):
            jid, _j, status = self.run_job(Fake(why=why))
            got = aj.fold(aj.rows_of(self.pg, jid))
            self.assertEqual((status, got["status"], got["reason"]), ("failed", "failed", why))
            self.assertTrue(got["message"])

    def test_an_empty_plan_is_failed_unless_it_names_the_record_that_shows_it_done(self):
        empty = {"summary": "", "checklist": [], "found": "", "not_found": "", "nothing": "", "pieces": []}
        jid, _j, status = self.run_job(Fake(plan=empty))
        self.assertEqual((status, aj.fold(aj.rows_of(self.pg, jid))["reason"]), ("failed", "plan_empty"))
        done = {**empty, "nothing": "PO 90412 shows received in full (po://90412)"}
        jid, _j, status = self.run_job(Fake(plan=done))
        got = aj.fold(aj.rows_of(self.pg, jid))
        self.assertEqual((status, got["status"], got["nothing"]), ("finished", "finished", done["nothing"]))

    def test_a_piece_that_raises_fails_alone_and_says_so(self):
        jid, _j, status = self.run_job(Fake(3, boom=(2,)))
        got = aj.fold(aj.rows_of(self.pg, jid))
        self.assertEqual(status, "finished")
        self.assertEqual([(p["n"], p["status"]) for p in got["pieces"]],
                         [(1, "prepared"), (2, "failed"), (3, "prepared")])
        self.assertEqual(got["pieces"][1]["why"], "piece_crashed")

    def test_plan_of_drops_what_is_unusable_and_refuses_non_json(self):
        self.assertIsNone(aj.plan_of("not json"))
        self.assertIsNone(aj.plan_of('{"summary": "x"}'))
        got = aj.plan_of(json.dumps({"summary": "s", "pieces": [{"title": "a", "instruction": "do", "records": ["R1", 5, None]},
                                                                 {"title": "b", "instruction": "  "}, "junk"]}))
        self.assertEqual([(p["title"], p["records"]) for p in got["pieces"]], [("a", ["R1", "5"])])

    def test_records_the_plan_named_that_no_read_shows_are_listed(self):
        self.assertEqual(aj.unverified(["PO-1", "PO-2"], ["x://PO-1"], [{"arguments": {"q": "PO-9"}}]), ["PO-2"])
        self.assertEqual(aj.unverified(["PO-9"], [], [{"arguments": {"q": "where PO-9"}}]), [])
        # a record the session saw in what a tool returned is read, though no argument names it
        self.assertEqual(aj.unverified(["bc://po/1"], [], [{"arguments": {"q": "select"}}], ["row bc://po/1 late"]), [])


    def test_a_handle_written_escaped_in_one_place_and_plain_in_another_is_the_same_record(self):
        self.assertEqual(aj.unverified(["calendar-event://x/AAA%3D%3D@1"], [], [], ["event calendar-event://x/AAA==@1"]), [])


class Stopping(JobCase):
    def test_stop_before_any_session_logs_stopped_and_starts_none(self):
        jid = aj.request(self.pg, ANN, "job")
        aj.append(self.pg, jid, ANN, "stop", {})
        fake = Fake(3)
        job = aj.Job(self.pg, jid, ANN, fake)
        job.stopped.set()
        self.assertEqual(job.run(), "stopped")
        self.assertEqual(fake.started, [])
        self.assertEqual(aj.fold(aj.rows_of(self.pg, jid))["status"], "stopped")

    def test_a_stop_mid_job_ends_sessions_keeps_prepared_pieces_and_starts_no_more(self):
        fake = Fake(12, delay=0.2)
        ended: list[int] = []
        jid = aj.request(self.pg, ANN, "job")
        watch = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(watch.close)
        job = aj.Job(self.pg, jid, ANN, fake, parallel=2, stop_poll=0.05, end=lambda: ended.append(1) or 0)
        t = threading.Thread(target=lambda: self.assertEqual(job.run(watch), "stopped"))
        t.start()
        while len(fake.started) < 2:
            time.sleep(0.01)
        aj.append(self.pg, jid, ANN, "stop", {})
        t.join(20)
        self.assertFalse(t.is_alive())
        got = aj.fold(aj.rows_of(self.pg, jid))
        self.assertEqual(got["status"], "stopped")
        self.assertTrue(ended)
        self.assertLess(len(fake.started), 12)                       # no session started after the stop was seen
        self.assertEqual(len(got["pieces"]), 12)                      # every piece has a row: prepared or stopped
        self.assertTrue(any(p["status"] == "stopped" for p in got["pieces"]))
        self.assertTrue(all(p["status"] in ("prepared", "stopped") for p in got["pieces"]))

    def test_a_piece_ended_by_the_stop_says_stopped_not_what_the_killed_session_reported(self):
        class Killed(Fake):
            def piece(self, n, total, piece, text):
                self.job.stopped.set()           # the person's Stop arrives while this session runs
                return {"status": "failed", "why": "check_failed", "message": "The second check could not run"}
        fake = Killed(1)
        jid = aj.request(self.pg, ANN, "job")
        fake.job = job = aj.Job(self.pg, jid, ANN, fake)
        self.assertEqual(job.run(), "stopped")
        piece = aj.fold(aj.rows_of(self.pg, jid))["pieces"][0]
        self.assertEqual((piece["status"], piece["why"], piece["message"]), ("stopped", "stopped", ""))

    def test_descendants_finds_only_children_of_this_process(self):
        import subprocess
        child = subprocess.Popen(["sleep", "30"])
        try:
            import os
            self.assertIn(child.pid, aj.descendants(os.getpid()))
            self.assertNotIn(os.getpid(), aj.descendants(os.getpid()))
        finally:
            child.kill()
            child.wait()

    def test_watch_repeats_stop_kill_on_every_poll_and_logs_watcher_errors(self):
        class FlakyWatch:
            def __init__(self):
                self.calls = 0

            def execute(self, sql, params=()):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient db error")
                return self

            def fetchone(self):
                return (1,)

        jid = aj.request(self.pg, ANN, "job")
        kills: list[int] = []

        def end_kill():
            kills.append(1)
            if len(kills) == 2:
                raise OSError("transient kill error")
            if len(kills) >= 3:
                job._done.set()
            return 1

        job = aj.Job(self.pg, jid, ANN, Fake(1), stop_poll=0.01, end=end_kill)
        flaky = FlakyWatch()
        with self.assertLogs("operating.assist_job", level="WARNING") as logs:
            job.watch(flaky)
        self.assertTrue(job.stopped.is_set())
        self.assertGreaterEqual(len(kills), 3)
        self.assertEqual(flaky.calls, 2)             # stopped querying DB once Stop was seen, kept calling end()
        self.assertGreaterEqual(len(logs.output), 2) # logged both the DB poll error and the kill error


class Dispatcher(JobCase):
    def test_serve_starts_each_requested_job_once_and_logs_a_dead_worker(self):
        import sys
        import tempfile
        from pathlib import Path
        fresh = aj.request(self.pg, ANN, "a new job")
        dead = aj.request(self.pg, ANN, "a job whose worker died")
        aj.append(self.pg, dead, ANN, "started", {})
        done = aj.request(self.pg, ANN, "a finished job")
        aj.append(self.pg, done, ANN, "started", {})
        aj.append(self.pg, done, ANN, "finished", {})
        out = Path(tempfile.mkdtemp()) / "spawned.txt"
        script = f"import sys; open({str(out)!r}, 'a').write(sys.argv[sys.argv.index('--job') + 1] + chr(10))"
        self.assertEqual(aj.serve("s", "d", argv0=[sys.executable, "-c", script, "--"], poll=0.05, dsn=self.dsn), 0)
        self.assertEqual(out.read_text().split(), [fresh])
        got = aj.fold(aj.rows_of(self.pg, dead))
        self.assertEqual((got["status"], got["reason"]), ("failed", "worker_died"))
        self.assertEqual(aj.fold(aj.rows_of(self.pg, done))["status"], "finished")
        # the child here ended without logging anything: said, and the job is not started a second time
        gone = aj.fold(aj.rows_of(self.pg, fresh))
        self.assertEqual((gone["status"], gone["reason"]), ("failed", "worker_died"))

    def test_a_job_stopped_before_it_started_is_ended_not_run(self):
        jid = aj.request(self.pg, ANN, "job")
        aj.append(self.pg, jid, ANN, "stop", {})
        self.assertEqual(aj.run_job(jid, "s", "d", dsn=self.dsn), 0)
        got = aj.fold(aj.rows_of(self.pg, jid))
        self.assertEqual(got["status"], "stopped")
        self.assertNotIn("started", [r["what"] for r in aj.rows_of(self.pg, jid)])


class Outbound(unittest.TestCase):
    def test_a_message_to_someone_outside_is_held_when_no_check_can_run(self):
        from operating import autopilot_agent_io as io
        mail = {"tool": "plx.prepare_write", "arguments": {"system": "mail", "operation": "send_email", "target": {},
                                                           "change": {"to": ["buyer@supplier.example"], "subject": "PO",
                                                                      "body": "Where is PO 1?"}, "reason": "chase"}}
        kept, back = io.outbound_proposals([mail], {"event_type": "assistant.job"}, {"ann": ["ann@ours.example"]},
                                           "ann", "", [], lambda: None)
        self.assertEqual((kept, len(back)), ([], 1))


if __name__ == "__main__":
    unittest.main()
