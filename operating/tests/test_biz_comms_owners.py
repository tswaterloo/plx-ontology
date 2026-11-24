"""The mailbox owners' addresses (``operating.pg.mail_owners``) on a host that cannot read
the mail capture: a whole ``emails`` build there (a Spark copy of the database, 11-23:
``owner_role`` null on 3.27M of 3.27M mails) decides every owner's place from the addresses
an earlier run kept, as the capture would; none kept and ``PLX_BIZ_OWNERS_REQUIRED=1``: the
build stops and the served table stands. On ``test_biz_comms``' fixture (its own tests are
not run again here). Evidence label: isolated-fixture."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from operating.pg import business_comms as bc
from operating.pg import business_views as bv
from operating.tests import test_biz_comms as tbc

setUpModule, tearDownModule = tbc.setUpModule, tbc.tearDownModule


class KeptOwners(tbc.EmailsThroughTheDoor):
    def test_a_build_without_the_capture_takes_the_kept_owners_or_stops(self):
        import psycopg
        before, none = self.flags(), {"PLX_STATE_DIR": str(self.root / "none")}
        q = f"SELECT * FROM {bv.SCHEMA}.emails"

        def roles(got):
            return {k: v[1:] for k, v in got.items()}   # owner_role, owner_replied_at
        self.assertIn("to", {v[1] for v in before.values()})
        try:
            with mock.patch.dict(os.environ, none):
                bv.build(self.dsn, ["emails"], schema=self.compiled)
            self.assertEqual(roles(self.flags()), roles(before))
            with psycopg.connect(self.dsn, autocommit=True) as pg:
                self.assertEqual(pg.execute(
                    f"SELECT notes->'marks'->'flags'->>'owners' FROM {bv.SCHEMA}.state "
                    "WHERE view = 'emails'").fetchone()[0], "stored")
                for schema in {bv.SCHEMA, bv.SERVED_SCHEMA}:
                    pg.execute(f"DROP TABLE IF EXISTS {schema}.{bc.OWNERS_TABLE}")
                served = sorted(map(repr, pg.execute(q).fetchall()))
                with mock.patch.dict(os.environ, {**none, "PLX_BIZ_OWNERS_REQUIRED": "1"}), \
                        self.assertRaisesRegex(RuntimeError, "views not built"):
                    bv.build(self.dsn, ["emails"], schema=self.compiled)
                self.assertEqual(sorted(map(repr, pg.execute(q).fetchall())), served)
            with mock.patch.dict(os.environ, none):   # not required: unknown, never guessed
                bv.build(self.dsn, ["emails"], schema=self.compiled)
            self.assertEqual({v[1] for v in self.flags().values()}, {None})
        finally:
            bv.build(self.dsn, ["emails"], schema=self.compiled)   # the capture: kept again
        self.assertEqual(self.flags(), before)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            self.assertTrue(pg.execute(
                f"SELECT count(*) FROM {bv.SCHEMA}.{bc.OWNERS_TABLE}").fetchone()[0])


    def test_no_reader_role_can_read_the_kept_addresses(self):
        """0058 makes biz.mailbox_owners with every reader grant revoked (the schema's
        grants would give them SELECT); a build still keeps the addresses in it."""
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        before = self.flags()
        table = f"{bv.SERVED_SCHEMA}.{bc.OWNERS_TABLE}"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for role in ("plx_api", "plx_eval"):
                pg.execute(f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN; EXCEPTION WHEN "
                           "duplicate_object THEN NULL; END $$")
            pg.execute(f"DROP TABLE IF EXISTS {table}")
            pg.execute((MIGRATIONS_DIR / "0058_mailbox_owners.sql").read_text())
            pg.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {bv.SERVED_SCHEMA} TO plx_api")
            pg.execute((MIGRATIONS_DIR / "0058_mailbox_owners.sql").read_text())   # re-run
        bv.build(self.dsn, ["emails"], schema=self.compiled)
        self.assertEqual(self.flags(), before)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            if bv.SCHEMA == bv.SERVED_SCHEMA:
                self.assertTrue(pg.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for role in ("plx_api", "plx_eval"):
                self.assertFalse(pg.execute("SELECT has_table_privilege(%s, %s, 'SELECT')",
                                            (role, table)).fetchone()[0], role)


def load_tests(loader, tests, pattern):   # only this module's own tests, not the fixture's
    return unittest.TestSuite([KeptOwners(n) for n in (
        "test_a_build_without_the_capture_takes_the_kept_owners_or_stops",
        "test_no_reader_role_can_read_the_kept_addresses")])


if __name__ == "__main__":
    unittest.main()
