"""``customer_profit.read_orders``: a customer's orders read side by side as the same reader.

Evidence label: isolated-fixture (a fake tool caller; the order figures themselves are
``tests.test_customer_profit``)."""
from __future__ import annotations

import contextvars
from datetime import date
import threading
import time
import unittest

from operating import customer_profit as cp

READER = contextvars.ContextVar("reader", default="nobody")


def orders(n):
    return [cp.Order(f"SalesOrder:bc:{i}", f"SO-{i}", [date(2026, 1, 1)]) for i in range(n)]


class ReadOrders(unittest.TestCase):
    def call_of(self, seen, delay=0.0, fail=()):
        lock = threading.Lock()

        def call(tool, arguments):
            self.assertEqual(tool, "plx.investigate")
            time.sleep(delay)
            if arguments["root"] in fail:
                raise RuntimeError(arguments["root"])
            with lock:
                seen.append((arguments["root"], READER.get(), threading.get_ident()))
            return {"answer": [], "gaps": []}
        return call

    def test_every_order_is_read_once_in_order_for_one_ten_and_sixty(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                seen, given = [], orders(n)
                read, stopped = cp.read_orders(self.call_of(seen), given, lambda: False)
                self.assertEqual([o.object_id for o in read], [o.object_id for o in given])
                self.assertEqual(sorted(r for r, _, _ in seen), sorted(o.object_id for o in given))
                self.assertFalse(stopped)
                self.assertTrue(all(o.run is not None for o in read))

    def test_the_readers_context_reaches_every_read(self):
        token = READER.set("finance-reader")
        try:
            seen = []
            cp.read_orders(self.call_of(seen), orders(12), lambda: False)
        finally:
            READER.reset(token)
        self.assertEqual({who for _, who, _ in seen}, {"finance-reader"})

    def test_reads_overlap_and_are_faster_than_one_at_a_time(self):
        seen = []
        start = time.monotonic()
        cp.read_orders(self.call_of(seen, delay=0.05), orders(20), lambda: False)
        took = time.monotonic() - start
        self.assertGreater(len({t for _, _, t in seen}), 1)
        self.assertLess(took, 20 * 0.05 * 0.6)

    def test_when_time_runs_out_the_orders_read_are_a_prefix_and_none_starts_late(self):
        seen, ticks = [], iter(range(1000))
        read, stopped = cp.read_orders(self.call_of(seen, delay=0.01), orders(30),
                                       lambda: next(ticks) >= 9)
        self.assertTrue(stopped)
        self.assertEqual([o.object_id for o in read], [f"SalesOrder:bc:{i}" for i in range(len(read))])
        self.assertEqual(len(read), len(seen))
        self.assertLess(len(read), 30)
        read, stopped = cp.read_orders(self.call_of([]), orders(5), lambda: True)
        self.assertEqual((read, stopped), ([], True))

    def test_a_read_the_tools_fail_on_is_that_order_s_gap_never_another_order_s(self):
        read, _ = cp.read_orders(self.call_of([], fail={"SalesOrder:bc:2", "SalesOrder:bc:5"}),
                                 orders(8), lambda: False)
        self.assertEqual([o.number for o in read], [f"SO-{i}" for i in range(8)])
        self.assertEqual([o.number for o in read if o.run is None], ["SO-2", "SO-5"])
        self.assertTrue(all(("ORDER_MARGIN_NOT_READ" in {g[0] for g in o.gaps})
                            == (o.run is None) for o in read))

    def test_one_thread_reads_one_at_a_time_as_before(self):
        import os
        os.environ["PLX_PROFIT_READ_THREADS"] = "1"
        try:
            seen = []
            cp.read_orders(self.call_of(seen), orders(6), lambda: False)
        finally:
            del os.environ["PLX_PROFIT_READ_THREADS"]
        self.assertEqual(len({t for _, _, t in seen}), 1)
        self.assertEqual([r for r, _, _ in seen], [f"SalesOrder:bc:{i}" for i in range(6)])


if __name__ == "__main__":
    unittest.main()
