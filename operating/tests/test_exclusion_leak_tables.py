import unittest

from operating import exclusion_leak


class _Cur:
    def __init__(self, names):
        self.names = names

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return [(n,) for n in self.names]


class StagingTablesLeftOut(unittest.TestCase):
    def test_a_refreshs_build_and_swap_tables_are_not_scanned(self):
        cur = _Cur(["emails", "emails_next", "sales_invoice_lines_next", "plan_section_items_new",
                    "emails_ship", "orders_old", "next_steps", "purchase_orders"])
        self.assertEqual(exclusion_leak._tables(cur, "biz"),
                         ["emails", "next_steps", "purchase_orders"])
        self.assertEqual(cur.params, ("biz",))


class UndefinedTable(Exception):
    pass


class SwapRetry(unittest.TestCase):
    def _run(self, outcomes):
        import tempfile
        from pathlib import Path
        calls, naps = [], []

        def scanner(*a):
            calls.append(1)
            out = outcomes[min(len(calls), len(outcomes)) - 1]
            if isinstance(out, Exception):
                raise out
            return out
        with tempfile.TemporaryDirectory() as d:
            status = exclusion_leak.run(Path(d), "dsn", {}, scanner=scanner, sleep=naps.append)
        return status, len(calls), naps

    def test_a_view_swapped_mid_scan_is_rerun_once(self):
        status, n, naps = self._run([UndefinedTable(), {"ok": True, "served_total": 0}])
        self.assertTrue(status["ok"])
        self.assertEqual((n, len(naps)), (2, 1))

    def test_a_real_fault_is_not_retried_and_a_second_swap_failure_is_reported(self):
        status, n, _ = self._run([ValueError("x")])
        self.assertEqual((status["ok"], n), (False, 1))
        status, n, _ = self._run([UndefinedTable()])
        self.assertEqual((status["ok"], n), (False, 2))
        self.assertIn("UndefinedTable", status["error"])


if __name__ == "__main__":
    unittest.main()
