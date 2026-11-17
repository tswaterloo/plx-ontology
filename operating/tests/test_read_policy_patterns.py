"""A read policy's audience decision (``ReadPolicy.may_read``): a key's exact entry, else
its longest matching wildcard pattern, else nothing. The lookup by pattern stem must decide
exactly what the walk over every pattern decided, and fast enough for a reader's whole
grant (every tool call's scope decides it over ~9,000 store keys)."""

from __future__ import annotations

import dataclasses
import pickle
import random
import time
import unittest

from ontology.types import Actor
from operating.ontology_tools_policy import ReadPolicy

TENANT = "t1"


def reference(audiences: dict[str, frozenset[str]], key: str) -> frozenset[str]:
    """The rule as it was written before the stem lookup: every pattern walked."""
    if key in audiences:
        return audiences[key]
    best = None
    for pattern, principals in audiences.items():
        if pattern.endswith("*") and key.startswith(pattern[:-1]):
            if best is None or len(pattern) > best[0]:
                best = (len(pattern), principals)
    return best[1] if best else frozenset()


def actor(*principals: str, tenant: str = TENANT) -> Actor:
    return Actor(tenant, frozenset(principals), "restricted")


class ReadPolicyPatternTests(unittest.TestCase):
    def test_longest_pattern_decides_and_a_narrower_one_takes_a_unit_out(self) -> None:
        policy = ReadPolicy(TENANT, {
            "*": frozenset({"admin"}),
            "mail:*": frozenset({"ann", "bob"}),
            "mail:box1:*": frozenset({"bob"}),          # ann is taken out of box1
            "mail:box1:secret": frozenset({"admin"}),   # exact entry beats every pattern
            "bc:units:*": frozenset({"ann"}),
        })
        ann, bob, admin = actor("ann"), actor("bob"), actor("admin")
        self.assertTrue(policy.may_read(ann, "mail:box2:m1"))
        self.assertFalse(policy.may_read(ann, "mail:box1:m1"))
        self.assertTrue(policy.may_read(bob, "mail:box1:m1"))
        self.assertFalse(policy.may_read(bob, "mail:box1:secret"))
        self.assertTrue(policy.may_read(admin, "mail:box1:secret"))
        # "*" only reaches keys no longer pattern covers
        self.assertFalse(policy.may_read(admin, "mail:box2:m1"))
        self.assertTrue(policy.may_read(admin, "sharepoint:site:x"))
        # a key shorter than a stem, or one sharing only part of it, is not that stem's
        self.assertFalse(policy.may_read(ann, "bc:unit"))
        self.assertTrue(policy.may_read(ann, "bc:units:"))

    def test_unknown_key_reader_or_tenant_fails_closed(self) -> None:
        policy = ReadPolicy(TENANT, {"mail:*": frozenset({"ann"})})
        self.assertFalse(policy.may_read(actor("ann"), "sharepoint:site:x"))
        self.assertFalse(policy.may_read(actor("ann"), ""))
        self.assertFalse(policy.may_read(actor("eve"), "mail:box:m"))
        self.assertFalse(policy.may_read(actor("ann", tenant="t2"), "mail:box:m"))
        self.assertFalse(policy.may_read(actor(), "mail:box:m"))

    def test_decides_as_the_walk_over_every_pattern_did(self) -> None:
        rng = random.Random(1115)
        parts = ["mail", "bc", "sharepoint", "a", "ab", "abc", "x:y", ""]
        for _ in range(40):
            audiences: dict[str, frozenset[str]] = {}
            for _ in range(rng.randint(1, 30)):
                stem = ":".join(rng.choice(parts) for _ in range(rng.randint(1, 3)))
                if ":" not in stem:
                    stem += ":"
                pattern = rng.choice([stem + "*", stem, stem + rng.choice(parts) + "*"])
                if rng.random() < 0.05:
                    pattern = "*"
                audiences[pattern] = frozenset(rng.sample(["p1", "p2", "p3"], rng.randint(1, 3)))
            policy = ReadPolicy(TENANT, audiences)
            for _ in range(200):
                key = ":".join(rng.choice(parts) for _ in range(rng.randint(1, 5)))
                want = reference(policy.audiences, key)
                self.assertEqual(policy._principals(key), want, (audiences, key))
                for who in ("p1", "p2", "p3"):
                    self.assertEqual(policy.may_read(actor(who), key), bool(want & {who}))

    def test_copies_keep_the_lookup(self) -> None:
        policy = ReadPolicy(TENANT, {"mail:*": frozenset({"ann"}),
                                     "mail:box1:*": frozenset({"bob"})})
        for other in (pickle.loads(pickle.dumps(policy)),
                      dataclasses.replace(policy, unrestricted=True)):
            self.assertTrue(other.may_read(actor("bob"), "mail:box1:m"))
            self.assertFalse(other.may_read(actor("ann"), "mail:box1:m"))
            self.assertTrue(other.may_read(actor("ann"), "mail:box2:m"))
        self.assertEqual(pickle.loads(pickle.dumps(policy)), policy)

    def test_a_whole_grant_is_decided_in_time(self) -> None:
        # ~240 patterns over 9,000 keys took two seconds of one core walked; any count of
        # keys (1, 10, 9,000) decides every key, none lost, well inside the budget
        audiences = {f"mail:box{i}:*": frozenset({f"p{i % 7}"}) for i in range(240)}
        audiences["mail:*"] = frozenset({"p0"})
        policy = ReadPolicy(TENANT, audiences)
        reader = actor("p3")
        for count in (1, 10, 9000):
            keys = [f"mail:box{i % 300}:m{i}" for i in range(count)]
            began = time.perf_counter()
            granted = [k for k in keys if policy.may_read(reader, k)]
            spent = time.perf_counter() - began
            want = [k for k in keys if "p3" in reference(policy.audiences, k)]
            self.assertEqual(granted, want)
            self.assertLess(spent, 0.25, count)


if __name__ == "__main__":
    unittest.main()
