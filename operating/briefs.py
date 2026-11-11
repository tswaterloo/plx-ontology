"""Briefs: a meeting prep brief and a weekly operations review, generated from the ontology
as ONE reader reads it, with no model call.

Both are built only from the reader's own tool calls (``call``: ``plx.object``,
``plx.around``, ``plx.find``, ``plx.aggregate`` dispatched as the reader, so every
permission rule is the primitives') and the reader's own trimmed alert inbox (``alerts``:
``ObjectWorkspace.gap_inbox``). On top of that, every value used is dropped unless the
reader holds all its markings (``held``), even if a caller handed an unfiltered read, and
a section about money (payments, balance, cash, margin) needs ``finance`` itself: without
it the section says only that it is finance-marked, the same words whatever the data holds.

No hidden counts: a count, a total or a percentage is computed only over what this reader
read; what the primitives withheld is never counted, named or hinted at (their withheld
counts are not passed on). What could not be read becomes a section state and a typed gap
(``read_truncated``, ``budget_exhausted``, ``source_not_served``, ``read_failed``), never a
silent empty section.

Meeting prep brief (``meeting_brief``): for a calendar event or meeting, its outside
parties are the customers and suppliers its links name (``CalendarEvent.with_customer``
and siblings: attendees' addresses and domains, ``ontology/sender_parties.py``); only
links above lead tier make a party, a lead-tier match is listed as a possible party and
never read further (prepare a brief for it by its own page). A brief can also be asked
for a customer or supplier directly. Sections: open orders and purchase orders, late items
and alerts, recent mail and Teams threads (subjects and dates only), payments and balance
(finance), open promises (the commitments alerts), quality issues, the last meeting's
notes when a transcript was captured, with the alerts that cite that meeting (its action
items), and talking points drawn from the shown lines (each keeps its line's citations).
Thread rows name who sent them (the sender's person record, read as the reader) only once
the people exclusion (``people``) is loaded: without it the thread and notes sections are
withheld and say so (fail closed), and a sender the exclusion names or cannot decide drops
its row.

Item access (``decide``: handles -> None when the reader may open it): every row and figure
is decided on all of its citations before any count or truncation, so a row the reader may
not open is neither shown nor counted; a decision that fails withholds every section.

Weekly review (``weekly_review``): for one Monday-to-Sunday week and the week before it,
orders booked, shipped and invoiced (counts, and invoiced totals per currency for a finance
reader), on-time shipping (shipments in the week against their order's required date),
late purchase orders and the top alerts open now, cash position and margin signals
(finance), quality holds, and the week-over-week change of each weekly figure. Totals are
per currency, never summed across currencies; money is Decimal. Also: top slips with their
reasons, AR and AP aging (finance), margin movers (finance), supplier reliability, alerts
opened and closed in the week (from the alert list read with its resolved alerts), promises
missed and the top customer asks. ``WeeklyArchive`` keeps each reader's finished weeks so a
past week reads as it was generated, re-decided for the reader before it is served.

Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

from operating.cited_reads import CitedReads, text_of

MEETING_SCHEMA = "plx-meeting-brief/v1"
WEEKLY_SCHEMA = "plx-weekly-review/v1"
FINANCE = "finance"
MAX_PARTIES = 3          # outside parties one meeting brief reads
MAX_ROWS = 10            # rows a section lists (the rest is "more", counted visibly)
MAX_SOURCES = 5          # citations one row or figure carries
MAX_TEXT = 160           # a subject or number as shown (untrusted text, clipped)
THREAD_DAYS = 90         # "recent" mail and Teams
UPCOMING_DAYS = 14
BUDGET_SECONDS = 15.0     # each brief answers well under 20 s on the live store
MAX_LINKED = 200         # ids one hop reads (more: read_truncated)
MAX_READS = 150          # records one section reads one by one (the time budget bounds it too)
TOP_ALERTS = 5

MEETING_TYPES = ("CalendarEvent", "Meeting")
PARTY_TYPES = ("Customer", "Supplier")
# The links from a meeting record to its outside parties (only those the schema declares
# are walked).
PARTY_LINKS = {"CalendarEvent": ("CalendarEvent.with_customer", "CalendarEvent.with_supplier"),
               "Meeting": ("Meeting.with_customer", "Meeting.with_supplier")}
# Per party type: the records a brief reads, each (link type walked in reverse from the
# party, properties read). The first ``orders`` link gives the open orders section.
ORDERS = {"Customer": ("SalesOrder.ordered_by",
                       ("order_number", "status", "bc_fully_shipped", "sage_complete",
                        "order_date", "required_date", "currency", "total_amount")),
          "Supplier": ("PurchaseOrder.ordered_from",
                       ("po_number", "status", "bc_fully_received", "sage_is_complete",
                        "order_date", "expected_date", "currency", "total_cost"))}
ORDER_NUMBER = ("order_number", "po_number")
ORDER_DUE = ("required_date", "expected_date")
ORDER_AMOUNT = ("total_amount", "total_cost")
DONE_FLAGS = ("bc_fully_shipped", "bc_fully_received", "sage_complete", "sage_is_complete")
# Other records of a party whose alerts belong to its brief (ids only, never read).
RELATED = {"Customer": ("SalesInvoice.billed_to", "Shipment.shipped_to", "Quote.for_customer",
                        "Payment.received_from"),
           "Supplier": ("PurchaseInvoice.billed_by", "Receipt.supplied_by",
                        "Payment.paid_to")}
THREAD_LINKS = {"Customer": ("Email.sent_by_customer", "Email.mentions_customer",
                             "TeamsChatMessage.mentions_customer",
                             "TeamsChannelMessage.mentions_customer"),
                "Supplier": ("Email.sent_by_supplier", "Email.mentions_supplier",
                             "TeamsChatMessage.mentions_supplier",
                             "TeamsChannelMessage.mentions_supplier")}
THREAD_PROPS = ("subject", "received_at", "sent_at", "created_at")
PAYMENT_LINKS = {"Customer": "Payment.received_from", "Supplier": "Payment.paid_to"}
BALANCE_LINKS = {"Customer": ("AgedReceivable.of_customer", "AgedReceivable.customer_record"),
                 "Supplier": ("AgedPayable.of_supplier", "AgedPayable.supplier_record")}
MEETINGS_OF = {"Customer": "Meeting.with_customer", "Supplier": "Meeting.with_supplier"}

# Alert kinds by section (every other visible alert on the party's records is "alerts").
PROMISE_KINDS = frozenset({"commitment_due", "commitment_missed", "promise_date_slip",
                           "tell_before_they_ask"})
QUALITY_KINDS = frozenset({"lot_on_hold", "quality_hold_aging", "reject_trend",
                           "lot_near_expiry", "assembly_damage_spike",
                           "recurring_component_damage"})
LATE_PO_KINDS = frozenset({"late_po", "unconfirmed_po"})
MARGIN_KINDS = frozenset({"negative_margin", "margin_erosion", "unit_cost_jump",
                          "price_below_cost", "customer_price_drop", "price_below_typical",
                          "po_impact_margin"})
SLIP_KINDS = frozenset({"promise_date_slip", "eta_slipped", "late_customer_order",
                        "production_late", "order_at_risk", "po_impact"})
AGING_KINDS = frozenset({"aged_receivable_worsening", "aged_payable_worsening",
                         "overdue_receivable", "customer_paying_slower"})
SUPPLIER_KINDS = frozenset({"supplier_reliability", "vendor_no_reply", "eta_slipped",
                            "unconfirmed_po"})
ASK_KINDS = frozenset({"customer_chasing_order", "teams_unanswered_ask", "needs_my_reply",
                       "tell_before_they_ask", "quote_accepted_no_order"})
# Finance-marked alert kinds: shown only to a finance reader, whatever section they fit.
FINANCE_KINDS = MARGIN_KINDS | AGING_KINDS
# Sections whose lines come from people's mail, chats and meetings: withheld while the
# people exclusion is not loaded.
PEOPLE_SECTIONS = frozenset({"threads", "notes", "promises", "asks", "commitments_missed"})
# Alert kinds read from people's mail, chats and meetings: dropped from every section while
# the people exclusion is not loaded.
PEOPLE_KINDS = PROMISE_KINDS | ASK_KINDS | frozenset({"commitment_missed", "vendor_no_reply",
                                                   "approval_waiting", "chase_escalating",
                                                   "party_chase_unanswered",
                                                   "internal_order_chase",
                                                   "rd_project_line", "rd_project_idle"})
PEOPLE_NOTE = ("The people exclusion is not loaded, so no mail, Teams or meeting line is "
               "shown here; this section may still have items.")
ITEM_NOTE = ("Your item access could not be checked, so nothing is shown here; this section "
             "may still have items.")
MAX_POINTS = 6           # talking points one brief suggests
PERSON_LINK = {"Email": "Email.sent_by_person",
               "TeamsChatMessage": "TeamsChatMessage.sent_by_person",
               "TeamsChannelMessage": "TeamsChannelMessage.sent_by_person"}
PRIORITY = {"high": 0, "medium": 1, "low": 2}

CLOSED_WORDS = frozenset({"closed", "cancelled", "canceled", "completed", "complete",
                          "realized", "cancel", "shipped", "invoiced", "received",
                          "fully received", "fully shipped"})

Call = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
Decide = Callable[[Sequence[str]], Mapping[str, Any]]


class BriefRefused(ValueError):
    """The record asked for is not one a brief can be prepared for (or not visible)."""


# ------------------------------------------------------------------------------- helpers

def type_of(object_id: str) -> str:
    return object_id.split(":", 1)[0] if isinstance(object_id, str) else ""


def _marks(value: Any) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return None   # unreadable markings: no reader holds them


def held_all(markings: Any, held: frozenset[str]) -> bool:
    marks = _marks(markings)
    return marks is not None and set(marks) <= held


def _clip(value: Any, n: int = MAX_TEXT) -> str | None:
    words = text_of(value)
    if words is None:
        return None
    words = " ".join(str(words).split())[:n]
    return words or None


def _day(value: Any) -> date | None:
    raw = text_of(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    raw = text_of(value)
    try:
        number = Decimal(str(raw).replace(",", "").strip()) if raw is not None else None
    except (InvalidOperation, ValueError):
        return None
    return number if number is not None and number.is_finite() else None


def _money(amount: Decimal) -> str:
    return format(amount.quantize(Decimal("0.01")), "f")


class Reads:
    """The reader's reads for one brief: ``CitedReads`` (budget, paging, typed gaps) with
    every cell checked against ``held``. Withheld counts are never passed on."""

    def __init__(self, call: Call, held: Iterable[str], *, budget_seconds: float,
                 clock: Callable[[], float] | None = None,
                 declared: frozenset[str] | None = None) -> None:
        kw: dict[str, Any] = {"budget_seconds": budget_seconds}
        if clock is not None:
            kw["clock"] = clock
        self.cited = CitedReads(lambda name, args, _actor: call(name, args), None,
                                max_records=MAX_LINKED, **kw)
        self.call = call
        self.held = frozenset(held)
        self.declared = declared
        self.today = datetime.now(timezone.utc).date()

    def finance(self) -> bool:
        return FINANCE in self.held

    def declares(self, link_type: str) -> bool:
        return self.declared is None or link_type in self.declared

    def cell(self, record: Any, *names: str) -> tuple[Any, str | None]:
        """(value, citation) of the first of ``names`` this reader holds the markings of."""
        for name in names:
            cell = (record.values or {}).get(name) if record is not None else None
            if not cell or cell.get("value") is None:
                continue
            if not held_all(cell.get("markings"), self.held):
                continue
            handle = (cell.get("citation") or {}).get("l1_handle")
            return cell["value"], handle if isinstance(handle, str) else None
        return None, None

    def linked(self, oid: str, link_type: str, direction: str = "reverse") -> list[str]:
        if not self.declares(link_type):
            return []
        return self.cited.around(oid, link_type, direction) or []

    def possible(self, oid: str, link_type: str) -> list[str]:
        """Lead-tier matches only (``possibly_related``): never a party."""
        if not self.declares(link_type) or self.cited.over():
            return []
        out = self.call("plx.around", {"from": oid, "limit": MAX_PARTIES, "path": [
            {"link_type": link_type, "direction": "forward"}]})
        rel = (out or {}).get("possibly_related") if isinstance(out, Mapping) else None
        return [o["object_id"] for o in (rel or {}).get("objects") or ()
                if isinstance(o, Mapping) and isinstance(o.get("object_id"), str)]

    def read(self, oid: str, props: Sequence[str] | None = None) -> Any:
        if self.cited.over():
            return None
        return self.cited.read(oid, props)

    def label(self, oid: str) -> tuple[str, str | None]:
        """The record's display words as this reader reads them, and its citation."""
        out = self.call("plx.object", {"object_id": oid}) if not self.cited.over() else None
        obj = (out or {}).get("object") if isinstance(out, Mapping) else None
        display = (obj or {}).get("display")
        words = _clip(display)
        handle = None
        if isinstance(display, Mapping):
            origin = display.get("origin") or {}
            handle = origin.get("citation") if isinstance(origin.get("citation"), str) else None
        return words or type_of(oid), handle

    def gaps(self) -> list[dict[str, str]]:
        """The typed gaps of the reads, without any count of what was withheld."""
        never = ("withheld_from_reader", "nothing_visible")
        return [dict(g) for g in self.cited.gaps
                if g.get("code") not in never and g.get("state") != "inaccessible"]


def _source(handle: str | None) -> list[dict[str, str]]:
    return [{"handle": handle}] if isinstance(handle, str) and handle else []


def _row(label: str, *, detail: str | None = None, day: date | None = None,
         amount: tuple[Decimal, str] | None = None, object_id: str | None = None,
         sources: Iterable[str | None] = (), who: str | None = None) -> dict[str, Any]:
    handles = list(dict.fromkeys(h for h in sources if isinstance(h, str) and h))
    return {"label": label[:MAX_TEXT], "detail": detail, "date": day.isoformat() if day else None,
            "who": who,
            "amount": ({"amount": _money(amount[0]), "currency": amount[1]} if amount else None),
            "object_id": object_id, "sources": [{"handle": h} for h in handles[:MAX_SOURCES]]}


def _section(key: str, title: str, rows: list[dict[str, Any]], *, state: str | None = None,
             note: str | None = None, figures: list[dict[str, Any]] | None = None,
             partial: bool = False, limit: int = MAX_ROWS) -> dict[str, Any]:
    # every row is kept here: ``finish`` decides them for the reader, then truncates to
    # ``limit`` (a row the reader may not open is never counted in "more")
    return {"key": key, "title": title, "state": state, "note": note, "rows": list(rows),
            "more": 0, "figures": figures or [], "_partial": partial, "_limit": limit}


def _handles_of(item: Mapping[str, Any]) -> list[str]:
    return [s["handle"] for s in item.get("sources") or () if isinstance(s, Mapping)
            and isinstance(s.get("handle"), str)]


def finish(sections: list[dict[str, Any]], decide: Decide | None, reads: "Reads"
           ) -> list[dict[str, Any]]:
    """Decide every row and figure for the reader on all its citations (``decide``: None
    means the reader's reads were decided already), drop what it may not open, then
    truncate to each section's limit (``MAX_ROWS``) and set its state. A decision that
    fails withholds every section (fail closed)."""
    refused: set[str] | None = set()
    if decide is not None:
        handles = sorted({h for s in sections for item in s["rows"] + s["figures"]
                          for h in _handles_of(item)})
        try:
            decided = decide(handles) if handles else {}
            refused = {h for h in handles if not isinstance(decided, Mapping)
                       or h not in decided or decided[h] is not None}
        except Exception:  # noqa: BLE001 - an undecidable item is never shown
            refused = None
            reads.cited.add({"code": "read_failed", "state": "unknown",
                             "subject": "item access",
                             "detail": "the reader's item access could not be checked; "
                                       "no section is shown"})
    out = []
    for s in sections:
        partial = s.pop("_partial", False)
        limit = s.pop("_limit", MAX_ROWS)
        if refused is None and s["state"] is None and (s["rows"] or s["figures"]):
            s.update(state="unread", note=ITEM_NOTE, rows=[], figures=[])
        if refused:
            s["rows"] = [r for r in s["rows"] if not set(_handles_of(r)) & refused]
            s["figures"] = [dict(f, value=None, previous=None, delta=None, sources=[],
                                 note="not readable for you")
                            if set(_handles_of(f)) & refused else f for f in s["figures"]]
        rows = s["rows"]
        s["rows"], s["more"] = rows[:limit], max(0, len(rows) - limit)
        if s["state"] is None:
            s["state"] = "partial" if partial else (
                "shown" if rows or s["figures"] else "empty")
        out.append(s)
    return out


def _finance_marked(key: str, title: str) -> dict[str, Any]:
    return _section(key, title, [], state="finance_marked",
                    note="Finance-marked: shown only to readers who hold finance.")


def declared_links() -> frozenset[str] | None:
    try:
        from ontology.schema import load_schema
        return frozenset(load_schema().link_types)
    except Exception:  # noqa: BLE001 - no schema: every link is tried, refusals become gaps
        return None


# ------------------------------------------------------------------------------- alerts

def _cite_handles(gap: Mapping[str, Any]) -> list[str]:
    out = []
    for group in gap.get("groups") or ():
        for cite in group or ():
            if isinstance(cite, (list, tuple)) and cite and isinstance(cite[0], str):
                out.append(cite[0])
    return list(dict.fromkeys(out))


def _cite_objects(gap: Mapping[str, Any]) -> set[str]:
    out = {gap.get("object_id")}
    for group in gap.get("groups") or ():
        for cite in group or ():
            if isinstance(cite, (list, tuple)) and len(cite) > 1 and isinstance(cite[1], str):
                out.add(cite[1])
    return {o for o in out if isinstance(o, str)}


def alert_label(kind: str) -> str:
    try:
        from operating import briefing as bf
        spec = bf.EVENT_TYPES.get(bf.TYPE_OF_KIND.get(kind, ""), {})
        return str(spec.get("label") or kind.replace("_", " "))
    except Exception:  # noqa: BLE001
        return kind.replace("_", " ")


def _alert_rows(gaps: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(gaps, key=lambda g: (
        PRIORITY.get(str((g.get("follow_up") or {}).get("priority")), 3),
        -int((g.get("follow_up") or {}).get("days_late") or 0), str(g.get("id"))))
    return [_row(alert_label(str(g.get("kind"))), detail=_clip(g.get("summary"), 300),
                 object_id=g.get("object_id") if isinstance(g.get("object_id"), str) else None,
                 sources=_cite_handles(g)) for g in ranked]


UNREAD_NOTE = ("Your alert list could not be read, so nothing is shown here; this section "
               "may still have items.")


def alerts_read(alerts: Mapping[str, Any] | None, reads: "Reads") -> bool:
    """Whether the reader's alert list read; when not, a ``read_failed`` gap says so."""
    if isinstance(alerts, Mapping) and alerts.get("status") == "read":
        return True
    status = alerts.get("status") if isinstance(alerts, Mapping) else None
    reads.cited.add({"code": "read_failed", "state": "unknown", "subject": "alert list",
                     "detail": f"the alert list did not read (status {str(status)[:40]}); "
                               "the alert sections could not be read"})
    return False


def _alert_section(key: str, title: str, gaps: Iterable[Mapping[str, Any]], read: bool, *,
                   limit: int | None = None, note: str | None = None,
                   figures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not read:
        return _section(key, title, [], state="unread", note=UNREAD_NOTE)
    rows = _alert_rows(gaps)
    return _section(key, title, rows[:limit] if limit else rows, note=note, figures=figures)


def gate_alerts(gaps: list[Mapping[str, Any]], decide: Decide | None
                ) -> list[Mapping[str, Any]] | None:
    """The alerts whose every citation this reader may open (``decide``), before anything
    is counted; None when the decision fails (the alert sections are then unread)."""
    if decide is None or not gaps:
        return gaps
    handles = sorted({h for g in gaps for h in _cite_handles(g)})
    try:
        decided = decide(handles)
        refused = {h for h in handles if h not in decided or decided[h] is not None}
    except Exception:  # noqa: BLE001
        return None
    return [g for g in gaps if not set(_cite_handles(g)) & refused]


def visible_alerts(alerts: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The reader's trimmed inbox alerts (already decided for this reader), only well-formed
    ones carrying a citation."""
    if not isinstance(alerts, Mapping) or alerts.get("status") != "read":
        return []
    return [g for g in alerts.get("gaps") or () if isinstance(g, Mapping)
            and isinstance(g.get("kind"), str) and _cite_handles(g)]


# ------------------------------------------------------------------------ people guard

def people_unavailable(people: Any) -> str | None:
    """Why the people exclusion cannot decide now (None: it can). No guard at all is
    unavailable: the sections that rest on people's mail, chats and meetings fail closed."""
    if people is None:
        return "exclusion_not_loaded"
    try:
        code = getattr(people, "unavailable")
    except Exception:  # noqa: BLE001
        return "exclusion_not_loaded"
    return str(code) if code else None


def env_people() -> Any:
    """The people exclusion as the passes load it (environment, then the protected env
    file); None when it cannot be built (fail closed)."""
    try:
        from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
        return PeopleExclusion.from_env(environ_with_exclusion())
    except Exception:  # noqa: BLE001
        return None


def _people_withheld(reads: "Reads", code: str) -> None:
    reads.cited.add({"code": "people_exclusion_unavailable", "state": "unknown",
                     "subject": "people exclusion",
                     "detail": f"the people exclusion is not loaded ({code[:40]}); mail, "
                               "Teams and meeting lines are not shown"})


def _withheld(key: str, title: str) -> dict[str, Any]:
    return _section(key, title, [], state="blocked", note=PEOPLE_NOTE)


def _sender(reads: "Reads", oid: str, people: Any, *, name: bool = True
            ) -> tuple[bool, str | None, str | None]:
    """(keep the row, who sent it as this reader reads it, its citation). A sender the
    exclusion names, or one it cannot decide, drops the row; no sender link: kept,
    nobody named."""
    link = PERSON_LINK.get(type_of(oid))
    persons = reads.linked(oid, link, "forward")[:1] if link else []
    for person in persons:
        record = reads.read(person, ("object_id", "upn"))
        ident, _ = reads.cell(record, "object_id", "upn")
        try:
            if ident is None or people.is_excluded(str(text_of(ident) or "")):
                return False, None, None
        except Exception:  # noqa: BLE001 - undecidable: never shown
            return False, None, None
        if not name:
            return True, None, None
        words, handle = reads.label(person)
        return True, words, handle
    if reads.cited.over():
        return False, None, None   # not checked before the budget ran out: never shown
    return True, None, None


# ------------------------------------------------------------------------- meeting brief

def _open(reads: Reads, record: Any) -> bool | None:
    """True open, False closed by its own cited status or a done flag; None unknown."""
    for flag in DONE_FLAGS:
        value, _ = reads.cell(record, flag)
        if str(text_of(value)).strip().lower() in ("true", "1", "yes"):
            return False
    status, _ = reads.cell(record, "status")
    words = (text_of(status) or "").strip().strip('"').lower()
    if not words:
        return None
    return words not in CLOSED_WORDS


def _order_rows(reads: Reads, party: str, ptype: str, ids: list[str]
                ) -> tuple[list[dict[str, Any]], bool]:
    link, props = ORDERS[ptype]
    rows, partial = [], len(ids) > MAX_READS or reads.cited.over() is not None
    for oid in ids[:MAX_READS]:
        record = reads.read(oid, props)
        if record is None:
            partial = partial or reads.cited.over() is not None
            continue
        state = _open(reads, record)
        if state is False:
            continue
        number, n_handle = reads.cell(record, *ORDER_NUMBER)
        status, s_handle = reads.cell(record, "status")
        due, d_handle = reads.cell(record, *ORDER_DUE)
        amount = None
        a_handle = c_handle = None
        if reads.finance():
            value, a_handle = reads.cell(record, *ORDER_AMOUNT)
            cur, c_handle = reads.cell(record, "currency")
            number_value, currency = _decimal(value), _clip(cur, 3)
            if number_value is not None and currency and len(currency) == 3:
                amount = (number_value, currency.upper())
        detail = (f"status {_clip(status, 40)}" if status is not None
                  else "status not recorded") + ("" if state else " (open or closed unknown)")
        due_day = _day(due)
        if state and due_day is not None and due_day < reads.today:
            detail += f"; {(reads.today - due_day).days} days past its " + (
                "required" if ptype == "Customer" else "expected") + " date"
        rows.append(_row(f"{'Order' if ptype == 'Customer' else 'PO'} "
                         f"{_clip(number, 60) or type_of(oid)}", detail=detail,
                         day=_day(due), amount=amount, object_id=oid,
                         sources=[n_handle, s_handle, d_handle, a_handle, c_handle]))
    rows.sort(key=lambda r: (r["date"] is None, r["date"] or "", r["label"]))
    return rows, partial


def _thread_rows(reads: Reads, ptype: str, party: str, today: date, people: Any = None
                 ) -> tuple[list[dict[str, Any]], bool]:
    ids: list[str] = []
    for link in THREAD_LINKS[ptype]:
        ids.extend(i for i in reads.linked(party, link) if i not in ids)
    rows, since = [], today - timedelta(days=THREAD_DAYS)
    partial = len(ids) > MAX_READS
    for oid in ids[:MAX_READS]:
        record = reads.read(oid, THREAD_PROPS)
        if record is None:
            partial = partial or reads.cited.over() is not None
            continue
        when, w_handle = reads.cell(record, "received_at", "sent_at", "created_at")
        day = _day(when)
        if day is None or day < since or day > today + timedelta(days=1):
            continue
        subject, s_handle = reads.cell(record, "subject")
        channel = "Teams" if type_of(oid).startswith("Teams") else "Mail"
        rows.append(_row(f"{channel}: {_clip(subject) or '(no subject)'}", day=day,
                         object_id=oid, sources=[s_handle, w_handle]))
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    named: list[dict[str, Any]] = []
    for row in rows:
        # every row's sender is decided by the exclusion before it is shown or counted;
        # who sent it is named for the rows a section shows
        keep, who, handle = _sender(reads, row["object_id"], people,
                                    name=len(named) < MAX_ROWS)
        if not keep:
            partial = partial or reads.cited.over() is not None
            continue
        row["who"] = who
        if handle:
            row["sources"] = (row["sources"] + [{"handle": handle}])[:MAX_SOURCES]
        named.append(row)
    return named, partial


def _finance_rows(reads: Reads, ptype: str, party: str) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    for link in BALANCE_LINKS[ptype]:
        for oid in reads.linked(party, link)[:MAX_ROWS]:
            record = reads.read(oid, ("balance_due", "currency", "aged_as_of_date",
                                      "period1_amount", "period2_amount", "period3_amount"))
            value, v_handle = reads.cell(record, "balance_due")
            cur, c_handle = reads.cell(record, "currency")
            as_of, d_handle = reads.cell(record, "aged_as_of_date")
            amount, currency = _decimal(value), _clip(cur, 3)
            if amount is None or not currency or len(currency) != 3:
                continue
            rows.append(_row("Balance due", detail="aged balance", day=_day(as_of),
                             amount=(amount, currency.upper()), object_id=oid,
                             sources=[v_handle, c_handle, d_handle]))
            overdue, cites, known = Decimal(0), [], True
            for bucket in ("period1_amount", "period2_amount", "period3_amount"):
                value, b_handle = reads.cell(record, bucket)
                part = _decimal(value)
                if part is None:
                    known = False   # one bucket unread: no overdue figure is stated
                    break
                overdue += part
                cites.append(b_handle)
            if known and overdue > 0:
                rows.append(_row("Overdue", detail="past-due buckets of the aged balance",
                                 day=_day(as_of), amount=(overdue, currency.upper()),
                                 object_id=oid, sources=[c_handle, d_handle] + cites))
    ids = reads.linked(party, PAYMENT_LINKS[ptype])
    payments = []
    for oid in ids[:MAX_READS]:
        record = reads.read(oid, ("document_number", "posting_date", "amount"))
        value, v_handle = reads.cell(record, "amount")
        when, d_handle = reads.cell(record, "posting_date")
        number, n_handle = reads.cell(record, "document_number")
        amount = _decimal(value)
        if amount is None:
            continue
        # the payment record names no currency: the figure is stated, never converted
        payments.append(_row(f"Payment {_clip(number, 60) or ''}".strip(),
                             detail=f"{_money(amount)} (currency not recorded on the payment)",
                             day=_day(when), object_id=oid,
                             sources=[v_handle, d_handle, n_handle]))
    payments.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows + payments, len(ids) > MAX_READS


def _last_notes(reads: Reads, ptype: str, party: str, now: datetime, exclude: set[str],
                last: set[str] | None = None) -> list[dict[str, Any]]:
    """The last earlier meeting's transcript row; ``last`` collects that meeting's ids (the
    meeting, its transcripts) so its action items can be found among the alerts."""
    best: tuple[str, str, Any, str | None] | None = None
    for oid in reads.linked(party, MEETINGS_OF[ptype])[:MAX_READS]:
        if oid in exclude:
            continue
        record = reads.read(oid, ("subject", "start"))
        start, s_handle = reads.cell(record, "start")
        raw = text_of(start) or ""
        if not raw or raw[:19] >= now.strftime("%Y-%m-%dT%H:%M:%S"):
            continue
        if best is None or raw > best[0]:
            best = (raw, oid, record, s_handle)
    if best is None:
        return []
    raw, oid, record, s_handle = best
    transcripts = reads.linked(oid, "MeetingTranscript.of_meeting")
    if last is not None:
        last.update([oid] + transcripts)
    if not transcripts:
        return []
    subject, t_handle = reads.cell(record, "subject")
    tid = transcripts[0]
    words, handle = reads.label(tid)
    return [_row(f"Notes: {_clip(subject) or words}", detail="meeting transcript captured",
                 day=_day(raw), object_id=tid, sources=[handle, s_handle, t_handle])]


RD_NOTE = ("The customer's sample, formulation, stability and certificate threads as projects, "
           "newest first; a status update is drafted on the R&D board.")
RD_STATE = {"waiting_on_us": "waiting on us", "waiting_on_customer": "waiting on the customer",
            "update_received": "update received", "closed": "closed"}


def _rd_rows(gaps: Iterable[Mapping[str, Any]], parties: Sequence[str]) -> list[dict[str, Any]]:
    """The R&D and sample projects of these parties (``operating.rd_projects`` lines of the
    reader's own decided alerts, one row per project: its latest line), newest first."""
    from operating import rd_projects as rd
    latest: dict[str, tuple[Mapping[str, Any], Mapping[str, Any], list[str]]] = {}
    for g in gaps:
        event = g.get("event") if isinstance(g.get("event"), Mapping) else {}
        if g.get("kind") != rd.LINE or event.get("customer") not in parties \
                or event.get("kind") not in rd.KIND_WORD or event.get("state") not in RD_STATE \
                or not isinstance(event.get("project"), str):
            continue
        key = event["project"]
        cites = (latest[key][2] if key in latest else []) + _cite_handles(g)
        if key not in latest or str(event.get("last_on") or "") > \
                str(latest[key][1].get("last_on") or ""):
            latest[key] = (g, event, cites)
        else:
            latest[key] = (latest[key][0], latest[key][1], cites)
    rows = []
    for g, event, cites in sorted(latest.values(), key=lambda x: (
            str(x[1].get("last_on") or ""), x[1]["project"]), reverse=True):
        rows.append(_row(f"{rd.KIND_WORD[event['kind']].capitalize()}: "
                         f"{RD_STATE[event['state']]}", day=_day(event.get("last_on")),
                         detail=f"last activity {event.get('last_on')}",
                         object_id=g.get("object_id") if isinstance(g.get("object_id"), str)
                         else None, sources=cites))
    return rows


def meeting_brief(call: Call, held: Iterable[str], alerts: Mapping[str, Any] | None,
                  object_id: str, *, now: datetime | None = None,
                  budget_seconds: float = BUDGET_SECONDS,
                  clock: Callable[[], float] | None = None,
                  declared: frozenset[str] | None | bool = True,
                  decide: Decide | None = None, people: Any = None) -> dict[str, Any]:
    """The meeting prep brief for ``object_id`` (a calendar event, a meeting, a customer or
    a supplier) as this reader reads it (module doc). ``BriefRefused`` for any other type
    or a record this reader cannot read. ``decide``: the reader's item decisions;
    ``people``: the people exclusion (None or unavailable: people's lines withheld)."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    otype = type_of(object_id)
    if otype not in MEETING_TYPES + PARTY_TYPES:
        raise BriefRefused("not_preparable")
    reads = Reads(call, held, budget_seconds=budget_seconds, clock=clock,
                  declared=declared_links() if declared is True else (declared or None))
    reads.today = today
    blocked = people_unavailable(people)
    if blocked:
        _people_withheld(reads, blocked)
    subject = reads.read(object_id)
    if subject is None:
        raise BriefRefused("not_found")
    title, title_handle = reads.label(object_id)
    when = None
    parties: list[str] = []
    possible: list[str] = []
    if otype in MEETING_TYPES:
        start, _ = reads.cell(subject, "start")
        when = text_of(start)
        for link in PARTY_LINKS[otype]:
            parties.extend(p for p in reads.linked(object_id, link, "forward")
                           if p not in parties and type_of(p) in PARTY_TYPES)
            possible.extend(p for p in reads.possible(object_id, link)
                            if p not in possible and p not in parties)
    else:
        parties = [object_id]
    parties, possible = parties[:MAX_PARTIES], possible[:MAX_PARTIES]
    party_refs = [{"object_id": p, "type": type_of(p), "label": reads.label(p)[0]}
                  for p in parties]
    possible_refs = [{"object_id": p, "type": type_of(p), "label": reads.label(p)[0]}
                     for p in possible]
    many = len(parties) > 1
    names = {r["object_id"]: r["label"] for r in party_refs}

    def tagged(rows: list[dict[str, Any]], party: str) -> list[dict[str, Any]]:
        if many:
            for row in rows:
                row["detail"] = f"{names[party]}: {row['detail']}" if row["detail"] \
                    else names[party]
        return rows

    orders, threads, money, notes = [], [], [], []
    partial = {"orders": False, "threads": False, "money": False}
    related: set[str] = set(parties)
    last: set[str] = set()
    exclude = {object_id}
    if otype == "CalendarEvent":
        exclude.update(reads.linked(object_id, "CalendarEvent.of_meeting", "forward"))
    for party in parties:
        ptype = type_of(party)
        ids = reads.linked(party, ORDERS[ptype][0])
        related.update(ids)
        for link in RELATED[ptype]:
            related.update(reads.linked(party, link))
        rows, cut = _order_rows(reads, party, ptype, ids)
        orders += tagged(rows, party)
        partial["orders"] |= cut
        if not blocked:
            rows, cut = _thread_rows(reads, ptype, party, today, people)
            threads += tagged(rows, party)
            partial["threads"] |= cut
        if reads.finance():
            rows, cut = _finance_rows(reads, ptype, party)
            money += tagged(rows, party)
            partial["money"] |= cut
        if not blocked:
            notes += tagged(_last_notes(reads, ptype, party, now, exclude, last), party)
    inbox = alerts_read(alerts, reads)
    gated = gate_alerts(visible_alerts(alerts), decide)
    if gated is None:
        inbox, gated = alerts_read(None, reads), []
    seen = [g for g in gated if g.get("state") != "resolved"
            and (reads.finance() or g["kind"] not in FINANCE_KINDS)
            and not (blocked and g["kind"] in PEOPLE_KINDS)]
    mine = [g for g in seen if _cite_objects(g) & related] if parties else []
    projects = _rd_rows(seen, parties)
    promises = [g for g in mine if g["kind"] in PROMISE_KINDS]
    quality = [g for g in mine if g["kind"] in QUALITY_KINDS]
    other = [g for g in mine if g["kind"] not in PROMISE_KINDS | QUALITY_KINDS]
    actions = _alert_rows(g for g in seen if _cite_objects(g) & last) if last else []
    for row in actions:
        row["label"] = f"Action item: {row['label']}"[:MAX_TEXT]
    over = reads.cited.over() is not None
    no_party = None if parties else ("No customer or supplier is linked to this meeting "
                                     "for you; nothing below is about a party.")
    sections = [
        _section("orders", "Open orders and purchase orders", orders,
                 partial=partial["orders"] or over, note=no_party),
        _alert_section("alerts", "Late items and alerts", other, inbox),
        (_withheld("threads", "Recent mail and Teams threads") if blocked else
         _section("threads", "Recent mail and Teams threads", threads,
                  partial=partial["threads"] or over,
                  note=f"Subjects, who and when only, the last {THREAD_DAYS} days.")),
        (_section("money", "Payments and balance", money, partial=partial["money"] or over)
         if reads.finance() else _finance_marked("money", "Payments and balance")),
        (_withheld("promises", "Open promises") if blocked else
         _alert_section("promises", "Open promises", promises, inbox)),
        _alert_section("quality", "Quality issues", quality, inbox),
        (_withheld("rd", "R&D and sample projects") if blocked else
         _section("rd", "R&D and sample projects", projects, state=None if inbox else "unread",
                  note=RD_NOTE if inbox else UNREAD_NOTE)),
        (_withheld("notes", "Last meeting: notes and action items") if blocked else
         _section("notes", "Last meeting: notes and action items", notes + actions,
                  note=None if notes or actions else
                  "No earlier meeting with a captured transcript or action item.")),
    ]
    # a standing internal review (no outside party, a title naming a review): its pack
    reviews = review_families(title) if otype in MEETING_TYPES and not parties else []
    if reviews:
        sections = review_sections(reviews, seen, inbox) + sections
    sections = finish(sections, decide, reads)
    sections.insert(0, talking_points(sections))
    agenda = review_agenda(sections)
    if agenda is not None:
        sections.insert(1, agenda)
    return {"schema": MEETING_SCHEMA, "kind": "meeting", "title": title,
            "review": [key for key, _, _ in reviews],
            "subject": {"object_id": object_id, "type": otype, "label": title},
            "subject_sources": _source(title_handle), "when": when,
            "as_of": now.isoformat(timespec="seconds"), "parties": party_refs,
            "possible_parties": possible_refs, "sections": sections,
            "gaps": reads.gaps(), "budget": reads.cited.budget(),
            "basis": ("Read from the ontology as you may read it, no model call: parties from "
                      "the meeting's attendee links (lead-tier matches listed apart, never "
                      "read), alerts from your alert list, open meaning not closed by the "
                      "record's own cited status.")}


# Standing internal reviews (a recurring meeting with no outside party): the meeting's
# title words name the review, and the review's pack is the reader's own alerts of the
# kinds that review discusses, built live (no agenda is kept anywhere). Vocabulary only:
# no entry names a meeting, a record or a person.
import re as _re
from operating import declared_schema as _schemas
from operating.declared_schema import briefs as _BRIEFS
REVIEW_FAMILIES: tuple[tuple[str, str, Any, frozenset[str]], ...] = (
    ("incoming", "Incoming deliveries and ETAs", _re.compile(
        r"\b(?:incoming|inbound)\b|\betas?\b|\bcontainers?\b|\breceiving\b", _re.I),
     frozenset({"late_po", "unconfirmed_po", "undated_po", "eta_slipped", "vendor_no_reply",
                "supplier_reliability", "po_receipt_quantity_mismatch"})),
    ("incomplete", "Incomplete records and orders", _re.compile(
        r"\bincomplete\b|\bmissing\b|\bopen\s+items?\b", _re.I),
     frozenset({"key_field_missing", "order_without_customer_po", "shipped_not_invoiced",
                "late_order", "late_customer_order", "conflicting_quantity",
                "twins_disagree", "duplicate_customer_order", "sync_missing",
                "sync_mismatch"})),
    ("production", "Production, schedule and KPIs", _re.compile(
        r"\bproduction\b|\bkpis?\b|\bschedul\w*|\bhuddle\b|\bprogress\b|\bmrp\b",
        _re.I),
     frozenset({"production_late", "assembly_damage_spike", "assembly_blocked_by_component",
                "recurring_component_damage", "stock_out_risk", "promise_date_slip"})),
    ("warehouse", "Warehouse and shipping", _re.compile(
        r"\bwarehouse\b|\bshipping\b|\bwms\b|\blogistics\b", _re.I),
     frozenset({"late_customer_order", "shipped_not_invoiced", "customer_chasing_order",
                "tell_before_they_ask", "expiring_lot_stock", "freight_cost_anomaly"})),
    ("inventory", "Inventory", _re.compile(r"\binventory\b|\bstock\b|\bcounts?\b", _re.I),
     frozenset({"slow_moving_stock", "expiring_lot_stock", "stock_out_risk",
                "assembly_blocked_by_component", "lot_near_expiry"})),
    ("costs", "Costs and margins", _re.compile(
        r"\bstandard\s+costs?\b|\bcost(?:s|ing)\b|\bmargins?\b", _re.I),
     frozenset({"unit_cost_jump", "negative_margin", "margin_erosion", "price_below_cost",
                "price_below_typical"})),
    ("quality", "Quality and inspection", _re.compile(
        r"\binspection\b|\bquality\b|\bqc\b|\bqa\b|\bcalibration\b", _re.I),
     frozenset({"receipt_without_qc", "quality_hold_aging", "reject_trend", "lot_on_hold",
                "certificate_expiring", "lot_near_expiry"})),
    ("launch", "Systems launch readiness", _re.compile(
        r"\blaunch\b|\breadiness\b|\bgo[- ]live\b|\buat\b|\berp\b", _re.I),
     frozenset({"mc_task_overdue", "mc_task_stuck", "mc_approval_pending",
                "planner_task_overdue", "source_stale", "twins_disagree",
                "sync_missing", "sync_mismatch"})),
)
# Every standing review also sees what waits on someone of ours.
WAITING_KINDS = frozenset({"approval_waiting", "chase_escalating", "teams_unanswered_ask",
                           "party_chase_unanswered", "internal_order_chase"})
MAX_REVIEWS = 3


def review_families(title: Any) -> list[tuple[str, str, frozenset[str]]]:
    """The standing reviews a meeting title names (at most ``MAX_REVIEWS``)."""
    text = title if isinstance(title, str) else ""
    return [(key, label, kinds) for key, label, pattern, kinds in REVIEW_FAMILIES
            if pattern.search(text)][:MAX_REVIEWS]


def review_sections(families: Sequence[tuple[str, str, frozenset[str]]],
                    seen: Sequence[Mapping[str, Any]], read: bool) -> list[dict[str, Any]]:
    out = []
    for key, label, kinds in families:
        out.append(_alert_section(f"review_{key}", f"Review: {label}",
                                  [g for g in seen if g.get("kind") in kinds], read))
    if families:
        out.append(_alert_section("review_waiting", "Review: waiting on someone",
                                  [g for g in seen if g.get("kind") in WAITING_KINDS], read))
    return out


def review_agenda(sections: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The agenda of a standing review, from its finished sections (rows the reader may
    open only: each item counts what the section shows plus its decided "more")."""
    parts = [s for s in sections if str(s.get("key", "")).startswith("review_")]
    if not parts:
        return None
    rows = []
    for number, s in enumerate((s for s in parts if s.get("rows")), start=1):
        count = len(s["rows"]) + int(s.get("more") or 0)
        sources = [src for r in s["rows"][:2] for src in r.get("sources") or ()][:MAX_SOURCES]
        rows.append({"label": f"{number}. {s['title'].removeprefix('Review: ')}",
                     "detail": f"{count} open item{'s' if count != 1 else ''}; first: "
                               f"{s['rows'][0]['label']}"[:MAX_TEXT],
                     "date": None, "who": None, "amount": None, "object_id": None,
                     "sources": sources})
    return {"key": "agenda", "title": "Agenda (built from your open alerts)",
            "state": "shown" if rows else "empty",
            "note": None if rows else "Nothing open for this review that you may see.",
            "rows": rows, "more": 0, "figures": []}


POINT_SOURCES = (("orders", "late"), ("promises", None), ("alerts", None), ("money", "overdue"),
                 ("quality", None), ("notes", "action"), ("threads", None))


def talking_points(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """Up to ``MAX_POINTS`` suggested talking points, each drawn from one line the brief
    already shows the reader (after its item decision), keeping that line's citations and
    record: late orders first, then promises, alerts, overdue balances, quality, the last
    meeting's action items and the newest thread. No model call, no new fact."""
    by_key = {s["key"]: s for s in sections}
    points: list[dict[str, Any]] = []
    for key, only in POINT_SOURCES:
        section = by_key.get(key)
        if section is None or section["state"] not in ("shown", "partial"):
            continue
        for row in section["rows"]:
            if len(points) >= MAX_POINTS:
                break
            detail = row.get("detail") or ""
            if only == "late" and "days past" not in detail:
                continue
            if only == "overdue" and row["label"] != "Overdue":
                continue
            if only == "action" and not row["label"].startswith("Action item"):
                continue
            words = {"orders": f"Agree a new date for {row['label']} ({detail})",
                     "promises": f"Where our promise stands: {row['label']}",
                     "alerts": f"Raise: {row['label']}",
                     "money": "Ask about the overdue balance",
                     "quality": f"Quality: {row['label']}",
                     "notes": f"Follow up on last meeting's {row['label'].lower()}",
                     "threads": f"Pick up the latest thread: {row['label']}"}[key]
            points.append(dict(row, label=words[:MAX_TEXT], who=None))
            if key in ("money", "threads"):
                break   # one point each
    return _section_done("talking_points", "Suggested talking points", points,
                         note="Drawn from the lines below, each with its citations; no model "
                              "call.")


def _section_done(key: str, title: str, rows: list[dict[str, Any]], *,
                  note: str | None = None) -> dict[str, Any]:
    return {"key": key, "title": title, "state": "shown" if rows else "empty", "note": note,
            "rows": rows[:MAX_ROWS], "more": 0, "figures": []}


UPCOMING_PROPS = ("subject", "start", "end", "is_cancelled", "is_all_day", "location",
                  "body_preview", "content", "organizer", "attendees", "is_online_meeting",
                  "online_meeting", "is_organizer", "mailbox")
MAX_AGENDA = 600          # agenda words shown per meeting (the capture keeps 2,000 at most)
MAX_ATTENDEES = 40
JOIN_PREFIX = "https://teams.microsoft.com/"


def _json_cell(value: Any) -> Any:
    """A json property's value as data: the store hands it back as data or as its text."""
    if isinstance(value, (list, Mapping)) and not (isinstance(value, Mapping)
                                                   and "untrusted_text" in value):
        return value
    words = text_of(value)
    if not isinstance(words, str) or not words.strip():
        return None
    try:
        return json.loads(words)
    except ValueError:
        return None


def _invitee(raw: Any, role: str) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    name, address = _clip(raw.get("name"), 120), _clip(raw.get("address"), 254)
    if not name and not address:
        return None
    kind = (_clip(raw.get("type"), 32) or "").lower()
    response = (_clip(raw.get("response"), 32) or "").lower()
    return {"name": name or address, "address": address,
            "role": role if role == "organizer" else (kind if kind in (
                "required", "optional", "resource") else "required"),
            "response": response if response in ("accepted", "declined",
                                                  "tentativelyaccepted", "none",
                                                  "notresponded", "organizer") else None}


def _invitees(reads: "Reads", record: Any, people: Any) -> tuple[bool, dict | None, list]:
    """(keep the event, organizer, attendees) as this reader may see them. An address the
    people exclusion names, or one it cannot decide, drops the whole event (as the capture
    does); a mailbox's own domain marks who is inside the company."""
    org_raw, o_handle = reads.cell(record, "organizer")
    att_raw, a_handle = reads.cell(record, "attendees")
    mailbox, _ = reads.cell(record, "mailbox")
    organizer = _invitee(_json_cell(org_raw), "organizer")
    listed = _json_cell(att_raw)
    attendees = [a for a in (_invitee(x, "attendee") for x in (
        listed if isinstance(listed, list) else ())[:MAX_ATTENDEES]) if a]
    home = str(text_of(mailbox) or "").rpartition("@")[2].lower()
    for person in [organizer, *attendees]:
        if person is None or not person["address"]:
            continue
        try:
            if people.is_excluded(person["address"]):
                return False, None, []
        except Exception:  # noqa: BLE001 - undecidable: never shown
            return False, None, []
        domain = person["address"].rpartition("@")[2].lower()
        person["outside"] = bool(home and domain and domain != home)
    org_address = (organizer or {}).get("address")
    attendees = [a for a in attendees if not org_address or a["address"] != org_address]
    if organizer is not None:
        organizer["sources"] = _source(o_handle)
    for person in attendees:
        person["sources"] = _source(a_handle)
    return True, organizer, attendees


def _join_url(raw: Any) -> str | None:
    online = _json_cell(raw)
    url = online.get("join_url") if isinstance(online, Mapping) else None
    return url if isinstance(url, str) and url.startswith(JOIN_PREFIX) and len(url) <= 4096 \
        else None


def upcoming_meetings(call: Call, held: Iterable[str], *, now: datetime | None = None,
                      days: int = UPCOMING_DAYS, limit: int = 60, people: Any = None,
                      since: datetime | None = None) -> list[dict[str, Any]]:
    """Calendar events from ``since`` (default: now) to ``days`` days ahead this reader may
    read, soonest first: subject, start and end, where (room or Teams), the organizer and
    attendees with their invite role and response, the agenda words, and the customers and
    suppliers the event is linked to, each cited.

    People fail closed: with the exclusion unavailable no organizer, attendee or agenda is
    shown (``people_withheld``); an event naming an excluded or undecidable address is not
    listed at all. A private or confidential event shows its time only."""
    now = now or datetime.now(timezone.utc)
    since = since or now
    reads = Reads(call, held, budget_seconds=BUDGET_SECONDS, declared=None)
    end = now + timedelta(days=days)
    blocked = people_unavailable(people)
    ids = reads.cited.find("CalendarEvent", [
        {"property": "start", "op": "gte", "value": since.strftime("%Y-%m-%dT%H:%M:%S")},
        {"property": "start", "op": "lt", "value": end.strftime("%Y-%m-%dT%H:%M:%S")}],
        empty_gap=False) or []
    out = []
    for oid in ids[:MAX_READS]:
        record = reads.read(oid, UPCOMING_PROPS)
        start, s_handle = reads.cell(record, "start")
        cancelled, _ = reads.cell(record, "is_cancelled")
        if start is None or str(text_of(cancelled)).lower() == "true":
            continue
        organizer, attendees = None, []
        if not blocked:
            keep, organizer, attendees = _invitees(reads, record, people)
            if not keep:
                continue
        subject, _ = reads.cell(record, "subject")
        finish, _ = reads.cell(record, "end")
        content, _ = reads.cell(record, "content")
        private = str(text_of(content) or "") == "withheld_sensitivity"
        location, l_handle = reads.cell(record, "location")
        agenda, g_handle = reads.cell(record, "body_preview")
        online, _ = reads.cell(record, "is_online_meeting")
        join, _ = reads.cell(record, "online_meeting")
        all_day, _ = reads.cell(record, "is_all_day")
        mine, _ = reads.cell(record, "is_organizer")
        parties = []
        if not private:
            for link in PARTY_LINKS["CalendarEvent"]:
                for party in reads.linked(oid, link, "forward")[:MAX_PARTIES]:
                    if type_of(party) in PARTY_TYPES and party not in {
                            p["object_id"] for p in parties}:
                        parties.append({"object_id": party, "type": type_of(party),
                                        "label": reads.label(party)[0]})
        show_people = not blocked and not private
        out.append({
            "object_id": oid, "label": _clip(subject) or "(no subject)",
            "start": text_of(start), "end": text_of(finish),
            "all_day": str(text_of(all_day)).lower() == "true",
            "location": None if private else _clip(location, 200),
            "online": str(text_of(online)).lower() == "true",
            "join_url": None if private else _join_url(join),
            "agenda": _clip(agenda, MAX_AGENDA) if show_people else None,
            "organizer": organizer if show_people else None,
            "attendees": attendees if show_people else [],
            "is_organizer": str(text_of(mine)).lower() == "true",
            "private": private, "people_withheld": bool(blocked),
            "parties": parties,
            "sources": _dedupe(_source(s_handle) + (
                _source(l_handle) if location is not None and not private else []) + (
                _source(g_handle) if agenda is not None and show_people else []))})
    out.sort(key=lambda e: e["start"] or "")
    return out[:limit]


def _dedupe(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    seen, out = set(), []
    for s in sources:
        if s["handle"] not in seen:
            seen.add(s["handle"])
            out.append(s)
    return out[:MAX_SOURCES]


# ------------------------------------------------------------------------- weekly review

def week_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _aggregate(reads: Reads, object_type: str, prop: str, start: date, end: date, *,
               measure: Mapping[str, Any], group_by: Sequence[str] = ()
               ) -> tuple[list[Mapping[str, Any]], bool] | None:
    """The groups of one ``plx.aggregate`` over ``object_type`` with ``prop`` in
    [start, end), each group kept only when the reader holds its markings; None when the
    read failed (its gap recorded)."""
    if reads.cited.over():
        reads.cited.budget_gap(object_type, "the weekly figure was not computed")
        return None
    args: dict[str, Any] = {"object_type": object_type, "measure": dict(measure), "filters": [
        {"property": prop, "op": "gte", "value": start.isoformat()},
        {"property": prop, "op": "lt", "value": end.isoformat()}]}
    if group_by:
        args["group_by"] = list(group_by)
    out = reads.call("plx.aggregate", args)
    if _failed(reads, object_type, out):
        return None
    for g in out.get("gaps") or ():
        # the aggregate's own gaps (twins in two systems, a date not captured, ...), never
        # one about what was withheld
        if isinstance(g, Mapping) and g.get("state") != "inaccessible":
            reads.cited.add({"code": str(g.get("code") or g.get("reason") or "aggregate_gap"),
                             "state": str(g.get("state") or "unknown"),
                             "subject": object_type,
                             "detail": str(g.get("detail") or g.get("missing") or "")[:600]})
    every = [g for g in out.get("groups") or () if isinstance(g, Mapping)]
    groups = [g for g in every if held_all(g.get("markings"), reads.held)]
    # a group dropped for its markings makes the figure partial (never how many)
    partial = len(groups) < len(every) or bool(out.get("partial")) or bool((out.get("completeness") or {}).get(
        "next_cursor"))
    return groups, partial


def _failed(reads: Reads, subject: str, out: Any) -> bool:
    """Whether a tool answer is an error or a refusal (its gap recorded)."""
    if not isinstance(out, Mapping):
        reads.cited._error(subject, {"code": "malformed_result"})
        return True
    problem = out.get("error") or out.get("refused")
    if problem:
        reads.cited._error(subject, problem if isinstance(problem, Mapping) else {})
        return True
    return False


def _count(reads: Reads, object_type: str, prop: str, start: date, end: date
           ) -> tuple[int, list[str], bool] | None:
    found = _aggregate(reads, object_type, prop, start, end, measure={"op": "count"})
    if found is None:
        return None
    groups, partial = found
    total, cited = 0, []
    for g in groups:
        value = _decimal(g.get("value"))
        if value is None:
            return None   # a group whose count is unknown: the total is unknown, never 0
        total += int(value)
        cited += [h for h in g.get("members_cited") or () if isinstance(h, str)]
    return total, cited, partial


def _sums(reads: Reads, object_type: str, prop: str, measure: str, start: date, end: date
          ) -> dict[str, tuple[Decimal, list[str]]] | None:
    found = _aggregate(reads, object_type, prop, start, end,
                       measure={"op": "sum", "property": measure}, group_by=("currency",))
    if found is None:
        return None
    out: dict[str, tuple[Decimal, list[str]]] = {}
    for g in found[0]:
        currency = _clip((g.get("key") or {}).get("currency"), 3) or g.get("currency")
        value = _decimal(g.get("value"))
        if not isinstance(currency, str) or len(currency) != 3:
            continue   # an amount without its currency is never stated
        if value is None:
            return None   # one currency's total unknown: no invoiced figure is stated
        handles = [c.get("l1_handle") for c in g.get("inputs_citations") or ()
                   if isinstance(c, Mapping) and isinstance(c.get("l1_handle"), str)]
        prev = out.get(currency.upper(), (Decimal(0), []))
        out[currency.upper()] = (prev[0] + value, prev[1] + handles)
    return out


def _figure(label: str, value: str | None, *, unit: str, previous: str | None = None,
            delta: str | None = None, currency: str | None = None,
            sources: Iterable[str] = (), note: str | None = None) -> dict[str, Any]:
    handles = list(dict.fromkeys(h for h in sources if isinstance(h, str)))
    return {"label": label, "value": value, "unit": unit, "currency": currency,
            "previous": previous, "delta": delta, "note": note,
            "sources": [{"handle": h} for h in handles[:MAX_SOURCES]]}


def _delta(now: Decimal | int | None, before: Decimal | int | None) -> str | None:
    if now is None or before is None:
        return None
    change = Decimal(now) - Decimal(before)
    return ("+" if change > 0 else "") + format(change.normalize() if change % 1 else
                                                change.quantize(Decimal(1)), "f")


def _on_time(reads: Reads, start: date, end: date) -> dict[str, Any]:
    """Shipments dated in [start, end) against their order's required date (the reader's
    reads only): on time, late, and those whose order or date it cannot read (``undated``,
    never counted either way)."""
    ids = reads.cited.find("Shipment", [
        {"property": "ship_date", "op": "gte", "value": start.isoformat()},
        {"property": "ship_date", "op": "lt", "value": end.isoformat()}],
        empty_gap=False)
    if ids is None:
        return {"on_time": 0, "late": 0, "undated": 0, "partial": True, "sources": [],
                "late_sources": [], "failed": True}
    on, late, undated, sources, late_sources = 0, 0, 0, [], []
    partial = len(ids) > MAX_READS
    for oid in ids[:MAX_READS]:
        record = reads.read(oid, ("ship_date",))
        shipped, s_handle = reads.cell(record, "ship_date")
        orders = reads.linked(oid, "Shipment.ships", "forward") if record is not None else []
        due, d_handle = None, None
        for order in orders[:1]:
            due, d_handle = reads.cell(reads.read(order, ("required_date",)), "required_date")
        ship_day, due_day = _day(shipped), _day(due)
        if reads.cited.over():
            partial = True
        if ship_day is None or due_day is None:
            undated += 1
            continue
        if ship_day <= due_day:
            on += 1
        else:
            late += 1
            late_sources += [s_handle, d_handle]
        sources += [s_handle, d_handle]
    return {"on_time": on, "late": late, "undated": undated, "partial": partial,
            "sources": [h for h in sources if h], "late_sources": [h for h in late_sources if h]}


def _in_week(value: Any, start: date, end: date) -> bool:
    day = _day(value)
    return day is not None and start <= day < end


def _alert_flow(alerts: Mapping[str, Any] | None, every: list[Mapping[str, Any]],
                start: date, end: date) -> list[dict[str, Any]]:
    """Alerts opened (first seen) and closed (resolved, last seen) in the week, counted over
    the reader's alert list only. An open-only list, or one cut at its limit, states no
    closed count / an at-least count; nothing hidden is counted."""
    cut = bool((alerts or {}).get("truncated"))
    opened = [g for g in every if _in_week(g.get("first_seen"), start, end)]
    note = "at least this many (your alert list was cut at its limit)" if cut else None
    out = [_figure("Alerts opened", str(len(opened)), unit="records", note=note,
                   sources=[h for g in opened for h in _cite_handles(g)[:1]])]
    if (alerts or {}).get("with_resolved"):
        closed = [g for g in every if g.get("state") == "resolved"
                  and _in_week(g.get("last_seen"), start, end)]
        out.append(_figure("Alerts closed", str(len(closed)), unit="records", note=note,
                           sources=[h for g in closed for h in _cite_handles(g)[:1]]))
    else:
        out.append(_figure("Alerts closed", None, unit="records",
                           note="could not be read: the alert list was read without its "
                                "resolved alerts"))
    return out


def _pct(part: int, whole: int) -> Decimal | None:
    if whole <= 0:
        return None
    return (Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"))


def _cash(reads: Reads, today: date) -> list[dict[str, Any]]:
    figures = []
    for otype, label in (("AgedReceivable", "Owed to us (aged receivables)"),
                         ("AgedPayable", "We owe (aged payables)")):
        if reads.cited.over():
            break
        out = reads.call("plx.aggregate", {"object_type": otype, "group_by": ["currency"],
                                           "measure": {"op": "sum", "property": "balance_due"}})
        if _failed(reads, otype, out):
            continue
        for g in out.get("groups") or ():
            if not isinstance(g, Mapping) or not held_all(g.get("markings"), reads.held):
                continue
            currency = _clip((g.get("key") or {}).get("currency"), 3) or g.get("currency")
            value = _decimal(g.get("value"))
            if isinstance(currency, str) and len(currency) == 3 and value is not None:
                figures.append(_figure(label, _money(value), unit="money",
                                       currency=currency.upper(), sources=[
                                           c.get("l1_handle") for c in
                                           g.get("inputs_citations") or ()
                                           if isinstance(c, Mapping)]))
    if not reads.cited.over():
        out = reads.call("plx.aggregate", {"mode": "cash_forecast", "measure": {"op": "sum"}})
        if not _failed(reads, "cash forecast", out):
            cites = [h for e in out.get("largest") or () if isinstance(e, Mapping)
                     for h in (e.get("citations") or ())[:1] if isinstance(h, str)]
            for row in out.get("totals") or ():
                net = _decimal(row.get("net")) if isinstance(row, Mapping) else None
                currency = row.get("currency") if isinstance(row, Mapping) else None
                if net is not None and isinstance(currency, str) and len(currency) == 3:
                    figures.append(_figure("Expected net cash, next 13 weeks", _money(net),
                                           unit="money", currency=currency, sources=cites,
                                           note="13-week cash forecast"))
    return figures


def weekly_review(call: Call, held: Iterable[str], alerts: Mapping[str, Any] | None, *,
                  week: date | None = None, today: date | None = None,
                  budget_seconds: float = BUDGET_SECONDS,
                  clock: Callable[[], float] | None = None,
                  decide: Decide | None = None, people: Any = None) -> dict[str, Any]:
    """The weekly review for the week starting ``week`` (default: the last full week) and
    its change from the week before, as this reader reads it (module doc). ``alerts``:
    the reader's alert list, read with its resolved alerts for the opened and closed
    counts (an open-only list states no closed count)."""
    today = today or datetime.now(timezone.utc).date()
    start = week_of(week) if week else week_of(today) - timedelta(days=7)
    if start > today:
        raise BriefRefused("future_week")
    end, before = start + timedelta(days=7), start - timedelta(days=7)
    reads = Reads(call, held, budget_seconds=budget_seconds, clock=clock, declared=None)
    reads.today = today
    blocked = people_unavailable(people)
    if blocked:
        _people_withheld(reads, blocked)
    figures: list[dict[str, Any]] = []
    partial = False
    for label, otype, prop in (("Orders booked", "SalesOrder", "order_date"),
                               ("Orders shipped", "Shipment", "ship_date"),
                               ("Invoices issued", "SalesInvoice", "invoice_date")):
        now_n, last_n = _count(reads, otype, prop, start, end), \
            _count(reads, otype, prop, before, start)
        partial |= bool((now_n and now_n[2]) or (last_n and last_n[2]))
        figures.append(_figure(label, str(now_n[0]) if now_n else None, unit="records",
                               previous=str(last_n[0]) if last_n else None,
                               delta=_delta(now_n[0] if now_n else None,
                                            last_n[0] if last_n else None),
                               sources=now_n[1] if now_n else (),
                               note=("could not be read" if not now_n else
                                     "at least this many (a partial count)" if now_n[2]
                                     else None)))
    if reads.finance():
        this, last = (_sums(reads, "SalesInvoice", "invoice_date", "total_amount", start, end),
                      _sums(reads, "SalesInvoice", "invoice_date", "total_amount", before, start))
        for currency in sorted(set(this or {}) | set(last or {})):
            a, b = (this or {}).get(currency), (last or {}).get(currency)
            figures.append(_figure(f"Invoiced ({currency})", _money(a[0]) if a else
                                   ("0.00" if this is not None else None), unit="money",
                                   currency=currency,
                                   previous=_money(b[0]) if b else
                                   ("0.00" if last is not None else None),
                                   delta=_delta(a[0] if a else Decimal(0) if this is not None
                                                else None,
                                                b[0] if b else Decimal(0) if last is not None
                                                else None),
                                   sources=a[1] if a else ()))
    ot_now, ot_last = _on_time(reads, start, end), _on_time(reads, before, start)
    pct_now = _pct(ot_now["on_time"], ot_now["on_time"] + ot_now["late"])
    pct_last = _pct(ot_last["on_time"], ot_last["on_time"] + ot_last["late"])
    figures.append(_figure(
        "Shipped on time", str(pct_now) if pct_now is not None else None, unit="percent",
        previous=str(pct_last) if pct_last is not None else None,
        delta=_delta(pct_now, pct_last), sources=ot_now["sources"],
        note=("could not be read" if ot_now.get("failed") else
              f"{ot_now['on_time']} of {ot_now['on_time'] + ot_now['late']} shipments by "
              f"their order's required date" + (f"; {ot_now['undated']} without a readable "
                                                 "required date not counted"
                                                 if ot_now["undated"] else "")
              + ("; a sample of the week's shipments" if ot_now["partial"] else ""))))
    if not ot_now.get("failed"):
        figures.append(_figure("Shipped late", str(ot_now["late"]), unit="records",
                               previous=None if ot_last.get("failed") else str(ot_last["late"]),
                               delta=None if ot_last.get("failed") else
                               _delta(ot_now["late"], ot_last["late"]),
                               sources=ot_now["late_sources"],
                               note="shipped after their order's required date"
                                    + ("; a sample of the week's shipments"
                                       if ot_now["partial"] else "")))
    partial |= ot_now["partial"] or ot_last["partial"]
    inbox = alerts_read(alerts, reads)
    gated = gate_alerts(visible_alerts(alerts), decide)
    if gated is None:
        inbox, gated = alerts_read(None, reads), []
    every = [g for g in gated if (reads.finance() or g["kind"] not in FINANCE_KINDS)
             and not (blocked and g["kind"] in PEOPLE_KINDS)]
    seen = [g for g in every if g.get("state") != "resolved"]
    late_pos = [g for g in seen if g["kind"] in LATE_PO_KINDS]
    quality = [g for g in seen if g["kind"] in QUALITY_KINDS]
    margin = [g for g in seen if g["kind"] in MARGIN_KINDS]
    top = list(seen)
    slips = [g for g in seen if g["kind"] in SLIP_KINDS]
    aging = [g for g in seen if g["kind"] in AGING_KINDS]
    suppliers = [g for g in seen if g["kind"] in SUPPLIER_KINDS]
    missed = [g for g in every if g["kind"] == "commitment_missed"
              and _in_week(g.get("first_seen"), start, end)
              or g["kind"] == "commitment_missed" and g.get("state") != "resolved"]
    asks = [g for g in seen if g["kind"] in ASK_KINDS]
    flow = _alert_flow(alerts, every, start, end) if inbox else []
    over = reads.cited.over() is not None
    sections = [
        _section("figures", "This week", [], figures=figures, partial=partial or over,
                 note=f"Week of {start.isoformat()} to {(end - timedelta(days=1)).isoformat()}"
                      f", compared with the week of {before.isoformat()}."),
        _alert_section("late_pos", "Late purchase orders (open now)", late_pos, inbox,
                       figures=[_figure("Late purchase orders", str(len(late_pos)),
                                        unit="records")] if late_pos else []),
        _alert_section("slips", "Top slips and their reasons", slips, inbox, limit=TOP_ALERTS,
                       note="Each reason is the alert's own cited summary."),
        _alert_section("alert_flow", "Alerts opened and closed", [
            g for g in every if _in_week(g.get("first_seen"), start, end)], inbox,
            limit=TOP_ALERTS, figures=flow,
            note="Counted over your alert list; the rows are alerts first seen this week."),
        _alert_section("top_alerts", "Top alerts", top, inbox, limit=TOP_ALERTS),
        (_alert_section("cash", "Cash, AR and AP aging", aging, inbox,
                        figures=_cash(reads, today),
                        note="Aged balances as captured now, and the aging alerts open now.")
         if reads.finance() else _finance_marked("cash", "Cash, AR and AP aging")),
        (_alert_section("margin", "Margin movers", margin, inbox,
                        note=("Margin signals from the margin and price alerts open now; a "
                              "per-week margin figure needs a per-week cost basis the store "
                              "does not hold yet."))
         if reads.finance() else _finance_marked("margin", "Margin movers")),
        _alert_section("suppliers", "Supplier reliability", suppliers, inbox),
        (_withheld("commitments_missed", "Promises missed") if blocked else
         _alert_section("commitments_missed", "Promises missed", missed, inbox)),
        (_withheld("asks", "Top customer asks") if blocked else
         _alert_section("asks", "Top customer asks", asks, inbox, limit=TOP_ALERTS)),
        _alert_section("quality", "Quality holds", quality, inbox),
    ]
    sections = finish(sections, decide, reads)
    return {"schema": WEEKLY_SCHEMA, "kind": "weekly",
            "title": f"Weekly review, week of {start.isoformat()}",
            "subject": None, "subject_sources": [], "when": start.isoformat(),
            "as_of": today.isoformat(), "parties": [], "possible_parties": [],
            "sections": sections, "gaps": reads.gaps(), "budget": reads.cited.budget(),
            "basis": ("Counted from the records you may read, no model call; alerts are those "
                      "open now on your alert list; money per currency, never converted.")}


# ------------------------------------------------------------------------- weekly archive

class WeeklyArchive:
    """Each reader's finished weekly reviews, kept so a past week reads as it was generated
    (a SQLite file in the app's own data directory, owner-only). A week is kept per reader
    and per grant (``grant``: the reader's held markings and policy state); a kept week is
    served only after every source it shows is decided for the reader again (``decide``),
    so a narrowed grant or a revoked item regenerates it. A week not finished yet is never
    kept. Any failure of the file is a miss, never a failed read."""

    def __init__(self, path: Any) -> None:
        from pathlib import Path
        self.path = Path(path)

    def _conn(self) -> Any:
        import os
        import sqlite3
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(self.path, timeout=5)
        os.chmod(self.path, 0o600)
        _schemas.apply(conn, _BRIEFS)
        return conn

    @staticmethod
    def handles(raw: Mapping[str, Any]) -> list[str]:
        out = [h["handle"] for h in raw.get("subject_sources") or () if isinstance(h, Mapping)]
        for sec in raw.get("sections") or ():
            for item in list(sec.get("rows") or ()) + list(sec.get("figures") or ()):
                out += _handles_of(item)
        return sorted(set(h for h in out if isinstance(h, str)))

    def get(self, reader: str, week: date, grant: str, decide: Decide | None
            ) -> dict[str, Any] | None:
        try:
            conn = self._conn()
            try:
                row = conn.execute("SELECT grant_key, stored_at, body FROM weeks WHERE "
                                   "reader = ? AND week = ?", (reader, week.isoformat())
                                   ).fetchone()
            finally:
                conn.close()
            if row is None or row[0] != grant:
                return None
            raw = json.loads(row[2])
            handles = self.handles(raw)
            if handles:
                if decide is None:
                    return None
                decided = decide(handles)
                if any(h not in decided or decided[h] is not None for h in handles):
                    return None
            return dict(raw, stored_at=row[1])
        except Exception:  # noqa: BLE001 - a miss
            return None

    def put(self, reader: str, week: date, grant: str, raw: Mapping[str, Any],
            today: date) -> bool:
        if week + timedelta(days=7) > today or (raw.get("budget") or {}).get("stopped"):
            return False   # an unfinished week or a budget-cut page is never kept
        try:
            conn = self._conn()
            try:
                with conn:
                    conn.execute("INSERT OR REPLACE INTO weeks VALUES (?, ?, ?, ?, ?)",
                                 (reader, week.isoformat(), grant,
                                  datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  json.dumps(raw, default=str)))
            finally:
                conn.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    def weeks(self, reader: str, grant: str) -> list[str]:
        try:
            conn = self._conn()
            try:
                return [r[0] for r in conn.execute(
                    "SELECT week FROM weeks WHERE reader = ? AND grant_key = ? ORDER BY week "
                    "DESC LIMIT 104", (reader, grant))]
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return []


# ------------------------------------------------------------------------------ QC packs

QC_DAILY_SCHEMA = "plx-qc-daily/v1"
QC_AUDIT_SCHEMA = "plx-qc-audit/v1"
QUALITY = "quality_controlled"
QC_MODE = "qc_compliance"
QC_ROWS = 40             # rows an audit or daily pack section lists (one printed page)
QC_HOLD_KINDS = frozenset({"quality_hold_aging", "lot_on_hold"})
STATUS_WORDS = {"missed": "missed its cycle", "overdue": "overdue",
                "upcoming": "due soon", "late": "done late"}
QC_BASIS = ("Counted from the quality records you may read, no model call: Planner tasks of "
            "quality plans (or titled calibration, swab, sanitation, pest control) and "
            "FileMaker production schedule slots. A check done after its due day but before "
            "the next one fell due is late; after that, or still open then, it missed its "
            "cycle.")


def _quality_marked(keys: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    return [{"key": k, "title": t, "state": "quality_marked", "rows": [], "more": 0,
             "figures": [], "note": "Quality records: shown only to readers who hold "
                                    "quality_controlled."} for k, t in keys]


def _qc_read(reads: Reads, view: str) -> Mapping[str, Any] | None:
    out = reads.call("plx.aggregate", {"mode": QC_MODE, "view": view,
                                       "measure": {"op": "count"}, "object_type": "PlannerTask"})
    return None if _failed(reads, f"QC compliance ({view})", out) else out


def _text_of(cell: Any) -> tuple[str | None, str | None]:
    """The words of an untrusted text envelope (clipped) and its citation."""
    if not isinstance(cell, Mapping):
        return None, None
    origin = cell.get("origin") or {}
    return _clip(cell.get("untrusted_text"), 120), (origin.get("citation") if isinstance(
        origin.get("citation"), str) else None)


def _counts_detail(row: Mapping[str, Any]) -> str:
    share = row.get("on_time_share")
    return (f"{row.get('judged', 0)} due: {row.get('on_time', 0)} on time, "
            f"{row.get('late', 0)} late, {row.get('missed', 0)} missed, "
            f"{row.get('overdue', 0)} overdue" + (f" ({share}% on time)" if share else ""))


def _area_figures(checks: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in [*(checks.get("by_area") or ()), {**(checks.get("totals") or {}),
                                                 "area": "all"}]:
        if not isinstance(row, Mapping) or not row.get("judged"):
            continue
        out.append(_figure(f"On time, {str(row['area']).replace('_', ' ')}",
                           row.get("on_time_share"), unit="percent",
                           note=_counts_detail(row)))
    return out


def _held_figure(out: Mapping[str, Any], *, alerts: bool) -> dict[str, Any]:
    holds = out.get("holds") or {}
    return _figure("Batches on quality hold", str(int(holds.get("batches_held") or 0)),
                   unit="records", note="Counted once per batch across systems. No hold "
                   "date is stored, so no age is shown" + ("; the aging alerts listed count "
                                                          "from the first order waiting on "
                                                          "the batch." if alerts else "."))


def _left_out(out: Mapping[str, Any], key: str) -> str:
    n = int((out.get("trimmed") or {}).get(key) or 0)
    return f" {n} more not listed (the answer's size limit)." if n else ""


RELEASE_NOTE = ("Batches and lots on hold that orders or receipts have waited on, the ones "
                "with open orders waiting first, then the longest waiting. Days count from "
                "the earliest allocated order (a batch) or the receipt (a lot): no hold date "
                "is stored, and on-hand quantities are not served yet.")
MATERIAL_KINDS = frozenset({"assembly_blocked_by_component", "production_late",
                            "stock_out_risk", "shortage"})


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _release_queue(hold_alerts: Sequence[Mapping[str, Any]], read: bool) -> dict[str, Any]:
    """The hold-release queue from the reader's own hold-aging alerts (already decided for
    the reader): open orders waiting first, then days waited."""
    if not read:
        return _section("hold_release", "Hold-release queue", [], state="unread",
                        note=UNREAD_NOTE)
    ranked = sorted(hold_alerts, key=lambda g: (
        -_int((g.get("event") or {}).get("orders_affected")),
        -_int((g.get("event") or {}).get("days")), str(g.get("id"))))
    rows = [_row(alert_label(str(g.get("kind"))), detail=_clip(g.get("summary"), 300),
                 object_id=g.get("object_id") if isinstance(g.get("object_id"), str) else None,
                 sources=_cite_handles(g)) for g in ranked]
    return _section("hold_release", "Hold-release queue", rows, limit=QC_ROWS,
                    note=RELEASE_NOTE, partial=True)


def _slot_rows(out: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for s in out.get("slots_not_completed") or ():
        line, line_cite = _text_of(s.get("line"))
        rows.append(_row(f"Slot on {line or 'an unnamed line'}", detail="past its start, "
                         "not marked completed", day=_day(s.get("start")),
                         object_id=s.get("object_id"),
                         sources=[*(s.get("citations") or ()), line_cite]))
    return rows


def qc_daily_pack(call: Call, held: Iterable[str], alerts: Mapping[str, Any] | None, *,
                  today: date | None = None, decide: Decide | None = None,
                  budget_seconds: float = BUDGET_SECONDS,
                  clock: Callable[[], float] | None = None) -> dict[str, Any]:
    """The daily QC pack for a quality reader: the quality checks open now (missed, overdue,
    due within a week), open calibration, production slots past start and not completed,
    batches on hold (the hold-aging alerts of the reader's list), the hold-release queue
    (those alerts ranked by the open orders waiting, then days; partial: no hold date or
    on-hand quantity is stored) and the production-materials alerts. A reader without quality_controlled gets every
    section marked so and nothing is read."""
    today = today or datetime.now(timezone.utc).date()
    keys = (("open_checks", "Quality checks open now"), ("calibration", "Open calibration"),
            ("slots", "Production slots not completed"), ("holds", "Batches on hold"),
            ("hold_release", "Hold-release queue"), ("materials", "Production materials"))
    head = {"schema": QC_DAILY_SCHEMA, "kind": "qc_daily", "title": f"Daily QC pack, "
            f"{today.isoformat()}", "subject": None, "subject_sources": [],
            "when": today.isoformat(), "as_of": today.isoformat(), "parties": [],
            "possible_parties": [], "basis": QC_BASIS}
    held = frozenset(held)
    if QUALITY not in held:
        return {**head, "sections": _quality_marked(keys), "gaps": []}
    reads = Reads(call, held, budget_seconds=budget_seconds, clock=clock, declared=None)
    reads.today = today
    out = _qc_read(reads, "daily")
    inbox = alerts_read(alerts, reads)
    gated = gate_alerts(visible_alerts(alerts), decide)
    if gated is None:
        inbox, gated = False, []
    hold_alerts = [g for g in gated if g["kind"] in QC_HOLD_KINDS]
    material_alerts = [g for g in gated if g["kind"] in MATERIAL_KINDS]
    if out is None:
        sections = [_section(k, t, [], state="unread", note="The QC figures could not be "
                             "read; this section may still have figures.") for k, t in keys[:3]]
        sections.append(_alert_section("holds", "Batches on hold", hold_alerts, inbox))
    else:
        rows = []
        for t in out.get("open_now") or ():
            title, title_cite = _text_of(t.get("title"))
            past = int(t.get("days_past_due") or 0)
            rows.append((t.get("area"), _row(
                title or "Untitled check", day=_day(t.get("due")), object_id=t.get("object_id"),
                detail=f"{str(t.get('area')).replace('_', ' ')}, "
                       f"{STATUS_WORDS.get(t.get('status'), t.get('status'))}"
                       + (f", {past} days past due" if past else ""),
                sources=[*(t.get("citations") or ()), title_cite])))
        counts = out.get("open_counts") or {}
        withheld = (out.get("checks") or {}).get("withheld")
        check_note = ("The people exclusion is not loaded, so no Planner check is shown."
                      if withheld else "Missed first, then overdue, then due within 7 days.")
        sections = [
            _section("open_checks", "Quality checks open now", [r for _a, r in rows],
                     state="blocked" if withheld else None, limit=QC_ROWS,
                     note=check_note + _left_out(out, "open_now"),
                     figures=[] if withheld else [
                         _figure("Missed, still open", str(counts.get("missed", 0)),
                                 unit="records"),
                         _figure("Overdue", str(counts.get("overdue", 0)), unit="records"),
                         _figure("Due within 7 days", str(counts.get("upcoming", 0)),
                                 unit="records")]),
            _section("calibration", "Open calibration",
                     [r for a, r in rows if a == "calibration"],
                     state="blocked" if withheld else None, limit=QC_ROWS),
            _section("slots", "Production slots not completed", _slot_rows(out),
                     limit=QC_ROWS, note="FileMaker schedule slots of the last 30 days past "
                     "their start day and not marked completed (the slot records no "
                     "completion day)."),
            _alert_section("holds", "Batches on hold", hold_alerts, inbox,
                           figures=[_held_figure(out, alerts=True)]),
        ]
    sections.append(_release_queue(hold_alerts, inbox))
    sections.append(_alert_section("materials", "Production materials", material_alerts,
                                   inbox, limit=QC_ROWS,
                                   note="Components short for an assembly, late production, "
                                        "stock running out: your alerts of these kinds."))
    sections = finish(sections, decide, reads)
    return {**head, "sections": sections, "gaps": reads.gaps(),
            "budget": reads.cited.budget()}


def qc_audit_pack(call: Call, held: Iterable[str], *, today: date | None = None,
                  decide: Decide | None = None, budget_seconds: float = BUDGET_SECONDS,
                  clock: Callable[[], float] | None = None) -> dict[str, Any]:
    """The QC audit pack (one printable page, ``pack_text``): on-time share of the quality
    checks by area over the last year, by month, each recurring check with its cycle and
    its missed, late and overdue occurrences cited, and the production schedule by line.
    A reader without quality_controlled gets every section marked so and nothing is
    read."""
    today = today or datetime.now(timezone.utc).date()
    keys = (("by_area", "Compliance by area"), ("by_month", "By month"),
            ("series", "Each recurring check"), ("schedule", "Production schedule by line"),
            ("holds", "Batches on hold"))
    head = {"schema": QC_AUDIT_SCHEMA, "kind": "qc_audit",
            "title": f"QC audit pack, as of {today.isoformat()}", "subject": None,
            "subject_sources": [], "when": today.isoformat(), "as_of": today.isoformat(),
            "parties": [], "possible_parties": [], "basis": QC_BASIS}
    held = frozenset(held)
    if QUALITY not in held:
        return {**head, "sections": _quality_marked(keys), "gaps": []}
    reads = Reads(call, held, budget_seconds=budget_seconds, clock=clock, declared=None)
    reads.today = today
    audit, monthly = _qc_read(reads, "audit"), _qc_read(reads, "compliance")
    if audit is None or monthly is None:
        sections = [_section(k, t, [], state="unread", note="The QC figures could not be "
                             "read; this section may still have figures.") for k, t in keys]
        return {**head, "sections": finish(sections, decide, reads), "gaps": reads.gaps(),
                "budget": reads.cited.budget()}
    checks = audit.get("checks") or {}
    blocked = "withheld" in checks
    window = audit.get("window") or {}
    series = []
    for s in audit.get("series") or ():
        title, cite = _text_of(s.get("title"))
        exceptions = s.get("exceptions") or ()
        listed = "; ".join(f"{STATUS_WORDS.get(e.get('status'), e.get('status'))} "
                           f"(due {e.get('due')})" for e in exceptions)
        series.append(_row(title or "Untitled check", day=_day(s.get("last_due")),
                           object_id=(exceptions[0] if exceptions else {}).get("object_id"),
                           detail=f"{str(s.get('area')).replace('_', ' ')}, every "
                                  f"{s.get('cycle_days')} days: " + _counts_detail(s)
                                  + (f". {listed}" if listed else ""),
                           sources=[h for e in exceptions for h in e.get("citations") or ()]
                           + [cite]))
    months = [_row(f"{m.get('month')} {str(m.get('area')).replace('_', ' ')}",
                   detail=_counts_detail(m)) for m in reversed(
                       (monthly.get("checks") or {}).get("by_month") or ())]
    schedule = monthly.get("schedule") or {}
    lines = []
    for row in schedule.get("by_line") or ():
        line, cite = _text_of(row.get("line"))
        lines.append(_row(line or "Unnamed line", sources=[cite], detail=(
            f"{row.get('judged', 0)} past start: {row.get('completed', 0)} completed, "
            f"{row.get('not_completed', 0)} not completed; {row.get('upcoming', 0)} "
            f"upcoming, {row.get('cancelled', 0)} cancelled")))
    totals = schedule.get("totals") or {}
    period = (f"Recurring checks due {window.get('from')} to {window.get('to')}; "
              f"{int(checks.get('not_recurring') or 0)} tasks of no recurring series are not "
              "judged.")
    note = "The people exclusion is not loaded, so no Planner check is shown."
    sections = [
        _section("by_area", "Compliance by area", [], figures=_area_figures(checks),
                 state="blocked" if blocked else None, note=note if blocked else period),
        _section("by_month", "By month", months, limit=QC_ROWS,
                 state="blocked" if blocked else None,
                 note=note if blocked else "Counts of the checks listed below, newest first."),
        _section("series", "Each recurring check", series, limit=QC_ROWS,
                 state="blocked" if blocked else None, note=note if blocked else
                 "Most missed first; each cites its missed, late and overdue occurrences."
                 + _left_out(audit, "series")),
        _section("schedule", "Production schedule by line", lines, limit=QC_ROWS,
                 figures=[_figure("Slots completed", totals.get("completed_share"),
                                  unit="percent", note=f"{totals.get('completed', 0)} of "
                                  f"{totals.get('judged', 0)} slots past start")],
                 note="FileMaker schedule slots by start day; the slot records no completion "
                      "day, so lateness is not measured."),
        _section("holds", "Batches on hold", [], figures=[_held_figure(audit, alerts=False)]),
    ]
    sections = finish(sections, decide, reads)
    return {**head, "sections": sections, "gaps": reads.gaps(),
            "budget": reads.cited.budget()}


def pack_text(pack: Mapping[str, Any]) -> str:
    """A pack as plain text for printing: each figure and line with its sources."""
    out = [str(pack.get("title") or ""), f"As of {pack.get('as_of')}", ""]
    for s in pack.get("sections") or ():
        out.append(str(s.get("title") or "").upper())
        if s.get("note"):
            out.append(f"  {s['note']}")
        for f in s.get("figures") or ():
            unit = "%" if f.get("unit") == "percent" and f.get("value") is not None else ""
            value = f.get("value") if f.get("value") is not None else "n/a"
            out.append(f"  {f.get('label')}: {value}{unit}"
                       + (f" ({f['note']})" if f.get("note") else ""))
        for r in s.get("rows") or ():
            when = f" [{r['date']}]" if r.get("date") else ""
            out.append(f"  - {r.get('label')}{when}" + (f": {r['detail']}" if r.get("detail")
                                                         else ""))
            for src in (r.get("sources") or ())[:MAX_SOURCES]:
                out.append(f"      source: {src.get('handle')}")
        if s.get("more"):
            out.append(f"  ... and {s['more']} more")
        out.append("")
    out.append(str(pack.get("basis") or ""))
    return "\n".join(out) + "\n"
