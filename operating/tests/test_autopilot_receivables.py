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
    def order(self, lines: list[tuple[str, str, str]], number: str = "OR-1",
              day: str = "2026-10-01") -> tuple:
        w = self.w
        order = w.obj("SalesOrder", {"order_number": number, "currency": "CAD",
                                     "customer_po": "PO-9", "order_date": day},
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
        order, (a, b, c), _ols = self.order([("SYN-A", "4", "17.705"), ("SYN-B", "3", "4"),
                                            ("SYN-C", "0", "1")])
        due = self.shipment(order, [(a, "4"), (b, "3"), (c, "0")], "2026-10-21")
        # an order shipped in parts (a part, or a second shipment): invoiced as the person
        # decides, never prepared
        part_order, (e,), _ = self.order([("SYN-E", "5", "2")], "OR-4")
        part = self.shipment(part_order, [(e, "2")], "2026-10-21", "SH-4")
        rest_order, (f,), _ = self.order([("SYN-F", "5", "2")], "OR-5")
        self.shipment(rest_order, [(f, "2")], "2026-10-14", "SH-5")
        rest = self.shipment(rest_order, [(f, "3")], "2026-10-21", "SH-6")
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
        self.assertEqual(set(got), {due, odd, part, rest})
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
        for split in (part, rest):
            self.assertEqual(ap._prepare(st, spec, got[split], DAY, (), 30),
                             {"skip": "shipped_in_parts"})

    def test_score_day_counts_an_entry_out_of_time_and_goes_on(self):
        from unittest import mock
        from operating import receivables_score as rs
        order, (a,), _ = self.order([("SYN-A", "2", "5")])
        self.shipment(order, [(a, "2")], "2026-10-21")
        st = self.store()

        def slow(_ctx, _event):
            raise ap.HandlerTimeout()
        flows = {**rs.JUDGES, "invoice_entry": (ar.invoice_entry_events, slow,
                                                rs.judge_invoice)}
        with mock.patch.object(rs, "JUDGES", flows):
            got = rs.score_day(st, people(), ap.load_registry(), "invoice_entry", DAY)
        self.assertEqual(got["counts"], {"events": 1, "skip:handler_timeout": 1})

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

    def test_one_orders_shipments_of_one_day_are_one_entry_never_two(self):
        # DEV 10-06..11-03: an invoice billing items its shipment lacked had them on another
        # shipment of the order that day in 14 of 27 cases (one invoice per order and day)
        order, (a, b), _ = self.order([("SYN-A", "4", "2"), ("SYN-B", "5", "3")])
        first = self.shipment(order, [(a, "4")], "2026-10-21", "SH-1")
        second = self.shipment(order, [(b, "5")], "2026-10-21", "SH-2")
        other_order, (c,), _ = self.order([("SYN-C", "1", "9")], "OR-2")
        apart = self.shipment(other_order, [(c, "1")], "2026-10-21", "SH-3")
        st = self.store()
        reg = ap.load_registry()
        got = {e["object_id"]: e for e in ar.invoice_entry_events(st, reg, DAY)}
        self.assertEqual(set(got), {first, apart})            # the second rides on the first
        self.assertEqual(got[first]["with"], [second])
        self.assertNotIn("with", got[apart])
        # a lone shipment's entry is keyed on its order and day too: the group it may grow
        # into in a later cycle is the same entry, never a second one billing it again
        self.assertEqual(got[apart]["event_id"],
                         ap._digest("record.due", "invoice_entry", other_order, "2026-10-21"))
        spec = next(s for s in reg["autopilots"] if s["id"] == "invoice_entry")
        entry = ap._prepare(st, spec, got[first], DAY, (), 30)["work"]["entry"]
        self.assertEqual([(ln["item"], ln["quantity"]) for ln in entry["lines"]],
                         [("SYN-A", "4"), ("SYN-B", "5")])
        self.assertEqual((entry["subtotal"], entry["with_shipments"]), ("23.00", ["SH-2"]))
        self.assertEqual(len(got[first]["handles"]), len(set(got[first]["handles"])))
        # a "with" naming another order's shipment is never billed here (no guess): the
        # day's own second shipment is then left out, so the entry is a part: none
        self.assertEqual(ap._prepare(st, spec, {**got[first], "with": [apart]}, DAY, (), 30),
                         {"skip": "shipped_in_parts"})

    def test_a_group_shipment_that_cannot_be_billed_leaves_no_entry(self):
        order, (a,), _ = self.order([("SYN-A", "4", "2")])
        stray = self.w.obj("Product", {"code": "SYN-X"})
        first = self.shipment(order, [(stray, "1")], "2026-10-21", "SH-1")   # not on the order
        second = self.shipment(order, [(a, "4")], "2026-10-21", "SH-2")
        st = self.store()
        reg = ap.load_registry()
        (event,) = ar.invoice_entry_events(st, reg, DAY)
        self.assertEqual((event["object_id"], event["with"]), (first, [second]))
        spec = next(s for s in reg["autopilots"] if s["id"] == "invoice_entry")
        # the stray shipment cannot be billed, so the rest is not the whole order at once:
        # no entry (an entry leaving a shipment of the day out would be a part)
        self.assertEqual(ap._prepare(st, spec, event, DAY, (), 30),
                         {"skip": "shipped_in_parts"})
        # a lead that ships nothing never blocks the day's shipment of the whole order
        empty_order, (e,), _ = self.order([("SYN-E", "3", "2")], "OR-7")
        lead = self.shipment(empty_order, [(e, "0")], "2026-10-21", "SH-70")
        rest = self.shipment(empty_order, [(e, "3")], "2026-10-21", "SH-71")
        st = self.store()
        (grown,) = [x for x in ar.invoice_entry_events(st, reg, DAY) if x["object_id"] == lead]
        self.assertEqual(grown["with"], [rest])
        work = ap._prepare(st, spec, {**grown, "with": [rest, rest, lead]}, DAY, (), 30)["work"]
        self.assertEqual([(ln["item"], ln["quantity"]) for ln in work["entry"]["lines"]],
                         [("SYN-E", "3")])                    # each shipment billed once
        # one entry per order and day, whoever is in it: a group grown by a shipment found
        # in a later cycle keeps the id (never a second entry billing the same lines)
        self.assertEqual(event["event_id"],
                         ap._digest("record.due", "invoice_entry", order, "2026-10-21"))

    def test_one_line_order_of_a_customer_whose_orders_ship_whole_gets_its_posting(self):
        w = self.w
        n = iter(range(100))

        def past(cust: str, day: str, shipped: str, after_days: int = 2) -> None:
            k = next(n)
            order, (p,), _ = self.order([("SYN-P", "4", "1")], f"OR-P{k}", day)
            w.link("SalesOrder.ordered_by", order, cust)
            d = date.fromisoformat(day).toordinal() + after_days
            self.shipment(order, [(p, shipped)], date.fromordinal(d).isoformat(), f"SH-P{k}")

        def new(cust: str, lines: list) -> str:
            order, items, _ = self.order(lines, f"OR-N{next(n)}", DAY.isoformat())
            w.link("SalesOrder.ordered_by", order, cust)
            self.events.append(("SalesOrder.ordered", order, f"{DAY.isoformat()}T00:00:00Z"))
            return order, items

        cust = {c: w.obj("Customer", {"code": c}, binding="portal.sage_ar_customer")
                for c in ("steady", "splitter", "recent", "few", "late")}
        whole = ("2026-09-20", "2026-10-01", "2026-10-12")      # 9+ days before the day
        for c in ("steady", "splitter", "recent", "late"):
            for day in whole:
                past(cust[c], day, "4", 9 if c == "late" else 2)   # late: shipped after 9 d
        past(cust["splitter"], "2026-10-13", "2")         # its latest known order: a part
        past(cust["recent"], "2026-10-18", "2", 1)        # a part, outcome not known yet
        for day in whole[1:]:
            past(cust["few"], day, "4")                   # only two orders to go by
        due, _ = new(cust["steady"], [("SYN-A", "6", "1")])
        shipped_already, (s_a,) = new(cust["steady"], [("SYN-A", "6", "1")])
        self.shipment(shipped_already, [(s_a, "1")], "2026-10-20", "SH-X")
        new(cust["steady"], [("SYN-A", "6", "1"), ("SYN-B", "1", "1")])
        recent, _ = new(cust["recent"], [("SYN-A", "6", "1")])
        for c in ("splitter", "few", "late"):
            new(cust[c], [("SYN-A", "6", "1")])
        st = self.store()
        reg = ap.load_registry()
        events = ar.shipment_posting_events(st, reg, DAY)
        # a one-line order with nothing shipped of a customer whose latest orders, each
        # known by then, shipped whole within a week; a recent part not yet known counts
        # for nothing; a split, late or short history, two lines or a shipment: none
        self.assertEqual(sorted(e["object_id"] for e in events), sorted([due, recent]))
        spec = next(s for s in reg["autopilots"] if s["id"] == "shipment_posting")
        event = next(e for e in events if e["object_id"] == due)
        self.assertTrue(ap.matches(spec["when"], event))
        prepared = ap._prepare(st, spec, event, DAY, (), 30)
        work = prepared["work"]
        self.assertEqual([(ln["item"], ln["quantity"]) for ln in work["entry"]["lines"]],
                         [("SYN-A", "6")])
        self.assertIn("finance", prepared["markings"])
        self.assertTrue(prepared["citations"])
        self.assertTrue(work["dry_run"])
        self.assertNotIn("write", work)
        # the next day the order is no longer new
        self.assertEqual(ar.shipment_posting_events(st, reg, date(2026, 10, 22)), [])


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
        self.doc("SYN-IN-4001")
        self.doc("SYN-IN-4001", "SYNTHOTHER")             # one number, two customers
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
                                           "NOPE-9", "SYN-IN-4001"))}
        # one remittance paying two customers: two entries (an accounting receipt is per
        # customer), each to who enters that customer's receipts
        remits[("f5", 0)] = [*remits[("f0", 0)], *remits[("f1", 0)]]
        # a line whose number names two customers' invoices: no split, left whole
        remits[("f6", 0)] = [*remits[("f0", 0)], *remits[("f4", 0)]]
        with mock.patch.object(ar, "remittances", lambda _st, _p: remits):
            events = ar.cash_application_events(st, reg, DAY, people())
            got = {(e["remittance"][0], *(e["remittance"][2] or [None])): e["owner_hint"]
                   for e in events}
            self.assertEqual(got, {("f0", "SYNTHCUST"): APPROVER_OID,
                                   ("f1", "SYNTHOTHER"): None, ("f2", "SYNTHNONE"): None,
                                   ("f3", None): None, ("f4", None): None,
                                   ("f5", "SYNTHCUST"): APPROVER_OID,
                                   ("f5", "SYNTHOTHER"): None, ("f6", None): None})
            self.assertEqual(len({e["event_id"] for e in events}), len(events))
            self.assertNotIn(EXCLUDED_OID, got.values())
            st.autopilot_people = people()
            work = {k: ar.h_cash_application(ap.Ctx(st, DAY, float("inf"), ()), e)
                    for e in events if (k := (e["remittance"][0], *(e["remittance"][2] or
                                                                     [None])))[0] in ("f5", "f6")}
            self.assertEqual([a["invoice"] for a in
                              work[("f5", "SYNTHCUST")]["entry"]["applied"]], ["SYN-IN-1001"])
            self.assertEqual(work[("f5", "SYNTHOTHER")]["entry"]["customer"], "SYNTHOTHER")
            self.assertEqual(work[("f6", None)], {"skip": "reference_ambiguous"})
            self.assertIsNone(ar.receipt_keyer(st, "SYNTHCUST", DAY, people(unavailable=True)))
        spec = next(s for s in reg["autopilots"] if s["id"] == "cash_application")
        self.assertEqual(spec["owner"], "hinted")

    def test_customers_paid_together_before_get_one_receipt_under_that_payer(self):
        from unittest import mock
        self.doc("SYN-IN-1001")
        self.doc("SYN-IN-2001", "SYNTHOTHER")
        self.doc("SYN-IN-3001", "SYNTHNONE")
        # an earlier receipt entered under SYNTHPAYER paid both customers' invoices
        for cust in ("SYNTHCUST", "SYNTHOTHER"):
            self.w.obj("RelSageArTcp", {"cntbtch": "7", "cntitem": "1", "idcust": cust,
                                        "audtdate": "20261001"},
                       binding="gen.sage.ar_tcp", audience="api:sage.ar", markings=FIN)
        self.w.obj("RelSageArTcr", {"cntbtch": "7", "cntitem": "1", "idcust": "SYNTHPAYER",
                                    "audtdate": "20261001", "enteredby": "zz-batch"},
                   binding="gen.sage.ar_tcr", audience="api:sage.ar", markings=FIN)
        # a receipt whose second customer's line was entered on the day: not read
        for cust, day in (("SYNTHCUST", "20261001"), ("SYNTHNONE", "20261021")):
            self.w.obj("RelSageArTcp", {"cntbtch": "8", "cntitem": "1", "idcust": cust,
                                        "audtdate": day},
                       binding="gen.sage.ar_tcp", audience="api:sage.ar", markings=FIN)
        self.w.obj("RelSageArTcr", {"cntbtch": "8", "cntitem": "1", "idcust": "SYNTHPAYER2",
                                    "audtdate": "20261001", "enteredby": "zz-batch"},
                   binding="gen.sage.ar_tcr", audience="api:sage.ar", markings=FIN)
        st = self.store()
        st.autopilot_people = people()
        both = self.lines(("SYN-IN-1001", "500.00"), ("SYN-IN-2001", "20"))
        apart = self.lines(("SYN-IN-1001", "500.00"), ("SYN-IN-3001", "20"))
        self.assertEqual(ar.receipt_plan(st, both, DAY),
                         [(("SYNTHCUST", "SYNTHOTHER"), "SYNTHPAYER")])
        self.assertEqual(ar.receipt_plan(st, apart, DAY),
                         [(("SYNTHCUST",), "SYNTHCUST"), (("SYNTHNONE",), "SYNTHNONE")])
        # the earlier receipt is after the day: never read (a replay never reads ahead)
        self.assertEqual(len(ar.receipt_plan(st, both, date(2026, 10, 1))), 2)
        with mock.patch.object(ar, "remittances", lambda _st, _p: {("f1", 0): both}):
            (event,) = ar.cash_application_events(st, ap.load_registry(), DAY, people())
            work = ar.h_cash_application(ap.Ctx(st, DAY, float("inf"), ()), event)
        self.assertEqual((work["entry"]["customer"], work["entry"]["total"],
                          work["entry"]["invoices_of"]),
                         ("SYNTHPAYER", "520.00", ["SYNTHCUST", "SYNTHOTHER"]))

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
