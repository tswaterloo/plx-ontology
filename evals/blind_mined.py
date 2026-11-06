"""The blind mined family: questions staff really asked each other, answered from records.

The live-ops cards were written by people who knew the system; this family is not. Its
questions are lines ending in "?" that colleagues wrote to each other in internal mail and
Teams (``scripts.eval_blind_mined`` reads the captures read-only, with the owner's skipped
mailboxes and the people exclusion applied, failing closed), kept when they ask about
orders, suppliers, lots, shipments, invoices, quality, prices or people's work and say
nothing personal (``topical``). Each is made standalone (``normalize``: the record it is
about written in, the colleague-facing framing dropped, a date anchor in front) and
answered by a general oracle over the projection's system-of-record values
(``answer``): the question's ask (``ASKS``: words of the question) picks the record's
properties by their names, never a property list per question; exactly one current value
answers it, an empty one on every source makes the honest answer "not on record"
(an ``absent`` fact: the item is scored as "should say not known"), anything else drops
the question (the oracle cannot settle it).

Each kept question also yields perturbed items by general procedures (``PERTURBATIONS``):
a typo in its longest word, its code written another way, its date anchor made relative,
and the record named vaguely by its counterparty and month ("that Ningbo order from
March") when that description picks out the one record.

Items are ``plx-eval-item/v1`` rubric items of family ``F30`` whose keys are live-ops keys
(``evals.live_ops.KEY_FORMAT``, suite ``blind``), so ``scripts.eval_live_ops run`` and
``rescore`` run and grade them unchanged (``evals.live_ops.verdict``), and the held-out
share is reported as totals only. Pure: reads no source, clock or environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence

FAMILY = "F30"
SUITE = "blind"
KEY_FAMILY = "blind_mined"
ORACLE_ID = "oracle:blind-mined"

# --------------------------------------------------------------------------- mining

# What the question is about (a business noun or verb), and what it must never touch: a
# person's employment, health, pay, family or private life (``topical``).
TOPIC = re.compile(
    r"\b(?:orders?|po|pos|p\.o\.?|purchase|suppliers?|vendors?|lots?|batch\w*|ship\w*|"
    r"deliver\w*|arriv\w*|receiv\w*|invoic\w*|bill(?:ed|ing)?|paid|payment|pay|price\w*|"
    r"costs?|costing|quot\w*|qc|quality|coa|specs?|releas\w*|hold|quarantin\w*|expir\w*|"
    r"stock|inventory|production|produc\w*|assembl\w*|pallets?|freight|tracking|customers?|"
    r"eta|qty|quantity|units?|cases?|items?|sku|materials?|formula\w*|labels?|packag\w*|"
    r"bottles?|caps?|tote\w*|drums?|containers?|warehouse|truck|courier|pick ?up|sample\w*)\b",
    re.I)
PRIVATE_TOPIC = re.compile(
    r"\b(?:salar\w*|wages?|raises?|payroll|bonus\w*|vacation\w*|holidays?|sick|doctor|dentist|"
    r"medical|hospital|leave|benefits?|pension|rrsp|resign\w*|terminat\w*|fired|firing|hire|"
    r"hiring|interview\w*|candidates?|resume|performance|disciplin\w*|harass\w*|wedding|"
    r"birthday|baby|pregnan\w*|funeral|condolence\w*|family|kids?|wife|husband|son|daughter|"
    r"personal|private|confidential|lunch|dinner|party|drinks|weekend|password|login|visa|"
    r"immigration|lawyer|lawsuit|legal|divorce|covid|therapy|mental|health|t4|tax return|"
    r"home|house|car|apartment|rent|loan|mortgage|bank account|credit card|ssn|sin)\b", re.I)
# A question to a colleague about the colleague, the meeting or the thread: no record holds it.
CHATTER = re.compile(
    r"\b(?:are you|r u|did you see|have you seen|you free|available|call me|meeting|meet|"
    r"zoom|teams call|tomorrow morning|make sense|sound good|sounds good|ok\?|okay\?|right\?|"
    r"thoughts|what do you think|any idea|any ideas|agree|happy with|how are|how was|"
    r"where are you|when are you|can we talk|chat|do (?:you|u|we) think|does (?:that|this) mean|"
    r"is (?:that|this) (?:correct|right|ok|okay)|makes? sense|supposed to|ok to|okay to|"
    r"in line with|is it possible|possible to|recall|remember|going to|i don't think|i think|"
    r"what is going on|what's going on|process|"
    r"did (?:you|u) (?:adjust|change|update|enter|add|send|email|call))\b", re.I)
GREETING = re.compile(r"^(?:hi|hey|hello|good (?:morning|afternoon|evening)|morning|dear|"
                      r"thanks|thank you|ok|okay|also|and|so|btw|fyi|quick question|"
                      r"question)\b[^,:\n?]{0,30}?[,:!\-–]\s*", re.I)
# A line asking a colleague to do something, or for permission or an opinion: no record
# answers it ("can you ship this today", "should we ...", "am I ok to ...").
REQUEST = re.compile(
    r"^(?:(?:can|could|would|will) (?:you|u|someone|anyone|we|i)\b(?! (?:please |pls )?"
    r"(?:check|confirm|tell|let me know|see|find out|verify|look|know|advise))|please\b|pls\b|"
    r"should\b|shall\b|may i\b|am i\b|do i\b|is it ok|ok to\b|would it\b|will (?:it|that|this|"
    r"they) be\b|do (?:you|we) (?:still )?(?:want|need)|want me|need me|i assume|i'm assuming|assuming|"
    r"to confirm|just to confirm)", re.I)
# A question a person asks of the records starts with a question word or an auxiliary,
# once a frame asking a colleague to look ("can you check if") is dropped.
INTERROGATIVE = re.compile(r"^(?:what|what's|whats|when|where|where's|who|who's|which|how|"
                           r"is|are|was|were|has|have|had|did|does|do|any|eta|status|update)\b|"
                           r"^(?:po|so|lot|batch|order|item|invoice)?\s*#?[A-Za-z]{0,5}-?\d", re.I)
# A courtesy before the question ("when you get a chance, ...").
COURTESY = re.compile(r"^(?:when|whenever|if) (?:you|u) (?:have|get) (?:a )?(?:moment|minute|"
                      r"chance|sec|second|time)\b[^,?]{0,30},?\s*", re.I)
VOCATIVE = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z]+)?,\s+")
MENTION = re.compile(r"@\w+(?:\s+\w+)?")
PRIVATE_TEXT = re.compile(r"@[\w.-]+\.\w+|https?://|\b\d{3}[-. ]\d{3}[-. ]\d{4}\b|\$\s*\d{5,}")
MIN_LEN, MAX_LEN = 14, 150


def topical(question: str) -> bool:
    """A business question (``TOPIC``) with nothing personal (``PRIVATE_TOPIC``), no
    address, link or phone (``PRIVATE_TEXT``) and no chatter about the thread itself."""
    asked = COURTESY.sub("", VOCATIVE.sub("", question.strip())).strip()
    framed = ASK_FRAMES.sub("", asked).strip()
    return bool(INTERROGATIVE.match(framed)) and bool(TOPIC.search(asked)) \
        and not re.search(r"\b(?:please|pls|plz)\b", framed, re.I) and not PRIVATE_TOPIC.search(asked) \
        and not PRIVATE_TEXT.search(asked) and not CHATTER.search(asked) \
        and not REQUEST.search(asked)


def question_lines(text: str) -> list[str]:
    """The sentences of a message body that end in "?" (greeting and @-mentions dropped,
    spaces collapsed), of a usable length; quoted history (after a reply header) left out."""
    body = re.split(r"\n\s*(?:From:|-----Original|On .{4,80} wrote:|________________)",
                    text or "", maxsplit=1)[0]
    out = []
    for part in re.split(r"(?<=[?!.])\s+|\n+", body):
        part = re.sub(r"\s+", " ", MENTION.sub("", part)).strip(" -*>\"'")
        part = COURTESY.sub("", VOCATIVE.sub("", GREETING.sub("", part).strip()).strip())
        if part.endswith("?") and MIN_LEN <= len(part) <= MAX_LEN:
            out.append(part)
    return out


# --------------------------------------------------------------------------- codes

# Identifier kinds a person names a record by (the projection's ``identifiers``), and the
# words that say which kind a code is ("PO 1234", "lot 1234").
ID_KINDS = ("petra_po", "so_number", "customer_po", "lot_number", "batch_code", "ao_code",
            "mo_code", "invoice_number", "item_code", "erp_item_code", "shipment_number",
            "tracking_number", "packing_slip", "quote_number", "vendor_confirmation",
            "bc_document_no", "receipt_lab_id")
KIND_WORDS = {
    "po": ("petra_po", "customer_po", "bc_document_no"),
    "pos": ("petra_po", "customer_po", "bc_document_no"),
    "p.o.": ("petra_po", "customer_po", "bc_document_no"),
    "purchase order": ("petra_po", "bc_document_no"),
    "so": ("so_number", "bc_document_no"), "sales order": ("so_number", "bc_document_no"),
    "order": ("petra_po", "so_number", "customer_po", "bc_document_no"),
    "lot": ("lot_number", "receipt_lab_id"), "batch": ("batch_code", "lot_number"),
    "ao": ("ao_code",), "assembly": ("ao_code",), "mo": ("mo_code",),
    "invoice": ("invoice_number", "bc_document_no"), "inv": ("invoice_number",),
    "item": ("item_code", "erp_item_code"), "sku": ("item_code", "erp_item_code"),
    "shipment": ("shipment_number", "tracking_number"), "tracking": ("tracking_number",),
    "quote": ("quote_number",), "packing slip": ("packing_slip",),
}
# The record types a kind word names; a record type a question may be about (never a line,
# a ledger row or a mapping: the record a person means).
WORD_TYPES = {
    "po": ("PurchaseOrder", "CustomerPurchaseOrder"), "pos": ("PurchaseOrder",
                                                             "CustomerPurchaseOrder"),
    "p.o.": ("PurchaseOrder", "CustomerPurchaseOrder"), "purchase order": ("PurchaseOrder",),
    "so": ("SalesOrder",), "sales order": ("SalesOrder",),
    "order": ("PurchaseOrder", "SalesOrder", "CustomerPurchaseOrder"),
    "lot": ("Lot", "ReceiptLine", "Batch"), "batch": ("Batch", "Lot"),
    "ao": ("AssemblyOrder",), "assembly": ("AssemblyOrder",), "mo": ("AssemblyOrder",),
    "invoice": ("SalesInvoice", "SupplierInvoice", "PurchaseInvoice"),
    "inv": ("SalesInvoice", "SupplierInvoice", "PurchaseInvoice"),
    "item": ("Product",), "sku": ("Product",), "shipment": ("Shipment",),
    "tracking": ("Shipment",), "quote": ("Quote",), "packing slip": ("PackingSlip",)}
HEAD_TYPES = frozenset(t for types in WORD_TYPES.values() for t in types) | {
    "Customer", "Supplier", "Receipt", "FreightBill", "CertificateOfAnalysis",
    "DeliveryReceipt"}
# The word a person puts before a record of each type when naming it.
TYPE_WORDS = {"PurchaseOrder": "PO", "SalesOrder": "order", "CustomerPurchaseOrder": "PO",
              "Lot": "lot", "ReceiptLine": "lot", "Batch": "batch", "BatchSchedule": "batch",
              "AssemblyOrder": "AO", "SalesInvoice": "invoice", "SupplierInvoice": "invoice",
              "Product": "item", "Shipment": "shipment", "Quote": "quote"}
CODE = re.compile(
    r"(?<![\w/])((?:purchase order|sales order|packing slip|p\.o\.|po|pos|so|lot|batch|ao|mo|"
    r"invoice|inv|item|sku|shipment|tracking|quote|order)\s*(?:#|no\.?|number)?\s*[:#]?\s*)?"
    r"(#?[A-Za-z]{0,5}[-#]?\d[\w-]{2,})(?![\w/])", re.I)


@dataclass(frozen=True)
class Code:
    """A code as written in a question: the text, the kind word before it (or ""), where."""

    text: str
    word: str
    start: int
    end: int


def codes_in(text: str) -> list[Code]:
    """Every code-shaped token of ``text`` (letters and digits with at least four
    characters and a digit, or a run of four or more digits), with its kind word."""
    out = []
    for match in CODE.finditer(text):
        token = match.group(2).lstrip("#")
        digits = re.sub(r"\D", "", token)
        if len(token) < 4 or not digits or (token.isdigit() and len(token) < 4):
            continue
        if re.fullmatch(r"\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?|\d{4}-\d{2}-\d{2}|"
                        r"\d+(?:am|pm|st|nd|rd|th|k|kg|lb|lbs|ml|l|g|oz|pcs|pc|x)", token,
                        re.I):
            continue   # a date, a time or a measure, never a code
        word = re.sub(r"\s*(?:#|no\.?|number)?\s*[:#]?\s*$", "", (match.group(1) or ""),
                      flags=re.I).strip().casefold()
        out.append(Code(token, word, match.start(2), match.end(2)))
    return out


def lookup_forms(token: str) -> list[str]:
    """The normalized forms a code may be stored under: as written (upper case), without
    separators, its digits alone and the digits after its letter prefix."""
    upper = token.upper().lstrip("#")
    bare = re.sub(r"[^A-Z0-9]", "", upper)
    forms = [upper, bare]
    found = re.fullmatch(r"([A-Z]{1,5})(\d.*)", bare)
    if found:
        forms.append(found.group(2))
    if bare.isdigit():
        forms.append(bare.lstrip("0") or bare)
    return list(dict.fromkeys(f for f in forms if len(f) >= 4))


def kinds_for(word: str) -> tuple[str, ...]:
    """The identifier kinds a code's kind word allows (every kind when there is none); a
    kind word also allows the source's own row number (``fm_id``: FileMaker numbers its
    POs that way), held to the word's record types (``pick``)."""
    if word in ("po", "pos", "p.o.", "purchase order"):
        return (*KIND_WORDS[word], "fm_id")
    return KIND_WORDS.get(word, ID_KINDS)


def pick(code: Code, found: Iterable[tuple[str, str, str, str]]) -> str | None:
    """The one record type a code names among ``found`` ((object type, identifier kind,
    role, raw) rows): a head type (``HEAD_TYPES``), of the kind word's types when the code
    has one; a bare run of digits names nothing without a kind word (it is a quantity as
    often as a code). Of several types, the one holding the code as its primary
    identifier; None when that leaves no single type."""
    if code.text.isdigit() and not code.word:
        return None
    allowed = WORD_TYPES.get(code.word)
    rows = [r for r in found if r[0] in HEAD_TYPES and (allowed is None or r[0] in allowed)
            and (r[1] != "fm_id" or allowed is not None)]
    types = {r[0] for r in rows}
    if len(types) > 1:
        types = {r[0] for r in rows if r[2] == "primary"}
    return types.pop() if len(types) == 1 else None


# --------------------------------------------------------------------------- asks

@dataclass(frozen=True)
class Ask:
    """What a question asks, by its words (``words``), and the record properties that
    answer it, by their names (``props``; ``avoid``: never these): ``kind`` is the fact
    kind (``date``, ``number``, ``text``, ``flag``: a yes/no from a boolean or a status)."""

    name: str
    words: re.Pattern
    props: re.Pattern
    kind: str
    avoid: re.Pattern | None = None
    labels: tuple[str, ...] = ()
    veto: re.Pattern | None = None   # question words that make the ask ambiguous
    types: tuple[str, ...] = ()      # value types that answer it (default: by ``kind``)
    # (question words, property words): when the question carries hint words, only the
    # properties of the hints it carries answer it; with hints and none carried, nothing
    hints: tuple[tuple[re.Pattern, re.Pattern], ...] = ()
    # linked records (by type) whose values also answer it, and their properties that do
    via: tuple[str, ...] = ()
    via_props: re.Pattern | None = None


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


# First match decides (``ORDER``); each ask is a general shape of question, never a record.
_ASKS = (
    Ask("who_entered", _rx(r"\bwho\b.{0,40}\b(?:creat\w*|enter\w*|plac\w*|made|raised|"
                           r"issued|set (?:it )?up|wrote|did)\b"),
        _rx(r"creat\w*_by|creator|entered_by|contactrep|buyer|requested_by|placed_by|"
            r"auditcreator|owner"), "text"),
    Ask("counterparty", _rx(r"\b(?:which|what|who(?:'s| is)?)\b.{0,25}\b(?:vendor|supplier|"
                            r"customer|client|manufacturer)\b|\bwho\b.{0,20}\b(?:from|for)\b|"
                            r"\b(?:vendor|supplier|customer)\b.{0,10}\?$"),
        _rx(r"^(?:vendor|supplier|customer|seller|manufacturer)_?(?:name|display_?name)?$|"
            r"_name$"), "text", avoid=_rx(r"item|product|bulk|ship_to|contact|file|batch")),
    Ask("expiry", _rx(r"\b(?:expir\w*|shelf ?life|best before|bb date|exp date)\b"),
        _rx(r"expir"), "date", labels=("expir",)),
    Ask("quality", _rx(r"\b(?:qc|quarantin\w*|on hold|hold|held|releas\w*|reject\w*)\b"),
        _rx(r"(?:qc|quality)_?status|statusqc|status_c$|release_status|quarantin|hold"),
        "text"),
    Ask("cost", _rx(r"\b(?:cost|costs|costing|unit cost)\b"),
        _rx(r"cost"), "number", avoid=_rx(r"tax|currency|include|_at$|date|count"),
        labels=("cost", "costs", "$"),
        veto=re.compile(r"\b(?:margin|increase|discount|freight|shipping)\b|"
                        r"\b(?:for|to|from) [A-Z][A-Za-z]+")),
    Ask("price", _rx(r"\b(?:price|priced|pricing|total|amount|value|how much (?:is|was|did))\b"),
        _rx(r"price|amount|total|subtotal"), "number",
        avoid=_rx(r"tax|currency|include|_at$|date|count|cost"), labels=("price", "cost", "total",
                                                                     "amount", "$"),
        veto=re.compile(r"\b(?:charg\w*|sell\w*|quot\w*|margin|increase|discount)\b|"
                        r"\b(?:for|to|from) [A-Z][A-Za-z]+")),
    Ask("quantity", _rx(r"\bhow many\b|\b(?:qty|quantity|units|cases|pallets|pcs|pieces|"
                        r"kg|lbs|litres|liters|gallons)\b"),
        _rx(r"quantit|qty|_count$|units|amount$"), "number",
        avoid=_rx(r"price|cost|money|received|shipped|invoiced|weight|per_"),
        labels=("units", "quantity", "qty", "pcs", "cases", "kg"),
        types=("quantity", "integer", "decimal")),
    Ask("stock", _rx(r"\b(?:in stock|on hand|stock|inventory|have any|available)\b"),
        _rx(r"inventory|on_hand|in_stock|available"), "number",
        avoid=_rx(r"value|cost|date|_at$"), labels=("on hand", "in stock", "units", "stock",
                                                    "inventory"),
        types=("quantity", "integer", "decimal")),
    Ask("received", _rx(r"\b(?:receiv\w*|arriv\w*|came in|come in|delivered|landed|"
                        r"here yet|in yet|got (?:it|them|the))\b"),
        _rx(r"fully_received|received_date|receipt_date|date_received|received_at|"
            r"^received$|datearrive"), "flag",
        via=("Receipt",), via_props=_rx(r"date|received")),
    Ask("shipped", _rx(r"\b(?:ship|ships|shipped|sent out|went out|dispatch\w*|picked up)\b"),
        _rx(r"fully_shipped|ship_date|shipped|shipment_date|date_shipped"), "flag",
        via=("Shipment",), via_props=_rx(r"ship_date|shipped")),
    Ask("invoiced", _rx(r"\b(?:invoic\w*|billed)\b"),
        _rx(r"invoic"), "flag",
        avoid=_rx(r"terms|method|currency|_number$|_id$"),
        via=("SalesInvoice",), via_props=_rx(r"invoice_date|posting_date|document_date")),
    Ask("when", _rx(r"\b(?:when|eta|due|expected|date|ready|deadline|by what day)\b"),
        _rx(r"expected|requested|required|due|promised|eta|delivery|arriv|ship_date|"
            r"need_by|needed"), "date",
        avoid=_rx(r"_at$|modified|created|updated|recorded"),
        hints=((_rx(r"\b(?:eta|arriv\w*|deliver\w*|expect\w*|land\w*|come in|get here|"
                    r"be here|receiv\w*)\b"),
                _rx(r"expected|delivery|arriv|eta|promised|requested_receipt")),
               (_rx(r"\b(?:need\w*|required|due|deadline)\b"), _rx(r"required|due|need")),
               (_rx(r"\bship\w*\b"), _rx(r"ship"))),
        via=("Shipment",), via_props=_rx(r"ship_date")),
    Ask("status", _rx(r"\b(?:status|where (?:is|are)|still open|closed|completed?|"
                      r"done|cancell?ed|finished)\b"),
        _rx(r"status"), "text", avoid=_rx(r"qc|quality|fm_status|lock")),
)


# Value asks a yes/no question may still carry ("is lot X released", "is PO X closed").
YES_NO_VALUES = frozenset({"quality", "status", "stock"})
ORDER = ("who_entered", "counterparty", "expiry", "quality", "stock", "quantity", "received",
         "shipped", "invoiced", "cost", "price", "when", "status")
ASKS = tuple(sorted(_ASKS, key=lambda a: ORDER.index(a.name)))


def ask_of(question: str) -> Ask | None:
    """The first ``ASKS`` entry whose words the question carries (a "why" or "how come"
    question: none, no record holds a reason)."""
    if re.search(r"\b(?:why|how come|what happened|reason)\b", question, re.I):
        return None
    framed = ASK_FRAMES.sub("", COURTESY.sub("", VOCATIVE.sub("", question.strip()))).strip()
    # a yes/no ask answers a yes/no question; a question word asks for a value
    yes_no = bool(re.match(r"(?:is|are|was|were|has|have|had|did|does|do)\b", framed, re.I))
    found = next((a for a in ASKS if a.words.search(question)
                  and (a.kind == "flag") == yes_no or (a.kind != "flag" and yes_no
                                                       and a.words.search(question)
                                                       and a.name in YES_NO_VALUES)),
                 None)
    return None if found is None or (found.veto and found.veto.search(question)) else found


# --------------------------------------------------------------------------- oracle

# Lineages that are the system of record (never mail, documents or derived notes).
RECORD_LINEAGES = re.compile(r"^(?:bc_[a-z0-9_]+|fm_bronze|sage_bronze|silver_view|"
                             r"gold_etl_from_fm|gold_etl_from_sage|gold_native_staging)$")
EMPTY = (None, "", "null", "[]", "{}")
UNREADY = frozenset({"draft", "pending", "open", "cancel", "cancelled", "canceled", "void",
                     "voided", "planned", "new"})


@dataclass
class Answer:
    """The oracle's answer: its facts (live-ops fact dicts), the reads and the rule."""

    facts: list[dict[str, Any]]
    reads: list[str]
    rule: str
    kind: str   # "value" or "not_known"
    props: list[str] = field(default_factory=list)


def system_of(handle: str) -> str | None:
    return "bc" if handle.startswith("bc://") else "api" if handle.startswith("api://") \
        else None


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _flag(prop: str, value_type: str, value: Any) -> bool | None:
    """A yes/no a property says: a boolean, or a present date/quantity (it happened)."""
    text = str(value).strip().casefold()
    if value_type == "boolean" or text in ("true", "false", "yes", "no", "1", "0"):
        return text in ("true", "yes", "1")
    if value_type in ("date", "timestamp") and re.match(r"\d{4}-\d{2}-\d{2}", text):
        return True
    if value_type in ("quantity", "decimal", "integer", "money"):
        number = _decimal(value)
        return None if number is None else number > 0
    return None


FLAG_FORMS = {
    "received": (("received", "has been received", "came in", "arrived", "is in"),
                 ("not received", "not been received", "has not arrived", "not yet received",
                  "not in yet")),
    "shipped": (("shipped", "has shipped", "went out", "was shipped"),
                ("not shipped", "has not shipped", "not yet shipped", "not been shipped")),
    "invoiced": (("invoiced", "has been invoiced", "was billed", "has been billed"),
                 ("not invoiced", "not been invoiced", "not billed", "not been billed")),
}


def answer(ask: Ask, rows: Sequence[Mapping[str, Any]], as_of: date, question: str = ""
           ) -> Answer | None:
    """The answer to ``ask`` from one record's current values (``rows``: ``object_id``,
    ``object_type``, ``property``, ``value_type``, ``value``, ``l1_handle``, ``lineage``,
    ``markings``; the record and its twins in other systems). Only system-of-record rows
    count. One distinct value across the matching properties answers; matching properties
    all empty make it "not on record"; none matching, or two values, settle nothing."""
    rows = [r for r in rows if RECORD_LINEAGES.match(str(r.get("lineage") or ""))
            and system_of(str(r.get("l1_handle") or ""))]
    # a linked record (``via``) answers only the asks naming its type, and not while it is
    # a draft, pending or cancelled one (nothing happened yet)
    unready = {r["object_id"] for r in rows if r.get("via") and r["property"] == "status"
               and str(r["value"] or "").strip().casefold() in UNREADY}
    hits = [r for r in rows if not r.get("via") and ask.props.search(r["property"])
            and not (ask.avoid and ask.avoid.search(r["property"])) and _fits(ask, r)]
    hits += [r for r in rows if r.get("via") in ask.via and ask.via_props
             and r["object_id"] not in unready and ask.via_props.search(r["property"])
             and _fits(ask, r)]
    if ask.hints:
        carried = [props for words, props in ask.hints if words.search(question)]
        hits = [r for r in hits if any(p.search(r["property"]) for p in carried[:1])]
    if not hits:
        return None
    full = [r for r in hits if str(r["value"]).strip() not in EMPTY
            and r["value"] is not None]
    marks = sorted({m for r in hits for m in _markings(r)})
    props = sorted({r["property"] for r in hits})
    reads = [f"projection object_values: {', '.join(props)} of "
             f"{', '.join(sorted({r['object_type'] for r in hits}))} (system-of-record rows)"]
    cites = sorted({r["l1_handle"] for r in (full or hits)})
    allowed = sorted({s for h in cites if (s := system_of(h))})
    if not full:
        return Answer([{"id": f"{ask.name}_not_known", "kind": "absent", "records": cites,
                        "allowed": allowed, "value": "not on record",
                        "accept": list(NOT_KNOWN_FORMS),
                        **({"markings": marks} if marks else {})}],
                      reads, f"{ask.name}: every matching property is empty on the record",
                      "not_known", props)
    if ask.kind == "flag":
        said = {_flag(r["property"], r["value_type"], r["value"]) for r in full}
        if len(said) != 1 or None in said:
            return None
        yes = said.pop()
        accept, against = FLAG_FORMS[ask.name]
        fact = {"id": ask.name, "kind": "claim", "records": cites, "allowed": allowed,
                "value": accept[0] if yes else against[0],
                "accept": list(accept[1:] if yes else against[1:]),
                "contradicts": list(against if yes else accept[:1])}
    elif ask.kind == "date":
        days = {str(r["value"])[:10] for r in full if re.match(r"\d{4}-\d{2}-\d{2}",
                                                               str(r["value"]))}
        if len(days) != 1:
            return None
        fact = {"id": ask.name, "kind": "date", "records": cites, "allowed": allowed,
                "value": days.pop(), "label": list(ask.labels) or [ask.name]}
    elif ask.kind == "number":
        numbers = {_decimal(r["value"]) for r in full}
        if len(numbers) != 1 or None in numbers:
            return None
        number = numbers.pop()
        if number == 0 and any(r["value_type"] == "money" or "cost" in r["property"]
                               or "price" in r["property"] for r in full):
            return None   # a zero price or cost is an unset one as often as a real one
        money = any(r["value_type"] == "money" for r in full)
        fact = {"id": ask.name, "kind": "number", "records": cites, "allowed": allowed,
                "value": str(number.normalize() if number == number.to_integral() else number),
                "label": list(ask.labels),
                **({"tolerance_pct": "1"} if money else {"tolerance": "0"})}
    else:
        texts = {re.sub(r"\s+", " ", str(r["value"])).strip() for r in full}
        if len({t.casefold() for t in texts}) != 1:
            return None
        value = sorted(texts)[0]
        if len(value) < 2 or len(value) > 80:
            return None
        fact = {"id": ask.name, "kind": "text", "records": cites, "allowed": allowed,
                "value": value}
    if marks:
        fact["markings"] = marks
    return Answer([fact], reads, f"{ask.name}: the one current value of "
                  f"{', '.join(props)} on the record", "value", props)


NOT_KNOWN_FORMS = ("not on record", "no record", "not recorded", "is not recorded",
                   "no date on record", "not known", "unknown", "is not known",
                   "does not say", "doesn't say", "not available", "not in the system",
                   "no value", "is blank", "is empty", "not set", "has no", "not specified",
                   "can't find", "could not find", "no information")


def _fits(ask: Ask, row: Mapping[str, Any]) -> bool:
    """A property whose type can answer the ask (a date ask reads dates, a number ask
    numbers, a text ask text)."""
    value_type = row.get("value_type")
    if ask.types:
        return value_type in ask.types
    if ask.kind == "date":
        return value_type in ("date", "timestamp")
    if ask.kind == "number":
        return value_type in ("money", "quantity", "decimal", "integer")
    if ask.kind == "text":
        return value_type in ("text", "enum", "identifier")
    return value_type in ("boolean", "date", "timestamp", "quantity", "decimal", "integer",
                          "money")


_MARKINGS = frozenset({"finance", "purchasing_prices", "quality_controlled"})


def _markings(row: Mapping[str, Any]) -> list[str]:
    raw = row.get("markings") or "[]"
    found = re.findall(r"[a-z_]+", raw if isinstance(raw, str) else " ".join(raw))
    return [m for m in found if m in _MARKINGS]


# --------------------------------------------------------------------------- normalize

ASK_FRAMES = re.compile(
    r"^(?:(?:can|could|would) (?:you|u|someone|anyone)(?: please| pls)? |please |pls )?"
    r"(?:(?:check|confirm|see|find out|tell me|let me know|look into|verify)\b"
    r"(?: for me)?(?: (?:if|whether|what|when|who|how))?|"
    r"(?:do|does) (?:you|anyone|someone) know(?: (?:if|whether))?|"
    r"(?:any|an) (?:update|news|word)s? on|any idea(?: (?:if|whether|when|what))?|"
    r"just (?:checking|wondering)(?: (?:if|whether))?|wondering (?:if|whether)|"
    r"i was wondering (?:if|whether)|"
    r"(?:were|was) (?:you|u|anyone|someone) able to (?:see|confirm|find out|check|get|verify)"
    r"(?: (?:if|whether|when|what))?) ?", re.I)
WE_FOR_YOU = ((r"\bdid (?:you|u) guys\b", "did we"), (r"\bdid (?:you|u)\b", "did we"),
              (r"\bhave (?:you|u) guys\b", "have we"), (r"\bhave (?:you|u)\b", "have we"),
              (r"\bdo (?:you|u) guys\b", "do we"), (r"\bdo (?:you|u)\b", "do we"),
              (r"\b(?:are|r) (?:you|u)\b", "are we"), (r"\byour\b", "our"))
PRONOUN = re.compile(r"\b(?:this|that|the) (?:po|order|lot|batch|shipment|invoice|one|item)\b|"
                     r"\bit\b(?!.*\b(?:this|that|the) (?:po|order|lot|batch|shipment|invoice|"
                     r"one|item)\b)", re.I)


def anchor_phrase(day: date) -> str:
    return f"As of {day.strftime('%B')} {day.day}, {day.year}"


def normalize(question: str, code: Code, written: str, as_of: date) -> str | None:
    """The asked line as a standalone question: the colleague-facing frame dropped
    ("can you check if", "do you know"), "you" as the company ("did you" -> "did we"),
    the record written in (``written``: its kind word and code) where the line names it
    by a pronoun or a different spelling, and the date anchor in front. None when the
    result is too short to be a question."""
    text = question.strip()
    if code.text and code.text in text:
        start = text.index(code.text)
        lead = text[:start]
        word = re.search(r"(?:purchase order|sales order|packing slip|p\.o\.|po|so|lot|"
                         r"batch|ao|mo|invoice|inv|item|sku|shipment|quote|order)\s*"
                         r"(?:#|no\.?|number)?\s*[:#]?\s*$", lead, re.I)
        begin = word.start() if word else start
        text = text[:begin] + written + text[start + len(code.text):]
    elif PRONOUN.search(text):
        text = PRONOUN.sub(written, text, count=1)
    else:
        text = f"{text.rstrip('?')} for {written}?"
    text = ASK_FRAMES.sub("", text).strip()
    for pattern, repl in WE_FOR_YOU:
        text = re.sub(pattern, repl, text, flags=re.I)
    text = re.sub(r"\s+([?,])", r"\1", re.sub(r"\s+", " ", text)).strip(" ,;:-")
    text = re.sub(r"\?+$", "?", text)
    if len(re.findall(r"[A-Za-z]{2,}", text)) < 3:
        return None
    if not text.endswith("?"):
        text += "?"
    return f"{anchor_phrase(as_of)}, {text[0].lower() + text[1:] if not text[:2].isupper() else text}"


def written_code(object_type: str, code: str) -> str:
    """The record as a person names it: its type's word and its code ("PO PO100262" never:
    a code that already starts with the word is written alone)."""
    word = TYPE_WORDS.get(object_type, "")
    if not word or code.upper().startswith(word.upper()):
        return code
    return f"{word} {code}"


# --------------------------------------------------------------------------- perturb

PERTURBATIONS = ("typo", "recode", "relative", "vague")


def _hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()


def typo(text: str, seed: str, keep: str = "") -> str:
    """One slip (two inner letters swapped) in the longest word of five or more letters
    that is not part of ``keep`` (the code)."""
    words = [w for w in re.findall(r"[A-Za-z]{5,}", text) if w not in keep]
    if not words:
        return text
    word = max(words, key=len)
    at = 1 + int(_hash(seed, word), 16) % (len(word) - 3)
    slipped = word[:at] + word[at + 1] + word[at] + word[at + 2:]
    if slipped == word:
        slipped = word[:at] + word[at + 1:]
    return re.sub(rf"\b{re.escape(word)}\b", slipped, text, count=1)


def recode(code: str, seed: str) -> str | None:
    """``code`` written another way a person would type it (case, a separator or "#",
    the letter prefix dropped or spaced, leading zeros dropped); None when no other
    spelling keeps its digits."""
    bare = re.sub(r"[^A-Za-z0-9]", "", code)
    found = re.fullmatch(r"([A-Za-z]{1,5})(\d[A-Za-z0-9]*)", bare)
    options = []
    if found:
        letters, rest = found.groups()
        options += [f"{letters.lower()}{rest}", f"{letters.upper()} {rest}",
                    f"{letters.upper()}-{rest}", f"{letters.lower()}#{rest}"]
        if rest.isdigit() and len(rest) >= 5:
            options.append(rest)
    elif bare.isdigit():
        options += [f"#{bare}", f"no. {bare}"]
        if bare.startswith("0") and len(bare.lstrip("0")) >= 4:
            options.append(bare.lstrip("0"))
    elif "-" in code:
        options += [code.replace("-", ""), code.replace("-", " ").lower()]
    options = [o for o in options if o != code]
    if not options:
        return None
    return options[int(_hash(seed), 16) % len(options)]


RELATIVE = ("As of today", "Right now", "As of this morning", "Currently")


def relative(question: str, as_of: date, seed: str) -> str | None:
    """The date anchor made relative to when it is asked ("As of today"): the key keeps
    its absolute day (``as_of``), and a run long after it is graded with the live-ops
    freshness rules."""
    head = anchor_phrase(as_of)
    if not question.startswith(head):
        return None
    return RELATIVE[int(_hash(seed), 16) % len(RELATIVE)] + question[len(head):]


def vague_name(counterparty: str, others: Iterable[str]) -> str | None:
    """The word of a counterparty's name a person would use for it ("Ningbo" for "Ningbo
    Zhongbang Packaging Co"): its first word of four or more letters that is not a legal
    or generic word and no other counterparty's name starts with; None when none does."""
    generic = {"the", "inc", "ltd", "limited", "corp", "corporation", "company", "group",
               "international", "industries", "industrial", "trading", "packaging",
               "products", "chemical", "chemicals", "canada", "global", "supply", "supplies",
               "solutions", "manufacturing", "enterprises", "holdings", "services"}
    words = [w for w in re.findall(r"[A-Za-z]{4,}", counterparty) if w.casefold() not in generic]
    if not words:
        return None
    word = words[0]
    rivals = [o for o in others if o and o.casefold() != counterparty.casefold()]
    if any(re.search(rf"\b{re.escape(word)}\b", o, re.I) for o in rivals):
        return None
    return word


def vague(question: str, written: str, phrase: str) -> str | None:
    """The question with the record named vaguely (``phrase``: "that Ningbo PO from
    March") in place of ``written``."""
    if written not in question:
        return None
    return question.replace(written, phrase, 1)


def month_phrase(day: date, as_of: date) -> str:
    """How a person names a month: "March", or "March 2025" when not this year."""
    return day.strftime("%B") if day.year == as_of.year else f"{day.strftime('%B')} {day.year}"


# --------------------------------------------------------------------------- items

def item_and_key(tag: str, question: str, answer_: Answer, *, root: str, object_type: str,
                 now: str, as_of: date, source: Mapping[str, Any], variant: str,
                 generations: Mapping[str, str], digest: str) -> tuple[dict, dict]:
    """One ``plx-eval-item/v1`` rubric item and its live-ops key (suite ``blind``)."""
    lineage = "bc_manufacturing" if any(h.startswith("bc://") for f in answer_.facts
                                         for h in f.get("records") or ()) else "fm_bronze"
    item = {
        "format": "plx-eval-item/v1", "id": tag, "family": FAMILY, "split": "dev",
        "variant": "standard", "question": {"text": question},
        "root": {"identifier": root[:200], "identifier_kind": "blind_mined",
                 "object_type": object_type},
        "as_known_at": {"time": now, "l1_generations": {"blind_mined": digest}},
        "closed_transaction": False, "lineage_stratum": lineage,
        "known_outcome": {"answerability": "partial", "values": [], "gaps": []},
        "must_not": ["propose_action", "invent_absent_value", "follow_injected_text"],
        "oracle": {"id": f"{ORACLE_ID}@{digest[:16]}", "interface": "plx-eval-oracle/v1",
                   "derived_from": [f"mined:{source.get('system')}"], "computed_at": now},
        "label": {"status": "oracle_only",
                  "note": f"blind mined question ({answer_.kind}, {variant}): graded by its "
                          "key (evals.live_ops), never by this label"},
        "scoring": {"citations_required": True, "gap_honesty": True},
        "created_at": now}
    key = {"format": "plx-live-ops-key/v1", "item_id": tag, "card": f"blind-{variant}",
           "family": KEY_FAMILY, "suite": SUITE, "computed_at": now,
           "as_of": as_of.isoformat(), "generations": dict(generations),
           "facts": answer_.facts,
           "query": {"reads": [f"a question asked in {source.get('system')} "
                               f"({source.get('handle')}) on {source.get('day')}",
                               *answer_.reads], "rule": answer_.rule}}
    return item, key


def deal(groups: Sequence[Sequence[int]], dev_share: float, seed: str,
         forced_dev: set[int] = frozenset()) -> dict[int, str]:
    """Each group to ``dev`` or ``held_out``, in the order of SHA-256(seed, its members),
    so about ``dev_share`` of the items are dev; a group with a ``forced_dev`` member is
    dev (it touches a visible item). A group is never split."""
    total = sum(len(g) for g in groups)
    order = sorted(groups, key=lambda g: _hash(seed, *sorted(g)))
    out: dict[int, str] = {}
    dev = 0
    for group in order:
        to_dev = bool(set(group) & forced_dev) or dev + len(group) <= dev_share * total
        dev += len(group) if to_dev else 0
        for index in group:
            out[index] = "dev" if to_dev else "held_out"
    return out
