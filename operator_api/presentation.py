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
    if said.endswith("ies"):
        said = said[:-3] + "y"           # "item ledger entries" -> "item ledger entry"
    elif said.endswith("s") and not said.endswith("ss"):
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


# ---- a record's fields as a business reader sees them -----------------------------------

# Systems a property name may start with (``bc_fully_received``): said after the field.
SYSTEM_PREFIXES = {"bc": "BC", "portal": "Portal", "fm": "FileMaker", "sage": "Sage"}
# Short words a field name spells in capitals.
ACRONYMS = {"po": "PO", "so": "SO", "ao": "AO", "eta": "ETA", "uom": "UOM", "gl": "GL",
            "sku": "SKU", "id": "ID", "url": "URL", "coa": "COA", "upc": "UPC"}
# Fields that are keys, hashes or plumbing, never business facts.
INTERNAL_FIELD = re.compile(
    r"(?:^|[._])(?:id|uuid|guid|etag|sha256|content_sha256|digest|change_key|row_version|"
    r"system_id|odata_etag|external_id|binding|lineage|native_key)$"
    r"|^(?:external|internal|system|record)_?id$", re.I)
# A property holding an amount of money (shown with the record's currency).
MONEY_FIELD = re.compile(r"(?:^|_)(?:cost|costs|amount|total|price|subtotal|balance|"
                         r"value|revenue|margin)(?:_|$)", re.I)


NOT_MONEY = re.compile(r"qty|quantit|count|boxes|units|weight|percent|pct|rate|days|lines",
                       re.I)


def internal_field(name: str) -> bool:
    """Whether a property is a key or plumbing (an id, a hash) rather than a fact."""
    return bool(INTERNAL_FIELD.search(str(name or "").strip()))


def field_label(name: str) -> str:
    """``bc_fully_received`` -> ``Fully received (BC)``; ``po_number`` -> ``PO number``;
    ``vendorName`` -> ``Vendor name``."""
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name or "").rsplit(".", 1)[-1])
    parts = [p for p in re.split(r"[_\s]+", raw.lower()) if p]
    system = None
    if len(parts) > 1 and parts[0] in SYSTEM_PREFIXES:
        system = SYSTEM_PREFIXES[parts.pop(0)]
    said = " ".join(ACRONYMS.get(p, p) for p in parts) or "Value"
    said = said[:1].upper() + said[1:]
    return f"{said} ({system})" if system else said


def record_currency(values: Any) -> Optional[str]:
    """The one currency a record's values state (the Portal's ``CAD$`` is ``CAD``), else
    None (none stated, or two)."""
    from ontology.units import currency_code

    codes = set()
    for value in values or ():
        if isinstance(value, Mapping) and str(value.get("property") or "").lower() in (
                "currency", "currency_code"):
            raw = value.get("value")
            raw = raw.get("untrusted_text") if isinstance(raw, Mapping) else raw
            code = currency_code(raw)[0] if isinstance(raw, str) else None
            if code:
                codes.add(code)
    return codes.pop() if len(codes) == 1 else None


def value_words(name: str, shown: str, currency: Optional[str]) -> str:
    """A record's value as said beside its label: a currency spelling as its code
    (``CAD$`` -> ``CAD``), an amount of money with the record's currency, an ISO date as
    a date, ``_x0020_`` as the space it encodes."""
    from ontology.units import currency_code

    text = str(shown).replace("_x0020_", " ").strip()
    key = str(name or "").lower()
    if key in ("currency", "currency_code"):
        return currency_code(text)[0] or text
    if currency and MONEY_FIELD.search(key) and not NOT_MONEY.search(key) and re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return f"{money_words(text)} {currency}"
    return date_words(text)


def money_words(amount: str) -> str:
    """``9680`` -> ``9,680.00``; ``0.4491`` keeps its places (never a float)."""
    neg = amount.startswith("-")
    whole, _, frac = amount.lstrip("-").partition(".")
    grouped = re.sub(r"\B(?=(\d{3})+(?!\d))", ",", whole.lstrip("0") or "0")
    frac = (frac + "00")[:max(2, len(frac))]
    return f"{'-' if neg else ''}{grouped}.{frac}"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
           "Dec")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                      r"(?:Z|[+-]\d{2}:?\d{2})?)?\b")


def date_words(text: str) -> str:
    """Every ISO date or timestamp in ``text`` as ``Jul 10, 2026`` (the day the ISO text
    states; a timestamp's time is not shown)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def said(found: re.Match) -> str:
        whole = found.group(0)
        year, month, day = int(found.group(1)), int(found.group(2)), int(found.group(3))
        if not 1 <= month <= 12:
            return whole
        if len(whole) > 10 and re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", whole):
            try:   # a timestamp is the Toronto day it falls on
                moment = datetime.fromisoformat(whole.replace("Z", "+00:00")).astimezone(
                    ZoneInfo("America/Toronto"))
                year, month, day = moment.year, moment.month, moment.day
            except ValueError:
                pass
        return f"{_MONTHS[month - 1]} {day}, {year}"
    return ISO_DATE.sub(said, text)


def record_label(display: Optional[str], object_type: str, object_id: str) -> str:
    """A related record's label: its display, else its kind in words, never its key
    (``PurchaseOrderLine:bc.purchase_order_lines:{...}``)."""
    if display and display.strip() and display.strip() != object_id:
        return display.strip()
    kind = words(re.sub(r"(?<=[a-z])(?=[A-Z])", "_", object_type or "Record"))
    for long, short in RECORD_WORDS:
        kind = kind.replace(long, short)
    return (kind[:1].upper() + kind[1:]) if kind else "Record"


# ---- the last pass over what the app shows ------------------------------------------------

# A record or gap code a reader should never see, in plain words.
CODE_WORDS = {"LINE_NOT_INVOICED": "line not invoiced",
              "CLAIM_CHECK_FAILED": "figures not confirmed against the records",
              "COST_NOT_VALUED": "cost not valued in BC",
              "COST_NOT_YET_ACTUAL": "cost not yet final in BC",
              "ORDER_MARGIN_NOT_READ": "margin could not be read",
              "ORDER_LINES_INCOMPLETE": "order lines not all read",
              "CITED_VALUE_UNRESOLVED": "a figure did not match its record"}
PHRASES = ((re.compile(r"\bclaim check failed\b", re.I),
            "figures not confirmed against the records"),
           (re.compile(r"\bcheck(?:ed)? claims?\b", re.I), "checked figures"))
XML_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")
CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z]{2,})+\b")   # letters only: never an item code
# "S-ORD101102: LINE_NOT_INVOICED: no posted invoice ..." : the code before its own words
CODE_LABEL = re.compile(r"\b[A-Z]{2,}(?:_[A-Z]{2,})+:\s+(?=\w)")
# Keys whose strings are identities, links, enums, source text or checked values: never
# reworded.
KEEP_KEYS = frozenset({"handle", "open_url", "object_id", "answer_id", "conversation_id",
                       "question", "type", "kind", "state", "check", "system", "as_of", "at",
                       "date", "amount", "currency", "outcome", "quote", "value", "event_type",
                       "id", "rule", "intent", "step", "capture", "evidence", "first_seen",
                       "plan", "contact_source", "margin_pct", "sources"})


# A binary float's noise in a figure (``116.25000000000001``, ``18.599999999999998``): 7 or
# more decimals that sit within a billionth of a figure of at most 4.
FLOAT_NOISE = re.compile(r"(?<![\w.])(-?\d+\.\d{7,})(?![\w.])")


def without_float_noise(text: str) -> str:
    """Each figure carrying a float's noise written as the figure it is (``116.25``),
    the fewest decimals (at most 4) within a billionth of it; a figure that really has
    more places stays as written. The answer gate backs the rounded figure
    (``claim_checks.rounded_from``)."""
    from decimal import Decimal, InvalidOperation

    def tidy(found: re.Match) -> str:
        try:
            value = Decimal(found.group(1))
        except InvalidOperation:
            return found.group(0)
        slack = Decimal("1e-9") * max(Decimal(1), abs(value))
        for places in range(0, 5):
            near = value.quantize(Decimal(1).scaleb(-places))
            if abs(near - value) <= slack:
                return format(near, "f")
        return found.group(0)
    return FLOAT_NOISE.sub(tidy, text)


def shown_words(text: str) -> str:
    """A shown sentence without codes or escapes: ``_x0020_`` is a space, a code labelling
    its own words goes, any other code reads as words (``CODE_WORDS``)."""
    if not isinstance(text, str) or not text:
        return text
    out = XML_ESCAPE.sub(lambda f: chr(int(f.group(1), 16)), text)
    out = without_float_noise(out)
    out = CODE_LABEL.sub("", out)
    out = CODE.sub(lambda f: CODE_WORDS.get(f.group(0), f.group(0).replace("_", " ").lower())
                   if "_" in f.group(0) else f.group(0), out)
    for pattern, said in PHRASES:
        out = pattern.sub(said, out)
    return out


def polished(model: Any) -> Any:
    """A response model with every reader-facing sentence through ``shown_words``
    (``KEEP_KEYS`` untouched)."""
    def walk(value: Any, key: str = "") -> Any:
        if key in KEEP_KEYS:
            return value
        if isinstance(value, str):
            return shown_words(value)
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        return value
    data = model.model_dump()
    return type(model).model_validate(walk(data))


def _listed(items: list[str]) -> str:
    items = [i.strip() for i in items if i.strip()]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] \
        if items else ""


def _either(items: list[str]) -> str:
    """"a", "a or b", "a, b or c"."""
    items = [i.strip() for i in items if i.strip()]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1] \
        if items else ""


# What a record held but short of its step has not done yet, by its kind: an order is
# issued, a receipt or an invoice is posted, a shipment ships.
HELD_WORDS = ((re.compile(r"\border\b", re.I), "not issued yet"),
              (re.compile(r"receipt|invoice|posting|credit|payment|journal", re.I),
               "not posted yet"),
              (re.compile(r"shipment|delivery", re.I), "not shipped yet"))


def held_words(stage: str) -> str:
    """"not issued yet" (an order), "not posted yet" (a receipt, an invoice), "not shipped
    yet" (a shipment), else "not at this step yet"."""
    return next((said for rule, said in HELD_WORDS if rule.search(stage or "")),
                "not at this step yet")


def status_words(text: str) -> str:
    """A record's process verdict ("Missing: supplier confirmation, receipt; held, its
    status not reached yet: purchase order; not established: shipment; 1 more step not
    visible to you") in plain sentences ("No supplier confirmation or receipt on record
    yet. The purchase order is on record but not issued yet. ..."). Any other text is
    returned as it is."""
    if not isinstance(text, str) or not re.match(
            r"^(?:Missing|Not established|Held, its status not reached yet): ", text):
        return text
    said = []
    for part in text.split("; "):
        head, _, rest = part.partition(": ")
        head = head.strip().lower()
        items = [i for i in rest.split(", ") if i.strip()]
        if head == "missing" and items:
            said.append(f"No {_either(items)} on record yet.")
        elif head.startswith("held, its status not reached yet") and items:
            by: dict[str, list[str]] = {}
            for item in items:
                by.setdefault(held_words(item), []).append(item)
            said += [f"On record but {words}: {_listed(group)}." for words, group in by.items()]
        elif head == "not established" and items:
            # "vendor shipped" is an event: whether it happened is what is not known
            said.append(f"Not known yet whether the {_either(items)}."
                        if all(re.search(r"\w+ed$", i.strip()) for i in items)
                        else f"Not known yet: {_listed(items)}.")
        elif re.fullmatch(r"\d+ more steps? not visible to you", part.strip()):
            n = int(part.split()[0])
            said.append(f"{n} more step{'s are' if n != 1 else ' is'} in places you can't "
                        "open.")
        else:
            said.append(part.strip().rstrip(".") + ".")
    return " ".join(said)


# ---- what to ask next ----------------------------------------------------------------------

# A gap whose words name no business fact (the reader's own question, a source's state) is
# no question to ask next.
GENERIC_GAP = re.compile(r"^(?:a value the question asks for|part of what you asked|some "
                         r"records|projection|store|index|mirror|readings?|records?)$", re.I)
SOURCE_STATE = re.compile(r"conflicting sources|schema drift|source stale|stale|"
                          r"not ingested|unavailable", re.I)
# Words of a question that already ask what a template would (the template is skipped).
ASKS = {"who": re.compile(r"\b(?:who|whom|contact|chase|owner|buyer|ask \w+)\b", re.I),
        "changed": re.compile(r"\b(?:chang\w*|latest|update\w*|new|recent\w*|timeline|"
                              r"history|going on)\b", re.I),
        "late": re.compile(r"\b(?:late|overdue|delay\w*|eta|on time|behind)\b", re.I),
        "open": re.compile(r"\b(?:open|outstanding|pending|orders|pos)\b", re.I),
        "supplies": re.compile(r"\b(?:suppl\w*|vendor|source[sd]?)\b", re.I)}
# Types whose records are links or bookkeeping, never a subject to ask about.
INTERNAL_TYPE = re.compile(r"(?:Mapping|EntityMap|Map|Binding|Mirror|Lineage|Line)$")
MAX_FOLLOW_UPS = 3


def subject_name(object_type: str, label: str) -> Optional[str]:
    """A record as the business names it in a question (``PO 106001``, a company's name);
    None for a link record, a key or an empty label."""
    text = " ".join(str(label or "").split())
    if not text or INTERNAL_TYPE.search(object_type or "") or "://" in text \
            or len(text) > 80 or re.search(r":[\w.]+[:{]", text):
        return None
    short = {"PurchaseOrder": "PO", "purchase_order": "PO", "SalesOrder": "SO",
             "sales_order": "SO"}.get(object_type or "")
    if short and re.fullmatch(r"\d[\w-]*", text):
        return f"{short} {text}"
    found = re.fullmatch(r"(po|so)[\s#-]*(\d[\w-]*)", text, re.I)
    if found:
        return f"{found.group(1).upper()} {found.group(2)}"
    return text


def _family(object_type: str) -> str:
    """``order``, ``party`` (a vendor, customer or company), ``item`` or ``record``."""
    kind = object_type or ""
    if re.search(r"Order$|_order$", kind):
        return "order"
    if re.search(r"Vendor|Supplier|Customer|Company|Account|Party", kind, re.I):
        return "party"
    if re.search(r"^(?:Item|Product|Material|Sku)", kind, re.I):
        return "item"
    return "record"


def _record_questions(family: str, name: str) -> list[tuple[str, str]]:
    """(what it asks, the question) for one record, most useful first."""
    if family == "order":
        return [("who", f"Who should I chase about {name}?"),
                ("late", f"Is {name} on time?"),
                ("changed", f"What changed on {name} recently?")]
    if family == "party":
        return [("open", f"What's still open with {name}?"),
                ("who", f"Who is our contact at {name}?")]
    if family == "item":
        return [("supplies", f"Who supplies {name}?"),
                ("open", f"Which open orders need {name}?")]
    return [("changed", f"What's the latest on {name}?"),
            ("who", f"Who owns {name}?")]


def follow_ups(question: str, answer: Any) -> list[str]:
    """Up to ``MAX_FOLLOW_UPS`` questions to ask next, from this answer's own records
    (its subjects) and what it could not confirm (its ``not_found`` and conflicts), never
    from anything the answer does not show its reader: the payload is already trimmed for
    them. A question carries no figure: it names a record or a missing fact in the words
    the answer already shows (a gap's words carrying a number no subject names are
    skipped). A question asking what the reader just asked is skipped. Deterministic."""
    asked = " ".join(str(question or "").lower().split())
    subjects = [(ref.type, name) for ref in getattr(answer, "subjects", None) or ()
                if (name := subject_name(ref.type, ref.label))]
    names = [name for _type, name in subjects]
    # what the answer already says: who to ask and whether it is late need no question
    said_text = " ".join([str(getattr(answer, "direct_answer", "") or "")] + [
        str(fact.text) for fact in getattr(answer, "facts", None) or ()])
    out: list[str] = []

    def covered(intent: str) -> bool:
        return bool(ASKS[intent].search(asked)) or (
            intent in ("who", "late") and bool(ASKS[intent].search(said_text)))

    def add(text: str) -> None:
        said = " ".join(text.lower().split())
        if said != asked and said not in {o.lower() for o in out}:
            out.append(text)

    primary = names[0] if names else None
    for conflict in getattr(answer, "conflicts", None) or ():
        field = words(str(conflict.field or ""))
        if field and primary and not re.search(r"\d", field):
            add(f"Which system is right about the {field} of {primary}?")
            break
    for item in getattr(answer, "not_found", None) or ():
        what = " ".join(str(item.what or "").split()).rstrip(".")
        if not what or GENERIC_GAP.match(what) or SOURCE_STATE.search(
                item.where_it_would_live or "") or len(what) > 80:
            continue
        digits = re.findall(r"\d[\w-]*", what)
        if any(not any(d in name for name in names) for d in digits):
            continue
        what = what if re.match(r"[A-Z]{2,}|[A-Z]\w*\s+\d", what) else what[:1].lower() + what[1:]
        if not re.match(r"(?:a|an|the|its|their|our|any|no|which|who|what|when|where|"
                        r"how|whether)\b|[A-Z]{2,}", what):
            what = "the " + what
        tail = f" for {primary}" if primary and primary.lower() not in what.lower() else ""
        add(f"Who can confirm {what}{tail}?")
        break
    seen: set[str] = set()
    for object_type, name in subjects[:3]:
        family = _family(object_type)
        if family in seen:
            continue
        seen.add(family)
        for intent, text in _record_questions(family, name):
            if not covered(intent):
                add(text)
                break
    if primary and len(out) < MAX_FOLLOW_UPS:
        for intent, text in _record_questions(_family(subjects[0][0]), primary):
            if not covered(intent):
                add(text)
            if len(out) >= MAX_FOLLOW_UPS:
                break
    return out[:MAX_FOLLOW_UPS]
