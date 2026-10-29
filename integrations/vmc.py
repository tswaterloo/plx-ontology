"""Guarded, read-only client for the brain's 7 agent knowledge GET routes.

Today the brain is served by VMC (agentic-swarm ``apps/vmc-web`` @
``b1117811c``, routes ``app/api/vmc/knowledge/agent/*``); after retirement R7
it moves to ``brain.plxcustomer.io`` with the same paths, envelope and header
(``devloop/CONTEXT/05-VMC-BRAIN-TOOLS.md`` §0.1, §4.1, §6). The host and key
are configured as one pair (``plx_hub_guard.BrainConfig``): ``BRAIN_BASE_URL``
+ ``BRAIN_API_KEY`` (``brn_*``) when the brain key is set, else
``VMC_API_URL`` + ``VMC_API_KEY``.

``VMC_API_KEY`` can also reach VMC trading POST routes, so every request is
checked by ``plx_hub_guard.check_brain_request`` before I/O: GET only, only
``agent/{search,items,node/{id},document/{...id},subgraph,trail,status}``.
Responses are retried (bounded) on 429/502/503/504.

``/api/vmc/cursor/context`` retired with VMC (not one of the routes that move
to the brain, pack 05 §0.2); task context is Mission Control ``mc_get_context``.

Brain content is derived engineering/agent knowledge with no per-employee
ACL; upstream labels agent text as 0.9 ``deterministic_link`` facts. Every
hit is an untrusted lead (T3), never a business fact. No
write path exists here (no ingest, relations or session-artifact).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode

from .http import SourceUnavailable
from .plx_hub_guard import (BrainConfig, HubTransport, check_brain_request,
                            error_from_response)


Transport = Callable[[str, Mapping[str, str]], Any]
UNTRUSTED_LEAD = {"label": "untrusted_lead", "trust_tier": "T3", "business_fact": False,
                  "note": "derived brain knowledge; upstream confidence/classification "
                          "labels are not evidence"}


class BrainReadClient:
    """The 7 key-authenticated brain GETs, nothing else."""

    def __init__(self, config: BrainConfig, *, agent_name: str = "plx-agent-workbench",
                 hub: HubTransport | None = None,
                 transport: Transport | None = None) -> None:
        if not isinstance(config, BrainConfig):
            raise TypeError("config must be a BrainConfig")
        if (not isinstance(agent_name, str) or not 1 <= len(agent_name) <= 80 or
                any(ord(character) < 33 or ord(character) > 126
                    for character in agent_name)):
            raise ValueError("VMC agent name must be printable ASCII without spaces")
        self._config = config
        self._agent = agent_name
        self._hub = hub or HubTransport()
        self._hub.register_secret(config.key)
        self._transport = transport

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._config!r})"

    @property
    def pair(self) -> str:
        return self._config.pair

    def _get(self, route: str, params: list[tuple[str, str]] | None = None) -> Mapping[str, Any]:
        url = (self._config.base_url.rstrip("/") + "/api/vmc/knowledge/agent/" + route
               + ("?" + urlencode(params) if params else ""))
        check_brain_request("GET", url, brain_host=self._config.host, environ=self._hub.environ)
        headers = {"X-API-Key": self._config.key.reveal(), "X-Agent-Name": self._agent,
                   "X-MCP-Session": "plx-agent-workbench", "Accept": "application/json"}
        if self._transport is not None:
            response = self._transport(url, headers)
        else:
            status, _headers, raw = self._hub.send("GET", url, headers, None, retry=True)
            if status != 200:
                raise error_from_response(status, raw, attempts=self._hub.last_attempts,
                                          scrub=self._hub.scrub)
            try:
                response = json.loads(raw)
            except ValueError:
                raise SourceUnavailable("brain returned non-JSON") from None
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise SourceUnavailable("brain response lacks data object")
        return response

    def status(self) -> Mapping[str, Any]:
        response = self._get("status")
        if not isinstance(response["data"].get("available"), bool):
            raise SourceUnavailable("brain status lacks available")
        return response

    def search(self, query: str, *, source: str = "all", limit: int = 10) -> Mapping[str, Any]:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("VMC knowledge query must be 1..200 characters")
        if source not in {"all", "memory", "documents"}:
            raise ValueError("VMC knowledge source invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25:
            raise ValueError("VMC knowledge limit must be 1..25")
        response = self._get("search", [("q", query.strip()), ("source", source),
                                        ("mode", "hybrid"), ("limit", str(limit))])
        data = response["data"]
        if (data.get("query") != query.strip() or data.get("source") != source or
                not isinstance(data.get("results"), list) or
                not isinstance(data.get("total"), int) or
                not isinstance(data.get("partial"), bool)):
            raise SourceUnavailable("VMC knowledge response shape invalid")
        return {**response, "trust": dict(UNTRUSTED_LEAD)}

    def items(self, *, query: str | None = None, order: str = "recent",
              since: str | None = None, limit: int = 25) -> Mapping[str, Any]:
        if order not in ("recent", "importance"):
            raise ValueError("items order invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("items limit must be 1..100")
        params = [("order", order), ("limit", str(limit))]
        if query:
            params.append(("q", query))
        if since:
            params.append(("since", since))
        return {**self._get("items", params), "trust": dict(UNTRUSTED_LEAD)}

    def node(self, node_id: str, *, include_content: bool = False) -> Mapping[str, Any]:
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("node id required")
        route = "node/" + quote(node_id, safe=":@")
        params = [("include", "content")] if include_content else None
        return {**self._get(route, params), "trust": dict(UNTRUSTED_LEAD)}

    def document(self, document_id: str) -> Mapping[str, Any]:
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("document id required")
        route = "document/" + "/".join(quote(s, safe=":@") for s in document_id.split("/"))
        return {**self._get(route), "trust": dict(UNTRUSTED_LEAD)}

    def subgraph(self, center: str, *, hops: int = 1) -> Mapping[str, Any]:
        if not isinstance(hops, int) or isinstance(hops, bool) or not 1 <= hops <= 3:
            raise ValueError("hops must be 1..3")
        return {**self._get("subgraph", [("center", center), ("hops", str(hops))]),
                "trust": dict(UNTRUSTED_LEAD)}

    def trail(self, start: str, end: str, *, max_hops: int = 3) -> Mapping[str, Any]:
        if not isinstance(max_hops, int) or isinstance(max_hops, bool) or not 1 <= max_hops <= 4:
            raise ValueError("maxHops must be 1..4")
        return {**self._get("trail", [("from", start), ("to", end), ("maxHops", str(max_hops))]),
                "trust": dict(UNTRUSTED_LEAD)}


__all__ = ["BrainReadClient", "UNTRUSTED_LEAD"]


def items_page(client: BrainReadClient, *, offset: int, limit: int = 100,
               order: str = "recent") -> Mapping[str, Any]:
    """One offset page of ``agent/items`` (the route's own ``offset`` key, guarded like
    every brain GET): ``integrations.mc_sources`` pages the brain's items with it."""
    if order not in ("recent", "importance"):
        raise ValueError("items order invalid")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("items limit must be 1..100")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 1_000_000:
        raise ValueError("items offset must be 0..1000000")
    response = client._get("items", [("order", order), ("limit", str(limit)),
                                     ("offset", str(offset))])
    return {**response, "trust": dict(UNTRUSTED_LEAD)}
