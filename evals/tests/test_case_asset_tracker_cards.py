"""Stephen's Asset Tracker case: PROPOSED dev-exposed cards and the ontology gaps they need.

``evals/dev/asset-tracker/proposed_items.json`` holds the employee questions with answer
criteria, the originals (rows of ``examples/case_originals.json``) and live sources each
needs; ``ontology_gaps.json`` names what the ontology lacks to answer them, each with a
real source (``path@commit``) or ``source unknown`` and the question that would find
one. Both are checked by ``evals.case_cards`` as one contract; a mutated copy proves the check fails closed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from evals import case_cards
from evals.case_cards import GAP_KINDS, ORIGINALS, UNKNOWN, problems

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "evals" / "dev" / "asset-tracker"


def load(name: str) -> dict:
    return case_cards.load(DIR, name)


class AssetTrackerCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards, self.gaps = load("proposed_items.json"), load("ontology_gaps.json")
        self.originals = json.loads(ORIGINALS.read_text(encoding="utf-8"))

    def test_committed_cards_and_gaps_satisfy_the_contract(self):
        self.assertEqual(problems(self.cards, self.gaps, self.originals), [])
        # The gaps cover missing objects, links and readers alike.
        kinds = {g["kind"] for g in self.gaps["gaps"]}
        self.assertEqual(kinds, GAP_KINDS)
        # A card answers from sources, never from a gold value or a business row.
        for card in self.cards["real_example"]["cards"]:
            self.assertNotIn("result", card)
            self.assertNotIn("values", card)

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        cases = {
            "uncited": lambda c, g: c["real_example"]["cards"][0].update(cites=["made-up"]),
            "exposure": lambda c, g: c["real_example"].update(exposure="held-out"),
            "unknown original": lambda c, g: c["real_example"]["cards"][0]["originals"]
            .append("co-000000000000"),
            "keys": lambda c, g: c["real_example"]["cards"][0].update(result="1"),
            "open question": lambda c, g: next(x for x in g["gaps"]
                                              if x.get("source") == UNKNOWN).pop("open_question"),
            "no source": lambda c, g: g["gaps"][0].update(sources=[]),
            "path@commit": lambda c, g: c["live_sources"][0].update(evidence="the portal"),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))

    def test_check_fails_closed_on_scope_and_maps(self):
        other = case_cards.load(case_cards.DEV / "procure-to-gl", "proposed_items.json")
        foreign = next(co for c in other["real_example"]["cards"] for co in c["originals"])
        cards = copy.deepcopy(self.cards)
        cards["real_example"]["cards"][0]["originals"].append(foreign)
        found = problems(cards, self.gaps, self.originals)
        self.assertTrue(any("unknown original" in p for p in found), found)
        # A case name no proposed_items.json under dev carries is refused.
        cards = copy.deepcopy(self.cards)
        cards["case"] = "no-such-case"
        self.assertTrue(any("no proposed_items.json" in p
                            for p in problems(cards, self.gaps, self.originals)))
        # A related source record is citable and pinned; a bad pin or unknown id is refused.
        cards = copy.deepcopy(self.cards)
        cards["source"]["related_advisories"] = [
            {"id": "advisory:aaaaaaaaaaaa", "path": "context/x.json", "ref": "r@abc1234"}]
        cards["real_example"]["cards"][0]["cites"] = ["advisory:aaaaaaaaaaaa"]
        self.assertEqual(problems(cards, self.gaps, self.originals), [])
        cards["source"]["related_advisories"][0]["path"] = "elsewhere/x.json"
        self.assertIn("advisory pin", problems(cards, self.gaps, self.originals))
        # With maps, a card must cite a source record of its own map.
        cards = copy.deepcopy(self.cards)
        card = cards["real_example"]["cards"][0]
        pfx = card["id"].split("-")[0]
        cards["maps"] = {pfx: {"advisories": ["advisory:bbbbbbbbbbbb"]}}
        self.assertTrue(any("not cited to its map" in p
                            for p in problems(cards, self.gaps, self.originals)))

    def test_new_case_directory_is_accepted_without_a_list_edit(self):
        cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
        cards["case"] = "brand-new-case"
        with tempfile.TemporaryDirectory() as tmp:
            new = Path(tmp) / "brand-new-case"
            new.mkdir()
            (new / "proposed_items.json").write_text(json.dumps(cards), encoding="utf-8")
            (new / "ontology_gaps.json").write_text(json.dumps(gaps), encoding="utf-8")
            self.assertEqual(case_cards.allowed_cases("brand-new-case", self.originals,
                                                      Path(tmp)),
                             case_cards.allowed_cases(self.cards["case"], self.originals))
            self.assertEqual(problems(case_cards.load(new, "proposed_items.json"),
                                      case_cards.load(new, "ontology_gaps.json"),
                                      self.originals, Path(tmp)), [])
            # Absent from the scanned dev dir, the same cards are refused.
            self.assertTrue(problems(cards, gaps, self.originals, Path(tmp) / "empty"))


if __name__ == "__main__":
    unittest.main()
