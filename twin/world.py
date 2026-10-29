"""Test mode end to end: every action type of the catalog, run on the write twin.

``TwinWorld`` is the isolated world of ``operating.goal2_exec.Lifecycle`` (case store,
action path, ``IsolatedOwnerSystem`` for the ``fixture.*`` types, the per-call approver,
the human MC decisions) with the twins in place of the owning systems: the Portal twin
answers the Portal adapter through ``TwinTransport`` and resolves its ``api://...@twin``
citations; the MC twin serves the MC client. The call approvers and the MC role roster the
action path checks deciders against come from the isolated approver roster, read through
``load_approver_roster`` as every host reads its own. ``registry`` sends every catalog
action type to the twin or refuses it as the catalog declares; the Workbench-internal types
write only the case store (``execute_internal``) and MC follow-ups go through MC's write
door (``send_call``). The BC twin (``twin.bc``) answers BC's OData through the same
transport and resolves its records per company; ``bc_registry`` sends the isolated-owner
types to its own isolated owner, whose door is BC's OData PATCH with If-Match the cited
etag, the Workbench owner's own stricter path; upstream sends ``*``
(``twin.bc.odata.ODataDoor``, ``ANY_ETAG``, ``twin.bc.lifecycle``).

``run_lifecycle(state_dir)`` runs each type through its lifecycle (propose, the approval
its type needs, execute, read-back, audit chain) and the Portal failure cases (a lost
answer, a 412, an unknown outcome escalated as one MC twin task), and reports states and
counts only. Everything runs in test mode (``twin.mode``); nothing leaves the process.
Evidence label: ``twin-transport`` (Portal), ``isolated-fixture`` (isolated owner, MC).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from operating import action_adapters
from operating import action_path as ap
from operating import goal2_exec as g2
from operating.action_catalog import GATED_ADAPTER, ActionCatalog
from operating.case_links import LinkContracts
from operating.cases import CaseStore
from operating.goal2_exec_owner import IsolatedOwnerSystem, read_back_for

from . import mode
from .bc import BcTwin, load_captures as load_bc_captures
from .bc.odata import ODataDoor
from .mc import McTwin
from .portal import PortalTwin, load_captures
from .transport import TwinTransport

SCHEMA = "plx-twin-lifecycle/v1"
ACTOR, SESSION = g2.ACTOR, g2.SESSION
PORTAL_CAPABILITY = "portal.sales_order.set_customer_po"
ORDER_RELATION = "public.app_sales_order"
# The MC twin's users (mc_users): the admin decides, the member and the agent cannot.
USERS = {g2.MC_ADMIN: {"role": "admin", "oid": g2.MC_ADMIN_OID},
         g2.MC_MEMBER: {"role": "member", "oid": g2.MC_MEMBER_OID},
         g2.CALL_APPROVER: {"role": "member", "oid": g2.CALL_APPROVER_OID}}
# Portal scenario -> (fixture order number, Portal twin faults on the write)
PORTAL_SCENARIOS: Mapping[str, tuple[int, tuple[str, ...]]] = {
    "verified": (1, ()), "lost_answer": (2, ("lost_answer",)),
    "etag_mismatch": (3, ("etag_mismatch",)), "unknown": (4, ("unavailable",))}
ESCALATION_PREFIX = "Unverified outcome of Workbench action "
# Adapter per twinned owning system (each reviewed adapter type names its system).
ADAPTERS = {adapter.SYSTEM: adapter for adapter in action_adapters.ADAPTER_TYPES}


def registry(world: "TwinWorld") -> ap.DispatcherRegistry:
    """Test mode only: every owner-system type of the catalog to the twin, or refused."""
    mode.require()
    catalog = ActionCatalog.builtin()
    out = ap.DispatcherRegistry()
    for name in catalog.proposal_ids():
        entry = catalog.get(name)
        dispatch = entry["executor"]["dispatch"]
        if name in ap.FIXTURE_CAPABILITIES:
            out.register(name, ap.FixtureDispatcher(world.owner))
        elif dispatch == GATED_ADAPTER:
            adapter = ADAPTERS[entry["real_capability"]["target_system"]]
            out.register(name, ap.RealSystemDispatcher(adapter(name, world.transport)))
        elif dispatch == "refused":
            out.register(name, ap.RefusingDispatcher(entry["owner_system"],
                                                     entry["executor"]["refusal"]))
        # mc_write_door: sent by ActionPath.send_call through the MC twin's write door.
    return out


def bc_registry(world: "TwinWorld") -> ap.DispatcherRegistry:
    """Test mode only: the catalog's isolated-owner types to the BC twin's own owner."""
    mode.require()
    out = ap.DispatcherRegistry()
    for name in sorted(ap.FIXTURE_CAPABILITIES):
        out.register(name, ap.FixtureDispatcher(world.bc.owner))
    return out


@dataclass
class TwinWorld(g2.Lifecycle):
    """One test-mode world under ``state_dir`` (fresh files)."""

    def __post_init__(self) -> None:
        mode.require()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.owner = IsolatedOwnerSystem(self.state_dir / "owner.db",
                                         readers=frozenset({"ops"}))
        # The Workbench's Portal session holds the routes' STAFF floor (assumed: no such
        # Portal service identity exists yet).
        self.portal = PortalTwin(readers=frozenset({"ops"}), clock=self.clock,
                                 session_role="STAFF")
        self.portal.seed_captures(load_captures())
        self.cases = CaseStore(self.state_dir / "cases.db", citation_resolver=self.resolve,
                               link_contracts=LinkContracts.builtin())
        self.mc = McTwin(self.clock, USERS)
        self.mc.buckets.add(g2.FOLLOW_UP_BUCKET)
        self.read_roster()
        self.path = self.new_path()
        self.mc_client = self.mc.client(self.state_dir / "mc-write-ledger.jsonl")
        self.mc_create = self.mc.client(self.state_dir / "mc-create-ledger.jsonl",
                                        write_tools=frozenset({"mc_create_task"}))
        self.bc = BcTwin.from_captures(self.state_dir / "bc-owner.db", load_bc_captures(),
                                       tenant=ACTOR.tenant, clock=self.clock)
        self.transport = TwinTransport(self.portal, self.bc)
        self.bc.owner.door = ODataDoor(self.bc, self.transport)
        self.registry = registry(self)
        self.bc_registry = bc_registry(self)
        self.bc_read_back = read_back_for(self.bc.owner)
        self.read_back = read_back_for(self.owner)
        self._records = 0
        self.case_ids: list[str] = []
        self.define_typed_relations()

    def new_path(self) -> ap.ActionPath:
        return ap.ActionPath(self.state_dir / "actions.db", self.cases,
                             **self.approvers.path_options(), mc_operator_email=g2.OPERATOR,
                             clock=self.clock, mc_follow_up_bucket=g2.FOLLOW_UP_BUCKET,
                             owner_peers=self.portal.peers)

    def resolve(self, handle: str, actor: Any) -> dict[str, Any]:
        if isinstance(handle, str) and handle.startswith("api://"):
            return self.portal.resolve(handle, actor)
        if self.bc.holds(handle):
            return self.bc.resolve(handle, actor)
        return self.owner.resolve(handle, actor)

    # ---- the Portal type: PO document -> proposal -> MC gate -> twin --------

    def portal_intent(self, number: int, value: str) -> tuple[str, str]:
        """A case on fixture order ``number`` whose captured PO document (a field read from
        the customer's PDF, held by the isolated owner) states ``value``; the
        ``set_customer_po`` proposal cites both and is registered. ``(case_id, intent_id)``."""
        order = f"so-twin-{number}"
        target = PortalTwin.citation(ORDER_RELATION, order)
        document = self.owner.seed(g2.SOURCE_RELATION, {"id": 100 + number},
                                   {"id": 100 + number, "customer_po": value,
                                    "lines_stated": "1"})
        case_id = self.path.open_case(
            ACTOR, f"Does order SO-T{number:04d} carry the customer PO its PO document "
                   "states?", roots=[f"twin-order-{number}"], objective_author_kind="human",
            session=SESSION)
        proposal = self.path.record_proposal(
            ACTOR, case_id, PORTAL_CAPABILITY,
            {"proposed_customer_po": value, "evidence_citation": document,
             "evidence_field": "customer_po"},
            [{"check": "field_empty", "citation": target, "field": "po_reference"},
             "a person at PLX confirms the evidence belongs to this order"],
            citations=[target, document], session=SESSION,
            expected_effect={"relation": ORDER_RELATION, "key": {"id": order},
                             "fields": ["po_reference"], "equals": value})
        intent = self.path.register(ACTOR, case_id, proposal["proposal_id"], session=SESSION)
        self.case_ids.append(case_id)
        return case_id, intent["intent_id"]

    def escalate(self, intent_id: str) -> dict[str, Any]:
        """``ActionPath.escalate_to_mc``, then the twin's call approver approves the exact
        call, it is sent once to the MC twin and read back."""
        out = self.path.escalate_to_mc(ACTOR, intent_id, session=SESSION)
        follow_up = out["intent_id"]
        if self.path.get(ACTOR, follow_up)["state"] == "call_approval_required":
            self.approve_follow_up(follow_up)
        self.path.send_call(ACTOR, follow_up, self.mc_create, session=SESSION)
        if self.path.get(ACTOR, follow_up)["state"] in ("dispatched", "dispatched_unknown"):
            self.path.read_back_call(ACTOR, follow_up, self.mc_create, session=SESSION)
        return {**out, "state": self.path.get(ACTOR, follow_up)["state"]}


def _row(world: TwinWorld, action_type: str, intent_id: str | None, case_id: str,
         **extra: Any) -> dict[str, Any]:
    """One type's result: its end state, outcome and both audit chains."""
    row: dict[str, Any] = {"action_type": action_type, **extra,
                           "case_chain_ok": world.cases.verify_chain(ACTOR, case_id)}
    if intent_id is not None:
        view = world.new_path().view(ACTOR, intent_id)
        row.update(end_state=view["state"], outcome=view["outcome"],
                   chain_ok=view["chain_ok"],
                   read_back=[item["outcome"] for item in view["read_backs"]],
                   evidence=sorted({item["detail"]["evidence"] for item in
                                    world.cases.get(ACTOR, case_id)["verifications"]
                                    if isinstance(item.get("detail"), Mapping)}))
    return row


def run_portal_scenario(world: TwinWorld, name: str) -> dict[str, Any]:
    """One ``PORTAL_SCENARIOS`` run of ``set_customer_po`` on the Portal twin."""
    number, faults = PORTAL_SCENARIOS[name]
    path = world.path
    case_id, iid = world.portal_intent(number, f"PO70{number:02d}")
    world.authorize(iid, f"TASK-TWIN-SO-{number}")
    world.portal.faults.extend(faults)
    writes = world.portal.writes
    first = path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
    dispatcher = world.registry.for_capability(PORTAL_CAPABILITY)
    if path.get(ACTOR, iid)["state"] in ("dispatched", "dispatched_unknown"):
        path.read_back(ACTOR, iid, dispatcher.read_back, session=SESSION)
    replay = path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
    escalations = None
    if path.get(ACTOR, iid)["state"] in ("dispatched_unknown", "failed"):
        world.escalate(iid)
        world.escalate(iid)   # a repeat: the same task, never a second one
        escalations = len(world.mc.created_tasks(ESCALATION_PREFIX + iid))
    return _row(world, PORTAL_CAPABILITY, iid, case_id, scenario=name,
                first_dispatch=first["state"], first_reason=first.get("reason"),
                replayed=replay.get("replayed"), twin_writes=world.portal.writes - writes,
                escalation_tasks=escalations)


def _internal(world: TwinWorld) -> list[dict[str, Any]]:
    """The four Workbench-internal types: executed now, verified by re-reading the case."""
    path = world.path
    case_id = path.open_case(ACTOR, "Is order SO-T0009 the Portal twin of FM order 9001?",
                             objective_author_kind="human", session=SESSION)
    order = PortalTwin.citation(ORDER_RELATION, "so-twin-9")
    fm_order = PortalTwin.citation("public.order_entry", 9001)
    runs = [
        ("workbench.identity_link.record", {"from_citation": order, "to_citation": fm_order,
                                            "contract_id": "portal-so-to-fm-order-by-fmid"}),
        ("workbench.draft.record", {"statement": "The FM order looks like this order's twin.",
                                    "citations": [order, fm_order]}),
        ("workbench.follow_up.record", {"missing": "The customer PO of order SO-T0009.",
                                        "why": "Its PO document is not captured yet.",
                                        "who_can_answer": ["customer service"],
                                        "state": "unknown"})]
    rows = []
    finding = None
    for action_type, parameters in runs:
        record = path.execute_internal(ACTOR, action_type, case_id, parameters, session=SESSION)
        finding = record["receipt"].get("finding_id") or finding
        rows.append((action_type, record))
    rows.append(("workbench.finding.correct", path.execute_internal(
        ACTOR, "workbench.finding.correct", case_id,
        {"finding_id": finding, "reason": "The link contract verified it; no guess needed."},
        session=SESSION)))
    audit = {item["action_id"]: item["outcome"] for item in path.internal_actions(ACTOR, case_id)}
    return [_row(world, action_type, None, case_id, end_state=audit.get(action_type),
                 outcome=record["read_back"]["outcome"], chain_ok=True)
            for action_type, record in rows]


def _refused(world: TwinWorld) -> list[dict[str, Any]]:
    """The two types the catalog refuses: nothing reaches MC or the twin."""
    path, rows = world.path, []
    target = PortalTwin.citation(ORDER_RELATION, "so-twin-5")
    case_id = path.open_case(ACTOR, "Set the customer PO of order SO-T0005 to PO7999?",
                             objective_author_kind="human", session=SESSION)
    proposal = path.record_proposal(
        ACTOR, case_id, "test.record.set_field", {"field": "po_reference", "value": "PO7999"},
        [{"check": "field_empty", "citation": target, "field": "po_reference"}],
        citations=[target], session=SESSION,
        expected_effect={"relation": ORDER_RELATION, "key": {"id": "so-twin-5"},
                         "fields": ["po_reference"], "equals": "PO7999"})
    iid = path.register(ACTOR, case_id, proposal["proposal_id"], session=SESSION)["intent_id"]
    world.mc.add_task("TASK-TWIN-TEST", "Test-host type")
    try:
        path.prepare_gate(ACTOR, iid, "TASK-TWIN-TEST", world.mc_client, session=SESSION)
        refusal = None
    except ap.ActionPathError as exc:
        refusal = exc.code
    rows.append(_row(world, "test.record.set_field", iid, case_id, refused_at="prepare_gate",
                     refusal=refusal))
    ao = PortalTwin.citation("public.assembly_order", 8001)
    case_id = path.open_case(ACTOR, "Set the planned quantity of AO-T8001 to 10?",
                             objective_author_kind="human", session=SESSION)
    proposal = path.record_proposal(
        ACTOR, case_id, "portal.assembly_order.set_header_quantities",
        {"qtytotal": 10, "evidence": {"qtytotal": {"citation": ao, "field": "qtytotal"}}},
        [{"check": "field_empty", "citation": ao, "field": "qtytotal"}], citations=[ao],
        session=SESSION,
        expected_effect={"relation": "public.assembly_order", "key": {"id": 8001},
                         "fields": ["qtytotal"], "equals": 10})
    try:
        path.register(ACTOR, case_id, proposal["proposal_id"], session=SESSION)
        refusal = None
    except ap.ActionPathError as exc:
        refusal = exc.code
    rows.append(_row(world, "portal.assembly_order.set_header_quantities", None, case_id,
                     end_state="not_registered", outcome="not_dispatched", chain_ok=True,
                     refused_at="register", refusal=refusal))
    return rows


def run_lifecycle(state_dir: Path) -> dict[str, Any]:
    """Every catalog action type and the Portal failure cases, in test mode."""
    state_dir = Path(state_dir)
    if state_dir.exists() and any(state_dir.iterdir()):
        raise ValueError("state_dir_not_empty: the run needs a fresh directory")
    with mode.test_mode(allow={PORTAL_CAPABILITY}):
        world = TwinWorld(state_dir)
        rows = _internal(world)
        fixture = g2.run_scenario(world, "happy", 1)
        rows.append({"action_type": ap.FIXTURE_CAPABILITY, "end_state": fixture["state"],
                     "outcome": fixture["outcome"], "chain_ok": fixture["chain_ok"],
                     "case_chain_ok": all(world.cases.verify_chain(ACTOR, c)
                                          for c in world.case_ids)})
        cited = g2.run_owner_scenario(world, "verified", 2)
        rows.append({"action_type": g2.CITED_VALUE_CAPABILITY, "end_state": cited["state"],
                     "outcome": cited["outcome"], "chain_ok": cited["chain_ok"],
                     "case_chain_ok": all(world.cases.verify_chain(ACTOR, c)
                                          for c in world.case_ids)})
        follow = g2.run_follow_up_scenario(world, world.mc_create, "verified", 3)
        rows.append({"action_type": g2.FOLLOW_UP_CAPABILITY, "end_state": follow["state"],
                     "outcome": follow["outcome"], "chain_ok": follow["chain_ok"],
                     "case_chain_ok": all(world.cases.verify_chain(ACTOR, c)
                                          for c in world.case_ids)})
        portal = [run_portal_scenario(world, name) for name in PORTAL_SCENARIOS]
        rows.append(portal[0])
        rows.extend(_refused(world))
    return {"schema": SCHEMA, "evidence_label": mode.TWIN_LABEL,
            "types": sorted(rows, key=lambda row: row["action_type"]),
            "portal_scenarios": portal,
            "portal_requests": _count(f"{m} {p.rsplit('/', 1)[0]}"
                                      for _s, m, p in world.transport.requests),
            "mc_tool_calls": _count(world.mc.tool_calls),
            "mc_refusals": _count(world.mc.refusals)}


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m twin.world",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="a new or empty directory for the test-mode stores")
    args = parser.parse_args(argv)
    try:
        report = run_lifecycle(args.state_dir)
    except (ValueError, mode.TwinModeRefused) as exc:
        parser.error(str(exc))
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
