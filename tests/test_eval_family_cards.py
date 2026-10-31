"""New eval families (scripts/eval_family_cards.py): F26 late-PO trail items are the
PO-lateness oracle's reading of captured rows, schema-valid, held out and cited."""

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from evals import runner
from evals import score as scoring
from evals.oracle import po_lateness
from evals.oracle.keys import FM_PO, PO, PO_LINE
from scripts import eval_family_cards as fam

AS_OF = date(2026, 10, 30)


def mirror(path: Path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE mirror_meta (name TEXT, value TEXT)")
    conn.execute("INSERT INTO mirror_meta VALUES ('generation', '5a1c29f59e06162d')")
    conn.execute("CREATE TABLE row_current (relation TEXT, key_json TEXT, row_json TEXT, "
                 "capture_id INTEGER)")
    conn.executemany("INSERT INTO row_current VALUES (?, ?, ?, 7)", [
        (relation, json.dumps({"id": row["id"]}), json.dumps(row)) for relation, row in rows])
    conn.commit()
    conn.close()


class LatePoTest(unittest.TestCase):
    def test_late_orders_become_cited_held_out_items(self):
        rows = [(PO, {"id": "po-1", "po_number": "PO9001", "status": "SUBMITTED",
                      "expected_date": "2026-10-20",
                      "custom_fields": {"fmGeneratedPoId": "3301"}}),
                # FileMaker's datearrive governs over the Portal's expected_date
                (FM_PO, {"id": 3301, "ponumber": "PO9001", "datearrive": "2026-10-01"}),
                (PO_LINE, {"id": "l-1", "purchase_order_id": "po-1", "quantity": 10,
                           "quantity_received": 0}),
                (PO_LINE, {"id": "l-2", "purchase_order_id": "po-1", "quantity": 5,
                           "quantity_received": 5}),
                # late but Portal-native: out until a person confirms its rows (R1)
                (PO, {"id": "po-2", "po_number": "PO9002", "status": "SUBMITTED",
                      "expected_date": "2026-09-01", "custom_fields": {}}),
                (PO_LINE, {"id": "l-3", "purchase_order_id": "po-2", "quantity": 1,
                           "quantity_received": 0})]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mirror.sqlite"
            mirror(db, rows)
            portal, generation = fam.mirror_rows(db, fam.LATE_PO_RELATIONS)
            self.assertTrue(all(scoring.API_HANDLE.match(r["citation"])
                                for r in portal[PO]))
            result = po_lateness.late_orders(portal, [], AS_OF)
            items = fam.late_po_items(result, as_of=AS_OF, generation=generation,
                                      now="2026-10-30T12:00:00Z")
            out = Path(tmp) / "out"
            self.assertEqual(fam.main(["late-po", "--mirror", str(db), "--as-of",
                                       "2026-10-30", "--out", str(out)]), 0)
            written = sorted(p.name for p in out.iterdir())
        self.assertEqual(written, ["ho-F26-0001.json"])
        (item,) = items
        # held-out items never go into a repository
        self.assertEqual(fam.main(["late-po", "--mirror", str(db), "--as-of", "2026-10-30",
                                   "--out", str(fam.ROOT / "evals" / "heldout-x")]), 2)
        self.assertFalse((fam.ROOT / "evals" / "heldout-x").exists())
        self.assertEqual(runner.validate_item(item), [])
        self.assertEqual((item["split"], item["label"]["status"], item["root"]["identifier"]),
                         ("held_out", "oracle_only", "PO9001"))
        values = {v["name"]: v for v in item["known_outcome"]["values"]}
        self.assertEqual({k: v["value"] for k, v in values.items()},
                         {"late_open_lines": 1, "governing_date": "2026-10-01",
                          "receipts_recorded": 0})
        # the governing date cites the record and field it is read from
        self.assertEqual(values["governing_date"]["citations"][0]["field_path"],
                         "row.datearrive")
        self.assertTrue(values["governing_date"]["citations"][0]["l1_handle"]
                        .startswith(f"api://{FM_PO}/"))
        self.assertEqual(item["lineage_stratum"], "gold_etl_from_fm")
        # a vendor's confirmation is in no relation the oracle reads: unknown, never a value
        self.assertEqual(item["known_outcome"]["answerability"], "partial")
        self.assertNotIn("vendor_confirmation", values)


if __name__ == "__main__":
    unittest.main()
