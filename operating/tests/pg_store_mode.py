"""Run existing read-tool tests with every store read from Postgres (``PLX_ONTOLOGY_PG``).

``python3 -m operating.tests.pg_store_mode tests.test_ontology_tools ...``

Each ``StoreReader`` the tests make over a SQLite store is given that store's rows in a
throwaway Postgres database (loaded again when the store changed since its last load; a
store in memory is copied to a file first) and reads them through
``sqlite_compat.PgCompatConnection``, as ``PgStoreReader`` does on a host: the same tests,
the same assertions, so every permission, marking and as-known-at rule they check holds
on both stores. The word index stays SQLite's (``PgStoreReader``); a delta layer stays
SQLite (the overlay reads it).
"""

from __future__ import annotations

import atexit
import io
import os
import re
import tempfile
from pathlib import Path
import sqlite3
import sys
import unittest

from operating.tests import pg_testdb

MIGRATIONS = [Path(__file__).resolve().parents[1] / "pg" / "migrations" / name
              for name in ("0006_ontology.sql", "0007_ontology_compact.sql",
                           "0008_ontology_read.sql", "0009_schema_migrations.sql",
                           "0010_ontology_words.sql", "0011_ontology_word_order.sql")]
_STATE: dict = {}
_WRITE = re.compile(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
_Following = None


def _database(key: object) -> str:
    """A database of its own per store (two stores open at once read apart)."""
    held = _STATE.setdefault("dbs", {})
    if key not in held:
        import psycopg
        name, dsn = pg_testdb.create_db()
        atexit.register(pg_testdb.drop_db, name)
        with psycopg.connect(dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        held[key] = {"dsn": dsn, "loaded": None}
    return held[key]["dsn"]


def _store_file(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        has_store = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'objects'"
                                 ).fetchone()
    except sqlite3.Error:
        return None
    return (row[2] or "") if row and has_store else None


def _stamp(path: str, conn: sqlite3.Connection) -> tuple:
    return (Path(path).stat().st_mtime_ns if path else 0, conn.total_changes,
            conn.execute("PRAGMA data_version").fetchone()[0])


def _load(path: str, conn: sqlite3.Connection) -> str:
    """The store's rows in its own database, loaded again when the store changed (a write
    on its connection or a commit on another one) since the last load."""
    from operating.pg import ontology_load
    key = path or id(conn)
    dsn = _database(key)
    held = _STATE["dbs"][key]
    stamp = _stamp(path, conn)
    if held["loaded"] != stamp or held.get("conn") is not conn:
        if conn.in_transaction:
            conn.commit()
            stamp = _stamp(path, conn)
        source = path
        if not path:   # a store in memory: its rows through a file of their own
            folder = _STATE.setdefault("tmp", tempfile.mkdtemp(prefix="pg-store-mode-"))
            source = str(Path(folder) / f"memory-{len(os.listdir(folder))}.sqlite")
            with sqlite3.connect(source) as copy:
                conn.backup(copy)
        ontology_load.load(source, dsn, workers=1, replace=True, log=io.StringIO())
        if not path:
            Path(source).unlink()
        held["loaded"], held["conn"] = stamp, conn
        _STATE["loads"] = _STATE.get("loads", 0) + 1
    return dsn


def install() -> None:
    from operating import ontology_tools as ot
    from operating.pg.ontology_reader import PgStoreReader
    from operating.pg.sqlite_compat import PgCompatConnection

    global _Following

    class _Following(PgCompatConnection):
        """Reads the database of its SQLite store, loaded again first whenever the store
        changed since (a test writes rows after its tools were built)."""

        def __init__(self, dsn: str, path: str, source: sqlite3.Connection) -> None:
            super().__init__(dsn)
            self._source_path, self._source = path, source

        def execute(self, sql, parameters=()):  # noqa: ANN001
            if _WRITE.match(sql):
                # a test's own row written through the reader's connection: into its
                # SQLite store, which the next read loads again (Postgres is read only)
                return self._source.execute(sql, parameters)
            try:
                _load(self._source_path, self._source)
            except sqlite3.ProgrammingError:   # the test closed its store
                pass
            return super().execute(sql, parameters)

    plain_init = ot.StoreReader.__init__

    def init(self, conn):  # noqa: ANN001
        path = _store_file(conn) if isinstance(conn, sqlite3.Connection) else None
        if path is not None and conn.execute("SELECT 1 FROM sqlite_master WHERE name = "
                                             "'delta_owned'").fetchone():
            path = None   # a delta layer: the overlay reads it as SQLite
        if path is None:
            plain_init(self, conn)
            return
        text = ot.StoreReader.__new__(ot.StoreReader)
        plain_init(text, conn)
        _STATE["readers"] = _STATE.get("readers", 0) + 1
        plain_init(self, _Following(_load(path, conn), path, conn))
        self.text_reader = text
        self.conn.mirror = conn   # the word index's functions, as PgStoreReader sets them
        self.sqlite_path = path
        if type(self) is ot.StoreReader:
            self.__class__ = PgStoreReader

    ot.StoreReader.__init__ = init


# Tests of the SQLite store's own mechanics, which a Postgres store has no part of: a query
# plan or a word-index narrowing step of SQLite's (Postgres reads the same rows by its own
# plan, which the read's answer, not its steps, is compared on) and the facts kept beside a
# store file (a Postgres store has no file; each process reads its facts once, in the
# background). Skipped here with their reason; they run on SQLite in the plain suites.
SQLITE_MECHANICS = {
    "ReviewL81Tests.test_text_passes_narrowed_by_layout_return_what_the_word_scan_returns":
        "spies on SQLite's layout narrowing step",
    "DocumentsTests.test_a_hits_text_is_read_by_its_versions_citation_never_by_scanning_"
    "every_chunk": "asserts SQLite's query plan",
    "KeptFactsTests.": "facts kept beside a SQLite store file",
}


def _skip_sqlite_mechanics(suite: unittest.TestSuite) -> unittest.TestSuite:
    out = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            out.addTest(_skip_sqlite_mechanics(test))
            continue
        name = ".".join(test.id().split(".")[-2:])
        reason = next((why for key, why in SQLITE_MECHANICS.items()
                       if name == key or (key.endswith(".") and name.startswith(key))), None)
        if reason is not None:
            _STATE["skipped"] = _STATE.get("skipped", 0) + 1
            setattr(test, test._testMethodName,
                    unittest.skip(f"SQLite store mechanics: {reason}")(lambda: None))
        out.addTest(test)
    return out


def main(argv: list[str]) -> int:
    install()
    suite = _skip_sqlite_mechanics(unittest.defaultTestLoader.loadTestsFromNames(argv))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(f"pg_store_mode: {_STATE.get('readers', 0)} readers on Postgres, "
          f"{_STATE.get('loads', 0)} store loads, {_STATE.get('skipped', 0)} SQLite-mechanics "
          "tests skipped", file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
