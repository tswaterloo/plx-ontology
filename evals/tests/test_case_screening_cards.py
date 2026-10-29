"""Vince's Screening Process Map case: PROPOSED dev-exposed cards and the ontology gaps they need.

The contract is the Asset Tracker cards' checker, scoped to this case.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from evals import case_cards as contract

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "evals" / "dev" / "screening"


problems = contract.problems


class ScreeningCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards, self.gaps = (json.loads((DIR / n).read_text(encoding="utf-8"))
                                 for n in ("proposed_items.json", "ontology_gaps.json"))
        self.originals = json.loads(contract.ORIGINALS.read_text(encoding="utf-8"))

    def test_committed_cards_and_gaps_satisfy_the_contract(self):
        self.assertEqual(problems(self.cards, self.gaps, self.originals), [])
        self.assertEqual({g["kind"] for g in self.gaps["gaps"]}, contract.GAP_KINDS)
        cards = self.cards["real_example"]["cards"]
        for card in cards:
            self.assertNotIn("result", card)
            self.assertNotIn("values", card)
        self.assertEqual(self.cards["source"]["original_zip"]["sha256"],
                         "c3bbec44baba86a761b4442854f5522820c9c5cc46598e899acde6e53c456707")
        # Missing originals stay open questions, not values.
        self.assertGreaterEqual(len(self.cards["open_questions"]), 5)

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        card0 = lambda c: c["real_example"]["cards"][0]  # noqa: E731
        cases = {
            "uncited": lambda c, g: card0(c).update(cites=["made-up"]),
            "exposure": lambda c, g: c["real_example"].update(split="held_out"),
            "keys": lambda c, g: card0(c).update(values=["1"]),
            "Dot question": lambda c, g: g["gaps"][0].update(dot_question="ask the Dot"),
            "needed_by": lambda c, g: g["gaps"][0]["needed_by"].append("SC-99"),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
