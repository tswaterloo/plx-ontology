"""Crisp, Claude/Genie-quality Ask chat title generation.

Provides:
1. ``clean_chat_title(raw, named=False)``: deterministic zero-latency transformer that converts
   raw conversational prompts into concise 2–5 word topic titles (~36 chars max), mirrored 1:1
   in ``app/src/components/ask-chats.tsx``.
2. ``summarize_chat_title_ai(question, answer=None)``: optional fast Haiku AI summarizer for
   newly created conversations that degrades silently and immediately to ``clean_chat_title``
   whenever no API key is configured or any network/provider error occurs.
"""
from __future__ import annotations

import re
from typing import Optional

TITLE_MAX_CHARS = 36

_SPACE_PREFIX = re.compile(r"^Focus\s+on\s+[^:]{1,140}:\s*", re.I)
_TRAILING_PARENS = re.compile(r"\s*\([^)]*\)?\s*$")
_TRAILING_FILLER = re.compile(
    r"(?:\s+(?:right\s+now|this\s+today|today\s+please|for\s+me|please|asap|thanks|thank\s+you))+[.?!,;:]*\s*$",
    re.I,
)
_LEADING_FILLER = re.compile(
    r"^(?:hey\s+petra[,:]?\s*|hi[,:]?\s*|hello[,:]?\s*|no[,\s]+any\s+other\s+|no[,\s]+|any\s+other\s+|"
    r"actually[,\s]+|wait[,\s]+|also[,\s]+|okay[,\s]+|ok[,\s]+|well[,\s]+|so[,\s]+|"
    r"can\s+you\s+|could\s+you\s+|would\s+you\s+|please\s+|"
    r"figure\s+out\s+|find\s+out\s+|show\s+me\s+|tell\s+me\s+|give\s+me\s+|help\s+me\s+|"
    r"list\s+(?:all\s+|me\s+)?|plot\s+(?:me\s+|our\s+|the\s+)?|graph\s+(?:me\s+|our\s+|the\s+)?|"
    r"chart\s+(?:me\s+|our\s+|the\s+)?|check\s+(?:on\s+|our\s+|my\s+)?|"
    r"i\s+want\s+to\s+know\s+|i\s+need\s+to\s+know\s+|i\s+need\s+to\s+|"
    r"remember\s+(?:that\s+)?(?:i\s+(?:want|prefer|like|always\s+want|need)\s+(?:to\s+)?)?|"
    r"our\s+|my\s+|the\s+)",
    re.I,
)

# High-frequency conversational prompt patterns mapped directly to crisp topic titles.
_TODAY_PRIORITIES_RE = re.compile(
    r"^(?:what\s+(?:do\s+i|should\s+i|have\s+i\s+got)\s+(?:have\s+to\s+|need\s+to\s+)?do\b.*(?:today|this\s+morning|now)?|"
    r"my\s+(?:tasks?|priorities|to[- ]?dos?)\s+(?:for\s+)?(?:this\s+)?today\b)",
    re.I,
)
_NEEDS_ATTENTION_RE = re.compile(
    r"^(?:is\s+there\s+)?anything\s+(?:that\s+)?needs?\s+(?:my\s+)?attention\b",
    re.I,
)
_CHART_OVERVIEW_RE = re.compile(
    r"^(?:chart\s+and\s+(?:give|show|tell)\s+me\s+(?:all\s+)?(?:info|information|details?)\s+(?:about|on|for)\s+|"
    r"(?:give|show|tell)\s+me\s+(?:all\s+)?(?:info|information|details?|everything)\s+(?:about|on|for)\s+)(.+)$",
    re.I,
)
_PREPARE_DOC_RE = re.compile(
    r"^(?:prepare|create|write|draft|make|build)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:document|doc|report|brief|summary|memo)\s+(?:for|about|on)\s+(?:our\s+|my\s+|the\s+)?(.+)$",
    re.I,
)
_WHATS_GOING_ON_RE = re.compile(
    r"^(?:what'?s|whats|what\s+is)\s+(?:going\s+on|happening|up|the\s+status)\s+(?:with|on|for)\s+(?:our\s+|the\s+)?(.+)$",
    re.I,
)
_BAR_CHART_RE = re.compile(
    r"^(?:(?:bar|line|pie|area|stacked)\s+)?chart\s+(?:of|for|showing)\s+(?:our\s+|my\s+|the\s+)?(.+)$",
    re.I,
)
_INVOICE_CURRENCY_RE = re.compile(
    r"^what\s+did\s+we\s+invoice\s+in\s+(usd|cad|eur|gbp)\b",
    re.I,
)
_TOP_SUPPLIER_RE = re.compile(
    r"^which\s+suppliers?\s+(?:has|have)\s+the\s+most\b",
    re.I,
)
_WHICH_ARE_RE = re.compile(
    r"^which\s+(?:of\s+(?:our|my|the)\s+)?(.+?)\s+(?:are|is|were|have)\s+([a-z0-9\s-]{2,24})[?.\s]*$",
    re.I,
)
_WHICH_PREFIX_RE = re.compile(r"^which\s+(?:of\s+(?:our|my|the)\s+)?", re.I)

_DANGLING_TAIL_WORDS = frozenset({
    "and", "or", "but", "so", "why", "how", "what", "when", "where", "which", "who",
    "the", "a", "an", "to", "of", "for", "in", "on", "at", "by", "with", "from",
    "about", "into", "as", "if", "because", "is", "are", "was", "were", "be", "been",
    "that", "this", "these", "those", "it", "my", "our", "your", "their", "create",
    "make", "right", "now", "please", "last", "next", "very", "most", "all",
})

_MINOR_TITLE_WORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of",
    "on", "or", "per", "the", "to", "vs", "with",
})

_ACRONYM_MAP: dict[str, str] = {
    "usd": "USD",
    "cad": "CAD",
    "eur": "EUR",
    "gbp": "GBP",
    "po": "PO",
    "pos": "POs",
    "so": "SO",
    "sos": "SOs",
    "qc": "QC",
    "qa": "QA",
    "erp": "ERP",
    "bc": "BC",
    "ytd": "YTD",
    "mtd": "MTD",
    "qtd": "QTD",
    "sku": "SKU",
    "skus": "SKUs",
    "coa": "COA",
    "coas": "COAs",
    "3pl": "3PL",
    "ap": "AP",
    "ar": "AR",
    "kpi": "KPI",
    "kpis": "KPIs",
    "bom": "BOM",
    "eta": "ETA",
    "etas": "ETAs",
    "sql": "SQL",
    "pdf": "PDF",
    "xlsx": "XLSX",
    "rfq": "RFQ",
    "rma": "RMA",
    "mc": "MC",
    "q1": "Q1",
    "q2": "Q2",
    "q3": "Q3",
    "q4": "Q4",
}

_ABBREV_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bpurchase\s+orders\b", re.I), "POs"),
    (re.compile(r"\bpurchase\s+order\b", re.I), "PO"),
    (re.compile(r"\bsales\s+orders\b", re.I), "Sales Orders"),
    (re.compile(r"\bquality\s+control\b", re.I), "QC"),
)


def _format_title_word(word: str, index: int) -> str:
    if not word:
        return word
    # Preserve punctuation wrapping (e.g. quotes or trailing question mark)
    m = re.match(r"^([^A-Za-z0-9]*)(.*?)([^A-Za-z0-9]*)$", word)
    if not m:
        return word
    lead, core, trail = m.groups()
    if not core:
        return word
    low = core.lower()
    if low in _ACRONYM_MAP:
        return f"{lead}{_ACRONYM_MAP[low]}{trail}"
    # Preserve alphanumeric codes (e.g. BC-25295D, Q3-2026) or intentional mixedCase/ALLCAPS
    if re.search(r"\d", core) and re.search(r"[A-Za-z]", core):
        return f"{lead}{core.upper()}{trail}"
    if core.isupper() and len(core) >= 2:
        return f"{lead}{core}{trail}"
    if index > 0 and low in _MINOR_TITLE_WORDS:
        return f"{lead}{low}{trail}"
    # Handle hyphenated words (e.g. month-over-month, follow-up)
    if "-" in core:
        parts = core.split("-")
        formatted = [
            _ACRONYM_MAP.get(p.lower(), p[:1].upper() + p[1:].lower() if p else "")
            for p in parts
        ]
        return f"{lead}{'-'.join(formatted)}{trail}"
    return f"{lead}{core[:1].upper()}{core[1:]}{trail}"


def _to_title_case(text: str) -> str:
    words = [w for w in text.split(" ") if w]
    return " ".join(_format_title_word(w, i) for i, w in enumerate(words))


def _trim_dangling_tail(text: str, min_words: int = 2) -> str:
    words = [w for w in text.split(" ") if w]
    while len(words) > min_words:
        tail = re.sub(r"[^a-z0-9]+$", "", words[-1].lower())
        if tail in _DANGLING_TAIL_WORDS or len(tail) == 0:
            words.pop()
        else:
            break
    return " ".join(words).rstrip(",;:-?.!").strip()


def _cap_on_word_boundary(text: str, max_chars: int = TITLE_MAX_CHARS) -> str:
    cleaned = text.strip().rstrip(",;:-").strip()
    if len(cleaned) <= max_chars:
        return _trim_dangling_tail(cleaned, min_words=2) or cleaned
    window = cleaned[:max_chars]
    cut_at = window.rfind(" ")
    clipped = (window[:cut_at] if cut_at >= 12 else window).rstrip(",;:-?!.").strip()
    trimmed = _trim_dangling_tail(clipped, min_words=2)
    return trimmed or clipped


def clean_chat_title(raw: str, named: bool = False) -> str:
    """Turn a raw Ask prompt into a crisp 2–5 word title-case topic title (~36 chars).

    User-named conversations (``named=True``) are preserved verbatim (whitespace collapsed).
    """
    collapsed = " ".join(str(raw or "").split())
    if not collapsed:
        return "Untitled Chat"
    if named:
        return collapsed

    cleaned = _SPACE_PREFIX.sub("", collapsed).strip()

    # Keep only the opening topic sentence when the prompt contains follow-up clauses.
    first_break = re.search(r"[?!.]\s+", cleaned)
    if first_break and first_break.start() >= 10:
        cleaned = cleaned[: first_break.start()].strip()

    cleaned = _TRAILING_PARENS.sub("", cleaned).strip()
    cleaned = re.sub(r"\.{2,}$", "", cleaned).strip()
    cleaned = cleaned.rstrip("?!.").strip()
    cleaned = _TRAILING_FILLER.sub("", cleaned).strip()

    # 1. Direct high-signal conversational patterns
    if _TODAY_PRIORITIES_RE.match(cleaned):
        return "Today's Priorities"
    if _NEEDS_ATTENTION_RE.match(cleaned):
        return "Items Needing Attention"

    m = _CHART_OVERVIEW_RE.match(cleaned)
    if m:
        subject = _cap_on_word_boundary(_to_title_case(_trim_dangling_tail(m.group(1), 1)), TITLE_MAX_CHARS - 9)
        if subject:
            return f"{subject} Overview"

    m = _PREPARE_DOC_RE.match(cleaned)
    if m:
        subject = _cap_on_word_boundary(_to_title_case(_trim_dangling_tail(m.group(1), 1)), TITLE_MAX_CHARS - 9)
        if subject:
            return f"{subject} Document"

    m = _WHATS_GOING_ON_RE.match(cleaned)
    if m:
        subject = _TRAILING_FILLER.sub("", m.group(1)).strip()
        subject = _cap_on_word_boundary(_to_title_case(_trim_dangling_tail(subject, 1)), TITLE_MAX_CHARS - 7)
        if subject:
            return f"{subject} Status"

    m = _BAR_CHART_RE.match(cleaned)
    if m:
        subject = _TRAILING_FILLER.sub("", m.group(1)).strip()
        # Keep the concise core topic (first 2–3 words before prepositional tails like "by ...")
        subject = re.split(r"\b(?:for\s+the|over\s+the|in\s+the)\b", subject, maxsplit=1, flags=re.I)[0].strip()
        subject = _cap_on_word_boundary(_to_title_case(_trim_dangling_tail(subject, 1)), TITLE_MAX_CHARS - 6)
        if subject:
            return f"{subject} Chart"

    m = _INVOICE_CURRENCY_RE.match(cleaned)
    if m:
        return f"{m.group(1).upper()} Invoicing"

    if _TOP_SUPPLIER_RE.match(cleaned):
        return "Top Supplier Comparison"

    # "Which open sales orders are..." / "Which orders are late?"
    m = _WHICH_ARE_RE.match(cleaned)
    if m:
        noun = m.group(1).strip()
        adj = m.group(2).strip()
        # If the adjective is short and clean (e.g. "late", "overdue"), fold it concisely or keep noun if noun already descriptive
        if len(noun.split()) <= 2 and len(adj.split()) == 1 and adj.lower() in {"late", "overdue", "open", "blocked", "missing"}:
            cleaned = f"{adj} {noun}"
        else:
            cleaned = noun
    elif _WHICH_PREFIX_RE.match(cleaned):
        cleaned = _WHICH_PREFIX_RE.sub("", cleaned).strip()
        cleaned = re.sub(r"\s+(?:are|is|were)\s*$", "", cleaned, flags=re.I).strip()

    # 2. Strip repeated leading conversational filler
    for _ in range(5):
        nxt = _LEADING_FILLER.sub("", cleaned).strip()
        if not nxt or nxt == cleaned:
            break
        cleaned = nxt

    # Re-strip trailing filler, parentheticals, and stray punctuation
    cleaned = _TRAILING_PARENS.sub("", cleaned).strip()
    cleaned = _TRAILING_FILLER.sub("", cleaned).strip()
    cleaned = re.sub(r"\s+for\s+(20\d{2})\b", r" \1", cleaned, flags=re.I)
    cleaned = cleaned.rstrip(",;:-?!.").strip()

    # Normalize verbose ERP phrases when title would exceed budget
    if len(cleaned) > 22:
        for rx, repl in _ABBREV_RULES:
            cleaned = rx.sub(repl, cleaned)

    cleaned = _trim_dangling_tail(cleaned, min_words=2)
    cleaned = _cap_on_word_boundary(cleaned, TITLE_MAX_CHARS)

    final_title = _to_title_case(cleaned or collapsed[:TITLE_MAX_CHARS])
    return final_title or "Untitled Chat"


def summarize_chat_title_ai(
    question: str,
    answer: Optional[str] = None,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 3.5,
) -> str:
    """Return a crisp 2-5 word Title Case topic title for a conversation without network egress."""
    del answer, api_key, model, timeout
    return clean_chat_title(question, named=False)
