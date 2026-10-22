"""Ricardo's process-map case: PROPOSED dev-exposed cards and the ontology gaps they need.

``evals/dev/ricardo-maps/proposed_items.json`` holds the questions on the three October 2
process-map packages and five original-backed anchors, each naming its missing outcome;
``ontology_gaps.json`` names what the ontology lacks. The contract is the Asset Tracker
cards' checker; the related source records are cited as the first, as in the procure-to-GL set.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from evals import case_cards as contract

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "evals" / "dev" / "ricardo-maps"


problems = contract.problems


class RicardoMapsCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards, self.gaps = (json.loads((DIR / n).read_text(encoding="utf-8"))
                                 for n in ("proposed_items.json", "ontology_gaps.json"))
        self.originals = json.loads(contract.ORIGINALS.read_text(encoding="utf-8"))

    def test_committed_cards_and_gaps_satisfy_the_contract(self):
        self.assertEqual(problems(self.cards, self.gaps, self.originals), [])
        cards = self.cards["real_example"]["cards"]
        for card in cards:
            self.assertNotIn("result", card)
            self.assertNotIn("values", card)
        # Every anchor outcome is named as missing and has a live source and a gap.
        outcome = [c for c in cards if c["layer"] == "actual_case_answerability"]
        self.assertEqual(len(outcome), 5)
        needed = {c for g in self.gaps["gaps"] for c in g["needed_by"]}
        for card in outcome:
            self.assertIn("Missing outcome", card["criterion"], card["id"])
            self.assertIn(card["id"], needed)
            # Q1-Q5 each cite the outcome review and at least one source.
            self.assertIn("advisory:5f2b9ba6bfd2", card["cites"], card["id"])
            self.assertTrue(card["live_sources"], card["id"])
        self.assertTrue({"object", "link", "reader"} <= {g["kind"] for g in self.gaps["gaps"]})

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        first = lambda c: c["real_example"]["cards"][0]  # noqa: E731
        cases = {
            "uncited": lambda c, g: first(c).update(cites=["made-up"]),
            "advisory pin": lambda c, g: c["source"]["related_advisories"][0]
            .update(path="responses/x.json"),
            "exposure": lambda c, g: c["real_example"].update(exposure="held-out"),
            "unknown original": lambda c, g: first(c)["originals"].append("co-000000000000"),
            "keys": lambda c, g: first(c).update(result="1"),
            "open question": lambda c, g: g["gaps"][0].pop("open_question"),
            "needed_by": lambda c, g: g["gaps"][0]["needed_by"].append("RM-99"),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
