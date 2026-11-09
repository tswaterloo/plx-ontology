"""Data-quality alerts (``operating.proactive_quality``) through the engine, the per-reader
inbox and the briefing: customers and suppliers entered twice (review proposals, never
links), key fields missing on active records, systems disagreeing on one record, and
sources not captured lately.

SYNTHETIC fixtures only (``World`` stores, synthetic names); evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
import unittest

from ontology.store import StoreBuilder
from operating import briefing
from operating import proactive_engine as pe
from operating import proactive_quality as q
from operating.tests.test_proactive_business import money
from operating.tests.test_proactive_engine import T0, Engine, World

NOW = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)


class MetaWorld(World):
    """A ``World`` whose store states each source's last capture (``freshness``)."""

    def __init__(self, freshness=()) -> None:
        super().__init__()
        self.freshness = list(freshness)

    def build(self, path):
        builder = StoreBuilder(path)
        for table in ("objects", "object_versions", "object_values", "links"):
            builder.add(table, self.rows[table])
        builder.set_meta("audience_keys", sorted({r["audience_key"]
                                                   for r in self.rows["objects"]}))
        builder.set_meta("freshness", json.dumps(self.freshness))
        builder.commit(built_at=T0)
        return path


def party(w: World, kind: str, binding: str, name: str, *, audience: str | None = None,
          **values) -> str:
    return w.obj(kind, {"name": name, **values}, binding=binding,
                 audience=audience or binding.split(".", 1)[0] + ":parties")


def order(w: World, kind: str, binding: str, **values) -> str:
    """An order with one line still to ship or receive (the one status definitions read
    an order without lines as unknown, never open)."""
    audience = binding.split(".", 1)[0] + ":orders"
    oid = w.obj(kind, values, binding=binding, audience=audience)
    line = w.obj(f"{kind}Line", {"ordered_quantity": ("5", "kg"),
                                 "received_quantity": ("0", "kg", "received")},
                 audience=audience)
    w.link(f"{kind}Line.line_of", line, oid)
    return oid


class QualityAlerts(Engine):

    def run_rules(self, w: World, rules=q.RULES) -> dict:
        return self.run_engine(w, only=list(rules), at=NOW)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    def test_duplicates_are_scored_review_proposals_counted_per_system_pair(self):
        w = World()
        bc = party(w, "Customer", "bc.customer", "SYNTH Foods Inc.",
                   bc_phone_number="+1 (555) 010-2000", bc_email="sales@synthfoods.example")
        portal = party(w, "Customer", "portal.app_customer", "Synth Foods Inc",
                       email="orders@synthfoods.example")
        fm = party(w, "Customer", "fm.customer", "SYNTH FOODS INC",
                   email="ap@synthfoods.example")
        settled_a = party(w, "Supplier", "bc.vendor", "SYNTH Packaging Ltd")
        settled_b = party(w, "Supplier", "portal.app_supplier", "SYNTH Packaging Ltd")
        w.link("Supplier.same_record_as", settled_a, settled_b)       # already one
        party(w, "Customer", "bc.customer", "Other Synth Co",
              bc_phone_number="555-010-2000")                        # a phone alone: below
        party(w, "Customer", "sage.customer", "Unrelated Synth Ltd",
              email="x@gmail.com")
        party(w, "Customer", "sage.customer", "Another Synth Ltd",
              email="y@gmail.com")                                   # public mail: no key
        summary = self.run_rules(w, [q.DUP_RULE])
        self.assertEqual(summary["detectors"][q.DUP_RULE], "complete")
        gaps = self.kinds(q.DUPLICATE)
        pairs = {(g["object_id"], g["follow_up"]["proposal"]["document"]) for g in gaps}
        self.assertEqual(pairs, {tuple(sorted(p)) for p in ((bc, portal), (bc, fm),
                                                            (fm, portal))})
        by = {g["object_id"] + g["follow_up"]["proposal"]["document"]: g for g in gaps}
        both = by[min(fm, portal) + max(fm, portal)]
        self.assertEqual(both["follow_up"]["proposal"]["link_type"], "Customer.same_record_as")
        self.assertEqual(both["follow_up"]["proposal"]["mode"], "prepare_link")
        self.assertIn("same email or web domain", both["summary"])
        self.assertEqual(both["follow_up"]["priority"], "medium")   # name and domain: 0.7
        # proposals per system pair, never a link written
        self.assertFalse(any(l["link_type"] == "Customer.same_record_as"
                             for l in w.rows["links"]))
        # a reader of one system's parties never sees a pair citing the other's
        self.assertEqual(self.kinds(q.DUPLICATE, may_read=lambda k: k == "bc:parties"), [])
        seen = self.kinds(q.DUPLICATE, may_read=lambda k: k in ("fm:parties",
                                                                 "portal:parties"))
        self.assertEqual([(g["object_id"], g["follow_up"]["proposal"]["document"]) for g in seen],
                         [tuple(sorted((fm, portal)))])

    def test_a_pair_with_a_blocked_or_deleted_copy_is_low_priority(self):
        # A blocked or deleted record beside its live twin is most
        # often the retired copy; still proposed, never dropped, but low priority
        w = World()
        live = party(w, "Customer", "portal.app_customer", "Synth Foods Inc",
                     email="orders@synthfoods.example")
        blocked = party(w, "Customer", "bc.customer", "SYNTH Foods Inc.",
                        bc_email="sales@synthfoods.example", bc_blocked="All")
        twin_a = party(w, "Customer", "fm.customer", "SYNTH Labs Inc",
                       email="ap@synthlabs.example")
        twin_b = party(w, "Customer", "portal.app_customer", "Synth Labs Inc",
                       email="x@synthlabs.example")
        self.run_rules(w, [q.DUP_RULE])
        by = {frozenset((g["object_id"], g["follow_up"]["proposal"]["document"])): g
              for g in self.kinds(q.DUPLICATE)}
        retired = by[frozenset((live, blocked))]
        self.assertEqual(retired["follow_up"]["priority"], "low")
        self.assertIn("blocked or deleted", retired["summary"])
        both_live = by[frozenset((twin_a, twin_b))]
        self.assertEqual(both_live["follow_up"]["priority"], "medium")
        self.assertNotIn("blocked or deleted", both_live["summary"])

    def test_duplicate_counts_per_system_pair_and_rejection_of_unknown_names(self):
        from operating.proactive_engine import _Store, _cites, _gap, _result, _Skips

        w = World()
        party(w, "Customer", "bc.customer", "SYNTH Alpha Ltd", bc_email="a@alpha.example")
        party(w, "Customer", "portal.app_customer", "SYNTH Alpha Ltd", email="b@alpha.example")
        party(w, "Customer", "portal.app_customer", "SYNTH Alpha Limited",
              email="c@alpha.example")
        party(w, "Customer", "sage.customer", "SYNTH Alpha Ltd")      # a name alone: none
        party(w, "Customer", "portal.app_customer", "")               # no name: counted
        w.build(self.store)
        st = _Store(self.store)
        out = q.duplicate_parties(st, NOW.date(), gap=_gap, cites=_cites, result=_result,
                                  settled=pe.SETTLED, skips=_Skips)
        # "Ltd" and "Limited" fold to one name: three pairs, two across BC and the Portal
        self.assertEqual(out["counts"]["pairs"], {"bc/parties|portal": 2, "portal|portal": 1})
        self.assertEqual(out["counts"]["proposed"], len(out["gaps"]))
        self.assertEqual({g["follow_up"]["proposal"]["link_type"] for g in out["gaps"]},
                         {"Customer.same_record_as"})
        self.assertEqual(out["not_evaluated"], {"name_unknown": 1})

    def test_two_bc_companies_are_two_entities_and_sibling_accounts_are_not_duplicates(self):
        from operating.proactive_engine import _Store, _cites, _gap, _result, _Skips

        w = World()
        one, two = "bc:aaaaaaaa-0000/customers", "bc:bbbbbbbb-0000/customers"
        a = party(w, "Customer", "bc.customers", "SYNTH Trading Inc", audience=one,
                  code="C100", bc_email="ap@trading.example")
        b = party(w, "Customer", "bc.customers", "SYNTH Trading Inc", audience=two,
                  code="C100", bc_email="ap@trading.example")      # the same party, company 2
        party(w, "Customer", "bc.customers", "SYNTH Trading Inc", audience=one,
              code="C100-2", bc_email="ship@trading.example")      # a suffixed sibling code
        party(w, "Customer", "bc.customers", "SYNTH Trading Inc Toronto", audience=one,
              code="C300", bc_email="to@trading.example")          # a location: prefix only
        cad = party(w, "Customer", "bc.customers", "SYNTH Metals Ltd", audience=one,
                    code="M1", currency="CAD", bc_phone_number="5550109999")
        party(w, "Customer", "bc.customers", "SYNTH Metals Ltd", audience=one, code="M2",
              currency="USD", bc_phone_number="5550109999")        # a per-currency account
        dup_a = party(w, "Customer", "bc.customers", "SYNTH Paper Co", audience=two,
                      code="P1", bc_phone_number="5550107777")
        dup_b = party(w, "Customer", "bc.customers", "SYNTH Paper Company", audience=two,
                      code="Q9", bc_phone_number="5550107777")      # a real copy in one book
        w.build(self.store)
        out = q.duplicate_parties(_Store(self.store), NOW.date(), gap=_gap, cites=_cites,
                                  result=_result, settled=pe.SETTLED, skips=_Skips)
        self.assertEqual({(g["object_id"], g["follow_up"]["proposal"]["document"])
                          for g in out["gaps"]}, {tuple(sorted((dup_a, dup_b)))})
        self.assertEqual(out["counts"]["pairs"], {"bc/bbbbbbbb|bc/bbbbbbbb": 1})
        across = [(r["record"], r["other"]) for r in out["related"]]
        self.assertIn(tuple(sorted((a, b))), across)
        self.assertTrue(all(r["relation"] == "same_party_across_companies"
                            for r in out["related"]))
        self.assertEqual(out["counts"]["across_companies"], len(out["related"]))
        self.assertGreaterEqual(out["counts"]["related_accounts"], 3)
        self.assertNotIn(cad, {g["object_id"] for g in out["gaps"]})

    def test_an_active_record_without_its_key_field_is_raised_once_per_twin_cluster(self):
        w = World()
        missing = order(w, "SalesOrder", "portal.app_sales_order", status="Open",
                        order_number="SO-SYN-1")
        order(w, "SalesOrder", "portal.app_sales_order", status="Closed")       # inactive
        odd = order(w, "SalesOrder", "portal.app_sales_order", status="Wibble")
        twin_a = order(w, "SalesOrder", "portal.app_sales_order", status="Open")
        twin_b = order(w, "SalesOrder", "bc.sales_order", status="Released",
                       required_date="2026-11-01")
        w.link("SalesOrder.same_record_as", twin_a, twin_b)   # a copy states the date
        item = w.obj("Product", {"code": "SYNTH-ITEM-1"}, audience="api:items")
        w.obj("Product", {"code": "SYNTH-ITEM-2", "uom": "kg"}, audience="api:items")
        w.obj("Product", {"code": "SYNTH-ITEM-3", "deleted_at": "2026-01-01"},
              audience="api:items")
        po = order(w, "PurchaseOrder", "portal.app_purchase_order", status="ORDERED",
                   po_number="PO-SYN-1")      # open by the one PO status definition
        summary = self.run_rules(w, [q.FIELDS_RULE])
        self.assertEqual(summary["detectors"][q.FIELDS_RULE], "complete")
        gaps = {g["object_id"]: g for g in self.kinds(q.MISSING)}
        self.assertEqual(set(gaps), {missing, item})   # an undated PO is undated_po's
        self.assertNotIn(odd, gaps)
        self.assertEqual(gaps[missing]["summary"],
                         "Sales order SO-SYN-1 is active and states no ship date")
        self.assertEqual(gaps[item]["summary"],
                         "Item SYNTH-ITEM-1 is active and states no unit")
        self.assertEqual(summary["not_evaluated"][q.FIELDS_RULE], {"status_unknown": 1})
        self.assertIn("not_served_Customer__payment_terms_code",
                      self.read()["detector_reasons"][q.FIELDS_RULE])
        # an absence: only a reader granted every audience of the type sees it (the sales
        # orders sit in the Portal and BC; a Portal reader alone cannot tell none is stated)
        self.assertEqual({g["object_id"] for g in self.kinds(
            q.MISSING, may_read=lambda k: k in ("api:items", "portal:orders"))}, {item})

    def test_twins_in_two_systems_stating_different_values_are_raised_per_property(self):
        w = World()
        bc = order(w, "PurchaseOrder", "bc.purchase_order", status="Open",
                   order_date="2026-10-01", total_cost="100.00", po_number="PO-SYN-9")
        portal = order(w, "PurchaseOrder", "portal.app_purchase_order", status="Closed",
                       order_date="2026-10-03", total_cost="100.00")
        money(w, bc, "total_cost", "CAD")
        money(w, portal, "total_cost", "CAD")
        w.link("PurchaseOrder.same_record_as", bc, portal)
        c1 = order(w, "PurchaseOrder", "bc.purchase_order", status="Open",
                   total_cost="50.00")
        c2 = order(w, "PurchaseOrder", "portal.app_purchase_order", status="Pending",
                   total_cost="70.00")
        money(w, c1, "total_cost", "CAD")
        money(w, c2, "total_cost", "USD")                    # another currency: not compared
        w.link("PurchaseOrder.same_record_as", c1, c2)
        lead_a = order(w, "PurchaseOrder", "bc.purchase_order", order_date="2026-01-01")
        lead_b = order(w, "PurchaseOrder", "fm.purchase_order", order_date="2026-02-01")
        w.link("PurchaseOrder.same_record_as", lead_a, lead_b, tier="lead")   # not settled
        summary = self.run_rules(w, [q.DISAGREE_RULE])
        self.assertEqual(summary["detectors"][q.DISAGREE_RULE], "complete")
        gaps = self.kinds(q.DISAGREE)
        self.assertEqual(sorted(g["follow_up"]["object_id"] for g in gaps), [bc, bc])
        words = sorted(g["summary"] for g in gaps)
        self.assertEqual(words, [
            "Purchase order PO-SYN-9: the systems disagree on its order date: bc says "
            "2026-10-01; portal says 2026-10-03",
            "Purchase order PO-SYN-9: the systems disagree on its status: bc says Open "
            "(open); portal says Closed (done)"])
        self.assertEqual(self.kinds(q.DISAGREE, may_read=lambda k: k == "bc:orders"), [])

    def test_a_source_not_captured_lately_is_raised_to_its_readers_only(self):
        w = MetaWorld([{"source": "bc", "captured_at": "2026-10-10T06:00:00Z"},
                       {"source": "portal_api", "captured_at": "2026-10-19T01:00:00Z"},
                       {"source": "graph_mail", "captured_at": None}])
        order(w, "SalesOrder", "bc.sales_order", status="Open", required_date="2026-11-01")
        order(w, "SalesOrder", "api.sales_order", status="Open", required_date="2026-11-01")
        summary = self.run_rules(w, [q.STALE_RULE])
        self.assertEqual(summary["detectors"][q.STALE_RULE], "complete")
        (gap,) = self.kinds(q.STALE)
        self.assertEqual(gap["summary"], "Business Central was last captured 2026-10-10T06:00 "
                         "UTC, 216 hours ago: newer changes are not in it")
        self.assertEqual(gap["follow_up"]["priority"], "high")
        self.assertEqual(summary["not_evaluated"][q.STALE_RULE], {"capture_time_unknown": 1})
        self.assertEqual(self.kinds(q.STALE, may_read=lambda k: k == "api:orders"), [])

    def test_unknown_or_unreadable_inputs_fail_closed(self):
        from operating.proactive_engine import _Store, _cites, _gap, _result, _Skips

        w = MetaWorld([{"source": "bc", "captured_at": "not a time"}, "junk"])
        w.obj("Customer", {"name": "SYNTH X"}, binding="bc.customer")
        w.build(self.store)
        st = _Store(self.store)
        common = dict(gap=_gap, cites=_cites, result=_result, settled=pe.SETTLED,
                      skips=_Skips)
        self.assertEqual(q.stale_captures(st, NOW.date(), now=None, **common)["status"],
                         "failed")
        out = q.stale_captures(st, NOW.date(), now=NOW, **common)
        self.assertEqual(out["gaps"], [])
        self.assertEqual(out["not_evaluated"], {"capture_time_unknown": 1,
                                                "entry_unreadable": 1})
        blocked = q.key_fields_missing(st, NOW.date(), requirements=(
            q.Requirement("Customer", ("no_such_field",), "Customer", "x", False),), **common)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reasons"], ["not_served_Customer__no_such_field"])
        self.assertEqual(q.duplicate_parties(st, NOW.date(), types=("NoSuchType",),
                                             **common)["status"], "blocked")

    def test_every_new_kind_has_a_check_a_next_step_and_its_words(self):
        from operating import declared_inputs, mcp_server, vendor_watch

        for kind in q.KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, briefing.EVENT_TYPES)
                self.assertIn(kind, briefing.NEXT_ACTIONS)
                self.assertIn(kind, briefing.TODAY_WORDS)
                self.assertIn(kind, mcp_server.HOME_KINDS)
        for rule in q.RULES:
            with self.subTest(rule=rule):
                self.assertIn(rule, pe.DETECTORS)
                self.assertIn(rule, briefing.CHECK_WORDS)
                self.assertIn(rule, vendor_watch.WHOLE_PASS)
                self.assertIn(rule, declared_inputs.NOT_INCREMENTAL)
                self.assertIn(rule, mcp_server._HOME_RULES)

    def test_every_record_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = MetaWorld([{"source": "bc", "captured_at": "2026-10-01T00:00:00Z"}])
                for i in range(n):
                    party(w, "Customer", "bc.customer", f"SYNTH Party {i} Ltd",
                          bc_email=f"sales@party{i}.example")
                    party(w, "Customer", "portal.app_customer", f"SYNTH Party {i} Ltd",
                          email=f"ap@party{i}.example")
                    order(w, "SalesOrder", "portal.app_sales_order", status="Open",
                          order_number=f"SO-SYN-{i}")
                    a = order(w, "PurchaseOrder", "bc.purchase_order", status="ORDERED",
                              order_date="2026-10-01", expected_date="2026-11-01")
                    b = order(w, "PurchaseOrder", "portal.app_purchase_order",
                              status="Open", order_date="2026-10-02")
                    w.link("PurchaseOrder.same_record_as", a, b)
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                for rule in q.RULES:
                    self.assertEqual(summary["detectors"][rule], "complete", rule)
                expected = {q.DUPLICATE: n, q.MISSING: n, q.DISAGREE: n, q.STALE: 1}
                for kind, count in expected.items():
                    found = self.kinds(kind)
                    self.assertEqual(len(found), count, kind)
                    self.assertEqual(len({g["id"] for g in found}), count, kind)


if __name__ == "__main__":
    unittest.main()
