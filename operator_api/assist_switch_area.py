"""The person's own limits on Auto and "always do this" (``operating.assist_switches``),
for ``AutopilotAreaMixin`` (which it is a base of; it uses that mixin's ``_apa_own_ids``,
``_apa_opt_in_state``, ``_ap_visible`` and ``_area_read``). Only ever the signed-in person's
own; limits only narrow Auto, and nothing here sends or writes to a business system.
Evidence: isolated-fixture (``operator_api/tests/test_assist_switches_api.py``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from operating import autopilot_write as aw

from . import models as m
from .errors import UserFacing

from .auth import Reader


class AssistSwitchMixin:
    def autopilot_conditions(self, reader: Reader, body: m.AutopilotConditionsRequest
                             ) -> m.AutopilotArea:
        """The reader sets the limits on one category's auto (senders, days, hours, their
        own bar). Only ever their own; limits only narrow auto (``assist_switches``)."""
        from operating import assist_modes as am
        from operating import assist_switches
        ids = self._apa_own_ids(reader)  # type: ignore[attr-defined]
        try:
            am.category(body.choice)
        except ValueError:
            raise UserFacing("not_found", "No such choice.", 404) from None
        try:
            cond = assist_switches.parse(body.model_dump(exclude={"choice"}))
        except ValueError as error:
            raise UserFacing("bad_request", str(error), 422) from None
        state, gate, _journal = self._apa_opt_in_state()  # type: ignore[attr-defined]
        if cond.min_percent is not None and cond.min_percent < gate.precision * 100:
            raise UserFacing("bad_request", "Your bar can only be stricter than the one "
                             f"auto already needs ({gate.precision * 100:.0f}%).", 422)
        assist_switches.write(state, ids, body.choice, cond, datetime.now(timezone.utc))
        return self._area_read(reader)  # type: ignore[attr-defined]

    def autopilot_always(self, reader: Reader, item_id: str, body: m.AutopilotAlwaysRequest
                         ) -> m.AutopilotArea:
        """"Always do this": the item's category goes to auto for the reader, for every
        sender (``category``) or for this item's sender only (``sender``: added to the
        category's senders, or the only one when it was not on auto). Only where their
        replay score earned auto (409 otherwise); the rules still hold money, flagged or
        incomplete items for the click."""
        from operating import assist_modes as am
        from operating import assist_switches
        from operating import workflows as wf
        ids = self._apa_own_ids(reader)  # type: ignore[attr-defined]
        found = next((i for i in self._ap_visible(reader) or ()  # type: ignore[attr-defined]
                      if i["item_id"] == item_id and i["mine"]), None)
        if found is None:
            raise UserFacing("not_found", "That item is not on your list.", 404)
        flow = wf.of_item(str(found.get("autopilot")),
                          (found.get("body") or {}).get("happened") or {})
        choice = assist_switches.choice_of(flow)
        if choice is None:
            raise UserFacing("action_refused", "This kind of item has no switch.", 409)
        sender = assist_switches.sender_of(found)
        if body.scope == "sender" and "@" not in sender:
            raise UserFacing("action_refused", "This item has no sender to choose.", 409)
        _label, flows = am.category(choice)
        state, gate, journal = self._apa_opt_in_state()  # type: ignore[attr-defined]
        opted = aw.opt_ins(journal.rows())
        was = am.mode_of(flows, {}, opted, ids)
        now = datetime.now(timezone.utc)
        # earned for this item's own work, not only somewhere in its category
        if not any(aw.eligible(gate, p, flow) for p in ids):
            raise UserFacing("action_refused", _not_eligible(), 409)
        from operating.card_actions import JournalBroken
        try:
            kept = assist_switches.read(state, ids)
        except (OSError, ValueError, JournalBroken):
            kept = {choice: None}
        if choice in kept and kept[choice] is None:
            # never replace limits that cannot be read with none: that would widen auto
            raise UserFacing("action_refused", "Your limits on this switch can't be read "
                             "now, so it was left as it is.", 409)
        prior = kept.get(choice)
        base = prior if prior is not None else assist_switches.Conditions()
        if body.scope == "sender":
            senders = (base.senders if was == am.AUTO else ()) + (sender,)
            if was == am.AUTO and not base.senders:
                senders = ()       # already auto for every sender: nothing to narrow
            cond = assist_switches.Conditions(tuple(dict.fromkeys(senders)), base.days,
                                              base.start, base.end, base.min_percent)
        else:
            cond = assist_switches.Conditions((), base.days, base.start, base.end,
                                              base.min_percent)
        # the limits first, then auto: a failure in between leaves auto narrower, never wider
        if cond != base or prior is None:
            assist_switches.write(state, ids, choice, cond, now)
        try:
            am.apply(journal, gate, ids, flows, am.AUTO, now, opted=opted)
        except ValueError:
            raise UserFacing("action_refused", _not_eligible(), 409) from None
        return self._area_read(reader)  # type: ignore[attr-defined]

    @staticmethod
    def _apa_with_switch(state: Optional[Path], me: set[str], raw: Mapping[str, Any],
                         out: list[m.AutopilotPrepared]) -> list[m.AutopilotPrepared]:
        """Each of the reader's own items with the switch it falls under and whether their
        replay score earned Auto for its work (``always_ok``); a passed-on item gets none."""
        from operating import assist_switches
        from operating import workflows as wf
        labels = {cid: label for cid, label, _flows in aw.DRAFT_CHOICES}
        gate = None
        if state is not None:
            try:
                gate = aw.WriteGate(state / aw.WRITE_GATE_FILE, aw.load_rules())
            except aw.RulesInvalid:
                gate = None
        done: list[m.AutopilotPrepared] = []
        for shown in out:
            item = raw.get(shown.item_id)
            if item is None or not item.get("mine"):
                done.append(shown)
                continue
            try:
                flow = wf.of_item(str(item.get("autopilot")),
                                  (item.get("body") or {}).get("happened") or {})
                cid = assist_switches.choice_of(flow) or ""
                ok = bool(cid and gate is not None
                          and any(aw.eligible(gate, p, flow) for p in me))
            except Exception:  # noqa: BLE001 - unknown: no switch offered
                cid, ok = "", False
            done.append(shown.model_copy(update={
                "category": cid, "category_label": labels.get(cid, ""), "always_ok": ok}))
        return done



def own_conditions(state: Any, me: Any) -> dict[str, Any]:
    """category -> the reader's own auto limits; {} when none or unreadable (the dry run
    and the executor hold auto back on an unreadable row themselves)."""
    from operating import assist_switches
    from operating.card_actions import JournalBroken
    try:
        found = assist_switches.read(Path(state) if state else None, me)
    except (OSError, ValueError, JournalBroken):
        return {}
    return {k: v for k, v in found.items() if v is not None}


def _not_eligible() -> str:
    from .autopilot_area import NOT_ELIGIBLE
    return NOT_ELIGIBLE
