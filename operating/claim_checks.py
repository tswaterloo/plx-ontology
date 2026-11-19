"""Checked value claims: the quantitative statements of a case, structured and verified.

A citation that resolves does not prove the sentence that cites it (requirements
RSN-06): "produced 1,509" citing an assembly-order item row is wrong when 1,509 is
that row's *planned* ``quantity``. Planned, produced, filled, inspected, ordered and
damaged quantities share a dimension but are different quantities (pack 02, AO-26005B:
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
    {"value": "420.13", "op": "avg", "of": [{"citation": "...", "field": "row.amount"}, ...],
     "currency": "USD"}                                          # also "min", "max"
    {"value": "50.19", "op": "ratio", "unit": "percent",          # also "difference"
     "of": [{"citation": "...", "field": "row.amount"}, {"citation": "...", "field": ...}]}
    {"value": "386.69", "op": "expression", "expression": "x0 - x1",
     "of": [{"citation": "original://...#page=11", "field": "text", "value": "18,901.08",
             "quote": "$18,901.08 received"}, ...]}

``value`` is an integer or a string (a decimal string is a number, anything else is
text); floats are refused, as for calculation literals. ``role`` is one of ``ROLES``
(quantity types plus non-quantity kinds such as ``identifier``) or, for a calculation,
a derived role name. An ``op`` claim computes its number over rows the item cites
(``of``, at most ``MAX_OP_INPUTS``): ``count`` over the distinct records cited (a mirror
row by relation and key, any other record by its re-read citation without version or
fragment; one record cited twice fails), ``sum`` the Decimal sum of one field of each,
checked as a recorded calculation is (recomputed from the re-read rows, typed by the type
algebra, every input in one unit, its own or the claim's, that no contract contradicts);
``avg``, ``min`` and ``max`` of one field of each (typed as their sum is), ``difference``
(``x0 - x1``, typed by the algebra) and ``ratio`` (``x0 / x1``, never typed; unit ``1``,
or ``percent`` for x 100, its inputs' own unit never the claim's) of exactly two, the same
input shape and unit and currency rules as a sum (a ratio's one currency cancels: it
states none), recomputed from the re-read cells by ``answer_contract.computed_claim``, the
eval scorer's rule: exact, except that an average or a ratio may be written rounded half-up
to its written decimals, never fewer than its floor (an average: its most precise
input's, up to 2; a ratio: 2, a whole percent); a ratio over a zero denominator holds no
value. Each backs the prose numbers it states as a sum's value does.
A count has no quantity type; its ``role``, like a sum's role outside ``ROLES``, is the
value's name only and types nothing. An ``expression`` names its inputs ``x0``, ``x1``, ...
(each at least once, and in its expansion into monomials never twice in one, never in
two equal ones and never alone beside another use of it; no number of its own; ``round_to_cents(...)`` around the whole rounds half-up
to 0.01): each input a figure as written that its cited field holds, checked as a cited
value claim (``expression_inputs``; a percent only where printed as one), and the result
the case evaluator's over those figures, as the eval scorer recomputes it (untyped).

A count is **exact** only when it binds to one complete count entry a tool returned to
this reader (``returned["counts"]``, ``returned_evidence``: a ``plx.aggregate`` count group
of a complete answer with no gap, listing every record it counts, taken now: a call naming
an ``as_known_at`` counts what was known then; that tool's identity lookup already left a
group with twins, one record held in two systems, without a value): the claimed value is
the entry's and every record the claim cites is one of the entry's (under any version
pin, or a twin the entry names as confirmed: ``twins_cited``, the same record held in
another system; a lead twin never). Any other count is a
**lower bound** (``bound: at_least``): the records it cites, where records of two source
collections (two relations, entity sets, mailboxes, drives) may be one record held twice
and so count once: the claimed value must be the most records it cites that are surely
distinct, those of one collection or those one count group a tool's identity lookup told
apart lists (a group with a value, taken now, complete or not: their deduplicated union),
or the value of a count group whole but for a source captured in part
(``source_partly_read``, the citation cap beside it: every member it counted is held, so
the source has at least that many) whose members the claim cites,
and its item's text (and the close outcome) states it only as ``at least N`` / ``no fewer
than N`` / ``N or more`` (``claim_count_lower_bound``).
A ``count_distinct`` claim states how many distinct values a ``plx.aggregate``
``count_distinct`` counted: exact only when it binds to that tool's complete entry (whole,
as known now, every record listed or the first ``MAX_COUNT_CITATIONS`` under the citation
cap alone, never more values than records) with the claimed value, every cited record one
the entry lists; never a lower bound, and a plain count's entry never binds it. The same
complete group is also a plain count of its ``inputs``, the records holding a value.
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
  untyped, as a page figure is, and so is every number in it for the wording check. A
  record's free-text field (``text_field``: ``row.content_text``, ``row.description``,
  ``row.body``, ``row.notes`` and the like, of any record) is prose: a text value on it
  that is not the whole cell is a quote as above, and a number value a figure of it
  (``_check_text_figure``), the page-figure rule: the number at its written scale, not
  negated (``not 12``) nor bounded (``at least 30``), outside identifiers and dates, with
  the claim's unit and currency where it states them (printed beside it; a bare ``$``
  names no currency), untyped; two occurrences with different meanings (the currency and
  word printed with each: ``30 cases``, ``30 days``) are ambiguous unless a ``quote`` pins
  one. A cell that is the value itself, and any structured column, keep the field rule.
  A cited claim may add ``quote``, the
  passage as written that holds its value: checked after the value passes, never instead
  (``_quote_mismatch``): the quote must hold the value as written and the cited text the
  quote (an original's text as a quote of it, any other cell verbatim), else
  ``claim_value_mismatch``;
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
  prefix (``SKU-1509``, ``x1509``) leave the digits a number, except, in the answer text
  (``outcome``), a gate label (``_is_gate_label``: hyphenated, ``SKU-1509``, or an
  identifier kind's shape; never ``NET30``, ``QTY-12``, ``Q3``). Numbers in words count from
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
* ``claim_currency_mismatch``: a sum's currency does not hold (above); any op's inputs
  are of more than one dimension (``answer_contract.dimension_problem``: two currencies,
  money beside a plain number, money in no known currency); or a recorded calculation
  adds or subtracts money in two currencies, or money whose currency no tool returned
  (``_calculation_currency_problem``);
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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
import ast
import bisect
import json
import math
import re
import unicodedata
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ontology.core import ValidationError
from ontology.measure import exact_normal
from ontology.units import (ISO_4217, RECEIVE_ALIASES, SIGN_CURRENCIES, UOM_ALIASES,
                            UOM_CANONICAL, WORD_CODES, js_trim)

from . import quantity_types as _qt
from .answer_contract import (NUMERIC_OPS, PAIR_OPS, ROUNDED_OPS, Dimension,
                              MixedDimensions, ZeroDenominator, calendar_day,
                              computed_claim, dimension_problem)
from .citation_schemes import HANDLE_PREFIX
from .cases import (GAP_STATES, UnitMismatch, evaluate as _evaluate, format_unit, parse_unit,
                    quantity_role as _quantity_role)

from .claim_checks_grammar import (
    _COLUMN_FIELD, Contracts, _ISO_DAY, QUANTITY_ROLES, ROLES, Rows, SCHEMA)
from .claim_checks_parse import (
    dig, to_decimal)
from .claim_checks_cells import (
    _bc_entity_set, _Dated)
from .claim_checks_text import (
    asked_tokens_blanked, cited_pages, _count_nouns, _date_reading, _states, _uncovered)
from .claim_checks_values import (
    _check_value_claim)
# operating/fast_answer.py (in flight tonight) still imports these through this module
from .claim_checks_values import returned_evidence  # re-export
from .claim_checks_wording import (  # re-export
    _claim_key, _id_key, _lower_bound_failures, screen_prose, _typed_claims, wording_check)


class _Evidence:
    """Rows of one case read once; recorded units per (handle, field); calculations."""

    def __init__(self, rows: Rows, contracts: Contracts | None,
                 calculations: Sequence[Mapping[str, Any]],
                 returned: Mapping[str, Any] | None = None) -> None:
        self._rows, self.contracts = rows, contracts
        returned = returned or {}
        # Complete count entries as (value, the records they list); the records of each
        # entry a tool's identity lookup told apart (``distinct``); money cells' currencies.
        # A count of distinct values (``op: count_distinct``) is kept apart: it binds only a
        # claim of that op, and its records (several may hold one value) tell none apart.
        entries = [entry for entry in returned.get("counts") or () if isinstance(entry, Mapping)]
        records = [entry for entry in entries if entry.get("op", "count") == "count"]
        # A complete entry also maps each confirmed twin it names (the same record held
        # in another system) to the member it is (``twins``): citing the twin cites it.
        self.counts = [(to_decimal(entry.get("value")), frozenset(entry.get("records") or ()))
                       for entry in records if entry.get("complete") is True]
        self.count_twins = [dict(entry.get("twins") or {})
                            for entry in records if entry.get("complete") is True]
        self.distinct = [frozenset(entry.get("records") or ()) for entry in records
                         if entry.get("complete") is True or entry.get("distinct") is True]
        # Count entries whole but for a source captured in part (``floor``): (value, the
        # records they list); the value is a lower bound, never exact.
        self.floors = [(to_decimal(entry.get("value")), frozenset(entry.get("records") or ()))
                       for entry in records if entry.get("floor") is True
                       and entry.get("complete") is not True
                       and entry.get("distinct") is True]
        self.value_counts = [(to_decimal(entry.get("value")),
                              frozenset(entry.get("records") or ()))
                             for entry in entries if entry.get("op") == "count_distinct"
                             and entry.get("complete") is True]
        # complete plx.aggregate sums: (value, currency, the cells they cite)
        self.sums = [(to_decimal(entry.get("value")), entry.get("currency"),
                      frozenset(tuple(cell) for cell in entry.get("cells") or ()))
                     for entry in entries if entry.get("op") == "sum"
                     and entry.get("complete") is True]
        # plx.table sums whole but for a source read in part, no input below zero
        # (``floor``): (value, currency, the cells they cite); each a lower bound only
        self.sum_floors = [(to_decimal(entry.get("value")), entry.get("currency"),
                            frozenset(tuple(cell) for cell in entry.get("cells") or ()))
                           for entry in entries if entry.get("op") == "sum"
                           and entry.get("floor") is True and entry.get("complete") is not True]
        self.currencies: Mapping[tuple[str, str], str | None] = \
            returned.get("currencies") or {}
        # The days a ``days`` claim may count from uncited (``_ISO_DAY`` strings).
        self.reference_days = frozenset(
            day for day in returned.get("reference_days") or ()
            if isinstance(day, str) and _ISO_DAY.fullmatch(day) and calendar_day(day))
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

    def tool_sum(self, claim: Mapping[str, Any], floors: bool = False) -> bool:
        """Whether a ``sum`` claim binds to a complete ``plx.aggregate`` sum this reader
        was returned: the same value (Decimal) and currency (both none, or the same code),
        every cell the claim cites one the sum cites (``_aggregate_sums``). ``floors``:
        to a sum that is only a lower bound instead (``sum_floors``: a source read in
        part), which the claim then passes as ``at least``."""
        if claim.get("op") != "sum":
            return False
        value = to_decimal(claim.get("value"))
        cells = {(given.get("citation"), given.get("field")) for given in claim.get("of") or ()
                 if isinstance(given, Mapping)}
        return value is not None and bool(cells) and any(
            total == value and currency == claim.get("currency") and cells <= listed
            for total, currency, listed in (self.sum_floors if floors else self.sums))

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
def _schema_roles() -> Mapping[tuple[str, str, str], tuple[str, str] | None]:
    """(source, BC entity set or mirror relation, column) -> (``ontology:<type>.<property>``,
    quantity role) of every quantity property the ontology schema binds to a BC column
    (and, for such a property, to a Portal reporting API column) without a row condition; None
    for a column two properties type differently. Empty when the schema does not load (no
    role is then grounded: stricter, never looser)."""
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
            sources = prop.get("sources") or ()
            # A Portal column is grounded only for a property the schema also reads from a
            # BC column (one cross-system quantity: a PO line's ordered and received).
            in_bc = any(schema.bindings.get(given.get("binding"), (None, {}))[1]
                        .get("source") == "bc" for given in sources)
            for source in sources:
                _type, binding = schema.bindings.get(source.get("binding"), (None, {}))
                path = source.get("path")
                if binding.get("source") not in (("bc", "portal_api") if in_bc else ("bc",)) \
                        or binding.get("where") or not isinstance(path, str) or "." in path:
                    continue
                key = (binding["source"], binding["relation"], path)
                if key not in out:
                    out[key] = (f"ontology:{type_id}.{prop['name']}", role)
                elif out[key] is not None and out[key][1] != role:
                    out[key] = None
    return out


def schema_role(handle: str, resolved: Mapping[str, Any], field: str
                ) -> dict[str, Any] | None:
    """The ontology schema's quantity role of a cited BC or Portal mirror row column
    (``row.<column>``), as a contract ``{"id", "role", "schema": True}`` (the role the
    projection gives the value read there; ``ontology/project.py``: a Portal PO line's
    ``quantity`` is ordered, its ``quantity_received`` received); None for any other
    citation or column. A BC entity set is the re-read view's relation (``bc.<entity
    set>``) or its own citation (a BC capture view); a mirror relation is the view's."""
    match = _COLUMN_FIELD.match(field)
    if match is None or not field.startswith("row."):
        return None
    if handle.startswith("bc://"):
        key = ("bc", _bc_entity_set(resolved))
    elif handle.startswith("api://") and isinstance(resolved.get("relation"), str):
        key = ("portal_api", resolved["relation"])
    else:
        return None
    found = _schema_roles().get((*key, match.group(1))) if key[1] else None
    return None if found is None else {"id": found[0], "role": found[1], "schema": True}


def check_items(items: Iterable[Mapping[str, Any]], *, rows: Rows,
                calculations: Sequence[Mapping[str, Any]] = (),
                contracts: Contracts | None = None,
                unusable: Iterable[str] = (),
                outcome: str | None = None,
                returned: Mapping[str, Any] | None = None,
                question: str | None = None) -> dict[str, Any]:
    """The gate over a case's readable items.

    ``items``: ``{"id", "texts": [...], "citations": [{"handle", "status"}],
    "value_claims": [...], "workbench_statement": bool}`` (findings and gaps).
    ``rows(handle)`` returns the resolved citation view the caller re-read and
    digest-checked, or None. ``unusable`` are ids of items that cannot be read (withheld,
    changed): the result is then not passed. ``outcome`` is the case's close outcome,
    whose numbers and ISO dates must be covered by some claim of the case; a token of it
    the ``question`` holds (``asked_tokens_blanked``: ``Q9`` of "the Q9 review") is the
    question restated, never a figure it asserts. ``returned``:
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
        outcome = asked_tokens_blanked(outcome, question, [
            claim["value"] for claim in all_claims if claim.get("kind") == "text"])
        for result in _lower_bound_failures([outcome], all_floors, all_exact,
                                            frozenset(all_pages)):
            counts["claims"] += 1
            counts["failed"] += 1
            failures.append({"item": "outcome", "code": result["code"],
                             "value": result["value"], "detail": result["detail"]})
        for written, scale, what in _uncovered([outcome], all_claims, all_dated,
                                               frozenset(all_pages), labels=True):
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


__all__ = ["asked_tokens_blanked", "QUANTITY_ROLES", "ROLES", "SCHEMA", "check_items", "dig",
           "screen_prose", "wording_check", "schema_role", "to_decimal"]
