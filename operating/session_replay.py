"""Ask sessions kept with their case, and replayed over the same evidence (AGT-02).

The episodes and context-packets modules are gone; this keeps what they were for (what an
agent session read and cited, and whether it still holds) in the case store, as case
events, and replays it with the agent loop's own client. No registry or store of its own.

* ``record(cases, actor, case_id, loop_result, anchors=None)``: one ``session.recorded`` event on the
  case (``operating.cases.CaseStore``: append-only, hash-chained, refused on a closed case
  or while another holder's claim is active) from an ``operating.agent_loop.run`` result
  (the Ask's ``loop``): the question, the stop, every tool call as the loop recorded it
  (step, tool, arguments, error, ``evidence``: the L1 handles the result returned as
  evidence) and each handle the kept claims cite with the case store's digest of what it
  resolves to for ``actor`` (``citations``, as a finding records them). Tool results are
  not stored: a result's envelope is per-call (``meta.as_known_at``, a fresh result-set
  id), its evidence is not. ``anchors``: the records of the object the question names (the
  Ask frames the question with its object id). A cited handle the store cannot resolve for
  the recorder refuses the record.
* ``sessions(cases, actor, case_id)``: the case's sessions as ``actor`` may see them. The
  case's own visibility applies (an unseen case is not found). A session is a withheld
  marker (``WITHHELD``: no question, object id, call, argument or citation), as the case
  store withholds a gap or proposal, unless every record it touched resolves for
  ``actor`` now: each it cites, each its calls returned as evidence and each anchor. A
  session that touched no record, or names an object without anchors, has nothing to gate
  on and is shown only to the reader who recorded it (fail closed). Arguments are never
  listed.
* ``replay(cases, server, case_id, session_id)``: the session's calls run again, in order,
  through ``server`` (the replaying reader's ``MCPServer``: its employee gate on every call,
  its output boundary), only tools it lists as generic and read-only
  (``agent_loop.catalog``); any other is not run (``tool_not_replayable``). Nothing is
  written. Each call's evidence and error state are compared with the record
  (``call_evidence_changed``, ``call_error_changed``) and each citation's digest with
  what it resolves to now (``citation_content_changed``). A session withheld from the
  reader is not replayed at all: no call runs. A case whose event chain is broken is not
  replayed (``CaseIntegrityError``). ``identical`` is True only when nothing diverged.

The case store keeps and reads the events (``CaseStore.record_session``,
``CaseStore.session_records``: public, so a case runtime's employee gate wraps them like
every other case read and write); this module shapes what is kept and decides what a reader
is shown.

The console lists and replays them through the object workspace's case path
(``ObjectWorkspace.case_records`` / ``replay``).

Evidence: isolated-fixture (``tests/test_session_replay.py``: the synthetic fixture world,
fixture employees, a scripted planner standing in for the model); the live check
``devloop/live-checks/session.replay.json`` (``scripts.object_workspace_probe
--session-replay``, a deterministic planner) is injected-transport only and
live-unverified until the verifier records a result.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ontology.core import Actor, ValidationError
from . import agent_loop
from .cases import (CITATION_CONTENT_CHANGED, SESSION_KIND, WITHHELD as WITHHELD_CITATION,
                    CaseIntegrityError, CaseStore, actor_label)

FORMAT = "plx-ask-session/v1"
WITHHELD = WITHHELD_CITATION
MAX_CALLS = 64  # agent_loop.Bounds caps max_tool_calls at 64
_CALL_KEYS = frozenset({"step", "tool", "arguments", "is_error", "evidence"})


def _calls(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The loop's tool calls, checked (an unknown shape fails closed)."""
    calls = result.get("tool_calls")
    if not isinstance(calls, list) or len(calls) > MAX_CALLS:
        raise ValidationError("session_tool_calls_invalid")
    out = []
    for call in calls:
        if (not isinstance(call, Mapping) or set(call) != _CALL_KEYS
                or type(call["step"]) is not int or not isinstance(call["tool"], str)
                or not isinstance(call["arguments"], dict)
                or not isinstance(call["is_error"], bool)
                or not isinstance(call["evidence"], list)
                or not all(isinstance(h, str) for h in call["evidence"])):
            raise ValidationError("session_tool_call_invalid")
        out.append({key: call[key] for key in sorted(_CALL_KEYS)})
    return out


def record(cases: CaseStore, actor: Actor, case_id: str, loop_result: Mapping[str, Any],
           anchors: Sequence[str] | None = None) -> dict[str, Any]:
    """Keep one Ask session (an ``agent_loop.run`` result) on case ``case_id``; the receipt
    names its ``session_id``. ``anchors``: the handles of the object in view (None: the
    question names no object)."""
    if not isinstance(loop_result, Mapping) \
            or loop_result.get("outcome") not in agent_loop.OUTCOMES \
            or not isinstance(loop_result.get("question"), str) \
            or not isinstance(loop_result.get("claims"), list):
        raise ValidationError("session_result_invalid")
    reason = loop_result.get("reason")
    calls = _calls(loop_result)
    cited = [handle for claim in loop_result["claims"]
             for handle in (claim.get("citations") or [])]
    gated = [*(anchors or ()), *(h for call in calls for h in call["evidence"])]
    return cases.record_session(actor, case_id, {
        "format": FORMAT, "question": loop_result["question"],
        "outcome": loop_result["outcome"],
        "reason": reason if isinstance(reason, str) else None,
        "calls": calls, "cited": cited, "gated": gated,
        # Nothing to gate the question on: shown to its recorder only.
        "recorder_only": (anchors is not None and not anchors) or not (cited or gated)})


def _withheld(session: Mapping[str, Any]) -> bool:
    if session.get("recorder_only", True) and not session["by_reader"]:
        return True
    return not session["gated_readable"] \
        or any(digest is None for digest in session["current"].values())


def sessions(cases: CaseStore, actor: Actor, case_id: str) -> list[dict[str, Any]]:
    """The case's recorded sessions as ``actor`` may see them (module doc)."""
    out = []
    for session in cases.session_records(actor, case_id)[1]:
        head = {"session_id": session["session_id"], "seq": session["seq"]}
        if _withheld(session):
            out.append({**head, "withheld": WITHHELD})
            continue
        out.append({**head, "at": session["at"], "recorded_by": session["recorded_by"],
                    "question": session["question"], "outcome": session["outcome"],
                    "reason": session["reason"],
                    "calls": [{key: call[key] for key in ("step", "tool", "is_error")}
                              for call in session["calls"]],
                    "citations": [{"handle": cite["handle"], "status": "unchanged"
                                   if session["current"][cite["handle"]] == cite["digest"]
                                   else CITATION_CONTENT_CHANGED}
                                  for cite in session["citations"]]})
    return out


def replay(cases: CaseStore, server: Any, case_id: str, session_id: str) -> dict[str, Any]:
    """Run session ``session_id`` of case ``case_id`` again for ``server``'s reader and
    report every divergence from its record (module doc)."""
    actor = server.actor
    chain_ok, recorded = cases.session_records(actor, case_id)
    session = next((item for item in recorded if item["session_id"] == session_id), None)
    if session is None:
        raise KeyError("session_not_found")
    if not chain_ok:
        raise CaseIntegrityError("case_event_chain_broken")
    if _withheld(session):
        return {"session_id": session_id, "replayed": False, "withheld": WITHHELD}
    allowed = {tool["name"] for tool in agent_loop.catalog(server)}
    client = agent_loop._Session(server)
    divergences: list[dict[str, Any]] = []
    for seq, call in enumerate(session["calls"]):
        where = {"seq": seq, "step": call["step"], "tool": call["tool"]}
        if call["tool"] not in allowed:
            divergences.append({**where, "kind": "tool_not_replayable"})
            continue
        result, is_error = client.call(call["tool"], dict(call["arguments"]))
        if is_error != call["is_error"]:
            divergences.append({**where, "kind": "call_error_changed"})
        elif sorted(() if is_error else agent_loop.evidence_handles(
                call["tool"], result, arguments=call["arguments"])) != call["evidence"]:
            divergences.append({**where, "kind": "call_evidence_changed"})
    divergences += [{"handle": cite["handle"], "kind": CITATION_CONTENT_CHANGED}
                    for cite in session["citations"]
                    if session["current"][cite["handle"]] != cite["digest"]]
    return {"session_id": session_id, "replayed": True, "replayed_by": actor_label(actor),
            "calls": len(session["calls"]), "identical": not divergences,
            "divergences": divergences}


__all__ = ["FORMAT", "SESSION_KIND", "WITHHELD", "record", "replay", "sessions"]
