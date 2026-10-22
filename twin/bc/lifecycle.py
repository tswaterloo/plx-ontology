"""A BC-targeted fixture action end to end, in test mode (``twin.world``, ``twin.bc``).

``run_bc_lifecycle(state_dir)`` runs the catalog's isolated-owner type
``fixture.record.set_field`` on BC twin purchase orders the actor's company audience grants:
an employee's case asks to set an order's ``requestedReceiptDate`` to the date the question
states; the proposal cites the order with its version (the etag) as a compare-and-set
precondition; register (the catalog hook), the MC gate decided on the MC twin, dispatch to
the BC twin's isolated owner (``TwinWorld.bc_registry``), whose door sends the write as
BC's OData (``odata.ODataDoor``: a GET, then a PATCH with ``If-Match`` the etag of the
cited version), read-back, both audit chains, and BC's own OData answer for the order
(through ``TwinTransport``). ``SCENARIOS``:

* ``verified``: written once by one PATCH carrying the cited etag, read back, and OData
  shows the value under a new etag; a replay sends nothing.
* ``edited_after_approval``: another BC user saves the order over OData (with the current
  If-Match) after the approval; the action path's re-read at dispatch refuses, nothing is
  sent, and the etag the case cited is refused by OData (412).
* ``etag_mismatch``: a save lands between the Workbench's checks and the owner's apply
  (the owner's ``concurrent_write``): the action's PATCH with the cited etag is refused
  (412), nothing written.

The last two are ``WORKBENCH_OWNER_ONLY``: they rest on the Workbench owner's own
stricter path (its re-read, and its door sending the cited etag as If-Match). Every BC
PATCH upstream sends ``If-Match: *`` (``odata.ANY_ETAG``, cited there), which BC applies
over any save, so neither proves anything about real BC write safety.

States and counts only. Evidence label: ``isolated-fixture``; nothing leaves the process.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from operating import action_path as ap
from operating import goal2_exec as g2
from operating.action_adapters import OwnerRequest
from operating.cases import CitationError

from .. import mode
from ..world import ACTOR, SESSION, TwinWorld, _count
from .odata import COMPANY_KEY, VERSION_FIELD

SCHEMA = "plx-twin-bc-lifecycle/v1"
TARGET_SET = "purchaseOrders"
TARGET_FIELD = "requestedReceiptDate"
SCENARIOS = ("verified", "edited_after_approval", "etag_mismatch")
WORKBENCH_OWNER_ONLY = frozenset({"edited_after_approval", "etag_mismatch"})


def targets(world: TwinWorld, actor: Any = ACTOR) -> list[str]:
    """Handles of the ``TARGET_SET`` records ``actor`` may read whose ``TARGET_FIELD`` is
    empty, company by company, in seeding order."""
    out = []
    for company in world.bc.companies():
        for handle in world.bc.handles(company, TARGET_SET):
            try:
                row = world.bc.resolve(handle, actor)["row"]
            except CitationError:
                continue
            if row.get(TARGET_FIELD) is None:
                out.append(handle)
    return out


def odata_path(cited: dict[str, Any]) -> str:
    key = cited["key"]
    return f"/companies({key[COMPANY_KEY]})/{TARGET_SET}({key['id']})"


def bc_intent(world: TwinWorld, target: str, value: str) -> tuple[str, str]:
    """A human-asked case on the cited order and its registered ``set_field`` proposal:
    the field is empty and the version is the one cited. ``(case_id, intent_id)``."""
    cited = world.resolve(target, ACTOR)   # read through the citation, as the agent would
    number = cited["row"]["number"]
    case_id = world.path.open_case(
        ACTOR, f"Set the requested receipt date of BC purchase order {number} to {value}?",
        roots=[number], objective_author_kind="human", session=SESSION)
    proposal = world.path.record_proposal(
        ACTOR, case_id, ap.FIXTURE_CAPABILITY, {"field": TARGET_FIELD, "value": value},
        [{"check": "field_empty", "citation": target, "field": TARGET_FIELD},
         {"check": "field_equals", "citation": target, "field": VERSION_FIELD,
          "value": cited["row"][VERSION_FIELD]},
         "a person confirms the supplier confirmed this receipt date"],
        citations=[target], session=SESSION,
        expected_effect={"relation": cited["relation"], "key": cited["key"],
                         "fields": [TARGET_FIELD], "equals": value})
    intent = world.path.register(ACTOR, case_id, proposal["proposal_id"], session=SESSION)
    world.case_ids.append(case_id)
    return case_id, intent["intent_id"]


def run_bc_scenario(world: TwinWorld, name: str, number: int) -> dict[str, Any]:
    """One ``SCENARIOS`` run on the next untouched order the actor may read."""
    if name not in SCENARIOS:
        raise ValueError("unknown_scenario")
    path, owner = world.path, world.bc.owner
    target, value = targets(world)[0], f"2026-11-{number:02d}"
    case_id, iid = bc_intent(world, target, value)
    world.authorize(iid, f"TASK-TWIN-BC-{number}")
    url = odata_path(world.resolve(target, ACTOR))
    cited_etag = world.transport.send(OwnerRequest("bc", "GET", url))["body"]["@odata.etag"]
    stale = None
    if name == "edited_after_approval":   # another BC user saves the order
        world.transport.send(OwnerRequest("bc", "PATCH", url, {"If-Match": cited_etag},
                                          {"purchaser": "another BC user"}))
        stale = world.transport.send(OwnerRequest("bc", "PATCH", url, {"If-Match": cited_etag},
                                                  {TARGET_FIELD: value}))["status"]
    elif name == "etag_mismatch":
        owner.faults.append("concurrent_write")
    calls, sent = owner.dispatch_calls, len(owner.door.answers)
    first = path.dispatch(ACTOR, iid, world.bc_registry, world.mc_client, session=SESSION)
    if path.get(ACTOR, iid)["state"] in ("dispatched", "dispatched_unknown"):
        path.read_back(ACTOR, iid, world.bc_read_back, session=SESSION)
    dispatched = owner.dispatch_calls - calls
    path.dispatch(ACTOR, iid, world.bc_registry, world.mc_client, session=SESSION)  # replay
    action = owner.door.answers[sent:]   # the OData the action path sent
    after = world.transport.send(OwnerRequest("bc", "GET", url))["body"]
    view = world.new_path().view(ACTOR, iid)   # read back from disk by a new instance
    return {"scenario": name, "workbench_owner_only": name in WORKBENCH_OWNER_ONLY,
            "state": view["state"], "outcome": view["outcome"],
            "chain_ok": view["chain_ok"],
            "case_chain_ok": world.cases.verify_chain(ACTOR, case_id),
            "first_dispatch": first["state"], "first_reason": first.get("reason"),
            "owner_dispatch_calls": dispatched,
            "replay_owner_calls": owner.dispatch_calls - calls - dispatched,
            "read_back": [item["outcome"] for item in view["read_backs"]],
            "bc_shows_value": after[TARGET_FIELD] == value,
            "etag_moved": after["@odata.etag"] != cited_etag,
            "stale_if_match_status": stale,
            "action_odata": [f"{method} {status}" for method, status, _m in action],
            "action_if_match_cited": [if_match == cited_etag for method, _s, if_match
                                      in action if method == "PATCH"]}


def run_bc_lifecycle(state_dir: Path) -> dict[str, Any]:
    """Every ``SCENARIOS`` run on one fresh test-mode world."""
    state_dir = Path(state_dir)
    if state_dir.exists() and any(state_dir.iterdir()):
        raise ValueError("state_dir_not_empty: the run needs a fresh directory")
    with mode.test_mode():
        world = TwinWorld(state_dir)
        rows = [run_bc_scenario(world, name, number)
                for number, name in enumerate(SCENARIOS, start=1)]
    return {"schema": SCHEMA, "evidence_label": g2.EVIDENCE_LABEL, "scenarios": rows,
            "bc_requests": _count(f"{method} {re.sub(r'[(][^)]*[)]', '', url)}"
                                  for system, method, url in world.transport.requests
                                  if system == "bc"),
            "owner_operations": _count(op["status"] for op in world.bc.owner.operations()),
            "mc_refusals": _count(world.mc.refusals)}


__all__ = ["SCENARIOS", "SCHEMA", "WORKBENCH_OWNER_ONLY", "bc_intent", "odata_path",
           "run_bc_lifecycle", "run_bc_scenario", "targets"]
