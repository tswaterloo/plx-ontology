"""The proactive engine's settings: rule names, priorities, time budgets, thread caps and the
link and type names the rules read (``operating.proactive_engine``)."""

from __future__ import annotations

from pathlib import Path

from . import po_status

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
             "mc_task_stuck", "mc_approval_pending", "portal_approval_pending",
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
                    "mc_task_stuck": 300.0,
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
# another (the tool-call rules on the run's own thread) split what is left of the
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
# The
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
