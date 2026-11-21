"""The PO-range covers link: what range notation names, and which orders it covers."""

from __future__ import annotations

import unittest

from operating import order_cover as oc


class CoversTest(unittest.TestCase):
    def test_range_notation_and_what_fails_closed(self):
        self.assertEqual(oc.covered_numbers("PO8735-43"), ("PO", tuple(range(8735, 8744))))
        self.assertEqual(oc.covered_numbers("8811-8812"), ("", (8811, 8812)))
        self.assertEqual(oc.covered_numbers("PO-008672/73"), ("PO", (8672, 8673)))
        self.assertEqual(oc.covered_numbers("1SPO-000035/36/37"), ("1SPO", (35, 36, 37)))
        self.assertEqual(oc.covered_numbers("8769/8771"), ("", (8769, 8771)))
        # one number, a falling or too long part, a range past MAX_COVERED, a repeat, junk
        for text in ("8735", "102126-01", "OR1000153861-1", "PO8735-87999", "PO8735-8799",
                     "8735/8735", "PO-TEST", "", None, 8735, "PO8735-43 and more"):
            self.assertIsNone(oc.covered_numbers(text), text)

    def test_covers_only_one_same_customer_order_per_number(self):
        orders = [("sub", "PO8735-38", None, "beta"),
                  ("a", "OR1", "8735", "beta"),
                  ("b", "OR2", "PO-008736", "beta"),
                  ("c", "OR3", "8737", "acme"),          # another customer
                  ("d1", "OR4", "8738", "beta"),          # two of beta's hold 8738
                  ("d2", "OR5", "PO8738", "beta"),
                  ("e", "OR6", "XY8736", None),           # no single customer
                  ("f", "OR7", "QQ8735", "beta"),         # another prefix: a second 8735
                  ("sub2", "8737-38", "PO-1", "beta"),     # has its own PO: covers nothing
                  ("nocust", "PO8735-36", None, None)]
        # 8735 is held twice by beta (a and f): ambiguous, so neither is covered
        self.assertEqual(oc.covers(orders), {"sub": ["b"]})
        self.assertEqual(oc.covered_by({"s1": ["x", "y"], "s2": ["y"]}),
                         {"x": ["s1"], "y": ["s1", "s2"]})

    def test_cardinality(self):
        # N covering submissions of 3 POs each: every covered order once, none lost
        for n in (1, 10, 60):
            orders = []
            for i in range(n):
                base = 1000 + 10 * i
                orders.append((f"s{i}", f"PO{base}-{str(base + 2)[-1]}", None, "beta"))
                orders += [(f"o{i}-{k}", f"OR{i}{k}", str(base + k), "beta") for k in range(3)]
            got = oc.covers(orders)
            self.assertEqual(len(got), n)
            self.assertEqual(sum(len(v) for v in got.values()), 3 * n)
            self.assertEqual(len({o for v in got.values() for o in v}), 3 * n)


if __name__ == "__main__":
    unittest.main()
