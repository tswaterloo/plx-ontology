"""A record page's core read: the record's own card, served before anything else.

The page's quick read walks the record's links, timeline and documents and checks a
customer's orders (tens of seconds cold, under load); its core read is the record's card
alone (one ``plx.object``), so the page shows the record's details at once while the
rest streams in. It is read as the reader, with the same resolution and visibility as the
page (``page_oid``: one the reader cannot see is not found), and kept per reader under
the page's own key (``page_key``: the reader's policy, the generation and the markings),
so no reader is ever served another's copy.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html
import json
import re
from typing import Any, Iterable, Optional

from . import models as m
from .errors import UserFacing

# ---- shared reader-facing text hygiene -----------------------------------------------------
# Deterministic cleanup applied at the API formatting boundary so raw HTML, reply headers,
# Teams JSON sender blobs, meeting join boilerplate, GUIDs, internal snake_case slugs,
# doubled prefixes, binary float noise, and em dashes never reach the screen.

_GUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_BARE_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_AMAZON_RELAY_RE = re.compile(r"^[^@\s]+\+[^@\s]+@marketplace\.amazon\.[a-z.]+$", re.IGNORECASE)
_GOOGLE_GROUP_CAL_RE = re.compile(r"^[^@\s]+@group\.calendar\.google\.com$", re.IGNORECASE)
_XML_ESCAPE_RE = re.compile(r"(?:_x[0-9A-Fa-f]{4}_)+")
_STYLE_SCRIPT_RE = re.compile(r"<(?:style|script)\b[^>]*>.*?</(?:style|script)>", re.IGNORECASE | re.S)
_TEAMS_AT_RE = re.compile(r"<at\b[^>]*>(.*?)</at>", re.IGNORECASE | re.S)
_TEAMS_ATTACH_RE = re.compile(r"<attachment\b[^>]*/?>(?:</attachment>)?", re.IGNORECASE)
_CID_TAG_RE = re.compile(r"\[?\s*cid:[^\s\]<>]+\s*\]?", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s+[^<>]{0,400})?/?>")
_ZENDESK_REPLY_RE = re.compile(r"##-\s*Please type your reply above this line\s*-##\s*", re.IGNORECASE)
_CAUTION_BANNER_RE = re.compile(
    r"(?:\[?\s*EXTERNAL(?:\s+EMAIL)?\s*\]?\s*:?\s*|"
    r"CAUTION:\s*This email originated from outside[^.\n]*\.?\s*|"
    r"Do not click links or open attachments unless you recognize the sender[^.\n]*\.?\s*)",
    re.IGNORECASE,
)
_REPLY_HEADER_RE = re.compile(
    r"(?:^|\s)(?:-{2,}\s*Original Message\s*-{2,}|_{8,}|"
    r"From:\s+[^\n]{2,120}\s+(?:Sent|Date):\s+[^\n]{2,120}(?:\s+To:\s+[^\n]{2,200})?"
    r"(?:\s+Subject:\s+[^\n]{0,200})?|"
    r"On\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|[A-Z][a-z]{2,8}\s+\d{1,2})[^\n]{4,90}\s+wrote:\s*).*$",
    re.IGNORECASE | re.S,
)
_MEETING_BOILER_RE = re.compile(
    r"(?:_{6,}\s*)?"
    r"(?:Microsoft Teams(?:\s+Need help\?)?\s*(?:meeting)?\s*Join(?:\s+on your computer|\s+the meeting now)?\s*:?.*|"
    r"Join with Google Meet\b.*|"
    r"You have been invited by\s+.+?\s+to attend an event named\b.*|"
    r"Join Zoom Meeting\b.*|"
    r"Microsoft Viva Insights has scheduled\b.*|"
    r"Meeting ID:\s*[\d\s]+(?:Passcode:\s*\S+)?.*|"
    r"Dial-in\s+numbers?\b.*|"
    r"Get Outlook for (?:iOS|Android).*)",
    re.IGNORECASE | re.S,
)
_GITHUB_FOOTER_RE = re.compile(
    r"(?:\s*(?:-|\u2014|--)\s*)?Reply to this email directly,\s*view it on GitHub,\s*or unsubscribe.*$",
    re.IGNORECASE | re.S,
)
_BROWSER_VIEW_RE = re.compile(
    r"\bView (?:this email in your browser|as a webpage)\b\.?\s*", re.IGNORECASE
)
_LONG_URL_RE = re.compile(r"https?://\S{55,}", re.IGNORECASE)
_TEAMS_JOIN_URL_RE = re.compile(r"https?://teams\.microsoft\.com/\S+", re.IGNORECASE)
_MEET_URL_RE = re.compile(r"https?://meet\.google\.com/\S+", re.IGNORECASE)
_ZOOM_URL_RE = re.compile(r"https?://[\w.-]*zoom\.us/\S+", re.IGNORECASE)
_SYNCED_INVITE_RE = re.compile(
    r"^Synced invitation:\s*(.+?)(?:\s*@\s*\([A-Z]{2,5}\))?(?:\s*\([^()\s]+@[^()\s]+\))?\s*$",
    re.IGNORECASE,
)
_NOT_JUNK_GUID_RE = re.compile(
    r"Not junk:\s*[0-9a-f-]{36}\|[^|\s]+\|\((.*)\)?", re.IGNORECASE
)
_STACKED_PREFIX_RE = re.compile(
    r"^(?:(?:re|fw|fwd)\s*:\s*){2,}", re.IGNORECASE
)
_BARE_REPLY_PREFIX_RE = re.compile(r"^(?:re|fw|fwd)\s*:\s*$", re.IGNORECASE)
_EMPTY_QUOTED_PREFIX_RE = re.compile(r'wrote\s+"(?:re|fw|fwd)\s*:\s*"', re.IGNORECASE)
_DOUBLE_APPROVAL_RE = re.compile(
    r"^(?:Approval (?:requested|to decide):\s*)+Approval requested for\b", re.IGNORECASE
)
_DOUBLE_PO_RE = re.compile(r"\bPO\s+(PO[-#]?\s*[A-Za-z0-9])", re.IGNORECASE)
_DOUBLE_SO_RE = re.compile(r"\bSO\s+(SO[-#]?\s*[A-Za-z0-9])", re.IGNORECASE)
_RAW_OID_RE = re.compile(
    r"\b(?:PurchaseOrder|SalesOrder|Email|TeamsMessage|Customer|Supplier|MCApproval|MCTask|"
    r"PlannerTask|Batch|Lot|Product|Shipment|Receipt|SalesInvoice|SupplierInvoice|"
    r"CalendarEvent|CertificateOfAnalysis):(?:[a-z][a-z0-9_.]*):(?:\{[^}\s]{1,120}\}|[^\s,.;)]+)"
)
_RAW_URI_RE = re.compile(
    r"\b(?:graph-mail|teams-chat|teams-channel|sharepoint|sharepoint-list|api|bc|mc|original|"
    r"fm|repo)://[^\s,;)>\]]+"
)
_CAMEL_COUNT_RE = re.compile(
    r"\b(\d+)\s+(PurchaseOrder|SalesOrder|AssemblyOrder|SalesInvoice|SupplierInvoice|"
    r"PurchaseInvoice|Customer|Supplier|Batch|Lot|Product|Shipment|Receipt)\b"
)
_PLAIN_AMOUNT_CUR_RE = re.compile(
    r"(?<![\w,.])(\d{4,}\.\d{2})\s+(CAD|USD|EUR|GBP)\b"
)
_HYPHEN_RUN_RE = re.compile(r"[-_]{4,}")
_TRAILING_DANGLING_ON_RE = re.compile(r"\b(Mission Control question|question)\s+on\s*$", re.IGNORECASE)
_EMPTY_PARENS_RE = re.compile(r"\s*\(\s*\)")
_DOUBLE_SEP_RE = re.compile(r"(?:\s*·\s*){2,}")
_DOUBLE_COMMA_RE = re.compile(r"(?:,\s*){2,}")
_NONE_LITERALS = frozenset({"none", "null", "n/a", "undefined"})

_ENUM_LABELS: dict[str, dict[str, str]] = {
    "item_type": {
        "raw_material": "Raw material",
        "final_product": "Finished good",
        "packaging": "Packaging",
        "distribution": "Distribution",
        "bulk": "Bulk",
        "inventory": "Inventory",
    },
    "boolean": {
        "true": "Yes",
        "false": "No",
        "yes": "Yes",
        "no": "No",
        "1": "Yes",
        "0": "No",
    },
    "status": {
        "open": "Open",
        "closed": "Closed",
        "draft": "Draft",
        "unknown": "Unknown",
        "active": "Active",
        "inactive": "Inactive",
        "cancelled": "Cancelled",
        "canceled": "Cancelled",
        "completed": "Completed",
        "released": "Released",
        "allocated": "Allocated",
        "committed": "Committed",
        "generated": "Generated",
        "realized": "Realized",
        "on-hold": "On hold",
        "on_hold": "On hold",
    },
    "document_type": {
        "invoice": "Invoice",
        "payment": "Payment",
        "prepayment": "Prepayment",
        "credit note": "Credit note",
        "credit_note": "Credit note",
        "debit note": "Debit note",
        "debit_note": "Debit note",
        "interest": "Interest",
    },
    "payee_class": {
        "tax_payroll": "Tax and payroll",
        "expense": "Expense",
        "trade": "Trade",
        "capital": "Capital",
        "finance": "Finance",
        "other": "Other",
        "unknown": "Unknown",
    },
    "routing_basis": {
        "owner_open": "all departments (owner)",
        "directory_department": "your department",
        "department": "your department",
        "role_map": "your role",
        "role": "your role",
        "all": "all departments",
        "unknown": "default",
    },
    "aging_bucket": {
        "current": "not yet due",
        "not_due": "not yet due",
        "0": "not yet due",
        "1-30": "1-30 days",
        "31-60": "31-60 days",
        "61-90": "61-90 days",
        "over 90": "over 90 days",
        "90+": "over 90 days",
    },
    "object_type": {
        "purchaseorder": "Purchase order",
        "salesorder": "Sales order",
        "assemblyorder": "Assembly order",
        "salesinvoice": "Sales invoice",
        "supplierinvoice": "Supplier invoice",
        "purchaseinvoice": "Purchase invoice",
        "salescreditmemo": "Credit note",
        "purchasecreditmemo": "Purchase credit note",
        "calendarevent": "Meeting",
        "teamsmessage": "Teams message",
        "certificateofanalysis": "Certificate of analysis",
        "mcapproval": "Mission Control approval",
        "mctask": "Mission Control task",
        "plannertask": "Planner task",
    },
}

_ALERT_CODE_LABELS: dict[str, str] = {
    "po_late": "Late purchase order",
    "po_due": "Purchase order due soon",
    "po_undated": "Purchase order missing due date",
    "unconfirmed_po": "Unconfirmed purchase order",
    "vendor_no_reply": "Vendor not replying",
    "eta_slipped": "Delivery date slipped",
    "customer_order_late": "Late customer order",
    "customer_order_at_risk": "Customer order at risk",
    "customer_order_open_long": "Customer order open longer than usual",
    "customer_order_slower_than_usual": "Customer order slower than usual",
    "customer_demand_drop": "Customer demand drop",
    "customer_price_drop": "Customer price drop",
    "price_below_cost": "Price below cost",
    "price_below_typical": "Price below typical",
    "negative_margin": "Negative margin",
    "open_quote_aging": "Aging open quote",
    "quote_accepted_no_order": "Accepted quote without order",
    "lot_on_hold": "Batch on quality hold",
    "lot_near_expiry": "Lot nearing expiry",
    "quality_hold_aging": "Aging quality hold",
    "qc_hold": "Quality hold",
    "batch_hold": "Batch hold",
    "order_hold": "Order hold",
    "needs_my_reply": "Needs your reply",
    "reply_owed": "Reply owed",
    "quantity_mismatch": "Quantity mismatch",
    "certificate_expiring": "Certificate expiring",
    "overdue_receivable": "Overdue receivable",
    "unit_cost_jump": "Unit cost jump",
    "stock_out_risk": "Stock-out risk",
    "reorder_soon": "Reorder soon",
}

_CAMEL_NOUNS: dict[str, tuple[str, str]] = {
    "PurchaseOrder": ("purchase order", "purchase orders"),
    "SalesOrder": ("sales order", "sales orders"),
    "AssemblyOrder": ("assembly order", "assembly orders"),
    "SalesInvoice": ("sales invoice", "sales invoices"),
    "SupplierInvoice": ("supplier invoice", "supplier invoices"),
    "PurchaseInvoice": ("purchase invoice", "purchase invoices"),
    "Customer": ("customer", "customers"),
    "Supplier": ("supplier", "suppliers"),
    "Batch": ("batch", "batches"),
    "Lot": ("lot", "lots"),
    "Product": ("product", "products"),
    "Shipment": ("shipment", "shipments"),
    "Receipt": ("receipt", "receipts"),
}

_SYSTEM_WORDS_INLINE = re.compile(
    r"\b(bc|fm|portal|sage)\s+says\b", re.IGNORECASE
)
_SYSTEM_NAMES = {"bc": "BC", "fm": "FileMaker", "portal": "Portal", "sage": "Sage"}


def _unescape_xml(match: re.Match) -> str:
    units = re.findall(r"_x([0-9A-Fa-f]{4})_", match.group(0))
    return b"".join(int(u, 16).to_bytes(2, "big") for u in units).decode(
        "utf-16-be", errors="replace"
    )


def clip_words(text: str, max_len: int) -> str:
    """Truncate ``text`` at a word boundary without leaving dangling punctuation."""
    if max_len <= 0 or len(text) <= max_len:
        return text
    cut = text[:max_len].rstrip()
    if max_len < len(text) and not text[max_len].isspace():
        space = cut.rfind(" ")
        if space >= max_len // 2:
            cut = cut[:space]
    cut = cut.rstrip(" ,;:-/(·")
    return f"{cut}..." if cut else text[:max_len].rstrip()


def fix_po_prefix(word: str, number: Any) -> str:
    """``fix_po_prefix('PO', 'PO100587')`` -> ``'PO100587'``; ``('PO', '106001')`` -> ``'PO 106001'``."""
    text = clean_text(number)
    if not text:
        return word
    if re.match(rf"^{re.escape(word)}(?:[\s#-]*[A-Za-z0-9]|\b)", text, re.IGNORECASE):
        return text
    return f"{word} {text}".strip()


def clean_person(value: Any) -> Optional[str]:
    """Turn a raw sender/person field (Teams JSON blob, GUID, relay email, etc.) into a
    clean human name or address; None when empty or an opaque identifier."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in _NONE_LITERALS:
        return None
    if raw.startswith("{") and raw.endswith("}"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            name = str(parsed.get("display_name") or parsed.get("name") or "").strip()
            if name and not _BARE_GUID_RE.fullmatch(name):
                return clean_text(name) or None
            return None
    if _BARE_GUID_RE.fullmatch(raw):
        return None
    if _GOOGLE_GROUP_CAL_RE.fullmatch(raw):
        return None
    if _AMAZON_RELAY_RE.fullmatch(raw):
        return "Amazon Marketplace"
    if raw.startswith("@") and "." in raw and " " not in raw:
        raw = raw[1:]
    cleaned = clean_text(raw)
    if not cleaned or cleaned.lower() in _NONE_LITERALS:
        return None
    return cleaned


def clean_location(value: Any) -> Optional[str]:
    """Turn a calendar location holding raw Teams/Meet/Zoom URL query soup into a crisp label."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in _NONE_LITERALS:
        return None
    provider = None
    if _TEAMS_JOIN_URL_RE.search(raw):
        provider = "Microsoft Teams"
        raw = _TEAMS_JOIN_URL_RE.sub("", raw)
    elif _MEET_URL_RE.search(raw):
        provider = "Google Meet"
        raw = _MEET_URL_RE.sub("", raw)
    elif _ZOOM_URL_RE.search(raw):
        provider = "Zoom"
        raw = _ZOOM_URL_RE.sub("", raw)
    raw = _LONG_URL_RE.sub("", raw)
    rest = clean_text(raw).strip(" ;,|-")
    if rest and provider and provider.lower() not in rest.lower():
        return f"{rest} ({provider})"
    return rest or provider


def clean_enum(value: Any, kind: str = "") -> Optional[str]:
    """Map an internal enum/status/code value to crisp human English."""
    if value is None:
        return None
    raw = clean_text(value)
    if not raw:
        return None
    low = raw.lower()
    table = _ENUM_LABELS.get(kind)
    if table and low in table:
        return table[low]
    if "_" in raw and re.fullmatch(r"[A-Za-z0-9_]+", raw):
        words = re.sub(r"_+", " ", raw).strip().lower()
        return words[:1].upper() + words[1:] if words else raw
    if raw.isupper() and len(raw) > 4 and raw.isalpha():
        return raw[:1] + raw[1:].lower()
    return raw


def clean_alert_label(rule: Any, label: Any = None, kind: Any = None) -> str:
    """Human-readable label for an alert rule/event type; never emits a raw snake_case code."""
    for candidate in (label,):
        if isinstance(candidate, str) and candidate.strip():
            txt = clean_text(candidate)
            if txt and not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", txt):
                return txt
    for code in (rule, label, kind):
        if not isinstance(code, str) or not code.strip():
            continue
        key = code.strip().lower()
        if key in _ALERT_CODE_LABELS:
            return _ALERT_CODE_LABELS[key]
        if "_" in key:
            words = re.sub(r"_+", " ", key).strip()
            return words[:1].upper() + words[1:] if words else "Alert"
    return "Alert"


def clean_number_text(value: Any, *, max_decimals: int = 4) -> Optional[str]:
    """Clean a numeric string: strips binary float residue while keeping exact stated numbers."""
    if value is None or isinstance(value, bool):
        return None
    from . import presentation
    raw = format(value.normalize(), "f") if isinstance(value, Decimal) else str(value).strip()
    if not raw:
        return None
    raw = presentation.without_float_noise(raw)
    try:
        dec = Decimal(raw)
    except InvalidOperation:
        return raw
    if not dec.is_finite():
        return None
    text = format(dec, "f")
    if "." in text:
        whole, frac = text.split(".", 1)
        if len(frac) > max_decimals:
            trimmed = frac.rstrip("0")
            if len(trimmed) > max_decimals:
                dec = dec.quantize(Decimal(10) ** -max_decimals, rounding=ROUND_HALF_UP)
                text = format(dec, "f").rstrip("0").rstrip(".")
            else:
                text = f"{whole}.{trimmed}" if trimmed else whole
    return text


def clean_money_amount(value: Any, *, max_decimals: int = 2) -> Optional[str]:
    """Clean a monetary amount string: preserves exact figures up to 4 places (e.g. '100',
    '112.50', '0.4491'), strips binary float residue, and rounds noisy >4-decimal floats."""
    if value is None or isinstance(value, bool):
        return None
    from . import presentation
    raw = str(value).replace(",", "").strip()
    if not raw:
        return None
    raw = presentation.without_float_noise(raw)
    try:
        dec = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite():
        return None
    text = format(dec, "f")
    if "." in text:
        whole, frac = text.split(".", 1)
        if len(frac) > max(4, max_decimals):
            trimmed = frac.rstrip("0")
            if len(trimmed) <= 2:
                text = f"{whole}.{(trimmed + '00')[:2]}"
            elif len(trimmed) <= max_decimals:
                text = f"{whole}.{trimmed}"
            else:
                quant = Decimal(10) ** -max_decimals
                q = dec.quantize(quant, rounding=ROUND_HALF_UP)
                text = format(q, "f")
        elif len(frac) > max_decimals and frac.endswith("0"):
            trimmed = frac.rstrip("0")
            keep = max(2, len(trimmed))
            text = f"{whole}.{(trimmed + '00')[:keep]}"
    return text


def clean_text(value: Any, *, max_len: Optional[int] = None, strip_email_noise: bool = False) -> str:
    """Clean user-facing prose: unescape HTML/XML, remove tags/boilerplate/em dashes/GUIDs/
    double prefixes/float residue, normalize whitespace, and optionally word-clip."""
    if value is None:
        return ""
    if isinstance(value, dict) and "untrusted_text" in value:
        value = value.get("untrusted_text")
        if value is None:
            return ""
    text = str(value)
    if not text.strip():
        return ""
    # Unescape XML _xHHHH_ and HTML entities.
    text = _XML_ESCAPE_RE.sub(_unescape_xml, text)
    if "&" in text:
        text = html.unescape(text)
    # Strip style/script blocks, Teams <at>/<attachment>, CID tags, and HTML tags.
    if "<" in text:
        text = _STYLE_SCRIPT_RE.sub(" ", text)
        text = _TEAMS_AT_RE.sub(r"\1", text)
        text = _TEAMS_ATTACH_RE.sub(" ", text)
        text = _HTML_TAG_RE.sub(" ", text)
    text = _CID_TAG_RE.sub(" ", text)
    # Zero em dashes; normalize control chars and non-breaking/zero-width spaces.
    text = text.replace("\u2014", " - ").replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[\r\n\t]+", " ", text)
    if strip_email_noise:
        text = _ZENDESK_REPLY_RE.sub("", text)
        text = _CAUTION_BANNER_RE.sub("", text)
        text = _MEETING_BOILER_RE.sub("", text)
        text = _REPLY_HEADER_RE.sub("", text)
        text = _GITHUB_FOOTER_RE.sub("", text)
        text = _BROWSER_VIEW_RE.sub("", text)
        text = _LONG_URL_RE.sub("", text)
    # Clean raw Not-junk GUID wrapper, Teams GUID-as-sender, raw ontology IDs and handles.
    text = _NOT_JUNK_GUID_RE.sub(r"\1", text)
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s+is waiting on a reply\b",
        "Someone is waiting on a reply",
        text,
        flags=re.IGNORECASE,
    )
    text = _RAW_OID_RE.sub(
        lambda m_: _ENUM_LABELS["object_type"].get(m_.group(0).split(":", 1)[0].lower(), "record"),
        text,
    )
    text = _RAW_URI_RE.sub("", text)
    text = _DOUBLE_APPROVAL_RE.sub("Approval requested for", text)
    text = _DOUBLE_PO_RE.sub(r"\1", text)
    text = _DOUBLE_SO_RE.sub(r"\1", text)
    text = _EMPTY_QUOTED_PREFIX_RE.sub("wrote (no subject)", text)
    text = _CAMEL_COUNT_RE.sub(
        lambda m_: f"{m_.group(1)} {_CAMEL_NOUNS[m_.group(2)][0 if m_.group(1) == '1' else 1]}",
        text,
    )
    text = _SYSTEM_WORDS_INLINE.sub(
        lambda m_: f"{_SYSTEM_NAMES[m_.group(1).lower()]} says", text
    )
    text = _PLAIN_AMOUNT_CUR_RE.sub(
        lambda m_: f"{Decimal(m_.group(1)):,.2f} {m_.group(2)}", text
    )
    text = re.sub(r"\b1\s+days\b", "1 day", text)
    text = re.sub(r"\b1\s+orders\b", "1 order", text)
    text = _HYPHEN_RUN_RE.sub(" - ", text)
    text = _EMPTY_PARENS_RE.sub("", text)
    text = _DOUBLE_SEP_RE.sub(" · ", text)
    text = _DOUBLE_COMMA_RE.sub(", ", text)
    text = _TRAILING_DANGLING_ON_RE.sub(r"\1", text)
    from . import presentation
    text = presentation.without_float_noise(text)
    text = presentation.counted_words(text)
    text = presentation.SPACED_PERCENT.sub(r"\1%", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"^(?:·\s*)+|(?:\s*·)+$", "", text).strip()
    if max_len is not None and len(text) > max_len:
        text = clip_words(text, max_len)
    return text


def clean_subject(value: Any, *, fallback: str = "", max_len: Optional[int] = 200) -> str:
    """Clean a title or email/meeting subject without stripping meaningful single Re:/Fw:."""
    if value is None:
        return fallback
    text = str(value).strip()
    synced = _SYNCED_INVITE_RE.match(text)
    if synced:
        text = synced.group(1).strip()
    text = clean_text(text, strip_email_noise=False)
    if _STACKED_PREFIX_RE.match(text):
        first = text.split(":", 1)[0].strip().capitalize()
        rest = _STACKED_PREFIX_RE.sub("", text).strip()
        text = f"{first}: {rest}" if rest else ""
    if not text or _BARE_REPLY_PREFIX_RE.fullmatch(text) or text.lower() in _NONE_LITERALS:
        return fallback
    if max_len is not None and len(text) > max_len:
        text = clip_words(text, max_len)
    return text or fallback


def join_clean(parts: Iterable[Any], sep: str = " · ") -> str:
    """Join non-empty, cleaned parts without case-insensitive duplicates or trailing separators."""
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = clean_text(part)
        if not cleaned or cleaned.lower() in _NONE_LITERALS:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return sep.join(out)

# On the read worker's queue ahead of page and section reads: it is the first thing the
# reader sees and costs one card read.
CORE_PRIORITY = -1
# Home's enrichment reads over the old alert engine (suggestion cards, the replies list,
# the approvals queue: 35-75 s cold each, 11-18): queued after every other page read, so
# a screen a person waits on (Home's own list, My work, a record) is never behind them.
# Each is a long read too (``long=True``): it runs on all but one read worker at once.
ENRICH_PRIORITY = 1


def core_page(backend: Any, view: Any, object_type: str, object_id: str) -> m.ObjectPage:
    """The record's core page for this reader, kept (``read_core``); when this reader's
    whole page is already kept (every section merged), that page itself (``core`` false,
    ``complete`` as kept), so the app asks for no section. RecordNotFound when the route
    names nothing this reader may see."""
    from .backend import full_page, page_key, page_oid
    from .page_parts import page_handles

    with view._request():
        oid = page_oid(backend, view, object_type, object_id)
        whole = page_key(view, oid)
        if whole is not None and backend.page_cache.get(whole) is not None:
            return backend.page_cache.kept(view, whole, lambda: full_page(backend, view, oid),
                                           page_handles,
                                           copy=lambda model: model.model_copy(deep=True))
        key = page_key(view, ("core", oid))
    return backend.page_cache.kept(view, key, lambda: read_core(backend, view, oid),
                                   page_handles,
                                   copy=lambda model: model.model_copy(deep=True))


def read_core(backend: Any, view: Any, oid: str) -> m.ObjectPage:
    """The card's title and facts, each citing a source this reader may open; nothing the
    walk decides (status, timeline, links, documents, what was not found, conflicts) is
    said: the status is "reading" until the quick read replaces the page."""
    from . import presentation
    from .backend import card_facts
    from .page_parts import page_handles, raw_type_label

    page = view.card(oid)   # its own request; RecordNotFound when not visible
    if page.get("status") != "card":
        # the card could not be read: no core page (the quick read says why)
        raise CoreUnread(oid)
    kind = str(((page.get("card") or {}).get("object") or {}).get("object_type") or "Object")
    with view._request():
        label, facts, _values, _currency = card_facts(backend, view, page, core=True)
        type_label = raw_type_label(kind)
        clean_lbl = clean_subject(label, fallback=type_label or clean_enum(kind, "object_type") or kind, max_len=200)
        model = m.ObjectPage(
            object_id=oid, type=kind, type_label=type_label, label=clean_lbl,
            summary=type_label or clean_enum(kind, "object_type") or kind,
            status=m.ObjectStatus(state="reading", text=""),
            facts=facts, complete=False, core=True,
            freshness=backend.freshness(page, handles=page_handles(facts)))
    return presentation.polished(model)


class CoreUnread(UserFacing):
    """The record's card returned an error: no core page; the page's quick read is the
    answer (the app keeps waiting for it)."""

    def __init__(self, oid: str) -> None:
        super().__init__("core_unread", "This record's details are still being read.", 503)


class CorePageMixin:
    """``WorkspaceBackend.object_core``."""

    def object_core(self, reader: Any, object_type: str, object_id: str) -> m.ObjectPage:
        """The record's core page (``core_page``) on the read worker, ahead of the page's
        other reads; one served under a full-read grant is labelled and journalled."""
        from .backend import PAGE_TIMEOUT_SECONDS
        page = self.reads.run(reader, lambda view: core_page(self, view, object_type,
                                                             object_id),
                              timeout=PAGE_TIMEOUT_SECONDS, priority=CORE_PRIORITY)
        return self._labelled(reader, page, "/api/object")
