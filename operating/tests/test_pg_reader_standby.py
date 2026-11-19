"""A reader on a session that cannot make temporary tables (a hot standby; stand-in: every
transaction read-only and ``PLX_PG_READER_STATELESS``): its gate state is held in session
settings (``operating.pg.reader_settings``, migration 0049) and it reads the same rows and
refusals as a reader with temporary tables. The item-audience suite (a staff reader in the
roster, one outside it, an excluded one) and the people-exclusion parity suite (a full
reader) run again on such a reader, unchanged. A long key list on such a reader is read from
the shared set of its SHA-256 (migration 0050) once one is kept, else bound as an array."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

from operating.tests import pg_testdb
from operating.tests import test_people_excluded_parity as parity
from operating.tests import test_pg_sqlite_compat as compat
from operating.tests import test_pg_item_audiences as audiences

SETTINGS = (Path(__file__).resolve().parents[1] / "pg" / "migrations"
            / "0049_reader_session_settings.sql")
KEY_SETS = SETTINGS.with_name("0050_reader_key_sets.sql")
READ_ONLY = "-c default_transaction_read_only=on"


def _settings_migration(dsn: str) -> None:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(SETTINGS.read_text(encoding="utf-8"))


def standby_reader(dsn: str):
    """A ``PgCompatConnection`` on a session where every transaction is read-only, asked
    to hold its state in settings."""
    from psycopg.conninfo import make_conninfo
    from operating.pg.sqlite_compat import PgCompatConnection
    with mock.patch.dict(os.environ, {"PLX_PG_READER_STATELESS": "1"}):
        return PgCompatConnection(make_conninfo(dsn, options=READ_ONLY))


class StandbyItemAudienceTests(audiences.ItemAudienceTests):
    """Every item-audience test, on a stateless reader on a read-only session."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _settings_migration(cls.dsn)

    def setUp(self):
        super().setUp()
        self.pg = standby_reader(self.dsn)

    def test_the_reader_holds_no_temporary_table_and_writes_nothing(self):
        self.record()
        read, asked = self.read(self.ana)
        self.assertTrue(read and asked)
        self.assertTrue(self.pg.stateless)
        cur = self.pg._pg.cursor()
        cur.execute("SELECT count(*) FROM pg_class WHERE relpersistence = 't'")
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SHOW transaction_read_only")
        self.assertEqual(cur.fetchone()[0], "on")
        # a write is refused on this session: the stand-in is a real read-only one
        import psycopg
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            cur.execute("CREATE TEMP TABLE sq_x (a int)")


class StandbyFullReaderParityTests(parity.PeopleExcludedParityTests):
    """Every people-exclusion parity test with the SQL gate read as ``sq_item_gate_s`` on
    a read-only session (a call it leaves to Python raises, as ``sq_call`` does with no
    answers table in the base suite)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _settings_migration(cls.dsn)

    def sql_answers(self, handles, exclusion=None) -> dict[str, bool]:
        import psycopg
        from psycopg.conninfo import make_conninfo
        from operating.pg import people_excluded, reader_settings
        key = people_excluded.reader_key(("people-excluded",
                                          exclusion or self.set.exclusion))
        with psycopg.connect(make_conninfo(self.dsn, options=READ_ONLY)) as pg:
            reader_settings.hold_gate(pg.cursor(), {}, "g", (key,), ())
            got = {h: pg.execute("SELECT ontology.sq_item_gate_s('g', %s)", (h,)).fetchone()[0]
                   for h in handles}
            if pg.execute("SELECT current_setting('plx_sq.pn', true)").fetchone()[0] not in (
                    None, "", "0"):
                raise psycopg.Error("left to the Python gate")
            return got


class SharedKeySetTests(unittest.TestCase):
    """A stateless reader's long ``IN`` key list: read from the shared set its SHA-256
    names (``ontology.reader_key_sets``) once it is kept whole, the same rows as the array
    and as a temporary-table reader; never for ``NOT IN``; never a set of another size."""

    QUERY = "SELECT count(*) FROM objects WHERE audience_key IN (SELECT value FROM json_each(:aud))"

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in (*compat.MIGRATIONS, SETTINGS, KEY_SETS):
                pg.execute(migration.read_text(encoding="utf-8"))
            with pg.cursor().copy("COPY ontology.object_versions (ver, object_id, l1_handle, "
                                  "digest, lineage, markings, recorded_at, superseded_at) "
                                  "FROM STDIN") as copy:
                for n, row in enumerate(compat.VERSIONS, 1):
                    copy.write_row((n, *row))
            # objects under kept keys, keys of no list and a key every list holds
            pg.execute("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                       "native_key, audience_key, first_recorded_at) SELECT 'o' || i, 't', "
                       "'b', i::text, CASE WHEN i % 3 = 0 THEN 'aud:other:' || i WHEN i % 3 = 1 "
                       "THEN 'aud:' || chr(97 + i % 4) || ':' || lpad((i % 900)::text, 5, '0') "
                       "ELSE 'A:b:1' END, '2026-01-01' FROM generate_series(1, 3000) i")
            pg.execute("ANALYZE ontology.objects")

    def setUp(self):
        from operating.pg import reader_settings
        import psycopg
        reader_settings._asked.clear()
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM ontology.reader_key_set_heads")
        self.sent: list[tuple[str, object]] = []

    def reader(self, *, stateless: bool = True):
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = standby_reader(self.dsn) if stateless else PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        run = pg._run

        def kept_run(cur, text, params):
            self.sent.append((text, params))
            return run(cur, text, params)
        pg._run = kept_run
        return pg

    def keys(self, tag: str = "a") -> str:
        handles = [v[0] for v in compat.VERSIONS]
        return json.dumps([f"aud:{tag}:{n:05d}" for n in range(900)] + handles)

    def held(self) -> dict[str, list[str]]:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            rows = pg.execute("SELECT digest, array_agg(key ORDER BY key) FROM "
                              "ontology.reader_key_sets GROUP BY digest").fetchall()
        return dict(rows)

    def test_a_long_list_is_kept_once_and_read_from_the_shared_set(self):
        from operating.pg import reader_settings
        aud = self.keys()
        full = self.reader(stateless=False)
        want = full.execute(self.QUERY, {"aud": aud}).fetchone()[0]
        self.assertGreater(want, 1000)   # a's keys and the handle every list holds
        pg = self.reader()
        self.assertTrue(pg.stateless and pg.shared_sets)
        self.assertEqual(pg.execute(self.QUERY, {"aud": aud}).fetchone()[0], want)
        self.assertIn("ontology.reader_key_sets", self.sent[-1][0])
        # the set holds exactly the list's members, under the list's SHA-256
        self.assertEqual(self.held(), {reader_settings.digest(aud): sorted(set(json.loads(aud)))})
        self.assertEqual(pg.execute(self.QUERY, {"aud": aud}).fetchone()[0], want)
        self.assertIn("ontology.reader_key_sets", self.sent[-1][0])

    def test_a_standby_reads_an_array_until_the_primary_keeps_the_set(self):
        aud = self.keys("b")
        want = self.reader(stateless=False).execute(self.QUERY, {"aud": aud}).fetchone()[0]
        pg = self.reader()
        pg._recovery = True   # a standby: the set is asked of the primary named in the env
        with mock.patch.dict(os.environ, {"PLX_PG_KEYSET_DSN": ""}):
            self.assertEqual(pg.execute(self.QUERY, {"aud": aud}).fetchone()[0], want)
        self.assertNotIn("reader_key_sets", self.sent[-1][0])
        self.assertEqual(self.held(), {})
        from operating.pg import reader_settings
        reader_settings._asked.clear()
        with mock.patch.dict(os.environ, {"PLX_PG_KEYSET_DSN": self.dsn}):
            self.assertEqual(pg.execute(self.QUERY, {"aud": aud}).fetchone()[0], want)
            self.assertNotIn("reader_key_sets", self.sent[-1][0])   # asked, not yet read
            self.assertEqual(pg.execute(self.QUERY, {"aud": aud}).fetchone()[0], want)
        self.assertIn("ontology.reader_key_sets", self.sent[-1][0])

    def test_not_in_and_a_set_of_another_size_stay_arrays(self):
        import psycopg
        from operating.pg import reader_settings
        aud = self.keys("c")
        pg = self.reader()
        want = self.reader(stateless=False).execute(
            "SELECT count(*) FROM objects WHERE audience_key NOT IN (SELECT value FROM "
            "json_each(:aud))", {"aud": aud}).fetchone()[0]
        self.assertEqual(pg.execute("SELECT count(*) FROM objects WHERE audience_key NOT IN "
                                    "(SELECT value FROM json_each(:aud))", {"aud": aud})
                         .fetchone()[0], want)
        self.assertNotIn("reader_key_sets", self.sent[-1][0])
        # a head whose size is not the list's (a set cut short, or forged by an owner of
        # the table) is never read
        other = self.keys("x")
        with psycopg.connect(self.dsn, autocommit=True) as admin:
            admin.execute("INSERT INTO ontology.reader_key_set_heads (digest, n) VALUES (%s, 5)",
                          (reader_settings.digest(other),))
        reader_settings._asked.add(reader_settings.digest(other))
        pg.execute(self.QUERY, {"aud": other}).fetchone()
        self.assertNotIn("reader_key_sets", self.sent[-1][0])

    def test_put_names_the_set_from_the_text_it_reads(self):
        import psycopg
        from operating.pg import reader_settings
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            aud = self.keys("p")
            got = pg.execute("SELECT ontology.reader_key_set_put(%s)", (aud,)).fetchone()[0]
            self.assertEqual(got, reader_settings.digest(aud))
            for bad in ('{"a": 1}', '["a", 1]', "not json", None):
                self.assertIsNone(pg.execute("SELECT ontology.reader_key_set_put(%s)",
                                             (bad,)).fetchone()[0])
            # a second put of the same text adds nothing
            pg.execute("SELECT ontology.reader_key_set_put(%s)", (aud,))
        self.assertEqual(self.held(), {got: sorted(set(json.loads(aud)))})


if __name__ == "__main__":
    unittest.main()
