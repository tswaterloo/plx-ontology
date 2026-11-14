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
  and the PO has an FM twin, FileMaker's own receiving state of each item
  (``generated_purchase_order_item.status``, ``fm_receiving_status``: Complete, OverPO or
  Cancel settled, B.Order partly received, Pending or Generated nothing yet) decides what
  was received, provided the FM items' ordered total is the gold one; any other item word
  or FM total leaves the quantities unknown. An item FileMaker stores no state for (a
  store or mirror that does not carry it) takes the order's one FM header word, FileMaker
  being the system of record of its own order (``fm_by_header``: B.Order partly
  received, Pending, Generated or Revised nothing yet but a recorded receipt; a Complete
  or Cancel header closes by itself and states no quantity); it never rolls up over a receipt the Portal lines record. The QC receiving log's ``sumqty`` is never
  read as a quantity: on the mirror it is a FileMaker running total across the whole log
  (each export's rows in id order), not one receipt's quantity;
* a null or unmapped word (Revised, Generated, anything new), quantities that are not
  readable in one unit, or no ordered quantity at all is **unknown**, with its reason,
  and counted by every reader, never guessed;
* a BC order reads its own ``status`` (Draft: not issued) and its own lines: open while
  any line has an outstanding quantity, received once none has; ``fullyReceived`` only
  when no line is readable;
* a Sage order whose ``iscomplete`` is 1 is **closed** ``HISTORICAL_SAGE``: a completed
  historical Sage record, never judged for lateness (on every captured header iscomplete 1
  has linescmpl >= lines and 0 has fewer; the vocabulary check keeps that cross-tab); any
  other value (0, open in Sage; null; a new value) is **unknown**.

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
side's own cited items and who governs: an issued BC order's lines govern receipt state
(a BC Draft whose lines record no receipt states none: never received against;
Late = requested date passed and not fully received), so a BC side decides the status;
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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
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
# A Sage header Sage itself says is complete (iscomplete 1): a historical record, closed
# and never judged for lateness (``sage``).
HISTORICAL_SAGE = "HISTORICAL_SAGE"
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
# The late-list rule's undecided orders that it decides are never late (the one rule the
# engine's late count, Home, the late list, Ask and the eval oracle ``po_lateness`` share):
# a status no record settles as open (every ``UNKNOWN_REASONS``: needs check, never late,
# Sage and an unmapped word decide
# nothing), no requested-by date (never late, listed apart), and the at-risk-only
# confirmation skips of an order not yet late. Any other skip (two disputed due dates, an
# unparseable one, a reason not named here) may hide a late order: the count is not exact.
NEVER_LATE_SKIPS = frozenset({*UNKNOWN_REASONS, "no_need_date", "confirmation_unsettled",
                              "confirmation_undated", "promises_disagree"})


def late_count_exact(skips: Any) -> bool:
    """Whether a late count is exact under the one late-list rule given the rule's skips
    (reason -> count, the engine's ``not_evaluated``): every skip is of a reason the rule
    decides is never late. Anything unreadable is not exact (fails closed)."""
    if not isinstance(skips, Mapping):
        return False
    for reason, count in skips.items():
        if type(count) is not int or count < 0:
            return False
        if count and reason not in NEVER_LATE_SKIPS:
            return False
    return True


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


def bc(status: Any, fully_received: Any, lines: Any = None) -> PoStatus:
    """A Business Central purchase order, BC being the system of record of its own
    order: Draft is not issued; an issued order with readable ``lines`` (``(ordered,
    received, unit)``) is open while any line has an outstanding quantity and received
    once none has (the lines govern);
    with no readable line its ``fullyReceived`` flag states it."""
    if header_class(status, "bc") == NOT_ISSUED:
        return PoStatus(DRAFT, "DRAFT")
    if lines and all(UNREAD not in (want, got) for want, got, _ in lines):
        return (PoStatus(OPEN) if any(line_open(want, got) for want, got, _ in lines)
                else PoStatus(CLOSED, RECEIVED))
    flag = _flag(fully_received)
    if flag is None:
        return PoStatus(UNKNOWN, reason="status_missing")
    return PoStatus(CLOSED, RECEIVED) if flag else PoStatus(OPEN)


def sage(iscomplete: Any = None) -> PoStatus:
    """A Sage purchase order. ``iscomplete`` 1 is a completed, historical Sage record:
    closed ``HISTORICAL_SAGE``, never judged for lateness (decision
    2026-11-02-po-due-dates-vendor-promise-sage-history: on every captured header
    iscomplete 1 has linescmpl >= lines and 0 has fewer). Anything else (0: Sage shows it
    open; null or a value never seen) stays unknown (``sage_unmapped``), never open."""
    if word(iscomplete) == "1":
        return PoStatus(CLOSED, HISTORICAL_SAGE)
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
        statuses.append(classify_received(text, *totals))
    if values.get("bc_status") or values.get("bc_fully_received"):
        heads = {word(v) for v in values.get("bc_status") or ()}
        flags = {_flag(v) for v in values.get("bc_fully_received") or ()}
        statuses.append(PoStatus(UNKNOWN, reason="status_disputed")
                        if len(heads) > 1 or len(flags) > 1
                        else bc(next(iter(heads), None), next(iter(flags), None),
                                lines if values.get("bc_status") else None))
    if values.get("sage_is_complete"):
        statuses.append(sage(values["sage_is_complete"][0]))
    return combine(statuses)


def classify_received(raw: Any, ordered: Decimal | None, received: Any) -> PoStatus:
    """``classify``, with a received quantity FileMaker states only as partly received
    (``PARTIAL_RECEIPT``): an issued word is open ``PARTIAL``, a Complete one closed
    ``COMPLETE`` (its remainder unread)."""
    if received is PARTIAL_RECEIPT:
        if word(raw) in COMPLETE_WORDS:
            return PoStatus(CLOSED, COMPLETE)
        return classify(raw, None, None) if header_class(raw) != ISSUED \
            else PoStatus(OPEN, PARTIAL)
    return classify(raw, ordered, received)


def _fm_received(totals: tuple[Decimal | None, Decimal | None], fm_lines: Any
                 ) -> tuple[Decimal | None, Any]:
    """The gold ``(ordered, received)`` with FileMaker's item receiving states as the
    received quantity when gold received nothing (``record``); unchanged otherwise. A
    partly received item makes the received ``PARTIAL_RECEIPT`` (some, not all)."""
    ordered, received = totals
    if ordered is None or received is None or received > EPS:
        return totals
    if fm_lines is UNREAD:
        return None, None
    fm_lines = list(fm_lines)
    got_total, partial = Decimal(0), False
    for _, got, _ in fm_lines:
        if got is UNREAD or isinstance(got, _Unstated):
            return None, None
        if got is PARTIAL_RECEIPT:
            partial = True
            continue
        got = Decimal(0) if got is None else _number(got)
        if got is None:
            return None, None
        got_total += max(Decimal(0), got)
    if got_total <= EPS and not partial:
        return totals   # FileMaker received nothing either: the gold 0 stands
    fm_ordered, _ = line_totals((want, None, None) for want, _, _ in fm_lines)
    if fm_ordered is None or abs(fm_ordered - ordered) > EPS:
        return None, None   # no proof the FileMaker quantities are in the gold unit
    return (ordered, PARTIAL_RECEIPT) if partial else (ordered, got_total)


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


class _Partial:
    def __repr__(self) -> str:
        return "PARTIAL"


PARTIAL_RECEIPT = _Partial()   # an item FileMaker says is partly received (amount unread)


class _Unstated:
    """An FM item FileMaker serves no receiving state for (none stored, not withheld):
    ``receipt`` whether goods are recorded against it. Only the FM header decides it
    (``fm_by_header``); anywhere else it is unread."""

    def __init__(self, receipt: bool) -> None:
        self.receipt = receipt

    def __repr__(self) -> str:
        return "UNSTATED"


UNSTATED, UNSTATED_RECEIPT = _Unstated(False), _Unstated(True)
# FileMaker's receiving state of a PO item (``generated_purchase_order_item.status``):
# settled (nothing outstanding: received in full, over-received or the item cancelled),
# partly received (a back order), nothing received yet; any other word is unread.
FM_RECEIVING = "fm_receiving_status"
FM_ITEM_SETTLED = frozenset({"COMPLETE", "OVERPO", "CANCEL"})
FM_ITEM_CANCELLED = frozenset({"CANCEL"})
FM_ITEM_PARTIAL = frozenset({"B.ORDER"})
FM_ITEM_NOTHING = frozenset({"PENDING", "GENERATED"})
# FM header words of an issued order with nothing settled yet: where FileMaker serves no
# item state, such a header says nothing arrived but recorded receipts (``fm_by_header``).
# Revised is a FileMaker order revised after issue, never cancelled or closed.
FM_HEADER_NOTHING = frozenset({*FM_ITEM_NOTHING, "REVISED"})


# A FileMaker QC lot's status that says the goods never arrived usable (QC refused them):
# any other QC status (released, quarantined, finished, expired) is goods received.
FM_LOT_REFUSED = frozenset({"REJECTED"})


def fm_item_received(status: Any, ordered: Any, receipt: bool | None = None) -> Any:
    """What an FM item received by FileMaker's own state of it: its ordered quantity when
    settled, ``PARTIAL_RECEIPT`` when partly received, None when nothing yet; ``UNREAD``
    for any other word (or a settled item whose ordered quantity is unread).

    ``receipt``: whether FileMaker records goods received against the item (a receiving
    row, or a QC lot whose status is not a refusal: ``FM_LOT_REFUSED``); None when not
    read. FileMaker's own item state decides whenever it is served; where no state is
    served at all, a recorded receipt says the item was received (a receipt in any
    record closes it), and no receipt leaves it unread (never guessed open)."""
    text = word(status) if status is not UNREAD else None
    if text in FM_ITEM_CANCELLED and ordered in (None, UNREAD):
        return Decimal(0)   # cancelled: nothing outstanding, whatever it ordered
    # settled; with no readable ordered quantity what arrived is not stated in a
    # quantity, so unread (the header decides)
    if text in FM_ITEM_SETTLED:
        return UNREAD if ordered is UNREAD or ordered is None else ordered
    # a partial receipt never closes the item (owner, 2026-11-06): a back-ordered item,
    # or one FileMaker still calls waiting while goods are recorded against it, is partly
    # received; the order stays open
    if text in FM_ITEM_PARTIAL or (text in FM_ITEM_NOTHING and receipt):
        return PARTIAL_RECEIPT
    if text in FM_ITEM_NOTHING:
        return None
    if text is None and status is not UNREAD:
        # no state stored: a receipt alone does not say whether all arrived; only the
        # FM header decides such an item (``fm_by_header``), anywhere else it is unread
        return UNSTATED_RECEIPT if receipt else UNSTATED
    return UNREAD


def fm_by_header(items: Any, header: Sequence[Any] | None) -> Any:
    """An FM order's items with every item FileMaker serves no state for
    (``_Unstated``) decided by the order's one FM header word, FileMaker being the system
    of record of its own order: B.Order is partly received; Pending, Generated or
    Revised received nothing but what a recorded receipt shows (partly received). A
    closing header (Complete, Cancel) states no received quantity (it closes the order by
    itself, its remainder unread), so it, two header words, none or any other word is
    ``UNREAD``, never guessed. Items that carry their own state are unchanged."""
    if items is None or items is UNREAD:
        return items
    items = list(items)
    if not any(isinstance(got, _Unstated) for _, got, _ in items):
        return items
    words = {word(v) for v in header or ()}
    text = next(iter(words)) if len(words) == 1 else None
    out = []
    for want, got, unit in items:
        if isinstance(got, _Unstated):
            if text in FM_ITEM_PARTIAL:
                got = PARTIAL_RECEIPT
            elif text in FM_HEADER_NOTHING:
                got = PARTIAL_RECEIPT if got.receipt else None
            else:
                return UNREAD
            if got is UNREAD:
                return UNREAD
        out.append((want, got, unit))
    return out


def fm_rollup(items: Any) -> "PoStatus | None":
    """A FileMaker order's status rolled up from its items (``(ordered, received by
    fm_item_received, unit)`` each), never its header word, which FileMaker leaves stale
    (B.Order over items all Complete): every item settled is closed ``RECEIVED``; any item
    outstanding is open (``PARTIAL`` once something arrived, else ``SUBMITTED``). None
    when an item is unread or there are none: the header word then decides."""
    if items is None or items is UNREAD:
        return None
    items = list(items)
    if not items:
        return None
    got_any = open_any = False
    for want, got, _unit in items:
        if got is UNREAD or want is UNREAD or isinstance(got, _Unstated):
            return None   # an item state not stated: the header decides
        if got is PARTIAL_RECEIPT:
            got_any = open_any = True
            continue
        if got is None:
            open_any = True
            continue
        number, ordered = _number(got), _number(want)
        if number is None:
            return None
        if ordered is not None and number + EPS < ordered:
            open_any = True
        got_any = got_any or number > EPS
    if not open_any:
        return PoStatus(CLOSED, RECEIVED)
    return PoStatus(OPEN, PARTIAL if got_any else SUBMITTED)


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

PO_TYPE, LINE_TYPE = "PurchaseOrder", "PurchaseOrderLine"
CONFIRMATION_TYPE = "SupplierConfirmation"
SAME_RECORD = "PurchaseOrder.same_record_as"
# the BC order the Portal posted a gold PO as (the entity map's join, served at verified)
POSTED = "PurchaseOrder.bc_posted_as"
TWIN_LINKS = (SAME_RECORD, POSTED)
LINE_OF = "PurchaseOrderLine.line_of"
CONFIRMS = "SupplierConfirmation.confirms"
# Every link by which a supplier's own document answers an order: its confirmation, and
# its invoice for the order (an invoice confirms the order was taken and shipped).
CONFIRMATION_LINKS = (CONFIRMS, "SupplierInvoice.for_purchase_order")
# The order numbers a record is known by (a number another order also holds collides).
NUMBER_PROPS = ("po_number", "fm_po_number")


def _number_key(raw: Any) -> str:
    """An order number compared across systems: letters and digits, upper-cased, the
    leading "PO"/"#" marks and zeros dropped (PO#4442, PO4442 and 4442 are one number)."""
    text = re.sub(r"[^0-9A-Z]", "", str(raw or "").upper())
    text = re.sub(r"^PO", "", text)
    return text.lstrip("0") or text
ORDERED, RECEIVED_QTY = "ordered_quantity", "received_quantity"
NEED_PROP, PROMISE_PROP, PROMISE_ORDER = "expected_date", "delivery_date", "document_date"
# Every property a confirmation states its arrival promise in: its document's, and the
# supplier's own carrying message's (ontology/document_fields.py carrier_fields).
PROMISE_PROPS = (PROMISE_PROP, "mail_delivery_date")
# A confirmation's per-item promises (ontology/document_fields.py line_promises).
LINE_PROMISES = "line_promises"
# A line's own expected receipt date (BC expectedReceiptDate): the lowest requested-by tier,
# read only where no header tier holds a date.
LINE_EXPECTED = "bc_expected_receipt_date"
PORTAL_BINDING = "portal.app_purchase_order"
# FileMaker's receiving rows and QC lots against an item (``fm_item_received``'s receipt
# evidence): the receiving row's own status, and the QC status of the lot it received.
RECEIPT_TYPE, LOT_TYPE = "Receipt", "Lot"
RECEIPT_RECEIVES_LINE, QC_RECEIVES_LINE = "Receipt.receives_line", "ReceiptLine.receives_line"
RECEIVED_LOT = "ReceiptLine.received_lot"
FM_RECEIPT_BINDINGS = frozenset({"portal.fm_receipt", "portal.fm_receipt_raw_material",
                                 "portal.fm_receipt_packaging"})
RECEIPT_STATUS, LOT_QC = "fm_status", "qc_status"
# A record's own days: the date a form or import set to the day it wrote the record is no
# requested date (``source_defaults.own_day``).
# BC's last-modified day is not one: any later edit or receipt posting rewrites it.
from operating.source_defaults import OWN_DAY_PROPS  # noqa: E402 (the one list)
# The supplier a record is ordered from, as each system stores it (number collisions).
VENDOR_NUMBER_PROPS = ("vendor_number", "vendor_name")
FM_BINDING = "portal.fm_generated_purchase_order"
FM_ITEM_BINDING = "portal.fm_generated_purchase_order_item"
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
    # where the due date comes from: ``requested`` (a requested-by date a record states),
    # or, only when no record states one, a labelled source: ``vendor_eta`` (the
    # supplier's confirmed date) or ``lead_time`` (the order day plus its lead time)
    basis: str = "requested"
    marks: tuple = ()     # markings of the links a labelled due date is read through
    # dates the records hold that are defaults (a computed or bulk-written day, a
    # placeholder such as 1900-01-01): never a requested-by date (``_default_date``)
    defaulted: tuple = ()
    # the due date is one of the order's own days (``OWN_DAY``): kept and judged, but it
    # may be a form default, so the late rule says so at low priority (BIZ-007)
    doubt: str | None = None


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


def vendor_verdict(promise_day: date | None, measured: date | None, against: str
                   ) -> tuple[str, int | None]:
    """The order against the vendor's own promised date (never our requested-by date):
    ``late`` with the days when ``measured`` (the last receipt when fully received, else
    today) is after it, ``on_time`` (received by it), ``not_due`` (open, the date not
    passed), ``undetermined`` with no promise or no measured date."""
    if promise_day is None or measured is None:
        return "undetermined", None
    if measured > promise_day:
        return "late", (measured - promise_day).days
    return ("on_time" if against == "received" else "not_due"), None


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
    # each record as read ``(status values, lines, FM QC log)``, the settled side first:
    # ``describe`` says in words what they say (an undecided status names them)
    notes: list = field(default_factory=list)


def system_of(object_id: str) -> str:
    """The system a record is from: its binding's source prefix (``portal``, ``bc``)."""
    return (binding_of(object_id) or "unknown").split(".", 1)[0]


def _header_closed(values: Sequence[Any] | None) -> bool:
    """Whether a FileMaker header states one word that closes the order by itself
    (Complete, a cancelled or closed word)."""
    words = {word(v) for v in values or ()}
    if len(words) != 1:
        return False
    (text,) = words
    return text in COMPLETE_WORDS or header_class(text) == IGNORED


def _read_side(source: Any, records: Sequence[str],
               reads: Mapping[str, tuple[dict[str, list[Any]], list, bool]],
               notes: list | None = None, *, bc_lines: bool = True
               ) -> tuple[PoStatus, list, list, bool, dict[str, tuple[Any, list]]]:
    """One side of a PO (its settled twins, or the firm twins linked to them) by the one
    definition: the combined status, its evidence and link marks, whether a part is
    hidden, and each record's lines as read ``(triples or None, evidence)``. ``notes``:
    each record's ``record_note`` is appended.

    A FileMaker record's status is rolled up from its items (``fm_rollup``: FileMaker
    leaves the header stale), except that items never reopen a header that closes the
    order (Complete, cancelled: ``_header_closed``), and a Portal record joined to a FileMaker one is FileMaker's
    mirror: once FileMaker's items decide, the mirror's copied header and ETL-seeded line
    quantities decide nothing of their own."""
    fm = [m for m in records if reads[m][0].get("fm_status")]
    statuses, cited, marks, blind = [], [], [], False
    lines_of: dict[str, tuple[Any, list]] = {}
    rolled: dict[str, PoStatus] = {}
    logs: dict[str, Any] = {}
    for twin in fm:
        found, evidence, hidden, link_marks = source.fm_lines(twin)
        # items FileMaker states no receiving state for take its header's word as the
        # received quantity of the mirror's seeded 0 (``fm_by_header``); they never roll
        # up (a stale header never overrides a receipt the Portal lines record)
        logs[twin] = fm_by_header(found, reads[twin][0].get("fm_status"))
        status = fm_rollup(found)
        if status is not None and status.cls == OPEN and _header_closed(
                reads[twin][0].get("fm_status")):
            # the items may close a stale open header (B.Order over items all Complete),
            # never reopen a closed one: Complete closes whatever was received (Ross)
            status = None
        if status is not None:
            rolled[twin] = status
            cited += evidence
            marks += link_marks
        else:
            blind = blind or (hidden and found is UNREAD)
    fm_status = combine(rolled.values()) if rolled else None
    # a Portal mirror whose own lines record every line received in full: the Portal
    # lines are the received quantity (FileMaker's items never roll up over a receipt they
    # record), so the order is received whatever FileMaker's stale item words or the
    # header (Revised included) say (11-13 judging: PO5354, 7 of 7 lines received and a
    # Received receipt, listed "open, no due date" by FileMaker's Pending items)
    gold_received = False
    from_fm: list[bool] = []   # per status: FileMaker's rollup (its own or as mirror)
    for member in records:
        values, evidence, hidden = reads[member]
        blind = blind or hidden
        cited += evidence
        lines = fm_lines = None
        words = {word(v) for v in values.get("status", ())}
        issued_bc = bc_lines and bool(values.get("bc_status")) and not bc_draft(values)
        mirror = bool(fm) and len(words) == 1 and \
            header_class(next(iter(words))) == UNMAPPED
        if issued_bc or mirror or len(words) == 1 and header_class(next(iter(words))) == ISSUED:
            lines, evidence, hidden, link_marks = source.lines(member)
            blind = blind or hidden
            cited += evidence
            marks += link_marks
            lines_of[member] = (lines, list(evidence))
            totals = line_totals(lines) if lines is not None else (None, None)
            if fm and totals[1] is not None and totals[1] <= EPS:
                fm_lines = _one_log([logs[twin] for twin in fm])
        if fm and not issued_bc and values.get("status") and _all_received(lines) \
                and _fm_orders_the_same(lines, [logs[twin] for twin in fm]):
            gold_received = True
            statuses.append(PoStatus(CLOSED, RECEIVED))
            from_fm.append(False)
        elif member in rolled:
            statuses.append(rolled[member])
            from_fm.append(True)
        elif fm_status is not None and values.get("status") and \
                header_class(next(iter(words), None)) in (ISSUED, UNMAPPED):
            statuses.append(fm_status)   # FileMaker's mirror: FileMaker's items decide
            from_fm.append(True)
        else:
            statuses.append(record(values, lines, fm_lines))
            from_fm.append(False)
        if notes is not None:
            notes.append((values, lines, fm_lines if fm_lines is not None
                          else logs.get(member)))
    if gold_received:
        # FileMaker's open rollup (items Pending or partly received, no quantity) yields
        # to the Portal lines' full receipt; a closing FileMaker word agrees already
        statuses = [PoStatus(CLOSED, RECEIVED) if fm_side and st.cls == OPEN else st
                    for st, fm_side in zip(statuses, from_fm)]
    return combine(statuses), cited, marks, blind, lines_of


def _all_received(lines: Any) -> bool:
    """Whether readable lines record every line received in full (at least one line
    ordered above zero, none outstanding, every quantity read)."""
    if not isinstance(lines, list) or not lines:
        return False
    if any(UNREAD in (want, got) for want, got, _ in lines):
        return False
    ordered, _received = line_totals(lines)
    return ordered is not None and ordered > EPS and \
        not any(line_open(want, got) for want, got, _ in lines)


def _fm_orders_the_same(lines: Any, fm_items: Sequence[Any]) -> bool:
    """Whether every FileMaker twin's items order the Portal lines' total (within EPS):
    only then do the Portal lines speak for the whole order (a FileMaker order revised to
    more items than its mirror holds may still be waiting for one of them)."""
    ordered, _ = line_totals(lines)
    if ordered is None or not fm_items:
        return False
    for items in fm_items:
        if items is None or items is UNREAD:
            return False
        items = list(items)
        if not items or any(want is UNREAD for want, _g, _u in items):
            return False
        fm_ordered, _ = line_totals((want, None, None) for want, _g, _u in items)
        if fm_ordered is None or abs(fm_ordered - ordered) > EPS:
            return False
    return True


# The systems a status property is the header word of, as a note names them.
_SYSTEM_NAME = {"status": "Portal", "fm_status": "FileMaker", "bc_status": "BC",
                "sage_is_complete": "Sage"}
# A header word quoted in a note: one short word of letters (the FileMaker, Portal and BC
# vocabularies); anything else is source text and is named, never quoted.
_QUOTABLE = re.compile(r"[A-Za-z][A-Za-z._-]{0,15}")


def _is_unit(text: Any) -> bool:
    from operating.injection_guard import is_unit
    return is_unit(text)


def _qty(value: Decimal) -> str:
    """A quantity as a note shows it: thousands grouped, no trailing zero fraction."""
    if value == value.to_integral_value():
        value = value.to_integral_value()
    return f"{value:,}"


def record_note(values: Mapping[str, Sequence[Any]], lines: Any, fm_lines: Any = None) -> str:
    """What one PO record says, in words: each system's header word (``Portal header
    Pending``), its lines' received of ordered quantity in one unit, and the FileMaker QC
    receiving log against the FileMaker items' ordered total. Read from what this reader
    read (a withheld quantity is "not readable"); never a guess."""
    parts = []
    for prop, name in _SYSTEM_NAME.items():
        words = sorted({str(v).strip() for v in values.get(prop) or () if v is not None})
        if prop == "bc_status" and values.get("bc_fully_received"):
            flags = {_flag(v) for v in values["bc_fully_received"]}
            flag = ("fully received" if flags == {True} else "not fully received"
                    if flags == {False} else "fully-received flag unreadable")
        else:
            flag = None
        if not words and flag is None:
            continue
        said = " / ".join(w if _QUOTABLE.fullmatch(w) else "an unreadable word"
                          for w in words) or "no word"
        parts.append(f"{name} header {said}" + (f" ({flag})" if flag else ""))
    if lines is UNREAD or (lines is not None and not isinstance(lines, list)):
        parts.append("lines not readable")
    elif lines is not None:
        units = {u.strip().upper() if isinstance(u, str) and u.strip() else None
                 for _w, _g, u in lines if u is not UNREAD}
        ordered, received = line_totals(lines)
        if not lines:
            parts.append("no line recorded")
        elif ordered is None:
            parts.append("line quantities not readable in one unit")
        else:
            unit = next(iter(units)) if len(units) == 1 else None
            seeded = fm_lines is not None and received <= EPS
            parts.append(f"lines received {_qty(received)} of {_qty(ordered)}"
                         + (f" {unit}" if unit and _is_unit(unit) else "")
                         + (" (the FileMaker import seeds 0)" if seeded else ""))
    if fm_lines is UNREAD:
        parts.append("FileMaker item receiving states not readable")
    elif fm_lines is not None:
        settled = sum(1 for _w, got, _u in fm_lines
                      if got is not None and got is not PARTIAL_RECEIPT)
        partly = sum(1 for _w, got, _u in fm_lines if got is PARTIAL_RECEIPT)
        waiting = len(fm_lines) - settled - partly
        fm_ordered, _ = line_totals((want, None, None) for want, _g, _u in fm_lines)
        parts.append(f"FileMaker items: {settled} received or closed, {partly} partly "
                     f"received, {waiting} not received"
                     + (f", a FileMaker ordered total of {_qty(fm_ordered)}"
                        if fm_ordered is not None else ""))
    return ", ".join(parts) or "no status recorded"


def describe(po: "PoRead", limit: int = 400) -> str:
    """What the PO's records say, record by record (``record_note``), as this reader read
    them: the evidence an undecided status rests on, so the reason is actionable."""
    text = "; ".join(dict.fromkeys(record_note(*note) for note in po.notes))
    return text if len(text) <= limit else text[:limit - 1].rstrip(" ,;") + "…"


# A side's receipt state, as the two systems are compared: fully received (a Portal or FM
# Complete counts, a FileMaker order whose items are all received, a completed Sage
# order) against open lines outstanding. Draft, cancelled and unknown compare nothing: no
# receipt state is stated.
FULLY_RECEIVED, OPEN_LINES = "fully_received", "open_lines_outstanding"
# The system whose records are copies of orders created elsewhere (the Portal posts its
# orders to BC; FileMaker-era orders were migrated into it): a BC record is compared with
# the other systems' records of the same order, never combined with them. No system is
# the receipt authority (owner, 2026-11-06): goods recorded received in ANY copy close the
# order for lateness, and an open copy of a received order is a reconciliation note, never
# a late alert. BC's own lines decide alone only for an order created natively in BC
# (no other system holds a copy of it). The requested-by date ranks by the declared
# precedence (``PurchaseOrder.expected_date``'s authority).
POSTED_COPY = "bc"
CONFLICT_FIELDS = ("receipt_state", "received_quantity", "expected_date")


def receipt_state(status: PoStatus) -> str | None:
    if status.cls == OPEN:
        return OPEN_LINES
    if status.cls == CLOSED and status.code in (RECEIVED, COMPLETE, HISTORICAL_SAGE):
        return FULLY_RECEIVED
    return None


def compared_twin(a: str, b: str) -> bool:
    """Whether a twin link joins a BC copy (``POSTED_COPY``) to another system's record:
    such a twin is compared (``_compare``: a receipt in any copy closes the order) at any
    tier from ``FIRM`` up, never a settled member whose status joins the other's
    (``combine`` would dispute the Portal's Complete against BC's open lines)."""
    return (system_of(a) == POSTED_COPY) != (system_of(b) == POSTED_COPY)


def _directions(link: str, member: str) -> tuple[str, ...]:
    """The directions a twin link can reach another record from ``member``: ``POSTED`` is
    declared from the Portal order to the BC order, so one side reads it forward and the
    other reverse; ``SAME_RECORD`` joins records of either system both ways."""
    if link == POSTED:
        return ("reverse",) if system_of(member) == POSTED_COPY else ("forward",)
    return ("forward", "reverse")


def side_receipt(records: Sequence[str],
                 reads: Mapping[str, tuple[dict[str, list[Any]], list, bool]],
                 status: PoStatus, lines: Mapping[str, tuple[Any, list]]) -> str | None:
    """A side's receipt state. A BC copy states its own receipt state by its lines: a BC
    record with readable lines is open while any line's received
    quantity is below its ordered one, else fully received; with no readable line its
    ``fullyReceived`` flag states it. A BC ``Draft`` never received against (no line
    records a received quantity above 0: the Portal posts its orders to BC as Draft, live
    2026-10-30) states none: its zero lines are no receipt record, and a draft is not
    applicable to lateness (decision open-demo-a-bc-draft-states-no-receipt). A Draft
    whose lines record a receipt (released, received, then reopened for edit) keeps its
    lines' receipt state, conflicts and all; so does one whose lines are unreadable. Any
    other side by the one definition's status. A missing, unparseable or disputed flag
    and BC records that disagree state none, never a guess."""
    bc_records = [r for r in records if system_of(r) == POSTED_COPY]
    if not bc_records:
        return receipt_state(status)
    if any(unreceived_draft(reads[r][0], lines.get(r)) for r in bc_records):
        return None
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


def bc_draft(values: Mapping[str, Sequence[Any]]) -> bool:
    """A BC record whose header word is Draft (or disputed with Draft): not released, so
    its lines record no receipt (``side_receipt``)."""
    return any(header_class(v, "bc") == NOT_ISSUED for v in values.get("bc_status") or ())


def unreceived_draft(values: Mapping[str, Sequence[Any]], lines: Any) -> bool:
    """A BC Draft whose readable lines record no received quantity above 0: never
    released or received against, so its lines are no receipt record. ``lines``: the
    record's ``(triples, evidence)`` as read (None or unreadable: not proven, False)."""
    if not bc_draft(values):
        return False
    triples = lines[0] if lines else None
    if triples is None:
        return False
    for _want, got, _unit in triples:
        if got is UNREAD:
            return False
        number = Decimal(0) if got is None else _number(got)
        if number is None or number > EPS:
            return False
    return True


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


def system_name(object_id: str) -> str:
    """The system a record's own source is, as a reader names it: ``bc``, ``filemaker``
    and ``sage`` (served through the Portal's mirror) or ``portal``."""
    binding = binding_of(object_id) or ""
    if binding.startswith("portal.fm_"):
        return "filemaker"
    if binding.startswith("portal.sage_"):
        return "sage"
    return system_of(object_id)


def _systems(records: Sequence[str]) -> str:
    return "+".join(sorted({system_name(r) for r in records}))


def _compare(source: Any, out: "PoRead", lines_of: Mapping[str, tuple[Any, list]],
             firm: Sequence[str], firm_marks: Sequence[Any],
             mine_reads: Mapping[str, tuple[dict[str, list[Any]], list, bool]]) -> None:
    """Compare this PO's settled side with its firm twins' (another system's record of the
    same order, linked as this reader may read it): receipt state and received quantity.
    A disagreement is a named conflict with both values, each side's own cited items, and
    the side that holds. Goods recorded received in either copy close the order (its
    evidence then joins the status's) and the other copy's open lines are a reconciliation
    note (``reconcile``), never a late order. A side this reader cannot read states
    nothing, so nothing is compared."""
    reads = {t: source.values(t) for t in firm}
    fm_members = [r for r, read in (*mine_reads.items(), *reads.items())
                  if read[0].get("fm_status")]
    theirs, their_evidence, their_marks, _hidden, their_lines = _read_side(source, firm, reads,
                                                                         out.notes)
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
    mine_bc = any(system_of(r) == POSTED_COPY for r in out.members)
    their_bc = any(system_of(r) == POSTED_COPY for r in firm)
    # a receipt in ANY copy closes the order (owner, 2026-11-06): whichever side records
    # the goods received decides, whatever the other copy's header or lines still say.
    # A BC copy compared here is the posted copy of a Portal or FileMaker order (that
    # order's own system closes it); a BC order with no such twin governs by its own lines
    received_by = (mine_side if mine == FULLY_RECEIVED else
                   their_side if other == FULLY_RECEIVED else None)
    if received_by is not None and out.status != PoStatus(CLOSED, RECEIVED):
        out.status = PoStatus(CLOSED, RECEIVED)
        if received_by == their_side:
            bc_lines = other_lines if their_bc else {}
            out.evidence += [e for e in [*their_evidence, *(
                e for _t, ev in bc_lines.values() for e in ev)]
                if isinstance(e, Mapping) and (e.get("property") in SOURCE_OF
                                               or e.get("property") == RECEIVED_QTY)
                and not any(e is x for x in out.evidence)]
            out.marks += [*their_marks, *firm_marks]
        elif mine_bc:
            out.evidence += [e for _t, ev in mine_lines.values() for e in ev
                             if isinstance(e, Mapping) and e.get("property") == RECEIVED_QTY
                             and not any(e is x for x in out.evidence)]
    if mine is not None and other is not None and mine != other:

        def cited(evidence: Sequence[Any], side_lines_: Mapping[str, tuple[Any, list]],
                  bc_side: bool) -> list:
            # the status values, and a BC side's line quantities (its own receipt record)
            items = [e for e in evidence if isinstance(e, Mapping)
                     and e.get("property") in SOURCE_OF]
            if bc_side:
                items += [e for _t, ev in side_lines_.values() for e in ev
                          if isinstance(e, Mapping) and e.get("property") == RECEIVED_QTY]
            return items
        mine_items = cited(out.evidence, mine_lines, mine_bc)
        their_items = cited(their_evidence, other_lines, their_bc)
        # one copy received, the other still open: a reconciliation note on the open copy
        # (``reconcile``), never a delivery risk; the received side is the one that holds
        out.conflicts.append({
            "field": "receipt_state", "governs": received_by, "reconcile": True,
            "marks": list(firm_marks),
            "sides": [_side_entry(mine_side, out.members, mine, mine_items),
                      _side_entry(their_side, firm, other, their_items)]})
    # received quantity where both systems record one in the same unit
    a = _recorded_received(mine_lines.values())
    b = _recorded_received(other_lines.values())

    # an FM-mirrored Portal order's received 0 is ETL-seeded, not a recorded receipt;
    # a BC draft never received against records no receipt at all
    def seeded(records: Sequence[str], got: Any) -> bool:
        # FileMaker's mirror: its line quantities are copies (seeded 0, container counts,
        # another unit), never a recorded receipt of their own
        return got is not None and any(r in fm_members for r in records)
    drafts = [r for r, read in (*mine_reads.items(), *reads.items())
              if system_of(r) == POSTED_COPY
              and unreceived_draft(read[0], mine_lines.get(r) or other_lines.get(r))]
    if seeded(out.members, a) or seeded(firm, b) or drafts:
        return
    from .answer_conflicts import same_quantity   # the one rule of one quantity

    if a is not None and b is not None and a[1] == b[1] and abs(a[0] - b[0]) > EPS \
            and same_quantity([str(a[0]), str(b[0])], quantity=True) is None:
        out.conflicts.append({
            "field": "received_quantity", "governs": received_by,
            "reconcile": out.status.cls == CLOSED,
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


def _item_handle(item: Any) -> str | None:
    cite = item.get("citation") if isinstance(item, Mapping) else None
    handle = cite.get("l1_handle") if isinstance(cite, Mapping) else None
    return handle if isinstance(handle, str) else None


def _undating_drafts(source: Any, records: Sequence[str],
                     reads: Mapping[str, tuple[dict[str, list[Any]], list, bool]]
                     ) -> tuple[set[str], set[str]]:
    """The BC records among ``records`` whose dates never date the order: a Draft whose
    lines record no received quantity above 0, or whose lines this reader cannot read
    (a date never decides late on a guess). A Draft received against keeps its dates.
    Also the citation handles of those records' own values (a dated item a tool reader
    cites by handle only)."""
    out: set[str] = set()
    handles: set[str] = set()
    for record_id in records:
        if system_of(record_id) != POSTED_COPY:
            continue
        values, evidence, _hidden = reads.get(record_id) or source.values(record_id)
        if not bc_draft(values):
            continue
        triples = source.lines(record_id)[0]
        received = triples not in (None, UNREAD) and any(
            got is not UNREAD and got is not None and (_number(got) or Decimal(0)) > EPS
            for _want, got, _unit in triples)
        if not received:
            out.add(record_id)
            handles |= {h for h in (_item_handle(e) for e in evidence) if h}
    return out, handles


def _item_prop(item: Any) -> str | None:
    if isinstance(item, Mapping):
        found = item.get("property")
        return found if isinstance(found, str) else None
    return None


OWN_DAY = "expected_date_own_day"


def _default_date(defaults: "DateDefaults | None", item: Any, day: date,
                  own: set[date]) -> bool:
    """Whether a dated item's day is a default rather than a requested date: a placeholder
    (``source_defaults.placeholder_day``: 1900-01-01, years before the order), or a bulk
    day with provenance (``DateDefaults``: a system computed it from the holders' own
    days). One of the order's own days is no proof: kept, with its doubt (``own_doubt``),
    BIZ-007."""
    from operating.source_defaults import placeholder_day

    if placeholder_day(day, own):
        return True
    record_id, prop = _item_record(item), _item_prop(item)
    return bool(defaults is not None and record_id and prop
                and defaults.is_default(prop, binding_of(record_id) or "", day))


def is_default_date(defaults: "DateDefaults | None", item: Any, day: date,
                    own: set[date]) -> bool:
    """Whether a dated item's day is a default rather than a stated date (a placeholder,
    or a bulk day a system computed): the one test every reader of a date asks (``read``,
    the slip rule)."""
    return _default_date(defaults, item, day, own)


def own_doubt(need: "Need", own: set[date]) -> bool:
    """Whether a requested-by due date is one of the order's own days (a same-day order or
    a form default): judged, said with that doubt."""
    from operating.source_defaults import own_day

    return need.basis == "requested" and any(
        own_day(d, own) for d in (need.lo, need.hi) if d is not None)


def _labelled_due(source: Any, stated: "Promise", records: Sequence[str],
                  defaults: "DateDefaults | None", need: "Need") -> "Need":
    """The due date of an order no record states a requested-by date for, from a labelled
    source only: the supplier's confirmed delivery date (a settled confirmation's,
    ``vendor_eta``), else the order day plus the order's own lead time (``lead_time``; a
    lead time a bulk share of the orders carry is a default and dates nothing). None
    found: ``need`` unchanged (undated)."""
    if stated.latest is not None:
        rows = tuple(row for row, _link in (i for i in stated.items if isinstance(i, tuple))
                     if isinstance(row, Mapping) and day_of(row.get("value")) == stated.latest)
        marks = tuple(link.get("markings") for _row, link in (
            i for i in stated.items if isinstance(i, tuple)) if isinstance(link, Mapping))
        return replace(need, lo=stated.latest, hi=stated.latest, basis="vendor_eta",
                       governing=rows or tuple(stated.items[-1:]), marks=marks)
    if not hasattr(source, "lead_time"):
        return need
    for record_id in records:
        found = source.lead_time(record_id)
        if found is None:
            continue
        start, days, items = found
        if defaults is not None and defaults.is_default(
                LEAD_TIME, binding_of(record_id) or "", days):
            continue
        if days > 0:
            due = start + timedelta(days=int(days))
            return replace(need, lo=due, hi=due, basis="lead_time", governing=tuple(items))
    return need


LEAD_TIME = "lead_time_days"


def _lead_time(props: Mapping[str, Sequence[Mapping[str, Any]]]
               ) -> tuple[date, Decimal, list] | None:
    days = {_number(r["value"]) for r in props.get(LEAD_TIME, ()) if r.get("value") not in
            (None, "")}
    starts = {day_of(r["value"]) for r in props.get("order_date", ())
              if r.get("value") not in (None, "")}
    if len(days) != 1 or len(starts) != 1 or None in days or None in starts:
        return None
    rows = [*props.get("order_date", ()), *props.get(LEAD_TIME, ())]
    return next(iter(starts)), next(iter(days)), [r for r in rows if r.get("value")]


def _join_marks(*texts: Any) -> str:
    """The union of markings (each a JSON array as stored)."""
    found: set[str] = set()
    for text in texts:
        try:
            found |= set(json.loads(text)) if isinstance(text, str) else set(text or ())
        except (TypeError, ValueError):
            found.add("unreadable")
    return json.dumps(sorted(found))


@dataclass
class DateDefaults:
    """The default values of each date (or lead-time) property, by source binding: the
    days a bulk share of that property's records hold (``source_defaults``). Built from
    the rows a reader holds (``of_rows``) or read from the store (``of_store``, counts
    only, cached per store generation), so every reader asks the same test."""
    values: dict[tuple[str, str], frozenset] = field(default_factory=dict)

    @staticmethod
    def _key(prop: str, raw: Any) -> Any:
        if prop == LEAD_TIME:
            return _number(raw)
        return day_of(raw)

    @classmethod
    def of_pairs(cls, pairs: Iterable[tuple]) -> "DateDefaults":
        """From ``(property, binding, raw value[, the record's own stamps])``: a lead time
        a bulk share holds; a date a bulk share holds that a system computed from the
        holders' own days (``source_defaults.calculated_defaults``, BIZ-007): many orders
        sharing a real delivery date keep it."""
        from operating.source_defaults import DATE_SHARE, bulk_values, calculated_defaults

        grouped: dict[tuple[str, str], list[tuple[Any, tuple]]] = defaultdict(list)
        for prop, binding, raw, *own in pairs:
            grouped[(prop, binding)].append((cls._key(prop, raw), tuple(own[0]) if own
                                             else ()))
        out = {}
        for key, held in grouped.items():
            if key[0] == LEAD_TIME:
                found = bulk_values((v for v, _own in held), share=DATE_SHARE)
            else:
                found = calculated_defaults(held, share=DATE_SHARE)
            if found:
                out[key] = found
        return cls(out)

    @classmethod
    def of_rows(cls, rows: Iterable[tuple[str, Mapping[str, Any]]],
                own: Callable[[str], Iterable[Any]] = lambda _oid: ()) -> "DateDefaults":
        """``own(record)``: its own stamps (a line: its order's)."""
        return cls.of_pairs((row["property"], binding_of(oid) or "", row["value"], own(oid))
                            for oid, row in rows if row.get("value") not in (None, ""))

    @classmethod
    def of_store(cls, conn: Any) -> "DateDefaults":
        digest = None
        try:
            found = conn.execute("SELECT value FROM store_meta WHERE key = "
                                 "'content_digest'").fetchone()
            digest = found[0] if found else None
        except Exception:  # noqa: BLE001 - a store without meta: not cached
            pass
        if digest is not None and digest in _DEFAULTS_CACHE:
            return _DEFAULTS_CACHE[digest]
        props = (NEED_PROP, LINE_EXPECTED, LEAD_TIME)
        rows = conn.execute(
            "SELECT o.object_id, v.property, v.value FROM objects o JOIN object_values v "
            "ON v.object_id = o.object_id WHERE o.object_type IN (?, ?) AND v.property IN "
            "(SELECT value FROM json_each(?)) AND v.superseded_at IS NULL AND v.value IS NOT "
            "NULL AND v.value <> ''", (PO_TYPE, LINE_TYPE, json.dumps(props))).fetchall()
        # each record's own days (a line's are its order's), counts only: the provenance
        # a bulk day needs to be a default (``of_pairs``)
        own: dict[str, list[Any]] = defaultdict(list)
        for oid, value in conn.execute(
                "SELECT o.object_id, v.value FROM objects o JOIN object_values v "
                "ON v.object_id = o.object_id WHERE o.object_type = ? AND v.property IN "
                "(SELECT value FROM json_each(?)) AND v.superseded_at IS NULL AND v.value "
                "IS NOT NULL", (PO_TYPE, json.dumps(OWN_DAY_PROPS))):
            own[oid].append(value)
        order_of: dict[str, str] = {}
        for line, order in conn.execute(
                "SELECT from_object, to_object FROM links WHERE link_type = ? "
                "AND superseded_at IS NULL", (LINE_OF,)):
            order_of.setdefault(line, order)
        made = cls.of_pairs((prop, binding_of(oid) or "", value,
                             own.get(order_of.get(oid, oid), ()))
                            for oid, prop, value in rows)
        if digest is not None:
            _DEFAULTS_CACHE.clear()
            _DEFAULTS_CACHE[digest] = made
        return made

    def is_default(self, prop: str, binding: str, raw: Any) -> bool:
        key = raw if isinstance(raw, (date, Decimal)) else self._key(prop, raw)
        return key is not None and key in self.values.get((prop, binding), frozenset())


_DEFAULTS_CACHE: dict[str, DateDefaults] = {}


def read(source: Any, oid: str, today: date | None = None, *, dates: bool = True,
         bc_lines: bool = True) -> PoRead:
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
    for an open or received PO when ``dates`` and ``today`` are given. ``bc_lines`` False:
    an issued BC order's status is its ``fullyReceived`` flag alone, its lines unread (the
    unnamed ``plx.process`` choice, which every reader of the order decides alike)."""
    members, blind, identity_marks = source.twins(oid)
    reads = {m: source.values(m) for m in members}
    out = PoRead(oid, list(members), PoStatus(UNKNOWN, reason="status_missing"),
                 identity_marks=list(identity_marks))
    out.status, out.evidence, out.marks, hidden, lines_of = _read_side(
        source, members, reads, out.notes, bc_lines=bc_lines)
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
        # a BC Draft never received against dates nothing (decision
        # 2026-11-01-late-list-draft-twins rule 1): its header and line dates are draft
        # or migration defaults, never the order's requested-by date
        drafts, draft_handles = _undating_drafts(source, dated, reads)
        dated = [d for d in dated if d not in drafts]
        # still this order's firm twins (the engine joins them as one order); only
        # their dates are left out
        out.date_twins, out.marks = list(firm), [*out.marks, *firm_marks]
        raw = [entry for entry in source.need_dates(oid, dated)
               if _item_record(entry[2]) not in drafts
               and _item_handle(entry[2]) not in draft_handles]
        # date provenance: a date a bulk share of its property's records hold (a sync's
        # or migration's day), or one of the order's own days (created, migrated,
        # ordered), is a default, never a requested-by date (``source_defaults``)
        defaults = source.date_defaults() if hasattr(source, "date_defaults") else None
        own: set[date] = set()
        if hasattr(source, "own_days"):
            for record_id in [*members, *firm]:
                own |= source.own_days(record_id)
        defaulted = [e[2] for e in raw if e[1] is not None
                     and _default_date(defaults, e[2], e[1], own)]
        raw = [e for e in raw if not any(e[2] is d for d in defaulted)]
        out.need = need_date(raw)
        found = _date_conflict(raw)
        if found is not None:
            out.conflicts.append({**found, "marks": list(firm_marks)})
        if out.need.lo is None:   # no header date: the open lines' own expected dates
            lines = []
            for day, item in source.line_dates(dated):
                if day is not None and _default_date(defaults, item, day, own):
                    defaulted.append(item)
                    day = None   # a default day dates nothing: the line is undated
                lines.append((day, item))
            out.need = line_need(lines, today)
        # confirmations linked to ANY copy of the order (its settled twins and every
        # other system's record of it), never only the record asked about
        stated = source.promises(oid, [*members, *(t for t in firm if t not in members)])
        # None: a confirmation listing this reader may not read in full; a promise it
        # cannot see may be later than the need date, so never "neither" from a part
        out.promise = promise(stated) if stated is not None else Promise(
            reason="promises_unread")
        if out.need.lo is None:
            # no record states a requested-by date: a due date only from a labelled
            # source, the supplier's own confirmed date, else the order's lead time
            out.need = _labelled_due(source, out.promise, [*members, *firm], defaults,
                                     out.need)
        if defaulted:
            out.need = replace(out.need, defaulted=tuple(defaulted))
        if own_doubt(out.need, own):
            out.need = replace(out.need, doubt=OWN_DAY)
        out.verdict = verdict(out.status, out.need, out.promise.latest
                              if out.need.basis == "requested" else None, today)
        if stated is None and out.verdict == (NEITHER, None):
            out.verdict = (UNKNOWN, "promises_unread")
    elif today is not None:
        out.verdict = verdict(out.status, Need(), None, today)
    return out


def _one_log(logs: Sequence[Any]) -> Any:
    """The FM QC log of a PO with one or more FM twins: unread when any is unread or more
    than one twin logs a receipt (two logs of one order are not summed); the one twin's
    that logs one; otherwise (no QC receipt anywhere) the first, which decides nothing."""
    if any(log is UNREAD or any(got is UNREAD or isinstance(got, _Unstated) or got is not None and got is not PARTIAL_RECEIPT
                                and _number(got) is None for _, got, _ in log)
           for log in logs):
        return UNREAD
    logged = [log for log in logs if any(
        got is PARTIAL_RECEIPT or got is not None and (_number(got) or Decimal(0)) > EPS
        for _, got, _ in log)]
    if len(logged) > 1:
        return UNREAD
    return logged[0] if logged else (logs[0] if logs else None)


# ---- the order's copies and its confirmations (every reader's one rule)

def copies(source: Any, oid: str) -> tuple[list[str], bool]:
    """Every record of the order ``oid`` as the one reader reads it: its settled identity
    twins (``twins``) and every other system's record a firm or compared twin link joins
    to them (``date_twins``), the root first; and whether a twin link is hidden from this
    reader (a fuller grant might join another copy). A copy is the order's for its
    confirmations, receipts and dates, never for its status (``read`` decides that)."""
    members, blind, _marks = source.twins(oid)
    firm, _firm_marks = (source.date_twins(oid, members)
                         if hasattr(source, "date_twins") else ([], []))
    return [*members, *(t for t in firm if t not in members)], bool(blind)


def counted_confirmations(links: Iterable[Mapping[str, Any]],
                          colliding: Callable[[str], bool]) -> list[dict[str, Any]]:
    """The confirmation links (``CONFIRMATION_LINKS``) that count for the order they point
    to: every settled one (``CONFIRMING`` tiers) and every lead, except a lead to a record
    whose order number another order also holds (``colliding``): a number-text match on a
    reused number may be the other order's, so it confirms nothing here."""
    return [dict(link) for link in links
            if link["tier"] in CONFIRMING or not colliding(str(link["to_object"]))]


VENDOR_NUMBER_PROP = "fm_po_number"


def vendor_facing(numbers: Iterable[tuple[Any, Any]]) -> Any:
    """The order number the supplier knows, of ``(value, item)`` the order's copies hold
    in ``VENDOR_NUMBER_PROP`` (a FileMaker-era order's FileMaker PO number, held by its
    FileMaker and Portal copies): the first item when exactly one number is held, else
    None (the copy's own number is said instead)."""
    held = [(str(value).strip(), item) for value, item in numbers
            if value is not None and str(value).strip()]
    return held[0][1] if len({v for v, _ in held}) == 1 else None


CONFIRMED, FOUND_UNVERIFIED, NONE_FOUND = "confirmed", "found_unverified", "none_found"
# a lead exists, but on a number another order also holds: it cannot be tied to this
# order, so neither "found" nor "none" is said (owner, 2026-11-06)
NEEDS_CHECK = "needs_check"


def reused_leads(links: Iterable[Mapping[str, Any]],
                 colliding: Callable[[str], bool]) -> list[dict[str, Any]]:
    """The leads ``counted_confirmations`` leaves out: on a record whose number another
    order also holds."""
    return [dict(link) for link in links
            if link["tier"] not in CONFIRMING and colliding(str(link["to_object"]))]


def confirmation_state(links: Iterable[Mapping[str, Any]],
                       reused: Iterable[Mapping[str, Any]] = ()) -> str:
    """What the counted confirmation links of an order's copies say of it: ``confirmed``
    (a settled link), ``found_unverified`` (only leads: never "none"), ``needs_check``
    (only leads on a reused number, ``reused``: evidence exists but cannot be tied to
    this order), ``none_found``."""
    links = list(links)
    if any(link["tier"] in CONFIRMING for link in links):
        return CONFIRMED
    if links:
        return FOUND_UNVERIFIED
    return NEEDS_CHECK if list(reused) else NONE_FOUND


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
        self.lines_proven = False

    def _reach(self, root_type: str, oid: str, paths: Sequence[Sequence[Mapping]],
               tid: str, bindings: Iterable[str] | None = None,
               side: Mapping[str, Any] | None = None) -> Any:
        tspec = self.tools.base.schema.object_types.get(tid, {})
        return self.tools._paths(self.ctx, root_type, oid, paths, self.floor,
                                 spec_marks=tspec.get("markings", ()),
                                 final_bindings=set(bindings) if bindings is not None
                                 else {b["id"] for b in tspec.get("bindings", ())},
                                 side=side)

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
            state, _none = self._quantities(item, (FM_RECEIVING,))
            got = fm_item_received(state, want)
            if got is UNREAD:
                return UNREAD, [], state is UNREAD or want is UNREAD, []
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
        # every line of the order is read (the per-line promise roll-up needs all of them)
        self.lines_proven = bool(reach.proven and not reach.truncated)
        out = []
        for lid in sorted(r for r in reach.records if ctx.obj(r)["object_type"] == tid):
            ordered, ounit, ocited = self._one(lid, spec["ordered"])
            received, runit, rcited = self._one(lid, spec["received"])
            line: dict[str, Any] = {"line": lid, "state": "unknown"}
            if ordered is not None and received is not None and ounit == runit and ordered > 0:
                line["state"] = ("not_received" if received <= 0 else
                                 "partial" if received < ordered else "received")
                # the quantities the state was decided from (the caller sums them; never
                # listed: the line's cited values are)
                line["_quantity"] = (ordered, max(Decimal(0), received), ounit)
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
        proven, hidden or reaches a lead only. ``unnamed``: the records a path passes
        through whose own reference names none (a FileMaker QC receiving-log row whose
        ``idreceipt`` names no receipt header) and whose record they hang from is named
        by none either (no header names the row's PO item, ``Receipt.receives_line``):
        the dated records may then miss one."""
        from operating.ontology_tools import _markings

        ctx, op = self.ctx, self.op
        schema = self.tools.base.schema
        tid = spec["object_type"]
        root_type = self.graph["root_type"]
        reach = self._reach(root_type, oid, spec["paths"], tid, side={})
        out = Records(records=sorted(r for r in reach.records
                                     if ctx.obj(r)["object_type"] == tid))
        # a root or its twin reaching none is an absence the proof covers, not a gap; a
        # record naming none is dated by the records naming the one it hangs from
        out.unnamed = sorted(r for r in reach.nameless
                             if ctx.obj(r)["object_type"] != root_type
                             and not reach.via.get(r, set()) & reach.named_by)
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
        # or only because a source's capture is stale: read as of that capture (the
        # verdict says so), as the late list reads the same captured records
        stale = (op.stale_sources(reach.reasons) or ()) \
            if not reach.proven and not lead_only else ()
        out.counted_proven = out.proven or ((lead_only or bool(stale)) and not reach.truncated
                                            and not out.hidden and not uncited)
        out.stale = tuple(stale) if out.counted_proven and not out.proven else ()
        out.lead_only = not out.records and not out.hidden and bool(reach.leads)
        # the records only a lead link reaches (visible to this reader): never counted
        out.leads = sorted({target for _, target in reach.leads
                            if target not in out.records
                            and ctx.obj(target)["object_type"] == tid})
        out.detail = "; ".join(dict.fromkeys(w.text for w in reach.reasons))
        return out

    def line_promise_blocks(self, records: Sequence[str]
                            ) -> list[tuple[date | None, str, Mapping[str, Any]]]:
        """``(document date, confirmation, stored row)`` of each per-item promise value
        (``SupplierConfirmation.line_promises``) of ``records`` this reader may read: a
        withheld value is not read. The document date is the ``promised`` declaration's
        ``order_by`` as :meth:`records` read it."""
        from operating.line_promises import blocks_of

        ctx, out = self.ctx, []
        found = self.promised
        for rid in records:
            if LINE_PROMISES not in ctx.values(rid) or LINE_PROMISES in ctx.withheld(rid):
                continue
            for row in ctx.values(rid)[LINE_PROMISES]:
                if blocks_of(row["value"]):
                    out.append((None if found is None else found.order.get(rid), rid, row))
        return out

    def line_descriptors(self, lids: Sequence[str]) -> dict[str, dict[str, list[str]]]:
        """Each order line's own item codes and descriptions this reader may read (a BC
        line's number only when it is an Item line), for :func:`line_promises.match`."""
        from operating.line_promises import descriptor

        ctx, out = self.ctx, {}
        for lid in lids:
            values, withheld = ctx.values(lid), ctx.withheld(lid)

            def read(name: str) -> list[str]:
                return [] if name in withheld else [
                    str(r["value"]) for r in values.get(name, ()) if r["value"] not in (None, "")]
            out[lid] = descriptor(read)
        return out

    def lead_promises(self) -> list[tuple[Any, date, Any]]:
        """The promised dates of the confirmations only a lead link reaches (``promises``
        must have run): ``(document date, day, item)`` each cited as this reader reads
        it. A lead is never this order's promise; the caller labels these a possible
        vendor promise. Empty when the promise read is hidden from this reader."""
        from operating.ontology_tools import _markings

        spec = ((self.stage or {}).get("due") or {}).get("lateness", {}).get("promised")
        found = self.promised
        if not spec or found is None or found.hidden or not found.leads:
            return []
        ctx, op, out = self.ctx, self.op, []
        order_by = spec.get("order_by")
        for _, subject, row in self.tools._visible_events(ctx, found.leads,
                                                          spec["event_types"]):
            cite, day = op._cite(row), op._date(row["time"])
            if cite is None or day is None:
                continue
            order = None
            if order_by and order_by not in ctx.withheld(subject):
                days = {op._date(r["value"]) for r in ctx.values(subject).get(order_by, ())
                        if r["value"] is not None}
                order = next(iter(days)) if len(days) == 1 and None not in days else None
            out.append((order, day, {
                "property": row["event_type"].split(".", 1)[1], "value_type": "date",
                "value": day.isoformat(), "citation": cite,
                "markings": list(_markings(row["markings"]))}))
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

    def date_defaults(self) -> "DateDefaults":
        """The store's default days per date property (counts only, never a record)."""
        return DateDefaults.of_store(self.tools.base.store.conn)

    def _own(self, oid: str) -> dict[str, list]:
        values = self.ctx.values(oid) if self.ctx.visible([oid]) else {}
        return {p: [r for r in values.get(p, ()) if isinstance(r, Mapping)]
                for p in (*OWN_DAY_PROPS, LEAD_TIME)}

    def own_days(self, oid: str) -> set[date]:
        own = self._own(oid)
        return {d for prop in OWN_DAY_PROPS for r in own.get(prop, ())
                if (d := day_of(r.get("value"))) is not None}

    def lead_time(self, oid: str) -> tuple[date, Decimal, list] | None:
        return _lead_time(self._own(oid))


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
    # records passed through on the way that name no record (``ContextSource.records``)
    unnamed: list[str] = field(default_factory=list)
    # records of the declared type only a lead link reaches (reported, never counted)
    leads: list[str] = field(default_factory=list)
    # counted proven only as of these sources' stale captures (``counted_proven``)
    stale: tuple[str, ...] = ()


# ---- the system store (the proactive engine: every current record, set-based)

class StoreSource:
    """The proactive engine's read of the whole store (``st``: its ``_Store``), preloaded
    set-wise: every PurchaseOrder with its status, need-date and number values, its
    settled identity clusters (``SAME_RECORD`` at ``SETTLED`` within a system; a settled
    twin across BC is a firm one, ``compared_twin``), its lines (``LINE_OF`` at
    ``CONFIRMING``) and their quantities and FileMaker receiving states, and every
    confirmation link at any tier with its dates. Evidence is
    the store's value rows; marks are the links' markings (the engine cites with them)."""

    STATUS_PROPS = tuple(SOURCE_OF)

    def __init__(self, st: Any) -> None:
        self.st = st
        self.pos = st.values((PO_TYPE,), (*SOURCE_OF, NEED_PROP, "po_number",
                                          *VENDOR_NUMBER_PROPS, *OWN_DAY_PROPS,
                                          LEAD_TIME))
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
        self.line_values = st.values((LINE_TYPE,), (ORDERED, RECEIVED_QTY, LINE_EXPECTED,
                                                    FM_RECEIVING),
                                     objects=wanted) if wanted else {}
        # what FileMaker recorded received against each item: its receiving rows and its
        # QC lots (``fm_item_received``'s receipt evidence), each with its cited status
        self.fm_receipts: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
        fm_items = {line for fm, found in self.lines_of.items()
                    if binding_of(fm) == FM_BINDING for line, _ in found}
        receipt_links = [l for l in st.links((RECEIPT_RECEIVES_LINE, QC_RECEIVES_LINE),
                                             CONFIRMING)
                         if l["to_object"] in fm_items
                         and binding_of(l["from_object"]) in FM_RECEIPT_BINDINGS]
        lot_links: dict[str, list[tuple[str, str]]] = defaultdict(list)
        if receipt_links:
            for link in st.links((RECEIVED_LOT,), CONFIRMING):
                lot_links[link["from_object"]].append((link["to_object"], link["markings"]))
        receipts = st.values((RECEIPT_TYPE,), (RECEIPT_STATUS,), objects=sorted(
            {l["from_object"] for l in receipt_links
             if l["from_object"].startswith(RECEIPT_TYPE + ":")})) if receipt_links else {}
        lots = st.values((LOT_TYPE,), (LOT_QC,), objects=sorted(
            {lot for found in lot_links.values() for lot, _ in found})) if lot_links else {}
        for link in receipt_links:
            found = receipts.get(link["from_object"])
            for row in ((found or {"props": {}})["props"].get(RECEIPT_STATUS) or []):
                if row["value"] not in (None, ""):
                    self.fm_receipts[link["to_object"]].append((row, link["markings"]))
            for lot, lot_marks in lot_links.get(link["from_object"], []):
                for row in ((lots.get(lot) or {"props": {}})["props"].get(LOT_QC) or []):
                    if row["value"] not in (None, "") and word(row["value"]) \
                            not in FM_LOT_REFUSED:
                        self.fm_receipts[link["to_object"]].append(
                            (row, _join_marks(link["markings"], lot_marks)))
        # an order number held by more than one order (records no settled or compared
        # twin link joins): a lead matched on that number may be the other order's, so
        # it confirms nothing here (number collisions; joins only on stored identifiers)
        order_root = clusters([*((l["from_object"], l["to_object"]) for l in settled),
                               *((a, b) for a, found in self.firm.items() for b, _ in found)],
                              self.pos)
        holders: dict[str, set[str]] = defaultdict(set)
        for oid, entry in self.pos.items():
            for prop in NUMBER_PROPS:
                for row in entry["props"].get(prop, []):
                    if word(row["value"]):
                        holders[_number_key(row["value"])].add(order_root.get(oid, oid))
        self.colliding = {oid for oid, entry in self.pos.items()
                          if any(len(holders.get(_number_key(r["value"]), ())) > 1
                                 for p in NUMBER_PROPS for r in entry["props"].get(p, [])
                                 if word(r["value"]))}
        self.confirms_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.reused_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
        every = st.links(CONFIRMATION_LINKS)
        for link in counted_confirmations(every, lambda oid: oid in self.colliding):
            self.confirms_of[link["to_object"]].append(link)
        for link in reused_leads(every, lambda oid: oid in self.colliding):
            self.reused_of[link["to_object"]].append(link)
        # each confirming document's own record versions (handle, markings): what a
        # claim about a confirmation cites
        self.documents: dict[str, list[dict[str, Any]]] = defaultdict(list)
        sources = sorted({l["from_object"] for found in (*self.confirms_of.values(),
                                                         *self.reused_of.values())
                          for l in found})
        for start in range(0, len(sources), 5000):
            for row in st.versions(objects=sources[start:start + 5000]):
                self.documents[row["object_id"]].append(row)
        self.confirmations = st.values((CONFIRMATION_TYPE,), (*PROMISE_PROPS, PROMISE_ORDER))
        # the days a bulk share of each date property's records hold, by source binding
        # (``source_defaults``): a sync's or migration's day, never a requested date
        self.defaults = DateDefaults.of_rows(
            [(oid, row) for oid, entry in self.pos.items()
             for row in entry["props"].get(NEED_PROP, [])]
            + [(oid, row) for oid, entry in self.line_values.items()
               for row in entry["props"].get(LINE_EXPECTED, [])]
            + [(oid, row) for oid, entry in self.pos.items()
               for row in entry["props"].get(LEAD_TIME, [])],
            own=lambda oid: [r.get("value") for order in (self.po_of.get(oid) or [oid])[:1]
                             for p in OWN_DAY_PROPS for r in self.props(order).get(p, [])])

    def date_defaults(self) -> "DateDefaults":
        return self.defaults

    def lead_time(self, oid: str) -> tuple[date, Decimal, list] | None:
        """The record's order day and lead time in days, with their rows; None when
        either is not one readable value."""
        return _lead_time(self.props(oid))

    def own_days(self, oid: str) -> set[date]:
        """The record's own days (created, migrated, ordered)."""
        props = self.props(oid)
        return {d for p in OWN_DAY_PROPS for r in props.get(p, [])
                if (d := day_of(r["value"])) is not None}

    def vendor(self, oid: str) -> set[str]:
        props = self.props(oid)
        return {word(r["value"]) for p in VENDOR_NUMBER_PROPS for r in props.get(p, [])
                if word(r["value"])}

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
            states = {word(r["value"]) for r in props.get(FM_RECEIVING, [])
                      if r["value"] is not None}
            got_rows = self.fm_receipts.get(item, [])
            rows += props.get(ORDERED, []) + props.get(FM_RECEIVING, [])
            marks.append(link_marks)
            ordered = UNREAD if want is UNREAD else want[0] if want else None
            state = UNREAD if len(states) > 1 else next(iter(states), None)
            got = fm_item_received(state, ordered, bool(got_rows))
            if got is UNREAD:
                return UNREAD, rows, False, marks   # an item state FileMaker does not say
            if got_rows:   # the receipt the item's state rests on is cited with it
                rows += [row for row, _m in got_rows]
                marks += [m for _r, m in got_rows]
            out.append((ordered, got, None))
        return out, rows, False, marks

    def need_dates(self, oid: str, members: Sequence[str]) -> list[tuple[int, date | None, Any]]:
        return [(rank_of(m), day_of(row["value"]), row) for m in members
                for row in self.props(m).get(NEED_PROP, [])]

    def confirm_links(self, members: Sequence[str]) -> list[dict[str, Any]]:
        """Every confirmation link to a member, at any tier (the engine settles them)."""
        return [link for m in members for link in self.confirms_of.get(m, [])]

    def reused_links(self, members: Sequence[str]) -> list[dict[str, Any]]:
        """The leads to a member on a number another order also holds (``reused_leads``)."""
        return [link for m in members for link in self.reused_of.get(m, [])]

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
            for row in (r for prop in PROMISE_PROPS for r in props.get(prop, [])):
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
            if floor:
                # a lowered floor's lead-only records come back apart (possibly_related)
                page = tt.with_leads(page)
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

    def _word(self, oid: str, name: str) -> tuple[Any] | None:
        """One text property as this principal reads it (a status word, never a quantity):
        ``(word,)``, ``(None,)`` when none is stored, ``(UNREAD,)`` when two differ; None
        when the answer withholds or cannot read it."""
        values = tool_values(self.call("plx.object", {"object_id": oid,
                                                      "properties": [name]}))
        if values is None:
            return None
        found = {str(v[0] if isinstance(v, tuple) else v).strip()
                 for v in values.get(name, []) if (v[0] if isinstance(v, tuple) else v)
                 not in (None, "")}
        if len(found) > 1:
            return (UNREAD,)
        return (next(iter(found), None),)

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
            state = self._word(item, FM_RECEIVING)
            if want is None or state is None:
                return UNREAD, [], True, []
            got = fm_item_received(state[0], want[0])
            if got is UNREAD:
                return UNREAD, [], False, []
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

    def _edges(self, oid: str, link: str, direction: str, floor: str
               ) -> list[dict[str, Any]] | None:
        """Every ``link`` edge one hop from ``oid`` this principal reads, every page; None
        when the listing is withheld in part or not complete (never decided from a part)."""
        from operating import ontology_tools_traverse as tt

        edges: list[dict[str, Any]] = []
        cursor = None
        for _ in range(PAGES):
            page = self.call("plx.around", {
                "from": oid, "limit": 50, "path": [{"link_type": link, "direction": direction}],
                "tier_floor": floor, **({"cursor": cursor} if cursor else {})})
            page = tt.with_leads(page)   # lead-only edges come back apart; tiers kept
            lost = {(g or {}).get("reason") for g in page.get("gaps") or ()} \
                & tt.PERMISSION_LOSSES
            if "error" in page or lost or (page.get("withheld") or {}).get("objects"):
                return None
            edges += [e for e in page.get("edges") or () if isinstance(e, Mapping)
                      and e.get("link_type") == link]
            completeness = page.get("completeness") or {}
            cursor = completeness.get("next_cursor")
            if not cursor:
                return edges if completeness.get("complete") is True else None
        return None

    def confirm_links(self, members: Sequence[str]) -> list[dict[str, Any]] | None:
        """Every confirmation link (``CONFIRMATION_LINKS``) to a member at any tier down to
        a lead, as ``StoreSource.confirm_links`` lists them; None when a listing is not
        read whole for this principal."""
        out: list[dict[str, Any]] = []
        for member in members:
            for link in CONFIRMATION_LINKS:
                edges = self._edges(member, link, "reverse", "lead")
                if edges is None:
                    return None
                out += [{"link_type": link, "from_object": e.get("from"), "to_object": member,
                         "tier": e.get("tier"), "markings": e.get("markings") or []}
                        for e in edges if e.get("to") == member
                        and isinstance(e.get("from"), str)]
        return out

    def colliding(self, members: Sequence[str]) -> bool:
        """Whether an order number a member holds is held by a purchase order no twin link
        joins to them (``StoreSource.colliding``), as this principal finds it; a number it
        cannot read, or a find it does not read whole, collides (a lead confirms nothing
        on a guess)."""
        numbers: set[str] = set()
        for member in members:
            values = tool_values(self.call("plx.object", {"object_id": member,
                                                          "properties": list(NUMBER_PROPS)}))
            if values is None:
                return True
            numbers |= {str(v) for p in NUMBER_PROPS for v in values.get(p, ()) if word(v)}
        for number in sorted(numbers):
            # untyped: the identifier lookup over every system (a typed find scans)
            found = self.call("plx.find", {"query": number[:200], "limit": 20})
            if "error" in found or (found.get("withheld") or {}).get("objects") or (
                    found.get("completeness") or {}).get("truncated"):
                return True
            for cand in found.get("candidates") or ():
                obj = (cand or {}).get("object") or {}
                other = obj.get("object_id") if obj.get("object_type") == PO_TYPE else None
                matched = (cand.get("matched") or {}) if isinstance(cand, Mapping) else {}
                said = matched.get("raw")
                said = said.get("value") if isinstance(said, Mapping) else said
                if isinstance(other, str) and other not in members \
                        and _number_key(matched.get("normalized") or said) == _number_key(number):
                    return True
        return False

    def promises(self, oid: str, members: Sequence[str]) -> list[tuple[Any, date, Any]]:
        out = []
        for member in members:
            found, _ = self._around(member, CONFIRMS, "reverse", f"{CONFIRMATION_TYPE}:",
                                    CONFIRMING[-1])
            if found is None:
                return None
            for confirmation in found:
                values = tool_values(self.call("plx.object", {
                    "object_id": confirmation, "properties": [*PROMISE_PROPS, PROMISE_ORDER]}))
                if values is None:
                    return None
                orders = {day_of(v) for v in values.get(PROMISE_ORDER, []) if v is not None}
                order = next(iter(orders)) if len(orders) == 1 else None
                for raw in (v for prop in PROMISE_PROPS for v in values.get(prop, [])):
                    day = day_of(raw)
                    if day is not None:
                        out.append((order, day, (confirmation, raw)))
        return out


# --------------------------------------------------------------------------- vocabulary

# The relations read, by the short name the summary keys them under.
RELATIONS = {"portal_po": "public.app_purchase_order",
             "portal_line": "public.app_purchase_order_line",
             "fm_po": "public.generated_purchase_order", "sage_po": "sage.po_header",
             # the FM items (gpi.idpogen = gpo.id, qty, status: FileMaker's own
             # receiving state of the item, fm_item_received)
             "fm_item": "public.generated_purchase_order_item"}
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
    """FM PO id -> its items as ``(qty, received by FileMaker's item state, None)`` from
    the scans (``fm_item_received``); ``UNREAD`` for every PO when the FM PO or item scan
    is not complete."""
    fm_scan, item_scan = scans.get("fm_po") or {}, scans.get("fm_item") or {}
    if not (fm_scan.get("complete") and item_scan.get("complete")):
        return lambda fm_id: UNREAD
    fm_ids = {str(r.get("id")) for r in fm_scan.get("rows") or ()}
    heads: dict[str, list[Any]] = defaultdict(list)
    for r in fm_scan.get("rows") or ():
        heads[str(r.get("id"))].append(r.get("status"))
    items: dict[str, list[tuple[Any, Any]]] = defaultdict(list)
    for row in item_scan.get("rows") or ():
        qty = row.get("qty", UNREAD)
        items[str(row.get("idpogen"))].append(
            (qty, fm_item_received(row.get("status", UNREAD), qty)))

    def log(fm_id: str) -> Any:
        if fm_id not in fm_ids:
            return UNREAD
        # an item with no state stored: the FM header decides it (``fm_by_header``)
        return fm_by_header([(qty, got, None) for qty, got in items.get(fm_id, ())],
                            heads.get(fm_id))
    return log


def _scan_label(scan: Mapping[str, Any]) -> dict[str, Any]:
    from integrations.source_mirror import scan_label
    return scan_label(scan)


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
            "reasons": list(scan.get("reasons") or ()), **_scan_label(scan)}
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
                       "qc_received" if received is PARTIAL_RECEIPT or received > EPS
                       else "qc_none"] += 1
            status = classify_received(raw, ordered, received)
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
    # ``sage`` closes an iscomplete 1 header as historical: the live check holds this at 0
    # (a complete header whose lines are open or unreadable reverses that mapping)
    out["sage_complete_lines_open"] = sum(
        n for value, c in cross.items() if value == "1"
        for state, n in c.items() if state != "lines_complete")
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
           "HISTORICAL_SAGE", "ISSUED_WORDS", "LATE", "NEITHER", "Need", "OPEN",
           "PAST_ORDERED", "PoRead", "PoStatus", "Promise", "StoreSource", "ToolSource",
           "UNKNOWN", "UNKNOWN_REASONS",
           "UNREAD", "bc", "classify", "clusters", "combine", "confirmation_state", "copies",
           "counted_confirmations", "header_class", "is_default_date", "vendor_facing",
           "NEVER_LATE_SKIPS", "late_count_exact", "lateness_verdict", "line_totals", "need_date", "promise", "read", "record",
           "sage", "vendor_verdict", "verdict", "vocabulary", "word"]


if __name__ == "__main__":
    sys.exit(main())
