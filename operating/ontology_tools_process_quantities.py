"""Events and quantities of ``ProcessTools``: dated events and the quantity and money
checks along a process.

Split from ``operating/ontology_tools_process.py``; moved unchanged.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Mapping, MutableMapping, Sequence
from ontology.units import is_unit_price, same_denominator, unit_token, value_basis
from operating.ontology_tools import _value_out
from operating.ontology_tools_identity import _gap
from operating.ontology_tools_policy import _IDENTITY_TIERS, _format_time
from operating.ontology_tools_results import unit_out, unit_text, units_text
from .ontology_tools_process_graphs import (
    NOT_REACHED, OUT_OF_ORDER, REACHED, SKIPPED, UNKNOWN, _link_tier,
)
from .ontology_tools_process_answer import (
    UNCHECKED, _CAUSE_DETAIL, _Ctx, _LEAD, _Outcome, _UNCAPTURED, _UNPROVEN, _WITHHELD,
    _bc_record, _timestamp,
)
from .ontology_tools_process_helpers import (
    ABSENT_IN_BC, _ATTRIBUTION, _COUNTED, _DELETED, _DROPPED, _HELD_GAPS, _IN_PROGRESS,
    _LEAD_ONLY, _LINELESS, _MISSING, _NOT_ATTRIBUTABLE, _NO_RECORD, _OK, _cite,
    _cite_rows, _fmt, _line_total, _one_unit, _present, _quantity_verdict, _side_cause,
    _unique,
)


class _QuantitiesMixin:
    """Methods of ``ProcessTools`` (``ontology_tools_process_quantities``); mixed in there, never used alone."""

    # ------------------------------------------------------------------ events

    def _visible_events(self, ctx: _Ctx, ids: Sequence[str], types: Sequence[str]
                        ) -> list[tuple[str, str, Any]]:
        if not ids or not types:
            return []
        rows = list(self.base.store.conn.execute(
            "SELECT e.*, eo.object_id AS subject FROM events e JOIN event_objects eo "
            "ON eo.event_id = e.event_id WHERE eo.object_id IN (SELECT value FROM json_each(?)) "
            "AND e.event_type IN (SELECT value FROM json_each(?)) ORDER BY e.event_id",
            (json.dumps(sorted(set(ids))), json.dumps(sorted(set(types))))))
        rows = [r for r in rows if ctx.row_visible(r)
                and self.base._item_allowed(ctx.actor, r["l1_handle"])]
        if not rows:
            return []
        related: dict[str, set[str]] = {}
        for row in self.base.store.conn.execute(
                "SELECT event_id, object_id FROM event_objects WHERE event_id IN "
                "(SELECT value FROM json_each(?))",
                (json.dumps(sorted({r["event_id"] for r in rows})),)):
            related.setdefault(row[0], set()).add(row[1])
        seen = ctx.visible({o for objs in related.values() for o in objs})
        return [(r["event_id"], r["subject"], r) for r in rows
                if related.get(r["event_id"], set()) <= set(seen)]

    def _events(self, ctx: _Ctx, ids: Sequence[str], types: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(eid for eid, _, _ in self._visible_events(ctx, ids, types)
                                  if len(eid) <= 128))

    def _has_event(self, ctx: _Ctx, rid: str, types: Sequence[str]) -> bool:
        return bool(self._visible_events(ctx, [rid], types))

    # ------------------------------------------------------------------ quantities

    def _side_lines(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                    side: Mapping[str, Any], paths: Sequence[Sequence[Mapping]], floor: str
                    ) -> dict[str, Any]:
        """The side's lines along ``paths``, grouped by the item each one links to
        ("" = no single visible item). Lines reached only through a record the side
        ignores (a cancelled header) are dropped; lines it cannot attribute to the
        root (a link the side declares not attributable, a header whose status is
        unreadable) and lines the source no longer holds (``missing_from_source``: not
        proof of deletion; ``deleted_in_source``) are kept apart in ``held``: cited, never
        summed."""
        side_type = self.base.schema.object_types.get(side["object_type"], {})
        reached = self._paths(ctx, graph["root_type"], oid, paths, floor,
                              spec_marks=side_type.get("markings", ()),
                              final_bindings={b["id"] for b in side_type.get("bindings", ())},
                              side=side)
        lines = {r: v for r, v in reached.records.items()
                 if ctx.obj(r)["object_type"] == side["object_type"]
                 and reached.states.get(r, _OK) not in _DROPPED}
        groups: dict[str, list[str]] = {}
        held: dict[str, list[str]] = {}
        pending: dict[str, list[str]] = {}  # lines of in-progress records, never summed
        held_states: dict[str, list[str]] = {}  # state -> held lines
        links_ok = True
        causes = reached.causes
        waiting = {r: v for r, v in reached.records.items()
                   if ctx.obj(r)["object_type"] == side["object_type"]
                   and reached.states.get(r, _OK) == _IN_PROGRESS}
        for rid in sorted(lines) + sorted(waiting):
            version = lines.get(rid) or waiting[rid]
            ctx.note_source(version)
            items = self._hop(ctx, {rid: version},
                              {"link": side["group_link"], "direction": "forward"}, floor, ())
            keys = sorted(items.records)
            key = keys[0] if len(keys) == 1 else ""
            if rid in waiting:  # only named in notes: never decides anything
                pending.setdefault(key, []).append(rid)
                continue
            # a hidden item link would regroup the line for a full-grant reader
            links_ok = links_ok and (items.proven or not items.applicable)
            if items.applicable and not items.proven:
                causes |= items.causes or {_UNPROVEN}
            state = reached.states.get(rid, _OK)
            removal = ctx.removal(rid) if state == _OK else None
            if removal is not None:  # a line its source no longer holds is never summed
                state = _DELETED if removal == "source_soft_delete" else _MISSING
            bucket = groups if state == _OK else held
            if state != _OK:
                held_states.setdefault(state, []).append(rid)
            bucket.setdefault(key, []).append(rid)
        # A lead row is never counted; ``_quantity_check`` classifies its target over
        # all the side's sources. A source with lead bindings but no lead row stays
        # complete.
        complete = reached.complete_but_leads and links_ok
        return {"lines": lines, "groups": groups, "held": held, "held_states": held_states,
                "pending": pending,
                # a lead to a record the side would drop anyway (cancelled, in progress)
                # could never count, so it is no lead of the side
                "lead_targets": {target for _, target in reached.leads
                                 if self._through(ctx, side, target) not in _DROPPED},
                "in_progress": sorted(
                    r for r, s in reached.counted.items() if s == _IN_PROGRESS
                    and self._through(ctx, side, r) == _IN_PROGRESS),
                "counted": set(reached.counted), "lineless": sorted(reached.lineless),
                "applicable": reached.applicable, "complete": complete,
                "causes": causes if not complete else set()}

    def _quantity_check(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                        check: Mapping[str, Any], outcomes: Mapping[str, _Outcome],
                        floor: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                             list[dict[str, Any]], list[dict[str, Any]]]:
        """(checks, conflicts, side gaps, per-item gaps). Each side reads one system of record,
        the one its stage was read from (``_Outcome.system``, decided once by
        ``_twin_decides``): its own ``paths``, or ``fallback_paths`` (the twin
        system) only when the stage's own records are proven none (a header
        without lines is a record: it keeps the own system and leaves the side
        undecided; a draft is a record for both). Both are never added; when both have
        lines and their per-item sums differ, that is a conflict, reported, never
        resolved. Every record the side reaches is classified once, over all its
        sources (module docstring): only ``lead_only`` records block as leads."""
        group_type = check["group_by"]["object_type"]
        sides: dict[str, dict[str, Any]] = {}
        twins: dict[str, dict[str, Any]] = {}
        for side in check["sides"]:
            own = self._side_lines(ctx, graph, oid, side, side["paths"], floor)
            twin = (self._side_lines(ctx, graph, oid, side, side["fallback_paths"], floor)
                    if side.get("fallback_paths") else None)
            sources = [own] + ([twin] if twin is not None else [])
            # Permission monotonicity: the side is decided only when every source a
            # full-grant reader could see is proven complete for this reader (own and
            # twin records, their lines and item links); otherwise nothing on it is.
            proven = (all(src["complete"] for src in sources)
                      and any(src["applicable"] for src in sources))
            causes = set().union(*(src["causes"] for src in sources))
            if not proven and not causes:
                causes.add(_UNPROVEN)
            chosen, origin = own, "own"
            if twin is not None and outcomes[side["stage"]].system == "twin":
                chosen, origin = twin, "twin"  # the stage's own system holds no record
            elif twin is not None and own["lines"] and twin["lines"]:
                twins[side["role"]] = twin
            classes = self._record_classes(sources, chosen)
            sides[side["role"]] = dict(chosen, spec=side, origin=origin, proven=proven,
                                       causes=causes, unchecked=False,
                                       # BC alone may decide it (module docstring)
                                       own_alone=(origin == "own" and own["complete"]
                                                  and own["applicable"]),
                                       foreign=twin["causes"] if twin is not None else set(),
                                       stage=outcomes[side["stage"]].state,
                                       stage_reason=outcomes[side["stage"]].reason,
                                       stage_demoted=outcomes[side["stage"]].demoted,
                                       lead_only=sorted(r for r, c in classes.items()
                                                        if c == _LEAD_ONLY),
                                       lineless=sorted(r for r, c in classes.items()
                                                       if c == _LINELESS),
                                       twin_withheld=origin == "own" and bool(
                                           twin and twin["lines"]) and not own["lines"])
        keys = {k for s in list(sides.values()) + list(twins.values())
                for k in list(s["groups"]) + list(s["held"]) if k}
        canon = self._identity_groups(ctx, check["group_by"].get("identity_link"), keys)
        identity = self._identity_causes(ctx, check["group_by"].get("identity_link"), keys,
                                         floor)
        if identity:
            for side in sides.values():
                side["proven"] = False
                side["causes"] = side["causes"] | identity
        found = self._binding(ctx.obj(oid)["binding_id"])
        if found is not None and found[1]["source"] == "bc":
            for side in sides.values():
                if not side["proven"] and side["own_alone"] \
                        and side["foreign"] | identity <= {_UNCAPTURED}:
                    side["proven"] = side["unchecked"] = True
        for side in list(sides.values()) + list(twins.values()):
            for bucket in ("groups", "held", "pending"):
                merged: dict[str, list[str]] = {}
                for key, rids in side[bucket].items():
                    merged.setdefault(canon.get(key, key), []).extend(rids)
                side[bucket] = merged
        drafts = [_gap("unknown", "side_draft", role,
                       f"the {role} side's stage {side['spec']['stage']} is not reached by its "
                       "records' status (a draft is not a business commitment), so no "
                       "comparison on it is decided")
                  for role, side in sorted(sides.items())
                  if side["stage"] == NOT_REACHED and side["stage_reason"] == "status_not_reached"]
        unproven = drafts
        for role, side in sorted(sides.items()):
            if side["unchecked"]:
                unproven.append(_gap("not_ingested", UNCHECKED, role, (
                    f"the {role} side is decided from its BC records alone: the twin "
                    "system (Portal) was not captured, so its twin records and item "
                    "identities were not read and the cross-system conflict check did not "
                    "run")[:600]))
            elif not side["proven"]:
                cause = _side_cause(side)
                unproven.append(_gap("unknown", cause, role, (
                    f"the {role} side's evidence (its own and twin records, their lines "
                    f"and item links) {_CAUSE_DETAIL[cause]}, so no comparison on it is "
                    "decided")[:600]))
        unproven += [_gap("unknown", _HELD_GAPS[state], role,
                          self._held_detail(side, state))
                     for role, side in sorted(sides.items())
                     for state in sorted(side["held_states"], key=_ATTRIBUTION.index)]
        for role, side in sorted(sides.items()):
            if side["in_progress"]:
                rule = next(r for r in side["spec"]["ignore_through"] if r.get("count_only")
                            and r["object_type"] == ctx.obj(side["in_progress"][0])["object_type"])
                unproven.append(_gap(
                    "unknown", "in_progress_not_counted", role,
                    (f"{len(side['in_progress'])} record(s) on the {role} side's path are in "
                     f"progress (status not {' or '.join(rule['values'])}: "
                     + ", ".join(side["in_progress"][:3]) + f"): not yet {role}, so their "
                     f"lines are never counted as {role}; a difference they cover is open "
                     "work, not a shortage claim")[:600]))
            # Seen only by a reader proven to see the whole side: a reader missing the
            # counted path could take a counted record for a lead-only one.
            if side["lead_only"] and side["proven"]:
                note = side["spec"].get("lead_note")
                unproven.append(_gap(
                    "unknown", "lead_evidence_not_counted", role,
                    (f"{len(side['lead_only'])} record(s) reach the {role} side only through "
                     "links below the tier floor (a text match, never counted in a sum: "
                     + ", ".join(side["lead_only"][:3]) + ")"
                     + (f"; {note}" if note else "")
                     + "; no comparison on this side is decided")[:600]))
            if side["lineless"]:
                unproven.append(_gap(
                    "unknown", "header_without_lines", role,
                    (f"{len(side['lineless'])} record(s) on the {role} side's path have no "
                     "captured line (" + ", ".join(side["lineless"][:3]) + "), so a missing "
                     "line is never read as zero"
                     + ("; the twin system's lines are not used in its place"
                        if side["twin_withheld"] else "")
                     + "; no comparison on this side is decided")[:600]))
        conflicts = [c for role, twin in sorted(twins.items())
                     for c in self._cross_system(ctx, check, role, sides[role], twin)]
        conflicts += [c for role, side in sorted(sides.items())
                      for c in self._mixed_systems(ctx, check, role, side)]
        conflicted = {(c["role"], c["group"]) for c in conflicts}
        mixed = {(c["role"], c["group"]) for c in conflicts if c.get("mixed")}
        recorded = {(r["role"], r["group"]): r for role, side in sorted(sides.items())
                    if side["spec"].get("recorded_on")
                    for r in self._recorded(ctx, check, role, side,
                                            sides[side["spec"]["recorded_on"]["role"]])}
        conflicts += [r for r in recorded.values() if r["conflict"]]
        # per-item gaps, reported after the side-level ones (``_run`` orders by severity)
        item_gaps = [r["gap"] for r in recorded.values() if not r["conflict"]]
        out: list[dict[str, Any]] = []
        for cmp in check["compare"]:
            a, b = (sides[r] for r in cmp["roles"])
            # (role recorded on, role recorded) when this compares a side with its figure
            pair = next(((on, rec) for rec, side in sides.items()
                         for on in [side["spec"].get("recorded_on", {}).get("role")]
                         if on and {on, rec} == set(cmp["roles"])), None)
            for group in sorted(set(a["groups"]) | set(b["groups"])):
                item = self._compare(ctx, cmp, a, b, group, group_type)
                gap = item.pop("_gap", None) if item is not None else None
                entry = recorded.get((pair[1], group)) if pair else None
                by_figure = None
                if gap and gap["reason"] == "unit_mismatch" and entry \
                        and cmp["measure"] == "quantity" and not any(
                            (r, group) in conflicted for r in cmp["roles"]):
                    by_figure = self._by_figure(ctx, cmp, sides[pair[0]], sides[pair[1]],
                                                group, entry)
                if gap:
                    item_gaps.append(gap)
                if by_figure is not None:
                    item = by_figure
                hit = [r for r in cmp["roles"] if (r, group) in conflicted]
                if item is not None and hit and cmp["measure"] == "quantity":
                    item["agree"] = None
                    item["undecided_reason"] = "cross_system_conflict"
                    rest = item["note"].removeprefix(f"{group}: ")
                    if any((r, group) in mixed for r in hit):
                        rest = ""  # the side's sum adds several systems: never shown
                    item["note"] = (f"{group}: cross-system conflict on the {', '.join(hit)} "
                                    "side (see gaps): not decided" + (f"; {rest}" if rest
                                                                      else ""))[:600]
                measured = [recorded[(r, group)] for r in cmp["roles"] if (r, group) in recorded]
                if item is not None and measured and cmp["measure"] == "quantity" \
                        and by_figure is None:
                    # two measures of one quantity disagree or cannot be matched: never
                    # pick one, so nothing on that quantity is decided. The entry is
                    # recorded once, as the gap whose subject is its id (role and item);
                    # items only name it, so a large order still fits one response.
                    item["agree"] = None
                    found = [m["gap"]["reason"] for m in measured if m["conflict"]]
                    if found:  # a contradiction in the evidence comes first
                        item["undecided_reason"] = found[0]
                    else:
                        item.setdefault("undecided_reason", measured[0]["gap"]["reason"])
                    item["note"] = (f"{group}: not decided: " + "; ".join(
                        f"gap {m['gap']['reason']} '{m['role']} <item>'" for m in measured)
                        + "; " + item["note"].removeprefix(f"{group}: "))[:600]
                if item is not None:
                    if item["agree"] is None:   # undecided: no figure stated as settled
                        for key in ("sums", "difference", "unit"):
                            item.pop(key, None)
                    out.append({"check": check["id"], "group": group or None, "item": item,
                                "finding": item["agree"] is False})
        return out, conflicts, unproven, item_gaps

    def _recorded_absence(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                          outcomes: MutableMapping[str, _Outcome], floor: str) -> None:
        """Rule C: a stage proven to have no record, whose quantity
        side the upstream system also records on another side's lines (``recorded_on``),
        stays proven absent only when this reader reads every such figure and each is
        zero. A non-zero figure makes it unknown (``recorded_measure_conflict``, cited);
        a figure this reader cannot read in full makes it unknown too
        (``recorded_measure_unproven``): a full-grant reader might see the conflict.
        Runs before the requirements pass, so stages requiring it are undecided."""
        for check in graph.get("quantity_checks", ()):
            by_role = {side["role"]: side for side in check["sides"]}
            for side in check["sides"]:
                declared, outcome = side.get("recorded_on"), outcomes.get(side["stage"])
                if not declared or outcome is None or outcome.state != NOT_REACHED \
                        or outcome.reason not in _NO_RECORD:
                    continue
                on = by_role[declared["role"]]
                sources = [self._side_lines(ctx, graph, oid, on, paths, floor)
                           for paths in [on["paths"]] + ([on["fallback_paths"]]
                                                         if on.get("fallback_paths") else [])]
                tid, name = on["object_type"], declared["quantity"]
                prop = self._prop(tid, name) or {}
                carriers = {s["binding"] for s in prop.get("sources", ())}
                marks = set(self.base.schema.object_types[tid].get("markings", ()))
                rows = [r for src in sources for rid in sorted(src["lines"])
                        if ctx.obj(rid)["binding_id"] in carriers
                        for r in _present(ctx, rid, name)]
                try:
                    nonzero = [r for r in rows if Decimal(r["value"]) != 0]
                    readable = all(src["complete"] for src in sources) \
                        and ctx.scope.holds(marks | set(prop.get("markings", ())))
                except (InvalidOperation, TypeError):
                    nonzero, readable = [], False
                if readable and not nonzero:
                    continue
                outcome.demoted, outcome.state = outcome.reason, UNKNOWN
                outcome.gap_state = "unknown"
                if nonzero:
                    outcome.reason = "recorded_measure_conflict"
                    outcome.detail = (
                        f"no {side['role']} record exists, yet {tid}.{name} records "
                        + ", ".join(f"{r['value']} {unit_text(r['unit'])}" if r["unit"]
                                    else r["value"] for r in nonzero[:3])
                        + f" {side['role']} (" + ", ".join(r["object_id"] for r in nonzero[:3])
                        + "): two measures disagree, so the absence is not proven")[:600]
                    outcome.citations += [c for c in (_cite(r) for r in nonzero[:10]) if c]
                else:
                    outcome.reason = "recorded_measure_unproven"
                    outcome.detail = (f"no {side['role']} record exists, but {tid}.{name}, "
                                      "which records it too, is not fully readable for this "
                                      "reader, so the absence is not proven")
        self._absent_in_bc(ctx, graph, oid, outcomes, floor)

    def _absent_in_bc(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                      outcomes: MutableMapping[str, _Outcome], floor: str) -> None:
        """``absent_by``: the
        stage of a BC-rooted record whose own BC records hold none (proven, or unknown
        only because its twin's system was never captured: ``past_uncaptured``) is
        ``absent_in_bc`` when BC's own figure proves it: this reader reads every line of
        the declared side along the root's own paths completely (no line held, in
        progress or hidden), each line carries the BC figure and every figure is zero,
        and the reads have a read time (``_bc_read_time``). Past an uncaptured twin it is
        ``unchecked`` (a ``cross_system_unchecked`` gap). Anything less leaves the stage
        as it was, except that a proven absence beside a non-zero BC figure becomes
        ``recorded_measure_conflict`` and beside an unreadable one
        ``recorded_measure_unproven`` (rule C)."""
        root, found = _bc_record(ctx.obj(oid)), self._binding(ctx.obj(oid)["binding_id"])
        if root is None or found is None or found[1]["source"] != "bc":
            return
        stages = {stage["id"]: stage for stage in graph["stages"]}
        done: set[str] = set()
        for check in graph.get("quantity_checks", ()):
            by_role = {side["role"]: side for side in check["sides"]}
            for side in check["sides"]:
                declared, outcome = side.get("absent_by"), outcomes.get(side["stage"])
                if not declared or outcome is None or side["stage"] in done:
                    continue
                proven = outcome.state == NOT_REACHED and outcome.reason in _NO_RECORD
                past = outcome.state == UNKNOWN and outcome.past_uncaptured
                if not (proven or past):
                    continue
                done.add(side["stage"])
                on = by_role[declared["role"]]
                tid, name = on["object_type"], declared["quantity"]
                prop = self._prop(tid, name) or {}
                carriers = {s["binding"] for s in prop.get("sources", ())
                            if (self._binding(s["binding"]) or ("", {}))[1].get("source")
                            == "bc"}
                src = self._side_lines(ctx, graph, oid, on, on["paths"], floor)
                lines = sorted(src["lines"])
                rows = [_present(ctx, rid, name) for rid in lines]
                flat = [r for found_rows in rows for r in found_rows]
                try:
                    figures = [Decimal(r["value"]) for r in flat]
                except (InvalidOperation, TypeError):
                    figures = None
                nonzero = [r for r, f in zip(flat, figures or ()) if f != 0]
                marks = set(self.base.schema.object_types[tid].get("markings", ())) \
                    | set(prop.get("markings", ()))
                read_at = self._bc_read_time(ctx, root, [
                    spec["object_type"] for spec in stages[side["stage"]].get("evidence", ())]
                    + [tid])
                if (ctx.now_answer and src["applicable"] and src["complete"]
                        and ctx.scope.holds(marks) and lines and figures is not None
                        and not nonzero and all(rows)
                        and all(ctx.obj(rid)["binding_id"] in carriers for rid in lines)
                        and not any(src[k] for k in ("held", "pending", "lineless"))
                        and read_at is not None):
                    outcome.state, outcome.gap_state = NOT_REACHED, "absent_in_source"
                    outcome.reason, outcome.demoted = ABSENT_IN_BC, None
                    outcome.unchecked = past
                    outcome.detail = (
                        f"no {side['role']} record of this BC record exists, and BC records "
                        f"{tid}.{name} 0 on all {len(lines)} of its line(s), read completely "
                        f"at {_format_time(read_at)}"
                        + ("; its twin system (Portal) was not captured, so its twin records "
                           "were not read" if past else ""))[:600]
                    outcome.citations = [c for c in (_cite(r) for r in flat[:10]) if c]
                    continue
                if not proven or not (nonzero or figures is None):
                    continue  # nothing contradicts the proven absence: it stands as it was
                outcome.demoted, outcome.state = outcome.reason, UNKNOWN
                outcome.gap_state = "unknown"
                if nonzero:
                    outcome.reason = "recorded_measure_conflict"
                    outcome.detail = (
                        f"no {side['role']} record exists, yet BC {tid}.{name} records "
                        + ", ".join(f"{r['value']} {unit_text(r['unit'])}" if r["unit"]
                                    else r["value"] for r in nonzero[:3])
                        + f" {side['role']} (" + ", ".join(r["object_id"] for r in nonzero[:3])
                        + "): two measures disagree, so the absence is not proven")[:600]
                    outcome.citations += [c for c in (_cite(r) for r in nonzero[:10]) if c]
                else:
                    outcome.reason = "recorded_measure_unproven"
                    outcome.detail = (f"no {side['role']} record exists, but a BC "
                                      f"{tid}.{name} figure is not one readable decimal, so "
                                      "the absence is not proven")

    def _bc_read_time(self, ctx: _Ctx, root: tuple[str, str], types: Sequence[str]
                      ) -> dt.datetime | None:
        """When BC's records of ``types`` for BC root ``root`` (company, id) were read:
        the BC capture time when the capture is complete and fresh, else the earliest
        start of the complete scoped reads covering the root, one per BC relation of
        those types (a company-wide claim or one anchored at the root). None when a
        relation has no such read with a read time: an absence is never proven
        undated."""
        fresh = ctx.freshness.get("bc")
        if fresh and fresh.get("completeness") == "complete" and not fresh.get("stale"):
            return _timestamp(fresh.get("captured_at"))
        times = []
        for tid in dict.fromkeys(types):
            for binding in self.base.schema.object_types.get(tid, {}).get("bindings", ()):
                if binding["source"] != "bc":
                    continue
                read = [c.read_at for c in ctx.claims.get(binding["relation"], ())
                        if c.complete and c.read_at is not None and c.company == root[0]
                        and (c.anchors is None or root[1] in c.anchors)]
                if not read:
                    return None
                times.append(min(read))
        return min(times) if times else None

    def _recorded(self, ctx: _Ctx, check: Mapping[str, Any], role: str,
                  side: Mapping[str, Any], on: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Per item, the figure the upstream system records for ``side`` on the lines
        of side ``on`` (``recorded_on``) against the side's own line sum: two measures
        of one quantity, both always read. A side proven to have no record sums to
        zero, so a non-zero figure beside it differs. A difference is a
        ``recorded_measure_conflict`` naming both (``kind`` conflict); a figure that
        cannot be matched (read in part, on a line of no single item, hidden from
        this reader, in a unit the lines' unit is no label of, or beside lines that
        cannot be established) is ``recorded_measure_unproven``. One entry per item,
        whose gap the check items reference by its subject (``id``); ``figure`` is
        the readable figure (sum, units), the measure ``_by_figure`` may use. Neither
        measure is ever chosen silently; nothing is converted."""
        declared = side["spec"]["recorded_on"]
        tid, name = on["spec"]["object_type"], declared["quantity"]
        prop = self._prop(tid, name) or {}
        carriers = {s["binding"] for s in prop.get("sources", ())}
        subject = f"{tid}.{name}"
        stage = side["spec"]["stage"]
        lines_known = (side["proven"] and side["stage"] in (REACHED, OUT_OF_ORDER)
                       and bool(side["lines"]))
        # proven to have no record at all: its sum is zero for every reader
        absent = (side["stage"] == NOT_REACHED
                  and side["stage_reason"] in _NO_RECORD + (ABSENT_IN_BC,)
                  or side["stage_demoted"] in _NO_RECORD)  # rule C kept the gap's figure

        def figure(rid: str) -> list[Any]:
            """The recorded figure of a line of ``on`` (none where its binding has no
            such column)."""
            if ctx.obj(rid)["binding_id"] not in carriers:
                return []
            return _present(ctx, rid, name)

        # a reader who may miss a line of ``on`` or a marking the projector puts on the
        # figure (its type's and its property's) cannot rule out a figure a full-grant
        # reader sees; decided from the policy alone, never from hidden rows
        marks = set(self.base.schema.object_types[tid].get("markings", ()))
        readable = on["proven"] and ctx.scope.holds(marks | set(prop.get("markings", ())))
        if not readable:
            if not lines_known:
                return []  # no figure is visible and the side is undecided already
            return [self._recorded_entry(
                check, role, group, "unproven",
                f"the {subject} figures are not all visible to this reader, so whether they "
                f"match the {role} lines is not established")
                for group in sorted((set(side["groups"]) | set(on["groups"])) - {""})]
        loose = [rid for bucket in ("groups", "held") for key, rids in on[bucket].items()
                 for rid in rids if (bucket == "held" or not key) and figure(rid)]
        out = []
        for group in sorted((set(side["groups"]) | set(on["groups"])) - {""}):
            lines = on["groups"].get(group, [])
            recorded = [figure(rid) for rid in lines]
            if not any(recorded) and not (loose and lines_known):
                continue  # the upstream system records nothing for this item
            cited = _cite_rows(ctx, recorded)
            got = _line_total(recorded) if all(recorded) else None
            said = (f"{tid} lines record {_fmt(got[0])} {_one_unit(got[1]) or '?'} {role} "
                    f"({name}: " + ", ".join(lines[:3]) + ")") if got else ""
            why = None
            if loose:
                why = (f"a {subject} figure sits on a line without one visible item or "
                       "attributable to the root (" + ", ".join(loose[:3]) + ")")
            elif not all(recorded):
                why = f"only some {tid} lines of this item record {name}"
            elif got is None:
                why = f"a {subject} figure is not one readable decimal"
            entry: dict[str, Any] | None = None
            alike_lines = False  # some side line is in the figure's unit (rule B)
            if why is None and absent:
                if got[0] != 0:
                    entry = self._recorded_entry(
                        check, role, group, "conflict",
                        f"{said}, yet no {role} record exists (stage {stage} is proven "
                        "absent): two measures of one quantity disagree; neither is chosen "
                        "and nothing on it is decided",
                        recorded=f"{_fmt(got[0])} {_one_unit(got[1]) or '?'}",
                        summed=f"no {role} line")
            elif why is None and not lines_known:
                if got[0] != 0:
                    why = (f"{said}, but the {role} lines could not be established (stage "
                           f"{stage} {side['stage']}), so the two are not compared")
            elif why is None:
                own = [_present(ctx, rid, side["spec"]["quantity"])
                       for rid in side["groups"].get(group, [])]
                summed = _line_total(own)
                unit = None if summed is None else _one_unit(got[1] | summed[1])
                if summed is None:
                    why = f"{said}, but a {role} line of this item has no single readable value"
                elif unit is None:
                    # rule B: lines in the figure's unit can check it;
                    # alone they already exceed it, or the item stays unmatched, never
                    # decided from the figure
                    alike = [rows for rows in own if _one_unit(got[1] | {rows[0]["unit"]})]
                    alike_sum = sum((Decimal(rows[0]["value"]) for rows in alike), Decimal(0))
                    alike_lines = bool(alike)
                    # zero against non-zero needs no conversion: it contradicts in any
                    # unit
                    received = any(Decimal(rows[0]["value"]) != 0 for rows in own)
                    if (got[0] == 0) == received:
                        cited += _cite_rows(ctx, own)
                        label, other = _one_unit(got[1]), units_text(summed[1])
                        entry = self._recorded_entry(
                            check, role, group, "conflict",
                            f"{said}; the {role} lines record "
                            + ("a non-zero quantity" if received else "only zero")
                            + f" ({other}): zero against non-zero disagrees in any unit; "
                            "neither is chosen and nothing on it is decided",
                            recorded=f"{_fmt(got[0])} {label}",
                            summed=("non-zero" if received else "0") + f" in {other}")
                    elif alike and alike_sum > got[0]:
                        cited += _cite_rows(ctx, alike)
                        label = _one_unit(got[1])
                        entry = self._recorded_entry(
                            check, role, group, "conflict",
                            f"{said}; the {role} lines in that unit alone sum to "
                            f"{_fmt(alike_sum)} {label}: two measures of one quantity "
                            "disagree; neither is chosen and nothing on it is decided",
                            recorded=f"{_fmt(got[0])} {label}",
                            summed=f"{_fmt(alike_sum)} {label} and more in other units")
                    else:
                        why = (f"{subject} is in {units_text(got[1])} and the "
                               f"{role} lines in {units_text(summed[1])}; no "
                               "conversion is inferred")
                elif got[0] != summed[0]:
                    cited += _cite_rows(ctx, own)
                    entry = self._recorded_entry(
                        check, role, group, "conflict",
                        f"{said}; the {role} lines sum to {_fmt(summed[0])} {unit}: two "
                        "measures of one quantity disagree; neither is chosen and nothing "
                        "on it is decided",
                        recorded=f"{_fmt(got[0])} {unit}", summed=f"{_fmt(summed[0])} {unit}")
            if why is not None:
                entry = self._recorded_entry(check, role, group, "unproven", why)
            if entry is not None:
                entry.update(figure=got, values=cited[:50], alike_lines=alike_lines,
                             figure_values=_cite_rows(ctx, recorded)[:10])  # ``_by_figure``
                out.append(entry)
        return out

    @staticmethod
    def _recorded_entry(check: Mapping[str, Any], role: str, group: str, kind: str,
                        detail: str, **said: str) -> dict[str, Any]:
        ident = f"{role} {group}"[:700]
        reason = "recorded_measure_conflict" if kind == "conflict" else "recorded_measure_unproven"
        return dict(said, check=check["id"], role=role, group=group, id=ident, kind=kind,
                    conflict=kind == "conflict", figure=None, values=[], figure_values=[],
                    alike_lines=False,
                    gap=_gap("unknown", reason, ident, detail[:600]))

    def _by_figure(self, ctx: _Ctx, cmp: Mapping[str, Any], on: Mapping[str, Any],
                   side: Mapping[str, Any], group: str, entry: Mapping[str, Any]
                   ) -> dict[str, Any] | None:
        """A comparison of ``on`` with ``side`` decided from the figure ``on``'s lines
        record for ``side`` (e.g. Portal's ordered vs quantity_received in purchase
        UoM), used only when every line of ``side``'s item is in a unit that is no
        label of the figure's (rule B). The item names its measure (``measured_by``);
        the caller keeps the gap saying the line sum was not comparable. None when the
        figure does not fit."""
        declared = side["spec"]["recorded_on"]
        if entry["kind"] != "unproven" or entry["alike_lines"] or entry["figure"] is None:
            return None  # rule B: never against lines that can check the figure
        rows = [_present(ctx, rid, on["spec"]["quantity"]) for rid in on["groups"].get(group, [])]
        mine = _line_total(rows)
        if mine is None:
            return None
        unit = _one_unit(mine[1] | entry["figure"][1])
        if unit is None:
            return None
        roles = list(cmp["roles"])
        by_role = {on["spec"]["role"]: mine[0], side["spec"]["role"]: entry["figure"][0]}
        sums = [by_role[r] for r in roles]
        agree, text = _quantity_verdict(cmp, roles, sums, unit)
        tid = on["spec"]["object_type"]
        note = (f"{group}: measured by {declared['measure_id']}: {roles[0]} {_fmt(sums[0])} "
                f"{unit} vs {roles[1]} {_fmt(sums[1])} {unit} as {tid}.{declared['quantity']} "
                f"records it; {text}; the {side['spec']['role']} lines' sum is in another unit "
                "and was not compared (see gaps), so this is not proven by those lines")
        values = _cite_rows(ctx, rows) + list(entry["figure_values"])
        return {"roles": roles, "values": _unique(values)[:50], "agree": agree,
                "note": note[:600], "measured_by": declared["measure_id"]}

    @staticmethod
    def _record_classes(sources: Sequence[Mapping[str, Any]], chosen: Mapping[str, Any]
                        ) -> dict[str, str]:
        """Each record a side reaches, classified once over all its sources (own and
        twin): ``counted`` when a link at or above the tier floor reaches it,
        ``header_without_lines`` when it is a counted last-hop record of the chosen
        system with no captured line, ``lead_only`` when only links below the floor
        reach it. A lead to a counted record is no lead of the side."""
        classes: dict[str, str] = {}
        for src in sources:
            classes.update(dict.fromkeys(src["counted"], _COUNTED))
        classes.update(dict.fromkeys(chosen["lineless"], _LINELESS))
        for src in sources:
            for rid in src["lead_targets"]:
                classes.setdefault(rid, _LEAD_ONLY)
        return classes

    def _mixed_systems(self, ctx: _Ctx, check: Mapping[str, Any], role: str,
                       side: Mapping[str, Any]) -> list[dict[str, Any]]:
        """One system of record per side: an item whose lines come from more than one
        source binding (e.g. a Sage and a BC invoice of the same order's twins) is a
        cross-system conflict, never a sum."""
        quantity = side["spec"]["quantity"]
        out = []
        for group, rids in sorted(side["groups"].items()):
            by: dict[str, list[str]] = {}
            for rid in rids:
                by.setdefault(ctx.obj(rid)["binding_id"], []).append(rid)
            if not group or len(by) < 2:
                continue
            said, values = [], []
            for binding, members in sorted(by.items()):
                rows = [r for rid in members for r in ctx.values(rid).get(quantity, [])]
                try:
                    total = _fmt(sum((Decimal(r["value"]) for r in rows), Decimal(0)))
                except (InvalidOperation, TypeError):
                    total = "unreadable"
                said.append(f"{binding} {total}")
                values += [v for r in rows for v in
                           [_value_out(r, ctx.obj(r["object_id"])["object_type"], 200)[0]] if v]
            out.append({"check": check["id"], "role": role, "group": group, "mixed": True,
                        "own": said[0], "twin": "; ".join(said[1:]), "values": values[:50],
                        "gap": _gap("unknown", "cross_system_conflict", f"{role} {group}"[:700],
                                    (f"{role}: lines from several systems ("
                                     + "; ".join(said) + "); not summed and not decided")[:600])})
        return out

    @staticmethod
    def _pending(ctx: _Ctx, side: Mapping[str, Any], group: str) -> str:
        """The quantity on a group's in-progress lines, as text ("4 each"), never summed
        into the side."""
        rows = [r for rid in side["pending"][group]
                for r in ctx.values(rid).get(side["spec"]["quantity"], [])]
        unit = _one_unit(r["unit"] for r in rows)
        try:
            total = sum((Decimal(r["value"]) for r in rows), Decimal(0))
        except (InvalidOperation, TypeError):
            return "an unreadable quantity"
        if not rows or unit is None:
            return "an unreadable quantity"
        return f"{_fmt(total)} {unit}"

    @staticmethod
    def _held_detail(side: Mapping[str, Any], state: str) -> str:
        count = len(side["held_states"][state])
        role = side["spec"]["role"]
        if state == _MISSING:
            text = (f"{count} {role} line(s) are no longer returned by the source (key-only "
                    "scan; not proof they were deleted: " + ", ".join(
                        side["held_states"][state][:3]) + "): cited, not summed, so no "
                    "comparison of their item is decided")
        elif state == _DELETED:
            text = (f"{count} {role} line(s) are deleted in the source (its own soft-delete "
                    "column: " + ", ".join(side["held_states"][state][:3]) + "): cited, not "
                    "summed, so no comparison of their item is decided")
        elif state == _NOT_ATTRIBUTABLE:
            why = "; ".join(b["reason"] for b in side["spec"].get("not_attributable", ()))
            text = (f"{count} {role} line(s) reach the root only through a link whose "
                    f"quantity is not attributable to it ({why}): cited, not summed")
        else:
            text = (f"{count} {role} line(s) reach the root through a record whose status "
                    "decides whether they count, and it is not readable: not summed")
        return text[:600]

    def _cross_system(self, ctx: _Ctx, check: Mapping[str, Any], role: str,
                      own: Mapping[str, Any], twin: Mapping[str, Any]) -> list[dict[str, Any]]:
        quantity = own["spec"]["quantity"]
        out = []
        held = set(own["held"]) | set(twin["held"])
        if "" in held:
            return out  # an unattributed line without an item could belong to any group
        for group in sorted((set(own["groups"]) | set(twin["groups"])) - {""} - held):
            sums = []
            for side in (own, twin):
                rids = side["groups"].get(group, [])
                per_line = [ctx.values(rid).get(quantity, []) for rid in rids]
                rows = [r for line in per_line for r in line]
                total: Any = "no line" if not rids else None
                if rids and all(len({r["value"] for r in line}) == 1 for line in per_line):
                    try:
                        total = sum((Decimal(line[0]["value"]) for line in per_line), Decimal(0))
                    except (InvalidOperation, TypeError):
                        total = None
                sums.append((total, {r["unit"] for r in rows}, rows))
            if isinstance(sums[0][0], Decimal) and sums[0][0] == sums[1][0] \
                    and _one_unit(sums[0][1] | sums[1][1]) is not None:
                continue
            values = [v for _, _, rows in sums for r in rows
                      for v in [_value_out(r, ctx.obj(r["object_id"])["object_type"], 200)[0]] if v]
            said = [_fmt(t) if isinstance(t, Decimal) else (t or "unreadable")
                    for t, _, _ in sums]
            units = units_text(sums[0][1] | sums[1][1])
            out.append({"check": check["id"], "role": role, "group": group,
                        "own": said[0], "twin": said[1], "values": values[:50],
                        "gap": _gap("unknown", "cross_system_conflict", f"{role} {group}"[:700],
                                    (f"{role}: the root's own system says {said[0]}, its twin "
                                     f"system says {said[1]} ({units}); not summed and not "
                                     "decided")[:600])})
        return out

    def _identity_causes(self, ctx: _Ctx, link_id: str | None, keys: set[str],
                         floor: str) -> set[str]:
        """Why an identity link a full-grant reader could see from these items may be
        hidden from this reader (``_CAUSES``; empty: none can be). Coverage only: a
        link's tier is the same for every reader."""
        if not link_id or not keys:
            return set()
        versions = ctx.visible(keys)
        causes: set[str] = set()
        for key in sorted(keys):
            if key not in versions:
                causes.add(_WITHHELD)
                continue
            for direction in ("forward", "reverse"):
                hop = self._hop(ctx, {key: versions[key]}, {"link": link_id,
                                                            "direction": direction}, floor, ())
                if hop.applicable:
                    causes |= {w.cause for w in hop.reasons if w.tier != _LEAD}
        return causes

    def _identity_groups(self, ctx: _Ctx, link_id: str | None, keys: set[str]) -> dict[str, str]:
        """Group key -> canonical key: items joined by a visible identity-tier
        ``link_id`` link (one hop out, so two records sharing a twin join)."""
        parent = {k: k for k in keys}
        if not link_id or not keys:
            return parent

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        rows = list(self.base.store.conn.execute(
            "SELECT * FROM links WHERE link_type = :link AND (from_object IN "
            "(SELECT value FROM json_each(:ids)) OR to_object IN (SELECT value FROM json_each(:ids)))",
            {"link": link_id, "ids": json.dumps(sorted(keys))}))
        ends = ctx.visible({r["from_object"] for r in rows} | {r["to_object"] for r in rows})
        # the tier as served (``_link_tier``: a link touching a never-authority row is a
        # lead), never the stored one: a staging twin never joins two items' quantities
        # (SEMANTIC-004)
        rows = [r for r in rows if _link_tier(r, ends) in _IDENTITY_TIERS
                and ctx.row_visible(r) and self._evidence_ok(ctx, r)]
        for row in rows:
            if row["from_object"] in ends and row["to_object"] in ends:
                parent[find(row["from_object"])] = find(row["to_object"])
        return {k: min(x for x in parent if find(x) == find(k)) for k in keys}

    def _compare(self, ctx: _Ctx, cmp: Mapping[str, Any], a: Mapping[str, Any],
                 b: Mapping[str, Any], group: str, group_type: str) -> dict[str, Any] | None:
        roles = list(cmp["roles"])
        measure = cmp["measure"]
        tolerance = Decimal(cmp["tolerance"])
        label = group or f"lines without one visible {group_type}"
        sides = []
        withheld: list[str] = []   # roles with a compared value this reader may not read
        for side, role in zip((a, b), roles):
            rids = side["groups"].get(group, []) + side["held"].get(group, [])
            spec = side["spec"]
            values = []
            for rid in rids:
                obj = ctx.obj(rid)
                names = [spec["quantity"]] if measure == "quantity" else list(spec.get("price", ()))
                rows = []
                for name in names:
                    if name in ctx.withheld(rid):
                        # never compared without it, nor on the next property in its place
                        rows = []
                        if role not in withheld:
                            withheld.append(role)
                        break
                    rows = ctx.values(rid).get(name, [])
                    if rows:
                        break
                values.append((rid, rows, obj["object_type"]))
            sides.append((side, rids, values))
        if not any(rids for _, rids, _ in sides):
            return None
        out_values = []
        for _, _, values in sides:
            for rid, rows, tid in values:
                for row in rows:
                    value, _ = _value_out(row, tid, 200)
                    if value is not None:
                        out_values.append(value)
        withheld_note = (f"some {' and '.join(withheld)} {measure} values are withheld from "
                         "this reader (a marking it does not hold), so this is not decided")
        if not out_values:
            if not withheld:
                return None  # nothing citable to show; the stage states carry the gap

        origin = "".join(f"{role} figures come from the twin system (its own system has "
                         "no record); " for side, role in zip((a, b), roles)
                         if side.get("origin") == "twin")
        origin += "".join(f"{len(side['in_progress'])} {role} record(s) are in progress "
                          f"(not yet {role}) and not counted (see gaps); "
                          for side, role in zip((a, b), roles) if side.get("in_progress"))

        def finish(agree: bool | None, note: str, gap: str | None = None,
                   reason: str | None = None) -> dict[str, Any]:
            """A check item; an undecided one names its code (``reason``, else ``gap``)."""
            item = {"roles": roles, "values": out_values[:50], "agree": agree,
                    "note": (f"{label}: " + origin + note)[:600]}
            if agree is None:
                item["undecided_reason"] = reason or gap
            if gap:  # internal: the caller moves it to the answer's gaps
                item["_gap"] = _gap("unknown", gap, f"{' vs '.join(roles)} {label}"[:700],
                                    note[:600])
            return item

        if not out_values:
            # Every compared value is withheld: the check is listed undecided with no
            # value to cite, and its gap says why; never dropped, so a reduction over
            # the listed checks never reads it as agreement.
            return finish(None, withheld_note, "marking_withheld")

        # a quantity side whose stage BC's own figures prove absent sums to zero
        # (``_absent_in_bc``); never a price, and never beside a line of its own
        zero = [measure == "quantity" and side["stage"] == NOT_REACHED
                and side.get("stage_reason") == ABSENT_IN_BC and not side["lines"]
                and not any(side[k] for k in ("held", "lineless", "lead_only"))
                for side in (a, b)]
        for (side, rids, _), role, absent in zip(sides, roles, zero):
            # A side is compared only once its stage is proven reached: a stage proven
            # not reached by status is a draft (no business commitment yet), and a
            # stage this reader cannot establish may be either for a full reader.
            if side["stage"] in (REACHED, OUT_OF_ORDER) or absent:
                continue
            if side["stage"] == NOT_REACHED and side.get("stage_reason") == "status_not_reached":
                return finish(None, f"the {role} side is a draft: stage "
                                    f"{side['spec']['stage']} is not reached by its status "
                                    "(not a business commitment)", reason="side_draft")
            if side["stage"] in (NOT_REACHED, SKIPPED):
                return finish(None, f"the {role} side has no evidence: stage "
                                    f"{side['spec']['stage']} is not reached",
                              reason=side["stage_reason"] or (
                                  "stage_not_applicable" if side["stage"] == SKIPPED
                                  else "stage_not_reached"))
            return finish(None, f"the {role} side is unknown: stage "
                                f"{side['spec']['stage']} could not be established",
                          reason=side["stage_reason"] or "stage_unknown")
        hidden = [(side, role) for side, role in zip((a, b), roles) if not side["proven"]]
        if hidden:
            return finish(None, f"the {' and '.join(role for _, role in hidden)} evidence is "
                                "not fully visible to this reader; not decided",
                          reason=_side_cause(hidden[0][0]))
        for side, role, absent in zip((a, b), roles, zero):
            if absent:
                continue  # proven to hold no line at all
            if side["lead_only"]:
                return finish(None, f"some {role} records are reached only by lead links; "
                                    "leads are never counted (see gaps), so this is not "
                                    "decided", reason="lead_evidence_not_counted")
            # an unattributed line of this item (or of no single item) could hold any
            # part of the root's quantity: nothing on this group is decided
            held = side["held"].get(group, []) + side["held"].get("", [])
            # the side gap of the worst-attributed held line's first state
            states = [_HELD_GAPS[s] for s in sorted(side["held_states"], key=_ATTRIBUTION.index)
                      if set(side["held_states"][s]) & set(held)]
            if held and set(held) <= {r for state in (_MISSING, _DELETED)
                                      for r in side["held_states"].get(state, ())}:
                return finish(None, f"some {role} lines are no longer returned by the source "
                                    "or are deleted in it (see gaps); their quantities are "
                                    "cited, not summed", reason=states[0])
            if held:
                return finish(None, f"some {role} lines are not attributable to the root "
                                    "(see gaps); their quantities are cited, not summed",
                              reason=states[0])
            if side["lineless"]:
                return finish(None, f"stage {side['spec']['stage']} is reached but no {role} "
                                    "line was found for some of its records (see gaps); "
                                    "not compared", reason="header_without_lines")
            if not side["lines"]:
                # the stage is reached, yet none of its lines was found: a header
                # without captured lines is not a zero quantity
                return finish(None, f"stage {side['spec']['stage']} is reached but no {role} "
                                    "line was found; not compared", "lines_not_found")
        if not group:
            return finish(None, f"some lines have no single visible {group_type}; "
                                "they are not grouped or compared", reason="group_not_single")
        for side, role in zip((a, b), roles):
            # a line without one visible item could belong here, for sums and prices
            if side["groups"].get(""):
                return finish(None, f"some {role} lines have no single visible {group_type}, "
                                    f"so per-item {measure} values are not compared",
                              reason="group_not_single")
        if withheld:
            return finish(None, withheld_note, "marking_withheld")
        parsed: list[list[tuple[Decimal, str | None, str | None]]] = []
        for (side, rids, values), role in zip(sides, roles):
            got = []
            for rid, rows, _ in values:
                texts = {(r["value"], unit_token(r["unit"]), r["currency"]) for r in rows}
                if len(texts) != 1:
                    return finish(None, f"a {role} line has no single readable {measure}",
                                  reason="value_not_single")
                value, unit, currency = rows[0]["value"], rows[0]["unit"], rows[0]["currency"]
                try:
                    got.append((Decimal(value), unit, currency))
                except (InvalidOperation, TypeError):
                    return finish(None, f"a {role} line's {measure} is not a decimal",
                                  reason="value_not_decimal")
            parsed.append(got)
        tol_text = "exact" if tolerance == 0 else f"tolerance {cmp['tolerance']}"
        if measure == "quantity":
            units = {u for side in parsed for _, u, _ in side}
            unit = _one_unit(units) if units else ""
            if unit is None:
                return finish(None, "units differ or are unknown "
                                    f"({units_text(units)}); "
                                    "no conversion is inferred", "unit_mismatch")
            sums = [sum((v for v, _, _ in side), Decimal(0)) for side in parsed]
            text = (f"{roles[0]} {_fmt(sums[0])} {unit} ({len(parsed[0])} lines) "
                    f"vs {roles[1]} {_fmt(sums[1])} {unit} ({len(parsed[1])} lines)")
            text += "".join(f"; stage {side['spec']['stage']} is proven absent in BC "
                            f"({ABSENT_IN_BC}): no {role} line, so {role} is 0"
                            for side, role, absent in zip((a, b), roles, zero) if absent)
            for side, role in zip((a, b), roles):
                if side.get("pending", {}).get(group):
                    text += (f"; {self._pending(ctx, side, group)} on {role} records in "
                             f"progress (not yet {role}): open work, not a shortage claim")
            agree, verdict = _quantity_verdict(cmp, roles, sums, unit)
            item = finish(agree, f"{text}; {verdict}")
            # The figures the note states, as fields: each role's sum and the first minus
            # the second (ordered vs received: the shortfall), so an answer states them.
            if len(sums) == 2:
                item.update(sums=[_fmt(total) for total in sums],
                            difference=_fmt(sums[0] - sums[1]))
                if unit_out(unit) is not None:   # only a unit token, never prose
                    item["unit"] = unit_out(unit)
            return item
        currencies = {c for side in parsed for _, _, c in side}
        if None in currencies or len(currencies) > 1:
            return finish(None, "currencies differ or are unknown "
                                f"({', '.join(sorted(str(c) for c in currencies))}); "
                                "no conversion is inferred", "currency_mismatch")
        per = self._denominators_mixed(sides)
        if per is not None:
            return finish(None, per, "price_denominator_mismatch")
        mixed = self._tax_bases_mixed(sides)
        if mixed is not None:
            return finish(None, mixed[1], mixed[0])
        base_prices = {v.normalize() for v, _, _ in parsed[0]}
        if not parsed[0] or not parsed[1]:
            return finish(None, f"a side has no {measure} for this group",
                          reason="value_missing")
        if len(base_prices) != 1:
            return finish(None, f"the {roles[0]} lines carry several prices "
                                f"({', '.join(_fmt(p) for p in sorted(base_prices))})",
                          reason="price_not_single")
        ref = base_prices.pop()
        off = [v for v, _, _ in parsed[1] if abs(v - ref) > tolerance]
        currency = currencies.pop()
        if off:
            return finish(False, f"{roles[0]} unit price {_fmt(ref)} {currency} vs {roles[1]} "
                                 f"{', '.join(_fmt(v) for v in off)} {currency}; {tol_text}")
        return finish(True, f"{roles[0]} and {roles[1]} unit price {_fmt(ref)} {currency}; "
                            f"{tol_text}")

    def _denominators_mixed(self, sides: Sequence[tuple]) -> str | None:
        """A note when the compared prices are not all unit prices per one known unit
        (the schema's ``money_measure: unit_price``; its denominator is the value's unit,
        folded by ``ontology/units.py``, never converted); None when they are, or when
        none is a unit price. As plx.compare's ``price_denominator_*``."""
        units, amounts = [], 0
        for _, _, values in sides:
            for _, rows, tid in values:
                props = {p["name"]: p for p in
                         self.base.schema.object_types.get(tid, {}).get("properties", ())}
                for row in rows:
                    if is_unit_price(props.get(row["property"])):
                        units.append(row["unit"])
                    else:
                        amounts += 1
        if not units:
            return None
        if amounts:
            return "a unit price and an amount are never compared"
        if same_denominator(units):
            return None
        return (f"the unit prices are per different or unknown units "
                f"({units_text({unit_token(u) for u in units})}); prices per two units "
                "are never compared and no conversion is inferred")

    def _tax_bases_mixed(self, sides: Sequence[tuple]) -> tuple[str, str] | None:
        """(gap reason, note) when the compared prices are on two tax bases, or an
        ``unknown`` basis meets a known one or another source column (the basis stored
        with each value: the schema's ``tax_basis``, or the record's own through
        ``tax_basis_from``, ``ontology/units.py``); None when one basis holds. As
        plx.compare: ``tax_basis_differs`` / ``tax_basis_unknown``."""
        bases: dict[str, set[str]] = {}
        for _, _, values in sides:
            for _, rows, tid in values:
                props = {p["name"]: p for p in
                         self.base.schema.object_types.get(tid, {}).get("properties", ())}
                for row in rows:
                    basis = value_basis(props.get(row["property"]), row)
                    bases.setdefault(basis, set()).add(
                        f"{tid}.{row['property']}@{row['binding_id']}")
        known = sorted(set(bases) - {"unknown"})
        unknown = sorted(bases.get("unknown", ()))
        if len(known) < 2 and not unknown or not known and len(unknown) == 1:
            return None
        named = ", ".join(known + (["unknown"] if unknown else []))
        detail = f" (unknown from {', '.join(unknown)})" if unknown else ""
        reason = "tax_basis_unknown" if unknown else "tax_basis_differs"
        return reason, (f"the prices are on tax bases {named}{detail}; prices with and "
                        "without tax are never compared")
