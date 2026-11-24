"""Parties (C7, D1): rows join only on evidence (a shared system code, a settled identity
link, the same name with the same address, phone or a domain only that name uses); two
accounts one system keeps apart, a name alone, a lead link and a shared mail service never
join; what looks alike is an undecided pair with its evidence; ids are stable."""
from __future__ import annotations

import unittest

from operating.pg import parties as P


def row(h, name, codes="", email="", phone=""):
    return {"handle": h, "name": name, "codes": codes, "email": email, "phone": phone}


class Decide(unittest.TestCase):
    def test_evidence_joins_and_nothing_else_does(self):
        rows = [
            row("a1", "Atlas Containers", "bc:ATL1", phone="416-743-1347"),
            row("a2", "ATLAS CONTAINERS", "portal:77", phone="(416) 743 1347"),
            row("n1", "National account X", "bc:NAT1"), row("n2", "National account X", "bc:NAT1"),
            row("r1", "Royal Club", "sage:ROYA03", email="a@rcyc.ca"),
            row("r2", "Royal Club", "sage:ROYA04", email="a@rcyc.ca"),
            row("g1", "Apex Motor Express", "bc:V1", email="x@gmail.com"),
            row("g2", "Western Canada Express", "bc:V2", email="y@gmail.com"),
            row("g3", "Apex Motor Express", "portal:9", email="z@gmail.com"),
            row("s1", "Same Name Co", "bc:S1"), row("s2", "Same Name Co", "portal:S2"),
            row("l1", "Lead One", "bc:L1"), row("l2", "Lead Two", "bc:L2"),
            row("v1", "Verified One", "bc:VA"), row("v2", "Verified Uno", "portal:VB"),
        ]
        links = [("l1", "l2", "lead"), ("v1", "v2", "verified")]
        sets, undecided = P.decide(rows, links)
        same = lambda a, b: sets.find(a) == sets.find(b)   # noqa: E731
        self.assertTrue(same("a1", "a2"))      # same name + same phone (two systems)
        self.assertTrue(same("n1", "n2"))      # a shared system code
        self.assertTrue(same("v1", "v2"))      # a settled identity link
        self.assertFalse(same("r1", "r2"))     # two Sage accounts: never one by a rule
        self.assertFalse(same("g1", "g3"))     # gmail is used by other names: says nothing
        self.assertFalse(same("g1", "g2"))
        self.assertFalse(same("s1", "s2"))     # a name alone
        self.assertFalse(same("l1", "l2"))     # a lead link alone
        pairs = {(a, b): ev for a, b, ev in undecided}
        self.assertIn(("r1", "r2"), pairs)
        self.assertIn(("s1", "s2"), pairs)
        self.assertIn(("l1", "l2"), pairs)
        self.assertEqual(pairs[("l1", "l2")], {"lead_link": "lead"})
        self.assertEqual(sets.why["a1"][0], "rule")
        self.assertEqual(sets.why["n1"], ("code", {"code": "bc:nat1"}))
        # stable ids: the smallest member's digest, whatever the order
        self.assertEqual(P.party_id(["b", "a"]), P.party_id(["a", "b"]))

    def test_cost_grows_with_rows_not_pairs_of_unrelated_rows(self):
        import time
        took = {}
        for n in (1, 10, 60):
            rows = [row(f"h{i}", f"Company {i}", f"bc:C{i}", phone=str(4160000000 + i))
                    for i in range(n * 50)]
            t = time.monotonic()
            sets, undecided = P.decide(rows, [])
            took[n] = time.monotonic() - t
            self.assertEqual(len({sets.find(r["handle"]) for r in rows}), n * 50)
            self.assertEqual(undecided, [])
        self.assertLess(took[60], 60 * took[1] * 3 + 0.5)


if __name__ == "__main__":
    unittest.main()
