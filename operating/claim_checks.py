"""Checked value claims: the quantitative statements of a case, structured and verified.

A citation that resolves does not prove the sentence that cites it (requirements
RSN-06): "produced 1,509" citing an assembly-order item row is wrong when 1,509 is
that row's *planned* ``quantity``. Planned, produced, filled, inspected, ordered and
damaged quantities share a dimension but are different quantities (AO-26005B:
planned 1,509 / manufactured 1,503 / filled 1,502). So a finding (or gap) carries
**structured value claims** that the Workbench checks exactly against the rows; the
prose is then checked fail-closed against those claims. Models propose, the checker
decides, and no role a caller declared is taken on trust.

A value claim (``CaseStore.finding(..., value_claims=[...])``, MCP ``value_claims``)::

    {"value": "1509", "citation": "<one of the item's citations>", "field": "row.quantity",
     "role": "planned", "unit": "pc"}
    {"value": "6", "calculation": "C1", "role": "planned_not_produced", "unit": "EA"}
    {"value": "P-02458", "citation": "...", "field": "row.code"}        # a text value
    {"value": "3", "op": "count", "of": [{"citation": "..."}, ...], "role": "receipt_count"}
    {"value": "1260.50", "op": "sum", "of": [{"citation": "...", "field": "row.amount"}, ...],
     "currency": "USD"}

``value`` is an integer or a string (a decimal string is a number, anything else is
text); floats are refused, as for calculation literals. ``role`` is one of ``ROLES``
(quantity types plus non-quantity kinds such as ``identifier``) or, for a calculation,
a derived role name. An ``op`` claim computes its number over rows the item cites
(``of``, at most ``MAX_OP_INPUTS``): ``count`` over the distinct records cited (a mirror
row by relation and key, any other record by its re-read citation without version or
fragment; one record cited twice fails), ``sum`` the Decimal sum of one field of each,
checked as a recorded calculation is (recomputed from the re-read rows, typed by the type
algebra, every input in one unit, its own or the claim's, that no contract contradicts).
A count has no quantity type; its ``role``, like a sum's role outside ``ROLES``, is the
value's name only and types nothing.

A count is **exact** only when it binds to one complete count entry a tool returned to
this reader (``returned["counts"]``, ``returned_evidence``: a ``plx.aggregate`` count group
of a complete answer with no gap, listing every record it counts, taken now: a call naming
an ``as_known_at`` counts what was known then; that tool's identity lookup already left a
group with twins, one record held in two systems, without a value): the claimed value is
the entry's and every record the claim cites is one of the entry's. Any other count is a
**lower bound** (``bound: at_least``): the records it cites, where records of two source
collections (two relations, entity sets, mailboxes, drives) may be one record held twice
and so count once: the claimed value must be the most records it cites that are surely
distinct, those of one collection or those one count group a tool's identity lookup told
apart lists (a group with a value, taken now, complete or not: their deduplicated union),
and its item's text (and the close outcome) states it only as ``at least N`` / ``no fewer
than N`` / ``N or more`` (``claim_count_lower_bound``).
A ``sum`` of money carries the ``currency`` (ISO 4217) of its cited values, and one cited
number may state one: a value is money when a tool returned it as money
(``returned["currencies"]``, its currency or None) or the ontology schema types its column
money (``_money_columns``); a sum adding money whose currency no tool returned, two
currencies, money and non-money, or stating none or another, and a currency stated over
values no tool returned as money (a sum's unit, or an input's, naming an ISO 4217 currency
states it too: ``USD``, never ``BOX``), are ``claim_currency_mismatch``. A currency a tool returned as read on another record
(``currency_citation``: a blank code read as its company's local currency) holds only
while the gate re-reads that record for the reader and finds the same code there; a
reader who cannot read it gets ``claim_currency_mismatch`` too.

The gate (``check_items``), over the rows re-read for the reader and digest-checked:

* ``claim_uncited``: the claim's row is not readable now, or a calculation claim's
  calculation reads a row the item does not cite (or does not recompute from them);
* ``claim_changed_since``: the re-read is a changed-since view (``changed_since:
  true``: a live record whose version moved, a BC or mirror capture revision a later
  capture superseded, which keeps its row; ``CaseRuntime.claim_row``) for the claim's
  row or a calculation input row; a revision that changed is never verified, whatever the view's ``status``. A reader that
  answers as of an earlier moment clears the flag on a revision current then
  (``plx.investigate`` ``as_known_at``), which is then checked as usual;
* ``claim_value_mismatch``: the cited field (or calculation result) holds another value.
  A value is compared in its **form** (``value_form``): a number by value; a date
  (``2026-01-07``, or an ISO timestamp) as a date, so a bare date holds when the cell's
  calendar date as written is that date (``2026-01-07T23:30:00-05:00`` is the 7th, never
  read as a UTC day; a compact ``20260107`` is the 7th), or, when the column's source
  declares a time zone (its unit contract's ``time_zone``), the cell instant's day there;
  a timestamp only when the cell states the same instant (both with an offset, or both
  without); an enum (a column the ontology schema types
  ``enum``, or a status/state column) case-folded with ``_`` read as a space
  (``Revised`` holds ``REVISED``, ``pending approval`` ``PENDING_APPROVAL``); any other
  text (a name, a code) exactly, except that on a column the identifier grammar declares a
  carrier of one kind (``ontology/schema/identifiers.json``) the claim may be the canonical
  form of the identifier the cell holds (``PO1001`` on BC ``number`` ``PO-1001``, the form
  the projection stores; ``_same_identifier``). A text value cited on the re-read text of an original
  (an original page's ``text``; the ``ORIGINAL_TEXT_FIELDS`` of an ``ORIGINAL_SCHEMES``
  citation: a captured file's or attachment's extracted text, a message body, a delivered
  artifact's text) is a quote: it holds when the value occurs verbatim in that text, both
  NFKC- and whitespace-normalised, on token boundaries (``quoted_spans``), neither directly
  after a negator (``not approved``, ``no longer approved`` and ``not yet approved`` hold
  no ``approved``; ``No`` after a capitalised label before an identifier is the
  abbreviation of number: ``Lot No LOT-FXA``) nor answered by one after it (``QC passed?
  No``, ``QC passed? No. 3``; ``Lot No. 5`` is a number), and the quote is long enough to tell
  one passage from another (two word tokens, eight characters, or an identifier-shaped
  token such as ``LOT-FXA``; never ``PO`` or ``.``; ``supported_quotes``). A quote is
  untyped, as a page figure is, and so is every number in it for the wording check; a
  number claim keeps the field and page-figure rules;
* ``claim_value_unstated``: a passing text, enum or date claim (not a quote) whose item
  has text that does not state its value, in its form (a name verbatim on token
  boundaries and not negated, as a quote; an enum case-folded; a date as an ISO date or
  timestamp or a month-name date with its year that it covers, as below): a value claim backs only
  what its text says, so ``SO-1 is Released`` (or ``is not DRAFT``) over a correct claim
  that the status is ``DRAFT`` fails. An item without text (an eval answer's structured
  values) states nothing and is not checked;
* ``claim_role_mismatch``: the field's role contract (``source_unit_contracts.json``; for a
  BC row column the ontology schema's quantity role, ``schema_role``, the role the
  projection gives that value) says another quantity type, or the claim states no role
  for a field whose source contract pins one (``produced`` accepts ``manufactured`` and
  ``actual``); a claim with no role on a BC column stays untyped. A calculation's
  **quantity type** is derived by the closed type algebra of ``quantity_types`` over its
  expression tree, from the role contracts of its re-read inputs only (declared input
  roles, literal roles, relation names and operand order are never trusted): the
  expression simplified over the source cells it reads is typed ``T`` only when it is
  one ``T`` input, a sum of distinct ``T`` inputs, or ``T`` minus documented parts of
  ``T`` (``quantity_types.SUBTRACTIONS``: ``manufactured - damaged``); anything else
  (mixed sums, other differences, products, ratios, scalings, constants) is untyped, and
  identities cancel (``m + (p - m)`` is the planned ``p``). A relation's ``as`` and a
  derived claim role are **labels**: never a source role (``planned - damaged as
  produced`` is refused), and each must fit the form it names (``planned_not_produced``
  fits ``planned - actual``; ``produced_total`` cannot name ``m + (p - m)``,
  ``damaged_total`` cannot name ``planned + damaged``). A claimed quantity type must be
  the computed type (or its alias), a derived role the recomputed label;
* ``claim_role_unverified``: a quantity type (``QUANTITY_ROLES``) is claimed on a field
  that no role contract grounds, or on a calculation the algebra leaves untyped: never
  a clean pass;
* ``claim_unit_mismatch``: the stated unit differs from the contract's unit, the unit a
  calculation recorded for that cited field, or the calculation's result unit;
* ``claim_unstructured``: a number in the item's text (finding statement, gap
  ``missing``/``why``; the case's close outcome against every claim of the case) that no
  claim of the item covers with the same value. A number keeps its written scale
  (``10k`` is 10000, ``2.5M`` 2500000). A number written as a percent (``%``,
  ``percent``, ``per cent``, ``pct``, also after words) is covered only by a claim whose
  unit is a percent, at percent scale: ``25%`` by a claim of 25 percent, never ``2500%``
  or ``0.25%``, and never by a unit-less (a 0.25 fraction), currency or physical-unit
  claim; a percent claim covers no number not written as a percent. Not numbers:
  ``api://`` handles, ISO dates and timestamps, month-name dates, times, identifiers
  (``AO-26005B``, ``OR1000151885``, ``SO-2026-0003``) and names under a known id prefix
  (``SHA-256``, ``AO-77``, ``F2``); capitals or letters plus digits alone under another
  prefix (``SKU-1509``, ``x1509``) leave the digits a number. Numbers in words count from
  ten up. Text claims cover their exact text (``PO#5447``, ``sup-synth-1``). An ISO date or
  timestamp in the text (a whole token: ``Lot 2026-01-07-A`` is an identifier) is
  ``claim_unstructured`` too unless a date claim of the item (the close outcome: of the
  case) covers it, or a text claim's covered text holds it. A date written is covered by
  a claim of that day; by a claimed timestamp, on its day in the time zone its source
  declares, else in its own offset (a value ending ``Z`` states its UTC day). A timestamp
  written is covered, time and offset with it, only by an instant the claim establishes:
  the claimed timestamp's, or that of the cell a claimed date was read from; a date claim
  establishes no other time of its day;
* ``claim_count_lower_bound``: a count no complete count entry binds is written as an
  exact number in its item's text or the close outcome: in digits; in words (``three``,
  ``three-receipt``, ``a dozen``) unless the words after it in its clause name none of
  what it counts (its role's, its collections' or a record noun) but a plural noun
  (``held in two systems``). Each other passing claim of that number covers one such
  occurrence, never every restatement of it;
* ``claim_currency_mismatch``: a sum's currency does not hold (above);
* ``claim_wording_role_mismatch`` / ``claim_wording_role_unverified`` (``wording_check``):
  a quantity-type word bound to a number in the text (``produced 1509``), or left unbound
  in a sentence of such numbers (``Produced, 1509``), while that number's claims carry
  another quantity type, or none; next to an identifier claim on an id column it is
  only an advisory lead. The type is the one the checker **grounded** (the source
  contract's role, a calculation's type by the algebra), never a name the caller chose.
  An untyped calculation is cited as a number only: any type word bound to it, or left
  unbound in its sentence, blocks. ``passed QC`` / ``QC passed`` / ``passed inspection``
  are inspected-family words. The close outcome is checked the same way against every
  claim of the case.

Known limits: digits under a known id prefix (``AO-1509``, ``MO1509``) are a name, so
neither the number nor the wording checks see them. Names and status words are checked
only through the value claims an item states: a name or status word written in the text
with no claim of it, beside the claims it does state (``Released, not DRAFT``), and a
month-name date with no claim are not detected; whether free text follows from its
values (entailment) is not judged.

Workbench-written texts (``claim.statement_by: workbench`` of a checked link, or
``statement_by: workbench`` set only by tool code: the attach / re-extract / mail tools'
findings, the customer-PO proposal evidence and coverage gaps) are composed from what
the Workbench read, never caller text, and are exempt from the prose checks. A claim on
a field with no role contract and no quantity type (an id, a price) passes as
``untyped``. The checker writes nothing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
import math
import re
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ontology.core import ValidationError
from ontology.units import js_trim

from . import quantity_types as _qt
from .citation_schemes import HANDLE_PREFIX
from .cases import GAP_STATES, evaluate as _evaluate, parse_unit, quantity_role as _quantity_role

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
IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])[A-Z]{2,8}-?\d{4,}[A-Z0-9]*"
    r"(?:-(?:(?=[A-Z0-9.]*[A-Z])[A-Z0-9.]+|\d+(?!\d|\.\d)))*"
    r"(?![A-Za-z0-9_])")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
HANDLE = re.compile(HANDLE_PREFIX + r"\S+")
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
# claim (their period split the sentence for the wording check).
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
# Characters that join two word runs into one token (``re-quote``, ``1,509``, ``3.78L``,
# ``it's``, ``10:30``): a quote never starts or ends between them.
_JOINERS = frozenset("-.,/'\u2019:")
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
# ISO-8601, UTF-8, AO-77). A single-letter code (P-02458) is not one: claim it as text.
# Prefixes whose hyphenated or glued digits are names, not quantities: standards and
# algorithms (SHA-256, ISO-8601, UTF-8, RFC-3339), PLX record codes (AO-77, SO-12, PO-5,
# OR-3, BC-9) and the case's own item ids (F2, G1, C3, P1, V1, L1). Any other letters
# glued or hyphenated to digits (SKU-1509, x1509) leave the digits a number.
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
                         "of", "currency"})
# A value computed over cited rows in the claim itself (``op`` over ``of``): the number of
# distinct records cited, or the Decimal sum of one field of each.
OPS = ("count", "sum")
MAX_OP_INPUTS = 100
_OP_INPUT_KEYS = frozenset({"citation", "field", "unit"})
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


# --------------------------------------------------------------------------- values

def to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = repr(value)
    text = str(value).strip()
    if "," in text:
        # "1,509" is 1509; "1,5" (a decimal comma) or "12,34" is no number, never 15.
        if not _THOUSANDS.fullmatch(text):
            return None
        text = text.replace(",", "")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def number_value(match: re.Match[str]) -> Decimal:
    """The value of a ``NUMBER`` match at its written scale: ``1,509`` is 1509, ``-6``
    is -6, ``0.25%`` is 0.0025, ``10k`` is 10000 and ``2.5M`` 2500000."""
    body = (match.group("int") or "0").replace(",", "") + (match.group("frac")
                                                        or match.group("lead") or "")
    number = Decimal(body)
    if match.group("sign") in ("-", "−") and number != 0:
        number = -number
    scale = (match.group("scale") or "").strip(" \u00a0").lower()
    return number * _SCALES_OF_SUFFIX[scale] if scale else number


def _plain(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text


# --------------------------------------------------------------------------- write-time shape

def parse_value_claims(raw: Any, handles: Sequence[str],
                       calculations: Sequence[str]) -> list[dict[str, Any]]:
    """Validate and normalise structured value claims (shape only; checked on read).

    Each claim cites one of ``handles`` (the item's own citations) with a ``field``,
    one recorded ``calculation`` id, or computes its value over cited rows (``op``
    ``count`` or ``sum`` ``of`` inputs, each one of ``handles``; a sum's with a ``field``).
    Raises ``ValidationError`` with a stable code.
    """
    if isinstance(raw, (str, Mapping)) or not isinstance(raw, Sequence):
        raise ValidationError("invalid_value_claims")
    if len(raw) > MAX_CLAIMS:
        raise ValidationError("too_many_value_claims")
    out = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or set(item) - _CLAIM_KEYS or "value" not in item:
            raise ValidationError(f"invalid_value_claim:{index}")
        value = item["value"]
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValidationError(f"value_claim_value_must_be_integer_or_string:{index}")
        text = str(value).strip()
        if not text or len(text) > 200:
            raise ValidationError(f"invalid_value_claim_value:{index}")
        claim: dict[str, Any] = {"value": text,
                                 "kind": "number" if _PLAIN_NUMBER.match(text) else "text"}
        if sum(key in item for key in ("citation", "calculation", "op")) != 1 \
                or ("op" in item) != ("of" in item):
            raise ValidationError(f"value_claim_needs_citation_or_calculation:{index}")
        if "op" in item:
            if "field" in item:
                raise ValidationError(f"invalid_value_claim_field:{index}")
            claim.update(op=item["op"], of=_op_inputs(item, handles, index))
            if claim["kind"] != "number":
                raise ValidationError(f"invalid_value_claim_value:{index}")
        elif "citation" in item:
            if item["citation"] not in handles:
                raise ValidationError(f"value_claim_citation_not_cited:{index}")
            field = item.get("field")
            if not isinstance(field, str) or not (_FIELD.match(field) or field == PAGE_TEXT):
                raise ValidationError(f"invalid_value_claim_field:{index}")
            claim.update(citation=item["citation"], field=field)
        else:
            if "field" in item:
                raise ValidationError(f"invalid_value_claim_field:{index}")
            calc = item["calculation"]
            if not isinstance(calc, str) or not _CALC_ID.match(calc) or calc not in calculations:
                raise ValidationError(f"value_claim_calculation_not_found:{index}")
            claim["calculation"] = calc
        role = item.get("role")
        if role is not None:
            if not isinstance(role, str) or not _ROLE_SHAPE.match(role) or (
                    "citation" in claim and role not in ROLES) or (
                    claim.get("op") == "count" and role in ROLES):
                raise ValidationError(f"invalid_value_claim_role:{index}")
            claim["role"] = role
        unit = item.get("unit")
        if unit is not None:
            try:
                if not isinstance(unit, str) or len(unit) > 100:
                    raise ValidationError("unit")
                parse_unit(unit)
            except ValidationError:
                raise ValidationError(f"invalid_value_claim_unit:{index}") from None
            claim["unit"] = unit.strip()
        currency = item.get("currency")
        if currency is not None:
            # A sum, or one cited number (not a page figure), states a currency.
            money = claim.get("op") == "sum" or (
                claim["kind"] == "number" and claim.get("field") not in (None, PAGE_TEXT))
            if not money or not isinstance(currency, str) or not _CURRENCY.match(currency):
                raise ValidationError(f"invalid_value_claim_currency:{index}")
            claim["currency"] = currency
        out.append(claim)
    return out


def _op_inputs(item: Mapping[str, Any], handles: Sequence[str], index: int
               ) -> list[dict[str, str]]:
    """The cited inputs of an ``op`` claim, shape only: 1 to ``MAX_OP_INPUTS``, each one of
    ``handles``, none twice; a sum's each with a ``field`` (a row column) and an optional
    ``unit``, a count's a citation alone."""
    op, inputs = item["op"], item["of"]
    if op not in OPS or isinstance(inputs, (str, Mapping)) or not isinstance(
            inputs, Sequence) or not 1 <= len(inputs) <= MAX_OP_INPUTS:
        raise ValidationError(f"invalid_value_claim_op:{index}")
    out, seen = [], set()
    for given in inputs:
        if not isinstance(given, Mapping) or set(given) - (
                _OP_INPUT_KEYS if op == "sum" else {"citation"}):
            raise ValidationError(f"invalid_value_claim_op:{index}")
        if given.get("citation") not in handles:
            raise ValidationError(f"value_claim_citation_not_cited:{index}")
        entry = {"citation": given["citation"]}
        if op == "sum":
            field, unit = given.get("field"), given.get("unit")
            if not isinstance(field, str) or not _FIELD.match(field):
                raise ValidationError(f"invalid_value_claim_field:{index}")
            entry["field"] = field
            if unit is not None:
                try:
                    if not isinstance(unit, str) or len(unit) > 100:
                        raise ValidationError("unit")
                    parse_unit(unit)
                except ValidationError:
                    raise ValidationError(f"invalid_value_claim_unit:{index}") from None
                entry["unit"] = unit.strip()
        if (entry["citation"], entry.get("field")) in seen:
            raise ValidationError(f"value_claim_duplicate_input:{index}")
        seen.add((entry["citation"], entry.get("field")))
        out.append(entry)
    return out


# --------------------------------------------------------------------------- evidence

def dig(resolved: Mapping[str, Any], field: str) -> tuple[bool, Any]:
    value: Any = resolved
    for part in field.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


class _Evidence:
    """Rows of one case read once; recorded units per (handle, field); calculations."""

    def __init__(self, rows: Rows, contracts: Contracts | None,
                 calculations: Sequence[Mapping[str, Any]],
                 returned: Mapping[str, Any] | None = None) -> None:
        self._rows, self.contracts = rows, contracts
        returned = returned or {}
        # Complete count entries as (value, the records they list); the records of each
        # entry a tool's identity lookup told apart (``distinct``); money cells' currencies.
        entries = [entry for entry in returned.get("counts") or () if isinstance(entry, Mapping)]
        self.counts = [(to_decimal(entry.get("value")), frozenset(entry.get("records") or ()))
                       for entry in entries if entry.get("complete") is True]
        self.distinct = [frozenset(entry.get("records") or ()) for entry in entries
                         if entry.get("complete") is True or entry.get("distinct") is True]
        self.currencies: Mapping[tuple[str, str], str | None] = \
            returned.get("currencies") or {}
        # (handle, field) of a money cell -> (handle, field) of the cell its currency was
        # read on, when another record states it (a company's local currency).
        self.currency_cites: Mapping[tuple[str, str], tuple[str, str]] = \
            returned.get("currency_citations") or {}
        self._cache: dict[str, Mapping[str, Any] | None] = {}
        self.units: dict[tuple[str, str], set[str]] = {}
        self.calcs: dict[str, dict[str, Any]] = {}
        for calc in calculations:
            inputs = calc.get("inputs")
            if not isinstance(inputs, Mapping) or calc.get("result") is None:
                continue  # withheld
            for spec in inputs.values():
                cite = spec.get("citation") if isinstance(spec, Mapping) else None
                if spec.get("kind") == "citation" and isinstance(cite, Mapping) \
                        and cite.get("handle") and spec.get("field") and spec.get("unit"):
                    self.units.setdefault((cite["handle"], spec["field"]), set()).add(
                        spec["unit"])
            self.calcs[str(calc.get("id"))] = dict(calc)

    def row(self, handle: str) -> Mapping[str, Any] | None:
        if handle not in self._cache:
            try:
                resolved = self._rows(handle)
            except Exception:  # noqa: BLE001 - an unreadable row is reported, never raised
                resolved = None
            self._cache[handle] = resolved if isinstance(resolved, Mapping) else None
        return self._cache[handle]

    def contract(self, handle: str, resolved: Mapping[str, Any], field: str
                 ) -> Mapping[str, Any] | None:
        """The role/unit contract of a cited column: the caller's contracts (the source
        unit contracts), else the ontology schema's quantity role of a BC column
        (``schema_role``)."""
        if not _COLUMN_FIELD.match(field):
            return None
        found = None
        if self.contracts is not None:
            try:
                found = self.contracts(handle, resolved, field)
            except Exception:  # noqa: BLE001 - a broken contract source pins nothing
                return None
        return found if found is not None else schema_role(handle, resolved, field)

    def zone(self, handle: str, resolved: Mapping[str, Any], field: str) -> tzinfo | None:
        """The time zone the cited column's source declares (its contract's ``time_zone``,
        configuration, never a guess), or None. Raises ``ValueError`` when the declared
        zone does not load: a claimed day is then read in no zone at all."""
        contract = self.contract(handle, resolved, field)
        name = contract.get("time_zone") if contract else None
        if name is None:
            return None
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, TypeError, OSError) as error:
            raise ValueError(f"the source declares the time zone {str(name)[:60]!r}, which "
                             "does not load") from error

    def grounded_role(self, calc: Mapping[str, Any]
                      ) -> tuple[str | None, str | None, str | None, str | None]:
        """(label, quantity type, form, problem) of a calculation, from its re-read inputs'
        role contracts only (declared input roles, literal roles and recorded results are
        never trusted).

        The **quantity type** is the closed type algebra of ``quantity_types`` over the
        expression tree: the expression simplified into a form over the source cells it
        reads, typed only by the documented rules (``m + (p - m)`` is planned, ``p + d``
        is untyped). The **label** is the result's name by the ``cases.quantity_role``
        combination guard (a source role or a relation's ``as``): it types nothing, and
        every relation name must fit the form of the subtree it was matched on, the
        result label the whole form (``planned - actual as planned_not_produced`` fits,
        ``m + (p - m) as produced_total`` and ``p + d as damaged_total`` do not). A
        relation naming a source role (``planned - damaged as produced``) is refused."""
        relations = list(calc.get("relations") or [])
        for relation in relations:
            if relation.get("as") in ROLES:
                return None, None, None, (f"its relation names the result "
                                          f"'{relation.get('as')}', a source role; a "
                                          "combination of roles is a derived quantity")
        roles: dict[str, str | None] = {}
        units: dict[str, Any] = {}
        atoms: dict[str, tuple[Any, str | None]] = {}
        negative = []
        try:
            for name, spec in calc["inputs"].items():
                units[name] = parse_unit(spec.get("unit"))
                if spec.get("kind") == "citation":
                    handle = spec["citation"]["handle"]
                    resolved = self.row(handle)
                    if resolved is None:
                        return None, None, None, "an input row is not readable now"
                    contract = self.contract(handle, resolved, spec["field"])
                    roles[name] = contract.get("role") if contract else None
                    # The value as re-read from the digest-pinned row: one cell read at two
                    # values never merges, and a cell absent now is a value of its own.
                    exists, cell = dig(resolved, spec["field"])
                    atoms[name] = (_qt.cell(handle, spec["field"]), roles[name],
                                   cell if exists and cell is not None else ("absent", name))
                else:
                    roles[name] = None if spec.get("role") else ""
                    atoms[name] = (("literal", name), None)
                    value = to_decimal(spec.get("value"))
                    if value is not None and value < 0:
                        negative.append(name)
            seen: list[Any] = []
            role, used = _quantity_role(calc["expression"], roles, relations, units, negative,
                                        seen=seen)
        except (ValidationError, SyntaxError, KeyError, TypeError) as error:
            return None, None, None, ("its inputs' contracted roles do not combine "
                                      f"({str(error)[:160]})")
        if len(used) != len(relations):
            return None, None, None, ("a declared relation does not match its inputs' "
                                      "contracted roles")
        label = role or None
        # Literals are their recorded numbers (a literal's declared role is not trusted).
        typed = _qt.type_calculation(
            calc["expression"], atoms, relations, seen,
            label if label and label not in ROLES else None,
            constants={name: spec.get("value") for name, spec in calc["inputs"].items()
                       if spec.get("kind") != "citation"})
        if typed["problem"]:
            return None, None, typed["form"], typed["problem"]
        return label, typed["type"], typed["form"], None

    def recomputed(self, calc: Mapping[str, Any]) -> Decimal | None:
        values: dict[str, tuple[Decimal, Any]] = {}
        try:
            for name, spec in calc["inputs"].items():
                if spec.get("kind") == "citation":
                    resolved = self.row(spec["citation"]["handle"])
                    if resolved is None:
                        return None
                    number = to_decimal(dig(resolved, spec["field"])[1])
                else:
                    number = to_decimal(spec.get("value"))
                if number is None:
                    return None
                values[name] = (number, parse_unit(spec.get("unit")))
            result, _unit, _inexact, _used = _evaluate(calc["expression"], values)
        except (Exception, RecursionError):  # noqa: BLE001 - not recomputable: not evidence
            return None
        return result


@lru_cache(maxsize=1)
def _bc_roles() -> Mapping[tuple[str, str], tuple[str, str] | None]:
    """(BC entity set, column) -> (``ontology:<type>.<property>``, quantity role) of every
    quantity property the ontology schema binds to a BC column without a row condition;
    None for a column two properties type differently. Empty when the schema does not
    load (no role is then grounded: stricter, never looser)."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: no schema roles
        return {}
    out: dict[tuple[str, str], tuple[str, str] | None] = {}
    for type_id, spec in schema.object_types.items():
        for prop in spec.get("properties") or ():
            role = prop.get("quantity_role")
            if prop.get("value_type") != "quantity" or role not in QUANTITY_ROLES:
                continue
            for source in prop.get("sources") or ():
                _type, binding = schema.bindings.get(source.get("binding"), (None, {}))
                path = source.get("path")
                if binding.get("source") != "bc" or binding.get("where") or \
                        not isinstance(path, str) or "." in path:
                    continue
                key = (binding["relation"], path)
                if key not in out:
                    out[key] = (f"ontology:{type_id}.{prop['name']}", role)
                elif out[key] is not None and out[key][1] != role:
                    out[key] = None
    return out


def schema_role(handle: str, resolved: Mapping[str, Any], field: str
                ) -> dict[str, Any] | None:
    """The ontology schema's quantity role of a cited BC row column (``row.<column>``), as
    a contract ``{"id", "role", "schema": True}`` (the role the projection gives the value
    read there; ``ontology/project.py``); None for any other citation or column. The
    entity set is the re-read view's: its relation (``bc.<entity set>``) or its own
    citation (a BC capture view)."""
    match = _COLUMN_FIELD.match(field)
    if not handle.startswith("bc://") or match is None or not field.startswith("row."):
        return None
    entity_set = _bc_entity_set(resolved)
    found = _bc_roles().get((entity_set, match.group(1))) if entity_set else None
    return None if found is None else {"id": found[0], "role": found[1], "schema": True}


def _bc_entity_set(resolved: Mapping[str, Any]) -> str | None:
    """The BC entity set of a re-read BC view: its relation (``bc.<entity set>``) or its
    own citation (a BC capture view)."""
    relation, cited = resolved.get("relation"), resolved.get("citation")
    if isinstance(relation, str):
        return relation.removeprefix("bc.") if relation.startswith("bc.") else None
    if isinstance(cited, str) and cited.startswith("bc://"):
        parts = cited[len("bc://"):].split("@", 1)[0].split("/")
        return parts[2] if len(parts) == 4 else None
    return None


@lru_cache(maxsize=1)
def _money_columns() -> frozenset[tuple[str, str, str]]:
    """(source, relation or BC entity set, column) of every Portal mirror and BC column the
    ontology schema types ``money``; none when the schema does not load (a tool's returned
    currency then marks money alone)."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: no money columns
        return frozenset()
    out = set()
    for spec in schema.object_types.values():
        for prop in spec.get("properties") or ():
            if prop.get("value_type") != "money":
                continue
            for source in prop.get("sources") or ():
                _type, binding = schema.bindings.get(source.get("binding"), (None, {}))
                path = source.get("path")
                if binding.get("source") in ("portal_api", "bc") and isinstance(path, str) \
                        and "." not in path:
                    out.add((binding["source"], binding["relation"], path))
    return frozenset(out)


def _money_cell(handle: str, resolved: Mapping[str, Any], field: str) -> bool:
    """Whether the schema types a cited row column money (``_money_columns``)."""
    match = _COLUMN_FIELD.match(field)
    if match is None or field.startswith("key."):
        return False
    if handle.startswith("api://"):
        where = ("portal_api", resolved.get("relation"))
    elif handle.startswith("bc://"):
        where = ("bc", _bc_entity_set(resolved))
    else:
        return False
    return (*where, match.group(1)) in _money_columns()


# --------------------------------------------------------------------------- the gate

def _fail(out: dict[str, Any], code: str, detail: str) -> dict[str, Any]:
    return {**out, "status": "failed", "code": code, "detail": detail}


def _changed(resolved: Mapping[str, Any] | None) -> bool:
    """Whether a re-read is a changed-since view: the cited revision moved on
    (``changed_since`` true, as ``CaseStore`` refuses support; ``CaseRuntime._live_view``,
    ``_capture_view``, ``claim_row``). Any flag but an explicit false is changed: it fails closed."""
    return resolved is not None and resolved.get("changed_since") not in (None, False)


def _moment_of(match: re.Match[str]) -> tuple[str, datetime | None] | None:
    """(the calendar date as written, the moment when a time is written: aware when an
    offset is) of a ``_MOMENT`` match; None when it names no real date or time."""
    try:
        day = date(int(match["y"]), int(match["m"]), int(match["d"]))
        if match["H"] is None:
            return day.isoformat(), None
        zone = match["tz"]
        tzinfo = None if zone is None else timezone.utc if zone == "Z" else timezone(
            (1 if zone[0] == "+" else -1) * timedelta(
                hours=int(zone[1:3]), minutes=int(zone[3:].lstrip(":") or 0)))
        fraction = (match["f"] or ".")[1:7].ljust(6, "0")
        return day.isoformat(), datetime(day.year, day.month, day.day, int(match["H"]),
                                         int(match["M"]), int(match["S"] or 0),
                                         int(fraction), tzinfo=tzinfo)
    except ValueError:
        return None


def _moment(text: Any) -> tuple[str, datetime | None] | None:
    """``_moment_of`` a whole ISO date or timestamp, else None."""
    match = _ISO_MOMENT.fullmatch(text.strip()) if isinstance(text, str) else None
    return _moment_of(match) if match else None


def _same_instant(one: datetime, other: datetime) -> bool:
    """Two timestamps on one instant: both with an offset, or both without (a time without
    an offset is no instant, never equal to one with an offset)."""
    return (one.tzinfo is None) == (other.tzinfo is None) and one == other


def _day(moment: tuple[str, datetime | None], zone: tzinfo | None) -> str:
    """The calendar day of a ``_moment``: an instant's day in the time zone its source
    declares (``zone``), else as written (its own offset: a value ending ``Z`` is its UTC
    day; a time without an offset, or a bare date, is that day)."""
    when = moment[1]
    if when is None or when.tzinfo is None or zone is None:
        return moment[0]
    return when.astimezone(zone).date().isoformat()


# A date claim as the prose checks read it: (its ``_moment``, the instant of the cell a
# bare date was read from or None, the time zone its source declares or None).
_Dated = tuple[tuple[str, "datetime | None"], "datetime | None", "tzinfo | None"]


def _covers(reading: _Dated, written: tuple[str, datetime | None]) -> bool:
    """Whether a date claim covers a date or timestamp written in prose: a date written
    when it is the claimed value's day (``_day``: a claimed timestamp in its source's
    zone); a timestamp written only when it is an instant the claim establishes, the
    claimed timestamp's or that of the cell a claimed date was read from. A date claim
    establishes no other time of its day (``2026-01-07`` read from ``23:30-05:00`` does
    not cover ``2026-01-07 12:00+00:00``)."""
    claimed, cell, zone = reading
    if written[1] is None:
        return written[0] == _day(claimed, zone)
    instant = claimed[1] if claimed[1] is not None else cell
    return instant is not None and _same_instant(instant, written[1])


def _fold(text: str) -> str:
    """An enum value as compared: NFKC, case-folded, ``_`` and whitespace runs one space."""
    return " ".join(re.split(r"[\s_]+", unicodedata.normalize("NFKC", text).casefold())
                    ).strip()


@lru_cache(maxsize=1)
def _enum_columns() -> frozenset[tuple[str, str]]:
    """(relation, column path) of every Portal mirror column the ontology schema types
    ``enum``; none when the schema does not load (values then compare exactly)."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: no enum folding (stricter, never looser)
        return frozenset()
    out = set()
    for spec in schema.object_types.values():
        for prop in spec.get("properties") or ():
            if prop.get("value_type") != "enum":
                continue
            for source in prop.get("sources") or ():
                _type, binding = schema.bindings.get(source.get("binding"), (None, {}))
                if binding.get("source") == "portal_api" and source.get("path"):
                    out.add((binding["relation"], source["path"]))
    return frozenset(out)


def value_form(claim: Mapping[str, Any], handle: str = "",
               resolved: Mapping[str, Any] | None = None, field: str = "") -> str:
    """How a claim's value is compared and stated: ``number``, ``date`` (an ISO date or
    timestamp), ``enum`` (a status/state column, or a Portal column the schema types
    enum) or ``text`` (names and codes, exactly)."""
    if claim["kind"] == "number":
        return "number"
    if _moment(claim["value"]) is not None:
        return "date"
    column = field.removeprefix("row.")
    if _COLUMN_FIELD.match(field) and _STATUS_COLUMN.search(field.rsplit(".", 1)[-1]) or (
            handle.startswith("api://") and field.startswith("row.") and resolved
            and (str(resolved.get("relation")), column) in _enum_columns()):
        return "enum"
    return "text"


def _same_value(claim: Mapping[str, Any], cell: Any, form: str = "text",
                zone: tzinfo | None = None) -> bool:
    if claim["kind"] == "number":
        number = to_decimal(cell)
        return number is not None and number == to_decimal(claim["value"])
    if cell is None or isinstance(cell, bool):
        return False
    text = str(cell).strip()
    if text == claim["value"]:
        return True
    if form == "enum":
        return _fold(text) == _fold(claim["value"])
    if form != "date":
        return False
    # A date holds on the cell's calendar day (``_day``: in the time zone its source
    # declares, else as written in its own offset, never a UTC day); a timestamp only on
    # the same instant: a timestamp the projection wrote in its canonical UTC form
    # (ontology/project.py ``_timestamp``) is the source's value when both state one
    # instant with an offset.
    claimed, held = _moment(claim["value"]), _moment(text)
    if claimed is not None and claimed[1] is None and held is None and _YYYYMMDD.fullmatch(text):
        # Sage/FM's compact date (ontology/project.py ``yyyymmdd_date``) is that day.
        held = _moment(f"{text[:4]}-{text[4:6]}-{text[6:]}")
    if claimed is None or held is None:
        return False
    if claimed[1] is None:
        return claimed[0] == _day(held, zone)
    return held[1] is not None and _same_instant(held[1], claimed[1])


def _carrier_kind(handle: str, resolved: Mapping[str, Any], field: str) -> str | None:
    """The identifier kind ``ontology/schema/identifiers.json`` declares for the cited
    column (a carrier's ``relation`` and ``field``: a BC entity set, a Portal mirror
    relation), or None: an undeclared or ambiguous carrier has no identifier form."""
    if not field.startswith("row.") or field.startswith("rows."):
        return None
    if handle.startswith("bc://"):
        relation = _bc_entity_set(resolved)
    elif handle.startswith("api://"):
        relation = resolved.get("relation")
    else:
        return None
    if not isinstance(relation, str):
        return None
    try:
        from ontology import identifiers
        return identifiers.carrier_kind(relation, field.removeprefix("row."))
    except Exception:  # noqa: BLE001 - no grammar: no identifier form
        return None


def _same_identifier(claim: Mapping[str, Any], cell: Any, handle: str,
                     resolved: Mapping[str, Any], field: str) -> bool:
    """Whether a text claim is the canonical written form (the identifier grammar's
    ``format``, the form the projection stores) of the identifier the cited carrier
    column holds: ``PO1001`` holds on BC ``purchaseOrders.number`` ``PO-1001``. Only on a
    column the grammar declares a carrier of one kind; the cell must parse as that kind,
    and the claim must be exactly its canonical form (any other spelling is a mismatch)."""
    if claim.get("kind") != "text" or not isinstance(cell, str):
        return False
    kind = _carrier_kind(handle, resolved, field)
    if kind is None:
        return False
    try:
        from ontology import identifiers
        parsed = identifiers.parse(kind, cell)
        return parsed is not None and identifiers.format(parsed) == claim["value"]
    except Exception:  # noqa: BLE001 - an unparseable cell proves nothing
        return False


def _role_accepts(claimed: str, pinned: str) -> bool:
    return claimed == pinned or pinned in _ROLE_ALIASES.get(claimed, frozenset())


def _check_value_claim(claim: Mapping[str, Any], handles: Sequence[str],
                       evidence: _Evidence) -> dict[str, Any]:
    out = {key: claim[key] for key in ("value", "citation", "field", "calculation", "role",
                                       "unit", "op", "of", "currency") if key in claim}
    role = claim.get("role")
    if claim.get("op") == "count":
        return _check_count(claim, out, handles, evidence)
    if "calculation" in claim or claim.get("op") == "sum":
        if "calculation" in claim:
            calc, name = evidence.calcs.get(claim["calculation"]), claim["calculation"]
            if calc is None:
                return _fail(out, "claim_uncited", "the calculation is not readable now")
        else:
            calc, name = _sum_calculation(claim, handles, evidence), "the sum"
            if isinstance(calc, str):
                return _fail(out, "claim_unit_mismatch", calc)
            problem = _currency_problem(claim, handles, evidence)
            if problem is not None:
                return _fail(out, "claim_currency_mismatch", problem)
        cited = {spec["citation"]["handle"] for spec in calc["inputs"].values()
                 if spec.get("kind") == "citation"}
        if not cited <= set(handles):
            return _fail(out, "claim_uncited", f"{name} reads rows the item does not cite")
        if any(_changed(evidence.row(handle)) for handle in sorted(cited)):
            return _fail(out, "claim_changed_since", f"{name} reads a cited "
                         "revision that is no longer the source's current one")
        result = evidence.recomputed(calc)
        if result is None or "op" not in claim and result != to_decimal(calc.get("result")):
            return _fail(out, "claim_uncited", f"{name} does not recompute "
                         "from its cited rows as read now")
        if claim["kind"] != "number" or to_decimal(claim["value"]) != result:
            return _fail(out, "claim_value_mismatch",
                         f"{name} is {_plain(result)}, not {claim['value']}")
        label, quantity_type, form, problem = evidence.grounded_role(calc)
        if problem is not None:
            return _fail(out, "claim_role_mismatch", f"{name}: {problem}")
        shown = f" ({form})" if form else ""
        if role is None:
            if quantity_type is not None:
                return _fail(out, "claim_role_mismatch",
                             f"{name} is a {quantity_type} quantity by its expression"
                             f"{shown}; the claim says no role")
        elif role in QUANTITY_ROLES:
            if quantity_type is None:
                return _fail(out, "claim_role_unverified",
                             f"{name} has no quantity type: its expression over the inputs' "
                             f"role contracts{shown} is not one by the type rules; {role} "
                             "cannot be verified")
            if not _role_accepts(role, quantity_type):
                return _fail(out, "claim_role_mismatch",
                             f"{name} is a {quantity_type} quantity by its expression{shown}, "
                             f"the claim says {role}")
        elif role in OTHER_ROLES:
            if quantity_type is not None:
                return _fail(out, "claim_role_mismatch",
                             f"{name} is a {quantity_type} quantity by its expression{shown}, "
                             f"the claim says {role}")
        elif role != label and "op" not in claim:
            # A sum's other role is the value's name only: it types nothing.
            return _fail(out, "claim_role_mismatch",
                         f"{name} is labelled {label or 'nothing'} by its relations (a "
                         f"{quantity_type or 'untyped'} value{shown}), the claim says {role}")
        if "unit" in claim and parse_unit(claim["unit"]) != parse_unit(calc.get("unit")):
            return _fail(out, "claim_unit_mismatch",
                         f"{name} is in {calc.get('unit')}, the claim says {claim['unit']}")
        return {**out, "status": "passed", "role_basis": "calculation",
                "quantity_type": quantity_type}
    handle, field = claim["citation"], claim["field"]
    resolved = evidence.row(handle) if handle in handles else None
    if resolved is None:
        return _fail(out, "claim_uncited", "the cited row is not readable now as the case "
                     "recorded it")
    if _changed(resolved):
        return _fail(out, "claim_changed_since", "the cited revision is no longer the "
                     "source's current one; cite its current version")
    if field == PAGE_TEXT:
        return _check_page_claim(claim, out, resolved)
    exists, cell = dig(resolved, field)
    if not exists:
        return _fail(out, "claim_value_mismatch", f"the cited row has no {field}")
    if claim["kind"] == "text" and field in ORIGINAL_TEXT_FIELDS and \
            handle.partition("://")[0] in ORIGINAL_SCHEMES:
        return _check_quote(claim, out, cell, field)
    form = value_form(claim, handle, resolved, field)
    zone = None
    if form in ("date", "enum"):
        out["value_form"] = form
    if form == "date":
        try:
            zone = evidence.zone(handle, resolved, field)
        except ValueError as error:
            return _fail(out, "claim_value_mismatch", str(error))
    if not _same_value(claim, cell, form, zone) and not (
            form == "text" and _same_identifier(claim, cell, handle, resolved, field)):
        return _fail(out, "claim_value_mismatch", f"the cited {field} holds another value"
                     + (f" (compared as {'an' if form == 'enum' else 'a'} {form})"
                        if form in ("date", "enum") else ""))
    if "currency" in claim:
        problem = _currency_problem(claim, handles, evidence)
        if problem is not None:
            return _fail(out, "claim_currency_mismatch", problem)
    contract = evidence.contract(handle, resolved, field)
    pinned = contract.get("role") if contract else None
    if contract and contract.get("schema") and role is None:
        # The schema types a BC column; a claim that names no role states an untyped value
        # (no type word of its text is verified by it), as it did before the schema did.
        contract = pinned = None
    where = f"{resolved.get('relation') or 'row'}.{field.rsplit('.', 1)[-1]}"
    if pinned is not None:
        out["role_contract"] = str(contract.get("id"))
        if role is None or not _role_accepts(role, pinned):
            return _fail(out, "claim_role_mismatch",
                         f"{where} is a {pinned} quantity (contract {contract.get('id')}); "
                         f"the claim says {role or 'no role'}")
        basis, quantity_type = "source_contract", pinned
    elif role in QUANTITY_ROLES:
        return _fail(out, "claim_role_unverified",
                     f"no role contract covers {where}, so the claimed {role} quantity "
                     "cannot be verified")
    else:
        # An untyped claim types no prose: an identifier only on an id-shaped column (a
        # caller cannot call a quantity column's value an identifier to silence a word).
        basis = "untyped"
        quantity_type = role if role in OTHER_ROLES and (
            role != "identifier" or _ID_COLUMN.match(field.rsplit(".", 1)[-1])) else None
    known = None
    if contract and contract.get("unit") is not None:
        known = contract["unit"]
    elif len(evidence.units.get((handle, field), ())) == 1:
        known = next(iter(evidence.units[(handle, field)]))
    if "unit" in claim:
        if known is None:
            out["unit_basis"] = "unverified"
        elif parse_unit(claim["unit"]) != parse_unit(known):
            return _fail(out, "claim_unit_mismatch",
                         f"{where} is recorded in {known}, the claim says {claim['unit']}")
        else:
            out["unit_basis"] = "source_contract" if contract and contract.get("unit") \
                else "recorded_calculation"
    return {**out, "status": "passed", "role_basis": basis, "quantity_type": quantity_type}


def _record(handle: str, resolved: Mapping[str, Any]) -> tuple[str, ...]:
    """The record a re-read view is of, version-free: a mirror row by its relation and key,
    any other by the view's own citation without its version and fragment (a BC capture
    revision, a page of an original)."""
    if isinstance(resolved.get("relation"), str) and isinstance(resolved.get("key"), Mapping):
        return ("row", resolved["relation"], json.dumps(resolved["key"], sort_keys=True,
                                                        default=str))
    cited = resolved.get("citation") if isinstance(resolved.get("citation"), str) else handle
    body = cited.split("#", 1)[0]
    return ("record", body.rpartition("@")[0] or body)


def _collection(record: tuple[str, ...]) -> tuple[str, ...]:
    """The source collection a ``_record`` is in: a mirror row's relation, else its
    citation up to the record's id (a BC company's entity set, a mailbox, a drive)."""
    return record[:2] if record[0] == "row" else ("record", record[1].rpartition("/")[0])


def _check_count(claim: Mapping[str, Any], out: dict[str, Any], handles: Sequence[str],
                 evidence: _Evidence) -> dict[str, Any]:
    """A count claim (``op: count``): every input is one of the item's citations, readable
    now and current, and no record is counted twice. Exact (``bound: exact``) when one
    complete count entry a tool returned has the claimed value and lists every record
    cited; else a lower bound (``bound: at_least``) whose value is the most records cited
    that are surely distinct: those of one source collection, or those one count entry a
    tool's identity lookup told apart lists (records of two collections may be twins). A
    count of records has no quantity type (``role`` is its name only) and no unit but
    ``1``."""
    records: dict[tuple[str, ...], str] = {}
    for given in claim["of"]:
        handle = given["citation"]
        resolved = evidence.row(handle) if handle in handles else None
        if resolved is None:
            return _fail(out, "claim_uncited", f"the count reads {handle[:200]}, which is "
                         "not readable now as the item cites it")
        if _changed(resolved):
            return _fail(out, "claim_changed_since", "the count reads a cited revision that "
                         "is no longer the source's current one")
        record = _record(handle, resolved)
        if record in records:
            return _fail(out, "claim_value_mismatch", "the count cites one record twice "
                         f"({records[record][:200]} and {handle[:200]}); it counts records")
        records[record] = handle
    value, cited = to_decimal(claim["value"]), set(records.values())
    bound = "exact" if any(total == value and cited <= listed
                           for total, listed in evidence.counts) else "at_least"
    if bound == "at_least":
        sources: dict[tuple[str, ...], int] = {}
        for record in records:
            sources[_collection(record)] = sources.get(_collection(record), 0) + 1
        floor = max([*sources.values(), *(len(cited & listed) for listed in evidence.distinct)])
        if value != floor:
            return _fail(out, "claim_value_mismatch", (
                f"the count cites {floor} records, not {claim['value']}" if len(sources) == 1
                else f"the count cites records of {len(sources)} sources, which may be one "
                f"record held in two systems: no identity lookup a tool returned tells more "
                f"than {floor} of them apart, so at least {floor}, not {claim['value']}")
                + "; no complete count entry a tool returned lists these records")
    if claim.get("role") in ROLES:
        return _fail(out, "claim_role_mismatch", "a count of records has no "
                     f"{claim['role']} type; name it, as receipt_count")
    if "unit" in claim and parse_unit(claim["unit"]) != parse_unit("1"):
        return _fail(out, "claim_unit_mismatch",
                     f"a count of records has no unit, the claim says {claim['unit']}")
    return {**out, "status": "passed", "role_basis": "calculation", "quantity_type": None,
            "bound": bound}


@lru_cache(maxsize=None)
def _unit_currencies(unit: str) -> frozenset[str]:
    """The ISO 4217 currencies a unit names (``USD``, ``USD/EA``); never ``BOX`` or ``PCS``."""
    from .landed_cost import ISO_4217  # loaded only when a unit is read as money
    return frozenset(name for name, _power in parse_unit(unit) if name in ISO_4217)


def _currency_problem(claim: Mapping[str, Any], handles: Sequence[str],
                      evidence: _Evidence) -> str | None:
    """Why a sum's (or one cited number's) ``currency`` does not hold its cited values
    (module docstring), or None: money is a cell a tool returned as money or a column the
    schema types money. A sum's unit (its own or an input's) that names a currency states
    that currency as money too."""
    stated = claim.get("currency")
    if "op" in claim:
        cells = [(given["citation"], given["field"]) for given in claim["of"]]
        units = [claim.get("unit")] + [given.get("unit") for given in claim["of"]]
    else:
        cells, units = [(claim["citation"], claim["field"])], [claim.get("unit")]
    named = sorted(frozenset().union(*(_unit_currencies(unit) for unit in units if unit)))
    if len(named) > 1:
        return "the units name " + " and ".join(named)
    if stated is not None and named and named[0] != stated:
        return f"the unit names {named[0]}, the claim's currency is {stated}"
    money: list[tuple[tuple[str, str], str | None]] = []
    other: list[tuple[str, str]] = []
    for key in cells:
        resolved = evidence.row(key[0]) if key[0] in handles else None
        if key in evidence.currencies or (resolved is not None
                                          and _money_cell(key[0], resolved, key[1])):
            money.append((key, evidence.currencies.get(key)))
        else:
            other.append(key)
    if not money:
        claimed = stated or (named[0] if named else None)
        return None if claimed is None else (
            "no tool returned the cited values as money and the schema types none of them "
            f"money, so {claimed} is not verified")
    if other:
        return f"the sum adds money to {other[0][0][:200]} {other[0][1]}, which is not money"
    unknown = next((key for key, currency in money if currency is None), None)
    if unknown is not None:
        return (f"{unknown[0][:200]} {unknown[1]} is money in a currency no tool returned "
                "to this reader")
    for key, currency in money:
        # A currency another record states holds only while this reader re-reads it there.
        cite = evidence.currency_cites.get(key)
        if cite is None:
            continue
        resolved = evidence.row(cite[0])
        exists, cell = dig(resolved, cite[1]) if resolved is not None else (False, None)
        if not exists or not isinstance(cell, str) or js_trim(cell).upper() != currency:
            return (f"{key[0][:200]} {key[1]} is money whose currency {cite[0][:200]} "
                    f"{cite[1]} states, which does not hold for this reader now")
    found = sorted({str(currency) for _key, currency in money})
    if len(found) > 1:
        return "the sum adds money in " + " and ".join(found)
    if stated != found[0]:
        return f"the cited values are in {found[0]}, the claim says {stated or 'no currency'}"
    return None


def returned_evidence(tool: str, arguments: Any, result: Any,
                      into: dict[str, Any] | None = None, *,
                      max_cells: int | None = None) -> dict[str, Any]:
    """Add what one tool result returned that ``check_items`` binds op claims to
    (``returned``) to ``into``: ``counts``, every ``plx.aggregate`` count group as
    ``{"value", "records", "distinct", "complete"}``: distinct when the group has a value
    (its identity lookup found no twins among its members) that lists each record it
    counts once (its ``inputs_citations``) as known now (the call names no
    ``as_known_at``: a count of what was known then is not a count of now); complete only
    when distinct in an answer that is complete, not partial, with no gap, refusal or
    withheld report; ``currencies``, per
    (handle, field) of a money value (``$defs/value``) or a cell of an aggregate group in
    a currency, the currency, None when results disagree or state none;
    ``currency_citations``, per such cell whose currency was read on another record, that
    record's (handle, field). A caller bounding what it keeps passes ``counts`` as a
    bounded deque (the oldest dropped: a later count claim is a lower bound) and
    ``max_cells``: a cell already held keeps the conflict rule, a new one past it is not
    kept, nor its currency citation, so no currency is known for it; ``leads``, per
    (handle, field) cell a ``plx.investigate`` answer entry states its value from (its
    ``citation``, or its calculation's ``inputs``), True while every entry stating a value
    from it rests on lead-tier links (``lead_basis``), False once one that does not states
    one from it (``max_cells`` bounds it as it does currencies). A cell, not a record: the
    record an entry reaches through a lead link may be the one the reader has in view,
    whose other cells the lead does not touch."""
    into = {"counts": [], "currencies": {}} if into is None else into
    if not isinstance(result, Mapping) or "error" in result:
        return into
    _money_values(result, into["currencies"], into.setdefault("currency_citations", {}),
                  max_cells)
    if tool == "plx.investigate":
        _lead_cells(result.get("answer"), into.setdefault("leads", {}), max_cells)
    measure = arguments.get("measure") if isinstance(arguments, Mapping) else None
    if tool != "plx.aggregate" or not isinstance(measure, Mapping) \
            or measure.get("op") != "count":
        return into
    whole = (result.get("partial") is False and not result.get("gaps")
             and not {"refused", "withheld"} & set(result)
             and (result.get("completeness") or {}).get("complete") is True)
    now = "as_known_at" not in arguments
    for group in result.get("groups") or ():
        if not isinstance(group, Mapping):
            continue
        records = [cite.get("l1_handle") for cite in group.get("inputs_citations") or ()
                   if isinstance(cite, Mapping)]
        distinct = now and to_decimal(group.get("value")) == len(records) == len(set(records))
        into["counts"].append({"value": group.get("value"), "records": records,
                               "distinct": distinct, "complete": whole and distinct})
    return into


def _lead_cells(answer: Any, leads: dict[tuple[str, str], bool], limit: int | None) -> None:
    for entry in answer if isinstance(answer, list) else ():
        if not isinstance(entry, Mapping):
            continue
        lead = entry.get("lead_basis") is True
        calculation = entry.get("calculation")
        inputs = calculation.get("inputs") if isinstance(calculation, Mapping) else None
        cells = {(cite["l1_handle"], cite["field_path"])
                 for cite in [entry.get("citation"), *(inputs if isinstance(inputs, list)
                                                       else ())]
                 if isinstance(cite, Mapping) and isinstance(cite.get("l1_handle"), str)
                 and isinstance(cite.get("field_path"), str)}
        for cell in sorted(cells):
            if cell in leads or limit is None or len(leads) < limit:
                leads[cell] = leads.get(cell, True) and lead


def _money_values(node: Any, out: dict[tuple[str, str], str | None],
                  cites: dict[tuple[str, str], tuple[str, str]],
                  limit: int | None = None) -> None:
    def key_of(cite: Any) -> tuple[str, str] | None:
        if isinstance(cite, Mapping) and isinstance(cite.get("l1_handle"), str) \
                and isinstance(cite.get("field_path"), str):
            return cite["l1_handle"], cite["field_path"]
        return None

    def note(cite: Any, currency: Any, source: Any = None) -> None:
        key = key_of(cite)
        if key is not None and (key in out or limit is None or len(out) < limit):
            code = currency if isinstance(currency, str) and _CURRENCY.match(currency) else None
            out[key] = code if out.get(key, code) == code else None
            if key_of(source) is not None:   # the record the currency was read on
                cites[key] = key_of(source)
    if isinstance(node, Mapping):
        if node.get("value_type") == "money":
            note(node.get("citation"), node.get("currency"), node.get("currency_citation"))
        if "currency" in node and isinstance(node.get("inputs_citations"), list):
            for cite in node["inputs_citations"]:
                note(cite, node["currency"])
        for value in node.values():
            _money_values(value, out, cites, limit)
    elif isinstance(node, list):
        for value in node:
            _money_values(value, out, cites, limit)


def _sum_calculation(claim: Mapping[str, Any], handles: Sequence[str],
                     evidence: _Evidence) -> dict[str, Any] | str:
    """A sum claim (``op: sum``) as a calculation over its cited cells, checked as a
    recorded one is (``_check_value_claim``): the result recomputed from the re-read rows,
    its quantity type by the type algebra. Every input in one unit (its own, else the
    claim's, else ``1``) that the cell's contract or recorded unit does not contradict;
    else why not (a ``claim_unit_mismatch`` detail)."""
    inputs: dict[str, dict[str, Any]] = {}
    units = set()
    for number, given in enumerate(claim["of"]):
        handle, field = given["citation"], given["field"]
        unit = given.get("unit") or claim.get("unit") or "1"
        resolved = evidence.row(handle) if handle in handles else None
        if resolved is not None:
            contract = evidence.contract(handle, resolved, field)
            known = contract.get("unit") if contract and contract.get("unit") is not None \
                else next(iter(evidence.units.get((handle, field), ())), None) \
                if len(evidence.units.get((handle, field), ())) == 1 else None
            if known is not None and parse_unit(known) != parse_unit(unit):
                return f"{handle[:200]} {field} is recorded in {known}, the sum says {unit}"
        units.add(parse_unit(unit))
        inputs[f"x{number}"] = {"kind": "citation", "citation": {"handle": handle},
                                "field": field, "unit": unit}
    if len(units) > 1:
        return "the sum adds inputs in different units"
    names = list(inputs)
    # Grouped by ten: MAX_OP_INPUTS terms stay within the evaluator's expression length
    # and depth (operating/cases.py _MAX_EXPR, _MAX_DEPTH), as the eval scorer groups.
    expression = "+".join("(" + "+".join(names[start:start + 10]) + ")"
                          for start in range(0, len(names), 10))
    return {"id": "sum", "expression": expression, "inputs": inputs,
            "unit": next(iter(inputs.values()))["unit"]}


# Units a page's machine-read text may print right after a figure ("1502 EA", "25 kg"),
# case-folded, by the unit they mean; the percent words are the number's own scale.
_PAGE_UNITS = {**dict.fromkeys(("ea", "each", "pc", "pcs", "piece", "pieces", "unit", "units"),
                               "ea"),
               **dict.fromkeys(_PERCENT_UNITS, "percent"), "lbs": "lb",
               **{unit: unit for unit in ("kg", "g", "mg", "lb", "oz", "l", "ml", "gal")}}
_UNIT_AFTER = re.compile(r"[ \t\u00a0]*([A-Za-z]+)\b")


def _page_unit(unit: str) -> str:
    folded = unit.strip().casefold()
    return _PAGE_UNITS.get(folded, folded)


def _check_page_claim(claim: Mapping[str, Any], out: dict[str, Any],
                      resolved: Mapping[str, Any]) -> dict[str, Any]:
    """A figure claimed on an original page citation (``field: "text"``): the page's
    machine-read text, as re-read now, holds it as one number token (by value at its
    written scale: ``07`` is 7, ``1,509`` is 1509, ``25%`` is 0.25, or 25 for a claim in
    percent, as the prose percent contract compares); a text value as a quote of it
    (``supported_quotes``). It proves presence on the page, not in a labelled field.
    Machine-read text grounds no quantity type: the claim is untyped (a quantity role is
    ``claim_role_unverified``) and stays a lead. A stated unit must agree with the unit the
    page prints right after the figure (``1502 EA``, ``25%``; ``_PAGE_UNITS``), else
    ``claim_unit_mismatch``; where the page prints none (``QC retain: 1``) it is recorded as
    ``unit_basis: unverified``, as on a source field no unit contract covers (an AO
    quantity's role contract pins no unit either), and the reader of the result (the
    scorer's expected unit, a reviewer) judges it."""
    text = resolved.get("text")
    if not isinstance(resolved.get("page"), int) or not isinstance(text, str):
        return _fail(out, "claim_value_mismatch", "the citation is not an original page with "
                     "machine-read text")
    claimed = _page_unit(claim["unit"]) if "unit" in claim else None
    printed: list[str | None] = []
    if claim["kind"] == "number":
        want = to_decimal(claim["value"])
        for match in NUMBER.finditer(text):
            percent = (match.group("scale") or "").strip(" \u00a0").lower() in _PERCENT_WORDS
            number = number_value(match)
            if (number * 100 if percent and claimed == "percent" else number) != want:
                continue
            after = _UNIT_AFTER.match(text, match.end())
            word = after and _PAGE_UNITS.get(after.group(1).casefold())
            printed.append("percent" if percent else word or None)
    else:
        problem = _quote_problem(claim["value"], text,
                                 f"page {resolved['page']}'s machine-read text")
        if problem:
            return _fail(out, "claim_value_mismatch", problem)
        printed = [None]
    if not printed:
        return _fail(out, "claim_value_mismatch", f"page {resolved['page']}'s machine-read "
                     f"text does not hold {claim['value']}")
    if claimed is not None:
        if claimed in printed:
            out["unit_basis"] = "page_text"
        elif None in printed:
            out["unit_basis"] = "unverified"
        else:
            return _fail(out, "claim_unit_mismatch", f"page {resolved['page']} prints "
                         f"{claim['value']} in {', '.join(sorted(set(printed)))}, the claim "
                         f"says {claim['unit']}")
    if claim.get("role") in QUANTITY_ROLES:
        return _fail(out, "claim_role_unverified", "a figure in a page's machine-read text has "
                     f"no role contract, so the claimed {claim['role']} cannot be verified")
    if claim["kind"] == "text":
        out["value_basis"] = "text_span"
    return {**out, "status": "passed", "role_basis": "untyped", "quantity_type": None,
            "text_source": resolved.get("text_source")}


def _check_quote(claim: Mapping[str, Any], out: dict[str, Any], cell: Any,
                 field: str) -> dict[str, Any]:
    """A text value claimed on the re-read text of a cited original or part (``field`` in
    ``ORIGINAL_TEXT_FIELDS``): a quote of it (``supported_quotes``). It proves the words
    are in that text, not what they mean: untyped (a quantity role is
    ``claim_role_unverified``), and a stated unit is ``unit_basis: unverified``."""
    problem = _quote_problem(claim["value"], cell if isinstance(cell, str) else "",
                             f"the cited {field}")
    if problem:
        return _fail(out, "claim_value_mismatch", problem)
    if "unit" in claim:
        out["unit_basis"] = "unverified"
    if claim.get("role") in QUANTITY_ROLES:
        return _fail(out, "claim_role_unverified", "a quote from an original's text has no "
                     f"role contract, so the claimed {claim['role']} cannot be verified")
    return {**out, "status": "passed", "role_basis": "untyped", "quantity_type": None,
            "value_basis": "text_span"}


def _normalised(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


# One cited text is quoted by many claims of a case: normalised once.
_normalised_text = lru_cache(maxsize=4)(_normalised)


def _splits_token(text: str, at: int) -> bool:
    """Whether position ``at`` of ``text`` lies inside one token: between two word
    characters, or next to a joiner between two (``re|-quote``, ``1,|509``)."""
    def word(index: int) -> bool:
        return 0 <= index < len(text) and _WORD_CHAR.match(text[index]) is not None
    return (word(at - 1) and word(at)) or (
        word(at - 1) and text[at:at + 1] in _JOINERS and word(at + 1)) or (
        word(at) and at > 0 and text[at - 1] in _JOINERS and word(at - 2))


def _spans(want: str, text: str) -> list[int]:
    found, at = [], text.find(want) if want else -1
    while at >= 0:
        if not _splits_token(text, at) and not _splits_token(text, at + len(want)):
            found.append(at)
        at = text.find(want, at + 1)
    return found


def quoted_spans(value: str, text: str) -> int:
    """How often ``value`` occurs verbatim in ``text``: both NFKC-normalised with every
    whitespace run one space, starting and ending on token boundaries (``moved it to Jan 9.``
    is in ``...moved it to Jan 9.``; ``quote`` is not in ``re-quote``, ``Jan 9`` not in
    ``Jan 9,2026``, ``supplier`` not in ``suppliers``). Case and punctuation are kept."""
    return len(_spans(_normalised(value), _normalised_text(text)))


def _quotable(want: str) -> bool:
    """Whether a normalised quote is long enough to tell one passage from another: two
    word tokens (whitespace-separated, each with a letter or digit: ``Jan 9``), one token
    of at least eight characters (``approved``), or an identifier-shaped token of at least
    four with a digit or capital: letters with digits or two joined word runs (``PO5447``,
    ``LOT-FXA``, ``2026-07``); never ``PO``, ``USD``, ``e.g.``, ``1`` or ``.``."""
    words = sum(_WORD_CHAR.search(token) is not None for token in want.split(" "))
    if words != 1:
        return words >= 2
    return len(want) >= 8 or _identifier_shaped(want)


def _identifier_shaped(token: str) -> bool:
    """At least four characters with a digit or capital: letters with digits or two
    joined word runs (``PO5447``, ``LOT-FXA``, ``2026-07``)."""
    digit, alpha = any(c.isdigit() for c in token), any(c.isalpha() for c in token)
    return len(token) >= 4 and (digit or any(c.isupper() for c in token)) \
        and (len(_WORD_RUN.findall(token)) >= 2 or digit and alpha)


def _negated(want: str, text: str, at: int) -> bool:
    """Whether the occurrence of ``want`` at ``at`` of ``text`` is negated: a negator
    directly before it (not the label abbreviation ``Lot No`` before an identifier) or a
    negating answer directly after it (``QC passed? No``)."""
    start = max(0, at - 40)
    if _NEGATOR.search(text, start, at) and not (
            _NUMBER_LABEL.search(text, start, at) and _identifier_shaped(want.split(" ")[0])):
        return True
    return _NEGATED_AFTER.match(text, at + len(want)) is not None


def supported_quotes(value: str, text: str) -> int:
    """How many occurrences of ``value`` in ``text`` support it as a quote: its
    ``quoted_spans`` that no negator reverses (``_negated``: ``approved`` in ``not
    approved`` or ``approved? No``; a quote that includes the negator, ``not approved``,
    is supported; ``No`` after a label before an identifier, ``Lot No LOT-FXA``, is the
    abbreviation of number), and none at all for a quote too short to tell passages apart
    (``_quotable``)."""
    want, text = _normalised(value), _normalised_text(text)
    if not _quotable(want):
        return 0
    return sum(not _negated(want, text, at) for at in _spans(want, text))


def _quote_problem(value: str, text: str, where: str) -> str | None:
    if not _quotable(_normalised(value)):
        return (f"{value} is too short to verify as a quote: quote at least two words or "
                "eight characters of the passage")
    if not supported_quotes(value, text):
        return (f"{where} does not hold {value} verbatim on token boundaries without a "
                "negator directly before or after it")
    return None


def _number_words(text: str) -> list[tuple[str, Decimal]]:
    return [(written, value) for _start, written, value in _number_word_spans(text)]


def _number_word_spans(text: str) -> list[tuple[int, str, Decimal]]:
    """(start, as written, value) of each number in words of ``text`` from ten up."""
    found = []
    for match in _NUMBER_WORDS.finditer(text):
        total = current = 0
        for word in re.split(r"[- ]+", match.group(0).lower().strip(" -")):
            if word in _SMALL:
                current += _SMALL[word]
            elif word in _TENS:
                current += _TENS[word]
            elif word == "hundred":
                current = max(current, 1) * 100
            elif word in _SCALES:
                total += max(current, 1) * _SCALES[word]
                current = 0
        if total + current >= 10:
            written = match.group(0).rstrip(" -")
            percent = _PERCENT_AFTER.match(text, match.start() + len(written))
            value = Decimal(total + current)
            found.append((match.start(), (written + percent.group(0)).strip(),
                          value * Decimal("0.01")) if percent
                         else (match.start(), written.strip(), value))
    return found


def _stem(word: str) -> str:
    """A word without its plural ending (``receipts``, ``boxes``, ``deliveries``)."""
    word = word.lower()
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    return word[:-1] if word.endswith("s") and not word.endswith("ss") and len(word) > 3 \
        else word


def _count_nouns(claim: Mapping[str, Any], evidence: _Evidence) -> frozenset[str]:
    """The words that name what a count claim counts: its role's (``receipt_count``), its
    cited records' collections' (``public.app_receipt``, a BC ``purchaseReceipts``) and
    any records' (``_RECORD_NOUNS``), as stems."""
    names = [str(claim.get("role") or "")]
    for given in claim["of"]:
        resolved = evidence.row(given["citation"]) or {}
        names.append(" ".join(_collection(_record(given["citation"], resolved))[1:]))
    words = {_stem(part) for name in names
             for part in _LETTERS.findall(re.sub(r"([a-z])([A-Z])", r"\1 \2", name))
             if len(part) > 2}
    return frozenset(words - _NAME_FILLER) | _RECORD_NOUNS


def _quantifies_other(text: str, end: int, nouns: frozenset[str]) -> bool:
    """Whether the number in words ending at ``end`` counts something that is no record:
    the next words of its clause (a hyphen compound's too: ``three-receipt``) name none of
    ``nouns`` and their first plural noun is in ``_NOT_RECORD_NOUNS`` (``held in two
    systems``). A number bound to no noun (``Receipts: three.``, ``Three arrived``) or to
    any other plural noun (``Three shipments arrived``) may state the count."""
    window = _CLAUSE_END.split(text[end:end + 60], 1)[0]
    words = [word.lower() for word in _LETTERS.findall(window)][:3]
    if any(_stem(word) in nouns for word in words):
        return False
    plural = next((word for word in words if len(word) > 3 and word.endswith("s")
                   and not word.endswith("ss") and word not in _NOT_PLURAL), None)
    return plural is not None and _stem(plural) in _NOT_RECORD_NOUNS


def _word_spans(text: str, value: Decimal) -> list[tuple[int, str, Decimal]]:
    """(start, as written, value) of each number in words of ``text`` that may be ``value``:
    from ten up (``_number_word_spans``), the value's own word below ten (``Three``, the
    ``three`` of ``three-receipt``; not ``one`` of ``twenty-one``) and dozens (``a dozen``)."""
    spans = [span for span in _number_word_spans(text) if span[2] == value]
    word = next((name for name, small in _SMALL.items() if 0 < small < 10 and small == value),
                None)
    if word is not None:
        spans += [(match.start(), match.group(0), value) for match in re.finditer(
            r"(?<![\w-])" + word + r"(?=-[A-Za-z]|(?![\w-]))(?![ -]dozen\b)", text, re.I)]
    for match in _DOZEN.finditer(text):
        times = match.group("times")
        factor = Decimal(1) / 2 if match.group("half") else Decimal(
            times if times.isdigit() else _SMALL.get(times.lower(), _TENS.get(times.lower(), 0))
        ) if times else Decimal(1)
        if factor and factor * 12 == value:
            spans.append((match.start(), match.group(0), value))
    return spans


def _stated_exactly(text: str, value: Decimal, pages: frozenset[int],
                    nouns: frozenset[str]) -> list[str]:
    """Each number of ``text`` (as written, in text order) that is ``value`` written as an
    exact number, not as a lower bound (``_AT_LEAST_BEFORE`` / ``_AT_LEAST_AFTER``),
    outside handles, identifiers, dates and page locators (``prose_numbers``): in digits
    always; in words too, unless they count something that is no record
    (``_quantifies_other``) while another number of the text states ``value``."""
    clean = _strip(_page_locators(text, pages, [value]),
                   lambda match: " " * len(match.group(0)))
    spans = [(match.start(), match.group(0), number_value(match), False)
             for match in NUMBER.finditer(clean)]
    spans += [(start, written, number, True) for start, written, number
              in _word_spans(clean, value)]
    spans = [(start, written, words and _quantifies_other(clean, start + len(written), nouns))
             for start, written, number, words in sorted(spans)
             if number == value and not _percent_written(written)]
    stated = not all(other for _start, _written, other in spans)
    return [written for start, written, other in spans
            if not (other and stated)
            and not _AT_LEAST_BEFORE.search(clean, max(0, start - 40), start)
            and not _AT_LEAST_AFTER.match(clean, start + len(written))]


def _names(match: re.Match[str]) -> str:
    """A known id prefix with digits is a name (blanked); otherwise keep the digits apart."""
    prefix = match.group(1)
    # Case item ids are glued (F2, P1); a single letter with a hyphen is a code (P-02458).
    if prefix in KNOWN_ID_PREFIXES and (len(prefix) > 1 or "-" not in match.group(0)):
        return " " * len(match.group(0))
    return match.group(1) + " " + match.group(2)


def _identifier(blank: Callable[[re.Match[str]], str]) -> Callable[[re.Match[str]], str]:
    """An identifier is blanked, except capitals + digits alone (``SKU-1509``,
    ``ABC12345``) under a prefix that is not a known id prefix: those digits stay numbers.
    Letters elsewhere (``AO-26005B``, ``DVS-5003-3.78L-A``) keep it an identifier."""
    def replace(match: re.Match[str]) -> str:
        digits_only = _DIGITS_ONLY_ID.fullmatch(match.group(0))
        if digits_only and digits_only.group(1) not in KNOWN_ID_PREFIXES:
            return digits_only.group(1) + " " + digits_only.group(2).replace("-", " ")
        return blank(match)
    return replace


def _strip(text: str, blank: Callable[[re.Match[str]], str]) -> str:
    for pattern in (HANDLE, _PROSE_MOMENT, ISO_DATE, _DATE_WORDS, _TIME):
        text = pattern.sub(blank, text)
    text = IDENTIFIER.sub(_identifier(blank), text)
    return _GLUED.sub(_names, _CODE_NAME.sub(_names, text))


def cited_pages(handles: Iterable[str]) -> frozenset[int]:
    """Page numbers of the original page citations among ``handles``."""
    return frozenset(int(match.group(1)) for handle in handles
                     if isinstance(handle, str) and (match := _PAGE_CITATION.match(handle)))


def _page_locators(text: str, pages: frozenset[int],
                   claimed: Iterable[Any] = ()) -> str:
    """``text`` with page locators blanked: in a "page(s) N, M ..." phrase, the leading
    numbers that are pages the item cites. The first number that is not a cited page ends
    the run and it and everything after stay (``pages 3 and 4, 1600 units`` keeps 1600;
    with no page citation nothing is a locator). Singular ``page`` takes one number, and a
    repeated number ends the run. Only ``page``/``pages`` are page words (``p. 3`` is a
    quantity needing a claim). Fail-closed backstop: a number equal to a claimed value of the item (``claimed``) is
    never a locator, so it stays a quantity for the number and the wording checks and its
    claim must fit (``Pages 4 and 3 units were produced`` with 3 claimed damaged).
    Blanking keeps the text's length."""
    if not pages:
        return text
    values = {to_decimal(value) for value in claimed} - {None}

    def blank(match: re.Match[str]) -> str:
        run, start = match.group(2), match.start(2) - match.start(0)
        out = list(match.group(0))
        single = match.group("word").lower() != "pages"
        seen: set[int] = set()
        for number in re.finditer(r"\d+", run):
            value = int(number.group(0))
            # "page" takes one number; a run ends at the first number that is not a cited
            # page or that repeats one ("Page 3, 3 cartons": the second 3 is a quantity).
            if value not in pages or value in seen or (single and seen) or \
                    Decimal(value) in values:
                break
            seen.add(value)
            for index in range(start + number.start(), start + number.end()):
                out[index] = " "
        return "".join(out)
    return _PAGE_PHRASE.sub(blank, text)


def prose_numbers(text: str, covered_texts: Iterable[str] = (),
                  pages: frozenset[int] = frozenset(),
                  claimed: Iterable[Any] = ()) -> list[tuple[str, Decimal]]:
    """(as written, value) of every number in ``text`` that is not part of a handle,
    identifier, date, time, known id name, one of ``covered_texts`` (text values a
    claim covers) or a locator of one of ``pages`` (pages the item cites; never a number
    in ``claimed``, the item's claimed values)."""
    text = _page_locators(text, pages, claimed)
    for covered in sorted({item for item in covered_texts if item}, key=len, reverse=True):
        text = text.replace(covered, " ")
    text = _strip(text, lambda match: " ")
    return _numbers_in(text) + _number_words(text)


def _numbers_in(text: str) -> list[tuple[str, Decimal]]:
    """(as written, value) of the digit numbers of ``text`` (``1,509`` is one number; a
    scale suffix is part of it: ``0.25%`` is 0.0025)."""
    return [(match.group(0), number_value(match)) for match in NUMBER.finditer(text)]


def _percent_written(written: str) -> bool:
    """Whether a prose number is written as a percent ("25%", "25 pct", "twenty-five
    percent")."""
    parsed = NUMBER.fullmatch(written)
    if parsed:
        return (parsed.group("scale") or "").strip(" \u00a0").lower() in _PERCENT_WORDS
    return re.search(r"(?:%|per ?cent|pct)$", written, re.I) is not None


def written_dates(text: str
                  ) -> list[tuple[str, tuple[str, datetime | None] | None, bool]]:
    """(as written, ``_moment`` or None for an ISO-shaped date that names no real day,
    whether ISO) of every date ``text`` writes outside a handle: ISO dates and timestamps
    on token boundaries, month-name dates with a year."""
    text = HANDLE.sub(lambda match: " " * len(match.group(0)), text)
    out: list[tuple[str, tuple[str, datetime | None] | None, bool]] = [
        (match.group(0), _moment_of(match), True) for match in _PROSE_MOMENT.finditer(text)]
    for match in _MONTH_DATE.finditer(text):
        month = (match["mon"] or match["mon2"])[:3].lower()
        try:
            day = date(int(match["y"] or match["y2"]), _MONTHS.index(month) + 1,
                       int(match["d"] or match["d2"]))
        except ValueError:
            continue
        out.append((match.group(0), (day.isoformat(), None), False))
    return out


def _date_reading(claim: Mapping[str, Any], handles: Sequence[str], evidence: _Evidence
                  ) -> _Dated | None:
    """A date claim as the prose checks read it (``_Dated``); None for any other claim.
    A declared zone that does not load is no zone here: the claim itself fails."""
    moment = _moment(claim["value"]) if claim["kind"] == "text" else None
    if moment is None:
        return None
    cell = zone = None
    handle, field = claim.get("citation"), claim.get("field")
    resolved = evidence.row(handle) if handle in handles else None
    if resolved is not None and field != PAGE_TEXT:
        exists, value = dig(resolved, field)
        held = _moment(value) if exists else None
        cell = held[1] if held else None
        try:
            zone = evidence.zone(handle, resolved, field)
        except ValueError:
            zone = None
    return moment, cell, zone


def _states(text: str, claim: Mapping[str, Any], form: str,
            reading: _Dated | None = None) -> bool:
    """Whether ``text`` states a claim's value in its form (``claim_value_unstated``): a
    name or status word in an occurrence no negator reverses (``Status: not REVISED``
    states no ``REVISED``), a date (``reading``) on a date written it covers
    (``_covers``)."""
    if form == "date":
        return reading is not None and any(
            moment is not None and _covers(reading, moment)
            for _written, moment, _iso in written_dates(text))
    want, text = ((_fold(claim["value"]), _fold(text)) if form == "enum" else
                  (_normalised(claim["value"]), _normalised_text(text)))
    return any(not _negated(want, text, at) for at in _spans(want, text))


def _uncovered(texts: Iterable[str], claims: Sequence[Mapping[str, Any]],
               dated: Sequence[_Dated], pages: frozenset[int] = frozenset()
               ) -> list[tuple[str, str, str]]:
    """(as written, detail, ``number`` or ``date``) of each number of ``texts`` no claim
    covers by value, and of each ISO date or timestamp (the whole token, its time and
    offset too) no date claim (``dated``) covers (``_covers``)."""
    numbers = [claim for claim in claims if claim["kind"] == "number"]
    percents = {to_decimal(claim["value"]) for claim in numbers
                if str(claim.get("unit", "")).strip().lower() in _PERCENT_UNITS}
    values = {to_decimal(claim["value"]) for claim in numbers
              if str(claim.get("unit", "")).strip().lower() not in _PERCENT_UNITS}
    # A date claim covers dates by ``_covers``, never as text: its value removed from a
    # timestamp would leave the timestamp's time unchecked.
    covered = [claim["value"] for claim in claims
               if claim["kind"] == "text" and _moment(claim["value"]) is None]
    out: list[tuple[str, str, str]] = []
    for text in texts:
        # A text claim's value covers the dates it holds (a quote "ETA 2026-01-09").
        rest = text or ""
        for value in sorted({value for value in covered if value}, key=len, reverse=True):
            rest = rest.replace(value, " ")
        for written, moment, iso in written_dates(rest):
            # An ISO-shaped date that names no real day (2026-02-30) is never covered.
            if iso and written not in (seen for seen, _, _ in out) and (moment is None or not any(
                    _covers(reading, moment) for reading in dated)):
                timed = moment is not None and moment[1] is not None and any(
                    reading[0][1] is None for reading in dated)
                out.append((written, f"{written} states a time of day; a date claim covers "
                                     "only the instant of the cell it was read from"
                            if timed else "", "date"))
        for written, number in prose_numbers(text or "", covered, pages, values | percents):
            if written in (seen for seen, _, _ in out):
                continue
            # The percent contract: a number written as a percent is covered ONLY by a
            # claim whose unit is a percent, compared at percent scale ("25%" by 25
            # percent, never "2500%" or "0.25%"); it never covers a unit-less, currency or
            # physical-unit claim, and a percent claim covers no other number ("25 USD").
            # A signed number is covered by its magnitude ("-6" states the difference 6).
            if _percent_written(written):
                stated = abs(number) * 100
                if stated in percents:
                    continue
                out.append((written, f"{written} is a percent; no claim of {_plain(stated)} "
                                     "with a percent unit covers it" if stated in values
                            or abs(number) in values else "", "number"))
                continue
            if number in values or abs(number) in values:
                continue
            parsed = NUMBER.fullmatch(written)  # None for a number in words
            suffix = (parsed.group("scale") or "") if parsed else ""
            unscaled = abs(number) / _SCALES_OF_SUFFIX[suffix.lower()] if suffix else None
            if unscaled is not None and unscaled in values:
                out.append((written, f"{written} is {_plain(number)} at its written scale "
                                     f"({suffix}); the claim of {_plain(unscaled)} "
                                     "states another scale", "number"))
            else:
                out.append((written, "", "number"))
    return out


def check_items(items: Iterable[Mapping[str, Any]], *, rows: Rows,
                calculations: Sequence[Mapping[str, Any]] = (),
                contracts: Contracts | None = None,
                unusable: Iterable[str] = (),
                outcome: str | None = None,
                returned: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The gate over a case's readable items.

    ``items``: ``{"id", "texts": [...], "citations": [{"handle", "status"}],
    "value_claims": [...], "workbench_statement": bool}`` (findings and gaps).
    ``rows(handle)`` returns the resolved citation view the caller re-read and
    digest-checked, or None. ``unusable`` are ids of items that cannot be read (withheld,
    changed): the result is then not passed. ``outcome`` is the case's close outcome,
    whose numbers and ISO dates must be covered by some claim of the case. ``returned``:
    what tools returned to this reader that op claims bind to (``returned_evidence``);
    without it every count is a lower bound and only the schema marks money.
    """
    evidence = _Evidence(rows, contracts, calculations, returned)
    entries, failures, all_claims, all_typed = [], [], [], []
    all_dated: list[_Dated] = []
    all_floors: list[tuple[Decimal | None, frozenset[str]]] = []
    all_exact: dict[Decimal | None, set[str]] = {}
    counts = {"claims": 0, "passed": 0, "failed": 0, "source_contract": 0, "calculation": 0,
              "untyped": 0, "unit_unverified": 0, "unstructured": 0, "unstated": 0,
              "wording": 0}
    items = list(items)
    all_pages: set[int] = set()
    for item in items:
        handles = [cite["handle"] for cite in item.get("citations") or []
                   if cite.get("handle") and cite.get("status") in (None, "resolved")]
        all_pages |= cited_pages(handles)
        claims = list(item.get("value_claims") or [])
        all_claims += claims
        readings = [_date_reading(claim, handles, evidence) for claim in claims]
        dated = [reading for reading in readings if reading is not None]
        all_dated += dated
        results = [_check_value_claim(claim, handles, evidence) for claim in claims]
        typed = _typed_claims(claims, results)
        all_typed += typed
        floors = [(to_decimal(claim["value"]), _count_nouns(claim, evidence))
                  for claim, result in zip(claims, results) if result.get("bound") == "at_least"]
        exact: dict[Decimal | None, set[str]] = {}
        for claim, result in zip(claims, results):
            if claim["kind"] == "number" and result["status"] == "passed" \
                    and result.get("bound") != "at_least":
                exact.setdefault(to_decimal(claim["value"]), set()).add(_claim_key(claim))
        all_floors += floors
        for number, keys in exact.items():
            all_exact.setdefault(number, set()).update(keys)
        if not item.get("workbench_statement"):
            results += _lower_bound_failures(item.get("texts") or [], floors, exact,
                                             cited_pages(handles))
            results += [{"value": written, "status": "failed", "code": "claim_unstructured",
                         "detail": scale or f"a {what} in the text that no value claim of "
                                            "this item covers"}
                        for written, scale, what in _uncovered(item.get("texts") or [],
                                                               claims, dated,
                                                               cited_pages(handles))]
            text = "\n".join(item.get("texts") or [])
            if text.strip():
                results += [{"value": claim["value"], "status": "failed",
                             "code": "claim_value_unstated",
                             "detail": f"the text does not state this {form} value; a value "
                                       "claim backs only what its item's text says"}
                            for claim, result, form, reading in (
                                (claim, result, result.get("value_form", "text"), reading)
                                for claim, result, reading in zip(claims, list(results),
                                                                  readings))
                            if claim["kind"] == "text" and "citation" in claim
                            and result["status"] == "passed"
                            and result.get("value_basis") != "text_span"
                            and not _states(text, claim, form, reading)]
        leads: list[dict[str, Any]] = []
        if not item.get("workbench_statement"):
            wording, leads = wording_check("\n".join(item.get("texts") or []), typed,
                                           cited_pages(handles))
            results += [{"value": entry["value"], "status": "failed", "code": entry["code"],
                         "detail": entry["detail"]} for entry in wording]
        for result in results:
            counts["claims"] += 1
            if result["status"] == "passed":
                counts["passed"] += 1
                counts[result["role_basis"]] += 1
                counts["unit_unverified"] += result.get("unit_basis") == "unverified"
            else:
                counts["failed"] += 1
                counts["unstructured"] += result["code"] == "claim_unstructured"
                counts["unstated"] += result["code"] == "claim_value_unstated"
                counts["wording"] += result["code"].startswith("claim_wording_")
                failures.append({"item": item["id"], "code": result["code"],
                                 "value": result["value"], "detail": result["detail"]})
        if results or leads:
            entries.append({"item": item["id"], "claims": results, "leads": leads})
    if outcome:
        for result in _lower_bound_failures([outcome], all_floors, all_exact,
                                            frozenset(all_pages)):
            counts["claims"] += 1
            counts["failed"] += 1
            failures.append({"item": "outcome", "code": result["code"],
                             "value": result["value"], "detail": result["detail"]})
        for written, scale, what in _uncovered([outcome], all_claims, all_dated,
                                               frozenset(all_pages)):
            counts["claims"] += 1
            counts["failed"] += 1
            counts["unstructured"] += 1
            failures.append({"item": "outcome", "code": "claim_unstructured", "value": written,
                             "detail": scale or f"a {what} in the close outcome that no "
                                                "value claim of the case covers"})
        # The outcome is the case's answer: its type words agree with the grounded types
        # of the case's claims, exactly as a finding's do.
        wording, leads = wording_check(outcome, all_typed, frozenset(all_pages))
        for entry in wording:
            counts["claims"] += 1
            counts["failed"] += 1
            counts["wording"] += 1
            failures.append({"item": "outcome", "code": entry["code"], "value": entry["value"],
                             "detail": entry["detail"]})
        if wording or leads:
            entries.append({"item": "outcome", "claims": [
                {"value": entry["value"], "status": "failed", "code": entry["code"],
                 "detail": entry["detail"]} for entry in wording], "leads": leads})
    unchecked = sorted(set(unusable), key=_id_key)
    return {"schema": SCHEMA, "items": entries, "failures": failures,
            "unchecked_items": unchecked, "counts": counts,
            "leads": sum(len(entry["leads"]) for entry in entries),
            "passed": not failures and not unchecked}


def _claim_key(claim: Mapping[str, Any]) -> str:
    """What a claim reads, so one claim made in two items is one claim."""
    return json.dumps({key: claim[key] for key in ("citation", "field", "calculation", "op",
                                                   "of", "value") if key in claim},
                      sort_keys=True)


def _lower_bound_failures(texts: Iterable[str],
                          floors: Iterable[tuple[Decimal | None, frozenset[str]]],
                          exact: Mapping[Decimal | None, set[str]], pages: frozenset[int]
                          ) -> list[dict[str, Any]]:
    """``claim_count_lower_bound`` for each lower-bound count (``floors``: value and the
    words naming what it counts) its texts write as an exact number, occurrence by
    occurrence: each other passing claim of that number (``exact``) covers one occurrence,
    never every restatement of it."""
    out, seen = [], set()
    for floor, nouns in floors:
        if floor is None:
            continue
        written = [found for text in texts
                   for found in _stated_exactly(text or "", floor, pages, nouns)]
        for found in written[len(exact.get(floor, ())):]:
            if (floor, found) in seen:
                continue
            seen.add((floor, found))
            out.append({"value": found, "status": "failed", "code": "claim_count_lower_bound",
                        "detail": "no complete count entry a tool returned lists these "
                                  "records, so the count is a lower bound: state it as at "
                                  f"least {_plain(floor)}"})
    return out


def _typed_claims(claims: Sequence[Mapping[str, Any]],
                  results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The claims as the wording check sees them: a passed claim's ``role`` is the quantity
    type the checker grounded (the source contract's role, the calculation's type by the
    type algebra, an identifier on an id column), never the name the caller chose; a
    passed calculation claim with no type is ``UNTYPED_CALCULATION``, which no word
    accepts and which blocks even an unbound word of its sentence. A failed claim keeps
    its declared role: its item fails already. A passed quote (``value_basis:
    text_span``) types none of the numbers it holds: each is an untyped number, as a page
    figure is, so a type word bound to it in the prose is never verified by the quote."""
    out = []
    for claim, result in zip(claims, results):
        if result["status"] != "passed":
            role = claim.get("role")
        elif result.get("role_basis") == "calculation" and result.get("quantity_type") is None:
            role = UNTYPED_CALCULATION
        else:
            role = result.get("quantity_type")
        out.append({"value": claim["value"], "kind": claim["kind"], "role": role})
        if result.get("value_basis") == "text_span":
            out += [{"value": _plain(number), "kind": "number", "role": None}
                    for _written, number in prose_numbers(claim["value"])]
    return out


def case_items(findings: Iterable[Mapping[str, Any]], gaps: Iterable[Mapping[str, Any]]
               ) -> tuple[list[dict[str, Any]], list[str]]:
    """(checkable items, ids of unreadable ones) from case-view findings and gaps."""
    items, unusable = [], []
    for finding in findings:
        if finding.get("statement") is None:
            unusable.append(str(finding.get("id")))
            continue
        claim = finding.get("claim") or {}
        items.append({"id": finding["id"], "texts": [finding["statement"]],
                      "citations": finding.get("citations") or [],
                      "value_claims": finding.get("value_claims") or [],
                      "workbench_statement": (claim.get("statement_by") == "workbench"
                                              or finding.get("statement_by") == "workbench")})
    for gap in gaps:
        if gap.get("citations_status") not in ("resolved", "uncited"):
            unusable.append(str(gap.get("id")))
            continue
        items.append({"id": gap["id"], "texts": [gap.get("missing") or "", gap.get("why") or ""],
                      "citations": gap.get("citations") or [],
                      "value_claims": gap.get("value_claims") or [],
                      "workbench_statement": gap.get("statement_by") == "workbench"})
    return items, unusable


def _id_key(ident: str) -> tuple[str, int]:
    match = re.match(r"^([A-Za-z]+)(\d+)$", ident)
    return (match.group(1), int(match.group(2))) if match else (ident, 0)


# --------------------------------------------------------------------------- prose wording

_TYPE_WORDS = {
    "planned": "planned", "plan": "planned", "estimated": "planned", "scheduled": "planned",
    "produced": "produced", "producing": "produced", "production": "produced",
    "manufactured": "produced", "made": "produced", "output": "produced", "yield": "produced",
    "filled": "filled", "fill": "filled", "filling": "filled",
    "inspected": "inspected", "inspection": "inspected", "accepted": "accepted",
    "ordered": "ordered", "damaged": "damaged", "received": "received",
    "shipped": "shipped", "delivered": "shipped", "rejected": "rejected",
    "retained": "retained", "returned": "returned", "qcpassed": "inspected"}
# "passed QC" / "QC passed" / "passed inspection": one inspected-family word.
_QC_PASSED = re.compile(r"\b(?:passed|passes|passing)\s+(?:QC|QA|inspection)\b"
                        r"|\b(?:QC|QA)[\s-]+(?:passed|passes|pass)\b", re.I)
# Clause separators for the binding: a type word never reaches across them.
_STOP_WORDS = frozenset({"and", "but", "or", "nor", "while", "whereas", "against", "than",
                         "vs", "versus", "compared", "exceeds", "exceeded", "exceeding",
                         "minus", "plus"})
_NEGATIONS = frozenset({"not", "never", "no"})
_WORD_TOKEN = re.compile(
    r"(?P<num>" + NUMBER.pattern + r")|(?P<word>[A-Za-z_][A-Za-z0-9_']*)|(?P<open>\()"
    r"|(?P<stop>[\n;!?)\[\],]|\.(?=\s|$)|^\s*[-*]\s)", re.M)
_MAX_GAP = 3


def _wording_accepts(typed: str, role: str | None) -> bool:
    """A type word agrees with a claim only through the claim's grounded quantity type
    (a ``ROLES`` name); any other name is ungrounded and agrees with nothing."""
    if role is None or role not in ROLES:
        return False
    return role == typed or role in _ROLE_ALIASES.get(typed, frozenset())


def _wording_verdict(typed: str, roles: set[Any]) -> tuple[str, list[str]]:
    """("accepted" | "lead" | "claim_wording_role_mismatch" | "claim_wording_role_unverified",
    the quantity roles that disagree) of a type word over a number's claim roles."""
    if any(_wording_accepts(typed, role) for role in roles):
        return "accepted", []
    quantity = sorted(str(role) for role in roles
                      if role is not None and role != UNTYPED_CALCULATION
                      and (role in QUANTITY_ROLES or role not in ROLES))
    if quantity:
        return "claim_wording_role_mismatch", quantity
    if "identifier" in roles and UNTYPED_CALCULATION not in roles:
        return "lead", []
    return "claim_wording_role_unverified", []


def wording_check(text: str, claims: Sequence[Mapping[str, Any]],
                  pages: frozenset[int] = frozenset()
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(blocking failures, advisory leads) of the quantity-type words of ``text``.

    ``claims``' ``role`` is the quantity type the checker grounded (``check_items`` passes
    ``_typed_claims``); a name that is not in ``ROLES`` is ungrounded and agrees with no
    word, so a caller-chosen derived name never types prose.

    A type word (``produced``, ``planned``, ``filled`` ...; not after ``not``/``never``)
    binds the closest number of its clause within three words: commas, semicolons, ``and``,
    ``but``, ``than``, ``against``, line breaks and bullets separate clauses (a colon, an
    equals sign and a table cell bar do not: ``Produced: 1509``, ``| produced | 1509 |``);
    a word may bind forward into a parenthesis (``Produced (1509)``) but not back out of
    one (``1503 (produced finished goods)`` is a gloss). Closest pairs first; ties take
    the number after the word; each word and number once. For a bound number with claims:
    * ``claim_wording_role_mismatch`` (blocking): its claims carry another quantity role
      (``produced 1509`` over a claim that 1509 is planned);
    * ``claim_wording_role_unverified`` (blocking): its claims carry no quantity role
      (``produced 1`` over an untyped sample count);
    * a lead (advisory): its claims are identifiers (``order line 1140 was produced``).

    Sentence backstop: a type word bound to no number (``Produced, 1509``, ``produced the
    full batch of 1509``, ``Inspected and released 1509``) is checked against the claimed
    numbers of its sentence that no type word bound (``.``/``!``/``?`` or a blank line end
    a sentence; line breaks do not, so ``Produced\\n1509`` is one sentence). It blocks
    (``claim_wording_role_mismatch``) when none of them accepts the word and one carries
    another quantity type; when they are only identifiers or untyped it is a lead
    (``prose_type_word_without_a_bound_number``).
    A number with no claim is ``claim_unstructured`` elsewhere. ``pages`` are the pages the
    text's item cites: their locators (``_page_locators``) are blanked before binding."""
    # Page locators of cited pages are blanked first, exactly as for the number check, so a
    # type word never binds (and is never used up) on "page 3".
    claimed = [claim["value"] for claim in claims if claim["kind"] == "number"]
    clean = _QC_PASSED.sub(" qcpassed ", _strip(_page_locators(text, pages, claimed),
                                                lambda match: " " * len(match.group(0))))
    clean = _IE_EG.sub(lambda match: match.group(0).replace(".", " "), clean)
    tokens: list[dict[str, Any]] = []
    sentence = 0
    for match in _WORD_TOKEN.finditer(clean):
        if match.group("num") is not None:
            tokens.append({"t": "num", "text": match.group("num"), "sent": sentence,
                           "value": number_value(match)})
        elif match.group("word") is not None:
            word = match.group("word")
            lower = word.lower()
            if lower in _STOP_WORDS:
                tokens.append({"t": "stop", "sent": sentence})
                continue
            # "not produced", and "no longer" / "not yet produced": no type word.
            negated = bool(tokens) and (tokens[-1].get("lower") in _NEGATIONS or (
                tokens[-1].get("lower") in ("longer", "yet") and len(tokens) > 1
                and tokens[-2].get("lower") in _NEGATIONS))
            tokens.append({"t": "word", "text": word, "lower": lower, "sent": sentence,
                           "typed": None if negated else _TYPE_WORDS.get(lower)})
        elif match.group("open") is not None:
            tokens.append({"t": "open", "sent": sentence})
        else:
            stop = match.group("stop")
            if (stop.startswith(".") and tokens and tokens[-1].get("lower") in _ABBREVIATIONS
                    and _DIGIT_NEXT.match(clean, match.start() + 1)):
                continue  # "p. 4", "No. 3": an abbreviation's period before a number
            blank_line = stop == "\n" and bool(tokens) and tokens[-1].get("stop") == "\n"
            tokens.append({"t": "stop", "stop": stop, "sent": sentence})
            if stop in ("!", "?") or stop.startswith(".") or blank_line:
                sentence += 1
    pairs = []
    for index, token in enumerate(tokens):
        if token["t"] != "word" or token["typed"] is None:
            continue
        for step in (1, -1):
            cursor, gap = index + step, 0
            while 0 <= cursor < len(tokens) and gap <= _MAX_GAP:
                other = tokens[cursor]
                if other["t"] == "stop" or (other["t"] == "open" and step == -1) or (
                        other["t"] == "word" and other["typed"] is not None):
                    break
                if other["t"] == "num":
                    pairs.append((gap, 0 if step == 1 else 1, index, cursor))
                    break
                if other["t"] == "word":
                    gap += 1
                cursor += step
    by_value: dict[Any, set[Any]] = {}
    for claim in claims:
        if claim["kind"] == "number":
            by_value.setdefault(to_decimal(claim["value"]), set()).add(claim.get("role"))
    used_words: set[int] = set()
    used_numbers: set[int] = set()
    failures, leads = [], []

    def report(verdict: str, quantity: list[str], word: str, typed: str, value: str,
               roles: set[Any], how: str) -> None:
        entry = {"word": word, "value": value,
                 "claim_roles": sorted(str(role) for role in roles)}
        if verdict == "lead":
            leads.append({**entry, "lead": "prose_type_word_next_to_an_identifier"})
        elif verdict == "claim_wording_role_mismatch":
            failures.append({**entry, "code": verdict,
                             "detail": f"the text calls {value} {typed} ('{word}'{how}), "
                                       "its claim says " + ", ".join(quantity)})
        else:
            why = ("its calculation has no quantity type by the type rules"
                   if UNTYPED_CALCULATION in roles else "no claim gives it a quantity role")
            failures.append({**entry, "code": verdict,
                             "detail": f"the text calls {value} {typed} ('{word}'{how}), "
                                       f"but {why}"})

    for _gap, _side, word, number in sorted(pairs):
        if word in used_words or number in used_numbers:
            continue
        used_words.add(word)
        used_numbers.add(number)
        typed = tokens[word]["typed"]
        roles = by_value.get(tokens[number]["value"])
        if roles is None:
            continue
        verdict, quantity = _wording_verdict(typed, roles)
        if verdict != "accepted":
            report(verdict, quantity, tokens[word]["text"], typed, tokens[number]["text"],
                   roles, "")
    seen: set[tuple[str, int]] = set()
    for index, token in enumerate(tokens):
        if token["t"] != "word" or token["typed"] is None or index in used_words:
            continue
        key = (token["typed"], token["sent"])
        numbers = [other for at, other in enumerate(tokens) if other["t"] == "num"
                   and other["sent"] == token["sent"] and other["value"] in by_value
                   and at not in used_numbers]
        if not numbers or key in seen:
            continue
        seen.add(key)
        roles = set().union(*(by_value[other["value"]] for other in numbers))
        verdict, quantity = _wording_verdict(token["typed"], roles)
        value = "/".join(dict.fromkeys(other["text"] for other in numbers))
        if verdict == "claim_wording_role_mismatch" or (
                verdict == "claim_wording_role_unverified" and UNTYPED_CALCULATION in roles):
            report(verdict, quantity, token["text"], token["typed"], value, roles,
                   " in the same sentence")
        elif verdict != "accepted":
            leads.append({"word": token["text"], "value": value,
                          "claim_roles": sorted(str(role) for role in roles),
                          "lead": "prose_type_word_without_a_bound_number"})
    return failures, sorted(leads, key=lambda lead: (lead["word"], lead["value"]))


# --------------------------------------------------------------------------- the prose screen
# The one prose screen (``screen_prose``) for an answer's text: the answer the gate renders
# from its templates (``operating.answer_gate.rendered_text``) and the answer text an eval
# scores (``evals.score.gate``). Words outside the claims' values state nothing no claim
# checked only if each is grammar, an ontology object-type noun (``frame_nouns``) or a
# record code (``KNOWN_ID_PREFIXES``), a quantity word framing a claim of its role, or a
# state word bound to a checked claim about that state.
PROSE_UNCHECKED = "claim_prose_unchecked"
# How the answer says a value rests on a lead-tier link (the gate's phrase writes it).
LEAD_MARK = "(lead: link not verified)"
# Function words, reporting verbs and the nouns of a field or value: the whole of the
# prose's own grammar (an allowlist: any other word is unchecked).
_PROSE_GRAMMAR = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "it", "its", "they", "their", "them",
    "is", "are", "was", "were", "be", "been", "has", "have", "had",
    "per", "on", "in", "for", "of", "by", "from", "at", "as", "to", "with", "under",
    "according", "and", "also", "least",
    "recorded", "reads", "shows", "holds", "lists", "listed", "states", "says", "gives",
    "comes", "carries", "carrying",
    "number", "numbers", "id", "ids", "value", "values", "field", "fields", "quantity",
    "quantities", "cell", "cells", "row", "rows", "date", "dates", "code", "codes", "name",
    "names", "unit", "units",
    *_RECORD_NOUNS, *(noun + "s" for noun in _RECORD_NOUNS), "entries"})
# A state word (``late``, ``received``) states a state of the record: it frames a value only
# beside a checked claim whose own field (its role, column or schema property, never the
# relation or type it sits on) states that state happened (``received_date``,
# ``fully_received``, ``shipped_at``; ``Receipt``, ``shipment_code`` or ``supplier`` do
# not). ``due`` is the one plan word: it binds to the planned date (``expected_date``).
_STATE_STEMS = {
    **dict.fromkeys(("late", "overdue"), frozenset({"late", "lateness", "overdue",
                                                    "delayed"})),
    "due": frozenset({"due", "expected", "promised", "requested"}),
    **dict.fromkeys(("confirmed", "confirms"), frozenset({"confirmed"})),
    **dict.fromkeys(("supplies", "supplied", "supply"), frozenset({"supplied"})),
    **dict.fromkeys(("received", "receives", "receive"), frozenset({"received"})),
    **dict.fromkeys(("shipped", "ships"), frozenset({"shipped"})),
    "delivered": frozenset({"delivered"}),
    "invoiced": frozenset({"invoiced"}),
    "paid": frozenset({"paid"}),
    "ordered": frozenset({"ordered"})}
# Words of a field that plans an event, not records it (``requestedReceiptDate``,
# ``expected_received_date``): such a field binds no event state, only ``due``.
_PLANNED_WORDS = frozenset({"expected", "requested", "planned", "promised", "scheduled",
                            "target", "estimated", "due", "projected", "forecast"})
# Values that deny a state: ``not received`` frames only a claim holding one of them.
_FALSE_VALUES = frozenset({"false", "no", "n", "f"})
# Words between ``not`` and the state word it denies (``not been received``).
_NOT_FILLER = frozenset({"be", "been"})
# Parts of a curated type name that are no record noun, or that state a fact or a state
# beside a value (``Aged``, ``ShipRelease``, ``AssemblyOrderDamage``, ``MCApproval``,
# ``ProductionTask``, ``BatchSchedule``): never frame nouns.
_NOT_FRAME_NOUNS = frozenset({"aged", "general", "ship", "release", "payable", "receivable",
                              "damage", "approval", "decision", "credit", "share", "point",
                              "measure", "schedule", "bill", "packing", "manufacturing",
                              "production"})
_PROSE_TOKEN = re.compile(r"(?P<word>[A-Za-z]+)|(?P<space>\s+)|(?P<punct>[.,;:()?!])")
_SENTENCE_END = frozenset(".?!;")
_NAME_PART = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")


def _name_words(name: Any) -> list[str]:
    """The lowercase words of an identifier (``po_number``, ``fullyReceived``,
    ``public.app_receipt``, ``PurchaseOrder``)."""
    return [part.lower() for part in _NAME_PART.findall(name)] if isinstance(name, str) else []


def _plural(noun: str) -> str:
    if noun.endswith(("s", "x", "ch", "sh")):
        return noun + "es"
    if noun.endswith("y") and noun[-2:-1] not in ("a", "e", "i", "o", "u"):
        return noun[:-1] + "ies"
    return noun + "s"


@lru_cache(maxsize=1)
def frame_nouns() -> frozenset[str]:
    """The nouns a prose may frame values with: each part of a curated ontology object
    type's name (``Lot``, ``Receipt``, ``SupplierConfirmation``, ``ItemLedgerEntry``),
    lowercase, of three letters or more, and its plural; none when the schema does not load
    (a template then falls back to lines: stricter, never looser)."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: no frame nouns
        return frozenset()
    out: set[str] = set()
    for type_id, origin in schema.origin_of_type.items():
        if origin != "curated":
            continue
        for word in _name_words(type_id):
            if len(word) >= 3 and word.isalpha() and word not in _NOT_FRAME_NOUNS:
                out.update((word, _plural(word)))
    return frozenset(out)


@lru_cache(maxsize=1)
def _schema_names() -> tuple[Mapping[tuple[str, str], frozenset[tuple[str, str]]],
                             Mapping[str, frozenset[str]]]:
    """((relation or BC entity set, column) -> {(type, property)} of each Portal mirror and
    BC column the schema binds, relation -> {type}); empty when the schema does not load."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: names from the citation alone
        return {}, {}
    columns: dict[tuple[str, str], set[tuple[str, str]]] = {}
    relations: dict[str, set[str]] = {}
    for type_id, spec in schema.object_types.items():
        for prop in spec.get("properties") or ():
            for source in prop.get("sources") or ():
                _type, binding = schema.bindings.get(source.get("binding"), (None, {}))
                path, relation = source.get("path"), binding.get("relation")
                if isinstance(path, str) and isinstance(relation, str):
                    columns.setdefault((relation, path), set()).add((type_id, prop["name"]))
                    relations.setdefault(relation, set()).add(type_id)
    return ({key: frozenset(value) for key, value in columns.items()},
            {key: frozenset(value) for key, value in relations.items()})


def _relation(handle: Any) -> str | None:
    """The relation (``public.app_receipt``) or BC entity set a cited handle names."""
    if not isinstance(handle, str) or "://" not in handle:
        return None
    scheme, rest = handle.split("://", 1)
    parts = rest.split("@", 1)[0].split("#", 1)[0].split("/")
    if scheme == "bc":
        return parts[2] if len(parts) == 4 else None
    return parts[0] or None


def _column(field: Any) -> str | None:
    """The row path a field reads (``po_number``, ``custom_fields.fmGeneratedPoId``)."""
    if not isinstance(field, str) or not _FIELD.match(field):
        return None
    return field.split(".", 2)[2] if field.startswith("rows.") else field.split(".", 1)[1]


def _cells(claim: Mapping[str, Any]) -> list[tuple[str | None, str | None]]:
    """(relation, column) of each cell a value claim reads (its citation, or each ``of``)."""
    given = [claim] if "citation" in claim else \
        [entry for entry in claim.get("of") or () if isinstance(entry, Mapping)]
    return [(_relation(entry.get("citation")), _column(entry.get("field"))) for entry in given]


def _concept(claim: Mapping[str, Any]) -> frozenset[str]:
    """The words of what a value claim is about: its role, and of each cell it reads its
    column, relation, schema property and that property's type."""
    columns, relations = _schema_names()
    words = set(_name_words(claim.get("role")))
    for relation, column in _cells(claim):
        words.update(_name_words(column), _name_words(relation))
        for type_id, prop in columns.get((relation, column), ()) if column else ():
            words.update(_name_words(type_id), _name_words(prop))
        for type_id in relations.get(relation, ()) if relation else ():
            words.update(_name_words(type_id))
    return frozenset(words)


def _field_words(claim: Mapping[str, Any]) -> frozenset[str]:
    """The words of the field a value claim states: its role, and of each cell it reads its
    column and schema property (never the relation or type the cell sits on)."""
    columns, _relations = _schema_names()
    words = set(_name_words(claim.get("role")))
    for relation, column in _cells(claim):
        words.update(_name_words(column))
        for _type, prop in columns.get((relation, column), ()) if column else ():
            words.update(_name_words(prop))
    return frozenset(words)


def field_label(claim: Mapping[str, Any]) -> str:
    """How an answer line names what a value claim states (``PO number: PO#5447``): its role,
    else the schema property of the cited column, else the column, as words with record
    codes capitalised; else its operation. Never a field path."""
    if isinstance(claim.get("role"), str) and claim["role"]:
        return " ".join(_name_words(claim["role"])) or claim["role"]
    field = claim.get("field")
    if isinstance(field, str) and "citation" in claim:
        relation, column = _cells(claim)[0]
        names = sorted({prop for _type, prop in _schema_names()[0].get((relation, column), ())}
                       ) if column else []
        name = names[0] if len(names) == 1 else (column or field).rsplit(".", 1)[-1]
        words = [word.upper() if len(word) > 1 and word.upper() in KNOWN_ID_PREFIXES
                 else word for word in _name_words(name)]
        if words:
            return " ".join(words)
    return str(claim.get("op") or "value")


def _number_claim(claim: Mapping[str, Any]) -> bool:
    return "op" in claim or claim.get("role") in QUANTITY_ROLES or \
        bool(_PLAIN_NUMBER.match(str(claim.get("value", ""))))


def _found_spans(text: str, claims: Sequence[Mapping[str, Any]]) -> list[tuple[int, int, int]]:
    """Where ``text`` writes each claim's value (start, end, claim index): its value on token
    boundaries, longest first, and for a number each figure of that value; each span taking
    the claim's unit or currency written right after it. No two spans overlap."""
    taken: list[tuple[int, int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e, _ in taken)

    order = sorted(range(len(claims)), key=lambda i: -len(str(claims[i].get("value", ""))))
    for index in order:
        value = str(claims[index].get("value", ""))
        found = [(at, at + len(value)) for at in _spans(value, text)] if value else []
        number = to_decimal(value) if _number_claim(claims[index]) else None
        if number is not None:
            found += [match.span() for match in NUMBER.finditer(text)
                      if not match.group("scale") and number_value(match) == number]
        for start, end in sorted(set(found)):
            for key in ("unit", "currency"):
                after = claims[index].get(key)
                if isinstance(after, str) and after and text.startswith(" " + after, end):
                    end += 1 + len(after)
            if free(start, end):
                taken.append((start, end, index))
    return sorted(taken)


def screen_prose(text: str, claims: Sequence[Mapping[str, Any]],
                 spans: Sequence[tuple[int, int, int]] | None = None,
                 gaps: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """The failures of an answer text against the checked value ``claims`` it writes (each
    ``{"value", "code", "detail"}``; none: the text states only what they state).
    ``spans`` are where it writes them (start, end, claim index: the gate's rendering knows);
    without them each claim's value is found in the text (an eval answer's prose).

    * ``claim_value_unstated``: a claim's value written negated (``is not OPEN``,
      ``passed? No``): the text denies what the claim states.
    * ``claim_prose_unchecked``, at most one per sentence: a word that is not grammar, an
      ontology object-type noun (``frame_nouns``) or a record code (``AO``, as written); a
      digit, a non-ASCII letter, a sign or a symbol outside a value; a value glued to a word
      or to another value with no word between them (``{p}{m}``, ``{p}k``); a quantity word
      (``planned``) beside a number claim of another role, or beside none; a state word
      (``late``, ``due``, ``confirmed``, ``supplies``, ``received``) no checked claim of the
      sentence is about (``late`` needs a lateness claim), or one a false claim value denies;
      ``not`` anywhere but before a state word a false claim value states (``not
      received``). A word of what a claim of the sentence is about (its role, column,
      schema property or type: ``order total``), or ``total`` beside a computed claim, is
      not unchecked.

    ``gaps`` are the answer's declared gaps (``{"state", "subject"}``): a sentence naming a
    gap's subject (``shipment``) may say its state as declared (``is not ingested``; ``is
    ingested`` inverts it), a ``GAP_STATES`` state only; any other gap adds nothing."""
    text = text if isinstance(text, str) else ""
    if spans is None:
        text = text.replace(LEAD_MARK, " " * len(LEAD_MARK))
        spans = _found_spans(text, claims)
    declared = _declared_gaps(gaps)
    failures: list[dict[str, Any]] = []
    tokens: list[tuple[str, str, int | None]] = []   # (kind, text, claim index)
    at = 0
    for start, end, index in sorted(spans):
        if start < at:
            return [{"value": "", "code": PROSE_UNCHECKED, "detail": "overlapping values"}]
        _tokenize(text[at:start], tokens)
        tokens.append(("name", text[start:end], index))
        claim = claims[index]
        if "op" not in claim and _negated(str(claim.get("value", "")), text, start):
            failures.append({"value": str(claim.get("value", "")),
                             "code": "claim_value_unstated",
                             "detail": "the text writes this value negated; a value claim "
                                       "backs only what the text says"})
        at = end
    _tokenize(text[at:], tokens)
    sentence: list[tuple[str, str, int | None]] = []
    tokens.append(("punct", ".", None))
    for position, token in enumerate(tokens):
        sentence.append(token)
        # A sentence ends at its stop before a space or the end: ``{p}.{m}`` is one.
        ends = position + 1 == len(tokens) or tokens[position + 1][0] == "space"
        if token[0] == "punct" and token[1] in _SENTENCE_END and ends or \
                token[0] == "space" and "\n" in token[1]:
            problem = _sentence_problem(sentence, claims, declared)
            if problem is not None:
                failures.append({"value": problem[0], "code": PROSE_UNCHECKED,
                                 "detail": problem[1]})
            sentence = []
    return failures


def _declared_gaps(gaps: Any) -> tuple[tuple[frozenset[str], frozenset[str]], ...]:
    """(subject words, state words) of each declared gap of a ``GAP_STATES`` state and an
    object-type subject; anything else is dropped (it permits no word)."""
    out = []
    nouns = frame_nouns()
    for gap in gaps if isinstance(gaps, (list, tuple)) else ():
        if not isinstance(gap, Mapping) or gap.get("state") not in GAP_STATES or \
                not isinstance(gap.get("subject"), str):
            continue
        subject = frozenset(word for word in _name_words(gap["subject"]) if len(word) >= 3)
        if subject and subject <= nouns:
            out.append((subject, frozenset(gap["state"].split("_"))))
    return tuple(out)


def _tokenize(text: str, tokens: list[tuple[str, str, int | None]]) -> None:
    at = 0
    while at < len(text):
        match = _PROSE_TOKEN.match(text, at)
        if match is None:   # a digit run is one token (``99``, never ``9``)
            run = re.match(r"\d+", text[at:])
            other = run.group(0) if run else text[at]
            tokens.append(("other", other, None))
            at += len(other)
            continue
        tokens.append((match.lastgroup or "other", match.group(0), None))
        at = match.end()


def _sentence_problem(tokens: Sequence[tuple[str, str, int | None]],
                      claims: Sequence[Mapping[str, Any]],
                      declared: Sequence[tuple[frozenset[str], frozenset[str]]] = ()
                      ) -> tuple[str, str] | None:
    """(the offending token, why) of the first thing one sentence states that no claim of it
    checked, or None."""
    named = [index for kind, _, index in tokens if kind == "name"]
    said = {text.lower() for kind, text, _ in tokens if kind == "word"}
    # each state word of a gap the sentence names, and whether its state negates it
    # (``not_ingested``: ``ingested`` only after ``not``); two polarities permit neither
    polarity: dict[str, set[bool]] = {}
    for subject, state in declared:
        if all(word in said or _plural(word) in said for word in subject):
            for word in state - {"not"}:
                polarity.setdefault(word, set()).add("not" in state)
    gap_words = {word: next(iter(sides)) for word, sides in polarity.items() if len(sides) == 1}
    # the sentence may name what its claims are about (``order total``) and call a computed
    # claim a ``total``; no schema, no such word (stricter, never looser)
    nouns = frame_nouns()
    about = frozenset().union(*(_concept(claims[index]) for index in named)) | (
        {"total"} if any("op" in claims[index] or "calculation" in claims[index]
                         for index in named) else set()) if nouns else frozenset()

    def bound(word: str, denied: bool) -> bool:
        stems = _STATE_STEMS.get(word)
        plan = word == "due"
        return stems is not None and any(
            not _number_claim(claims[index]) and _field_words(claims[index]) & stems
            and (plan or not _field_words(claims[index]) & _PLANNED_WORDS)
            and (_fold(str(claims[index].get("value", ""))) in _FALSE_VALUES) == denied
            for index in named)

    def beside(index: int) -> list[int]:
        out = []
        if index >= 2 and tokens[index - 1][0] == "space" and tokens[index - 2][0] == "name":
            out.append(tokens[index - 2][2])
        rest = index + 1
        if rest < len(tokens) and tokens[rest][1] == ":":
            rest += 1
        if rest < len(tokens) and tokens[rest][0] == "space" and " " == tokens[rest][1]:
            rest += 1
        if rest > index + 1 and rest < len(tokens) and tokens[rest][0] == "name":
            out.append(tokens[rest][2])
        return out

    def next_word(index: int) -> tuple[int, str] | None:
        for at in range(index + 1, len(tokens)):
            kind, text, _ = tokens[at]
            if kind == "word" and text.lower() not in _NOT_FILLER:
                return at, text.lower()
            if kind != "space" and kind != "word":
                return None
        return None

    def denied_by_not(index: int) -> bool:
        for at in range(index - 1, -1, -1):
            kind, text, _ = tokens[at]
            if kind == "word" and text.lower() in _NOT_FILLER or kind == "space":
                continue
            return kind == "word" and text.lower() == "not"
        return False

    last_name = None
    for index, (kind, text, _claim) in enumerate(tokens):
        if kind == "other":
            return text, "a digit, letter, sign or symbol no claim states"
        if kind == "name":
            glued = [tokens[i] for i in (index - 1, index + 1) if 0 <= i < len(tokens)]
            if any(k in ("word", "name") for k, _, _ in glued) or last_name is not None and \
                    not any(k == "word" for k, _, _ in tokens[last_name + 1:index]):
                return text, "a value glued to a word or to another value"
            last_name = index
            continue
        if kind != "word":
            continue
        lower = text.lower()
        if lower == "not":
            following = next_word(index)
            if following is not None and gap_words.get(following[1]) is True:
                continue
            if following is None or following[1] not in _STATE_STEMS or \
                    not bound(following[1], True):
                return text, "a negation no false claim value states"
            continue
        if lower in QUANTITY_ROLES:
            numbers = [i for i in beside(index) if _number_claim(claims[i])]
            if numbers:
                if any(claims[i].get("role") != lower for i in numbers):
                    return text, "a quantity word beside a claim of another role"
                continue
        if lower in gap_words:
            if denied_by_not(index) != gap_words[lower]:
                return text, "a gap state the text writes inverted"
            continue
        if lower in _STATE_STEMS:
            if not bound(lower, denied_by_not(index)):
                return text, "a state word no checked claim of the sentence is about"
            continue
        if lower in QUANTITY_ROLES:
            return text, "a quantity word beside no claim of its role"
        if lower in _PROSE_GRAMMAR or lower in nouns or lower in about or \
                len(text) > 1 and text in KNOWN_ID_PREFIXES:
            continue
        return text, "a word that is not grammar, an object-type noun or a record code"
    return None


__all__ = ["FAILURE_CODES", "HANDLE", "IDENTIFIER", "ISO_DATE", "LEAD_MARK", "NUMBER",
           "OTHER_ROLES", "PROSE_UNCHECKED", "QUANTITY_ROLES", "ROLES", "SCHEMA", "case_items",
           "check_items", "dig", "field_label", "frame_nouns", "screen_prose", "wording_check",
           "number_value", "parse_value_claims", "prose_numbers", "quoted_spans",
           "returned_evidence", "schema_role", "supported_quotes", "to_decimal", "value_form",
           "written_dates"]
