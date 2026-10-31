"""Vince's Artwork Lifecycle case: PROPOSED dev-exposed cards and the ontology gaps they need.

``evals/dev/artwork-lifecycle/proposed_items.json`` holds the employee questions with
answer criteria, the originals (``vince-artwork`` rows of ``examples/case_originals.json``)
and live sources each needs; ``ontology_gaps.json`` names what the ontology lacks to
answer them (artwork version, proof, printed-component lineage, customer approval, label
reconciliation), each with a real source or ``source unknown`` and a question. The
contract is the Asset Tracker cards' checker, scoped to this case's originals.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from evals import case_cards as contract

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "evals" / "dev" / "artwork-lifecycle"


problems = contract.problems


class ArtworkLifecycleCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards, self.gaps = (json.loads((DIR / n).read_text(encoding="utf-8"))
                                 for n in ("proposed_items.json", "ontology_gaps.json"))
        self.originals = json.loads(contract.ORIGINALS.read_text(encoding="utf-8"))

    def test_an_anchor_or_anchor_gap_is_accepted_only_well_formed(self):
        # card anchors: AL-08 carries an anchor with its source, AL-06 an anchor_gap
        by = {c["id"]: c for c in self.cards["real_example"]["cards"]}
        self.assertTrue(by["AL-08"]["anchor"]["source"])
        self.assertTrue(by["AL-06"]["anchor"]["source"])
        broken = {
            "AL-08": lambda c: c["anchor"].pop("source"),
            "AL-09": lambda c: c["anchor"].update(note="x"),
            "AL-10": lambda c: c.update(anchor_gap="also a gap"),
            "AL-06": lambda c: (c.pop("anchor"), c.update(anchor_gap=" ")),
            "AL-07": lambda c: c.update(anchor_note="an unknown key"),
        }
        cards = copy.deepcopy(self.cards)
        for card in cards["real_example"]["cards"]:
            if card["id"] in broken:
                broken[card["id"]](card)
        self.assertEqual(problems(cards, self.gaps, self.originals),
                         ["AL-06: anchor_gap empty", "AL-07: keys ['anchor_note']",
                          "AL-08: anchor malformed", "AL-09: anchor malformed",
                          "AL-10: anchor and anchor_gap"])

    def test_committed_cards_and_gaps_satisfy_the_contract(self):
        self.assertEqual(problems(self.cards, self.gaps, self.originals), [])
        self.assertEqual(self.cards["case"], "vince-artwork")
        self.assertEqual({g["kind"] for g in self.gaps["gaps"]}, contract.GAP_KINDS)
        cards = self.cards["real_example"]["cards"]
        for card in cards:
            self.assertNotIn("result", card)
            self.assertNotIn("values", card)
        # Every card is needed by a gap or answerable from the map alone (no live source).
        needed = {c for g in self.gaps["gaps"] for c in g["needed_by"]}
        self.assertEqual([c["id"] for c in cards if c["live_sources"] and c["id"] not in needed],
                         [])

    def test_handoff_cases_are_cited_by_sha256_and_mapped_to_gaps(self):
        arts = {a["case_id"]: a for a in self.cards["source"]["related_advisories"][0]["source_artifacts"]}
        self.assertEqual({k: a["sha256"] for k, a in arts.items()}, {
            "a7bdb9e8": "9bc825a28e334b25886a29b44a671a29ba209d71e2e677f5b2eea2730102784a",
            "b98e41f2": "63a92f8f15b4cd2bd64eddb0adfef914632b0263abaa030822edf44eb9278a3b"})
        gap_ids = {g["id"] for g in self.gaps["gaps"]}
        for art in arts.values():
            self.assertEqual(set(art["maps_to"]), {"exact-version approval",
                                                   "re-upload invalidation", "lineage"})
            self.assertEqual(art["maps_to"]["lineage"], ["AL-GAP-04"])
            for ids in art["maps_to"].values():
                self.assertLessEqual(set(ids), gap_ids)
            self.assertEqual(set(art["maps_to"]["exact-version approval"]), {"AL-GAP-01", "AL-GAP-02"})
            self.assertFalse({"body", "text", "content"} & set(art))
        dot = [g for g in self.gaps["gaps"] if g.get("source") == contract.UNKNOWN
               and "Artwork Handoff" in g["name"]]
        self.assertEqual(len(dot), 4)

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        card0 = lambda c: c["real_example"]["cards"][0]  # noqa: E731
        cases = {
            "uncited": lambda c, g: card0(c).update(cites=["made-up"]),
            "exposure": lambda c, g: c["real_example"].update(split="held_out"),
            # Another case's original (Stephen's asset brief) is not this case's.
            "unknown original": lambda c, g: card0(c)["originals"].append("co-2b32fe4f123c"),
            "unknown live source": lambda c, g: card0(c)["live_sources"].append("bc.nowhere"),
            "keys": lambda c, g: card0(c).update(values=["1"]),
            "Dot question": lambda c, g: next(x for x in g["gaps"]
                                              if x.get("source") == contract.UNKNOWN)
            .update(dot_question="ask the Dot"),
            "no source": lambda c, g: next(x for x in g["gaps"] if "source" not in x)
            .update(sources=[]),
            "path@commit": lambda c, g: g["gaps"][0]["sources"][0].update(ref="the portal"),
            "needed_by": lambda c, g: g["gaps"][0]["needed_by"].append("AL-99"),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
