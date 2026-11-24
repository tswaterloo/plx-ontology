"""The Insights fast copy on the API's own role, and a fallback that is said (review of
f22debbf3a/6f727a2c07, 11-23).

* As plx_api (not a superuser) the findings tile counts what the superuser counts: the role
  reads the findings feed (``comms_findings.FEED``) and the comms tables, never
  ``assist.readings`` itself.
* No feed yet: the tile is not served from the copy, and the copy says why.
* A built-in the copy cannot serve is read live, and the tile's result carries
  ``served = {status: failed, reason}`` (C1 status words), for the person and the model.

Evidence label: isolated-fixture."""
from __future__ import annotations

import unittest
from unittest import mock

from operator_api import insights as ins
from operator_api.tests import test_insights as ti

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


class FastCopyAsTheApiRole(unittest.TestCase):
    setUpClass = classmethod(ti.FastCopies.setUpClass.__func__)
    tearDownClass = classmethod(ti.FastCopies.tearDownClass.__func__)
    setUp = ti.FastCopies.setUp
    mail = ti.FastCopies.mail
    maker = ti.FastCopies.maker
    counts = ti.FastCopies.counts
    findings_fixture = ti.FastCopies.findings_fixture

    def api_role(self):
        """The API's role in this cluster (made NOLOGIN when missing; never dropped: a role
        is the cluster's), granted this database's comms tables as on the host, and the
        findings feed through its own grant (``comms_findings.feed_ddl``)."""
        from operating.pg import comms_findings as cf
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            if not pg.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                              (cf.FEED_ROLE,)).fetchone():
                try:
                    pg.execute(f"CREATE ROLE {cf.FEED_ROLE} NOLOGIN")
                except psycopg.errors.DuplicateObject:
                    pass   # another test made it meanwhile
            for sch in (self.schema, "ontology"):
                pg.execute(f"GRANT USAGE ON SCHEMA {sch} TO {cf.FEED_ROLE}")
                pg.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO {cf.FEED_ROLE}")
            for stmt in cf.feed_ddl():
                pg.execute(stmt)
        return cf.FEED_ROLE

    def test_the_api_role_counts_the_findings_through_the_feed_only(self):
        from operator_api import insights_fast as f
        from operating.pg import comms_findings as cf
        spec = self.findings_fixture()
        self.assertEqual(self.counts(self.maker([spec]), spec), {"complaint": "1"})
        api = self.api_role()
        self.assertEqual(self.counts(self.maker([spec], role=api), spec), {"complaint": "1"})
        with psycopg.connect(self.dsn) as pg, self.assertRaises(
                psycopg.errors.InsufficientPrivilege):
            pg.execute(f"SET ROLE {api}")
            pg.execute("SELECT count(*) FROM assist.readings")
        # no feed yet (the migration not applied): not answered from the copy, and said
        with psycopg.connect(self.dsn) as pg:
            pg.execute(f"DROP VIEW {cf.FEED}")
        with self.assertLogs("operator_api.insights_fast", "WARNING") as said, \
                self.assertRaises(f.Unserved):
            self.maker([spec], role=api).get(spec)
        self.assertIn("findings feed not built", " ".join(said.output))


class FastFallbackIsSaid(unittest.TestCase):
    def test_the_live_answer_and_the_tile_carry_the_fast_copys_failure(self):
        from operator_api import insights_fast as f
        spec = ti.fast_spec(person="", scope="full", sid="fb")

        class Maker:
            def get(self, _spec):
                raise f.Unserved("InsufficientPrivilege")
        b = ti.Backend(None, everyone=True)
        live = {"got": {"columns": [], "rows": []}, "read_at": "2026-11-23T20:00:00+00:00"}
        with mock.patch.object(ins, "declared", return_value=[spec]), \
                mock.patch.object(ins, "ref", side_effect=lambda s: s.id), \
                mock.patch.object(f, "serves", return_value=True), \
                mock.patch.object(ins, "read_now", return_value=live), \
                mock.patch.object(ti.Backend, "_insight_fast", return_value=Maker(), create=True), \
                self.assertLogs("operator_api.insights", "WARNING") as said:
            out = b._insight_read_as(ti.reader("owner"), "fb")
        self.assertEqual(out["fast"], {"status": "failed", "reason": "InsufficientPrivilege"})
        self.assertEqual(out["got"], live["got"])
        self.assertIn("fast copy not served", " ".join(said.output))
        shown = ins.result(b, ti.reader("owner"), spec, b.scope, out)
        self.assertEqual((shown.served.status, shown.served.reason),
                         ("failed", "InsufficientPrivilege"))
        self.assertIsNone(ins.result(b, ti.reader("owner"), spec, b.scope, live).served)


if __name__ == "__main__":
    unittest.main()
