"""A value a bulk share of a source's records hold is that source's default, not a business
statement: the one test every proactive rule asks before it reads a value as a state.

A date or status a person entered is spread over the records it belongs to; a value an
import, a migration or a form default wrote sits on a large share of them at once. Two
readings, one test (``bulk_values``), each with its own share:

* **a date** (a requested-by date, an expected receipt date) held by at least
  ``DATE_SHARE`` of the property's records (and at least ``MINIMUM`` of them) is the day a
  sync or migration wrote them, so no record holding it states a requested date
  (``default_days``); a date equal to the record's own creation, migration or order day
  is that record's default (``own_day``);
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
