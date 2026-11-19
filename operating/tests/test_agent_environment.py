"""The agent session's environment: the allowlist and the Claude Code login alias.

Evidence label: ``isolated-fixture`` (synthetic environments; no token is read or printed).
"""

from __future__ import annotations

import unittest

from operating.agent_environment import (CONTENT_SWITCHES, TELEMETRY, TOKEN, TOKEN_ALIAS,
                                         agent_environment)


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

    def test_telemetry_settings_pass_only_when_on_and_never_content_or_headers(self):
        otel = {"OTEL_LOGS_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
                "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=x", "OTEL_RESOURCE_ATTRIBUTES":
                "plx.source=plx-api,plx.caller=old", **{name: "1" for name in CONTENT_SWITCHES}}
        self.assertEqual(agent_environment({"PATH": "/bin", **otel}), {"PATH": "/bin"})
        self.assertEqual(agent_environment({"PATH": "/bin", TELEMETRY: "0", **otel}),
                         {"PATH": "/bin"})
        env = agent_environment({"PATH": "/bin", TELEMETRY: "1", **otel})
        self.assertEqual(env[TELEMETRY], "1")
        self.assertEqual(env["OTEL_LOGS_EXPORTER"], "otlp")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://127.0.0.1:4318")
        for name in CONTENT_SWITCHES | {"OTEL_EXPORTER_OTLP_HEADERS"}:
            self.assertNotIn(name, env)
        # the calling module replaces any inherited caller tag; the unit's source stays
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"],
                         f"plx.source=plx-api,plx.caller={__name__}")
        self.assertEqual(agent_environment({TELEMETRY: "1"})["OTEL_RESOURCE_ATTRIBUTES"],
                         f"plx.caller={__name__}")


if __name__ == "__main__":
    unittest.main()
