"""What each customer pays for the same product (``operating.proactive_prices``) through
the engine, the per-reader inbox and the briefing.

SYNTHETIC fixtures only (``World`` stores, synthetic codes); evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
import time
import unittest

from operating import briefing
from operating import declared_inputs
from operating import proactive_engine as pe
from operating import proactive_prices as pp
from operating import vendor_watch
from operating.tests.test_proactive_business import NO_FINANCE, cost_line, money
from operating.tests.test_proactive_engine import AS_OF, Engine, World

FIN = '["finance"]'


def day(offset: int) -> str:
    return (AS_OF - timedelta(days=offset)).isoformat()


def customer(w: World, code: str, *, audience: str = "bc:customers") -> str:
    return w.obj("Customer", {"erp_code": code, "name": f"SYNTH {code}"},
                 binding="bc.customers", audience=audience)


def sale(w: World, who: str, product: str, qty: str, amount: str, *, ago: int = 30,
         unit: str = "kg", currency: str = "CAD", status: str = "Open",
         audience: str = "bc:invoices", own_cost: tuple[str, str, str] | None = None) -> str:
    """A posted invoice billed to ``who`` with one line of ``product``; ``own_cost``:
    (unit cost, unit conversion, cost unit conversion) the line's system booked."""
    inv = w.obj("SalesInvoice", {"status": status, "posting_date": day(ago)},
                binding="bc.sales_invoices", audience=audience, markings=FIN)
    values = {"invoiced_quantity": (qty, unit, "invoiced"), "amount_excluding_tax": amount}
    if own_cost:
        values.update(unit_cost=own_cost[0], unit_conversion=own_cost[1],
                      cost_unit_conversion=own_cost[2])
    line = w.obj("SalesInvoiceLine", values, binding="bc.sales_invoice_lines",
                 audience=audience, markings=FIN)
    money(w, line, "amount_excluding_tax", currency)
    if own_cost:
        money(w, line, "unit_cost", currency)
    w.link("SalesInvoiceLine.line_of", line, inv)
    w.link("SalesInvoiceLine.for_item", line, product)
    w.link("SalesInvoice.billed_to", inv, who)
    return inv


def market(w: World, product: str, n: int, *, price: int = 10, audience: str = "bc:invoices"
           ) -> list[str]:
    """``n`` customers each paying ``price`` CAD/kg (two lines each)."""
    out = []
    for i in range(n):
        who = customer(w, f"SYNTHC{i:03d}")
        sale(w, who, product, "10", str(price * 10), ago=20, audience=audience)
        sale(w, who, product, "5", str(price * 5), ago=50, audience=audience)
        out.append(who)
    return out


class PriceAlerts(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=[pp.RULE], **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    def test_a_customer_far_below_the_typical_price_is_raised_with_every_line_cited(self):
        w = World()
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        others = market(w, p, 4)
        cheap = customer(w, "SYNTHLOW")
        sale(w, cheap, p, "10", "70")                 # 7 CAD/kg: 30% below the typical 10
        near = customer(w, "SYNTHNEAR")
        sale(w, near, p, "10", "90")                  # 10% below: within the band
        sale(w, cheap, p, "10", "100", ago=400)       # older than both windows: not counted
        sale(w, cheap, p, "10", "10", status="Draft")  # not posted
        sale(w, cheap, p, "10", "50", currency="USD")  # another currency: its own series
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pp.RULE], "complete")
        (gap,) = self.kinds(pp.BELOW_TYPICAL_KIND)
        self.assertEqual(gap["object_id"], cheap)
        self.assertIn("Price 30% below typical: 7 CAD/kg paid for a product against a "
                      "typical 10 CAD/kg (volume-weighted, 5 other customers) in the last "
                      "180 days", gap["summary"])
        self.assertIn("(SYNTH SYNTHLOW)", gap["summary"])
        self.assertEqual(gap["event"]["amount"], {"value": "30.00", "currency": "CAD",
                                                  "basis": "below_typical"})
        self.assertEqual(gap["follow_up"]["priority"], "high")
        # its own line and the ten lines of the other five customers, each with its invoice
        self.assertEqual(len(gap["citations"]), 2 * (1 + 2 * len(others) + 1))
        self.assertEqual(self.kinds(pp.BELOW_COST_KIND), [])

    def test_a_price_below_the_booked_or_purchase_cost_is_raised_once(self):
        w = World()
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        market(w, p, 3)
        sage = customer(w, "SYNTHSAGE")
        sale(w, sage, p, "10", "95", own_cost=("12", "1", "1"))   # 9.5 against 12 booked
        bought = w.obj("Product", {"code": "SYNTH-BOUGHT"}, audience="api:items")
        cost_line(w, bought, "8", day(90))
        cost_line(w, bought, "11", day(400))          # older and higher: not the latest
        buyer = customer(w, "SYNTHBUY")
        sale(w, buyer, bought, "4", "30")             # 7.5 against the latest purchase 8
        fine = customer(w, "SYNTHFINE")
        sale(w, fine, bought, "4", "40")
        close = customer(w, "SYNTHCLOSE")
        sale(w, close, bought, "10", "78")            # 7.8: within 5% of the cost 8
        free = customer(w, "SYNTHFREE")
        sale(w, free, bought, "4", "0")               # a free sample: no price paid
        self.last_summary = self.run_rules(w)
        got = {g["object_id"]: g for g in self.kinds(pp.BELOW_COST_KIND)}
        self.assertEqual(set(got), {sage, buyer})
        self.assertIn("Price below cost: 9.5 CAD/kg paid for a product on 1 invoice line in "
                      "the last 180 days against a cost of 12 CAD/kg", got[sage]["summary"])
        self.assertEqual(got[buyer]["event"]["floor"], "8")
        self.assertEqual(got[buyer]["event"]["amount"]["value"], "2.00")
        summary = self.last_summary
        self.assertEqual(summary["not_evaluated"][pp.RULE].get("free_line"), 1)
        # 9.5 is 5% below the typical 10: below cost wins, never two alerts for one series
        self.assertEqual([g for g in self.kinds(pp.BELOW_TYPICAL_KIND)
                          if g["object_id"] == sage], [])

    def test_a_customer_whose_own_price_dropped_is_raised_citing_both_windows(self):
        w = World()
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        fell = customer(w, "SYNTHFELL")
        sale(w, fell, p, "10", "100", ago=250)        # 10 CAD/kg in the window before
        sale(w, fell, p, "10", "85", ago=20)          # 8.5 now: down 15%
        held = customer(w, "SYNTHHELD")
        sale(w, held, p, "10", "100", ago=250)
        sale(w, held, p, "10", "95", ago=20)          # down 5%: within the band
        self.run_rules(w)
        (gap,) = self.kinds(pp.DROP_KIND)
        self.assertEqual(gap["object_id"], fell)
        self.assertIn("Price down 15%: 8.5 CAD/kg paid for a product in the last 180 days "
                      "against 10 CAD/kg in the 180 days before", gap["summary"])
        self.assertEqual(len(gap["citations"]), 4)    # each line and its invoice, both windows
        self.assertEqual(gap["event"]["amount"]["value"], "15.00")

    def test_unknown_or_unreadable_figures_fail_closed(self):
        w = World()
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        market(w, p, 3)
        odd = customer(w, "SYNTHODD")
        sale(w, odd, p, "10", "10", currency=None)     # no currency stated
        sale(w, odd, p, "0", "10")                     # no quantity
        sale(w, odd, p, "10", "n/a")                   # no amount
        orphan = sale(w, odd, p, "10", "10")
        w.rows["links"] = [l for l in w.rows["links"]
                           if not (l["link_type"] == "SalesInvoice.billed_to"
                                   and l["from_object"] == orphan)]
        summary = self.run_rules(w)
        self.assertEqual(self.kinds(pp.BELOW_TYPICAL_KIND), [])
        self.assertEqual(self.kinds(pp.BELOW_COST_KIND), [])
        skipped = summary["not_evaluated"][pp.RULE]
        self.assertEqual({k: skipped.get(k) for k in ("currency_unknown", "quantity_unreadable",
                                                      "amount_unreadable", "party_unknown")},
                         {"currency_unknown": 1, "quantity_unreadable": 1,
                          "amount_unreadable": 1, "party_unknown": 1})

    def test_two_customers_never_make_a_typical_price(self):
        w = World()
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        market(w, p, 1)
        sale(w, customer(w, "SYNTHLOW"), p, "10", "20")
        self.run_rules(w)
        self.assertEqual(self.kinds(pp.BELOW_TYPICAL_KIND), [])

    def test_a_restricted_reader_sees_only_what_it_may_open(self):
        w = World()
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        market(w, p, 3)
        market_hidden = customer(w, "SYNTHHIDDEN")
        sale(w, market_hidden, p, "10", "100", audience="bc:hidden")
        cheap = customer(w, "SYNTHLOW")
        sale(w, cheap, p, "10", "60", own_cost=("5", "1", "1"))
        under = customer(w, "SYNTHUNDER")
        sale(w, under, p, "10", "95", own_cost=("12", "1", "1"))
        self.run_rules(w)
        self.assertEqual(len(self.kinds(pp.BELOW_TYPICAL_KIND)), 1)
        self.assertEqual(len(self.kinds(pp.BELOW_COST_KIND)), 1)
        # without finance: nothing, and no figure anywhere
        found = self.read(held=NO_FINANCE)
        self.assertEqual([g for g in found["gaps"] if g["kind"] in pp.KINDS], [])
        self.assertNotIn("CAD/kg", str(found))
        # a reader who may not open one customer's invoices: the below-typical alert rests
        # on them (the median), so it is not shown; the below-cost one cites only its own
        partial = self.read(may_read=lambda key: key != "bc:hidden")
        shown = {g["kind"] for g in partial["gaps"]}
        self.assertNotIn(pp.BELOW_TYPICAL_KIND, shown)
        self.assertIn(pp.BELOW_COST_KIND, shown)
        self.assertNotIn(market_hidden, str(partial["gaps"]))

    def test_every_customer_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
                market(w, p, n)
                cheap = customer(w, "SYNTHLOW")
                sale(w, cheap, p, "10", "50")
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(summary["detectors"][pp.RULE], "complete")
                got = self.kinds(pp.BELOW_TYPICAL_KIND)
                # 1 other customer: 2 in all, no typical price; else exactly the cheap one
                self.assertEqual([g["object_id"] for g in got], [] if n < 2 else [cheap])
                self.assertEqual(len({g["id"] for g in got}), len(got))
                st = pe._Store(self.store)
                try:
                    counts = pp.price_consistency(
                        st, AS_OF, gap=pe._gap, cites=pe._cites, result=pe._result,
                        settled=pe.SETTLED, skips=pe._Skips)["counts"]
                finally:
                    st.close()
                self.assertEqual((counts["lines"], counts["series"]), (2 * n + 1, 1))
                self.setUp()

    def test_the_briefing_has_a_next_step_words_and_a_check_label(self):
        for kind in pp.KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, briefing.EVENT_TYPES)
                self.assertIn(kind, briefing.NEXT_ACTIONS)
                self.assertIn(kind, briefing.TODAY_WORDS)
        self.assertIn(pp.RULE, pe.DETECTORS)
        self.assertIn(pp.RULE, pe.DETECTOR_SECONDS)
        self.assertIn(pp.RULE, declared_inputs.NOT_INCREMENTAL)
        self.assertIn(pp.RULE, vendor_watch.WHOLE_PASS)
        w = World()
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        market(w, p, 3)
        sale(w, customer(w, "SYNTHLOW"), p, "10", "50")
        self.run_rules(w)
        checks = {c["check"]: c for c in briefing._checks(self.read())}
        self.assertEqual(checks[pp.RULE]["label"],
                         "what each customer pays for the same product")
        self.assertTrue(checks[pp.RULE]["last_run"])


if __name__ == "__main__":
    unittest.main()
