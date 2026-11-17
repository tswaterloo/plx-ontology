"""Count, listing, day and calculation checks: aggregate counts and sums, listings, home
late counts, cited days, computed values and page claims."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import ast
import json
import re
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .answer_contract import PAIR_OPS, ROUNDED_OPS, Dimension, MixedDimensions, ZeroDenominator, calendar_day, computed_claim
from .cases import format_unit, parse_unit

from .claim_checks_grammar import COUNT_OPS, _CURRENCY, DAY_UNITS, _ISO_DAY, QUANTITY_ROLES
from .claim_checks_parse import dig, _percent_unit, _plain, to_decimal, _withheld
from .claim_checks_cells import _changed, _fail, _money_cell, _moved_on
from .claim_checks_text import _check_text_figure, _printed_date_problem, _quote_problem

if TYPE_CHECKING:
    from .claim_checks import _Evidence


def _query_counts(arguments: Any, result: Mapping[str, Any], into: dict[str, Any]) -> None:
    """The counts a ``plx.query`` measured, each as the ``plx.aggregate`` call it made over
    the set its walk reached (``ontology_query._measure``): its groups, completeness and
    partial flag, with every gap of the walk (a hop's gap leaves its count incomplete).
    Walks of several records of one thing (``by_record``) are each that record's own,
    never a count of the thing: none is kept."""
    if not isinstance(arguments, Mapping) or result.get("by_record") is not None:
        return
    common = {key: arguments[key] for key in ("as_known_at", "group_by")
              if key in arguments}
    gaps = list(result.get("gaps") or ())
    for measured in result.get("results") or ():
        if not isinstance(measured, Mapping) or measured.get("error") \
                or not isinstance(measured.get("measure"), Mapping) \
                or "partial" not in measured:
            continue
        call = {"set": result.get("set"), "measure": measured["measure"], **common}
        got = {key: measured[key] for key in ("groups", "completeness", "partial",
                                               "refused", "withheld") if key in measured}
        got["meta"] = measured.get("meta") or result.get("meta")
        if measured.get("inputs_citations_kept") is not None:
            got["partial"] = True   # citations cut to fit: its records are not all listed
        _aggregate_counts("plx.aggregate", call, {**got, "gaps": gaps}, into)


# Gaps that state only the age of what was read, never a member left out of a count.
AGE_ONLY_GAPS = frozenset({("stale", "stale_source"), ("stale", "schema_drift"),
                           # records set apart because only lead links reach them
                           # (``possibly_related``): never members of what was counted
                           ("unknown", "possibly_related"),
                           # a source read only in relations holding none of the
                           # answer's records (``ontology_tools._partly_read_elsewhere``)
                           ("unknown", "partly_read_elsewhere"),
                           # a listed property no member type declares (plx.query rows,
                           # plx.rows: listed without it); a filter or measure on one is
                           # refused, so it never leaves a member out of a count
                           ("unknown", "unknown_property")})
# A result whose records rest on lead-tier links (a set walked from a lead set, twins
# joined only by a lead): what it counts or lists is a likely match, said as a lead.
LEAD_BASIS_GAP = "lead_basis"
MAX_LEAD_RECORDS = 4096


def _age_only(gap: Any) -> bool:
    return isinstance(gap, Mapping) and (gap.get("state"), gap.get("reason")) in AGE_ONLY_GAPS


def _lead_records(tool: str, result: Mapping[str, Any], into: dict[str, Any]) -> None:
    """``lead_records``: per record handle a counting or walking tool returned, True while
    every result naming it rested on lead-tier links (a ``lead_basis`` gap, or listed
    apart as ``possibly_related``: records only lead links reach), False once a result
    that did not names it. A count claim citing a record still True rests partly on lead
    links (``answer_gate._on_a_lead``)."""
    if tool not in ("plx.around", "plx.query", "plx.aggregate"):
        return
    lead = any(isinstance(g, Mapping) and g.get("reason") == LEAD_BASIS_GAP
               for g in result.get("gaps") or ())
    handles = _object_citations(result.get("objects"))
    related = result.get("possibly_related")
    apart = _object_citations(related.get("objects")) if isinstance(related, Mapping) else []
    groups = list(result.get("groups") or ())
    for measured in result.get("results") or ():
        if isinstance(measured, Mapping):
            groups += list(measured.get("groups") or ())
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        handles += [c.get("l1_handle") for c in group.get("inputs_citations") or ()
                    if isinstance(c, Mapping) and isinstance(c.get("l1_handle"), str)]
        handles += [h for h in group.get("members_cited") or () if isinstance(h, str)]
    for row in result.get("rows") or ():
        cite = row.get("citation") if isinstance(row, Mapping) else None
        handle = cite.get("l1_handle") if isinstance(cite, Mapping) else cite
        if isinstance(handle, str):
            handles.append(handle)
    held = into.setdefault("lead_records", {})
    for handle in handles:
        if handle in held or len(held) < MAX_LEAD_RECORDS:
            held[handle] = held.get(handle, True) and lead
    for handle in apart:
        # Reached only through lead links: a lead until a solid result names it.
        if handle in held or len(held) < MAX_LEAD_RECORDS:
            held[handle] = held.get(handle, True)


def _object_citations(objects: Any) -> list[str]:
    """The record handle (display value's citation) of each listed object."""
    handles: list[str] = []
    for obj in objects if isinstance(objects, list) else ():
        origin = ((obj.get("display") or {}).get("origin") or {}) \
            if isinstance(obj, Mapping) else {}
        cite = origin.get("citation") if isinstance(origin, Mapping) else None
        if isinstance(cite, str):
            handles.append(cite)
    return handles


def _aggregate_counts(tool: str, arguments: Any, result: Mapping[str, Any],
                      into: dict[str, Any]) -> dict[str, Any]:
    measure = arguments.get("measure") if isinstance(arguments, Mapping) else None
    op = measure.get("op") if isinstance(measure, Mapping) else None
    if tool == "plx.aggregate" and op == "sum" and isinstance(arguments, Mapping):
        _aggregate_sums(arguments, result, into)
        return into
    if tool != "plx.aggregate" or op not in COUNT_OPS \
            or (op == "count_distinct" and not measure.get("property")):
        return into
    # A count whose only gap is the citation cap (more members than one answer cites) is
    # still the tool's exact count: it lists a sample of its records, all distinct.
    # A count whose filter some sources never record (``filter_not_sourced``) is not: it
    # counted only the records of the sources that record the filtered property.
    # A gap that says only how old the captures are (``AGE_ONLY_GAPS``: a source captured
    # before the host's bound, a projection built under another schema version) leaves
    # no member out: the count is the tool's over every record captured, and the answer
    # states the data's end beside it. Every other gap (a delta not yet merged or
    # aggregated among them) still makes it a lower bound.
    gaps = [gap for gap in result.get("gaps") or () if not _age_only(gap)]
    capped = bool(gaps) and all(gap.get("reason") == CITATIONS_CAPPED for gap in gaps)
    # A page of the groups (``group_page``: paged, or only those ``having`` keeps; or cut
    # at the result size) leaves other groups out, never a member of a group shown: each
    # group returned is whole as the tool computed it over all of its members.
    done = result.get("completeness") or {}
    whole = (result.get("partial") is False and (not gaps or capped)
             and len(gaps) == len([gap for gap in result.get("gaps") or ()
                                   if not _age_only(gap)])
             and not {"refused", "withheld"} & set(result)
             and (done.get("complete") is True
                  or done.get("reason") in GROUP_PAGE_REASONS))
    # The same answer whole but for a source captured in part (``PARTLY_READ``): its count
    # is still every member the tool holds, a lower bound of the source's (``floor``).
    floor = (result.get("partial") is False
             and any(gap.get("reason") == PARTLY_READ for gap in gaps)
             and all(isinstance(gap, Mapping) and gap.get("state") == "unknown"
                     and gap.get("reason") in (CITATIONS_CAPPED, PARTLY_READ)
                     for gap in gaps)
             and not {"refused", "withheld"} & set(result)
             and (done.get("complete") is True
                  or done.get("reason") in GROUP_PAGE_REASONS))
    now = known_now(arguments, result)
    # A count of a set's members (no property, no grouping) cites no value rows: its
    # records are those the complete listing that issued the set showed (``_note_listing``).
    listed = (into.get("listings") or {}).get(arguments.get("set")) \
        if not measure.get("property") and not arguments.get("group_by") else None
    for group in result.get("groups") or ():
        if not isinstance(group, Mapping):
            continue
        records = [cite.get("l1_handle") for cite in group.get("inputs_citations") or ()
                   if isinstance(cite, Mapping)]
        # a count of members cites each member's own record (``members_cited``, per
        # group with group_by); its other citations (a group key's fix) are no members
        cited = group.get("members_cited")
        if not measure.get("property") and isinstance(cited, list) \
                and all(isinstance(h, str) for h in cited):
            records = list(cited)
        elif not measure.get("property") and arguments.get("group_by"):
            records = []
        pairs = group.get("twins_cited") if records else None
        twins = {pair[1]: pair[0] for pair in pairs or ()
                 if isinstance(pair, list) and len(pair) == 2
                 and all(isinstance(h, str) for h in pair) and pair[0] in records
                 and pair[1] not in records}
        if listed is not None and group.get("key") == {}:
            if to_decimal(group.get("value")) == len(listed):
                records = records or list(listed)
            else:
                # A count of the same set that does not match its complete listing: the
                # two disagree, so neither is exact (fail closed, ``_contradict_listing``).
                _contradict_listing(arguments.get("set"), into)
                listed = None
        value = to_decimal(group.get("value"))
        unique = len(records) == len(set(records))
        if op == "count_distinct":
            _note_distinct(group, value, records, now and unique, whole, capped, into)
            continue
        distinct = now and unique and (value == len(records) or (
            (capped or floor and any(gap.get("reason") == CITATIONS_CAPPED for gap in gaps))
            and value is not None and len(records) == MAX_COUNT_CITATIONS
            and value > len(records)))
        entry = {"value": group.get("value"), "records": records,
                 "distinct": distinct, "complete": whole and distinct}
        if distinct and not whole and floor:
            # whole but for a source read in part: the value is at least the source's
            entry["floor"] = True
        if twins and distinct:
            entry["twins"] = twins
        into["counts"].append(entry)
    return into


# Gaps that leave a sum the tool's exact total over every input: the citation cap (the
# first inputs cited, every one added) and a source column whose tax basis no code
# grounds (one column summed as recorded; the total is never called net or gross).
SUM_WHOLE_GAPS = frozenset({"citations_capped", "tax_basis_unknown"})


def _aggregate_sums(arguments: Mapping[str, Any], result: Mapping[str, Any],
                    into: dict[str, Any]) -> None:
    """Each group of a ``plx.aggregate`` ``sum`` as a sum entry (``op: sum``, kept with the
    counts and bounded as they are): its value, currency and unit, the cells it cites
    (``inputs_citations``: every input, or the first ``MAX_COUNT_CITATIONS`` past the cap)
    and whether it is complete: the answer whole (not partial, complete, nothing refused
    or withheld, no gap but ``SUM_WHOLE_GAPS`` and age-only ones), as known now, its cells
    cited once each and, past the cap, exactly the cap's number of them. A sum claim
    citing only cells of a complete entry with its value and currency binds to it
    (``_Evidence.tool_sum``): the tool added every input in Decimal; the claim's own few
    citations cannot (11-10: every Sage order total over more than 8 orders refused)."""
    gaps = [gap for gap in result.get("gaps") or () if not _age_only(gap)]
    done = result.get("completeness") or {}
    whole = (result.get("partial") is False
             and all(isinstance(gap, Mapping) and gap.get("reason") in SUM_WHOLE_GAPS
                     for gap in gaps)
             and not {"refused", "withheld"} & set(result)
             and (done.get("complete") is True or done.get("reason") in GROUP_PAGE_REASONS)
             and known_now(arguments, result))
    capped = any(isinstance(gap, Mapping) and gap.get("reason") == CITATIONS_CAPPED
                 for gap in gaps)
    for group in result.get("groups") or ():
        if not isinstance(group, Mapping) or not isinstance(group.get("value"), str):
            continue
        cells = [(c.get("l1_handle"), c.get("field_path"))
                 for c in group.get("inputs_citations") or () if isinstance(c, Mapping)]
        inputs = group.get("inputs")
        listed = bool(cells) and len(set(cells)) == len(cells) and all(
            isinstance(h, str) and isinstance(f, str) for h, f in cells) and \
            type(inputs) is int and (inputs == len(cells) or (
                capped and len(cells) == MAX_COUNT_CITATIONS and inputs > len(cells)))
        into["counts"].append({
            "op": "sum", "value": group["value"], "currency": group.get("currency"),
            "unit": group.get("unit"), "cells": cells if listed else [],
            # its records, so a session drops it when an item's permissions change
            "records": sorted({h for h, _ in cells if isinstance(h, str)}),
            "complete": whole and listed})


def _contradict_listing(handle: Any, into: dict[str, Any]) -> None:
    """A set whose complete listing a count of its members contradicts: the listing's
    own count entry (``_note_listing``) becomes a lower bound and the set's listing is
    dropped, so no later count of it binds to those records either."""
    (into.get("listings") or {}).pop(handle, None)
    for entry in into["counts"]:
        if entry.get("listing") == handle:
            entry["complete"] = False
            entry["distinct"] = False


def _note_distinct(group: Mapping[str, Any], value: Decimal | None, records: list[Any],
                   unique: bool, whole: bool, capped: bool, into: dict[str, Any]) -> None:
    """A ``count_distinct`` group as two count entries: its value, the distinct values
    among its records (``op: count_distinct``), and its ``inputs``, the records holding a
    value (a plain count: the tool states a distinct count only when no member lacks a
    value, is withheld, conflicts or has a twin, so every member is one input). Each is
    complete under the count rule: the answer whole, as known now, the records listed
    once each, every one of them (``inputs`` of them) or, past the citation cap, the
    first ``MAX_COUNT_CITATIONS`` of more; never more values than records."""
    inputs = group.get("inputs")
    if value is None or type(inputs) is not int or not 0 < value <= inputs:
        return
    listed = unique and (inputs == len(records) or (
        capped and len(records) == MAX_COUNT_CITATIONS and inputs > len(records)))
    into["counts"].append({"op": "count_distinct", "value": group.get("value"),
                           "records": records, "distinct": False,
                           "complete": whole and listed})
    into["counts"].append({"value": str(inputs), "records": records, "distinct": listed,
                           "complete": whole and listed})


MAX_LISTINGS = 32   # complete set listings one session keeps for count claims (newest)


def _note_listing(arguments: Any, result: Mapping[str, Any], into: dict[str, Any]) -> None:
    """A ``plx.around`` answer that lists every member of the set it issues, as known now
    (``listing_count``): a complete count entry of those records, so a count claim citing
    them binds exactly without a second call, and the set handle with its records, so a
    ``plx.aggregate`` count of that set binds to them too. Anything else keeps nothing
    (the count stays a lower bound)."""
    records = listing_count(arguments, result)
    if records is None:
        return
    into["counts"].append({"value": str(len(records)), "records": list(records),
                           "distinct": True, "complete": True, "listing": result["set"]})
    listings = into.setdefault("listings", {})
    listings.pop(result["set"], None)
    listings[result["set"]] = records
    while len(listings) > MAX_LISTINGS:
        listings.pop(next(iter(listings)))


def known_now(arguments: Any, result: Mapping[str, Any]) -> bool:
    """Whether a tool result counts what is known now: the call names no ``as_known_at``,
    or the moment it names (as the result states it, ``meta.as_known_at``) is at or after
    every capture the result read (``meta.freshness``, each source's ``captured_at``, all
    listed): nothing was captured since, so what was known then is what is known now.
    Any doubt (no moment, no capture time, a capture list cut short): not now. A
    ``plx.aggregate`` count that read the same groups now as at its moment says so
    (``same_as_now``): its count is the count known now too.
    """
    if not isinstance(arguments, Mapping) or "as_known_at" not in arguments:
        return True
    if isinstance(result, Mapping) and result.get("same_as_now") is True:
        return True
    meta = result.get("meta") if isinstance(result, Mapping) else None
    coverage = result.get("freshness_coverage") if isinstance(result, Mapping) else None
    if not isinstance(meta, Mapping) or (isinstance(coverage, Mapping)
                                         and coverage.get("truncated") is not False):
        return False
    at = _instant(meta.get("as_known_at"))
    freshness = meta.get("freshness")
    if at is None or not isinstance(freshness, list) or not freshness:
        return False
    for entry in freshness:
        captured = _instant(entry.get("captured_at")) if isinstance(entry, Mapping) else None
        if captured is None or captured > at:
            return False
    return True


def _instant(text: Any) -> datetime | None:
    """An aware moment of an ISO 8601 timestamp with its offset (``Z`` read as UTC); None
    for anything else (a bare date, a naive time)."""
    if not isinstance(text, str) or "T" not in text:
        return None
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def listing_count(arguments: Any, result: Any) -> tuple[str, ...] | None:
    """The records a ``plx.around`` answer lists when it lists every member of the set it
    issues, as known now (``known_now``): each member's record (its display value's
    citation), so the listing is itself an exact count of them. Only when the answer is
    complete on its first page (no cursor, not truncated, every member listed once,
    ``total`` equal to the listed count) with no gap but the data's age and no withheld
    member counted; anything else is None (a count of it stays a lower bound)."""
    if not isinstance(result, Mapping) or "error" in result:
        return None
    handle = result.get("set")
    done = result.get("completeness")
    objects = result.get("objects")
    if not isinstance(handle, str) or not isinstance(done, Mapping) \
            or not isinstance(objects, list) or not objects \
            or (isinstance(arguments, Mapping) and arguments.get("cursor")) \
            or not known_now(arguments, result):
        return None
    withheld = result.get("withheld")
    if done.get("complete") is not True or done.get("truncated") is not False \
            or done.get("next_cursor") is not None or done.get("total") != len(objects) \
            or any(not _age_only(gap) for gap in result.get("gaps") or ()) \
            or (isinstance(withheld, Mapping) and withheld.get("objects")):
        return None
    records: list[str] = []
    for obj in objects:
        origin = ((obj.get("display") or {}).get("origin") or {}) \
            if isinstance(obj, Mapping) else {}
        cite = origin.get("citation") if isinstance(origin, Mapping) else None
        if not isinstance(cite, str) or not cite:
            return None
        records.append(cite)
    return tuple(records) if len(set(records)) == len(records) else None


# The aggregate's citation cap (``ontology_tools_aggregate.MAX_CITATIONS``) and the gap it
# states past it: a count group listing that many records under that gap alone counts more.
MAX_COUNT_CITATIONS = 50
CITATIONS_CAPPED = "citations_capped"
# A gap saying only that a source's last capture read it in part: every record counted is
# one the tool holds, so its count is a floor of the source's (records may be missing,
# none counted is invented); the tool's own value is then a lower bound (``floor``).
PARTLY_READ = "source_partly_read"
# Why an aggregate returned only some of its groups, each of them whole
# (``ontology_tools_aggregate``: a page, the groups ``having`` keeps, a size cut).
GROUP_PAGE_REASONS = frozenset({"group_page", "result_size"})


def _home_late_count(home: Mapping[str, Any], arguments: Any, into: dict[str, Any]) -> None:
    """plx.find home mode's late purchase orders (``home.late_orders``: the distinct orders
    of the reader's late list, Home's) as a count entry: complete only when the home answer
    says the count is complete and lists one record per late order on its page, none twice
    (the first citation of each late order's gap, a sample when the list is longer than
    the page). A call naming ``as_known_at`` counts nothing now."""
    late = home.get("late_orders")
    if not isinstance(late, Mapping) or type(late.get("count")) is not int \
            or (isinstance(arguments, Mapping) and "as_known_at" in arguments):
        return
    seen: dict[str, str] = {}
    for gap in home.get("gaps") or ():
        if not isinstance(gap, Mapping) or type(gap.get("days_late")) is not int:
            continue
        cites = gap.get("citations")
        oid = gap.get("object_id")
        if isinstance(oid, str) and oid not in seen and isinstance(cites, list) and cites \
                and isinstance(cites[0], str):
            seen[oid] = cites[0]
    records = list(seen.values())
    unique = len(records) == len(set(records)) and len(records) <= late["count"]
    into["counts"].append({"value": str(late["count"]), "records": records,
                           "distinct": unique,
                           "complete": unique and late.get("complete") is True})


def _engine_currencies(answer: Any, into: dict[str, Any], limit: int | None) -> None:
    """A plan engine's money total over cited cells (``plx.investigate`` ``calculation``
    of a ``computed`` entry with a ``currency``): each cell is money in the currency the
    engine read for it from the cited document (a blank code read as its book's local
    currency), so the cell takes it where no tool stated one. Two engines stating two
    currencies for one cell state none (``currency_conflicts``), and a cell a tool
    stated another currency for keeps the conflict rule."""
    out = into["currencies"]
    conflicts = into.setdefault("currency_conflicts", set())
    for entry in answer if isinstance(answer, list) else ():
        calculation = entry.get("calculation") if isinstance(entry, Mapping) else None
        currency = entry.get("currency") if isinstance(entry, Mapping) else None
        if entry.get("kind") != "computed" or not isinstance(calculation, Mapping) \
                or not isinstance(currency, str) or not _CURRENCY.match(currency):
            continue
        for cite in calculation.get("inputs") or ():
            if not isinstance(cite, Mapping) or not isinstance(cite.get("l1_handle"), str) \
                    or not isinstance(cite.get("field_path"), str):
                continue
            key = (cite["l1_handle"], cite["field_path"])
            if key in conflicts or (key not in out and limit is not None
                                    and len(out) >= limit):
                continue
            held = out.get(key)
            if held is None or held == currency:
                out[key] = currency
            else:
                out[key] = None
                conflicts.add(key)


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
                  limit: int | None = None,
                  conflicts: set[tuple[str, str]] | None = None) -> None:
    def key_of(cite: Any) -> tuple[str, str] | None:
        if isinstance(cite, Mapping) and isinstance(cite.get("l1_handle"), str) \
                and isinstance(cite.get("field_path"), str):
            return cite["l1_handle"], cite["field_path"]
        return None

    def note(cite: Any, currency: Any, source: Any = None) -> None:
        key = key_of(cite)
        if key is not None and (key in out or limit is None or len(out) < limit):
            code = currency if isinstance(currency, str) and _CURRENCY.match(currency) else None
            if conflicts is not None and code is not None and out.get(key) not in (None, code):
                conflicts.add(key)   # two results state two currencies: never one of them
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
            _money_values(value, out, cites, limit, conflicts)
    elif isinstance(node, list):
        for value in node:
            _money_values(value, out, cites, limit, conflicts)


def _computed_problem(claim: Mapping[str, Any], calc: Mapping[str, Any], name: str,
                      evidence: _Evidence) -> tuple[str, str] | None:
    """Why an ``avg``, ``min``, ``max``, ``difference`` or ``ratio`` claim's value does not
    hold over its cited cells as re-read now ((code, detail)), or None: the value
    recomputed from those cells by ``answer_contract.computed_claim``, the eval scorer's
    rule (exact, or for an average or a ratio rounded half-up to the decimals the claim
    writes, never coarser than its floor). A ratio over a zero denominator is refused."""
    numbers = []
    for spec in calc["inputs"].values():
        resolved = evidence.row(spec["citation"]["handle"])
        number = to_decimal(dig(resolved, spec["field"])[1]) if resolved is not None else None
        if number is None:
            return "claim_uncited", f"{name} does not recompute from its cited rows as read now"
        numbers.append(number)
    try:
        holds, result = computed_claim(
            claim["op"], to_decimal(claim["value"]), numbers,
            percent=_percent_unit(claim.get("unit")),
            dimensions=_input_dimensions(calc, list(_cited_handles(calc)), evidence))
    except MixedDimensions as mixed:
        return _MIXED_CODES[mixed.code], mixed.reason
    except ZeroDenominator:
        return "claim_value_mismatch", (f"{name}'s denominator (its second input) is 0 as "
                                        "read now: no ratio holds")
    except (ValueError, ArithmeticError):
        return "claim_uncited", f"{name} does not recompute from its cited rows as read now"
    if holds:
        return None
    rounding = (" (written rounded half-up to the decimals it gives, at least "
                + ("the cents or units of its inputs" if claim["op"] == "avg"
                   else "0.01, or a whole percent") + ")") \
        if claim["op"] in ROUNDED_OPS else ""
    shown = format(result, "f") if claim["op"] in ROUNDED_OPS else _plain(result)
    return "claim_value_mismatch", f"{name} is {shown}, not {claim['value']}{rounding}"


def _cited_day(handle: str, field: str, handles: Sequence[str], evidence: "_Evidence"
               ) -> tuple[date | None, str]:
    """(the calendar day a cited date cell holds as re-read now, "") or (None, why not):
    an ISO day, or a timestamp's own day (its first ten characters); a revision that moved
    on stands only where the cell holds the same value now (``_moved_on``)."""
    resolved = evidence.row(handle) if handle in handles else None
    if resolved is None:
        return None, f"{handle[:200]} is not readable now as the case recorded it"
    if _changed(resolved):
        current, problem = _moved_on(resolved, [field], evidence)
        if current is None:
            return None, problem
        resolved = current
    if _withheld(resolved, field):
        return None, f"the cited {field} is withheld from this reader by a marking"
    exists, cell = dig(resolved, field)
    day = calendar_day(cell) if exists else None
    if day is None:
        return None, f"the cited {field} holds no date"
    return day, ""


def _check_days(claim: Mapping[str, Any], out: dict[str, Any], handles: Sequence[str],
                evidence: "_Evidence") -> dict[str, Any]:
    """A ``days`` claim: the whole days from its second cited date to its first (x0 - x1,
    negative when the first is earlier), or from its ``day`` to its one cited date (x0 -
    day; ``day`` one of ``evidence.reference_days``, else refused), each cited date re-read
    now (``_cited_day``), equal to the
    claimed whole number; in days if it states a unit; no quantity role (a span of time
    is no quantity of goods) and no currency (refused at parse)."""
    days = []
    for given in claim["of"]:
        day, problem = _cited_day(given["citation"], given["field"], handles, evidence)
        if day is None:
            return _fail(out, "claim_value_mismatch", f"the days: {problem}")
        days.append(day)
    if "day" in claim:
        if claim["day"] not in evidence.reference_days:
            return _fail(out, "claim_value_mismatch",
                         f"the days count from {claim['day']}, which is neither a day the "
                         "question writes in full nor today: cite a record's date instead")
        days.append(calendar_day(claim["day"]))
    if "role" in claim:
        return _fail(out, "claim_role_mismatch", "a number of days between two dates has "
                     f"no quantity role; the claim says {claim['role']}")
    if "unit" in claim and claim["unit"].strip().casefold() not in DAY_UNITS:
        return _fail(out, "claim_unit_mismatch",
                     f"the days are in days, the claim says {claim['unit']}")
    span = (days[0] - days[1]).days
    if to_decimal(claim["value"]) != Decimal(span):
        return _fail(out, "claim_value_mismatch", f"from {days[1].isoformat()} to "
                     f"{days[0].isoformat()} is {span} days, not {claim['value']}")
    return {**out, "status": "passed", "role_basis": "calculation", "quantity_type": None}


# A column whose name ends in its unit, days (``lead_time_days``, ``shelf_life_days``,
# ``payment_terms_days``, ``days``): an ``add_days`` count of days where no contract
# states one; never a rate or a calendar part (``usage_per_day``, ``day_of_week``,
# ``due_day``), nor working days, which a calendar sum would misread (``business_days``).
_DAYS_COLUMN = re.compile(r"^(?!.*(?:business|work|working)_days$)(?:.*_)?days$", re.I)


def _cited_day_count(handle: str, field: str, handles: Sequence[str], evidence: "_Evidence"
                     ) -> tuple[int | None, str]:
    """(the whole number of days a cited cell holds as re-read now, "") or (None, why
    not): a whole number (no fraction), in days by the column's contract or recorded unit
    or, where none is known, by the column's own name (``_DAYS_COLUMN``); a lead time
    recorded in weeks, or a count of no stated unit, is refused, never read as days."""
    resolved = evidence.row(handle) if handle in handles else None
    if resolved is None:
        return None, f"{handle[:200]} is not readable now as the case recorded it"
    if _changed(resolved):
        current, problem = _moved_on(resolved, [field], evidence)
        if current is None:
            return None, problem
        resolved = current
    if _withheld(resolved, field):
        return None, f"the cited {field} is withheld from this reader by a marking"
    exists, cell = dig(resolved, field)
    number = to_decimal(cell) if exists and not isinstance(cell, bool) else None
    if number is None or number != number.to_integral_value() or abs(number) > 36600:
        return None, f"the cited {field} holds no whole number of days"
    contract = evidence.contract(handle, resolved, field)
    known = contract.get("unit") if contract and contract.get("unit") is not None else next(
        iter(evidence.units.get((handle, field), ())), None) \
        if len(evidence.units.get((handle, field), ())) == 1 else None
    if known is not None:
        if str(known).strip().casefold() not in DAY_UNITS:
            return None, f"the cited {field} is recorded in {known}, not in days"
    elif not _DAYS_COLUMN.search(field.rsplit(".", 1)[-1]):
        return None, (f"the cited {field} states no unit: a count of days is a column "
                      "recorded in days")
    return int(number), ""


def _check_add_days(claim: Mapping[str, Any], out: dict[str, Any], handles: Sequence[str],
                    evidence: "_Evidence") -> dict[str, Any]:
    """An ``add_days`` claim: the calendar day its cited whole number of days after its
    first cited date (x0 + x1), or after its ``day`` (day + x0; ``day`` one of
    ``evidence.reference_days``, else refused), each input re-read now, equal to the
    claimed ISO day; no quantity role, unit or currency (a day is no quantity)."""
    given = claim["of"]
    if "day" in claim:
        if claim["day"] not in evidence.reference_days:
            return _fail(out, "claim_value_mismatch",
                         f"the day counts from {claim['day']}, which is neither a day the "
                         "question writes in full nor today: cite a record's date instead")
        start, counted = calendar_day(claim["day"]), given[0]
    else:
        start, problem = _cited_day(given[0]["citation"], given[0]["field"], handles,
                                    evidence)
        if start is None:
            return _fail(out, "claim_value_mismatch", f"the day: {problem}")
        counted = given[1]
    number, problem = _cited_day_count(counted["citation"], counted["field"], handles,
                                       evidence)
    if number is None:
        return _fail(out, "claim_value_mismatch", f"the day: {problem}")
    if "role" in claim:
        return _fail(out, "claim_role_mismatch", "a calendar day has no quantity role; the "
                     f"claim says {claim['role']}")
    if "unit" in claim:
        return _fail(out, "claim_unit_mismatch",
                     f"a calendar day has no unit; the claim says {claim['unit']}")
    try:
        result = start + timedelta(days=number)
    except OverflowError:
        return _fail(out, "claim_value_mismatch", "the day is out of the calendar's range")
    if calendar_day(claim["value"]) != result:
        return _fail(out, "claim_value_mismatch", f"{number} days after "
                     f"{start.isoformat()} is {result.isoformat()}, not {claim['value']}")
    return {**out, "status": "passed", "role_basis": "calculation", "quantity_type": None}


# The claim gate's code for each way an op's inputs fail to share one dimension.
_MIXED_CODES = {"mixed_currency": "claim_currency_mismatch",
                "missing_currency": "claim_currency_mismatch",
                "money_with_non_money": "claim_currency_mismatch",
                "mixed_unit": "claim_unit_mismatch"}


def _cited_handles(calc: Mapping[str, Any]) -> set[str]:
    return {spec["citation"]["handle"] for spec in calc["inputs"].values()
            if spec.get("kind") == "citation"}


def _cell_money(handle: str, field: str, handles: Sequence[str], evidence: _Evidence
                ) -> tuple[bool, str | None]:
    """(money, its currency or None) of one cited cell: money when a tool returned it as
    money or the schema types its column money (``_currency_problem``'s rule); its
    currency only as a tool returned it."""
    key = (handle, field)
    if key in evidence.currencies:
        return True, evidence.currencies.get(key)
    resolved = evidence.row(handle) if handle in handles else None
    return resolved is not None and _money_cell(handle, resolved, field), None


def _input_dimensions(calc: Mapping[str, Any], handles: Sequence[str],
                      evidence: _Evidence) -> list[Dimension]:
    """The ``Dimension`` of each input of an op calculation (``_op_calculation``), in
    order: money and currency as this reader was returned them (``_cell_money``), the
    unit the calculation reads it in (folded by ``parse_unit``)."""
    out = []
    for spec in calc["inputs"].values():
        money, currency = _cell_money(spec["citation"]["handle"], spec["field"], handles,
                                      evidence)
        try:
            unit = format_unit(parse_unit(spec.get("unit")))
        except Exception:  # noqa: BLE001 - an unparseable unit only equals itself
            unit = ("unparsed", str(spec.get("unit")))
        out.append(Dimension(money=money, currency=currency, unit=unit))
    return out


_COMPOSITE = object()   # a product or quotient of money: no one currency, never checked


def _calculation_currency_problem(calc: Mapping[str, Any], handles: Sequence[str],
                                  evidence: _Evidence) -> str | None:
    """Why a recorded calculation (``CaseRuntime.compute``) adds or subtracts money in two
    currencies, or money whose currency no tool returned beside other money, or None.
    Units are the evaluator's (``cases.evaluate``); a currency is no unit there, so it is
    checked here over the expression tree: money times or over a plain number keeps its
    currency, money over money of one currency is a plain number, any other product or
    quotient of money is no amount in one currency and is not checked further."""
    try:
        tree = ast.parse(str(calc.get("expression") or "").strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None   # not recomputable: refused as such by the recompute below
    inputs = calc["inputs"]

    def walk(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Name):
            spec = inputs.get(node.id)
            if not isinstance(spec, Mapping) or spec.get("kind") != "citation":
                return None
            money, currency = _cell_money(spec["citation"]["handle"], spec["field"],
                                          handles, evidence)
            return (currency or "?") if money else None
        if isinstance(node, ast.UnaryOp):
            return walk(node.operand)
        if not isinstance(node, ast.BinOp):
            return None
        left, right = walk(node.left), walk(node.right)
        if _COMPOSITE in (left, right):
            return _COMPOSITE
        if isinstance(node.op, (ast.Add, ast.Sub)):
            if left is None or right is None:
                return right if left is None else left
            if "?" in (left, right):
                raise MixedDimensions("missing_currency",
                                      "it adds money whose currency is not known")
            if left != right:
                raise MixedDimensions("mixed_currency", "it adds money in "
                                      + " and ".join(sorted((left, right))))
            return left
        if isinstance(node.op, ast.Mult):
            return _COMPOSITE if None not in (left, right) else (
                right if left is None else left)
        if right is None:
            return left
        return None if left == right and left != "?" else _COMPOSITE

    try:
        walk(tree)
    except MixedDimensions as mixed:
        return mixed.reason
    except RecursionError:
        return None
    return None


def _op_calculation(claim: Mapping[str, Any], handles: Sequence[str],
                    evidence: _Evidence) -> dict[str, Any] | str:
    """A ``NUMERIC_OPS`` claim (``op: sum``, ``avg``, ``min``, ``max``, ``difference``,
    ``ratio``) as a calculation over its cited cells, checked as a recorded one is
    (``_check_value_claim``): the result recomputed from the re-read rows, its quantity
    type by the type algebra (an average, a min or a max typed as the sum of its inputs
    is: one type each; a difference as ``x0 - x1``; a ratio never). Every input in one unit
    (its own, else the claim's, else ``1``; a ratio's never the claim's, which is its
    result's: ``1`` or a percent) that the cell's contract or recorded unit does not
    contradict; else why not (a ``claim_unit_mismatch`` detail)."""
    op = claim["op"]
    inputs: dict[str, dict[str, Any]] = {}
    units = set()
    for number, given in enumerate(claim["of"]):
        handle, field = given["citation"], given["field"]
        unit = given.get("unit") or (claim.get("unit") if op != "ratio" else None) or "1"
        resolved = evidence.row(handle) if handle in handles else None
        if resolved is not None:
            contract = evidence.contract(handle, resolved, field)
            known = contract.get("unit") if contract and contract.get("unit") is not None \
                else next(iter(evidence.units.get((handle, field), ())), None) \
                if len(evidence.units.get((handle, field), ())) == 1 else None
            if known is not None and parse_unit(known) != parse_unit(unit):
                return f"{handle[:200]} {field} is recorded in {known}, the {op} says {unit}"
        units.add(parse_unit(unit))
        inputs[f"x{number}"] = {"kind": "citation", "citation": {"handle": handle},
                                "field": field, "unit": unit}
    if len(units) > 1:
        return f"the {op} combines inputs in different units"
    names = list(inputs)
    unit = next(iter(inputs.values()))["unit"]
    if op in PAIR_OPS:
        expression = f"{names[0]} {'-' if op == 'difference' else '/'} {names[1]}"
        if op == "ratio":
            # Same-unit inputs: a pure number, or that number x 100 stated as a percent.
            unit = claim["unit"] if _percent_unit(claim.get("unit")) else "1"
    else:
        # Grouped by ten: MAX_OP_INPUTS terms stay within the evaluator's expression length
        # and depth (operating/cases.py _MAX_EXPR, _MAX_DEPTH), as the eval scorer groups.
        expression = "+".join("(" + "+".join(names[start:start + 10]) + ")"
                              for start in range(0, len(names), 10))
    return {"id": op, "expression": expression, "inputs": inputs, "unit": unit}


def _check_page_claim(claim: Mapping[str, Any], out: dict[str, Any],
                      resolved: Mapping[str, Any]) -> dict[str, Any]:
    """A figure claimed on an original page citation (``field: "text"``): a figure of the
    page's machine-read text as re-read now, by the one prose-figure rule a record's
    free text follows too (``_check_text_figure``: ``07`` is 7, ``1,509`` is 1509,
    ``25%`` is 0.25, or 25 for a claim in percent; never a negated, bounded, range,
    label, date or code number; one meaning or a quote); a text value as a quote of it
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
    where = f"page {resolved['page']}'s machine-read text"
    if claim["kind"] == "number":
        result = _check_text_figure(claim, out, text, where, "page_text")
        return {**result, "text_source": resolved.get("text_source")} \
            if result["status"] == "passed" else result
    problem = _quote_problem(claim["value"], text, where)
    if problem and _ISO_DAY.fullmatch(claim["value"]):
        # RRO-09: a day the page prints "31 Aug" is claimed as written ISO, 2026-08-31.
        problem = _printed_date_problem(claim, text, where)
    if problem:
        return _fail(out, "claim_value_mismatch", problem)
    if "unit" in claim:
        out["unit_basis"] = "unverified"
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
    if problem and isinstance(cell, str) and _ISO_DAY.fullmatch(claim["value"]):
        problem = _printed_date_problem(claim, cell, f"the cited {field}")
    if problem:
        return _fail(out, "claim_value_mismatch", problem)
    if "unit" in claim:
        out["unit_basis"] = "unverified"
    if claim.get("role") in QUANTITY_ROLES:
        return _fail(out, "claim_role_unverified", "a quote from a record's text has no "
                     f"role contract, so the claimed {claim['role']} cannot be verified")
    return {**out, "status": "passed", "role_basis": "untyped", "quantity_type": None,
            "value_basis": "text_span"}


def _states_whole_cell(claim: Mapping[str, Any], cell: Any) -> bool:
    """Whether a text claim states a structured cell whole, as stored (the same JSON
    object or array): it is checked as any cell's value is, not as a quote of the
    cell's text, which never holds the JSON itself."""
    value = claim.get("value")
    if claim.get("kind") != "text" or not isinstance(value, str):
        return False
    try:
        stated = json.loads(value)
        held = json.loads(cell) if isinstance(cell, str) else cell
    except ValueError:
        return False
    return isinstance(stated, (Mapping, list)) and stated == held
