"""The migration pre-flight's join counts (``ontology.build_budget``): exact against a
reference count, bounded in memory, on the build's worker count."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology import build_budget
from ontology.project_parallel import ProjectionError


def _mirror(path: str, rows: list[tuple[str, int, dict]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE row_current (relation TEXT, run_index INTEGER, row_json TEXT)")
    conn.executemany("INSERT INTO row_current VALUES (?, ?, ?)",
                     [(r, i, json.dumps(j)) for r, i, j in rows])
    conn.commit()
    conn.close()


def _reference(rows, a, b):
    """The pre-11-09 Python count: every key value's from-rows times its to-rows."""
    def hist(relation, paths):
        held, total = {}, 0
        for r, i, j in rows:
            if r != relation or i != 0:
                continue
            total += 1
            vals = [j.get(p[4:] if p.startswith("row.") else p) for p in paths]
            if any(v is None for v in vals):
                continue
            key = "\x1f".join(str(v).strip().lower() for v in vals)
            held[key] = held.get(key, 0) + 1
        return total, held
    ta, ha = hist(*a)
    tb, hb = hist(*b)
    products = [n * hb.get(v, 0) for v, n in ha.items()]
    return {"from_rows": ta, "to_rows": tb, "candidates": sum(products),
            "largest_value": max(products, default=0)}


class JoinSizeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.mirror = os.path.join(self.tmp.name, "m.sqlite")
        rows = []
        for i in range(60):   # lines: order key with case/space noise, some missing
            j = {"order_id": f" O{i % 7} " if i % 2 else f"o{i % 7}", "part": f"P{i % 3}"}
            if i % 10 == 0:
                del j["order_id"]
            rows.append(("public.line", 0, j))
            rows.append(("public.line", 1, {"order_id": "o1"}))   # older copy: not counted
        for i in range(9):    # orders O0..O3 twice, O4 once
            rows.append(("public.order", 0, {"id": f"O{i % 5}", "part": "p1"}))
        rows.append(("public.order", 0, {"id": None}))
        rows.append(("public.other", 0, {"id": "zz"}))
        self.rows = rows
        _mirror(self.mirror, rows)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def check(self, a, b):
        got = build_budget.join_size(self.mirror, a, b)
        got.pop("seconds")
        self.assertEqual(got, _reference(self.rows, a, b))
        return got

    def test_counts_equal_the_reference(self) -> None:
        got = self.check(("public.line", ["order_id"]), ("public.order", ["id"]))
        self.assertGreater(got["candidates"], 0)
        self.check(("public.line", ["order_id", "part"]), ("public.order", ["id", "part"]))
        self.check(("public.line", ["row.order_id"]), ("public.line", ["row.order_id"]))
        self.check(("public.line", ["part"]), ("public.line", ["order_id"]))
        none = self.check(("public.line", ["order_id"]), ("public.other", ["id"]))
        self.assertEqual(none["candidates"], 0)
        empty = self.check(("public.absent", ["id"]), ("public.order", ["id"]))
        self.assertEqual((empty["from_rows"], empty["largest_value"]), (0, 0))

    def test_preflight_pool_follows_the_build_workers(self) -> None:
        seen = []

        class Pool:
            def __init__(self, max_workers, mp_context):
                seen.append(max_workers)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(build_budget, "specs", return_value=([], [])), \
                mock.patch.object(build_budget.futures, "ProcessPoolExecutor", Pool):
            with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "3"}):
                build_budget.preflight({}, None, Path(self.tmp.name), budget=1)
            with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "lots"}):
                with self.assertRaises(ProjectionError):
                    build_budget.preflight({}, None, Path(self.tmp.name), budget=1)
        self.assertEqual(seen, [3])


if __name__ == "__main__":
    unittest.main()
