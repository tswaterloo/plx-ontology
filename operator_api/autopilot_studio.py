"""The autopilot studio's reads and its mode switch (``/api/autopilot/workflows/{id}``,
``/api/autopilot/catalog``, ``/api/autopilot/schedules``, ``PUT .../{id}/mode``).

Every read is cut from ``autopilot_area`` as the reader may see it (a reader who is not the
owner sees only their own roles' rows and items) plus the autopilot registry and the
workflow table: nothing here reads a business system. A workflow's trigger, conditions and
actions are its registered autopilots' (``operating/autopilot_registry.json``) through
``operating/workflows.json``'s ``autopilot_workflow`` table.

Modes on this server: off, draft and auto (with approval) are the existing switches
(``autopilot_workflow``: the per-workflow off switch and the per-role live flag after the
fresh gate). Suggest and watch, a kept drafted workflow and a replay of a draft are the
workflow builder's; until it lands they are refused with 409, never faked.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from operating import autopilot as ap
from operating import autopilot_write as aw
from operating import workflows as wf

from . import models as m
from .auth import Reader
from .errors import UserFacing

SERVER_MODES: tuple[str, ...] = ("off", "draft", "auto")
NOT_BUILT = ("The workflow builder is not on this server yet: {what}. Off, Draft and Auto "
             "with approval work through the existing switches.")

TRIGGER_WORDS = {
    "message.received": "A new email or Teams message arrives",
    "record.due": "A record comes due (checked on each run)",
    "approval.requested": "An approval is requested in Mission Control",
    "task.assigned": "A task is assigned",
    "meeting.ended": "A meeting ends",
    "qc.result": "A QC result is recorded",
    "question.asked": "Someone asks a question",
    "schedule": "On a schedule",
}
CONDITION_WORDS = {
    "intents_any": "It asks for", "intents_none": "It is not about",
    "side_in": "It comes from", "object_type_in": "The record is a",
    "status_in": "Its status is", "quality_in": "The QC result is",
    "mailbox_role": "It reaches the mailbox of", "mailbox_role_none": "It does not reach",
    "assigned": "It is assigned to a person", "open": "It is still open",
    "asks": "It asks something", "closing": "It closes a thread", "ended": "The meeting ended",
    "engaged_or_asks": "The person is engaged or asked", "addressed_other": "It is addressed to "
    "someone else", "external_or_linked": "It is external or linked to a record",
    "organizer_copy": "It is the organizer's copy", "quiet_sender": "The sender is quiet",
    "tested": "It was tested", "to_me": "It is addressed to the person",
    "after_own_post": "It answers the person's own message in a conversation they keep writing in",
    "cc_min": "Several people are copied on it",
}
# conditions whose words run straight into their value ("The record is a purchase order")
PLAIN_JOIN = frozenset({"object_type_in", "status_in", "quality_in"})
ACTION_WORDS = {
    "order_entry": "Draft the sales order and a reply", "po_followup": "Draft a chase to the "
    "supplier", "invoice_send": "Draft the invoice email", "routine_update": "Draft the "
    "recurring update", "invoice_entry": "Prepare the invoice entry", "shipment_posting":
    "Prepare the shipment posting", "cash_application": "Prepare the cash application",
    "supplier_eta": "Update the expected date", "desk": "Prepare an answer",
    "answer_request": "Draft the answer with documents", "delay_impact": "Show the delay's "
    "impact and draft the replies", "approval_recommendation": "Recommend approve or hold",
    "task_prepare": "Prepare the task's first step", "ledger_answer": "Answer from the ledger",
    "qc_impact": "Show the QC hold's impact", "reply_prep": "Draft the reply",
    "meeting_followup": "Draft the follow-ups and action items", "question_prepare":
    "Prepare the answer",
}


def _humanize(key: str) -> str:
    return key.replace("_", " ").replace(".", " ").strip().capitalize()


def _value_words(value: Any) -> str:
    """A condition's value in words: "PurchaseOrder" -> "purchase order"."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_value_words(v) for v in value)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(value)).replace("_", " ").lower()


def steps_of(spec: Mapping[str, Any]) -> tuple[m.AutopilotStep, list[m.AutopilotStep],
                                                  list[m.AutopilotStep]]:
    """(trigger, conditions, actions) of one registered autopilot, in words."""
    on = str(spec.get("on") or "")
    trigger = m.AutopilotStep(kind=on, words=TRIGGER_WORDS.get(on, _humanize(on)),
                              params=dict(spec.get("params") or {}))
    conditions = []
    for key, value in (spec.get("when") or {}).items():
        lead = CONDITION_WORDS.get(key, _humanize(key))
        words = lead if isinstance(value, bool) and value else \
            f"{lead} {_value_words(value)}" if key in PLAIN_JOIN else f"{lead}: {_value_words(value)}"
        conditions.append(m.AutopilotStep(kind=str(key), words=words[:300],
                                          params={"value": value}))
    handler = str(spec.get("handler") or "")
    actions = [m.AutopilotStep(kind=handler, words=ACTION_WORDS.get(handler, _humanize(handler)),
                               params={"owner": spec.get("owner"),
                                       "due_minutes": spec.get("due_minutes")})]
    return trigger, conditions, actions


def mode_of(rows: list[m.AutopilotWorkflowRow]) -> str:
    """A workflow's mode from its rows: off when switched off, auto when any role's live
    flag is on, else draft."""
    if not rows or not all(r.on for r in rows):
        return "off"
    return "auto" if any(r.live_flag for r in rows) else "draft"


class AutopilotStudioMixin:
    """Needs ``AutopilotAreaMixin``."""

    def _studio_gate_rule(self) -> str:
        try:
            gate = aw.load_rules().get("gate") or {}
            pct = int(float(gate.get("precision") or 0.9) * 100)
            return f"{pct}% right on at least {int(gate.get('min_judged') or 15)} held-out items"
        except Exception:  # noqa: BLE001 - unreadable rules: nothing passes
            return "The write rules are unreadable: nothing passes."

    def _studio_specs(self, workflow: str) -> list[dict[str, Any]]:
        table = wf.load().get("autopilot_workflow") or {}
        return [s for s in ap.load_registry().get("autopilots") or ()
                if table.get(s.get("id")) == workflow or s.get("id") == workflow]

    def _studio_modes(self, passed: bool) -> list[str]:
        return list(SERVER_MODES)

    def autopilot_workflow_detail(self, reader: Reader, workflow: str
                                  ) -> m.AutopilotWorkflowDetail:
        area = self._area_read(reader)  # type: ignore[attr-defined]
        rows = [r for r in area.workflows if r.workflow == workflow]
        items = [i for i in area.items if i.workflow == workflow]
        specs = self._studio_specs(workflow)
        if not rows and not items and not (area.owner and specs):
            # a reader outside every role of it is told no more than an unknown name
            raise UserFacing("not_found", "No such workflow.", 404)
        trigger, conditions, actions = (None, [], [])
        notes: list[str] = []
        if specs:
            trigger, conditions, actions = steps_of(specs[0])
            for extra in specs[1:]:
                _, more_c, more_a = steps_of(extra)
                conditions += [c for c in more_c if c not in conditions]
                actions += [a for a in more_a if a not in actions]
            off = [s.get("label") or s.get("id") for s in specs if s.get("enabled") is False]
            if off:
                notes.append("Switched off in the registry: " + ", ".join(map(str, off)) + ".")
        else:
            notes.append("No registered autopilot feeds it: it is scored in replay only.")
        fresh = [r.fresh for r in rows if r.fresh]
        dev = [r.dev for r in rows if r.dev]
        scores = fresh or dev
        judged = sum(s.judged for s in scores)
        matched = sum(s.matched for s in scores)
        passed = any(r.state in ("passed_awaiting_owner", "live_held", "live") for r in rows)
        pct = round(matched * 100 / judged, 1) if judged else None
        history: list[m.AutopilotAuditRow] = []
        if area.owner:
            try:
                history = [r for r in self.autopilot_audit(reader).rows  # type: ignore[attr-defined]
                           if r.workflow == workflow][:30]
            except UserFacing:
                history = []
        return m.AutopilotWorkflowDetail(
            workflow=workflow, label=str((specs[0].get("label") if specs else None)
                                         or _humanize(workflow)),
            trigger=trigger, conditions=conditions, actions=actions,
            autopilots=[str(s.get("id")) for s in specs], mode=mode_of(rows),
            modes=self._studio_modes(passed),
            gate=m.AutopilotGate(percent=pct, judged=judged, matched=matched,
                                 fresh=bool(fresh), passed=passed,
                                 rule=self._studio_gate_rule()),
            rows=rows, caught=matched if scores else None,
            missed=judged - matched if scores else None, false_alarms=None,
            hours_saved=None, hours_basis="Not measured on this server yet.",
            open_items=sum(1 for i in items if i.state in ("waiting", "edited")),
            history=history, notes=notes)

    def autopilot_catalog(self, reader: Reader) -> m.AutopilotCatalog:
        specs = ap.load_registry().get("autopilots") or ()
        triggers = sorted({str(s.get("on")) for s in specs if s.get("on")})
        conditions = sorted({str(k) for s in specs for k in (s.get("when") or {})})
        handlers = sorted({str(s.get("handler")) for s in specs if s.get("handler")})
        return m.AutopilotCatalog(
            triggers=[m.AutopilotStep(kind=t, words=TRIGGER_WORDS.get(t, _humanize(t)))
                      for t in triggers],
            conditions=[m.AutopilotStep(kind=c, words=CONDITION_WORDS.get(c, _humanize(c)))
                        for c in conditions],
            actions=[m.AutopilotStep(kind=h, words=ACTION_WORDS.get(h, _humanize(h)))
                     for h in handlers],
            builder=False)

    def autopilot_schedules(self, reader: Reader) -> m.AutopilotSchedules:
        area = self._area_read(reader)  # type: ignore[attr-defined]
        table = wf.load().get("autopilot_workflow") or {}
        seen = {r.workflow for r in area.workflows} | {i.workflow for i in area.items}
        off = set(area.switches.workflows_off)
        out = []
        for spec in ap.load_registry().get("autopilots") or ():
            if spec.get("on") != "record.due":
                continue
            flow = str(table.get(spec.get("id")) or spec.get("id"))
            if not area.owner and flow not in seen:
                continue
            params = spec.get("params") or {}
            kinds = _value_words((spec.get("when") or {}).get("object_type_in") or ())
            window = params.get("window_days") or params.get("lookback_days")
            checks = (kinds.capitalize() + " records" if kinds else "Records") + \
                (f" from the last {window} days" if window else "")
            if params.get("after_days"):
                checks += f", {params['after_days']} days after they were sent"
            last = max((r.last_run for r in area.workflows if r.workflow == flow and r.last_run),
                       default=None)
            out.append(m.AutopilotSchedule(
                id=str(spec.get("id")), label=str(spec.get("label") or spec.get("id")),
                workflow=flow, cadence="Each autopilot run", checks=checks,
                on=spec.get("enabled") is not False and flow not in off and not
                area.switches.global_off, last_run=last, next_run=None))
        return m.AutopilotSchedules(schedules=out, notes=[] if out else [
            "No scheduled workflow is registered."])

    def autopilot_set_mode(self, reader: Reader, workflow: str, body: m.AutopilotModeRequest
                           ) -> m.AutopilotWorkflowDetail:
        """Off, draft and auto through the existing owner-only switches (auto only after the
        fresh gate passed for the role); suggest and watch are refused until the builder."""
        if body.mode not in SERVER_MODES:
            if not self._apa_is_owner(reader):  # type: ignore[attr-defined]
                raise UserFacing("forbidden", "Only the owner changes a workflow's mode.", 403)
            raise UserFacing("action_refused", NOT_BUILT.format(
                what=f"{body.mode.capitalize()} mode is not available"), 409)
        if body.mode == "off":
            self.autopilot_workflow(reader, workflow, m.AutopilotWorkflowSwitch(on=False))  # type: ignore[attr-defined]
        else:
            role = body.role
            if not role:
                rows = [r for r in self._area_read(reader).workflows  # type: ignore[attr-defined]
                        if r.workflow == workflow]
                if len(rows) != 1:
                    raise UserFacing("bad_request", "Say which role's mode changes.", 422)
                role = rows[0].role
            # the live flag first: a refused auto (gate not passed) changes nothing
            self.autopilot_workflow(reader, workflow, m.AutopilotWorkflowSwitch(  # type: ignore[attr-defined]
                live=body.mode == "auto", role=role))
            self.autopilot_workflow(reader, workflow, m.AutopilotWorkflowSwitch(on=True))  # type: ignore[attr-defined]
        return self.autopilot_workflow_detail(reader, workflow)

    def autopilot_draft(self, reader: Reader, body: m.AutopilotWorkflowDraft
                        ) -> m.AutopilotDraftKept:
        if not self._apa_is_owner(reader):  # type: ignore[attr-defined]
            raise UserFacing("forbidden", "Only the owner drafts a workflow.", 403)
        raise UserFacing("action_refused", NOT_BUILT.format(what="the draft was not kept"), 409)

    def autopilot_replay(self, reader: Reader, workflow: str, body: m.AutopilotReplayRequest
                         ) -> m.AutopilotReplay:
        if not self._apa_is_owner(reader):  # type: ignore[attr-defined]
            raise UserFacing("forbidden", "Only the owner replays a workflow.", 403)
        raise UserFacing("action_refused", NOT_BUILT.format(
            what="a replay on past days runs in the nightly scorer only"), 409)

