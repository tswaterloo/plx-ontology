"""Create mode in the one box ("create an order from this PO", "create a PO for SYN-10",
"enter this invoice"): a pre-filled record comes back as the reader's answer, every filled
value with its source and every other field a check; nothing is entered or sent, no model
runs, and a value from a record the reader cannot open is never shown. Evidence:
isolated-fixture (the Ask's instruct harness over a fixture store)."""

from __future__ import annotations

import sqlite3
import tempfile
import time
from datetime import date
from pathlib import Path

from operating import autopilot as ap
from operating import proactive_engine as pe
from operating.tests.test_proactive_engine import World as StoreWorld
from operator_api import entry_create_api as ec
from operator_api import models as m
from operator_api.tests import test_instruct as ti
from operator_api.tests.test_instruct import ANA, BEN, BEN_MAIL, World


class CreateWorld(World):
    """Ana opens the item records; Ben opens none."""
    entry_store: Path
    open_ids: set = set()

    def _entry_ctx(self):
        st = pe._Store(self.entry_store, threads=1)
        return st, ap.Ctx(st, date(2026, 10, 19), time.monotonic() + 60, ())

    def object_page(self, reader, object_type, object_id, *, quick=False):
        if object_id not in self.open_ids or reader.session.subject_oid != ANA[1]:
            raise KeyError("object_not_found")
        return m.ObjectPage(object_id=object_id, type=object_type, label=f"Item {object_id}",
                            summary="", status=m.ObjectStatus(state="open", text="Open"),
                            freshness=m.Freshness(), facts=[])


class CreateApi(ti.Desk):
    def setUp(self):
        super().setUp()
        self.backend.__class__ = CreateWorld
        self.model = self.backend.instruct_call = ti.Model()     # never called for a create
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        w = StoreWorld()
        item = w.obj("Product", {"code": "SYN-10"}, binding="bc.items")
        sup = w.obj("Supplier", {"name": "Synth supplier"})
        po = w.obj("PurchaseOrder", {"po_number": "P-1", "order_date": "2026-10-01",
                                     "currency": "CAD"})
        w.link("PurchaseOrder.ordered_from", po, sup)
        pol = w.obj("PurchaseOrderLine", {"ordered_quantity": "120", "unit_price": "2.10",
                                          "uom": "KG"})
        w.link("PurchaseOrderLine.line_of", pol, po)
        w.link("PurchaseOrderLine.for_item", pol, item)
        sol = w.obj("SalesOrderLine", {"outstanding_quantity": "30"})
        w.link("SalesOrderLine.for_item", sol, item)
        self.records = {"item": item, "supplier": sup, "po": po, "demand": sol}
        path = Path(tmp.name) / "s.sqlite"
        w.build(path)
        with sqlite3.connect(path) as c:
            c.execute("INSERT INTO identifiers (identifier_kind, normalized, raw, role, "
                      "object_id, property, l1_handle, field_path, lineage, recorded_at) "
                      "VALUES ('item_code', 'SYN-10', 'SYN-10', 'primary', ?, 'code', 'h', "
                      "'row.code', 'workbench', '2026-01-01T00:00:00Z')", (item,))
        self.backend.entry_store = path
        self.backend.open_ids = {item}

    def test_a_pasted_po_prefills_an_order_with_sources_and_checks_and_runs_no_model(self):
        out = self.instruct(self.ana, "create an order from this PO: PO 88123\n"
                                      "1. SYN-10 Gallon soap: 6 cases")
        self.assertEqual(out.status_code, 200, out.text)
        answer = out.json()["answer"]
        facts = [f["text"] for f in answer["facts"]]
        self.assertIn("Customer PO: 88123", facts)
        self.assertTrue(any(f.startswith("Item (line 1): ") for f in facts), facts)
        self.assertIn("Quantity (line 1): 6", facts)
        said = answer["direct_answer"]
        self.assertIn("Nothing is entered", said)
        self.assertIn("Check - Customer:", said)            # no sender: never guessed
        self.assertIn("Check - Unit price (line 1):", said)
        self.assertEqual(self.model.prompts, [])

    def test_a_value_from_a_record_the_reader_cannot_open_is_a_check_never_shown(self):
        out = self.instruct(self.ben, "create an order from this PO: PO 88123\n"
                                      "1. SYN-10 Gallon soap: 6 cases")
        self.assertEqual(out.status_code, 200, out.text)
        answer = out.json()["answer"]
        self.assertFalse(any(f["text"].startswith("Item") for f in answer["facts"]))
        self.assertIn("Check - Item (line 1): from a record you cannot open",
                      answer["direct_answer"])
        self.assertNotIn("SYN-10", " ".join(f["text"] for f in answer["facts"]))

    def test_another_persons_mail_or_nothing_to_read_is_refused(self):
        out = self.instruct(self.ana, "create an order from this PO",
                            {"kind": "email", "handle": BEN_MAIL})
        self.assertNotEqual(out.status_code, 200)
        self.assertIn("That mail is not one you can open.", out.text)
        empty = self.instruct(self.ana, "enter this invoice")
        self.assertIn("create_refused", empty.text)
        self.assertIn("paste its text after a colon", empty.text)
        self.assertNotIn('"facts"', empty.text)

    def test_a_po_shows_only_what_comes_from_records_the_reader_opens(self):
        # Ana opens the item only: the supplier, its price and both quantity hints come
        # from records she cannot open, so none of them is shown
        said = self.instruct(self.ana, "create a PO for SYN-10").json()["answer"]
        text = said["direct_answer"] + " ".join(f["text"] for f in said["facts"])
        self.assertIn("Check - Supplier:", text)
        self.assertNotIn("suggested", text)
        self.assertNotIn("2.10", text)
        self.assertNotIn(self.records["supplier"], text)
        # all of them opened: the supplier suggested by its label, never filled; the
        # quantity a check with both hints
        self.backend.open_ids = set(self.records.values())
        said = self.instruct(self.ana, "create a PO for SYN-10").json()["answer"]
        self.assertIn(f"suggested Item {self.records['supplier']} (the items' last PO)",
                      said["direct_answer"])
        self.assertIn("suggested 30 (open sales order demand)", said["direct_answer"])
        # no supplier named: no price or last quantity "from this supplier" is offered
        self.assertIn("Check - Unit price (line 1): no earlier price from this supplier",
                      said["direct_answer"])
        self.assertFalse(any(f["text"].startswith("Supplier") for f in said["facts"]))

    def test_only_a_record_to_create_takes_the_create_path(self):
        self.assertEqual(ec.create_kind("create an order from this PO"), "order")
        self.assertEqual(ec.create_kind("create a PO for SYN-10"), "purchase_order")
        self.assertEqual(ec.create_kind("enter this invoice"), "supplier_invoice")
        for other in ("draft an invoice reminder to Pat", "create an order confirmation",
                      "what is the order status", "reply to this", "make a note",
                      "Create a bill of materials for SYN-10",
                      "prepare the bill of lading for order 51007",
                      "create an invoice for SO 51007", "prepare an invoice for Acme",
                      "Enter the PO number in the tracker",
                      "prepare the order for shipping to Acme"):
            self.assertIsNone(ec.create_kind(other), other)
