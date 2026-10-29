"""The one purchase-order status definition and the one purchase-order reader every
consumer uses: the proactive engine, the ``po_delivery_window`` process graph
(``open_when``, the PO stage predicate and lateness), the packaging eval check and the
vocabulary live check.

A PO's status word is its source's raw header text, never the Portal's derived code: the
Portal ``app_purchase_order.status`` column holds the FileMaker words (live capture
2026-10-20: Cancel, Complete, DRAFT, Pending, B.Order, Revised, Generated) and the Portal
derives its fulfillment code from the header and the summed line quantities
(plx-customer-portal portal/src/lib/mrp/po-fulfillment-status.ts derivePoFulfillmentCode;
@d39d91b open-po-lines/route.ts:38-46,302-312 and po-queries.ts:196-206). So:

* a cancelled word, and ``closed`` (open-po-lines/route.ts:46 @5afa0a20
  ``CANCELLED_STATUSES``), is **closed** ``CANCELLED``; DRAFT is **draft** (not issued);
* ``Complete`` (the Portal header and FileMaker alike) is **closed** whatever was received
  (Ross): received in full it is ``RECEIVED``, else ``COMPLETE`` with
  the outstanding quantity as its ``canceled_remainder`` (unknown when the quantities are
  not readable), never open;
* any other mapped word (``ISSUED_WORDS``) is **open** while the summed received quantity
  is below the summed ordered one (``SUBMITTED`` nothing received, ``PARTIAL`` some) and
  **closed** (``RECEIVED``) once it covers it, within the Portal's ``EPS``;
* the gold received quantity of an FM-mirrored PO is ETL-seeded 0 (open-po-lines/
  route.ts:117-120, po-queries.ts:529-533,572-574 @5afa0a20): when the Portal lines say 0
  and the PO has an FM twin, the FM QC receiving log (the receipt rows' ``sumqty`` by
  ``idpogenitem``, the stand-in for ``noitemsqc``) is the received quantity, provided the
  FM items' ordered total is the gold one (the only proof the unit-less QC quantity is in
  the gold unit); otherwise the quantities are unknown;
* a null or unmapped word (Revised, Generated, anything new), quantities that are not
  readable in one unit, or no ordered quantity at all is **unknown**, with its reason,
  and counted by every reader, never guessed;
* a BC order reads its own ``status`` (Draft: not issued) and ``fullyReceived``;
* a Sage order is **unknown** until its ``iscomplete`` values are mapped (the SOP's
  1-5 enumeration was replaced by a quantity check, @48b6cfc docs/Internal-SOP.md
  v8.598; the vocabulary check records iscomplete against linescmpl >= lines).

Lateness (Vince): an open PO is **late** once its requested-by
date has passed and **at risk** while that date has not passed but the supplier's
confirmed date is later; a closed or draft PO is neither. The requested-by date is the
reference or it is reported missing: the supplier's date is never put in its place. Its
dates rank by ``PurchaseOrder.expected_date``'s authority precedence (the graph's
``lateness.precedence``): the top tier holding a date governs.

``read(source, oid, today)`` is the one reader: a PO's settled identity twins, each
record's status values and lines, the FM QC receiving log, its requested-by dates and
the supplier's promises, read through a ``source`` that keeps its caller's permission
filtering and markings: ``ContextSource`` (a ``plx.process`` reader context),
``StoreSource`` (the proactive engine's system store read) and ``ToolSource`` (the
``plx`` tools as one principal calls them). A twin link the reader may not see leaves
the PO's unknown ``blind``: never decided from the twins it can see.

Linked records of two systems that disagree are a named conflict (``PoRead.conflicts``),
never one side silently picked: a firm (``corroborated``) twin, another system's record
of the same order (a number-text twin the supplier and date corroborate, or a BC
``Your_Reference`` naming the FileMaker/Portal PO number that is unique and corroborated,
a lead is never compared), is compared on
receipt state (fully received or Complete against open lines outstanding) and received
quantity (one unit, both recorded; an FM-mirrored order's seeded 0 is not), and the
requested-by dates on their precedence tiers. Each conflict holds both values, each
side's own cited items and who governs: BC lines govern receipt state (Late = requested date passed and not fully received), so a BC side decides the status;
with no governing side the status is ``status_disputed``. A twin this reader cannot read
is no twin here, so it gets no conflict that reveals the other side. A settled twin link
that joins a BC record to another system's (the Portal entity map's verified
``bc_posted_as``, or a verified ``same_record_as`` across the two) is compared the same
way, never a member whose status joins the other's (``compared_twin``): the Portal closes
an order it posted (Complete) while BC still holds its receipts, and every source
(``ContextSource``, ``StoreSource``, ``ToolSource``) reads it so.

``python3 -m operating.po_status`` is the ``po_status.vocabulary`` live check: one host
capture cycle, then offline over the mirror the per-word classes of every captured PO
header and the Sage cross-tab, counts and status words only.
"""

from __future__ import annotations

import argparse
import functools
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import re
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

SCHEMA = "plx-po-status-vocabulary/v1"
OPEN, CLOSED, DRAFT, UNKNOWN = "open", "closed", "draft", "unknown"
CLASSES = (OPEN, CLOSED, DRAFT, UNKNOWN)
SUBMITTED, PARTIAL, RECEIVED, CANCELLED = "SUBMITTED", "PARTIAL", "RECEIVED", "CANCELLED"
# A Complete header over an outstanding quantity: closed, the rest a cancelled remainder.
COMPLETE = "COMPLETE"
# po-fulfillment-status.ts: received + EPS < ordered is PARTIAL, more is RECEIVED.
EPS = Decimal("0.0001")
CANCELLED_WORDS = frozenset({"CANCEL", "CANCELLED", "CANCELED"})
# open-po-lines/route.ts:46 @5afa0a20 CANCELLED_STATUSES: "closed" is cancelled there too.
CLOSED_WORDS = frozenset({"CLOSED"})
# Ross: Complete closes whatever was received.
COMPLETE_WORDS = frozenset({COMPLETE})
DRAFT_WORDS = frozenset({"DRAFT"})
# Issued words: their quantities decide open or closed. The FileMaker words the Portal
# header holds, the Portal's own codes (a PO the Portal created) and ORDERED, which the
# purchase-order-sent hook writes on a send (purchase-order-sent.ts@4af6957:89-92).
ISSUED_WORDS = frozenset({"PENDING", "B.ORDER", COMPLETE, "ORDERED",
                          SUBMITTED, PARTIAL, RECEIVED})
# purchase-order-sent.ts@4af6957:89-92 PAST_ORDERED (ORDERED included): the hook leaves these.
PAST_ORDERED = frozenset({
    "ORDERED", "PARTIALLY_RECEIVED", "PARTIAL", "RECEIVED", "INVOICED", "CLOSED",
    "CANCELLED", "CANCELED", "CANCEL", "COMPLETE", "B.ORDER"})
# Why a status is unknown (every reader counts these by name).
UNKNOWN_REASONS = ("status_missing", "status_unmapped", "quantities_unknown",
                   "nothing_ordered", "sage_unmapped", "status_disputed")
# Header classes (the process graph's PO stage predicate reads these).
ISSUED, NOT_ISSUED, IGNORED, UNMAPPED = "issued", "not_issued", "cancelled", "unmapped"
# Which source a PurchaseOrder property is the status of.
SOURCE_OF = {"status": "portal", "fm_status": "fm", "bc_status": "bc",
             "bc_fully_received": "bc", "sage_is_complete": "sage"}
# The header word of each source (a process graph's PO stage predicate reads these).
HEADER_PROPS = ("status", "fm_status", "bc_status", "sage_is_complete")
# The lateness verdict of one PO.
LATE, AT_RISK, NEITHER = "late", "at_risk", "neither"
# Lateness is judged on the business's calendar day, never the UTC day (decided
# 2026-10-28): a due day has passed once it has ended in America/New_York. zoneinfo, so
# both daylight-saving changes move the day boundary with the clock (no fixed offset).
BUSINESS_TZ = ZoneInfo("America/New_York")


def business_today(now: datetime | None = None) -> date:
    """``now`` (timezone-aware; default the current moment) as the business's calendar
    day (ET): the one "today" every lateness reader judges against. A naive ``now`` is
    refused (its zone is unknown), never read as local or UTC time."""
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError("business_today_needs_an_aware_moment")
    return moment.astimezone(BUSINESS_TZ).date()


@dataclass(frozen=True)
class PoStatus:
    cls: str                  # open / closed / draft / unknown
    code: str | None = None   # the Portal fulfillment code when decided
    reason: str | None = None  # UNKNOWN_REASONS when unknown
    # COMPLETE: the ordered quantity never received (None: not readable)
    canceled_remainder: Decimal | None = None


def word(raw: Any) -> str | None:
    """The status word compared: trimmed and upper-cased; None for a null or blank."""
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip().upper()
    return text or None


def header_class(raw: Any, source: str = "portal") -> str | None:
    """What a header word alone says: ``cancelled``, ``not_issued`` (draft), ``issued``
    or ``unmapped``; None for no word. A BC status other than Draft is issued."""
    text = word(raw)
    if text is None:
        return None
    if source == "sage":
        return UNMAPPED
    if text in DRAFT_WORDS:
        return NOT_ISSUED
    if source == "bc":
        return ISSUED
    if text in CANCELLED_WORDS or text in CLOSED_WORDS:
        return IGNORED
    return ISSUED if text in ISSUED_WORDS else UNMAPPED


def classify(raw: Any, ordered: Decimal | None, received: Decimal | None) -> PoStatus:
    """A Portal or FileMaker header word with the PO's summed line quantities (None: not
    readable). Mirrors derivePoFulfillmentCode for the mapped words; stricter where it
    would guess: an unmapped or null word, unreadable quantities or nothing ordered is
    unknown (the Portal would call those SUBMITTED). Complete is closed whatever was
    received: ``RECEIVED`` in full, else ``COMPLETE`` with its cancelled remainder."""
    kind = header_class(raw)
    if kind is None:
        return PoStatus(UNKNOWN, reason="status_missing")
    if kind == IGNORED:
        return PoStatus(CLOSED, CANCELLED)
    if kind == NOT_ISSUED:
        return PoStatus(DRAFT, "DRAFT")
    if kind == UNMAPPED:
        return PoStatus(UNKNOWN, reason="status_unmapped")
    complete = word(raw) in COMPLETE_WORDS
    if ordered is None or received is None:
        return (PoStatus(CLOSED, COMPLETE) if complete
                else PoStatus(UNKNOWN, reason="quantities_unknown"))
    ordered, received = max(Decimal(0), ordered), max(Decimal(0), received)
    if complete:
        if ordered <= EPS:
            return PoStatus(CLOSED, COMPLETE)
        if received + EPS < ordered:
            return PoStatus(CLOSED, COMPLETE, canceled_remainder=ordered - received)
        return PoStatus(CLOSED, RECEIVED)
    if ordered <= EPS:
        return PoStatus(UNKNOWN, reason="nothing_ordered")
    if received <= EPS:
        return PoStatus(OPEN, SUBMITTED)
    if received + EPS < ordered:
        return PoStatus(OPEN, PARTIAL)
    return PoStatus(CLOSED, RECEIVED)


def bc(status: Any, fully_received: Any) -> PoStatus:
    """A Business Central purchase order: Draft is not issued; else ``fullyReceived``."""
    if header_class(status, "bc") == NOT_ISSUED:
        return PoStatus(DRAFT, "DRAFT")
    flag = _flag(fully_received)
    if flag is None:
        return PoStatus(UNKNOWN, reason="status_missing")
    return PoStatus(CLOSED, RECEIVED) if flag else PoStatus(OPEN)


def sage(_iscomplete: Any = None) -> PoStatus:
    """A Sage purchase order: unknown until its iscomplete values are mapped."""
    return PoStatus(UNKNOWN, reason="sage_unmapped")


def combine(statuses: Iterable[PoStatus]) -> PoStatus:
    """One PO across its settled identities (the Portal record, its FM, Sage and BC
    twins): the class the records that decide agree on; an unknown record decides
    nothing; two classes disagree (``status_disputed``); none decided: the first reason."""
    statuses = list(statuses)
    known = {s.cls for s in statuses if s.cls != UNKNOWN}
    if len(known) > 1:
        return PoStatus(UNKNOWN, reason="status_disputed")
    if known:
        (cls,) = known
        codes = {s.code for s in statuses if s.cls == cls and s.code}
        if RECEIVED in codes and codes <= {RECEIVED, COMPLETE}:
            return PoStatus(cls, RECEIVED)   # a Complete record whose twin received all
        if codes == {COMPLETE}:
            rest = {s.canceled_remainder for s in statuses
                    if s.code == COMPLETE and s.canceled_remainder is not None}
            return PoStatus(cls, COMPLETE,
                            canceled_remainder=next(iter(rest)) if len(rest) == 1 else None)
        return PoStatus(cls, next(iter(codes)) if len(codes) == 1 else None)
    reasons = sorted({s.reason for s in statuses if s.reason},
                     key=UNKNOWN_REASONS.index)
    return PoStatus(UNKNOWN, reason=reasons[0] if reasons else "status_missing")


def record(values: Mapping[str, Sequence[Any]],
           lines: Iterable[tuple[Any, Any, Any]] | None,
           fm_lines: Any = None) -> PoStatus:
    """One PurchaseOrder record from the raw values of its status properties
    (``SOURCE_OF``: ``status`` the Portal header, ``fm_status`` the FileMaker one,
    ``bc_status``/``bc_fully_received``, ``sage_is_complete``) and its own lines'
    ``(ordered, received, unit)`` (None: its lines were not read). The FileMaker header
    has no received quantity, so its issued words other than Complete stay unknown; two
    values of one property are disputed; a record with no status property is
    ``status_missing``.

    ``fm_lines``: the FM twin's items as ``(ordered, QC received, unit)``, the QC receiving
    log summed per item (None: no FM twin; ``UNREAD``: not readable). When the Portal
    lines received nothing (the ETL-seeded 0 of an FM-mirrored PO), the QC log is the
    received quantity if the FM ordered total is the gold one; any other FM total or an
    unreadable FM line or QC value makes the quantities unknown."""
    statuses = []
    for prop in ("status", "fm_status"):
        raw = values.get(prop) or ()
        words = {word(v) for v in raw}
        if not raw:
            continue
        if len(words) > 1:
            statuses.append(PoStatus(UNKNOWN, reason="status_disputed"))
            continue
        (text,) = words
        totals = (line_totals(lines) if prop == "status" and lines is not None
                  and header_class(text) == ISSUED else (None, None))
        if prop == "status" and fm_lines is not None:
            totals = _fm_received(totals, fm_lines)
        statuses.append(classify(text, *totals))
    if values.get("bc_status") or values.get("bc_fully_received"):
        heads = {word(v) for v in values.get("bc_status") or ()}
        flags = {_flag(v) for v in values.get("bc_fully_received") or ()}
        statuses.append(PoStatus(UNKNOWN, reason="status_disputed")
                        if len(heads) > 1 or len(flags) > 1
                        else bc(next(iter(heads), None), next(iter(flags), None)))
    if values.get("sage_is_complete"):
        statuses.append(sage(values["sage_is_complete"][0]))
    return combine(statuses)


def _fm_received(totals: tuple[Decimal | None, Decimal | None], fm_lines: Any
                 ) -> tuple[Decimal | None, Decimal | None]:
    """The gold ``(ordered, received)`` with the FM QC receiving log as the received
    quantity when gold received nothing (``record``); unchanged otherwise."""
    ordered, received = totals
    if ordered is None or received is None or received > EPS:
        return totals
    if fm_lines is UNREAD:
        return None, None
    fm_lines = list(fm_lines)
    qc = Decimal(0)
    for _, got, _ in fm_lines:
        if got is UNREAD:
            return None, None
        got = Decimal(0) if got is None else _number(got)
        if got is None:
            return None, None
        qc += max(Decimal(0), got)
    if qc <= EPS:
        return totals   # nothing in the QC log: the gold 0 stands
    fm_ordered, _ = line_totals((want, None, None) for want, _, _ in fm_lines)
    if fm_ordered is None or abs(fm_ordered - ordered) > EPS:
        return None, None   # no proof the QC quantity is in the gold unit
    return ordered, qc


def _flag(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    text = word(raw)
    return {"TRUE": True, "1": True, "FALSE": False, "0": False}.get(text or "")


def _number(raw: Any) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        out = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


class _Unread:
    def __repr__(self) -> str:
        return "UNREAD"


UNREAD = _Unread()   # a quantity or unit not readable (absent, withheld, disputed)


def line_totals(lines: Iterable[tuple[Any, Any, Any]]) -> tuple[Decimal | None, Decimal | None]:
    """The summed (ordered, received) of a PO's lines, each ``(ordered, received, unit)``
    as raw values. A null received quantity counts 0 (the Portal's SUM/COALESCE); an
    ``UNREAD`` value, an ordered quantity that is null or not a number, or lines in more
    than one unit make both None: never summed across units, never guessed."""
    ordered = received = Decimal(0)
    units = set()
    for want, got, unit in lines:
        if UNREAD in (want, got, unit):
            return None, None
        want = _number(want)
        got = Decimal(0) if got is None else _number(got)
        if want is None or got is None:
            return None, None
        units.add(unit.strip().upper() if isinstance(unit, str) and unit.strip() else None)
        ordered, received = ordered + want, received + got
    if len(units) > 1:
        return None, None
    return ordered, received


def line_open(ordered: Any, received: Any) -> bool:
    """One line is open: ordered above zero and received (none read as 0) below it. An
    unparseable quantity is open (never closed on a guess)."""
    want = _number(ordered)
    got = Decimal(0) if received is None else _number(received)
    if want is None or got is None:
        return True
    return want > 0 and got < want


def lateness_verdict(status: PoStatus | str, requested: date | None,
                     confirmed: date | None, today: date) -> tuple[str, str | None]:
    """``late``, ``at_risk`` or ``neither`` for one PO, or ``unknown`` with why. Late: open
    and the requested-by date has passed. At risk: open, not yet late, and the supplier's
    confirmed date is after the requested-by date. The requested-by date is never
    replaced by the supplier's date: without it the verdict is unknown."""
    return verdict(status, Need(requested, requested), confirmed, today)


# --------------------------------------------------------------------------- the one reader

PO_TYPE, LINE_TYPE, RECEIPT_LINE_TYPE = "PurchaseOrder", "PurchaseOrderLine", "ReceiptLine"
CONFIRMATION_TYPE = "SupplierConfirmation"
SAME_RECORD = "PurchaseOrder.same_record_as"
# the BC order the Portal posted a gold PO as (the entity map's join, served at verified)
POSTED = "PurchaseOrder.bc_posted_as"
TWIN_LINKS = (SAME_RECORD, POSTED)
LINE_OF = "PurchaseOrderLine.line_of"
RECEIVES_LINE = "ReceiptLine.receives_line"
CONFIRMS = "SupplierConfirmation.confirms"
ORDERED, RECEIVED_QTY = "ordered_quantity", "received_quantity"
NEED_PROP, PROMISE_PROP, PROMISE_ORDER = "expected_date", "delivery_date", "document_date"
# A line's own expected receipt date (BC expectedReceiptDate): the lowest requested-by tier,
# read only where no header tier holds a date.
LINE_EXPECTED = "bc_expected_receipt_date"
PORTAL_BINDING = "portal.app_purchase_order"
FM_BINDING = "portal.fm_generated_purchase_order"
FM_ITEM_BINDING = "portal.fm_generated_purchase_order_item"
# The FM QC receiving log: receipt rows by idpogenitem (ReceiptLine.receives_line), their
# sumqty the stand-in for noitemsqc, which the Portal does not serve.
FM_QC_BINDINGS = frozenset({"portal.fm_receipt_raw_material", "portal.fm_receipt_packaging"})
# Identity tiers that join twins (the store and tool readers; the process context reads
# its own ``_IDENTITY_TIERS``), and the tiers a confirmation link counts at.
SETTLED = ("verified", "reviewed")
CONFIRMING = ("verified", "reviewed", "corroborated")
FIRM = "corroborated"   # a firm Portal-BC twin link (identity_scoring TWIN_TIER)
MAX_TWINS = 8
PAGES = 20
LATENESS_GRAPH = Path(__file__).resolve().parents[1] / "ontology" / "schema" / "process" \
    / "po_delivery_window.json"


def binding_of(object_id: str) -> str | None:
    """The source binding of an object id (``Type:binding:key``)."""
    parts = str(object_id).split(":", 2)
    return parts[1] if len(parts) == 3 else None


@functools.lru_cache(maxsize=1)
def binding_ranks() -> dict[str, int]:
    """binding -> precedence rank of its requested-by date (0 governs): the receipt
    stage's ``lateness.precedence`` of ``po_delivery_window``, each event type's binding
    (``PurchaseOrder.expected_date``'s authority precedence). A binding not ranked there
    ranks below every one (``len(precedence)``)."""
    from ontology.schema import load_schema

    events = load_schema().event_types
    graph = json.loads(LATENESS_GRAPH.read_text("utf-8"))
    out: dict[str, int] = {}
    for stage in graph.get("stages", ()):
        precedence = ((stage.get("due") or {}).get("lateness") or {}).get("precedence")
        for rank, tier in enumerate(precedence or ()):
            for event in tier:
                out.setdefault(events[event]["time"]["binding"], rank)
    return out


def rank_of(object_id: str) -> int:
    ranks = binding_ranks()
    return ranks.get(binding_of(object_id) or "", len(set(ranks.values())))


def event_ranks(due: Mapping[str, Any]) -> dict[str, int]:
    """A ``due`` declaration's planned event (short name) -> its precedence rank, its
    ``replaced_by`` property ranked as the event it replaces."""
    tier_of = {eid.split(".", 1)[1]: rank
               for rank, tier in enumerate(due["lateness"]["precedence"]) for eid in tier}
    replaced = due.get("replaced_by")
    if replaced:
        tier_of[replaced["property"]] = tier_of[replaced["event_type"].split(".", 1)[1]]
    return tier_of


def day_of(raw: Any) -> date | None:
    """An ISO date (a timestamp's day); None when not one."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def clusters(pairs: Iterable[tuple[str, str]], members: Iterable[str]) -> dict[str, str]:
    """member -> its cluster's least object id, joining the pairs (settled identities)."""
    parent: dict[str, str] = {m: m for m in members}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return {m: find(m) for m in parent}


@dataclass(frozen=True)
class Need:
    """The requested-by date: the top precedence tier holding a date governs (``lo`` to
    ``hi`` when its dates disagree); ``governing`` its items, ``others`` the lower tiers',
    ``unparseable`` the items with no readable date."""
    lo: date | None = None
    hi: date | None = None
    governing: tuple = ()
    others: tuple = ()
    unparseable: tuple = ()
    undated: tuple = ()   # open lines holding no readable date (``line_need``)


def need_date(dated: Iterable[tuple[int, date | None, Any]]) -> Need:
    """From ``(rank, day or None, item)``: the governing requested-by date."""
    dated = list(dated)
    unparseable = tuple(item for _, day, item in dated if day is None)
    parsed = [(rank, day, item) for rank, day, item in dated if day is not None]
    if not parsed:
        return Need(unparseable=unparseable)
    top = min(rank for rank, _, _ in parsed)
    days = sorted({day for rank, day, _ in parsed if rank == top})
    governing = tuple(item for rank, _, item in parsed if rank == top)
    others = tuple(item for rank, _, item in sorted(
        ((r, d, i) for r, d, i in parsed if r != top), key=lambda e: (e[0], e[1])))
    return Need(days[0], days[-1], governing, others, unparseable)


def line_need(dated: Iterable[tuple[date | None, Any]], today: date) -> Need:
    """The requested-by date from the dated lines (``(day or None, item)`` per open line),
    the lowest tier, used when no header holds a date: the open lines' earliest date, so an
    order is late once any open line is (the lateness oracle's rule). Not late is decided
    only when every open line is dated: otherwise a past date still decides late, but a
    later one decides nothing (``undated`` names the lines without one)."""
    dated = list(dated)
    parsed = sorted(((d, i) for d, i in dated if d is not None), key=lambda e: e[0])
    undated = tuple(i for d, i in dated if d is None)
    if not parsed or (undated and parsed[0][0] >= today):
        return Need(undated=undated)
    first = parsed[0][0]
    return Need(first, first, tuple(i for d, i in parsed if d == first),
                tuple(i for d, i in parsed if d != first), undated=undated)


@dataclass(frozen=True)
class Promise:
    """The supplier's promise: ``first`` and ``latest`` (the revision, ordered by the
    confirmation's document date), or why none is stated (``reason``: ``none``,
    ``promises_disagree``); ``items`` every promised date's item, earliest document first."""
    first: date | None = None
    latest: date | None = None
    reason: str | None = None
    items: tuple = ()


def promise(stated: Iterable[tuple[date | None, date, Any]]) -> Promise:
    """From ``(document date or None, promised day, item)``: one promise when they agree;
    a later document's differing date is the revision; two dates of one document, or
    differing dates any of which is undated, disagree."""
    stated = list(stated)
    items = tuple(item for _, _, item in sorted(stated, key=lambda s: (
        s[0] is None, s[0] or date.min, s[1])))
    if not stated:
        return Promise(reason="none")
    if len({day for _, day, _ in stated}) == 1:
        return Promise(stated[0][1], stated[0][1], None, items)
    if any(order is None for order, _, _ in stated):
        return Promise(reason="promises_disagree", items=items)
    by_doc: dict[date, set[date]] = {}
    for order, day, _ in stated:
        by_doc.setdefault(order, set()).add(day)
    early, late = by_doc[min(by_doc)], by_doc[max(by_doc)]
    if len(early) == 1 and len(late) == 1 and min(by_doc) != max(by_doc):
        return Promise(next(iter(early)), next(iter(late)), None, items)
    return Promise(reason="promises_disagree", items=items)


def verdict(status: PoStatus | str, need: Need, latest: date | None, today: date
            ) -> tuple[str, str | None]:
    """Vince's rule over the governing requested-by date: closed or draft is neither;
    open is late once ``hi`` has passed, at risk while ``lo`` has not and the latest
    promise is after ``hi``; today between disagreeing top-tier dates is unknown
    (``need_date_disputed``); no requested-by date is unknown, never the promise's."""
    cls = status.cls if isinstance(status, PoStatus) else status
    if cls in (CLOSED, DRAFT):
        return NEITHER, None
    if cls != OPEN:
        return UNKNOWN, "status_unknown"
    if need.lo is None:
        return UNKNOWN, "no_requested_date"
    if today > need.hi:
        return LATE, None
    if today <= need.lo:
        return (AT_RISK, None) if latest is not None and latest > need.hi else (NEITHER, None)
    return UNKNOWN, "need_date_disputed"


@dataclass
class PoRead:
    """One PO as one reader reads it: its settled identity twins (``members``, the root
    first), the combined status, whether an unknown status is unknown because something
    is withheld from this reader (``blind``), the source's evidence of the status (status
    values and line quantities) and the link marks it was read through, and, when read,
    the requested-by date, the supplier's promise and the lateness verdict."""
    root: str
    members: list[str]
    status: PoStatus
    blind: bool = False
    evidence: list = field(default_factory=list)
    marks: list = field(default_factory=list)
    identity_marks: list = field(default_factory=list)
    need: Need | None = None
    promise: Promise | None = None
    verdict: tuple[str, str | None] | None = None
    date_twins: list = field(default_factory=list)   # firm twins its dates were read from
    # where linked records disagree (``CONFLICT_FIELDS``): both values, each side's cited
    # items and who governs; never one side silently picked
    conflicts: list = field(default_factory=list)


def system_of(object_id: str) -> str:
    """The system a record is from: its binding's source prefix (``portal``, ``bc``)."""
    return (binding_of(object_id) or "unknown").split(".", 1)[0]


def _read_side(source: Any, records: Sequence[str],
               reads: Mapping[str, tuple[dict[str, list[Any]], list, bool]]
               ) -> tuple[PoStatus, list, list, bool, dict[str, tuple[Any, list]]]:
    """One side of a PO (its settled twins, or the firm twins linked to them) by the one
    definition: the combined status, its evidence and link marks, whether a part is
    hidden, and each record's lines as read ``(triples or None, evidence)``."""
    fm = [m for m in records if reads[m][0].get("fm_status")]
    statuses, cited, marks, blind = [], [], [], False
    lines_of: dict[str, tuple[Any, list]] = {}
    for member in records:
        values, evidence, hidden = reads[member]
        blind = blind or hidden
        cited += evidence
        lines = fm_lines = None
        words = {word(v) for v in values.get("status", ())}
        if len(words) == 1 and header_class(next(iter(words))) == ISSUED:
            lines, evidence, hidden, link_marks = source.lines(member)
            blind = blind or hidden
            cited += evidence
            marks += link_marks
            lines_of[member] = (lines, list(evidence))
            totals = line_totals(lines) if lines is not None else (None, None)
            if fm and totals[1] is not None and totals[1] <= EPS:
                logs = []
                for twin in fm:
                    found, evidence, hidden, link_marks = source.fm_lines(twin)
                    blind = blind or (hidden and found is UNREAD)
                    cited += evidence
                    marks += link_marks
                    logs.append(found)
                fm_lines = _one_log(logs)
        statuses.append(record(values, lines, fm_lines))
    return combine(statuses), cited, marks, blind, lines_of


# A side's receipt state, as the two systems are compared: fully received (a Portal or FM
# Complete counts) against open lines outstanding. Draft, cancelled and unknown compare
# nothing: no receipt state is stated.
FULLY_RECEIVED, OPEN_LINES = "fully_received", "open_lines_outstanding"
# Who governs a field the systems disagree on (BC lines govern receipt
# state; late = requested date passed and not fully received). The requested-by date ranks
# by the declared precedence (``PurchaseOrder.expected_date``'s authority).
RECEIPT_AUTHORITY = "bc"
CONFLICT_FIELDS = ("receipt_state", "received_quantity", "expected_date")


def receipt_state(status: PoStatus) -> str | None:
    if status.cls == OPEN:
        return OPEN_LINES
    if status.cls == CLOSED and status.code in (RECEIVED, COMPLETE):
        return FULLY_RECEIVED
    return None


def compared_twin(a: str, b: str) -> bool:
    """Whether a twin link joins the receipt authority's record (BC) to another system's:
    such a twin is compared (``_compare``: BC lines govern receipt state) at any tier from
    ``FIRM`` up, never a settled member whose status joins the other's (``combine`` would
    dispute the Portal's Complete against BC's open lines)."""
    return (system_of(a) == RECEIPT_AUTHORITY) != (system_of(b) == RECEIPT_AUTHORITY)


def _directions(link: str, member: str) -> tuple[str, ...]:
    """The directions a twin link can reach another record from ``member``: ``POSTED`` is
    declared from the Portal order to the BC order, so one side reads it forward and the
    other reverse; ``SAME_RECORD`` joins records of either system both ways."""
    if link == POSTED:
        return ("reverse",) if system_of(member) == RECEIPT_AUTHORITY else ("forward",)
    return ("forward", "reverse")


def side_receipt(records: Sequence[str],
                 reads: Mapping[str, tuple[dict[str, list[Any]], list, bool]],
                 status: PoStatus, lines: Mapping[str, tuple[Any, list]]) -> str | None:
    """A side's receipt state. BC lines govern receipt state, whatever
    the BC header word (the BC API's ``Draft`` is a document not yet released; live
    2026-10-27 every BC order linked to a Portal one reads Draft): a BC record with
    readable lines is open while any line's received quantity is below its ordered one,
    else fully received; with no readable line its ``fullyReceived`` flag states it. Any
    other side by the one definition's status. A missing, unparseable or disputed flag
    and BC records that disagree state none, never a guess."""
    bc_records = [r for r in records if system_of(r) == RECEIPT_AUTHORITY]
    if not bc_records:
        return receipt_state(status)
    states = set()
    for record_id in bc_records:
        triples = (lines.get(record_id) or (None, []))[0]
        if triples and all(UNREAD not in (want, got) for want, got, _ in triples):
            states.add(OPEN_LINES if any(line_open(want, got) for want, got, _ in triples)
                       else FULLY_RECEIVED)
            continue
        flags = {_flag(v) for v in reads[record_id][0].get("bc_fully_received") or ()}
        if len(flags) != 1 or None in flags:
            return None
        states.add(FULLY_RECEIVED if flags.pop() else OPEN_LINES)
    return states.pop() if len(states) == 1 else None


def _recorded_received(lines: Iterable[tuple[Any, list]]) -> tuple[Decimal, str | None,
                                                                   list] | None:
    """A side's summed received quantity in one unit and its cited received values; None
    when not recorded (no line, every received value null), unreadable or in two units."""
    total, units, cited, any_recorded = Decimal(0), set(), [], False
    for triples, evidence in lines:
        if triples is None:
            return None
        for _want, got, unit in triples:
            if UNREAD in (got, unit):
                return None
            if got is None:
                continue
            number = _number(got)
            if number is None:
                return None
            any_recorded = True
            total += number
            units.add(word(unit))
        cited += [e for e in evidence if isinstance(e, Mapping)
                  and e.get("property") == RECEIVED_QTY]
    if not any_recorded or len(units) != 1:
        return None
    return total, next(iter(units)), cited


def _side_entry(system: str, records: Sequence[str], value: str, items: Sequence[Any]
                ) -> dict[str, Any]:
    return {"system": system, "records": list(records), "value": value, "items": list(items)}


def _systems(records: Sequence[str]) -> str:
    return "+".join(sorted({system_of(r) for r in records}))


def _compare(source: Any, out: "PoRead", lines_of: Mapping[str, tuple[Any, list]],
             firm: Sequence[str], firm_marks: Sequence[Any],
             mine_reads: Mapping[str, tuple[dict[str, list[Any]], list, bool]]) -> None:
    """Compare this PO's settled side with its firm twins' (another system's record of the
    same order, linked as this reader may read it): receipt state and received quantity.
    A disagreement is a named conflict with both values, each side's own cited items, and
    who governs; BC lines govern receipt state, so a disagreement BC governs decides the
    status (its evidence then joins the status's); with no governing side the status is
    disputed. A side this reader cannot read states nothing, so nothing is compared."""
    reads = {t: source.values(t) for t in firm}
    fm_members = [r for r, read in (*mine_reads.items(), *reads.items())
                  if read[0].get("fm_status")]
    theirs, their_evidence, their_marks, _hidden, their_lines = _read_side(source, firm, reads)
    mine_side, their_side = _systems(out.members), _systems(firm)

    def side_lines(records: Sequence[str], read: Mapping[str, tuple[Any, list]]
                   ) -> dict[str, tuple[Any, list]]:
        found = {}
        for record_id in records:
            if record_id in read:
                found[record_id] = read[record_id]
            else:
                triples, evidence, _h, _m = source.lines(record_id)
                found[record_id] = (triples, list(evidence))
        return found
    mine_lines = side_lines(out.members, lines_of)
    other_lines = side_lines(firm, their_lines)
    mine = side_receipt(out.members, mine_reads, out.status, mine_lines)
    other = side_receipt(firm, reads, theirs, other_lines)
    mine_bc = RECEIPT_AUTHORITY in mine_side.split("+")
    their_bc = RECEIPT_AUTHORITY in their_side.split("+")
    # BC lines govern receipt state whichever record is asked: when the one BC side reads
    # fully received, the order is received even though its own header word (Open, a
    # fullyReceived flag still false) says otherwise, and both sides agree with it
    bc_state = mine if mine_bc and not their_bc else other if their_bc and not mine_bc else None
    if bc_state == FULLY_RECEIVED and out.status != PoStatus(CLOSED, RECEIVED):
        out.status = PoStatus(CLOSED, RECEIVED)
        bc_lines = mine_lines if mine_bc else other_lines
        out.evidence += [e for _t, ev in bc_lines.values() for e in ev
                         if isinstance(e, Mapping) and e.get("property") == RECEIVED_QTY
                         and not any(e is x for x in out.evidence)]
        if their_bc:
            out.marks += [*their_marks, *firm_marks]
    if mine is not None and other is not None and mine != other:
        governs = (mine_side if mine_bc and not their_bc else
                   their_side if their_bc and not mine_bc else None)

        def cited(evidence: Sequence[Any], side_lines_: Mapping[str, tuple[Any, list]],
                  bc_side: bool) -> list:
            # the status values, and a BC side's line quantities (its lines govern)
            items = [e for e in evidence if isinstance(e, Mapping)
                     and e.get("property") in SOURCE_OF]
            if bc_side:
                items += [e for _t, ev in side_lines_.values() for e in ev
                          if isinstance(e, Mapping) and e.get("property") == RECEIVED_QTY]
            return items
        mine_items = cited(out.evidence, mine_lines, mine_bc)
        their_items = cited(their_evidence, other_lines, their_bc)
        out.conflicts.append({
            "field": "receipt_state", "governs": governs, "marks": list(firm_marks),
            "sides": [_side_entry(mine_side, out.members, mine, mine_items),
                      _side_entry(their_side, firm, other, their_items)]})
        if governs is None:
            out.status = PoStatus(UNKNOWN, reason="status_disputed")
        elif governs == mine_side:
            # this record is BC's: its lines decide, whatever its header word (Draft)
            if mine == OPEN_LINES and out.status.cls != OPEN:
                out.status = PoStatus(OPEN)
        elif governs == their_side:
            # the governing side's receipt state decides: open lines keep the order open
            # (its own open code when it has one), fully received closes it received
            out.status = (PoStatus(CLOSED, RECEIVED) if other == FULLY_RECEIVED else
                          theirs if theirs.cls == OPEN else PoStatus(OPEN))
            out.evidence += [e for e in their_items if not any(e is x for x in out.evidence)]
            out.marks += [*their_marks, *firm_marks]
    # received quantity where both systems record one in the same unit
    a = _recorded_received(mine_lines.values())
    b = _recorded_received(other_lines.values())

    # an FM-mirrored Portal order's received 0 is ETL-seeded, not a recorded receipt
    def seeded(records: Sequence[str], got: Any) -> bool:
        return got is not None and got[0] <= EPS and any(r in fm_members for r in records)
    if seeded(out.members, a) or seeded(firm, b):
        return
    if a is not None and b is not None and a[1] == b[1] and abs(a[0] - b[0]) > EPS:
        out.conflicts.append({
            "field": "received_quantity", "governs": (
                RECEIPT_AUTHORITY if RECEIPT_AUTHORITY in (mine_side, their_side) else None),
            "unit": a[1], "marks": list(firm_marks),
            "sides": [_side_entry(mine_side, out.members, str(a[0]), a[2]),
                      _side_entry(their_side, firm, str(b[0]), b[2])]})


def _date_conflict(raw: Sequence[tuple[int, date | None, Any]]) -> dict[str, Any] | None:
    """The requested-by dates linked records state differently across precedence tiers:
    the governing (top) tier's against each lower tier's differing one, all cited; the
    declared precedence governs. None when they agree or only one tier holds a date."""
    parsed = [(rank, day, item) for rank, day, item in raw if day is not None]
    if not parsed:
        return None
    top = min(rank for rank, _, _ in parsed)
    top_days = {day for rank, day, _ in parsed if rank == top}
    lower = [(rank, day, item) for rank, day, item in parsed
             if rank != top and day not in top_days]
    if not lower:
        return None
    sides = []
    for label, items, days in (
            ("governing", [item for rank, _, item in parsed if rank == top], top_days),
            ("lower_precedence", [item for _, _, item in lower], {d for _, d, _ in lower})):
        records = list(dict.fromkeys(r for r in map(_item_record, items) if r))
        systems = sorted({s for s in map(_item_system, items) if s})
        sides.append(_side_entry("+".join(systems) if systems else label, records,
                                 ",".join(sorted(d.isoformat() for d in days)), items))
    return {"field": "expected_date", "governs": "precedence", "sides": sides}


# a citation's L1 scheme -> the system it is from (a value that names no record)
_SCHEME_SYSTEM = {"api": "portal", "bc": "bc"}


def _item_system(item: Any) -> str | None:
    """The system a dated item is from: its record's binding, else its citation's L1
    scheme; None when neither says."""
    found = _item_record(item)
    if found is not None:
        return system_of(found)
    cite = item.get("citation") if isinstance(item, Mapping) else None
    handle = cite.get("l1_handle") if isinstance(cite, Mapping) else None
    if isinstance(handle, str) and "://" in handle:
        return _SCHEME_SYSTEM.get(handle.split("://", 1)[0])
    return None


def _item_record(item: Any) -> str | None:
    """The record a dated item was read from, when the item names it (a store row's
    ``object_id``, a tool reader's ``(record, raw)``); None otherwise."""
    if isinstance(item, Mapping):
        found = item.get("object_id")
    elif isinstance(item, tuple) and item:
        found = item[0]
    else:
        found = None
    return found if isinstance(found, str) and found else None


def read(source: Any, oid: str, today: date | None = None, *, dates: bool = True) -> PoRead:
    """The one purchase-order reader. ``source`` reads for one caller with its own
    permission filtering and markings: ``twins(oid)`` -> (members, blind, identity marks);
    ``values(m)`` -> (status values, evidence, hidden); ``lines(m)`` and ``fm_lines(m)``
    -> (``(ordered, received, unit)`` triples, None or ``UNREAD``; evidence; hidden; link
    marks); ``need_dates(oid, members)`` -> ``(rank, day, item)``;
    ``line_dates(members)`` -> ``(day or None, item)`` per open line;
    ``promises(oid, members)`` -> ``(document date, day, item)``, or None when a
    confirmation listing is withheld from this reader (``promises_unread``).

    Lines are read only for a single issued Portal word (a closed, draft, unmapped or
    missing word is the same class for every reader); the FM QC log only when the Portal
    lines received nothing and the PO has an FM twin (two twins logging receipts: unread). Dates are read
    for an open or received PO when ``dates`` and ``today`` are given."""
    members, blind, identity_marks = source.twins(oid)
    reads = {m: source.values(m) for m in members}
    out = PoRead(oid, list(members), PoStatus(UNKNOWN, reason="status_missing"),
                 identity_marks=list(identity_marks))
    out.status, out.evidence, out.marks, hidden, lines_of = _read_side(source, members, reads)
    blind = blind or hidden
    # a firm (``corroborated``) twin is another system's record of this order: its status
    # never joins this PO's, but where the two disagree the conflict is named (``_compare``)
    firm, firm_marks = (source.date_twins(oid, members)
                        if hasattr(source, "date_twins") else ([], []))
    if firm:
        _compare(source, out, lines_of, firm, firm_marks, reads)
    out.blind = blind and out.status.cls == UNKNOWN
    if dates and today is not None and (out.status.cls == OPEN
                                        or out.status.code == RECEIVED):
        # a firm (``corroborated``) Portal-BC twin is not an identity (its status never
        # joins this PO's), but its dates are this order's: read through the link, cited
        dated = [*members, *(t for t in firm if t not in members)]
        out.date_twins, out.marks = list(firm), [*out.marks, *firm_marks]
        raw = list(source.need_dates(oid, dated))
        out.need = need_date(raw)
        found = _date_conflict(raw)
        if found is not None:
            out.conflicts.append({**found, "marks": list(firm_marks)})
        if out.need.lo is None:   # no header date: the open lines' own expected dates
            out.need = line_need(source.line_dates(dated), today)
        stated = source.promises(oid, members)
        # None: a confirmation listing this reader may not read in full; a promise it
        # cannot see may be later than the need date, so never "neither" from a part
        out.promise = promise(stated) if stated is not None else Promise(
            reason="promises_unread")
        out.verdict = verdict(out.status, out.need, out.promise.latest, today)
        if stated is None and out.verdict == (NEITHER, None):
            out.verdict = (UNKNOWN, "promises_unread")
    elif today is not None:
        out.verdict = verdict(out.status, Need(), None, today)
    return out


def _one_log(logs: Sequence[Any]) -> Any:
    """The FM QC log of a PO with one or more FM twins: unread when any is unread or more
    than one twin logs a receipt (two logs of one order are not summed); the one twin's
    that logs one; otherwise (no QC receipt anywhere) the first, which decides nothing."""
    if any(log is UNREAD or any(got is UNREAD or got is not None and _number(got) is None
                                for _, got, _ in log) for log in logs):
        return UNREAD
    logged = [log for log in logs if any(
        got is not None and (_number(got) or Decimal(0)) > EPS for _, got, _ in log)]
    if len(logged) > 1:
        return UNREAD
    return logged[0] if logged else (logs[0] if logs else None)


# ---- the process reader context (plx.process: the reader's grant, scope and floor)

class ContextSource:
    """``plx.process``'s reader: ``tools`` (``ProcessTools``) and ``ctx`` (its ``_Ctx``: the
    reader's grant, markings, as-known-at), the graph's ``open_when`` rules, lines
    declaration and tier floor. A link, value or record this reader may not read makes
    its part hidden, never read; ``stage``/``conflicts``: the lateness stage (its due
    dates are ``_due_date``'s, kept in ``dated``; the promise read in ``promised``)."""

    def __init__(self, tools: Any, ctx: Any, graph: Mapping[str, Any],
                 stage: Mapping[str, Any] | None = None,
                 conflicts: Sequence[Mapping[str, Any]] = ()) -> None:
        from operating import ontology_tools_process as op

        self.tools, self.ctx, self.graph, self.op = tools, ctx, graph, op
        self.stage, self.conflicts = stage, conflicts
        self.floor = graph.get("tier_floor", op.DEFAULT_TIER_FLOOR)
        self.spec = next((((s.get("due") or {}).get("lateness") or {}).get("lines")
                          for s in graph.get("stages", ())
                          if ((s.get("due") or {}).get("lateness") or {}).get("lines")), None)
        self.dated: Any = None
        self.promised: Records | None = None

    def _reach(self, root_type: str, oid: str, paths: Sequence[Sequence[Mapping]],
               tid: str, bindings: Iterable[str] | None = None) -> Any:
        tspec = self.tools.base.schema.object_types.get(tid, {})
        return self.tools._paths(self.ctx, root_type, oid, paths, self.floor,
                                 spec_marks=tspec.get("markings", ()),
                                 final_bindings=set(bindings) if bindings is not None
                                 else {b["id"] for b in tspec.get("bindings", ())})

    def twins(self, oid: str) -> tuple[list[str], bool, list]:
        """The root and the records its settled identity links join it to (an identity
        link type, a verified or reviewed tier as served, the link and both ends visible
        to this reader, its evidence allowed), followed transitively; and whether an
        identity link of a member is hidden from this reader (a fuller grant might join
        a twin)."""
        op, ctx, schema = self.op, self.ctx, self.tools.base.schema
        kinds = sorted(lid for lid, link in schema.link_types.items()
                       if link["from"] == ctx.obj(oid)["object_type"]
                       and op.identity_link_type(link))
        members, frontier, blind = [oid], [oid], False
        while kinds and frontier:
            held = json.dumps(frontier)
            frontier = []
            for row in self.tools.base.store.conn.execute(
                    "SELECT * FROM links WHERE link_type IN (SELECT value FROM json_each(?)) "
                    "AND (from_object IN (SELECT value FROM json_each(?)) OR to_object IN "
                    "(SELECT value FROM json_each(?))) ORDER BY link_id",
                    (json.dumps(kinds), held, held)):
                if not op._known_at(row, ctx.at):
                    continue
                ends = ctx.visible([row["from_object"], row["to_object"]])
                if not ctx.row_visible(row) or len(ends) < 2 \
                        or not self.tools._evidence_ok(ctx, row):
                    blind = True
                    continue
                if op._link_tier(row, ends) not in op._IDENTITY_TIERS \
                        or compared_twin(row["from_object"], row["to_object"]):
                    continue   # a lead, or another system's record: compared, not joined
                for end in (row["from_object"], row["to_object"]):
                    if end not in members:
                        members.append(end)
                        frontier.append(end)
        return members, blind, []

    def date_twins(self, oid: str, members: Sequence[str]) -> tuple[list[str], list[str]]:
        """The records a firm (``corroborated``) twin link, or a settled one to another
        system's record (``compared_twin``), joins to a member, the link and both ends
        visible to this reader and its evidence allowed; their dates are read, never their
        status. A link this reader may not read is no twin here."""
        op, ctx, schema = self.op, self.ctx, self.tools.base.schema
        kinds = sorted(lid for lid, link in schema.link_types.items()
                       if link["from"] == ctx.obj(oid)["object_type"]
                       and op.identity_link_type(link))
        out: list[str] = []
        if not kinds:
            return out, []
        held = json.dumps(list(members))
        for row in self.tools.base.store.conn.execute(
                "SELECT * FROM links WHERE link_type IN (SELECT value FROM json_each(?)) "
                "AND (from_object IN (SELECT value FROM json_each(?)) OR to_object IN "
                "(SELECT value FROM json_each(?))) ORDER BY link_id",
                (json.dumps(kinds), held, held)):
            if not op._known_at(row, ctx.at):
                continue
            ends = ctx.visible([row["from_object"], row["to_object"]])
            if not ctx.row_visible(row) or len(ends) < 2 \
                    or not self.tools._evidence_ok(ctx, row):
                continue
            tier = op._link_tier(row, ends)
            if tier != FIRM and not (tier in op._IDENTITY_TIERS and compared_twin(
                    row["from_object"], row["to_object"])):
                continue
            out += [e for e in (row["from_object"], row["to_object"])
                    if e not in members and e not in out]
        return out, []

    def values(self, oid: str) -> tuple[dict[str, list[Any]], list[dict[str, Any]], bool]:
        """The status values the ``open_when`` rules name for this record's binding, cited;
        a withheld one is not read (hidden)."""
        from operating.ontology_tools import _value_out

        ctx = self.ctx
        obj = ctx.obj(oid)
        values: dict[str, list[Any]] = {}
        cited: list[dict[str, Any]] = []
        hidden = False
        for rule in (self.graph.get("open_when") or {}).get("rules", ()):
            prop = rule["property"]
            if rule["binding"] != obj["binding_id"]:
                continue
            if prop in ctx.withheld(oid):
                hidden = True
                continue
            for row in ctx.values(oid).get(prop, ()):
                values.setdefault(prop, []).append(row["value"])
                out = (_value_out(row, obj["object_type"], 200)[0]
                       if row["value"] is not None else None)
                if out is not None:
                    cited.append(out)
        return values, cited, hidden

    def _quantities(self, lid: str, names: Sequence[str]) -> tuple[Any, ...]:
        """One line's raw quantities ``names`` and the first one's unit: no value is None
        (counted 0 when received), a withheld or disputed quantity or a unit other than
        the first one's is ``UNREAD``."""
        read = []
        for name in names:
            if name in self.ctx.withheld(lid):
                return (UNREAD,) * (len(names) + 1)
            values = {(row["value"], row["unit"]) for row in self.ctx.values(lid).get(name, ())
                      if row["value"] is not None}
            if len(values) > 1:
                return (UNREAD,) * (len(names) + 1)
            read.append(next(iter(values), (None, None)))
        unit = read[0][1]
        if any(value is not None and other != unit for value, other in read[1:]):
            return (UNREAD,) * (len(names) + 1)
        return (*(value for value, _ in read), unit)

    def lines(self, oid: str) -> tuple[list | None, list, bool, list]:
        """This record's lines (the graph's ``lines`` declaration) as triples; None when
        not proven complete for this reader (hidden when a fuller grant would read them)."""
        spec = self.spec
        if not spec:
            return None, [], False, []
        reach = self._reach(self.graph["root_type"], oid, spec["paths"], spec["object_type"])
        if not (reach.proven and not reach.truncated):
            return None, [], self.op._HIDDEN in reach.causes, []
        ids = [lid for lid in sorted(reach.records)
               if self.ctx.obj(lid)["object_type"] == spec["object_type"]]
        from operating.ontology_tools import _value_out

        names = (spec["ordered"], spec["received"])
        hidden = any(name in self.ctx.withheld(lid) for lid in ids for name in names)
        # the received values cited (a systems-disagree conflict on the received quantity
        # cites them); the status's own evidence stays its status values
        cited = [out for lid in ids if spec["received"] not in self.ctx.withheld(lid)
                 for out in (_value_out(row, spec["object_type"], 200)[0]
                             for row in self.ctx.values(lid).get(spec["received"], ())
                             if row["value"] is not None) if out is not None]
        return [self._quantities(lid, names) for lid in ids], cited, hidden, []

    def fm_lines(self, fm: str) -> tuple[Any, list, bool, list]:
        """The FM twin's items (``line_of``) as ``(qty, QC received, None)``, the QC
        receiving log (``receives_line`` from the FM receipt relations) summed per item;
        ``UNREAD`` when the items or a QC quantity are not proven readable for this reader
        (a QC row without a quantity is unread, never 0)."""
        ctx, op = self.ctx, self.op
        reach = self._reach(PO_TYPE, fm, [[{"link": LINE_OF, "direction": "reverse"}]],
                            LINE_TYPE, {FM_ITEM_BINDING})
        if not reach.proven or reach.truncated:
            return UNREAD, [], op._HIDDEN in reach.causes, []
        out = []
        for item in sorted(r for r in reach.records if ctx.obj(r)["object_type"] == LINE_TYPE):
            want, _unit = self._quantities(item, (ORDERED,))
            qc = self._reach(LINE_TYPE, item, [[{"link": RECEIVES_LINE,
                                                 "direction": "reverse"}]],
                             RECEIPT_LINE_TYPE, FM_QC_BINDINGS)
            if not qc.proven or qc.truncated:
                return UNREAD, [], op._HIDDEN in qc.causes, []
            got: Any = None
            for row in sorted(r for r in qc.records
                              if ctx.obj(r)["object_type"] == RECEIPT_LINE_TYPE):
                amount, _unit = self._quantities(row, (RECEIVED_QTY,))
                number = None if amount in (None, UNREAD) else _number(amount)
                if number is None:
                    return UNREAD, [], amount is UNREAD, []
                got = (got or Decimal(0)) + number
            out.append((want, got, None))
        return out, [], False, []

    def line_report(self, oid: str, spec: Mapping[str, Any] | None
                    ) -> list[dict[str, Any]]:
        """Each line reached by the lateness ``lines`` declaration with its state
        (not_received, partial, received, unknown), its own expected date when declared
        (BC expectedReceiptDate: reported, never a due date) and its cited values."""
        from operating.ontology_tools import _value_out

        if not spec:
            return []
        ctx, op = self.ctx, self.op
        tid = spec["object_type"]
        reach = self._reach(self.graph["root_type"], oid, spec["paths"], tid)
        out = []
        for lid in sorted(r for r in reach.records if ctx.obj(r)["object_type"] == tid):
            ordered, ounit, ocited = self._one(lid, spec["ordered"])
            received, runit, rcited = self._one(lid, spec["received"])
            line: dict[str, Any] = {"line": lid, "state": "unknown"}
            if ordered is not None and received is not None and ounit == runit and ordered > 0:
                line["state"] = ("not_received" if received <= 0 else
                                 "partial" if received < ordered else "received")
            values = ocited + rcited
            if "expected" in spec and spec["expected"] not in ctx.withheld(lid):
                rows = [row for row in ctx.values(lid).get(spec["expected"], ())
                        if row["value"] is not None]
                days = {op._date(row["value"]) for row in rows}
                if len(days) == 1 and None not in days:
                    line["expected_date"] = next(iter(days)).isoformat()
                    values += [v for v in (_value_out(r, tid, 200)[0] for r in rows) if v]
            if values:
                line["values"] = values
            out.append(line)
        return out

    def _one(self, rid: str, name: str) -> tuple[Decimal | None, str | None, list]:
        """One readable quantity of ``rid`` (all its sources agreeing on value and unit),
        its unit and the cited values; (None, None, cited) otherwise."""
        from operating.ontology_tools import _value_out

        rows = [row for row in self.ctx.values(rid).get(name, ()) if row["value"] is not None]
        cited = [v for v in (_value_out(row, self.ctx.obj(rid)["object_type"], 200)[0]
                             for row in rows) if v is not None]
        try:
            found = {(Decimal(str(row["value"])), row["unit"]) for row in rows}
        except InvalidOperation:
            return None, None, cited
        if len(found) != 1 or name in self.ctx.withheld(rid) or len(cited) != len(rows):
            return None, None, cited
        (amount, unit), = found
        return amount, unit, cited

    def records(self, oid: str, spec: Mapping[str, Any]) -> "Records":
        """What one ``promised``/``received`` declaration reaches for a root: the records,
        their dated events (cited), their ``order_by`` date, and whether the read is
        proven, hidden or reaches a lead only."""
        from operating.ontology_tools import _markings

        ctx, op = self.ctx, self.op
        schema = self.tools.base.schema
        tid = spec["object_type"]
        reach = self._reach(self.graph["root_type"], oid, spec["paths"], tid)
        out = Records(records=sorted(r for r in reach.records
                                     if ctx.obj(r)["object_type"] == tid))
        marks = {m for e in spec["event_types"]
                 for m in schema.event_types[e].get("markings", ())}
        out.hidden = not ctx.scope.holds(marks) or op._HIDDEN in reach.causes
        uncited = False
        out.dates = {r: [] for r in out.records}
        for _, subject, row in self.tools._visible_events(ctx, out.records,
                                                          spec["event_types"]):
            cite, day = op._cite(row), op._date(row["time"])
            if cite is None or day is None or subject not in out.dates:
                uncited = True
                continue
            out.dates[subject].append((day, {
                "property": row["event_type"].split(".", 1)[1], "value_type": "date",
                "value": day.isoformat(), "citation": cite,
                "markings": list(_markings(row["markings"]))}))
        order_by = spec.get("order_by")
        for rid in out.records if order_by else ():
            rows = [row for row in ctx.values(rid).get(order_by, ()) if row["value"] is not None]
            days = {op._date(row["value"]) for row in rows}
            out.order[rid] = (next(iter(days)) if len(days) == 1 and None not in days
                              and order_by not in ctx.withheld(rid) else None)
        out.proven = reach.proven and not reach.truncated and not out.hidden and not uncited
        # unproven only because a path reached a lead (below the tier floor): a lead is
        # never counted and hides nothing a counted read holds (``_Why`` tier ``lead``)
        lead_only = (not reach.proven and bool(reach.reasons)
                     and all(w.tier == op._LEAD for w in reach.reasons))
        out.counted_proven = out.proven or (lead_only and not reach.truncated
                                            and not out.hidden and not uncited)
        out.lead_only = not out.records and not out.hidden and bool(reach.leads)
        out.detail = "; ".join(dict.fromkeys(w.text for w in reach.reasons))
        return out

    def need_dates(self, oid: str, members: Sequence[str]) -> list[tuple[int, date | None, Any]]:
        """The stage's declared planned dates (``_due_date``, kept in ``dated``: every
        source, never repaired), each ranked by the declared precedence."""
        if self.stage is None:
            return []
        self.dated = self.tools._due_date(self.ctx, self.graph, oid, self.stage,
                                          self.conflicts)
        tier_of = event_ranks(self.stage["due"])
        out = []
        for value in self.dated.values:
            rank = tier_of.get(value["property"])
            if rank is not None:
                day = self.op._date(value["value"])
                out.append((rank, day, {**value, "authority_rank": rank}
                            if day is not None else value))
        return out

    def line_dates(self, members: Sequence[str]) -> list[tuple[date | None, Any]]:
        """Each open line's own expected date (the lateness ``lines`` declaration's
        ``expected``), cited; ``None`` for an open line whose date is withheld, disputed or
        absent (open until proven otherwise, also when its quantities are unread)."""
        from operating.ontology_tools import _value_out

        spec = self.spec
        if not spec or "expected" not in spec:
            return []
        ctx, out = self.ctx, []
        for member in members:
            reach = self._reach(self.graph["root_type"], member, spec["paths"],
                                spec["object_type"])
            for lid in sorted(r for r in reach.records
                              if ctx.obj(r)["object_type"] == spec["object_type"]):
                ordered, received, unit = self._quantities(lid, (spec["ordered"],
                                                                 spec["received"]))
                if UNREAD not in (ordered, received, unit) and not line_open(ordered, received):
                    continue
                rows = [row for row in ctx.values(lid).get(spec["expected"], ())
                        if row["value"] is not None]
                days = {self.op._date(row["value"]) for row in rows}
                if spec["expected"] in ctx.withheld(lid) or len(days) != 1 or None in days:
                    out.append((None, lid))
                    continue
                cited = [v for v in (_value_out(r, spec["object_type"], 200)[0] for r in rows)
                         if v]
                out.append((next(iter(days)), cited[0] if len(cited) == len(rows) else lid))
        return out

    def promises(self, oid: str, members: Sequence[str]) -> list[tuple[Any, date, Any]]:
        """The supplier's promised dates the ``promised`` declaration reaches at the
        graph's tier floor (kept in ``promised``), with their document dates."""
        spec = ((self.stage or {}).get("due") or {}).get("lateness", {}).get("promised")
        if not spec:
            return []
        self.promised = self.records(oid, spec)
        return [(self.promised.order.get(r), day, value) for r in self.promised.records
                for day, value in self.promised.dates[r]]


@dataclass
class Records:
    """What one ``promised``/``received`` declaration reaches for a root."""
    records: list[str] = field(default_factory=list)
    dates: dict[str, list[tuple[date, dict[str, Any]]]] = field(default_factory=dict)
    order: dict[str, date | None] = field(default_factory=dict)
    proven: bool = True
    hidden: bool = False
    lead_only: bool = False
    detail: str = ""
    # proven but for leads: every record a counted link reaches is read (lateness)
    counted_proven: bool = True


# ---- the system store (the proactive engine: every current record, set-based)

class StoreSource:
    """The proactive engine's read of the whole store (``st``: its ``_Store``), preloaded
    set-wise: every PurchaseOrder with its status, need-date and number values, its
    settled identity clusters (``SAME_RECORD`` at ``SETTLED`` within a system; a settled
    twin across BC is a firm one, ``compared_twin``), its lines (``LINE_OF`` at
    ``CONFIRMING``) and their quantities, the FM QC receipt lines (``RECEIVES_LINE`` at
    ``CONFIRMING``) and every confirmation link at any tier with its dates. Evidence is
    the store's value rows; marks are the links' markings (the engine cites with them)."""

    STATUS_PROPS = tuple(SOURCE_OF)

    def __init__(self, st: Any) -> None:
        self.st = st
        self.pos = st.values((PO_TYPE,), (*SOURCE_OF, NEED_PROP, "po_number"))
        settled = st.links(TWIN_LINKS, SETTLED)
        same = [l for l in settled if not compared_twin(l["from_object"], l["to_object"])]
        crossing = [l for l in settled if compared_twin(l["from_object"], l["to_object"])]
        root = clusters(((l["from_object"], l["to_object"]) for l in same), self.pos)
        self.identity: dict[str, list[str]] = defaultdict(list)
        for link in same:
            if link["from_object"] in root:
                self.identity[root[link["from_object"]]].append(link["markings"])
        self.clusters: dict[str, list[str]] = defaultdict(list)
        for object_id, top in root.items():
            self.clusters[top].append(object_id)
        self.root = root
        self.firm: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for link in [*st.links(TWIN_LINKS, (FIRM,)), *crossing]:
            self.firm[link["from_object"]].append((link["to_object"], link["markings"]))
            self.firm[link["to_object"]].append((link["from_object"], link["markings"]))
        self.lines_of: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.po_of: dict[str, list[str]] = defaultdict(list)
        for link in st.links((LINE_OF,), CONFIRMING):
            self.lines_of[link["to_object"]].append((link["from_object"], link["markings"]))
            self.po_of[link["from_object"]].append(link["to_object"])
        wanted = sorted({line for found in self.lines_of.values() for line, _ in found})
        self.line_values = st.values((LINE_TYPE,), (ORDERED, RECEIVED_QTY, LINE_EXPECTED),
                                     objects=wanted) if wanted else {}
        self.qc_of: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for link in st.links((RECEIVES_LINE,), CONFIRMING):
            if binding_of(link["from_object"]) in FM_QC_BINDINGS:
                self.qc_of[link["to_object"]].append((link["from_object"], link["markings"]))
        qc = sorted({r for found in self.qc_of.values() for r, _ in found})
        self.qc_values = st.values((RECEIPT_LINE_TYPE,), (RECEIVED_QTY,),
                                   objects=qc) if qc else {}
        self.confirms_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for link in st.links((CONFIRMS,)):
            self.confirms_of[link["to_object"]].append(link)
        self.confirmations = st.values((CONFIRMATION_TYPE,), (PROMISE_PROP, PROMISE_ORDER))

    def props(self, oid: str) -> dict[str, list[dict[str, Any]]]:
        return (self.pos.get(oid) or {"props": {}})["props"]

    def twins(self, oid: str) -> tuple[list[str], bool, list[str]]:
        top = self.root.get(oid, oid)
        return sorted(self.clusters.get(top, [oid])), False, self.identity.get(top, [])

    def date_twins(self, oid: str, members: Sequence[str]) -> tuple[list[str], list[str]]:
        """The records a firm twin link joins to a member (each with its settled
        cluster), and the markings of the links read."""
        out: list[str] = []
        marks: list[str] = []
        for member in members:
            for other, link_marks in self.firm.get(member, []):
                if other in members:
                    continue
                marks.append(link_marks)
                top = self.root.get(other, other)
                out += [m for m in self.clusters.get(top, [other]) if m not in out]
        return out, marks

    def values(self, oid: str) -> tuple[dict[str, list[Any]], list[dict[str, Any]], bool]:
        props = self.props(oid)
        return ({p: [r["value"] for r in props.get(p, [])] for p in SOURCE_OF if props.get(p)},
                [r for p in SOURCE_OF for r in props.get(p, [])], False)

    @staticmethod
    def _one(rows: Sequence[Mapping[str, Any]]) -> Any:
        """The one (number, unit) the rows state (unit None when none is recorded); None:
        no row or only null values (counted 0 received, as the Portal sums); ``UNREAD``: a
        value that does not parse, or disagreeing values."""
        from ontology.units import unit_token

        found = {(_number(r["value"]), unit_token(r["unit"]))
                 for r in rows if r["value"] is not None}
        if not found:
            return None
        if len(found) > 1 or any(number is None for number, _ in found):
            return UNREAD
        return found.pop()

    def lines(self, oid: str) -> tuple[list, list[dict[str, Any]], bool, list[str]]:
        triples, rows, marks = [], [], []
        for line, link_marks in self.lines_of.get(oid, []):
            props = (self.line_values.get(line) or {"props": {}})["props"]
            want, got = self._one(props.get(ORDERED, [])), self._one(props.get(RECEIVED_QTY, []))
            rows += props.get(ORDERED, []) + props.get(RECEIVED_QTY, [])
            marks.append(link_marks)
            if UNREAD in (want, got) or (want and got and want[1] != got[1]):
                triples.append((UNREAD,) * 3)
            else:
                triples.append((want[0] if want else None, got[0] if got else None,
                                want[1] if want else None))
        return triples, rows, False, marks

    def line_dates(self, members: Sequence[str]) -> list[tuple[date | None, Any]]:
        """Each open line's own expected date, its row the item (``None`` day: undated)."""
        out = []
        for member in members:
            for line, _marks in self.lines_of.get(member, []):
                props = (self.line_values.get(line) or {"props": {}})["props"]
                want = self._one(props.get(ORDERED, []))
                got = self._one(props.get(RECEIVED_QTY, []))
                if UNREAD not in (want, got) and not line_open(
                        want[0] if want else None, got[0] if got else None):
                    continue
                rows = [r for r in props.get(LINE_EXPECTED, []) if r["value"] is not None]
                days = {day_of(r["value"]) for r in rows}
                out.append((next(iter(days)) if len(days) == 1 and None not in days else None,
                            rows[0] if len(rows) == 1 else line))
        return out

    def fm_lines(self, fm: str) -> tuple[Any, list[dict[str, Any]], bool, list[str]]:
        out, rows, marks = [], [], []
        for item, link_marks in self.lines_of.get(fm, []):
            props = (self.line_values.get(item) or {"props": {}})["props"]
            want = self._one(props.get(ORDERED, []))
            rows += props.get(ORDERED, [])
            marks.append(link_marks)
            got: Any = None
            for receipt, qc_marks in self.qc_of.get(item, []):
                found = self._one((self.qc_values.get(receipt) or {"props": {}})["props"]
                                  .get(RECEIVED_QTY, []))
                if found is None or found is UNREAD:
                    return UNREAD, rows, False, marks   # a QC row with no quantity: unread
                rows += self.qc_values[receipt]["props"][RECEIVED_QTY]
                marks.append(qc_marks)
                got = (got or Decimal(0)) + found[0]
            out.append((UNREAD if want is UNREAD else want[0] if want else None, got, None))
        return out, rows, False, marks

    def need_dates(self, oid: str, members: Sequence[str]) -> list[tuple[int, date | None, Any]]:
        return [(rank_of(m), day_of(row["value"]), row) for m in members
                for row in self.props(m).get(NEED_PROP, [])]

    def confirm_links(self, members: Sequence[str]) -> list[dict[str, Any]]:
        """Every confirmation link to a member, at any tier (the engine settles them)."""
        return [link for m in members for link in self.confirms_of.get(m, [])]

    def promises(self, oid: str, members: Sequence[str]) -> list[tuple[Any, date, Any]]:
        """The delivery dates of the confirmations linked at ``CONFIRMING`` tiers, each
        item ``(row, link)``; a confirmation whose date does not parse states none."""
        out = []
        for link in self.confirm_links(members):
            if link["tier"] not in CONFIRMING:
                continue
            props = (self.confirmations.get(link["from_object"]) or {"props": {}})["props"]
            orders = {day_of(r["value"]) for r in props.get(PROMISE_ORDER, [])
                      if r["value"] is not None}
            order = next(iter(orders)) if len(orders) == 1 else None
            for row in props.get(PROMISE_PROP, []):
                day = day_of(row["value"])
                if day is not None:
                    out.append((order, day, (row, link)))
        return out


# ---- the plx tools as one principal calls them (the eval check)

def tool_values(page: Mapping[str, Any]) -> dict[str, list[Any]] | None:
    """A ``plx.object`` answer's values per property, raw (a text value's untrusted text,
    a quantity with its unit); None when the answer withholds or cannot read one."""
    if "error" in page or page.get("withheld") or any(
            (g or {}).get("reason") in ("unparseable_value", "uncitable_value")
            for g in page.get("gaps") or []):
        return None
    out: dict[str, list[Any]] = {}
    for value in page.get("values") or []:
        raw = value.get("value")
        if isinstance(raw, Mapping):
            raw = raw.get("untrusted_text")
        out.setdefault(str(value.get("property")), []).append(
            (raw, value.get("unit")) if value.get("value_type") == "quantity" else raw)
    return out


class ToolSource:
    """The ``plx`` tools as one principal calls them (``call(name, args)``): every read is
    that principal's, filtered and marked by the served host. A listing the tool does not
    say is complete, or that withholds a record, is unread (twins: blind)."""

    def __init__(self, call: Callable[[str, dict], Mapping[str, Any]]) -> None:
        self.call = call

    def _around(self, oid: str, link: str, direction: str, prefix: str,
                floor: str | None = None) -> tuple[list[str] | None, bool]:
        """The object ids of ``prefix`` one hop away, every page; (None, withheld) when
        the listing is not complete for this reader."""
        from operating import ontology_tools_traverse as tt

        ids: list[str] = []
        cursor = None
        for _ in range(PAGES):
            page = self.call("plx.around", {
                "from": oid, "limit": 50, "path": [{"link_type": link, "direction": direction}],
                **({"tier_floor": floor} if floor else {}),
                **({"cursor": cursor} if cursor else {})})
            # plx.around's ``withheld`` block is constant by design (it never reveals
            # what a grant removed); a path that lost records says so in its gaps
            lost = {(g or {}).get("reason") for g in page.get("gaps") or ()} \
                & tt.PERMISSION_LOSSES
            if "error" in page or lost or (page.get("withheld") or {}).get("objects"):
                return None, bool(lost) or "error" not in page
            ids += [o["object_id"] for o in page.get("objects") or []
                    if str(o.get("object_id", "")).startswith(prefix)]
            completeness = page.get("completeness") or {}
            cursor = completeness.get("next_cursor")
            if not cursor:
                if completeness.get("complete") is not True:
                    return None, False
                return list(dict.fromkeys(ids)), False
        return None, False

    def twins(self, oid: str) -> tuple[list[str], bool, list]:
        members, frontier, blind = [oid], [oid], False
        while frontier and len(members) <= MAX_TWINS:
            nxt = []
            for member in frontier:
                for direction in ("forward", "reverse"):
                    found, _ = self._around(member, SAME_RECORD, direction, f"{PO_TYPE}:",
                                            SETTLED[-1])
                    if found is None:
                        blind = True
                        continue
                    for twin in found:
                        if twin not in members and not compared_twin(member, twin):
                            members.append(twin)
                            nxt.append(twin)
            frontier = nxt
        if frontier:
            blind = True   # more twins than read: never decided from a part
        return members, blind, []

    def date_twins(self, oid: str, members: Sequence[str]) -> tuple[list[str], list[str]]:
        """The records a firm twin link joins to a member (``plx.around`` at the
        ``corroborated`` floor, this principal's), their dates read, never their status;
        a listing this principal cannot read in full finds none."""
        out: list[str] = []
        for member in members:
            for link in TWIN_LINKS:
                for direction in _directions(link, member):
                    found, _ = self._around(member, link, direction, f"{PO_TYPE}:", FIRM)
                    out += [t for t in found or () if t not in members and t not in out]
        return out, []

    def values(self, oid: str) -> tuple[dict[str, list[Any]], list, bool]:
        values = tool_values(self.call("plx.object", {"object_id": oid,
                                                      "properties": list(SOURCE_OF)}))
        if values is None:
            return {}, [], True
        return {k: v for k, v in values.items() if k in SOURCE_OF}, [], False

    def _quantities(self, line: str, names: Sequence[str]) -> tuple[Any, ...] | None:
        values = tool_values(self.call("plx.object", {"object_id": line,
                                                      "properties": list(names)}))
        if values is None:
            return None
        read = []
        for name in names:
            # a stored null (the Portal's empty received cell) states no quantity: null
            found = {v for v in values.get(name, []) if v[0] is not None}
            if len(found) > 1:
                return (UNREAD,) * (len(names) + 1)
            read.append(next(iter(found), (None, None)))
        unit = read[0][1]
        if any(value is not None and other != unit for value, other in read[1:]):
            return (UNREAD,) * (len(names) + 1)
        return (*(value for value, _ in read), unit)

    def lines(self, oid: str) -> tuple[list | None, list, bool, list]:
        ids, withheld = self._around(oid, LINE_OF, "reverse", f"{LINE_TYPE}:")
        if ids is None:
            return None, [], withheld, []
        out = []
        for line in ids:
            read = self._quantities(line, (ORDERED, RECEIVED_QTY))
            if read is None:
                return None, [], True, []
            out.append(read)
        return out, [], False, []

    def fm_lines(self, fm: str) -> tuple[Any, list, bool, list]:
        items, withheld = self._around(fm, LINE_OF, "reverse", f"{LINE_TYPE}:{FM_ITEM_BINDING}:")
        if items is None:
            return UNREAD, [], withheld, []
        out = []
        for item in items:
            want = self._quantities(item, (ORDERED,))
            receipts, withheld = self._around(item, RECEIVES_LINE, "reverse",
                                              f"{RECEIPT_LINE_TYPE}:")
            if want is None or receipts is None:
                return UNREAD, [], withheld or want is None, []
            got: Any = None
            for receipt in receipts:
                if binding_of(receipt) not in FM_QC_BINDINGS:
                    continue
                read = self._quantities(receipt, (RECEIVED_QTY,))
                number = None if read is None or read[0] in (None, UNREAD) else _number(read[0])
                if number is None:
                    return UNREAD, [], read is None, []
                got = (got or Decimal(0)) + number
            out.append((want[0], got, None))
        return out, [], False, []

    def line_dates(self, members: Sequence[str]) -> list[tuple[date | None, Any]]:
        """Each open line's own expected date as this principal reads it, ``(member line,
        raw value)`` its item; ``None`` for an open line whose list, quantities or date
        this principal cannot read in full (open until proven otherwise)."""
        out: list[tuple[date | None, Any]] = []
        for member in members:
            ids, _ = self._around(member, LINE_OF, "reverse", f"{LINE_TYPE}:")
            if ids is None:
                out.append((None, member))
                continue
            for line in ids:
                read = self._quantities(line, (ORDERED, RECEIVED_QTY))
                if read is not None and UNREAD not in read and not line_open(
                        read[0], read[1]):
                    continue
                values = tool_values(self.call("plx.object", {
                    "object_id": line, "properties": [LINE_EXPECTED]}))
                raws = {v for v in (values or {}).get(LINE_EXPECTED, []) if v is not None}
                days = {day_of(v) for v in raws}
                out.append((next(iter(days)) if len(days) == 1 and None not in days else None,
                            (line, next(iter(raws))) if len(raws) == 1 else line))
        return out

    def need_dates(self, oid: str, members: Sequence[str]) -> list[tuple[int, date | None, Any]]:
        out = []
        for member in members:
            values = tool_values(self.call("plx.object", {"object_id": member,
                                                          "properties": [NEED_PROP]}))
            for raw in (values or {}).get(NEED_PROP, []):
                if raw is not None:
                    out.append((rank_of(member), day_of(raw), (member, raw)))
        return out

    def promises(self, oid: str, members: Sequence[str]) -> list[tuple[Any, date, Any]]:
        out = []
        for member in members:
            found, _ = self._around(member, CONFIRMS, "reverse", f"{CONFIRMATION_TYPE}:",
                                    CONFIRMING[-1])
            if found is None:
                return None
            for confirmation in found:
                values = tool_values(self.call("plx.object", {
                    "object_id": confirmation, "properties": [PROMISE_PROP, PROMISE_ORDER]}))
                if values is None:
                    return None
                orders = {day_of(v) for v in values.get(PROMISE_ORDER, []) if v is not None}
                order = next(iter(orders)) if len(orders) == 1 else None
                for raw in values.get(PROMISE_PROP, []):
                    day = day_of(raw)
                    if day is not None:
                        out.append((order, day, (confirmation, raw)))
        return out


# --------------------------------------------------------------------------- vocabulary

# The relations read, by the short name the summary keys them under.
RELATIONS = {"portal_po": "public.app_purchase_order",
             "portal_line": "public.app_purchase_order_line",
             "fm_po": "public.generated_purchase_order", "sage_po": "sage.po_header",
             # the FM QC receiving log: items (gpi.idpogen = gpo.id, qty) and the receipt
             # details against them (idpogenitem = gpi.id, sumqty: the noitemsqc stand-in)
             "fm_item": "public.generated_purchase_order_item",
             "fm_receipt_raw": "public.receipt_raw_material",
             "fm_receipt_pack": "public.receipt_packaging"}
FM_RECEIPTS = ("fm_receipt_raw", "fm_receipt_pack")
MAX_ROWS = 50_000
MAX_WORD = 40
# The engine's rule before this definition (header codes only), for the before/after count.
_BEFORE_OPEN, _BEFORE_CLOSED = frozenset({SUBMITTED, PARTIAL}), frozenset({RECEIVED, CANCELLED})


def _shown(raw: Any) -> str:
    """A status word as listed (an enum the live record may copy): its trimmed text with
    any other character than ``[A-Za-z0-9_.:/-]`` as ``_``, clipped; ``_null_`` for a
    null, ``_blank_`` for an empty word."""
    if raw is None:
        return "_null_"
    text = re.sub(r"[^A-Za-z0-9_.:/\-]", "_", " ".join(str(raw).split()))[:MAX_WORD]
    return text or "_blank_"


def _fm_id(raw: Any) -> str | None:
    """``custom_fields.fmGeneratedPoId`` (a dict or its JSON text) as an integer id's
    text; None when absent or not an integer (the link contract compares integer ids)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    value = raw.get("fmGeneratedPoId") if isinstance(raw, Mapping) else None
    number = _number(value)
    return str(int(number)) if number is not None and number == int(number) else None


def _fm_log(scans: Mapping[str, Mapping[str, Any]]) -> Callable[[str], Any]:
    """FM PO id -> its items as ``(qty, QC received or None, None)`` from the scans; the
    log is ``UNREAD`` for every PO when the FM PO or item scan is not complete, and for a
    PO an item of which a receipt scan cannot prove (not complete, or a receipt row
    against it with no ``sumqty``: the packaging catalog lacks it)."""
    fm_scan, item_scan = scans.get("fm_po") or {}, scans.get("fm_item") or {}
    if not (fm_scan.get("complete") and item_scan.get("complete")):
        return lambda fm_id: UNREAD
    fm_ids = {str(r.get("id")) for r in fm_scan.get("rows") or ()}
    items: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    for row in item_scan.get("rows") or ():
        items[str(row.get("idpogen"))].append((str(row.get("id")), row.get("qty", UNREAD)))
    got: dict[str, Any] = {}
    unproven = False
    for name in FM_RECEIPTS:
        scan = scans.get(name) or {}
        unproven = unproven or not scan.get("complete")
        for row in scan.get("rows") or ():
            item = str(row.get("idpogenitem"))
            raw = row.get("sumqty", UNREAD)   # no such column: unread, never 0
            amount = None if raw is UNREAD else _number(raw)
            if amount is None or got.get(item) is UNREAD:
                got[item] = UNREAD   # a receipt detail against it with no quantity
            else:
                got[item] = got.get(item, Decimal(0)) + amount

    def log(fm_id: str) -> Any:
        if fm_id not in fm_ids or unproven:
            return UNREAD
        return [(qty, got.get(item), None) for item, qty in items.get(fm_id, ())]
    return log


def vocabulary(scans: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Counts only from the relation scans (``scan_relation`` results with each row's
    cells, keyed as ``RELATIONS``): per raw status word the classes the shared definition
    gives its rows, the class totals, the unknown rows by word and by reason, the old
    rule's counts, and Sage iscomplete against linescmpl >= lines."""
    out: dict[str, Any] = {"relations": {}, "words": {}, "classes": {}, "unknown_words": {},
                           "unknown_reasons": {}, "before_engine_rule": {}, "complete_open": 0,
                           "canceled_remainder": {}}
    for name in RELATIONS:
        scan = scans.get(name) or {"status": "not_scanned", "rows": [], "complete": False,
                                   "reasons": ["not_scanned"]}
        out["relations"][name] = {"status": scan.get("status"), "complete": bool(
            scan.get("complete")), "rows": len(scan.get("rows") or ()),
            "reasons": list(scan.get("reasons") or ())}
    lines: dict[Any, list[tuple[Any, Any, Any]]] = defaultdict(list)
    line_scan = scans.get("portal_line") or {}
    for row in line_scan.get("rows") or ():
        lines[row.get("purchase_order_id")].append(
            (row.get("quantity", UNREAD), row.get("quantity_received", UNREAD),
             row.get("uom", UNREAD)))
    lines_complete = bool(line_scan.get("complete"))
    fm_log = _fm_log(scans)
    for name, source in (("portal_po", "portal"), ("fm_po", "fm")):
        scan = scans.get(name) or {}
        words: dict[str, Counter] = defaultdict(Counter)
        classes, unknown, reasons, before = Counter(), Counter(), Counter(), Counter()
        without_qc, qc = Counter(), Counter()
        for row in scan.get("rows") or ():
            raw = row.get("status")
            if source == "portal" and lines_complete:
                ordered, received = line_totals(lines.get(row.get("id"), ()))
            else:   # the FM header carries no received quantity; an unread line scan
                ordered = received = None
            if source == "portal":
                without_qc[classify(raw, ordered, received).cls] += 1
                fm_id = _fm_id(row.get("custom_fields"))
                if fm_id is not None and ordered is not None and received is not None \
                        and received <= EPS:
                    # an FM-mirrored PO whose gold received is the ETL-seeded 0
                    log = fm_log(fm_id)
                    ordered, received = _fm_received((ordered, received), log)
                    qc["mirrored_zero_received"] += 1
                    qc["unread" if received is None else
                       "qc_received" if received > EPS else "qc_none"] += 1
            status = classify(raw, ordered, received)
            if word(raw) in COMPLETE_WORDS and status.cls == OPEN:
                out["complete_open"] += 1
            if status.canceled_remainder is not None and status.canceled_remainder > EPS:
                out["canceled_remainder"][name] = out["canceled_remainder"].get(name, 0) + 1
            words[_shown(raw)][status.cls] += 1
            classes[status.cls] += 1
            if status.cls == UNKNOWN:
                unknown[_shown(raw)] += 1
                reasons[status.reason] += 1
            text = word(raw)
            before[OPEN if text in _BEFORE_OPEN else CLOSED if text in _BEFORE_CLOSED
                   else "neither"] += 1
        out["words"][name] = {w: dict(sorted(c.items())) for w, c in sorted(words.items())}
        out["classes"][name] = {cls: classes.get(cls, 0) for cls in CLASSES}
        out["unknown_words"][name] = dict(sorted(unknown.items()))
        out["unknown_reasons"][name] = dict(sorted(reasons.items()))
        out["before_engine_rule"][name] = {k: before.get(k, 0)
                                           for k in (OPEN, CLOSED, "neither")}
        if source == "portal":
            out["classes_before_fm_qc"] = {cls: without_qc.get(cls, 0) for cls in CLASSES}
            out["fm_qc"] = {k: qc.get(k, 0) for k in (
                "mirrored_zero_received", "qc_received", "qc_none", "unread")}
    # every row lands in one of the four classes (a row outside them would be unmapped)
    out["unmapped_rows"] = sum(
        len((scans.get(name) or {}).get("rows") or ()) - sum(out["classes"][name].values())
        for name in ("portal_po", "fm_po"))
    portal = out["classes"]["portal_po"]
    out["open_plus_closed"] = portal[OPEN] + portal[CLOSED]
    cross: dict[str, Counter] = defaultdict(Counter)
    for row in (scans.get("sage_po") or {}).get("rows") or ():
        total, done = _number(row.get("lines")), _number(row.get("linescmpl"))
        cross[_shown(row.get("iscomplete"))][
            "unknown" if total is None or done is None
            else "lines_complete" if done >= total else "lines_open"] += 1
    out["sage_iscomplete"] = {value: dict(sorted(c.items())) for value, c in sorted(cross.items())}
    return out


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None) -> int:
    from integrations.source_mirror import scan_relation
    from ontology.core import Actor

    from . import case_workspace_live as live

    parser = argparse.ArgumentParser(prog="python3 -m operating.po_status",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--host-config", type=Path, default=None)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    summary: dict[str, Any] = {"schema": SCHEMA, "evidence": live.MIRROR_EVIDENCE["label"],
                               "capture": None}
    if args.host_config is not None:
        code, summary["capture"] = live._host_cycle(args, env, host_options or {})
        if code != 0:
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    actor = Actor(args.tenant, frozenset(args.principal), args.clearance)
    runtime = live.offline_runtime(args.state_dir, args.mirror_audience, args.tenant)
    try:
        scans = {}
        for name, relation in RELATIONS.items():
            scan = scan_relation(runtime.mirror, actor, relation, MAX_ROWS, None)
            scans[name] = {**scan, "rows": [item["row"] for item in scan["rows"]]}
        summary["blocked_upstream"] = live.upstream_parts(live.portal_upstream(args.state_dir))
    finally:
        runtime.close()
    summary.update(vocabulary(scans))
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


__all__ = ["AT_RISK", "CANCELLED_WORDS", "CLASSES", "CLOSED", "CLOSED_WORDS", "COMPLETE",
           "COMPLETE_WORDS", "ContextSource", "DRAFT", "DRAFT_WORDS", "EPS", "HEADER_PROPS",
           "ISSUED_WORDS", "LATE", "NEITHER", "Need", "OPEN", "PAST_ORDERED", "PoRead",
           "PoStatus", "Promise", "StoreSource", "ToolSource", "UNKNOWN", "UNKNOWN_REASONS",
           "UNREAD", "bc", "classify", "clusters", "combine", "header_class",
           "lateness_verdict", "line_totals", "need_date", "promise", "read", "record",
           "sage", "verdict", "vocabulary", "word"]


if __name__ == "__main__":
    sys.exit(main())
