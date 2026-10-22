"""The one purchase-order status definition every reader uses: the proactive engine, the
``po_delivery_window`` process graph (``open_when`` and the PO stage predicate), PO
lateness, the packaging eval check and the vocabulary live check
(docs/decisions/2026-10-21-po-open-sage-fm.md).

A PO's status word is its source's raw header text, never the Portal's derived code: the
Portal ``app_purchase_order.status`` column holds the FileMaker words (live capture
2026-10-20: Cancel, Complete, DRAFT, Pending, B.Order, Revised, Generated) and the Portal
derives its fulfillment code from the header and the summed line quantities
(plx-customer-portal portal/src/lib/mrp/po-fulfillment-status.ts derivePoFulfillmentCode;
@d39d91b open-po-lines/route.ts:38-46,302-312 and po-queries.ts:196-206: openness is by
quantity, FM Complete means issued). So:

* a cancelled word is **closed**; DRAFT is **draft** (not issued);
* any other mapped word (``ISSUED_WORDS``) is **open** while the summed received quantity
  is below the summed ordered one (``SUBMITTED`` nothing received, ``PARTIAL`` some) and
  **closed** (``RECEIVED``) once it covers it, within the Portal's ``EPS``; FM Complete
  alone never closes a PO;
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
reference or it is reported missing: the supplier's date is never put in its place.

``python3 -m operating.po_status`` is the ``po_status.vocabulary`` live check: one host
capture cycle, then offline over the mirror the per-word classes of every captured PO
header and the Sage cross-tab, counts and status words only.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import os
import re
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "plx-po-status-vocabulary/v1"
OPEN, CLOSED, DRAFT, UNKNOWN = "open", "closed", "draft", "unknown"
CLASSES = (OPEN, CLOSED, DRAFT, UNKNOWN)
SUBMITTED, PARTIAL, RECEIVED, CANCELLED = "SUBMITTED", "PARTIAL", "RECEIVED", "CANCELLED"
# po-fulfillment-status.ts: received + EPS < ordered is PARTIAL, more is RECEIVED.
EPS = Decimal("0.0001")
CANCELLED_WORDS = frozenset({"CANCEL", "CANCELLED", "CANCELED"})
DRAFT_WORDS = frozenset({"DRAFT"})
# Issued words: their quantities decide open or closed. The FileMaker words the Portal
# header holds, the Portal's own codes (a PO the Portal created) and ORDERED, which the
# purchase-order-sent hook writes on a send (purchase-order-sent.ts@4af6957:89-92).
ISSUED_WORDS = frozenset({"PENDING", "B.ORDER", "COMPLETE", "ORDERED",
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


@dataclass(frozen=True)
class PoStatus:
    cls: str                  # open / closed / draft / unknown
    code: str | None = None   # the Portal fulfillment code when decided
    reason: str | None = None  # UNKNOWN_REASONS when unknown


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
    if text in CANCELLED_WORDS:
        return IGNORED
    return ISSUED if text in ISSUED_WORDS else UNMAPPED


def classify(raw: Any, ordered: Decimal | None, received: Decimal | None) -> PoStatus:
    """A Portal or FileMaker header word with the PO's summed line quantities (None: not
    readable). Mirrors derivePoFulfillmentCode for the mapped words; stricter where it
    would guess: an unmapped or null word, unreadable quantities or nothing ordered is
    unknown (the Portal would call those SUBMITTED)."""
    kind = header_class(raw)
    if kind is None:
        return PoStatus(UNKNOWN, reason="status_missing")
    if kind == IGNORED:
        return PoStatus(CLOSED, CANCELLED)
    if kind == NOT_ISSUED:
        return PoStatus(DRAFT, "DRAFT")
    if kind == UNMAPPED:
        return PoStatus(UNKNOWN, reason="status_unmapped")
    if ordered is None or received is None:
        return PoStatus(UNKNOWN, reason="quantities_unknown")
    ordered, received = max(Decimal(0), ordered), max(Decimal(0), received)
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
        return PoStatus(cls, next(iter(codes)) if len(codes) == 1 else None)
    reasons = sorted({s.reason for s in statuses if s.reason},
                     key=UNKNOWN_REASONS.index)
    return PoStatus(UNKNOWN, reason=reasons[0] if reasons else "status_missing")


def record(values: Mapping[str, Sequence[Any]],
           lines: Iterable[tuple[Any, Any, Any]] | None) -> PoStatus:
    """One PurchaseOrder record from the raw values of its status properties
    (``SOURCE_OF``: ``status`` the Portal header, ``fm_status`` the FileMaker one,
    ``bc_status``/``bc_fully_received``, ``sage_is_complete``) and its own lines'
    ``(ordered, received, unit)`` (None: its lines were not read). The FileMaker header
    has no received quantity, so its issued words stay unknown; two values of one
    property are disputed; a record with no status property is ``status_missing``."""
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


def lateness_verdict(status: PoStatus | str, requested: date | None,
                     confirmed: date | None, today: date) -> tuple[str, str | None]:
    """``late``, ``at_risk`` or ``neither`` for one PO, or ``unknown`` with why. Late: open
    and the requested-by date has passed. At risk: open, not yet late, and the supplier's
    confirmed date is after the requested-by date. The requested-by date is never
    replaced by the supplier's date: without it the verdict is unknown."""
    cls = status.cls if isinstance(status, PoStatus) else status
    if cls in (CLOSED, DRAFT):
        return NEITHER, None
    if cls != OPEN:
        return UNKNOWN, "status_unknown"
    if requested is None:
        return UNKNOWN, "no_requested_date"
    if today > requested:
        return LATE, None
    if confirmed is not None and confirmed > requested:
        return AT_RISK, None
    return NEITHER, None


# --------------------------------------------------------------------------- vocabulary

# The relations read, by the short name the summary keys them under.
RELATIONS = {"portal_po": "public.app_purchase_order",
             "portal_line": "public.app_purchase_order_line",
             "fm_po": "public.generated_purchase_order", "sage_po": "sage.po_header"}
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


def vocabulary(scans: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Counts only from the relation scans (``scan_relation`` results with each row's
    cells, keyed as ``RELATIONS``): per raw status word the classes the shared definition
    gives its rows, the class totals, the unknown rows by word and by reason, the old
    rule's counts, and Sage iscomplete against linescmpl >= lines."""
    out: dict[str, Any] = {"relations": {}, "words": {}, "classes": {}, "unknown_words": {},
                           "unknown_reasons": {}, "before_engine_rule": {}}
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
    for name, source in (("portal_po", "portal"), ("fm_po", "fm")):
        scan = scans.get(name) or {}
        words: dict[str, Counter] = defaultdict(Counter)
        classes, unknown, reasons, before = Counter(), Counter(), Counter(), Counter()
        for row in scan.get("rows") or ():
            raw = row.get("status")
            if source == "portal" and lines_complete:
                ordered, received = line_totals(lines.get(row.get("id"), ()))
            else:   # the FM header carries no received quantity; an unread line scan
                ordered = received = None
            status = classify(raw, ordered, received)
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


__all__ = ["AT_RISK", "CANCELLED_WORDS", "CLASSES", "CLOSED", "DRAFT", "DRAFT_WORDS", "EPS",
           "HEADER_PROPS", "ISSUED_WORDS", "LATE", "NEITHER", "OPEN", "PAST_ORDERED", "PoStatus", "UNKNOWN",
           "UNKNOWN_REASONS", "UNREAD", "bc", "classify", "combine", "header_class",
           "lateness_verdict", "line_totals", "record", "sage", "vocabulary", "word"]


if __name__ == "__main__":
    sys.exit(main())
