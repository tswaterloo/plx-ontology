"""The eval principal: the declared reader the answer evals run as when no employee is named.

Decision (2026-10-19): when ``PLX_EVAL_READER`` is absent, the answer evals
(``eval.case_cards_answers`` and the live parts of ``eval.ask_agent`` and
``eval.real_agent``) run as this principal instead of being blocked
``no_employee_identity``. It is never a person and never mistaken for one:

* **Declared, not chosen.** Its grants come only from the committed
  ``examples/eval_principal.json`` (``plx-eval-principal/v1``, read from that fixed path;
  no command line names another): ``staff:`` roles, a clearance below ``restricted`` and
  the markings it may hold, never ``finance``. An unknown key or value refuses the file.
* **The employee gate, unchanged.** A host started with ``--eval-principal``
  (``operating.mcp_server``) resolves the session's ``employee:<object_id>`` through
  ``EvalPrincipalResolver``, which grants exactly the declared roles to the declared
  sentinel object id and denies everything else (``eval_principal_only``); every consumer
  (``EmployeeActorGate``, item ACLs, file classes, audiences) applies as for an
  employee. It has no Portal row (no Scratch), no file clearance (no Finance or Business
  Central file), no Entra membership, and its own identities (the sentinel id and an
  ``.invalid`` email) match no item ACL or mailbox.
* **Read only.** The host never serves it the action path, and a marking grant to one of
  its roles outside the declared markings is refused (``operating.host`` drops them from
  the eval session's arguments).
* **Labelled.** The session's proof is ``eval_principal`` (its intents' ``sub.kind``
  too) and every eval result names it (``reader_block``). The A6 permission proof
  (``denied_leaks`` with a real identity) is never made by it: that still requires
  ``PLX_EVAL_READER``, and the result says so.

The harness signs the session ticket with the host's own secret, as for a named employee
(``scripts.eval_real_agent.LiveHost``); the ticket's tenant is the host's employee
policy's (``ticket_claims``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ontology.core import Actor

from .employee_permissions import (EmployeeEntitlement, EmployeePermissionPolicy,
                                   EmployeePermissionResolver, EmployeePrincipal)
from . import session_identity as si

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples" / "eval_principal.json"
SCHEMA = "plx-eval-principal/v1"
LABEL = "eval_principal"
A6_PROOF = ("requires PLX_EVAL_READER: denied_leaks (A6) is proven only with a real "
            "employee identity, never by the eval principal")
_KEYS = frozenset({"schema", "label", "note", "object_id", "email", "uid", "principals",
                   "clearance", "markings"})
_ROLE = re.compile(r"^staff:[a-z][a-z0-9_-]{0,63}$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EMAIL = re.compile(r"^[a-z0-9._-]{1,64}@[a-z0-9-]{1,63}\.invalid$")
_UID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CLEARANCES = ("public", "internal", "confidential")   # never restricted
REFUSED_MARKINGS = frozenset({"finance"})


@dataclass(frozen=True)
class EvalPrincipal:
    """The declared eval principal (``load``)."""

    object_id: str
    email: str
    uid: str
    principals: frozenset[str]
    clearance: str
    markings: frozenset[str]
    source: str          # the config, cited with its digest


def load(path: Path = CONFIG) -> EvalPrincipal:
    """The committed eval principal, or ValueError (a code) on anything not exactly shaped."""
    from ontology.store_ddl import MARKINGS
    try:
        raw = Path(path).read_bytes()
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        raise ValueError("eval_principal_config_unreadable") from None
    if not isinstance(doc, dict) or set(doc) != _KEYS or doc["schema"] != SCHEMA \
            or doc["label"] != LABEL:
        raise ValueError("eval_principal_config_invalid")
    principals, markings = doc["principals"], doc["markings"]
    if not isinstance(principals, list) or not principals or len(set(principals)) != len(
            principals) or not all(isinstance(p, str) and _ROLE.fullmatch(p)
                                   for p in principals):
        raise ValueError("eval_principal_principals_invalid")
    if doc["clearance"] not in CLEARANCES:
        raise ValueError("eval_principal_clearance_invalid")
    if not isinstance(markings, list) or not all(isinstance(m, str) and m in MARKINGS
                                                 for m in markings):
        raise ValueError("eval_principal_markings_invalid")
    if REFUSED_MARKINGS & set(markings):
        raise ValueError("eval_principal_marking_refused:finance")
    for key, shape in (("object_id", _GUID), ("email", _EMAIL), ("uid", _UID)):
        if not isinstance(doc[key], str) or not shape.fullmatch(doc[key]):
            raise ValueError(f"eval_principal_{key}_invalid")
    try:
        cited = Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        cited = Path(path).name
    return EvalPrincipal(doc["object_id"], doc["email"], doc["uid"], frozenset(principals),
                         doc["clearance"], frozenset(markings),
                         f"{cited}@sha256:{sha256(raw).hexdigest()}")


class EvalPrincipalResolver(EmployeePermissionResolver):
    """Resolves the eval principal's sentinel object id, in the policy's Entra tenant, to
    its declared roles; every other principal is denied (``eval_principal_only``). Reads
    no Portal row and no directory."""

    def __init__(self, policy: EmployeePermissionPolicy, principal: EvalPrincipal) -> None:
        super().__init__(policy, lambda employee: None)
        if not isinstance(principal, EvalPrincipal):
            raise TypeError("EvalPrincipal required")
        self.eval_principal = principal

    def resolve(self, principal: EmployeePrincipal) -> EmployeeEntitlement:
        if not isinstance(principal, EmployeePrincipal):
            raise TypeError("EmployeePrincipal required")
        p, declared = self.policy, self.eval_principal
        if principal.kind != "employee" or principal.tenant_id != p.entra_tenant_id \
                or principal.object_id != declared.object_id \
                or principal.object_id in p.service_principal_object_ids:
            return EmployeeEntitlement(principal, None, "eval_principal_only",
                                       fingerprint=self._fingerprint(
                                           None, frozenset(), "eval_principal_only"))
        actor = Actor(p.tenant, declared.principals | {principal.principal},
                      declared.clearance)
        identities = frozenset({declared.object_id, declared.email})
        return EmployeeEntitlement(
            principal, actor, None, (), identities, (LABEL,),
            (("eval_principal", declared.source, ""),),
            self._fingerprint(actor, identities, None), "", frozenset(),
            dict(p.library_security_classes))


def eval_session(resolver: EvalPrincipalResolver, *, ticket: str, secret: Any,
                 replay_guard: si.TicketReplayGuard, client: str | None = None
                 ) -> si.SessionIdentity:
    """The eval principal's agent session from a single-use start-up ticket (verified,
    replay-guarded and matched to the resolved identity as ``employee_session`` does),
    proof ``eval_principal``; ``SessionIdentityRefused`` otherwise."""
    if not isinstance(resolver, EvalPrincipalResolver):
        raise si.SessionIdentityRefused("eval_principal_resolver_required")
    session = si.employee_session(resolver, channel=si.CHANNEL_MCP, ticket=ticket,
                                  secret=secret, replay_guard=replay_guard, single_use=True,
                                  client=client)
    return replace(session, proof=si.PROOF_EVAL, portal_user_id=None)


def check_host(principal: EvalPrincipal, *, action_path: bool,
               marking_grants: Mapping[str, Any]) -> None:
    """Refuse (``SessionIdentityRefused``) an eval session the host would serve the action
    path, or a marking its roles hold beyond the declared markings."""
    if action_path:
        raise si.SessionIdentityRefused("eval_principal_action_path_refused")
    for marking, holders in sorted(marking_grants.items()):
        if marking not in principal.markings and set(holders) & principal.principals:
            raise si.SessionIdentityRefused(f"eval_principal_marking:{marking}")


def session_grants(principal: EvalPrincipal, marking_grants: Mapping[str, Any]
                   ) -> dict[str, Any]:
    """The host's marking grants an eval session is started with: only the declared
    markings (every other grant is the employees', never the eval principal's)."""
    return {m: holders for m, holders in marking_grants.items() if m in principal.markings}


def ticket_claims(host_config: Path, principal: EvalPrincipal | None = None
                  ) -> dict[str, str]:
    """The eval principal's session-ticket claims for the host at ``host_config``: its
    tenant is the host's employee policy's. ValueError (a code) when the config or its
    policy does not load."""
    from .host import HostConfig, HostError
    principal = load() if principal is None else principal
    try:
        config = HostConfig.from_file(host_config)
    except HostError as error:
        raise ValueError(error.code) from None
    if config.employee_policy is None:
        raise ValueError("host_config_invalid:employee_policy")
    try:
        policy = EmployeePermissionPolicy.from_file(config.employee_policy)
    except (ValueError, OSError, UnicodeDecodeError):
        raise ValueError("employee_policy_invalid") from None
    return {"uid": principal.uid, "email": principal.email,
            "entraTenantId": policy.entra_tenant_id, "entraObjectId": principal.object_id}


def reader_block(kind: str | None) -> dict[str, Any]:
    """Who an eval result was read as: ``employee`` (the named ``PLX_EVAL_READER``),
    ``eval_principal`` (with its roles, config and the A6 note) or ``none``."""
    if kind != LABEL:
        return {"kind": kind or "none"}
    declared = load()
    return {"kind": LABEL, "principals": sorted(declared.principals),
            "clearance": declared.clearance, "markings": sorted(declared.markings),
            "config": declared.source, "a6_permission_proof": A6_PROOF}


__all__ = ["A6_PROOF", "CONFIG", "LABEL", "SCHEMA", "EvalPrincipal", "EvalPrincipalResolver",
           "check_host", "eval_session", "load", "reader_block", "session_grants",
           "ticket_claims"]
