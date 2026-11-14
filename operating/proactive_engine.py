"""Proactive engine: scheduled, set-based gap detection over the whole projection store,
and a persisted per-employee gap inbox the console Home reads.

The detectors read the store with SQL over all its current records (never one object
read at a time), so ``scanned`` is the whole store; each says ``complete``, ``truncated``,
``blocked`` (a source the rule needs is not served), ``not_configured`` or ``failed``:

* ``late_or_unconfirmed_po``: a purchase order open by the one PO status definition
  (``operating.po_status``: each settled record by its own source, the Portal header word
  with its own lines' summed quantities, a BC order's fullyReceived; an unknown status,
  a Sage-only or FileMaker-only order included, is counted ``not_evaluated`` by its
  reason, never open) judged on its requested-by date (its ``expected_date``; none is
  ``no_need_date``, never a line's or the supplier's date in its place): past it while
  open is late (``late_po`` ``overdue``, or ``unconfirmed_po`` when no supplier
  confirmation is linked); not yet past it with a settled confirmation after it is at
  risk (``late_po`` ``confirmed_after_need_date``). A confirmation linked only as a lead
  (when not already late), two need dates or two statuses across one order's settled
  identities are counted ``not_evaluated``, never raised; the result's ``counts`` (and
  the run summary's ``po_lateness``) say how many were evaluated, late and at risk;
* ``systems_disagree`` (raised by ``late_or_unconfirmed_po``): two linked records of one
  purchase order disagree (``po_status.read``'s conflicts: receipt state, received
  quantity, requested-by date), ONE gap per pair and field naming both values and who
  governs; each side's citations are their own group, so a reader of one side never
  sees it. A BC side that governs receipt state also joins the late gap's evidence;
* ``late_customer_order``: a customer order open past its own required date, ONE gap per
  order across its records (settled ``SalesOrder`` identity links only): open when every
  record stating a status is open by its own system's word, closed when any record is
  closed (a closed word, BC fully shipped or kept only as posted documents) or a settled
  shipment or invoice is linked to any; an unknown word, no status, a lead twin that is
  closed or a lead shipment is ``not_evaluated``, never open. Its required date must be a
  requested one (``operating.source_defaults``: never a bulk default of its source nor the
  record's own creation or order day; such an order is undated, never late). Cited by the
  date and each status value (each its own group); it rests on no closure being recorded,
  so it is a ``REQUIRES_GRANT`` rule (every audience of orders, shipments and invoices);
* ``po_receipt_quantity_mismatch``: the received quantity of a PO line (its receipt
  lines through settled ``receives_line`` links, one quantity per receipt line identity)
  above the ordered one, or below it on an order recorded as fully received (closed
  ``RECEIVED`` by the one PO status definition); a unit that
  differs, a disputed quantity or a lead-only link is ``not_evaluated``;
* ``receipt_without_qc``: a received receipt with no QC record linked to it or its
  lines. ``blocked`` while the schema declares no QC link (the QC relations are not
  served: upstream ask Q3) unless the caller names the link types;
* ``order_without_customer_po``: the orders ``operating.customer_po_coverage``'s full
  mirror scan classifies ``absent_in_this_source`` (no linked FM row holding one), mapped
  to their projected record; ``complete`` only when that scan was;
* ``late_order``: ``operating.proactive_gaps``' late-order rule over every purchase order
  line (expected receipt date before ``as_of``, received below ordered; one cited value of
  each, else the line is ``not_evaluated``), ONE gap per order through its settled
  ``line_of`` links, with the never-sent supplier draft (``follow_up.draft``,
  ``late_order_draft``): the order's number, its late lines and the governing date (the
  supplier's confirmed delivery date through a settled ``SupplierConfirmation.confirms``,
  else "our expected date"), each fact in a side list of citations. A reader who may not
  open every fact the draft cites gets the gap without the draft; the console words it
  again for the person who proposes it (``order_draft``) and records it on the case;
* ``expired_quote`` and ``conflicting_quantity``: ``operating.proactive_gaps``' rules over
  every quote and every quantity of each settled identity cluster;
* ``orders_at_risk``: ``operating.impact_chain``'s walk from every late or slipping
  purchase order (``plx.process``'s own verdict, asked through ``call``) over recorded
  links only to the customer orders it feeds: ONE gap per order at risk (days at risk
  above 0; days at risk is a lower bound, no assembly lead time is recorded), citing the PO's lateness
  dates (each its own group), every record of the chain (each its own group, so a reader
  who may not open one link of the chain never sees the gap) and the order's required
  date; never a value. The verdict is the run principal's: it also rests on what the
  receipt and promise sources do NOT hold, so the gap further requires (``gap_requires``)
  a grant on every source audience the ``po_delivery_window`` graph reads
  (``_lateness_audiences``): a reader whose own verdict would be withheld never sees it.
  The summary's ``top_finding`` says "N customer orders at risk from M late POs, worst
  first" (counts only). With the run's process pool every PO reaching an assembly is
  asked (no ``card_bound``); one the budget did not reach is counted ``time_budget`` in
  the rule's coverage. Where ``plx.process`` judged the PO per line, the line feeding the
  order decides by its own promise (``impact_chain.Late.material``): the gap cites the
  confirmation's per-item date as its own group and its event names the line, how it was
  matched, its promised date and the order's required date. A gap whose date is a
  supplier's promise tells a reader who may not read every audience a promise sits in
  (supplier confirmations, mailboxes), or every reader when the run's own promises were
  not proven complete, that a later promise it cannot see may exist (``read_inbox``; the
  audiences are never shown). ``not_configured`` without a caller;
* the proactive events (``operating.proactive_events``): ``eta_slipped``,
  ``lot_attention`` (``lot_near_expiry``, ``lot_on_hold``), ``accepted_quote_no_order``,
  ``invoice_quantity_mismatch`` and ``vendor_no_reply`` (F28's question, set-based over
  the store's mail, people cleared first) over the store, and ``vendor_terms_unapplied``
  (F27) and ``negative_margin`` (F10/F31) through the caller's ``plx.investigate``,
  bounded, or over every candidate on the run's process pool (``run(pool=)``,
  ``operating.proactive_pool``). Each rule states its ``coverage`` (records judged of
  those it should judge, the rest by reason; the summary's ``coverage``). A gap about a purchase order, sales order, quote or
  purchase invoice carries the record's cited total and currency as its ranking amount
  (``_with_amounts``), shown only to a reader who may open both; ``operating.briefing``
  ranks and routes them per reader;
* the business alerts (``operating.proactive_business``): ``stock_out_risk`` (open customer
  orders an item's on hand plus inbound purchase orders cannot cover by their ship dates;
  ``REQUIRES_GRANT``), ``overdue_receivable`` and ``unit_cost_jump`` (finance-marked);
* the operations alerts (``operating.proactive_ops``): overdue and stuck Mission Control
  tasks, MC approvals waiting, overdue Planner tasks, aged receivables and payables
  worsening (finance-marked), late production and assembly damage spikes, and Teams asks a
  person left unanswered (people cleared first; ``REQUIRES_GRANT``);
* the chief-executive signals (``operating.proactive_ceo``): ``customer_demand_drop`` (a
  customer's last 90 days of orders against its run-rate, finance-marked),
  ``promise_date_slip`` (a customer's orders missing their promised date, with a trend) and
  ``supplier_reliability`` (a supplier's late receipts getting worse), all
  ``REQUIRES_GRANT``; ``margin_erosion`` (recent invoiced margin against the earlier
  orders', from the negative-margin rule's order margins, finance-marked);
  ``shipped_not_invoiced`` (``REQUIRES_GRANT``), ``duplicate_customer_order`` and
  ``recurring_component_damage``;
* ``operating.proactive_chasers``: ``customer_chase`` (customers chasing where an order is,
  and orders to tell them about first; people cleared first, ``REQUIRES_GRANT``) and
  ``quality_holds`` (batches and lots on hold past their days, reject trends);
* ``operating.proactive_cash``: ``customer_payment_behaviour`` (a customer paying slower
  than its own prior year), ``early_pay_discounts`` (a supplier discount missed) and
  ``duplicate_payables`` (a supplier invoice or payment entered twice),
  ``returns_and_credits`` (a customer's credit notes rising against its own year), finance-marked;
* ``operating.proactive_stock``: ``slow_moving_stock`` and ``assembly_component_shortfall``
  (``REQUIRES_GRANT``), ``expiring_lot_stock``, ``certificate_expiry`` and
  ``freight_cost_anomaly`` (finance-marked), each ``blocked`` naming a field not served;
  ``reorder_soon`` (forecast demand, ``operating.demand_forecast``, past on hand plus
  inbound; ``REQUIRES_GRANT``, ``blocked`` while on hand is not served);
* ``operating.proactive_prices``: ``price_consistency`` (a customer paying far below the
  typical price of a product, or below its cost, over the last 180 days), finance-marked;
* ``operating.proactive_sales``: ``stale_quote`` (a Portal or Business Central quote open past
  its days with no order; ``REQUIRES_GRANT``; the value finance-marked in the event);
* ``operating.proactive_quality``: ``duplicate_parties`` (customers and suppliers that may be
  one, each a review proposal a person confirms), ``key_fields_missing`` (an active record
  without a key field, ``REQUIRES_GRANT``), ``twin_disagreement`` (copies in two systems
  stating different values) and ``stale_captures`` (a source not captured lately);
* ``operating.proactive_sync``: ``sync_gaps`` (a record missing from, or differing on a key
  field in, the system it must reach ``SYNC_DAYS`` after it was made; ``REQUIRES_GRANT``);
* every gap about a sales or purchase order passes the one active-population predicate
  (``inactive_records``, ``_drop_inactive_orders``): a closed or historic order is counted
  ``closed_order``/``historic_order``, never listed (``CLOSED_ORDER_RULES`` keep closed ones);
* ``operating.proactive_commitments``: ``commitments`` (promises in our messages about an
  open customer order, due or missed) and ``needs_reply`` (inbound mail and one-to-one
  Teams messages waiting on their addressee's reply, shown to that person only); people
  cleared first, ``REQUIRES_GRANT``;
* ``card_rules``: ``missing_in_record`` and attested conflicts from the ``plx.object``
  cards of the documents that attest a record (bounded per-object reads, ``CARD_BOUND``).

A gap carries the citations its claim rests on, grouped (each group must stay cited for a
reader to see the gap), a stable id and a proposed ``follow_up``
(``operator_api.workspace.object_workspace.FOLLOW_UP_FIELDS``) that only a person sends. ``run``
writes one inbox file next to the store: a gap found again keeps its id and first sight,
a gap a complete detector no longer finds is ``resolved`` (found again: reopened), and an
acknowledgement is per reader. ``read_inbox`` trims for one reader: a gap shows only when
its subject and at least one citation of every group sit in audiences the reader is
granted, at markings it holds, and pass the host's per-item decision (a gap of a
``REQUIRES_GRANT`` detector also needs its recorded audience requirements, every one
granted; none recorded: never shown); the counts it
returns are over what that reader may read.

``refresh`` is the full scheduled run (every detector: the customer PO coverage scan and
the tool caller's ``plx.process``/``plx.object`` reads as the run principal), shared by
``main`` and the host's post-build hook (``after_build(scheduled=)``), within a time
budget (``REFRESH_BUDGET_SECONDS``): a detector the budget stops is ``truncated`` with
reason ``time_budget``, never ``complete``, and leaves its gaps as they were.

``python3 -m operating.proactive_engine`` is the live check's command
(live check ``case.proactive_engine``): one host capture cycle, the engine
over its projection, then the inbox read per reader; counts and enums only. Evidence
label: that of the captures it reads (live-unverified until the verifier runs it).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
from collections import Counter, OrderedDict, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology import store as ostore
from ontology.units import unit_token

from . import alert_kinds, copy_gate, drafts
from .copy_gate import say_date
from . import active_population as active
from . import po_status
from . import test_records as test_records_rule
from . import proactive_gaps as pg
from . import sales_order_status as so_status
from . import suspect_money
from . import order_value
from operating import declared_schema as _schemas
from operating.declared_schema import proactive_inbox as _PROACTIVE_INBOX
from operating.declared_schema.proactive_inbox import _VISITS_DDL

SCHEMA = "plx-proactive-engine/v1"
INBOX_SCHEMA = "plx-gap-inbox/v1"
MAX_SHOWN = 100
CARD_BOUND = 500
CARD_ALL = 1_000_000   # card_rules on the pool: every candidate document (never a cut)
VALUES_CHUNK = 500   # object ids per value read (``_Store.values``)
# On the Postgres ontology a value read joins the value tables itself (``_PG_VALUES_SQL``)
# and a whole type up to ``PG_TYPE_READ_MAX`` records is read in one statement by its type
# (32k assembly orders: 9-19 s on a loaded host, against 40-60 s in chunks of any size);
# a larger type, a type read for all its properties, or named records go in chunks.
PG_VALUES_CHUNK = 500
PG_TYPE_READ_MAX = 200_000
SCOPE_CHUNK = 500    # object ids per scoped list or link read (an incremental pass)
# The link scan (``_Store.links``): threads (every core unless ``SCAN_THREADS_ENV`` says),
# rowid ranges per thread (a slow range is evened out by the others), the table size from
# which it splits, and the link rows a run keeps for the rules after it (~1.6 GB: the
# 2026-11-08 live store's run link types hold ~3.1M rows).
SCAN_THREADS_ENV = "PLX_PROACTIVE_SCAN_THREADS"
LINK_PARTS_PER_THREAD = 4
PARALLEL_LINK_ROWS = 200_000
LINK_CACHE_ROWS = 4_500_000
LINK_COLUMNS = ("link_id", "link_type", "from_object", "to_object", "tier", "markings")
KEEP_RUNS = 100
# The host cycle's full refresh (``after_build(scheduled=)``) starts no detector and makes
# no tool call after this many seconds: the cycle lock is not held unboundedly. Sized for a
# ~2.5M-object store (store-only detectors alone ~4 min there) next to a 1-4 h build.
REFRESH_BUDGET_SECONDS = 1500
# Of it, kept for the amounts and their plausibility check after the detectors (about 5 s
# warm and 50 s cold on the live store), so they too end within the bound.
AMOUNTS_RESERVE_SECONDS = 120
TIME_BUDGET = "time_budget"
SETTLED = ("verified", "reviewed")
CONFIRMING = ("verified", "reviewed", "corroborated")
QC_TYPES = ("RelPublicAppIncomingQcRecord", "QualityRecord")
# FileMaker's own receiving QC decision, held on the receiving line (lot -> line): a decided
# status or QC initials on the row. Not a QC record type, so it is named here.
QC_LINKS = ("Lot.qc_decided_receipt_line",)
# Receipt systems that hold no QC decision of any kind: never a "no QC" gap, unless a line
# is tied to a FileMaker line (which holds one)
NO_QC_SOURCE_BINDINGS = frozenset({"portal.sage_po_receipt_header", "bc.purchase_receipts"})
PRIORITY = ("high", "medium", "low")
# The order they start in: the store-only checks (the proactive events among them) side by
# side on threads (``run(threads=)``), the tool-call rules one after another on the run's
# own thread; each in its own time window (``DETECTOR_SECONDS``), so a slow one never
# leaves another unrun.
# An open purchase order with no requested-by date: never late, its own Home group.
UNDATED_KIND = "undated_po"
LATE_RULE = "late_or_unconfirmed_po"   # the rule Home's late count is read from
DETECTORS = ("late_or_unconfirmed_po", "late_customer_order",
             # the store-only proactive events (``operating.proactive_events``)
             "eta_slipped", "lot_attention", "accepted_quote_no_order",
             "invoice_quantity_mismatch",
             "po_receipt_quantity_mismatch", "receipt_without_qc", "expired_quote",
             "conflicting_quantity", "late_order", "order_without_customer_po",
             # the business alerts (``operating.proactive_business``): stock that cannot
             # cover open customer orders, overdue receivables, a purchase cost jump
             "stock_out_risk", "overdue_receivable", "unit_cost_jump",
             # the operations alerts (``operating.proactive_ops``): stalled work, ageing
             # balances, late or damaged production, unanswered Teams asks
             "mc_task_overdue", "mc_task_stuck", "mc_approval_pending", "portal_approval_pending",
             "planner_task_overdue", "aged_balance_worsening", "production_late",
             "assembly_damage_spike", "teams_unanswered_ask",
             # the signals a chief executive wants unasked (``operating.proactive_ceo``):
             # a customer ordering less, promised dates missed, a supplier getting later
             "customer_demand_drop", "promise_date_slip", "supplier_reliability",
             # order-to-cash alerts found in the business's traffic: a shipment no invoice
             # bills, the same customer order keyed twice
             "shipped_not_invoiced", "duplicate_customer_order",
             "recurring_component_damage",
             # the two largest pains (``operating.proactive_chasers``): customers chasing
             # where an order is, and quality holds aging with rejects rising
             "customer_chase", "quality_holds",
             # pains found in the mail (``operating.proactive_mail_pains``): expedites with
             # no answer, supplier shortage notices against open orders, suppliers chasing
             # us for payment (our chases of customers ride on ``overdue_receivable``),
             # customers asking to return goods or for a credit with no answer
             "expedite_requests", "supplier_shortage", "payment_chase", "return_requests",
             # receivables and payables (``operating.proactive_cash``): customers paying
             # slower, early-pay discounts missed, supplier invoices or payments twice
             "customer_payment_behaviour", "early_pay_discounts", "duplicate_payables",
             "returns_and_credits",
             # operations (``operating.proactive_stock``): stock not moving, lots expiring with
             # stock, assembly orders short of components, certificates expiring, freight
             # bills far above their carrier's and lane's norm
             "slow_moving_stock", "expiring_lot_stock", "assembly_component_shortfall",
             "certificate_expiry", "freight_cost_anomaly",
             # what each customer pays for the same product (``operating.proactive_prices``)
             "price_consistency",
             # sales (``operating.proactive_sales``): open quotes not converted to an order
             "stale_quote",
             # priority / fill lists customers send (``operating.business_documents``)
             "priority_fill_unmet",
             # data quality (``operating.proactive_quality``): duplicate customers and
             # suppliers, key fields missing, twins disagreeing, sources not captured lately
             "duplicate_parties", "key_fields_missing", "twin_disagreement",
             "stale_captures",
             # a record missing from, or differing in, the system it must reach
             # (``operating.proactive_sync``)
             "sync_gaps",
             # an item whose forecast demand exceeds on hand plus inbound
             # (``operating.demand_forecast``)
             "reorder_soon",
             # integrity (``operating.proactive_integrity``): stock written down by hand,
             # orders with blocked accounts, ledger batches that failed to post, recurring
             # tasks that missed a cycle
             "inventory_adjustment", "blocked_account_activity", "gl_posting_errors",
             "recurring_task_missed",
             # sheet readings (``operating.proactive_sheets``): remittances matched to open
             # invoices, supplier statements against our payables
             "cash_application", "statement_reconciliation",
             # promises we made, due or missed, and mail waiting on its owner's reply
             # (``operating.proactive_commitments``; one message read shared by both)
             "commitments", "needs_reply",
             # mail threads waiting on someone (``operating.proactive_threads``; one read
             # shared by all three): approvals unanswered, chases escalating, a customer's
             # or supplier's chase nobody of ours answered
             "approvals_waiting", "chase_escalation", "party_chases", "internal_chases",
             # what each person owes a reply in their own mail and one-to-one chats
             # (``operating.inbox_triage``), label and artwork changes requested, approved
             # and done (``operating.label_changes``), every label and artwork revision and
             # the open orders on a superseded one (``operating.label_revisions``, the
             # change rule's source of new label files and revision approvals)
             "inbox_triage", "label_revisions", "label_changes",
             # R&D and sample projects per customer (``operating.rd_projects``)
             "rd_projects",
             # unanswered vendor requests over the whole store's mail (after the late rule:
             # it judges the orders that rule finds open)
             "vendor_no_reply",
             # the rules that make tool calls, the last three proactive events among them:
             # the quick margins first (what they leave carries on), the customer orders a
             # late PO hits next, the attested-document cards last
             "negative_margin", "margin_erosion", "orders_at_risk",
             "vendor_terms_unapplied", "card_rules")
# Each detector's own time window (seconds from its start, never past the run's deadline,
# ``Budget.open``): a detector that reaches it is ``truncated`` (``time_budget``) and the
# others still run. Measured on the live store (2.5M objects, 2026-11-06, a loaded host):
# every store-only check under 3 min (the quantity conflicts ~2 min), so 10 min each is
# room; the tool-call rules read on the process pool, the customer orders a late PO hits
# weighing most (the briefing's first criterion); the margins' reads start with the run
# (``_margin_head_start``), so their window is what is left of them at their turn.
DETECTOR_SECONDS_DEFAULT = 600.0
# How long past its window a detector is waited for before it is cut off (``_run``): its
# reads stop at the window (the store's progress handler, ``_tick``); this bounds one that
# does not read the store meanwhile.
HARD_GRACE_SECONDS = 30.0
DETECTOR_SECONDS = {"conflicting_quantity": 900.0, "negative_margin": 900.0,
                    "orders_at_risk": 1500.0, "vendor_terms_unapplied": 600.0,
                    "card_rules": 900.0,
                    # the business alerts: one pass over orders, invoices or PO lines each
                    "stock_out_risk": 600.0, "overdue_receivable": 300.0,
                    "unit_cost_jump": 300.0,
                    # the operations alerts: one pass over tasks, gates, snapshots or
                    # production records each; the Teams asks scan every message's keys
                    "mc_task_overdue": 300.0, "mc_task_stuck": 300.0,
                    "mc_approval_pending": 300.0, "portal_approval_pending": 300.0,
                    "planner_task_overdue": 300.0,
                    "aged_balance_worsening": 300.0, "production_late": 300.0,
                    "assembly_damage_spike": 300.0, "teams_unanswered_ask": 600.0,
                    # the chief-executive signals: one pass over orders or receipts each; the
                    # margin erosion reads the earlier orders' margins on the pool
                    "customer_demand_drop": 300.0, "promise_date_slip": 300.0,
                    "supplier_reliability": 600.0, "margin_erosion": 600.0,
                    # measured alone on the gen 11 store (2026-11-08, a loaded host, links
                    # not prefetched): 421 s, 358 s and 279 s
                    "shipped_not_invoiced": 600.0, "duplicate_customer_order": 600.0,
                    "recurring_component_damage": 600.0,
                    # bounded by the open orders' and held batches' records (index reads
                    # only); the order join is shared with the late-order rule
                    "customer_chase": 300.0, "quality_holds": 120.0,
                    # the mail naming open orders and payables, by the link end indexes
                    # (dates first, at most ``MAX_MESSAGES`` a rule); the order join and
                    # the purchase-order source are shared with the late-order rules
                    "expedite_requests": 60.0, "supplier_shortage": 60.0,
                    "payment_chase": 60.0, "return_requests": 60.0,
                    # the Sage AR and AP documents (122k and 73k records) read once per run
                    # on the store's threads (``proactive_cash.read_records``), shared by
                    # the three rules
                    "customer_payment_behaviour": 300.0, "early_pay_discounts": 300.0,
                    "duplicate_payables": 300.0, "returns_and_credits": 300.0,
                    # operations: index reads of their record types and the links at those
                    # records only (``_Store.touching``), each well under a minute
                    # (but slow-moving stock: every stocked item's sales, assembly use and
                    # ledger entries by their posting events, 122 s alone and 199 s beside
                    # three other rules at host load 70-97, 2026-11-12)
                    "slow_moving_stock": 300.0, "expiring_lot_stock": 60.0,
                    "assembly_component_shortfall": 60.0, "certificate_expiry": 60.0,
                    "freight_cost_anomaly": 60.0,
                    # every posted invoice line of the last 360 days (two windows; 253 s live
                    # for one, 2026-11-08) and the purchase costs
                    # (the cost-jump rule's reads)
                    "price_consistency": 600.0, "stale_quote": 60.0, "priority_fill_unmet": 60.0,
                    # data quality: the party, order and item records by type and their
                    # identity links by type index (``_Store.of_type``); the capture times
                    # and one record per audience (one pass over the record table: 108 s
                    # on the whole store). Measured alone on the live store (2026-11-12, a
                    # loaded host): the duplicates 179 s before their links were read by
                    # type (155 s of it by end index); the missing fields 254 s, 147 s of
                    # it the run's one customer-order read (``_customer_orders``, shared
                    # with the late rules: the first rule to ask reads it, the others wait).
                    # At 120 s both were cut short on every served pass from 2026-11-10.
                    # Beside the label rules on 3 threads at host load 70-97 (2026-11-12,
                    # removals read per type): duplicates 165 s, missing fields 474 s (cut
                    # short at 420 s): 720 s leaves a third spare.
                    "duplicate_parties": 300.0, "key_fields_missing": 720.0,
                    "twin_disagreement": 300.0, "stale_captures": 240.0,
                    # every invoice line of 3 years read once (shared with nothing else)
                    "reorder_soon": 300.0,
                    # integrity: one type read each (ledger 7k, GL errors 12k and batches
                    # 47k, Planner 6k) and links at those records only, under a minute live;
                    # the blocked accounts' orders are judged by the run's one order read
                    # (``_customer_orders``, ``_po_source``: 146 s and 119 s cold on gen 11,
                    # shared with the late, stock and data-quality rules)
                    "inventory_adjustment": 60.0, "blocked_account_activity": 300.0,
                    "gl_posting_errors": 60.0, "recurring_task_missed": 60.0,
                    # sheet readings: the readings by index, the AR and AP documents
                    # through the cash rules' one read (``cash:ar``, ``cash:ap``)
                    "cash_application": 300.0, "statement_reconciliation": 300.0,
                    # the open orders' messages, dates first, read once for both
                    "commitments": 60.0, "needs_reply": 60.0,
                    # the mail the word index finds for both, read once (measured on the
                    # gen 11 store, 2026-11-08: see proactive_threads)
                    "approvals_waiting": 120.0, "chase_escalation": 60.0,
                    "party_chases": 60.0, "internal_chases": 60.0,
                    # the window's mail by the event index (rowid ranges side by side) and
                    # the label words (measured on the gen 11 store, 2026-11-08: 20 s and
                    # 51 s, a loaded host)
                    # (label_changes also waits on label_revisions' read, its source: 93 s
                    # on its own on the gen 12 store, 2026-11-09)
                    # (Postgres has no word index: the 2.25M attachment and file names
                    # are scanned, ``label_revisions._scan_named``; one chunk after another
                    # that took 336 s of the change rule's 465 s alone, 2026-11-12, and the
                    # change rule was cut short at 180 s on every served pass from
                    # 2026-11-10; now side by side, the server dropping unworded names)
                    # beside each other and the missing-fields rule on 3 threads at host
                    # load 70-97 (2026-11-12): the revisions 459 s, the changes 260 s after
                    # it (cut short at 420 s on the first such pass): 720 s each
                    "inbox_triage": 60.0, "label_changes": 720.0,
                    # the label words of attachment and file names, then their threads
                    # (measured on the gen 12 store, 2026-11-09: 48 to 61 s; the change
                    # rule waits on this read, so the two windows are one)
                    "label_revisions": 720.0,
                    "rd_projects": 120.0}
# Each detector's share of a run with a budget: the detectors one thread runs one after
# another (a lane; the tool-call rules on the run's own thread) split what is left of the
# run's deadline at each one's turn by their windows (``_share``): a detector is given at
# most its window and at most its weight's part of the time left, so the ones queued after
# a slow one always start with their part (time one leaves unused carries to the next).
# Before, the windows were taken whole one after another: the two margin rules (900 s and
# 600 s) spent the 1,380 s the detectors had of a 1,500 s pass and the customer orders at
# risk, the vendor terms and the card rules started with 0 s on every served pass
# (2026-11-12, passes 39-43).
#
# A run-thread rule on Postgres has no progress handler to cut a statement short at its
# window: its statement still running this long after it is cancelled (``_Store.
# interrupt_thread``), so it ends as ``truncated`` and the next rule keeps its share (the
# margin rules ran 1,344 s and 1,590 s past windows of 900 s and 600 s live).
RUN_THREAD_GRACE_SECONDS = 5.0
# Rules run at most once per this many hours (a pass between leaves their gaps, records and
# status as they were, named in the summary's ``not_due``): their reads take more than
# their share of a pass, so each other pass runs without them. Due again at once when the
# deployed engine's code is not the one their last run was by. Measured alone on the served
# store (2026-11-12, see the commit): the margin rules.
DETECTOR_CADENCE_HOURS: dict[str, float] = {"negative_margin": 12.0, "margin_erosion": 12.0}
_CADENCE_KEY = "cadence:"   # inbox meta key per rule: when its last run started, by what code
# Store-only detectors that run side by side on threads (each on its own store connection):
# every set-based rule but the customer-PO scan (it reads the runtime's mirror, on the
# run's own thread); the vendor rule after the late rule on the same thread (it reads that
# rule's order join).
THREADS_ENV = "PLX_PROACTIVE_DETECTOR_THREADS"
MAX_DETECTOR_THREADS = 6
# (the reply rule after the promise rule: it reads the messages that one read, ``memo``)
CHAINED = {"vendor_no_reply": LATE_RULE, "needs_reply": "commitments",
           "chase_escalation": "approvals_waiting", "party_chases": "approvals_waiting",
           "internal_chases": "approvals_waiting",
           # the Sage AP chases read the thread rules' mail window (proactive_threads)
           "payment_chase": "approvals_waiting"}
# The vendor-terms check is incremental: it keeps its readings per purchase order in the
# inbox (``event_cache``) and investigates only the ones whose mail thread changed.
INCREMENTAL = ("vendor_terms_unapplied",)
# Rules that make tool calls (the run principal's ``plx.process``/``plx.object``/
# ``plx.investigate``): not configured without a caller, never part of the store scope.
CALLING = frozenset({"card_rules", "orders_at_risk", "vendor_terms_unapplied",
                     "negative_margin", "margin_erosion"})
SET_BASED = frozenset(DETECTORS) - CALLING
# Detectors judged on the run's own thread, never handed to a lane's runner (``_run``): the
# tool-call rules and the customer-PO scan (the runtime's mirror and the caller's session
# may be bound to the thread that opened them).
ON_RUN_THREAD = CALLING | {"order_without_customer_po"}
STATES = ("open", "acknowledged", "resolved")
# Detectors whose claim rests on what sources do not hold (an absence), so on the run
# principal's grant: their gaps show only to a reader granted every recorded audience.
REQUIRES_GRANT = frozenset({"orders_at_risk", "accepted_quote_no_order", "vendor_no_reply",
                            "late_customer_order", "stock_out_risk",
                            "teams_unanswered_ask",
                            "customer_demand_drop", "promise_date_slip",
                            "supplier_reliability", "shipped_not_invoiced",
                            "customer_chase", "expedite_requests", "return_requests",
                            "supplier_shortage", "slow_moving_stock",
                            "assembly_component_shortfall", "commitments", "needs_reply",
                            "stale_quote", "key_fields_missing",
                            "reorder_soon",
                            "blocked_account_activity",
                            # a record missing from the system it must reach
                            "sync_gaps",
                            # resting on no reply in any copy (``proactive_threads``)
                            "approvals_waiting", "chase_escalation", "party_chases",
                            "internal_chases",
                            # no reply, no approval, nothing done in the thread; no
                            # copy of the newer revision naming the order
                            "inbox_triage", "label_changes", "label_revisions",
                            # no later activity in any thread of the project
                            "rd_projects"})
# Rule statuses of a host that has not set up a rule's input yet (the reading step's file):
# not a run that missed anything, so never incomplete (``read_inbox``'s ``not_run``).
SETUP_STATES = ("not_set_up",)
# Gap kinds whose wording sums over the whole run (money at stake): an open one is reworded
# only by a ``complete`` run of its detector.
WHOLE_ONLY = frozenset({"po_impact_margin"})
LATENESS_GRAPH = Path(__file__).resolve().parents[1] / "ontology" / "schema" / "process" \
    / "po_delivery_window.json"
UNREADABLE = '["unreadable"]'   # a markings value no reader holds (fail closed)
RECEIPT_LINKS = ("ReceiptLine.same_record_as", "ReceiptLine.receives_line",
                 "ReceiptLine.line_of")
CONFIRMATION_TYPE = "SupplierConfirmation"
CONFIRMATION_LINK = po_status.CONFIRMS   # confirmation -> the order it confirms
CONFIRMED_DATE = "delivery_date"
DRAFT_LABEL = "Draft supplier message (not sent)"
DRAFT_STATUS = "draft_not_sent"
MAX_DRAFT_TEXT = 1500
MAX_DRAFT_LINES = 6
MAX_DRAFT_CITATIONS = 20   # the catalog's workbench.draft.record citation_list max_items

Cite = tuple  # (l1_handle, object_id, canonical markings)


# --------------------------------------------------------------------------- store reads

def scan_threads(environ: Mapping[str, str] | None = None) -> int:
    """Threads a link scan reads with: every core (``PLX_PROACTIVE_SCAN_THREADS``
    overrides; 1: one connection, no thread)."""
    raw = (os.environ if environ is None else environ).get(SCAN_THREADS_ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return 1
    return max(1, os.cpu_count() or 1)


def _connect(path: Path, file_only: bool = False) -> sqlite3.Connection:
    """A read-only connection to the store at ``path``: Postgres when it serves it, else
    the file. ``file_only``: the file itself, whatever Postgres serves (a scratch store
    of a few records, ``operating.autopilot_fast``)."""
    from operating.pg import ontology_reader
    if not file_only and ontology_reader.pg_only():
        # Postgres is the only store: read it whole or not at all (an error, never an
        # empty run over a store being loaded)
        problem = ontology_reader.whole()
        if problem is not None:
            raise ostore.StoreError("store_not_whole", problem)
    if not file_only and ontology_reader.serves(path):
        # PLX_ONTOLOGY_PG: the Postgres ontology (current to the last delta layer), the
        # detectors' SQL run as written (operating/pg/sqlite_compat.py)
        from operating.pg.sqlite_compat import PgCompatConnection
        return PgCompatConnection(                        # type: ignore[return-value]
            ontology_reader.dsn(), statement_timeout_ms=statement_timeout_ms())
    # read only, one thread at a time (``_Store.conn`` is per thread); the run's own thread
    # may still set its progress handler and close it
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # No query of a detector may build a transient index over a whole table: on a
    # store of millions of value cells that index is a multi-GB temp file and minutes.
    conn.execute("PRAGMA automatic_index = OFF")
    return conn


def _link_row(row: Sequence[Any]) -> tuple:
    """A link row as kept (``_LinkRows``): its type, tier and markings shared strings."""
    return (row[0], row[1], sys.intern(row[2]), row[3], row[4], sys.intern(row[5]),
            sys.intern(row[6]), bool(row[7]))


class _ThreadConn:
    """A thread's store connection, closed when the thread's locals are freed (the thread
    ended). The finalizer holds the connection and the release callback, never this object."""

    def __init__(self, conn: Any, release: Callable[[Any], None]) -> None:
        self.conn = conn
        import weakref
        weakref.finalize(self, release, conn)


class _LinkRows:
    """The run's link rows by link type, read once (``_Store.links``): each row
    (rowid, link_id, link_type, from_object, to_object, tier, markings, gone), ``gone``
    when either end is removed; every current link not contradicted, in rowid order.
    Kept up to ``LINK_CACHE_ROWS`` rows."""

    def __init__(self) -> None:
        self.by_type: dict[str, list[tuple]] = {}
        self.kept = 0
        self.lock = threading.Lock()


# ``_Store.values`` on the Postgres ontology: the ``object_values`` view's columns read from
# its tables (a record's versions, their current value sources, the cells), the same rows
_PG_VALUES_SQL = (
    "SELECT o.object_id, o.object_type, f.property, k.value_type, c.value, k.unit, "
    "k.quantity_role, v.l1_handle, k.markings, COALESCE(s.lineage, v.lineage) AS lineage "
    "FROM objects o JOIN object_versions v ON v.object_id = o.object_id "
    "JOIN value_sources s ON s.ver = v.rowid AND s.superseded_at IS NULL "
    "JOIN value_cells c ON c.src = s.src JOIN value_kinds k ON k.kind = c.kind "
    "JOIN value_fields f ON f.field = c.field WHERE ")


class _Store:
    """One store generation, read only, every read over current records not removed.

    The link table has no index by link type, so each link read scans it whole (about a
    minute on the live store, ~20 reads a run): ``links`` reads every type it is asked for
    once per run, the table split into rowid ranges read side by side on ``threads``
    connections, and keeps the rows for the rules after it (``prefetch_links``).

    ``conn`` is per thread (the detectors run side by side, ``run(threads=)``): each thread
    reads on its own read-only connection, all closed with the store."""

    def __init__(self, path: Path, *, threads: int | None = None,
                 scope: Iterable[str] | None = None,
                 audience_keys: Iterable[str] | None = None, file_only: bool = False) -> None:
        from operating.pg import ontology_reader
        self.file_only = file_only   # the file at ``path`` only (``_connect``)
        if not Path(path).is_file() and (file_only or not ontology_reader.pg_only()):
            raise ostore.StoreError("store_missing")
        self.path = Path(path)
        # an incremental pass (``operating.proactive_incremental``): the records it may read
        # whole types of (every record a changed record's rules read, per their declared
        # inputs); a whole-type read lists only these, a link read only links touching
        # them. A read naming its objects (``objects=``, ``handles=``) is never narrowed.
        self.limit: frozenset[str] | None = None if scope is None else frozenset(scope)
        self._scope_ids: list[str] = sorted(self.limit) if self.limit is not None else []
        self._known_keys = None if audience_keys is None else frozenset(audience_keys)
        self.whole_rows = _LinkRows()   # links read whole in a scoped store (``whole``)
        # the populations the rules judged against (``population``): key -> digest, and the
        # rules that read each ("*": every rule, a filter over all their gaps)
        self.populations: dict[str, str] = {}
        self.population_readers: dict[str, set[str]] = defaultdict(set)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conn_thread: dict[int, threading.Thread] = {}   # id(conn) -> the thread it reads on
        self._helpers: list[threading.Thread] = []
        self._conns_lock = threading.Lock()
        # idle connections the ranged scans' parts read on (``_part_conn``): kept for the
        # run, never one opened and closed per range
        self._parts: list[Any] = []
        self.interrupt: Callable[[], int] | None = None
        self.left: Callable[[], float] | None = None
        # the file the recent-mail read keeps what it judged in (set by a run over an inbox)
        self.cursor_path: Path | None = None
        self.pin: Callable[[], Callable[[], int]] | None = None
        problem = ostore.format_problem(self.conn)
        if problem is not None:
            self.close()
            raise ostore.StoreError("store_format_mismatch", problem)
        from operating.pg.sqlite_compat import PgCompatConnection
        # the Postgres ontology (``_connect``): value reads join its value tables directly
        self.pg = isinstance(self.conn, PgCompatConnection)
        self.digest = self.meta("content_digest")
        self.threads = scan_threads() if threads is None else max(1, threads)
        self.link_rows = _LinkRows()
        # the budget's SQLite progress handler (``Budget.interrupt``, ``interrupt``), on
        # every connection this store opens: a scan stops at the deadline as the detectors'
        # queries do (it reads the window of the thread running the statement)
        self._memo: dict[str, Any] = {}
        self._making: dict[str, threading.Lock] = {}
        self._removed: frozenset[str] | None = None
        self._removed_of: dict[str, frozenset[str]] = {}
        self._removed_lock = threading.Lock()

    @property
    def scoped(self) -> bool:
        """Whether this thread's reads are narrowed to the scope (not inside ``whole``)."""
        return self.limit is not None and not getattr(self._local, "whole", False)

    @contextlib.contextmanager
    def whole(self) -> Iterator[None]:
        """Reads on this thread over the whole store, even in an incremental pass: what a
        rule judges a subject against that sums over every record (a population)."""
        before = getattr(self._local, "whole", False)
        self._local.whole = True
        try:
            yield
        finally:
            self._local.whole = before

    def judging(self, rule: str | None) -> None:
        """The rule this thread judges now (``population`` names its readers)."""
        self._local.rule = rule

    def population(self, key: str, make: Callable[[], Any],
                   digest: Callable[[Any], Any] = lambda made: made) -> Any:
        """What ``make()`` reads over the whole store (never the scope), once per run, its
        ``digest`` kept with the rule reading it: an incremental pass judges a rule whole
        when one of its populations moved (``run(scope=)``), since that moves subjects
        it did not read."""
        rule = getattr(self._local, "rule", None) or "*"

        def read() -> Any:
            with self.whole():
                made = make()
            text = json.dumps(_canonical(digest(made)), sort_keys=True)
            with self.link_rows.lock:
                self.populations[key] = hashlib.sha256(text.encode()).hexdigest()[:32]
            return made
        made = self.memo("population:" + key, read)
        with self.link_rows.lock:
            self.population_readers[key].add(rule)
        return made

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's read-only connection (opened on its first read)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _connect(self.path, self.file_only)
            if self.interrupt is not None:
                conn.set_progress_handler(self.interrupt, 10000)
            with self._conns_lock:
                self._conns.append(conn)
                self._conn_thread[id(conn)] = threading.current_thread()
            self._local.conn = holder = _ThreadConn(conn, self._release)
            return holder.conn
        return conn.conn

    def _part_conn(self) -> Any:
        """An idle connection of the ranged scans' parts, else a new one (opened once per
        part thread at most and kept for the run, closed with the store; each is one of
        the store's connections, so ``interrupt_all`` reaches it)."""
        with self._conns_lock:
            if self._parts:
                return self._parts.pop()
        conn = _connect(self.path, self.file_only)
        with self._conns_lock:
            self._conns.append(conn)
        return conn

    def _part_done(self, conn: Any, ok: bool) -> None:
        """A part's connection back to the idle ones; after a failed read (an interrupt,
        a lost server, a replaced generation) it is closed instead."""
        with contextlib.suppress(Exception):
            conn.set_progress_handler(None, 0)
        with self._conns_lock:
            if ok and conn in self._conns:
                self._parts.append(conn)
                return
            self._conns = [c for c in self._conns if c is not conn]
        with contextlib.suppress(Exception):
            conn.close()

    def _release(self, conn: Any) -> None:
        """A thread ended: its connection is closed and no longer kept (a Postgres server
        has a fixed number of connections; threads of the lanes and pools come and go)."""
        with self._conns_lock:
            self._conns = [c for c in self._conns if c is not conn]
            self._conn_thread.pop(id(conn), None)
        with contextlib.suppress(Exception):
            conn.close()

    def watch(self, interrupt: Callable[[], int] | None,
              pin: Callable[[], Callable[[], int]] | None = None,
              left: Callable[[], float] | None = None) -> None:
        """Every connection's progress handler (None: none); ``pin()``: the handler for
        another thread reading for this one (``Budget.pinned``); ``left()``: the seconds
        left of the asking thread's detector window (a read that can stop early bounds
        itself by it; Postgres has no progress handler to cut a read short)."""
        self.interrupt = interrupt
        self.left = left
        self.pin = pin if interrupt is not None else None
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            if interrupt is None:
                conn.set_progress_handler(None, 0)
            else:
                conn.set_progress_handler(interrupt, 10000)

    def memo(self, key: str, make: Callable[[], Any]) -> Any:
        """What ``make()`` reads, once per run (several rules ask for it; rules running
        side by side wait for the first one's read instead of reading it again)."""
        with self.link_rows.lock:
            if key in self._memo:
                return self._memo[key]
            making = self._making.setdefault(key, threading.Lock())
        with making:
            with self.link_rows.lock:
                if key in self._memo:
                    return self._memo[key]
            made = make()
            with self.link_rows.lock:
                return self._memo.setdefault(key, made)

    def interrupt_all(self) -> None:
        """Abort whatever statement each of this store's connections is running."""
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.interrupt()

    def interrupt_thread(self, thread: threading.Thread) -> None:
        """Abort the statement ``thread``'s own connection is running (a run-thread rule
        past its window on Postgres, which has no progress handler)."""
        with self._conns_lock:
            conns = [c for c in self._conns if self._conn_thread.get(id(c)) is thread]
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.interrupt()

    def spawn(self, name: str, work: Callable[[], None]) -> None:
        """Run ``work`` on a thread of its own that reads this store (a read a rule starts
        early and collects later); ``close`` stops and waits for it before it closes the
        connections it reads on (a connection closed under a running statement can crash
        the process)."""
        thread = threading.Thread(target=work, name=name, daemon=True)
        with self._conns_lock:
            self._helpers.append(thread)
        thread.start()

    def close(self) -> None:
        with self._conns_lock:
            helpers, self._helpers = self._helpers, []
        if any(t.is_alive() for t in helpers):
            self.interrupt_all()
            for thread in helpers:
                thread.join(HELPER_JOIN_SECONDS)
        running = {t for t in helpers if t.is_alive()}
        with self._conns_lock:
            conns, self._conns = self._conns, []
            self._parts = []
            owners = {id(c): self._conn_thread.pop(id(c), None) for c in conns}
        for conn in conns:
            if owners.get(id(conn)) in running:
                # a helper still reading past its join: its connection is never closed
                # under the running statement; it closes when that thread ends (_release)
                print(f"proactive_engine: helper {owners[id(conn)].name} still running at "
                      "close; its connection is left to close with it", file=sys.stderr)
                continue
            conn.close()

    def meta(self, key: str) -> str | None:
        if key == "content_digest" and not isinstance(self.conn, sqlite3.Connection):
            # Postgres: the data it holds now (``ontology_reader.change_stamp``), so a run
            # over data a delta layer or migration changed since is told apart
            from operating.pg import ontology_reader
            stamp = ontology_reader.change_stamp()
            if stamp is not None:
                return stamp
        row = self.conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def values(self, types: Sequence[str], props: Sequence[str] | None = None, *,
               objects: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        """object_id -> {"type", "props": {property: [row, ...]}}; ``objects``: only these.

        Object first, in chunks of ``VALUES_CHUNK`` ids: the records of the types (not
        removed) are listed by the type index, then each chunk's values are read by object
        id (version, source, cell). One query over a whole type let the planner start from
        the property instead and index every value cell of the store on the fly (a
        multi-GB temp file; the home's engine run took 23 minutes on 2.5M objects)."""
        params: dict[str, Any] = {"types": json.dumps(list(types))}
        listed = ("SELECT o.object_id FROM objects o "
                  "WHERE o.object_type IN (SELECT value FROM json_each(:types)) AND NOT "
                  + ostore.removed_at_sql("o.object_id"))
        if objects is not None or self.scoped:
            # named objects, or the scope's (an incremental pass): listed by object id
            listed += " AND o.object_id IN (SELECT value FROM json_each(:objects))"
            named = sorted(set(objects)) if objects is not None else self._scope_ids
            ids = []
            for start in range(0, len(named), SCOPE_CHUNK):
                params["objects"] = json.dumps(named[start:start + SCOPE_CHUNK])
                ids.extend(row[0] for row in self.conn.execute(listed, params))
            ids.sort()
        else:
            ids = [row[0] for row in self.conn.execute(listed + " ORDER BY o.object_id",
                                                       params)]
        chunk_params: dict[str, Any] = {}
        size = VALUES_CHUNK
        if getattr(self, "pg", False):
            # the view's columns from its tables, and the properties' fields named first:
            # through the ``object_values`` view the planner looked the fields up again for
            # every value source of every record (1.3 s per 500 records)
            fields = ""
            if props is not None:
                fields = " AND c.field IN (SELECT value FROM json_each(:fields))"
                chunk_params["fields"] = json.dumps(sorted(row[0] for row in self.conn.execute(
                    "SELECT field FROM value_fields WHERE property IN (SELECT value FROM "
                    "json_each(:props))", {"props": json.dumps(list(props))})))
            order = " ORDER BY o.object_id, f.property, v.l1_handle"
            if objects is None and not self.scoped and props is not None \
                    and len(ids) <= PG_TYPE_READ_MAX:
                # the whole type in one statement, the records the listing gave (same
                # order: the store's own object id order)
                return self._shape(self.conn.execute(
                    _PG_VALUES_SQL + "o.object_type IN (SELECT value FROM json_each(:types)) "
                    "AND NOT " + ostore.removed_at_sql("o.object_id") + fields + order,
                    {**chunk_params, "types": params["types"]}), ids)
            sql = (_PG_VALUES_SQL + "o.object_id IN (SELECT value FROM json_each(:chunk))"
                   + fields + order)
            size = PG_VALUES_CHUNK
        else:
            sql = ("SELECT o.object_id, o.object_type, v.property, v.value_type, v.value, "
                   "v.unit, v.quantity_role, v.l1_handle, v.markings, v.lineage FROM objects o "
                   "JOIN object_values v ON v.object_id = o.object_id "
                   "WHERE o.object_id IN (SELECT value FROM json_each(:chunk)) "
                   "AND v.superseded_at IS NULL")
            if props is not None:
                sql += " AND v.property IN (SELECT value FROM json_each(:props))"
                chunk_params["props"] = json.dumps(list(props))
            sql += " ORDER BY o.object_id, v.property, v.l1_handle"
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), size):
            chunk_params["chunk"] = json.dumps(ids[start:start + size])
            self._shape(self.conn.execute(sql, chunk_params), None, out)
        return out

    @staticmethod
    def _shape(rows: Iterable[Any], listed: Sequence[str] | None,
               out: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
        """``values``' rows by object (``listed``: only the records the listing gave)."""
        out = {} if out is None else out
        keep = None if listed is None else frozenset(listed)
        for row in rows:
            if keep is not None and row["object_id"] not in keep:
                continue
            entry = out.setdefault(row["object_id"], {"type": row["object_type"], "props": {}})
            entry["props"].setdefault(row["property"], []).append(dict(row))
        return out

    def links(self, types: Sequence[str], tiers: Sequence[str] | None = None, *,
              held: bool = True) -> list[dict[str, Any]]:
        """Current links of these types, never contradicted, in the store's (rowid)
        order; ``held``: only those between records not removed."""
        rows = self._link_rows(list(dict.fromkeys(types)))
        tier_set = None if tiers is None else frozenset(tiers)
        return [dict(zip(LINK_COLUMNS, row[1:7])) for row in rows
                if (tier_set is None or row[5] in tier_set) and not (held and row[7])]

    def prefetch_links(self, types: Iterable[str]) -> None:
        """Read the links of these types in one scan, kept for the rules after it."""
        self._link_rows(list(dict.fromkeys(types)))

    def _link_rows(self, types: list[str]) -> list[tuple]:
        cache = self.link_rows if self.limit is None or self.scoped else self.whole_rows
        with cache.lock:
            missing = [t for t in types if t not in cache.by_type]
            found = {t: cache.by_type[t] for t in types if t in cache.by_type}
            if missing:
                read = self._scan_links(missing)
                for name in missing:
                    rows = read.get(name, [])
                    found[name] = rows
                    if cache.kept + len(rows) <= LINK_CACHE_ROWS:
                        cache.by_type[name] = rows
                        cache.kept += len(rows)
        if len(types) == 1:
            return found[types[0]]
        return sorted((row for name in types for row in found[name]), key=lambda r: r[0])

    def _scan_links(self, types: Sequence[str]) -> dict[str, list[tuple]]:
        """type -> its rows (``_LinkRows``), in rowid order (``_ranged``)."""
        out: dict[str, list[tuple]] = defaultdict(list)
        if self.scoped:
            # an incremental pass: the links with an end in its scope, by the end indexes
            for row in self.touching(self._scope_ids, types):
                out[row[2]].append(row)
            return out
        if self.limit is not None:
            # a population of an incremental pass: each type whole, by the end index of
            # its smaller end type (every record of it), never a scan of the link table
            rest = []
            for link_type, ends in _link_ends(types).items():
                sized = sorted((self._type_count(e), e) for e in ends)
                if not sized or sized[0][0] > WHOLE_BY_INDEX_MAX:
                    rest.append(link_type)
                    continue
                ids = [r[0] for r in self.conn.execute(
                    "SELECT object_id FROM objects WHERE object_type = ?", (sized[0][1],))]
                for row in self.touching(ids, [link_type]):
                    out[row[2]].append(row)
            if not rest:
                return out
            types = rest
        # a whole scan: the records removed now are read once per end type and each row's
        # ends looked up in them (``_gone``), never a removal probe per end of every link
        # row (two index probes per row, a correlated subquery per row on Postgres)
        self._prefetch_gone(types)
        gone = self._gone
        plain = ("SELECT l.rowid, l.link_id, l.link_type, l.from_object, l.to_object, l.tier, "
                 "l.markings FROM link_rows l "
                 "WHERE l.link_type IN (SELECT value FROM json_each(:types)) "
                 "AND l.superseded_at IS NULL AND l.tier <> 'contradicted'")

        def shape(r: Sequence[Any]) -> tuple:
            return _link_row((*r[:7], gone(r[3]) or gone(r[4])))
        for rows in self._ranged("link_rows", "l", plain, " ORDER BY l.rowid",
                                 {"types": json.dumps(list(types))}, shape):
            # the ranges in rowid order, each in rowid order
            for row in self._settle_gone(rows):
                out[row[2]].append(row)
        return out

    def removed(self, types: Iterable[str] | None = None) -> frozenset[str]:
        """The records of ``types`` not held by its source now (``ostore.removed_at_sql``
        at the store's latest knowledge), one indexed read per type, each read once per
        run. None: every record in the store (one scan of every record: 55-77 s on the
        served store, 2026-11-12), only for a read whose record types are unknown."""
        # its own lock: a link scan asks for it while holding the link cache's lock,
        # which ``memo`` takes too
        with self._removed_lock:
            if types is None:
                if self._removed is None:
                    self._removed = frozenset(
                        r[0] for r in self.conn.execute(ostore.removed_ids_sql()))
                return self._removed
            out: set[str] = set()
            for object_type in sorted(set(types)):
                if object_type not in self._removed_of:
                    self._removed_of[object_type] = frozenset(
                        r[0] for r in self.conn.execute(
                            ostore.removed_ids_sql(object_type_sql="?"),
                            (object_type, object_type)))
                out |= self._removed_of[object_type]
            return frozenset(out)

    def _gone(self, object_id: str) -> bool:
        """Whether a record is not held by its source now, among the types ``removed``
        read so far (a record id opens with its type: ``project.object_id_of``); a type not
        read yet: False here, settled by ``_settle_gone`` after the read (the link scan's
        threads never read the store for it)."""
        known = self._removed_of.get(object_id.split(":", 1)[0])
        return known is not None and object_id in known

    def _prefetch_gone(self, link_types: Iterable[str]) -> None:
        """``removed`` of the declared end types of ``link_types`` (``_link_ends``), read
        before a link scan on this thread."""
        self.removed({e for ends in _link_ends(list(link_types)).values() for e in ends})

    def _settle_gone(self, rows: list[tuple]) -> list[tuple]:
        """``rows`` with ``gone`` read again for each row with an end whose type was not
        read before the scan (an end type the schema does not declare): that type read
        now, on this thread."""
        def kind(oid: str) -> str:
            return oid.split(":", 1)[0]
        missing = {kind(o) for r in rows for o in (r[3], r[4])} - set(self._removed_of)
        if not missing:
            return rows
        self.removed(missing)
        return [(*r[:7], self._gone(r[3]) or self._gone(r[4]))
                if kind(r[3]) in missing or kind(r[4]) in missing else r for r in rows]

    def _type_count(self, object_type: str) -> int:
        return self.memo("count:" + object_type, lambda: self.conn.execute(
            "SELECT count(*) FROM objects WHERE object_type = ?", (object_type,)).fetchone()[0])

    def touching(self, objects: Sequence[str], types: Sequence[str] | None = None,
                 *, every: bool = False) -> list[tuple]:
        """The current links (``_LinkRows`` rows) with an end at one of ``objects``, of
        ``types`` (None: every type), in rowid order, read by the link table's end indexes;
        ``every``: contradicted ones too (a walk of what a record is tied to)."""
        base = ("SELECT l.rowid, l.link_id, l.link_type, l.from_object, l.to_object, l.tier, "
                "l.markings, (" + ostore.removed_at_sql("l.from_object") + " OR "
                + ostore.removed_at_sql("l.to_object") + ") AS gone FROM link_rows l "
                "WHERE l.superseded_at IS NULL"
                + ("" if every else " AND l.tier <> 'contradicted'"))
        params: dict[str, Any] = {}
        if types is not None:
            base += " AND l.link_type IN (SELECT value FROM json_each(:types))"
            params["types"] = json.dumps(list(types))
        found: dict[str, tuple] = {}
        ids = sorted(set(objects))
        for start in range(0, len(ids), SCOPE_CHUNK):
            params["chunk"] = json.dumps(ids[start:start + SCOPE_CHUNK])
            for end in ("from_object", "to_object"):
                for r in self.conn.execute(
                        base + f" AND l.{end} IN (SELECT value FROM json_each(:chunk))",
                        params):
                    found.setdefault(r[1], _link_row(r))
        return sorted(found.values(), key=lambda r: r[0])

    def of_type(self, link_type: str) -> list[tuple]:
        """Every current link of one type, contradicted ones too (``_LinkRows`` rows, in
        rowid order), read once per run by the link table's type index; each row's ends
        looked up in the records removed now (``_gone``), never a removal probe per row.
        A type of tens of thousands of rows reads in seconds this way, where a read by the
        end indexes at every record of its end type took 40 s a type (2026-11-12 live
        store: ``Customer.same_record_as`` 30k rows 1.5 s by type, 40 s by end)."""
        def read() -> list[tuple]:
            self._prefetch_gone([link_type])
            gone = self._gone
            sql = ("SELECT l.rowid, l.link_id, l.link_type, l.from_object, l.to_object, "
                   "l.tier, l.markings FROM link_rows l WHERE l.link_type = ? "
                   "AND l.superseded_at IS NULL")
            if self.scoped:
                # an incremental pass: its scope's ends only, as ``touching`` reads them
                return self.touching(self._scope_ids, [link_type], every=True)
            return sorted(self._settle_gone(
                [_link_row((*r[:7], gone(r[3]) or gone(r[4])))
                 for r in self.conn.execute(sql, (link_type,))]), key=lambda r: r[0])
        return self.memo("of_type:" + link_type, read)

    def _ranged(self, table: str, alias: str, sql: str, tail: str,
                params: Mapping[str, Any], shape: Callable[[Any], Any]) -> list[list[Any]]:
        """``sql`` (over ``table`` as ``alias``, then ``tail``) read by rowid ranges of the
        table side by side, one connection per thread (each checked to read this
        generation), the ranges' rows in rowid order of the ranges; a small table, one
        thread or a generation replaced meanwhile: one read on this connection."""
        top = self.conn.execute(f"SELECT max(rowid) FROM {table}").fetchone()[0] or 0
        if self.threads > 1 and top >= PARALLEL_LINK_ROWS:
            from concurrent.futures import ThreadPoolExecutor

            step = top // (self.threads * LINK_PARTS_PER_THREAD) + 1
            ranged = sql + f" AND {alias}.rowid >= :lo AND {alias}.rowid < :hi" + tail
            # the asking thread's window, not the part's own thread's
            handler = self.pin() if self.pin is not None else self.interrupt

            def part(lo: int) -> list[Any]:
                conn, ok = self._part_conn(), False
                try:
                    conn.set_progress_handler(handler, 10000) if handler is not None \
                        else conn.set_progress_handler(None, 0)
                    # a generation replaced under the run (Postgres is never replaced: a
                    # delta layer changes it in place, as the run's own reads see)
                    held = conn.execute("SELECT value FROM store_meta WHERE key = "
                                        "'content_digest'").fetchone()
                    if isinstance(conn, sqlite3.Connection) and \
                            (held[0] if held else None) != self.digest:
                        raise ostore.StoreError("store_replaced")
                    got = [shape(r) for r in conn.execute(ranged, {**params, "lo": lo,
                                                                   "hi": lo + step})]
                    ok = True
                    return got
                finally:
                    self._part_done(conn, ok)
            try:
                with ThreadPoolExecutor(self.threads) as pool:
                    return list(pool.map(part, range(0, top + 1, step)))
            except ostore.StoreError:
                pass   # another generation is served now: read this one on this connection
        return [[shape(r) for r in self.conn.execute(sql + tail, dict(params))]]

    def versions(self, object_type: str | None = None, handles: Iterable[str] | None = None,
                 objects: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Current versions (handle, object, markings) of a type, of these handles or of
        these objects."""
        sql = ("SELECT v.l1_handle, v.object_id, v.markings, o.object_type FROM object_versions v "
               "JOIN objects o ON o.object_id = v.object_id WHERE v.superseded_at IS NULL AND NOT "
               + ostore.removed_at_sql("o.object_id"))
        params: dict[str, Any] = {}
        if object_type is not None:
            sql += " AND o.object_type = :type"
            params["type"] = object_type
            if handles is None and objects is None and self.scoped:
                objects = self._scope_ids   # a whole type in an incremental pass: its scope
        if handles is not None:
            sql += " AND v.l1_handle IN (SELECT value FROM json_each(:handles))"
            params["handles"] = json.dumps(sorted(set(handles)))
        if objects is not None:
            sql += " AND v.object_id IN (SELECT value FROM json_each(:objects))"
            params["objects"] = json.dumps(sorted(set(objects)))
        return [dict(row) for row in self.conn.execute(sql, params)]

    def audience_keys(self) -> set[str]:
        """Every audience key a record of the store sits in (removed ones too), read once
        per run by object rowid ranges side by side (``_ranged``): a scan of every record
        (minutes on the live store), asked by several rules."""
        if self._known_keys is not None:
            # an incremental pass: the generation's keys and its changed records' (kept
            # beside the inbox, ``operating.proactive_incremental``), never a whole scan
            return set(self._known_keys)

        return set(self.memo("audience_keys", lambda: frozenset(self.audience_firsts())))

    def audience_firsts(self) -> dict[str, str]:
        """audience key -> the smallest object id in it, over every record (removed ones
        too), in one scan per run by object rowid ranges side by side (``_ranged``): the
        audience keys (``audience_keys``) and the data-quality rule's one record per
        audience (``proactive_quality.stale_captures``) read it, never a scan each."""
        def read() -> dict[str, str]:
            firsts: dict[str, str] = {}
            for rows in self._ranged(
                    "objects", "o",
                    "SELECT o.audience_key, MIN(o.object_id) FROM objects o WHERE 1 = 1",
                    " GROUP BY o.audience_key", {}, tuple):
                for key, oid in rows:   # each range's smallest, the smallest of them kept
                    if key not in firsts or oid < firsts[key]:
                        firsts[key] = oid
            return firsts
        return dict(self.memo("audience_firsts", read))

    def scope(self) -> list[tuple[str, str, int]]:
        """(audience key, current markings, objects): the whole store the run covered,
        counted by object rowid ranges side by side (``_ranged``) and summed."""
        counts: Counter = Counter()
        for rows in self._ranged(
                "objects", "o",
                "SELECT o.audience_key, v.markings, count(*) FROM objects o JOIN "
                "object_versions v ON v.object_id = o.object_id AND v.superseded_at IS NULL "
                "WHERE NOT " + ostore.removed_at_sql("o.object_id"),
                " GROUP BY o.audience_key, v.markings", {}, tuple):
            for key, marks, n in rows:
                counts[(key, marks)] += n
        return [(key, marks, n) for (key, marks), n in sorted(counts.items())]


WHOLE_BY_INDEX_MAX = 300_000   # an end type of up to this many records: read by index


def _link_ends(types: Sequence[str]) -> dict[str, list[str]]:
    """link type -> its declared end types (schema ``from``/``to``): a population reads
    the type whole through the end index of the smaller one (every link of the type has
    that end); none declared: a scan of the link table."""
    from ontology.schema import load_schema

    declared = load_schema().link_types
    return {t: [e for e in ((declared.get(t) or {}).get("from"), (declared.get(t) or {})
                            .get("to")) if isinstance(e, str) and e] for t in types}


def _canonical(value: Any) -> Any:
    """``value`` as JSON in one order (a population's digest)."""
    if isinstance(value, Mapping):
        return sorted(([_canonical(k), _canonical(v)] for k, v in value.items()), key=repr)
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _markings(*texts: str | None) -> str:
    """The union of markings values, canonical; anything unreadable never shows."""
    held: set[str] = set()
    for text in texts:
        try:
            parsed = json.loads(text) if text else []
        except (TypeError, ValueError):
            return UNREADABLE
        if not isinstance(parsed, list) or not all(isinstance(m, str) for m in parsed):
            return UNREADABLE
        held.update(parsed)
    try:
        return ostore.markings_text(held)
    except ostore.StoreError:
        return UNREADABLE


def _cites(rows: Iterable[Mapping[str, Any]], *extra_markings: str | None) -> list[Cite]:
    out: dict[str, Cite] = {}
    for row in rows:
        handle = row.get("l1_handle")
        if isinstance(handle, str) and handle and handle not in out:
            out[handle] = (handle, row.get("object_id"),
                           _markings(row.get("markings"), *extra_markings))
    return list(out.values())


_clusters = po_status.clusters   # member -> its settled cluster's least object id


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _gap(kind: str, detector: str, object_id: str, object_type: str, key: str, *,
         summary: str, missing: str, why: str, priority: str, about: tuple[str, str],
         groups: Sequence[Sequence[Cite]], extra: Sequence[Cite] = (),
         days_late: int | None = None, event: Mapping[str, Any] | None = None,
         **facts: Any) -> dict[str, Any] | None:
    """``days_late``: the days a purchase order open by the one status definition is past its
    requested-by date (None: not late; ``late_order``'s line-level lateness is another gap
    kind and never carries it); the inbox lists every late purchase order first, longest late
    first, whatever its kind's priority. ``event``: the facts the briefing ranks by
    (``days``: days between two cited dates; ``amount``: a cited figure, shown only with
    every group or, with a ``handle``, only to a reader who may open that citation;
    ``orders_affected``: a count of cited records), kept beside the follow-up. ``facts``:
    enum words the pages read (``confirmation``: ``none_found``, ``found_unverified`` or
    ``confirmed``; ``due_basis``: where the due date comes from)."""
    groups = [list(group) for group in groups]
    if not groups or not all(groups):
        return None   # a claim with an uncited part is not raised
    handles = list(dict.fromkeys([c[0] for g in groups for c in g] + [c[0] for c in extra]))
    who = pg._who(object_type, {"about": about, "handles": handles})
    return {"id": "pe-" + hashlib.sha256(f"{kind}|{object_id}|{key}".encode("utf-8"))
            .hexdigest()[:16],
            "kind": kind, "detector": detector, "object_id": object_id,
            "object_type": object_type, "summary": pg._clip(summary),
            "groups": groups, "extra": list(extra),
            "follow_up": {"object_id": object_id, "missing": pg._clip(missing, 480),
                          "why": why, "who_can_answer": who, "state": "unknown",
                          "priority": priority,
                          **({"days_late": days_late} if days_late is not None else {}),
                          **({"event": dict(event)} if event else {}),
                          # enums only (``confirmation``, ``due_basis``): never a value
                          **{k: v for k, v in facts.items() if v is not None}}}


Part = tuple  # (object_id, canonical markings): one record a skip's cause reads


def _parts(rows: Iterable[Mapping[str, Any]], *extra_markings: str | None) -> list[Part]:
    """The records ``rows`` were read from, each at its row's markings joined with
    ``extra_markings`` (the links that tie it to the rest of the cause)."""
    return [(row.get("object_id"), _markings(row.get("markings"), *extra_markings))
            for row in rows]


class _Skips(Counter):
    """A rule's records it did not judge: the count per reason (the run summary) and each
    skip with its subjects (the records it left unjudged) and its cause (every record and
    link, at its markings, the decision to skip read: ``skips``). A reader is told how many
    of the records it may read a rule left unjudged, counted only over skips whose whole
    cause it may read, the way a gap shows only when the reader may open all of its
    evidence: a skip caused by a record or a link it may not read (an identity twin
    disputing the need date, a lead from a hidden confirmation) is not counted for it, so
    no count names or hints at a hidden record. A skip no record stands for (an order the
    store never projected) or whose cause names a value without its record is
    ``unattributed``: unknown for every reader alike."""

    def __init__(self) -> None:
        super().__init__()
        self.skips: list[tuple[tuple[str, ...], tuple[Part, ...]]] = []
        self.reasons: list[str] = []   # each skip's reason, in ``skips`` order
        self.unattributed = 0

    def note(self, reason: str, subjects: Sequence[str] = (),
             cause: Iterable[Part] = ()) -> None:
        self[reason] += 1
        parts = {(subject, "[]") for subject in subjects}
        for object_id, markings in cause:
            if not isinstance(object_id, str) or not object_id:
                parts = set()   # a cause no reader can be checked against: fail closed
                break
            parts.add((object_id, markings if isinstance(markings, str) else UNREADABLE))
        if not subjects or not parts:
            self.unattributed += 1
            return
        self.skips.append((tuple(sorted(set(subjects))), tuple(sorted(parts))))
        self.reasons.append(reason)


def _result(gaps: Iterable[dict[str, Any] | None], *, status: str = "complete",
            reasons: Sequence[str] = (), not_evaluated: Counter | None = None
            ) -> dict[str, Any]:
    skips = not_evaluated if isinstance(not_evaluated, _Skips) else None
    counted = sum((not_evaluated or Counter()).values())
    return {"status": status, "reasons": list(reasons),
            "gaps": [gap for gap in gaps if gap is not None],
            "not_evaluated": dict(sorted((not_evaluated or Counter()).items())),
            "unjudged": [{"subjects": list(subjects), "cause": [list(p) for p in cause],
                          "reason": reason}
                         for (subjects, cause), reason in zip(skips.skips, skips.reasons)]
            if skips is not None else [],
            # a count without its records (a plain Counter) is attributed to nobody
            "unattributed": skips.unattributed if skips is not None else counted}


def _coverage(total: int, evaluated: int, not_evaluated: Mapping[str, int], *,
              unit: str) -> dict[str, Any]:
    """A detector's coverage: of ``total`` records of ``unit`` it should judge, how many it
    judged and, by reason, how many it did not (never silently omitted)."""
    missing = {k: int(v) for k, v in sorted(not_evaluated.items()) if v}
    return {"unit": unit, "total": int(total), "evaluated": int(evaluated),
            "not_evaluated": missing}


@functools.lru_cache(maxsize=1)
def _run_link_types() -> list[str]:
    """The link types a full run's rules read, read in one scan up front (``_Store.links``;
    a type not listed is read when a rule asks for it, as fast, just not shared)."""
    from . import impact_chain as ic
    from . import proactive_events as ev

    amounts = [name for spec in suspect_money.SPECS.values()
               for name in (spec.get("line_of"), spec.get("item"), spec.get("uom"),
                            spec["party"][1] if (spec.get("party") or ("",))[0] == "link"
                            else None) if name]
    return list(dict.fromkeys([
        *po_status.TWIN_LINKS, po_status.LINE_OF, po_status.RECEIPT_RECEIVES_LINE,
        po_status.QC_RECEIVES_LINE, po_status.RECEIVED_LOT, *po_status.CONFIRMATION_LINKS,
        *TEST_ORDER_LINKS, *TEST_TWIN_LINKS, *RECEIPT_LINKS, *_identity_link_types(),
        *declared_qc_links(),
        pg.LINE_LINK, pg.SUPPLIER_LINK, CONFIRMATION_LINK, *so_status.twin_links(),
        so_status.LINE_OF, *WORK_LINE_LINKS, *WORK_ALLOCATION,
        *so_status.FULFILLED_LINKS, *SHIP_LINKS, *ev.LINK_TYPES,
        *ic.LINK_TYPES, ic.LINE_SELLS, *ic.LEAD_ROUTE_TYPES, *amounts,
        *_rule_link_types()]))


# The rule modules whose detectors read links (``_Store.links``): every link type a module
# names (``_rule_link_types``) is read in the run's one scan, never a scan of its own at its
# detector's turn (2026-11-08 live store: each scan of the 55M-row link table 60-130 s, the
# order-to-cash, price and data-quality rules three to four each).
RULE_MODULES = ("proactive_business", "proactive_ops", "proactive_ceo", "proactive_chasers",
                "proactive_cash", "proactive_stock", "proactive_prices", "proactive_sales",
                "proactive_quality", "proactive_commitments")


@functools.lru_cache(maxsize=1)
def _rule_link_types() -> tuple[str, ...]:
    """Every schema link type the rule modules name in their constants (upper-case module
    names: a text, or texts in a tuple, list, set or mapping), in module then name order."""

    from ontology.schema import load_schema

    known = set(load_schema().link_types)
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if value in known:
                found.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                walk(item)
        elif isinstance(value, (set, frozenset)):
            for item in sorted(value, key=str):
                walk(item)
    # imported by name, never a computed one (the HTTP egress gate reads every import)
    from . import (proactive_business, proactive_cash, proactive_ceo, proactive_chasers,
                   proactive_commitments, proactive_ops, proactive_prices, proactive_quality,
                   proactive_sales, proactive_stock)
    modules = {"proactive_business": proactive_business, "proactive_ops": proactive_ops,
               "proactive_ceo": proactive_ceo, "proactive_chasers": proactive_chasers,
               "proactive_cash": proactive_cash, "proactive_stock": proactive_stock,
               "proactive_prices": proactive_prices, "proactive_sales": proactive_sales,
               "proactive_quality": proactive_quality,
               "proactive_commitments": proactive_commitments}
    for name in RULE_MODULES:
        module = modules[name]
        for constant, value in vars(module).items():
            if constant.isupper():
                walk(value)
    return tuple(dict.fromkeys(found))


def _identity_link_types() -> tuple[str, ...]:
    from ontology.schema import load_schema
    from .ontology_tools import identity_link_type

    return tuple(sorted(lid for lid, link in load_schema().link_types.items()
                        if identity_link_type(link)))


@functools.lru_cache(maxsize=1)
def declared_qc_links() -> tuple[str, ...]:
    """Link types the schema declares as a QC decision: ``QC_LINKS`` when declared, and
    every link between a QC record type (served by the Portal later) and anything."""
    from ontology.schema import load_schema

    links = load_schema().link_types
    return tuple(sorted(lid for lid, link in links.items()
                        if lid in QC_LINKS or link.get("from") in QC_TYPES
                        or link.get("to") in QC_TYPES))


# --------------------------------------------------------------------------- detectors

PO_STATUS_PROPS = tuple(po_status.SOURCE_OF)
# the follow-up field naming the order an item is about (its key and its record's rank):
# never shown, read by ``read_inbox`` to list one item per order for each reader
_ORDER = "order"
# The order record a gap's evidence attests, on gaps that are not that order's own item
# (``missing_in_record``): read by the active-population and test-record filters only,
# never the inbox's one-item-per-order fold (``_ORDER``), so such a note never displaces
# or folds that order's late item.
_ATTESTS = "attests_order"


def _po_source(st: _Store) -> po_status.StoreSource:
    """The whole store's purchase orders as the one PO reader reads them, read once per
    run (the late rule and the receipt quantity rule both read every order)."""
    # read whole even in an incremental pass: its number collisions and default dates
    # are populations every order is judged against
    return st.population("po_source", lambda: po_status.StoreSource(st),
                         lambda src: (sorted(src.colliding), src.defaults.values))


def _mention_resolver(st: _Store, people: Any) -> Any:
    """The store's reused-number mention resolver (``ontology.po_mention_resolve``) with
    our own domains from the people exclusion; None without them (nothing resolves)."""
    from ontology import po_mention_resolve

    own = tuple(getattr(people, "internal", ()) or ())
    if people is None or getattr(people, "unavailable", None) or not own:
        return None
    try:
        with st.whole():
            return po_mention_resolve.Resolver(st.conn, own)
    except Exception:  # noqa: BLE001 - unreadable here: nothing resolves (counted), never a guess
        return None


def _late_or_unconfirmed_po(st: _Store, as_of: date) -> dict[str, Any]:
    """Each settled PO read by the one purchase-order reader (``po_status.read`` over
    ``po_status.StoreSource``: status, lines, FM QC log, ranked need date, promise and
    verdict); this rule adds only its counts, skips, citations and gaps."""
    name = "late_or_unconfirmed_po"
    src = _po_source(st)
    skipped = _Skips()
    counts: Counter = Counter()
    classes: Counter = Counter()
    gaps = []
    disagreeing: set[tuple[str, frozenset]] = set()
    # one order across systems: a settled cluster and every record it is compared with
    # (``po_status.compared_twin``), joined transitively (one BC order, two Portal twins:
    # one order). Each record's late or unconfirmed item is kept with its order key and
    # rank; ``read_inbox`` shows one per order among those its reader may open (``_ORDER``)
    reads = [po_status.read(src, top, as_of) for top, _ in sorted(src.clusters.items())]
    joined = [(po.members[0], other) for po in reads
              for other in (*po.members[1:], *po.date_twins)]
    root = po_status.clusters(joined, [m for po in reads
                                       for m in (*po.members, *po.date_twins)])
    counted: dict[str, set[str]] = defaultdict(set)
    order_class: dict[str, tuple[list, str]] = {}
    # orders open by any read; those any read dates; each undated read's pending item
    open_orders: set[str] = set()
    dated_orders: set[str] = set()
    undated_reads: dict[str, list[tuple[str, list, Any]]] = defaultdict(list)
    for po in reads:
        members = po.members
        order = root.get(members[0], members[0])
        # BC's record governs; among others the least record id, so the choice is pinned
        # the copy that holds the number the supplier knows (a FileMaker-era order's
        # FileMaker PO number) is shown first, then a BC copy, then the least record id
        rank = [0 if any(src.props(m).get("fm_po_number") for m in members) else
                1 if any(po_status.system_of(m) == po_status.POSTED_COPY for m in members)
                else 2, min(members)]

        def order_gap(gap: Any, _order: str = order, _rank: list = rank) -> None:
            if gap is not None:
                gap["follow_up"][_ORDER] = {"key": _order, "rank": _rank}
                gaps.append(gap)

        def count(key: str, _order: str = order) -> int:
            if key in counted[_order]:
                return 0
            counted[_order].add(key)
            return 1
        marks = po.identity_marks
        state = po.status
        # one class per order: its governing record's (BC first, else the least id)
        if order not in order_class or rank < order_class[order][0]:
            order_class[order] = (rank, state.cls)
        for conflict in po.conflicts:
            records = frozenset(r for side in conflict["sides"] for r in side["records"])
            if not records or (conflict["field"], records) in disagreeing:
                continue   # the same disagreement read again from the other record's side
            disagreeing.add((conflict["field"], records))
            # an open copy of an order another copy records received is a reconciliation
            # note (a receipt in any system closes the order), never a delivery risk
            if conflict.get("reconcile"):
                doubt = _receipt_doubt(conflict)
                if doubt is not None:
                    # the received side's own record does not stand behind "received"
                    # (11-13 judging: 12 of 17 rested on a B.Order header or no receipt)
                    counts[doubt] += 1
                    continue
            gaps.append((_receipt_reconciliation if conflict.get("reconcile")
                         else _systems_disagree)(name, conflict, records,
                                                 [*marks, *po.marks]))

        def rows(prop: str) -> list[dict[str, Any]]:
            return [r for m in members for r in src.props(m).get(prop, [])]
        # what every skip of this order read: its records and the identity links joining
        # them, their status, line quantity and date values (``skip``'s cause)
        read: list[Part] = [(m, _markings(*marks)) for m in members] + _parts(
            [*po.evidence, *rows("expected_date")], *marks, *po.marks)

        def skip(reason: str, *more: Part) -> None:
            skipped.note(reason, members, [*read, *more])
        if state.cls == po_status.UNKNOWN:
            skip(state.reason)   # neither open nor closed: counted, never guessed
            continue
        if state.cls != po_status.OPEN:
            continue
        open_orders.add(order)
        rep = min(m for m in members if any(src.props(m).get(p) for p in PO_STATUS_PROPS))
        for _ in po.need.unparseable:
            skip("date_unparseable")
        verdict, why = po.verdict
        if why == "no_requested_date":
            # the requested-by date is the reference or it is missing: never a line's or
            # the supplier's date in its place (nor a BC Draft twin's, decision
            # 2026-11-01-late-list-draft-twins): never late, listed apart as
            # "open, no due date recorded", cited by the status it is open by
            skip("no_need_date")
            # decided once every read of the order is done: an order another read dates
            # is never undated (one order, one verdict)
            status_cites = [[cite] for cite in _cites(po.evidence, *marks, *po.marks)]
            undated_reads[order].append((order, rank, _gap(
                UNDATED_KIND, name, rep, "PurchaseOrder", "undated",
                summary="An open purchase order has no due date recorded"
                        + (" (its only dates are system-computed or placeholder defaults)"
                           if po.need is not None and po.need.defaulted else ""),
                missing="What is the requested-by date of this open purchase order?",
                why="The order is open, but no requested-by date, supplier-confirmed "
                    "date or lead time is recorded on it or on any released system record "
                    "of it, so it is never counted late.",
                priority="low", about=("PurchaseOrder", "expected_date"),
                groups=status_cites,
                extra=_cites(rows("po_number"), *marks)) if status_cites else None))
            continue
        dated_orders.add(order)
        if why == "need_date_disputed":
            skip("need_date_disputed")
            continue
        need = po.need.hi
        need_cites = _cites(po.need.governing, *marks, *po.need.marks)
        basis = po.need.basis
        # every status record and line quantity the verdict rests on is its own evidence:
        # a reader who may not open one of them sees no gap (none is an alternative)
        status_cites = [[cite] for cite in _cites(po.evidence, *marks, *po.marks)] or [[]]
        number = _cites(rows("po_number") + rows("fm_po_number"), *marks) + _cites(
            [r for t in po.date_twins for p in ("po_number", "fm_po_number")
             for r in src.props(t).get(p, [])], *marks, *po.marks)
        # confirmations linked to ANY copy of the order (its twins in every system)
        links = src.confirm_links([*members, *(t for t in po.date_twins if t not in members)])
        linked = [(link["from_object"], _markings(link["markings"], *marks)) for link in links]
        settled = [link for link in links if link["tier"] in CONFIRMING]
        late_now = verdict == po_status.LATE
        if late_now or not links:   # decided without a confirmation (none is at risk)
            counts["evaluated"] += count("evaluated")
            counts["late"] += count("late") if late_now else 0
        if late_now and links and not settled:
            # a confirmation is linked, but only as an unverified lead: never "none"
            counts["confirmation_unverified"] += count("confirmation_unverified")
            found = _link_cites(src, links)
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=_late_summary(basis, need, po.need.doubt),
                missing=f"When will the purchase order due {need.isoformat()} be received?",
                why="The order is still open after its due date.",
                priority=_doubt_priority("medium", po.need.doubt), about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number + found,
                days_late=(as_of - need).days,
                # "found, unverified" only to a reader who may open one of the found
                # confirmations (``_shown``); never "none"
                confirmation="found_unverified", confirmation_cites=[c[0] for c in found],
                due_basis=basis))
            continue
        reused = [] if links else src.reused_links(
            [*members, *(t for t in po.date_twins if t not in members)])
        if late_now and reused:
            # only leads on a number another order also holds: evidence that cannot be
            # tied to this order, so "needs check", never "none found"
            found = _link_cites(src, reused)
            counts["confirmation_needs_check"] += count("confirmation_needs_check")
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=_late_summary(basis, need, po.need.doubt),
                missing=f"When will the purchase order due {need.isoformat()} be received?",
                why="The order is still open after its due date. A document that may be its "
                    "supplier's confirmation names a number another order also holds.",
                priority=_doubt_priority("medium", po.need.doubt), about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number + found,
                days_late=(as_of - need).days, confirmation=po_status.NEEDS_CHECK,
                confirmation_cites=[c[0] for c in found], due_basis=basis))
            continue
        if not links:
            if need <= as_of:
                counts["unconfirmed"] += count("unconfirmed")
                order_gap(_gap(
                    "unconfirmed_po", name, rep, "PurchaseOrder", "unconfirmed",
                    summary=_unconfirmed_summary(basis, need, po.need.doubt),
                    missing=f"Has the supplier confirmed the purchase order due "
                            f"{need.isoformat()}?",
                    why="The order is open, its due date has come and no supplier "
                        "confirmation is linked to any copy of it in the store.",
                    priority=_doubt_priority("high", po.need.doubt), about=("PurchaseOrder", "expected_date"),
                    groups=[need_cites, *status_cites], extra=number,
                    days_late=(as_of - need).days if late_now else None,
                    confirmation="none_found", due_basis=basis))
            continue
        if late_now:   # late on the requested-by date alone, whatever was confirmed
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=_late_summary(basis, need, po.need.doubt),
                missing=f"When will the purchase order due {need.isoformat()} be "
                        "received?",
                why="The order is still open after its due date.",
                priority=_doubt_priority("medium", po.need.doubt), about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number + _link_cites(src, settled),
                days_late=(as_of - need).days, confirmation="confirmed",
                confirmation_cites=[c[0] for c in _link_cites(src, settled)], due_basis=basis))
            continue
        if any(link["tier"] not in CONFIRMING for link in links):
            skip("confirmation_unsettled", *linked)
            continue   # a lead may be the confirmation: at risk or not is not known
        dated = {id(link) for _, link in po.promise.items}
        undated = any(id(link) not in dated for link in links)
        if verdict == po_status.AT_RISK:
            counts["evaluated"] += count("evaluated")
            counts["at_risk"] += count("at_risk")
            shown = po.promise.latest.isoformat()
            by_link: dict[int, tuple[dict[str, Any], list]] = {}
            for row, link in po.promise.items:
                if row["value"] is not None and po_status.day_of(row["value"]) \
                        == po.promise.latest:
                    by_link.setdefault(id(link), (link, []))[1].append(row)
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "confirmed_after_need_date",
                summary=f"The supplier confirmed delivery on {shown}, after the need date "
                        f"{need.isoformat()}" + _doubt_words(po.need.doubt),
                missing=f"Can the purchase order needed by {need.isoformat()} arrive in time, "
                        f"or must the plan move to {shown}?",
                why="A linked supplier confirmation states a delivery date later than the "
                    "order's need date.",
                priority=_doubt_priority("high", po.need.doubt), about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites,
                        *[_cites(found, link["markings"]) for link, found in by_link.values()]],
                extra=number))
        elif undated:
            skip("confirmation_undated", *linked)
        elif po.promise.reason == "promises_disagree":
            # the confirmations' dates disagree and no document date orders them
            skip("promises_disagree", *linked)
        else:
            counts["evaluated"] += count("evaluated")
    stale = _historic_orders(st, as_of, {m: o for m, o in root.items()
                                         if o in undated_reads and o not in dated_orders})
    for order, pending in undated_reads.items():
        if order in dated_orders:
            continue   # another read of this order dates it: never undated
        if order in stale:
            # open and undated, but nothing about it moved in the active window (a legacy
            # back-order): counted apart, never listed as current work
            counts["historic"] += 1
            continue
        counts["undated"] += 1   # counted whether or not a status value is citable
        counted[order].add("undated")
        for key, rank, gap in pending:
            if gap is not None:
                gap["follow_up"][_ORDER] = {"key": key, "rank": rank}
                gaps.append(gap)
    late_orders = {o for o, keys in counted.items() if "late" in keys}
    result = _result(gaps, not_evaluated=skipped)
    # per order, the counts it is in: a gap the active-population filter drops takes its
    # order out of them (``_drop_inactive_orders``), so the counts say what is listed
    result["counted"] = {o: sorted(keys) for o, keys in counted.items()}
    # each record's order (the cross-system join) and that order's class (open when any
    # read of it is open): the vendor-mail rule judges open orders only, one per order
    result["po_orders"] = {m: (order, po_status.OPEN if order in open_orders
                               else order_class.get(order, ([], po_status.UNKNOWN))[1])
                           for m, order in root.items()}
    classes.update(cls for _rank, cls in order_class.values())
    result["counts"] = {"pos": len(order_class),
                        "classes": {cls: classes.get(cls, 0) for cls in po_status.CLASSES},
                        **{key: counts.get(key, 0) for key in (
                            "evaluated", "late", "at_risk", "unconfirmed", "undated",
                            "historic", *RECEIPT_DOUBTS)},
                        # orders open by any of their reads (a Portal Pending order beside
                        # its BC Draft twin is classed by the BC record, yet open here):
                        # open_by_read = late + undated + historic + open_other
                        "open_by_read": len(open_orders),
                        "open_other": len(open_orders - late_orders
                                          - (set(undated_reads) - dated_orders))}
    return result


# where an order's due date comes from (``po_status.Need.basis``), as a gap words it
_DUE_WORDS = {"requested": "its need date",
              "vendor_eta": "the supplier's confirmed date",
              "lead_time": "its lead-time due date"}


PO_ACTIVITY = ("order_date", "portal_created_at", "fm_created_at", *active.EDIT_TIME_PROPS)


def _historic_orders(st: _Store, as_of: date, order_of: Mapping[str, Any]) -> set[Any]:
    """The purchase orders (of ``order_of``: record -> its order) none of whose records
    holds a business date (order, creation or edit day; bulk import days left out) within
    ``active.ACTIVE_DAYS`` of ``as_of`` (``active.inactive_reason``)."""
    if not order_of:
        return set()

    def every_import_day() -> dict[Any, frozenset]:
        held = st.values(("PurchaseOrder",), PO_ACTIVITY)
        return active.import_days(((po_status.binding_of(oid), n), r.get("value"))
                                  for oid, entry in held.items() for n in PO_ACTIVITY
                                  for r in entry["props"].get(n, ()))
    imported = st.population("po_import_days", every_import_day)
    found = st.values(("PurchaseOrder",), PO_ACTIVITY, objects=sorted(order_of))
    dates: dict[Any, list[date]] = defaultdict(list)
    for oid, entry in found.items():
        dates[order_of[oid]] += active.business_days(
            ({"value": r.get("value"), "binding": po_status.binding_of(oid), "property": n}
             for n in PO_ACTIVITY for r in entry["props"].get(n, ())), imported)
    return {order for order in set(order_of.values()) if active.inactive_reason(
        po_status.OPEN, as_of=as_of, dates=dates.get(order, ())) == active.HISTORIC}


def _doubt_words(doubt: str | None) -> str:
    """What a doubted due date says (``po_status.Need.doubt``, BIZ-007): the date is the
    order's own day, kept and judged, but it may be a form default."""
    if doubt == po_status.OWN_DAY:
        return ("; that date is the day the order was placed, so it may be a form default "
                "rather than the requested date")
    return "; the due date is in doubt" if doubt else ""


def _doubt_priority(priority: str, doubt: str | None) -> str:
    return "low" if doubt else priority


def _late_summary(basis: str, need: date, doubt: str | None = None) -> str:
    return (f"An open purchase order is past {_DUE_WORDS[basis]} {need.isoformat()}"
            + _doubt_words(doubt))


def _unconfirmed_summary(basis: str, need: date, doubt: str | None = None) -> str:
    due = ("needed by" if basis == "requested" else "due by its lead time on"
           if basis == "lead_time" else "due by")
    return (f"An open purchase order {due} {need.isoformat()} has no supplier confirmation"
            + _doubt_words(doubt))


RECEIPT_TYPES = (po_status.RECEIPT_TYPE, "ReceiptLine", po_status.LOT_TYPE)   # goods in
RECEIPT_DOUBTS = ("received_header_open", "received_without_receipt")


def _receipt_doubt(conflict: Mapping[str, Any]) -> str | None:
    """Why a reconciliation note is not raised, else None. The side said to hold the goods
    received must stand behind it on its own record: its header word may not read open
    (B.Order, Pending, Generated or Revised: FileMaker's items rolled up over a header still
    saying back-ordered are no closing record a person can act on), and it must cite a
    recorded receipt (a ``Receipt``/``ReceiptLine`` record, or a BC side's own received
    line quantities); a status word alone is no receipt. Judged on the cited items only,
    so the verdict never reads more than the note shows."""
    if conflict.get("field") != "receipt_state":
        return None
    received = conflict.get("governs")
    side = next((s for s in conflict.get("sides") or () if s.get("system") == received), None)
    if side is None:
        return RECEIPT_DOUBTS[1]
    items = [i for i in side.get("items") or () if isinstance(i, Mapping)]
    open_words = po_status.FM_ITEM_PARTIAL | po_status.FM_HEADER_NOTHING
    if any(str(i.get("object_id") or "").startswith("PurchaseOrder:")
           and i.get("property") in ("status", "fm_status")
           and po_status.word(i.get("value")) in open_words for i in items):
        return RECEIPT_DOUBTS[0]
    if not any(str(i.get("object_id") or "").split(":", 1)[0] in RECEIPT_TYPES
               or i.get("property") == po_status.RECEIVED_QTY for i in items):
        return RECEIPT_DOUBTS[1]
    return None


def _receipt_reconciliation(name: str, conflict: Mapping[str, Any], records: Iterable[str],
                            marks: Sequence[str]) -> dict[str, Any] | None:
    """A ``receipt_reconciliation`` note: one copy of a purchase order records the goods
    received while another copy (a BC order, say) still shows them outstanding. A receipt
    in any system closes the order, so this is never a late or at-risk claim: the open
    copy needs reconciling. Each side's citations are their own group."""
    field, sides = conflict["field"], conflict["sides"]
    received = conflict.get("governs")
    groups = [_cites(side["items"], *marks, *conflict.get("marks", ())) for side in sides]
    open_side = next((side for side in sides if side["system"] != received), sides[-1])
    subject = (open_side["records"] or [min(records)])[0]
    unit = f" {conflict['unit']}" if conflict.get("unit") else ""

    def said(side: Mapping[str, Any]) -> str:
        value = side["value"]
        return (_STATE_WORDS.get(value, value) if field == "receipt_state"
                else f"{value}{unit}")
    return _gap(
        "receipt_reconciliation", name, subject, "PurchaseOrder",
        f"{field}|{'|'.join(sorted(records))}",
        summary=(f"A received purchase order still shows {_FIELD_WORDS[field]} differently "
                 "in another copy: " + "; ".join(
                     f"{side['system']} says {said(side)}" for side in sides)
                 + (f" ({received} records the goods received; the other copy needs "
                    "reconciling)" if received else "")),
        missing=f"Can the open copy of this received purchase order be closed or "
                f"corrected ({_FIELD_WORDS[field]})?",
        why="A receipt in any system closes the order; the copy that still shows it open "
            "is a bookkeeping difference, not a late delivery.",
        priority="low",
        about=("PurchaseOrder", "status"), groups=groups)


def _link_cites(src: po_status.StoreSource, links: Sequence[Mapping[str, Any]]
                ) -> list[Cite]:
    """The confirming documents' own records, each cited through its link's markings."""
    out: list[Cite] = []
    for link in links:
        out += _cites(src.documents.get(link["from_object"], []), link["markings"])
    return list({c[0]: c for c in out}.values())


_FIELD_WORDS = {"receipt_state": "whether it is fully received",
                "received_quantity": "the quantity received",
                "expected_date": "the requested-by date"}
_STATE_WORDS = {po_status.FULLY_RECEIVED: "fully received",
                po_status.OPEN_LINES: "open lines still outstanding"}


def _systems_disagree(name: str, conflict: Mapping[str, Any], records: Iterable[str],
                      marks: Sequence[str]) -> dict[str, Any] | None:
    """A ``systems_disagree`` gap: two linked records of one purchase order state different
    values (``po_status.read``'s conflict). Each side's cited records are their own
    evidence group, so a reader who may not open either side sees no gap; the summary
    names both values and who governs, never one picked silently."""
    field, governs = conflict["field"], conflict["governs"]
    sides = conflict["sides"]
    unit = f" {conflict['unit']}" if conflict.get("unit") else ""

    def said(side: Mapping[str, Any]) -> str:
        value = side["value"]
        return (_STATE_WORDS.get(value, value) if field == "receipt_state"
                else f"{value}{unit}")
    groups = [_cites(side["items"], *marks, *conflict.get("marks", ())) for side in sides]
    rule = ("the declared date precedence governs" if field == "expected_date" and governs
            else f"{governs} records the goods received" if governs
            else "no system governs")
    subject = next((side["records"][0] for side in sides
                    if side["system"] == governs and side["records"]), min(records))
    return _gap(
        "systems_disagree", name, subject, "PurchaseOrder",
        f"{field}|{'|'.join(sorted(records))}",
        summary=(f"Systems disagree on {_FIELD_WORDS[field]} of a purchase order: "
                 + "; ".join(f"{side['system']} says {said(side)}" for side in sides)
                 + f" ({rule})"),
        missing=f"Which record of this purchase order is right about {_FIELD_WORDS[field]}, "
                "and can the other one be corrected?",
        why="Two linked records of the same purchase order state different values; the "
            "answer cites both and names the one that governs.",
        priority="medium",
        about=("PurchaseOrder", "expected_date" if field == "expected_date" else "status"),
        groups=groups)


# --------------------------------------------------------------------------- customer orders

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


def _po_receipt_quantity_mismatch(st: _Store, as_of: date) -> dict[str, Any]:
    name = "po_receipt_quantity_mismatch"
    ordered = st.values(("PurchaseOrderLine",), ("ordered_quantity",))
    received = st.values(("ReceiptLine",), ("received_quantity",))
    src = _po_source(st)
    twins = st.links((RECEIPT_LINKS[0],), SETTLED)
    root = _clusters(((l["from_object"], l["to_object"]) for l in twins), received)
    identity: dict[str, list[str]] = defaultdict(list)
    for link in twins:
        if link["from_object"] in root:
            identity[root[link["from_object"]]].append(link["markings"])
    receives = st.links((RECEIPT_LINKS[1],))
    targets: dict[str, set[str]] = defaultdict(set)
    link_marks: dict[str, list[str]] = defaultdict(list)
    unsettled: dict[str, list[Part]] = defaultdict(list)
    for link in receives:
        if link["from_object"] not in root:
            continue
        if link["tier"] not in CONFIRMING:
            unsettled[link["to_object"]].append((link["from_object"], link["markings"]))
            continue
        targets[root[link["from_object"]]].add(link["to_object"])
        link_marks[root[link["from_object"]]].append(link["markings"])
    members: dict[str, list[str]] = defaultdict(list)
    for object_id, top in root.items():
        members[top].append(object_id)
    skipped = _Skips()
    per_line: dict[str, list[tuple[tuple[Decimal, str], list[Cite]]]] = defaultdict(list)
    broken: set[str] = set(unsettled)
    for top, cluster in sorted(members.items()):
        lines = targets.get(top, set())
        if not lines:
            continue
        rows = [r for m in cluster for r in received[m]["props"].get("received_quantity", [])]
        # what a skip of this receipt read: its records and identity links, their
        # quantities, and the lines they receive (at the linking marks)
        marks = [*identity.get(top, []), *link_marks[top]]
        read = ([(m, _markings(*marks)) for m in cluster] + _parts(rows, *marks)
                + [(line, _markings(*marks)) for line in sorted(lines)])
        if len(lines) > 1:
            skipped.note("receipt_line_ambiguous", cluster, read)
            broken |= lines
            continue
        found = _quantity(rows)
        (line,) = lines
        if found is False:
            skipped.note("sources_unreconciled", cluster, read)
            broken.add(line)
            continue
        if found is not None:
            per_line[line].append((found, _cites(rows, *link_marks[top])))
    for line in sorted(set(unsettled) & set(ordered)):
        # a lead may be its receipt: not evaluated (the lead's receipt is its cause)
        skipped.note("receipt_link_unsettled", [line],
                     [(receipt, _markings(m)) for receipt, m in unsettled[line]])
    gaps = []
    for line, parts in sorted(per_line.items()):
        if line in broken or line not in ordered:
            continue
        rows = ordered[line]["props"].get("ordered_quantity", [])
        want = _quantity(rows)
        # the line's ordered quantities and every receipt quantity read against them
        read = _parts(rows) + [(c[1], c[2]) for _, cites in parts for c in cites]
        if not want:
            skipped.note("sources_unreconciled" if want is False else "no_ordered_quantity",
                         [line], read)
            continue
        if any(unit != want[1] for (_, unit), _ in parts):
            skipped.note("unit_differs", [line], read)
            continue
        got = sum((number for (number, _), _ in parts), Decimal(0))
        unit = want[1]
        shown = (format(got.normalize(), "f"), format(want[0].normalize(), "f"))
        groups = [_cites(rows), *[cites for _, cites in parts]]
        if got > want[0]:
            gaps.append(_gap(
                "po_receipt_quantity_mismatch", name, line, "PurchaseOrderLine", "over",
                summary=f"Received {shown[0]} {unit} against {shown[1]} {unit} ordered",
                missing=f"Why was more received ({shown[0]} {unit}) than ordered "
                        f"({shown[1]} {unit})?",
                why="The receipt lines linked to this PO line total more than its ordered "
                    "quantity, in the same unit.",
                priority="high", about=("PurchaseOrderLine", "ordered_quantity"),
                groups=groups))
            continue
        if got == want[0]:
            continue
        closed = []
        for po in src.po_of.get(line, []):
            if po not in src.pos:
                continue
            # the order by the one reader: its settled twins' status, lines and FM QC log
            found = po_status.read(src, po, dates=False)
            if found.status == po_status.PoStatus(po_status.CLOSED, po_status.RECEIVED):
                closed += [[cite] for cite in _cites(found.evidence, *found.identity_marks,
                                                     *found.marks)]
        if not closed:
            continue   # still receiving (or not known closed): not yet a gap
        gaps.append(_gap(
            "po_receipt_quantity_mismatch", name, line, "PurchaseOrderLine", "short",
            summary=f"Received {shown[0]} {unit} against {shown[1]} {unit} ordered on an "
                    "order recorded as fully received",
            missing=f"Is the shortfall of the PO line ({shown[0]} of {shown[1]} {unit}) "
                    "still coming, or should the order be corrected?",
            why="The order is recorded as received while its linked receipt lines total "
                "less than the ordered quantity.",
            priority="medium", about=("PurchaseOrderLine", "ordered_quantity"),
            groups=[*groups, *closed]))
    return _result(gaps, not_evaluated=skipped)


def _receipt_without_qc(st: _Store, as_of: date, link_types: Sequence[str]
                        ) -> dict[str, Any]:
    name = "receipt_without_qc"
    if not link_types:
        # blocked, never a clean "none": the receiving QC FileMaker keeps on each receiving
        # line (its QC lot and status, ``Lot.qc_status``) has no declared QC relation
        # yet; say also when no dedicated QC record (the
        # Portal's incoming QC and eQMS tables, not served) is in the store at all
        reasons = ["qc_link_not_declared"]
        if not any(st._type_count(t) for t in QC_TYPES):
            reasons.append("no_qc_record_captured")
        return _result([], status="blocked", reasons=reasons)
    receipts = st.values(("Receipt",), ("received_date", "receipt_number"))
    lines_of: dict[str, set[str]] = defaultdict(set)
    line_marks: dict[tuple[str, str], str] = {}
    for link in st.links((RECEIPT_LINKS[2],), CONFIRMING):
        lines_of[link["to_object"]].add(link["from_object"])
        line_marks[(link["to_object"], link["from_object"])] = link["markings"]
    qc: dict[str, set[str]] = defaultdict(set)
    qc_parts: dict[str, list[Part]] = defaultdict(list)   # end -> the QC link's other end
    for link in st.links(tuple(link_types)):
        for end, other in ((link["from_object"], link["to_object"]),
                           (link["to_object"], link["from_object"])):
            qc[end].add(link["tier"])
            qc_parts[end].append((other, link["markings"]))
    # a line's twin (the same row in another system) holds that system's QC decision too
    twins: dict[str, set[str]] = defaultdict(set)
    for link in st.links((RECEIPT_LINKS[0],), CONFIRMING):
        twins[link["from_object"]].add(link["to_object"])
        twins[link["to_object"]].add(link["from_object"])
    skipped = _Skips()
    gaps = []
    for receipt, entry in sorted(receipts.items()):
        dated = [r for r in entry["props"].get("received_date", [])
                 if (d := _day(r["value"])) is not None and d <= as_of]
        if not dated:
            continue
        lines = lines_of.get(receipt, set())
        held = {receipt, *lines, *(t for line in lines for t in twins.get(line, ()))}
        tiers = set().union(*(qc.get(o, set()) for o in held))
        if not tiers and po_status.binding_of(receipt) in NO_QC_SOURCE_BINDINGS and not any(
                po_status.binding_of(t) in po_status.FM_RECEIPT_BINDINGS
                for line in lines for t in twins.get(line, ())):
            # Sage and BC keep no QC decision: no QC here is not a gap
            skipped.note("no_qc_source_for_system", [receipt])
            continue
        if tiers & set(CONFIRMING):
            continue
        if tiers:
            # its cause: the receipt's lines and every QC link on them or on it
            cause = [(other, _markings(m)) for other, m in qc_parts.get(receipt, ())]
            for line in sorted(lines_of.get(receipt, ())):
                joined = line_marks.get((receipt, line))
                cause.append((line, _markings(joined)))
                for end in (line, *sorted(twins.get(line, ()))):
                    cause += [(other, _markings(m, joined)) for other, m in qc_parts.get(end, ())]
            skipped.note("qc_link_unsettled", [receipt], cause)
            continue
        gaps.append(_gap(
            "receipt_without_qc", name, receipt, "Receipt", "no_qc",
            summary="A received receipt has no QC record linked to it",
            missing="Was the received material inspected, and where is its QC record?",
            why="No QC record is linked to the receipt or its lines in the store.",
            priority="medium", about=("Receipt", "received_date"), groups=[_cites(dated)],
            extra=_cites(entry["props"].get("receipt_number", []))))
    return _result(gaps, not_evaluated=skipped)


def _order_without_customer_po(st: _Store, coverage: Mapping[str, Any] | None,
                               as_of: date | None = None) -> dict[str, Any]:
    """A live sales order holding no customer PO. Only the active population is asked
    (``_inactive_orders``): a closed or historic order is counted apart (``closed_order``,
    ``historic_order``), never listed as work."""
    name = "order_without_customer_po"
    if coverage is None:
        return _result([], status="not_configured", reasons=["coverage_not_given"])
    inactive = _inactive_orders(st, as_of) if as_of is not None else {}
    excluded: Counter = Counter()
    completeness = coverage.get("completeness") or {}
    versions = st.versions("SalesOrder")
    exact = {v["l1_handle"]: v for v in versions}
    stem = {v["l1_handle"].split("@", 1)[0]: v for v in versions}
    held = st.values(("SalesOrder",), ("customer_po",))
    skipped = _Skips()
    gaps = []
    for order in coverage.get("orders") or []:
        if (not isinstance(order, Mapping) or order.get("status") != "absent_in_this_source"
                or order.get("fm_order_entry_holds_customer_po")):
            continue
        citation = order.get("citation")
        found = (exact.get(citation) or stem.get(citation.split("@", 1)[0])
                 if isinstance(citation, str) else None)
        if found is None:
            skipped.note("not_projected")   # no record stands for it
            continue
        if any(r["value"] not in (None, "") for r in
               (held.get(found["object_id"]) or {"props": {}})["props"].get("customer_po", [])):
            continue   # the projection holds one (another source): not absent
        if found["object_id"] in inactive:
            excluded[inactive[found["object_id"]] + "_order"] += 1   # judged: not live work
            continue
        gaps.append(_gap(
            "order_without_customer_po", name, found["object_id"], "SalesOrder",
            "customer_po",
            summary="A sales order holds no customer PO in any field the Portal row carries",
            missing="Which customer PO does this sales order answer to?",
            why="The full scan of the Portal's sales orders found no customer PO on this "
                "order and no linked FM order entry holding one.",
            priority="medium", about=("SalesOrder", "customer_po"),
            groups=[[(found["l1_handle"], found["object_id"], _markings(found["markings"]))]]))
    status = "complete" if completeness.get("complete") is True else "truncated"
    result = _result(gaps, status=status, not_evaluated=skipped,
                     reasons=[] if status == "complete" else list(
                         completeness.get("reasons") or ["scan_incomplete"]))
    result["counts"] = {"found": len(result["gaps"]), "excluded": dict(sorted(excluded.items()))}
    return result


def _expired_quote(st: _Store, as_of: date) -> dict[str, Any]:
    _documents, quotes, _records = pg._roster()
    found = st.values(quotes, ("status", "valid_until", *pg.QUOTE_OUTCOME_FIELDS))
    gaps = []
    for object_id, entry in sorted(found.items()):
        rows = [r for prop_rows in entry["props"].values() for r in prop_rows]
        by_handle = {r["l1_handle"]: r for r in rows}
        items = [{**r, "citation": {"l1_handle": r["l1_handle"]}} for r in rows]
        for item in pg._expired_quote(entry["type"], items, as_of):
            gaps.append(_gap(
                item["kind"], "expired_quote", object_id, entry["type"], item["key"],
                summary=item["summary"], missing=item["missing"], why=item["why"],
                priority=item["priority"], about=item["about"],
                groups=[_cites(by_handle[h] for h in group) for group in item["requires"]]))
    return _result(gaps)


def _quantity_rows(st: _Store) -> list[dict[str, Any]]:
    """Every current quantity value of a record not removed: the ``object_values`` view's
    rows of ``value_type`` quantity, read from its tables quantity kind first (the
    layouts that hold a quantity, then their cells by layout). Through the view the
    planner scanned every value source of the store (51 s on 2.8M). The order is the
    view's scan order: value source, then cell, then the field's place in its layout."""
    if getattr(st.conn, "postgres", False):
        found = _pg_quantity_rows(st)
    elif st.scoped:
        found = _scoped_quantity_rows(st)
    else:
        found = st.conn.execute(_QUANTITY_SQL).fetchall()
    found = list(found)
    found.sort(key=lambda r: (r["src"], r["cell_field"] is not None, r["cell_field"] or 0,
                              r["pos"]))
    keep = ("object_id", "object_type", "property", "value", "unit", "quantity_role",
            "l1_handle", "markings")
    return [{key: row[key] for key in keep} for row in found]


_QUANTITY_SQL = (
    "SELECT s.src, c.field AS cell_field, lf.pos, v.object_id, o.object_type, "
    "f.property, CASE WHEN c.field IS NULL THEN json_extract(c.value, lf.path) "
    "ELSE c.value END AS value, k.unit, k.quantity_role, v.l1_handle, k.markings "
    "FROM value_kinds k CROSS JOIN value_layout_fields lf ON lf.kind = k.kind "
    "CROSS JOIN value_cells c ON c.layout = lf.layout "
    "JOIN value_sources s ON s.src = c.src JOIN object_versions v ON v.rowid = s.ver "
    "JOIN objects o ON o.object_id = v.object_id JOIN value_fields f ON f.field = lf.field "
    "WHERE k.value_type = 'quantity' AND s.superseded_at IS NULL AND NOT "
    + ostore.removed_at_sql("o.object_id"))


def _pg_quantity_rows(st: _Store) -> list[Any]:
    """``_quantity_rows``' rows on the Postgres ontology, which holds no packed layouts
    (``value_layout_fields``): one cell per field, each with its own value kind. A whole
    pass reads only the records of the types an identity link joins (``_identity_link_types``:
    a dispute is two copies of one record; a lone record holds one current source), by the
    type index, and of each only the cells of the fields the schema declares a quantity
    (kind checked as well): the kind has no index there, so a read by kind scanned every
    cell of the store (520M rows; 120 s statement timeout on every full run, 2026-11-11).
    An incremental pass reads its scope's records' own value sources."""
    base = ("SELECT s.src, c.field AS cell_field, 0 AS pos, v.object_id, o.object_type, "
            "f.property, c.value, k.unit, k.quantity_role, v.l1_handle, k.markings ")
    if not st.scoped:
        sql = (base + "FROM objects o JOIN object_versions v ON v.object_id = o.object_id "
               "JOIN value_sources s ON s.ver = v.rowid AND s.superseded_at IS NULL "
               "JOIN value_cells c ON c.src = s.src "
               "AND c.field IN (SELECT value FROM json_each(:fields)) "
               "JOIN value_kinds k ON k.kind = c.kind AND k.value_type = 'quantity' "
               "JOIN value_fields f ON f.field = c.field "
               "WHERE o.object_type = :type AND NOT " + ostore.removed_at_sql("o.object_id"))
        fields = json.dumps([row[0] for row in st.conn.execute(
            "SELECT field FROM value_fields WHERE property IN (SELECT value FROM "
            "json_each(:props))", {"props": json.dumps(_quantity_properties())})])
        found = []
        for object_type in sorted({t.split(".", 1)[0] for t in _identity_link_types()}):
            found.extend(st.conn.execute(sql, {"fields": fields, "type": object_type}))
        return found
    sql = (base + "FROM value_kinds k JOIN value_cells c ON c.kind = k.kind "
           "JOIN value_sources s ON s.src = c.src JOIN object_versions v ON v.rowid = s.ver "
           "JOIN objects o ON o.object_id = v.object_id JOIN value_fields f ON f.field = c.field "
           "WHERE k.value_type = 'quantity' AND s.superseded_at IS NULL AND NOT "
           + ostore.removed_at_sql("o.object_id")
           + " AND v.object_id IN (SELECT value FROM json_each(:chunk))")
    found: list[Any] = []
    ids = st._scope_ids
    for start in range(0, len(ids), SCOPE_CHUNK):
        found.extend(st.conn.execute(sql, {"chunk": json.dumps(ids[start:start + SCOPE_CHUNK])}))
    return found


@functools.lru_cache(maxsize=1)
def _quantity_properties() -> list[str]:
    """Every property name the schema declares a quantity on some type."""
    from ontology.schema import load_schema

    return sorted({p["name"] for spec in load_schema().object_types.values()
                   for p in spec.get("properties", ())
                   if (p.get("value_type") or p.get("type")) == "quantity"})


def _scoped_quantity_rows(st: _Store) -> list[Any]:
    """``_quantity_rows``' rows of the scope's records (an incremental pass), read from
    each record's own value sources (its version, never a scan of the quantity cells)."""
    found: list[Any] = []
    sql = ("SELECT s.src, c.field AS cell_field, lf.pos, v.object_id, o.object_type, "
           "f.property, CASE WHEN c.field IS NULL THEN json_extract(c.value, lf.path) "
           "ELSE c.value END AS value, k.unit, k.quantity_role, v.l1_handle, k.markings "
           "FROM object_versions v JOIN objects o ON o.object_id = v.object_id "
           "JOIN value_sources s ON s.ver = v.rowid JOIN value_cells c ON c.src = s.src "
           "JOIN value_layout_fields lf ON lf.layout = c.layout "
           "JOIN value_kinds k ON k.kind = lf.kind JOIN value_fields f ON f.field = lf.field "
           "WHERE v.object_id IN (SELECT value FROM json_each(:chunk)) "
           "AND k.value_type = 'quantity' AND s.superseded_at IS NULL AND NOT "
           + ostore.removed_at_sql("o.object_id"))
    ids = st._scope_ids
    for start in range(0, len(ids), SCOPE_CHUNK):
        found.extend(st.conn.execute(sql, {"chunk": json.dumps(ids[start:start + SCOPE_CHUNK])}))
    return found


def _conflicting_quantity(st: _Store, as_of: date) -> dict[str, Any]:
    rows = _quantity_rows(st)
    objects = {row["object_id"] for row in rows}
    identity = [link for link in st.links(_identity_link_types(), SETTLED)
                if link["from_object"] in objects and link["to_object"] in objects]
    root = _clusters(((l["from_object"], l["to_object"]) for l in identity), objects)
    marks: dict[str, list[str]] = defaultdict(list)
    for link in identity:
        marks[root[link["from_object"]]].append(link["markings"])
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(root[row["object_id"]], row["object_type"], row["property"])].append(dict(row))
    disputed = {top: {m["object_id"] for m in members}
                for (top, _t, _p), members in grouped.items()
                if len({m["l1_handle"] for m in members}) >= 2}
    closed = _closed_clusters(st, disputed, {row["object_type"] for row in rows})
    edited = _edit_times(st, disputed, {row["object_type"] for row in rows})
    gaps = []
    skipped: Counter[str] = Counter()
    for (top, object_type, prop), members in sorted(grouped.items()):
        if len({m["l1_handle"] for m in members}) < 2:
            continue
        if _superseded(members, edited):
            # the newest system of record wins: a value only an older record of the same
            # identity still holds is superseded, no risk (the record page shows both)
            skipped["superseded_by_newer_record"] += 1
            continue
        by_handle = {m["l1_handle"]: m for m in members}
        subject = f"{object_type}.{prop}"
        conflict = {"kind": "value_disagreement", "subject": subject, "values": [
            {"value_type": "quantity", "value": m["value"], "unit": unit_token(m["unit"]),
             "quantity_role": m["quantity_role"], "citation": {"l1_handle": m["l1_handle"]}}
            for m in members]}
        items = pg._conflicting_quantities([conflict], skipped)
        if items and top in closed:
            skipped["record_closed"] += len(items)
            continue   # a finished or cancelled record: nothing left to act on
        for item in items:
            gaps.append(_gap(
                item["kind"], "conflicting_quantity", top, object_type, item["key"],
                summary=item["summary"], missing=item["missing"], why=item["why"],
                priority=item["priority"], about=(object_type, prop),
                groups=[_cites((by_handle[h] for h in group), *marks.get(top, []))
                        for group in item["requires"]]))
    return _result(gaps, not_evaluated=skipped)


# A record's own last-edit time as its system stamps it (never when it was read).
EDIT_TIME_PROPS = ("portal_updated_at", "fm_modified_at", "bc_last_modified_at")


def _edit_times(st: _Store, disputed: Mapping[str, set[str]], types: Iterable[str]
                ) -> dict[str, Any]:
    """object id -> its newest own edit time (``EDIT_TIME_PROPS``), of the objects holding
    a disputed value; an object with none stated is absent."""
    from .answer_conflicts import parse_time
    objects = sorted({o for members in disputed.values() for o in members})
    if not objects:
        return {}
    out: dict[str, Any] = {}
    for oid, entry in st.values(sorted(types), EDIT_TIME_PROPS, objects=objects).items():
        times = [t for p in EDIT_TIME_PROPS for r in entry["props"].get(p, [])
                 for t in [parse_time(r["value"])] if t is not None]
        if times:
            out[oid] = max(times)
    return out


def _superseded(members: Sequence[Mapping[str, Any]], edited: Mapping[str, Any]) -> bool:
    """Whether one value of a disputed quantity is held by the newest edited record of the
    identity: the one conflict rule (``answer_conflicts.resolve``'s "newest", records
    dated by their own edit time) decides, never a value two records of one system hold
    alike. Each disagreeing record must state its edit time, and they must be different
    records (one record stating two values is no supersession)."""
    from .answer_conflicts import Reading, resolve
    if len({m["object_id"] for m in members}) < 2 \
            or any(m["object_id"] not in edited for m in members):
        return False
    by_value: dict[str, set[str]] = defaultdict(set)
    for m in members:
        by_value[str(pg._decimal(m["value"]))].add(m["object_id"])
    if any(a & b for x, a in by_value.items() for y, b in by_value.items() if x < y):
        return False
    found = resolve("quantity", [Reading(value=m["value"], handle=m["l1_handle"],
                                         as_of=edited[m["object_id"]]) for m in members])
    return bool(found) and found.get("rule") == "newest"


def _closed_clusters(st: _Store, disputed: Mapping[str, set[str]],
                     types: Iterable[str]) -> set[str]:
    """The clusters (of ``disputed``: root -> its objects holding the disputed values) any
    of whose records' own ``status`` says closed (``proactive_gaps.status_closed``)."""
    owner = {object_id: top for top, members in disputed.items() for object_id in members}
    if not owner:
        return set()
    found = st.values(sorted(types), ("status",), objects=sorted(owner))
    return {owner[object_id] for object_id, entry in found.items()
            if any(pg.status_closed(row["value"])
                   for row in entry["props"].get("status", []))}


def _card_of(object_id: str, object_type: str, props: Mapping[str, Any]) -> dict[str, Any]:
    """A ``plx.object``-shaped card of the store's current values, for the shared rules."""
    return {"object": {"object_id": object_id, "object_type": object_type},
            "values": [{"property": prop, "value": row["value"], "unit": row["unit"],
                        "citation": {"l1_handle": row["l1_handle"]}}
                       for prop, rows in props.items() for row in rows]}


def _unreceived_draft(st: _Store, order: str, order_lines: Sequence[str],
                      lines: Mapping[str, Mapping[str, Any]]) -> bool:
    """``order`` is a BC Draft (its ``bc_status`` read as not issued) none of whose lines
    records a received quantity above 0 (an unreadable one counts as none: a Draft's
    date never decides late on a guess)."""
    values = (st.values((order.split(":", 1)[0],), ("bc_status",), objects=[order])
              .get(order) or {"props": {}})["props"]
    if not po_status.bc_draft({"bc_status": [r["value"] for r in values.get("bc_status", [])]}):
        return False
    for line_id in order_lines:
        for row in (lines.get(line_id) or {"props": {}})["props"].get(pg.LINE_RECEIVED, ()):
            number = po_status._number(row["value"])
            if number is not None and number > po_status.EPS:
                return False
    return True


def _late_orders(st: _Store, as_of: date, only: str | None = None
                 ) -> tuple[list[dict[str, Any]], "_Skips"] | None:
    """Each order with late lines (``pg._late_line``) and the facts its supplier draft
    states, every fact as ``(value, Cite)``: its number, its supplier's name, its
    confirmed delivery date and each late line. ``only``: that one order (the console's
    re-derivation). None when the schema does not declare the rule."""
    order_type = pg._late_target()
    if order_type is None:
        return None
    line_props = (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED, pg.LINE_ITEM)
    orders_of: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in st.links((pg.LINE_LINK,), CONFIRMING):
        orders_of[link["from_object"]].append((link["to_object"], link["markings"]))
    wanted = None if only is None else sorted(
        line for line, reached in orders_of.items() if any(o == only for o, _m in reached))
    lines = st.values((pg.LINE_TYPE,), line_props, objects=wanted)
    skipped = _Skips()
    late: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = defaultdict(list)
    for line_id, entry in sorted(lines.items()):
        found = pg._late_line(_card_of(line_id, pg.LINE_TYPE, entry["props"]), as_of)
        if found is None:
            if any(len(entry["props"].get(p, ())) > 1 for p in line_props[:3]):
                skipped.note("line_value_disputed", [line_id], _parts(
                    [r for p in line_props[:3] for r in entry["props"].get(p, ())]))
            continue
        reached = {order for order, _marks in orders_of.get(line_id, [])
                   if order.startswith(f"{order_type}:")}
        if len(reached) != 1:
            # unlinked: nothing beyond the line; ambiguous: each order and its link
            skipped.note("line_order_unlinked" if not reached else "line_order_ambiguous",
                         [line_id], [(order, _markings(m)) for order, m in
                                     orders_of.get(line_id, []) if order in reached])
            continue
        (order,) = reached
        if only is not None and order != only:
            continue
        marks = _markings(*[m for o, m in orders_of[line_id] if o == order])
        late[order].append((found, entry, marks))
    # A BC Draft never received against
    # dates nothing, so its lines are never late (a Draft any line of which records a
    # received quantity keeps its lines' dates)
    lines_of_order: dict[str, list[str]] = defaultdict(list)
    for line_id, reached in orders_of.items():
        for order, _m in reached:
            if order in late:
                lines_of_order[order].append(line_id)
    for order in sorted(o for o in late
                        if _unreceived_draft(st, o, lines_of_order[o], lines)):
        skipped.note("draft_dates_nothing", [order],
                     [(line["line_id"], m) for line, _e, m in late[order]])
        del late[order]
    if not late:
        return [], skipped
    picked = sorted(late)
    numbers = st.values((order_type,), (pg.ORDER_NUMBER,), objects=picked)
    supplier_type = pg._supplier_target()
    suppliers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if supplier_type is not None:
        for link in st.links((pg.SUPPLIER_LINK,), CONFIRMING):
            if link["from_object"] in late:
                suppliers[link["from_object"]].append((link["to_object"], link["markings"]))
    names = st.values((supplier_type,), (pg.SUPPLIER_NAME,), objects=sorted(
        {t for found in suppliers.values() for t, _m in found})) if supplier_type else {}
    confirms: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    for link in st.links((CONFIRMATION_LINK,), CONFIRMING):
        if link["to_object"] in late:
            confirms[link["to_object"]].append((link["from_object"], link["markings"],
                                                link["tier"] in SETTLED))
    dated = st.values((CONFIRMATION_TYPE,), (CONFIRMED_DATE,), objects=sorted(
        {c for found in confirms.values() for c, _m, _s in found}))
    out = []
    for order in picked:
        facts: dict[str, Any] = {"order": order, "order_type": order_type, "number": None,
                                 "supplier": None, "confirmed": [], "corroborated": [],
                                 "unresolved": []}
        card = _card_of(order, order_type, (numbers.get(order) or {"props": {}})["props"])
        number = pg._one_text(card, pg.ORDER_NUMBER)
        if number is None:
            facts["unresolved"].append("order_number:not_in_record")
        else:
            facts["number"] = (number[0], _cites([r for r in numbers[order]["props"][
                pg.ORDER_NUMBER] if r["l1_handle"] == number[1]])[0])
        if supplier_type is None:
            facts["unresolved"].append("supplier:link_undeclared")
        else:
            reached = {t: m for t, m in suppliers.get(order, [])}
            if len(reached) != 1 or not next(iter(reached)).startswith(f"{supplier_type}:"):
                # none, two, or one that is not a supplier record: no one supplier
                facts["unresolved"].append("supplier:not_linked" if not reached
                                           else "supplier:ambiguous")
            else:
                ((one, link_marks),) = reached.items()
                card = _card_of(one, supplier_type, (names.get(one) or {"props": {}})["props"])
                name = pg._one_text(card, pg.SUPPLIER_NAME)
                if name is None:
                    facts["unresolved"].append("supplier:name_not_in_record")
                else:
                    facts["supplier"] = (name[0], _cites([
                        r for r in names[one]["props"][pg.SUPPLIER_NAME]
                        if r["l1_handle"] == name[1]], link_marks)[0])
        for confirmation, link_marks, settled in sorted(confirms.get(order, [])):
            rows = (dated.get(confirmation) or {"props": {}})["props"].get(CONFIRMED_DATE, [])
            days = {_day(r["value"]) for r in rows}
            if len(days) != 1 or None in days:
                facts["unresolved"].append("confirmed_date:unreadable")
                continue
            # only a settled confirmation is told to the supplier as theirs; a corroborated
            # one stays an internal note
            facts["confirmed" if settled else "corroborated"].append(
                (next(iter(days)).isoformat(), _cites(rows[:1], link_marks)[0]))
        facts["lines"] = []
        for line, entry, marks in late[order]:
            props = entry["props"]

            def cite(prop: str, handle: str | None) -> list[Cite]:
                return _cites([r for r in props.get(prop, []) if r["l1_handle"] == handle],
                              marks)
            facts["lines"].append({
                **line, "date_cite": cite(pg.LINE_DATE, line["date_handle"]),
                "quantity_cites": cite(pg.LINE_ORDERED, line["ordered"][2])
                + cite(pg.LINE_RECEIVED, line["received"][2]),
                "item_cite": cite(pg.LINE_ITEM, line["item"][1]) if line.get("item") else [],
                "evidence": {prop: _cites([r for r in props[prop]
                                           if r["l1_handle"] in line["handles"]], marks)
                             for prop in (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED)}})
        out.append(facts)
    return out, skipped


def _worded(facts: Mapping[str, Any], roles: Sequence[str],
            readable: Callable[[Sequence[Cite]], set[str]] | None = None
            ) -> dict[str, Any] | None:
    """The supplier draft of one order's facts as a reader may read them: a line is used
    only cited whole (its date and both quantities), a withheld item name is left out, a
    withheld number, supplier or confirmation is named in ``unresolved`` (a withheld
    confirmation drops the confirmed date: the reader cannot see whether it agrees). None
    when no late line is left."""
    every = [c for line in facts["lines"] for c in (*line["date_cite"],
                                                    *line["quantity_cites"],
                                                    *line["item_cite"])]
    every += [f[1] for f in (facts["number"], facts["supplier"]) if f is not None]
    every += [c for _day_text, c in (*facts["confirmed"], *facts["corroborated"])]
    every += [facts["due"][2]] if facts.get("due") else []
    shown = {c[0] for c in every} if readable is None else readable(every)
    opens = lambda cites: bool(cites) and all(c[0] in shown for c in cites)  # noqa: E731
    lines = []
    for line in facts["lines"]:
        if not opens(line["date_cite"] + line["quantity_cites"]):
            continue
        lines.append({**line, "item": line["item"] if opens(line["item_cite"]) else None})
    if not lines:
        return None
    unresolved = list(facts["unresolved"])
    told: dict[str, Any] = {}
    for name in ("number", "supplier"):
        fact = facts[name]
        if fact is not None and not opens([fact[1]]):
            unresolved.append(f"{'order_number' if name == 'number' else name}:withheld")
            fact = None
        told[name] = None if fact is None else (fact[0], fact[1][0])
    confirmed = facts["confirmed"]
    told["confirmed"] = None
    told["internal_notes"] = []
    if confirmed and not opens([c for _d, c in confirmed]):
        unresolved.append("confirmed_date:withheld")
    elif len({d for d, _c in confirmed}) > 1:
        unresolved.append("confirmed_date:conflict")
    elif confirmed and "confirmed_date:unreadable" not in unresolved:
        told["confirmed"] = (confirmed[0][0], confirmed[0][1][0])
    corroborated = facts["corroborated"]
    if (corroborated and told["confirmed"] is None and len({d for d, _c in corroborated}) == 1
            and opens([c for _d, c in corroborated])):
        told["internal_notes"] = [{"note": "confirmed_date:corroborated_only",
                                   "value": corroborated[0][0],
                                   "citation": corroborated[0][1][0]}]
    due = facts.get("due")
    told["due"] = (due[0], due[1], due[2][0]) if due is not None and opens([due[2]]) else None
    told["unresolved"] = unresolved
    return late_order_draft(lines, told, roles)


def _amount(number: Decimal, unit: str | None) -> str:
    shown = format(number.normalize(), "f")
    return f"{shown} {pg._clip(' '.join(unit.split()), 20)}" if unit and unit.strip() else shown


def late_order_draft(lines: Sequence[Mapping[str, Any]], facts: Mapping[str, Any],
                     roles: Sequence[str]) -> dict[str, Any]:
    """The supplier message asking it to confirm delivery of one order's late lines, worded
    from cited facts only, never sent (``DRAFT_STATUS``, ``sent`` False). It names the
    purchase order, each late line (its item, ordered and received quantities) and the
    governing date: the supplier's confirmed delivery date when exactly one is readable
    (``date_basis`` ``confirmed``), else each line's expected receipt date worded "our
    expected date" (``expected``). Its citations are a side list: each stated fact one
    ``facts`` entry with its handle, no marker in the text; at most ``MAX_DRAFT_LINES``
    lines and ``MAX_DRAFT_CITATIONS`` handles (the catalog's draft citation bound)."""
    citations: list[str] = []
    listed: list[dict[str, Any]] = []

    def fact(name: str, value: str, handle: str, **extra: Any) -> None:
        if handle not in citations:
            citations.append(handle)
        listed.append({"fact": name, "value": value, **extra, "citation": handle})

    number, supplier, confirmed = facts.get("number"), facts.get("supplier"), \
        facts.get("confirmed")
    if supplier is not None:
        to = {"kind": "supplier", "name": supplier[0], "citation": supplier[1]}
        to_text = supplier[0]
        fact("supplier", supplier[0], supplier[1])
    else:
        role = roles[0] if roles else None
        to = {"kind": "answerer_role", "role": role,
              "reason": "the order's supplier name is not readable here"}
        to_text = (f"{role} (answerer role; the order's supplier name is not readable here)"
                   if role else "(no supplier name or answerer role is known)")
    if number is not None:
        fact("order_number", number[0], number[1])
        order_text, subject = (f"purchase order {number[0]}",
                               f"{number[0]}: please confirm the delivery date")
    else:
        order_text, subject = ("our purchase order (its number is not readable here)",
                               "Our purchase order: please confirm the delivery date")
    due = facts.get("due")
    if confirmed is not None:
        fact("confirmed_delivery_date", confirmed[0], confirmed[1])
        basis = (f"Your confirmation gives the delivery date {say_date(confirmed[0])}, and these lines "
                 "are not fully received:\n")
    elif due is not None:
        fact("due_date", due[0], due[2], basis=due[1])
        words = {"requested": "our requested date", "vendor_eta": "your confirmed date",
                 "lead_time": "the order's lead time, due"}[due[1]]
        basis = f"These lines are past {words} {say_date(due[0])} and not fully received:\n"
    else:
        basis = "These lines are past our expected date and not fully received:\n"
    # one composer (``drafts``): the sender's voice (no greeting name for a firm, no fixed
    # sign-off) and the copy gate; the late lines follow as record lines
    ask = f"Can you confirm the delivery date for {order_text}?"
    composed = drafts.compose(
        "supplier_date_ask", [ask, basis.rstrip("\n").rstrip(":") + ":"],
        subject=subject[:60], specific=False,
        facts=[("n", number[0] if number else None)]
        + [(f["fact"], f["value"]) for f in listed])
    opening = (composed["body"] or ask) + "\n"
    head = f"{DRAFT_LABEL}\nTo: {to_text}\nSubject: {subject}\n\n{opening}"
    tail = ""
    ordered = sorted(lines, key=lambda line: (line["day"], str(line.get("line_id"))))
    body, shown = "", 0
    for line in ordered[:MAX_DRAFT_LINES]:
        mark = len(listed), list(citations)
        line_id = str(line.get("line_id") or "")
        item = line.get("item")
        if item:
            fact("line_item", item[0], item[1], line_id=line_id)
        name = item[0] if item else "a line (the record names no item)"
        when = ""
        if confirmed is None and due is None:
            day = line["day"].isoformat()
            fact("expected_receipt", day, line["date_handle"], line_id=line_id)
            when = f"our expected date {say_date(day)}, "
        (q_o, u_o, h_o), (q_r, u_r, h_r) = line["ordered"], line["received"]
        fact("ordered_quantity", format(q_o.normalize(), "f"), h_o, line_id=line_id, unit=u_o)
        fact("received_quantity", format(q_r.normalize(), "f"), h_r, line_id=line_id, unit=u_r)
        row = (f"- {name}: {when}ordered {_amount(q_o, u_o)}, "
               f"received {_amount(q_r, u_r)}\n")
        if (len(head) + len(body) + len(row) + len(tail) + 60 > MAX_DRAFT_TEXT
                or len(citations) > MAX_DRAFT_CITATIONS):
            del listed[mark[0]:]   # this line does not fit: neither its text nor its facts
            citations[:] = mark[1]
            break
        body += row
        shown += 1
    if shown < len(ordered):
        body += f"- and {len(ordered) - shown} more late lines, not listed here\n"
    copy_check = copy_gate.check(opening + body, surface="draft_body")
    return {"label": DRAFT_LABEL, "status": DRAFT_STATUS, "sent": False, "to": to,
            "copy_check": copy_check, "subject": pg._clip(subject, 200), "text": pg._clip((head + body + tail).rstrip("\n"),
                                                                 MAX_DRAFT_TEXT),
            "date_basis": "confirmed" if confirmed is not None else
            ("due" if due is not None else "expected"),
            "internal_notes": [dict(n) for n in facts.get("internal_notes") or ()],
            "facts": listed, "citations": citations, "lines_late": len(ordered),
            "lines_listed": shown, "unresolved": list(facts.get("unresolved") or ())}


def _vendor_facing(src: po_status.StoreSource, po: po_status.PoRead,
                   facts: dict[str, Any]) -> None:
    """The order number the supplier knows (the number on the document we sent: a
    FileMaker-era order's FileMaker PO number, held by its FileMaker and Portal copies),
    in place of a copy's own internal number, when exactly one is readable and cited."""
    row = po_status.vendor_facing(
        (r.get("value"), r) for m in (*po.members, *po.date_twins)
        for r in src.props(m).get(po_status.VENDOR_NUMBER_PROP, []))
    if row is not None:
        facts["number"] = (str(row["value"]).strip(), _cites([row])[0])


def _late_order(st: _Store, as_of: date) -> dict[str, Any]:
    """``late_order``: late lines (``pg._late_line``) grouped by their one order, each gap
    with its supplier draft (``late_order_draft``) as the store holds the facts."""
    name = "late_order"
    found = _late_orders(st, as_of)
    if found is None:
        return _result([], status="not_configured", reasons=["rule_not_declared"])
    orders, skipped = found
    gaps = []
    src = _po_source(st)
    for facts in orders:
        # the order is late only by the one purchase-order definition (a receipt in any
        # copy closes it; a default date dates nothing): a late line of an order that
        # rule does not call late is no claim (one rule, never a second copy of it)
        po = po_status.read(src, facts["order"], as_of)
        if po.verdict is None or po.verdict[0] != po_status.LATE:
            skipped.note("order_not_late", [facts["order"]],
                         [(facts["order"], _markings(*po.identity_marks))])
            continue
        _vendor_facing(src, po, facts)
        by_lines = all(isinstance(r, Mapping) and r.get("property") == pg.LINE_DATE
                       for r in po.need.governing)
        due_cites = _cites(po.need.governing, *po.need.marks)
        if due_cites and not by_lines:
            # dated by its header or a labelled source: the draft states that one date
            facts["due"] = (po.need.hi.isoformat(), po.need.basis, due_cites[0])
        # a line whose own date is a default (``po_status``'s date provenance) is never
        # stated as its expected date
        defaulted = {(r.get("object_id"), str(r.get("value"))) for r in po.need.defaulted
                     if isinstance(r, Mapping)}
        facts["lines"] = [line for line in facts["lines"] if not by_lines
                          or (line.get("line_id"), line["day"].isoformat()) not in defaulted]
        if not facts["lines"]:
            skipped.note("order_not_late", [facts["order"]],
                         [(facts["order"], _markings(*po.identity_marks))])
            continue
        rows = facts["lines"]
        groups = [[c for line in rows for c in line["evidence"][prop]]
                  for prop in (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED)]
        extra = [c for line in rows for c in line["item_cite"]]
        extra += [f[1] for f in (facts["number"], facts["supplier"]) if f is not None]
        extra += [c for _d, c in facts["confirmed"]]
        count = len(rows)
        noun = "line" if count == 1 else "lines"
        due = po.need.hi.isoformat()
        gap = _gap(
            "late_order", name, facts["order"], facts["order_type"], "late_lines",
            summary=f"{count} {noun} not fully received past {_DUE_WORDS[po.need.basis]} "
                    f"{due}",
            missing="Confirm a new delivery date for the lines not yet received",
            why=pg._clip(f"The order is past {_DUE_WORDS[po.need.basis]} {due} and {count} "
                         f"{noun} still have a received quantity below the ordered "
                         "quantity in every copy of the order, as cited."),
            priority="high", about=(pg.LINE_TYPE, pg.LINE_DATE), groups=groups,
            extra=list({c[0]: c for c in extra}.values()))
        if gap is not None:
            draft = _worded(facts, gap["follow_up"]["who_can_answer"])
            if draft is not None:
                gap["follow_up"]["draft"] = draft
            gaps.append(gap)
    return _result(gaps, not_evaluated=skipped)


def order_draft(store_path: Any, order_id: str, as_of: date, *,
                may_read: Callable[[str], bool], held: Iterable[str],
                decide: Callable[[Sequence[str]], Mapping[str, Any]]) -> dict[str, Any] | None:
    """The supplier draft of one order (``late_order_draft``), derived again from the store
    for one reader: a fact is stated only when its record sits in an audience
    ``may_read`` admits, at markings the reader ``held`` covers, and ``decide`` (None per
    handle it may open) lets it open, as ``read_inbox`` shows a gap. The console words a
    proposed draft with this, never from the text or answerers a client sent: the
    answerer roles are the gap's own (``pg._who``, as ``_gap`` resolves them). None when
    the order has no late line the reader may open whole (or the rule is not declared)."""
    st = _Store(Path(store_path))
    try:
        found = _late_orders(st, as_of, only=order_id)
        if not found or not found[0]:
            return None
        (facts,) = found[0]
        allowed = set(ostore.allowed_marking_sets(held))

        def readable(cites: Sequence[Cite]) -> set[str]:
            ids = sorted({c[1] for c in cites if isinstance(c[1], str)})
            seen: dict[str, bool] = {}
            for row in st.conn.execute(
                    "SELECT o.object_id, o.audience_key, v.markings FROM objects o JOIN "
                    "object_versions v ON v.object_id = o.object_id AND v.superseded_at IS "
                    "NULL WHERE o.object_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(ids),)):
                try:
                    ok = may_read(row["audience_key"]) is True
                except Exception:  # noqa: BLE001 - a failing grant check denies
                    ok = False
                seen[row["object_id"]] = (seen.get(row["object_id"], True) and ok
                                          and row["markings"] in allowed)
            open_ = [c for c in cites if seen.get(c[1]) is True and c[2] in allowed]
            handles = sorted({c[0] for c in open_})
            refused = {h for h, why in decide(handles).items() if why is not None} \
                if handles else set()
            return {c[0] for c in open_ if c[0] not in refused}

        first = facts["lines"][0]["evidence"][pg.LINE_DATE][0][0]
        roles = pg._who(facts["order_type"], {"about": (pg.LINE_TYPE, pg.LINE_DATE),
                                              "handles": [first]})
        return _worded(facts, roles, readable)
    finally:
        st.close()


def _lateness_audiences(st: _Store) -> list[str]:
    """Every audience a reader needs for ``plx.process`` to decide a purchase order's
    lateness for it, as the tool decides a source is withheld (a Portal relation's
    ``api:`` key, a BC relation's stored ``bc:<company>/`` keys or, none stored, the
    family-wide ``bc:*``; else ``<family>:*`` and the family's stored keys;
    item-ACL sources are unproven for every reader alike): over each binding the
    ``po_delivery_window`` graph names and each binding of the link types it walks, plus
    the audience of every stored record of those bindings."""
    from ontology.schema import load_schema
    from operating.ontology_tools import _AUDIENCE_SOURCE, _ITEM_ACL_AUDIENCES

    schema = load_schema()
    strings: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            strings.add(node)
        elif isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(LATENESS_GRAPH.read_text(encoding="utf-8")))
    bindings = {s for s in strings if s in schema.bindings}
    for link in (schema.link_types[s] for s in strings if s in schema.link_types):
        for lb in link.get("bindings", ()):
            bindings |= {lb.get("from_binding"), lb.get("to_binding"),
                         (lb.get("via") or {}).get("binding")}
    bindings.discard(None)
    row = st.meta("audience_keys")
    stored = set(json.loads(row)) if row else set()
    stored |= st.audience_keys()
    prefix_of = {source: prefix for prefix, source in _AUDIENCE_SOURCE.items()}
    need: set[str] = set()
    for bid in sorted(bindings):
        if bid not in schema.bindings:
            raise ValueError(f"lateness_binding_unknown:{bid}")   # fail closed
        tid, binding = schema.bindings[bid]
        source, relation = binding["source"], binding["relation"]
        prefix = prefix_of.get(source)
        if source == "portal_api":
            need.add(f"api:{relation}")
        elif source == "bc":
            # per company where the store holds that relation's companies; else the tool
            # asks whether the grant covers the whole family: only a family-wide pattern
            # grants the literal ``bc:*`` key
            company = {k for k in stored if k.startswith("bc:") and k.endswith(f"/{relation}")}
            need |= company or {"bc:*"}
        elif prefix is not None and prefix not in _ITEM_ACL_AUDIENCES:
            need |= {f"{prefix}:*"} | {k for k in stored if k.startswith(f"{prefix}:")}
        prefix_id = f"{tid}:{bid}:"
        need |= {r[0] for r in st.conn.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_type = ? AND "
            "substr(object_id, 1, ?) = ?", (tid, len(prefix_id), prefix_id))}
    return sorted(need)


# The audience prefixes of the record systems a lateness verdict decides on.
_DECIDING_AUDIENCES = ("api:", "bc:")


def _reader_judges_lateness(st: _Store, may_read: Callable[[str], bool] | None) -> bool:
    """Whether the run principal (``may_read``: its grant; None: not checked) reads every
    record-system audience (Portal ``api:``, ``bc:``: the status, lines, requested-by dates
    and receipts a verdict decides on) the ``po_delivery_window`` graph reads that the
    store holds records of (``_lateness_audiences``; a source with no record here blinds
    no verdict). A supplier promise read from a document source (mail, SharePoint: item
    permissions, gated per message) never decides late and is a stated caveat
    (``promises_unproven``), so it is not required here."""
    if may_read is None:
        return True
    row = st.meta("audience_keys")
    held = set(json.loads(row)) if row else set()
    held |= st.audience_keys()
    return all(may_read(key) for key in _lateness_audiences(st)
               if key in held and key.startswith(_DECIDING_AUDIENCES))


# A gap whose date is a supplier's promise says, to a reader who may not read every source
# a promise sits in (or when the run's own reader could not see every promise), that a
# later promise it cannot see may exist.
PROMISE_CAVEAT = "later_promise_may_exist"
PROMISE_CAVEAT_TEXT = " (a later promise could sit in mail you cannot open)"


def _promise_audiences(st: _Store) -> list[str]:
    """Every audience a supplier's promise may sit in: the stored supplier confirmations'
    and the mailboxes' (a promise is read from the carrying message too)."""
    row = st.meta("audience_keys")
    keys = {k for k in (json.loads(row) if row else ()) if isinstance(k, str)}
    keys |= st.audience_keys()
    found = {r[0] for r in st.conn.execute(
        "SELECT DISTINCT audience_key FROM objects WHERE object_type = ?",
        (po_status.CONFIRMATION_TYPE,))}
    return sorted(found | {k for k in keys if k.startswith("mail:")})


def _promise_caveat(gap: dict[str, Any] | None, unless: Sequence[str], always: bool) -> None:
    """Record on ``gap`` the audiences a reader must read for the promise it rests on to
    be every promise there is (``read_inbox`` adds the caveat for any other reader;
    ``always``: the run's own reader could not see every promise either)."""
    if gap is not None:
        gap["follow_up"]["promise_caveat"] = {"unless": sorted(set(unless)),
                                              "always": bool(always)}


def _caveat_all(gap: dict[str, Any] | None, *, unless: Sequence[str]) -> None:
    _promise_caveat(gap, unless, False)


def _line_cites(verdict: Any, chain: Any) -> list[Mapping[str, Any]]:
    """The cited per-item promise the chain's line is judged by, when its own promise
    decides (``impact_chain.Late.own_promise``)."""
    own = verdict.own_promise(chain.line)
    return list(own.cites) if own is not None else []


def _line_event(own: Any, chain: Any, required: date | None) -> dict[str, Any]:
    """The evidence trail of a per-line verdict: the PO line, how it was matched to the
    confirmation's item, its promised date and the order's required date."""
    if own is None or own.promised is None:
        return {}
    return {"line": chain.line, "matched_by": own.matched_by,
            "line_promised": own.promised.isoformat(),
            **({"required": required.isoformat()} if required is not None else {})}


def _orders_at_risk(st: _Store, as_of: date, call: Callable | None, bound: int,
                    first: Iterable[str] = (), many: Callable | None = None
                    ) -> dict[str, Any]:
    """``orders_at_risk``: one gap per customer order a late PO puts at risk, and per late
    PO one ``po_impact`` gap (the orders at risk it feeds, never money) and, when every one
    of those orders has a stated margin in one currency, one ``po_impact_margin`` gap with
    the margin at stake whose every figure is its own finance-marked citation group (only a
    reader holding finance, who may open every figure, sees it). ``first``: the purchase
    orders the late rules found, walked first within ``bound``. Each gap's event names the
    chain's ends its groups cite (``pos``: the late PO an order's worst chain starts from;
    ``orders``: the orders a PO's impact reaches), so a reader's list joins a late PO to
    the customer orders it holds up only among gaps that reader may see. ``many``: the
    run's process pool (``run(pool=)``): every PO reaching an assembly is asked, not
    ``bound`` of them; one the time left did not reach is ``po_not_checked``."""
    if call is None:
        return _result([], status="not_configured", reasons=["tool_caller_not_given"])
    from . import impact_chain as ic

    found = ic.scan(st, call, bound=None if many is not None else bound,
                    margin_values=ic.store_margin_values(st),
                    landed_values=ic.investigate_landed_values(call), first=frozenset(first),
                    call_many=many)
    risky = found["assessment"].at_risk
    requires = _lateness_audiences(st) if risky else []
    late = found["late"]
    handles = {v["citation"]["l1_handle"] for order in risky
               for v in late[order.worst].cites}
    # per line: the confirmation's cited per-item promise of the line each chain leaves by
    handles |= {v["citation"]["l1_handle"] for order in risky for chain in order.chains
                for v in _line_cites(late[chain.po], chain)}
    handles |= {ref for order in risky for ref in order.required_from}
    records = {r for order in risky for chain in order.chains if chain.po == order.worst
               for r in chain.records}
    by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    impact_versions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for version in st.versions(objects=records | {r for order in risky for chain in order.chains
                                                  for r in chain.records}):
        impact_versions[version["object_id"]].append(version)
        if version["object_id"] in records:
            by_object[version["object_id"]].append(version)
    handles |= {v["citation"]["l1_handle"] for order in risky for chain in order.chains
                for v in late[chain.po].cites}
    handles |= {r for margin in found["margins"].values() for f in margin.figures
                for r in (f.refs or (f.ref,))}
    handles |= {ref for order in risky for chain in order.chains for ref in chain.refs}
    held = {v["l1_handle"]: v for v in st.versions(handles=handles)}

    def cite_value(fig: Any) -> Cite:
        """A margin figure's citation at its value's own markings (finance) joined with its
        record's: a reader who does not hold finance never sees the money claim."""
        version = held.get(fig.ref)   # a handle the store does not hold: shown to nobody
        return ((fig.ref, version["object_id"], _markings(version["markings"], fig.markings))
                if version else (fig.ref, None, UNREADABLE))

    def cite(handle: str) -> Cite:
        version = held.get(handle)   # a handle the store does not hold: shown to nobody
        return ((handle, version["object_id"], _markings(version["markings"])) if version
                else (handle, None, UNREADABLE))

    unless = _promise_audiences(st) if risky else []
    gaps = []
    skipped: Counter[str] = Counter()
    for order in risky:
        chain = order.chain_of(order.worst)
        verdict = late[order.worst]
        own = verdict.own_promise(chain.line)
        into: dict[str, list[str]] = defaultdict(list)
        for edge in chain.edges:
            into[edge.frm].append(edge.markings)
            into[edge.to].append(edge.markings)
        # each lateness value its own group: a reader who may not open one of them never
        # sees the verdict that rests on it
        groups = [[cite(v["citation"]["l1_handle"])] for v in late[order.worst].cites]
        # the line's own promise: the confirmation's cited per-item date, its own group
        groups += [[cite(v["citation"]["l1_handle"])] for v in _line_cites(verdict, chain)]
        groups += [[(v["l1_handle"], record, _markings(v["markings"], *into[record]))
                    for v in by_object.get(record, ())] for record in chain.records]
        groups.append([cite(ref) for ref in order.required_from])
        # a lead's own basis (the planned date it was paired by) is cited too
        groups += [[cite(ref)] for ref in chain.refs]
        pos = len({c.po for c in order.chains})
        noun = "purchase order" if pos == 1 else "purchase orders"
        # the day measured stands for "not received yet", never for a date the material
        # comes: the PO's own due date is stated, and the event holds no material date
        pending = order.material_how.startswith("not received as of")
        due = verdict.governing
        material = (("its material is not received yet"
                     + (f" (the purchase order was due {due.isoformat()})" if due else ""))
                    if pending else
                    f"material {order.material.isoformat()} ({order.material_how})")
        firm = chain.tier == "firm"
        how = ("an assembly order made or allocated for this order (recorded links, each "
               "cited)" if firm else
               f"an assembly order tied to this order only by a lead ({ic.CHAIN_BASES[chain.basis]}; "
               "each record it rests on cited)")
        gap = _gap(
            "order_at_risk", "orders_at_risk", order.key, "SalesOrder", "at_risk",
            summary=("" if firm else "Lead, not firm: ")
                    + f"{order.days} days at risk from {pos} late {noun} (days at risk is a "
                    "lower bound: no assembly lead time is recorded)",
            missing="Confirm whether the order's required date still holds, or when the "
                    "late purchase order's material will arrive"
                    + ("" if firm else ", and whether this assembly order is made for it"),
            why=pg._clip(f"The item of a late purchase order is consumed by {how}: "
                         f"{material}, against the required date "
                         f"{order.required.isoformat()}."),
            priority=("high" if order.days >= 7 else "medium") if firm else
                     ("medium" if order.days >= 7 else "low"),
            about=("SalesOrder", "required_date"), groups=groups,
            event={"days": order.days, "orders_affected": 1, "pos": [order.worst],
                   **({"material_pending": True,
                       **({"due": due.isoformat()} if due else {})} if pending else
                      {"material": order.material.isoformat()}),
                   **({"required": order.required.isoformat()} if order.required else {}),
                   **_line_event(own, chain, order.required)})
        if gap is None:
            skipped["order_at_risk_uncited"] += 1
        else:
            gap["requires"] = requires
            if verdict.rests_on_promise(chain.line):
                _promise_caveat(gap, unless, verdict.unseen_promise)
            gaps.append(gap)
    by_key = {order.key: order for order in risky}
    for impact in found["po_impacts"]:
        orders = [by_key[k] for k in impact["orders"]]
        if not orders:
            continue
        po = impact["po"]
        # the PO's lateness values, then every record of each chain from it, each its own
        # group: a reader who may not open one never sees the claim
        groups = [[cite(v["citation"]["l1_handle"])] for v in late[po].cites]
        for order in orders:
            chain = order.chain_of(po)
            groups += [[cite(v["citation"]["l1_handle"])] for v in _line_cites(late[po], chain)]
            into: dict[str, list[str]] = defaultdict(list)
            for edge in chain.edges:
                into[edge.frm].append(edge.markings)
                into[edge.to].append(edge.markings)
            groups += [[(v["l1_handle"], record, _markings(v["markings"], *into[record]))
                        for v in impact_versions.get(record, ())] for record in chain.records]
            groups.append([cite(ref) for ref in order.required_from])
        n = len(orders)
        noun = "sales order" if n == 1 else "sales orders"
        worst = max(o.days for o in orders)
        missing = ("Confirm when the late purchase order's material will arrive, or whether "
                   "the sales orders' required dates still hold")
        leads = sum(1 for o in orders if o.chain_of(po).tier != "firm")
        groups += [[cite(ref)] for o in orders for ref in o.chain_of(po).refs]
        why = pg._clip(f"An item of this purchase order is consumed by assembly orders made or "
                       f"allocated for {n} {noun} at risk (recorded links, each cited"
                       + (f"; {leads} of them tied by a lead only, not firm: inferred by "
                          "item and date or mentioned in a document" if leads else "")
                       + f"); the worst is {worst} days at risk (a lower bound: no assembly "
                       "lead time is recorded).")
        priority = "high" if worst >= 7 else "medium"
        plain = _gap("po_impact", "orders_at_risk", po, "PurchaseOrder", "impact",
                     summary=f"affects {n} {noun}, worst {worst} days at risk",
                     missing=missing, why=why, priority=priority,
                     about=("PurchaseOrder", "lateness"), groups=groups,
                     event={"days": worst, "orders_affected": n,
                            "orders": [o.key for o in orders]})
        if plain is None:
            skipped["po_impact_uncited"] += 1
            continue
        plain["requires"] = requires
        promised = any(late[po].rests_on_promise(o.chain_of(po).line) for o in orders)
        if promised:
            _promise_caveat(plain, unless, late[po].unseen_promise)
        gaps.append(plain)
        margin = impact["margin"]
        if margin is None or impact["orders_with_margin"] != n:
            continue   # the money claim needs every order's margin: never a part as the whole
        figures = [f for order in orders for f in found["margins"][order.key].figures]
        # a landed-cost engine figure: each cited value it was computed from is its own
        # finance-marked group (a reader who may not open one never sees the claim)
        value_groups = [[cite_value(f)] if not f.refs else [cite_value(ic.Figure(
            f.amount, f.currency, f.unit, ref, f.object_id, f.markings))] for f in figures
            for ref in (f.refs or (f.ref,))]
        money = _gap("po_impact_margin", "orders_at_risk", po, "PurchaseOrder", "margin",
                     summary=f"affects {n} {noun}, {margin['amount']} {margin['currency']} "
                             f"margin at stake ({margin['basis']}), worst {worst} days at "
                             "risk",
                     missing=missing, why=pg._clip(why + " " + ic.MARGIN_BASIS + "."),
                     priority=priority, about=("PurchaseOrder", "lateness"),
                     groups=groups + value_groups,
                     event={"days": worst, "orders_affected": n,
                            "amount": {"value": str(margin["amount"]),
                                       "currency": margin["currency"],
                                       "basis": "margin_at_stake"}})
        if money is None:
            skipped["po_impact_margin_uncited"] += 1
        else:
            money["requires"] = requires
            if promised:
                _promise_caveat(money, unless, late[po].unseen_promise)
            gaps.append(money)
    if found["lateness_undetermined"]:
        skipped["lateness_undetermined"] = found["lateness_undetermined"]
    if found["pos_not_checked"]:
        skipped["po_not_checked"] = found["pos_not_checked"]
    unread = found.get("pos_not_checked_unread", 0)
    failed = found.get("pos_not_checked_failed", 0)
    cut_by_bound = found["pos_not_checked"] - unread - failed
    result = _result(gaps, status="truncated" if found["truncated"] else "complete",
                     reasons=(["impact_bound"] if cut_by_bound else [])
                     + ([TIME_BUDGET] if unread else [])
                     + (["worker_failed"] if failed else []),
                     not_evaluated=skipped)
    total = found.get("pos_reaching_assembly", found["pos_checked"] + found["pos_not_checked"])
    result["coverage"] = _coverage(
        total, total - found["pos_not_checked"] - found["lateness_undetermined"],
        {"lateness_undetermined": found["lateness_undetermined"],
         **({"impact_bound": cut_by_bound} if cut_by_bound else {}),
         **({TIME_BUDGET: unread} if unread else {}),
         **({"worker_failed": failed} if failed else {})},
        unit="purchase_orders_reaching_an_assembly")
    result["finding"] = {"orders_at_risk": len(risky),
                         "orders_at_risk_by_tier": found["orders_at_risk_by_tier"],
                         "late_pos": found["late_pos_with_orders_at_risk"],
                         "text": ic.headline(len(risky),
                                             found["late_pos_with_orders_at_risk"])}
    return result


LATE_FOUND_KINDS = frozenset({"late_po", "late_order", "unconfirmed_po", "eta_slipped"})
IMPACT_KINDS = ("order_at_risk", "po_impact", "po_impact_margin")


def _late_list_only(st: _Store, results: dict[str, dict[str, Any]], as_of: date) -> None:
    """One late rule for Late, Home and the briefing: a customer order is at risk from a
    purchase order that is not received only while that order is on the late list
    (``LATE_RULE``'s gaps that state days late, Home's ``late_total``), across its copies
    (the rule's ``po_orders`` join). ``plx.process`` may judge a PO late that the list does
    not hold (a line's bulk import date, or no requested date at all) and then dates the
    material by the day measured: such an impact is dropped, counted
    ``po_not_on_late_list``. A material date that is a cited fact (a receipt, a supplier's
    promise for the line) stands on its own citation. When the late rule did not run this
    run it is read once; when it failed nothing is on the list."""
    found = results.get("orders_at_risk")
    if not found or not found.get("gaps"):
        return
    def event_of(gap: Mapping[str, Any]) -> Mapping[str, Any]:
        return (gap.get("follow_up") or {}).get("event") or {}

    late = results.get(LATE_RULE)
    if late is None:
        try:
            late = _late_or_unconfirmed_po(st, as_of)
        except Exception:  # noqa: BLE001 - a late check that cannot run drops nothing
            late = None
    if late is None or late.get("status") in ("failed", "truncated"):
        # the late list is unknown or incomplete: every impact stays, and the ones resting
        # on the day measured say their late status was not checked
        for gap in found["gaps"]:
            event = event_of(gap)
            if isinstance(event, dict) and (event.get("material_pending")
                                            or gap.get("kind") != "order_at_risk"):
                event["late_status_not_checked"] = True
        return
    joined = late.get("po_orders") or {}
    listed = {((gap.get("follow_up") or {}).get(_ORDER) or {}).get("key") or gap["object_id"]
              for gap in late.get("gaps") or ()
              if (gap.get("follow_up") or {}).get("days_late") is not None}

    def on_list(po: Any) -> bool:
        return isinstance(po, str) and (joined.get(po) or (po,))[0] in listed

    # a PO with an order whose material date is a cited fact, not the day measured
    dated = {p for gap in found["gaps"] if gap.get("kind") == "order_at_risk"
             and not event_of(gap).get("material_pending") for p in event_of(gap).get("pos") or ()}

    def stands(gap: Mapping[str, Any]) -> bool:
        if gap.get("kind") == "order_at_risk":
            pos = event_of(gap).get("pos") or ()
            return bool(pos) and (not event_of(gap).get("material_pending")
                                  or all(on_list(p) for p in pos))
        po = gap.get("object_id")
        return on_list(po) or po in dated

    kept, dropped = [], 0
    for gap in found["gaps"]:
        if gap.get("kind") not in IMPACT_KINDS or stands(gap):
            kept.append(gap)
        else:
            dropped += 1
    if dropped:
        found["gaps"] = kept
        skipped = found.setdefault("not_evaluated", {})
        if isinstance(skipped, dict):
            skipped["po_not_on_late_list"] = skipped.get("po_not_on_late_list", 0) + dropped
        finding = found.get("finding")
        if isinstance(finding, dict):
            risky = sum(1 for g in kept if g.get("kind") == "order_at_risk")
            pos = len({g["object_id"] for g in kept if g.get("kind") == "po_impact"})
            from . import impact_chain as ic
            finding.update({"orders_at_risk": risky, "late_pos": pos,
                            "text": ic.headline(risky, pos)})


def _late_found(results: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """The purchase orders this run's earlier rules found late, unconfirmed or slipping
    (their gaps' subjects): the impact walk asks their verdicts first."""
    return {str(gap.get("object_id")) for result in results.values()
            for gap in result.get("gaps") or ()
            if gap.get("kind") in LATE_FOUND_KINDS and gap.get("object_id")}


def _card_documents(st: _Store, as_of: date, limit: int) -> list[str]:
    """The attesting documents ``card_rules`` reads, newest first (at most ``limit``): only
    those whose card could raise a gap (``proactive_gaps.card_candidates``, one set-based
    pass over the current links and values, a superset; the card stays the judge). Before,
    the newest ``CARD_BOUND`` of all ~176k documents were read one card each and a pass
    never finished (11-13: 216 s for 500 cards, truncated)."""
    documents = [name for name, _field in pg._roster()[0]]
    wanted = sorted(pg.card_candidates(st.conn, as_of))
    return [row[0] for row in st.conn.execute(
        "SELECT o.object_id FROM objects o WHERE o.object_type IN (SELECT value FROM "
        "json_each(?)) AND o.object_id IN (SELECT value FROM json_each(?)) AND NOT "
        + ostore.removed_at_sql("o.object_id")
        + " ORDER BY o.first_recorded_at DESC, o.object_id LIMIT ?",
        (json.dumps(documents), json.dumps(wanted), limit))]


def _card_rules(st: _Store, as_of: date, call: Callable | None, bound: int,
                many: Callable[[str, Sequence[Any]], list[Any]] | None = None
                ) -> dict[str, Any]:
    """``missing_in_record`` and attested conflicts on the attesting documents' cards (the
    newest ``bound``). ``many``: the run's process pool reads the cards side by side (one
    the time left did not reach is not read: the budget marks the rule truncated)."""
    if call is None:
        return _result([], status="not_configured", reasons=["tool_caller_not_given"])
    # on the run's pool every candidate is read (the window decides, as orders_at_risk
    # asks every PO); one by one, at most ``bound``
    ids = _card_documents(st, as_of, (bound if many is None else CARD_ALL) + 1)
    unread: Counter[str] = Counter()
    skipped = _Skips()
    found: list[tuple[dict[str, Any], list[list[str]]]] = []
    asked = [{"object_id": object_id, "response_format": "detailed"} for object_id in ids[:bound]]
    pooled = many("plx.object", asked) if many is not None else None
    for n, object_id in enumerate(ids[:bound]):
        if pooled is None:
            answer = call("plx.object", asked[n])
        else:
            from . import proactive_pool
            answer = pooled[n]
            if answer is proactive_pool.NOT_READ:
                continue   # the time left did not reach it (``Budget.pooled`` noted it)
        error = answer.get("error") if isinstance(answer, Mapping) else {"code": "error"}
        if error:
            code = error.get("code") if isinstance(error, Mapping) else None
            unread[code if isinstance(code, str) else "error"] += 1
            skipped.note(f"unread_{code if isinstance(code, str) else 'error'}", [object_id])
            continue
        conflicts = answer.get("conflicts") if isinstance(answer.get("conflicts"), list) else []
        card = {"object": answer.get("object"), "leads": answer.get("leads"), "values": [],
                "conflicts": [c for c in conflicts if isinstance(c, Mapping) and c.get("tier")]}
        found.extend(pg._detect(card, as_of))
    held = {v["l1_handle"]: v for v in st.versions(handles={
        h for gap, requires in found for h in gap["citations"]})}

    def cite(handle: str) -> Cite:
        version = held.get(handle)   # a handle the store does not hold: shown to nobody
        return ((handle, version["object_id"], _markings(version["markings"])) if version
                else (handle, None, UNREADABLE))

    gaps = []
    seen: set[tuple[str, str, frozenset[str]]] = set()
    repeats = 0
    for gap, requires in found:
        follow = gap["follow_up"]
        records: frozenset[str] = frozenset()
        if gap["kind"] == pg.KINDS[0]:
            # the records the document attests (every empty read's own record): one gap
            # per (records, field) however many documents state it (the newest document
            # is read first and kept; 11-13 judging: one order raised up to 4 times, once
            # per assembly packet), and the order among them carried on the follow-up so
            # the active-population filter leaves a closed or historic order out
            records = frozenset(held[h]["object_id"] for group in requires[1:]
                                for h in group if h in held)
            if records:
                if (gap["kind"], gap["summary"], records) in seen:
                    repeats += 1
                    continue
                seen.add((gap["kind"], gap["summary"], records))
        made = _gap(
            gap["kind"], "card_rules", gap["object_id"], gap["object_type"],
            gap["id"], summary=gap["summary"], missing=follow["missing"], why=follow["why"],
            priority=follow["priority"], about=(gap["object_type"], ""),
            groups=[[cite(h) for h in group] for group in requires],
            extra=[cite(h) for h in gap["citations"]])
        orders = sorted(r for r in records if r.split(":", 1)[0] in ORDER_TYPES)
        if made is not None and orders:
            made["follow_up"][_ATTESTS] = orders[0]
        gaps.append(made)
    status = "truncated" if len(ids) > bound or unread else "complete"
    out = _result(gaps, status=status,
                  reasons=(["card_bound"] if len(ids) > bound else [])
                  + [f"unread:{code}" for code in sorted(unread)],
                  not_evaluated=skipped)
    out["counts"] = {"cards": len(ids[:bound]), "repeats_merged": repeats}
    return out


# --------------------------------------------------------------------------- the inbox

# The cited amount an event about a record of these types is ranked by (the record's own
# total and its currency, both one value across its sources): (amount, currency) property.
AMOUNT_OF = {"PurchaseOrder": ("total_cost", "currency"),
             "SalesOrder": ("total_amount", "currency"),
             "Quote": ("total_amount", "currency"),
             "PurchaseInvoice": ("total_amount", "currency")}


# A sales order's value derived where its header states none (``operating.order_value``):
# the amount's basis, as the briefing words it.
DERIVED_BASIS = {order_value.INVOICES: "order_value_invoices",
                 order_value.ORDER_LINES: "order_value_lines"}


def _business_steps(st: _Store, as_of: date,
                    on_hand: Sequence[tuple[str, str, str | None]] | None,
                    people: Callable[[], Any] = lambda: None,
                    moment: datetime | None = None, sheets: Path | None = None
                    ) -> dict[str, Callable[[], dict[str, Any]]]:
    """The business alerts (``operating.proactive_business``) as engine steps; ``on_hand``:
    where on hand is read (None: what the schema declares, ``declared_on_hand``);
    ``sheets``: the sheet readings file (``proactive_sheets.readings_path``)."""
    from . import proactive_business as pb
    from . import proactive_ceo as ceo
    from . import proactive_chasers as chasers
    from . import proactive_mail_pains as mail_pains
    from . import proactive_cash as cash
    from . import proactive_stock as stock
    from . import proactive_prices as prices
    from . import proactive_sales as sales
    from . import business_documents as docs
    from . import proactive_quality as quality
    from . import proactive_sync as sync
    from . import proactive_commitments as commit
    from . import proactive_integrity as integrity
    from . import proactive_sheets as sheet_rules

    common = {"gap": _gap, "cites": _cites, "result": _result, "settled": SETTLED,
              "skips": _Skips}
    return {
        # with sheet readings served, stock reads mail: people first, fail closed
        "stock_out_risk": lambda: (sheets is not None and sheet_rules.people_blocked(
            people(), _result))
        or pb.stock_out_risk(
            st, as_of, orders=lambda: _customer_orders(st), po_source=_po_source(st),
            absence=lambda types: _absence_audiences(st, types, "stock:none_stored"),
            on_hand=on_hand, sheet_on_hand=None if sheets is None else (
                lambda item_of, wanted, cites, skipped: sheet_rules.sheet_on_hand(
                    st, as_of, sheets, people(), item_of, wanted, cites, skipped)),
            **common),
        "overdue_receivable": lambda: pb.overdue_receivable(
            st, as_of, chases=lambda invoices: mail_pains.receivable_chases(
                st, people(), as_of, invoices, {}), **common),
        "unit_cost_jump": lambda: pb.unit_cost_jump(st, as_of, po_source=_po_source(st),
                                                    **common),
        # the chief-executive signals (``operating.proactive_ceo``)
        "customer_demand_drop": lambda: ceo.customer_demand_drop(
            st, as_of, orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "promise_date_slip": lambda: ceo.promise_date_slip(
            st, as_of, orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "supplier_reliability": lambda: ceo.supplier_reliability(
            st, as_of, po_source=_po_source(st),
            absence=lambda types: _absence_audiences(st, types, "receipt:none_stored"),
            **common),
        "shipped_not_invoiced": lambda: pb.shipped_not_invoiced(
            st, as_of, absence=lambda types: _absence_audiences(st, types,
                                                                "invoice:none_stored"),
            **common),
        "duplicate_customer_order": lambda: pb.duplicate_customer_order(st, as_of,
                                                                    **common),
        "recurring_component_damage": lambda: pb.recurring_component_damage(
            st, as_of, **common),
        # the customer chase and quality-hold alerts (``operating.proactive_chasers``)
        "customer_chase": lambda: chasers.customer_chase(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "quality_holds": lambda: chasers.quality_holds(st, as_of, **common),
        # pains found in the mail (``operating.proactive_mail_pains``)
        "expedite_requests": lambda: mail_pains.expedite_requests(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            po_source=lambda: _po_source(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "supplier_shortage": lambda: mail_pains.supplier_shortage(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            po_source=lambda: _po_source(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "payment_chase": lambda: mail_pains.payment_chase(st, as_of, people=people(),
                                                          **common),
        "return_requests": lambda: mail_pains.return_requests(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        # receivables and payables (``operating.proactive_cash``)
        "customer_payment_behaviour": lambda: cash.customer_payment_behaviour(
            st, as_of, **common),
        "early_pay_discounts": lambda: cash.early_pay_discounts(st, as_of, **common),
        "duplicate_payables": lambda: cash.duplicate_payables(st, as_of, **common),
        "returns_and_credits": lambda: cash.returns_and_credits(st, as_of, **common),
        # operations (``operating.proactive_stock``)
        "slow_moving_stock": lambda: stock.slow_moving_stock(
            st, as_of, absence=lambda types: _absence_audiences(st, types, "stock:none_stored"),
            on_hand=on_hand, **common),
        "expiring_lot_stock": lambda: (sheets is not None and sheet_rules.people_blocked(
            people(), _result))
        or stock.expiring_lot_stock(
            st, as_of, orders=lambda: _customer_orders(st), on_hand=on_hand,
            sheet_lots=None if sheets is None else (
                lambda item_of, horizon, exclude, cites, skipped: sheet_rules.sheet_lots(
                    st, as_of, sheets, people(), item_of, horizon, exclude, cites,
                    skipped)),
            **common),
        "assembly_component_shortfall": lambda: stock.assembly_component_shortfall(
            st, as_of, po_source=lambda: _po_source(st),
            absence=lambda types: _absence_audiences(st, types, "stock:none_stored"),
            on_hand=on_hand, **common),
        "certificate_expiry": lambda: stock.certificate_expiry(st, as_of, **common),
        "freight_cost_anomaly": lambda: stock.freight_cost_anomaly(st, as_of, **common),
        # what each customer pays for the same product (``operating.proactive_prices``)
        "price_consistency": lambda: prices.price_consistency(st, as_of, **common),
        # sales (``operating.proactive_sales``)
        "stale_quote": lambda: sales.stale_quote(st, as_of, **common),
        # priority / fill lists (``operating.business_documents``)
        "priority_fill_unmet": lambda: docs.priority_fill_unmet(st, as_of, **common),
        # data quality (``operating.proactive_quality``)
        "duplicate_parties": lambda: quality.duplicate_parties(st, as_of, **common),
        "key_fields_missing": lambda: quality.key_fields_missing(
            st, as_of, absence=lambda types: _absence_audiences(st, types,
                                                                "record:none_stored"),
            status_of={"PurchaseOrder": lambda: _po_classes(st, as_of),
                       "SalesOrder": lambda: st.memo("quality:so_open", lambda: {
                           m: v.cls for v in _customer_orders(st)[1] for m in v.group})},
            **common),
        "twin_disagreement": lambda: quality.twin_disagreement(st, as_of, **common),
        "stale_captures": lambda: quality.stale_captures(st, as_of, now=moment, **common),
        "sync_gaps": lambda: sync.sync_gaps(
            st, as_of, inactive=lambda: inactive_records(st, as_of),
            tests=lambda: test_records(st, as_of), **common),
        "reorder_soon": lambda: (sheets is not None and sheet_rules.people_blocked(
            people(), _result))
        or stock.reorder_soon(
            st, as_of, po_source=lambda: _po_source(st),
            absence=lambda types: _absence_audiences(st, types, "stock:none_stored"),
            on_hand=on_hand, sheet_on_hand=None if sheets is None else (
                lambda item_of, wanted, cites, skipped: sheet_rules.sheet_on_hand(
                    st, as_of, sheets, people(), item_of, wanted, cites, skipped)),
            **common),
        # integrity (``operating.proactive_integrity``)
        "inventory_adjustment": lambda: integrity.inventory_adjustment(st, as_of, **common),
        "blocked_account_activity": lambda: integrity.blocked_account_activity(
            st, as_of, order_state=_order_state(st, as_of),
            absence=lambda types: _absence_audiences(st, types, "order:none_stored"),
            **common),
        "gl_posting_errors": lambda: integrity.gl_posting_errors(st, as_of, **common),
        "recurring_task_missed": lambda: integrity.recurring_task_missed(
            st, as_of, people=people(), **common),
        # sheet readings (``operating.proactive_sheets``)
        "cash_application": lambda: sheet_rules.cash_application(
            st, as_of, people=people(), readings=sheets or Path("/nonexistent"), **common),
        "statement_reconciliation": lambda: sheet_rules.statement_reconciliation(
            st, as_of, people=people(), readings=sheets or Path("/nonexistent"), **common),
        # promises we made and mail waiting on a reply (``operating.proactive_commitments``)
        **{name: (lambda rule=rule: rule(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common))
           for name, rule in (("commitments", commit.commitments),
                              ("needs_reply", commit.needs_reply))}}


def _sheets(store_path: Any, given: Any) -> Path | None:
    """The sheet readings file the run reads (``proactive_sheets.readings_path``), None
    when there is none: the sheet rules are then ``blocked`` and stock reads no sheet."""
    from . import proactive_sheets as sheet_rules
    path = Path(given) if given is not None else sheet_rules.readings_path(store_path)
    return path if path.is_file() else None


def _order_state(st: _Store, as_of: date) -> dict[str, Callable[[], Callable[[str], Any]]]:
    """Per order type, a reader of one order record's (cluster, status class) by the one
    status definition of its type: customer orders from the run's one order read
    (``_customer_orders``), purchase orders read only for the clusters asked about."""
    def customer() -> Callable[[str], Any]:
        state = {m: (v.top, v.cls) for v in _customer_orders(st)[1] for m in v.group}
        return state.get

    def purchase() -> Callable[[str], Any]:
        src = _po_source(st)
        top_of = {m: top for top, members in src.clusters.items() for m in members}
        read: dict[str, str] = {}

        def one(member: str) -> Any:
            top = top_of.get(member)
            if top is None:
                return None
            if top not in read:
                read[top] = po_status.read(src, top, as_of).status.cls
            return top, read[top]
        return one
    return {"SalesOrder": customer, "PurchaseOrder": purchase}


def _ops_steps(st: _Store, as_of: date, people: Callable[[], Any],
               sources: Mapping[str, Any] | None
               ) -> dict[str, Callable[[], dict[str, Any]]]:
    """The operations alerts (``operating.proactive_ops``) as engine steps; ``sources``:
    overrides of what the schema declares (``proactive_ops.declared_sources``)."""
    from . import proactive_ops as po
    from . import inbox_triage, label_changes, label_revisions, rd_projects
    from . import proactive_threads as threads

    given = dict(sources or {})
    common = {"gap": _gap, "cites": _cites, "result": _result, "skips": _Skips}
    tasks = lambda: st.memo("ops:mc_tasks", lambda: po.mc_tasks(   # noqa: E731
        st, as_of, people=people(), settled=SETTLED, due_props=given.get("mc_task_due"),
        **common))
    return {
        "mc_task_overdue": lambda: tasks()[po.MC_OVERDUE],
        "mc_task_stuck": lambda: tasks()[po.MC_STUCK],
        "mc_approval_pending": lambda: po.mc_approval_pending(
            st, as_of, people=people(), settled=SETTLED, **common),
        "portal_approval_pending": lambda: po.portal_approval_pending(
            st, as_of, people=people(), settled=SETTLED, **common),
        "planner_task_overdue": lambda: po.planner_task_overdue(
            st, as_of, people=people(), settled=SETTLED, **common),
        "aged_balance_worsening": lambda: po.aged_balance_worsening(
            st, as_of, settled=SETTLED, **common),
        "production_late": lambda: po.production_late(
            st, as_of, settled=SETTLED, sources=given.get("production_due"), **common),
        "assembly_damage_spike": lambda: po.assembly_damage_spike(
            st, as_of, settled=SETTLED, dates=given.get("damage_date"), **common),
        "teams_unanswered_ask": lambda: po.teams_unanswered_ask(
            st, as_of, people=people(), text_props=given.get("ask_text"), **common),
        # mail threads waiting on someone (``operating.proactive_threads``)
        "approvals_waiting": lambda: threads.approvals_waiting(
            st, as_of, people=people(), **common),
        "chase_escalation": lambda: threads.chase_escalation(
            st, as_of, people=people(), **common),
        "party_chases": lambda: threads.party_chases(st, as_of, people=people(), **common),
        "internal_chases": lambda: threads.internal_chases(st, as_of, people=people(),
                                                           **common),
        "inbox_triage": lambda: inbox_triage.inbox_triage(st, as_of, people=people(), **common),
        "label_changes": lambda: label_changes.label_changes(
            st, as_of, people=people(), **common),
        "label_revisions": lambda: label_revisions.label_revisions(
            st, as_of, people=people(), **common),
        "rd_projects": lambda: rd_projects.rd_projects(st, as_of, people=people(), **common)}


# A cached margin reading is kept only when it is whole: decided (a figure stated) and
# read from every answer page; any other reading is read again next run (a refused or
# partly read run may not repeat).
_MARGIN_TRANSIENT = frozenset({"ORDER_MARGIN_NOT_READ", "ANSWER_PAGES_UNREAD",
                               "ORDER_LINES_NOT_ALL_SHOWN"})


def _margin_keeps(order: Any, cp: Any) -> bool:
    return (isinstance(order, cp.Order) and order.margin is not None
            and not {code for code, _detail in order.gaps} & _MARGIN_TRANSIENT)


def _margin_reader(call: Any) -> str | None:
    """The reader a margin reading was read as, with what it is granted: tenant,
    principals, clearance and the decided policy's audience patterns and markings it holds
    (``host.stored_policy``), so a grant added or withdrawn reads every order again. None
    when the caller names no reader or its policy cannot be read: then nothing is reused."""
    actor = getattr(call, "actor", None)
    host = getattr(call, "host", None)
    if actor is None or not getattr(actor, "principals", None) or host is None:
        return None
    try:
        policy = host.stored_policy(actor)
        mine = frozenset(actor.principals)
        granted = sorted(p for p, who in policy.audiences.items() if mine & frozenset(who))
        held = sorted(m for m, who in policy.markings.items() if mine & frozenset(who))
        whole = bool(getattr(policy, "unrestricted", False))
    except Exception:  # noqa: BLE001 - a policy not read: nothing reused
        return None
    grants = hashlib.sha256(json.dumps([granted, held, whole]).encode()).hexdigest()
    return f"{reader_key(actor)}|{getattr(actor, 'clearance', '')}|{grants}"


def _event_steps(st: _Store, as_of: date, call: Callable | None,
                 cache: Mapping[str, Mapping[str, Any]] | None = None,
                 vendor_bound: int | None = None, *,
                 pmap: Callable[[Callable, Sequence[Any]], list[Any]] | None = None,
                 results: Callable[[], Mapping[str, Mapping[str, Any]]] = dict,
                 people: Callable[[], Any] = lambda: None, margins: Any = None
                 ) -> dict[str, Callable[[], dict[str, Any]]]:
    """The proactive event detectors (``operating.proactive_events``) as engine steps.
    ``results()``: this run's results so far (the vendor rule reads the late rule's order
    join, else reads it itself); ``people()``: the people exclusion with the tenant's
    mail directory (``operating.chunking.permissions.Exclusion``)."""
    from . import proactive_events as ev

    common = {"gap": _gap, "cites": _cites, "result": _result}
    mail: dict[str, Any] = {} if vendor_bound is None else {"bound": vendor_bound}
    if pmap is not None:
        mail["pmap"] = pmap

    def orders() -> Mapping[str, tuple[str, str]] | None:
        """The late rule's order join; None when it ran this run and did not finish (the
        orders open are then not known: the vendor rule is blocked, never guessed)."""
        ran = results()
        if LATE_RULE in ran:
            return ran[LATE_RULE].get("po_orders")
        return _late_or_unconfirmed_po(st, as_of)["po_orders"]

    def margin_map(task: Callable, items: Sequence[Any]) -> list[Any]:
        """The margins read since the run's start (``margins``: ``_HeadStart``)."""
        if task is margins.task and list(items) == margins.items:
            return st.memo("recent_margins", margins.collect)
        return pmap(task, items)

    def roots() -> list[str]:
        return st.memo("margin_roots", lambda: ev._recent_invoiced_orders(st, as_of))

    def order_margins(items: Sequence[str]) -> list[Any]:
        """Each order's checked margin (``read_margin``), the recent ones read once for
        the negative-margin rule and the erosion rule alike; without a pool one by one
        (an order whose run fails is undecided, never a figure)."""
        items = list(items)
        if margins is not None:
            return margin_map(ev.read_margin, items)
        if pmap is not None:
            if items == roots():
                return st.memo("recent_margins", lambda: pmap(ev.read_margin, items))
            return pmap(ev.read_margin, items)
        from .proactive_pool import NOT_READ

        out = []
        for n, root in enumerate(items):
            if n >= ev.CALL_BOUND:   # without a pool as bounded as the negative-margin rule
                out.append(NOT_READ)
                continue
            try:
                out.append(one_margin(root))
            except Exception as error:  # noqa: BLE001 - one order fails alone
                if type(error).__name__ == "TimeBudgetSpent":
                    raise
                out.append(None)
        return out

    def one_margin(root: str) -> Any:
        """One order's margin read once per run without a pool (both margin rules)."""
        return st.memo(f"margin:{root}", lambda: ev.read_margin(call, root))

    def erosion() -> dict[str, Any]:
        from . import customer_profit as cp
        from . import proactive_ceo as ceo
        from .proactive_pool import NOT_READ

        recent = roots() if call is not None else []
        trailing = st.memo("margin_trailing", lambda: ev._recent_invoiced_orders(
            st, as_of, days=ev.MARGIN_DAYS + ceo.MARGIN_TRAILING_DAYS,
            since=ev.MARGIN_DAYS)) if call is not None else []
        reader = _margin_reader(call)
        kept: dict[str, Any] = {}
        margins_of = order_margins
        if reader is not None and (recent or trailing):
            # the cross-run reading cache: an order whose fingerprint (every row its margin
            # run can read, ``ev.margin_fingerprints``) is unchanged keeps its reading; the
            # recent orders are read as the negative-margin rule reads them (shared)
            everything = list(dict.fromkeys([*recent, *trailing]))
            fingerprints = ev.margin_fingerprints(
                st, everything, ev.margin_context(st, reader, st.path.parent.parent))
            prior = (cache or {}).get("margin_erosion") or {}

            def margins_of(items: Sequence[str]) -> list[Any]:
                items = list(items)
                hits: dict[str, Any] = {}
                if items != list(recent):
                    for root in items:
                        held = prior.get(root)
                        if isinstance(held, Mapping) and held.get("fp") == fingerprints[root]:
                            try:
                                hits[root] = ev.margin_from_reading(held["order"])
                            except (KeyError, TypeError, ValueError, ArithmeticError):
                                pass   # a reading of another shape is read again
                misses = [root for root in items if root not in hits]
                read = dict(zip(misses, order_margins(misses) if misses else []))
                out = [hits[root] if root in hits else read[root] for root in items]
                for root, order in zip(items, out):
                    if _margin_keeps(order, cp):
                        kept[root] = {"fp": fingerprints[root],
                                      "order": ev.margin_reading(order)}
                st.memo("margin_cache_hits", dict).update(dict.fromkeys(hits, True))
                return out
        try:
            found = ceo.margin_erosion(
                st, as_of, call, recent=recent, trailing=trailing,
                margins=margins_of, not_read=NOT_READ, settled=SETTLED, skips=_Skips,
                **common)
        except TimeBudgetSpent:
            if not kept:
                raise
            # cut at its window's end after reading: the readings stay for the next run
            # (a pass spans runs until every order is held), nothing judged on a part
            found = _result([], status="truncated", reasons=[TIME_BUDGET])
        if reader is not None and (recent or trailing):
            found["cache"], found["roots"] = kept, list(dict.fromkeys([*recent, *trailing]))
            found.setdefault("counts", {})["margins_reused"] = len(
                st.memo("margin_cache_hits", dict))
        return found

    def no_reply() -> dict[str, Any]:
        found = orders()
        if found is None:
            return _result([], status="blocked", reasons=["order_status_not_read"])
        src = _po_source(st)
        exclusion = people()
        return ev.vendor_no_reply(st, as_of, people=exclusion, orders=found,
                                  order_key=_ORDER, colliding=src.colliding.__contains__,
                                  resolver=_mention_resolver(st, exclusion), **common)
    return {
        "eta_slipped": lambda: ev.eta_slipped(
            st, as_of, settled=SETTLED, confirming=CONFIRMING,
            caveat=functools.partial(_caveat_all, unless=_promise_audiences(st)),
            orders=_po_source(st), **common),
        "lot_attention": lambda: ev.lot_attention(st, as_of, **common),
        "accepted_quote_no_order": lambda: ev.accepted_quote_no_order(st, as_of, **common),
        "invoice_quantity_mismatch": lambda: ev.invoice_quantity_mismatch(
            st, as_of, settled=SETTLED, orders=_po_source(st), **common),
        "vendor_no_reply": no_reply,
        "vendor_terms_unapplied": lambda: ev.vendor_mail(
            st, as_of, call, family="F27", cache=(cache or {}).get("vendor_terms_unapplied"),
            **mail, **common),
        "negative_margin": lambda: ev.negative_margin(
            st, as_of, call, pmap=None if pmap is None else (
                lambda task, items: order_margins(items) if task is ev.read_margin
                else (margin_map if margins is not None else pmap)(task, items)),
            roots=roots(), read=one_margin, **common),
        "margin_erosion": erosion}


def currency_word(raw: Any) -> str | None:
    """A currency cell as its ISO 4217 code by the ontology's one table
    (``ontology.units.currency_code``): ``CAD``, the Portal's ``CAD$``/``USD$``, ``C$``,
    ``US$``; None for anything no rule reads or a code ISO 4217 does not list (``CDN``,
    ``$``): such an amount is never stated nor ranked."""
    from ontology.units import ISO_4217, currency_code

    code, _ = currency_code(str(raw)) if raw not in (None, "") else (None, None)
    return code if code in ISO_4217 else None


def _suspects(st: _Store, object_type: str, objects: Sequence[str] = ()) -> dict[str, Any]:
    """``suspect_money.suspects`` of ``objects``, judged once over every record of the type
    that states a total (its medians are populations: an incremental pass compares them
    with the last fold's, ``_Store.population``). Once the run's time is spent no new check
    is issued: the caller says the total could not be checked."""
    if st.interrupt is not None and st.interrupt():
        raise sqlite3.OperationalError("interrupted: the run's time is spent")
    every = st.population(f"suspects:{object_type}", lambda: suspect_money.suspects(
        st, object_type, sorted(st.values([object_type], AMOUNT_OF[object_type][:1]))))
    return {oid: every[oid] for oid in objects if oid in every}


def _with_amounts(st: _Store, results: Mapping[str, Mapping[str, Any]]) -> None:
    """Each gap about a record ``AMOUNT_OF`` names that states no amount gets the record's
    own cited total with its cited currency: both citations join the gap as extra (they
    never decide who sees the gap) and the amount names them, so a reader shown the gap
    sees the figure only when it may open both (``_shown``). A total missing, disputed
    across sources, not a decimal, or without one 3-letter currency gives none. A total
    implausible against its lines or history (``operating.suspect_money``), or one that
    could not be checked, carries ``suspect`` (why): the briefing never ranks by it."""
    from . import proactive_events as ev

    for object_type in AMOUNT_OF:   # every run reads the same populations, gaps or none
        try:
            _suspects(st, object_type)
        except Exception:  # noqa: BLE001 - checked again below, per gap
            pass
    wanted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results.values():
        for gap in result["gaps"]:
            event = gap["follow_up"].get("event") or {}
            if gap["object_type"] in AMOUNT_OF and "amount" not in event:
                wanted[gap["object_type"]].append(gap)
    for object_type, gaps in wanted.items():
        amount, currency = AMOUNT_OF[object_type]
        try:
            checked = _suspects(st, object_type, sorted({g["object_id"] for g in gaps}))
        except Exception:  # noqa: BLE001 - an unchecked total is never stated as fact
            checked = None
        try:
            found = st.values([object_type], (amount, currency),
                              objects=sorted({g["object_id"] for g in gaps}))
        except sqlite3.OperationalError:
            continue   # interrupted at the deadline: no amount stated, none ranked
        derived: dict[str, Any] = {}
        if object_type == order_value.TYPE:
            try:   # a header stating no value: what its invoices or lines derive
                derived = {oid: got for oid, got in order_value.for_store(
                    st, sorted({g["object_id"] for g in gaps})).items() if got.derived}
            except sqlite3.OperationalError:
                derived = {}
        for gap in gaps:
            got = derived.get(gap["object_id"])
            if got is not None:
                rows = list(got.rows)
                marks = _markings(*(r.get("markings") for r in rows))
                gap["extra"] = list(dict.fromkeys([*gap["extra"], *_cites(rows)]))
                gap["follow_up"]["event"] = {
                    **(gap["follow_up"].get("event") or {}),
                    "amount": {"value": str(got.amount), "currency": got.currency,
                               "basis": DERIVED_BASIS[got.basis],
                               "handle": rows[0]["l1_handle"],
                               "currency_handle": rows[0]["l1_handle"], "markings": marks}}
                continue
            props = (found.get(gap["object_id"]) or {}).get("props") or {}
            total, code = ev._one(props.get(amount)), ev._one(props.get(currency))
            value = ev._decimal(total["value"]) if total else None
            word = currency_word(code["value"]) if code else None
            if value is None or value <= 0 or word is None:
                # no figure, none entered yet (a total of 0: an order not priced), or a
                # currency no rule reads: nothing stated or ranked
                continue
            extra = _cites([total, code])
            stated: dict[str, Any] = {
                "value": str(value), "currency": word, "basis": f"{amount}",
                "handle": total["l1_handle"], "currency_handle": code["l1_handle"],
                # the figure's own markings (a value may be marked apart from its record)
                "markings": _markings(total.get("markings"), code.get("markings"))}
            found_of = None if checked is None else checked.get(gap["object_id"]) or {}
            if found_of is None:
                stated["suspect"] = "its plausibility could not be checked"
            elif found_of.get("suspect"):
                # implausible against its history: shown as suspect, never ranked; the
                # reason's ratio band only for a reader who may see every peer it rests on.
                # Checked first: an outlier is never shown as a convention
                finding = found_of["suspect"]
                stated.update(suspect=finding["reason"], suspect_plain=finding["plain"],
                              suspect_scope=finding["scope"])
            if found_of and found_of.get("lines"):
                # checked against its own lines and not simply theirs (the Portal's unit-
                # price convention, a disagreement, or no priced line): never ranked; what
                # the lines say is stated only to a reader who may open every one of them
                lines = found_of["lines"]
                rows = lines.get("rows")
                line_cites = _cites(rows) if rows else []
                extra = [*extra, *line_cites]
                stated["lines"] = {
                    "verdict": lines["verdict"],
                    **({"lines_sum": lines["lines_sum"]} if "lines_sum" in lines else {}),
                    "handles": None if rows is None else [c[0] for c in line_cites],
                    "markings": lines["markings"]}
            gap["extra"] = list(dict.fromkeys([*gap["extra"], *extra]))
            gap["follow_up"]["event"] = {**(gap["follow_up"].get("event") or {}),
                                         "amount": stated}




def _read_cache(inbox: Path) -> dict[str, dict[str, Any]]:
    """The incremental detectors' last readings (``event_cache``): detector -> root ->
    reading. None stored (or no inbox yet): every root is read."""
    if not inbox.is_file():
        return {}
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    with closing(_open_inbox(inbox)) as conn:
        for row in conn.execute("SELECT detector, root, reading FROM event_cache"):
            try:
                out[row["detector"]][row["root"]] = json.loads(row["reading"])
            except ValueError:
                continue   # an unreadable reading is read again
    return dict(out)


def inbox_path(store_path: Any) -> Path:
    """The inbox file of a projection store: beside it, named after it."""
    store = Path(store_path)
    return store.with_name(store.name + ".gap-inbox.sqlite")


def reader_key(actor: Any) -> str:
    """The reader an acknowledgement belongs to: tenant and sorted principals."""
    return f"{actor.tenant}:" + ",".join(sorted(actor.principals))


def _open_inbox(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    _schemas.apply(conn, _PROACTIVE_INBOX)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)",
                     (INBOX_SCHEMA,))
    elif row[0] != INBOX_SCHEMA:
        conn.close()
        raise ValueError("gap_inbox_schema_mismatch")
    return conn


def _people(store_path: Path) -> Any:
    """The people exclusion (environment, else the passes' env file:
    ``integrations.people_exclusion.environ_with_exclusion``) with the tenant's mail
    directory and domains
    from the mail capture of the state directory ``store_path`` sits in (the projection
    file, or a generation's store under it); unreadable: an unavailable guard."""
    from integrations.people_exclusion import environ_with_exclusion
    from .chunking.permissions import load_exclusion

    parent = store_path.parent
    state = parent.parent.parent.parent if parent.parent.name == "generations" \
        else parent.parent
    try:
        # the passes' env file when this process was started without the exclusion
        return load_exclusion(state, environ_with_exclusion())
    except Exception:  # noqa: BLE001 - no directory: the rule is blocked, never guessed
        return None


class TimeBudgetSpent(Exception):
    """A tool call refused because the run's time budget is spent."""


TOOL_BUDGET_MAX_MS = 600000   # plx.investigate's schema maximum for time_budget_ms


# A detector starts beside the ones running only while the process's cgroup (the unit's,
# with the pool's workers in it) holds, counting ``DETECTOR_RESERVE_BYTES`` for each
# detector already running (one just started has not grown yet) and each one cut off
# whose thread still runs, less than this share of its memory limit (the lower of
# memory.high and memory.max); its page cache is not counted (the kernel reclaims it at
# the limit). Over it, a lane waits for a running detector to end, so the rules' reads
# stop adding up past the limit (CC-P-04: plx-proactive OOM-killed at 15.0 GB of 16,
# plx-proactive-whole at 12.6 of 14, plx-proactive-inc at 5.7 of 6, 11-12/13; one detector
# at a time grows by up to ~1.6 GB on the live store, 11-13). A detector alone always
# runs; no limit (or none readable): no wait. ``PLX_PROACTIVE_MEMORY_SHARE`` overrides
# the share (0 < share <= 1).
# The limit is also never more than what the host can still give: the cgroup's use plus
# the host's MemAvailable (``_host_room``). The kills of 11-12/13 were the host's own OOM
# killer ("Out of memory", not a memory-cgroup kill: no unit was at its MemoryMax; the
# passes and the lanes' jobs together ran the 61 GB host out), so a gate that watched only
# the unit's own 15 GB limit started detectors side by side with 4 GB left on the host.
MEMORY_GATE_SHARE = 0.6
HOST_MEMINFO = "/proc/meminfo"
DETECTOR_RESERVE_BYTES = 1_500_000_000
MEMORY_GATE_POLL_SECONDS = 2.0
MEMORY_SHARE_ENV = "PLX_PROACTIVE_MEMORY_SHARE"


def _own_cgroup(proc_cgroup: str = "/proc/self/cgroup",
                root: str = "/sys/fs/cgroup") -> str | None:
    """The directory of this process's own cgroup (v2), or None."""
    try:
        with open(proc_cgroup, encoding="ascii") as handle:
            path = next((line.strip().split(":", 2)[2] for line in handle
                         if line.startswith("0::")), None)
    except (OSError, IndexError):
        return None
    return None if path is None else root.rstrip("/") + path


def _cgroup_value(directory: str | None, name: str) -> int | None:
    """``name`` (a one-number cgroup file) in bytes; None: unlimited (``max``) or not
    readable."""
    if directory is None:
        return None
    try:
        with open(f"{directory}/{name}", encoding="ascii") as handle:
            raw = handle.read().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def _cgroup_used(directory: str | None) -> int | None:
    """memory.current less the page cache the kernel can reclaim (memory.stat ``file``
    less ``shmem``); None when either cannot be read."""
    current = _cgroup_value(directory, "memory.current")
    if current is None:
        return None
    try:
        with open(f"{directory}/memory.stat", encoding="ascii") as handle:
            stat = dict(line.split() for line in handle if line.count(" ") == 1)
        cache = int(stat["file"]) - int(stat.get("shmem", 0))
    except (OSError, ValueError, KeyError):
        return None
    return max(0, current - max(0, cache))


def _cgroup_limit(directory: str | None) -> int | None:
    found = [v for v in (_cgroup_value(directory, "memory.high"),
                         _cgroup_value(directory, "memory.max")) if v is not None]
    return min(found) if found else None


def _host_available(meminfo: str = HOST_MEMINFO) -> int | None:
    """The host's MemAvailable in bytes; None when it cannot be read."""
    try:
        with open(meminfo, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    return int(parts[1]) * (1024 if parts[2:3] == ["kB"] else 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _host_room(directory: str | None, meminfo: str = HOST_MEMINFO) -> int | None:
    """The memory this process's cgroup may grow to: the lower of its own limit and its
    use now plus what the host still has available (the host's OOM killer, not the
    unit's limit, ends a pass on a loaded host). A host figure that cannot be read leaves
    the cgroup's limit; a use that cannot be read with a readable host figure: the host
    figure alone (never no limit)."""
    limit = _cgroup_limit(directory)
    available = _host_available(meminfo)
    if available is None:
        return limit
    room = (_cgroup_used(directory) or 0) + available
    return room if limit is None else min(limit, room)


class _MemoryGate:
    """Admission of detectors by memory (``MEMORY_GATE_SHARE``): ``enter`` waits while
    another detector runs and ``used()`` plus ``reserve`` for each running or cut-off
    (``abandon``) detector is at or over ``share`` of ``limit()``; ``leave`` when it ends.
    ``held`` counts the detectors that waited, ``most`` the most that ran at once."""

    def __init__(self, used: Callable[[], int | None], limit: Callable[[], int | None],
                 share: float = MEMORY_GATE_SHARE, reserve: int = DETECTOR_RESERVE_BYTES,
                 poll: float = MEMORY_GATE_POLL_SECONDS) -> None:
        if not 0 < share <= 1 or reserve < 0:
            raise ValueError("memory_gate_invalid")
        self.used, self.limit, self.share, self.reserve = used, limit, share, reserve
        self.poll = poll
        self.running = self.most = self.held = self.orphans = 0
        self._cond = threading.Condition()

    @classmethod
    def for_process(cls, environ: Mapping[str, str] | None = None) -> "_MemoryGate":
        env = os.environ if environ is None else environ
        share = MEMORY_GATE_SHARE
        raw = (env.get(MEMORY_SHARE_ENV) or "").strip()
        if raw:
            try:
                share = float(raw)
            except ValueError:
                share = MEMORY_GATE_SHARE   # unreadable: the default, never no gate
            if not 0 < share <= 1:
                share = MEMORY_GATE_SHARE
        directory = _own_cgroup()
        return cls(lambda: _cgroup_used(directory), lambda: _host_room(directory, HOST_MEMINFO),
                   share)

    def over(self) -> bool:
        limit = self.limit()
        if limit is None:
            return False
        used = self.used()
        if used is None:
            return True   # a limit whose use cannot be read: over (one at a time)
        return used + (self.running + self.orphans) * self.reserve >= self.share * limit

    def enter(self, stop: Callable[[], bool] = lambda: False) -> None:
        with self._cond:
            waited = False
            while self.running > 0 and not stop() and self.over():
                waited = True
                self._cond.wait(self.poll)
            self.held += waited
            self.running += 1
            self.most = max(self.most, self.running)

    def leave(self) -> None:
        with self._cond:
            self.running -= 1
            self._cond.notify_all()

    def abandon(self, ended: threading.Event) -> None:
        """A detector cut off at its window whose thread still runs: its memory is counted
        (a reserve) until ``ended`` is set, without holding up the others' turns."""
        with self._cond:
            self.orphans += 1

        def wait() -> None:
            ended.wait()
            with self._cond:
                self.orphans -= 1
                self._cond.notify_all()
        threading.Thread(target=wait, name="proactive-gate-orphan", daemon=True).start()


class Budget:
    """A run's time budget (``seconds`` from now on ``monotonic``): ``guard`` wraps a tool
    caller so that once it is spent every call raises ``TimeBudgetSpent`` and is noted
    (``refused``), so a detector that swallows a failing call is still not complete.

    Each detector runs in its own window (``open``: its own seconds, ``DETECTOR_SECONDS``,
    never past the run's deadline), kept per thread: detectors running side by side
    (``run(threads=)``) each stop at their own window, and a refusal counts against the
    detector whose thread it happened on (``refused_here``)."""

    def __init__(self, seconds: float, monotonic: Callable[[], float] = time.monotonic,
                 *, reserve: float = 0.0) -> None:
        self.monotonic = monotonic
        # the detectors end ``reserve`` seconds early (at most a quarter of the budget): the
        # amounts and their plausibility check run in that reserve, inside the bound
        self.end = monotonic() + seconds
        self.deadline = self.end - min(max(reserve, 0.0), seconds / 4)
        self.refused = 0
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def until(self) -> float | None:
        """This thread's detector window's end, when it has one."""
        return getattr(self._local, "until", None)

    def spent(self) -> bool:
        return self.monotonic() >= self.deadline

    def open(self, seconds: float | None) -> None:
        """This thread's next detector runs for at most ``seconds`` (None: to the run's
        deadline), so no one slow detector starves the rest."""
        self._local.until = None if seconds is None else self.monotonic() + max(0.0, seconds)

    def share(self, weight: float, of: float) -> None:
        """Give the next detector ``weight``/``of`` of what is left (time it leaves unused
        carries to the ones after it)."""
        now = self.monotonic()
        self._local.until = now + max(0.0, self.deadline - now) * weight / max(weight, of)

    def unshare(self) -> None:
        self._local.until = None

    def note_refused(self) -> None:
        with self._lock:
            self.refused += 1
        self._local.refused = getattr(self._local, "refused", 0) + 1

    def refused_here(self) -> int:
        """Refusals noted on this thread (its detector's calls and maps)."""
        return getattr(self._local, "refused", 0)

    def release(self) -> None:
        """The detectors are done: what is left up to the end is the reserve's."""
        self._local.until, self.deadline = None, self.end

    def guard(self, call: Callable | None) -> Callable | None:
        if call is None:
            return None

        def guarded(tool: str, arguments: dict) -> Any:
            left = self.left()
            if left <= 0:
                self.note_refused()
                raise TimeBudgetSpent(tool)
            if tool == "plx.investigate":
                # the investigation stops itself at the deadline (between its steps), never
                # only the next call: the budget bounds the call in flight too
                # (the tool refuses more than TOOL_BUDGET_MAX_MS: a long pass asks for that much)
                arguments = {**arguments,
                             "time_budget_ms": max(1, min(int(left * 1000), TOOL_BUDGET_MAX_MS))}
            found = call(tool, arguments)
            reasons = ((found.get("answerability") or {}).get("reasons") or ()
                       if isinstance(found, Mapping) else ())
            if tool == "plx.investigate" and "time_budget_exhausted" in reasons:
                self.note_refused()   # a part-read investigation is no reading at all
                raise TimeBudgetSpent(tool)
            return found
        return guarded

    def pooled(self, pool: Any) -> Callable[[Callable, Sequence[Any]], list[Any]]:
        """``pool.map`` within this budget: a map that leaves items unread (the time left
        did not reach them) is noted ``refused``, so its detector is not complete."""
        from . import proactive_pool

        def run_map(task: Callable, items: Sequence[Any]) -> list[Any]:
            out = pool.map(task, items, left=self.left)
            if any(r is proactive_pool.NOT_READ for r in out):
                self.note_refused()
            return out
        return run_map

    def left(self) -> float:
        """Seconds left for this thread's detector: to its window's end, else the
        deadline."""
        until = self.until
        end = self.deadline if until is None else min(self.deadline, until)
        return end - self.monotonic()

    def interrupt(self) -> int:
        """SQLite's progress handler: nonzero aborts the statement running (a set-based
        rule's query stops at the deadline, not after it)."""
        return int(self.left() <= 0)

    def pinned(self) -> Callable[[], int]:
        """``interrupt`` at this thread's window, for reads it hands to other threads (a
        link scan's or a value read's parts): they stop where the detector asking stops."""
        until = self.until
        end = self.deadline if until is None else min(self.deadline, until)
        return lambda: int(self.monotonic() >= end)


class _Task:
    """One call handed to a ``_Runner``: ``wait`` for it, then read ``value``."""

    def __init__(self, fn: Callable[..., Any], args: tuple) -> None:
        self.fn, self.args = fn, args
        self.value: Any = None
        self.event = threading.Event()

    def wait(self, timeout: float | None) -> bool:
        return self.event.wait(timeout)


class _Runner:
    """One daemon thread running the calls handed to it in turn (a lane's detectors, so
    they share its store connection); one given up on (``stop``) is never joined: the
    process ends without waiting for it."""

    def __init__(self) -> None:
        import queue

        self._tasks: Any = queue.SimpleQueue()
        threading.Thread(target=self._loop, name="proactive-detector", daemon=True).start()

    def _loop(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            try:
                task.value = task.fn(*task.args)
            finally:
                task.event.set()

    def submit(self, fn: Callable[..., Any], *args: Any) -> _Task:
        task = _Task(fn, args)
        self._tasks.put(task)
        return task

    def stop(self) -> None:
        self._tasks.put(None)


class _HeadStart:
    """``pool.map(task, items)`` started before its rule's turn, on a thread that only
    waits on the pool: the workers read while the store-only rules run on the main thread
    (the pool idles otherwise). Until the rule's turn the map reads to the detectors'
    deadline; from it (``collect``) to the rule's share of the time, as ``Budget.pooled``;
    ``stop`` ends it unread (the rule never ran)."""

    def __init__(self, pool: Any, task: Callable, items: Sequence[Any],
                 budget: Budget | None) -> None:
        self.task, self.items, self.budget = task, list(items), budget
        self.turn = self.stopped = False
        self.until: float | None = None   # the collecting detector's window end
        self.out: list[Any] | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run_map, args=(pool,), daemon=True,
                                       name="proactive-head-start")
        self.thread.start()

    def left(self) -> float:
        if self.stopped:
            return 0.0
        if self.budget is None:
            return float("inf")
        if self.turn:
            end = self.budget.deadline if self.until is None \
                else min(self.budget.deadline, self.until)
            return end - self.budget.monotonic()
        return self.budget.deadline - self.budget.monotonic()

    def _run_map(self, pool: Any) -> None:
        try:
            self.out = pool.map(self.task, self.items, left=self.left)
        except BaseException as error:  # noqa: BLE001 - raised in the rule that collects
            self.error = error

    def collect(self) -> list[Any]:
        from . import proactive_pool

        if self.budget is not None:
            self.until = self.budget.until   # read on the collecting detector's thread
        self.turn = True
        self.thread.join()
        if self.error is not None:
            raise self.error
        out = list(self.out or [])
        if self.budget is not None and any(r is proactive_pool.NOT_READ for r in out):
            self.budget.note_refused()   # as ``Budget.pooled``: its rule is not complete
        return out

    def stop(self) -> None:
        self.stopped = True
        self.thread.join()


def _margin_head_start(st: _Store, as_of: date, pool: Any, budget: Budget | None,
                       order: Sequence[str]) -> _HeadStart | None:
    """The negative-margin rule's order margins read on the pool from the run's start
    (``_HeadStart``): each read is one order's margin investigation (minutes for a Sage
    order on the live store), the slowest rule of a run. None without a pool, when the rule
    does not run, the time is spent or no order was invoiced recently."""
    if pool is None or "negative_margin" not in order or (budget is not None
                                                           and budget.spent()):
        return None
    from . import proactive_events as ev

    try:
        roots = st.memo("margin_roots", lambda: ev._recent_invoiced_orders(st, as_of))
    except sqlite3.OperationalError:
        return None   # interrupted at the deadline: the rule reads them at its turn
    return _HeadStart(pool, ev.read_margin, roots, budget) if roots else None


def detector_threads(environ: Mapping[str, str] | None = None) -> int:
    """Threads the store-only detectors run on side by side: every core but two, at most
    ``MAX_DETECTOR_THREADS`` (``PLX_PROACTIVE_DETECTOR_THREADS`` overrides; 1: one after
    another)."""
    raw = (os.environ if environ is None else environ).get(THREADS_ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return 1
    return max(1, min(MAX_DETECTOR_THREADS, (os.cpu_count() or 1) - 2))


def _lanes(order: Sequence[str], threads: int) -> list[list[str]]:
    """The store-only detectors of ``order`` as lanes run side by side (each lane's
    detectors one after another: ``CHAINED`` after the rule it reads), slowest known first;
    none with one thread (every detector then runs in ``order`` on the run's thread)."""
    if threads <= 1:
        return []
    side = [d for d in order if d in SET_BASED and d != "order_without_customer_po"]
    lanes: list[list[str]] = []
    for name in side:
        after = CHAINED.get(name)
        lane = next((ln for ln in lanes if after in ln), None)
        if lane is not None:
            lane.append(name)
        else:
            lanes.append([name])
    first = {"conflicting_quantity": 0, LATE_RULE: 1}
    return sorted(lanes, key=lambda ln: first.get(ln[0], 2))


def _share(name: str, rest: Sequence[str], windows: Mapping[str, float],
           left: float, cadenced: bool = False) -> float:
    """``name``'s window at its turn: its own (``windows``), but at most its part of the
    ``left`` seconds of the run by weight (its window) against the ``rest`` its thread
    runs after it, so none of them starts with nothing (``DETECTOR_SECONDS``)."""
    own = windows.get(name, DETECTOR_SECONDS_DEFAULT)
    if math.isinf(left):
        return own
    if cadenced:
        # a rule due on its cadence runs seldom because it needs its whole window: it is
        # given that (never more, never past the run's deadline), not a part of the pass
        return max(0.0, min(own, left))
    total = own + sum(windows.get(other, DETECTOR_SECONDS_DEFAULT) for other in rest)
    return max(0.0, min(own, left * own / total if total > 0 else left))


class _Watchdog:
    """While a run-thread rule runs: from ``seconds`` on, the statements its thread's store
    connection runs are cancelled (each second, until it ends), as SQLite's progress
    handler stops them at the window (``RUN_THREAD_GRACE_SECONDS``)."""

    def __init__(self, st: "_Store", seconds: float) -> None:
        self.st, self.seconds = st, seconds
        self.thread = threading.current_thread()
        self.lock = threading.Lock()
        self.done = False
        self.stop = threading.Event()
        self.watcher: threading.Thread | None = None

    def _watch(self) -> None:
        if self.stop.wait(max(0.0, self.seconds)):
            return
        while True:
            with self.lock:   # never a cancel once the rule ended (the next one's read)
                if self.done:
                    return
                self.st.interrupt_thread(self.thread)
            if self.stop.wait(1.0):
                return

    def __enter__(self) -> "_Watchdog":
        if not math.isinf(self.seconds):
            self.watcher = threading.Thread(target=self._watch, daemon=True,
                                            name="proactive-watchdog")
            self.watcher.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        with self.lock:
            self.done = True
        self.stop.set()


def _not_due(inbox: Path, names: Sequence[str], hours: Mapping[str, float],
             moment: datetime) -> list[str]:
    """Of ``names``, the rules whose last run started less than their ``hours`` before
    ``moment`` by the engine code deployed (``_CADENCE_KEY``); read only. A rule never
    run, run by other code, or whose record cannot be read is due (it runs, never
    skipped unseen)."""
    if not names or not inbox.is_file():
        return []
    try:
        with closing(sqlite3.connect(f"{inbox.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            held = dict(conn.execute(
                "SELECT key, value FROM meta WHERE key IN (SELECT value FROM json_each(?))",
                (json.dumps([_CADENCE_KEY + name for name in names]),)))
    except sqlite3.Error:
        return []
    code = engine_code()
    out = []
    for name in names:
        try:
            record = json.loads(held.get(_CADENCE_KEY + name) or "null")
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("code") != code:
            continue
        at = _moment(record.get("at"))
        if at is None or moment.tzinfo is None or at > moment:
            continue
        if moment - at < timedelta(hours=float(hours[name])):
            out.append(name)
    return out


def run(store_path: Any, inbox: Any, as_of: date | None = None, *,
        locked: bool = False, **options: Any) -> dict[str, Any]:
    """Run the detectors and fold their gaps into the inbox (``_run``), holding the
    inbox's pass lock (``pass_lock``; ``locked``: the caller holds it): two runs never
    read and fold over each other, so a fold is never of older data than the last."""
    if locked:
        return _run(store_path, inbox, as_of, **options)
    with pass_lock(inbox) as _held:
        return _run(store_path, inbox, as_of, **options)


HELPER_JOIN_SECONDS = 30.0   # a closing store waits this long for each helper read to stop
LOCK_POLL_SECONDS = 2.0   # how often a pass waiting for the inbox's lock tries it


@contextlib.contextmanager
def pass_lock(inbox: Any, *, wait: bool | float = True,
              monotonic: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> Iterator[bool]:
    """The inbox's pass lock (``<inbox>.lock``); ``wait`` False:
    yields False at once when another pass holds it; a number of seconds: waits up to
    that long for it (a pass that ends meanwhile hands it over), then yields False. The
    lock is the kernel's, so a pass that died holds nothing."""
    import fcntl

    path = Path(inbox).with_name(Path(inbox).name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if wait is True:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            until = monotonic() + max(0.0, float(wait or 0))
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    left = until - monotonic()
                    if left <= 0:
                        yield False
                        return
                    sleep(min(LOCK_POLL_SECONDS, left))
        yield True
    finally:
        os.close(fd)


PROACTIVE_STATEMENT_SECONDS = 600


def statement_timeout_ms() -> int:
    """The longest one detector statement may run on Postgres: ``PLX_PROACTIVE_STATEMENT_SECONDS``
    (default 600). The shared connection default (120 s) cut vendor_no_reply,
    teams_unanswered_ask, price_consistency and others at every busy pass, so those rules
    produced no alerts at all (CC-P-03); a bad or non-positive value falls back."""
    try:
        seconds = float(os.environ.get("PLX_PROACTIVE_STATEMENT_SECONDS", ""))
    except ValueError:
        seconds = 0.0
    return int((seconds if seconds > 0 else PROACTIVE_STATEMENT_SECONDS) * 1000)


def _failure_cause(error: BaseException) -> list[str]:
    """What a detector's failure was, as an enum beside its class: a Postgres statement
    cut at the connection's ``statement_timeout`` (``sqlite_compat`` raises it as
    ``OperationalError("interrupted")``; Postgres has no progress handler, so it is never
    the run's own window), or the Postgres error class ``sqlite_compat`` names; else none.
    Never the message text."""
    if not isinstance(error, sqlite3.OperationalError):
        return []
    text = str(error)
    if text == "interrupted":
        return ["statement_timeout"]
    if text.startswith("postgres: "):
        name = text[len("postgres: "):].split(":", 1)[0].strip()
        return [f"postgres_{name}"] if name.isidentifier() else []
    return []


def _run(store_path: Any, inbox: Any, as_of: date | None = None, *,
         coverage: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
        call: Callable | None = None, card_bound: int = CARD_BOUND,
        qc_link_types: Sequence[str] | None = None, budget: Budget | None = None,
        on_hand_sources: Sequence[tuple[str, str, str | None]] | None = None,
        ops_sources: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        only: Sequence[str] | None = None, vendor_bound: int | None = None,
        reader_may_read: Callable[[str], bool] | None = None,
        pool: Any = None, people: Any = None, threads: int | None = 1,
        detector_seconds: Mapping[str, float] | None = None,
        scope: "Scope | None" = None, sheet_readings: Any = None,
        cadence: Mapping[str, float] | None = None,
        memory: "_MemoryGate | None" = None) -> dict[str, Any]:
    """Run every detector over the store and fold the gaps into the inbox; the summary
    (counts and enums only). A detector that fails is ``failed`` and leaves its gaps as
    they were; only a ``complete`` detector resolves the gaps it no longer finds. The
    run's day (``as_of``, what late and days late are counted to) defaults to the
    business day (ET) of ``clock()`` (``po_status.business_today``), never its UTC day.
    ``coverage`` may be a callable, scanned at its detector's turn (its failure fails that
    detector alone). With a ``budget``, a detector not started before it is spent, or one
    whose tool call it refused, is ``truncated`` (``time_budget``). ``only``: run these
    detectors alone (the others' gaps and records are left as they are: the separate
    vendor-mail pass, ``operating.vendor_watch``). ``vendor_bound``: how many purchase
    orders the incremental vendor checks may investigate this run (0: reuse their
    readings only, the build's run when the vendor pass is scheduled apart).
    ``reader_may_read(audience_key)``: the grant of the principal ``call`` reads as; a
    rule whose verdict needs a source it may not read is ``blocked``
    (``reader_lacks_lateness_access``), never judged blind (None: not checked).
    ``pool``: an ``operating.proactive_pool.ToolPool`` reading as the same principal as
    ``call``: the tool-call rules ask every record of the store on its workers (no
    per-run record bound; the budget still bounds them), else one call at a time.
    ``people``: the people exclusion the vendor-mail rule clears senders and mailboxes
    with (``operating.chunking.permissions.Exclusion``); None: read from the environment
    and the mail capture of the state directory the store sits in (``_people``).
    A full run reads every link type its rules read in one scan first (``_Store.links``);
    with a pool, the margins of the recently invoiced orders are read on it from the start
    (``_margin_head_start``), while the store-only rules run. ``threads``: the store-only
    rules run side by side on this many threads (``_lanes``; 1: one after another; None:
    ``detector_threads()``), the tool-call rules on this thread. Every detector runs in its
    own window (``DETECTOR_SECONDS``, ``detector_seconds`` overriding some), within the
    run's deadline when there is a ``budget``: a store-only one still running
    ``HARD_GRACE_SECONDS`` past it is cut off (``truncated``, named in the summary's
    ``cut_off``) and its lane goes on; the summary's ``seconds`` holds each one's time.
    ``scope``: an incremental pass (``Scope``, ``operating.proactive_incremental``): its
    rules read only the scope's records and keep only the gaps about their own subjects;
    only those subjects' gaps are folded and resolved (``_fold``), nothing else of the
    inbox (the full run's records, counts and generation) changes. With a ``budget`` each
    detector is given its share of what is left at its turn (``_share``). ``cadence``:
    hours per rule (default ``DETECTOR_CADENCE_HOURS``; ``{}``: none) a rule waits after
    its last run started before it runs again (``_not_due``); one not due is left as it
    was and named in the summary's ``not_due``."""
    moment = clock()
    as_of = po_status.business_today(moment) if as_of is None else as_of
    if type(as_of) is not date:   # a datetime or text is no day: refused, never coerced
        raise TypeError("as_of_must_be_a_date")
    run_at = moment.isoformat()
    call = call if budget is None else budget.guard(call)
    pmap: Callable[[Callable, Sequence[Any]], list[Any]] | None = None
    many: Callable[[str, Sequence[Any]], list[Any]] | None = None
    if pool is not None and call is not None:
        from . import proactive_pool

        pmap = pool.map if budget is None else budget.pooled(pool)

        def many(tool: str, arguments: Sequence[Any]) -> list[Any]:
            return pmap(proactive_pool.tool_call, [(tool, a) for a in arguments])
    cache = _read_cache(Path(inbox))
    if scope is not None:
        only = [d for d in DETECTORS if d in scope.subjects]
    st = _Store(Path(store_path), scope=None if scope is None else scope.read,
                audience_keys=None if scope is None else scope.audience_keys)
    # every detector runs in its own window, with or without a run budget: an unbounded
    # run is one whose deadline never comes (its tool calls are not guarded)
    window = budget if budget is not None else Budget(math.inf)
    gate = memory if memory is not None else _MemoryGate.for_process()
    st.watch(window.interrupt, window.pinned, window.left)
    st.cursor_path = Path(str(inbox) + ".commitments-recent.json")
    try:
        results: dict[str, dict[str, Any]] = {}
        seconds: dict[str, float] = {}
        unknown = set(only or ()) - set(DETECTORS)
        if unknown:
            raise ValueError("detector_unknown")
        order = list(DETECTORS) if only is None else [d for d in DETECTORS if d in only]
        hours = DETECTOR_CADENCE_HOURS if cadence is None else cadence
        not_due = [] if scope is not None else _not_due(
            Path(inbox), [d for d in order if d in hours], hours, moment)
        order = [d for d in order if d not in not_due]
        started_at: dict[str, str] = {}   # the cadenced rules this run started: when
        if (only is None or scope is not None) and not (budget is not None and budget.spent()):
            # every link type the rules read, in one scan of the link table (``_Store``)
            started = time.monotonic()
            try:
                # with the run's own QC links and on-hand links, when it is handed them
                st.prefetch_links([*_run_link_types(), *(qc_link_types or ()),
                                   *(link for _type, _prop, link in on_hand_sources or ()
                                     if link)])
            except sqlite3.OperationalError:
                pass   # stopped at the deadline: each rule then stops at its own read
            seconds["link_scan"] = round(time.monotonic() - started, 1)
        head = _margin_head_start(st, as_of, pool if call is not None else None, budget,
                                  order)
        steps: dict[str, Callable[[], dict[str, Any]]] = {
            "late_or_unconfirmed_po": lambda: _late_or_unconfirmed_po(st, as_of),
            "late_customer_order": lambda: _late_customer_order(st, as_of),
            "po_receipt_quantity_mismatch": lambda: _po_receipt_quantity_mismatch(st, as_of),
            "receipt_without_qc": lambda: _receipt_without_qc(
                st, as_of, declared_qc_links() if qc_link_types is None else qc_link_types),
            "order_without_customer_po": lambda: _order_without_customer_po(
                st, coverage() if callable(coverage) else coverage, as_of),
            "expired_quote": lambda: _expired_quote(st, as_of),
            **_business_steps(st, as_of, on_hand_sources,
                              people=lambda: people if people is not None
                              else _people(Path(store_path)), moment=moment,
                              sheets=_sheets(store_path, sheet_readings)),
            **_ops_steps(st, as_of, lambda: people if people is not None
                         else st.memo("ops:people", lambda: _people(Path(store_path))),
                         ops_sources),
            "conflicting_quantity": lambda: _conflicting_quantity(st, as_of),
            "late_order": lambda: _late_order(st, as_of),
            "orders_at_risk": lambda: (
                _orders_at_risk(st, as_of, call, card_bound, first=_late_found(results),
                                many=many)
                if call is None or _reader_judges_lateness(st, reader_may_read) else
                # the run principal cannot read a source a lateness verdict rests on (a
                # PO's BC twin): its verdicts would be undetermined, never late, for every
                # reader, so it does not judge (its gaps stay as they were); never a clean
                # "none at risk"
                _result([], status="blocked", reasons=["reader_lacks_lateness_access"])),
            "card_rules": lambda: _card_rules(st, as_of, call, card_bound, many),
            **_event_steps(st, as_of, call, cache, vendor_bound, pmap=pmap,
                           results=lambda: results,
                           people=lambda: people if people is not None
                           else _people(Path(store_path)), margins=head)}
        windows = {**DETECTOR_SECONDS, **(detector_seconds or {})}

        def body(name: str, seconds_: float) -> tuple[dict[str, Any], float]:
            """One detector in its own window (``seconds_``), on the thread it is handed
            to; never raising: (its result, its seconds)."""
            refused = window.refused_here()
            window.open(seconds_)
            started = time.monotonic()
            if name in hours:
                started_at[name] = clock().isoformat()
            st.judging(name)
            try:
                result = steps[name]()
            except TimeBudgetSpent:
                result = _result([], status="truncated", reasons=[TIME_BUDGET])
            except Exception as error:  # noqa: BLE001 - one detector fails alone, typed
                if window.left() <= 0 and isinstance(error, sqlite3.OperationalError):
                    # its query was interrupted at its window's end: cut short, not failed
                    result = _result([], status="truncated", reasons=[TIME_BUDGET])
                else:
                    result = _result([], status="failed",
                                     reasons=[type(error).__name__, *_failure_cause(error)])
                    # the cause by class only (never the message: it may quote values), so
                    # the next failed run says why in the journal (R8: three rules failed
                    # three full runs running with nothing but "OperationalError")
                    print(f"proactive_engine: {name} failed: "
                          f"{' '.join(result['reasons'])}", file=sys.stderr, flush=True)
            finally:
                st.judging(None)
                window.unshare()
            if window.refused_here() > refused and result["status"] != "failed":
                # a call it made was refused: what it found stands, never all it would find
                result["status"] = "truncated"
                if TIME_BUDGET not in result["reasons"]:
                    result["reasons"].append(TIME_BUDGET)
            return result, round(time.monotonic() - started, 1)

        def judge(name: str, runner: list, rest: Sequence[str] = ()) -> None:
            """A store-only ``name`` on its lane's runner thread (``runner[0]``), waited for no longer
            than its window (and the run's deadline) and ``HARD_GRACE_SECONDS``: one that
            does not end by then (a loop that never reads the store) is ``truncated``, its
            thread left to end alone (its result never read) and the lane goes on on a new
            thread, so no detector holds up the ones after it. ``rest``: the detectors
            this thread runs after it (its window is its share, ``_share``)."""
            if budget is not None and budget.spent():
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
                return
            # beside the running detectors only while the memory allows (``_MemoryGate``):
            # the wait is the run's time, not this detector's window
            gate.enter(lambda: (budget is not None and budget.spent())
                       or window.deadline - window.monotonic() <= 0)
            try:
                judged(name, runner, rest)
            finally:
                gate.leave()

        def judged(name: str, runner: list, rest: Sequence[str]) -> None:
            if budget is not None and budget.spent():
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
                return
            started = time.monotonic()
            limit = _share(name, rest, windows, window.deadline - window.monotonic(),
                           cadenced=name in hours)
            if name in ON_RUN_THREAD:
                # on the run's own thread (the caller's session and the runtime's mirror
                # may be bound to it): every tool call and pool map is refused past the
                # window (``Budget.guard``, ``Budget.pooled``; an investigation is handed
                # its time left) and every store read interrupted at it (on Postgres, by
                # cancelling the statement still running ``RUN_THREAD_GRACE_SECONDS``
                # after it)
                with _Watchdog(st, limit + RUN_THREAD_GRACE_SECONDS):
                    results[name], seconds[name] = body(name, limit)
                return
            done = runner[0].submit(body, name, limit)
            if done.wait(None if math.isinf(limit) else limit + HARD_GRACE_SECONDS):
                results[name], seconds[name] = done.value or (
                    _result([], status="failed", reasons=["detector_thread_ended"]),
                    round(time.monotonic() - started, 1))
            else:
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
                seconds[name] = round(time.monotonic() - started, 1)
                gate.abandon(done.event)   # its thread still holds what it read
                runner[0].stop()
                runner[0] = _Runner()
                abandoned.append(name)

        abandoned: list[str] = []
        runners: list[list] = []

        def runner_() -> list:
            made = [_Runner()]
            runners.append(made)
            return made

        lanes = _lanes(order, detector_threads() if threads is None else threads)
        try:
            if lanes:
                def lane(names: Sequence[str]) -> None:
                    mine = runner_()
                    for n, name in enumerate(names):
                        judge(name, mine, names[n + 1:])
                side = [d for names in lanes for d in names]
                width = min(len(lanes), detector_threads() if threads is None else threads)
                own = runner_()
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=width,
                                        thread_name_prefix="proactive-lane") as threads_:
                    running = {name: future for names in lanes
                               for future in [threads_.submit(lane, names)] for name in names}
                    mine_ = [name for name in order if name not in side]
                    for n, name in enumerate(mine_):
                        if name == "orders_at_risk" and LATE_RULE in running:
                            running[LATE_RULE].result()   # it asks the late POs first
                        judge(name, own, mine_[n + 1:])
                    for future in set(running.values()):
                        future.result()
            else:
                own = runner_()
                for n, name in enumerate(order):
                    judge(name, own, order[n + 1:])
        finally:
            if head is not None:
                head.stop()   # its rule did not run (the time ran out): no read goes on
            for made in runners:
                made[0].stop()
            if abandoned:
                # a cut-off detector's statement still running stops now, never reads on
                st.interrupt_all()
        if gate.held:
            # counts only: the journal says when memory, not the store, set the pace
            print(f"proactive_engine: {gate.held} detector(s) waited for memory "
                  f"(at most {gate.most} side by side)", file=sys.stderr, flush=True)
        if budget is not None:
            budget.release()   # the amounts run in the reserve, still inside the bound
        if scope is not None:
            # an incremental pass: a gap is this pass's only when its subject is one the
            # pass judged whole (the others' inputs were not all read)
            for name, result in results.items():
                mine = scope.subjects.get(name, frozenset())
                result["gaps"] = [g for g in result["gaps"] if g["object_id"] in mine]
        _late_list_only(st, results, as_of)
        _with_amounts(st, results)
        # one test-record filter for every detector (never a page's own): a claim about a
        # test, UAT, demo or proof record, a go-live first document or a carried-over old
        # empty draft is not a business claim
        try:
            _drop_test_records(st, results, as_of)
        except sqlite3.Error:
            pass   # the store could not be read for it: the gaps stand as found
        # one active-population filter for every record-level rule (R3): a claim about a
        # closed or historic order is not current work
        _drop_inactive_orders(st, results, as_of)
        for result in results.values():
            result.pop("counted", None)   # per-order counts: the filter's only
        populations = {name: {key: digest for key, digest in sorted(st.populations.items())
                              if name in st.population_readers[key]
                              or "*" in st.population_readers[key]}
                       for name in results}
        # the bound is spent on the detectors and the amounts only: the run's record (scope,
        # digest) is always read whole, so a run cut short still folds what it found
        st.watch(None)
        held = st.scope() if scope is None else []
        digest = st.meta("content_digest")
    finally:
        st.close()
    gaps: dict[str, dict[str, Any]] = {}
    for result in results.values():
        for gap in result["gaps"]:
            gaps.setdefault(gap["id"], gap)
    with closing(_open_inbox(Path(inbox))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            held_pops = _populations(conn)
            moved: list[str] = []
            if scope is not None:
                # a population a rule judges against moved since the rule's last fold:
                # subjects this pass did not read may move with it, so the rule is not
                # folded here but judged whole (``proactive_incremental``)
                moved = sorted(name for name in results
                               if populations.get(name) != held_pops.get(name))
                for name in moved:
                    del results[name]
                gaps = {gid: gap for gid, gap in gaps.items() if gap["detector"] in results}
            summary = _summary(results, gaps, held)
            summary["seconds"] = seconds   # each detector's run time (counts and enums only)
            if not_due:
                summary["not_due"] = sorted(not_due)   # their cadence: left as they were
            code = engine_code()
            conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [
                (_CADENCE_KEY + name, json.dumps({"at": at, "code": code}, sort_keys=True))
                for name, at in sorted(started_at.items())
                if name in results and results[name].get("status") not in
                ("not_configured", "failed")])
            if abandoned:
                summary["cut_off"] = sorted(abandoned)   # past their hard window
            if moved:
                summary["population_moved"] = moved
                summary["populations_moved"] = sorted({
                    key for name in moved for key in set(populations.get(name, {}))
                    | set(held_pops.get(name) or {})
                    if populations.get(name, {}).get(key)
                    != (held_pops.get(name) or {}).get(key)})
            for name, result in results.items():
                if result["status"] in READ_WHOLE_DATA:
                    held_pops[name] = populations.get(name, {})
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (_POPULATIONS_KEY, json.dumps(held_pops, sort_keys=True)))
            if scope is not None:
                _fold_scoped(conn, results, gaps, run_at, scope)
            else:
                _fold(conn, results, gaps, held, run_at, as_of, digest, summary,
                      record_run=only is None)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return summary


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


def _drop_test_records(st: _Store, results: dict[str, dict[str, Any]], as_of: date) -> None:
    """Remove every gap about a test record (its subject, or the order it is about) from
    every detector's result, counted ``test_record`` in its coverage."""
    tests = test_records(st, as_of)
    if not tests:
        return
    for result in results.values():
        kept, dropped = [], 0
        for gap in result.get("gaps") or ():
            follow = gap.get("follow_up") or {}
            order = (follow.get(_ORDER) or {}).get("key")
            if gap.get("object_id") in tests or order in tests \
                    or follow.get(_ATTESTS) in tests:
                dropped += 1
            else:
                kept.append(gap)
        if dropped:
            result["gaps"] = kept
            skipped = result.setdefault("not_evaluated", {})
            if isinstance(skipped, dict):
                skipped["test_record"] = skipped.get("test_record", 0) + dropped


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


COUNTED_KEYS = ("late", "at_risk", "unconfirmed", "undated")


def _uncount(result: dict[str, Any], kept: Sequence[Mapping[str, Any]],
             gone: Mapping[str, str]) -> None:
    """Take each order whose every gap the filter dropped (``gone``: order -> closed or
    historic) out of the rule's per-order counts (``counted``) into ``historic`` or
    ``closed``: the counts then say what is listed."""
    counted, counts = result.get("counted"), result.get("counts")
    if not isinstance(counted, Mapping) or not isinstance(counts, dict):
        return
    left = {o for g in kept for o in [((g.get("follow_up") or {}).get(_ORDER) or {})
                                      .get("key")] if o}
    for order, why in gone.items():
        if order in left or order not in counted:
            continue
        for key in counted[order]:
            if key in COUNTED_KEYS and counts.get(key):
                counts[key] -= 1
        counts[why] = counts.get(why, 0) + 1


def _summary(results: Mapping[str, Mapping[str, Any]], gaps: Mapping[str, Mapping[str, Any]],
             scope: Sequence[tuple[str, str, int]]) -> dict[str, Any]:
    cited = [g for g in gaps.values() if all(g["groups"])]
    return {"scanned": sum(n for _, _, n in scope),
            "scope_truncated": any(results[d]["status"] in ("truncated", "failed")
                                   for d in SET_BASED if d in results),
            "truncated": any(r["status"] in ("truncated", "failed") for r in results.values()),
            "gaps_found": len(gaps), "gaps_cited": len(cited),
            "gaps_uncited": len(gaps) - len(cited),
            "with_follow_up": sum(1 for g in gaps.values() if g["follow_up"]["who_can_answer"]),
            "gaps_by_kind": dict(sorted(Counter(g["kind"] for g in gaps.values()).items())),
            "detectors": {name: results[name]["status"] for name in DETECTORS
                          if name in results},
            "detector_reasons": {name: results[name]["reasons"] for name in DETECTORS
                                 if name in results and results[name]["reasons"]},
            "not_evaluated": {name: results[name]["not_evaluated"] for name in DETECTORS
                              if name in results and results[name]["not_evaluated"]},
            "reused": {name: results[name]["reused"] for name in INCREMENTAL
                       if "reused" in results.get(name, {})},
            "po_lateness": results.get("late_or_unconfirmed_po", {}).get("counts"),
            # per detector: of the records it should judge, how many it judged and why
            # the others were not (``_coverage``)
            "coverage": {name: results[name]["coverage"] for name in DETECTORS
                         if name in results and results[name].get("coverage")},
            "top_finding": results.get("orders_at_risk", {}).get("finding")}


_POPULATIONS_KEY = "populations"   # inbox meta: per rule, the populations of its last fold


def _populations(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_POPULATIONS_KEY,)).fetchone()
    try:
        held = json.loads(row[0]) if row else {}
    except ValueError:
        held = {}
    return held if isinstance(held, dict) else {}


@dataclasses.dataclass(frozen=True)
class Scope:
    """An incremental pass (``operating.proactive_incremental``): ``subjects``, per rule,
    the records it judges again (every subject whose declared inputs hold a changed
    record, or whose truth moved with the day); ``read``, every record those judgements
    read; ``audience_keys``: the store's keys (the generation's and its changed records')."""
    subjects: Mapping[str, frozenset[str]]
    read: frozenset[str]
    audience_keys: frozenset[str] | None = None


def _fold_scoped(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
                 gaps: Mapping[str, Mapping[str, Any]], run_at: str, scope: Scope) -> None:
    """Fold an incremental pass: the gaps it found are written as a full run writes them
    (``_upsert_gaps``), and of each rule that judged its subjects whole (``complete``) an
    open gap about one of them that it no longer finds is resolved. The run records,
    counts, coverage and generation stay the last full run's."""
    _upsert_gaps(conn, results, gaps, run_at)
    _note_unchecked(conn, results, scope.subjects)
    for name, result in results.items():
        if result["status"] != "complete":
            continue
        unchecked = {o for o in result.get("unchecked") or () if isinstance(o, str)}
        conn.execute(
            "UPDATE gaps SET state = 'resolved', resolved_at = ? WHERE state = 'open' "
            "AND detector = ? AND object_id IN (SELECT value FROM json_each(?)) "
            "AND gap_id NOT IN (SELECT value FROM json_each(?))",
            (run_at, name, json.dumps(sorted(set(scope.subjects.get(name, ())) - unchecked)),
             json.dumps(sorted(gaps))))


def _fold(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
          gaps: Mapping[str, Mapping[str, Any]], scope: Sequence[tuple[str, str, int]],
          run_at: str, as_of: date, digest: str | None, summary: Mapping[str, Any], *,
          record_run: bool = True) -> None:
    if record_run:
        # a run of some detectors only (the vendor-mail pass) is no build's run: it never
        # moves the run the inbox is read as of ("new this generation", stale, as of)
        conn.execute("INSERT INTO runs (run_at, as_of, store_digest, summary) VALUES "
                     "(?, ?, ?, ?)", (run_at, as_of.isoformat(), digest,
                                      json.dumps(summary, sort_keys=True)))
        conn.execute("DELETE FROM runs WHERE run_id NOT IN (SELECT run_id FROM runs "
                     "ORDER BY run_id DESC LIMIT ?)", (KEEP_RUNS,))
    if set(SET_BASED) <= set(results):   # a run of some detectors only keeps the scope
        conn.execute("DELETE FROM scope")
        conn.executemany("INSERT INTO scope VALUES (?, ?, ?)", scope)
    for name, result in results.items():
        if result["status"] == "not_configured" and conn.execute(
                "SELECT 1 FROM detector_runs WHERE detector = ?", (name,)).fetchone():
            continue   # a run without this detector's input says nothing about its gaps
        conn.execute(
            "INSERT INTO detector_runs VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (detector) DO "
            "UPDATE SET status = excluded.status, reasons = excluded.reasons, found = "
            "excluded.found, not_evaluated = excluded.not_evaluated, run_at = excluded.run_at",
            (name, result["status"], json.dumps(result["reasons"]), len(result["gaps"]),
             json.dumps(result["not_evaluated"], sort_keys=True), run_at))
        # The records this run's rule left unjudged with each skip's cause, replacing the
        # last run's: a reader is told the count of those it may read whose whole cause it
        # may read (``read_inbox``), never of the others.
        conn.execute("DELETE FROM unjudged_causes WHERE detector = ?", (name,))
        conn.execute("DELETE FROM unjudged_reasons WHERE detector = ?", (name,))
        conn.executemany("INSERT OR IGNORE INTO unjudged_reasons VALUES (?, ?, ?)",
                         [(name, n, str(skip.get("reason") or "not_judged"))
                          for n, skip in enumerate(result.get("unjudged") or ())])
        conn.executemany(
            "INSERT OR IGNORE INTO unjudged_causes VALUES (?, ?, ?, ?, ?)",
            [(name, n, object_id, markings, int(object_id in skip["subjects"]))
             for n, skip in enumerate(result.get("unjudged") or ())
             for object_id, markings in skip["cause"]])
        if result["status"] != "not_configured":
            conn.execute("INSERT OR REPLACE INTO detector_history VALUES (?, ?, ?, ?)",
                         (name, run_at, result["status"], len(result["gaps"])))
            # what was found per gap kind, and per kind whether late by the status rule
            # (``kind:late``/``kind:not_late``): "seen live" is said per event type
            found = Counter()
            for gap in result["gaps"]:
                late = gap["follow_up"].get("days_late") is not None
                found.update([gap["kind"], f"{gap['kind']}:{'late' if late else 'not_late'}"])
            conn.executemany("INSERT OR REPLACE INTO kind_history VALUES (?, ?, ?)",
                             [(kind, run_at, n) for kind, n in sorted(found.items())])
        if isinstance(result.get("cache"), Mapping):
            # merged, never replaced wholesale (a build's run and the vendor pass may fold
            # one after the other): a root no longer named by recent mail drops, every
            # reading this run holds is stored, the others stay as they were
            conn.execute("DELETE FROM event_cache WHERE detector = ? AND root NOT IN (SELECT "
                         "value FROM json_each(?))", (name, json.dumps(result["roots"])))
            conn.executemany("INSERT OR REPLACE INTO event_cache VALUES (?, ?, ?)", [
                (name, root, json.dumps(reading, sort_keys=True))
                for root, reading in result["cache"].items()])
        conn.execute(
            "INSERT INTO rule_coverage VALUES (?, ?, ?) ON CONFLICT (detector) DO UPDATE SET "
            "unattributed = excluded.unattributed, run_at = excluded.run_at",
            (name, int(result.get("unattributed") or 0), run_at))
        if result["status"] in READ_WHOLE_DATA:
            _note_generation(conn, name, digest, run_at)
    _upsert_gaps(conn, results, gaps, run_at)
    complete = [name for name, result in results.items() if result["status"] == "complete"]
    _note_unchecked(conn, results)
    conn.execute(
        "UPDATE gaps SET state = 'resolved', resolved_at = ? WHERE state = 'open' "
        "AND detector IN (SELECT value FROM json_each(?)) "
        "AND gap_id NOT IN (SELECT value FROM json_each(?)) "
        "AND (detector || '|' || object_id) NOT IN (SELECT value FROM json_each(?))",
        (run_at, json.dumps(complete), json.dumps(sorted(gaps)),
         json.dumps(_unchecked_keys(results))))


# Inbox meta key per detector: the subjects its last run read but could not judge (a failed
# read, an undecided figure; the rule's result ``unchecked``). Their open gaps are neither
# found again nor resolved by that run: they stay listed, marked as not rechecked
# (``read_inbox``), until a run judges them.
_UNCHECKED_KEY = "unchecked:"


def _unchecked_keys(results: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(f"{name}|{oid}" for name, result in results.items()
                  for oid in result.get("unchecked") or () if isinstance(oid, str))


def _note_unchecked(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
                    scoped: Mapping[str, Iterable[str]] | None = None) -> None:
    """Record each rule's unchecked subjects (a whole run replaces its list; an incremental
    pass replaces only its own subjects' entries, ``scoped``)."""
    for name, result in results.items():
        key = _UNCHECKED_KEY + name
        now = {o for o in result.get("unchecked") or () if isinstance(o, str)}
        if scoped is not None:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            try:
                held = set(json.loads(row[0])) if row is not None else set()
            except ValueError:
                held = set()
            now |= held - set(scoped.get(name, ()))
        if now:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (key, json.dumps(sorted(now))))
        else:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def _held_unchecked(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(rule, subject) of every subject a rule's last run could not judge; an unreadable
    record holds none (its gaps then follow the rule's status alone)."""
    out: set[tuple[str, str]] = set()
    for key, value in conn.execute("SELECT key, value FROM main.meta WHERE key LIKE ?",
                                   (_UNCHECKED_KEY + "%",)):
        try:
            ids = json.loads(value)
        except ValueError:
            continue
        out |= {(key[len(_UNCHECKED_KEY):], o) for o in ids if isinstance(o, str)}
    return out


def _upsert_gaps(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
                 gaps: Mapping[str, Mapping[str, Any]], run_at: str) -> None:
    """Write each found gap (a resolved one reopened, its acknowledgements dropped)."""
    for gap in gaps.values():
        before = conn.execute("SELECT state FROM gaps WHERE gap_id = ?", (gap["id"],)).fetchone()
        if before is not None and before["state"] == "resolved":
            conn.execute("DELETE FROM acks WHERE gap_id = ?", (gap["id"],))   # reopened
        elif before is not None and gap["kind"] in WHOLE_ONLY \
                and results[gap["detector"]]["status"] != "complete":
            # a money claim summed over a run that did not finish may be a part: the open
            # gap keeps the wording, citations and time of the last run that did (never a
            # fresh last-seen: over newer data it is then listed marked as not rechecked,
            # ``_carried``, never as current)
            continue
        conn.execute(
            "INSERT INTO gaps VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL) "
            "ON CONFLICT (gap_id) DO UPDATE SET summary = excluded.summary, follow_up = "
            "excluded.follow_up, priority = excluded.priority, state = 'open', last_seen = "
            "excluded.last_seen, resolved_at = NULL",
            (gap["id"], gap["kind"], gap["detector"], gap["object_id"], gap["object_type"],
             gap["summary"], json.dumps(gap["follow_up"], sort_keys=True),
             PRIORITY.index(gap["follow_up"]["priority"]), run_at, run_at))
        conn.execute("DELETE FROM gap_cites WHERE gap_id = ?", (gap["id"],))
        rows = {(grp, c[0]): (gap["id"], grp, c[0], c[1], c[2])
                for grp, group in enumerate(gap["groups"]) for c in group}
        for c in gap["extra"]:
            rows.setdefault((-1, c[0]), (gap["id"], -1, c[0], c[1], c[2]))
        conn.executemany("INSERT INTO gap_cites VALUES (?, ?, ?, ?, ?)", rows.values())
        conn.execute("DELETE FROM gap_requires WHERE gap_id = ?", (gap["id"],))
        conn.executemany("INSERT INTO gap_requires VALUES (?, ?)",
                         [(gap["id"], key) for key in sorted(set(gap.get("requires", ())))])


# A detector whose run ended in one of these read the store it ran over: what it found is
# that store's (a truncated run's findings stand, its unfound older gaps do not).
READ_WHOLE_DATA = ("complete", "truncated")
_GENERATION_KEY = "generation:"   # inbox meta key per detector: the data its gaps are of


def _note_generation(conn: sqlite3.Connection, detector: str, digest: str | None,
                     run_at: str) -> None:
    """Record that ``detector`` ran over the store of ``digest`` at ``run_at``: the first
    run over that data is kept (``since``), so a gap last seen before it is of older data."""
    key = _GENERATION_KEY + detector
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        held = json.loads(row[0]) if row is not None else None
    except ValueError:
        held = None
    # and the code it ran: a gap found by older code than the deployed engine is of
    # older code (``_current_since``), never shown as current (``operating.derived_status``)
    code = engine_code()
    if isinstance(held, dict) and held.get("digest") == digest \
            and isinstance(held.get("since"), str) and held.get("code") == code:
        return
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (key, json.dumps({"digest": digest, "since": run_at, "code": code},
                                  sort_keys=True)))


def engine_code() -> str | None:
    """The deployed engine's code version (``operating.derived_status``): what each rule's
    record names, so findings of older code are told apart from this code's."""
    from . import derived_status
    return derived_status.version("proactive-inbox")


def _moment(text: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _current_since(conn: sqlite3.Connection, served: str | None,
                   older: set[str] | None = None
                   ) -> tuple[dict[str, datetime | None], dict[str, str | None]]:
    """Per detector, from when its gaps are of the data served now (``served``: the store's
    content digest): ``{detector: since}`` for those that ran over it (None: every gap, an
    inbox written before per-detector records whose last full run was over this data), and
    ``{detector: last run}`` for those that did not (their gaps are of older data: never
    shown as current, ``read_inbox``'s ``refreshing``). A rule whose last run was by
    other code than the deployed engine's is behind too (``older``: its name added)."""
    older = set() if older is None else older
    held: dict[str, Any] = {}
    for key, value in conn.execute("SELECT key, value FROM main.meta WHERE key LIKE ?",
                                   (_GENERATION_KEY + "%",)):
        try:
            held[key[len(_GENERATION_KEY):]] = json.loads(value)
        except ValueError:
            held[key[len(_GENERATION_KEY):]] = None   # unreadable: of no known data
    last = conn.execute("SELECT store_digest FROM main.runs ORDER BY run_id DESC "
                        "LIMIT 1").fetchone()
    ran = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, run_at FROM main.detector_runs")}
    statuses = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, status FROM main.detector_runs")}
    current: dict[str, datetime | None] = {}
    behind: dict[str, str | None] = {}
    code = engine_code()
    for name in sorted(set(DETECTORS) | set(ran) | set(held)):
        record = held.get(name, ...)
        if (isinstance(record, dict) and (code is None or record.get("code") != code)) or (
                record is ... and statuses.get(name) in READ_WHOLE_DATA):
            # found by older code than the engine deployed (or one not known): its gaps
            # wait for a run of this code (the deploy's refresh), as older data's do
            behind[name] = ran.get(name)
            older.add(name)
            continue
        if record is ...:
            # no per-detector record (an older engine's inbox): its last full run says
            if last is not None and last[0] == served:
                current[name] = None
            else:
                behind[name] = ran.get(name)
            continue
        since = _moment(record.get("since")) if isinstance(record, dict) else None
        if isinstance(record, dict) and record.get("digest") == served and since is not None:
            current[name] = since
        else:
            behind[name] = ran.get(name)
    return current, behind


def _of_served(row: Mapping[str, Any], current: Mapping[str, datetime | None]) -> bool:
    """Whether a gap was found over the data served now (else it may say what the record
    pages no longer do: never shown as current)."""
    if row["detector"] not in current:
        return False
    since = current[row["detector"]]
    if since is None:
        return True
    # an open gap was found again over it; a resolved one was resolved by a run over it
    keys = row.keys() if hasattr(row, "keys") else ()
    resolved = "state" in keys and row["state"] == "resolved"
    seen = _moment(row["resolved_at"] if resolved else row["last_seen"])
    return seen is not None and seen >= since


def rules_behind(store_path: Any, inbox: Any = None) -> set[str]:
    """The rules whose findings in the inbox are not of the served generation and the
    deployed code (``_current_since``): each needs a whole run before it is current."""
    store = Path(store_path)
    path = Path(inbox) if inbox is not None else inbox_path(store)
    if not path.is_file():
        return set(DETECTORS)
    with closing(_attach(path, store)) as conn:
        served = _served_digest(conn, store)
        older: set[str] = set()
        _current, behind = _current_since(conn, served[0] if served else None, older)
    return (set(behind) | older) & set(DETECTORS)


def served_behind(store_path: Any, inbox: Any = None) -> bool:
    """Whether the inbox's last full run is over other data than the store served now (or
    there is none): the scheduled refresh is due (``operating.vendor_watch``). Read only."""
    store = Path(store_path)
    path = inbox_path(store) if inbox is None else Path(inbox)
    if not path.is_file():
        return True
    with closing(_attach(path, store)) as conn:
        last = conn.execute("SELECT store_digest FROM main.runs ORDER BY run_id DESC "
                            "LIMIT 1").fetchone()
        served = _served_digest(conn, store)
        older: set[str] = set()
        _current_since(conn, served[0] if served else None, older)
    # or a rule's findings are of older code than the engine deployed
    return last is None or last[0] != (served[0] if served else None) or bool(older)


def _reader_view(conn: sqlite3.Connection, may_read: Callable[[str], bool],
                 held: Iterable[str]) -> tuple[str, str]:
    """(visible audience keys, allowed markings values) as JSON, from the store's keys."""
    row = conn.execute("SELECT value FROM st.store_meta WHERE key = 'audience_keys'").fetchone()
    keys = json.loads(row[0]) if row is not None else None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        keys = [r[0] for r in conn.execute("SELECT DISTINCT audience_key FROM st.objects")]
    if _has_requires(conn):   # a gap's required audiences, held by no record maybe
        keys = list(dict.fromkeys([*keys, *(r[0] for r in conn.execute(
            "SELECT DISTINCT audience_key FROM gap_requires ORDER BY audience_key"))]))
    visible = []
    for key in keys:
        try:
            if may_read(key) is True:
                visible.append(key)
        except Exception:  # noqa: BLE001 - a failing grant check denies
            continue
    return json.dumps(visible), json.dumps(ostore.allowed_marking_sets(held))


def _owned(follow_up: Any, kind: str, reader: Any) -> bool:
    """A gap of a person's own mail (``proactive_commitments.OWNER_KINDS``) only for that
    person's reader (its ``employee:<oid>`` principal); any other kind for every reader."""
    from .proactive_commitments import owned_by

    return owned_by(follow_up if isinstance(follow_up, Mapping) else {}, kind, reader)


_OWNER_KINDS = frozenset({"needs_my_reply", "reply_owed"})   # ``proactive_commitments.OWNER_KINDS``


def _has_requires(conn: sqlite3.Connection) -> bool:
    """An inbox written before ``gap_requires`` has none: its ``REQUIRES_GRANT`` gaps
    then show to nobody until the next run records their requirements."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                        "'gap_requires'").fetchone() is not None


_OBJECT_VISIBLE = (
    "EXISTS (SELECT 1 FROM st.objects so JOIN st.object_versions sv ON sv.object_id = "
    "so.object_id AND sv.superseded_at IS NULL WHERE so.object_id = {o} "
    "AND so.audience_key IN (SELECT value FROM json_each(:aud)) "
    "AND sv.markings IN (SELECT value FROM json_each(:marks)))")


def _visible(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], params: Mapping[str, str],
             decide: Callable[[Sequence[str]], Mapping[str, Any]]
             ) -> list[tuple[sqlite3.Row, list[str]]]:
    """Each gap the reader may see, with the citations it may open."""
    ids = json.dumps([row["gap_id"] for row in rows])
    cites: dict[str, list[tuple[int, str]]] = defaultdict(list)
    objects: dict[str, set[Any]] = defaultdict(set)
    groups: dict[str, set[int]] = defaultdict(set)
    for cite in conn.execute(
            "SELECT gc.gap_id, gc.grp, gc.handle, gc.object_id, "
            "gc.object_id IS NOT NULL AND gc.markings IN "
            "(SELECT value FROM json_each(:marks)) AND "
            + _OBJECT_VISIBLE.format(o="gc.object_id")
            + " AS shown FROM gap_cites gc WHERE gc.gap_id IN (SELECT value FROM "
            "json_each(:ids)) ORDER BY gc.gap_id, gc.grp, gc.handle", {**params, "ids": ids}):
        if cite["grp"] >= 0:
            groups[cite["gap_id"]].add(cite["grp"])
        if cite["shown"]:
            cites[cite["gap_id"]].append((cite["grp"], cite["handle"]))
            objects[cite["handle"]].add(cite["object_id"])
    requires: dict[str, set[str]] = defaultdict(set)
    if _has_requires(conn):
        for req in conn.execute(
                "SELECT gap_id, audience_key FROM gap_requires WHERE gap_id IN (SELECT value "
                "FROM json_each(:ids))", {"ids": ids}):
            requires[req["gap_id"]].add(req["audience_key"])
    granted = set(json.loads(params["aud"]))
    handles = sorted({h for found in cites.values() for _, h in found})
    refused = {h for h, why in decide(handles).items() if why is not None} if handles else set()
    # a document the reader's markings withhold (a pay stub) is never cited, whatever the
    # row's own markings say (``operating.search.document_marks``)
    from .search import document_marks
    held = set(json.loads(params["marks"]))
    refused |= {h for h in handles if document_marks.handle_withheld(
        h, held, [o for o in objects.get(h, ()) if o])}
    out = []
    for row in rows:
        open_ = [(grp, h) for grp, h in cites.get(row["gap_id"], []) if h not in refused]
        need = groups.get(row["gap_id"], set())
        required = requires.get(row["gap_id"], set())
        if row["detector"] in REQUIRES_GRANT and not (required and required <= granted):
            continue   # a verdict on what a source does not hold: the run's grant only
        if need and need <= {grp for grp, _ in open_}:
            out.append((row, list(dict.fromkeys(h for _, h in open_))))
    return out


def _served_digest(conn: sqlite3.Connection, store: Any) -> tuple[str] | None:
    """The data an inbox's runs are compared with, read as the engine stamps a run
    (``_Store.meta``): a host reading Postgres (``ontology_reader.serves``) compares with
    Postgres's change stamp, else with the served generation's digest (``(digest,)``, None
    when it has none). Without it a mode-1 host (``PLX_ONTOLOGY_PG=1``) compared a Postgres
    run with the SQLite generation's digest and held every gap back as of older data.
    Postgres unreadable: the generation's digest, so a Postgres run's gaps stay held back."""
    from operating.pg import ontology_reader
    if ontology_reader.serves(store):
        stamp = ontology_reader.change_stamp()
        if stamp is not None:
            return (stamp,)
    row = conn.execute(
        "SELECT value FROM st.store_meta WHERE key = 'content_digest'").fetchone()
    return (row[0],) if row else None


def _attach(path: Path, store: Path, *, write: bool = False) -> sqlite3.Connection:
    mode = "rw" if write else "ro"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, isolation_level=None,
                           timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if not write:
            conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != INBOX_SCHEMA:
            raise ValueError("gap_inbox_schema_mismatch")
        from operating.pg import ontology_reader
        if ontology_reader.pg_only():
            _attach_postgres(conn)
        elif not Path(store).is_file():
            raise ostore.StoreError("store_missing")
        else:
            conn.execute("ATTACH DATABASE ? AS st", (f"file:{store}?mode=ro",))
        row = conn.execute("SELECT value FROM st.store_meta WHERE key = 'format'").fetchone()
        if row is None or row[0] != ostore.FORMAT:
            raise ostore.StoreError("store_format_mismatch")
    except BaseException:
        conn.close()
        raise
    return conn


def _attach_postgres(conn: sqlite3.Connection) -> None:
    """``st`` for an inbox when Postgres is the only store (``ontology_reader.pg_only``):
    the rows of the store the inbox's reads join, copied from Postgres into memory, so its
    permission SQL (``_OBJECT_VISIBLE``, ``_reader_view``) runs as written: ``store_meta``
    (format, content digest; ``audience_keys``: every audience key the ontology's objects
    hold now), and the objects the inbox names (gaps, citations, unjudged records) with
    their current versions' markings. Postgres not whole: ``StoreError``, never an inbox
    read over a partial store."""
    from operating.pg import ontology_reader
    problem = ontology_reader.whole()
    if problem is not None:
        raise ostore.StoreError("store_not_whole", problem)
    named: set[str] = set()
    for table in ("gaps", "gap_cites", "unjudged_causes"):
        try:
            named.update(r[0] for r in conn.execute(
                f"SELECT DISTINCT object_id FROM {table} WHERE object_id IS NOT NULL"))
        except sqlite3.OperationalError:   # an inbox written before the table
            continue
    import psycopg
    with psycopg.connect(ontology_reader.dsn(), connect_timeout=5) as pg:
        meta = dict(pg.execute("SELECT key, value FROM ontology.store_meta WHERE key IN "
                               "('format', 'content_digest')").fetchall())
        # the data Postgres holds now, changed by every delta layer and migration (a
        # generation's digest no longer changes): what the inbox's runs are compared with
        stamp = ontology_reader.change_stamp()
        if stamp is not None:
            meta["content_digest"] = stamp
            # said: the digest is the data version, so a reader's counts may be kept by it
            meta["changes_stamp"] = "1"
        # the distinct keys by their index, one probe per key
        keys = [r[0] for r in pg.execute(
            "WITH RECURSIVE k (key) AS (SELECT min(audience_key) FROM ontology.objects "
            "UNION ALL SELECT (SELECT min(audience_key) FROM ontology.objects "
            "WHERE audience_key > k.key) FROM k WHERE k.key IS NOT NULL) "
            "SELECT key FROM k WHERE key IS NOT NULL ORDER BY key")]
        ids = sorted(named)
        objects = pg.execute("SELECT object_id, audience_key FROM ontology.objects "
                             "WHERE object_id = ANY(%s)", (ids,)).fetchall()
        versions = pg.execute("SELECT object_id, superseded_at, markings FROM "
                              "ontology.object_versions WHERE object_id = ANY(%s) "
                              "AND superseded_at IS NULL", (ids,)).fetchall()
    query_only = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only = OFF")   # the copy in memory only; restored below
    conn.execute("ATTACH DATABASE ':memory:' AS st")
    conn.execute("CREATE TABLE st.store_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE st.objects (object_id TEXT PRIMARY KEY, audience_key TEXT)")
    conn.execute("CREATE TABLE st.object_versions (object_id TEXT, superseded_at TEXT, "
                 "markings TEXT)")
    conn.execute("CREATE INDEX st.object_versions_by_object ON object_versions (object_id)")
    conn.executemany("INSERT INTO st.store_meta VALUES (?, ?)",
                     [*meta.items(), ("audience_keys", json.dumps(keys))])
    conn.executemany("INSERT INTO st.objects VALUES (?, ?)", objects)
    conn.executemany("INSERT INTO st.object_versions VALUES (?, ?, ?)",
                     [(o, None if at is None else str(at), m) for o, at, m in versions])
    if query_only:
        conn.execute("PRAGMA query_only = ON")


# the skips a reader may not read the whole cause of: any record of the skip outside the
# reader's markings or audiences. Computed once per query (an uncorrelated set SQLite builds
# once), never re-evaluated per counted record: a skip of 10,000 records was 10^8 checks.
_HIDDEN_SKIPS = (
    "SELECT c.detector, c.skip FROM unjudged_causes c WHERE NOT (c.markings IN (SELECT "
    "value FROM json_each(:marks)) AND " + _OBJECT_VISIBLE.format(o="c.object_id") + ")")

# per (inbox, store data, reader's grant): the counts ``_rule_coverage`` reads; the inbox
# and the store change only on a run or a delta, while every page read asks again
_COVERAGE_KEPT = 64
_coverage_kept: OrderedDict[tuple[Any, ...],
                            tuple[dict[str, int], dict[str, dict[str, int]]]] = OrderedDict()
_coverage_lock = threading.Lock()


def _file_stamp(path: str) -> tuple[Any, ...] | None:
    """A SQLite file as it stands: identity, time, size and its first 32 bytes (a
    database's file change counter, which every commit not through a WAL moves; a WAL's
    checkpoint sequence and salts, which every restart of it moves). None: no file."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            head = fh.read(32)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size, head)


def _wal_stamp(path: str) -> tuple[Any, ...] | None:
    """``_file_stamp`` of a WAL file, None for an empty one as for none: an empty WAL holds
    no frame, so the database is its main file (whose stamp says when it changed); each
    reader that opened and reset it moved its time, and every page read built the table
    again (40 s of CPU three times over at once, 2026-11-13)."""
    stamp = _file_stamp(path)
    return None if stamp is None or stamp[3] == 0 else stamp


def _coverage_key(conn: sqlite3.Connection, params: Mapping[str, str]
                  ) -> tuple[Any, ...] | None:
    """What the reader's counts depend on, or None (never kept) when any part cannot be
    said: the inbox file (and its WAL) and its last write, the store's data (a SQLite
    file and its digest, or Postgres's change stamp), and the reader's audiences and
    markings exactly as the query reads them."""
    files = {row[1]: row[2] for row in conn.execute("PRAGMA database_list")}
    inbox, store = files.get("main") or "", files.get("st")
    if not inbox or store is None:
        return None   # an in-memory inbox: nothing says when it changed
    stamps = (_file_stamp(inbox), _wal_stamp(inbox + "-wal"))
    if stamps[0] is None:
        return None
    meta = dict(conn.execute("SELECT key, value FROM st.store_meta WHERE key IN "
                             "('content_digest', 'changes_stamp')").fetchall())
    if store:
        held = (store, _file_stamp(store), _wal_stamp(store + "-wal"),
                meta.get("content_digest"))
        if held[1] is None:
            return None
    else:
        # Postgres copied into memory: its stamp changes with every delta layer; without
        # one the store's change is unknown, so nothing is kept
        if not meta.get("changes_stamp") or not meta.get("content_digest"):
            return None
        held = ("postgres", meta["content_digest"])
    inner = tuple(conn.execute(
        "SELECT (SELECT max(rowid) FROM unjudged_causes), (SELECT count(*) FROM "
        "unjudged_causes), (SELECT max(run_at) FROM detector_runs), (SELECT "
        "group_concat(detector || '=' || unattributed || '@' || run_at) FROM "
        "rule_coverage)").fetchone())
    return (inbox, stamps, inner, held, params["aud"], params["marks"])


def _rule_coverage(conn: sqlite3.Connection, params: Mapping[str, str],
                   detectors: Mapping[str, str]) -> list[dict[str, Any]]:
    """Per rule, its last run's status and how many records this reader may read it left
    unjudged (``not_evaluated``): counted over the records the reader's grant shows, of
    the skips whose whole cause (every record at its markings) the reader may read, never
    the store's: a skip a hidden record or link caused is not counted, so the count never
    hints at it. None (unknown, never 0) when the rule left a skip no record stands for,
    or the inbox predates per-skip causes while its run counted skips."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    recorded: dict[str, int] = {}
    if {"rule_coverage", "unjudged_causes"} <= tables:
        recorded = {row[0]: row[1] for row in conn.execute(
            "SELECT detector, unattributed FROM rule_coverage")}
    visible, by_reason = _visible_unjudged(conn, params, tables)
    counted = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, not_evaluated FROM detector_runs")}
    said = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, reasons FROM detector_runs")}
    out = []
    for name in DETECTORS:
        try:
            skips = sum(int(v) for v in json.loads(counted.get(name) or "{}").values())
        except (ValueError, TypeError, AttributeError):
            skips = 1   # unreadable: never read as nothing skipped
        if name not in recorded:
            shown = None if skips else 0
        else:
            shown = None if recorded[name] else visible.get(name, 0)
        try:
            given = json.loads(said.get(name) or "[]")
            reasons = [r for r in given if isinstance(r, str)] if isinstance(given, list) else []
        except (ValueError, TypeError):
            reasons = []    # unreadable: no reason is said, never a made-up one
        out.append({"rule": name, "status": detectors.get(name, "not_run"),
                    "not_evaluated": shown, "reasons": reasons,
                    # per reason, only when the whole count is known for this reader
                    "not_evaluated_by_reason": dict(sorted(by_reason.get(name, {}).items()))
                    if shown else {}})
    return out


def _visible_unjudged(conn: sqlite3.Connection, params: Mapping[str, str], tables: set[str]
                      ) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Per rule, the records this reader may read it left unjudged, of the skips whose
    whole cause the reader may read; and the same per reason. Kept per
    ``_coverage_key`` (never for a key that cannot be said)."""
    if "unjudged_causes" not in tables:
        return {}, {}
    key = _coverage_key(conn, params) if {"detector_runs", "rule_coverage"} <= tables else None
    if key is not None:
        with _coverage_lock:
            hit = _coverage_kept.get(key)
            if hit is not None:
                _coverage_kept.move_to_end(key)
                return hit[0], {k: dict(v) for k, v in hit[1].items()}
    # what every reader's counts are made of, read once per inbox and store (not per
    # reader); this reader's grant then decides each kind of cause, never each record
    table = _unjudged_table(conn, tables, key[:4] if key is not None else None)
    marks = set(json.loads(params["marks"]))
    auds = set(json.loads(params["aud"]))
    shown_cause = [cause_marks in marks and any(a in auds and m in marks for a, m in pairs)
                   for cause_marks, pairs in table["causes"]]
    shown_skip = [all(shown_cause[c] for c in kinds) for kinds in table["skips"]]
    counted: dict[str, int] = defaultdict(int)
    per_reason: dict[tuple[str, str], int] = defaultdict(int)
    for (name, reason, skips), n in table["counts"].items():
        if any(shown_skip[k] for k in skips):
            if reason is None:
                counted[name] += n
            else:
                per_reason[(name, reason)] += n
    visible = dict(counted) if "rule_coverage" in tables else {}
    by_reason: dict[str, dict[str, int]] = defaultdict(dict)
    for (name, reason), n in per_reason.items():
        by_reason[name][reason] = n
    by_reason = dict(by_reason)
    if key is not None:
        with _coverage_lock:
            _coverage_kept[key] = (dict(visible), {k: dict(v) for k, v in by_reason.items()})
            while len(_coverage_kept) > _COVERAGE_KEPT:
                _coverage_kept.popitem(last=False)
    return visible, by_reason


# per (inbox, store data): ``_unjudged_table``, the same for every reader
_TABLES_KEPT = 4
_tables_kept: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()


def _unjudged_table(conn: sqlite3.Connection, tables: set[str],
                    key: tuple[Any, ...] | None) -> dict[str, Any]:
    """The rule coverage's unjudged records sorted by what a grant decides of them, for
    every reader at once: ``causes``, each kind of cause (its own markings, and the
    (audience, markings) of every current version of its record: none when the record is
    not in the store, never readable); ``skips``, each kind of skip (the cause kinds it
    holds: a skip is readable only when every cause is); ``counts``, per (rule, reason or
    None, the skip kinds a record is counted under) how many records. A reader's count of
    a rule is the records with at least one readable skip kind, as ``_visible_unjudged``
    counted them one by one. Kept per ``key`` (the inbox and store parts of
    ``_coverage_key``), never for a key that cannot be said."""
    if key is not None:
        with _coverage_lock:
            hit = _tables_kept.get(key)
            if hit is not None:
                _tables_kept.move_to_end(key)
                return hit
        hit = _table_from_disk(key)
        if hit is not None:
            with _coverage_lock:
                _tables_kept[key] = hit
                while len(_tables_kept) > _TABLES_KEPT:
                    _tables_kept.popitem(last=False)
            return hit
        # one build per key at a time: the page reads that ask at once wait for it and
        # read what it kept, rather than each building it again beside it
        with _coverage_lock:
            building = _tables_building.setdefault(key, threading.Lock())
        with building:
            with _coverage_lock:
                hit = _tables_kept.get(key)
            if hit is not None:
                return hit
            try:
                return _built_table(conn, tables, key)
            finally:
                with _coverage_lock:
                    if _tables_building.get(key) is building:
                        del _tables_building[key]
    return _built_table(conn, tables, key)


_tables_building: dict[tuple[Any, ...], threading.Lock] = {}


def _built_table(conn: sqlite3.Connection, tables: set[str],
                 key: tuple[Any, ...] | None) -> dict[str, Any]:
    """``_unjudged_table`` read from the inbox (and kept, under ``key``)."""
    pairs_of: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for object_id, audience, markings in conn.execute(
            "SELECT so.object_id, so.audience_key, sv.markings FROM st.objects so JOIN "
            "st.object_versions sv ON sv.object_id = so.object_id AND sv.superseded_at IS "
            "NULL WHERE so.object_id IN (SELECT DISTINCT object_id FROM unjudged_causes)"):
        pairs_of[object_id].add((audience, markings))
    # each record's versions as one of the few kinds there are (not in the store: none)
    places: dict[frozenset, int] = {frozenset(): 0}
    place_of = {o: places.setdefault(frozenset(p), len(places)) for o, p in pairs_of.items()}
    del pairs_of
    causes: dict[tuple[Any, int], int] = {}
    skip_causes: dict[tuple[str, str], set[int]] = defaultdict(set)
    subjects: list[tuple[str, str, str]] = []
    for name, skip, object_id, markings, subject in conn.execute(
            "SELECT detector, skip, object_id, markings, subject FROM unjudged_causes"):
        kind = (markings, place_of.get(object_id, 0))
        skip_causes[(name, skip)].add(causes.setdefault(kind, len(causes)))
        if subject == 1:
            subjects.append((name, skip, object_id))
    skips: dict[frozenset, int] = {}
    skip_kind = {pair: skips.setdefault(frozenset(held), len(skips))
                 for pair, held in skip_causes.items()}
    reasons: dict[tuple[str, str], list[str]] = defaultdict(list)
    if "unjudged_reasons" in tables:
        for name, skip, reason in conn.execute(
                "SELECT detector, skip, reason FROM unjudged_reasons"):
            if reason is not None:
                reasons[(name, skip)].append(reason)
    under: dict[tuple[str, Any, str], set[int]] = defaultdict(set)
    for name, skip, object_id in subjects:
        kind = skip_kind[(name, skip)]
        under[(name, None, object_id)].add(kind)
        for reason in reasons.get((name, skip), ()):
            under[(name, reason, object_id)].add(kind)
    counts: dict[tuple[str, Any, frozenset], int] = defaultdict(int)
    for (name, reason, _), kinds in under.items():
        counts[(name, reason, frozenset(kinds))] += 1
    place_pairs = {n: tuple(sorted(p)) for p, n in places.items()}
    table = {"causes": [(m, place_pairs[p]) for (m, p), _ in
                        sorted(causes.items(), key=lambda item: item[1])],
             "skips": [tuple(sorted(k)) for k, _ in sorted(skips.items(),
                                                          key=lambda item: item[1])],
             "counts": dict(counts)}
    if key is not None:
        with _coverage_lock:
            _tables_kept[key] = table
            while len(_tables_kept) > _TABLES_KEPT:
                _tables_kept.popitem(last=False)
        _table_to_disk(key, table)
    return table


# ``_unjudged_table`` kept on disk too, per key, so every process (the API, its warm-up and
# section workers, each Ask host) and a restart read it instead of each building it again
# (15 s for the owner's inbox under load, 2026-11-12). It holds markings, audience keys and
# counts only, never a record. ``PLX_RULE_COVERAGE_DIR`` (default
# ``~/.cache/plx/rule-coverage``); the newest ``_TABLES_ON_DISK`` files are kept.
_TABLE_FORMAT = "unjudged-table/1"
_TABLES_ON_DISK = 8


def _table_file(key: tuple[Any, ...]) -> Path:
    folder = Path(os.environ.get("PLX_RULE_COVERAGE_DIR")
                  or Path.home() / ".cache" / "plx" / "rule-coverage")
    digest = hashlib.sha256(f"{_TABLE_FORMAT}\x00{key!r}".encode()).hexdigest()
    return folder / f"table-{digest[:32]}.json"


def _table_from_disk(key: tuple[Any, ...]) -> dict[str, Any] | None:
    """The table kept for ``key`` (its file names the key's digest and holds the key's
    text again: a digest collision or a foreign file reads as none); anything unreadable
    or of another shape: none (built as before)."""
    try:
        doc = json.loads(_table_file(key).read_text(encoding="utf-8"))
        if doc.get("format") != _TABLE_FORMAT or doc.get("key") != repr(key):
            return None
        causes = [(m if m is None else str(m),
                   tuple((str(a), str(b)) for a, b in pairs)) for m, pairs in doc["causes"]]
        skips = [tuple(int(k) for k in kinds) for kinds in doc["skips"]]
        counts = {(str(name), None if reason is None else str(reason),
                   frozenset(int(k) for k in kinds)): int(n)
                  for name, reason, kinds, n in doc["counts"]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    if any(k >= len(skips) for _, _, kinds in counts for k in kinds) or \
            any(c >= len(causes) for kinds in skips for c in kinds):
        return None   # an index past its list: not this table
    return {"causes": causes, "skips": skips, "counts": counts}


def _table_to_disk(key: tuple[Any, ...], table: Mapping[str, Any]) -> None:
    """Keep ``table`` for ``key``, written whole or not at all (owner-only, a temporary
    file renamed over it); a directory that cannot be written keeps nothing."""
    path = _table_file(key)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = json.dumps({"format": _TABLE_FORMAT, "key": repr(key),
                           "causes": [[m, [list(p) for p in pairs]]
                                      for m, pairs in table["causes"]],
                           "skips": [list(kinds) for kinds in table["skips"]],
                           "counts": [[name, reason, sorted(kinds), n] for
                                      (name, reason, kinds), n in table["counts"].items()]})
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        kept = sorted(path.parent.glob("table-*.json"), key=lambda p: p.stat().st_mtime)
        for old in kept[:-_TABLES_ON_DISK]:
            old.unlink(missing_ok=True)
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink(missing_ok=True)   # a write that failed leaves nothing behind
        except OSError:
            pass


def read_inbox(inbox: Any, store: Any, *, reader: str, may_read: Callable[[str], bool],
               held: Iterable[str], decide: Callable[[Sequence[str]], Mapping[str, Any]],
               states: Sequence[str] = ("open", "acknowledged"),
               limit: int = MAX_SHOWN, kinds: Sequence[str] | None = None,
               today: date | None = None, since: str | None = None,
               all_owners: bool = False, include_off: bool = False,
               enabled: Iterable[str] = ()) -> dict[str, Any]:
    """The gaps one reader may see, trimmed: every late order first (longest late first), then
    by priority; what the last run
    covered of what this reader may read. ``may_read(audience_key)``, ``held`` (the
    markings the reader holds) and ``decide(handles)`` (None per handle it may open)
    are the reader's grant; anything they refuse is never shown nor counted. ``complete``
    is true only when every rule ran to the end; ``not_run`` names each rule that did not
    (not configured on this schedule, blocked, or never run), so no reader takes an
    inbox some rules never looked at for a clean result. ``kinds``: only gaps of these
    kinds are read (the briefing's events), so other kinds never use up ``limit``.

    A rule whose findings are not of the served data and deployed code (``refreshing``)
    or whose last run failed keeps its last good findings on the list, each marked
    (``carried``: why, and when it was last checked; a failed rule's failure time), never
    shown as current and never hidden all at once (a code change would otherwise empty
    the briefing until the next pass). Relative wording ("12 days past", "in 3 days") is
    counted to ``today`` (default the business day now) when read, never frozen at the
    run (``_relative``).
    ``since``: only gaps first seen at or after this ISO moment, newest first (an alert
    rule's new findings: an older backlog never uses up ``limit``).
    ``all_owners``: a person's own-mail kinds are listed whoever owns them (everything else
    about the trim is unchanged); only the Team drill-down sets it: for the owner and the named Team
    readers, after the route has refused every other reader."""

    # an alert type the registry keeps off (``alert_kinds``) yields nothing to a reader
    # unless they switched it on; only a page that lists it on purpose reads it
    # (``include_off``: the Late POs page's at-risk panel)
    # An unnarrowed read (Home, plx.find's home mode) returns only kinds a registered type
    # names: a kind no one measured never reaches home (business rule 11-11: an alert reaches
    # home only once measured at the bar); a page naming its kinds still reads them.
    off_kinds: list[str] = []
    if not include_off:
        off = alert_kinds.off_gap_kinds(enabled)
        if kinds is None:
            kinds = sorted(alert_kinds.registered_gap_kinds() - off)
        kinds = [k for k in kinds if k not in off] or ["\x00none"]
        off_kinds = sorted(off)

    path = Path(inbox)
    today = _today() if today is None else today
    if not path.is_file():
        return {"status": "not_scheduled", "gaps": [], "scanned": 0, "truncated": False,
                "max_objects": None, "unscanned": None, "unread": {}, "complete": False,
                "not_run": {name: "not_run" for name in DETECTORS}}
    unknown = set(states) - set(STATES)
    if unknown:
        raise ValueError("gap_state_unknown")
    from . import feedback_signals
    # people's feedback on a gap (not useful, wrong): suppressed for whom it covers
    hide = feedback_signals.suppressor(feedback_signals.state_of(store))
    with closing(_attach(path, Path(store))) as conn:
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks}
        allowed = set(json.loads(marks))
        readable = set(json.loads(aud))   # the audiences this reader may read
        last = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        digest = _served_digest(conn, store)
        older: set[str] = set()
        current, behind = _current_since(conn, digest[0] if digest else None, older)
        scanned = conn.execute(
            "SELECT COALESCE(SUM(objects), 0) FROM scope WHERE audience_key IN (SELECT value "
            "FROM json_each(:aud)) AND markings IN (SELECT value FROM json_each(:marks))",
            params).fetchone()[0]
        runs = conn.execute("SELECT detector, status, reasons, not_evaluated, run_at FROM "
                            "detector_runs ORDER BY detector").fetchall()
        detectors = {row["detector"]: row["status"] for row in runs}
        failed_at = {row["detector"]: row["run_at"] for row in runs
                     if row["status"] == "failed"}
        # the rules whose last good findings are carried, marked, until a run of the
        # deployed code over the served data replaces them
        carried_rules = (set(behind) | older) & set(detectors)
        # the rules whose last run was cut short (its window or the run's budget spent):
        # what they found stands; what they did not reach again is their last good
        # finding over older data, carried and marked, never shown as current nor
        # dropped unseen (a rule slower than its window would otherwise show nothing)
        cut_short = {name for name, status in detectors.items() if status == "truncated"}
        # likewise a subject a rule's last run read but could not judge (``unchecked``)
        unchecked = _held_unchecked(conn)
        # each rule's last run (a full, 4-hourly, calling or incremental pass): its time
        run_at = {row["detector"]: row["run_at"] for row in runs
                  if isinstance(row["run_at"], str)}
        reasons = {row["detector"]: json.loads(row["reasons"]) for row in runs
                   if row["reasons"] not in (None, "[]")}
        # the late rule's skips by reason (store-wide, never per reader: a restricted
        # reader is told at most that the count is not exact, never what was skipped)
        late_skips = next((row["not_evaluated"] for row in runs
                           if row["detector"] == LATE_RULE), None)
        try:
            late_skips = json.loads(late_skips) if isinstance(late_skips, str) else None
        except ValueError:
            late_skips = None   # unreadable: never an exact count
        # each rule's recorded runs over this data: whether one ever found something, and
        # when first (the counts are the run's, never per reader)
        # an inbox an earlier engine wrote may lack the history tables (its next run adds
        # them): that history is unknown, the rest of the inbox still reads
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        has_history = "detector_history" in tables
        first_found = {row[0]: row[1] for row in (conn.execute(
            "SELECT kind, min(run_at) FROM kind_history WHERE found > 0 GROUP BY kind")
            if "kind_history" in tables else ())}
        history = {row["detector"]: {"runs": row["runs"], "first_found": row["first"]}
                   for row in (conn.execute(
                       "SELECT detector, count(*) AS runs, min(CASE WHEN found > 0 THEN "
                       "run_at END) AS first FROM detector_history GROUP BY detector")
                       if has_history else ())}
        stored = {"open"} if set(states) & {"open", "acknowledged"} else set()
        stored |= {"resolved"} & set(states)
        global_states = json.dumps(sorted(stored))
        shown: list[dict[str, Any]] = []
        more = False
        orders: dict[str, tuple[Any, dict[str, Any]]] = {}   # order key -> (rank, entry)
        offset, page = 0, max(limit + 1, 200)
        while not more:
            rows = conn.execute(
                "SELECT g.*, (SELECT at FROM acks a WHERE a.gap_id = g.gap_id AND a.reader = "
                ":reader) AS acked FROM gaps g WHERE g.state IN (SELECT value FROM "
                "json_each(:states)) AND (:kinds IS NULL OR g.kind IN (SELECT value FROM "
                "json_each(:kinds))) AND g.kind NOT IN (SELECT value FROM json_each(:off)) "
                "AND (:since IS NULL OR julianday(g.first_seen) >= "
                "julianday(:since)) AND " + _OBJECT_VISIBLE.format(o="g.object_id")
                + " ORDER BY CASE WHEN :since IS NOT NULL THEN julianday(g.first_seen) END "
                "DESC, json_extract(g.follow_up, '$.days_late') IS NULL, "
                "json_extract(g.follow_up, '$.days_late') DESC, "
                # a list of near-expiry lots alone: soonest expiry first, so a page cut
                # short still holds every lot expiring before its last one
                "CASE WHEN :soonest THEN json_extract(g.follow_up, '$.event.days') END, "
                "g.priority, g.last_seen DESC, g.gap_id LIMIT :page OFFSET :offset",
                {**params, "reader": reader, "states": global_states, "page": page,
                 "soonest": kinds is not None and list(kinds) == ["lot_near_expiry"],
                 "offset": offset, "since": since,
                 "kinds": None if kinds is None else json.dumps(list(kinds)),
                 "off": json.dumps(off_kinds)}).fetchall()
            if not rows:
                break
            offset += len(rows)
            # a gap found over older data than the store served now is never shown: the
            # record page reads the served data, so the two would disagree
            rows = [row for row in rows if _of_served(row, current) or (
                (row["detector"] in carried_rules | cut_short
                 or (row["detector"], row["object_id"]) in unchecked)
                and row["state"] == "open")]
            for row, citations in _visible(conn, rows, params, decide):
                state = ("resolved" if row["state"] == "resolved"
                         else "acknowledged" if row["acked"] else "open")
                if state not in states or hide(row["gap_id"], reader) is not None:
                    continue
                follow_up = json.loads(row["follow_up"])
                if not all_owners and not _owned(follow_up, row["kind"], reader):
                    continue   # another person's own mail: never theirs to see
                order = follow_up.pop(_ORDER, None)
                follow_up.pop(_ATTESTS, None)   # the filters' key, never shown
                held = orders.get(order["key"]) if isinstance(order, dict) else None
                if held is not None:
                    # this order is listed: its governing record's item replaces a twin's,
                    # never a second item (each was decided visible to this reader)
                    if order["rank"] < held[0]:
                        index = next(i for i, g in enumerate(shown) if g is held[1])
                        shown[index] = entry = _shown(row, citations, state, follow_up, allowed, readable, today=today,
                                   carried=_carried(row, carried_rules, older, failed_at,
                                                    cut_short, current, unchecked))
                        orders[order["key"]] = (order["rank"], entry)
                    continue
                if len(shown) >= limit:
                    more = True
                    break
                entry = _shown(row, citations, state, follow_up, allowed, readable, today=today,
                                   carried=_carried(row, carried_rules, older, failed_at,
                                                    cut_short, current, unchecked))
                shown.append(entry)
                if isinstance(order, dict):
                    orders[order["key"]] = (order["rank"], entry)
        rules = _rule_coverage(conn, params, detectors)
    try:
        _learned_order(shown, store, reader)
    except Exception:  # noqa: BLE001 - lessons never break a read: the order stands
        pass
    failed = sorted(name for name, status in detectors.items() if status == "failed")
    truncated = any(status == "truncated" for status in detectors.values())
    # A rule that did not run (not configured on this schedule, blocked on a source, or
    # with no row at all) looked for nothing: the read is never complete while one remains.
    # a rule whose input this host has not set up yet (``SETUP_STATES``: the sheet readings
    # before the reading step runs here) is a setup state, named in ``detectors``, never a
    # rule that looked and missed: the read stays complete
    not_run = {name: detectors.get(name, "not_run") for name in DETECTORS
               if detectors.get(name) not in ("complete", "truncated", "failed",
                                              *SETUP_STATES)}
    # the rules whose findings are of older data than the store served now: theirs are not
    # shown until they run over it (the scheduled refresh), and the read says so
    refreshing = sorted(name for name in behind if name in detectors)
    checked = sorted(at for name in refreshing if isinstance(at := behind[name], str))
    return {"status": "read", "as_of": last["as_of"] if last else None,
            # each failed rule's failure time: its findings shown are its last good ones
            "failed_at": dict(sorted(failed_at.items())),
            "carried": sum(1 for g in shown if g.get("carried")),
            "run_at": last["run_at"] if last else None,
            "stale": bool(refreshing) or (bool(last) and (digest[0] if digest else None)
                                          != last["store_digest"]),
            "refreshing": refreshing, "checked_on": checked[0] if checked else None,
            # of those, the rules whose findings older code made: held back until the
            # deployed engine's run (``operating.derived_status refresh``)
            "older_code": sorted(name for name in older if name in detectors),
            "scanned": scanned, "truncated": truncated or bool(failed),
            "max_objects": None, "unscanned": None,
            "unread": {f"{name}_failed": 1 for name in failed},
            "detectors": detectors, "detector_reasons": reasons, "history": history,
            "detector_run_at": run_at,
            "kind_history": first_found,
            "not_run": not_run, "rules": rules,
            "complete": not (truncated or failed or not_run or refreshing),
            # the late count is the late rule's alone: exact when that rule ran to the end
            # over the data served and left no order its rule could not decide (the one
            # late-list rule, ``po_status.late_count_exact``); another rule cut short
            # never makes it inexact
            "late_exact": (detectors.get(LATE_RULE) == "complete"
                           and LATE_RULE not in refreshing
                           and po_status.late_count_exact(late_skips)),
            "gaps": shown, "more": more, "limit": limit}


def _today() -> date:
    """The business day a read counts relative wording to (``read_inbox``)."""
    return po_status.business_today()


def _carried(row: Mapping[str, Any], carried_rules: set[str], older: set[str],
             failed_at: Mapping[str, Any], cut_short: set[str] = frozenset(),
             current: Mapping[str, datetime | None] | None = None,
             unchecked: set[tuple[str, str]] = frozenset()) -> dict[str, Any] | None:
    """Why a shown finding is not current (None: it is): its rule's last run failed
    (``check_failed``, with the failure time), its rule ran older code (``older_code``) or
    over older data (``older_data``), or its rule's last run was cut short before finding
    it again (``check_incomplete``: ``cut_short``, a finding not of the data ``current``
    names; or its subject was ``unchecked`` by the rule's last run); with the time it was
    last found."""
    name = row["detector"]
    unfinished = (name in cut_short or (name, row["object_id"]) in unchecked) \
        and current is not None and not _of_served(row, current)
    if name not in carried_rules and name not in failed_at and not unfinished:
        return None
    reason = ("check_failed" if name in failed_at else "older_code" if name in older
              else "older_data" if name in carried_rules else "check_incomplete")
    return {"reason": reason, "last_checked": row["last_seen"],
            **({"check_failed_at": failed_at[name]} if name in failed_at else {})}


CARRIED_WORDS = {"check_failed": "check failed at {failed}; last checked {checked}",
                 "check_incomplete": "the latest check did not finish; last checked {checked}",
                 "older_code": "not rechecked since the rules changed; last checked {checked}",
                 "older_data": "not rechecked on the newest data; last checked {checked}"}
# A count of days in a finding's words, and which way it moves as days pass: "in 3 days"
# counts down, "12 days past/late/overdue/ago/old" counts up; any other count is left.
_RELATIVE = re.compile(r"\b(?P<in>in )?(?P<n>\d+) (?P<unit>days?)\b(?P<after> (?:past|late|"
                       r"overdue|ago|old|behind|since|without)\b)?")


def _relative(summary: str, event: Any, days_late: Any, found_day: date | None,
              today: date) -> tuple[str, Any, Any]:
    """``summary``'s day counts (and ``event['days']``, ``days_late``) moved from the day
    the run found them (``found_day``) to ``today``: only a count equal to the finding's
    own days, so a stated term ("30 days") is never touched."""
    if found_day is None or today <= found_day or not isinstance(summary, str):
        return summary, event, days_late
    shift = (today - found_day).days
    known = {v for v in ((event or {}).get("days") if isinstance(event, dict) else None,
                         days_late) if isinstance(v, int)}
    moved: dict[str, int] = {}

    def move(match: re.Match) -> str:
        n = int(match["n"])
        if n not in known or not (match["in"] or match["after"]):
            return match[0]
        new = n - shift if match["in"] else n + shift
        moved["down" if match["in"] else "up"] = new
        if match["in"] and new <= 0:
            return "today" if new == 0 else "by now"
        return f"{match['in'] or ''}{new} day{'' if new == 1 else 's'}{match['after'] or ''}"
    text = _RELATIVE.sub(move, summary)
    if "up" in moved and isinstance(days_late, int):
        days_late += shift
    if isinstance(event, dict) and isinstance(event.get("days"), int) and moved:
        event = {**event, "days": event["days"] + (shift if "up" in moved else -shift)}
    return text, event, days_late


def _learned_order(shown: list[dict[str, Any]], store: Any, reader: str) -> None:
    """An alert kind this reader keeps dismissing (``operating.learning``: a lesson from
    their own not useful / wrong marks) is weighed down (``learned``: its weight and the
    lesson) and listed after the rest, in place; nothing is hidden by it."""
    from . import feedback_signals, learning
    state = feedback_signals.state_of(store)
    lessons = learning.Lessons.load(learning.ledger_dir(state))
    if not lessons or not shown:
        return
    who = learning.person_id(reader)
    roles = [r for r, members in learning._roles_of(state).items()
             if who in {learning.person_id(m) for m in members}]
    from .briefing import EVENT_TYPES
    weights = []
    for entry in shown:
        names = [entry.get("kind")] + [n for n, spec in EVENT_TYPES.items()
                                       if entry.get("kind") in (spec.get("kinds") or ())]
        weight, lesson = min((lessons.alert_weight(reader, str(n), roles) for n in names
                              if n), key=lambda x: x[0], default=(1.0, None))
        if lesson is not None:
            entry["learned"] = {"weight": weight, "lesson_id": lesson}
        weights.append(weight)
    order = sorted(range(len(shown)), key=lambda i: -weights[i])
    shown[:] = [shown[i] for i in order]


def _shown(row: Mapping[str, Any], citations: Sequence[str], state: str,
           follow_up: dict[str, Any], allowed: Iterable[str] = (),
           audiences: Iterable[str] = (), *, today: date | None = None,
           carried: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One inbox row as its reader sees it (``follow_up`` without the order field): its
    day counts as of ``today`` (``_relative``) and, when it is not current (``carried``),
    marked so in its words."""
    days_late = follow_up.pop("days_late", None)
    event = follow_up.pop("event", None)
    summary = row["summary"]
    if today is not None:
        seen = _moment(row["last_seen"])
        summary, event, days_late = _relative(
            summary, event, days_late,
            po_status.business_today(seen) if seen is not None else None, today)
    if carried is not None:
        words = CARRIED_WORDS[carried["reason"]].format(
            checked=_at_words(carried.get("last_checked")) or "an earlier run",
            failed=_at_words(carried.get("check_failed_at")) or "the last run")
        summary = pg._clip(summary, pg.MAX_TEXT - len(words) - 3) + " (" + words + ")"
    caveat = follow_up.pop("promise_caveat", None)
    if caveat is not None:
        unless = caveat.get("unless") if isinstance(caveat, dict) else None
        # a reader who may not read every audience a promise sits in (or a caveat this
        # engine cannot read) is told a later promise may exist; the audiences are never
        # shown
        if not isinstance(unless, list) or caveat.get("always") is not False \
                or not set(unless) <= set(audiences):
            summary = pg._clip(summary, pg.MAX_TEXT - len(PROMISE_CAVEAT_TEXT)) \
                + PROMISE_CAVEAT_TEXT
            event = {**(event if isinstance(event, dict) else {}), "caveat": PROMISE_CAVEAT}
    if isinstance(event, dict) and isinstance(event.get("amount"), dict):
        amount = event["amount"]
        handle = amount.get("handle")
        needed = {h for h in (handle, amount.get("currency_handle")) if h}
        if handle is not None and (not needed <= set(citations) or amount.get(
                "markings", UNREADABLE) not in set(allowed)):
            event.pop("amount")   # a figure this reader may not open is never shown
        else:
            scope = amount.pop("suspect_scope", None)
            plain = amount.pop("suspect_plain", None)
            if scope is not None and (scope.get("audience") not in set(audiences)
                                      or scope.get("markings") not in set(allowed)):
                amount["suspect"] = plain   # its peers' scope is not this reader's: no band
            lines = amount.get("lines")
            if isinstance(lines, dict):
                hidden = lines.get("verdict") != "unpriced" and (
                    lines.get("handles") is None
                    or not set(lines["handles"]) <= set(citations)
                    or lines.get("markings", UNREADABLE) not in set(allowed))
                # a line this reader may not open: nothing its lines say is stated, only
                # that the total was not checked against them
                amount["lines"] = {"verdict": "not_open"} if hidden else {
                    k: v for k, v in lines.items() if k in ("verdict", "lines_sum")}
    if isinstance(event, dict) and isinstance(event.get("chases"), dict):
        # our payment chases of an overdue invoice (``proactive_mail_pains.chase_fold``):
        # counted of only the messages this reader may open, never a handle shown
        mine = [m for m in event.pop("chases").get("messages") or ()
                if isinstance(m, dict) and m.get("handle") in set(citations)]
        if mine:
            last = max(m.get("sent") or "" for m in mine)
            summary = pg._clip(f"{summary}; chased {len(mine)} time"
                               f"{'' if len(mine) == 1 else 's'}, last on {last}")
            event["chases"] = {"count": len(mine), "last": last,
                               "messages": [{k: m.get(k) for k in ("id", "sent", "subject")}
                                            for m in mine]}
    shown_by = follow_up.pop("confirmation_cites", None)
    if isinstance(shown_by, list) and not set(shown_by) & set(citations):
        follow_up.pop("confirmation", None)   # a confirmation this reader may not open
    draft = follow_up.get("draft")
    if isinstance(draft, dict) and not set(draft.get("citations") or ()) <= set(citations):
        follow_up.pop("draft")   # a fact it states is not this reader's to open
    return {"id": row["gap_id"], "kind": row["kind"],
            "object_id": row["object_id"], "object_type": row["object_type"],
            "summary": summary, "citations": citations,
            "detected_by": "rule", "state": state,
            **({"carried": dict(carried)} if carried is not None else {}),
            "first_seen": row["first_seen"], "last_seen": row["last_seen"],
            **({"days_late": days_late} if days_late is not None else {}),
            **({"event": event} if isinstance(event, dict) and event else {}),
            "follow_up": follow_up}


def _at_words(stamp: Any) -> str | None:
    from .briefing import at_words
    return at_words(stamp) if isinstance(stamp, str) else None


def acknowledge(inbox: Any, store: Any, gap_id: str, *, reader: str,
                may_read: Callable[[str], bool], held: Iterable[str],
                decide: Callable[[Sequence[str]], Mapping[str, Any]],
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
                ) -> dict[str, Any]:
    """Record that this reader saw an open gap; a gap it may not see is refused alike
    whether it exists or not."""
    path = Path(inbox)
    if not isinstance(gap_id, str) or not path.is_file():
        raise PermissionError("gap_not_visible")
    with closing(_attach(path, Path(store), write=True)) as conn:
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks}
        rows = conn.execute(
            "SELECT g.* FROM gaps g WHERE g.gap_id = :id AND g.state = 'open' AND "
            + _OBJECT_VISIBLE.format(o="g.object_id"), {**params, "id": gap_id}).fetchall()
        rows = [row for row in rows if _owned(json.loads(row["follow_up"]), row["kind"], reader)]
        if not _visible(conn, rows, params, decide):
            raise PermissionError("gap_not_visible")
        at = clock().isoformat()
        conn.execute("INSERT OR IGNORE INTO acks VALUES (?, ?, ?)", (gap_id, reader, at))
    return {"id": gap_id, "state": "acknowledged"}


def hidden_counts(inbox: Any, store: Any, *, kinds: Sequence[str],
                  may_read: Callable[[str], bool], held: Iterable[str],
                  decide: Callable[[Sequence[str]], Mapping[str, Any]]) -> dict[str, int]:
    """Per kind, the open gaps this reader may NOT see (its subject, a citation group or a
    required grant withheld): a count, never an id, a subject or a summary. No inbox: {}."""
    off = alert_kinds.off_gap_kinds()   # an off type is never counted to a reader either
    kinds = [k for k in kinds if k not in off]
    path = Path(inbox)
    if not path.is_file():
        return {}
    with closing(_attach(path, Path(store))) as conn:
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks, "kinds": json.dumps(list(kinds))}
        served = _served_digest(conn, store)
        current, _ = _current_since(conn, served[0] if served else None)
        # a gap of older data than the store served is shown to no one (``read_inbox``):
        # it is not counted as one hidden from this reader either
        # a person's own mail (``OWNER_KINDS``) is nobody else's: never counted as hidden
        total = Counter(row["kind"] for row in conn.execute(
            "SELECT kind, detector, last_seen FROM gaps WHERE state = 'open' AND kind IN "
            "(SELECT value FROM json_each(:kinds))", params) if _of_served(row, current)
            and row["kind"] not in _OWNER_KINDS)
        rows = [row for row in conn.execute(
            "SELECT g.* FROM gaps g WHERE g.state = 'open' AND g.kind IN (SELECT value FROM "
            "json_each(:kinds)) AND " + _OBJECT_VISIBLE.format(o="g.object_id"),
            params).fetchall() if _of_served(row, current)]
        seen = Counter(row["kind"] for row, _ in _visible(conn, rows, params, decide)
                       if row["kind"] not in _OWNER_KINDS)
    return {kind: total[kind] - seen[kind] for kind in sorted(total)
            if total[kind] - seen[kind] > 0}


UNJUDGED_SHOWN = 200   # records of one check's unjudged list read per page at most


class UnknownCheck(ValueError):
    """A check or reason no rule has (``unjudged_records``): refused, nothing read."""


def unjudged_records(inbox: Any, store: Any, *, rule: str, reason: str | None = None,
                     may_read: Callable[[str], bool], held: Iterable[str],
                     offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """The records one rule left unjudged that this reader is counted (``_rule_coverage``'s
    ``not_evaluated``, per ``reason`` its ``not_evaluated_by_reason``): each a subject of a
    skip whose whole cause the reader may read, so the list never names or hints at a
    record the count leaves out, and its ``total`` is that count. ``complete`` is false
    when the rule left a skip no record stands for (its count is unknown to every reader).
    An unknown rule or a malformed reason is refused (``UnknownCheck``); no inbox or no
    recorded causes: nothing listed, not complete."""
    if rule not in DETECTORS:
        raise UnknownCheck("rule_unknown")
    if reason is not None and (not isinstance(reason, str)
                               or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason)):
        raise UnknownCheck("reason_unknown")
    offset = max(0, int(offset))
    limit = max(0, min(int(limit), UNJUDGED_SHOWN))
    out: dict[str, Any] = {"rule": rule, "reason": reason, "total": 0, "records": [],
                           "complete": False}
    path = Path(inbox)
    if not path.is_file():
        return out
    with closing(_attach(path, Path(store))) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"unjudged_causes", "rule_coverage"} <= tables \
                or (reason is not None and "unjudged_reasons" not in tables):
            return out
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks, "rule": rule, "reason": reason}
        row = conn.execute("SELECT unattributed FROM rule_coverage WHERE detector = :rule",
                           params).fetchone()
        if row is None:   # never run with per-skip causes: no count, so no list
            return out
        out["complete"] = int(row[0] or 0) == 0
        hidden = ("SELECT c.skip FROM unjudged_causes c WHERE c.detector = :rule AND NOT "
                  "(c.markings IN (SELECT value FROM json_each(:marks)) AND "
                  + _OBJECT_VISIBLE.format(o="c.object_id") + ")")
        shown = ("SELECT DISTINCT u.object_id FROM unjudged_causes u "
                 + ("JOIN unjudged_reasons r ON r.detector = u.detector AND r.skip = u.skip "
                    "AND r.reason = :reason " if reason is not None else "")
                 + f"WHERE u.detector = :rule AND u.subject = 1 AND u.skip NOT IN ({hidden})")
        out["total"] = conn.execute(f"SELECT count(*) FROM ({shown})", params).fetchone()[0]
        out["records"] = [r[0] for r in conn.execute(
            f"{shown} ORDER BY u.object_id LIMIT :limit OFFSET :offset",
            {**params, "limit": limit, "offset": offset})]
    return out




def last_visit(inbox: Any, reader: str) -> str | None:
    """When this reader last marked its briefing seen (``record_visit``); None: never."""
    path = Path(inbox)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                        "'visits'").fetchone() is None:
            return None
        row = conn.execute("SELECT at FROM visits WHERE reader = ?", (reader,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def record_visit(inbox: Any, reader: str, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    """Mark this reader's briefing seen now (the Workbench's own inbox; nothing else is
    written). An inbox the engine never wrote is refused."""
    path = Path(inbox)
    if not path.is_file():
        raise FileNotFoundError("gap_inbox_missing")
    at = clock().isoformat()
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != INBOX_SCHEMA:
            raise ValueError("gap_inbox_schema_mismatch")
        conn.execute(_VISITS_DDL)
        conn.execute("INSERT INTO visits (reader, at, previous) VALUES (?, ?, NULL) ON CONFLICT "
                     "(reader) DO UPDATE SET previous = visits.at, at = excluded.at",
                     (reader, at))
    finally:
        conn.close()
    return at


# The markings the engine's own reader holds in its tool calls: an invoiced order's margin
# rests on finance-marked money (BC and Sage revenue and cost), so without it every margin
# is withheld and ``negative_margin`` never decides one. Its findings cite each figure at
# its value's markings, so a reader who does not hold finance never sees the loss
# (``read_inbox`` trims per reader).
ENGINE_MARKINGS = ("finance",)


def engine_marking_grants(actor: Any) -> dict[str, list[str]]:
    """``marking_grants`` of the engine's case sessions: ``ENGINE_MARKINGS`` held by the
    run principal (``actor``'s principals) only."""
    return {marking: sorted(actor.principals) for marking in ENGINE_MARKINGS}


def refresh(runtime: Any, investigator: Any, audience: Any, projection: Path, as_of: date,
            *, captures: Path | None, scan_coverage: bool = True,
            budget: Budget | None = None,
            clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
            vendor_bound: int | None = None, pool: Any = None, threads: int | None = None,
            detector_seconds: Mapping[str, float] | None = None,
            only: Sequence[str] | None = None) -> dict[str, Any]:
    """The full scheduled run over ``projection`` into its inbox, as ``investigator``: the
    customer PO coverage scan of the runtime's mirror (``scan_coverage``; its order
    statuses ``absent_in_this_source``) and a case session over the projection and the
    state directory's captures as the tool caller (``orders_at_risk``, ``card_rules``).
    ``investigator`` must hold every source the engine's verdicts rest on (the host's
    capture principal with its capture audience: Portal and BC, ``host.engine_readers``);
    ``orders_at_risk`` is ``blocked`` (``reader_lacks_lateness_access``) when it does not.
    The inbox stores each gap with its citations' audiences; ``read_inbox`` trims it per
    reader. Shared by ``main`` and the host's ``after_build(scheduled=)``. The store-only
    detectors run side by side (``threads``, default ``detector_threads()``), each detector
    in its own window (``DETECTOR_SECONDS``, ``detector_seconds`` overriding some)."""
    from . import case_workspace_live as live
    from . import customer_po_coverage

    def coverage() -> Mapping[str, Any]:
        return customer_po_coverage.coverage(runtime.mirror, investigator,
                                             statuses=("absent_in_this_source",))

    # no SharePoint warm-up beside the rules: a batch reader, not one answering questions
    session = live._Session(runtime, investigator, audience, projection, captures=captures,
                            marking_grants=engine_marking_grants(investigator), warm=False)
    try:
        # the reader the engine's verdicts are read as (the capture principal, granted
        # what it captured: ``host.engine_readers``): every reader's inbox and briefing
        # are trimmed from them, so a rule it cannot judge for lack of a grant says so.
        # Decided over the units the store holds (the keys it checks are stored ones):
        # no live BC company listing is requested for it.
        policy = session.host.stored_policy(investigator)
        return run(projection, inbox_path(projection), as_of,
                   coverage=coverage if scan_coverage else None, call=session,
                   budget=budget, clock=clock, vendor_bound=vendor_bound,
                   reader_may_read=lambda key: policy.may_read(investigator, key),
                   pool=pool, threads=threads, detector_seconds=detector_seconds, only=only)
    finally:
        session.close()


def after_build(store_path: Any, *, as_of: date | None = None,
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                scheduled: Callable[[date, Budget], Mapping[str, Any]] | None = None,
                budget_seconds: float | None = None,
                monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The host's hook after a projection build, never raising; counts and enums only.
    ``scheduled(as_of, budget)``: the full run (``refresh``) within a ``budget_seconds``
    budget; without it the store-only detectors (no mirror scan, no cards). Judged on
    ``as_of``, else the business day (ET) of ``clock()``."""
    try:
        if scheduled is None:
            found = run(store_path, inbox_path(store_path), as_of, clock=clock)
        else:
            found = scheduled(po_status.business_today(clock()) if as_of is None else as_of,
                              Budget(REFRESH_BUDGET_SECONDS if budget_seconds is None
                                     else budget_seconds, monotonic,
                                     reserve=AMOUNTS_RESERVE_SECONDS))
    except Exception as error:  # noqa: BLE001 - detection never fails a capture cycle
        return {"state": "failed", "error": type(error).__name__}
    return {"state": "ran", "gaps_found": found["gaps_found"], "detectors": found["detectors"],
            **({"detector_reasons": found["detector_reasons"]}
               if found.get("detector_reasons") else {})}


# --------------------------------------------------------------------------- live command

def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None, graph_sender: Any = None) -> int:
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor
    from operator_api.workspace.object_workspace import HostAccess

    from . import case_workspace_live as live

    parser = argparse.ArgumentParser(prog="python3 -m operating.proactive_engine",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True,
                        help="the investigator's principals (repeat)")
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--reader", action="append", default=[], metavar="P1,P2",
                        help="another reader whose inbox is counted (repeat)")
    parser.add_argument("--host-config", type=Path, default=None,
                        help="run one host capture cycle into --state-dir first")
    parser.add_argument("--live-source-audience", type=Path, default=None)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        audience = AudienceMap.from_file(args.mirror_audience)
        readers = [live._reader(text, args.tenant, args.clearance) for text in args.reader]
    except (OSError, ValueError) as error:
        parser.error(f"invalid input: {type(error).__name__}")
    if audience.tenant != args.tenant:
        parser.error("mirror audience tenant must equal --tenant")
    as_of = args.as_of or po_status.business_today()
    summary: dict[str, Any] = {"schema": SCHEMA, "evidence": live.MIRROR_EVIDENCE["label"],
                               "as_of": as_of.isoformat(), "capture": None}
    if args.host_config is not None:
        code, summary["capture"] = live._host_cycle(args, env, host_options or {})
        if code != 0:
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    investigator = Actor(args.tenant, frozenset(args.principal), args.clearance)
    readers_live = None
    if args.live_source_audience is not None:
        from .case_runtime import build_live_readers
        from .ingestion import IngestError, load_live_audience
        try:
            readers_live = build_live_readers(
                load_live_audience(args.live_source_audience, env), env,
                graph_sender=graph_sender, bc_sender=(host_options or {}).get("sender"))
        except IngestError as error:
            parser.error(f"live source audience is invalid: {error.code}")
    runtime = live.offline_runtime(args.state_dir, args.mirror_audience, args.tenant,
                                   live=readers_live, environ=env)
    projection = live.projection_file(args.state_dir, args.tenant)
    inbox = inbox_path(projection)
    rendered = []
    try:
        live.project_state(args.state_dir, projection, tenant=args.tenant)
        summary["blocked_upstream"] = live.upstream_parts(live.portal_upstream(args.state_dir))
        summary.update(refresh(runtime, investigator, audience, projection, as_of,
                               captures=args.state_dir))
        for actor in (investigator, *readers):
            session = live._Session(runtime, actor, audience, projection,
                                    captures=args.state_dir)
            try:
                access = HostAccess(session.host, runtime, actor)
                with access.request():
                    may_read, held = access.grant()
                    found = read_inbox(inbox, projection, reader=reader_key(actor),
                                       may_read=may_read, held=held, decide=access.decide)
            finally:
                session.close()
            rendered.append({"reader": sorted(actor.principals),
                             "investigator": actor is investigator,
                             "scanned": found["scanned"], "shown": len(found["gaps"]),
                             "gaps_by_kind": dict(sorted(Counter(
                                 g["kind"] for g in found["gaps"]).items()))})
    finally:
        runtime.close()
    summary["readers"] = rendered
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 1 if "failed" in summary.get("detectors", {}).values() else 0


if __name__ == "__main__":
    sys.exit(main())
