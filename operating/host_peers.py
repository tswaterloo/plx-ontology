"""Concurrent tool calls of one agent session: the session's host and its helper hosts.

The case host answers one JSON-RPC line at a time, so a slow read (a document search
over the whole capture, 25-60 s) held every call the agent made beside it. The tool gate
(``agent_session.serve_gate``) now keeps, beside the session's own host (the *primary*),
up to a few *helper* hosts: the same server command, a fresh single-use ticket of the
same reader, the same agent definition and the same limits. A read-only ontology call
(``mcp_server.RETURNING_TOOLS``) that arrives while the primary is serving another call
goes to an idle helper, so independent calls run at once, each under its own per-call
limit (``call_limits``) in its own process.

Each helper decides its call exactly as the primary would: the employee gate, the read
policy, every item gate and the output boundary run in the helper for that call. What
the session's answer gate binds claims to stays in the primary: a helper's result
carries, under ``EXPORT_KEY`` in its ``_meta``, the call, the result as built, the result
as it left the output boundary, the evidence scope it was read under and the item
permission digests it read before the call. The gate strips that entry before the result
reaches the agent and hands it to the primary as an ``IMPORT_METHOD`` notification,
written to the primary before the result is relayed, so a later answer submission is
always decided after it. The primary keeps it as it keeps its own calls' evidence
(``MCPServer._peer_import``): under the scope in force now (another scope keeps
nothing), with the item permissions read now, and drops what an item gates whose
permissions the helper read otherwise (fail closed).

The import is accepted only with this session's key (``KEY_ENV``, or ``KEY_FILE_ENV`` for
a host started before its session: a random value the gate gives the primary and never
shows the agent), only by a stdio host started as the primary, and the gate never
forwards an agent's message of that method. A host without a role serves exactly as
before. Stdlib only.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Mapping

ROLE_ENV = "PLX_HOST_PEER_ROLE"      # "primary" or "helper"; unset: neither
KEY_ENV = "PLX_HOST_PEER_KEY"        # the session's import key (the primary only)
# A primary started before its session (``agent_session.HostSpares``) reads its key from
# this file instead, written when a session takes it; unwritten: it imports nothing.
KEY_FILE_ENV = "PLX_HOST_PEER_KEY_FILE"
PRIMARY, HELPER = "primary", "helper"
EXPORT_KEY = "plx/peer_evidence"     # a helper result's ``_meta`` entry (gate-only)
IMPORT_METHOD = "plx/peer_evidence"  # the gate's notification to the primary
# The gate's notice to the primary, once its client has the tool list: the session's own
# question and, when the app resolved them before the session, the things it names
# (``objective_runner.resolve_entities``), so the answer gate's relevance check has the
# question's records before the agent submits (``mcp_server``'s ``_told_question``).
QUESTION_METHOD = "plx/question"
# An import carries one result twice (as built and as shown): larger than any agent line.
MAX_IMPORT_BYTES = 4 * 1024 * 1024
MIN_KEY_CHARS = 32


def role(environ: Mapping[str, str] | None = None) -> tuple[str | None, Any]:
    """This host's peer role and, for the primary, where its import key is read from (a
    callable: the key, or None while there is none); (None, None) unless the gate
    started it with one."""
    environ = os.environ if environ is None else environ
    given = environ.get(ROLE_ENV)
    if given == HELPER:
        return HELPER, None
    if given != PRIMARY:
        return None, None
    fixed, path = environ.get(KEY_ENV) or None, environ.get(KEY_FILE_ENV) or None
    held: list[str] = []

    def key() -> str | None:
        if fixed is not None:
            return fixed if len(fixed) >= MIN_KEY_CHARS else None
        if not held and path is not None:
            try:
                with open(path, encoding="utf-8") as handle:
                    read = handle.read(256).strip()
            except OSError:
                read = ""
            if len(read) >= MIN_KEY_CHARS:
                held.append(read)
        return held[0] if held else None
    return PRIMARY, key


def keyed(params: Any, key: Any) -> bool:
    """Whether an import's ``params`` carry this primary's key (``key``: ``role``'s)."""
    expected = key() if callable(key) else None
    if expected is None or not isinstance(params, Mapping):
        return False
    given = params.get("key")
    return isinstance(given, str) and hmac.compare_digest(given.encode("utf-8"),
                                                          expected.encode("utf-8"))
