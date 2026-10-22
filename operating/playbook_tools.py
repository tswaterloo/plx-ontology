"""MCP tools of the one playbook adoption path (RSN-08, ENG-13): propose, evaluate, adopt.

``plx.playbook_propose``, ``plx.playbook_evaluate`` and ``plx.playbook_adopt`` are the product
callers of ``operating.semantic_proposals`` through ``playbook_eval.proposal_store``, whose
records live in the change chain of the state directory's ``playbook-reports.sqlite``
(``PlaybookReportStore``, the case console's report store). Their I/O is
``operating/ontology_tool_contracts.json``; ``operating.mcp_server`` serves them to a session
running ``plx.agent.playbook`` (``operating/agent_definitions.json``), so the default agents'
listing keeps its 22-tool budget. Every call acts as the session actor through the existing
gates; the tools add no authority of their own:

* **Propose** (any principal, an agent included). Every evidence item must be a case id or a
  citation the proposer can read now (``CaseStore.verify_chain`` / ``CaseRuntime.
  resolve_citation``; else ``evidence_not_readable``), a case's change chain intact (else
  ``chain_broken``), then ``ProposalStore.propose`` keeps
  its own rules (non-empty evidence and rationale, a later version of a current playbook that
  loads, never a published file). The answer names no frozen case.
* **Evaluate** (a session holding a ``service:`` or ``employee:`` principal and no agent
  principal; the store refuses one sharing a principal with the proposer). The registered
  runner (``playbook_eval.PROPOSAL_RUNNER``) scores every frozen case before and after the edit,
  reading each case as this session (``reads``: plx.case_get and plx.source_citation under the
  session's own agent definition), and stores each report as the evaluator's, which the case
  audience gate refuses for a case the evaluator may not read: then no comparison is recorded
  and nothing about the case is answered. A case the evaluator reads only in part (an item
  withheld from it, a citation it cannot re-read) is answered ``unscored`` and the comparison's
  ``verdict`` is ``unscored``: never non-regressing, so adoption refuses it
  (``comparison_unscored``) until a reader of every frozen case evaluates again. A case NO
  reader of the host can read in full (``CaseRuntime.hidden_from_every_reader``: a source not
  configured, a rebuilt mirror) is answered ``unscorable`` with those reasons and left out of the
  comparison; when no frozen case is scorable, or once any is left out fewer than half are
  scored (``semantic_proposals.MIN_SCORED_SHARE``), the ``verdict`` is ``unscorable`` and
  adoption refuses (``no_scorable_case``, ``too_few_scored_cases``). A comparison recorded before the full-case reader rule is
  refused (``comparison_predates_full_read_rule``): evaluate the edit again.
* **Adopt** (one human steward). Only a session holding exactly one ``employee:`` principal that
  the host's configuration names a steward, whose client declared elicitation, is asked: the
  Workbench re-derives every adoption rule (``ProposalStore.check_adoption``) and sends the
  person an MCP ``elicitation/create`` form: a Workbench confirmation form
  (``operating.confirmation_forms``, purpose ``playbook_adoption``: the one form id, TTL,
  single-use answer and store the document-fields confirmation uses), whose id never appears in
  a tool result and which is answerable once, by that steward, before it expires. It is kept in
  the host's state directory, so the steward can answer it after a host restart. On ``accept`` with
  the confirm tick the rules are re-derived again (the comparison and content the form named
  must still hold, else ``proposal_changed_since_request``) and ``ProposalStore.adopt`` applies
  it (decision, atomic move into the overlay of adopted versions under the host's state
  directory, never the shipped ``operating/playbooks``; confirmation). Both records carry the
  steward, the form id (spent once answered) and the session client's declared ``clientInfo``.
  Mode ``status`` reads the proposal's state and the last answer to a form that asked this
  session's principal; an adoption whose move failed (``adoption_not_completed``) stays
  ``adoption_pending`` and a steward's next form completes it; one that failed before anything
  was recorded (the overlay folder could not be created: ``adoption_not_recorded``) changed
  nothing, stays ``evaluated`` and a new request retries it.

Host configuration (``load_config``, ``plx-playbook-proposals/v1``): the stewards (employee
principals) and the frozen case ids. Without it the tools refuse
(``playbook_proposals_not_configured``). Refusals leave as their code only.

Known limits: as ``kind_confirmation``, the Workbench cannot prove a person answered the form: the
form travels over the same MCP transport as the tool call, so an agent client running under a
steward's identity can answer it itself (self-consent). That risk is ACCEPTED
(``docs/decisions/2026-10-08-playbook-adopt-overlay.md``): the records name the
steward, form id and declared client so an adoption is attributable, and a human-authenticated
console adopt is the follow-up surface. An investigating agent's own session
(``plx.agent.investigate``/``plx.agent.act``) cannot propose, it runs ``plx.agent.playbook``;
nothing proposes or evaluates on a schedule (ENG-13: adoption is steward-only by design). Evidence
label: isolated-fixture / injected-transport, **live-unverified** (tests drive a simulated MCP
client over fixture cases).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from ontology.json_contract import validate

from .confirmation_forms import ConfirmationForms, FormRefused
from .kind_confirmation import client_supports_elicitation, is_agent_principal
from .playbook_eval import PROPOSAL_RUNNER, EvaluationError
from .semantic_proposals import ProposalRefused, ProposalStore, scored_share_refusal

PROPOSE, EVALUATE, ADOPT = "plx.playbook_propose", "plx.playbook_evaluate", "plx.playbook_adopt"
TOOL_NAMES = (PROPOSE, EVALUATE, ADOPT)
CONFIG_SCHEMA = "plx-playbook-proposals/v1"
MAX_FROZEN_CASES = 100
KIND = "playbook"
PURPOSE = "playbook_adoption"   # this module's confirmation forms
_CASE_ID = re.compile(r"case-[0-9a-f]{32}")
_ARGUMENT = re.compile(r"^\$\.([a-z_]+)|property '([a-z_]+)'")
# Pattern of a refusal code (a ProposalRefused code or this module's own).
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class PlaybookConfigError(ValueError):
    """A playbook proposal host configuration that cannot be served."""


def load_config(path: str | Path) -> dict[str, tuple[str, ...]]:
    """``{"stewards", "frozen_cases"}`` of a ``plx-playbook-proposals/v1`` file; fails closed
    on another schema, an unknown key, a steward that is not an employee principal (an agent
    or service never adopts) or a frozen id that is not a case id."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(document, Mapping) or document.get("schema") != CONFIG_SCHEMA
            or not {"schema", "stewards", "frozen_cases"} <= set(document)
            or set(document) - {"schema", "note", "stewards", "frozen_cases"}):
        raise PlaybookConfigError("playbook_proposals_document_invalid")
    stewards, cases = document["stewards"], document["frozen_cases"]
    if (not isinstance(stewards, list) or not stewards or len(set(map(str, stewards)))
            != len(stewards) or not all(isinstance(s, str) and s.startswith("employee:")
                                        and len(s) > 9 and not is_agent_principal(s)
                                        for s in stewards)):
        raise PlaybookConfigError("playbook_proposals_stewards_invalid")
    if (not isinstance(cases, list) or not 1 <= len(cases) <= MAX_FROZEN_CASES
            or len(set(map(str, cases))) != len(cases)
            or not all(isinstance(c, str) and _CASE_ID.fullmatch(c) for c in cases)):
        raise PlaybookConfigError("playbook_proposals_frozen_cases_invalid")
    return {"stewards": tuple(stewards), "frozen_cases": tuple(cases)}


def tool_specs(contracts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """MCP listings of the three tools from their contracts (they write Workbench records)."""
    return [{"name": name, "description": contracts["tools"][name]["description"],
             "inputSchema": contracts["tools"][name]["input"],
             "annotations": {"readOnlyHint": False, "destructiveHint": False,
                             "openWorldHint": False}}
            for name in TOOL_NAMES]


def _refused(code: str) -> PermissionError:
    return PermissionError(code if _CODE.fullmatch(code) else "proposal_refused")


class PlaybookTools:
    """One session's playbook tools over ``store`` (``playbook_eval.proposal_store``), asking
    stewards through the host's confirmation ``forms``."""

    def __init__(self, store: ProposalStore, contracts: Mapping[str, Any],
                 forms: ConfirmationForms) -> None:
        if not isinstance(store, ProposalStore):
            raise TypeError("store must be a ProposalStore")
        if not isinstance(forms, ConfirmationForms):
            raise TypeError("forms must be ConfirmationForms")
        self.store, self.contracts, self.forms = store, contracts, forms

    def _checked(self, name: str, direction: str, value: Any) -> None:
        problems = validate(value, self.contracts["tools"][name][direction],
                            root=self.contracts, limit=3)
        if not problems:
            return
        if direction == "output":   # never hand out a result that breaks the contract
            raise RuntimeError(f"{name}: output failed the contract: {problems}")
        names = sorted({m.group(1) or m.group(2) for p in problems
                        for m in [_ARGUMENT.search(p)] if m})
        raise ValueError("arguments_invalid" + (":" + ",".join(names) if names else ""))

    def call(self, name: str, arguments: Mapping[str, Any], actor: Any, *, runtime: Any,
             reads: Callable[[], Callable[[str, dict[str, Any]], dict[str, Any]]],
             evaluation_options: Mapping[str, Any], client_capabilities: Any,
             send_request: Callable[[dict[str, Any]], None],
             client_info: Any = None) -> dict[str, Any]:
        """Run one tool for the session ``actor``. ``reads()`` builds the session's own case
        reads for an evaluation; ``send_request`` writes a server-to-client request on the
        session transport; ``client_info`` is the client's declared ``clientInfo``."""
        self._checked(name, "input", arguments)
        try:
            if name == PROPOSE:
                result = self._propose(arguments, actor, runtime)
            elif name == EVALUATE:
                result = self._evaluate(arguments, actor, runtime, reads, evaluation_options)
            elif name == ADOPT:
                result = self._adopt(arguments, actor, client_capabilities, send_request,
                                     client_info)
            else:
                raise KeyError(name)
        except ProposalRefused as refused:
            raise _refused(refused.code) from None
        except EvaluationError:
            raise ValueError("evaluation_incomplete") from None
        self._checked(name, "output", result)
        return result

    # ------------------------------------------------------------------ propose

    def _propose(self, arguments: Mapping[str, Any], actor: Any, runtime: Any) -> dict[str, Any]:
        for index, item in enumerate(arguments["evidence"]):
            try:
                if _CASE_ID.fullmatch(item):
                    intact = runtime.cases.verify_chain(actor, item)
                else:
                    runtime.resolve_citation(item, actor)
                    intact = True
            except Exception:  # noqa: BLE001 - any refusal or failure denies
                raise PermissionError(f"evidence_not_readable:{index}") from None
            if intact is not True:   # a case whose change chain no longer verifies
                raise PermissionError(f"chain_broken:{index}")
        content = arguments["content"]
        proposal_id = self.store.propose(actor, kind=KIND, file=arguments["file"],
                                         content=content, rationale=arguments["rationale"],
                                         evidence=arguments["evidence"])
        return {"proposal_id": proposal_id, "proposal_status": self.store.status(proposal_id),
                "playbook_id": content["id"], "version": content["version"],
                "file": arguments["file"], "business_system_writes": 0}

    # ------------------------------------------------------------------ evaluate

    def _evaluate(self, arguments: Mapping[str, Any], actor: Any, runtime: Any,
                  reads: Callable[[], Callable[[str, dict[str, Any]], dict[str, Any]]],
                  options: Mapping[str, Any]) -> dict[str, Any]:
        principals = sorted({str(p) for p in getattr(actor, "principals", None) or ()})
        if (not principals or any(is_agent_principal(p) for p in principals)
                or not any(p.startswith(("service:", "employee:")) for p in principals)):
            raise PermissionError("evaluator_must_be_a_service_or_employee")
        proposal_id = arguments["proposal_id"]
        comparison = self.store.evaluate(
            proposal_id, actor, PROPOSAL_RUNNER, call=reads(), reader_principals=principals,
            unscorable=lambda case_id: runtime.hidden_from_every_reader(actor, case_id),
            **options)
        regressed, unscorable = set(comparison["regressed_items"]), comparison["unscorable"]
        verdict = ("regresses" if comparison["regression"]
                   else "unscored" if comparison["unscored_items"]
                   else "unscorable" if scored_share_refusal(len(comparison["item_ids"]),
                                                             len(unscorable))
                   else "non_regressing")
        return {"proposal_id": proposal_id, "proposal_status": self.store.status(proposal_id),
                "runner": comparison["runner"], "regression": comparison["regression"],
                "verdict": verdict,
                "playbook_reasons": list(comparison["playbook_reasons"]),
                "cases": [{"case_id": case_id, "before_outcome": comparison["before"][case_id],
                           "after_outcome": comparison["after"][case_id],
                           "regressed": case_id in regressed,
                           **({"unscorable_reasons": unscorable[case_id]}
                              if case_id in unscorable else {})}
                          for case_id in comparison["item_ids"]],
                "business_system_writes": 0}

    # ------------------------------------------------------------------ adopt

    def _adopt(self, arguments: Mapping[str, Any], actor: Any, capabilities: Any,
               send_request: Callable[[dict[str, Any]], None],
               client_info: Any) -> dict[str, Any]:
        proposal_id = arguments["proposal_id"]
        if arguments.get("mode", "request") == "status":
            out = {"proposal_id": proposal_id, "proposal_status": self.store.status(proposal_id),
                   "business_system_writes": 0}
            last = self.forms.last_outcome(PURPOSE, actor.tenant, proposal_id,
                                           getattr(actor, "principals", None) or ())
            if last is not None:
                out["last_answer"] = last
            return out
        steward = self.store.steward_of(actor)
        if not client_supports_elicitation(capabilities):
            raise PermissionError("human_confirmation_surface_unavailable")
        status = self.store.status(proposal_id)
        # A recorded adoption whose move was not confirmed is completed by ``adopt`` itself.
        preview = None if status == "adoption_pending" else \
            self.store.check_adoption(proposal_id, actor)
        # Only the steward's latest form for a proposal can be answered.
        token, _expires = self.forms.open(PURPOSE, actor.tenant, proposal_id, steward, {
            "proposal_id": proposal_id, "client_info": _declared(client_info),
            "binding": None if preview is None else [preview["comparison_sha256"],
                                                     preview["adopted_sha256"]]})
        send_request({"jsonrpc": "2.0", "id": token, "method": "elicitation/create",
                      "params": _form(proposal_id, steward, preview)})
        out = {"proposal_id": proposal_id, "proposal_status": status,
               "confirmation": "awaiting_steward_confirmation", "business_system_writes": 0}
        if preview is not None:
            out.update(playbook_id=preview["playbook"]["id"],
                       version=preview["playbook"]["version"], file=preview["file"])
        return out

    def answer(self, message: Mapping[str, Any], actor: Any) -> str:
        """Complete an adoption form from the client's JSON-RPC response on the session
        transport (single use, by the steward it asked, from any of their sessions). Returns
        the outcome (``adopted`` or a refusal code; ``confirmation_forms.UNKNOWN`` for a form
        that is not this module's), which mode status reports; a response gets no reply."""
        token = message.get("id")
        try:
            form = self.forms.claim(token, purpose=PURPOSE,
                                    tenant=getattr(actor, "tenant", None),
                                    answerer=lambda: self.store.steward_of(actor))
        except FormRefused as refused:
            return refused.code
        except ProposalRefused as refused:   # not a steward (any more): the form is spent
            return refused.code if _CODE.fullmatch(refused.code) else "proposal_refused"
        try:
            outcome = self._complete(form["payload"], token, message, actor)
        except BaseException:
            self.forms.finish(token, "refused", "answer_failed")
            raise
        self.forms.finish(token, _STATES.get(outcome, "refused"), outcome)
        return outcome

    def _complete(self, request: Mapping[str, Any], token: str, message: Mapping[str, Any],
                  actor: Any) -> str:
        proposal_id = request["proposal_id"]
        try:
            answer = message.get("result") if "result" in message else {"action": "cancel"}
            action = answer.get("action") if isinstance(answer, Mapping) else None
            if action in ("decline", "cancel"):
                return "declined" if action == "decline" else "cancelled"
            if action != "accept" or answer.get("content") != {"confirm": True}:
                return "not_confirmed_by_the_steward"
            if request["binding"] is not None:
                now = self.store.check_adoption(proposal_id, actor)
                if [now["comparison_sha256"], now["adopted_sha256"]] != request["binding"]:
                    return "proposal_changed_since_request"
            self.store.adopt(proposal_id, actor, confirmation={
                "via": "mcp_elicitation", "form_id": token,
                "client_info": request["client_info"]})
        except ProposalRefused as refused:
            return refused.code if _CODE.fullmatch(refused.code) else "proposal_refused"
        except OSError:
            # The overlay failed: before the decision was recorded nothing changed (a new
            # request retries); after it, the decision stays adoption_pending.
            try:
                recorded = self.store.status(proposal_id) != "evaluated"
            except (ProposalRefused, OSError):
                recorded = True
            return "adoption_not_completed" if recorded else "adoption_not_recorded"
        return "adopted"


_STATES = {"adopted": "confirmed", "declined": "declined", "cancelled": "cancelled"}


def _declared(client_info: Any) -> dict[str, str | None]:
    """The client's declared ``clientInfo`` name and version, bounded (as declared, never
    verified)."""
    info = client_info if isinstance(client_info, Mapping) else {}
    return {key: info[key][:120] if isinstance(info.get(key), str) else None
            for key in ("name", "version")}


def _scope(preview: Mapping[str, Any]) -> str:
    """What the latest comparison scored: every frozen case, or how many, and which reasons
    left the others out (unscorable: no reader of this host can read them in full)."""
    total, left_out = preview["frozen"], preview["unscorable"]
    if not left_out:
        return ("Its latest comparison ran every frozen case of this host before and after "
                "the edit")
    reasons = ", ".join(sorted({r for found in left_out.values() for r in found}))
    return (f"Its latest comparison scored {total - len(left_out)} of {total} frozen cases of "
            f"this host before and after the edit; {len(left_out)} left out, unscorable for "
            f"every reader ({reasons})")


def _form(proposal_id: str, steward: str, preview: Mapping[str, Any] | None) -> dict[str, Any]:
    """``elicitation/create`` params (MCP 2025-11-25 form mode): Workbench facts only (no
    proposer-written text) and the steward's explicit tick."""
    if preview is None:
        what = (f"Complete the recorded adoption of playbook proposal {proposal_id}: its "
                "decision is recorded and its file move was not confirmed.")
    else:
        what = (f"Adopt playbook {preview['playbook']['id']} version "
                f"{preview['playbook']['version']} ({preview['file']}, SHA-256 "
                f"{preview['adopted_sha256'][:12]}...) from proposal {proposal_id}. "
                f"{_scope(preview)} and does not regress, and the edit neither widens "
                "authority nor weakens a check.")
    return {"mode": "form",
            "message": (f"{what} Answer only if you are {steward}, the steward this session is "
                        "for; an agent must not answer. The version is written to this host's "
                        "adopted-playbook directory (never the shipped code) and takes effect "
                        "when the playbooks are next loaded; nothing is written to any Petra "
                        "system."),
            "requestedSchema": {"type": "object", "properties": {"confirm": {
                "type": "boolean", "default": False,
                "title": "I reviewed this playbook version and adopt it"}},
                "required": ["confirm"]}}


__all__ = ["ADOPT", "CONFIG_SCHEMA", "EVALUATE", "PROPOSE", "PlaybookConfigError",
           "PlaybookTools", "TOOL_NAMES", "load_config", "tool_specs"]
