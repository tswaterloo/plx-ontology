"""Checking one value claim against its cited cells: same value and identifier, quotes,
counts, expressions and their currencies, and the evidence a check returns."""

from __future__ import annotations

from datetime import tzinfo
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
import json
import re
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ontology.core import ValidationError
from ontology.units import js_trim

from .answer_contract import NUMERIC_OPS, dimension_problem
from .cases import UnitMismatch, evaluate as _evaluate, format_unit, parse_unit

from .claim_checks_grammar import (
    ADD_DAYS_OP, COUNT_OPS, DAYS_OP, EXPRESSION_INPUT, _ID_COLUMN, _ISO_DAY, NUMBER,
    ORIGINAL_SCHEMES, ORIGINAL_TEXT_FIELDS, OTHER_ROLES, PAGE_TEXT, _page_unit, _PAGE_UNITS,
    QUANTITY_ROLES, _ROLE_ALIASES, ROLES, ROUND_TO_CENTS, _YYYYMMDD,
)
from .claim_checks_parse import (
    dig, expression_inputs, number_value, _percent_unit, _plain, _repeated_in_term,
    _share_parts, to_decimal, _withheld,
)
from .claim_checks_cells import (
    _as_stored, _bc_entity_set, _changed, _collection, _day, _fail, _fold, _moment,
    _money_cell, _moved_on, _not_in_quote, _part_of, _record, _same_instant, stamp_zone,
    value_form,
)
from .claim_checks_text import (
    _check_text_figure, _line_breaks, _negated, _normalised, _normalised_text,
    _printed_date_problem, _quote_problem, quoted_spans, _spans, structured_text,
    structured_values, text_field, _text_figures,
)
from .claim_checks_counts import (
    _aggregate_counts, _calculation_currency_problem, _check_add_days, _check_days,
    _check_page_claim, _check_quote, _computed_problem, _engine_currencies, _home_late_count,
    _input_dimensions, _lead_cells, _lead_records, _MIXED_CODES, _money_values, _note_listing,
    _op_calculation, _query_counts, _states_whole_cell,
)

if TYPE_CHECKING:
    from .claim_checks_text import _Figure
    from .claim_checks import _Evidence


def _same_value(claim: Mapping[str, Any], cell: Any, form: str = "text",
                zone: tzinfo | None = None, stamp: Mapping[str, str] | None = None) -> bool:
    if claim["kind"] == "number":
        number = to_decimal(cell)
        return number is not None and number == to_decimal(claim["value"])
    if cell is None or isinstance(cell, bool):
        return False
    # A JSON cell (a Teams sender, a chat's participants) is stated as the projection
    # writes it: canonical JSON (ontology/project.py ``_text``), never Python's repr.
    text = (json.dumps(cell, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if isinstance(cell, (dict, list)) else str(cell).strip())
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
    # a claimed day is the cell's day as written (``_day``); a claimed instant is the
    # instant the projection stored under the source's declared zone (C-09)
    claimed = _moment(claim["value"])
    held = _moment(text if claimed is None or claimed[1] is None else _as_stored(text, stamp))
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
    """A value claim's result (``_checked_value``); a passed one that gives a ``quote``
    then passes only when the quote holds too (``_quote_mismatch``)."""
    result = _checked_value(claim, handles, evidence)
    if "quote" not in claim or result.get("status") != "passed":
        return result
    problem = _quote_mismatch(claim, evidence.row(claim["citation"]), result)
    if problem is not None:
        return _fail({key: result[key] for key in _RESULT_KEYS if key in result},
                     "claim_value_mismatch", problem)
    return {**result, "quote_basis": "verified"}


_RESULT_KEYS = ("value", "citation", "field", "calculation", "role", "unit", "op", "of",
                "currency", "quote")


def _quote_mismatch(claim: Mapping[str, Any], resolved: Mapping[str, Any] | None,
                    result: Mapping[str, Any]) -> str | None:
    """Why a cited claim's ``quote`` does not hold, or None. The quote must hold the value
    as written (a number as a number token of the same value, a status word case-folded,
    a date as written, other text verbatim on token boundaries and not negated inside the
    quote), and the cited text must hold the quote: an original's text or page text as a
    quote of it (``supported_quotes``), any other cell verbatim on token boundaries. A cell
    that is no text or number, or a row not readable now, holds no quote (fail closed).
    The value's own check has passed already; the quote only narrows it."""
    quote, value, field = claim["quote"], claim["value"], claim["field"]
    if resolved is None:
        return "the cited row is not readable now, so the quote cannot be verified"
    if field == PAGE_TEXT:
        text, original = resolved.get("text"), True
    else:
        _exists, text = dig(resolved, field)
        if structured_text(text) is not None:
            text = structured_text(text)
        original = field in ORIGINAL_TEXT_FIELDS and \
            claim["citation"].partition("://")[0] in ORIGINAL_SCHEMES or \
            isinstance(text, str) and text_field(field) and result.get("value_basis") in (
                "text_span", "text_figure")
    want = _normalised(quote)
    if isinstance(text, (int, float, Decimal)) and not isinstance(text, bool):
        # A number cell holds a quote that is that one number as written (``12500`` of
        # 12500.0), nothing around it.
        whole = NUMBER.fullmatch(want)
        if whole is None or to_decimal(text) is None \
                or number_value(whole) != to_decimal(text):
            return f"the cited {field} is a number; the quote is not that number"
        text = want
    if not isinstance(text, str):
        return f"the cited {field} holds no text, so it holds no quote"
    if claim["kind"] == "number":
        number = to_decimal(value)
        percent = "unit" in claim and _page_unit(claim["unit"]) == "percent"
        held = any((figure.value * 100 if figure.percent and percent else figure.value)
                   == number for figure in _text_figures(want))
    elif result.get("value_form") == "enum":
        folded = _normalised(value).casefold().replace("_", " ")
        held = bool(_spans(folded, want.casefold().replace("_", " ")))
    elif result.get("value_form") == "date":
        held = _normalised(value) in want
    else:
        held = any(not _negated(_normalised(value), want, at)
                   for at in _spans(_normalised(value), want)) or bool(
            claim["kind"] == "text" and isinstance(text, str) and _ISO_DAY.fullmatch(value)
            and _printed_date_problem(claim, text, "the cited text") is None)
    if not held:
        return _not_in_quote(value)
    if original:
        return _quote_problem(quote, text, f"the cited {field}")
    if not quoted_spans(quote, text):
        return f"the cited {field} does not hold the quote verbatim on token boundaries"
    return None


def _checked_value(claim: Mapping[str, Any], handles: Sequence[str],
                   evidence: _Evidence) -> dict[str, Any]:
    out = {key: claim[key] for key in _RESULT_KEYS if key in claim}
    role = claim.get("role")
    if claim.get("op") in COUNT_OPS:
        return _check_count(claim, out, handles, evidence)
    if claim.get("op") == "expression":
        return _check_expression(claim, out, handles, evidence)
    if claim.get("op") == DAYS_OP:
        return _check_days(claim, out, handles, evidence)
    if claim.get("op") == ADD_DAYS_OP:
        return _check_add_days(claim, out, handles, evidence)
    if "calculation" in claim or claim.get("op") in NUMERIC_OPS:
        if "calculation" in claim:
            calc, name = evidence.calcs.get(claim["calculation"]), claim["calculation"]
            if calc is None:
                return _fail(out, "claim_uncited", "the calculation is not readable now")
            problem = _calculation_currency_problem(calc, handles, evidence)
            if problem is not None:
                return _fail(out, "claim_currency_mismatch", f"{name}: {problem}")
        else:
            calc, name = _op_calculation(claim, handles, evidence), f"the {claim['op']}"
            if isinstance(calc, str):
                return _fail(out, "claim_unit_mismatch", calc)
            problem = _currency_problem(claim, handles, evidence)
            if problem is not None:
                return _fail(out, "claim_currency_mismatch", problem)
            mixed = dimension_problem(claim["op"], _input_dimensions(calc, handles, evidence))
            if mixed is not None:
                return _fail(out, _MIXED_CODES[mixed.code], mixed.reason)
        cited = {spec["citation"]["handle"] for spec in calc["inputs"].values()
                 if spec.get("kind") == "citation"}
        if not cited <= set(handles):
            return _fail(out, "claim_uncited", f"{name} reads rows the item does not cite")
        for handle in sorted(cited):
            resolved = evidence.row(handle)
            if not _changed(resolved):
                continue
            fields = [spec["field"] for spec in calc["inputs"].values()
                      if spec.get("kind") == "citation"
                      and spec["citation"]["handle"] == handle and spec.get("field")]
            current, problem = _moved_on(resolved, fields, evidence) if fields \
                else (None, "the cited revision is no longer the source's current one")
            if current is None:
                return _fail(out, "claim_changed_since", f"{name} reads {handle[:200]}: "
                             f"{problem}")
        if claim.get("op") in NUMERIC_OPS and claim["op"] != "sum":
            problem = _computed_problem(claim, calc, name, evidence)
            if problem is not None:
                return _fail(out, *problem)
        else:
            result = evidence.recomputed(calc)
            if result is None or "op" not in claim \
                    and result != to_decimal(calc.get("result")):
                return _fail(out, "claim_uncited", f"{name} does not recompute "
                             "from its cited rows as read now")
            if claim["kind"] != "number" or to_decimal(claim["value"]) != result \
                    and not evidence.tool_sum(claim):
                # a sum of more cells than one claim cites: the complete aggregate that
                # added every one of them states it (its cited cells re-read above)
                return _fail(out, "claim_value_mismatch",
                             f"{name} is {_plain(result)}, not {claim['value']}")
        if claim.get("op") == "ratio":
            # A quotient has no quantity type (the type algebra's rule for ratios).
            label, quantity_type, form, problem = None, None, "x0 / x1", None
        else:
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
    if _changed(resolved) and field != PAGE_TEXT:
        current, problem = _moved_on(resolved, [field], evidence)
        if current is None:
            return _fail(out, "claim_changed_since", problem)
        # The cited value is the current one: checked on the current revision.
        out["current_citation"] = resolved["current_citation"]
        resolved = current
    elif _changed(resolved):
        return _fail(out, "claim_changed_since", "the cited revision is no longer the "
                     "source's current one; cite its current version")
    if field == PAGE_TEXT:
        return _check_page_claim(claim, out, resolved)
    if _withheld(resolved, field):
        # A marking withholds the cell from this reader (``raw_markings.mark_view``): it is
        # not checked, so a stated value, right or wrong, is refused alike.
        return _fail(out, "claim_uncited", f"the cited {field} is withheld from this "
                     "reader by a marking: it is neither read nor checked")
    exists, cell = dig(resolved, field)
    if not exists:
        return _fail(out, "claim_value_mismatch", f"the cited row has no {field}")
    if claim["kind"] == "text" and field in ORIGINAL_TEXT_FIELDS and \
            handle.partition("://")[0] in ORIGINAL_SCHEMES:
        return _check_quote(claim, out, cell, field)
    if isinstance(cell, str) and text_field(field) and not _same_value(claim, cell):
        # Prose of any record: a figure or a quote of it, never the whole cell. A cell that
        # is the value itself keeps the field rule below (contracts, roles, units).
        if claim["kind"] == "number":
            return _check_text_figure(claim, out, cell, f"the cited {field}")
        return _check_quote(claim, out, cell, field)
    structured = structured_text(cell)
    if structured is not None and not _states_whole_cell(claim, cell):
        # A structured cell (a task event's payload, a list item's fields) is served as
        # its text: a value it holds whole passes as that value; anything else it says is
        # quoted or figured from that text, as prose is.
        if claim["kind"] == "text" and any(_same_value(claim, value) for _key, value
                                           in structured_values(cell) or ()):
            return {**out, "status": "passed", "role_basis": "untyped",
                    "quantity_type": None, "value_basis": "text_span"}
        if claim["kind"] == "number":
            return _check_text_figure(claim, out, structured, f"the cited {field}")
        return _check_quote(claim, out, structured, field)
    form = value_form(claim, handle, resolved, field)
    zone = None
    if form in ("date", "enum"):
        out["value_form"] = form
    if form == "date":
        try:
            zone = evidence.zone(handle, resolved, field)
        except ValueError as error:
            return _fail(out, "claim_value_mismatch", str(error))
    if not _same_value(claim, cell, form, zone,
                       stamp_zone(handle, resolved, field) if form == "date" else None) \
            and not (
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
    contracted = contract is not None
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
        # A claim naming no role on an id-shaped column no contract types (``number``,
        # ``ordnumber``, ``po_number``) is that identifier too: "Order 1000096354 shipped"
        # is an order number beside a type word, a lead, never an untyped shipped count.
        basis = "untyped"
        id_column = bool(_ID_COLUMN.match(field.rsplit(".", 1)[-1]))
        quantity_type = role if role in OTHER_ROLES and (
            role != "identifier" or id_column) else (
            "identifier" if role is None and id_column and not contracted else None)
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


def _check_count(claim: Mapping[str, Any], out: dict[str, Any], handles: Sequence[str],
                 evidence: _Evidence) -> dict[str, Any]:
    """A count claim (``op: count``, or ``count_distinct``: exact only, below): every input
    is one of the item's citations, readable
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
        if _changed(resolved) and _moved_on(resolved, [], evidence)[0] is None:
            # A count reads that the record exists: its current revision says so too.
            return _fail(out, "claim_changed_since", "the count reads a cited revision that "
                         "is no longer the source's current one, and no current one can be "
                         "read now")
        record = _record(handle, resolved)
        if record in records:
            return _fail(out, "claim_value_mismatch", "the count cites one record twice "
                         f"({records[record][:200]} and {handle[:200]}); it counts records")
        within = next((seen for seen in records
                       if _part_of(record, seen) or _part_of(seen, record)), None)
        if within is not None:
            return _fail(out, "claim_value_mismatch", "the count cites a file together "
                         f"with its own part ({records[within][:200]} and {handle[:200]}); "
                         "a file and its parts are not separate records")
        records[record] = handle
    value, cited = to_decimal(claim["value"]), set(records.values())
    if claim.get("op") == "count_distinct":
        # Distinct values are no records: only the tool's complete count states how many,
        # so the claim is exact or refused, never a lower bound.
        if not any(total == value and cited <= listed
                   for total, listed in evidence.value_counts):
            return _fail(out, "claim_value_mismatch", (
                f"no complete count_distinct a tool returned is {claim['value']} over these "
                "records: a count of distinct values is stated only as a complete "
                "plx.aggregate count_distinct of now counts it"))
        bound = "exact"
    else:
        bound = "exact" if any(
            total == value and _members_named(cited, listed, twins)
            for (total, listed), twins in zip(evidence.counts, evidence.count_twins)) \
            else "at_least"
    if bound == "at_least":
        sources: dict[tuple[str, ...], int] = {}
        for record in records:
            sources[_collection(record)] = sources.get(_collection(record), 0) + 1
        floor = max([*sources.values(), *(len(cited & listed) for listed in evidence.distinct)])
        if value is not None and any(total == value and _members_named(cited, listed, {})
                                     for total, listed in evidence.floors):
            # the tool's own count of every member it holds, a source read in part
            floor = value
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


def _unpinned(handle: str) -> str:
    """A record handle without its version pin (``...@rev``): the record, any revision.
    A handle with a fragment (a field, a page, a container member) stays whole."""
    return handle if "#" in handle else (handle.rpartition("@")[0] or handle)


def _members_named(cited: set[str], listed: frozenset[str],
                   twins: Mapping[str, str]) -> bool:
    """Whether every cited record is a member a complete count entry lists: the member's
    handle (under any version pin), or a confirmed twin of it the entry names
    (``twins``: twin handle -> member handle; a lead twin is never named), and no two
    citations name one member (a member and its twin are one record)."""
    members = {_unpinned(h): h for h in listed}
    pairs = {_unpinned(t): m for t, m in twins.items() if m in listed}
    named: set[str] = set()
    for handle in cited:
        member = handle if handle in listed else (
            members.get(_unpinned(handle)) or pairs.get(_unpinned(handle)))
        if member is None or member in named:
            return False
        named.add(member)
    return True


def _check_expression(claim: Mapping[str, Any], out: dict[str, Any], handles: Sequence[str],
                      evidence: _Evidence) -> dict[str, Any]:
    """An expression claim (``op: expression``): every input a figure its cited field holds
    now, checked as a cited value claim is (``expression_inputs``: on a page or in free
    text the figure rule, its quote pinning the occurrence; a percent only where it is
    printed as one), then the expression evaluated over those figures by the case
    evaluator (``operating.cases.evaluate``: Decimal, units combined and never added
    across; ``round_to_cents`` rounds half-up to 0.01) as the eval scorer recomputes it: a
    percent is its fraction, an input's unit its own (else none). The claimed value must
    be the result, the claim's unit the result's. A figure of text grounds no quantity
    type: the result is untyped, a quantity role ``claim_role_unverified``."""
    values: dict[str, tuple[Decimal, Any]] = {}
    unverified = False
    marks: list[tuple[str | None, str | None]] = []
    places: dict[tuple[Any, ...], str] = {}
    aliases: dict[str, str] = {}
    for number, sub in enumerate(expression_inputs(claim)):
        percent = sub.get("unit") == "percent"
        shown = f"input x{number} ({sub['value']}{'%' if percent else ''})"
        result = _check_value_claim(sub, handles, evidence)
        if result.get("status") != "passed":
            return _fail(out, result.get("code") or "claim_value_mismatch",
                         f"{shown}: {result.get('detail')}"[:400])
        figure = to_decimal(sub["value"])
        if figure is None:
            return _fail(out, "claim_value_mismatch", f"{shown} is no number")
        # Once by place: an input that may be the figure another input reads (the same
        # spot, however quoted or written: "1,000" and "1000") is that input again
        # (``aliases``), so the expression is judged as naming that one input there.
        for place in _input_places(sub, evidence):
            if place in places:
                aliases.setdefault(f"x{number}", places[place])
            else:
                places[place] = f"x{number}"
        if percent:
            if result.get("unit_basis") in (None, "unverified"):
                return _fail(out, "claim_value_mismatch", f"{shown}: the cited text never "
                             "prints it as a percent")
            figure, unit = figure / 100, parse_unit("1")
        else:
            unit = parse_unit(sub.get("unit") or "1")
            unverified = unverified or (
                "unit" in sub and result.get("unit_basis") in (None, "unverified"))
            marks.append(_printed_mark(sub, evidence))
        values[f"x{number}"] = (figure, unit)
    rounded = ROUND_TO_CENTS.match(claim["expression"])
    body = rounded.group(1) if rounded else claim["expression"]
    share = _percent_unit(claim.get("unit"))
    problem = _mixed_marks(marks, body)
    if problem is not None:
        return _fail(out, "claim_unit_mismatch", problem)
    try:
        value, unit, _inexact, _names = _evaluate(body, values)
    except UnitMismatch:
        return _fail(out, "claim_unit_mismatch", "the expression combines inputs in units "
                     "that do not add (state each input's unit as its text prints it)")
    except (ValidationError, ArithmeticError, RecursionError) as error:
        return _fail(out, "claim_value_mismatch", "the expression does not evaluate over "
                     f"its inputs ({str(error)[:80]})")
    if aliases:
        # A12 (v14): one printed pack size quoted at its one place for two kit lines.
        # Named as the input it is, the expression must still be one an input may
        # repeat in (``_repeated_in_term``); x0 / x1 over one figure read twice is not.
        same = EXPRESSION_INPUT.sub(lambda m: aliases.get(m.group(0), m.group(0)), body)
        first = next(iter(aliases))
        if _repeats_badly(same, share):
            return _fail(out, "claim_value_mismatch", (
                f"value_claim_expression_repeated_input: input {first} reads the figure "
                f"input {aliases[first]} reads, so the expression, multiplied out, counts "
                "that figure twice, uses it twice in one term or cancels it; a figure "
                "printed twice is two inputs, each quoted where it is printed"))
        body_checked = same
        values_checked = {name: item for name, item in values.items() if name not in aliases}
    else:
        body_checked, values_checked = body, values
    inert = _inert_repeat(body_checked, values_checked, value)
    if inert is not None:
        return _fail(out, "claim_value_mismatch", (
            f"value_claim_expression_repeated_input: the result does not change with "
            f"input {inert}, which the expression names more than once, so it cancels "
            "itself; name each input once, or only where it applies (x0 * x1 + x0 * x2)"))
    if share:
        # A percent claim is the share its expression computes, x 100 (round_to_cents
        # rounds the percent): a part over a whole in one unit and currency.
        if unit != parse_unit("1"):
            return _fail(out, "claim_unit_mismatch", "a percent is a share of figures in "
                         f"one unit; the expression is in {format_unit(unit)}")
        value = value * 100
    if rounded:
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if to_decimal(claim["value"]) != value:
        return _fail(out, "claim_value_mismatch",
                     f"the expression is {_plain(value)} over its inputs, not {claim['value']}")
    if claim.get("role") in QUANTITY_ROLES:
        return _fail(out, "claim_role_unverified", "an expression over figures of a text has "
                     f"no quantity type, so the claimed {claim['role']} cannot be verified")
    if "currency" in claim:
        problem = _currency_problem(claim, handles, evidence)
        if problem is not None:
            return _fail(out, "claim_currency_mismatch", problem)
    if share:
        out["unit_basis"] = "unverified" if unverified else "calculation"
    elif "unit" in claim:
        if parse_unit(claim["unit"]) != unit:
            return _fail(out, "claim_unit_mismatch",
                         f"the expression is in {format_unit(unit)} by its inputs' units, the "
                         f"claim says {claim['unit']}")
        out["unit_basis"] = "unverified" if unverified else "calculation"
    return {**out, "status": "passed", "role_basis": "calculation", "quantity_type": None}


def _repeats_badly(body: str, share: bool) -> bool:
    """``_repeated_in_term`` of an expression, a percent claim's share (``N / D``) judged
    side by side as ``_expression_shape`` judges it."""
    parts = _share_parts(body) if share else None
    if parts is None:
        return _repeated_in_term(body)
    top, bottom = (EXPRESSION_INPUT.findall(part) for part in parts)
    return any(_repeated_in_term(part) for side, part in zip((top, bottom), parts)
               if len(side) != len(set(side))) or not set(top) - set(bottom)


def _inert_repeat(body: str, values: Mapping[str, tuple[Decimal, Any]],
                  result: Decimal) -> str | None:
    """The first input an expression names more than once whose figure the result does
    not move with (its figure plus one, and twice it, leave the result as it is: ``(x0 +
    x1) - x0 - x1`` builds a zero from x0), or None. A repeated input stands for one
    figure applied to several terms, never one that cancels itself."""
    named = EXPRESSION_INPUT.findall(body)
    for name in dict.fromkeys(n for n in named if named.count(n) > 1):
        figure, unit = values[name]
        for moved in (figure + 1, figure * 2):
            try:
                other = _evaluate(body, {**values, name: (moved, unit)})[0]
            except (ValidationError, UnitMismatch, ArithmeticError, RecursionError):
                return name
            if other != result:
                break
        else:
            return name
    return None


def _input_figures(sub: Mapping[str, Any], evidence: _Evidence
                   ) -> tuple[str, list[_Figure]] | None:
    """(normalised cited text, the supporting figures of an expression input's value in
    it, inside its quote where it gives one); None for a cell that is no text (a
    structured cell is its own place and prints no unit or currency)."""
    resolved = evidence.row(sub["citation"])
    if not isinstance(resolved, Mapping):
        return None
    text = resolved.get("text") if sub["field"] == PAGE_TEXT else dig(resolved, sub["field"])[1]
    if not isinstance(text, str) or not (sub["field"] == PAGE_TEXT or text_field(sub["field"])):
        return None
    text, breaks, value = _normalised_text(text), _line_breaks(text), to_decimal(sub["value"])
    percent = sub.get("unit") == "percent"
    found = [figure for figure in _text_figures(text, breaks) if figure.supports and (
        figure.value * 100 if figure.percent and percent else figure.value) == value]
    if "quote" in sub:
        want = _normalised(sub["quote"])
        spans = [(at, at + len(want)) for at in _spans(want, text)]
        found = [figure for figure in found
                 if any(start <= figure.start and figure.end <= end for start, end in spans)]
    return text, found


def _input_places(sub: Mapping[str, Any], evidence: _Evidence) -> set[tuple[Any, ...]]:
    """Where an expression input's figure may be printed: each supporting occurrence (the
    citation and the figure's offset in the cited text), whatever the quote or the
    number's formatting; a structured cell is its one place."""
    read = _input_figures(sub, evidence)
    if read is None:
        return {(sub["citation"], sub["field"])}
    return {(sub["citation"], sub["field"], figure.start) for figure in read[1]}


def _printed_mark(sub: Mapping[str, Any], evidence: _Evidence
                  ) -> tuple[str | None, str | None]:
    """(currency, page unit) the cited text prints beside an expression input's figure
    (``_input_figures``: ``$``, an ISO code, ``kg``), or None for either it does not print
    or prints more than one way; a structured cell prints neither."""
    read = _input_figures(sub, evidence)
    if read is None:
        return None, None
    text, found = read
    currencies = {figure.currency for figure in found}
    # A unit ending a sentence ("500 g.") is no verified unit, yet it is printed: here,
    # where it can only refuse, it counts.
    units = {_PAGE_UNITS.get(figure.word or "") or _sentence_unit(text, figure.end)
             for figure in found}
    return (next(iter(currencies)) if len(currencies) == 1 else None,
            next(iter(units)) if len(units) == 1 else None)


_SENTENCE_UNIT = re.compile(r"[ \t\u00a0]*([A-Za-z]{1,6})\.(?=\s|$)")


def _sentence_unit(text: str, end: int) -> str | None:
    """The page unit a figure ending at ``end`` prints right before a sentence's period
    ("500 g."), or None."""
    found = _SENTENCE_UNIT.match(text, end)
    return _PAGE_UNITS.get(found.group(1).casefold()) if found else None


def _mixed_marks(marks: Sequence[tuple[str | None, str | None]], body: str) -> str | None:
    """Why an expression's inputs, as their text prints them, do not combine
    (``expression_mixed_units``), or None: two currencies anywhere (two bare ``$`` combine;
    a bare ``$`` beside a named one, ``US$``, ``USD``, ``C$``, does not); and in a sum or
    difference alone, two page units ("10 kg" and "500 g."). Inputs that print none are left
    as they are."""
    currencies = {mark[0] for mark in marks} - {None}
    if len(currencies) > 1:
        return ("expression_mixed_units: the inputs are printed in different currencies ("
                f"{', '.join(sorted(currencies))}); an expression combines one currency")
    if not re.search(r"[*/]", body):
        units = {mark[1] for mark in marks} - {None}
        if len(units) > 1:
            return ("expression_mixed_units: the inputs added or subtracted are printed in "
                    f"different units ({', '.join(sorted(units))})")
    return None


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
    # A ratio's inputs share one currency (or none) that cancels: its value states none.
    cancels = claim.get("op") == "ratio"
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
        claimed = stated or (named[0] if named and not cancels else None)
        return None if claimed is None else (
            "no tool returned the cited values as money and the schema types none of them "
            f"money, so {claimed} is not verified")
    if other:
        return (f"the {claim.get('op') or 'sum'} combines money with {other[0][0][:200]} "
                f"{other[0][1]}, which is not money")
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
        return f"the {claim.get('op') or 'sum'} combines money in " + " and ".join(found)
    if cancels:
        return None
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
                  max_cells, into.setdefault("currency_conflicts", set()))
    if tool == "plx.investigate":
        _engine_currencies(result.get("answer"), into, max_cells)
    if tool == "plx.investigate":
        _lead_cells(result.get("answer"), into.setdefault("leads", {}), max_cells)
    _lead_records(tool, result, into)
    if tool == "plx.find" and isinstance(result.get("home"), Mapping):
        _home_late_count(result["home"], arguments, into)
        return into
    if tool == "plx.around":
        _note_listing(arguments, result, into)
        return into
    if tool == "plx.query":
        _query_counts(arguments, result, into)
        return into
    return _aggregate_counts(tool, arguments, result, into)
