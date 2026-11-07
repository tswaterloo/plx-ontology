"""A find reads the delta overlay's layers side by side (operating/ontology_host.py
``_each_layer``): the answers come back in the layers' order, each read runs in the
caller's context (its SQL deadline), a failing read fails the whole, and a busy pool
never holds the caller back. Evidence label: ``isolated-fixture`` (made-up layers)."""

from __future__ import annotations

import threading
import time
import unittest

from operating import call_limits, ontology_host
from operating.ontology_host import _each_layer


class EachLayerTests(unittest.TestCase):
    def test_answers_keep_the_layers_order_and_run_side_by_side(self):
        layers = [f"layer-{i}" for i in range(12)]
        threads = set()

        def read(layer):
            threads.add(threading.get_ident())
            time.sleep(0.05)
            return layer.upper()
        began = time.monotonic()
        self.assertEqual(_each_layer(layers, read), [x.upper() for x in layers])
        self.assertLess(time.monotonic() - began, 12 * 0.05 * 0.6)
        self.assertGreater(len(threads), 1)

    def test_each_read_sees_the_callers_deadline(self):
        with call_limits.sql_deadline(time.monotonic() + 30):
            left = _each_layer(range(5), lambda _layer: call_limits.seconds_left())
        self.assertTrue(all(value is not None and 0 < value <= 30 for value in left))
        self.assertEqual(_each_layer(range(3), lambda _l: call_limits.seconds_left()),
                         [None] * 3)

    def test_a_failing_read_fails_the_whole(self):
        def read(layer):
            if layer == 3:
                raise ValueError("layer-unreadable")
            return layer
        with self.assertRaisesRegex(ValueError, "layer-unreadable"):
            _each_layer(range(6), read)

    def test_a_busy_pool_never_holds_the_caller(self):
        pool = ontology_host._layer_pool()
        release = threading.Event()
        held = [pool.submit(release.wait, 10) for _ in range(ontology_host.LAYER_WORKERS)]
        try:
            began = time.monotonic()
            self.assertEqual(_each_layer(range(8), lambda layer: layer * 2),
                             [i * 2 for i in range(8)])
            self.assertLess(time.monotonic() - began, 2.0)
        finally:
            release.set()
            for future in held:
                future.result(timeout=10)


if __name__ == "__main__":
    unittest.main()
