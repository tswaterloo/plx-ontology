"""A value a bulk share of a source's records hold is that source's default, not a business
statement: the one test every proactive rule asks before it reads a value as a state.

A date or status a person entered is spread over the records it belongs to; a value an
import, a migration or a form default wrote sits on a large share of them at once. Two
readings, one test (``bulk_values``), each with its own share:

* **a date** (a requested-by date, an expected receipt date) held by at least
  ``DATE_SHARE`` of the property's records (and at least ``MINIMUM`` of them) is a bulk
  day (``default_days``). For a customer's required date a bulk day is erased only with
  provenance (``proven_defaults``, BIZ-007): it is also a day the holders were written (a
  bulk creation or import day of the population, or the own day of most of its holders).
  Many orders sharing a real deadline keep it. A date equal to one record's own creation,
  migration or order day (``own_day``) is a doubt, never proof: the date is kept as a
  candidate and the rule that judges it says so;
* **a status word** that marks at least ``STATE_SHARE`` of the records in one lifecycle
  state (and at least ``MINIMUM`` of them) is that state's default, never a decision
  someone took about one record (``state_defaults``): a QC "on hold" every unmade batch
  carries says nothing about any of them.

No value, record or source is named here: every rule passes its own population.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any, Hashable, Iterable, Mapping

DATE_SHARE = 0.10
STATE_SHARE = 0.95
MINIMUM = 10
# A record's own days (its creation, migration or order stamp), as the sources store them:
# a date equal to one of them was set to the day a form or an import wrote the record.
OWN_DAY_PROPS = ("order_date", "portal_created_at", "fm_created_at")


def bulk_values(values: Iterable[Hashable], *, share: float, minimum: int = MINIMUM
                ) -> frozenset:
    """The values (None never counts) held by at least ``share`` of the non-null values
    and by at least ``minimum`` of them."""
    counts = Counter(v for v in values if v is not None)
    total = sum(counts.values())
    if not total:
        return frozenset()
    return frozenset(v for v, n in counts.items() if n >= minimum and n / total >= share)


def default_days(days: Iterable[date | None], *, share: float = DATE_SHARE,
                 minimum: int = MINIMUM) -> frozenset:
    """The days a bulk share of one date property's records hold (``bulk_values``)."""
    return bulk_values(days, share=share, minimum=minimum)


PROVEN_SHARE = 0.5   # of a bulk day's holders written that same day: the day wrote them


def own_days(values: Iterable[Any]) -> frozenset:
    """A record's own days from its stored stamps or dates: each one's business day and
    its written day (a migration that wrote ``2026-04-16T02:54`` UTC wrote ``2026-04-16``
    though the business day is the 15th)."""
    from .time_window import business_day

    out: set[date] = set()
    for value in values:
        if isinstance(value, date):
            out.add(value)
            continue
        for day in (business_day(value), business_day(str(value).strip()[:10])
                    if value is not None else None):
            if day is not None:
                out.add(day)
    return frozenset(out)


def proven_defaults(records: Iterable[tuple[date | None, Iterable[Any]]], *,
                    share: float = DATE_SHARE, minimum: int = MINIMUM) -> frozenset:
    """``(date, the record's own stamps)`` per record of one population -> the bulk days
    (``default_days``) with provenance: a bulk creation or import day of the same records
    (``default_days`` over their own days), or the own day of at least ``PROVEN_SHARE``
    of the records holding it. A bulk day without either is many records sharing a date,
    kept (BIZ-007)."""
    held: list[tuple[date | None, frozenset]] = [(d, own_days(own)) for d, own in records]
    bulk = default_days((d for d, _own in held), share=share, minimum=minimum)
    if not bulk:
        return frozenset()
    written = default_days((d for _d, own in held for d in own), share=share,
                           minimum=minimum)
    out = set()
    for day in bulk:
        holders = [own for d, own in held if d == day]
        if day in written or sum(day in own for own in holders) >= PROVEN_SHARE * len(holders):
            out.add(day)
    return frozenset(out)


def own_day(day: date | None, record_days: Iterable[date | None]) -> bool:
    """Whether ``day`` is one of the record's own days (its creation, migration or
    order day): a date a form or an import set to the day it wrote the record."""
    return day is not None and any(day == d for d in record_days if d is not None)


def state_defaults(pairs: Iterable[tuple[Hashable, Hashable]], *, share: float = STATE_SHARE,
                   minimum: int = MINIMUM) -> dict[Hashable, frozenset]:
    """``(lifecycle state, value)`` per record -> per state, the values that mark a bulk
    share of its records (``bulk_values`` within each state)."""
    by_state: dict[Hashable, list[Hashable]] = defaultdict(list)
    for state, value in pairs:
        by_state[state].append(value)
    return {state: found for state, values in by_state.items()
            if (found := bulk_values(values, share=share, minimum=minimum))}


def is_state_default(defaults: Mapping[Hashable, frozenset], state: Hashable, value: Any
                     ) -> bool:
    return value in defaults.get(state, frozenset())
