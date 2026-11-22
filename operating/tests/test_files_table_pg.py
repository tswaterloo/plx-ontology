"""SharePoint files searched from their table (``business_comms.FILES``) on Postgres: the
search never returns a file the per-file check refuses.

A real SharePoint capture (the capture tests' in-process FakeDrive behind the GET-only
client: the seeded quality folder plus a Freightcom-text file, a file with no text, and 60
files of one word, half of them on a permission list sales is not on) projected by the
real projector and loaded into a throwaway Postgres; the people exclusion's set built by
its real build; ``sharepoint_files`` built by the business build and its guarded view
installed. Quality and sales both hold the folder, so the table's own rule (the drive's
unit) admits every file to both; the item permissions are the per-file check's
(``item_gate_from_case_runtime``: the case host's citation check). For a full reader
(``people_excluded.full_reader``) of either kind, every page ``SqlTool.comms_search``
returns, walked by its cursor at limits 1, 3 and 50, is exactly the files the item gate
serves among the matches, each once (none refused, none lost); the Freightcom file never;
the text-less file found by its name; and a reader who is not full searches no file at all
(its files go to the documents search, as before).

Evidence label: injected-transport (no network, made-up ids and addresses)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from integrations import capture_sharepoint as csp
from integrations import graph_read
from integrations.capture_sharepoint import SharePointCapture, SharePointCaptureStore
from integrations.live_source_guard import BoundedBackoff
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_capture_sharepoint import (CAPTURE, DRIVE, ENV, EMPLOYEE,
                                                        QUALITY, RICARDO, SALES, Clock,
                                                        capture_audience, reader_audience,
                                                        scope, seeded_drive)
from ontology import schema as schema_loader
from ontology.project import SharePointCaptures, build_store
from operating import ontology_tools as ot
from operating import ontology_tools_sql as ts
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaseCaptures
from operating.ontology_tools_results import item_gate_from_case_runtime
from operating.pg import business_views as bv
from operating.pg import comms_search as cms
from operating.pg import people_excluded, sql_views
from tests import test_ontology_tools_evidence as ev

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
# within the host's max capture age of the capture (its Clock starts 2026-10-01 12:00)
SOON = dt.datetime(2026, 10, 1, 13, 0, tzinfo=dt.timezone.utc)
FREIGHTCOM = "01FCTEXT"
NO_TEXT = "01NOTEXT"
N = 60
_NO_FLOOR = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0"})


def setUpModule():
    _NO_FLOOR.start()


def tearDownModule():
    _NO_FLOOR.stop()


def _drive():
    drive = seeded_drive()
    drive.file(FREIGHTCOM, "pickup.txt", "01AOS",
               b"Freightcom pickup quote for lot 7 widget", mime="text/plain",
               acl=(RICARDO, SALES))
    drive.file(NO_TEXT, "Dandylion widget COA.png", "01AOS", b"\x89PNG\r\n\x1a\n0000",
               mime="image/png", acl=(RICARDO, SALES))
    # 60 files naming one word; the odd ones on a list sales is not on
    for i in range(N):
        drive.file(f"01W{i:03d}", f"widget note {i:03d}.txt", "01LOTS",
                   f"widget batch {i}".encode(), mime="text/plain",
                   acl=(RICARDO,) if i % 2 else (RICARDO, SALES))
    return drive


class FilesTableThroughTheGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from ontology.text_index import store_text_index
        from operating.pg import words
        from operating.pg.migrate import MIGRATIONS_DIR
        from operating.tests import pg_store_mode
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = root = Path(cls.tmp.name)
        cls.state = root / "sharepoint"
        store = SharePointCaptureStore(cls.state, clock=Clock())
        api, transport = graph_read.build_graph(ENV, sender=_drive(), retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = SharePointCapture(store, lambda d: SharePointReader(
            api, capture_audience(), d, transport=transport, read_permissions=True,
            max_acl_reads=csp.MAX_ACL_READS), CAPTURE).run(scope())
        assert run["complete"], run
        store.close()
        cls.projection = root / "ontology.sqlite"
        cls.compiled = schema_loader.load_schema()
        with SharePointCaptures(cls.state) as files:
            build_store(cls.projection, cls.compiled, {"sharepoint": files},
                        clock=lambda: NOW, post_project={"text_index": store_text_index})
        cls.sqlite = sqlite3.connect(cls.projection)
        cls.dsn = pg_store_mode._load(str(cls.projection), cls.sqlite)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            if not pg.execute("SELECT count(*) FROM ontology.chunk_words").fetchone()[0]:
                rows = pg.execute("SELECT chunk_id, body FROM ontology.text_chunks").fetchall()
                for cid, vec in words.chunk_word_rows((r[0], bytes(r[1])) for r in rows):
                    pg.execute("INSERT INTO ontology.chunk_words VALUES (%s, %s::tsvector)",
                               (cid, vec))
            for name in ("0027_item_audiences.sql", "0038_own_issued_documents.sql", "0040_people_excluded_objects.sql",
                         "0041_business_views.sql"):
                pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        # the exclusion's set reads the people directory a mail capture records: an empty one
        from integrations.capture_mail import MailCaptureStore
        MailCaptureStore(root / "mail", clock=Clock()).close()
        with psycopg.connect(cls.dsn) as pg:
            people_excluded.build(pg, root / "mail", environ=ENV, workers=2)
        cls.tag = people_excluded.current_exclusion(ENV)
        assert cls.tag, "the fixture's exclusion list must be decidable"
        cls.built = bv.build(cls.dsn, ["sharepoint_files"], schema=cls.compiled)
        sql_views.install(cls.dsn)
        cls.files = {row[1]: row[0] for row in cls.sqlite.execute(
            "SELECT o.object_id, v.l1_handle FROM objects o JOIN object_versions v ON "
            "v.object_id = o.object_id AND v.superseded_at IS NULL "
            "WHERE o.object_type = 'SharePointFile'")}
        keys = {row[0] for row in cls.sqlite.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_type = 'SharePointFile'")}
        cls.policy = ot.ReadPolicy(tenant="plx", audiences={
            k: {"staff:quality", "staff:sales"} for k in keys})
        audience_file = root / "audience.json"
        audience_file.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                             "audiences": {}}), encoding="utf-8")
        cls.audience_file = audience_file

    @classmethod
    def tearDownClass(cls):
        cls.sqlite.close()
        cls.tmp.cleanup()

    def runtime(self) -> CaseRuntime:
        runtime = CaseRuntime(self.root / "case-state", client=None,
                              audience=self.audience_file, tenant="plx",
                              live=LiveReaders(audience=reader_audience()),
                              people_exclusion=PeopleExclusion.from_env(ENV))
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, sharepoint_capture_dir=self.state,
                                        store_path=self.projection,
                                        clock=lambda: SOON)
        return runtime

    def tool(self, gate=None) -> ts.SqlTool:
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.sqlite))
        base = ot.OntologyTools(reader, self.policy, item_gate=gate,
                                schema=ot.SchemaView.from_compiled(self.compiled),
                                grammar=None, contracts=ev.CONTRACTS, clock=lambda: NOW)
        return ts.SqlTool(base)

    def walk(self, tool, actor, query, limit, *, full=True):
        """Every page of one search, by its cursor: the handles in order, and the
        sources it says it searched. Every cursor names only files this search returned
        (never one the gate refused: not its handle, not its id)."""
        got, cursor, searched = [], None, None
        with mock.patch.object(ts, "_people_tag", return_value=self.tag), \
                mock.patch.object(people_excluded, "full_reader", return_value=full):
            refused = {h for h in self.files if not tool.base._item_allowed(actor, h)}
            # each refused file's own item id (the drive is shared with served files)
            parts = {h.split("://", 1)[-1].split("/", 1)[-1].split("@", 1)[0] + "@"
                     for h in refused}
            for _ in range(N + 10):
                args = {"query": query, "sources": ["sharepoint"], "limit": limit}
                if cursor:
                    args["cursor"] = cursor
                out = tool.comms_search(args, actor)
                searched = out["searched"]
                self.assertLessEqual(len(out["documents"]), limit)
                got += [d["citation"] for d in out["documents"]]
                cursor = out["completeness"]["next_cursor"]
                self.assertNotEqual(out["completeness"].get("reason"), "withheld_run")
                for part in parts:
                    self.assertNotIn(part, cursor or "", "a refused file rode in a cursor")
                if not cursor:
                    return got, searched
        raise AssertionError("the cursor never ended")

    def matching(self, word):
        """The files whose name, folder or text holds ``word`` (the fixture's own record)."""
        out = set()
        for handle, oid in self.files.items():
            text = " ".join(str(v) for (v,) in self.sqlite.execute(
                "SELECT value FROM object_values WHERE object_id = ?", (oid,)))
            if word in text.lower():
                out.add(handle)
        return out

    def test_every_page_is_the_item_gates_files_each_once(self):
        runtime = self.runtime()
        gate = item_gate_from_case_runtime(runtime)
        matches = self.matching("widget")
        fc = {h for h, o in self.files.items() if FREIGHTCOM.lower() in h.lower()}
        self.assertEqual(len(fc), 1, fc)
        self.assertGreaterEqual(len(matches), N + 1)
        served_by = {a: {h for h in matches if gate(a, h)} for a in (QUALITY, EMPLOYEE)}
        # the per-file check serves quality every file it matches, sales only the files
        # on its list, though the table's own rule admits every one of them to both
        self.assertEqual(served_by[QUALITY], matches)
        self.assertEqual(len(matches - served_by[EMPLOYEE]), N // 2)
        for actor in (QUALITY, EMPLOYEE):
            served = served_by[actor]
            refused = matches - served
            for limit in (1, 3, 50):
                with self.subTest(actor=sorted(actor.principals), limit=limit):
                    got, searched = self.walk(self.tool(gate), actor, "widget", limit)
                    self.assertIn("sharepoint", searched["sources"])
                    self.assertEqual(len(got), len(set(got)), "a file came back twice")
                    self.assertFalse(set(got) & refused, "a refused file was returned")
                    self.assertEqual(set(got), served - fc)
                    self.assertFalse(set(got) & fc, "the Freightcom file was returned")

    def test_a_files_search_is_the_same_whatever_rows_the_walk_reads_first(self):
        """A files search walks the newest rows before it reads the word index's whole
        match (``comms_search.WALK_ROWS``): a walk of 1, 2 or 7 rows, one over every row
        and a rare word's (the index at once) give each reader the same files in the same
        order, page by page, with the item gate refusing some of them."""
        from operating.pg import comms_search as cs
        gate = item_gate_from_case_runtime(self.runtime())
        walked = 0
        for actor in (QUALITY, EMPLOYEE):
            for limit in (1, 3, 50):
                with mock.patch.object(cs, "WALK_ROWS", 100000), \
                        mock.patch.object(cs, "COMMON_CHUNKS", 0):
                    whole, _ = self.walk(self.tool(gate), actor, "widget", limit)
                self.assertEqual(self.walk(self.tool(gate), actor, "widget", limit)[0], whole)
                for rows in (1, 2, 7):
                    with self.subTest(actor=sorted(actor.principals), limit=limit,
                                      walk=rows), \
                            mock.patch.object(cs, "WALK_ROWS", rows), \
                            mock.patch.object(cs, "COMMON_CHUNKS", 0):
                        self.assertEqual(
                            self.walk(self.tool(gate), actor, "widget", limit)[0], whole)
                walked += len(whole) > 7
        self.assertTrue(walked, "no search held more rows than the walk: the index read "
                                "past the walk never ran")

    def test_a_long_run_of_refused_files_pages_past_them_and_loses_none(self):
        # a gate that serves only the two oldest matches: every page before them reads
        # refused files only, so each cursor carries the start and a count, never a
        # refused file, and the walk still ends with both
        runtime = self.runtime()
        real = item_gate_from_case_runtime(runtime)
        ordered, _ = self.walk(self.tool(real), QUALITY, "widget", 50)
        self.assertGreaterEqual(len(ordered), 8)
        keep = set(ordered[-2:])      # the search's last two: every row before is refused
        gate = lambda actor, handle: handle in keep and real(actor, handle)  # noqa: E731
        for limit in (1, 3, 50):
            with self.subTest(limit=limit):
                got, _ = self.walk(self.tool(gate), QUALITY, "widget", limit)
                self.assertEqual(got, ordered[-2:])

    def test_a_file_without_text_is_found_by_its_name(self):
        gate = item_gate_from_case_runtime(self.runtime())
        image = [h for h in self.files if NO_TEXT.lower() in h.lower()]
        self.assertEqual(len(image), 1)
        got, _ = self.walk(self.tool(gate), QUALITY, "Dandylion", 20)
        self.assertEqual(got, image)

    def test_a_reader_who_is_not_full_or_a_host_without_a_gate_searches_no_file(self):
        gate = item_gate_from_case_runtime(self.runtime())
        for tool, full in ((self.tool(gate), False), (self.tool(None), True)):
            with self.subTest(full=full):
                # asked for files alone, nothing here is searched: not served (plx.search
                # then reads them through the documents search)
                with self.assertRaises(ts.ToolError) as caught:
                    self.walk(tool, QUALITY, "widget", 20, full=full)
                self.assertEqual(caught.exception.code, "not_served")

    def count(self, tool, actor, *, full):
        with mock.patch.object(ts, "_people_tag", return_value=self.tag), \
                mock.patch.object(people_excluded, "full_reader", return_value=full):
            out = tool.call("plx.sql", {"sql": "SELECT count(*) FROM sharepoint_files"}, actor)
        self.assertNotIn("error", out, out)
        cell = out["rows"][0]["cells"][0]
        return int(cell["untrusted_text"] if isinstance(cell, dict) else cell)

    def test_a_full_reader_counts_the_files_through_the_door_and_no_other_reader_reads_one(self):
        """11-22 (A full reader sees every business record): plx.sql counts and
        lists the files for a full reader (the door's rows: its unit, Freightcom, the people
        flag), and reads none for a reader who is not full or on a host with no item gate."""
        gate = item_gate_from_case_runtime(self.runtime())
        served = self.count(self.tool(gate), QUALITY, full=True)
        self.assertGreater(served, 0)
        self.assertEqual(served, self.count(self.tool(gate), EMPLOYEE, full=True))
        self.assertEqual(self.count(self.tool(gate), QUALITY, full=False), 0)
        self.assertEqual(self.count(self.tool(None), QUALITY, full=True), 0)
        # listed by folder: the rows are the table's own
        with mock.patch.object(ts, "_people_tag", return_value=self.tag), \
                mock.patch.object(people_excluded, "full_reader", return_value=True):
            out = self.tool(gate).call("plx.sql", {"sql": "SELECT name, path, size FROM "
                                                   "sharepoint_files ORDER BY name LIMIT 5"},
                                       QUALITY)
        self.assertNotIn("error", out, out)
        self.assertTrue(out["rows"])


from integrations.graph_read import SharePointReader  # noqa: E402

if __name__ == "__main__":
    unittest.main()
