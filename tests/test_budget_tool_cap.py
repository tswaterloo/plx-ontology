"""A long pass asks plx.investigate for no more time than the tool accepts."""
import unittest

from operating.proactive_engine import Budget, TOOL_BUDGET_MAX_MS


class BudgetToolCapTest(unittest.TestCase):
    def test_long_budget_is_capped_to_tool_maximum(self) -> None:
        seen = []
        guarded = Budget(5100).guard(lambda tool, args: seen.append(args) or {})
        guarded("plx.investigate", {"family": "F28", "root": "x"})
        self.assertEqual(seen[0]["time_budget_ms"], TOOL_BUDGET_MAX_MS)

    def test_short_budget_passes_through(self) -> None:
        seen = []
        guarded = Budget(30).guard(lambda tool, args: seen.append(args) or {})
        guarded("plx.investigate", {})
        self.assertLessEqual(seen[0]["time_budget_ms"], 30000)


if __name__ == "__main__":
    unittest.main()
