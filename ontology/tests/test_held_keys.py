"""A step build's held keys (``ontology.held_keys``): without the tripwire nothing in the
guarded input reads a held table (no view, trigger or index outside the held tables'
own names one), so the build copies none and reads the same rows; with the tripwire the
views read them and the build copies them as before. The step_migrate code the claim
rests on is pinned."""

from __future__ import annotations

import os
import re
import sqlite3
import unittest
from unittest import mock

from ontology import held_keys
from ontology import step_migrate as sm
from tests.test_step_input_guard import GuardTests

HELD_TABLES = sorted({t for _v, t, _c, _k in sm.HELD})


def _readers(conn: sqlite3.Connection) -> list[str]:
    """Schema entries outside the held tables themselves that name a held table."""
    out = []
    for kind, name, tbl, sql in conn.execute("SELECT type, name, tbl_name, sql FROM "
                                             "sqlite_master WHERE sql IS NOT NULL"):
        if tbl in HELD_TABLES:
            continue
        if any(re.search(rf"\b{t}\b", sql) for t in HELD_TABLES):
            out.append(f"{kind} {name}")
    return out


class _Fixture(GuardTests):
    def runTest(self):  # pragma: no cover - only its guarded() is used
        pass


class HeldKeysTests(unittest.TestCase):

    def test_the_guard_code_the_claim_rests_on_is_pinned(self):
        self.assertEqual(held_keys.source_digest(), held_keys.GUARD_DIGEST)

    def test_without_the_tripwire_nothing_reads_a_held_key(self):
        f = _Fixture()
        try:
            self.assertEqual(_readers(f.guarded(tripwire=False)), [])
            # the test sees readers where they exist: the tripwire's views
            self.assertTrue(_readers(f.guarded(tripwire=True)))
        finally:
            f.doCleanups()

    def test_the_copy_is_skipped_only_without_the_tripwire(self):
        conn = sqlite3.connect(":memory:")
        cur = mock.Mock(side_effect=AssertionError)
        cur.execute.side_effect = AssertionError("Postgres read without the tripwire")
        with mock.patch.dict(os.environ, {sm.TRIPWIRE_ENV: "0"}), held_keys.skipped():
            sm._copy_held_keys(cur, conn, [], [], [])
        for t in HELD_TABLES:   # made, empty: what guard_inputs adds to and indexes
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 0)
        self.assertIs(sm._copy_held_keys, held_keys._ORIGINAL)   # restored on exit
        with mock.patch.dict(os.environ, {sm.TRIPWIRE_ENV: "1"}), held_keys.skipped(), \
                mock.patch.object(held_keys, "_ORIGINAL") as original:
            sm._copy_held_keys(cur, conn, ["T"], ["T:"], [])
        original.assert_called_once_with(cur, conn, ["T"], ["T:"], [])

    def test_without_the_tripwire_the_guard_holds_no_key_and_reads_alike(self):
        f = _Fixture()
        try:
            plain = f.guarded(tripwire=False)
            with held_keys.skipped():
                lean = f.guarded(tripwire=False)
                trip = f.guarded(tripwire=True)
            # the fixture's undeclared rows are links: the plain guard holds their keys
            self.assertGreater(plain.execute("SELECT count(*) FROM held_links").fetchone()[0],
                               0)
            for t in HELD_TABLES:
                self.assertEqual(lean.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 0)
            self.assertGreater(trip.execute("SELECT count(*) FROM held_links").fetchone()[0], 0)
            for q in ("SELECT * FROM objects ORDER BY 1", "SELECT * FROM links ORDER BY 1",
                      "SELECT * FROM identifiers ORDER BY 1",
                      "SELECT * FROM object_values ORDER BY 1, 2"):
                self.assertEqual(lean.execute(q).fetchall(), plain.execute(q).fetchall(), q)
            with self.assertRaisesRegex(sqlite3.OperationalError, "malformed JSON"):
                trip.execute("SELECT * FROM links WHERE link_type = 'Other.same'").fetchall()
        finally:
            f.doCleanups()


if __name__ == "__main__":
    unittest.main()
