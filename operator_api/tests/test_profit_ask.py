"""The app's Ask on "is <customer> profitable, and why not?" (``backend._profit_ask``) over the
BC + Portal fixture world of ``tests.test_customer_profit``: the real ``WorkspaceBackend``
and Ask pool, a reader view over the real tools. The answer event validates against the
contract; every source is decided for the reader; a record the reader may not open is
never named. Evidence label: isolated-fixture."""

from __future__ import annotations

import contextlib
import json
import threading
from types import SimpleNamespace
import unittest

from operator_api import backend as be
from operator_api import models as m
from tests.test_customer_profit import TODAY, World, bc_records


class View:
    """The reader's workspace as the backend uses it: tool calls, the per-item decision
    (``withheld``: a record set, e.g. ``/itemLedgerEntries/``, whose records the reader may
    not open) and openability. The world is built on
    the thread that first uses the view (the Ask worker's owner thread: its stores are
    SQLite connections bound to it, as the console's runtime is)."""

    def __init__(self, case: unittest.TestCase, withheld: str | None = None,
                 records: list | None = None) -> None:
        self.withheld, self._world, self.calls = withheld, None, 0
        self.records = records
        self._cleanups: list = []
        self.case = SimpleNamespace(addCleanup=self._cleanups.append)
        self.access = SimpleNamespace(decide=lambda hs: {
            h: ({"reason": "item_refused"} if self.withheld and self.withheld in h else None)
            for h in hs})

    @property
    def world(self) -> World:
        if self._world is None:
            self._world = World(self.case, records=self.records)
        return self._world

    def _call(self, name, arguments):
        self.calls += 1
        return self.world.call(name, arguments)

    @contextlib.contextmanager
    def _request(self):
        yield

    def _base(self):
        return {}

    def _can_open(self, handle):
        return True

    def bind(self, session):
        pass

    def close(self):        # on the owner thread (``Worker.loop``)
        while self._cleanups:
            self._cleanups.pop()()


class ProfitAskTests(unittest.TestCase):
    def backend(self, view: View) -> be.WorkspaceBackend:
        backend = be.WorkspaceBackend(lambda session: view, data_label="isolated-fixture",
                                      today=lambda: TODAY)
        worker = backend.add_ask_worker(lambda session: view)
        thread = threading.Thread(target=worker.loop, daemon=True)
        thread.start()
        self.addCleanup(backend.stop)
        return backend

    @staticmethod
    def reader():
        return SimpleNamespace(session=SimpleNamespace(principals=frozenset({"staff:full"})),
                               key="staff:full")

    def ask(self, view: View, question: str) -> tuple[list[str], dict]:
        backend = self.backend(view)
        events = list(backend.ask(self.reader(), m.AskRequest(question=question)))
        for name, payload in events:
            m.ASK_EVENTS[name].model_validate(payload.model_dump())
        answer = events[-1][1].model_dump()
        return [n for n, _ in events], json.loads(json.dumps(answer))

    def test_a_customer_answer_states_the_totals_ranks_orders_and_explains_the_worst(self):
        names, answer = self.ask(View(self),
                                 "Is Northwind Test Foods profitable and why not?")
        self.assertEqual(names[-1], "answer")
        self.assertEqual(answer["outcome"], "partial")     # SO-104 has no margin yet
        self.assertTrue(answer["direct_answer"].startswith(
            "Northwind Test Foods is not profitable in the last 12 months: revenue 190.00 "
            "CAD, item cost 231.00 CAD, margin on item cost only: -41.00 CAD (-21.58%) over "
            "3 invoiced orders; an order counts in the period by its first posted "
            "invoice's posting date"), answer["direct_answer"])
        self.assertIn("1 order not yet invoiced, excluded. Labour and overhead aren't booked "
                      "in Business Central, so this overstates true margin.",
                      answer["direct_answer"])
        self.assertIn("Worst SO-101: revenue 90.00 CAD, item cost 135.00 CAD, loss on item "
                      "cost only 45.00 CAD (-50.00%). Why: Item FG-9", answer["direct_answer"])
        self.assertIn("Suggested next step: Review the price for Northwind Test Foods on item "
                      "FG-9", answer["direct_answer"])
        facts = [f["text"] for f in answer["facts"]]
        self.assertIn("Order SO-101: revenue 90.00 CAD, item cost 135.00 CAD, loss on item "
                      "cost only 45.00 CAD.", facts)
        for fact in answer["facts"]:
            self.assertEqual(fact["check"], "computed_from_cited_records")
            self.assertTrue(fact["sources"])
            self.assertTrue(all(s["open_url"] for s in fact["sources"]))
        profit = answer["profitability"]
        self.assertEqual(profit["margin"], {"amount": "-41.00", "currency": "CAD"})
        self.assertEqual([o["order"]["label"] for o in profit["orders"]],
                         ["SO-101", "SO-102", "SO-103", "SO-104"])
        self.assertEqual(profit["orders"][-1]["margin"], None)
        self.assertIn("line not invoiced", profit["orders"][-1]["gap"])
        breakdown = profit["breakdown"]
        self.assertEqual(breakdown["order"]["label"], "SO-101")
        self.assertEqual({s["kind"] for s in breakdown["cost_sources"]},
                         {"item_ledger", "credit_memo", "return", "material_po", "quote"})
        self.assertTrue(breakdown["drivers"])
        self.assertTrue(profit["next_action"]["suggestion_only"])
        trail = answer["trail"]
        self.assertEqual(trail["kind"], "claims")
        steps = trail["steps"]
        self.assertEqual(steps[2]["title"], "Order SO-101")
        self.assertEqual(steps[-1]["kind"], "verdict")
        self.assertTrue(steps[-1]["title"].startswith("Item FG-9 (Synthetic tube) lost"))
        self.assertTrue(any(s["source"] and s["source"]["system"] == "BC" for s in steps))
        self.assertEqual(profit["next_action"]["rule"], "item_below_cost_on_repeat_orders")
        gaps = " ".join(n["what"] for n in answer["not_found"])
        self.assertIn("Labour overhead not recorded", gaps)
        self.assertIn("SO-104", gaps)

    def test_an_invoiced_order_with_no_cost_value_gives_no_verdict_only_a_partial_ranking(self):
        """The review's repro: SO-103's ledger cost set to 0. No verdict, no customer total,
        the blocking order named; the checked orders ranked as partial; the uninvoiced
        order listed as excluded; every margin labelled item cost only."""
        records = bc_records()
        for record in records:
            if record["fields"].get("documentNumber") == "SS-103":
                record["fields"]["costAmountActual"] = 0
        _names, answer = self.ask(View(self, records=records),
                                  "Is Northwind Test Foods profitable and why not?")
        text = answer["direct_answer"]
        self.assertTrue(text.startswith(
            "I can't give a verdict for Northwind Test Foods in the last 12 months: 1 "
            "invoiced order has no cost value in BC (SO-103). 1 order not yet invoiced, "
            "excluded. The 2 checked orders are ranked below (partial)."), text)
        self.assertNotIn("is profitable", text)
        self.assertNotIn("is not profitable", text)
        self.assertIn("Labour and overhead aren't booked in Business Central", text)
        profit = answer["profitability"]
        self.assertIsNone(profit["margin"])
        self.assertIsNone(profit["verdict"])
        self.assertEqual(profit["margin_basis"], "on item cost only")
        self.assertEqual(profit["orders_excluded"], 1)
        self.assertTrue(profit["ranking"].startswith("partial: the 2 checked orders only"))
        self.assertEqual([o["order"]["label"] for o in profit["orders"] if o["margin"]],
                         ["SO-101", "SO-102"])
        self.assertNotEqual(answer["outcome"], "answered")

    def test_a_customer_with_no_order_in_the_period_says_so(self):
        records = bc_records()
        template = next(r for r in records if r["entity_set"] == "customers"
                        and r["fields"].get("number") == "C-51")
        extra = {**template, "id": template["id"][:-4] + "beef",
                 "fields": {**template["fields"], "id": template["id"][:-4] + "beef",
                            "number": "C-52", "displayName": "Eastwind Test"}}
        records.append(extra)
        _names, answer = self.ask(View(self, records=records), "Is Eastwind Test profitable?")
        self.assertEqual(answer["direct_answer"].split(" Suggested")[0],
                         "Eastwind Test has no order you can see in the last 12 months.")
        self.assertIsNone(answer["profitability"]["margin"])
        self.assertIsNone(answer["profitability"]["verdict"])

    def test_checked_orders_in_different_currencies_give_no_single_total(self):
        """Every invoiced order checked, but in two currencies: no verdict, no single
        total, each order ranked within its own currency."""
        from unittest import mock
        from operating import customer_profit as cp
        real = cp.read_order

        def in_usd(call, order):
            got = real(call, order)
            if got.number == "SO-103" and got.decided:
                got.currency = "USD"
            return got
        with mock.patch.object(cp, "read_order", in_usd):
            _names, answer = self.ask(View(self), "Is Northwind Test Foods profitable?")
        text = answer["direct_answer"]
        self.assertTrue(text.startswith(
            "I can't give a verdict for Northwind Test Foods in the last 12 months: its orders "
            "are in different currencies (CAD and USD), so no single total. The 3 checked "
            "orders are ranked below within each currency."), text)
        profit = answer["profitability"]
        self.assertIsNone(profit["margin"])
        self.assertIsNone(profit["verdict"])
        self.assertIn("different currencies (CAD and USD), so no single total",
                      profit["ranking"])
        ranked = [(o["order"]["label"], (o["margin"] or {}).get("currency"))
                  for o in profit["orders"] if o["margin"]]
        self.assertEqual(ranked, [("SO-101", "CAD"), ("SO-102", "CAD"), ("SO-103", "USD")])

    def test_a_record_the_reader_may_not_open_is_never_named(self):
        _names, answer = self.ask(View(self), "Is Northwind Test Foods profitable?")
        self.assertIn("/itemLedgerEntries/", json.dumps(answer))
        _names, held = self.ask(View(self, "/itemLedgerEntries/"),
                                "Is Northwind Test Foods profitable?")
        self.assertNotIn("/itemLedgerEntries/", json.dumps(held))
        # The figures stand (the reader's tools read them); the sources are not shown, and
        # the trail steps resting on them are counted, never shown.
        self.assertEqual(held["profitability"]["margin"]["amount"], "-41.00")
        self.assertEqual(held["trail"]["hidden_steps"], 3)
        self.assertEqual(held["trail"]["hidden_message"], "3 more steps in places you can't "
                                                          "open.")

    def test_past_the_read_limit_there_is_no_verdict_only_a_partial_ranking(self):
        from unittest import mock
        from operating import customer_profit as cp
        with mock.patch.object(cp, "MAX_ORDERS", 2):
            _names, answer = self.ask(View(self), "Is Northwind Test Foods profitable?")
        text = answer["direct_answer"]
        self.assertTrue(text.startswith("I can't give a customer total for Northwind Test "
                                        "Foods in the last 12 months: it has 4 orders in the "
                                        "period and only the 2 most recent were read (2 more "
                                        "in the period not read)"), text)
        self.assertNotIn("profitable", text)
        self.assertIn("Worst of the 2 most recent orders read SO-101", text)
        profit = answer["profitability"]
        self.assertEqual((profit["margin"], profit["orders_in_period"], profit["orders_read"]),
                         (None, 4, 2))
        self.assertEqual(profit["ranking"], "partial: worst of the 2 most recent orders; 2 "
                                            "more in the period not read")

    def test_a_name_written_differently_is_picked_after_normalising(self):
        _names, answer = self.ask(View(self), "Is the northwind test foods inc. profitable?")
        self.assertEqual(answer["profitability"]["customer"]["label"], "Northwind Test Foods")

    def test_each_order_sentence_carries_that_orders_own_currency(self):
        _names, answer = self.ask(View(self), "Is Southwind Test profitable?")
        self.assertNotIn("$", answer["direct_answer"])
        # Southwind's one order is invoiced in USD against CAD cost: no margin, so no
        # customer currency exists; no amount is printed without a currency.
        self.assertIsNone(answer["profitability"]["margin"])
        _names, answer = self.ask(View(self), "Is Northwind Test Foods profitable?")
        orders = [f["text"] for f in answer["facts"] if f["text"].startswith("Order ")]
        self.assertTrue(orders)
        for text in orders:
            self.assertNotIn("$", text)
            self.assertEqual(text.count(" CAD"), 3, text)

    def test_a_cut_order_list_says_more_may_exist_never_zero_more(self):
        from unittest import mock
        from operating import customer_profit as cp
        real = cp.customer_orders

        def cut(*args, **kwargs):
            orders, trimmed, _whole, outside, skipped = real(*args, **kwargs)
            return orders, trimmed, False, outside, skipped
        with mock.patch.object(cp, "customer_orders", cut):
            _names, answer = self.ask(View(self), "Is Northwind Test Foods profitable?")
        text = answer["direct_answer"]
        self.assertIn("it has at least 4 orders in the period and only the 4 most recent "
                      "were read (the order list itself was cut, so more may exist)", text)
        self.assertNotIn("0 more", text)
        self.assertIsNone(answer["profitability"]["margin"])

    def test_a_fuzzy_customer_match_is_offered_never_picked(self):
        _names, answer = self.ask(View(self), "Is Northwind profitable?")
        self.assertEqual(answer["direct_answer"],
                         "Did you mean Northwind Test Foods (C-50)?")
        self.assertIsNone(answer["profitability"])

    def test_an_order_asked_about_leads_with_its_margin_and_why(self):
        """No customer named: the order's own customer, led by the order, its why against
        the same item's other order; an order older than the default period widens it."""
        _names, answer = self.ask(View(self), "Why did SO-102 lose money?")
        direct = answer["direct_answer"]
        self.assertTrue(direct.startswith(
            "Order SO-102: revenue 60.00 CAD, item cost 80.00 CAD, loss on item cost only "
            "20.00 CAD (-33.33%). Why: Item FG-9 (Synthetic tube) lost 20.00 CAD"), direct)
        self.assertIn("against SO-101 (2026-09-05, the next order with it)", direct)
        self.assertIn("Northwind Test Foods is not profitable in the last 12 months", direct)
        self.assertLess(direct.index("Why:"), direct.index("Northwind Test Foods is"))
        self.assertIn("By product, worst first: item FG-9 (Synthetic tube): margin on item "
                      "cost only -65.00 CAD (-43.33%) on 14 PCS over 2 orders", direct)
        profit = answer["profitability"]
        self.assertEqual(profit["breakdown"]["order"]["label"], "SO-102")
        self.assertEqual([p["item"] for p in profit["products"]],
                         ["item FG-9 (Synthetic tube)", "item FG-1 (Synthetic jar)"])
        self.assertTrue(all(p["sources"] for p in profit["products"]))
        _names, older = self.ask(View(self), "what margin did we make on SO-105")
        self.assertTrue(older["direct_answer"].startswith(
            "Order SO-105: revenue 30.00 CAD, item cost 4.00 CAD, margin on item cost only "
            "26.00 CAD (86.67%)."), older["direct_answer"])
        self.assertEqual(older["profitability"]["period_start"], "2024-03-01")
        # an order number naming no order goes on (the fast path or the agent answers)
        backend = self.backend(View(self))
        steps = backend._profit_ask(self.reader(), m.AskRequest(
            question="why did SO-999 lose money"), "c1", lambda: 0)
        with self.assertRaises(StopIteration) as done:
            while True:
                next(steps)
        self.assertIsNone(done.exception.value)

    def test_the_order_and_customer_pages_carry_the_margin_and_its_why(self):
        """The ``margin`` page section: a BC sales order's page leads with that order's
        breakdown and why; its customer's page ranks every order; any other record has no
        margin section (nothing read)."""
        view = View(self)
        backend = be.WorkspaceBackend(lambda session: view, data_label="isolated-fixture",
                                      today=lambda: TODAY)
        self.addCleanup(view.close)
        found = view._call("plx.find", {"query": "SO-102", "object_type": "SalesOrder"})
        order = next(c["object"]["object_id"] for c in found["candidates"]
                     if c["object"]["object_id"].split(":")[1].startswith("bc."))
        page = SimpleNamespace(object_id=order, type="SalesOrder", label="SO-102")
        section = be.read_section(backend, view, page, "margin")
        self.assertTrue(section.applies)
        got = section.profitability
        self.assertEqual(got.breakdown.order.label, "SO-102")
        self.assertEqual(got.breakdown.margin.amount, "-20.00")
        self.assertTrue(any("the next order with it" in d for d in got.breakdown.drivers))
        merged = be.merged(m.ObjectPage.model_construct(), section)
        self.assertEqual(merged.profitability.breakdown.order.label, "SO-102")
        customer = view.world.customer("C-50")
        section = be.read_section(backend, view, SimpleNamespace(
            object_id=customer, type="Customer", label="Northwind Test Foods"), "margin")
        self.assertEqual(section.profitability.margin.amount, "-41.00")
        self.assertEqual(section.profitability.breakdown.order.label, "SO-101")
        calls = view.calls
        other = be.read_section(backend, view, SimpleNamespace(
            object_id="PurchaseOrder:bc.purchase_orders:x", type="PurchaseOrder",
            label="PO9001"), "margin")
        self.assertFalse(other.applies)
        self.assertIsNone(other.profitability)
        self.assertEqual(view.calls, calls)

    def test_no_customer_found_goes_on_and_several_are_offered_never_merged(self):
        backend = self.backend(View(self))
        steps = backend._profit_ask(self.reader(), m.AskRequest(
            question="is Zebulon Unknown profitable?"), "c1", lambda: 0)
        with self.assertRaises(StopIteration) as done:
            while True:
                next(steps)
        self.assertIsNone(done.exception.value)
        _names, answer = self.ask(View(self), "Is Test profitable?")
        self.assertEqual(answer["outcome"], "partial")
        self.assertEqual(sorted(c["label"] for c in answer["did_you_mean"]),
                         ["Northwind Test Foods (C-50)", "Southwind Test (C-51)"])
        self.assertIsNone(answer["profitability"])

    def test_a_did_you_mean_pick_keeps_the_question_it_answers(self):
        """The app sends a pick as "I mean <choice>" with the picked customer and the
        conversation so far: it answers the earlier profitability question for that
        customer, and the choice's own wording ("Name (number)") picks it as well."""
        view = View(self)
        backend = self.backend(view)
        first = list(backend.ask(self.reader(), m.AskRequest(question="Is Test profitable?")))
        choice = next(c for c in first[-1][1].did_you_mean
                      if c.label.startswith("Northwind"))
        events = list(backend.ask(self.reader(), m.AskRequest(
            question=f"I mean {choice.label}", object_id=choice.object_id,
            history=[m.HistoryTurn(role="user", text="Is Test profitable?"),
                     m.HistoryTurn(role="assistant", text=first[-1][1].direct_answer)])))
        for name, payload in events:
            m.ASK_EVENTS[name].model_validate(payload.model_dump())
        picked = events[-1][1].model_dump()
        self.assertEqual(picked["profitability"]["customer"]["object_id"], choice.object_id)
        self.assertTrue(picked["direct_answer"].startswith(
            "Northwind Test Foods is not profitable in the last 12 months"),
            picked["direct_answer"])
        _names, named = self.ask(View(self), f"is {choice.label} profitable, and why not?")
        self.assertEqual(named["profitability"]["customer"]["object_id"], choice.object_id)
        # A pick with no earlier question to carry is no profitability question, and a
        # record that is not a customer never runs this path.
        self.assertIsNone(be.picked_question(m.AskRequest(
            question=f"I mean {choice.label}", object_id=choice.object_id)))
        self.assertFalse(be.customer_object("PurchaseOrder:bc.purchase_orders:{}"))


if __name__ == "__main__":
    unittest.main()


class SameNamedCustomerTests(unittest.TestCase):
    def test_a_same_named_card_without_orders_is_no_choice_beside_one_with_them(self):
        """One company's cards of a name: the one with orders is the customer; a card whose
        orders the reader may not see, or whose read failed, stays a choice; none with
        orders: every card is offered."""
        def view(results):
            return SimpleNamespace(_call=lambda name, args: results[args["from"]])
        cards = [{"object_id": f"Customer:bc.customers:{{\"id\":\"{i}\"}}"} for i in range(3)]
        ids = [c["object_id"] for c in cards]
        orders = {"objects": [{"object_id": "SalesOrder:x", "display": "SO1"}]}
        self.assertEqual(be.with_orders(view({ids[0]: {}, ids[1]: orders, ids[2]: {}}), cards),
                         [cards[1]])
        self.assertEqual(be.with_orders(view({ids[0]: {"withheld": {"bc": 2}},
                                              ids[1]: orders,
                                              ids[2]: {"error": {"code": "x"}}}), cards), cards)
        self.assertEqual(be.with_orders(view({i: {} for i in ids}), cards), cards)
