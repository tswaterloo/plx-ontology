"""Alerts as SQL rules (operating.pg.alerts) over business tables of the declared shape
(``business_views.VIEWS`` columns, door v2 class columns) in a throwaway database:
each rule fires, keeps its since, resolves; an unrun rule changes nothing; a restricted
reader sees only the alerts every source row of which it may see. isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import unittest
from pathlib import Path
from decimal import Decimal

from operating.pg import alerts as al
from operating.pg import business_views as bv
from operating.tests import pg_testdb

S = "biz_alert_t"
DAY = "2026-11-17"
TABLES = ("purchase_orders", "sales_orders", "batches", "sales_invoice_lines", "customers",
          "sales_invoices", "sales_order_lines", "purchase_order_lines")
# the emails table as the comms build makes it (operating.pg.business_comms: one row per
# mailbox copy, door v2 ``unit``/``ai_ok``/``excluded``) with the reply flags its build adds
# (is_bulk, owner_role over all the person's addresses, owner_replied_at per thread); which
# sender is the owner the runner reads from the owners' addresses (``OWNERS`` below)
EMAIL_COLS = ("handle text, unit integer, ai_ok boolean, excluded boolean, mailbox text, "
              "mailbox_shared boolean, bulk_sender boolean, is_bulk boolean, "
              "owner_role text, owner_replied_at timestamptz, sender text, sender_domain text, "
              "subject text, records text[], object_ids text[], "
              "to_addresses text[], cc_addresses text[], thread text, message_id text, "
              "received_at timestamptz, sent_at timestamptz")
ME = "ann@ours.example"
# per mailbox id its owner's addresses (the mail capture's, in production): ann's mailbox
# id is her address here; cy's mailbox knows two addresses
OWNERS = {ME: [ME, "ann.b@ours.example"], "cy@ours.example": ["cy@ours.example", "cy.alias@ours.example"]}


class AlertRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        cls.conn = psycopg.connect(cls.dsn, autocommit=True)
        cls.addClassCleanup(cls.conn.close)
        with open(bv.MIGRATION, encoding="utf-8") as fh:
            cls.conn.execute(re.sub(r"\bbiz\b", S, fh.read()))

    def setUp(self):
        for t in TABLES:
            cols = ", ".join(f'"{n}" {k}' for n, k in bv.columns(bv.VIEWS[t]))
            self.conn.execute(f"DROP TABLE IF EXISTS {S}.{t}")
            self.conn.execute(f"CREATE TABLE {S}.{t} ({cols})")
        self.conn.execute(f"DROP TABLE IF EXISTS {S}.emails")
        self.conn.execute(f"CREATE TABLE {S}.emails ({EMAIL_COLS})")
        for t in ("alerts", "alert_runs"):
            self.conn.execute(f"DROP TABLE IF EXISTS {S}.{t}")
        self.conn.execute(f"DELETE FROM {S}.col_classes WHERE id <> 0")
        self.conn.execute(f"DELETE FROM {S}.lineage")
        # the ontology's current values, as the served view shows them (a copy's own times)
        self.conn.execute("CREATE SCHEMA IF NOT EXISTS ontology")
        self.conn.execute("DROP TABLE IF EXISTS ontology.object_values")
        self.conn.execute("CREATE TABLE ontology.object_values (object_id text, property text, "
                          "value text, recorded_at text, superseded_at text, binding_id text, "
                          "field_path text)")
        self.conn.execute("DROP TABLE IF EXISTS ontology.links")
        self.conn.execute("CREATE TABLE ontology.links (link_type text, from_object text, "
                          "to_object text, tier text, superseded_at text)")

    # ------------------------------------------------------------------ helpers
    def put(self, table, handle, cls=1, ai=True, col=0, **values):
        values.update(handle=handle, access_class=cls, ai_ok=ai, col_class=col)
        names = ", ".join(f'"{k}"' for k in values)
        self.conn.execute(f"INSERT INTO {S}.{table} ({names}) VALUES "
                          f"({', '.join(['%s'] * len(values))})", list(values.values()))

    def update(self, table, handle, **values):
        sets = ", ".join(f'"{k}" = %s' for k in values)
        self.conn.execute(f"UPDATE {S}.{table} SET {sets} WHERE handle = %s",
                          [*values.values(), handle])

    def fire(self, *rules, day=DAY):
        got = al.run(self.dsn, list(rules) or None, schema=S, day=day, owners=OWNERS)
        for g in got:   # a failing rule's reason in the test output
            if g["status"] != "ok":
                print(g["rule"], g["status"], g.get("reason"), file=sys.stderr)
        return {g["rule"]: g for g in got}

    def rows(self, rule):
        return {r[0]: dict(zip(("handle", "topic", "state", "since", "why", "first_seen"), r))
                for r in self.conn.execute(
                    f"SELECT handle, topic, state, since, why, first_seen FROM {S}.alerts "
                    "WHERE rule = %s", (rule,))}

    def po(self, handle, days_late, verdict="late", cls=1, **kw):
        req = dt.date(2026, 11, 17) - dt.timedelta(days=days_late)
        self.put("purchase_orders", handle, cls=cls, po_number=handle.upper(),
                 supplier="Supplier " + handle, verdict=verdict, days_late=days_late,
                 requested_date=req, status="open", **kw)

    def mail(self, handle, hours_ago=5, unit=7, known=True, from_owner=False, **kw):
        values = dict(handle=handle, unit=unit, ai_ok=True, excluded=False, mailbox=ME,
                      mailbox_shared=False, bulk_sender=False, is_bulk=False,
                      owner_role="to", sender="bo@client.example", subject="Quote for May",
                      to_addresses=[ME], cc_addresses=[], thread="t-" + handle,
                      message_id="<" + handle + ">",
                      received_at=dt.datetime(2026, 11, 17, 12, tzinfo=dt.timezone.utc)
                      - dt.timedelta(hours=hours_ago))
        values.update(kw)
        if from_owner:   # sent from one of the mailbox owner's addresses
            values["sender"] = OWNERS[values["mailbox"]][-1]
        values["sender_domain"] = values["sender"].rpartition("@")[2]
        self._insert(values)
        if known and values["sender"] and not from_owner:
            # the owner wrote to this sender once before, in another thread (a correspondent)
            self.sent("k-" + handle, to=[values["sender"]], mailbox=values["mailbox"],
                      days_ago=60)

    def sent(self, handle, to, mailbox=ME, days_ago=0.0, thread=None, cc=()):
        """A message the mailbox owner sent ``days_ago`` before DAY noon."""
        at = (dt.datetime(2026, 11, 17, 12, tzinfo=dt.timezone.utc)
              - dt.timedelta(days=days_ago))
        self._insert(dict(handle=handle, unit=7, ai_ok=True, excluded=False, mailbox=mailbox,
                          mailbox_shared=False, bulk_sender=False, is_bulk=False,
                          owner_role="none", sender=mailbox,
                          sender_domain=mailbox.rpartition("@")[2],
                          subject="sent", to_addresses=list(to), cc_addresses=list(cc),
                          thread=thread or "t-" + handle, message_id="<" + handle + ">",
                          received_at=at, sent_at=at))

    def _insert(self, values):
        names = ", ".join(f'"{k}"' for k in values)
        self.conn.execute(f"INSERT INTO {S}.emails ({names}) VALUES "
                          f"({', '.join(['%s'] * len(values))})", list(values.values()))

    def read(self, classes, **kw):
        return {(r["rule"], r["handle"]) for r in al.read(self.conn, classes=classes,
                                                           schema=S, **kw)}

    def slack_history(self, cust="hist", n=10, slip=2, cls=1, inv_cls=None):
        """``n`` dated closed orders of record of ``cust``, each first invoiced ``slip``
        days after its required date (what "usual" lateness is measured on)."""
        for i in range(n):
            h = f"{cust}-h{i}"
            req = dt.date(2026, 6, 1) + dt.timedelta(days=7 * i)
            self.put("sales_orders", h, cls=cls, order_number=h.upper(), status="closed",
                     of_record=True, customer_handle=cust, order_date=req,
                     required_date=req)
            self.put("sales_invoices", h + "-i", cls=inv_cls or cls, order_number=h.upper(),
                     invoice_date=req + dt.timedelta(days=slip), pilot=False)

    # ------------------------------------------------------------------ lifecycle
    def test_late_po_fires_keeps_since_and_resolves_and_reopens(self):
        self.po("p1", 3)
        self.assertEqual(self.fire("po_late")["po_late"]["status"], "ok")
        first = self.rows("po_late")["p1"]
        self.assertEqual(first["state"], "open")
        self.assertEqual(first["since"].date(), dt.date(2026, 11, 15))
        self.assertIn("3 days past its requested date", first["why"])
        self.update("purchase_orders", "p1", days_late=4)
        out = self.fire("po_late", day="2026-11-18")["po_late"]
        again = self.rows("po_late")["p1"]
        self.assertEqual((out["open"], out["opened"]), (1, 0))
        self.assertEqual((again["since"], again["first_seen"]),
                         (first["since"], first["first_seen"]))
        self.assertIn("4 days", again["why"])
        self.update("purchase_orders", "p1", verdict="neither", days_late=None)
        self.assertEqual(self.fire("po_late")["po_late"]["resolved"], 1)
        self.assertEqual(self.rows("po_late")["p1"]["state"], "resolved")
        self.update("purchase_orders", "p1", verdict="late", days_late=2)
        self.assertEqual(self.fire("po_late")["po_late"]["opened"], 1)
        back = self.rows("po_late")["p1"]
        self.assertEqual(back["state"], "open")
        self.assertGreater(back["first_seen"], first["first_seen"])

    def test_po_due_today_unconfirmed_and_po_undated_only_while_recent(self):
        self.put("purchase_orders", "due", po_number="D1", supplier="S", status="open",
                 verdict="neither", requested_date=dt.date(2026, 11, 17))
        self.put("purchase_orders", "promised", po_number="D2", supplier="S", status="open",
                 verdict="neither", requested_date=dt.date(2026, 11, 17),
                 promised_date=dt.date(2026, 11, 20))
        self.put("purchase_orders", "draft", po_number="D3", supplier="S", status="draft",
                 verdict="neither", requested_date=dt.date(2026, 11, 17))
        self.put("purchase_orders", "tomorrow", po_number="D4", supplier="S", status="open",
                 verdict="neither", requested_date=dt.date(2026, 11, 18))
        for h, day, status, verdict, why in (
                ("new", dt.date(2026, 10, 1), "open", "unknown", "no_requested_date"),
                ("old", dt.date(2025, 1, 1), "open", "unknown", "no_requested_date"),
                ("nodate", None, "open", "unknown", "no_requested_date"),
                ("unknownstatus", dt.date(2026, 10, 1), "unknown", "unknown", "status_unknown"),
                ("draftund", dt.date(2026, 10, 1), "draft", "neither", None)):
            self.put("purchase_orders", h, po_number=h.upper(), supplier="S", status=status,
                     verdict=verdict, verdict_reason=why, order_date=day)
        got = self.fire("po_due", "po_undated")
        self.assertEqual(set(self.rows("po_due")), {"due"})
        self.assertIn("due today", self.rows("po_due")["due"]["why"])
        self.assertEqual(set(self.rows("po_undated")), {"new"})
        self.assertEqual(self.rows("po_undated")["new"]["since"].date(), dt.date(2026, 10, 1))
        self.assertEqual(got["po_undated"]["open"], 1)
        # the next day the due PO is no longer "due today"
        self.assertEqual(self.fire("po_due", day="2026-11-18")["po_due"]["resolved"], 1)
        self.assertEqual(set(r for r, v in self.rows("po_due").items() if v["state"] == "open"),
                         {"tomorrow"})

    def test_negative_margin_item_lines_priced_one_currency_decided(self):
        def order(h, lines, inv_day=dt.date(2026, 11, 10), src="sage"):
            self.put("sales_orders", h, order_number=h.upper(), customer="C " + h,
                     status="open", of_record=True)
            self.put("sales_invoices", "i" + h, order_number=h.upper(), invoice_date=inv_day,
                     source_systems=src, pilot=False)
            for n, (rev, mar, basis, cur, kind) in enumerate(lines):
                self.put("sales_invoice_lines", f"{h}l{n}", invoice_handle="i" + h,
                         line_type=kind, revenue=Decimal(rev), margin=None if mar is None
                         else Decimal(mar), margin_basis=basis, margin_currency=cur,
                         pilot=False)
        ok = "same_currency"
        order("loss", [("100", "-30", ok, "CAD", "item"), ("50", "10", ok, "CAD", "item"),
                       ("0", "-5", ok, "CAD", "item"),      # free goods left out
                       ("20", "20", ok, "CAD", "misc")])    # freight is not item cost
        order("gain", [("100", "30", ok, "CAD", "item")])
        order("freeonly", [("0", "-5", ok, "CAD", "item")])
        order("unknown", [("100", None, "cost_unknown", "CAD", "item"),
                          ("10", "-50", ok, "CAD", "item")])   # not decided: not judged
        order("twocur", [("100", "-30", ok, "CAD", "item"), ("100", "-30", ok, "USD", "item")])
        order("stale", [("100", "-30", ok, "CAD", "item")], inv_day=dt.date(2026, 9, 1))
        # one order, two invoices: judged on both (loss only over the sum)
        order("split", [("100", "-60", ok, "CAD", "item")])
        self.put("sales_invoices", "isplit2", order_number="SPLIT", invoice_date=dt.date(2026, 11, 12),
                 source_systems="sage", pilot=False)
        self.put("sales_invoice_lines", "split_b", invoice_handle="isplit2", line_type="item",
                 revenue=Decimal(100), margin=Decimal(80), margin_basis=ok,
                 margin_currency="CAD", pilot=False)
        # another system's newest invoice sets its own window
        order("bc_loss", [("100", "-1", ok, "CAD", "item")], inv_day=dt.date(2026, 9, 1), src="bc")
        self.put("sales_invoices", "ibcnew", order_number="OTHER", invoice_date=dt.date(2026, 9, 5),
                 source_systems="bc", pilot=False)
        got = self.fire("negative_margin")
        self.assertEqual(set(self.rows("negative_margin")), {"loss", "bc_loss"})
        self.assertIn("lost 20.00 CAD", self.rows("negative_margin")["loss"]["why"])
        self.assertEqual(got["negative_margin"]["open"], 2)
        self.assertEqual(got["negative_margin"]["skipped"].get("margin_not_decided"), 1)

    def test_eta_slipped_only_a_later_date_on_an_open_po_not_a_bulk_default(self):
        def po(h, status="open", order_day=dt.date(2026, 10, 1)):
            self.put("purchase_orders", h, po_number=h.upper(), supplier="S", status=status,
                     order_date=order_day)
            return self.lineage("purchase_orders", h, ["bc"], [f"PO:{h}"])[0]
        def slip(oid, was, now, when="2026-11-12T10:00:00Z"):
            self.value(oid, "expected_date", was, "2026-10-01T00:00:00Z", when)
            self.value(oid, "expected_date", now, when, None)
        slip(po("later"), "2026-11-20", "2026-11-27")
        slip(po("earlier"), "2026-11-27", "2026-11-20")
        slip(po("closed", status="closed"), "2026-11-20", "2026-11-27")
        slip(po("ownday", order_day=dt.date(2026, 11, 27)), "2026-11-20", "2026-11-27")
        slip(po("old"), "2026-11-20", "2026-11-27", when="2026-10-12T10:00:00Z")
        # a line's later receipt date moves its order
        self.put("purchase_orders", "withline", po_number="WL", supplier="S", status="open",
                 order_date=dt.date(2026, 10, 1))
        self.lineage("purchase_orders", "withline", ["bc"], ["PO:withline"])
        self.put("purchase_order_lines", "wl1", col=7, po_handle="withline")
        line = self.lineage("purchase_order_lines", "wl1", ["bc"], ["POL:wl1"])[0]
        self.value(line, "bc_expected_receipt_date", "2026-11-18", "2026-10-01T00:00:00Z",
                   "2026-11-12T10:00:00Z")
        self.value(line, "bc_expected_receipt_date", "2026-11-25", "2026-11-12T10:00:00Z", None)
        # an object with no current value (re-keyed) is no slip
        self.value(po("gone"), "expected_date", "2026-11-20", "2026-10-01T00:00:00Z",
                   "2026-11-12T10:00:00Z")
        self.conn.execute(f"INSERT INTO {S}.col_classes (id, marks) VALUES (7, %s)",
                          ('{"expected_date": ["finance"]}',))
        self.fire("eta_slipped")
        got = self.rows("eta_slipped")
        self.assertEqual(set(got), {"later", "withline"})
        # the line's own column marking is a need of the card that shows the line's date
        needs = dict(self.conn.execute(f"SELECT handle, col_needs FROM {S}.alerts WHERE "
                                       "rule = 'eta_slipped'").fetchall())
        self.assertEqual((needs["withline"], needs["later"]), ([7], []))
        self.assertIn("7 days", got["later"]["why"])
        self.assertIn("Nov 20, 2026 to Nov 27, 2026", got["later"]["why"])
        # a day most current values share is a default, not a promise
        for n in range(12):
            slip(po(f"bulk{n}"), "2026-12-30", "2026-12-31")
        for n in range(12):
            self.value(f"PO:bulk{n}", "expected_date", "2026-12-31", "x", None)
        self.fire("eta_slipped")
        self.assertFalse([h for h, v in self.rows("eta_slipped").items()
                          if h.startswith("bulk") and v["state"] == "open"])

    def test_vendor_no_reply_waiting_request_without_an_outside_answer(self):
        def thread(h, msgs, status="open"):
            self.put("purchase_orders", h, po_number=h.upper(), supplier="V " + h, status=status)
            for n, (days_ago, who, subject, preview, kw) in enumerate(msgs):
                sender = "us@petrasoap.com" if who == "us" else "v@vendor.example"
                told = ["v@vendor.example"] if who == "us" else ["us@petrasoap.com"]
                oid = f"m-{h}-{n}"
                self._insert(dict(handle=oid, unit=5 + n, ai_ok=True, excluded=False,
                                  mailbox=ME, mailbox_shared=False, sender=sender,
                                  subject=subject, to_addresses=kw.get("told", told),
                                  cc_addresses=[], thread="t-" + h, records=[h],
                                  object_ids=[oid],
                                  received_at=dt.datetime(2026, 11, 17, 12, tzinfo=dt.timezone.utc)
                                  - dt.timedelta(days=days_ago),
                                  sent_at=dt.datetime(2026, 11, 17, 12, tzinfo=dt.timezone.utc)
                                  - dt.timedelta(days=days_ago)))
                self.value(oid, "body_preview", preview, "x")
        ask = "Please confirm the delivery date for this order."
        thread("waits", [(20, "them", "Re: PO", "Shipping soon.", {}), (10, "us", "PO", ask, {})])
        thread("answered", [(10, "us", "PO", ask, {}), (3, "them", "Re: PO", "Friday.", {})])
        thread("fresh", [(3, "us", "PO", ask, {})])
        thread("ancient", [(120, "us", "PO", ask, {})])
        thread("noask", [(10, "us", "PO", "Thanks for the update.", {})])
        thread("closed", [(10, "us", "PO", ask, {})], status="closed")
        thread("internal", [(10, "us", "PO", ask, {"told": ["boss@petrasoap.com"]})])
        self.conn.execute(f"UPDATE {S}.emails SET records = records || ARRAY['a','b','c','d']"
                          " WHERE handle = 'm-noask-0'")
        got = self.fire("vendor_no_reply")
        self.assertEqual(set(self.rows("vendor_no_reply")), {"waits"})
        self.assertIn("no reply for 10 days", self.rows("vendor_no_reply")["waits"]["why"])
        self.assertEqual(got["vendor_no_reply"]["status"], "ok")
        # the unit ids of every message in the thread are the reader's requirement
        units = self.conn.execute(f"SELECT units FROM {S}.alerts WHERE rule = 'vendor_no_reply'"
                                  ).fetchone()[0]
        self.assertEqual(sorted(units), [5, 6])
        # the vendor writes: it resolves
        self._insert(dict(handle="late", unit=9, ai_ok=True, excluded=False, mailbox=ME,
                          sender="v@vendor.example", subject="Re: PO", to_addresses=[ME],
                          cc_addresses=[], thread="t-waits", records=["waits"],
                          object_ids=["late"], received_at=dt.datetime(
                              2026, 11, 16, tzinfo=dt.timezone.utc),
                          sent_at=dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc)))
        self.assertEqual(self.fire("vendor_no_reply")["vendor_no_reply"]["resolved"], 1)

    def test_customer_order_at_risk_firm_chain_from_a_late_po_only(self):
        def link(kind, a, b, tier="verified"):
            self.conn.execute("INSERT INTO ontology.links VALUES (%s, %s, %s, %s, NULL)",
                              (kind, a, b, tier))
        def chain(n, tier="verified", required=dt.date(2026, 11, 10), verdict="late",
                  status="open"):
            po, so = f"po{n}", f"so{n}"
            self.put("purchase_orders", po, po_number=po.upper(), supplier="S", status="open",
                     verdict=verdict, promised_date=dt.date(2026, 11, 25))
            self.lineage("purchase_orders", po, ["bc"], [f"PO:{po}"])
            self.put("sales_orders", so, order_number=so.upper(), customer="C", status=status,
                     required_date=required, of_record=True)
            self.lineage("sales_orders", so, ["sage"], [f"SO:{so}"])
            link("PurchaseOrderLine.line_of", f"L:{n}", f"PO:{po}")
            link("PurchaseOrderLine.for_item", f"L:{n}", f"I:{n}")
            link("AssemblyOrderComponent.uses_item", f"C:{n}", f"I:{n}", tier)
            link("AssemblyOrderComponent.component_of", f"C:{n}", f"AO:{n}")
            link("AssemblyOrderAllocation.allocates", f"AL:{n}", f"AO:{n}")
            link("AssemblyOrderAllocation.for_order_line", f"AL:{n}", f"OL:{n}")
            link("SalesOrderLine.line_of", f"OL:{n}", f"SO:{so}")
        chain(1)                                   # firm chain, late PO: 15 days at risk
        chain(2, tier="lead")                      # a lead link makes no firm chain
        chain(3, required=dt.date(2026, 12, 1))    # required after the material date
        chain(4, verdict="unknown")                # the PO is not late
        chain(5, status="closed")                  # the order is closed
        self.fire("customer_order_at_risk")
        got = self.rows("customer_order_at_risk")
        self.assertEqual(set(got), {"so1"})
        self.assertIn("at least 15 days at risk", got["so1"]["why"])
        # the alert is a row of sales_orders (the page door's own-source check) and records
        # the late PO's lineage objects too (the people exclusion)
        src, objs = self.conn.execute(f"SELECT sources, objects FROM {S}.alerts WHERE rule = "
                                      "'customer_order_at_risk' AND handle = 'so1'").fetchone()
        self.assertEqual(src, ["so1"])
        self.assertTrue({"SO:so1", "PO:po1"} <= set(objs), objs)
        # a second late PO feeding the same order: still one card, the worst days
        self.put("purchase_orders", "po1b", po_number="PO1B", supplier="S", status="open",
                 verdict="late", promised_date=dt.date(2026, 12, 5))
        self.lineage("purchase_orders", "po1b", ["bc"], ["PO:po1b"])
        for kind, a, b in (("PurchaseOrderLine.line_of", "L:1b", "PO:po1b"),
                           ("PurchaseOrderLine.for_item", "L:1b", "I:1")):
            self.conn.execute("INSERT INTO ontology.links VALUES (%s, %s, %s, 'verified', NULL)",
                              (kind, a, b))
        self.fire("customer_order_at_risk")
        got = self.rows("customer_order_at_risk")
        self.assertEqual(len(got), 1)
        self.assertIn("25 days at risk from 2 late purchase orders", got["so1"]["why"])

    def test_cardinality_1_10_60_each_record_once(self):
        for n in (1, 10, 60):
            self.setUp()
            self.slack_history()
            for i in range(n):
                self.po(f"c{n}x{i}", 1 + i % 7)
                self.put("sales_orders", f"o{n}x{i}", order_number=f"O{i}", status="open",
                         days_late=16, required_date=dt.date(2026, 11, 1))
            got = self.fire("po_late", "customer_order_late")
            self.assertEqual((got["po_late"]["open"], got["customer_order_late"]["open"]),
                             (n, n))
            count = self.conn.execute(f"SELECT count(*), count(DISTINCT handle) FROM {S}.alerts"
                                      ).fetchone()
            self.assertEqual(count, (2 * n, 2 * n))

    def test_late_order_only_while_live_and_something_is_outstanding(self):
        """Old Sage headers open for years are not late orders: an order counts only with a
        date or an invoice within 180 days; one every line says is done, or invoiced with
        nothing said outstanding, has shipped. Each left-out order is counted by reason."""
        def order(h, ordered, required, **kw):
            self.put("sales_orders", h, order_number=h.upper(), status="open", of_record=True,
                     order_date=ordered, required_date=required,
                     days_late=(dt.date(2026, 11, 17) - required).days, **kw)

        def line(order_h, n, outstanding, cls=1):
            self.put("sales_order_lines", f"{order_h}-l{n}", cls=cls, order_handle=order_h,
                     order_number=order_h.upper(), outstanding_quantity=outstanding)

        def invoice(order_h, day, cls=1):
            self.put("sales_invoices", f"{order_h}-i{day}", cls=cls, invoice_date=day,
                     order_number=order_h.upper())
        d = dt.date
        self.slack_history()
        order("recent", d(2026, 10, 20), d(2026, 11, 1))                        # late
        order("old", d(2023, 1, 2), d(2023, 2, 1))                               # historic
        order("undated_lines", d(2023, 1, 2), d(2023, 2, 1))                     # historic
        line("undated_lines", 1, 4)
        order("active", d(2026, 3, 1), d(2026, 3, 5))      # its invoice keeps it live
        invoice("active", d(2026, 8, 1), cls=2)
        line("active", 1, 5)
        order("shipped", d(2026, 10, 20), d(2026, 11, 1))
        invoice("shipped", d(2026, 11, 5))
        line("shipped", 1, None)                           # quantity not stated
        order("done", d(2026, 10, 20), d(2026, 11, 1))
        line("done", 1, 0)
        line("done", 2, 0)
        order("partial", d(2026, 10, 20), d(2026, 11, 1))
        invoice("partial", d(2026, 11, 5))
        line("partial", 1, 0)
        line("partial", 2, 3)
        order("future_invoice", d(2023, 1, 2), d(2023, 2, 1))   # a date after the day: none
        invoice("future_invoice", d(2026, 12, 1))
        out = self.fire("customer_order_late")["customer_order_late"]
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(set(self.rows("customer_order_late")), {"recent", "active", "partial"})
        self.assertEqual(out["skipped"], {"historic": 3, "invoiced": 1,
                                          "nothing_outstanding": 1})
        stored = self.conn.execute(f"SELECT skipped FROM {S}.alert_runs WHERE rule = "
                                   "'customer_order_late'").fetchone()[0]
        self.assertEqual(stored, out["skipped"])
        # the invoice that keeps "active" live is class 2: a reader without it never sees
        # the alert (its verdict rests on that row); every other one needs class 1 only
        late = {"customer_order_late"}
        self.assertEqual({h for r, h in self.read([1]) if r in late}, {"recent", "partial"})
        self.assertEqual({h for r, h in self.read([1, 2]) if r in late},
                         {"recent", "active", "partial"})
        self.assertEqual(self.read([2]), set())

    def lineage(self, view, handle, systems, objs=None):
        objs = objs or [f"{handle}:{s}" for s in systems]
        self.conn.execute(f"INSERT INTO {S}.lineage (view, handle, object_ids, systems, copies) "
                          "VALUES (%s, %s, %s, %s, %s)", (view, handle, objs, systems, len(objs)))
        return objs

    def value(self, oid, prop, value, recorded, superseded=None):
        self.conn.execute("INSERT INTO ontology.object_values VALUES (%s, %s, %s, %s, %s)",
                          (oid, prop, value, recorded, superseded))

    def test_copy_only_orders_portal_judged_filemaker_only_while_edited(self):
        """No Sage copy: a Portal-only order is judged like one of record (the Portal is
        becoming the system of record); a FileMaker-only one only while FileMaker still
        edits it; any other copy-only order (BC pilot) never. Each left-out one counted."""
        d = dt.date

        def order(h, systems, of_record=False, required=d(2026, 11, 1)):
            self.put("sales_orders", h, order_number=None if "filemaker" in systems else
                     h.upper(), status="open", of_record=of_record, customer="C " + h,
                     order_date=None, required_date=required,
                     days_late=(d(2026, 11, 17) - required).days)
            return self.lineage("sales_orders", h, systems)
        self.slack_history()
        order("sage", ["sage", "portal"], of_record=True)
        order("portal_only", ["portal"])
        order("portal_old", ["portal"], required=d(2025, 1, 2))           # historic
        fm = order("fm_edited", ["filemaker"])
        self.value(fm[0], "fm_modified_at", "2026-10-02T10:00:00.000000Z", "2026-10-28")
        fm = order("fm_stale", ["filemaker"])
        self.value(fm[0], "fm_modified_at", "2025-01-02T10:00:00.000000Z", "2026-10-28")
        order("fm_never", ["filemaker"])                                  # no edit stamp
        order("bc_pilot", ["bc"])
        order("no_lineage_copy", [])                                      # not judged
        out = self.fire("customer_order_late")["customer_order_late"]
        self.assertEqual(out["status"], "ok", out)
        rows = self.rows("customer_order_late")
        self.assertEqual(set(rows), {"sage", "portal_only", "fm_edited"})
        self.assertIn("Order with no order number for C fm_edited", rows["fm_edited"]["why"])
        self.assertEqual(out["skipped"], {"historic": 1, "retiring_source_untouched": 2})
        # the edit stamp is read from the CURRENT value only
        self.conn.execute("UPDATE ontology.object_values SET superseded_at = '2026-11-01'")
        self.fire("customer_order_late")
        self.assertEqual({h for h, r in self.rows("customer_order_late").items()
                          if r["state"] == "open"}, {"sage", "portal_only"})

    def test_late_only_beyond_the_customers_usual_slack(self):
        """A day past the required date is normal: an order
        fires only when later than 75% of its customer's dated orders were at their first
        invoice (10 or more of them; else every customer's), never under 3 days. With no
        history at all nothing is judged (counted). A verdict on the customer's own history
        needs every history row's class."""
        d = dt.date

        def order(h, cust, late):
            req = d(2026, 11, 17) - dt.timedelta(days=late)
            self.put("sales_orders", h, order_number=h.upper(), status="open", of_record=True,
                     customer_handle=cust, customer="C " + cust, order_date=req,
                     required_date=req, days_late=late)
        order("lone", "new", 9)
        out = self.fire("customer_order_late")["customer_order_late"]
        self.assertEqual((out["open"], out["skipped"]), (0, {"no_slack_history": 1}))
        self.slack_history("slow", slip=20, inv_cls=3)     # its own: usually 20 days late
        self.slack_history("hist", slip=1)
        order("slow_ok", "slow", 15)          # within its own usual 20 days
        order("slow_late", "slow", 25)
        order("min_floor", "new", 3)          # every customer's 75th = 20 (10 of 20), floor 3
        order("new_late", "new", 21)
        order("tiny_ok", "hist", 3)           # its own usual 1 day: the 3-day floor holds
        order("tiny_late", "hist", 4)
        out = self.fire("customer_order_late")["customer_order_late"]
        self.assertEqual(out["status"], "ok", out)
        rows = self.rows("customer_order_late")
        self.assertEqual(set(rows), {"slow_late", "new_late", "tiny_late"})
        self.assertEqual(out["skipped"], {"within_usual_slack": 4})
        self.assertIn("this customer's orders usually ship within 20 days of it",
                      rows["slow_late"]["why"])
        self.assertIn("orders usually ship within 20 days of it (75% of 20 dated orders",
                      rows["new_late"]["why"])
        # the late rule's day is the end of the allowed slack, not the required date
        self.assertEqual(rows["tiny_late"]["since"].date(), d(2026, 11, 17))
        late = {"customer_order_late"}
        self.assertEqual({h for r, h in self.read([1]) if r in late},
                         {"new_late", "tiny_late"})
        self.assertEqual({h for r, h in self.read([1, 3]) if r in late},
                         {"slow_late", "new_late", "tiny_late"})

    def test_a_filemaker_copy_of_an_order_of_record_does_not_fire(self):
        """A FileMaker-only order with an order of record for the same customer dated within
        7 days of its creation (or required date) and the same line quantities is that
        order's copy: not judged, counted. Other
        quantities, or an unknown one, or 8 days apart: judged."""
        d = dt.date
        self.slack_history()

        def fm(h, created, qtys, cust="cu"):
            self.put("sales_orders", h, order_number=None, status="open", of_record=False,
                     customer_handle=cust, customer="C " + cust, order_date=None,
                     required_date=d(2026, 11, 1), days_late=16)
            objs = self.lineage("sales_orders", h, ["filemaker"])
            self.value(objs[0], "fm_modified_at", "2026-10-30T10:00:00Z", "2026-10-30")
            self.value(objs[0], "fm_created_at", created + "T09:00:00Z", "2026-10-30")
            for i, q in enumerate(qtys):
                self.put("sales_order_lines", f"{h}-l{i}", order_handle=h,
                         ordered_quantity=None if q is None else Decimal(q))

        def sage(h, day, qtys, cust="cu"):
            self.put("sales_orders", h, order_number=h.upper(), status="closed",
                     of_record=True, customer_handle=cust, order_date=day)
            for i, q in enumerate(qtys):
                self.put("sales_order_lines", f"{h}-l{i}", order_handle=h,
                         ordered_quantity=Decimal(q))
        sage("s1", d(2026, 10, 5), ["500", "1000"])
        fm("twin", "2026-10-01", ["1000", "500"])          # same lines, 4 days apart
        fm("other_qty", "2026-10-01", ["1000", "400"])
        fm("unknown_qty", "2026-10-01", ["1000", None])
        fm("far", "2026-09-27", ["500", "1000"])           # 8 days apart
        fm("other_cust", "2026-10-01", ["500", "1000"], cust="cu2")
        out = self.fire("customer_order_late")["customer_order_late"]
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(set(self.rows("customer_order_late")),
                         {"other_qty", "unknown_qty", "far", "other_cust"})
        self.assertEqual(out["skipped"], {"copy_of_order_of_record": 1})

    def test_open_longer_than_usual_without_a_promised_date(self):
        """An open order with no promised date is past the customer's own 90th-percentile
        order-to-first-invoice time (10+ closed orders), else every customer's; the
        customer's history rows are among the alert's needs."""
        d = dt.date

        def closed(h, cust, ordered, days, cls=1):
            self.put("sales_orders", h, cls=cls, order_number=h.upper(), status="closed",
                     of_record=True, customer_handle=cust, order_date=ordered)
            self.put("sales_invoices", h + "-i", cls=cls, order_number=h.upper(),
                     invoice_date=ordered + dt.timedelta(days=days))
        for i in range(10):     # "fast": its orders ship within 1..10 days (p90 = 9)
            closed(f"f{i}", "fast", d(2026, 6, 1) + dt.timedelta(days=i), 1 + i, cls=3)
        for i in range(3):      # "few": too few of its own: every customer's p90 (here 30)
            closed(f"w{i}", "few", d(2026, 6, 1), 30)

        def open_(h, cust, age, **kw):
            values = dict(order_number=h.upper(), status="open", of_record=True,
                          customer_handle=cust, customer="C " + cust, required_date=None,
                          order_date=d(2026, 11, 17) - dt.timedelta(days=age))
            values.update(kw)
            self.put("sales_orders", h, **values)
        open_("fast_long", "fast", 12)          # 12 > 9: fires
        open_("fast_ok", "fast", 8)             # within its usual time
        open_("few_long", "few", 31)            # 31 > 30 (all customers' p90)
        open_("few_ok", "few", 20)
        open_("dated", "fast", 40, required_date=d(2026, 12, 1))   # has a promise: not this rule
        open_("undated_day", "fast", 0, order_date=None)            # cannot be aged: counted
        out = self.fire("customer_order_open_long")["customer_order_open_long"]
        self.assertEqual(out["status"], "ok", out)
        rows = self.rows("customer_order_open_long")
        self.assertEqual(set(rows), {"fast_long", "few_long"})
        self.assertIn("this customer's orders usually ship within 9 days (90% of 10 of its "
                      "orders", rows["fast_long"]["why"])
        self.assertIn("orders usually ship within 30 days (90% of 13 orders",
                      rows["few_long"]["why"])
        self.assertEqual(rows["fast_long"]["since"].date(), d(2026, 11, 15))
        self.assertEqual(out["skipped"], {"no_order_date": 1})
        # the customer's own history (class 3) is needed to see its alert
        late = {"customer_order_open_long"}
        self.assertEqual({h for r, h in self.read([1]) if r in late}, {"few_long"})
        self.assertEqual({h for r, h in self.read([1, 3]) if r in late},
                         {"fast_long", "few_long"})

    def test_parity_matches_old_gaps_to_rows_by_lineage_read_only(self):
        import sqlite3
        import tempfile
        self.po("p1", 3)
        self.po("p2", 2)
        self.conn.execute(f"DELETE FROM {S}.lineage")
        for h, objs in (("p1", ["o1", "o1b"]), ("p2", ["o2"]), ("p3", ["o3"])):
            self.conn.execute(f"INSERT INTO {S}.lineage (view, handle, object_ids) VALUES "
                              "('purchase_orders', %s, %s)", (h, objs))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.sqlite"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE gaps (kind text, object_id text, state text)")
            db.executemany("INSERT INTO gaps VALUES (?, ?, ?)", [
                ("late_po", "o1b", "open"), ("late_po", "o3", "open"),
                ("late_po", "o2", "resolved"), ("late_po", "gone", "open"),
                ("lot_on_hold", "o2", "open")])
            db.commit()
            db.close()
            before = path.read_bytes()
            got = al.parity(self.conn, path, ["po_late"], schema=S, day=DAY)["po_late"]
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual({k: got[k] for k in ("new", "old_open", "old_in_model",
                                              "old_not_in_model", "both", "new_only",
                                              "old_only")},
                         {"new": 2, "old_open": 3, "old_in_model": 2, "old_not_in_model": 1,
                          "both": 1, "new_only": 1, "old_only": 1})
        self.assertEqual((got["new_only_handles"], got["old_only_handles"]), (["p2"], ["p3"]))
        # read only: no alert written, no run recorded
        self.assertIsNone(self.conn.execute(f"SELECT to_regclass('{S}.alerts')").fetchone()[0])
        with self.assertRaises(ValueError):
            al.parity(self.conn, path, ["needs_my_reply"], schema=S, day=DAY)

    def test_the_migration_is_the_runners_ddl_and_a_run_works_on_it(self):
        sql = "".join((Path(al.__file__).parent / "migrations" / name).read_text()
                      for name in ("0044_alerts.sql", "0045_alerts_read_time.sql"))
        body = "\n".join(ln for ln in sql.splitlines() if not ln.startswith("--"))
        self.assertEqual([st.strip().rstrip(";") for st in body.split(";\n") if st.strip()],
                         [st.strip() for st in al._ddl("biz")])
        self.conn.execute(re.sub(r"\bbiz\b", S, body))
        self.po("p1", 3)
        self.assertEqual(self.fire("po_late")["po_late"]["open"], 1)

    # ------------------------------------------------------------------ fail closed
    def test_unknown_rule_missing_table_and_failing_rule_change_nothing(self):
        with self.assertRaises(ValueError):
            al.run(self.dsn, ["po_late", "no_such_rule"], schema=S, day=DAY)
        with self.assertRaises(ValueError):
            al.run(self.dsn, None, schema="biz; drop", day=DAY)
        with self.assertRaises(ValueError):
            al.read(self.conn, classes=None, schema=S)
        self.po("p1", 3)
        self.fire("po_late")
        self.conn.execute(f"DROP TABLE {S}.purchase_orders")
        out = self.fire("po_late")["po_late"]
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(self.rows("po_late")["p1"]["state"], "open")
        cols = ", ".join(f'"{n}" {k}' for n, k in bv.columns(bv.VIEWS["purchase_orders"])
                         if n != "verdict")
        self.conn.execute(f"CREATE TABLE {S}.purchase_orders ({cols})")
        out = self.fire("po_late")["po_late"]
        self.assertEqual(out["status"], "failed")
        self.assertEqual(self.rows("po_late")["p1"]["state"], "open")
        status = self.conn.execute(f"SELECT status FROM {S}.alert_runs WHERE rule = 'po_late'"
                                   ).fetchone()[0]
        self.assertEqual(status, "failed")

    def test_stale_after_two_cycles_of_the_timer_that_starts_the_runner(self):
        units = Path(al.__file__).resolve().parents[2] / "deploy" / "systemd"
        timer = (units / "plx-business-views.timer").read_text()
        every = re.search(r"^OnCalendar=\*:\d+/(\d+)$", timer, re.M)
        self.assertIsNotNone(every, timer)
        self.assertEqual(al.REFRESH_MINUTES, int(every.group(1)))
        self.assertEqual(al.STALE_MINUTES, 2 * al.REFRESH_MINUTES)
        # 11-18: a views rebuild ran 80 min (and one was OOM-killed), so a runner started
        # only after a refresh left every alert "unavailable"; its own timer runs it at
        # least twice within the stale window, whatever the views do
        own = (units / "plx-alerts.timer").read_text()
        runs = re.search(r"^OnCalendar=\*:\d+/(\d+)$", own, re.M)
        self.assertIsNotNone(runs, own)
        self.assertLessEqual(2 * int(runs.group(1)), al.STALE_MINUTES)

    def test_one_run_at_a_time(self):
        """The timer's run and the one after a refresh never overlap: while one holds the
        run lock a second leaves at once and changes nothing."""
        import psycopg
        self.po("p1", 3)
        with psycopg.connect(self.dsn, autocommit=True) as other:
            other.execute("SELECT pg_advisory_lock(%s)", (al.RUN_LOCK,))
            self.assertEqual(al.run(self.dsn, ["po_late"], schema=S, day=DAY, owners=OWNERS),
                             [])
            self.assertIsNone(self.conn.execute("SELECT to_regclass(%s)",
                                                (f"{S}.alert_runs",)).fetchone()[0])
        self.assertEqual(self.fire("po_late")["po_late"]["open"], 1)

    def test_every_rule_is_a_declared_alert_type(self):
        kinds = json.loads(al.KINDS_FILE.read_text(encoding="utf-8"))
        types = {t["type"]: t["kind"] for t in kinds["types"]}
        for rule in al.RULES.values():
            self.assertEqual(types.get(rule.name), rule.kind, rule.name)
            for table in rule.reads:
                self.assertIn(table, bv.VIEWS, rule.name)
            self.assertIn(rule.cols_of or rule.table, rule.reads, rule.name)
            for col in rule.shows:
                self.assertIn(col, {c.name for c in bv.VIEWS[rule.cols_of or rule.table].cols},
                              rule.name)
            for table, col in rule.also_shows:
                self.assertIn(table, rule.reads, rule.name)
                self.assertIn(col, {c.name for c in bv.VIEWS[table].cols}, rule.name)

    # ------------------------------------------------------------------ restricted readers
    def test_restricted_reader_sees_only_what_every_source_row_allows(self):
        self.po("p1", 3, cls=1)
        self.po("p2", 3, cls=2, ai=False)
        self.put("customers", "cu1", cls=1, name="Cust One")
        self.price_series()
        self.line("cu1", "l1b", "9", "10", dt.date(2026, 11, 2), cls=2)   # one line of class 2
        self.fire()
        self.assertEqual(self.read([1]), {("po_late", "p1")})
        self.assertEqual(self.read([1, 2]) >= {("po_late", "p1"), ("po_late", "p2"),
                                               ("price_below_cost", "cu1")}, True)
        self.assertNotIn(("po_late", "p2"), self.read([1, 2], ai=True))
        self.assertEqual(self.read([2]), {("po_late", "p2")})
        self.assertEqual(self.read([]), set())
        # the sentence shows a finance-marked cell: only a reader the door lets read that
        # column class sees it (an allow-list: none held, not seen)
        self.conn.execute(f"INSERT INTO {S}.col_classes (id, marks) VALUES (5, %s)",
                          (json.dumps({"revenue": ["finance"]}),))
        self.conn.execute(f"INSERT INTO {S}.col_classes (id, marks) VALUES (6, %s)",
                          (json.dumps({"description": ["finance"]}),))
        self.conn.execute(f"UPDATE {S}.sales_invoice_lines SET col_class = 5")
        self.fire("price_below_cost")
        self.assertNotIn(("price_below_cost", "cu1"), self.read([1, 2]))
        self.assertNotIn(("price_below_cost", "cu1"), self.read([1, 2], col_classes=[6]))
        self.assertIn(("price_below_cost", "cu1"), self.read([1, 2], col_classes=[5]))
        # a class marking a column the sentence does not show needs nothing
        self.conn.execute(f"UPDATE {S}.sales_invoice_lines SET col_class = 6")
        self.fire("price_below_cost")
        self.assertIn(("price_below_cost", "cu1"), self.read([1, 2]))

    # ------------------------------------------------------------------ each rule fires
    def price_series(self, cust_cls=1, other_cls=1, price="9", cost="10", others=4):
        day = dt.date(2026, 11, 1)
        self.line("cu1", "l1", price, cost, day, cust_cls)
        for i in range(others):
            h = f"oc{i}"
            self.put("customers", h, cls=other_cls, name=f"Other {i}")
            self.line(h, f"lo{i}", "20", cost, day, other_cls)

    def line(self, cust, handle, price, cost, day, cls=1, item="IT1", qty="10"):
        q, p, c = Decimal(qty), Decimal(price), Decimal(cost)
        self.put("sales_invoice_lines", handle, cls=cls, customer_handle=cust, customer=cust,
                 item_code=item, item_name="Item " + item, quantity=q, quantity_unit="EA",
                 revenue=q * p, revenue_currency="CAD", cost=q * c, cost_currency="CAD",
                 margin=q * (p - c), margin_currency="CAD", margin_basis="same_currency",
                 invoice_date=day, pilot=False)

    def test_price_rules_fire_and_below_cost_wins(self):
        self.put("customers", "cu1", name="Cust One")
        self.price_series(price="12", cost="5")
        for i, item in enumerate(("IT2", "IT3")):      # the customer's standing: 2 more items
            self.line("cu1", f"s{i}", "10", "1", dt.date(2026, 11, 1), item=item)
        got = self.fire("price_below_typical", "price_below_cost")
        self.assertEqual((got["price_below_typical"]["open"], got["price_below_cost"]["open"]),
                         (1, 0))
        self.assertEqual(self.rows("price_below_typical")["cu1"]["topic"], "IT1|EA|CAD")
        self.update("sales_invoice_lines", "l1", cost=Decimal(150), margin=Decimal(-30))
        got = self.fire("price_below_typical", "price_below_cost")
        self.assertEqual((got["price_below_typical"]["resolved"],
                          got["price_below_cost"]["opened"]), (1, 1))

    def test_customer_price_drop(self):
        self.put("customers", "cu1", name="Cust One")
        self.line("cu1", "now", "8", "1", dt.date(2026, 11, 1))
        self.line("cu1", "was", "10", "1", dt.date(2026, 3, 1))
        self.lineage("sales_invoice_lines", "now", ["sage"], ["obj-now"])
        self.lineage("sales_invoice_lines", "was", ["sage"], ["obj-was"])
        self.lineage("customers", "cu1", ["sage"], ["obj-cu1"])
        self.conn.execute(f"UPDATE {S}.lineage SET read_ids = '{{obj-read}}', col_objects = "
                          "'{\"customer\": [\"obj-col\"]}' WHERE handle = 'was'")
        self.assertEqual(self.fire("customer_price_drop")["customer_price_drop"]["open"], 1)
        # what the page view re-checks at read time: both windows' lines (the sentence's
        # price rests on each) and every object their and the record's lineage names
        got = self.conn.execute(f"SELECT source_table, sources, objects, record_shows, shows "
                                f"FROM {S}.alerts WHERE rule = 'customer_price_drop'").fetchone()
        self.assertEqual((got[0], sorted(got[1]), sorted(got[2]), got[3]),
                         ("sales_invoice_lines", ["now", "was"],
                          ["obj-col", "obj-cu1", "obj-now", "obj-read", "obj-was"], []))
        self.assertIn("customer", got[4])
        self.update("sales_invoice_lines", "now", revenue=Decimal(95))
        self.assertEqual(self.fire("customer_price_drop")["customer_price_drop"]["resolved"], 1)

    def test_lot_on_hold_made_or_not_the_state_default(self):
        self.put("batches", "made", batch_code="B1", status="Realized", quality_status="On-Hold",
                 realized_date=dt.date(2026, 11, 2))
        self.put("batches", "gone", batch_code="B2", status="Cancelled", quality_status="On-Hold")
        # made years ago and never released in its old system: history, not news; a year
        # to the day is still listed
        self.put("batches", "old", batch_code="B3", status="Realized", quality_status="On-Hold",
                 realized_date=dt.date(2019, 4, 26))
        self.put("batches", "year", batch_code="B4", status="Realized", quality_status="On-Hold",
                 realized_date=dt.date(2025, 11, 17))
        for i in range(12):   # every scheduled batch carries the hold before release
            self.put("batches", f"s{i}", batch_code=f"S{i}", status="Scheduled",
                     quality_status="On Hold")
        self.fire("lot_on_hold")
        self.assertEqual(set(self.rows("lot_on_hold")), {"made", "year"})
        self.assertEqual(self.rows("lot_on_hold")["made"]["since"].date(), dt.date(2026, 11, 2))
        self.update("batches", "made", quality_status="Released")
        self.assertEqual(self.fire("lot_on_hold")["lot_on_hold"]["resolved"], 1)
        # a day later the year-old batch ages out: it resolves without a release
        self.assertEqual(self.fire("lot_on_hold", day="2026-11-18")["lot_on_hold"]["resolved"], 1)

    def test_lot_on_hold_reads_the_newest_copy(self):
        """Copies disagreeing on the hold: the one recorded last wins (a tie keeps the
        hold); the state's default is still read from the ranked copy."""
        def batch(h, status, ranked, copies):
            self.put("batches", h, batch_code=h.upper(), status=status, quality_status=ranked,
                     realized_date=dt.date(2026, 11, 2))
            objs = self.lineage("batches", h, ["portal", "filemaker"][:len(copies)])
            for oid, (said, at) in zip(objs, copies):
                self.value(oid, "quality_status", said, at)
        batch("released_later", "Realized", "On-Hold",
              [("On-Hold", "2026-11-06T21:34:23Z"), ("Released", "2026-11-17T15:55:03Z")])
        batch("held_later", "Realized", "Released",
              [("Released", "2026-11-06T21:34:23Z"), ("On-Hold", "2026-11-17T15:55:03Z")])
        batch("tie", "Realized", "Released",
              [("Released", "2026-11-06T21:34:23Z"), ("On-Hold", "2026-11-06T21:34:23Z")])
        batch("one_copy", "Realized", "On-Hold", [("On-Hold", "2026-11-01T00:00:00Z")])
        # every scheduled batch's ranked copy holds it (the default); one copy releasing
        # some of them makes none of the rest an alert
        for i in range(12):
            batch(f"s{i}", "Scheduled", "On Hold",
                  [("On Hold", "2026-11-01T00:00:00Z")] + (
                      [("Released", "2026-11-02T00:00:00Z")] if i < 6 else []))
        self.assertEqual(self.fire("lot_on_hold")["lot_on_hold"]["status"], "ok")
        self.assertEqual(set(self.rows("lot_on_hold")), {"held_later", "tie", "one_copy"})

    def test_customer_demand_drop_not_judged_on_unknown_value_status_or_currency(self):
        self.put("customers", "big", name="Big Co")
        base = dt.date(2026, 11, 17) - dt.timedelta(days=200)
        for i in range(6):
            self.put("sales_orders", f"b{i}", customer_handle="big", status="closed",
                     order_type="order", value=Decimal(1000), value_currency="CAD",
                     order_date=base + dt.timedelta(days=i))
        self.put("sales_orders", "now", customer_handle="big", status="closed",
                 value=Decimal(1), value_currency="CAD", order_date=dt.date(2026, 11, 10))
        self.assertEqual(self.fire("customer_demand_drop")["customer_demand_drop"]["open"], 1)
        # a recent order of no known value: its sum would be short (a drop that is not one)
        self.put("sales_orders", "unvalued", customer_handle="big", status="open",
                 value=None, value_currency=None, order_date=dt.date(2026, 11, 12))
        out = self.fire("customer_demand_drop")["customer_demand_drop"]
        self.assertEqual((out["open"], out["resolved"], out["skipped"]),
                         (0, 1, {"order_value_unknown": 1}))
        self.update("sales_orders", "unvalued", status="unknown")
        self.assertEqual(self.fire("customer_demand_drop")["customer_demand_drop"]["skipped"],
                         {"order_status_unknown": 1})
        self.update("sales_orders", "unvalued", status="closed", value=Decimal(1),
                    value_currency="USD")
        self.assertEqual(self.fire("customer_demand_drop")["customer_demand_drop"]["skipped"],
                         {"currencies_mixed": 1})

    def test_customer_demand_drop_stale_open_and_zero_valued_orders(self):
        """A stale open order (open, nothing dated or invoiced in 180 days) is no demand: it
        never swells the usual level. An order of value 0
        is of unknown value (a header zeroed once invoiced): its customer is not judged."""
        self.put("customers", "big", name="Big Co")
        base = dt.date(2026, 11, 17) - dt.timedelta(days=300)
        for i in range(4):
            self.put("sales_orders", f"b{i}", customer_handle="big", status="closed",
                     order_type="order", value=Decimal(100), value_currency="CAD",
                     order_date=base + dt.timedelta(days=i))
        self.put("sales_orders", "now", customer_handle="big", status="closed",
                 value=Decimal(90), value_currency="CAD", order_date=dt.date(2026, 11, 10))
        self.assertEqual(self.fire("customer_demand_drop")["customer_demand_drop"]["open"], 0)
        # two old open headers, never invoiced: without the rule they make it a "drop"
        for i in range(2):
            self.put("sales_orders", f"stale{i}", customer_handle="big", status="open",
                     value=Decimal(5000), value_currency="CAD",
                     order_date=base + dt.timedelta(days=10))
        out = self.fire("customer_demand_drop")["customer_demand_drop"]
        self.assertEqual((out["open"], out["skipped"]), (0, {"stale_open_order": 2}))
        # invoiced recently, the same open order is live demand again
        self.update("sales_orders", "stale0", order_number="ST0")
        self.put("sales_invoices", "inv-s0", order_number="ST0",
                 invoice_date=dt.date(2026, 10, 1))
        out = self.fire("customer_demand_drop")["customer_demand_drop"]
        self.assertEqual((out["open"], out["skipped"]), (1, {"stale_open_order": 1}))
        self.update("sales_orders", "now", value=Decimal(0))
        out = self.fire("customer_demand_drop")["customer_demand_drop"]
        self.assertEqual((out["open"], out["resolved"], out["skipped"]),
                         (0, 1, {"order_value_unknown": 1, "stale_open_order": 1}))

    def test_customer_demand_drop(self):
        self.put("customers", "big", name="Big Co")
        self.put("customers", "small", name="Small Co")
        base = dt.date(2026, 11, 17) - dt.timedelta(days=200)
        for i in range(6):
            self.put("sales_orders", f"b{i}", customer_handle="big", status="closed",
                     order_type="order", value=Decimal(1000), value_currency="CAD",
                     order_date=base + dt.timedelta(days=i))
        self.put("sales_orders", "s0", customer_handle="small", status="closed",
                 value=Decimal(10), value_currency="CAD", order_date=base)
        self.put("sales_orders", "recent", customer_handle="small", status="open",
                 value=Decimal(10), value_currency="CAD", order_date=dt.date(2026, 11, 10))
        got = self.fire("customer_demand_drop")["customer_demand_drop"]
        self.assertEqual(got["open"], 1)
        self.assertEqual(set(self.rows("customer_demand_drop")), {"big"})
        got = self.conn.execute(f"SELECT source_table, sources, record_shows FROM {S}.alerts "
                                "WHERE rule = 'customer_demand_drop'").fetchone()
        self.assertEqual((got[0], sorted(got[1]), got[2]),
                         ("sales_orders", [f"b{i}" for i in range(6)], ["name"]))
        self.put("sales_orders", "b-back", customer_handle="big", status="open",
                 value=Decimal(5000), value_currency="CAD", order_date=dt.date(2026, 11, 1))
        self.assertEqual(self.fire("customer_demand_drop")["customer_demand_drop"]["resolved"], 1)

    # ------------------------------------------------------------------ needs_my_reply
    # who waits on whom is decided by the emails build's flags (bulk, from the owner, the
    # owner's To/Cc role, the owner's last write in the thread); the rule only combines them

    def test_fires_only_for_a_person_waiting_on_the_owner(self):
        self.mail("m_ok")
        self.mail("m_bulk", is_bulk=True)
        self.mail("m_bulk_unknown", is_bulk=None)
        self.mail("m_cc", owner_role="cc")
        self.mail("m_role_unknown", owner_role=None)
        self.mail("m_from_me", from_owner=True)
        self.mail("m_shared", mailbox_shared=True)
        self.mail("m_excluded", excluded=True)
        self.mail("m_no_sender", sender="")
        self.mail("m_list", to_addresses=[ME] + [f"p{i}@ours.example" for i in range(5)])
        self.mail("m_old", hours_ago=24 * 31)
        self.mail("m_future", hours_ago=-48)
        self.mail("m_answered", owner_replied_at=dt.datetime(2026, 11, 17, 11,
                                                              tzinfo=dt.timezone.utc))
        out = self.fire("needs_my_reply")["needs_my_reply"]
        self.assertEqual(out["status"], "ok")
        self.assertEqual(set(self.rows("needs_my_reply")), {"m_ok"})
        row = self.rows("needs_my_reply")["m_ok"]
        self.assertIn('bo@client.example wrote "Quote for May" on Nov 17', row["why"])
        self.assertEqual(row["since"], dt.datetime(2026, 11, 17, 7, tzinfo=dt.timezone.utc))

    def test_only_a_correspondent_or_a_thread_the_owner_wrote_in(self):
        self.mail("m_known")                                   # written to before (To)
        self.mail("m_cold", known=False, sender="sales@cold.example")
        self.mail("m_cc_known", known=False, sender="cc@client.example")
        self.sent("s_cc", to=["x@client.example"], cc=["cc@client.example"], days_ago=9)
        self.mail("m_late", known=False, sender="late@client.example")
        self.sent("s_late", to=["late@client.example"], days_ago=0.01)   # only after it came
        self.mail("m_other_box", known=False, sender="ob@client.example")
        self.sent("s_other_box", to=["ob@client.example"], mailbox="cy@ours.example",
                  days_ago=9)
        self.sent("s_thread", to=["team@client.example"], thread="t-m_thread", days_ago=2)
        self.mail("m_thread", known=False, sender="new@client.example", thread="t-m_thread")
        self.fire("needs_my_reply")
        self.assertEqual(set(self.rows("needs_my_reply")),
                         {"m_known", "m_cc_known", "m_thread"})

    def test_reply_eval_scores_the_rule_against_what_the_owner_did(self):
        from operating.pg import reply_eval
        H = 24
        self.mail("a", hours_ago=10 * H, thread="A")                  # answered next day
        self.sent("a_re", to=["bo@client.example"], thread="A", days_ago=9)
        self.mail("b", hours_ago=10 * H, thread="B", known=False,      # cold, answered
                  sender="new@cold.example")
        self.sent("b_re", to=["new@cold.example"], thread="B", days_ago=9)
        self.mail("c", hours_ago=12 * H, thread="C")                  # never answered
        self.mail("d", hours_ago=20 * H, thread="D")                  # answered 10 days on
        self.mail("d2", hours_ago=19 * H, thread="D")
        self.sent("d_re", to=["bo@client.example"], thread="D", days_ago=10)
        self.mail("e", hours_ago=5 * H, thread="E", is_bulk=True)     # bulk, never
        self.mail("f", hours_ago=60 * H, thread="F")                  # before the range
        self.sent("f_re", to=["bo@client.example"], thread="F", days_ago=59)
        got = reply_eval.measure(self.conn, ME.upper(), dt.date(2026, 10, 1),
                                 dt.date(2026, 11, 17), owners=OWNERS, schema=S)
        self.assertEqual({k: got[k] for k in ("answered", "caught", "flagged",
                                              "flagged_answered", "flagged_later",
                                              "flagged_never", "miss_by_test")},
                         {"answered": 3, "caught": 2, "flagged": 3, "flagged_answered": 1,
                          "flagged_later": 1, "flagged_never": 1,
                          "miss_by_test": {"known to you": 1}}, got)
        self.assertEqual((got["recall"], got["flagged_never_pct"]), (66.7, 33.3))
        self.assertEqual(got["miss_domains"], [{"d": "cold.example", "f": "known to you", "n": 1}])
        with self.assertRaises(ValueError):
            reply_eval.measure(self.conn, ME, dt.date(2026, 11, 2), dt.date(2026, 11, 1),
                               owners=OWNERS, schema=S)
        with self.assertRaises(ValueError):
            reply_eval.measure(self.conn, ME, dt.date(2026, 10, 1), dt.date(2026, 11, 17),
                               owners=OWNERS, schema="biz; DROP")

    def test_one_alert_per_thread_keeps_since_and_resolves_on_reply(self):
        self.mail("a1", hours_ago=30, thread="t1")
        self.mail("a2", hours_ago=3, thread="t1")
        self.mail("b1", hours_ago=2, thread="t2", mailbox="cy@ours.example")
        self.fire("needs_my_reply")
        rows = self.rows("needs_my_reply")
        self.assertEqual(set(rows), {"a1", "b1"})
        self.assertIn("and 1 more since", rows["a1"]["why"])
        # the owner wrote in t1 after a1 but before a2: a2 is now the one waiting
        self.conn.execute(f"UPDATE {S}.emails SET owner_replied_at = %s WHERE thread = 't1'",
                          (dt.datetime(2026, 11, 17, 6, tzinfo=dt.timezone.utc),))
        out = self.fire("needs_my_reply")["needs_my_reply"]
        rows = self.rows("needs_my_reply")
        self.assertEqual((rows["a1"]["state"], rows["a2"]["state"]), ("resolved", "open"))
        self.assertEqual((out["opened"], out["resolved"]), (1, 1))
        self.conn.execute(f"UPDATE {S}.emails SET owner_replied_at = %s WHERE thread = 't1'",
                          (dt.datetime(2026, 11, 17, 11, 30, tzinfo=dt.timezone.utc),))
        self.fire("needs_my_reply")
        rows = self.rows("needs_my_reply")
        self.assertEqual((rows["a2"]["state"], rows["b1"]["state"]), ("resolved", "open"))

    def test_cardinality_1_10_60_threads(self):
        for n in (1, 10, 60):
            self.setUp()
            for i in range(n):
                self.mail(f"x{n}_{i}a", hours_ago=10 + i, thread=f"th{i}")
                self.mail(f"x{n}_{i}b", hours_ago=1 + i % 9, thread=f"th{i}")
            out = self.fire("needs_my_reply")["needs_my_reply"]
            self.assertEqual(out["open"], n)
            got = self.conn.execute(f"SELECT count(*), count(DISTINCT handle) FROM {S}.alerts"
                                    ).fetchone()
            self.assertEqual(got, (n, n))

    def test_flags_missing_from_the_build_fail_and_change_nothing(self):
        self.mail("m1")
        self.fire("needs_my_reply")
        # the owners' addresses unreadable (no mail capture): blocked, nothing resolved
        got = al.run(self.dsn, ["needs_my_reply"], schema=S, day=DAY, owners={})
        self.assertEqual(got[0]["status"], "blocked")
        self.assertEqual(self.rows("needs_my_reply")["m1"]["state"], "open")
        self.conn.execute(f"ALTER TABLE {S}.emails DROP COLUMN owner_replied_at")
        self.mail("m2")
        out = self.fire("needs_my_reply")["needs_my_reply"]
        self.assertEqual(out["status"], "failed")
        self.assertEqual({h: r["state"] for h, r in self.rows("needs_my_reply").items()},
                         {"m1": "open"})
        self.conn.execute(f"DROP TABLE {S}.emails")
        self.assertEqual(self.fire("needs_my_reply")["needs_my_reply"]["status"], "blocked")
        self.assertEqual(self.rows("needs_my_reply")["m1"]["state"], "open")

    def test_only_the_mailbox_owner_holding_the_rows_unit_sees_it(self):
        self.mail("m1", thread="t1", unit=7)
        self.mail("m2", thread="t1", unit=8, ai_ok=False, hours_ago=1)
        self.po("p1", 3)
        self.fire("needs_my_reply", "po_late")
        full = dict(mailboxes=[ME], me=[ME], units=[7, 8])
        self.assertEqual(self.read([1], **full), {("needs_my_reply", "m1"), ("po_late", "p1")})
        # every message the alert was computed from must be readable
        self.assertEqual(self.read([1], **{**full, "units": [7]}), {("po_late", "p1")})
        self.assertEqual(self.read([1], mailboxes=[ME], me=[ME]), {("po_late", "p1")})
        self.assertEqual(self.read([1], **{**full, "me": ["cy@ours.example"]}),
                         {("po_late", "p1")})
        self.assertEqual(self.read([1], **{**full, "mailboxes": []}), {("po_late", "p1")})
        self.assertEqual(self.read([1], ai=True, **full), {("po_late", "p1")})
        # a mail alert needs no business class, but a reader with none still needs the unit
        self.assertEqual(self.read([], **full), {("needs_my_reply", "m1")})
        self.assertEqual(self.read([]), set())
        with self.assertRaises(ValueError):
            al.read(self.conn, classes=[1], units="7", schema=S)
        # an alert computed from no guarded row at all can never be stored
        import psycopg
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                f"INSERT INTO {S}.alerts (rule, kind, record_table, handle, why, since, "
                "first_seen, last_seen, state, needs, ai_ok) VALUES ('x', 'x', 'emails', 'h', "
                "'w', now(), now(), now(), 'open', '{}', true)")


if __name__ == "__main__":
    unittest.main()
