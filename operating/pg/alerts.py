"""Alerts as set-based SQL rules over the business model (``operating.pg.business_views``),
written to ONE table, ``<schema>.alerts``, that Home, Watching and Late POs read through the
same door as every business table (ARCH-FINAL-1117 sections 6, 16, 17).

* A rule is DECLARED (``Rule``): its alert type and kind (``operating/alert_kinds.json``), a
  plain description, the business tables it reads, ONE SELECT producing its current rows,
  and its resolve condition in words. No rule names a customer, an item, a person or a
  phrase; thresholds are named constants carried over from the reviewed Python detector
  each rule ports (``ported``).
* A rule's SELECT yields, per alert: ``handle`` (the record's handle in ``table``), ``topic``
  (what about it, when one record carries several: an item series; '' else), ``concerns``
  (the person it concerns: the mailbox, purchaser or salesperson as recorded; '' none),
  ``private`` (true: only that person sees it: their own mail), ``mailbox`` (a mail
  alert's mailbox, read by the door's ``mailbox = ANY``), ``why`` (one plain sentence built
  from columns), ``since`` (when the condition began; NULL: when first seen),
  ``access_class`` (the record row's), ``needs`` (every access class of every row it was
  computed from: a reader must hold all of them), ``ai_ok`` (false when any of those rows
  is never for AI) and ``col_classes`` (the col_class ids of those rows in ``table``).
* The runner (``run``) executes each rule in its own transaction inside Postgres: the
  current rows go to a temp table, a row new or back -> ``open`` (``since`` from the rule
  or now), a row still there -> keeps its ``since``, an open row gone -> ``resolved``.
  A rule whose tables are missing or whose statement fails is ``blocked``/``failed`` in
  ``alert_runs`` and leaves its alerts exactly as they were: an unrun rule never resolves
  anything. No record is loaded into Python; memory is Postgres's.
* Column markings: a rule names the columns its sentence shows (``shows``); the col_class
  ids (``col_classes``, shared by every view) of the rows' cells whose markings cover one
  of them are stored in ``col_needs``; a reader must hold every one (the door's allow-list
  of col classes; a class added later stays hidden). ``read`` is the one named read:
  classes, held column classes,
  mailboxes and the reader's own identities are its parameters, never spliced.

Fixture runs are ``isolated-fixture``; parity against the Python detectors on the served
data is recorded per rule in MASTER. Live-unverified until then.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from operating.measures import where_sql as measure_where
from operating.pg import business_views as bv
from operating.po_pilot import bc_pilot_po_sql
from operating.pg.people_excluded import internal_domains

KINDS_FILE = Path(__file__).resolve().parents[1] / "alert_kinds.json"
RULE_STATEMENT_MS = 120_000       # one rule's statement; a rule over it fails, never resolves
KEEP_RESOLVED_DAYS = 90
OUT_COLS = ("handle", "topic", "concerns", "private", "mailbox", "why", "since",
            "access_class", "needs", "ai_ok", "col_classes", "sources")
# a rule over the communications tables (``operating.pg.business_comms``) also yields
# ``units``: the comms unit ids (``comms_units``: audience key + record marks) of every
# message row it was computed from; a reader must hold all of them.
COMMS_COLS = OUT_COLS + ("units",)
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,60}$")


@dataclass(frozen=True)
class Rule:
    name: str                 # the alert type (alert_kinds.json "type")
    kind: str                 # its kind (alert_kinds.json "kind")
    about: str                # what it finds, in plain words
    table: str                # the business table its handle is in
    reads: tuple[str, ...]    # every business table the SELECT reads
    select: str               # ONE SELECT yielding OUT_COLS; {s} = the schema, {p} = params
    resolves: str             # when it resolves, in plain words
    ported: str               # the reviewed Python detector it ports
    shows: tuple[str, ...] = ()           # the columns of ``cols_of`` its sentence shows
    cols_of: str = ""                     # the table its col_classes are of (default table)
    # the columns of ``table`` (the record's own row) its sentence shows, when ``cols_of`` is
    # another table (same table: ``shows``)
    record_shows: tuple[str, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    # the measure whose rows the alert is (operating/measures.json): its where is the rule's,
    # never a copy (master plan AL1); "" for a rule not ported to a measure yet
    measure: str = ""
    comms: bool = False                   # its SELECT also yields ``units`` (COMMS_COLS)
    skips: str = ""                       # SELECT (reason, count): judged, left out, why
    # relations outside the business tables the SELECT reads ({s} = the schema): the
    # lineage side table, the ontology's current values (a copy's own change time)
    side: tuple[str, ...] = ()
    # SELECT (handle, object id) of lineage objects of rows OTHER than ``sources`` that the
    # alert rests on (a chain's purchase orders): recorded into ``objects`` too, so the
    # people exclusion hides the alert when one of them is excluded
    also_objects: str = ""
    # (table, column) the sentence also shows from a table other than ``cols_of`` (a line's
    # date on a header's card): tested against that column's markings like ``shows``
    also_shows: tuple[tuple[str, str], ...] = ()
    # the money the alert is about, in the currency its topic ends with, as SQL over the
    # window's sums (``below`` cost minus revenue on its lines under cost, ``q`` and ``rev``
    # the last window's quantity and revenue, ``prior_price`` the window before's price):
    # Home's Financials section lists the biggest first (page_reads ``alerts_impact_top``)
    impact: str = ""

    @property
    def shown_cols(self) -> tuple[str, ...]:
        return tuple(self.shows) + tuple(c for _, c in self.also_shows)


# --------------------------------------------------------------------------- thresholds
# carried over from the Python detectors (``ported``): never per record
LATE_GRACE_DAYS = 0
ACTIVE_DAYS = 180                      # active_population.ACTIVE_DAYS
# Orders with no Sage copy (``of_record`` false), decision 2026-11-18: the Portal
# is becoming the system of record, so a Portal-only open order is judged like one of
# record; FileMaker (retiring) only while its own record still changes: its last edit
# (``fm_modified_at``, FileMaker's audit stamp) within ACTIVE_DAYS. Any other copy-only
# order (BC pilot) is not judged.
SUCCESSOR_SYSTEMS = ("portal",)
RETIRING_SYSTEMS = ("filemaker",)
RETIRING_TOUCHED = ("fm_modified_at",)
# A retiring-source order that is a copy of an order of record:
# an order of record for the same customer dated within TWIN_DAYS of the copy's
# creation (``RETIRING_CREATED``: FileMaker's own stamp) or of its required date, with the
# same line quantities (every line, none unknown). Item codes are not compared: the two
# systems code the same product differently (measured 11-18: 5 of 43 late FileMaker-only
# orders have such a Sage twin, each checked by item name; quantities and names agree).
RETIRING_CREATED = ("fm_created_at",)
TWIN_DAYS = 7
# An open order with no promised date (none given, or the entry default: the order's own
# day, which 84% of Sage orders carry and 91% of those ship after; measured 2026-11-18) is
# "open longer than usual" past the 90th percentile of the days its customer's orders took
# from order to first invoice over USUAL_HISTORY_DAYS (at least USUAL_MIN_ORDERS of them),
# else of every customer's.
USUAL_SHARE = "0.9"
USUAL_HISTORY_DAYS = 730
USUAL_MIN_ORDERS = 10
# Late beyond the customer's usual slack: dated orders of
# record ship on or before their required date only 16% of the time (lead p50 2 days), so a
# day past it is not news. An order fires only when it is more days late than SLACK_SHARE
# of the customer's dated closed orders of the last USUAL_HISTORY_DAYS were at their first
# invoice (at least USUAL_MIN_ORDERS of them; else every customer's), and never under
# SLACK_MIN_DAYS. Measured 11-18: all-customer 75th percentile 4 days (1,710 orders).
SLACK_SHARE = "0.75"
SLACK_MIN_DAYS = 3
PRICE_DAYS = 180                       # proactive_prices.PRICE_DAYS
MIN_CUSTOMERS = 3                      # proactive_prices.MIN_CUSTOMERS
MIN_OTHER_PRODUCTS = 2                 # proactive_prices.MIN_OTHER_PRODUCTS
BELOW_TYPICAL = "0.15"                 # proactive_prices.BELOW_TYPICAL
LOW_SHARE = "0.10"                     # proactive_prices.LOW_SHARE
COST_TOLERANCE = "0.95"                # proactive_prices.COST_TOLERANCE
OWN_DROP = "0.10"                      # proactive_prices.OWN_DROP
DEMAND_RECENT_DAYS = 90                # proactive_ceo.DEMAND_RECENT_DAYS
DEMAND_BASELINE_DAYS = 365             # proactive_ceo.DEMAND_BASELINE_DAYS
DEMAND_MIN_ORDERS = 4                  # proactive_ceo.DEMAND_MIN_ORDERS
DEMAND_DROP_SHARE = "0.5"              # proactive_ceo.DEMAND_DROP_SHARE
SOURCE_CURRENT_DAYS = 30               # proactive_ceo.SOURCE_CURRENT_DAYS
HOLD_WORDS = ("on-hold", "on hold", "hold", "onhold")        # stock_state.HOLD_WORDS
CLOSED_BATCH = ("cancelled", "canceled", "closed")           # stock_state.CLOSED_BATCH
MADE_BATCH = ("realized", "realised", "completed", "complete", "manufactured", "produced")
ACTIVE_BATCH = ("allocated", "committed", "generated", "scheduled", "planned",
                "in progress", "released")
STATE_SHARE = "0.95"                   # source_defaults.STATE_SHARE
STATE_MINIMUM = 10                     # source_defaults.MINIMUM
# A batch made longer ago than this and still marked on hold is history, not news: the hold
# was never cleared in its old system (batches made in 2017-2019 still read On-Hold), and
# nobody decides a release for stock that old. It stays readable on the batch itself.
HOLD_STALE_DAYS = 365
# 11-23 (limits audit #37): a reply owed stays listed 60 days (was 30: older ones vanished
# unanswered); no cap on the To list ("more than 5 is a mailing") and none on the records a
# message names ("more than 4 is a report"): the bulk and automated-sender tests carry that
# judgement, a count of people does not.
REPLY_DAYS = 60                        # was proactive_commitments.REPLY_DAYS 30
MARGIN_DAYS = 30                       # proactive_events.MARGIN_DAYS
REVISION_DAYS = 14                     # proactive_events.REVISION_DAYS
REVISED_DATES = ("expected_date", "bc_expected_receipt_date")   # proactive_events.REVISED_DATES
BULK_DATE_SHARE = "0.10"               # source_defaults.DATE_SHARE
FIRM_TIERS = ("verified", "reviewed", "corroborated")   # impact_chain's firm links
NO_REPLY_DAYS = 7                      # proactive_events.NO_REPLY_DAYS
NO_REPLY_WINDOW_DAYS = 90              # proactive_events.NO_REPLY_WINDOW_DAYS
# vendor_terms._ASKS / _REQUEST_WORDS and proactive_events._ETA_WORDS, as Postgres regexes
_TERM_RE = (r"\m(quot(e|es|ation|ations)|rfq|pric(e|es|ing)|confirm(ation)?|"
            r"lead[ \t-]*time|moq|availability|proforma)\M")
_REQUEST_RE = (r"\?|\m(please|kindly|could you|can you|would you|let us know|let me know|"
               r"send (us|me)|share|advise|provide|confirm)\M")
_ETA_RE = (r"\m(eta|etas|delivery|deliver(ed|ing)?|ship(s|ping|ment|ped)?|dispatch(ed)?|"
           r"arriv(e|al|ing)|tracking|status|update)\M")
_FORWARD_RE = r"^\s*(\[[^\]]{1,30}\]|[A-Za-z]{2,4}(\[[0-9]+\])?\s*:\s*)*(FW|FWD|WG|TR|RV)\s*:"
_REPLY_RE = r"^\s*((\[[^\]]{1,30}\]|[A-Za-z]{2,4}(\[[0-9]+\])?\s*:\s*)*)?(RE|AW|SV|VS|R|RIF|ANTW)\s*:"
# A line whose booked cost is more than this many times its revenue is a wrong unit cost in
# Sage (a case cost on an each line), not a price below cost: measures.json's cost_looks_wrong
# ("margin < -4 * revenue") and its gross-margin and loss measures leave the same lines out
# (operating/tests/test_po_pilot.py holds the two together).
COST_WRONG_TIMES = 5
COST_LOOKS_WRONG_SQL = f"(l.revenue > 0 AND l.margin < -{COST_WRONG_TIMES - 1} * l.revenue)"
MARGIN_BASES = ("same_currency", "cost_converted_at_invoice_rate")   # a decided line margin


# --------------------------------------------------------------------------- the rules
# The single-record parts every rule shares: one row's own class, and AI use.
ONE = ("r.access_class, ARRAY[r.access_class]::smallint[], r.ai_ok, "
       "ARRAY[r.col_class]::smallint[], ARRAY[r.handle]::text[]")

LATE_PO = Rule(
    "po_late", "late_po",
    "An open purchase order past its requested-by date: the model's own verdict "
    "(purchase_orders.verdict = 'late'; a draft or unreleased PO is never late there).",
    "purchase_orders", ("purchase_orders",),
    "SELECT r.handle, '' , coalesce(r.purchaser, ''), false, NULL::text, "
    "concat_ws(' ', 'PO', r.po_number, 'from', coalesce(r.supplier, 'an unnamed supplier'), "
    "'is', r.days_late, CASE WHEN r.days_late = 1 THEN 'day' ELSE 'days' END, "
    "'past its requested date of', to_char(r.requested_date, 'Mon DD, YYYY') || '.'), "
    "(r.requested_date + 1)::timestamptz, " + ONE + " "
    "FROM {s}.purchase_orders r WHERE " + measure_where("late_purchase_orders")
    + " AND r.days_late > {p[grace]}",
    "the PO is received, closed or cancelled, or its date moves (its verdict is no longer late)",
    "measure late_purchase_orders (operating/measures.json; its stated exclusion: held only "
    "in Business Central)",
    shows=("po_number", "supplier", "days_late", "requested_date"),
    params={"grace": LATE_GRACE_DAYS}, measure="late_purchase_orders")

# Due today, not confirmed (the old late list's ``po_due``: a late-rule PO with no days late
# yet): an open PO whose requested-by date is the day judged, no supplier-promised date on
# it, and not already late by the model's verdict.
DUE_PO = Rule(
    "po_due", "late_po",
    "An open purchase order whose requested-by date is today, with no date the supplier "
    "promised on record: not late yet, worth a confirmation.",
    "purchase_orders", ("purchase_orders",),
    "SELECT r.handle, '', coalesce(r.purchaser, ''), false, NULL::text, "
    "concat_ws(' ', 'PO', r.po_number, 'from', coalesce(r.supplier, 'an unnamed supplier'), "
    "'is due today and the supplier has not confirmed a date.'), "
    "r.requested_date::timestamptz, " + ONE + " "
    "FROM {s}.purchase_orders r WHERE r.status = 'open' AND r.verdict IS DISTINCT FROM 'late' "
    "AND r.requested_date = {p[day]}::date AND r.promised_date IS NULL "
    "AND NOT " + bc_pilot_po_sql("r"),
    "the day passes (late, if still open: po_late), the supplier confirms a date, or the PO is "
    "received, closed or cancelled",
    "proactive_purchasing late_or_unconfirmed_po, not late yet (briefing po_due)",
    shows=("po_number", "supplier", "requested_date"))

# Open, no due date recorded (the old engine's ``undated_po``): the model's own verdict
# (``unknown`` for ``no_requested_date``: no requested-by date, supplier date or lead time on
# any copy); a draft is never open. Only a PO ordered within ACTIVE_DAYS: an older one is a
# stale legacy back-order (precision sample 11-08: 7 of 15 were), counted in ``skips``.
UNDATED_PO = Rule(
    "po_undated", "late_po",
    "An open purchase order ordered within the last 180 days with no requested-by date, "
    "supplier-confirmed date or lead time recorded on any copy of it, so it can never be "
    "counted late.",
    "purchase_orders", ("purchase_orders",),
    "SELECT r.handle, '', coalesce(r.purchaser, ''), false, NULL::text, "
    "concat_ws(' ', 'PO', r.po_number, 'from', coalesce(r.supplier, 'an unnamed supplier'), "
    "'ordered', to_char(r.order_date, 'Mon DD, YYYY'), "
    "'is open with no due date recorded, so it is never counted late.'), "
    "r.order_date::timestamptz, " + ONE + " "
    "FROM {s}.purchase_orders r WHERE r.status = 'open' AND r.verdict = 'unknown' "
    "AND r.verdict_reason = 'no_requested_date' "
    "AND r.order_date >= {p[day]}::date - {p[active]} AND NOT " + bc_pilot_po_sql("r"),
    "a requested-by, promised or lead-time date is recorded, or the PO is received, closed "
    "or cancelled",
    "proactive_engine_purchasing undated_po (po_undated)",
    shows=("po_number", "supplier", "order_date"),
    params={"active": ACTIVE_DAYS},
    skips="SELECT CASE WHEN r.order_date IS NULL THEN 'no_order_date' ELSE 'historic' END, "
    "count(*) FROM {s}.purchase_orders r WHERE r.status = 'open' AND r.verdict = 'unknown' "
    "AND r.verdict_reason = 'no_requested_date' AND (r.order_date IS NULL OR "
    "r.order_date < {p[day]}::date - {p[active]}) GROUP BY 1")

# An order whose item lines lost money (the old engine's ``negative_margin``): orders whose
# latest invoice is within MARGIN_DAYS before the newest invoice its own source system holds,
# judged on the invoices' priced item lines alone (the margin on item cost: freight and other
# misc lines are not in it; zero-price lines cost their items but lose no sale, so they are
# left out; an order with nothing priced is a no-charge order, no loss). Every priced line
# must carry a decided margin (cost known) in one currency, else the order is not judged
# (counted in ``skips``). Credit memos are not in the business tables yet.
NEGATIVE_MARGIN = Rule(
    "negative_margin", "margin_loss",
    "A customer order invoiced in the last 30 days (before its system's newest invoice) whose "
    "priced item lines cost more than they were billed: the sum of the lines' margins on "
    "item cost is below zero, in one currency.",
    "sales_orders", ("sales_orders", "sales_invoices", "sales_invoice_lines"),
    "WITH newest AS (SELECT source_systems AS sys, max(invoice_date) AS d "
    "FROM {s}.sales_invoices WHERE invoice_date <= {p[day]}::date GROUP BY 1), "
    "roots AS (SELECT i.order_number, max(i.invoice_date) AS last_invoice "
    "FROM {s}.sales_invoices i JOIN newest n ON n.sys = i.source_systems "
    "WHERE i.order_number IS NOT NULL AND i.pilot IS NOT TRUE "
    "AND i.invoice_date <= {p[day]}::date GROUP BY i.order_number "
    "HAVING bool_or(i.invoice_date > n.d - {p[days]})), "
    "m AS (SELECT o.order_number, max(o.last_invoice) AS last_invoice, "
    "count(DISTINCT l.margin_currency) AS curs, max(l.margin_currency) AS cur, "
    "sum(l.margin) AS mar, sum(l.revenue) AS rev, "
    "count(*) FILTER (WHERE l.margin IS NULL OR l.margin_basis <> ALL({p[bases]})) AS unknown, "
    "array_agg(DISTINCT l.access_class) || array_agg(DISTINCT i.access_class) AS classes, "
    "bool_and(l.ai_ok AND i.ai_ok) AS ai_ok, array_agg(DISTINCT l.col_class) AS cols, "
    "array_agg(DISTINCT l.handle) AS srcs FROM roots o "
    "JOIN {s}.sales_invoices i ON i.order_number = o.order_number AND i.pilot IS NOT TRUE "
    "AND i.invoice_date <= {p[day]}::date "
    "JOIN {s}.sales_invoice_lines l ON l.invoice_handle = i.handle AND l.pilot IS NOT TRUE "
    "AND l.line_type = 'item' AND l.revenue > 0 GROUP BY o.order_number), "
    "so AS (SELECT DISTINCT ON (r.order_number) r.* FROM {s}.sales_orders r "
    "WHERE r.of_record IS NOT FALSE ORDER BY r.order_number, r.handle) "
    "SELECT so.handle, '', coalesce(so.salesperson, ''), false, NULL::text, "
    "concat_ws(' ', 'Order', so.order_number, 'for', coalesce(so.customer, 'an unnamed customer'), "
    "'lost', round(-m.mar, 2)::text, m.cur, 'on item cost: its priced invoice lines cost "
    "more than they billed.'), m.last_invoice::timestamptz, so.access_class, "
    "ARRAY(SELECT DISTINCT c FROM unnest(m.classes || so.access_class) c WHERE c IS NOT NULL), "
    "so.ai_ok AND m.ai_ok, m.cols, m.srcs "
    "FROM m JOIN so ON so.order_number = m.order_number "
    "WHERE m.mar < 0 AND m.rev > 0 AND m.curs = 1 AND m.unknown = 0",
    "the order's priced item lines no longer lose money (a late invoice or credit), or its "
    "latest invoice leaves the window",
    "proactive_events negative_margin (margin loss on item cost)",
    shows=("margin", "margin_currency", "margin_basis", "revenue", "line_type"),
    cols_of="sales_invoice_lines", record_shows=("order_number", "customer", "salesperson"),
    params={"days": MARGIN_DAYS, "bases": list(MARGIN_BASES)},
    skips="WITH newest AS (SELECT source_systems AS sys, max(invoice_date) AS d "
    "FROM {s}.sales_invoices WHERE invoice_date <= {p[day]}::date GROUP BY 1), "
    "roots AS (SELECT i.order_number FROM {s}.sales_invoices i JOIN newest n "
    "ON n.sys = i.source_systems WHERE i.order_number IS NOT NULL AND i.pilot IS NOT TRUE "
    "AND i.invoice_date <= {p[day]}::date GROUP BY i.order_number "
    "HAVING bool_or(i.invoice_date > n.d - {p[days]})) "
    "SELECT 'margin_not_decided', count(*) FROM roots o WHERE EXISTS (SELECT 1 FROM "
    "{s}.sales_invoices i JOIN {s}.sales_invoice_lines l ON l.invoice_handle = i.handle "
    "WHERE i.order_number = o.order_number AND l.line_type = 'item' AND l.revenue > 0 "
    "AND (l.margin IS NULL OR l.margin_basis <> ALL({p[bases]})))")

# A date moved later (the old engine's ``eta_slipped``, the source-field half): an open PO's
# requested-by date (or a line's expected receipt date) whose current value is later than the
# value this copy held before, superseded within REVISION_DAYS, read from the ontology's own
# value history. Not a slip: a closed or draft order, a day that is the order's own day, or a
# day a bulk share (BULK_DATE_SHARE) of that property's current values carry (a default, not a
# promise). The supplier-confirmation half (two confirmations promising later) has no business
# table yet and is not judged here.
ETA_SLIPPED = Rule(
    "eta_slipped", "late_po",
    "An open purchase order whose requested-by date (or a line's expected receipt date) was "
    "changed to a later day within the last 14 days, on the source's own value history.",
    "purchase_orders", ("purchase_orders", "purchase_order_lines"),
    "WITH v AS (SELECT lg.view, lg.handle AS rec, v.object_id, v.property, v.binding_id, "
    "v.field_path, v.value, v.superseded_at FROM {s}.lineage lg "
    "CROSS JOIN LATERAL unnest(lg.object_ids) x(oid) JOIN ontology.object_values v "
    "ON v.object_id = x.oid AND v.property = ANY({p[props]}) "
    "AND (v.superseded_at IS NULL OR v.superseded_at >= ({p[day]}::date - {p[days]})::text) "
    "AND v.value ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' "
    "WHERE lg.view IN ('purchase_orders', 'purchase_order_lines')), "
    "k AS (SELECT view, rec, object_id, property, binding_id, field_path, "
    "count(*) FILTER (WHERE superseded_at IS NULL) AS n_cur, "
    "max(left(value, 10)) FILTER (WHERE superseded_at IS NULL) AS now_day, "
    "(array_agg(left(value, 10) ORDER BY superseded_at DESC) "
    "FILTER (WHERE superseded_at IS NOT NULL))[1] AS was_day "
    "FROM v GROUP BY 1, 2, 3, 4, 5, 6), "
    "bulk AS (SELECT property, left(value, 10) AS day, count(*) AS n, "
    "sum(count(*)) OVER (PARTITION BY property) AS tot FROM ontology.object_values "
    "WHERE superseded_at IS NULL AND property = ANY({p[props]}) GROUP BY 1, 2), "
    "s AS (SELECT k.rec, k.view, k.property, k.was_day::date AS was, k.now_day::date AS now, "
    "CASE WHEN k.view = 'purchase_orders' THEN k.rec ELSE (SELECT l.po_handle "
    "FROM {s}.purchase_order_lines l WHERE l.handle = k.rec) END AS po, k.rec AS src, "
    "CASE WHEN k.view = 'purchase_order_lines' THEN (SELECT l.col_class "
    "FROM {s}.purchase_order_lines l WHERE l.handle = k.rec) END AS lcc "
    "FROM k WHERE k.n_cur = 1 AND k.was_day IS NOT NULL AND k.now_day > k.was_day "
    "AND NOT EXISTS (SELECT 1 FROM bulk b WHERE b.property = k.property AND b.tot >= 10 "
    "AND b.day IN (k.now_day, k.was_day) AND b.n >= b.tot * {p[bulk]}::numeric)) "
    "SELECT r.handle, max(s.property), coalesce(r.purchaser, ''), false, NULL::text, "
    "concat_ws(' ', 'PO', r.po_number, 'from', coalesce(r.supplier, 'an unnamed supplier'), "
    "'has its', CASE WHEN max(s.property) = 'expected_date' THEN 'requested-by date' "
    "ELSE 'expected receipt date' END, 'moved later by', max(s.now - s.was)::text, 'days, from', "
    "to_char(min(s.was), 'Mon DD, YYYY'), 'to', to_char(max(s.now), 'Mon DD, YYYY') || '.'), "
    "NULL::timestamptz, r.access_class, ARRAY[r.access_class]::smallint[], r.ai_ok, "
    "ARRAY[r.col_class]::smallint[] || coalesce(array_agg(DISTINCT s.lcc::smallint) "
    "FILTER (WHERE s.lcc IS NOT NULL), '{{}}'::smallint[]), ARRAY[r.handle]::text[] "
    "FROM s JOIN {s}.purchase_orders r ON r.handle = s.po "
    "WHERE r.status = 'open' AND (r.order_date IS NULL OR (r.order_date <> s.was "
    "AND r.order_date <> s.now)) GROUP BY r.handle, s.property, r.po_number, r.supplier, "
    "r.purchaser, r.access_class, r.ai_ok, r.col_class",
    "the PO is received, closed or cancelled, or the change leaves the 14-day window",
    "proactive_events eta_slipped (source field revisions)",
    shows=("po_number", "supplier", "requested_date", "order_date"),
    also_shows=(("purchase_order_lines", "expected_date"),),
    params={"props": list(REVISED_DATES), "days": REVISION_DAYS, "bulk": BULK_DATE_SHARE},
    side=("{s}.lineage", "ontology.object_values"))

# Our request to a vendor with no answer (the old engine's ``vendor_no_reply``): the mail
# thread naming an open PO (any number of records a message names counts, 11-23), our last message in it to an outside address (a sender in one of
# our domains; recipients when projected, else when the outside wrote before it), sent
# NO_REPLY_DAYS to NO_REPLY_WINDOW_DAYS before the day judged, asking (a term or ETA word and a
# request word in one sentence of its preview or, if it is no reply, its subject), with no
# later message from the outside in the thread. A forward with no recipients projected is no
# request. One alert per PO (its longest wait). A reader must hold every message unit of the
# thread. Not ported: the resolver that picks which order a reused PO number is about (such a
# mention is simply not read: ``skips``).
def _asks(text: str) -> str:
    return ("EXISTS (SELECT 1 FROM regexp_split_to_table(coalesce(" + text + ", ''), "
            "'(?<=[.!?])|\\n') q WHERE (q ~* {p[term]} OR q ~* {p[eta]}) AND q ~* {p[request]})")


VENDOR_NO_REPLY = Rule(
    "vendor_no_reply", "waiting_on_them",
    "An open purchase order whose mail thread shows our last message to the outside asking a "
    "vendor for terms or a date, sent 7 to 90 days ago, with no later message from the outside.",
    "purchase_orders", ("purchase_orders", "emails"),
    "WITH named AS (SELECT DISTINCT p.handle AS po, e.thread FROM {s}.emails e "
    "JOIN {s}.purchase_orders p ON p.handle = ANY(e.records) "
    "WHERE p.status = 'open' AND e.thread IS NOT NULL "
    "AND e.excluded IS NOT TRUE), "
    "m AS (SELECT n.po, e.handle, e.unit, e.ai_ok, e.object_ids, e.subject, e.thread, "
    "coalesce(e.sent_at, e.received_at) AS t, "
    "split_part(lower(e.sender), '@', 2) = ANY({p[domains]}) AS ours, "
    "coalesce(e.to_addresses, '{{}}') || coalesce(e.cc_addresses, '{{}}') AS told "
    "FROM named n JOIN {s}.emails e ON e.thread = n.thread "
    "WHERE coalesce(e.sent_at, e.received_at) IS NOT NULL), "
    "r AS (SELECT m.*, (cardinality(m.told) > 0 AND EXISTS (SELECT 1 FROM unnest(m.told) a "
    "WHERE split_part(lower(a), '@', 2) <> ALL({p[domains]}))) OR (cardinality(m.told) = 0 "
    "AND EXISTS (SELECT 1 FROM m o WHERE o.po = m.po AND o.thread = m.thread AND NOT o.ours "
    "AND o.t < m.t)) AS to_outside FROM m), "
    "last AS (SELECT DISTINCT ON (po, thread) po, thread, handle, object_ids, subject, t, "
    "cardinality(told) AS n_told FROM r WHERE ours AND to_outside ORDER BY po, thread, t DESC, "
    "handle), "
    "w AS (SELECT l.*, ({p[day]}::date - l.t::date) AS waited, "
    "(SELECT value FROM ontology.object_values v WHERE v.object_id = ANY(l.object_ids) "
    "AND v.property = 'body_preview' AND v.superseded_at IS NULL LIMIT 1) AS preview "
    "FROM last l WHERE l.t::date <= {p[day]}::date - {p[wait]} "
    "AND l.t::date >= {p[day]}::date - {p[window]} AND NOT EXISTS (SELECT 1 FROM r o "
    "WHERE o.po = l.po AND o.thread = l.thread AND NOT o.ours AND o.t > l.t)), "
    "k AS (SELECT w.* FROM w WHERE NOT (w.n_told = 0 AND (w.subject ~* {p[forward]} "
    "OR coalesce(w.preview, '') ~* 'forwarded message')) AND (" + _asks("w.preview") + " OR "
    "(w.subject !~* {p[reply]} AND " + _asks("w.subject") + "))), "
    "best AS (SELECT DISTINCT ON (po) k.* FROM k ORDER BY po, waited DESC, thread), "
    "u AS (SELECT b.po, array_agg(DISTINCT x.unit) AS units, bool_and(x.ai_ok) AS ai_ok "
    "FROM best b JOIN {s}.emails x ON x.thread = b.thread GROUP BY 1) "
    "SELECT p.handle, '', coalesce(p.purchaser, ''), false, NULL::text, "
    "concat_ws(' ', 'Our last request about PO', p.po_number, 'to', coalesce(p.supplier, "
    "'its vendor'), 'was sent', to_char(b.t AT TIME ZONE '" + bv.BUSINESS_TZ + "', "
    "'Mon DD, YYYY'), 'and has had no reply for', b.waited::text, 'days.'), b.t, "
    "p.access_class, ARRAY[p.access_class]::smallint[], p.ai_ok AND u.ai_ok, "
    "ARRAY[p.col_class]::smallint[], ARRAY[p.handle]::text[], u.units "
    "FROM best b JOIN {s}.purchase_orders p ON p.handle = b.po JOIN u ON u.po = b.po",
    "a message from the outside arrives in the thread, or the PO closes, or the request "
    "leaves the 90-day window",
    "proactive_events vendor_no_reply (F28)",
    shows=("po_number", "supplier"), comms=True,
    params={"wait": NO_REPLY_DAYS, "window": NO_REPLY_WINDOW_DAYS,
            "domains": list(internal_domains()), "term": _TERM_RE, "eta": _ETA_RE,
            "request": _REQUEST_RE, "forward": _FORWARD_RE, "reply": _REPLY_RE})

# A customer order a late purchase order puts at risk (the old engine's
# ``customer_order_at_risk``, ``impact_chain`` firm chains only): from each open PO the model
# judges late, over recorded links at a firm tier: PO <- PurchaseOrderLine.line_of - line -
# PurchaseOrderLine.for_item -> item <- AssemblyOrderComponent.uses_item - component -
# AssemblyOrderComponent.component_of -> assembly order <- AssemblyOrderAllocation.allocates -
# allocation - AssemblyOrderAllocation.for_order_line -> order line - SalesOrderLine.line_of ->
# customer order. The material date is the later of the day judged and the supplier's promise;
# days at risk = material date - the order's required date, the worst over the POs feeding it
# (no assembly lead time is recorded, so a lower bound). Lead-tier chains (a lead link, an item
# inference, a packet mention) are not walked: the old engine labelled them "lead, not firm"
# and they are left out of the card.
def _link(kind: str, alias: str) -> str:
    return (f"{alias} AS (SELECT from_object, to_object FROM ontology.links WHERE link_type = "
            f"'{kind}' AND tier = ANY({{p[firm]}}) AND superseded_at IS NULL)")


_AT_RISK_WITH = ("WITH " + ", ".join((
        "lp AS (SELECT p.handle AS po, p.po_number, p.supplier, p.access_class AS pc, p.ai_ok AS pai, "
        "p.col_class AS pcol, greatest({p[day]}::date, p.promised_date) AS material, o AS pobj "
        "FROM {s}.purchase_orders p JOIN {s}.lineage g ON g.view = 'purchase_orders' "
        "AND g.handle = p.handle CROSS JOIN LATERAL unnest(g.object_ids) o "
        "WHERE p.status = 'open' AND p.verdict = 'late')",
        _link("PurchaseOrderLine.line_of", "l1"), _link("PurchaseOrderLine.for_item", "l2"),
        _link("AssemblyOrderComponent.uses_item", "l3"),
        _link("AssemblyOrderComponent.component_of", "l4"),
        _link("AssemblyOrderAllocation.allocates", "l5"),
        _link("AssemblyOrderAllocation.for_order_line", "l6"),
        _link("SalesOrderLine.line_of", "l7"),
        "ch AS (SELECT lp.po, lp.po_number, lp.supplier, lp.pc, lp.pai, lp.pcol, lp.material, "
        "l7.to_object AS oobj FROM lp JOIN l1 ON l1.to_object = lp.pobj "
        "JOIN l2 ON l2.from_object = l1.from_object JOIN l3 ON l3.to_object = l2.to_object "
        "JOIN l4 ON l4.from_object = l3.from_object JOIN l5 ON l5.to_object = l4.to_object "
        "JOIN l6 ON l6.from_object = l5.from_object JOIN l7 ON l7.from_object = l6.to_object)",
        "hit AS (SELECT g.handle AS ord, ch.po, ch.po_number, ch.supplier, ch.pc, ch.pai, "
        "ch.pcol, ch.material FROM ch JOIN {s}.lineage g ON g.view = 'sales_orders' "
        "AND ch.oobj = ANY(g.object_ids)), "
        "risk AS (SELECT r.handle, r.order_number, r.customer, r.salesperson, r.access_class, "
        "r.ai_ok, r.col_class, r.required_date, max(h.material - r.required_date) AS days, "
        "count(DISTINCT h.po) AS pos, (array_agg(h.po_number ORDER BY h.material DESC))[1] AS po_number, "
        "array_agg(DISTINCT h.pc) AS pcs, bool_and(h.pai) AS pai, array_agg(DISTINCT h.po) AS srcs "
        "FROM hit h JOIN {s}.sales_orders r ON r.handle = h.ord "
        "WHERE r.status = 'open' AND r.required_date IS NOT NULL AND h.material > r.required_date "
        "GROUP BY r.handle, r.order_number, r.customer, r.salesperson, r.access_class, r.ai_ok, "
        "r.col_class, r.required_date)")) + " ")

AT_RISK = Rule(
    "customer_order_at_risk", "customer_order_late",
    "An open customer order that a late purchase order puts at risk through recorded links: "
    "the PO's item is consumed by an assembly order allocated to one of the order's lines, "
    "and the material is expected after the order's required date.",
    "sales_orders", ("sales_orders", "purchase_orders"),
    _AT_RISK_WITH +
    "SELECT x.handle, '', coalesce(x.salesperson, ''), false, NULL::text, "
    "concat_ws(' ', 'Order', x.order_number, 'for', coalesce(x.customer, 'an unnamed customer'), "
    "'is at least', x.days::text, 'days at risk from', x.pos::text, 'late purchase', "
    "CASE WHEN x.pos = 1 THEN 'order' ELSE 'orders' END, '(PO ' || x.po_number || "
    "CASE WHEN x.pos > 1 THEN ' and others' ELSE '' END || ') against its required date of', "
    "to_char(x.required_date, 'Mon DD, YYYY') || '.'), x.required_date::timestamptz, "
    "x.access_class, ARRAY(SELECT DISTINCT c FROM unnest(x.pcs || x.access_class) c), "
    "x.ai_ok AND x.pai, ARRAY[x.col_class]::smallint[], ARRAY[x.handle] FROM risk x",
    "the late PO is received or no longer late, or the order ships or its required date moves",
    "impact_chain (customer_order_at_risk, firm chains)",
    shows=("order_number", "customer", "required_date"), params={"firm": list(FIRM_TIERS)},
    side=("{s}.lineage", "ontology.links"),
    also_objects=_AT_RISK_WITH + "SELECT DISTINCT h.ord, o FROM hit h JOIN risk x ON x.handle = h.ord "
    "JOIN {s}.lineage lg ON lg.view = 'purchase_orders' AND lg.handle = h.po "
    "CROSS JOIN LATERAL unnest(lg.object_ids || lg.read_ids) o WHERE o IS NOT NULL")

# A late customer order is current work only while the order is live (the old engine's
# active population, ``active_population``): a business date of it (its order day, its
# required day, or the newest invoice billing it: the activity Sage records) within
# ACTIVE_DAYS of the day judged. Measured on served data 11-18: 29 of 55 open past-due
# orders of record had no date or invoice within 180 days (open in Sage since 2018-2026,
# 211-3159 days "late": stale headers, not late deliveries). An order whose lines all state
# nothing outstanding (and none states something) has shipped without a closing record
# (5 of the rest: Sage states 0 outstanding on every line), and one an invoice bills with
# no line stating anything outstanding has shipped too (8: the old engine closes an order
# an invoice bills, ``sales_order_status``); a line still owed keeps a partly shipped order
# late. Each left-out order is counted by its reason (``skips``), never listed.
def _order_judged(which: str) -> str:
    """The open orders a customer-order rule judges (``which``: ``late``: past a real
    required date; ``undated``: no required date), each with its skip reason (NULL: judged
    live), its ``needs`` (the order's, its invoices' and its lines' classes) and AI use.
    Of record, or Portal-only, or FileMaker-only still edited within ACTIVE_DAYS."""
    pick = {"late": "r.days_late > {p[grace]}",
            "undated": "r.required_date IS NULL"}[which]
    return (
        "WITH o AS (SELECT r.*, lg.systems, lg.object_ids AS objs FROM {s}.sales_orders r "
        "LEFT JOIN {s}.lineage lg ON lg.view = 'sales_orders' AND lg.handle = r.handle "
        f"WHERE r.status = 'open' AND {pick} AND (r.of_record IS NOT FALSE OR "
        "(cardinality(lg.systems) > 0 AND (lg.systems <@ {p[successor]} "
        "OR lg.systems <@ {p[retiring]})))), "
        "touch AS (SELECT o.handle, max(v.value::timestamptz) FILTER (WHERE v.property = "
        "ANY({p[touched]})) AS touched, (max(v.value::timestamptz) FILTER (WHERE "
        "v.property = ANY({p[created]})))::date AS created FROM o "
        "CROSS JOIN LATERAL unnest(o.objs) x(oid) JOIN ontology.object_values v "
        "ON v.object_id = x.oid AND v.superseded_at IS NULL "
        "AND v.property = ANY({p[touched]} || {p[created]}) "
        "AND v.value ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' "
        "WHERE o.of_record IS FALSE AND o.systems <@ {p[retiring]} GROUP BY 1), "
        # a retiring copy's line quantities, and whether an order of record is its twin
        "cq AS (SELECT o.handle, array_agg(l.ordered_quantity ORDER BY l.ordered_quantity) AS qs "
        "FROM o JOIN {s}.sales_order_lines l ON l.order_handle = o.handle "
        "WHERE o.of_record IS FALSE AND o.systems <@ {p[retiring]} GROUP BY 1), "
        "twin AS (SELECT DISTINCT cq.handle FROM cq JOIN o ON o.handle = cq.handle "
        "JOIN touch t ON t.handle = cq.handle "
        "JOIN {s}.sales_orders g ON g.of_record AND g.customer_handle = o.customer_handle "
        "AND (g.order_date BETWEEN t.created - {p[twin]} AND t.created + {p[twin]} "
        "OR g.order_date BETWEEN o.required_date - {p[twin]} AND o.required_date + {p[twin]}) "
        "WHERE array_position(cq.qs, NULL) IS NULL AND cq.qs = (SELECT array_agg("
        "gl.ordered_quantity ORDER BY gl.ordered_quantity) FROM {s}.sales_order_lines gl "
        "WHERE gl.order_handle = g.handle)), "
        "inv AS (SELECT i.order_number, max(i.invoice_date) AS last_invoice, "
        "array_agg(DISTINCT i.access_class) AS classes, bool_and(i.ai_ok) AS ai_ok "
        "FROM {s}.sales_invoices i WHERE i.order_number IN (SELECT order_number FROM o) "
        "AND i.invoice_date <= {p[day]}::date GROUP BY 1), "
        "ln AS (SELECT l.order_handle, count(*) FILTER (WHERE l.outstanding_quantity > 0) AS owed, "
        "count(*) FILTER (WHERE l.outstanding_quantity = 0) AS done, "
        "array_agg(DISTINCT l.access_class) AS classes, bool_and(l.ai_ok) AS ai_ok "
        "FROM {s}.sales_order_lines l WHERE l.order_handle IN (SELECT handle FROM o) GROUP BY 1), "
        "j AS (SELECT r.*, CASE "
        "WHEN r.of_record IS FALSE AND r.systems <@ {p[retiring]} AND (touch.touched IS NULL "
        "OR touch.touched < {p[day]}::date - {p[active]}) THEN 'retiring_source_untouched' "
        "WHEN r.handle IN (SELECT handle FROM twin) THEN 'copy_of_order_of_record' "
        "WHEN greatest(r.order_date, r.required_date, inv.last_invoice) "
        "< {p[day]}::date - {p[active]} THEN 'historic' "
        "WHEN greatest(r.order_date, r.required_date, inv.last_invoice) IS NULL "
        "AND r.of_record IS FALSE THEN 'undated_copy' "
        "WHEN coalesce(ln.owed, 0) = 0 AND coalesce(ln.done, 0) > 0 THEN 'nothing_outstanding' "
        "WHEN coalesce(ln.owed, 0) = 0 AND inv.last_invoice IS NOT NULL THEN 'invoiced' "
        "END AS skip, ARRAY(SELECT DISTINCT c FROM unnest(ARRAY[r.access_class] "
        "|| coalesce(inv.classes, '{{}}') || coalesce(ln.classes, '{{}}')) c "
        "WHERE c IS NOT NULL) AS needs, "
        "r.ai_ok AND coalesce(inv.ai_ok, true) AND coalesce(ln.ai_ok, true) AS all_ai "
        "FROM o r LEFT JOIN inv USING (order_number) LEFT JOIN ln ON ln.order_handle = r.handle "
        "LEFT JOIN touch ON touch.handle = r.handle) ")


_ORDER_JUDGED = _order_judged("late")
_ORDER_PARAMS = {"active": ACTIVE_DAYS, "successor": list(SUCCESSOR_SYSTEMS),
                 "retiring": list(RETIRING_SYSTEMS), "touched": list(RETIRING_TOUCHED),
                 "created": list(RETIRING_CREATED), "twin": TWIN_DAYS}
_ORDER_SIDE = ("{s}.lineage", "ontology.object_values")
# The customer's usual slack: days from the required date to the first invoice of each of
# its dated closed orders of record (required date inside USUAL_HISTORY_DAYS), its
# SLACK_SHARE percentile with at least USUAL_MIN_ORDERS of them, else every customer's.
# Those history rows' classes are among ``needs`` when the customer's own slack is used.
_SLACK = (
    ", slip AS (SELECT g.customer_handle AS cust, g.access_class AS gc, "
    "min(i.invoice_date) - g.required_date AS d, array_agg(DISTINCT i.access_class) AS ic "
    "FROM {s}.sales_orders g JOIN {s}.sales_invoices i ON i.order_number = g.order_number "
    "AND i.pilot IS NOT TRUE WHERE g.status = 'closed' AND g.of_record IS NOT FALSE "
    "AND g.required_date > {p[day]}::date - {p[history]} AND g.required_date <= {p[day]}::date "
    "GROUP BY g.handle, g.customer_handle, g.access_class, g.required_date), "
    "cu AS (SELECT cust, count(*) AS n, percentile_disc({p[share]}::float8) WITHIN GROUP "
    "(ORDER BY d) AS slack FROM slip WHERE cust IS NOT NULL GROUP BY cust), "
    "cc AS (SELECT cust, array_agg(DISTINCT c) FILTER (WHERE c IS NOT NULL) AS classes "
    "FROM slip CROSS JOIN LATERAL unnest(ic || gc) c WHERE cust IS NOT NULL GROUP BY cust), "
    "al AS (SELECT percentile_disc({p[share]}::float8) WITHIN GROUP (ORDER BY d) AS slack, "
    "count(*) AS n FROM slip), "
    "k AS (SELECT r.*, coalesce(cu.n >= {p[min_orders]}, false) AS own, "
    "CASE WHEN cu.n >= {p[min_orders]} THEN cu.slack ELSE al.slack END AS slack, "
    "CASE WHEN cu.n >= {p[min_orders]} THEN cu.n ELSE al.n END AS basis, "
    "greatest({p[min_slack]}, CASE WHEN cu.n >= {p[min_orders]} THEN cu.slack "
    "ELSE al.slack END) AS allowed, "
    "CASE WHEN cu.n >= {p[min_orders]} THEN r.needs || cc.classes ELSE r.needs END "
    "AS all_needs FROM j r LEFT JOIN cu ON cu.cust = r.customer_handle "
    "LEFT JOIN cc ON cc.cust = r.customer_handle, al WHERE r.skip IS NULL) ")
# "Late customer order" has ONE meaning everywhere (measure ``late_orders``): open and past its
# required date, of record. This is a different signal: past it by MORE than the customer's
# orders usually are. It is never called late: its own type and kind.
SLOWER_ORDER = Rule(
    "customer_order_slower_than_usual", "customer_order_slower",
    "An open customer order later than its customer's orders usually are: past its required "
    "date by more days than 75% of the customer's dated orders of the last two years were "
    "at their first invoice (with fewer than 10, every customer's), and never under 3 days; "
    "still live: dated or invoiced within the last 180 days, and something still "
    "outstanding (a line owed, or no invoice and no line saying nothing is owed).",
    "sales_orders", ("sales_orders", "sales_invoices", "sales_order_lines"),
    _ORDER_JUDGED + _SLACK +
    "SELECT r.handle, '', coalesce(r.salesperson, ''), false, NULL::text, "
    "concat_ws(' ', 'Order', coalesce(r.order_number, 'with no order number'), 'for', "
    "coalesce(r.customer, 'an unnamed customer'), 'is', r.days_late, CASE WHEN r.days_late = 1 THEN 'day' ELSE 'days' END, "
    "'past its required date of', to_char(r.required_date, 'Mon DD, YYYY') || ';', "
    "CASE WHEN r.own THEN 'this customer''s orders' ELSE 'orders' END, 'usually ship', "
    "CASE WHEN r.slack <= 0 THEN 'by their required date' WHEN r.slack = 1 THEN "
    "'within 1 day of it' ELSE 'within ' || r.slack || ' days of it' END, "
    "'(75% of', r.basis, CASE WHEN r.own THEN 'of its dated orders' ELSE 'dated orders' END, "
    "'over the last two years).'), "
    "(r.required_date + r.allowed + 1)::timestamptz, r.access_class, "
    "ARRAY(SELECT DISTINCT c FROM unnest(r.all_needs) c), r.all_ai, "
    "ARRAY[r.col_class]::smallint[], ARRAY[r.handle]::text[] FROM k r WHERE r.slack IS NOT NULL AND r.days_late > r.allowed",
    "the order ships, closes or is cancelled, its required date moves, or it goes 180 days "
    "with no date or invoice",
    "late_customer_order (customer_order_late) with active_population.inactive_reason, judged against this customer's usual slack",
    shows=("order_number", "customer", "days_late", "required_date"),
    params={"grace": LATE_GRACE_DAYS, **_ORDER_PARAMS, "share": SLACK_SHARE,
            "history": USUAL_HISTORY_DAYS, "min_orders": USUAL_MIN_ORDERS,
            "min_slack": SLACK_MIN_DAYS}, side=_ORDER_SIDE,
    skips=_ORDER_JUDGED + _SLACK + "SELECT skip, count(*) FROM j WHERE skip IS NOT NULL "
    "GROUP BY 1 UNION ALL SELECT CASE WHEN slack IS NULL THEN 'no_slack_history' ELSE "
    "'within_usual_slack' END, count(*) FROM k WHERE slack IS NULL OR days_late <= allowed "
    "GROUP BY 1")

LATE_ORDER = Rule(
    "customer_order_late", "customer_order_late",
    "An open customer order past its required date and still going: exactly the orders the "
    "measure late_orders counts (its where, never a copy of it).",
    "sales_orders", ("sales_orders",),
    "SELECT r.handle, '', coalesce(r.salesperson, ''), false, NULL::text, "
    "concat_ws(' ', 'Order', coalesce(r.order_number, 'with no order number'), 'for', "
    "coalesce(r.customer, 'an unnamed customer'), 'is', r.days_late, "
    "CASE WHEN r.days_late = 1 THEN 'day' ELSE 'days' END, 'past its required date of', "
    "to_char(r.required_date, 'Mon DD, YYYY') || '.'), "
    "(r.required_date + 1)::timestamptz, r.access_class, "
    "ARRAY(SELECT DISTINCT c FROM unnest(ARRAY[r.access_class]) c WHERE c IS NOT NULL), "
    "r.ai_ok, ARRAY[r.col_class]::smallint[], ARRAY[r.handle]::text[] "
    "FROM {s}.sales_orders r WHERE " + measure_where("late_orders").replace(
        "to_jsonb(sales_orders)", "to_jsonb(r)"),
    "the order ships, closes or is cancelled, its required date moves, or it stops being live "
    "(nothing left to ship, or no work on it for longer than closed orders take)",
    "measure late_orders (operating/measures.json)",
    shows=("order_number", "customer", "days_late", "required_date"), measure="late_orders")

# Open longer than usual: an order with no promised date is never "late", so an ASAP order
# is not silently dropped: past the customer's own usual order-to-first-invoice time (its
# 90th percentile over USUAL_HISTORY_DAYS of closed orders of record, with at least
# USUAL_MIN_ORDERS), else every customer's. The customer's history rows are among ``needs``
# (the number shown rests on them); the all-customer percentile is a population statistic
# (as the price rules' medians). An order with no order day cannot be aged: counted.
_UNDATED = _order_judged("undated")
_USUAL = (
    ", ship AS (SELECT g.customer_handle AS cust, g.access_class AS gc, "
    "min(i.invoice_date) - g.order_date AS d, array_agg(DISTINCT i.access_class) AS ic "
    "FROM {s}.sales_orders g JOIN {s}.sales_invoices i ON i.order_number = g.order_number "
    "AND i.pilot IS NOT TRUE WHERE g.status = 'closed' AND g.of_record IS NOT FALSE "
    "AND g.order_date > {p[day]}::date - {p[history]} AND g.order_date <= {p[day]}::date "
    "GROUP BY g.handle, g.customer_handle, g.access_class, g.order_date), "
    "cu AS (SELECT cust, count(*) AS n, percentile_disc({p[share]}::float8) WITHIN GROUP "
    "(ORDER BY d) AS usual FROM ship WHERE d >= 0 AND cust IS NOT NULL GROUP BY cust), "
    "cc AS (SELECT cust, array_agg(DISTINCT c) FILTER (WHERE c IS NOT NULL) AS classes "
    "FROM ship CROSS JOIN LATERAL unnest(ic || gc) c WHERE d >= 0 AND cust IS NOT NULL "
    "GROUP BY cust), "
    "al AS (SELECT percentile_disc({p[share]}::float8) WITHIN GROUP (ORDER BY d) AS usual, "
    "count(*) AS n FROM ship WHERE d >= 0), "
    "k AS (SELECT r.*, {p[day]}::date - r.order_date AS age, "
    "CASE WHEN cu.n >= {p[min_orders]} THEN cu.usual ELSE al.usual END AS usual, "
    "cu.n >= {p[min_orders]} AS own, CASE WHEN cu.n >= {p[min_orders]} THEN cu.n "
    "ELSE al.n END AS basis, CASE WHEN cu.n >= {p[min_orders]} THEN r.needs || cc.classes "
    "ELSE r.needs END AS all_needs FROM j r LEFT JOIN cu ON cu.cust = r.customer_handle "
    "LEFT JOIN cc ON cc.cust = r.customer_handle, al "
    "WHERE r.skip IS NULL AND r.order_date IS NOT NULL) ")
OPEN_LONG = Rule(
    "customer_order_open_long", "customer_order_slower",
    "An open customer order with no promised date (none given, or only the order's own day, "
    "the entry default), still live, open longer than the customer's usual time from order "
    "to first invoice (90% of its orders of the last two years shipped sooner; with fewer "
    "than 10 such orders, every customer's).",
    "sales_orders", ("sales_orders", "sales_invoices", "sales_order_lines"),
    _UNDATED + _USUAL +
    "SELECT r.handle, '', coalesce(r.salesperson, ''), false, NULL::text, "
    "concat_ws(' ', 'Order', r.order_number, 'for', coalesce(r.customer, 'an unnamed customer'), "
    "'has been open', r.age, 'days with no promised date;', CASE WHEN r.own THEN "
    "'this customer''s orders' ELSE 'orders' END, 'usually ship within', r.usual, "
    "'days (90% of', r.basis, CASE WHEN r.own THEN 'of its orders' ELSE 'orders' END, "
    "'over the last two years).'), "
    "(r.order_date + r.usual + 1)::timestamptz, r.access_class, "
    "ARRAY(SELECT DISTINCT c FROM unnest(r.all_needs) c), r.all_ai, "
    "ARRAY[r.col_class]::smallint[], ARRAY[r.handle]::text[] FROM k r WHERE r.usual IS NOT NULL AND r.age > r.usual",
    "the order ships, closes or is cancelled, gets a promised date, or goes 180 days with no "
    "date or invoice",
    "none (new; the late-order rule's live tests, decision 2026-11-18)",
    shows=("order_number", "customer", "order_date"),
    params={"grace": LATE_GRACE_DAYS, **_ORDER_PARAMS, "share": USUAL_SHARE,
            "history": USUAL_HISTORY_DAYS, "min_orders": USUAL_MIN_ORDERS},
    side=_ORDER_SIDE,
    skips=_UNDATED + "SELECT CASE WHEN skip IS NULL THEN 'no_order_date' ELSE skip END, "
    "count(*) FROM j WHERE skip IS NOT NULL OR order_date IS NULL GROUP BY 1")

# A batch on quality hold (stock_state.batch_hold): a hold word, not closed, a known state;
# the hold word read from the batch's NEWEST copy: when its
# copies disagree (Portal batch record vs the FileMaker mirror: 15 batches on 11-18, every
# one the Portal saying On-Hold and the newer FileMaker copy Released) the copy whose
# quality status was recorded last wins; a tie keeps the hold (fail toward showing it).
# Whether a hold is its state's default is still read from the ranked copy (the batch
# record the default comes from): one copy releasing a batch does not make the default
# hold of every other batch in that state an alert.
# a made batch on hold is always a hold; an unmade one only when the hold is not what a
# bulk share (STATE_SHARE of at least STATE_MINIMUM) of the batches in its state carry.
LOT_ON_HOLD = Rule(
    "lot_on_hold", "quality_hold",
    "A batch Quality holds: its QC status is a hold word, it is not cancelled, it was made "
    "within the last year (or is not made yet), and the hold "
    "is not the default every unmade batch in its state carries.",
    "batches", ("batches",),
    "WITH cp AS (SELECT DISTINCT ON (lg.handle) lg.handle, v.value AS qs FROM {s}.lineage lg "
    "CROSS JOIN LATERAL unnest(lg.object_ids) x(oid) JOIN ontology.object_values v "
    "ON v.object_id = x.oid AND v.superseded_at IS NULL AND v.property = 'quality_status' "
    "WHERE lg.view = 'batches' AND lg.copies > 1 AND v.value IS NOT NULL "
    "ORDER BY lg.handle, v.recorded_at::timestamptz DESC, "
    "lower(btrim(v.value)) = ANY({p[hold]}) DESC), "
    "b AS (SELECT r.*, lower(btrim(r.status)) AS said, "
    "lower(btrim(coalesce(cp.qs, r.quality_status))) = ANY({p[hold]}) AS held, "
    "lower(btrim(r.quality_status)) = ANY({p[hold]}) AS ranked_held "
    "FROM {s}.batches r LEFT JOIN cp ON cp.handle = r.handle), "
    "st AS (SELECT said, count(*) AS n, count(*) FILTER (WHERE ranked_held) AS h FROM b "
    "GROUP BY said) "
    "SELECT r.handle, '', '', false, NULL::text, "
    "concat_ws(' ', 'Batch', coalesce(r.batch_code, r.lot_number), "
    "CASE WHEN r.product IS NOT NULL THEN '(' || r.product || ')' END, "
    "'is on quality hold' || CASE WHEN r.realized_date IS NOT NULL THEN ' since it was made on ' "
    "|| to_char(r.realized_date, 'Mon DD, YYYY') ELSE '' END || '.'), "
    "r.realized_date::timestamptz, " + ONE + " "
    "FROM b r JOIN st ON st.said = r.said WHERE r.held AND r.said <> ALL({p[closed]}) "
    "AND (r.realized_date IS NULL OR r.realized_date >= {p[day]}::date - {p[stale]}) "
    "AND (r.said = ANY({p[made]}) OR (r.said = ANY({p[active]}) AND st.n >= {p[minimum]} "
    "AND st.h < st.n * {p[share]}::numeric))",
    "Quality releases or rejects the batch (on its newest copy), it is cancelled, or it "
    "was made more than a year ago",
    "lot_attention (lot_on_hold, stock_state.batch_hold)", side=("{s}.lineage",
                                                                "ontology.object_values"),
    shows=("batch_code", "lot_number", "product", "quality_status", "realized_date"),
    params={"hold": list(HOLD_WORDS), "closed": list(CLOSED_BATCH), "made": list(MADE_BATCH),
            "active": list(ACTIVE_BATCH), "minimum": STATE_MINIMUM, "share": STATE_SHARE,
            "stale": HOLD_STALE_DAYS})

# The price rules' lines (proactive_prices): posted Sage lines of record in the window with a
# positive quantity in a unit, a positive revenue in a currency, an item code. A series is
# one item, one unit, one currency (never converted, never mixed); a customer is its
# customers row (one per real party).
_LINES = (
    "lines AS (SELECT l.customer_handle AS cust, l.customer, l.item_code AS item, "
    "coalesce(l.item_name, l.item_code) AS item_name, l.quantity_unit AS unit, "
    "l.revenue_currency AS cur, l.quantity AS q, l.revenue AS amt, l.invoice_date AS day, "
    "CASE WHEN l.margin_basis IN ('same_currency', 'cost_converted_at_invoice_rate') "
    "AND NOT " + COST_LOOKS_WRONG_SQL + " THEN l.revenue - l.margin END AS floor, "
    "l.access_class, l.ai_ok, l.col_class, l.handle FROM {s}.sales_invoice_lines l "
    "WHERE l.pilot IS NOT TRUE AND l.customer_handle IS NOT NULL AND l.item_code IS NOT NULL "
    "AND l.quantity > 0 AND l.quantity_unit IS NOT NULL AND l.revenue > 0 "
    "AND l.revenue_currency IS NOT NULL AND l.invoice_date > {p[day]}::date - {p[days]} "
    "AND l.invoice_date <= {p[day]}::date), "
    "paid AS (SELECT cust, max(customer) AS customer, item, max(item_name) AS item_name, unit, "
    "cur, sum(amt) / sum(q) AS price, sum(q) AS q, sum(amt) FILTER (WHERE floor IS NOT NULL) "
    "AS f_amt, sum(floor) AS f_cost, array_agg(DISTINCT access_class) AS classes, "
    "bool_and(ai_ok) AS ai_ok, array_agg(DISTINCT col_class) AS cols, "
    "array_agg(DISTINCT handle) AS srcs FROM lines GROUP BY cust, item, unit, cur)")
# what every price sentence shows of the lines besides the price: who, which item, its unit
# and currency, and the quantities the price is weighted by
_PRICE_SHOWS = ("customer", "item_code", "item_name", "quantity", "quantity_unit",
                "revenue_currency")
_PRICE_OUT = ("SELECT c.handle, x.item || '|' || x.unit || '|' || x.cur, '', false, NULL::text, "
              "{why}, NULL::timestamptz, c.access_class, "
              "ARRAY(SELECT DISTINCT unnest(x.classes || c.access_class)), "
              "x.ai_ok AND c.ai_ok, x.cols, x.srcs ")

PRICE_BELOW_COST = Rule(
    "price_below_cost", "margin_loss",
    "A customer paying under 95% of the cost booked on its own invoice lines for one item "
    "over the last 180 days (quantity-weighted, one unit and currency).",
    "customers", ("sales_invoice_lines", "customers"),
    "WITH " + _LINES + " " + _PRICE_OUT.format(
        why="concat_ws(' ', x.customer, 'paid', round(x.f_amt / nullif(x.f_cost, 0) * 100) "
            "|| '% of cost for', x.item_name, '(' || x.cur || ' per ' || x.unit || ') over the "
            "last {p[days]} days.')") +
    "FROM paid x JOIN {s}.customers c ON c.handle = x.cust "
    "WHERE x.f_cost > 0 AND x.f_amt < x.f_cost * {p[tolerance]}::numeric",
    "the customer's price over the window comes back to cost, or the lines leave the window",
    "price_consistency (price_below_cost)",
    shows=("revenue", "cost", "margin", "unit_price", "unit_cost") + _PRICE_SHOWS,
    cols_of="sales_invoice_lines",
    params={"days": PRICE_DAYS, "tolerance": COST_TOLERANCE}, impact="below")

# far below typical: the other customers' lines of the series, weighted by quantity, give a
# median and 10th percentile; with MIN_CUSTOMERS others, a price under that percentile AND
# BELOW_TYPICAL under the median, unless a tenth of the others pay as little; a customer
# with fewer than MIN_OTHER_PRODUCTS other priced products has no known standing; below
# cost wins for the same series.
PRICE_BELOW_TYPICAL = Rule(
    "price_below_typical", "price_exception",
    "A customer paying far below what the other customers pay for the same item, unit and "
    "currency over the last 180 days.",
    "customers", ("sales_invoice_lines", "customers"),
    "WITH " + _LINES + ", "
    "peer AS (SELECT x.cust, x.item, x.unit, x.cur, o.cust AS other, o.price AS p, o.q AS w, "
    "o.classes FROM paid x JOIN paid o ON o.item = x.item AND o.unit = x.unit AND o.cur = x.cur "
    "AND o.cust <> x.cust), "
    "cum AS (SELECT *, sum(w) OVER (PARTITION BY cust, item, unit, cur ORDER BY p, other) AS run, "
    "sum(w) OVER (PARTITION BY cust, item, unit, cur) AS tot FROM peer), "
    "typ AS (SELECT cust, item, unit, cur, count(DISTINCT other) AS others, "
    "min(p) FILTER (WHERE run >= tot * 0.5) AS median, "
    "min(p) FILTER (WHERE run >= tot * {p[low]}::numeric) AS low, "
    "array_agg(DISTINCT cl) AS classes FROM cum, unnest(classes) cl GROUP BY 1, 2, 3, 4), "
    "standing AS (SELECT cust, count(*) - 1 AS others FROM paid GROUP BY cust) "
    + _PRICE_OUT.format(
        why="concat_ws(' ', x.customer, 'paid', round((1 - x.price / t.median) * 100), "
            "'% below the usual price for', x.item_name, '(' || x.cur || ' per ' || x.unit || "
            "') over the last {p[days]} days.')").replace(
        "x.classes || c.access_class", "x.classes || t.classes || c.access_class") +
    "FROM paid x JOIN typ t USING (cust, item, unit, cur) JOIN standing sd USING (cust) "
    "JOIN {s}.customers c ON c.handle = x.cust "
    "WHERE t.others >= {p[min_customers]} AND sd.others >= {p[min_products]} "
    "AND x.price < t.low AND x.price <= t.median * (1 - {p[below]}::numeric) "
    "AND (SELECT count(*) FROM paid o WHERE o.item = x.item AND o.unit = x.unit "
    "AND o.cur = x.cur AND o.cust <> x.cust AND o.price <= x.price) < t.others * {p[low]}::numeric "
    "AND NOT (x.f_cost > 0 AND x.f_amt < x.f_cost * {p[tolerance]}::numeric)",
    "the customer's price comes back near the others', or the lines leave the window",
    "price_consistency (price_below_typical)",
    shows=("revenue", "unit_price") + _PRICE_SHOWS, cols_of="sales_invoice_lines",
    params={"days": PRICE_DAYS, "low": LOW_SHARE, "below": BELOW_TYPICAL,
            "min_customers": MIN_CUSTOMERS, "min_products": MIN_OTHER_PRODUCTS,
            "tolerance": COST_TOLERANCE})

CUSTOMER_PRICE_DROP = Rule(
    "customer_price_drop", "price_exception",
    "A customer's own price for one item, unit and currency down 10% or more against the "
    "180 days before (an item mostly booked at no cost, a service or miscellaneous line, is "
    "not judged).",
    "customers", ("sales_invoice_lines", "customers"),
    "WITH " + _LINES + ", "
    + _LINES.replace("lines AS", "prior AS", 1).replace("paid AS", "was AS", 1)
    .replace("FROM lines GROUP", "FROM prior GROUP")
    .replace("{p[day]}::date - {p[days]} AND l.invoice_date <= {p[day]}::date",
             "{p[day]}::date - 2 * {p[days]} AND l.invoice_date <= {p[day]}::date - {p[days]}")
    + " " + _PRICE_OUT.format(
        why="concat_ws(' ', x.customer || '''s price for', x.item_name, 'dropped', "
            "round((1 - x.price / w.price) * 100), '% (' || x.cur || ' per ' || x.unit || "
            "') against the {p[days]} days before.')").replace(
        "x.classes || c.access_class", "x.classes || w.classes || c.access_class").replace(
        "x.cols, x.srcs", "x.cols || w.cols, x.srcs || w.srcs") +
    "FROM paid x JOIN was w USING (cust, item, unit, cur) "
    # a code its system mostly books at no cost (labour hours, a miscellaneous non-stock
    # line) prices different things under one code: its "price" has no meaning to drop
    "JOIN (SELECT item, unit, cur FROM lines GROUP BY 1, 2, 3 "
    "HAVING count(*) FILTER (WHERE floor > 0) * 2 >= count(*)) k USING (item, unit, cur) "
    "JOIN {s}.customers c ON c.handle = x.cust "
    "WHERE x.price <= w.price * (1 - {p[drop]}::numeric)",
    "the customer's price comes back, or the lines leave the window",
    "price_consistency (customer_price_drop)",
    shows=("revenue", "unit_price") + _PRICE_SHOWS, cols_of="sales_invoice_lines",
    params={"days": PRICE_DAYS, "drop": OWN_DROP},
    impact="q * prior_price - rev")

# per customer and currency, recent order value against the baseline run-rate (scaled to the
# same days); only customers with DEMAND_MIN_ORDERS baseline orders and a baseline value at
# least the median of every customer's in that currency; orders open or closed (no draft,
# no unknown status, no quote), of record; a source whose newest order is older than
# SOURCE_CURRENT_DAYS judges nobody.
# Not judged (``proactive_ceo.customer_demand_drop``'s skips): a customer with an order in
# the windows of no known value or currency (its sum would be short by that order: a drop
# that is not one; 11-18 parity: one such customer fired on 12 recent unvalued orders), one
# with an order of unknown status dated in the recent window, or one ordering in two
# currencies. A value of 0 is unknown too (``proactive_ceo._positive``: a header zeroed once
# invoiced, nothing pricing it; 11-18 parity: two customers fired on such an order, one of
# them with nothing else in its recent window). Each counted by reason (``skips``).
# A stale open order (open, no order day, required day or invoice billing it within
# ACTIVE_DAYS: the late-order rule's 'historic') is not demand:
# such headers sit open in Sage for months uninvoiced and would swell the
# usual level (11-18: one customer's baseline was 94% two such orders). Counted per order.
_DEMAND_JUDGED = (
    "WITH a AS (SELECT r.customer_handle AS cust, r.value_currency AS cur, r.value, r.status, "
    "r.order_date AS day, r.access_class, r.ai_ok, r.col_class, r.handle, r.status = 'open' AND "
    "greatest(r.order_date, r.required_date, (SELECT max(i.invoice_date) FROM "
    "{s}.sales_invoices i WHERE i.order_number = r.order_number AND i.invoice_date <= "
    "{p[day]}::date)) < {p[day]}::date - {p[active]} AS stale FROM {s}.sales_orders r "
    "WHERE r.customer_handle IS NOT NULL AND r.status IN ('open', 'closed', 'unknown') "
    "AND r.of_record IS NOT FALSE AND coalesce(r.order_type, 'order') <> 'quote' "
    "AND r.order_date > {p[day]}::date - {p[recent]} - {p[base]} "
    "AND r.order_date <= {p[day]}::date), "
    "w AS (SELECT * FROM a WHERE NOT stale), "
    "unjudged AS (SELECT cust, CASE "
    "WHEN bool_or(status <> 'unknown' AND (value IS NULL OR value <= 0 OR cur IS NULL)) "
    "THEN 'order_value_unknown' "
    "WHEN bool_or(status = 'unknown' AND day > {p[day]}::date - {p[recent]}) "
    "THEN 'order_status_unknown' "
    "WHEN count(DISTINCT cur) > 1 THEN 'currencies_mixed' END AS skip FROM w GROUP BY cust), "
    "o AS (SELECT w.* FROM w JOIN unjudged u USING (cust) WHERE u.skip IS NULL "
    "AND w.status <> 'unknown'), ")
DEMAND_DROP = Rule(
    "customer_demand_drop", "weekly_trends",
    "A large customer whose orders over the last 90 days are under half its usual run-rate "
    "over the year before.",
    "customers", ("sales_orders", "customers"),
    _DEMAND_JUDGED +
    "src AS (SELECT max(day) AS newest FROM w), "
    "per AS (SELECT cust, cur, "
    "sum(value) FILTER (WHERE day > {p[day]}::date - {p[recent]}) AS recent, "
    "sum(value) FILTER (WHERE day <= {p[day]}::date - {p[recent]}) AS base, "
    "count(*) FILTER (WHERE day <= {p[day]}::date - {p[recent]}) AS base_orders, "
    "array_agg(DISTINCT access_class) AS classes, bool_and(ai_ok) AS ai_ok, "
    "array_agg(DISTINCT col_class) AS cols, array_agg(DISTINCT handle) AS srcs "
    "FROM o GROUP BY cust, cur), "
    # the size floor over every customer's valued baseline, the unjudged ones too
    "allb AS (SELECT cust, cur, sum(value) AS base FROM w WHERE status <> 'unknown' "
    "AND value IS NOT NULL AND cur IS NOT NULL AND day <= {p[day]}::date - {p[recent]} "
    "GROUP BY 1, 2), "
    "med AS (SELECT cur, percentile_cont(0.5) WITHIN GROUP (ORDER BY base) AS m FROM allb "
    "GROUP BY cur) "
    "SELECT c.handle, x.cur, '', false, NULL::text, concat_ws(' ', c.name, 'ordered', "
    "x.cur, to_char(coalesce(x.recent, 0), 'FM999,999,999,990.00'), 'in the last {p[recent]} "
    "days against a usual', x.cur, to_char(x.base * {p[recent]} / {p[base]}, "
    "'FM999,999,999,990.00'), 'for that time.'), NULL::timestamptz, c.access_class, "
    "ARRAY(SELECT DISTINCT unnest(x.classes || c.access_class)), x.ai_ok AND c.ai_ok, x.cols, "
    "x.srcs FROM per x JOIN med USING (cur) JOIN {s}.customers c ON c.handle = x.cust, src "
    "WHERE src.newest > {p[day]}::date - {p[current]} AND x.base_orders >= {p[min_orders]} "
    "AND x.base >= med.m AND coalesce(x.recent, 0) < x.base * {p[recent]} / {p[base]} "
    "* {p[share]}::numeric",
    "the customer's recent orders come back above half its run-rate",
    "customer_demand_drop (proactive_ceo)",
    shows=("value", "value_currency"), cols_of="sales_orders", record_shows=("name",),
    params={"recent": DEMAND_RECENT_DAYS, "base": DEMAND_BASELINE_DAYS,
            "min_orders": DEMAND_MIN_ORDERS, "share": DEMAND_DROP_SHARE,
            "current": SOURCE_CURRENT_DAYS, "active": ACTIVE_DAYS},
    skips=_DEMAND_JUDGED + "z AS (SELECT 1) SELECT skip, count(*) FROM unjudged "
    "WHERE skip IS NOT NULL GROUP BY 1 UNION ALL SELECT 'stale_open_order', count(*) "
    "FROM a WHERE stale HAVING count(*) > 0")

# A message waiting for its mailbox owner's reply, per (mailbox, thread): the thread's first
# message since the owner last wrote in it that is not bulk (``is_bulk``: automated, list or
# marketing mail, decided at the emails build), comes from someone else (``from_owner``
# false) with an address, names the owner on To (``owner_role``: over ALL the person's
# addresses, not only the mailbox's), has at least one To address, sits in the person's
# own mailbox (a shared mailbox is a team queue, not a person waiting) and arrived inside
# REPLY_DAYS. It shows from arrival (no waiting period: the page orders by age) and resolves
# the moment the owner writes in the thread (``owner_replied_at`` after it). No text test,
# no model and nothing per sender. ``private``: only the mailbox owner sees it, and only a
# reader holding the message row's unit (the comms door's own rule).
# Each test a message must pass to be owed a reply, by name (the evaluation,
# ``operating.pg.reply_eval``, reports a miss by the first test it fails).
OWED_TESTS: tuple[tuple[str, str], ...] = (
    ("withheld", "e.excluded IS FALSE"),
    ("own mailbox", "e.mailbox_shared IS FALSE AND e.mailbox IS NOT NULL AND e.mailbox <> ''"),
    ("from someone else", "e.from_owner IS FALSE AND e.sender ~ '.@.'"),
    ("bulk", "e.is_bulk IS FALSE"),
    ("on To", "e.owner_role = 'to'"),
    ("To list", "cardinality(e.to_addresses) >= 1"),
    ("known to you", "e.wrote_to_at < e.received_at OR e.wrote_in_at < e.received_at"),
)
OWED = " AND ".join(f"({sql})" for _, sql in OWED_TESTS)
# The emails with ``from_owner`` (the sender is one of the mailbox owner's addresses:
# ``OWNERS``, the mail capture's UPN and proxy addresses, as the emails build reads them for
# ``owner_role``) and what "known to you" reads: when the mailbox owner first wrote to the
# sender (on To or Cc of a message they sent from this mailbox) and first wrote in the
# thread. Measured on Vince's mail (reply_eval, 30 days): without it 73% of the flagged
# threads were never answered (cold sales, newsletters a bulk test misses); with it 52%,
# recall 88% -> 84%.
OWNERS = "al_owner"      # temp table (mailbox, addr): ``owner_table``
OWED_FROM = (
    "(SELECT e.*, (ow.addr IS NOT NULL) AS from_owner, k.first_at AS wrote_to_at, "
    "w.first_at AS wrote_in_at FROM {s}.emails e "
    "LEFT JOIN " + OWNERS + " ow ON ow.mailbox = e.mailbox AND ow.addr = e.sender "
    "LEFT JOIN (SELECT o.mailbox, a.addr, min(coalesce(o.sent_at, o.received_at)) AS first_at "
    "FROM {s}.emails o JOIN " + OWNERS + " oo ON oo.mailbox = o.mailbox AND oo.addr = o.sender "
    "CROSS JOIN LATERAL unnest(o.to_addresses || o.cc_addresses) a(addr) "
    "GROUP BY 1, 2) k ON k.mailbox = e.mailbox AND k.addr = e.sender "
    "LEFT JOIN (SELECT o.mailbox, o.thread, min(coalesce(o.sent_at, o.received_at)) AS first_at "
    "FROM {s}.emails o JOIN " + OWNERS + " oo ON oo.mailbox = o.mailbox AND oo.addr = o.sender "
    "WHERE o.thread IS NOT NULL GROUP BY 1, 2) w "
    "ON w.mailbox = e.mailbox AND w.thread = e.thread) e")


def owner_table(conn: Any, owners: Mapping[str, Sequence[str]]) -> int:
    """``OWNERS`` for this session: every (mailbox id, address) of ``owners`` (lower-case),
    replacing what it held. Returns the rows."""
    conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {OWNERS} (mailbox text, addr text, "
                 "PRIMARY KEY (mailbox, addr))")
    conn.execute(f"TRUNCATE {OWNERS}")
    rows = sorted({(str(m).strip().lower(), str(a).strip().lower())
                   for m, addrs in owners.items() for a in addrs if a and m})
    with conn.cursor() as cur:
        cur.executemany(f"INSERT INTO {OWNERS} (mailbox, addr) VALUES (%s, %s)", rows)
    conn.execute(f"ANALYZE {OWNERS}")
    return len(rows)


_WAITING = (
    "SELECT e.handle, e.mailbox, e.thread, e.sender, e.subject, e.received_at, e.unit, "
    "e.ai_ok, count(*) OVER w AS waiting, max(e.received_at) OVER w AS newest, "
    "array_agg(e.unit) OVER w AS units, bool_and(e.ai_ok) OVER w AS all_ai, "
    "row_number() OVER (PARTITION BY e.mailbox, coalesce(e.thread, e.handle) "
    "ORDER BY e.received_at, e.handle) AS nth "
    "FROM " + OWED_FROM + " WHERE " + OWED + " "
    "AND e.received_at >= {p[day]}::date - {p[days]} AND e.received_at < {p[day]}::date + 1 "
    "AND (e.owner_replied_at IS NULL OR e.owner_replied_at < e.received_at) "
    "WINDOW w AS (PARTITION BY e.mailbox, coalesce(e.thread, e.handle))")
NEEDS_MY_REPLY = Rule(
    "needs_my_reply", "reply_owed",
    "A message to you (on To) from someone you have written to before (or in a thread you "
    "wrote in), not bulk, in your own mailbox in the last 60 days, with no reply from you "
    "in its thread since it came.",
    "emails", ("emails",),
    "SELECT x.handle, '', x.mailbox, true, x.mailbox, "
    "concat_ws(' ', x.sender, 'wrote', '\"' || left(coalesce(nullif(btrim(x.subject), ''), "
    "'(no subject)'), 90) || '\"', 'on', to_char(x.received_at AT TIME ZONE '"
    + bv.BUSINESS_TZ + "', 'Mon DD') || CASE WHEN x.waiting > 1 THEN ', and ' || "
    "(x.waiting - 1) || ' more since,' ELSE '' END, 'with no reply from you in the "
    "thread yet.'), x.received_at, NULL::smallint, '{{}}'::smallint[], x.all_ai, "
    "'{{}}'::smallint[], '{{}}'::text[], x.units FROM (" + _WAITING + ") x WHERE x.nth = 1",
    "the mailbox owner writes in the thread, or the message leaves the 60-day window",
    "inbox_triage needs_my_reply (proactive_commitments reply_owed)",
    params={"days": REPLY_DAYS}, comms=True)

RULES: dict[str, Rule] = {r.name: r for r in (
    LATE_PO, DUE_PO, UNDATED_PO, NEGATIVE_MARGIN, ETA_SLIPPED, VENDOR_NO_REPLY, AT_RISK, LATE_ORDER, SLOWER_ORDER, OPEN_LONG, LOT_ON_HOLD, PRICE_BELOW_COST,
    PRICE_BELOW_TYPICAL,
    CUSTOMER_PRICE_DROP, DEMAND_DROP, NEEDS_MY_REPLY)}


# --------------------------------------------------------------------------- the table
def _ddl(s: str) -> list[str]:
    """Migration 0044_alerts then 0045_alerts_read_time, for schema ``s`` (a test holds
    them equal)."""
    return [
        f"CREATE TABLE IF NOT EXISTS {s}.alerts ("
        "rule text NOT NULL, kind text NOT NULL, record_table text NOT NULL, "
        "handle text NOT NULL, topic text NOT NULL DEFAULT '', concerns text NOT NULL DEFAULT '', "
        "private boolean NOT NULL DEFAULT false, mailbox text, why text NOT NULL, "
        "since timestamptz NOT NULL, first_seen timestamptz NOT NULL, "
        "last_seen timestamptz NOT NULL, "
        "state text NOT NULL CHECK (state IN ('open', 'resolved')), "
        "resolved_at timestamptz, access_class smallint, "
        "needs smallint[] NOT NULL, col_needs integer[] NOT NULL DEFAULT '{}', "
        "units integer[] NOT NULL DEFAULT '{}', "
        "ai_ok boolean NOT NULL, PRIMARY KEY (rule, handle, topic, concerns), "
        # computed from at least one guarded row: a business row's class or a message's unit
        "CHECK (access_class IS NOT NULL OR cardinality(units) > 0))",
        f"CREATE INDEX IF NOT EXISTS alerts_open ON {s}.alerts (state, kind, since)",
        f"CREATE INDEX IF NOT EXISTS alerts_record ON {s}.alerts (record_table, handle)",
        f"CREATE INDEX IF NOT EXISTS alerts_concerns ON {s}.alerts (concerns) WHERE concerns <> ''",
        f"CREATE TABLE IF NOT EXISTS {s}.alert_runs (rule text PRIMARY KEY, run_at timestamptz "
        "NOT NULL, status text NOT NULL, reason text, open integer, opened integer, "
        "resolved integer, seconds double precision)",
        f"ALTER TABLE {s}.alert_runs ADD COLUMN IF NOT EXISTS skipped jsonb",
        # the columns of ``source_table`` the sentence shows (each column class is tested
        # against the reader's allow-list of exactly those columns)
        f"ALTER TABLE {s}.alerts ADD COLUMN IF NOT EXISTS shows text[] NOT NULL DEFAULT '{{}}'",
        # 0045: what the page view re-checks at READ time: the record row's shown columns,
        # the source rows (table, handles) and every object their lineage names (own, read,
        # per column); the rule's last finished run
        f"ALTER TABLE {s}.alerts ADD COLUMN IF NOT EXISTS record_shows text[] NOT NULL "
        "DEFAULT '{}'",
        f"ALTER TABLE {s}.alerts ADD COLUMN IF NOT EXISTS source_table text",
        f"ALTER TABLE {s}.alerts ADD COLUMN IF NOT EXISTS sources text[]",
        f"ALTER TABLE {s}.alerts ADD COLUMN IF NOT EXISTS objects text[]",
        f"ALTER TABLE {s}.alert_runs ADD COLUMN IF NOT EXISTS ok_at timestamptz",
    ]


# A rule's results are current for REFRESH_MINUTES (the business views timer, which starts
# the runner after each refresh: deploy/systemd/plx-business-views.timer); a rule whose last
# finished run is older than STALE_CYCLES of them shows "unavailable since" on every page,
# never its old items.
REFRESH_MINUTES = 30
STALE_CYCLES = 2
STALE_MINUTES = REFRESH_MINUTES * STALE_CYCLES
# the person's guarded schema's objects the runner owns (``ensure``)
PAGE_VIEW, PAGE_RUNS, PAGE_HELD = "alerts", "alert_runs", "alert_held"
RUN_LOCK = 41044   # pg advisory lock key: one alerts run at a time (business views: 41041)


def held_function_ddl(page: str) -> str:
    """``<page>.alert_held(record_table, handle, record_shows, source_table, sources,
    shows)``: true only when the reader, as the door decides it NOW (this transaction's
    settings and snapshot), sees the alert's record row in its own guarded view
    ``<page>.<record_table>`` with none of ``record_shows`` withheld, and every source row
    in ``<page>.<source_table>`` with none of ``shows`` withheld. Runs as the caller
    (``plx_page_reader``: it reads only the guarded views); the table names are read with
    ``to_regclass``/``%I`` (no dependency: the business build swaps those views freely). A
    view missing, a row gone (people exclusion, a record removed), a class or column class
    tightened in place: false (the alert is not shown)."""
    return (
        f"CREATE OR REPLACE FUNCTION {page}.{PAGE_HELD}(record_table text, rec text, "
        "record_shows text[], source_table text, sources text[], shows text[]) "
        "RETURNS boolean LANGUAGE plpgsql STABLE SECURITY INVOKER PARALLEL RESTRICTED "
        "SET search_path = pg_catalog AS $f$\n"
        "DECLARE n bigint; want bigint;\n"
        "BEGIN\n"
        "  IF record_table IS NULL OR rec IS NULL OR source_table IS NULL OR sources IS NULL\n"
        "     OR cardinality(sources) = 0 OR array_position(sources, NULL) IS NOT NULL\n"
        f"     OR to_regclass(format('%I.%I', '{page}', record_table)) IS NULL\n"
        f"     OR to_regclass(format('%I.%I', '{page}', source_table)) IS NULL THEN\n"
        "    RETURN false;\n"
        "  END IF;\n"
        f"  EXECUTE format('SELECT count(*) FROM %I.%I WHERE handle = $1 AND NOT (withheld "
        f"&& $2)', '{page}', record_table) INTO n USING rec, coalesce(record_shows, '{{}}');\n"
        "  IF n <> 1 THEN RETURN false; END IF;\n"
        # the record row is its only source and shows the same columns: already read
        "  IF source_table = record_table AND sources = ARRAY[rec]\n"
        "     AND coalesce(shows, '{}') <@ coalesce(record_shows, '{}') THEN\n"
        "    RETURN true;\n"
        "  END IF;\n"
        "  SELECT count(DISTINCT x) INTO want FROM unnest(sources) x;\n"
        f"  EXECUTE format('SELECT count(DISTINCT handle) FROM %I.%I WHERE handle = ANY($1) "
        f"AND NOT (withheld && $2)', '{page}', source_table) INTO n USING sources, "
        "coalesce(shows, '{}');\n"
        "  RETURN n = want;\n"
        "END\n$f$")


def page_view_ddl(s: str, page: str) -> str:
    """The person's guarded view ``<page>.alerts`` (``sql_views.page_schema``; read by
    ``plx.page`` as ``plx_page_reader`` with the door's settings, never by the AI's role):
    an OPEN alert is there only when, at READ time (the reader's transaction, one snapshot):

    * the reader holds what the run recorded: its record's class (a document class: the
      record not among the reader's hidden documents), EVERY class in ``needs`` (a document
      class there only when the reader has no hidden document at all: those rows' handles
      are not kept), and per column class in ``col_needs`` each shown column it marks or
      needs on the reader's allow-list for that column (``door.range_setting_for``; an
      unset setting holds nothing; a column class no longer known hides it);
    * no object the record's or its source rows' lineage named (own, read or per column:
      ``objects``) is in the people exclusion's set as it stands now
      (``ontology.people_excluded_objects``: hidden the moment the set grows, before the
      business tables follow it);
    * ``alert_held``: the record row and every source row are in the reader's own guarded
      views now, none of the shown columns withheld (a class or column class tightened in
      place, a row the exclusion removed: hidden at once, not after the next run).

    An alert from before ``objects``/``sources`` were recorded is hidden until its rule runs
    again. Mail alerts (no record class: the comms units), private ones and mailbox ones are
    never here (fail closed). Freightcom rows stay (a person may see them; the AI never
    reads this schema)."""
    from operating.pg import door
    from operating.pg import people_excluded as pe
    held = door._in("x.c", door.HELD, "plx.classes")
    dheld = door._in("x.c", door.DHELD, "plx.dclasses")
    hidden = "NULLIF(current_setting('plx.d_hidden', true), '')"
    return (
        f"CREATE OR REPLACE VIEW {page}.{PAGE_VIEW} WITH (security_barrier) AS SELECT a.rule, "
        "a.kind, a.record_table, a.handle, a.topic, a.why, a.since, a.first_seen, a.state, "
        f"a.resolved_at FROM {s}.alerts a WHERE a.state = 'open' "
        "AND a.access_class IS NOT NULL "
        "AND NOT a.private AND a.mailbox IS NULL AND cardinality(a.units) = 0 "
        "AND a.objects IS NOT NULL AND a.sources IS NOT NULL AND a.source_table IS NOT NULL "
        f"AND EXISTS (SELECT 1 FROM (SELECT a.access_class::int AS c) x WHERE {held} OR "
        f"({dheld} AND {hidden} IS NOT NULL AND NOT (a.handle IN (SELECT unnest("
        f"{hidden}::text[]))))) "
        "AND NOT EXISTS (SELECT 1 FROM (SELECT unnest(a.needs)::int AS c) x WHERE NOT "
        f"(coalesce({held}, false) OR (coalesce({dheld}, false) AND {hidden} = '{{}}'))) "
        "AND NOT EXISTS (SELECT 1 FROM unnest(a.col_needs) n(id) WHERE NOT EXISTS "
        f"(SELECT 1 FROM {s}.col_classes cc WHERE cc.id = n.id)) "
        f"AND NOT EXISTS (SELECT 1 FROM unnest(a.col_needs) n(id) JOIN {s}.col_classes cc "
        "ON cc.id = n.id CROSS JOIN LATERAL unnest(a.shows) k(col) WHERE (cc.marks ? k.col "
        "OR cc.needs ? k.col) AND NOT coalesce(n.id <@ NULLIF(current_setting('plx.r_' || "
        "k.col, true), '')::int4multirange, false)) "
        f"AND NOT EXISTS (SELECT 1 FROM unnest(a.objects) o(id) JOIN {pe.TABLE} p "
        "ON p.object_id = o.id) "
        f"AND {page}.{PAGE_HELD}(a.record_table, a.handle, a.record_shows, a.source_table, "
        "a.sources, a.shows)")


def runs_view_ddl(s: str, page: str) -> str:
    """``<page>.alert_runs``: per rule when it last finished (``ok_at``) and last ran, so a
    page can say "as of" and "unavailable since". No record, no reason text."""
    return (f"CREATE OR REPLACE VIEW {page}.{PAGE_RUNS} AS SELECT rule, ok_at, run_at, "
            f"status FROM {s}.alert_runs")


def _schema(schema: str | None) -> str:
    s = schema or bv.SCHEMA
    if not _NAME.match(s):
        raise ValueError(f"not a schema name: {s!r}")
    return s


def rule_sql(rule: Rule, schema: str, day: str, select: str | None = None) -> str:
    """The rule's SELECT (or its ``skips``: ``select``) for one schema and day; its
    parameters are constants of this module (numbers, ISO days and word lists), rendered as
    SQL literals, never input."""
    def lit(v: Any) -> str:
        if isinstance(v, bool) or v is None:
            raise ValueError(f"{rule.name}: unsupported parameter {v!r}")
        if isinstance(v, int):
            return str(v)
        if isinstance(v, (list, tuple)):
            return "ARRAY[" + ", ".join(lit(x) for x in v) + "]::text[]"
        return "'" + str(v).replace("'", "''") + "'"
    params = {k: lit(v) for k, v in {**rule.params, "day": day}.items()}
    return (select or rule.select).replace("{s}", schema).format(p=params)


def _missing(conn: Any, s: str, tables: Sequence[str], side: Sequence[str] = ()) -> list[str]:
    names = [f"{s}.{t}" for t in tables] + [x.replace("{s}", s) for x in side]
    return [n for n in names
            if conn.execute("SELECT to_regclass(%s)", (n,)).fetchone()[0] is None]


def run_rule(conn: Any, rule: Rule, s: str, day: str) -> dict[str, Any]:
    """One rule, one transaction (``conn`` autocommit off): current rows -> upsert -> resolve
    the open rows no longer found. Blocked (a table missing) or failed: nothing changes."""
    started = time.monotonic()
    # a business rule records its rows' lineage objects (``objects``): no lineage, no run
    missing = _missing(conn, s, rule.reads, tuple(rule.side) + (
        () if rule.comms or "{s}.lineage" in rule.side else ("{s}.lineage",)))
    if missing:
        out = {"rule": rule.name, "status": "blocked", "reason": "missing: " + ", ".join(missing)}
        _record(conn, s, out, started)
        return out
    try:
        with conn.transaction():
            conn.execute(f"SET LOCAL statement_timeout = {int(RULE_STATEMENT_MS)}")
            cols = COMMS_COLS if rule.comms else OUT_COLS
            units = "" if rule.comms else ", '{}'::integer[] AS units"
            conn.execute("CREATE TEMP TABLE al_now ON COMMIT DROP AS SELECT DISTINCT ON "
                         "(handle, topic, concerns) * FROM (SELECT q.*" + units + " FROM (" +
                         rule_sql(rule, s, day) + ") AS q(" + ", ".join(cols) + ")) q "
                         "WHERE handle IS NOT NULL AND why IS NOT NULL AND (access_class "
                         "IS NOT NULL OR (units IS NOT NULL AND cardinality(units) > 0 "
                         "AND array_position(units, NULL) IS NULL))")
            conn.execute(
                "ALTER TABLE al_now ADD COLUMN col_needs integer[], "
                "ALTER COLUMN topic SET DATA TYPE text USING coalesce(topic, ''), "
                "ALTER COLUMN concerns SET DATA TYPE text USING coalesce(concerns, '')")
            src_table = rule.cols_of or rule.table
            if not rule.comms:
                # every object the record's and the source rows' lineage names (own, read,
                # per column): the page view hides the alert the moment one is excluded
                conn.execute(
                    "ALTER TABLE al_now ADD COLUMN objects text[]")
                conn.execute(
                    "UPDATE al_now n SET objects = ARRAY(SELECT DISTINCT o FROM ("
                    f"SELECT l.* FROM {s}.lineage l WHERE l.view = %s AND l.handle = n.handle "
                    f"UNION ALL SELECT l.* FROM {s}.lineage l WHERE l.view = %s "
                    "AND l.handle = ANY(n.sources)) l CROSS JOIN LATERAL unnest(l.object_ids "
                    "|| l.read_ids || ARRAY(SELECT jsonb_array_elements_text(e.v) FROM "
                    "jsonb_each(l.col_objects) e(k, v) WHERE jsonb_typeof(e.v) = 'array')) o "
                    "WHERE o IS NOT NULL)", (rule.table, src_table))
                if rule.also_objects:
                    conn.execute(
                        "UPDATE al_now n SET objects = ARRAY(SELECT DISTINCT o FROM unnest("
                        "n.objects || ARRAY(SELECT x.o FROM (" +
                        rule_sql(rule, s, day, rule.also_objects) +
                        ") AS x(h, o) WHERE x.h = n.handle)) o)")
            conn.execute(
                "UPDATE al_now n SET col_needs = ARRAY(SELECT DISTINCT cc.id FROM "
                f"{s}.col_classes cc WHERE cc.id = ANY(n.col_classes) AND (cc.marks ?| "
                "%s::text[] OR cc.needs ?| %s::text[]))",
                (list(rule.shown_cols) or [""], list(rule.shown_cols) or [""]))
            opened = conn.execute(
                f"INSERT INTO {s}.alerts AS a (rule, kind, record_table, handle, topic, concerns, "
                "private, mailbox, why, since, first_seen, last_seen, state, resolved_at, "
                "access_class, needs, col_needs, units, ai_ok, shows, record_shows, source_table, "
                "sources, objects) SELECT %s, %s, %s, handle, topic, "
                "concerns, coalesce(private, false), mailbox, why, coalesce(since, now()), now(), "
                "now(), 'open', NULL, access_class, coalesce(needs, CASE WHEN access_class IS "
                "NULL THEN '{}' ELSE ARRAY[access_class] END), coalesce(col_needs, '{}'), "
                "units, coalesce(ai_ok, false), %s::text[], %s::text[], %s, sources, "
                + ("NULL::text[]" if rule.comms else "objects") + " FROM al_now "
                "ON CONFLICT (rule, handle, topic, concerns) DO UPDATE SET "
                "kind = EXCLUDED.kind, record_table = EXCLUDED.record_table, "
                "private = EXCLUDED.private, mailbox = EXCLUDED.mailbox, why = EXCLUDED.why, "
                "since = CASE WHEN a.state = 'open' THEN a.since ELSE EXCLUDED.since END, "
                "first_seen = CASE WHEN a.state = 'open' THEN a.first_seen ELSE now() END, "
                "last_seen = now(), state = 'open', resolved_at = NULL, "
                "access_class = EXCLUDED.access_class, needs = EXCLUDED.needs, "
                "col_needs = EXCLUDED.col_needs, units = EXCLUDED.units, ai_ok = EXCLUDED.ai_ok, "
                "shows = EXCLUDED.shows, record_shows = EXCLUDED.record_shows, "
                "source_table = EXCLUDED.source_table, sources = EXCLUDED.sources, "
                "objects = EXCLUDED.objects "
                "RETURNING (xmax = 0) OR a.first_seen = now()",
                (rule.name, rule.kind, rule.table, list(rule.shown_cols),
                 list(rule.record_shows if rule.cols_of and rule.cols_of != rule.table
                      else rule.shows), src_table)).fetchall()
            resolved = conn.execute(
                f"UPDATE {s}.alerts a SET state = 'resolved', resolved_at = now() "
                "WHERE a.rule = %s AND a.state = 'open' AND NOT EXISTS (SELECT 1 FROM al_now n "
                "WHERE n.handle = a.handle AND n.topic = a.topic AND n.concerns = a.concerns)",
                (rule.name,)).rowcount
            conn.execute(f"DELETE FROM {s}.alerts WHERE rule = %s AND state = 'resolved' AND "
                         f"resolved_at < now() - interval '{int(KEEP_RESOLVED_DAYS)} days'",
                         (rule.name,))
            skipped = ({str(k): int(n) for k, n in conn.execute(
                rule_sql(rule, s, day, rule.skips)).fetchall()} if rule.skips else None)
        out = {"rule": rule.name, "status": "ok", "open": len(opened),
               "opened": sum(1 for (new,) in opened if new), "resolved": resolved,
               "skipped": skipped}
    except Exception as exc:   # noqa: BLE001 - recorded; the rule's alerts stay as they were
        out = {"rule": rule.name, "status": "failed", "reason": f"{type(exc).__name__}: "
               + str(exc).splitlines()[0][:300]}
    _record(conn, s, out, started)
    return out


def _record(conn: Any, s: str, out: Mapping[str, Any], started: float) -> None:
    with conn.transaction():
        conn.execute(
            f"INSERT INTO {s}.alert_runs (rule, run_at, status, reason, open, opened, resolved, "
            "seconds, skipped, ok_at) VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, "
            "CASE WHEN %s = 'ok' THEN now() END) ON CONFLICT (rule) DO UPDATE "
            "SET run_at = EXCLUDED.run_at, status = EXCLUDED.status, reason = EXCLUDED.reason, "
            "open = EXCLUDED.open, opened = EXCLUDED.opened, resolved = EXCLUDED.resolved, "
            "seconds = EXCLUDED.seconds, skipped = EXCLUDED.skipped, "
            f"ok_at = coalesce(EXCLUDED.ok_at, {s}.alert_runs.ok_at)",
            (out["rule"], out["status"], out.get("reason"), out.get("open"), out.get("opened"),
             out.get("resolved"), round(time.monotonic() - started, 3),
             json.dumps(out["skipped"]) if out.get("skipped") is not None else None,
             out["status"]))


def _columns(conn: Any, s: str, table: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
        "AND table_name = %s", (s, table)).fetchall()}


# the columns a table made by an older ``_ddl`` gains (ADD COLUMN IF NOT EXISTS takes an
# ACCESS EXCLUSIVE lock even when the column is there: run only when one is missing)
_ADDED = {"alerts": (("shows", "text[] NOT NULL DEFAULT '{}'"),
                     ("record_shows", "text[] NOT NULL DEFAULT '{}'"),
                     ("source_table", "text"), ("sources", "text[]"), ("objects", "text[]")),
          "alert_runs": (("skipped", "jsonb"), ("ok_at", "timestamptz"))}


def _definition_tag(ddl: Sequence[str]) -> str:
    import hashlib
    return "plx-alerts " + hashlib.sha256("\n".join(ddl).encode()).hexdigest()[:24]


def ensure(conn: Any, schema: str | None = None) -> str:
    """The tables (``_ddl``) and, once the person's guarded schema exists (migration
    0043_page_reader), its alerts view, its run view and the ``alert_held`` function,
    granted to ``plx_page_reader`` only.

    Never in a reader's way: a table or column already there is left alone (no lock), and
    the guarded objects are replaced only when their definition changed (the view's
    comment holds its definition's tag), with CREATE OR REPLACE in one transaction (a
    reader sees the old view or the new one, never none); only a column list change falls
    back to drop and create, in that same transaction."""
    from operating.pg import sql_views
    from operating.pg import people_excluded as pe
    s = _schema(schema)
    page = sql_views.page_schema(s)
    with conn.transaction():
        if conn.execute("SELECT to_regclass(%s) IS NULL OR to_regclass(%s) IS NULL",
                        (f"{s}.alerts", f"{s}.alert_runs")).fetchone()[0]:
            for stmt in _ddl(s):
                conn.execute(stmt)
        for table, cols in _ADDED.items():
            have = _columns(conn, s, table)
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {s}.{table} ADD COLUMN IF NOT EXISTS {name} {decl}")
        if conn.execute("SELECT to_regnamespace(%s)", (page,)).fetchone()[0] is None:
            return s
        if conn.execute("SELECT to_regclass(%s)", (pe.TABLE,)).fetchone()[0] is None:
            # the people exclusion's set is not there: no view (fail closed; the door
            # refuses its readers too until it is)
            conn.execute(f"DROP VIEW IF EXISTS {page}.{PAGE_VIEW}")
            return s
        ddl = [held_function_ddl(page), page_view_ddl(s, page), runs_view_ddl(s, page)]
        tag = _definition_tag(ddl)
        now = conn.execute("SELECT obj_description(to_regclass(%s), 'pg_class')",
                           (f"{page}.{PAGE_VIEW}",)).fetchone()[0]
        if now == tag:
            return s
        conn.execute(ddl[0])
        for view, stmt in ((PAGE_VIEW, ddl[1]), (PAGE_RUNS, ddl[2])):
            conn.execute("SAVEPOINT al_view")
            try:
                conn.execute(stmt)
            except Exception:  # noqa: BLE001 - a changed column list: replace in this transaction
                conn.execute("ROLLBACK TO SAVEPOINT al_view")
                conn.execute(f"DROP VIEW IF EXISTS {page}.{view}")
                conn.execute(stmt)
            conn.execute("RELEASE SAVEPOINT al_view")
            conn.execute(f"REVOKE ALL ON {page}.{view} FROM PUBLIC")
        conn.execute(f"REVOKE ALL ON FUNCTION {page}.{PAGE_HELD}(text, text, text[], text, "
                     "text[], text[]) FROM PUBLIC")
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                        (sql_views.PAGE_ROLE,)).fetchone():
            for view in (PAGE_VIEW, PAGE_RUNS):
                conn.execute(f"GRANT SELECT ON {page}.{view} TO {sql_views.PAGE_ROLE}")
            conn.execute(f"GRANT EXECUTE ON FUNCTION {page}.{PAGE_HELD}(text, text, text[], "
                         f"text, text[], text[]) TO {sql_views.PAGE_ROLE}")
        conn.execute(f"COMMENT ON VIEW {page}.{PAGE_VIEW} IS " + "'" + tag + "'")
    return s


def run(dsn: str, names: Sequence[str] | None = None, *, schema: str | None = None,
        day: str | None = None, owners: Mapping[str, Sequence[str]] | None = None
        ) -> list[dict[str, Any]]:
    """Every rule (or ``names``; an unknown name refuses the whole run) over the business
    schema, for ``day`` (ISO; default the business day today). ``owners``: per mailbox id
    its owner's addresses (default: read-only from the mail capture, as the emails build
    reads them); unknown, every mail rule is blocked (a sender cannot be told from the
    owner: nothing is guessed)."""
    import psycopg
    unknown = sorted(set(names or ()) - set(RULES))
    if unknown:
        raise ValueError(f"unknown alert rules: {', '.join(unknown)}")
    when = day or bv.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
        raise ValueError(f"not an ISO day: {when!r}")
    chosen = [RULES[n] for n in (names or list(RULES))]
    if owners is None and any(r.comms for r in chosen):
        from operating.pg import business_comms
        owners = business_comms.mailbox_addresses(business_comms.capture_db())
    with psycopg.connect(dsn, autocommit=True) as conn:
        # one run at a time: the timer's and the one after a views refresh never overlap
        # (the second leaves at once; the first's results are as current)
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK,)).fetchone()[0]:
            return []
        try:
            s = ensure(conn, schema)
        except psycopg.errors.InsufficientPrivilege as exc:
            # 11-18: run by hand as the API's role, ensure() could not read biz_page
            raise PermissionError(
                f"the alerts runner makes the guarded alerts view in {_schema(schema)}'s "
                "page schema: run it as that schema's owner (the plx-alerts unit's user), "
                f"not as role {conn.info.user!r}: {str(exc).splitlines()[0]}") from None
        if owners:
            owner_table(conn, owners)
        out = []
        for rule in chosen:
            if rule.comms and not owners:
                got = {"rule": rule.name, "status": "blocked",
                       "reason": "the mailbox owners' addresses could not be read"}
                _record(conn, s, got, time.monotonic())
                out.append(got)
            else:
                out.append(run_rule(conn, rule, s, when))
        return out


# --------------------------------------------------------------------------- the named read
def read_sql(schema: str | None = None) -> str:
    """The one read every page uses: open (or resolved) alerts a reader may see, newest
    condition first. Parameters (named): classes (int2[] the reader holds), units (int[]
    the comms units the reader holds: ``comms_units`` whose audience key and marks the
    reader holds, as the comms door computes them; none given sees no mail alert), col_classes
    (int[] the column classes the door lets the reader read; none: no alert whose sentence
    shows a marked cell), mailboxes (text[] the
    reader may read), me (text[] the reader's own identities: mailbox addresses, staff
    names), kinds (text[] or NULL for every kind), state, ai (true: an AI reader), limit."""
    s = _schema(schema)
    return (f"SELECT rule, kind, record_table, handle, topic, concerns, why, since, state, "
            f"resolved_at FROM {s}.alerts a WHERE a.state = %(state)s "
            "AND (a.access_class IS NULL OR a.access_class = ANY(%(classes)s::smallint[])) "
            "AND (a.access_class IS NOT NULL OR cardinality(a.units) > 0) "
            "AND a.units <@ %(units)s::integer[] "
            "AND a.needs <@ %(classes)s::smallint[] "
            "AND a.col_needs <@ %(col_classes)s::integer[] "
            "AND (a.mailbox IS NULL OR a.mailbox = ANY(%(mailboxes)s::text[])) "
            "AND (NOT a.private OR a.concerns = ANY(%(me)s::text[])) "
            "AND (%(kinds)s::text[] IS NULL OR a.kind = ANY(%(kinds)s::text[])) "
            "AND (NOT %(ai)s OR a.ai_ok) "
            "ORDER BY a.since DESC, a.rule, a.handle LIMIT %(limit)s")


def read(conn: Any, *, classes: Sequence[int], units: Sequence[int] = (),
         col_classes: Sequence[int] = (),
         mailboxes: Sequence[str] = (), me: Sequence[str] = (),
         kinds: Sequence[str] | None = None, state: str = "open", ai: bool = False,
         limit: int = 500, schema: str | None = None) -> list[dict[str, Any]]:
    """``read_sql`` with its parameters checked: classes must be given (a full reader passes
    every class id; nothing given sees nothing), state open or resolved."""
    if classes is None or isinstance(classes, (str, bytes)):
        raise ValueError("classes: the reader's access class ids")
    if units is None or isinstance(units, (str, bytes)):
        raise ValueError("units: the reader's comms unit ids")
    if state not in ("open", "resolved"):
        raise ValueError(f"unknown state {state!r}")
    cur = conn.execute(read_sql(schema), {
        "classes": [int(c) for c in classes], "units": [int(u) for u in units],
        "col_classes": [int(c) for c in col_classes],
        "mailboxes": [m.lower() for m in mailboxes], "me": [m.lower() for m in me],
        "kinds": list(kinds) if kinds is not None else None, "state": state, "ai": bool(ai),
        "limit": int(limit)})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# --------------------------------------------------------------------------- parity
# The old engine's gap kinds each rule replaces (its ``ported`` detector's output).
OLD_KINDS: dict[str, tuple[str, ...]] = {
    "po_late": ("late_po", "po_late"), "customer_order_late": ("late_customer_order",),
    "lot_on_hold": ("lot_on_hold",), "price_below_cost": ("price_below_cost",),
    "price_below_typical": ("price_below_typical",),
    "customer_price_drop": ("customer_price_drop",),
    "customer_demand_drop": ("customer_demand_drop",)}


def parity(conn: Any, inbox: str | Path, names: Sequence[str] | None = None, *,
           schema: str | None = None, day: str | None = None, sample: int = 40
           ) -> dict[str, dict[str, Any]]:
    """Record-level parity of each rule against the old engine's open gaps (the gap inbox,
    opened read-only): every old gap object -> the row of the rule's table holding it
    (``lineage``), compared with the rule's current handles. READ ONLY on both sides (the
    rule's SELECT runs in a read-only transaction; nothing is written). A gap object no
    row holds is counted (``old_not_in_model``), never matched by guess."""
    import sqlite3
    s = _schema(schema)
    when = day or bv.today().isoformat()
    unknown = sorted(set(names or ()) - set(OLD_KINDS))
    if unknown:
        raise ValueError(f"no old kinds for: {', '.join(unknown)}")
    db = sqlite3.connect(f"file:{Path(inbox)}?mode=ro", uri=True)
    out: dict[str, dict[str, Any]] = {}
    try:
        for name in names or list(OLD_KINDS):
            rule, kinds = RULES[name], OLD_KINDS[name]
            old = sorted({o for (o,) in db.execute(
                "SELECT object_id FROM gaps WHERE state = 'open' AND kind IN (%s)"
                % ",".join("?" * len(kinds)), kinds)})
            started = time.monotonic()
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute(f"SET LOCAL statement_timeout = {int(RULE_STATEMENT_MS)}")
                cols = COMMS_COLS if rule.comms else OUT_COLS
                new = {h for (h,) in conn.execute(
                    "SELECT DISTINCT q.handle FROM (" + rule_sql(rule, s, when) + ") AS q("
                    + ", ".join(cols) + ")").fetchall()}
                secs = round(time.monotonic() - started, 2)
                held = dict(conn.execute(
                    f"SELECT o, l.handle FROM {s}.lineage l, unnest(l.object_ids) o "
                    "WHERE l.view = %s AND o = ANY(%s)", (rule.table, old)).fetchall())
                skipped = ({str(k): int(n) for k, n in conn.execute(
                    rule_sql(rule, s, when, rule.skips)).fetchall()} if rule.skips else None)
            old_h = {held[o] for o in old if o in held}
            out[name] = {"seconds": secs, "new": len(new), "old_open": len(old),
                         "old_in_model": len(old_h),
                         "old_not_in_model": sum(1 for o in old if o not in held),
                         "both": len(new & old_h), "new_only": len(new - old_h),
                         "old_only": len(old_h - new), "skipped": skipped,
                         "new_only_handles": sorted(new - old_h)[:sample],
                         "old_only_handles": sorted(old_h - new)[:sample]}
    finally:
        db.close()
    return out


def describe() -> list[dict[str, Any]]:
    """Every rule as declared (what the dictionary and the detector inventory show)."""
    return [{"rule": r.name, "kind": r.kind, "about": r.about, "table": r.table,
             "reads": list(r.reads), "resolves": r.resolves, "ported": r.ported,
             "shows": list(r.shows)} for r in RULES.values()]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="operating.pg.alerts")
    ap.add_argument("command", choices=("run", "describe", "sql", "parity"))
    ap.add_argument("--inbox", help="parity: the old engine's gap inbox (read-only)")
    ap.add_argument("--rule", action="append")
    ap.add_argument("--day")
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    if args.command == "describe":
        print(json.dumps(describe(), indent=1))
        return 0
    if args.command == "sql":
        for n in args.rule or list(RULES):
            day = args.day or bv.today().isoformat()
            print(f"-- {n}\n{rule_sql(RULES[n], _schema(None), day)};")
        return 0
    from operating.pg import config
    if args.command == "parity":
        if not args.inbox:
            ap.error("parity needs --inbox")
        import psycopg
        with psycopg.connect(args.dsn or config.dsn(), autocommit=True) as conn:
            print(json.dumps(parity(conn, args.inbox, args.rule, day=args.day), indent=1,
                             default=str))
        return 0
    got = run(args.dsn or config.dsn(), args.rule, day=args.day)
    for out in got:
        print(json.dumps(out))
    return 0 if all(o["status"] != "failed" for o in got) else 1


if __name__ == "__main__":
    sys.exit(main())
