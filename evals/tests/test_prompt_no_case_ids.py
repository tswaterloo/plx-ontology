"""The answer prompt names no real case: every example in its fixed text is synthetic.

The prompt every eval item sends once named AO-26005B, OR1000151885 and SKU-1509, real-case
data (verify/hardcoded-case-denylist.json) and the subject of an exposed item, so it hinted
that item's answer. scripts/check_no_hardcoded_cases.py does not scan evals/; this test
holds the prompt to the same denylist and to the identifiers that checker derives from
evals/.

Evidence label: isolated-fixture (the committed dev and exposed items, no host)."""

from __future__ import annotations

import unittest

from evals import runner
from scripts import check_no_hardcoded_cases as checker

# Stands in for the item's own question, which names its subject by design.
QUESTION = "What is the answer?"


class PromptNamesNoCaseTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = runner.REPO_ROOT
        deny = checker.load_denylist(root)
        cls.patterns = deny.identifiers + checker.eval_identifiers(
            checker.tree_eval_documents(root), deny.identifiers)
        cls.items = [item for split in ("dev", "exposed")
                     for item in runner.load_items(root / "evals" / split, split="dev",
                                                   admit=False)]

    def test_the_checker_derives_identifiers_and_items_load(self):
        self.assertTrue(any(name.startswith("eval:") for name, _ in self.patterns))
        self.assertTrue(any(item["id"].endswith("-exposed") for item in self.items))
        self.assertTrue(any(not item["id"].endswith("-exposed") for item in self.items))

    def test_no_prompt_names_a_denylisted_or_eval_identifier(self):
        for item in self.items:
            prompt = runner.build_prompt(item, QUESTION)
            with self.subTest(item=item["id"]):
                self.assertEqual([(name, match.group(0)) for name, pattern in self.patterns
                                  for match in pattern.finditer(prompt)], [])

    def test_the_check_finds_a_real_case_id_in_a_prompt(self):
        prompt = runner.build_prompt(self.items[0], QUESTION) + " (AO-26005B states 1509)"
        found = {name for name, pattern in self.patterns if pattern.search(prompt)}
        self.assertTrue({"AO-26005B", "AO-26005B-quantities"} <= found)


if __name__ == "__main__":
    unittest.main()
