"""The three R&D process maps' case: PROPOSED dev-exposed cards and the ontology gaps they need.

``evals/dev/rnd-maps/proposed_items.json`` holds graded questions on Allison's bi-weekly
agenda map (AG), Erin's raw material samples map (RS) and Giulia's ingredient-list map (IL):
comprehension, tracing answerable today and the maps' worked examples, each naming its
missing outcome; ``ontology_gaps.json`` names what the ontology lacks. The contract is
``evals.case_cards``; each card cites its own map.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from evals import case_cards as contract

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "evals" / "dev" / "rnd-maps"


class RndMapsCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards, self.gaps = (json.loads((DIR / n).read_text(encoding="utf-8"))
                                 for n in ("proposed_items.json", "ontology_gaps.json"))
        self.originals = json.loads(contract.ORIGINALS.read_text(encoding="utf-8"))

    def test_committed_cards_and_gaps_satisfy_the_contract(self):
        self.assertEqual(contract.problems(self.cards, self.gaps, self.originals), [])
        cards = self.cards["real_example"]["cards"]
        self.assertEqual({c["id"].split("-")[0] for c in cards}, {"AG", "RS", "IL"})
        needed = {c for g in self.gaps["gaps"] for c in g["needed_by"]}
        for card in cards:
            self.assertFalse({"result", "values"} & set(card), card["id"])
            if card["layer"] == "actual_case_answerability":
                self.assertTrue(card["criterion"].startswith("Missing outcome"), card["id"])
                self.assertIn(card["id"], needed)
            if card["layer"] == "tracing":   # answered today: each names a live source
                self.assertTrue(card["live_sources"], card["id"])
        self.assertEqual({g["kind"] for g in self.gaps["gaps"]}, {"object", "link", "reader"})

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return contract.problems(cards, gaps, self.originals)

        def card(c, cid):
            return next(x for x in c["real_example"]["cards"] if x["id"] == cid)
        cases = {
            "not cited to its map": lambda c, g: card(c, "RS-02").update(
                cites=["advisory:d7076d889756"]),
            "uncited": lambda c, g: card(c, "AG-02").update(cites=["made-up"]),
            "unknown live source": lambda c, g: card(c, "IL-02")["live_sources"].append("fm.x"),
            "needed_by": lambda c, g: g["gaps"][0]["needed_by"].append("AG-99"),
            "Dot question": lambda c, g: next(x for x in g["gaps"]
                                              if x["id"] == "AG-GAP-02").pop("dot_question"),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
