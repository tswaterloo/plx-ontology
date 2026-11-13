"""Lineage inheritance cycles are found over every hop a binding declares, not only its
first one, and the cycles reported never depend on the order bindings or hops are
declared in (SCHEMA-002)."""
import itertools
import random
import unittest

from ontology.schema import _inheritance_cycles, load_schema


def _shuffled(edges, seed):
    rnd = random.Random(seed)
    keys = list(edges)
    rnd.shuffle(keys)
    out = {}
    for k in keys:
        hops = list(edges[k])
        rnd.shuffle(hops)
        out[k] = hops
    return out


class InheritanceCycles(unittest.TestCase):
    def test_a_cycle_through_a_later_hop_is_found(self):
        # a's first hop leads nowhere; its second closes a -> c -> a
        edges = {"a": ["b", "c"], "c": ["a"]}
        cycles = _inheritance_cycles(edges)
        self.assertEqual([c for c, _ in cycles], [["a", "c", "a"]])
        self.assertEqual(cycles[0][1], {"a", "c"})

    def test_self_loop_figure_eight_and_acyclic_diamond(self):
        self.assertEqual([c for c, _ in _inheritance_cycles({"s": ["s"]})], [["s", "s"]])
        eight = {"a": ["b"], "b": ["a", "c"], "c": ["b"]}
        got = _inheritance_cycles(eight)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1], {"a", "b", "c"})        # every binding of the group
        self.assertEqual(got[0][0], ["a", "b", "a"])
        diamond = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
        self.assertEqual(_inheritance_cycles(diamond), [])

    def test_declaration_order_does_not_change_the_result(self):
        edges = {"a": ["x", "b"], "b": ["c"], "c": ["a", "y"], "y": ["z"], "z": ["y"],
                 "q": ["q", "a"], "x": [], "m": ["n"], "n": ["o"], "o": ["m", "x"]}
        want = _inheritance_cycles(edges)
        self.assertEqual({tuple(c) for c, _ in want},
                         {("a", "b", "c", "a"), ("m", "n", "o", "m"), ("q", "q"),
                          ("y", "z", "y")})
        for seed in range(60):
            self.assertEqual(_inheritance_cycles(_shuffled(edges, seed)), want, seed)
        for perm in itertools.permutations(["x", "b"]):
            self.assertEqual(_inheritance_cycles({**edges, "a": list(perm)}), want)

    def test_a_long_chain_is_walked_without_recursion(self):
        n = 5000
        chain = {f"b{i:05d}": [f"b{i + 1:05d}"] for i in range(n)}
        self.assertEqual(_inheritance_cycles(chain), [])
        chain[f"b{n:05d}"] = ["b00000"]
        (cycle, group), = _inheritance_cycles(chain)
        self.assertEqual(len(group), n + 1)
        self.assertEqual(cycle[0], cycle[-1])

    def test_the_shipped_schema_has_no_cycle(self):
        load_schema()   # raises on any schema error, a lineage cycle included


if __name__ == "__main__":
    unittest.main()
