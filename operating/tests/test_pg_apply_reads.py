"""The delta apply's per-layer reads of the owned records stay on their indexes and give
the rows of the forms they replaced.

Live 11-15 (EXPLAIN on the served store): the kept link slice's fold read the owned
records' links with one ``from_object IN owned OR to_object IN owned``, a sequential scan
of every ``link_rows`` partition (82 GB) per layer; the change feed's record signatures,
taken twice a layer, scanned ``text_fields`` whole at 75k owned records and merged whole
indexes plus ``objects`` at 780k. Both now read per record (``link_marks._ends_slice``,
``change_feed._SIGNATURE``). Checked here at the boundary, on a projected generation loaded
into a scratch database: for owned sets of every shape (links from, to, both and neither
end owned; records with and without versions, text, identifiers and removals, and ids the
store does not hold) the new statements return exactly the old statements' rows.
Scratch Postgres database, no network, no credentials. Evidence: isolated-fixture.
"""

from __future__ import annotations

import io
import random
import re
import unittest

from operating.tests import pg_testdb
from operating.tests.test_pg_schema_migrate import MIGRATIONS

# the forms replaced, kept verbatim as the reference
_OLD_ENDS = ("WHERE l.from_object IN (SELECT object_id FROM owned) "
             "OR l.to_object IN (SELECT object_id FROM owned)")
_OLD_SIGNATURE = """
SELECT o.object_id, ob.object_id IS NOT NULL AS present, ob.object_type,
       md5(concat_ws('|', ob::text, v.s, t.s, r.s, i.s)) AS sig,
       coalesce(r.open, false) AS removed
FROM owned o
LEFT JOIN ontology.objects ob ON ob.object_id = o.object_id
LEFT JOIN (SELECT object_id, string_agg(ver::text || ':' || coalesce(superseded_at, '-'),
                                        ',' ORDER BY ver) AS s
           FROM ontology.object_versions WHERE object_id IN (SELECT object_id FROM owned)
           GROUP BY object_id) v ON v.object_id = o.object_id
LEFT JOIN (SELECT object_id, string_agg(field_id::text, ',' ORDER BY field_id) AS s
           FROM ontology.text_fields WHERE object_id IN (SELECT object_id FROM owned)
           GROUP BY object_id) t ON t.object_id = o.object_id
LEFT JOIN (SELECT object_id, string_agg(x::text, ',' ORDER BY x::text) AS s,
                  bool_or(x.until IS NULL) AS open
           FROM ontology.object_removals x WHERE object_id IN (SELECT object_id FROM owned)
           GROUP BY object_id) r ON r.object_id = o.object_id
LEFT JOIN (SELECT object_id, string_agg(x::text, ',' ORDER BY x::text) AS s
           FROM ontology.identifiers x WHERE object_id IN (SELECT object_id FROM owned)
           GROUP BY object_id) i ON i.object_id = o.object_id
"""


# the layer link reads ``ontology_delta._links_staged`` replaced (SQLite, then served)
_OLD_LAYER_LINKS = (
    "SELECT {cols} FROM links WHERE from_object IN (SELECT value FROM json_each(:b)) "
    "AND {ex} UNION ALL SELECT {cols} FROM links WHERE "
    "to_object IN (SELECT value FROM json_each(:b)) AND from_object NOT IN "
    "(SELECT value FROM json_each(:ids)) AND {ex}")
_OLD_SERVED_FROM = "l.from_object = ANY(%s::text[]) AND {ex}"
_OLD_SERVED_TO = ("l.to_object = ANY(%s::text[]) AND l.from_object NOT IN "
                  "(SELECT object_id FROM owned) AND {ex}")


class ApplyReadsAreTheOldRows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import psycopg
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        from operating.pg import change_feed, ontology_load
        name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
            pg.execute(change_feed.DDL)
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        cls.addClassCleanup(fixture.doCleanups)
        fixture.capture(T1)
        base = cls.base = fixture.full("gen1.sqlite")
        ontology_load.load(base, cls.dsn, workers=1, replace=True, log=io.StringIO())
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            objects = [r[0] for r in pg.execute(
                "SELECT object_id FROM ontology.objects ORDER BY object_id")]
            # removals, open and closed, two on one record (the projection made none here)
            pg.execute("INSERT INTO ontology.object_removals (object_id, basis, since, until) "
                       "VALUES (%s, 'missing_from_source', '2026-01-01', NULL), "
                       "(%s, 'source_soft_delete', '2026-02-01', '2026-03-01'), "
                       "(%s, 'missing_from_source', '2026-03-02', NULL)",
                       (objects[0], objects[1], objects[1]))
            cls.links = pg.execute("SELECT from_object, to_object FROM ontology.link_rows"
                                   ).fetchall()
        cls.objects = objects

    def owned_sets(self):
        """Owned sets of every shape: all, none held, each link's from end only, its to end
        only, random halves, plus ids the store does not hold."""
        rng = random.Random(1115)
        froms = {f for f, _ in self.links}
        tos = {t for _, t in self.links}
        yield "all", set(self.objects)
        yield "none_held", {"zz:not-held", "Unknown:1"}
        yield "from_only", froms - tos
        yield "to_only", tos - froms
        for k in range(4):
            yield f"half{k}", {o for o in self.objects if rng.random() < 0.5} | {"zz:absent"}

    def rows_under(self, owned, statement, params=None):
        import psycopg
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) ON COMMIT DROP")
            with cur.copy("COPY owned (object_id) FROM STDIN") as copy:
                for oid in sorted(owned):
                    copy.write_row((oid,))
            cur.execute("ANALYZE owned")
            cur.execute(statement, params)
            out = sorted(cur.fetchall(), key=repr)
            pg.rollback()
        return out

    def test_the_fixture_has_links_of_every_end_shape(self):
        froms = {f for f, _ in self.links}
        tos = {t for _, t in self.links}
        self.assertGreater(len(self.links), 5)
        self.assertTrue(froms - tos and tos - froms, "links with only one owned end")

    def test_the_kept_link_slice_fold_reads_the_or_forms_rows(self):
        from operating.pg import link_marks
        grouped = link_marks._GROUPED
        params = {"schemes": link_marks._schemes()}
        seen = 0
        for shape, owned in self.owned_sets():
            with self.subTest(shape=shape):
                old = self.rows_under(owned, grouped.format(
                    slice=link_marks._ROW_SLICE + _OLD_ENDS), params)
                new = self.rows_under(owned, grouped.format(
                    slice=link_marks._ends_slice("owned")), params)
                self.assertEqual(new, old)
                seen += bool(old)
        self.assertGreaterEqual(seen, 4, "most shapes fold some links")

    def test_the_change_feed_signatures_are_the_grouped_forms(self):
        from operating.pg import change_feed
        for shape, owned in self.owned_sets():
            with self.subTest(shape=shape):
                old = self.rows_under(owned, _OLD_SIGNATURE)
                new = self.rows_under(owned, change_feed._SIGNATURE)
                self.assertEqual(len(new), len(owned))
                self.assertEqual(new, old)
        removed = {r[0]: r[4] for r in self.rows_under(set(self.objects[:3]),
                                                        change_feed._SIGNATURE)}
        self.assertEqual(removed, {self.objects[0]: True, self.objects[1]: True,
                                   self.objects[2]: False})

    def test_a_layers_link_batches_are_the_old_reads_rows_in_order(self):
        """``_links_staged``'s per-batch reads: the layer's links (owned test in Python)
        and the served ones (per owned record), against the old statements, in batches."""
        import json
        import sqlite3
        import psycopg
        from ontology import store_ddl
        from operating.pg import ontology_delta, step_staging
        from operating.pg import whole_steps
        cols = ", ".join(store_ddl.LINK_COLUMNS)
        at_from = store_ddl.LINK_COLUMNS.index("from_object")
        ex_pg, ex_sq = whole_steps.pg_excluded(), whole_steps.sqlite_excluded()
        layer = sqlite3.connect(f"file:{self.base}?mode=ro", uri=True)
        self.addCleanup(layer.close)
        compared = 0
        for shape, owned_ids in self.owned_sets():
            owned = sorted(owned_ids)
            owned_set = set(owned)
            ids = json.dumps(owned)
            with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
                cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) "
                            "ON COMMIT DROP")
                with cur.copy("COPY owned (object_id) FROM STDIN") as copy:
                    for oid in owned:
                        copy.write_row((oid,))
                for k in range(0, len(owned), 7):
                    batch = owned[k:k + 7]
                    b = json.dumps(batch)
                    with self.subTest(shape=shape, batch=k):
                        old = list(layer.execute(_OLD_LAYER_LINKS.format(cols=cols, ex=ex_sq),
                                                 {"b": b, "ids": ids}))
                        new = list(layer.execute(
                            f"SELECT {cols} FROM links WHERE from_object IN (SELECT value "
                            f"FROM json_each(:b)) AND {ex_sq}", {"b": b}))
                        new += [r for r in layer.execute(
                            f"SELECT {cols} FROM links WHERE to_object IN (SELECT value "
                            f"FROM json_each(:b)) AND {ex_sq}", {"b": b})
                            if r[at_from] not in owned_set]
                        self.assertEqual(new, old)
                        held_old = step_staging.served_rows(
                            cur, _OLD_SERVED_FROM.format(ex=ex_pg), [batch])
                        step_staging.served_rows(cur, _OLD_SERVED_TO.format(ex=ex_pg),
                                                 [batch], held=held_old)
                        held_new = step_staging.served_rows(
                            cur, ex_pg, [batch],
                            source=ontology_delta._PER_OWNED.format(end="from_object"))
                        step_staging.served_rows(
                            cur, f"{ontology_delta._NOT_OWNED_FROM} AND {ex_pg}", [batch],
                            held=held_new,
                            source=ontology_delta._PER_OWNED.format(end="to_object"))
                        self.assertEqual(held_new, held_old)
                        compared += bool(old) + bool(held_old)
                pg.rollback()
        self.assertGreaterEqual(compared, 8, "most batches hold layer and served links")

    def test_the_not_owned_tests_never_scan_the_owned_list_per_row(self):
        """The "link from no owned record" tests (the served to-side batch read, the fold's
        to-side slice) are anti-joins on ``owned``'s key. 11-15 live: with 741k owned ids
        past hash_mem, ``NOT IN`` planned an unhashed subplan reading the whole spilled
        owned list for each candidate link (~10 min per 5k batch). Here hash_mem is made
        smaller than the owned list, which turns the old form into that plan."""
        import psycopg
        from operating.pg import link_marks, ontology_delta, peg_scope, whole_steps
        ex = whole_steps.pg_excluded()
        owned = sorted(self.objects) + [f"zz:pad:{i:06d}" for i in range(5000)]
        to_read = (f"SELECT l.link_id FROM "
                   f"{ontology_delta._PER_OWNED.format(end='to_object')} WHERE "
                   f"{ontology_delta._NOT_OWNED_FROM} AND {ex}")
        old_read = f"SELECT l.link_id FROM ontology.link_rows l WHERE " + \
            _OLD_SERVED_TO.format(ex=ex)
        fold = f"SELECT count(*) FROM ({link_marks._ends_slice('owned')}) x"
        # the pegging scope's to-side read (``peg_scope.ScopedConnection``), same test
        peg = (f"SELECT l.from_object FROM {peg_scope.SCOPE_TABLE} sc CROSS JOIN LATERAL "
               f"(SELECT from_object FROM ontology.link_rows WHERE to_object = sc.object_id"
               f" AND superseded_at IS NULL) l WHERE {peg_scope._NOT_FROM_SCOPE}")
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            for table in ("owned", peg_scope.SCOPE_TABLE):
                cur.execute(f"CREATE TEMP TABLE {table} (object_id text PRIMARY KEY) "
                            "ON COMMIT DROP")
                with cur.copy(f"COPY {table} (object_id) FROM STDIN") as copy:
                    for oid in owned:
                        copy.write_row((oid,))
                cur.execute(f"ANALYZE {table}")
            cur.execute("SET LOCAL work_mem = '64kB'; SET LOCAL hash_mem_multiplier = 1")

            def plan(sql, params=None):
                return "\n".join(r[0] for r in cur.execute("EXPLAIN " + sql, params))
            # the old form does turn into the per-row scan here (the probe is live)
            self.assertIn("(SubPlan", plan(old_read, [owned[:50]]))
            for name, sql, params in (("served to-side", to_read, [owned[:50]]),
                                      ("fold slice", fold,
                                       {"schemes": link_marks._schemes()}),
                                      ("pegging scope to-side", peg, None)):
                with self.subTest(name):
                    text = plan(sql, params)
                    self.assertNotIn("NOT (ANY", text)
                    self.assertNotIn("(SubPlan", text)
            pg.rollback()

    def test_replaced_links_are_staged_gone_once_each_in_one_write(self):
        """``stage_rows``: a row replacing a served one of its id puts that id in ``gone``
        (written with the batch, not one COPY a row); a row held alike puts nothing."""
        import psycopg
        from ontology import store_ddl
        from operating.pg import step_staging
        copies = []

        class CountingCursor(psycopg.Cursor):
            """Counts the COPY statements (``Cursor.copy`` is read-only on an instance)."""

            def copy(self, statement, *a, **kw):
                copies.append(str(statement).split(" (")[0])
                return super().copy(statement, *a, **kw)
        with psycopg.connect(self.dsn) as pg, CountingCursor(pg) as cur:
            served = list(step_staging.served_rows(cur, "TRUE").items())
            self.assertGreater(len(served), 5)
            from operating.pg.migrate_chunks import _served_links
            rows = list(_served_links(cur, "TRUE", []))
            at_reason = store_ddl.LINK_COLUMNS.index("reason")
            changed = [r if i % 2 else tuple(v if j != at_reason else f"{v} (changed)"
                                             for j, v in enumerate(r))
                       for i, r in enumerate(rows)]
            changed.append(("new:link:1", *rows[0][1:]))
            held = dict(served)
            s = step_staging.schema()
            step_staging.ensure(cur, "apply-reads-test", s)
            copies.clear()
            report, _, replaced = step_staging.stage_rows(cur, s, "t:step", 0, changed, held)
            counted = list(copies)
            replacing = sorted(r[0] for i, r in enumerate(rows) if not i % 2)
            gone = sorted(r[0] for r in cur.execute(
                f"SELECT link_id FROM {s}.gone WHERE step = 't:step' AND part = 0"))
            self.assertEqual(gone, replacing)
            self.assertEqual(replaced, len(replacing))
            self.assertEqual(report["gone"], len(replacing))
            self.assertEqual(report["same"], len(rows) - len(replacing))
            written = sorted(r[0] for r in cur.execute(
                f"SELECT link_id FROM {s}.links WHERE step = 't:step' AND part = 0"))
            self.assertEqual(written, sorted([*replacing, "new:link:1"]))
            self.assertEqual(len(counted), 2, counted)   # one gone COPY, one links COPY
            self.assertEqual(held, {})
            pg.rollback()



# ``NOT IN`` over a subquery, written out or as a module constant (``{_OWNED_PG}``)
_NOT_IN_SUBQUERY = re.compile(r"NOT\s+IN\s*(\(\s*SELECT\b|\{_?[A-Z])", re.IGNORECASE)


class NoNotInSubqueryOnTheApplyPath(unittest.TestCase):
    """The delta apply's modules write "not in a set" as an anti-join, never ``NOT IN``
    over a subquery: 11-15 the follower's 30 min statement limit cancelled a 4 h apply of
    a 741k-object layer on one ``NOT IN`` read (an unhashed subplan once the set outgrew
    hash_mem). A literal list (``NOT IN ('a', 'b')``) stays allowed."""

    MODULES = ("ontology_delta", "link_marks", "link_apply", "change_feed", "step_staging",
               "whole_steps", "peg_scope", "migrate_chunks", "ontology_migrate")

    def test_no_apply_module_reads_not_in_a_subquery(self):
        import importlib
        import inspect
        pattern = _NOT_IN_SUBQUERY
        for name in self.MODULES:
            with self.subTest(module=name):
                source = inspect.getsource(importlib.import_module(f"operating.pg.{name}"))
                hits = [line.strip() for line in source.splitlines()
                        if pattern.search(line) and not line.strip().startswith("#")]
                self.assertEqual(hits, [])

    def test_the_guard_sees_both_old_forms(self):
        pattern = _NOT_IN_SUBQUERY
        self.assertTrue(pattern.search('f"l.from_object NOT IN {_OWNED_PG} AND"'))
        self.assertTrue(pattern.search("WHERE x not in (select object_id FROM owned)"))
        self.assertFalse(pattern.search("WHERE kind NOT IN ('mention', 'same_record')"))


if __name__ == "__main__":
    unittest.main()
