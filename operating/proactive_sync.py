"""Cross-system sync gaps: a record kept in one system that, ``SYNC_DAYS`` after it was made,
is still missing from the system it must reach, or whose copy there states another value
of a key field. Run by ``operating.proactive_engine`` as ``sync_gaps`` beside its other
rules, set-based over the store with index reads only (records by the type index, links
and identifiers by their end indexes). Nothing here writes: a gap's next step is a person
keying or correcting the record.

Each ``SyncPair`` declares a record type, the system a record is kept in first, the system
it must reach and the key fields compared there. A record's counterpart is found over the
existing cross-system joins only, never a guess made here:

* the type's identity links (``<Type>.same_record_as``) and posting links
  (``<Type>.bc_posted_as``, the entity map) the schema declares, any end, any tier: a
  settled or corroborated one (``COMPARE_TIERS``) is a copy whose fields are compared, a
  lead (a number match) only says one probably exists;
* key joins: a primary identifier (``identifiers``: the record's own number, kind and
  normalized value) the record shares with a primary identifier of a record of the same
  type in the counterpart system, a probable copy as well;
* a pair's ``names`` (an item's name): a counterpart record of the type stating the same
  folded name is a probable copy keyed under another code, so the record is not raised
  missing (never compared, never a join).

A record is judged only when it is in the active population (``inactive``: the engine's
one predicate for orders, else its own status words or deleted/blocked flags and its
business dates within ``active_population.ACTIVE_DAYS``), was made ``SYNC_DAYS`` or more
before the day judged (its earliest business date, import days left out, or the day the
store first recorded it), and on or after the first business date the counterpart system
holds a record of the type (a record older than the counterpart system's own records is
history it was never meant to reach). A pair whose judged records are joined to a copy
less than ``MIN_JOINED`` of the time is not in use for that type yet: no record of it is
raised missing, one reason says so (``counterpart_not_in_use_<system>``). Test, UAT and demo records (the engine's
``test_records``) never make a system count as holding a type nor date its first day: a
system holding only test records of a type is not keyed into yet
(``counterpart_not_held_<system>``); a join to one still proves the record was keyed. Missing (``sync_missing``): no counterpart at all, and the record is not a draft (a draft
is not placed yet, so nothing must reach the other system);
its claim rests on what the counterpart system does not hold, so it requires a grant on
every audience that system's records of the type sit in (``requires``). Differs
(``sync_mismatch``): a compared copy states another value of a key field (an amount as
``Decimal`` in one currency, another currency never compared; a text by its folded
words, a unit by its canonical token (``ontology.units.unit_token``: CSE, cs and case are
one unit); a side that states none, or a total of zero, is not compared; the same
two values on ``CONVENTION_MIN`` or more copies and ``CONVENTION_SHARE`` of those compared
is how the two systems spell it, counted ``convention``, never raised), ONE gap per pair of records and
field, each record its own cited group (a reader who may not open both never sees it),
amounts finance-marked, a customer PO (often a buyer's name) personal-data-marked; no
contact or commercial term is compared.

No rule names a record, a customer or an item; fixture runs are ``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.units import unit_token

from . import active_population as active
from . import proactive_business as pb
from .proactive_quality import (_bindings, _flag_active, _said, _status_class, _system,
                                _text)
from .proactive_stock import declares, waiting

RULE = "sync_gaps"
MISSING = "sync_missing"
MISMATCH = "sync_mismatch"
KINDS = (MISSING, MISMATCH)
SYNC_DAYS = 7
JOIN_SUFFIXES = (".same_record_as", ".bc_posted_as")
COMPARE_TIERS = ("corroborated",)   # beside the run's settled tiers
LABEL_PROPS = ("order_number", "po_number", "invoice_number", "bc_number", "code",
               "erp_code", "name")
STATUS_PROPS = ("status", "bc_status")
DRAFT_WORDS = frozenset({"draft", "planned"})
FLAG_PROPS = ("deleted_at", "bc_blocked", "blocked", "is_active")
CREATED_PROPS = ("portal_created_at", "fm_created_at")
SYSTEM_WORDS = {"bc": "Business Central", "portal": "the Portal", "sage": "Sage",
                "fm": "FileMaker"}
AMOUNT, TEXT, UNIT = "amount", "text", "unit"
MIN_JOINED = Decimal("0.5")   # of a pair's judged records, the share joined to a copy
CONVENTION_MIN = 20           # copies two values must differ the same way on ...
CONVENTION_SHARE = Decimal("0.2")   # ... and their share of the compared copies
# a customer PO field often holds a buyer's name instead of a number: shown only to
# readers holding personal data (``pii``); amounts only to finance readers
PII = '["pii"]'


@dataclass(frozen=True)
class Field:
    words: str
    props: tuple[str, ...]   # the property each system may state it in (first stated)
    kind: str                # AMOUNT or TEXT
    marks: str | None = None  # the markings a reader must hold to see the two values


@dataclass(frozen=True)
class SyncPair:
    type: str
    source: str              # the system a record is kept in first
    counterpart: str         # the system it must reach
    fields: tuple[Field, ...]
    dates: tuple[str, ...]   # the business dates that age a record
    label: str
    by_status: bool          # active by its status words (else by its flags)
    # names whose exact folded text on a counterpart record makes it a probable copy (an
    # item keyed under another code in each system): never a join, only "not missing"
    names: tuple[str, ...] = ()


PAIRS = (
    SyncPair("SalesOrder", "portal", "bc",
             (Field("total", ("total_amount",), AMOUNT),
              Field("customer PO", ("customer_po",), TEXT, PII)),
             ("order_date",), "Sales order", True),
    SyncPair("PurchaseOrder", "bc", "portal",
             (Field("total", ("total_cost",), AMOUNT),),
             ("order_date",), "Purchase order", True),
    SyncPair("SalesInvoice", "sage", "bc",
             (Field("total", ("total_amount",), AMOUNT),
              Field("customer PO", ("customer_po",), TEXT, PII)),
             ("invoice_date",), "Sales invoice", False),
    SyncPair("Product", "bc", "portal",
             (Field("unit", ("bc_base_uom", "uom"), UNIT),),
             (), "Item", False, ("name",)),
)


def _in(system: str, want: str) -> bool:
    return system == want or system.startswith(want + "/")


def _words(system: str) -> str:
    return SYSTEM_WORDS.get(system.split("/", 1)[0], system)


def _fold(raw: Any) -> str:
    return " ".join(str(raw or "").split()).casefold()


def _stated(row: Mapping[str, Any] | None) -> bool:
    return row is not None and _fold(row.get("value")) not in ("", "none", "null")


def _key_partners(st: Any, ids: Sequence[str], want: set[str]) -> dict[str, set[str]]:
    """record -> the records of ``want`` it shares an identifier (kind and normalized
    value) with: a key join, read by the identifier table's indexes."""
    out: dict[str, set[str]] = defaultdict(set)
    # the record's own number (a primary identifier) on both sides: a shared customer or
    # item code says nothing about this record
    sql = ("SELECT i.object_id, j.object_id FROM identifiers i JOIN identifiers j "
           "ON j.identifier_kind = i.identifier_kind AND j.normalized = i.normalized "
           "WHERE i.object_id IN (SELECT value FROM json_each(?)) "
           "AND i.role = 'primary' AND j.role = 'primary' "
           "AND i.superseded_at IS NULL AND j.superseded_at IS NULL "
           "AND j.object_id <> i.object_id")
    for start in range(0, len(ids), 500):
        pb._tick(st)
        for a, b in st.conn.execute(sql, (json.dumps(list(ids[start:start + 500])),)):
            if b in want:
                out[a].add(b)
    return out


def _name_partners(st: Any, pair: SyncPair, ids: Sequence[str], want: set[str]
                   ) -> dict[str, set[str]]:
    """record -> the records of ``want`` stating the same folded name: a probable copy the
    other system keyed under its own code. An empty or placeholder name joins nothing."""
    if not ids or not want:
        return {}
    named: dict[str, set[str]] = defaultdict(set)
    for oid, entry in st.values([pair.type], pair.names, objects=sorted(want)).items():
        for prop in pair.names:
            for row in entry["props"].get(prop, ()):
                if _stated(row):
                    named[_fold(row.get("value"))].add(oid)
    out: dict[str, set[str]] = defaultdict(set)
    for oid, entry in st.values([pair.type], pair.names, objects=list(ids)).items():
        for prop in pair.names:
            for row in entry["props"].get(prop, ()):
                if _stated(row) and named.get(_fold(row.get("value"))):
                    out[oid] |= named[_fold(row.get("value"))]
    return out


def _first_recorded(st: Any, ids: Sequence[str]) -> dict[str, date]:
    out = {}
    for start in range(0, len(ids), 500):
        for oid, raw in st.conn.execute(
                "SELECT object_id, first_recorded_at FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(ids[start:start + 500])),)):
            day = active.day_of(raw)
            if day is not None:
                out[oid] = day
    return out


def _value(field: Field, props: Mapping[str, list], codes: Mapping[tuple, str | None]
           ) -> tuple[Any, Mapping[str, Any]] | None | bool:
    """(comparable value, its row); None when the record states none; False when what it
    states cannot be read (several values, no number, no currency)."""
    for name in field.props:
        rows = [r for r in props.get(name) or () if _stated(r)]
        if not rows:
            continue
        row = pb._one(rows)
        if row is None:
            return False
        if field.kind == AMOUNT:
            number = pb._dec(row.get("value"))
            if number == 0:
                return None   # an unpriced copy states no total, never a total of nothing
            currency = pb._currency(codes, row)
            return ((number, currency), row) if number is not None and currency else False
        if field.kind == UNIT:
            # a unit by its canonical token: CSE, cs and case are one unit
            return unit_token(row.get("value")) or _fold(row.get("value")), row
        return _fold(row.get("value")), row
    return None


def _shown(value: Any, row: Mapping[str, Any]) -> str:
    if isinstance(value, tuple):
        return f"{format(value[0], 'f')} {value[1]}"
    return _text(row)   # as the record states it


def sync_gaps(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
              settled: Sequence[str], skips: Callable,
              inactive: Callable[[], Mapping[str, str]] | None = None,
              tests: Callable[[], Mapping[str, str]] | None = None,
              pairs: Sequence[SyncPair] = PAIRS, days: int = SYNC_DAYS) -> dict[str, Any]:
    """``inactive``: the engine's one active-population predicate (order record ->
    ``closed``/``historic``); a type it does not judge is judged by its own status words
    or flags and its business dates. ``tests``: the engine's test records (test, UAT,
    demo): never a counterpart, never the counterpart system's first day (a system that
    holds only test records of a type is not yet keyed into)."""
    served = []
    for pair in pairs:
        joins = tuple(pair.type + s for s in JOIN_SUFFIXES
                      if declares(pair.type, None, pair.type + s))
        if declares(pair.type) and joins:
            served.append((pair, joins))
    if not served:
        return result([], status="blocked",
                      reasons=[waiting(f"{p.type}.same_record_as") for p in pairs])
    skipped = skips()
    counts: Counter = Counter()
    gaps = []
    verdicts = inactive() if inactive is not None else {}
    trial = tests() if tests is not None else {}
    compare = frozenset((*settled, *COMPARE_TIERS))
    for pair, joins in served:
        binding = _bindings(st, [pair.type])
        system = {oid: _system(held) for oid, held in binding.items()}
        source = sorted(o for o, s in system.items() if _in(s, pair.source))
        # a test record there still proves the record was keyed (a join to it is a copy);
        # it never makes the system count as holding the type, nor dates its first day
        held_there = {o for o, s in system.items() if _in(s, pair.counterpart)}
        other = held_there - set(trial)
        if not source:
            continue
        if not other:
            skipped.note(f"counterpart_not_held_{pair.counterpart}")
            continue
        # every audience the counterpart's records sit in: a "missing" verdict rests on
        # what they do not hold
        absent_in = sorted({binding[o][1] for o in other})
        partners: dict[str, dict[str, str]] = defaultdict(dict)   # record -> copy -> link marks
        probable: dict[str, set[str]] = defaultdict(set)
        for r in st.touching(source, list(joins)):
            if r[7]:
                continue
            a, b, tier, marks = r[3], r[4], r[5], r[6]
            for mine, theirs in ((a, b), (b, a)):
                if theirs in held_there and system.get(mine) and _in(system[mine], pair.source):
                    if tier in compare:
                        partners[mine].setdefault(theirs, marks)
                    else:
                        probable[mine].add(theirs)
        pair_counts: Counter = Counter()
        compared: Counter = Counter()
        pending_missing: list[dict] = []
        pending_differs: list[tuple[tuple, dict]] = []
        lonely = [o for o in source if o not in partners and o not in probable]
        for mine, theirs in _key_partners(st, lonely, held_there).items():
            probable[mine] |= theirs
        if pair.names:
            lonely = [o for o in lonely if o not in probable]
            for mine, theirs in _name_partners(st, pair, lonely, held_there).items():
                probable[mine] |= theirs
        props = (*pair.dates, *CREATED_PROPS, *active.EDIT_TIME_PROPS, *STATUS_PROPS,
                 *FLAG_PROPS, *LABEL_PROPS, *(p for f in pair.fields for p in f.props))
        copies = sorted({c for held in partners.values() for c in held})
        found = st.values([pair.type], props, objects=[*source, *copies])
        floor_rows = st.values([pair.type], pair.dates, objects=sorted(other)) \
            if pair.dates else {}
        dated = (*pair.dates, *CREATED_PROPS)
        imported = active.import_days(
            ((binding[o][0], n), r.get("value")) for held in (found, floor_rows)
            for o, e in held.items() for n in dated for r in e["props"].get(n, ()))

        def business(oid: str, names: Sequence[str], held: Mapping = found) -> list[date]:
            entry = held.get(oid) or {"props": {}}
            return active.business_days(
                ({"value": r.get("value"), "binding": binding[oid][0], "property": n}
                 for n in names for r in entry["props"].get(n, ())), imported)
        floor = min((d for o in floor_rows for d in business(o, pair.dates, floor_rows)),
                    default=None)
        seen = _first_recorded(st, source)
        codes = pb._currencies(st, [r for e in found.values() for f in pair.fields
                                    if f.kind == AMOUNT for p in f.props
                                    for r in e["props"].get(p) or ()])
        for oid in source:
            pb._tick(st)
            entry = found.get(oid)
            if entry is None:
                skipped.note("record_unread", [oid])
                continue
            counts["records"] += 1
            own = entry["props"]
            why = verdicts.get(oid)
            if why is None and pair.type not in ("SalesOrder", "PurchaseOrder"):
                cls, _row = (_status_class(own, STATUS_PROPS) if pair.by_status
                             else _flag_active(own))
                if cls == "unknown":
                    skipped.note("status_unknown", [oid])
                    continue
                why = active.inactive_reason(
                    active.CLOSED if cls == "done" else "open", as_of=as_of,
                    dates=business(oid, (*pair.dates, *CREATED_PROPS,
                                         *active.EDIT_TIME_PROPS)))
            if why is not None:
                counts[why] += 1
                continue
            made_on = min([*business(oid, dated), *([seen[oid]] if oid in seen else [])],
                          default=None)
            if made_on is None:
                skipped.note("age_unknown", [oid])
                continue
            if (as_of - made_on).days < days:
                counts["recent"] += 1
                continue
            label_rows = [r for p in LABEL_PROPS for r in [pb._one(_said(own, (p,)))] if r]
            if not label_rows:
                skipped.note("record_uncited", [oid])
                continue
            named = f"{pair.label} {_text(label_rows[0])}"
            here, there = _words(system[oid]), _words(pair.counterpart)
            if oid not in partners:
                if oid in probable:
                    counts["probable_copy"] += 1
                    pair_counts["joined"] += 1
                    continue
                if floor is not None and made_on < floor:
                    counts["before_counterpart"] += 1   # older than the system it must reach
                    continue
                if any(_fold(r.get("value")) in DRAFT_WORDS for r in _said(own, STATUS_PROPS)):
                    counts["draft"] += 1   # not placed yet: nothing to key elsewhere
                    continue
                pair_counts["missing"] += 1
                made = gap(MISSING, RULE, oid, pair.type, f"missing|{pair.counterpart}",
                           summary=f"{named} is in {here} but not in {there} "
                                   f"{(as_of - made_on).days} days after it was made",
                           missing=f"Key {named} into {there}, or link it to its copy there "
                                   "if it was keyed under another number.",
                           why=f"No record in {there} is linked to it or shares one of its "
                               f"identifiers, and it is still active.",
                           priority="medium", about=(pair.type, "sync"),
                           groups=[cites(label_rows[:1])],
                           event={"days": (as_of - made_on).days})
                if made is not None:
                    made["requires"] = absent_in
                    pending_missing.append(made)
                continue
            counts["linked"] += 1
            pair_counts["joined"] += 1
            for copy, marks in sorted(partners[oid].items()):
                theirs = found.get(copy)
                if theirs is None:
                    skipped.note("copy_unread", [oid, copy])
                    continue
                copy_label = [r for p in LABEL_PROPS
                              for r in [pb._one(_said(theirs["props"], (p,)))] if r]
                for field in pair.fields:
                    a = _value(field, own, codes)
                    b = _value(field, theirs["props"], codes)
                    if a is False or b is False:
                        skipped.note("value_unreadable", [oid, copy])
                        continue
                    if a is None or b is None:
                        continue   # a side that states none is not compared
                    if field.kind == AMOUNT and a[0][1] != b[0][1]:
                        counts["currency_differs"] += 1
                        continue
                    compared[field.props[0]] += 1
                    if a[0] == b[0]:
                        counts["agree"] += 1
                        continue
                    marks_needed = pb.FINANCE if field.kind == AMOUNT else field.marks
                    other_name = (f"{pair.label} {_text(copy_label[0])}" if copy_label
                                  else f"its {there} copy")
                    made = gap(MISMATCH, RULE, oid, pair.type,
                               f"{copy}|{field.props[0]}",
                               summary=f"{named} in {here} and {other_name} in {there} "
                                       f"disagree on the {field.words}: {here} says "
                                       f"{_shown(*a)}, {there} says {_shown(*b)}",
                               missing=f"Which {field.words} is right? Correct it in the "
                                       "system that is wrong.",
                               why=f"The two copies of one record state different "
                                   f"{field.words} values {days} or more days after it "
                                   "was made.",
                               priority="medium", about=(pair.type, field.props[0]),
                               groups=[cites([a[1]], marks, marks_needed),
                                       cites([b[1]], marks, marks_needed)],
                               event={"days": (as_of - made_on).days})
                    if made is not None:
                        made["requires"] = sorted({binding[oid][1], binding[copy][1]})
                        pending_differs.append(((field.props[0], str(a[0]), str(b[0])), made))
        # a counterpart system this type is (almost) never joined to is not in use for it
        # yet: every record would be "missing", one reason says so instead
        judged = pair_counts["joined"] + pair_counts["missing"]
        if pending_missing and pair_counts["joined"] < MIN_JOINED * judged:
            counts["not_in_use"] += len(pending_missing)
            skipped.note(f"counterpart_not_in_use_{pair.counterpart}")
        else:
            counts["missing"] += len(pending_missing)
            gaps.extend(pending_missing)
        # the same two values on a bulk share of the copies is how the two systems spell
        # it (a unit's two names), never a keying error
        pattern = Counter(key for key, _made in pending_differs)
        for key, made in pending_differs:
            if pattern[key] >= CONVENTION_MIN and \
                    pattern[key] >= CONVENTION_SHARE * compared[key[0]]:
                counts["convention"] += 1
            else:
                counts["differs"] += 1
                gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in (
        "records", "closed", "historic", "recent", "before_counterpart", "probable_copy",
        "draft", "missing", "not_in_use", "linked", "agree", "differs", "convention",
        "currency_differs")}
    return out
