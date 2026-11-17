"""The collapse guard on a real Postgres: once
``operating.pg.mention_collapse`` folded the store, a step re-run that is not occurrence-aware
(``sender_corroboration`` writes one row per mention occurrence and would lose the folded
twins' rows) is refused by the planner and by every apply path, with the step named; the
occurrence-aware steps still pass, and a store never folded plans exactly as before."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from operating.tests import pg_testdb
from operating.tests.test_pg_mention_collapse import COLS, TYPES, ordered, row

REFUSED = "collapsed_store:sender_parties"


class CollapseGuardTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        from operating.pg.migrate import migrate
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            migrate(pg)
        k, t = ordered("g1", True)   # one mail naming one order twice: a keeper and a twin
        for r in (row(k, "Email:m:1", "PurchaseOrder:a:1", "PO907711", [4, 12], "g1a"),
                  row(t, "Email:m:1", "PurchaseOrder:a:1", "PO907711", [40, 48], "g1b")):
            self.sql(f"INSERT INTO ontology.link_rows ({', '.join(COLS)}) VALUES "
                     f"({', '.join(['%s'] * len(COLS))})", [r[c] for c in COLS])

    def sql(self, statement, params=None):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            cur = pg.execute(statement, params)
            return cur.fetchall() if cur.description else None

    def refusal(self, run):
        import psycopg
        from ontology.step_migrate import collapse_refusal
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            return collapse_refusal(cur, run)

    def collapsed(self):
        import psycopg
        from ontology.step_migrate import store_collapsed
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            return store_collapsed(cur)

    def collapse(self):
        from operating.pg import mention_collapse as mc
        out = mc.run(self.dsn, workers=1, types=TYPES, log=lambda m: None)
        self.assertEqual(out["failed"], [], out)
        self.assertEqual(out["totals"].get("twins_deleted"), 1, out)

    def test_a_folded_store_refuses_a_rerun_not_occurrence_aware_until_undone(self):
        run = ["sender_parties", "unique_mentions"]
        from ontology import step_migrate as sm
        from operating.pg import mention_collapse as mc
        self.assertEqual((sm.COLLAPSE_MARK, sm.COLLAPSE_RETIRED, sm.COLLAPSE_UNDO),
                         (mc.MARK, mc.RETIRED, mc.UNDO))   # the collapse's own markers
        self.assertFalse(self.collapsed())
        self.assertIsNone(self.refusal(run))   # never folded: nothing refused
        self.collapse()
        self.assertTrue(self.collapsed())
        self.assertEqual(self.refusal(run), REFUSED)
        self.assertIsNone(self.refusal(["unique_mentions", "task_ids", "sender_corroboration"]))
        # fail closed: a step the guard holds no proof for is refused too
        self.assertEqual(self.refusal(["some_new_step"]), "collapsed_store:some_new_step")
        # the batch marks outlive the undo table: retired is still collapsed
        self.sql("DELETE FROM ontology.mention_collapse_undo")
        self.assertTrue(self.collapsed())
        # undone whole: the marks go, the store reads as never folded
        self.sql("DELETE FROM ontology.load_state WHERE starts_with(name, 'c04:')")
        self.assertFalse(self.collapsed())
        self.sql("INSERT INTO ontology.load_state (name, value) VALUES ('c04-retired', '{}')")
        self.assertTrue(self.collapsed())

    def test_every_apply_path_refuses_on_a_folded_store_and_writes_nothing(self):
        from ontology import pg_step_inplace as inplace
        from ontology import step_migrate as sm
        from operating.pg import ontology_migrate as om
        from operating.pg import step_staging
        from operating.pg.ontology_load import ORDER
        self.collapse()
        # a served store whole, its step code known, so only the guard can refuse
        self.sql("INSERT INTO ontology.load_state (name, value) VALUES ('generation', %s) "
                 "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                 (json.dumps({"content_digest": "c"}),))
        for table in ORDER:
            self.sql("INSERT INTO ontology.load_state (name, value) VALUES (%s, '{}') "
                     "ON CONFLICT DO NOTHING", (f"table:{table}",))
        self.sql("INSERT INTO ontology.store_meta (key, value) VALUES ('steps_digest', "
                 "'steps:served') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        before = self.sql("SELECT count(*) FROM ontology.link_rows")
        work = Path(tempfile.mkdtemp())
        layer = work / "steps.sqlite"
        conn = sqlite3.connect(layer)
        conn.execute(f"CREATE TABLE {sm.TABLE} (key TEXT PRIMARY KEY, value TEXT)")
        for k, v in {"id": "L1", "from": "steps:served", "to": "steps:new", "unit": None,
                     "steps": ["sender_parties"]}.items():
            conn.execute(f"INSERT INTO {sm.TABLE} VALUES (?, ?)", (k, json.dumps(v)))
        conn.commit()
        conn.close()
        out = om.apply_migration(None, self.dsn, steps=layer)
        self.assertEqual((out["applied"], out["reason"]), (False, REFUSED), out)
        # the in-place step run and the copy path refuse before reading anything
        plan = {"id": "p", "steps": {"run": {"sender_parties": None}, "refused": []}}
        with self.assertRaisesRegex(inplace.Refused, REFUSED):
            inplace.run_steps(plan, plan["steps"]["run"], None, {}, self.dsn, work / "w")
        self.assertFalse((work / "w").exists())
        # the copy path's layer (step_migrate.build_layer) is refused at its apply (above)
        # a staged bit of it is not applied
        import psycopg
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            step_staging.ensure(cur, "run-1")
            cur.execute(f"INSERT INTO {step_staging.schema()}.staged VALUES "
                        "('sender_parties', 0, '{}')")
            pg.commit()
        with self.assertRaisesRegex(step_staging.StageRefused, REFUSED):
            step_staging.apply_now(self.dsn, "run-1", "sender_parties")
        self.assertEqual(self.sql("SELECT count(*) FROM ontology.link_rows"), before)
        self.assertEqual(self.sql("SELECT count(*) FROM ontology.schema_migrations"), [(0,)])
        self.assertEqual(self.sql("SELECT value FROM ontology.store_meta WHERE key = "
                                  "'steps_digest'"), [("steps:served",)])


class CollapsePlanTests(unittest.TestCase):
    def test_the_plan_refuses_a_rerun_that_is_not_occurrence_aware(self):
        from ontology import step_migrate as sm
        from ontology.schema import load_schema
        schema = load_schema()
        code = sm.code_digests(None)
        present = sorted(sm.STEPS)
        changed = {**code, "sender_parties": "served:other"}
        plain = sm.plan_schemas(schema, schema, changed, code, present)
        folded = sm.plan_schemas(schema, schema, changed, code, present, store_collapsed=True)
        self.assertIn("sender_parties", plain["steps"]["run"])
        self.assertEqual(plain["steps"]["refused"], [])
        self.assertNotIn("store_collapsed", plain["steps"])   # never folded: as before
        self.assertIn(REFUSED, folded["steps"]["refused"])
        self.assertTrue(folded["steps"]["store_collapsed"])
        self.assertEqual(folded["id"], plain["id"])   # the same migration, refused
        # only the occurrence-aware unique_mentions re-runs: nothing refused
        firm = {**code, "unique_mentions": "served:other"}
        aware = sm.plan_schemas(schema, schema, firm, code, present, store_collapsed=True)
        refused = [r for r in aware["steps"]["refused"]
                   if r.startswith(sm.COLLAPSE_REFUSAL)]
        self.assertEqual(refused, [f"{sm.COLLAPSE_REFUSAL}{n}" for n in sorted(
            aware["steps"]["run"]) if n not in sm.COLLAPSE_SAFE])
        self.assertNotIn("collapsed_store:unique_mentions", aware["steps"]["refused"])


if __name__ == "__main__":
    unittest.main()
