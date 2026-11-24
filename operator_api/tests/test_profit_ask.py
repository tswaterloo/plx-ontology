"""The app's Ask on "is <customer> profitable, and why not?" (``backend._profit_ask``) over the
BC + Portal fixture world of ``tests.test_customer_profit``: the real ``WorkspaceBackend``
and Ask pool, a reader view over the real tools. The answer event validates against the
contract; every source is decided for the reader; a record the reader may not open is
never named. Evidence label: isolated-fixture."""

from __future__ import annotations

import contextlib
import json
import re
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from operator_api import backend as be
from operator_api import models as m
from tests.test_customer_profit import TODAY, World, bc_records


def draft_records() -> list:
    """The fixture world plus customer C-52 (Eastwind Test) with two BC Draft orders and
    nothing invoiced."""
    records = bc_records()
    customer = next(r for r in records if r["entity_set"] == "customers"
                    and r["fields"].get("number") == "C-51")
    order = next(r for r in records if r["entity_set"] == "salesOrders"
                 and r["fields"].get("number") == "SO-104")
    line = next(r for r in records if r["entity_set"] == "salesOrderLines"
                and r.get("document_id") == order["id"])

    def copy(record: dict, suffix: str, **fields) -> dict:
        rid = record["id"][:-4] + suffix
        return {**record, "id": rid, "fields": {**record["fields"], "id": rid, **fields}}
    records.append(copy(customer, "be01", number="C-52", displayName="Eastwind Test"))
    for n, (number, amount) in enumerate((("SO-301", 1000.0), ("SO-302", 250.5))):
        head = copy(order, f"be1{n}", number=number, customerNumber="C-52", status="Draft",
                    currencyCode="CAD", totalAmountExcludingTax=amount, orderDate="2026-10-0"
                    + str(n + 2))
        records.append(head)
        row = copy(line, f"be2{n}", documentId=head["id"])
        records.append({**row, "document_id": head["id"]})
    return records


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
        # The read worker owns a view of its own (the same world, opened on its thread):
        # a view's stores are bound to the thread that opened them, and each worker
        # closes the views it holds on its own thread when it stops.
        reads_view = View(self, view.withheld, records=view.records)
        backend = be.WorkspaceBackend(lambda session: reads_view,
                                      data_label="isolated-fixture", today=lambda: TODAY)
        # The general-knowledge pass a partial answer gets (``general_answer`` fallback)
        # runs on a stand-in: never the box's real model CLI (a host with one would
        # otherwise answer through it, and its words would land in these answers).
        backend.general_call = self.general_reply
        worker = backend.add_ask_worker(lambda session: view)
        thread = threading.Thread(target=worker.loop, daemon=True)
        thread.start()
        # The read worker, as the served app runs one: an answer asks it for the markings
        # the reader holds (``feedback.served_markings``); without it that read waited out
        # its 60 s limit on every ask (20 asks: past the suite's 900 s module cap).
        threading.Thread(target=backend.reads.loop, daemon=True).start()
        self.addCleanup(backend.stop)
        return backend

    def setUp(self) -> None:
        self.general_said: str | None = None   # the stand-in model's reply (None: none)
        self.general_asked: list[str] = []

    def general_reply(self, text, images=()):
        self.general_asked.append(text)
        return self.general_said

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

















    def test_a_customer_pages_margin_reads_its_orders_beside_the_ask_pool(self):
        """With two or more Ask workers, the margin section's orders are read side by side
        on the pool's workers, each as this reader on its own runtime, and the margin is the
        one read one order after another; with fewer, the section reads alone."""
        page_view, helpers = View(self), (View(self), View(self))
        self.addCleanup(page_view.close)
        alone = be.WorkspaceBackend(lambda session: page_view, data_label="isolated-fixture",
                                    today=lambda: TODAY)
        customer = page_view.world.customer("C-50")
        page = SimpleNamespace(object_id=customer, type="Customer",
                               label="Northwind Test Foods")
        job = lambda view: be.read_section(alone, view, page, "margin")   # noqa: E731
        self.assertIs(alone._spread(self.reader(), job), job)
        plain = job(page_view).profitability
        backend = be.WorkspaceBackend(lambda session: page_view, data_label="isolated-fixture",
                                      today=lambda: TODAY)
        for view in helpers:   # each worker's own, opened and closed on its thread
            worker = backend.add_ask_worker(lambda session, view=view: view)
            threading.Thread(target=worker.loop, daemon=True).start()
        self.addCleanup(backend.stop)
        spread = backend._spread(self.reader(), lambda view: be.read_section(
            backend, view, page, "margin"))(page_view).profitability
        # a helper read orders as this reader
        self.assertGreater(sum(view.calls for view in helpers), 0)
        # the helper's world is captured on its own (revision suffixes of handles differ)
        def same(model):
            return re.sub(r"(@|%40)[0-9a-f]+-", "@-",
                          json.dumps(model.model_dump(), sort_keys=True))
        self.assertEqual(same(spread), same(plain))
        self.assertEqual(spread.margin.amount, "-41.00")

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
        # why it loses money, over the checked orders (stated even without a total)
        drivers = {d.kind: d for d in section.profitability.drivers}
        self.assertEqual(drivers["losing_orders"].amount.model_dump(),
                         {"amount": "-65.00", "currency": "CAD"})
        self.assertTrue(drivers["credit"].sources and drivers["low_margin_item"].sources)
        self.assertIsNone(drivers["freight"].amount)
        self.assertIn("/itemLedgerEntries/", json.dumps([d.model_dump() for d in
                                                         drivers.values()]))
        # a reader who may not open the item ledger: the reasons stand on the figures its
        # tools read, the ledger records behind them are never shown
        held_view = View(self, "/itemLedgerEntries/")
        self.addCleanup(held_view.close)
        held = be.read_section(be.WorkspaceBackend(
            lambda session: held_view, data_label="isolated-fixture", today=lambda: TODAY),
            held_view, SimpleNamespace(object_id=customer, type="Customer",
                                       label="Northwind Test Foods"), "margin")
        shown = json.dumps([d.model_dump() for d in held.profitability.drivers])
        self.assertIn("lost money", shown)
        self.assertNotIn("/itemLedgerEntries/", shown)
        self.assertEqual(section.profitability.margin.amount, "-41.00")
        self.assertEqual(section.profitability.breakdown.order.label, "SO-101")
        calls = view.calls
        other = be.read_section(backend, view, SimpleNamespace(
            object_id="PurchaseOrder:bc.purchase_orders:x", type="PurchaseOrder",
            label="PO9001"), "margin")
        self.assertFalse(other.applies)
        self.assertIsNone(other.profitability)
        self.assertEqual(view.calls, calls)

    def test_order_and_customer_pages_walk_the_materials_holding_them(self):
        """The ``trail`` section of a customer order's and a customer's page is the
        materials walk Ask runs (late materials holding the order, each late PO's cited
        trail); a link the records do not hold is a typed gap, never a guess. Other records
        than a purchase order, customer order or customer have no trail section."""
        view = View(self)
        backend = be.WorkspaceBackend(lambda session: view, data_label="isolated-fixture",
                                      today=lambda: TODAY)
        self.addCleanup(view.close)
        found = view._call("plx.find", {"query": "SO-101", "object_type": "SalesOrder"})
        order = next(c["object"]["object_id"] for c in found["candidates"]
                     if c["object"]["object_id"].split(":")[1].startswith("bc."))
        for oid, kind, label in ((order, "SalesOrder", "SO-101"),
                                 (view.world.customer("C-50"), "Customer",
                                  "Northwind Test Foods")):
            with self.subTest(kind=kind):
                page = m.ObjectPage.model_construct(
                    object_id=oid, type=kind, label=label, not_found=[], late_lines=[],
                    facts=[], status=m.ObjectStatus(state="read", text=""))
                section = be.read_section(backend, view, page, "trail")
                self.assertTrue(section.applies)
                self.assertEqual(section.trail.kind, "materials")
                self.assertIn("assembly_order_not_linked_to_order_line",
                              {g.reason for g in section.trail.gaps})
        calls = view.calls
        other = be.read_section(backend, view, SimpleNamespace(
            object_id="Item:bc.items:x", type="Item", label="FG-9"), "trail")
        self.assertFalse(other.applies)
        self.assertEqual(view.calls, calls)








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
