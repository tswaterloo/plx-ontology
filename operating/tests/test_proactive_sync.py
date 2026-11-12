"""Cross-system sync gaps (``operating.proactive_sync``) and the one active-population
filter every record-level rule passes (``proactive_engine._drop_inactive_orders``), through
the engine, the per-reader inbox and the briefing.

SYNTHETIC fixtures only (``World`` stores, synthetic names); evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone
import time
import unittest

from operating import alert_kinds, briefing
from operating import proactive_engine as pe
from operating import proactive_sync as sync
from operating.tests.test_proactive_business import money
from operating.tests.test_proactive_engine import AS_OF, T0, Engine, World
from operating.tests.test_proactive_quality import order

NOW = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
PORTAL, BC = "portal:orders", "bc:orders"


def portal_order(w: World, number: str, **values) -> str:
    values = {"status": "Open", "order_date": "2026-09-05", "order_number": number,
              **values}
    return order(w, "SalesOrder", "portal.app_sales_order", **values)


def bc_order(w: World, number: str, **values) -> str:
    values = {"bc_status": "Open", "order_date": "2026-09-02", "order_number": number,
              **values}
    return order(w, "SalesOrder", "bc.sales_orders", **values)


def item(w: World, binding: str, code: str, **values) -> str:
    return w.obj("Product", {"code": code, **values}, binding=binding,
                 audience=binding.split(".", 1)[0] + ":items")


class KeyedWorld(World):
    """A ``World`` whose store holds identifiers too (the key joins)."""

    def build(self, path):
        from ontology.store import StoreBuilder

        builder = StoreBuilder(path)
        for table in ("objects", "object_versions", "object_values", "links", "identifiers"):
            builder.add(table, self.rows.get(table, []))
        builder.set_meta("audience_keys", sorted({r["audience_key"]
                                                   for r in self.rows["objects"]}))
        builder.commit(built_at=T0)
        return path


class SyncGaps(Engine):

    def run_rule(self, w: World) -> dict:
        return self.run_engine(w, only=[sync.RULE], at=NOW)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    def test_a_record_missing_from_its_other_system_names_it_with_a_next_step(self):
        w = KeyedWorld()
        bc_order(w, "S-SYN-0", order_date="2026-08-01")      # the counterpart's first day
        missing = portal_order(w, "SO-SYN-1")
        lead = portal_order(w, "SO-SYN-2")                    # a number match: probable copy
        w.link("SalesOrder.same_record_as", lead, bc_order(w, "S-SYN-2"), tier="lead")
        keyed = portal_order(w, "SO-SYN-3")                   # a key join: probable copy
        twin = bc_order(w, "S-SYN-3")
        for oid in (keyed, twin):
            w.rows.setdefault("identifiers", []).append({
                "identifier_kind": "order_number", "normalized": "synkey3", "raw": "SYNKEY3",
                "role": "primary", "object_id": oid, "property": "order_number",
                "l1_handle": next(v["l1_handle"] for v in w.rows["object_versions"]
                                  if v["object_id"] == oid),
                "field_path": "row.order_number",
                "lineage": "workbench", "markings": "[]", "recorded_at": T0})
        portal_order(w, "SO-SYN-4", status="Closed")          # closed: not current work
        portal_order(w, "SO-SYN-5", order_date="2024-01-02")  # historic: a legacy order
        portal_order(w, "SO-SYN-6", order_date="2026-07-01")  # before BC held any order
        summary = self.run_rule(w)
        self.assertEqual(summary["detectors"][sync.RULE], "complete")
        (gap,) = self.kinds(sync.MISSING)
        self.assertEqual(gap["follow_up"]["object_id"], missing)
        self.assertEqual(gap["summary"], "Sales order SO-SYN-1 is in the Portal but not in "
                                         "Business Central 44 days after it was made")
        self.assertIn("Key Sales order SO-SYN-1 into Business Central",
                      gap["follow_up"]["missing"])
        self.assertTrue(briefing.NEXT_ACTIONS[sync.MISSING][0])
        # the verdict rests on what BC does not hold: a Portal-only reader never sees it
        self.assertEqual(self.kinds(sync.MISSING, may_read=lambda k: k == PORTAL), [])

    def test_a_copy_that_differs_names_both_records_both_values_to_readers_of_both(self):
        w = World()
        a = portal_order(w, "SO-SYN-7", total_amount="120.00", customer_po="CPO-SYN-7")
        b = bc_order(w, "S-SYN-7", total_amount="100.00", customer_po="cpo-syn-7")
        money(w, a, "total_amount", "CAD")
        money(w, b, "total_amount", "CAD")
        w.link("SalesOrder.bc_posted_as", a, b)
        c = portal_order(w, "SO-SYN-8", total_amount="50.00")
        d = bc_order(w, "S-SYN-8", total_amount="70.00")
        money(w, c, "total_amount", "CAD")
        money(w, d, "total_amount", "USD")                   # another currency: not compared
        w.link("SalesOrder.same_record_as", c, d)
        self.run_rule(w)
        (gap,) = self.kinds(sync.MISMATCH)
        self.assertEqual(gap["summary"], "Sales order SO-SYN-7 in the Portal and Sales order "
                         "S-SYN-7 in Business Central disagree on the total: the Portal says "
                         "120.00 CAD, Business Central says 100.00 CAD")
        self.assertEqual(self.kinds(sync.MISSING), [])
        # a reader must open both records, and the amounts are finance-marked
        for may_read in (lambda k: k == PORTAL, lambda k: k == BC):
            self.assertEqual(self.kinds(sync.MISMATCH, may_read=may_read), [])
        self.assertEqual(self.kinds(sync.MISMATCH, held=frozenset()), [])
        # a customer PO that differs is shown only to a reader holding personal data
        e = portal_order(w2 := World(), "SO-SYN-P", customer_po="SYNTH BUYER")
        f = bc_order(w2, "S-SYN-P", customer_po="CPO-SYN-P")
        w2.link("SalesOrder.bc_posted_as", e, f)
        self.run_rule(w2)
        (named,) = self.kinds(sync.MISMATCH)
        self.assertIn("customer PO: the Portal says SYNTH BUYER", named["summary"])
        self.assertEqual(self.kinds(sync.MISMATCH, held=frozenset({"finance"})), [])

    def test_items_and_purchase_orders_run_in_their_own_direction(self):
        w = World()
        lonely = item(w, "bc.items", "R-SYN-1", bc_base_uom="KG")
        item(w, "bc.items", "R-SYN-2", bc_base_uom="KG", blocked="true")   # blocked
        unit_a = item(w, "bc.items", "R-SYN-3", bc_base_uom="KG")
        unit_b = item(w, "portal.app_product", "R-SYN-3", uom="kg")       # same unit
        w.link("Product.bc_posted_as", unit_b, unit_a)
        odd = item(w, "bc.items", "R-SYN-4", bc_base_uom="EA", blocked="maybe")
        po = order(w, "PurchaseOrder", "bc.purchase_orders", bc_status="Open",
                   order_date="2026-09-01", po_number="PO-SYN-1")
        w.link("PurchaseOrder.bc_posted_as",
               order(w, "PurchaseOrder", "portal.app_purchase_order", status="Open",
                     order_date="2026-08-01", po_number="PO-SYN-0"),
               order(w, "PurchaseOrder", "bc.purchase_orders", bc_status="Open",
                     order_date="2026-09-01", po_number="PO-SYN-2"))
        order(w, "PurchaseOrder", "bc.purchase_orders", bc_status="Draft",
              order_date="2026-09-01", po_number="PO-SYN-3")      # a draft: not placed yet
        summary = self.run_rule(w)
        found = sorted(g["follow_up"]["object_id"] for g in self.kinds(sync.MISSING))
        self.assertEqual(found, sorted([lonely, po]))
        self.assertEqual(self.kinds(sync.MISMATCH), [])
        # an unknown flag word is never read as active or not
        self.assertEqual(summary["not_evaluated"][sync.RULE], {"status_unknown": 1})
        self.assertNotIn(odd, found)

    def test_an_item_keyed_under_another_code_or_a_test_item_is_never_missing(self):
        w = World()
        for i in range(6):                                  # BC is in use for items
            w.link("Product.bc_posted_as",
                   item(w, "portal.app_product", f"R-SYN-J{i}", uom="kg"),
                   item(w, "bc.items", f"R-SYN-J{i}", bc_base_uom="KG"))
        renamed = item(w, "bc.items", "EU-SYN-1", name="Synth Serum - 50ml")
        item(w, "portal.app_product", "SYN-1", name="synth  serum - 50ML")
        lonely = item(w, "bc.items", "R-SYN-L", name="Synth Towel")
        item(w, "portal.app_product", "R-SYN-T", name="Synth Towels 12")   # not the same name
        demo = item(w, "bc.items", "FG-SYN-D", name="V1 Demo 60 mL")
        e2e = item(w, "bc.items", "E2E-SYN-FG-1", name="E2E SYN FG 1")
        unnamed = item(w, "bc.items", "R-SYN-U", name="")
        item(w, "portal.app_product", "R-SYN-V", name="")                  # names nothing
        self.assertEqual(self.run_rule(w)["detectors"][sync.RULE], "complete")
        found = sorted(g["follow_up"]["object_id"] for g in self.kinds(sync.MISSING))
        self.assertEqual(found, sorted([lonely, unnamed]))
        self.assertNotIn(renamed, found)   # the Portal holds it under its own code
        for test_item in (demo, e2e):
            self.assertNotIn(test_item, found)

    def test_a_system_not_in_use_and_two_spellings_of_one_value_raise_nothing(self):
        w = World()
        for i in range(3):                              # BC holds orders, none joined
            portal_order(w, f"SO-SYN-N{i}")
        bc_order(w, "S-SYN-N")
        for i in range(sync.CONVENTION_MIN):            # BC says PCS where the Portal says ea
            w.link("Product.bc_posted_as",
                   item(w, "portal.app_product", f"R-SYN-C{i}", uom="ea"),
                   item(w, "bc.items", f"R-SYN-C{i}", bc_base_uom="PCS"))
        odd = item(w, "bc.items", "R-SYN-O", bc_base_uom="KG")
        w.link("Product.bc_posted_as", item(w, "portal.app_product", "R-SYN-O", uom="L"), odd)
        for i, (bc_unit, portal_unit) in enumerate((("CSE", "cs"), ("CSE", "case"),
                                                    ("EACH", "ea"))):   # one unit each
            w.link("Product.bc_posted_as",
                   item(w, "portal.app_product", f"R-SYN-S{i}", uom=portal_unit),
                   item(w, "bc.items", f"R-SYN-S{i}", bc_base_uom=bc_unit))
        summary = self.run_rule(w)
        self.assertEqual(self.kinds(sync.MISSING), [])
        self.assertEqual(summary["not_evaluated"][sync.RULE],
                         {"counterpart_not_in_use_bc": 1})
        (gap,) = self.kinds(sync.MISMATCH)
        self.assertEqual(gap["follow_up"]["object_id"], odd)

    def test_unknown_or_unserved_inputs_fail_closed(self):
        from operating.proactive_engine import _Store, _cites, _gap, _result, _Skips

        w = World()
        portal_order(w, "SO-SYN-9")
        demo = bc_order(w, "S-SYN-9")                  # BC holds a test order only
        w.build(self.store)
        st = _Store(self.store)
        common = dict(gap=_gap, cites=_cites, result=_result, settled=pe.SETTLED,
                      skips=_Skips)
        out = sync.sync_gaps(st, AS_OF, inactive=dict, tests=lambda: {demo: "mark"},
                             **common)
        self.assertEqual(out["gaps"], [])
        self.assertEqual(out["not_evaluated"], {"counterpart_not_held_bc": 1})
        joined = World()                               # joined to a test copy: keyed there
        a = portal_order(joined, "SO-SYN-J")
        demo_j = bc_order(joined, "S-SYN-J")
        joined.link("SalesOrder.bc_posted_as", a, demo_j)
        live = portal_order(joined, "SO-SYN-K")
        joined.link("SalesOrder.bc_posted_as", live, bc_order(joined, "S-SYN-K"))
        joined.build(self.store.with_name("joined.sqlite"))
        st2 = _Store(self.store.with_name("joined.sqlite"))
        kept = sync.sync_gaps(st2, AS_OF, inactive=dict, tests=lambda: {demo_j: "mark"},
                              **common)
        st2.close()
        self.assertEqual((kept["gaps"], kept["counts"]["linked"]), ([], 2))
        real = sync.sync_gaps(st, AS_OF, inactive=dict, **common)   # a real, unjoined copy
        self.assertEqual(real["not_evaluated"], {"counterpart_not_in_use_bc": 1})
        blocked = sync.sync_gaps(st, AS_OF, pairs=(sync.SyncPair(
            "NoSuchType", "portal", "bc", (), (), "x", True),), **common)
        self.assertEqual(blocked["status"], "blocked")
        st.close()

    def test_every_record_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                for i in range(n):
                    portal_order(w, f"SO-SYN-M{i}")
                    a = portal_order(w, f"SO-SYN-D{i}", total_amount=f"{i + 10}.00")
                    b = bc_order(w, f"S-SYN-D{i}", total_amount=f"{i + 11}.00")
                    money(w, a, "total_amount", "CAD")
                    money(w, b, "total_amount", "CAD")
                    w.link("SalesOrder.bc_posted_as", a, b)
                    item(w, "bc.items", f"R-SYN-{i}", bc_base_uom="KG")
                    w.link("Product.bc_posted_as",
                           item(w, "portal.app_product", f"R-SYN-L{i}", uom="kg"),
                           item(w, "bc.items", f"R-SYN-L{i}", bc_base_uom="KG"))
                started = time.monotonic()
                summary = self.run_rule(w)
                self.assertLess(time.monotonic() - started, 60)
                self.assertEqual(summary["detectors"][sync.RULE], "complete")
                for kind, count in ((sync.MISSING, 2 * n), (sync.MISMATCH, n)):
                    found = self.kinds(kind)
                    self.assertEqual(len(found), count, kind)
                    self.assertEqual(len({g["id"] for g in found}), count, kind)

    def test_the_kinds_are_registered_and_off_until_measured(self):
        from operating import declared_inputs, mcp_server, vendor_watch

        for kind in sync.KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, briefing.EVENT_TYPES)
                self.assertIn(kind, briefing.NEXT_ACTIONS)
                self.assertIn(kind, briefing.TODAY_WORDS)
                self.assertIn(kind, mcp_server.HOME_KINDS)
                entry = alert_kinds.entry(kind)
                self.assertIsNotNone(entry)
                on = entry["default"] == "on"
                precision = entry.get("precision") or {}
                self.assertEqual(on, bool(precision) and precision["value"] >= 0.9
                                 and precision["checked"] >= 15)
        self.assertIn(sync.RULE, pe.DETECTORS)
        self.assertIn(sync.RULE, pe.REQUIRES_GRANT)
        self.assertIn(sync.RULE, briefing.CHECK_WORDS)
        self.assertIn(sync.RULE, vendor_watch.WHOLE_PASS)
        self.assertIn(sync.RULE, declared_inputs.NOT_INCREMENTAL)
        self.assertIn(sync.RULE, mcp_server._HOME_RULES)


class ActivePopulation(Engine):
    """R3: every record-level rule leaves closed and historic orders out, counted; a rule
    about a done order's own trouble keeps closed ones but never historic ones; an
    unreadable predicate withholds every order claim (fail closed)."""

    def gap(self, rule: str, oid: str, kind: str = "x") -> dict:
        return {"id": f"{rule}|{oid}", "kind": kind, "detector": rule, "object_id": oid,
                "object_type": oid.split(":", 1)[0], "follow_up": {}}

    def test_closed_and_historic_orders_leave_every_rule_counted(self):
        w = World()
        live = portal_order(w, "SO-SYN-A")
        closed = portal_order(w, "SO-SYN-B", status="Closed")
        closed_old = portal_order(w, "SO-SYN-C", status="Closed", order_date="2023-01-02")
        old = portal_order(w, "SO-SYN-D", order_date="2024-01-02")
        po_old = order(w, "PurchaseOrder", "portal.app_purchase_order", status="Open",
                       order_date="2025-01-02", po_number="PO-SYN-OLD")
        customer = w.obj("Customer", {"name": "SYNTH C"})
        w.build(self.store)
        st = pe._Store(self.store)
        try:
            verdicts = pe.inactive_records(st, AS_OF)
            self.assertEqual({k: verdicts.get(k) for k in (live, closed, old, po_old)},
                             {live: None, closed: "closed", old: "historic",
                              po_old: "historic"})
            subjects = (live, closed, closed_old, old, po_old, customer)
            results = {rule: {"status": "complete", "reasons": [], "not_evaluated": {},
                              "gaps": [self.gap(rule, s) for s in subjects]}
                       for rule in ("a_rule_added_later", "shipped_not_invoiced")}
            # an order named only by the follow-up is judged too
            named = self.gap("a_rule_added_later", customer)
            named["id"] += "|named"
            named["follow_up"] = {pe._ORDER: {"key": closed, "rank": 0}}
            results["a_rule_added_later"]["gaps"].append(named)
            # one copy closed, another open is the claim itself: kept, never when stale
            results["late_or_unconfirmed_po"] = {
                "status": "complete", "reasons": [], "not_evaluated": {},
                "gaps": [self.gap("late_or_unconfirmed_po", s, "systems_disagree")
                         for s in (closed, closed_old)]}
            pe._drop_inactive_orders(st, results, AS_OF)
        finally:
            st.close()
        new = results["a_rule_added_later"]
        self.assertEqual([g["object_id"] for g in new["gaps"]], [live, customer])
        self.assertEqual(new["not_evaluated"], {"closed_order": 3, "historic_order": 2})
        done = results["shipped_not_invoiced"]
        self.assertEqual([g["object_id"] for g in done["gaps"]], [live, closed, customer])
        self.assertEqual(done["not_evaluated"], {"historic_order": 3})
        disagree = results["late_or_unconfirmed_po"]
        self.assertEqual([g["object_id"] for g in disagree["gaps"]], [closed])
        self.assertEqual(disagree["not_evaluated"], {"historic_order": 1})

    def test_an_open_po_with_a_due_date_is_never_stale_by_age(self):
        """Ordered long ago, due recently and not received: late, listed and counted."""
        w = World()
        po = order(w, "PurchaseOrder", "portal.app_purchase_order", status="B.Order",
                   order_date="2026-03-02", expected_date="2026-10-10",
                   po_number="PO-SYN-DUE")
        summary = self.run_engine(w, only=["late_or_unconfirmed_po"], at=NOW)
        late = [g for g in self.read(limit=1000)["gaps"] if g["kind"] in ("late_po",
                                                                         "unconfirmed_po")]
        self.assertEqual([g["follow_up"]["object_id"] for g in late], [po])
        self.assertEqual(summary["po_lateness"]["late"], 1)
        self.assertNotIn("historic_order",
                         summary["not_evaluated"].get("late_or_unconfirmed_po", {}))

    def test_an_unreadable_predicate_withholds_every_order_claim(self):
        w = World()
        so = portal_order(w, "SO-SYN-E")
        customer = w.obj("Customer", {"name": "SYNTH D"})
        w.build(self.store)
        st = pe._Store(self.store)

        def broken():
            raise RuntimeError("unreadable")
        results = {"a_rule": {"status": "complete", "reasons": [], "not_evaluated": {},
                              "gaps": [self.gap("a_rule", so), self.gap("a_rule", customer)]},
                   "other": {"status": "complete", "reasons": [], "not_evaluated": {},
                             "gaps": [self.gap("other", customer)]}}
        try:
            pe._drop_inactive_orders(st, results, AS_OF, inactive=broken)
        finally:
            st.close()
        self.assertEqual([g["object_id"] for g in results["a_rule"]["gaps"]], [customer])
        self.assertEqual(results["a_rule"]["status"], "failed")
        self.assertEqual(results["a_rule"]["reasons"], [pe.ACTIVE_UNREADABLE])
        self.assertEqual(results["other"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
