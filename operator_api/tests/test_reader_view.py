"""What the CEO's view shows, at the API boundary: a record page whose plan facts cite by
handle string (the AO page 500), facts in plain labels with money in its currency and no
keys, an answer that does not apply left out, not-found items said against the trail,
test and placeholder records kept off Home's lists (with an N=1/10/60 cardinality probe),
days late counted to one day everywhere, and no code or escape reaching the reader.
Evidence: isolated-fixture."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from operator_api import backend as be
from operator_api import models as m
from operator_api import presentation
from operator_api import test_records

BC = "bc://production/c/purchaseOrders/p1@e1"
MAIL = "graph-mail://box/m1@c1"


class Access:
    def decide(self, handles):
        return {h: None for h in handles}


class View:
    access = Access()

    @staticmethod
    def _can_open(handle):
        return True


class Backend:
    _source = staticmethod(be.WorkspaceBackend._source)

    @staticmethod
    def _conflicts(view, raw, label):
        return []

    @staticmethod
    def freshness(page):
        return m.Freshness()


def page(**extra):
    """A record's workspace page as the console returns it, every citation a handle
    string where a plan writes one."""
    values = [{"property": p, "value": v, "citation": {"l1_handle": BC}}
              for p, v in (("bc_fully_received", False), ("po_number", "106093"),
                           ("total_cost", "9680"), ("currency", "USD$"),
                           ("external_id", "fm-cust-89"), ("order_date", "2026-07-10"))]
    return {"object_id": "PurchaseOrder:bc.purchase_orders:p1",
            "card": {"object": {"object_type": "PurchaseOrder", "display": "106093"},
                     "values": values + [{"property": "status", "value": "Draft",
                                          "citation": BC}]},
            "answers": {"status": {"state": "late", "text": "Late by 3 days",
                                   "facts": [{"citation": BC, "openable": True},
                                             "a stray string"]},
                        "supplier": {"state": "not_established", "applicable": False,
                                     "text": "Not established: this record is not an order "
                                             "linked to a supplier"}},
            "timeline": {"events": [{"event_type": "po.posted", "at": "2026-07-07",
                                     "citation": BC}, "not an event"]},
            "documents": {"result": {"documents": [{"title": "Invoice", "citation": MAIL},
                                                   {"title": "x", "citation": 7}]}},
            "around": {"hops": [{"result": {"objects": [
                {"object_id": "PurchaseOrderLine:bc.purchase_order_lines:{\"id\":\"l1\"}",
                 "object_type": "PurchaseOrderLine"}]}}]},
            **extra}


class RecordPage(unittest.TestCase):
    def test_a_page_whose_citations_are_handle_strings_renders(self):
        got = be._page_model(Backend(), View(), page())
        texts = [f.text for f in got.facts]
        # the string-cited value is a fact like any other; nothing raised
        self.assertIn("Status: Draft", texts)
        self.assertEqual([e.source.handle for e in got.timeline if e.source], [BC])
        self.assertEqual([i.source.handle for i in got.linked], [MAIL])
        self.assertEqual(got.status.text, "Late by 3 days")
        self.assertEqual([s.handle for s in got.status.sources], [BC])

    def test_facts_read_as_labels_with_currency_and_no_keys(self):
        got = be._page_model(Backend(), View(), page())
        texts = [f.text for f in got.facts]
        self.assertIn("Fully received (BC): no", texts)
        self.assertIn("PO number: 106093", texts)
        self.assertIn("Total cost: 9,680.00 USD", texts)   # its record's currency
        self.assertIn("Currency: USD", texts)               # USD$ is USD
        self.assertIn("Order date: Jul 10, 2026", texts)
        self.assertFalse(any("fm-cust-89" in t or "External" in t for t in texts))
        # the related line is named by its kind, never its key
        self.assertEqual([r.label for r in got.related], ["PO line"])
        # an answer that does not apply to the record is not said
        self.assertNotIn("not an order linked to a supplier", got.summary)
        # the cited field opens first on the source page
        source = next(f for f in got.facts if f.text.startswith("Total cost")).sources[0]
        self.assertTrue(source.open_url.endswith("&field=total_cost"))

    def test_not_found_is_said_against_what_the_trail_shows(self):
        step = lambda kind, title: m.TrailStep(kind=kind, title=title, check="lead")  # noqa
        trail = m.EvidenceTrail(kind="purchase_order", title="How we know", trails=[
            m.PoTrail(subject=m.ObjectRef(object_id="o", type="PurchaseOrder", label="106093",
                                          open_url="/o"),
                      steps=[step("follow_up", "Email: Billing Invoice"),
                             step("follow_up", "Email: Billing Invoice - Payment Reminder"),
                             step("verdict", "Not late: invoice")])])
        items = [m.NotFound(what="Vendor invoice of Purchase order 106093",
                            where_it_would_live="Ask Finance. Vendor invoice: Not found"),
                 m.NotFound(what="GL posting of Purchase order 106093",
                            where_it_would_live="Ask Finance. GL posting: Not found")]
        got = be.reconciled_not_found(items, trail)
        self.assertEqual(got[0].where_it_would_live,
                         "Ask Finance. Not on record in the systems searched; 2 messages in "
                         "How we know mention it (not confirmed as this order's)")
        self.assertEqual(got[1], items[1])   # nothing on the trail talks about it
        self.assertEqual(be.reconciled_not_found(items, None), items)


def inbox(n_real, n_test, *, as_of="2026-10-31"):
    gaps = []
    for i in range(n_real + n_test):
        gaps.append({"object_id": f"PurchaseOrder:bc.purchase_orders:{i}",
                     "object_type": "PurchaseOrder", "days_late": 200 - i,
                     "summary": "An open purchase order has no supplier confirmation",
                     "citations": []})
    labels = {g["object_id"]: f"PO{100000 + i}" for i, g in enumerate(gaps)}
    suppliers = {g["object_id"]: ("Laura TEST" if i >= n_real else "DSM")
                 for i, g in enumerate(gaps)}
    return ({"status": "read", "gaps": gaps, "late_total": len(gaps), "as_of": as_of},
            labels, suppliers)


class TestRecords(unittest.TestCase):
    def test_placeholders_are_off_home_and_real_records_stay_at_any_size(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                box, labels, suppliers = inbox(n, 2)
                shown, labels, suppliers = be.without_tests(box, labels, suppliers)
                late, _ = be.attention_items(shown, labels, limit=100, suppliers=suppliers)
                ids = [p.object_id for p in late]
                self.assertEqual(len(ids), n)                 # none lost
                self.assertEqual(len(set(ids)), n)            # none twice
                self.assertTrue(all(p.supplier == "DSM" for p in late))
                self.assertEqual(shown["late_total"], n)

    def test_an_order_that_moves_up_past_a_left_out_test_record_keeps_its_name(self):
        # Home labels the subjects it reads; a test record left out lets the next late
        # order onto the list, which must be named and carry its supplier, never
        # "Purchase order" with no supplier.
        from contextlib import nullcontext
        n = be.LATE_LIST_LIMIT + 2
        gaps = [{"object_id": f"PurchaseOrder:bc.purchase_orders:{i}",
                 "object_type": "PurchaseOrder", "days_late": 200 - i,
                 "summary": "An open purchase order has no supplier confirmation",
                 "citations": []} for i in range(n)]

        class Workspace:
            def gap_inbox(self, limit=None):
                return {"status": "read", "gaps": gaps}

            def _request(self):
                return nullcontext()

            def _call(self, tool, args):
                i = int(args["object_id"].rsplit(":", 1)[1])
                vendor = "Lorpon Labels (DO NOT USE)" if i == 1 else "DSM"
                return {"object": {"display": f"PO{100000 + i}"},
                        "values": [{"property": "vendor_name", "value": vendor}]}

        shown, labels, suppliers = be.without_tests(*be.WorkspaceBackend._attention(Workspace()))
        late, _ = be.attention_items(shown, labels, suppliers=suppliers)
        self.assertEqual(len(late), be.LATE_LIST_LIMIT)
        self.assertEqual([p.label for p in late],
                         [f"PO{100000 + i}" for i in range(n) if i != 1][:be.LATE_LIST_LIMIT])
        self.assertTrue(all(p.supplier == "DSM" for p in late))

    def test_the_briefing_leaves_out_a_test_late_order_past_its_top_as_home_does(self):
        # A placeholder late order ranked past the top was counted in the briefing (23 late)
        # and listed in its "+N more", while Home left it out (22 late).
        from contextlib import nullcontext
        from operating import briefing as bf
        n, test_at = 20, 18
        items = [{"id": f"g{i}", "rank": i + 1, "event_type": "po_late",
                  "object_type": "PurchaseOrder",
                  "object_id": f"PurchaseOrder:bc.purchase_orders:{i}",
                  "facts": {"days": 100}, "summary": "late"} for i in range(n)]
        found = {"status": "read", "items": items, "count": n, "late_total": n,
                 "groups": [{"event_type": "po_late", "label": "Late purchase order",
                             "count": n, "in_top": bf.TOP_ITEMS, "new_since_visit": 0,
                             "acknowledged": 0}]}

        class Workspace:
            def _request(self):
                return nullcontext()

            def _call(self, tool, args):
                i = int(args.get("object_id", args.get("from", "x:0")).rsplit(":", 1)[1])
                vendor = "Lorpon Labels (DO NOT USE)" if i == test_at else "DSM"
                return {"object": {"display": f"PO{100000 + i}"},
                        "values": [{"property": "vendor_name", "value": vendor}]}

        shown, labels = be.briefing_without_tests(Workspace(), found, frozenset())
        said = be.briefing_model(shown, [], digest="", tests_hidden=shown["tests_hidden"])
        self.assertEqual(said.late_total, n - 1)
        self.assertEqual(said.count, n - 1)
        self.assertEqual([(g.count, g.in_top) for g in said.groups],
                         [(n - 1, bf.TOP_ITEMS)])
        self.assertIn(f"{n - 1} POs", said.today)
        self.assertEqual(said.test_records_hidden, "1 test record hidden")
        self.assertNotIn(f"PurchaseOrder:bc.purchase_orders:{test_at}",
                         [i["object_id"] for i in shown["items"]])
        self.assertEqual(len({i["object_id"] for i in shown["items"]}), n - 1)

    def test_the_rule_names_only_what_says_it_is_a_test(self):
        for text in ("Lorpon Labels (DO NOT USE)", "Crown Grooming (Do Not Use)", "TEST",
                     "Laura TEST", "UAT-SO100004", "uat-AO-26001A"):
            self.assertTrue(test_records.is_test([text]), text)
        for party in ("TEST", "Laura TEST"):
            self.assertTrue(test_records.is_test([], parties=[party]), party)
        # a supplier or customer card marked (DO NOT USE) is retired, not a test
        for party in ("Lorpon Labels (DO NOT USE)", "Crown Grooming (Do Not Use)"):
            self.assertFalse(test_records.is_test(["PO100035"], parties=[party]), party)
        for text in ("CAMBRIDGE MATERIALS TESTING LIMITED", "pH Test Strips", "1UP CARGO",
                     "Test Kitchen Inc", "Testa Foods", "XYZ Lab Test", "Covid test", "S-ORD101148", "PO 5275 UAT-ready",
                     "", None):
            self.assertFalse(test_records.is_test([text]), text)

    def test_only_names_and_numbers_are_judged_never_free_text(self):
        box, labels, suppliers = inbox(2, 0)
        box["gaps"][0]["summary"] = "The supplier says do not use lot 4 (DO NOT USE)"
        shown, _, _ = be.without_tests(box, labels, suppliers)
        self.assertEqual(len(shown["gaps"]), 2)
        item = {"object_id": "o", "summary": "(DO NOT USE)", "why": "TEST"}
        self.assertFalse(be.briefing_test(item, {"o": "PO100001"}, frozenset(), {"o": "DSM"}))
        self.assertTrue(be.briefing_test(item, {"o": "PO100001"}, frozenset(),
                                         {"o": "Laura TEST"}))
        self.assertFalse(be.briefing_test(item, {"o": "PO100001"}, frozenset(),
                                          {"o": "Lorpon Labels (DO NOT USE)"}))

    def test_a_late_order_with_a_retired_supplier_card_stays_late_and_counted(self):
        """An open order placed with a supplier whose card is now marked (DO NOT USE) (a
        FileMaker-era order on a retired BC vendor card) is the business's order: Home and
        the late answer list and count it, at 1, 10 and 60 orders, none lost."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                box, labels, suppliers = inbox(n, 0)
                retired = box["gaps"][-1]["object_id"]
                suppliers[retired] = "Lorpon Labels (DO NOT USE)"
                shown, labels, suppliers = be.without_tests(box, labels, suppliers)
                late, _ = be.attention_items(shown, labels, limit=100, suppliers=suppliers)
                self.assertEqual(len({p.object_id for p in late}), n)
                self.assertIn(retired, [p.object_id for p in late])
                self.assertEqual(shown["late_total"], n)
                self.assertNotIn("tests_hidden", shown)

    def test_the_reader_is_told_how_many_test_records_were_left_out(self):
        box, labels, suppliers = inbox(3, 2)
        shown, _, _ = be.without_tests(box, labels, suppliers)
        self.assertEqual(test_records.hidden_words(shown["tests_hidden"]),
                         "2 test records hidden")
        self.assertIsNone(test_records.hidden_words(0))
        found = {"status": "read", "count": 3, "items": [], "groups": [], "checks": [],
                 "routing": {}, "complete": True}
        said = be.briefing_model(found, [], digest="", tests_hidden=1)
        self.assertEqual(said.test_records_hidden, "1 test record hidden")

    def test_the_deployments_own_list_lives_with_the_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(test_records.listed(Path(tmp), {}), frozenset())
            (Path(tmp) / test_records.LIST_FILE).write_text(json.dumps(
                {"records": ["PO100001"], "note": "a test order"}), encoding="utf-8")
            named = test_records.listed(Path(tmp), {})
            box, labels, suppliers = inbox(3, 0)
            shown, _, _ = be.without_tests(box, labels, suppliers, named=named)
            self.assertEqual([g["object_id"].rsplit(":", 1)[1] for g in shown["gaps"]],
                             ["0", "2"])
            (Path(tmp) / test_records.LIST_FILE).write_text("{not json", encoding="utf-8")
            self.assertEqual(test_records.listed(Path(tmp), {}), frozenset())


class ProfitabilityOrders(unittest.TestCase):
    def test_a_staging_order_is_neither_counted_nor_read(self):
        from operating import customer_profit as cp

        orders = ["S-ORD101148", "UAT-SO100004", "S-ORD101150"]
        read = []

        def call(name, arguments):
            if name == "plx.around" and arguments["from"] == "Customer:c":
                return {"objects": [{"object_id": f"SalesOrder:{n}", "display": {"untrusted_text": n}}
                                    for n in orders], "completeness": {}}
            if name == "plx.around":
                return {"objects": [], "completeness": {}}
            read.append(arguments["object_id"])
            return {"values": [{"property": "order_date", "value": "2026-09-01"}]}
        period = cp.Period(date(2026, 1, 1), date(2026, 11, 1), "this year", True)
        got, _w, _c, _o, skipped = cp.customer_orders(
            call, "Customer:c", period,
            lambda number: test_records.is_test((number,)))
        self.assertEqual([o.number for o in got], ["S-ORD101148", "S-ORD101150"])
        self.assertEqual(skipped, 1)
        self.assertNotIn("SalesOrder:UAT-SO100004", read)


class OneDayCount(unittest.TestCase):
    def test_days_late_are_counted_to_today_on_every_list(self):
        box, _, _ = inbox(2, 0, as_of="2026-10-31")
        aged = be.aged_to_today(box, date(2026, 11, 1))
        self.assertEqual([g["days_late"] for g in aged["gaps"]], [201, 200])
        self.assertEqual(aged["days_as_of"], "2026-11-01")
        same = be.aged_to_today(box, date(2026, 10, 31))
        self.assertEqual([g["days_late"] for g in same["gaps"]], [200, 199])
        briefing = be.briefing_aged({"as_of": "2026-10-31", "items": [
            {"event_type": "po_late", "facts": {"days": 129}},
            {"event_type": "lot_near_expiry", "facts": {"days": 5}}]}, date(2026, 11, 1))
        self.assertEqual([i["facts"]["days"] for i in briefing["items"]], [130, 5])

    def test_the_po_page_counts_to_the_same_day_as_home(self):
        box, _, _ = inbox(1, 0, as_of="2026-10-31")
        view = SimpleNamespace(gap_inbox=lambda: box)
        oid = box["gaps"][0]["object_id"]
        [(po, day)] = be.listed_late(view, oid, "PO100000", date(2026, 11, 3))
        home = be.aged_to_today(box, date(2026, 11, 3))
        self.assertEqual(po.days_late, home["gaps"][0]["days_late"])
        self.assertEqual((po.days_late, day), (203, "2026-11-03"))

    def test_rounding_alike_quantities_are_no_risk(self):
        risk = {"object_id": "AssemblyOrder:x", "object_type": "AssemblyOrder",
                "kind": "conflicting_quantity", "citations": [],
                "summary": "AssemblyOrder.ordered_boxes: sources report different quantity "
                           "values (1257, 1257.1666666666667 box)"}
        real = {**risk, "object_id": "AssemblyOrder:y",
                "summary": "AssemblyOrder.ordered_boxes: sources report different quantity "
                           "values (1200, 1257.17 box)"}
        _, at_risk = be.attention_items({"gaps": [risk, real]}, {})
        self.assertEqual([r.object_id for r in at_risk], ["AssemblyOrder:y"])


class DidYouMean(unittest.TestCase):
    def test_a_lone_business_record_beside_a_mapping_is_the_answers_record(self):
        po = {"object_id": "PurchaseOrder:bc.purchase_orders:p1", "object_type":
              "PurchaseOrder", "display": "106093"}
        mapping = {"object_id": "BcEntityMapping:portal.app_bc_entity_map:m1",
                   "object_type": "BcEntityMapping", "display": "106093"}
        other = {"object_id": "PurchaseOrder:portal.app_purchase_order:p2",
                 "object_type": "PurchaseOrder", "display": "PO#106093"}
        choices, promoted = be.business_choices([
            {"mention": "106093", "candidates": [po, mapping]},
            {"mention": "PO 7", "candidates": [po, other, mapping]}])
        self.assertEqual([r.object_id for r in promoted], [po["object_id"]])
        self.assertEqual(promoted[0].label, "106093")
        self.assertTrue(promoted[0].open_url.startswith("/object/PurchaseOrder/"))
        # per mention: the second still offers its two business records, no mapping
        self.assertEqual([r.object_id for r in choices], [po["object_id"], other["object_id"]])
        self.assertEqual(be.business_choices([{"mention": "x", "candidates": [mapping]}]),
                         ([], []))


class Words(unittest.TestCase):
    def test_a_timestamp_in_a_sentence_is_its_toronto_day(self):
        self.assertEqual(presentation.date_words("as of 2026-11-01T02:00:00Z"),
                         "as of Oct 31, 2026")
        self.assertEqual(presentation.date_words("needed by 2026-06-24"),
                         "needed by Jun 24, 2026")

    def test_a_process_headline_reads_plainly(self):
        self.assertEqual(presentation.status_words(
            "Missing: supplier confirmation, receipt; held, its status not reached yet: "
            "purchase order; 1 more step not visible to you"),
            "No supplier confirmation or receipt on record yet. On record but not issued "
            "yet: purchase order. 1 more step is in places you can't open.")
        # an event not established is said as a question still open, never "either way"
        self.assertEqual(presentation.status_words(
            "Missing: supplier confirmation; not established: vendor shipped"),
            "No supplier confirmation on record yet. Not known yet whether the vendor shipped.")
        self.assertEqual(presentation.status_words("Not established: shipment"),
                         "Not known yet: shipment.")
        self.assertEqual(presentation.status_words("Late by 3 days"), "Late by 3 days")
        # a held record is said by its kind: a receipt is posted, never "issued"
        self.assertEqual(presentation.status_words(
            "Held, its status not reached yet: receipt, purchase order, quality check"),
            "On record but not posted yet: receipt. On record but not issued yet: purchase "
            "order. On record but not at this step yet: quality check.")

    def test_no_code_or_escape_reaches_the_reader(self):
        answer = m.AnswerPayload(
            answer_id="a", question="q", outcome="partial", conversation_id="c", latency_ms=1,
            direct_answer="2 orders have no checked margin (claim check failed).",
            hidden=m.Hidden(count=0), freshness=m.Freshness(),
            not_found=[m.NotFound(what="Line not invoiced: S-ORD101102: LINE_NOT_INVOICED: "
                                       "no posted invoice line", where_it_would_live="Finance")],
            facts=[m.Fact(text="Blocked: Sales_x0020_Shipment", sources=[m.Source(
                system="BC", handle="bc://x/LINE_NOT_INVOICED", label="BC")])])
        got = presentation.polished(answer)
        self.assertEqual(got.direct_answer, "2 orders have no checked margin (figures not "
                                             "confirmed against the records).")
        self.assertEqual(got.not_found[0].what,
                         "Line not invoiced: S-ORD101102: no posted invoice line")
        self.assertEqual(got.facts[0].text, "Blocked: Sales Shipment")
        self.assertEqual(got.facts[0].sources[0].handle, "bc://x/LINE_NOT_INVOICED")


if __name__ == "__main__":
    unittest.main()


class BriefingEventTypes(unittest.TestCase):
    def test_every_engine_event_type_is_one_the_api_returns(self):
        # An event type the engine emits but the response model refuses fails the whole
        # /api/briefing with a 500 (Home's briefing): the two lists are one.
        from typing import get_args
        from operating import briefing
        self.assertEqual(set(get_args(m.EventType)), set(briefing.EVENT_TYPES))


class AlikeChoices(unittest.TestCase):
    def test_choices_that_read_alike_say_their_system_and_their_own_values(self):
        # "whats going on w/ po PO100040" offered "PO100040" and "PO100040": no way to pick
        from contextlib import nullcontext
        bc = 'PurchaseOrder:bc.purchase_orders:{"id":"b1"}'
        portal = 'PurchaseOrder:portal.app_purchase_order:{"id":"c1"}'
        vendors = {bc: "New Directions Aromatics", portal: "FlavorSum Canada ULC"}

        class Workspace:
            def _request(self):
                return nullcontext()

            def _call(self, tool, args):
                return {"values": [{"property": "vendor_name",
                                    "value": vendors[args["object_id"]]}]}

        refs = [m.ObjectRef(object_id=o, type="PurchaseOrder", label="PO100040",
                            open_url="/x") for o in (bc, portal)]
        refs.append(m.ObjectRef(object_id="Supplier:bc.vendors:v", type="Supplier",
                                label="DSM", open_url="/y"))
        said = [c.label for c in be.told_apart(Workspace(), refs)]
        self.assertEqual(said, ["PO100040 in Business Central (New Directions Aromatics)",
                                "PO100040 in the Portal (FlavorSum Canada ULC)", "DSM"])
