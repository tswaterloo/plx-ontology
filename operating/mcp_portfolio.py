"""plx.find's portfolio mode and the readable answers of the connector's business questions.

A portfolio question (``portfolio_plan``: a phrase list matched whole, as home mode's,
naming no record) is answered by its investigate plan (``ontology/plans/<id>.json``) on
every object of the plan's root type the reader may list (``plx.find`` with
``object_type`` alone, paged), one ranked row per object: a one-line reason or the named
missing evidence, with the citations the reader may open. ``late_po`` rows are the one
lateness verdict (``operating.status_card.verdict``: the console card's and
``plx.investigate`` F14's readable answer), so the three never disagree; any other plan is
summarised generically from its run (its last required evidence is the headline: answered
with a value, else the ``missing_<evidence>`` reasons it gave). A phrase routes only to a
plan file that exists: no plan, no route (lot hold impact has none yet).

Everything is read through the caller's ``call`` (the host's tools under this call's
grant) and ``readable`` (the console's access decision); nothing is written. A row with
nothing the reader may cite is withheld (counted, never shown); an object listed but not
answered is counted in ``not_evaluated`` by typed reason, never dropped silently.

The ``*_text`` functions render the default MCP text content of these answers: at most
``TEXT_BYTES`` of plain lines in the Workbench's own words, typed values (dates, counts,
field names) and citation handles. The text replaces the output firewall's JSON for the
client, so it holds only what that firewall would leave bare: a record's label shows only
as an identifier of a kind-specific identifier kind its type declares
(``injection_guard.is_identifier``, the firewall's own test), record names inside a
rolled-up status-card line are replaced by the Workbench's words, a home gap's summary
(source text) is never echoed, and any other line not in the Workbench's plain words is
left to its citation.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from functools import lru_cache

from . import injection_guard, po_status, status_card

PLAN_DIR = Path(__file__).resolve().parent.parent / "ontology" / "plans"
SCAN_CAP = 60          # objects judged per question (past it: not_evaluated scan_cap)
LIST_PAGE = 50         # plx.find's page maximum
LIST_CAP = 2000        # objects listed per question (past it: more)
ROW_CITATIONS = 5
ROW_LIMIT = 20
TEXT_BYTES = 2048
LATE_PO = "late_po"

# The business's time zone: "today" for an inbox's age, a date-range scope and lateness
# (one definition, po_status.BUSINESS_TZ).
BUSINESS_TZ = po_status.BUSINESS_TZ
# What counts as a scope that starts a fresh scan, per plan root type: every filter must be
# one of these, by property (each declared by the type's schema; tested). A date range
# needs a lower bound (gte) no more than SCOPE_MAX_DAYS before today (ET).
SCOPE_PROPERTIES: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "PurchaseOrder": {
        "supplier": ("vendor_number", "vendor_name"),
        "order_set": ("po_number", "fm_po_number", "sage_po_number", "fm_generated_po_id",
                      "renumbered_to"),
        "date_range": ("order_date", "expected_date"),
    },
}
SCOPE_MAX_DAYS = 366
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def business_today(now: datetime) -> date:
    """``now`` (timezone-aware) as the business's calendar day (America/New_York):
    ``po_status.business_today``, the day lateness is judged on."""
    return po_status.business_today(now)


def scope_refused(object_type: str, filters: Any, today: date) -> list[str]:
    """Why ``filters`` are not a scope for a fresh scan of ``object_type`` (empty: they
    are): each filter must name a supplier (eq, in), an order set (eq, in) or a date
    range (gte, lt with a day value), and a date range needs a lower bound no older than
    ``SCOPE_MAX_DAYS`` before ``today``. Unknown shapes are refused, never scanned."""
    kinds = SCOPE_PROPERTIES.get(object_type)
    if not kinds or not isinstance(filters, list) or not filters:
        return ["no_scope"]
    refused: list[str] = []
    lower: dict[str, date] = {}
    for one in filters:
        prop = one.get("property") if isinstance(one, Mapping) else None
        op = one.get("op") if isinstance(one, Mapping) else None
        value = one.get("value") if isinstance(one, Mapping) else None
        kind = next((k for k, props in kinds.items() if prop in props), None)
        if kind in ("supplier", "order_set") and op in ("eq", "in") and value not in (None, [], ""):
            continue
        if kind == "date_range" and op in ("gte", "lt") and isinstance(value, str) \
                and _DAY.match(value):
            try:
                day = date.fromisoformat(value[:10])
            except ValueError:
                refused.append(f"{prop}_{op}_not_a_day")
                continue
            if op == "gte":
                lower[prop] = max(day, lower.get(prop, day))
            continue
        refused.append(f"{prop if isinstance(prop, str) else 'filter'}_{op}_not_a_scope"[:64])
    dated = {f.get("property") for f in filters if isinstance(f, Mapping)
             and f.get("property") in kinds.get("date_range", ())}
    only_dates = all(isinstance(f, Mapping) and f.get("property") in dated for f in filters)
    for prop in sorted(p for p in dated if isinstance(p, str)):
        if prop not in lower:
            if only_dates:
                refused.append(f"{prop}_without_lower_bound")
        elif (today - lower[prop]).days > SCOPE_MAX_DAYS:
            refused.append(f"{prop}_range_too_wide")
    return refused

Call = Callable[[str, dict[str, Any]], dict[str, Any]]
Readable = Callable[[Sequence[str]], Iterable[str]]

_POS = r"(?:purchase orders|pos)"
_LATE = r"(?:late|overdue|past due|behind)"
_OUR = r"(?:my |our |the |any |any of (?:my |our |the )?)?"
_GOODS = r"(?:products?|items?|orders?|purchase orders?|pos?)"
_PORTFOLIO_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), plan) for pattern, plan in (
        (rf"(?:which|what) {_OUR}{_POS} (?:are )?{_LATE},? and why", LATE_PO),
        (rf"why (?:are|is) {_OUR}{_POS} {_LATE}", LATE_PO),
        (rf"(?:are|is) {_OUR}{_POS} {_LATE},? and why", LATE_PO),
        (rf"what does (?:this|each|every|a) {_GOODS} (?:really |actually )?cost"
         r"(?: us)?(?: to land| landed| once landed| when landed)", "landed_cost"),
        (rf"what (?:is|are) the (?:real |true |actual )?landed costs?"
         rf"(?: of {_OUR}(?:each |every )?{_GOODS}s?)?", "landed_cost"),
        (rf"what did {_OUR}(?:each |every )?{_GOODS}s? (?:really |actually )?cost"
         r"(?: us)?(?: to land| landed)", "landed_cost"),
    ))

_SAFE_LINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ,.:;'()/%+-]{0,239}")
# A label is shown only as one identifier-shaped token (PO5447, SKU-12/A): a label with
# spaces may carry prose, so it falls back to the object id's type and binding.
_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{0,47}")
# An answered plan row shows its value only when it is a number or a date, with an
# optional currency or unit code; any other source value is left to its citation.
_ANSWERED_LINE = re.compile(
    r"[a-z][a-z0-9 ]{0,63}: (?:-?[0-9][0-9,]{0,18}(?:\.[0-9]{1,8})?(?: [A-Z]{3})?"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2})")
_SNAKE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{5,15}(?:Z|[+-][0-9]{2}:[0-9]{2})")
_TYPE = re.compile(r"[A-Z][A-Za-z0-9]{0,63}")
_NUMBER = re.compile(r"-?[0-9]{1,15}(?:\.[0-9]{1,8})?")


def normalized(query: Any) -> str | None:
    """The question as home mode matches it: lower case, runs of spaces as one, one
    trailing '?', '.' or '!' dropped."""
    if not isinstance(query, str):
        return None
    return re.sub(r" +", " ", query.strip().lower()).rstrip("?.!").strip()


def plan_doc(plan_id: str, directory: Path = PLAN_DIR) -> dict[str, Any] | None:
    """The plan file ``plan_id`` names, or None when there is none (no route to it)."""
    try:
        doc = json.loads((directory / f"{plan_id}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("id") == plan_id else None


def portfolio_plan(query: Any, directory: Path = PLAN_DIR) -> dict[str, Any] | None:
    """The plan a portfolio question asks, or None (not a portfolio phrase, or its plan
    file is absent)."""
    text = normalized(query)
    if text is None:
        return None
    for pattern, plan in _PORTFOLIO_PHRASES:
        if pattern.fullmatch(text):
            return plan_doc(plan, directory)
    return None


def list_objects(call: Call, object_type: str, cap: int = LIST_CAP,
                 filters: Sequence[Mapping[str, Any]] = ()
                 ) -> tuple[list[str], bool, str | None]:
    """Every object of ``object_type`` the reader may list (``plx.find`` object_type
    alone, or with ``filters``, paged), up to ``cap``: (ids, more, failure reason or
    None)."""
    ids: list[str] = []
    seen: set[str] = set()
    cursor = None
    while True:
        arguments: dict[str, Any] = {"object_type": object_type, "limit": LIST_PAGE}
        if filters:
            arguments["filters"] = [dict(f) for f in filters]
        if cursor:
            arguments["cursor"] = cursor
        found = call("plx.find", arguments)
        if not isinstance(found, Mapping) or found.get("error") or \
                found.get("status") not in ("found", "ambiguous", "not_found"):
            return ids, False, "listing_failed"
        for candidate in found.get("candidates") or ():
            oid = ((candidate or {}).get("object") or {}).get("object_id")
            if isinstance(oid, str) and oid not in seen:
                seen.add(oid)
                ids.append(oid)
        completeness = found.get("completeness") or {}
        cursor = completeness.get("next_cursor")
        if len(ids) >= cap:
            return ids[:cap], len(ids) > cap or bool(cursor), None
        if not cursor:
            more = bool(completeness.get("truncated"))
            return ids, more, "listing_truncated" if more else None


def _cased(words: Iterable[str]) -> list[str]:
    return sorted({form for w in words for form in (w, w.lower(), w.title())})


# Past SCAN_CAP, which objects are judged first: those no "settled" listing returns
# (for a purchase order: a header word po_status reads as cancelled, draft or received,
# or a received / complete flag), then the most recent (``RECENT_PROP`` on or after each
# of ``RECENT_DAYS`` before as_of), else listing order. Each listing is plx.find under
# the same grant; one that fails orders nothing (the counts never depend on it).
_SETTLED: Mapping[str, tuple[dict[str, Any], ...]] = {
    "PurchaseOrder": (
        *({"property": prop, "op": "in", "value": _cased(
            {*po_status.CANCELLED_WORDS, *po_status.DRAFT_WORDS, po_status.RECEIVED})}
          for prop in ("status", "fm_status", "bc_status")),
        {"property": "bc_fully_received", "op": "eq", "value": True},
        {"property": "sage_is_complete", "op": "eq", "value": True}),
}
RECENT_PROP = {"PurchaseOrder": "order_date"}
RECENT_DAYS = (30, 90, 365)


def _judging_order(call: Call, object_type: str, ids: list[str], *,
                   as_of: date | None,
                   list_cap: int, scope: Sequence[Mapping[str, Any]] = ()) -> list[str]:
    """``ids`` in the order they are judged when they are more than the scan cap
    (``scope``: the question's own filters, kept on every ordering listing)."""
    def listed(filters: Sequence[Mapping[str, Any]]) -> set[str]:
        found, _more, failed = list_objects(call, object_type, list_cap,
                                            [*scope, *filters])
        return set() if failed == "listing_failed" else set(found)

    settled: set[str] = set()
    for one in _SETTLED.get(object_type, ()):
        settled |= listed([one])
    recent: list[set[str]] = []
    prop = RECENT_PROP.get(object_type) if as_of is not None else None
    for days in RECENT_DAYS if prop else ():
        since = (as_of - timedelta(days=days)).isoformat()
        recent.append(listed([{"property": prop, "op": "gte", "value": since}]))

    def key(item: tuple[int, str]) -> tuple[int, int, int]:
        n, oid = item
        age = next((i for i, ids_since in enumerate(recent) if oid in ids_since),
                   len(recent))
        return (oid in settled, age, n)
    return [oid for _n, oid in sorted(enumerate(ids), key=key)]


def _handles(citations: Iterable[Any]) -> list[str]:
    out = []
    for c in citations:
        handle = c.get("handle") if isinstance(c, Mapping) else c
        if isinstance(handle, str) and handle not in out:
            out.append(handle)
    return out


def _kept(handles: Sequence[str], readable: Readable | None) -> list[str]:
    if readable is None or not handles:
        return list(handles)
    allowed = set(readable(list(handles)))
    return [h for h in handles if h in allowed]


def late_po_row(call: Call, object_id: str, *, as_of: date, readable: Readable | None
                ) -> tuple[dict[str, Any] | None, str | None]:
    """One late_po row: the object's lateness verdict (``status_card.verdict``), or the
    typed reason it has none."""
    found = status_card.verdict(call, object_id, as_of=as_of, readable=readable)
    if found["status"] != "read":
        return None, f"card_{found['status']}"
    return {"object_id": object_id, "state": found["state"], "line": found["line"],
            "missing": [m for m in found["missing"] if _SNAKE.fullmatch(m)][:8],
            "days_late": found["days_late"],
            "label": found["label"], "label_citations": _handles(found["label_citations"]),
            "citations": _handles(found["citations"])}, None


def _plain(value: Any) -> str | None:
    if isinstance(value, Mapping):
        if "untrusted_text" in value:
            value = value["untrusted_text"]
        elif "amount" in value:
            unit = value.get("currency") or value.get("unit") or ""
            value = f"{value['amount']} {unit}".strip()
        elif "value" in value:
            return _plain(value["value"])
        else:
            return None
    if isinstance(value, bool) or value is None:
        return None
    return str(value)


def plan_row(call: Call, plan: Mapping[str, Any], object_id: str, *,
             readable: Readable | None) -> tuple[dict[str, Any] | None, str | None]:
    """One row of a plan other than late_po: its run on ``object_id`` summarised by the
    plan's headline (its last required evidence)."""
    found = call("plx.investigate", {"family": plan["id"], "root": object_id})
    error = found.get("error") if isinstance(found, Mapping) else "investigate_failed"
    if error:
        code = error.get("code") if isinstance(error, Mapping) else None
        code = code if isinstance(code, str) and _SNAKE.fullmatch(code) else "failed"
        return None, f"investigate_{code}"[:64]
    required = [e["name"] for e in plan.get("evidence") or () if e.get("required")]
    head = required[-1] if required else None
    reasons = [r for r in (found.get("answerability") or {}).get("reasons") or ()
               if isinstance(r, str) and _SNAKE.fullmatch(r)]
    missing = list(dict.fromkeys(r[len("missing_"):] for r in reasons
                                 if r.startswith("missing_") and _SNAKE.fullmatch(r[8:])))
    answer = [a for a in found.get("answer") or () if isinstance(a, Mapping)]
    named = re.compile(re.escape(head) + r"(?:_[0-9]+)?") if head else None
    heads = [a for a in answer if named and isinstance(a.get("name"), str)
             and named.fullmatch(a["name"]) and _plain(a.get("value")) is not None]
    cited = _handles((a.get("citation") or {}).get("l1_handle") for a in answer
                     if a.get("value") is not None and isinstance(a.get("citation"), Mapping))
    root = found.get("root") if isinstance(found.get("root"), Mapping) else {}
    display = root.get("display") if isinstance(root.get("display"), Mapping) else {}
    label_handle = (display.get("origin") or {}).get("citation")
    label_citations = _handles([label_handle]) if label_handle else []
    if heads and head not in missing:
        value = _plain(heads[0].get("value"))
        state, line = "answered", f"{head.replace('_', ' ')}: {value}"
        head_cites = _handles([(heads[0].get("citation") or {}).get("l1_handle")])
        cited = head_cites + [h for h in cited if h not in head_cites]
        missing = []
    else:
        state = "missing"
        line = (f"Not established: {', '.join(m.replace('_', ' ') for m in missing)} "
                f"missing." if missing else
                f"Not established ({', '.join(reasons[:3]) or 'no answer'}).")
    return {"object_id": object_id, "state": state, "line": line, "missing": missing[:8],
            "days_late": None, "label": _plain(display) if label_citations else None,
            "label_citations": label_citations,
            "citations": [*label_citations, *(h for h in cited if h not in label_citations)]
            }, None


_LATE_ORDER = {"late": 0, "undecided": 1, "on_time": 3}


def _rank(plan_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if plan_id == LATE_PO:
        def key(item: tuple[int, dict[str, Any]]) -> tuple[Any, ...]:
            n, row = item
            order = _LATE_ORDER[row["state"]] + (0 if row["state"] != "undecided"
                                                 or row["missing"] else 1)
            return (order, -(row["days_late"] or 0), n)
    else:
        def key(item: tuple[int, dict[str, Any]]) -> tuple[Any, ...]:
            n, row = item
            return (0 if row["state"] == "answered" else 1, len(row["missing"]), n)
    return [row for _n, row in sorted(enumerate(rows), key=key)]


def scan(call: Call, plan: Mapping[str, Any], *, as_of: date, readable: Readable | None,
         scan_cap: int = SCAN_CAP, list_cap: int = LIST_CAP,
         filters: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """``plan`` answered on every object of its root type the reader may list that meets
    ``filters`` (the question's scope: a supplier, an order set, a date range): the ranked
    rows (each with at least one readable citation) and the counts that account for every
    listed object (``evaluated`` + ``not_evaluated`` = ``in_scope``). Past ``scan_cap``
    the open, then most recent, objects are judged first (``_judging_order``); the rest
    are counted ``scan_cap`` and ``more`` is set."""
    object_type = plan["root"]["object_types"][0]
    ids, more, failed = list_objects(call, object_type, list_cap, filters)
    reasons: Counter[str] = Counter()
    if failed == "listing_failed":
        reasons["listing_failed"] += 1
    rows: list[dict[str, Any]] = []
    withheld = 0
    judged = (_judging_order(call, object_type, ids, as_of=as_of, list_cap=list_cap,
                             scope=filters)
              if len(ids) > scan_cap else ids)
    for n, oid in enumerate(judged):
        if n >= scan_cap:
            reasons["scan_cap"] += len(ids) - scan_cap
            more = True
            break
        try:
            row, why = (late_po_row(call, oid, as_of=as_of, readable=readable)
                        if plan["id"] == LATE_PO else plan_row(call, plan, oid, readable=readable))
        except (KeyError, ValueError, TypeError, PermissionError) as exc:
            row, why = None, f"read_failed_{type(exc).__name__.lower()}"[:64]
        if row is None:
            reasons[why or "not_answered"] += 1
            continue
        row["citations"] = _kept(row["citations"], readable)
        row["label_citations"] = _kept(row["label_citations"], readable)
        if not row["label_citations"]:
            row["label"] = None
        if not row["citations"]:
            withheld += 1
            continue
        rows.append(row)
    rows = _rank(plan["id"], rows)
    evaluated = len(rows) + withheld
    return {"plan": plan["id"], "family": plan["families"][0], "object_type": object_type,
            "in_scope": len(ids), "evaluated": evaluated,
            "not_evaluated": len(ids) - evaluated,
            "not_evaluated_reasons": [{"reason": r, "count": c}
                                      for r, c in sorted(reasons.items()) if c][:16],
            "withheld": withheld,
            "answered": sum(1 for r in rows if not r["missing"]),
            "missing": sum(1 for r in rows if r["missing"]),
            "more": more, "rows": rows}


def _envelope(text: str, handle: str) -> dict[str, Any]:
    return {"untrusted_text": text, "origin": {"citation": handle}}


def portfolio_result(found: Mapping[str, Any], limit: int = ROW_LIMIT) -> dict[str, Any]:
    """The contract's ``portfolio`` object of a ``scan`` (rows cut to ``limit``)."""
    rows = []
    for rank, row in enumerate(found["rows"][:limit], 1):
        shown: dict[str, Any] = {
            "rank": rank, "object_id": row["object_id"], "state": row["state"],
            "line": _envelope(row["line"], row["citations"][0]),
            "missing": row["missing"], "days_late": row["days_late"],
            "citations": row["citations"][:ROW_CITATIONS],
            "citations_total": len(row["citations"])}
        if row["label"] and row["label_citations"]:
            shown["label"] = _envelope(row["label"], row["label_citations"][0])
        rows.append(shown)
    out = {key: found[key] for key in (
        "plan", "family", "object_type", "in_scope", "evaluated", "not_evaluated",
        "not_evaluated_reasons", "withheld", "answered", "missing", "more")}
    out.update(returned=len(rows), rows=rows, hint=(
        "Every record of the plan's root type you may list, answered by the plan "
        "(late_po: the status card's lateness verdict), ranked; each row one line with "
        "its citations or the evidence it names missing. Run plx.investigate with the "
        "family and a row's object_id for the whole chain; cite what you read. "
        "not_evaluated: listed but not answered (reasons); withheld: nothing you may cite."))
    return out


# ---- readable text (the default MCP text content) ---------------------------------

SOURCE_NOTE = ("Values are cited; read a citation with plx.read. Ask with "
               "response_format detailed for the full JSON.")


def _safe(text: Any, pattern: re.Pattern[str] = _SAFE_LINE) -> str | None:
    if isinstance(text, Mapping):
        text = text.get("untrusted_text")
    return text if isinstance(text, str) and pattern.fullmatch(text) else None


@lru_cache(maxsize=None)
def _label_kinds(object_type: str) -> tuple[str, ...]:
    """The kind-specific identifier kinds ``object_type``'s properties declare (the
    committed ontology schema): the only kinds whose value the firewall leaves bare
    whatever text it came from."""
    from ontology.schema import load_schema
    spec = load_schema().object_types.get(object_type) or {}
    kinds = (injection_guard.identifier_kind_of(object_type, item.get("name", ""))
             for item in spec.get("properties") or () if isinstance(item, Mapping))
    return tuple(dict.fromkeys(k for k in kinds if injection_guard.kind_specific(k)))


def _id_type(object_id: Any) -> str | None:
    parts = str(object_id or "").split(":", 2)
    return parts[0] if len(parts) == 3 and _TYPE.fullmatch(parts[0]) else None


def bare_label(label: Any, object_id: Any) -> str | None:
    """``label`` when the output firewall would emit it bare (an identifier of a
    kind-specific kind of the object's type: 'PO5447' as a petra_po), else None: a
    supplier's name or 'IGNORE-ALL-PREVIOUS-1' is source text, never shown in the text."""
    label = _safe(label, _SAFE_LABEL)
    object_type = _id_type(object_id)
    if label is None or object_type is None:
        return None
    return label if any(injection_guard.is_identifier(label, kind)
                        for kind in _label_kinds(object_type)) else None


def _name(row: Mapping[str, Any]) -> str:
    """A row's label when the firewall would leave it bare (``bare_label``), else its
    object id's type and binding (the id's grammar holds no source prose)."""
    label = bare_label(row.get("label"), row.get("object_id"))
    if label:
        return label
    parts = str(row.get("object_id") or "").split(":", 2)
    return f"{parts[0]} in {parts[1]}" if len(parts) == 3 and _id_type(row.get("object_id")) \
        else "record"


# A status card's rolled-up line names the orders it rolls up by their labels (source
# text): in the text the names are replaced by the Workbench's words.
_ROLLUP_LINES = tuple(
    (re.compile(re.escape(template).replace(re.escape("{names}"), ".*")
                + r"(?: The link to (?:it|them) is a lead to confirm\.)?", re.S),
     template.format(names="the orders in the cited records"))
    for template in (*status_card.ROLLUP_WHY.values(), "Seen for {names}."))


def _line(text: Any, pattern: re.Pattern[str] = _SAFE_LINE) -> str | None:
    """A verdict or row line for the text: a rolled-up line with its names replaced,
    else the line when it is plain words (``pattern``), else None."""
    if isinstance(text, Mapping):
        text = text.get("untrusted_text")
    if not isinstance(text, str):
        return None
    for rolled, words in _ROLLUP_LINES:
        if rolled.fullmatch(text):
            return words
    return _safe(text, pattern)


def _fit(head: list[str], items: list[str], tail: list[str], total: int) -> str:
    """``head``, as many ``items`` as fit ``TEXT_BYTES`` with ``tail``, and a line saying
    how many were left out."""
    def build(n: int) -> str:
        rest = total - n
        more = [f"... {rest} more in the structured result."] if rest > 0 else []
        return "\n".join([*head, *items[:n], *more, *tail])
    n = len(items)
    while n and len(build(n).encode("utf-8")) > TEXT_BYTES:
        n -= 1
    text = build(n)
    return text if len(text.encode("utf-8")) <= TEXT_BYTES else \
        text.encode("utf-8")[:TEXT_BYTES].decode("utf-8", "ignore")


_STATE_WORDS = {"late": "late", "undecided": "undecided", "on_time": "on time",
                "answered": "answered", "missing": "missing evidence"}


def _row_line(rank: int, row: Mapping[str, Any]) -> str:
    state = _STATE_WORDS.get(row["state"], row["state"])
    if row.get("days_late"):
        state += f", {row['days_late']} days"
    if row.get("missing"):
        state += f" (missing: {', '.join(row['missing'])})"
    if row["state"] == "answered":
        line = _line(row["line"], _ANSWERED_LINE) or "Given in the cited record."
    else:
        line = _line(row["line"]) or "See the cited records."
    cite = row["citations"][0] if row["citations"] else ""
    return f"{rank}. {_name(row)} - {state}: {line} [{cite}]"


def portfolio_text(portfolio: Mapping[str, Any], *, scoped: bool = False) -> str:
    """plx.find portfolio mode's readable answer (``scoped``: asked within filters)."""
    p = portfolio
    states = Counter(r["state"] for r in p["rows"])
    counts = ", ".join(f"{n} {_STATE_WORDS[s]}" for s, n in sorted(states.items()))
    over = (f"every {p['object_type']} you may list within the asked filters" if scoped
            else f"every {p['object_type']} you may list")
    head = [f"{p['plan']} ({p['family']}) over {over}: "
            f"{p['in_scope']} listed, {p['evaluated']} judged, {p['not_evaluated']} not "
            f"judged" + (f" ({', '.join(r['reason'] for r in p['not_evaluated_reasons'])})"
                           if p["not_evaluated_reasons"] else "")
            + (f", {p['withheld']} withheld (nothing you may cite)" if p["withheld"] else "")
            + (", more past the cap" if p["more"] else "") + ".",
            f"Rows: {p['answered']} with a reason, {p['missing']} naming missing evidence"
            + (f"; shown: {counts}" if counts else "") + ".",
            "Ranked:" if p["rows"] else "No row you may cite."]
    items = [_row_line(r["rank"], r) for r in p["rows"]]
    return _fit(head, items, [SOURCE_NOTE], p["answered"] + p["missing"])


def verdict_text(found: Mapping[str, Any]) -> str:
    """plx.investigate late_po's readable answer: the record's verdict, cited."""
    if found.get("status") == "verdict_failed":
        return (f"No lateness verdict: reading it failed ({found['reason']}); the "
                "structured result is the run as returned. " + SOURCE_NOTE)
    if found.get("status") != "read":
        return ("No lateness verdict: no record you may read answers this root. "
                + SOURCE_NOTE)
    type_label = _safe(found.get("type_label")) or "record"
    name = bare_label(found.get("label"), found.get("object_id")) or "The record"
    state = _STATE_WORDS.get(found["state"], found["state"])
    if found.get("days_late"):
        state += f", {found['days_late']} days"
    if found.get("missing"):
        state += f" (missing: {', '.join(found['missing'])})"
    line = _line(found.get("line")) or "See the cited records."
    cites = _handles([*(found.get("citations") or ()), *(found.get("label_citations") or ())])
    head = [f"{name} ({type_label}): {state}.", line,
            f"Verdict as of {found['as_of']}, the same as its status card.",
            "Cited:" if cites else "Nothing you may cite stands behind it."]
    return _fit(head, [f"- {h}" for h in cites[:ROW_CITATIONS]], [SOURCE_NOTE],
                min(len(cites), ROW_CITATIONS))


def _age_words(seconds: Any) -> str:
    if type(seconds) is not int:
        return "age unknown"
    if seconds < 3600:
        return f"{seconds // 60} min old"
    if seconds < 172800:
        return f"{seconds // 3600} h old"
    return f"{seconds // 86400} days old"


def home_text(home: Mapping[str, Any], gaps: Sequence[Mapping[str, Any]] = ()) -> str:
    """plx.find home mode's readable answer (``gaps``: the answer's own gaps about the
    inbox, each said by its reason code)."""
    rules = [r for r in home["rules"] if r["status"] != "complete" or r["not_evaluated"]]
    rule_words = "; ".join(
        f"{r['rule']} {r['status']}"
        + (f" ({', '.join(r['reasons'])})" if r["reasons"] else "")
        + (f", {r['not_evaluated']} not judged" if r["not_evaluated"] else "")
        for r in rules)
    head = [f"What needs your attention (engine inbox {home['inbox']}"
            + (f", as of {home['as_of']}" if home.get("as_of") else "")
            + ("; stale" if home["stale"] else "") + f"): {home['returned']} open gap"
            + ("" if home["returned"] == 1 else "s")
            + ("" if home["complete"] else ", not a complete answer") + ".",
            "Inbox built " + (f"at {home['run_at']}, " if isinstance(home.get("run_at"), str)
                              and _STAMP.fullmatch(home["run_at"]) else "")
            + _age_words(home.get("age_seconds"))
            + ("; stale, so newer gaps are not listed" if home["stale"] else "") + ".",
            *(f"Gap: {g['reason']}." for g in gaps
              if isinstance(g.get("reason"), str) and _SNAKE.fullmatch(g["reason"])),
            "Rules not complete: " + (rule_words or "none") + "."]
    items = []
    for gap in home["gaps"]:
        # The gap's summary is source text (an envelope in the JSON): never echoed.
        on = gap.get("object_type")
        on = f" on a {on}" if isinstance(on, str) and _TYPE.fullmatch(on) else ""
        items.append(f"{gap['rank']}. [{gap['priority']}] {gap['kind']}: "
                     f"{gap['kind'].replace('_', ' ')}{on} [{gap['citations'][0]}]")
    return _fit(head, items, [SOURCE_NOTE], len(home["gaps"]))
