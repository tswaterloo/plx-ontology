"""Which records the proactive rules count: test and placeholder records, and inactive or
historic-only orders dropped from findings."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import re
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from . import active_population as active
from . import po_status
from . import test_records as test_records_rule
from . import proactive_gaps as pg

from .proactive_engine_config import CONFIRMING, SETTLED
from .proactive_engine_findings import _day
from .proactive_engine_purchasing import _ATTESTS, _historic_orders, _ORDER, _po_source
from .proactive_engine_sales import _customer_orders, _inactive_orders
from .proactive_engine_inbox import _uncount

if TYPE_CHECKING:
    from .proactive_engine import _Store

# The one test-record rule (``operating.test_records``): marks, names and numbers that say a
# record is a test or placeholder. The engine reads the marks on the documents an order owns
# (BC invoices, receipts, sales orders' external numbers, order lines) and the names on the
# order and its party; a record carrying one, or the order it belongs to, is no claim.
TEST_MARK = test_records_rule.TEST_MARK
# Where a test mark is read: (record type, property).
TEST_MARK_PROPS = (("PurchaseInvoice", "vendor_invoice_number"),
                   ("PurchaseOrderLine", "description"),
                   ("SalesOrder", "customer_po"),
                   ("SalesInvoice", "external_document_number"),
                   ("SalesInvoice", "customer_po"),
                   ("Receipt", "vendor_invoice_number"),
                   ("Product", "name"))
# An order's own numbers and names the name rules read, and the links to its party.
TEST_NAME_PROPS = ("po_number", "fm_po_number", "order_number")
TEST_PARTY_LINKS = ("PurchaseOrder.ordered_from", "SalesOrder.ordered_by")
# The links from a marked record to the order it belongs to (each system's own stored
# order reference) and the identity links joining an order's copies.
TEST_ORDER_LINKS = (po_status.LINE_OF, "PurchaseInvoice.invoices", "PurchaseInvoice.names_order",
                    "Receipt.receives")
TEST_TWIN_LINKS = (*po_status.TWIN_LINKS, "SalesOrder.same_record_as")
# A carried-over draft: an order whose Portal copy is a draft ordering nothing, last changed
# more than this many days before the run's day (an old draft carried into BC at go-live).
OLD_DRAFT_DAYS = 365
# Number series: a document number's prefix up to its last non-digit and its trailing
# digits, read as one running number of a fixed width (BC ``106001``: series ("", 6),
# number 106001; ``PO100409``: series ("PO", 6)). A number ending in 001 is a series' first
# only when nothing of its series is numbered below it but its own predecessor (the 000 a
# series may start at): past a thousand, ``PO101001`` follows ``PO100999`` and is an
# ordinary order. A first is a go-live first only on the BC go-live day the deployment
# states (``test_records.go_live_day``): a new series begun later (``PO1001``, ``PO1002``
# with lines on an ordinary day) is ordinary business (AUTOMATION-015).
_SERIES = re.compile(r"^(.*?)(\d+)$")


def _iso(day: Any) -> str | None:
    return day.isoformat() if isinstance(day, date) else (str(day)[:10] if day else None)


def series_firsts(members: Iterable[tuple[Any, str, Any]], go_live: date | None
                  ) -> set[Any]:
    """The go-live first documents of BC number series: from (id, number, day) members,
    each series' first number (running number ...001, nothing of the series numbered below
    it but its predecessor) dated the go-live day ``go_live``, and the series' documents
    dated that same day. No go-live day marks nothing; a series whose first has no one day,
    or with two firsts, marks nothing."""
    if go_live is None:
        return set()
    series: dict[tuple[str, int], list[tuple[Any, int, Any]]] = defaultdict(list)
    for rid, number, day in members:
        m = _SERIES.match(str(number or "").strip())
        if m:
            series[(m.group(1), len(m.group(2)))].append((rid, int(m.group(2)), _iso(day)))
    marked: set[Any] = set()
    for rows in series.values():
        first = [r for r in rows if r[1] % 1000 == 1
                 and not any(o[1] < r[1] - 1 for o in rows)]
        if len(first) == 1 and first[0][2] == go_live.isoformat():
            marked |= {rid for rid, _n, day in rows if day == first[0][2]}
    return marked


def test_records(st: _Store, as_of: date) -> dict[str, str]:
    """object id -> why it is a test or carried-over record (``mark``, ``go_live_first``,
    ``old_empty_draft``), every copy of the order included: the one filter every detector
    and every page shares (``_drop_test_records``; the operator API reads it too)."""
    # a population (number series across records): read whole in an incremental pass
    return st.population(f"test_records:{as_of.isoformat()}",
                         lambda: _find_test_records(st, as_of))


def _find_test_records(st: _Store, as_of: date) -> dict[str, str]:
    found: dict[str, str] = {}
    for kind, prop in TEST_MARK_PROPS:
        for oid, entry in st.values((kind,), (prop,)).items():
            if any(TEST_MARK.search(str(r["value"] or "")) for r in entry["props"].get(prop, [])):
                found.setdefault(oid, "mark")
    links = st.links(TEST_ORDER_LINKS, (*CONFIRMING, "stored_by_text_join"))
    for link in links:
        if link["from_object"] in found:
            found.setdefault(link["to_object"], found[link["from_object"]])
    pos = st.values(("PurchaseOrder",), ("po_number", "order_date", "status",
                                          "portal_updated_at"))
    # an order with a line that orders something (a line of nothing is an empty line)
    line_links = st.links((po_status.LINE_OF,), CONFIRMING)
    ordered = st.values(("PurchaseOrderLine",), ("ordered_quantity",), objects=sorted(
        {l["from_object"] for l in line_links}))
    lines = {l["to_object"] for l in line_links if any(
        (pg._decimal(r["value"]) or 0) > 0
        for r in (ordered.get(l["from_object"]) or {"props": {}})["props"].get(
            "ordered_quantity", []))}
    # go-live first documents: in each BC number series, the first number the series
    # issued (its running number 001) on the stated go-live day and the documents of the
    # series dated that same day
    twins = st.links(TEST_TWIN_LINKS, (*SETTLED, "corroborated"))
    twinned = {end for link in twins for end in (link["from_object"], link["to_object"])}
    members: list[tuple[str, str, date | None]] = []
    for oid, entry in pos.items():
        # a BC-native document (no other system holds a copy): a copy's number is the
        # other system's series, whose first document is a real order
        if not oid.startswith("PurchaseOrder:bc.") or oid in twinned:
            continue
        days = {_day(r["value"]) for r in entry["props"].get("order_date", [])}
        for row in entry["props"].get("po_number", []):
            members.append((oid, str(row["value"] or ""),
                            next(iter(days)) if len(days) == 1 else None))
    for oid in sorted(series_firsts(members, test_records_rule.go_live_day())):
        found.setdefault(oid, "go_live_first")
    # old empty drafts: a Portal draft ordering nothing, untouched for a year
    for oid, entry in pos.items():
        props = entry["props"]
        kinds = {po_status.header_class(r["value"]) for r in props.get("status", [])}
        changed = [_day(r["value"]) for r in props.get("portal_updated_at", [])]
        if kinds == {po_status.NOT_ISSUED} and oid not in lines and changed and all(
                d is not None and (as_of - d).days > OLD_DRAFT_DAYS for d in changed):
            found.setdefault(oid, "old_empty_draft")
    # names and numbers that say so (``operating.test_records.is_test``): an order's own
    # number or name (a "(DO NOT USE)" marker, a TEST name, a UAT- prefixed number, the
    # deployment's own list named by ``PLX_APP_TEST_RECORDS``), or its supplier's or
    # customer's TEST name (a party's "(DO NOT USE)" marks a retired card, never a test)
    named = test_records_rule.listed(None)
    parties = {oid: [str(r["value"]) for r in entry["props"].get("name", []) if r["value"]]
               for oid, entry in st.values(("Supplier", "Customer"), ("name",)).items()}
    party_of: dict[str, list[str]] = defaultdict(list)
    for link in st.links(TEST_PARTY_LINKS, (*SETTLED, "corroborated")):
        party_of[link["from_object"]] += parties.get(link["to_object"], [])
    orders = st.values(("PurchaseOrder", "SalesOrder"), TEST_NAME_PROPS)
    for oid in sorted({*orders, *party_of}):
        entry = orders.get(oid) or {"props": {}}
        texts = [str(r["value"]) for p in TEST_NAME_PROPS for r in entry["props"].get(p, [])
                 if r["value"]]
        if test_records_rule.is_test(texts, named, party_of.get(oid, ())):
            found.setdefault(oid, "name")
    # every copy of a test order is the same test order
    for _ in range(2):
        for link in twins:
            a, b = link["from_object"], link["to_object"]
            if a in found and b not in found:
                found[b] = found[a]
            elif b in found and a not in found:
                found[a] = found[b]
    return found


# The rules whose claim is about a done order's own trouble keep closed orders (never
# historic ones) in ``_drop_inactive_orders``; every other rule, a new one included, loses
# them. Each says why.
CLOSED_ORDER_RULES = {
    "po_receipt_quantity_mismatch": "over-receipt, or short on an order recorded as fully "
                                    "received: closed by definition",
    "invoice_quantity_mismatch": "an invoice billing another quantity than its order",
    "shipped_not_invoiced": "a shipped order no invoice bills: shipping closes it",
    "duplicate_customer_order": "an order keyed twice: either copy may be shipped",
    "twin_disagreement": "one copy closed and the other open is the disagreement",
    "negative_margin": "margins are judged on invoiced orders",
    "margin_erosion": "margins are judged on invoiced orders",
    "return_requests": "goods come back after the order shipped",
}
# Gap kinds whose claim is that one copy is closed and another open (the late rule's
# ``systems_disagree``): kept for a closed order whatever rule raised them (hand-check
# 2026-11-09: 4 of 15 dropped late-rule gaps were these); a receipt reconciliation is the
# same claim about a received (closed) order whose other copy still reads open
CLOSED_ORDER_KINDS = frozenset({"systems_disagree", "twins_disagree",
                                "receipt_reconciliation"})
ORDER_TYPES = ("SalesOrder", "PurchaseOrder")
ACTIVE_UNREADABLE = "active_population_unreadable"
ACTIVE_READER = "active_population"   # the filter's reads, attributed to no rule


def _po_classes(st: _Store, as_of: date) -> dict[str, str]:
    """Purchase order record -> its order's class by the one PO status definition
    (``po_status.read``), read once per run (``key_fields_missing`` reads it too)."""
    return {m: cls for m, (cls, _dated) in _po_reads(st, as_of).items()}


def _po_reads(st: _Store, as_of: date) -> dict[str, tuple[str, bool]]:
    """Purchase order record -> (its order's class, whether the order holds a due date: a
    requested-by, supplier or lead-time date, readable or not), one ``po_status.read`` per
    order, once per run."""
    def read() -> dict[str, tuple[str, bool]]:
        out = {}
        for top, members in _po_source(st).clusters.items():
            found = po_status.read(_po_source(st), top, as_of)
            need = found.need
            dated = need is not None and bool(need.lo or need.hi or need.unparseable)
            out.update({m: (found.status.cls, dated) for m in members})
        return out
    return st.memo("po_reads", read)


def inactive_records(st: _Store, as_of: date) -> dict[str, str]:
    """The one active-population predicate (``operating.active_population``) of every
    record-level rule: sales and purchase order record -> ``closed`` (its order is closed
    by its type's one status definition across every copy) or ``historic`` (open on paper,
    no business date within ``active.ACTIVE_DAYS``: a legacy back-order); an active record
    is absent. A sales order's verdict is ``_inactive_orders``'; a purchase order's is
    ``po_status`` with ``_historic_orders``, and an open purchase order holding a due date
    is never stale by age (its lateness is judged on that date; only an undated order is
    stale, as the late rule's own ``historic`` count says)."""
    def read() -> dict[str, str]:
        out = dict(_inactive_orders(st, as_of))
        reads = _po_reads(st, as_of)
        source = _po_source(st)
        top_of = {m: top for top, members in source.clusters.items() for m in members}
        live = {}
        for record, (cls, dated) in reads.items():
            if cls == po_status.CLOSED:
                out[record] = active.CLOSED
            elif not dated:
                live[record] = top_of.get(record, record)
        stale = _historic_orders(st, as_of, live)
        out.update({m: active.HISTORIC for m, top in live.items() if top in stale})
        return out
    return st.memo(f"inactive_records:{as_of.isoformat()}", read)


def _historic_only(st: _Store, as_of: date, records: Iterable[str]) -> set[str]:
    """Of ``records`` (orders closed by their status), those with no business date within
    the active window: a closed-order rule still leaves these out."""
    wanted = sorted(set(records))
    if not wanted:
        return set()
    so = [r for r in wanted if r.startswith("SalesOrder:")]
    po = [r for r in wanted if r.startswith("PurchaseOrder:")]
    out: set[str] = set()
    if po:
        top_of = {m: top for top, members in _po_source(st).clusters.items()
                  for m in members}
        order_of = {r: top_of.get(r, r) for r in po}
        stale = _historic_orders(st, as_of, order_of)
        out |= {r for r, top in order_of.items() if top in stale}
    if so:
        _found, verdicts = _customer_orders(st)
        for verdict in verdicts:
            hit = set(verdict.group) & set(so)
            # judged on its dates alone (its status already closed it)
            if hit and active.inactive_reason("open", as_of=as_of, dates=verdict.dates) \
                    == active.HISTORIC:
                out |= hit
    return out


def _gap_orders(gap: Mapping[str, Any]) -> list[str]:
    """The order records a gap is about: its subject when an order, and the order its
    follow-up names."""
    out = []
    if gap.get("object_type") in ORDER_TYPES:
        out.append(gap.get("object_id"))
    follow = gap.get("follow_up") or {}
    for order in ((follow.get(_ORDER) or {}).get("key"), follow.get(_ATTESTS)):
        if isinstance(order, str) and order.split(":", 1)[0] in ORDER_TYPES:
            out.append(order)
    return [o for o in out if isinstance(o, str)]


def _drop_inactive_orders(st: _Store, results: dict[str, dict[str, Any]], as_of: date,
                          inactive: Callable[[], Mapping[str, str]] | None = None) -> None:
    """Remove every gap about a closed or historic order (``inactive_records``) from every
    rule's result, counted ``closed_order``/``historic_order``; a ``CLOSED_ORDER_RULES``
    rule loses only the historic ones. Fails closed: when the predicate cannot be read,
    every gap about an order is withheld and its rule is ``failed``
    (``active_population_unreadable``), so nothing it found resolves or shows unjudged."""
    if not any(_gap_orders(g) for r in results.values() for g in r.get("gaps") or ()):
        return
    # the populations it reads are no rule's own (``population``): reading them here never
    # turns an incremental pass into a whole one
    st.judging(ACTIVE_READER)
    try:
        verdicts = (inactive or (lambda: inactive_records(st, as_of)))()
        closed_kept = {o for name, r in results.items() for g in r.get("gaps") or ()
                       if name in CLOSED_ORDER_RULES or g.get("kind") in CLOSED_ORDER_KINDS
                       for o in _gap_orders(g)
                       if verdicts.get(o) == active.CLOSED}
        historic_closed = _historic_only(st, as_of, closed_kept) if closed_kept else set()
    except Exception:   # noqa: BLE001 - an unreadable predicate never lets a gap through
        for result in results.values():
            kept = [g for g in result.get("gaps") or () if not _gap_orders(g)]
            dropped = len(result.get("gaps") or ()) - len(kept)
            if dropped:
                result["gaps"] = kept
                result["status"] = "failed"
                result["reasons"] = sorted({*result.get("reasons", ()), ACTIVE_UNREADABLE})
        return
    finally:
        st.judging(None)
    for name, result in results.items():
        kept, dropped = [], Counter()
        gone: dict[str, str] = {}
        for gap in result.get("gaps") or ():
            why = None
            for order in _gap_orders(gap):
                said = verdicts.get(order)
                if said == active.CLOSED and (name in CLOSED_ORDER_RULES
                                              or gap.get("kind") in CLOSED_ORDER_KINDS):
                    said = active.HISTORIC if order in historic_closed else None
                if said is not None:
                    why = said
                    break
            if why is None:
                kept.append(gap)
            else:
                dropped[why + "_order"] += 1
                order = ((gap.get("follow_up") or {}).get(_ORDER) or {}).get("key")
                if isinstance(order, str):
                    gone[order] = why
        if dropped:
            result["gaps"] = kept
            skipped = result.setdefault("not_evaluated", {})
            if isinstance(skipped, dict):
                for key, n in dropped.items():
                    skipped[key] = skipped.get(key, 0) + n
            _uncount(result, kept, gone)
