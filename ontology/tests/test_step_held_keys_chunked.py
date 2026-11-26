"""Unit test for chunked ``_copy_held_keys`` in ``ontology.step_migrate``: verifies
that ``outp`` is sliced into 200k-row chunks joining compact value tables
(``object_versions -> value_sources -> value_cells -> value_fields`` and
``identifiers``) and that ``held_links`` queries ``ontology.link_rows`` directly
without a redundant self-join to ``ontology.links``."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
import unittest

from ontology import step_migrate as sm


def _desc(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=n) for n in names]


class _FakeNamedCursor:
    def __init__(self, log: list[tuple[str, tuple]], name: str) -> None:
        self._log = log
        self._name = name
        self.description: list[SimpleNamespace] = []
        self._rows: list[tuple] = []
        self._idx = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._log.append((sql, params))
        if "FROM outp o JOIN ontology.object_versions" in " ".join(sql.split()):
            self.description = _desc("object_id", "property")
            lo = params[0]
            self._rows = [(f"Obj:{lo}", "prop_a")]
        elif "FROM outp o JOIN ontology.identifiers" in " ".join(sql.split()):
            self.description = _desc("identifier_kind", "normalized", "object_id", "property")
            lo = params[0]
            self._rows = [("code", f"k{lo}", f"Obj:{lo}", "num")]
        elif "FROM ontology.link_rows l" in sql:
            self.description = _desc(
                "link_id", "link_type", "from_object", "to_object",
                "tier", "binding_kind", "superseded_at",
            )
            self._rows = [("lnk:1", "A.b", "A:1", "B:1", "firm", "direct", None)]
        else:
            self.description = []
            self._rows = []
        self._idx = 0

    def fetchmany(self, size: int = 1000) -> list[tuple]:
        chunk = self._rows[self._idx:self._idx + size]
        self._idx += len(chunk)
        return chunk


class _FakeConn:
    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self._log = log

    def cursor(self, name: str = "") -> _FakeNamedCursor:
        return _FakeNamedCursor(self._log, name)


class _FakeCur:
    def __init__(self, max_rn: int) -> None:
        self.log: list[tuple[str, tuple]] = []
        self.connection = _FakeConn(self.log)
        self._max_rn = max_rn
        self._last = None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.log.append((sql, params))
        if "max(rn)" in sql:
            self._last = (self._max_rn,)
        else:
            self._last = None

    def fetchone(self):
        return self._last


class CopyHeldKeysChunkedTests(unittest.TestCase):
    def test_chunks_outp_in_200k_slices_and_avoids_links_self_join(self) -> None:
        conn = sqlite3.connect(":memory:")
        cur = _FakeCur(max_rn=450_000)
        sm._copy_held_keys(cur, conn, ["DropType"], ["DropType:"], [("L", "direct", "A:", "B:")])

        val_queries = [(sql, p) for sql, p in cur.log if "ontology.value_fields" in sql]
        id_queries = [(sql, p) for sql, p in cur.log if "ontology.identifiers" in sql]
        link_queries = [(sql, p) for sql, p in cur.log if "ontology.link_rows" in sql]

        # 450,000 rows -> 3 chunks of 200,000: (1, 200000), (200001, 400000), (400001, 600000)
        self.assertEqual([p for _, p in val_queries], [(1, 200000), (200001, 400000), (400001, 600000)])
        self.assertEqual([p for _, p in id_queries], [(1, 200000), (200001, 400000), (400001, 600000)])
        self.assertEqual(len(link_queries), 1)
        self.assertNotIn("ontology.links", link_queries[0][0])
        self.assertNotIn("ontology.object_values", " ".join(sql for sql, _ in cur.log))

        # Rows actually landed in SQLite held tables
        self.assertEqual(conn.execute("SELECT count(*) FROM held_values").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT count(*) FROM held_identifiers").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT count(*) FROM held_links").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
