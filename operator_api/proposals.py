"""Proposed follow-ups and record updates, drafted from what a reader's page or answer
already shows: never sent, never written.

Input is the app contract after the gate (``ObjectPage`` / ``AnswerPayload``): every step,
fact, line and contact in it is one this reader may open (``operator_api.trail`` decided
each source again, the people directory cleared each shown sender). So a draft built only
from those values states nothing the reader could not open, and nothing invented: every
date and figure a draft says is one of its ``figures``, each with its source. A value the
records do not state is named in ``missing`` (no vendor contact: the draft says so and
leaves the address empty).

Three kinds:

* ``vendor_chase``: a late purchase order's chase email to the vendor: the PO, its late
  lines and quantities, the original due date, the last vendor message, and the ask for a
  confirmed ship date;
* ``customer_heads_up``: a customer order whose materials wait on a late PO (an answer's
  materials trail);
* ``record_update``: a vendor message or document confirms a delivery date the record
  (BC, the Portal, FileMaker, Sage) still states differently: before, after and the
  evidence. The person applies it in that system.

Deterministic; reads nothing; no network, mail or write API is reachable from here.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from . import models as m

DAY = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RECORD_SYSTEMS = ("BC", "Portal", "FileMaker", "Sage")
EVIDENCE_SYSTEMS = ("Email", "Teams", "File")
# Field labels that state the vendor's / the customer's email address on a record.
VENDOR_EMAIL = re.compile(r"(?:vendor|supplier|buy.?from|pay.?to).*e.?mail|"
                          r"e.?mail.*(?:vendor|supplier)", re.I)
CUSTOMER_EMAIL = re.compile(r"(?:customer|sell.?to|bill.?to|ship.?to).*e.?mail|"
                            r"e.?mail.*customer", re.I)
VENDOR_NAME = re.compile(r"^(?:buy.?from )?(?:vendor|supplier)(?: name)?$", re.I)
RECORD_DATE = re.compile(r"(?:expected|promised) receipt date", re.I)
NO_CONTACT = "No {who} contact found in the mail, BC or FileMaker records you can see: " \
             "add the address before sending."
MAX_LINES = 12


def for_page(page: m.ObjectPage) -> list[m.Proposal]:
    """A purchase order page's chase draft and proposed updates (its trail, lines and
    facts); nothing for any other page or a PO that is not late."""
    if page.type != "PurchaseOrder" or page.trail is None:
        return []
    return [p for t in page.trail.trails[:1] for p in _po(t, page)]


def for_answer(answer: m.AnswerPayload) -> list[m.Proposal]:
    """An answer's drafts: each late PO trail's chase, and a heads-up per customer order a
    materials trail shows waiting on a late PO."""
    trail = answer.trail
    if trail is None:
        return []
    out = [p for t in trail.trails for p in _po(t, None)]
    if trail.kind == "materials":
        out += _heads_up(trail)
    seen: set[str] = set()
    return [p for p in out if not (p.id in seen or seen.add(p.id))]


# --------------------------------------------------------------------------- vendor chase

def _po(trail: m.PoTrail, page: Optional[m.ObjectPage]) -> list[m.Proposal]:
    if trail.state != "late":
        return []
    out = [_chase(trail, page)]
    change = _update(trail, page)
    if change is not None:
        out.append(change)
    return out


def _figure(label: str, value: str, source: Optional[m.Source]) -> m.DraftFigure:
    return m.DraftFigure(label=label, value=value[:300], source=source)


def _day(text: Optional[str]) -> Optional[str]:
    found = DAY.findall(text or "")
    return found[-1] if found else None


def _due(trail: m.PoTrail) -> Optional[m.TrailStep]:
    """The governing requested-by date's step, else the first dated due-date step."""
    dated = [s for s in trail.steps if s.kind == "due_date" and s.source and _day(s.title)]
    governing = [s for s in dated if (s.detail or "").startswith("the governing")]
    return (governing or dated or [None])[0]


def _vendor_message(trail: m.PoTrail) -> Optional[m.TrailStep]:
    """The newest message from the vendor side the reader may open."""
    vendor = [s for s in trail.steps if s.source and (
        (s.kind == "follow_up" and (s.sender or s.title.startswith("Email from the vendor")))
        or (s.kind in ("confirmation", "revision") and s.source.system in EVIDENCE_SYSTEMS))]
    return max(vendor, key=lambda s: (s.at or "", s.kind == "follow_up"), default=None)


def _topic(step: Optional[m.TrailStep]) -> Optional[str]:
    """A message step's subject ("Email from the vendor side: <subject>")."""
    if step is None or step.kind != "follow_up":
        return None
    head, sep, rest = step.title.partition(": ")
    return rest.strip()[:160] or None if sep else None


def _confirmed(trail: m.PoTrail) -> Optional[tuple[str, m.Source]]:
    """The vendor's confirmed date and its source, when the ETA check states one this reader
    may open."""
    eta = trail.eta
    if eta is None or eta.state != "confirmed" or eta.source is None:
        return None
    day = _day(re.split(r"\(", eta.text, maxsplit=1)[0])
    return (day, eta.source) if day else None


def _fact_values(page: Optional[m.ObjectPage]) -> Iterable[tuple[str, str, m.Source]]:
    """(label, value, source) of the page's facts and of their sources' fields."""
    for fact in (page.facts if page else ()):
        if not fact.sources:
            continue
        label, sep, value = fact.text.partition(":")
        if sep and value.strip():
            yield label.strip(), value.strip(), fact.sources[0]
        for source in fact.sources:
            for key, val in (source.fields or {}).items():
                yield key, str(val), source


def _vendor(trail: m.PoTrail, page: Optional[m.ObjectPage]) -> Optional[m.DraftFigure]:
    for label, value, source in _fact_values(page):
        if VENDOR_NAME.match(label.replace("_", " ").strip()) and not ADDRESS.search(value):
            return _figure("Vendor", value, source)
    return None


def _contact(trail: m.PoTrail, page: Optional[m.ObjectPage], pattern: re.Pattern
             ) -> tuple[Optional[str], Optional[str], Optional[m.DraftFigure], Optional[str]]:
    """(address, name, figure, where) of the counterpart: the vendor-side sender the trail
    cleared for this reader, else an email field of a record on the page."""
    action = trail.next_action
    if action is not None and action.contact and action.contact_source is not None:
        address = ADDRESS.search(action.contact)
        if address:
            name = action.contact.split("<")[0].strip().strip('"') or None
            if name and ADDRESS.fullmatch(name):
                name = None
            return (address.group(0), name,
                    _figure("Contact", action.contact, action.contact_source),
                    f"from the last message they sent ({action.contact_source.label})")
    for label, value, source in _fact_values(page):
        address = ADDRESS.search(value)
        if address and pattern.search(label.replace("_", " ")):
            return (address.group(0), None, _figure("Contact", address.group(0), source),
                    f"from {source.label}")
    return None, None, None, None


def _lines(page: Optional[m.ObjectPage]) -> list[tuple[str, m.LateLine]]:
    """The page's late lines as draft text, each one cited."""
    out = []
    for line in (page.late_lines if page else ())[:MAX_LINES]:
        if not line.sources or re.match(r"^PO number:", line.line, re.I):
            continue
        if line.days_late == 0:
            continue   # an on-time line is not chased
        if line.item or line.quantity:
            name = line.item or (f"Line {line.line}" if line.line.strip().isdigit()
                                 else line.line)
            text = name + (f": {line.quantity}" if line.quantity else "")
            text += f", due {line.due_date}" if line.due_date else ""
        else:
            text = line.line
        out.append((text[:240], line))
    return out


def _chase(trail: m.PoTrail, page: Optional[m.ObjectPage]) -> m.Proposal:
    po = trail.subject.label
    figures = [_figure("Purchase order", po, None)]
    missing: list[str] = []
    vendor = _vendor(trail, page)
    if vendor:
        figures.append(vendor)
    to, name, contact, where = _contact(trail, page, VENDOR_EMAIL)
    if contact:
        figures.append(contact)
    else:
        missing.append("The vendor's contact address")
    due = _due(trail)
    due_day = _day(due.title) if due else None
    if due and due_day:
        figures.append(_figure("Original due date", due_day, due.source))
    else:
        missing.append("The original due date")
    lines = _lines(page)
    for text, line in lines:
        figures.append(_figure("Late line", text, line.sources[0]))
    if not lines:
        missing.append("The late lines and their quantities (open the PO page)"
                       if page is None else "Which lines are late")
    message = _vendor_message(trail)
    message_day = _day(message.at) if message else None
    topic = _topic(message)
    if message and message_day:
        figures.append(_figure("Last vendor message",
                               message_day + (f": {topic}" if topic else ""), message.source))
    confirmed = _confirmed(trail)
    if confirmed:
        figures.append(_figure("Vendor's last confirmed date", confirmed[0], confirmed[1]))

    said = [f"Hello{' ' + name if name else ''},", ""]
    ours = f"Our {po}" if re.match(r"PO\b", po) else f"Our purchase order {po}"
    late = f"{ours} was due on {due_day}" if due_day else f"{ours} is past its due date"
    said.append(late + " and has not been received in full.")
    if lines:
        said += ["", "Still open:"] + [f"- {text}" for text, _ in lines]
    if confirmed:
        said += ["", f"Your last confirmed delivery date was {confirmed[0]}."]
    if message and message_day:
        said += ["", f"Your last message about it was on {message_day}"
                     + (f' ("{topic}").' if topic else ".")]
    elif not confirmed:
        said += ["", "We have no message from you about this order on record."]
    said += ["", "Please confirm the date these items will ship.", "", "Thank you,"]
    return m.Proposal(
        id=f"vendor_chase:{trail.subject.object_id}", kind="vendor_chase",
        title=f"Chase {vendor.value if vendor else 'the vendor'} for {po}",
        subject=trail.subject, to=to, to_name=name,
        contact_note=(f"Address {where}." if to else NO_CONTACT.format(who="vendor")),
        email_subject=f"{po}: please confirm the ship date",
        body="\n".join(said), figures=figures, missing=missing)


# --------------------------------------------------------------------------- record update

def _update(trail: m.PoTrail, page: Optional[m.ObjectPage]) -> Optional[m.Proposal]:
    """The record's expected receipt date when a vendor message or document it does not
    reflect confirms another: one proposal (the first record found)."""
    confirmed = _confirmed(trail)
    if confirmed is None or confirmed[1].system not in EVIDENCE_SYSTEMS:
        return None
    after, evidence = confirmed
    stated: list[tuple[str, str, m.Source]] = []
    for s in trail.steps:
        if s.kind == "due_date" and s.title.startswith("Expected on a line by") and s.source:
            stated.append(("Expected receipt date", _day(s.title) or "", s.source))
    for label, value, source in _fact_values(page):
        if RECORD_DATE.search(label.replace("_", " ")) and _day(value):
            stated.append((label[:1].upper() + label[1:], _day(value) or "", source))
    for field, before, source in stated:
        if before and before != after and source.system in RECORD_SYSTEMS:
            po = trail.subject.label
            return m.Proposal(
                id=f"record_update:{trail.subject.object_id}:{source.handle}",
                kind="record_update",
                title=f"Update {field.lower()} on {po} in {source.system}",
                subject=trail.subject,
                change=m.ProposedChange(
                    system=source.system, record=po, field=field, before=before,
                    after=after, before_source=source, evidence=evidence,
                    why=f"The vendor confirmed {after} ({evidence.label}); {source.label} "
                        f"still says {before}."),
                figures=[_figure(field, before, source),
                         _figure("Vendor's confirmed date", after, evidence)])
    return None


# --------------------------------------------------------------------------- customer

def _heads_up(trail: m.EvidenceTrail) -> list[m.Proposal]:
    """One heads-up per customer order whose materials wait on a late PO."""
    by_po = {t.subject.object_id: t for t in trail.trails}
    orders: dict[str, list[m.MaterialRow]] = {}
    for row in trail.materials:
        if row.order is not None and row.state == "late":
            orders.setdefault(row.order.object_id, []).append(row)
    out = []
    for rows in orders.values():
        order = rows[0].order
        assert order is not None
        figures = [_figure("Customer order", order.label, None)]
        items, dates = [], []
        for row in rows[:MAX_LINES]:
            items.append(row.item.label)
            figures.append(_figure("Material waiting", row.item.label, None))
            figures.append(_figure("On purchase order", row.po.label, None))
            po_trail = by_po.get(row.po.object_id)
            confirmed = _confirmed(po_trail) if po_trail else None
            if confirmed:
                dates.append(confirmed[0])
                figures.append(_figure("Supplier's confirmed date", confirmed[0],
                                       confirmed[1]))
        said = ["Hello,", "",
                f"A short update on your order {order.label}: "
                f"{_listed(list(dict.fromkeys(items)))} for it "
                f"{'is' if len(set(items)) == 1 else 'are'} delayed at our supplier."]
        if dates:
            said.append(f"Our supplier's latest confirmed date is {max(dates)}.")
        said += ["We will confirm the new date for your order as soon as we have it.", "",
                 "Thank you,"]
        out.append(m.Proposal(
            id=f"customer_heads_up:{order.object_id}", kind="customer_heads_up",
            title=f"Heads-up to the customer on {order.label}", subject=order,
            contact_note=NO_CONTACT.format(who="customer"),
            email_subject=f"Update on your order {order.label}", body="\n".join(said),
            figures=figures,
            missing=["The customer's contact address"]
            + ([] if dates else ["A confirmed date from the supplier"])))
    return out


def _listed(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
