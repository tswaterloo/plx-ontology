"""A Messages API planner for ``operating.agent_loop.run``: native tool use, prompt caching
and streaming over HTTPS, stdlib only.

It is a planner like ``agent_loop.ClaudeCodePlanner`` (``planner(request) -> (decision,
tokens)``) and nothing more: the loop still owns the tools (the session's read-only
catalog), the bounds, the evidence rule and the claim gate, and it trusts no decision this
planner returns. What differs is how a request reaches the model:

* Tools: the request's catalog becomes Messages API tools (``plx.find`` is ``plx_find``: a
  tool name takes no dot); a name that does not fit, or collides, is not offered (fail
  closed). Three decision tools end a turn: ``final_answer`` (the loop's answer object),
  ``abstain`` and ``escalate`` (a reason). ``tool_choice`` ``any``: every reply is a tool
  call. Read calls the model makes become ``{"action": "call"}``; a decision tool beside a
  read call waits (the reads run first); no tool call at all falls back to the JSON reply
  of ``PLANNER_RULES``, else the reply is invalid (None), as ``ClaudeCodePlanner``'s.
* Conversation: stateless, as the loop's planners are: each request rebuilds the messages
  from the loop's transcript (each step's calls as ``tool_use`` blocks, their observations
  as ``tool_result`` blocks; a refused call of a tool not in the catalog, a skipped call and
  an unusable reply as text), then one closing text block with ``remaining`` and any
  ``repair``.
* Prompt caching: ``cache_control`` (ephemeral) on the last tool, on the system prompt
  (``MESSAGES_RULES`` + ``agent_loop.PLANNER_RULES``) and on the last block before the
  closing text, so each step re-reads the conversation so far from the cache.
* Streaming: ``stream: true``; the server-sent events are parsed as they arrive (text and
  ``input_json_delta`` per block, usage from ``message_start`` and ``message_delta``); a
  stream that ends without ``message_stop``, carries an ``error`` event, outgrows
  ``MAX_STREAM_BYTES`` or outlasts ``timeout`` is an invalid reply (None).
* Failures: an HTTP 400/401/403/404 (a request, key or model the API refuses; repeating
  cannot help) escalates ``planner_process_failed:http_<status>``, as a planner process that
  cannot start does; anything else (429, 5xx, transport, timeout) is an invalid reply, so
  two in a row escalate the loop (``planner_reply_invalid``).
* Usage: the reported tokens are the conversation's size (input, cache writes, cache reads
  and output summed), the loop's token bounds' unit; every request appends its measured
  latency, time to the first event and token counts to ``metrics`` (``ask_usage`` turns an
  Ask's requests into its latency and cost, ``usage_summary`` a bench's).

Settings (names only; a value is never printed, logged or put in an error): the key from
``KEY_SETTINGS`` (``ANTHROPIC_API_KEY``, else the Workbench's ``PLX_ANTHROPIC_API_KEY``), the
model from ``MODEL_SETTING`` or the ``model`` argument (no model is built in). A missing one
is a typed refusal, ``PlannerRefused`` ``credentials_absent`` / ``model_setting_absent``
naming the settings. ``ask_planner`` is the Ask's selection (``PLANNER_SETTING``
``messages``; default ``claude-code``): a refused Messages planner falls back to
``ClaudeCodePlanner``, the refusal reported beside it. The request carries the tool results
this employee's session returned (their gate applied): they leave the host for the model
provider, as ``ClaudeCodePlanner``'s do. The one HTTP sender is ``post_messages``: a POST to
``API_URL`` only, redirects refused (``scripts/check_http_egress.py`` allowlists it).

Evidence: injected-transport (operating/tests/test_messages_planner.py, a fake transport);
live-unverified until live check ``eval.ask_agent_messages`` has a result.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import math
import re
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import urllib.error
import urllib.request

from .agent_loop import PLANNER_RULES

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
KEY_SETTINGS = ("ANTHROPIC_API_KEY", "PLX_ANTHROPIC_API_KEY")
MODEL_SETTING = "PLX_MESSAGES_MODEL"
PLANNER_SETTING = "PLX_ASK_PLANNER"
PLANNERS = ("claude-code", "messages")
PRICES_SETTING = "PLX_MESSAGES_PRICES"  # {"input", "cache_write", "cache_read", "output"}
                                        # per million tokens and "currency" (ISO 4217)
MAX_OUTPUT_TOKENS = 4096
MAX_STREAM_BYTES = 8 << 20
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
REFUSING_STATUSES = (400, 401, 403, 404)
TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
              "output_tokens")
PRICE_KEYS = {"input_tokens": "input", "cache_creation_input_tokens": "cache_write",
              "cache_read_input_tokens": "cache_read", "output_tokens": "output"}
EPHEMERAL = {"type": "ephemeral"}

_REASON = {"type": "object", "properties": {"reason": {"type": "string"}},
           "required": ["reason"]}
DECISION_TOOLS = {
    "final_answer": ("answer", "Answer the question: the answer object of the rules below "
                     "(structured claims, each one value with the L1 handle the tools "
                     "returned as citation and its field; text naming claims as {id}; "
                     "unknowns).",
                     {"type": "object", "properties": {
                         "claims": {"type": "array", "items": {"type": "object", "properties": {
                             "id": {"type": "string"},
                             "value": {"type": ["string", "integer"]},
                             "citation": {"type": "string"},
                             "field": {"type": "string"},
                             "role": {"type": "string"},
                             "unit": {"type": "string"},
                             "currency": {"type": "string"},
                             "op": {"type": "string", "enum": ["count", "sum", "expression"]},
                             "of": {"type": "array", "items": {"type": "object"}},
                             "expression": {"type": "string"},
                             "quote": {"type": "string"},
                             "lead": {"type": "boolean"}},
                             "required": ["id", "value"]}},
                         "text": {"type": "string"},
                         "unknowns": {"type": "array", "items": {"type": "string"}}},
                      "required": ["claims"]}),
    "abstain": ("abstain", "Abstain: no record the tools returned answers any part.", _REASON),
    "escalate": ("escalate", "Escalate: a person must decide.", _REASON),
}

MESSAGES_RULES = (
    "You decide through tool calls, never by a text reply. Read with the plx_ tools (plx_find "
    "is the Workbench tool plx.find, and so on). The rules below write each decision as a "
    'JSON object; here each is a tool call instead: "call" is one or more plx_ tool calls, '
    '"answer" is final_answer with the claims and unknowns, "abstain" and "escalate" are '
    "those tools with a reason. Each user turn ends with what remains of your budget, and "
    'with "repair" when your last decision was not accepted.\n\n')


class PlannerRefused(Exception):
    """A Messages planner that cannot be built: ``code`` (``credentials_absent``,
    ``model_setting_absent``) and the ``settings`` (names) that would supply it."""

    def __init__(self, code: str, settings: Sequence[str]) -> None:
        super().__init__(code)
        self.code, self.settings = code, list(settings)

    def as_dict(self) -> dict[str, Any]:
        return {"refused": self.code, "settings": self.settings}


class TransportError(Exception):
    """A request the transport did not complete: ``status`` (an HTTP status, or None)."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise TransportError("redirect_refused", args[2])


def post_messages(url: str, headers: Mapping[str, str], body: bytes,
                  timeout: float) -> Iterable[bytes]:
    """The one sender: a POST of ``body`` to ``API_URL``, redirects refused; yields the
    response's lines (server-sent events). An HTTP error is a ``TransportError``."""
    if url != API_URL:
        raise TransportError("url_not_allowed")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        response = urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise TransportError(f"http_{status}", status) from None
    with response:
        yield from response


def _api_tools(catalog: Sequence[Mapping[str, Any]]) -> tuple[list[dict], dict[str, str]]:
    """The Messages API tools of the loop's catalog and the decision tools, and each read
    tool's API name -> Workbench name. A name that does not fit or collides is dropped."""
    names: dict[str, str] = {}
    tools: list[dict[str, Any]] = []
    for tool in catalog:
        name = tool.get("name") if isinstance(tool, Mapping) else None
        api = name.replace(".", "_") if isinstance(name, str) else ""
        if not TOOL_NAME.fullmatch(api) or api in names or api in DECISION_TOOLS:
            continue
        names[api] = name
        schema = tool.get("inputSchema")
        tools.append({"name": api, "description": str(tool.get("description") or "")[:4000],
                      "input_schema": schema if isinstance(schema, Mapping)
                      else {"type": "object"}})
    tools += [{"name": api, "description": text, "input_schema": schema}
              for api, (_, text, schema) in DECISION_TOOLS.items()]
    tools[-1] = {**tools[-1], "cache_control": EPHEMERAL}
    return tools, names


def _text(value: Any) -> dict[str, Any]:
    return {"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}


def _user(turns: list[dict[str, Any]], *blocks: dict[str, Any]) -> None:
    if turns and turns[-1]["role"] == "user":
        turns[-1]["content"].extend(blocks)
    else:
        turns.append({"role": "user", "content": list(blocks)})


def conversation(request: Mapping[str, Any], names: Mapping[str, str]) -> list[dict]:
    """The messages of one loop request (module doc, "Conversation")."""
    api_of = {name: api for api, name in names.items()}
    turns: list[dict[str, Any]] = [{"role": "user", "content": [
        _text({"question": request.get("question")})]}]
    for entry in request.get("transcript") or []:
        step = entry.get("step")
        if entry.get("planner_reply_invalid"):
            _user(turns, _text({"step": step, "your_reply": "not usable: call a tool"}))
            continue
        uses, results, notes = [], [], []
        for index, observation in enumerate(entry.get("observations") or []):
            api = api_of.get(observation.get("tool"))
            if api is None:
                notes.append(_text({"step": step, **observation}))
                continue
            use_id = f"toolu_{step}_{index}"
            arguments = observation.get("arguments")
            uses.append({"type": "tool_use", "id": use_id, "name": api,
                         "input": arguments if isinstance(arguments, dict) else {}})
            content = observation["result"] if "result" in observation else {
                k: v for k, v in observation.items() if k not in ("tool", "arguments")}
            results.append({"type": "tool_result", "tool_use_id": use_id,
                            "content": json.dumps(content, ensure_ascii=False, default=str),
                            **({"is_error": True} if observation.get("is_error")
                               or "refused" in observation else {})})
        if uses:
            turns.append({"role": "assistant", "content": uses})
            turns.append({"role": "user", "content": results + notes})
        elif notes:
            _user(turns, *notes)
    last = turns[-1]["content"]
    last[-1] = {**last[-1], "cache_control": EPHEMERAL}
    status: dict[str, Any] = {"remaining": request.get("remaining")}
    if "repair" in request:
        status["repair"] = request["repair"]
    _user(turns, _text(status))
    return turns


class _Stream:
    """One streamed reply: its content blocks, usage, stop reason and whether it ended."""

    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, int] = {}
        self.stopped = False
        self.error: str | None = None

    def event(self, data: Mapping[str, Any]) -> None:
        kind = data.get("type")
        if kind == "message_start":
            self._usage((data.get("message") or {}).get("usage"))
        elif kind == "content_block_start":
            block = data.get("content_block") or {}
            self.blocks[data.get("index")] = {"type": block.get("type"),
                                              "name": block.get("name"),
                                              "text": block.get("text") or "", "json": ""}
        elif kind == "content_block_delta":
            block, delta = self.blocks.get(data.get("index")), data.get("delta") or {}
            if block is not None and delta.get("type") == "text_delta":
                block["text"] += str(delta.get("text") or "")
            elif block is not None and delta.get("type") == "input_json_delta":
                block["json"] += str(delta.get("partial_json") or "")
        elif kind == "message_delta":
            self._usage(data.get("usage"))
        elif kind == "message_stop":
            self.stopped = True
        elif kind == "error":
            self.error = str((data.get("error") or {}).get("type") or "error")[:60]

    def _usage(self, usage: Any) -> None:
        for key in TOKEN_KEYS:
            value = (usage or {}).get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.usage[key] = value


def _decision(stream: _Stream, names: Mapping[str, str]) -> Any:
    """The loop decision of a complete reply (module doc, "Tools"), or None."""
    calls, final, text = [], None, ""
    for _, block in sorted(stream.blocks.items(), key=lambda item: (not isinstance(item[0], int), item[0] if isinstance(item[0], int) else 0)):
        if block["type"] == "text":
            text += block["text"]
        if block["type"] != "tool_use":
            continue
        try:
            arguments = json.loads(block["json"]) if block["json"].strip() else {}
        except ValueError:
            continue
        if not isinstance(arguments, dict):
            continue
        if block["name"] in DECISION_TOOLS:
            if final is None:
                action = DECISION_TOOLS[block["name"]][0]
                final = {"action": action, **({k: arguments[k] for k in ("claims", "text", "unknowns")
                                                if k in arguments} if action == "answer"
                                               else {"reason": arguments.get("reason")})}
        else:
            calls.append({"tool": names.get(block["name"], block["name"]),
                          "arguments": arguments})
    if calls:
        return {"action": "call", "calls": calls}
    if final is not None:
        return final
    match = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(match.group(0)) if match else None
    except ValueError:
        return None


class MessagesPlanner:
    """The next decision from the Messages API (module doc). ``transport`` (tests: a fake)
    replaces ``post_messages``; ``clock`` the monotonic clock. ``timeout`` (seconds) bounds a
    whole request, the stream included; the Ask's eval lowers it to the session's time."""

    def __init__(self, *, key: str | None, model: str | None,
                 transport: Callable[[str, Mapping[str, str], bytes, float],
                                     Iterable[Any]] | None = None,
                 timeout: int = 120, max_output_tokens: int = MAX_OUTPUT_TOKENS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not key:
            raise PlannerRefused("credentials_absent", KEY_SETTINGS)
        if not model:
            raise PlannerRefused("model_setting_absent", (MODEL_SETTING,))
        self._key, self.model, self.timeout = key, model, timeout
        self.max_output_tokens, self.clock = max_output_tokens, clock
        self.transport = transport or post_messages
        self.metrics: list[dict[str, Any]] = []

    @classmethod
    def from_settings(cls, environ: Mapping[str, str], *, model: str | None = None,
                      **options: Any) -> "MessagesPlanner":
        key = next((environ[name] for name in KEY_SETTINGS if environ.get(name)), None)
        return cls(key=key, model=model or environ.get(MODEL_SETTING), **options)

    def __repr__(self) -> str:  # never the key
        return f"MessagesPlanner(model={self.model!r})"

    def body(self, request: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        """The request body for one loop request, and its tool names (API -> Workbench)."""
        tools, names = _api_tools(request.get("tools") or [])
        return {"model": self.model, "max_tokens": self.max_output_tokens, "stream": True,
                "system": [{"type": "text", "text": MESSAGES_RULES + PLANNER_RULES,
                            "cache_control": EPHEMERAL}],
                "tools": tools, "tool_choice": {"type": "any"},
                "messages": conversation(request, names)}, names

    def __call__(self, request: Mapping[str, Any]) -> tuple[Any, int | None]:
        body, names = self.body(request)
        headers = {"x-api-key": self._key, "anthropic-version": API_VERSION,
                   "content-type": "application/json", "accept": "text/event-stream"}
        start = self.clock()
        metric: dict[str, Any] = {"outcome": "ok", "latency_ms": None, "first_event_ms": None}
        stream = _Stream()
        try:
            self._read(self.transport(API_URL, headers, json.dumps(body).encode("utf-8"),
                                      self.timeout), stream, start, metric)
        except TransportError as error:
            metric["outcome"] = str(error)
            if error.status in REFUSING_STATUSES:
                return self._done(metric, start, stream, {
                    "action": "escalate",
                    "reason": f"planner_process_failed:http_{error.status}"})
            return self._done(metric, start, stream, None)
        except (OSError, ValueError) as error:  # transport, timeout, undecodable stream
            metric["outcome"] = f"transport_{type(error).__name__}"[:60]
            return self._done(metric, start, stream, None)
        if stream.error or not stream.stopped:
            if stream.error:
                metric["outcome"] = f"stream_error_{stream.error}"
            elif metric["outcome"] == "ok":
                metric["outcome"] = "stream_incomplete"
            return self._done(metric, start, stream, None)
        return self._done(metric, start, stream, _decision(stream, names))

    def _read(self, lines: Iterable[Any], stream: _Stream, start: float,
              metric: dict[str, Any]) -> None:
        size, data = 0, []
        for raw in lines:
            line = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            size += len(line)
            if size > MAX_STREAM_BYTES or self.clock() - start > self.timeout:
                metric["outcome"] = "stream_too_large" if size > MAX_STREAM_BYTES \
                    else "stream_timeout"
                return
            if metric["first_event_ms"] is None:
                metric["first_event_ms"] = int((self.clock() - start) * 1000)
            line = line.rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                event = json.loads("\n".join(data))
                data = []
                if isinstance(event, Mapping):
                    stream.event(event)
        if data:
            event = json.loads("\n".join(data))
            if isinstance(event, Mapping):
                stream.event(event)

    def _done(self, metric: dict[str, Any], start: float, stream: _Stream,
              decision: Any) -> tuple[Any, int | None]:
        metric["latency_ms"] = int((self.clock() - start) * 1000)
        if decision is None and metric["outcome"] == "ok":
            metric["outcome"] = "reply_unusable"
        metric.update({key: stream.usage.get(key, 0) for key in TOKEN_KEYS})
        self.metrics.append(metric)
        tokens = sum(stream.usage.values()) if stream.usage else None
        return decision, tokens


def ask_planner(environ: Mapping[str, str], *, claude_code: Callable[[], Any] | None = None,
                **options: Any) -> tuple[Any, dict[str, Any]]:
    """The Ask's planner by ``PLANNER_SETTING`` and the selection made: ``claude-code``
    (default) or ``messages``; a refused Messages planner falls back to
    ``ClaudeCodePlanner`` (``claude_code`` builds it), its refusal in the selection. Any
    other setting value is refused (ValueError), never guessed."""
    from .agent_loop import ClaudeCodePlanner
    choice = (environ.get(PLANNER_SETTING) or "claude-code").strip()
    if choice not in PLANNERS:
        raise ValueError(f"{PLANNER_SETTING.lower()}_invalid")
    fallback = claude_code or ClaudeCodePlanner
    if choice == "claude-code":
        return fallback(), {"planner": "claude-code"}
    try:
        return MessagesPlanner.from_settings(environ, **options), {"planner": "messages"}
    except PlannerRefused as refused:
        return fallback(), {"planner": "claude-code", "fallback_from": "messages",
                            **refused.as_dict()}


def prices(environ: Mapping[str, str]) -> tuple[dict[str, Any] | None, str]:
    """The per-million-token prices of ``PRICES_SETTING`` (Decimal) and the cost basis:
    ``price_setting``, ``price_setting_absent`` or ``price_setting_invalid`` (no cost)."""
    raw = environ.get(PRICES_SETTING)
    if not raw:
        return None, "price_setting_absent"
    try:
        value = json.loads(raw)
        table = {key: Decimal(str(value[key])) for key in PRICE_KEYS.values()}
        currency = value["currency"]
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return None, "price_setting_invalid"
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency) \
            or any(not price.is_finite() or price < 0 for price in table.values()):
        return None, "price_setting_invalid"
    return {**table, "currency": currency}, "price_setting"


def ask_usage(metrics: Sequence[Mapping[str, Any]],
              table: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One Ask's requests: their count, failures, latency (summed, ms), each request's
    latency and time to the first event, token counts, and its cost (a Decimal string in
    ``table``'s currency; None without prices)."""
    tokens = {key: sum(int(m.get(key) or 0) for m in metrics) for key in TOKEN_KEYS}
    cost = None
    if table is not None:
        cost = _money(sum((Decimal(tokens[key]) * table[price] / Decimal(1_000_000)
                           for key, price in PRICE_KEYS.items()), Decimal(0)))
    return {"requests": len(metrics),
            "failed_requests": sum(m.get("outcome") != "ok" for m in metrics),
            "latency_ms": sum(int(m.get("latency_ms") or 0) for m in metrics),
            "request_latency_ms": [int(m.get("latency_ms") or 0) for m in metrics],
            "first_event_ms": [m["first_event_ms"] for m in metrics
                               if isinstance(m.get("first_event_ms"), int)],
            "tokens": tokens, "cost": cost,
            "currency": table["currency"] if table is not None else None}


def _money(value: Decimal) -> str:
    """A Decimal amount as a plain string, trailing zeros dropped (never an exponent)."""
    return format(value.normalize(), "f") if value else "0"


def _rank(values: Sequence[int], share: float) -> int | None:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * share) - 1)] if ordered else None


def usage_summary(asks: Sequence[Mapping[str, Any]], basis: str) -> dict[str, Any]:
    """A bench's Asks (``ask_usage`` each): latency per Ask (mean, p50, max) and per request
    (p50, p95, first event p50), tokens summed and per Ask, the cache-read share of the
    input, and cost (total and per Ask, Decimal strings) when every Ask has one."""
    per_ask = [int(a["latency_ms"]) for a in asks]
    requests = [ms for a in asks for ms in a["request_latency_ms"]]
    tokens = {key: sum(a["tokens"][key] for a in asks) for key in TOKEN_KEYS}
    read = sum(tokens[key] for key in TOKEN_KEYS[:3])
    costs = [a["cost"] for a in asks]
    currencies = {a["currency"] for a in asks}
    total = sum((Decimal(c) for c in costs), Decimal(0)) \
        if asks and None not in costs and len(currencies) == 1 else None
    return {"asks": len(asks), "requests": len(requests),
            "failed_requests": sum(a["failed_requests"] for a in asks),
            "latency_ms": {"per_ask_mean": sum(per_ask) // len(per_ask) if per_ask else None,
                           "per_ask_p50": _rank(per_ask, 0.5),
                           "per_ask_max": max(per_ask) if per_ask else None,
                           "request_p50": _rank(requests, 0.5),
                           "request_p95": _rank(requests, 0.95),
                           "first_event_p50": _rank(
                               [ms for a in asks for ms in a["first_event_ms"]], 0.5)},
            "tokens": tokens,
            "tokens_per_ask_mean": sum(tokens.values()) // len(asks) if asks else None,
            "cache_read_share": round(tokens["cache_read_input_tokens"] / read, 4)
            if read else None,
            "cost": None if total is None else {
                "currency": next(iter(currencies)), "total": _money(total),
                "per_ask_mean": _money(total / len(asks))},
            "cost_basis": basis}


__all__ = ["API_URL", "KEY_SETTINGS", "MODEL_SETTING", "MessagesPlanner", "PLANNER_SETTING",
           "PRICES_SETTING", "PlannerRefused", "TransportError", "ask_planner", "ask_usage",
           "conversation", "post_messages", "prices", "usage_summary"]
