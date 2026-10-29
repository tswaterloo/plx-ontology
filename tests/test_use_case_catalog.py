"""The shipped use-case catalog (``examples/use_case_catalog.json``) against the registry
and the eval card sets: every committed card set's use case is in the catalog and named
by its own id in a question ("In the <id> use case"), every case original and gap row it
names is a registry row, and the catalog loads (``operating.use_case_catalog``).

Evidence label: ``isolated-fixture``. Reads committed files only.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from operating import use_case_catalog as ucc

ROOT = Path(__file__).resolve().parents[1]


class ShippedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(ucc.CATALOG.read_text(encoding="utf-8"))
        self.catalog = ucc.load()
        registry = json.loads((ROOT / "examples" / "case_originals.json").read_text(
            encoding="utf-8"))
        self.rows = {row["id"]: row for row in registry["rows"]}

    def test_every_card_set_is_a_use_case_its_questions_name(self):
        sets = sorted(path.parent.name for path in (ROOT / "evals" / "dev").glob(
            "*/proposed_items.json"))
        self.assertTrue(sets)
        ids = {case.id for case in self.catalog.use_cases}
        self.assertEqual(sorted(set(sets) - ids), [])
        for name in sets:
            with self.subTest(case=name):
                named, unknown = self.catalog.named(f"In the {name} use case, what is due?")
                self.assertEqual([case.id for case in named], [name])
                self.assertFalse(unknown)

    def test_card_openings_select_their_use_case_and_record_queries_select_none(self):
        # The question openings of the v8 card sets (the RRO set names its map file).
        openings = {
            "rro": "In 'Related Release Order Map.md' (Draft 8): Which day-one release rule",
            **{name: f"In the {name} use case, what does the map say?"
               for name in (case.id for case in self.catalog.use_cases) if name != "rro"}}
        self.assertEqual(len(openings), 13)
        for name, opening in openings.items():
            with self.subTest(case=name):
                self.assertEqual([c.id for c in self.catalog.named(opening)[0]], [name])
        for query in ("PO 5280 expired material", "amazon fba shipment 123",
                      "screening blanks", "asset tracker", "quote 2026-10-09 G2 Beauty",
                      "packaging sourcing PO5447 freight", "R&D bi-weekly agenda 14 Oct"):
            with self.subTest(query=query):
                self.assertEqual(self.catalog.named(query), ([], False))

    def test_every_original_and_gap_row_is_in_the_registry(self):
        for case in self.raw["use_cases"]:
            for document in case["documents"]:
                if "original" in document:
                    self.assertIn(document["original"], self.rows, case["id"])
            for gap in case["gaps"]:
                self.assertEqual(self.rows[gap["registry_row"]]["status"], "gap", case["id"])


if __name__ == "__main__":
    unittest.main()
