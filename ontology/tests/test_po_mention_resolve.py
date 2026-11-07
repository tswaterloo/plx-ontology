"""A reused purchase order number's mention tied to one order only on decisive evidence
(``ontology.po_mention_resolve``) and the firm link it gets (``ontology.mention_firm``).

Evidence label: ``isolated-fixture`` (made-up orders, parties and messages).
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import json
import sqlite3
import unittest

from ontology import mention_firm as mf
from ontology import po_mention_resolve as pmr


def order(key, suppliers=(), start=None, last=None, closed=False):
    return pmr.Order(key, (key,), frozenset(suppliers), start, last, closed)


class Resolve(unittest.TestCase):
    def test_the_supplier_decides_and_a_date_outside_its_life_is_a_conflict(self):
        bc = order("PurchaseOrder:bc:1", {"name:p r resources"}, date(2026, 7, 10))
        old = order("PurchaseOrder:portal:1", {"name:brenntag"}, date(2016, 6, 9),
                    date(2016, 6, 9), closed=True)
        mail = pmr.Mail(date(2026, 8, 1), frozenset({"name:p r resources"}))
        picked = pmr.resolve([bc, old], mail)
        self.assertEqual((picked.order, picked.reason), (bc, "supplier"))
        early = pmr.Mail(date(2026, 1, 1), frozenset({"name:p r resources"}))
        self.assertEqual(pmr.resolve([bc, old], early).reason, "supplier_out_of_life")
        self.assertIsNone(pmr.resolve([bc, old], early).order)

    def test_the_date_alone_decides_only_when_every_other_life_is_known_and_excludes_it(self):
        bc = order("PurchaseOrder:bc:1", (), date(2026, 7, 10))
        old = order("PurchaseOrder:portal:1", (), date(2016, 6, 9), date(2016, 6, 9), True)
        unknown = order("PurchaseOrder:portal:2")
        mail = pmr.Mail(date(2026, 8, 1))
        self.assertEqual(pmr.resolve([bc, old], mail).reason, "order_life")
        self.assertEqual(pmr.resolve([bc, old], mail).order, bc)
        for orders, why in (([bc, old, unknown], "life_unknown"),
                            ([bc, order("PurchaseOrder:bc:2", (), date(2026, 6, 1))],
                             "lives_overlap")):
            found = pmr.resolve(orders, mail)
            self.assertEqual((found.order, found.reason), (None, why))
        self.assertEqual(pmr.resolve([bc, old], pmr.Mail(None)).reason, "no_evidence")

    def test_a_supplier_of_both_orders_with_no_date_stays_ambiguous(self):
        a = order("PurchaseOrder:a:1", {"Supplier:x:1"})
        b = order("PurchaseOrder:b:1", {"Supplier:x:1"})
        found = pmr.resolve([a, b], pmr.Mail(None, frozenset({"Supplier:x:1"})))
        self.assertEqual((found.order, found.reason), (None, "suppliers_tie"))
        self.assertEqual(pmr.resolve([], pmr.Mail(None)).reason, "no_candidates")


class Picker:
    """A resolver stand-in: the message's senders decide (the store reader is
    ``Resolver``)."""

    def __init__(self, orders, supplier_of):
        self.orders, self.supplier_of = orders, supplier_of

    def mail(self, sent, senders):
        parties = {self.supplier_of[s] for s in senders.values() if s in self.supplier_of}
        used = tuple(o for o, s in senders.items() if s in self.supplier_of)
        return pmr.Mail(pmr._day(sent), frozenset(parties), used)

    def resolve_linked(self, number, mail, _linked):
        return pmr.resolve(self.orders.get(number, []), mail)


class FirmStep(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE link_rows (link_id, link_type, from_object, to_object, "
                          "tier, reason, evidence_handles, markings, recorded_at, "
                          "superseded_at)")
        self.conn.execute("CREATE VIEW links AS SELECT * FROM link_rows")
        self.conn.execute("CREATE TABLE object_values (object_id, property, value, "
                          "superseded_at)")
        self.conn.execute("CREATE TABLE object_versions (object_id, l1_handle, superseded_at)")
        for oid, conv, sent, sender in (
                ("Email:m:1", "c1", "2026-10-01T10:00:00Z", "ops@synth-tenant.example"),
                ("Email:m:2", "c1", "2026-09-20T10:00:00Z", "sales@synth-vendor.example")):
            for prop, value in (("conversation_id", conv), ("sent_at", sent),
                                ("sender", sender)):
                self.conn.execute("INSERT INTO object_values VALUES (?, ?, ?, NULL)",
                                  (oid, prop, value))
            self.conn.execute("INSERT INTO object_versions VALUES (?, ?, NULL)",
                              (oid, "h-" + oid))
        reason = [{"feature": "identifier_mention", "agrees": True,
                   "identifier_kind": "petra_po", "normalized": "PO4820", "span": [3, 9],
                   "mention_group": "g1"}]
        for n, to in enumerate(("PurchaseOrder:bc:1", "PurchaseOrder:portal:1")):
            self.conn.execute("INSERT INTO link_rows VALUES (?, ?, ?, ?, 'lead', ?, ?, '[]', "
                              "'2026-10-01T00:00:00Z', NULL)",
                              (f"mention:{n}", "Email.mentions_purchase_order", "Email:m:1",
                               to, json.dumps(reason), json.dumps(["h-Email:m:1"])))
        self.orders = {"4820": [order("PurchaseOrder:bc:1", {"S:vendor"}),
                                order("PurchaseOrder:portal:1", {"S:other"})]}

    def firm(self, supplier_of):
        counts = Counter()
        rows = list(mf.firm_links(self.conn, ["PurchaseOrder", "Email"], counts,
                                  resolver=Picker(self.orders, supplier_of)))
        return rows, counts

    def test_the_resolved_order_alone_gets_a_firm_link_citing_why_and_the_deciding_mail(self):
        rows, counts = self.firm({"sales@synth-vendor.example": "S:vendor"})
        self.assertEqual([r["to_object"] for r in rows], ["PurchaseOrder:bc:1"])
        self.assertEqual(rows[0]["tier"], "corroborated")
        why = [f for f in json.loads(rows[0]["reason"])
               if f.get("feature") == "number_disambiguated"]
        self.assertEqual(why, [{"feature": "number_disambiguated", "agrees": True,
                                "reason": "supplier", "candidates": 2}])
        self.assertEqual(json.loads(rows[0]["evidence_handles"]),
                         ["h-Email:m:1", "h-Email:m:2"])
        self.assertEqual(counts["resolved:supplier"], 1)

    def test_no_deciding_evidence_or_a_rejected_pair_writes_nothing(self):
        rows, counts = self.firm({})
        self.assertEqual((rows, counts["ambiguous:life_unknown"]), ([], 1))
        self.conn.execute("INSERT INTO link_rows (link_type, from_object, to_object, tier) "
                          "VALUES ('Email.mentions_purchase_order', 'Email:m:1', "
                          "'PurchaseOrder:bc:1', 'contradicted')")
        rows, counts = self.firm({"sales@synth-vendor.example": "S:vendor"})
        self.assertEqual((rows, counts["rejected"]), ([], 1))

    def test_without_our_domains_nothing_resolves(self):
        counts = Counter()
        rows = list(mf.firm_links(self.conn, ["PurchaseOrder", "Email"], counts))
        self.assertEqual((rows, counts["ambiguous:own_domains_unknown"]), ([], 1))


class StoreHolders(FirmStep):
    """The real ``Resolver`` over a store: holders come from the identifier index the
    mention writer searches, so a number a Sage-era order holds as ``sage_po_number`` is a
    second holder, and every record a lead reaches is a candidate (fail closed)."""

    def setUp(self):
        super().setUp()
        self.conn.execute("CREATE TABLE objects (object_id, object_type)")
        self.conn.execute("CREATE TABLE identifiers (identifier_kind, normalized, object_id, "
                          "property, superseded_at)")
        self.conn.execute("UPDATE link_rows SET to_object = 'PurchaseOrder:sage:1' "
                          "WHERE to_object = 'PurchaseOrder:portal:1'")
        for oid in ("PurchaseOrder:bc:1", "PurchaseOrder:sage:1", "Email:m:1", "Email:m:2"):
            self.conn.execute("INSERT INTO objects VALUES (?, ?)", (oid, oid.split(":")[0]))
        for oid, prop, value in (("PurchaseOrder:bc:1", "po_number", "PO4820"),
                                 ("PurchaseOrder:sage:1", "sage_po_number", "4820")):
            self.conn.execute("INSERT INTO object_values VALUES (?, ?, ?, NULL)",
                              (oid, prop, value))
            self.conn.execute("INSERT INTO identifiers VALUES ('petra_po', ?, ?, ?, NULL)",
                              (pmr.number_key(value), oid, prop))
        # a lead twin between the two joins nothing
        self.conn.execute("INSERT INTO link_rows (link_id, link_type, from_object, to_object, "
                          "tier) VALUES ('t1', 'PurchaseOrder.same_record_as', "
                          "'PurchaseOrder:bc:1', 'PurchaseOrder:sage:1', 'lead')")

    def run_step(self):
        counts = Counter()
        rows = list(mf.firm_links(self.conn, ["PurchaseOrder", "Email"], counts,
                                  own=("synth-tenant.example",)))
        return rows, counts

    def test_a_sage_holder_of_the_number_is_a_second_order(self):
        resolver = pmr.Resolver(self.conn, ("synth-tenant.example",))
        self.assertEqual([o.key for o in resolver.candidates("4820")],
                         ["PurchaseOrder:bc:1", "PurchaseOrder:sage:1"])
        rows, counts = self.run_step()
        self.assertEqual(rows, [])
        self.assertEqual(counts["ambiguous:life_unknown"], 1)
        self.assertEqual(sum(v for k, v in counts.items() if k.startswith("resolved:")), 0)

    def test_a_linked_record_the_index_misses_is_still_a_candidate(self):
        # the Sage order reached by a reviewed alias only: no index row, no number value
        self.conn.execute("DELETE FROM identifiers WHERE object_id = 'PurchaseOrder:sage:1'")
        self.conn.execute("DELETE FROM object_values WHERE object_id = 'PurchaseOrder:sage:1'")
        resolver = pmr.Resolver(self.conn, ("synth-tenant.example",))
        self.assertEqual([o.key for o in resolver.candidates("4820")], ["PurchaseOrder:bc:1"])
        rows, counts = self.run_step()
        self.assertEqual((rows, counts["ambiguous:life_unknown"]), ([], 1))
        # a lead to a record that is no known order: no pick at all
        self.conn.execute("DELETE FROM objects WHERE object_id = 'PurchaseOrder:sage:1'")
        rows, counts = self.run_step()
        self.assertEqual((rows, counts["ambiguous:candidate_unknown"]), ([], 1))

    def test_a_sole_holder_still_gets_its_firm_link(self):
        self.conn.execute("DELETE FROM link_rows WHERE to_object = 'PurchaseOrder:sage:1'")
        self.conn.execute("INSERT INTO link_rows (link_id, link_type, from_object, to_object, "
                          "tier, reason, evidence_handles, markings, recorded_at) SELECT "
                          "'mention:9', link_type, from_object, 'PurchaseOrder:sage:1', 'lead', "
                          "replace(reason, 'PO4820', 'PO7777'), evidence_handles, markings, "
                          "recorded_at FROM link_rows WHERE link_id = 'mention:0'")
        # PO7777: no order holds it in the index, yet the lead reaches the Sage order
        self.conn.execute("UPDATE link_rows SET reason = replace(reason, '\"g1\"', '\"g2\"') "
                          "WHERE link_id = 'mention:9'")
        rows, counts = self.run_step()
        self.assertEqual(sorted(r["to_object"] for r in rows),
                         ["PurchaseOrder:bc:1", "PurchaseOrder:sage:1"])


if __name__ == "__main__":
    unittest.main()
