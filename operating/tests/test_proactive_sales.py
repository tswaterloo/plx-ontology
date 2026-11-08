"""Sales alerts and figures (``operating.proactive_sales``): the stale-quote alert through the
engine, the per-reader inbox and the briefing; the quote-to-order conversion rate and the
cross-sell suggestions as modes of ``plx.aggregate``, for a finance reader, one without the
marking and one who may not read part of the records.

SYNTHETIC fixtures only (``World`` stores, synthetic codes); evidence label: isolated-fixture.
"""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
import sqlite3
import time
import unittest
from types import SimpleNamespace

from operating import briefing
from operating import proactive_sales as sales
from operating.tests.test_proactive_business import NO_FINANCE, money
from operating.tests.test_proactive_engine import AS_OF, Engine, World

FIN = frozenset({"finance"})


def ago(days: int) -> str:
    return (AS_OF - timedelta(days=days)).isoformat()


def customer(w: World, name: str = "SYNTH Buyer", audience: str = "api:customers") -> str:
    return w.obj("Customer", {"name": name}, audience=audience)


def quote(w: World, who: str, status: str, days: int, *, value: str | None = "100.00",
          currency: str = "CAD", number: str | None = None, order: str | None = None,
          order_tier: str = "verified", audience: str = "api:quotes", **extra) -> str:
    number = number or f"QUO-SYN-{w.n + 1}"
    oid = w.obj("Quote", {"status": status, "issue_date": ago(days), "quote_number": number,
                          **extra}, audience=audience)
    if value is not None:
        base = next(r for r in w.rows["object_values"]
                    if r["object_id"] == oid and r["property"] == "status")
        w.rows["object_values"].append(dict(base, property="subtotal", value=value,
                                            markings='["finance"]'))
        money(w, oid, "subtotal", currency)
    w.link("Quote.for_customer", oid, who)
    if order:
        w.link("Quote.converted_to", oid, order, order_tier)
    return oid


def sales_order(w: World) -> str:
    return w.obj("SalesOrder", {"status": "Open"}, audience="api:portal")


def bc_quote(w: World, status: str, days: int, number: str, *, external: str | None = None) -> str:
    return w.obj("RelBcSalesQuotes", {
        "status": status, "sent_date": ago(days), "number": number, "customer_name": "SYNTH BC Buyer",
        **({"external_document_number": external} if external else {})}, audience="bc:quotes")


class StaleQuotes(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=[sales.STALE_RULE], **options)

    def kinds(self, **read) -> list[dict]:
        return self.read(kinds=[sales.STALE], limit=1000, **read)["gaps"]

    def test_an_open_quote_past_its_days_with_no_order_is_raised_with_its_value(self):
        w = World()
        buyer = customer(w)
        medium = quote(w, buyer, "SENT", 45, number="QUO-SYN-M", valid_until=ago(5))
        high = quote(w, buyer, "NEGOTIATION", 75, value="2500.50", number="QUO-SYN-H")
        young = quote(w, buyer, "SENT", 10)
        converted = quote(w, buyer, "SENT", 90, order=sales_order(w))
        numbered = quote(w, buyer, "SENT", 90, converted_sales_order_number="SO-SYN-1")
        quote(w, buyer, "ACCEPTED", 90)          # accepted_quote_no_order judges it
        quote(w, buyer, "REJECTED", 90)
        quote(w, buyer, "DRAFT", 90)
        odd = quote(w, buyer, "PARKED", 90)      # a word no rule maps: not judged, counted
        lead = quote(w, buyer, "SENT", 90, order=sales_order(w), order_tier="lead")
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][sales.STALE_RULE], "complete")
        gaps = {g["object_id"]: g for g in self.kinds()}
        self.assertEqual(set(gaps), {medium, high})
        for skipped in (young, converted, numbered, odd, lead):
            self.assertNotIn(skipped, gaps)
        self.assertEqual(gaps[medium]["summary"],
                         f"Quote QUO-SYN-M to SYNTH Buyer was issued {ago(45)} (45 days ago) "
                         f"and is still sent, with no order converted from it; its offer ran out {ago(5)}")
        self.assertEqual(gaps[medium]["follow_up"]["priority"], "medium")
        self.assertEqual(gaps[high]["follow_up"]["priority"], "high")
        self.assertEqual(gaps[high]["event"]["amount"]["value"], "2500.5")
        self.assertEqual(gaps[high]["event"]["amount"]["currency"], "CAD")
        self.assertEqual(gaps[high]["event"]["days"], 75)
        self.assertEqual(summary["not_evaluated"][sales.STALE_RULE],
                         {"conversion_unsettled": 1, "status_unmapped": 1})

    def test_a_reader_without_finance_sees_the_quote_but_never_the_amount(self):
        w = World()
        stale = quote(w, customer(w), "SENT", 50)
        self.run_rules(w)
        got = {g["object_id"]: g for g in self.kinds(held=NO_FINANCE)}
        self.assertIn(stale, got)
        self.assertNotIn("amount", got[stale].get("event") or {})
        self.assertNotIn("100", got[stale]["summary"])
        full = {g["object_id"]: g for g in self.kinds()}
        self.assertEqual(full[stale]["event"]["amount"]["value"], "100")

    def test_the_claim_rests_on_no_order_so_a_reader_not_granted_orders_gets_nothing(self):
        w = World()
        sales_order(w)
        quote(w, customer(w), "SENT", 50)
        self.run_rules(w)
        self.assertEqual(len(self.kinds()), 1)
        self.assertEqual(self.kinds(may_read=lambda k: k != "api:portal"), [])
        self.assertEqual(self.kinds(may_read=lambda k: k != "api:quotes"), [])

    def test_a_business_central_quote_is_raised_and_a_copy_of_a_portal_quote_counts_once(self):
        w = World()
        quote(w, customer(w), "SENT", 50, number="QUO-SYN-DUP")
        native = bc_quote(w, "Sent", 41, "SQ-SYN-1")
        bc_quote(w, "Sent", 50, "SQ-SYN-2", external="quo-syn-dup")
        bc_quote(w, "Draft", 90, "SQ-SYN-3")
        self.run_rules(w)
        got = {g["object_id"]: g for g in self.kinds()}
        self.assertEqual(len(got), 2)
        self.assertIn(native, got)
        self.assertEqual(got[native]["object_type"], "RelBcSalesQuotes")
        self.assertIn("SYNTH BC Buyer", got[native]["summary"])

    def test_one_ten_and_sixty_quotes_each_raise_exactly_one_alert_in_budget(self):
        for n in (1, 10, 60):
            with self.subTest(quotes=n):
                self.tearDown()
                self.setUp()
                w = World()
                buyers = [customer(w, f"SYNTH Buyer {i}") for i in range(3)]
                made = {quote(w, buyers[i % 3], "SENT", 31 + i, number=f"QUO-SYN-{i}")
                        for i in range(n)}
                started = time.monotonic()
                self.run_rules(w)
                got = [g["object_id"] for g in self.kinds()]
                self.assertEqual(sorted(got), sorted(made))
                self.assertLess(time.monotonic() - started, 30)

    def test_the_briefing_names_the_follow_up_step_and_a_one_line_wording(self):
        spec = briefing.EVENT_TYPES["stale_quote"]
        self.assertIn("SALES", spec["departments"])
        text, playbook = briefing.NEXT_ACTIONS["stale_quote"]
        self.assertTrue(text.startswith("Follow up"))
        self.assertEqual(playbook, "quote_follow_up")
        self.assertEqual(briefing.CHECK_WORDS["stale_quote"], "open quotes with no order")
        self.assertEqual(briefing.TODAY_WORDS["stale_quote"][2], "went stale with no order")


# --------------------------------------------------------------------------- the modes

def purchase(w: World, who: str, product: str, days: int = 30, status: str | None = "Open"
             ) -> None:
    """One posted invoice line; ``status=None``: the Sage invoice history's shape (a number
    and a date, no status word)."""
    invoice = w.obj("SalesInvoice", {"invoice_date": ago(days),
                                     **({"status": status} if status else
                                        {"invoice_number": f"SYN-INV-{w.n + 1}"})},
                    audience="api:invoices")
    line = w.obj("SalesInvoiceLine", {"invoiced_quantity": ("5", "kg")}, audience="api:invoices")
    w.link("SalesInvoice.billed_to", invoice, who)
    w.link("SalesInvoiceLine.line_of", line, invoice)
    w.link("SalesInvoiceLine.for_item", line, product)


def product(w: World, name: str) -> str:
    return w.obj("Product", {"name": name, "code": name}, audience="api:items")


class Modes(Engine):

    def book(self, w: World) -> dict:
        w.build(self.store)
        return sales.read_book(str(self.store), AS_OF)

    def conversion(self, book: dict, *, held=FIN, may_read=lambda _k: True) -> dict:
        return sales.quote_conversion(book, AS_OF, may_read=may_read, held=held)

    def conversion_world(self, per_customer: int = 1) -> tuple[World, dict]:
        w = World()
        ids = {}
        for name in ("SYNTH Alpha", "SYNTH Beta"):
            who = ids[name] = customer(w, name)
            for _ in range(per_customer):
                quote(w, who, "ACCEPTED", 100, order=sales_order(w), value="100.00")
                quote(w, who, "REJECTED", 90, value="50.00")
        quote(w, ids["SYNTH Alpha"], "SENT", 70, value="10.00")      # old open: in the rate
        quote(w, ids["SYNTH Alpha"], "SENT", 5, value="999.00")      # young: out of the rate
        quote(w, ids["SYNTH Beta"], "DRAFT", 5)                      # no offer
        quote(w, ids["SYNTH Beta"], "SENT", 800)                     # outside the window
        return w, ids

    def test_the_rate_counts_converted_over_decided_and_old_open_quotes(self):
        w, ids = self.conversion_world()
        got = self.conversion(self.book(w))
        self.assertEqual({k: got["overall"][k] for k in ("quotes", "converted", "lost", "open",
                                                          "young", "rate_pct")},
                         {"quotes": 6, "converted": 2, "lost": 2, "open": 1, "young": 1,
                          "rate_pct": "40.0"})
        by = {c["label"]["untrusted_text"]: c for c in got["by_customer"]}
        self.assertEqual(by["SYNTH Alpha"]["rate_pct"], "33.3")
        self.assertEqual(by["SYNTH Beta"]["rate_pct"], "50.0")
        self.assertEqual(sum(c["quotes"] for c in got["by_customer"]), got["overall"]["quotes"])
        self.assertEqual(sum(p["quotes"] for p in got["by_period"]), got["overall"]["quotes"])
        self.assertEqual(got["by_rep"]["state"], "not_served")
        self.assertEqual(got["value"][0]["converted"], "200.00")
        self.assertEqual(got["value"][0]["quoted"], "1309.00")
        tool = sales.SalesTool.__new__(sales.SalesTool)
        self.assertEqual(tool._checked(got), got)                    # the contract holds

    def test_a_reader_without_finance_gets_counts_and_rates_but_no_money(self):
        got = self.conversion(self.book(self.conversion_world()[0]), held=NO_FINANCE)
        self.assertEqual(got["overall"]["rate_pct"], "40.0")
        self.assertNotIn("value", got)
        self.assertNotIn("1309", str(got))

    def test_a_reader_who_may_not_read_a_customers_quotes_counts_only_the_rest(self):
        w = World()
        shown, hidden = customer(w, "SYNTH Shown"), customer(w, "SYNTH Hidden")
        quote(w, shown, "ACCEPTED", 100, order=sales_order(w))
        quote(w, shown, "REJECTED", 100)
        for _ in range(3):
            quote(w, hidden, "ACCEPTED", 100, order=sales_order(w), audience="api:secret")
        book = self.book(w)
        full = self.conversion(book)
        self.assertEqual((full["overall"]["quotes"], full["overall"]["rate_pct"], full["complete"]),
                         (5, "80.0", True))
        part = self.conversion(book, may_read=lambda k: k != "api:secret")
        self.assertEqual((part["overall"]["quotes"], part["overall"]["rate_pct"], part["complete"]),
                         (2, "50.0", False))
        self.assertNotIn("SYNTH Hidden", str(part))
        self.assertNotIn(hidden, str(part))
        # a quote whose order the reader may not read is not counted for it either
        w2 = World()
        who = customer(w2)
        order = w2.obj("SalesOrder", {"status": "Open"}, audience="api:secret")
        quote(w2, who, "ACCEPTED", 100, order=order)
        none = self.conversion(self.book(w2), may_read=lambda k: k != "api:secret")
        self.assertEqual(none["overall"]["quotes"], 0)
        self.assertIsNone(none["overall"]["rate_pct"])

    def test_one_ten_and_sixty_quotes_are_each_counted_once_in_every_cut(self):
        for n in (1, 10, 60):
            with self.subTest(quotes=n):
                self.tearDown()
                self.setUp()
                w = World()
                buyers = [customer(w, f"SYNTH Buyer {i}") for i in range(4)]
                for i in range(n):
                    if i % 3 == 0:
                        quote(w, buyers[i % 4], "ACCEPTED", 100, order=sales_order(w))
                    else:
                        quote(w, buyers[i % 4], "REJECTED", 100 + i % 5)
                started = time.monotonic()
                got = self.conversion(self.book(w))
                self.assertEqual(got["overall"]["quotes"], n)
                self.assertEqual(sum(c["quotes"] for c in got["by_customer"]), n)
                self.assertEqual(sum(p["quotes"] for p in got["by_period"]), n)
                self.assertEqual(got["overall"]["converted"], (n + 2) // 3)
                self.assertLess(time.monotonic() - started, 30)

    def sell_world(self, n_customers: int = 12) -> tuple[World, dict]:
        w = World()
        items = {name: product(w, name) for name in ("SYNTH Base", "SYNTH Pair", "SYNTH Solo",
                                                      "SYNTH Rare")}
        target = customer(w, "SYNTH Target")
        purchase(w, target, items["SYNTH Base"])
        for i in range(n_customers):
            who = customer(w, f"SYNTH Peer {i}")
            purchase(w, who, items["SYNTH Base"])
            if i < 8:
                purchase(w, who, items["SYNTH Pair"], 20 + i)
            if i < 2:
                purchase(w, who, items["SYNTH Rare"])        # too few buyers of both
            if i % 2:
                purchase(w, who, items["SYNTH Solo"], 400)   # outside the window
        return w, {"target": target, **items}

    def test_customers_like_this_also_buy_with_support_and_confidence_for_finance(self):
        w, ids = self.sell_world()
        got = sales.cross_sell(self.book(w), AS_OF, ids["target"], may_read=lambda _k: True,
                               held=FIN)
        (pair,) = got["suggestions"]
        self.assertEqual(pair["object_id"], ids["SYNTH Pair"])
        self.assertEqual(pair["label"]["untrusted_text"], "SYNTH Pair")
        self.assertEqual((pair["customers_buying_both"], pair["customers_buying_base"],
                          pair["confidence_pct"]), (8, 12, "66.7"))
        self.assertEqual([b["object_id"] for b in pair["because"]], [ids["SYNTH Base"]])
        tool = sales.SalesTool.__new__(sales.SalesTool)
        self.assertEqual(tool._checked(got), got)

    def test_a_reader_without_finance_gets_the_items_and_no_figures(self):
        w, ids = self.sell_world()
        got = sales.cross_sell(self.book(w), AS_OF, ids["target"], may_read=lambda _k: True,
                               held=NO_FINANCE)
        (pair,) = got["suggestions"]
        self.assertEqual(set(pair), {"object_id", "label", "because"})
        self.assertNotIn("66", str(got))

    def test_a_restricted_reader_counts_only_the_customers_it_may_read(self):
        w, ids = self.sell_world()
        book = self.book(w)
        # peers it may not read leave too few buyers of the base item: nothing is suggested
        names = {r["object_id"]: r["value"] for r in w.rows["object_values"]
                 if r["property"] == "name"}
        blocked = {o for o, n in names.items() if n.startswith("SYNTH Peer ") and
                   int(n.rsplit(" ", 1)[1]) >= 4}
        audiences = {o: ("api:secret" if o in blocked else k)
                     for o, k in book["audiences"].items()}
        part = sales.cross_sell({**book, "audiences": audiences}, AS_OF, ids["target"],
                                may_read=lambda k: k != "api:secret", held=FIN)
        self.assertEqual(part["suggestions"], [])
        self.assertFalse(part["complete"])
        self.assertNotIn("SYNTH Peer 9", str(part))

    def test_an_invoice_with_no_status_word_counts_when_numbered_and_a_draft_never_does(self):
        w = World()
        base, pair = product(w, "SYNTH Base"), product(w, "SYNTH Pair")
        target = customer(w, "SYNTH Target")
        purchase(w, target, base, status=None)
        for i in range(6):
            who = customer(w, f"SYNTH Peer {i}")
            purchase(w, who, base, status=None)
            purchase(w, who, pair, status=None if i < 2 else "Draft")
        got = sales.cross_sell(self.book(w), AS_OF, target, may_read=lambda _k: True, held=FIN)
        self.assertEqual(got["suggestions"], [])           # 2 of 6 buy both, drafts left out
        seventh = customer(w, "SYNTH Peer 7")
        purchase(w, seventh, base, status=None)
        purchase(w, seventh, pair, status=None)
        got = sales.cross_sell(self.book(w), AS_OF, target, may_read=lambda _k: True, held=FIN)
        self.assertEqual((got["suggestions"][0]["customers_buying_both"],
                          got["suggestions"][0]["customers_buying_base"]), (3, 7))

    def test_small_baskets_posted_only_and_one_to_sixty_customers(self):
        for n in (1, 10, 60):
            with self.subTest(customers=n):
                self.tearDown()
                self.setUp()
                w, ids = self.sell_world(n)
                purchase(w, customer(w, "SYNTH Cancelled"), ids["SYNTH Rare"], 5,
                         status="Cancelled")
                started = time.monotonic()
                got = sales.cross_sell(self.book(w), AS_OF, ids["target"],
                                       may_read=lambda _k: True, held=FIN)
                wanted = n >= 5 and min(8, n) >= sales.MIN_BOTH
                self.assertEqual(len(got["suggestions"]), 1 if wanted and min(8, n) / n >= .3
                                 else 0)
                self.assertNotIn(ids["SYNTH Solo"], str(got))
                self.assertLess(time.monotonic() - started, 30)

    def test_the_modes_are_served_by_plx_aggregate_and_fail_closed(self):
        w, ids = self.sell_world()
        quote(w, ids["target"], "REJECTED", 40)
        w.build(self.store)
        held = {"fin": FIN, "ops": frozenset()}
        base = SimpleNamespace(
            store=SimpleNamespace(conn=sqlite3.connect(self.store)),
            policy=SimpleNamespace(held_markings=lambda actor: held[actor],
                                   may_read=lambda actor, key: key != "api:customers"
                                   or actor == "fin"),
            _as_known_at=lambda _a: dt.datetime(2026, 10, 19, 6), _store_meta=lambda: {})
        self.addCleanup(base.store.conn.close)
        table = {"plx.aggregate": {"name": "plx.aggregate",
                                   "handler": lambda arguments, actor: {"groups": []}}}
        sales.register(table, base)
        call = table["plx.aggregate"]["handler"]
        self.assertEqual(call({"measure": {"op": "count"}, "object_type": "X"}, "fin"),
                         {"groups": []})                         # plx.aggregate as before
        tool = sales.SalesTool(base, wait=120.0)
        rate = tool.call({"mode": "quote_conversion"}, "fin")
        self.assertEqual(rate["mode"], "quote_conversion")
        self.assertEqual(rate["overall"]["quotes"], 1)
        self.assertEqual(tool.call({"mode": "cross_sell"}, "fin")["error"]["code"],
                         "invalid_input")
        self.assertEqual(tool.call({"mode": "cross_sell", "customer": "Customer:fx:nope"},
                                   "fin")["error"]["code"], "not_found")
        self.assertEqual(tool.call({"mode": "cross_sell", "customer": ids["target"]},
                                   "ops")["error"]["code"], "not_found")   # may not read it
        got = tool.call({"mode": "cross_sell", "customer": ids["target"]}, "fin")
        self.assertEqual(len(got["suggestions"]), 1)
        self.assertEqual(tool.call({"mode": "what_if"}, "fin")["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
