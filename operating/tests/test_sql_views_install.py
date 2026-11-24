"""``sql_views.install`` on a store people are reading: a reader holding one guarded view
(a long plx.sql statement) never makes the readers of the other views wait, a reader of
the held view itself waits at most about the lock budget, an unchanged view is not made
again (no lock at all), and a table swapped under its view gets the view made again so
it reads the new table. A throwaway Postgres built from the business views fixture."""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from operating.pg import business_views as bv
from operating.pg import sql_views
from operating.tests import test_business_views as tbv

BUDGET_MS = 400


class InstallOnLiveStore(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        from operating.tests import pg_store_mode
        cls.store = tbv.build()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            for name in ("0038_own_issued_documents.sql", "0040_people_excluded_objects.sql", "0041_business_views.sql"):
                c.execute((MIGRATIONS_DIR / name).read_text())
        bv.build(cls.dsn, None, schema=tbv.SCHEMA, as_of=tbv.AS_OF)
        cls.views = cls._install()
        cls.dst = sql_views.guarded_schema()

    @classmethod
    def _install(cls):
        import psycopg
        for attempt in range(5):   # the roles are the cluster's: parallel modules may race
            try:
                return sql_views.install(cls.dsn)
            except psycopg.errors.InternalError:
                time.sleep(0.5 * (attempt + 1))
        return sql_views.install(cls.dsn)

    def connect(self):
        import psycopg
        c = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(c.close)
        return c

    def oids(self):
        with self.connect() as c:
            return {n: c.execute("SELECT to_regclass(%s)::oid", (n,)).fetchone()[0]
                    for n in self.views}

    def guarded(self):
        """The business views made (no comms view: its own rule, same path)."""
        return [n for n in self.views if n.startswith(self.dst + ".")
                and n.split(".", 1)[1] in bv.VIEWS and not bv.VIEWS[n.split(".", 1)[1]].fill]

    def hold(self, name):
        """A reader inside an open transaction that has read ``name`` (its ACCESS SHARE
        lock held until ``release``)."""
        c = self.connect()
        c.execute("BEGIN")
        c.execute(f"SELECT count(*) FROM {name}")
        return c

    def install_in_background(self):
        out = {}

        def run():
            try:
                out["views"] = self._install()
            except Exception as exc:  # noqa: BLE001 - reported by the test
                out["error"] = exc
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, out

    def probe(self, name, timeout_s):
        """Seconds one fresh reader takes to read ``name`` (None: it waited past
        ``timeout_s``)."""
        import psycopg
        with self.connect() as c:
            c.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")
            t0 = time.monotonic()
            try:
                c.execute(f"SELECT count(*) FROM {name}")
            except psycopg.errors.QueryCanceled:
                return None
            return time.monotonic() - t0

    def test_unchanged_install_takes_no_lock(self):
        before = self.oids()
        held = self.hold(self.guarded()[0])
        t, out = self.install_in_background()
        t.join(60)
        held.execute("COMMIT")
        self.assertFalse(t.is_alive(), "install waited on a reader of an unchanged view")
        self.assertNotIn("error", out)
        self.assertEqual(sorted(out["views"]), sorted(self.views))
        self.assertEqual(self.oids(), before, "an unchanged view was made again")

    def test_held_view_stalls_no_other_reader(self):
        names = self.guarded()
        self.assertGreaterEqual(len(names), 4, names)
        held_name = names[len(names) // 2]   # views made before it stay readable
        with self.connect() as c:   # every view changed: install makes each again
            for n in names:
                c.execute(f"COMMENT ON VIEW {n} IS NULL")
        before = self.oids()
        held = self.hold(held_name)
        with mock.patch.object(sql_views, "LOCK_TIMEOUT_MS", BUDGET_MS):
            t, out = self.install_in_background()
            waits: dict[str, list] = {}
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                for n in names:
                    waits.setdefault(n, []).append(self.probe(n, 3.0))
            held.execute("COMMIT")
            t.join(120)
        self.assertFalse(t.is_alive(), "install never finished after the reader left")
        self.assertNotIn("error", out)
        for n, got in waits.items():
            self.assertNotIn(None, got, f"a reader of {n} waited 3 s behind install")
            limit = BUDGET_MS / 1000 + 1.0 if n == held_name else 1.0
            self.assertLess(max(got), limit, (n, max(got)))
        after = self.oids()
        for n in names:
            self.assertNotEqual(after[n], before[n], f"{n} was not made again")
        with self.connect() as c:
            for n in names:
                self.assertEqual(
                    c.execute("SELECT obj_description(to_regclass(%s), 'pg_class')",
                              (n,)).fetchone()[0],
                    sql_views.tag(self._ddl(c, n), sql_views._refs(c, n)))

    def _ddl(self, c, n):
        from operating.pg import door
        name = n.split(".", 1)[1]
        with c.transaction():
            ddl = door.view_ddl(c, bv.VIEWS[name], bv.SCHEMA, self.dst)
        return ddl

    def test_swapped_table_gets_its_view_again(self):
        name = self.guarded()[0].split(".", 1)[1]
        src = f"{bv.SCHEMA}.{name}"
        with self.connect() as c:
            c.execute(f"CREATE TABLE {src}__next (LIKE {src} INCLUDING ALL)")
            c.execute(f"INSERT INTO {src}__next SELECT * FROM {src}")
            c.execute(f"ALTER TABLE {src} RENAME TO {name}__prev")
            c.execute(f"ALTER TABLE {src}__next RENAME TO {name}")
            self.assertEqual(sql_views._refs(c, f"{self.dst}.{name}"),
                             [f"{bv.SCHEMA}.{name}__prev"])
        self._install()
        with self.connect() as c:
            self.assertEqual(sql_views._refs(c, f"{self.dst}.{name}"), [src])
            self.assertEqual(sql_views._refs(c, f"{sql_views.page_schema()}.{name}"), [src])
            c.execute(f"DROP TABLE {bv.SCHEMA}.{name}__prev")

    def test_a_readers_door_check_waits_on_no_view_a_swap_holds(self):
        """plx.sql's door reads whether every view takes the ranges from the stored rules:
        a view held ACCESS EXCLUSIVE by a refresh's swap never makes it wait (pg_views
        did: its pg_get_viewdef locks each view); an older int4[] view is still seen."""
        from operating import ontology_tools_sql as ots
        swap = self.connect()
        swap.execute("BEGIN")
        swap.execute(f"LOCK TABLE {self.guarded()[0]} IN ACCESS EXCLUSIVE MODE")
        try:
            with self.connect() as c:
                c.execute("SET statement_timeout = 2000")
                t0 = time.monotonic()
                self.assertTrue(ots._ranges_only(c))
                self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            swap.execute("ROLLBACK")
        name = self.guarded()[0]
        with self.connect() as c:
            ddl = c.execute("SELECT pg_get_viewdef(%s::regclass)", (name,)).fetchone()[0]
            c.execute("BEGIN")
            c.execute(f"DROP VIEW {name}")
            c.execute(f"CREATE VIEW {name} AS SELECT 1 AS x WHERE 1 = ANY("
                      "(SELECT NULLIF(current_setting('plx.classes', true), '')::int4[])::int4[])")
            self.assertFalse(ots._ranges_only(c))
            c.execute("ROLLBACK")
            self.assertTrue(ddl and ots._ranges_only(c))

    def test_lock_retry_gives_up_loudly(self):
        """A lock that never frees: install's step fails after its tries, never hangs."""
        held = self.hold(self.guarded()[0])
        self.addCleanup(held.execute, "COMMIT")
        with self.connect() as c:
            with self.assertRaises(RuntimeError) as got:
                sql_views.short(c, lambda w: w.execute(
                    f"LOCK TABLE {self.guarded()[0]} IN ACCESS EXCLUSIVE MODE"),
                    "probe", lock_timeout_ms=100, tries=2)
        self.assertIn("probe", str(got.exception))


    def test_changed_comms_view_keeps_the_findings_view(self):
        """The findings view reads the guarded mail and Teams views: a changed comms view
        is made again with the findings view dropped first and made again after (a bare
        DROP fails on the dependent view), and the findings view reads the new one."""
        from operating import assist_readings
        from operating.pg import comms_findings
        with self.connect() as c:
            assist_readings.ensure(c)
            comms = comms_findings.present(c, self.dst)
        self.assertTrue(comms, "the fixture builds no guarded mail/Teams view")
        self._install()
        findings = f"{self.dst}.{comms_findings.VIEW}"
        changed = f"{self.dst}.{comms[0]}"
        with self.connect() as c:
            self.assertIsNotNone(c.execute("SELECT to_regclass(%s)", (findings,)).fetchone()[0])
            old = c.execute("SELECT to_regclass(%s)::oid", (changed,)).fetchone()[0]
            c.execute(f"COMMENT ON VIEW {changed} IS NULL")   # its DDL changed: made again
        self._install()
        with self.connect() as c:
            self.assertNotEqual(
                c.execute("SELECT to_regclass(%s)::oid", (changed,)).fetchone()[0], old)
            self.assertIn(changed, sql_views._refs(c, findings))
            # the findings view gone (a failed findings step): an unchanged install makes it
            c.execute(f"DROP VIEW {findings}")
        self._install()
        with self.connect() as c:
            self.assertIsNotNone(c.execute("SELECT to_regclass(%s)", (findings,)).fetchone()[0])

    def test_changed_mail_view_keeps_the_attachments_view_reading_it(self):
        """The attachments view reads the guarded mail view (an attachment is readable only
        where its message is): a changed mail view is made again with the attachments view
        dropped first and made again after, reading the new one."""
        self._install()
        mail, att = f"{self.dst}.emails", f"{self.dst}.attachments"
        with self.connect() as c:
            self.assertIsNotNone(c.execute("SELECT to_regclass(%s)", (att,)).fetchone()[0])
            old = c.execute("SELECT to_regclass(%s)::oid", (mail,)).fetchone()[0]
            c.execute(f"COMMENT ON VIEW {mail} IS NULL")   # its DDL changed: made again
        self._install()
        with self.connect() as c:
            self.assertNotEqual(c.execute("SELECT to_regclass(%s)::oid", (mail,)).fetchone()[0],
                                old)
            self.assertIn(mail, sql_views._refs(c, att))

    def test_a_failed_step_fails_install_and_the_rest_still_run(self):
        """A view whose DROP fails (a view nobody here knows reads it): install still makes
        every other changed view, then fails naming the step (a non-zero exit)."""
        stuck, other = self.guarded()[0], self.guarded()[-1]
        with self.connect() as c:
            c.execute(f"CREATE VIEW public.reads_it AS SELECT * FROM {stuck}")
            for n in (stuck, other):
                c.execute(f"COMMENT ON VIEW {n} IS NULL")
            old = c.execute("SELECT to_regclass(%s)::oid", (other,)).fetchone()[0]
        try:
            with self.assertRaises(sql_views.InstallFailed) as got:
                sql_views.install(self.dsn)
            self.assertIn(stuck, str(got.exception))
            self.assertNotIn(f"{other}:", str(got.exception))
            with self.connect() as c:
                self.assertNotEqual(
                    c.execute("SELECT to_regclass(%s)::oid", (other,)).fetchone()[0], old)
                self.assertIsNone(c.execute(
                    "SELECT obj_description(to_regclass(%s), 'pg_class')", (stuck,)
                ).fetchone()[0], "the failed step's view was changed")
        finally:
            with self.connect() as c:
                c.execute("DROP VIEW public.reads_it")
            self._install()

    def test_a_refresh_whose_view_fails_keeps_the_served_table_and_view(self):
        """No deploy step installs views: a release's changed view reaches the live store
        in its table's refresh swap, the view made inside the swap's transaction. A view
        that fails there rolls the whole swap back (the served table and its view as they
        were, readable) and the build fails loudly (the refresh unit fails: its alarm)."""
        import psycopg
        view = self.guarded()[0]
        name = view.split(".", 1)[1]
        with self.connect() as c:
            table = c.execute("SELECT to_regclass(%s)::oid", (f"{bv.SCHEMA}.{name}",)
                              ).fetchone()[0]
            old = c.execute("SELECT to_regclass(%s)::oid", (view,)).fetchone()[0]
            rows = c.execute(f"SELECT count(*) FROM {bv.SCHEMA}.{name}").fetchone()[0]

        def broken(*_a, **_k):
            raise psycopg.errors.InsufficientPrivilege("view make failed")
        with mock.patch.object(sql_views, "make", side_effect=broken), \
                self.assertRaisesRegex(RuntimeError, "view make failed"):
            bv.build(self.dsn, [name], schema=tbv.SCHEMA, as_of=tbv.AS_OF)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT to_regclass(%s)::oid", (f"{bv.SCHEMA}.{name}",)
                                       ).fetchone()[0], table, "the served table was swapped")
            self.assertEqual(c.execute("SELECT to_regclass(%s)::oid", (view,)).fetchone()[0],
                             old, "the served view was dropped or changed")
            self.assertEqual(sql_views._refs(c, view), [f"{bv.SCHEMA}.{name}"])
            c.execute(f"SELECT count(*) FROM {view}").fetchone()   # still readable
            self.assertEqual(c.execute(f"SELECT count(*) FROM {bv.SCHEMA}.{name}"
                                       ).fetchone()[0], rows)
            self.assertIsNone(c.execute("SELECT to_regclass(%s)", (f"{bv.SCHEMA}.{name}_next",)
                                        ).fetchone()[0], "a _next table left behind")


if __name__ == "__main__":
    unittest.main()
