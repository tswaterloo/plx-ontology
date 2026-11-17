"""The claim grammar of ``operating.claim_checks``: roles, failure codes, the number, date,
label and unit patterns, and the word lists the checks read."""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Callable, Mapping

from ontology.units import RECEIVE_ALIASES, UOM_ALIASES, UOM_CANONICAL

from . import quantity_types as _qt
from .answer_contract import NUMERIC_OPS
from .citation_schemes import HANDLE_PREFIX

SCHEMA = "plx-claim-checks/v2"
FAILURE_CODES = ("claim_uncited", "claim_changed_since", "claim_value_mismatch",
                 "claim_role_mismatch", "claim_role_unverified", "claim_unit_mismatch",
                 "claim_unstructured", "claim_value_unstated", "claim_count_lower_bound",
                 "claim_currency_mismatch", "claim_wording_role_mismatch",
                 "claim_wording_role_unverified", "claim_prose_unchecked")
QUANTITY_ROLES = _qt.QUANTITY_TYPES
OTHER_ROLES = frozenset({"identifier", "price", "amount", "rate", "ratio", "attribute"})
ROLES = QUANTITY_ROLES | OTHER_ROLES
# A claimed type that a contract role also satisfies.
_ROLE_ALIASES = _qt.TYPE_ALIASES
MAX_CLAIMS = 40
# The wording role of a calculation result the type algebra leaves untyped.
UNTYPED_CALCULATION = "<untyped calculation>"

# Numbers, identifiers, dates and handles: shared with ``playbook_eval`` (one definition).
# Numbers take an optional sign only when not glued to a preceding word/digit, and keep
# a scale suffix (``scale``): a percent sign or word (``%``, ``percent``, ``per cent``,
# ``pct``), or a glued k/K/M (``10k``, ``2.5M``; ``10kg`` is ten and a unit).
# ``number_value`` applies it.
NUMBER = re.compile(
    r"(?:(?<![\w.])|(?<=[A-Za-z]\.))(?:(?P<sign>[-+−])(?=[\d.]))?"
    r"(?:(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?|(?P<lead>\.\d+))"
    r"(?P<scale>[ \u00a0]?(?:%|[Pp]er ?cent\b|[Pp]ct\b)|[kKM](?!\w))?")
_PERCENT_WORDS = frozenset({"%", "percent", "per cent", "pct"})
_SCALES_OF_SUFFIX = {**dict.fromkeys(_PERCENT_WORDS, Decimal("0.01")), "k": Decimal(1000),
                     "m": Decimal(1000000)}
_PERCENT_UNITS = frozenset({"%", "percent", "pct"})
# A percent sign or word right after a number in words ("twenty-five percent").
_PERCENT_AFTER = re.compile(r"[ \u00a0]?(?:%|per ?cent\b|pct\b)", re.I)
# A comma inside a number is a thousands separator only between groups of three digits.
_THOUSANDS = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d*)?")
# Identifiers: 2-8 capitals, optional hyphen, four or more digits, then letters/digits and
# hyphen segments that hold a letter or only digits (AO-26005B, OR1000151885, PO12172025,
# DVS-5003-3.78L-A, SO-2026-0003, also before a sentence's full stop).
_IDENTIFIER_TAIL = (r"[A-Z0-9]*(?:-(?:(?=[A-Z0-9.]*[A-Z])[A-Z0-9.]+|\d+(?!\d|\.\d)))*"
                    r"(?![A-Za-z0-9_])")
IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])[A-Z]{2,8}-?\d{4,}" + _IDENTIFIER_TAIL)
# A label: the identifier shape with one or more capitals and any number of digits (Q3,
# P-01234, F-26-0001, BC-12345D, OR1000000001, SKU-1509), never followed by a decimal part
# (``V1.5`` is a version, ``_GLUED``). The final reply screen (``answer_gate.unbacked``)
# reads each as one token a passed value, the question or its numbers must back. The
# gate's answer-text check skips only the narrower gate labels (``_is_gate_label``:
# hyphenated or an identifier kind's shape, never after a quantity, unit or currency
# word); a claim's own text skips none. A lower-case prefix (``x3``, a multiplier) and a
# currency code (``USD1200``, an amount) make no label.
LABEL = re.compile(r"(?<![A-Za-z0-9_.])[A-Z]{1,8}-?\d+" + _IDENTIFIER_TAIL + r"(?![.,]\d)")
_LABEL_PREFIX = re.compile(r"[A-Z]+")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
HANDLE = re.compile(HANDLE_PREFIX + r"\S+")
# A result-set handle (``ontology_tools`` mints ``set:`` + 24 url-safe characters; a gap's
# ``missing`` names one): random characters, never a number or date the text states
# ("set:2kYv..." is no claim of 2).
SET_HANDLE = re.compile(r"(?<![A-Za-z0-9_-])set:[A-Za-z0-9_-]{16,}")
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
          r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATE_WORDS = re.compile(
    r"\b" + _MONTH + r"\.?\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?(?:,?\s+\d{4})?\b"
    r"|\b\d{1,2}\s+" + _MONTH + r"\b(?:,?\s+\d{4})?", re.I)
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
# A month-name date with its year ("Jan 7, 2026", "7 January 2026"): states that day.
_MONTH_DATE = re.compile(
    r"\b(?P<mon>" + _MONTH + r")\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})\b"
    r"|\b(?P<d2>\d{1,2})\s+(?P<mon2>" + _MONTH + r")\.?,?\s+(?P<y2>\d{4})\b", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# An ISO date, or timestamp with an optional offset (``Z``, ``+hh:mm``; Postgres also
# renders ``+hh`` and up to nine fraction digits): ``_moment``.
_MOMENT = (r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?:[T ](?P<H>\d{2}):(?P<M>\d{2})"
           r"(?::(?P<S>\d{2})(?P<f>\.\d{1,9})?)?(?P<tz>Z|[+-]\d{2}(?::?\d{2})?)?)?")
_ISO_MOMENT = re.compile(_MOMENT)
_YYYYMMDD = re.compile(r"\d{8}")
# One written in prose: not inside a longer token (``SO-2026-01-07``, ``2026-01-071``, the
# lot code ``2026-01-07-A``).
_PROSE_MOMENT = re.compile(r"(?<![\w.:-])" + _MOMENT + r"(?!\w|-\w|[:.]\d)")
# A "page(s)" phrase and its run of numbers ("page 3", "pages 3 and 11"). Only numbers of
# the run that name a page the item cites (original://...#page=N) are locators; the run
# ends at the first other number, which stays a quantity (``_page_locators``).
# Only the words "page" / "pages": abbreviations (p., pp., pg) stay quantities needing a
# claim.
_PAGE_PHRASE = re.compile(r"\b(?P<word>pages?)\s+(\d+(?:\s*(?:,|and|to|-|–)\s*\d+)*)", re.I)
# Words whose period is an abbreviation (no sentence end for the binder) only when a digit
# follows it directly or after one space ("p. 4", "No. 3"); "Line E. The ..." keeps its end.
_ABBREVIATIONS = frozenset({"p", "pp", "pg", "pgs", "no", "nos", "vs", "fig", "ref", "approx",
                            "ca", "qty"})
_DIGIT_NEXT = re.compile(r" ?\d")
# "i.e." / "e.g." as whole tokens (their inner and final periods end no sentence).
_IE_EG = re.compile(r"\b(?:i\.e|e\.g)\.", re.I)
_PAGE_CITATION = re.compile(r"^original://[^#\s]+#(?:[^#\s]*&)?page=([1-9][0-9]*)$")
# The field of a claim on an original page citation: the figure is in its machine-read text.
PAGE_TEXT = "text"
# Citations of originals, and their fields that hold the original's (or part's) re-read
# text: a text value claimed on one is a quote (``_check_quote``). A Teams channel or chat
# message's ``body_text`` is one.
ORIGINAL_SCHEMES = ("original", "sharepoint", "graph-mail", "teams-channel", "teams-chat")
ORIGINAL_TEXT_FIELDS = frozenset({"row.text", "row.content_text", "row.body_text"})
# Free-text fields of any record (a SharePoint file's text, a mail body, a Mission Control
# task's description, a note): a field whose last segment is one of these holds prose,
# never one structured value (``text_field``). A number claimed on one is a figure of
# that prose and a text claimed on one, when it is not the whole cell, a quote of it
# (``_check_text_figure``, ``_check_quote``).
TEXT_FIELD_NAMES = frozenset({"text", "content_text", "body_text", "body", "description",
                              "notes", "note", "comment", "comments", "remarks", "memo"})
# What makes a number of prose no figure of a value (``_text_figures``), checked around
# it in the text. A bound or an approximation before it ("at least 30", "max. 30", "up
# to 30", "about 30", "≥ 30") or after it, after at most one word ("30 max", "30 cases
# or fewer", "30+"): the figure is not the value.
_BOUND_BEFORE = re.compile(
    r"(?:\b(?:at (?:least|most)|(?:no|not) (?:more|less|fewer|greater|higher|lower) than|"
    r"(?:more|less|fewer|greater|higher|lower) than|over|under|above|below|up to|"
    r"upwards of|in excess of|(?:min|max)(?:imum)?\.?(?: of)?|within|about|around|"
    r"approx(?:imately)?\.?|roughly|nearly|almost|some|circa|ca\.)|[<>≤≥~±])[  ]*$",
    re.I)
_BOUND_AFTER = re.compile(
    r"(?:\+|(?:[  ]+[A-Za-z]+)?[  ]*(?:or (?:more|less|fewer|above|over|under|"
    r"below|so|greater|higher|lower)|at (?:most|least|the most)|(?:min|max)(?:imum)?|"
    r"and (?:up|above|over|more)|plus)\b)", re.I)
# A range: "20-30", "20 – 30", "20 to 30", "between 20 and 30": neither end is the value.
_RANGE_BEFORE = re.compile(r"(?:\d[  ]*(?:-|–|—|to)[  ]*|"
                           r"\bbetween[  ]+(?:\S+[  ]+and[  ]+)?)$", re.I)
_RANGE_AFTER = re.compile(r"[  ]*(?:-|–|—|to)[  ]*\d", re.I)
# A negator before a figure that `_NEGATOR` does not cover ("excluding 30", "except 30",
# "other than 30"), and a negating answer after its clause's words ("30 cases? No.").
_EXCLUDED_BEFORE = re.compile(r"\b(?:excluding|exclude[sd]?|except|other than|apart from|"
                              r"instead of|rather than|not including)[  ]*$", re.I)
_ANSWERED_NO = re.compile(r"[^.?!;\n]{0,40}?[  ]*[?:=][  ]*(?:no|not(?: yet)?|never|"
                          r"false)(?=$|[ .,;:!?)\]])(?! [\w(])", re.I)
# A number that is a label's number, not a quantity: after an id label of the allow-list
# with or without its delimiters ("SKU 4410", "SKU: 4410", "Order #: 4521", "Invoice No.
# 4521", "Lot - 5", "page 3"), or after one "#" (a Markdown heading's run, "### 15%
# deposit", is no label) or "No."; a glued or joined token ("ABC12345", "10/14/2026",
# "905-555-0187", "1st") is no figure either (``_joined``). An unknown capitalised word
# is no label.
_ID_LABEL_BEFORE = re.compile(
    r"(?:(?:^|(?<=[^\w.]))(?P<label>"
    # Record and document numbers (the review's allow-list).
    r"sku|p\.?o|inv|invoice|order|s\.?o|ao|batch|lot|account|acct|ref|reference|item|"
    r"part|quote|quotation|case|ticket|job|rro|qrm|vendor|customer|task|receipt|"
    r"shipment|container|tracking|bol|b/l|pi|so|mo|code|"
    # Locators, versions, contacts and addresses.
    r"pages?|pg|pp?|steps?|stages?|line|row|sections?|clauses?|rule|table|"
    r"figure|fig|chapter|gap|phase|question|q|version|ver|v|rev|revision|"
    r"model|serial|phone|tel|fax|mobile|cell|ext|extension|id|room|bay|aisle|shelf|bin|"
    r"dock|door|zone|form|route|store|site|suite|unit|apt|zip|zip code|postal|"
    r"postal code|postcode)\.?"
    # The label's own number word and delimiters ("Order #:", "Invoice No.", "Lot:").
    r"(?:[  ]*(?:no|nr|num|number)\b\.?)?[  ]*(?:[:#\-]+[  ]*)*"
    # A bare "#" names a number; a run of them is a Markdown heading ("### 15% deposit").
    r"|(?:^|(?<=[^\w#]))(?:#|no\.|nr\.)[  ]*:?[  ]*)$", re.I)
# A quantity label is never an id label: "TOTAL QTY 1513", "CARTONS 245", "GROSS WT".
_QUANTITY_LABELS = frozenset({
    "total", "qty", "quantity", "cartons", "carton", "cases", "pcs", "pieces", "units", "ea",
    "each", "gross", "net", "wt", "weight", "kg", "lb", "lbs", "pallets", "boxes", "amount",
    "subtotal", "balance", "count", "yield"})
# A street number ("123 Main St"), a state code before a zip ("NY 10001").
_STREET_AFTER = re.compile(r"(?:[  ]+[A-Z][\w'.-]*){1,3}[  ]+(?:St|Street|Ave|"
                           r"Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|Hwy|Highway|"
                           r"Ct|Court|Pl|Place|Pkwy|Parkway|Cres|Crescent|Suite)\b")
# A heading label: one of these words and a number with a colon after it at a clause or line
# start ("Release 2:", "Step 3:", "Line 5:"). It numbers what follows, so its number is no
# figure. The colon is required (then white space or the end) and the word must start a
# clause: the text's or a line's start, or after ";" or ".". Any other word keeps its number
# ("Received 3: ok", "Qty 2:").
HEADING_WORDS = ("release", "step", "item", "line", "part", "section", "phase", "stage",
                 "option", "round", "version", "revision", "page")
_HEADING_LABEL = re.compile(r"(?:^|(?<=[\n;.]))[ \t]*(?P<word>" + "|".join(HEADING_WORDS)
                            + r")[ \t]+(?P<number>\d+)[ \t]*:(?=\s|$)", re.IGNORECASE)
_LAST_WORD = re.compile(r"([A-Za-z]+)[ \u00a0]*[:#]?[ \u00a0]*$")
_STATE_BEFORE = re.compile(r"\b[A-Z]{2}[  ]+$")
_PHONE = re.compile(r"(?<!\w)(?:\+\d{1,3}[ . -]?)?\(?\d{3}\)?[ . -]?\d{3}[ . -]"
                    r"\d{4}(?!\w)")
_TOKEN_JOINER = frozenset("-/\\.:_@#–—")
# A scale word after a figure multiplies it ("1.5 million", "12 thousand", "3 dozen").
_SCALE_WORDS = {"hundred": Decimal(100), "thousand": Decimal(1000), "dozen": Decimal(12),
                "million": Decimal(10) ** 6, "mn": Decimal(10) ** 6, "mln": Decimal(10) ** 6,
                "billion": Decimal(10) ** 9, "bn": Decimal(10) ** 9,
                "trillion": Decimal(10) ** 12}
_SCALE_AFTER = re.compile(r"[  ]*(" + "|".join(_SCALE_WORDS) + r")\b", re.I)
_COMPOUND = re.compile(r"-([a-z]{2,})\b")
_ORDINAL = re.compile(r"(?:st|nd|rd|th)\b", re.I)
# A currency printed right before a prose figure ("USD 1,710.00", "US$10,800", "€5") or
# right after it ("1,710.00 USD"). A bare "$" names no single currency.
_CURRENCY_BEFORE = re.compile(r"(?:\b(?P<code>[A-Z]{3})[  ]?|(?P<sign>(?:US|CA|C|AU|A|NZ|"
                              r"HK|S)?\$|€|£|¥)[  ]?)$")
_CURRENCY_AFTER = re.compile(r"[  ]?(?P<code>[A-Z]{3})\b")
_CURRENCY_SIGNS = {"US$": "USD", "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD",
                   "NZ$": "NZD", "HK$": "HKD", "S$": "SGD", "€": "EUR", "£": "GBP",
                   "$": "$", "¥": "¥"}
# Characters that join two word runs into one token (``re-quote``, ``1,509``, ``3.78L``,
# ``it's``, ``10:30``): a quote never starts or ends between them. A comma or a colon
# joins two digits only (``1,509``, ``10:30``); between other characters it separates
# (``app_quote,status`` in a CSV row, ``Status:Open``).
_JOINERS = frozenset("-.,/'\u2019:")
_DIGIT_JOINERS = frozenset(",:")
_WORD_CHAR = re.compile(r"\w")
_WORD_RUN = re.compile(r"\w+")
# A negator written directly before a quoted passage reverses it ("not approved", "never
# shipped", "didn't ship", "non-conforming", "no longer approved", "not yet shipped"): that
# occurrence supports no quote of the passage without it. Matched on the normalised text
# just before the occurrence.
_NEGATOR = re.compile(r"(?:\b(?:not|no|never|nor|cannot|without)(?: (?:longer|yet))? |"
                      r"\wn['\u2019]t(?: (?:longer|yet))? |"
                      r"\b(?:non|un)[-\u2010\u2011\u2012\u2013]?)$", re.I)
# "No" after a capitalised label is the abbreviation of number ("Lot No", "PO No") when
# an identifier-shaped token follows it: no negation.
_NUMBER_LABEL = re.compile(r"\b[A-Z][\w#]* N[oO] $")
# A negating answer right after the passage ("QC passed? No", "Approved: not yet"):
# a negator standing alone after at most one separator, before a clause end. "No." before
# a number with no separator is the abbreviation of number ("Lot No. 5"); after a
# separator it is the answer ("QC passed? No. 3 units failed").
_ANSWERS = r"no longer|not(?: yet)?|never|false"
_NEGATED_AFTER = re.compile(r"(?: ?[?:=\-\u2013\u2014] ?(?:" + _ANSWERS + r"|no)"
                            r"| ?(?:" + _ANSWERS + r"|no(?!\.? ?\d)))"
                            r"(?=$|[ .,;:!?)\]])(?! [\w(])", re.I)

# Names of standards, algorithms and short codes: 2-8 capitals, a hyphen, digits (SHA-256,
# ISO-8601, UTF-8, AO-77). A single-letter code (P-02458) is not one: in a claim's text,
# claim it as text; in an answer text it is a gate label (``_is_gate_label``).
# Prefixes whose hyphenated or glued digits are names, not quantities: standards and
# algorithms (SHA-256, ISO-8601, UTF-8, RFC-3339), PLX record codes (AO-77, SO-12, PO-5,
# OR-3, BC-9) and the case's own item ids (F2, G1, C3, P1, V1, L1). Any other letters
# glued or hyphenated to digits (SKU-1509, x1509) leave the digits a number, except a gate
# label in an answer text (``_is_gate_label``: SKU-1509, never NET30 or QTY-12).
KNOWN_ID_PREFIXES = frozenset({"SHA", "ISO", "UTF", "RFC", "AO", "SO", "PO", "OR", "BC", "MO",
                               "F", "G", "C", "P", "V", "L"})
_DIGITS_ONLY_ID = re.compile(r"([A-Z]{2,8})-?(\d+(?:-\d+)*)")
_CODE_NAME = re.compile(r"(?<![A-Za-z0-9_])([A-Z]{1,8})-?(\d+)(?![\d.]*\d)(?![A-Za-z0-9_])")
_GLUED = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z]{1,8})(\d+(?:\.\d+)?)(?![A-Za-z0-9_])")
_SMALL = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
          "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000}
_NUMBER_WORDS = re.compile(
    r"\b(?:(?:" + "|".join([*_SMALL, *_TENS, *_SCALES]) + r")(?:[- ](?:and[ ])?|\b))+", re.I)
_FIELD = re.compile(r"^(?:row|key|rows\.(?:0|[1-9][0-9]*))(?:\.[A-Za-z_][A-Za-z0-9_]*){1,3}$")
_COLUMN_FIELD = re.compile(r"^(?:row|key|rows\.(?:0|[1-9][0-9]*))\.([A-Za-z_][A-Za-z0-9_]*)$")
_CALC_ID = re.compile(r"^C[1-9][0-9]{0,5}$")
_ROLE_SHAPE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_PLAIN_NUMBER = re.compile(r"^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
# Columns whose values are names, not counts (id, idoe, assembly_item_id, po_number).
_ID_COLUMN = re.compile(r"^(?:id[a-z0-9]*|[a-z0-9_]*_id|[a-z0-9_]*(?:code|number|_no|_ref))$",
                        re.I)
# Columns whose values are status words (status, state, order_status, approvalState).
_STATUS_COLUMN = re.compile(r"(?:^|_)(?:[Ss]tatus|[Ss]tate|STATUS|STATE)$|[a-z0-9](?:Status|State)$")
_CLAIM_KEYS = frozenset({"value", "citation", "field", "calculation", "role", "unit", "op",
                         "of", "currency", "quote", "expression", "day"})
# A ``days`` claim over ONE cited date counts from a ``day`` it does not cite (x0 - day):
# a day the question writes in full or today (``returned["reference_days"]``,
# ``operating.time_window.reference_days``); any other day is refused.
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# A cited claim's ``quote``: the passage as written in the cited text that holds its value
# (``_quote_mismatch``). It adds a check and never replaces one.
MAX_QUOTE_CHARS = 500
# A value computed over cited rows in the claim itself (``op`` over ``of``): the number of
# distinct records cited, the number of distinct values a complete ``plx.aggregate``
# ``count_distinct`` counted over records that include them (``count_distinct``: exact or
# refused, never a bound), the Decimal sum of one field of each, or an ``expression`` over
# figures each input reads (``_check_expression``).
COUNT_OPS = ("count", "count_distinct")
# ``sum``, ``avg``, ``min``, ``max``, ``difference`` and ``ratio`` over one numeric field of
# each cited cell (``_check_value_claim``), recomputed by the rule the eval scorer uses
# (``answer_contract.computed_claim``).
# ``days``: the whole days from one cited date to another (x0 - x1: a receipt after its
# expected date, a due date after its invoice date), each re-read as a calendar day.
DAYS_OP = "days"
DAY_UNITS = frozenset({"day", "days", "d"})
# ``add_days``: the calendar day a whole number of days after a day: a cited date plus a
# cited number of days (x0 + x1: an order date plus its supplier's lead time), or a
# ``day`` the question writes in full, or today, plus one cited number of days (day + x0:
# an order placed on a day the question names). Its value is that ISO day (YYYY-MM-DD).
ADD_DAYS_OP = "add_days"
DAY_OPS = (DAYS_OP, ADD_DAYS_OP)
OPS = (*COUNT_OPS, *NUMERIC_OPS, "expression", *DAY_OPS)
MAX_OP_INPUTS = 100
MAX_VALUE_CHARS = 200
_OP_INPUT_KEYS = frozenset({"citation", "field", "unit"})
# An expression input: the figure as written (``value``) where its cited field holds it,
# pinned by an optional ``quote``, in an optional ``unit`` (``%``: a printed percent).
_EXPRESSION_INPUT_KEYS = frozenset({"citation", "field", "value", "quote", "unit"})
_EXPRESSION_FIGURE = re.compile(r"^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$")
MAX_EXPRESSION_CHARS = 500
# ``x<i>``, the i-th input of an expression claim, and ``round_to_cents(<expression>)``
# around the whole (half-up to 0.01): the eval scorer's names and rounding
# (``evals.score.ROUND_TO_CENTS``, ``_INPUT_NAME``), so both recompute one value.
EXPRESSION_INPUT = re.compile(r"\bx(?:0|[1-9][0-9]{0,2})\b")
ROUND_TO_CENTS = re.compile(r"^\s*round_to_cents\((.*)\)\s*$", re.S)
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# A number written as a lower bound: "at least 8", "no fewer than 8", "8 or more", "8+".
_AT_LEAST_BEFORE = re.compile(r"(?:\bat least|\bno (?:fewer|less) than|\bnot (?:fewer|less) "
                              r"than|\bminimum of|>=|≥)[  ]*$", re.I)
_AT_LEAST_AFTER = re.compile(r"\+|[  ]*or (?:more|above|over)\b", re.I)
# "a dozen", "half a dozen", "two dozen", "3 dozen" (not "dozens"): twelve each.
_DOZEN = re.compile(r"\b(?:(?P<half>half )?an? |(?P<times>[A-Za-z]+|\d+)[ -])dozen\b(?!s)",
                    re.I)
# What a number in words quantifies: the next words of its clause (a hyphen compound's too).
_CLAUSE_END = re.compile(r"[.;:!?,()\n]")
_LETTERS = re.compile(r"[A-Za-z]+")
# Words that name records of any kind: a count of any records is stated by them.
_RECORD_NOUNS = frozenset({"record", "row", "item", "entry", "document", "file", "message",
                           "email", "mail", "line"})
# Parts of a count's role or collection name that name no records (``receipt_count``,
# ``public.app_receipt``).
_NAME_FILLER = frozenset({"count", "number", "total", "num", "qty", "app", "api", "public",
                          "silver", "sage", "bc"})
# Words ending in s that are no plural noun.
_NOT_PLURAL = frozenset({"this", "thus", "plus", "does", "always", "perhaps", "whereas",
                         "sometimes", "besides", "towards", "afterwards", "series"})
# Nouns (as stems) that name no records: where records are held and spans of time. A
# number in words counting one of them (``held in two systems``) may be the count's own
# word in unrelated prose; any other noun (``three shipments``) may be the records.
_NOT_RECORD_NOUNS = frozenset({"system", "source", "database", "site", "location",
                               "company", "environment", "time", "minute", "hour", "day",
                               "week", "month", "year"})

Rows = Callable[[str], "Mapping[str, Any] | None"]
Contracts = Callable[[str, Mapping[str, Any], str], "Mapping[str, Any] | None"]


# A figure written rounded for display (``116.25`` for a cell holding the float
# ``116.25000000000001``): 2 to 4 decimals, the cell's value rounded half up to the
# written places. Never fewer than 2 (``116`` for ``116.25`` is no display rounding).
ROUNDED_PLACES = (2, 4)


# Units a page's machine-read text may print right after a figure ("1502 EA", "25 kg",
# "68,160 grams"), case-folded, by the unit they mean: the Portal's units and spellings
# (``ontology/units.py``, the one unit table: "cases" is cse, "kilograms" kg), the
# spellings a page uses that the Portal lists none for, and the percent words (the
# number's own scale). Assumption, not measured: a ton is the metric tonne (t).
_PAGE_UNITS = {**{unit: unit for unit in UOM_CANONICAL}, **UOM_ALIASES, **RECEIVE_ALIASES,
               **dict.fromkeys(("piece", "pieces"), "ea"),
               **dict.fromkeys(("kilo", "kilos", "kilogramme", "kilogrammes", "kgm"), "kg"),
               **dict.fromkeys(("gm", "gms"), "g"), "lbm": "lb",
               **dict.fromkeys(("t", "mt", "mts", "ton", "tons", "tonne", "tonnes",
                                "metric ton", "metric tons", "metric tonne", "metric tonnes"),
                               "t"),
               **dict.fromkeys(("litres", "liters", "ltr", "ltrs", "lts"), "l"),
               **dict.fromkeys(("millilitre", "millilitres", "milliliter", "milliliters",
                                "mls"), "ml"),
               **dict.fromkeys(("pk", "pks", "pkg", "pkgs"), "pack"),
               **dict.fromkeys(("bx", "bxs"), "box"),
               **dict.fromkeys(("pallet", "pallets", "plt", "plts"), "plt"),
               **dict.fromkeys(_PERCENT_UNITS, "percent")}
_KNOWN_UNITS = frozenset(_PAGE_UNITS.values())
_KNOWN_RATES = frozenset("/" + unit for unit in _KNOWN_UNITS if unit != "percent")
# The word right after a figure. A one-letter word (g, l, t) is a unit only standing
# alone: never inside a token or before a period ("7 t-shirts", "line 4 l.", "7 L/min").
_UNIT_AFTER = re.compile(r"[ \t\u00a0]*((?i:metric[ \t\u00a0]+ton(?:ne)?s?)\b|[A-Za-z]{2,}\b"
                         r"|[A-Za-z](?![\w'\u2019./-]))")


def _page_unit(unit: str) -> str:
    folded = " ".join(unit.casefold().split())
    return _PAGE_UNITS.get(folded, folded)


_ISO_DAY = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
