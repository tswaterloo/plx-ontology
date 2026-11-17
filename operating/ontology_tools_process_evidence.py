"""The evidence of ``ProcessTools``: reading the records a stage cites and the gaps,
conflicts and prerequisites they show.

Split from ``operating/ontology_tools_process.py`` (``_blockers`` stays there); moved
unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence
from operating.ontology_tools_identity import identity_link_type
from operating.ontology_tools_policy import _IDENTITY_TIERS, NEVER_AUTHORITY
from operating import po_status
from operating.ontology_tools_traverse import WITHHELD
from .ontology_tools_process_graphs import (
    AMBIGUOUS, DEFAULT_TIER_FLOOR, NOT_APPLICABLE, NOT_REACHED, REACHED, SKIPPED,
    UNKNOWN, _link_tier,
)
from .ontology_tools_process_answer import (
    _Ctx, _HIDDEN, _LEAD, _Outcome, _PathResult, _Reach, _SCHEMA_LEADS, _UNCAPTURED,
    _twin_decides,
)
from .ontology_tools_process_helpers import (
    _HELD_BACK, _NO_RECORD, _REMOVED, _UNCARRIED, _UNSEEN, _cite, _norm, _open_detail,
    _reach_of, _unproven_reach,
)


class _EvidenceMixin:
    """Methods of ``ProcessTools`` (``ontology_tools_process_evidence``); mixed in there, never used alone."""

    # ------------------------------------------------------------------ evidence

    def _evidence(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                  specs: Sequence[Mapping[str, Any]], floor: str) -> _Outcome:
        """A stage's (or condition's) outcome over its evidence specs' own ``paths``;
        their ``fallback_paths`` (the twin system) only when ``_twin_decides``, the
        rule quantity sides use: the own paths are proven, lead rows aside, to reach
        no live record and the twin's reach one. An open, unreadable or hidden own
        record keeps the own answer."""
        found = [self._spec(ctx, graph, oid, spec, floor) for spec in specs]
        own = self._combine(found)
        twins = [dict(spec, paths=spec["fallback_paths"]) for spec in specs
                 if spec.get("fallback_paths")]
        if not twins:
            return own
        read: list[_Outcome] = []

        def twin_reach() -> _Reach:
            read.extend(self._spec(ctx, graph, oid, spec, floor) for spec in twins)
            return _reach_of(read)

        if _twin_decides(_reach_of(found), twin_reach):
            twin = self._combine(read)
            twin.detail = ((twin.detail + "; " if twin.detail else "")
                           + "read from the twin system's records (the root's own system "
                           "has no live record)")[:600]
            twin.system = "twin"
            return twin
        own.system = "own"
        if not read:
            return own
        twin = self._combine(read)
        twin.system = "own"
        if own.reason == "not_applicable_to_source":
            # the root's own source carries no path to this evidence (e.g. a Portal PO
            # for BC invoices): its twin path is the only one, so its answer stands
            if twin.state == NOT_REACHED and twin.gap_state == "absent_in_source":
                twin.detail = ((twin.detail + "; " if twin.detail else "")
                               + "the root's own source carries no such record and no "
                               "twin of it holds one")[:600]
                return twin
            if twin.reason == "not_applicable_to_source" or twin.lead_by_schema:
                own.detail = ("neither the root's own source nor its twin path carries "
                              "counted evidence of this kind")
                return own
            return twin  # undecided for this reader: a full-grant reader may decide
        # The twin holds no record this reader can see; when that is not proven, a
        # full-grant reader could find one there, so the own absence stays unknown.
        if own.state == UNKNOWN or twin.state != UNKNOWN \
                or twin.reason == "not_applicable_to_source":
            return own
        # unknown only because the twin's system was never captured: a figure the own
        # system records may still prove the absence (``_absent_in_bc``)
        twin.past_uncaptured = (
            own.state == NOT_REACHED and own.reason in _NO_RECORD and own.reach.complete
            and not own.reach.found and all(
                o.state == NOT_APPLICABLE or (o.state == NOT_REACHED and o.reason in _NO_RECORD)
                or (o.state == UNKNOWN and o.uncaptured) for o in read))
        return twin

    def _evidence_of(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                     ref: Mapping[str, Any], floor: str) -> _Outcome:
        """A stage read as another graph's stage (``evidence_of``) of each record its
        ``paths`` reach: that stage's evidence from each, exactly as that graph reads it
        (``_stage_evidence``: own paths, then its twin, and that stage's
        ``absence_unproven``). Reached when any is. An absence holds only when the
        reached records are proven complete and each proves one; with no record reached
        the stage is unknown, never missing."""
        other = self.graphs.get(ref["process"]) or {}
        stage = next((s for s in other.get("stages", ())
                      if s["id"] == ref["stage"] and "evidence" in s), None)
        if stage is None:
            return _Outcome(UNKNOWN, gap_state="unknown", reason="evidence_process_missing",
                            detail=f"process {ref['process']} with stage {ref['stage']} is "
                                   "not loaded, so this stage is not read")
        tid = ref["object_type"]
        reach, roots = self._reached(ctx, graph, oid, ref, floor)
        if not reach.applicable:
            return _Outcome(UNKNOWN, gap_state="unknown", reason="not_applicable_to_source",
                            detail=f"no declared path reaches a {tid} from this record's source")
        other_floor = other.get("tier_floor", DEFAULT_TIER_FLOOR)
        found = self._combine([self._stage_evidence(ctx, other, r, stage, other_floor)
                               for r in roots]) if roots else None
        if found is not None and found.state == REACHED:
            return found
        if not reach.proven or reach.truncated:
            return _unproven_reach(reach, f"the {tid} records this stage is read from")
        if found is None:
            return _Outcome(UNKNOWN, gap_state="unknown", reason="no_record_reached",
                            detail=f"no {tid} is reached, so none of its {ref['stage']} "
                                   "records is: unknown, never missing")
        return found

    def _reached(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, ref: Mapping[str, Any],
                 floor: str) -> tuple[_PathResult, list[str]]:
        """The records of another graph's root type an ``evidence_of`` or prerequisite
        reference's ``paths`` reach from ``oid``, with the reach and its proof."""
        tid = ref["object_type"]
        tspec = self.base.schema.object_types.get(tid, {})
        reach = self._paths(ctx, graph["root_type"], oid, ref["paths"], floor,
                            spec_marks=tspec.get("markings", ()),
                            final_bindings={b["id"] for b in tspec.get("bindings", ())})
        return reach, sorted(r for r in reach.records if ctx.obj(r)["object_type"] == tid)

    def _read_stage(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                    stage: Mapping[str, Any], variant: str | None, floor: str) -> _Outcome:
        """One stage of ``oid`` from its evidence, skipped when its variant skips it
        (unknown when that hangs on an unknown variant). No requirements: a reached
        stage is reached whatever its gates say."""
        sid = stage["id"]
        skips = {v["id"]: set(v["skips"]) for v in graph.get("variants", ())}
        if variant is not None and sid in skips.get(variant, set()):
            return _Outcome(SKIPPED, reason="skipped_by_variant",
                            detail=f"variant {variant} skips this stage")
        if variant is None and any(sid in s for s in skips.values()):
            if all(sid in s for s in skips.values()):
                return _Outcome(SKIPPED, reason="skipped_by_variant")
            return _Outcome(UNKNOWN, gap_state="unknown", reason="variant_unknown",
                            detail="whether this stage applies depends on the variant, which "
                                   "is unknown")
        if "evidence_of" in stage:
            return self._evidence_of(ctx, graph, oid, stage["evidence_of"], floor)
        return self._stage_evidence(ctx, graph, oid, stage, floor)

    def _stage_of(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, sid: str) -> _Outcome:
        """Stage ``sid`` of a prerequisite record as its own graph reads it for this
        reader: its variant, its evidence, rule C (``_recorded_absence``) and R1 (a
        staging record proves nothing). Cached per call."""
        key = (graph["id"], sid, oid)
        if key not in ctx.stage_reads:
            floor = graph.get("tier_floor", DEFAULT_TIER_FLOOR)
            stage = next(s for s in graph["stages"] if s["id"] == sid)
            outcomes = {sid: self._read_stage(ctx, graph, oid, stage,
                                              self._variant(ctx, graph, oid)[0], floor)}
            self._recorded_absence(ctx, graph, oid, outcomes, floor)
            outcome = outcomes[sid]
            lineage = ctx.visible([oid])[oid]["lineage"]
            if lineage in NEVER_AUTHORITY and outcome.state != UNKNOWN:
                outcome = _Outcome(UNKNOWN, gap_state="unknown", reason="staging_root",
                                   detail=f"observed {outcome.state} on a {lineage} record: a "
                                          "lead, never proven")
            ctx.stage_reads[key] = outcome
        return ctx.stage_reads[key]

    def _twins(self, ctx: _Ctx, records: Sequence[str]) -> list[list[str]]:
        """``records`` as business records: those a visible identity link joins (a link
        type between two records of one type that declares a ``same_record`` binding, at
        an identity tier, no staging end) are one group. Groups and their handles sorted."""
        root = {rid: rid for rid in records}

        def find(rid: str) -> str:
            while root[rid] != rid:
                rid = root[rid]
            return rid

        types = {ctx.obj(rid)["object_type"] for rid in records}
        identity = sorted(lid for lid, link in self.base.schema.link_types.items()
                          if link["from"] in types and identity_link_type(link))
        if identity and len(records) > 1:
            held = json.dumps(sorted(records))
            for row in self.base.store.conn.execute(
                    "SELECT * FROM links WHERE link_type IN (SELECT value FROM json_each(?)) "
                    "AND from_object IN (SELECT value FROM json_each(?)) AND to_object IN "
                    "(SELECT value FROM json_each(?)) ORDER BY link_id",
                    (json.dumps(identity), held, held)):
                ends = ctx.visible([row["from_object"], row["to_object"]])
                if _link_tier(row, ends) not in _IDENTITY_TIERS or not ctx.row_visible(row) \
                        or not self._evidence_ok(ctx, row) or len(ends) < 2:
                    continue
                a, b = sorted((find(row["from_object"]), find(row["to_object"])))
                root[b] = a
        groups: dict[str, list[str]] = {}
        for rid in sorted(records):
            groups.setdefault(find(rid), []).append(rid)
        return sorted(groups.values())

    def _stage_evidence(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                        stage: Mapping[str, Any], floor: str) -> _Outcome:
        """A stage's outcome over its ``evidence`` (``_evidence``), with its
        ``absence_unproven`` rule: a root of a declared binding or lineage never proves
        the stage absent (its source does not link every such record to it), so a
        not-reached or not-carried answer there is unknown, never missing."""
        out = self._evidence(ctx, graph, oid, stage["evidence"], floor)
        rule = stage.get("absence_unproven")
        if rule and (out.state == NOT_REACHED or out.reason == "not_applicable_to_source") \
                and (ctx.obj(oid)["binding_id"] in rule.get("bindings", ())
                     or ctx.visible([oid])[oid]["lineage"] in rule.get("lineages", ())):
            out.state, out.gap_state, out.reason = UNKNOWN, "unknown", "absence_unproven"
            out.detail = f"{rule['note']} ({oid}; {rule['evidence']})"[:600]
            out.past_uncaptured = False  # the root's source never proves it absent
        return out

    def _combine(self, outcomes: Sequence[_Outcome]) -> _Outcome:
        reached = [o for o in outcomes if o.state == REACHED]
        if reached:
            out = _Outcome(REACHED)
            for o in reached:
                out.records += [r for r in o.records if r not in out.records]
                out.events += [e for e in o.events if e not in out.events]
                out.citations += o.citations
            return out
        for state in (UNKNOWN, NOT_REACHED):
            # a reader-dependent unknown outranks not_served (the same for every reader)
            hits = sorted((o for o in outcomes if o.state == state),
                          key=lambda o: o.reason == "not_served")
            if hits:
                out = hits[0]
                for o in hits[1:]:
                    out.records += [r for r in o.records if r not in out.records]
                    out.citations += o.citations
                return out
        return _Outcome(UNKNOWN, gap_state="unknown", reason="not_applicable_to_source",
                        detail="no declared path carries this evidence from this record's source")

    def _spec(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, spec: Mapping[str, Any],
              floor: str) -> _Outcome:
        """One evidence spec's outcome over its ``paths``, with its ``reach``."""
        tid = spec["object_type"]
        tspec = self.base.schema.object_types.get(tid, {})
        availability = tspec.get("availability", {})
        if availability.get("state") == "not_served":
            return _Outcome(UNKNOWN, gap_state="not_ingested", reason="not_served",
                            detail=(f"{tid} is not served: " + availability.get("reason", ""))[:600])
        wanted = set(spec.get("bindings", ()))
        if wanted and spec["paths"] == [[]] and ctx.obj(oid)["binding_id"] not in wanted:
            # the root's own source has another vocabulary
            return _Outcome(NOT_APPLICABLE, reach=_Reach(found=False, complete=True))
        result = self._paths(ctx, graph["root_type"], oid, spec["paths"], floor,
                             spec_marks=tspec.get("markings", ()),
                             final_bindings=wanted or {b["id"] for b in tspec.get("bindings", ())})
        if not result.applicable:
            return _Outcome(NOT_APPLICABLE, reach=_Reach(found=False, complete=True))
        records = {r: v for r, v in result.records.items()
                   if ctx.obj(r)["object_type"] == tid
                   and (not wanted or ctx.obj(r)["binding_id"] in wanted)}
        for version in records.values():
            ctx.note_source(version)
        proven = result.proven and not result.truncated
        why = "; ".join(dict.fromkeys(w.text for w in result.reasons))[:600] or None
        # unproven only by lead rows (never by hidden evidence): a twin may still decide
        leads_only = not proven and result.complete_but_leads
        outcome, live = self._decide(ctx, spec, records, proven, why, leads_only)
        if outcome.state == UNKNOWN and outcome.reason in _UNSEEN and _HIDDEN in result.causes:
            # this reader's grant or markings keep evidence out of view: withheld, which a
            # fuller grant would decide, never a missing step
            outcome.gap_state, outcome.reason = "inaccessible", WITHHELD
        elif outcome.state == UNKNOWN and outcome.reason == "absence_unproven":
            ambiguous = [w for w in result.reasons if w.code == AMBIGUOUS]
            if ambiguous:  # the number names several orders: typed, never a pick
                outcome.reason, outcome.detail = AMBIGUOUS, ambiguous[0].text
        outcome.reach = _Reach(found=live, complete=result.complete_but_leads)
        outcome.lead_by_schema = (not result.truncated and bool(result.reasons) and all(
            w.code in _SCHEMA_LEADS for w in result.reasons))
        outcome.uncaptured = (not live and not result.truncated and bool(result.reasons)
                              and all(w.cause == _UNCAPTURED if w.tier != _LEAD
                                      else w.code in _SCHEMA_LEADS for w in result.reasons))
        return outcome

    def _decide(self, ctx: _Ctx, spec: Mapping[str, Any], records: Mapping[str, Any],
                proven: bool, why: str | None, leads_only: bool) -> tuple[_Outcome, bool]:
        """The outcome over a spec's reached records, and whether any of them is live
        (done, open or undecided: not only ignored ones)."""
        pred = spec["predicate"]
        done, open_, unknown, cites = [], [], [], []
        marker_unknown: list[str] = []
        verdicts: dict[str, str] = {}
        for rid in sorted(records):
            verdict, rows = self._excluded(ctx, rid, spec.get("exclude_present"))
            if verdict == "unknown":
                marker_unknown.append(rid)
            if verdict is None:
                verdict, rows = self._classify(ctx, rid, pred)
            cites += [c for c in (_cite(r) for r in rows) if c]
            verdicts[rid] = verdict
            {"done": done, "open": open_, "unknown": unknown,
             _HELD_BACK: unknown, _REMOVED: unknown}.get(verdict, []).append(rid)
        live = done + open_ + unknown
        events = self._events(ctx, sorted(records), spec.get("events", ()))
        hidden = [r for r, verdict in verdicts.items() if verdict == _HELD_BACK]
        removed = {r: ctx.removal(r) for r, verdict in verdicts.items() if verdict == _REMOVED}
        if pred["kind"] == "event":
            done = [r for r in open_ if self._has_event(ctx, r, pred["event_types"])]
            open_ = [r for r in open_ if r not in done]
            events = list(dict.fromkeys(events + self._events(ctx, done, pred["event_types"])))

        def outcome() -> _Outcome:
            # Only records decided live (done or open) are listed: an unreadable record
            # may be ignored (cancelled) for a full-grant reader, so listing it, or an
            # ignored one, could put a record outside the full-grant list (monotonicity).
            cited = [r for r in sorted(done + open_) if len(r) <= 128]
            quantifier = pred.get("quantifier", "any")
            if quantifier == "all" and pred["kind"] in ("status", "po_status"):
                if open_:
                    return _Outcome(NOT_REACHED, cited, events, "absent_in_source",
                                    "status_not_reached", _open_detail(open_), cites)
                if unknown:
                    return self._unreadable(spec, marker_unknown, hidden, removed, cited, events,
                                           cites)
                if not live:
                    return self._none(proven, why, cited, events, cites, bool(records),
                                      leads_only)
                if proven:
                    return _Outcome(REACHED, cited, events, citations=cites)
                return _Outcome(UNKNOWN, cited, events, "unknown", "partial_visibility",
                                ("every visible record is done, but a record this reader "
                                 "cannot see could be open" + (f" ({why})" if why else ""))[:600],
                                cites)
            if done:
                return _Outcome(REACHED, cited, events, citations=cites)
            if unknown:
                return self._unreadable(spec, marker_unknown, hidden, removed, cited, events,
                                           cites)
            if open_:
                if proven:
                    return _Outcome(NOT_REACHED, cited, events, "absent_in_source",
                                    "status_not_reached", _open_detail(open_), cites)
                return _Outcome(UNKNOWN, cited, events, "unknown", "partial_visibility",
                                ("no visible record is done; records this reader cannot see "
                                 "were not checked" + (f" ({why})" if why else ""))[:600], cites)
            return self._none(proven, why, cited, events, cites, bool(records), leads_only)

        return outcome(), bool(live)

    @staticmethod
    def _unreadable(spec: Mapping[str, Any], marker_unknown: Sequence[str],
                    hidden: Sequence[str], removed: Mapping[str, str], cited: list[str],
                    events: list[str], cites: list[dict]) -> _Outcome:
        if hidden and not marker_unknown:
            pred = spec["predicate"]
            prop = (pred.get("property") or (pred.get("ignore") or {}).get("property")
                    or "purchase-order status")
            return _Outcome(UNKNOWN, cited, events, "inaccessible", WITHHELD,
                            f"a record's {prop} is withheld from this reader (a marking it "
                            "lacks), so whether the stage is reached is not stated", cites)
        if removed and not marker_unknown:
            deleted = sorted(r for r, basis in removed.items() if basis == "source_soft_delete")
            if deleted:
                return _Outcome(UNKNOWN, cited, events, "unknown", "deleted_in_source",
                                (f"{len(deleted)} record(s) of this stage are deleted in the "
                                 "source (its own soft-delete column): " + ", ".join(
                                     deleted[:3]) + "; whether the stage is reached is "
                                 "unknown")[:600], cites)
            return _Outcome(UNKNOWN, cited, events, "unknown", "missing_from_source",
                            (f"{len(removed)} record(s) of this stage are no longer returned "
                             "by the source (key-only scan; not proof of a deletion): "
                             + ", ".join(sorted(removed)[:3]) + "; whether the stage is "
                             "reached is unknown")[:600], cites)
        if spec["predicate"]["kind"] == "present" and not marker_unknown:
            return _Outcome(UNKNOWN, cited, events, "unknown", "value_not_stated",
                            f"the record leaves {spec['predicate']['property']} blank or does "
                            "not carry it: unknown, never a no", cites)
        if marker_unknown:
            prop = spec["exclude_present"]["property"]
            return _Outcome(UNKNOWN, cited, events, "unknown", "deleted_marker_unreadable",
                            f"a record's deleted marker ({prop}) is not readable for this "
                            "reader, so whether it still counts is unknown", cites)
        return _Outcome(UNKNOWN, cited, events, "unknown", "status_unreadable",
                        "a record's status is not readable for this reader", cites)

    @staticmethod
    def _none(proven: bool, why: str | None, cited: list[str], events: list[str],
              cites: list[dict], ignored_only: bool, leads_only: bool = False) -> _Outcome:
        if proven:
            return _Outcome(NOT_REACHED, cited, events, "absent_in_source",
                            "no_live_record" if ignored_only else "no_evidence_record",
                            "only ignored (cancelled or deleted) records exist" if ignored_only else
                            "no record of this kind exists for this root in the sources",
                            cites)
        return _Outcome(UNKNOWN, cited, events, "unknown", "absence_unproven",
                        ("no record was found, but absence is not proven"
                         + (f": {why}" if why else ""))[:600], cites, leads_only=leads_only)

    def _excluded(self, ctx: _Ctx, rid: str, rule: Mapping[str, Any] | None
                  ) -> tuple[str | None, list[Any]]:
        """``ignored`` when the record's ``exclude_present`` property holds a value
        (a soft delete), ``unknown`` when that property is carried but not readable,
        None when the record stays evidence."""
        if not rule:
            return None, []
        obj = ctx.obj(rid)
        spec = self._prop(obj["object_type"], rule["property"])
        if spec is None or obj["binding_id"] not in {s["binding"] for s in spec["sources"]}:
            return None, []
        rows = ctx.values(rid).get(rule["property"], [])
        if not rows:
            return "unknown", []
        if any(r["value"] is not None for r in rows):
            return "ignored", rows
        return None, []

    def _classify(self, ctx: _Ctx, rid: str, pred: Mapping[str, Any]
                  ) -> tuple[str, list[Any]]:
        """done / open / unknown / ignored for one evidence record."""
        if ctx.removal(rid):
            return _REMOVED, []
        kind = pred["kind"]
        if kind == "event":
            return "open", []  # decided by its events
        if kind == "present":
            name = pred["property"]
            if self._carried(ctx, rid, name) is None:
                return "unknown", []
            rows = ctx.values(rid).get(name, [])
            filled = [r for r in rows if r["value"] is not None]
            if filled:
                return "done", filled
            if name in ctx.withheld(rid):
                return _HELD_BACK, []
            return ("open" if pred.get("blank", "open") == "open" else "unknown"), rows
        if kind == "exists":
            ignore = pred.get("ignore")
            if not ignore:
                return "done", []
            status, rows = self._status(ctx, rid, ignore["property"], ignore.get("compare", "exact"))
            if status is _UNCARRIED:
                return "done", []
            if status is None:
                return (_HELD_BACK if ignore["property"] in ctx.withheld(rid) else "unknown"), rows
            values = {_norm(v, ignore.get("compare", "exact")) for v in ignore["values"]}
            return ("ignored" if status in values else "done"), rows
        if kind == "po_status":
            return self._po_issued(ctx, rid)
        compare = pred.get("compare", "exact")
        status, rows = self._status(ctx, rid, pred["property"], compare)
        if status is None and pred["property"] in ctx.withheld(rid):
            return _HELD_BACK, rows
        if status is None or status is _UNCARRIED:
            return "unknown", rows
        if status in {_norm(v, compare) for v in pred.get("ignore", ())}:
            return "ignored", rows
        if "done" in pred:
            hit = status in {_norm(v, compare) for v in pred["done"]}
        else:
            hit = status not in {_norm(v, compare) for v in pred["not_done"]}
        return ("done" if hit else "open"), rows

    def _po_issued(self, ctx: _Ctx, rid: str) -> tuple[str, list[Any]]:
        """A purchase-order record by the shared definition's header class of the status
        property its binding carries: issued is done, a draft open, a cancelled record
        ignored; an unmapped, null, disputed or uncarried status unknown."""
        from operating import po_status

        for prop in po_status.HEADER_PROPS:
            status, rows = self._status(ctx, rid, prop, "upper")
            if status is _UNCARRIED:
                continue
            if status is None:
                return (_HELD_BACK if prop in ctx.withheld(rid) else "unknown"), rows
            return {po_status.ISSUED: "done", po_status.NOT_ISSUED: "open",
                    po_status.IGNORED: "ignored"}.get(
                        po_status.header_class(status, po_status.SOURCE_OF[prop]),
                        "unknown"), rows
        return "unknown", []

    def _carried(self, ctx: _Ctx, rid: str, prop: str) -> Mapping[str, Any] | None:
        """The property spec when the record's binding carries ``prop``, else None."""
        obj = ctx.obj(rid)
        spec = self._prop(obj["object_type"], prop)
        if spec is None or obj["binding_id"] not in {s["binding"] for s in spec["sources"]}:
            return None
        return spec

    def _status(self, ctx: _Ctx, rid: str, prop: str, compare: str) -> tuple[Any, list[Any]]:
        """The record's normalized status text, ``_UNCARRIED`` when its binding does
        not carry the property, None when unreadable (no visible value, a null,
        disagreeing values, or a value of it this reader may not read)."""
        if self._carried(ctx, rid, prop) is None:
            return _UNCARRIED, []
        rows = ctx.values(rid).get(prop, [])
        texts = {_norm(r["value"], compare) for r in rows if r["value"] is not None}
        if len(texts) != 1 or any(r["value"] is None for r in rows):
            return None, rows
        if prop in ctx.withheld(rid):
            # a status value this reader may not read may disagree with the one it can
            # (SEMANTIC-005): the status is undecided for it, never the visible value's,
            # so less evidence never makes a stronger conclusion (callers: held back)
            return None, rows
        return texts.pop(), rows

    def _prop(self, tid: str, name: str) -> Mapping[str, Any] | None:
        for prop in self.base.schema.object_types.get(tid, {}).get("properties", ()):
            if prop["name"] == name:
                return prop
        return None
