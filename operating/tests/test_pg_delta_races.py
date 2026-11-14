"""Delta layers applied to the Postgres ontology while other writers and readers run at
the same time (``operating.pg.ontology_delta``): real threads on a scratch Postgres, no
stand-in for the lock or the database.

* several followers applying one layer at once apply it once: its objects hold exactly
  the full build's rows (none twice) and the layer is listed once;
* two followers, one that knows only the first layer and one that knows both, racing:
  every object either layer owns ends with the newest captures' rows;
* a reader during an apply sees each owned object whole, as before the layer or as
  after it, never half replaced or missing;
* a generation reload racing an apply: the layer is recorded applied only when its rows
  are there, and a layer the reload wiped is applied again by the next follow.

Evidence: isolated fixture (the projection tests' synthetic Portal mirror) + scratch
Postgres; no network."""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from operating.tests import pg_testdb
from operating.tests.test_pg_ontology_delta import MIGRATIONS

FOLLOWERS = 4   # threads at once (the load cap: at most four workers per job)
ROUNDS = 2      # each race twice, the racers started in both orders


class DeltaApplyRaces(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import psycopg
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1, T2

        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        facts = tempfile.mkdtemp(prefix="pg-delta-races-facts-")
        cls.addClassCleanup(shutil.rmtree, facts, True)
        env = mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": facts})
        env.start()
        cls.addClassCleanup(env.stop)
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        cls.addClassCleanup(fixture.doCleanups)
        fixture.capture(T1)
        cls.base = fixture.full("gen1.sqlite")
        # layer 1: a changed order, a line moved to another order, a new line
        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.fake.set("public.app_purchase_order_line", "l1", purchase_order_id="po-b")
        fixture.fake.tables["public.app_purchase_order_line"].append(
            {"id": "l9", "purchase_order_id": "po-c", "quantity": "7", "uom": "EA",
             "unit_cost": "1"})
        fixture.capture(T2)
        cls.first = Path(fixture.cycle(cls.base)["store"])
        cls.full_first = fixture.full("gen2.sqlite")
        # layer 2 over the same generation: the new line changed again (owned by both)
        fixture.fake.set("public.app_purchase_order_line", "l9", quantity="8")
        fixture.capture(T2 + timedelta(hours=1))
        cls.second = Path(fixture.cycle(cls.base)["store"])
        cls.full_second = fixture.full("gen3.sqlite")
        cls.owned_first = cls.owned(cls.first)
        cls.owned_second = cls.owned(cls.second)
        assert cls.owned_second and set(cls.owned_second) <= set(cls.owned_first), \
            (cls.owned_first, cls.owned_second)

    @staticmethod
    def owned(store: Path) -> list[str]:
        with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
            return [r[0] for r in conn.execute("SELECT object_id FROM delta_owned ORDER BY 1")]

    # ---- helpers ------------------------------------------------------------------------

    def reload(self):
        from operating.pg import ontology_load
        ontology_load.load(self.base, self.dsn, workers=1, replace=True, log=io.StringIO())

    def rows(self, cur, oids) -> dict:
        from operating.pg.ontology_parity import _BY_OBJECT, _pg_rows
        return {(part, oid): _pg_rows(cur, part, oid)
                for oid in oids for part in [*_BY_OBJECT, "object_values"]}

    def built(self, store: Path, oids) -> dict:
        from operating.pg.ontology_parity import _BY_OBJECT, _sqlite_rows
        with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
            return {(part, oid): _sqlite_rows(conn, part, oid)
                    for oid in oids for part in [*_BY_OBJECT, "object_values"]}

    def served(self, oids) -> dict:
        import psycopg
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            return self.rows(cur, oids)

    def layers(self) -> tuple[list, set]:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            listed = pg.execute("SELECT value FROM ontology.load_state WHERE name = "
                                "'delta_layers'").fetchone()
            recorded = {r[0] for r in pg.execute("SELECT name FROM ontology.load_state "
                                                 "WHERE name LIKE 'delta:%%'")}
        return (listed[0] if listed else []), recorded

    def at_once(self, *calls):
        """Run every call on its own thread, all released together; their results in
        order (an exception is re-raised here, never swallowed)."""
        gate = threading.Barrier(len(calls))
        out: list = [None] * len(calls)

        def run(i, call):
            gate.wait()
            try:
                out[i] = ("ok", call())
            except BaseException as exc:   # carried to the test thread
                out[i] = ("raised", exc)
        threads = [threading.Thread(target=run, args=(i, c), daemon=True)
                   for i, c in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)
            self.assertFalse(thread.is_alive(), "a racing call never finished")
        for kind, value in out:
            if kind == "raised":
                raise value
        return [value for _kind, value in out]

    # ---- the races ----------------------------------------------------------------------

    def test_one_layer_applied_by_several_followers_at_once_lands_once(self):
        from operating.pg import ontology_delta
        expected = self.built(self.full_first, self.owned_first)
        for _ in range(ROUNDS):
            self.reload()
            results = self.at_once(*[lambda: ontology_delta.apply_layer(self.first, self.dsn)
                                     ] * FOLLOWERS)
            self.assertEqual(sum(bool(r.get("applied")) for r in results), 1, results)
            self.assertEqual(sorted(r.get("reason") for r in results if not r["applied"]),
                             ["already_applied"] * (FOLLOWERS - 1), results)
            # exactly the full build's rows: a row written twice would show twice here
            self.assertEqual(self.served(self.owned_first), expected)
            listed, recorded = self.layers()
            self.assertEqual((listed, recorded), ([self.first.name],
                                                  {f"delta:{self.first.name}"}))

    def test_two_followers_over_two_layers_end_with_the_newest_rows(self):
        """One follower read the pointer when it listed only the first layer, the other
        after the second was added: whichever applies what first, every object either
        layer owns ends as the newest full build, and each layer is applied once."""
        from operating.pg import ontology_delta
        expected = self.built(self.full_second, self.owned_first)
        newest, older = str(self.second), str(self.first)
        for round_no in range(ROUNDS):
            self.reload()
            calls = [lambda: ontology_delta.apply_listed([older], self.dsn),
                     lambda: ontology_delta.apply_listed([newest, older], self.dsn)]
            if round_no % 2:
                calls.reverse()
            results = self.at_once(*calls)
            self.assertEqual(sum(r["applied_now"] for r in results), 2, results)
            self.assertEqual([r["refused"] for r in results], [[], []], results)
            self.assertEqual(self.served(self.owned_first), expected, round_no)
            listed, recorded = self.layers()
            self.assertEqual(sorted(listed), sorted([self.first.name, self.second.name]))
            self.assertEqual(len(listed), 2, listed)
            self.assertEqual(recorded, {f"delta:{self.first.name}",
                                        f"delta:{self.second.name}"})

    def test_a_reader_during_an_apply_sees_each_record_before_or_after_never_between(self):
        import psycopg
        from operating.pg import ontology_delta
        oids = self.owned_first
        before = self.built(self.base, oids)
        after = self.built(self.full_first, oids)
        self.assertNotEqual(before, after)
        for _ in range(ROUNDS):
            self.reload()
            done = threading.Event()
            seen: list = []
            odd: list = []

            def read():
                with psycopg.connect(self.dsn) as pg:
                    while True:
                        finished = done.is_set()
                        # one snapshot per read, as a request's read of a record page
                        with pg.transaction(), pg.cursor() as cur:
                            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                            now = self.rows(cur, oids)
                        state = "before" if now == before else "after" if now == after \
                            else None
                        if state is None:
                            odd.append(now)
                            return
                        seen.append(state)
                        if finished:
                            return

            readers = [threading.Thread(target=read, daemon=True)
                       for _ in range(FOLLOWERS - 1)]
            for reader in readers:
                reader.start()
            try:
                applied = ontology_delta.apply_layer(self.first, self.dsn)
            finally:
                done.set()
                for reader in readers:
                    reader.join(timeout=120)
            self.assertTrue(applied["applied"], applied)
            self.assertEqual(odd[:1], [], "a reader saw a record half replaced")
            self.assertIn("after", seen)
            self.assertEqual(seen[-1], "after")

    def test_a_reload_racing_an_apply_records_the_layer_only_with_its_rows(self):
        """A reload empties the tables under the layers' lock and forgets the layers it
        wiped: after the race the layer is either recorded with its rows in place or not
        recorded at all (its rows the base's), and the next apply then brings it in."""
        from operating.pg import ontology_delta, ontology_load
        oids = self.owned_first
        base_rows = self.built(self.base, oids)
        layer_rows = self.built(self.full_first, oids)
        outcomes = set()
        for round_no in range(ROUNDS + 1):
            self.reload()
            calls = [lambda: ontology_delta.apply_layer(self.first, self.dsn),
                     lambda: ontology_load.load(self.base, self.dsn, workers=1, replace=True,
                                                log=io.StringIO())]
            if round_no % 2:
                calls.reverse()
            self.at_once(*calls)
            _listed, recorded = self.layers()
            served = self.served(oids)
            if recorded:
                self.assertEqual(recorded, {f"delta:{self.first.name}"})
                self.assertEqual(served, layer_rows, "recorded applied without its rows")
                outcomes.add("applied after the reload")
                continue
            self.assertEqual(served, base_rows, "a layer's rows with no record of it")
            outcomes.add("wiped by the reload")
            again = ontology_delta.apply_layer(self.first, self.dsn)
            self.assertTrue(again["applied"], again)
            self.assertEqual(self.served(oids), layer_rows)
        self.assertTrue(outcomes)


if __name__ == "__main__":
    unittest.main()
