"""Briefs at their boundary: the meeting prep brief and the weekly review as one reader reads
them through a reader-scoped fake of the primitives (``plx.object``, ``plx.around``,
``plx.find``, ``plx.aggregate``), the reader's trimmed alert list, the finance marking
bounding every money value even when a read leaks one, no hidden count, lead-tier parties
never read, fail-closed types, N=1/10/60 cardinality.

SYNTHETIC records and handles only; evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
import time
import unittest

from operating import briefs as br

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
EVENT = "CalendarEvent:meetings.calendar_events:synth-ev-1"
CUS = "Customer:bc.customers:synth-cus-1"
LEAD_SUP = "Supplier:bc.vendors:synth-sup-lead"
FIN = ["finance"]
OPS, ALL = frozenset({"ops"}), frozenset({"ops", "secret"})


class People:
    """The people exclusion as the briefs see it: loaded (``unavailable`` None) naming the
    synthetic ids in ``excluded``, or unavailable with a code."""

    def __init__(self, excluded=(), unavailable=None) -> None:
        self.excluded, self.unavailable = set(excluded), unavailable

    def is_excluded(self, value):
        if self.unavailable:
            raise RuntimeError(self.unavailable)
        if value == "undecidable":
            raise RuntimeError("excluded_people_unresolved")
        return value in self.excluded


PEOPLE = People()


def h(oid: str, prop: str = "") -> str:
    return "synth://" + oid.replace(":", "/") + "@1" + (f"#{prop}" if prop else "")


class Store:
    """The primitives as one reader gets them: records in audiences (``sees``), values with
    markings the reader holds (``held``; ``leaky``: values handed back regardless), links
    with tiers (a lead only in ``possibly_related``)."""

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.edges: list[tuple[str, str, str, str]] = []
        self.calls: list[tuple[str, dict]] = []
        self.sees, self.held, self.leaky = ALL, frozenset(FIN), False
        self.derived: dict[str, list[dict]] = {}    # plx.object ``derived`` per record

    def add(self, oid: str, label: str, aud: str = "ops", **values) -> str:
        rows = []
        for prop, raw in values.items():
            value, marks = raw if isinstance(raw, tuple) else (raw, [])
            rows.append({"property": prop, "value": {"untrusted_text": str(value)},
                         "markings": marks, "citation": {"l1_handle": h(oid, prop)}})
        self.objects[oid] = {"label": label, "aud": aud, "values": rows}
        return oid

    def link(self, src: str, link_type: str, dst: str, tier: str = "verified") -> None:
        self.edges.append((src, link_type, dst, tier))

    def visible(self, oid: str) -> bool:
        return oid in self.objects and self.objects[oid]["aud"] in self.sees

    def _values(self, oid: str, props=None) -> list[dict]:
        return [v for v in self.objects[oid]["values"]
                if (props is None or v["property"] in props)
                and (self.leaky or set(v["markings"]) <= self.held)]

    def _value(self, oid: str, prop: str):
        for v in self._values(oid):
            if v["property"] == prop:
                return v["value"]["untrusted_text"]
        return None

    def _match(self, oid: str, filters) -> bool:
        for f in filters or ():
            value = self._value(oid, f["property"])
            if value is None:
                return False
            if f["op"] == "gte" and not value >= f["value"]:
                return False
            if f["op"] == "lt" and not value < f["value"]:
                return False
        return True

    def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if tool == "plx.object":
            oid = args["object_id"]
            if not self.visible(oid):
                return {"error": {"code": "object_not_found", "message": "no"}}
            return {"object": {"object_id": oid, "object_type": br.type_of(oid), "display": {
                "untrusted_text": self.objects[oid]["label"], "origin": {"citation": h(oid)}}},
                "values": self._values(oid, args.get("properties")),
                **({"derived": self.derived[oid]} if oid in self.derived else {})}
        if tool == "plx.around":
            start, hop = args["from"], args["path"][0]
            if not self.visible(start):
                return {"error": {"code": "object_not_found", "message": "no"}}
            forward = hop["direction"] == "forward"
            found, leads = [], []
            for src, link, dst, tier in self.edges:
                if link != hop["link_type"]:
                    continue
                if (src if forward else dst) != start:
                    continue
                other = dst if forward else src
                if not self.visible(other):
                    continue
                (leads if tier == "lead" else found).append(other)
            offset = int(args.get("cursor") or 0)
            page = found[offset:offset + args.get("limit", 50)]
            nxt = offset + len(page)
            out = {"objects": [{"object_id": o} for o in page],
                   "completeness": {"next_cursor": str(nxt) if nxt < len(found) else None}}
            if leads:
                out["possibly_related"] = {"count": len(leads), "objects": [
                    {"object_id": o} for o in leads]}
            return out
        if tool == "plx.find":
            ids = [o for o in self.objects if br.type_of(o) == args["object_type"]
                   and self.visible(o) and self._match(o, args.get("filters"))]
            offset = int(args.get("cursor") or 0)
            page = ids[offset:offset + args.get("limit", 50)]
            nxt = offset + len(page)
            return {"candidates": [{"object": {"object_id": o}} for o in page],
                    "completeness": {"next_cursor": str(nxt) if nxt < len(ids) else None}}
        if tool == "plx.aggregate":
            if args.get("mode"):
                return {"error": {"code": "not_served", "message": "no forecast here"}}
            ids = [o for o in self.objects if br.type_of(o) == args["object_type"]
                   and self.visible(o) and self._match(o, args.get("filters"))]
            measure = args["measure"]
            if measure["op"] == "count":
                return {"groups": [{"key": {}, "value": str(len(ids)), "inputs": len(ids),
                                    "markings": [], "members_cited": [h(o) for o in ids][:50]}],
                        "partial": False, "gaps": [], "completeness": {}, "meta": {}}
            groups: dict[str, Decimal] = {}
            marks = None
            for o in ids:
                v = next((r for r in self.objects[o]["values"]
                          if r["property"] == measure["property"]), None)
                if v is None:
                    continue
                if not (self.leaky or set(v["markings"]) <= self.held):
                    continue
                marks = v["markings"]
                cur = self._value(o, "currency")
                groups[cur] = groups.get(cur, Decimal(0)) + Decimal(v["value"]["untrusted_text"])
            return {"groups": [{"key": {"currency": {"untrusted_text": c}}, "value": str(v),
                                "inputs": 1, "markings": marks or [],
                                "inputs_citations": [{"l1_handle": h(c)}]}
                               for c, v in groups.items()],
                    "partial": False, "gaps": [], "completeness": {}, "meta": {}}
        return {"error": {"code": "tool_not_configured", "message": tool}}


def gap(kind: str, oid: str, summary: str, priority: str = "medium") -> dict:
    return {"id": f"pe-{kind}-{oid}", "kind": kind, "object_id": oid,
            "object_type": br.type_of(oid), "summary": summary,
            "groups": [[[h(oid), oid, []]]], "follow_up": {"priority": priority}}


def world(n_orders: int = 2, n_mail: int = 2) -> tuple[Store, dict, list[str]]:
    st = Store()
    st.add(EVENT, "Quarterly review", start="2026-11-12T14:00:00")
    st.add(CUS, "Synth Foods")
    st.add(LEAD_SUP, "Lead Packaging")
    st.add("PurchaseOrder:bc.purchase_orders:synth-lead-po", "PO LEAD-1", status="Open")
    st.link("PurchaseOrder:bc.purchase_orders:synth-lead-po", "PurchaseOrder.ordered_from",
            LEAD_SUP)
    st.link(EVENT, "CalendarEvent.with_customer", CUS, "corroborated")
    st.link(EVENT, "CalendarEvent.with_supplier", LEAD_SUP, "lead")
    orders = []
    for i in range(n_orders):
        so = st.add(f"SalesOrder:portal.app_sales_order:synth-so-{i}", f"SO-{i:03d}",
                    order_number=f"SO-{i:03d}", status="Released",
                    required_date=f"2026-12-{1 + i % 28:02d}", currency="CAD",
                    total_amount=(f"{1000 + i}.50", FIN))
        st.link(so, "SalesOrder.ordered_by", CUS)
        orders.append(so)
    closed = st.add("SalesOrder:portal.app_sales_order:synth-so-closed", "SO-CLOSED",
                    order_number="SO-CLOSED", status="Completed")
    st.link(closed, "SalesOrder.ordered_by", CUS)
    hidden = st.add("SalesOrder:portal.app_sales_order:synth-so-hidden", "SO-HIDDEN",
                    aud="secret", order_number="SO-HIDDEN", status="Released")
    st.link(hidden, "SalesOrder.ordered_by", CUS)
    for i in range(n_mail):
        mail = st.add(f"Email:graph.mail:synth-mail-{i}", f"mail {i}",
                      subject=f"Re: order SO-{i:03d}", received_at=f"2026-11-0{1 + i % 8}T09:00:00",
                      body_preview="CONFIDENTIAL BODY TEXT")
        st.link(mail, "Email.sent_by_customer", CUS)
    old = st.add("Email:graph.mail:synth-mail-old", "old", subject="Ancient thread",
                 received_at="2025-01-01T09:00:00")
    st.link(old, "Email.sent_by_customer", CUS)
    secret_mail = st.add("Email:graph.mail:synth-mail-secret", "s", aud="secret",
                         subject="Secret pricing thread", received_at="2026-11-05T09:00:00")
    st.link(secret_mail, "Email.sent_by_customer", CUS)
    ar = st.add("AgedReceivable:bc.aged:synth-ar", "AR", balance_due=("4200.10", FIN),
                currency=("CAD", FIN), aged_as_of_date=("2026-11-08", FIN))
    st.link(ar, "AgedReceivable.of_customer", CUS)
    pay = st.add("Payment:bc.payments:synth-pay", "PAY", document_number="PAY-1",
                 posting_date=("2026-10-30", FIN), amount=("900.00", FIN))
    st.link(pay, "Payment.received_from", CUS)
    past = st.add("Meeting:meetings.meetings:synth-past", "Past review",
                  subject="Last quarter review", start="2026-08-10T14:00:00")
    st.link(past, "Meeting.with_customer", CUS)
    tr = st.add("MeetingTranscript:meetings.transcripts:synth-tr", "Transcript of review")
    st.link(tr, "MeetingTranscript.of_meeting", past)
    alerts = [gap("late_customer_order", orders[0], "Order SO-000 is 3 days late", "high"),
              gap("commitment_due", orders[0], "We promised: ship Friday"),
              gap("lot_on_hold", orders[0], "Batch for SO-000 on hold"),
              gap("negative_margin", orders[0], "Order SO-000 lost money on item cost"),
              gap("late_po", "PurchaseOrder:bc.purchase_orders:synth-other", "Not ours")]
    return st, {"status": "read", "gaps": alerts}, orders


def section(brief: dict, key: str) -> dict:
    return next(s for s in brief["sections"] if s["key"] == key)


def dumped(brief: dict) -> str:
    return json.dumps(brief)


class MeetingBrief(unittest.TestCase):
    def brief(self, st, alerts, oid=EVENT, held=FIN):
        return br.meeting_brief(st, frozenset(held), alerts, oid, now=NOW, declared=None,
                                people=PEOPLE)

    def test_a_derived_order_value_says_how_it_was_derived(self):
        # review of C-01: an order row valued from its invoices (its header reads 0) says
        # so; the amount alone would read as the order's own total
        st, alerts, orders = world()
        zeroed = st.add("SalesOrder:portal.sage_oe_order_header:synth-z", "SO-Z",
                        order_number="SO-Z", status="Released", currency="USD",
                        total_amount=("0", FIN))
        st.link(zeroed, "SalesOrder.ordered_by", CUS)
        st.derived[zeroed] = [{
            "property": "order_value", "value_type": "money", "value": "60",
            "currency": "USD", "derived_from": "invoices",
            "description": "derived: the sum of the invoices that bill it, before tax",
            "inputs": [{"l1_handle": h("SalesInvoice:x:synth-inv", "subtotal")}],
            "markings": FIN}]
        rows = {r["label"]: r for r in section(self.brief(st, alerts), "orders")["rows"]}
        self.assertEqual(rows["Order SO-Z"]["amount"], {"amount": "60.00", "currency": "USD"})
        self.assertIn("value derived from the invoices that bill it, before tax",
                      rows["Order SO-Z"]["detail"])
        self.assertNotIn("derived", rows["Order SO-000"]["detail"])

    def test_full_reader_sees_every_section_cited(self):
        st, alerts, orders = world()
        out = self.brief(st, alerts)
        self.assertEqual([p["object_id"] for p in out["parties"]], [CUS])
        self.assertEqual([p["object_id"] for p in out["possible_parties"]], [LEAD_SUP])
        # a lead-tier party is never read further
        self.assertFalse([c for c in st.calls if c[1].get("from") == LEAD_SUP])
        self.assertNotIn("LEAD-1", dumped(out))
        rows = section(out, "orders")["rows"]
        self.assertEqual([r["label"] for r in rows],
                         ["Order SO-000", "Order SO-001", "Order SO-HIDDEN"])
        self.assertEqual(rows[0]["amount"], {"amount": "1000.50", "currency": "CAD"})
        self.assertNotIn("SO-CLOSED", dumped(out))
        self.assertTrue(all(r["sources"] for s in out["sections"] for r in s["rows"]))
        self.assertEqual([r["label"] for r in section(out, "alerts")["rows"]],
                         ["Customer order past its required date", "Order lost money on item cost"])
        self.assertEqual(len(section(out, "promises")["rows"]), 1)
        self.assertEqual(len(section(out, "quality")["rows"]), 1)
        threads = section(out, "threads")["rows"]
        self.assertEqual({r["label"] for r in threads},
                         {"Mail: Re: order SO-000", "Mail: Re: order SO-001",
                          "Mail: Secret pricing thread"})
        self.assertNotIn("CONFIDENTIAL BODY", dumped(out))   # subjects and dates only
        self.assertNotIn("Ancient thread", dumped(out))
        money = section(out, "money")
        self.assertEqual(money["state"], "shown")
        self.assertEqual(money["rows"][0]["amount"], {"amount": "4200.10", "currency": "CAD"})
        self.assertIn("currency not recorded", money["rows"][1]["detail"])
        notes = section(out, "notes")["rows"]
        self.assertEqual(notes[0]["label"], "Notes: Last quarter review")
        self.assertEqual(notes[0]["date"], "2026-08-10")
        self.assertFalse([c for c in st.calls if c[0] not in (
            "plx.object", "plx.around", "plx.find")])   # no model, no write

    def test_the_customers_rd_projects_one_row_each_newest_first(self):
        st, alerts, _ = world()

        def line(n, project, customer, last_on, state="waiting_on_us"):
            g = gap("rd_project_line", f"Email:graph.mail:synth-rd-{n}", "R&D thread", "low")
            g["event"] = {"project": project, "customer": customer, "kind": "sample_request",
                          "state": state, "last_on": last_on}
            return g
        other = "Customer:bc.customers:synth-cus-2"
        alerts["gaps"] += [line(1, "p1", CUS, "2026-10-01", "waiting_on_customer"),
                           line(2, "p1", CUS, "2026-10-20"), line(3, "p2", CUS, "2026-10-05"),
                           line(4, "p3", other, "2026-10-30")]
        rows = section(self.brief(st, alerts, CUS), "rd")["rows"]
        self.assertEqual([(r["label"], r["date"]) for r in rows],
                         [("Sample request: waiting on us", "2026-10-20"),
                          ("Sample request: waiting on us", "2026-10-05")])
        self.assertEqual(len(rows[0]["sources"]), 2)     # both threads of the project cited
        # a thread the reader may not open is no part of its project, nor cited, nor counted
        out = br.meeting_brief(st, frozenset(FIN), alerts, CUS, now=NOW, declared=None,
                               people=PEOPLE, decide=lambda hs: {
                                   x: ({"reason": "no"} if "synth-rd-2" in x else None)
                                   for x in hs})
        rows = section(out, "rd")["rows"]
        self.assertEqual([(r["label"], r["date"]) for r in rows],
                         [("Sample request: waiting on us", "2026-10-05"),
                          ("Sample request: waiting on the customer", "2026-10-01")])
        self.assertNotIn("synth-rd-2", json.dumps(rows))
        self.assertEqual(section(out, "rd")["more"], 0)
        # the people exclusion unavailable: withheld; the alert list unread: said so
        blocked = self.brief_people(st, alerts, People(unavailable="x"))
        self.assertEqual((section(blocked, "rd")["state"], section(blocked, "rd")["rows"]),
                         ("blocked", []))
        self.assertEqual(section(self.brief(st, {"status": "error"}, CUS), "rd")["state"],
                         "unread")

    def brief_people(self, st, alerts, people):
        return br.meeting_brief(st, frozenset(FIN), alerts, CUS, now=NOW, declared=None,
                                people=people)

    def test_restricted_reader_sees_nothing_it_may_not(self):
        for leaky in (False, True):
            st, alerts, orders = world()
            st.sees, st.held, st.leaky = OPS, frozenset(), leaky
            out = self.brief(st, alerts, held=())
            text = dumped(out)
            for hidden in ("SO-HIDDEN", "Secret pricing", "4200.10", "1000.50", "900.00",
                           "lost money"):
                self.assertNotIn(hidden, text, (leaky, hidden))
            self.assertEqual(section(out, "money")["state"], "finance_marked")
            self.assertEqual(section(out, "money")["rows"], [])
            orders_sec = section(out, "orders")
            self.assertEqual(len(orders_sec["rows"]), 2)   # only what it may read
            self.assertEqual(orders_sec["more"], 0)
            self.assertTrue(all(r["amount"] is None for r in orders_sec["rows"]))
            self.assertNotIn("withheld", text)                  # no hidden count
            self.assertFalse([g for g in out["gaps"] if g["code"] == "withheld_from_reader"])

    def test_the_finance_section_reads_the_same_whatever_the_data(self):
        st, alerts, _ = world()
        bare = Store()
        bare.add(EVENT, "Quarterly review", start="2026-11-12T14:00:00")
        a = section(self.brief(st, alerts, held=()), "money")
        b = section(self.brief(bare, {"status": "read", "gaps": []}, held=()), "money")
        self.assertEqual(a, b)

    def test_unknown_or_unreadable_records_fail_closed(self):
        st, alerts, orders = world()
        for oid in (orders[0], "Nonsense", "", "Email:graph.mail:synth-mail-0"):
            with self.assertRaises(br.BriefRefused):
                self.brief(st, alerts, oid)
        st.sees = frozenset({"secret"})
        with self.assertRaises(br.BriefRefused):
            self.brief(st, alerts)                              # hidden event: not found
        st, alerts, _ = world()
        st.edges = [e for e in st.edges if e[2] != CUS]         # no party linked
        out = self.brief(st, alerts)
        self.assertEqual(out["parties"], [])
        self.assertTrue(all(not s["rows"] for s in out["sections"]))
        self.assertIn("No customer or supplier", section(out, "orders")["note"])

    def test_a_party_brief_and_malformed_alerts(self):
        st, alerts, _ = world()
        alerts["gaps"].append({"kind": "late_customer_order", "object_id": CUS})  # uncited
        alerts["gaps"].append("garbage")
        out = self.brief(st, alerts, CUS)
        self.assertEqual(out["subject"]["object_id"], CUS)
        self.assertEqual(len(section(out, "alerts")["rows"]), 2)
        self.assertEqual(br.meeting_brief(st, frozenset(), {"status": "not_configured"}, CUS,
                                          now=NOW, declared=None)["sections"][2]["rows"], [])

    def test_an_unread_alert_list_never_reads_as_none(self):
        st, _, _ = world()
        for inbox in ({"status": "not_configured", "gaps": []}, {"status": "error"}, None):
            out = self.brief(st, inbox)
            for key in ("alerts", "promises", "quality"):
                sec = section(out, key)
                self.assertEqual((sec["state"], sec["rows"]), ("unread", []), (inbox, key))
                self.assertIn("could not be read", sec["note"])
            self.assertIn("read_failed", {g["code"] for g in out["gaps"]
                                          if g["subject"] == "alert list"})
            self.assertEqual(section(out, "orders")["state"], "shown")

    def test_cardinality_no_row_lost_or_duplicated(self):
        for n in (1, 10, 60):
            st, _, orders = world(n_orders=n, n_mail=n)
            st.sees = OPS
            alerts = {"status": "read", "gaps": [gap("late_customer_order", o, f"late {o}")
                                                 for o in orders]}
            started = time.monotonic()
            out = self.brief(st, alerts)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 5, n)
            for key, total in (("orders", n), ("alerts", n), ("threads", n)):
                sec = section(out, key)
                self.assertEqual(len(sec["rows"]) + sec["more"], total, (n, key))
                self.assertEqual(len(sec["rows"]), min(n, br.MAX_ROWS))
                labels = [r["label"] + str(r["object_id"]) for r in sec["rows"]]
                self.assertEqual(len(labels), len(set(labels)), (n, key))
                self.assertNotEqual(sec["state"], "partial", (n, key))

    def test_budget_runs_out_into_a_partial_section_never_a_silent_one(self):
        st, alerts, _ = world(n_orders=10)
        ticks = iter(range(0, 10_000))
        out = br.meeting_brief(st, frozenset(FIN), alerts, EVENT, now=NOW, declared=None,
                               people=PEOPLE,
                               budget_seconds=12, clock=lambda: next(ticks))
        self.assertIn("partial", {s["state"] for s in out["sections"]})
        self.assertIn("budget_exhausted", {g["code"] for g in out["gaps"]})


def weekly_world(n: int, *, late_every: int = 3) -> Store:
    st = Store()
    for i in range(n):
        so = st.add(f"SalesOrder:portal.app_sales_order:w-so-{i}", f"W-{i}",
                    order_date="2026-11-03", required_date="2026-11-05")
        sh = st.add(f"Shipment:bc.shipments:w-sh-{i}", f"SH-{i}",
                    ship_date="2026-11-07" if i % late_every == 0 else "2026-11-04")
        st.link(sh, "Shipment.ships", so)
        st.add(f"SalesInvoice:bc.sales_invoices:w-inv-{i}", f"INV-{i}",
               invoice_date="2026-11-06", currency="USD" if i % 2 else "CAD",
               total_amount=("100.10", FIN))
    st.add("SalesOrder:portal.app_sales_order:w-last", "W-L", order_date="2026-10-28")
    st.add("SalesOrder:portal.app_sales_order:w-hidden", "W-H", aud="secret",
           order_date="2026-11-04")
    return st


class WeeklyReview(unittest.TestCase):
    def review(self, st, held=FIN, alerts=None, **kw):
        return br.weekly_review(st, frozenset(held), alerts or {"status": "read", "gaps": []},
                                week=date(2026, 11, 4), today=date(2026, 11, 9),
                                people=PEOPLE, **kw)

    def figure(self, out, label):
        return next(f for f in section(out, "figures")["figures"] if f["label"] == label)

    def test_figures_deltas_on_time_and_currencies(self):
        for n in (1, 10, 60):
            st = weekly_world(n)
            st.sees = OPS
            out = self.review(st)
            booked = self.figure(out, "Orders booked")
            self.assertEqual((booked["value"], booked["previous"]), (str(n), "1"))
            self.assertEqual(booked["delta"], ("+" if n > 1 else "") + str(n - 1))
            late = len([i for i in range(n) if i % 3 == 0])
            ontime = self.figure(out, "Shipped on time")
            expect = (Decimal(n - late) * 100 / n).quantize(Decimal("0.1"))
            self.assertEqual(ontime["value"], str(expect), n)
            self.assertIn(f"{n - late} of {n} shipments", ontime["note"])
            cad, usd = (self.figure(out, "Invoiced (CAD)"),
                        self.figure(out, "Invoiced (USD)") if n > 1 else None)
            self.assertEqual(Decimal(cad["value"]), Decimal("100.10") * ((n + 1) // 2))
            if usd:
                self.assertEqual(Decimal(usd["value"]), Decimal("100.10") * (n // 2))
                self.assertEqual(usd["currency"], "USD")
            self.assertTrue(booked["sources"])

    def test_restricted_reader_no_money_no_hidden_count(self):
        for leaky in (False, True):
            st = weekly_world(10)
            st.sees, st.held, st.leaky = OPS, frozenset(), leaky
            alerts = {"status": "read", "gaps": [
                gap("late_po", "PurchaseOrder:bc.purchase_orders:p1", "PO P1 late", "high"),
                gap("margin_erosion", "SalesOrder:portal.app_sales_order:w-so-1",
                    "Margin down on W-1"),
                gap("quality_hold_aging", "Batch:portal.batch:b1", "Batch B1 held 9 days")]}
            out = self.review(st, held=(), alerts=alerts)
            text = dumped(out)
            self.assertEqual(self.figure(out, "Orders booked")["value"], "10")  # hidden: not
            self.assertNotIn("Invoiced (", text)
            self.assertNotIn("100.10", text)
            self.assertNotIn("Margin down", text)
            self.assertEqual(section(out, "cash")["state"], "finance_marked")
            self.assertEqual(section(out, "margin")["state"], "finance_marked")
            self.assertEqual(len(section(out, "late_pos")["rows"]), 1)
            self.assertEqual(len(section(out, "quality")["rows"]), 1)
            self.assertNotIn("withheld", text)

    def test_a_future_week_and_an_unreadable_store(self):
        with self.assertRaises(br.BriefRefused):
            br.weekly_review(Store(), frozenset(), None, week=date(2026, 12, 7),
                             today=date(2026, 11, 9))

        def broken(tool, args):
            return {"error": {"code": "store_unavailable", "message": "down"}}
        out = br.weekly_review(broken, frozenset(FIN), None, today=date(2026, 11, 9))
        self.assertIsNone(self.figure(out, "Orders booked")["value"])
        self.assertEqual(section(out, "figures")["state"], "partial")
        self.assertIn("read_failed", {g["code"] for g in out["gaps"]})


    def test_unread_inbox_failed_shipment_find_and_dropped_groups(self):
        st = weekly_world(4)
        st.sees = OPS

        def mixed(tool, args):
            if tool == "plx.find" and args.get("object_type") == "Shipment":
                return {"error": {"code": "store_unavailable", "message": "down"}}
            out = st(tool, args)
            if tool == "plx.aggregate" and args.get("object_type") == "SalesOrder":
                out = {**out, "groups": out["groups"] + [
                    {"key": {}, "value": "7", "inputs": 7, "markings": ["secret_mark"]}]}
            return out
        out = br.weekly_review(mixed, frozenset(FIN), {"status": "not_configured"},
                               week=date(2026, 11, 4), today=date(2026, 11, 9))
        for key in ("late_pos", "top_alerts", "margin", "quality"):
            self.assertEqual(section(out, key)["state"], "unread", key)
        ontime = self.figure(out, "Shipped on time")
        self.assertIsNone(ontime["value"])
        self.assertEqual(ontime["note"], "could not be read")
        booked = self.figure(out, "Orders booked")
        self.assertEqual(booked["value"], "4")              # the dropped group: not counted
        self.assertEqual(section(out, "figures")["state"], "partial")
        self.assertNotIn("7", booked["value"] + (booked["note"] or ""))

    def test_an_unknown_count_or_total_is_never_zero(self):
        st = weekly_world(4)

        def twins(tool, args):
            out = st(tool, args)
            if tool == "plx.aggregate" and args.get("object_type") in ("SalesOrder",
                                                                      "SalesInvoice"):
                out = {**out, "partial": True, "groups": [{**g, "value": None}
                                                          for g in out["groups"]],
                       "gaps": [{"code": "cross_system_twins", "state": "unknown",
                                 "detail": "one record in two systems"},
                                {"code": "withheld", "state": "inaccessible",
                                 "detail": "3 withheld"}]}
            return out
        out = self.review(twins)
        self.assertIsNone(self.figure(out, "Orders booked")["value"])
        self.assertIsNone(self.figure(out, "Invoices issued")["value"])
        self.assertFalse([f for f in section(out, "figures")["figures"]
                          if f["label"].startswith("Invoiced (")])
        codes = {g["code"] for g in out["gaps"]}
        self.assertIn("cross_system_twins", codes)
        self.assertNotIn("3 withheld", dumped(out))


if __name__ == "__main__":
    unittest.main()


def people_world(n_mail: int = 3):
    """``world`` plus a sender person per mail (the first one excluded, the second one
    undecidable), an action item citing the last meeting's transcript and an overdue aged
    balance."""
    st, alerts, orders = world(n_mail=n_mail)
    for i in range(n_mail):
        ident = {0: "synth-oid-excluded", 1: "undecidable"}.get(i, f"synth-oid-{i}")
        person = st.add(f"Person:graph.users:synth-p{i}", f"Sender {i}", object_id=ident)
        st.link(f"Email:graph.mail:synth-mail-{i}", "Email.sent_by_person", person)
    st.objects["AgedReceivable:bc.aged:synth-ar"]["values"] += [
        {"property": f"period{k}_amount", "value": {"untrusted_text": v}, "markings": FIN,
         "citation": {"l1_handle": h("AgedReceivable:bc.aged:synth-ar", f"p{k}")}}
        for k, v in ((1, "100.10"), (2, "0.20"), (3, "50"))]
    st.objects[orders[0]]["values"] = [
        dict(v, value={"untrusted_text": "2026-11-01"}) if v["property"] == "required_date"
        else v for v in st.objects[orders[0]]["values"]]
    alerts["gaps"].append(gap("commitment_due", "MeetingTranscript:meetings.transcripts:synth-tr",
                              "Send the revised spec"))
    return st, alerts, orders


class PrepBriefAdditions(unittest.TestCase):
    """Who sent each thread, the people exclusion failing closed, item access, talking
    points, overdue balances and the last meeting's action items."""

    def brief(self, st, alerts, *, held=FIN, people=PEOPLE, decide=None, oid=EVENT):
        return br.meeting_brief(st, frozenset(held), alerts, oid, now=NOW, declared=None,
                                people=people, decide=decide)

    def test_threads_name_who_and_drop_excluded_or_undecidable_senders(self):
        st, alerts, _ = people_world(n_mail=4)
        st.sees = OPS
        out = self.brief(st, alerts, people=People(excluded={"synth-oid-excluded"}))
        rows = section(out, "threads")["rows"]
        self.assertEqual(sorted(r["who"] for r in rows), ["Sender 2", "Sender 3"])
        self.assertEqual(section(out, "threads")["more"], 0)
        text = dumped(out)
        for word in ("Sender 0", "Sender 1", "Re: order SO-000", "Re: order SO-001",
                     "Secret pricing"):
            self.assertNotIn(word, text)
        for row in rows:
            self.assertIn(h(row["object_id"].replace("Email:graph.mail:synth-mail-",
                                                     "Person:graph.users:synth-p")),
                          {s["handle"] for s in row["sources"]})

    def test_people_exclusion_unavailable_fails_closed(self):
        st, alerts, _ = people_world()
        for people in (None, People(unavailable="excluded_people_oids_absent")):
            out = self.brief(st, alerts, people=people)
            for key in ("threads", "notes"):
                sec = section(out, key)
                self.assertEqual((sec["state"], sec["rows"], sec["more"]), ("blocked", [], 0))
            self.assertIn("people_exclusion_unavailable", {g["code"] for g in out["gaps"]})
            text = dumped(out)
            self.assertEqual(section(out, "promises")["state"], "blocked")
            for word in ("Re: order", "Sender", "Transcript", "revised spec", "We promised"):
                self.assertNotIn(word, text, people)
            # nothing about people was even read
            self.assertFalse([c for c in st.calls if "Email" in json.dumps(c[1])
                              and c[0] == "plx.around" and c[1]["from"] == CUS
                              and people is None and False])
            weekly = br.weekly_review(weekly_world(2), frozenset(FIN), {"status": "read", "gaps": [
                gap("customer_chasing_order", "SalesOrder:portal.app_sales_order:w-so-0",
                    "Customer asked where it is")]}, week=date(2026, 11, 4),
                today=date(2026, 11, 9), people=people)
            for key in ("asks", "commitments_missed"):
                self.assertEqual(section(weekly, key)["state"], "blocked")
            self.assertNotIn("Customer asked", dumped(weekly))

    def test_item_access_drops_rows_uncounted_and_fails_closed(self):
        st, alerts, orders = people_world()
        refused = h(orders[0], "status")
        out = self.brief(st, alerts, decide=lambda hs: {x: ({"reason": "item_refused"}
                                                            if x == refused else None)
                                                        for x in hs})
        sec = section(out, "orders")
        self.assertNotIn(orders[0], [r["object_id"] for r in sec["rows"]])
        self.assertIn(orders[1], [r["object_id"] for r in sec["rows"]])
        self.assertEqual(sec["more"], 0)
        self.assertNotIn("Agree a new date for Order SO-000", dumped(out))

        def broken(_hs):
            raise RuntimeError("policy store down")
        out = self.brief(st, alerts, decide=broken)
        for sec in out["sections"]:
            self.assertEqual(sec["rows"], [], sec["key"])
            self.assertEqual(sec["more"], 0)
        self.assertNotIn("SO-00", dumped(out))
        self.assertIn("item access", {g["subject"] for g in out["gaps"]})

    def test_talking_points_overdue_and_action_items_are_cited_lines(self):
        st, alerts, orders = people_world()
        out = self.brief(st, alerts)
        points = section(out, "talking_points")["rows"]
        self.assertTrue(points)
        self.assertLessEqual(len(points), br.MAX_POINTS)
        shown = {s["handle"] for sec in out["sections"] if sec["key"] != "talking_points"
                 for r in sec["rows"] for s in r["sources"]}
        for p in points:
            self.assertTrue(p["sources"], p)
            self.assertLessEqual({s["handle"] for s in p["sources"]}, shown, p)
        self.assertTrue(points[0]["label"].startswith("Agree a new date for Order SO-000"))
        self.assertIn("days past its required date", section(out, "orders")["rows"][0]["detail"])
        money = section(out, "money")["rows"]
        overdue = next(r for r in money if r["label"] == "Overdue")
        self.assertEqual(overdue["amount"], {"amount": "150.30", "currency": "CAD"})
        self.assertIn("Ask about the overdue balance", {p["label"] for p in points})
        notes = section(out, "notes")["rows"]
        self.assertIn("Action item: Promise due", {r["label"] for r in notes})
        # a reader without finance: no money line, no overdue point, no amount at all
        st.sees = OPS
        st.held = frozenset()
        out = self.brief(st, alerts, held=())
        self.assertEqual(section(out, "money")["state"], "finance_marked")
        text = dumped(out)
        for word in ("150.30", "4200.10", "overdue balance", "lost money", "1000.50"):
            self.assertNotIn(word, text)

    def test_one_unread_aging_bucket_states_no_overdue(self):
        st, alerts, _ = people_world()
        st.objects["AgedReceivable:bc.aged:synth-ar"]["values"] = [
            v for v in st.objects["AgedReceivable:bc.aged:synth-ar"]["values"]
            if v["property"] != "period2_amount"]
        out = self.brief(st, alerts)
        self.assertNotIn("Overdue", {r["label"] for r in section(out, "money")["rows"]})

    def test_cardinality_with_senders(self):
        for n in (1, 10, 60):
            st, alerts, _ = people_world(n_mail=max(n, 2))
            st.sees = OPS
            started = time.monotonic()
            out = self.brief(st, alerts, people=People())
            self.assertLess(time.monotonic() - started, 5)
            sec = section(out, "threads")
            # the undecidable sender's row is dropped; every other row is shown or counted
            self.assertEqual(len(sec["rows"]) + sec["more"], max(n, 2) - 1, n)
            ids = [r["object_id"] for r in sec["rows"]]
            self.assertEqual(len(ids), len(set(ids)))


class WeeklyAdditions(unittest.TestCase):
    WEEK, TODAY = date(2026, 11, 2), date(2026, 11, 9)

    def alerts(self, n: int) -> dict:
        gaps = []
        for i in range(n):
            g = gap("promise_date_slip", f"SalesOrder:portal.app_sales_order:w-so-{i}",
                    f"Promised date moved: carrier delay {i}")
            g.update(first_seen="2026-11-03T10:00:00Z", last_seen="2026-11-08T10:00:00Z",
                     state="open")
            gaps.append(g)
        closed = gap("late_po", "PurchaseOrder:bc.purchase_orders:wp", "PO late")
        closed.update(first_seen="2026-10-20T10:00:00Z", last_seen="2026-11-05T10:00:00Z",
                      state="resolved")
        missed = gap("commitment_missed", "SalesOrder:portal.app_sales_order:w-so-0",
                     "Promise missed: send COA")
        missed.update(first_seen="2026-11-04T10:00:00Z", state="open")
        ar = gap("aged_receivable_worsening", "Customer:bc.customers:wc", "AR aging worse")
        ask = gap("customer_chasing_order", "SalesOrder:portal.app_sales_order:w-so-0",
                  "Customer chased the order")
        return {"status": "read", "with_resolved": True,
                "gaps": gaps + [closed, missed, ar, ask]}

    def review(self, st, alerts, held=FIN, **kw):
        return br.weekly_review(st, frozenset(held), alerts, week=self.WEEK, today=self.TODAY,
                                people=PEOPLE, **kw)

    def test_sections_flow_and_cardinality(self):
        for n in (1, 10, 60):
            st = weekly_world(n)
            started = time.monotonic()
            out = self.review(st, self.alerts(n))
            self.assertLess(time.monotonic() - started, 5)
            slips = section(out, "slips")
            self.assertEqual(len(slips["rows"]), min(n, br.TOP_ALERTS))
            self.assertIn("carrier delay", slips["rows"][0]["detail"])
            flow = {f["label"]: f["value"] for f in section(out, "alert_flow")["figures"]}
            self.assertEqual(flow, {"Alerts opened": str(n + 1), "Alerts closed": "1"})
            self.assertEqual(len(section(out, "commitments_missed")["rows"]), 1)
            self.assertEqual(len(section(out, "asks")["rows"]), 1)
            self.assertEqual(len(section(out, "cash")["rows"]), 1)
            late = next(f for f in section(out, "figures")["figures"]
                        if f["label"] == "Shipped late")
            self.assertEqual(late["value"], str(len(range(0, n, 3))))
            # a resolved alert is never an open one
            self.assertNotIn("PO late", json.dumps(section(out, "top_alerts")))

    def test_restricted_reader_and_open_only_list(self):
        st = weekly_world(3)
        st.held = frozenset()
        alerts = self.alerts(2)
        alerts.pop("with_resolved")
        out = self.review(st, alerts, held=())
        for key in ("cash", "margin"):
            self.assertEqual(section(out, key)["state"], "finance_marked")
        self.assertNotIn("AR aging worse", dumped(out))
        closed = next(f for f in section(out, "alert_flow")["figures"]
                      if f["label"] == "Alerts closed")
        self.assertIsNone(closed["value"])


class Archive(unittest.TestCase):
    def test_kept_week_is_redecided_and_unfinished_weeks_never_kept(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            arc = br.WeeklyArchive(Path(tmp) / "weekly.sqlite")
            raw = br.weekly_review(weekly_world(2), frozenset(FIN), {"status": "read",
                                   "gaps": []}, week=date(2026, 11, 2),
                                   today=date(2026, 11, 9), people=PEOPLE)
            ok = lambda hs: {x: None for x in hs}  # noqa: E731
            week = date(2026, 11, 2)
            self.assertTrue(arc.put("r1", week, "g1", raw, date(2026, 11, 9)))
            self.assertFalse(arc.put("r1", date(2026, 11, 9), "g1", raw, date(2026, 11, 12)))
            got = arc.get("r1", week, "g1", ok)
            self.assertEqual(got["sections"], raw["sections"])
            self.assertIn("stored_at", got)
            self.assertIsNone(arc.get("r2", week, "g1", ok))        # another reader
            self.assertIsNone(arc.get("r1", week, "g2", ok))        # a changed grant
            self.assertIsNone(arc.get("r1", week, "g1", None))      # undecidable
            self.assertIsNone(arc.get("r1", week, "g1", lambda hs: {
                x: {"reason": "item_refused"} for x in hs}))         # a revoked item
            self.assertEqual(arc.weeks("r1", "g1"), ["2026-11-02"])
            self.assertEqual(arc.weeks("r1", "g2"), [])
            self.assertEqual(oct((Path(tmp) / "weekly.sqlite").stat().st_mode & 0o777), "0o600")



class AgendaBriefOption(unittest.TestCase):
    """``plx.agenda`` option ``brief``: the brief read through the host table's primitives
    as the caller, its answer within the tool's output contract."""

    def tool(self, st, held=FIN):
        from operating.meeting_agenda import AgendaTool
        return AgendaTool(lambda name, args, actor: st(name, args), None,
                          held_markings=lambda actor: held)

    def test_brief_and_weekly_through_the_tool(self):
        import os
        from unittest import mock
        from ontology.json_contract import validate
        from operating import ontology_tools
        contracts = ontology_tools.load_contracts()
        out_schema = contracts["tools"]["plx.agenda"]["output"]
        st, _, _ = world()
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": "",
                                          "PLX_EXCLUSION_ENV_FILE": ""}):
            out = self.tool(st).call({"brief": CUS}, "actor")
            self.assertEqual(out["schema"], br.MEETING_SCHEMA)
            self.assertEqual(validate(out, out_schema, root=contracts), [])
            # no exclusion configured: people's lines fail closed
            self.assertEqual(section(out, "threads")["state"], "blocked")
            self.assertNotIn("Re: order", dumped(out))
            # the alert list is not read here: said, never empty
            self.assertEqual(section(out, "alerts")["state"], "unread")
            weekly = self.tool(weekly_world(3)).call({"brief": "weekly", "week": "2026-11-02"},
                                                     "actor")
            self.assertEqual(weekly["schema"], br.WEEKLY_SCHEMA)
            self.assertEqual(validate(weekly, out_schema, root=contracts), [])
        # restricted reader: no money at all
        st.held = frozenset()
        out = self.tool(st, held=frozenset()).call({"brief": CUS}, "actor")
        self.assertEqual(section(out, "money")["state"], "finance_marked")
        self.assertNotIn("4200.10", dumped(out))

    def test_unknown_or_ambiguous_input_fails_closed(self):
        from operating.meeting_agenda import AgendaTool
        st, _, _ = world()
        tool = self.tool(st)
        for args, code in (({}, "invalid_input"), ({"brief": CUS, "agenda": "x"}, "invalid_input"),
                           ({"brief": "Invoice:bc.x:synth-1"}, "brief_refused"),
                           ({"brief": "weekly", "week": "2026-13-45"}, "invalid_input"),
                           ({"brief": CUS, "week": "2026-11-02"}, "invalid_input"),
                           ({"brief": "Customer:bc.customers:absent"}, "brief_refused")):
            self.assertEqual(tool.call(args, "actor")["error"]["code"], code, args)
        bare = AgendaTool(lambda name, args, actor: st(name, args), None)
        self.assertEqual(bare.call({"brief": CUS}, "actor")["error"]["code"],
                         "brief_not_served")


class StandingReviewPack(unittest.TestCase):
    """A recurring internal review (no outside party) gets its pack and agenda from the
    reader's own alerts, built live; a row the reader may not open is not counted."""

    def world(self, title: str = "Weekly incoming ETAs review"):
        st = Store()
        st.add(EVENT, title, start="2026-11-12T14:00:00")
        pos = [f"PurchaseOrder:bc.purchase_orders:synth-po-{i}" for i in range(3)]
        for i, po in enumerate(pos):
            st.add(po, f"PO-{i}", status="Open")
        alerts = [gap("eta_slipped", pos[0], "PO-0 delivery moved a week", "high"),
                  gap("late_po", pos[1], "PO-1 is 4 days late"),
                  gap("late_po", pos[2], "PO-2 is 9 days late"),
                  gap("approval_waiting", pos[0], "An approval has waited 3 days"),
                  gap("production_late", pos[1], "Not this review")]
        return st, {"status": "read", "gaps": alerts}, pos

    def brief(self, st, alerts, decide=None):
        return br.meeting_brief(st, frozenset(FIN), alerts, EVENT, now=NOW, declared=None,
                                people=PEOPLE, decide=decide)

    def test_the_review_pack_and_agenda_come_from_the_readers_alerts(self):
        st, alerts, pos = self.world()
        out = self.brief(st, alerts)
        self.assertEqual(out["review"], ["incoming"])
        incoming = section(out, "review_incoming")
        self.assertEqual(len(incoming["rows"]), 3)
        self.assertNotIn("Not this review", dumped(incoming))
        self.assertEqual(len(section(out, "review_waiting")["rows"]), 1)
        agenda = section(out, "agenda")
        self.assertEqual([r["label"] for r in agenda["rows"]],
                         ["1. Incoming deliveries and ETAs", "2. waiting on someone"])
        self.assertTrue(agenda["rows"][0]["detail"].startswith("3 open items"))
        self.assertTrue(all(r["sources"] for r in agenda["rows"]))
        # an alert on a record the reader may not open is neither listed nor counted
        hidden = h(pos[2])
        out = self.brief(st, alerts, decide=lambda hs: {x: ({"reason": "item"}
                                                              if x == hidden else None)
                                                          for x in hs})
        self.assertEqual(len(section(out, "review_incoming")["rows"]), 2)
        self.assertTrue(section(out, "agenda")["rows"][0]["detail"].startswith("2 open items"))
        self.assertNotIn("PO-2 is 9 days late", dumped(out))

    def test_a_meeting_with_a_party_or_no_review_words_gets_no_pack(self):
        st, alerts, _ = self.world("Lunch")
        out = self.brief(st, alerts)
        self.assertEqual(out["review"], [])
        self.assertFalse([s for s in out["sections"] if s["key"].startswith("review_")
                          or s["key"] == "agenda"])


class UpcomingCalendar(unittest.TestCase):
    """The Meetings calendar (``upcoming_meetings``): who, where, agenda and parties of each
    event as the reader may read them; the people exclusion fails closed; a private event
    shows its time only; N=1/10/60 cardinality."""

    SUP = "Supplier:bc.vendors:synth-sup-1"

    def cal(self, n: int = 1, **over) -> Store:
        st = Store()
        st.add(CUS, "Synth Foods")
        st.add(self.SUP, "Synth Packaging", aud="secret")
        for i in range(n):
            oid = f"CalendarEvent:meetings.calendar_events:synth-up-{i}"
            values = dict(
                subject=f"Review {i}", start=f"2026-11-{10 + i % 5:02d}T{8 + i % 9:02d}:{i % 60:02d}:00",
                end=f"2026-11-{10 + i % 5:02d}T{9 + i % 9:02d}:00:00", is_cancelled="false",
                mailbox="me@synth-home.example", location="Room 1",
                body_preview="1. Orders\n2. Quality", content="read", is_online_meeting="true",
                online_meeting=json.dumps({"join_url": "https://teams.microsoft.com/l/x"}),
                organizer=json.dumps({"name": "Org Person", "address": "org@synth-home.example"}),
                attendees=json.dumps([
                    {"name": "Org Person", "address": "org@synth-home.example",
                     "type": "required", "response": "accepted"},
                    {"name": "Out Side", "address": "buyer@synth-foods.example",
                     "type": "optional", "response": "tentativelyAccepted"},
                    {"name": "No Reply", "address": "nr@synth-home.example"}]))
            values.update(over)
            st.add(oid, f"Review {i}", **{k: v for k, v in values.items() if v is not None})
            st.link(oid, "CalendarEvent.with_customer", CUS)
            st.link(oid, "CalendarEvent.with_supplier", self.SUP)
        return st

    def read(self, st: Store, people=PEOPLE, held=frozenset(FIN)) -> list[dict]:
        st.held = held
        return br.upcoming_meetings(st, held, now=NOW, people=people,
                                    since=datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc))

    def test_each_event_names_its_people_place_agenda_and_parties_cited(self):
        st = self.cal()
        st.sees = OPS   # the supplier is in an audience this reader does not hold
        [e] = self.read(st)
        self.assertEqual((e["label"], e["location"], e["online"]), ("Review 0", "Room 1", True))
        self.assertEqual(e["join_url"], "https://teams.microsoft.com/l/x")
        self.assertEqual(e["agenda"], "1. Orders 2. Quality")
        self.assertEqual(e["organizer"]["name"], "Org Person")
        self.assertEqual(e["organizer"]["role"], "organizer")
        # the organizer is not listed again among the attendees
        self.assertEqual([(a["name"], a["role"], a["response"], a["outside"])
                          for a in e["attendees"]],
                         [("Out Side", "optional", "tentativelyaccepted", True),
                          ("No Reply", "required", None, False)])
        self.assertEqual([p["object_id"] for p in e["parties"]], [CUS])   # supplier unseen
        self.assertNotIn("Synth Packaging", json.dumps(e))
        self.assertTrue(e["sources"] and all(s["handle"].startswith("synth://")
                                             for s in e["sources"]))
        self.assertEqual(len({s["handle"] for s in e["sources"]}), len(e["sources"]))

    def test_people_exclusion_fails_closed(self):
        # an excluded or undecidable address drops the whole event
        self.assertEqual(self.read(self.cal(), People({"buyer@synth-foods.example"})), [])
        st = self.cal(attendees=json.dumps([{"name": "U", "address": "undecidable"}]))
        self.assertEqual(self.read(st), [])
        # the exclusion not loaded: the event's time and subject, no people, no agenda
        [e] = self.read(self.cal(), People(unavailable="exclusion_not_loaded"))
        self.assertTrue(e["people_withheld"])
        self.assertEqual((e["organizer"], e["attendees"], e["agenda"]), (None, [], None))
        self.assertNotIn("Out Side", json.dumps(e))
        [e] = self.read(self.cal(), None)
        self.assertTrue(e["people_withheld"])

    def test_private_marked_and_malformed_input_shows_nothing_extra(self):
        [e] = self.read(self.cal(content="withheld_sensitivity"))
        self.assertTrue(e["private"])
        self.assertEqual((e["location"], e["agenda"], e["join_url"], e["organizer"],
                          e["attendees"], e["parties"]), (None, None, None, None, [], []))
        # a join link off Teams, attendees not a list, an organizer not an object
        [e] = self.read(self.cal(
            online_meeting=json.dumps({"join_url": "https://evil.example/teams.microsoft.com"}),
            attendees="{not json", organizer=json.dumps(["x"])))
        self.assertEqual((e["join_url"], e["attendees"], e["organizer"]), (None, [], None))
        # a marked attendees cell the reader does not hold is never shown
        [e] = self.read(self.cal(attendees=(json.dumps([{"name": "Marked", "address":
                                                          "m@synth-home.example"}]), ["hr"])))
        self.assertEqual(e["attendees"], [])
        self.assertNotIn("Marked", json.dumps(e))
        # a cancelled event is not listed
        self.assertEqual(self.read(self.cal(is_cancelled="true")), [])

    def test_cardinality_no_event_lost_or_duplicated(self):
        for n in (1, 10, 60):
            started = time.monotonic()
            got = self.read(self.cal(n))
            self.assertEqual(len(got), n, n)
            self.assertEqual(len({e["object_id"] for e in got}), n, n)
            self.assertEqual([e["start"] for e in got], sorted(e["start"] for e in got))
            self.assertLess(time.monotonic() - started, 5.0, n)
