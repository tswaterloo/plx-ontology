"""Scheduled reports and monitors (operator_api/insights_runs.py): a saved insight runs by
itself as its owner only (an identity resolved at each run; refused: nothing read), keeps
counts and keyed digests only, comes to its owner on Home by its notify rule, and runs once
across processes. isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import unittest

from operator_api import insights as ins
from operator_api import insights_runs as ir
from operator_api import models as m
from operator_api.errors import UserFacing
from operator_api.tests.test_memory import Guard
from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

ANA = "aaaaaaaa-0000-4000-8000-0000000000aa"
BEN = "aaaaaaaa-0000-4000-8000-0000000000bb"
UTC = dt.timezone.utc


def at(text: str) -> dt.datetime:
    """A business-time (Eastern) moment as UTC."""
    from zoneinfo import ZoneInfo
    return dt.datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("America/New_York")) \
        .astimezone(UTC)


class Due(unittest.TestCase):
    def test_when_each_schedule_runs(self):
        thu_8am, thu_6am = at("2026-11-19T08:00"), at("2026-11-19T06:00")
        self.assertTrue(ir.due("hourly", None, thu_8am))
        self.assertFalse(ir.due("hourly", thu_8am - dt.timedelta(minutes=30), thu_8am))
        self.assertTrue(ir.due("hourly", thu_8am - dt.timedelta(minutes=61), thu_8am))
        # daily: once a day from 7 AM business time
        self.assertTrue(ir.due("daily", at("2026-11-18T07:05"), thu_8am))
        self.assertFalse(ir.due("daily", at("2026-11-19T07:01"), thu_8am))
        self.assertFalse(ir.due("daily", at("2026-11-18T07:05"), thu_6am))   # not yet 7
        # weekly: from Monday 7 AM
        self.assertFalse(ir.due("weekly", at("2026-11-16T07:30"), thu_8am))
        self.assertTrue(ir.due("weekly", at("2026-11-13T09:00"), thu_8am))
        for never in ("", "monthly", None):
            self.assertFalse(ir.due(never, None, thu_8am))

    def test_only_a_single_employee_key_runs(self):
        self.assertEqual(ir.owner_oid(f"employee:{ANA}"), ANA)
        for key in ("plx:staff:ana", f"employee:{ANA},plx:staff:ana", "", "employee:"):
            self.assertIsNone(ir.owner_oid(key))

    def test_the_home_line(self):
        self.assertEqual(ir.said("new_rows", 5, 2), "2 new rows since it last ran (5 rows now).")
        self.assertEqual(ir.said("rows", 1, 0), "1 row now.")
        self.assertEqual(ir.said("every_run", 0, 0), "No rows now.")


class Worker:
    """The reads worker: records who each read ran as."""

    def __init__(self, rows, delay=0.0):
        self.rows, self.delay, self.as_who = rows, delay, []

    def run(self, reader, job, timeout=0):
        view = type("V", (), {})()
        self.as_who.append(sorted(reader.session.principals))
        if self.delay:
            time.sleep(self.delay)

        def call(tool, args):
            rows = self.rows() if callable(self.rows) else self.rows
            return {"columns": ["po", "days_late"], "truncated": False,
                    "rows": [{"cells": list(r)} for r in rows]}
        view._call = call
        return job(view)


def session_for(oid, exp):
    return type("S", (), {"principals": frozenset({f"employee:{oid}"}), "view_as": False,
                          "expires_at": exp})()


@unittest.skipIf(psycopg is None, "psycopg not installed")
class Runs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from operating.pg import migrate
        from operator_api import memory as mem
        cls.db, cls.dsn = pg_testdb.create_db()
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 3})
        cls.memory = mem.Memory.for_dsn(cls.dsn, sync=True)
        cls.memory.guard = lambda: Guard()
        cls.memory.names = lambda: frozenset({"quinn harrow", "quinn", "harrow"})

    @classmethod
    def tearDownClass(cls):
        cls.memory.pool.close()
        pg_testdb.drop_db(cls.db)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DELETE FROM insights.run")
            conn.execute("DELETE FROM insights.insight")
        self.rows = [["PO100109", "126"]]
        self.worker = Worker(lambda: self.rows)
        self.backend = type("B", (), {"reads": self.worker, "identity_for":
                                      staticmethod(session_for), "reader_mode": "entra"})()
        self.now = [at("2026-11-19T08:00")]
        self.runner = self.make()

    def make(self, worker=None):
        backend = self.backend if worker is None else type(
            "B", (), {"reads": worker, "identity_for": staticmethod(session_for),
                      "reader_mode": "entra"})()
        return ir.Runner(backend, self.memory.pool.connection, b"k" * 32,
                         clock=lambda: self.now[0])

    def insight(self, owner=f"employee:{ANA}", schedule="hourly", notify="new_rows", watch="{}",
                question=""):
        with psycopg.connect(self.dsn) as conn:
            (iid,) = conn.execute(
                "INSERT INTO insights.insight (owner, title, sql, schedule, notify, watch, "
                "question) VALUES (%s, 'Late POs', 'SELECT po, days_late FROM purchase_orders', "
                "%s, %s, %s, %s) RETURNING id",
                (owner, schedule, notify, watch, question)).fetchone()
        return iid

    def runs(self, iid):
        with psycopg.connect(self.dsn) as conn:
            return conn.execute("SELECT status, reason, row_count, keys, new_rows, fired, "
                                "ran_at FROM insights.run WHERE insight_id = %s ORDER BY id",
                                (iid,)).fetchall()

    def test_it_runs_as_its_owner_and_keeps_no_business_value(self):
        iid = self.insight()
        self.insight(schedule="")                       # not scheduled: never runs
        got = self.runner.tick()
        self.assertEqual([g["insight_id"] for g in got], [iid])
        self.assertEqual(self.worker.as_who, [[f"employee:{ANA}"]])
        (status, reason, n, keys, new, fired, _at), = self.runs(iid)
        self.assertEqual((status, reason, n, new, fired), ("ok", "", 1, 0, False))
        with psycopg.connect(self.dsn) as conn:
            dumped = str(conn.execute("SELECT * FROM insights.run").fetchall())
        self.assertNotIn("PO100109", dumped)
        self.assertNotIn("126", "".join(keys))
        self.assertEqual(self.runner.tick(), [])        # ran this hour: not again

    def test_a_new_row_comes_to_its_owner_only_and_leaves_when_put_away(self):
        iid = self.insight()
        self.runner.tick()                              # the first run sets what was there
        self.rows = [["PO100109", "126"], ["PO100200", "3"]]
        self.now[0] += dt.timedelta(hours=1)
        self.runner.tick()
        self.assertEqual([r[4:6] for r in self.runs(iid)], [(0, False), (1, True)])
        mine = ir.news(self.memory.pool.connection, f"employee:{ANA}", self.now[0])
        self.assertEqual([(n.insight_id, n.row_count, n.new_rows) for n in mine],
                         [(f"s{iid}", 2, 1)])
        self.assertIn("1 new row", mine[0].said)
        self.assertEqual(ir.news(self.memory.pool.connection, f"employee:{BEN}",
                                 self.now[0]), [])
        self.assertFalse(ir.put_away(self.memory.pool.connection, f"employee:{BEN}",
                                     mine[0].run_id))
        for bad in ("r", "x1", "r1;", "r" + "9" * 30):
            self.assertFalse(ir.put_away(self.memory.pool.connection, f"employee:{ANA}", bad))
        self.assertTrue(ir.put_away(self.memory.pool.connection, f"employee:{ANA}",
                                    mine[0].run_id))
        self.assertEqual(ir.news(self.memory.pool.connection, f"employee:{ANA}",
                                 self.now[0]), [])

    def test_refused_or_not_an_employee_reads_nothing(self):
        refused = self.insight()
        demo = self.insight(owner="plx:staff:ana")
        other = self.insight(owner=f"employee:{BEN}")

        def identity(oid, exp):
            if oid == ANA:
                raise RuntimeError("inactive")
            return session_for(ANA, exp)               # never anyone but the owner
        self.backend.identity_for = staticmethod(identity)
        self.runner.tick()
        self.assertEqual(self.worker.as_who, [])
        for iid, why in ((refused, "refused"), (demo, "not_employee"), (other, "refused")):
            (status, reason, n, *_rest), = self.runs(iid)
            self.assertEqual((status, reason, n), ("failed", ir.REASONS[why], 0))

    def test_nothing_runs_until_this_process_can_run_as_people(self):
        iid = self.insight()
        can = [None]
        runner = ir.Runner(self.backend, lambda: can[0], b"", clock=lambda: self.now[0])
        self.assertEqual(runner.tick(), [])             # no store, identity or key yet
        can[0] = (self.memory.pool.connection, b"k" * 32)
        self.assertEqual([g["insight_id"] for g in runner.tick()], [iid])

    def test_several_processes_run_it_once(self):
        iid = self.insight()
        slow = Worker(lambda: self.rows, delay=0.5)
        runners = [self.make(slow) for _ in range(4)]
        threads = [threading.Thread(target=r.tick) for r in runners]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(self.runs(iid)), 1)
        self.assertEqual(len(slow.as_who), 1)

    def test_a_statement_that_cannot_be_read_says_why_and_comes_to_nobody(self):
        iid = self.insight(notify="every_run")
        self.worker.rows = None

        def broken(reader, job, timeout=0):
            view = type("V", (), {})()
            view._call = lambda tool, args: {"error": {"code": "sql_error", "message":
                                                       'invalid input syntax for type '
                                                       'integer: "ACME-4471"'}}
            return job(view)
        self.worker.run = broken
        self.runner.tick()
        (status, reason, n, _k, _new, fired, _at), = self.runs(iid)
        self.assertEqual((status, n, fired), ("failed", 0, False))
        # a fixed sentence: the database's own message (it may carry a row's value) is not kept
        self.assertIn(reason, set(ir.FAILED.values()) | {ir.FAILED_OTHER})
        self.assertNotIn("ACME", reason)
        self.assertNotIn("rephras", reason.lower())

    def test_schedule_is_set_by_its_owner_only_and_shows_the_last_run(self):
        from operator_api.tests.test_memory import reader
        b = type("B", (ins.InsightsMixin,), {})()
        b.memory, b.reads = self.memory, self.worker
        ana = reader("ana")
        iid = self.insight(owner=ana.key, schedule="")
        body = m.InsightSchedule(schedule="daily", notify="rows")
        self.assertEqual(b.insight_schedule(ana, f"s{iid}", body).schedule, "daily")
        with self.assertRaises(UserFacing) as e:
            b.insight_schedule(reader("ben"), f"s{iid}", body)
        self.assertEqual(e.exception.status, 404)
        viewing = reader("ana")
        viewing.session.view_as = True
        with self.assertRaises(UserFacing) as e:
            b.insight_schedule(viewing, f"s{iid}", body)
        self.assertEqual(e.exception.code, "view_as_refused")
        self.assertEqual(b.insight_news(viewing).items, [])
        with psycopg.connect(self.dsn) as conn:
            conn.execute("INSERT INTO insights.run (insight_id, ran_at, status, row_count, "
                         "new_rows) VALUES (%s, %s, 'ok', 4, 1)", (iid, at("2026-11-19T07:02")))
        listed = [i for i in b.insight_store().list(ana.key) if i.id == f"s{iid}"]
        self.assertEqual(listed[0].last_run, "Last ran Nov 19, 7:02 AM ET: 4 rows, 1 new.")

    def watched(self, line, question=""):
        return self.insight(notify="crosses", watch=json.dumps(line), question=question)

    def tick_at(self, minutes):
        self.now[0] = self.now[0] + dt.timedelta(minutes=minutes)
        return self.runner.tick()

    def test_a_watched_number_comes_to_them_when_it_crosses_the_line(self):
        iid = self.watched({"column": "days_late", "op": "above", "value": 100})
        fired = []
        for days in ("126", "130", "90", "99.99", "100", "100.01", "250"):
            self.rows = [["PO100109", days], ["PO100110", "12"]]
            self.tick_at(61)
            fired.append(self.runs(iid)[-1][5])
        # past the line at the first run; not again while it stays past; again once it
        # came back under and crossed (100 is not above 100)
        self.assertEqual(fired, [True, False, False, False, False, True, False])
        with psycopg.connect(self.dsn) as conn:
            overs = [r[0] for r in conn.execute("SELECT over_rows FROM insights.run WHERE "
                                                "insight_id = %s ORDER BY id", (iid,))]
        self.assertEqual(overs, [1, 1, 0, 0, 0, 1, 1])
        news = ir.news(self.memory.pool.connection, f"employee:{ANA}", self.now[0])
        self.assertEqual([n.said for n in news][:1],
                         ["1 row now above 100 in days late (2 rows in all)."])

    def test_only_a_monitor_s_news_offers_its_why_question(self):
        why = "Why are late POs above 100 now? Break it down and say what changed."
        crossing = self.watched({"column": "days_late", "op": "above", "value": 100}, why)
        listing = self.insight(question="late POs by buyer")
        self.tick_at(61)                                # sets what was there for the listing
        self.rows = [["PO100109", "126"], ["PO100200", "3"]]
        self.tick_at(61)
        news = {n.insight_id: n.ask for n in
                ir.news(self.memory.pool.connection, f"employee:{ANA}", self.now[0])}
        self.assertEqual(news, {f"s{crossing}": why, f"s{listing}": ""})

    def test_below_counts_every_row_past_the_line(self):
        iid = self.watched({"column": "days_late", "op": "below", "value": 50})
        self.rows = [["PO1", "12"], ["PO2", "-3"], ["PO3", "51"], ["PO4", None], ["PO5", "n/a"]]
        self.tick_at(61)
        with psycopg.connect(self.dsn) as conn:
            (over, fired), = conn.execute("SELECT over_rows, fired FROM insights.run WHERE "
                                          "insight_id = %s", (iid,)).fetchall()
        self.assertEqual((over, fired), (2, True))

    def test_a_watched_column_it_no_longer_reads_fails_plainly_and_comes_to_nobody(self):
        iid = self.watched({"column": "amount", "op": "above", "value": 1})
        self.tick_at(61)
        (status, reason, n, _k, _new, fired, _at), = self.runs(iid)
        self.assertEqual((status, reason, fired), ("failed", ir.FAILED["insight_watch"], False))

    def test_a_watch_is_set_with_its_line_only(self):
        from operator_api.tests.test_memory import reader
        b = type("B", (ins.InsightsMixin,), {})()
        b.memory, b.reads = self.memory, self.worker
        ana = reader("ana")
        iid = self.insight(owner=ana.key, schedule="")
        with self.assertRaises(UserFacing) as e:
            b.insight_schedule(ana, f"s{iid}", m.InsightSchedule(schedule="daily",
                                                                 notify="crosses"))
        self.assertEqual(e.exception.code, "insight_watch")
        for bad in ({"column": "x; drop", "op": "above", "value": 1},
                    {"column": "x", "op": "near", "value": 1},
                    {"column": "x", "op": "above", "value": float("nan")}, "nonsense"):
            self.assertEqual(ins.watch_of(bad), {})
        line = m.InsightWatch(column="days_late", op="above", value=30)
        got = b.insight_schedule(ana, f"s{iid}", m.InsightSchedule(
            schedule="daily", notify="crosses", watch=line))
        self.assertEqual((got.notify, got.watch.column, got.watch.value),
                         ("crosses", "days_late", 30.0))
        # another notify drops the line
        got = b.insight_schedule(ana, f"s{iid}", m.InsightSchedule(
            schedule="daily", notify="rows", watch=line))
        self.assertIsNone(got.watch)


if __name__ == "__main__":
    unittest.main()
