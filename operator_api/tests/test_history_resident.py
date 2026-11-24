"""The History tab kept current (speed specs 8.3 ``history`` "Incremental", 9.3 V2): on the business
views fixture every purchase order page's history is read by the tab's own code as the owner and
stored with the watermarks it was read at; the resident builder's ``Finder`` then finds exactly the
records that need reading again (a request first, then a record whose objects or links moved, then
a member record with no history: a purchase order created after the fill), and the box's puller
stores the builder's files for their records only, sets a bad file aside, and leaves every file in
place, said, when the disk budget refuses."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import psycopg

from operating import disk_budget
from operating.pg import business_views as bv
from operating.pg import plan_sections_pull as psp
from operating.pg import record_history as rh
from operator_api import history_batch, history_resident as hr, plan_resident as pr
from operator_api.tests import test_served_history as tsh
from operator_api.tests import test_served_read as tsr

S = bv.SCHEMA
ALL = (0, 1)


# the build's disk floor (PLX_BIZ_MIN_FREE_GB) is the host's, not what this module tests: the
# fixture's tmp disk is not the served one (the real floor in the code is untouched)
_NO_FLOOR = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0"})


def setUpModule():
    _NO_FLOOR.start()


def tearDownModule():
    _NO_FLOOR.stop()


class Incremental(tsr.Fixture, unittest.TestCase):
    def lines(self, keys, deps):
        tools = self.base()
        return [{**tsh.line_of(tools, k), "deps": deps} for k in keys]

    def put(self, lines):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for line in lines:
                fh.write(json.dumps(line, default=str) + "\n")
        self.addCleanup(os.unlink, fh.name)
        return rh.load(self.dsn, [fh.name])

    def fresh(self):
        """Every page's history stored at the current watermarks; no request waiting."""
        with psycopg.connect(self.dsn, autocommit=True) as c:
            deps = history_batch.deps_of(c)
            c.execute(f"DELETE FROM {S}.page_items WHERE page_kind = 'history'")
            c.execute(f"DELETE FROM {S}.page_requests WHERE page_kind = 'history'")
        got = self.put(self.lines(self.keys(), deps))
        self.assertEqual((got["failed"], got["refused"]), (0, {}))
        return deps

    def found(self, finder, now=None):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            return finder.discover(c, ALL, now=now)

    def test_members_are_the_record_pages_keys(self):
        with psycopg.connect(self.dsn) as c:
            ids = [r[0] for r in c.execute(history_batch.ids_sql(S))]
        self.assertEqual(sorted(ids), sorted(self.keys()))
        self.assertGreaterEqual(len(ids), 60)

    def test_a_fresh_store_needs_nothing_and_each_kind_of_move_is_found(self):
        self.fresh()
        keys = self.keys()
        finder = hr.Finder(S)
        self.assertEqual(self.found(finder), [])           # first look: every head decided
        # a version of the record itself written late (a transaction past the horizon)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("UPDATE ontology.object_versions SET recorded_at = recorded_at WHERE "
                      "object_id = %s", (keys[0],))
            linked = c.execute("SELECT from_object FROM ontology.link_rows WHERE from_object = "
                               "ANY(%s) AND from_object <> %s LIMIT 1",
                               (keys, keys[0])).fetchone()
            if linked:
                c.execute("UPDATE ontology.link_rows SET row_no = row_no WHERE from_object = %s",
                          (linked[0],))
        # keys[0]'s page and every page whose history names it; each page the link rows start or
        # end at
        want = {r[0]: "moved" for r in self.truth(
            "SELECT DISTINCT p.page_key FROM {biz}.page_items p JOIN {biz}.lineage g ON g.view = "
            "'page_items' AND g.handle = p.handle WHERE p.page_kind = 'history' AND (g.object_ids "
            "|| g.read_ids) && ARRAY[%s] UNION SELECT h.page_key FROM {biz}.page_items h JOIN "
            "ontology.link_rows k ON h.page_key IN (k.from_object, k.to_object) WHERE "
            "h.page_kind = 'history' AND h.section = 'history_head' AND k.from_object = %s",
            (keys[0], linked[0] if linked else ""))}
        self.assertIn(keys[0], want)
        if linked:
            self.assertIn(linked[0], want)
        self.assertEqual(dict(self.found(finder)), want)
        # answered (the memo): not asked again while the store has not replayed the put
        with psycopg.connect(self.dsn, autocommit=True) as c:
            deps = history_batch.deps_of(c)
        for k in want:
            finder.memo[k] = (0.0, deps)
        self.assertEqual(self.found(finder, now=1.0), [])

    def test_a_record_with_no_history_and_a_request_are_found_request_first(self):
        self.fresh()
        keys = self.keys()
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"DELETE FROM {S}.page_items WHERE page_kind = 'history' AND page_key = "
                      "ANY(%s)", (keys[:2],))
            c.execute(f"INSERT INTO {S}.page_requests (page_kind, page_key) VALUES "
                      "('history', %s)", (keys[1],))
        got = self.found(hr.Finder(S))
        self.assertEqual(got, [(keys[1], "requested"), (keys[0], "missing")])

    def test_a_line_without_watermarks_is_read_again_and_odd_watermarks_are_refused(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"DELETE FROM {S}.page_items WHERE page_kind = 'history'")
        keys = self.keys()[:3]
        lines = self.lines(keys, None)
        for line in lines:
            line.pop("deps")
        self.put(lines)
        self.assertEqual(sorted(k for k, _ in self.found(hr.Finder(S)) if k in keys), keys)
        for bad in ({"ver": 1}, {"ver": "1", "link": 1, "xmin": 1}, [1, 2, 3]):
            with self.assertRaises(ValueError):
                rh.items({**lines[0], "deps": bad})

    def test_one_look_is_the_same_statements_at_1_10_60_moved_records(self):
        self.fresh()
        keys = self.keys()
        finder = hr.Finder(S)
        self.found(finder)
        counts = []
        for n in (1, 10, 60):
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute("UPDATE ontology.object_versions SET recorded_at = recorded_at WHERE "
                          "object_id = ANY(%s)", (keys[:n],))
                real, calls = c.execute, []

                def counting(*a, **k):
                    calls.append(1)
                    return real(*a, **k)
                with mock.patch.object(c, "execute", counting):
                    got = finder.discover(c, ALL)
            # the records moved, and every other page whose history names one of them (a link
            # to it, its evidence)
            want = {r[0] for r in self.truth(
                "SELECT DISTINCT p.page_key FROM {biz}.page_items p JOIN {biz}.lineage g ON "
                "g.view = 'page_items' AND g.handle = p.handle WHERE p.page_kind = 'history' AND "
                "(g.object_ids || g.read_ids) && %s::text[]", (keys[:n],))}
            self.assertTrue(set(keys[:n]) <= want)
            self.assertEqual(sorted(k for k, _ in got), sorted(want), n)
            counts.append(len(calls))
            for k in keys[:n]:   # not found again once answered
                finder.memo.pop(k, None)
            self.fresh()
            finder = hr.Finder(S)
            self.found(finder)
        self.assertEqual(len(set(counts)), 1, counts)


class Pull(tsr.Fixture, unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        with psycopg.connect(self.dsn, autocommit=True) as c:
            self.deps = history_batch.deps_of(c)
            c.execute(f"DELETE FROM {S}.page_items WHERE page_kind = 'history'")
        self.tools = self.base()

    def write(self, key, n, line=None):
        line = line or {**tsh.line_of(self.tools, key), "deps": self.deps}
        return pr.write_atomic(self.folder, f"h0of1-1-{n}", [{"stamps": {"why": "missing"}},
                                                               line])

    def stored(self):
        with psycopg.connect(self.dsn) as c:
            return {r[0] for r in c.execute(f"SELECT DISTINCT page_key FROM {S}.page_items WHERE "
                                            "page_kind = 'history'")}

    def test_files_are_stored_for_their_records_a_bad_one_set_aside_an_error_counted(self):
        keys = self.keys()[:3]
        for i, k in enumerate(keys):
            self.write(k, i)
        with open(os.path.join(self.folder, "h0of1-1-9.jsonl"), "w") as fh:
            fh.write("{not json\n")
        self.write(None, 10, {"record_id": "PurchaseOrder:gone", "error": "RuntimeError: x"})
        with self.assertLogs("operating.pg.record_history", "WARNING") as logs:
            got = rh.pull_once(self.dsn, psp.DirTransport(self.folder, max_bytes=None))
        self.assertEqual(self.stored(), set(keys))
        self.assertEqual((got["files"], got["records"], got["failed"], got["bad"]), (4, 3, 1, 1))
        self.assertEqual(sorted(os.listdir(self.folder)), [".lease", "bad"])
        self.assertTrue(any("PurchaseOrder:gone" in m for m in logs.output))

    def test_the_disk_budget_refusing_leaves_every_file_and_says_so(self):
        keys = self.keys()[:2]
        for i, k in enumerate(keys):
            self.write(k, i)
        refused = disk_budget.Refused("record_history.pull", 1.0, 0.2)
        with mock.patch.object(disk_budget, "reserve", side_effect=refused), \
                self.assertLogs("operating.pg.record_history", "ERROR"):
            got = rh.pull_once(self.dsn, psp.DirTransport(self.folder, max_bytes=None))
        self.assertIn("disk budget refused", got["disk_refused"])
        self.assertEqual(self.stored(), set())
        self.assertEqual(len([n for n in os.listdir(self.folder) if n.endswith(".jsonl")]), 2)

    def test_a_big_file_is_read_whole(self):
        key = self.keys()[0]
        line = {**tsh.line_of(self.tools, key), "deps": self.deps}
        line["pad"] = "x" * (psp.MAX_BYTES + 10)
        self.write(key, 0, line)
        got = rh.pull_once(self.dsn, psp.DirTransport(self.folder, max_bytes=None))
        self.assertEqual((got["files"], got["records"]), (1, 1))
        self.assertEqual(self.stored(), {key})

class SweepMarker(unittest.TestCase):
    """A resident shard's sweep marker: unseen too long names the shard (an unswept shard leaves
    narrowed readers seeing old items without bound); a shard that never reported counts as late."""

    def test_a_silent_shard_is_named_and_a_reporting_one_is_not(self):
        w = rh.SweepWatch(0.0)
        self.assertEqual(w.stale(rh.SWEEP_STALE_S), [])                    # the count is unknown yet
        self.assertEqual(w.stale(rh.SWEEP_STALE_S + 1), ["no sweep marker seen"])
        w.note(["0/3", "1/3"], 100.0)
        self.assertEqual(w.stale(100.0 + rh.SWEEP_STALE_S), ["shard 2/3"])  # never heard from
        w.note(["2/3"], 650.0)
        self.assertEqual(w.stale(650.0), [])
        self.assertEqual(w.stale(100.0 + rh.SWEEP_STALE_S + 1), ["shard 0/3", "shard 1/3"])

    def test_a_pull_reports_the_markers_it_saw_and_stores_nothing_for_them(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        pr.write_atomic(folder, "h1of4-1-1", [{"stamps": {"why": "sweep"}},
                                              {"sweep": {"shard": "1/4", "moved": 0},
                                               "stale": [], "reset": []}])
        with mock.patch.object(rh, "load", return_value={"records": 0, "failed": 0}):
            got = rh.pull_once("x", psp.DirTransport(folder, max_bytes=None))
        self.assertEqual(got["sweeps"], ["1/4"])


class ConflictSubject(unittest.TestCase):
    def view(self, display, values):
        class V:
            def workspace(self, oid, documents=True):
                assert documents is False      # the builder never reads the mail around a record
                return {"card": {"object": {"object_type": "PurchaseOrder", "display": display},
                                 "values": values}, "conflicts": []}
        return V()

    def test_the_subject_is_the_record_display_only_else_blank(self):
        owner_only = [{"property": "po_number", "value": {"kind": "text", "text": "PO-SECRET-77"}}]
        got = history_batch.conflicts_of(self.view("Order 1234", owner_only), "PurchaseOrder:x")
        self.assertEqual(got["label"], "Order 1234")
        for display in (None, "", "PurchaseOrder:x", "{\"a\": 1}"):
            got = history_batch.conflicts_of(self.view(display, owner_only), "PurchaseOrder:x")
            self.assertEqual(got["label"], "", display)     # no title built from owner values


if __name__ == "__main__":
    unittest.main()
