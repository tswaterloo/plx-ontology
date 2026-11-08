"""The one active-population rule every record-level rule asks before it raises an item
about an order, and the one business-date reader that tells an import stamp from a date a
person entered.

A rule about one order (late, undated, missing a customer PO) is worth raising only while
the order is live: open by the one status definition of its type AND touched recently.
An order is **active** when it is not closed and either a business date of it (its due,
order or own edit date) falls within ``ACTIVE_DAYS`` of the day judged, or nothing dates it
at all (an undated record is never called historic on no evidence). Otherwise it is
``closed`` or ``historic`` (``inactive_reason``); a rule counts those apart (its coverage
says how many) instead of listing them as current work.

A date that a bulk share of one source's records hold (``source_defaults.default_days``:
the day an import, a migration or a sync wrote them) is no business date: it never makes a
record active, never dates it on a page (``business_day``), and the copy of another system
that holds a real date is preferred. Timestamps are compared by their day, so a load that
stamped thousands of rows within one day is found the same way.

No record, value or source is named here: every caller passes its own population.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Hashable, Iterable, Mapping, Sequence

from . import source_defaults

ACTIVE_DAYS = 180
CLOSED, HISTORIC = "closed", "historic"
# the edit stamps a record's own system writes (the newest is its last activity)
EDIT_TIME_PROPS = ("portal_updated_at", "fm_modified_at", "bc_last_modified_at")
IMPORT_DATE = "import_date"   # how a page marks a date that is a bulk load's day


def day_of(value: Any) -> date | None:
    """The day a stored date or timestamp names (a row's ``value`` or plain text)."""
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def import_days(pairs: Iterable[tuple[Hashable, Any]]) -> dict[Hashable, frozenset]:
    """``(population key, date or stamp)`` pairs (a population: one property of one
    source's records) -> per key, the days a bulk share of its records hold: the days an
    import or migration wrote, never a business date (``source_defaults.default_days``)."""
    days: dict[Hashable, list[date | None]] = defaultdict(list)
    for key, value in pairs:
        days[key].append(day_of(value))
    return {key: found for key, held in days.items()
            if (found := source_defaults.default_days(held))}


def business_days(rows: Iterable[Mapping[str, Any]], defaults: Mapping[Hashable, frozenset],
                  key: Any = lambda row: (row.get("binding"), row.get("property"))
                  ) -> list[date]:
    """The days of ``rows`` that are business dates: readable and not a bulk import day of
    their population (``key(row)``, by default its binding and property)."""
    out = []
    for row in rows:
        day = day_of(row)
        if day is not None and day not in defaults.get(key(row), frozenset()):
            out.append(day)
    return out


def business_day(rows: Sequence[Mapping[str, Any]], defaults: Mapping[Hashable, frozenset],
                 key: Any = lambda row: (row.get("binding"), row.get("property"))
                 ) -> tuple[date | None, list[Mapping[str, Any]]]:
    """One date of a record across its copies (``rows``, each with ``value`` and its
    population ``key``): the earliest business date any copy holds (a later copy's day
    is the day it was loaded into a newer system), and the rows that are import days
    (shown marked, never as the date). None when every copy's date is an import day."""
    real = [(day_of(r), r) for r in rows if day_of(r) is not None
            and day_of(r) not in defaults.get(key(r), frozenset())]
    imported = [r for r in rows if day_of(r) is not None
                and day_of(r) in defaults.get(key(r), frozenset())]
    return (min(d for d, _ in real) if real else None), imported


def inactive_reason(cls: str, *, as_of: date, dates: Iterable[date | None] = (),
                    days: int = ACTIVE_DAYS) -> str | None:
    """Why an order is out of the active population (None: it is active). ``cls``: its
    class by its type's one status definition (``closed`` is out); ``dates``: its business
    dates (due, order and edit days, import days already removed). Historic when it holds
    dates and none falls within ``days`` of ``as_of``."""
    if cls == CLOSED:
        return CLOSED
    held = [d for d in dates if d is not None]
    if held and not any(abs((as_of - d).days) <= days for d in held):
        return HISTORIC
    return None


# The page dates a bulk load may have written (a record's order day): marked on its page
PAGE_DATE_PROPS = ("order_date",)
_IMPORT_CACHE: dict[tuple, dict] = {}


def store_import_days(store_path: Any, object_type: str,
                      props: Sequence[str] = PAGE_DATE_PROPS) -> dict[Hashable, frozenset]:
    """Per ``(binding, property)`` of ``object_type``'s ``props``, the bulk import days the
    served store holds (``import_days``), read once per store file (its size and time)."""
    from pathlib import Path

    from . import po_status
    from . import proactive_engine as pe

    path = Path(store_path)
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns, object_type, tuple(props))
    except OSError:
        return {}
    if key not in _IMPORT_CACHE:
        st = pe._Store(path)
        try:
            held = st.values((object_type,), tuple(props))
        finally:
            st.close()
        _IMPORT_CACHE.clear()   # one store generation's days at a time
        _IMPORT_CACHE[key] = import_days(
            ((po_status.binding_of(oid), n), r.get("value")) for oid, entry in held.items()
            for n in props for r in entry["props"].get(n, ()))
    return _IMPORT_CACHE[key]
