"""The demo world's calendar (``--mock``): a synthetic store the real readers run over.

``upcoming`` and ``brief`` call ``operating.briefs.upcoming_meetings`` and
``operating.briefs.meeting_brief`` unchanged over ``DemoCalendar``, a fake of the primitives
(``plx.find``, ``plx.object``, ``plx.around``) holding synthetic events, people, parties,
orders and mail dated relative to now. ``weekly`` is a fixed synthetic week. SYNTHETIC names,
addresses and numbers only; evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from typing import Any, Mapping

from zoneinfo import ZoneInfo

from operating import briefs as br
from operating.time_window import BUSINESS_TZ

DOMAIN = "plx-demo.example"


def _h(oid: str, prop: str = "") -> str:
    return "demo://" + oid.replace(":", "/") + (f"#{prop}" if prop else "")


_SOURCE_KIND = {
    "PurchaseOrder": ("BC", "BC purchase order {n}"), "SalesOrder": ("BC", "BC sales order {n}"),
    "Lot": ("BC", "BC lot {n}"), "Supplier": ("Portal", "Portal supplier record"),
    "Customer": ("Portal", "Portal customer record"),
    "MeetingTranscript": ("Teams", "Teams meeting notes"),
    "CalendarEvent": ("Email", "Outlook calendar invite"),
    "Weekly": ("BC", "BC posted documents"), "Alert": ("Portal", "Portal alert"),
    "Person": ("Email", "Directory entry"),
}


def _labelled(node: Any) -> Any:
    """Demo sources named as the reader would see a real one ("BC purchase order 90412"),
    in place; a source without a demo handle is left as it is."""
    if isinstance(node, list):
        for item in node:
            _labelled(item)
    elif isinstance(node, dict):
        handle = node.get("handle")
        if isinstance(handle, str) and handle.startswith("demo://") and not node.get("label"):
            kind, _, rest = handle[len("demo://"):].partition("/")
            system, label = _SOURCE_KIND.get(kind, ("File", "Demo record"))
            node["system"] = system
            node["label"] = label.format(n=rest.split("/")[-1].split("#")[0])
        for value in node.values():
            if isinstance(value, (list, dict)):
                _labelled(value)
    return node


class _Nobody:
    """The demo's people exclusion: loaded, excluding no synthetic address."""
    unavailable = None

    def is_excluded(self, value: str) -> bool:
        return False


def _person(name: str, local: str, domain: str = DOMAIN) -> dict[str, str]:
    return {"name": name, "address": f"{local}@{domain}"}


ANA = _person("Ana Ferreira", "ana.ferreira")
BEN = _person("Ben Okafor", "ben.okafor")
CLAIRE = _person("Claire Dubois", "claire.dubois")
DEV = _person("Dev Raman", "dev.raman")
EMMA = _person("Emma Lindqvist", "emma.lindqvist")
FRANK = _person("Frank Moreau", "frank.moreau")
GRACE = _person("Grace Chen", "grace.chen")
MAPLE = _person("Paul Girard", "paul.girard", "mapleleaf-soap.example")
GLOW = _person("Nadia Haddad", "nadia.haddad", "glowbotanicals.example")
GLOW2 = _person("Owen Price", "owen.price", "glowbotanicals.example")
NORTH = _person("Ruth Adeyemi", "ruth.adeyemi", "northwind-pack.example")
HARBOR = _person("Sam Whitaker", "sam.whitaker", "harborskin.example")

TEAMS = "https://teams.microsoft.com/l/meetup-join/demo-"


def _att(p: Mapping[str, str], kind: str = "required", response: str = "accepted") -> dict:
    return {**p, "type": kind, "response": response}


# (key, day offset, start hh:mm local, minutes, subject, organizer, attendees, where,
#  online, agenda, parties)
EVENTS: tuple[tuple, ...] = (
    ("ops-daily-0", 0, "08:30", 15, "Ops stand-up", ANA,
     [_att(BEN), _att(CLAIRE), _att(DEV), _att(FRANK, "optional", "tentativelyaccepted")],
     None, True, "Late POs, today's receipts, line changeovers, anything blocking shipping.",
     ()),
    ("maple-0", 0, "10:00", 45, "Maple Leaf Soapworks: PO 90412 delay and Q1 volumes", BEN,
     [_att(ANA), _att(MAPLE), _att(GRACE, "optional", "none")], None, True,
     "1. Revised ship date for PO 90412 (12,000 lavender bars).\n2. Expedite options and "
     "freight cost.\n3. Q1 forecast and capacity on the saponification line.",
     ("Supplier:demo:maple-leaf",)),
    ("glow-0", 0, "14:00", 60, "Glow Botanicals quarterly review", CLAIRE,
     [_att(BEN), _att(GLOW), _att(GLOW2, "required", "tentativelyaccepted"), _att(EMMA)],
     "Boardroom 2, Mississauga", True,
     "Review SO 77120 timing against the Maple Leaf delay, holiday SKU launch, payment "
     "terms on the October invoices.", ("Customer:demo:glow",)),
    ("qa-1", 1, "09:00", 30, "QC hold review: lot L-24-0917", GRACE,
     [_att(ANA), _att(DEV), _att(EMMA, "required", "declined")], "QA lab", False,
     "Fragrance deviation on lot L-24-0917, retest results, release or rework decision.",
     ()),
    ("north-1", 1, "13:30", 30, "Northwind Packaging: shipper carton spec", DEV,
     [_att(NORTH), _att(BEN)], None, True,
     "New 24-count shipper dimensions, the late PO 90388 and the next artwork deadline.",
     ("Supplier:demo:northwind",)),
    ("ops-daily-1", 1, "08:30", 15, "Ops stand-up", ANA,
     [_att(BEN), _att(CLAIRE), _att(DEV), _att(FRANK, "optional", "tentativelyaccepted")],
     None, True, "Late POs, today's receipts, line changeovers, anything blocking shipping.",
     ()),
    ("weekly-2", 2, "11:00", 60, "Weekly operations review", ANA,
     [_att(BEN), _att(CLAIRE), _att(DEV), _att(EMMA), _att(FRANK), _att(GRACE)],
     "Boardroom 1", True,
     "Shipped and invoiced this week, incoming deliveries and ETAs, open quality holds, "
     "incomplete orders.", ()),
    ("harbor-3", 3, "15:00", 45, "Harbor Skin Co: spa line samples", CLAIRE,
     [_att(HARBOR), _att(EMMA)], None, True,
     "Sample feedback on the eucalyptus range, pricing for 6,000 units, first order date.",
     ("Customer:demo:harbor",)),
    ("ridge-7", 7, "10:30", 30, "Ridgeway Labels: label reprint", BEN,
     [_att(DEV), _att(GRACE, "optional", "none")], None, True,
     "Reprint of the misprinted lavender labels on PO 90455 and who pays.",
     ("Supplier:demo:ridgeway",)),
    ("ops-daily-7", 7, "08:30", 15, "Ops stand-up", ANA,
     [_att(BEN), _att(CLAIRE), _att(DEV)], None, True,
     "Late POs, today's receipts, line changeovers, anything blocking shipping.", ()),
    ("weekly-9", 9, "11:00", 60, "Weekly operations review", ANA,
     [_att(BEN), _att(CLAIRE), _att(DEV), _att(EMMA), _att(FRANK), _att(GRACE)],
     "Boardroom 1", True,
     "Shipped and invoiced this week, incoming deliveries and ETAs, open quality holds, "
     "incomplete orders.", ()),
    ("private-4", 4, "12:00", 60, "", ANA, [], None, False, None, ()),
)

ORDERS = (
    ("PurchaseOrder:demo:90412", "Supplier:demo:maple-leaf", "90412", "Released", -25, "Lavender bars"),
    ("PurchaseOrder:demo:90388", "Supplier:demo:northwind", "90388", "Released", -18, "Shippers"),
    ("PurchaseOrder:demo:90455", "Supplier:demo:ridgeway", "90455", "Released", 2, "Labels"),
    ("SalesOrder:demo:77120", "Customer:demo:glow", "77120", "Open", 3, ""),
    ("SalesOrder:demo:77131", "Customer:demo:glow", "77131", "Open", -4, ""),
)

MAILS = (
    ("Email:demo:m1", "Supplier:demo:maple-leaf", "Email.sent_by_supplier",
     "RE: PO 90412 revised ship date", -3, "Person:demo:paul"),
    ("Email:demo:m2", "Supplier:demo:maple-leaf", "Email.mentions_supplier",
     "Expedite quote for 90412", -1, "Person:demo:ben"),
    ("Email:demo:m3", "Customer:demo:glow", "Email.sent_by_customer",
     "Holiday SKU launch dates", -5, "Person:demo:nadia"),
    ("Email:demo:m4", "Customer:demo:glow", "Email.mentions_customer",
     "SO 77120: can we split the shipment?", -2, "Person:demo:claire"),
    ("TeamsChannelMessage:demo:t1", "Supplier:demo:northwind",
     "TeamsChannelMessage.mentions_supplier", "Shipper carton artwork v3", -4, None),
)
PEOPLE = {"Person:demo:paul": "Paul Girard", "Person:demo:ben": "Ben Okafor",
          "Person:demo:nadia": "Nadia Haddad", "Person:demo:claire": "Claire Dubois"}
LABELS = {"Supplier:demo:maple-leaf": "Maple Leaf Soapworks",
          "Supplier:demo:northwind": "Northwind Packaging Ltd",
          "Supplier:demo:ridgeway": "Ridgeway Labels",
          "Customer:demo:glow": "Glow Botanicals", "Customer:demo:harbor": "Harbor Skin Co"}


def event_id(key: str) -> str:
    return f"CalendarEvent:demo:{key}"


class DemoCalendar:
    """The primitives over the synthetic calendar, dated relative to ``now`` (business day)."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime.now(timezone.utc)
        local = self.now.astimezone(ZoneInfo(BUSINESS_TZ))
        self.objects: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str]] = []
        for key, day, hhmm, minutes, subject, org, atts, where, online, agenda, parties \
                in EVENTS:
            hh, mm = (int(x) for x in hhmm.split(":"))
            start = (local + timedelta(days=day)).replace(hour=hh, minute=mm, second=0,
                                                         microsecond=0)
            oid = event_id(key)
            private = not subject
            self.objects[oid] = {"label": subject or "(no subject)", "values": {
                "subject": subject or None, "mailbox": ANA["address"],
                "start": _utc(start), "end": _utc(start + timedelta(minutes=minutes)),
                "is_cancelled": "false", "is_all_day": "false",
                "content": "withheld_sensitivity" if private else "read",
                "location": where, "body_preview": agenda,
                "organizer": json.dumps(org), "attendees": json.dumps(atts),
                "is_online_meeting": "true" if online else "false",
                "online_meeting": json.dumps({"join_url": TEAMS + key}) if online else None,
                "is_organizer": "true" if org is ANA else "false"}}
            for party in parties:
                link = "CalendarEvent.with_customer" if party.startswith("Customer") \
                    else "CalendarEvent.with_supplier"
                self.edges.append((oid, link, party))
        for oid, label in LABELS.items():
            self.objects[oid] = {"label": label, "values": {}}
        today = local.date()
        for oid, party, number, status, due, _ in ORDERS:
            supplier = party.startswith("Supplier")
            self.objects[oid] = {"label": ("PO " if supplier else "SO ") + number, "values": {
                ("po_number" if supplier else "order_number"): number, "status": status,
                ("expected_date" if supplier else "required_date"):
                    (today + timedelta(days=due)).isoformat()}}
            self.edges.append((oid, "PurchaseOrder.ordered_from" if supplier
                               else "SalesOrder.ordered_by", party))
        for oid, party, link, subject, day, person in MAILS:
            self.objects[oid] = {"label": subject, "values": {
                "subject": subject, "received_at": (today + timedelta(days=day)).isoformat()}}
            self.edges.append((oid, link, party))
            if person:
                kind = br.type_of(oid)
                self.edges.append((oid, f"{kind}.sent_by_person", person))
        for oid, name in PEOPLE.items():
            local_part = name.lower().replace(" ", ".")
            self.objects[oid] = {"label": name, "values": {"upn": f"{local_part}@{DOMAIN}"}}

    def _cells(self, oid: str, props: Any = None) -> list[dict[str, Any]]:
        values = self.objects[oid]["values"]
        return [{"property": k, "value": {"untrusted_text": str(v)}, "markings": [],
                 "citation": {"l1_handle": _h(oid, k)}}
                for k, v in values.items() if v is not None and (props is None or k in props)]

    def __call__(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if tool == "plx.object":
            oid = args["object_id"]
            if oid not in self.objects:
                return {"error": {"code": "object_not_found", "message": "not found"}}
            return {"object": {"object_id": oid, "object_type": br.type_of(oid), "display": {
                "untrusted_text": self.objects[oid]["label"], "origin": {"citation": _h(oid)}}},
                "values": self._cells(oid, args.get("properties"))}
        if tool == "plx.around":
            start, hop = args["from"], args["path"][0]
            forward = hop["direction"] == "forward"
            found = [(d if forward else s) for s, link, d in self.edges
                     if link == hop["link_type"] and (s if forward else d) == start]
            return {"objects": [{"object_id": o} for o in found],
                    "completeness": {"next_cursor": None}}
        if tool == "plx.find":
            ids = [o for o in self.objects if br.type_of(o) == args["object_type"]
                   and all(self._match(o, f) for f in args.get("filters") or ())]
            return {"candidates": [{"object": {"object_id": o}} for o in ids],
                    "completeness": {"next_cursor": None}}
        return {"error": {"code": "not_served", "message": "the demo serves no such read"}}

    def _match(self, oid: str, f: Mapping[str, Any]) -> bool:
        value = self.objects[oid]["values"].get(f["property"])
        if value is None:
            return False
        return value >= f["value"] if f["op"] == "gte" else value < f["value"] \
            if f["op"] == "lt" else False


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


OPEN_ITEMS = {
    "ops-daily": [{"label": "Action item: Chase Northwind for the PO 90388 ship date",
                   "detail": "Dev Raman, due yesterday (overdue)", "days": -1},
                  {"label": "Action item: Book the line 2 changeover for the holiday SKU",
                   "detail": "Frank Moreau (open)", "days": -2}],
    "weekly": [{"label": "Action item: Send Glow the split-shipment proposal for SO 77120",
                "detail": "Claire Dubois, due Friday (open)", "days": -7},
               {"label": "Action item: Close the October receipts that are still open in BC",
                "detail": "Emma Lindqvist (open)", "days": -7},
               {"label": "Action item: Get a second quote for shipper cartons",
                "detail": "Dev Raman (open)", "days": -14}],
    "maple": [{"label": "Action item: Ask Maple Leaf for a written ship-date commitment",
               "detail": "Ben Okafor, due today (open)", "days": -21}],
}


def upcoming(now: datetime | None = None) -> list[dict[str, Any]]:
    """The demo calendar through the real reader, with synthetic carried action items."""
    now = now or datetime.now(timezone.utc)
    cal = DemoCalendar(now)
    since = now.astimezone(ZoneInfo(BUSINESS_TZ)).replace(hour=0, minute=0, second=0, microsecond=0)
    raw = br.upcoming_meetings(cal, frozenset(), now=now, people=_Nobody(), since=since)
    today = now.astimezone(ZoneInfo(BUSINESS_TZ)).date()
    for e in raw:
        key = e["object_id"].rsplit(":", 1)[1].rsplit("-", 1)[0]
        e["open_items"] = [{"label": i["label"], "detail": i["detail"],
                            "date": (today + timedelta(days=i["days"])).isoformat(),
                            "sources": [{"handle": _h("MeetingTranscript:demo:" + key)}]}
                           for i in OPEN_ITEMS.get(key, ())]
    return _labelled(raw)


def brief(object_id: str, now: datetime | None = None) -> dict[str, Any] | None:
    """The demo prep brief through the real builder; None for a record the demo lacks."""
    now = now or datetime.now(timezone.utc)
    cal = DemoCalendar(now)
    if object_id not in cal.objects or br.type_of(object_id) not in br.MEETING_TYPES + \
            br.PARTY_TYPES:
        return None
    try:
        raw = br.meeting_brief(cal, frozenset(), {"status": "read", "gaps": []}, object_id,
                               now=now, declared=False, people=_Nobody())
    except br.BriefRefused:
        return None
    key = object_id.rsplit(":", 1)[1].rsplit("-", 1)[0]
    today = now.astimezone(ZoneInfo(BUSINESS_TZ)).date()
    for section in raw["sections"]:
        if section.get("key") == "notes" and OPEN_ITEMS.get(key):
            section["rows"] = list(section.get("rows") or ()) + [
                {"label": i["label"], "detail": i["detail"],
                 "date": (today + timedelta(days=i["days"])).isoformat(), "who": None,
                 "amount": None, "object_id": None,
                 "sources": [{"handle": _h("MeetingTranscript:demo:" + key)}]}
                for i in OPEN_ITEMS[key]]
            section["state"] = "shown"
            section["note"] = None
    return _labelled(raw)


def week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())


def _fig(label: str, value: str | None, unit: str, previous: str | None = None,
         currency: str | None = None, note: str | None = None, key: str = "") -> dict:
    delta = None
    if value is not None and previous is not None:
        from decimal import Decimal
        d = Decimal(value) - Decimal(previous)
        delta = f"{'+' if d > 0 else ''}{d}"
    return {"label": label, "value": value, "unit": unit, "previous": previous,
            "delta": delta, "currency": currency, "note": note,
            "sources": [{"handle": _h("Weekly:demo:" + (key or label))}]}


def _alert(label: str, detail: str, day: date, oid: str | None = None) -> dict:
    return {"label": label, "detail": detail, "date": day.isoformat(), "who": None,
            "amount": None, "object_id": oid, "sources": [{"handle": _h(oid or "Alert:demo:" + label[:20])}]}


def weekly(week: date | None = None, now: datetime | None = None) -> dict[str, Any]:
    """A synthetic finished week in the weekly review's own shape and sections
    (``operating.briefs``), as a reader without the finance marking reads it."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(BUSINESS_TZ)).date()
    start = week_start(week) if week else week_start(today) - timedelta(days=7)
    d = lambda n: start + timedelta(days=n)  # noqa: E731
    sec = lambda key, title, rows=(), figures=(), note=None, state=None: {  # noqa: E731
        "key": key, "title": title, "state": state or ("shown" if rows or figures else "empty"),
        "note": note, "rows": list(rows), "more": 0, "figures": list(figures)}
    sections = [
        sec("figures", "This week", figures=[
            _fig("Orders booked", "23", "records", "19"),
            _fig("Orders shipped", "17", "records", "21"),
            _fig("Invoices issued", "15", "records", "18"),
            _fig("Shipped on time", "82.4", "percent", "90.5",
                 note="14 of 17 shipments by their order's required date"),
            _fig("Shipped late", "3", "records", "2", note="shipped after their order's "
                 "required date")],
            note=f"Week of {start.isoformat()} to {d(6).isoformat()}, compared with the week "
                 f"of {(start - timedelta(days=7)).isoformat()}."),
        sec("late_pos", "Late purchase orders (open now)", [
            _alert("PO 90412 Maple Leaf Soapworks: 12 days late", "Lavender bars, 12,000 EA; "
                   "supplier moved the ship date to 4 Nov", d(1), "PurchaseOrder:demo:90412"),
            _alert("PO 90388 Northwind Packaging Ltd: 5 days late", "Shipper cartons; no reply "
                   "to two chasers", d(3), "PurchaseOrder:demo:90388")],
            figures=[_fig("Late purchase orders", "2", "records")]),
        sec("slips", "Top slips and their reasons", [
            _alert("SO 77120 Glow Botanicals: ship date moved 6 Nov to 13 Nov",
                   "Waiting on PO 90412 lavender bars", d(2), "SalesOrder:demo:77120"),
            _alert("PO 90455 Ridgeway Labels: at risk", "Label artwork reprint after a "
                   "misprint", d(4), "PurchaseOrder:demo:90455")],
            note="Each reason is the alert's own cited summary."),
        sec("alert_flow", "Alerts opened and closed", [
            _alert("Lot L-24-0917 fragrance deviation", "QC hold opened", d(2),
                   "Lot:demo:L-24-0917")], figures=[
            _fig("Opened", "9", "records", "6", key="opened"),
            _fig("Closed", "7", "records", "8", key="closed")]),
        sec("top_alerts", "Top alerts", [
            _alert("PO 90412 Maple Leaf Soapworks: 12 days late", "Blocks SO 77120", d(1),
                   "PurchaseOrder:demo:90412"),
            _alert("Lot L-24-0917 on hold", "Fragrance deviation", d(2), "Lot:demo:L-24-0917")]),
        # the demo reader holds no finance marking: money sections are marked, as the real
        # builder marks them
        sec("cash", "Cash, AR and AP aging", [], state="finance_marked",
            note="Finance-marked: shown only to readers who hold finance."),
        sec("suppliers", "Supplier reliability", [
            _alert("Northwind Packaging Ltd: 3 of the last 5 POs late", "Average 6 days", d(4),
                   "Supplier:demo:northwind")]),
        sec("commitments_missed", "Promises missed", [
            _alert("Reply to Harbor Skin Co on sample pricing", "Promised by Claire Dubois for "
                   "Wednesday", d(2), "Customer:demo:harbor")]),
        sec("asks", "Top customer asks", [
            _alert("Glow Botanicals asked to split SO 77120", "Nadia Haddad, by mail", d(1),
                   "SalesOrder:demo:77120")]),
        sec("quality", "Quality holds", [
            _alert("Lot L-24-0917 on hold", "Fragrance deviation; retest booked", d(2),
                   "Lot:demo:L-24-0917")]),
        sec("margin", "Margin movers", [], state="finance_marked",
            note="Finance-marked: shown only to readers who hold finance."),
    ]
    return _labelled({"schema": br.WEEKLY_SCHEMA, "kind": "weekly",
            "title": f"Weekly review, week of {start.isoformat()}", "subject": None,
            "subject_sources": [], "when": start.isoformat(), "as_of": today.isoformat(),
            "parties": [], "possible_parties": [], "sections": sections, "gaps": [],
            "basis": "Counted from the records you may read, no model call; alerts are those "
                     "open now on your alert list; money per currency, never converted."})


def action_items(now: datetime | None = None) -> list[dict[str, Any]]:
    """Synthetic action items in ``operating.meeting_followup.my_items``' shape."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(BUSINESS_TZ)).date()
    day = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731

    def item(i: int, text: str, owner: Mapping[str, str], due: str | None, status: str,
             meeting: str, met: int, records=(), prepared=None, evidence=None) -> dict:
        return {"id": f"demo-act-{i}", "kind": "action", "text": text, "owner": dict(owner),
                "due": due, "status": status, "said_by": owner["name"], "at": "00:12:40",
                "citations": [_h(f"MeetingTranscript:demo:{meeting}") + "#t=00:12:40"],
                "records": [dict(r) for r in records], "prepared": prepared,
                "evidence": evidence or [], "meeting_id": f"demo-{meeting}-{met}",
                "meeting": MEETING_TITLES[meeting], "meeting_start": day(met) + "T15:00:00Z"}
    po412 = {"value": "PO 90412", "status": "linked", "object_id": "PurchaseOrder:demo:90412",
             "type": "PurchaseOrder", "label": "PO 90412"}
    po388 = {"value": "90388", "status": "linked", "object_id": "PurchaseOrder:demo:90388",
             "type": "PurchaseOrder", "label": "PO 90388"}
    so120 = {"value": "SO 77120", "status": "linked", "object_id": "SalesOrder:demo:77120",
             "type": "SalesOrder", "label": "SO 77120"}
    draft = lambda to, subject: {"kind": "message", "sent": False, "writes": 0,  # noqa: E731
                                 "citations": [], "card_id": None, "card": {
                                     "id": "demo-card", "subject": {"label": subject},
                                     "draft": {"to": {"address": to}, "subject": subject}}}
    return [
        item(1, "Chase Northwind for the PO 90388 ship date", DEV, day(-1), "overdue",
             "ops-daily", -1, [po388], draft(NORTH["address"], "PO 90388: ship date?")),
        item(2, "Ask Maple Leaf for a written ship-date commitment on 90412", BEN, day(0),
             "open", "maple", -21, [po412], draft(MAPLE["address"], "PO 90412 ship date")),
        item(3, "Send Glow the split-shipment proposal for SO 77120", CLAIRE, day(2), "open",
             "weekly", -7, [so120], {"kind": "document_pack", "sent": False, "writes": 0,
                                     "citations": [], "object_id": "SalesOrder:demo:77120"}),
        item(4, "Book the line 2 changeover for the holiday SKU", FRANK, day(4), "open",
             "ops-daily", -2, (), {"kind": "planner_task", "sent": False, "writes": 0,
                                   "citations": [], "task": {"title": "Line 2 changeover, "
                                                             "holiday SKU", "due": day(4)}}),
        item(5, "Close the October receipts still open in BC", EMMA, None, "open", "weekly",
             -7, (), {"kind": "write_intent", "sent": False, "writes": 0, "citations": [],
                      "label": "Receipt close-out", "needs": ["receipt numbers"]}),
        item(6, "Get a second quote for shipper cartons", DEV, day(9), "open", "weekly", -14),
        item(7, "Confirm retest booking for lot L-24-0917", GRACE, day(-3), "done", "qa", -6,
             (), None, [{"what": "retest booked in the QC log on " + day(-4)}]),
        item(8, "Reply to Harbor Skin Co on sample pricing", CLAIRE, day(-2), "overdue",
             "harbor", -9, ({"value": "Harbor", "status": "ambiguous"},),
             draft(HARBOR["address"], "Eucalyptus range pricing")),
    ]


MEETING_TITLES = {"ops-daily": "Ops stand-up", "maple": "Maple Leaf Soapworks: PO 90412 delay",
                  "weekly": "Weekly operations review", "qa": "QC hold review",
                  "harbor": "Harbor Skin Co: spa line samples"}
