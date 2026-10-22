"""Explicit agent definitions (AGT-06): what each Workbench agent may do, and its run.

The Workbench's agents are data (``operating/agent_definitions.json``, format
``operating/agent_definitions.schema.json``, validated with ``ontology/json_contract.py``;
an unknown field anywhere fails closed). Each definition names its role and
responsibilities, the tools it may call (by name), the action types of
``operating/action_catalog.json`` it may propose or run, the citation schemes it may read
(``data_scopes``), its limits, when it stops and to whom it escalates, and what it is
forbidden to do. ``load`` cross-checks a document against the action catalog: every
action type declared, every approval rule of those types mapped to an escalation target,
every limit a stop condition, the never-allowed operations (``NEVER``: approve, dispatch,
MC writes, business writes) forbidden, and nothing forbidden also allowed.

``data_scopes`` must be every scope (``SCOPE_SCHEMES``): a narrower definition is refused
(``AgentDefinition`` itself refuses it, so neither the loader nor a constructed definition
can carry one). Narrowing is not enforced today: the ontology projection
(``plx.find``/``plx.object``/``plx.around``/``plx.documents``...), the case records, the
mail and SharePoint captures and ``source_reads`` serve rows of every scheme, so a narrower
definition would be advertised as enforced while its tools still read outside it.

``AgentRun`` is one session's run of one definition, the one place it is enforced
(``operating.mcp_server.MCPServer`` consults it on every tool call):

* a tool outside ``tools`` and an action type outside ``action_types`` are refused before
  the tool is called, with the reason recorded;
* the run stops when a limit is reached (``max_tool_calls`` client calls, or
  ``max_consecutive_errors`` failed calls in a row): the reason is recorded, the stop is
  escalated to ``stop.escalate_to`` and every later call is refused;
* an action that needs approval is never performed by the agent: the action path parks it
  (a registered intent waiting for the gate) and the run records an escalation to the
  definition's target for that approval rule (``mc_approval``: a human MC admin/owner;
  ``human``). An agent is never an escalation target, so it never approves itself;
* ``meta()`` is the effective definition's stamp (id, version, digest, limits) with the
  run's counts and state; every answer of the session carries it (``_meta`` key
  ``plx/agent``), so an answer says which agent with which limits produced it.

Host code (the in-process deterministic drivers) is held to the allow list but not
counted against the limits: it is not the agent.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from ontology.json_contract import validate

SCHEMA = "plx-agent-definitions/v1"
DEFINITIONS_PATH = Path(__file__).with_name("agent_definitions.json")
FORMAT_PATH = Path(__file__).with_name("agent_definitions.schema.json")
CATALOG_PATH = Path(__file__).with_name("action_catalog.json")
# Citation schemes per data scope (operating.case_runtime.SUPPORTED_SCHEMES).
SCOPE_SCHEMES = {"api": "api://", "original": "original://", "graph-mail": "graph-mail://",
                 "sharepoint": "sharepoint://", "bc": "bc://"}
# Operations no agent may ever perform: every definition must forbid them.
NEVER = frozenset({"approve", "dispatch", "mc_write", "business_write"})
# The tool that runs plans and the primitive it resolves roots with (besides each plan's
# step tools): an agent serving plx.investigate must be allowed every tool a plan calls.
INVESTIGATE_TOOL, ROOT_LOOKUP_TOOL = "plx.investigate", "plx.find"
TRACE_MAX = 200


class AgentDefinitionError(ValueError):
    """A definitions document, or a definition for this host, that cannot be enforced."""


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    version: int
    role: str
    responsibilities: tuple[str, ...]
    tools: frozenset[str]
    action_types: frozenset[str]
    data_scopes: frozenset[str]
    limits: Mapping[str, int]
    stop_when: frozenset[str]
    stop_escalate_to: str
    escalation: Mapping[str, str]
    forbidden: frozenset[str]
    digest: str

    def __post_init__(self) -> None:
        # Fail closed: data-scope narrowing has no enforcement on every read path yet.
        if self.data_scopes != frozenset(SCOPE_SCHEMES):
            raise AgentDefinitionError(
                f"{self.id}: data_scopes must be every scope {sorted(SCOPE_SCHEMES)}: "
                "narrowing is not enforced on every read path")

    def stamp(self) -> dict[str, Any]:
        """The effective definition as every answer names it."""
        return {"id": self.id, "version": self.version, "digest": self.digest,
                "limits": dict(self.limits)}


@lru_cache(maxsize=1)
def _format() -> dict[str, Any]:
    return json.loads(FORMAT_PATH.read_text("utf-8"))


@lru_cache(maxsize=1)
def _approval_rules() -> dict[str, str]:
    """Action type -> its catalog approval rule (``approval.required``)."""
    actions = json.loads(CATALOG_PATH.read_text("utf-8"))["actions"]
    return {name: entry["approval"]["required"] for name, entry in actions.items()}


def _definition(entry: Mapping[str, Any]) -> AgentDefinition:
    where = entry["id"]
    rules = _approval_rules()
    undeclared = sorted(set(entry["action_types"]) - set(rules))
    if undeclared:
        raise AgentDefinitionError(f"{where}: action types not in the catalog: {undeclared}")
    needed = {rules[name] for name in entry["action_types"]} - {"none"}
    unmapped = sorted(needed - set(entry["escalation"]))
    if unmapped:
        raise AgentDefinitionError(f"{where}: approval rules without escalation: {unmapped}")
    if set(entry["stop"]["when"]) != set(entry["limits"]):
        raise AgentDefinitionError(f"{where}: every limit is a stop condition and only those")
    forbidden = frozenset(entry["forbidden"])
    if NEVER - forbidden:
        raise AgentDefinitionError(f"{where}: must forbid {sorted(NEVER - forbidden)}")
    allowed_and_forbidden = forbidden & (set(entry["tools"]) | set(entry["action_types"]))
    if allowed_and_forbidden:
        raise AgentDefinitionError(f"{where}: allowed and forbidden: "
                                   f"{sorted(allowed_and_forbidden)}")
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return AgentDefinition(
        id=entry["id"], version=entry["version"], role=entry["role"],
        responsibilities=tuple(entry["responsibilities"]), tools=frozenset(entry["tools"]),
        action_types=frozenset(entry["action_types"]),
        data_scopes=frozenset(entry["data_scopes"]), limits=dict(entry["limits"]),
        stop_when=frozenset(entry["stop"]["when"]),
        stop_escalate_to=entry["stop"]["escalate_to"], escalation=dict(entry["escalation"]),
        forbidden=forbidden, digest="sha256:" + sha256(canonical.encode()).hexdigest()[:16])


@dataclass(frozen=True)
class AgentDefinitions:
    agents: Mapping[str, AgentDefinition]
    default: str

    def get(self, agent_id: str | None = None) -> AgentDefinition:
        definition = self.agents.get(self.default if agent_id is None else agent_id)
        if definition is None:
            raise AgentDefinitionError(f"unknown_agent:{str(agent_id)[:64]}")
        return definition


def load(document: Mapping[str, Any] | None = None) -> AgentDefinitions:
    """The definitions of ``document`` (default: the shipped file), validated."""
    if document is None:
        return _shipped()
    errors = validate(document, _format())
    if errors:
        raise AgentDefinitionError("agent_definitions_invalid: " + "; ".join(errors[:5]))
    agents: dict[str, AgentDefinition] = {}
    for entry in document["agents"]:
        if entry["id"] in agents:
            raise AgentDefinitionError(f"duplicate agent id {entry['id']}")
        agents[entry["id"]] = _definition(entry)
    if document["default_agent"] not in agents:
        raise AgentDefinitionError("default_agent is not defined")
    return AgentDefinitions(agents, document["default_agent"])


@lru_cache(maxsize=1)
def _shipped() -> AgentDefinitions:
    return load(json.loads(DEFINITIONS_PATH.read_text("utf-8")))


def document() -> dict[str, Any]:
    """A fresh copy of the shipped definitions document."""
    return json.loads(DEFINITIONS_PATH.read_text("utf-8"))


def approval_rule(action_type: str) -> str | None:
    """The catalog approval rule of an action type (None: undeclared)."""
    return _approval_rules().get(action_type)


class AgentRun:
    """One session's run of one definition on a host serving ``served`` tools, with
    ``plan_tools`` the tools plx.investigate's plans call."""

    def __init__(self, definition: AgentDefinition, *, served: frozenset[str],
                 plan_tools: frozenset[str]) -> None:
        if not isinstance(definition, AgentDefinition):
            raise AgentDefinitionError("agent_definition_required")
        unserved = sorted(definition.tools - served)
        if unserved:
            raise AgentDefinitionError(f"{definition.id}: tools this host does not serve: "
                                       f"{unserved}")
        if INVESTIGATE_TOOL in definition.tools:
            # A plan's primitives are called in-process: an agent running plans must be
            # allowed every one of them, or a plan would reach past its allow list.
            missing = sorted((plan_tools | {ROOT_LOOKUP_TOOL}) - definition.tools)
            if missing:
                raise AgentDefinitionError(f"{definition.id}: plx.investigate plans call "
                                           f"tools it is not allowed: {missing}")
        self.definition = definition
        self.tool_calls = 0
        self.consecutive_errors = 0
        self.refused = 0
        self.stop_reason: str | None = None
        self.escalations: list[dict[str, Any]] = []
        self.trace: deque[dict[str, Any]] = deque(maxlen=TRACE_MAX)

    def refusal(self, tool: str, action_type: str | None, *, host_code: bool) -> str | None:
        """Why this call may not run, decided before the tool is called (None: it may).
        A refusal is recorded in the trace."""
        definition = self.definition
        reason = None
        if self.stop_reason is not None and not host_code:
            reason = f"agent_stopped:{self.stop_reason}"
        elif tool not in definition.tools:
            reason = f"tool_not_allowed_for_agent:{definition.id}"
        elif action_type is not None and action_type not in definition.action_types:
            reason = f"action_type_not_allowed_for_agent:{definition.id}"
        if reason is not None:
            self.refused += 1
            self.trace.append({"tool": tool, "outcome": "refused", "reason": reason})
            if not host_code and self.stop_reason is None:
                self._failed()
        return reason

    def record(self, tool: str, *, error: bool, host_code: bool,
               escalation: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """Record one call that ran; returns the escalation it raised, if any."""
        self.trace.append({"tool": tool, "outcome": "error" if error else "ok"})
        raised = None
        if escalation is not None:
            raised = self._escalate(**escalation)
        if host_code:
            return raised
        self.tool_calls += 1
        if error:
            self._failed()
        else:
            self.consecutive_errors = 0
        if self.stop_reason is None and self.tool_calls >= self.definition.limits["max_tool_calls"]:
            self._stop("max_tool_calls")
        return raised

    def approval_escalation(self, rule: str, **detail: Any) -> dict[str, Any] | None:
        """The escalation an action needing ``rule`` raises: parked with the definition's
        target for that rule, never performed by the agent (None: no approval needed)."""
        if rule == "none":
            return None
        # Validated to cover every rule of the agent's action types; anything else goes
        # to a person.
        return {"on": f"approval:{rule}", "to": self.definition.escalation.get(rule, "human"),
                **detail}

    def _failed(self) -> None:
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.definition.limits["max_consecutive_errors"]:
            self._stop("max_consecutive_errors")

    def _stop(self, reason: str) -> None:
        self.stop_reason = reason
        self.trace.append({"outcome": "stopped", "reason": reason})
        self._escalate(on="stop", to=self.definition.stop_escalate_to, reason=reason)

    def _escalate(self, *, on: str, to: str, **detail: Any) -> dict[str, Any]:
        entry = {"on": on, "to": to, "state": "parked", **detail}
        self.escalations.append(entry)
        return entry

    def meta(self, escalation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The effective definition and this run's state, for an answer's ``_meta``."""
        out = {**self.definition.stamp(), "tool_calls": self.tool_calls,
               "refused": self.refused,
               "state": "stopped" if self.stop_reason else "running",
               "escalations": len(self.escalations)}
        if self.stop_reason is not None:
            out["stop_reason"] = self.stop_reason
        if escalation is not None:
            out["escalation"] = dict(escalation)
        return out


@lru_cache(maxsize=1)
def plan_tools() -> frozenset[str]:
    """Every tool the shipped plx.investigate plans call."""
    from .ontology_tools_investigate import PLAN_DIR

    tools: set[str] = set()
    for path in sorted(PLAN_DIR.glob("*.json")):
        if not path.name.startswith("_"):
            tools.update(step["tool"] for step in json.loads(path.read_text("utf-8"))["steps"])
    return frozenset(tools)


__all__ = ["AgentDefinition", "AgentDefinitionError", "AgentDefinitions", "AgentRun",
           "DEFINITIONS_PATH", "NEVER", "SCHEMA", "SCOPE_SCHEMES", "approval_rule", "document",
           "load", "plan_tools"]
