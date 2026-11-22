"""The off-box old-mail store's work list and runner (``operating.embed.oldmail``): only mail
missing from search is planned, newest first; a unit reads its own mail and attachments and
nothing else, with the people exclusion; a stopped or foreign-code run records nothing."""
from __future__ import annotations

import contextlib
import io
import json
import unittest

from operating.chunking.projection import Counts
from operating.embed import oldmail
from tests import test_chunks_embed as tce

exclusion = tce.exclusion


class OldMailTests(unittest.TestCase):
    # the Postgres ontology fixture of the history parity tests (borrowed, not inherited:
    # their own tests are not run again here)
    setUp = tce.PgSourceParityTests.setUp
    email = tce.PgSourceParityTests.email
    build = tce.PgSourceParityTests.build
    copy_to_pg = tce.PgSourceParityTests.copy_to_pg

    def store(self):
        import psycopg
        self.build()
        dsn = self.copy_to_pg()
        box = "12345678-aaaa-bbbb-cccc-0000000000ef"
        mails = {"Email:jun": "2026-06-04", "Email:nov": "2026-11-03",
                 "Email:may": "2026-05-04", "Email:ex": "2026-06-05"}
        with psycopg.connect(dsn) as pg:
            pg.execute("CREATE SCHEMA biz; CREATE SCHEMA search")
            pg.execute("CREATE TABLE biz.emails (handle text, sent_at timestamptz, "
                       "received_at timestamptz)")
            pg.execute("CREATE TABLE search.chunks (source_handle text, kind text)")
            for oid, sent in mails.items():
                pg.execute("INSERT INTO biz.emails VALUES (%s, %s, NULL)",
                           (f"graph-mail://{box}/{oid}@1", sent))
            # May is searchable already (its mail chunk is in search.chunks)
            pg.execute("INSERT INTO search.chunks VALUES (%s, 'mail')",
                       (f"graph-mail://{box}/Email:may@1",))
        return dsn

    def units(self, dsn):
        import psycopg
        with psycopg.connect(dsn) as pg:
            plan = {h.split("/")[-1]: u for h, u in pg.execute(
                "SELECT handle, unit FROM search.oldmail_plan")}
            objects = {}
            for unit, oid in pg.execute("SELECT unit, object_id FROM "
                                        "search.oldmail_plan_objects"):
                objects.setdefault(unit, set()).add(oid)
        return plan, objects

    def test_only_mail_missing_from_search_is_planned_newest_first(self):
        dsn = self.store()
        got = oldmail.plan(dsn, unit_size=2)
        self.assertEqual(got["planned"]["mails"], 3, got)
        self.assertEqual(got["planned"]["units"], 2, got)
        plan, objects = self.units(dsn)
        self.assertEqual(plan, {"Email:nov@1": 0, "Email:ex@1": 0, "Email:jun@1": 1})
        # each unit holds its mails' own objects and their attachments', no other mail's
        self.assertEqual(objects[0], {"Email:nov", "Attachment:A3", "Email:ex", "Attachment:A2"})
        self.assertEqual(objects[1], {"Email:jun", "Attachment:A1"})
        self.assertEqual(oldmail.plan(dsn, unit_size=2), {"refused": "plan_exists"})
        with self.assertRaises(ValueError):
            oldmail.plan(dsn, unit_size=0)

    def test_a_unit_chunks_its_own_mail_and_attachments_with_the_exclusion(self):
        from operating.chunking.pg_source import PG_PROJECTION
        from operating.embed.shards import produce
        dsn = self.store()
        oldmail.plan(dsn, unit_size=2)
        _plan, objects = self.units(dsn)
        for unit, want in ((0, {"Email:nov", "Attachment:A3"}),
                           (1, {"Email:jun", "Attachment:A1"})):
            counts = Counts()
            got = list(produce(self.path.parent, PG_PROJECTION, exclusion(), counts,
                               list(oldmail.KINDS), None, objects=frozenset(objects[unit]),
                               dsn=dsn))
            # the excluded sender's message and attachment stay withheld (unit 0)
            self.assertEqual({c.object_id for c in got}, want, dict(counts.skipped))

    def run_quiet(self, *args, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = oldmail.run(*args, **kw)
        return rc, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_a_run_without_a_plan_or_by_other_code_or_under_the_floor_records_nothing(self):
        import psycopg
        dsn = self.store()
        state = str(self.path.parent)
        rc, lines = self.run_quiet(dsn, state, 1, "c1", 0.0)
        self.assertEqual((rc, lines[-1]), (2, {"refused": "no_plan"}))
        oldmail.plan(dsn, unit_size=2)
        with psycopg.connect(dsn) as pg:
            pg.execute(oldmail.UNITS_DDL)
            pg.execute("INSERT INTO search.oldmail_units (unit, mails, objects, documents, "
                       "chunks, inserted, seconds, code) VALUES (1, 1, 2, 2, 2, 2, 1, 'c0')")
        rc, lines = self.run_quiet(dsn, state, 1, "c1", 0.0)
        self.assertEqual(rc, 2)
        self.assertEqual(lines[-1]["refused"], "store_built_by_other_code")
        # same code: the done unit is skipped; under the disk floor nothing is begun
        rc, lines = self.run_quiet(dsn, state, 1, "c0", 1e12)
        self.assertEqual(rc, 3)
        self.assertEqual(lines[0], {"pending_units": 1, "workers": 1})
        self.assertEqual(lines[-1], {"stopped": "disk_below_min_free"})
        with psycopg.connect(dsn) as pg:
            self.assertEqual([r[0] for r in pg.execute(
                "SELECT unit FROM search.oldmail_units ORDER BY unit")], [1])


class ChunkerCodeTests(unittest.TestCase):

    def test_the_units_are_pinned_to_the_chunk_code_not_to_the_runner(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("operating/chunking/projection.py", "operating/chunking/rules.json",
                        "operating/chunking/tests/test_x.py", "operating/embed/shards.py",
                        "operating/embed/store.py", "operating/embed/oldmail.py"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(rel)
            first = oldmail.chunker_code(root)
            for rel in ("operating/embed/oldmail.py", "operating/chunking/tests/test_x.py"):
                (root / rel).write_text("changed")
                self.assertEqual(oldmail.chunker_code(root), first, rel)
            for rel in ("operating/chunking/projection.py", "operating/chunking/rules.json",
                        "operating/embed/store.py"):
                (root / rel).write_text("changed " + rel)
                self.assertNotEqual(oldmail.chunker_code(root), first, rel)
                first = oldmail.chunker_code(root)


if __name__ == "__main__":
    unittest.main()
