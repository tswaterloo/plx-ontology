"""Operations alerts (``operating.proactive_stock``) through the engine, the per-reader inbox
and the briefing: stock not moving, lots with stock expiring, assembly orders short of
components, certificates expiring and freight bills far above their norm.

SYNTHETIC fixtures only (``World`` stores, synthetic codes); evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
import time
import unittest
from unittest import mock

from operating import briefing
from operating import proactive_engine as pe
from operating import proactive_stock as ops
from operating.tests.test_proactive_business import (NO_FINANCE, item, money, purchase,
                                                     sales_order)
from operating.tests.test_proactive_engine import AS_OF, Engine, World

LOTS = (("InventoryLot", "quantity_on_hand", "InventoryLot.of_item"),)


def ago(days: int) -> str:
    return (AS_OF - timedelta(days=days)).isoformat()


def ahead(days: int) -> str:
    return (AS_OF + timedelta(days=days)).isoformat()


def ordered(w: World, product: str, days_ago: int, *, status: str = "Open") -> str:
    order = w.obj("SalesOrder", {"status": status, "order_date": ago(days_ago)},
                  binding="portal.app_sales_order", audience="api:portal")
    line = w.obj("SalesOrderLine", {"ordered_quantity": ("5", "kg")}, audience="api:portal")
    w.link("SalesOrderLine.line_of", line, order)
    w.link("SalesOrderLine.for_item", line, product)
    return order


def priced(w: World, product: str, cost: str = "2.50", base: str = "KG") -> None:
    for row in list(w.rows["object_values"]):
        if row["object_id"] == product and row["property"] == "code":
            w.rows["object_values"].append(dict(row, property="bc_unit_cost", value=cost,
                                                markings='["finance"]'))
            w.rows["object_values"].append(dict(row, property="bc_base_uom", value=base))
    money(w, product, "bc_unit_cost", "CAD")


def lot(w: World, product: str, qty: str, expiry: str, number: str = "SYN-LOT-1") -> str:
    oid = w.obj("InventoryLot", {"quantity_on_hand": (qty, "kg"), "expiry_date": expiry,
                                 "lot_number": number}, audience="api:stock")
    w.link("InventoryLot.of_item", oid, product)
    return oid


def assembly(w: World, start: str, components, *, status: str = "Allocated",
             code: str = "AO-SYN1", binding: str = "fx") -> str:
    order = w.obj("AssemblyOrder", {"status": status, "start_date": start, "ao_code": code},
                  audience="api:assembly", binding=binding)
    for product, planned, consumed in components:
        part = w.obj("AssemblyOrderComponent", {
            "planned_quantity": (planned, "kg", "planned"),
            "consumed_quantity": (consumed, "kg", "actual")}, audience="api:assembly")
        w.link("AssemblyOrderComponent.component_of", part, order)
        w.link("AssemblyOrderComponent.uses_item", part, product)
    return order


def coa(w: World, expiry: str, *, lot_no: str | None = "SYN-L1",
        supplier: str | None = "SYNTH Supplier", audience: str = "graph:coa",
        product: str | None = "SYNTH Material") -> str:
    return w.obj("CertificateOfAnalysis", {"expiry_date": expiry, "supplier": supplier,
                                           "product": product, "lot": lot_no,
                                           "tier": "lead"}, audience=audience)


def freight(w: World, carrier: str, amount: str, day: int, *, currency: str | None = "CAD",
            number: str | None = None) -> str:
    oid = w.obj("FreightBill", {"amount": amount, "carrier": carrier,
                                "port_of_loading": "SYNTH Port A",
                                "port_of_discharge": "SYNTH Port B",
                                "document_date": ago(day),
                                "invoice_number": number or f"FB-{w.n + 1}"},
                audience="graph:freight", markings='["finance"]')
    money(w, oid, "amount", currency)
    return oid


class OpsAlerts(Engine):

    def run_rules(self, w: World, **options) -> dict:
        with mock.patch.object(ops, "declared_starts", return_value=("start_date",)), \
                mock.patch.object(ops, "assembly_keeper", return_value="fx"):
            return self.run_engine(w, only=list(ops.RULES),
                                   on_hand_sources=options.pop("on_hand", LOTS), **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    def test_stock_not_moving_names_its_bucket_and_value_and_a_moving_item_is_not_raised(self):
        w = World()
        slow = item(w, ("100",))
        priced(w, slow)
        ordered(w, slow, 200)
        ordered(w, slow, 20, status="Cancelled")          # a cancelled order is no sale
        moving = item(w, ("40",))
        ordered(w, moving, 10)
        dead = item(w, ("7",))
        empty = item(w, ("0",))                           # nothing on hand: not raised
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][ops.SLOW_RULE], "complete")
        gaps = {g["object_id"]: g for g in self.kinds(ops.SLOW)}
        self.assertEqual(set(gaps), {slow, dead})
        self.assertNotIn(moving, gaps)
        self.assertNotIn(empty, gaps)
        self.assertEqual(gaps[slow]["summary"],
                         f"Item SYNTH-ITEM: 100 kg on hand and no customer order, assembly "
                         f"use or ledger outflow (sale, use or transfer) in 200 days "
                         f"(last {ago(200)})")
        self.assertEqual(gaps[slow]["follow_up"]["priority"], "medium")
        self.assertEqual(gaps[slow]["event"]["amount"]["value"], "250.00")
        self.assertEqual(gaps[slow]["event"]["amount"]["currency"], "CAD")
        self.assertIn("recorded", gaps[dead]["summary"])
        self.assertEqual(gaps[dead]["follow_up"]["priority"], "high")
        # the claim rests on no later sale being recorded: a reader not granted the orders'
        # audience gets nothing, and the value needs finance
        self.assertEqual(self.kinds(ops.SLOW, may_read=lambda k: k != "api:portal"), [])
        held = {g["object_id"]: g for g in self.kinds(ops.SLOW, held=NO_FINANCE)}
        self.assertIn(slow, held)
        self.assertNotIn("amount", held[slow].get("event") or {})

    def test_stock_whose_sales_or_use_may_sit_elsewhere_is_never_called_not_moving(self):
        # an item tied to its twin only by a lead identity link, or named on a movement line
        # only by a lead link, may be selling on that twin: not judged, never "no sale"; a
        # ledger sale, consumption or transfer taking stock out (dated by its posting event)
        # is movement; one bringing stock in is not, nor a count correction or write-off
        # (it would reset a slow mover's clock)
        w = World()
        twin_sells = item(w, ("50",))
        twin = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        w.link("Product.same_record_as", twin_sells, twin, tier="lead")
        ordered(w, twin, 10)
        lead_use = item(w, ("60",))
        part = w.obj("AssemblyOrderComponent", {}, audience="api:assembly")
        w.link("AssemblyOrderComponent.uses_item", part, lead_use, tier="lead")
        moved = {}
        for qty, kind, moves in (("-4", "Sale", True), ("-3", "Consumption", True),
                                 ("-2", "Transfer", True), ("12", "Sale", False),
                                 ("-5", "Negative_x0020_Adjmt_x002E_", False),
                                 ("-6", "Purchase", False), ("-1", "Mystery", False)):
            product = item(w, ("70",))
            entry = w.obj("ItemLedgerEntry", {"quantity": (qty, "kg", "actual"),
                                              "entry_type": kind}, audience="bc:ledger")
            w.link("ItemLedgerEntry.for_item", entry, product)
            w.event("ItemLedgerEntry.posted", entry, ago(15))
            moved[product] = moves
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][ops.SLOW_RULE], "complete")
        mystery = list(moved)[-1]   # an entry type BC never named: not judged at all
        self.assertEqual({g["object_id"] for g in self.kinds(ops.SLOW)},
                         {p for p, moves in moved.items() if not moves} - {mystery})
        skipped = summary["not_evaluated"][ops.SLOW_RULE]
        self.assertEqual(skipped.get("identity_unsettled"), 1)
        self.assertEqual(skipped.get("movement_unsettled"), 1)
        self.assertEqual(skipped.get("movement_type_unknown"), 1)

    def test_an_undated_movement_makes_recency_unproven_never_no_movement(self):
        """A known customer order whose day is unknown may be the latest movement
        (BIZ-016): the item is not judged and is counted, never "no ... recorded" nor the
        oldest bucket; a recent dated movement still says the item moves."""
        w = World()
        undated = item(w, ("100",))
        line = w.obj("SalesOrderLine", {"ordered_quantity": ("5", "kg")},
                     audience="api:portal")
        w.link("SalesOrderLine.line_of", line, w.obj(
            "SalesOrder", {"status": "Open"}, binding="portal.app_sales_order",
            audience="api:portal"))
        w.link("SalesOrderLine.for_item", line, undated)
        old_and_undated = item(w, ("100",))
        ordered(w, old_and_undated, 400)
        later = w.obj("SalesOrderLine", {"ordered_quantity": ("5", "kg")},
                      audience="api:portal")
        w.link("SalesOrderLine.line_of", later, w.obj(
            "SalesOrder", {"status": "Open", "order_date": "not a day"},
            binding="portal.app_sales_order", audience="api:portal"))
        w.link("SalesOrderLine.for_item", later, old_and_undated)
        recent = item(w, ("100",))
        ordered(w, recent, 10)
        w.link("SalesOrderLine.for_item", w.obj("SalesOrderLine", {}, audience="api:portal"),
               recent)
        dead = item(w, ("7",))
        summary = self.run_rules(w)
        self.assertEqual({g["object_id"] for g in self.kinds(ops.SLOW)}, {dead})
        self.assertEqual(summary["not_evaluated"][ops.SLOW_RULE],
                         {"movement_date_unknown": 2})

    def test_a_lot_with_stock_expiring_names_the_open_orders_that_could_use_it(self):
        w = World()
        p = item(w, ())
        soon = lot(w, p, "5", ahead(17))
        lot(w, p, "9", ahead(150), "SYN-LOT-2")           # not soon
        lot(w, p, "0", ahead(10), "SYN-LOT-3")            # used up
        sales_order(w, p, "4", ahead(30))
        self.run_rules(w)
        (gap,) = self.kinds(ops.LOT_EXPIRING)
        self.assertEqual(gap["object_id"], soon)
        self.assertEqual(gap["summary"],
                         f"Lot SYN-LOT-1 with 5 kg on hand expires {ahead(17)} (in 17 days); "
                         "1 open customer order could use it")
        self.assertEqual(gap["follow_up"]["priority"], "high")
        self.assertEqual(self.kinds(ops.LOT_EXPIRING, may_read=lambda k: k != "api:stock"), [])

    def test_an_assembly_order_short_of_components_by_its_start_is_raised(self):
        w = World()
        short = item(w, ("50",))
        covered = item(w, ("500",))
        purchase(w, short, "10", line_expected=ahead(3))    # due before the start: counted
        purchase(w, short, "99", line_expected=ahead(30))   # after: not counted
        order = assembly(w, ahead(6), [(short, "80", "0"), (covered, "20", "5")])
        assembly(w, ahead(6), [(short, "500", "0")], status="Realized", code="AO-SYN2")
        odd = assembly(w, ahead(6), [(short, "1", "0")], status="Paused", code="AO-SYN3")
        # a mirror's copy (its id sorts first) is not the record of production: alone it is
        # not judged, and a copy the keeper realized is done whatever the mirror still says
        assembly(w, ahead(6), [(short, "300", "0")], code="AO-SYN4", binding="a-mirror")
        w.link("AssemblyOrder.same_record_as",
               assembly(w, ahead(6), [(short, "400", "0")], code="AO-SYN5", binding="a-mirror"),
               assembly(w, ahead(6), [(short, "400", "0")], status="Realized", code="AO-SYN5"))
        summary = self.run_rules(w)
        (gap,) = self.kinds(ops.ASSEMBLY_SHORT)
        self.assertEqual(gap["object_id"], order)
        self.assertEqual(gap["summary"],
                         f"Assembly order AO-SYN1 starts {ahead(6)} and 1 component will be "
                         "short: SYNTH-ITEM needs 80 kg by then, 60 on hand or due in")
        self.assertEqual(gap["follow_up"]["priority"], "high")
        # an unknown status word is never read as open or done
        self.assertEqual(summary["not_evaluated"][ops.ASSEMBLY_RULE],
                         {"status_unknown": 1, "status_source_absent": 1})
        self.assertNotEqual(gap["object_id"], odd)
        for hidden in ("api:po", "api:stock", "api:assembly"):
            self.assertEqual(self.kinds(ops.ASSEMBLY_SHORT, may_read=lambda k, h=hidden: k != h),
                             [], hidden)

    def test_a_certificate_expiring_is_raised_once_and_unserved_sources_are_named(self):
        w = World()
        first = coa(w, ahead(20))
        coa(w, ahead(20), audience="sharepoint:coa")          # the same certificate again
        coa(w, ahead(45), lot_no="SYN-L2")
        coa(w, ahead(200), lot_no="SYN-L3")                   # not soon
        coa(w, "not a date", lot_no="SYN-L4")
        coa(w, ahead(30), lot_no="SYN-L5", supplier=None)     # the document names no supplier
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][ops.CERT_RULE], "complete")
        gaps = sorted(self.kinds(ops.CERT_EXPIRING), key=lambda g: g["event"]["days"])
        self.assertEqual([g["event"]["days"] for g in gaps], [20, 30, 45])
        # a label the document does not state is left out, never worded as "None"
        self.assertEqual(gaps[1]["summary"], "Certificate of analysis for SYNTH Material lot "
                         f"SYN-L5 expires {ahead(30)} (in 30 days, as read from the document)")
        self.assertEqual(gaps[0]["object_id"], first)
        self.assertEqual(gaps[0]["summary"],
                         "Certificate of analysis for SYNTH Material lot SYN-L1 from SYNTH "
                         f"Supplier expires {ahead(20)} (in 20 days, as read from the document)")
        self.assertEqual([g["follow_up"]["priority"] for g in gaps], ["high", "high", "medium"])
        self.assertEqual(summary["not_evaluated"][ops.CERT_RULE], {"date_unreadable": 1})
        self.assertIn("not_served_RelPublicAppSupplierCertification__expiry_date",
                      self.read()["detector_reasons"][ops.CERT_RULE])
        self.assertEqual(self.kinds(ops.CERT_EXPIRING, may_read=lambda k: k == "api:a"), [])

    def test_a_printed_date_whose_day_and_month_cannot_be_told_apart_is_no_expiry(self):
        """live 11-12: expiries printed 12/03/2026 and 01/11/2027 were read month first with
        nothing on the page to say so. The document's other numeric dates with the same
        separator decide the order; without one, or against the stored reading, no alert."""
        import json
        from datetime import date

        n = next(n for n in range(5, 80)
                 if date.fromisoformat(ahead(n)).day <= 12
                 and date.fromisoformat(ahead(n)).day != date.fromisoformat(ahead(n)).month)
        day = date.fromisoformat(ahead(n))
        month_first = f"{day.month:02d}/{day.day:02d}/{day.year}"

        def printed(lot, expiry_text, **others):
            fields = {"expiry_date": {"citations": [{"page": 1, "text": expiry_text}],
                                      "value": day.isoformat()}}
            for name, text in others.items():
                fields[name] = {"citations": [{"page": 1, "text": text}]}
            oid = coa(w, day.isoformat(), lot_no=lot)
            w.rows["object_values"].append({**next(
                r for r in w.rows["object_values"]
                if r["object_id"] == oid and r["property"] == "lot"),
                "property": "evidence", "value": json.dumps({"fields": fields}),
                "value_type": "json", "unit": None})
            return oid

        w = World()
        printed("SYN-A1", month_first)                                     # no telling
        forced = printed("SYN-A2", month_first, manufacture_date="04/16/2026")
        printed("SYN-A3", month_first, manufacture_date="19/11/2024")       # day first
        printed("SYN-A4", month_first, manufacture_date="2024-11-19")       # not numeric
        worded = printed("SYN-A5", day.strftime("%B %d, %Y"))
        plain = coa(w, day.isoformat(), lot_no="SYN-A6")                    # no evidence
        summary = self.run_rules(w)
        self.assertEqual(sorted(g["object_id"] for g in self.kinds(ops.CERT_EXPIRING)),
                         sorted([forced, worded, plain]))
        self.assertEqual(summary["not_evaluated"][ops.CERT_RULE],
                         {"date_order_ambiguous": 2, "date_order_misread": 1})

    def test_a_certificate_of_nothing_or_superseded_by_a_newer_one_is_not_raised(self):
        # live 11-10: the reader wrote "None" for fields a document did not hold (worded
        # "for None lot None from None"), and an older certificate of a lot was raised
        # beside the newer one stating a later date
        w = World()
        coa(w, ahead(10), lot_no="None", product="None", supplier="None")   # of nothing
        coa(w, ahead(12), lot_no=None, product=None)                        # of nothing
        coa(w, ahead(8), lot_no="SYN-L7")                                   # superseded
        newer = coa(w, ahead(38), lot_no="SYN-L7")
        named = coa(w, ahead(15), lot_no="SYN-L8", supplier="None")
        summary = self.run_rules(w)
        gaps = sorted(self.kinds(ops.CERT_EXPIRING), key=lambda g: g["event"]["days"])
        self.assertEqual([g["object_id"] for g in gaps], [named, newer])
        self.assertEqual(gaps[0]["summary"], "Certificate of analysis for SYNTH Material lot "
                         f"SYN-L8 expires {ahead(15)} (in 15 days, as read from the document)")
        self.assertEqual(summary["not_evaluated"][ops.CERT_RULE],
                         {"certificate_unidentified": 2})

    def test_another_suppliers_or_a_partly_named_certificate_never_supersedes(self):
        """A lot string is not proven unique across suppliers (BIZ-015): a later certificate
        of the same material and lot from another supplier, or one missing its supplier or
        lot, never erases the expiring one; the same supplier's later one does."""
        w = World()
        other = coa(w, ahead(9), lot_no="SYN-L9")
        coa(w, ahead(200), lot_no="SYN-L9", supplier="SYNTH Other Supplier")
        coa(w, ahead(201), lot_no="SYN-L9", supplier=None)
        coa(w, ahead(202), lot_no=None)
        coa(w, ahead(11), lot_no="SYN-L10")                              # superseded
        coa(w, ahead(203), lot_no="SYN-L10")
        self.run_rules(w)
        days = sorted((g["event"]["days"], g["object_id"])
                      for g in self.kinds(ops.CERT_EXPIRING))
        self.assertEqual(days, [(9, other)])

    def test_a_freight_bill_far_above_its_carrier_and_lane_norm_is_raised(self):
        w = World()
        for day in (200, 150, 100):
            freight(w, "SYNTH Freight", "100.00", day)
        high = freight(w, "SYNTH Freight", "250.00", 10, number="FB-HIGH")
        freight(w, "SYNTH Freight", "250.00", 10, number="FB-HIGH")  # a copy of the bill
        freight(w, "SYNTH Freight", "260.00", 12, currency="USD")   # another currency
        freight(w, "Other Carrier", "900.00", 5)                    # too little history
        freight(w, "Other Carrier", "100.00", 50, currency=None)
        summary = self.run_rules(w)
        (gap,) = self.kinds(ops.FREIGHT_HIGH)
        self.assertEqual(gap["object_id"], high)
        self.assertEqual(gap["summary"],
                         "Freight with SYNTH Freight from SYNTH Port A to SYNTH Port B cost "
                         f"250.00 CAD per shipment on {ago(10)}, 2.5 times the usual 100.00 "
                         "CAD over 3 other bills")
        self.assertEqual(gap["follow_up"]["priority"], "high")
        self.assertEqual(gap["event"]["amount"], {**gap["event"]["amount"],
                                                  "value": "250.00", "currency": "CAD"})
        self.assertEqual(summary["not_evaluated"][ops.FREIGHT_RULE], {"currency_unknown": 1})
        self.assertEqual(self.kinds(ops.FREIGHT_HIGH, held=NO_FINANCE), [])

    def test_each_rule_is_blocked_naming_the_field_its_sources_do_not_serve(self):
        w = World()
        item(w, ("10",))
        with mock.patch.object(ops, "declared_starts", return_value=()):
            summary = self.run_engine(w, only=list(ops.RULES), on_hand_sources=())
        reasons = self.read()["detector_reasons"]
        for rule in (ops.SLOW_RULE, ops.LOT_RULE, ops.ASSEMBLY_RULE):
            self.assertEqual(summary["detectors"][rule], "blocked", rule)
        self.assertIn("not_served_Product__quantity_on_hand", reasons[ops.SLOW_RULE])
        self.assertIn("not_served_InventoryLot__quantity_on_hand", reasons[ops.LOT_RULE])
        self.assertIn("not_served_AssemblyOrder__start_date", reasons[ops.ASSEMBLY_RULE])
        checks = {c["check"]: c for c in briefing._checks(self.read())}
        self.assertIn("waiting on data the sources do not serve yet (item stock on hand",
                      checks[ops.SLOW_RULE]["words"])
        self.assertIn("assembly order start date", checks[ops.ASSEMBLY_RULE]["words"])
        import re
        from operating import mcp_server
        pattern = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
        for rule in (ops.SLOW_RULE, ops.LOT_RULE, ops.ASSEMBLY_RULE):
            self.assertTrue(all(pattern.fullmatch(r) for r in reasons[rule]), reasons[rule])
        self.assertTrue(set(ops.RULES) <= set(mcp_server._HOME_RULES))

    def test_every_new_kind_has_a_check_a_next_step_and_its_words(self):
        from operating import declared_inputs, mcp_server, vendor_watch

        for kind in ops.KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, briefing.EVENT_TYPES)
                self.assertIn(kind, briefing.NEXT_ACTIONS)
                self.assertIn(kind, briefing.TODAY_WORDS)
                self.assertIn(kind, mcp_server.HOME_KINDS)
        for rule in ops.RULES:
            with self.subTest(rule=rule):
                self.assertIn(rule, pe.DETECTORS)
                # each under a minute, but slow-moving stock: every stocked item's movement
                # (measured 199 s on a loaded served pass, 2026-11-12)
                self.assertLessEqual(pe.DETECTOR_SECONDS[rule],
                                     300 if rule == ops.SLOW_RULE else 60)
                self.assertIn(rule, briefing.CHECK_WORDS)
                self.assertIn(rule, vendor_watch.WHOLE_PASS)
                self.assertIn(rule, declared_inputs.NOT_INCREMENTAL)

    def test_every_item_lot_order_certificate_and_bill_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                for i in range(n):
                    p = item(w, ("100",))
                    ordered(w, p, 120)
                    q = item(w, ())
                    lot(w, q, "3", ahead(20), f"SYN-LOT-{i}")
                    assembly(w, ahead(5), [(item(w, ("1",)), "9", "0")], code=f"AO-S{i}")
                    coa(w, ahead(25), lot_no=f"SYN-L{i}")
                    for day in (200, 150, 100):
                        freight(w, f"SYNTH Carrier {i}", "100.00", day)
                    freight(w, f"SYNTH Carrier {i}", "300.00", 5)
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                for rule in ops.RULES:
                    self.assertEqual(summary["detectors"][rule], "complete", rule)
                self.assertEqual(summary["not_evaluated"][ops.SLOW_RULE],
                                 {"movement_date_unknown": n})
                for kind in ops.KINDS:
                    found = self.kinds(kind)
                    # three stocked items each; the component an assembly uses on no
                    # stated day has a movement of unknown day: not judged (BIZ-016)
                    self.assertEqual(len(found), 2 * n if kind == ops.SLOW else n, kind)
                    self.assertEqual(len({g["object_id"] for g in found}), len(found), kind)


if __name__ == "__main__":
    unittest.main()
