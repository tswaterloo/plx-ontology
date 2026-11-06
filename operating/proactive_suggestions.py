"""Proactive suggestions: what needs attention, what to do about it and a drafted message,
each with how we know, its confidence and what is missing, ranked by business impact.

This is a composer over what one reader may already see; it reads no source and decides no
permission itself:

* the reader's briefing (``operating.briefing``: the gap inbox's events trimmed for the
  reader, routed and ranked: customer orders affected, then money within its currency, then
  what is new, then days) gives the findings and their order;
* the reader's ``late_order`` gaps (``proactive_engine.read_inbox``: a gap's supplier draft
  is kept only when the reader may open every fact it cites) give a purchase order's late
  lines (item, ordered and received quantities), its number and its supplier;
* a purchase order's lateness trail as the app states it for the reader
  (``operator_api.trail.Mapper.po``): every step the reader may not open is dropped and
  counted, a message's sender and the next action's vendor contact are present only when the
  people directory clears the address (an excluded person, and anyone undecided, never is).

One suggestion per record: the findings about one purchase order (late, due, slipped date,
no vendor reply, the customer orders it holds up) are merged into one card. Each card says:

* ``headline`` and ``impact`` (the cited amount, days late, the customer orders at risk);
* ``eta``: "ETA confirmed?" as yes / no / possibly / conflicting / hidden / unknown, when,
  and the confirmation that says so (a mail or a record) when the reader can open it;
* ``reasoning``: the chain, each link a sentence with the source it rests on;
* ``action``: the suggested next step, and ``draft``: a message a person may copy, edit and
  send themselves. Drafts are worded only from cited values (record numbers, dates, items,
  quantities) and from a vendor-side sender the trail already cleared; never from a
  message's text, subject or quote, and never from a step whose sender was not cleared.
  ``sent`` is always False: nothing here sends, writes or proposes anything;
* ``confidence`` (high / medium / low, with why) and ``missing`` (what the reader would need
  to be sure).

Kinds of suggestion (``KIND_OF_TYPE``): ``vendor_chase`` (a late, due or slipped purchase
order, or a vendor that has not replied), ``customer_update`` (a customer order a late
purchase order puts at risk: an update the account owner may send), ``margin_review`` (an
order that lost money on item cost: an internal note). Any other event type is not a
suggestion; it stays counted in ``collapsed`` by type, never dropped silently.

Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "plx-suggestions/v1"
TOP = 12                 # suggestions composed one by one
MAX_DRAFT_LINES = 8      # item lines a draft lists (the rest counted)
MAX_REASONS = 8
DRAFT_STATUS = "draft_not_sent"

KIND_OF_TYPE = {
    "po_late": "vendor_chase", "po_due": "vendor_chase", "eta_slipped": "vendor_chase",
    "vendor_no_reply": "vendor_chase", "po_undated": "vendor_chase",
    "customer_order_at_risk": "customer_update", "negative_margin": "margin_review",
    "customer_order_late": "customer_update",
}
# Which finding leads a merged card (lower first): the one that names the problem best.
_LEAD = {"po_late": 0, "eta_slipped": 1, "vendor_no_reply": 2, "po_due": 3,
         "customer_order_at_risk": 4, "po_undated": 5, "negative_margin": 0,
         "customer_order_late": 0}
ETA_ANSWERS = {"confirmed": "yes", "no_confirmation": "no", "no_confirmation_found": "no",
               "lead_only": "possibly", "promises_disagree": "conflicting",
               "withheld": "hidden"}
_DAY = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# Where a due date comes from, as a card says it (``po_status`` ``Need.basis``).
_BASIS_SAID = {"requested": "the requested-by date on the order",
               "vendor_eta": "the supplier's confirmed date; no requested-by date is on record",
               "lead_time": "the order date plus its lead time; no requested-by date is on "
                            "record"}


def _summary_due(summary: str) -> tuple[str, str] | None:
    """The due date and its basis a late or unconfirmed finding's own summary states, as
    the engine words it (``proactive_engine._late_summary``, ``_unconfirmed_summary``);
    None when it states none."""
    from datetime import date as _date
    from .proactive_engine import _late_summary, _unconfirmed_summary
    marker = _date(1, 1, 1)
    for basis in _BASIS_SAID:
        for words in (_late_summary, _unconfirmed_summary):
            head, _, tail = words(basis, marker).partition(marker.isoformat())
            found = re.match(re.escape(head) + r"(\d{4}-\d{2}-\d{2})" + re.escape(tail),
                             summary)
            if found:
                return found.group(1), basis
    return None


_REQUIRED = re.compile(r"required date (\d{4}-\d{2}-\d{2})")


def _get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _po_trail(trail: Any) -> Mapping[str, Any] | None:
    """The first purchase-order trail of an evidence trail (the app's ``EvidenceTrail``
    as a dict), else None."""
    trails = _get(trail, "trails")
    if isinstance(trails, list) and trails and isinstance(trails[0], Mapping):
        return trails[0]
    return None


def _src(source: Any) -> dict[str, Any] | None:
    """A cited source as the card names it (handle, label, system, open link)."""
    if not isinstance(source, Mapping) or not isinstance(source.get("handle"), str):
        return None
    return {k: source.get(k) for k in ("handle", "label", "system", "open_url")}


# --------------------------------------------------------------------------- ETA confirmed?

def eta_answer(po: Mapping[str, Any] | None) -> dict[str, Any]:
    """"ETA confirmed?" for one purchase order from its reader-decided trail: ``answer``
    yes / no / possibly / conflicting / hidden / unknown (an unknown state fails closed to
    unknown, never yes), ``when`` (the vendor's date, only when the reader may open the
    confirmation), ``in_mail`` (the confirmation is a message), ``source`` and ``text``."""
    if po is None:
        return {"answer": "unknown", "when": None, "in_mail": False, "source": None,
                "text": "Not checked: the purchase order's trail was not read in time."}
    eta = po.get("eta")
    if not isinstance(eta, Mapping):
        return {"answer": "unknown", "when": None, "in_mail": False, "source": None,
                "text": "Not determined: the record does not say whether the vendor "
                        "confirmed a date."}
    state = str(eta.get("state") or "")
    answer = ETA_ANSWERS.get(state, "unknown")
    source = _src(eta.get("source"))
    if answer == "yes" and source is None:
        # a confirmation stated without its record: never read as confirmed
        return {"answer": "unknown", "when": None, "in_mail": False, "source": None,
                "text": "A vendor confirmation is stated, but its record is not available "
                        "to you, so it is not counted."}
    promise = eta.get("promise") if isinstance(eta.get("promise"), Mapping) else {}
    when = promise.get("promised_date") if isinstance(promise.get("promised_date"), str) \
        else None
    if answer == "yes" and when is None:
        found = _DAY.search(str(eta.get("text") or ""))
        when = found.group(1) if found else None
    # a date is said only beside a confirmation this reader may open
    when = when if answer == "yes" and source is not None else None
    return {"answer": answer, "when": when,
            "in_mail": bool(source and source.get("system") == "Email"),
            "source": source, "text": str(eta.get("text") or "")[:300]}


def last_vendor_message(po: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The latest message on the trail whose sender the people directory cleared
    (``TrailStep.sender`` set) and whose source this reader may open: its date, sender and
    source. A step without a cleared sender is never used (fail closed)."""
    best = None
    for step in (po or {}).get("steps") or ():
        if not isinstance(step, Mapping) or step.get("kind") not in (
                "follow_up", "confirmation", "revision", "communication"):
            continue
        sender, source, at = step.get("sender"), _src(step.get("source")), step.get("at")
        if not sender or source is None or not isinstance(at, str):
            continue
        if best is None or at > best["at"]:
            best = {"at": at[:10], "sender": sender, "source": source}
    return best


# --------------------------------------------------------------------------- drafts

def _draft_facts(draft: Mapping[str, Any] | None) -> dict[str, Any]:
    """A late-order supplier draft's cited facts by name: supplier, order number, and the
    lines (item, expected date, ordered and received quantity with unit)."""
    out: dict[str, Any] = {"lines": {}}
    for fact in (draft or {}).get("facts") or ():
        if not isinstance(fact, Mapping) or not isinstance(fact.get("citation"), str):
            continue
        name, value = fact.get("fact"), fact.get("value")
        line = fact.get("line_id")
        if line:
            row = out["lines"].setdefault(line, {})
            row[name] = (value, fact.get("unit"), fact["citation"])
        elif name in ("supplier", "order_number", "confirmed_delivery_date"):
            out[name] = (value, fact["citation"])
        elif name == "due_date" and fact.get("basis") in _DUE_SAID:
            out["due_date"], out["due_basis"] = (value, fact["citation"]), fact["basis"]
    return out


def _qty(entry: Any) -> str:
    value, unit = entry[0], entry[1]
    return f"{value} {unit}".strip() if unit else str(value)


# How a draft states the due date by its basis (``po_status`` ``Need.basis``).
_DUE_SAID = {"requested": "was needed by {}", "vendor_eta": "was due by {}, your confirmed date",
             "lead_time": "was due by {} (its lead time)"}


def vendor_draft(*, po_number: str, supplier: str | None, need_date: str | None,
                 eta: Mapping[str, Any], contact: Mapping[str, Any] | None,
                 last_message: Mapping[str, Any] | None, lines: Sequence[Mapping[str, Any]],
                 cited: Sequence[Mapping[str, Any]], received_any: bool | None = None,
                 need_basis: str = "requested") -> dict[str, Any]:
    """The follow-up message to a vendor, from cited facts only (never sent). ``need_date``
    is the order's due date with its ``need_basis`` (the one PO reader's), never a line's
    default date; "no confirmation on file" is said only when ``eta`` says none is linked
    to any copy of the order."""
    if contact and contact.get("address"):
        to = {"name": supplier, "address": contact["address"], "basis": contact["basis"],
              "source": contact.get("source")}
    else:
        to = {"name": supplier, "address": None,
              "basis": "No vendor contact address in the records and mail you can open: "
                       "add the vendor's address before sending."}
    if eta.get("answer") == "yes" and eta.get("when"):
        subject = f"{po_number}: your confirmed delivery date {eta['when']} has passed"
        opening = (f"You confirmed delivery of our purchase order {po_number} for "
                   f"{eta['when']}. We have not received it in full yet.")
    else:
        subject = f"{po_number}: please confirm the delivery date"
        opening = (f"Our purchase order {po_number} "
                   + _DUE_SAID.get(need_basis, _DUE_SAID["requested"]).format(need_date)
                   if need_date else f"Our purchase order {po_number} is open")
        opening += (" and is not yet received in full." if need_date else ".")
        if eta.get("answer") == "no":
            opening += " We do not have a delivery confirmation from you on file."
        elif eta.get("answer") == "conflicting":
            opening += " The delivery dates we have from you disagree."
    body = [f"Hello{' ' + supplier if supplier else ''},", "", opening]
    if last_message:
        body += ["", f"Following up on your message of {last_message['at']}."]
    if lines:
        body += ["", "Open items:"]
        for row in lines[:MAX_DRAFT_LINES]:
            body.append(f"- {row['text']}")
        if len(lines) > MAX_DRAFT_LINES:
            body.append(f"- and {len(lines) - MAX_DRAFT_LINES} more open lines")
    body += ["", "Please reply with a confirmed delivery date for each open item, or tell us "
                 "now if any part cannot ship.", "", "Thank you,"]
    return {"status": DRAFT_STATUS, "sent": False, "channel": "email", "audience": "vendor",
            "to": to, "subject": subject[:200], "body": "\n".join(body)[:2000],
            "facts": list(cited)}


def customer_draft(*, order_label: str, required: str | None, eta: Mapping[str, Any] | None,
                   cited: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """An update the account owner may send the customer about an order a late purchase
    order puts at risk; it names no supplier, price or internal record (never sent)."""
    when = (eta or {}).get("when") if (eta or {}).get("answer") == "yes" else None
    lines = ["Hello,", "",
             f"An update on your order {order_label}"
             + (f", which you need by {required}." if required else "."),
             "", "One of the materials for it is arriving later than planned"
             + (f"; our supplier has confirmed delivery for {when}." if when else "."),
             "We will confirm a revised ship date as soon as we have it, and we can discuss "
             "a partial shipment if that helps.", "", "Thank you for your patience,"]
    return {"status": DRAFT_STATUS, "sent": False, "channel": "email", "audience": "customer",
            "to": {"name": None, "address": None,
                   "basis": "The customer's contact is not in the records you can open: the "
                            "account owner sends this."},
            "subject": f"Update on your order {order_label}"[:200],
            "body": "\n".join(lines)[:2000], "facts": list(cited),
            "review": "Confirm the new date with Purchasing before sending."}


def margin_note(*, order_label: str, amount: Mapping[str, Any] | None, why: str | None,
                cited: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """An internal note to Sales and Finance about an order that lost money (never sent)."""
    loss = (f" lost {amount['value']} {amount['currency']} on item cost"
            if amount and not amount.get("suspect") else " lost money on item cost")
    body = [f"Order {order_label}{loss}."]
    if why:
        body += ["", f"Why: {why}"]
    body += ["", "Please review the item's price against its current cost before this "
                 "customer's next order."]
    return {"status": DRAFT_STATUS, "sent": False, "channel": "internal_note",
            "audience": "internal", "to": {"name": "Sales and Finance", "address": None,
                                           "basis": "internal"},
            "subject": f"Margin alert: {order_label}"[:200], "body": "\n".join(body)[:2000],
            "facts": list(cited)}


# --------------------------------------------------------------------------- compose

def _money(item: Mapping[str, Any]) -> dict[str, Any] | None:
    amount = _get(item.get("facts"), "amount")
    if not isinstance(amount, Mapping) or amount.get("value") is None:
        return None
    return {"value": str(amount["value"]), "currency": amount.get("currency"),
            "basis": amount.get("basis"),
            **({"suspect": str(amount["suspect"])} if amount.get("suspect") else {})}


def group(items: Sequence[Mapping[str, Any]]) -> tuple[list[list[Mapping[str, Any]]],
                                                        dict[str, int]]:
    """The briefing's items as one group per record, by business impact (``_TIERS``) and
    then the briefing's rank order (a group's place is its best-ranked finding's); items of a type that is no suggestion are
    counted per type in the second value, never dropped silently. A customer order at risk
    keeps its own card (the customer update); its late purchase order's card lists it."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    order: list[str] = []
    collapsed: dict[str, int] = {}
    for item in sorted(items, key=lambda i: int(i.get("rank") or 0)):
        kind = KIND_OF_TYPE.get(str(item.get("event_type")))
        oid = item.get("object_id")
        if kind is None or not isinstance(oid, str):
            key = str(item.get("event_type"))
            collapsed[key] = collapsed.get(key, 0) + 1
            continue
        if oid not in groups:
            groups[oid] = []
            order.append(oid)
        groups[oid].append(item)
    # business impact first (``_TIERS``), the briefing's order within a tier; a record
    # whose every finding is acknowledged after the others
    ranked = sorted(order, key=lambda o: (
        all(bool(m.get("acknowledged")) for m in groups[o]), _tier(groups[o]),
        min(int(m.get("rank") or 0) for m in groups[o])))
    return [groups[o] for o in ranked], collapsed


def compose(members: Sequence[Mapping[str, Any]], *, label: str,
            labels: Mapping[str, str] | None = None, trail: Any = None,
            late_draft: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One suggestion card from one record's findings (``group``), its reader-decided
    trail (the app's ``EvidenceTrail`` as a dict, or None) and its reader-trimmed late-order
    supplier draft (or None)."""
    labels = labels or {}
    own = [m for m in members if m.get("object_id") == members[0].get("object_id")]
    lead = min(own, key=lambda m: (_LEAD.get(str(m.get("event_type")), 9),
                                   int(m.get("rank") or 0)))
    etype = str(lead.get("event_type"))
    oid, otype = str(lead["object_id"]), str(lead.get("object_type") or "")
    # a finding about a purchase order is the vendor's to answer, whatever its type
    kind = "vendor_chase" if otype == "PurchaseOrder" else KIND_OF_TYPE[etype]
    po = _po_trail(trail) if otype == "PurchaseOrder" else None
    facts = _draft_facts(late_draft)
    money = next((x for x in (_money(m) for m in own) if x), None)
    # days late are a lateness finding's own (an at-risk finding's days are days at risk)
    days = next((int(m["facts"]["days"]) for m in own
                 if isinstance(_get(m.get("facts"), "days"), int)
                 and (m.get("event_type") != "customer_order_at_risk"
                      or otype != "PurchaseOrder")), None)
    risk_days = max((int(m["facts"]["days"]) for m in members
                     if m.get("event_type") == "customer_order_at_risk"
                     and isinstance(_get(m.get("facts"), "days"), int)), default=None)
    if po is not None and isinstance(po.get("days_late"), int):
        days = po["days_late"]
    at_risk = [m for m in members if m.get("event_type") == "customer_order_at_risk"]
    orders = []
    for m in at_risk:
        targets = list(_get(m.get("links"), "orders") or ()) \
            if m.get("object_type") == "PurchaseOrder" else [m["object_id"]]
        for t in targets:
            if t not in orders:
                orders.append(t)
    orders_affected = max([len(orders)] + [int(_get(m.get("facts"), "orders_affected") or 0)
                                           for m in own])
    reasons: list[dict[str, Any]] = []
    missing: list[str] = []
    cited: list[dict[str, Any]] = []

    def reason(text: str, source: Any = None) -> None:
        if len(reasons) < MAX_REASONS:
            reasons.append({"text": text, "source": _src(source)})

    def cite(name: str, value: str, handle: str | None) -> None:
        if handle:
            cited.append({"fact": name, "value": value, "citation": handle})

    for m in own:
        handle = next((h for h in m.get("citations") or () if isinstance(h, str)), None)
        reason(str(m.get("summary") or m.get("label") or ""),
               {"handle": handle, "label": None, "system": None, "open_url": None}
               if handle else None)
    eta = eta_answer(po) if otype == "PurchaseOrder" else None
    draft = None
    action_text = str(_get(lead.get("next_action"), "text") or "")
    action_rule = str(_get(lead.get("next_action"), "rule") or "")
    if po is not None:
        due = _get(po, "due")
        if isinstance(due, Mapping) and _src(due.get("source")) and due.get("date"):
            said = _BASIS_SAID.get(str(due.get("basis")), _BASIS_SAID["requested"])
            reason(f"Due by {due['date']} ({said}).", due["source"])
        if isinstance(po.get("verdict"), str):
            reason(po["verdict"])
        if eta and eta["answer"] != "unknown":
            reason(f"ETA confirmed? {eta['answer']}"
                   + (f", {eta['when']}" if eta.get("when") else "") + f". {eta['text']}",
                   eta.get("source"))
        nxt = po.get("next_action")
        if isinstance(nxt, Mapping) and nxt.get("text"):
            action_text, action_rule = str(nxt["text"]), str(nxt.get("rule") or action_rule)
        for gap in po.get("not_found") or ():
            if isinstance(gap, Mapping) and gap.get("what"):
                missing.append(str(gap["what"]))
        if int(po.get("hidden_steps") or 0):
            n = int(po["hidden_steps"])
            missing.append(f"{n} more {'step sits' if n == 1 else 'steps sit'} in places "
                           "you can't open.")
        if po.get("may_hide_more"):
            missing.append("Mail or files you can't open may hold a later vendor message.")
    elif otype == "PurchaseOrder":
        missing.append("The purchase order's trail (confirmations, messages) was not read "
                       "in time; open the order for it.")
    if orders and otype == "PurchaseOrder":
        names = [labels.get(o) or "a customer order" for o in orders[:5]]
        reason(f"It holds up {len(orders)} customer order{'s' if len(orders) != 1 else ''}: "
               + ", ".join(names) + ("" if len(orders) <= 5 else ", ..."))
    for m in at_risk:
        if m.get("why"):
            reason(str(m["why"]))
    if kind == "vendor_chase":
        # the number the supplier knows: the late-order draft's (the engine's), else the
        # trail's (both ``po_status.vendor_facing``), else the record's own label
        number = facts.get("order_number")
        known = _get(po, "vendor_number")
        if number is None and isinstance(known, Mapping) and _src(known.get("source")):
            number = (str(known["value"]), known["source"]["handle"])
        po_number = number[0] if number else label
        if number:
            cite("order_number", number[0], number[1])
        supplier = facts.get("supplier")
        if supplier:
            cite("supplier", supplier[0], supplier[1])
        lines = []
        for n, line in enumerate(facts["lines"].values(), 1):
            item = line.get("line_item")
            if "ordered_quantity" not in line:
                continue
            # a line whose record names no item is listed by its place and quantities
            text = f"{item[0] if item else f'Line {n}'}: ordered {_qty(line['ordered_quantity'])}"
            if "received_quantity" in line:
                text += f", received {_qty(line['received_quantity'])}"
            if "expected_receipt" in line:
                text += f" (needed by {line['expected_receipt'][0]})"
            lines.append({"text": text})
            if item:
                cite("line_item", item[0], item[2])
            cite("ordered_quantity", _qty(line["ordered_quantity"]),
                 line["ordered_quantity"][2])
            if "received_quantity" in line:
                cite("received_quantity", _qty(line["received_quantity"]),
                     line["received_quantity"][2])
            if "expected_receipt" in line:
                cite("expected_receipt", line["expected_receipt"][0],
                     line["expected_receipt"][2])
        # the due date and its basis are the one PO reader's (the trail's ``due``, else
        # the late-order draft's, else the finding's own summary), never a line's date
        need_date, need_basis = None, "requested"
        due = _get(po, "due")
        if isinstance(due, Mapping) and _src(due.get("source")) and due.get("date"):
            need_date, need_basis = str(due["date"]), str(due.get("basis") or "requested")
            cite("need_date", need_date, due["source"]["handle"])
        elif facts.get("due_date"):
            need_date, need_basis = facts["due_date"][0], facts["due_basis"]
            cite("need_date", need_date, facts["due_date"][1])
        else:
            # the finding's own summary states the due date it was judged on (cited)
            for m in own:
                found = _summary_due(str(m.get("summary") or ""))
                handle = next((h for h in m.get("citations") or () if isinstance(h, str)),
                              None)
                if found and handle:
                    need_date, need_basis = found
                    cite("need_date", need_date, handle)
                    break
        message = last_vendor_message(po)
        contact = None
        nxt = (po or {}).get("next_action")
        if isinstance(nxt, Mapping) and nxt.get("contact") and _src(nxt.get("contact_source")):
            address = _address(str(nxt["contact"]))
            if address:
                contact = {"address": address, "source": _src(nxt.get("contact_source")),
                           "basis": "The vendor-side sender of the latest message about "
                                    "this order you can open."}
        if message:
            cite("last_vendor_message", message["at"], message["source"]["handle"])
            reason(f"Last cleared message about it: {message['at']}.", message["source"])
        else:
            missing.append("No vendor message about this order that you can open "
                           "(the draft does not refer to one).")
        if eta and eta["answer"] == "yes" and eta.get("when") and eta.get("source"):
            cite("confirmed_delivery_date", eta["when"], eta["source"]["handle"])
        if not lines:
            missing.append("The order's open lines are not listed here (no line you can "
                           "open whole); the draft names the order only.")
        elif not any(line.get("line_item") for line in facts["lines"].values()):
            missing.append("The order's lines name no item in the records: the draft lists "
                           "them by quantity.")
        if contact is None:
            missing.append("The vendor's contact address.")
        draft = vendor_draft(po_number=po_number, supplier=supplier[0] if supplier else None,
                             need_date=need_date, eta=eta or {}, contact=contact,
                             last_message=message, lines=lines, cited=cited,
                             need_basis=need_basis)
    elif kind == "customer_update":
        event = lead.get("links") or {}
        required = event.get("required") if isinstance(event.get("required"), str) else None
        if required is None:
            found = _REQUIRED.search(str(lead.get("why") or ""))
            required = found.group(1) if found else None
        handle = next((h for h in lead.get("citations") or () if isinstance(h, str)), None)
        if required:
            cite("required_date", required, handle)
        else:
            missing.append("The order's required date is not stated in this finding.")
        draft = customer_draft(order_label=label, required=required, eta=None, cited=cited)
    elif kind == "margin_review":
        handle = next((h for h in lead.get("citations") or () if isinstance(h, str)), None)
        if money and handle:
            cite("loss", f"{money['value']} {money['currency']}", handle)
        draft = margin_note(order_label=label, amount=money, why=lead.get("why"), cited=cited)
    if eta is not None and eta["answer"] == "unknown" and po is not None:
        missing.append("Whether the vendor confirmed a date is not decided from what you "
                       "can see.")
    if money and money.get("suspect"):
        missing.append(f"The order total looks wrong in the source ({money['suspect']}); "
                       "it is not used to rank.")
    confidence = _confidence(po, eta, money, kind, lead)
    headline = _headline(kind, label, days, eta, orders_affected, etype, risk_days)
    return {"id": str(lead.get("id")), "kind": kind, "event_types":
            sorted({str(m.get("event_type")) for m in members}),
            "rank": min(int(m.get("rank") or 0) for m in members),
            "tier": _tier(members), "headline": headline,
            "subject": {"object_id": oid, "object_type": otype, "label": label},
            "impact": {"amount": money, "days_late": days, "days_at_risk": risk_days,
                       "orders_at_risk": orders_affected,
                       "orders": [{"object_id": o, "label": labels.get(o)} for o in orders]},
            "eta": eta, "reasoning": reasons,
            "action": {"text": action_text[:400], "rule": action_rule,
                       "suggestion_only": True},
            "draft": draft, "confidence": confidence,
            "missing": list(dict.fromkeys(missing))[:8],
            "acknowledged": all(bool(m.get("acknowledged")) for m in own),
            "new": any(bool(m.get("new_this_generation")) for m in own),
            "finding_ids": [str(m.get("id")) for m in members]}


_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _address(text: str) -> str | None:
    found = _ADDRESS.search(text or "")
    return found.group(0).lower() if found else None


# Business impact first: a card that holds up customer orders, then a late, slipped,
# unanswered or loss-making one, then one due today, then one with no due date.
_TIERS = {"po_late": 1, "eta_slipped": 1, "vendor_no_reply": 1, "negative_margin": 1,
          "customer_order_at_risk": 0, "po_due": 2, "po_undated": 3,
          "customer_order_late": 1}


def _tier(members: Sequence[Mapping[str, Any]]) -> int:
    return min(_TIERS.get(str(m.get("event_type")), 3) for m in members)


def _confidence(po: Mapping[str, Any] | None, eta: Mapping[str, Any] | None,
                money: Mapping[str, Any] | None, kind: str, lead: Mapping[str, Any]
                ) -> dict[str, Any]:
    """high: the verdict rests on cited records and the vendor's confirmation question is
    decided from what the reader can see; medium: part of it is not decided (an ETA
    unknown, a lead only, steps hidden); low: no trail was read for a purchase order or
    the finding rests on a lead (``Lead, not firm``)."""
    why = []
    level = "high"
    summary = str(lead.get("summary") or "")
    if summary.startswith("Lead, not firm"):
        return {"level": "low", "why": "The link to the customer order is a lead, not a "
                                       "recorded link."}
    if kind == "vendor_chase" and po is None:
        return {"level": "low", "why": "Found by the scheduled check on the records; the "
                                       "order's confirmations and messages were not read "
                                       "for you yet."}
    if eta is not None and eta.get("answer") in ("unknown", "possibly", "conflicting",
                                                 "hidden"):
        level = "medium"
        why.append(f"vendor confirmation: {eta['answer']}")
    if po is not None and (int(po.get("hidden_steps") or 0) or po.get("may_hide_more")):
        level = "medium"
        why.append("some evidence sits where you can't open it")
    if money and money.get("suspect"):
        why.append("the order total is suspect (not ranked by)")
    if level == "high":
        why.insert(0, "the dates and status are read from cited records")
    return {"level": level, "why": "; ".join(why)}


def _headline(kind: str, label: str, days: int | None, eta: Mapping[str, Any] | None,
              orders: int, etype: str, risk_days: int | None = None) -> str:
    if kind == "vendor_chase":
        if etype == "po_due":
            text = f"{label} is due today"
        elif etype == "po_undated":
            text = f"{label} is open with no due date"
        elif days:
            text = f"{label} is {_days(days)} late"
        elif orders:
            text = f"{label} is late"
        else:
            text = f"{label} needs the vendor's attention"
        answer = (eta or {}).get("answer")
        text += {"yes": f"; vendor confirmed {eta.get('when')}" if eta and eta.get("when")
                 else "; vendor confirmed", "no": "; vendor has not confirmed",
                 "possibly": "; confirmation found, unverified",
                 "conflicting": "; vendor's dates disagree"}.get(answer or "", "")
        if orders:
            text += f"; holds up {orders} customer order{'s' if orders != 1 else ''}"
            text += f" (worst {_days(risk_days)} at risk)" if risk_days else ""
        return text
    if etype == "customer_order_late":
        return (f"Customer order {label} is past its required date"
                + (f" ({_days(days)} late)" if days else ""))
    if kind == "customer_update":
        return (f"Customer order {label} is at risk"
                + (f" ({_days(risk_days)} at risk)" if risk_days else "")
                + " from a late purchase order")
    return f"Order {label} lost money on item cost"


def build(items: Sequence[Mapping[str, Any]], *, labels: Mapping[str, str],
          trails: Mapping[str, Any], late_drafts: Mapping[str, Mapping[str, Any]],
          top: int = TOP) -> dict[str, Any]:
    """Every suggestion card of one reader's briefing items, in impact order, at most
    ``top`` composed; the rest counted. ``trails`` and ``late_drafts`` by record id."""
    groups, collapsed = group(items)
    cards = [compose(g, label=labels.get(str(g[0]["object_id"])) or str(g[0]["object_id"]),
                     labels=labels, trail=trails.get(str(g[0]["object_id"])),
                     late_draft=late_drafts.get(str(g[0]["object_id"])))
             for g in groups[:top]]
    by_kind: dict[str, int] = {}
    for g in groups:
        k = KIND_OF_TYPE[str(g[0].get("event_type"))]
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"schema": SCHEMA, "suggestions": cards, "total": len(groups),
            "more": max(0, len(groups) - len(cards)), "by_kind": by_kind,
            "collapsed": collapsed,
            "note": "Suggestions only: every draft is for a person to copy, edit and send "
                    "themselves; nothing is sent or written."}


def wanted(items: Iterable[Mapping[str, Any]], top: int = TOP) -> list[list[Mapping[str, Any]]]:
    """The groups ``build`` will compose (to read their trails and labels first)."""
    return group(list(items))[0][:top]
