"""A whole-store step (demand pegging) over delta layers applied to Postgres
(``operating.pg.whole_steps``, ``operating.pg.peg_scope``, ``operating.pg.ontology_delta``).

Evidence label: ``isolated-fixture`` (the made-up Portal orders of
``ontology.tests.test_supply_pegging``, the real projector, schema, pegging step, loader and
layer apply, a scratch Postgres database). Reproduces the 11-12 loss: the catch-up layer
replaced the 28 served peg links touching its objects by the 2 its closure could compute.

* After a layer whose closure holds only part of an item's lines, Postgres holds exactly
  the peg links a full build of the new data writes (not the layer's subset allocation,
  not the stale served ones): by a whole-store run when none ran before, and by the
  incremental run over the layer's part (a quantity change, an order closing, a line
  moving to another item) once one did.
* The run moves the data version only when links changed, as a layer the traverse-facts
  fold accepts: the layer's own fold and the run's fold keep the facts (review of
  4258858296: a pending mark in every layer forced a full facts read per layer).
* Pending is read from the layers' replaced objects of the step's input types only.
* A run that fails changes nothing, says so, and the next follow cycle does it even when no
  layer is new.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from operating.tests import pg_testdb
from operating.tests.test_pg_ontology_delta import MIGRATIONS

PEG = "link_id >= 'peg:' AND link_id < 'peg;'"


def _build(path: Path, rows: dict, *, owned=None, digest=None) -> Path:
    from ontology import demand_pegging as dp, identifiers, schema
    from ontology.project import build_store
    from ontology.tests.test_supply_pegging import Source
    build_store(path, schema.load_schema(), {"portal_api": Source("portal_api", rows)},
                normalizer=identifiers.normalize,
                clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
                post_project={"supply_pegging": dp.supply_pegging_step})
    if owned is not None:   # a delta layer: its owned objects, over the base generation
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in owned])
            conn.execute("UPDATE store_meta SET value = ? WHERE key = 'content_digest'",
                         (digest,))
    return path


def _sqlite_pegs(path: Path) -> list[tuple]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return sorted(conn.execute(f"SELECT link_type, from_object, to_object, tier, reason "
                                   f"FROM links WHERE {PEG}"))


class WholeStoreStepsOverALayer(unittest.TestCase):

    def setUp(self):
        import psycopg
        from ontology.tests import test_supply_pegging as fx
        from operating.pg import ontology_load
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fx = fx
        base = _build(self.dir / "base.sqlite", fx.PORTAL)
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        with sqlite3.connect(base) as conn:
            self.digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                       "'content_digest'").fetchone()[0]
        self.base_pegs = _sqlite_pegs(base)
        # sales order line 12 grows from 50 to 60: it now takes all of PO line 31, so line
        # 11 no longer does. The layer owns line 12 and its order; its closure (their link
        # partners) holds PO line 31 and the products, not line 11, PO line 32 or the rest.
        self.new = copy.deepcopy(fx.PORTAL)
        for line in self.new["public.app_sales_order_line"]:
            if line["id"] == 12:
                line["quantity"] = "60"
        closure = {"public.app_product": self.new["public.app_product"],
                   "public.app_sales_order": [r for r in self.new["public.app_sales_order"]
                                              if r["id"] == 2],
                   "public.app_sales_order_line": [
                       r for r in self.new["public.app_sales_order_line"] if r["id"] == 12],
                   "public.app_purchase_order": [
                       r for r in self.new["public.app_purchase_order"] if r["id"] == 21],
                   "public.app_purchase_order_line": [
                       r for r in self.new["public.app_purchase_order_line"] if r["id"] == 31]}
        self.layer = _build(self.dir / "delta-1.sqlite", closure,
                            owned=[fx.L[12], fx.S[2]], digest=self.digest)
        self.full = _sqlite_pegs(_build(self.dir / "full.sqlite", self.new))

    def served_pegs(self) -> list[tuple]:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return sorted(tuple(r) for r in pg.execute(
                "SELECT link_type, from_object, to_object, tier, reason FROM "
                "ontology.links WHERE superseded_at IS NULL AND link_id COLLATE \"C\" >= "
                "'peg:' AND link_id COLLATE \"C\" < 'peg;'"))

    def pairs(self, rows):
        return sorted((r[0], r[1], r[2]) for r in rows)

    def layer_of(self, name, new, owned):
        closure = {"public.app_product": new["public.app_product"],
                   "public.app_sales_order": [r for r in new["public.app_sales_order"]
                                              if r["id"] == 2],
                   "public.app_sales_order_line": [
                       r for r in new["public.app_sales_order_line"] if r["id"] == 12],
                   "public.app_purchase_order": [
                       r for r in new["public.app_purchase_order"] if r["id"] == 21],
                   "public.app_purchase_order_line": [
                       r for r in new["public.app_purchase_order_line"] if r["id"] == 31]}
        return _build(self.dir / name, closure, owned=owned, digest=self.digest)

    def version(self):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return pg.execute("SELECT ontology.sq_data_version()").fetchone()[0]

    def test_postgres_holds_the_full_builds_peg_links_after_a_partial_layer(self):
        import psycopg
        from operating.pg import ontology_delta
        fx, line = self.fx, "SalesOrderLine.supplied_by"
        # the fixture says what it should: the change moves line 11 off PO line 31, and the
        # layer's own allocation (its closure) differs from the full build's
        self.assertIn((line, fx.L[11], fx.P[31]), self.pairs(self.base_pegs))
        self.assertNotIn((line, fx.L[11], fx.P[31]), self.pairs(self.full))
        self.assertNotEqual(self.pairs(_sqlite_pegs(self.layer)), self.pairs(self.full))
        self.assertEqual(self.pairs(self.served_pegs()), self.pairs(self.base_pegs))

        out = ontology_delta.apply_listed([self.layer], self.dsn, log=io.StringIO())
        self.assertEqual(out["applied_now"], 1, out)
        run = out["whole_steps"]
        self.assertEqual(run["mode"], "full", run)   # never run over this load
        self.assertTrue(run["layer"].startswith("whole-steps-"), run)
        # exactly the full build's rows: pairs, tier (lead) and the cited reason
        self.assertEqual(self.served_pegs(), self.full)
        self.assertEqual({r[3] for r in self.served_pegs()}, {"lead"})
        # the layer wrote no load state of the step (the mark is store_meta's, by the run)
        with psycopg.connect(self.dsn) as pg:
            names = [r[0] for r in pg.execute("SELECT name FROM ontology.load_state")]
        self.assertNotIn("whole_steps", names)
        # nothing pending: the next cycle with no new layer does nothing, moves nothing
        before = self.version()
        again = ontology_delta.apply_listed([self.layer], self.dsn, log=io.StringIO())
        self.assertEqual((again["applied_now"], again["whole_steps"]), (0, {"pending": False}))
        self.assertEqual(self.version(), before)

    def test_the_incremental_run_equals_a_full_build_for_each_kind_of_change(self):
        """Once a run left its mark, each layer is followed by an incremental run over the
        part it can move, and Postgres holds the full build's peg links of the new data."""
        from operating.pg import ontology_delta, ontology_load, whole_steps
        fx = self.fx
        first = whole_steps.recompute(self.dsn, full=True)   # the mark, over the load
        self.assertEqual(first["mode"], "full")
        # the loaded rows are the full build's: nothing written, no version moved
        self.assertEqual(first["steps"]["supply_pegging"]["full"]["changed"], 0, first)
        self.assertIsNone(first["layer"], first)

        def closed(new):
            for order in new["public.app_sales_order"]:
                if order["id"] == 2:
                    order["status"] = "Closed"

        def other_item(new):
            for row in new["public.app_sales_order_line"]:
                if row["id"] == 12:
                    row["product_id"] = 2

        def more(new):
            for row in new["public.app_sales_order_line"]:
                if row["id"] == 12:
                    row["quantity"] = "60"

        for n, change in enumerate((more, closed, other_item)):
            with self.subTest(change.__name__):
                if n:   # each change over the base again
                    base = _build(self.dir / f"base{n}.sqlite", fx.PORTAL)
                    ontology_load.load(base, self.dsn, workers=1, replace=True,
                                       log=io.StringIO())
                    whole_steps.recompute(self.dsn, full=True)
                new = copy.deepcopy(fx.PORTAL)
                change(new)
                full = _sqlite_pegs(_build(self.dir / f"full{n}.sqlite", new))
                self.assertNotEqual(self.pairs(full), self.pairs(self.base_pegs))
                layer = self.layer_of(f"delta-{change.__name__}.sqlite", new,
                                      [fx.L[12], fx.S[2]])
                before = self.version()
                out = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO())
                run = out["whole_steps"]
                self.assertEqual((out["applied_now"], run.get("mode")),
                                 (1, "incremental"), out)
                self.assertEqual(self.served_pegs(), full, run)
                counts = run["steps"]["supply_pegging"]["incremental"]
                self.assertNotIn("outside_scope", counts["counts"], counts)
                self.assertGreater(counts["gone"] + counts["changed"] + counts["added"], 0)
                self.assertTrue(run["layer"], run)
                self.assertGreater(self.version(), before)

    def test_the_traverse_facts_fold_over_the_layer_and_the_run(self):
        """The layer's own fold keeps the facts (no foreign load state in its
        transaction), and the run's changed links are folded as a layer of their own: no
        host reads the version in full after a cycle."""
        import os
        from operating import ontology_tools_traverse as tt
        from operating.pg import ontology_delta, whole_steps
        facts_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, facts_dir, True)
        self.enterContext(mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": facts_dir}))
        tt._LOADED.clear()
        self.addCleanup(tt._LOADED.clear)
        whole_steps.recompute(self.dsn, full=True)
        self.assertTrue(tt.prime_pg_facts(self.dsn))
        log = io.StringIO()
        out = ontology_delta.apply_listed([self.layer], self.dsn, log=log)
        self.assertEqual(out["applied_now"], 1, out)
        applied = json.loads(log.getvalue().splitlines()[0])
        self.assertTrue(applied["applied"] and applied["facts_kept"], applied)
        self.assertEqual(self.served_pegs(), self.full)
        self.assertEqual(out["whole_steps"]["mode"], "incremental", out)
        self.assertTrue(out["whole_steps"]["facts_kept"], out)
        # a host over the kept version folds nothing more and reads nothing whole
        self.assertFalse(tt.catch_up_pg_facts(self.dsn))   # already the version's facts
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            layers = pg.execute("SELECT value FROM ontology.load_state WHERE name = "
                                "'delta_layers'").fetchone()[0]
        self.assertEqual(layers[0], self.layer.name)
        self.assertTrue(layers[1].startswith("whole-steps-"), layers)

    def test_pending_is_a_replaced_object_of_the_steps_input_types(self):
        import psycopg
        from operating.pg import whole_steps
        with psycopg.connect(self.dsn) as pg:
            pg.execute("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s)",
                       ("whole_steps", json.dumps({"through": 0, "full_at": 0})))
            pg.execute("INSERT INTO ontology.applied_owned (layer, object_id) VALUES "
                       "('delta-x', 'Email:graph.messages:{}')")
            pg.commit()
        self.assertFalse(whole_steps.pending(self.dsn))
        self.assertEqual(whole_steps.recompute(self.dsn), {"pending": False})
        with psycopg.connect(self.dsn) as pg:   # a run's own layer is no input change
            pg.execute("INSERT INTO ontology.applied_owned (layer, object_id) VALUES "
                       "('whole-steps-1-2', %s)", (self.fx.L[12],))
            pg.commit()
        self.assertFalse(whole_steps.pending(self.dsn))
        with psycopg.connect(self.dsn) as pg:
            pg.execute("INSERT INTO ontology.applied_owned (layer, object_id) VALUES "
                       "('delta-y', %s)", (self.fx.L[12],))
            pg.commit()
        self.assertTrue(whole_steps.pending(self.dsn))
        # more replaced objects than a part is worth: the whole store (off the lock)
        with mock.patch.dict("os.environ", {"PLX_WHOLE_STEPS_SCOPE_MAX": "1"}):
            with psycopg.connect(self.dsn) as pg:
                pg.execute("INSERT INTO ontology.applied_owned (layer, object_id) VALUES "
                           "('delta-z', %s)", (self.fx.S[2],))
                pg.commit()
            self.assertEqual(whole_steps.recompute(self.dsn)["mode"], "full")
        self.assertFalse(whole_steps.pending(self.dsn))
        # an unreadable mark is no mark: the next run is a whole-store one
        with psycopg.connect(self.dsn) as pg:
            pg.execute("INSERT INTO ontology.applied_owned (layer, object_id) VALUES "
                       "('delta-w', %s)", (self.fx.L[12],))
            pg.commit()
        with psycopg.connect(self.dsn) as pg:
            pg.execute("UPDATE ontology.store_meta SET value = 'x' WHERE key = 'whole_steps'")
            pg.commit()
        self.assertEqual(whole_steps.recompute(self.dsn)["mode"], "full")

    def test_a_failed_recompute_changes_nothing_and_the_next_cycle_does_it(self):
        from ontology import demand_pegging as dp
        from operating.pg import ontology_delta
        err = io.StringIO()
        with mock.patch.object(dp, "peg_links", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stderr", err):
            out = ontology_delta.apply_listed([self.layer], self.dsn, log=io.StringIO())
        self.assertEqual(out["applied_now"], 1, out)
        self.assertIn("boom", out["whole_steps"]["error"])
        self.assertIn("ONTOLOGY WHOLE STEPS FAILED", err.getvalue())
        # the layer neither deleted the served peg links nor copied in its own
        self.assertEqual(self.served_pegs(), self.base_pegs)
        # no new layer: the layer's replaced objects make the next cycle run
        again = ontology_delta.apply_listed([self.layer], self.dsn, log=io.StringIO())
        self.assertEqual(again["applied_now"], 0)
        self.assertEqual(self.served_pegs(), self.full)


if __name__ == "__main__":
    unittest.main()
