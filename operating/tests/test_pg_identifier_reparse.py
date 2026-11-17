"""An identifier kind that only accepts more text, completed in place
(``operating.pg.identifier_reparse``): only the carrier versions holding no value of it
whose captured value now parses get the value cell and identifier row a projection writes
(the version's lineage and interval, the kind's role and markings); blank, still
unparsed, already held or since replaced versions are left alone; a rerun writes nothing
more; the writes go into the run's undo log and come back out with it; the unit layer's
final step refuses until the re-parse is recorded; a served row the new grammar reads
otherwise fails the check."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from operating.tests import pg_testdb

FROM, TO = "schema-from", "schema-to"
CARRIER = {"kind": "internet_message_id", "type": "Email", "property": "internet_message_id",
           "binding": "graph_mail.messages", "relation": "messages", "source": "graph_mail",
           "path": "internet_message_id", "role": "alias", "markings": []}
# what the old Message-ID pattern rejected (a second @, a colon) and the new one reads
LOOSE = "<a:b@c@example.test>"


class IdentifierReparseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import psycopg
            cls.name, cls.dsn = pg_testdb.create_db()
        except unittest.SkipTest:
            raise
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        from operating.pg.migrate import migrate
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            migrate(pg)

    def setUp(self):
        import psycopg
        self.pg = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.pg.close)
        for table in ("value_cells", "value_sources", "object_versions", "identifiers",
                      "value_fields", "value_kinds", "schema_migrations", "store_meta"):
            self.pg.execute(f"TRUNCATE ontology.{table}")
        self.pg.execute("INSERT INTO ontology.store_meta VALUES ('schema_digest', %s)", (FROM,))
        self.pg.execute("INSERT INTO ontology.value_fields VALUES (7, 'internet_message_id', "
                        "'row.internet_message_id'), (8, 'subject', 'row.subject')")
        self.pg.execute("INSERT INTO ontology.value_kinds (kind, value_type, parser, markings)"
                        " VALUES (3, 'identifier', 'identifier', '[]'), "
                        "(4, 'text', 'as_is', '[]')")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        mail = self.root / "state" / "mail-capture"
        mail.mkdir(parents=True)
        self.capture = sqlite3.connect(mail / "capture.db")
        self.addCleanup(self.capture.close)
        self.capture.execute("CREATE TABLE messages (version_id INTEGER PRIMARY KEY, "
                             "l1_handle TEXT, record TEXT)")
        # ver: (raw Message-ID or absent, cell held already)
        self.versions = {1: ("<ok@example.test>", True), 2: (LOOSE, False), 3: ("  ", False),
                         4: ("not an id <>", False), 5: (None, False)}
        for ver, (raw, held) in self.versions.items():
            oid, handle = f"Email:graph_mail.messages:m{ver}", f"graph-mail://box/m{ver}@k"
            at = f"2026-01-0{ver}T00:00:00Z"
            self.pg.execute("INSERT INTO ontology.object_versions VALUES (%s, %s, %s, 'd', "
                            "'mail', '[]', %s, NULL)", (ver, oid, handle, at))
            # a packed value source holds no digest or lineage: the version row's are read
            self.pg.execute("INSERT INTO ontology.value_sources VALUES (%s, %s, "
                            "'graph_mail.messages', NULL, NULL, %s, NULL)", (100 + ver, ver, at))
            self.pg.execute("INSERT INTO ontology.value_cells (src, field, kind, value) VALUES "
                            "(%s, 8, 4, 'hello')", (100 + ver,))
            if held:
                self.pg.execute("INSERT INTO ontology.value_cells (src, field, kind, value) "
                                "VALUES (%s, 7, 3, 'ok@example.test')", (100 + ver,))
                self.pg.execute(
                    "INSERT INTO ontology.identifiers VALUES ('internet_message_id', "
                    "'ok@example.test', %s, 'alias', %s, 'internet_message_id', %s, "
                    "'row.internet_message_id', 'mail', '[]', %s, NULL)",
                    (raw, oid, handle, at))
            record = {"subject": "hello"} if raw is None else {"internet_message_id": raw}
            # an older interval of the same handle: the latest is read
            self.capture.execute("INSERT INTO messages (l1_handle, record) VALUES (?, ?)",
                                 (handle, json.dumps({"internet_message_id": "<old@x>"})))
            self.capture.execute("INSERT INTO messages (l1_handle, record) VALUES (?, ?)",
                                 (handle, json.dumps(record)))
        self.capture.commit()
        self.plan = {"from": FROM, "to": TO, "id": "p", "identifier_reparse": [CARRIER]}

    def stage(self):
        from operating.pg import identifier_reparse as ir
        out = self.root / "stage.sqlite"
        return ir.stage(self.plan, self.root / "state", out, self.dsn), out

    def test_only_the_newly_parsed_versions_get_their_rows(self):
        from operating.pg import identifier_reparse as ir
        from operating.pg import ontology_migrate as om
        self.assertTrue(ir.check(self.plan, self.dsn, workers=2)["ok"])
        report, out = self.stage()
        carrier = report["carriers"][0]
        self.assertEqual((carrier["missing"], carrier["parsed"], carrier["blank_or_unparsed"],
                          carrier["path_absent"]), (4, 1, 2, 1))
        with self.pg.cursor() as cur:
            self.assertEqual(om.reparse_pending(cur, {**self.plan}),
                             ["internet_message_id:graph_mail.messages"])
        done = ir.apply(out, self.dsn)
        self.assertEqual((done["cells"], done["identifiers"]), (1, 1))
        cells = self.pg.execute("SELECT src, kind, value FROM ontology.value_cells WHERE "
                                "field = 7 ORDER BY src").fetchall()
        self.assertEqual(cells, [(101, 3, "ok@example.test"), (102, 3, "a:b@c@example.test")])
        row = self.pg.execute(
            "SELECT normalized, raw, role, object_id, l1_handle, field_path, lineage, markings,"
            " recorded_at, superseded_at FROM ontology.identifiers WHERE raw = %s",
            (LOOSE,)).fetchone()
        self.assertEqual(row, ("a:b@c@example.test", LOOSE, "alias",
                               "Email:graph_mail.messages:m2", "graph-mail://box/m2@k",
                               "row.internet_message_id", "mail", "[]",
                               "2026-01-02T00:00:00Z", None))
        with self.pg.cursor() as cur:
            self.assertEqual(om.reparse_pending(cur, self.plan), [])
        # a rerun (or a resumed run over a fresh stage) writes nothing more
        again = ir.apply(self.stage()[1], self.dsn)
        self.assertEqual((again["cells"], again["identifiers"], again["skipped"]), (0, 0, 0))
        self.assertTrue(ir.check(self.plan, self.dsn, workers=2)["ok"])

    def test_a_replaced_version_and_another_schema_are_left_alone(self):
        from operating.pg import identifier_reparse as ir
        _, out = self.stage()
        # the delta replaced the version since the stage: its source is gone
        self.pg.execute("DELETE FROM ontology.value_cells WHERE src = 102")
        self.pg.execute("DELETE FROM ontology.value_sources WHERE src = 102")
        done = ir.apply(out, self.dsn)
        self.assertEqual((done["cells"], done["skipped"]), (0, 1))
        self.pg.execute("UPDATE ontology.store_meta SET value = 'other' "
                        "WHERE key = 'schema_digest'")
        with self.assertRaisesRegex(ValueError, "served_schema_differs"):
            ir.apply(out, self.dsn)

    def test_the_writes_are_undone_with_the_runs_log(self):
        from operating.pg import identifier_reparse as ir
        from operating.pg import migrate_chunks as mc
        _, out = self.stage()
        with self.assertRaisesRegex(ValueError, "undo_log_not_of_the_running_migration"):
            ir.apply(out, self.dsn, undo_log="undo_reparse_t")
        with self.pg.transaction(), self.pg.cursor() as cur:
            mc.install_undo_log(cur, "undo_reparse_t")
            cur.execute("INSERT INTO ontology.store_meta VALUES (%s, %s)",
                        (mc.FLAG, json.dumps({"key": "k", "undo": "undo_reparse_t"})))
        self.addCleanup(self._drop_log)
        done = ir.apply(out, self.dsn, undo_log="undo_reparse_t")
        self.assertEqual(done["cells"], 1)
        undone = mc.undo_migration(self.dsn, "undo_reparse_t")
        self.assertTrue(undone["undone"], undone)
        self.assertEqual(self.pg.execute("SELECT count(*) FROM ontology.value_cells WHERE "
                                         "field = 7").fetchone()[0], 1)
        self.assertEqual(self.pg.execute("SELECT count(*) FROM ontology.identifiers"
                                         ).fetchone()[0], 1)
        self.assertEqual(self.pg.execute("SELECT count(*) FROM ontology.schema_migrations"
                                         ).fetchone()[0], 0)

    def _drop_log(self):
        from operating.pg import migrate_chunks as mc
        with self.pg.transaction(), self.pg.cursor() as cur:
            mc.drop_undo_triggers(cur, "undo_reparse_t")
            cur.execute("DROP SCHEMA IF EXISTS undo_reparse_t CASCADE")

    def test_a_served_row_the_new_grammar_reads_otherwise_fails_the_check(self):
        from operating.pg import identifier_reparse as ir
        self.pg.execute("UPDATE ontology.identifiers SET normalized = 'OK@example.test'")
        out = ir.check(self.plan, self.dsn, workers=2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["kinds"]["internet_message_id"]["reparsed_unequal"], 1)

    def test_a_carrier_the_phase_cannot_read_is_refused(self):
        from operating.pg import identifier_reparse as ir
        plan = {**self.plan, "identifier_reparse": [{**CARRIER, "source": "bc"}]}
        with self.assertRaisesRegex(ValueError, "reparse_source_unreadable"):
            ir.stage(plan, self.root / "state", self.root / "x.sqlite", self.dsn)


class ReparseCarrierTests(unittest.TestCase):
    """``step_migrate.reparse_carriers``: the Message-ID kind of this schema qualifies; a
    kind a link compares by does not (its links would move)."""

    def test_the_message_id_qualifies_and_a_compared_kind_does_not(self):
        from ontology.schema import load_schema
        from ontology import step_migrate as sm
        schema = load_schema()
        got = sm.reparse_carriers(schema, "internet_message_id")
        self.assertEqual([(c["type"], c["binding"], c["role"]) for c in got],
                         [("Email", "graph_mail.messages", "alias")])
        compared = next(p["identifier_kind"] for link in schema.link_types.values()
                        for lb in link["bindings"] for p in lb.get("pairs", ())
                        if p.get("identifier_kind"))
        self.assertIsNone(sm.reparse_carriers(schema, compared))

    def test_a_kind_read_from_text_is_re_projected(self):
        """A kind a mention profile reads (or another kind aliases) would need the mention
        scope to cover the keys it newly parses: never the set-based phase."""
        import json
        from ontology.schema import load_schema
        from ontology import identifiers, step_migrate as sm
        schema = load_schema()
        grammar = json.loads(identifiers.SCHEMA_PATH.read_text(encoding="utf-8"))
        for change in ("profile", "alias"):
            with self.subTest(change=change):
                g = json.loads(json.dumps(grammar))
                if change == "profile":
                    g.setdefault("mention_profiles", {})["probe"] = {"patterns": [
                        {"kind": "internet_message_id", "pattern": "<([^>]+)>", "group": 1}]}
                else:
                    other = next(k for k in g["kinds"] if k != "internet_message_id")
                    g["kinds"][other].setdefault("alias_kinds", []).append("internet_message_id")
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "identifiers.json"
                    path.write_text(json.dumps(g), encoding="utf-8")
                    with mock.patch.object(identifiers, "SCHEMA_PATH", path):
                        self.assertIsNone(sm.reparse_carriers(schema, "internet_message_id"))
        self.assertIsNotNone(sm.reparse_carriers(schema, "internet_message_id"))


if __name__ == "__main__":
    unittest.main()
