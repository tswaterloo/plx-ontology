"""The output of ``InvestigateTools``: shaping the answer, its pages and its gaps into
the ``plx.investigate`` result.

Split from ``operating/ontology_tools_investigate.py``; moved unchanged.
"""

from __future__ import annotations

import copy
from collections import Counter
import datetime as dt
import hashlib
from typing import Any, Callable, Mapping, Sequence
from ontology.json_contract import validate
from ontology.types import Actor
from . import answerers
from .explanations import FRESHNESS_SOURCES
from .ontology_tools_policy import _envelope_bytes
from .ontology_tools_investigate_gaps import _error, _merge_one_group
from .ontology_tools_investigate_plans import (
    MAX_ANSWER, MAX_ANSWERERS, _Collection, _ENTRY_SLACK, _EVIDENCE_TIERS,
    _GAP_DETAIL_FLOOR, _Run, _canonical, _settle_refused,
)
from .ontology_tools_investigate_pages import (
    DETAIL_LISTS, GAPS_CUT, MIN_GAPS_KEPT, _EvidenceNote, _answer_units, _beside,
    _cite_key, _conflict_cites, _conflicts_on_detail_pages, _detail_base, _detail_items,
    _detail_lost, _detail_of, _drop_empty_cut_note, _edge_cited, _gaps_cut_note,
    _halve_gap_list, _is_cut_note, _later_base, _later_page, _mark_cut,
    _note_conflicts_cut, _page_base, _page_conflicts, _question, _too_large,
    _write_cursor,
)
from .ontology_tools_investigate_follow_ups import DRAFT_ACTION


class _OutputMixin:
    """Methods of ``InvestigateTools`` (``ontology_tools_investigate_output``); mixed in there, never used alone."""

    # ------------------------------------------------------------------ output

    def _meta(self, run: _Run | None, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if run is not None and run.meta is not None:
            meta = {k: run.meta[k] for k in ("schema_digest", "as_known_at", "freshness")
                    if k in run.meta}
        else:
            moment = arguments.get("as_known_at") or self.clock().astimezone(
                dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            meta = {"schema_digest": (self.schema.digest_hex if self.schema else None)
                    or "0" * 64, "as_known_at": moment, "freshness": []}
        if run is not None:
            meta["procedure"] = run.plan.procedure
        return meta

    def _answer(self, run: _Run, arguments: Mapping[str, Any],
                cursor: tuple[int, int, str] | None = None) -> dict[str, Any]:
        blocked = bool({"ambiguous_root", "root_not_found", "root_lead_only"}
                       & set(run.reasons))
        _settle_refused(run)
        answer, cited = ([], []) if blocked else self._evidence(run)
        claims = self._claim_checks(run, answer) if not blocked else {
            "checked": False, "passed": False, "claims": 0, "failures": []}
        state = "blocked" if blocked else ("partial" if run.reasons else "answerable")
        if not blocked and answer and all(e.get("value") is None for e in answer):
            state = "blocked"
            run.reason("no_evidence_found")
        root_ids = (run.collections.get("root") or _Collection()).ids
        hidden = run.withheld_objects
        objects = [o for oid, o in run.objects.items() if oid not in hidden]
        edges = [e for e in run.edges.values()
                 if e.get("from") not in hidden and e.get("to") not in hidden]
        out: dict[str, Any] = {
            "procedure": run.plan.procedure,
            "answerability": {"state": state, "reasons": sorted(run.reasons)[:20]},
            "chain": {"objects": objects[:100], "edges": edges[:200]},
            "values": cited[:400],
            "gaps": [dict(gap, who_can_answer=self._answerers(gap, root_ids))
                     for gap in run.gaps[:50]],
            "conflicts": run.conflicts[:50],
            "steps": {"used": run.used, "budget": run.budget},
            "completeness": self._completeness(run, len(objects)),
            "meta": self._meta(run, arguments),
            "answer": answer,
            "claim_checks": claims,
            "trace": run.trace[:80],
        }
        if len(root_ids) == 1 and root_ids[0] in run.objects:
            out["root"] = run.objects[root_ids[0]]
        if run.candidates:
            out["candidates"] = run.candidates
        if run.withheld is not None:
            out["withheld"] = run.withheld
        if "follow_ups" in arguments and self.follow_ups is not None:
            # Recorded from the whole answer before paging, so every page keeps room for them.
            from .cases import CaseNotFound
            try:
                done = self.follow_ups(run.actor, arguments["follow_ups"]["case_id"], out)
            except CaseNotFound:
                return _error("case_not_found", "no such case for this reader; nothing recorded")
            # Compact, as they repeat on every page: the gaps each records (by index), its
            # case record, ``draft`` for a draft request; an outcome other than recorded and
            # a read-back other than verified only when they occur.
            out["follow_ups"] = [{
                "gaps": item["gaps"],
                **({"outcome": item["outcome"]} if item["outcome"] != "recorded" else {}),
                **({"draft": True} if item["action"] == DRAFT_ACTION else {}),
                **({"record_id": item["record_id"]} if item.get("record_id") else {}),
                # The refusal class only: a store refusal's code may carry its message.
                **({"refused": item["refused"].split(":", 1)[0][:64]}
                   if item.get("refused") else {}),
                **({"read_back": item["read_back"]}
                   if item.get("read_back") not in (None, "verified") else {})}
                for item in done]
        return self._page(out, arguments, run.actor, cursor)

    def _answerers(self, gap: Mapping[str, Any], roots: Sequence[str]) -> list[str]:
        """Who can answer ``gap``, through the one resolver (``operating.answerers``: a Portal
        Department code, else a typed ``undeclared:`` label): the engine's named answerer;
        else the owner_role of the process stages that evidence the source records the gap
        names or searched (an object id: its type and binding; ``Type.property``: that
        property's stages; ``Type`` or ``Type.link``: the type's), else ``undeclared:`` their
        owners (the type's bindings in the source families the gap searched, every one when
        it names none; only the bound one for a property); else the owner of a source family
        it searched; else the same for the root records (``roots``: their object ids)."""
        if gap.get("who_can_answer"):
            return answerers.resolve(named=gap["who_can_answer"])
        types = self.schema.object_types if self.schema is not None else {}
        searched = list((gap.get("searched") or {}).get("sources") or ())

        def about(names: Sequence[str]) -> tuple[list[tuple[str, str, str]], list[str]]:
            subjects: list[tuple[str, str, str]] = []
            owners: list[str] = []
            for name in names:
                head = name.split(" ", 1)[0]
                type_id, colon, rest = head.partition(":")
                binding, prop = (rest.split(":", 1)[0], "") if colon else ("", "")
                if not colon:
                    type_id, _, prop = head.partition(".")
                if type_id not in types:
                    continue
                subjects.append((type_id, prop, binding))
                spec = types[type_id]
                bound = {b.get("id"): b for b in spec.get("bindings") or ()}
                props = spec.get("properties") or ()
                props = ([{"name": k, **v} for k, v in props.items()]
                         if isinstance(props, Mapping) else props)
                used = [binding] if binding in bound else [
                    src.get("binding") for p in props if p.get("name") == prop
                    for src in p.get("sources") or ()] or [
                    bid for bid, b in bound.items() if b.get("source") in searched] or list(bound)
                owners += [f"{FRESHNESS_SOURCES.get(b['source'], b['source'])} owner of "
                           f"{b.get('relation') or bid}"
                           for bid in used for b in (bound.get(bid) or {},) if b.get("source")]
            return subjects, owners

        found = answerers.resolve(*about([*searched, str(gap.get("subject") or "")]))
        if not found:
            found = answerers.resolve(owners=[f"{FRESHNESS_SOURCES[name]} owner"
                                              for name in searched
                                              if name in FRESHNESS_SOURCES])
        if not found:
            found = answerers.resolve(*about(roots))
        return found[:MAX_ANSWERERS] or [answerers.typed("Workbench operator")]

    def _page(self, out: dict[str, Any], arguments: Mapping[str, Any], actor: Actor,
              cursor: tuple[int, int, str, Any] | tuple[int, int, str] | None
              ) -> dict[str, Any]:
        """The page of the answer the cursor names (the first without one), see Pages in the
        module docstring; an answer that fits is returned whole, as before.

        The chain, values and conflicts a page has no room for are never only cut: when
        the first page cannot hold them all (``_fit`` halves them to fit), they are listed
        whole on detail pages after the answer's last page (``_detail_page``), reached by
        the same ``next_cursor`` (AUD-11: late_po, chain, order_margin and
        counterparty_position came back truncated, ``result_too_large``, with no way to
        read the rest)."""
        answer = out["answer"]
        digest = hashlib.sha256(_canonical(answer).encode("utf-8")).hexdigest()[:16]
        if cursor is not None and cursor[2] != digest:
            return _error("cursor_stale", "the answer changed since the previous page was read",
                          "ask again without cursor; pages of two answers are never joined")
        detail = _detail_of(out)
        detail_digest = hashlib.sha256(_canonical(detail).encode("utf-8")).hexdigest()[:16]
        wanted = cursor[3] if cursor is not None and len(cursor) > 3 else None
        fingerprint = _question(arguments, actor)
        if wanted is not None and wanted[1] != detail_digest:
            return _error("cursor_stale", "the answer changed since the previous page was read",
                          "ask again without cursor; pages of two answers are never joined")
        first = self._answer_page(copy.deepcopy(out), None, digest, fingerprint, None)
        if not _detail_lost(first, detail, answer):
            if wanted is not None:
                return _error("invalid_cursor", "the cursor is past the end of this answer",
                              "ask again without cursor")
            if cursor is None:
                return first
            return self._answer_page(out, cursor, digest, fingerprint, None)
        bounds = self._detail_bounds(out, detail)
        if wanted is not None:
            if wanted[0] >= len(bounds):
                return _error("invalid_cursor", "the cursor is past the end of this answer",
                              "ask again without cursor")
            return self._detail_page(out, detail, bounds, cursor[0], wanted[0], digest,
                                     detail_digest, fingerprint)
        return self._answer_page(out, cursor, digest, fingerprint,
                                 (len(bounds), detail_digest))

    def _answer_page(self, out: dict[str, Any], cursor: Any, digest: str, fingerprint: str,
                     details: tuple[int, str] | None) -> dict[str, Any]:
        """A page of the answer's entries (``_page``); ``details``: (detail pages after the
        answer's last page, their digest), None when there are none."""
        answer = out["answer"]
        shared, units = _answer_units(answer)
        flat = [e for unit in units for e in unit]
        page, start = cursor[:2] if cursor is not None else (1, 0)
        if cursor is not None and start >= len(flat):
            return _error("invalid_cursor", "the cursor is past the end of this answer",
                          "ask again without cursor")
        self._make_room(out, shared, units)
        bounds = self._bounds(out, shared, units, start)
        if bounds is None and self._summarise_gaps(out, shared, units):
            bounds = self._bounds(out, shared, units, start)
        if bounds is None:   # an entry wider than any page: one page each, working dropped
            bounds = [(at, at + 1) for at in range(start, sum(map(len, units)))] or [(0, 0)]
        extra = details[0] if details else 0
        if page == 1 and len(bounds) == 1 and not extra:
            out["answer"] = answer[:MAX_ANSWER]
            return self._fit(out)
        first, end = bounds[0]
        kept = {id(e) for e in shared} | {id(e) for e in flat[first:end]}
        out["conflicts"] = _page_conflicts(out, [e for e in answer if id(e) in kept],
                                           page == 1)
        out["answer"] = [e for e in answer if id(e) in kept][:MAX_ANSWER]
        cited = {(e["citation"]["l1_handle"], e["citation"]["field_path"])
                 for e in out["answer"] if e.get("citation")}
        out["values"] = [v for v in out["values"]
                         if (v["citation"]["l1_handle"], v["citation"]["field_path"]) in cited]
        pages = page + len(bounds) - 1
        out["answer_page"] = {"page": page, "pages": pages + extra,
                              "returned": len(out["answer"]), "total": len(answer)}
        if page > 1:
            _later_page(out)
            out["answer_page"]["gaps_on_page"] = 1
        if end < len(flat):
            following = _write_cursor(page + 1, end, digest, fingerprint)
        elif details:   # the answer's last page: the detail pages follow
            following = _write_cursor(page + 1, len(flat), digest, fingerprint,
                                      (0, details[1]))
        else:
            following = None
        out["completeness"] = dict(
            out["completeness"], complete=False, truncated=True,
            reason=out["completeness"].get("reason") or "answer_paged",
            next_cursor=following)
        result = self._squeeze(self._fit(out))
        if details:
            _conflicts_on_detail_pages(result)
        return result

    def _detail_bounds(self, out: Mapping[str, Any], detail: Mapping[str, list[Any]]
                       ) -> list[tuple[int, int]]:
        """The detail pages' ranges over ``_detail_items(detail)``: in order, as many
        items as fit beside what a detail page repeats (``_detail_base``) and within each
        list's contract bound (``DETAIL_LISTS``); at least one item a page, so paging
        always advances and no item is left out."""
        cap = self.page_cap
        base = _detail_base(out, (10 ** 6, 10 ** 6, 10 ** 6), "0" * 16, "0" * 16)
        size0 = _envelope_bytes(base)
        empty = _envelope_bytes({"a": []})
        items = _detail_items(detail)
        bounds: list[tuple[int, int]] = []
        first, size, counts = 0, size0, Counter()
        for at, (key, item) in enumerate(items):
            cost = _envelope_bytes({"a": [item]}) - empty + _ENTRY_SLACK
            if at > first and (size + cost > cap or counts[key] + 1 > DETAIL_LISTS[key]):
                bounds.append((first, at))
                first, size, counts = at, size0, Counter()
            size += cost
            counts[key] += 1
        if items:
            bounds.append((first, len(items)))
        return bounds

    def _detail_page(self, out: dict[str, Any], detail: Mapping[str, list[Any]],
                     bounds: list[tuple[int, int]], page: int, index: int, digest: str,
                     detail_digest: str, fingerprint: str) -> dict[str, Any]:
        """Detail page ``index`` (``_detail_bounds``), page ``page`` of the answer: no
        answer entries (``returned`` 0) and no gaps (``gaps_on_page`` 1: the first page
        lists them), the chain objects, edges, cited values and conflicts of its range.
        An item that does not fit even alone is trimmed as ``_fit`` trims it (an edge's
        feature citations), never dropped unsaid."""
        first, end = bounds[index]
        pages = page - index - 1 + len(bounds)
        result = _detail_base(out, (page, pages, len(out["answer"])), digest, fingerprint)
        part: dict[str, list[Any]] = {key: [] for key in DETAIL_LISTS}
        for key, item in _detail_items(detail)[first:end]:
            part[key].append(item)
        result["chain"] = {"objects": part["objects"], "edges": part["edges"]}
        result["values"] = part["values"]
        result["conflicts"] = part["conflicts"]
        result["completeness"] = dict(
            result["completeness"],
            next_cursor=_write_cursor(page + 1, 0, digest, fingerprint,
                                      (index + 1, detail_digest))
            if index + 1 < len(bounds) else None)
        return self._fit(result)

    def _summarise_gaps(self, out: dict[str, Any], shared: list[dict[str, Any]],
                        units: list[list[dict[str, Any]]]) -> bool:
        """Room for the widest unit when what every page repeats leaves none even once the
        unit is split (dev F29 item_lineage: 50 gaps filled every page and the answer was
        refused): the plan trace, then the gaps sharing a state and a reason merged one group
        at a time (``_merge_one_group``: every gap still counted), then the gap list halved
        (never below ``MIN_GAPS_KEPT`` gaps nor past a gap a follow-up records), the dropped
        gaps counted by reason in one ``gaps_truncated`` gap, until the widest unit fits a
        page alone or nothing is left to cut. Whether anything was cut (the answer is then
        cut: ``result_too_large``, on every page)."""
        widest = max(units, key=lambda u: _envelope_bytes({"a": u}), default=[])
        dropped: Counter[str] = Counter()
        changed = False
        while not self._trims_to_fit(dict(base := _page_base(out, shared), conflicts=_beside(
                out, base, widest)), list(widest)):
            held = max((i for item in out.get("follow_ups") or () for i in item["gaps"]),
                       default=-1)
            gaps = [g for g in out.get("gaps") or () if g.get("reason") != GAPS_CUT]
            keep = max(len(gaps) // 2, held + 1, MIN_GAPS_KEPT)
            if len(out.get("trace") or ()) > 8:
                out["trace"] = out["trace"][: len(out["trace"]) // 2]
            elif _merge_one_group(out):
                pass
            elif keep < len(gaps):
                dropped.update(str(g.get("reason")) for g in gaps[keep:])
                out["gaps"] = gaps[:keep] + [_gaps_cut_note(dropped)]
            else:
                break
            changed = True
        if changed:
            out["completeness"] = dict(
                out["completeness"], complete=False, truncated=True,
                reason=out["completeness"].get("reason") or "result_too_large")
        return changed

    def _squeeze(self, out: dict[str, Any]) -> dict[str, Any]:
        """A page ``_fit`` left over the bound (one entry wider than any page): the working
        of its widest entries (``steps``, ``basis``, ``inputs``) dropped, widest first, until
        it fits; the value, its check and citation stay, and the entry says ``inputs_cut``
        (named unsupported by the ``evidence_truncated`` gap, ``_EvidenceNote``)."""
        if _envelope_bytes(out) <= self.page_cap:
            return out
        worked = [e for e in out["answer"]
                  if any(key in e for key in ("steps", "basis", "inputs"))]
        note = _EvidenceNote(out, [e["name"] for e in out["answer"] if e.get("inputs_cut")
                                   or any(e is w for w in worked)], len(worked))
        squeezed = 0
        for entry in sorted(worked, key=lambda e: -_envelope_bytes({"a": [e]})):
            if _envelope_bytes(out) <= self.page_cap:
                break
            for key in ("steps", "basis", "inputs"):
                entry.pop(key, None)
            entry["inputs_cut"] = True
            squeezed += 1
            _mark_cut(out)
        note.settle(squeezed)
        return out

    def _bounds(self, out: Mapping[str, Any], shared: list[dict[str, Any]],
                units: list[list[dict[str, Any]]], start: int) -> list[tuple[int, int]] | None:
        """Ranges of the pages from ``start``, as positions in the units' entries laid end
        to end (what a cursor names, so a page starts exactly where the previous ended):
        each as many whole units as fit beside what the page repeats (the first page: the
        shared entries, gaps, conflicts, trace and follow-ups; a later page: the shared
        entries and the conflicts its entries cite, ``_later_page``) and the rest of the
        result (chain and values, which ``_fit`` trims, left out); a unit that fits no
        page alone even as ``_fit`` trims it (or the rest of one a previous page began) is
        split across consecutive pages in its order, as many of its entries on each as fit
        (at least one), so no entry is ever left out. None when one entry fits no page
        even alone beside the shared entries and trimmed: the answer is too large, refused
        before any page rather than a page over the bound."""
        cap = self.page_cap
        frames = {True: _page_base(out, shared), False: _later_base(out, shared)}
        sizes = {key: _envelope_bytes(base) for key, base in frames.items()}

        def cost(head: bool, entries: list[dict[str, Any]]) -> int:
            base = frames[head]
            return (_envelope_bytes(dict(base, answer=list(shared) + entries,
                                         conflicts=_beside(out, base, entries)))
                    - sizes[head] + _ENTRY_SLACK * len(entries))

        def fits(head: bool, entries: list[dict[str, Any]]) -> bool:
            return self._trims_to_fit(dict(frames[head], conflicts=_beside(
                out, frames[head], entries)), entries)

        segments: list[tuple[int, list[dict[str, Any]]]] = []  # (first position, entries)
        position = 0
        for unit in units:
            if position + len(unit) > start:
                skip = max(start - position, 0)
                segments.append((position + skip, unit[skip:]))
            position += len(unit)
        bounds: list[tuple[int, int]] = []
        head = start == 0   # the first page lists what later pages leave to it
        first, size, count = start, sizes[head], len(shared)

        def turn(at: int) -> None:
            nonlocal first, size, count, head
            bounds.append((first, at))
            head = False
            first, size, count = at, sizes[head], len(shared)

        for at, entries in segments:
            whole = cost(head, entries)
            if at > first and (size + whole > cap or count + len(entries) > MAX_ANSWER):
                turn(at)
                whole = cost(head, entries)
            if (size + whole <= cap or at == first and fits(head, entries)) \
                    and count + len(entries) <= MAX_ANSWER:
                size += whole
                count += len(entries)
                continue
            for offset, entry in enumerate(entries):  # fits no page alone: split it
                one = cost(head, [entry])
                if at + offset > first and (size + one > cap or count + 1 > MAX_ANSWER):
                    turn(at + offset)
                    one = cost(head, [entry])
                if at + offset == first and size + one > cap and not fits(head, [entry]):
                    return None  # not even alone and trimmed: no page can hold it
                size += one
                count += 1
        bounds.append((first, position))
        return bounds

    def _make_room(self, out: dict[str, Any], shared: list[dict[str, Any]],
                   units: list[list[dict[str, Any]]]) -> None:
        """Drop the conflicts no answer entry cites, last first, until the widest unit fits
        a page beside what every page repeats (the shared entries, gaps and conflicts): the
        same for every page of one answer, so the pages pack units, never one each."""
        conflicts = out.get("conflicts") or []
        cited = {_cite_key(e.get("citation")) for e in out["answer"]} - {None}
        candidates = [i for i in reversed(range(len(conflicts)))
                      if not cited & _conflict_cites(conflicts[i])]
        if not candidates or not units:
            return
        base = _page_base(out, shared)
        fixed = _envelope_bytes(base)
        widest = max(_envelope_bytes(dict(base, answer=list(shared) + unit)) - fixed
                     + _ENTRY_SLACK * len(unit) for unit in units)
        excess = fixed + widest - self.page_cap
        if excess <= 0:
            return
        before = _envelope_bytes({"g": out.get("gaps") or []})
        _note_conflicts_cut(out, 0)
        excess += _envelope_bytes({"g": out.get("gaps") or []}) - before
        empty = _envelope_bytes({"c": []})
        cut: list[int] = []
        while candidates and excess > 0:
            index = candidates.pop(0)
            cut.append(index)
            excess -= _envelope_bytes({"c": [conflicts[index]]}) - empty
        for index in sorted(cut, reverse=True):
            del conflicts[index]
        _note_conflicts_cut(out, len(cut))
        out["completeness"] = dict(
            out["completeness"], complete=False, truncated=True,
            reason=out["completeness"].get("reason") or "result_too_large")

    def _trims_to_fit(self, base: Mapping[str, Any], entries: list[dict[str, Any]]) -> bool:
        """Whether a page of ``entries`` alone (beside the shared entries of ``base``) fits
        once ``_fit`` trims it with every gap kept (``keep_gaps``), so it stays whole rather
        than split. Gaps are not merged or dropped to keep a unit whole: a split loses
        nothing, so it comes first, and the gaps are summarised (``_summarise_gaps``) only
        when even a split leaves no room."""
        page = copy.deepcopy(dict(base, answer=list(base["answer"]) + entries))
        return _envelope_bytes(self._fit(page, keep_gaps=True)) <= self.page_cap

    @staticmethod
    def _completeness(run: _Run, total: int) -> dict[str, Any]:
        returned = min(total, 100)
        reason = run.truncated or ("result_cap" if total > 100 else None)
        out: dict[str, Any] = {"complete": reason is None, "truncated": reason is not None,
                               "returned": returned, "total": total, "next_cursor": None}
        if reason:
            out["reason"] = reason
        return out

    def _fit(self, out: dict[str, Any], keep_gaps: bool = False) -> dict[str, Any]:
        """Bound the result to the envelope (``page_cap``: the host's labels fit beside it),
        each step only while it is still too large, the evidence nothing lists again last:

        1. what the detail pages list whole when this page leaves it out (``_page``): the
           feature citations of chain edges (edges below the evidence tiers first, a lead's
           citations backing no counted value, then the rest; last edge first in each; the
           edge, its tier and features stay), then chain objects (only the display of an
           edge's ends, which ``plx.object`` resolves), then edges, then cited values, each
           list halved, then the conflicts no answer entry cites.
        2. the gaps: those sharing a state and a reason merged into one, largest group first
           (``_merge_one_group``: a count, their subjects, every source they searched and who
           can answer them); then the longest gap details: a gap a follow-up holds loses it
           (the case record keeps it whole), any other is halved down to
           ``_GAP_DETAIL_FLOOR`` characters; then the gap list halved past the gaps a
           follow-up holds and ``MIN_GAPS_KEPT`` more, one ``gaps_truncated`` gap counting
           the rest by reason. A cut-list note (``<list>_truncated``) is never merged,
           shortened or dropped.
        3. the evidence no other page lists: the records and cells of the largest entries
           (the value and its citation stay; an entry whose value rested on them says
           ``inputs_cut``), stated by one ``evidence_truncated`` gap naming every figure it
           left unsupported, so no figure is shown without its records as if it were whole.
        4. the conflicts on cited records (the detail pages list them).

        ``keep_gaps`` (the split decision, ``_trims_to_fit``): step 2 only shortens details;
        no gap is merged or dropped."""
        cap = self.page_cap
        # 1. what the detail pages list whole
        edges = out["chain"]["edges"]
        self._strip_edge_citations(out, sorted(
            reversed(range(len(edges))), key=lambda i: edges[i].get("tier") in _EVIDENCE_TIERS))
        for holder, key in ((out["chain"], "objects"), (out["chain"], "edges"),
                            (out, "values")):
            self._halve(out, holder, key)
        # a conflict no answer entry cites goes before any entry's own inputs: the entry is
        # the answer, the conflict context (the conflicts_truncated gap counts the drop)
        cited = {_cite_key(e.get("citation")) for e in out["answer"]} - {None}
        self._drop_conflicts(out, lambda c: not cited & _conflict_cites(c))
        # 2. the gaps, before any figure loses its records (review-90e00433da: a chain with
        # many gaps came back with its counts stripped of their records, dev-F1-0006 0/13)
        while not keep_gaps and _envelope_bytes(out) > cap and _merge_one_group(out):
            _mark_cut(out)
        held = {i for item in out.get("follow_ups") or ()
                if not item.get("draft") and item.get("outcome") != "refused"
                for i in item["gaps"]}
        while _envelope_bytes(out) > cap:
            cut = [(i in held, len(gap.get("detail") or ""), i) for i, gap in
                   enumerate(out["gaps"]) if not _is_cut_note(gap)
                   and len(gap.get("detail") or "") > (0 if i in held else _GAP_DETAIL_FLOOR)]
            if not cut:
                break
            gap = out["gaps"][max(cut)[2]]
            if max(cut)[0]:
                del gap["detail"]
            else:
                gap["detail"] = gap["detail"][:max(len(gap["detail"]) // 2,
                                                   _GAP_DETAIL_FLOOR) - 3] + "..."
            _mark_cut(out)
        while not keep_gaps and _envelope_bytes(out) > cap and _halve_gap_list(out):
            _mark_cut(out)
        # 3. the records and cells behind the figures, their cut stated
        inputs = sorted((e for e in out["answer"] if "records" in e or "cells" in e),
                        key=lambda e: -len(e.get("records") or e.get("cells") or ()))
        if _envelope_bytes(out) > cap and inputs:
            may_lose = {id(e) for e in inputs}
            note = _EvidenceNote(out, [e["name"] for e in out["answer"]
                                       if e.get("inputs_cut") or id(e) in may_lose],
                                 len(inputs))
            stripped = 0
            for entry in inputs:
                if _envelope_bytes(out) <= cap:
                    break
                # the entry's value rests on a record or a cell when one of them holds it
                rests = bool(entry.get("records")) or any(
                    isinstance(cell, Mapping) and cell.get("value") == entry.get("value")
                    for cell in entry.get("cells") or ())
                entry.pop("records", None)
                entry.pop("cells", None)
                if rests:
                    entry["inputs_cut"] = True
                stripped += 1
                _mark_cut(out)
            note.settle(stripped)
        # 4. the conflicts on cited records
        self._drop_conflicts(out, lambda c: True)
        return out

    def _strip_edge_citations(self, out: dict[str, Any], order: Sequence[int]) -> int:
        """The feature citations of the chain edges at ``order``, in that order, while
        ``out`` is over the page cap (the edge, its tier and features stay); how many edges
        lost theirs."""
        edges = out["chain"]["edges"]
        stripped = 0
        for index in order:
            if _envelope_bytes(out) <= self.page_cap:
                break
            edge = edges[index]
            if not _edge_cited(edge):
                continue
            edges[index] = dict(edge, **{key: [{k: v for k, v in f.items() if k != "citations"}
                                               for f in edge[key]]
                                         for key in ("reason", "why_not") if key in edge})
            stripped += 1
            _mark_cut(out)
        return stripped

    def _halve(self, out: dict[str, Any], holder: dict[str, Any], key: str) -> int:
        """``holder[key]`` halved while ``out`` is over the page cap; how many rows went."""
        before = len(holder.get(key) or ())
        while _envelope_bytes(out) > self.page_cap and holder.get(key):
            holder[key] = holder[key][: len(holder[key]) // 2]
            _mark_cut(out)
        return before - len(holder.get(key) or ())

    def _drop_conflicts(self, out: dict[str, Any], droppable: Callable[[Mapping[str, Any]], bool]
                        ) -> None:
        """Drop ``droppable`` conflicts of ``out``, last first, until it fits the page cap;
        the answer is then cut (``result_too_large``)."""
        conflicts = out.get("conflicts") or []
        candidates = [i for i in reversed(range(len(conflicts))) if droppable(conflicts[i])]
        if not candidates or _envelope_bytes(out) <= self.page_cap:
            return
        _note_conflicts_cut(out, 0)   # its own bytes are counted before any drop
        try:
            self._drop_until_fits(out, conflicts, candidates)
        finally:
            _drop_empty_cut_note(out)

    def _drop_until_fits(self, out: dict[str, Any], conflicts: list[dict[str, Any]],
                         candidates: list[int]) -> None:
        empty = _envelope_bytes({"c": []})
        while candidates:
            excess = _envelope_bytes(out) - self.page_cap
            if excess <= 0:
                return
            # as many as their own bytes cover the excess, last first; then measure again
            cut: list[int] = []
            while candidates and excess > 0:
                index = candidates.pop(0)
                cut.append(index)
                excess -= _envelope_bytes({"c": [conflicts[index]]}) - empty
            for index in sorted(cut, reverse=True):
                del conflicts[index]
            _note_conflicts_cut(out, len(cut))
            out["completeness"] = dict(
                out["completeness"], complete=False, truncated=True,
                reason=out["completeness"].get("reason") or "result_too_large")

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"]["plx.investigate"]["output"],
                            root=self.contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if _envelope_bytes(result) > self.page_cap:  # the host's labels still fit beside it
            return _too_large()
        return result
