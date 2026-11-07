"""A find reads the delta overlay's layers side by side (operating/ontology_host.py
``_each_layer``): the answers come back in the layers' order, each read runs in the
caller's context (its SQL deadline), a failing read fails the whole, and a busy pool
never holds the caller back. Evidence label: ``isolated-fixture`` (made-up layers)."""

from __future__ import annotations

import threading
import time
import unittest

from operating import call_limits, delta_overlay, ontology_host, ontology_tools
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


class SharedDerivationTests(unittest.TestCase):
    """What a find derives from the schema and from the layers' ownership is kept across
    sessions (each loads its own schema view and opens its own overlay), never across
    different content."""

    DIGEST = "sha256:" + "a" * 64

    def view(self, digest, label):
        return ontology_tools.SchemaView(object_types={"T": {"label": label}}, digest=digest)

    def test_a_schema_derivation_is_shared_by_content_digest_only(self):
        built = []

        def derive(view):
            return lambda: built.append(view) or view.object_types["T"]["label"]
        first, again = self.view(self.DIGEST, "one"), self.view(self.DIGEST, "one")
        self.assertEqual(ontology_tools._schema_memo(first, "k", derive(first)), "one")
        self.assertEqual(ontology_tools._schema_memo(again, "k", derive(again)), "one")
        self.assertEqual(built, [first])   # the same content, another session's view
        other = self.view("sha256:" + "b" * 64, "two")
        self.assertEqual(ontology_tools._schema_memo(other, "k", derive(other)), "two")
        # without a content digest each view derives its own, however alike
        bare, twin = self.view("", "three"), self.view("", "four")
        self.assertEqual(ontology_tools._schema_memo(bare, "k", derive(bare)), "three")
        self.assertEqual(ontology_tools._schema_memo(twin, "k", derive(twin)), "four")
        loose, fake = self.view("test", "five"), self.view("test", "six")
        self.assertEqual(ontology_tools._schema_memo(loose, "k", derive(loose)), "five")
        self.assertEqual(ontology_tools._schema_memo(fake, "k", derive(fake)), "six")

    def test_every_overlay_of_the_same_layers_serves_from_the_newest_owner(self):
        newest, middle, oldest = (frozenset({"a", "b"}), frozenset({"b", "c"}),
                                  frozenset({"a", "c", "d"}))

        class Reader:
            def audience_keys(self):
                return []
        for _ in range(2):   # a second host's overlay reuses the first's derivation
            layers = [delta_overlay.Layer(Reader(), None, ids)
                      for ids in (newest, middle, oldest)]
            overlay = delta_overlay.Overlay(layers, ("stamp",))
            self.assertEqual([layer.serves for layer in layers],
                             [{"a", "b"}, {"c"}, {"d"}])
            self.assertEqual({oid: layers.index(overlay.owner(oid)) for oid in "abcd"},
                             {"a": 0, "b": 0, "c": 1, "d": 2})
            self.assertIsNone(overlay.owner("e"))
            self.assertIsNone(overlay.owner(None))
            self.assertEqual(overlay.owned, {"a", "b", "c", "d"})
        # the same ids in other set objects (another layer list) are derived anew
        swapped = [delta_overlay.Layer(Reader(), None, ids)
                   for ids in (frozenset(oldest), frozenset(newest))]
        delta_overlay.Overlay(swapped, ("other",))
        self.assertEqual([layer.serves for layer in swapped], [oldest, {"b"}])


if __name__ == "__main__":
    unittest.main()


class SchemaMemoByValueTest(__import__("unittest").TestCase):
    def test_two_unpickled_copies_of_one_schema_share_the_memo(self):
        from operating import ontology_tools as t
        built = []
        class S:
            def __init__(self, d): self.digest = d
        d = "sha256:" + "ab" * 32
        a, b = S("".join(list(d))), S("".join(list(d)))
        self.assertIsNot(a.digest, b.digest)
        if not t._CONTENT_DIGEST.match(a.digest):
            self.skipTest("digest shape differs")
        t._schema_memo(a, "k-test", lambda: built.append(1) or 1)
        t._schema_memo(b, "k-test", lambda: built.append(1) or 1)
        t._schema_memo(a, "k-test", lambda: built.append(1) or 1)
        self.assertEqual(len(built), 1)
