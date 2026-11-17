"""Customer-order rules of the proactive engine: open customer orders, their work orders and
shipping evidence, and the late customer order rule."""

from __future__ import annotations

import dataclasses
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
import json
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from ontology.units import unit_token

from .copy_gate import say_date
from . import active_population as active
from . import po_status
from . import proactive_gaps as pg
from . import sales_order_status as so_status

from .proactive_engine_config import CONFIRMING, SETTLED
from .proactive_engine_findings import (
    _cites, _clusters, _coverage, _day, _gap, _markings, _parts, _result, _Skips,
)

if TYPE_CHECKING:
    from .proactive_engine_config import Cite
    from .proactive_engine_findings import Part
    from .proactive_engine import _Store

LATE_SO = "late_customer_order"
SO_NUMBERS = ("order_number", "fm_erp_code")
SO_PROPS = (*so_status.READ_PROPS, so_status.DATE_PROP, *so_status.OWN_DAYS, *SO_NUMBERS,
            *active.EDIT_TIME_PROPS)
# the dates that make an order active (``operating.active_population``): its due, order,
# creation and edit days, each bulk import day of its source left out
SO_ACTIVITY = (so_status.DATE_PROP, *so_status.OWN_DAYS, *active.EDIT_TIME_PROPS)
STALE_SO = "stale_order_record"            # open past its date, nothing touched it lately
PROBABLE_SO = "order_probably_fulfilled"   # open, but every work order for it is done
# the records whose absence the verdict rests on (a shipment or invoice not linked, a
# closing status not held): a reader must be granted every audience they sit in
SO_ABSENCE_TYPES = (so_status.TYPE, "Shipment", "SalesInvoice")
# A shipping document naming an order: evidence the goods left (a packing slip is cut when
# the order is packed to ship). A mention link, so only a confirming tier counts, and the
# slip must state its own ship date (``SHIP_DATE``) to stand as shipped evidence.
SHIP_LINKS = ("PackingSlip.for_sales_order",)
SHIP_DATE = "ship_date"


@dataclasses.dataclass
class _OrderVerdict:
    """One customer order (a settled identity cluster of ``SalesOrder`` records) as the one
    sales-order definition reads it: ``cls`` (``open``, ``closed``, ``draft`` or
    ``unknown``), the skip ``reason`` and its cause (``read``) when unknown or undated, the
    requested date (``wanted``) of an open order and the identity links' markings."""
    top: str
    group: list
    cls: str
    read: list
    link_marks: list
    order: Any = None
    wanted: Any = None
    reason: str | None = None
    dates: list = dataclasses.field(default_factory=list)   # business dates (activity)
    work: tuple = ()   # (work order id, closed) per work order made for its lines
    shipped: tuple = ()   # (shipping document, link markings) naming it (``SHIP_LINKS``)


def _customer_orders(st: _Store) -> tuple[dict[str, Any], list[_OrderVerdict]]:
    """Every customer order of the store by the one sales-order definition
    (``operating.sales_order_status``), joined by settled identity links only: closed when
    any record is closed or a settled shipment or invoice is linked; a lead shipment or
    invoice, a closed lead twin, an unknown status or no line on any record leaves it
    ``unknown`` (with its reason), never open; an open order carries its requested date
    (``wanted``: never a bulk default of its source nor the record's own day). Read once per
    run (``memo``): the late-order rule and the stock-out rule judge the same orders."""
    return st.memo("customer_orders", lambda: _read_customer_orders(st))


def _read_customer_orders(st: _Store) -> tuple[dict[str, Any], list[_OrderVerdict]]:
    found = st.values((so_status.TYPE,), SO_PROPS)
    binding = {oid: po_status.binding_of(oid) for oid in found}
    twins = st.links(so_status.twin_links())
    settled = [l for l in twins if l["tier"] in SETTLED
               and l["from_object"] in found and l["to_object"] in found]
    root = _clusters(((l["from_object"], l["to_object"]) for l in settled), found)
    members: dict[str, list[str]] = defaultdict(list)
    for oid in sorted(found):
        members[root[oid]].append(oid)
    marks: dict[str, list[str]] = defaultdict(list)
    for link in settled:
        marks[root[link["from_object"]]].append(link["markings"])
    fulfilled: dict[str, bool] = {}   # record -> a settled (True) or lead shipment/invoice
    for link in st.links(so_status.FULFILLED_LINKS):
        if link["to_object"] in found:
            fulfilled[link["to_object"]] = fulfilled.get(link["to_object"], False) \
                or link["tier"] in SETTLED
    line_links = [link for link in st.links([so_status.LINE_OF])
                  if link["to_object"] in found]
    lined = {link["to_object"] for link in line_links}   # records holding at least one line
    work = _work_orders(st, {link["from_object"]: link["to_object"] for link in line_links
                             if link["tier"] in SETTLED})
    shipping: dict[str, set[tuple[str, str]]] = defaultdict(set)   # record -> its slips
    for link in st.links(SHIP_LINKS):
        if link["tier"] in CONFIRMING and link["to_object"] in found:
            shipping[link["to_object"]].add((link["from_object"], link["markings"]))
    lead_twins: dict[str, set[tuple[str, str]]] = defaultdict(set)   # order -> (twin, link)
    for link in twins:
        ends = (link["from_object"], link["to_object"])
        if link["tier"] not in SETTLED and all(e in found for e in ends) \
                and root[ends[0]] != root[ends[1]]:
            lead_twins[root[ends[0]]].add((ends[1], link["markings"]))
            lead_twins[root[ends[1]]].add((ends[0], link["markings"]))
    scoped = st.scoped

    def every_default() -> dict[Any, frozenset]:
        # the default required dates of each binding: a population (all its orders), a
        # bulk day erased only with provenance (the holders' own creation/import day)
        held = st.values((so_status.TYPE,), (so_status.DATE_PROP, *so_status.OWN_DAYS)) \
            if scoped else found
        return so_status.date_defaults(_grouped(
            (po_status.binding_of(oid),
             (_day(r.get("value")), [o.get("value") for n in so_status.OWN_DAYS
                                     for o in entry["props"].get(n, ())]))
            for oid, entry in held.items()
            for r in entry["props"].get(so_status.DATE_PROP, ())))
    defaults = st.population("so_default_dates", every_default)

    def every_import_day() -> dict[Any, frozenset]:
        # the days a bulk load wrote, per binding and property: never activity
        held = st.values((so_status.TYPE,), SO_ACTIVITY) if scoped else found
        return active.import_days(
            ((po_status.binding_of(oid), n), r.get("value")) for oid, entry in held.items()
            for n in SO_ACTIVITY for r in entry["props"].get(n, ()))
    imported = st.population("so_import_days", every_import_day)
    state = {oid: so_status.record_status(entry["props"], binding=binding[oid],
                                          fulfilled=fulfilled.get(oid, False))
             for oid, entry in found.items()}
    out: list[_OrderVerdict] = []
    for top, group in sorted(members.items()):
        link_marks = marks.get(top, [])
        read: list[Part] = [(m, _markings(*link_marks)) for m in group] + _parts(
            [r for m in group for n in (*so_status.READ_PROPS, so_status.DATE_PROP)
             for r in found[m]["props"].get(n, ())], *link_marks)
        verdict = _OrderVerdict(top, group, "unknown", read, link_marks)
        verdict.dates = sorted(active.business_days(
            ({"value": r.get("value"), "binding": binding[m], "property": n}
             for m in group for n in SO_ACTIVITY for r in found[m]["props"].get(n, ())),
            imported))
        verdict.work = tuple(sorted({w for m in group for w in work.get(m, ())}))
        verdict.shipped = tuple(sorted({s for m in group for s in shipping.get(m, ())}))
        out.append(verdict)
        order = verdict.order = so_status.order_status(state[m] for m in group)

        def wanted(group: Sequence[str] = group) -> Any:
            return so_status.requested_date([
                (found[m]["props"].get(so_status.DATE_PROP, ()),
                 [r.get("value") for n in so_status.OWN_DAYS
                  for r in found[m]["props"].get(n, ())],
                 defaults.get(binding[m], ())) for m in group])
        if order.cls == so_status.CLOSED:
            verdict.cls = "closed"
            # its promise too: the promise-date rule judges a shipped order against it
            verdict.wanted = wanted()
            continue
        if any(fulfilled.get(m) is False for m in group):   # a lead shipment or invoice only
            verdict.reason = "fulfilment_unsettled"
            continue
        leads = sorted(lead_twins.get(top, ()))
        if any(state[t].cls == so_status.CLOSED or t in fulfilled for t, _m in leads):
            # a lead (a number match) may be this order's closed record: never judged open
            verdict.reason = "closed_in_unsettled_twin"
            verdict.read = [*read, *((t, _markings(m, *link_marks)) for t, m in leads)]
            continue
        if order.cls == so_status.UNKNOWN:
            verdict.reason = order.reason
            continue
        if order.cls == so_status.DRAFT:
            verdict.cls = "draft"
            continue
        if not any(m in lined for m in group):
            # no record of the order holds a line: no goods are stated on it (a header
            # copied without its lines, or an empty shell), so it is not judged
            verdict.reason = so_status.NO_LINES
            continue
        verdict.cls = "open"
        verdict.wanted = wanted()
    return found, out


# The work orders made for an order's lines (an assembly order for the line, or allocated
# to it), each closed when its own status says done: every one done is evidence the goods
# were made for the order, never proof they shipped (``PROBABLE_SO``).
WORK_LINE_LINKS = ("AssemblyOrder.for_order_line",)
WORK_ALLOCATION = ("AssemblyOrderAllocation.allocates", "AssemblyOrderAllocation.for_order_line")
WORK_TYPE = "AssemblyOrder"


def _work_orders(st: _Store, line_order: Mapping[str, str]) -> dict[str, list[tuple]]:
    """Sales order record -> its lines' work orders as ``(work order, closed)``, joined
    by settled links only (``WORK_LINE_LINKS`` and the allocations' two links)."""
    if not line_order:
        return {}
    pairs: set[tuple[str, str]] = set()   # (work order, order line)
    for link in st.links(WORK_LINE_LINKS):
        if link["tier"] in SETTLED and link["to_object"] in line_order:
            pairs.add((link["from_object"], link["to_object"]))
    allocates: dict[str, set[str]] = defaultdict(set)
    lines_of: dict[str, set[str]] = defaultdict(set)
    for link in st.links(WORK_ALLOCATION):
        if link["tier"] not in SETTLED:
            continue
        if link["link_type"] == WORK_ALLOCATION[0]:
            allocates[link["from_object"]].add(link["to_object"])
        elif link["to_object"] in line_order:
            lines_of[link["from_object"]].add(link["to_object"])
    for allocation, lines in lines_of.items():
        pairs |= {(w, line) for w in allocates.get(allocation, ()) for line in lines}
    if not pairs:
        return {}
    status = st.values((WORK_TYPE,), ("status",), objects=sorted({w for w, _ in pairs}))
    out: dict[str, list[tuple]] = defaultdict(list)
    for w, line in sorted(pairs):
        words = {so_status.word(r) for r in (status.get(w) or {"props": {}})["props"].get(
            "status", ())} - {""}
        out[line_order[line]].append((w, bool(words) and words <= so_status.CLOSED_WORDS))
    return out


def _inactive_orders(st: _Store, as_of: date) -> dict[str, str]:
    """Every sales order record outside the active population (``active_population``):
    record -> ``closed`` (its order is closed across every copy and linked system) or
    ``historic`` (no business date of it within ``ACTIVE_DAYS``). The record-level rules
    about customer orders leave these out, counted, never listed as current work."""
    def read() -> dict[str, str]:
        _found, verdicts = _customer_orders(st)
        out = {}
        for verdict in verdicts:
            why = active.inactive_reason(
                active.CLOSED if verdict.cls == "closed" else verdict.cls, as_of=as_of,
                dates=verdict.dates)
            if why is not None:
                out.update({m: why for m in verdict.group})
        return out
    return st.memo(f"inactive_orders:{as_of.isoformat()}", read)


def _absence_audiences(st: _Store, types: Sequence[str], none: str) -> list[str]:
    """Every audience a record of ``types`` sits in: a verdict resting on what those
    records do not hold needs a grant on each (``REQUIRES_GRANT``); none stored: ``none``."""
    return sorted({r[0] for r in st.conn.execute(
        "SELECT DISTINCT audience_key FROM objects WHERE object_type IN "
        "(SELECT value FROM json_each(?))", (json.dumps(list(types)),))}) or [none]


def _late_customer_order(st: _Store, as_of: date) -> dict[str, Any]:
    """A customer order open past its own required date by the one sales-order definition
    (``_customer_orders``): ONE gap per order across its records. Late when its
    requested date is before the business day ``as_of``: cited by the date and by each
    status value it is open by (each its own group), and shown only to a reader granted
    every audience the absence of a closure rests on (``REQUIRES_GRANT``)."""
    name = LATE_SO
    found, verdicts = _customer_orders(st)
    requires = _absence_audiences(st, SO_ABSENCE_TYPES, "sales_order:none_stored")
    skipped = _Skips()
    classes: Counter = Counter()
    counts: Counter = Counter()
    gaps = []
    for verdict in verdicts:
        group, link_marks, read = verdict.group, verdict.link_marks, verdict.read

        def rows(*names: str) -> list[dict[str, Any]]:
            return [r for m in group for n in names for r in found[m]["props"].get(n, ())]
        classes[verdict.cls] += 1
        if verdict.cls != "open":
            if verdict.reason is not None:
                skipped.note(verdict.reason, group, read)
            continue
        order, wanted = verdict.order, verdict.wanted
        if wanted.day is None:
            if wanted.reason != "required_date_disputed":
                counts["undated"] += 1
            skipped.note(wanted.reason, group, read)
            continue
        required = wanted.day
        counts["evaluated"] += 1
        if required >= as_of:
            continue
        late = (as_of - required).days
        work = verdict.work
        if work and all(done for _w, done in work):
            # every work order made for its lines is done: the goods were made; with a
            # shipping document naming it that states its ship date, it has probably shipped
            # without a closing record (said apart, ranked low, never late). Made with no
            # shipping evidence is not said either way (11-13 judging: 15 of 15 such
            # notes could not be told shipped or not): counted, never listed
            slips = _ship_evidence(st, verdict, as_of)
            if not slips:
                counts["made_no_ship_evidence"] += 1
                continue
            counts["probably_fulfilled"] += 1
            gaps.append(_required(requires, _gap(
                PROBABLE_SO, name, min(r["object_id"] for r in wanted.governing),
                so_status.TYPE, "probably_fulfilled",
                summary=f"A customer order still reads open {late} day"
                        f"{'' if late == 1 else 's'} past its required date "
                        f"{required.isoformat()}, but every work order made for it is done "
                        f"and a packing slip naming it shows it shipped {slips[0]}: its "
                        "status was probably never closed",
                missing="Did this order ship? If so, its status was never closed.",
                why="Its status says open and no shipment or invoice is linked, but every "
                    "assembly order for its lines is complete and a packing slip naming the "
                    "order states its ship date, so the open status is probably stale "
                    "rather than the order late.",
                priority="low", about=(so_status.TYPE, so_status.DATE_PROP),
                groups=[_cites(wanted.governing, *link_marks),
                        *[[cite] for cite in _cites(order.open_by, *link_marks)],
                        slips[1]],
                extra=_cites(rows(*SO_NUMBERS), *link_marks),
                event={"days": late, "required": required.isoformat()})))
            continue
        if active.inactive_reason(verdict.cls, as_of=as_of, dates=verdict.dates) \
                == active.HISTORIC:
            # open on paper, but nothing about it moved in the active window: a stale
            # record to close or confirm, not a late order
            counts["stale"] += 1
            gaps.append(_required(requires, _gap(
                STALE_SO, name, min(r["object_id"] for r in wanted.governing),
                so_status.TYPE, "stale_record",
                summary=f"A customer order reads open {late} days past its required date "
                        f"{required.isoformat()}, and no record of it changed in the last "
                        f"{active.ACTIVE_DAYS} days: a stale record, not a current late order",
                missing="Is this order still open, or should its record be closed?",
                why="Its status says open and nothing closes it, but none of its records "
                    f"was created, edited or due in the last {active.ACTIVE_DAYS} days "
                    "(import dates are not counted), so it is listed as a stale record.",
                priority="low", about=(so_status.TYPE, so_status.DATE_PROP),
                groups=[_cites(wanted.governing, *link_marks),
                        *[[cite] for cite in _cites(order.open_by, *link_marks)]],
                extra=_cites(rows(*SO_NUMBERS), *link_marks),
                event={"days": late, "required": required.isoformat()})))
            continue
        counts["late"] += 1
        by_system: dict[str, set[str]] = defaultdict(set)
        for row in order.open_by:
            by_system[so_status.SYSTEM_OF[row["property"]]].add(str(row["value"]).strip())
        said = "; ".join(f"{system}: {', '.join(sorted(words))}"
                         for system, words in sorted(by_system.items()))
        # BIZ-007: a required date on the day the order was entered is kept and judged,
        # and said with its doubt (a same-day order, or a form that filled in the day)
        doubt = wanted.reason == so_status.OWN_DAY
        if doubt:
            counts["own_day_doubt"] += 1
        made = _gap(
            LATE_SO, name, min(r["object_id"] for r in wanted.governing), so_status.TYPE,
            "overdue",
            summary=f"A customer order is open {late} day{'' if late == 1 else 's'} past its "
                    f"required date {required.isoformat()} (open in {said})" +
                    ("; that date is the day the order was entered, so it may be a default "
                     "rather than the customer's date" if doubt else ""),
            missing=f"Has the customer order required by {required.isoformat()} shipped, and "
                    "if not, what date can the customer now expect?" +
                    (" Was that date the customer's, or filled in when the order was "
                     "entered?" if doubt else ""),
            why="Every record of the order that states a status says it is open, none is "
                "closed, shipped or invoiced, and no shipment or invoice is linked to it, "
                "while its required date has passed." +
                (" Its required date equals the day the order was entered: a same-day order "
                 "or a form default, unconfirmed." if doubt else ""),
            priority="low" if doubt else "medium", about=(so_status.TYPE, so_status.DATE_PROP),
            groups=[_cites(wanted.governing, *link_marks),
                    *[[cite] for cite in _cites(order.open_by, *link_marks)]],
            extra=_cites(rows(*SO_NUMBERS), *link_marks),
            event={"days": late, "required": required.isoformat()})
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    result = _result(gaps, not_evaluated=skipped)
    # every order is closed, a draft, judged on its date, or counted unjudged by reason
    judged = classes["closed"] + classes["draft"] + counts["evaluated"]
    result["coverage"] = _coverage(len(verdicts), judged, dict(skipped), unit="sales_orders")
    result["counts"] = {"orders": len(verdicts),
                        "classes": {c: classes.get(c, 0)
                                    for c in ("open", "closed", "draft", "unknown")},
                        **{k: counts.get(k, 0) for k in ("evaluated", "late", "undated",
                                                         "stale", "probably_fulfilled",
                                                         "made_no_ship_evidence",
                                                         "own_day_doubt")}}
    return result


def _ship_evidence(st: _Store, verdict: _OrderVerdict, as_of: date
                   ) -> tuple[str, list[Cite]] | None:
    """(the newest ship day said, the cited ship dates) of the shipping documents naming
    the order (``verdict.shipped``) that state a ship date on or before ``as_of``, each
    cited through its link's markings and the order's identity links; None when none
    does."""
    if not verdict.shipped:
        return None
    marks = dict(verdict.shipped)
    found = st.values(("PackingSlip",), (SHIP_DATE,), objects=sorted(marks))
    rows, days = [], []
    for slip, entry in sorted(found.items()):
        for row in entry["props"].get(SHIP_DATE, ()):
            day = _day(row.get("value"))
            if day is not None and day <= as_of:
                rows.append((row, marks.get(slip)))
                days.append(day)
    if not rows:
        return None
    cited = [c for row, mark in rows for c in _cites([row], mark, *verdict.link_marks)]
    return say_date(max(days)), cited


def _required(requires: Sequence[str], gap: dict[str, Any] | None) -> dict[str, Any] | None:
    """``gap`` shown only to a reader granted every audience in ``requires``."""
    if gap is not None:
        gap["requires"] = list(requires)
    return gap


def _grouped(pairs: Iterable[tuple[Any, Any]]) -> dict[Any, list[Any]]:
    out: dict[Any, list[Any]] = defaultdict(list)
    for key, value in pairs:
        out[key].append(value)
    return out


def _quantity(rows: Sequence[Mapping[str, Any]]) -> tuple[Decimal, str] | None | bool:
    """The one (number, unit) the rows state; None: none; False: they disagree."""
    found = set()
    for row in rows:
        number, unit = pg._decimal(row["value"]), unit_token(row["unit"])
        if number is None or unit is None:
            return False
        found.add((number, unit))
    if not found:
        return None
    return found.pop() if len(found) == 1 else False
