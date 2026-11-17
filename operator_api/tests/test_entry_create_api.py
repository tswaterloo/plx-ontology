"""Create mode in the one box ("create an order from this PO", "create a PO for SYN-10",
"enter this invoice"): a pre-filled record comes back as the reader's answer, every filled
value with its source and every other field a check; nothing is entered or sent, no model
runs, and a value from a record the reader cannot open is never shown. Evidence:
isolated-fixture (the Ask's instruct harness over a fixture store)."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import types
from datetime import date
from pathlib import Path

from operating import autopilot as ap
from operating import autopilot_handlers
from operating import proactive_engine as pe
from operating import write_intents as wi
from operating.tests.test_proactive_engine import World as StoreWorld
from operator_api import entry_create_api as ec
from operator_api import models as m
from operator_api.errors import RecordNotFound, UserFacing
from operator_api.tests import test_instruct as ti
from operator_api.tests.test_instruct import ANA, BEN, BEN_MAIL, World


class CreateWorld(World):
    """Ana opens the item records; Ben opens none."""
    entry_store: Path
    open_ids: set = set()

    def _entry_ctx(self):
        st = pe._Store(self.entry_store, threads=1)
        return st, autopilot_handlers.Ctx(st, date(2026, 10, 19), time.monotonic() + 60, ())

    def object_page(self, reader, object_type, object_id, *, quick=False):
        if object_id not in self.open_ids or reader.session.subject_oid != ANA[1]:
            raise RecordNotFound()
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


class DeskBase(CreateBase):
    """Ana holds the BC sales order grant; a sandbox company is configured (no tests of
    its own)."""

    def setUp(self):
        super().setUp()
        w = StoreWorld()
        item = w.obj("Product", {"code": "SYN-10"}, binding="bc.items")
        sage = w.obj("Customer", {"name": "Synth customer", "erp_code": "C100",
                                  "currency": "CAD"}, binding="portal.sage_ar_customer")
        bc = w.obj("Customer", {"name": "Synth customer", "erp_code": "C100",
                                "currency": "CAD"}, binding="bc.customers")
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


class CreateToTheWriteDesk(DeskBase):
    """A cited order header goes to the write desk as a dry-run BC Sandbox entry prepared
    by the reader (a grant holder); never Production, never a PHSI company, never an AP
    invoice, never confirmed or sent here."""

    PO = ("create an order from this PO: Customer #: C100\nPO 88123\n"
          "1. SYN-10 Gallon soap 6 CS 4.50 27.00")

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
        self.assertEqual(line["unitPrice"], "4.50")     # the price the PO states, cited
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

    def test_a_line_whose_price_is_a_check_holds_the_whole_entry(self):
        # A line sent without a price takes BC's item default, so a
        # price the PO does not state (a last price is only a suggestion) holds the entry
        # until a person enters or confirms it
        (w,) = self.writes(self.ana, "create an order from this PO: Customer #: C100\n"
                                     "PO 88125\n1. SYN-10 Gallon soap: 6 cases")
        self.assertEqual(w["status"], "refused")
        self.assertIn("Line 1: its price is a check", " ".join(w["problems"]))
        self.assertEqual(self.backend._wdesk()._rows(), [])
        self.assertEqual(self.sends, [])

    def test_an_ap_invoice_never_goes_to_the_desk(self):
        self.backend.write_grants = wi.WriteGrants({ANA[0]: ["bc:salesOrders",
                                                             "bc:purchaseOrders"]})
        self.assertEqual(self.writes(self.ana, "enter this invoice: Invoice INV-1 total "
                                               "CAD 100.00"), [])


class ConfirmHeldEntry(DeskBase):
    """An entry held by its checks is settled by the person preparing it: each value they
    enter or confirm is kept as theirs (who, when), and with none left the entry goes to
    the write desk as a fully cited one does. Nothing is written to BC, approved or sent;
    nobody else, and no view-as session, sees or confirms it."""

    PRICELESS = ("create an order from this PO: Customer #: C100\nPO 88125\n"
                 "1. SYN-10 Gallon soap: 6 cases")

    def held(self, who, text=None):
        out = self.instruct(who, text or self.PRICELESS)
        self.assertEqual(out.status_code, 200, out.text)
        return out.json()["answer"]["instruction"]

    def confirm(self, who, entry_id, **body):
        return who.post(f"/api/entries/{entry_id}/confirm", json=body)

    def test_a_confirmed_price_clears_the_hold_and_the_entry_goes_to_the_desk(self):
        part = self.held(self.ana)
        (w,) = part["writes"]
        self.assertEqual(w["status"], "refused")
        entry = part["entry"]
        by_key = {f["key"]: f for f in entry["fields"]}
        self.assertEqual({k: f["status"] for k, f in by_key.items()},
                         {"customer": "cited", "line:1:item": "cited",
                          "line:1:quantity": "cited", "line:1:unit_price": "check"})
        self.assertEqual(entry["open"], 1)
        # the same PO asked again is the same held entry
        self.assertEqual(self.held(self.ana)["entry"]["entry_id"], entry["entry_id"])
        eid = entry["entry_id"]
        # a price in another currency than the customer's never goes to the desk
        out = self.confirm(self.ana, eid, key="line:1:unit_price", value="4.50",
                           currency="usd")
        self.assertEqual(out.status_code, 200, out.text)
        got = out.json()
        self.assertEqual((got["open"], got["write"]["status"]), (0, "refused"))
        self.assertIn("billed in CAD", " ".join(got["write"]["problems"]))
        self.assertEqual(self.backend._wdesk()._rows(), [])
        # entered again in CAD: prepared on the desk, by Ana, as a dry run
        got = self.confirm(self.ana, eid, key="line:1:unit_price", value="4.50",
                           currency="CAD").json()
        self.assertEqual(got["write"]["status"], "prepared", got)
        price = next(f for f in got["fields"] if f["key"] == "line:1:unit_price")
        self.assertEqual((price["status"], price["value"], price["currency"]),
                         ("confirmed", "4.50", "CAD"))
        self.assertTrue(price["confirmed_by"] and price["confirmed_at"])
        intent = self.backend._wdesk().intent(got["write"]["intent_id"])
        (line,) = intent["payload"]["lines"]
        self.assertEqual((line["lineObjectNumber"], line["quantity"], line["unitPrice"]),
                         ("SYN-10", "6", "4.50"))
        self.assertEqual(intent["prepared_by"]["email"], ANA[0])
        self.assertIn("1 value entered or confirmed by", intent["reason"])
        self.assertEqual(self.backend._wdesk().status(got["write"]["intent_id"])["status"],
                         "prepared")
        self.assertEqual(self.sends, [])
        # the hold reads back with its desk entry; nothing on it changes any more
        view = self.ana.get(f"/api/entries/{eid}").json()
        self.assertEqual((view["open"], view["write"]["intent_id"]),
                         (0, got["write"]["intent_id"]))
        again = self.confirm(self.ana, eid, key="line:1:unit_price", value="9", currency="CAD")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(len(self.backend._wdesk()._rows()), 1)

    def test_only_the_preparer_confirms_and_view_as_never(self):
        eid = self.held(self.ana)["entry"]["entry_id"]
        self.assertIsNone(self.held(self.ben).get("entry"))     # no grant: nothing held
        price = {"key": "line:1:unit_price", "value": "4.50", "currency": "CAD"}
        # Ben (no grant) and an unknown id: the same not-found, nothing told apart
        for who, ident in ((self.ben, eid), (self.ana, "eh_" + "0" * 24), (self.ana, "x")):
            self.assertEqual(who.get(f"/api/entries/{ident}").status_code, 404)
            self.assertEqual(self.confirm(who, ident, **price).status_code, 404)
        # another grant holder is not the preparer either
        self.backend.write_grants = wi.WriteGrants({ANA[0]: ["bc:salesOrders"],
                                                    BEN[0]: ["bc:salesOrders"]})
        self.assertEqual(self.confirm(self.ben, eid, **price).status_code, 404)
        # a view-as session is refused at the door and at the backend
        from operator_api import view_as as va
        out = self.ana.post(f"/api/entries/{eid}/confirm", json=price,
                            headers={va.HEADER: "someone"})
        self.assertEqual(out.status_code, 403, out.text)
        viewing = types.SimpleNamespace(
            session=types.SimpleNamespace(view_as=True, subject_oid=ANA[1],
                                          identities=frozenset({ANA[0], ANA[1]})),
            display_name="Ana", email=ANA[0], mode="mock")
        with self.assertRaises(UserFacing) as no:
            self.backend.entry_confirm(viewing, eid, m.EntryConfirm(**price))
        self.assertEqual(no.exception.status, 403)
        # the grant gone: the preparer confirms nothing either
        self.backend.write_grants = wi.WriteGrants({})
        self.assertEqual(self.confirm(self.ana, eid, **price).status_code, 403)
        self.assertEqual(self.backend._wdesk()._rows(), [])
        self.assertEqual([r["event"] for r in self.backend._holds()._rows()], ["held"])

    def test_unknown_cited_or_malformed_values_are_refused(self):
        eid = self.held(self.ana)["entry"]["entry_id"]
        for body, status in (
                ({"key": "line:9:unit_price", "value": "1", "currency": "CAD"}, 422),
                ({"key": "supplier", "record": self.records["supplier"]}, 422),
                ({"key": "lines", "value": "1"}, 422),
                ({"key": "line:1:item", "record": self.records["item"]}, 409),
                ({"key": "line:1:unit_price", "value": "NaN", "currency": "CAD"}, 422),
                ({"key": "line:1:unit_price", "value": "-1", "currency": "CAD"}, 422),
                ({"key": "line:1:unit_price", "value": "1e5", "currency": "CAD"}, 422),
                ({"key": "line:1:unit_price", "value": "", "currency": "CAD"}, 422),
                ({"key": "line:1:unit_price", "value": "4.50"}, 422),
                ({"key": "line:1:unit_price", "value": "4.50", "currency": "dollars"}, 422)):
            out = self.confirm(self.ana, eid, **body)
            self.assertEqual(out.status_code, status, (body, out.text))
        self.assertEqual([r["event"] for r in self.backend._holds()._rows()], ["held"])
        self.assertEqual(self.backend._wdesk()._rows(), [])

    def test_a_customer_is_picked_as_a_record_the_reader_opens(self):
        sage, bc = self.party
        part = self.held(self.ana, "create an order from this PO: PO 88123\n"
                                   "1. SYN-10 Gallon soap 6 CS 4.50 27.00")
        entry = part["entry"]
        self.assertEqual([f["key"] for f in entry["fields"] if f["status"] == "check"],
                         ["customer"])
        eid = entry["entry_id"]
        self.backend.open_ids = self.backend.open_ids - {bc}
        self.assertEqual(self.confirm(self.ana, eid, key="customer", record=bc).status_code,
                         404)
        self.assertEqual(self.confirm(self.ana, eid, key="customer",
                                      record=self.party[0].replace("Customer:", "Product:")
                                      ).status_code, 422)
        self.backend.open_ids = self.backend.open_ids | {bc}
        got = self.confirm(self.ana, eid, key="customer", record=sage).json()
        self.assertEqual(got["write"]["status"], "prepared", got)
        customer = got["fields"][0]
        self.assertEqual((customer["status"], customer["value"].split()[0]),
                         ("confirmed", "C100"))
        intent = self.backend._wdesk().intent(got["write"]["intent_id"])
        self.assertEqual(intent["payload"]["customerNumber"], "C100")
        self.assertEqual(intent["precondition"]["party_id"], BC_CUSTOMER)

    def test_every_line_of_a_long_entry_is_kept_once(self):
        # cardinality probe: 1, 10 and 60 priceless lines, each confirmed, one desk entry
        # carrying every line once, in order
        for n in (1, 10, 60):
            self.backend._write_desk = None
            self.backend.state_dir = Path(tempfile.mkdtemp(dir=self.tmp))
            self.backend.entry_holds = None
            text = f"Customer #: C100\nPO 9{n:04d}\n" + \
                "\n".join(f"{i}. SYN-10 Gallon soap: {i} cases" for i in range(1, n + 1))
            if n == 1:
                entry = self.held(self.ana, "create an order from this PO: " + text)["entry"]
            else:   # longer than an Ask line: held as the Ask holds it
                from operating import entry_assist as ea
                st, ctx = self.backend._entry_ctx()
                try:
                    got = ea.order_entry(ctx, text=text)
                finally:
                    st.close()
                entry = {"entry_id": self.backend._holds().hold(ANA[1], "order", got)}
                entry = self.ana.get(f"/api/entries/{entry['entry_id']}").json()
            self.assertEqual(entry["open"], n, n)
            got = None
            for i in range(1, n + 1):
                got = self.confirm(self.ana, entry["entry_id"], key=f"line:{i}:unit_price",
                                   value=f"{i}.25", currency="CAD").json()
            self.assertEqual(got["write"]["status"], "prepared", (n, got))
            lines = self.backend._wdesk().intent(got["write"]["intent_id"])["payload"]["lines"]
            self.assertEqual([(ln["quantity"], ln["unitPrice"]) for ln in lines],
                             [(str(i), f"{i}.25") for i in range(1, n + 1)], n)
            self.assertEqual(len(self.backend._wdesk()._rows()), 1, n)

    def test_a_number_takes_commas_only_between_thousands(self):
        # "1,0,0" is never read as 100: a comma anywhere but between groups of three
        # digits is refused, so the person says what they mean
        eid = self.held(self.ana)["entry"]["entry_id"]
        for raw in ("1,0,0", "1,00", "1000,000", ",100", "1,000,", "1,,000", "12,3456.5",
                    "1.000,50", "4,50", "４.50", "1 000"):
            out = self.confirm(self.ana, eid, key="line:1:unit_price", value=raw,
                               currency="CAD")
            self.assertEqual(out.status_code, 422, (raw, out.text))
        self.assertEqual([r["event"] for r in self.backend._holds()._rows()], ["held"])
        got = self.confirm(self.ana, eid, key="line:1:unit_price", value="1,234.50",
                           currency="CAD").json()
        price = next(f for f in got["fields"] if f["key"] == "line:1:unit_price")
        self.assertEqual(price["value"], "1234.50")
        line = self.backend._wdesk().intent(got["write"]["intent_id"])["payload"]["lines"][0]
        self.assertEqual(line["unitPrice"], "1234.50")

    def test_a_corrupt_line_is_skipped_logged_alarmed_and_old_holds_pruned(self):
        from datetime import datetime, timedelta, timezone
        from operating import entry_confirm as ecf
        from operating import ops_alarms
        eid = self.held(self.ana)["entry"]["entry_id"]
        store = self.backend._holds().dir / "holds.jsonl"
        old = {"event": "held", "entry_id": "eh_" + "a" * 24, "preparer": ANA[1],
               "kind": "order", "digest": "d", "prefill": {"header": {}, "lines": []},
               "at": "2026-01-01T00:00:00Z"}
        import json
        with store.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(old) + "\n")
            fh.write("{not json\n")
            fh.write(json.dumps({"event": "held", "entry_id": "eh_x"}) + "\n")  # no fields
            fh.write('{"event": "confirmed", "entry_id": "' + eid + '", "pre')  # torn
        with self.assertLogs("operating.entry_confirm", "WARNING") as logged:
            view = self.ana.get(f"/api/entries/{eid}")
        self.assertEqual(view.status_code, 200, view.text)
        self.assertIn("3 unreadable", " ".join(logged.output))
        self.assertNotIn("not json", " ".join(logged.output))      # counts, never content
        now = datetime.now(timezone.utc)
        (alarm,) = ops_alarms.entry_holds_checks(self.backend.state_dir, now)
        self.assertEqual((alarm["name"], alarm["state"], alarm["value"]),
                         ("entry_holds_unreadable", "breach", 3))
        # the next write drops the bad lines and the expired hold, and the entry still works
        with self.assertLogs("operating.entry_confirm", "WARNING"):
            got = self.confirm(self.ana, eid, key="line:1:unit_price", value="4.50",
                               currency="CAD").json()
        self.assertEqual(got["write"]["status"], "prepared", got)
        rows = [json.loads(x) for x in store.read_text("utf-8").splitlines()]
        self.assertEqual([r["event"] for r in rows], ["held", "confirmed", "prepared"])
        self.assertEqual({r["entry_id"] for r in rows}, {eid})
        self.assertEqual(oct(store.stat().st_mode & 0o777), "0o600")
        # the folder and the note are the state group's to read (the ops alarm runs as the
        # jobs' user); the store and its lock are not
        entry = self.backend.state_dir / "entry"
        self.assertEqual(oct(entry.stat().st_mode & 0o777), "0o750")
        self.assertEqual(oct((entry / ecf.UNREADABLE_FILE).stat().st_mode & 0o777), "0o640")
        self.assertEqual(oct((entry / "holds.lock").stat().st_mode & 0o777), "0o600")
        (later,) = ops_alarms.entry_holds_checks(self.backend.state_dir,
                                                 now + timedelta(hours=25))
        self.assertEqual(later["state"], "ok")
        (self.backend.state_dir / "entry" / ecf.UNREADABLE_FILE).write_text("{", "utf-8")
        (odd,) = ops_alarms.entry_holds_checks(self.backend.state_dir, now)
        self.assertEqual(odd["state"], "unknown")
        # a count that is not a number is said as unknown, never "None"
        (self.backend.state_dir / "entry" / ecf.UNREADABLE_FILE).write_text(json.dumps(
            {"lines": "x", "first_at": "2026-01-01T00:00:00Z",
             "last_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}), "utf-8")
        (vague,) = ops_alarms.entry_holds_checks(self.backend.state_dir, now)
        self.assertEqual((vague["state"], vague["value"]), ("breach", None))
        self.assertIn("an unknown number of unreadable", vague["detail"])
        self.assertNotIn("None", vague["detail"])

    def test_a_second_process_never_prepares_the_entry_again(self):
        import subprocess
        import sys
        import threading
        from operating import entry_confirm as ecf
        eid = self.held(self.ana)["entry"]["entry_id"]
        folder = self.backend._holds().dir
        # another API process holds the store's lock: this one waits for it
        other = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; from pathlib import Path; from operating import entry_confirm as e\n"
             "h = e.EntryHolds(Path(sys.argv[1]))\n"
             "with h.lock:\n print('locked', flush=True); sys.stdin.read()\n",
             str(folder)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            cwd=str(Path(ecf.__file__).resolve().parents[1]))
        try:
            self.assertEqual(other.stdout.readline().strip(), "locked")
            done = []
            t = threading.Thread(target=lambda: done.append(self.confirm(
                self.ana, eid, key="line:1:unit_price", value="4.50", currency="CAD")))
            t.start()
            t.join(0.5)
            self.assertEqual(done, [])                      # blocked on the other process
            self.assertEqual(self.backend._wdesk()._rows(), [])
        finally:
            other.stdin.close()
            other.wait(10)
        t.join(30)
        self.assertEqual(done[0].json()["write"]["status"], "prepared", done[0].text)
        # a second process (its own store object, nothing shared in memory) sees the entry
        # on the desk and prepares nothing more
        self.backend.entry_holds = ecf.EntryHolds(folder)
        again = self.confirm(self.ana, eid, key="line:1:unit_price", value="4.50",
                             currency="CAD")
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(len(self.backend._wdesk()._rows()), 1)

    def test_a_value_the_reader_cannot_see_is_never_theirs_to_settle(self):
        eid = self.held(self.ana)["entry"]["entry_id"]
        # the cited customer's records are no longer Ana's to open
        self.backend.open_ids = self.backend.open_ids - set(self.party)
        view = self.ana.get(f"/api/entries/{eid}").json()
        customer = next(f for f in view["fields"] if f["key"] == "customer")
        self.assertEqual((customer["status"], customer.get("value")), ("check", None))
        for body in ({"key": "line:1:unit_price", "value": "4.50", "currency": "CAD"},
                     {"key": "customer", "record": self.party[0]}):
            out = self.confirm(self.ana, eid, **body)
            self.assertEqual(out.status_code, 409, out.text)
            self.assertEqual(out.json()["error"]["code"], "not_visible", out.text)
        self.assertEqual([r["event"] for r in self.backend._holds()._rows()], ["held"])
        self.assertEqual(self.backend._wdesk()._rows(), [])
        # asked again, it is not held for her: someone who can open the record prepares it
        part = self.held(self.ana)
        self.assertIsNone(part.get("entry"))
        self.assertIn(ec.UNSEEN, part["writes"][0]["problems"])

    def test_an_expired_hold_tells_its_preparer_to_ask_again_and_nobody_else_anything(self):
        import json
        from datetime import datetime, timedelta, timezone
        from operating import entry_confirm as ecf
        self.backend.write_grants = wi.WriteGrants({ANA[0]: ["bc:salesOrders"],
                                                    BEN[0]: ["bc:salesOrders"]})
        eid = self.held(self.ana)["entry"]["entry_id"]
        store = self.backend._holds().dir / "holds.jsonl"
        old = (datetime.now(timezone.utc) - timedelta(days=ecf.HOLD_DAYS + 1)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [dict(json.loads(x), at=old) for x in store.read_text("utf-8").splitlines()]
        store.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
        price = {"key": "line:1:unit_price", "value": "4.50", "currency": "CAD"}

        def answers():
            out = []
            for who in (self.ana, self.ben):
                out.append((who.get(f"/api/entries/{eid}").status_code,
                            self.confirm(who, eid, **price)))
            return [(v, c.status_code, c.json()["error"]["code"]) for v, c in out]
        self.assertEqual(answers()[0][1:], (409, "entry_expired"))
        # another hold is written: the expired one is cut to who, which kind and when,
        # and still answers its preparer "ask again"; anyone else still gets 404
        self.backend._holds().hold(ANA[1], "order", {"header": {"po": "other"}, "lines": []})
        cut = [json.loads(x) for x in store.read_text("utf-8").splitlines()]
        self.assertEqual([(r["event"], r["entry_id"] == eid) for r in cut],
                         [("expired", True), ("held", False)])
        self.assertNotIn("prefill", cut[0])
        (ana, ben) = answers()
        self.assertEqual(ana[1:], (409, "entry_expired"))
        self.assertEqual(ana[0], 409)
        self.assertEqual(ben, (404, 404, "not_found"))
        self.assertEqual(self.backend._wdesk()._rows(), [])
        # dropped after EXPIRED_DAYS: then no entry at all
        gone = (datetime.now(timezone.utc) - timedelta(days=ecf.EXPIRED_DAYS + 1)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.write_text("".join(json.dumps(dict(r, at=gone) if r["entry_id"] == eid else r)
                                 + "\n" for r in cut), "utf-8")
        self.backend._holds().hold(ANA[1], "order", {"header": {"po": "third"}, "lines": []})
        self.assertNotIn(eid, store.read_text("utf-8"))
        self.assertEqual(self.ana.get(f"/api/entries/{eid}").status_code, 404)

    def test_a_store_locked_too_long_answers_busy_not_a_hang(self):
        import subprocess
        import sys
        from unittest import mock
        from operating import entry_confirm as ecf
        eid = self.held(self.ana)["entry"]["entry_id"]
        folder = self.backend._holds().dir
        other = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; from pathlib import Path; from operating import entry_confirm as e\n"
             "h = e.EntryHolds(Path(sys.argv[1]))\n"
             "with h.lock:\n print('locked', flush=True); sys.stdin.read()\n",
             str(folder)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            cwd=str(Path(ecf.__file__).resolve().parents[1]))
        try:
            self.assertEqual(other.stdout.readline().strip(), "locked")
            with mock.patch.object(ecf, "LOCK_WAIT", 0.3):
                started = time.monotonic()
                view = self.ana.get(f"/api/entries/{eid}")
                out = self.confirm(self.ana, eid, key="line:1:unit_price", value="4.50",
                                   currency="CAD")
                again = self.held(self.ana)
            self.assertLess(time.monotonic() - started, 10)
            for r in (view, out):
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (503, "busy"),
                                 r.text)
            self.assertIsNone(again.get("entry"))
            self.assertIn(ecf.Busy().message, again["writes"][0]["problems"])
            self.assertEqual(self.backend._wdesk()._rows(), [])
        finally:
            other.stdin.close()
            other.wait(10)
        got = self.confirm(self.ana, eid, key="line:1:unit_price", value="4.50",
                           currency="CAD")
        self.assertEqual(got.json()["write"]["status"], "prepared", got.text)
