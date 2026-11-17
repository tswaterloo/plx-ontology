"""Gap entries of ``plx.investigate`` answers: building a gap, merging twin gaps
(searched names, answerers) and the small value helpers the steps share.

Split from ``operating/ontology_tools_investigate.py`` (module docstring there); moved
unchanged.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# =========================================================================== run state

MAX_GAP_TWINS = 20  # the contract's gap ``twins`` max_items


def _gap(state: str, reason: str, subject: str, detail: str | None = None,
         concerns: str | None = None) -> dict[str, Any]:
    gap = {"state": state, "reason": reason[:64], "subject": subject[:700]}
    if detail:
        gap["detail"] = detail[:600]
    if concerns:
        gap["concerns"] = concerns[:64]
    return gap


MAX_MERGED_NAMES = 12   # subjects (or link types) a merged gap names before "and K more"
_SEARCHED_CAPS = {"sources": 8, "scopes": 16, "queries": 16}   # the contract's gap.searched


def _name_list(names: Sequence[str], limit: int, sep: str = ", ",
               most: int = MAX_MERGED_NAMES) -> str:
    """``names`` in order, as many as fit ``limit`` characters (at most ``most``), the rest
    counted: ``a, b and 3 more``; at least the first name (cut to fit)."""
    shown: list[str] = []
    for index, name in enumerate(names):
        rest = len(names) - index - 1
        tail = f" and {rest} more" if rest else ""
        if index >= most or shown and \
                len(sep.join([*shown, name]) + tail) > limit:
            break
        shown.append(name)
    more = len(names) - len(shown)
    tail = f" and {more} more" if more else ""
    text = sep.join(shown)
    return (text[:max(limit - len(tail), 1)] + tail) if names else ""


def _searched_union(gaps: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any] | None, int]:
    """What ``gaps`` searched, as one ``searched``: every source, scope and query in their
    first order (each list at its contract bound), the earliest ``since`` and latest
    ``until`` only when every gap states one. With how many sources the bound left out (a
    merged gap says so in its detail). None when no gap names a source."""
    have = [g["searched"] for g in gaps if isinstance(g.get("searched"), Mapping)]
    joined = {key: list(dict.fromkeys(v for h in have for v in h.get(key) or ()
                                      if isinstance(v, str) and v))
              for key in _SEARCHED_CAPS}
    if not joined["sources"]:
        return None, 0
    out: dict[str, Any] = {key: values[:_SEARCHED_CAPS[key]]
                           for key, values in joined.items() if values}
    if len(have) == len(gaps):
        for key, pick in (("since", min), ("until", max)):
            values = [h.get(key) for h in have]
            if all(isinstance(v, str) and v for v in values):
                out[key] = pick(values)
    return out, max(len(joined["sources"]) - _SEARCHED_CAPS["sources"], 0)


def _merged_gap(gaps: Sequence[Mapping[str, Any]], state: str, reason: str, subject: str,
                detail: str) -> dict[str, Any]:
    """One gap standing for ``gaps`` (all of ``state``): how many (``count``, a merged gap's
    own counts summed), every source they searched (``searched``) and everyone who can
    answer them (``who_can_answer``), so a follow-up on it routes to the owner of what was
    searched, not of the root records; their ``basis`` when they share one."""
    searched, more = _searched_union(gaps)
    if more:
        detail += f"; searched {more} more source(s) not listed"
    gap = _gap(state, reason, subject, detail)
    count = sum(g["count"] if isinstance(g.get("count"), int) and g["count"] > 1 else 1
                for g in gaps)
    if count > 1:
        gap["count"] = count
    if searched:
        gap["searched"] = searched
    who = list(dict.fromkeys(w for g in gaps for w in g.get("who_can_answer") or ()))
    if who:
        gap["who_can_answer"] = who[:10]
    bases = {g.get("basis") for g in gaps}
    if len(bases) == 1 and isinstance(next(iter(bases)), str):
        gap["basis"] = next(iter(bases))
    return gap


def _mergeable(gap: Mapping[str, Any]) -> bool:
    """Whether ``_fit`` may merge ``gap`` with others of its state and reason: never a
    note of a cut list (``<list>_truncated``, stated on its own), a gap tied to an answer
    entry (``concerns``), one carrying the root's twins or source text as data."""
    return not (str(gap.get("reason") or "").endswith("_truncated")
                or any(key in gap for key in ("concerns", "twins", "source_text")))


def _replace_gaps(out: dict[str, Any], members: Sequence[int], merged: dict[str, Any]
                  ) -> None:
    """``out``'s gaps with those at ``members`` (none a follow-up holds) as ``merged``, at
    the first one's place; every follow-up's gap indices moved with them."""
    drop = set(members[1:])
    gaps: list[dict[str, Any]] = []
    where: dict[int, int] = {}
    for index, gap in enumerate(out.get("gaps") or ()):
        if index in drop:
            continue
        where[index] = len(gaps)
        gaps.append(merged if index == members[0] else gap)
    out["gaps"] = gaps
    if out.get("follow_ups"):
        out["follow_ups"] = [dict(item, gaps=[
            where[i] if i in where else i - sum(1 for d in drop if d < i)
            for i in item.get("gaps") or ()]) for item in out["follow_ups"]]


def _merge_one_group(out: dict[str, Any]) -> bool:
    """Merge the largest group (two or more) of ``out``'s gaps that share a state and a
    reason, none a follow-up holds (``_mergeable``), into one (``_merged_gap``): its
    subject names theirs, its detail theirs when they all say the same. Whether one was."""
    gaps = out.get("gaps") or []
    held = {i for item in out.get("follow_ups") or () for i in item.get("gaps") or ()}
    groups: dict[tuple[str, str], list[int]] = {}
    for index, gap in enumerate(gaps):
        if index not in held and _mergeable(gap):
            groups.setdefault((str(gap.get("state")), str(gap.get("reason"))), []).append(index)
    members = max(groups.values(), key=len, default=[])
    if len(members) < 2:
        return False
    found = [gaps[i] for i in members]
    state, reason = str(found[0]["state"]), str(found[0]["reason"])
    count = sum(g["count"] if isinstance(g.get("count"), int) and g["count"] > 1 else 1
                for g in found)
    details = {g.get("detail") for g in found}
    said = next(iter(details)) if len(details) == 1 and next(iter(details)) else ""
    subject = _name_list(list(dict.fromkeys(str(g["subject"]) for g in found)), 700, "; ")
    _replace_gaps(out, members, _merged_gap(
        found, state, reason, subject,
        f"{count} gaps of this state and reason, merged into one so the answer keeps its "
        "cited records within one response" + (f": {said}" if said else
                                              "; their details are not shown")))
    return True


def _element_gaps(elements: Sequence[tuple[Any, list[Any]]], concerns: str,
                  limit: int = 4) -> list[dict[str, Any]]:
    """The gaps a primitive put on the elements whose verdict it left undecided (an
    element's own ``gap`` and ``gaps``), restated as concerning ``concerns``: the reason a
    value is null travels with it, never "see its gaps" with nothing to see. Only
    well-formed gaps, at most ``limit``, in the primitive's order."""
    out: list[dict[str, Any]] = []
    for element, picked in elements:
        if None not in picked or not isinstance(element, Mapping):
            continue
        own = element.get("gaps")
        for gap in [element.get("gap"), *(own if isinstance(own, list) else ())]:
            if not isinstance(gap, Mapping) or not all(
                    isinstance(gap.get(k), str) and gap.get(k)
                    for k in ("state", "reason", "subject")):
                continue
            detail = gap.get("detail")
            made = _gap(gap["state"], gap["reason"], gap["subject"],
                        detail if isinstance(detail, str) else None, concerns)
            if made not in out:
                out.append(made)
            if len(out) >= limit:
                return out
    return out


def _citation(candidate: Mapping[str, Any]) -> str | None:
    """The citation a candidate's match carries, if any."""
    origin = ((candidate.get("matched") or {}).get("raw") or {}).get("origin") or {}
    citation = origin.get("citation")
    return citation if isinstance(citation, str) and citation else None


def _twin_entry(candidate: Mapping[str, Any], tier: str) -> dict[str, Any]:
    """A ``root_twins_not_roots`` gap's ``twins`` entry (the gap contract)."""
    entry = {"object_id": candidate["object"]["object_id"], "tier": tier}
    citation = _citation(candidate)
    if citation:
        entry["handle"] = citation
    return entry


def _listed(candidates: Sequence[Mapping[str, Any]], room: int = 500) -> str:
    """Candidates named in a gap by the citation their match carries (else their object
    id; the reader saw each one in plx.find), as many as fit in ``room`` characters, then
    a count."""
    named: list[str] = []
    for candidate in candidates:
        entry = _citation(candidate) or candidate["object"]["object_id"]
        if named and sum(len(n) + 2 for n in named) + len(entry) > room:
            break
        named.append(entry)
    left = len(candidates) - len(named)
    return "; ".join(named) + (f"; and {left} more" if left else "")


def _error(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    error = {"code": code, "message": message[:600]}
    if hint:
        error["hint"] = hint[:600]
    return {"error": error}


def _as_corrected(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A property entry a person corrected as an engine taking reviewed inputs reads it:
    the person's value, unit and currency (only those the correction states) in place of
    the source's, its ``review`` (the correction and its citations) kept; the source
    citation stays so the engine can name what was corrected, never re-read it as the
    value's. A money correction states its currency and may leave out the unit: a unit
    price then stays per the source's denominator."""
    review = entry["review"]
    out = {k: v for k, v in entry.items() if k not in ("unit", "currency")}
    out.update({k: review[k] for k in ("value", "unit", "currency") if k in review})
    if entry.get("value_type") == "money" and "unit" not in review and "unit" in entry:
        out["unit"] = entry["unit"]   # an amount corrected in its currency: per the same unit
    return out


def _corrected_under(entry: Mapping[str, Any], corrected: Mapping[str, Any],
                     computed: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """The corrected inputs a computed entry rests on: those it names and those of every
    computed entry (``computed``, by name) it names, such as a total over lines computed
    from a corrected value, so the total cites the correction as its lines do."""
    found: set[str] = set()
    seen: set[str] = {entry["name"]}
    todo = list(entry.get("inputs") or ())
    while todo:
        name = todo.pop()
        if name in corrected:
            found.add(name)
        elif name not in seen and name in computed:
            seen.add(name)
            todo.extend(computed[name].get("inputs") or ())
    return found


def _type_of(object_id: str) -> str:
    return object_id.split(":", 1)[0]


def _binding_of(object_id: str) -> str:
    parts = object_id.split(":", 2)
    return parts[1] if len(parts) > 1 else ""


def _value_text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        text = value.get("untrusted_text")
        return text if isinstance(text, str) else None
    if value is None or isinstance(value, bool):
        return None
    return str(value)


def _empty_value(value: Any) -> bool:
    """An ``omit_empty`` item's empty value: none, or text (a trimmed source cell, untrusted
    or not) holding nothing, e.g. FileMaker's blank ``tariff_c``."""
    if isinstance(value, Mapping):
        value = value.get("untrusted_text", value)
    return value is None or (isinstance(value, str) and not value.strip())
