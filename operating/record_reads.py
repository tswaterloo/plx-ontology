"""Reads in code as the reader, with no model: the host's generic tools for one reader
(``Reads``: plx.find, plx.read, plx.object, under the employee gate and one projection
generation) and the one identifier rule (``by_identifier``: a record is the one whose own
identifier is the token; several are ``Choices``, never picked; a text match is never the
record). Kept from the retired fast-answer router (11-23): its live users are the Assist's
party addresses (``assist.party_addresses``), the form fill's records
(``doc_gather``) and the Assist's record tokens (``record_tokens``)."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import re
import time
from typing import Any, Iterator, Mapping, Optional, Sequence

FIND_LIMIT = 10

# An identifier-shaped token: letters, digits and joiners with at least one digit
# (``R-0162``, ``PO#4819``, ``S-ORD101085``, ``TASK-3383``, ``25A0165``).
_TOKEN = re.compile(r"(?<![\w#-])((?:[A-Za-z]{1,3}-)?[A-Za-z]{0,6}[#-]?[A-Za-z0-9]*\d[A-Za-z0-9#-]*)"
                    r"(?![\w#-])")


def record_tokens(text: str) -> list[str]:
    """The identifier-shaped tokens of ``text`` (3 characters or more, one digit at
    least), each once, in order; a date is a time frame, never a record."""
    out = []
    for found in _TOKEN.finditer(text or ""):
        token = found.group(1).strip("-#")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            continue
        if len(token) >= 3 and any(c.isdigit() for c in token) and token not in out:
            out.append(token)
    return out


def plain(value: Any) -> Any:
    """A tool value with its output envelope removed."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value["untrusted_text"]
    return value


class Unanswerable(Exception):
    """These reads cannot settle it (a code, never shown)."""


class Reads:
    """One reader's reads in code (no model): the host's generic tools for ``actor``
    (``host``: its ``CaseHostTools``; ``runtime``: its ``CaseRuntime``), served as
    ``MCPServer._generic_call`` serves them (the employee gate, the read policy decided
    for the reader, plx.read paged), all inside one ``host.request()`` (one projection
    generation); ``server``: the reader's MCP session, whose answer gate re-reads every
    cited record as the reader. Every call is a session event (as an agent transcript
    records it); ``seen`` and ``returned`` are what the gate binds claims to."""

    def __init__(self, host: Any, runtime: Any, actor: Any, server: Any) -> None:
        self.host, self.runtime, self.actor, self.server = host, runtime, actor, server
        self.events: list[dict[str, Any]] = []
        self.seen: dict[str, None] = {}
        self.returned: dict[str, Any] = {"counts": [], "currencies": {},
                                         "currency_citations": {}}
        self._gated: Any = None
        self._read: Any = None
        self.last_find: Mapping[str, Any] = {}
        # Monotonic second past which no further read starts: past it a read is
        # ``Unanswerable`` (``time_budget``), never a guess.
        self.deadline: Optional[float] = None

    @classmethod
    def of_server(cls, server: Any) -> "Reads":
        """The reads of an ``MCPServer`` session (its ontology host, runtime and actor)."""
        if server.ontology is None:
            raise Unanswerable("no_ontology_host")
        return cls(server.ontology, server.case_runtime, server.actor, server)

    @contextmanager
    def request(self) -> Iterator["Reads"]:
        with self.host.request():
            self._gated = self.runtime.employee_gate(self.actor)
            # One answer, one policy: decided once for its reads, as the console pins one
            # per request (``CaseHostTools.policy_pinned``), not again for every read.
            pinned = getattr(self.host, "policy_pinned", None)
            with pinned(self._gated) if callable(pinned) else nullcontext():
                yield self

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """``name`` called as the reader."""
        from . import answer_gate, mcp_server
        from .claim_checks import returned_evidence
        if self._gated is None:
            raise Unanswerable("outside_request")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise Unanswerable("time_budget")
        arguments = dict(arguments)
        started = time.monotonic()
        # In-process host code, as ``MCPServer.handle(host_code=True)``: a result is bounded
        # by its own size, never by the size its envelopes would add for a client (a plan's
        # answer is fitted unwrapped, so measured wrapped it was refused whole); the result
        # is still fire-walled below before it is recorded or bound as evidence.
        to_client = mcp_server._TO_CLIENT.set(False)
        # A store statement still running at the answer's deadline is interrupted
        # (``call_limits.sql_deadline``): one long statement never holds the answer
        # past its budget; whatever the call returns after the deadline is unread.
        from .call_limits import sql_deadline
        within = sql_deadline(self.deadline)
        within.__enter__()
        try:
            if name == "plx.read":
                from .case_tools_host import PagedRead
                tools = self.host.tools
                tools.policy = getattr(self.host, "policy_for", self.host.policy_now)(
                    self._gated)
                if self._read is None or self._read[0] is not tools:
                    self._read = (tools, PagedRead(self.host.evidence, self.runtime))
                result = self._read[1].call(arguments, self._gated)
            else:
                result = self.host.call(name, arguments, self._gated)
                if name == "plx.find":
                    result = self._home(arguments, result)
            error = not isinstance(result, Mapping) or "error" in result
        except Exception:  # noqa: BLE001 - a refused or failed read answers nothing
            result, error = {"error": {"code": "tool_execution_failed"}}, True
        finally:
            within.__exit__(None, None, None)
            mcp_server._TO_CLIENT.reset(to_client)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            # ran past the budget: a statement may have been interrupted and the result
            # read in part (a tool that degrades on a failed statement): never used
            self.events.append({"seq": len(self.events), "type": "tool_call", "name": name,
                                "arguments": arguments,
                                "result": {"error": {"code": "time_budget"}},
                                "is_error": True,
                                "elapsed_ms": int((time.monotonic() - started) * 1000)})
            raise Unanswerable("time_budget")
        shown = mcp_server._firewall(result, name) if not error else result
        self.events.append({"seq": len(self.events), "type": "tool_call", "name": name,
                            "arguments": arguments, "result": shown, "is_error": error,
                            "elapsed_ms": int((time.monotonic() - started) * 1000)})
        if error:
            raise Unanswerable(f"tool_refused:{name}")
        returned_evidence(name, arguments, result, self.returned)
        for handle in sorted(answer_gate.evidence_handles(name, shown, arguments=arguments)):
            self.seen[handle] = None
        return dict(result)

    def _home(self, arguments: Mapping[str, Any], found: Any) -> Any:
        """plx.find's home mode as the reader's MCP session serves it
        (``MCPServer._home_mode``: a home phrase that names no record answers with the
        reader's own gap inbox, Home's late list and its count, under this call's policy),
        detailed, so the session's text is never set from here. A session without it, or
        a query that is no home phrase: ``found`` unchanged."""
        from . import mcp_server
        mode = getattr(self.server, "_home_mode", None)
        if mode is None or mcp_server.home_kinds(arguments.get("query")) is None:
            return found
        return mode(self.host, self._gated, {**arguments, "response_format": "detailed"},
                    found)

    def find(self, query: str, object_type: Optional[str] = None,
             limit: int = FIND_LIMIT) -> dict[str, Any]:
        arguments: dict[str, Any] = {"query": query[:200], "limit": limit}
        if object_type:
            arguments["object_type"] = object_type
        self.last_find = self.call("plx.find", arguments)
        return self.last_find

    def object(self, object_id: str) -> dict[str, Any]:
        return self.call("plx.object", {"object_id": object_id})


@dataclass
class Choices:
    """Several records the question's name or number may mean: asked back, never picked."""
    mention: str
    candidates: list[dict[str, Any]]


def _norm_id(text: Any) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(plain(text) or "").upper())


def candidates(found: Mapping[str, Any], types: Sequence[str]) -> list[dict[str, Any]]:
    out = []
    for c in found.get("candidates") or ():
        obj = c.get("object") or {}
        if obj.get("object_type") in types and isinstance(obj.get("object_id"), str):
            out.append(c)
    return out


def _binding(candidate: Mapping[str, Any]) -> str:
    oid = str((candidate.get("object") or {}).get("object_id") or "")
    return oid.split(":", 2)[1] if oid.count(":") >= 2 else ""


def by_identifier(reads: Reads, token: str, types: Sequence[str],
                  binding: Optional[str] = None, found: Optional[Mapping[str, Any]] = None
                  ) -> dict[str, Any] | Choices:
    """The one record of ``types`` (of ``binding``'s system when given) whose own
    identifier is ``token`` (in ``found``, a ``plx.find`` already made, else one typed
    find); several such records are ``Choices``; none, or only lead (text) matches, is
    ``Unanswerable``."""
    if found is None:
        found = reads.find(token, types[0] if len(types) == 1 else None)
    want = _norm_id(token)
    exact = []
    for c in candidates(found, types):
        matched = c.get("matched") or {}
        said = _norm_id(matched.get("raw") or matched.get("normalized"))
        if c.get("tier") == "lead" and c.get("reason") not in ("exact_identifier",):
            # a text match is a lead: it is the record only when the cell it matched is the
            # record's own identifier field and reads exactly the token
            if matched.get("identifier_kind") != "display_text" or said != want \
                    or matched.get("property") not in ("task_id", "code", "batch_code",
                                                       "lot_number", "po_number",
                                                       "order_number"):
                continue
        elif said and said != want and _norm_id(matched.get("normalized")) != want:
            continue
        if binding and not _binding(c).startswith(binding):
            continue
        exact.append(c)
    if not exact:
        raise Unanswerable("not_resolved")
    ids = list(dict.fromkeys(c["object"]["object_id"] for c in exact))
    if len(ids) > 1:
        return Choices(token, [{"object_id": c["object"]["object_id"],
                                "object_type": c["object"]["object_type"],
                                "display": c["object"].get("display")} for c in exact])
    return exact[0]
