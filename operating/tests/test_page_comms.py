"""The page's linked messages (speed specs 8.3): ``served.comms_sql`` joins the content-free index
``record_comms`` to the GUARDED person view in one statement, with the reader's six mail settings
for the transaction. On the mail fixture of ``test_biz_comms`` (three mailboxes, the people set's mail,
the item gate): every reader's linked messages are exactly the index's messages that the same reader
reads through ``plx.sql`` (mailbox scope), the people set's mail only for the owner, and the answer says
what it cuts (``total`` the reader's own visible count)."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import psycopg

from operating import ontology_tools_sql as ts
from operating.pg import business_views as bv
from operating.pg import business_comms as bc
from operating.pg import record_comms as rc
from operating.pg import sql_views
from operating.tests import test_biz_comms as tbc
from operating.tests import test_table_mail_parity as tmp
from operator_api import served

KEY = "PurchaseOrder:test:1"

# the build's disk floor (PLX_BIZ_MIN_FREE_GB) is the served host's, not what this test is about
_NO_FLOOR = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0"})


def setUpModule():
    _NO_FLOOR.start()


def tearDownModule():
    _NO_FLOOR.stop()


class PageComms(unittest.TestCase):
    tearDownClass = classmethod(tbc.EmailsThroughTheDoor.tearDownClass.__func__)
    @classmethod
    def setUpClass(cls):
        tbc.EmailsThroughTheDoor.setUpClass.__func__(cls)
        # the other comms kinds (attachments, Teams, files) as the business build makes them
        cls.others = bv.build(cls.dsn, ["attachments", "teams_messages", "sharepoint_files"],
                              schema=cls.compiled)
        sql_views.install(cls.dsn)

    runtime = tbc.EmailsThroughTheDoor.runtime
    gated = tbc.EmailsThroughTheDoor.gated
    subjects = tbc.EmailsThroughTheDoor.subjects
    tool = tbc.EmailsThroughTheDoor.tool
    sql = tbc.EmailsThroughTheDoor.sql
    handles = tbc.EmailsThroughTheDoor.handles

    def linked(self, actor, owner=False, cursor=None):
        """The page's mail for ``actor``: ``served.comms_sql`` under that reader's settings."""
        tool = self.tool(owner)
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            settings = tool._comms_settings(tool.base._scope(actor, tool.base.clock()), actor)
        names = list(settings)
        with psycopg.connect(self.dsn) as pg:
            with pg.transaction():
                pg.execute("SELECT " + ", ".join("set_config(%s, %s, true)" for _ in names)
                           + ", set_config('search_path', %s, true)",
                           [x for n in names for x in (n, settings[n])]
                           + [f"{sql_views.guarded_schema()}, {sql_views.person_schema()}, "
                              f"{bv.SCHEMA}, pg_catalog"])
                built = [v for v, _, _ in served.COMMS_KINDS if pg.execute(
                    "SELECT to_regclass(%s)", (f"{sql_views.person_schema()}.{v}",)).fetchone()[0]]
                out = pg.execute(served.comms_sql(sql_views.person_schema(), built, True),
                                 {"key": KEY, "lost": "[]", "off": "{}", "failed": "{}", "view": cursor and "emails",
                                  "after_at": cursor and cursor["at"],
                                  "after_h": cursor and cursor["handle"]}).fetchone()[0]
        return json.loads(out)["emails"]

    def not_ai(self):
        with psycopg.connect(self.dsn) as pg:
            return {r[0] for r in pg.execute(f"SELECT handle FROM {bv.SCHEMA}.emails WHERE "
                                             "NOT ai_ok")}

    def kinds(self, actor, owner=False):
        """The whole answer for ``actor`` (every kind), built as ``served._comms`` builds it."""
        tool = self.tool(owner)
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            settings = tool._comms_settings(tool.base._scope(actor, tool.base.clock()), actor)
        from operating.pg import business_comms as bc
        from operating import page_reads as pr
        if tool._files_reader(actor):
            settings[bc.FILES_SETTING], settings[pr.FILES_OK] = bc.FILES_FULL, "[]"
        else:   # as PageReads._decide_door: the item gate's allowed linked files, decided ahead
            settings[bc.FILES_SETTING] = bc.FILES_SEARCH
            allowed, state = pr.PageReads(tool.base)._allowed_files(self.dsn, actor)
            settings[pr.FILES_OK], settings[pr.FILES_STATE] = json.dumps(allowed), state
        names = list(settings)
        with psycopg.connect(self.dsn) as pg:
            with pg.transaction():
                pg.execute("SELECT " + ", ".join("set_config(%s, %s, true)" for _ in names)
                           + ", set_config('search_path', %s, true)",
                           [x for n in names for x in (n, settings[n])]
                           + [f"{sql_views.guarded_schema()}, {sql_views.person_schema()}, "
                              f"{bv.SCHEMA}, pg_catalog"])
                return json.loads(served._comms(pg, "record", KEY, None, settings)), \
                    bool(tool._files_reader(actor))

    def test_every_kind_is_read_or_said_off_with_its_reason(self):
        self.index()
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            # one message of every other kind names the same record
            for view, at in (("attachments", "seen_at"), ("teams_messages", "created_at"),
                             ("sharepoint_files", "modified_at")):
                pg.execute(f"INSERT INTO {bv.SCHEMA}.record_comms SELECT %s, %s, handle, "
                           f"{at}, 'x' FROM {bv.SCHEMA}.{view}", (KEY, view))
        for name, actor in (("OWNER", tmp.OWNER), ("MID", tmp.MID)):
            got, files_full = self.kinds(actor)
            for view in ("emails", "attachments", "teams_messages"):
                self.assertIn(view, got, (name, sorted(got)))
                self.assertEqual(got[view]["total"], len(got[view]["items"]) if
                                 got[view]["next"] is None else got[view]["total"], (name, view))
            # a full reader by the door alone, any other through the item gate's allowed set:
            # both read the kind (never typed off while the host has an item gate)
            self.assertIn("sharepoint_files", got, (name, files_full))
            self.assertNotIn("sharepoint_files", got.get("off", {}))

    def test_a_file_the_item_gate_denies_is_absent_from_the_page_for_a_non_full_reader(self):
        """The SQL keeps a file only for a full reader or when its handle is in the allowed set
        the door decided (plx.reader_files_ok): a linked file the gate denies is not returned."""
        person = "tmp_files_person"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute(f"DROP SCHEMA IF EXISTS {person} CASCADE")
            pg.execute(f"CREATE SCHEMA {person}")
            pg.execute(f"CREATE TABLE {person}.record_comms (record_id text, comm_view text, "
                       "comm_handle text, at timestamptz, via text)")
            pg.execute(f"CREATE TABLE {person}.sharepoint_files (handle text, name text, path text)")
            for i in range(3):
                pg.execute(f"INSERT INTO {person}.record_comms VALUES (%s, 'sharepoint_files', %s, "
                           "now() - make_interval(secs => %s), 'x')", (KEY, f"sharepoint://f/{i}", i))
                pg.execute(f"INSERT INTO {person}.sharepoint_files VALUES (%s, %s, '/p')",
                           (f"sharepoint://f/{i}", f"file {i}"))
            sql = served.comms_sql(person, ["sharepoint_files"])
            base = {"key": KEY, "view": None, "after_at": None, "after_h": None, "lost": "[]",
                    "off": "{}", "failed": "{}"}

            def read(files, ok):
                with pg.transaction():
                    pg.execute("SELECT set_config('plx.reader_files', %s, true), "
                               "set_config('plx.reader_files_ok', %s, true)", (files, ok))
                    got = json.loads(pg.execute(sql, base).fetchone()[0])["sharepoint_files"]
                return sorted(i["handle"] for i in got["items"]), got["total"]
            self.assertEqual(read("search", json.dumps(["sharepoint://f/1"])),
                             (["sharepoint://f/1"], 1))                      # the denied two absent
            self.assertEqual(read("search", "[]"), ([], 0))                  # the gate allowed none
            self.assertEqual(read("search", ""), ([], 0))                    # no answer: none
            self.assertEqual(len(read("full", "[]")[0]), 3)                  # a full reader: all
            pg.execute(f"DROP SCHEMA {person} CASCADE")

    def test_the_allowed_linked_files_are_what_the_hosts_item_gate_lets_this_reader_open(self):
        from operating import page_reads as pr
        self.index()
        files = [f"sharepoint://site/{i}" for i in range(5)]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for h in files:
                pg.execute(f"INSERT INTO {bv.SCHEMA}.record_comms VALUES (%s, 'sharepoint_files', %s, "
                           "now(), 'x') ON CONFLICT DO NOTHING", (KEY, h))
        tool = self.tool()
        reads = pr.PageReads(tool.base)
        calls = []
        tool.base.item_gate = lambda actor, h: calls.append(h) or h.endswith(("/1", "/3"))
        self.assertEqual(reads._allowed_files(self.dsn, tmp.MID), (files[1:2] + files[3:4], ""))
        self.assertEqual(sorted(calls), sorted(files))             # the real gate asked of each file
        tool.base.item_gate = lambda actor, h: (_ for _ in ()).throw(RuntimeError("gate down"))
        # undecidable: not allowed, and the kind says it failed (never "0 files"), logged
        with self.assertLogs("operating.page_reads", "WARNING") as logged:
            self.assertEqual(reads._allowed_files(self.dsn, tmp.MID), ([], "gate_error"))
        self.assertIn("item gate failed on 5 of 5", logged.output[0])
        from operator_api import served
        with psycopg.connect(self.dsn) as pg:
            got = json.loads(served._comms(pg, "record", KEY, None, {
                bc.FILES_SETTING: bc.FILES_SEARCH, pr.FILES_OK: "[]", pr.FILES_STATE: "gate_error"}))
        self.assertEqual((got["status"], got["failed"]), ("failed", {"sharepoint_files": "gate_error"}))
        self.assertNotIn("sharepoint_files", got)

    def index(self):
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute(f"CREATE SCHEMA IF NOT EXISTS {bv.SCHEMA}_person")
            rc.ensure(pg, bv.SCHEMA)
            pg.execute(f"DELETE FROM {bv.SCHEMA}.record_comms")
            pg.execute(f"INSERT INTO {bv.SCHEMA}.record_comms SELECT %s, 'emails', handle, "
                       f"received_at, 'x' FROM {bv.SCHEMA}.emails", (KEY,))
            return pg.execute(f"SELECT count(*) FROM {bv.SCHEMA}.emails").fetchone()[0]

    def test_each_readers_linked_mail_is_what_the_guarded_view_lets_that_reader_read(self):
        every = self.index()
        self.assertGreater(every, 3)
        seen = {}
        for name, actor in (("OWNER", tmp.OWNER), ("MID", tmp.MID), ("NARROW", tmp.NARROW)):
            got = self.linked(actor)
            handles = {i["handle"] for i in got["items"]}
            # the person view reads what plx.sql's AI view reads, plus the rows the AI is kept
            # from (ai_ok false: a person may read them), and nothing else
            ai = self.handles(actor)
            self.assertTrue(ai <= handles, name)
            self.assertEqual(handles - ai, handles & self.not_ai(), name)
            self.assertEqual(got["total"], len(handles))
            self.assertIsNone(got["next"])
            seen[name] = handles
        self.assertLess(len(seen["NARROW"]), len(seen["MID"]))
        owner = {i["handle"] for i in self.linked(tmp.OWNER, owner=True)["items"]}
        ai_owner = self.handles(tmp.OWNER, owner=True)
        self.assertTrue(ai_owner <= owner)
        self.assertEqual(owner - ai_owner, owner & self.not_ai())
        self.assertTrue(owner - seen["OWNER"], "the fixture's set holds mail the owner alone reads")
        for name in ("MID", "NARROW"):
            self.assertFalse(seen[name] & (owner - seen["OWNER"]))

    def cites(self, actor, handles, owner=False):
        """``served._readable_cites`` under ``actor``'s mail settings (the History tab's check)."""
        tool = self.tool(owner)
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            settings = tool._comms_settings(tool.base._scope(actor, tool.base.clock()), actor)
        names = list(settings)
        with psycopg.connect(self.dsn) as pg:
            with pg.transaction():
                pg.execute("SELECT " + ", ".join("set_config(%s, %s, true)" for _ in names),
                           [x for n in names for x in (n, settings[n])])
                return served._readable_cites(pg, sql_views.person_schema(), ["emails"], handles)

    def test_a_history_cite_is_readable_exactly_when_the_readers_mail_view_reads_it(self):
        """The History tab keeps a change resting on mail only if the reader's guarded mail view
        reads that message (one indexed lookup): each reader's readable cites are exactly their
        linked mail, a message's attachment (``#...``) is its message's, the people set's mail is
        the owner's alone, and a handle of no known view is never readable."""
        self.index()
        every = sorted(self.linked(tmp.OWNER, owner=True)["items"], key=lambda i: i["handle"])
        handles = [i["handle"] for i in every]
        cites = handles + [h + "#att=1" for h in handles] + ["original://x/1", "graph-mail://nope/1"]
        for name, actor, owner in (("OWNER", tmp.OWNER, True), ("MID", tmp.MID, False),
                                   ("NARROW", tmp.NARROW, False)):
            seen = {i["handle"] for i in self.linked(actor, owner=owner)["items"]}
            got = self.cites(actor, cites, owner=owner)
            self.assertEqual(got, seen | {h + "#att=1" for h in seen}, name)
        owner_only = {i["handle"] for i in self.linked(tmp.OWNER, owner=True)["items"]} - {
            i["handle"] for i in self.linked(tmp.OWNER)["items"]}
        self.assertTrue(owner_only)
        for actor in (tmp.MID, tmp.NARROW):
            self.assertFalse(self.cites(actor, sorted(owner_only)))
        counts = {}
        for n in (1, 10, 60):   # the same statements whatever the number of cites
            calls = []
            real = psycopg.Connection.execute

            def counting(self_, query, *a, **k):
                calls.append(str(query))
                return real(self_, query, *a, **k)
            with mock.patch.object(psycopg.Connection, "execute", counting):
                self.cites(tmp.MID, (cites * 60)[:n])
            counts[n] = len(calls)
            self.assertEqual(sum("= ANY" in q for q in calls), 1, n)   # one lookup
        self.assertEqual(len(set(counts.values())), 1, counts)

    def test_the_hidden_count_is_the_other_mailboxes_and_never_the_people_sets_mail(self):
        every = self.index()
        with psycopg.connect(self.dsn) as pg:
            people = pg.execute(f"SELECT count(*) FROM {bv.SCHEMA}.emails WHERE excluded").fetchone()[0]
        self.assertTrue(people, "the fixture's set holds mail")
        for name, actor in (("NARROW", tmp.NARROW), ("MID", tmp.MID), ("OWNER", tmp.OWNER)):
            got = self.linked(actor)
            self.assertEqual(got["hidden"], every - people - got["total"], name)
        self.assertGreater(self.linked(tmp.NARROW)["hidden"], 0)
        # Nothing is held back
        owner = self.linked(tmp.OWNER, owner=True)
        self.assertEqual((owner["hidden"], owner["total"]), (0, every))

    def test_paging_reaches_every_message_and_the_count_is_exact(self):
        self.index()
        want = {i["handle"] for i in self.linked(tmp.OWNER)["items"]}
        self.assertGreater(len(want), 4)
        with mock.patch.object(served, "PAGE", 2):   # read when the statement is built
            got, cursor, pages = [], None, 0
            while True:
                page = self.linked(tmp.OWNER, cursor=cursor)
                pages += 1
                got += [i["handle"] for i in page["items"]]
                self.assertEqual(page["total"], len(want))
                cursor = page["next"]
                if cursor is None:
                    break
                self.assertLess(pages, 100)
        self.assertEqual(len(got), len(set(got)))
        self.assertEqual(set(got), want)
        self.assertGreater(pages, 1)


if __name__ == "__main__":
    unittest.main()
