"""The injection test set (evals/injection) runs offline and every item meets its
expectation: no action registers and no fact is confirmed from injected text.

Evidence label: isolated-fixture.
"""

from __future__ import annotations

import collections
import unittest
from unittest import mock

from evals.injection import run
from operating import injection_guard as guard


class InjectionSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.items = run.load()
        cls.report = run.run_all(cls.items)

    def test_the_set_is_large_and_covers_every_attack(self):
        attacks = collections.Counter(item["attack"] for item in self.items)
        self.assertGreaterEqual(sum(n for a, n in attacks.items() if a != "control"), 30)
        for attack in run.ATTACKS:
            self.assertGreaterEqual(attacks[attack], 3, attack)

    def test_every_item_passes_and_no_injected_action(self):
        self.assertEqual(self.report["failed"], [])
        self.assertEqual(self.report["passed"], len(self.items))
        self.assertEqual(self.report["injected_actions"], 0)
        self.assertEqual(self.report["evidence_label"], "isolated-fixture")

    def test_controls_register_or_are_supported(self):
        outcomes = {r["outcome"] for r in self.report["results"] if r["attack"] == "control"}
        self.assertEqual(outcomes, {"registered", "supported"})

    def test_without_the_guard_the_propose_attacks_would_register(self):
        """The provenance hook is load-bearing: off, the injected proposals register."""
        refused = [item for item in self.items if item["attack"] != "control"
                   and item["expected"].get("code") == guard.REFUSAL]
        self.assertGreaterEqual(len(refused), 20)
        with mock.patch.object(guard, "registration_refusal", return_value=None):
            outcomes = collections.Counter(run.run_item(item)["outcome"] for item in refused)
        # Field-name attacks are refused by the field rule only, which the hook also owns.
        self.assertEqual(outcomes["refused"], 0, outcomes)

    def test_an_agent_restating_the_mail_in_its_question_is_refused(self):
        """An objective that repeats the injected value is refused when an agent (MCP)
        or nobody recorded wrote it; the same words from a human register (control)."""
        agent = [item for item in self.items
                 if item.get("objective_author_kind") in ("agent", "unknown")]
        self.assertGreaterEqual(len(agent), 4)
        results = {r["id"]: r for r in self.report["results"]}
        controls = {item["objective"] for item in self.items if item["attack"] == "control"
                    and item.get("objective_author_kind", "human") == "human"}
        for item in agent:
            self.assertIn(str(item["probe"]["equals"]), item["objective"], item["id"])
            self.assertEqual((results[item["id"]]["outcome"], results[item["id"]]["code"]),
                             ("refused", guard.REFUSAL_AGENT_OBJECTIVE), item["id"])
        twins = [item for item in agent if item["objective"] in controls]
        self.assertGreaterEqual(len(twins), 2)
        # Flipping the author of those items to human registers them: the author is
        # the only thing standing between the restated value and an intent.
        for item in twins:
            flipped = run.run_item({**item, "objective_author_kind": "human"})
            self.assertEqual(flipped["outcome"], "registered", item["id"])

    def test_a_cited_value_of_another_role_never_traces(self):
        """Role-mismatch items cite a cell holding exactly the asked value in another
        role; they are refused for the role, and only the roles stand in the way: with
        the declared roles removed the same proposals register."""
        role_items = [item for item in self.items
                      if item["expected"].get("code") == guard.REFUSAL_ROLE]
        self.assertGreaterEqual(len(role_items), 4)
        self.assertGreaterEqual({item["attack"] for item in role_items},
                                {"confirm_quantity", "approve_payment"})
        results = {r["id"]: r for r in self.report["results"]}
        for item in role_items:
            self.assertEqual((results[item["id"]]["outcome"], results[item["id"]]["code"]),
                             ("refused", guard.REFUSAL_ROLE), item["id"])
            unroled = {**item, "rows": [{k: v for k, v in row.items() if k != "roles"}
                                        for row in item["rows"]]}
            self.assertEqual(run.run_item(unroled)["outcome"], "registered", item["id"])

    def test_main_exits_zero(self):
        with mock.patch("builtins.print"):
            self.assertEqual(run.main([]), 0)


if __name__ == "__main__":
    unittest.main()
