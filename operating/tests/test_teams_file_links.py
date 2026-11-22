"""The file a Teams message shares, linked to the captured file it is
(``operating.pg.teams_file_links``).

Checked at the boundary: the two exact rules (a webUrl equal after one percent-decoding; a
personal OneDrive's owner + stored path), an address two files hold links nothing, an
address no file holds links nothing, query-string or case variants are not matched; the
capture is read from a real SQLite capture shaped file; the table is written from stand-in
business tables carrying both ends' door columns, a second run is the same rows, a link no
longer derived goes, a missing table refuses before any work; the served view shows a link
only when the reader's guarded message view AND guarded file view both show their row
(either end hidden: no row), 1/10/60 links per message are counted exactly.

Evidence label: isolated-fixture (a scratch Postgres with stand-in guarded views, a made-up
capture; the real door of the two views is theirs, tested with them)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from operating import ontology_tools_sql as ts
from operating.pg import comms_findings
from operating.pg import teams_file_links as tfl
from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

D = "b!drive1"
OWNER = "https://tenant-my.sharepoint.com/personal/ann_example_com"


def fid(item):
    return tfl.file_object_id(D, item)


def make_capture(path: Path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE items (drive_id TEXT, item_id TEXT, path TEXT, record TEXT, "
                "superseded_at TEXT, removed_reason TEXT)")
    for item, p, url, old in rows:
        con.execute("INSERT INTO items VALUES (?, ?, ?, ?, ?, NULL)",
                    (D, item, p, json.dumps({"original_url": url}), "2026-01-01" if old else None))
    con.commit()
    con.close()


class Derivation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "capture.db"
        make_capture(self.db, [
            ("A", "/Docs/a b.docx", "https://t.sharepoint.com/sites/S/Docs/a%20b.docx", False),
            ("B", "/Microsoft Teams Chat Files/x.png",
             OWNER + "/_layouts/15/Doc.aspx?sourcedoc=%7B1%7D", False),
            ("C1", "/Docs/dup.pdf", "https://t.sharepoint.com/sites/S/Docs/dup.pdf", False),
            ("C2", "/Docs/dup.pdf", "https://t.sharepoint.com/sites/S/Docs/dup.pdf", False),
            ("OLD", "/Docs/old.pdf", "https://t.sharepoint.com/sites/S/Docs/old.pdf", True),
            ("N", "/Docs/none.pdf", None, False)])
        self.addCleanup(self.tmp.cleanup)

    def test_exact_rules_and_what_links_nothing(self):
        exact, rebuilt = tfl.capture_addresses(self.db)
        msgs = {
            "M1": {tfl.decode("https://t.sharepoint.com/sites/S/Docs/a b.docx")},
            "M2": {tfl.decode(OWNER + "/Documents/Microsoft Teams Chat Files/x.png")},
            "M3": {tfl.decode("https://t.sharepoint.com/sites/S/Docs/dup.pdf")},
            "M4": {tfl.decode("https://t.sharepoint.com/sites/S/Docs/old.pdf")},
            "M5": {tfl.decode("https://t.sharepoint.com/sites/S/Docs/A%20B.docx")},
            "M6": {tfl.decode("https://t.sharepoint.com/sites/S/Docs/a%20b.docx?web=1")},
            "M7": {tfl.decode("https://t.sharepoint.com/sites/S/Docs/missing.docx")}}
        links, stats = tfl.derive(msgs, exact, rebuilt)
        self.assertEqual({k: v[1] for k, v in links.items()},
                         {("M1", fid("A")): "url_exact", ("M2", fid("B")): "url_owner_path"})
        self.assertEqual(stats, {"addresses": 7, "matched": 2, "ambiguous": 1, "links": 2,
                                 "unmatched": 4})

    def test_not_an_address_is_nothing(self):
        for bad in (None, 5, "", "ftp://x", "/relative/path"):
            self.assertIsNone(tfl.decode(bad))


@unittest.skipIf(psycopg is None, "psycopg not installed")
class Table(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.pg = psycopg.connect(cls.dsn, autocommit=True)
        cls.migration = (Path(tfl.__file__).parent / "migrations"
                         / "0055_teams_file_links.sql").read_text()
        for stmt in (
            "CREATE SCHEMA biz", "CREATE SCHEMA biz_sql",
            "CREATE TABLE biz.teams_messages (handle text, object_ids text[], "
            "audience_keys text[], rec_marks text[], col_marks jsonb, ai_ok boolean, "
            "hidden boolean DEFAULT false)",
            "CREATE TABLE biz.sharepoint_files (handle text, object_ids text[], "
            "audience_keys text[], rec_marks text[], col_marks jsonb, ai_ok boolean, "
            "name text, path text, mime_type text, size numeric, hidden boolean DEFAULT false)",
            # the guarded views: a row the reader may not read is not shown (stand-in door)
            "CREATE VIEW biz_sql.teams_messages AS SELECT handle FROM biz.teams_messages "
            "WHERE NOT hidden",
            "CREATE VIEW biz_sql.sharepoint_files AS SELECT handle, name, path, mime_type, "
            "size FROM biz.sharepoint_files WHERE NOT hidden"):
            cls.pg.execute(stmt)

    @classmethod
    def tearDownClass(cls):
        cls.pg.close()
        pg_testdb.drop_db(cls.name)

    def setUp(self):
        self.pg.execute("DROP VIEW IF EXISTS biz_sql.teams_message_files")
        self.pg.execute("DROP TABLE IF EXISTS biz.teams_file_links")
        self.pg.execute("TRUNCATE biz.teams_messages, biz.sharepoint_files")
        self.pg.execute("DROP VIEW IF EXISTS biz_sql.sharepoint_files")
        self.pg.execute("CREATE VIEW biz_sql.sharepoint_files AS SELECT handle, name, path, "
                        "mime_type, size FROM biz.sharepoint_files WHERE NOT hidden")

    def seed(self, n_files):
        for i in range(n_files):
            self.pg.execute("INSERT INTO biz.sharepoint_files (handle, object_ids, "
                            "audience_keys, rec_marks, col_marks, ai_ok, name, path) VALUES "
                            "(%s, %s, %s, '{}', '{}', true, %s, %s)",
                            (f"sp://f{i}", [fid(f"F{i}")], ["sharepoint:d"], f"f{i}", f"/f{i}"))
        for m, objs in (("teams://m1", ["TM1", "TeamsChatMessage:TM1"]),
                        ("teams://m2", ["TM2", "TeamsChatMessage:TM2"])):
            self.pg.execute("INSERT INTO biz.teams_messages (handle, object_ids, "
                            "audience_keys, rec_marks, col_marks, ai_ok) VALUES (%s, %s, "
                            "%s, '{}', '{}', true)", (m, objs, ["chat:c"]))

    def links(self, pairs):
        return {(m, f): (f"https://x/{f[-2:]}", "url_exact") for m, f in pairs}

    def test_missing_table_refuses_before_any_work(self):
        with self.assertRaises(RuntimeError) as got:
            tfl.write_links(self.pg, self.links([("TM1", fid("F0"))]), min_free_gb=0)
        self.assertIn("0055", str(got.exception))
        self.assertIsNone(self.pg.execute(
            "SELECT to_regclass('biz.teams_file_links_stage')").fetchone()[0])

    def test_write_is_idempotent_carries_both_doors_and_drops_stale_links(self):
        self.pg.execute(self.migration)
        self.seed(1 + 10 + 60)
        wanted = [("TM1", fid("F0"))] + [("TM2", fid(f"F{i}")) for i in range(1, 71)]
        self.assertEqual(tfl.write_links(self.pg, self.links(wanted), min_free_gb=0), 71)
        self.assertEqual(tfl.write_links(self.pg, self.links(wanted), min_free_gb=0), 71)
        count = lambda where="true": self.pg.execute(  # noqa: E731
            f"SELECT count(*) FROM biz.teams_file_links WHERE {where}").fetchone()[0]
        self.assertEqual(count(), 71)
        for n in (1, 10, 60):   # one message sharing 1, 10 and 60 files
            self.pg.execute("TRUNCATE biz.teams_file_links")
            part = [("TM2", fid(f"F{i}")) for i in range(n)]
            self.assertEqual(tfl.write_links(self.pg, self.links(part), min_free_gb=0), n)
            self.assertEqual(count("message_handle = 'teams://m2'"), n)
        self.assertEqual(self.pg.execute(
            "SELECT msg_audience_keys, file_audience_keys, msg_ai_ok, file_ai_ok FROM "
            "biz.teams_file_links LIMIT 1").fetchone(), (["chat:c"], ["sharepoint:d"], True, True))
        # a link no longer derived goes
        tfl.write_links(self.pg, self.links([("TM2", fid("F0"))]), min_free_gb=0)
        self.assertEqual(count(), 1)
        # a link whose message or file the business tables do not hold is not written
        tfl.write_links(self.pg, self.links([("TM9", fid("F0")), ("TM1", fid("F99"))]),
                        min_free_gb=0)
        self.assertEqual(count(), 0)

    def test_the_view_shows_a_link_only_when_both_ends_are_readable(self):
        self.pg.execute(self.migration)
        self.seed(2)
        tfl.write_links(self.pg, self.links([("TM1", fid("F0")), ("TM1", fid("F1"))]),
                        min_free_gb=0)
        self.assertTrue(tfl.create_view(self.pg))
        seen = lambda: sorted(self.pg.execute(  # noqa: E731
            "SELECT file_handle FROM biz_sql.teams_message_files").fetchall())
        self.assertEqual(seen(), [("sp://f0",), ("sp://f1",)])
        self.pg.execute("UPDATE biz.sharepoint_files SET hidden = true WHERE handle = 'sp://f0'")
        self.assertEqual(seen(), [("sp://f1",)])          # the file end hidden
        self.pg.execute("UPDATE biz.teams_messages SET hidden = true")
        self.assertEqual(seen(), [])                      # the message end hidden
        self.pg.execute("UPDATE biz.teams_messages SET hidden = false")
        self.pg.execute("UPDATE biz.sharepoint_files SET hidden = true")
        self.assertEqual(seen(), [])                      # a reader who reads no file
        # without the table or a guarded view there is no view at all (fail closed)
        self.pg.execute("DROP VIEW biz_sql.sharepoint_files CASCADE")
        self.assertFalse(tfl.create_view(self.pg))
        self.assertIsNone(self.pg.execute(
            "SELECT to_regclass('biz_sql.teams_message_files')").fetchone()[0])

    def test_a_scoped_run_reads_only_new_messages_and_removes_nothing(self):
        self.pg.execute(self.migration)
        self.seed(2)
        self.pg.execute("CREATE SCHEMA IF NOT EXISTS ontology")
        self.pg.execute("DROP TABLE IF EXISTS ontology.object_values")
        self.pg.execute("CREATE TABLE ontology.object_values (object_id text, property text, "
                        "value text)")
        for oid, url in (("TeamsChatMessage:TM1", "https://t.sharepoint.com/a.docx"),
                         ("TeamsChatMessage:TM2", "https://t.sharepoint.com/b.docx")):
            self.pg.execute("INSERT INTO ontology.object_values VALUES (%s, 'attachments', %s)",
                            (oid, json.dumps([{"content_url": url}])))
        self.assertEqual(set(tfl.shared_addresses(self.pg)),
                         {"TeamsChatMessage:TM1", "TeamsChatMessage:TM2"})
        tfl.write_links(self.pg, self.links([("TeamsChatMessage:TM1", fid("F0"))]),
                        min_free_gb=0)
        self.assertEqual(set(tfl.shared_addresses(self.pg, only_new=True)),
                         {"TeamsChatMessage:TM2"})   # TM1 has a link row now
        # a scoped write adds and never removes
        tfl.write_links(self.pg, self.links([("TeamsChatMessage:TM2", fid("F1"))]),
                        keep=True, min_free_gb=0)
        self.assertEqual(self.pg.execute("SELECT count(*) FROM biz.teams_file_links"
                                         ).fetchone()[0], 2)

    def test_the_findings_run_survives_a_failed_link_pass(self):
        with mock.patch.object(tfl, "run", side_effect=RuntimeError("biz.teams_file_links "
                                                                    "is missing")):
            comms_findings._link_new_files("dsn")   # logs, never raises

    def test_an_opened_teams_message_lists_the_files_its_reader_may_read(self):
        self.pg.execute(self.migration)
        self.seed(2)
        tfl.write_links(self.pg, self.links([("TM1", fid("F0")), ("TM1", fid("F1"))]),
                        min_free_gb=0)
        tool = ts.SqlTool.__new__(ts.SqlTool)
        shared = lambda: [f["citation"] for f in tool._shared_files(  # noqa: E731
            self.pg, "biz_sql", "teams://m1")]
        self.assertEqual(shared(), [])                    # no view yet: none, no error
        tfl.create_view(self.pg)
        self.assertEqual(shared(), ["sp://f0", "sp://f1"])
        first = tool._shared_files(self.pg, "biz_sql", "teams://m1")[0]
        self.assertEqual(first["name"]["untrusted_text"], "f0")
        self.assertEqual(first["linked_by"], "url_exact")
        self.pg.execute("UPDATE biz.sharepoint_files SET hidden = true WHERE handle = 'sp://f0'")
        self.assertEqual(shared(), ["sp://f1"])           # a file the reader may not read
        self.pg.execute("UPDATE biz.sharepoint_files SET hidden = true")
        self.assertEqual(shared(), [])


if __name__ == "__main__":
    unittest.main()
