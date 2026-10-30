"""Lot traceability from served rows: a supplier lot, from its certificate to the receipt.

* FM's raw and packaging receipt rows state the supplier's lot as ``lotnoqc`` and the gold
  receipt line as ``supplier_lot_number`` (the Portal seed copies one into the other): each
  projects ``ReceiptLine.supplier_lot_number`` as a ``lot_number`` reference in the
  identifier index, so a lot number finds the receipt lines that received it, every row
  once at any cardinality. A blank lot is no reference, never an empty one.
* The lot names a lot, never its holder: ``plx.find`` on the lot finds no ReceiptLine of
  that identity (``referenced_only``); a filter on the property lists the lines to an
  operations reader (the lot carries no marking).
* A supplier certificate of analysis states the same supplier lot: its ``lot`` field links
  the receipt lines whose supplier lot it is (``CertificateOfAnalysis.for_receipt_line``,
  a lead), and the trace runs on through the served links to the Supplier, the
  PurchaseOrder and the Product. A supplier lot is never a PLX lab id or batch code: a
  lot written in a lab id's shape links no line by its lab id. Lot itself stays unserved.
* A hop the trace cannot reach is typed by its source cause, never lowered: an FM line
  whose ``idreceipt`` (or receipt whose ``idsupp``) is null reaches no Supplier for
  ``source_column_empty``, an FM line no Product for ``no_binding``; the PO's vendor reaches
  the Supplier the receipt does not name. A cause not read from the served gap and row
  is ``unknown``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from integrations.tests import test_capture_sharepoint as tsp
from ontology import document_fields, identifiers, schema
from ontology.core import Actor
from ontology.project import SharePointCaptures, build_store
from ontology.tests.test_schema_coverage import Source
from operating import ontology_tools as ot
from operating import ontology_tools_traverse as tt

RM, PK, GOLD = ("public.receipt_raw_material", "public.receipt_packaging",
                "public.app_receipt_line")


def raw(index, lot):
    return {"id": index, "idreceipt": 5, "idpogenitem": 7, "idlabid": f"26161-{index}",
            "sumqty": "10", "lotnoqc": lot, "expirydate": "2028-08-31"}


def build(rows):
    temp = tempfile.TemporaryDirectory()
    path = Path(temp.name) / "store.sqlite"
    build_store(path, schema.load_schema(), {"portal_api": Source(rows)},
                normalizer=identifiers.normalize,
                clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return temp, conn


def lot_refs(conn):
    return sorted(conn.execute(
        "SELECT object_id, identifier_kind, normalized FROM identifiers "
        "WHERE property = 'supplier_lot_number' AND superseded_at IS NULL"))


class SupplierLotOfAReceiptLine(unittest.TestCase):
    def test_the_supplier_lot_is_a_lot_reference_from_fm_and_the_gold_line(self):
        (prop,) = [p for p in schema.load_schema().object_types["ReceiptLine"]["properties"]
                   if p["name"] == "supplier_lot_number"]
        self.assertEqual((prop["value_type"], prop["identifier_kind"], prop["identifies"]),
                         ("identifier", "lot_number", "reference"))
        self.assertEqual({(s["binding"], s["path"]) for s in prop["sources"]}, {
            ("portal.fm_receipt_raw_material", "lotnoqc"),
            ("portal.fm_receipt_packaging", "lotnoqc"),
            ("portal.app_receipt_line", "supplier_lot_number")})
        temp, conn = build({
            RM: [raw(11, " SUP-LOT-A1 "), raw(12, ""), raw(13, "   "), raw(14, None)],
            PK: [{"id": 21, "idreceipt": 6, "idpogenitem": 8, "idlab": "RP0000001",
                  "sumqty": "5", "lotnoqc": "PK-9"}],
            GOLD: [{"id": "rl-1", "receipt_id": "r-1", "product_id": "p-1", "quantity": "4",
                    "uom": "kg", "lab_id": "26161-9", "supplier_lot_number": "GL-1"}]})
        self.addCleanup(temp.cleanup)
        self.addCleanup(conn.close)
        self.assertEqual(lot_refs(conn), [
            ('ReceiptLine:portal.app_receipt_line:{"id":"rl-1"}', "lot_number", "GL-1"),
            ('ReceiptLine:portal.fm_receipt_packaging:{"id":21}', "lot_number", "PK-9"),
            ('ReceiptLine:portal.fm_receipt_raw_material:{"id":11}', "lot_number",
             "SUP-LOT-A1")])
        # A blank lot is no value, never an empty reference.
        blanks = dict(conn.execute(
            "SELECT object_id, value FROM object_values WHERE property = "
            "'supplier_lot_number' AND object_id LIKE '%fm_receipt_raw_material%'"))
        self.assertEqual({oid[-3:-1]: value for oid, value in blanks.items()},
                         {"11": "SUP-LOT-A1", "12": None, "13": None, "14": None})

    def test_every_row_is_one_reference_at_any_cardinality(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                temp, conn = build({RM: [raw(i, f"SUP-{i:03d}") for i in range(1, n + 1)]})
                refs = lot_refs(conn)
                conn.close()
                temp.cleanup()
                self.assertEqual(len(refs), n)
                self.assertEqual(len({oid for oid, _, _ in refs}), n)
                self.assertEqual(sorted(v for _, _, v in refs),
                                 [f"SUP-{i:03d}" for i in range(1, n + 1)])

    def test_an_operations_reader_lists_the_lines_of_a_lot_never_as_their_identity(self):
        temp, conn = build({GOLD: [{
            "id": "rl-1", "receipt_id": "r-1", "product_id": "p-1", "quantity": "4",
            "uom": "kg", "lab_id": "26161-9", "supplier_lot_number": "GL-7"}]})
        self.addCleanup(temp.cleanup)
        self.addCleanup(conn.close)
        tools = ot.OntologyTools(
            ot.StoreReader(conn), ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}},
                                               markings={"finance": frozenset({"fin"})}),
            schema=ot.SchemaView.from_compiled(schema.load_schema()), grammar=identifiers,
            item_gate=lambda actor, handle: True)
        ops = Actor("plx", frozenset({"ops"}))
        # The lot names a lot, never the line holding it: a query for it finds no
        # ReceiptLine of that identity and says which lines reference it.
        named = tools.call("plx.find", {"query": "GL-7", "object_type": "ReceiptLine"}, ops)
        self.assertEqual((named["status"], named["candidates"]), ("not_found", []))
        self.assertIn("referenced_only", {g["reason"] for g in named["gaps"]})
        found = tools.call("plx.find", {"object_type": "ReceiptLine", "filters": [
            {"property": "supplier_lot_number", "op": "eq", "value": "GL-7"}]}, ops)
        self.assertEqual(found["status"], "found", found)
        (candidate,) = found["candidates"]
        oid = candidate["object"]["object_id"]
        self.assertEqual(oid, 'ReceiptLine:portal.app_receipt_line:{"id":"rl-1"}')
        out = tools.call("plx.object", {"object_id": oid}, ops)
        read = {v["property"]: v for v in out["values"]}
        self.assertEqual(read["supplier_lot_number"]["value"], "GL-7")


    def test_a_placeholder_lot_is_no_lot_reference(self):
        lots = ["N/A", "n/a", "NA", "None", "-", "", "  ", "REAL-1", "NANO-1", "NONE-7"]
        temp, conn = build({RM: [raw(i, lot) for i, lot in enumerate(lots, 1)]})
        self.addCleanup(temp.cleanup)
        self.addCleanup(conn.close)
        # Only a lot that is not a stand-in for "no lot" is indexed (NANO-1 and NONE-7
        # merely start like one).
        self.assertEqual(sorted(normalized for _, _, normalized in lot_refs(conn)),
                         ["NANO-1", "NONE-7", "REAL-1"])

    def test_a_placeholder_lot_is_no_value_not_an_unparsed_one(self):
        temp, conn = build({RM: [raw(1, "N/A"), raw(2, "REAL-1"), raw(3, "none")]})
        self.addCleanup(temp.cleanup)
        self.addCleanup(conn.close)
        report = json.loads(dict(conn.execute("SELECT key, value FROM store_meta"))
                            .get("build_report", "{}"))
        self.assertEqual([g for g in report.get("gaps", ())
                          if str(g.get("code", "")).startswith("parse_")], [])
        tools = ot.OntologyTools(
            ot.StoreReader(conn), ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}},
                                               markings={}),
            schema=ot.SchemaView.from_compiled(schema.load_schema()), grammar=identifiers,
            item_gate=lambda actor, handle: True)
        ops = Actor("plx", frozenset({"ops"}))
        # The source says "no lot": a find never claims a lot it could not read.
        for args in ({"query": "ZZZ-404"}, {"query": "ZZZ-404", "object_type": "ReceiptLine"}):
            with self.subTest(args=args):
                found = tools.call("plx.find", args, ops)
                self.assertNotIn("identifier_unparsed", {g["reason"] for g in found["gaps"]})
        out = tools.call("plx.object", {
            "object_id": 'ReceiptLine:portal.fm_receipt_raw_material:{"id":1}'}, ops)
        self.assertNotIn("value_unparsed", {g.get("reason") for g in out.get("gaps", ())})
        absent = tools.call("plx.find", {"object_type": "ReceiptLine", "filters": [
            {"property": "supplier_lot_number", "op": "absent"}]}, ops)
        self.assertEqual(len(absent["candidates"]), 2, absent)

    def test_a_lower_case_lot_query_finds_the_upper_cased_record(self):
        temp, conn = build({GOLD: [{
            "id": "rl-1", "receipt_id": "r-1", "product_id": "p-1", "quantity": "4",
            "uom": "kg", "lab_id": "26161-9", "supplier_lot_number": "gl-7"}]})
        self.addCleanup(temp.cleanup)
        self.addCleanup(conn.close)
        tools = ot.OntologyTools(
            ot.StoreReader(conn), ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}},
                                               markings={}),
            schema=ot.SchemaView.from_compiled(schema.load_schema()), grammar=identifiers,
            item_gate=lambda actor, handle: True)
        ops = Actor("plx", frozenset({"ops"}))
        for written in ("gl-7", " Gl-7 ", "GL-7"):
            with self.subTest(written=written):
                found = tools.call("plx.find", {"object_type": "ReceiptLine", "filters": [
                    {"property": "supplier_lot_number", "op": "eq", "value": written}]}, ops)
                self.assertEqual(found["status"], "found", found)
                (candidate,) = found["candidates"]
                self.assertEqual(candidate["object"]["object_id"],
                                 'ReceiptLine:portal.app_receipt_line:{"id":"rl-1"}')
        other = tools.call("plx.find", {"object_type": "ReceiptLine", "filters": [
            {"property": "supplier_lot_number", "op": "eq", "value": "gl-8"}]}, ops)
        self.assertEqual(other["status"], "not_found")


class PropertyReferenceFailsClosed(unittest.TestCase):
    def test_a_malformed_property_reference_is_refused_at_load(self):
        from unittest import mock
        data = json.loads(document_fields.PROFILES_PATH.read_text(encoding="utf-8"))
        (coa,) = [p for p in data["profiles"] if p["object_type"] == "CertificateOfAnalysis"]
        (lot,) = [f for f in coa["fields"] if f["name"] == "lot"]
        good = dict(lot["refers_to_property"])
        bad = [{**good, "identifier_kind": "no_such_kind"},
               {k: v for k, v in good.items() if k != "property"},
               {**good, "extra": 1}, "ReceiptLine.supplier_lot_number"]
        for target in bad + ["also_refers_to", "also_identifier_kind"]:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                lot.pop("refers_to", None)
                lot.pop("identifier_kind", None)
                lot["refers_to_property"] = good
                if target == "also_refers_to":
                    lot["refers_to"] = "lot_number"
                elif target == "also_identifier_kind":
                    lot["identifier_kind"] = "lot_number"
                else:
                    lot["refers_to_property"] = target
                path = Path(tmp) / "profiles.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                document_fields.profiles.cache_clear()
                try:
                    with mock.patch.object(document_fields, "PROFILES_PATH", path), \
                            self.assertRaisesRegex(ValueError,
                                                   "refers_to_property_invalid:lot"):
                        document_fields.profiles()
                finally:
                    document_fields.profiles.cache_clear()


def coa_pdf(lot, po="PO100236"):
    from reportlab.pdfgen import canvas
    import io

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    for y, line in zip(range(780, 0, -18), [
            "CERTIFICATE OF ANALYSIS", "Supplier: Ningbo Chemical Co", f"Customer PO: {po}",
            "Product: Glycerin USP", f"Lot No: {lot}", "Expiry date: 2028-08-31"]):
        page.drawString(60, y, line)
    page.showPage()
    page.save()
    return buffer.getvalue()


GOLD_LINE = 'ReceiptLine:portal.app_receipt_line:{"id":"%s"}'
FM_LINE = 'ReceiptLine:portal.fm_receipt_raw_material:{"id":%d}'
QUALITY = Actor("plx", frozenset({"staff:quality"}), "restricted")
RECORDS_ONLY = Actor("plx", frozenset({"staff:records"}), "restricted")


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "certificates are read from PDFs: needs pypdf and reportlab")
class CertificateToReceiptTrace(unittest.TestCase):
    """Three certificates captured from SharePoint beside Portal gold and FM receipts: one
    states the gold line's supplier lot, one an FM row's, one an RP-shaped lot equal to
    another line's PLX lab id."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.case = tsp.CaptureCase("run")
        cls.case.setUp()
        for item, lot in (("01COAG", "GL240917"), ("01COAF", "sup-lot-a1"),
                          ("01COAR", "RP0000002")):
            cls.case.drive.file(item, f"{item}.pdf", "01AOS", coa_pdf(lot))
        run = cls.case.capture().run(tsp.scope())
        assert run["complete"], run
        rows = {
            GOLD: [{"id": "rl-1", "receipt_id": "r-1", "product_id": "p-1", "quantity": "4",
                    "uom": "kg", "lab_id": "26161-9", "supplier_lot_number": "GL240917"},
                   {"id": "rl-2", "receipt_id": "r-2", "product_id": "p-1", "quantity": "2",
                    "uom": "kg", "lab_id": "RP0000002", "supplier_lot_number": "OTHER-LOT"}],
            "public.app_receipt": [
                {"id": "r-1", "receipt_number": "26161-9", "supplier_id": "s-1",
                 "purchase_order_id": "po-1"},
                {"id": "r-2", "receipt_number": "RP0000002", "supplier_id": "s-1",
                 "purchase_order_id": "po-1"}],
            "public.app_supplier": [{"id": "s-1", "code": "NINGBO"}],
            "public.app_product": [{"id": "p-1", "code": "GLY-USP"}],
            "public.app_purchase_order": [{"id": "po-1", "po_number": "PO100236",
                                           "status": "SUBMITTED"}],
            RM: [raw(11, "SUP-LOT-A1")],
            "public.receipt": [{"id": 5, "idsupp": 40}],
            "public.supplier": [{"id": 40, "namesupp": "Ningbo"}],
            "public.generated_purchase_order_item": [{"id": 7, "idpogen": 900}],
            "public.generated_purchase_order": [{"id": 900, "ponumber": "PO100237"}]}
        cls.compiled = schema.load_schema()
        cls.path = Path(cls.case.tmp.name) / "ontology.sqlite"
        with SharePointCaptures(cls.case.state) as files:
            cls.report = build_store(
                cls.path, cls.compiled, {"portal_api": Source(rows), "sharepoint": files},
                normalizer=identifiers.normalize,
                clock=lambda: datetime(2026, 10, 4, tzinfo=timezone.utc),
                post_project={"document_fields": document_fields.store_document_links})
        cls.conn = sqlite3.connect(cls.path)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.case.tearDown()

    def certificate(self, item):
        (oid,) = [r[0] for r in self.conn.execute(
            "SELECT object_id FROM objects WHERE object_type = 'CertificateOfAnalysis' AND "
            "object_id LIKE ?", (f"%{item}%",))]
        return oid

    def traverse(self):
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:quality", "staff:records"},
            "sharepoint:*": {"staff:quality", "staff:records"}}, markings={})
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        gate = self.case.store.item_gate(tsp.reader_audience())
        return tt.TraverseTools(ot.OntologyTools(
            reader, policy, item_gate=gate, grammar=identifiers,
            schema=ot.SchemaView.from_compiled(self.compiled)))

    def around(self, tools, start, *links, actor=QUALITY):
        out = tools.call("plx.around", {"from": start, "path": [{"link": n} for n in links],
                                        "tier_floor": "lead"}, actor)
        self.assertNotIn("error", out, json.dumps(out)[:1500])
        return {o["object_id"] for o in out["objects"]}

    def test_a_certificate_traces_its_supplier_lot_to_supplier_po_and_product(self):
        tools = self.traverse()
        gold, fm = self.certificate("01COAG"), self.certificate("01COAF")
        self.assertEqual(self.around(tools, gold, "for_receipt_line"), {GOLD_LINE % "rl-1"})
        self.assertEqual(self.around(tools, gold, "for_receipt_line", "line_of", "supplied_by"),
                         {'Supplier:portal.app_supplier:{"id":"s-1"}'})
        self.assertEqual(self.around(tools, gold, "for_receipt_line", "line_of", "receives"),
                         {'PurchaseOrder:portal.app_purchase_order:{"id":"po-1"}'})
        self.assertEqual(self.around(tools, gold, "for_receipt_line", "for_item"),
                         {'Product:portal.app_product:{"id":"p-1"}'})
        # The FM row's lotnoqc, matched case-blind as the lot normaliser reads it, reaches
        # the FM supplier and the FM PO through the PO line it received.
        self.assertEqual(self.around(tools, fm, "for_receipt_line"), {FM_LINE % 11})
        self.assertEqual(self.around(tools, fm, "for_receipt_line", "line_of", "supplied_by"),
                         {'Supplier:portal.fm_supplier:{"id":40}'})
        self.assertEqual(self.around(tools, fm, "for_receipt_line", "receives_line", "line_of"),
                         {'PurchaseOrder:portal.fm_generated_purchase_order:{"id":900}'})
        links = self.conn.execute(
            "SELECT tier, binding_kind, why_not FROM links WHERE link_type = "
            "'CertificateOfAnalysis.for_receipt_line'").fetchall()
        self.assertEqual(len(links), 2)
        for row in links:
            self.assertEqual((row["tier"], row["binding_kind"]), ("lead", "mention"))
            self.assertIn("external_assertion", row["why_not"])

    def test_a_supplier_lot_never_matches_a_plx_lab_id_or_batch_code(self):
        shaped = self.certificate("01COAR")
        links = self.conn.execute("SELECT link_type, to_object FROM links WHERE from_object = ?"
                                  " AND link_type <> 'CertificateOfAnalysis.for_purchase_order'",
                                  (shaped,)).fetchall()
        self.assertEqual([tuple(r) for r in links], [])
        evidence = json.loads(self.conn.execute(
            "SELECT value FROM object_values WHERE object_id = ? AND property = 'evidence'",
            (shaped,)).fetchone()[0])
        # The RP-shaped lot is one lot reference, never also a receipt lab id.
        kinds = {(r["identifier_kind"], r["normalized"]) for r in evidence["references"]}
        self.assertIn(("lot_number", "RP0000002"), kinds)
        self.assertEqual({k for k, v in kinds if v == "RP0000002"}, {"lot_number"})
        self.assertFalse({k for k, _ in kinds} & {"receipt_lab_id", "batch_code"}, kinds)
        # The lot kind's alias kinds (receipt_lab_id, batch_code) would have reached rl-2
        # by its lab id; the field resolves on ReceiptLine.supplier_lot_number alone.
        self.assertIn("receipt_lab_id", identifiers.alias_kinds("lot_number"))

    def test_the_property_filter_reports_the_candidates_it_drops(self):
        counts = self.report["post_project"]["document_fields"]["counts"]
        # The RP-shaped lot matches the line rl-2 and its receipt r-2 by their lab id (an
        # alias kind): the filter drops both. The other two certificates' supplier lots
        # also find the Lot holding that number (another holder of the lot_number kind):
        # the field names ReceiptLine.supplier_lot_number, so each drops that one Lot.
        self.assertEqual(counts["dropped_count"], 4, counts)
        self.assertEqual(counts["links"], self.conn.execute(
            "SELECT COUNT(*) FROM links WHERE link_type LIKE 'CertificateOfAnalysis.%'"
        ).fetchone()[0])

    def test_a_reader_without_the_certificate_sees_no_trace_through_any_consumer(self):
        tools = self.traverse()
        gold = self.certificate("01COAG")
        line = GOLD_LINE % "rl-1"
        self.assertEqual(self.around(tools, line, "certificates_of_analysis"), {gold})
        # staff:records holds the Portal rows but no SharePoint identity: the line is
        # read, its certificate and the link to it are not.
        self.assertEqual(self.around(tools, line, "certificates_of_analysis",
                                     actor=RECORDS_ONLY), set())
        shown = tools.base.call("plx.object", {"object_id": gold}, RECORDS_ONLY)
        self.assertEqual(shown["error"]["code"], "object_not_found")
        listed = tools.base.call("plx.object", {"object_id": line}, RECORDS_ONLY)
        self.assertNotIn("01COAG", json.dumps(listed))

    def test_the_lot_field_names_a_lot_reference_property_its_type_links(self):
        """Every refers_to_property names a declared identifier property of that kind on a
        type the profile's type declares a mention link to (else it would link nothing)."""
        for profile in document_fields.profiles()["profiles"]:
            for spec in profile["fields"]:
                target = spec.get("refers_to_property")
                if not target:
                    continue
                props = {p["name"]: p for p in self.compiled.object_types[
                    target["object_type"]]["properties"]}
                prop = props[target["property"]]
                self.assertEqual((prop["value_type"], prop["identifier_kind"]),
                                 ("identifier", target["identifier_kind"]))
                self.assertTrue(any(
                    link["from"] == profile["object_type"] and
                    link["to"] == target["object_type"] and
                    any(b["kind"] == "mention" for b in link["bindings"])
                    for link in self.compiled.link_types.values()), spec["name"])


LOT_TRACE_SPEC = Path(__file__).resolve().parents[2] / "devloop" / "live-checks" / \
    "ontology.lot_trace.json"


def trace_portal(rows):
    """A fixture Portal serving ``rows`` (relation -> rows), every trace relation present."""
    from evals.oracle.fixture_world import World
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from scripts.eval_dev_baseline import FIXTURE_KEY, FIXTURE_URL
    from scripts.eval_lot_trace import TRACE_RELATIONS
    from scripts.eval_unfamiliar_sample import _fixture_fetch
    return WorkbenchReadApiClient(FIXTURE_URL, FIXTURE_KEY, fetch=_fixture_fetch(World(
        {**{relation: [] for relation in TRACE_RELATIONS}, **rows})))


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "certificates are read from PDFs: needs pypdf and reportlab")
class LotTraceLiveCheckTests(unittest.TestCase):
    """``ontology.lot_trace`` over the in-process FakeDrive and a fixture Portal
    (injected-transport, never live)."""

    def run_trace(self, lots, rows, portal=True, extra=()):
        import contextlib
        import io
        from integrations import capture_sharepoint as cs
        from scripts import eval_document_fields_live as live
        drive = tsp.seeded_drive()
        for n, lot in enumerate(lots):
            drive.file(f"01COA{n}", f"coa {n}.pdf", "01AOS", coa_pdf(lot))
        with tempfile.TemporaryDirectory() as tmp:
            scope, audience = Path(tmp) / "scope.json", Path(tmp) / "audience.json"
            scope.write_text(json.dumps({"schema": cs.SCOPE_SCHEMA, "targets": [
                {"id": "quality", "drive_id": tsp.DRIVE, "folders": [tsp.QUALITY_FOLDER]}]}))
            audience.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {"sharepoint:*": [cs.CAPTURE_PRINCIPAL]}}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = live.main(["--lot-trace", "--state-dir", str(Path(tmp) / "state"),
                                  "--scope", str(scope), "--live-audience", str(audience),
                                  "--min-documents", "1", *extra],
                                 environ=dict(tsp.ENV), sender=drive,
                                 portal_client=trace_portal(rows) if portal else None)
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        return json.loads(out.getvalue()), code, out.getvalue()

    def rows(self, n):
        """``n`` gold receipt lines (lots GL-0001..) on one receipt, PO, supplier, product,
        and one FM raw-material receipt row (lot SUP-LOT-A1) on the FM PO chain."""
        return {
            GOLD: [{"id": f"rl-{i}", "receipt_id": "r-1", "product_id": "p-1",
                    "quantity": "4", "uom": "kg", "lab_id": f"RP{i:07d}",
                    "supplier_lot_number": f"GL-{i:04d}"} for i in range(1, n + 1)],
            "public.app_receipt": [{"id": "r-1", "receipt_number": "RP0000000",
                                    "supplier_id": "s-1", "purchase_order_id": "po-1"}],
            "public.app_supplier": [{"id": "s-1", "code": "NINGBO"}],
            "public.app_product": [{"id": "p-1", "code": "GLY-USP"}],
            "public.app_purchase_order": [{"id": "po-1", "po_number": "PO100236",
                                           "status": "SUBMITTED"}],
            "public.receipt_raw_material": [raw(11, "SUP-LOT-A1")],
            "public.receipt": [{"id": 5, "idsupp": 40}],
            "public.supplier": [{"id": 40, "namesupp": "Ningbo"}],
            "public.generated_purchase_order_item": [{"id": 7, "idpogen": 900}],
            "public.generated_purchase_order": [{"id": 900, "ponumber": "PO100237"}]}

    def test_the_spec_passes_on_certificates_tracing_to_their_receipt(self):
        from verify import live_verify as lv
        spec = json.loads(LOT_TRACE_SPEC.read_text("utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem=spec["id"]), [])
        # GL-0001 and the FM lot trace; RP0000002 is a PLX lab id in a lot's place.
        report, code, text = self.run_trace(["GL-0001", "sup-lot-a1", "RP0000002"],
                                            self.rows(3))
        self.assertEqual((report["ok"], code, report["evidence_label"]),
                         (True, 0, "injected-transport"), text[:3000])
        self.assertEqual(lv.check_expectations(spec, code, report), [])
        self.assertEqual((report["certificates"], report["certificates_with_lot"],
                          report["certificates_linked"], report["links"]), (3, 3, 2, 2))
        self.assertEqual((report["receipt_lines"], report["receipt_lines_with_supplier_lot"],
                          report["receipt_lines_linked"]), (4, 4, 2))
        self.assertEqual(report["hops"], {"supplier": 2, "purchase_order": 2, "product": 1})
        self.assertEqual(report["line_sources"], {"gold": 1, "fm": 1})
        self.assertEqual(sorted(json.dumps(s, sort_keys=True) for s in report["samples"]),
                         sorted(json.dumps(s, sort_keys=True) for s in [
                             {"line_sources": ["fm"], "supplier": True,
                              "purchase_order": True, "product": False},
                             {"line_sources": ["gold"], "supplier": True,
                              "purchase_order": True, "product": True}]))
        self.assertEqual(report["leaks"], 0)
        # Counts, booleans and enums only: no lot, id or name in the output.
        for secret in ("GL-0001", "SUP-LOT-A1", "RP0000002", "rl-1", "01COA", "Ningbo",
                       "PO100236"):
            self.assertNotIn(secret.casefold(), text.casefold())

    def test_the_spec_fails_when_no_certificate_states_a_served_lot(self):
        from verify import live_verify as lv
        spec = json.loads(LOT_TRACE_SPEC.read_text("utf-8"))
        report, code, text = self.run_trace(["NOT-RECEIVED"], self.rows(1))
        self.assertEqual((report["ok"], code, report["certificates_linked"]), (False, 1, 0),
                         text[:3000])
        self.assertNotEqual(lv.check_expectations(spec, code, report), [])

    def test_every_row_is_counted_once_at_any_cardinality(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                report, code, text = self.run_trace(["GL-0001"], self.rows(n))
                self.assertEqual((report["ok"], report["receipt_lines"],
                                  report["receipt_lines_with_supplier_lot"],
                                  report["certificates_linked"], report["links"]),
                                 (True, n + 1, n + 1, 1, 1), text[:2000])

    def test_a_reader_holding_the_files_is_counted_as_a_leak(self):
        # The leak count is live: the "withheld" reader set to the capture principal is
        # served both certificates and both reverse links, and the run fails.
        from integrations import capture_sharepoint as cs
        report, code, text = self.run_trace(["GL-0001", "sup-lot-a1"], self.rows(2),
                                            extra=["--withheld-principal",
                                                   cs.CAPTURE_PRINCIPAL])
        self.assertEqual((report["leaks"], report["ok"], code), (4, False, 1), text[:2000])

    def test_without_the_portal_the_run_is_blocked_never_ok(self):
        from verify import live_verify as lv
        spec = json.loads(LOT_TRACE_SPEC.read_text("utf-8"))
        report, code, text = self.run_trace(["GL-0001"], self.rows(1), portal=False)
        self.assertEqual((report["ok"], report.get("blocked"), report["certificates_linked"]),
                         (False, "no_record_source", 0), text[:2000])
        self.assertEqual(report["blocked_upstream"][0]["error_code"], "not_configured")
        self.assertNotEqual(lv.check_expectations(spec, code, report), [])

    def fm_rows(self, n, idreceipt=None, idsupp=None, idsupplier=None):
        """The live funnel's shape: ``n`` FM raw-material lines (lots FM-0001..) on one FM
        PO chain, gold receipt tables empty; the line's ``idreceipt``, the receipt's
        ``idsupp`` and the PO's ``idsupplier`` as given (None: the column is null)."""
        rows = {relation: [] for relation in self.rows(1)}
        rows.update({
            "public.receipt_raw_material": [{**raw(i, f"FM-{i:04d}"), "idreceipt": idreceipt}
                                            for i in range(1, n + 1)],
            "public.receipt": [{"id": 5, "idsupp": idsupp}],
            "public.supplier": [{"id": 40, "namesupp": "Ningbo"}],
            "public.generated_purchase_order_item": [{"id": 7, "idpogen": 900}],
            "public.generated_purchase_order": [{"id": 900, "ponumber": "PO100237",
                                                 "idsupplier": idsupplier}]})
        return rows

    def assert_shortfall_sums(self, report):
        for hop, causes in report["hop_shortfall"].items():
            short = report["certificates_linked"] - report["hops"][hop]
            self.assertEqual(sum(causes.values()), short, (hop, report["hop_shortfall"]))
            for path, stops in report["hop_shortfall_at"][hop].items():
                self.assertEqual(sum(stops.values()), short, (hop, path, stops))
            self.assertEqual(set(report["hop_paths"][hop]),
                             {">".join(p) for p in __import__(
                                 "scripts.eval_lot_trace", fromlist=["HOPS"]).HOPS[hop]})

    def test_a_hop_the_source_leaves_empty_is_typed_by_its_cause(self):
        from verify import live_verify as lv
        spec = json.loads(LOT_TRACE_SPEC.read_text("utf-8"))
        supplied = "for_receipt_line>line_of>supplied_by"
        via_po = "for_receipt_line>receives_line>line_of>ordered_from"
        # FM lines with no idreceipt, receipts with no idsupp, a PO with no vendor: the
        # certificate links its line and reaches the PO, no Supplier, no Product; the
        # minima are not lowered, so the run fails, each shortfall typed by its cause.
        report, code, text = self.run_trace(["fm-0001"], self.fm_rows(1))
        self.assertEqual((report["ok"], code, report["certificates_linked"], report["hops"]),
                         (False, 1, 1, {"supplier": 0, "purchase_order": 1, "product": 0}),
                         text[:3000])
        self.assertNotEqual(lv.check_expectations(spec, code, report), [])
        self.assert_shortfall_sums(report)
        self.assertEqual({hop: {c: n for c, n in causes.items() if n}
                          for hop, causes in report["hop_shortfall"].items()},
                         {"supplier": {"source_column_empty": 1}, "purchase_order": {},
                          "product": {"no_binding": 1}})
        at = report["hop_shortfall_at"]
        self.assertEqual(at["supplier"][supplied],
                         {"ReceiptLine.line_of source_column_empty": 1})
        self.assertEqual(at["supplier"][via_po],
                         {"PurchaseOrder.ordered_from source_column_empty": 1})
        self.assertEqual(at["product"], {"for_receipt_line>for_item": {
            "ReceiptLine.for_item no_binding": 1}})
        # The line names its receipt, the receipt no supplier: stopped one step later.
        report, code, text = self.run_trace(["fm-0001"], self.fm_rows(1, idreceipt=5))
        self.assertEqual(report["hop_shortfall_at"]["supplier"][supplied],
                         {"Receipt.supplied_by source_column_empty": 1}, text[:3000])
        # A vendor id no served supplier holds is a target not held, never an empty column.
        report, code, text = self.run_trace(["fm-0001"],
                                            self.fm_rows(1, idreceipt=5, idsupplier=999))
        self.assertEqual(report["hop_shortfall"]["supplier"]["target_not_held"], 1, text[:3000])
        self.assertEqual(report["hop_shortfall_at"]["supplier"][via_po],
                         {"PurchaseOrder.ordered_from target_not_held": 1})
        # The receipt silent, the PO's vendor reaches the Supplier: linked as far as the
        # source allows, and the run passes on the minima it always had.
        report, code, text = self.run_trace(["fm-0001"],
                                            self.fm_rows(1, idreceipt=5, idsupplier=40))
        self.assertEqual((report["ok"], code, report["hops"]["supplier"]), (True, 0, 1),
                         text[:3000])
        self.assertEqual(lv.check_expectations(spec, code, report), [])
        self.assertEqual(report["hop_paths"]["supplier"][via_po], 1)
        self.assertEqual(report["hop_paths"]["supplier"][supplied], 0)
        self.assert_shortfall_sums(report)
        for secret in ("FM-0001", "Ningbo", "PO100237", "01COA", "26161-1"):
            self.assertNotIn(secret.casefold(), text.casefold())

    def test_every_certificate_is_typed_once_at_any_cardinality(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                lots = [f"FM-{i:04d}" for i in range(1, n + 1)]
                report, code, text = self.run_trace(lots, self.fm_rows(n))
                self.assertEqual((report["certificates"], report["certificates_linked"],
                                  report["links"], report["hops"]["purchase_order"]),
                                 (n, n, n, n), text[:2000])
                self.assertEqual(report["hop_shortfall"]["supplier"]["source_column_empty"],
                                 n)
                self.assertEqual(report["hop_shortfall"]["product"]["no_binding"], n)
                self.assert_shortfall_sums(report)


class ShortfallCauseFailsClosed(unittest.TestCase):
    """A shortfall cause is named only from a served gap it recognises and a row the reader
    is served whole; anything else is ``unknown``, never a source cause."""

    LINE = 'ReceiptLine:portal.fm_receipt_raw_material:{"id":11}'
    SUBJECT = "ReceiptLine.line_of forward"

    def cause(self, around, read=None, bindings=None, handles=("api://h@g",)):
        from scripts import eval_lot_trace as trace

        class Served:
            def call(self, name, args, actor):
                return around if name == "plx.around" else (read or {}).get(
                    args.get("column_offset", 0), {"status": "withheld"})

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE object_versions (object_id, l1_handle, superseded_at)")
        conn.executemany("INSERT INTO object_versions VALUES (?, ?, NULL)",
                         [(self.LINE, h) for h in handles])
        compiled = schema.load_schema()
        if bindings is not None:
            compiled = type("Schema", (), {"link_types": {"ReceiptLine.line_of": {
                "bindings": bindings}}})()
        walk = trace._Walk(Served(), QUALITY, conn, compiled)
        objects, (link_type, why) = walk.step(self.LINE, "line_of")
        self.assertEqual((objects, link_type), ((), "ReceiptLine.line_of"))
        return why

    def gap(self, **extra):
        return {"objects": [], "gaps": [{"subject": self.SUBJECT, **extra}]}

    def test_an_unrecognised_gap_or_row_is_unknown(self):
        empty = {0: {"status": "resolved", "fields": {"row.idreceipt": None}}}
        self.assertEqual(self.cause(self.gap(reason="no_match", state="unknown"), empty),
                         "source_column_empty")
        self.assertEqual(self.cause(self.gap(reason="no_match", state="absent"), {0: {
            "status": "resolved", "fields": {"row.idreceipt": {"untrusted_text": " "}}}}),
                         "source_column_empty")
        self.assertEqual(self.cause(self.gap(reason="no_match", state="unknown"), {0: {
            "status": "resolved", "fields": {"row.idreceipt": 5}}}), "target_not_held")
        no_match = self.gap(reason="no_match", state="unknown")
        for label, why in {
                "an error": self.cause({"error": {"code": "invalid_input"}}),
                "no gap": self.cause({"objects": [], "gaps": []}),
                "another subject's gap": self.cause({"objects": [], "gaps": [
                    {"subject": "Receipt.supplied_by forward", "reason": "no_match"}]}),
                "two gaps": self.cause({"objects": [], "gaps": [
                    {"subject": self.SUBJECT, "reason": "no_match"},
                    {"subject": self.SUBJECT, "reason": "not_applicable_to_source"}]}),
                "an unknown reason": self.cause(self.gap(reason="something_new")),
                "a withheld row": self.cause(no_match, {}),
                "a missing column": self.cause(no_match, {0: {
                    "status": "resolved", "fields": {"row.id": 11}}}),
                "a row paged without end": self.cause(no_match, {
                    i: {"status": "resolved", "fields": {"row.idreceipt": None},
                        "columns": {"next_offset": i + 1}} for i in range(40)}),
                "no current version": self.cause(no_match, empty, handles=()),
                "a link through a via binding": self.cause(no_match, empty, bindings=[{
                    "from_binding": "portal.fm_receipt_raw_material", "via": "x",
                    "pairs": [{"from": "idreceipt", "to": "id"}]}]),
                "no binding of the record's source": self.cause(no_match, empty, bindings=[
                    {"from_binding": "portal.app_receipt_line",
                     "pairs": [{"from": "receipt_id", "to": "id"}]}])}.items():
            with self.subTest(label):
                self.assertEqual(why, "unknown")
        self.assertEqual(self.cause(self.gap(reason="no_match", state="not_ingested")),
                         "not_ingested")
        self.assertEqual(self.cause(self.gap(reason="unreadable_in_source")),
                         "source_unreadable")
        self.assertEqual(self.cause(self.gap(reason="not_applicable_to_source")),
                         "no_binding")


if __name__ == "__main__":
    unittest.main()
