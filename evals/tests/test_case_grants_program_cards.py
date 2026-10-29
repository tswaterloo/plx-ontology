"""Stephen's Grants Program case: PROPOSED dev-exposed cards and the ontology gaps they need.

``evals/dev/grants-program/proposed_items.json`` holds the employee questions (NGen 14091
first, then FedDev and AMIC) with answer criteria, the originals and live sources each
needs; ``ontology_gaps.json`` names the grant, claim, cost row, ruling and hours objects,
links and readers the ontology lacks, each with a real source or the Dot question that
would find one. The contract is the asset-tracker cards' one (same formats); this module
applies it to the grants files and proves it fails closed on them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from evals.case_cards import GAP_KINDS, UNKNOWN, problems

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "evals" / "dev" / "grants-program"
ORIGINALS = ROOT / "examples" / "case_originals.json"
# The advisory capture the cards are drawn from (codex/advisory context record).
CAPTURE = "context/4db485a7f2e63ce6ab2d57e451f625fc3630e0ab2a7b1bcbe6d8ca448a86572d.json"


def load(name: str) -> dict:
    return json.loads((DIR / name).read_text(encoding="utf-8"))


class GrantsProgramCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards, self.gaps = load("proposed_items.json"), load("ontology_gaps.json")
        self.originals = json.loads(ORIGINALS.read_text(encoding="utf-8"))

    def test_committed_cards_and_gaps_satisfy_the_contract(self):
        self.assertEqual(problems(self.cards, self.gaps, self.originals), [])
        advisory = self.cards["source"]["advisory"]
        self.assertEqual(advisory["path"], CAPTURE)
        self.assertEqual(advisory["id"], "advisory:" + Path(CAPTURE).stem[:12])
        # Every card cites the advisory or the brief it summarises.
        brief = {r["id"] for r in self.originals["rows"] if r.get("case") == "stephen-grants"}
        for card in self.cards["real_example"]["cards"]:
            self.assertTrue({advisory["id"]} & set(card["cites"]) or brief & set(card["cites"]),
                            card["id"])
            # A card answers from sources, never from a gold value or a business row.
            self.assertNotIn("result", card)
            self.assertNotIn("values", card)
        # The gaps cover missing objects, links and readers alike, and every card that
        # needs a missing piece is named by one.
        self.assertEqual({g["kind"] for g in self.gaps["gaps"]}, GAP_KINDS)
        tracing = {c["id"] for c in self.cards["real_example"]["cards"]
                   if c["layer"] == "tracing"}
        needed = {cid for g in self.gaps["gaps"] for cid in g["needed_by"]}
        self.assertLessEqual(tracing, needed)

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        cases = {
            "uncited": lambda c, g: c["real_example"]["cards"][0].update(cites=["made-up"]),
            "exposure": lambda c, g: c["real_example"].update(exposure="held-out"),
            "Dot question": lambda c, g: next(x for x in g["gaps"]
                                              if x.get("source") == UNKNOWN).pop("dot_question"),
            "no source": lambda c, g: next(x for x in g["gaps"] if "sources" not in x)
            .pop("source"),
            "path@commit": lambda c, g: g["gaps"][0]["sources"][0].update(ref="the brief"),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
