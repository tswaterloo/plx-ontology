"""The refresh's per-view isolation and freshness (master plan P2 + P6, contract C6):
a view that fails never stops the others, is recorded with its reason and class in
``biz.view_state``, is held after two identical failures until its backoff ends or its code
changes, and the run still ends normally; the watchdog stops a step past its deadline."""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import unittest
from unittest import mock

from operating.pg import business_views as bv
from operating.pg import view_state as vs
from operating.tests import test_business_views as tbv

_ONE_PROCESS = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0",
                                            "PLX_BIZ_VIEW_WORKERS": "1"})


def setUpModule():
    _ONE_PROCESS.start()


def tearDownModule():
    _ONE_PROCESS.stop()


class RefreshIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tbv.BusinessViews.setUpClass.__func__(cls)
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute((MIGRATIONS_DIR / "0060_view_state.sql").read_text())

    def state(self):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            return {r["view"]: r for r in vs.read(c, "biz")}

    def notes_day(self):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            return dict(c.execute("SELECT view, notes->>'as_of' FROM biz.state").fetchall())

    def test_a_planted_failing_view_never_stops_the_others_and_is_held_then_released(self):
        calls = []

        def boom(reader, as_of):
            calls.append(as_of)
            raise ValueError("planted failure in products (row 17)")
        original = bv.VIEWS["products"]
        broken = dataclasses.replace(original, build=boom)
        day1 = tbv.AS_OF + dt.timedelta(days=1)
        with mock.patch.dict(bv.VIEWS, {"products": broken}):
            out = bv.refresh(self.dsn, schema=tbv.SCHEMA, as_of=day1)
            # every other view was rebuilt for the new day; products kept its table; the run
            # ends normally (exit 0: not every view failed)
            days = self.notes_day()
            others = [n for n in bv.VIEWS if n != "products"]
            plain = [n for n in others if bv.VIEWS[n].fill is None]   # comms: in place
            self.assertEqual({n: days[n] for n in plain}, {n: day1.isoformat() for n in plain})
            self.assertEqual(days["products"], tbv.AS_OF.isoformat())
            self.assertEqual(set(out["failed"]), {"products"})
            self.assertFalse(out["all_failed"])
            got = out["failed"]["products"]
            self.assertEqual((got["class"], got["count"], got["until"]), ("code", 1, None))
            self.assertIn("planted failure", got["reason"])
            st = self.state()
            self.assertEqual((st["products"]["fail_class"], st["products"]["fail_count"]),
                             ("code", 1))
            self.assertIsNotNone(st["products"]["failing_since"])
            self.assertIsNone(st["products"]["last_ok_at"])
            for n in others:
                self.assertIsNotNone(st[n]["last_ok_at"], n)
                self.assertIsNone(st[n]["failing_since"], n)
                self.assertEqual(st[n]["behind_minutes"], 0.0, n)
            since = st["products"]["failing_since"]
            # the same failure again: held for 2 h; failing_since is set once
            out = bv.refresh(self.dsn, schema=tbv.SCHEMA, as_of=day1)
            got = out["failed"]["products"]
            self.assertEqual(got["count"], 2)
            self.assertAlmostEqual((got["until"] - dt.datetime.now(dt.timezone.utc))
                                   .total_seconds(), vs.HOLD.total_seconds(), delta=120)
            self.assertEqual(self.state()["products"]["failing_since"], since)
            # held: the third cycle does not try it, says so, and the alarm names it
            tried = len(calls)
            out = bv.refresh(self.dsn, schema=tbv.SCHEMA, as_of=day1)
            self.assertEqual(len(calls), tried)
            self.assertIn("products", out["quarantined"])
            self.assertNotIn("failed", out)
            alarms = {c["name"]: c for c in vs.alarm_checks(self.dsn)}
            self.assertEqual(alarms["view_behind:products"]["state"], "breach")
            self.assertIn("held", alarms["view_behind:products"]["detail"])
            self.assertEqual(alarms["view_behind:customers"]["state"], "ok")
        # a new release (the fix bumps the view's declared version, so its print moves):
        # released at once and built
        fixed = dataclasses.replace(original, version=original.version + 1)
        with mock.patch.dict(bv.VIEWS, {"products": fixed}):
            out = bv.refresh(self.dsn, schema=tbv.SCHEMA, as_of=day1)
        self.assertNotIn("failed", out)
        self.assertIn("products", [g["view"] for g in out["built"]])
        st = self.state()["products"]
        self.assertEqual((st["fail_count"], st["quarantined_until"], st["failing_since"],
                          st["fail_class"]), (0, None, None, None))
        self.assertIsNotNone(st["last_ok_at"])
        self.assertEqual(self.notes_day()["products"], day1.isoformat())

    def test_every_view_failing_is_the_one_run_that_exits_1(self):
        def boom(reader, as_of):
            raise ValueError("all down")
        day3 = tbv.AS_OF + dt.timedelta(days=3)
        plain = {n: dataclasses.replace(v, build=boom) for n, v in bv.VIEWS.items()
                 if v.fill is None}
        with mock.patch.dict(bv.VIEWS, plain), \
                mock.patch("operating.pg.business_comms.refresh_changed",
                           side_effect=ValueError("all down")), \
                mock.patch("operating.pg.business_views_parallel._each",
                           side_effect=lambda dsn, names, *a: ([], {n: "ValueError: all down"
                                                                    for n in names})):
            out = bv.refresh(self.dsn, schema=tbv.SCHEMA, as_of=day3)
        self.assertTrue(out["all_failed"], out.get("failed"))
        with mock.patch.object(bv, "refresh", return_value=out), \
                mock.patch.object(bv, "_heartbeat") as hb:
            self.assertEqual(bv.main(["refresh", "--dsn", self.dsn]), 1)
            hb.return_value.stop.assert_called_once()
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:   # leave the others clean
            for n in bv.VIEWS:
                vs.release(c, "biz", n)

    def test_a_disk_failure_waits_for_the_budget_not_the_clock(self):
        import psycopg
        text = "Refused: disk budget refused stock: needs 3.0 GB, 1.0 GB available"
        with psycopg.connect(self.dsn, autocommit=True) as c:
            vs.failed(c, "biz", "stock", text, version="v1")
            got = vs.failed(c, "biz", "stock", text, version="v1")
            self.assertEqual(got["class"], "disk")
            go, held = vs.runnable(c, "biz", ["stock", "customers"], {"stock": "v1"},
                                   available_gb=lambda: 0.0)
            self.assertEqual(go, ["customers"])
            self.assertIn("GB needed", held["stock"])
            go, held = vs.runnable(c, "biz", ["stock"], {"stock": "v1"},
                                   available_gb=lambda: 10_000.0)
            self.assertEqual((go, held), (["stock"], {}))
            vs.release(c, "biz", "stock")
            # a lock failure is never held
            for _ in range(3):
                got = vs.failed(c, "biz", "customers",
                                "LockNotAvailable: could not obtain lock on relation")
            self.assertEqual((got["class"], got["until"]), ("lock", None))
            vs.ok(c, "biz", ["customers", "stock"], watermark=None,
                  at=dt.datetime.now(dt.timezone.utc))

    def test_bookkeeping_statements_per_view_are_constant(self):
        import psycopg
        names = list(bv.VIEWS)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            per = []
            for n in (1, 10, len(names)):
                seen = []
                real = c.execute
                with mock.patch.object(c, "execute",
                                       side_effect=lambda q, *a, **k: seen.append(q) or
                                       real(q, *a, **k)):
                    vs.ok(c, "biz", names[:n], watermark=1,
                          at=dt.datetime.now(dt.timezone.utc))
                per.append((len(seen) - 1) / n)   # one insert of the missing rows per call
            self.assertEqual(len(set(per)), 1, per)

    def test_column_health_measures_each_view_in_one_statement_and_hides_marked_values(self):
        import psycopg
        import time
        from operating.pg import column_health as ch
        names = [n for n, v in bv.VIEWS.items() if v.fill is None]
        with psycopg.connect(self.dsn, autocommit=True) as c:
            got = ch.measure(c, "biz", names, [])
            self.assertTrue(all(got[n] > 0 for n in names), got)
            rows = ch.read(c, "biz")
            by = {(r["view"], r["column"]): r for r in rows}
            # every column of every view, NULLs counted (fill a share)
            for n in names:
                cols = {r[0] for r in c.execute(
                    "SELECT column_name FROM information_schema.columns WHERE "
                    "table_schema = 'biz' AND table_name = %s", (n,))}
                self.assertEqual({k[1] for k in by if k[0] == n}, cols, n)
            self.assertTrue(all(r["fill"] is None or 0 <= r["fill"] <= 1 for r in rows))
            self.assertTrue(all(len(r["top_values"] or []) <= ch.TOP for r in rows))
            # a column some row carries a marking for keeps no values
            marked = {(n, k) for n in names for k in ch._marked(c, "biz", n, True)}
            self.assertTrue(marked, "the fixture marks a column (a price)")
            for key in marked:
                if key in by:
                    self.assertIsNone(by[key]["top_values"], key)
            # a comms view: sampled, never values
            comms = [n for n, v in bv.VIEWS.items() if v.fill is not None][:1]
            ch.measure(c, "biz", comms, comms)
            self.assertTrue(all(r["top_values"] is None for r in ch.read(c, "biz", comms[0])))
            # one statement per view whatever the view count (1/10/all)
            seen = []
            real = c.execute

            def counting(sql, *a, **k):
                seen.append(sql)
                return real(sql, *a, **k)
            for n_views in (1, 10, len(names)):
                seen.clear()
                with mock.patch.object(c, "execute", side_effect=counting):
                    t0 = time.monotonic()
                    ch.measure(c, "biz", names[:n_views], [])
                    took = time.monotonic() - t0
                measuring = [q for q in seen if q.startswith("WITH s AS MATERIALIZED")]
                self.assertEqual(len(measuring), n_views)
                self.assertLess(took, 60)


class Units(unittest.TestCase):
    def test_classes_come_from_the_type_and_sqlstate(self):
        for text, want in (
                ("Refused: disk budget refused x: needs 3 GB", "disk"),
                ("RuntimeError: disk below the 40 GB floor, build stopped", "disk"),
                ("LockNotAvailable: could not obtain lock", "lock"),
                ("DeadlockDetected: deadlock detected", "lock"),
                ("OutOfMemory: out of memory", "memory"),
                ("MemoryError: ", "memory"),
                ("BrokenProcessPool: a child process terminated abruptly", "memory"),
                ("InvalidDatetimeFormat: invalid input syntax for type date", "data"),
                ("UniqueViolation: duplicate key value", "data"),
                ("KeyError: 'name'", "code"),
                ("QueryCanceled: canceling statement due to statement timeout", "code"),
                ("something without a type", "code")):
            self.assertEqual(vs.classify(*vs.split(text))[0], want, text)
        a = vs.classify(*vs.split("ValueError: bad row 17 of 200"))[1]
        b = vs.classify(*vs.split("ValueError: bad row 18 of 201"))[1]
        self.assertEqual(a, b)

    def test_heartbeat_beats_only_when_the_run_moved(self):
        sent, beats = [], []
        clock = [1]
        with mock.patch.object(vs, "notify", side_effect=lambda m, e=None: sent.append(m)
                               or True):
            hb = vs.Heartbeat(lambda: clock[0], lambda views, phase: beats.append(phase),
                              environ={"NOTIFY_SOCKET": "/run/x"}, every=3600)
            hb.start()
            hb.step("build stock", ["stock"])
            self.assertTrue(hb.tick())        # a step started: moved
            self.assertFalse(hb.tick())       # nothing since: no ping (systemd stops it)
            clock[0] = 2                      # a statement of the run started or ended
            self.assertTrue(hb.tick())
            hb.stop()
        self.assertEqual(sent, ["READY=1", "WATCHDOG=1", "WATCHDOG=1", "STOPPING=1"])
        self.assertEqual(beats, ["build stock", "build stock"])
        self.assertFalse(vs.notify("READY=1", {}))
        with mock.patch("subprocess.run", side_effect=OSError("gone")):
            self.assertFalse(vs.notify("WATCHDOG=1", {"NOTIFY_SOCKET": "/run/x"}))


if __name__ == "__main__":
    unittest.main()
