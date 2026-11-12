"""The in-place step run with its links staged in Postgres (``operating.pg.step_staging``):
staged, applied by ``operating.pg.migrate_chunks`` and resumed, Postgres ends as the full
step build's; the plain apply refuses the layer; a mention link that differs only by its
extractor keeps the served row and is never written; the apply refuses a stage that no
longer matches the layer or the served links. Scratch Postgres databases, no network, no
credentials. Evidence: isolated-fixture.
"""

from __future__ import annotations

import json
import unittest

from operating.tests.test_pg_step_migrate import PREFIX
from operating.tests.test_pg_step_run import CommittedRunTests


class StagedRunTests(CommittedRunTests):

    test_a_layer_scoped_to_documents_replaces_only_their_links = None
    test_a_step_that_skipped_documents_is_refused = None

    def _run(self, plan, work, **kwargs):
        from ontology import pg_step_inplace as ip
        from ontology.tests.test_projection import normalizer
        from operating import build_steps
        from operating.pg.ontology_migrate import delta_mark
        case = self.case
        case.version = 2
        try:
            steps = build_steps.post_project(case.fixture.state, "plx",
                                             scratch=case.fixture.root / "steps")
            report = ip.run_steps(plan, plan["steps"]["run"], case.fixture.schema, steps,
                                  self.other, work, specs=case.specs(), normalizer=normalizer,
                                  stage_key="run-1")
        finally:
            case.version = 1
        out = work / "layer.sqlite"
        built = ip.write_run_layer(plan, plan["steps"]["run"], case.fixture.schema, work, out,
                                   unit_id=None, delta_mark=delta_mark(self.other),
                                   sequence=7, specs=case.specs(), **kwargs)
        return report, built, out

    def test_the_committed_run_applied_in_chunks_equals_the_one_transaction(self):
        import psycopg
        from operating.pg import migrate_chunks, ontology_migrate, step_staging
        plan = self._base()
        self._reference(plan)
        work = self.case.fixture.root / "run"
        report, built, out = self._run(plan, work)
        staged = report["steps"]["probe"]["staged"]
        # version 2 rewrites the even lines (a second link each) and keeps the odd ones
        self.assertGreater(staged["same"], 0)
        self.assertGreater(staged["written"], 0)
        self.assertEqual(staged["rows"], staged["same"] + staged["written"])
        (work / "probe.sqlite").unlink()   # resumed from its markers alone
        again, built_again, out = self._run(plan, work)
        self.assertEqual(built_again["id"], built["id"])   # the same layer again
        self.assertTrue(again["steps"]["probe"]["resumed"])
        self.assertEqual(again["steps"]["probe"]["staged"], staged)
        plain = ontology_migrate.apply_migration(None, self.other, steps=out)
        self.assertEqual((plain["applied"], plain.get("reason")), (False, "other_steps"))
        first = migrate_chunks.apply_chunked(None, self.other, steps=out, stop_after=1)
        self.assertEqual(first["reason"], "stopped")
        applied = migrate_chunks.apply_chunked(None, self.other, steps=out)
        self.assertTrue(applied["applied"], applied)
        written = first["chunks"]["steps:staged:probe"]   # the first chunk
        self.assertEqual(applied["chunks"]["steps:staged:probe"], {"skipped": "done"})
        self.assertEqual(written["written"], staged["written"])
        self.assertEqual(written["deleted"], staged["gone"])
        record = json.loads(sqlite_meta(out)["id"])
        self.assertEqual(step_staging.drop(self.other, record)["dropped"], step_staging.SCHEMA)
        with psycopg.connect(self.other) as pg:
            self.assertIsNone(pg.execute("SELECT to_regnamespace(%s)",
                                         (step_staging.SCHEMA,)).fetchone()[0])
        # the full build's rows and state; only the migration's id differs (its staged
        # links are not in the layer's content digest: the stage's digest is)
        from operating.tests.test_pg_migrate_chunks import _state
        from scripts.schema_migration_proof import compare, logical_rows
        equal = compare(logical_rows(dsn=self.case.dsn), logical_rows(dsn=self.other))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)
        one, other = _state(self.case.dsn), _state(self.other)
        self.assertEqual([m[1:] for m in one.pop("migrations")],
                         [m[1:] for m in other.pop("migrations")])
        self.assertEqual(one, other)

    def test_a_stage_that_no_longer_matches_is_refused_and_nothing_written(self):
        import psycopg
        from operating.pg import migrate_chunks, step_staging
        plan = self._base()
        work = self.case.fixture.root / "run"
        _r, _b, out = self._run(plan, work)
        with psycopg.connect(self.other) as pg:
            before = pg.execute("SELECT count(*), max(row_no) FROM ontology.link_rows"
                                ).fetchone()
            # a served link the run removes or replaces is gone meanwhile
            pg.execute(f"DELETE FROM ontology.link_rows WHERE link_id = (SELECT min(link_id) "
                       f"FROM {step_staging.SCHEMA}.gone)")
        got = migrate_chunks.apply_chunked(None, self.other, steps=out)
        self.assertFalse(got["applied"])
        self.assertTrue(got["reason"].startswith("staged_gone_not_served:probe"), got)
        with psycopg.connect(self.other) as pg:
            self.assertEqual(pg.execute("SELECT count(*) + 1, max(row_no) FROM "
                                        "ontology.link_rows").fetchone(), before)
            # a stage of another run (or none): refused before any write
            pg.execute(f"UPDATE {step_staging.SCHEMA}.staged SET report = "
                       "jsonb_set(report::jsonb, '{written}', '0')::text")
        got = migrate_chunks.apply_chunked(None, self.other, steps=out)
        self.assertEqual(got["reason"], "staged_differs:probe")

    def test_applied_as_it_goes_equals_the_staged_view_and_undoes(self):
        """``apply_now``: the served links become what a later step's staged view showed,
        per-type counts recorded, a second apply skips; ``undo`` restores them as before."""
        import psycopg
        from ontology import pg_step_inplace as ip
        from ontology import store_ddl
        from operating.pg import migrate_chunks as mc
        from operating.pg import step_staging
        from operating.pg.sqlite_compat import PgCompatConnection
        self._base()
        cols = list(store_ddl.LINK_COLUMNS)
        q = ("SELECT link_id, tier, reason FROM {t} WHERE "
             "starts_with(link_id, %s) ORDER BY link_id")
        with psycopg.connect(self.other) as pg:
            served = list(mc._served_links(pg.cursor(), "starts_with(l.link_id, %s)",
                                           [PREFIX]))
            before = pg.execute("SELECT * FROM ontology.link_rows WHERE starts_with("
                                "link_id, %s) ORDER BY link_id", (PREFIX,)).fetchall()
        store = self.case.fixture.root / "mentions.sqlite"
        local = ip._local_store(store)
        rows = [dict(zip(cols, r)) for r in served[1:]]
        rows[0]["tier"] = "corroborated"
        local.add("links", rows + [{**rows[1], "link_id": PREFIX + "zz-new"}])
        if not local.connection.in_transaction:
            local.connection.execute("BEGIN IMMEDIATE")
        from pathlib import Path
        Path(local.keep_as_part()).replace(store)
        scope = [{"step": "mentions", "prefix": PREFIX, "end": PREFIX[:-1] + ";",
                  "from": None}]
        got = step_staging.stage(self.other, "run-1", "mentions", 3, store, scope)
        pg = PgCompatConnection(self.other, statement_timeout_ms=0)
        try:
            ip.Overlay(pg).show([], staged_before="reviews")
            view = pg._pg.execute(q.format(t="pg_temp.links"), (PREFIX,)).fetchall()
        finally:
            pg.close()
        done = step_staging.apply_now(self.other, "run-1", "mentions")
        bit = done["bits"][3]
        self.assertEqual((bit["inserted"], bit["deleted"]), (got["written"], got["gone"]))
        self.assertEqual(sum(v["inserted"] - v["deleted"] for v in bit["types"].values()),
                         got["written"] - got["gone"])
        with psycopg.connect(self.other) as c:
            now = c.execute(q.format(t="ontology.links"), (PREFIX,)).fetchall()
        self.assertEqual(now, view)
        with psycopg.connect(self.other) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM ontology.link_rows WHERE "
                                       "row_no IS NULL").fetchone()[0], 0)
        again = step_staging.apply_now(self.other, "run-1", "mentions")
        self.assertEqual(again["bits"][3], {"skipped": "applied"})
        step_staging.undo(self.other, "mentions")
        with psycopg.connect(self.other) as c:
            self.assertEqual(c.execute("SELECT * FROM ontology.link_rows WHERE starts_with("
                                       "link_id, %s) ORDER BY link_id", (PREFIX,)).fetchall(),
                             before)




def _worker_read(job):
    """A worker's read through its overlay session, and what a write there gets."""
    sql, params = job
    from ontology import mention_firm_parallel
    conn = mention_firm_parallel._WORKER["conn"]   # where the overlay initializer puts it
    rows = [tuple(r) for r in conn.execute(sql, params).fetchall()]
    try:
        conn.pg._pg.execute("DELETE FROM ontology.link_rows WHERE false")
        refused = ""
    except Exception as exc:   # noqa: BLE001 - the refusal is the result
        refused = str(exc)
        conn.pg._pg.rollback()
    return rows, refused


def sqlite_meta(layer):
    import sqlite3
    from ontology.step_migrate import TABLE
    c = sqlite3.connect(layer)
    try:
        return dict(c.execute(f"SELECT key, value FROM {TABLE}").fetchall())
    finally:
        c.close()


class ExtractorNarrowingTests(CommittedRunTests):
    """``mentions`` (:data:`step_staging.NARROWED`): a re-derived link differing from the
    served one only by its reason's ``extractor`` keeps the served row; any other
    difference replaces it; a served link the run does not hold is removed; a new one is
    written. Then the overlay of a later step shows exactly that."""

    test_the_committed_run_applied_in_chunks_equals_the_one_transaction = None
    test_a_layer_scoped_to_documents_replaces_only_their_links = None
    test_a_step_that_skipped_documents_is_refused = None

    def test_only_an_extractor_difference_keeps_the_served_row(self):
        import psycopg
        from ontology import pg_step_inplace as ip
        from ontology import store_ddl
        from operating.pg import migrate_chunks as mc
        from operating.pg import step_staging
        from operating.pg.sqlite_compat import PgCompatConnection
        self._base()
        cols = list(store_ddl.LINK_COLUMNS)
        with psycopg.connect(self.other) as pg:
            ids = [r[0] for r in pg.execute(
                "SELECT link_id FROM ontology.link_rows WHERE starts_with(link_id, %s) "
                "ORDER BY link_id", (PREFIX,)).fetchall()]
            served = {r[0]: r for r in mc._served_links(
                pg.cursor(), "starts_with(l.link_id, %s)", [PREFIX])}
            stamped = [{"feature": "identifier_mention", "extractor": "grammar@old"}]
            pg.execute("UPDATE ontology.link_rows SET reason = %s, reason_id = NULL WHERE "
                       "starts_with(link_id, %s)", (json.dumps(stamped), PREFIX))
            pg.commit()
        self.assertGreaterEqual(len(ids), 3)
        same_but_stamp, changed, removed = ids[0], ids[1], ids[2]
        store = self.case.fixture.root / "mentions.sqlite"
        local = ip._local_store(store)
        rows = []
        for i in ids:
            if i == removed:
                continue
            row = dict(zip(cols, served[i]))
            row["reason"] = json.dumps([{"feature": "identifier_mention",
                                         "extractor": "grammar@new"}])
            if i == changed:
                row["tier"] = "corroborated"
            rows.append(row)
        rows.append({**rows[0], "link_id": PREFIX + "zz-new"})
        local.add("links", rows)
        if not local.connection.in_transaction:
            local.connection.execute("BEGIN IMMEDIATE")
        from pathlib import Path
        Path(local.keep_as_part()).replace(store)
        scope = [{"step": "mentions", "prefix": PREFIX, "end": PREFIX[:-1] + ";",
                  "from": None}]
        got = step_staging.stage(self.other, "run-1", "mentions", 0, store, scope)
        self.assertEqual((got["extractor_only"], got["written"], got["gone"]),
                         (len(ids) - 2, 2, 2))
        # a later step's view: the served row kept (the old stamp), the changed one
        # replaced, the removed one gone, the new one there; the step itself sees none
        views = {}
        for step, hidden in (("reviews", []), ("mentions", scope)):
            pg = PgCompatConnection(self.other, statement_timeout_ms=0)
            try:
                ip.Overlay(pg).show(hidden, staged_before=step)
                views[step] = {r[0]: r for r in pg._pg.execute(
                    "SELECT link_id, tier, reason FROM pg_temp.links WHERE "
                    "starts_with(link_id, %s)", (PREFIX,)).fetchall()}
            finally:
                pg.close()
        self.assertEqual(views["mentions"], {})
        view = views["reviews"]
        self.assertEqual(len(view), len(ids))
        self.assertIn("grammar@old", view[same_but_stamp][2])
        self.assertEqual(view[changed][1], "corroborated")
        self.assertNotIn(removed, view)
        self.assertIn(PREFIX + "zz-new", view)

    def test_a_part_staging_never_blocks_the_reads_of_the_others(self):
        """Mention parts stage while the other parts read the staged links through their
        overlays: a stage in progress holds no lock a reader waits on (11-11: the schema
        DDL inside each stage's transaction held the links table exclusively, and every
        other part's reads waited for each whole stage)."""
        import psycopg
        from ontology import pg_step_inplace as ip
        from ontology import store_ddl
        from operating.pg import migrate_chunks as mc
        from operating.pg import step_staging
        self._base()
        cols = list(store_ddl.LINK_COLUMNS)
        with psycopg.connect(self.other) as pg:
            served = list(mc._served_links(pg.cursor(), "starts_with(l.link_id, %s)",
                                           [PREFIX]))
        store = self.case.fixture.root / "mentions.sqlite"
        local = ip._local_store(store)
        local.add("links", [dict(zip(cols, r)) for r in served])
        if not local.connection.in_transaction:
            local.connection.execute("BEGIN IMMEDIATE")
        from pathlib import Path
        Path(local.keep_as_part()).replace(store)
        scope = [{"step": "mentions", "prefix": PREFIX, "end": PREFIX[:-1] + ";",
                  "from": None}]
        step_staging.stage(self.other, "run-1", "mentions", 0, store, scope)
        reads = []
        rows = step_staging._store_rows

        def reading(path):
            # another part's session reads the stage while this one is mid-transaction
            with psycopg.connect(self.other) as other:
                other.execute("SET lock_timeout = '2s'")
                reads.append(other.execute(
                    f"SELECT count(*) FROM {step_staging.SCHEMA}.links l WHERE NOT EXISTS "
                    f"(SELECT 1 FROM {step_staging.SCHEMA}.gone g WHERE g.link_id = "
                    "l.link_id)").fetchone()[0])
            yield from rows(path)

        step_staging._store_rows = reading
        try:
            got = step_staging.stage(self.other, "run-1", "mentions", 1, store, scope)
        finally:
            step_staging._store_rows = rows
        self.assertEqual(len(reads), 1)
        self.assertEqual(got["same"], len(served))

    def test_a_steps_workers_read_its_overlay_and_cannot_write(self):
        """A step judging with worker processes (``pg_step_inplace.OVERLAY_WORKERS``,
        ``unique_mentions``): each worker's own Postgres session shows the step's overlay
        (the staged steps before it, its hidden entries) row for row, and refuses a write."""
        import psycopg
        from concurrent import futures
        import multiprocessing
        import sys
        import types
        from unittest import mock
        from ontology import pg_step_inplace as ip
        from ontology import store_ddl
        from operating.pg import migrate_chunks as mc
        from operating.pg import step_staging
        from operating.pg.sqlite_compat import PgCompatConnection
        self._base()
        cols = list(store_ddl.LINK_COLUMNS)
        with psycopg.connect(self.other) as pg:
            served = list(mc._served_links(pg.cursor(), "starts_with(l.link_id, %s)",
                                           [PREFIX]))
        self.assertGreaterEqual(len(served), 3)
        store = self.case.fixture.root / "mentions.sqlite"
        local = ip._local_store(store)
        rows = [dict(zip(cols, r)) for r in served[1:]]   # the first one removed
        rows[0]["tier"] = "corroborated"                     # one replaced
        local.add("links", rows + [{**rows[1], "link_id": PREFIX + "zz-new"}])
        if not local.connection.in_transaction:
            local.connection.execute("BEGIN IMMEDIATE")
        from pathlib import Path
        Path(local.keep_as_part()).replace(store)
        scope = [{"step": "mentions", "prefix": PREFIX, "end": PREFIX[:-1] + ";",
                  "from": None}]
        step_staging.stage(self.other, "run-1", "mentions", 0, store, scope)
        # the step's own range hidden (a later step's entry), and the staged mentions shown
        hidden = [{"step": "with_people", "prefix": "person:", "end": "person;",
                   "from": None}]
        read = ("SELECT link_id, tier, from_object FROM links WHERE link_id >= ? AND "
                "link_id < ? ORDER BY link_id")
        pg = PgCompatConnection(self.other, statement_timeout_ms=0)
        try:
            ip.Overlay(pg).show(hidden, staged_before="unique_mentions")
            mine = [tuple(r) for r in pg.execute(read, (PREFIX, PREFIX[:-1] + ";")).fetchall()]
        finally:
            pg.close()
        self.assertIn((PREFIX + "zz-new",), [r[:1] for r in mine])
        self.assertNotIn(served[0][0], [r[0] for r in mine])
        from ontology import mention_firm_parallel as mfp
        real_init = mfp._init
        work = self.case.fixture.root / "work"
        work.mkdir(exist_ok=True)
        builder = types.SimpleNamespace(temp=work / "no-step-input")
        if True:
            with ip._overlay_workers("unique_mentions", builder, hidden, True, work,
                                     self.other, ["crb-mid:"], "unique_mentions"):
                this = mfp
                self.assertIs(this._init, ip._overlay_worker_init)
                with futures.ProcessPoolExecutor(
                        2, mp_context=multiprocessing.get_context("spawn"),
                        initializer=this._init, initargs=(str(builder.temp),)) as pool:
                    got = list(pool.map(_worker_read,
                                        [(read, (PREFIX, PREFIX[:-1] + ";"))] * 3))
            self.assertEqual(builder.temp, work / "no-step-input")   # restored
            self.assertIs(mfp._init, real_init)   # restored
        for rows_seen, refused in got:
            self.assertEqual(rows_seen, mine)
            self.assertIn("read-only", refused)
        # applied as they go (the owner's run): the served links hold the staged ones, so a
        # worker shows none staged again (shown again over the applied links, it read other
        # rows than the step's own overlay, 11-11)
        step_staging.apply_now(self.other, "run-1", "mentions")
        seen = []
        for staged_before in (None, "unique_mentions"):
            pg = PgCompatConnection(self.other, statement_timeout_ms=0)
            try:
                ip.Overlay(pg).show(hidden, staged_before=staged_before)
                seen.append([tuple(r) for r in pg.execute(read, (PREFIX, PREFIX[:-1] + ";"))])
            finally:
                pg.close()
        applied, twice = seen
        self.assertEqual(sorted(applied), sorted(mine))
        self.assertNotEqual(sorted(twice), sorted(applied))   # what the workers read before
        if True:
            with ip._overlay_workers("unique_mentions", builder, hidden, True, work,
                                     self.other, ["crb-mid:"], None):
                this = mfp
                with futures.ProcessPoolExecutor(
                        2, mp_context=multiprocessing.get_context("spawn"),
                        initializer=this._init, initargs=(str(builder.temp),)) as pool:
                    got = list(pool.map(_worker_read,
                                        [(read, (PREFIX, PREFIX[:-1] + ";"))] * 2))
        for rows_seen, _ in got:
            self.assertEqual(rows_seen, applied)


if __name__ == "__main__":
    unittest.main()
