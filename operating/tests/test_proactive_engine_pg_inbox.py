"""Postgres inbox copy: the Home inbox read over a fake Postgres connection.

Split out of test_proactive_engine.py (module size cap). SYNTHETIC fixtures only.
Evidence label: isolated-fixture.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from operating import proactive_engine as pe
from operating.tests.test_proactive_engine import ALL_MARKINGS, Engine, admit, everyone, seeded


class _FakePostgres:
    """The four reads the inbox's Postgres copy makes, answered from the test's SQLite
    store (``ontology.`` dropped, ``= ANY(%s)`` read as a JSON list); counts its opens."""

    def __init__(self, store: Path) -> None:
        self.store, self.opened = store, 0

    def connect(self, *_a, **_k):
        self.opened += 1
        conn = sqlite3.connect(self.store)

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                conn.close()

            def execute(self, sql, params=()):
                sql = sql.replace("ontology.", "").replace(
                    "= ANY(%s)", "IN (SELECT value FROM json_each(?))")
                return conn.execute(sql, tuple(json.dumps(p) if isinstance(p, list) else p
                                               for p in params))
        return Conn()


class PostgresInboxCopyTest(Engine):
    """Postgres the only store: an inbox's copy of the store rows its permission SQL joins
    is made once per (inbox as it stands, Postgres change stamp) and shared by every
    read-only open, never by a write; each reader still sees exactly what its own grant
    admits, and a re-marked record is withheld as soon as Postgres says the data moved."""

    def setUp(self) -> None:
        super().setUp()
        from operating import proactive_engine_inbox as pi
        from operating.pg import ontology_reader
        self.addCleanup(lambda: [held[1].close() for held in pi._st_kept.values()])
        self.addCleanup(pi._st_kept.clear)
        self.stamp = ["sha256:gen@1"]
        self.pg = _FakePostgres(self.store)
        fake = type(sys)("psycopg")
        fake.connect = self.pg.connect
        for patch in (mock.patch.dict(sys.modules, {"psycopg": fake}),
                      mock.patch.object(ontology_reader, "whole", lambda *a, **k: None),
                      mock.patch.object(ontology_reader, "dsn", lambda *a, **k: "fake"),
                      mock.patch.object(ontology_reader, "change_stamp",
                                        lambda *a, **k: self.stamp[0])):
            patch.start()
            self.addCleanup(patch.stop)

    def pg_read(self, **options) -> dict:
        from operating.pg import ontology_reader
        with mock.patch.object(ontology_reader, "pg_only", lambda: True):
            return self.read(**options)

    def test_one_copy_serves_every_reader_as_its_own_grant_admits(self):
        world, ids = seeded()
        self.run_engine(world, ids["coverage"])
        readers = {"all": {}, "api_b_only": {"may_read": lambda key: key == "api:b"},
                   "no_finance": {"held": ALL_MARKINGS - {"finance"}}}
        expected = {name: sorted(g["id"] for g in self.read(**r)["gaps"])
                    for name, r in readers.items()}
        self.assertTrue(expected["all"])
        for _ in range(2):
            for name, r in readers.items():
                with self.subTest(reader=name):
                    got = sorted(g["id"] for g in self.pg_read(**r)["gaps"])
                    self.assertEqual(got, expected[name])
        opened = self.pg.opened
        self.assertEqual(opened, 1)   # one copy for six opens by three readers

        # the subject re-marked beyond a reader: once Postgres says the data moved, the
        # copy is made again and that reader no longer sees the gap; one holding it does
        subject = ids["unconfirmed"]
        conn = sqlite3.connect(self.store)
        conn.execute("UPDATE object_versions SET markings = '[\"finance\"]' "
                     "WHERE object_id = ?", (subject,))
        conn.commit()
        conn.close()
        self.stamp[0] = "sha256:gen@2"
        def subjects(shown):
            return {g["object_id"] for g in shown["gaps"]}
        self.assertNotIn(subject, subjects(self.pg_read(held=ALL_MARKINGS - {"finance"})))
        self.assertIn(subject, subjects(self.pg_read()))
        self.assertEqual(self.pg.opened, opened + 1)

        # the inbox written (a run): a new copy, as its records may differ
        self.run_engine(world, ids["coverage"],
                        at=datetime(2026, 10, 19, 9, tzinfo=timezone.utc))
        self.pg_read()
        self.assertEqual(self.pg.opened, opened + 2)

    def test_a_write_or_an_unstamped_open_never_shares_the_copy(self):
        world, ids = seeded()
        self.run_engine(world, ids["coverage"])
        from operating import proactive_engine_inbox as pi
        from operating.pg import ontology_reader
        self.pg_read()
        opened = self.pg.opened
        [gap] = [g for g in self.read()["gaps"] if g["object_id"] == ids["unconfirmed"]][:1]
        with mock.patch.object(ontology_reader, "pg_only", lambda: True), \
                mock.patch.object(pi, "_st_copy", side_effect=AssertionError("shared")):
            pe.acknowledge(self.inbox, self.store, gap["id"], reader="plx:ops",
                           may_read=everyone, held=ALL_MARKINGS, decide=admit)
        self.assertEqual(self.pg.opened, opened + 1)   # its own copy
        self.stamp[0] = None   # Postgres cannot say what it holds: nothing is shared
        self.pg_read()
        self.pg_read()
        self.assertEqual(self.pg.opened, opened + 3)



if __name__ == "__main__":
    unittest.main()
