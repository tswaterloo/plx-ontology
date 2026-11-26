"""Regression tests for circular-import safety across ``operating.pg.business_views`` and
its split view submodules (``business_views_supply``, ``business_views_chain``,
``business_views_chain_extra``, ``business_views_ar``, ``business_views_shipments``) plus
running ``python3 -m operating.pg.business_views``."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class BusinessViewsImportOrders(unittest.TestCase):
    def _run_py(self, code: str) -> dict:
        got = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, f"stderr:\n{got.stderr}\nstdout:\n{got.stdout}")
        return json.loads(got.stdout.strip().splitlines()[-1])

    def test_every_submodule_import_order_and_cli_succeed_with_identical_views(self):
        orders = (
            "operating.pg.business_views",
            "operating.pg.business_views_supply",
            "operating.pg.business_views_chain",
            "operating.pg.business_views_chain_extra",
            "operating.pg.business_views_ar",
            "operating.pg.business_views_shipments",
        )
        baseline = None
        for mod in orders:
            with self.subTest(first_import=mod):
                got = self._run_py(
                    f"import json, {mod}; "
                    "from operating.pg import business_views as bv, business_views_code as c; "
                    "print(json.dumps({n: c.fingerprint(v) for n, v in sorted(bv.VIEWS.items())}))"
                )
                self.assertIn("purchase_orders", got)
                self.assertIn("receipts", got)
                self.assertIn("freight_bills", got)
                self.assertIn("receivable_documents", got)
                self.assertIn("shipments", got)
                if baseline is None:
                    baseline = got
                else:
                    self.assertEqual(got, baseline)

    def test_running_business_views_as_main_succeeds(self):
        got = subprocess.run(
            [sys.executable, "-m", "operating.pg.business_views", "--help"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("business_views", got.stdout + got.stderr)


if __name__ == "__main__":
    unittest.main()
