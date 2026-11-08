"""Receivable and payable alerts and the cash forecast (``operating.proactive_cash``,
``operating.cash_forecast``) through the engine, the per-reader inbox and the tool's answer.

SYNTHETIC fixtures only (``World`` stores, synthetic codes); evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import time
import unittest

from operating import briefing
from operating import cash_forecast as cf
from operating import proactive_cash as pc
from operating import proactive_engine as pe
from operating.tests.test_proactive_engine import AS_OF, Engine, World

FIN = '["finance"]'
AR = ("RelSageArOpenDocument", "gen.sage.ar_open_document", "api:sage.ar_open_document")
AP = ("RelSageApOpenDocument", "gen.sage.ap_open_document", "api:sage.ap_open_document")
TCR = ("RelSageApTcr", "gen.sage.ap_tcr", "api:sage.ap_tcr")
TCP = ("RelSageApTcp", "gen.sage.ap_tcp", "api:sage.ap_tcp")


def sage(offset: int | None) -> str:
    """A Sage date ``offset`` days before the run's day (None: Sage's 0)."""
    return "0" if offset is None else (AS_OF - timedelta(days=offset)).strftime("%Y%m%d")


def doc(w: World, kind: tuple, values: dict, *, audience: str | None = None) -> str:
    """A Sage record; its money columns make it finance-marked."""
    return w.obj(kind[0], values, binding=kind[1], audience=audience or kind[2],
                 markings=FIN if any(k.startswith("amt") for k in values) else "[]")


def ar_invoice(w: World, cust: str, number: str, invoiced: int, paid: int | None, *,
               due: int | None = None, amount: str = "100.00", currency: str = "CAD",
               audience: str | None = None) -> str:
    return doc(w, AR, {"idcust": cust, "idinvc": number, "dateinvc": sage(invoiced),
                       "datedue": sage(invoiced - 30 if due is None else due),
                       "datepaid": sage(paid), "swpaid": "0" if paid is None else "1",
                       "trxtypetxt": "1", "codecurn": currency, "amtinvctc": amount,
                       "amtduetc": amount if paid is None else "0"}, audience=audience)


def ap_invoice(w: World, vend: str, number: str, invoiced: int, *, paid: int | None = None,
               due: int | None = None, amount: str = "500.00", disc_date: int | None = None,
               disc: str = "0", kind: str = "1", po: str = "", currency: str = "CAD") -> str:
    return doc(w, AP, {"idvend": vend, "idinvc": number, "dateinvc": sage(invoiced),
                       "dateinvcdu": sage(invoiced - 30 if due is None else due),
                       "datedisc": sage(disc_date), "datepaid": sage(paid),
                       "swpaid": "0" if paid is None else "1", "txttrxtype": kind,
                       "codecurn": currency, "amtinvctc": amount,
                       "amtduetc": amount if paid is None else "0", "amtdisctc": disc,
                       "idponbr": po})


def slow_customer(w: World, cust: str, *, recent_days: int = 60, prior_days: int = 30) -> None:
    """Six invoices paid in ``prior_days`` within the prior year, three in ``recent_days``
    within the last 90 days."""
    for i in range(6):
        paid = 120 + 30 * i
        ar_invoice(w, cust, f"{cust}-P{i}", paid + prior_days, paid)
    for i in range(3):
        paid = 10 + 20 * i
        ar_invoice(w, cust, f"{cust}-R{i}", paid + recent_days, paid)


def payment(w: World, vend: str, number: str, day: int, amount: str, pays: str,
            batch: str, entry: str) -> str:
    oid = doc(w, TCR, {"idvend": vend, "docnbr": number, "datermit": sage(day),
                       "codecurn": "CAD", "btchtype": "PY", "cntbtch": batch, "cntentr": entry,
                       "idinvcmtch": "", "amtrmittc": amount})
    doc(w, TCP, {"idvend": vend, "cntbtch": batch, "cntrmit": entry, "idinvc": pays})
    return oid


class CashAlerts(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=list(pc.RULES), **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    def test_a_customer_paying_slower_than_its_own_prior_year_is_raised(self):
        w = World()
        slow_customer(w, "SYNTHC1")
        who = w.obj("Customer", {"erp_code": "SYNTHC1", "name": "SYNTH One"},
                    binding="portal.sage_ar_customers", audience="api:sage.ar_customers")
        slow_customer(w, "SYNTHC2", recent_days=31)                   # steady
        for i in range(3):                                             # too little history
            ar_invoice(w, "SYNTHC3", f"C3-{i}", 90, 5 + i)
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pc.PAYMENT_RULE], "complete")
        (gap,) = self.kinds(pc.SLOWER)
        self.assertEqual(gap["object_id"], who)
        self.assertEqual(gap["summary"],
                         "Customer SYNTHC1 (sage) now pays in 60.0 days on average over 3 "
                         "invoices paid in the last 90 days, against 30.0 days over 6 "
                         "invoices in the 365 days before (30 days slower)")
        self.assertEqual(gap["follow_up"]["priority"], "high")
        self.assertEqual(gap["event"]["days"], 30)
        self.assertEqual(len(gap["citations"]), 10)   # every averaged invoice, the customer
        self.assertEqual(self.kinds(pc.SLOWER, may_read=lambda a: a != "api:sage.ar_customers"),
                         [])

    def test_a_restricted_reader_sees_no_money_alert_it_may_not_open(self):
        w = World()
        slow_customer(w, "SYNTHC1")
        ap_invoice(w, "SYNTHV1", "INV-0042", 30, paid=10, disc_date=20, disc="10.00")
        ap_invoice(w, "SYNTHV2", "SYN-7781", 40, paid=20)
        ap_invoice(w, "SYNTHV2", "SYN7781", 30, paid=5)
        self.run_rules(w)
        every = [pc.SLOWER, pc.DISCOUNT_MISSED, pc.DUP_INVOICE]
        for kind in every:
            self.assertEqual(len(self.kinds(kind)), 1, kind)
        for kind in every:
            with self.subTest(kind=kind):
                self.assertEqual(self.kinds(kind, held=frozenset()), [])
                self.assertEqual(self.kinds(kind, may_read=lambda a: not a.startswith(
                    "api:sage.a")), [])

    def test_a_discount_missed_paid_late_or_unpaid_past_its_date_is_raised(self):
        w = World()
        late = ap_invoice(w, "SYNTHV1", "INV-1", 30, paid=10, disc_date=20, disc="12.50")
        ap_invoice(w, "SYNTHV1", "INV-2", 30, paid=25, disc_date=20, disc="0")   # taken
        ap_invoice(w, "SYNTHV1", "INV-3", 30, paid=10, disc_date=20, disc="0")   # none left
        ap_invoice(w, "SYNTHV1", "INV-4", 300, paid=200, disc_date=280, disc="9")  # history
        unpaid = ap_invoice(w, "SYNTHV1", "INV-5", 20, disc_date=5, disc="3.00")
        ap_invoice(w, "SYNTHV1", "INV-6", 5, disc_date=-5, disc="3.00")          # not yet
        self.run_rules(w)
        gaps = {g["object_id"]: g for g in self.kinds(pc.DISCOUNT_MISSED)}
        self.assertEqual(set(gaps), {late, unpaid})
        self.assertEqual(gaps[late]["summary"],
                         f"Early-pay discount of 12.50 CAD missed on supplier SYNTHV1 invoice "
                         f"INV-1: paid {(AS_OF - timedelta(days=10)).isoformat()}, 10 days "
                         f"after its discount date {(AS_OF - timedelta(days=20)).isoformat()}")
        self.assertEqual(gaps[unpaid]["follow_up"]["priority"], "high")
        self.assertEqual(gaps[late]["event"]["amount"]["value"], "12.50")

    def test_an_invoice_keyed_twice_is_raised_and_a_series_or_a_reversal_is_not(self):
        w = World()
        a = ap_invoice(w, "SYNTHV1", "INV-0042517", 40, paid=10)
        b = ap_invoice(w, "SYNTHV1", "42517", 30, paid=5)
        ap_invoice(w, "SYNTHV2", "2026-101", 40)                  # a series: rent
        ap_invoice(w, "SYNTHV2", "2026-102", 20)
        ap_invoice(w, "SYNTHV3", "AB-1234", 40)                   # reversed by a credit
        ap_invoice(w, "SYNTHV3", "AB 1234", 30)
        ap_invoice(w, "SYNTHV3", "CN-1", 25, amount="-500.00", kind="3")
        ap_invoice(w, "SYNTHV4", "Q-5521", 40, amount="500.00")    # another amount
        ap_invoice(w, "SYNTHV4", "Q5521", 30, amount="500.01")
        summary = self.run_rules(w)
        (gap,) = self.kinds(pc.DUP_INVOICE)
        self.assertEqual(gap["event"]["documents"], sorted([a, b]))
        self.assertTrue(gap["summary"].endswith("are both 500.00 CAD and both were paid"))
        self.assertEqual(gap["follow_up"]["priority"], "high")
        self.assertEqual(summary["detectors"][pc.DUPLICATE_RULE], "complete")

    def test_a_supplier_paid_twice_for_one_invoice_is_raised(self):
        w = World()
        payment(w, "SYNTHV1", "PY-1", 12, "800.00", "INV-77", "1", "1")
        payment(w, "SYNTHV1", "PY-2", 4, "800.00", "INV77", "2", "1")
        payment(w, "SYNTHV2", "PY-3", 12, "300.00", "INV-80", "3", "1")   # other invoices
        payment(w, "SYNTHV2", "PY-4", 4, "300.00", "INV-81", "4", "1")
        payment(w, "SYNTHV3", "PY-5", 40, "300.00", "INV-90", "5", "1")   # too far apart
        payment(w, "SYNTHV3", "PY-6", 4, "300.00", "INV-90", "6", "1")
        self.run_rules(w)
        (gap,) = self.kinds(pc.DUP_PAYMENT)
        self.assertIn("Supplier SYNTHV1 was paid 800.00 CAD twice for INV-77", gap["summary"])
        self.assertEqual(self.kinds(pc.DUP_PAYMENT, may_read=lambda a: a != TCP[2]), [])

    def test_numbers_of_a_series_are_never_one_document_keyed_twice(self):
        for a, b in (("INV-0042517", "42517"), ("INV 5521", "5521"), ("BA-1234", "AB-1234"),
                     ("SYN-7781", "syn7781"), ("INV-0012345", "12345")):
            with self.subTest(a=a, b=b):
                self.assertTrue(pc.similar_numbers(a, b))
        for a, b in (("LEASE12345-OCT", "LEASE12345-NOV"),
                     ("ACCT 0045123 OCT26", "ACCT 0045123 NOV26"), ("12345-A", "12345-B"),
                     ("INV1001", "INV1010"), ("AB1234", "AB1243"),
                     ("2017120-003", "2017120-004"),
                     ("10783", "10799"), ("12345", "123456"),
                     ("LOAN REPAY OCT/26", "LOAN REPAY OCT 30/26")):
            with self.subTest(a=a, b=b):
                self.assertFalse(pc.similar_numbers(a, b))

    def test_a_monthly_series_of_one_amount_is_not_a_duplicate(self):
        w = World()
        ap_invoice(w, "SYNTHV1", "LEASE12345-OCT", 40)
        ap_invoice(w, "SYNTHV1", "LEASE12345-NOV", 10)
        ap_invoice(w, "SYNTHV2", "INV1001", 40)
        ap_invoice(w, "SYNTHV2", "INV1010", 10)
        self.run_rules(w)
        self.assertEqual(self.kinds(pc.DUP_INVOICE), [])

    def test_the_same_amount_for_one_po_under_two_numbers_is_raised_and_rent_is_not(self):
        w = World()
        a = ap_invoice(w, "SYNTHV1", "88123", 30, amount="4200.00", po="PO-SYN-1")
        b = ap_invoice(w, "SYNTHV1", "INV-77", 27, amount="4200.00", po="PO-SYN-1")
        ap_invoice(w, "SYNTHV2", "A-1", 30, amount="900.00")          # nothing in common
        ap_invoice(w, "SYNTHV2", "B-2", 29, amount="900.00")
        for i, day in enumerate((95, 64, 33, 30)):                     # rent: a repeat charge
            ap_invoice(w, "SYNTHV3", f"R{i}9", day, amount="7000.00", po="PO-RENT")
        ap_invoice(w, "SYNTHV4", "X-10", 40, amount="300.00", po="PO-SYN-4")  # 10 days apart
        ap_invoice(w, "SYNTHV4", "Y-20", 30, amount="300.00", po="PO-SYN-4")
        ap_invoice(w, "SYNTHV5", "C-1", 30, amount="650.00", po="PO-SYN-5")   # other POs
        ap_invoice(w, "SYNTHV5", "D-2", 29, amount="650.00", po="PO-SYN-6")
        summary = self.run_rules(w)
        (gap,) = self.kinds(pc.DUP_INVOICE)
        self.assertEqual(gap["event"]["documents"], sorted([a, b]))
        self.assertEqual(gap["follow_up"]["match"], "amount_and_po")
        self.assertEqual(gap["follow_up"]["priority"], "low")
        self.assertIn("for purchase order PO-SYN-1", gap["summary"])
        self.assertEqual(summary["detectors"][pc.DUPLICATE_RULE], "complete")
        self.assertEqual(self.kinds(pc.DUP_INVOICE, held=frozenset()), [])

    def test_a_customer_whose_credits_rise_against_its_own_year_is_raised(self):
        w = World()
        steady = []
        for cust, recent_credits, prior_credits in (("SYNTHC1", 2, 0), ("SYNTHC2", 2, 6),
                                                    ("SYNTHC3", 1, 0)):
            for i in range(4):
                ar_invoice(w, cust, f"{cust}-I{i}", 20 + i, 5, amount="1000.00")
                ar_invoice(w, cust, f"{cust}-P{i}", 150 + 40 * i, 120, amount="1000.00")
            for i in range(recent_credits):
                doc(w, AR, {"idcust": cust, "idinvc": f"{cust}-CN{i}", "dateinvc": sage(10 + i),
                            "trxtypetxt": "3", "codecurn": "CAD", "amtinvctc": "-300.00"})
            for i in range(prior_credits):
                steady.append(doc(w, AR, {"idcust": cust, "idinvc": f"{cust}-OC{i}",
                                          "dateinvc": sage(200 + i), "trxtypetxt": "3",
                                          "codecurn": "CAD", "amtinvctc": "-300.00"}))
        for i in range(2):                                   # new: no year before to judge
            ar_invoice(w, "SYNTHC4", f"C4-I{i}", 20 + i, 5, amount="1000.00")
            doc(w, AR, {"idcust": "SYNTHC4", "idinvc": f"C4-CN{i}", "dateinvc": sage(10 + i),
                        "trxtypetxt": "3", "codecurn": "CAD", "amtinvctc": "-300.00"})
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pc.CREDITS_RULE], "complete")
        (gap,) = self.kinds(pc.CREDITS_RISING)
        self.assertEqual(gap["event"]["party"], {"system": "sage", "code": "SYNTHC1"})
        self.assertEqual(gap["summary"],
                         "Customer SYNTHC1 (sage) got 2 credit notes worth 600.00 CAD in the "
                         "last 90 days: 15.0% of the 4000.00 CAD invoiced, against 0.0% in "
                         "the 365 days before")
        self.assertEqual(gap["event"]["amount"]["value"], "600.00")
        self.assertEqual(len(gap["citations"]), 10)   # 2 credits and 8 invoices summed
        # a reader without finance, or who may not open the Sage documents, sees nothing
        self.assertEqual(self.kinds(pc.CREDITS_RISING, held=frozenset()), [])
        self.assertEqual(self.kinds(pc.CREDITS_RISING, may_read=lambda a: a != AR[2]), [])

    def test_a_bc_credit_memo_counts_as_a_credit_and_one_without_a_currency_is_skipped(self):
        w = World()
        for i in range(4):
            w.obj("SalesInvoice", {"customer_number": "SYNTHB1", "invoice_number": f"SI-{i}",
                                   "invoice_date": str(AS_OF - timedelta(days=20 + i)),
                                   "total_amount": "1000.00", "remaining_amount": "0",
                                   "currency": "USD", "status": "Paid"},
                  binding="bc.salesInvoices", audience="api:bc.sales", markings=FIN)
            w.obj("SalesInvoice", {"customer_number": "SYNTHB1", "invoice_number": f"SP-{i}",
                                   "invoice_date": str(AS_OF - timedelta(days=200 + i)),
                                   "total_amount": "1000.00", "remaining_amount": "0",
                                   "currency": "USD", "status": "Paid"},
                  binding="bc.salesInvoices", audience="api:bc.sales", markings=FIN)
        for i, cur in enumerate(("USD", "USD", "")):
            w.obj("SalesCreditMemo", {"customer_number": "SYNTHB1", "bc_number": f"CM-{i}",
                                      "credit_memo_date": str(AS_OF - timedelta(days=5 + i)),
                                      "total_amount": "500.00", "status": "Paid",
                                      **({"currency_code": cur} if cur else {})},
                  binding="bc.salesCreditMemos", audience="api:bc.sales", markings=FIN)
        summary = self.run_rules(w)
        (gap,) = self.kinds(pc.CREDITS_RISING)
        self.assertIn("got 2 credit notes worth 1000.00 USD", gap["summary"])
        self.assertEqual(gap["object_type"], "SalesCreditMemo")
        self.assertEqual(summary["detectors"][pc.CREDITS_RULE], "complete")

    def test_each_check_says_its_label_and_last_run_time(self):
        w = World()
        slow_customer(w, "SYNTHC1")
        self.run_rules(w)
        checks = {c["check"]: c for c in briefing._checks(self.read())}
        for rule, label in ((pc.PAYMENT_RULE, "customers paying more slowly"),
                            (pc.DISCOUNT_RULE, "early-pay discounts missed"),
                            (pc.DUPLICATE_RULE,
                             "supplier invoices and payments entered twice")):
            with self.subTest(rule=rule):
                self.assertEqual(checks[rule]["label"], label)
                self.assertTrue(checks[rule]["last_run"])
                self.assertIn(" ET)", checks[rule]["words"])
        from operating import vendor_watch
        from operating import declared_inputs
        self.assertTrue(set(pc.RULES) <= vendor_watch.WHOLE_PASS)
        self.assertEqual(vendor_watch.WHOLE_PASS, set(declared_inputs.NOT_INCREMENTAL)
                         - {"vendor_no_reply", "order_without_customer_po"})
        self.assertEqual(briefing.at_words("2026-10-19T14:05:00Z"), "2026-10-19 10:05 AM ET")

    def test_every_new_kind_has_a_next_step_and_its_words(self):
        for kind in pc.KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, briefing.EVENT_TYPES)
                self.assertIn(kind, briefing.NEXT_ACTIONS)
                self.assertIn(kind, briefing.TODAY_WORDS)
        self.assertTrue(set(pc.RULES) <= set(pe.DETECTORS))
        self.assertTrue(set(pc.RULES) <= set(pe.DETECTOR_SECONDS))

    def test_every_customer_and_pair_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                for i in range(n):
                    slow_customer(w, f"SYNTHC{i}")
                    ap_invoice(w, f"SYNTHV{i}", f"INV-{10000 + i}", 40, paid=10)
                    ap_invoice(w, f"SYNTHV{i}", f"{10000 + i}", 30, paid=5)
                    for j in range(4):
                        ar_invoice(w, f"SYNTHR{i}", f"R{i}-I{j}", 20 + j, 5, amount="1000.00")
                        ar_invoice(w, f"SYNTHR{i}", f"R{i}-P{j}", 150 + j, 120,
                                   amount="1000.00")
                    for j in range(2):
                        doc(w, AR, {"idcust": f"SYNTHR{i}", "idinvc": f"R{i}-CN{j}",
                                    "dateinvc": sage(10 + j), "trxtypetxt": "3",
                                    "codecurn": "CAD", "amtinvctc": "-300.00"})
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                for rule in pc.RULES:
                    self.assertEqual(summary["detectors"][rule], "complete")
                self.assertEqual(len(self.kinds(pc.SLOWER)), n)
                self.assertEqual(len({g["event"]["party"]["code"]
                                      for g in self.kinds(pc.SLOWER)}), n)
                self.assertEqual(len(self.kinds(pc.DUP_INVOICE)), n)
                credits = self.kinds(pc.CREDITS_RISING)
                self.assertEqual(len({g["event"]["party"]["code"] for g in credits}), n)
                self.assertEqual(len(credits), n)
                self.inbox.unlink()
                self.store.unlink()


class Forecast(Engine):

    def book(self, w: World) -> dict:
        w.build(self.store)
        return cf.read_book(str(self.store), AS_OF)

    def world(self) -> World:
        w = World()
        # a customer who pays 14 days late: its open invoice due in 3 days lands in week 3
        for i in range(3):
            ar_invoice(w, "SYNTHC1", f"H{i}", 100 + i, 56 + i, due=70 + i)
        ar_invoice(w, "SYNTHC1", "OPEN-1", 27, None, due=-3, amount="1000.00")
        ar_invoice(w, "SYNTHC2", "OPEN-2", 20, None, due=-8, amount="250.00",
                   currency="USD")
        ar_invoice(w, "SYNTHC2", "OLD-1", 200, None, due=170, amount="40.00")  # past expected
        ap_invoice(w, "SYNTHV1", "B-1", 10, due=-1, amount="300.00")
        ap_invoice(w, "SYNTHV1", "B-2", 10, due=-20, amount="200.00", po="PO-9")
        order = w.obj("PurchaseOrder", {"status": "Pending", "po_number": "PO-9",
                                        "expected_date": (AS_OF + timedelta(days=30))
                                        .isoformat(), "total_cost": "900.00",
                                        "currency": "CAD"}, markings=FIN)
        line = w.obj("PurchaseOrderLine", {"ordered_quantity": ("10", "ea"),
                                           "received_quantity": ("0", "ea", "received")})
        w.link("PurchaseOrderLine.line_of", line, order)
        return w

    def test_weeks_sum_cited_amounts_per_currency_with_customer_lateness(self):
        book = self.book(self.world())
        got = cf.answer(book, AS_OF, may_read=lambda _a: True, held=frozenset({"finance"}))
        self.assertTrue(got["complete"])
        week = {w["week"]: {c["currency"]: c for c in w["by_currency"]} for w in got["weeks"]}
        self.assertEqual(week[3]["CAD"]["inflow"], "1000.00")       # due +3, 14 days late
        self.assertEqual(week[2]["USD"]["inflow"], "250.00")        # no history: due date
        self.assertEqual(week[1]["CAD"]["outflow"], "300.00")
        self.assertEqual(week[3]["CAD"]["outflow"], "200.00")
        self.assertEqual(week[5]["CAD"]["outflow"], "700.00")       # PO less its invoice
        self.assertEqual(week[5]["CAD"]["outflow_purchase_orders"], "700.00")
        totals = {t["currency"]: t for t in got["totals"]}
        self.assertEqual(totals["CAD"]["past_expected_inflow"], "40.00")
        for code, total in totals.items():   # the totals are the weeks' sums
            inflow = sum(Decimal(c[code]["inflow"]) for c in week.values() if code in c)
            self.assertEqual(Decimal(total["inflow"]), inflow)
        lateness = next(e for e in got["largest"] if e["number"] == "OPEN-1")
        self.assertEqual(lateness["basis"], "due_date_plus_customer_lateness")
        tool = cf.CashForecastTool.__new__(cf.CashForecastTool)
        self.assertEqual(tool._checked(got), got)                    # the contract holds

    def test_a_restricted_reader_counts_only_what_it_may_read(self):
        book = self.book(self.world())
        no_ap = cf.answer(book, AS_OF, may_read=lambda a: a != AP[2],
                          held=frozenset({"finance"}))
        self.assertFalse(no_ap["complete"])
        self.assertNotIn("not_counted", no_ap)
        outflow = [c["outflow"] for w in no_ap["weeks"] for c in w["by_currency"]
                   if c["outflow"] != "0.00"]
        # the open PO's remainder rests on the invoice against it: unseen, so not counted
        self.assertEqual(outflow, [])
        self.assertEqual([], [e for e in no_ap["largest"] if e["source"] == "payable"])
        hidden = cf.answer(book, AS_OF, may_read=lambda _a: True, held=frozenset())
        self.assertEqual(hidden["error"]["code"], "finance_not_held")
        nothing = cf.answer(book, AS_OF, may_read=lambda _a: False,
                            held=frozenset({"finance"}))
        self.assertEqual(nothing["totals"], [])

    def test_an_entry_without_a_known_audience_or_marking_fails_closed(self):
        book = self.book(self.world())
        entry = book["entries"][0]
        self.assertTrue(cf.visible(entry, book["audiences"], lambda _a: True,
                                   frozenset({"finance"})))
        self.assertFalse(cf.visible(entry, {}, lambda _a: True, frozenset({"finance"})))
        broken = pc.Entry(**{**entry.__dict__, "handles": [("h", "o", "not json")]})
        self.assertFalse(cf.visible(broken, book["audiences"], lambda _a: True,
                                    frozenset({"finance"})))

    def test_the_forecast_is_a_mode_of_plx_aggregate_for_a_finance_reader_only(self):
        import datetime as dt
        import sqlite3
        from types import SimpleNamespace

        self.world().build(self.store)
        held = {"fin": frozenset({"finance"}), "ops": frozenset()}
        base = SimpleNamespace(
            store=SimpleNamespace(conn=sqlite3.connect(self.store)),
            policy=SimpleNamespace(held_markings=lambda actor: held[actor],
                                   may_read=lambda actor, key: True),
            _as_known_at=lambda _a: dt.datetime(2026, 10, 19, 6), _store_meta=lambda: {})
        self.addCleanup(base.store.conn.close)
        table = {"plx.aggregate": {"name": "plx.aggregate",
                                   "handler": lambda arguments, actor: {"groups": []}}}
        cf.register(table, base)
        call = table["plx.aggregate"]["handler"]
        self.assertEqual(call({"measure": {"op": "count"}, "object_type": "X"}, "fin"),
                         {"groups": []})                         # plx.aggregate as before
        self.assertEqual(call({"mode": "cash_forecast"}, "ops")["error"]["code"],
                         "finance_not_held")
        self.assertEqual(call({"mode": "what_if"}, "fin")["error"]["code"], "invalid_input")
        tool = cf.CashForecastTool(base, wait=120.0)
        got = tool.call({"mode": "cash_forecast", "measure": {"op": "count"},
                         "object_type": "Payment"}, "fin")      # its own fields: ignored
        self.assertEqual(got["mode"], "cash_forecast")
        self.assertTrue(got["complete"])


if __name__ == "__main__":
    unittest.main()
