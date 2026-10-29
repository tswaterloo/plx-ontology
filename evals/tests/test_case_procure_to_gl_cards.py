"""Two procure-to-GL case maps: PROPOSED dev-exposed cards and the ontology gaps they need.

``evals/dev/procure-to-gl/proposed_items.json`` holds the employee questions of the
Screening Process Map (SP-*, gap-25d6764d61b3) and of Vince's Packaging Procure-to-GL Map (PG-*, gap-894ce0e4f47d), each with answer criteria, the
originals and live sources it needs; ``ontology_gaps.json`` names the screening decision,
conversion step, G/L posting lineage and the other objects, links and readers the
ontology lacks. The contract is the Asset Tracker cards' checker; the captures beyond the
first are pinned under ``related_advisories`` and each card must cite its own map's.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from evals import case_cards as contract

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "evals" / "dev" / "procure-to-gl"
MAPS = {"SP": ("gap-25d6764d61b3", {"advisory:4f8c497adb40", "advisory:6d4918ddaffe"}),
        "PG": ("gap-894ce0e4f47d", {"advisory:1a0ce08d8937"})}


problems = contract.problems


class ProcureToGlCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards, self.gaps = (json.loads((DIR / n).read_text(encoding="utf-8"))
                                 for n in ("proposed_items.json", "ontology_gaps.json"))
        self.originals = json.loads(contract.ORIGINALS.read_text(encoding="utf-8"))

    def test_committed_cards_and_gaps_satisfy_the_contract(self):
        self.assertEqual(problems(self.cards, self.gaps, self.originals), [])
        cards = self.cards["real_example"]["cards"]
        for prefix, (gap, advisories) in MAPS.items():
            entry = self.cards["maps"][prefix]
            self.assertEqual(entry["gap"], gap)
            mine = [c for c in cards if c["id"].startswith(prefix + "-")]
            self.assertTrue(mine, prefix)
            for card in mine:
                self.assertTrue(advisories & set(card["cites"]), card["id"])
        self.assertEqual({c["id"].split("-")[0] for c in cards}, set(MAPS))
        self.assertEqual({g["kind"] for g in self.gaps["gaps"]}, contract.GAP_KINDS)
        for card in cards:
            self.assertNotIn("result", card)
            self.assertNotIn("values", card)
        # Every card that needs a live source is named by a gap.
        needed = {c for g in self.gaps["gaps"] for c in g["needed_by"]}
        self.assertEqual([c["id"] for c in cards if c["live_sources"] and c["id"] not in needed],
                         [])

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        cards_of = lambda c, prefix: [x for x in c["real_example"]["cards"]  # noqa: E731
                                      if x["id"].startswith(prefix)]
        cases = {
            "uncited": lambda c, g: cards_of(c, "SP")[0].update(cites=["made-up"]),
            # A packaging card citing only the screening map is not cited to its map.
            "not cited to its map": lambda c, g: cards_of(c, "PG")[0]
            .update(cites=["advisory:4f8c497adb40"]),
            "advisory pin": lambda c, g: c["source"]["related_advisories"][0]
            .update(path="responses/x.json"),
            "exposure": lambda c, g: c["real_example"].update(exposure="held-out"),
            # Another case's original (the artwork map) is not this case's.
            "unknown original": lambda c, g: cards_of(c, "PG")[0]["originals"]
            .append("co-bbd2a0e1abe4"),
            "unknown live source": lambda c, g: cards_of(c, "SP")[0]["live_sources"]
            .append("bc.valueEntries"),
            "keys": lambda c, g: cards_of(c, "SP")[0].update(values=["1"]),
            "Dot question": lambda c, g: next(x for x in g["gaps"]
                                              if x.get("source") == contract.UNKNOWN)
            .update(dot_question="ask the Dot"),
            "no source": lambda c, g: next(x for x in g["gaps"] if "source" not in x)
            .update(sources=[]),
            "path@commit": lambda c, g: g["gaps"][0]["sources"][0].update(ref="the map"),
            "needed_by": lambda c, g: g["gaps"][0]["needed_by"].append("SP-99"),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
