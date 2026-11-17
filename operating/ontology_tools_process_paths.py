"""The paths of ``ProcessTools``: following a graph's links from a root and deciding
what each path proves.

Split from ``operating/ontology_tools_process.py`` (``_staging_reason`` stays there);
moved unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Sequence
from ontology.schema import BC_COMPANY_NOT_KNOWN
from operating.ontology_tools_policy import _ITEM_ACL_AUDIENCES, NEVER_AUTHORITY
from .ontology_tools_process_graphs import (
    AMBIGUOUS, MAX_EVIDENCE, NEVER_AUTHORITY_RECORD, _BINDING_GAPS, _LINK_GAPS,
    _SOURCE_PREFIX, _TIER_RANK, _link_tier,
)
from .ontology_tools_process_answer import (
    _Ctx, _EVIDENCE, _HIDDEN, _INCOMPLETE, _LEAD, _NOT_READ, _PathResult, _STALE_TEXT,
    _UNCAPTURED, _UNPROVEN, _WITHHELD, _Why, _bc_record, _is_stale,
)
from .ontology_tools_process_helpers import (
    _DROPPED, _IGNORED, _IN_PROGRESS, _NOT_ATTRIBUTABLE, _OK, _UNCARRIED, _UNREADABLE,
    _better, _norm, _worse,
)


class _PathsMixin:
    """Methods of ``ProcessTools`` (``ontology_tools_process_paths``); mixed in there, never used alone."""

    # ------------------------------------------------------------------ paths

    def _paths(self, ctx: _Ctx, root_type: str, oid: str, paths: Sequence[Sequence[Mapping]],
               floor: str, spec_marks: Sequence[str] = (),
               final_bindings: Iterable[str] | None = None,
               side: Mapping[str, Any] | None = None) -> _PathResult:
        """Union of the records reached along ``paths`` from ``oid``. ``proven``:
        every applicable path's reached set is complete for this reader (the
        absence rules in the module docstring). With a quantity ``side``, each
        reached record also gets its attribution state in ``states`` (``_ATTRIBUTION``:
        the best over the chains that reach it, each chain as bad as its worst
        step): ``ignored`` through a record whose status the side's
        ``ignore_through`` drops, ``unreadable`` through one whose status is not
        readable, ``not_attributable`` through a link the side declares so."""
        out = _PathResult(applicable=False, proven=True)
        root_version = ctx.visible([oid]).get(oid)
        fed: set[str] = set()  # last-hop records a counted edge leaves, over all paths
        for path in paths:
            if not path:
                out.applicable = True
                out.records[oid] = root_version
                out.states[oid] = _better(out.states.get(oid), _OK)
                continue
            frontier = {oid: root_version}
            states = {oid: _OK}
            came: list[tuple[str, str]] = []  # the previous hop's counted edges
            applicable = True
            useful = self._useful(path, final_bindings)
            for index, hop in enumerate(path):
                step = self._hop(ctx, frontier, hop, floor, spec_marks, useful[index])
                if index == 0 and not step.applicable:
                    applicable = False
                    break
                out.proven = out.proven and step.proven
                out.reasons += step.reasons
                out.truncated = out.truncated or step.truncated
                out.leads += step.leads
                if side is not None:
                    if index == len(path) - 1:
                        # a header that contributes no line is never a zero quantity
                        fed |= {src for src, _ in step.edges}
                        kept = {h for h in frontier if states.get(h, _OK) not in _DROPPED}
                        out.lineless |= kept
                        if hop["direction"] == "forward":
                            out.nameless |= kept
                            for h in kept:
                                out.via.setdefault(h, set()).update(
                                    {a for a, b in came if b == h} if index else {oid})
                        else:
                            out.named_by |= {src for src, _ in step.edges}
                    came = step.edges
                    states = self._step_states(ctx, side, hop, step, states)
                    for rid, state in states.items():
                        out.counted[rid] = _better(out.counted.get(rid), state)
                frontier = step.records
                if not frontier:
                    break
            if applicable and not self._countable(path, ctx.obj(oid)["binding_id"], floor,
                                                  final_bindings):
                # A path no chain of counted link bindings runs along proves nothing:
                # an empty hop on it is no absence (rule A).
                out.proven = False
                out.reasons.append(_Why(_LEAD, "path_lead_only", (
                    "from this source, " + " -> ".join(h["link"] for h in path)
                    + " carries leads at best")[:600]))
            if applicable:
                out.applicable = True
                out.records.update(frontier)
                for rid in frontier:
                    out.states[rid] = _better(out.states.get(rid), states.get(rid, _OK))
        out.lineless -= fed
        out.nameless -= fed
        return out

    def _countable(self, path: Sequence[Mapping[str, Any]], start: str, floor: str,
                   final: Iterable[str] | None) -> bool:
        """Whether the schema declares a chain of link bindings at or above the tier
        floor along ``path`` from binding ``start`` to an evidence binding: the only
        kind of path whose empty hop may prove an absence."""
        reach, floor_rank = {start}, _TIER_RANK[floor]
        for hop in path:
            link = self.base.schema.link_types[hop["link"]]
            near, far = (("from_binding", "to_binding") if hop["direction"] == "forward"
                         else ("to_binding", "from_binding"))
            reach = {lb.get(far) for lb in link["bindings"] if lb.get(near) in reach
                     and _TIER_RANK.get(lb["tier_ceiling"], 99) <= floor_rank}
            if not reach:
                return False
        return final is None or bool(reach & set(final))

    def _step_states(self, ctx: _Ctx, side: Mapping[str, Any], hop: Mapping[str, Any],
                     step: _PathResult, states: Mapping[str, str]) -> dict[str, str]:
        """Attribution states of one hop's reached records from their sources'."""
        forward = hop["direction"] == "forward"
        blocked = {(b["link"], b["from_binding"]) for b in side.get("not_attributable", ())}
        out: dict[str, str] = {}
        for source, target in step.edges:
            link_from = source if forward else target  # the link row's from_object
            state = states.get(source, _OK)
            if (hop["link"], ctx.obj(link_from)["binding_id"]) in blocked:
                state = _worse(state, _NOT_ATTRIBUTABLE)
            state = _worse(state, self._through(ctx, side, target))
            out[target] = _better(out.get(target), state)
        return out

    def _through(self, ctx: _Ctx, side: Mapping[str, Any], rid: str) -> str:
        """``ignored`` / ``in_progress`` / ``unreadable`` / ok for a record on a side's
        path, from the side's ``ignore_through`` rules for its type (a ``count_only``
        rule's other statuses are in progress)."""
        tid = ctx.obj(rid)["object_type"]
        state = _OK
        for rule in side.get("ignore_through", ()):
            if rule["object_type"] != tid:
                continue
            compare = rule.get("compare", "exact")
            status, _ = self._status(ctx, rid, rule["property"], compare)
            if status is _UNCARRIED:
                continue
            if status is None:
                state = _worse(state, _UNREADABLE)
                continue
            if rule.get("match") == "contains_ci":
                hit = any(v.lower() in status.lower() for v in rule["values"])
            else:
                hit = status in {_norm(v, compare) for v in rule["values"]}
            if rule.get("count_only") and not hit:
                state = _worse(state, _IN_PROGRESS)
            elif hit and not rule.get("count_only"):
                state = _worse(state, _IGNORED)
        return state

    def _useful(self, path: Sequence[Mapping[str, Any]], final: Iterable[str] | None
                ) -> list[set[str] | None]:
        """Per hop, the far-side bindings from which the rest of the path can still
        reach evidence (``final`` at the end; None = any). A link binding to any
        other binding can never carry evidence, so it neither counts nor blocks a
        proof (L9's per-binding rule, applied along the path)."""
        if final is None:
            return [None] * len(path)
        out: list[set[str] | None] = [None] * len(path)
        wanted = set(final)
        for index in range(len(path) - 1, -1, -1):
            out[index] = wanted
            hop = path[index]
            link = self.base.schema.link_types[hop["link"]]
            near, far = (("from_binding", "to_binding") if hop["direction"] == "forward"
                         else ("to_binding", "from_binding"))
            wanted = {lb.get(near) for lb in link["bindings"] if lb.get(far) in wanted}
        return out

    def _hop(self, ctx: _Ctx, frontier: Mapping[str, Any], hop: Mapping[str, Any],
             floor: str, spec_marks: Sequence[str], useful: set[str] | None = None
             ) -> _PathResult:
        link = self.base.schema.link_types[hop["link"]]
        forward = hop["direction"] == "forward"
        near, far = ("from_binding", "to_binding") if forward else ("to_binding", "from_binding")
        out = _PathResult(applicable=False, proven=True)
        floor_rank = _TIER_RANK[floor]
        anchors: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
        for oid in frontier:
            binding = ctx.obj(oid)["binding_id"]
            carriers = [(i, lb) for i, lb in enumerate(link["bindings"]) if lb.get(near) == binding
                        and (useful is None or lb.get(far) in useful)]
            if carriers:
                anchors[oid] = carriers
        if not anchors:
            return out
        out.applicable = True
        strong = [(i, lb) for carriers in anchors.values() for i, lb in carriers
                  if _TIER_RANK.get(lb["tier_ceiling"], 99) <= floor_rank]
        if not strong:
            out.proven = False
            out.reasons.append(_Why(_LEAD, "lead_carrier_only",
                                    f"{hop['link']} links from this source are leads at best"))
        def problem_of(index: int, lb: Mapping[str, Any]) -> tuple[str, str] | None:
            starts = [oid for oid, carriers in anchors.items()
                      if any(i == index for i, _ in carriers)]
            return self._carrier_problem(ctx, hop["link"], index, lb, far,
                                         self._link_marks(link, lb) | set(spec_marks), starts)

        for index, lb in {i: lb for i, lb in strong}.items():
            problem = problem_of(index, lb)
            if problem:
                out.proven = False
                # a stale capture is old, not withheld: its own code, so a reader that
                # judges "as of the capture" (lateness) can tell it from a hidden part
                out.reasons.append(_Why(_EVIDENCE, "carrier_stale" if _is_stale(problem)
                                        else "carrier_unproven", problem[1], problem[0]))
        weak = {i: lb for carriers in anchors.values() for i, lb in carriers
                if _TIER_RANK.get(lb["tier_ceiling"], 99) > floor_rank}
        for index, lb in weak.items():
            # A lead row is reported, never counted (``_side_lines``); a reader must be
            # able to see every lead row a full-grant reader sees, or its answer could
            # be decided where the full-grant one is not.
            problem = problem_of(index, lb)
            if problem:
                out.proven = False
                # A key the projector could not read on some record of a lead carrier
                # changes nothing any reader counts (a lead row is never counted): a lead
                # reason. What the reader may not see stays an evidence reason.
                # A stale capture of a lead carrier is the same: its rows are old, and a
                # lead row is never counted however new.
                tier = (_LEAD if (problem[0] == _UNPROVEN
                                  and problem[1].startswith("the projector"))
                        or _is_stale(problem) else _EVIDENCE)
                out.reasons.append(_Why(tier, "lead_carrier_unproven",
                                        f"lead carrier: {problem[1]}", problem[0]))
        column, other = ("from_object", "to_object") if forward else ("to_object", "from_object")
        rows = list(self.base.store.conn.execute(
            f"SELECT * FROM links WHERE link_type = ? AND {column} IN "
            "(SELECT value FROM json_each(?)) ORDER BY link_id",
            (hop["link"], json.dumps(sorted(anchors)))))
        rows = [r for r in rows if ctx.row_visible(r) and self._evidence_ok(ctx, r)]
        ends = ctx.visible([r[other] for r in rows] + list(anchors))
        by_reader = self._company_by_reader(ctx, hop["link"], link, rows)
        cache: dict[str, str | None] = {}
        reached: dict[str, Any] = {}
        for row in rows:
            target = row[other]
            if target not in ends or (useful is not None
                                      and ctx.obj(target)["binding_id"] not in useful):
                continue
            tier = row["tier"]
            verdict, why = by_reader.get(row["link_id"], (None, ""))
            if verdict == _WITHHELD:
                out.proven = False
                out.reasons.append(_Why(_EVIDENCE, "company_unproven", why, _HIDDEN))
                continue
            if verdict is not None and verdict != AMBIGUOUS:
                tier = verdict  # the one company is proven for this reader: its ceiling
            tier = _link_tier(row, ends, tier)
            if tier == "corroborated" and row["binding_kind"] == "mention" and \
                    not self._corroboration_readable(ctx, row, cache):
                # a mention link counts at its corroborated tier only for a reader of every
                # record its corroboration cites (``ontology.mentions.reader_view``, the one
                # rule plx.around serves): to any other it is the lead its mention is
                tier = "lead"
            if _TIER_RANK[tier] > floor_rank:
                out.proven = False
                out.reasons.append(
                    _Why(_LEAD, AMBIGUOUS, why) if verdict == AMBIGUOUS else
                    _Why(_LEAD, "link_below_tier_floor",
                         f"a {hop['link']} link below the tier floor ({tier}) is not counted"))
                out.leads.append((row[column], target))
                continue
            lineage = ends[target]["lineage"]
            anchor = ends.get(row[column])
            if lineage in NEVER_AUTHORITY and (anchor is None
                                               or anchor["lineage"] not in NEVER_AUTHORITY):
                # R1, stricter than the link: a never-authority record is never evidence
                # for an authority one (an anchor whose version is unknown counts as one)
                out.proven = False
                out.reasons.append(_Why(_LEAD, NEVER_AUTHORITY_RECORD, (
                    f"a {hop['link']} link ({tier}) reaches a {lineage} record: never "
                    "evidence for an authority record (R1), not counted")))
                out.leads.append((row[column], target))
                continue
            reached[target] = ends[target]
            out.edges.append((row[column], target))
        if len(reached) > MAX_EVIDENCE:
            out.truncated = True
            reached = dict(sorted(reached.items())[:MAX_EVIDENCE])
        out.records = reached
        return out

    def _company_by_reader(self, ctx: _Ctx, link_id: str, link: Mapping[str, Any],
                           rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
        """link_id -> (verdict, why) for the visible ``rows`` of a ``company_by_reader``
        binding (a BC record joined on a number, capped at lead by the projector only
        because its company is not known): the binding's ``tier_ceiling`` when this reader
        proves one order carries the number, else ``ambiguous_target`` (several BC
        companies' records name the far record, or the projector found several far
        records: the number names several orders; never a pick) or ``_WITHHELD`` (the
        reader's grant or markings may hide a company, so it could not tell; decided from
        the policy alone, so a narrower reader is never decided where a full-grant reader
        is ambiguous, and nothing it cannot read is named)."""
        flagged = [lb for lb in link["bindings"] if lb.get("company_by_reader")]
        if not flagged:
            return {}

        def binding_of(oid: str) -> str | None:
            row = ctx.obj(oid)
            return row["binding_id"] if row is not None else None

        out: dict[str, tuple[str, str]] = {}
        for lb in flagged:
            ends = {"from_object": lb["from_binding"], "to_object": lb["to_binding"]}
            bc_end = next((col for col, bid in ends.items()
                           if (self._binding(bid) or ("", {}))[1].get("source") == "bc"), None)
            if bc_end is None:
                continue
            named = "to_object" if bc_end == "from_object" else "from_object"
            mine = [r for r in rows if r["binding_kind"] == lb["kind"]]
            ctx.visible([r[col] for r in mine for col in ends])
            mine = [r for r in mine if all(binding_of(r[col]) == bid for col, bid in ends.items())]
            if not mine:
                continue
            # every link of the binding to these far records, not only this hop's rows
            every = [r for r in self.base.store.conn.execute(
                f"SELECT * FROM links WHERE link_type = ? AND binding_kind = ? AND {named} IN "
                "(SELECT value FROM json_each(?)) AND tier <> 'contradicted'",
                (link_id, lb["kind"], json.dumps(sorted({r[named] for r in mine}))))
                if ctx.row_visible(r) and self._evidence_ok(ctx, r)]
            seen = ctx.visible([r[bc_end] for r in every])
            companies: dict[str, set[str | None]] = {}
            for r in every:
                if r[bc_end] in seen and binding_of(r[bc_end]) == ends[bc_end]:
                    ref = _bc_record(ctx.obj(r[bc_end]))
                    companies.setdefault(r[named], set()).add(ref[0] if ref else None)
            source = self._binding(ends[bc_end])[1]["source"]
            covered = self.base.policy.covers_family(ctx.actor, _SOURCE_PREFIX.get(source, "")) \
                and ctx.scope.holds(self._link_marks(link, lb))
            for r in mine:
                why_not = json.loads(r["why_not"])  # a JSON array (store DDL)
                if not all(isinstance(f, Mapping) for f in why_not):
                    continue  # an unreadable cap is never lifted: the row stays as it is
                span = companies.get(r[named], set())
                if any(f.get("feature") == AMBIGUOUS for f in why_not):
                    out[r["link_id"]] = (AMBIGUOUS, (
                        f"a {link_id} link names several records: the number names several "
                        "orders and none is counted")[:600])
                elif not why_not or not all(f.get("feature") == "tier_cap" and f.get(
                        "because") == BC_COMPANY_NOT_KNOWN for f in why_not):
                    continue  # capped for another reason, or not capped: read as it is
                elif not covered:
                    out[r["link_id"]] = (_WITHHELD, (
                        f"{link_id} links count only for a reader who reads every BC company "
                        "and so can tell that one company's records name the number")[:600])
                elif len(span) > 1 or None in span:
                    out[r["link_id"]] = (AMBIGUOUS, (
                        f"{link_id} links to this record start in {len(span)} BC companies: "
                        "the number names an order in each and none is counted")[:600])
                else:
                    out[r["link_id"]] = (lb["tier_ceiling"], "")
        return out

    def _corroboration_readable(self, ctx: _Ctx, row: Mapping[str, Any],
                                cache: dict[str, str | None]) -> bool:
        """Whether this reader sees every record the corroborating features of a stored
        corroborated link cite (``ontology.mentions.corroboration_objects``)."""
        from ontology.mentions import corroboration_objects
        try:
            reason = json.loads(row["reason"])
        except (TypeError, ValueError):
            return False
        cited = corroboration_objects(self.base.store.conn,
                                      reason if isinstance(reason, list) else [], cache)
        return cited is not None and set(ctx.visible(sorted(cited))) >= cited

    def _evidence_ok(self, ctx: _Ctx, row: Mapping[str, Any]) -> bool:
        try:
            handles = json.loads(row["evidence_handles"])
        except ValueError:
            return False
        return isinstance(handles, list) and all(
            isinstance(h, str) and self.base._item_allowed(ctx.actor, h) for h in handles)

    def _carrier_problem(self, ctx: _Ctx, link_id: str, index: int, lb: Mapping[str, Any],
                         far: str, marks: Sequence[str], starts: Sequence[str]
                         ) -> tuple[str, str] | None:
        """(cause, why) a carrier link binding's reached set from ``starts`` may be
        incomplete for this reader, or None when it is proven complete (policy,
        freshness, scoped claims and build report only; never hidden rows)."""
        if not ctx.now_answer:
            return _UNPROVEN, "absence is proven only for an answer about now"
        if ctx.report is None:
            return _UNPROVEN, "the projection has no build report"
        if not ctx.scope.holds(marks):
            return _HIDDEN, "the reader does not hold every marking declared on this evidence"
        via = lb["via"]["binding"] if "via" in lb else None
        keys = frozenset(p["to" if far == "to_binding" else "from"] for p in lb.get("pairs", ()))
        companies = self._start_companies(ctx, link_id, lb, far, starts)
        stale: str | None = None
        for bid in [lb.get(far)] + ([via] if via else []):
            found = self._binding(bid)
            if found is None:
                return _UNPROVEN, f"binding {bid} is not declared"
            tid, binding = found
            if not ctx.scope.holds(self.base.schema.object_types[tid].get("markings", ())):
                return _HIDDEN, f"the reader does not hold every marking declared on {tid}"
            source = binding["source"]
            prefix = _SOURCE_PREFIX.get(source)
            if prefix is None or prefix in _ITEM_ACL_AUDIENCES:
                return _WITHHELD, f"{source} records carry item-level permissions"
            if source == "portal_api":
                granted = self.base.policy.may_read(ctx.actor, f"api:{binding['relation']}")
            elif source == "bc" and companies:
                granted = all(self.base.policy.may_read(
                    ctx.actor, f"bc:{company}/{binding['relation']}") for company in companies)
            else:
                granted = self.base.policy.covers_family(ctx.actor, prefix)
            if not granted:
                return _HIDDEN, f"the reader's grant does not cover every {source} record"
            for gap in ctx.report:
                if gap["where"] == bid and gap["code"] in _BINDING_GAPS:
                    return (_UNCAPTURED if gap["code"] == "source_not_captured" else _UNPROVEN,
                            f"the projector reported {gap['code']} for {bid}")
            fresh = ctx.freshness.get(source)
            if fresh and fresh.get("stale"):
                # said last: a stale capture is the problem only when nothing else is (a
                # verdict read "as captured" must not pass over an unread or cut part)
                stale = stale or source
            if (not fresh or fresh.get("completeness") != "complete") \
                    and not self._relation_complete(ctx, source, bid, binding["relation"]):
                cause = self._claim_cause(ctx, source, binding["relation"],
                                          None if bid == via else keys, starts)
                if cause == _NOT_READ and any(
                        g["where"] == bid and g["code"] == "source_capture_incomplete"
                        for g in ctx.report):
                    cause = _INCOMPLETE
                if cause:
                    return cause, (f"the {source} capture is not known complete and fresh, and "
                                   + ("the read of " + binding["relation"] + " scoped to this "
                                      "record did not finish" if cause == _INCOMPLETE else
                                      "no complete read scoped to this record covers "
                                      + binding["relation"]))
        where = f"{link_id}#{index}"
        names = self._pair_wheres(lb)
        near = "from_binding" if far == "to_binding" else "to_binding"
        # A key unreadable only on the starts' own side hides no link of a start whose own
        # key the projector read: every start already holds this binding's link to the far
        # side (``_keys_read``). Unreadable far-side keys may still point at a start.
        own_side = (self._pair_wheres(lb, near) - self._pair_wheres(lb, far)
                    if self._keys_read(link_id, lb, far, starts) else set())
        for gap in ctx.report:
            if gap["where"] == where and gap["code"] in _LINK_GAPS:
                return _UNPROVEN, f"the projector reported {gap['code']} for {where}"
            if gap["where"] in names and gap["where"] not in own_side and (
                    gap["code"].startswith("parse_") or gap["code"] == "path_absent"):
                if gap["code"] == "path_absent" and self._key_unserved(gap["where"]):
                    # The source carries the key on no record (the reporting API does not
                    # serve it): no link through it is ever made, so it hides none from
                    # this answer; said as a caveat, never a reason to state nothing. A
                    # key served on some records but absent or unreadable on others
                    # still leaves the hop unproven.
                    ctx.unserved.add(gap["where"])
                    continue
                return _UNPROVEN, f"the projector could not read a key of {where}"
        return (_INCOMPLETE, _STALE_TEXT.format(source=stale)) if stale else None

    def _start_companies(self, ctx: _Ctx, link_id: str, lb: Mapping[str, Any], far: str,
                         starts: Sequence[str]) -> frozenset[str]:
        """The BC companies of ``starts`` when every binding of ``lb`` is a BC capture and
        every ``link_id`` row on ``starts`` is projector-made, else empty. The projector
        pairs BC records of one capture company only (``ontology.schema.
        link_company_scope``), so the far records a full-grant reader reaches are in the far
        binding's unit of these companies, and a grant of those units (the policy's, never
        which units the store holds) proves them visible. A row the projector did not
        derive from a schema binding (a person-reviewed link, ``ontology.reviews``) has no
        company check: any such row on the starts, visible to this reader or not, leaves the
        hop to the whole-family grant."""
        names = [lb.get("from_binding"), lb.get("to_binding")]
        if "via" in lb:
            names.append(lb["via"].get("binding"))
        if not starts or not all(
                (found := self._binding(name)) is not None and found[1].get("source") == "bc"
                for name in names):
            return frozenset()
        refs = [_bc_record(ctx.obj(oid)) for oid in starts]
        if None in refs:
            return frozenset()
        link = (self.base.schema.link_types or {}).get(link_id) or {}
        kinds = sorted({b.get("kind") for b in link.get("bindings", ())} - {None, "reviewed"})
        column = "from_object" if far == "to_binding" else "to_object"
        other = self.base.store.conn.execute(
            f"SELECT 1 FROM links WHERE link_type = ? AND {column} IN "
            "(SELECT value FROM json_each(?)) AND binding_kind NOT IN "
            "(SELECT value FROM json_each(?)) LIMIT 1",
            (link_id, json.dumps(sorted(starts)), json.dumps(kinds))).fetchone()
        return frozenset() if other else frozenset(ref[0] for ref in refs)

    @staticmethod
    def _relation_complete(ctx: _Ctx, source: str, bid: str, relation: str) -> bool:
        """A Portal or BC binding whose own relation was fully traversed by ``ctx.at``, is
        not stale, and whose capture the build report does not call incomplete. A BC
        relation is whole only per company:
        its entry is complete only when every
        captured company's unit of the set is settled (``ontology/project_bc.describe``),
        and it also needs a ``company=`` claim on the relation with every such claim
        complete; the relation entry alone (no claims, or one company's unit not settled)
        proves nothing."""
        if source == "bc":
            whole = [c for c in ctx.claims.get(relation, ()) if c.anchors is None]
            if not whole or not all(c.complete for c in whole):
                return False
        elif source != "portal_api":
            return False
        finished = ctx.relations.get((source, relation))
        return finished is not None and finished <= ctx.at and not any(
            g["where"] == bid and g["code"] == "source_capture_incomplete" for g in ctx.report)

    @staticmethod
    def _claim_cause(ctx: _Ctx, source: str, relation: str, keys: frozenset[str] | None,
                     starts: Sequence[str]) -> str | None:
        """None when a complete scoped claim on ``relation`` covers every record in
        ``starts``: a ``company=`` claim of its company, or (``keys``: the link binding's
        far-side key fields, None for a ``via`` binding) an anchored claim naming it whose
        ``by`` holds every key field. Else ``source_capture_incomplete`` when a fitting
        claim says the read did not finish, ``scope_not_read`` when none fits."""
        claims = ctx.claims.get(relation, ()) if source == "bc" else ()
        for oid in starts:
            ref = _bc_record(ctx.obj(oid))
            fitting = [c for c in claims if ref is not None and c.company == ref[0] and (
                c.anchors is None or (keys and ref[1] in c.anchors and keys <= c.by))]
            if not any(c.complete for c in fitting):
                return _INCOMPLETE if fitting else _NOT_READ
        return None if starts else _NOT_READ

    def _link_marks(self, link: Mapping[str, Any], lb: Mapping[str, Any]) -> set[str]:
        """Markings the projector puts on a link row of this binding (ontology/project.py:
        the link type's, both endpoint types' and those of the properties its pairs
        read), so a reader without one of them could miss a link it cannot see."""
        types = self.base.schema.object_types
        marks = set(link.get("markings", ()))
        for end in ("from", "to"):
            marks |= set(types.get(link[end], {}).get("markings", ()))
        names = self._pair_wheres(lb)
        for tid, spec in types.items():
            for prop in spec.get("properties", ()):
                if any(f"{tid}.{prop['name']}@{s['binding']}" in names
                       for s in prop.get("sources", ())):
                    marks |= set(prop.get("markings", ()))
        return marks

    def _binding(self, bid: str | None) -> tuple[str, Mapping[str, Any]] | None:
        for tid, spec in self.base.schema.object_types.items():
            for binding in spec.get("bindings", ()):
                if binding["id"] == bid:
                    return tid, binding
        return None

    def _key_unserved(self, where: str) -> bool:
        """Whether the property a build-report ``where`` (``Type.property@binding``) names
        holds no value on any record of that binding while the binding has records: the
        source does not serve the field at all. Cached per tools instance (one store)."""
        cache = self.__dict__.setdefault("_unserved_cache", {})
        if where not in cache:
            found = re.fullmatch(r"([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)@([A-Za-z0-9_.]+)", where)
            unserved = False
            if found:
                tid, prop, binding = found.groups()
                conn = self.base.store.conn
                held = conn.execute("SELECT 1 FROM objects WHERE object_type = ? AND "
                                    "binding_id = ? LIMIT 1", (tid, binding)).fetchone()
                valued = conn.execute(
                    "SELECT 1 FROM objects o JOIN object_values v ON v.object_id = "
                    "o.object_id WHERE o.object_type = ? AND o.binding_id = ? AND "
                    "v.property = ? LIMIT 1", (tid, binding, prop)).fetchone()
                unserved = held is not None and valued is None
            cache[where] = unserved
        return cache[where]

    def _keys_read(self, link_id: str, lb: Mapping[str, Any], far: str,
                   starts: Sequence[str]) -> bool:
        """Every start holds a ``link_id`` row of ``lb``'s kind whose other end is a
        record of ``lb``'s far binding, with the start on ``lb``'s near side: the projector
        read each start's own key of this binding (a single-pair binding only: with more
        pairs a row proves the pairs matched, not which key a second link would use)."""
        if not starts or len(lb.get("pairs", ())) != 1 or "via" in lb:
            return False
        column, other = (("from_object", "to_object") if far == "to_binding"
                         else ("to_object", "from_object"))
        found = self.base.store.conn.execute(
            f"SELECT COUNT(DISTINCT l.{column}) FROM links l JOIN objects o "
            f"ON o.object_id = l.{other} WHERE l.link_type = ? AND l.binding_kind = ? "
            "AND l.superseded_at IS NULL AND o.binding_id = ? AND l."
            f"{column} IN (SELECT value FROM json_each(?))",
            (link_id, lb.get("kind"), lb.get(far), json.dumps(sorted(set(starts))))
        ).fetchone()[0]
        return found == len(set(starts))

    def _pair_wheres(self, lb: Mapping[str, Any], side: str | None = None) -> set[str]:
        """Build-report ``where`` names (``Type.property@binding``) of the properties
        that read a link binding's key paths (``side``: ``from_binding`` or
        ``to_binding`` only)."""
        if self._where_index is None:
            index: dict[tuple[str, str], list[str]] = {}
            for tid, spec in self.base.schema.object_types.items():
                for prop in spec.get("properties", ()):
                    for source in prop.get("sources", ()):
                        index.setdefault((source["binding"], source["path"]), []).append(
                            f"{tid}.{prop['name']}@{source['binding']}")
            self._where_index = {k: tuple(v) for k, v in index.items()}
        out: set[str] = set()
        pairs = [(lb.get("from_binding"), p["from"], lb.get("to_binding"), p["to"])
                 for p in lb.get("pairs", ())]
        for fb, fp, tb, tp in pairs:
            if side in (None, "from_binding"):
                out.update(self._where_index.get((fb, fp), ()))
            if side in (None, "to_binding"):
                out.update(self._where_index.get((tb, tp), ()))
        return out
