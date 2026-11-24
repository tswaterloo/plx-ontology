"""The mail and file search indexes (Ask T3/T4): made concurrently on the tables in place, each
only after the disk budget grants its peak; a refusal, a missing column and an invalid leftover
are said, never silent; a second run makes nothing; an equality on the column reaches the
index."""
from __future__ import annotations

import unittest
from unittest import mock

from operating import disk_budget
from operating.pg import number_indexes as ni
from operating.tests import test_business_views as tbv

SCHEMA = "idx_t"


class CommsIndexes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.tests import pg_store_mode
        cls.store = tbv.build()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            c.execute(f"CREATE SCHEMA {SCHEMA}")
            c.execute(f"CREATE TABLE {SCHEMA}.emails AS SELECT 's' || (g % 500) AS subject, "
                      f"'d' || (g % 50) || '.com' AS sender_domain, 'm' || (g % 10) AS mailbox "
                      f"FROM generate_series(1, 20000) g")
            c.execute(f"CREATE TABLE {SCHEMA}.attachments AS SELECT 'f' || g || '.pdf' AS name "
                      f"FROM generate_series(1, 20000) g")
            c.execute(f"ANALYZE {SCHEMA}.emails")
            c.execute(f"ANALYZE {SCHEMA}.attachments")

    def conn(self):
        import psycopg
        c = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(c.close)
        return c

    def names(self, c):
        return {r[0] for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname = %s",
                                        (SCHEMA,)).fetchall()}

    def test_made_once_valid_and_reached_by_an_equality(self):
        c = self.conn()
        with mock.patch.object(disk_budget, "reserve") as res, \
                mock.patch.object(disk_budget, "release") as rel:
            got = ni.ensure_comms(c, SCHEMA)
            want = {"emails_subject_pattern", "emails_sender_domain_pattern",
                    "emails_mailbox_pattern", "attachments_name_pattern"}
            self.assertEqual(set(got["made"]), want)
            self.assertEqual(res.call_count, 4)
            self.assertEqual(rel.call_count, 4)
            self.assertEqual(self.names(c), want)
            self.assertEqual(ni.ensure_comms(c, SCHEMA)["made"], [])   # a second run: nothing
        c.execute("SET enable_seqscan = off")
        plan = "\n".join(r[0] for r in c.execute(
            f"EXPLAIN SELECT 1 FROM {SCHEMA}.emails WHERE sender_domain = 'd7.com'").fetchall())
        self.assertIn("emails_sender_domain_pattern", plan)
        c.execute("RESET enable_seqscan")
        # an invalid leftover (a failed concurrent build) is dropped and made again
        c.execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
                  f"'{SCHEMA}.attachments_name_pattern'::regclass")
        with mock.patch.object(disk_budget, "reserve"), mock.patch.object(disk_budget, "release"):
            got = ni.ensure_comms(c, SCHEMA)
        self.assertEqual((got["rebuilt"], got["made"]), (["attachments_name_pattern"],
                                                         ["attachments_name_pattern"]))
        self.assertTrue(c.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = "
                                  f"'{SCHEMA}.attachments_name_pattern'::regclass").fetchone()[0])

    def test_a_refused_budget_and_a_missing_column_are_said_not_skipped(self):
        c = self.conn()
        c.execute(f"DROP INDEX IF EXISTS {SCHEMA}.emails_subject_pattern")
        refused = disk_budget.Refused("index:emails_subject_pattern", 3.0, 1.0)
        with mock.patch.object(disk_budget, "reserve", side_effect=refused):
            got = ni.ensure_comms(c, SCHEMA, (("emails", "subject"), ("emails", "nope")))
        self.assertEqual(got["made"], [])
        self.assertIn("disk budget refused", got["refused"]["emails_subject_pattern"])
        self.assertIn("no column nope", got["refused"]["emails_nope_pattern"])
        self.assertNotIn("emails_subject_pattern", self.names(c))

    def test_a_trigram_index_is_not_reached_through_a_security_barrier_view(self):
        """Measured for Ask's R9 (substring search on subject / file name): ILIKE is not a
        leakproof operator, so a guarded (security_barrier) view runs it AFTER its own rules
        and the planner cannot use a trigram index below the view: it reads every row. Only
        =, prefix and range reach an index there (the btree indexes above)."""
        c = self.conn()
        c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        self.addCleanup(c.execute, f"DROP INDEX IF EXISTS {SCHEMA}.emails_trgm")
        c.execute(f"CREATE INDEX IF NOT EXISTS emails_trgm ON {SCHEMA}.emails USING gin "
                  "(subject gin_trgm_ops)")
        c.execute(f"CREATE OR REPLACE VIEW {SCHEMA}.emails_guarded WITH (security_barrier) AS "
                  f"SELECT * FROM {SCHEMA}.emails WHERE mailbox <> 'none'")
        c.execute("SET enable_seqscan = off")
        direct = "\n".join(r[0] for r in c.execute(
            f"EXPLAIN SELECT 1 FROM {SCHEMA}.emails WHERE subject ILIKE '%s12%'").fetchall())
        guarded = "\n".join(r[0] for r in c.execute(
            f"EXPLAIN SELECT 1 FROM {SCHEMA}.emails_guarded WHERE subject ILIKE '%s12%'").fetchall())
        c.execute("RESET enable_seqscan")
        self.assertIn("emails_trgm", direct)           # the index works on the bare table
        self.assertNotIn("emails_trgm", guarded)       # and not through the guarded view

    def test_the_unit_runs_the_indexes_outside_the_refresh_and_prints_what_it_did(self):
        import contextlib
        import io
        import json
        out = io.StringIO()
        with mock.patch.object(disk_budget, "reserve", side_effect=disk_budget.Refused(
                "index:x", 9.0, 1.0)), contextlib.redirect_stdout(out):
            rc = ni.main(["comms", "--dsn", self.dsn])
        self.assertEqual(rc, 0)
        got = json.loads(out.getvalue())
        self.assertEqual(set(got), {"made", "refused", "rebuilt"})
        # the refresh no longer makes them (a long statement is not a hang for its watchdog)
        from operating.pg import business_views as bv
        import inspect
        self.assertNotIn("ensure_comms", inspect.getsource(bv))
        unit = (__import__("pathlib").Path(__file__).resolve().parents[2] / "deploy" / "systemd"
                / "plx-comms-indexes.service").read_text()
        self.assertNotIn("WatchdogSec", "\n".join(l for l in unit.splitlines() if not l.startswith("#")))
        views = (__import__("pathlib").Path(__file__).resolve().parents[2] / "deploy" / "systemd"
                 / "plx-business-views.service").read_text()
        self.assertIn("OnSuccess=plx-comms-indexes.service", views)

    def test_a_table_not_built_yet_is_left_for_the_next_refresh(self):
        c = self.conn()
        got = ni.ensure_comms(c, "idx_none", (("emails", "subject"),))
        self.assertEqual(got, {"made": [], "refused": {}, "rebuilt": []})


if __name__ == "__main__":
    unittest.main()
