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
"""

from __future__ import annotations

from typing import Mapping

ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ",
    "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy",
    "http_proxy", "no_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE"})
TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_ALIAS = "PLX_CLAUDE_CODE_OAUTH_TOKEN"


def agent_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment of an agent session: ``ENV_ALLOW`` and ``LC_*`` only, the login
    from ``TOKEN_ALIAS`` when the canonical name is absent or empty."""
    env = {name: value for name, value in environ.items()
           if name in ENV_ALLOW or name.startswith("LC_")}
    if not env.get(TOKEN) and environ.get(TOKEN_ALIAS):
        env[TOKEN] = environ[TOKEN_ALIAS]
    return env


__all__ = ["ENV_ALLOW", "TOKEN", "TOKEN_ALIAS", "agent_environment"]
