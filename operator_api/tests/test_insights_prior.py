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
    def test_a_measure_tile_with_nothing_kept_is_left_to_the_page_and_read_by_nobody(self):
        """The one-response dashboard reads the declared tiles; a measure tile with nothing
        kept for the reader (nor a last answer under the same access state) is a ``later``
        line, read once the page asks for it as it nears the view, then streamed kept."""
        from operator_api import insights as ins
        from operator_api.tests import test_insights as ti
        stamp = [7]
        b = ti.Backend(ti.sql_out(ti.Mixin.rows, ti.OUTPUT), everyone=True)
        keeper = ic.Keeper(ic.Kept(lambda: stamp[0], fresh=300.0),
                           lambda r, sql: ins.read_now(b, r, sql))
        keeper.ahead = lambda reader, sqls: []        # nothing read ahead in this test
        b._insight_keep = keeper
        owner = ti.person("employee:owner")
        got = list(b.insights_stream(owner))
        tiles = {t["id"]: t for t in got[1:]}
        measures = [i for i in tiles if i.startswith(ins.MEASURE_TILE)]
        self.assertTrue(measures)
        self.assertTrue(all(tiles[i] == {"id": i, "later": True} for i in measures))
        self.assertTrue(all("later" not in t for i, t in tiles.items()
                            if not i.startswith(ins.MEASURE_TILE)))
        sqls = " ".join(b.reads.sql)
        name = measures[0]
        spec = next(s for s in ins.declared() if s.id == name)
        self.assertNotIn(spec.sql, sqls)                 # never read by the stream
        b.insight_result(owner, name)                    # the page's own read, near the view
        again = {t["id"]: t for t in list(b.insights_stream(owner))[1:]}
        self.assertIn("result", again[name])
        # the door refreshed, access unchanged: the last answer streams; access moved: later
        stamp[0] = 8
        self.assertIn("result", {t["id"]: t for t in list(b.insights_stream(owner))[1:]}[name])
        keeper.pool().shutdown(wait=True)               # its read behind it is done
        keeper._pool = None
        stamp[0] = 9
        b.access = ("policy-2", False, "excl-1", "marks-1")
        moved = {t["id"]: t for t in list(b.insights_stream(owner))[1:]}
        self.assertEqual(moved[name], {"id": name, "later": True})


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
