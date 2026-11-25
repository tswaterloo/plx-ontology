"""Plain-English streamed plan lines for Ask (Ask spec 19 S5, A3): first words from the
question immediately when Ask starts, and one combined plain-English plan line from each
round of tool calls ("Reading PO100523, receipts and mail for 'Croda'")."""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

LOG = logging.getLogger("operator_api.ask")

_READS = re.compile(
    r'\b(?:from|join)\s+(?:"?[A-Za-z_]\w*"?\s*\.\s*)?"?([A-Za-z_]\w*)"?(?!\s*\()', re.I)

_SOURCE_LABELS = {  # guard-ok: display labels for plx.search source enum values
    "graph_mail": "mail",
    "teams": "Teams",
    "sharepoint": "documents",
    "original": "documents",
    "records": "records",
}

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX_ID_RE = re.compile(r"\b[0-9a-f]{24,}\b", re.I)
_TYPED_OPAQUE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*:[A-Za-z0-9_.-]+:\{[^}]*"
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24,})[^}]*\}",
    re.I,
)
_QUOTED_UUID_RE = re.compile(
    r'["\u201c\']?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}["\u201d\']?',
    re.I,
)
_QUOTED_HEX_RE = re.compile(r'["\u201c\']?[0-9a-f]{24,}["\u201d\']?', re.I)

_TYPE_LABELS = {  # guard-ok: display labels for typed ontology object prefixes
    "PurchaseOrder": "that purchase order",
    "SalesOrder": "that sales order",
    "SalesInvoice": "that invoice",
    "PurchaseInvoice": "that invoice",
    "Customer": "that customer",
    "Supplier": "that supplier",
    "Vendor": "that supplier",
    "Product": "that product",
    "Item": "that item",
    "Batch": "that batch",
    "Lot": "that batch",
    "Task": "that task",
    "MCTask": "that task",
    "Document": "that document",
    "Email": "that message",
    "Person": "that person",
}


def is_opaque_id(value: str) -> bool:
    """True when ``value`` contains a raw UUID, long hex hash, or JSON key object rather than
    a human-readable record name."""
    text = str(value or "").strip()
    if not text:
        return False
    if _UUID_RE.search(text) or _HEX_ID_RE.search(text):
        return True
    if ("{" in text and "}" in text) or '":"' in text or '"id"' in text:
        return True
    return False


def safe_record_label(raw: Any, *, fallback: str = "that record") -> str:
    """Human-readable label for a record reference, never exposing raw UUIDs or JSON keys."""
    text = str(raw or "").strip()
    if not text or "://" in text:
        return ""
    prefix = text.split(":", 1)[0].strip() if ":" in text else ""
    if is_opaque_id(text):
        return _TYPE_LABELS.get(prefix, fallback)
    tail = text.split(":")[-1].strip().strip('"\'{} ')
    if not tail or is_opaque_id(tail):
        return _TYPE_LABELS.get(prefix, fallback)
    return tail


def scrub_uuids(text: str, replacement: str = "that record") -> str:
    """Replace any raw UUID, long hex hash, or typed JSON-UUID reference in a progress line."""
    raw = str(text or "")
    if not raw:
        return raw
    if not (_UUID_RE.search(raw) or _HEX_ID_RE.search(raw)):
        return raw
    out = _TYPED_OPAQUE_RE.sub(replacement, raw)
    out = _QUOTED_UUID_RE.sub(replacement, out)
    out = _QUOTED_HEX_RE.sub(replacement, out)
    return " ".join(out.split())


def _join_words(items: Sequence[str]) -> str:
    kept = [x.strip() for x in items if isinstance(x, str) and x.strip()]
    uniq = list(dict.fromkeys(kept))
    if not uniq:
        return ""
    if len(uniq) == 1:
        return uniq[0]
    return ", ".join(uniq[:-1]) + " and " + uniq[-1]


def _view_phrase(names: Iterable[str]) -> str:
    words = []
    for name in names:
        said = re.sub(r"\bteams\b", "Teams", str(name).replace("_", " ")).strip()
        if said and said not in words:
            words.append(said)
    if len(words) > 3:
        words = [*words[:3], "more"]  # guard-ok: progress label shows first three views plus "and more"
    return _join_words(words)


def _short_query(raw: str) -> str:
    words = " ".join(raw.split())
    if len(words) > 50:
        return words[:49].rsplit(" ", 1)[0] + "\u2026"  # guard-ok: progress display ellipsis only; full query runs uncut
    return words


def initial_plan(question: str, *, record: Optional[str] = None) -> str:
    """Immediate first-words plan emitted at 0 ms before the model starts: names the
    records or entities mentioned in the question (from ``entity_resolution.mentions``).
    No model call, no keyword rules."""
    parts: list[str] = []
    opaque_fallback = ""
    if record:
        raw_rec = str(record).strip()
        if "://" not in raw_rec:
            if is_opaque_id(raw_rec):
                opaque_fallback = safe_record_label(raw_rec, fallback="that record")
            else:
                label = safe_record_label(raw_rec, fallback="")
                if label:
                    parts.append(label)
    text = str(question or "").strip()
    if text:
        try:
            from operating import entity_resolution
            for m in entity_resolution.mentions(text):
                said = str(m.get("text") or "").strip()
                if said and not is_opaque_id(said) and said not in parts:
                    parts.append(said)
        except Exception:  # noqa: BLE001 - fallback to plain wording, never breaks Ask
            LOG.warning("ask_plan: mention extraction failed", exc_info=True)
    if not parts and opaque_fallback:
        parts.append(opaque_fallback)
    if parts:
        return f"Reading {_join_words(parts)}"
    return "Checking the business records"


def call_phrase(tool: str, arguments: Any) -> Optional[str]:
    """One short noun phrase for a single tool call, suitable for joining into a round's
    plan ("Reading PO100523, purchase orders and mail for 'Croda'")."""
    args = arguments if isinstance(arguments, Mapping) else {}
    if tool == "plx.sql":
        if isinstance(args.get("record"), str) and args["record"].strip():
            return safe_record_label(args["record"], fallback="a record") or "a record"
        if isinstance(args.get("chain"), str) and args["chain"].strip():
            label = safe_record_label(args["chain"], fallback="that record") or "that record"
            return f"{label} and its linked records"
        if isinstance(args.get("names"), str) and args["names"].strip():
            raw_names = args["names"].strip()
            if is_opaque_id(raw_names):
                return "a record"
            return f"what \u201c{raw_names}\u201d names"
        if isinstance(args.get("measure"), str) and args["measure"].strip():
            return args["measure"].strip().replace("_", " ")
        if isinstance(args.get("sql"), str):
            from operating.pg import business_views as bv
            names = [n.lower() for n in _READS.findall(args["sql"]) if n.lower() in bv.VIEWS]
            if names:
                return _view_phrase(names)
            return "business records"
        if args.get("describe"):
            return "the record catalog"
        if args.get("page"):
            return "more rows"
        return "business records"
    if tool == "plx.search":
        sources = args.get("sources")
        if isinstance(sources, list) and sources:
            where = _join_words([_SOURCE_LABELS.get(str(s), str(s)) for s in sources])
        else:
            where = "mail, Teams and documents"
        q = args.get("query")
        if isinstance(q, str) and q.strip():
            cleaned = scrub_uuids(_short_query(q), replacement="that record")
            if cleaned == "that record":
                return where
            return f"{where} for \u201c{cleaned}\u201d"
        return where
    if tool == "plx.open":
        return "messages and documents"
    if tool == "plx.read_document":
        return "the document"
    if tool == "plx.my_open_work":
        return "your open work"
    if tool == "plx.my_work":
        return "your work log"
    if tool == "plx.insight":
        return "Insights"
    if tool == "plx.legs":
        legs = args.get("legs")
        if isinstance(legs, list):
            parts = [scrub_uuids(str(l.get("part") or "").strip())
                     for l in legs if isinstance(l, Mapping)]
            joined = _join_words(parts)
            return joined or None
        return None
    return None


def calls_plan(calls: Sequence[tuple[str, Any]]) -> Optional[str]:
    """Combine a round of tool calls into one plain-English plan line, e.g.
    ``Reading PO100523, purchase orders and mail for "Croda"``."""
    reads = [(t, a) for t, a in calls if t != "plx.legs"]
    if reads:
        phrases = [p for t, a in reads if (p := call_phrase(t, a))]
        joined = _join_words(phrases)
        return f"Reading {joined}" if joined else None
    legs = [(t, a) for t, a in calls if t == "plx.legs"]
    if legs:
        joined = call_phrase("plx.legs", legs[0][1])
        return f"Checking {joined}" if joined else None
    return None
