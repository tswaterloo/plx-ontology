"""C-09 applied in place (``operating.pg.timestamp_rezone``): only the timestamp cells and
event times of the plan's carriers change, each to the projection's own parse of its
stored text under the declared zone; a zoneless Prisma stamp becomes its UTC instant, a
FileMaker wall time becomes the Eastern wall time's instant, an instant the source wrote
with a zone stays, a wall time in the DST change hour is deleted (with its event's
attachments), other properties and bindings are untouched; a rerun writes nothing more;
a cell a delta rewrote since the stage is skipped; the writes go into the run's undo log
and come back out with it, or back through the stage's own undo; the unit layer's final
step and the plain apply refuse until the rewrite is recorded; check refuses a wall-clock
relation whose captures carry another offset, and re-reads served cells from the mirror."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from operating.tests import pg_testdb

FROM, TO = "schema-from", "schema-to"
NAIVE = {"type": "Quote", "property": "created_at", "binding": "gen.public.app_quote",
         "relation": "public.app_quote", "path": "created_at", "zone": {"naive_zone": "UTC"}}
WALL = {"type": "SalesOrder", "property": "fm_modified_at", "binding": "portal.fm_order_entry",
        "relation": "public.order_entry", "path": "audittimestampmodified",
        "zone": {"wall_clock_zone": "America/New_York"}}
EVENT = {"event_type": "SalesOrder.fm_modified", "type": "SalesOrder",
         "binding": "portal.fm_order_entry", "relation": "public.order_entry",
         "path": "audittimestampmodified", "parser": None,
         "zone": {"wall_clock_zone": "America/New_York"}}
PLAN = {"id": "p", "from": FROM, "to": TO, "units": {"affected": []},
        "timestamp_rezone": {"values": [NAIVE, WALL], "events": [EVENT]}}


class RezoneTextTests(unittest.TestCase):

    def test_the_projection_own_parse_of_the_stored_text(self):
        from operating.pg.timestamp_rezone import rezoned
        utc, ny = {"naive_zone": "UTC"}, {"wall_clock_zone": "America/New_York"}
        self.assertEqual(rezoned("2026-04-15T20:58:49.864000", utc),
                         ("changed", "2026-04-15T20:58:49.864000Z"))
        self.assertEqual(rezoned("2026-04-15T20:58:49.864000Z", utc),
                         ("same", "2026-04-15T20:58:49.864000Z"))
        # EDT in June (-4), EST in November (-5)
        self.assertEqual(rezoned("2025-06-17T09:46:36.000000Z", ny),
                         ("changed", "2025-06-17T13:46:36.000000Z"))
        self.assertEqual(rezoned("2025-11-17T09:46:36.000000Z", ny),
                         ("changed", "2025-11-17T14:46:36.000000Z"))
        # the repeated hour (Nov 2 2025, 1-2 AM) and the skipped one (Mar 9 2025, 2-3 AM)
        self.assertEqual(rezoned("2025-11-02T01:30:00.000000Z", ny), ("ambiguous", None))
        self.assertEqual(rezoned("2025-03-09T02:30:00.000000Z", ny), ("ambiguous", None))
        self.assertEqual(rezoned("2025-06-17", ny), ("unparsed", "2025-06-17"))
        self.assertEqual(rezoned(None, ny), ("same", None))

    def test_the_wall_proof_counts_every_written_offset(self):
        from operating.pg.timestamp_rezone import wall_proof
        mirror = sqlite3.connect(":memory:")
        for table in ("row_current", "row_version"):
            mirror.execute(f"CREATE TABLE {table} (relation TEXT, row_json TEXT)")
        values = {"2025-06-17T09:46:36Z": "utc", "2025-06-17T09:46:36+00": "utc",
                  "2025-06-17T09:46:36.5+00:00": "utc", "2025-06-17T09:46:36": "utc",
                  "2025-06-17": "utc", "2025-06-17T09:46:36+05": "other",
                  "2025-06-17T09:46:36-04:00": "other", "2025-06-17T09:46-0500": "other"}
        for n, raw in enumerate(values):
            mirror.execute(f"INSERT INTO {('row_current', 'row_version')[n % 2]} VALUES "
                           "('r', ?)", (json.dumps({"t": raw}),))
        mirror.execute("INSERT INTO row_current VALUES ('other', ?)",
                       (json.dumps({"t": "2025-06-17T09:46:36+05"}),))
        out = wall_proof(mirror, "r", "t")
        self.assertEqual((out["values"], out["utc_or_none"], out["other_offset"]), (8, 5, 3))

    def test_a_carrier_without_one_known_zone_is_refused(self):
        from operating.pg.timestamp_rezone import carriers
        for zone in ({}, {"naive_zone": "UTC", "wall_clock_zone": "America/New_York"},
                     {"zone": "UTC"}):
            with self.subTest(zone=zone), self.assertRaises(ValueError):
                carriers({"timestamp_rezone": {"values": [{**NAIVE, "zone": zone}]}})


class TimestampRezoneTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        from operating.pg.migrate import migrate
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            migrate(pg)

    def setUp(self):
        import psycopg
        self.pg = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.pg.close)
        for table in ("value_cells", "value_sources", "object_versions", "value_fields",
                      "value_kinds", "schema_migrations", "store_meta", "events",
                      "event_objects"):
            self.pg.execute(f"TRUNCATE ontology.{table}")
        self.pg.execute("INSERT INTO ontology.store_meta VALUES ('schema_digest', %s)", (FROM,))
        self.pg.execute("INSERT INTO ontology.value_fields VALUES (1, 'created_at', "
                        "'row.created_at'), (2, 'fm_modified_at', 'row.audittimestampmodified'),"
                        " (3, 'title', 'row.title'), (4, 'created_at', 'row.updated_at')")
        self.pg.execute("INSERT INTO ontology.value_kinds (kind, value_type, parser, markings)"
                        " VALUES (5, 'timestamp', 'iso_timestamp', '[]'), "
                        "(6, 'text', 'as_is', '[]')")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        mirror_dir = self.root / "state" / "case-runtime"
        mirror_dir.mkdir(parents=True)
        self.mirror = sqlite3.connect(mirror_dir / "source-mirror.sqlite")
        self.addCleanup(self.mirror.close)
        self.mirror.execute("CREATE TABLE row_current (relation TEXT, key_json TEXT, "
                            "run_index INTEGER, capture_id INTEGER, row_json TEXT)")
        self.mirror.execute("CREATE TABLE row_version (relation TEXT, key_json TEXT, "
                            "run_index INTEGER, capture_id INTEGER, row_json TEXT)")
        # src: (binding, field, stored text, captured raw)
        self.cells = {
            11: ("gen.public.app_quote", 1, "2026-04-15T20:58:49.864000",
                 "2026-04-15T20:58:49.864"),                               # naive: changes
            12: ("gen.public.app_quote", 1, "2026-04-15T20:58:49.864000Z",
                 "2026-04-15T20:58:49.864Z"),                              # had a zone: same
            13: ("gen.public.app_quote", 3, "2026-04-15T20:58:49.864000", None),  # not a stamp
            14: ("other.binding", 1, "2026-04-15T20:58:49.864000", None),  # other binding
            21: ("portal.fm_order_entry", 2, "2025-06-17T09:46:36.000000Z",
                 "2025-06-17T09:46:36Z"),                                  # wall: changes
            22: ("portal.fm_order_entry", 2, "2025-11-02T01:30:00.000000Z",
                 "2025-11-02T01:30:00Z"),                                  # DST hour: deleted
        }
        for src, (binding, field, stored, raw) in self.cells.items():
            relation = {"gen.public.app_quote": "public.app_quote",
                        "portal.fm_order_entry": "public.order_entry"}.get(binding, "x.y")
            key = json.dumps({"id": src}, separators=(",", ":"))
            from integrations.source_mirror import make_citation
            handle = make_citation(relation, {"id": src}, 7)
            at = "2026-01-01T00:00:00.000000Z"
            self.pg.execute("INSERT INTO ontology.object_versions VALUES (%s, %s, %s, 'd', "
                            "'l', '[]', %s, NULL)", (src, f"T:{binding}:{src}", handle, at))
            self.pg.execute("INSERT INTO ontology.value_sources VALUES (%s, %s, %s, NULL, NULL,"
                            " %s, NULL)", (src, src, binding, at))
            self.pg.execute("INSERT INTO ontology.value_cells (src, field, kind, value) VALUES "
                            "(%s, %s, %s, %s)", (src, field, 5 if field != 3 else 6, stored))
            if raw is not None:
                path = "created_at" if field == 1 else "audittimestampmodified"
                self.mirror.execute("INSERT INTO row_current VALUES (?, ?, 0, 7, ?)",
                                    (relation, key, json.dumps({path: raw})))
        self.mirror.commit()
        for eid, time in (("E:1", "2025-06-17T09:46:36.000000Z"),
                          ("E:2", "2025-11-02T01:30:00.000000Z"),
                          ("E:3", "2025-06-17")):
            self.pg.execute("INSERT INTO ontology.events VALUES (%s, 'SalesOrder.fm_modified', "
                            "'source_stated', %s, NULL, NULL, 'h', "
                            "'row.audittimestampmodified', 'd', 'l', '[]', %s, NULL)",
                            (eid, time, "2026-01-01T00:00:00.000000Z"))
            self.pg.execute("INSERT INTO ontology.event_objects VALUES (%s, 'SalesOrder:x', "
                            "'self')", (eid,))
        self.planfile = self.root / "plan.json"
        self.planfile.write_text(json.dumps(PLAN))
        self.stage = self.root / "stage.sqlite"

    def served(self):
        cells = dict(self.pg.execute("SELECT src, value FROM ontology.value_cells").fetchall())
        events = dict(self.pg.execute("SELECT event_id, time FROM ontology.events").fetchall())
        objects = sorted(r[0] for r in self.pg.execute(
            "SELECT event_id FROM ontology.event_objects").fetchall())
        return cells, events, objects

    def test_check_stage_apply_rerun_and_undo(self):
        from operating.pg import timestamp_rezone as tr
        before = self.served()
        checked = tr.check(PLAN, self.dsn, self.root / "state")
        self.assertTrue(checked["ok"], checked)
        naive, wall = checked["values"]
        self.assertEqual((naive["changed"], naive["same"], naive["cells"]), (1, 1, 2))
        self.assertEqual((wall["changed"], wall["ambiguous"]), (1, 1))
        self.assertEqual(wall["wall_proof"]["other_offset"], 0)
        self.assertEqual(naive["sample"]["unequal"], 0)
        self.assertEqual(naive["sample"]["equal"], 2)
        self.assertEqual(wall["sample"]["equal"], 2)
        self.assertEqual(checked["events"][0]["unparsed"], 1)   # a date stays a date
        staged = tr.stage(PLAN, self.stage, self.dsn)
        self.assertEqual((staged["cells"], staged["event_rows"]), (3, 2))
        self.assertEqual(self.served(), before)   # stage reads only
        done = tr.apply(self.stage, self.dsn)
        self.assertEqual((done["cells_updated"], done["cells_deleted"], done["events_updated"],
                          done["events_deleted"], done["event_objects_deleted"]),
                         (2, 1, 1, 1, 1))
        cells, events, objects = self.served()
        self.assertEqual(cells[11], "2026-04-15T20:58:49.864000Z")
        self.assertEqual(cells[12], before[0][12])
        self.assertEqual(cells[13], before[0][13])
        self.assertEqual(cells[14], before[0][14])
        self.assertEqual(cells[21], "2025-06-17T13:46:36.000000Z")
        self.assertNotIn(22, cells)
        self.assertEqual(events, {"E:1": "2025-06-17T13:46:36.000000Z", "E:3": "2025-06-17"})
        self.assertEqual(objects, ["E:1", "E:3"])
        from operating.pg import ontology_migrate as om
        with self.pg.cursor() as cur:
            self.assertEqual(om.rezone_pending(cur, PLAN | {"to": TO}), [])
        again = tr.apply(self.stage, self.dsn)
        self.assertEqual(again["resumed"], 2)
        self.assertEqual(again["cells_updated"] + again["cells_deleted"], 0)
        # a fresh rerun (the chunk records lost) skips every row: none holds its old text
        with sqlite3.connect(self.stage) as db:
            db.execute("DELETE FROM chunks")
        fresh = tr.apply(self.stage, self.dsn)
        self.assertEqual((fresh["cells_updated"], fresh["skipped"]), (0, 5))
        self.assertEqual(self.served()[:2], (cells, events))
        undone = tr.undo(self.stage, self.dsn)
        self.assertEqual((undone["cells_restored"], undone["cells_reinserted"],
                          undone["events_restored"], undone["events_reinserted"]),
                         (2, 1, 1, 1))
        self.assertEqual(self.served(), before)
        with self.pg.cursor() as cur:
            self.assertEqual(len(om.rezone_pending(cur, PLAN | {"to": TO})), 3)

    def test_a_cell_a_delta_rewrote_since_the_stage_is_left(self):
        from operating.pg import timestamp_rezone as tr
        tr.stage(PLAN, self.stage, self.dsn)
        self.pg.execute("UPDATE ontology.value_cells SET value = '2026-05-01T00:00:00.000000'"
                        " WHERE src = 11")
        done = tr.apply(self.stage, self.dsn)
        self.assertEqual(done["cells_updated"], 1)   # only 21
        self.assertEqual(self.served()[0][11], "2026-05-01T00:00:00.000000")

    def test_another_served_schema_or_a_foreign_undo_log_is_refused(self):
        from operating.pg import timestamp_rezone as tr
        tr.stage(PLAN, self.stage, self.dsn)
        self.pg.execute("UPDATE ontology.store_meta SET value = 'other' WHERE key = "
                        "'schema_digest'")
        with self.assertRaisesRegex(ValueError, "served_schema_differs"):
            tr.apply(self.stage, self.dsn)
        self.pg.execute("UPDATE ontology.store_meta SET value = %s WHERE key = "
                        "'schema_digest'", (FROM,))
        with self.assertRaisesRegex(ValueError, "undo_log_not_of_the_running_migration"):
            tr.apply(self.stage, self.dsn, undo_log="undo_other")
        self.assertEqual(self.served()[0][11], "2026-04-15T20:58:49.864000")

    def test_a_wall_clock_relation_with_another_offset_fails_the_check(self):
        from operating.pg import timestamp_rezone as tr
        self.mirror.execute("INSERT INTO row_version VALUES ('public.order_entry', '{\"id\":99}',"
                            " 0, 3, ?)", (json.dumps({"audittimestampmodified":
                                                      "2025-06-17T09:46:36-04:00"}),))
        self.mirror.commit()
        checked = tr.check(PLAN, self.dsn, self.root / "state")
        self.assertFalse(checked["ok"])
        self.assertEqual(checked["values"][1]["wall_proof"]["other_offset"], 1)

    def test_a_served_cell_unequal_to_its_capture_fails_the_check(self):
        from operating.pg import timestamp_rezone as tr
        # the capture says 10:46 wall time; the store holds 09:46: not the projection's
        self.mirror.execute("UPDATE row_current SET row_json = ? WHERE key_json = '{\"id\":21}'",
                            (json.dumps({"audittimestampmodified": "2025-06-17T10:46:36Z"}),))
        self.mirror.commit()
        checked = tr.check(PLAN, self.dsn, self.root / "state")
        self.assertFalse(checked["ok"])
        self.assertEqual(checked["values"][1]["sample"]["unequal"], 1)

    def test_the_stage_undo_refuses_once_the_served_schema_moved(self):
        from operating.pg import timestamp_rezone as tr
        tr.stage(PLAN, self.stage, self.dsn)
        tr.apply(self.stage, self.dsn)
        self.pg.execute("UPDATE ontology.store_meta SET value = %s WHERE key = "
                        "'schema_digest'", (TO,))
        with self.assertRaisesRegex(ValueError, "served_schema_differs"):
            tr.undo(self.stage, self.dsn)
        self.assertEqual(self.served()[0][11], "2026-04-15T20:58:49.864000Z")

    def test_the_undo_log_takes_the_rewrite_back(self):
        from operating.pg import migrate_chunks as mc
        from operating.pg import timestamp_rezone as tr
        before = self.served()
        tr.stage(PLAN, self.stage, self.dsn)
        with self.pg.transaction(), self.pg.cursor() as cur:
            mc.install_undo_log(cur, "undo_rezone_t")
            cur.execute("INSERT INTO ontology.store_meta VALUES (%s, %s) ON CONFLICT (key) DO "
                        "UPDATE SET value = EXCLUDED.value",
                        (mc.FLAG, json.dumps({"undo": "undo_rezone_t"})))
        self.addCleanup(self.pg.execute, "DROP SCHEMA IF EXISTS undo_rezone_t CASCADE")
        tr.apply(self.stage, self.dsn, undo_log="undo_rezone_t")
        self.assertNotEqual(self.served(), before)
        self.pg.execute("DELETE FROM ontology.store_meta WHERE key = %s", (mc.FLAG,))
        out = mc.undo_migration(self.dsn, "undo_rezone_t")
        self.assertTrue(out["undone"], out)
        cells, events, objects = self.served()
        self.assertEqual((cells, events, objects), before)


class RezoneGatesTests(unittest.TestCase):

    def test_the_final_chunk_and_the_apply_wait_for_the_rewrite(self):
        from operating.pg import ontology_migrate as om
        cur = mock.MagicMock()
        cur.fetchone.return_value = None
        record = {"to": TO, "timestamp_rezone": {"values": [NAIVE], "events": [EVENT]}}
        self.assertEqual(om.rezone_pending(cur, record),
                         ["created_at:gen.public.app_quote",
                          "SalesOrder.fm_modified:portal.fm_order_entry"])
        cur.fetchone.return_value = (1,)
        self.assertEqual(om.rezone_pending(cur, record), [])
        self.assertEqual(om.rezone_pending(cur, {"to": TO}), [])
        self.assertNotEqual(om.rezone_id(TO, NAIVE), om.rezone_id(TO, {**NAIVE, "path": "x"}))


if __name__ == "__main__":
    unittest.main()
