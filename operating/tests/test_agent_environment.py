"""The agent session's environment: the allowlist and the Claude Code login alias.

Evidence label: ``isolated-fixture`` (synthetic environments; no token is read or printed).
"""

from __future__ import annotations

import unittest

from operating.agent_environment import TOKEN, TOKEN_ALIAS, agent_environment


class AgentEnvironmentTests(unittest.TestCase):
    def test_alias_fills_an_absent_or_empty_login_and_is_never_passed_on(self):
        base = {"PATH": "/bin", "PLX_WORKBENCH_API_KEY": "k-secret", "ANTHROPIC_API_KEY": "a"}
        for canonical in ({}, {TOKEN: ""}):
            env = agent_environment({**base, **canonical, TOKEN_ALIAS: "alias-value"})
            self.assertEqual(env, {"PATH": "/bin", TOKEN: "alias-value"})
        self.assertNotIn(TOKEN_ALIAS, agent_environment({**base, TOKEN: "c", TOKEN_ALIAS: "a"}))
        self.assertEqual(agent_environment({**base, TOKEN_ALIAS: ""}), {"PATH": "/bin"})

    def test_canonical_login_wins_over_the_alias(self):
        env = agent_environment({TOKEN: "canonical", TOKEN_ALIAS: "alias"})
        self.assertEqual(env, {TOKEN: "canonical"})


if __name__ == "__main__":
    unittest.main()
