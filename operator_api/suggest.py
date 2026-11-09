"""Search suggestions of the app API: the phrases looked up, same-record collapsing, party
and record codes, ranked suggestions and object resolution.

Moved from ``operator_api.backend`` by concern (no behaviour change); every name is
re-exported there."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import models as m
from .serving import (object_url, plain)
from .answers import (normalize, words_of)


SUGGEST_FUZZY = 0.6     # word similarity a near spelling needs ("diversifed": 0.73)
SUGGEST_ROWS = 24       # rows read per phrase
SUGGEST_WORDS = 4       # the longest trailing phrase looked up
# How a suggestion's entity type ranks within one kind of match: the parties and orders a
# person names first, places and people last.
SUGGEST_TYPE_RANK = {"customer": 0, "supplier": 0, "purchase_order": 1, "sales_order": 1,
                     "item": 2, "manufacturer": 3, "customer_po": 3, "assembly_order": 3,
                     "lot": 4, "batch": 4, "person": 5, "customer_location": 6}
SYSTEM_WORDS = {"portal": "Portal", "bc": "BC", "gen": "Sage"}
# A copy of a record to open first: the Portal's own app record, then Business Central.
OPEN_FIRST = ("portal.app_", "bc.")


def suggest_phrases(q: str) -> list[tuple[int, str]]:
    """(words, folded phrase) for each trailing phrase of ``q``, longest first: the last
    ``SUGGEST_WORDS`` words typed, then without the first, down to the last word alone;
    phrases under two characters are not looked up."""
    from operating.chunking.entities import fold
    words = fold(q).split()[-SUGGEST_WORDS:]
    out = []
    for start in range(len(words)):
        phrase = " ".join(words[start:])
        if len(phrase) >= 2:
            out.append((len(words) - start, phrase))
    return out


SAME_RECORD_ASKS = 12   # records a suggestion list asks about, at most
SAME_RECORD_SECONDS = 5   # past this, copies without a shared code stay apart


def same_records(view: Any, object_ids: Sequence[str]) -> set[frozenset[str]]:
    """Pairs of ``object_ids`` this reader reads as one record in two systems: a verified
    ``<Type>.same_record_as`` link, walked as the reader (``plx.around``: its grants and
    the link's markings). Only ids the reader was already shown are asked about."""
    ids = list(dict.fromkeys(object_ids))[:SAME_RECORD_ASKS]
    wanted, pairs = set(ids), set()
    with view._request():
        for oid in ids:
            kind = oid.split(":", 1)[0]
            for direction in ("forward", "reverse"):
                got = view._call("plx.around", {
                    "from": oid, "tier_floor": "verified", "limit": 20,
                    "path": [{"link_type": f"{kind}.same_record_as",
                              "direction": direction}]})
                for obj in got.get("objects") or ():
                    other = obj.get("object_id") if isinstance(obj, Mapping) else None
                    if other in wanted and other != oid:
                        pairs.add(frozenset((oid, other)))
    return pairs


# The identifier labels an order's entity carries for its party's code ("Supplier V08380":
# the vendor's number on the order, ``operating.chunking.headers``): the party's code,
# never the order's own (two orders of one supplier are never one record by it).
PARTY_LABELS = ("Supplier", "Customer")
ORDER_TYPES = frozenset({"purchase_order", "sales_order", "customer_po"})


def party_codes(alias: Iterable[Any]) -> set[str]:
    """The party codes among an order entity's aliases (``PARTY_LABELS``)."""
    names = {str(a) for a in alias or () if isinstance(a, str)}
    return {name.split(" ", 1)[1] for name in names if " " in name
            and name.split(" ", 1)[0] in PARTY_LABELS and name.split(" ", 1)[1] in names}


def record_codes(alias: Iterable[Any]) -> set[str]:
    """The identifiers among an entity's aliases: an alias that is also given labelled
    ("DIVE11" beside "Customer DIVE11"), as the entity index writes every identifier."""
    names = [str(a) for a in alias or () if isinstance(a, str)]
    held = set(names)
    return {name.split(" ", 1)[1] for name in names
            if " " in name and name.split(" ", 1)[1] in held
            and re.fullmatch(r"[A-Z][A-Za-z]*", name.split(" ", 1)[0])}


def system_word(object_id: str) -> str:
    binding = object_id.split(":", 2)[1] if object_id.count(":") >= 2 else ""
    family = binding.split(".", 1)[0]
    return SYSTEM_WORDS.get(family, family.capitalize() or "Record")


def suggestions(rows: Sequence[tuple], limit: int, *,
                same_record: Callable[[Sequence[str]], set[frozenset[str]]],
                members: Optional[dict[str, list[str]]] = None,
                parties: Optional[Mapping[str, str]] = None
                ) -> list[m.Suggestion]:
    """The ranked, collapsed suggestions of ``rows`` ((words of the phrase matched,
    object_id, entity type, name, aliases, exact, code, prefix, word, similarity), every
    one already within the reader's scope). Ranked: any match of the words as typed before
    a near spelling, then by the phrase's length (a whole name before one of its words),
    the match (an exact name or code, a name starting with the phrase, a word starting
    with it), the entity type
    (``SUGGEST_TYPE_RANK``), then similarity and a shorter name. Rows of one type and one
    name are one suggestion when they share a record code or the projection records them
    as the same record (``same_record``); others of that name stay apart, each told by
    its code and system. The suggestion opens the Portal's own copy first
    (``OPEN_FIRST``). ``members``, when given, gets each suggestion's copies by its id.
    An order's party code (``party_codes``) is no code of its own: it never joins copies,
    and the order says its party by name (``parties``: code -> name, as the reader may
    read the party), the code beside it ("New Directions Aromatics (V08380)")."""
    from operating.chunking.headers import undoubled
    best: dict[str, tuple] = {}
    for words, oid, kind, name, alias, exact, code, prefix, word, sim in rows:
        match = 0 if exact or code else 1 if prefix else 2 if word else 3
        rank = (match == 3, -words, match, SUGGEST_TYPE_RANK.get(str(kind), 7),
                -float(sim or 0), len(str(name or "")), str(name or ""))
        if oid not in best or rank < best[oid][0]:
            best[oid] = (rank, oid, str(kind or "object"), str(name or oid), alias,
                         "exact" if match == 0 else "prefix" if match < 3 else "fuzzy",
                         float(sim or 0))
    ordered = sorted(best.values())
    # copies of one record: same type and name, a shared code or a recorded same record
    groups: dict[tuple[str, str], list[tuple]] = {}
    for item in ordered:
        groups.setdefault((item[2], normalize(item[3])), []).append(item)
    asked = [item[1] for g in groups.values() if len(g) > 1 for item in g]
    try:
        pairs = same_record(asked) if asked else set()
    except (TimeoutError, LookupError):   # busy: copies without a shared code stay apart
        pairs = set()
    out: list[m.Suggestion] = []
    for item in ordered:
        group = groups[(item[2], normalize(item[3]))]
        if item not in group:
            continue   # already shown as a copy of an earlier one
        order = item[2] in ORDER_TYPES

        def own(alias: Any) -> set[str]:
            return record_codes(alias) - (party_codes(alias) if order else set())
        cluster = [item]
        codes = own(item[4])
        changed = True
        while changed:   # every row reachable by a shared code or a same-record link
            changed = False
            for other in group:
                if other in cluster:
                    continue
                if codes & own(other[4]) or any(
                        frozenset((other[1], c[1])) in pairs for c in cluster):
                    cluster.append(other)
                    codes |= own(other[4])
                    changed = True
        for member in cluster:
            group.remove(member)
        lead = min(cluster, key=lambda c: (next((n for n, p in enumerate(OPEN_FIRST)
                                                 if c[1].split(":", 2)[1].startswith(p)),
                                                len(OPEN_FIRST))
                                           if c[1].count(":") >= 2 else len(OPEN_FIRST),
                                           c[1]))
        object_type = lead[1].split(":", 1)[0] or "Object"
        systems = list(dict.fromkeys(system_word(c[1]) for c in cluster))
        detail = [words_of(lead[2]).capitalize()]
        party = sorted({code for c in cluster for code in party_codes(c[4])}) if order else []
        if party:
            detail.append(", ".join(f"{parties[code]} ({code})" if (parties or {}).get(code)
                                    else code for code in party[:2]))
        if codes:
            detail.append(", ".join(sorted(codes)[:2]))
        detail.append(" and ".join(systems))
        out.append(m.Suggestion(object_id=lead[1], type=object_type,
                                label=undoubled(lead[3])[:200],
                                sublabel=" · ".join(detail)[:200], match=item[5],
                                score=round(max(item[6], 1.0 if item[5] == "exact" else 0.0),
                                            3),
                                open_url=object_url(object_type, lead[1])))
        if members is not None:
            members[lead[1]] = [c[1] for c in cluster]
        if len(out) >= limit:
            break
    return out


def projection_suggest(view: Any, q: str, limit: int) -> list[m.Suggestion]:
    """``plx.find`` for the reader (its grants, its ranking); typo tolerance by retrying
    shorter prefixes of the last word and dropping one word at a time."""
    from difflib import SequenceMatcher

    needle = q.strip()
    tries = [needle]
    words = needle.split()
    if words and len(words[-1]) > 3:
        tries += [" ".join(words[:-1] + [words[-1][:n]]) for n in
                  range(len(words[-1]) - 1, 2, -1)]
    if len(words) > 1:
        tries += [" ".join(words[:i] + words[i + 1:]) for i in range(len(words))]
    out: dict[str, m.Suggestion] = {}
    with view._request():
        for attempt in tries[:6]:
            if len(out) >= limit:
                break
            result = view._call("plx.find", {"query": attempt[:200], "limit": limit})
            for cand in result.get("candidates") or ():
                obj = cand.get("object") or {}
                oid, kind = obj.get("object_id"), str(obj.get("object_type") or "Object")
                if not isinstance(oid, str) or oid in out:
                    continue
                label = plain(obj.get("display")) or oid
                ratio = SequenceMatcher(None, normalize(needle), normalize(label)).ratio()
                match = ("exact" if normalize(label) == normalize(needle) else
                         "prefix" if attempt == needle else "fuzzy")
                out[oid] = m.Suggestion(object_id=oid, type=kind, label=label[:200],
                                        sublabel=kind, match=match,
                                        score=round(max(ratio, 0.5 if match != "fuzzy"
                                                        else ratio), 3),
                                        open_url=object_url(kind, oid))
    ranked = sorted(out.values(), key=lambda s: ({"exact": 0, "prefix": 1, "fuzzy": 2}[s.match],
                                                -s.score, s.label))
    return ranked[:limit]


def sublabel_tell(s: m.Suggestion) -> Optional[str]:
    """What tells a record of a shared name apart (its code and system, "C01000 · BC"),
    without the type word the page already says ("Customer · C01000 · BC"); None when the
    sublabel says nothing else."""
    parts = [p.strip() for p in (s.sublabel or "").split("·") if p.strip()]
    kind = re.sub(r"(?<!^)(?=[A-Z])", " ", s.type).lower()
    parts = [p for p in parts if p.lower() not in {kind, s.type.lower()}]
    return " · ".join(parts) or None


def resolve_object(view: Any, object_type: str, object_id: str) -> str:
    """The object id a page asks for: given whole (``<Type>:...``), else the one record of
    that type a business reference finds for the reader; several or none: not found."""
    if ":" in object_id and (object_id.split(":", 1)[0].lower()
                             == object_type.replace("_", "").lower()
                             or re.fullmatch(r"[A-Z][A-Za-z0-9]{1,63}:[a-z][\w.]*:\{.*\}",
                                             object_id)):
        # a whole object id names its own type ("item" links once opened Product ids)
        return object_id
    result = view._call("plx.find", {"query": object_id[:200], "object_type": object_type,
                                     "limit": 3})
    if (result.get("error") or {}).get("code") == "invalid_input":
        result = view._call("plx.find", {"query": object_id[:200], "limit": 3})
    wanted = object_type.replace("_", "").lower()   # "assembly_order" is AssemblyOrder
    found = [c["object"]["object_id"] for c in result.get("candidates") or ()
             if isinstance((c.get("object") or {}).get("object_id"), str)
             and str(c["object"].get("object_type", "")).lower() == wanted]
    if len(found) != 1:
        raise KeyError("object_not_found")   # never merged: two matches are no page
    return found[0]
