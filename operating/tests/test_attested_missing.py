"""Missing-in-record leads and the new document profiles at the ``plx.object`` boundary.

Crafted PDFs (reportlab) of a customer purchase order, a bill of lading, a delivery receipt
and a forwarder quote are captured as mail attachments over the injected Graph transport
of the capture tests, next to synthetic Portal sales orders, purchase orders and shipments
whose citations are mirror handles. The projection serves each document as its profile's
type, linked (lead mentions) to the record it names; ``plx.object`` on the record reports
a value the document states and the record holds empty as a ``missing_in_record`` lead
cited to the page (``operating/attested_conflicts.py``), a disagreement as the same
conflict as before, and nothing a reader may not see. No network, no credentials.
Evidence: injected-transport / isolated-fixture, never live.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.source_mirror import make_citation
from integrations.tests import test_capture_mail as tm
from ontology import document_fields, mentions, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import CapturedRecord, MailCaptures, build_store
from ontology.types import Actor
from operating import attested_conflicts
from operating import ontology_tools as ot
from operating.action_catalog import ActionCatalog

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2026-09-01T00:00:00.000Z"
READER = Actor("plx", frozenset({"staff:orders"}), "restricted")
NO_FINANCE = Actor("plx", frozenset({"staff:sales"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:outsider"}), "restricted")


def customer_po(po, order, required="2026-10-20"):
    return [["PURCHASE ORDER", f"PO Number: {po}", "Date: 2026-09-02",
             f"Our ref: Sales Order {order}", "Bill to: Northwind Retail Ltd",
             "Ship to: Northwind DC, Dock 4", f"Required date: {required}",
             "Incoterms: FCA Toronto", "Currency: USD",
             "Item  Description  Qty  UOM  Unit Price  Amount",
             "FG-1001  Hand soap 500 mL  2400  pcs  2.15  5,160.00",
             "FG-1002  Body wash 1 L  600?  pcs  3.40  2,040.00",
             "FG-1003  Hand cream    pcs  2.10  210.00", "",
             "Order total: USD 7,200.00"]]


BOL = [["BILL OF LADING", "B/L No: MAEU240915733", "Shipper: Ningbo Packaging Co",
        "Consignee: PLX Manufacturing", "Ref PO: PO100771", "Ship date: 2026-09-15",
        "Port of loading: Ningbo", "Packages: 240", "Gross weight: 1,812.5 kg"]]
POD = [["PROOF OF DELIVERY", "Shipment: SH1000771", "Delivered on: 2026-09-28",
        "Received by: J. Ortiz", "Condition: 2 cartons crushed"]]
QUOTES = [[["FREIGHT QUOTATION", "Quote No: FQ-3301", "Ref PO: PO100771",
            "Incoterm: FOB Ningbo", "All-in total: US$ 4,200.00"]],
          [["FREIGHT QUOTATION", "Quote No: FQ-3302", "Ref PO: PO100771",
            "Incoterm: EXW Ningbo", "All-in total: US$ 3,900.00"]]]


# Skip reason when pypdf/reportlab cannot import (BaseException: pyo3 PanicException).
_PDF_SKIP = ""
_missing = "pypdf"
try:
    import pypdf  # noqa: F401
    _missing = "reportlab"
    from reportlab.pdfgen import canvas  # noqa: F401
except BaseException as _error:  # noqa: BLE001 - a broken cryptography backend panics
    if isinstance(_error, (KeyboardInterrupt, SystemExit)):
        raise
    _PDF_SKIP = f"documents are PDFs: {_missing} import failed ({type(_error).__name__})"


class _NeedsPdf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _PDF_SKIP:
            raise unittest.SkipTest(_PDF_SKIP)


def pdf(pages):
    if _PDF_SKIP:
        raise unittest.SkipTest(_PDF_SKIP)
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    for lines in pages:
        y = 780
        for line in lines:
            page.drawString(60, y, line)
            y -= 18
        page.showPage()
    page.save()
    return buffer.getvalue()


def row(relation, key, cells):
    """A Portal row as the mirror captures it (its citation a mirror handle)."""
    return CapturedRecord("portal_api", relation, json.dumps({"id": key}), cells,
                          make_citation(relation, {"id": key}, 7, "0123456789abcdef"), "row",
                          "0" * 64, T0, None, T0, f"api:{relation}")


def order(key, number, **cells):
    return row("public.app_sales_order", key, {
        "id": key, "order_number": number, "status": "DRAFT", "required_date": "2026-10-20",
        "custom_fields": {"source": "gold-so-bridge"}, **cells})


class StaticPortal:
    source = "portal_api"

    def __init__(self, records):
        self._records = records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


# Orders 1..4 each named by one customer PO; order 5 by 10 and order 6 by 60 (cardinality).
ORDERS = {
    "so1": ("OR1000777001", {"po_reference": None}),            # holds no PO
    "so2": ("OR1000777002", {"po_reference": "OR1000777002"}),  # its own number: a placeholder
    "so3": ("OR1000777003", {"po_reference": "4500099999"}),    # another PO: a conflict
    "so4": ("OR1000777004", {"po_reference": "4500012345"}),    # the same PO: silent
    "so7": ("OR1000777007", {}),                                # the PO field is not served
    "so5": ("OR1000777005", {"po_reference": None}),
    "so6": ("OR1000777006", {"po_reference": None}),
}
BINDINGS = {"SalesOrder": "app_sales_order", "PurchaseOrder": "app_purchase_order",
            "Shipment": "app_shipment"}
NAMED = {"so1": 1, "so2": 1, "so3": 1, "so4": 1, "so7": 1, "so5": 10, "so6": 60}


class MissingInRecordTests(_NeedsPdf):
    @classmethod
    def setUpClass(cls):
        # A borrowing class may bring its own world (orders, documents naming each).
        orders, named = getattr(cls, "ORDERS", ORDERS), getattr(cls, "NAMED", NAMED)
        if _PDF_SKIP:  # borrowed by other classes' setUpClass, so no super()
            raise unittest.SkipTest(_PDF_SKIP)
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        state = Path(cls.tmp.name) / "state"
        graph = tm.FakeGraph()
        cls.bytes = {}
        n = 0
        for key, count in named.items():   # a distinct PO per document; so3's and so4's agree
            for _ in range(count):
                n += 1
                stated = "4500012345" if key in ("so3", "so4") else f"45000{n:05d}"
                cls.bytes[f"ATT-{n}"] = pdf(customer_po(stated, orders[key][0]))
        for name, pages in (("BOL", BOL), ("POD", POD), ("Q1", QUOTES[0]), ("Q2", QUOTES[1])):
            cls.bytes[f"ATT-{name}"] = pdf(pages)
        names = sorted(cls.bytes)
        for start in range(0, len(names), 20):
            graph.put(tm.BOX_A, f"MSG-{start}", f"CK-{start}", subject="Orders", body="See PDF.",
                      attachments=[tm.file_attachment(aid, cls.bytes[aid], f"{aid}.pdf")
                                   for aid in names[start:start + 20]])
        cls.mail = MailCaptureStore(state, clock=tm.Clock())
        api, _ = graph_read.build_graph(tm.ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(cls.mail, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
                          sleep=lambda s: None).run(tm.SCOPE)
        assert run["complete"], run
        cls.compiled = schema_loader.load_schema()
        portal = StaticPortal(
            [order(key, number, **cells) for key, (number, cells) in orders.items()] +
            [row("public.app_purchase_order", "p1", {
                "id": "p1", "po_number": "PO100771", "status": "SUBMITTED",
                "tracking_number": None, "incoterms": "FOB Ningbo", "shipping_cost": None,
                "custom_fields": {"source": "etl-silver-to-gold"}}),
             row("public.app_shipment", "s1", {
                 "id": "s1", "shipment_code": "SH1000771", "status": "SHIPPED",
                 "ship_date": "2026-09-20", "delivery_date": None, "tracking_number": None})])
        cls.path = Path(cls.tmp.name) / "ontology.sqlite"
        with MailCaptures(state, internal_domains=tm.SCOPE.internal_domains) as mail:
            build_store(cls.path, cls.compiled, {"portal_api": portal, "graph_mail": mail},
                        clock=lambda: NOW,
                        post_project={"mentions": mentions.store_mention_links,
                                      "document_fields": document_fields.store_document_links})

    @classmethod
    def tearDownClass(cls):
        cls.mail.close()
        cls.tmp.cleanup()

    def tools(self, compiled=None, path=None):
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:orders", "staff:sales", "staff:outsider"},
            "mail:*": {"staff:orders", "staff:sales", "staff:outsider"}},
            markings={m: {"staff:orders"} for m in store_ddl.MARKINGS})
        audience = LiveSourceAudience(
            {f"mail:{tm.UPN_A}": ["staff:orders", "staff:sales"]}, tenant="plx",
            classifications={"mail:*": "restricted"})
        reader = ot.StoreReader.open(path or self.path)
        self.addCleanup(reader.conn.close)
        return ot.OntologyTools(reader, policy, item_gate=self.mail.item_gate(audience),
                                schema=ot.SchemaView.from_compiled(compiled or self.compiled),
                                grammar=None, contracts=CONTRACTS, clock=lambda: NOW)

    def read(self, object_id, actor=READER, compiled=None, path=None):
        out = self.tools(compiled, path).call("plx.object", {"object_id": object_id}, actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.object"]["output"],
                                  root=CONTRACTS), [], json.dumps(out)[:1500])
        self.assertNotIn("error", out, json.dumps(out)[:800])
        return out

    def object_id(self, object_type, key):
        return f'{object_type}:portal.{BINDINGS[object_type]}:{{"id":"{key}"}}'

    def documents(self, object_type):
        conn = sqlite3.connect(self.path)
        try:
            return [r[0] for r in conn.execute(
                "SELECT object_id FROM objects WHERE object_type = ? ORDER BY object_id",
                (object_type,))]
        finally:
            conn.close()

    def test_a_po_the_order_holds_empty_is_a_lead_cited_to_the_page(self):
        out = self.read(self.object_id("SalesOrder", "so1"))
        self.assertEqual(out["conflicts"], [])
        (lead,) = out["leads"]
        self.assertEqual((lead["kind"], lead["tier"], lead["subject"]),
                         ("missing_in_record", "lead",
                          "SalesOrder.customer_po vs CustomerPoDocument.po_number"))
        stated, empty = lead["values"]
        self.assertEqual((stated["value"], stated["read_by"]), ("4500000001", "parser"))
        self.assertTrue(stated["citation"]["l1_handle"].startswith("graph-mail://"))
        self.assertEqual((empty["value"], empty["citation"]["field_path"]),
                         (None, "row.po_reference"))
        # custom_fields holds no custPoCode key: unread, listed, never taken as empty.
        self.assertEqual(lead["fields_not_read"], ["custom_fields.custPoCode"])
        (cited,) = lead["document_citations"]
        self.assertEqual(cited["page"], 1)
        # The page and span name the stated value in the original bytes (SHA-256 pinned).
        (content,) = [b for b in self.bytes.values()
                      if hashlib.sha256(b).hexdigest() == cited["source_sha256"]]
        self.assertEqual(document_fields.resolve_citation(
            content, {**cited, "text": "4500000001"}, mime_type="application/pdf"),
            "4500000001")
        # The plx.case_propose draft names the catalog's customer-PO proposal type.
        draft = lead["propose"]
        entry = ActionCatalog.builtin().get(draft["capability"])
        self.assertEqual(draft["capability"], "portal.sales_order.set_customer_po")
        self.assertEqual(set(draft["payload"]), {name for name, spec in
                                                 entry["parameters"].items() if spec["required"]})
        self.assertEqual(draft["payload"]["proposed_customer_po"], "4500000001")
        self.assertEqual(draft["payload"]["evidence_field"], "fields.po_number.value")
        self.assertEqual(draft["citations"], [cited["l1_handle"], empty["citation"]["l1_handle"]])
        self.assertEqual(draft["expected_effect"], {
            "relation": "public.app_sales_order", "key": {"id": "so1"},
            "values": {"po_reference": "4500000001"}})
        # A placeholder copy of the order's own number is empty too; the same lead is read
        # from the document's side.
        (placeholder,) = self.read(self.object_id("SalesOrder", "so2"))["leads"]
        self.assertEqual(placeholder["values"][0]["value"], "4500000002")
        (document,) = [d for d in self.documents("CustomerPoDocument")
                       if self.read(d).get("leads") == [lead]]
        # The line items are typed cells, each cited by its own span.
        values = {v["property"]: v for v in self.read(document)["values"]}
        evidence = json.loads(values["evidence"]["value"]["untrusted_text"])
        first, second = evidence["line_items"]
        self.assertEqual({k: (c["value"], c.get("unit") or c.get("currency"))
                          for k, c in first["cells"].items()},
                         {"item": ("FG-1001", None), "description": ("Hand soap 500 mL", None),
                          "quantity": ("2400", "ea"), "unit": ("pcs", None),
                          "unit_price": ("2.15", "USD"), "amount": ("5160.00", "USD")})
        self.assertEqual((first["cells"]["unit_price"]["per_unit"],
                          first["cells"]["unit_price"]["per_unit_from"]), ("ea", "quantity"))
        # A digit the writer left unclear is that cell's gap; the price per an unread
        # quantity's unit is unknown, never guessed.
        self.assertEqual([(c["value"], c.get("gap"), c["citation"]["text"]) for c in (
            second["cells"]["quantity"], second["cells"]["unit_price"])],
            [(None, "unreadable", "600?"), (None, "unit_unknown", "3.40")])
        # A row whose blank cell shifts the others is never fitted to the columns.
        (unaligned,) = [g for g in evidence["gaps"] if g.get("reason") == "unaligned"]
        self.assertEqual(unaligned["citations"][0]["text"],
                         "FG-1003  Hand cream    pcs  2.10  210.00")

    def test_a_disagreement_stays_a_conflict_and_unread_is_never_empty(self):
        conflict = self.read(self.object_id("SalesOrder", "so3"))
        self.assertNotIn("leads", conflict)
        (found,) = conflict["conflicts"]
        self.assertEqual((found["kind"], [v["value"]["untrusted_text"]
                                          if isinstance(v["value"], dict) else v["value"]
                                          for v in found["values"]]),
                         ("value_disagreement", ["4500012345", "4500099999"]))
        self.assertEqual(found["note"], attested_conflicts.NOTE)
        same = self.read(self.object_id("SalesOrder", "so4"))
        self.assertEqual((same["conflicts"], same.get("leads")), ([], None))
        # No column of the property was served: nothing is known, so nothing is said.
        unread = self.read(self.object_id("SalesOrder", "so7"))
        self.assertEqual((unread["conflicts"], unread.get("leads")), ([], None))

    def test_a_property_the_schema_does_not_declare_is_a_named_gap_never_a_skip(self):
        so1 = self.object_id("SalesOrder", "so1")
        # The document type lacks the compared field's property (profile and schema drift).
        drifted = copy.deepcopy(self.compiled)
        spec = drifted.object_types["CustomerPoDocument"]
        spec["properties"] = [p for p in spec["properties"] if p["name"] != "po_number"]
        out = self.read(so1, compiled=drifted)
        self.assertNotIn("leads", out)
        (gap,) = [g for g in out["gaps"] if g["reason"] == "property_not_declared"]
        self.assertEqual((gap["state"], gap["subject"]),
                         ("unknown", "CustomerPoDocument field po_number"))
        self.assertIn("not compared", gap["detail"])
        self.assertNotIn("count", gap)   # one document
        for key, count in (("so5", 10), ("so6", 60)):   # one gap, counted per document
            many = self.read(self.object_id("SalesOrder", key), compiled=drifted)
            self.assertEqual(([(g["subject"], g.get("count")) for g in many["gaps"]
                               if g["reason"] == "property_not_declared"], many.get("leads")),
                             ([("CustomerPoDocument field po_number", count)], None))
        self.assertNotIn("property_not_declared",
                         json.dumps(self.read(so1, OUTSIDER, compiled=drifted)))
        # A rule naming a record property the attested type does not declare.
        rules = [{**rule, "property": "customer_po_text"} if rule["field"] == "po_number"
                 else rule for rule in document_fields.comparisons()]
        with mock.patch.object(document_fields, "comparisons", lambda: rules):
            out = self.read(so1)
        self.assertNotIn("leads", out)
        self.assertEqual([g["subject"] for g in out["gaps"]
                          if g["reason"] == "property_not_declared"],
                         ["SalesOrder.customer_po_text vs CustomerPoDocument.po_number"])

    def test_agreement_gives_the_cause_it_decided_a_field_was_not_compared(self):
        def decided(key, actor=READER, compiled=None):
            tools = self.tools(compiled)
            scope = tools._scope(actor, tools._as_known_at({}))
            record = self.object_id("SalesOrder", key)
            reads = READER if actor is OUTSIDER else actor   # the documents, as a reader sees them
            reader_tools = self.tools(compiled)
            documents = attested_conflicts._ends(
                reader_tools, reader_tools._scope(reads, reader_tools._as_known_at({})),
                [record], "CustomerPoDocument.for_sales_order", "reverse")
            self.assertEqual(len(documents), 1, key)
            out = attested_conflicts.agreement_with_causes(tools, scope, documents[0], record)
            # agreement's own contract is the outcomes of that one decision.
            self.assertEqual(attested_conflicts.agreement(tools, scope, documents[0], record),
                             {field: outcome for field, (outcome, _) in out.items()})
            return out
        self.assertEqual(decided("so1"), {"po_number": ("not_comparable", "missing_in_record"),
                                          "required_date": ("agree", None)})
        self.assertEqual(decided("so3")["po_number"], ("conflict", None))
        self.assertEqual(decided("so4")["po_number"], ("agree", None))
        # No column of the property served: no readable record value, no lead.
        self.assertEqual(decided("so7")["po_number"], ("not_comparable", "record_value_absent"))
        drifted = copy.deepcopy(self.compiled)
        spec = drifted.object_types["CustomerPoDocument"]
        spec["properties"] = [p for p in spec["properties"] if p["name"] != "po_number"]
        self.assertEqual(decided("so1", compiled=drifted)["po_number"],
                         ("not_comparable", "property_not_declared"))
        # A reader the documents are withheld from is given no field and no cause.
        self.assertEqual(decided("so1", OUTSIDER), {})

    def test_a_mixed_reach_compares_the_declaring_types_and_names_the_others(self):
        so3 = self.object_id("SalesOrder", "so3")
        # so3's customer PO record (it declares po_number) and, by a link of the same type,
        # a shipment (it does not): the shipment is a gap, the PO record is still compared.
        path = Path(self.tmp.name) / "mixed.sqlite"
        conn = sqlite3.connect(path)
        with sqlite3.connect(self.path) as source:
            source.backup(conn)
        conn.row_factory = sqlite3.Row
        link = dict(conn.execute("SELECT * FROM links WHERE link_type = ? AND from_object = ?",
                                 ("SalesOrder.references_customer_po", so3)).fetchone())
        link.update(link_id=link["link_id"] + "-mixed",
                    to_object=self.object_id("Shipment", "s1"))
        conn.execute(f"INSERT INTO links ({', '.join(link)}) VALUES "
                     f"({', '.join('?' * len(link))})", list(link.values()))
        conn.commit()
        conn.close()
        rules = [{**rule, "property": "po_number", "path": [
            {"link_type": "SalesOrder.references_customer_po", "direction": "forward"}]}
            if rule["field"] == "po_number" else rule for rule in document_fields.comparisons()]
        with mock.patch.object(document_fields, "comparisons", lambda: rules):
            out = self.read(so3, path=path)
            hidden = self.read(so3, OUTSIDER, path=path)
        (found,) = [c for c in out["conflicts"] if "CustomerPoDocument" in c["subject"]]
        self.assertEqual((found["subject"], [v["value"]["untrusted_text"]
                                             if isinstance(v["value"], dict) else v["value"]
                                             for v in found["values"]]),
                         ("CustomerPurchaseOrder.po_number vs CustomerPoDocument.po_number",
                          ["4500012345", "4500099999"]))
        (gap,) = [g for g in out["gaps"] if g["reason"] == "property_not_declared"]
        self.assertEqual(gap["subject"], "Shipment.po_number vs CustomerPoDocument.po_number")
        self.assertIn("Shipment", gap["detail"])
        self.assertNotIn("CustomerPurchaseOrder", gap["detail"])
        # A reader the document is withheld from sees neither.
        self.assertNotIn("CustomerPoDocument", json.dumps(hidden))

    def test_a_reader_sees_no_lead_or_citation_it_may_not(self):
        so1 = self.object_id("SalesOrder", "so1")
        self.assertNotIn("leads", self.read(so1, OUTSIDER))   # the mail is not theirs
        (lead,) = self.read(so1, NO_FINANCE)["leads"]          # the extraction is marked
        self.assertNotIn("document_citations", lead)
        self.assertEqual(lead["values"][0]["value"], "4500000001")

    def test_shipping_documents_attest_the_records_they_name(self):
        po = self.read(self.object_id("PurchaseOrder", "p1"))
        leads = sorted(po["leads"], key=lambda lead: (lead["subject"],
                                                      lead["values"][0]["value"]))
        self.assertEqual([(lead["subject"], lead["values"][0]["value"]) for lead in leads], [
            ("PurchaseOrder.shipping_cost_estimate vs ForwarderQuote.amount", "3900.00"),
            ("PurchaseOrder.shipping_cost_estimate vs ForwarderQuote.amount", "4200.00"),
            ("PurchaseOrder.tracking_number vs FreightBill.tracking_number", "MAEU240915733")])
        self.assertEqual([lead["values"][0]["currency"] for lead in leads[:2]], ["USD", "USD"])
        self.assertNotIn("propose", leads[2])   # no proposal type is declared for it
        # The incoterm compares code with code: FOB matches 'FOB Ningbo', EXW conflicts.
        (incoterm,) = [c for c in po["conflicts"] if c["subject"].endswith("incoterm")]
        self.assertEqual(incoterm["values"][0]["value"], "EXW")
        # Money the reader may not read gives no lead, from either side.
        hidden = self.read(self.object_id("PurchaseOrder", "p1"), NO_FINANCE)
        self.assertEqual([lead["subject"] for lead in hidden["leads"]],
                         ["PurchaseOrder.tracking_number vs FreightBill.tracking_number"])
        (pod,) = self.read(self.object_id("Shipment", "s1"))["leads"]
        self.assertEqual((pod["subject"], pod["values"][0]["value"]),
                         ("Shipment.delivery_date vs DeliveryReceipt.delivery_date",
                          "2026-09-28"))

    def test_many_documents_on_one_order_lose_no_lead(self):
        by_order: dict[str, set[str]] = {}
        for document in self.documents("CustomerPoDocument"):
            for lead in self.read(document).get("leads", ()):
                handle = lead["values"][1]["citation"]["l1_handle"]
                by_order.setdefault(handle, set()).add(lead["values"][0]["value"])
        for key, count in (("so1", 1), ("so5", 10), ("so6", 60)):
            started = time.monotonic()
            out = self.read(self.object_id("SalesOrder", key))
            self.assertLess(time.monotonic() - started, 5.0, key)
            stated = [lead["values"][0]["value"] for lead in out["leads"]]
            self.assertEqual(len(set(stated)), len(stated), key)   # none duplicated
            truncated = [g for g in out["gaps"] if g["reason"] == "leads_truncated"]
            if len(stated) < count:   # cut to fit, and said so: never silently fewer
                self.assertTrue(truncated[0]["detail"].startswith(
                    f"{count} missing-in-record leads, the first {len(stated)} shown"), key)
            else:
                self.assertEqual((len(stated), truncated), (count, []), key)
            # Every one is still read from its own document's side.
            (handle,) = {lead["values"][1]["citation"]["l1_handle"] for lead in out["leads"]}
            self.assertEqual(len(by_order[handle]), count, key)
            self.assertLessEqual(set(stated), by_order[handle])

    def test_an_unknown_profile_key_is_refused(self):
        from operating import capability_guards
        # A declared proposal type must be one the case host offers (a typo would drop
        # every draft silently).
        declared = {rule["propose"] for rule in document_fields.comparisons()
                    if "propose" in rule}
        self.assertTrue(declared)
        self.assertTrue(all(capability_guards.available(name) for name in declared), declared)
        clean = json.loads(document_fields.PROFILES_PATH.read_text("utf-8"))
        data = json.loads(json.dumps(clean))
        receipt = next(p for p in data["profiles"] if p["id"] == "delivery_receipt")
        receipt["attests"]["compare"][0]["proprety"] = "delivery_date"
        issuer = json.loads(json.dumps(clean))
        # The own-record rule that decides direction: a misspelt key or an identifier
        # kind the grammar does not declare is refused, never read as "no own record".
        rule = next(p["not_issued_by_own_party"] for p in issuer["profiles"]
                    if "not_issued_by_own_party" in p)
        rule["own_record"]["identifier_kind"] = "petra_poo"
        synonyms = json.loads(json.dumps(clean))
        synonyms["code_synonyms"]["incoterm"]["CNX"] = "CFR"
        for broken, reason in ((data, "document_field_key_unknown"),
                               (issuer, "document_field_issuer_invalid"),
                               (synonyms, "document_field_code_synonyms_invalid")):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "document_fields.json"
                path.write_text(json.dumps(broken), "utf-8")
                document_fields.profiles.cache_clear()
                try:
                    with mock.patch.object(document_fields, "PROFILES_PATH", path), \
                            self.assertRaisesRegex(ValueError, reason):
                        document_fields.profiles()
                finally:
                    document_fields.profiles.cache_clear()


class AgreementCauseTests(_NeedsPdf):
    """``agreement_with_causes`` over a world of its own: orders with amounts (one
    currency, another, none) and a status naming two Incoterms, beside so1 (no PO) and
    so7 (PO field not served), one customer PO document naming each."""
    ORDERS = {key: ORDERS[key] for key in ("so1", "so7")} | {
        "so8": ("OR1000777008", {"total_amount": "7200.00", "subtotal": "7200.00",
                                 "currency": "USD", "status": "FOB CIF"}),
        "so9": ("OR1000777009", {"subtotal": "7200.00", "currency": "CAD"}),
        "so10": ("OR1000777010", {"subtotal": "7200.00"})}           # no currency
    NAMED = dict.fromkeys(ORDERS, 1)
    tools = MissingInRecordTests.tools
    object_id = MissingInRecordTests.object_id

    @classmethod
    def setUpClass(cls):
        MissingInRecordTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        MissingInRecordTests.tearDownClass.__func__(cls)

    def test_each_cause_a_field_was_not_compared_for_is_named_by_agreement(self):
        """Per rule, the cause ``agreement_with_causes`` gives a not_comparable field: each
        gap reason ``_compare`` reports and each way the records gave no value."""
        customer_po_rule = next(rule for rule in document_fields.comparisons()
                                if rule["document_type"] == "CustomerPoDocument")
        incoterms = ["EXW", "FCA", "FOB", "CIF"]

        def rule(**kw):
            return {**customer_po_rule, "keyed": False, "path": [], "tax_basis": "unknown",
                    "vocabulary": [], **kw}

        def cause(key, one, actor=READER, compiled=None):
            tools = self.tools(compiled)
            scope = tools._scope(actor, tools._as_known_at({}))
            record = self.object_id("SalesOrder", key)
            (document,) = attested_conflicts._ends(
                tools, scope, [record], "CustomerPoDocument.for_sales_order", "reverse")
            with mock.patch.object(document_fields, "comparisons", lambda: [one]):
                (decided,) = attested_conflicts.agreement_with_causes(
                    tools, scope, document, record).values()
            return decided

        # The document states USD 7,200.00 (tax basis unread), Incoterm FCA, no ship via.
        money = rule(field="amount", kind="money", property="subtotal", tax_basis="excl_tax")
        cases = [
            ("so8", money, ("agree", None)),   # the control: one currency, one tax basis
            ("so8", rule(field="amount", kind="money", property="total_amount"),
             "tax_basis_unknown"),
            ("so8", {**money, "tax_basis": "incl_tax"}, "tax_basis_differs"),
            ("so9", money, "currency_differs"),
            ("so10", money, "currency_unknown"),
            ("so1", rule(field="incoterm", kind="code", property="status",
                         vocabulary=incoterms), "unreadable"),          # DRAFT
            ("so8", rule(field="incoterm", kind="code", property="status",
                         vocabulary=incoterms), "several_values"),      # FOB CIF
            ("so1", rule(field="incoterm_place", kind="text", property="status"),
             "property_not_declared"),
            ("so1", rule(field="required_date", kind="date", event="SalesOrder.created"),
             "record_event_absent"),
            ("so1", rule(field="required_date", kind="date", property="ship_date",
                         path=[{"link_type": "Shipment.ships", "direction": "reverse"}]),
             "no_record_reached"),
            ("so1", rule(field="ship_via", kind="text", property="total_amount"),
             "document_blank"),
            ("so1", money, "record_value_absent"),
            ("so8", rule(field="ship_via", kind="text", property="status"),
             "document_value_unread"),
        ]
        for key, one, expected in cases:
            with self.subTest(key=key, field=one["field"], expected=expected):
                self.assertEqual(cause(key, one), expected if isinstance(expected, tuple)
                                 else ("not_comparable", expected))
        # The amount withheld from a reader without the finance marking: not stated.
        self.assertEqual(cause("so8", money, NO_FINANCE),
                         ("not_comparable", "document_not_stated"))
        # The property every reached record declares not served, or no source of theirs
        # binds: so7's order row holds no PO column.
        po_rule = next(rule for rule in document_fields.comparisons()
                       if rule["document_type"] == "CustomerPoDocument"
                       and rule["field"] == "po_number")
        for drift, expected in (("not_served", "record_property_not_served"),
                                ("unbound", "record_source_lacks_property")):
            drifted = copy.deepcopy(self.compiled)
            (spec,) = [p for p in drifted.object_types["SalesOrder"]["properties"]
                       if p["name"] == "customer_po"]
            if drift == "not_served":
                spec["availability"] = "not_served"
            else:
                spec["sources"] = [source for source in spec["sources"]
                                   if source.get("binding") != "portal.app_sales_order"]
            self.assertEqual(cause("so7", po_rule, compiled=drifted),
                             ("not_comparable", expected), drift)


class IssuedByOwnPartyTests(unittest.TestCase):
    def test_only_a_record_of_the_own_record_type_raises_the_doubt(self):
        """The document's number held by an object of another
        type than the class's own-record type is no own purchase order."""
        evidence = {"fields": {"po_number": {"value": "PO100236"}}}
        ((kind, value, record_type),) = document_fields.own_record_keys(
            "CustomerPoDocument", evidence)

        def reasons(holder_type):
            store = mock.Mock()
            store.identifiers_by_keys.return_value = [
                {"object_id": "other", "object_type": holder_type, "identifier_kind": kind,
                 "normalized": value}]
            return [g["reason"] for g in attested_conflicts._issued_by_own_party(
                mock.Mock(store=store), None, "document", "CustomerPoDocument", evidence)]

        self.assertEqual(reasons(record_type), [attested_conflicts.ISSUED_BY_OWN_PARTY])
        self.assertEqual(reasons("SalesOrder"), [])


class CustomerPoLiveCheckTests(_NeedsPdf):
    """``verify/live-checks/ontology.document_fields_customer_po.json`` over the in-process
    FakeDrive: the spec's expectations hold on the check's own output."""

    def test_the_customer_po_check_meets_its_spec_leaking_nothing(self):
        import argparse
        from verify import live_verify as lv
        from integrations import capture_sharepoint as cs
        from integrations.tests import test_capture_sharepoint as tsp
        from operating import sharepoint_host_check as check
        from scripts import eval_document_fields_live as live
        # The Portal stated its columns on the PO (QA-06); the other files' are empty.
        drive = tsp.seeded_drive(tsp.ColumnsDrive({"01CPO": {
            "PartyType": "Customer", "ItemRef": "OR1000777001", "DocStatus": "Approved"}}))
        drive.file("01CPO", "PO 4500000001.pdf", "01AOS",
                   pdf(customer_po("4500000001", "OR1000777001")))
        with tempfile.TemporaryDirectory() as tmp:
            scope_file, audience_file = Path(tmp) / "scope.json", Path(tmp) / "audience.json"
            scope_file.write_text(json.dumps({"schema": cs.SCOPE_SCHEMA, "targets": [
                {"id": "customers", "drive_id": tsp.DRIVE, "folders": [tsp.QUALITY_FOLDER]}],
                "list_item_columns": "portal_taxonomy"}))
            audience_file.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {"sharepoint:*": [cs.CAPTURE_PRINCIPAL]}}))
            report = check.run(argparse.Namespace(
                state_dir=str(Path(tmp) / "state"), scope=str(scope_file),
                live_audience=str(audience_file), principal=cs.CAPTURE_PRINCIPAL,
                document_class="customer_po", min_documents=1),
                environ=tsp.ENV, sender=drive, sleep=lambda s: None, evaluate=live.evaluate)
        self.assertEqual((report["ok"], report["evidence_label"], report["documents"]),
                         (True, "injected-transport", 1), report)
        self.assertEqual(report["citations_resolved"], report["citations"])
        self.assertEqual(report["field_outcomes"]["po_number"]["extracted"], 1)
        self.assertEqual({k: report["portal_metadata"][k] for k in (
            "documents_read", "documents_with_portal_metadata", "served_to_reader",
            "visible_to_withheld", "columns_stated")},
            {"documents_read": 4, "documents_with_portal_metadata": 1, "served_to_reader": 1,
             "visible_to_withheld": 0,
             "columns_stated": {"DocStatus": 1, "ItemRef": 1, "PartyType": 1}})
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        for secret in ("4500000001", "OR1000777001", "Northwind", tsp.DRIVE):
            self.assertNotIn(secret, json.dumps(report))
        spec = json.loads((ROOT / "verify/live-checks/ontology.document_fields_customer_po.json")
                          .read_text("utf-8"))
        lv.validate_spec(spec, stem="ontology.document_fields_customer_po")
        self.assertEqual(lv.check_expectations(spec, 0, report), [])


class TitleCueTests(_NeedsPdf):
    """A new class's title in another class's document (a table header cell, a bare line
    after the document's own title) never turns that document ambiguous."""

    def test_an_older_class_naming_a_po_or_an_invoice_keeps_its_class_and_fields(self):
        cases = {
            "supplier_confirmation": [["PROFORMA INVOICE", "PI No: PI-70001",
                                       "Purchase Order  Qty  Unit Price",
                                       "PO100234  400,000 pcs  USD 0.04125", "Purchase Order",
                                       "Quantity: 400,000 pcs", "Total Amount: USD 16,500.00"]],
            "packing_slip": [["PACKING LIST", "Packing List No: PL-6001",
                              "Invoice  INV-2024-77", "Invoice", "Quantity: 12000 pcs"]],
            "forwarder_quote": [["FREIGHT QUOTATION", "Quote No: FQ-3303", "Invoice",
                                 "All-in total: US$ 4,200.00"]],
        }
        for profile, pages in cases.items():
            found = document_fields.extract_fields(pdf(pages), "application/pdf",
                                                   filename="doc.pdf")
            self.assertEqual((found["status"], found["profile"]), ("classified", profile),
                             found["class_evidence"])
            stated = [f for f in found["fields"].values() if f.get("value") is not None]
            self.assertGreaterEqual(len(stated), 2, profile)
        # Its own title alone on the first line still makes a customer PO.
        found = document_fields.extract_fields(pdf(customer_po("4500000001", "OR1000777001")),
                                               "application/pdf", filename="po.pdf")
        self.assertEqual(found["profile"], "customer_po")


if __name__ == "__main__":
    unittest.main()
