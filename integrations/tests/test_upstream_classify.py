"""One upstream-failure rule (``upstream_blocks``, by source). MC and brain
(``UpstreamError.blocks``): an outage blocks, our request, our credential or an unknown
answer fails closed, and ``mc_sources`` and ``mc_brain_probe`` read the same answer for
every code either handled. Graph: a refused credential or permission (401/403) is a
blocked part, which ``capture_teams`` reports through the same function.

Evidence label: fixture. No network I/O.
"""

from __future__ import annotations

import json
import unittest

from integrations import capture_teams, mc_brain_probe, mc_sources
from integrations.http import SourceUnavailable
from integrations.plx_hub_guard import UpstreamError, error_from_response, upstream_blocks


def http(status: int, code: str | None = None) -> UpstreamError:
    raw = json.dumps({"error": {"code": code}} if code else {}).encode()
    return error_from_response(status, raw, attempts=1, scrub=str)


def err(layer, code="upstream_error", rpc_code=None, status=200) -> UpstreamError:
    return UpstreamError("x", code=code, status=status, layer=layer, rpc_code=rpc_code)


# (case, error, blocks, mc_sources ``error``)
TABLE = [
    ("transport unreachable", err("transport", "upstream_unreachable", status=None), True,
     "transport"),
    ("transport invalid answer", err("transport", status=None), True, "transport"),
    *[(f"http {s}", http(s), True, f"http_{s}") for s in (500, 502, 503, 504, 408, 429)],
    ("http 401", http(401), False, "auth_refused"),
    ("http 403", http(403, "forbidden"), False, "auth_refused"),
    *[(f"http {s}", http(s), False, f"http_{s}") for s in (400, 404, 409, 410, 422)],
    ("http answer over the byte bound", err("http", "response_bound"), False,
     "response_bound"),
    ("checkout held", http(409, "checkout_held"), False, "checkout_held"),
    *[(f"jsonrpc {c}", err("jsonrpc", rpc_code=c), False, f"jsonrpc_{c}")
      for c in (-32700, -32600, -32601, -32602)],
    *[(f"jsonrpc {c}", err("jsonrpc", rpc_code=c), True, f"jsonrpc_{c}")
      for c in (-32603, -32000)],
    ("jsonrpc without a code", err("jsonrpc"), False, "jsonrpc_code_invalid"),
    ("tool internal", err("tool", "internal"), True, "tool_error"),
    ("tool catalog_unavailable", err("tool", "catalog_unavailable"), True, "tool_error"),
    ("tool forbidden", err("tool", "forbidden"), False, "tool_forbidden"),
    ("tool not_found", err("tool", "not_found"), False, "tool_not_found"),
    ("tool unknown code", err("tool", "brand_new"), False, "tool_other"),
    ("relay upstream_error", err("knowledge_status"), True, "knowledge_status"),
    ("relay upstream_unreachable", err("knowledge_status", "upstream_unreachable"), True,
     "knowledge_status"),
    ("relay unknown status", err("knowledge_status", "degraded"), False, "degraded"),
    ("unknown layer", err("smoke_signal", "upstream_unreachable"), False,
     "upstream_unreachable"),
    ("no layer", err(None), False, "upstream_error"),
]


class UpstreamClassifyTest(unittest.TestCase):
    def test_every_code_blocks_or_fails_the_same_for_each_consumer(self):
        for case, error, blocks, code in TABLE:
            with self.subTest(case):
                self.assertIs(error.blocks(), blocks)
                self.assertEqual(mc_sources._diagnosis(error),
                                 ("blocked_upstream" if blocks else "source_error", code))

                def fail(error=error):
                    raise error
                step = mc_brain_probe._step("brain agent/search", fail, dict)
                self.assertIs(mc_brain_probe._outage(step), blocks)

    def test_graph_blocks_on_a_refused_credential_or_permission_only(self):
        # (layer, status, graph blocks, mc blocks): one function, each source's mapping.
        for layer, status, graph, mc in (("http", 401, True, False), ("http", 403, True, False),
                                         ("http", 404, False, False), ("http", 429, False, True),
                                         ("http", 503, False, True), ("http", "403", False, False),
                                         ("transport", None, False, True)):
            with self.subTest(layer=layer, status=status):
                self.assertIs(upstream_blocks(layer, None, status, None, "graph"), graph)
                self.assertIs(upstream_blocks(layer, None, status, None), mc)
                self.assertIs(upstream_blocks(layer, None, status, None, "bc"), False)
        # Teams capture reports Graph's mapping: the part, its cause and the permission.
        for status, reason, permission in ((401, "blocked:unauthorized", None),
                                           (403, "blocked:missing_permission:Chat.Read.All",
                                            "Chat.Read.All"),
                                           (404, "source_error:", None)):
            with self.subTest(teams=status):
                error, blocked = SourceUnavailable("x"), []
                error.http_status = status
                got = capture_teams._failure("teams.chat_messages", "graph.chat_messages",
                                             error, blocked)
                self.assertEqual(got[:len(reason)] if status == 404 else got, reason)
                self.assertEqual(blocked, [] if status == 404 else [{
                    "part": "teams.chat_messages", "source": "microsoft-graph",
                    "http_status": status, "cause": "host_misconfiguration",
                    "error_code": reason.split(":")[1],
                    **({"permission": permission} if permission else {})}])


if __name__ == "__main__":
    unittest.main()
