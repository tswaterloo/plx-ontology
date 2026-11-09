"""Conflicts, duplicates and stale values in answers (RISHI-DIRECTION section 5), one
layer the agent's tools, the MCP host and the app all read.

Same fact, different values (``resolve``): every reading the reader may open is shown
with its source; one is marked trusted only by a stated rule, tried in this order, and
every value says why:

0. **measured agreement**: for a type x field the measured precedence table holds
   (``operating.source_precedence``), the source whose values agreed most often with the
   downstream evidence (shipments, invoices, receipts, completion records); a tie goes
   to the most recently updated copy. Measured on history, re-measured on a schedule;
   never a fixed order.
1. **system of record**: the side a field's existing rule says governs. ``governs``
   (``operating.po_status``: BC lines govern receipt state), then the declared
   precedence of the property (``authority_rank``, the projection's per-property
   precedence: comparable only inside one precedence list, so a document's rank is
   never weighed against a record's), then the source tier (``operating.chunking.
   AUTHORITY``, the search tables' tier: a system of record over an approved document
   over an email over a chat; a machine-read document against the record it attests is
   a lead, never the authority).
2. **newest**: the reading first seen latest, when every side has a known time and the
   newest is strictly newer than every differing side; its age is stated.
3. **most specific document**: the document naming the fewest records (one that is
   about this record over a list naming many).
4. otherwise none is trusted: both are shown, and the reader is told no rule decides.

Never a silent pick: a value the rule did not choose says which one did and why. A side
the reader cannot open is dropped before anything is decided, and a disagreement with
fewer than two visible distinct values is not stated (never half a conflict, never a
hint that a hidden value exists). Values are compared as text, numbers as ``Decimal``
(``10.00`` equals ``10``), never as floats.

A document that contradicts itself (``self_contradictions``): the same labelled field
stated twice with two values in one document's text is flagged with both places; none
is trusted.

Duplicates (``collapse_copies``): items sharing a duplicate cluster
(``search.chunks.dup_group``, the content hash and MinHash) are shown once, the
representative the reader may open (highest tier, then earliest), listing where else
the copies live, only those this reader may open; a copy the reader may not open is
never named, counted or located.

Near matches (``did_you_mean``): the query understanding decides which mentions
are ambiguous (``operating.query_understanding``); this only shapes its choices, never
merging without an exact id.

Read-only: nothing here writes, and no value here enters an answer except through the
answer gate (a conflict card cites the values the tools returned).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Sequence

STALE_DAYS = 30            # a value older than this has its age stated
# Readings first seen within a day of each other are not ordered by date: one build
# records every value of a source within minutes, which is no "newer" fact.
NEWER_BY = dt.timedelta(days=1)
MAX_VALUES = 6
MAX_ALSO_IN = 5

# The source tier of a citation handle: the search tables' authority scale
# (operating.chunking.AUTHORITY: 4 system of record, 3 approved document, 2 email,
# 1 chat), by the handle's scheme.
_TIERS = (("bc://", 4, "BC"), ("api://", 4, "Portal"), ("sage", 4, "Sage"),
          ("fm://", 4, "FileMaker"), ("filemaker", 4, "FileMaker"),
          ("sharepoint", 3, "File"), ("original://", 3, "File"), ("repo://", 3, "File"),
          ("graph-mail://", 2, "Email"), ("mail://", 2, "Email"), ("mc://", 2, "MC"),
          ("teams", 1, "Teams"))
_TIER_WORDS = {4: "the system of record", 3: "an approved document", 2: "an email",
               1: "a chat message"}
SYSTEM_NAMES = {"BC": "BC", "Portal": "the Portal", "Sage": "Sage",
                "FileMaker": "FileMaker", "File": "the document", "Email": "the email",
                "MC": "Mission Control", "Teams": "the Teams message"}
# po_status names the governing system by its lineage word.
_GOVERNS = {"bc": "BC", "portal": "Portal", "sage": "Sage", "fm": "FileMaker",
            "filemaker": "FileMaker"}


def system_of(handle: str) -> str:
    for prefix, _, system in _TIERS:
        if handle.startswith(prefix):
            return system
    return "File"


def source_label(handle: str) -> str:
    """Words for where a value was read, specific enough to tell two Portal tables
    apart ('the Portal generated purchase order record'); no value, id or name from the
    record."""
    from urllib.parse import unquote
    system = system_of(handle)
    part = ""
    if handle.startswith("api://"):
        part = unquote(handle[len("api://"):].split("/", 1)[0]).split(".")[-1]
        part = re.sub(r"^app_", "", part)
    part = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", part).replace("_", " ").strip().lower()
    if not re.fullmatch(r"[a-z ]{2,40}", part or ""):
        return SYSTEM_NAMES.get(system, system)
    return f"the Portal {part} record" if system == "Portal" else f"{system} {part}"


def tier_of(handle: str) -> int | None:
    for prefix, tier, _ in _TIERS:
        if handle.startswith(prefix):
            return tier
    return None


# ------------------------------------------------------------------------- readings

@dataclass(frozen=True)
class Reading:
    """One side of a disagreement: a value and where it was read."""
    value: Any
    handle: str
    field_path: str | None = None
    authority_rank: int | None = None
    rank_scope: str | None = None        # the precedence list the rank belongs to
    as_of: dt.datetime | None = None     # when this reading was first seen
    names_records: int | None = None     # how many records the document names
    document: bool = False               # a machine-read document (a lead)
    unit: str | None = None
    currency: str | None = None
    lineage: str | None = None           # the value's lineage (the measured precedence)

    @property
    def system(self) -> str:
        return system_of(self.handle)


def value_key(value: Any) -> str | None:
    """The comparison key of a value: text folded and trimmed, numbers as Decimal;
    None for no value (a blank is unknown, never a side of a disagreement)."""
    if isinstance(value, Mapping):          # an untrusted_text envelope
        if value.get("withheld"):
            # withheld from this reader: a side the tool compared on its source text,
            # shown as withheld, never equal to another
            origin = value.get("origin") if isinstance(value.get("origin"), Mapping) else {}
            return "withheld:" + str(origin.get("citation")) + str(origin.get("field_path"))
        value = _envelope_text(value)
    if value is None or isinstance(value, bool):
        return None if value is None else str(value).lower()
    text = " ".join(str(value).split())
    if not text:
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return text.casefold()
    if not number.is_finite():
        return text.casefold()
    return "n:" + format(number.normalize(), "f")


def _envelope_text(value: Mapping[str, Any]) -> Any:
    for key in ("untrusted_text", "text", "value"):
        if key in value:
            return value[key]
    return None


def shown(value: Any) -> str:
    if isinstance(value, Mapping):
        if value.get("withheld"):
            return "(withheld)"
        value = _envelope_text(value)
    return "(empty)" if value is None else " ".join(str(value).split())[:300]


def parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        out = value
    elif isinstance(value, dt.date):
        out = dt.datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            out = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return out if out.tzinfo else out.replace(tzinfo=dt.timezone.utc)


def age_words(when: dt.datetime, now: dt.datetime) -> str:
    days = (now - when).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    if days < 90:
        return f"{days} days ago"
    return f"{days // 30} months ago"


# --------------------------------------------------------------------------- resolve

def _agreeing(readings: Sequence[Reading]) -> bool:
    return len({value_key(r.value) for r in readings}) == 1


def _by_rule(readings: Sequence[Reading], governs: str | None,
             measured_as: str | None = None) -> tuple[str, list[Reading], str] | None:
    """(rule, trusted readings, why) of the first rule that decides, else None."""
    if measured_as:
        from operating.source_precedence import decide
        found = decide(measured_as, readings)
        if found is not None:
            return "measured_agreement", found[0], found[1]
    if governs:
        system = _GOVERNS.get(str(governs).lower(), governs)
        held = [r for r in readings if r.system == system]
        if held and len(held) < len(readings) and _agreeing(held):
            return ("system_of_record", held,
                    f"{SYSTEM_NAMES.get(system, system)} is the system of record for this "
                    "field (its rule says it governs)")
    scopes = {r.rank_scope for r in readings}
    if len(scopes) == 1 and all(isinstance(r.authority_rank, int) for r in readings):
        best = min(r.authority_rank for r in readings)
        held = [r for r in readings if r.authority_rank == best]
        if len(held) < len(readings) and _agreeing(held):
            label = source_label(held[0].handle)
            return ("system_of_record", held,
                    f"{label} is the system of record for this field (first in its "
                    "declared precedence)")
    tiers = [tier_of(r.handle) for r in readings]
    if all(t is not None for t in tiers) and len(set(tiers)) > 1:
        top = max(t for t in tiers if t is not None)
        held = [r for r, t in zip(readings, tiers) if t == top]
        if _agreeing(held):
            system = held[0].system
            lead = any(r.document for r in readings if r not in held)
            why = (f"{SYSTEM_NAMES.get(system, system)} is {_TIER_WORDS[top]} for this "
                   "field")
            if lead:
                why += "; the document's value is machine-read, a lead to check"
            return "system_of_record", held, why
    groups: dict[str | None, list[Reading]] = {}
    for r in readings:
        groups.setdefault(value_key(r.value), []).append(r)
    if all(r.as_of is not None for r in readings):
        newest = {k: max(r.as_of for r in g) for k, g in groups.items()}  # type: ignore
        order = sorted(newest.items(), key=lambda kv: kv[1], reverse=True)
        if order[0][1] - order[1][1] >= NEWER_BY:
            held = groups[order[0][0]]
            return ("newest", held, "the newest reading "
                    f"(first seen {order[0][1].date().isoformat()}, the other "
                    f"{order[1][1].date().isoformat()})")
    if all(isinstance(r.names_records, int) and r.names_records > 0 for r in readings):
        fewest = {k: min(r.names_records for r in g) for k, g in groups.items()}  # type: ignore
        order = sorted(fewest.items(), key=lambda kv: kv[1])
        if order[0][1] < order[1][1]:
            return ("most_specific", groups[order[0][0]],
                    "the most specific document (it is about this record; the other "
                    f"names {order[1][1]} records)")
    return None


# ------------------------------------------------------------------- tolerances
#
# Not every difference is a disagreement. Three kinds never are, and are never shown as
# "the sources disagree" (``not_a_disagreement``):
#
# * one quantity (``same_quantity``, the one rule every caller uses):
#   rounding: numbers that agree once the more precise is rounded to the places the
#   least precise states (1257 and 1257.17; 1257.17 and 1257.1666666666667), within
#   ``ROUNDING_SHARE`` of their size (0 and 0.4 still disagree), and only within one
#   currency (1000 CAD and 1000.4 USD are two amounts); and, for a quantity with a unit
#   (never money), an integer one source keeps of the other's decimal (32 and 32.5 boxes);
# * currency spellings: ``CAD`` and the Portal's ``CAD$`` are one currency
#   (``ontology.units.currency_code``, the one currency table);
# * each system's own number: a record's number or id field (a PO number)
#   whose values differ only across systems is each system numbering the same linked
#   record (BC's PO100095 and the Portal's PO#4442); two numbers inside one system still
#   disagree.
#
# A measure that is not the same measure is not compared either: the Portal's purchase
# order ``total_cost`` adds its lines' unit prices (with tax), not quantity x price,
# so
# beside another system's order
# total it is dropped as a different figure (``_comparable``), never a side of a conflict.

ROUNDING_SHARE = Decimal("0.005")
# a record's own number or id (a lot code or an item code is shared, and can disagree)
_IDENTIFIER_FIELD = re.compile(r"^(?:(?:po|so|order|purchase order|sales order|document|"
                               r"record|external|portal|bc|fm po)[ _])?(?:number|no|num|id)$",
                               re.I)
_PORTAL_UNIT_PRICE_TOTAL = re.compile(r"(?:^|\.)total_cost$")


def _number(value: Any) -> Decimal | None:
    key = value_key(value)
    if not key or not key.startswith("n:"):
        return None
    return Decimal(key[2:])


def _places(value: Any) -> int:
    text = " ".join(str(_envelope_text(value) if isinstance(value, Mapping) else value)
                    .split()).replace(",", "")
    return len(text.split(".", 1)[1]) if "." in text else 0


def rounding_only(values: Sequence[Any]) -> bool:
    """Whether numbers differ only by rounding (module note above)."""
    numbers = [_number(v) for v in values]
    if len(numbers) < 2 or any(n is None for n in numbers):
        return False
    places = min(_places(v) for v in values)
    step = Decimal(1).scaleb(-places)
    rounded = {n.quantize(step) for n in numbers}  # type: ignore[union-attr]
    if len(rounded) != 1:
        return False
    size = max(abs(n) for n in numbers)  # type: ignore[arg-type]
    spread = max(numbers) - min(numbers)  # type: ignore[type-var,operator]
    return size > 0 and spread <= size * ROUNDING_SHARE


def integer_of_decimal(values: Sequence[Any]) -> bool:
    """Whether two numbers are one quantity one source stores as an integer: exactly two
    distinct values, one a whole number and the other a value with a fraction whose floor,
    round or ceiling it is (32 and 32.5, 0 and 0.926, 157 and 156.4). Anything else is not:
    three values, two fractions, a whole number further off (31 and 32.5)."""
    numbers = [_number(v) for v in values]
    if any(n is None for n in numbers) or len(set(numbers)) != 2:
        return False
    whole = [n for n in set(numbers) if n == n.to_integral_value()]  # type: ignore[union-attr]
    if len(whole) != 1:
        return False
    (part,) = [n for n in set(numbers) if n != whole[0]]
    return abs(part - whole[0]) < 1   # type: ignore[operator]  # floor or ceiling; round is one


def same_quantity(values: Sequence[Any], *, quantity: bool) -> str | None:
    """The one rule for whether differing numbers are one value, every caller's (agent
    answers, the proactive rules, the console): ``"rounding"`` (``rounding_only``), else,
    for a ``quantity`` (a count or measure with a unit, never money), ``"integer_of_decimal"``
    (``integer_of_decimal``); None: they disagree."""
    if rounding_only(values):
        return "rounding"
    if quantity and integer_of_decimal(values):
        return "integer_of_decimal"
    return None


def currency_key(value: Any) -> str | None:
    """A currency cell's ISO code by the one currency table, else None."""
    from ontology.units import currency_code
    raw = _envelope_text(value) if isinstance(value, Mapping) else value
    return currency_code(raw)[0] if isinstance(raw, str) else None


def _comparable(field: str, readings: Sequence[Reading]) -> list[Reading]:
    """``readings`` without a Portal unit-price total beside another system's total."""
    portal = [r for r in readings if r.system == "Portal" and "purchase_order" in r.handle
              and _PORTAL_UNIT_PRICE_TOTAL.search(str(r.field_path or ""))]
    if portal and len(portal) < len(readings) and "total" in field.lower():
        return [r for r in readings if r not in portal]
    return list(readings)


def not_a_disagreement(field: str, readings: Sequence[Reading]) -> bool:
    """Whether the readings' differences are rounding, a currency spelling or each
    system's own number (the tolerances above)."""
    values = [r.value for r in readings]
    currencies = {currency_key(r.currency) or r.currency for r in readings if r.currency}
    quantity = not currencies and all(r.unit for r in readings)
    if len(currencies) <= 1 and same_quantity(values, quantity=quantity):
        # a sub-unit difference within one currency (1000 CAD and 1000.4 USD differ), or
        # a quantity one source keeps as an integer (32 and 32.5 boxes)
        return True
    if "currency" in field.lower():
        codes = {currency_key(v) for v in values}
        if len(codes) == 1 and None not in codes:
            return True
    if _IDENTIFIER_FIELD.search(field.strip()):
        by_value: dict[str | None, set[str]] = {}
        for r in readings:
            by_value.setdefault(value_key(r.value), set()).add(r.system)
        systems = list(by_value.values())
        if all(len(s) == 1 for s in systems) and \
                len(set().union(*systems)) == len(systems):
            return True
    return False


def display_value(value: Any) -> str:
    """A value as a person reads it: a number with more than four decimals is shown with
    two (1257.1666666666667 -> 1257.17); anything else as ``shown``."""
    number = _number(value)
    if number is not None and _places(value) > 4:
        return format(number.quantize(Decimal("0.01")).normalize(), "f") \
            if abs(number) >= 1 else format(number.quantize(Decimal("0.0001")).normalize(), "f")
    return shown(value)


def resolve(field: str, readings: Iterable[Reading], *, governs: str | None = None,
            now: dt.datetime | None = None, subject: str | None = None,
            measured_as: str | None = None) -> dict[str, Any] | None:
    """The contract's conflict ``{field, values:[{value, source, trusted, why}]}`` (plus
    ``rule``, ``subject`` and each value's ``as_of``/``age``) for readings the reader may
    open, or None when fewer than two distinct values remain, or when they differ only
    by rounding, a currency spelling or each system's own number (``not_a_disagreement``).
    ``measured_as``: the ``Type.field`` the measured precedence is keyed by
    (``operating.source_precedence``), tried before every other rule."""
    now = now or dt.datetime.now(dt.timezone.utc)
    present = [r for r in readings if value_key(r.value) is not None
               and isinstance(r.handle, str) and r.handle]
    present = _comparable(field, present)
    if len({value_key(r.value) for r in present}) < 2:
        return None
    if not_a_disagreement(field, present):
        return None
    present = present[:MAX_VALUES * 2]
    same_place = len({r.handle.split("#", 1)[0] for r in present}) == 1
    decided = None if same_place else _by_rule(present, governs, measured_as)
    rule = decided[0] if decided else ("self_contradiction" if same_place else "undecided")
    trusted = decided[1] if decided else []
    values = []
    for r in present[:MAX_VALUES]:
        if same_place:
            why = ("This document states this field twice with different values; both "
                   "places are cited. Check the document.")
        elif decided is None:
            why = ("No rule says which system governs this field and the readings cannot "
                   "be ordered by date, so both are shown; check with the owner.")
        elif r in trusted:
            why = decided[2][0].upper() + decided[2][1:] + "."
        else:
            why = (f"Disagrees with {source_label(trusted[0].handle)}, which is trusted "
                   f"here: {decided[2]}.")
        age = None
        if r.as_of is not None:
            age = age_words(r.as_of, now)
            if rule == "newest" or (now - r.as_of).days > STALE_DAYS:
                why += f" Read {age}."
        values.append({"value": display_value(r.value), "source": r.handle,
                       "trusted": r in trusted,
                       "why": why, "system": r.system,
                       "as_of": r.as_of.isoformat() if r.as_of else None, "age": age,
                       **({"unit": r.unit} if r.unit else {}),
                       **({"currency": r.currency} if r.currency else {})})
    out = {"field": field, "rule": rule, "values": values}
    if subject:
        out["subject"] = subject
    return out


# ---------------------------------------------------- from the tools' conflict shapes

def _is_document(handle: str, field_path: str | None) -> bool:
    return (tier_of(handle) or 0) < 4 or str(field_path or "").startswith("fields.")


def readings_of(conflict: Mapping[str, Any], *, dated: Mapping[str, Any] | None = None,
                specific: Mapping[str, int] | None = None) -> list[Reading]:
    """Readings of one ``$defs/conflict`` (``plx.object``/``plx.process``): each value
    with its citation. ``dated``: handle (or (handle, field_path)) -> when first seen;
    ``specific``: handle -> how many records its document names. A machine-read document
    against records (``tier`` lead) ranks in its own precedence list, never against the
    record's."""
    dated = dated or {}
    specific = specific or {}
    lead = conflict.get("tier") == "lead"
    out = []
    for v in conflict.get("values") or ():
        if not isinstance(v, Mapping):
            continue
        cite = v.get("citation") if isinstance(v.get("citation"), Mapping) else {}
        handle = cite.get("l1_handle")
        if not isinstance(handle, str) or not handle:
            continue
        path = cite.get("field_path")
        document = _is_document(handle, path)
        when = parse_time(dated.get((handle, path), dated.get(handle)))
        rank = v.get("authority_rank")
        out.append(Reading(
            value=v.get("value"), handle=handle, field_path=path,
            authority_rank=rank if isinstance(rank, int) and not isinstance(rank, bool)
            else None,
            rank_scope=("document" if document else "record") if lead else "record",
            as_of=when, names_records=specific.get(handle), document=document,
            lineage=cite.get("lineage") if isinstance(cite.get("lineage"), str) else None,
            unit=v.get("unit") if isinstance(v.get("unit"), str) else None,
            currency=v.get("currency") if isinstance(v.get("currency"), str) else None))
    return out


def field_name(subject: Any) -> str:
    text = str(subject or "")
    return text.split(".", 1)[-1].replace("_", " ").strip() or "value"


def tool_conflicts(conflicts: Iterable[Any], *, visible: Callable[[Sequence[str]], set],
                   dated: Mapping[str, Any] | None = None,
                   specific: Mapping[str, int] | None = None,
                   now: dt.datetime | None = None, subject: str | None = None
                   ) -> list[dict[str, Any]]:
    """The contract conflicts of the tools' ``conflicts`` list for one reader:
    ``visible(handles)`` returns the handles this reader may open; the rest are dropped
    before any rule runs."""
    out = []
    for conflict in conflicts:
        if not isinstance(conflict, Mapping) or conflict.get("kind") not in (
                None, "value_disagreement"):
            continue
        stated = (conflict.get("resolution") or {}).get("as_of") or ()
        cited = [v.get("citation") or {} for v in conflict.get("values") or ()
                 if isinstance(v, Mapping)]
        own = {(c.get("l1_handle"), c.get("field_path")): when
               for c, when in zip(cited, stated) if when}
        readings = readings_of(conflict, dated={**own, **(dated or {})}, specific=specific)
        allowed = visible([r.handle for r in readings]) if readings else set()
        readings = [r for r in readings if r.handle in allowed]
        found = resolve(field_name(conflict.get("subject")), readings, now=now,
                        subject=subject, measured_as=conflict.get("subject"))
        if found is not None:
            out.append(found)
    return out


def systems_disagree_conflicts(disagree: Iterable[Any], *,
                               visible: Callable[[Sequence[str]], set],
                               now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """``plx.process`` lateness ``systems_disagree`` (``po_status``: receipt state,
    received quantity, requested-by date) as contract conflicts; its ``governs`` names
    the system of record."""
    out = []
    for conflict in disagree:
        if not isinstance(conflict, Mapping):
            continue
        readings = []
        for side in conflict.get("sides") or ():
            cited = [v for v in side.get("values") or ()
                     if isinstance((v.get("citation") or {}).get("l1_handle"), str)]
            if not cited:
                continue
            handle = cited[0]["citation"]["l1_handle"]
            readings.append(Reading(value=side.get("value"), handle=handle,
                                    field_path=cited[0]["citation"].get("field_path"),
                                    unit=conflict.get("unit")))
        allowed = visible([r.handle for r in readings]) if readings else set()
        readings = [r for r in readings if r.handle in allowed]
        found = resolve(str(conflict.get("field") or "value").replace("_", " "), readings,
                        governs=conflict.get("governs"), now=now)
        if found is not None:
            out.append(found)
    return out


def resolution(conflict: Mapping[str, Any], *, dated: Mapping[str, Any] | None = None,
               now: dt.datetime | None = None) -> dict[str, Any] | None:
    """The tool-side summary of one ``$defs/conflict``: which values (indexes into its
    ``values``) the rules trust, the rule and why; None for a conflict without two
    distinct cited values. The tool output already holds only what its reader may read."""
    readings = readings_of(conflict, dated=dated)
    found = resolve(field_name(conflict.get("subject")), readings, now=now,
                    measured_as=conflict.get("subject"))
    if found is None:
        return None
    trusted_handles = {(v["source"], v["value"]) for v in found["values"] if v["trusted"]}
    index = [i for i, r in enumerate(readings)
             if (r.handle, display_value(r.value)) in trusted_handles]
    why = next((v["why"] for v in found["values"] if v["trusted"]), found["values"][0]["why"])
    return {"rule": found["rule"], "trusted": index[:12], "note": why[:600],
            "as_of": [r.as_of.isoformat() if r.as_of else None for r in readings][:12]}


# ---------------------------------------------------------- self-contradiction (text)

_LABELLED = re.compile(
    r"^[ \t>*-]*(?P<label>[A-Za-z][A-Za-z0-9 ./#&()-]{1,40}?)[ \t]*[:=][ \t]*"
    r"(?P<value>[^\n]{1,120})$", re.M)
_QUOTED_REPLY = re.compile(r"^(?:>|On .{5,80} wrote:|From: |-----Original Message)", re.M)
_HAS_FIGURE = re.compile(r"\d")
# Labels of fields a document states once (its dates and its own ids). Quantities,
# weights, prices and totals repeat legitimately per line, model or section (measured on
# live documents: spec sheets list a weight per model, a proposal a total per option),
# so they are never compared.
_FIELD_WORDS = re.compile(
    r"\b(date|dated|expiry|expiration|exp|due|delivery|eta|etd|po|p\.o|lot|batch|"
    r"invoice)\b")
MAX_FIGURE_CHARS = 40
_SKIP_LABELS = {"from", "to", "cc", "bcc", "sent", "subject", "date", "re", "fw", "fwd",
                "http", "https", "mailto", "tel", "phone", "fax", "email", "e-mail",
                "mobile", "cell", "time", "page", "note", "notes", "ref", "attachment"}


def _fold_label(label: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9#]+", " ", label.casefold()).split())


def self_contradictions(handle: str, parts: Sequence[tuple[str, str]], *,
                        kind: str | None = None) -> list[dict[str, Any]]:
    """The same labelled field with two values in one document. ``parts``: (place,
    text) pairs of the document (its chunks or pages, in order). Only labels whose value
    holds a figure (a date, quantity, price or id) are compared, a quoted reply in mail
    is cut first (an earlier message is not this one contradicting itself), and a sheet
    of rows (one label per row by design) is never read. Each flag cites both places."""
    if kind in ("sheet_row", "map_stage", "teams", "mc_task"):
        return []
    seen: dict[str, list[tuple[str, str, str]]] = {}
    for place, text in parts:
        cut = _QUOTED_REPLY.search(text or "")
        body = (text or "")[:cut.start()] if cut else (text or "")
        for m in _LABELLED.finditer(body):
            label = _fold_label(m.group("label"))
            value = " ".join(m.group("value").split()).rstrip(".;,")
            if (not label or label in _SKIP_LABELS or label.split()[0] in _SKIP_LABELS
                    or not _FIELD_WORDS.search(label) or not _HAS_FIGURE.search(value)
                    or len(value) > MAX_FIGURE_CHARS or ":" in value):
                # a value holding another "label:" is two fields run together by the
                # text layer, not a value
                continue
            seen.setdefault(label, []).append((place, m.group("label").strip(), value))
    out = []
    for label, reads in seen.items():
        distinct: dict[str, tuple[str, str, str]] = {}
        for read in reads:
            key = value_key(read[2]) or ""
            if not key.startswith("n:"):   # formatting is no difference: "72 °F" = "72°F"
                key = re.sub(r"[^0-9a-z]", "", key)
            distinct.setdefault(key, read)
        if len(distinct) != 2:
            continue    # one value, or a list or table of many: not a contradiction
        firsts = list(distinct.values())
        out.append({"field": firsts[0][1], "rule": "self_contradiction",
                    "source": handle,
                    "values": [{"value": value, "source": handle, "place": place,
                                "trusted": False,
                                "why": ("This document states this field more than once "
                                        "with different values; both places are cited. "
                                        "Check the document.")}
                               for place, _, value in firsts]})
    return out


def document_contradictions(conn: Any, scope: Mapping[str, Any], handles: Sequence[str],
                            *, item_allowed: Callable[[str], bool]) -> list[dict[str, Any]]:
    """``self_contradictions`` of each document the reader may open, read from its
    search chunks (``search.chunks``: the permission predicate inside the SQL, then the
    item gate per document); a document they may not open is never read."""
    from operating.search.retrievers import PERM
    handles = sorted({h for h in handles if isinstance(h, str) and h})
    if not handles:
        return []
    rows = conn.execute(
        f"""SELECT c.source_handle, c.kind, c.chunk_id, c.body FROM search.chunks c
            WHERE c.source_handle = ANY(%(handles)s::text[]) AND {PERM}
            ORDER BY c.source_handle, c.chunk_id""", dict(scope, handles=handles)).fetchall()
    parts: dict[str, list[tuple[str, str]]] = {}
    kinds: dict[str, str] = {}
    for handle, kind, chunk_id, body in rows:
        parts.setdefault(handle, []).append((chunk_id, body or ""))
        kinds[handle] = kind
    out = []
    for handle in sorted(parts):
        if item_allowed(handle):
            out += self_contradictions(handle, parts[handle], kind=kinds[handle])
    return out


# ------------------------------------------------------------------------ duplicates

def collapse_copies(items: Sequence[Mapping[str, Any]], *,
                    may_open: Callable[[str], bool],
                    location: Callable[[Mapping[str, Any]], str | None] | None = None
                    ) -> list[dict[str, Any]]:
    """One item per duplicate cluster, in first-seen order. ``items``: dicts with
    ``handle`` and ``dup_group`` (None: unique), optional ``authority`` and
    ``occurred_at``. The representative is the best copy this reader may open (highest
    tier, then earliest, then handle: ``chunking.dedup.representative``); ``also_in``
    lists the other copies they may open, with their location. Copies they may not open
    are dropped without trace; a cluster none of whose copies they may open is gone."""
    from operating.chunking.dedup import representative

    order: list[str] = []
    clusters: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        handle = item.get("handle")
        if not isinstance(handle, str) or not handle:
            continue
        key = ("g:" + item["dup_group"]) if item.get("dup_group") else ("h:" + handle)
        if key not in clusters:
            order.append(key)
        clusters.setdefault(key, []).append(item)
    out = []
    for key in order:
        members: dict[str, Mapping[str, Any]] = {}
        for item in clusters[key]:
            if item["handle"] not in members and may_open(item["handle"]):
                members[item["handle"]] = item
        if not members:
            continue
        rows = [(h, int(m.get("authority") or tier_of(h) or 0),
                 str(m.get("occurred_at") or "")) for h, m in members.items()]
        best = representative(rows)
        rep = dict(members[best])
        others = [h for h in sorted(members) if h != best]
        rep["also_in"] = {"count": len(others), "copies": [
            {"handle": h, "location": (location(members[h]) if location else None)
             or system_of(h)} for h in others[:MAX_ALSO_IN]]}
        out.append(rep)
    return out


# ---------------------------------------------------------------------- near matches

def did_you_mean(understanding: Any) -> list[dict[str, Any]]:
    """the ambiguous mentions as "did you mean" choices: (mention, prompt,
    choices[{object_id, type, label, why}]). Never resolved here: an exact id decides."""
    out = []
    for mention in getattr(understanding, "entities", None) or ():
        if not getattr(mention, "ambiguous", False):
            continue
        ids = list(getattr(mention, "choices", None) or ())
        by_id = {c.object_id: c for c in getattr(mention, "candidates", ())}
        choices = [{"object_id": i, "type": by_id[i].type, "label": by_id[i].label,
                    "why": by_id[i].why} for i in ids if i in by_id]
        if choices:
            out.append({"mention": mention.text, "prompt": mention.prompt,
                        "choices": choices})
    return out


__all__ = ["Reading", "collapse_copies", "did_you_mean", "resolve", "resolution",
           "self_contradictions", "systems_disagree_conflicts", "tool_conflicts",
           "value_key"]
