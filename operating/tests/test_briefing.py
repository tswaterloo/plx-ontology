"""Proactive events (operating/proactive_events.py) folded by the scheduled engine, and the
per-person briefing over them (operating/briefing.py): each detector, ranking, routing,
gating as the viewer, since-last-visit and the digest preview.

SYNTHETIC fixtures only (``StoreBuilder`` stores from rows written here, synthetic handles,
fake grants and a fake tool caller). Evidence label: isolated-fixture.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from operating import briefing as bf
from operating import customer_profit as cp
from operating import proactive_engine as pe
from operating.tests.test_proactive_engine import (ALL_MARKINGS, AS_OF, EXCLUDED_OID, T0,
                                                  World, admit, people, po,
                                                  everyone, handle)

MOMENT = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
ALL = ALL_MARKINGS | {"purchasing_prices"}


def revise(w: World, oid: str, prop: str, old: str, at: str) -> None:
    """An earlier version of ``oid`` holding ``prop`` = ``old``, superseded at ``at`` by the
    current one (which is then recorded at ``at``)."""
    first = "2026-07-01T00:00:00.000000Z"
    w.n += 1
    h = handle(w.n)
    for row in w.rows["objects"]:
        if row["object_id"] == oid:
            row["first_recorded_at"] = first
    for table in ("object_versions", "object_values"):
        for row in w.rows[table]:
            if row["object_id"] == oid and row.get("superseded_at") is None:
                row["recorded_at"] = at
    w.rows["object_versions"].append({
        "object_id": oid, "l1_handle": h, "digest": f"d{w.n}", "lineage": "workbench",
        "markings": "[]", "recorded_at": first, "superseded_at": at})
    w.rows["object_values"].append({
        "object_id": oid, "property": prop, "value_type": "date", "value": old, "unit": None,
        "quantity_role": None, "l1_handle": h, "field_path": f"row.{prop}", "digest": f"d{w.n}",
        "lineage": "workbench", "binding_id": "fx", "parser": "fixture", "markings": "[]",
        "recorded_at": first, "superseded_at": at})


class Case(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store.sqlite"
        self.inbox = pe.inbox_path(self.store)

    def run_engine(self, world: World, *, call=None, at: datetime = MOMENT,
                   **options) -> dict:
        world.build(self.store)
        return pe.run(self.store, self.inbox, AS_OF, call=call, clock=lambda: at,
                      people=options.pop("people", people()), **options)

    def read(self, *, may_read=everyone, held=ALL, decide=admit) -> dict:
        return pe.read_inbox(self.inbox, self.store, reader="r", may_read=may_read,
                             held=held, decide=decide, states=("open",), limit=500)

    def events(self, **grant) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for gap in self.read(**grant)["gaps"]:
            out.setdefault(gap["kind"], []).append(gap)
        return out

    def stored(self, oid: str, **grant) -> dict:
        """The engine's stored item of ``oid`` as the reader's grant trims it (the briefing
        leaves a suspect one out)."""
        return next(g for gaps in self.events(**grant).values() for g in gaps
                    if g["object_id"] == oid)

    def brief(self, routing: bf.Routing, *, reader="r", **grant) -> dict:
        grant = {"may_read": everyone, "held": ALL, "decide": admit, **grant}
        return bf.read(self.inbox, self.store, reader=reader, routing=routing, **grant)


BOX_OURS = "mail:00000000-0000-4000-8000-0000000000a1"     # a cleared person's mailbox
BOX_OTHER = "mail:00000000-0000-4000-8000-0000000000b2"
EVERYTHING = bf.Routing(basis="fixture", everything=True)
PROC = bf.Routing(frozenset({"PROC"}))
QUALITY = bf.Routing(frozenset({"QUALITY"}))


def said(number: str) -> str:
    """A mention lead's reason: the order number the message's text names."""
    return json.dumps([{"feature": "identifier_mention", "agrees": True,
                        "identifier_kind": "petra_po", "normalized": number}])


def po_with(w: World, total=None, currency="USD", *, markings="[]", audience="api:a",
            binding="fx", **values) -> str:
    props = dict(values)
    if total is not None:
        props.update(total_cost=total, currency=currency)
    return w.obj("PurchaseOrder", props, markings=markings, audience=audience,
                 binding=binding)


class DetectorTest(Case):

    def test_eta_slipped_from_a_revised_date_and_from_two_confirmations(self):
        w = World()
        slipped = po_with(w, expected_date="2026-10-25")
        revise(w, slipped, "expected_date", "2026-10-15", "2026-10-12T00:00:00.000000Z")
        earlier = po_with(w, expected_date="2026-10-05")              # moved earlier: none
        revise(w, earlier, "expected_date", "2026-10-15", "2026-10-12T00:00:00.000000Z")
        old = po_with(w, expected_date="2026-10-25")                  # revised long ago
        revise(w, old, "expected_date", "2026-10-15", "2026-08-01T00:00:00.000000Z")
        confirmed = po_with(w)
        for issued, promised in (("2026-09-01", "2026-10-01"), ("2026-09-20", "2026-10-11")):
            w.link("SupplierConfirmation.confirms", w.obj("SupplierConfirmation", {
                "document_date": issued, "delivery_date": promised}), confirmed)
        summary = self.run_engine(w)
        self.assertEqual(summary["detectors"]["eta_slipped"], "complete")
        found = {g["object_id"]: g for g in self.events()["eta_slipped"]}
        self.assertEqual(set(found), {slipped, confirmed})
        self.assertEqual(found[slipped]["event"]["days"], 10)
        self.assertIn("from 2026-10-15 to 2026-10-25", found[slipped]["summary"])
        self.assertEqual(found[confirmed]["event"]["days"], 10)
        self.assertEqual(len(found[confirmed]["citations"]), 2)   # both confirmations

    def test_a_default_date_rewrite_or_a_closed_order_is_no_slip(self):
        """The one PO reader decides: a date moved to the day a bulk share of its records
        hold (a sync's re-write) is no promise moving, and an order closed by the shared
        status (received) never slips."""
        w = World()
        for _ in range(10):   # the bulk day a re-sync wrote on every order
            po_with(w, expected_date="2026-07-14")
        rewritten = po_with(w, expected_date="2026-07-14")
        revise(w, rewritten, "expected_date", "2026-07-01", "2026-10-12T00:00:00.000000Z")
        received = po(w, "Complete", "2026-10-25", lines=(("10", "10"),))
        revise(w, received, "expected_date", "2026-10-15", "2026-10-12T00:00:00.000000Z")
        moved = po(w, "Pending", "2026-10-25")
        revise(w, moved, "expected_date", "2026-10-15", "2026-10-12T00:00:00.000000Z")
        summary = self.run_engine(w)
        found = {g["object_id"] for g in self.events()["eta_slipped"]}
        self.assertEqual(found, {moved})
        self.assertEqual(summary["not_evaluated"]["eta_slipped"],
                         {"default_date": 1, "order_not_open": 1})

    def per_item_world(self, n_lines=2):
        """One PO whose lines two supplier confirmations (mailbox a) each promise their own
        dates: line 0 moves 2026-10-01 -> 2026-10-11, every other line moves earlier."""
        w = World()
        order = po_with(w)
        lines = []
        for i in range(n_lines):
            ln = w.obj("PurchaseOrderLine", {"description": f"InLab item number {i:03d}"},
                       audience="api:lines")
            w.link("PurchaseOrderLine.line_of", ln, order)
            lines.append(ln)
        confs = []
        for issued, first, rest in (("2026-09-01", "2026-10-01", "2026-10-20"),
                                    ("2026-09-20", "2026-10-11", "2026-10-15")):
            blocks = [{"line": str(10 * (i + 1)), "text": f"InLab item number {i:03d} 3001{i:04d}",
                       "codes": [], "delivery_date": {"value": first if i == 0 else rest,
                                                      "citations": []}}
                      for i in range(n_lines)]
            conf = w.obj("SupplierConfirmation", {"document_date": issued,
                                                  "line_promises": json.dumps(blocks)},
                         audience="mail:box-a")
            w.link("SupplierConfirmation.confirms", conf, order)
            confs.append(conf)
        return w, order, lines, confs

    def test_eta_slipped_per_line_when_a_later_confirmation_moves_its_promise(self):
        w, order, lines, confs = self.per_item_world()
        w.obj("Message", {}, audience="mail:box-b")   # a mailbox some readers may not read
        summary = self.run_engine(w)
        self.assertEqual(summary["not_evaluated"].get("eta_slipped", {}), {})
        (found,) = self.events()["eta_slipped"]
        self.assertEqual(found["object_id"], order)
        self.assertEqual(found["event"], {
            "days": 10, "date_was": "2026-10-01", "date_now": "2026-10-11", "line": lines[0],
            "matched_by": "description", "confirmation_was": confs[0],
            "confirmation_now": confs[1]})
        # both confirmations and the line's matched description, each cited
        self.assertEqual(len(found["citations"]), 3)
        self.assertNotIn("cannot see", found["summary"])
        # a reader without mailbox b is told a later promise may exist, never which mailbox
        (caveated,) = self.events(may_read=lambda key: key != "mail:box-b")["eta_slipped"]
        self.assertTrue(caveated["summary"].endswith(
            "(a later promise you cannot see may exist)"))
        self.assertEqual(caveated["event"]["caveat"], "later_promise_may_exist")
        self.assertNotIn("box-b", json.dumps(caveated))
        # nor does a reader of neither the line nor a confirmation see the event
        self.assertNotIn("eta_slipped", self.events(may_read=lambda key: key != "api:lines"))
        self.assertNotIn("eta_slipped", self.events(may_read=lambda key: key != "mail:box-a"))

    def test_eta_slipped_per_line_fails_closed_on_marked_or_unmatched_promises(self):
        # the later confirmation's per-item promise is marked: a reader without the
        # marking never sees the event
        w, order, lines, confs = self.per_item_world()
        for row in w.rows["object_values"] + w.rows["object_versions"]:
            if row["object_id"] == confs[1]:
                row["markings"] = '["purchasing_prices"]'
        self.run_engine(w)
        self.assertEqual(len(self.events()["eta_slipped"]), 1)
        self.assertNotIn("eta_slipped", self.events(held=ALL_MARKINGS))
        # two lines of one description: which item is whose is no firm evidence, no event
        w, order, lines, confs = self.per_item_world()
        for row in w.rows["object_values"]:
            if row["object_id"] == lines[1] and row["property"] == "description":
                row["value"] = "InLab item number 000"
        self.run_engine(w)
        self.assertNotIn("eta_slipped", self.events())

    def test_eta_slipped_per_line_cardinality(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w, order, lines, confs = self.per_item_world(n)
                for row in w.rows["object_values"]:   # every line moves later now
                    if row["object_id"] == confs[1] and row["property"] == "line_promises":
                        row["value"] = row["value"].replace("2026-10-15", "2026-10-25")
                started = datetime.now()
                self.run_engine(w)
                self.assertLess((datetime.now() - started).total_seconds(), 30)
                found = self.events()["eta_slipped"]
                self.assertEqual(sorted(g["event"]["line"] for g in found), sorted(lines))
                self.assertEqual(sorted(g["event"]["days"] for g in found),
                                 sorted([10] + [5] * (n - 1)))

    def test_lot_near_expiry_and_an_active_batch_on_hold(self):
        w = World()
        soon = w.obj("Lot", {"expiry_date": (AS_OF + timedelta(days=10)).isoformat(),
                             "lot_number": "SYNTH-L1", "qc_status": "Released"})
        w.obj("Lot", {"expiry_date": (AS_OF + timedelta(days=40)).isoformat()})
        w.obj("Lot", {"expiry_date": (AS_OF - timedelta(days=1)).isoformat()})
        held = w.obj("Batch", {"quality_status": "On-Hold", "status": "Realized"})
        w.obj("Batch", {"quality_status": "On-Hold", "status": "Cancelled"})
        w.obj("Batch", {"quality_status": "Released", "status": "Allocated"})
        w.obj("Batch", {"quality_status": "On-Hold", "status": "SYNTH-word"})   # unmapped
        summary = self.run_engine(w)
        found = self.events()
        self.assertEqual([g["object_id"] for g in found["lot_near_expiry"]], [soon])
        self.assertEqual(found["lot_near_expiry"][0]["event"]["days"], 10)
        self.assertEqual(found["lot_near_expiry"][0]["summary"], "Expires on 2026-10-29, in 10 days")
        self.assertEqual([g["object_id"] for g in found["lot_on_hold"]], [held])
        self.assertEqual(summary["not_evaluated"]["lot_attention"], {"status_unmapped": 1})

    def test_a_lot_near_expiry_is_claimed_only_while_its_status_holds_stock(self):
        """A used-up, rejected or expired lot has nothing left to expire; a lot whose QC
        status is missing or unknown is not judged, and says so."""
        w = World()
        day = (AS_OF + timedelta(days=5)).isoformat()
        stock = [w.obj("Lot", {"expiry_date": day, "qc_status": word})
                 for word in ("Released", "Quarantine")]
        for word in ("Finished", "Rejected", "Expired"):
            w.obj("Lot", {"expiry_date": day, "qc_status": word})
        w.obj("Lot", {"expiry_date": day})                              # no status
        w.obj("Lot", {"expiry_date": day, "qc_status": "SYNTH-word"})   # unmapped
        summary = self.run_engine(w)
        found = self.events()["lot_near_expiry"]
        self.assertEqual(sorted(g["object_id"] for g in found), sorted(stock))
        self.assertEqual(summary["not_evaluated"]["lot_attention"],
                         {"lot_status_unknown": 1, "lot_status_unmapped": 1})

    def test_a_hold_every_batch_in_its_state_carries_is_no_hold(self):
        """The QC value a whole lifecycle state carries in one system (every unmade batch
        "On-Hold" before release) is that state's default, never claimed; a made batch
        still on hold is, and a cancelled one never is. Another system is profiled on its
        own batches, a state too small there over every system's."""
        w = World()
        for _ in range(12):
            w.obj("Batch", {"quality_status": "On-Hold", "status": "Allocated"},
                  binding="sysA")
            w.obj("Batch", {"quality_status": "On-Hold", "status": "Cancelled"},
                  binding="sysA")
            w.obj("Batch", {"quality_status": "Released", "status": "Realized"},
                  binding="sysA")
        made = w.obj("Batch", {"quality_status": "On-Hold", "status": "Realized"},
                     binding="sysA")
        # sysB holds back one allocated batch of twelve: that one is a decision
        other = [w.obj("Batch", {"quality_status": "Released", "status": "Allocated"},
                       binding="sysB") for _ in range(11)]
        held = w.obj("Batch", {"quality_status": "On-Hold", "status": "Allocated"},
                     binding="sysB")
        self.assertEqual(len(other), 11)
        # sysC has too few Generated batches to profile its own: every system's are read
        # (sysA's twelve, all On-Hold: the default); a lone unmade state none profiles
        # is not judged, and says so
        for _ in range(12):
            w.obj("Batch", {"quality_status": "On-Hold", "status": "Generated"},
                  binding="sysA")
        for _ in range(2):
            w.obj("Batch", {"quality_status": "On-Hold", "status": "Generated"},
                  binding="sysC")
        w.obj("Batch", {"quality_status": "On-Hold", "status": "Committed"}, binding="sysC")
        summary = self.run_engine(w)
        found = {g["object_id"]: g for g in self.events()["lot_on_hold"]}
        self.assertEqual(sorted(found), sorted([made, held]))
        self.assertEqual(summary["not_evaluated"]["lot_attention"], {"state_unprofiled": 1})
        self.assertEqual(found[made]["summary"],
                         "Made but never released: Quality still holds the batch")

    def test_a_batch_and_its_migrated_copy_are_one_hold(self):
        """A batch copied into another system (``Batch.same_record_as``, origin to copy)
        is counted once, on its origin, the copy's values cited beside it; the copy is
        decided by its origin even when the copy alone still says hold."""
        w = World()
        origin = w.obj("Batch", {"quality_status": "On-Hold", "status": "Realized"},
                       binding="fm")
        copy = w.obj("Batch", {"quality_status": "On-Hold", "status": "Realized"},
                     binding="gold")
        w.link("Batch.same_record_as", origin, copy)
        released = w.obj("Batch", {"quality_status": "Released", "status": "Realized"},
                         binding="fm")
        stale = w.obj("Batch", {"quality_status": "On-Hold", "status": "Realized"},
                      binding="gold")
        w.link("Batch.same_record_as", released, stale)
        lone = w.obj("Batch", {"quality_status": "On-Hold", "status": "Realized"},
                     binding="gold")                       # no origin: judged on its own
        self.run_engine(w)
        found = {g["object_id"]: g for g in self.events()["lot_on_hold"]}
        self.assertEqual(sorted(found), sorted([origin, lone]))
        self.assertIn(handle(int(copy.rsplit(":", 1)[1])), found[origin]["citations"])

    def test_accepted_quote_with_no_order_needs_every_sales_order_grant(self):
        w = World()
        lone = w.obj("Quote", {"status": "ACCEPTED"})
        converted = w.obj("Quote", {"status": "ACCEPTED"})
        w.link("Quote.converted_to", converted, w.obj("SalesOrder", {}, audience="api:so"))
        w.obj("Quote", {"status": "DRAFT"})
        self.run_engine(w)
        self.assertEqual([g["object_id"] for g in self.events()["quote_accepted_no_order"]],
                         [lone])
        # a reader who may not read the sales orders never gets the absence verdict
        partial = self.events(may_read=lambda key: key != "api:so")
        self.assertNotIn("quote_accepted_no_order", partial)

    def test_invoice_billing_more_than_received_in_one_unit(self):
        w = World()
        order = po_with(w)
        item = w.obj("Product", {})
        line = w.obj("PurchaseOrderLine", {"received_quantity": ("5", "ea", "received")})
        w.link("PurchaseOrderLine.line_of", line, order)
        w.link("PurchaseOrderLine.for_item", line, item)
        invoice = w.obj("PurchaseInvoice", {"status": "Open"})
        w.link("PurchaseInvoice.invoices", invoice, order)
        billed = w.obj("PurchaseInvoiceLine", {"invoiced_quantity": ("8", "ea", "invoiced")})
        w.link("PurchaseInvoiceLine.line_of", billed, invoice)
        w.link("PurchaseInvoiceLine.for_item", billed, item)
        # a second order whose invoice states no unit: never compared
        other = po_with(w)
        other_line = w.obj("PurchaseOrderLine", {"received_quantity": ("5", "ea", "received")})
        w.link("PurchaseOrderLine.line_of", other_line, other)
        w.link("PurchaseOrderLine.for_item", other_line, item)
        other_inv = w.obj("PurchaseInvoice", {"status": "Paid"})
        w.link("PurchaseInvoice.invoices", other_inv, other)
        no_unit = w.obj("PurchaseInvoiceLine", {"invoiced_quantity": ("9", None, "invoiced")})
        w.link("PurchaseInvoiceLine.line_of", no_unit, other_inv)
        w.link("PurchaseInvoiceLine.for_item", no_unit, item)
        summary = self.run_engine(w)
        found = self.events()["invoice_quantity_mismatch"]
        self.assertEqual([g["object_id"] for g in found], [order])
        self.assertIn("Invoiced 8 ea of an item, received 5 ea", found[0]["summary"])
        self.assertEqual(summary["not_evaluated"]["invoice_quantity_mismatch"],
                         {"unit_not_stated": 1})

    def test_received_is_read_on_every_copy_of_the_order(self):
        """An invoice linked to one record of an order is compared with what any copy of
        the order received (each copy on its own, never added): the order's other system
        recording the goods received is no billing above receipt."""
        w = World()
        item = w.obj("Product", {})
        orders = []
        for n, got in enumerate(("8", "5")):   # the other copy received 8, or only 5
            order = po_with(w, po_number=f"PO10{n}")
            copy = po_with(w, po_number=f"PO10{n}")
            w.link("PurchaseOrder.same_record_as", order, copy)
            for record, qty in ((order, "5"), (copy, got)):
                line = w.obj("PurchaseOrderLine", {"received_quantity": (qty, "ea",
                                                                         "received")})
                w.link("PurchaseOrderLine.line_of", line, record)
                w.link("PurchaseOrderLine.for_item", line, item)
            invoice = w.obj("PurchaseInvoice", {"status": "Open"})
            w.link("PurchaseInvoice.invoices", invoice, order)
            billed = w.obj("PurchaseInvoiceLine", {"invoiced_quantity": ("8", "ea",
                                                                         "invoiced")})
            w.link("PurchaseInvoiceLine.line_of", billed, invoice)
            w.link("PurchaseInvoiceLine.for_item", billed, item)
            orders.append(order)
        self.run_engine(w)
        found = self.events()["invoice_quantity_mismatch"]
        self.assertEqual([g["object_id"] for g in found], [orders[1]])
        self.assertIn("Invoiced 8 ea of an item, received 5 ea", found[0]["summary"])

    def test_vendor_terms_events_through_the_callers_investigations(self):
        w = World()
        order = po_with(w)
        request = w.obj("Email", {"sent_at": "2026-10-05T10:00:00Z", "conversation_id": "c1"},
                        audience="mail:box")
        w.obj("Email", {"sent_at": "2026-10-06T10:00:00Z", "conversation_id": "c1"},
              audience="mail:other")
        w.link("Email.mentions_purchase_order", request, order, "lead")
        stale = w.obj("Email", {"sent_at": "2026-08-01T10:00:00Z"})     # outside the window
        w.link("Email.mentions_purchase_order", stale, po_with(w), "lead")
        reply_handle = handle(w.n - 2)   # the second message's own citation
        calls = []

        def call(tool, args):
            calls.append((tool, args["family"], args["root"]))
            return {"answer": [{"name": "replies_1", "kind": "computed",
                                "value": {"untrusted_text": "SYNTH",
                                          "origin": {"citation": reply_handle}},
                                "steps": ["proposed update (differs): SYNTH"],
                                "basis": [request, order]}]}

        summary = self.run_engine(w, call=call)
        # the unanswered requests are read from the store: only F27 is investigated
        self.assertEqual(calls, [("plx.investigate", "F27", order)])
        self.assertEqual(summary["detectors"]["vendor_terms_unapplied"], "complete")
        self.assertEqual(len(self.events()["vendor_terms_unapplied"]), 1)
        self.assertEqual(summary["coverage"]["vendor_terms_unapplied"]["evaluated"], 1)

    def no_reply_world(self) -> tuple[World, dict]:
        """Open and closed orders with their vendor conversations (``vendor_no_reply``)."""
        w = World()
        ids: dict = {}
        ours, vendor = "ops@synth-tenant.example", "sales@synth-vendor.example"

        def mail(conversation, sent, sender, *, box=BOX_OURS, subject="RE: PO", preview="", names=(), tier="lead"):
            oid = w.obj("Email", {"conversation_id": conversation, "sent_at": sent,
                                  "sender": sender, "subject": subject,
                                  "body_preview": preview}, audience=box)
            for order in names:
                w.link("Email.mentions_purchase_order", oid, order, tier)
            return oid
        ids["waiting"] = po(w, "Pending", "2026-11-30")
        mail("c1", "2026-09-20T10:00:00Z", vendor, box=BOX_OTHER,
             preview="We received your order.")
        ids["request"] = mail("c1", "2026-10-05T10:00:00Z", ours, names=[ids["waiting"]],
                              preview="Hi, could you please confirm the delivery date?",
                              tier="corroborated")
        # the vendor's reply sits only in an excluded person's mailbox: never read
        mail("c1", "2026-10-07T10:00:00Z", vendor, box=f"mail:{EXCLUDED_OID}")
        # another conversation about it, answered
        mail("c2", "2026-09-01T10:00:00Z", vendor, names=[ids["waiting"]])
        mail("c2", "2026-09-02T10:00:00Z", ours, preview="Please send the ETA.")
        mail("c2", "2026-09-03T10:00:00Z", vendor, preview="Next week.")
        # an open order whose conversation an excluded person of ours wrote in: unjudged
        ids["uncleared"] = po(w, "Pending", "2026-11-30")
        mail("c3", "2026-09-20T10:00:00Z", vendor, names=[ids["uncleared"]])
        mail("c3", "2026-10-01T10:00:00Z", "boss@synth-tenant.example",
             preview="Please confirm the price?")
        # a closed order: nothing to wait for
        closed = po(w, "Complete", "2026-10-01", lines=(("10", "10"),))
        mail("c4", "2026-09-20T10:00:00Z", vendor, names=[closed])
        mail("c4", "2026-10-01T10:00:00Z", ours, preview="Please confirm the delivery?")
        # our last word thanks them: no request is waiting
        ids["thanked"] = po(w, "Pending", "2026-11-30")
        mail("c5", "2026-09-20T10:00:00Z", vendor, names=[ids["thanked"]])
        mail("c5", "2026-10-01T10:00:00Z", ours, preview="Many thanks, have a good day.")
        # our closing word after the vendor answered: the reply keeps the asking subject
        thanked_reply = po(w, "Pending", "2026-11-30")
        mail("c6", "2026-09-10T10:00:00Z", ours, subject="Please confirm the delivery date?",
             preview="Could you please confirm the delivery date?", names=[thanked_reply])
        mail("c6", "2026-09-12T10:00:00Z", vendor, subject="RE: Please confirm the delivery "
             "date?", preview="It ships on the 20th.")
        mail("c6", "2026-10-01T10:00:00Z", ours, subject="RE: Please confirm the delivery "
             "date?", preview="Thank you, noted.")
        # an internal forward: who it went to is not projected, never a vendor request
        forwarded = po(w, "Pending", "2026-11-30")
        mail("c7", "2026-09-20T10:00:00Z", vendor, names=[forwarded])
        mail("c7", "2026-10-01T10:00:00Z", ours, subject="FW: price",
             preview="Could you please confirm the price?")
        # an excluded person's copy without a sent time still leaves it unjudged
        undated = po(w, "Pending", "2026-11-30")
        mail("c8", "2026-09-20T10:00:00Z", vendor, names=[undated])
        mail("c8", "2026-10-01T10:00:00Z", ours, preview="Please confirm the ETA?")
        w.obj("Email", {"conversation_id": "c8", "sender": "boss@synth-tenant.example"},
              audience=BOX_OURS)
        # an open order no mail names
        po(w, "Pending", "2026-11-30")
        return w, ids

    def test_vendor_no_reply_is_read_set_based_people_first(self):
        w, ids = self.no_reply_world()
        summary = self.run_engine(w)    # no tool caller: a store rule
        self.assertEqual(summary["detectors"]["vendor_no_reply"], "complete")
        found = self.events()["vendor_no_reply"]
        self.assertEqual([(g["object_id"], g["event"]["days"]) for g in found],
                         [(ids["waiting"], 14)])
        self.assertEqual(found[0]["citations"], [handle(int(ids["request"].rsplit(":", 1)[1]))])
        self.assertEqual(summary["coverage"]["vendor_no_reply"], {
            "unit": "open_purchase_orders", "total": 7, "evaluated": 4,
            "not_evaluated": {"no_cleared_mail_names_it": 1, "participants_not_cleared": 2}})
        self.assertEqual(summary["not_evaluated"]["vendor_no_reply"],
                         {"forward_not_judged": 1, "last_message_asks_nothing": 2})
        # no reply is a verdict on the whole conversation: a reader who may not read every
        # cleared mailbox of it never sees it; the excluded mailbox is never required
        self.assertNotIn("vendor_no_reply",
                         self.events(may_read=lambda key: key != BOX_OTHER))
        self.assertEqual(len(self.events(
            may_read=lambda key: key != f"mail:{EXCLUDED_OID}")["vendor_no_reply"]), 1)
        # without a usable people exclusion nothing of the mail is read (fail closed)
        self.inbox.unlink()
        blind = self.run_engine(w, people=people(unavailable=True))
        self.assertEqual(blind["detectors"]["vendor_no_reply"], "blocked")
        # blocked for the missing exclusion list; other truthful reasons may accompany it
        self.assertIn("people_exclusion_unavailable",
                      blind["detector_reasons"]["vendor_no_reply"])
        self.assertNotIn("vendor_no_reply", self.events())

    def test_a_mention_of_a_reused_number_ties_no_mail_to_the_order(self):
        """A number-text mention (a lead) of an order whose number another order also
        holds may be the other order's: it is no request waiting on this order."""
        for reused in (False, True):
            with self.subTest(reused=reused):
                w = World()
                order = w.obj("PurchaseOrder", {"status": "Pending",
                                                "expected_date": "2026-11-30",
                                                "po_number": "PO4820"})
                w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
                    "ordered_quantity": ("10", "ea"),
                    "received_quantity": ("0", "ea", "received")}), order)
                if reused:   # another order, no twin link, the same number
                    w.obj("PurchaseOrder", {"status": "Pending", "po_number": "4820"})
                for sent, sender, preview in (
                        ("2026-09-20T10:00:00Z", "sales@synth-vendor.example", "Got it."),
                        ("2026-10-01T10:00:00Z", "ops@synth-tenant.example",
                         "Could you please confirm the delivery date?")):
                    mail = w.obj("Email", {"conversation_id": "c1", "sent_at": sent,
                                           "sender": sender, "subject": "RE: PO4820",
                                           "body_preview": preview}, audience=BOX_OURS)
                    w.link("Email.mentions_purchase_order", mail, order, "lead")
                if self.inbox.exists():
                    self.inbox.unlink()
                summary = self.run_engine(w)
                found = self.events().get("vendor_no_reply", [])
                self.assertEqual([g["object_id"] for g in found], [] if reused else [order])
                if reused:
                    self.assertEqual(summary["not_evaluated"]["vendor_no_reply"],
                                     {"mention_of_reused_number": 2})

    def test_a_reused_number_resolves_to_the_order_of_the_conversations_supplier(self):
        """Two orders hold PO4820; the mail mentions it (a lead to each). The vendor who
        wrote in the conversation is one order's supplier: the request waits on that order,
        and the other order gets nothing. A vendor who supplies neither (or both, with no
        date to tell): ambiguous, no alert, and why is counted."""
        cases = {"supplier": "sales@synth-vendor.example",
                 "neither": "sales@other-vendor.example",
                 "both": "sales@synth-vendor.example"}
        for case, writer in cases.items():
            with self.subTest(case=case):
                w = World()
                vendor = w.obj("Supplier", {"name": "Synth Vendor",
                                            "bc_email": "sales@synth-vendor.example"})
                other_vendor = w.obj("Supplier", {"name": "Northwind Resins",
                                                  "bc_email": "info@northwind.example"})
                orders = []
                for number, supplier in (("PO4820", vendor),
                                         ("4820", vendor if case == "both" else other_vendor)):
                    order = w.obj("PurchaseOrder", {"status": "Pending",
                                                    "expected_date": "2026-11-30",
                                                    "po_number": number})
                    w.link("PurchaseOrder.ordered_from", order, supplier)
                    w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
                        "ordered_quantity": ("10", "ea"),
                        "received_quantity": ("0", "ea", "received")}), order)
                    orders.append(order)
                for sent, sender, preview in (
                        ("2026-09-20T10:00:00Z", writer, "Got it."),
                        ("2026-10-01T10:00:00Z", "ops@synth-tenant.example",
                         "Could you please confirm the delivery date?")):
                    mail = w.obj("Email", {"conversation_id": "c1", "sent_at": sent,
                                           "sender": sender, "subject": "RE: PO4820",
                                           "body_preview": preview}, audience=BOX_OURS)
                    for order in orders:
                        w.link("Email.mentions_purchase_order", mail, order, "lead",
                               reason=said("PO4820"))
                if self.inbox.exists():
                    self.inbox.unlink()
                summary = self.run_engine(w)
                found = [g["object_id"] for g in self.events().get("vendor_no_reply", [])]
                skipped = summary["not_evaluated"].get("vendor_no_reply", {})
                if case == "supplier":
                    self.assertEqual(found, [orders[0]])
                    self.assertTrue(self.stored(orders[0])["summary"].startswith("Lead"))
                    self.assertEqual(skipped, {"mention_of_reused_number": 2})
                else:
                    self.assertEqual(found, [])
                    self.assertEqual(skipped, {"mention_of_reused_number": 4})

    def test_a_mention_of_a_records_other_number_is_that_numbers_one_order(self):
        """A Portal order holds its BC number PO100025 (also a BC order's) and its
        FileMaker number PO5081 (its FileMaker twin's too). A mail naming PO5081 leads to
        the Portal and FileMaker records: one order, so the request waits on it. A mail
        naming PO100025 leads to the Portal and BC records: two orders, nothing decides,
        also when no FileMaker twin shares PO5081 (the Portal row's own unique number is
        not what the mail named: only a number the leads cite counts)."""
        for told, alert, twin in (("PO5081", True, True), ("PO100025", False, True),
                                  ("PO100025", False, False)):
            with self.subTest(said=told, twin=twin):
                w = World()
                portal = w.obj("PurchaseOrder", {"status": "Pending", "po_number": "PO100025",
                                                 "fm_po_number": "PO5081",
                                                 "expected_date": "2026-11-30"})
                fm = w.obj("PurchaseOrder", {"fm_status": "Pending",
                                             "fm_po_number": "PO5081" if twin else "PO9"},
                           binding="fm")
                bc = w.obj("PurchaseOrder", {"bc_status": "Open", "po_number": "PO100025"},
                           binding="bc")
                w.link("PurchaseOrder.same_record_as", portal, fm)
                w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
                    "ordered_quantity": ("10", "ea"),
                    "received_quantity": ("0", "ea", "received")}), portal)
                named = [portal, fm] if told == "PO5081" else [portal, bc]
                for sent, sender, preview in (
                        ("2026-09-20T10:00:00Z", "sales@synth-vendor.example", "Got it."),
                        ("2026-10-01T10:00:00Z", "ops@synth-tenant.example",
                         "Could you please confirm the delivery date?")):
                    mail = w.obj("Email", {"conversation_id": "c1", "sent_at": sent,
                                           "sender": sender, "subject": f"RE: {told}",
                                           "body_preview": preview}, audience=BOX_OURS)
                    for order in named:
                        w.link("Email.mentions_purchase_order", mail, order, "lead",
                               reason=said(told))
                if self.inbox.exists():
                    self.inbox.unlink()
                self.run_engine(w)
                found = [g["object_id"] for g in self.events().get("vendor_no_reply", [])]
                self.assertEqual(len(found), 1 if alert else 0)

    def test_vendor_no_reply_cardinality_one_ten_sixty(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                orders = set()
                for i in range(n):
                    order = po(w, "Pending", "2026-11-30")
                    orders.add(order)
                    w.obj("Email", {"conversation_id": f"c{i}", "sent_at":
                                    "2026-09-20T10:00:00Z", "sender": "a@synth-vendor.example"},
                          audience=BOX_OTHER)
                    asked = w.obj("Email", {"conversation_id": f"c{i}", "sent_at":
                                            "2026-10-01T10:00:00Z",
                                            "sender": "ops@synth-tenant.example",
                                            "body_preview": "Please confirm the ETA?"},
                                  audience=BOX_OTHER)
                    w.link("Email.mentions_purchase_order", asked, order, "lead")
                summary = self.run_engine(w)
                found = [g["object_id"] for g in self.events()["vendor_no_reply"]]
                self.assertEqual((len(found), set(found)), (n, orders))
                self.assertEqual(summary["coverage"]["vendor_no_reply"]["evaluated"], n)
                self.inbox.unlink()

    def test_negative_margin_from_the_checked_order_figures_only(self):
        w = World()
        loser = w.obj("SalesOrder", {})
        fine = w.obj("SalesOrder", {})
        undecided = w.obj("SalesOrder", {})
        figures = {}
        stale = w.obj("SalesOrder", {})
        w.link("SalesInvoice.bills", w.obj("SalesInvoice", {"posting_date": "2026-06-01"}),
               stale)   # more than 30 days before the newest invoice: not a candidate
        for order in (loser, fine, undecided):
            # the newest invoices are 70 days before the run's day: the window is the data's
            invoice = w.obj("SalesInvoice", {"posting_date": "2026-08-10"},
                            markings='["finance"]')
            figures[order] = handle(w.n)
            w.link("SalesInvoice.bills", invoice, order)

        def read_order(_call, order):
            self.assertNotEqual(order.object_id, stale)
            if order.object_id == undecided:
                return order
            order.margin = Decimal("-120.50") if order.object_id == loser else Decimal("5")
            order.revenue, order.cost = Decimal("100"), order.margin and Decimal("220.50")
            order.currency = "CAD"
            order.lines = [cp.Line(1, "l", None, invoice_handles=[figures[order.object_id]])]
            return order

        from decimal import Decimal
        with mock.patch.object(cp, "read_order", read_order):
            summary = self.run_engine(w, call=lambda *_: {})
        found = self.events()["negative_margin"]
        self.assertEqual([g["object_id"] for g in found], [loser])
        self.assertEqual(found[0]["event"]["amount"], {"value": "120.50", "currency": "CAD",
                                                       "basis": "loss"})
        # every amount carries its own currency
        self.assertEqual(found[0]["summary"], "Lost 120.50 CAD on item cost only: revenue "
                                              "100 CAD, item cost 220.50 CAD")
        self.assertEqual(summary["not_evaluated"]["negative_margin"],
                         {"margin_not_decided": 1})
        # a reader without finance may not open the figure: never sees the loss
        self.assertNotIn("negative_margin", self.events(held=frozenset()))

    def test_one_event_is_one_item_and_a_suspect_impact_stays_unranked(self):
        """A late PO's impact and the order it holds up are one item; an impact resting on
        a suspect total stays, marked suspect and never ranked by its figure; an order whose
        PO item the reader cannot see keeps its own."""
        suspect = {"value": "4410000.21", "currency": "CAD", "basis": "total_cost",
                   "suspect": "the total is over 1,000 times this vendor's usual order in CAD"}

        def gap(kind, oid, **event):
            return {"id": f"g-{kind}-{oid}", "kind": kind, "object_id": oid, "state": "open",
                    "object_type": "PurchaseOrder" if kind != "order_at_risk" else "SalesOrder",
                    "event": event, "follow_up": {}}
        inbox = {"status": "read", "gaps": [
            gap("po_impact", "PO-typo", orders=["SO-1"], orders_affected=1, days=63,
                amount=suspect),
            gap("order_at_risk", "SO-1", pos=["PO-typo"], orders_affected=1, days=63),
            gap("po_impact", "PO-a", orders=["SO-2"], orders_affected=1, days=9),
            gap("po_impact_margin", "PO-a", orders=["SO-2"], orders_affected=1, days=9),
            gap("order_at_risk", "SO-2", pos=["PO-a"], orders_affected=1, days=9),
            gap("order_at_risk", "SO-3", pos=["PO-hidden"], orders_affected=1, days=4)]}
        out = bf.build(inbox, routing=EVERYTHING, hidden=None, last_visit=None)
        self.assertEqual(sorted((i["kind"], i["object_id"]) for i in out["items"]),
                         [("order_at_risk", "SO-3"), ("po_impact", "PO-typo"),
                          ("po_impact_margin", "PO-a")])
        self.assertEqual(out["left_out"], {"same_event": 3})
        self.assertEqual(out["count"], 3)
        typo = next(i for i in out["items"] if i["object_id"] == "PO-typo")
        self.assertTrue(bf.money_words(typo["facts"]).startswith(bf.SUSPECT_WORDS))

    def test_overdue_orders_and_losses_stay_whatever_their_total_check_says(self):
        """A suspect or unchecked total never removes an overdue order, an order at risk or a
        loss: it stays, its figure unranked and marked (not checked is said so, never dropped)."""
        def gap(kind, oid, otype, **event):
            return {"id": f"g-{oid}", "kind": kind, "object_id": oid, "state": "open",
                    "object_type": otype, "event": event, "follow_up": {}}
        big = {"value": "9000000.00", "currency": "CAD", "basis": "total_amount",
               "suspect": "the total is over 1,000 times this customer's usual order in CAD"}
        unchecked = {"value": "500.00", "currency": "CAD", "basis": "total_amount",
                     "suspect": bf.NOT_CHECKED}
        loss = {"value": "80.00", "currency": "CAD", "basis": "loss",
                "suspect": bf.NOT_CHECKED}
        inbox = {"status": "read", "gaps": [
            gap("late_customer_order", "SO-21", "SalesOrder", days=21, amount=big),
            gap("late_customer_order", "SO-30", "SalesOrder", days=30, amount=unchecked),
            gap("negative_margin", "SO-loss", "SalesOrder", amount=loss),
            gap("order_at_risk", "SO-risk", "SalesOrder", pos=["PO-x"], days=5,
                amount=unchecked)]}
        out = bf.build(inbox, routing=EVERYTHING, hidden=None, last_visit=None)
        self.assertEqual({i["object_id"] for i in out["items"]},
                         {"SO-21", "SO-30", "SO-loss", "SO-risk"})
        self.assertEqual(out["left_out"], {})
        by = {i["object_id"]: i for i in out["items"]}
        self.assertTrue(bf.money_words(by["SO-21"]["facts"]).startswith(bf.SUSPECT_WORDS))
        self.assertEqual(bf.money_words(by["SO-30"]["facts"]),
                         "500.00 CAD order total, figure not checked")
        self.assertEqual(bf.money_words(by["SO-loss"]["facts"]),
                         "80.00 CAD lost, figure not checked")

    def test_a_no_charge_order_is_its_own_kind_never_a_loss(self):
        """Nothing invoiced net of credits (a sample, a replacement): its item cost is
        stated as a no-charge order, never "lost"."""
        from decimal import Decimal
        w = World()
        free, sold = w.obj("SalesOrder", {}), w.obj("SalesOrder", {})
        figures = {}
        for order in (free, sold):
            invoice = w.obj("SalesInvoice", {"posting_date": "2026-08-10"},
                            markings='["finance"]')
            figures[order] = handle(w.n)
            w.link("SalesInvoice.bills", invoice, order)

        def read_order(_call, order):
            order.revenue = Decimal("0") if order.object_id == free else Decimal("100")
            order.cost = Decimal("31.78")
            order.margin, order.currency = order.revenue - order.cost, "CAD"
            order.lines = [cp.Line(1, "l", None, invoice_handles=[figures[order.object_id]])]
            return order

        with mock.patch.object(cp, "read_order", read_order):
            self.run_engine(w, call=lambda *_: {})
        found = self.events()
        self.assertEqual([g["object_id"] for g in found.get("negative_margin", [])], [])
        (charge,) = found["no_charge_order"]
        self.assertEqual((charge["object_id"], charge["summary"]),
                         (free, "No-charge order: nothing invoiced, item cost 31.78 CAD"))
        self.assertNotIn("event", charge)
        self.assertNotIn(free, json.dumps(self.brief(EVERYTHING)["items"]))

    def test_a_credited_order_is_a_loss_unless_its_goods_came_back(self):
        """Revenue zero after credit memos: a no-charge order only when the goods were
        returned; credited without a return stays a loss (a write-off)."""
        from decimal import Decimal
        w = World()
        orders = {k: w.obj("SalesOrder", {}) for k in ("returned", "written_off")}
        figures = {}
        for order in orders.values():
            invoice = w.obj("SalesInvoice", {"posting_date": "2026-08-10"},
                            markings='["finance"]')
            figures[order] = handle(w.n)
            w.link("SalesInvoice.bills", invoice, order)

        def read_order(_call, order):
            order.revenue, order.cost = Decimal("0"), Decimal("31.78")
            order.margin, order.currency = Decimal("-31.78"), "CAD"
            back = [figures[order.object_id]] if order.object_id == orders["returned"] else []
            order.lines = [cp.Line(1, "l", None, credited=Decimal("2"),
                                   credit_amount=Decimal("100"),
                                   invoice_handles=[figures[order.object_id]],
                                   return_handles=back)]
            return order

        with mock.patch.object(cp, "read_order", read_order):
            self.run_engine(w, call=lambda *_: {})
        found = self.events()
        self.assertEqual([g["object_id"] for g in found["no_charge_order"]],
                         [orders["returned"]])
        self.assertEqual([g["object_id"] for g in found["negative_margin"]],
                         [orders["written_off"]])

    def test_a_partly_returned_credited_order_loses_only_the_unreturned_credit(self):
        """Fully credited, only one of two credited lines came back: the order is not
        no-charge; the loss is the credited value of the unreturned line."""
        from decimal import Decimal
        w = World()
        order_id = w.obj("SalesOrder", {})
        invoice = w.obj("SalesInvoice", {"posting_date": "2026-08-10"}, markings='["finance"]')
        fig = handle(w.n)
        w.link("SalesInvoice.bills", invoice, order_id)

        def read_order(_call, order):
            order.revenue, order.cost = Decimal("0"), Decimal("31.78")
            order.margin, order.currency = Decimal("-31.78"), "CAD"
            order.lines = [
                cp.Line(1, "a", None, credited=Decimal("1"), credit_amount=Decimal("60"),
                        invoice_handles=[fig], return_handles=[fig]),
                cp.Line(2, "b", None, credited=Decimal("1"), credit_amount=Decimal("40"),
                        invoice_handles=[fig])]
            return order

        with mock.patch.object(cp, "read_order", read_order):
            self.run_engine(w, call=lambda *_: {})
        found = self.events()
        self.assertNotIn("no_charge_order", found)
        (loss,) = found["negative_margin"]
        self.assertEqual(loss["event"]["amount"]["value"], "40")

    def test_a_late_check_that_failed_keeps_the_impacts_marked_not_checked(self):
        """When the late rule failed or timed out, no impact is dropped for not being on
        the late list; each says its late status was not checked."""
        def gap(kind, oid, **event):
            return {"kind": kind, "object_id": oid, "follow_up": {"event": event}}
        for status in ("failed", "truncated"):
            gaps = [gap("order_at_risk", "SO-1", pos=["PO-1"], material_pending=True),
                    gap("po_impact", "PO-1", orders=["SO-1"])]
            results = {"orders_at_risk": {"gaps": gaps, "not_evaluated": {}},
                       pe.LATE_RULE: {"status": status, "gaps": [], "reasons": []}}
            pe._late_list_only(None, results, date(2026, 11, 7))
            self.assertEqual(len(results["orders_at_risk"]["gaps"]), 2, status)
            self.assertNotIn("po_not_on_late_list", results["orders_at_risk"]["not_evaluated"])
            for g in gaps:
                self.assertTrue(g["follow_up"]["event"]["late_status_not_checked"])
        # a complete late list that does not hold the PO still drops it (one late rule)
        gaps = [gap("order_at_risk", "SO-1", pos=["PO-1"], material_pending=True)]
        results = {"orders_at_risk": {"gaps": gaps, "not_evaluated": {}},
                   pe.LATE_RULE: {"status": "complete", "gaps": [], "reasons": []}}
        pe._late_list_only(None, results, date(2026, 11, 7))
        self.assertEqual(results["orders_at_risk"]["gaps"], [])

    def test_negative_margin_window_counts_back_from_each_systems_own_newest_invoice(self):
        """BC's invoices end months before Sage's: each system's 30 days count back from
        its own newest invoice date, so BC's latest orders are read too; the systems take
        turns, each most recent first, so Sage's slow margin reads never keep BC's unread."""
        w = World()
        orders = {}
        for name, binding, day in (
                ("sage_later", "portal.sage_oe_invoice_header", "2026-10-25"),   # after
                ("sage_new", "portal.sage_oe_invoice_header", "2026-10-15"),
                ("sage_mid", "portal.sage_oe_invoice_header", "2026-10-02"),
                ("sage_old", "portal.sage_oe_invoice_header", "2026-09-10"),
                ("bc_new", "bc.sales_invoices", "2026-08-20"),
                ("bc_mid", "bc.sales_invoices", "2026-08-10"),
                ("bc_old", "bc.sales_invoices", "2026-07-10")):
            orders[name] = w.obj("SalesOrder", {}, binding=binding.replace("invoice", "order"))
            invoice = w.obj("SalesInvoice", {"posting_date": day}, binding=binding)
            w.link("SalesInvoice.bills", invoice, orders[name])
        asked = []

        def read_order(_call, order):
            asked.append(order.object_id)
            return order

        with mock.patch.object(cp, "read_order", read_order):
            summary = self.run_engine(w, call=lambda *_: {})
        self.assertEqual(asked, [orders[n] for n in ("bc_new", "sage_new", "bc_mid",
                                                     "sage_mid")])
        self.assertEqual(summary["coverage"]["negative_margin"]["total"], 4)

    def test_margins_are_read_on_the_pool_from_the_runs_start(self):
        """With a pool the order margins are read while the store-only rules run (the pool
        idles otherwise), once: the rule takes those readings at its turn."""
        from decimal import Decimal
        w = World()
        loser = w.obj("SalesOrder", {})
        invoice = w.obj("SalesInvoice", {"posting_date": "2026-10-10"})
        figure = handle(w.n)
        w.link("SalesInvoice.bills", invoice, loser)
        maps = []

        class Pool:   # in-process
            def map(self, task, items, left=None):
                if task is proactive_events.read_margin:
                    maps.append((threading.current_thread().name, list(items)))
                return [task(lambda *_: {}, item) for item in items]

        def read_order(_call, order):
            order.margin, order.revenue, order.cost = (Decimal("-1"), Decimal("1"),
                                                       Decimal("2"))
            order.currency = "CAD"
            order.lines = [cp.Line(1, "l", None, invoice_handles=[figure])]
            return order

        import threading
        from operating import proactive_events
        with mock.patch.object(cp, "read_order", read_order):
            summary = self.run_engine(w, call=lambda *_: {}, pool=Pool(),
                                      budget=pe.Budget(600))
        # read once, from the start: beside the store-only rules, not at the rule's turn
        self.assertEqual(maps, [("proactive-head-start", [loser])])
        self.assertEqual(summary["detectors"]["negative_margin"], "complete")
        self.assertEqual([g["object_id"] for g in self.events()["negative_margin"]], [loser])

    def test_negative_margin_is_read_with_finance_and_cited_at_its_value_markings(self):
        """The store marks a figure's money on its value, not its record: the engine's own
        sessions hold finance (else every margin is withheld) and the loss is cited at the
        value's markings, so a reader without finance never sees it."""
        from decimal import Decimal
        from ontology.core import Actor
        from operating import case_workspace_live as live
        from operating import proactive_pool
        w = World()
        loser = w.obj("SalesOrder", {})
        invoice = w.obj("SalesInvoice", {"posting_date": "2026-10-10", "subtotal": "100"})
        figure = handle(w.n)
        for row in w.rows["object_values"]:
            if row["object_id"] == invoice and row["property"] == "subtotal":
                row["markings"] = '["finance"]'
        w.link("SalesInvoice.bills", invoice, loser)

        def read_order(_call, order):
            order.margin, order.revenue, order.cost = (Decimal("-1"), Decimal("100"),
                                                       Decimal("101"))
            order.currency = "CAD"
            order.lines = [cp.Line(1, "l", None, invoice_handles=[figure])]
            return order

        with mock.patch.object(cp, "read_order", read_order):
            self.run_engine(w, call=lambda *_: {})
        self.assertEqual([g["object_id"] for g in self.events()["negative_margin"]], [loser])
        self.assertNotIn("negative_margin", self.events(held=ALL - {"finance"}))

        engine = Actor("plx", frozenset({"svc:engine"}), "restricted")
        held = {"finance": ["svc:engine"]}
        with mock.patch.object(live, "_Session") as session, \
                mock.patch.object(pe, "run", return_value={}):
            pe.refresh(mock.Mock(), engine, None, self.store, AS_OF, captures=None,
                       scan_coverage=False)
        self.assertEqual(session.call_args.kwargs["marking_grants"], held)
        spec = proactive_pool.WorkerSpec("/state", str(self.store), "plx", "svc:engine",
                                         "/mirror.json", None)
        with mock.patch.object(live, "_Session") as session, \
                mock.patch.object(live, "offline_runtime"), \
                mock.patch("integrations.source_mirror.AudienceMap.from_file"), \
                mock.patch.object(proactive_pool, "_CALL", None):
            proactive_pool._init(spec)
        self.assertEqual(session.call_args.kwargs["marking_grants"], held)

    def test_vendor_term_over_a_marked_price_is_shown_only_to_price_readers(self):
        """A reply's price that differs from a PO line's price tells what the hidden price
        is not: the line is cited at its values' markings, and a price (one the system
        leaves empty included) at purchasing_prices."""
        for marked in (True, False):
            with self.subTest(marked=marked):
                w = World()
                order = po_with(w)
                line = w.obj("PurchaseOrderLine", {"unit_price": "11.00"} if marked else {})
                for row in w.rows["object_values"]:
                    if row["object_id"] == line and row["property"] == "unit_price":
                        row["markings"] = '["purchasing_prices"]'
                request = w.obj("Email", {"sent_at": "2026-10-05T10:00:00Z",
                                          "conversation_id": "c1"}, audience="mail:box")
                w.link("Email.mentions_purchase_order", request, order, "lead")
                reply = handle(w.n)
                word = "differs" if marked else "fills"

                def call(_tool, _args, word=word, request=request, line=line, reply=reply):
                    return {"answer": [{
                        "name": "replies_1", "kind": "computed",
                        "value": {"untrusted_text": "price 12.50 USD per kg",
                                  "origin": {"citation": reply}},
                        "steps": [f"proposed update ({word}): price 12.50 USD per kg"],
                        "basis": [request, line]}]}

                self.run_engine(w, call=call)
                self.assertEqual(len(self.events()["vendor_terms_unapplied"]), 1)
                self.assertNotIn("vendor_terms_unapplied",
                                 self.events(held=ALL - {"finance", "purchasing_prices"}))
                self.inbox.unlink()

    def test_slow_investigations_stop_in_their_own_window_and_keep_what_they_found(self):
        """Each investigation can take minutes on a full store: each tool-call event
        detector runs in its own window (``DETECTOR_SECONDS``), so a slow vendor-mail scan
        never leaves the margins unrun, and what a cut-short scan found is kept (never
        discarded)."""
        from decimal import Decimal
        w = World()
        requests = {}
        for n in range(3):
            order = po_with(w)
            requests[order] = w.obj("Email", {"sent_at": f"2026-10-0{n + 1}T10:00:00Z",
                                              "conversation_id": f"c{n}"},
                                    audience="mail:box")
            w.link("Email.mentions_purchase_order", requests[order], order, "lead")
        loser = w.obj("SalesOrder", {})
        invoice = w.obj("SalesInvoice", {"posting_date": "2026-10-10"})
        figure = handle(w.n)
        w.link("SalesInvoice.bills", invoice, loser)
        now = [0.0]

        def call(_tool, args):        # every investigation costs 20 seconds
            now[0] += 20
            if args.get("family") == "F27":
                return {"answer": [{"name": "replies_1", "kind": "computed",
                                    "value": {"untrusted_text": "SYNTH", "origin": {
                                        "citation": handle(int(requests[args["root"]]
                                                               .rsplit(":", 1)[1]))}},
                                    "steps": ["proposed update (differs): SYNTH"],
                                    "basis": [requests[args["root"]], args["root"]]}]}
            return {"answer": []}

        def read_order(the_call, order):
            the_call("plx.investigate", {"family": "F10", "root": order.object_id})
            order.margin, order.revenue, order.cost = (Decimal("-1"), Decimal("1"),
                                                       Decimal("2"))
            order.currency = "USD"
            order.lines = [cp.Line(1, "l", None, invoice_handles=[figure])]
            return order

        w.build(self.store)
        nothing = lambda *_args: pe._result([])
        with mock.patch.object(cp, "read_order", read_order), \
                mock.patch.object(pe, "_orders_at_risk", nothing), \
                mock.patch.object(pe, "_card_rules", nothing):
            summary = pe.run(self.store, self.inbox, AS_OF, call=call, clock=lambda: MOMENT,
                             budget=pe.Budget(90, monotonic=lambda: now[0]),
                             detector_seconds={"vendor_terms_unapplied": 35})
        detectors = summary["detectors"]
        # its own window for the mail scan: two of three orders read, both findings kept
        self.assertEqual(detectors["vendor_terms_unapplied"], "truncated")
        self.assertIn(pe.TIME_BUDGET, summary["detector_reasons"]["vendor_terms_unapplied"])
        self.assertEqual(len(self.events()["vendor_terms_unapplied"]), 2)
        # the margins still get their share and finish
        self.assertEqual(detectors["negative_margin"], "complete")
        self.assertEqual([g["object_id"] for g in self.events()["negative_margin"]], [loser])

    def test_vendor_mail_is_read_again_only_when_its_thread_changed(self):
        """The vendor-terms check is incremental: an unchanged thread keeps its reading (no
        investigation), a new message in the conversation is investigated again."""
        def world(extra: bool) -> tuple[World, str, str]:
            w = World()
            order = po_with(w)
            request = w.obj("Email", {"sent_at": "2026-10-05T10:00:00Z",
                                      "conversation_id": "c1"}, audience="mail:box")
            w.link("Email.mentions_purchase_order", request, order, "lead")
            quiet = po_with(w)
            asked = w.obj("Email", {"sent_at": "2026-10-15T10:00:00Z",
                                    "conversation_id": "c2"}, audience="mail:box")
            w.link("Email.mentions_purchase_order", asked, quiet, "lead")
            if extra:   # the vendor answers in the first conversation
                w.obj("Email", {"sent_at": "2026-10-20T10:00:00Z", "conversation_id": "c1"},
                      audience="mail:box")
            self.request = request
            return w, order, quiet

        calls = []

        def call(tool, args):
            calls.append((args["family"], args["root"]))
            if args["root"] == self.order:
                return {"answer": [{"name": "replies_1", "kind": "computed",
                                    "value": {"untrusted_text": "SYNTH", "origin": {
                                        "citation": handle(int(self.request
                                                               .rsplit(":", 1)[1]))}},
                                    "steps": ["proposed update (fills): SYNTH"],
                                    "basis": [self.request, self.order]}]}
            return {"answer": []}

        def run(on: date, w: World) -> dict:
            for path in self.store.parent.glob("store.sqlite"):
                path.unlink()
            w.build(self.store)
            calls.clear()
            return pe.run(self.store, self.inbox, on, call=call, clock=lambda: MOMENT,
                          people=people())

        w, self.order, quiet = world(False)
        run(AS_OF, w)
        self.assertEqual(sorted(calls), sorted([("F27", self.order), ("F27", quiet)]))
        summary = run(AS_OF + timedelta(days=2), w)          # nothing changed
        self.assertEqual(calls, [])
        self.assertEqual(summary["detectors"]["vendor_terms_unapplied"], "complete")
        self.assertEqual(summary["reused"], {"vendor_terms_unapplied": 2})
        self.assertEqual(len(self.events()["vendor_terms_unapplied"]), 1)
        w, self.order, quiet = world(True)                     # a reply arrives
        run(AS_OF + timedelta(days=4), w)
        self.assertEqual(calls, [("F27", self.order)])

    def test_the_build_reuses_vendor_readings_and_the_vendor_pass_reads_apart(self):
        """The build's run never investigates vendor mail (``vendor_bound=0``: readings
        reused, the rest named as waiting for the vendor pass); the vendor pass runs that
        check alone, leaves every other rule's record as it was, and the two merge their
        readings whichever folds last."""
        w = World()
        order = po_with(w)
        request = w.obj("Email", {"sent_at": "2026-10-05T10:00:00Z", "conversation_id": "c1"},
                        audience="mail:box")
        w.link("Email.mentions_purchase_order", request, order, "lead")
        w.obj("Lot", {"expiry_date": "2026-10-25", "qc_status": "Released"})
        w.build(self.store)
        calls = []

        def call(tool, args):
            calls.append(args.get("family"))
            if args.get("family") == "F27":
                return {"answer": [{"name": "replies_1", "kind": "computed",
                                    "value": {"untrusted_text": "SYNTH", "origin": {
                                        "citation": handle(int(request.rsplit(":", 1)[1]))}},
                                    "steps": ["proposed update (differs): SYNTH"],
                                    "basis": [request, order]}]}
            return {"answer": []}

        build = pe.run(self.store, self.inbox, AS_OF, call=call, clock=lambda: MOMENT,
                       vendor_bound=0, people=people())
        self.assertNotIn("F27", calls)
        self.assertEqual(build["detector_reasons"]["vendor_terms_unapplied"], ["read_apart"])
        checks = {c["check"]: c for c in self.brief(EVERYTHING)["checks"]}
        self.assertEqual(checks["vendor_terms_unapplied"]["words"], "vendor terms in replies: "
                         "partly checked this run (the rest wait for the vendor-mail pass)")
        with closing(sqlite3.connect(self.inbox)) as conn:
            before = conn.execute("SELECT detector, run_at FROM detector_runs WHERE detector "
                                  "= 'lot_attention'").fetchall()
            runs = conn.execute("SELECT run_id, run_at FROM runs").fetchall()
        as_read = self.read()
        calls.clear()
        later = MOMENT + timedelta(hours=1)
        apart = pe.run(self.store, self.inbox, AS_OF, call=call, clock=lambda: later,
                       only=pe.INCREMENTAL, people=people())
        self.assertEqual(sorted(calls), ["F27"])
        self.assertEqual(set(apart["detectors"]), set(pe.INCREMENTAL))
        with closing(sqlite3.connect(self.inbox)) as conn:
            self.assertEqual(conn.execute("SELECT detector, run_at FROM detector_runs WHERE "
                                          "detector = 'lot_attention'").fetchall(), before)
        # the pass is no build's run: the run the inbox is read as of does not move, so
        # "new this generation" and stale stay the build's
        with closing(sqlite3.connect(self.inbox)) as conn:
            self.assertEqual(conn.execute("SELECT run_id, run_at FROM runs").fetchall(), runs)
        after = self.read()
        self.assertEqual((after["run_at"], after["as_of"], after["stale"]),
                         (as_read["run_at"], as_read["as_of"], as_read["stale"]))
        events = self.events()
        self.assertEqual(len(events["vendor_terms_unapplied"]), 1)
        self.assertEqual(len(events["lot_near_expiry"]), 1)     # untouched by the pass
        calls.clear()
        again = pe.run(self.store, self.inbox, AS_OF, call=call,
                       clock=lambda: later + timedelta(hours=1), vendor_bound=0,
                       people=people())
        self.assertEqual(calls, [])
        self.assertEqual(again["reused"], {"vendor_terms_unapplied": 1})
        self.assertEqual(again["detectors"]["vendor_terms_unapplied"], "complete")
        self.assertEqual(len(self.events()["vendor_terms_unapplied"]), 1)
        with self.assertRaises(ValueError):
            pe.run(self.store, self.inbox, AS_OF, call=call, only=("SYNTH-unknown",))

    def test_the_budget_is_a_hard_bound_and_store_checks_run_first(self):
        """The store-only event checks run before any tool-call rule, so a slow one never
        leaves them unrun; an investigation is told the time left and one that ran out is
        no reading; a query running at the deadline is interrupted, not let finish."""
        w = World()
        order = po_with(w)
        request = w.obj("Email", {"sent_at": "2026-10-05T10:00:00Z", "conversation_id": "c"},
                        audience="mail:box")
        w.link("Email.mentions_purchase_order", request, order, "lead")
        w.obj("Lot", {"expiry_date": "2026-10-25", "qc_status": "Released"})
        w.build(self.store)
        now, budgets = [0.0], []

        def call(tool, args):
            if tool == "plx.investigate":
                budgets.append(args.get("time_budget_ms"))
                now[0] += 10_000            # it ran out of time inside
                return {"answer": [], "answerability": {"reasons": ["time_budget_exhausted"]}}
            now[0] += 10_000                # every tool-call rule is slow
            return {}

        summary = pe.run(self.store, self.inbox, AS_OF, call=call, clock=lambda: MOMENT,
                         budget=pe.Budget(600, monotonic=lambda: now[0]))
        detectors = summary["detectors"]
        for name in ("eta_slipped", "lot_attention", "accepted_quote_no_order",
                     "invoice_quantity_mismatch"):
            self.assertEqual(detectors[name], "complete", name)
        self.assertIn("lot_near_expiry", summary["gaps_by_kind"])
        self.assertTrue(budgets and all(isinstance(b, int) and 0 < b <= 600_000
                                        for b in budgets), budgets)
        self.assertEqual(detectors["vendor_terms_unapplied"], "truncated")
        self.assertIn(pe.TIME_BUDGET, summary["detector_reasons"]["vendor_terms_unapplied"])

        # a set-based query still running when the time is up is interrupted
        def slow_sql(st, _as_of):
            now[0] = 10 ** 9
            st.conn.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
                            "WHERE i < 10000000) SELECT count(*) FROM n").fetchone()
            return pe._result([])
        now[0] = 0.0
        with mock.patch.object(pe, "_expired_quote", slow_sql):
            summary = pe.run(self.store, self.inbox, AS_OF, call=call, clock=lambda: MOMENT,
                             budget=pe.Budget(600, monotonic=lambda: now[0]))
        self.assertEqual(summary["detectors"]["expired_quote"], "truncated")
        self.assertEqual(summary["detector_reasons"]["expired_quote"], [pe.TIME_BUDGET])

    def test_the_amounts_and_their_check_run_in_the_reserve_inside_the_bound(self):
        """The detectors stop ``reserve`` seconds early; the amounts and their plausibility
        check use that reserve and are interrupted at the end like everything else: an
        unchecked total is then said to be unchecked, never stated as a fact."""
        from operating import suspect_money
        w = World()
        oid = po_with(w, "100.00", "CAD", expected_date="2026-10-25")
        revise(w, oid, "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
        w.build(self.store)
        now, seen = [0.0], []

        def slow(st, object_type, objects):
            seen.append(now[0])
            now[0] = 10 ** 6        # past the end: the next long query is interrupted
            st.conn.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
                            "WHERE i < 10000000) SELECT count(*) FROM n").fetchone()
            return {}
        budget = pe.Budget(400, monotonic=lambda: now[0], reserve=100)
        self.assertEqual(budget.left(), 300)          # the detectors' part
        with mock.patch.object(suspect_money, "suspects", slow):
            pe.run(self.store, self.inbox, AS_OF, clock=lambda: MOMENT, budget=budget)
        self.assertEqual(len(seen), 1)
        amount = self.events()["eta_slipped"][0]["event"]["amount"]
        self.assertEqual(amount["suspect"], "its plausibility could not be checked")

    def test_a_spent_reserve_never_loses_the_run_at_a_real_size(self):
        """With the reserve spent inside the plausibility check, on a store large enough
        that the run's own record (its scope) takes many database steps, the run still
        completes and folds: the total says it could not be checked, nothing is lost."""
        from operating import suspect_money
        w = World()
        oid = po_with(w, "100.00", "CAD", expected_date="2026-10-25")
        revise(w, oid, "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
        for n in range(3000):
            w.obj("Note", {"text": f"synthetic {n}"}, audience=f"api:n{n % 7}")
        w.build(self.store)
        now = [0.0]

        def spent(st, object_type, objects):
            now[0] = 10 ** 6      # the reserve runs out inside the check
            st.conn.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
                            "WHERE i < 10000000) SELECT count(*) FROM n").fetchone()
            return {}
        with mock.patch.object(suspect_money, "suspects", spent):
            summary = pe.run(self.store, self.inbox, AS_OF, clock=lambda: MOMENT,
                             budget=pe.Budget(400, monotonic=lambda: now[0], reserve=100))
        self.assertGreaterEqual(summary["scanned"], 3000)
        self.assertEqual(summary["detectors"]["eta_slipped"], "complete")
        amount = self.events()["eta_slipped"][0]["event"]["amount"]
        self.assertEqual(amount["suspect"], "its plausibility could not be checked")

    def test_cardinality_one_ten_sixty_lots_each_once(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                lots = {w.obj("Lot", {"expiry_date": (AS_OF + timedelta(days=i % 30))
                                      .isoformat(), "qc_status": "Released"})
                        for i in range(n)}
                self.run_engine(w)
                found = [g["object_id"] for g in self.events()["lot_near_expiry"]]
                self.assertEqual((len(found), set(found)), (n, lots))
                self.inbox.unlink()


class BriefingTest(Case):

    def world(self) -> tuple[World, dict]:
        w = World()
        ids = {}
        ids["big"] = po_with(w, "9000.00", expected_date="2026-10-25")
        revise(w, ids["big"], "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
        ids["small"] = po_with(w, "100.00", expected_date="2026-11-25")
        revise(w, ids["small"], "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
        ids["no_amount"] = po_with(w, expected_date="2026-12-25")
        revise(w, ids["no_amount"], "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
        ids["secret"] = po_with(w, "50.00", audience="api:secret", expected_date="2026-10-30")
        revise(w, ids["secret"], "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
        ids["hold"] = w.obj("Batch", {"quality_status": "On-Hold", "status": "Realized"})
        return w, ids

    def test_ranked_by_cited_amount_then_days_with_the_basis_stated(self):
        w, ids = self.world()
        self.run_engine(w)
        out = self.brief(EVERYTHING)
        order = [i["object_id"] for i in out["items"]]
        self.assertEqual(order[:3], [ids["big"], ids["small"], ids["secret"]])
        self.assertEqual(out["rank_basis"], bf.RANK_BASIS)
        self.assertEqual(out["items"][0]["facts"]["amount"],
                         {"value": "9000.00", "currency": "USD", "basis": "total_cost"})
        # the items without an amount follow, longest slip first
        self.assertEqual(order[3:], [ids["no_amount"], ids["hold"]])
        self.assertTrue(all(i["next_action"]["suggestion_only"] for i in out["items"]))

    def test_money_ranks_within_each_currency_and_one_stray_currency_keeps_it(self):
        """Amounts are never converted or compared across currencies, yet one amount in
        another currency does not switch money ranking off for the rest: each amount is
        placed within its own currency."""
        w, ids = self.world()
        cad = po_with(w, "1.00", "CAD", expected_date="2026-10-25")
        revise(w, cad, "expected_date", "2026-10-01", "2026-10-12T00:00:00.000000Z")
        self.run_engine(w)
        out = self.brief(EVERYTHING)
        self.assertEqual(out["rank_basis"], bf.RANK_BASIS)
        order = [i["object_id"] for i in out["items"]]
        # USD still by money: the 9,000 order before the smaller one; the lone CAD amount
        # is the largest of its currency, so it ranks with the largest (then by days)
        self.assertLess(order.index(ids["big"]), order.index(ids["small"]))
        self.assertLess(order.index(cad), order.index(ids["small"]))
        self.assertLess(order.index(ids["small"]), order.index(ids["no_amount"]))

    def test_routing_by_department_and_a_reader_with_none(self):
        w, ids = self.world()
        self.run_engine(w)
        proc = self.brief(PROC)
        self.assertNotIn(ids["hold"], [i["object_id"] for i in proc["items"]])
        self.assertEqual(proc["other_teams"], 1)
        quality = self.brief(QUALITY)
        self.assertEqual([i["object_id"] for i in quality["items"]], [ids["hold"]])
        nobody = self.brief(bf.Routing(basis="no_department_on_record"))
        self.assertEqual((nobody["count"], nobody["other_teams"]), (0, 5))
        self.assertIn("No department is on record",
                      bf.digest_text(nobody, first_name="Synth"))
        for role, count in (("ADMIN", 5), ("SUPER_ADMIN", 5), ("MANAGER", 0), ("SYNTH", 0)):
            with self.subTest(role=role):
                self.assertEqual(self.brief(bf.Routing(role=role))["count"], count)

    def test_hidden_items_are_counted_never_named_and_amounts_gated(self):
        w, ids = self.world()
        w.rows["object_values"] = [dict(r, markings='["purchasing_prices"]')
                                   if r["object_id"] == ids["big"] and r["property"] in (
                                       "total_cost", "currency") else r
                                   for r in w.rows["object_values"]]
        self.run_engine(w)
        no_secret = {"may_read": lambda key: key != "api:secret",
                     "held": ALL_MARKINGS}
        out = self.brief(PROC, **no_secret)
        text = json.dumps(out) + bf.digest_text(out, first_name="Synth")
        self.assertEqual(out["hidden"], 1)
        self.assertNotIn(ids["secret"], text)
        self.assertNotIn("2026-10-30", text)       # the hidden event's date
        big = next(i for i in out["items"] if i["object_id"] == ids["big"])
        self.assertNotIn("amount", big["facts"])   # the total is marked purchasing_prices
        self.assertNotIn("9000", text)
        # without the marking the big order no longer outranks the small one by money
        self.assertEqual(out["items"][0]["object_id"], ids["small"])
        self.assertEqual(out["hidden_by_type"], {"eta_slipped": 1})
        self.assertIn("Delivery date slipped: 1 more for your team is in places you can't "
                      "open (a count only)", bf.digest_text(out, first_name="Synth"))

    def test_an_inbox_from_before_the_history_tables_reads(self):
        # the live inbox predated detector_history and kind_history: Home, the briefing
        # and the late answer failed with "no such table" until the next engine run
        w, _ids = self.world()
        self.run_engine(w)
        whole = self.read()
        with closing(sqlite3.connect(self.inbox)) as conn:
            conn.execute("DROP TABLE kind_history")
            conn.execute("DROP TABLE detector_history")
            conn.commit()
        older = self.read()
        self.assertEqual([g["id"] for g in older["gaps"]],
                         [g["id"] for g in whole["gaps"]])
        self.assertEqual(older["kind_history"], {})

    def test_new_since_last_visit_and_new_this_generation(self):
        w, ids = self.world()
        self.run_engine(w)
        seen = pe.record_visit(self.inbox, "r", clock=lambda: MOMENT + timedelta(hours=1))
        self.assertEqual(pe.last_visit(self.inbox, "r"), seen)
        self.assertIsNone(pe.last_visit(self.inbox, "someone-else"))
        late = po_with(w, expected_date="2026-10-29")
        revise(w, late, "expected_date", "2026-10-21", "2026-10-13T00:00:00.000000Z")
        self.run_engine(w, at=MOMENT + timedelta(hours=2))
        out = self.brief(EVERYTHING)
        self.assertEqual(out["last_visit"], seen)
        new = [i["object_id"] for i in out["items"] if i["new_since_visit"]]
        self.assertEqual((new, out["new_since_visit"]), ([late], 1))
        self.assertEqual([i["object_id"] for i in out["items"] if i["new_this_generation"]],
                         [late])
        text = bf.digest_text(out, first_name="Synth")
        self.assertIn("(1 new since your last visit)", text)

    def test_other_gap_kinds_never_crowd_events_out_of_the_read(self):
        w, ids = self.world()
        for n in range(6):   # six expired quotes: gaps of a kind that is no event
            w.obj("ForwarderQuote", {"valid_until": f"2026-01-0{n + 1}"})
        self.run_engine(w)
        self.assertEqual(len([g for g in self.read()["gaps"] if g["kind"] == "expired_quote"]),
                         6)
        with mock.patch.object(bf, "MAX_READ", 5):
            out = self.brief(EVERYTHING)
        self.assertEqual(out["count"], 5)
        self.assertNotIn("expired_quote", {i["kind"] for i in out["items"]})
        # the read itself is narrowed to the event kinds, whatever the inbox's history holds
        events = pe.read_inbox(self.inbox, self.store, reader="r", may_read=everyone,
                               held=ALL, decide=admit, limit=500, kinds=bf.EVENT_KINDS)
        self.assertTrue(events["gaps"])
        self.assertLessEqual({g["kind"] for g in events["gaps"]}, set(bf.EVENT_KINDS))

    def test_an_unknown_kind_is_no_event_and_an_unread_inbox_is_said(self):
        inbox = {"status": "read", "gaps": [{"id": "pe-1", "kind": "SYNTH-unknown",
                                             "object_id": "X:1", "summary": "s"}],
                 "complete": True}
        out = bf.build(inbox, routing=EVERYTHING, hidden={}, last_visit=None)
        self.assertEqual((out["count"], out["items"]), (0, []))
        missing = bf.build({"status": "not_scheduled", "gaps": []}, routing=EVERYTHING,
                           hidden=None, last_visit=None)
        self.assertEqual(missing["status"], "not_scheduled")
        self.assertIn("not available yet", bf.digest_text(missing, first_name="Synth"))

    def test_late_is_the_one_late_list_and_each_item_says_if_seen_live(self):
        """"Late purchase order" is the landed late list (the Home count's kinds), never
        a PO with a late BC line beside it, so one PO is never briefed twice as late."""
        from operating import mcp_server
        self.assertEqual(set(bf.EVENT_TYPES["po_late"]["kinds"]),
                         set(mcp_server.LATE_PO_KINDS))
        gaps = [{"id": "pe-1", "kind": "unconfirmed_po", "object_id": "PurchaseOrder:1",
                 "summary": "s", "days_late": 3},
                {"id": "pe-2", "kind": "late_order", "object_id": "PurchaseOrder:1",
                 "summary": "s"},
                {"id": "pe-3", "kind": "quote_accepted_no_order", "object_id": "Quote:1",
                 "summary": "s"}]
        # seen live: per event type, from the recorded runs' history of each kind over this
        # data (never a fixed list, never per check: one check finds several types)
        kinds = {"unconfirmed_po": "2026-10-30T06:00", "unconfirmed_po:late": "2026-10-30T06:00",
                 "lot_near_expiry": "2026-10-29T06:00"}
        out = bf.build({"status": "read", "gaps": gaps, "complete": True,
                        "kind_history": kinds}, routing=EVERYTHING, hidden={},
                       last_visit=None)
        self.assertEqual([(i["id"], i["evidence"]) for i in out["items"]],
                         [("pe-1", "live"), ("pe-3", "fixture-only")])
        types = {t["event_type"]: t for t in out["types"]}
        self.assertEqual((types["po_late"]["found_live"], types["po_late"]["first_found"]),
                         (True, "2026-10-30"))
        self.assertIs(types["po_due"]["found_live"], False)    # one check, two types
        self.assertIs(types["lot_near_expiry"]["found_live"], True)
        self.assertIs(types["lot_on_hold"]["found_live"], False)
        self.assertIs(types["quote_accepted_no_order"]["found_live"], False)
        self.assertEqual(out["late_total"], 1)

    def test_a_suspect_total_is_flagged_never_ranked_nor_stated_as_fact(self):
        """A total far beyond its vendor's usual orders (a unit cost typed in cents) is
        "suspect source data (why)": the briefing keeps the item, never ranks by it nor
        states the figure."""
        w = World()
        for _ in range(4):     # the vendor's usual order: about 100 CAD
            po_with(w, "100.00", "CAD", vendor_number="V1")
        typo = po_with(w, "4410000.21", "CAD", vendor_number="V1",
                       expected_date="2026-10-25")
        revise(w, typo, "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
        fair = po_with(w, "120.00", "CAD", vendor_number="V1", expected_date="2026-10-22")
        revise(w, fair, "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
        self.run_engine(w)
        out = self.brief(EVERYTHING)
        items = {i["object_id"]: i for i in out["items"]}
        self.assertEqual(self.stored(typo)["event"]["amount"]["suspect"],
                         "the total is over 10,000 times this vendor's usual order in CAD")
        self.assertNotIn("suspect", items[fair]["facts"]["amount"])
        # kept and marked suspect, never ranked by its figure; the fair amount leads
        self.assertIn(typo, items)
        self.assertEqual(out["left_out"], {})
        self.assertEqual([i["object_id"] for i in out["items"]][:1], [fair])
        text = bf.digest_text(out, first_name="Synth")
        self.assertNotIn("4410000", text)
        self.assertEqual(bf.money_words(items[fair]["facts"]), "120.00 CAD order total")

    def test_a_total_and_its_lines_disagreeing_is_said_neutrally_and_normal_pos_pass(self):
        """A Business Central total equal to its lines, and a Portal total that adds unit
        prices with tax (the Portal's own convention), are normal: no flag. A total its
        lines truly disagree with says so neutrally with the lines' sum (to a reader who may
        open every line), is never called suspect and is not ranked by. A unit-cost
        outlier is measured only against peers in the line's own scope: a reader outside it
        gets the reason without the ratio band."""
        w = World()

        def po(total, lines, *, audience="api:a", line_audience="api:a", due="2026-10-25"):
            oid = po_with(w, total, "CAD", expected_date=due, audience=audience)
            revise(w, oid, "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
            for qty, cost, item in lines:
                line = w.obj("PurchaseOrderLine", {
                    "ordered_quantity": qty, "bc_direct_unit_cost": cost,
                    "bc_amount_excluding_tax": str(Decimal(qty) * Decimal(cost))},
                    audience=line_audience)
                w.link("PurchaseOrderLine.line_of", line, oid)
                w.link("PurchaseOrderLine.for_item", line, item)
            return oid

        from decimal import Decimal
        soap, box = w.obj("Item", {}), w.obj("Item", {})
        bc = po("1500.00", [("100", "10", soap), ("50", "10", box)])          # = its lines
        portal = po("22.60", [("100", "10", soap), ("50", "10", box)])        # (10+10) x 1.13
        wrong = po("900.00", [("100", "10", soap), ("50", "10", box)],        # neither
                   line_audience="api:lines")
        for _ in range(3):   # the box's usual cost, on lines a reader of api:a can't open
            po("10.00", [("1", "10", box)], audience="api:a", line_audience="api:lines")
        typo = po("15000.00", [("1", "15000", box)], line_audience="api:lines")
        self.run_engine(w)
        items = {i["object_id"]: i for i in self.brief(EVERYTHING)["items"]}
        self.assertNotIn("suspect", items[bc]["facts"]["amount"])
        self.assertNotIn("disagree", items[bc]["facts"]["amount"])
        self.assertNotIn("suspect", items[portal]["facts"]["amount"])
        self.assertNotIn("disagree", items[portal]["facts"]["amount"])
        self.assertEqual(bf.money_words(items[wrong]["facts"]),
                         "order total 900.00 CAD and its lines disagree (lines sum to 1500 CAD)")
        self.assertEqual(self.stored(typo)["event"]["amount"]["suspect"],
                         "a line's unit cost is over 1,000 times what this item costs on "
                         "other orders")
        self.assertIn("suspect", items[typo]["facts"]["amount"])   # kept, marked suspect
        # a reader of api:a only: the lines' sum is not stated, the band is dropped
        own = self.brief(EVERYTHING, may_read=lambda key: key == "api:a")
        mine = {i["object_id"]: i for i in own["items"]}
        # nothing its unopenable lines say, not even that they disagree; and unranked
        self.assertEqual(bf.money_words(mine[wrong]["facts"]),
                         "900.00 CAD total (not checked against its lines)")
        self.assertNotIn("disagree", json.dumps(mine[wrong]))
        self.assertEqual(bf.money_words(bf._out(bf._item(
            self.stored(typo, may_read=lambda key: key == "api:a"), 0, None, None), 1, {})[
            "facts"]), "suspect source data (a line's unit cost is far above what this item "
                       "costs on other orders)")
        # a disagreeing total is not ranked by money
        ranked = [i["object_id"] for i in self.brief(EVERYTHING)["items"]]
        self.assertLess(ranked.index(bc), ranked.index(wrong))

    def test_portal_currency_spellings_are_read_and_its_unit_price_total_said_as_such(self):
        """The Portal writes CAD$ and USD$ (C$ and US$ read the same way): its orders carry
        amounts. A Portal total that adds its lines' unit prices is stated as that, never
        as an order's value, and is not ranked; a code no rule reads stays unstated."""
        from decimal import Decimal
        w = World()

        def po(total, currency, lines, binding="portal.app_purchase_order"):
            oid = po_with(w, total, currency, expected_date="2026-10-25", binding=binding)
            revise(w, oid, "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
            for qty, cost in lines:
                line = w.obj("PurchaseOrderLine", {
                    "ordered_quantity": qty, "unit_price": cost,
                    "line_total": str(Decimal(qty) * Decimal(cost))})
                w.link("PurchaseOrderLine.line_of", line, oid)
            return oid

        whole = po("226.00", "USD$", [("1", "100"), ("1", "100")])     # its lines, with tax
        small = po("113.00", "US$", [("1", "100")])
        canadian = po("50.00", "C$", [("1", "50")])
        summed = po("22.60", "CAD$", [("100", "10"), ("50", "10")])    # unit prices x 1.13
        unknown = po("99.00", "CDN", [("1", "99")])
        unpriced = po("0", "CAD$", [("400", "0")])                      # no price entered
        no_lines = po("5000.00", "CAD$", [])          # an fm_generated/Sage-like order
        bc_no_lines = po("5000.00", "CAD", [], binding="bc.purchase_orders")
        self.run_engine(w)
        out = self.brief(EVERYTHING)
        items = {i["object_id"]: i for i in out["items"]}
        self.assertEqual(items[whole]["facts"]["amount"]["currency"], "USD")
        self.assertEqual(items[canadian]["facts"]["amount"]["currency"], "CAD")
        self.assertEqual(bf.money_words(items[whole]["facts"]), "226.00 USD order total")
        self.assertEqual(bf.money_words(items[summed]["facts"]),
                         "22.60 CAD Portal total (its lines' unit prices added, not quantity "
                         "x price)")
        self.assertNotIn("amount", items[unknown]["facts"])
        self.assertNotIn("amount", items[unpriced]["facts"])           # never "0 CAD total"
        # a Portal total with no priced line to check it: said so, never ranked; a Business
        # Central total is its order's own
        self.assertEqual(bf.money_words(items[no_lines]["facts"]),
                         "5000.00 CAD Portal total (lines not priced)")
        self.assertEqual(bf.money_words(items[bc_no_lines]["facts"]), "5000.00 CAD order total")
        order = [i["object_id"] for i in out["items"]]
        # USD by money (226 before 113); the unit-price sum and the unknown code unranked
        self.assertLess(order.index(whole), order.index(small))
        self.assertLess(order.index(canadian), order.index(summed))
        self.assertLess(order.index(small), order.index(unknown))
        self.assertLess(order.index(bc_no_lines), order.index(no_lines))

    def test_an_outlier_reads_suspect_even_when_its_total_follows_the_portal_convention(self):
        """The suspicion is checked first: a Portal total that adds unit prices but one of
        whose unit costs is far above the item's usual reads "suspect source data"."""
        from decimal import Decimal
        w = World()
        item = w.obj("Item", {})

        def po(total, qty, cost):
            oid = po_with(w, total, "CAD$", expected_date="2026-10-25",
                          binding="portal.app_purchase_order")
            revise(w, oid, "expected_date", "2026-10-20", "2026-10-12T00:00:00.000000Z")
            line = w.obj("PurchaseOrderLine", {
                "ordered_quantity": qty, "unit_price": cost,
                "line_total": str(Decimal(qty) * Decimal(cost))},
                binding="portal.app_purchase_order_line")
            w.link("PurchaseOrderLine.line_of", line, oid)
            w.link("PurchaseOrderLine.for_item", line, item)
            return oid

        for _ in range(3):
            po("11.30", "1", "10")
        outlier = po("1130.00", "100", "1000")    # unit prices x 1.13, the cost 100 x usual
        self.run_engine(w)
        stored = self.stored(outlier)
        self.assertIn(outlier, [i["object_id"] for i in self.brief(EVERYTHING)["items"]])
        self.assertEqual(bf.money_words(bf._out(bf._item(stored, 0, None, None), 1, {})["facts"]),
                         "suspect source data (a line's unit cost is over 100 times what "
                         "this item costs on other orders)")

    def test_due_today_is_not_late_and_acknowledged_items_stay_counted(self):
        """The late count is Home's own (``late_total`` over the same read): a PO due today
        is "due today", and an acknowledged late PO is still listed, marked so."""
        from operator_api.backend import late_total
        gaps = [{"id": "pe-1", "kind": "unconfirmed_po", "object_id": "PurchaseOrder:1",
                 "summary": "s", "days_late": 3, "state": "open"},
                {"id": "pe-2", "kind": "unconfirmed_po", "object_id": "PurchaseOrder:2",
                 "summary": "s", "state": "open"},                       # due today
                {"id": "pe-3", "kind": "late_po", "object_id": "PurchaseOrder:3",
                 "summary": "s", "days_late": 40, "state": "acknowledged"}]
        inbox = {"status": "read", "gaps": gaps, "complete": True}
        out = bf.build(inbox, routing=EVERYTHING, hidden={}, last_visit=None)
        kinds = {i["id"]: (i["event_type"], i["acknowledged"]) for i in out["items"]}
        self.assertEqual(kinds, {"pe-1": ("po_late", False), "pe-2": ("po_due", False),
                                 "pe-3": ("po_late", True)})
        self.assertEqual(out["late_total"], late_total(inbox))
        self.assertEqual(out["late_total"], 2)
        # acknowledged after the others, even when longer late
        self.assertEqual([i["id"] for i in out["items"]][-1], "pe-3")
        self.assertEqual(out["acknowledged"], 1)
        self.assertIn("[acknowledged]", bf.digest_text(out, first_name="Synth"))

    def test_top_items_then_every_other_item_grouped_by_type_with_its_count(self):
        """A large briefing is never cut silently: the top items one by one, every other
        item in its type's group (the counts add up to the total), the since-visit view
        counting exactly what it lists."""
        gaps = [{"id": f"pe-l{n}", "kind": "unconfirmed_po", "object_id": f"PurchaseOrder:{n}",
                 "summary": "s", "days_late": 100 - n, "first_seen": f"2026-10-{10 + n % 9}"}
                for n in range(30)]
        gaps += [{"id": f"pe-x{n}", "kind": "lot_near_expiry", "object_id": f"Lot:{n}",
                  "summary": "s", "event": {"days": n}, "first_seen": "2026-10-18"}
                 for n in range(60)]
        inbox = {"status": "read", "gaps": gaps, "complete": True}
        out = bf.build(inbox, routing=EVERYTHING, hidden={}, last_visit="2026-10-17")
        self.assertEqual((out["count"], out["top"], len(out["items"])), (90, 7, 90))
        groups = {g["event_type"]: g for g in out["groups"]}
        self.assertEqual(sum(g["count"] for g in groups.values()), out["count"])
        self.assertEqual((groups["po_late"]["count"], groups["lot_near_expiry"]["count"]),
                         (30, 60))
        text = bf.digest_text(out, first_name="Synth")
        self.assertIn("- Late purchase order: 23 more", text)
        self.assertIn("- Lot near expiry: 60 more", text)
        new = bf.build(inbox, routing=EVERYTHING, hidden={}, last_visit="2026-10-17",
                       since_visit=True)
        self.assertEqual(new["count"], out["new_since_visit"])
        self.assertEqual(len(new["items"]), new["count"])
        self.assertTrue(all(i["new_since_visit"] for i in new["items"]))

    def test_hidden_counts_follow_each_types_policy(self):
        """Hidden events are counts only, and for a type whose very existence may sit in
        an excluded person's mailbox (vendor mail) not even a count is given."""
        inbox = {"status": "read", "gaps": [], "complete": True}
        out = bf.build(inbox, routing=EVERYTHING, last_visit=None,
                       hidden={"lot_on_hold": 592, "vendor_no_reply": 3,
                               "vendor_terms_unapplied": 1})
        self.assertEqual(out["hidden_by_type"], {"lot_on_hold": 592})
        self.assertEqual(out["hidden"], 592)
        text = bf.digest_text(out, first_name="Synth")
        self.assertIn("Batch on quality hold: 592 more for your team are in places you "
                      "can't open (a count only).", text)
        self.assertNotIn("vendor has not replied", text.lower())
        types = {t["event_type"]: t for t in out["types"]}
        self.assertIsNone(types["vendor_no_reply"]["found_live"])
        self.assertIn("Vendor replies: not checked this run.", text)

    def test_digest_preview_text(self):
        w, ids = self.world()
        self.run_engine(w)
        out = self.brief(PROC)
        text = bf.digest_text(out, first_name="Synth Person", labels={ids["big"]: "PO SYNTH-9"})
        self.assertTrue(text.startswith(
            "Hello Synth Person,\n\nToday in one line: In the latest check: 4 delivery "
            "dates slipped.\n\n4 items need your attention as of 2026-10-19."))
        self.assertIn("1. Delivery date slipped: PO SYNTH-9", text)
        self.assertIn("(9000.00 USD order total)", text)
        self.assertIn("Next: Check whether the new date", text)
        self.assertIn("1 more you may see is routed to other teams.", text)
        self.assertTrue(text.endswith("(Preview only: this digest is not sent.)"))

    def test_every_event_type_builds_with_a_next_step_for_every_department(self):
        """One gap of every event kind, read by an admin and by a reader of each department
        any type names: the briefing builds (an undated PO once had no next step and broke
        every reader's briefing) and every item says a next step and a line of its type."""
        gaps = [{"id": f"pe-{n}", "kind": kind, "object_id": f"X:{n}", "summary": "s",
                 "first_seen": "2026-10-18"} for n, kind in enumerate(bf.EVENT_KINDS)]
        inbox = {"status": "read", "gaps": gaps, "complete": True}
        departments = {d for spec in bf.EVENT_TYPES.values() for d in spec["departments"]}
        for routing in [EVERYTHING, *(bf.Routing(frozenset({d})) for d in departments)]:
            out = bf.build(inbox, routing=routing, hidden={}, last_visit=None)
            self.assertTrue(out["items"], routing)
            for item in out["items"]:
                self.assertTrue(item["next_action"]["text"])
                self.assertIn(item["event_type"], bf.TODAY_WORDS)
        self.assertEqual(set(bf.TODAY_WORDS), set(bf.EVENT_TYPES))
        self.assertLessEqual(set(bf.NEXT_ACTIONS_BY_DEPARTMENT), set(bf.EVENT_TYPES))

    def test_the_next_step_is_said_for_the_department_that_acts_on_it(self):
        """A Quality reader is never told to "ask Quality"; Purchasing chases the supplier of
        an order at risk while Sales tells the customer; an admin, routed everything, gets
        the general step; QA and QC speak as Quality."""
        gaps = [{"id": "pe-h", "kind": "lot_on_hold", "object_id": "Batch:1", "summary": "s"},
                {"id": "pe-r", "kind": "order_at_risk", "object_id": "SalesOrder:1",
                 "summary": "s", "event": {"orders_affected": 1}}]
        inbox = {"status": "read", "gaps": gaps, "complete": True}

        def steps(routing):
            out = bf.build(inbox, routing=routing, hidden={}, last_visit=None)
            return {i["event_type"]: i["next_action"]["text"] for i in out["items"]}
        for code in ("QUALITY", "QA", "QC"):
            hold = steps(bf.Routing(frozenset({code})))["lot_on_hold"]
            self.assertNotIn("Ask Quality", hold)
            self.assertIn("decide its release", hold)
        self.assertIn("Ask Quality", steps(bf.Routing(frozenset({"PROD"})))["lot_on_hold"])
        self.assertIn("Chase the supplier", steps(bf.Routing(frozenset({"PROC"})))[
            "customer_order_at_risk"])
        self.assertIn("Tell the customer", steps(bf.Routing(frozenset({"ACCTMGMT"})))[
            "customer_order_at_risk"])
        self.assertEqual(steps(EVERYTHING), {name: bf.NEXT_ACTIONS[name][0]
                                             for name in ("lot_on_hold",
                                                          "customer_order_at_risk")})

    def test_the_fewest_days_to_expiry_first_and_what_the_latest_check_found_new(self):
        """Days left to expiry count down (tomorrow before next month); days late count up;
        at equal orders and money what the latest check found new comes before the backlog."""
        run = "2026-11-01T08:00:00+00:00"
        gaps = [{"id": f"pe-x{d}", "kind": "lot_near_expiry", "object_id": f"Lot:{d}",
                 "summary": "s", "event": {"days": d}, "first_seen": "2026-10-20"}
                for d in (30, 1, 12, 0)]
        gaps += [{"id": "pe-old", "kind": "unconfirmed_po", "object_id": "PurchaseOrder:1",
                  "summary": "s", "days_late": 110, "first_seen": "2026-10-20"},
                 {"id": "pe-new", "kind": "unconfirmed_po", "object_id": "PurchaseOrder:2",
                  "summary": "s", "days_late": 1, "first_seen": run}]
        out = bf.build({"status": "read", "gaps": gaps, "complete": True, "run_at": run},
                       routing=EVERYTHING, hidden={}, last_visit=None)
        order = [i["id"] for i in out["items"]]
        self.assertEqual([i for i in order if i.startswith("pe-x")],
                         ["pe-x0", "pe-x1", "pe-x12", "pe-x30"])
        self.assertEqual(order[:2], ["pe-new", "pe-old"])
        self.assertIn("then what the latest check found new", out["rank_basis"])

    def test_undated_pos_are_listed_but_rank_below_every_real_event(self):
        """An open PO with no due date is ranked by money among its own kind, yet however
        large its amount it follows a loss, an overdue order or a late PO."""
        def amt(v):
            return {"amount": {"value": v, "currency": "USD", "basis": "total_cost"}}

        def gap(n, kind, event, **extra):
            return {"id": f"pe-{n}", "kind": kind, "object_id": f"X:{n}", "summary": "s",
                    "first_seen": "2026-10-20", "event": event, **extra}
        gaps = [gap(1, "undated_po", amt("900000.00")), gap(2, "undated_po", amt("500000.00")),
                gap(3, "negative_margin", amt("10.00")),
                gap(4, "late_po", amt("5.00"), days_late=3),
                gap(5, "order_at_risk", {"orders_affected": 1})]
        out = bf.build({"status": "read", "gaps": gaps, "complete": True},
                       routing=EVERYTHING, hidden={}, last_visit=None)
        kinds = [i["kind"] for i in out["items"]]
        self.assertEqual(len(kinds), 5)
        self.assertEqual(kinds[3:], ["undated_po", "undated_po"])
        self.assertEqual([i["object_id"] for i in out["items"][3:]], ["X:1", "X:2"])

    def test_today_in_one_line(self):
        """The line names what is new (the latest check, or since the reader's last visit)
        worst first, else what is still open with the soonest expiry; an empty briefing says
        so or counts only what the reader can't open; at most three kinds are named."""
        run = "2026-11-01T08:00:00+00:00"

        def gap(n, kind, first="2026-10-20", **extra):
            return {"id": f"pe-{n}", "kind": kind, "object_id": f"X:{n}", "summary": "s",
                    "first_seen": first, **extra}
        gaps = [gap(1, "unconfirmed_po", run, days_late=1),
                gap(2, "late_po", run, days_late=2), gap(3, "unconfirmed_po", run, days_late=1),
                gap(4, "order_at_risk", run, event={"orders_affected": 1}),
                gap(5, "lot_near_expiry", event={"days": 9}),
                gap(6, "lot_near_expiry", event={"days": 4})]
        inbox = {"status": "read", "gaps": gaps, "complete": True, "run_at": run}
        out = bf.build(inbox, routing=EVERYTHING, hidden={}, last_visit=None)
        self.assertEqual(out["today"], "In the latest check: 1 customer order now at risk "
                                       "from late POs; 3 POs went late.")
        self.assertTrue(bf.digest_text(out, first_name="S").startswith(
            "Hello S,\n\nToday in one line: In the latest check: 1 customer order"))
        visit = bf.build(inbox, routing=EVERYTHING, hidden={}, last_visit="2026-11-02")
        self.assertEqual(visit["today"], "Nothing new since your last visit; still open: "
                         "1 customer order at risk from late POs; 3 POs late; 2 lots near "
                         "expiry (the first in 4 days).")
        quality = bf.build(inbox, routing=bf.Routing(frozenset({"QUALITY"})),
                           hidden={"lot_on_hold": 5}, last_visit=None)
        self.assertEqual(quality["today"], "Nothing new in the latest check; still open: "
                                           "2 lots near expiry (the first in 4 days); 5 "
                                           "more you can't open.")
        empty = {"status": "read", "gaps": [], "complete": True}
        self.assertEqual(bf.build(empty, routing=EVERYTHING, hidden={"lot_on_hold": 5},
                                  last_visit=None)["today"],
                         "Nothing you can open needs attention; 5 more for your team are in "
                         "places you can't open.")
        self.assertEqual(bf.build(empty, routing=EVERYTHING, hidden={}, last_visit=None)[
            "today"], "Nothing routed to you needs attention today.")
        none = bf.build(inbox, routing=bf.Routing(basis="no_department_on_record"),
                        hidden={}, last_visit=None)
        self.assertIn("No department is on record", none["today"])
        many = [gap(n, kind, run) for n, kind in enumerate(
            ("lot_on_hold", "lot_near_expiry", "quote_accepted_no_order",
             "invoice_quantity_mismatch", "undated_po"), start=10)]
        line = bf.build({"status": "read", "gaps": many, "complete": True, "run_at": run},
                        routing=EVERYTHING, hidden={}, last_visit=None)["today"]
        self.assertEqual(line.count(";"), 3)
        self.assertTrue(line.endswith("; 2 more kinds."))
        one = bf.build({"status": "read", "gaps": many[:4], "complete": True, "run_at": run},
                       routing=EVERYTHING, hidden={}, last_visit=None)["today"]
        self.assertTrue(one.endswith("; 1 more kind."), one)   # never "1 more kinds"

    def test_routing_from_an_employee_entitlement(self):
        class Ent:
            actor = object()
            routing_departments = frozenset({"PROC"})
            portal_role = "USER"
        self.assertEqual(bf.Routing.of(Ent()).departments, {"PROC"})
        Ent.routing_departments = frozenset()
        self.assertEqual(bf.Routing.of(Ent()).basis, "no_department_on_record")
        self.assertEqual(bf.Routing.of(None).basis, "not_an_employee")
        Ent.actor = None
        self.assertEqual(bf.Routing.of(Ent()).basis, "not_an_employee")


if __name__ == "__main__":
    unittest.main()
