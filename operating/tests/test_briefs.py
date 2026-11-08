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
                "values": self._values(oid, args.get("properties"))}
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
        return br.meeting_brief(st, frozenset(held), alerts, oid, now=NOW, declared=None)

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
                                          now=NOW, declared=None)["sections"][1]["rows"], [])

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
    def review(self, st, held=FIN, alerts=None):
        return br.weekly_review(st, frozenset(held), alerts or {"status": "read", "gaps": []},
                                week=date(2026, 11, 4), today=date(2026, 11, 9))

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
