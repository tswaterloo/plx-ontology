"""A customer order's page shows the records one hop away by link kind, named from the
schema: its customer (wherever the hop comes in the walk), its shipments and invoices, the
same record in another system and any other link, a link kind not declared yet included;
each record with the evidence its link was read from that this reader may open. A reader
who may not open a citation never sees it on any part of the page; a link reaching 1, 10
or 60 records loses and duplicates none of the shown ones and keeps its count.
Evidence: isolated-fixture."""

from __future__ import annotations

import unittest

from operator_api import backend as be
from operator_api import models as m

SO = "SalesOrder:bc.sales_orders:so1"
ORDER_ROW = "bc://production/c/salesOrders/so1@e1"
MAIL = "graph-mail://box/m1@c1"
SECRET = "bc://production/c/salesInvoices/inv1@e9"
INV_ROW = "bc://production/c/salesInvoices/inv1@e2"


class Access:
    def __init__(self, withheld=()):
        self.withheld = set(withheld)

    def decide(self, handles):
        return {h: ("withheld" if h in self.withheld else None) for h in handles}


class View:
    def __init__(self, withheld=()):
        self.access = Access(withheld)

    @staticmethod
    def _can_open(handle):
        return True


class Backend:
    _source = staticmethod(be.WorkspaceBackend._source)

    @staticmethod
    def _conflicts(view, raw, label):
        return []

    @staticmethod
    def freshness(page, **_):
        return m.Freshness()


def obj(oid, kind, display):
    return {"object_id": oid, "object_type": kind, "display": display}


def edge(link_type, a, b, tier, *handles):
    return {"link_type": link_type, "from": a, "to": b, "tier": tier,
            "reason": [{"feature": "f", "citations": [{"l1_handle": h} for h in handles]}]}


def hop(link_type, direction, objects, edges=(), total=None, leads=(), lead_edges=(),
        gaps=()):
    """A workspace hop as ``plx.around`` answers it: the solid records in ``objects``, the
    ones only lead or disputed links reach apart in ``possibly_related`` (with its gap)."""
    result = {"set": "set:solid", "objects": objects, "edges": list(edges),
              "gaps": list(gaps),
              "completeness": {"complete": True, "truncated": False,
                               "returned": len(objects), "total": len(objects)}}
    if total is not None:
        result["completeness"]["total"] = total
    if leads:
        result["possibly_related"] = {"set": "set:lead", "count": len(leads),
                                      "objects": list(leads), "edges": list(lead_edges)}
        result["gaps"].append({"state": "unknown", "reason": "possibly_related",
                               "subject": "set:lead"})
    return {"link_type": link_type, "direction": direction, "listed_count": len(objects),
            "result": result}


def lines(n):
    return hop("SalesOrderLine.line_of", "reverse",
               [obj(f"SalesOrderLine:bc.sales_order_lines:l{i}", "SalesOrderLine", f"Line {i}")
                for i in range(n)])


def page(hops, *, truncated=False):
    return {"object_id": SO,
            "card": {"object": {"object_type": "SalesOrder", "display": "SO-101"},
                     "values": [{"property": "order_date", "value": "2026-09-01",
                                 "citation": {"l1_handle": ORDER_ROW}}]},
            "answers": {}, "timeline": {"events": []}, "documents": {"result": {}},
            "around": {"hops": hops, "truncated": truncated}}


INVOICE = "SalesInvoice:bc.sales_invoices:inv1"
SHIPMENT = "Shipment:bc.shipments:sh1"
CUSTOMER = "Customer:bc.customers:c1"


def full_hops():
    return [
        # twenty-five lines first: the customer hop comes after the related list is full
        lines(25),
        hop("SalesInvoice.bills", "reverse", [obj(INVOICE, "SalesInvoice", "PSI-9")],
            [edge("SalesInvoice.bills", INVOICE, SO, "verified", INV_ROW, ORDER_ROW, SECRET,
                  MAIL)]),
        # a shipment only a lead link reaches (a mail naming the order): set apart
        hop("Shipment.ships", "reverse", [], leads=[obj(SHIPMENT, "Shipment", "SH-4")],
            lead_edges=[edge("Shipment.ships", SHIPMENT, SO, "lead", MAIL)]),
        hop("SalesOrder.same_record_as", "forward",
            [obj("SalesOrder:portal.app_sales_order:p1", "SalesOrder", "OR101")]),
        hop("SalesOrder.mentioned_in_meeting_note", "reverse",
            [obj("MeetingNote:meetings.notes:n1", "MeetingNote", "Weekly ops")]),
        hop("SalesOrder.ordered_by", "forward", [obj(CUSTOMER, "Customer", "Acme Foods")],
            [edge("SalesOrder.ordered_by", SO, CUSTOMER, "verified", ORDER_ROW)]),
    ]


class OrderLinks(unittest.TestCase):
    def model(self, hops, withheld=(), **kw):
        return be._page_model(Backend(), View(withheld), page(hops, **kw))

    def test_each_link_kind_is_a_group_named_from_the_schema(self):
        got = self.model(full_hops())
        groups = {g.link_type: g for g in got.links}
        self.assertEqual(groups["SalesInvoice.bills"].words, "Invoices")
        self.assertEqual(groups["SalesInvoice.bills"].role, "fulfilment")
        self.assertEqual(groups["Shipment.ships"].words, "Shipments")
        self.assertEqual(groups["Shipment.ships"].role, "fulfilment")
        self.assertEqual(groups["SalesOrder.ordered_by"].role, "customer")
        self.assertEqual(groups["SalesOrder.same_record_as"].role, "same_record")
        self.assertEqual(groups["SalesOrder.same_record_as"].words,
                         "Same record in another system")
        # a link kind the schema does not declare yet still shows, in its own words
        future = groups["SalesOrder.mentioned_in_meeting_note"]
        self.assertEqual((future.role, future.words, future.type),
                         ("related", "Mentioned in meeting note", "MeetingNote"))
        # the customer is named although its hop came after twenty other records
        self.assertIn("for Acme Foods", got.summary)
        self.assertNotIn("The customer of this order", [n.what for n in got.not_found])
        self.assertNotIn("Shipments and invoices of this order",
                         [n.what for n in got.not_found])

    def test_evidence_is_mail_first_without_the_page_own_row(self):
        got = self.model(full_hops())
        invoice = next(g for g in got.links if g.link_type == "SalesInvoice.bills").records[0]
        self.assertEqual(invoice.tier, "verified")
        self.assertEqual([s.handle for s in invoice.evidence], [MAIL, INV_ROW, SECRET])
        self.assertTrue(all(s.open_url for s in invoice.evidence))
        shipment = next(g for g in got.links if g.link_type == "Shipment.ships").records[0]
        self.assertEqual((shipment.tier, shipment.possible,
                          [s.system for s in shipment.evidence]), ("lead", True, ["Email"]))
        group = next(g for g in got.links if g.link_type == "Shipment.ships")
        self.assertEqual((group.count, group.possible), (0, 1))   # never counted
        self.assertFalse(invoice.possible)
        customer = next(g for g in got.links if g.role == "customer").records[0]
        self.assertEqual(customer.evidence, [])   # only the order's own row: not repeated

    def test_a_citation_the_reader_may_not_open_is_nowhere_on_the_page(self):
        got = self.model(full_hops(), withheld={SECRET, MAIL})
        self.assertNotIn(SECRET, be.page_handles(got))
        self.assertNotIn(MAIL, be.page_handles(got))
        self.assertNotIn(SECRET, got.model_dump_json())
        invoice = next(g for g in got.links if g.link_type == "SalesInvoice.bills").records[0]
        self.assertEqual([s.handle for s in invoice.evidence], [INV_ROW])

    def said(self, hops, **kw):
        return {n.what: n.where_it_would_live for n in self.model(hops, **kw).not_found
                }.get("Shipments and invoices of this order")

    def test_absence_is_said_only_after_a_whole_walk(self):
        others = [h for h in full_hops() if h["link_type"] not in ("SalesInvoice.bills",
                                                                   "Shipment.ships")]
        self.assertEqual(self.said(others), "No shipment or invoice is linked to this order "
                                            "in the records you can open.")
        unseen = ("None found in what you can see: part of this order's links could not be "
                  "read for you, so this is not absence.")
        # the hop cap left link types unwalked
        self.assertEqual(self.said(others, truncated=True), unseen)
        # a hop that errored
        errored = others + [{"link_type": "SalesInvoice.bills", "direction": "reverse",
                             "result": {"error": {"code": "timeout"}}}]
        self.assertEqual(self.said(errored), unseen)
        # a hop cut back by permission
        withheld = others + [hop("Shipment.ships", "reverse", [], gaps=[
            {"state": "unknown", "reason": "marking_withheld", "subject": "x"}])]
        self.assertEqual(self.said(withheld), unseen)
        cut = [dict(h, result=dict(h["result"], gaps=[
            {"state": "inaccessible", "reason": "withheld_by_permission", "subject": "x"}]))
               for h in others[:1]] + others[1:]
        self.assertEqual(self.said(cut), unseen)
        # a page returned short of the hop's total
        short = [dict(h, result=dict(h["result"], completeness={
            "complete": False, "truncated": True})) for h in others[:1]] + others[1:]
        self.assertEqual(self.said(short), unseen)

    def test_only_lead_links_say_possibly_related_never_absence(self):
        hops = [h for h in full_hops() if h["link_type"] != "SalesInvoice.bills"]
        said = self.said(hops)
        self.assertIn("possibly related", said)
        self.assertNotIn("No shipment", said)
        self.assertIsNone(self.said(full_hops()))   # a confirmed invoice: nothing to say

    def test_unexpected_hop_shapes_fail_closed(self):
        hops = ["not a hop", {"link_type": "X.y", "result": "nope"},
                {"link_type": "SalesInvoice.bills", "direction": "sideways",
                 "result": {"objects": [{"object_type": "SalesInvoice"}, 7,
                                        obj(SO, "SalesOrder", "itself")],
                            "edges": ["x", {"from": SO, "to": 3, "reason": "bad"}]}}]
        got = self.model(hops)
        self.assertEqual(got.links, [])   # no record without an id, never the page itself

    def test_cardinality_keeps_every_shown_record_once_and_the_count(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                got = self.model([lines(n)])
                group = got.links[0]
                ids = [r.object_id for r in group.records]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(len(ids), min(n, be.LINK_RECORDS))
                self.assertEqual(ids, [f"SalesOrderLine:bc.sales_order_lines:l{i}"
                                       for i in range(min(n, be.LINK_RECORDS))])
                self.assertEqual(group.count, n)

    def test_possible_records_follow_the_solid_ones_and_are_never_counted(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                solid = [obj(f"Shipment:bc.shipments:s{i}", "Shipment", f"SH-{i}")
                         for i in range(3)]
                leads = [obj(f"Shipment:bc.shipments:l{i}", "Shipment", f"LS-{i}")
                         for i in range(n)] + solid[:1]   # one also solid: shown once
                group = self.model([hop("Shipment.ships", "reverse", solid,
                                        leads=leads)]).links[0]
                ids = [r.object_id for r in group.records]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual([r.possible for r in group.records][:3], [False] * 3)
                self.assertEqual(len(ids), min(3 + n, be.LINK_RECORDS))
                self.assertEqual((group.count, group.possible), (3, n + 1))

    def test_a_reported_total_beyond_the_returned_page_is_the_count(self):
        many = hop("Shipment.ships", "reverse", [obj(SHIPMENT, "Shipment", "SH-4")], total=140)
        self.assertEqual(self.model([many]).links[0].count, 140)


if __name__ == "__main__":
    unittest.main()
