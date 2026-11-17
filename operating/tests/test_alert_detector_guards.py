"""Boundary guards of the 11-13 alert detector fixes (alerts-fix-1113): a receipt note's
own record must stand behind "received", a probably-fulfilled order needs a dated packing
slip, ``missing_in_record`` is one gap per attested record and field, a stale label
request names its item or customer, a demand drop skips mixed-currency and
same-name-other-source customers (citing every record its skip read), and the
``label_change`` kind stays off.

Each test pins one boundary a plausible edit of the detector would break.

SYNTHETIC fixtures only (``World`` stores, fake stores, synthetic handles and names);
evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest import mock

from operating import label_changes as lc
from operating import proactive_ceo as ceo
from operating import proactive_engine as pe
from operating import proactive_engine_purchasing
from operating import proactive_engine_sales
from operating.tests.test_label_changes import ask
from operating.tests.test_proactive_ceo import day, dropping, order
from operating.tests.test_proactive_engine import AS_OF, Engine, World

RECEIPT_DOUBTS = dict(zip(("header_open", "no_receipt"), proactive_engine_purchasing.RECEIPT_DOUBTS))


class _EngineBase(unittest.TestCase):
    """The ``Engine`` fixture (a store, an inbox, the business day pinned) without
    inheriting its tests, so this module runs only its own."""

    setUp = Engine.setUp
    run_engine = Engine.run_engine
    read = Engine.read


# --------------------------------------------------------------------------- receipt notes

def _conflict(*items: dict, governs: str = "filemaker", field: str = "receipt_state",
              sides: list | None = None) -> dict:
    return {"field": field, "governs": governs, "reconcile": True,
            "sides": sides if sides is not None else [
                {"system": governs, "items": list(items)},
                {"system": "bc", "items": [{"object_id": "PurchaseOrder:bc:2",
                                            "property": "bc_status", "value": "Open"}]}]}


HEADER = "PurchaseOrder:portal.fm:1"
RECEIPT = {"object_id": "Receipt:portal.fm_receipt:9", "property": "fm_status",
           "value": "Received"}


class ReceiptDoubt(unittest.TestCase):

    def test_an_open_header_is_doubted_even_beside_a_cited_receipt(self):
        # mutation caught: checking for a receipt before the header (a receipt then
        # excuses a B.Order header)
        for prop in ("fm_status", "status"):
            for raw in ("B.Order", " b.order ", "Pending", "generated", "REVISED"):
                with self.subTest(prop=prop, raw=raw):
                    got = proactive_engine_purchasing._receipt_doubt(_conflict(
                        {"object_id": HEADER, "property": prop, "value": raw}, RECEIPT))
                    self.assertEqual(got, RECEIPT_DOUBTS["header_open"])

    def test_a_closed_header_with_a_receipt_stands_and_a_line_word_is_no_header(self):
        # mutation caught: matching an open word on any record (a line's B.Order is
        # rolled up, not the order's header)
        for header in ("Complete", "Received", None):
            with self.subTest(header=header):
                self.assertIsNone(proactive_engine_purchasing._receipt_doubt(_conflict(
                    {"object_id": HEADER, "property": "fm_status", "value": header},
                    {"object_id": "PurchaseOrderLine:portal.fm:3", "property": "fm_status",
                     "value": "B.Order"}, RECEIPT)))

    def test_every_receipt_record_kind_and_a_bc_received_quantity_count(self):
        # mutation caught: dropping ReceiptLine/Lot or the BC received-quantity basis
        for item in ({"object_id": "ReceiptLine:fx:1", "property": "qty", "value": "5"},
                     {"object_id": "Lot:fx:1", "property": "qc_status", "value": "Released"},
                     {"object_id": "PurchaseOrderLine:bc:1",
                      "property": "received_quantity", "value": "10"}):
            with self.subTest(item=item["object_id"]):
                self.assertIsNone(proactive_engine_purchasing._receipt_doubt(_conflict(
                    {"object_id": HEADER, "property": "fm_status", "value": "Complete"},
                    item)))

    def test_a_status_word_alone_or_a_missing_received_side_is_no_receipt(self):
        # mutation caught: treating a missing received side, or a header word, as cited
        self.assertEqual(proactive_engine_purchasing._receipt_doubt(_conflict(
            {"object_id": HEADER, "property": "fm_status", "value": "Complete"})),
            RECEIPT_DOUBTS["no_receipt"])
        self.assertEqual(proactive_engine_purchasing._receipt_doubt(_conflict(RECEIPT, governs="portal", sides=[
            {"system": "bc", "items": [RECEIPT]}])), RECEIPT_DOUBTS["no_receipt"])
        # malformed sides and items never raise: no receipt is cited
        for sides in ([], [{"system": "filemaker"}],
                      [{"system": "filemaker", "items": None}],
                      [{"system": "filemaker", "items": ["Receipt:fx:1", None, 3]}]):
            with self.subTest(sides=sides):
                self.assertEqual(proactive_engine_purchasing._receipt_doubt(_conflict(sides=sides)),
                                 RECEIPT_DOUBTS["no_receipt"])
        conflict = _conflict(RECEIPT)
        del conflict["sides"]
        self.assertEqual(proactive_engine_purchasing._receipt_doubt(conflict), RECEIPT_DOUBTS["no_receipt"])

    def test_only_a_receipt_state_conflict_is_judged(self):
        # mutation caught: doubting every reconcile conflict (a received-quantity
        # disagreement is not a receipt note)
        self.assertIsNone(proactive_engine_purchasing._receipt_doubt(_conflict(field="received_quantity")))


# --------------------------------------------------------------------------- ship evidence

class _Slips:
    """A store holding each packing slip's ``ship_date`` values (raw, as served)."""

    def __init__(self, dates: dict[str, list]) -> None:
        self.dates = dates
        self.asked: list = []

    def values(self, types, props, *, objects):
        self.asked.append((tuple(types), tuple(props), tuple(objects)))
        return {s: {"props": {proactive_engine_sales.SHIP_DATE: [
            {"value": v, "l1_handle": f"h:{s}:{i}", "object_id": s, "markings": "[]"}
            for i, v in enumerate(self.dates[s])]}} for s in objects if s in self.dates}


def _verdict(*slips: str) -> SimpleNamespace:
    return SimpleNamespace(shipped=tuple((s, "[]") for s in slips), link_marks=())


class ShipEvidence(unittest.TestCase):

    def test_an_undated_or_unparseable_slip_is_no_evidence_and_never_raises(self):
        # mutation caught: accepting any slip, or parsing without a guard
        st = _Slips({"PackingSlip:fx:1": [None, "", "n/a", "13/10/2026", "2026-13-40"],
                     "PackingSlip:fx:2": []})
        self.assertIsNone(proactive_engine_sales._ship_evidence(st, _verdict("PackingSlip:fx:1",
                                                         "PackingSlip:fx:2",
                                                         "PackingSlip:fx:3"), AS_OF))
        self.assertIsNone(proactive_engine_sales._ship_evidence(st, _verdict(), AS_OF))
        self.assertEqual(len(st.asked), 1)   # no slip named: nothing read

    def test_the_newest_dated_slip_on_or_before_the_day_is_said_and_each_is_cited(self):
        # mutation caught: ``<`` for ``<=`` (a slip dated the business day), ``min`` for
        # ``max``, citing only the first slip
        st = _Slips({"PackingSlip:fx:1": ["2026-10-01", "garbled"],
                     "PackingSlip:fx:2": [AS_OF.isoformat() + "T08:00:00Z"],
                     "PackingSlip:fx:3": [(AS_OF + timedelta(days=1)).isoformat()]})
        said, cited = proactive_engine_sales._ship_evidence(st, _verdict(*st.dates), AS_OF)
        self.assertEqual(said, pe.say_date(AS_OF))
        self.assertEqual(sorted(c[0] for c in cited),
                         ["h:PackingSlip:fx:1:0", "h:PackingSlip:fx:2:0"])

    def test_a_slip_dated_only_after_the_day_is_no_evidence(self):
        st = _Slips({"PackingSlip:fx:1": [(AS_OF + timedelta(days=1)).isoformat()]})
        self.assertIsNone(proactive_engine_sales._ship_evidence(st, _verdict("PackingSlip:fx:1"), AS_OF))


# --------------------------------------------------------------------------- missing_in_record

class _Cards:
    """A store and a tool caller serving one card per document, each with the
    ``missing_in_record`` leads given: document -> [(subject, record or None)]; a record
    of None is an empty read whose handle the store does not hold."""

    def __init__(self, leads: dict[str, list[tuple[str, str | None]]]) -> None:
        self.leads = leads
        docs = list(leads)
        held = {f"doc#{d}" for d in docs} | {f"rec#{r}" for ls in leads.values()
                                               for _s, r in ls if r}

        class St:
            class conn:
                @staticmethod
                def execute(_sql, _params):
                    return [(d,) for d in docs]

            @staticmethod
            def versions(handles):
                return [{"l1_handle": h, "object_id": h.split("#", 1)[1], "markings": "[]"}
                        for h in handles if h in held]

            @staticmethod
            def judging(_name):
                return None
        self.st = St

    def call(self, _tool, arguments):
        doc = arguments["object_id"]
        return {"object": {"object_id": doc, "object_type": "AssemblyOrderPacket"},
                "conflicts": [],
                "leads": [{"kind": "missing_in_record", "subject": subject,
                           "values": [{"value": "2026-10-01",
                                       "citation": {"l1_handle": f"doc#{doc}"}},
                                      {"value": None, "citation": {
                                          "l1_handle": f"rec#{record or 'gone-' + doc}"}}]}
                          for subject, record in self.leads[doc]]}

    def run(self) -> dict:
        # every document is a candidate (AL-04's set-based prefilter keeps a superset;
        # test_proactive_engine covers it): these tests judge the cards themselves
        with mock.patch.object(pe.pg, "_roster",
                               lambda: ([("AssemblyOrderPacket", "f")], None)), \
                mock.patch.object(pe.pg, "card_candidates",
                                  lambda _conn, _as_of: set(self.leads)):
            return pe._card_rules(self.st, AS_OF, self.call, len(self.leads))


class MissingInRecord(unittest.TestCase):

    def test_two_fields_of_one_order_are_two_gaps_each_once(self):
        # mutation caught: merging on the attested records alone (a second field of the
        # same order is lost)
        order_id = "SalesOrder:fx:o1"
        cards = _Cards({f"AssemblyOrderPacket:fx:{i}": [
            ("SalesOrder.required_date", order_id), ("SalesOrder.customer_po", order_id)]
            for i in range(3)})
        result = cards.run()
        self.assertEqual(sorted(g["summary"].split(" for ", 1)[1].split(" ", 1)[0]
                                for g in result["gaps"]),
                         ["SalesOrder.customer_po", "SalesOrder.required_date"])
        self.assertEqual(result["counts"]["repeats_merged"], 4)
        self.assertEqual({g["follow_up"][pe._ATTESTS] for g in result["gaps"]}, {order_id})

    def test_an_empty_read_the_store_does_not_hold_is_never_merged(self):
        # mutation caught: dropping the ``if records`` guard (every unheld read keys on
        # the same empty set, so unrelated documents would merge into one)
        cards = _Cards({f"AssemblyOrderPacket:fx:{i}": [("SalesOrder.required_date", None)]
                        for i in range(3)})
        result = cards.run()
        self.assertEqual(len(result["gaps"]), 3)
        self.assertEqual(result["counts"]["repeats_merged"], 0)
        self.assertFalse([g for g in result["gaps"] if pe._ATTESTS in g["follow_up"]])

    def test_only_an_order_among_the_attested_records_rides_on_the_follow_up(self):
        # mutation caught: carrying any attested record (an item is no order the
        # active-population filter can close)
        cards = _Cards({"AssemblyOrderPacket:fx:1": [("Product.name", "Product:fx:p1")],
                        "AssemblyOrderPacket:fx:2": [("PurchaseOrder.expected_date",
                                                      "PurchaseOrder:fx:po1")]})
        by_doc = {g["object_id"]: g["follow_up"] for g in cards.run()["gaps"]}
        self.assertNotIn(pe._ATTESTS, by_doc["AssemblyOrderPacket:fx:1"])
        self.assertEqual(by_doc["AssemblyOrderPacket:fx:2"][pe._ATTESTS],
                         "PurchaseOrder:fx:po1")

    def test_a_note_attesting_a_test_order_is_dropped_as_a_test_record(self):
        # mutation caught: the test-record filter reading only the subject and the
        # inbox order key (the note's order rides on ``_ATTESTS``)
        cards = _Cards({"AssemblyOrderPacket:fx:1": [("SalesOrder.required_date",
                                                      "SalesOrder:fx:test")],
                        "AssemblyOrderPacket:fx:2": [("SalesOrder.required_date",
                                                      "SalesOrder:fx:real")]})
        results = {"card_rules": cards.run()}
        with mock.patch.object(pe, "test_records", lambda _st, _d: {"SalesOrder:fx:test"}):
            pe._drop_test_records(cards.st, results, AS_OF)
        left = [g["follow_up"][pe._ATTESTS] for g in results["card_rules"]["gaps"]]
        self.assertEqual(left, ["SalesOrder:fx:real"])
        self.assertEqual(results["card_rules"]["not_evaluated"]["test_record"], 1)

    def test_an_unreadable_active_population_withholds_an_attested_note(self):
        # mutation caught: ``_gap_orders`` ignoring ``_ATTESTS`` (the note escapes the
        # fail-closed path when the closed/historic predicate cannot be read)
        cards = _Cards({"AssemblyOrderPacket:fx:1": [("SalesOrder.required_date",
                                                      "SalesOrder:fx:o1")],
                        "AssemblyOrderPacket:fx:2": [("Product.name", "Product:fx:p1")]})
        results = {"card_rules": cards.run()}

        def unreadable():
            raise RuntimeError("predicate store gone")
        pe._drop_inactive_orders(cards.st, results, AS_OF, inactive=unreadable)
        result = results["card_rules"]
        self.assertEqual([g["object_id"] for g in result["gaps"]],
                         ["AssemblyOrderPacket:fx:2"])
        self.assertEqual(result["status"], "failed")


# --------------------------------------------------------------------------- label requests

class LabelGuards(_EngineBase):

    def stale(self, **read) -> dict[str, dict]:
        return {g["object_id"]: g for g in self.read(kinds=[lc.STALE], limit=1000,
                                                     **read)["gaps"]}

    def test_an_item_named_only_by_its_description_is_named_and_a_blank_name_is_not(self):
        # mutation caught: reading ``name`` only, or taking a blank name as a name
        w = World()
        described = w.obj("Product", {"description": "SYNTH Amber Jar"}, audience="api:items")
        ask(w, links=(("Email.mentions_product", described),))
        blank = w.obj("Product", {"name": "   ", "description": ""}, audience="api:items")
        ask(w, links=(("Email.mentions_product", blank),))
        both = w.obj("Product", {"name": "SYNTH Lid", "description": "SYNTH lid, 38 mm"},
                     audience="api:items")
        ask(w, links=(("Email.mentions_product", both),))
        self.run_engine(w, only=[lc.RULE])
        stale = self.stale()
        self.assertEqual(sorted(stale), sorted([described, both]))
        self.assertIn("for item SYNTH Amber Jar", stale[described]["summary"])
        self.assertIn("for item SYNTH Lid requested", stale[both]["summary"])

    def test_a_subject_saying_the_labels_are_made_closes_the_request(self):
        # mutation caught: dropping the printed/shipped/in-production clause of ``DONE``
        # or the subject check
        w = World()
        open_item = w.obj("Product", {"name": "SYNTH Open"}, audience="api:items")
        ask(w, subject="Label for SYNTH Open", links=(("Email.mentions_product", open_item),))
        done = []
        for subject in ("Artwork is in production", "RE: labels were shipped",
                        "Dieline has been printed"):
            item = w.obj("Product", {"name": f"SYNTH {subject}"}, audience="api:items")
            ask(w, subject=subject, links=(("Email.mentions_product", item),))
            done.append(item)
        self.run_engine(w, only=[lc.RULE])
        self.assertEqual(sorted(self.stale()), [open_item])

    def test_the_label_change_kind_is_off_for_a_reader(self):
        # mutation caught: switching ``label_change`` back on in the registry before it is
        # re-judged (the reader-facing filter would list it)
        w = World()
        item = w.obj("Product", {"name": "SYNTH Bottle"}, audience="api:items")
        ask(w, links=(("Email.mentions_product", item),))
        self.run_engine(w, only=[lc.RULE])
        self.assertEqual(sorted(self.stale()), [item])           # found by the rule
        self.assertEqual(self.stale(include_off=False), {})      # never shown


# --------------------------------------------------------------------------- demand drop

class DemandGuards(_EngineBase):

    def drops(self, **read) -> list[str]:
        return [g["object_id"] for g in self.read(kinds=[ceo.DEMAND], limit=1000,
                                                  **read)["gaps"]]

    def told(self, **read) -> dict:
        rules = {r["rule"]: r for r in self.read(**read)["rules"]}
        return rules[ceo.DEMAND].get("not_evaluated_by_reason") or {}

    def test_a_same_named_customer_elsewhere_with_only_old_orders_explains_nothing(self):
        # mutation caught: skipping on any same-name customer of another source, or on
        # one with any order rather than one in the recent window
        w = World()
        drop, _orders = dropping(w)
        old = w.obj("Customer", {"name": "SYNTH Customer"}, audience="api:customers",
                    binding="sage.customers")
        order(w, old, placed=day(200), binding="sage.oe_orders", audience="api:sage")
        w.obj("Customer", {"name": "synth customer"}, audience="api:customers",
              binding="bc.customers")                             # no orders at all
        summary = self.run_engine(w, only=list(ceo.SET_RULES))
        self.assertEqual(self.drops(), [drop])
        self.assertNotIn("customer_same_name_unlinked",
                         summary["not_evaluated"].get(ceo.DEMAND, {}))

    def test_a_same_name_skip_cites_the_other_customer_and_its_tie(self):
        # mutation caught: a skip cause leaving out the same-name customer's name record,
        # or the tie of its recent order (a reader who may not read either would be told
        # of a skip that rests on it)
        w = World()
        drop, _orders = dropping(w)
        other = w.obj("Customer", {"name": "SYNTH Customer"}, audience="api:sagecust",
                      binding="sage.customers")
        order(w, other, placed=day(12), binding="sage.oe_orders", audience="api:sage")
        tie = w.rows["links"][-1]
        self.assertEqual((tie["link_type"], tie["to_object"]), ("SalesOrder.ordered_by", other))
        tie["markings"] = '["pii"]'
        summary = self.run_engine(w, only=list(ceo.SET_RULES))
        self.assertEqual(self.drops(), [])
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND],
                         {"customer_same_name_unlinked": 1})
        self.assertEqual(self.told().get("customer_same_name_unlinked"), 1)
        self.assertNotIn("customer_same_name_unlinked",
                         self.told(may_read=lambda key: key != "api:sagecust"))
        self.assertNotIn("customer_same_name_unlinked",
                         self.told(held=frozenset({"finance", "l5", "quality_controlled"})))

    def test_one_order_of_unknown_currency_is_a_value_unknown_not_a_second_currency(self):
        # mutation caught: counting an order of no stated currency as its own currency
        # series (the skip would read "currencies mixed" and cite no order of it)
        w = World()
        drop, _orders = dropping(w)
        order(w, drop, placed=day(20), currency=None)
        summary = self.run_engine(w, only=list(ceo.SET_RULES))
        self.assertEqual(self.drops(), [])
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND].get("customer_currencies_mixed"),
                         None)
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND].get(
            "customer_order_value_unknown"), 1)

    def test_a_mixed_currency_skip_cites_the_other_currency_orders(self):
        # the other currency's orders are the skip's cause: a reader who may not read them
        # must not be told a skip that hints at them
        w = World()
        drop, _orders = dropping(w)
        for i in range(3):
            order(w, drop, placed=day(5 + i), total="5000", currency="USD",
                  audience="api:usd")
        summary = self.run_engine(w, only=list(ceo.SET_RULES))
        self.assertEqual(self.drops(), [])
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND],
                         {"customer_currencies_mixed": 1})
        self.assertEqual(self.told().get("customer_currencies_mixed"), 1)
        self.assertNotIn("customer_currencies_mixed",
                         self.told(may_read=lambda key: key != "api:usd"))


if __name__ == "__main__":
    unittest.main()
