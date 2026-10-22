"""Employee wording for the console's case and home views (presentation only).

A recorded gap names what is missing in the engine's terms (a plan step id such as
``po_twin_of``, a link ``Receipt.receives reverse``, a property ``PurchaseOrder.currency``)
and why in a reason code (``plx.investigate:no_match: ...``). ``plain_gap`` words both
for an employee from the shared schema, the plan files and the process graphs' stage
owners; the recorded code stays on the gap, untouched, for an engineer to read. Unknown
input never reaches the plain words: a title it cannot word is a generic line, a reason
it does not know is "not established", so no engine code leaks and nothing is claimed.

``case_summary`` names a case's business object from the rows the reader resolved
itself (``CaseWorkspace._records``): the root record, its counterparty, status and the
cited lines. It reads no other source and decides nothing; a root the reader cannot
resolve is named by its id only.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from operating.explanations import TIER_MEANING, field_words as words, reason_words, type_words

PLAN_DIR = Path(__file__).resolve().parents[1] / "ontology" / "plans"
GENERIC_ANSWERER = "the owner of the source record"
NO_TITLE = "A gap recorded on this case"
NOT_ESTABLISHED = "Not established from the records you may read."
NO_ANSWERER = "Not named in the records: whoever keeps the source record"
PARTY_TYPES = ("Supplier", "Customer")
_ABBREVIATIONS = {"po": "purchase order", "so": "sales order", "ao": "assembly order",
                  "pi": "purchase invoice", "fg": "FG item"}
_DATE_WORDS = {"manuf": "manufacture", "req": "required", "po": "PO", "ship": "ship",
               "inv": "invoice", "due": "due", "rec": "received", "ord": "order"}
_SIDES = {"ordered": "Ordered quantity", "received": "Received quantity",
          "invoiced": "Invoiced quantity", "produced": "Produced quantity",
          "shipped": "Shipped quantity", "decisions": "Past decisions about this record"}
_COST_PARTS = {"goods": "Goods", "freight": "Freight", "duty": "Duty",
               "receipt_actual": "Actual receipt"}
_OUTCOMES = {"partial": "partly answered", "truncated": "only part of the answer is shown",
             "blocked": "not answered"}
_SHORT = {"gap_not_ingested": "a source that is not loaded",
          "gap_inaccessible": "a source you may not read", "gap_stale": "an out-of-date source",
          "gap_unknown": "a value not established", "missing_decisions": "a recorded decision",
          "no_evidence_found": "any evidence", "unpriced_line": "a unit price on every line",
          "po_lines_incomplete": "complete purchase order lines",
          "receipt_actual_not_read": "the actual receipt costs"}
_CODE = re.compile(r"(?:plx\.[a-z_]+:)?([a-z][a-z0-9_]*)(?::\s*(.*))?", re.S)
_HOP = re.compile(r"([a-z][a-z0-9_]*) ([A-Z]\w*):([\w.]+):\{.*\}", re.S)
_ROOT_RECORD = re.compile(r"([A-Z]\w*):([\w.]+):\{.*\}", re.S)
_LINK = re.compile(r"([A-Z]\w*)\.(\w+) (forward|reverse)")
_PROPERTY = re.compile(r"([A-Z]\w*)\.(\w+)")
_CONFLICT = re.compile(r"Evidence conflict: ([A-Z]\w*)\.(\w+)")
_COST = re.compile(r"(\w+) of (\w+(?:, \w+)*)")
_PLAN_OUTCOME = re.compile(r"(.+): the answer is (partial|truncated|blocked)")
_PHRASE = re.compile(r"[A-Za-z][A-Za-z ,'&-]{0,118}")


def _schema() -> Any:
    from operating.ontology_tools import shared_schema
    return shared_schema()


def _kind(type_id: Any) -> str:
    return type_words(type_id) or "record"


def plural(label: str) -> str:
    head, sep, tail = label.partition(" of ")
    if sep:
        return f"{plural(head)} of {tail}"
    if re.search(r"[^aeiou]y$", label):
        return label[:-1] + "ies"
    return label + ("es" if re.search(r"(s|x|ch|sh)$", label) else "s")


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _department_names() -> dict[str, str]:
    from operating.employee_permissions import PORTAL_DEPARTMENT_CODES
    return {code: ("IT" if name == "it" else name.title().replace(" And ", " and "))
            for name, code in PORTAL_DEPARTMENT_CODES.items()}


@lru_cache(maxsize=1)
def _plan_index(stamp: tuple) -> dict[str, Any]:
    """Step paths by step id, evidence names, plan titles and hops from the plan files."""
    steps: dict[str, list[tuple[tuple[str, ...], list[dict[str, Any]]]]] = {}
    evidence: set[str] = set()
    hops: dict[str, dict[str, Any]] = {}
    titles: dict[str, tuple[str, ...]] = {}
    for name, _, _ in stamp:
        try:
            plan = json.loads((PLAN_DIR / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        roots = tuple((plan.get("root") or {}).get("object_types") or ())
        if plan.get("title") and roots:
            titles[str(plan["title"])] = roots
        for step in plan.get("steps") or ():
            path = (step.get("args") or {}).get("path")
            if isinstance(path, list) and path and all(isinstance(h, dict) and "link_type" in h
                                                       for h in path):
                steps.setdefault(str(step.get("id")), []).append((roots, path))
        for item in plan.get("evidence") or ():
            evidence.add(str(item.get("name")))
            args = item.get("engine_args")
            for hop in (args.get("hops") or ()) if isinstance(args, dict) else ():
                if isinstance(hop, dict) and hop.get("label"):
                    hops.setdefault(str(hop["label"]), hop)
    return {"steps": steps, "evidence": evidence, "hops": hops, "titles": titles}


def _plans() -> dict[str, Any]:
    try:
        stamp = tuple(sorted((p.name, s.st_mtime_ns, s.st_size) for p in PLAN_DIR.glob("*.json")
                             if not p.name.startswith("_") for s in (p.stat(),)))
    except OSError:
        stamp = ()
    return _plan_index(stamp)


def _ends(path: Sequence[Mapping[str, Any]]) -> tuple[str, str, bool, bool] | None:
    """``(anchor type, reached type, every hop forward, same-record hop)`` of a path."""
    schema = _schema()
    links = getattr(schema, "link_types", {}) or {}
    types: list[str] = []
    forward, same = True, False
    for hop in path:
        link = links.get(hop.get("link_type"))
        if not link:
            return None
        start, end = ((link["from"], link["to"]) if hop.get("direction") == "forward"
                      else (link["to"], link["from"]))
        types += [start, end]
        forward &= hop.get("direction") == "forward"
        same |= link.get("name") == "same_record_as"
    return types[0], types[-1], forward, same and len(path) == 1


def _path_title(path: Sequence[Mapping[str, Any]]) -> tuple[str, str, str] | None:
    ends = _ends(path)
    if ends is None:
        return None
    anchor, reached, forward, same = ends
    if same:
        title = f"Copies of this {_kind(anchor)} in other systems"
    elif forward:
        title = f"The {_kind(reached)} of this {_kind(anchor)}"
    else:
        title = f"{plural(_kind(reached))} for this {_kind(anchor)}"
    return _sentence(title), anchor, reached


def _reason(why: Any, hop: Mapping[str, Any] | None = None) -> str:
    match = _CODE.fullmatch(str(why or "").strip())
    if hop and hop.get("question"):
        return str(hop["question"])
    if not match:
        return NOT_ESTABLISHED
    code, detail = match.group(1), match.group(2) or ""
    if (said := reason_words(code)) is not None:
        return said
    if code in ("plan_partial", "plan_blocked"):
        codes = [c.strip() for c in detail.split(",") if c.strip()]
        open_items = list(dict.fromkeys(_short(c) for c in codes)) or ["an open question"]
        listed = (", ".join(open_items[:-1]) + " and " + open_items[-1]
                  if len(open_items) > 1 else open_items[0])
        return (f"Partly answered. Still open: {listed}." if code == "plan_partial"
                else f"Not answered. Missing: {listed}.")
    if code.endswith("_not_recorded"):
        return (f"No {words(code[:-len('_not_recorded')])} was found recorded for this in "
                "what the Workbench reads; that is not proof there is none.")
    if code.endswith("_source_unknown"):
        return (f"No system the Workbench reads is known to record the "
                f"{words(code[:-len('_source_unknown')])}; a person has to say where it is kept.")
    return NOT_ESTABLISHED


def _short(code: str) -> str:
    if code in _SHORT:
        return _SHORT[code]
    for suffix, form in (("_not_recorded", "a recorded {}"), ("_source_unknown", "a known source for the {}")):
        if code.endswith(suffix):
            return form.format(words(code[:-len(suffix)]))
    if code.startswith("missing_"):
        return f"the {words(code[len('missing_'):])}"
    if code.startswith("conflict_"):
        return f"agreement on the {words(code[len('conflict_'):])}"
    return "an open question"


def _expand(label: str) -> str:
    parts = [_ABBREVIATIONS.get(w, w) for w in label.split("_") if w]
    if "to" in parts:
        cut = parts.index("to")
        return _sentence(f"{' '.join(parts[:cut])} to {' '.join(parts[cut + 1:])} link")
    return _sentence(" ".join(parts))


def question_root_types(question: str) -> list[str]:
    """The root object types a case question names, for wording only: its ``: <Type> <id>``
    tail, else the types every plan titled in it starts from; ``[]`` when neither says."""
    question = str(question or "")
    objects = getattr(_schema(), "object_types", {}) or {}
    if (match := re.search(r":\s*([A-Z][A-Za-z]+)\s+\S+\s*$", question)) \
            and match.group(1) in objects:
        return [match.group(1)]
    named = [roots for title, roots in _plans()["titles"].items() if title in question]
    if not named:
        return []
    return [type_id for type_id in named[0] if all(type_id in roots for roots in named)]


def plain_gap(gap: Mapping[str, Any], root_types: Iterable[str] = ()) -> dict[str, Any]:
    """``{"title", "text", "who"}`` for one recorded gap (or conflict); ``root_types`` are
    the case's root object types, used to tell same-named plan steps apart."""
    missing = str(gap.get("missing") or gap.get("question") or "").strip()
    roots = [t for t in root_types if t]
    schema = _schema()
    objects = getattr(schema, "object_types", {}) or {}
    plans = _plans()
    title, hop, anchor, reached = None, None, None, None
    if match := _CONFLICT.fullmatch(missing):
        anchor = match.group(1)
        title = f"Sources disagree on the {words(match.group(2))} of this {_kind(anchor)}"
    elif match := _LINK.fullmatch(missing):
        worded = _path_title([{"link_type": f"{match.group(1)}.{match.group(2)}",
                               "direction": match.group(3)}])
        if worded:
            title, anchor, reached = worded
    elif match := _HOP.fullmatch(missing):
        hop, anchor = plans["hops"].get(match.group(1)), match.group(2)
        title = _expand(match.group(1)) if hop else None
    elif match := _ROOT_RECORD.fullmatch(missing):
        anchor = match.group(1) if match.group(1) in objects else None
        title = f"Where this {_kind(anchor)} record comes from" if anchor else None
    elif match := _PLAN_OUTCOME.fullmatch(missing):
        if _PHRASE.fullmatch(match.group(1)):
            title = f"{match.group(1)}: {_OUTCOMES[match.group(2)]}"
    elif (match := _PROPERTY.fullmatch(missing)) and match.group(1) in objects:
        anchor = match.group(1)
        title = f"The {words(match.group(2))} of this {_kind(anchor)}"
    elif (match := _COST.fullmatch(missing)) and match.group(1) in _COST_PARTS:
        count = len(match.group(2).split(", "))
        title = f"{_COST_PARTS[match.group(1)]} cost of {count} order line{'s' * (count != 1)}"
    elif missing in plans["steps"]:
        candidates = plans["steps"][missing]
        chosen = next((c for c in candidates if set(c[0]) & set(roots)), candidates[0])
        worded = _path_title(chosen[1])
        if worded:
            title, anchor, reached = worded
    elif missing in _SIDES:
        title = _SIDES[missing]
    elif missing in plans["evidence"]:
        title = _sentence(words(missing))
    elif _PHRASE.fullmatch(missing) and not re.search(r"[A-Z][a-z]+[A-Z]", missing):
        title = _sentence(missing)
    return {"title": _sentence(title) if title else NO_TITLE,
            "text": _reason(gap.get("why"), hop),
            "who": _answerers(gap, hop, [reached, anchor, *roots])}


def _answerers(gap: Mapping[str, Any], hop: Mapping[str, Any] | None,
               types: Sequence[str | None]) -> list[str]:
    from operating import answerers
    names = _department_names()
    recorded = [str(w) for w in gap.get("who_can_answer") or () if str(w).strip()
                and str(w).strip().lower() != GENERIC_ANSWERER]
    if not recorded and hop and hop.get("who_can_answer"):
        recorded = [str(hop["who_can_answer"])]
    if not recorded:
        for type_id in types:
            if type_id and (codes := answerers.stage_owners(type_id)):
                recorded = codes
                break
    if not recorded:
        handles = [c.get("handle") for c in gap.get("citations") or ()
                   if isinstance(c, Mapping) and c.get("status") == "resolved"]
        recorded = answerers.for_citation(handles[0]) if handles else []
    out = []
    for item in recorded:
        if item in names:
            out.append(names[item])
        elif item.startswith(answerers.UNDECLARED):
            out.append(_undeclared(item[len(answerers.UNDECLARED):]))
        else:
            out.append(item)
    return list(dict.fromkeys(out)) or [NO_ANSWERER]


def _undeclared(label: str) -> str:
    """``Portal owner of public.app_purchase_order`` -> who keeps that record, in words."""
    match = re.fullmatch(r"(.+?) owner of (\S+)", label)
    if not match:
        return f"{label} (no department is declared)"
    kinds = _types_of(match.group(2))
    what = _kind(kinds[0][0]) if kinds else "source"
    return f"Whoever keeps the {what} record in {match.group(1)} (no department is declared)"


@lru_cache(maxsize=1024)
def _types_of(relation: str) -> tuple[tuple[str, str], ...]:
    """``(object type, binding id)`` of every binding of ``relation``."""
    schema = _schema()
    return tuple((type_id, binding["id"])
                 for type_id, spec in (getattr(schema, "object_types", {}) or {}).items()
                 for binding in spec.get("bindings") or () if binding.get("relation") == relation)


@lru_cache(maxsize=1024)
def _links(end: str, type_id: str) -> tuple[Mapping[str, Any], ...]:
    """The link types whose ``end`` (``from`` or ``to``) is ``type_id``, self-links aside."""
    return tuple(link for link in (getattr(_schema(), "link_types", {}) or {}).values()
                 if link[end] == type_id and link["from"] != link["to"])


def source_date_label(relation: str, column: str) -> str:
    """``public.order_entry`` / ``datereq`` -> ``Required date · sales order``."""
    schema = _schema()
    for type_id, binding in _types_of(relation):
        for prop in (schema.object_types[type_id].get("properties") or ()):
            if any(s.get("binding") == binding and s.get("path") == column
                   for s in prop.get("sources") or ()):
                return f"{_sentence(words(prop['name']))} · {_kind(type_id)}"
    kinds = _types_of(relation)
    # An undeclared column: ``datemanuf`` -> ``Manufacture date`` (a raw name is kept as is).
    if (match := re.fullmatch(r"date_?([a-z]+)", column)) and match.group(1) in _DATE_WORDS:
        named = f"{_DATE_WORDS[match.group(1)]} date"
    else:
        named = words(column)
    return f"{_sentence(named)} · {_kind(kinds[0][0]) if kinds else 'source record'}"


# ---- case summary ---------------------------------------------------------------

def _get(row: Mapping[str, Any], path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _norm(value: Any) -> str:
    """A stored key as text, compared exactly (no case or punctuation folding: the Workbench
    does not make a text join the engine does not make)."""
    return str(value).strip() if value not in (None, "") else ""


def _fold(value: Any) -> str:
    """A cited record's key as typed in a question (``PO5447``) against as stored
    (``PO#5447``): only to find which cited record a case root names, never to join."""
    return re.sub(r"[^0-9a-z]", "", str(value).lower()) if value not in (None, "") else ""


def _paths(type_id: str, binding: str, names: Iterable[str]) -> list[str]:
    props = {p["name"]: p for p in _schema().object_types[type_id].get("properties") or ()}
    return [s["path"] for name in names for s in (props.get(name) or {}).get("sources") or ()
            if s.get("binding") == binding]


def case_summary(view: Mapping[str, Any], rows: Mapping[str, Sequence[Mapping[str, Any]]]
                 ) -> dict[str, Any]:
    """The business summary of a case for its reader: ``roots`` (type, label, status, the
    cited record), ``counterparty``, ``lines`` cited and ``open_gaps``, with a ``title``.

    ``rows`` maps each cited row handle to the rows the reader resolved for it now; a
    root found in none of them is named by its id only."""
    schema = _schema()
    objects = getattr(schema, "object_types", {}) or {}
    relations = {handle: _relation(handle) for handle in rows}
    found: list[dict[str, Any]] = []
    for root in view.get("roots") or ():
        target, hit = _fold(root), None
        for handle in sorted(rows):
            for type_id, binding in _types_of(relations[handle]):
                spec = objects[type_id]
                keys = list(spec.get("display") or ()) + [
                    p for key in spec.get("identity_keys") or () for p in key.get("properties") or ()]
                for row in rows[handle]:
                    if target and any(_fold(_get(row, p)) == target
                                      for p in _paths(type_id, binding, keys)):
                        hit = hit or {"type": type_id, "binding": binding, "handle": handle,
                                      "row": row}
        entry: dict[str, Any] = {"id": root}
        if hit:
            display = [_get(hit["row"], p) for p in _paths(hit["type"], hit["binding"],
                                                         objects[hit["type"]].get("display") or ())]
            status = [_get(hit["row"], p) for p in _paths(hit["type"], hit["binding"], ["status"])]
            entry.update({"type": hit["type"], "type_label": _sentence(_kind(hit["type"])),
                          "label": next((str(v) for v in display if v not in (None, "")), root),
                          "status": next((str(v) for v in status if v not in (None, "")), None),
                          "handle": hit["handle"]})
            entry["_hit"] = hit
        found.append(entry)
    hits = [entry.pop("_hit") for entry in found if "_hit" in entry]
    party = _counterparty(view, hits, rows, relations)
    lines = _lines(hits, rows, relations)
    gaps = [g for g in view.get("gaps") or () if not g.get("content")]
    first = found[0] if found else None
    if first and first.get("type"):
        title = f"{first['type_label']} {first['label']}"
    elif found:
        title = "Case about " + ", ".join(str(entry["id"]) for entry in found)
    else:
        title = "Case"
    if party and party.get("name"):
        title += f" · {party['name']}"
    return {"title": title, "roots": found, "counterparty": party, "lines": lines,
            "open_gaps": len(gaps), "basis": "rows cited on this case that you may read now"}


def _relation(handle: str) -> str:
    from urllib.parse import unquote
    match = re.match(r"api://([^/]+)/", handle)
    return unquote(match.group(1)) if match else ""


TYPED_TIERS = ("verified", "reviewed")   # tiers at which the engine's link names a party
LEAD_TIER = "stored_by_text_join"


def _counterparty(view: Mapping[str, Any], hits: Sequence[Mapping[str, Any]],
                  rows: Mapping[str, Sequence[Mapping[str, Any]]],
                  relations: Mapping[str, str]) -> dict[str, Any] | None:
    """The supplier or customer of the case, only as the engine served it: a link recorded
    on the case at a typed tier (``verified``, ``reviewed``) between a cited party record
    and another cited record names the party, with that tier. A reference value a cited
    record stores for a party (a declared text reference) is shown as a lead in the
    ``stored_by_text_join`` words and names no party: the Workbench does not join text."""
    schema = _schema()
    standing = _standing(view)
    root_handles = {hit["handle"] for hit in hits}
    named = []
    for link in view.get("links") or ():
        if link.get("content") or link.get("outcome") not in TYPED_TIERS:
            continue
        handles = [c["handle"] for c in link.get("citations") or ()
                   if isinstance(c, Mapping) and c.get("status") == "resolved"
                   and c.get("handle") in rows]
        for handle in handles:
            kinds = [(t, b) for t, b in _types_of(relations[handle]) if t in PARTY_TYPES]
            others = [h for h in handles if h != handle]
            if not kinds or not others or not rows[handle]:
                continue
            type_id, binding = kinds[0]
            names = [_get(rows[handle][0], p) for p in _paths(
                type_id, binding, schema.object_types[type_id].get("display") or ())]
            name = next((str(v) for v in names if v not in (None, "")), None)
            if name:
                named.append((not root_handles.intersection(others), link.get("id") or "", {
                    "type_label": _sentence(_kind(type_id)), "name": name, "handle": handle,
                    "standing": standing.get(handle), "via": others[0],
                    "tier": link["outcome"], "tier_meaning": TIER_MEANING[link["outcome"]],
                    "link_id": link.get("id")}))
    if named:
        return min(named, key=lambda item: item[:2])[2]
    starts = list(hits) + [{"type": t, "binding": b, "handle": h, "row": r}
                           for h in sorted(rows) for t, b in _types_of(relations[h])
                           for r in rows[h]]
    for start in starts:
        for link in _links("from", start["type"]):
            if link["to"] not in PARTY_TYPES:
                continue
            for binding in link.get("bindings") or ():
                pairs = binding.get("pairs") or []
                if binding.get("from_binding") != start["binding"] or len(pairs) != 1:
                    continue
                ref = _get(start["row"], pairs[0]["from"])
                if ref in (None, ""):
                    continue
                return {"type_label": _sentence(_kind(link["to"])), "name": None,
                        "reference": f"{words(pairs[0]['from'])} {ref}",
                        "handle": start["handle"], "via": start["handle"],
                        "tier": LEAD_TIER, "tier_meaning": TIER_MEANING[LEAD_TIER],
                        "note": f"a lead: the cited {_kind(start['type'])} stores this "
                                f"{_kind(link['to'])} reference as text; "
                                f"{TIER_MEANING[LEAD_TIER]}, so no {_kind(link['to'])} is named"}
    return None


def _standing(view: Mapping[str, Any]) -> dict[str, str]:
    """Per cited handle: ``supported`` when a supported finding cites it, else the weakest
    status of a finding that does (``unknown``: a lead, not established)."""
    out: dict[str, str] = {}
    for bucket, items in (view.get("findings") or {}).items():
        for item in items or ():
            for cite in item.get("citations") or ():
                handle = cite.get("handle") if isinstance(cite, Mapping) else None
                if handle and out.get(handle) != "supported":
                    out[handle] = "supported" if bucket == "supported" else out.get(handle, bucket)
    return out


def _lines(hits: Sequence[Mapping[str, Any]], rows: Mapping[str, Sequence[Mapping[str, Any]]],
           relations: Mapping[str, str]) -> list[dict[str, Any]]:
    """Cited records that point at a root by a declared N:1 reference, counted by kind."""
    counts: dict[str, set[str]] = {}
    for hit in hits:
        for link in _links("to", hit["type"]):
            for binding in link.get("bindings") or ():
                pairs = binding.get("pairs") or []
                if binding.get("to_binding") != hit["binding"] or len(pairs) != 1:
                    continue
                key = _norm(_get(hit["row"], pairs[0]["to"]))
                for handle in rows:
                    if key and (link["from"], binding["from_binding"]) in _types_of(relations[handle]) \
                            and any(_norm(_get(r, pairs[0]["from"])) == key for r in rows[handle]):
                        counts.setdefault(words(link.get("reverse_name") or link["id"]),
                                          set()).add(handle)
    return [{"kind": kind, "cited": len(handles)} for kind, handles in sorted(counts.items())]


__all__ = ["case_summary", "plain_gap", "plural", "question_root_types", "source_date_label"]
