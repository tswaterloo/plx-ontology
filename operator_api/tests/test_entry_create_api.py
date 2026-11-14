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
from operating import write_intents as wi
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


class CreateBase(ti.Desk):
    """The one box over a fixture store (no tests of its own)."""

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


class CreateApi(CreateBase):

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


SANDBOX_CO = "c0c0c0c0-1111-4111-8111-000000000001"
BC_CUSTOMER = "b1b1b1b1-2222-4222-8222-000000000002"


class CreateToTheWriteDesk(CreateBase):
    """A cited order header goes to the write desk as a dry-run BC Sandbox entry prepared
    by the reader (a grant holder); never Production, never a PHSI company, never an AP
    invoice, never confirmed or sent here."""

    def setUp(self):
        super().setUp()
        w = StoreWorld()
        item = w.obj("Product", {"code": "SYN-10"}, binding="bc.items")
        sage = w.obj("Customer", {"name": "Synth customer", "erp_code": "C100"},
                     binding="portal.sage_ar_customer")
        bc = w.obj("Customer", {"name": "Synth customer", "erp_code": "C100"},
                   binding="bc.customers")
        bc_handle = f"bc://production/{SANDBOX_CO}/customers/{BC_CUSTOMER}@v1"
        for row in w.rows["object_versions"] + w.rows["object_values"]:
            if row["object_id"] == bc:
                row["l1_handle"] = bc_handle
        w.link("Customer.same_record_as", sage, bc)
        path = self.backend.entry_store.parent / "w.sqlite"
        w.build(path)
        with sqlite3.connect(path) as c:
            for kind, norm, oid in (("item_code", "SYN-10", item),
                                    ("customer_erp_code", "C100", sage),
                                    ("customer_erp_code", "C100", bc)):
                c.execute("INSERT INTO identifiers (identifier_kind, normalized, raw, role, "
                          "object_id, property, l1_handle, field_path, lineage, recorded_at) "
                          "VALUES (?, ?, ?, 'primary', ?, 'code', 'h', 'row.code', "
                          "'workbench', '2026-01-01T00:00:00Z')", (kind, norm, norm, oid))
        self.backend.entry_store = path
        self.backend.open_ids = {item, sage, bc}
        self.party = (sage, bc)
        self.backend.write_grants = wi.WriteGrants({ANA[0]: ["bc:salesOrders"]})
        self.backend.writes_environ = {**self.backend.writes_environ,
                                       wi.SANDBOX_ENV: "Sandbox2",
                                       ec.SANDBOX_COMPANY: SANDBOX_CO}
        self.companies = {SANDBOX_CO: "PLX Sandbox"}
        self.backend.write_company = lambda env, co: self.companies.get(co) \
            if env == "Sandbox2" else None
        self.reads = []

        def current(env, company, entity_set, rid):
            self.reads.append((env, company, entity_set, rid))
            return {"etag": 'W/"c1"', "fields": {"id": rid, "number": "C100"}}
        self.backend.write_current = current

    PO = ("create an order from this PO: Customer #: C100\nPO 88123\n"
          "1. SYN-10 Gallon soap: 6 cases")

    def writes(self, who, text=None):
        out = self.instruct(who, text or self.PO)
        self.assertEqual(out.status_code, 200, out.text)
        return out.json()["answer"]["instruction"]["writes"]

    def test_a_cited_header_is_prepared_on_the_desk_for_the_sandbox_only(self):
        (w,) = self.writes(self.ana)
        self.assertEqual(w["status"], "prepared", w)
        intent = self.backend._wdesk().intent(w["intent_id"])
        self.assertEqual((intent["environment"], intent["company_id"], intent["operation"]),
                         ("Sandbox2", SANDBOX_CO, "sales_order_create"))
        payload = intent["payload"]
        (line,) = payload.pop("lines")
        self.assertEqual(payload, {"customerNumber": "C100", "externalDocumentNumber": "88123"})
        self.assertEqual((line["lineType"], line["lineObjectNumber"], line["quantity"]),
                         ("Item", "SYN-10", "6"))
        self.assertNotIn("unitPrice", line)              # no cited price: never filled
        self.assertIn("SYN-10", line["description"])
        self.assertEqual(intent["precondition"], {"etag": 'W/"c1"', "party_id": BC_CUSTOMER})
        self.assertEqual(intent["prepared_by"]["email"], ANA[0])
        self.assertTrue(all(r[0] == "Sandbox2" for r in self.reads))
        # prepared only: nothing approved, executed or sent
        status = self.backend._wdesk().status(w["intent_id"])
        self.assertEqual(status["status"], "prepared")
        self.assertEqual(self.sends, [])
        # the same PO again is the same entry, not a second one
        (again,) = self.writes(self.ana)
        self.assertEqual(again["intent_id"], w["intent_id"])

    def test_no_sandbox_a_phsi_company_or_an_unopened_party_is_refused(self):
        self.companies[SANDBOX_CO] = "PHSI Group"
        (w,) = self.writes(self.ana)
        self.assertEqual(w["status"], "refused")
        self.assertIn("never written", " ".join(w["problems"]))
        self.companies[SANDBOX_CO] = "PLX Sandbox"
        for env in ("", "Production", "production"):
            self.backend.writes_environ = {**self.backend.writes_environ,
                                           wi.SANDBOX_ENV: env}
            (w,) = self.writes(self.ana)
            self.assertEqual(w["status"], "refused", env)
            self.assertIn("No BC sandbox", " ".join(w["problems"]))
        self.backend.writes_environ = {**self.backend.writes_environ,
                                       wi.SANDBOX_ENV: "Sandbox2"}
        self.backend.open_ids = self.backend.open_ids - {self.party[1]}
        (w,) = self.writes(self.ana)
        self.assertEqual(w["status"], "refused")
        self.assertIn("record you cannot open", " ".join(w["problems"]))
        self.assertNotIn("C100", " ".join(w["problems"]))
        self.assertEqual(self.backend._wdesk()._rows(), [])     # nothing put on the desk

    def test_a_customer_that_is_a_check_is_refused_and_no_grant_offers_nothing(self):
        (w,) = self.writes(self.ana, "create an order from this PO: PO 88123\n"
                                     "1. SYN-10 Gallon soap: 6 cases")
        self.assertEqual(w["status"], "refused")
        self.assertIn("customer is a check", " ".join(w["problems"]))
        self.assertEqual(self.writes(self.ben), [])
        self.backend.write_grants = wi.WriteGrants({})
        self.assertEqual(self.writes(self.ana), [])

    def test_a_line_with_a_check_holds_the_whole_entry(self):
        (w,) = self.writes(self.ana, "create an order from this PO: Customer #: C100\n"
                                     "PO 88124\n1. SYN-10 Gallon soap: 6 cases\n"
                                     "2. ZZ-404 Unknown thing: 2 cases")
        self.assertEqual(w["status"], "refused")
        self.assertIn("Line 2: its item or quantity is a check", " ".join(w["problems"]))
        self.assertEqual(self.backend._wdesk()._rows(), [])

    def test_an_ap_invoice_never_goes_to_the_desk(self):
        self.backend.write_grants = wi.WriteGrants({ANA[0]: ["bc:salesOrders",
                                                             "bc:purchaseOrders"]})
        self.assertEqual(self.writes(self.ana, "enter this invoice: Invoice INV-1 total "
                                               "CAD 100.00"), [])
