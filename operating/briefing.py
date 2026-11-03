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
* **Ranking** (``RANK_BASIS``), over what the reader sees only: customer orders affected,
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
  ranked by and is shown only as "suspect source data (why)". Ties keep the inbox's order.
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
from typing import Any, Iterable, Mapping, Sequence

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
    "vendor_no_reply": {"kinds": ("vendor_no_reply",), "label": "Vendor has not replied",
                        "departments": ("PROC",), "check": ("vendor_no_reply",),
                        "hidden": "none"},
    "vendor_terms_unapplied": {"kinds": ("vendor_terms_unapplied",),
                               "label": "Vendor terms not applied",
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
}
TYPE_OF_KIND = {kind: name for name, spec in EVENT_TYPES.items() for kind in spec["kinds"]}
EVENT_KINDS = tuple(TYPE_OF_KIND)
# What may be said of the events of a type routed to a reader that it may NOT open
# (``HIDDEN_POLICY``): "count" (how many, never which, what or where) or "none" (not
# even that some exist). See the module doc.
HIDDEN_POLICY = {name: spec.get("hidden", "count") for name, spec in EVENT_TYPES.items()}
# A type whose days count down to a deadline (days left): its urgency is the horizon the
# check looks ahead (``proactive_events.EXPIRY_DAYS``) less the days left, so a lot expiring
# tomorrow ranks before one expiring in a month; every other type's days are days already
# passed (late, slipped, waiting), more first.
COUNTDOWN_HORIZON = 30

# The checks behind the event types, in words, as the briefing names them.
CHECK_WORDS = {"late_or_unconfirmed_po": "late purchase orders",
               "eta_slipped": "slipped delivery dates",
               "vendor_no_reply": "vendor replies",
               "vendor_terms_unapplied": "vendor terms in replies",
               "negative_margin": "order margins",
               "lot_attention": "lots near expiry and quality holds",
               "accepted_quote_no_order": "accepted quotes",
               "po_receipt_quantity_mismatch": "received against ordered quantities",
               "invoice_quantity_mismatch": "invoiced against received quantities",
               "orders_at_risk": "customer orders hit by late purchase orders"}
REASON_WORDS = {"time_budget": "time ran out", "event_bound": "only the most recent were read",
                "impact_bound": "only the first ones were followed",
                "card_bound": "only the first ones were read",
                "read_apart": "the rest wait for the vendor-mail pass"}

RANK_BASIS = ("Ranked by customer orders affected, then money involved (largest first within "
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
    "vendor_no_reply": ("Follow up with the vendor (a draft is in the investigation; nothing "
                        "is sent until you send it).", "no_reply_follow_up"),
    "vendor_terms_unapplied": ("Review the vendor's offered term and, if it is right, enter "
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
    with the rest; a suspect total, or one its own lines disagree with, has no place."""
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
    ordered.sort(key=lambda i: (i.acknowledged, -i.orders_affected, i.money_place,
                                not i.new_this_generation, -_urgency(i)))
    return ordered, RANK_BASIS


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
        return "failed", "could not be checked this run"
    return "not_checked", "not checked this run"


def _checks(inbox: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each check behind the event types with this run's state."""
    out = []
    for name in dict.fromkeys(d for spec in EVENT_TYPES.values() for d in spec["check"]):
        state, words = _check_state(inbox, name)
        out.append({"check": name, "label": CHECK_WORDS.get(name, name), "status": state,
                    "words": f"{CHECK_WORDS.get(name, name)}: {words}"})
    return out


def _types(inbox: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each event type: its checks' state this run (the least complete of them) and
    whether a recorded run over this data ever found one of THIS type (``found_live``,
    per type from the kinds' history; never said of a type whose existence is withheld)."""
    order = ("not_checked", "failed", "partial", "complete")
    kinds = inbox.get("kind_history") or {}
    out = []
    for name, spec in EVENT_TYPES.items():
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
          last_visit: str | None, limit: int = MAX_ITEMS, since_visit: bool = False
          ) -> dict[str, Any]:
    """One reader's briefing from its own trimmed inbox read (``read_inbox``, open and
    acknowledged) and the counts of events it may not see (``hidden_counts``): every
    routed item ranked (``items``, the first ``TOP_ITEMS`` shown one by one), the groups
    by type with their counts, each check's state and the hidden counts by type.
    ``since_visit``: only what is new since the reader's last visit."""
    if inbox.get("status") != "read":
        return {"schema": SCHEMA, "status": str(inbox.get("status") or "unavailable"),
                "items": [], "top": 0, "groups": [], "checks": [], "types": [], "count": 0,
                "new_since_visit": 0, "other_teams": 0, "hidden": None, "hidden_by_type": {},
                "rank_basis": RANK_BASIS, "routing": _routing_out(routing),
                "last_visit": last_visit, "as_of": None, "run_at": None, "complete": False,
                "since_visit": since_visit, "late_total": None,
                "today": today_line({"status": "unavailable"})}
    run_at = inbox.get("run_at") if isinstance(inbox.get("run_at"), str) else None
    mine: list[Item] = []
    others = 0
    for n, gap in enumerate(inbox.get("gaps") or ()):
        if gap.get("state", "open") not in ("open", "acknowledged"):
            continue
        item = _item(gap, n, last_visit, run_at)
        if item is None:
            continue
        if routing.routes(item.departments):
            mine.append(item)
        else:
            others += 1
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
            "groups": groups, "more": len(ordered) > limit,
            # the read stopped at ``MAX_READ``: the counts are "at least"
            "counts_exact": not inbox.get("more"),
            "new_since_visit": new, "since_visit": since_visit,
            "acknowledged": sum(1 for i in ordered if i.acknowledged),
            "late_total": late, "other_teams": others,
            "hidden": None if hidden_by_type is None else sum(hidden_by_type.values()),
            "hidden_by_type": hidden_by_type or {}, "last_visit": last_visit,
            "checks": checks, "types": types,
            "complete": bool(inbox.get("complete")) and not inbox.get("more"),
            "not_run": sorted((inbox.get("not_run") or {}).keys()),
            # checks whose findings are of older data than served now: not listed until
            # they run over it (``proactive_engine.read_inbox``)
            "refreshing": list(inbox.get("refreshing") or ()),
            "checked_on": inbox.get("checked_on")
            if isinstance(inbox.get("checked_on"), str) else None}
    found["today"] = today_line(found)
    return found


def read(inbox: Any, store: Any, *, reader: str, may_read: Any, held: Iterable[str],
         decide: Any, routing: Routing, limit: int = MAX_ITEMS,
         since_visit: bool = False) -> dict[str, Any]:
    """One reader's briefing from the inbox beside ``store``: its open and acknowledged
    events only (other gap kinds never use up the read), the events routed to it that it
    may not see as counts, and its last visit; the reader's grant is ``may_read``/
    ``held``/``decide``."""
    from . import proactive_engine as pe

    found = pe.read_inbox(inbox, store, reader=reader, may_read=may_read, held=held,
                          decide=decide, states=("open", "acknowledged"), limit=MAX_READ,
                          kinds=EVENT_KINDS)
    hidden = pe.hidden_counts(inbox, store, kinds=EVENT_KINDS, may_read=may_read, held=held,
                              decide=decide) if found.get("status") == "read" else None
    return build(found, routing=routing, hidden=hidden, last_visit=pe.last_visit(inbox, reader),
                 limit=limit, since_visit=since_visit)


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
    return {"rank": n, "id": gap.get("id"), "event_type": item.event_type,
            "label": spec["label"], "kind": gap.get("kind"),
            # its type found by a recorded run over this data, else fixture
            "evidence": "live" if found else "fixture-only",
            "object_id": gap.get("object_id"), "object_type": gap.get("object_type"),
            "summary": gap.get("summary"), "why": follow_up.get("why"),
            "next_action": {"text": text, "rule": rule, "suggestion_only": True},
            "facts": facts, "departments": item.departments,
            "citations": list(gap.get("citations") or ()),
            "acknowledged": item.acknowledged,
            "first_seen": gap.get("first_seen"), "last_seen": gap.get("last_seen"),
            "new_since_visit": item.new_since_visit,
            "new_this_generation": item.new_this_generation}


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
    "vendor_no_reply": ("vendor request", "vendor requests", "without a reply",
                        "without a reply"),
    "vendor_terms_unapplied": ("vendor term", "vendor terms", "not applied", "not applied"),
    "negative_margin": ("order", "orders", "lost money on item cost",
                        "lost money on item cost"),
    "lot_near_expiry": ("lot", "lots", "came near expiry", "near expiry"),
    "lot_on_hold": ("batch", "batches", "went on quality hold", "on quality hold"),
    "quote_accepted_no_order": ("accepted quote", "accepted quotes", "with no order",
                                "with no order"),
    "quantity_mismatch": ("quantity mismatch", "quantity mismatches", "found", "open"),
    "customer_order_at_risk": ("customer order", "customer orders",
                               "now at risk from late POs", "at risk from late POs"),
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
            shown.append(f"{len(out) - TODAY_PARTS} more kinds")
        return shown

    hidden = int(briefing.get("hidden") or 0)
    # what the reader can't open is a count only (``HIDDEN_POLICY``), never which kind
    unopened = f"; {hidden} more you can't open" if hidden else ""
    # checks last run on older data: their findings are held back, never "nothing"
    behind = (" Some checks are re-running on the latest data"
              + (f" (last checked {str(briefing['checked_on'])[:10]})"
                 if briefing.get("checked_on") else "")
              + ", so more may show shortly.") if briefing.get("refreshing") else ""
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
    name = " ".join(str(first_name or "").split())[:60] or "there"
    if briefing.get("status") != "read":
        return (f"Hello {name},\n\nYour briefing is not available yet: the watch list has "
                "not run on the current data.\n\n(Preview only: nothing is sent.)")
    count = int(briefing.get("count") or 0)
    lines = [f"Hello {name},", "", f"Today in one line: {today_line(briefing)}", ""]
    as_of = briefing.get("as_of")
    noun = "item needs" if count == 1 else "items need"
    at_least = "" if briefing.get("counts_exact", True) else "at least "
    head = f"{at_least}{count} {noun} your attention" + (f" as of {as_of}" if as_of else "")
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
        lines.append(f"   {item.get('summary') or ''}" + (f" ({money})" if money else ""))
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
