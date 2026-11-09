"""scripts/mirror_record_counts at its boundary: an ETL-reinserted relation's copies (equal
but for id, created_at, updated_at) count as one record, an absent row is not counted, a
relation with unique keys counts its keys, and a missing mirror fails closed."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import mirror_record_counts as mrc


def mirror(rows: dict[str, list[tuple[str, dict, bool]]]) -> Path:
    path = Path(tempfile.mkdtemp()) / "m.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE relation_state (relation TEXT PRIMARY KEY, row_count INTEGER)")
    conn.execute("CREATE TABLE row_current (relation TEXT, key_json TEXT, run_index INTEGER,"
                 " row_json TEXT, absent_since_capture INTEGER)")
    for relation, items in rows.items():
        conn.execute("INSERT INTO relation_state VALUES (?, ?)",
                     (relation, sum(1 for *_, absent in items if not absent)))
        for key, row, absent in items:
            conn.execute("INSERT INTO row_current VALUES (?, ?, 0, ?, ?)",
                         (relation, json.dumps({"id": key}), json.dumps(row),
                          7 if absent else None))
    conn.commit()
    conn.close()
    return path


class MirrorRecordCountsTest(unittest.TestCase):
    def test_copies_are_one_record_and_unique_keys_count_as_rows(self):
        task = "public.app_production_task"
        self.assertIn(task, mrc.minted_columns())
        copies = [(f"t{n}", {"id": f"t{n}", "created_at": f"2026-0{n}", "updated_at": "x",
                             "mo_id": "mo-1", "qty": 3}, False) for n in range(1, 61)]
        other = [("u1", {"id": "u1", "created_at": "a", "updated_at": "b", "mo_id": "mo-2",
                         "qty": 3}, False),
                 ("u2", {"id": "u2", "created_at": "a", "updated_at": "b", "mo_id": "mo-3",
                         "qty": 3}, True)]  # absent from the latest traversal
        orders = [(f"o{n}", {"id": f"o{n}", "created_at": "same"}, False) for n in range(10)]
        path = mirror({task: copies + other, "public.app_purchase_order": orders})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(mrc.main(["--mirror", str(path)]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["relations"][task],
                         {"rows_current": 61, "records": 2, "copies_per_record": 30.5,
                          "basis": "natural_key_less_minted"})
        self.assertEqual(report["relations"]["public.app_purchase_order"],
                         {"rows_current": 10, "records": 10, "copies_per_record": 1.0,
                          "basis": "distinct_key"})
        self.assertEqual((report["rows_current"], report["records"]), (71, 12))

    def test_missing_mirror_fails_closed(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = mrc.main(["--mirror", str(Path(tempfile.mkdtemp()) / "none.sqlite")])
        self.assertEqual((code, json.loads(out.getvalue())), (2, {"error": "mirror_not_found"}))


if __name__ == "__main__":
    unittest.main()
