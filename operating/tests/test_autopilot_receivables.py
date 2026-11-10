"""Receivables entries prepared from the records, dry run (``operating.autopilot_receivables``):
the invoice a shipment needs, the shipment an order ready to ship needs, the cash
application a remittance needs; finance-marked, nothing written. Synthetic records only."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
import unittest

from operating import autopilot as ap
from operating import autopilot_receivables as ar
from operating import proactive_sheets as ps
from operating.tests.test_autopilot_replay import World
from operating.tests.test_proactive_threads import people

FIN = '["finance"]'
DAY = date(2026, 10, 21)
SHIP = "portal.sage_oe_shipment_header"


class Receivables(World):
    def order(self, lines: list[tuple[str, str, str]], number: str = "OR-1") -> tuple:
        w = self.w
        order = w.obj("SalesOrder", {"order_number": number, "currency": "CAD",
                                     "customer_po": "PO-9", "order_date": "2026-10-01"},
                      binding="portal.sage_oe_order_header", markings=FIN)
        products, ols = [], []
        for i, (code, qty, price) in enumerate(lines):
            item = w.obj("Product", {"code": code})
            ol = w.obj("SalesOrderLine", {"sage_item_code": code, "ordered_quantity": qty,
                                          "unit_price": price, "line_type": "1"},
                       binding="portal.sage_oe_order_detail", markings=FIN)
            w.link("SalesOrderLine.line_of", ol, order)
            w.link("SalesOrderLine.for_item", ol, item)
            products.append(item)
            ols.append(ol)
        return order, products, ols

    def shipment(self, order: str, shipped: list[tuple[str, str]], day: str,
                 number: str = "SH-1") -> str:
        w = self.w
        sh = w.obj("Shipment", {"sage_shipment_number": number, "ship_date": day},
                   binding=SHIP)
        w.link("Shipment.ships", sh, order)
        for item, qty in shipped:
            sl = w.obj("ShipmentLine", {"shipped_quantity": qty},
                       binding="portal.sage_oe_shipment_detail")
            w.link("ShipmentLine.line_of", sl, sh)
            w.link("ShipmentLine.for_item", sl, item)
        self.events.append(("Shipment.sage_shipped", sh, f"{day}T00:00:00Z"))
        return sh

    def invoice(self, order: str, day: str) -> str:
        inv = self.w.obj("SalesInvoice", {"invoice_number": "IN-1", "invoice_date": day},
                         binding="portal.sage_oe_invoice_header")
        self.w.link("SalesInvoice.bills", inv, order)
        return inv

    def test_shipment_gets_its_invoice_priced_from_the_order_finance_marked(self):
        order, (a, b, c), _ols = self.order([("SYN-A", "10", "17.705"), ("SYN-B", "3", "4"),
                                            ("SYN-C", "5", "1")])
        due = self.shipment(order, [(a, "4"), (b, "3"), (c, "0")], "2026-10-21")
        # an order already invoiced before the day: nothing to enter
        done_order, (d,), _ = self.order([("SYN-D", "1", "9")], "OR-2")
        self.shipment(done_order, [(d, "1")], "2026-10-20", "SH-2")
        self.invoice(done_order, "2026-10-20")
        # a shipped item the order does not hold: no entry (no entry beats a wrong one)
        odd_order, _p, _ = self.order([("SYN-E", "1", "9")], "OR-3")
        stray = self.w.obj("Product", {"code": "SYN-X"})
        odd = self.shipment(odd_order, [(stray, "1")], "2026-10-21", "SH-3")
        st = self.store()
        reg = ap.load_registry()
        got = {e["object_id"]: e for e in ar.invoice_entry_events(st, reg, DAY)}
        self.assertEqual(set(got), {due, odd})
        spec = next(s for s in reg["autopilots"] if s["id"] == "invoice_entry")
        self.assertTrue(ap.matches(spec["when"], got[due]))
        self.assertFalse(any(ap.matches(s.get("when") or {}, got[due])
                             for s in reg["autopilots"] if s["on"] == "record.due"
                             and s["id"] != "invoice_entry"))
        prepared = ap._prepare(st, spec, got[due], DAY, (), 30)
        entry = prepared["work"]["entry"]
        self.assertEqual([(ln["item"], ln["quantity"], ln["amount"]) for ln in entry["lines"]],
                         [("SYN-A", "4", "70.82"), ("SYN-B", "3", "12.00")])
        self.assertEqual((entry["subtotal"], entry["currency"]), ("82.82", "CAD"))
        self.assertEqual(Decimal(entry["subtotal"]),
                         sum(Decimal(ln["amount"]) for ln in entry["lines"]))
        self.assertIn("finance", prepared["markings"])
        self.assertTrue(prepared["citations"])
        self.assertTrue(prepared["work"]["dry_run"])
        self.assertNotIn("write", prepared["work"])
        self.assertIsNone(ap.write_spec({"body": {"prepared": prepared["work"]},
                                         "citations": prepared["citations"]}))
        self.assertEqual(ap._prepare(st, spec, got[odd], DAY, (), 30),
                         {"skip": "order_line_unknown"})

    def test_entry_goes_to_who_keys_this_customers_invoices_never_the_role(self):
        from operating.tests.test_proactive_engine import EXCLUDED_OID
        from operating.tests.test_proactive_threads import APPROVER_OID
        w = self.w
        keyed, nobody = (w.obj("Customer", {"code": c}, binding="portal.sage_ar_customer")
                         for c in ("SYNC1", "SYNC2"))
        for n, (cust, day, code) in enumerate([(keyed, "2026-10-02", "lead"),
                                               (keyed, "2026-10-09", "boss"),
                                               (nobody, "2026-10-09", "zz-batch")]):
            inv = w.obj("SalesInvoice", {"invoice_number": f"IN-9{n}", "invoice_date": day},
                        binding="portal.sage_oe_invoice_header")
            w.link("SalesInvoice.billed_to", inv, cust)
            w.obj("RelSageArIbh", {"idinvc": f"IN-9{n}", "enteredby": code},
                  binding="gen.sage.ar_ibh", audience="api:sage.ar", markings=FIN)
        due = {}
        for n, cust in enumerate((keyed, nobody)):
            order, (a,), _ = self.order([("SYN-A", "1", "5")], f"OR-K{n}")
            due[cust] = self.shipment(order, [(a, "1")], "2026-10-21", f"SH-K{n}")
            w.link("Shipment.shipped_to", due[cust], cust)
        st = self.store()
        reg = ap.load_registry()
        got = {e["object_id"]: e for e in ar.invoice_entry_events(st, reg, DAY, people())}
        # the latest keyer is excluded: the one before them; a code naming no one: no owner
        self.assertEqual(got[due[keyed]]["owner_hint"], APPROVER_OID)
        self.assertNotEqual(got[due[keyed]]["owner_hint"], EXCLUDED_OID)
        self.assertIsNone(got[due[nobody]]["owner_hint"])
        spec = next(s for s in reg["autopilots"] if s["id"] == "invoice_entry")
        self.assertEqual(spec["owner"], "hinted")
        # no exclusion list: nobody is hinted
        self.assertTrue(all(e["owner_hint"] is None for e in ar.invoice_entry_events(
            st, reg, DAY, people(unavailable=True))))

    def test_cardinality_one_entry_per_shipment(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    order, (a,), _ = self.order([("SYN-A", "2", "5")], f"OR-{i}")
                    self.shipment(order, [(a, "2")], "2026-10-21", f"SH-{i}")
                st = self.store()
                events = ar.invoice_entry_events(st, ap.load_registry(), DAY)
                self.assertEqual(len({e["object_id"] for e in events}), n)
                self.assertEqual(len(events), n)

    def test_order_ready_to_ship_gets_its_outstanding_lines(self):
        order, (a, b), _ = self.order([("SYN-A", "10", "1"), ("SYN-B", "4", "1")])
        self.shipment(order, [(a, "6")], "2026-10-15", "SH-0")        # shipped before
        self.shipment(order, [(b, "4")], "2026-10-25", "SH-9")        # after: not known
        desk = self.w.obj("SalesOrder", {"status": "READY_TO_SHIP"}, binding="portal.app_x")
        self.w.link("SalesOrder.same_record_as", desk, order)
        open_desk = self.w.obj("SalesOrder", {"status": "Open"}, binding="portal.app_x")
        self.w.link("SalesOrder.same_record_as", open_desk,
                    self.order([("SYN-C", "1", "1")], "OR-5")[0])
        st = self.store()
        # the fixture records every status on its one day: ready since then
        ready_day = date(2026, 10, 1)
        events = ar.shipment_posting_events(st, ap.load_registry(), ready_day)
        self.assertEqual([e["object_id"] for e in events], [order])
        # ready since before the window: raised once, on the day it became ready
        self.assertEqual(ar.shipment_posting_events(st, ap.load_registry(), DAY), [])
        work = ar.h_shipment_posting(ap.Ctx(st, DAY, float("inf"), ()), events[0])
        self.assertEqual([(ln["item"], ln["quantity"]) for ln in work["entry"]["lines"]],
                         [("SYN-A", "4"), ("SYN-B", "4")])


class Cash(World):
    def doc(self, number: str, party: str = "SYNTHCUST", *, paid_on: str = "") -> str:
        return self.w.obj("RelSageArOpenDocument", {
            "idcust": party, "idinvc": number, "dateinvc": "20261001", "datedue": "20261031",
            "amtinvctc": "500.00", "amtduetc": "500.00", "codecurn": "CAD",
            "swpaid": "1" if paid_on else "0", "datepaid": paid_on, "trxtypetxt": "1"},
            binding="gen.sage.ar_open_document", audience="api:sage.ar", markings=FIN)

    def lines(self, *refs: tuple[str, str]) -> list[ps.Reading]:
        cite = {"l1_handle": "synth://remit/1", "object_id": "Attachment:fx:1",
                "markings": "[]"}
        return [ps.Reading("f1", 0, i, f"A{i}", {"ref": [ref], "amount_paid": [paid]}, {},
                           "remit.xlsx", DAY, cite) for i, (ref, paid) in enumerate(refs)]

    def test_remittance_applies_to_one_customers_open_invoices_or_nothing(self):
        self.doc("SYN-IN-1001")
        self.doc("SYN-IN-1002")
        self.doc("SYN-IN-2001", "SYNTHOTHER")
        self.doc("SYN-IN-3001", paid_on="20261010")
        st = self.store()
        ctx = lambda: ap.Ctx(st, DAY, float("inf"), ())
        work = ar.prepare_cash(ctx(), self.lines(("SYN-IN-1001", "500.00"),
                                                 ("in 1002", "120.5"), ("NOPE-9", "3")))
        self.assertEqual(work["entry"]["applied"],
                         [{"invoice": "SYN-IN-1001", "amount": "500.00", "discount": None},
                          {"invoice": "SYN-IN-1002", "amount": "120.50", "discount": None}])
        self.assertEqual((work["entry"]["total"], work["entry"]["currency"]),
                         ("620.50", "CAD"))
        c = ctx()
        ar.prepare_cash(c, self.lines(("SYN-IN-1001", "500.00")))
        self.assertIn("finance", {m for row in ap._markings(st, c.objects) for m in row})
        for refs, why in ((("SYN-IN-1001", "1"), ("SYN-IN-2001", "1")),
                          "customer_or_currency_not_one"), \
                         ((("SYN-IN-3001", "1"),), "invoice_not_open"), \
                         ((("SYN-IN-1001", "abc"),), "amount_unread"), \
                         ((("NOPE-9", "1"),), "no_reference_matched"):
            self.assertEqual(ar.prepare_cash(ctx(), self.lines(*refs)), {"skip": why})

    def test_cash_goes_to_who_enters_this_customers_receipts_never_the_role(self):
        from unittest import mock
        from operating.tests.test_proactive_engine import EXCLUDED_OID
        from operating.tests.test_proactive_threads import APPROVER_OID
        self.doc("SYN-IN-1001")
        self.doc("SYN-IN-2001", "SYNTHOTHER")
        self.doc("SYN-IN-3001", "SYNTHNONE")
        for cust, day, code in (("SYNTHCUST", "20261002", "lead"),
                                ("SYNTHCUST", "20261009", "boss"),     # excluded: skipped
                                ("SYNTHCUST", "20261021", "lead2"),    # the day itself: unknown
                                ("SYNTHOTHER", "20261009", "zz-batch"),
                                ("SYNTHNONE", "20261022", "lead")):   # after: never read
            self.w.obj("RelSageArTcr", {"idcust": cust, "audtdate": day, "enteredby": code},
                       binding="gen.sage.ar_tcr", audience="api:sage.ar", markings=FIN)
        st = self.store()
        reg = ap.load_registry()
        cite = {"l1_handle": "synth://remit/1", "object_id": "Attachment:fx:1",
                "markings": "[]"}
        remits = {(f"f{i}", 0): [ps.Reading(f"f{i}", 0, 0, "A0", {"ref": [ref],
                                                                  "amount_paid": ["10"]},
                                            {}, "remit.xlsx", DAY, cite)]
                  for i, ref in enumerate(("SYN-IN-1001", "SYN-IN-2001", "SYN-IN-3001",
                                           "NOPE-9"))}
        remits[("f4", 0)] = [*remits[("f0", 0)], *remits[("f1", 0)]]   # two customers
        with mock.patch.object(ar, "remittances", lambda _st, _p: remits):
            got = {e["remittance"][0]: e["owner_hint"]
                   for e in ar.cash_application_events(st, reg, DAY, people())}
            self.assertEqual(got, {"f0": APPROVER_OID, "f1": None, "f2": None, "f3": None,
                                   "f4": None})
            self.assertNotIn(EXCLUDED_OID, got.values())
            self.assertIsNone(ar.receipt_keyer(st, "SYNTHCUST", DAY, people(unavailable=True)))
        spec = next(s for s in reg["autopilots"] if s["id"] == "cash_application")
        self.assertEqual(spec["owner"], "hinted")

    def test_no_people_exclusion_no_remittance_is_read(self):
        st = self.store()
        reg = ap.load_registry()
        self.assertEqual(ar.cash_application_events(st, reg, DAY, people(unavailable=True)),
                         [])
        self.assertEqual(ar.cash_application_events(st, reg, DAY, None), [])
        event = {"object_id": "Attachment:fx:1", "remittance": ["f1", 0]}
        self.assertEqual(ar.h_cash_application(ap.Ctx(st, DAY, float("inf"), ()), event),
                         {"skip": "people_exclusion_unavailable"})


if __name__ == "__main__":
    unittest.main()
