"""The oracle's own three-way reading and the po_three_way stratum (injected transport).

Evidence label: ``isolated-fixture`` over ``evals/oracle/fixture_world.py``; nothing here is
live. Purchase order ``i`` of the fixture world: every PO has one Portal receipt; PO 5 and
every fifth are received 2 short on their first line; every seventh has no BC invoice;
every sixth is SUBMITTED with an Open invoice; the BC invoice of every third carries no
order No. (found by the vendor invoice No. on the receipt).
"""

from __future__ import annotations

import unittest

from evals.oracle import BcReads, PortalReads, Sources
from evals.oracle import cross_sample as cs
from evals.oracle import keys as K
from evals.oracle.fixture_world import build_live_shaped_world, build_world, po_number
from evals.oracle.three_way import ASPECTS, decided, reading


def sources_of(world, *, bc: bool = True) -> Sources:
    return Sources(PortalReads(world.read_relation), BcReads(world.bc_port()) if bc else None)


def po_key(i: int) -> tuple:
    return (("id", f"fx-po-{i}"),)


def values(entry) -> dict:
    return {name: entry["aspects"][name]["value"] for name in ASPECTS}


class ReadingTest(unittest.TestCase):
    def setUp(self):
        self.world = build_world()

    def test_a_matched_purchase_order_reads_every_stage_present_and_both_sides_agree(self):
        entry = reading(sources_of(self.world), K.PO, po_key(1))
        self.assertEqual(entry["status"], "read")
        self.assertEqual(values(entry), {
            "stage:purchase_order": "present", "stage:receipt": "present",
            "stage:vendor_invoice": "present", "side:ordered_received": True,
            "side:received_invoiced": True})
        self.assertEqual(decided(entry), list(ASPECTS))
        # Each aspect names the sources its evidence lives in.
        self.assertEqual(entry["aspects"]["stage:vendor_invoice"]["sources"],
                         ["bc:purchaseInvoices"])
        self.assertEqual(entry["aspects"]["side:ordered_received"]["sources"],
                         sorted([K.PO_LINE, K.RECEIPT_LINE]))

    def test_a_short_receipt_disagrees_on_ordered_vs_received_only(self):
        entry = reading(sources_of(self.world), K.PO, po_key(5))
        self.assertEqual(values(entry)["side:ordered_received"], False)
        self.assertEqual(entry["aspects"]["side:ordered_received"]["reason"], "item_disagrees")
        # BC invoiced what was received.
        self.assertEqual(values(entry)["side:received_invoiced"], True)

    def test_no_invoice_is_absent_and_leaves_received_vs_invoiced_undecided(self):
        entry = reading(sources_of(self.world), K.PO, po_key(7))
        self.assertEqual(values(entry)["stage:vendor_invoice"], "absent")
        self.assertEqual(entry["aspects"]["stage:vendor_invoice"]["reason"], "no_invoice_found")
        self.assertIsNone(values(entry)["side:received_invoiced"])

    def test_an_invoice_found_by_vendor_invoice_number_counts(self):
        entry = reading(sources_of(self.world), K.PO, po_key(3))   # no BC order No.
        self.assertEqual(values(entry)["stage:vendor_invoice"], "present")

    def test_draft_invoices_and_draft_or_cancelled_orders_follow_portal(self):
        world = build_world()
        for record in world.bc["purchaseInvoices"]:
            if record["fields"].get("orderNumber") == po_number(1):
                record["fields"]["status"] = "Draft"
        entry = reading(sources_of(world), K.PO, po_key(1))
        self.assertEqual((values(entry)["stage:vendor_invoice"],
                          entry["aspects"]["stage:vendor_invoice"]["reason"]),
                         ("absent", "only_draft_or_cancelled"))
        for row in world.relations[K.PO]:
            if row["id"] == "fx-po-2":
                row["status"] = "DRAFT"
            if row["id"] == "fx-po-4":
                row["status"] = "CANCELLED"
        self.assertEqual(values(reading(sources_of(world), K.PO, po_key(2)))[
            "stage:purchase_order"], "absent")
        self.assertIsNone(values(reading(sources_of(world), K.PO, po_key(4)))[
            "stage:purchase_order"])

    def test_without_bc_the_bc_aspects_are_undecided_never_absent(self):
        entry = reading(sources_of(self.world, bc=False), K.PO, po_key(1))
        self.assertIsNone(values(entry)["stage:vendor_invoice"])
        self.assertEqual(entry["aspects"]["stage:vendor_invoice"]["reason"], "bc_not_read")
        self.assertIsNone(values(entry)["side:received_invoiced"])
        self.assertEqual(values(entry)["side:ordered_received"], True)

    def test_a_purchase_order_without_a_portal_receipt_leaves_the_receipt_undecided(self):
        world = build_world()
        world.relations[K.RECEIPT] = [r for r in world.relations[K.RECEIPT]
                                      if r["purchase_order_id"] != "fx-po-1"]
        entry = reading(sources_of(world), K.PO, po_key(1))
        self.assertIsNone(values(entry)["stage:receipt"])     # a twin system may hold one
        self.assertIsNone(values(entry)["side:ordered_received"])

    def test_an_unreadable_root_is_blocked_not_wrong(self):
        self.assertEqual(reading(sources_of(self.world), K.PO, (("id", "nope"),))["status"],
                         "blocked")
        self.assertEqual(reading(sources_of(self.world), K.RECEIPT, (("id", "x"),))["reason"],
                         "root_not_a_portal_po")
        world = build_world()
        world.fail.add(K.PO_LINE)
        entry = reading(sources_of(world), K.PO, po_key(1))
        self.assertEqual(entry["status"], "blocked")
        self.assertTrue(entry["reason"].startswith("read_failed_"))


class StratumTest(unittest.TestCase):
    def test_the_stratum_draws_only_served_purchase_orders_with_something_to_match(self):
        world = build_world()
        sources = sources_of(world)
        drawn = cs.sample(sources, run_id="seed-a", count=28, documents=world.documents())
        entry = drawn.kinds[cs.THREE_WAY]
        self.assertEqual(entry["status"], "read")
        self.assertEqual(entry["tried"], list(cs.THREE_WAY_TRIED))
        self.assertGreater(entry["candidates"], 0)
        items = [i for i in drawn.items if i.kind == cs.THREE_WAY]
        self.assertTrue(items)
        receipts = {r["purchase_order_id"] for r in world.relations[K.RECEIPT]}
        for item in items:
            self.assertEqual((item.relation, item.root_kind), (K.PO, "purchase_order"))
            self.assertIn(dict(item.key)["id"], receipts)
            # Identifier from another system's record, like every find question.
            self.assertNotEqual(item.reference_system, "portal")
            self.assertEqual(drawn.readings[item.hash16]["status"], "read")
        self.assertEqual(entry["readings"], {"read": len(items)})
        # A process question hashes apart from a find question on the same root.
        finds = {i.hash16 for i in drawn.items if i.kind != cs.THREE_WAY}
        self.assertFalse(finds & {i.hash16 for i in items})

    def test_a_bc_invoice_alone_makes_a_candidate(self):
        world = build_live_shaped_world()       # app_receipt is empty, as on the replica
        sources = sources_of(world)
        drawn = cs.sample(sources, run_id="live-shaped-a", count=28,
                          documents=world.documents())
        entry = drawn.kinds[cs.THREE_WAY]
        invoiced = {r["fields"].get("orderNumber") for r in world.bc["purchaseInvoices"]}
        numbers = {r["id"]: r["po_number"] for r in world.relations[K.PO]}
        for item in (i for i in drawn.items if i.kind == cs.THREE_WAY):
            self.assertIn(numbers[dict(item.key)["id"]], invoiced)
        self.assertGreater(entry["candidates"], 0)
        self.assertTrue([i for i in drawn.items if i.kind == cs.THREE_WAY])

    def test_no_candidate_is_empty_with_the_source_tried(self):
        world = build_world()
        world.relations[K.RECEIPT] = []
        drawn = cs.sample(sources_of(world, bc=False), run_id="seed-a", count=28,
                          documents=world.documents())
        entry = drawn.kinds[cs.THREE_WAY]
        self.assertEqual((entry["status"], entry["reason"], entry["candidates"]),
                         ("empty", "no_candidates", 0))
        self.assertGreater(entry["roots"], 0)
        self.assertEqual(entry["tried"], list(cs.THREE_WAY_TRIED))
        self.assertFalse([i for i in drawn.items if i.kind == cs.THREE_WAY])
        self.assertEqual(drawn.readings, {})

    def test_a_failed_purchase_order_read_is_unavailable_not_empty(self):
        world = build_world()
        world.fail.add(K.PO)
        drawn = cs.sample(sources_of(world), run_id="seed-a", count=28,
                          documents=world.documents())
        self.assertEqual(drawn.kinds[cs.THREE_WAY]["status"], "unavailable")

    def test_not_drawn_when_the_product_holds_no_purchase_order(self):
        world = build_world()
        drawn = cs.sample(sources_of(world), run_id="seed-a", count=28,
                          documents=world.documents(), record_kinds=["sales_order"])
        self.assertEqual(drawn.kinds[cs.THREE_WAY]["status"], "not_sampled")
        self.assertGreater(drawn.kinds[cs.THREE_WAY]["candidates"], 0)


if __name__ == "__main__":
    unittest.main()
