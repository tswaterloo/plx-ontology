"""The environment a headless Claude Code agent session is started with.

An allowlist (``ENV_ALLOW`` and ``LC_*``): the process basics, the Claude Code
subscription login and the outbound proxy/CA settings the session needs to reach
Claude. Everything else (credentials, DSNs, ``ANTHROPIC_API_KEY``,
``CLAUDE_CODE_USE_BEDROCK`` / ``_VERTEX`` and other paid-provider switches) is dropped
(Q7: no paid API). Used by the employee agent loop's planner (``operating.agent_loop``)
and the eval runner (``evals.runner``).

``PLX_CLAUDE_CODE_OAUTH_TOKEN`` is an alias for the login: a Claude Code session can hide
its own ``CLAUDE_CODE_OAUTH_TOKEN`` from the subprocesses its tools start, so the token is
saved under that name too. The agent session gets it as ``CLAUDE_CODE_OAUTH_TOKEN`` only
when the canonical name is absent or empty; the alias name itself is never passed on.

Usage metering (``operating.usage_meter``): ``CLAUDE_CODE_ENABLE_TELEMETRY``, the
``OTEL_*`` exporter settings and ``CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS`` pass, so every
headless call reports its tokens and cost; the content switches (``CONTENT_SWITCHES``:
prompts, responses, tool text, raw bodies) and exporter headers/keys never do. With
telemetry on, the module that asked for the environment is added to
``OTEL_RESOURCE_ATTRIBUTES`` as ``plx.caller``.
"""

from __future__ import annotations

import sys
from typing import Mapping

ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ",
    "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy",
    "http_proxy", "no_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "MAX_MCP_OUTPUT_TOKENS", "MCP_TOOL_TIMEOUT"})
TELEMETRY = "CLAUDE_CODE_ENABLE_TELEMETRY"
TELEMETRY_ALLOW = frozenset({TELEMETRY, "CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS"})
CONTENT_SWITCHES = frozenset({
    "OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_TOOL_CONTENT",
    "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_RAW_API_BODIES"})
RESOURCE = "OTEL_RESOURCE_ATTRIBUTES"
TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_ALIAS = "PLX_CLAUDE_CODE_OAUTH_TOKEN"


def telemetry_name(name: str) -> bool:
    """A usage-metering setting that may pass (never a content switch, header or key)."""
    if name in TELEMETRY_ALLOW:
        return True
    return (name.startswith("OTEL_") and name not in CONTENT_SWITCHES
            and "HEADERS" not in name and "CLIENT_KEY" not in name)


def agent_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment of an agent session: ``ENV_ALLOW`` and ``LC_*`` only, the login
    from ``TOKEN_ALIAS`` when the canonical name is absent or empty, and the metering
    settings when telemetry is on (tagged with the calling module)."""
    env = {name: value for name, value in environ.items()
           if name in ENV_ALLOW or name.startswith("LC_")}
    if not env.get(TOKEN) and environ.get(TOKEN_ALIAS):
        env[TOKEN] = environ[TOKEN_ALIAS]
    if str(environ.get(TELEMETRY) or "").strip() not in ("", "0", "false"):
        env.update({name: value for name, value in environ.items() if telemetry_name(name)})
        caller = sys._getframe(1).f_globals.get("__name__") or "?"
        tag = "plx.caller=" + "".join(c if c.isalnum() or c in "._-" else "_" for c in caller)
        kept = [p.strip() for p in str(environ.get(RESOURCE) or "").split(",")
                if p.strip() and not p.strip().startswith("plx.caller=")]
        env[RESOURCE] = ",".join(kept + [tag])
    return env


__all__ = ["CONTENT_SWITCHES", "ENV_ALLOW", "TELEMETRY", "TOKEN", "TOKEN_ALIAS",
           "agent_environment", "telemetry_name"]
