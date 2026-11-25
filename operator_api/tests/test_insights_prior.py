"""An Insights tile after the business tables refreshed (its door stamp moved) or after a
restart: the reader's last answer is served at once, read again behind it, only while their
access state is the one it was read under; never another reader's, never past
``STALE_MOST_SECONDS``, never on a Refresh (11-22: every tile read cold after each refresh
and restart, Insights 32-56 s with 504s)."""
from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

from operator_api import insights_cache as ic
from operator_api.page_store import PageStore


class PriorAnswer(unittest.TestCase):
    def test_the_last_answer_under_an_older_stamp_is_served_only_with_the_same_access(self):
        stamp, offset, reads, asked = [7], [0.0], [], []

        def read(value):
            return lambda: reads.append(value) or {"rows": value, "access": "acc-1"}

        same = lambda held: held.get("access") == "acc-1"   # noqa: E731
        moved = lambda held: False                          # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            store = PageStore(pathlib.Path(tmp), code="c1")

            def process():   # its own memory, the same file
                return ic.Kept(lambda: stamp[0], fresh=300.0, disk=store, later=asked.append,
                               wall=lambda: time.time() + offset[0])
            a, b = ("employee:a",), ("employee:b",)
            api = process()
            self.assertEqual(api.get((a, "S"), read("A7"))["rows"], "A7")
            stamp[0] = 8                                  # the business tables refreshed
            self.assertEqual(api.get((a, "S"), read("A8"), prior=same)["rows"], "A7")
            self.assertEqual(asked, [(a, "S")])           # read again behind it
            # access changed since: read now; no prior check given: read now
            self.assertEqual(api.get((a, "S"), read("A8b"), prior=moved)["rows"], "A8b")
            stamp[0] = 9
            self.assertEqual(api.get((a, "S"), read("A9"))["rows"], "A9")
            # restarted under a moved stamp: the file's last answer, for that reader only
            stamp[0] = 10
            self.assertEqual(process().get((a, "S"), read("A10"), prior=same)["rows"], "A9")
            self.assertEqual(process().get((b, "S"), read("B10"), prior=same)["rows"], "B10")
            # a Refresh reads now; a dropped answer (access narrowed) is never served again
            self.assertEqual(process().get((a, "S"), read("A10r"), again=True,
                                           prior=same)["rows"], "A10r")
            stamp[0] = 11
            gone = process()
            gone.drop((a, "S"))
            self.assertEqual(gone.get((a, "S"), read("A11"), prior=same)["rows"], "A11")
            # past STALE_MOST_SECONDS: read now
            stamp[0] = 12
            offset[0] = ic.STALE_MOST_SECONDS + 5
            self.assertEqual(process().get((a, "S"), read("A12"), prior=same)["rows"], "A12")
            # no reader to read it again for (``later`` refused): read now
            stamp[0] = 13

            def refused(key):
                raise LookupError("reader not seen")
            k = ic.Kept(lambda: stamp[0], fresh=300.0, disk=store, later=refused,
                        wall=lambda: time.time() + offset[0])
            self.assertEqual(k.get((a, "S"), read("A13"), prior=same)["rows"], "A13")
        self.assertEqual(reads, ["A7", "A8b", "A9", "B10", "A10r", "A11", "A12", "A13"])


class TileAfterRefresh(unittest.TestCase):
    def test_a_tile_after_a_refresh_opens_from_the_readers_last_answer_with_the_same_access(self):
        from operator_api import insights as ins
        from operator_api.tests import test_insights as ti
        stamp = [7]
        b = ti.Backend(ti.sql_out(ti.Mixin.rows, ti.OUTPUT), everyone=True)
        keeper = ic.Keeper(ic.Kept(lambda: stamp[0], fresh=300.0),
                           lambda r, sql: ins.read_now(b, r, sql))
        b._insight_keep = keeper
        owner = ti.person("employee:owner")
        first = b.insight_result(owner, "team_output")
        n = len(b.reads.sql)
        stamp[0] = 8                                   # the business tables refreshed
        got = b.insight_result(owner, "team_output")
        self.assertEqual((got.rows, got.read_at), (first.rows, first.read_at))
        keeper.pool().shutdown(wait=True)              # read again behind it
        self.assertEqual(len(b.reads.sql), n + 1)
        keeper._pool = None
        stamp[0] = 9
        b.access = ("policy-2", False, "excl-1", "marks-1")   # a grant revoked since
        b.insight_result(owner, "team_output")
        self.assertEqual(len(b.reads.sql), n + 2)             # read now on the request
        self.assertIsNone(keeper._pool)                       # nothing served to read behind


class StreamLater(unittest.TestCase):
    def test_a_tile_with_nothing_stored_is_waiting_never_read_by_the_stream(self):
        """The stream draws stored values only: a tile with nothing stored for the reader (nor
        a last answer under the same access state) is a ``waiting`` line whose read runs in
        the background; the page asks again for the stored value (never computed in the
        request: the route says 425 insight_filling until it is stored)."""
        from operator_api import insights as ins
        from operator_api.tests import test_insights as ti
        stamp = [7]
        b = ti.Backend(ti.sql_out(ti.Mixin.rows, ti.OUTPUT), everyone=True)
        keeper = ic.Keeper(ic.Kept(lambda: stamp[0], fresh=300.0),
                           lambda r, sql: b._insight_read(r, sql))
        started = []
        keeper.ahead = lambda reader, sqls: started.extend(sqls) or []
        b._insight_keep = keeper
        owner = ti.person("employee:owner")
        got = list(b.insights_stream(owner))
        tiles = {t["id"]: t for t in got[1:]}
        self.assertEqual(len(tiles), len(got[0]["list"]["items"]))     # none lost
        self.assertTrue(all(t == {"id": i, "later": True, "waiting": True}
                            for i, t in tiles.items()))
        self.assertEqual(b.reads.sql, [])                # not one statement run in the stream
        self.assertTrue(started)                         # each one's read was started
        name = next(i for i in tiles if i.startswith(ins.MEASURE_TILE))
        with self.assertRaises(ins.NotStored):
            b.insight_result(owner, name, stored_only=True)
        self.assertEqual(b.reads.sql, [])
        b.insight_result(owner, name)                    # a background read stores it
        again = {t["id"]: t for t in list(b.insights_stream(owner))[1:]}
        self.assertIn("result", again[name])
        stored = b.insight_result(owner, name, stored_only=True)       # served, age in read_at
        self.assertEqual(stored.read_at, again[name]["result"]["read_at"])
        # their Refresh reads behind the stored value: the request still returns what is stored
        refreshed = b.insight_result(owner, name, True, stored_only=True)
        self.assertEqual(refreshed.read_at, stored.read_at)
        keeper.pool().shutdown(wait=True)                 # the read behind it finishes
        keeper._pool = None
        self.assertEqual(ins.FILLING.status, 425)         # the route's word for "not stored yet"
        # the door refreshed, access unchanged: the last answer streams; access moved: waiting
        stamp[0] = 8
        self.assertIn("result", {t["id"]: t for t in list(b.insights_stream(owner))[1:]}[name])
        keeper.pool().shutdown(wait=True)               # its read behind it is done
        keeper._pool = None
        stamp[0] = 9
        b.access = ("policy-2", False, "excl-1", "marks-1")
        moved = {t["id"]: t for t in list(b.insights_stream(owner))[1:]}
        self.assertEqual(moved[name], {"id": name, "later": True, "waiting": True})


class StreamKeptFirst(unittest.TestCase):
    def test_kept_tiles_stream_first_with_the_door_stamp_and_access_read_once(self):
        """A warm dashboard: every kept tile is sent before any tile is read, and the door's
        stamp and the reader's access state are read once for the stream, not per tile
        (11-22: 61 kept tiles took 1.9-2.9 s waiting on a stamp read each)."""
        from operator_api import insights as ins
        from operator_api.tests import test_insights as ti
        stamps, accesses = [], []
        b = ti.Backend(ti.sql_out(ti.Mixin.rows, ti.OUTPUT), everyone=True)
        keeper = ic.Keeper(ic.Kept(lambda: stamps.append(1) or 7, fresh=300.0),
                           lambda r, sql: ins.read_now(b, r, sql))
        keeper.ahead = lambda reader, sqls: []
        b._insight_keep = keeper
        real = b._insight_access
        b._insight_access = lambda r: accesses.append(1) or real(r)
        owner = ti.person("employee:owner")
        b.insight_result(owner, "team_output")           # one tile kept
        stamps.clear(), accesses.clear()
        got = list(b.insights_stream(owner))[1:]
        self.assertEqual(got[0]["id"], "team_output")    # the kept one first
        self.assertIn("result", got[0])
        n = len(b.reads.sql)
        stamps.clear(), accesses.clear()
        warm = list(b.insights_stream(owner))[1:]        # every declared tile kept now
        self.assertEqual(len(b.reads.sql), n)             # nothing read
        self.assertTrue(all("result" in t or "later" in t for t in warm))
        self.assertEqual(len(stamps), 1)
        self.assertLessEqual(len(accesses), 1)


if __name__ == "__main__":
    unittest.main()


class ExclusionCountsKeptWithTheTile(unittest.TestCase):
    """A measure tile's stated exclusions are read when the tile is, and kept with it: an
    open of a kept tile reads nothing (11-23: sixteen counts read live on every open, one
    after another, were 56 s of the dashboard's stream)."""

    def backend(self, out=None):
        from operator_api import insights as ins
        from operator_api.tests import test_insights as ti
        b = ti.Backend(out or ti.sql_out([["x", 1]], ("name", "value")), everyone=True)
        b._insight_keep = ic.Keeper(ic.Kept(lambda: 7, fresh=300.0),
                                    lambda r, sql: ins.read_now(b, r, sql))
        return b, ti.person("employee:owner")

    def tile_with_exclusions(self):
        from operator_api import insights as ins
        from operating import measures as ms
        names = [n for n in ms.measures() if ms.excluded_statements(n)]
        self.assertTrue(names)
        return f"{ins.MEASURE_TILE}{names[0]}", len(ms.excluded_statements(names[0]))

    def test_a_kept_tile_opens_with_its_counts_and_reads_nothing(self):
        from operator_api import insights as ins
        from operator_api.tests import test_insights as ti
        iid, n = self.tile_with_exclusions()
        out = {"columns": ["row_count", "value"], "rows": [{"cells": [12, 3.5]}],
               "as_of": "t"}
        b, owner = self.backend(out)
        first = b.insight_result(owner, iid)
        read = len(b.reads.sql)
        from operating import measures as ms
        entry = ms.measures()[iid[len(ins.MEASURE_TILE):]]
        checks = int(bool(entry.get("date")) and not ms.is_rate(entry)
                     and str(entry["value"]).startswith(("sum(", "count(*)")))
        self.assertEqual(read, 1 + n + checks)     # the tile, each count and its check, once
        self.assertEqual(len(first.excluded), n)
        self.assertTrue(all(e.n == 12 for e in first.excluded))
        again = b.insight_result(owner, iid)
        self.assertEqual(len(b.reads.sql), read)              # kept: nothing read
        self.assertEqual([e.model_dump() for e in again.excluded],
                         [e.model_dump() for e in first.excluded])
        # another reader never gets this reader's kept counts
        other = b.insight_result(ti.person("employee:other"), iid)
        self.assertEqual(len(b.reads.sql), read + read)
        self.assertEqual(len(other.excluded), n)

    def test_a_count_that_could_not_be_read_says_why_and_is_read_again_next_open(self):
        iid, n = self.tile_with_exclusions()
        b, owner = self.backend()          # no row_count column: "not counted"
        first = b.insight_result(owner, iid)
        self.assertTrue(all(e.n is None and e.why for e in first.excluded))
        read = len(b.reads.sql)
        b.insight_result(owner, iid)
        self.assertEqual(len(b.reads.sql), read + n)          # only the counts, again


class PollsOfOneColdTile(unittest.TestCase):
    def test_n_polls_of_one_cold_tile_leave_exactly_one_read_queued(self):
        """The page polls a tile with nothing stored every few seconds, from every open
        page: while its read is queued or running, ahead / tick / again queue no second one."""
        from operator_api import insights as ins
        from operator_api.tests import test_insights as ti
        b = ti.Backend(ti.sql_out(ti.Mixin.rows, ti.OUTPUT), everyone=True)
        later = ti.Later()
        keeper = ic.Keeper(ic.Kept(lambda: 7, fresh=300.0),
                           lambda r, sql: b._insight_read(r, sql), pool=later)
        b._insight_keep = keeper
        owner = ti.person("employee:owner")
        for _ in range(25):
            with self.assertRaises(ins.NotStored):
                b.insight_result(owner, "team_output", stored_only=True)
            keeper.tick()
        self.assertEqual(len(later.queued), 1)
        later.run()                                   # the one read stores it
        self.assertEqual(keeper._pending, set())
        self.assertIn("result", {t["id"]: t for t in b.insights_stream(owner)
                                 if "id" in t}["team_output"])
        self.assertEqual(keeper.failed, {})           # nothing failed is kept past a success


class CheckedLine(unittest.TestCase):
    def test_a_measure_tile_says_whether_its_months_add_up_to_the_measure_total(self):
        """The stored tile carries a 'checked' line: its rows summed per currency against the
        measure's own total over the same window (same door, same reader); a mismatch is said
        with both figures, a rate or snapshot is said not to be checked, never passed."""
        from decimal import Decimal
        from operator_api import insights as ins
        name = next(s.id for s in ins.declared() if s.id == "measure_revenue")
        spec = next(s for s in ins.declared() if s.id == name)
        month = {"columns": ["month", "value", "revenue_currency", "row_count", "unread_cells"],
                 "rows": [{"cells": ["2026-10-01", "100.50", "CAD", "3", "0"]},
                          {"cells": ["2026-11-01", "50.25", "CAD", "2", "0"]}]}

        class B:
            reads = None
        calls = []

        def run(total):
            def read_sql(backend, reader, sql, seconds=0, ahead=False):
                calls.append(sql)
                return {"columns": ["value", "revenue_currency", "row_count", "unread_cells"],
                        "rows": [{"cells": [total, "CAD", "5", "0"]}]}
            return read_sql
        real = ins.read_sql
        try:
            ins.read_sql = run("150.75")
            ok = ins.checked_of(B(), object(), spec, {"got": month})
            ins.read_sql = run("151.00")
            bad = ins.checked_of(B(), object(), spec, {"got": month})
        finally:
            ins.read_sql = real
        self.assertEqual(ok["status"], "reconciles")
        self.assertIn("150.75", ok["says"])
        self.assertEqual(bad["status"], "differs")
        self.assertIn("150.75", bad["says"])
        self.assertIn("151.00", bad["says"])
        self.assertEqual(len(calls), 2)
        rate = next(s for s in ins.declared() if s.id == "measure_late_order_share")
        self.assertEqual(ins.checked_of(B(), object(), rate, {"got": month})["status"], "not_checked")
        snap = next(s for s in ins.declared() if s.id == "measure_open_orders")
        self.assertEqual(ins.checked_of(B(), object(), snap, {"got": month})["status"], "not_checked")
        self.assertEqual(Decimal("150.75"), Decimal("100.50") + Decimal("50.25"))


class WhyDidItChange(unittest.TestCase):
    def test_the_steps_stream_each_with_the_best_answer_so_far_and_stop_anywhere(self):
        """Step 1 the change, step 2 which dimension holds it, step 3 that dimension's parts:
        each line carries the answer so far, so stopping after any step leaves a true answer;
        a step that cannot be read ends the stream with its typed error; a snapshot or a
        non-measure is refused before anything is read."""
        from operator_api import insights as ins
        from operator_api.errors import UserFacing
        from operator_api.tests import test_insights as ti
        b = ti.Backend(ti.sql_out(ti.Mixin.rows, ti.OUTPUT), everyone=True)
        owner = ti.person("employee:owner")
        reads = []

        def fake(backend, reader, sql, seconds=0, ahead=False):
            reads.append(sql)
            cells = lambda *c: {"cells": list(c)}   # noqa: E731
            if "parts_for_half" in sql:
                return {"columns": ["dimension", "parts", "parts_for_half", "concentration_pct",
                                    "top_part", "top_change", "whole_change",
                                    "top_share_of_change_pct", "unknown_parts"],
                        "rows": [cells("customer", "10", "2", "20.0", "Acme", "40", "50", "80.0", "0"),
                                 cells("customer_chain", "5", "3", "60.0", "X", "10", "50", "20.0", "0")]}
            if "by" in sql.lower() and "customer" in sql and "current" in sql and "GROUP BY" in sql:
                return {"columns": ["customer", "current", "previous", "change"],
                        "rows": [cells("Acme", "50", "10", "40"), cells("Zed", "5", "5", "0")]}
            return {"columns": ["current", "previous", "change"], "rows": [cells("55", "5", "50")]}
        real = ins.read_sql
        ins.read_sql = fake
        try:
            got = list(b.insight_why_stream(owner, "measure_invoices_issued"))
        finally:
            ins.read_sql = real
        self.assertEqual([g["step"] for g in got], [1, 2, 3])
        self.assertIn("55 now against 5 before, a change of 50", got[0]["answer"])
        self.assertIn("customer: Acme (40, 80.0% of the whole change); 2 of 10 parts", got[1]["answer"])
        self.assertTrue(got[2]["answer"].startswith(got[1]["answer"]))
        self.assertEqual(got[2]["rows"][0][0], "Acme")
        self.assertEqual(len(reads), 3)
        # answer now: a reader who stops after step 1 has read one statement and has an answer
        reads.clear()
        ins.read_sql = fake
        try:
            gen = b.insight_why_stream(owner, "measure_invoices_issued")
            first = next(gen)
            gen.close()
        finally:
            ins.read_sql = real
        self.assertEqual((len(reads), bool(first["answer"])), (1, True))

        def broke(backend, reader, sql, seconds=0, ahead=False):
            raise UserFacing("insight_too_slow", "slow", 504)
        ins.read_sql = broke
        try:
            (only,) = list(b.insight_why_stream(owner, "measure_invoices_issued"))
        finally:
            ins.read_sql = real
        self.assertEqual(only["error"]["code"], "insight_too_slow")
        for bad in ("team_output", "measure_open_orders"):
            with self.assertRaises(UserFacing):
                list(b.insight_why_stream(owner, bad))
        # step 2 carries every ranked dimension as figures, not only the top one
        ins.read_sql = fake
        try:
            two = list(b.insight_why_stream(owner, "measure_invoices_issued"))[1]
        finally:
            ins.read_sql = real
        self.assertEqual([r["dimension"] for r in two["ranking"]], ["customer", "customer_chain"])
        self.assertEqual(two["ranking"][1]["top_share_of_change_pct"], "20.0")
        # a drill reads one dimension's parts alone; one the measure does not declare is refused
        reads.clear()
        ins.read_sql = fake
        try:
            (parts,) = list(b.insight_why_stream(owner, "measure_invoices_issued", drill="customer"))
            with self.assertRaises(UserFacing):
                list(b.insight_why_stream(owner, "measure_invoices_issued", drill="not_a_dimension"))
            # the named change and the filter reach every statement read
            list(b.insight_why_stream(owner, "measure_invoices_issued", period="2026-10",
                                      filter={"customer": "Acme"}))
        finally:
            ins.read_sql = real
        self.assertEqual((parts["of"], parts["rows"][0][0]), (1, "Acme"))
        self.assertTrue(all("lower('Acme')" in q and "2026-10-01" in q for q in reads[1:]))

    def test_insight_why_uses_stored_drill_when_available_and_falls_back_on_filter(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest import mock
        from operating.pg import measure_values as mv
        from operator_api import insights as ins
        from operator_api.tests import test_insights as ti
        b = ti.Backend(ti.sql_out(ti.Mixin.rows, ti.OUTPUT), everyone=True)
        b.doors = SimpleNamespace(get=lambda r: (SimpleNamespace(grant="g"), "dsn"),
                                  decide_now=lambda r: None)
        owner = ti.person("employee:owner")
        live_calls = []

        def fake_live(backend, reader, sql, seconds=0, ahead=False):
            live_calls.append(sql)
            return {"columns": ["customer", "current", "previous", "change"],
                    "rows": [{"cells": ["Acme", "50", "10", "40"]}]}

        def fake_stored(pg, door, measure_id, *, by=None, period=None, against=None, why=False):
            return {"columns": [by or "customer", "current", "previous", "change"],
                    "rows": [["Acme", "50", "10", "40"]], "period": "2026-10",
                    "as_of": "2026-11-24T00:00:00Z", "source": "stored"}

        fake_pool = SimpleNamespace(connection=lambda: nullcontext(object()))
        with mock.patch("operating.page_reads.pool_for", return_value=fake_pool), \
                mock.patch.object(mv, "stored_compare", side_effect=fake_stored), \
                mock.patch.object(ins, "read_sql", side_effect=fake_live):
            (got,) = list(b.insight_why_stream(owner, "measure_invoices_issued", drill="customer"))
            self.assertEqual((got["source"], got["rows"][0][0], len(live_calls)), ("stored", "Acme", 0))
            # filter -> cannot use single-dimension stored rows -> falls back to live statement
            (filtered,) = list(b.insight_why_stream(owner, "measure_invoices_issued",
                                                    drill="customer", filter={"status": "posted"}))
            self.assertEqual((filtered["source"], len(live_calls)), ("statement", 1))


class LastGoodDiskAndStoredFallback(unittest.TestCase):
    def test_last_good_reads_from_disk_after_restart_and_respects_age(self):
        stamp, offset = [7], [0.0]
        with tempfile.TemporaryDirectory() as tmp:
            store = PageStore(pathlib.Path(tmp), code="c1")
            a = ("employee:a",)
            api = ic.Kept(lambda: stamp[0], fresh=300.0, disk=store,
                          wall=lambda: time.time() + offset[0])
            api.get((a, "S"), lambda: {"rows": "A7", "access": "acc-1"})
            # fresh process with empty memory: last_good finds the disk copy
            restarted = ic.Kept(lambda: stamp[0], fresh=300.0, disk=store,
                                wall=lambda: time.time() + offset[0])
            self.assertEqual(restarted.last_good((a, "S"))["rows"], "A7")
            # expired past LAST_GOOD_SECONDS: returns None
            offset[0] = ic.LAST_GOOD_SECONDS + 10
            self.assertIsNone(restarted.last_good((a, "S")))

    def test_stored_only_serves_last_good_with_as_of_instead_of_504(self):
        from operator_api import insights as ins
        from operator_api.tests import test_insights as ti
        stamp = [7]
        out = ti.sql_out(ti.Mixin.rows, ti.OUTPUT)
        out["as_of"] = None
        b = ti.Backend(out, everyone=True)
        later = ti.Later()
        keeper = ic.Keeper(ic.Kept(lambda: stamp[0], fresh=300.0),
                           lambda r, sql: b._insight_read(r, sql), pool=later)
        b._insight_keep = keeper
        owner = ti.person("employee:owner")
        first = b.insight_result(owner, "team_output")
        # simulate door stamp unreadable and background read failing with 504 timeout
        stamp[0] = None
        slot = (ic.reader_key(owner), ins.ref(next(s for s in ins.builtin() if s.id == "team_output")))
        keeper.failed[slot] = ins.Refused("insight_too_slow", "30-second")
        got = b.insight_result(owner, "team_output", stored_only=True)
        self.assertEqual(got.rows, first.rows)
        self.assertEqual(got.as_of, first.read_at)
        # access state changed: refuses stale fallback and raises the 504
        b.access = ("policy-2", False, "excl-1", "marks-1")
        with self.assertRaises(ins.Refused) as ctx:
            b.insight_result(owner, "team_output", stored_only=True)
        self.assertEqual(ctx.exception.status, 504)

    def test_fixed_dates_formats_plain_english_dates(self):
        from operator_api import insights as ins
        self.assertEqual(
            ins.fixed_dates("SELECT * FROM sales WHERE d >= '2026-01-01' AND d <= '2026-11-25'"),
            "1 Jan 2026 to 25 Nov 2026",
        )
        self.assertEqual(
            ins.fixed_dates("SELECT * FROM sales WHERE d = '2026-11-25'"),
            "25 Nov 2026",
        )
        self.assertIsNone(ins.fixed_dates("SELECT * FROM sales WHERE d >= now() - interval '7 days'"))
