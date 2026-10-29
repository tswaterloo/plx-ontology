"""The explanation block of a workspace answer, in business terms (WS-14).

Every answer the employee workspace serves (an opened object, a question family run with
``plx.investigate``, a ``plx.find`` search) is mapped into one block::

    {"why": [...], "evidence": [...], "gaps": [...], "as_known_at": {...}}

* ``why``: the rule, plan step or link reason that produced the answer: how the object
  was identified (identity edges), why each linked set is linked (tier and the agreeing
  and disagreeing features), the process stage and quantity checks, the plan and how each
  answer entry was obtained (read, selected from the deciding tool, computed, counted),
  why each search candidate matched.
* ``evidence``: the cited source records the reader may open (source system, record,
  fields, page), each with the handle the workspace's source route opens.
* ``gaps``: grouped by class (``GAP_CLASSES``): missing, conflicting, withheld (worded
  ``WITHHELD``), incomplete source, upstream outage. A record, value or source the reader
  may not see is a withheld gap with at most a count the tools revealed, never its handle,
  subject or content; an absence answered while something was withheld says it may be
  withheld, never "not found" alone.
* ``as_known_at``: the capture the answer reflects (per source, from the tools'
  ``meta.freshness``), the projection's build time and mirror generation, and the state:
  ``outage`` (a source served from a held capture because the live source failed:
  ``source_unavailable``, ``live_reread_failed`` or a ``cached_mirror`` /
  ``persisted_capture`` freshness), ``stale`` (a capture older than its bound, or newer at
  the source), ``current`` or ``unknown``. A file served from the Workbench's capture in
  normal operation (``as_of_capture``) is "as of capture", never an outage.

Nothing here reads a store or a source: the block is built only from fields the tools
already answered for this reader (and, for an investigation, the access decision the
workspace passes in), so it never shows more than the answer it explains. Texts are built
from typed fields (enums, identifiers, counts); source text stays an ``untrusted_text``
envelope in ``value`` / ``title``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote

GAP_CLASSES = ("missing", "conflicting", "withheld", "incomplete_source", "upstream_outage")
WITHHELD = "withheld for your access"
MAX_WHY = 40
MAX_EVIDENCE = 40
MAX_DETAILS = 3
SOURCE_SYSTEMS = {"api": "Portal", "bc": "Business Central", "graph-mail": "Mail",
                  "sharepoint": "SharePoint", "original": "Attached original"}
FRESHNESS_SOURCES = {"portal_api": "Portal", "bc": "Business Central", "graph_mail": "Mail",
                     "sharepoint": "SharePoint", "original": "Attached originals",
                     "workbench": "Workbench case store"}
TIER_MEANING = {"verified": "confirmed: a typed stored key agrees",
                "reviewed": "confirmed by a person against the exact source records",
                "corroborated": "inferred: two or more independent signals agree",
                "stored_by_text_join": "a stored text match, not a typed key",
                "lead": "a lead, not established",
                "contradicted": "contradicted by the evidence"}
_WITHHELD_REASONS = frozenset({
    "withheld_by_permission", "policy_changed_since_handle", "marking_withheld",
    "audience_not_granted", "undecided_by_withheld_value", "partial_visibility",
    "evidence_not_fully_visible", "no_visible_match"})
# Only a live source that failed now; anything else (and any unknown reason) never reads
# as an outage.
_OUTAGE_REASONS = frozenset({"source_unavailable", "live_reread_failed"})
# Stale when the evidence says so: a capture past its bound, or (as_of_capture with state
# "stale") a newer version the live re-read found at the source.
_STALE_REASONS = frozenset({"stale_source", "as_of_capture"})
_INCOMPLETE_REASONS = frozenset({
    "as_of_capture", "stale_source", "ocr_text_unconfirmed", "ocr_unavailable", "ocr_failed",
    "cite_as_unresolvable", "cite_as_withheld", "live_read_budget", "capture_stale",
    "capture_unreadable", "capture_too_old",
    "not_served", "scan_cap", "page_cap", "freshness_truncated", "freshness_unknown",
    "content_exceeds_cap", "content_unavailable", "scope_not_read", "source_not_configured",
    "superseded_in_capture", "step_unavailable", "step_truncated",
    "not_ingested", "not_in_projection", "originals_not_versioned"})
_ABSENCES = frozenset({"no_match", "no_event", "missing_from_source", "absence_unproven"})
_REASON_TEXT = {
    "not_served": "the source does not provide this field",
    "no_match": "no matching record", "no_event": "no dated event recorded",
    "missing_from_source": "empty in the source record",
    "absence_unproven": "not found, but its absence is not proven",
    "freshness_unknown": "how current this source is, is not known",
    "audience_not_granted": "part of this source is outside your access",
    "marking_withheld": "values carry a marking you do not hold",
    "evidence_not_fully_visible": "some of the evidence is outside your access",
    "source_unavailable": "the live source did not answer",
    "live_reread_failed": "the live re-read failed now; the file is shown as captured",
    "as_of_capture": "as of capture, served from the Workbench's capture, not read live now",
    "stale_source": "the source's last capture is older than its freshness bound",
    "ocr_text_unconfirmed": "machine-read OCR text, for a person to confirm",
    "below_tier_floor": "only weak links were found",
    "staging_root": "the record comes from a staging copy: a lead, never proven",
    "not_applicable_to_source": "this source does not record it",
    "original_not_openable": "matching originals exist that you cannot open",
    "originals_store_refused": "the captured originals could not be searched for you",
    "before_first_capture": "nothing captured from this source is that old",
}
# Reason codes only the case and home views show, as full sentences (abstentions stay
# abstentions: "not proof there is none"). A code in ``_REASON_TEXT`` always takes those
# words, so every view of one code reads alike.
_SHARED_REASONS = ("no_match", "not_served", "not_applicable_to_source",
                   "evidence_not_fully_visible")
_CASE_REASON_TEXT = {
    "lead_tier_links_followed": "Found only through weaker links, such as a text match; "
                                "treat what it reached as a lead to confirm, not an answer.",
    "no_recorded_decision": "No recorded decision about this record was found in what you "
                            "may read; that is not proof there is none.",
    "value_disagreement": "The cited sources disagree. The Workbench does not pick one; "
                          "a person decides.",
    "degraded_binding": "The value is read, but its currency or unit is not proven for "
                        "this record.",
    "not_carried": "No record reached carries a readable value for this.",
    "value_empty": "The source record holds this field empty.",
    "po_lines_incomplete": "Some purchase order lines were not read with a price and an "
                           "ordered and received quantity, so totals are not complete.",
    "unpriced_line": "A line has no cited unit price.",
    "unit_price_withheld": "The unit price carries a marking you do not hold, so no line "
                           "is priced for you.",
    "receipt_actual_not_read": "The freight and duty actually posted at receipt were not "
                               "read; the estimates on the order are not the actuals.",
    "lead_evidence_not_counted": "Some records reach this only through weak links and are "
                                 "not counted in any total.",
    "root_lineage_staging": "This record was created in the Portal's staging data, so "
                            "every answer from it is a lead to confirm, not an authority.",
    "gap_cap": "More gaps were found than one case records; ask the question again to "
               "read the rest.",
    "answer_truncated": "The answer was too large to keep in full; part of it is not shown.",
    "budget_exhausted": "The investigation ran out of time before it finished.",
}


# Reason codes a one-line verdict states (the console's question box) that neither table
# above words: plx.process's lateness reason chain (``operating.process_lateness.REASONS``)
# and a question plan's answerability reasons, as clauses.
_VERDICT_REASON_TEXT = {
    "not_issued_or_closed": "the order is not issued or is closed",
    "no_expected_date": "no planned arrival date",
    "expected_dates_disagree": "the planned arrival dates disagree",
    "no_confirmation": "no supplier confirmation",
    "no_confirmation_found": "no supplier confirmation found",
    "confirmation_lead_only": "the supplier confirmation is only a lead",
    "no_promised_date": "no promised date",
    "promises_disagree": "the promised dates disagree",
    "promise_slipped": "the promised date slipped",
    "partial_receipt": "only part received",
    "receipt_coverage_unproven": "a receipt may not cover the order",
    "not_received": "no receipt",
    "missing_unit_price": "no unit price",
    "gap_not_ingested": "a source was not captured",
    "no_evidence_found": "no record answers it",
    "ambiguous_root": "several records match",
    "root_not_found": "no such record",
    "root_lead_only": "the record is only a lead",
}


def reason_phrase(code: Any) -> str | None:
    """A reason code as a clause for a one-line verdict ("Not established: <clause>, ..."):
    the words every view gives the code, ``_REASON_TEXT`` first, then the case view's
    sentence (``reason_words``) as a clause, then ``_VERDICT_REASON_TEXT``; ``None`` for an
    unknown code."""
    code = str(code or "")
    if code in _REASON_TEXT:
        return _REASON_TEXT[code]
    sentence = reason_words(code)
    if sentence:
        return sentence[:1].lower() + sentence[1:].rstrip(".")
    return _VERDICT_REASON_TEXT.get(code)


def reason_words(code: Any) -> str | None:
    """A reason code as a sentence for the case and home views; ``None`` for an unknown
    code. The words of ``_REASON_TEXT`` (the object view's) win where both have the code."""
    code = str(code or "")
    if code in _SHARED_REASONS:
        return _REASON_TEXT[code][:1].upper() + _REASON_TEXT[code][1:] + "."
    return _CASE_REASON_TEXT.get(code)
_PREFIX = {"missing": "Not established", "conflicting": "Sources disagree",
           "withheld": "Withheld for your access", "incomplete_source": "Source incomplete",
           "upstream_outage": "Source unavailable, shown as last captured"}
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


_ACRONYMS = frozenset({"bc", "po", "mc", "fm", "qc", "qa", "fg", "so", "ao", "gl", "lcy", "hs",
                       "uom", "erp", "id", "sku", "coa", "msa", "it"})
_TYPE_PREFIX = re.compile(r"^Rel(?:Public|Bc)(?:App)?(?=[A-Z])")
_TYPE_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def words(text: Any) -> str:
    return str(text or "").replace("_", " ").strip()


def field_words(name: Any) -> str:
    """``bc_unit_cost`` -> ``BC unit cost``: a field or code name with known acronyms in
    capitals (``words`` leaves them as they are, for free prose)."""
    return " ".join(w.upper() if w in _ACRONYMS else w for w in str(name or "").split("_") if w)


def type_words(name: Any) -> str:
    """``PurchaseOrderLine`` -> ``purchase order line``; ``MCApproval`` -> ``MC approval``."""
    text = _TYPE_PREFIX.sub("", str(name or "")).replace("SharePoint", "Sharepoint")
    parts = [w.upper() if w.lower() in _ACRONYMS else w.lower() for w in _TYPE_PARTS.findall(text)]
    return " ".join(parts).replace("sharepoint", "SharePoint")


def link_words(link_type: Any) -> str:
    """``PurchaseOrderLine.line_of`` -> ``purchase order line: line of``."""
    owner, _, name = str(link_type or "").partition(".")
    return f"{type_words(owner)}: {words(name)}" if name else type_words(owner)


def subject_words(subject: Any) -> str:
    """A gap or conflict subject in words: an object id names its type and key (and what
    follows it), a set handle reads "the linked records", anything else its words."""
    text = str(subject or "")
    if text.startswith("set:"):
        return "the linked records"
    kind, _, rest = text.partition(":")
    if rest and re.fullmatch(r"[A-Z][A-Za-z0-9]+", kind):
        key = rest.partition(":")[2]
        try:
            parsed, end = json.JSONDecoder().raw_decode(key)
        except ValueError:
            return citer_words(text)[:200]
        values = parsed.values() if isinstance(parsed, dict) else [parsed]
        tail = citer_words(key[end:])
        return " ".join(part for part in (type_words(kind), " ".join(
            str(v) for v in values)[:80], tail) if part)[:200]
    return citer_words(text)[:200]


def _key_words(key: str) -> str:
    try:
        parsed = json.loads(key)
    except ValueError:
        return key[:80]
    if isinstance(parsed, dict):
        return " ".join(str(v) for v in parsed.values())[:80]
    return str(parsed)[:80]


def citer_words(label: Any) -> str:
    """A workspace citer label (``identity AssemblyOrder.same_record_as``) in words."""
    return words(re.sub(r"\b[A-Z][A-Za-z0-9]+(?:\.[a-z][a-z0-9_]*)?",
                        lambda m: link_words(m.group(0)), str(label or "")))


def record_label(handle: str) -> tuple[str, str, str | None]:
    """(source system, record in words, page) of an L1 handle."""
    scheme, _, rest = handle.partition("://")
    base, _, fragment = rest.partition("#")
    at = base.rfind("@")
    if at > base.rfind("/"):
        base = base[:at]
    page = None
    match = re.search(r"(?:^|&)page=(\d+)", fragment)
    if match:
        page = f"page {match.group(1)}"
    relation, _, key = base.partition("/")
    if scheme == "api":
        relation = relation.split(".", 1)[-1]
        record = f"{words(relation)} {_key_words(unquote(key))}".strip()
    elif scheme == "original":
        record = "document"
    else:
        record = unquote(relation)[:120]   # the mailbox, company or site
    return SOURCE_SYSTEMS.get(scheme, scheme), record, page


def where_words(origin: str) -> str:
    """The workspace's call label of a gap in words."""
    tool, _, rest = origin.partition(" ")
    if tool == "plx.object":
        return "this record"
    if tool == "plx.around":
        return "linked " + link_words(rest.split(" ")[0])
    if tool == "plx.timeline":
        return "timeline" if rest in ("", "object") else "timeline of linked " + link_words(
            rest.split(" ")[0])
    if tool == "plx.process":
        return "process " + words(rest) if rest else "process"
    if tool == "plx.documents":
        return "mail and documents"
    if tool == "plx.investigate":
        return "this question"
    if tool == "plx.find":
        return "the search"
    return words(origin)


class _Gaps:
    """Gaps grouped by class and wording, each with where it came from."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.stale = False

    def add(self, cls: str, text: str, where: str, *, about: str | None = None,
            detail: str | None = None, count: int = 1) -> None:
        item = self.items.setdefault((cls, text), {"class": cls, "text": text, "where": [],
                                                  "about": [], "details": [], "count": 0})
        item["count"] += count
        for key, value in (("where", where), ("about", about), ("details", detail)):
            if value and value not in item[key] and len(item[key]) < (
                    MAX_DETAILS if key == "details" else 12):
                item[key].append(value)

    def gap(self, gap: Mapping[str, Any], where: str, *, call_withheld: bool = False) -> None:
        reason = str(gap.get("reason") or "")
        state = gap.get("state")
        if reason in _STALE_REASONS and state == "stale":
            self.stale = True
        if reason in _WITHHELD_REASONS:
            cls = "withheld"
        elif reason in _OUTAGE_REASONS:
            cls = "upstream_outage"
        elif reason in _INCOMPLETE_REASONS or state in ("stale", "not_ingested"):
            cls = "incomplete_source"
        elif state == "inaccessible":
            cls = "withheld"
        else:
            cls = "missing"
        phrase = _REASON_TEXT.get(reason, words(reason) or "not established")
        if cls == "withheld":
            # Never the subject or detail of what is withheld.
            self.add(cls, f"{_PREFIX[cls]}: {phrase}", where)
            return
        if cls == "missing" and reason in _ABSENCES and call_withheld:
            phrase += f"; some records may be {WITHHELD}"
        self.add(cls, f"{_PREFIX[cls]}: {phrase}", where,
                 about=subject_words(gap.get("subject")), detail=gap.get("detail"))

    def withheld(self, marker: Any, where: str) -> None:
        """A tool's ``withheld`` report: the count when revealed, never zero."""
        if not isinstance(marker, Mapping):
            return
        parts = [f"{n} {plural if n != 1 else one}"
                 for name, one, plural in (("properties", "value", "values"),
                                           ("objects", "record", "records"))
                 if (n := marker.get(name))]
        if parts:
            self.add("withheld", f"{' and '.join(parts)} {WITHHELD}", where)
        elif marker.get("properties") is None or marker.get("objects") is None:
            self.add("withheld", f"Some records may be {WITHHELD}; the count is not shown",
                     where)

    def result(self, result: Mapping[str, Any] | None, where: str) -> None:
        """Gaps, conflicts, withheld report and completeness of one tool answer."""
        result = result or {}
        held = isinstance(result.get("withheld"), Mapping)
        for gap in result.get("gaps") or []:
            self.gap(gap, where, call_withheld=held)
        self.conflicts(result.get("conflicts"), where)
        self.withheld(result.get("withheld"), where)
        self.completeness(result.get("completeness"), where)
        if result.get("error"):
            self.add("incomplete_source", "Source incomplete: this part could not be read",
                     where, detail=words((result["error"] or {}).get("code")))

    def conflicts(self, conflicts: Any, where: str) -> None:
        for conflict in conflicts or []:
            self.add("conflicting", f"Sources disagree: {words(conflict.get('kind'))}", where,
                     about=subject_words(conflict.get("subject")), detail=conflict.get("note"))

    def completeness(self, completeness: Any, where: str) -> None:
        if not isinstance(completeness, Mapping) or completeness.get("complete", True):
            return
        reason = completeness.get("reason") or ""
        if reason in _WITHHELD_REASONS:
            self.add("withheld", f"Part of this list is {WITHHELD}", where)
        else:
            self.add("incomplete_source",
                     f"Source incomplete: only part of the list is shown ({words(reason)})",
                     where)

    def out(self) -> list[dict[str, Any]]:
        order = {cls: index for index, cls in enumerate(GAP_CLASSES)}
        return sorted(self.items.values(), key=lambda item: (order[item["class"]],
                                                             item["text"]))


class _Evidence:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def add(self, handle: str, *, field: str | None = None, cited_for: str | None = None,
            openable: bool = True, title: Any = None) -> None:
        source, record, page = record_label(handle)
        item = self.items.setdefault(handle, {
            "label": f"{source}: {record}" + (f", {page}" if page else ""),
            "source": source, "record": record, "page": page, "fields": [],
            "cited_for": [], "handle": handle, "openable": openable})
        if title is not None:
            item["title"] = title
        if field and words(field.split(".")[-1]) not in item["fields"]:
            item["fields"].append(words(field.split(".")[-1]))
        if cited_for and cited_for not in item["cited_for"]:
            item["cited_for"].append(cited_for)

    def out(self) -> tuple[list[dict[str, Any]], int]:
        items = sorted(self.items.values(), key=lambda i: (not i["openable"], i["label"]))
        return items[:MAX_EVIDENCE], len(items)


def _features(items: Any, agrees: bool | None) -> list[str]:
    return sorted({words(f.get("feature")) for f in items or []
                   if isinstance(f, Mapping) and (agrees is None or f.get("agrees") is agrees)})


def _link_why(edges: list[Mapping[str, Any]], kind: str, label: str) -> dict[str, Any]:
    tiers: dict[str, int] = {}
    for edge in edges:
        tiers[edge.get("tier") or "unknown"] = tiers.get(edge.get("tier") or "unknown", 0) + 1
    parts = [f"{n} {words(tier)} ({TIER_MEANING.get(tier, 'tier unknown')})"
             for tier, n in sorted(tiers.items())]
    basis = sorted({f for edge in edges for f in _features(edge.get("reason"), True)})
    against = sorted({f for edge in edges for f in _features(edge.get("why_not"), None)})
    text = f"{label}: {'; '.join(parts)}"
    if basis:
        text += f". Because {', '.join(basis)} agree"
    if against:
        text += f". Against: {', '.join(against)}"
    return {"kind": kind, "text": text + ".", "tiers": tiers}


def _identity_pairs(edges: Any) -> dict[tuple[Any, frozenset[str]], list[Mapping[str, Any]]]:
    """Identity edges grouped by (link type, the two objects, either direction)."""
    pairs: dict[tuple[Any, frozenset[str]], list[Mapping[str, Any]]] = {}
    for edge in edges or []:
        pairs.setdefault((edge.get("link_type"),
                          frozenset((str(edge.get("from")), str(edge.get("to"))))), []).append(edge)
    return pairs


def _merge_features(into: list[Any], items: Any) -> None:
    for feature in items or []:
        same = next((g for g in into if g.get("feature") == feature.get("feature")
                     and g.get("agrees") == feature.get("agrees")), None)
        if same is None:
            into.append(dict(feature))
            continue
        seen = {c.get("l1_handle") for c in same.get("citations") or [] if isinstance(c, Mapping)}
        extra = [c for c in feature.get("citations") or []
                 if isinstance(c, Mapping) and c.get("l1_handle") not in seen]
        if extra:
            same["citations"] = [*(same.get("citations") or []), *extra]


def identity_candidates(edges: Any) -> list[dict[str, Any]]:
    """The identity candidates of an opened object, one per pair: a link stored in both
    directions (A same as B, B same as A) is one candidate, its evidence features merged
    (citations de-duplicated by handle). ``directions`` lists each stored (from, to);
    ``tiers_differ`` lists the tiers when the two directions disagree. The first edge's
    other fields (tier, link id, markings) stand; nothing is merged across pairs."""
    out: list[dict[str, Any]] = []
    for group in _identity_pairs(edges).values():
        first = group[0]
        merged = {**first, "reason": [], "why_not": [],
                  "directions": [{"from": e.get("from"), "to": e.get("to")} for e in group]}
        for edge in group:
            _merge_features(merged["reason"], edge.get("reason"))
            _merge_features(merged["why_not"], edge.get("why_not"))
        tiers = list(dict.fromkeys(e.get("tier") for e in group))
        if len(tiers) > 1:
            merged["tiers_differ"] = tiers
        out.append(merged)
    return out


_OBJECT_ID = re.compile(r"\b([A-Z][A-Za-z0-9]*):[a-z0-9_]+(?:\.[a-z0-9_]+)+:"
                        r"(\{[^{}]*\}|\"[^\"]*\"|-?\d+(?:\.\d+)?)")
MAX_LABELS = 500


def object_labels(*parts: Any) -> dict[str, str]:
    """Every object id named in ``parts``, in words (``subject_words``), so the console
    renders the server's wording and never rebuilds it."""
    labels: dict[str, str] = {}

    def walk(value: Any) -> None:
        if len(labels) >= MAX_LABELS:
            return
        if isinstance(value, str):
            for match in _OBJECT_ID.finditer(value):
                if match.group(0) not in labels and len(labels) < MAX_LABELS:
                    labels[match.group(0)] = subject_words(match.group(0))
        elif isinstance(value, Mapping):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    for part in parts:
        walk(part)
    return labels


def _as_known_at(metas: Iterable[Any], store: Mapping[str, Any] | None,
                 outage_gap: bool, stale_gap: bool = False) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    for meta in metas:
        if not isinstance(meta, Mapping):
            continue
        for item in meta.get("freshness") or []:
            name = item.get("source")
            kept = sources.setdefault(name, {"source": FRESHNESS_SOURCES.get(name, words(name)),
                                             "captured_at": None, "stale": False,
                                             "served_from": "live"})
            captured = item.get("captured_at")
            if captured and (kept["captured_at"] is None or captured < kept["captured_at"]):
                kept["captured_at"] = captured
            kept["stale"] = kept["stale"] or bool(item.get("stale"))
            if item.get("served_from") in ("cached_mirror", "persisted_capture"):
                kept["served_from"] = item["served_from"]
                kept["stale"] = True
                if item.get("label"):
                    kept["label"] = item["label"]
    store = dict(store or {})
    captured = sorted(s["captured_at"] for s in sources.values() if s["captured_at"])
    outage = outage_gap or any(s["served_from"] != "live" for s in sources.values())
    if outage:
        state = "outage"
    elif stale_gap or store.get("state") == "stale" or any(
            s["stale"] for s in sources.values()):
        state = "stale"
    elif store.get("state") == "current" or (captured and store.get("state") is None):
        state = "current"
    else:
        state = "unknown"
    when = captured[0] if captured else None
    label = f"As known at {when}" if when else "Capture time not stated"
    if store.get("built_at"):
        label += (f" (projection built {store['built_at']}, mirror generation "
                  f"{store.get('mirror_generation') or 'unknown'})")
    if state == "outage":
        held = [s.get("label") or f"{s['source']}: last capture" for s in sources.values()
                if s["served_from"] != "live"]
        label = ("STALE: a live source is unavailable; this answer is as last captured. "
                 + label + (". " + "; ".join(held) if held else ""))
    elif state == "stale":
        label = "STALE: the source may hold newer data than this answer reflects. " + label
    return {"captured_at": when, "built_at": store.get("built_at"),
            "generation": store.get("mirror_generation"), "state": state, "label": label,
            "sources": sorted(sources.values(), key=lambda s: s["source"])}


def _block(why: list[dict[str, Any]], evidence: _Evidence, gaps: _Gaps,
           metas: Iterable[Any], store: Mapping[str, Any] | None) -> dict[str, Any]:
    items, total = evidence.out()
    listed = gaps.out()
    return {"why": why[:MAX_WHY], "evidence": items, "evidence_total": total,
            "gaps": listed,
            "as_known_at": _as_known_at(metas, store, any(
                g["class"] == "upstream_outage" for g in listed), gaps.stale)}


def _sources_withheld(answer: Mapping[str, Any], gaps: _Gaps) -> None:
    for item in answer.get("withheld_sources") or []:
        name = str(item.get("label") or words(item.get("source")))
        gaps.add("withheld", f"{name[:1].upper()}{name[1:]} records: "
                 f"{WITHHELD}", "this console", detail=item.get("reason"))


def explain_object(answer: Mapping[str, Any]) -> dict[str, Any]:
    """The block of an opened object (``ObjectWorkspace.workspace``)."""
    card = answer.get("card") or {}
    around = answer.get("around") or {}
    timeline = answer.get("timeline") or {}
    process = answer.get("process") or {}
    documents = answer.get("documents") or {}
    why: list[dict[str, Any]] = []
    gaps, evidence = _Gaps(), _Evidence()
    values = card.get("values") or []
    if values:
        why.append({"kind": "values", "text": (
            f"{len(values)} value(s) are read from the cited source records; where sources "
            "disagree, both sides are listed as a gap, never merged.")})
    # One candidate per pair: a link stored in both directions is one identity candidate
    # (each of its distinct tiers still counted).
    for (link_type, _), edges in _identity_pairs(card.get("identity")).items():
        kept: list[Mapping[str, Any]] = []
        for edge in edges:
            if all(other.get("tier") != edge.get("tier") for other in kept):
                kept.append(edge)
        why.append(_link_why(kept, "identity", "Identified as " + link_words(link_type)))
    for hop in around.get("hops") or []:
        edges = (hop.get("result") or {}).get("edges") or []
        if edges:
            why.append(_link_why(edges, "link", f"{len(edges)} linked "
                                 f"({link_words(hop.get('link_type'))})"))
    if process.get("status") == "read":
        result = process.get("result") or {}
        stages = result.get("stages") or []
        present = sum(1 for s in stages if s.get("state") == "present")
        text = (f"Process {words(result.get('process'))}: {present} of {len(stages)} stages "
                "evidenced")
        if result.get("current_stage"):
            text += f", current stage {words(result['current_stage'])}"
        why.append({"kind": "process", "text": text + "."})
        for check in result.get("quantity_checks") or []:
            verdict = {True: "agree", False: "do not agree"}.get(check.get("agree"),
                                                                "undecided")
            why.append({"kind": "check", "text": (
                f"{' vs '.join(words(r) for r in check.get('roles') or [])}: {verdict}"
                + (f" ({check['note']})" if check.get("note") else "") + ".")})
    # Evidence: the cited captures the reader may open; the others are counted gaps.
    originals = answer.get("originals") or {}
    held: dict[str, int] = {}
    for item in originals.get("items") or []:
        if item.get("withheld"):
            source = record_label(item.get("l1_handle") or "")[0]
            held[source] = held.get(source, 0) + 1
            continue
        for field in item.get("fields") or [None]:
            evidence.add(item["l1_handle"], field=field, openable=bool(item.get("openable")))
        for label in item.get("cited_by") or []:
            evidence.add(item["l1_handle"], cited_for=citer_words(label))
    for source, count in sorted(held.items()):
        gaps.add("withheld", f"{source} records cited here: {WITHHELD}", "originals",
                 count=count)
    unlisted = ((originals.get("withheld") or {}).get("total") or 0) - sum(held.values())
    if unlisted > 0:
        gaps.add("withheld", f"More cited records: {WITHHELD}", "originals", count=unlisted)
    if documents.get("status") == "read":
        result = documents.get("result") or {}
        for doc in result.get("documents") or []:
            if doc.get("not_openable_reason"):
                gaps.add("withheld", f"Documents about this object: {WITHHELD}",
                         "mail and documents")
            else:
                evidence.add(doc["citation"], cited_for=f"{words(doc.get('kind'))} about this "
                             "object", title=doc.get("title"), openable=bool(doc.get("openable")))
    # Gaps of every call, as the workspace labelled them.
    withheld_by_origin = {"plx.object": card.get("withheld")}
    for hop in around.get("hops") or []:
        origin = f"plx.around {hop.get('link_type')} {hop.get('direction')}"
        withheld_by_origin[origin] = (hop.get("result") or {}).get("withheld")
    for call in timeline.get("calls") or []:
        withheld_by_origin[f"plx.timeline {call.get('root')}"] = (call.get("result") or {}).get(
            "withheld")
    if process.get("status") == "read":
        withheld_by_origin["plx.process"] = (process.get("result") or {}).get("withheld")
    if documents.get("status") == "read":
        withheld_by_origin["plx.documents"] = (documents.get("result") or {}).get("withheld")
    for gap in (answer.get("gaps") or {}).get("items") or []:
        origin = str(gap.get("from") or "")
        call = origin if origin in withheld_by_origin else origin.split(" stage ")[0].split(
            " condition ")[0]
        gaps.gap(gap, where_words(origin),
                 call_withheld=isinstance(withheld_by_origin.get(call), Mapping))
    for conflict in answer.get("conflicts") or []:
        gaps.conflicts([conflict], where_words(str(conflict.get("from") or "")))
    for origin, marker in withheld_by_origin.items():
        gaps.withheld(marker, where_words(origin))
    gaps.completeness(card.get("completeness"), "this record")
    for hop in around.get("hops") or []:
        gaps.completeness((hop.get("result") or {}).get("completeness"),
                          where_words(f"plx.around {hop.get('link_type')}"))
    if timeline.get("truncated"):
        gaps.add("incomplete_source", "Source incomplete: only part of the timeline is shown",
                 "timeline")
    if documents.get("status") == "not_configured":
        gaps.add("withheld", f"Mail and documents: {WITHHELD} (not served on this console)",
                 "mail and documents")
    _sources_withheld(answer, gaps)
    metas = [card.get("meta")]
    metas += [(hop.get("result") or {}).get("meta") for hop in around.get("hops") or []]
    metas += [(call.get("result") or {}).get("meta") for call in timeline.get("calls") or []]
    metas += [(process.get("result") or {}).get("meta"), (documents.get("result") or {}).get(
        "meta")]
    return _block(why, evidence, gaps, metas, answer.get("store"))


def _cited_handles(value: Any, out: dict[str, str]) -> None:
    """Every value citation in ``value``: handle -> field path."""
    if isinstance(value, Mapping):
        handle = value.get("l1_handle")
        if isinstance(handle, str):
            out.setdefault(handle, value.get("field_path") or "")
            return
        for item in value.values():
            _cited_handles(item, out)
    elif isinstance(value, list):
        for item in value:
            _cited_handles(item, out)


def _decided_evidence(cited: Mapping[str, str], labels: Mapping[str, str],
                      decide: Callable[[list[str]], Mapping[str, Any]] | None,
                      gaps: _Gaps, where: str) -> _Evidence:
    evidence = _Evidence()
    decided = decide(sorted(cited)) if decide is not None else {}
    held: dict[str, int] = {}
    for handle in sorted(cited):
        if decided.get(handle) is not None:
            source = record_label(handle)[0]
            held[source] = held.get(source, 0) + 1
            continue
        evidence.add(handle, field=cited[handle], cited_for=labels.get(handle))
    for source, count in sorted(held.items()):
        gaps.add("withheld", f"{source} records cited here: {WITHHELD}", where, count=count)
    return evidence


def _plan_title(family: Any) -> str | None:
    from operating.ontology_tools_investigate import PLAN_DIR
    if not isinstance(family, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", family):
        return None
    try:
        return json.loads((PLAN_DIR / f"{family}.json").read_text("utf-8")).get("title")
    except (OSError, ValueError):
        return None


def _entry_why(entry: Mapping[str, Any]) -> dict[str, Any]:
    name, kind = words(entry.get("name")), entry.get("kind")
    if entry.get("value") is None:
        how = "not established (see the gaps)"
    elif kind == "property":
        how = (f"read from the {type_words(entry.get('object_type'))} "
               f"{words(entry.get('property'))} in its source record")
    elif kind == "select":
        how = "the verdict of the tool that decides it, not recomputed"
    elif kind == "computed":
        how = "computed by the Workbench from " + ", ".join(
            words(i) for i in entry.get("inputs") or [])
    elif kind == "count":
        how = f"counted: {type_words(entry.get('object_type'))} records reached and visible to you"
    else:
        how = words(kind)
    if entry.get("check") == "passed":
        how += "; re-checked against its source row"
    elif entry.get("check") == "failed":
        how += "; it no longer matches its source row"
    if entry.get("lead_basis"):
        how += "; it rests on a lead link, so its connection is not established"
    item = {"kind": "answer", "text": f"{name}: {how}.", "value": entry.get("value")}
    for key in ("unit", "currency", "quantity_role", "steps"):
        if entry.get(key):
            item[key] = entry[key]
    return item


def explain_investigation(answer: Mapping[str, Any],
                          decide: Callable[[list[str]], Mapping[str, Any]] | None = None
                          ) -> dict[str, Any]:
    """The block of a question family run (``ObjectWorkspace.investigate``); ``decide``
    is the workspace access's per-record decision (a withheld record is a gap)."""
    result = answer.get("investigation") or {}
    gaps = _Gaps()
    why: list[dict[str, Any]] = []
    title = _plan_title(answer.get("family"))
    steps = result.get("steps") or {}
    why.append({"kind": "plan", "text": (
        f"Answered by the question plan “{title or words(answer.get('family'))}”"
        + (f" in {steps['used']} of {steps['budget']} tool steps" if steps else "") + ".")})
    answerability = result.get("answerability") or {}
    if answerability:
        reasons = ", ".join(words(r) for r in answerability.get("reasons") or [])
        why.append({"kind": "answerability", "text": (
            f"Answerability: {words(answerability.get('state'))}"
            + (f" ({reasons})" if reasons else "") + ".")})
    for entry in result.get("answer") or []:
        why.append(_entry_why(entry))
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for edge in (result.get("chain") or {}).get("edges") or []:
        by_type.setdefault(edge.get("link_type") or "", []).append(edge)
    for link_type, edges in sorted(by_type.items()):
        why.append(_link_why(edges, "link", f"{len(edges)} linked ({link_words(link_type)})"))
    cited: dict[str, str] = {}
    labels: dict[str, str] = {}
    for entry in result.get("answer") or []:
        before = set(cited)
        _cited_handles(entry.get("citation"), cited)
        for handle in set(cited) - before:
            labels[handle] = words(entry.get("name"))
    _cited_handles(result.get("values"), cited)
    _cited_handles((result.get("chain") or {}).get("edges"), cited)
    evidence = _decided_evidence(cited, labels, decide, gaps, "this question")
    gaps.result(result, "this question")
    for failure in (result.get("claim_checks") or {}).get("failures") or []:
        gaps.add("conflicting", "Sources disagree: a cited value no longer matches its source "
                 "row", "this question", about=words(failure.get("name")),
                 detail=words(failure.get("code")))
    _sources_withheld(answer, gaps)
    return _block(why, evidence, gaps, [result.get("meta")], answer.get("store"))


def explain_find(answer: Mapping[str, Any]) -> dict[str, Any]:
    """The block of a search (``ObjectWorkspace.find``)."""
    result = answer.get("find") or {}
    gaps = _Gaps()
    why: list[dict[str, Any]] = []
    candidates = result.get("candidates") or []
    if len(candidates) > 1:
        why.append({"kind": "match", "text": (
            f"{len(candidates)} records match; none is picked for you, they are ranked by "
            "how strongly the identifier matches.")})
    for candidate in candidates[:10]:
        matched = candidate.get("matched") or {}
        normalized = matched.get("normalized")
        shown = f" {normalized}" if isinstance(normalized, str) else ""
        kind = type_words((candidate.get("object") or {}).get("object_type"))
        text = (f"#{candidate.get('rank')} {kind}: "
                f"its {words(matched.get('identifier_kind'))}{shown} matched "
                f"({'an alias' if matched.get('role') == 'alias' else 'its own identifier'}), "
                f"{TIER_MEANING.get(candidate.get('tier'), 'tier unknown')}")
        against = _features(candidate.get("why_not"), None)
        if against:
            text += f". Against: {', '.join(against)}"
        why.append({"kind": "match", "text": text + "."})
    evidence = _Evidence()
    for candidate in candidates[:10]:
        display = (candidate.get("object") or {}).get("display")
        origin = display.get("origin") if isinstance(display, Mapping) else None
        if isinstance(origin, Mapping) and isinstance(origin.get("citation"), str):
            evidence.add(origin["citation"], field=origin.get("field_path"),
                         cited_for=f"match #{candidate.get('rank')}")
    held = isinstance(result.get("withheld"), Mapping)
    if result.get("status") == "not_found" and held and any(
            result["withheld"].get(name) != 0 for name in ("properties", "objects")):
        gaps.add("withheld", f"No match visible to you; a match may be {WITHHELD}",
                 "the search")
    gaps.result(result, "the search")
    return _block(why, evidence, gaps, [result.get("meta")], answer.get("store"))


def explain(answer: Mapping[str, Any],
            decide: Callable[[list[str]], Mapping[str, Any]] | None = None
            ) -> dict[str, Any]:
    """``answer`` with its ``explanation`` block, for whichever workspace answer it is
    (an opened object, a question family run, a search); other answers are unchanged."""
    if not isinstance(answer, dict):
        return answer
    if answer.get("status") == "read" and "card" in answer:
        block = explain_object(answer)
        block["identity"] = identity_candidates((answer.get("card") or {}).get("identity"))
    elif "investigation" in answer:
        block = explain_investigation(answer, decide)
    elif "find" in answer:
        block = explain_find(answer)
    else:
        return answer
    block["labels"] = object_labels(answer, block)
    return {**answer, "explanation": block}


__all__ = ["GAP_CLASSES", "WITHHELD", "explain", "explain_find", "explain_investigation",
           "explain_object", "field_words", "identity_candidates", "object_labels",
           "reason_phrase", "reason_words", "record_label", "type_words", "words"]
