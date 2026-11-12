"""A customer order open past its own required date (``late_customer_order``) by the one
sales-order definition (``operating.sales_order_status``).

SYNTHETIC fixtures only (``World`` stores, synthetic handles); evidence label:
isolated-fixture.
"""

from __future__ import annotations

import time
import unittest

from operating import proactive_engine as pe
from operating import sales_order_status as so
from operating.tests.test_proactive_engine import ALL_MARKINGS, Engine, World, admit

RULE = "late_customer_order"
PORTAL, FM = "portal.app_sales_order", "portal.fm_order_entry"
SHIP_AUDIENCE = "api:shipments"


def order(w: World, *, status: str | None = "Open", fm: str | None = None,
          required: str | None = None, fm_required: str | None = None,
          tier: str = "verified", lines: bool = True, **values) -> tuple[str, str | None]:
    """A Portal order (``status``, ``required``) and, with ``fm``, its FileMaker twin (that
    status word, ``fm_required``) joined at ``tier``; with ``lines`` one order line on
    the twin (else the Portal record)."""
    portal = w.obj("SalesOrder", {**({"status": status} if status else {}),
                                  **({"required_date": required} if required else {}),
                                  **values}, binding=PORTAL, audience="api:portal")
    twin = None
    if fm is not None:
        twin = w.obj("SalesOrder", {"fm_status": fm, **(
            {"required_date": fm_required} if fm_required else {})}, binding=FM,
            audience="api:fm")
        w.link("SalesOrder.same_record_as", portal, twin, tier)
    if lines:
        on = twin or portal
        w.link(so.LINE_OF, w.obj("SalesOrderLine", {"quantity": "5"}, audience=(
            "api:fm" if twin else "api:portal")), on)
    return portal, twin


class LateCustomerOrder(Engine):

    def run_rule(self, world: World) -> dict:
        return self.run_engine(world, only=[RULE])

    def test_one_gap_per_late_order_closed_in_any_copy_and_unknowns_counted(self):
        w = World()
        late, late_fm = order(w, fm="Open", fm_required="2026-10-01")
        order(w, fm="Closed", required="2026-10-01")                    # closed in FM
        shipped, _ = order(w, status=None, required="2026-10-01")
        w.link("Shipment.ships", w.obj("Shipment", {"ship_date": "2026-10-02"},
                                       audience=SHIP_AUDIENCE), shipped)   # fulfilled
        partly, _ = order(w, required="2026-10-01")      # still Open beside its shipment
        w.link("Shipment.ships", w.obj("Shipment", {"ship_date": "2026-10-02"},
                                       audience=SHIP_AUDIENCE), partly)    # partly fulfilled
        posted, _ = order(w, required="2026-10-01")
        w.link("SalesOrder.same_record_as", posted, w.obj(
            "SalesOrder", {"order_number": "S1"}, binding="bc.sales_invoices.order"))
        own, _ = order(w, required="2026-10-01", order_date="2026-10-01")   # its own day:
        # judged with its doubt (BIZ-007), never erased
        order(w, status="Revised", required="2026-10-01")              # unknown word
        order(w, status=None, required="2026-10-01")                   # no status
        order(w, fm="Closed", required="2026-10-01", tier="lead")      # lead twin closed
        order(w, required="2026-11-30")                                # not yet due
        order(w)                                                       # no date
        order(w, status="DRAFT", required="2026-10-01")                # not placed
        order(w, required="2026-10-01", lines=False)                   # no goods stated
        summary = self.run_rule(w)
        self.assertEqual(summary["detectors"][RULE], "complete")
        gaps = self.read()["gaps"]
        self.assertEqual(sorted((g["kind"], g["object_id"]) for g in gaps),
                         sorted([(RULE, late_fm), (RULE, own)]))
        doubted = next(g for g in gaps if g["object_id"] == own)
        self.assertIn("the day the order was entered", doubted["summary"])
        (gap,) = [g for g in gaps if g["object_id"] == late_fm]
        self.assertIn("open 18 days past its required date 2026-10-01", gap["summary"])
        self.assertIn("the Portal: Open", gap["summary"])
        self.assertIn("FileMaker: Open", gap["summary"])
        self.assertEqual(summary["not_evaluated"][RULE], {
            "closed_in_unsettled_twin": 1, "no_order_lines": 1, "no_required_date": 1,
            "partly_fulfilled": 1, "status_missing": 1, "status_unmapped": 1})
        self.assertEqual(summary["coverage"][RULE]["total"], 14)
        self.assertEqual(summary["coverage"][RULE]["evaluated"], 8)

    def test_a_reader_who_may_not_read_a_copy_or_a_closure_source_sees_no_gap(self):
        w = World()
        order(w, fm="Open", fm_required="2026-10-01")
        w.obj("Shipment", {"ship_date": "2026-10-02"}, audience=SHIP_AUDIENCE)
        self.run_rule(w)
        self.assertEqual(len(self.read()["gaps"]), 1)
        for hidden in ("api:fm", "api:portal", SHIP_AUDIENCE):
            with self.subTest(hidden=hidden):
                shown = self.read(may_read=lambda key, h=hidden: key != h,
                                  held=ALL_MARKINGS, decide=admit)
                self.assertEqual(shown["gaps"], [])

    def test_a_bulk_date_written_on_the_day_a_load_wrote_the_records_is_no_requested_date(self):
        w = World()
        for _ in range(12):
            # a migration run at 02:10 UTC on Jul 14 (Jul 13 in Toronto) wrote its own day
            # as every record's required date: provenance, so the date is erased
            order(w, required="2026-07-14", portal_created_at="2026-07-14T02:10:00")
        real, _ = order(w, required="2026-09-01", portal_created_at="2026-08-20T15:00:00")
        summary = self.run_rule(w)
        self.assertEqual([g["object_id"] for g in self.read()["gaps"]], [real])
        self.assertEqual(summary["not_evaluated"][RULE], {"required_date_default": 12})

    def test_many_orders_sharing_a_real_deadline_keep_it(self):
        # BIZ-007: 30 orders, 10 of them due on one campaign day, each entered on its own
        # day: a shared deadline is no default, so all ten are judged late
        w = World()
        shared = [order(w, required="2026-09-15",
                        portal_created_at=f"2026-08-{10 + i:02d}T14:00:00")[0]
                  for i in range(10)]
        others = [order(w, required=f"2026-10-{25 + i % 5:02d}",
                        portal_created_at=f"2026-08-{1 + i:02d}T14:00:00")[0]
                  for i in range(20)]
        summary = self.run_rule(w)
        late = sorted(g["object_id"] for g in self.read(limit=100)["gaps"])
        self.assertEqual(late, sorted(shared))     # the others are not yet due
        self.assertNotIn("required_date_default", summary["not_evaluated"].get(RULE, {}))
        self.assertEqual(len(others), 20)

    def test_a_same_day_order_is_judged_and_says_its_doubt(self):
        w = World()
        rush, _ = order(w, required="2026-10-01", portal_created_at="2026-10-01T13:30:00")
        plain, _ = order(w, required="2026-10-02", portal_created_at="2026-09-20T13:30:00")
        summary = self.run_rule(w)
        gaps = {g["object_id"]: g for g in self.read()["gaps"]}
        self.assertEqual(sorted(gaps), sorted([rush, plain]))
        self.assertIn("the day the order was entered", gaps[rush]["summary"])
        self.assertNotIn("the day the order was entered", gaps[plain]["summary"])
        # the doubted one ranks after the plain one though it is a day later (low priority)
        self.assertEqual(list(gaps), [plain, rush])
        self.assertNotIn("required_date_default", summary["not_evaluated"].get(RULE, {}))

    def test_cardinality_every_order_once_within_the_budget(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                # required dates inside the 180-day active window before the engine's
                # fixed day (2026-10-19): an older open order is counted, not listed
                subjects = [order(w, fm="Open", fm_required=f"2026-0{5 + i % 5}-{10 + i % 19}")
                            [1] for i in range(n)]
                started = time.monotonic()
                self.run_rule(w)
                self.assertLess(time.monotonic() - started, 30)
                found = [g["object_id"] for g in self.read(limit=200)["gaps"]]
                self.assertEqual(sorted(found), sorted(subjects))
                self.store.unlink()
                self.inbox.unlink()


SAGE = "portal.sage_oe_order_header"


class OpenAcrossSystems(Engine):
    """Open is decided across every copy and linked system, and only live orders are
    listed late: a long-dead open record is a stale record, an order whose work orders
    are all done is probably fulfilled, and an import stamp is never activity."""

    def run_rule(self, world: World) -> dict:
        return self.run_engine(world, only=[RULE])

    def sage_twin(self, w: World, fm: str, code: str) -> str:
        sage = w.obj("SalesOrder", {"sage_complete": code}, binding=SAGE, audience="api:sage")
        w.link("SalesOrder.erp_order", fm, sage)
        return sage

    def test_a_sage_complete_order_closes_its_filemaker_order_once_linked(self):
        w = World()
        _p, done = order(w, fm="Open", fm_required="2026-10-01")
        self.sage_twin(w, done, "5")                       # Sage: complete
        _p, open_ = order(w, fm="Open", fm_required="2026-10-01")
        self.sage_twin(w, open_, "2")                      # Sage: still to ship
        _p, odd = order(w, fm="Open", fm_required="2026-10-01")
        self.sage_twin(w, odd, "9")                        # a code no rule reads
        summary = self.run_rule(w)
        self.assertEqual([(g["kind"], g["object_id"]) for g in self.read()["gaps"]],
                         [(RULE, open_)])
        self.assertIn("Sage: 2", self.read()["gaps"][0]["summary"])
        self.assertEqual(summary["not_evaluated"][RULE], {"status_unmapped": 1})

    def test_a_long_dead_open_order_is_a_stale_record_and_import_stamps_are_no_activity(self):
        w = World()
        _p, dead = order(w, fm="Open", fm_required="2025-06-01")
        _p, touched = order(w, fm="Open", fm_required="2025-06-01",
                            fm_modified_at="2026-10-02T10:00:00Z")
        # a migration stamped many records' edit time on one day: no activity
        for _ in range(12):
            order(w, status="Closed", portal_updated_at="2026-09-30T03:00:00Z")
        bulk, _ = order(w, required="2025-06-01", portal_updated_at="2026-09-30T03:10:00Z")
        summary = self.run_rule(w)
        kinds = {g["object_id"]: g["kind"] for g in self.read()["gaps"]}
        # one active-population predicate (2b227874e1): an open-on-paper order with no
        # activity in 180 days is counted (historic_order), never listed as current work;
        # the touched order and not the import-stamped ones is the only one listed
        self.assertEqual(kinds, {touched: RULE})
        counted = summary["not_evaluated"][RULE]
        self.assertEqual(counted.get("historic_order"), 2, counted)

    def test_an_order_whose_work_orders_are_all_done_is_probably_fulfilled(self):
        w = World()
        portal, fm = order(w, fm="Open", fm_required="2026-10-01", lines=False)
        line = w.obj("SalesOrderLine", {"quantity": "5"}, audience="api:fm")
        w.link(so.LINE_OF, line, fm)
        w.link("AssemblyOrder.for_order_line",
               w.obj("AssemblyOrder", {"status": "Realized"}, audience="api:fm"), line)
        _p, busy = order(w, fm="Open", fm_required="2026-10-01", lines=False)
        line2 = w.obj("SalesOrderLine", {"quantity": "5"}, audience="api:fm")
        w.link(so.LINE_OF, line2, busy)
        allocation = w.obj("AssemblyOrderAllocation", {}, audience="api:fm")
        w.link("AssemblyOrderAllocation.allocates", allocation,
               w.obj("AssemblyOrder", {"status": "In Progress"}, audience="api:fm"))
        w.link("AssemblyOrderAllocation.for_order_line", allocation, line2)
        summary = self.run_rule(w)
        kinds = {g["object_id"]: g["kind"] for g in self.read()["gaps"]}
        self.assertEqual(kinds, {fm: pe.PROBABLE_SO, busy: RULE})
        self.assertEqual(summary["detectors"][RULE], "complete")
        # a reader who may not read the FileMaker records (the work orders' and the
        # order's) sees neither item
        self.assertEqual(self.read(may_read=lambda key: key != "api:fm")["gaps"], [])

    def test_records_a_rule_cannot_judge_are_counted_per_reason_for_who_may_read_them(self):
        w = World()
        order(w, required="2026-10-01", lines=False)        # no goods stated: unjudged
        order(w, required="2026-10-01", lines=False)
        self.run_rule(w)
        rules = {r["rule"]: r for r in self.read()["rules"]}
        self.assertEqual(rules[RULE]["not_evaluated_by_reason"], {so.NO_LINES: 2})
        hidden = {r["rule"]: r for r in self.read(
            may_read=lambda key: key != "api:portal")["rules"]}
        self.assertEqual(hidden[RULE]["not_evaluated_by_reason"], {})

    def test_day_counts_are_read_at_view_time_never_frozen_at_the_run(self):
        w = World()
        _p, late = order(w, fm="Open", fm_required="2026-10-01")
        self.run_rule(w)
        (now,) = self.read()["gaps"]
        self.assertIn("open 18 days past", now["summary"])
        (later,) = self.read(today=pe.date(2026, 10, 22))["gaps"]
        self.assertIn("open 21 days past", later["summary"])
        self.assertEqual(later["event"]["days"], 21)
        self.assertNotIn("18 days", later["summary"])

    def test_a_closed_or_historic_order_is_not_listed_without_a_customer_po(self):
        w = World()
        live, _ = order(w, required="2026-11-30")
        shut, _ = order(w, status="Shipped", required="2026-11-30")
        old, _ = order(w, required="2024-03-01", order_date="2024-02-01")
        st = pe._Store(self._built(w))
        try:
            inactive = pe._inactive_orders(st, pe.date(2026, 10, 19))
        finally:
            st.close()
        self.assertEqual(inactive, {shut: "closed", old: "historic"})
        self.assertNotIn(live, inactive)

    def _built(self, w: World):
        w.build(self.store)
        return self.store

    def test_cardinality_with_sage_twins_every_order_once(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                late = []
                for i in range(n):
                    _p, fm = order(w, fm="Open", fm_required=f"2026-09-{10 + i % 19}")
                    self.sage_twin(w, fm, "5" if i % 2 else "1")
                    if not i % 2:
                        late.append(fm)
                started = time.monotonic()
                self.run_rule(w)
                self.assertLess(time.monotonic() - started, 30)
                found = [g["object_id"] for g in self.read(limit=200)["gaps"]]
                self.assertEqual(sorted(found), sorted(late))
                self.store.unlink()
                self.inbox.unlink()


class OneDefinition(unittest.TestCase):
    """The rule every caller reads an order by."""

    def test_closed_in_any_record_unknown_never_open(self):
        portal_open = so.record_status({"status": ["READY_TO_SHIP"]})
        self.assertEqual(portal_open.cls, so.OPEN)
        self.assertEqual(so.order_status([portal_open, so.record_status(
            {"fm_status": ["Closed"]})]).cls, so.CLOSED)
        self.assertEqual(so.order_status([portal_open, so.record_status(
            {"bc_status": ["Draft"], "bc_fully_shipped": ["true"]})]).cls, so.CLOSED)
        # Sage's completion code: 3 to 5 complete, 1 and 2 still to ship, others unmapped
        self.assertEqual(so.order_status([portal_open, so.record_status(
            {"sage_complete": ["5"]})]).cls, so.CLOSED)
        self.assertEqual(so.record_status({"sage_complete": ["2"]}).cls, so.OPEN)
        self.assertEqual(so.order_status([portal_open, so.record_status(
            {"sage_complete": ["9"]})]).reason, so.STATUS_UNMAPPED)
        # a linked shipment or invoice closes a record only when nothing of its own says
        # goods are still to ship (BIZ-005): a partly fulfilled one is unknown, never closed
        for values in ({"sage_complete": ["2"]}, {"status": ["PARTIALLY_SHIPPED"]},
                       {"status": ["Open"]},
                       {"bc_status": ["Open"], "bc_fully_shipped": ["false"]},
                       {"bc_fully_shipped": ["false"]}):
            with self.subTest(values=values):
                state = so.record_status(values, fulfilled=True)
                self.assertEqual((state.cls, state.reason), (so.UNKNOWN, so.PARTLY_FULFILLED))
                self.assertEqual(so.order_status([state]).cls, so.UNKNOWN)
        for values in ({}, {"sage_complete": ["5"]}, {"sage_complete": ["2", "5"]},
                       {"bc_status": ["Open"], "bc_fully_shipped": ["true"]},
                       {"status": ["Revised"]}):
            with self.subTest(values=values):
                self.assertEqual(so.record_status(values, fulfilled=True).cls, so.CLOSED)
        self.assertEqual(so.record_status({"status": [{"untrusted_text": "Shipped"}]}).cls,
                         so.CLOSED)


if __name__ == "__main__":
    unittest.main()
