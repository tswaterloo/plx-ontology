"""The agent answer's words as a business reader sees them: a deterministic presentation
layer over what the answer gate already passed.

It only removes or relabels, never adds a figure or a fact:

* a sentence about how the answer was checked (the answer gate, claims it passed or
  refused, null values, field paths, citation handles: ``INTERNAL``) is removed;
* a field path (``row.description``, `` `row.status` ``) is relabelled in words, and one
  in parentheses beside a statement is removed;
* a citation handle (``original://nr-...``) is relabelled as the kind of record it names,
  and one in parentheses is removed;
* a line's ``text:`` / ``value:`` prefix is removed and a ``snake_case:`` prefix (a value
  or gap name) is relabelled in words; a snake_case code in prose becomes its words.

A gated fact (``fact_text``) keeps its checked phrase byte-identical: only the label
before it changes (``description`` -> ``PO line description``; ``text`` -> none).
Deterministic; reads nothing.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional
from urllib.parse import unquote, urlsplit

# A sentence that talks about the checking machinery rather than the business: only
# phrases no business sentence uses ("the gate" alone is a yard gate, "claim" beside
# "accepted" a freight claim, "null and void" a contract term).
INTERNAL = re.compile(
    r"\banswer gate\b|\bvalue claims?\b|\bclaim_\w+|\bnull values?\b|\bfield[ _]paths?\b"
    r"|\bcitation handles?\b|\bl1_handle\b|\bmy answer values?\b|\banswer block\b"
    r"|\bthe gate (?:\w+ ){0,2}(?:refused|passed|rejected|accepted|pass|accept)\b"
    r"[^.\n]*\bmy (?:claims?|quotes?|values?|answer)\b"
    r"|\bleft (?:it|that value|the value|them|that|this value) (?:as )?null\b", re.I)
SCHEMES = ("original", "bc", "api", "graph-mail", "mail", "sharepoint", "sharepoint-list",
           "teams-chat", "teams-channel", "teams", "mc", "repo", "fm")
HANDLE = re.compile(r"`?\b(?:" + "|".join(re.escape(s) for s in SCHEMES)
                    + r")://[^\s)`,;\]]*[^\s)`,;\].:!?]`?")
FIELD_PATH = re.compile(r"`?\b(?:row|fields|record|custom_fields)\.[A-Za-z_][\w.]*`?"
                        r"|`(?:text|row)`")
# A parenthesis holding only field paths and handles (and separators).
BARE_PAREN = re.compile(r"\s*\((?:\s*(?:" + FIELD_PATH.pattern + "|" + HANDLE.pattern
                        + r"|on|in|at|from|field|page \d+|#page=\d+)\s*[,;/]?)+\)")
LINE_PREFIX = re.compile(r"^(\s*(?:[-*]\s+)?)(?:\*\*)?([a-z][a-z0-9]*(?:_[a-z0-9]+)*)"
                         r"(?:\*\*)?:\s+", re.M)
SNAKE = re.compile(r"`?\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b`?")
DROPPED_LABELS = frozenset({"text", "value", "field", "quote"})
# A table or collection name's words for a record kind (as the business says it).
RECORD_WORDS = (("purchase order", "PO"), ("sales order", "sales order"))


def words(code: str) -> str:
    """``map_coverage`` -> ``map coverage``; ``lineObjectNumber`` -> ``line object number``."""
    code = code.strip("`").rsplit(".", 1)[-1]
    code = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", code)
    return re.sub(r"[_\s]+", " ", code).strip().lower()


def record_kind(handle: str) -> Optional[str]:
    """The kind of record an L1 handle names, in words (``PO line`` for a BC
    ``purchaseOrderLines`` row or a Portal ``app_purchase_order_line``); None when the
    handle names no table (a document, a message)."""
    if not isinstance(handle, str) or "://" not in handle:
        return None
    parts = urlsplit(handle)
    if parts.scheme == "bc":
        segments = [s for s in parts.path.split("/") if s]   # <company>/<entity set>/<id>
        table = segments[1] if len(segments) > 1 else ""
    elif parts.scheme == "api":
        table = unquote(parts.netloc).split(".")[-1]
        table = re.sub(r"^app_", "", table)
    else:
        return None
    said = words(table)
    if said.endswith("s") and not said.endswith("ss"):
        said = said[:-1]
    for long, short in RECORD_WORDS:
        said = said.replace(long, short)
    return said or None


def handle_words(handle: str) -> str:
    kind = record_kind(handle.strip("`"))
    if kind:
        return f"the {kind} record"
    return "the cited document" if handle.strip("`").startswith(
        ("original", "sharepoint", "repo")) else "the cited message" if handle.strip(
        "`").startswith(("graph-mail", "mail", "teams")) else "the cited record"


def _relabel_prefix(match: re.Match) -> str:
    lead, code = match.group(1), match.group(2)
    if code in DROPPED_LABELS:
        return lead
    if "_" not in code:
        return match.group(0)        # a plain word label ("Status:") is the reader's own
    said = words(code)
    return f"{lead}{said[:1].upper()}{said[1:]}: "


def _plain_words(text: str, keep: frozenset[str] = frozenset()) -> str:
    """Field paths, handles and codes relabelled; bare parentheses of them removed. A
    snake_case token in ``keep`` (one a checked value holds) stays as written."""
    text = BARE_PAREN.sub("", text)
    text = HANDLE.sub(lambda m: handle_words(m.group(0)), text)
    text = FIELD_PATH.sub(lambda m: words(m.group(0).strip("`").replace("row.", "")), text)
    text = LINE_PREFIX.sub(_relabel_prefix, text)
    return _outside_quotes(text, lambda part: SNAKE.sub(
        lambda m: m.group(0) if m.group(0).strip("`") in keep else words(m.group(0)), part))


def _outside_quotes(text: str, change: Any) -> str:
    """``change`` applied to the parts of ``text`` outside double quotes (a quoted
    passage stays as written)."""
    pieces = re.split(r'("[^"\n]*")', text)
    return "".join(piece if index % 2 else change(piece) for index, piece in enumerate(pieces))


def reader_prose(text: str, values: Any = ()) -> tuple[str, int]:
    """(the prose for a business reader, how many sentences were removed). Removes each
    sentence or line that talks about the check (``INTERNAL``), then relabels what is
    left (module doc). A paragraph or list item left empty goes too. ``values``: the
    checked phrases the answer states (a snake_case token one of them holds is kept)."""
    if not isinstance(text, str) or not text.strip():
        return "", 0
    kept, removed = without(text, INTERNAL, values)
    keep = frozenset(token for value in values if isinstance(value, str)
                     for token in SNAKE.findall(value))
    out = tidy(_plain_words(kept, keep))
    return out, removed


def tidy(out: str) -> str:
    """Lines left as only a bullet or a bold label go; blank runs fold to one."""
    lines = [line.rstrip() for line in out.splitlines()]
    lines = [line for line in lines if not re.fullmatch(r"\s*(?:[-*]|\d+[.)])?\s*(?:\*\*[^*]*"
                                                        r"\*\*:?)?\s*", line) or not line]
    out = "\n".join(lines)
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out.strip()


def carries(sentence: str, values: Any) -> bool:
    """Whether ``sentence`` states a checked phrase, or a figure one of them holds."""
    phrases = [v for v in values if isinstance(v, str) and v.strip()]
    if any(v in sentence for v in phrases):
        return True
    figures = {n for v in phrases for n in re.findall(r"\d+(?:[.,]\d+)*", v)}
    return any(n in figures for n in re.findall(r"\d+(?:[.,]\d+)*", sentence))


def without(text: str, pattern: "re.Pattern[str]", values: Any = ()) -> tuple[str, int]:
    """``text`` without each sentence, list item or line ``pattern`` finds (the answer
    gate's own sentence split, so layout stays), and how many went. A sentence that
    states a checked phrase or figure (``values``, ``carries``) is never removed."""
    from operating.answer_gate import _sentences

    kept: list[str] = []
    removed = 0
    values = list(values or ())
    for sentence, gap in _sentences(text or ""):
        if sentence.strip() and pattern.search(sentence) and not carries(sentence, values):
            removed += 1
            if "\n" in gap:
                kept.append(gap)
            continue
        kept.append(sentence + gap)
    return "".join(kept), removed


# A source family's code as a gap's subject names it, in the business's words.
SOURCE_NAMES = {"bc": "Business Central", "portal api": "the Portal", "portal": "the Portal",
                "graph mail": "email", "mail": "email", "sharepoint": "SharePoint",
                "teams": "Teams", "mc": "Mission Control", "mission control":
                "Mission Control"}


def first_sentence(text: str, pattern: "re.Pattern[str]") -> Optional[str]:
    """The first sentence of ``text`` (the answer gate's split) ``pattern`` finds."""
    from operating.answer_gate import _sentences

    return next((sentence for sentence, _gap in _sentences(text or "")
                 if sentence.strip() and pattern.search(sentence)), None)


def plain_words(text: str) -> str:
    """A short label (a gap's subject or reason) with field paths, handles and codes in
    words; a bare source code (``bc``, ``graph mail``) as the source's name."""
    said = " ".join(_plain_words(text or "").split())
    typed = re.fullmatch(r"([A-Z][A-Za-z]+)\.([A-Za-z][\w ]*)", said)
    if typed:   # PurchaseOrder.expected date -> PO expected date
        kind = words(typed.group(1))
        for long, short in RECORD_WORDS:
            kind = kind.replace(long, short)
        said = f"{kind[:1].upper()}{kind[1:]} {words(typed.group(2))}"
    return SOURCE_NAMES.get(said.lower(), said)


def fact_text(text: str, claim: Mapping[str, Any]) -> str:
    """A gated claim's line (``<label>: <phrase>``) with its label in a reader's words;
    the phrase (what the gate checked) is kept byte-identical. A ``text`` label is
    dropped; a bare column label gets its record's kind (``description`` of a PO line
    -> ``PO line description``); a snake_case label becomes words."""
    phrase = claim.get("phrase")
    if not isinstance(text, str) or not isinstance(phrase, str) or not phrase \
            or not text.endswith(": " + phrase):
        return text
    label = text[:-len(phrase) - 2]
    if label.strip().lower() in DROPPED_LABELS:
        return phrase
    value = next((v for v in claim.get("value_claims") or () if isinstance(v, Mapping)), {})
    field = value.get("field") if isinstance(value.get("field"), str) else ""
    citation = value.get("citation") if isinstance(value.get("citation"), str) else (
        (claim.get("citations") or [None])[0])
    said = words(label) if "_" in label else label
    if field and words(field) == said.lower() and not value.get("role"):
        kind = record_kind(citation or "")
        if kind and not said.lower().startswith(kind.lower()):
            said = f"{kind} {said}"
    said = said[:1].upper() + said[1:]
    return f"{said}: {phrase}"
