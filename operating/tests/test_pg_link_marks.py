"""The link slice kept in Postgres (migration 0028, CC-P-07): ``ontology.link_marks``.

Live 11-13..11-14: every host's first read of a data version scanned the whole 128 GB
link_rows for the link slice (213 calls, 316-421 s each). Rebuilt by ``link_marks.refresh``
and carried over a delta layer by its apply, a reader takes it from a few rows instead, only
while it is current.
Checked here at the boundary: the
slice a reader gets equals the composed read on every evidence shape after a refresh; it is
not used (readers fall back to link_rows; nothing refuses the write itself) after any later
write (to the table, straight to a partition, an update, a delete, a TRUNCATE), a write during the refresh, a moved data version or generation, other gated
schemes, a new partition, another table or counts not kept; a layer's fold keeps it current
and covering; a whole facts read takes it without reading a link_rows range; undo drops it;
an apply's command line (train, steps, link_apply, link_partitions) refreshes it after.
Scratch Postgres database, no network, no credentials. Evidence: isolated-fixture.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest import mock

from operating.tests import pg_testdb
from operating.tests.test_pg_schema_migrate import MIGRATIONS

TEXTS = {9001: "graph-mail://m/1", 9002: "api://public.x/1", 9003: 'graph-mail://q"x',
         9004: "sharepoint://s/2", 9005: "teams-chat://c/3"}
SKELETONS = {9101: '["%s","%s"]', 9102: '["sharepoint://lit/9","%s"]', 9103: '{"a":"%s"}',
             9104: '["100%% api://p","%s"]', 9105: '["x\\u0041","%s"]', 9106: '["%s"]'}
ROWS = [  # (id, type, evidence_id, ek, slots, evidence_handles, superseded_at)
    ("L1", "A.b", 9101, 2, [9001, 9002], None, None),
    ("L2", "A.b", 9102, 1, [9004], None, "2026-01-02T00:00:00Z"),
    ("L3", "A.b", 9101, 2, [9003, 9004], None, None),   # an odd slot text
    ("L4", "C.d", 9103, 1, [9005], None, None),         # not an array
    ("L5", "C.d", 9104, 1, [9001], None, "2025-05-05"),
    ("L6", "C.d", 9105, 1, [9004], None, None),
    ("L7", "C.d", 9106, 2, [9002, 9005], None, None),   # a surplus slot
    ("L8", "E.f", None, 0, [], '["graph-mail://p/7"]', "not a time"),
    ("L9", "E.f", 9101, 2, [9004, 9005, 9001], None, None),
]
INSERT = ("INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, tier, "
          "binding_kind, why_not, markings, recorded_at, reason, evidence_id, ek, slots, "
          "evidence_handles, superseded_at) VALUES (%s, %s, 'a', 'b', 'T1', 'key', '', %s, "
          "'2026-01-01', 'r', %s, %s, %s, %s, %s)")


def _insert(pg, rows):
    pg.execute("SELECT pg_stat_force_next_flush()")   # the counts shared at once
    for lid, ltype, evidence, ek, slots, plain, until in rows:
        pg.execute(INSERT, (lid, ltype, f'["m{lid[-1]}"]', evidence, ek, slots, plain, until))


class LinkMarks(unittest.TestCase):
    plain = False   # link_rows partitioned (a new database)
    layers = __import__("itertools").count(1)

    def setUp(self):
        import psycopg
        name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                if self.plain and migration.name.startswith("0017"):
                    # a table holding rows stays plain (the served one until DI-03)
                    _insert(pg, ROWS[-1:])
                pg.execute(migration.read_text(encoding="utf-8"))
            if self.plain:
                pg.execute("DELETE FROM ontology.link_rows")
            kind = pg.execute("SELECT relkind FROM pg_class "
                              "WHERE oid = 'ontology.link_rows'::regclass").fetchone()[0]
            self.assertEqual(kind, "r" if self.plain else "p")
            for tid, text in TEXTS.items():
                pg.execute("INSERT INTO ontology.link_texts VALUES (%s, %s)", (tid, text))
            for kid, text in SKELETONS.items():
                pg.execute("INSERT INTO ontology.link_skeletons VALUES (%s, %s)", (kid, text))
            _insert(pg, ROWS)
        facts_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, facts_dir, True)
        self.enterContext(mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": facts_dir}))

    def sql(self, *statements):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("SELECT pg_stat_force_next_flush()")   # the counts shared at once
            out = None
            for statement in statements:
                cur = pg.execute(*statement) if isinstance(statement, tuple) else \
                    pg.execute(statement)
                out = cur.fetchall() if cur.description else out
            return out

    def as_superuser(self, *statements):
        """``statements`` on one connection as the cluster's superuser login (scratch_db), or
        None when this cluster gives the run none."""
        import psycopg
        from operating.pg import scratch_db
        try:
            pg = psycopg.connect(scratch_db.superuser_dsn(self.dsn), autocommit=True)
        except psycopg.OperationalError:
            return None
        with pg:
            if not pg.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                              ).fetchone()[0]:
                return None
            out = None
            for statement in statements:
                cur = pg.execute(*statement) if isinstance(statement, tuple) else \
                    pg.execute(statement)
                out = cur.fetchall() if cur.description else out
            return out

    def reader(self):
        from operating.pg.ontology_reader import PgStoreReader
        reader = PgStoreReader.open(self.dsn, only=False)
        self.addCleanup(reader.close)
        return reader

    def slice_of(self, kept: bool):
        """The link slice a reader gets: from the kept rows (not taken when not current:
        None) or composed from link_rows the generic way."""
        from operating import ontology_tools as ot, ontology_tools_traverse as tt
        reader = self.reader()
        if kept:
            rows = reader.link_marks(sorted(tt.GATED_SCHEMES))
            return None if rows is None else tt._folded_slices(rows)
        reader.slice_rows = lambda kind, schemes, **kw: None
        tools = tt.TraverseTools(ot.OntologyTools(reader, ot.ReadPolicy("plx", {})))
        with reader.lock:
            return tools._slices("link")

    def refresh(self):
        from operating.pg import link_marks
        return link_marks.refresh(self.dsn, workers=2, log=io.StringIO(), settle=0)

    def assertCovers(self, kept, composed):
        """Every marking and gated handle of the composed slice is kept, with an end at
        least as late (more is allowed: a slice may only look less covered)."""
        for name, (marks, handles) in composed.items():
            self.assertIn(name, kept)
            got_marks, got_handles = kept[name]
            for mark, until in marks.items():
                self.assertGreaterEqual(got_marks.get(mark, ""), until, (name, mark))
            if handles is None:
                self.assertIsNone(got_handles, name)
                continue
            if got_handles is None:
                continue   # past the cap: no handle is taken as checked
            for handle, until in handles.items():
                self.assertGreaterEqual(got_handles.get(handle, ""), until, (name, handle))

    def kept_now(self):
        from operating import ontology_tools_traverse as tt
        return self.reader().link_marks(sorted(tt.GATED_SCHEMES)) is not None

    def test_a_refresh_keeps_the_composed_slice_exactly(self):
        self.assertIsNone(self.slice_of(True), "never built: read from link_rows")
        out = self.refresh()
        self.assertTrue(out["readers_use_it"], out)
        composed = self.slice_of(False)
        self.assertEqual(self.slice_of(True), composed)
        # one type alone, as a call's typed read asks it
        from operating import ontology_tools_traverse as tt
        rows = self.reader().link_marks(sorted(tt.GATED_SCHEMES), "C.d")
        self.assertEqual(tt._folded_slices(rows), {"C.d": composed["C.d"]})
        # the slice_rows entry every caller goes through takes the kept rows
        reader = self.reader()
        with mock.patch.object(reader, "link_odd", side_effect=AssertionError("scanned")):
            self.assertEqual(tt._folded_slices(reader.slice_rows("link", sorted(
                tt.GATED_SCHEMES))), composed)
        self.assertTrue(self.refresh()["readers_use_it"], "a second refresh is the same")
        self.assertEqual(self.slice_of(True), composed)

    def test_any_later_write_sends_readers_to_link_rows_until_a_refresh(self):
        leaf = self.sql("SELECT relid::regclass::text FROM pg_partition_tree("
                        "'ontology.link_rows') WHERE isleaf LIMIT 1")
        target = leaf[0][0] if leaf else "ontology.link_rows"
        writes = [
            ("an insert", (INSERT, ("W1", "Q.q", '["secret"]', None, 0, [], "[]", None))),
            # straight into a partition, past anything on the parent (review b560a49f98)
            ("a write to a leaf", (INSERT.replace("ontology.link_rows", target),
                                   ("W2", "Q.r", '["secret"]', None, 0, [], "[]", None))),
            ("an update", ("UPDATE ontology.link_rows SET markings = '[\"hr\"]' "
                           "WHERE link_id = 'L2'",)),
            ("a supersede", ("UPDATE ontology.link_rows SET superseded_at = '2026-02-02' "
                             "WHERE link_id = 'L1'",)),
            ("a delete", ("DELETE FROM ontology.link_rows WHERE link_id = 'L6'",)),
        ]
        for what, statement in writes:
            self.assertTrue(self.refresh()["readers_use_it"], what)
            self.sql(statement)
            self.assertFalse(self.kept_now(), what)
            self.assertIsNone(self.slice_of(True), what)
            self.assertTrue(self.refresh()["readers_use_it"], what)
            self.assertEqual(self.slice_of(True), self.slice_of(False), what)
        kept = self.slice_of(True)
        self.assertIn('["secret"]', kept["Q.q"][0])
        self.assertIn('["secret"]', kept["Q.r"][0])
        self.assertIn('["hr"]', kept["A.b"][0])
        self.sql(f"TRUNCATE {target}")
        self.assertFalse(self.kept_now(), "a truncate")

    def test_a_state_it_cannot_vouch_for_reads_link_rows(self):
        from operating import ontology_tools_traverse as tt
        self.assertTrue(self.refresh()["readers_use_it"])
        # other gated schemes than the ones it was built for
        self.assertIsNone(self.reader().link_marks(["graph-mail"]))
        # a load or a delta layer moved the data version (no link written)
        self.sql("INSERT INTO ontology.load_state (name, value, updated_at) VALUES "
                 "('delta:x', '{}', now() + interval '1 hour')")
        self.assertFalse(self.kept_now())
        self.assertTrue(self.refresh()["readers_use_it"])
        # another generation
        self.sql("INSERT INTO ontology.load_state (name, value) VALUES ('generation', "
                 "'{\"content_digest\": \"other\"}') ON CONFLICT (name) DO UPDATE SET "
                 "value = excluded.value, updated_at = ontology.load_state.updated_at")
        self.assertFalse(self.kept_now())
        self.assertTrue(self.refresh()["readers_use_it"])
        # counts not kept: no counter, never vouched
        # (track_counts is a superuser setting: plx_test cannot set it on the gate's cluster)
        vouched = self.as_superuser("SET track_counts = off", (
            "SELECT ontology.link_marks_vouched(%s::text[])", (sorted(tt.GATED_SCHEMES),)))
        if vouched is not None:
            self.assertEqual(vouched[0][0], False)
        # a partition swapped in (a rebuild's fill): the leaves differ
        if not self.plain:
            self.sql("CREATE TABLE ontology.lr_swap (LIKE ontology.link_rows INCLUDING DEFAULTS "
                     "INCLUDING CONSTRAINTS)",
                     "INSERT INTO ontology.lr_swap (link_id, link_type, from_object, to_object, "
                     "tier, binding_kind, why_not, markings, recorded_at, reason, "
                     "evidence_handles) VALUES ('S1', 'Z.z', 'a', 'b', 'T1', 'key', '', "
                     "'[\"secret\"]', '2026-01-01', 'r', '[]')")
            self.assertTrue(self.refresh()["readers_use_it"])
            self.sql("ALTER TABLE ontology.link_rows ATTACH PARTITION ontology.lr_swap "
                     "FOR VALUES IN ('Z.z')")
            self.assertFalse(self.kept_now())
            self.assertTrue(self.refresh()["readers_use_it"])
            self.assertIn('["secret"]', self.slice_of(True)["Z.z"][0])
        else:   # another table under the name (the plain-to-partitioned conversion)
            self.sql("ALTER TABLE ontology.link_rows RENAME TO link_rows_old",
                     "CREATE TABLE ontology.link_rows (LIKE ontology.link_rows_old INCLUDING ALL)",
                     "INSERT INTO ontology.link_rows SELECT * FROM ontology.link_rows_old")
            self.assertFalse(self.kept_now(), "another table: never vouched for")
        # undo: an older schema (no kept slice), read from link_rows, no error
        from operating.pg import link_marks
        link_marks.undo(self.dsn)
        self.assertIsNone(self.reader().link_marks(sorted(tt.GATED_SCHEMES)))
        self.assertFalse(link_marks.refresh(self.dsn, settle=0)["readers_use_it"])

    def build(self):
        """The build record (``ontology.link_marks_build``) as ``status`` gives it."""
        import psycopg
        from operating.pg import link_marks
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            return link_marks.status(pg)["build"]

    def work_tables(self):
        return self.sql("SELECT relname FROM pg_class c JOIN pg_namespace n ON "
                        "n.oid = c.relnamespace WHERE nspname = 'ontology' AND relname IN "
                        "('link_marks_parts', 'link_marks_next', 'link_marks_catchup')")

    def test_a_write_outside_a_layer_during_the_build_leaves_it_unkept(self):
        from operating.pg import link_marks
        real = link_marks._bounds

        def write_then_bounds(conn, pieces):
            self.sql((INSERT, ("D1", "A.b", '["late"]', None, 0, [], "[]", None)))
            return real(conn, pieces)

        with mock.patch.object(link_marks, "_bounds", side_effect=write_then_bounds):
            out = self.refresh()
        self.assertFalse(out["readers_use_it"], out)
        self.assertEqual(out["phase"], "building")
        self.assertIsNotNone(out["build"]["failed_at"], out)
        self.assertFalse(out["build"]["active"])
        self.assertEqual(out["build"]["why"], link_marks.FAILED)
        self.assertEqual(self.work_tables(), [], "a failed build leaves no tables")
        self.assertTrue(self.refresh()["readers_use_it"])
        self.assertIn('["late"]', self.slice_of(True)["A.b"][0])
        self.assertIsNone(self.build()["failed_at"], "a kept build clears the failure")

    def test_layers_go_on_during_the_build_and_are_caught_up_at_a_short_swap(self):
        # review 3f30c615a8: the rebuild held the delta layers off for the whole read
        # (30-60 min on served); now they write beside it and the swap holds them seconds
        import psycopg
        from operating.pg import link_marks
        real = link_marks._bounds
        seen = []
        moved = INSERT.replace("'a', 'b'", "'a2', 'b'")

        def layers_during_the_read(conn, pieces):
            with psycopg.connect(self.dsn, autocommit=True) as other:
                seen.append(other.execute(
                    f"SELECT pg_try_advisory_lock({link_marks.WRITERS_LOCK})").fetchone()[0])
                other.execute(f"SELECT pg_advisory_unlock({link_marks.WRITERS_LOCK})")
            seen.append(self.kept_now())   # readers: not kept while it builds
            # two layers, each in its own transaction: the build counts both in
            self.fold([(moved, ("G1", "G.g", '["mid"]', None, 0, [],
                                '["sharepoint://g/1"]', None))])
            self.fold([("UPDATE ontology.link_rows SET from_object = 'a2', "
                        "markings = '[\"later\"]' WHERE link_id = 'L4'",)])
            return real(conn, pieces)

        with mock.patch.object(link_marks, "_bounds", side_effect=layers_during_the_read):
            out = self.refresh()
        self.assertEqual(seen, [True, False], "a layer takes the lock mid-build")
        self.assertTrue(out["readers_use_it"], out)
        self.assertEqual(out["build"]["layers"], 2)
        kept, composed = self.slice_of(True), self.slice_of(False)
        self.assertCovers(kept, composed)
        self.assertIn('["mid"]', kept["G.g"][0])
        self.assertIn('["later"]', kept["C.d"][0])
        self.assertEqual(self.work_tables(), [])
        # the next layer carries the kept slice as before
        current, added = self.fold([(moved, ("G2", "G.g", '["next"]', None, 0, [], "[]",
                                             None))])
        self.assertTrue(current)
        self.assertTrue(self.kept_now())

    def test_a_layer_under_way_at_the_start_fails_the_build_and_records_it(self):
        import psycopg
        from operating.pg import link_marks
        self.sql((INSERT, ("W1", "A.b", '["w"]', None, 0, [], "[]", None)))
        with psycopg.connect(self.dsn, autocommit=True) as layer:
            layer.execute(f"SELECT pg_advisory_lock({link_marks.WRITERS_LOCK})")
            with mock.patch.object(link_marks, "WRITERS_WAIT_SECONDS", 0.2):
                out = self.refresh()
        self.assertFalse(out["readers_use_it"])
        self.assertIn("writers' lock", out["build"]["why"])
        self.assertIsNotNone(out["build"]["failed_at"])
        self.assertTrue(self.refresh()["readers_use_it"])
        self.assertIn('["w"]', self.slice_of(True)["A.b"][0])

    def test_a_failing_worker_records_its_time_and_the_back_off_holds(self):
        # live 11-15 5:57 AM ET: the build died (its own workers deadlocked) with why NULL,
        # so no back-off applied and every layer would have started it again
        from operating.pg import link_marks
        with mock.patch.object(link_marks, "_STAGE", link_marks._STAGE + " nonsense"):
            out = self.refresh()
        self.assertFalse(out["readers_use_it"], out)
        self.assertIsNotNone(out["build"]["failed_at"], out)
        self.assertTrue(out["build"]["why"].startswith("SyntaxError"), out["build"])
        self.assertEqual(self.work_tables(), [])
        with mock.patch.object(link_marks, "refresh", side_effect=AssertionError("again")):
            later = link_marks.refresh_if_stale(self.dsn, retry_after=3600)
            self.assertTrue(later.get("retry_later"), later)
            self.assertEqual(link_marks.start_rebuild(self.dsn), "never_built")
        # a build killed mid-way (active, nobody holds the lock): its start backs off too
        self.sql("UPDATE ontology.link_marks_build SET active = true, failed_at = NULL, "
                 "started_at = now() - interval '10 minutes'")
        with mock.patch.object(link_marks, "refresh", side_effect=AssertionError("again")):
            self.assertTrue(link_marks.refresh_if_stale(self.dsn, retry_after=3600)
                            .get("retry_later"))
        out = link_marks.refresh_if_stale(self.dsn, retry_after=60, workers=2,
                                          log=io.StringIO(), settle=0)
        self.assertTrue(out["readers_use_it"], out)

    def test_workers_never_write_the_same_key_and_lose_no_row(self):
        # the 11-15 deadlock: two workers' INSERT ... ON CONFLICT on one key from different
        # link-id ranges. Every range here shares the keys; one range per link, 8 workers.
        from operating.pg import link_marks
        for n in (1, 10, 60):
            self.sql("DELETE FROM ontology.link_rows")
            self.sql(*[(INSERT, (f"K{i:03d}", "S.s", '["same"]', 9101, 2, [9001, 9004],
                                 None, f"2026-03-{1 + i % 28:02d}" if i % 3 else None))
                       for i in range(n)])
            ids = [row[0] for row in self.sql("SELECT link_id FROM ontology.link_rows "
                                              "ORDER BY link_id COLLATE \"C\"")]
            staged = []

            def every_link(conn, pieces):
                # its one index: the generated id's primary key (LM-MIG), nothing on the
                # slice's key for two workers to wait on
                staged.append(self.sql(
                    "SELECT array_agg(a.attname::text || ':' || i.indisprimary::text) "
                    "FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND "
                    "a.attnum = ANY (i.indkey) WHERE i.indrelid = "
                    "'ontology.link_marks_parts'::regclass")[0][0])
                return [None, *ids[1:], None]

            with mock.patch.object(link_marks, "_bounds", side_effect=every_link):
                out = link_marks.refresh(self.dsn, workers=8, log=io.StringIO(), settle=0)
            self.assertTrue(out["readers_use_it"], (n, out))
            self.assertEqual(staged, [["id:true"]],
                             "the staging table has no slice key to wait on")
            self.assertEqual(self.slice_of(True), self.slice_of(False), n)
            self.assertEqual(self.sql("SELECT count(*) FROM ontology.link_marks "
                                      "GROUP BY link_type, markings, handle "
                                      "HAVING count(*) > 1"), [], n)

    def test_the_swap_is_whole_or_nothing(self):
        import threading
        import psycopg
        from operating.pg import link_marks
        composed = self.slice_of(False)
        # a reader holding the old table through every swap: nothing changes, recorded
        with psycopg.connect(self.dsn) as held:
            held.execute("SELECT count(*) FROM ontology.link_marks")
            with mock.patch.object(link_marks, "SWAP_TRIES", 2), \
                    mock.patch.object(link_marks, "SWAP_LOCK_TIMEOUT_MS", 100):
                out = self.refresh()
            self.assertEqual(held.execute("SELECT phase FROM ontology.link_marks_state"
                                          ).fetchone()[0], "building")
            held.rollback()
        self.assertFalse(out["readers_use_it"], out)
        self.assertIn("readers held", out["build"]["why"])
        self.assertEqual(self.work_tables(), [])
        # readers through a build and its swap: no slice, or the whole one; the grants of
        # the old table carried to the one swapped in (the API's role reads it)
        self.sql("GRANT SELECT ON ontology.link_marks TO PUBLIC")
        from operating import ontology_tools_traverse as tt
        got, stop, reader = [], threading.Event(), self.reader()

        def read():
            while not stop.is_set():
                rows = reader.link_marks(sorted(tt.GATED_SCHEMES))
                got.append(None if rows is None else tt._folded_slices(rows))

        thread = threading.Thread(target=read)
        thread.start()
        try:
            self.assertTrue(self.refresh()["readers_use_it"])
        finally:
            stop.set()
            thread.join()
        for slice_ in got + [self.slice_of(True)]:
            self.assertIn(slice_, (None, composed))
        self.assertEqual(self.slice_of(True), composed)
        self.assertTrue(self.sql("SELECT has_table_privilege('public', "
                                 "'ontology.link_marks', 'SELECT')")[0][0])
        self.assertEqual(self.sql("SELECT conname FROM pg_constraint WHERE conrelid = "
                                  "'ontology.link_marks'::regclass AND contype = 'p'"),
                         [("link_marks_pkey",)])

    def test_a_layer_starts_one_rebuild_apart_only_after_a_first_build(self):
        import psycopg
        from operating.pg import link_marks
        with mock.patch("subprocess.Popen") as popen:
            self.assertEqual(link_marks.start_rebuild(self.dsn), "never_built")
            self.assertTrue(self.refresh()["readers_use_it"])
            self.sql((INSERT, ("B1", "A.b", '["b"]', None, 0, [], "[]", None)))
            with psycopg.connect(self.dsn, autocommit=True) as other:
                other.execute(f"SELECT pg_advisory_lock({link_marks.LOCK})")
                self.assertEqual(link_marks.start_rebuild(self.dsn), "busy")
            with mock.patch.object(link_marks, "_last_start", 0.0), \
                    mock.patch.dict(os.environ, {"PLX_LINK_MARKS_LOG": os.devnull}):
                self.assertEqual(link_marks.start_rebuild(self.dsn), "started")
                self.assertEqual(link_marks.start_rebuild(self.dsn), "started_lately")
            self.sql("UPDATE ontology.link_marks_build SET failed_at = now()")
            with mock.patch.object(link_marks, "_last_start", 0.0):
                self.assertEqual(link_marks.start_rebuild(self.dsn), "retry_later")
        self.assertEqual(popen.call_count, 1)
        args, kw = popen.call_args
        self.assertIn("--if-stale", args[0])
        self.assertTrue(kw["start_new_session"])
        self.assertIs(kw["stdin"], __import__("subprocess").DEVNULL)

    def test_a_role_that_may_not_write_it_never_tries(self):
        # the API's read-only role: every API process started a refresh that failed
        import psycopg
        from operating.pg import link_marks
        self.assertTrue(self.refresh()["readers_use_it"])
        self.sql((INSERT, ("P1", "A.b", '["p"]', None, 0, [], "[]", None)))
        self.assertFalse(self.kept_now())
        login, role = self._login(), f"plx_marks_reader_{os.getpid()}_{int(self.plain)}"
        made = self.as_superuser(
            f"DROP ROLE IF EXISTS {role}",
            f"CREATE ROLE {role} NOLOGIN",
            f"GRANT USAGE ON SCHEMA ontology TO {role}",
            f"GRANT SELECT ON ALL TABLES IN SCHEMA ontology TO {role}",
            f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ontology TO {role}",
            f"GRANT {role} TO {login}")
        if made is None:
            self.skipTest("no superuser login on this cluster to make a read-only role")
        self.addCleanup(self.as_superuser, f"DROP OWNED BY {role}",
                        f"DROP ROLE IF EXISTS {role}")
        with mock.patch.object(link_marks, "refresh", side_effect=AssertionError):
            out = link_marks.refresh_if_stale(self._as_role(role))
        self.assertTrue(out.get("not_writable"), out)
        self.assertFalse(out["readers_use_it"])
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            self.assertEqual(pg.execute("SELECT phase FROM ontology.link_marks_state"
                                        ).fetchone()[0], "kept", "never reset")

    def _login(self):
        return self.sql("SELECT quote_ident(current_user)")[0][0]

    def _as_role(self, role):
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        parts = conninfo_to_dict(self.dsn)
        return make_conninfo(**{**parts, "options": f"{parts.get('options', '')} -c role={role}"})

    def fold(self, statements):
        """A layer's transaction as ``ontology_delta.apply_layer`` runs it: current or not
        before it writes, its link writes, its data version moved, the fold."""
        import psycopg
        from operating.pg import link_marks
        with psycopg.connect(self.dsn) as pg:
            pg.execute("SELECT pg_stat_force_next_flush()")
            with pg.cursor() as cur:
                cur.execute(f"SELECT pg_advisory_xact_lock({link_marks.WRITERS_LOCK})")
                start = link_marks.layer_start(cur)
                cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) "
                            "ON COMMIT DROP")
                cur.execute("INSERT INTO owned VALUES ('a2')")
                for statement in statements:
                    cur.execute(*statement)
                cur.execute("INSERT INTO ontology.load_state (name, value, updated_at) "
                            "VALUES (%s, '{}', now() + interval '2 hours')",
                            (f"delta:{next(self.layers)}",))
                added = link_marks.fold_layer(cur, start)
            pg.commit()
        return start["current"], added

    def test_a_layer_fold_keeps_it_current_and_covering(self):
        self.assertTrue(self.refresh()["readers_use_it"])
        moved = INSERT.replace("'a', 'b'", "'a2', 'b'")
        current, added = self.fold([
            (moved, ("F1", "A.b", '["fin"]', None, 0, [], '["sharepoint://f/1"]', None)),
            (moved, ("F2", "N.n", '["new"]', 9101, 2, [9001, 9004], None, "2026-04-04")),
            ("UPDATE ontology.link_rows SET from_object = 'a2', superseded_at = '2026-05-05' "
             "WHERE link_id = 'L9'",)])
        self.assertTrue(current)
        self.assertGreater(added, 0)
        self.assertTrue(self.kept_now(), "the fold moved it to the layer's version")
        kept, composed = self.slice_of(True), self.slice_of(False)
        self.assertCovers(kept, composed)
        self.assertEqual(kept["N.n"], composed["N.n"])
        self.assertIn('["fin"]', kept["A.b"][0])
        # a write outside any layer: refused again; a layer over it folds nothing
        self.sql((INSERT, ("X1", "A.b", '["x"]', None, 0, [], "[]", None)))
        self.assertFalse(self.kept_now())
        self.assertEqual(self.fold([(moved, ("F3", "A.b", '["y"]', None, 0, [], "[]", None))]),
                         (False, 0))
        self.assertFalse(self.kept_now())

    def test_a_whole_facts_read_takes_the_kept_slice_without_a_link_range(self):
        from operating import ontology_tools_traverse as tt
        self.assertTrue(self.refresh()["readers_use_it"])
        expected = self.slice_of(False)
        real = tt._range_slices

        def no_link(tools, kind, *args, **kw):
            if kind == "link":
                raise AssertionError("read a link_rows range")
            return real(tools, kind, *args, **kw)

        with mock.patch.object(tt, "_range_slices", side_effect=no_link):
            _, facts = tt._read_pg_facts_parallel(self.dsn, (), workers=2)
        self.assertEqual(facts[("slices", "link")], expected)
        # not kept: the ranges are read as before, the same facts
        self.sql("SELECT ontology.link_marks_stale('test')")
        _, facts = tt._read_pg_facts_parallel(self.dsn, (), workers=2)
        self.assertEqual(facts[("slices", "link")], expected)

    def test_an_api_start_refreshes_only_when_asked_and_stale(self):
        from operating.pg import link_marks
        with mock.patch.dict(os.environ, {"PLX_LINK_MARKS_REFRESH": "off"}):
            self.assertIsNone(link_marks.start_refresh_if_stale(self.dsn))
        out = link_marks.refresh_if_stale(self.dsn, settle=0)
        self.assertTrue(out["readers_use_it"])
        with mock.patch.object(link_marks, "refresh", side_effect=AssertionError("rebuilt")):
            self.assertTrue(link_marks.refresh_if_stale(self.dsn)["readers_use_it"])


    def test_an_apply_refreshes_it_after(self):
        from operating.pg import link_marks, ontology_migrate
        self.assertTrue(self.refresh()["readers_use_it"])
        self.sql((INSERT, ("A1", "Q.q", '["secret"]', None, 0, [], "[]", None)))
        self.assertFalse(self.kept_now())
        with mock.patch.dict(os.environ, {"PLX_LINK_MARKS_REFRESH": "off"}):
            self.assertIsNone(link_marks.after_apply(self.dsn, "train", settle=0))
        self.assertFalse(self.kept_now())
        # the train's command line: an applied layer refreshes (a refused one does not)
        calls = []
        with mock.patch.object(ontology_migrate, "apply_migration",
                               return_value={"applied": False}), \
                mock.patch.object(link_marks, "after_apply",
                                  side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(ontology_migrate.main(["--dsn", self.dsn, "apply", "x"]), 1)
        self.assertEqual(calls, [])
        real = link_marks.after_apply
        with mock.patch.object(ontology_migrate, "apply_migration",
                               return_value={"applied": True}), \
                mock.patch.object(link_marks, "after_apply",
                                  side_effect=lambda d, w: real(d, w, settle=0)), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(ontology_migrate.main(["--dsn", self.dsn, "apply", "x"]), 0)
        self.assertTrue(self.kept_now())
        self.assertIn('["secret"]', self.slice_of(True)["Q.q"][0])
        # a failed refresh never fails the apply that already committed
        with mock.patch.object(link_marks, "refresh_if_stale", side_effect=RuntimeError), \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertIsNone(link_marks.after_apply(self.dsn, "steps"))

    # LM-MIG: the build record declared by migration 0032, and a
    # build killed while marked active no longer holds the delta's rebuilds off for good

    def test_migration_0032_declares_the_build_record_and_keeps_one_already_there(self):
        from operating.pg import link_marks
        (owner, me, rows), = self.sql(
            "SELECT pg_get_userbyid(c.relowner), current_user, (SELECT count(*) FROM "
            "ontology.link_marks_build) FROM pg_class c "
            "WHERE c.oid = 'ontology.link_marks_build'::regclass")
        self.assertEqual((owner, rows), (me, 1))
        (api,), = self.sql("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api')")
        if api:
            self.assertEqual(self.sql("SELECT has_table_privilege('plx_api', "
                                      "'ontology.link_marks_build', 'SELECT')"), [(True,)])
        self.assertTrue(self.refresh()["readers_use_it"])
        kept = self.build()["kept_at"]
        # the served database: the first build made it at run time, then 0032 runs
        from operating.tests.test_pg_schema_migrate import MIGRATIONS
        text = next(m for m in MIGRATIONS if m.name.startswith("0032")).read_text("utf-8")
        self.sql(text)
        self.assertEqual(self.build()["kept_at"], kept, "0032 keeps the record as it is")
        self.assertEqual(self.sql("SELECT count(*) FROM ontology.link_marks_build"), [(1,)])
        # a database without 0032: the first refresh makes it, granted as link_marks is
        self.sql("DROP TABLE ontology.link_marks_build",
                 "GRANT SELECT ON ontology.link_marks TO PUBLIC")
        self.assertTrue(self.refresh()["readers_use_it"])
        self.assertIsNotNone(self.build()["kept_at"])
        self.assertEqual(self.sql("SELECT has_table_privilege('public', "
                                  "'ontology.link_marks_build', 'SELECT')"), [(True,)])
        # an emptied record gets its one row back, no DDL
        self.sql("DELETE FROM ontology.link_marks_build")
        with mock.patch.object(link_marks, "BUILD_SQL", "SELECT nonsense()"):
            self.assertTrue(self.refresh()["readers_use_it"])
        self.assertEqual(self.sql("SELECT count(*) FROM ontology.link_marks_build"), [(1,)])

    def test_every_work_table_has_a_primary_key_and_0032_gives_leftovers_one(self):
        # train 6's undo capture refuses an ontology table without a primary key
        from operating.pg import link_marks
        real, seen = link_marks._bounds, []
        keyless = ("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = "
                   "c.relnamespace WHERE n.nspname = 'ontology' AND c.relkind IN ('r', 'p') "
                   "AND c.relname LIKE 'link_marks%' AND NOT EXISTS (SELECT 1 FROM "
                   "pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'p')")

        def look(conn, pieces):
            seen.append((sorted(r[0] for r in self.work_tables()), self.sql(keyless)))
            return real(conn, pieces)

        with mock.patch.object(link_marks, "_bounds", side_effect=look):
            self.assertTrue(self.refresh()["readers_use_it"])
        self.assertEqual(seen, [(["link_marks_catchup", "link_marks_parts"], [])])
        # a killed build's keyless leftovers (made by the code before): 0032 keys them
        self.sql("CREATE UNLOGGED TABLE ontology.link_marks_parts (LIKE ontology.link_marks)",
                 "INSERT INTO ontology.link_marks_parts VALUES ('A.b', 'm', '', 'x'), "
                 "('A.b', 'm', '', 'x')",
                 "CREATE UNLOGGED TABLE ontology.link_marks_catchup (object_id text NOT NULL)")
        self.assertEqual(sorted(r[0] for r in self.sql(keyless)),
                         ["link_marks_catchup", "link_marks_parts"])
        from operating.tests.test_pg_schema_migrate import MIGRATIONS
        text = next(m for m in MIGRATIONS if m.name.startswith("0032")).read_text("utf-8")
        self.sql(text, text)   # twice: idempotent
        self.assertEqual(self.sql(keyless), [])
        self.assertEqual(self.sql("SELECT count(DISTINCT id) FROM ontology.link_marks_parts"),
                         [(2,)])
        self.assertTrue(self.refresh()["readers_use_it"], "a build drops and remakes them")

    def _killed_build(self, hours_ago=0.1):
        """A build that marked itself active and made its work tables, then died."""
        from operating.pg import link_marks
        self.sql(f"CREATE UNLOGGED TABLE {link_marks.CATCHUP} ({link_marks.WORK_ID}, "
                 "object_id text NOT NULL)",
                 ("UPDATE ontology.link_marks_build SET active = true, broken = NULL, "
                  "failed_at = NULL, why = NULL, counter = ontology.link_marks_counter(), "
                  "started_at = now() - make_interval(secs => %s)", (hours_ago * 3600,)),
                 "UPDATE ontology.link_marks_state SET phase = 'building'")

    def test_a_build_killed_while_active_is_reset_by_the_next_layer(self):
        import psycopg
        from operating.pg import link_marks
        self.assertTrue(self.refresh()["readers_use_it"])
        self._killed_build()
        moved = INSERT.replace("'a', 'b'", "'a2', 'b'")
        with mock.patch.object(link_marks, "LAYER_SETTLE_SECONDS", 0.2):
            current, _ = self.fold([(moved, ("K1", "A.b", '["k"]', None, 0, [], "[]", None))])
        self.assertFalse(current)
        build = self.build()
        self.assertFalse(build["active"], build)
        self.assertEqual(build["why"], link_marks.DEAD)
        self.assertIsNotNone(build["failed_at"])
        self.assertEqual(self.work_tables(), [], "its catch-up table no longer fills")
        self.assertEqual(self.sql("SELECT why FROM ontology.link_marks_state"),
                         [(f"build failed: {link_marks.DEAD}",)])
        # the lock is free again for a refresher, and the delta's rebuild follows the
        # usual back-off, then starts (before: never, the dead build counted every layer in)
        with psycopg.connect(self.dsn, autocommit=True) as other:
            self.assertTrue(other.execute(
                f"SELECT pg_try_advisory_lock({link_marks.LOCK})").fetchone()[0])
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.dict(os.environ, {"PLX_LINK_MARKS_LOG": os.devnull}):
            with mock.patch.object(link_marks, "_last_start", 0.0):
                self.assertEqual(link_marks.start_rebuild(self.dsn), "retry_later")
            self.sql("UPDATE ontology.link_marks_build SET failed_at = now() - "
                     "interval '2 hours'")
            with mock.patch.object(link_marks, "_last_start", 0.0):
                self.assertEqual(link_marks.start_rebuild(self.dsn), "started")
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(self.refresh()["readers_use_it"])
        self.assertIn('["k"]', self.slice_of(True)["A.b"][0])

    def test_a_live_build_is_never_reset_and_a_stalled_one_keeps_its_tables(self):
        import psycopg
        from operating.pg import link_marks
        self.assertTrue(self.refresh()["readers_use_it"])
        self._killed_build()
        with psycopg.connect(self.dsn, autocommit=True) as refresher:
            refresher.execute(f"SELECT pg_advisory_lock({link_marks.LOCK})")
            current, _ = self.fold([("SELECT 1",)])
            build = self.build()
            self.assertTrue(build["active"], "a refresher holding its lock is alive")
            self.assertIsNone(build["why"])
            self.assertEqual(build["layers"], 1, "the layer counted in its chain")
            # active past the stale time: taken as stalled; its tables are its own
            self.sql("UPDATE ontology.link_marks_build SET started_at = now() - "
                     "interval '7 hours'")
            self.fold([("SELECT 1",)])
            build = self.build()
            self.assertFalse(build["active"])
            self.assertEqual(build["why"], link_marks.STALE)
            self.assertEqual(self.work_tables(), [("link_marks_catchup",)])
            self.assertEqual(build["layers"], 1, "not counted in any more")


class PlainLinkMarks(LinkMarks):
    """The same over a link_rows that is not partitioned (the served table today)."""
    plain = True


if __name__ == "__main__":
    unittest.main()
