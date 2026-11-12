"""Alert playbooks: for every proactive alert a reader may see, the concrete next steps,
ready to use, worded from that alert's own cited sources as THIS reader reads them.

A playbook is built per alert type (``operating.briefing.EVENT_TYPES``) from one data table
(``PLAYBOOKS``); no entry names a record, a party or a case. For one alert it offers:

* ``email``: a drafted message to the outside party of the record the alert is about (the
  supplier of a purchase order, the customer of a sales order, invoice or quote), with the
  alert's facts and the cited record numbers filled in, addressed to the party's address
  read through the ontology (``PARTY``, ``PARTY_ADDRESSES``) when the reader may read it;
* ``teams``: a drafted note to the record's internal owner (buyer, rep, account owner) when
  the loaded schema declares an owner link for the record (``OWNER_LINKS``: only declared
  links are walked; none declared, the note names no owner and says so);
* ``links``: the record's page in this app, and its page in its source system where the
  configuration knows the pattern (``ontology/source_links.json``; a base URL from the
  environment, never guessed: otherwise the link is ``not_configured`` by reason);
* ``change``: the change a person makes in the source system themselves ("do this in BC"),
  with the values (reorder quantity, expedite date, credit hold, re-quote price). The
  Workbench never writes BC, the Portal or MC: ``writes`` is always 0;
* ``local``: snooze, assign to and mark handled, kept in the Workbench's own actions
  journal (``operating.card_actions``).

``card`` turns a playbook into a card ``operating.card_actions.ActionDesk`` accepts, so a
send goes only through its preview, an explicit confirm by the signed-in person, the
people-exclusion guard, the uncited-figure check and the sender's live-send gate.

Permissions and markings (fail closed): every read is the reader's own tool call
(``call``: ``plx.object``/``plx.around`` as the reader), and every value used is dropped
unless its markings are all held by the reader (``held``), even if a caller handed an
unfiltered read: a finance figure reaches a draft, a fact or a proposed change only for a
reader holding ``finance``, a party's address only for one holding its marking (``pii``).
An alert type without an entry, an unknown party type or an unreadable record gives a
playbook with only the local actions and the record page, never a guessed step.

Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from ontology.measure import money_text

from . import drafts
from .copy_gate import count, say_date, sentences

_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

SCHEMA = "plx-alert-playbook/v1"
CARD_PREFIX = "pb-"
LINKS_FILE = Path(__file__).resolve().parents[1] / "ontology" / "source_links.json"
MAX_WORD = 80          # a record number or party name as drafted (untrusted text, clipped)
MAX_LIST = 5           # related records named in one draft

# The outside party of a record: (its role word, the link from the record to the party).
PARTY = {"PurchaseOrder": ("supplier", "PurchaseOrder.ordered_from"),
         "SalesOrder": ("customer", "SalesOrder.ordered_by"),
         "SalesInvoice": ("customer", "SalesInvoice.billed_to"),
         "Quote": ("customer", "Quote.for_customer")}
# A party's addresses, in the order a draft uses them (each its own cited, marked value).
PARTY_ADDRESSES = {"Supplier": ("rep_email", "csr_email", "bc_email"),
                   "Customer": ("email", "bc_email", "email_2", "email_3")}
# A record's internal owner (buyer, rep, account owner): walked only when the loaded schema
# declares the link (``declared_owner_links``); its person's address is read like a party's.
OWNER_LINKS = {"PurchaseOrder": ("PurchaseOrder.buyer", "PurchaseOrder.purchaser"),
               "SalesOrder": ("SalesOrder.salesperson", "SalesOrder.account_owner"),
               "SalesInvoice": ("SalesInvoice.salesperson",),
               "Quote": ("Quote.salesperson",),
               "Customer": ("Customer.account_owner", "Customer.salesperson"),
               "Supplier": ("Supplier.buyer",),
               "Product": ("Product.buyer", "Product.planner")}
PERSON_ADDRESSES = ("email", "mail", "user_principal_name")
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Per alert type: which record the outside message is about (``about``: the alert's own
# subject, or the first record an event field names), what the message asks, what the
# owner note asks, and the change in the source system (``change``: the system's record,
# the field, and where its value comes from: an event field, or None when the person
# fills it in from the party's answer). ``{n}`` is the record's number as cited.
PLAYBOOKS: dict[str, dict[str, Any]] = {
    "po_late": {
        "about": "subject",
        "ask": "Purchase order {n} is past its requested date and not fully received. "
               "Please confirm when the open lines will ship and send the tracking details.",
        "owner": "Purchase order {n} is late: please chase the supplier for a ship date.",
        "change": {"text": "Set the expected receipt date on purchase order {n} to the date "
                           "the supplier confirms.", "field": "Expected Receipt Date",
                   "value": None}},
    "po_due": {"same_as": "po_late"},
    "po_undated": {
        "about": "subject",
        "ask": "Purchase order {n} has no delivery date on our side. Please confirm the "
               "date the order will ship.",
        "owner": "Purchase order {n} is open with no requested date: please set one.",
        "change": {"text": "Set the requested receipt date on purchase order {n}.",
                   "field": "Requested Receipt Date", "value": None}},
    "eta_slipped": {
        "about": "subject",
        "ask": "The delivery date of purchase order {n} moved from {date_was} to {date_now}. "
               "Can you bring it back to {date_was}, or ship what is ready?",
        "owner": "Purchase order {n}'s delivery moved from {date_was} to {date_now}: check "
                 "what depends on it.",
        "change": {"text": "Expedite purchase order {n}: ask for {date_was}; once agreed, set "
                           "its expected receipt date.", "field": "Expected Receipt Date",
                   "value": "date_was"}},
    "vendor_no_reply": {
        "about": "subject",
        "ask": "We have not had a reply about purchase order {n} for {days} days. Please "
               "confirm its status and delivery date.",
        "owner": "The supplier of purchase order {n} has not replied for {days} days.",
        "change": None},
    "vendor_terms_unapplied": {
        "about": "subject", "ask": None,
        "owner": "The supplier's terms are not applied on purchase order {n}: please check.",
        "change": {"text": "Apply the supplier's agreed terms on purchase order {n}.",
                   "field": "Payment Terms Code", "value": None}},
    "negative_margin": {
        "about": "subject", "ask": None,
        "owner": "Order {n} lost money on item cost: review the price before the next order.",
        "change": {"text": "Re-quote the price on order {n} before it is repeated.",
                   "field": "Unit Price", "value": None}},
    "lot_near_expiry": {
        "about": "subject", "ask": None,
        "owner": "Lot {n} expires in {days} days: use, rework or block it.",
        "change": {"text": "Block lot {n} if it will not be used before it expires.",
                   "field": "Blocked", "value": "yes"}},
    "lot_on_hold": {
        "about": "subject", "ask": None,
        "owner": "Batch {n} is on quality hold: decide release or rejection.",
        "change": {"text": "Release or reject batch {n} once quality decides.",
                   "field": "Status", "value": None}},
    "quote_accepted_no_order": {
        "about": "subject",
        "ask": "Quote {n} is accepted on our side. Can you send the purchase order so we can "
               "schedule it?",
        "owner": "Quote {n} was accepted {days} days ago and has no order.",
        "change": {"text": "Create the sales order from quote {n} once the customer's "
                           "purchase order arrives.", "field": "Make Order", "value": None}},
    "quantity_mismatch": {
        "about": "subject",
        "ask": "The quantities on purchase order {n} do not match what we received or were "
               "billed. Please check your shipment and invoice against the order.",
        "owner": "Purchase order {n} has a quantity mismatch: hold the invoice until it is "
                 "resolved.",
        "change": {"text": "Hold the purchase invoice for order {n} until the billed "
                           "quantity matches the received quantity.", "field": "On Hold",
                   "value": "yes"}},
    "customer_order_at_risk": {
        "about": "subject",
        "ask": "Order {n} will ship later than planned: material for it is late. We will "
               "confirm a new date once we have it.",
        "owner": "Late material holds up order {n}: {pos}.",
        "change": {"text": "Expedite {pos} for order {n}, or agree a later "
                           "shipment date.", "field": "Expected Receipt Date",
                   "value": "required"},
        # the late purchase order itself (its impact on the customer orders it holds up)
        "by_type": {"PurchaseOrder": {
            "ask": "Purchase order {n} is holding up {orders_said}. Can any of it ship "
                   "sooner? Please confirm the earliest date.",
            "owner": "Late purchase order {n} holds up {orders_said}: please expedite it.",
            "change": {"text": "Expedite purchase order {n}; once the supplier confirms, set "
                               "its expected receipt date.", "field": "Expected Receipt Date",
                       "value": None}}}},
    "customer_order_late": {
        "about": "subject",
        "ask": "Order {n} is past its required date {required}. We will confirm a new "
               "shipment date.",
        "owner": "Order {n} is past its required date {required}: agree a new date with the "
                 "customer.",
        "change": {"text": "Set the promised shipment date on order {n} to the date agreed "
                           "with the customer.", "field": "Promised Delivery Date",
                   "value": None}},
    "stock_out_risk": {
        "about": "inbound",
        "ask": "We need purchase order {n} sooner: please confirm whether it can arrive by "
               "{first_short}, and how much more you could supply.",
        "owner": "Item {item} is {shortfall} {unit} short by {short_by} for {orders_said}.",
        "change": {"text": "Buy {shortfall} {unit} more of item {item} (or raise an inbound "
                           "order), needed by {first_short}.", "field": "Quantity",
                   "value": "shortfall"}},
    "reorder_soon": {
        "about": "inbound",
        "ask": "We expect to need more soon: could purchase order {n} arrive earlier, and "
               "how much more could you supply by {short_by}?",
        "owner": "Item {item}: forecast demand is {shortfall} {unit} more than on hand plus "
                 "inbound by {short_by}.",
        "change": {"text": "Raise a purchase order for {shortfall} {unit} of item {item}, "
                           "needed by {short_by}.", "field": "Quantity",
                   "value": "shortfall"}},
    "overdue_receivable": {
        "about": "subject",
        "ask": "Invoice {n} for {amount} is {days} days past due. Please let us know when "
               "it will be paid, or send remittance details if it already has been.",
        "owner": "Invoice {n} for {amount} is {days} days overdue: please chase payment.",
        "change": {"text": "Put the customer of invoice {n} on credit hold until {amount} is "
                           "paid.", "field": "Blocked", "value": "credit_hold"}},
    "unit_cost_jump": {
        "about": "latest_order",
        "ask": "The unit cost on purchase order {n} is {change_pct}% away from what we have "
               "paid recently. Please confirm the price and whether it will hold.",
        "owner": "The purchase cost of item {item} moved {change_pct}% on order {n}: check "
                 "it before it is paid.",
        "change": {"text": "Re-quote the price on purchase order {n} with the supplier and "
                           "set the line's direct unit cost to the agreed price.",
                   "field": "Direct Unit Cost", "value": None}},
    # the chief-executive signals (``operating.proactive_ceo``): the subject is the party
    # itself (a customer or a supplier), or a product for a margin
    "customer_demand_drop": {
        "about": "subject", "finance": True,
        # the customer is asked about its needs; our figures stay inside
        "ask": "We noticed fewer orders from you recently than usual. Are your needs "
               "changing, or is there anything we can do better for upcoming orders?",
        "owner": "{n} is ordering {change_pct}% less than its usual run-rate ({amount} "
                 "below it): please check in with them.",
        "change": {"text": "Review the sales forecast for {n} and the purchases planned on "
                           "it.", "field": "Demand Forecast", "value": None}},
    "promise_date_slip": {
        "about": "subject",
        "ask": "{orders} of your recent orders missed the date we promised, the worst by "
               "{days} days. We are reviewing each one and will confirm new dates.",
        "owner": "{n}: {orders} recent orders missed their promised date (worst {days} "
                 "days late).",
        "change": {"text": "Set new promised dates on the {orders} slipped orders of {n} "
                           "once agreed with the customer.",
                   "field": "Promised Delivery Date", "value": None}},
    "margin_erosion": {
        "about": "subject", "finance": True, "ask": None,
        "owner": "Margin on {n} fell {change_points} points to {recent_pct}% (from "
                 "{trailing_pct}%), {amount} less: review prices and costs.",
        "change": {"text": "Re-quote the prices for {n}, or check the item costs behind the "
                           "drop.", "field": "Unit Price", "value": None},
        "by_type": {"Product": {
            "owner": "Margin on item {item} fell {change_points} points to {recent_pct}% "
                     "(from {trailing_pct}%), {amount} less: review its price and cost.",
            "change": {"text": "Re-quote the price of item {item}, or check its purchase "
                               "cost.", "field": "Unit Price", "value": None}}}},
    "supplier_reliability": {
        "about": "subject",
        "ask": "{late_share}% of your recent deliveries to us arrived after the due date, "
               "up from {late_share_before}% before. Can we review lead times and what is "
               "causing the delays?",
        "owner": "Supplier {n} delivers late more often: {late_share}% of receipts now, "
                 "{late_share_before}% before.",
        "change": {"text": "Lengthen the lead time for {n}'s items to cover its recent "
                           "delays, until it improves.", "field": "Lead Time Calculation",
                   "value": None}},
    "duplicate_customer_order": {"about": "subject", "ask": None,
        "owner": "{n} looks entered twice: check and cancel the duplicate.", "change": None},
    "recurring_component_damage": {"about": "subject", "ask": None,
        "owner": "{n} keeps being damaged: find the cause.", "change": None},
    "customer_chasing_order": {"about": "subject", "ask": None,
        "owner": "{n} has asked again about an order: reply with a firm date.", "change": None},
    "expedite_unanswered": {"about": "subject", "ask": None,
        "owner": "{n} was asked to be expedited and nobody answered: confirm a date.",
        "change": None},
    "supplier_shortage_open_orders": {"about": "subject", "ask": None,
        "owner": "{n}: the supplier announced a shortage or delay that hits open orders: "
                 "re-plan and tell the customers.", "change": None},
    "supplier_chasing_payment": {"about": "subject", "finance": True, "ask": None,
        "owner": "The supplier is chasing payment of {n}: pay it or give a date.",
        "change": None},
    "tell_before_they_ask": {"about": "subject", "ask": None,
        "owner": "{n}: tell the customer about the slip before they ask.", "change": None},
    "quality_hold_aging": {"about": "subject", "ask": None,
        "owner": "{n} has been on quality hold a long time: decide release or rejection.", "change": None},
    "reject_trend": {"about": "subject", "ask": None,
        "owner": "{n}: rejects are rising: look into the cause.", "change": None},
    "shipped_not_invoiced": {"about": "subject", "ask": None,
        "owner": "{n} shipped and has no invoice: invoice it.", "change": None},
    "customer_paying_slower": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} is paying slower than before: check in with them.", "change": None},
    "early_pay_discount_missed": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n}: an early-pay discount was missed: check payment timing.", "change": None},
    "duplicate_supplier_invoice": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} looks entered twice: check before paying.", "change": None},
    "customer_credits_rising": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n}: credits and returns are rising: review the account.", "change": None},
    "duplicate_supplier_payment": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} looks paid twice: ask the supplier for a refund or credit.", "change": None},
    "aged_receivable_worsening": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n}: overdue receivables are growing: follow up.", "change": None},
    "aged_payable_worsening": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n}: overdue payables are growing: review the payment plan.", "change": None},
    "mc_task_overdue": {"about": "subject", "ask": None,
        "owner": "{n} is overdue: finish it or move the date.", "change": None},
    "mc_task_stuck": {"about": "subject", "ask": None,
        "owner": "{n} has not moved: unblock it or reassign it.", "change": None},
    "mc_approval_pending": {"about": "subject", "ask": None,
        "owner": "{n} is waiting for an approval: decide it.", "change": None},
    "planner_task_overdue": {"about": "subject", "ask": None,
        "owner": "{n} is overdue: finish it or move the date.", "change": None},
    "production_late": {"about": "subject", "ask": None,
        "owner": "{n} is late in production: check the schedule.", "change": None},
    "assembly_damage_spike": {"about": "subject", "ask": None,
        "owner": "{n}: assembly damage jumped: look into the cause.", "change": None},
    "teams_unanswered_ask": {"about": "subject", "ask": None,
        "owner": "{n}: a Teams question has had no reply: answer it.", "change": None},
    "commitment_due": {"about": "subject", "ask": None,
        "owner": "{n}: a promise we made is due: keep it.", "change": None},
    "commitment_missed": {"about": "subject", "ask": None,
        "owner": "{n}: a promise we made was missed: tell the customer.", "change": None},
    "needs_my_reply": {"about": "subject", "ask": None,
        "owner": "{n} is waiting on your reply.", "change": None},
    "reply_owed": {"about": "subject", "ask": None,
        "owner": "{n} is waiting on your reply.", "change": None},
    # the briefing's label_change type (both kinds below): what the record still lacks
    "label_change": {"about": "subject", "ask": None,
        "owner": "{n}: a label or artwork change has no approval recorded or is not done: "
                 "record who approved it, or make it and tell the requester.", "change": None},
    "label_change_unapproved": {"about": "subject", "ask": None,
        "owner": "{n}: a label or artwork change has no approval recorded.", "change": None},
    "label_change_stale": {"about": "subject", "ask": None,
        "owner": "{n}: a label or artwork change requested is not done.", "change": None},
    # an open order on a superseded label revision (``operating.label_revisions``)
    "label_revision_superseded": {"about": "subject", "ask": None,
        "owner": "{n}: an open order points at a superseded label or artwork revision: "
                 "confirm it runs on the current one.", "change": None},
    # R&D and sample projects (``operating.rd_projects``): the board's idle alert
    "rd_project": {"about": "subject", "ask": None,
        "owner": "{n}: an R&D or sample project has had no activity: send the customer a "
                 "status update or close it.", "change": None},
    "rd_project_idle": {"about": "subject", "ask": None,
        "owner": "{n}: an R&D or sample project has had no activity: send the customer a "
                 "status update or close it.", "change": None},
    "rd_project_line": {"about": "subject", "ask": None,
        "owner": "{n}: an R&D or sample thread is tracked on the board.", "change": None},
    # mail threads waiting on someone (``operating.proactive_threads``): the nudge and the
    # holding reply are drafted per item by ``proactive_threads`` (never the thread's text)
    "approval_waiting": {"about": "subject", "ask": None,
        "owner": "An approval asked {days} business days ago has no answer: decide it or "
                 "say who should.", "change": None},
    "chase_escalating": {"about": "subject", "ask": None,
        "owner": "A thread was chased repeatedly with no answer for {days} days: "
                 "answer it today or say who owns it.", "change": None},
    "internal_order_chase": {"about": "subject", "ask": None,
        "owner": "A colleague chased an order and nobody it asked has answered for {days} "
                 "business days: answer them or say who has the answer.", "change": None},
    "party_chase_unanswered": {"about": "subject", "ask": None,
        "owner": "A customer or supplier chased us and nobody of ours has answered for {days} "
                 "business days: answer it today or say who owns it.", "change": None},
    # operations (``operating.proactive_stock``): stock and freight figures are finance-marked
    "slow_moving_stock": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} has stopped moving: decide whether to sell it off, use it, return it "
                 "or write it down.", "change": None},
    "expiring_lot_stock": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} has stock near expiry: plan it into an order, or ask Quality to "
                 "retest it.", "change": None},
    "assembly_blocked_by_component": {"about": "subject", "ask": None,
        "owner": "{n} is short of components: expedite them or move its start.",
        "change": None},
    "certificate_expiring": {"about": "subject", "ask": None,
        "owner": "{n} is near expiry: ask the supplier for a renewed certificate.",
        "change": None},
    "freight_cost_anomaly": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} is far above the usual freight cost: check its charges.",
        "change": None},
    # what each customer pays for the same product (``operating.proactive_prices``),
    # finance-marked
    "price_below_typical": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} is paying far below the usual price: check the agreed price.",
        "change": None},
    "customer_price_drop": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n}'s price dropped: check whether it was meant.", "change": None},
    "price_below_cost": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} is priced below cost: reprice the next order.", "change": None},
    # sales (``operating.proactive_sales``): the quote's value is finance-marked
    # priority / fill lists (``operating.business_documents``)
    "priority_order_unfilled": {"about": "subject", "finance": False, "ask": None,
        "owner": "{n} is on the customer's priority list but late or unscheduled: set a "
                 "ship date and tell the customer.", "change": None},
    "stale_quote": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} is open with no order: follow up with the customer, or close it.",
        "change": None},
    # data quality (``operating.proactive_quality``): a duplicate is a proposal a person
    # confirms, never a link made here
    "duplicate_party_candidate": {"about": "subject", "ask": None,
        "owner": "{n} may be entered twice: confirm or reject the match.", "change": None},
    "key_field_missing": {"about": "subject", "ask": None,
        "owner": "{n} is missing a key field: enter it in its system.", "change": None},
    "twins_disagree": {"about": "subject", "ask": None,
        "owner": "{n}: the systems disagree: correct the wrong one.", "change": None},
    "sync_missing": {"about": "subject", "ask": None,
        "owner": "{n} is not in its other system: key it there or link its copy.",
        "change": None},
    "sync_mismatch": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} differs in its other system: correct the wrong one.", "change": None},
    "source_stale": {"about": "subject", "ask": None,
        "owner": "{n} was not updated lately: check its capture.", "change": None},
    # integrity (``operating.proactive_integrity``): every one finance-marked but the tasks
    "inventory_adjustment": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} was written down by hand: find out why.", "change": None},
    "blocked_account_activity": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} is blocked and has open orders: hold them or lift the block.",
        "change": None},
    "gl_posting_errors": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} failed to post: correct and post it before close.", "change": None},
    "recurring_task_missed": {"about": "subject", "ask": None,
        "owner": "{n} missed its cycle: check it was done.", "change": None},
    # sheet readings (``operating.proactive_sheets``): finance-marked
    "unapplied_remittance": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} was paid by remittance: apply the payment.", "change": None},
    "short_payment": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} was paid short: find out why.", "change": None},
    "statement_difference": {"about": "subject", "finance": True, "ask": None,
        "owner": "{n} differs from the supplier's statement: reconcile it.", "change": None},
    "watcher_attention": {"about": "subject", "ask": None, "owner": None, "change": None},
}
# The figures a finance-marked alert type states: only for a reader holding ``finance``.
FINANCE_WORDS = ("amount", "change_pct", "change_points", "recent_pct", "trailing_pct")
LOCAL = ("snooze", "assign", "mark_done")


# --------------------------------------------------------------------------- helpers

def spec_of(event_type: str, object_type: str = "") -> Mapping[str, Any] | None:
    """The playbook entry of an alert type, with its variant for the record type the alert
    is about (``by_type``)."""
    spec = PLAYBOOKS.get(event_type)
    if spec and "same_as" in spec:
        spec = PLAYBOOKS.get(spec["same_as"])
    if spec and object_type in (spec.get("by_type") or {}):
        spec = {**spec, **spec["by_type"][object_type]}
    return spec


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


def _held(row: Mapping[str, Any], held: frozenset[str]) -> bool:
    marks = _marks(row.get("markings"))
    return marks is not None and set(marks) <= held


def _text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    if not isinstance(value, str):
        return None
    words = " ".join(value.split())[:MAX_WORD]
    return words or None


def _cited(value: Any) -> str | None:
    if isinstance(value, Mapping):
        citation = value.get("citation")
        if isinstance(citation, Mapping):
            return citation.get("l1_handle") if isinstance(citation.get("l1_handle"), str) \
                else None
        origin = value.get("origin")
        if isinstance(origin, Mapping) and isinstance(origin.get("citation"), str):
            return origin["citation"]
    return None


class _Reader:
    """The reader's own reads (``call``), each kept, every value checked against
    ``held``."""

    def __init__(self, call: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
                 held: Iterable[str]) -> None:
        self.call, self.held = call, frozenset(held)
        self._objects: dict[str, Mapping[str, Any] | None] = {}

    def object(self, oid: str) -> Mapping[str, Any] | None:
        if oid not in self._objects:
            try:
                found = self.call("plx.object", {"object_id": oid})
            except Exception:  # noqa: BLE001 - an unreadable record gives no step
                found = None
            self._objects[oid] = found if isinstance(found, Mapping) and isinstance(
                found.get("object"), Mapping) else None
        return self._objects[oid]

    def number(self, oid: str) -> tuple[str, str] | None:
        """(the record's display number, its citation) as this reader reads it."""
        found = self.object(oid)
        display = ((found or {}).get("object") or {}).get("display")
        words, handle = _text(display), _cited(display)
        return (words, handle) if words and handle else None

    def value(self, oid: str, props: Sequence[str]) -> tuple[str, str, str] | None:
        """(property, value, citation) of the first of ``props`` the reader may read."""
        found = self.object(oid)
        rows = [v for v in (found or {}).get("values") or () if isinstance(v, Mapping)]
        for prop in props:
            for row in rows:
                if row.get("property") == prop and _held(row, self.held):
                    words, handle = _text(row.get("value")), _cited(row)
                    if words and handle:
                        return prop, words, handle
        return None

    def linked(self, oid: str, link_type: str) -> list[str]:
        try:
            found = self.call("plx.around", {"from": oid, "path": [
                {"link_type": link_type, "direction": "forward"}]})
        except Exception:  # noqa: BLE001 - an unreadable link gives no party
            return []
        out = []
        for edge in (found or {}).get("edges") or ():
            if isinstance(edge, Mapping) and edge.get("from") == oid \
                    and edge.get("link_type") == link_type \
                    and edge.get("tier") in ("verified", "reviewed") \
                    and isinstance(edge.get("to"), str) and edge["to"] not in out:
                out.append(edge["to"])
        return out


def declared_owner_links() -> dict[str, tuple[str, ...]]:
    """``OWNER_LINKS`` the loaded schema declares (none declared: no owner is named)."""
    try:
        from ontology.schema import load_schema
        declared = set(load_schema().link_types)
    except Exception:  # noqa: BLE001 - no schema: no owner link is walked
        return {}
    return {t: tuple(l for l in links if l in declared) for t, links in OWNER_LINKS.items()
            if any(l in declared for l in links)}


def load_links(path: Path = LINKS_FILE) -> Mapping[str, Any]:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _type_of(oid: str) -> str:
    return oid.split(":", 1)[0]


def _binding(oid: str) -> str:
    parts = oid.split(":", 2)
    return parts[1] if len(parts) == 3 else ""


REPO = Path(__file__).resolve().parents[1]


def _company_names(path: str) -> Mapping[str, Mapping[str, str]]:
    """environment -> company id (lower case) -> the company's name, from a books file."""
    try:
        names = json.loads((REPO / path).read_text()).get("names") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {str(env).lower(): {str(k).lower(): str(v) for k, v in (ids or {}).items()}
            for env, ids in names.items() if isinstance(ids, Mapping)}


def _in_company(base: str, spec: Mapping[str, Any], handle: str | None) -> str | None:
    """``base`` with its company query parameter set to the company the record's cited
    ``handle`` is of (``spec``: the system's company block). None when the handle names no
    company the books file knows: the base's own company is never assumed."""
    match = re.match(str(spec.get("handle_pattern") or "^$"), handle or "")
    if match is None:
        return None
    name = _company_names(str(spec.get("names_file") or "")).get(
        match.group("environment").lower(), {}).get(match.group("company").lower())
    param = str(spec.get("query_param") or "")
    if not name or not param:
        return None
    parts = urlsplit(base)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, name))
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def source_links(oid: str, number: str | None, config: Mapping[str, Any],
                 environ: Mapping[str, str], handle: str | None = None
                 ) -> list[dict[str, Any]]:
    """The record's page in its source system, from the configured patterns: the system its
    binding is of, its page for the record type, and a base URL the environment holds
    (never a guessed host). A system with a ``company`` block opens the record in the
    company its cited number's ``handle`` is of. Otherwise one row saying why there is no
    link."""
    binding = _binding(oid)
    for system, spec in (config.get("systems") or {}).items():
        if not isinstance(spec, Mapping) or not any(
                binding.startswith(p) for p in spec.get("binding_prefixes") or ()):
            continue
        name = str(spec.get("label") or system)
        pattern = (spec.get("records") or {}).get(_type_of(oid))
        base = (environ.get(str(spec.get("base_env") or "")) or "").strip().rstrip("/")
        if not pattern:
            return [{"system": name, "url": None, "status": "not_configured",
                     "reason": f"no {name} page is configured for this record type"}]
        if not base.startswith("https://"):
            return [{"system": name, "url": None, "status": "not_configured",
                     "reason": f"{spec.get('base_env')} is not set"}]
        if isinstance(spec.get("company"), Mapping):
            base = _in_company(base, spec["company"], handle) or ""
            if not base:
                return [{"system": name, "url": None, "status": "not_configured",
                         "reason": f"the record's {name} company is not known"}]
        key = oid.split(":", 2)[2] if oid.count(":") >= 2 else ""
        if "{number}" in pattern and not number:
            return [{"system": name, "url": None, "status": "not_configured",
                     "reason": "the record's number is not readable for you"}]
        url = base + pattern.format(number=quote(number or "", safe=""),
                                    key=quote(key, safe=""))
        return [{"system": name, "url": url, "status": "ready", "reason": None}]
    return [{"system": binding.split(".", 1)[0] or "source", "url": None,
             "status": "not_configured", "reason": "no source link pattern for this system"}]


def _fill(template: str | None, words: Mapping[str, str]) -> str | None:
    """``template`` with every placeholder filled, or None when one is not known to this
    reader (a step is never worded around a missing fact)."""
    if not template:
        return None
    try:
        # a date is written "Wed 12 Nov" in anything a person reads, never ISO
        return template.format_map(_Strict({k: say_date(v) if isinstance(v, str)
                                             and _ISO_DAY.fullmatch(v) else v
                                             for k, v in words.items()}))
    except KeyError:
        return None


class _Strict(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def _money(amount: Any) -> str | None:
    """The event's amount as stated (``ontology.measure.money_text``): exact, never rounded
    to cents, never NaN; a figure without its three-letter currency is never stated."""
    if not isinstance(amount, Mapping):
        return None
    currency = amount.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        return None   # a figure without its currency is never stated
    return money_text(amount.get("value"), currency)


# --------------------------------------------------------------------------- build

def build(gap: Mapping[str, Any], event_type: str, *,
          call: Callable[[str, Mapping[str, Any]], Mapping[str, Any]], held: Iterable[str],
          environ: Mapping[str, str] | None = None, links: Mapping[str, Any] | None = None,
          owner_links: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
    """One alert's playbook for this reader. ``gap``: the alert as ``read_inbox`` gives it to
    this reader (already trimmed); ``call``: the reader's own tool call; ``held``: the
    markings the reader holds."""
    environ = environ or {}
    links = load_links() if links is None else links
    owner_links = declared_owner_links() if owner_links is None else owner_links
    held = frozenset(held)
    reader = _Reader(call, held)
    oid, otype = str(gap.get("object_id") or ""), str(gap.get("object_type") or "")
    event = gap.get("event") if isinstance(gap.get("event"), Mapping) else {}
    spec = spec_of(event_type, otype)
    facts: list[dict[str, str]] = []
    words: dict[str, str] = {}

    def fact(name: str, value: str, handle: str) -> None:
        if not any(f["fact"] == name for f in facts):
            facts.append({"fact": name, "value": value, "source": {"handle": handle},
                          "citation": handle})

    own = reader.number(oid)
    if own:
        words["item" if otype == "Product" else "n"] = own[0]
        fact("record", own[0], own[1])
    # the alert's own figures, as the reader's trimmed alert carries them
    for key in ("date_was", "date_now", "required", "short_by", "first_short", "due"):
        value = event.get(key)
        if isinstance(value, str) and _DAY.fullmatch(value):
            words[key] = value
    days = event.get("days", gap.get("days_late"))
    if isinstance(days, int) and not isinstance(days, bool):
        words["days"] = str(days)
    for key in ("shortfall", "change_pct", "change_points", "recent_pct", "trailing_pct",
                "late_share", "late_share_before"):
        if isinstance(event.get(key), str) and re.fullmatch(r"-?\d+(?:\.\d+)?", event[key]):
            words[key] = event[key].lstrip("-") if key in ("change_pct", "change_points") \
                else event[key]
    if isinstance(event.get("unit"), str) and event["unit"].strip():
        words["unit"] = _text(event["unit"]) or ""
    if isinstance(event.get("orders_affected"), int):
        words["orders"] = str(event["orders_affected"])
        words["orders_said"] = count(event["orders_affected"], "customer order")
    amount = event.get("amount")
    # a figure: only when the reader's trimmed alert still carries it AND, when it names
    # its citation, the reader holds that citation's markings
    if isinstance(amount, Mapping) and _money(amount) and _held(amount, held) \
            and (not amount.get("handle") or amount["handle"] in set(gap.get("citations") or ())):
        words["amount"] = _money(amount)
        if isinstance(amount.get("handle"), str):
            fact("amount", words["amount"], amount["handle"])
    if spec and spec.get("finance") and "finance" not in held:
        for key in FINANCE_WORDS:   # fail closed: whatever the alert's own markings say
            words.pop(key, None)
        facts[:] = [f for f in facts if f["fact"] != "amount"]
    pos = [p for p in (event.get("pos") or ()) if isinstance(p, str)][:MAX_LIST]
    named = [reader.number(p) for p in pos]
    if pos and all(named):
        words["pos"] = ", ".join(n[0] for n in named)

    # the record the outside message is about: the subject, or the first one the event names
    about: str | None = oid
    if spec and spec.get("about") not in (None, "subject"):
        field = event.get(spec["about"])
        if isinstance(field, (list, tuple)):
            field = next((x for x in field if isinstance(x, str)), None)
        about = field if isinstance(field, str) else None
        number = reader.number(about) if about else None
        if number:
            words["n"] = number[0]
            fact("about_record", number[0], number[1])
        else:
            about = None   # a record this reader may not open is never addressed

    plays: dict[str, Any] = {"email": None, "teams": None, "change": None}
    missing: list[str] = []
    if spec is None:
        missing.append("no playbook for this alert type")
    # (a) the outside party's email
    if spec and spec.get("ask") and about:
        role, link = PARTY.get(_type_of(about), (None, None))
        parties = reader.linked(about, link) if link else []
        if _type_of(about) in PARTY_ADDRESSES:   # the alert is about the party itself
            role, parties = _type_of(about).lower(), [about]
        party = parties[0] if len(parties) == 1 else None
        body = _fill(spec["ask"], words)
        to = None
        if party:
            name = reader.number(party)
            if name:
                words["party"] = name[0]
                fact("party", name[0], name[1])
            found = reader.value(party, PARTY_ADDRESSES.get(_type_of(party), ()))
            if found and _ADDRESS.fullmatch(found[1]):
                to = {"address": found[1].lower(), "source": found[2]}
                fact("party_address", found[1].lower(), found[2])
        if role is None:
            missing.append("the record has no outside party type")
        elif party is None:
            missing.append(f"no single settled {role} is linked to the record for you")
        elif to is None:
            missing.append(f"no {role} address you may read is recorded")
        if body is None:
            missing.append("a fact the message states is not readable for you")
        else:
            if _type_of(about) in PARTY_ADDRESSES:
                subject = "Your recent orders" if role == "customer" \
                    else "Your recent deliveries"
            else:
                subject = f"{'Order' if role == 'customer' else 'Purchase order'} " \
                          f"{words.get('n', '')}".strip()
            # one composer, the sender's voice: the party is a firm, so no greeting name
            draft = drafts.compose("playbook_ask", sentences(body), subject=subject,
                                   facts=[(f["fact"], f["value"]) for f in facts]
                                   + [(k, v) for k, v in words.items()],
                                   # a note about the party's account names no one record
                                   specific=_type_of(about) not in PARTY_ADDRESSES
                                   and "{n}" in str(spec["ask"]))
            if draft["body"] is None:   # never a failing draft: the alert shows, no draft
                missing.append("the drafted message did not pass the copy check")
            else:
                plays["email"] = {"channel": "email", "party": role, "to": to,
                                  "subject": subject, "status": "draft_not_sent",
                                  "body": draft["body"], "copy_check": draft["copy_check"]}
    # (b) the internal owner's Teams note
    if spec and spec.get("owner"):
        note = _fill(spec["owner"], words)
        owner = None
        for start in dict.fromkeys(x for x in (oid, about) if x):
            for link in owner_links.get(_type_of(start), ()):
                people = reader.linked(start, link)
                if len(people) == 1:
                    name = reader.number(people[0])
                    address = reader.value(people[0], PERSON_ADDRESSES)
                    if name:
                        owner = {"name": name[0], "address": address[1] if address else None,
                                 "link": link}
                        break
            if owner:
                break
        if note:
            if owner:
                note = f"{owner['name']}: {note}"
            plays["teams"] = {"channel": "teams", "owner": owner, "body": note,
                              "owner_known": owner is not None,
                              **({} if owner else {"why_no_owner": "the ontology records no "
                                                   "owner for this record"}),
                              "status": "draft_not_sent"}
    # (d) the change a person makes in the source system
    if spec and spec.get("change"):
        change = spec["change"]
        text = _fill(change["text"], words)
        source = _binding(about or oid).split(".", 1)[0]
        system = {"bc": "BC", "portal": "Portal", "fm": "FileMaker", "sage": "Sage"}.get(
            source, "the source system")
        value = change.get("value")
        shown = words.get(value) if value in words else (value if value in (
            "yes", "credit_hold") else None)
        if text:
            plays["change"] = {"system": system, "do_this": f"In {system}: {text}",
                               "field": change["field"], "value": shown,
                               "value_basis": value if value in words else (
                                   "fixed" if shown else "the person fills it in"),
                               "writes": 0, "by": "a person, in the source system"}
    # (c) deep links
    record_page = f"/object/{quote(otype, safe='')}/{quote(oid, safe='')}"
    deep = [{"system": "Workbench", "url": record_page, "status": "ready", "reason": None}]
    deep += source_links(oid, own[0] if own else None, links, environ,
                         own[1] if own else None)
    if about and about != oid:
        number = reader.number(about)
        deep.append({"system": "Workbench", "url": f"/object/{quote(_type_of(about), safe='')}"
                     f"/{quote(about, safe='')}", "status": "ready", "reason": None})
        deep += source_links(about, number[0] if number else None, links, environ,
                             number[1] if number else None)
    gid = str(gap.get("id") or "")
    return {"schema": SCHEMA, "id": CARD_PREFIX + gid, "alert_id": gid,
            "event_type": event_type, "kind": gap.get("kind"), "object_id": oid,
            "object_type": otype, "summary": gap.get("summary"),
            "email": plays["email"], "teams": plays["teams"], "change": plays["change"],
            "links": deep, "local": list(LOCAL), "facts": facts,
            "rests_on": list(gap.get("citations") or ()), "missing": missing,
            "sent": False, "writes": 0}


def card(playbook: Mapping[str, Any]) -> dict[str, Any]:
    """The playbook as a card ``operating.card_actions`` accepts: its email is the card's
    draft (sent only through preview and confirm), its owner note the Teams draft."""
    email = playbook.get("email") or {}
    draft = {"channel": "email", "to": email.get("to"), "subject": email.get("subject"),
             "body": email.get("body"), "facts": list(playbook.get("facts") or ())} \
        if email else {"channel": None, "facts": list(playbook.get("facts") or ())}
    teams = playbook.get("teams") or {}
    change = playbook.get("change") or {}
    return {"id": playbook["id"], "headline": str(playbook.get("summary") or "")[:300],
            "subject": {"label": str(playbook.get("summary") or "")[:200]},
            "action": {"text": change.get("do_this") or ""},
            "draft": draft, "teams_draft": {"body": teams["body"]} if teams else None,
            "playbook": True}
