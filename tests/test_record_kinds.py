"""The record page's section map is generated from the ontology schema and stays fresh."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KINDS = json.loads((ROOT / "app/src/lib/record-kinds.json").read_text())["types"]


class RecordKindsTest(unittest.TestCase):
    def test_file_is_fresh(self):
        r = subprocess.run([sys.executable, str(ROOT / "tools/gen_record_kinds.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_families_cover_the_gap_types(self):
        want = {"Meeting": "event", "CalendarEvent": "event", "PlannerTask": "task", "MCTask": "task",
                "MCApproval": "task", "MCProject": "task", "TeamsChat": "thread",
                "GeneralLedgerEntry": "finance", "AgedReceivable": "finance", "AgedPayable": "finance",
                "Payment": "finance", "SalesInvoice": "finance", "PurchaseInvoice": "finance"}
        for t, fam in want.items():
            self.assertEqual(KINDS[t].get("family"), fam, t)

    def test_money_fields_carry_the_finance_marking(self):
        # a money-role property of a finance record is finance-marked, so the server withholds it
        # from a reader without that marking; the page shows only what is sent
        for t, e in KINDS.items():
            if e.get("family") != "finance":
                continue
            for name, p in e["props"].items():
                if p["role"] == "money":
                    self.assertIn("finance", p.get("marked", []), f"{t}.{name}")


if __name__ == "__main__":
    unittest.main()
