"""The one sales-order status definition and the one required-date provenance test every
caller uses: the proactive engine's late customer order rule, the impact chain's orders
at risk and item-demand leads, the fast answer's open orders and the live-ops evals.

A customer order is one business order held as several records (Portal, FileMaker order
entry, Sage OE, BC), joined by settled ``SalesOrder`` identity links only. Each record is
read by its own system's status word (the vocabularies differ):

* a closed word of any system (``CLOSED_WORDS``), a BC ``fullyShipped`` true, or a record
  that stands only for an order BC deleted once it was posted (a binding with
  ``stands_for``) is **closed**; so is a record a settled shipment or invoice is linked to
  (``FULFILLED_LINKS``: fulfilled at least in part, never claimed open past its date);
* an open word of its own system (``OPEN_WORDS``) is **open**; the Portal's DRAFT is
  **draft** (an order not yet placed: neither open nor closed);
* any other word, a record holding no status, and a Sage record by its COMPLETE code
  (``SAGE_CODE``: its values are not documented here, a Sage record is closed only by the
  shipments and invoices linked to it) is **unknown**, with its reason.

The order (``order_status``) is closed when ANY record of it is closed (a stale Open
record beside a Closed twin is closed), else unknown when any is unknown (never guessed
open), else open when any is open, else draft.

An order states goods to ship only through its lines: an order none of whose records holds
a line (``LINE_OF`` from any system's order line) is not judged (``NO_LINES``). A header
copied without its lines (a bulk load's second copy of an order another record carries
with its items) or an empty shell says nothing about what the customer waits for; the
record that holds the items carries the order.

A required date is a requested date only when it is no source default
(``operating.source_defaults``): not a day a bulk share of its binding's records hold
(``date_defaults``), and not one of the record's own days (its creation or order day,
``OWN_DAYS``). An order whose every date is a default is undated, never late
(``requested_date``). Among requested dates the declared precedence of
``SalesOrder.required_date`` governs (the top lineage holding one); two dates in it are
disputed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import functools
from typing import Any, Iterable, Mapping, Sequence

from . import source_defaults

OPEN, CLOSED, DRAFT, UNKNOWN = "open", "closed", "draft", "unknown"
TYPE = "SalesOrder"
STATUS_PROPS = ("status", "fm_status", "bc_status")
SHIPPED_FLAG = "bc_fully_shipped"
SAGE_CODE = "sage_complete"
READ_PROPS = (*STATUS_PROPS, SHIPPED_FLAG, SAGE_CODE)
SYSTEM_OF = {"status": "the Portal", "fm_status": "FileMaker", "bc_status": "BC",
             SHIPPED_FLAG: "BC", SAGE_CODE: "Sage"}
# Closed words of any system: the Portal's SHIPPED, COMPLETE and CANCELLED, FileMaker's
# Closed and Complete, the Portal's reading of FM text as finished ("realized").
CLOSED_WORDS = frozenset({"shipped", "invoiced", "closed", "cancelled", "canceled",
                          "completed", "complete", "delivered", "realized"})
# Each system's words for an order still to ship. The Portal's SALES_ORDER_STATUSES (and
# its legacy "Open"); FileMaker's "Open"; BC's API status (Draft, In Review, Open: BC's
# unreleased and released order documents, both still to ship unless fullyShipped).
OPEN_WORDS = {"status": frozenset({"open", "confirmed", "in production", "ready",
                                   "ready to ship", "partially shipped"}),
              "fm_status": frozenset({"open"}),
              "bc_status": frozenset({"draft", "in review", "open"})}
DRAFT_WORDS = {"status": frozenset({"draft"})}
FULFILLED_LINKS = ("Shipment.ships", "SalesInvoice.bills")
DATE_PROP = "required_date"
OWN_DAYS = ("order_date", "portal_created_at", "fm_created_at")
# Unknown reasons, as every reader counts them.
STATUS_MISSING, STATUS_UNMAPPED = "status_missing", "status_unmapped"
# An order line of any system -> its order; an order no record of which has one is not
# judged (``NO_LINES``).
LINE_OF = "SalesOrderLine.line_of"
NO_LINES = "no_order_lines"


@dataclass(frozen=True)
class Status:
    cls: str
    reason: str | None = None   # why it is unknown
    open_by: tuple = ()         # the items (rows or values) it is open by, as given


def word(value: Any) -> str:
    """A status as compared: a row's value, the tools' ``{"untrusted_text": ...}``
    wrapper or plain text, casefolded with ``_`` and ``-`` read as spaces."""
    if isinstance(value, Mapping):
        value = value.get("value", value.get("untrusted_text"))
        if isinstance(value, Mapping):
            value = value.get("untrusted_text")
    if value is None:
        return ""
    return " ".join(str(value).replace("_", " ").replace("-", " ").split()).casefold()


def record_status(values: Mapping[str, Sequence[Any]], *, binding: str | None = None,
                  fulfilled: bool = False) -> Status:
    """One record's own state from its values by property (``READ_PROPS``; each item a
    value, a row with ``value`` or the tools' wrapper). ``binding``: the record's source
    binding (a posted-only BC order is closed); ``fulfilled``: a settled shipment or
    invoice is linked to it."""
    if fulfilled or (binding is not None and binding in posted_bindings()):
        return Status(CLOSED)
    if any(word(v) == "true" for v in values.get(SHIPPED_FLAG, ()) or ()):
        return Status(CLOSED)
    items = [(prop, v) for prop in STATUS_PROPS for v in values.get(prop, ()) or ()
             if word(v)]
    if not items:
        return Status(UNKNOWN, STATUS_UNMAPPED if any(
            word(v) for v in values.get(SAGE_CODE, ()) or ()) else STATUS_MISSING)
    classes = set()
    for prop, value in items:
        said = word(value)
        classes.add(CLOSED if said in CLOSED_WORDS else OPEN if said in OPEN_WORDS[prop]
                    else DRAFT if said in DRAFT_WORDS.get(prop, ()) else UNKNOWN)
    if CLOSED in classes:
        return Status(CLOSED)
    if UNKNOWN in classes:
        return Status(UNKNOWN, STATUS_UNMAPPED)
    if OPEN in classes:
        return Status(OPEN, open_by=tuple(v for p, v in items if word(v) in OPEN_WORDS[p]))
    return Status(DRAFT)


def order_status(records: Iterable[Status]) -> Status:
    """One order from its records' states: closed when any is closed, else unknown when
    any is (its reason; the last in name order when several), else open (every open item)
    when any is, else draft. No record: unknown (``status_missing``)."""
    states = list(records)
    if not states:
        return Status(UNKNOWN, STATUS_MISSING)
    if any(s.cls == CLOSED for s in states):
        return Status(CLOSED)
    unknown = sorted({s.reason or STATUS_UNMAPPED for s in states if s.cls == UNKNOWN})
    if unknown:
        return Status(UNKNOWN, unknown[-1])
    open_by = tuple(item for s in states if s.cls == OPEN for item in s.open_by)
    return Status(OPEN, open_by=open_by) if open_by else Status(DRAFT)


def spellings(prop: str) -> frozenset[str]:
    """The stored spellings of ``prop``'s open words an exact-match filter asks for (the
    Portal writes UPPER_SNAKE codes and the legacy Title-case "Open")."""
    out: set[str] = set()
    for said in OPEN_WORDS.get(prop, ()):
        out |= {said.upper().replace(" ", "_"), said.title(), said, said.upper()}
    return frozenset(out)


# --------------------------------------------------------------------------- required date

def day_of(value: Any) -> date | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def date_defaults(days_by_binding: Mapping[Any, Iterable[date | None]]
                  ) -> dict[Any, frozenset]:
    """Per binding (a source's own population), the days a bulk share of its records'
    required dates hold (``source_defaults.default_days``)."""
    return {b: source_defaults.default_days(days) for b, days in days_by_binding.items()}


def is_requested(day: date | None, *, own: Iterable[Any] = (),
                 defaults: Iterable[date] = ()) -> bool:
    """Whether a required date is a requested one: a readable day that is neither a bulk
    default of its binding nor one of the record's own (creation, order) days."""
    return day is not None and day not in frozenset(defaults) and not source_defaults.own_day(
        day, [day_of(d) if not isinstance(d, date) else d for d in own])


@dataclass(frozen=True)
class Requested:
    day: date | None
    governing: tuple = ()       # the rows that state it (the top precedence lineage)
    reason: str | None = None   # no_required_date, required_date_default,
    #                             required_date_unreadable, required_date_disputed


def requested_date(records: Sequence[tuple[Sequence[Mapping[str, Any]], Iterable[Any],
                                           Iterable[date]]]) -> Requested:
    """An order's requested date over its records, each (its ``required_date`` rows with
    ``value`` and ``lineage``, its own days, its binding's default days): the top
    precedence lineage holding a requested date governs."""
    real: list[Mapping[str, Any]] = []
    default = unreadable = False
    for rows, own, defaults in records:
        own, defaults = list(own), frozenset(defaults)
        for row in rows:
            if row.get("value") in (None, ""):
                continue
            day = day_of(row)
            if day is None:
                unreadable = True
            elif is_requested(day, own=own, defaults=defaults):
                real.append(row)
            else:
                default = True
    if not real:
        return Requested(None, reason="required_date_default" if default else
                         "required_date_unreadable" if unreadable else "no_required_date")
    ranks = precedence()

    def rank(row: Mapping[str, Any]) -> int:
        return ranks.index(row.get("lineage")) if row.get("lineage") in ranks else len(ranks)
    top = min(rank(r) for r in real)
    governing = tuple(r for r in real if rank(r) == top)
    days = {day_of(r) for r in governing}
    if len(days) != 1:
        return Requested(None, governing, "required_date_disputed")
    return Requested(days.pop(), governing)


# --------------------------------------------------------------------------- schema

@functools.lru_cache(maxsize=1)
def _schema() -> tuple[tuple[str, ...], frozenset, tuple[str, ...]]:
    from ontology.schema import load_schema
    from .ontology_tools import identity_link_type

    schema = load_schema()
    twins = tuple(sorted(lid for lid, link in schema.link_types.items()
                         if link.get("from") == TYPE and link.get("to") == TYPE
                         and identity_link_type(link)))
    so = schema.object_types[TYPE]
    posted = frozenset(b["id"] for b in so.get("bindings", ()) if b.get("stands_for"))
    prop = next(p for p in so["properties"] if p["name"] == DATE_PROP)
    ranks = tuple(lin for tier in (prop.get("authority") or {}).get("precedence", ())
                  for lin in tier.get("lineage", ()))
    return twins, posted, ranks


def twin_links() -> tuple[str, ...]:
    """The identity link types between two sales orders (the schema's)."""
    return _schema()[0]


def posted_bindings() -> frozenset:
    """The bindings that stand for an order BC deleted once posted (``stands_for``)."""
    return _schema()[1]


def precedence() -> tuple[str, ...]:
    """``SalesOrder.required_date``'s authority lineages, top first."""
    return _schema()[2]
