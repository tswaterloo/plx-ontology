"""Running ``ProcessTools`` over its roots: the per-root walk of a process graph.

Split from ``operating/ontology_tools_process.py`` (``_run`` stays there); moved
unchanged.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Sequence
from ontology.types import Actor
from operating.ontology_tools import _value_out
from operating.ontology_tools_identity import ToolError, _gap
from operating.ontology_tools_policy import NEVER_AUTHORITY, _markings
from operating.ontology_tools_traverse import WITHHELD
from .ontology_tools_process_graphs import (
    DEFAULT_TIER_FLOOR, MAX_DUE_BY, NOT_REACHED, OUT_OF_ORDER, REACHED, SKIPPED,
    TIER_ORDER, UNKNOWN, _LINEAGE_PLAIN, _path_types,
)
from .ontology_tools_process_answer import (
    Dated, UNCHECKED, _Ctx, _HIDDEN, _LEAD, _Outcome, _PathResult, _business_date,
    _date, stale_sources,
)
from .ontology_tools_process_helpers import ABSENT_IN_BC, _cite, _norm


class _RunMixin:
    """Methods of ``ProcessTools`` (``ontology_tools_process_run``); mixed in there, never used alone."""

    def _roots(self, root: str, actor: Actor) -> tuple[list[str], bool, bool]:
        """Root ids, whether they came from a set, and whether that set is marked
        withheld (made from an answer this reader's grants trimmed)."""
        if root.startswith("set:"):
            ids = self.base.resolve_set(root, actor)
            if ids is None:
                raise ToolError("handle_expired", "the set handle is unknown or expired",
                                "re-run the call that produced it")
            return list(ids), True, self.base.sets.is_withheld(root, actor)
        return [root], False, False

    def _set_scope(self, root: str, actor: Actor) -> tuple[bool, int | None, bool]:
        """(complete, how many matched when known, lead) of a set root, as the set
        registry recorded them (SEMANTIC-003: a subset is never the population, a lead
        cohort never a confirmed one); a single record is (True, None, False)."""
        if not root.startswith("set:"):
            return True, None, False
        sets = self.base.sets
        whole, matched = sets.completeness(root, actor) \
            if getattr(sets, "completeness", None) else (False, None)
        lead = bool(getattr(sets, "is_lead", None) and sets.is_lead(root, actor))
        return bool(whole), matched, lead

    def _revoked(self, ctx: "_Ctx", unseen: Sequence[str]) -> bool:
        """Whether set members this reader cannot see at T are hidden by its grants
        (the set's creator saw them), not merely first captured after T or absent from
        the projection store."""
        held = list(self.base.store.objects(unseen))
        if not held:
            return False
        if ctx.now_answer:
            return True
        now = self.base.clock().astimezone(dt.timezone.utc)
        readable = self.base._visible_versions(self.base._scope(ctx.actor, now), held)
        return len(readable) < len(held)

    def _graph(self, process: str | None, root_types: set[str], ctx: _Ctx | None = None,
               roots: Sequence[str] = ()) -> Mapping[str, Any]:
        """The named graph; unnamed, the one graph whose ``open_when`` every root of its
        type meets (``process_lateness.openness``: an open PO gets po_delivery_window),
        else the one implicit graph of the roots' types. A root whose openness this reader
        cannot decide (withheld or unproven) fails closed: name the process, never a
        graph a reader with more access would not get."""
        if process is not None:
            graph = self.graphs.get(process)
            if graph is None:
                raise ToolError("unknown_process", f"no process {process} is declared",
                                "processes: " + ", ".join(sorted(self.graphs))[:500])
            return graph
        fits = sorted(g["id"] for g in self.graphs.values() if g["root_type"] in root_types)
        if ctx is not None:
            from operating.process_lateness import openness
            chosen = []
            for gid in fits:
                graph = self.graphs[gid]
                mine = [r for r in roots if ctx.obj(r)["object_type"] == graph["root_type"]]
                if not (graph.get("open_when") and mine):
                    continue
                seen = [openness(self, ctx, graph, r) for r in mine]
                if None in seen:
                    raise ToolError("ambiguous_process",
                                    "name the process: whether the record is open is not "
                                    "decided with what this reader may read",
                                    ("processes: " + ", ".join(fits))[:500])
                if all(seen):
                    chosen.append(graph)
            if len(chosen) == 1:
                return chosen[0]
        implicit = [g for g in fits if self.graphs[g].get("implicit", True)]
        if len(implicit) != 1:
            raise ToolError("ambiguous_process" if fits else "no_process",
                            "name the process" if fits else
                            "no process is declared for this object type",
                            ("processes: " + ", ".join(fits or sorted(self.graphs)))[:500])
        return self.graphs[implicit[0]]

    # ------------------------------------------------------------------ one root

    def _evaluate_root(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str) -> dict[str, Any]:
        floor = graph.get("tier_floor", DEFAULT_TIER_FLOOR)
        variant, variant_gap = self._variant(ctx, graph, oid)
        gaps: list[dict[str, Any]] = [variant_gap] if variant_gap else []
        outcomes = {stage["id"]: self._read_stage(ctx, graph, oid, stage, variant, floor)
                    for stage in graph["stages"]}
        self._recorded_absence(ctx, graph, oid, outcomes, floor)  # before the requirements
        conditions: dict[str, _Outcome] = {}

        unreported: dict[str, _Outcome] = {}

        def condition(cid: str, report: bool = True) -> _Outcome:
            if cid not in conditions and cid not in unreported:
                spec = next(c for c in graph.get("conditions", ()) if c["id"] == cid)
                unreported[cid] = self._evidence(ctx, graph, oid, spec["evidence"], floor)
            if report and cid in unreported:
                conditions[cid] = unreported.pop(cid)
            return conditions.get(cid) or unreported[cid]

        def requirements(stage: Mapping[str, Any]) -> tuple[list[str], list[str]]:
            """(requirements that apply, requirements that may apply). With the variant
            undecided, every variant's own requirements may apply: they can only make
            the stage undecided, never out_of_order (the unknown variant may not
            require them), and their conditions are not reported."""
            requires = stage.get("requires", {})
            sure = list(requires.get("*", ()))
            maybe: list[str] = []
            if variant is not None:
                sure += requires.get(variant, ())
            else:
                for key, refs in sorted(requires.items()):
                    if key != "*":
                        maybe += [r for r in refs if r not in sure and r not in maybe]
            return sure, maybe

        def state_of(ref: str, report: bool) -> str:
            kind, name = ref.split(":", 1)
            return outcomes[name].state if kind == "stage" else condition(name, report).state

        for stage in graph["stages"]:  # gates are reported even when their stage is not
            if outcomes[stage["id"]].state != SKIPPED:
                for ref in requirements(stage)[0]:
                    if ref.startswith("condition:"):
                        condition(ref.split(":", 1)[1])
        for stage in graph["stages"]:
            outcome = outcomes[stage["id"]]
            if outcome.state != REACHED:
                continue
            sure, maybe = requirements(stage)
            unmet = [r for r in sure if state_of(r, True) == NOT_REACHED]
            undecided = [r for r in sure if state_of(r, True) not in
                         (REACHED, OUT_OF_ORDER, NOT_REACHED)]
            undecided += [r for r in maybe if state_of(r, False) not in (REACHED, OUT_OF_ORDER)]
            if undecided and not unmet:
                # a full-grant reader might find the requirement unmet (out_of_order)
                outcome.state, outcome.gap_state = UNKNOWN, "unknown"
                outcome.reason = "requirement_unproven"
                outcome.detail = ("reached, but whether " + ", ".join(undecided)
                                  + " holds is not established")[:600]
                continue
            if unmet:
                outcome.state = OUT_OF_ORDER
                outcome.detail = ("reached although " + ", ".join(unmet) + " is not met")[:600]

        checks: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        item_gaps: list[dict[str, Any]] = []
        for check in graph.get("quantity_checks", ()):
            found, crossed, side_gaps, per_item = self._quantity_check(
                ctx, graph, oid, check, outcomes, floor)
            checks += found
            conflicts += crossed
            gaps += side_gaps
            item_gaps += per_item
        item_gaps = [c["gap"] for c in conflicts] + item_gaps
        lineage = ctx.visible([oid])[oid]["lineage"]
        if lineage in NEVER_AUTHORITY:
            # R1: a staging row is never authority, so nothing about it is proven; the
            # binding's own lineage rules say what an authority row of it carries.
            basis = self._staging_reason(oid, lineage)
            plain = _LINEAGE_PLAIN.get(lineage, lineage.replace("_", " "))
            gap = _gap("unknown", "root_lineage_staging", oid[:700],
                       f"This record comes from {plain} data, so every stage answer from it "
                       "is a lead to confirm, not an authority.")
            gap["basis"] = basis
            gaps.append(gap)
            for outcome in list(outcomes.values()) + list(conditions.values()):
                if outcome.state in (REACHED, NOT_REACHED, OUT_OF_ORDER, SKIPPED):
                    outcome.detail = (f"observed {outcome.state} from a record of {plain} "
                                      "data: a lead to confirm, never proven")
                    outcome.basis = basis
                    outcome.state, outcome.gap_state = UNKNOWN, "unknown"
                    outcome.reason = "staging_root"
            for check in checks:
                item = check["item"]
                if item["agree"] is not None:
                    item["note"] = (f"lead only ({lineage} root, observed agree="
                                    f"{str(item['agree']).lower()}): " + item["note"])[:600]
                    item["agree"] = None
                    item["undecided_reason"] = "root_lineage_staging"
                check["finding"] = False
        for stage in graph["stages"]:
            outcome = outcomes[stage["id"]]
            if outcome.unchecked and outcome.state == NOT_REACHED:
                gaps.append(_gap("not_ingested", UNCHECKED, stage["id"], (
                    f"stage {stage['id']} is proven absent from BC's records alone "
                    f"({ABSENT_IN_BC}): the twin system (Portal) was not captured, so its "
                    "twin records were not read and the cross-system check did not run")[:600]))

        current, certain = None, True
        for stage in graph["stages"]:
            state = outcomes[stage["id"]].state
            if state == SKIPPED or stage.get("optional"):
                continue  # an optional stage never positions the order
            if state not in (REACHED, OUT_OF_ORDER):
                current = stage["id"]
                certain = certain and state == NOT_REACHED
                break
            if state == OUT_OF_ORDER:
                certain = False
        # the step expected once current_stage is done: decided only with current_stage
        # proven and a later stage whose applicability does not hang on an unknown variant
        following, following_certain = None, current is None or certain
        if current is not None and following_certain:
            order = [s["id"] for s in graph["stages"]]
            for stage in graph["stages"][order.index(current) + 1:]:
                outcome = outcomes[stage["id"]]
                if stage.get("optional") or outcome.state == SKIPPED:
                    continue
                following_certain = outcome.reason != "variant_unknown"
                following = stage["id"]
                break
        stages_out = [{"stage": s["id"], "state": outcomes[s["id"]].state,
                       "records": outcomes[s["id"]].records,
                       "events": outcomes[s["id"]].events,
                       "reason": outcomes[s["id"]].reason,
                       "detail": outcomes[s["id"]].detail,
                       "citations": outcomes[s["id"]].citations,
                       "_outcome": outcomes[s["id"]]} for s in graph["stages"]]
        return {"root": oid, "variant": variant, "current_stage": current,
                "current_certain": bool(current is None or certain),
                "next_stage": following, "next_certain": following_certain,
                "stages": stages_out,
                "conditions": {cid: {"state": o.state, "records": o.records,
                                     "reason": o.reason, "_outcome": o}
                               for cid, o in conditions.items()},
                "checks": checks, "conflicts": conflicts, "gaps": gaps,
                "item_gaps": item_gaps}

    def _due(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, stage: Mapping[str, Any],
             outcome: _Outcome, conflicts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Whether ``stage`` is overdue at the answer's as_known_at, from its declared due
        date (``_due_date``). Reached is never overdue. Proven not reached is overdue only
        when ``_due_date`` proves one date and that date has ended (``_business_date``)."""
        dated = self._due_date(ctx, graph, oid, stage, conflicts)
        entry: dict[str, Any] = {"stage": stage["id"], "overdue": None}
        if dated.values:
            entry["due_by"] = dated.values[:MAX_DUE_BY]
        if outcome.state in (REACHED, OUT_OF_ORDER):
            entry["overdue"] = False  # whatever its due date says
            return entry
        if dated.gap is not None:
            entry["gap"] = dated.gap
        elif outcome.state != NOT_REACHED:
            entry["gap"] = _gap("unknown", "stage_undecided", f"{stage['id']} due",
                                "whether the stage is reached is not established, so neither "
                                "is whether it is overdue")
        elif dated.unattested is not None:
            entry["gap"] = dated.unattested
        else:
            today = _business_date(ctx.at)
            entry["overdue"] = today > dated.date
            if today > dated.date:
                entry["days_overdue"] = (today - dated.date).days
        return entry

    def _due_date(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                  stage: Mapping[str, Any], conflicts: Sequence[Mapping[str, Any]]) -> Dated:
        """``stage``'s due date for root ``oid`` from its declared due dates only (``due``:
        planned events or a date property of the records its paths reach): one date only
        when every due date is visible and proven, they are one date, no captured document
        this reader sees contradicts it (``conflicts``: a date under a document-vs-record
        conflict is an evidence conflict, not a fact) and every document that could is
        proven visible (else ``unattested``). Never a computed or default date."""
        spec = stage["due"]
        tid = spec["object_type"]
        tspec = self.base.schema.object_types.get(tid, {})
        floor = graph.get("tier_floor", DEFAULT_TIER_FLOOR)
        reach = self._paths(ctx, graph["root_type"], oid, spec["paths"], floor,
                            spec_marks=tspec.get("markings", ()),
                            final_bindings={b["id"] for b in tspec.get("bindings", ())})
        records = sorted(r for r in reach.records if ctx.obj(r)["object_type"] == tid)
        values: list[dict[str, Any]] = []
        parts, uncited = _PathResult(), False   # what replaced_by reads (its parts)
        if "event_types" in spec:
            marks = {m for e in spec["event_types"]
                     for m in self.base.schema.event_types[e].get("markings", ())}
            hidden = not ctx.scope.holds(marks)
            replaced = spec.get("replaced_by")
            keeps = set(records)   # records whose own replaced_by event date still counts
            if replaced:
                part = replaced["object_type"]
                pspec = self.base.schema.object_types.get(part, {})
                for rid in records:
                    sub = self._paths(ctx, tid, rid, replaced["paths"], floor,
                                      spec_marks=pspec.get("markings", ()), final_bindings={
                                          b["id"] for b in pspec.get("bindings", ())})
                    parts.proven = parts.proven and sub.proven
                    parts.truncated = parts.truncated or sub.truncated
                    parts.reasons += sub.reasons
                    dated = 0
                    found = sorted(r for r in sub.records if ctx.obj(r)["object_type"] == part)
                    for pid in found:
                        hidden = hidden or replaced["property"] in ctx.withheld(pid)
                        read = [_value_out(row, part, 200)[0]
                                for row in ctx.values(pid).get(replaced["property"], ())
                                if row["value"] is not None]
                        uncited = uncited or None in read   # a date it may not stand in for
                        values += [v for v in read if v is not None]
                        dated += bool(read)
                    if found and dated == len(found):
                        keeps.discard(rid)
            for _, subject, row in self._visible_events(ctx, records, spec["event_types"]):
                if replaced and row["event_type"] == replaced["event_type"] \
                        and subject not in keeps:
                    continue
                cite = _cite(row)
                if cite and row["time"]:
                    values.append({"property": row["event_type"].split(".", 1)[1],
                                   "value_type": "date", "value": str(row["time"])[:10],
                                   "citation": cite, "markings": list(_markings(row["markings"]))})
            columns = {c for e in spec["event_types"] for c in self._columns(tid, e)}
        else:
            hidden = any(spec["property"] in ctx.withheld(r) for r in records)
            for rid in records:
                for row in ctx.values(rid).get(spec["property"], ()):
                    value = _value_out(row, tid, 200)[0] if row["value"] is not None else None
                    if value is not None:
                        values.append(value)
            columns = self._columns(tid, spec["property"])
        dates = {_date(v["value"]) for v in values}
        subject = f"{stage['id']} due"
        attesting, attested_proven = self._attesting(ctx, graph, oid, floor, tid, columns)
        contradicted = [c["subject"] for c in conflicts
                        if c["subject"].rsplit(" vs ", 1)[-1] in attesting]
        gap, lead_only, stale = None, False, ()
        if contradicted:
            gap = _gap("unknown", "due_date_conflict", contradicted[0], (
                "a captured document contradicts this due date (" + "; ".join(contradicted)
                + ", see conflicts): lateness is an evidence conflict, not a fact"))
        elif hidden or (values and _HIDDEN in reach.causes | parts.causes):
            gap = _gap("inaccessible", WITHHELD, subject,
                       "a due date of this stage is withheld from this reader")
        elif _HIDDEN in reach.causes | parts.causes:
            # No date on any record this reader reads, and a source its grant does not
            # cover might hold one. Said from the policy alone, whatever those records
            # hold: the reader learns nothing of a record it may not read.
            gap = _gap("unknown", "no_visible_due_date", subject,
                       "no due date is recorded on the records this reader can read; "
                       "records outside this reader's grant may hold one")
        elif not (reach.proven and parts.proven) or reach.truncated or parts.truncated \
                or uncited or None in dates:
            gap = _gap("unknown", "due_date_unproven", subject, (
                "the due dates are not proven complete or readable: "
                + "; ".join(dict.fromkeys(w.text for w in reach.reasons + parts.reasons))))
            why = reach.reasons + parts.reasons
            exact = not (reach.truncated or parts.truncated or uncited or None in dates)
            lead_only = bool(why) and all(w.tier == _LEAD for w in why) and exact
            stale = (stale_sources(why) or ()) if exact and not lead_only else ()
        elif not dates:
            gap = _gap("absent_in_source", "no_due_date", subject,
                       "no declared due date is recorded for this stage")
        elif len(dates) > 1:
            gap = _gap("unknown", "due_dates_disagree", subject,
                       "the records state different due dates ("
                       + ", ".join(sorted(map(str, dates))) + "); none is chosen")
        unattested = None if attested_proven else _gap(
            "unknown", "due_date_attestation_unproven", subject, (
                "a captured document (" + ", ".join(attesting) + ") may state this due date "
                "differently, and no such document of this record is proven visible to this "
                "reader: lateness is not stated"))
        if gap is not None:   # unattested still read: lateness ranks disagreeing dates
            return Dated(values, None, gap, unattested, lead_only, tuple(stale))
        return Dated(values, next(iter(dates)), None, unattested)

    def _columns(self, tid: str, name: str) -> set[tuple[str, str]]:
        """The (binding, column) pairs an event id or a property ``name`` of ``tid`` is
        read from (empty when neither is declared)."""
        event = self.base.schema.event_types.get(name)
        if event is not None:
            time = event.get("time") or {}
            return {(time.get("binding"), time.get("path"))}
        prop = self._prop(tid, name) or {}
        return {(src.get("binding"), src.get("path")) for src in prop.get("sources", ())}

    def _attesting(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, floor: str,
                   tid: str, columns: set[tuple[str, str]]) -> tuple[list[str], bool]:
        """The declared document comparisons (``ontology.document_fields.comparisons``)
        that compare a ``tid`` date read from ``columns`` and apply to this root, as the
        document side of their conflict subjects (``operating/attested_conflicts.py``),
        and whether every such document of this root is proven visible to this reader.
        A document link no counted binding carries (a lead) never proves that."""
        from ontology.document_fields import comparisons
        from operating.attested_conflicts import _property_of

        links = self.base.schema.link_types
        root_type, binding = graph["root_type"], ctx.obj(oid)["binding_id"]
        subjects: list[str] = []
        proven = True
        for rule in comparisons():
            target = (links.get(rule["link_type"]) or {}).get("to")
            doc_type = rule["document_type"]
            doc_prop = _property_of(self.base, doc_type, rule["field"], rule["keyed"])
            path = [{"link": h["link_type"], "direction": h["direction"]} for h in rule["path"]]
            record = (_path_types(path, target, links) or [target])[-1]
            compared = self._columns(record, rule.get("event") or rule.get("property", ""))
            if not (record == tid and compared & columns or doc_prop and doc_type == tid
                    and self._columns(doc_type, doc_prop) & columns):
                continue
            if root_type == doc_type:
                walk = [{"link": rule["link_type"], "direction": "forward"}] + path
                final = {b["id"] for b in self.base.schema.object_types[record]["bindings"]}
            elif root_type == target and self._countable(path, binding, TIER_ORDER[-1], None):
                walk = [{"link": rule["link_type"], "direction": "reverse"}]
                final = {b["id"] for b in self.base.schema.object_types[doc_type]["bindings"]}
            else:
                continue
            subjects.append(f"{doc_type}.{doc_prop}")
            found = self._paths(ctx, root_type, oid, [walk], floor, final_bindings=final)
            proven = proven and found.applicable and found.proven and not found.truncated
        return subjects, proven

    def _variant(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str
                 ) -> tuple[str | None, dict[str, Any] | None]:
        by = graph.get("variant_by")
        if not by:
            return None, None
        obj = ctx.obj(oid)
        prop = self._prop(obj["object_type"], by["property"])
        subject = f"{obj['object_type']}.{by['property']}"
        if prop is None or obj["binding_id"] not in {s["binding"] for s in prop["sources"]}:
            return None, _gap("unknown", "variant_not_carried", subject,
                              "this record's source does not carry the property that selects "
                              "the process variant")
        rows = ctx.values(oid).get(by["property"], [])
        texts = {r["value"] for r in rows}
        if not rows or len(texts) != 1:
            return None, _gap("unknown", "variant_unreadable", subject,
                              "the variant property is not readable for this reader")
        text = texts.pop()
        if text is None:
            return by["default"], None
        key = _norm(text, by.get("compare", "exact"))
        for value, variant in by["values"].items():
            if _norm(value, by.get("compare", "exact")) == key:
                return variant, None
        return None, _gap("unknown", "variant_value_undeclared", subject,
                          "the variant property holds a value the process does not declare")
