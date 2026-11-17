"""Answer pages of ``plx.investigate``: selecting cells and records, citations, the
page cursor, cutting conflicts, gaps and evidence notes to fit a page, and detail
pages.

Split from ``operating/ontology_tools_investigate.py`` (module docstring there); moved
unchanged.
"""

from __future__ import annotations

import base64
import copy
from collections import Counter
import datetime as dt
import json
import re
from typing import Any, Mapping, Sequence
from integrations.source_mirror import parse_citation
from ontology.types import Actor
from . import answerers
from .ontology_tools_results import _fingerprint
from .ontology_tools_investigate_gaps import _binding_of, _error, _name_list
from .ontology_tools_investigate_plans import _Run, _canonical


def _superseded_by(view: Mapping[str, Any], moment: str | None) -> bool:
    """Whether a re-read changed-since revision (a capture version read back, or a live
    record that moved) was superseded at or before ``moment`` (None: now). A version
    superseded later was the current one at ``moment`` (an ``as_known_at`` answer cites
    it); an unreadable supersession time counts as superseded."""
    if moment is None:
        return True
    at = (view.get("capture") or {}).get("superseded_at") or view.get("superseded_at")
    try:
        return _instant(str(at)) <= _instant(moment)
    except ValueError:
        return True


def _instant(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _select_path(value: Any, parts: Sequence[str]) -> list[Any]:
    """Values at a dotted path (``*`` iterates a list; a key met on a list is read of
    each of its items, so ``lateness.lines.*`` is the lines of every root's entry)."""
    if not parts:
        return [value]
    head, rest = parts[0], parts[1:]
    if head == "*":
        if not isinstance(value, list):
            return []
        return [x for item in value for x in _select_path(item, rest)]
    if isinstance(value, list):
        return [x for item in value for x in _select_path(item, parts)]
    if isinstance(value, Mapping) and head in value:
        return _select_path(value[head], rest)
    return []


def _select_elements(value: Any, parts: Sequence[str], where: Mapping[str, Any] | None,
                     where_any: Sequence[Mapping[str, Any]] | None = None
                     ) -> list[tuple[Any, list[Any]]]:
    """(element, the values at the rest of the path in it) for each element of the path's
    first ``*`` that ``where`` (and one of ``where_any``) keeps; a path without ``*`` is one
    (None, values) pair."""
    if "*" not in parts:
        return [(None, _select_path(value, parts))]

    def carries(element: Any, keys: Mapping[str, Any]) -> bool:
        return isinstance(element, Mapping) and all(
            _canonical(element.get(k)) == _canonical(v) for k, v in keys.items())
    star = list(parts).index("*")
    return [(element, _select_path(element, parts[star + 1:]))
            for holder in _select_path(value, parts[:star]) if isinstance(holder, list)
            for element in holder if (not where or carries(element, where))
            and (not where_any or any(carries(element, keys) for keys in where_any))]


def _cells(elements: Sequence[Any], only: Sequence[str] | None = None
           ) -> list[dict[str, Any]] | None:
    """The cited values (``$defs/value``) the elements compared, one per citation; None
    unless every element carries one (a verdict is never shown with part of its inputs).
    ``only``: just the values of these properties (a plan item's ``cells_only``)."""
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for element in elements:
        values = element.get("values") if isinstance(element, Mapping) else None
        cited = [v for v in values or () if isinstance(v, Mapping)
                 and isinstance(v.get("citation"), Mapping)
                 and (only is None or v.get("property") in only)]
        if not cited:
            return None
        for value in cited:
            cells.setdefault((value["citation"]["l1_handle"], value["citation"]["field_path"]),
                             dict(value))
    return list(cells.values()) if 0 < len(cells) <= 50 else None


def _records(run: _Run, objects: Sequence[str]) -> list[dict[str, str]] | None:
    """One citation of each counted object's own record, from what the primitives showed
    this reader (a value it read on the object, else its display); None unless every object
    has one and no two share a record."""
    out: list[dict[str, str]] = []
    for oid in objects[:101]:
        cite = _citation_of(run, oid)
        if cite is None:
            return None
        out.append(cite)
    if len(out) > 100 or len({r["l1_handle"] for r in out}) != len(out):
        return None
    return out


def _evidence_properties(doc: Mapping[str, Any]) -> set[str]:
    """``Type.property`` of each property a plan's ``property`` evidence reads."""
    return {f"{item['object_type']}.{prop}" for item in doc.get("evidence") or ()
            if item.get("kind") == "property" for prop in item.get("properties") or ()}


def _cell_of(declared: Mapping[str, Mapping[str, Any]], binding: str,
             cite: Mapping[str, Any] | None, path: str) -> dict[str, str] | None:
    """The citation of ``path`` in the record ``cite`` cites (``_citation_of``: the object's
    own record): its field prefix is the cited field less the longest source path of
    ``binding`` (any declared property's) it ends with; None when none does."""
    if not cite or not isinstance(cite.get("field_path"), str):
        return None
    cited = cite["field_path"]
    own = sorted({src["path"] for spec in declared.values()
                  for src in spec.get("sources") or ()
                  if isinstance(src, Mapping) and src.get("binding") == binding
                  and isinstance(src.get("path"), str)
                  and cited.endswith("." + src["path"])}, key=len)
    if not own:
        return None
    return {"l1_handle": cite["l1_handle"],
            "field_path": f"{cited[:-len(own[-1]) - 1]}.{path}"}


def _citation_of(run: _Run, oid: str) -> dict[str, str] | None:
    """A citation of ``oid``'s own record a primitive showed this reader: a value of it the
    plan read, else its display, else a value of it read for a count; None without one."""
    cite = next((v["citation"] for v in run.values.get(oid, ())
                 if isinstance(v.get("citation"), Mapping)), None)
    if cite is None:
        display = (run.objects.get(oid) or {}).get("display")
        origin = display.get("origin") if isinstance(display, Mapping) else None
        if isinstance(origin, Mapping):
            cite = {"l1_handle": origin.get("citation"), "field_path": origin.get("field_path")}
    cite = cite or run.record_cites.get(oid) or _edge_citation(run, oid) or {}
    if isinstance(cite.get("l1_handle"), str) and isinstance(cite.get("field_path"), str):
        return {"l1_handle": cite["l1_handle"], "field_path": cite["field_path"]}
    return None


def _edge_citation(run: _Run, oid: str) -> dict[str, str] | None:
    """A citation of ``oid``'s own Portal row among the feature citations of an edge the
    reader was shown that ends at ``oid`` (a line with no value or display of its own is
    still cited by the reference that links it): the row of its binding's relation under
    exactly its own key, else None. Only rows the reader's edge trimming kept are seen."""
    relation = run.relations.get(_binding_of(oid))
    key = _own_key(oid)
    if relation is None or key is None:
        return None
    for edge in run.edges.values():
        if oid not in (edge.get("from"), edge.get("to")):
            continue
        for feature in edge.get("reason") or ():
            for cite in (feature.get("citations") or ()) if isinstance(feature, Mapping) else ():
                if isinstance(cite, Mapping) and isinstance(cite.get("field_path"), str) \
                        and _api_row(cite.get("l1_handle")) == (relation, key):
                    return {"l1_handle": cite["l1_handle"], "field_path": cite["field_path"]}
    return None


def _plain_key(raw: Any) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(raw, Mapping) or not raw:
        return None
    return tuple(sorted((str(k), str(v)) for k, v in raw.items()))


def _own_key(oid: str) -> tuple[tuple[str, str], ...] | None:
    parts = oid.split(":", 2)
    try:
        return _plain_key(json.loads(parts[2])) if len(parts) == 3 else None
    except ValueError:
        return None


def _api_row(handle: Any) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """(relation, key) of an ``api://`` row handle, parsed by the mirror's one strict
    parser (``integrations.source_mirror.parse_citation``); None for any other handle."""
    try:
        relation, _, key, _ = parse_citation(handle)
    except ValueError:
        return None
    return relation, _plain_key(key)


def _beside(out: Mapping[str, Any], base: Mapping[str, Any],
            entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The conflicts of a page of ``base`` with ``entries`` added: its own and those the
    entries cite (``_page_conflicts``), each once, in the answer's order."""
    held = {id(c) for c in base["conflicts"]} | {id(c) for c in _page_conflicts(
        out, entries, False)}
    return [c for c in out.get("conflicts") or () if id(c) in held]


def _later_page(out: dict[str, Any]) -> None:
    """A page after the first: the gaps, the plan trace and the follow-ups are listed whole
    on the first page only (``answer_page.gaps_on_page``), so the later pages hold the rest
    of the rows rather than the same notes again."""
    out["gaps"] = []
    out.pop("trace", None)
    out.pop("follow_ups", None)


def _page_conflicts(out: Mapping[str, Any], entries: Sequence[Mapping[str, Any]],
                    first: bool) -> list[dict[str, Any]]:
    """The conflicts of ``out`` a page of ``entries`` lists, in their order: those on a
    record one of its entries cites (a conflict travels with the values it questions); the
    first page also every conflict its declared precedence did not resolve (no trusted
    value: it qualifies the whole answer, so a reader of page 1 alone sees it) and those on
    no record an entry of the whole answer (``out["answer"]``) cites."""
    on = {_cite_key(e.get("citation")) for e in entries} - {None}
    anywhere = {_cite_key(e.get("citation")) for e in out["answer"]} - {None}
    return [c for c in out.get("conflicts") or ()
            if on & _conflict_cites(c) or first and (
                not (c.get("resolution") or {}).get("trusted")
                or not anywhere & _conflict_cites(c))]


def _later_base(out: Mapping[str, Any], shared: list[dict[str, Any]]) -> dict[str, Any]:
    """What every page after the first holds whatever its units (``_later_page``)."""
    base = dict(_page_base(out, shared), conflicts=_page_conflicts(out, shared, False))
    _later_page(base)
    base["answer_page"] = dict(base["answer_page"], gaps_on_page=1)
    return base


def _page_base(out: Mapping[str, Any], shared: list[dict[str, Any]]) -> dict[str, Any]:
    """What every page of ``out`` holds whatever its units: the shared entries, the gaps,
    conflicts and the rest, with page numbers and a cursor as wide as any real one (chain
    and values, which ``_fit`` trims, left out)."""
    widest = 10 ** 6
    return dict(out, answer=list(shared), values=[], chain={"objects": [], "edges": []},
                conflicts=_page_conflicts(out, shared, True),
                answer_page=dict.fromkeys(("page", "pages", "returned", "total"), widest),
                completeness=dict(out["completeness"], complete=False, truncated=True,
                                  reason=out["completeness"].get("reason") or "answer_paged",
                                  next_cursor=_write_cursor(widest, widest, "0" * 16, "0" * 16,
                                                            (widest, "0" * 16))))


CONFLICTS_CUT = "conflicts_truncated"
GAPS_CUT = "gaps_truncated"
MIN_GAPS_KEPT = 10  # gaps a page always lists whole (beyond them, the gaps_truncated note)


def _gaps_cut_note(dropped: Counter[str]) -> dict[str, Any]:
    """The gap that stands for the gaps no page has room for: how many, by reason."""
    reasons = ", ".join(f"{reason} x{count}" for reason, count in dropped.most_common())
    return {"state": "unknown", "reason": GAPS_CUT, "subject": "gaps",
            "detail": (f"{sum(dropped.values())} further gap(s) are not listed, so the answer "
                       f"fits one response: {reasons}")[:600]}
_CUT_DETAIL = re.compile(r"^([0-9]+) ")


def _mark_cut(out: dict[str, Any]) -> None:
    """``out`` was cut to fit one response (``result_too_large`` unless already cut)."""
    out["completeness"] = dict(
        out["completeness"], complete=False, truncated=True,
        reason=out["completeness"].get("reason") or "result_too_large")


def _edge_cited(edge: Mapping[str, Any]) -> bool:
    return any("citations" in f for f in [*edge.get("reason", ()), *edge.get("why_not", ())]
               if isinstance(f, Mapping))


def _is_cut_note(gap: Mapping[str, Any]) -> bool:
    return str(gap.get("reason") or "").endswith("_truncated")


_CUT_COUNT = re.compile(r"([a-z][a-z0-9_]*) x([0-9]+)")


def _fold_gaps(out: dict[str, Any], indices: Sequence[int]) -> None:
    """The gaps at ``indices`` (all past every gap a follow-up holds, so no index a
    follow-up names moves) counted by reason into the one ``gaps_truncated`` gap, with
    those it already counts; the note keeps its place, else goes last."""
    gaps = out["gaps"]
    note = next((g for g in gaps if g.get("reason") == GAPS_CUT), None)
    counts: Counter[str] = Counter()
    for reason, count in _CUT_COUNT.findall((note or {}).get("detail") or ""):
        counts[reason] += int(count)
    drop = set(indices)
    for index in indices:
        gap = gaps[index]
        counts[str(gap.get("reason"))] += gap["count"] if isinstance(
            gap.get("count"), int) and gap["count"] > 1 else 1
    # who can answer them stays with the note, so a recorded answer still routes them
    who = list(dict.fromkeys(w for g in [*(gaps[i] for i in indices), note or {}]
                             for w in g.get("who_can_answer") or ()))
    made = dict(_gaps_cut_note(counts), **({"who_can_answer": who[:10]} if who else {}))
    out["gaps"] = [g for i, g in enumerate(gaps) if i not in drop]
    if note is None:
        out["gaps"].append(made)
    else:
        note.clear()
        note.update(made)


def _held_last(out: Mapping[str, Any]) -> int:
    """The last gap index any follow-up names (-1: none)."""
    return max((i for item in out.get("follow_ups") or () for i in item.get("gaps") or ()),
               default=-1)


def _halve_gap_list(out: dict[str, Any]) -> bool:
    """The gaps after the last a follow-up holds halved, never below ``MIN_GAPS_KEPT``
    gaps nor a cut-list note, the dropped ones counted in ``gaps_truncated``
    (``_fold_gaps``). Whether any was dropped."""
    gaps = out.get("gaps") or []
    ordinary = [i for i, g in enumerate(gaps) if not _is_cut_note(g)]
    droppable = [i for i in ordinary if i > _held_last(out)]
    count = min(len(ordinary) - max(len(ordinary) // 2, MIN_GAPS_KEPT), len(droppable))
    if count <= 0:
        return False
    _fold_gaps(out, droppable[-count:])
    return True


EVIDENCE_CUT = "evidence_truncated"
_EVIDENCE_COUNT = re.compile(r"cut from ([0-9]+) answer entr")


def _evidence_detail(unsupported: Sequence[str], entries: int) -> str:
    head = (f"{len(unsupported)} figure(s) are shown without the records or cells they rest "
            f"on and are unsupported here (never state them as whole): "
            f"{_name_list(list(unsupported), 200)}" if unsupported
            else "every figure keeps its own citation")
    return (f"{head}; records or cells cut from {entries} answer entr"
            f"{'y' if entries == 1 else 'ies'} so the answer fits one response; ask again "
            "narrower (a smaller scope or limit) or read the cited records "
            "(plx.object)")[:600]


class _EvidenceNote:
    """The one ``evidence_truncated`` gap of a result whose entries' records or cells are
    about to be cut (no other page lists them): made (or found) before any cut, at the size
    its widest wording could take (every figure that may lose its inputs, every entry cut),
    so the bytes it adds are counted before the cuts that pay for them; ``settle`` then
    names the figures left without their records or cells (``inputs_cut``: unsupported)
    and how many entries lost theirs. A new note that names nothing goes."""

    def __init__(self, out: dict[str, Any], may_lose: Sequence[str], may_cut: int) -> None:
        self.out = out
        gaps = out.setdefault("gaps", [])
        self.note = next((g for g in gaps if g.get("reason") == EVIDENCE_CUT), None)
        self.created = self.note is None
        found = _EVIDENCE_COUNT.search((self.note or {}).get("detail") or "")
        self.before = int(found[1]) if found else 0
        if self.note is None:
            if len(gaps) >= 50:   # the contract's bound: gaps folded make room
                droppable = [i for i, g in enumerate(gaps)
                             if i > _held_last(out) and not _is_cut_note(g)]
                need = 1 if any(g.get("reason") == GAPS_CUT for g in gaps) else 2
                if len(droppable) < need:
                    return
                _fold_gaps(out, droppable[-need:])
            # the host cut it, not a source: the Workbench operator answers it (``_answerers``)
            self.note = {"state": "unknown", "reason": EVIDENCE_CUT, "subject": "evidence",
                         "who_can_answer": [answerers.typed("Workbench operator")]}
            out["gaps"].append(self.note)
        widest = _evidence_detail(may_lose, self.before + may_cut)
        self.room = min(len(widest) + 40, 600)
        self.note["detail"] = widest.ljust(self.room)

    def settle(self, cut: int) -> None:
        if self.note is None:
            return
        unsupported = [e["name"] for e in self.out.get("answer") or () if e.get("inputs_cut")]
        if self.created and not unsupported and not cut:
            self.out["gaps"] = [g for g in self.out["gaps"] if g is not self.note]
            return
        self.note["detail"] = _evidence_detail(unsupported, self.before + cut)[
            :self.room].rstrip()


def _note_conflicts_cut(out: dict[str, Any], dropped: int) -> None:
    """Add ``dropped`` to the answer's ``conflicts_truncated`` gap (made at 0 first, so its
    bytes are counted before a drop): how many conflicts it does not show and why. A gap
    list already at its contract bound (50) gets none; ``completeness`` still says cut."""
    gaps = out.setdefault("gaps", [])
    gap = next((g for g in gaps if g.get("reason") == CONFLICTS_CUT), None)
    if gap is None:
        if len(gaps) >= 50:
            return
        gap = {"state": "unknown", "reason": CONFLICTS_CUT, "subject": "conflicts"}
        gaps.append(gap)
    found = _CUT_DETAIL.match(gap.get("detail") or "")
    total = (int(found[1]) if found else 0) + dropped
    gap["detail"] = (f"{total} conflict(s) between linked records are not shown: none of "
                     "them is on a record an answer entry cites, and with them the answer "
                     "does not fit one response; read those records (plx.object) to see "
                     "them")[:600]


def _drop_empty_cut_note(out: dict[str, Any]) -> None:
    """Remove a ``conflicts_truncated`` gap that counts no drop (the answer fit without)."""
    out["gaps"] = [g for g in out.get("gaps") or () if not (
        g.get("reason") == CONFLICTS_CUT and (g.get("detail") or "").startswith("0 "))]


def _ref_handle(ref: Any) -> str | None:
    """The version handle an object reference of the chain shows (its display's origin)."""
    display = ref.get("display") if isinstance(ref, Mapping) else None
    origin = display.get("origin") if isinstance(display, Mapping) else None
    citation = origin.get("citation") if isinstance(origin, Mapping) else None
    return citation if isinstance(citation, str) else None


def _cite_key(cite: Any) -> tuple[str, str] | None:
    if isinstance(cite, Mapping) and isinstance(cite.get("l1_handle"), str) \
            and isinstance(cite.get("field_path"), str):
        return cite["l1_handle"], cite["field_path"]
    return None


def _conflict_cites(conflict: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The (record, field) of each value a conflict compares."""
    return {key for value in conflict.get("values") or () if isinstance(value, Mapping)
            for key in (_cite_key(value.get("citation")),) if key is not None}


def _conflict_held(conflict: Mapping[str, Any]) -> tuple[Any, frozenset] | None:
    """What a conflict compares: its kind, subject and other fields, and the set of its
    values' (record, field) or its events; None when a value carries no citation (then it
    is never merged)."""
    values = [v for v in conflict.get("values") or ()]
    keys = {_cite_key(v.get("citation")) if isinstance(v, Mapping) else None for v in values}
    if None in keys:
        return None
    rest = _canonical({k: v for k, v in conflict.items() if k not in ("values", "events")})
    return rest, frozenset(keys) | frozenset(("event", str(e)) for e in
                                             conflict.get("events") or ())


def _add_conflict(conflicts: list[dict[str, Any]], conflict: Mapping[str, Any]) -> None:
    """Add ``conflict`` once: one whose compared records (``_conflict_held``) another of the
    same kind, subject and note already holds adds nothing; one holding more replaces every
    such one, at the first's place. Several plan items read one property of an order's
    twins, each naming the same disagreement over its own subset or order of the records."""
    held = _conflict_held(conflict)
    if held is None:
        if conflict not in conflicts:
            conflicts.append(dict(conflict))
        return
    rest, keys = held
    place = None
    for index in reversed(range(len(conflicts))):
        other = _conflict_held(conflicts[index])
        if other is None or other[0] != rest:
            continue
        if keys <= other[1]:
            return
        if other[1] < keys:
            del conflicts[index]
            place = index
    conflicts.insert(len(conflicts) if place is None else place, dict(conflict))


def _too_large() -> dict[str, Any]:
    return _error("result_too_large", "the answer does not fit one response",
                  "ask a narrower question about this root (a smaller family); cutting "
                  "the step budget gives a partial answer, never a complete one")


def _answer_units(answer: Sequence[dict[str, Any]]
                  ) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """(shared entries, units): a unit is a computed entry none of whose inputs is computed,
    with the inputs no other such entry uses; beside such units, an entry no computed entry
    names as an input (an item's empty cell no line read) is a unit of its own; everything
    else is shared (all of an answer with no computed entry, which is never paged)."""
    computed = {e["name"] for e in answer if e["kind"] == "computed"}
    leaves = [e for e in answer if e["kind"] == "computed"
              and not computed.intersection(e.get("inputs") or ())]
    users = Counter(name for leaf in leaves for name in set(leaf.get("inputs") or ()))
    named = {name for e in answer if e["kind"] == "computed" for name in e.get("inputs") or ()}
    by_name = {e["name"]: e for e in answer}
    units = [[leaf] + [by_name[name] for name in dict.fromkeys(leaf.get("inputs") or ())
                       if users[name] == 1 and name in by_name and name not in computed]
             for leaf in leaves]
    if leaves:
        units += [[e] for e in answer if e["kind"] != "computed" and e["name"] not in named]
    grouped = {id(e) for unit in units for e in unit}
    return [e for e in answer if id(e) not in grouped], units


def _question(arguments: Mapping[str, Any], actor: Actor) -> str:
    """The cursor's fingerprint: the question and reader, not the follow_ups option (the
    first page records them; the next pages are asked without it) nor the response_format
    (how the MCP server words its text, never the answer)."""
    return _fingerprint({k: v for k, v in arguments.items()
                         if k not in ("follow_ups", "response_format")}, actor)


def _write_cursor(page: int, start: int, digest: str, fingerprint: str,
                  detail: tuple[int, str] | None = None) -> str:
    body: dict[str, Any] = {"p": page, "s": start, "d": digest, "f": fingerprint}
    if detail is not None:
        body["x"], body["y"] = detail
    raw = json.dumps(body, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _read_cursor(cursor: str, fingerprint: str
                 ) -> tuple[int, int, str, tuple[int, str] | None] | None:
    """(page, first entry position, answer digest, detail) of a cursor issued for this
    question and reader (positions in the answer's units laid end to end, ``_bounds``);
    ``detail``: (detail page index, the details' digest) on a detail page's cursor
    (``_detail_page``), else None."""
    try:
        body = json.loads(base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4))
                                                   .encode()).decode())
        page, start, digest = int(body["p"]), int(body["s"]), str(body["d"])
        detail = None
        if "x" in body:
            detail = (int(body["x"]), str(body["y"]))
            if detail[0] < 0 or start < 0:
                return None
        elif start < 1:
            return None
        if body["f"] != fingerprint or page < 2:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return page, start, digest, detail


# The lists a detail page holds (``_detail_page``), in order, with their contract bounds.
DETAIL_LISTS = {"objects": 100, "edges": 200, "values": 400, "conflicts": 50}


def _detail_of(out: Mapping[str, Any]) -> dict[str, list[Any]]:
    """The whole answer's chain objects, edges, values and conflicts, before any page
    trims them (copies: paging trims ``out`` in place)."""
    return copy.deepcopy({"objects": list(out["chain"]["objects"]),
                          "edges": list(out["chain"]["edges"]),
                          "values": list(out.get("values") or ()),
                          "conflicts": list(out.get("conflicts") or ())})


def _detail_items(detail: Mapping[str, list[Any]]) -> list[tuple[str, Any]]:
    return [(key, item) for key in DETAIL_LISTS for item in detail.get(key) or ()]


def _detail_lost(first: Mapping[str, Any], detail: Mapping[str, list[Any]],
                 answer: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the answer's first page (as built) trims or leaves out a chain object or
    edge, or a value or conflict the page is meant to hold (all of them on a page of the
    whole answer; on the first of several, the values its entries cite and the conflicts
    ``_page_conflicts`` gives it, the rest travelling with their entries): then detail
    pages list them all."""
    if "error" in first:
        return False
    chain = first.get("chain") or {}
    if _canonical(chain.get("objects") or []) != _canonical(detail["objects"]) \
            or _canonical(chain.get("edges") or []) != _canonical(detail["edges"]):
        return True
    values, conflicts = detail["values"], detail["conflicts"]
    if "answer_page" in first:
        entries = first.get("answer") or []
        cited = {_cite_key(e.get("citation")) for e in entries} - {None}
        values = [v for v in values if _cite_key(v.get("citation")) in cited]
        conflicts = _page_conflicts({"answer": answer, "conflicts": conflicts}, entries, True)
    return (_canonical(first.get("values") or []) != _canonical(values)
            or _canonical(first.get("conflicts") or []) != _canonical(conflicts))


def _conflicts_on_detail_pages(out: dict[str, Any]) -> None:
    """The ``conflicts_truncated`` gap of an answer with detail pages: the conflicts its
    first page has no room for are listed there, so it says where (its count stays)."""
    for gap in out.get("gaps") or ():
        found = _CUT_DETAIL.match(gap.get("detail") or "") \
            if gap.get("reason") == CONFLICTS_CUT else None
        if found:
            # no longer than the text it replaces: the page was fitted with that one
            gap["detail"] = (f"{found[1]} conflict(s) between linked records are not "
                             "shown here: none is on a record an answer entry cites, and "
                             "with them the answer does not fit one response; the detail "
                             "pages after the answer list them")[:600]


def _detail_base(out: Mapping[str, Any], numbers: tuple[int, int, int], digest: str,
                 fingerprint: str) -> dict[str, Any]:
    """What a detail page holds besides its items: the answer's head (procedure,
    answerability, root, steps, meta, claim checks) with no entries and no gaps
    (``gaps_on_page``: the first page lists them), page ``numbers`` (page, pages, the
    answer's entries) and a cursor as wide as any real one."""
    page, pages, total = numbers
    base = dict(out, answer=[], values=[], chain={"objects": [], "edges": []}, conflicts=[])
    _later_page(base)
    base["answer_page"] = {"page": page, "pages": pages, "returned": 0, "total": total,
                           "gaps_on_page": 1}
    base["completeness"] = dict(
        out["completeness"], complete=False, truncated=True,
        reason=out["completeness"].get("reason") or "answer_paged",
        next_cursor=_write_cursor(10 ** 6, 10 ** 6, digest, fingerprint,
                                  (10 ** 6, "0" * 16)))
    return base


def _merge_withheld(current: Mapping[str, Any] | None, new: Mapping[str, Any]
                    ) -> dict[str, Any]:
    if current is None:
        return {k: new.get(k) for k in ("properties", "objects", "reason")}
    out: dict[str, Any] = {}
    for key in ("properties", "objects"):
        a, b = current.get(key), new.get(key)
        out[key] = None if a is None or b is None else a + b
    reasons = {current.get("reason"), new.get("reason")}
    out["reason"] = (current["reason"] if len(reasons) == 1 else "marking_and_audience")
    return out
