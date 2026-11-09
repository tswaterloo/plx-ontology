"""Per-person briefing: the proactive events one reader may see, routed to them, ranked by
impact, each with why and a suggested next action, and a daily digest preview.

Inputs are what the scheduled engine (``operating.proactive_engine``) stored for the
current generation and what the reader's own grant shows of it: ``read_inbox`` (every
gap trimmed for the reader: subject and every citation group visible, at markings held,
passing the per-item decision, and a verdict on what a source does not hold only with
the grants it needs) and ``hidden_counts`` (the open events of each kind the reader may
NOT see: counted, never named). Nothing here reads a source, decides a permission again
more widely, writes to a business system or sends anything.

* **Event types** (``EVENT_TYPES``): each gap kind maps to one event type; a kind no type
  names is not an event (it stays on the needs-attention list). An unknown kind is never
  guessed into a type.
* **Routing**: an event goes to the departments its type names (``departments``) and the
  Portal department codes the gap's follow-up names as who can answer (the one resolver,
  ``operating.answerers``). A reader is routed an event when one of its departments (the
  Portal-confirmed memberships with the Entra department's code,
  ``EmployeeEntitlement.routing_departments``) is among them; Executive or a Portal admin
  role (ADMIN or above) is routed every type. A reader with no department on record is routed nothing
  (``no_department_on_record``): the events it may see are counted as other teams'.
  Routing never widens what a reader sees: it only chooses among visible events.
* **Yours** (``operating.involvement``): an event about a record the reader is involved
  in (named on it, mentioned in their own mailbox, or its supplier or customer writes to
  them), read as the reader from the ontology's links and their employee identity, is in
  their briefing even when not routed to their department, and ranked before the rest.
  It only chooses among the events the reader's own trimmed inbox holds.
* **Ranking** (``RANK_BASIS``), over what the reader sees only: the reader's own first,
  then customer orders affected,
  then money involved, then what the latest run found new, then days (the most late,
  slipped or waiting; the FEWEST to expiry, ``COUNTDOWN_HORIZON``), then the most recently
  first seen; acknowledged items after the others. Money
  is a cited amount the reader may open, placed within its own currency (its rank among
  that currency's amounts): amounts are never converted or compared across currencies,
  and one stray currency does not switch money off for the rest (currencies read by the
  ontology's one table: the Portal's ``CAD$`` is ``CAD``; an unknown code is unranked).
  A Portal total that adds its lines' unit prices is stated as such and never ranked. A
  total
  ``operating.suspect_money`` finds implausible (against its lines, the item's usual
  price, the vendor's or customer's usual order, or every order in that currency) is never
  ranked by: its item is left out (``real_impact``, counted in ``left_out``), with any
  order put at risk only by it. Ties keep the inbox's order.
* **One event, one item** (``real_impact``): a late PO's impact item stands for the orders
  at risk it names and for its plain impact when it states the margin at stake.
* **Late is Home's late**: a purchase order of the one late list's kinds is late when the
  status rule states its days late (Home's ``late_total``); due today it is "due today".
  Acknowledged items stay listed (marked), so the counts reconcile with Home's.
* **Size**: the first ``TOP_ITEMS`` are shown one by one, every other item sits in its
  type's group with the group's count; nothing is cut silently (a read that stopped at
  ``MAX_READ`` says its counts are "at least").
* **Checks**: each check behind the event types is named with this run's state ("vendor
  replies: not checked this run", "partly checked (time ran out)"), and whether a
  recorded run over this data ever found something (the inbox's ``detector_history``;
  never a fixed list).
* **Totals and their lines**: what a record's own lines say of its total (the Portal's
  unit-price convention, a disagreement and its sum) is stated only to a reader who may
  open every line it rests on; any other reader is told only "total (not checked against
  its lines)", never that they disagree. Neither is ranked.
* **Hidden counts** (``HIDDEN_POLICY``): events routed to the reader that it may not open
  are given as a count per type, never an identifier, amount, date or text. A count says
  only that so many exist somewhere the reader cannot open; that is accepted for each
  type except the vendor-mail ones (``vendor_no_reply``, ``vendor_terms_unapplied``),
  which may rest on a mailbox whose owner is excluded from the Workbench: for those not
  even a count is given (nor whether the check ever found one). Per type: late and
  due-today POs, slipped dates, accepted quotes, quantity mismatches, customer orders at
  risk (business records the reader's team works on: count accepted); lots near expiry
  and batches on quality hold (QC records: count accepted); negative margins (finance
  figures: count accepted, the amounts never shown); vendor mail: no count.
* **Since your last visit**: an event first seen after the reader's previous visit
  (``proactive_engine.last_visit``); ``new_this_generation``: first seen by the latest run.
* **Next action**: a sentence per event type from its own facts, in the words of the
  reader's department when it acts on the type differently (``NEXT_ACTIONS_BY_DEPARTMENT``:
  Quality decides a hold, Purchasing chases the supplier of an order at risk, Sales tells
  the customer); an Executive or admin reader gets the general one. A suggestion only.
* **Today in one line** (``today_line``): what is new (since the last visit, else in the
  latest run) by type, worst first, else what is still open with the soonest expiry, and a
  count of what the reader can't open; the digest opens with it.
* **Digest preview** (``digest_text``): the text a daily digest would say to this reader.
  Preview only: nothing is sent (sending needs a permission the Workbench does not hold).

Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import alert_kinds, copy_gate
from .copy_gate import say_date

SCHEMA = "plx-briefing/v2"
TOP_ITEMS = 7        # shown one by one; the rest are grouped by type with their counts
MAX_ITEMS = 2000     # the ranked items a briefing holds (each group pages through them)
MAX_READ = 2000      # inbox entries read per briefing (the inbox keeps its own order)
DIGEST_ITEMS = 7
EVERYTHING = frozenset({"EXEC"})

# The one late list (``mcp_server.LATE_PO_KINDS``, Home's ``late_total``): a purchase order
# the status rule holds late (``days_late`` stated) is late; one of those kinds due today
# (no ``days_late`` yet) is "due today", never late. A PO with a late BC line
# (``late_order``) is not late unless its own status rule says so.
LATE_KINDS = ("late_po", "unconfirmed_po")
# The kinds whose money figure is the event itself (an amount anomaly): the only items a
# suspect figure removes. No current event type is one.
AMOUNT_EVENT_KINDS: frozenset[str] = frozenset()
# What ``proactive_engine._with_amounts`` states when the plausibility check did not run.
NOT_CHECKED = "its plausibility could not be checked"
LATE_TYPES = ("po_late", "po_due")   # the late list's event types (one late rule)

EVENT_TYPES: dict[str, dict[str, Any]] = {
    "po_late": {"kinds": LATE_KINDS, "label": "Late purchase order",
                "departments": ("PROC",), "check": ("late_or_unconfirmed_po",),
                "seen": tuple(f"{k}:late" for k in LATE_KINDS)},
    "po_due": {"kinds": (), "label": "Due today, not confirmed", "departments": ("PROC",),
               "check": ("late_or_unconfirmed_po",),
               "seen": tuple(f"{k}:not_late" for k in LATE_KINDS)},
    # open with no requested-by date recorded: never late, listed as its own group
    "po_undated": {"kinds": ("undated_po",), "label": "Open, no due date recorded",
                   "departments": ("PROC",), "check": ("late_or_unconfirmed_po",)},
    "eta_slipped": {"kinds": ("eta_slipped",), "label": "Delivery date slipped",
                    "departments": ("PROC", "OPS"), "check": ("eta_slipped",)},
    "vendor_no_reply": {"kinds": ("vendor_no_reply",), "label": "Supplier has not replied",
                        "departments": ("PROC",), "check": ("vendor_no_reply",),
                        "hidden": "none"},
    "vendor_terms_unapplied": {"kinds": ("vendor_terms_unapplied",),
                               "label": "Supplier terms not applied",
                               "departments": ("PROC",), "check": ("vendor_terms_unapplied",),
                               "hidden": "none"},
    "negative_margin": {"kinds": ("negative_margin",), "label": "Order lost money on item cost",
                        "departments": ("FIN", "SALES", "ACCTMGMT"),
                        "check": ("negative_margin",)},
    # its days are the days LEFT to expiry: fewer is more urgent (``COUNTDOWN``)
    "lot_near_expiry": {"kinds": ("lot_near_expiry",), "label": "Lot near expiry",
                        "departments": ("QUALITY", "QA", "QC", "WH", "PROD"),
                        "check": ("lot_attention",), "days": "left"},
    "lot_on_hold": {"kinds": ("lot_on_hold",), "label": "Batch on quality hold",
                    "departments": ("QUALITY", "QA", "QC", "PROD"), "check": ("lot_attention",)},
    "quote_accepted_no_order": {"kinds": ("quote_accepted_no_order",),
                                "label": "Accepted quote with no order",
                                "departments": ("SALES", "ACCTMGMT"),
                                "check": ("accepted_quote_no_order",)},
    "quantity_mismatch": {"kinds": ("po_receipt_quantity_mismatch",
                                    "invoice_quantity_mismatch"),
                          "label": "Quantity mismatch", "departments": ("PROC", "WH", "FIN"),
                          "check": ("po_receipt_quantity_mismatch",
                                    "invoice_quantity_mismatch")},
    "customer_order_at_risk": {"kinds": ("order_at_risk", "po_impact", "po_impact_margin"),
                               "label": "Late PO hits a customer order",
                               "departments": ("ACCTMGMT", "SALES", "OPS", "PROC"),
                               "check": ("orders_at_risk",)},
    # a customer order open past its own required date (whatever holds it up)
    "customer_order_late": {"kinds": ("late_customer_order",),
                            "label": "Customer order past its required date",
                            "departments": ("ACCTMGMT", "SALES", "OPS"),
                            "check": ("late_customer_order",)},
    # the business alerts (``operating.proactive_business``); the receivable and the cost
    # jump rest on finance-marked figures, so only a reader holding finance opens them
    "stock_out_risk": {"kinds": ("stock_out_risk",), "label": "Stock cannot cover open orders",
                       "departments": ("OPS", "PROC", "SALES", "WH"),
                       "check": ("stock_out_risk",)},
    "overdue_receivable": {"kinds": ("overdue_receivable",),
                           "label": "Customer invoice overdue",
                           "departments": ("FIN", "ACCTMGMT"),
                           "check": ("overdue_receivable",), "hidden": "none"},
    "unit_cost_jump": {"kinds": ("unit_cost_jump",), "label": "Purchase cost jumped",
                       "departments": ("PROC", "FIN"), "check": ("unit_cost_jump",),
                       "hidden": "none"},
    # the operations alerts (``operating.proactive_ops``): stalled work, ageing balances
    # (finance-marked), late or damaged production, Teams asks left unanswered (resting on
    # a person's chat: a reader who may not open one is never told it exists)
    "mc_task_overdue": {"kinds": ("mc_task_overdue",), "label": "Mission Control task overdue",
                        "departments": ("OPS", "PROD"), "check": ("mc_task_overdue",)},
    "mc_task_stuck": {"kinds": ("mc_task_stuck",), "label": "Mission Control task stuck",
                      "departments": ("OPS", "PROD"), "check": ("mc_task_stuck",)},
    "mc_approval_pending": {"kinds": ("mc_approval_pending",),
                            "label": "Approval waiting for a decision",
                            "departments": ("OPS", "FIN", "PROC"),
                            "check": ("mc_approval_pending",)},
    "planner_task_overdue": {"kinds": ("planner_task_overdue",),
                             "label": "Planner task overdue",
                             "departments": ("OPS", "PROD", "PROC", "SALES", "WH"),
                             "check": ("planner_task_overdue",), "hidden": "none"},
    "aged_receivable_worsening": {"kinds": ("aged_receivable_worsening",),
                                  "label": "Customer balance older",
                                  "departments": ("FIN", "ACCTMGMT"),
                                  "check": ("aged_balance_worsening",), "hidden": "none"},
    "aged_payable_worsening": {"kinds": ("aged_payable_worsening",),
                               "label": "Supplier balance older",
                               "departments": ("FIN", "PROC"),
                               "check": ("aged_balance_worsening",), "hidden": "none"},
    "production_late": {"kinds": ("production_late",), "label": "Production past due",
                        "departments": ("PROD", "OPS"), "check": ("production_late",)},
    "assembly_damage_spike": {"kinds": ("assembly_damage_spike",),
                              "label": "Assembly damage spiked",
                              "departments": ("PROD", "QUALITY", "QA", "QC", "PROC"),
                              "check": ("assembly_damage_spike",)},
    "teams_unanswered_ask": {"kinds": ("teams_unanswered_ask",),
                             "label": "Teams question unanswered",
                             "departments": ("OPS", "SALES", "PROC", "FIN", "PROD", "WH",
                                             "ACCTMGMT", "QUALITY"),
                             "check": ("teams_unanswered_ask",), "hidden": "none"},
    # the chief-executive signals (``operating.proactive_ceo``); the demand drop and the
    # margin erosion state finance-marked figures, so only a reader holding finance opens them
    "customer_demand_drop": {"kinds": ("customer_demand_drop",),
                             "label": "Customer ordering much less",
                             "departments": ("SALES", "ACCTMGMT", "FIN"),
                             "check": ("customer_demand_drop",), "hidden": "none"},
    "promise_date_slip": {"kinds": ("promise_date_slip",),
                          "label": "Customer orders missing their promised date",
                          "departments": ("OPS", "SALES", "ACCTMGMT"),
                          "check": ("promise_date_slip",)},
    "margin_erosion": {"kinds": ("margin_erosion",), "label": "Margin eroding",
                       "departments": ("FIN", "SALES", "PROC"),
                       "check": ("margin_erosion",), "hidden": "none"},
    "supplier_reliability": {"kinds": ("supplier_reliability",),
                             "label": "Supplier delivering later than before",
                             "departments": ("PROC", "OPS"),
                             "check": ("supplier_reliability",)},
    # the order-to-cash and damage alerts (``operating.proactive_business``); a shipment no
    # invoice bills rests on an absence, so it shows only to a reader granted every invoice
    # audience (``REQUIRES_GRANT``)
    "shipped_not_invoiced": {"kinds": ("shipped_not_invoiced",),
                             "label": "Shipped but not invoiced",
                             "departments": ("FIN", "ACCTMGMT", "OPS"),
                             "check": ("shipped_not_invoiced",)},
    "duplicate_customer_order": {"kinds": ("duplicate_customer_order",),
                                 "label": "Customer order entered twice",
                                 "departments": ("SALES", "ACCTMGMT", "OPS"),
                                 "check": ("duplicate_customer_order",)},
    "recurring_component_damage": {"kinds": ("recurring_component_damage",),
                                   "label": "Item damaged repeatedly in assembly",
                                   "departments": ("QUALITY", "QA", "QC", "PROC", "OPS"),
                                   "check": ("recurring_component_damage",)},
    # the two largest pains (``operating.proactive_chasers``): a customer chasing where an
    # order is rests on mail and on no shipment being recorded (``REQUIRES_GRANT``), so
    # what a reader may not open is never counted to it
    "customer_chasing_order": {"kinds": ("customer_chasing_order",),
                               "label": "Customer chasing an order",
                               "departments": ("SALES", "ACCTMGMT", "CS", "OPS"),
                               "check": ("customer_chase",), "hidden": "none"},
    # pains found in the mail (``operating.proactive_mail_pains``): an expedite and a
    # shortage rest on mail and on no shipment or receipt being recorded
    # (``REQUIRES_GRANT``); a supplier's payment chase is finance-marked
    "expedite_unanswered": {"kinds": ("expedite_unanswered",),
                            "label": "Expedite with no answer",
                            "departments": ("OPS", "PROC", "SALES", "CS"),
                            "check": ("expedite_requests",), "hidden": "none"},
    "supplier_shortage_open_orders": {"kinds": ("supplier_shortage_open_orders",),
                                      "label": "Supplier shortage hits open orders",
                                      "departments": ("PROC", "OPS", "SALES"),
                                      "check": ("supplier_shortage",), "hidden": "none"},
    "supplier_chasing_payment": {"kinds": ("supplier_chasing_payment",),
                                 "label": "Supplier chasing us for payment",
                                 "departments": ("FIN", "PROC"),
                                 "check": ("payment_chase",)},
    "tell_before_they_ask": {"kinds": ("tell_before_they_ask",),
                             "label": "Tell the customer before they ask",
                             "departments": ("SALES", "ACCTMGMT", "CS", "OPS"),
                             "check": ("customer_chase",), "hidden": "none"},
    # promises we made (``operating.proactive_commitments``): resting on no shipment,
    # invoice or later message being recorded (``REQUIRES_GRANT``)
    "commitment_due": {"kinds": ("commitment_due",), "label": "Promise due",
                       "departments": ("SALES", "ACCTMGMT", "CS", "OPS"),
                       "check": ("commitments",), "hidden": "none"},
    "commitment_missed": {"kinds": ("commitment_missed",), "label": "Promise missed",
                          "departments": ("SALES", "ACCTMGMT", "CS", "OPS"),
                          "check": ("commitments",), "hidden": "none"},
    # a person's own mail waiting on their reply: shown to that person only, for every
    # department (``proactive_engine.read_inbox``), never counted as hidden to another
    "needs_my_reply": {"kinds": ("needs_my_reply", "reply_owed"), "label": "Needs your reply",
                       "departments": (),
                       "check": ("needs_reply", "inbox_triage"), "hidden": "none"},
    # label and artwork changes (``operating.label_changes``): done with no approval, or
    # requested and not done (the tracked ones show on the item page only)
    "label_change": {"kinds": ("label_change_unapproved", "label_change_stale"),
                     "label": "Label or artwork change", "departments": (
                         "ARTWORK", "QA", "QC", "QUALITY", "PROD", "SALES", "ACCTMGMT"),
                     "check": ("label_changes",), "hidden": "none"},
    # an R&D or sample project with no activity for days (``operating.rd_projects``); its
    # lines are the board's, never the briefing's
    "rd_project": {"kinds": ("rd_project_idle",), "label": "R&D or sample project idle",
                   "departments": ("RND", "QUALITY", "QA", "QC", "SALES", "ACCTMGMT", "CS"),
                   "check": ("rd_projects",), "hidden": "none"},
    # mail threads waiting on someone (``operating.proactive_threads``): resting on no later
    # reply being recorded (``requires``: every audience of the conversation's copies)
    "approval_waiting": {"kinds": ("approval_waiting",), "label": "Approval waiting",
                         "departments": ("FIN", "PROC", "SALES", "QUALITY", "OPS",
                                         "ACCTMGMT", "PROD", "WH"),
                         "check": ("approvals_waiting",), "hidden": "none"},
    "chase_escalating": {"kinds": ("chase_escalating",), "label": "Chased repeatedly",
                         "departments": ("SALES", "ACCTMGMT", "CS", "OPS", "PROC", "FIN",
                                         "WH", "QUALITY"),
                         "check": ("chase_escalation",), "hidden": "none"},
    "quality_hold_aging": {"kinds": ("quality_hold_aging",),
                           "label": "Quality hold open too long",
                           "departments": ("QUALITY", "QA", "QC", "OPS"),
                           "check": ("quality_holds",)},
    "reject_trend": {"kinds": ("reject_trend",), "label": "Rejects rising",
                     "departments": ("QUALITY", "QA", "QC", "PROC"),
                     "check": ("quality_holds",)},
    # receivables and payables (``operating.proactive_cash``), every figure finance-marked
    "customer_paying_slower": {"kinds": ("customer_paying_slower",),
                               "label": "Customer paying more slowly",
                               "departments": ("FIN", "ACCTMGMT", "SALES"),
                               "check": ("customer_payment_behaviour",)},
    "early_pay_discount_missed": {"kinds": ("early_pay_discount_missed",),
                                  "label": "Early-pay discount missed",
                                  "departments": ("FIN", "PROC"),
                                  "check": ("early_pay_discounts",)},
    "duplicate_supplier_invoice": {"kinds": ("duplicate_supplier_invoice",),
                                   "label": "Supplier invoice entered twice",
                                   "departments": ("FIN", "PROC"),
                                   "check": ("duplicate_payables",)},
    "duplicate_supplier_payment": {"kinds": ("duplicate_supplier_payment",),
                                   "label": "Supplier paid twice",
                                   "departments": ("FIN", "PROC"),
                                   "check": ("duplicate_payables",)},
    "customer_credits_rising": {"kinds": ("customer_credits_rising",),
                                "label": "Customer credits and returns rising",
                                "departments": ("FIN", "ACCTMGMT", "SALES", "QA"),
                                "check": ("returns_and_credits",)},
    # operations (``operating.proactive_stock``): stock and value are finance-marked; a slow
    # item and an assembly shortfall rest on an absence (``REQUIRES_GRANT``)
    "slow_moving_stock": {"kinds": ("slow_moving_stock",), "label": "Stock not moving",
                          "departments": ("OPS", "WH", "PROC", "FIN"),
                          "check": ("slow_moving_stock",)},
    "expiring_lot_stock": {"kinds": ("expiring_lot_stock",),
                           "label": "Lot with stock expiring soon",
                           "departments": ("WH", "PROD", "QUALITY", "QA", "OPS"),
                           "check": ("expiring_lot_stock",), "days": "left"},
    "assembly_blocked_by_component": {"kinds": ("assembly_blocked_by_component",),
                                      "label": "Assembly order short of components",
                                      "departments": ("PROD", "PROC", "OPS"),
                                      "check": ("assembly_component_shortfall",),
                                      "days": "left"},
    "certificate_expiring": {"kinds": ("certificate_expiring",),
                             "label": "Certificate expiring soon",
                             "departments": ("QUALITY", "QA", "QC", "PROC"),
                             "check": ("certificate_expiry",), "days": "left"},
    "freight_cost_anomaly": {"kinds": ("freight_cost_anomaly",),
                             "label": "Freight bill far above usual",
                             "departments": ("FIN", "WH", "OPS"),
                             "check": ("freight_cost_anomaly",)},
    # what each customer pays for the same product (``operating.proactive_prices``),
    # finance-marked
    "price_below_typical": {"kinds": ("price_below_typical",),
                            "label": "Customer paying far below the usual price",
                            "departments": ("SALES", "ACCTMGMT", "FIN"),
                            "check": ("price_consistency",)},
    "customer_price_drop": {"kinds": ("customer_price_drop",),
                            "label": "Customer's price dropped",
                            "departments": ("SALES", "ACCTMGMT", "FIN"),
                            "check": ("price_consistency",)},
    "price_below_cost": {"kinds": ("price_below_cost",),
                         "label": "Customer price below cost",
                         "departments": ("SALES", "ACCTMGMT", "FIN"),
                         "check": ("price_consistency",)},
    # sales (``operating.proactive_sales``): the value is in the event, finance-marked; the
    # claim rests on no order being recorded (``REQUIRES_GRANT``)
    "priority_order_unfilled": {"kinds": ("priority_order_unfilled",),
                            "label": "Priority-listed order late or unscheduled",
                            "departments": ("SALES", "ACCTMGMT", "OPS"),
                            "check": ("priority_fill_unmet",)},
    "stale_quote": {"kinds": ("stale_quote",), "label": "Quote not converted to an order",
                    "departments": ("SALES", "ACCTMGMT", "OPS", "FIN"),
                    "check": ("stale_quote",)},
    # data quality (``operating.proactive_quality``): a duplicate is a proposal a person
    # confirms; a missing field rests on an absence (``REQUIRES_GRANT``)
    "duplicate_party_candidate": {"kinds": ("duplicate_party_candidate",),
                                  "label": "Customer or supplier entered twice",
                                  "departments": ("FIN", "SALES", "PROC", "OPS"),
                                  "check": ("duplicate_parties",)},
    "key_field_missing": {"kinds": ("key_field_missing",), "label": "Key field missing",
                          "departments": ("OPS", "FIN", "SALES", "PROC", "WH"),
                          "check": ("key_fields_missing",)},
    "twins_disagree": {"kinds": ("twins_disagree",), "label": "Systems disagree on a record",
                       "departments": ("OPS", "FIN", "SALES", "PROC"),
                       "check": ("twin_disagreement",)},
    # integrity (``operating.proactive_integrity``): finance-marked but the tasks; a blocked
    # account's open orders rest on no closure being recorded (``REQUIRES_GRANT``)
    "inventory_adjustment": {"kinds": ("inventory_adjustment",),
                             "label": "Stock written down by hand",
                             "departments": ("FIN", "WH", "OPS"),
                             "check": ("inventory_adjustment",)},
    "blocked_account_activity": {"kinds": ("blocked_account_activity",),
                                 "label": "Open orders with a blocked account",
                                 "departments": ("FIN", "SALES", "PROC"),
                                 "check": ("blocked_account_activity",)},
    "gl_posting_errors": {"kinds": ("gl_posting_errors",),
                          "label": "Ledger batch failed to post",
                          "departments": ("FIN",), "check": ("gl_posting_errors",)},
    "recurring_task_missed": {"kinds": ("recurring_task_missed",),
                              "label": "Recurring check missed a cycle",
                              "departments": ("QUALITY", "QA", "QC", "OPS"),
                              "check": ("recurring_task_missed",)},
    # sheet readings (``operating.proactive_sheets``): finance-marked
    "unapplied_remittance": {"kinds": ("unapplied_remittance",),
                             "label": "Remittance not applied",
                             "departments": ("FIN",), "check": ("cash_application",)},
    "short_payment": {"kinds": ("short_payment",), "label": "Customer paid short",
                      "departments": ("FIN", "SALES"), "check": ("cash_application",)},
    "statement_difference": {"kinds": ("statement_difference",),
                             "label": "Supplier statement differs from our books",
                             "departments": ("FIN", "PROC"),
                             "check": ("statement_reconciliation",)},
    "source_stale": {"kinds": ("source_stale",), "label": "Source not updated lately",
                     "departments": ("OPS", "IT", "FIN"), "check": ("stale_captures",),
                     "days": "old"},
    # an item whose forecast demand exceeds on hand plus inbound (``REQUIRES_GRANT``)
    "reorder_soon": {"kinds": ("reorder_soon",), "label": "Reorder soon",
                     "departments": ("PROC", "OPS", "WH"), "check": ("reorder_soon",)},
    # what the AI watcher raised from a cycle of changes (``operating.ai_watcher``), shown
    # only while its switch is on (``optional``: the type and its check are listed only
    # when the check reports a run): routed by the departments the item names, and never
    # counted for a reader who may not open it (it may rest on mail)
    "watcher_attention": {"kinds": ("watcher_attention",), "label": "Watcher finding",
                          "departments": (), "check": ("ai_watcher",), "hidden": "none",
                          "optional": True},
}
TYPE_OF_KIND = {kind: name for name, spec in EVENT_TYPES.items() for kind in spec["kinds"]}
EVENT_KINDS = tuple(TYPE_OF_KIND)
# What may be said of the events of a type routed to a reader that it may NOT open
# (``HIDDEN_POLICY``): "count" (how many, never which, what or where) or "none" (not
# even that some exist). See the module doc.
# Event types of a person's own mail (``proactive_engine.read_inbox`` shows them to their
# owner only): always the reader's own, routed whatever the department.
OWN_MAIL = frozenset({"needs_my_reply", "reply_owed"})
HIDDEN_POLICY = {name: spec.get("hidden", "count") for name, spec in EVENT_TYPES.items()}
# A type whose days count down to a deadline (days left): its urgency is the horizon the
# check looks ahead (``proactive_events.EXPIRY_DAYS``) less the days left, so a lot expiring
# tomorrow ranks before one expiring in a month; every other type's days are days already
# passed (late, slipped, waiting), more first.
COUNTDOWN_HORIZON = 30

# The checks behind the event types, in words, as the briefing names them.
CHECK_WORDS = {"late_or_unconfirmed_po": "late purchase orders",
               "eta_slipped": "slipped delivery dates",
               "vendor_no_reply": "supplier replies",
               "vendor_terms_unapplied": "supplier terms in replies",
               "negative_margin": "order margins",
               "lot_attention": "lots near expiry and quality holds",
               "accepted_quote_no_order": "accepted quotes",
               "po_receipt_quantity_mismatch": "received against ordered quantities",
               "invoice_quantity_mismatch": "invoiced against received quantities",
               "orders_at_risk": "customer orders hit by late purchase orders",
               "late_customer_order": "customer orders past their required date",
               "ai_watcher": "new mail, chat and record changes",
               "customer_payment_behaviour": "customers paying more slowly",
               "early_pay_discounts": "early-pay discounts missed",
               "duplicate_payables": "supplier invoices and payments entered twice",
               "returns_and_credits": "customers' credits and returns rising",
               "slow_moving_stock": "stock not moving",
               "expiring_lot_stock": "lots with stock expiring",
               "assembly_component_shortfall": "assembly orders short of components",
               "certificate_expiry": "certificates expiring",
               "freight_cost_anomaly": "freight bills far above usual",
               "price_consistency": "what each customer pays for the same product",
               "stale_quote": "open quotes with no order",
               "priority_fill_unmet": "priority-listed orders late or unscheduled",
               "duplicate_parties": "customers and suppliers entered twice",
               "key_fields_missing": "key fields missing on active records",
               "twin_disagreement": "systems disagreeing on one record",
               "stale_captures": "sources not updated lately",
               "reorder_soon": "items to reorder soon",
               "inventory_adjustment": "stock written down by hand",
               "blocked_account_activity": "open orders with blocked accounts",
               "gl_posting_errors": "ledger batches that failed to post",
               "recurring_task_missed": "recurring checks that missed a cycle",
               "cash_application": "remittances matched to open invoices",
               "statement_reconciliation": "supplier statements against our payables"}
REASON_WORDS = {"time_budget": "time ran out", "event_bound": "only the most recent were read",
                "impact_bound": "only the first ones were followed",
                "card_bound": "only the first ones were read",
                "read_apart": "the rest wait for the supplier-mail pass"}

# Why a rule could not judge some records, in plain words ("cannot check: 12 records lack
# order lines"); a reason not listed is said by its own name, never dropped.
UNJUDGED_WORDS = {"no_order_lines": "lack order lines",
                  "status_missing": "hold no status",
                  "status_unmapped": "hold a status no rule reads",
                  "no_required_date": "hold no required date",
                  "required_date_default": "hold only an import or default required date",
                  "required_date_disputed": "hold two different required dates",
                  "no_need_date": "hold no due date",
                  "need_date_disputed": "hold two different due dates",
                  "fulfilment_unsettled": "have a shipment or invoice not yet confirmed as theirs",
                  "closed_in_unsettled_twin": "closed only in an unconfirmed copy",
                  "date_unparseable": "hold a date that cannot be read",
                  "not_projected": "are not in the records yet"}


def data_notes(inbox: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the checks could not judge, as data-quality notes per check and reason: only
    the counts this reader's own trimmed read gives (``read_inbox``'s ``rules``), never
    a record named."""
    out = []
    for rule in inbox.get("rules") or ():
        if not isinstance(rule, Mapping):
            continue
        name = str(rule.get("rule"))
        for reason, n in sorted((rule.get("not_evaluated_by_reason") or {}).items()):
            if not isinstance(n, int) or n <= 0:
                continue
            words = UNJUDGED_WORDS.get(reason, str(reason).replace("_", " "))
            out.append({"check": name, "reason": reason, "count": n,
                        "words": f"{CHECK_WORDS.get(name, name)}: cannot check {n} "
                                 f"record{'' if n == 1 else 's'} that {words}"})
    return out


RANK_BASIS = ("Your own items first (records you are named on, that your mail mentions, or "
              "whose supplier or customer writes to you), then by customer orders affected, then money involved (largest first within "
              "each currency; amounts in different currencies are never converted or "
              "compared, and a suspect total is never used), then what the latest check "
              "found new, then the most days late, slipped or waiting and the fewest days "
              "to expiry, then the most recent first. Acknowledged items come after the "
              "others.")

NEXT_ACTIONS = {
    "po_late": ("Ask the supplier for a confirmed delivery date and tell the people whose "
                "orders need this material.", "late_ask_supplier"),
    "po_due": ("Ask the supplier to confirm it ships today; it is not late yet.",
               "due_confirm_today"),
    "po_undated": ("Ask the supplier for a delivery date and record the date the order is "
                   "needed by, so it can be tracked as late or on time.", "undated_ask_date"),
    "eta_slipped": ("Check whether the new date still meets the orders this purchase order "
                    "supplies, and agree a recovery with the supplier if not.",
                    "date_moved_check_orders"),
    "vendor_no_reply": ("Follow up with the supplier on the cited request (nothing is sent "
                        "until you send it).", "no_reply_follow_up"),
    "vendor_terms_unapplied": ("Review the supplier's offered term and, if it is right, enter "
                               "it in the Portal (nothing is written here).",
                               "terms_review_and_enter"),
    "negative_margin": ("Open the order's cost lines to see which item lost money, then "
                        "review its price or cost before the next order.",
                        "margin_review_cost"),
    "lot_near_expiry": ("Plan the lot's use or disposition before it expires.",
                        "expiry_plan_use"),
    "lot_on_hold": ("Ask Quality why the batch is held and when its release will be decided.",
                    "hold_ask_quality"),
    "quote_accepted_no_order": ("Convert the accepted quote to a sales order, or record why "
                                "it will not be.", "quote_convert_or_close"),
    "quantity_mismatch": ("Reconcile the quantities with the supplier and the receiving "
                          "record before the invoice is paid.", "reconcile_quantities"),
    "customer_order_at_risk": ("Confirm with the customer's account owner whether the "
                               "required date still holds, and chase the late material.",
                               "at_risk_confirm_date"),
    "customer_order_late": ("Check whether the order shipped; if it did, close it in the "
                            "system that still shows it open, else agree a new date with "
                            "the customer.", "late_order_ship_or_close"),
    "watcher_attention": ("Open the cited sources and decide whether anyone needs to act "
                          "(nothing is sent).", "watcher_review"),
    "mc_task_overdue": ("Ask the task's owner for a new date or to hand it on, and update "
                        "the task in Mission Control.", "mc_task_new_date"),
    "mc_task_stuck": ("Ask the task's owner what blocks it; move, reassign or cancel it in "
                      "Mission Control.", "mc_task_unblock"),
    "mc_approval_pending": ("Remind the approver to decide; never approve on their behalf.",
                            "mc_approval_remind"),
    "planner_task_overdue": ("Ask the assignee for a new date, or close the task if it is "
                             "no longer needed.", "planner_task_new_date"),
    "aged_receivable_worsening": ("Review the customer's overdue invoices and agree a "
                                  "payment plan or credit hold with their account owner.",
                                  "aged_ar_review"),
    "aged_payable_worsening": ("Check which supplier bills are overdue and why, and schedule "
                               "payment or raise the dispute with the supplier.",
                               "aged_ap_review"),
    "production_late": ("Ask production for a finish date and tell the owners of the "
                        "customer orders that wait on it.", "production_new_date"),
    "assembly_damage_spike": ("Have Quality review the recent damage entries for a common "
                              "cause (line, component lot or handling).",
                              "damage_find_cause"),
    "teams_unanswered_ask": ("Follow up with the person in the conversation, or ask someone "
                             "else who can answer (nothing is sent for you).",
                             "teams_follow_up"),
    "stock_out_risk": ("Decide how to cover the shortfall before the first order hit "
                       "ships: expedite an inbound order, buy more, or agree a later date "
                       "with the customers.", "stock_cover_shortfall"),
    "overdue_receivable": ("Ask the customer's account owner to chase payment, and check "
                           "whether a payment or credit is not yet recorded.",
                           "receivable_chase_payment"),
    "unit_cost_jump": ("Check the purchase order's cost with the supplier: a new price, a "
                       "different pack or an entry error, before it is paid or reused.",
                       "cost_jump_check_supplier"),
    "customer_demand_drop": ("Ask the customer's account owner why orders fell and whether "
                             "the business is moving elsewhere.", "demand_ask_why"),
    "promise_date_slip": ("Find what keeps delaying this customer's orders and tell the "
                          "customer the dates that will hold.", "slip_find_cause"),
    "margin_erosion": ("Compare recent prices and costs with the earlier orders and decide "
                       "whether to re-price or renegotiate the cost.", "margin_review_price"),
    "supplier_reliability": ("Raise the late deliveries with the supplier and check the open "
                             "orders that depend on it.", "supplier_raise_lateness"),
    "shipped_not_invoiced": ("Check whether the shipment was invoiced; if not, raise the "
                             "invoice for what shipped (nothing is posted until you post it).",
                             "unbilled_raise_invoice"),
    "duplicate_customer_order": ("Ask the customer whether they meant two orders; if not, "
                                 "propose cancelling the later one before it ships.",
                                 "duplicate_confirm_cancel"),
    "recurring_component_damage": ("Look at the damage entries together and decide whether to "
                                   "raise it with the supplier or hold the item's stock.",
                                   "damage_review_supplier"),
    "customer_chasing_order": ("Send the customer the drafted status reply (nothing is sent "
                               "until you send it) and tell the account's rep.",
                               "chase_reply_status"),
    "expedite_unanswered": ("Answer the expedite with the date that will hold, or chase "
                            "the other side for one.", "expedite_answer_date"),
    "supplier_shortage_open_orders": ("Get the supplier's new date or quantity, then re-plan "
                                      "or tell the customers of the orders it hits.",
                                      "shortage_replan_tell"),
    "supplier_chasing_payment": ("Check whether the supplier's invoice is paid; if not, "
                                 "schedule it and tell the supplier when.",
                                 "payable_answer_supplier"),
    "tell_before_they_ask": ("Tell the customer where the order stands before its date, "
                             "with the drafted note (nothing is sent until you send it).",
                             "chase_tell_first"),
    "commitment_due": ("Do what we promised by its date, or tell the customer the new date "
                       "now.", "promise_keep"),
    "commitment_missed": ("Tell the customer where it stands and the new date, then do what "
                          "was promised.", "promise_missed_tell"),
    "needs_my_reply": ("Reply to the message, or hand it to who should answer.",
                       "reply_or_hand_off"),
    "label_change": ("Record who approved the change, or make it and tell the requester.",
                     "label_change_follow_up"),
    "rd_project": ("Send the customer a status update, or close the project.",
                   "rd_project_follow_up"),
    "approval_waiting": ("Decide the approval, or send the approver the drafted nudge "
                         "(nothing is sent until you send it).", "approval_nudge"),
    "chase_escalating": ("Find who owns the answer, send the chaser a holding reply with "
                         "the date it will come (nothing is sent until you send it), and "
                         "raise it with the owner's lead.", "chase_escalate_owner"),
    "quality_hold_aging": ("Ask QC to release or reject the held stock; the orders waiting "
                           "on it are held up until then.", "hold_decide_release"),
    "reject_trend": ("Review the rejects with QC and, for a supplier's lots, send the drafted "
                     "complaint (nothing is sent until you send it).", "reject_review_supplier"),
    "customer_paying_slower": ("Call the customer's payer about the slower payments and "
                               "decide whether its terms or credit limit should change.",
                               "payer_call_terms"),
    "early_pay_discount_missed": ("Ask the supplier whether the discount can still be "
                                  "taken, and pay discount invoices by their date.",
                                  "discount_claim_supplier"),
    "duplicate_supplier_invoice": ("Check the two invoices; cancel the copy before it is "
                                   "paid, or ask the supplier for the money back.",
                                   "duplicate_invoice_cancel"),
    "duplicate_supplier_payment": ("Check the two payments and ask the supplier for a "
                                   "refund or a credit on the next invoice.",
                                   "duplicate_payment_recover"),
    "customer_credits_rising": ("Review the customer's credit notes for one product, "
                                "shipment or price behind them, and talk to the account.",
                                "credits_account_review"),
    "slow_moving_stock": ("Decide what to do with the stock: sell it off, use it in another "
                          "product, return it to the supplier or write it down.",
                          "slow_stock_decide"),
    "expiring_lot_stock": ("Plan this lot into an open order before it expires, or ask "
                           "Quality to retest or dispose of it.", "lot_expiry_plan_use"),
    "assembly_blocked_by_component": ("Expedite the short components' purchase orders, or "
                                      "move the assembly order's start.",
                                      "assembly_expedite_or_move"),
    "certificate_expiring": ("Ask the supplier for a renewed certificate before the material "
                             "is used or shipped.", "certificate_request_renewal"),
    "freight_cost_anomaly": ("Check the freight bill's charges and ask the carrier to "
                             "correct it if the rate is wrong.", "freight_check_bill"),
    "price_below_typical": ("Check the customer's agreed price for the product and raise "
                            "it at the next quote, or record why it is lower.",
                            "price_review_customer"),
    "customer_price_drop": ("Ask the account owner why the customer's price fell and "
                            "restore the agreed price on the next order if it was not "
                            "meant.", "price_drop_review"),
    "price_below_cost": ("Stop quoting the product at this price: check the agreed price "
                         "and the cost, and reprice the customer's next order.",
                         "price_below_cost_reprice"),
    "priority_order_unfilled": ("Set a ship date for the priority-listed order (or expedite "
                                "the late one) and confirm it to the customer.",
                                "priority_order_schedule"),
    "stale_quote": ("Follow up with the customer on the open quote: ask what holds the "
                    "decision, renew the offer if it lapsed, and close it if the deal is "
                    "lost.", "quote_follow_up"),
    "duplicate_party_candidate": ("Open both records and confirm they are one, or reject "
                                  "the match; nothing is linked until you confirm.",
                                  "duplicate_party_review"),
    "key_field_missing": ("Enter the missing field in the system the record is kept in.",
                          "key_field_fill"),
    "twins_disagree": ("Check which system is right and correct the other one.",
                       "twins_reconcile"),
    "source_stale": ("Check that the source's capture is running and restart it if it "
                     "stopped.", "source_capture_check"),
    "reorder_soon": ("Raise a purchase order for the shortfall now, or expedite an open "
                     "one, before the forecast demand uses up the stock.", "reorder_raise_po"),
    "inventory_adjustment": ("Ask the warehouse why the item was written down and post a "
                             "loss, damage or correction against the right account.",
                             "adjustment_review"),
    "blocked_account_activity": ("Hold the open orders, or have finance lift the block "
                                 "before they ship or are received.", "blocked_account_hold"),
    "gl_posting_errors": ("Correct the entries (open the period or the account) and post "
                          "the batch before month end.", "gl_batch_post"),
    "recurring_task_missed": ("Check the task was done; record it, or do it now and keep "
                              "the schedule.", "recurring_task_check"),
    "unapplied_remittance": ("Find the payment in the bank and apply it to the invoices "
                             "the remittance lists.", "cash_apply"),
    "short_payment": ("Ask the customer about the short amount: a deduction to accept, a "
                      "dispute to settle or a balance to collect.", "short_pay_follow_up"),
    "statement_difference": ("Reconcile each item with the supplier: request a missing "
                             "invoice, confirm a payment, or correct our books.",
                             "statement_reconcile"),
}
# The next step in the words of the department that acts on it, for a reader routed by
# department (an Executive or admin reader, routed every type, gets the general step above):
# the first of the reader's departments, in the order listed, that names one.
NEXT_ACTIONS_BY_DEPARTMENT: dict[str, dict[str, tuple[str, str]]] = {
    "lot_on_hold": {
        "QUALITY": ("Record why the batch is held and decide its release or rejection; "
                    "production and shipping wait on the decision.", "hold_decide_release"),
    },
    "lot_near_expiry": {
        "QUALITY": ("Decide before it expires: release it for use first, retest it to extend "
                    "its date, or quarantine it for disposal.", "expiry_decide_quality"),
        "WH": ("Pick this lot first (first expiring, first out), or tell Quality now if it "
               "will not be used in time.", "expiry_pick_first"),
        "PROD": ("Schedule this lot into production first, or tell Quality now if it will "
                 "not be used in time.", "expiry_schedule_first"),
    },
    "customer_order_at_risk": {
        "PROC": ("Chase the supplier of the late material for a firm date, then tell the "
                 "account owner which date the customer order can now make.",
                 "at_risk_chase_supplier"),
        "SALES": ("Tell the customer before the date is missed: confirm with Purchasing when "
                  "the late material arrives, then agree a new date or a partial shipment.",
                  "at_risk_tell_customer"),
        "OPS": ("Re-plan the work around the late material and confirm the new completion "
                "date with the account owner.", "at_risk_replan"),
    },
    "negative_margin": {
        "SALES": ("Before quoting this customer again, review the item's price against its "
                  "cost: the order lost money on item cost.", "margin_review_price"),
    },
    "quantity_mismatch": {
        "WH": ("Recount what was received against the packing slip and correct the receipt "
               "if it is wrong.", "mismatch_recount"),
        "FIN": ("Hold the invoice until the billed quantity matches what was received.",
                "mismatch_hold_invoice"),
    },
    "assembly_damage_spike": {
        "QUALITY": ("Review the recent damage entries for a common cause and hold the "
                    "component lot if one is behind it.", "damage_quality_review"),
        "PROC": ("Ask the component supplier about the lots behind the damage.",
                 "damage_ask_supplier"),
    },
    "aged_receivable_worsening": {
        "ACCTMGMT": ("Ask the customer about their overdue invoices and agree a date to pay.",
                     "aged_ar_ask_customer"),
    },
    "stock_out_risk": {
        "PROC": ("Expedite or add a purchase order for the item so it arrives before the "
                 "first order hit ships.", "stock_expedite_supply"),
        "SALES": ("Tell the customers hit before the date is missed and agree a later date "
                  "or a partial shipment.", "stock_tell_customer"),
        "OPS": ("Re-plan the orders hit around the stock that will arrive, and confirm the "
                "new dates with the account owners.", "stock_replan"),
        "WH": ("Count the item's stock and correct the record if it is wrong; the shortfall "
               "rests on it.", "stock_count"),
    },
    "overdue_receivable": {
        "FIN": ("Check for an unapplied payment or credit, then send the customer a payment "
                "reminder (nothing is sent until you send it).", "receivable_check_remind"),
        "SALES": ("Ask the customer when they will pay, and hold new orders on terms if "
                  "Finance asks.", "receivable_ask_customer"),
    },
    "customer_demand_drop": {
        "SALES": ("Call the customer: ask what changed and whether orders are going to "
                  "another supplier.", "demand_call_customer"),
        "ACCTMGMT": ("Review the account with the customer: open quotes, products they "
                     "stopped buying and their plans.", "demand_review_account"),
        "FIN": ("Update the forecast for this customer's lower orders.",
                "demand_update_forecast"),
    },
    "promise_date_slip": {
        "OPS": ("Find what delays this customer's orders (stock, capacity or shipping) and "
                "re-plan the open ones.", "slip_replan"),
        "SALES": ("Tell the customer the dates that will hold and stop promising dates "
                  "Operations cannot meet.", "slip_tell_customer"),
        "ACCTMGMT": ("Call the customer about the missed dates before they escalate, with "
                     "the new dates from Operations.", "slip_call_customer"),
    },
    "margin_erosion": {
        "FIN": ("Check whether cost went up or price went down on the recent orders, and "
                "flag it for pricing.", "margin_check_cause"),
        "SALES": ("Review the prices quoted to this customer or for this product against the "
                  "earlier orders before quoting again.", "margin_review_quotes"),
        "PROC": ("Check whether the item's purchase cost rose and whether a better price "
                 "can be agreed.", "margin_check_cost"),
    },
    "supplier_reliability": {
        "PROC": ("Ask the supplier why deliveries are later and get dates that hold, or "
                 "line up another source.", "supplier_ask_why"),
        "OPS": ("Plan with the supplier's later deliveries in mind and check the orders "
                "that depend on its open purchase orders.", "supplier_plan_buffer"),
    },
    "unit_cost_jump": {
        "PROC": ("Ask the supplier why the price changed and get the agreed price in "
                 "writing before the next order.", "cost_jump_ask_supplier"),
        "FIN": ("Check the cost against the supplier's invoice before it is paid, and "
                "review the item's standard cost.", "cost_jump_check_invoice"),
    },
    "shipped_not_invoiced": {
        "FIN": ("Raise the invoice for what shipped, or record the invoice that already "
                "exists against the order.", "unbilled_invoice_now"),
        "SALES": ("Confirm with the customer what was delivered so Finance can invoice it.",
                  "unbilled_confirm_delivery"),
        "OPS": ("Confirm the shipment really left and its quantities, so Finance can invoice "
                "it.", "unbilled_confirm_shipment"),
    },
    "duplicate_customer_order": {
        "SALES": ("Ask the customer whether the second order is wanted; draft the question "
                  "(nothing is sent until you send it).", "duplicate_ask_customer"),
        "OPS": ("Hold picking the later order until Sales confirms it is not a repeat.",
                "duplicate_hold_picking"),
    },
    "recurring_component_damage": {
        "QUALITY": ("Inspect the item's stock and its damage entries, and open a supplier "
                    "complaint if it is the supplier's packaging.", "damage_inspect_complain"),
        "PROC": ("Raise the damage with the supplier and ask for credit or a packaging "
                 "change before the next order.", "damage_tell_supplier"),
        "OPS": ("Check how the item is handled on the line and in storage.",
                "damage_check_handling"),
    },
    "customer_chasing_order": {
        "SALES": ("Reply to the customer with the drafted status and the date that will "
                  "hold (nothing is sent until you send it).", "chase_reply_status"),
        "OPS": ("Confirm what is holding the order up and the date it will ship, so Sales "
                "can answer the customer.", "chase_confirm_date"),
    },
    "expedite_unanswered": {
        "OPS": ("Confirm the date the order can ship or the PO can arrive, and answer the "
                "expedite.", "expedite_confirm_date"),
        "PROC": ("Chase the supplier for a firm date on the expedited purchase order.",
                 "expedite_chase_supplier"),
        "SALES": ("Tell the customer the date that will hold for the expedited order.",
                  "expedite_tell_customer"),
    },
    "supplier_shortage_open_orders": {
        "PROC": ("Ask the supplier for its new date or quantity, or find another source.",
                 "shortage_ask_supplier"),
        "OPS": ("Re-plan the open orders that depend on the short purchase order.",
                "shortage_replan"),
        "SALES": ("Tell the customers of the affected orders before their dates.",
                  "shortage_tell_customers"),
    },
    "supplier_chasing_payment": {
        "FIN": ("Check the supplier's invoice: pay it or tell the supplier when it will be "
                "paid.", "payable_pay_or_date"),
        "PROC": ("Make sure the supplier's chase is answered so supply is not held.",
                 "payable_protect_supply"),
    },
    "tell_before_they_ask": {
        "SALES": ("Send the customer the drafted note on where the order stands before its "
                  "date (nothing is sent until you send it).", "chase_tell_first"),
        "OPS": ("Check whether the order will be ready by its date and tell Sales.",
                "chase_check_ready"),
    },
    "quality_hold_aging": {
        "QUALITY": ("Test or review the held stock now and release or reject it.",
                    "hold_release_or_reject"),
        "OPS": ("Re-plan the orders waiting on the held stock, or ask QC for a decision.",
                "hold_replan"),
    },
    "reject_trend": {
        "QUALITY": ("Look at the rejects together and open a supplier complaint for the "
                    "supplier's lots (the draft is on the alert).", "reject_open_complaint"),
        "PROC": ("Raise the rejects with the supplier and ask for credit or replacement.",
                 "reject_tell_supplier"),
    },
    "eta_slipped": {
        "OPS": ("Check whether the plan still holds with the new date and move the work that "
                "depends on it.", "date_moved_replan"),
    },
}
# Departments that act alike share a voice: Quality Assurance and Quality Control as
# Quality, Account Management as Sales.
VOICE_OF = {"QA": "QUALITY", "QC": "QUALITY", "ACCTMGMT": "SALES"}
SUSPECT_WORDS = "suspect source data"


@dataclass(frozen=True)
class Routing:
    """Where a reader's work is routed: department codes and the Portal role; ``basis``
    says where they came from (``employee``, ``fixture`` or a reason none is known)."""
    departments: frozenset[str] = frozenset()
    role: str = ""
    basis: str = "employee"
    everything: bool = False

    @classmethod
    def of(cls, entitlement: Any) -> "Routing":
        """From an ``EmployeeEntitlement`` (None: not an employee, routed nothing)."""
        if entitlement is None or getattr(entitlement, "actor", None) is None:
            return cls(basis="not_an_employee")
        departments = frozenset(getattr(entitlement, "routing_departments", ()) or ())
        role = str(getattr(entitlement, "portal_role", "") or "")
        return cls(departments, role, "employee" if departments or _admin(role)
                   else "no_department_on_record")

    @property
    def all_types(self) -> bool:
        """Routed every event type: told so, an Executive member or a Portal admin."""
        return self.everything or _admin(self.role) or bool(self.departments & EVERYTHING)

    def routes(self, departments: Iterable[str]) -> bool:
        return self.all_types or bool(self.departments & set(departments))


def _admin(role: str) -> bool:
    """The Portal's own admin test (roles.ts ``hasRole``: ADMIN or above; unknown: no)."""
    from .employee_permissions_portal import is_admin

    return bool(role) and is_admin(role)


def event_departments(gap: Mapping[str, Any]) -> list[str]:
    """The departments an event goes to: its type's and the codes its follow-up names."""
    from . import answerers

    kind = TYPE_OF_KIND.get(str(gap.get("kind")))
    out = list(EVENT_TYPES[kind]["departments"]) if kind else []
    codes = answerers.departments()
    for who in (gap.get("follow_up") or {}).get("who_can_answer") or ():
        if isinstance(who, str) and who in codes and who not in out:
            out.append(who)
    return out


def _amount(event: Mapping[str, Any]) -> tuple[Decimal, str] | None:
    amount = event.get("amount")
    if not isinstance(amount, Mapping):
        return None
    try:
        value = Decimal(str(amount.get("value")))
    except (InvalidOperation, ValueError):
        return None
    currency = amount.get("currency")
    if not value.is_finite() or not isinstance(currency, str) or len(currency) != 3:
        return None
    return value, currency


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class Item:
    gap: Mapping[str, Any]
    event_type: str
    departments: list[str]
    order: int
    amount: tuple[Decimal, str] | None = None
    suspect: str | None = None
    disagree: dict[str, Any] | None = None   # total and its lines disagree (neutral)
    basis: str = ""                           # what the amount is (``_AMOUNT_WORDS``)
    orders_affected: int = 0
    days: int | None = None
    new_since_visit: bool = False
    new_this_generation: bool = False
    acknowledged: bool = False
    money_place: float = 1.0   # 0: the largest amount in its currency; 1: no usable amount
    yours: list[dict[str, str]] = field(default_factory=list)   # why it is the reader's


def _item(gap: Mapping[str, Any], order: int, since: str | None, run_at: str | None
          ) -> Item | None:
    kind = TYPE_OF_KIND.get(str(gap.get("kind")))
    if kind is None:
        return None
    event = gap.get("event") if isinstance(gap.get("event"), Mapping) else {}
    days = _int(gap.get("days_late"))
    if kind == "po_late" and days is None:
        kind = "po_due"     # not late until Home's rule says so (``days_late``)
    if days is None:
        days = _int(event.get("days"))
    amount = event.get("amount") if isinstance(event.get("amount"), Mapping) else {}
    suspect = amount.get("suspect")
    lines = amount.get("lines") if isinstance(amount.get("lines"), Mapping) else {}
    verdict = lines.get("verdict")
    disagree = ({k: v for k, v in lines.items() if k == "lines_sum"}
                if verdict == "disagree" else None)
    basis = LINE_BASES.get(verdict, str(amount.get("basis") or ""))
    first = gap.get("first_seen") if isinstance(gap.get("first_seen"), str) else None
    return Item(gap, kind, event_departments(gap), order, _amount(event),
                str(suspect)[:200] if suspect else None,
                disagree, basis,
                max(_int(event.get("orders_affected")) or 0, 0), days,
                new_since_visit=bool(first and since and first > since),
                new_this_generation=bool(first and run_at and first == run_at),
                acknowledged=gap.get("state") == "acknowledged")


def ranking_amount(gap: Mapping[str, Any]) -> tuple[Decimal, str, str] | None:
    """(value, currency, its cited handle) of a gap's amount when the briefing would rank
    by it (``rank``'s usable amount: not suspect, its lines not disagreeing, a basis
    that is the record's own total); None otherwise."""
    item = _item(gap, 0, None, None)
    event = gap.get("event") if isinstance(gap.get("event"), Mapping) else {}
    amount = event.get("amount") if isinstance(event.get("amount"), Mapping) else {}
    handle = amount.get("handle")
    if item is None or not item.amount or item.suspect or item.disagree is not None \
            or item.basis in UNRANKED_BASES or not isinstance(handle, str):
        return None
    return item.amount[0], item.amount[1], handle


def rank(items: list[Item]) -> tuple[list[Item], str]:
    """The items worst first (``RANK_BASIS``). Money is placed within its own currency:
    each usable amount's place among the amounts of that currency (0 the largest), so
    one amount in another currency neither switches money ranking off nor is compared
    with the rest; a suspect total, or one its own lines disagree with, has no place. An open PO with no due date is listed after every real event
    (loss, overdue order, late PO) and ranked by money only among its own kind."""
    usable = [i for i in items if i.amount and not i.suspect and i.disagree is None
              and i.basis not in UNRANKED_BASES]
    by_currency: dict[str, list[Decimal]] = {}
    for item in usable:
        by_currency.setdefault(item.amount[1], []).append(item.amount[0])
    for values in by_currency.values():
        values.sort(reverse=True)
    for item in usable:
            values = by_currency[item.amount[1]]
            item.money_place = values.index(item.amount[0]) / len(values)
    ordered = sorted(items, key=lambda i: i.order)
    ordered.sort(key=lambda i: str(i.gap.get("first_seen") or ""), reverse=True)
    ordered.sort(key=lambda i: (not i.yours, i.acknowledged, i.event_type == "po_undated",
                                -i.orders_affected, i.money_place, not i.new_this_generation, -_urgency(i)))
    return ordered, RANK_BASIS


def _event(item: Item) -> Mapping[str, Any]:
    event = item.gap.get("event")
    return event if isinstance(event, Mapping) else {}


def _ids(value: Any) -> set[str]:
    return {v for v in value if isinstance(v, str)} if isinstance(value, (list, tuple)) else set()


def real_impact(items: list[Item]) -> tuple[list[Item], dict[str, int]]:
    """The items that are each one real event, and how many were left out and why:

    * ``suspect_source_data``: an item whose money figure IS the event (``AMOUNT_EVENT_KINDS``:
      an amount anomaly) and that ``suspect_money`` finds implausible is no business event;
      it is left out and counted. Every other item stays, however its total checks: a total
      found implausible is unranked and said to be suspect, a total whose check could not
      run is unranked and said not to have been checked (``NOT_CHECKED``); nothing is ever
      dropped for a check that did not run;
    * ``same_event``: one late purchase order and the customer orders it holds up are one
      event: a PO's impact with the margin at stake stands for its plain impact, and a PO's
      impact item stands for each order at risk it names (an order at risk whose every
      late PO has a visible impact item naming it). A reader who may not see the PO's item
      keeps the order's own.
    """
    out: dict[str, int] = {}
    kept: list[Item] = []
    for item in items:
        if item.suspect and item.suspect != NOT_CHECKED \
                and item.gap.get("kind") in AMOUNT_EVENT_KINDS:
            out["suspect_source_data"] = out.get("suspect_source_data", 0) + 1
        else:
            kept.append(item)
    with_margin = {i.gap.get("object_id") for i in kept if i.gap.get("kind") == "po_impact_margin"}
    names: dict[str, set[str]] = {}
    for i in kept:
        if i.gap.get("kind") in ("po_impact", "po_impact_margin"):
            names.setdefault(str(i.gap.get("object_id")), set()).update(
                _ids(_event(i).get("orders")))
    final: list[Item] = []
    for item in kept:
        kind = item.gap.get("kind")
        pos = _ids(_event(item).get("pos"))
        same = (kind == "po_impact" and item.gap.get("object_id") in with_margin) or (
            kind == "order_at_risk" and bool(pos) and all(
                item.gap.get("object_id") in names.get(p, ()) for p in pos))
        if same:
            out["same_event"] = out.get("same_event", 0) + 1
        else:
            final.append(item)
    return final, out


def _urgency(item: Item) -> int:
    """How pressing an item's days are: days passed as they are, days left to a deadline
    (``COUNTDOWN_HORIZON``) the fewer the more; no days: least (-1)."""
    if item.days is None:
        return -1
    if EVENT_TYPES[item.event_type].get("days") == "left":
        return max(COUNTDOWN_HORIZON - item.days, 0)
    return item.days


def _check_state(inbox: Mapping[str, Any], name: str) -> tuple[str, str]:
    """A check's state this run and its words: never a silent gap."""
    status = (inbox.get("detectors") or {}).get(name)
    why = [REASON_WORDS.get(r, "") for r in (inbox.get("detector_reasons") or {}).get(name)
           or ()]
    if status == "complete":
        return "complete", "checked"
    if status == "truncated":
        return "partial", "partly checked this run" + (
            f" ({'; '.join(w for w in why if w)})" if any(why) else "")
    if status == "failed":
        failed = at_words((inbox.get("failed_at") or {}).get(name))
        return "failed", (f"check failed at {failed}" if failed else
                          "could not be checked this run") + (
            "; its earlier findings are shown marked as not rechecked")
    if status == "not_set_up":
        return "not_checked", "not set up yet"
    if status == "blocked":
        from .proactive_stock import field_words

        fields = field_words((inbox.get("detector_reasons") or {}).get(name) or ())
        if fields:
            return "not_checked", ("not checked: waiting on data the sources do not serve "
                                   f"yet ({', '.join(fields)})")
    return "not_checked", "not checked this run"


def at_words(stamp: Any) -> str | None:
    """A run time in the business's words, to the minute in US Eastern ("2026-11-08 9:30
    AM ET"); None when it does not read as a time."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(ZoneInfo("America/New_York"))
    hour = local.hour % 12 or 12
    return (f"{local.date().isoformat()} {hour}:{local.minute:02d} "
            f"{'AM' if local.hour < 12 else 'PM'} ET")


def _checks(inbox: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each check behind the event types with this run's state and its last run's time."""
    out = []
    ran = inbox.get("detectors") or {}
    run_at = inbox.get("detector_run_at") or {}
    for name in dict.fromkeys(d for spec in EVENT_TYPES.values() for d in spec["check"]
                              if not spec.get("optional") or d in ran):
        state, words = _check_state(inbox, name)
        last = at_words(run_at.get(name)) if run_at.get(name) else None
        out.append({"check": name, "label": CHECK_WORDS.get(name, name), "status": state,
                    "words": f"{CHECK_WORDS.get(name, name)}: {words}"
                             + (f" (last run {last})" if last else ""),
                    "last_run": run_at.get(name) if last else None})
    return out


def _types(inbox: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each event type: its checks' state this run (the least complete of them) and
    whether a recorded run over this data ever found one of THIS type (``found_live``,
    per type from the kinds' history; never said of a type whose existence is withheld)."""
    order = ("not_checked", "failed", "partial", "complete")
    kinds = inbox.get("kind_history") or {}
    out = []
    for name, spec in EVENT_TYPES.items():
        if spec.get("optional") and not set(spec["check"]) & set(inbox.get("detectors") or {}):
            continue   # a check switched off is not this reader's briefing
        states = [_check_state(inbox, d) for d in spec["check"]]
        state = min((s for s, _ in states), key=order.index)
        quiet = HIDDEN_POLICY[name] == "none"
        first = None if quiet else _first_found(name, kinds)
        out.append({"event_type": name, "label": spec["label"], "status": state,
                    "words": f"{spec['label']}: " + "; ".join(dict.fromkeys(
                        w for _, w in states)),
                    "found_live": None if quiet else first is not None,
                    "first_found": None if first is None else first[:10]})
    return out


def _hidden(hidden: Mapping[str, int] | None, routing: Routing) -> dict[str, int] | None:
    """The events routed to this reader that it may not open, per type, as counts only,
    for the types whose policy allows a count (``HIDDEN_POLICY``)."""
    if hidden is None:
        return None
    out: dict[str, int] = {}
    for kind, n in hidden.items():
        name = TYPE_OF_KIND.get(kind)
        if name is None or HIDDEN_POLICY[name] != "count" or not int(n):
            continue
        if routing.routes(event_departments({"kind": kind})):
            out[name] = out.get(name, 0) + int(n)
    return out


def build(inbox: Mapping[str, Any], *, routing: Routing, hidden: Mapping[str, int] | None,
          last_visit: str | None, limit: int = MAX_ITEMS, since_visit: bool = False,
          involved: Any = None, enabled: Iterable[str] = ()) -> dict[str, Any]:
    """One reader's briefing from its own trimmed inbox read (``read_inbox``, open and
    acknowledged) and the counts of events it may not see (``hidden_counts``): every
    routed item ranked (``items``, the first ``TOP_ITEMS`` shown one by one), the groups
    by type with their counts, each check's state and the hidden counts by type.
    ``since_visit``: only what is new since the reader's last visit.

    ``involved``: what the reader is involved in (``operating.involvement``), called with
    the records of every item the reader may see, in ranked order, returning an
    ``Involvement``. An item about the reader's own record (``yours``) is in their
    briefing whether or not it is routed to their department, and every one of theirs is
    ranked before the rest (``RANK_BASIS``); it only ever chooses among items the reader's
    own trimmed inbox holds, so it never widens what they see."""
    if inbox.get("status") != "read":
        return {"schema": SCHEMA, "status": str(inbox.get("status") or "unavailable"),
                "items": [], "top": 0, "groups": [], "checks": [], "types": [], "count": 0,
                "new_since_visit": 0, "other_teams": 0, "hidden": None, "hidden_by_type": {},
                "rank_basis": RANK_BASIS, "routing": _routing_out(routing),
                "last_visit": last_visit, "as_of": None, "run_at": None, "complete": False,
                "since_visit": since_visit, "late_total": None, "yours": 0,
                "involvement": None, "carried": 0, "data_notes": [],
                "today": today_line({"status": "unavailable"})}
    run_at = inbox.get("run_at") if isinstance(inbox.get("run_at"), str) else None
    visible: list[Item] = []
    for n, gap in enumerate(inbox.get("gaps") or ()):
        if gap.get("state", "open") not in ("open", "acknowledged"):
            continue
        # a type the registry keeps off never reaches a briefing, its digest or its counts
        # (the AI watcher's items are merged in after the inbox read, so checked here too)
        if gap.get("kind") in alert_kinds.off_gap_kinds(enabled):
            continue
        item = _item(gap, n, last_visit, run_at)
        if item is not None:
            visible.append(item)
    involvement = None
    if involved is not None and visible:
        from .involvement import gather, related_ids
        involvement = involved(gather(oid for i in rank(list(visible))[0]
                                      for oid in related_ids(i.gap)))
        for item in visible:
            reasons = [r for oid in related_ids(item.gap)
                       for r in involvement.by_object.get(oid, ())]
            if reasons:
                item.yours = reasons
    for item in visible:
        if item.event_type in OWN_MAIL and not item.yours:
            # a person's own mail (the inbox shows it to its owner only): theirs, whatever
            # their department
            item.yours = [{"reason": "your_mail", "via": item.gap.get("object_id")}]
    mine: list[Item] = []
    others = 0
    for item in visible:
        if item.yours or routing.routes(item.departments):
            mine.append(item)
        else:
            others += 1
    mine, left_out = real_impact(mine)
    new = sum(1 for i in mine if i.new_since_visit)
    late = len({i.gap.get("object_id") for i in mine if i.event_type == "po_late"})
    if since_visit:
        mine = [i for i in mine if i.new_since_visit]
    ordered, basis = rank(mine)
    groups = []
    for name, spec in EVENT_TYPES.items():
        members = [i for i in ordered if i.event_type == name]
        if members:
            groups.append({"event_type": name, "label": spec["label"], "count": len(members),
                           "new_since_visit": sum(1 for i in members if i.new_since_visit),
                           "acknowledged": sum(1 for i in members if i.acknowledged),
                           "in_top": sum(1 for i in members[:TOP_ITEMS]
                                         if i in ordered[:TOP_ITEMS])})
    groups.sort(key=lambda g: min(ordered.index(i) for i in ordered
                                  if i.event_type == g["event_type"]))
    hidden_by_type = _hidden(hidden, routing)
    kinds = inbox.get("kind_history") or {}
    shown = [_out(i, rank_n, kinds, routing)
             for rank_n, i in enumerate(ordered[:limit], start=1)]
    checks = _checks(inbox)
    types = _types(inbox)
    found = {"schema": SCHEMA, "status": "read", "as_of": inbox.get("as_of"), "run_at": run_at,
            "rank_basis": basis, "routing": _routing_out(routing),
            "count": len(ordered), "top": min(TOP_ITEMS, len(ordered)), "items": shown,
            # My work holds the home kinds only (at most ``TOP_ITEMS``); the rest wait in
            # Watching ("14 more in Watching")
            "home_count": sum(1 for i in ordered if alert_kinds.home(i.event_type)),
            "watching_count": sum(1 for i in ordered if not alert_kinds.home(i.event_type)),
            "groups": groups, "more": len(ordered) > limit,
            # the read stopped at ``MAX_READ``: the counts are "at least"
            "counts_exact": not inbox.get("more"),
            "new_since_visit": new, "since_visit": since_visit,
            "yours": sum(1 for i in ordered if i.yours),
            "involvement": involvement.out() if involvement is not None else None,
            "acknowledged": sum(1 for i in ordered if i.acknowledged),
            "late_total": late, "other_teams": others, "left_out": left_out,
            "hidden": None if hidden_by_type is None else sum(hidden_by_type.values()),
            "hidden_by_type": hidden_by_type or {}, "last_visit": last_visit,
            "checks": checks, "types": types,
            "complete": bool(inbox.get("complete")) and not inbox.get("more"),
            "not_run": sorted((inbox.get("not_run") or {}).keys()),
            # checks whose findings are of older data than served now: not listed until
            # they run over it (``proactive_engine.read_inbox``)
            "refreshing": list(inbox.get("refreshing") or ()),
            # of the items, those carried from an earlier or failed check (marked)
            "carried": sum(1 for i in shown if i.get("carried")),
            "data_notes": data_notes(inbox),
            "checked_on": inbox.get("checked_on")
            if isinstance(inbox.get("checked_on"), str) else None}
    found["today"] = today_line(found)
    return found


def read(inbox: Any, store: Any, *, reader: str, may_read: Any, held: Iterable[str],
         decide: Any, routing: Routing, limit: int = MAX_ITEMS,
         since_visit: bool = False, involved: Any = None,
         enabled: Iterable[str] = ()) -> dict[str, Any]:
    """One reader's briefing from the inbox beside ``store``: its open and acknowledged
    events only (other gap kinds never use up the read), the events routed to it that it
    may not see as counts, and its last visit; the reader's grant is ``may_read``/
    ``held``/``decide``."""
    from . import proactive_engine as pe

    from . import ai_watcher

    found = pe.read_inbox(inbox, store, reader=reader, may_read=may_read, held=held,
                          decide=decide, states=("open", "acknowledged"), limit=MAX_READ,
                          kinds=EVENT_KINDS, enabled=enabled)
    # the AI watcher's open items this reader may see, only while its switch is on
    found = ai_watcher.merge_briefing(found, Path(store), may_read=may_read, held=held,
                                      decide=decide, reader=reader)
    hidden = pe.hidden_counts(inbox, store, kinds=EVENT_KINDS, may_read=may_read, held=held,
                              decide=decide) if found.get("status") == "read" else None
    return build(found, routing=routing, hidden=hidden, last_visit=pe.last_visit(inbox, reader),
                 limit=limit, since_visit=since_visit, involved=involved, enabled=enabled)


def _routing_out(routing: Routing) -> dict[str, Any]:
    return {"departments": sorted(routing.departments), "basis": routing.basis,
            "everything": routing.all_types}


def _first_found(event_type: str, kinds: Mapping[str, Any]) -> str | None:
    """When a recorded run over this data first found an event of this type (its kinds'
    history, ``kind_history``), or None."""
    spec = EVENT_TYPES[event_type]
    found = [str(kinds[k]) for k in spec.get("seen", spec["kinds"]) if kinds.get(k)]
    return min(found) if found else None


def next_action(event_type: str, routing: Routing | None = None) -> tuple[str, str]:
    """The suggested next step of an event type for this reader: its department's words
    (``NEXT_ACTIONS_BY_DEPARTMENT``) when it is routed by department, else the general one."""
    voices = NEXT_ACTIONS_BY_DEPARTMENT.get(event_type) or {}
    if routing is not None and not routing.all_types:
        held = {VOICE_OF.get(d, d) for d in routing.departments}
        for department, words in voices.items():
            if department in held:
                return words
    return NEXT_ACTIONS[event_type]


def _out(item: Item, n: int, kinds: Mapping[str, Any],
         routing: Routing | None = None) -> dict[str, Any]:
    gap = item.gap
    spec = EVENT_TYPES[item.event_type]
    text, rule = next_action(item.event_type, routing)
    follow_up = gap.get("follow_up") or {}
    if item.event_type == "watcher_attention" and isinstance(follow_up.get("next_step"), str) \
            and follow_up["next_step"].strip():
        # the watcher's own suggestion, screened by the answer gate (never sent)
        text = follow_up["next_step"].strip()[:400] + " (Suggestion only; nothing is sent.)"
    if item.event_type == "po_late" and isinstance(follow_up.get("draft"), Mapping):
        text += " A draft supplier message is ready to review (not sent)."
    facts: dict[str, Any] = {}
    if item.amount:
        facts["amount"] = {"value": str(item.amount[0]), "currency": item.amount[1],
                           "basis": item.basis}
        if item.suspect:
            facts["amount"]["suspect"] = item.suspect
        if item.disagree is not None:
            facts["amount"]["disagree"] = item.disagree
    if item.orders_affected:
        facts["orders_affected"] = item.orders_affected
    if item.days is not None:
        facts["days"] = item.days
    found = _first_found(item.event_type, kinds) is not None
    if isinstance(gap.get("event"), Mapping) and gap["event"].get("late_status_not_checked"):
        facts["late_status"] = "late status not checked"
    # the records a finding joins (a late PO and the customer orders it holds up): the
    # gap's own event, so they are among what this reader may see whenever the gap is
    event = gap.get("event") if isinstance(gap.get("event"), Mapping) else {}
    links = {k: list(event[k]) if isinstance(event[k], (list, tuple)) else event[k]
             for k in ("pos", "orders", "required", "material", "due", "line_promised")
             if isinstance(event.get(k), (str, list, tuple))}
    summary = gap.get("summary")
    return {"rank": n, "id": gap.get("id"), "event_type": item.event_type,
            "label": spec["label"], "kind": gap.get("kind"),
            # the registry (``alert_kinds``): its merged kind, on by default, on My work
            "alert_kind": alert_kinds.kind_of(item.event_type),
            "default_on": alert_kinds.default_on(item.event_type),
            "home": alert_kinds.home(item.event_type),
            # the slop gate's verdict on the summary as written (counted, never hidden)
            "copy_check": copy_gate.check(summary, surface="summary")
            if isinstance(summary, str) else None,
            # its type found by a recorded run over this data, else fixture
            "evidence": "live" if found else "fixture-only",
            "object_id": gap.get("object_id"), "object_type": gap.get("object_type"),
            "summary": summary, "why": follow_up.get("why"),
            "next_action": {"text": text, "rule": rule, "suggestion_only": True},
            "facts": facts, "departments": item.departments,
            "citations": list(gap.get("citations") or ()),
            **({"links": links} if links else {}),
            "acknowledged": item.acknowledged,
            "first_seen": gap.get("first_seen"), "last_seen": gap.get("last_seen"),
            "new_since_visit": item.new_since_visit,
            "new_this_generation": item.new_this_generation,
            "yours": bool(item.yours),
            "involvement": [dict(r) for r in item.yours],
            # a finding from an earlier or failed check: shown marked, never as current
            **({"carried": dict(gap["carried"])} if isinstance(gap.get("carried"), Mapping)
               else {})}


_AMOUNT_WORDS = {"loss": "lost", "margin_at_stake": "margin at stake",
                 "total_cost": "order total", "total_amount": "order total"}
PORTAL_TOTAL_WORDS = "Portal total (its lines' unit prices added, not quantity x price)"
# A total checked against its own lines and not simply theirs (``suspect_money``): what it
# is said to be, and never ranked by.
LINE_BASES = {"unit_prices": "unit_price_sum", "unpriced": "portal_lines_unpriced",
              "not_open": "not_checked_against_lines"}
BASIS_WORDS = {"unit_price_sum": PORTAL_TOTAL_WORDS,
               "portal_lines_unpriced": "Portal total (lines not priced)",
               "not_checked_against_lines": "total (not checked against its lines)"}
UNRANKED_BASES = frozenset(BASIS_WORDS)


def money_words(facts: Mapping[str, Any]) -> str:
    """An item's amount in words: the figure with what it is; for a suspect total never the
    figure as fact ("suspect source data (why)"); for a total its own lines disagree with,
    both, neutrally ("order total X and its lines disagree (lines sum to Y)")."""
    amount = facts.get("amount")
    if not amount:
        return ""
    if amount.get("suspect") == NOT_CHECKED:
        what = _AMOUNT_WORDS.get(amount["basis"], "at stake")
        return f"{amount['value']} {amount['currency']} {what}, figure not checked"
    if amount.get("suspect"):
        return f"{SUSPECT_WORDS} ({amount['suspect']})"
    if amount.get("basis") in BASIS_WORDS:
        return f"{amount['value']} {amount['currency']} {BASIS_WORDS[amount['basis']]}"
    what = _AMOUNT_WORDS.get(amount["basis"], "at stake")
    lines = amount.get("disagree")
    if isinstance(lines, Mapping):
        sums = (f" (lines sum to {lines['lines_sum']} {amount['currency']})"
                if lines.get("lines_sum") is not None else "")
        return f"{what} {amount['value']} {amount['currency']} and its lines disagree{sums}"
    return f"{amount['value']} {amount['currency']} {what}"


# "Today in one line": each type as a count says it (noun, plural, said of what the latest
# check found new, said of what is open).
TODAY_WORDS = {
    "po_late": ("PO", "POs", "went late", "late"),
    "po_due": ("PO", "POs", "fell due today unconfirmed", "due today, unconfirmed"),
    "po_undated": ("open PO", "open POs", "with no due date", "with no due date"),
    "eta_slipped": ("delivery date", "delivery dates", "slipped", "slipped"),
    "vendor_no_reply": ("supplier request", "supplier requests", "without a reply",
                        "without a reply"),
    "vendor_terms_unapplied": ("supplier term", "supplier terms", "not applied", "not applied"),
    "negative_margin": ("order", "orders", "lost money on item cost",
                        "lost money on item cost"),
    "lot_near_expiry": ("lot", "lots", "came near expiry", "near expiry"),
    "lot_on_hold": ("batch", "batches", "went on quality hold", "on quality hold"),
    "quote_accepted_no_order": ("accepted quote", "accepted quotes", "with no order",
                                "with no order"),
    "quantity_mismatch": ("quantity mismatch", "quantity mismatches", "found", "open"),
    "customer_order_at_risk": ("customer order", "customer orders",
                               "now at risk from late POs", "at risk from late POs"),
    "customer_order_late": ("customer order", "customer orders", "went past its required date",
                            "past their required date"),
    "watcher_attention": ("change", "changes", "flagged for attention",
                          "flagged for attention"),
    "stock_out_risk": ("item", "items", "fell short of open orders",
                       "short of open orders"),
    "overdue_receivable": ("customer invoice", "customer invoices", "went overdue",
                           "overdue"),
    "unit_cost_jump": ("purchase cost", "purchase costs", "jumped", "jumped"),
    "mc_task_overdue": ("task", "tasks", "went overdue", "overdue"),
    "mc_task_stuck": ("task", "tasks", "stalled", "stalled"),
    "mc_approval_pending": ("approval", "approvals", "is waiting", "waiting"),
    "planner_task_overdue": ("Planner task", "Planner tasks", "went overdue", "overdue"),
    "aged_receivable_worsening": ("customer balance", "customer balances", "aged worse",
                                  "older"),
    "aged_payable_worsening": ("supplier balance", "supplier balances", "aged worse",
                               "older"),
    "production_late": ("production record", "production records", "went past due",
                        "past due"),
    "assembly_damage_spike": ("item", "items", "had a damage spike", "with a damage spike"),
    "teams_unanswered_ask": ("Teams question", "Teams questions", "went unanswered",
                             "unanswered"),
    "customer_demand_drop": ("customer", "customers", "ordered much less",
                             "ordering much less"),
    "promise_date_slip": ("customer", "customers", "kept missing promised dates",
                          "missing promised dates"),
    "margin_erosion": ("margin", "margins", "eroded", "eroding"),
    "supplier_reliability": ("supplier", "suppliers", "got later", "delivering later"),
    "shipped_not_invoiced": ("shipped order", "shipped orders", "went uninvoiced",
                             "not invoiced"),
    "duplicate_customer_order": ("customer order", "customer orders", "was entered twice",
                                 "entered twice"),
    "recurring_component_damage": ("item", "items", "kept getting damaged",
                                   "damaged again"),
    "customer_chasing_order": ("order", "orders", "was chased by its customer",
                               "chased by customers"),
    "expedite_unanswered": ("expedite", "expedites", "went unanswered", "unanswered"),
    "supplier_shortage_open_orders": ("supplier shortage", "supplier shortages",
                                      "hit open orders", "hitting open orders"),
    "supplier_chasing_payment": ("supplier payment chase", "supplier payment chases",
                                 "came in", "chasing us"),
    "tell_before_they_ask": ("order", "orders", "came due for a customer who chases",
                             "to tell customers about first"),
    "commitment_due": ("promise", "promises", "comes due", "due"),
    "commitment_missed": ("promise", "promises", "was missed", "missed"),
    "needs_my_reply": ("message", "messages", "waits on your reply", "waiting on your reply"),
    "approval_waiting": ("approval", "approvals", "went unanswered", "waiting"),
    "label_change": ("label or artwork change", "label or artwork changes",
                     "is not approved or not done", "open"),
    "rd_project": ("R&D or sample project", "R&D or sample projects",
                   "has had no activity for days", "idle"),
    "chase_escalating": ("thread", "threads", "was chased repeatedly", "escalating"),
    "quality_hold_aging": ("held batch or lot", "held batches and lots",
                           "stayed on hold too long", "on hold too long"),
    "reject_trend": ("reject trend", "reject trends", "started rising", "rising"),
    "customer_paying_slower": ("customer", "customers", "started paying more slowly",
                               "paying more slowly"),
    "early_pay_discount_missed": ("supplier discount", "supplier discounts", "was missed",
                                  "missed"),
    "duplicate_supplier_invoice": ("supplier invoice", "supplier invoices",
                                   "was entered twice", "entered twice"),
    "duplicate_supplier_payment": ("supplier payment", "supplier payments", "was made twice",
                                   "made twice"),
    "customer_credits_rising": ("customer", "customers", "started getting more credits",
                                "getting more credits"),
    "slow_moving_stock": ("item", "items", "stopped moving", "not moving"),
    "expiring_lot_stock": ("lot with stock", "lots with stock", "came near expiry",
                           "near expiry"),
    "assembly_blocked_by_component": ("assembly order", "assembly orders",
                                      "became short of components", "short of components"),
    "certificate_expiring": ("certificate", "certificates", "came near expiry",
                             "near expiry"),
    "duplicate_party_candidate": ("customer or supplier", "customers or suppliers",
                                  "entered twice, to confirm", "entered twice"),
    "key_field_missing": ("record", "records", "is missing a key field",
                          "missing a key field"),
    "twins_disagree": ("record", "records", "has systems disagreeing", "disagreeing"),
    "source_stale": ("source", "sources", "was not updated lately", "not updated lately"),
    "inventory_adjustment": ("item", "items", "was written down by hand", "written down"),
    "blocked_account_activity": ("blocked account", "blocked accounts", "has open orders",
                                 "with open orders"),
    "gl_posting_errors": ("ledger batch", "ledger batches", "failed to post",
                          "failed to post"),
    "recurring_task_missed": ("recurring check", "recurring checks", "missed a cycle",
                              "missed a cycle"),
    "unapplied_remittance": ("remittance", "remittances", "is not applied", "not applied"),
    "short_payment": ("short payment", "short payments", "paid short", "paid short"),
    "statement_difference": ("supplier statement", "supplier statements",
                             "differs from our books", "differing from our books"),
    "freight_cost_anomaly": ("freight bill", "freight bills", "came in far above usual",
                             "far above usual"),
    "price_below_typical": ("customer price", "customer prices", "fell far below the usual",
                            "far below the usual"),
    "customer_price_drop": ("customer price", "customer prices", "dropped", "dropped"),
    "price_below_cost": ("customer price", "customer prices", "fell below cost",
                         "below cost"),
    "stale_quote": ("quote", "quotes", "went stale with no order", "with no order"),
    "reorder_soon": ("item", "items", "needs reordering soon", "to reorder soon"),
    "priority_order_unfilled": ("priority-listed order", "priority-listed orders",
                                "is late or unscheduled", "late or unscheduled"),
}
TODAY_PARTS = 3   # the line names the worst types; the rest are "and N more"


def today_line(briefing: Mapping[str, Any]) -> str:
    """The briefing in one line, worst first: what is new (since the reader's last visit
    when it has one, else in the latest check) by type, else what is still open; the
    soonest expiry among lots; never a type the reader may not see named (only a count of
    what sits in places it can't open)."""
    if briefing.get("status") != "read":
        return "Your briefing is not available yet: the watch list has not run on this data."
    if (briefing.get("routing") or {}).get("basis") == "no_department_on_record":
        return "No department is on record for you, so nothing is routed to you yet."
    at_least = "" if briefing.get("counts_exact", True) else "at least "
    items = [i for i in briefing.get("items") or () if not i.get("acknowledged")]
    visit = bool(briefing.get("last_visit"))
    mark = "new_since_visit" if visit else "new_this_generation"
    new = [i for i in items if i.get(mark)]

    def parts(members: Sequence[Mapping[str, Any]], fresh: bool,
              counts: Mapping[str, int] | None = None) -> list[str]:
        out: list[str] = []
        types = list(dict.fromkeys(i["event_type"] for i in members
                                   if i.get("event_type") in TODAY_WORDS))
        for name in types:
            of = [i for i in members if i["event_type"] == name]
            n = (counts or {}).get(name, len(of))
            noun, nouns, said_new, said_open = TODAY_WORDS[name]
            words = f"{n} {noun if n == 1 else nouns} {said_new if fresh else said_open}"
            left = [d for i in of if isinstance(d := (i.get("facts") or {}).get("days"), int)]
            if EVENT_TYPES[name].get("days") == "left" and left:
                soon = min(left)
                words += (" (one expires today)" if soon == 0 else
                          f" (the first in {soon} day{'' if soon == 1 else 's'})")
            out.append(words.strip())
        shown = out[:TODAY_PARTS]
        if len(out) > TODAY_PARTS:
            more = len(out) - TODAY_PARTS
            shown.append(f"{more} more kind{'' if more == 1 else 's'}")
        return shown

    hidden = int(briefing.get("hidden") or 0)
    # what the reader can't open is a count only (``HIDDEN_POLICY``), never which kind
    unopened = f"; {hidden} more you can't open" if hidden else ""
    # checks last run on older data: their findings are held back, never "nothing"
    carried = int(briefing.get("carried") or 0)
    behind = (" Some checks are re-running on the latest data"
              + (f" (last checked {at_words(briefing['checked_on']) or str(briefing['checked_on'])[:10]})"
                 if briefing.get("checked_on") else "")
              + ": more can follow.") if briefing.get("refreshing") else ""
    if carried:
        behind += (f" {carried} item{' is' if carried == 1 else 's are'} from an earlier "
                   "check, marked with when it was last checked.")
    if new:
        lead = "Since your last visit: " if visit else "In the latest check: "
        return lead + "; ".join(parts(new, True)) + "." + behind
    if not items and behind:
        return "Checking against the latest data." + behind
    if not items:
        if hidden:
            return (f"Nothing you can open needs attention; {hidden} more for your team "
                    f"{'is' if hidden == 1 else 'are'} in places you can't open.")
        return "Nothing routed to you needs attention today."
    # the items list holds every ranked item unless it was cut at its limit (``more``):
    # then the groups' counts (test records the console leaves out are already gone)
    counts = ({g["event_type"]: int(g["count"]) - int(g.get("acknowledged") or 0)
               for g in briefing.get("groups") or ()} if briefing.get("more") else None)
    return ("Nothing new " + ("since your last visit" if visit else "in the latest check")
            + f"; still open: {at_least}" + "; ".join(parts(items, False, counts))
            + unopened + "." + behind)


def digest_text(briefing: Mapping[str, Any], *, first_name: str,
                labels: Mapping[str, str] | None = None, limit: int = DIGEST_ITEMS) -> str:
    """The daily digest this reader would get (Teams or email), as a preview: plain text
    built from the briefing's own words and cited facts: the top items one by one, the
    rest as counts by type, the checks not made whole, the hidden counts. Never sent."""
    labels = labels or {}
    name = " ".join(str(first_name or "").split())[:60]
    # the drafts' voice (``drafts``): "Hi <first name>," or no greeting
    hello = f"Hi {name},\n\n" if name else ""
    if briefing.get("status") != "read":
        return (f"{hello}Your briefing is not available yet: the watch list has "
                "not run on the current data.\n\n(Preview only: nothing is sent.)")
    count = int(briefing.get("count") or 0)
    lines = ([f"Hi {name},", ""] if name else []) + [today_line(briefing), ""]
    as_of = briefing.get("as_of")
    noun = "item needs" if count == 1 else "items need"
    at_least = "" if briefing.get("counts_exact", True) else "at least "
    head = f"{at_least}{count} {noun} your attention" + (f" as of {say_date(as_of)}"
                                                         if as_of else "")
    head = head[0].upper() + head[1:]
    new = int(briefing.get("new_since_visit") or 0)
    if briefing.get("last_visit"):
        head += f" ({new} new since your last visit)"
    lines.append(head + ".")
    if count:
        lines.append(str(briefing.get("rank_basis") or RANK_BASIS))
    lines.append("")
    items = list(briefing.get("items") or ())
    for item in items[:limit]:
        label = labels.get(str(item.get("object_id"))) or str(item.get("object_type") or
                                                                "Record")
        marks = (" [new]" if item.get("new_since_visit") else "") + \
            (" [acknowledged]" if item.get("acknowledged") else "")
        lines.append(f"{item['rank']}. {item['label']}: {label}{marks}")
        money = money_words(item.get("facts") or {})
        lines.append(f"   {item.get('summary') or ''}" + (f" ({money})" if money else "")
                     + (f" [{item['facts']['late_status']}]"
                        if (item.get("facts") or {}).get("late_status") else ""))
        if item.get("why"):
            lines.append(f"   Why: {item['why']}")
        lines.append(f"   Next: {item['next_action']['text']}")
    rest = [(g["label"], g["count"] - min(g["count"], sum(
        1 for i in items[:limit] if i["event_type"] == g["event_type"])))
        for g in briefing.get("groups") or ()]
    rest = [(label, n) for label, n in rest if n]
    if rest:
        lines += ["", "Also in the Workbench:"]
        lines += [f"- {label}: {n} more" for label, n in rest]
    tail = []
    other = int(briefing.get("other_teams") or 0)
    if other:
        tail.append(f"{other} more you may see {'is' if other == 1 else 'are'} routed to "
                    "other teams.")
    for kind, n in (briefing.get("hidden_by_type") or {}).items():
        spec = EVENT_TYPES.get(kind)
        if spec and n:
            tail.append(f"{spec['label']}: {n} more for your team "
                        f"{'is' if n == 1 else 'are'} in places you can't open (a count only).")
    for check in briefing.get("checks") or ():
        if check["status"] != "complete":
            tail.append(check["words"][0].upper() + check["words"][1:] + ".")
    if (briefing.get("routing") or {}).get("basis") == "no_department_on_record":
        tail.append("No department is on record for you, so nothing is routed to you yet.")
    if tail:
        lines += ["", *tail]
    lines += ["", "(Preview only: this digest is not sent.)"]
    return "\n".join(lines)
