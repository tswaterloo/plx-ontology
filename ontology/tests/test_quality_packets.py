"""Quality AO packets at the tool and evaluation boundaries.

A scanned assembly-order packet (an image-only PDF, so its text is the capture's OCR text)
is captured from SharePoint over the injected Graph transport of the capture tests, next
to FileMaker rows mirrored from a fixture reporting API shaped like the live AO trace
(EVIDENCE/live/2026-09-30-ao-26005b-live-trace.json). The projection serves the packet
as a typed ``AssemblyOrderPacket`` lead linked to its ``AssemblyOrder``; ``plx.object``
reports where paper and FileMaker disagree as typed conflicts; and the exposed eval item
``dev-F3-1001-exposed`` scores through the general path (the deterministic investigator
over the case host and the generic tools, ``evals.score``), and the P2/P8 bundle is
scored field by field against the same reader (``evals.document_fields``). A person's
confirmed document kind and Portal metadata (``operating.kind_confirmation``) project onto
the packet's SharePointFile, where ``plx.find`` answers them, and so does a document type a
person recorded through ``plx.review`` before (one kind store, one write path). No network,
no credentials.
Evidence: injected-transport / isolated-fixture, never live.
"""

from __future__ import annotations

from contextlib import closing
import datetime as dt
from hashlib import sha256
import importlib.util
import io
import json
import re
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from evals import baseline as bl
from evals import runner
from integrations import capture_sharepoint as cs
from integrations.live_source_guard import LiveSourceAudience
from integrations.tests import test_capture_sharepoint as tsp
from ontology import document_fields, mentions, reviews
from ontology.json_contract import validate
from ontology.project import MirrorCaptures, SharePointCaptures, build_store
from ontology.schema import load_schema
from ontology.types import Actor
from operating import attested_conflicts, kind_confirmation
from operating import ontology_tools_review as rt
from operating import ontology_tools as ot
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
ITEM = ROOT / "evals/exposed/dev-F3-1001-exposed.json"
PRINCIPALS = ("eval:dev-baseline", "staff:quality")
READER = Actor("plx", frozenset(PRINCIPALS))
NO_FILES = Actor("plx", frozenset({"eval:dev-baseline"}))
AO = 'AssemblyOrder:portal.fm_assembly_order:{"id":10718}'

# The packet as its scan's OCR text reads.
PAGES = [
    ["ASSEMBLY ORDER MO", "AO LOT#: AO-26005B   BULK LOT#: BC-26007E", "OE: OR1000151885",
     "PO:", "REQUIRED DATE:", "SIZE: 1509 EA"],
    ["ASSEMBLY ORDER - SHOP FLOOR", "Production date: 2026-01-07", "Filled qty: 1502 EA",
     "QC retain: 1", "Total QTY (EA): 150?", "Samples (EA): 1",
     "Damages: P-0121 = 07; P-0156 = 03; P-01274 = 11", "QA/QC checked: SA 2026-01-07"],
    ["FINISHED GOODS INSPECTION / RELEASE SHEET", "Rejects: 0",
     "QA release date: 2026-01-07", "[X] Shipment released", "[ ] On hold"]]
OCR_TEXT = mentions.PAGE_BREAK.join("\n".join(lines) for lines in PAGES)

# FileMaker rows as the live trace read them (ids, quantities and dates).
FM = {
    "public.assembly_order": [{"id": 10718, "nameamo_c": "AO-26005B", "status": "Realized",
                               "datemanuf": "2026-01-05", "qtymanufold": None,
                               "totalprodqty_c": None, "qtysample": None, "qtyacc_c": None,
                               "finalqty": None, "qtytotal": None, "units": "EA"}],
    "public.assembly_order_item": [
        {"id": 58383, "idassem": 10718, "idpack": 162, "qty_c": "1509", "actualamount": "1503",
         "qtydamage_c": None},
        {"id": 58384, "idassem": 10718, "idpack": 122, "qty_c": "1509", "actualamount": "1503",
         "qtydamage_c": None},
        {"id": 58386, "idassem": 10718, "idpack": 1415, "qty_c": "378",
         "actualamount": "375.75", "qtydamage_c": None}],
    "public.packaging": [{"id": 162, "packcode": "P-0156", "name": "Gallon jug"},
                         {"id": 122, "packcode": "P-0121", "name": "Cap"},
                         {"id": 1415, "packcode": "P-01274", "name": "Box"}],
    "public.assembly_order_damage": [
        {"id": 5444, "idassembly": 10718, "idassemblyitem": 58386, "qtydamaged": "20",
         "rawpack": "Pack"},
        {"id": 5445, "idassembly": 10718, "idassemblyitem": 58383, "qtydamaged": "3",
         "rawpack": "Pack"},
        {"id": 5446, "idassembly": 10718, "idassemblyitem": 58384, "qtydamaged": "7",
         "rawpack": "Pack"}],
    "public.production": [
        {"id": 14355, "categorymo": "AO", "idmo": 10718, "type": "Filling", "qty": "1502",
         "datestart": "2026-01-07"},
        {"id": 14356, "categorymo": "AO", "idmo": 10718, "type": "Cleaning", "qty": "0",
         "datestart": "2026-01-05"}],
    "public.order_entry_item_assembly": [{"id": 1245, "idassembly": 10718, "idoeitem": 1140,
                                          "qtyplan_c": "0.0", "qtymfg_c": "1503"}],
    "public.order_entry_item": [{"id": 1140, "idoe": 351, "qty": "1920"}],
    "public.order_entry": [{"id": 351, "erpcode": "OR1000151885", "datereq": "2026-01-01",
                            "custpocode": "PO12172025"}],
}


def audience() -> LiveSourceAudience:
    """The capture tests' view-time audience (the Quality folder; item permissions decide)
    at the internal clearance an evaluation reader holds."""
    folder = f"sharepoint:{tsp.DRIVE}:{tsp.QUALITY_FOLDER}"
    return LiveSourceAudience({folder: ["staff:quality"], folder + "/*": ["staff:quality"]},
                              tenant="plx", identities={"staff:quality": [tsp.RICARDO]},
                              source_acl_required=[f"sharepoint:{tsp.DRIVE}:*"],
                              default_classification="internal")


def confirm_packet(path: Path, captured: dict, digest: str) -> list[str]:
    """Confirmations as ``KindConfirmations.complete`` records them (the form flow is
    tested in operating/tests/test_kind_confirmation.py): the packet confirmed as a batch
    record through one original, re-confirmed as a fill record through another original
    of the same bytes, plus one of other bytes and one of a mail attachment (neither may
    reach the file). Returns the two packet confirmation times."""
    store = kind_confirmation.KindConfirmations(path)
    recorded = dt.datetime.fromisoformat(captured["recorded_at"].replace("Z", "+00:00"))
    times = [(recorded + dt.timedelta(milliseconds=n)).isoformat() for n in (1, 2)]
    sharepoint = {"source": "sharepoint-drive", "drive_id": tsp.DRIVE,
                  "item_id": "01AOPACKET", "etag": captured["etag"]}
    rows = [("orig-" + "1" * 32, digest, sharepoint, times[0], "BATCH_RECORD"),
            ("orig-" + "2" * 32, digest, sharepoint, times[1], "FILL_RECORD"),
            ("orig-" + "3" * 32, "0" * 64, sharepoint, times[0], "COA"),
            ("orig-" + "4" * 32, digest, {"source": "graph-mail-attachment"}, times[0],
             "COA")]
    db = store._db()
    try:
        for n, (original, sha, origin, at, category) in enumerate(rows):
            record = {"schema": kind_confirmation.SCHEMA, "id": f"kc-rec-{n:024d}",
                      "tenant": "plx", "original_id": original,
                      "citation": f"original://{original}", "original_sha256": sha,
                      "provenance": origin, "confirmed_fields": {
                          "DocumentCategory": category, "DocumentType": "Quality"},
                      "document_kind": "ao_scan", "amended_columns": ["DocumentCategory"],
                      "confirmed_by": "employee:00000000-0000-0000-0000-00000000000a",
                      "confirmed_at": at, "written_to_sources": False, "supersedes": None}
            text = kind_confirmation._canon(record)
            db.execute("INSERT INTO confirmation (id, tenant, original_id, drive_id, item_id, "
                       "record_json, record_sha256, supersedes) VALUES (?,?,?,?,?,?,?,?)",
                       (record["id"], "plx", original, origin.get("drive_id"),
                        origin.get("item_id"), text, kind_confirmation._sha(text), None))
    finally:
        db.close()
    return times


def scan(pages: int) -> bytes:
    """An image-only PDF (no text layer): the shape of the scanned packets."""
    from reportlab.pdfgen import canvas
    out = io.BytesIO()
    page = canvas.Canvas(out)
    for _ in range(pages):
        page.rect(60, 60, 400, 600, fill=1)
        page.showPage()
    page.save()
    return out.getvalue()


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "packets are PDFs: needs pypdf and reportlab")
class QualityPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from evals.oracle.fixture_world import World
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts import eval_dev_baseline as script
        from scripts.eval_unfamiliar_sample import _fixture_fetch

        cls.script = script
        cls.tmp = tempfile.TemporaryDirectory()
        work = Path(cls.tmp.name)
        client = WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                        fetch=_fixture_fetch(World(FM)))
        cls.product = script.build_product(
            client, work / "fm", sorted(FM), budget_seconds=None,
            clock=lambda: dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc))
        # The packet: captured as a scan (no OCR engine on this host), then OCR-read, as a
        # later capture walk on a host with tesseract fills the pinned text.
        cls.case = tsp.CaptureCase("run")
        cls.case.setUp()
        cls.bytes = scan(len(PAGES))
        cls.case.drive.file("01AOPACKET", "AO-26005B.pdf", "01AOS", cls.bytes)
        with mock.patch("integrations.document_understanding.local_ocr_available",
                        return_value=False):
            cls.case.capture().run(tsp.scope())
        cls.files = cls.case.store
        handle = cls.case.handle("01AOPACKET")
        assert cls.files.current(tsp.DRIVE, "01AOPACKET")["text_status"] == cs.OCR_UNAVAILABLE
        cls.files.fill_text(handle, cs.TEXT_OCR,
                            cls.files.put_blob(OCR_TEXT.encode("utf-8"), kind="text"))
        cls.captured = cls.files.current(tsp.DRIVE, "01AOPACKET")
        cls.confirmed_at = confirm_packet(work / "kind-confirmations.sqlite", cls.captured,
                                          sha256(cls.bytes).hexdigest())
        cls.store = work / "ontology.sqlite"
        cls.steps = {"mentions": mentions.store_mention_links,
                     "document_fields": document_fields.store_document_links,
                     "confirmations": kind_confirmation.projection_step(
                         work / "kind-confirmations.sqlite", "plx")}
        with MirrorCaptures(cls.product.mirror_path) as fm, \
                SharePointCaptures(cls.case.state) as files:
            cls.report = build_store(cls.store, load_schema(),
                                     {"portal_api": fm, "sharepoint": files},
                                     clock=lambda: dt.datetime(2026, 10, 4, 13, 0,
                                                               tzinfo=dt.timezone.utc),
                                     post_project=cls.steps)
        cls.gate = staticmethod(cls.files.item_gate(audience()))

    @classmethod
    def tearDownClass(cls):
        cls.case.tearDown()
        cls.tmp.cleanup()

    def tools(self, family=False, **kwargs):
        reader = ot.StoreReader.open(self.store)
        self.addCleanup(reader.conn.close)
        keys = list(reader.audience_keys()) + (["api:*"] if family else [])
        policy = ot.ReadPolicy(tenant="plx",
                               audiences={key: set(PRINCIPALS) for key in keys},
                               markings={m: set(PRINCIPALS) for m in ot.store_ddl.MARKINGS})
        return ot.OntologyTools(reader, policy, item_gate=self.gate, schema=ot.default_schema(),
                                grammar=None, contracts=CONTRACTS, **kwargs)

    def ok(self, name, result):
        self.assertEqual(validate(result, CONTRACTS["tools"][name]["output"], root=CONTRACTS),
                         [], json.dumps(result)[:1500])
        self.assertNotIn("error", result, json.dumps(result)[:800])
        return result

    def packet(self):
        conn = sqlite3.connect(self.store)
        try:
            (oid,), = conn.execute(
                "SELECT object_id FROM objects WHERE object_type = 'AssemblyOrderPacket'")
        finally:
            conn.close()
        return oid

    def test_packet_fields_are_typed_leads_cited_to_the_scan_and_its_ocr_text(self):
        # The whole packet fills more than one response: ask for the values inspected.
        out = self.ok("plx.object", self.tools().call("plx.object", {
            "object_id": self.packet(), "properties": [
                "planned_quantity", "filled_quantity", "retained_samples", "rejected_quantity",
                "produced_quantity", "required_date", "tier", "evidence", "shipment_released",
                "shipment_on_hold"]}, READER))
        values = {v["property"]: v for v in out["values"]}
        roles = {name: (values[name]["value"], values[name].get("unit"),
                        values[name].get("quantity_role"))
                 for name in ("planned_quantity", "filled_quantity", "retained_samples",
                              "rejected_quantity", "produced_quantity")}
        self.assertEqual(roles, {"planned_quantity": ("1509", "ea", "planned"),
                                 "filled_quantity": ("1502", "ea", "filled"),
                                 "retained_samples": ("1", "ea", "retained"),
                                 "rejected_quantity": ("0", "ea", "rejected"),
                                 "produced_quantity": (None, None, "produced")})
        # Every value was read from the scan's OCR text: a lead a person confirms.
        self.assertEqual({values[name].get("read_by") for name in roles}, {"ocr"})
        # Blank on paper is unknown (a null with a gap), never zero; "150?" is unreadable.
        evidence = json.loads(values["evidence"]["value"]["untrusted_text"])
        reasons = {g["field"]: g["reason"] for g in evidence["gaps"] if "key" not in g}
        self.assertEqual((reasons["required_date"], reasons["customer_po"],
                          reasons["produced_quantity"]),
                         ("value_missing", "value_missing", "unreadable"))
        self.assertIsNone(values["required_date"]["value"])
        self.assertEqual(values["tier"]["value"], "lead")
        # A box reads only as a known mark: "[X]" is checked, an empty box is a gap.
        boxes = evidence["fields"]
        self.assertEqual((boxes["shipment_released"]["value"], boxes["shipment_on_hold"]["value"],
                          boxes["shipment_on_hold"]["gap"]), ("checked", None, "unchecked"))
        self.assertEqual((values["shipment_released"]["value"],
                          values["shipment_on_hold"]["value"]), ("checked", None))
        # Damage per component, each its own cited count; nothing adds them up.
        entries = {e["key"]: e for e in evidence["fields"]["damaged"]["entries"]}
        self.assertEqual({k: e["value"] for k, e in entries.items()},
                         {"P-0121": "7", "P-0156": "3", "P-01274": "11"})
        self.assertNotIn('"21"', json.dumps(out))
        # Every citation resolves against the scan's bytes plus its pinned OCR text, and
        # against nothing else.
        handle = values["filled_quantity"]["citation"]["l1_handle"]
        self.assertIn("#sha256=" + sha256(self.bytes).hexdigest(), handle)
        self.assertEqual((evidence["l1_handle"], evidence["text_source"],
                          evidence["text_sha256"]),
                         (handle, "ocr", sha256(OCR_TEXT.encode("utf-8")).hexdigest()))
        cited = [document_fields.full_citation(evidence, c) for c in
                 [c for field in evidence["fields"].values() for c in field["citations"]] +
                 [c for e in entries.values() for c in e["citations"]]]
        for citation in cited:
            self.assertEqual(document_fields.resolve_citation(
                self.bytes, citation, mime_type="application/pdf", text=OCR_TEXT),
                citation["text"])
        box = document_fields.full_citation(evidence, entries["P-01274"]["citations"][0])
        self.assertEqual((box["page"], box["text"]), (2, "P-01274 = 11"))
        self.assertIsNone(document_fields.resolve_citation(
            self.bytes, box, mime_type="application/pdf", text=OCR_TEXT.replace("11", "12")))
        self.assertIsNone(document_fields.resolve_citation(
            self.bytes, box, mime_type="application/pdf"))   # the scan has no text layer

    def test_an_ao_lot_printed_on_every_page_is_one_value_and_two_lots_conflict(self):
        # The lot printed on three pages (once beside the bulk lot, one space apart) and
        # its label repeated unfilled is one value; a different lot on a page is a conflict.
        def read(third):
            pages = [["ASSEMBLY ORDER MO", "AO LOT#: AO-26005B BULK LOT#: BC-26007E"],
                     ["ASSEMBLY ORDER - SHOP FLOOR", "AO LOT#: AO-26005B", "AO LOT#:"],
                     ["FINISHED GOODS INSPECTION", f"AO LOT #: {third}"]]
            text = mentions.PAGE_BREAK.join("\n".join(lines) for lines in pages)
            return document_fields.extract_fields(scan(3), "application/pdf",
                                                  text=text)["fields"]
        fields = read("AO-26005B")
        self.assertEqual((fields["ao_lot"]["value"], fields["bulk_lot"]["value"]),
                         ("AO-26005B", "BC-26007E"))
        self.assertEqual([(c["page"], c["text"]) for c in fields["ao_lot"]["citations"]],
                         [(1, "AO-26005B"), (2, "AO-26005B"), (3, "AO-26005B")])
        self.assertEqual((read("AO-26006C")["ao_lot"]["value"],
                          read("AO-26006C")["ao_lot"]["gap"]), (None, "conflicting_values"))
        # SIZE stated in eaches and cases: read in its own unit (ea), the case counts kept
        # as cited alternates; pages that differ only in cases are no ambiguity, a second
        # count of eaches is.
        def planned(first, last):
            text = mentions.PAGE_BREAK.join((f"ASSEMBLY ORDER MO\nSIZE: {first}",
                                             "ASSEMBLY ORDER - SHOP FLOOR",
                                             f"FINISHED GOODS INSPECTION\nSIZE: {last}"))
            return document_fields.extract_fields(scan(3), "application/pdf",
                                                  text=text)["fields"]["planned_quantity"]
        size = planned("1509EA; 378CS", "1509EA; 377.25CS")
        self.assertEqual((size["value"], size["unit"], size.get("gap"),
                          [(a["value"], a["unit"], [(c["page"], c["text"]) for c in a[
                              "citations"]]) for a in size["alternates"]]),
                         ("1509", "ea", None, [("377.25", "cse", [(3, "377.25CS")]),
                                               ("378", "cse", [(1, "378CS")])]))
        self.assertEqual(size["alternate_conflicts"], ["cse"])
        self.assertEqual(planned("1509EA; 378CS", "1510EA; 378CS")["gap"], "conflicting_values")
        # A page whose OCR kept only the eaches before the separator ('1509EA;') states
        # the same count: one value, the other page's cases its alternates; a second count
        # of eaches that way is still a conflict, and a bare '1509EA;' has no alternates.
        cut = planned("1509EA; 378CS", "1509EA;")
        self.assertEqual((cut["value"], cut["unit"], cut.get("gap"),
                          [(a["value"], a["unit"]) for a in cut["alternates"]]),
                         ("1509", "ea", None, [("378", "cse")]))
        self.assertEqual(planned("1509EA; 378CS", "1510EA;")["gap"], "conflicting_values")
        alone = planned("1509EA;", "1509EA;")
        self.assertEqual((alone["value"], alone["unit"], "alternates" in alone),
                         ("1509", "ea", False))

    def test_unclear_boxes_and_quantities_are_gaps_and_roles_never_merge(self):
        # OCR of the release sheet as it may come back: only a known checked mark is a
        # value; a blank box, no box and any other glyph are gaps; inspected and produced
        # each keep their own read.
        head = "FINISHED GOODS INSPECTION / RELEASE SHEET\nTotal QTY (EA): 1503\n"
        cases = {"[x] Shipment released": ("checked", None),
                 "Shipment released: \u2611": ("checked", None),
                 "\u2610 Shipment released": (None, "unchecked"),
                 "Shipment released: [ ]": (None, "unchecked"),
                 "Shipment released:": (None, "unreadable"),
                 "Shipment released: [/]": (None, "unreadable"),
                 "Shipment released: yes": (None, "unreadable"),
                 "[x] Shipment released\nShipment released: [ ]": (None, "conflicting_values"),
                 "Shipment releasedx: x": (None, "not_stated"),
                 # a lone letter is OCR noise as often as a mark, even if the profile lists it
                 "Shipment released: x": (None, "unreadable"),
                 "x Shipment released": (None, "unreadable"),
                 # a box on both sides: which one is the label's is unknown
                 "[x]  Shipment released  [ ]": (None, "unreadable"),
                 "[ ] Shipment released [x]": (None, "unreadable"),
                 "[x] Shipment released  [ ] Other item": ("checked", None)}
        for line, want in cases.items():
            out = document_fields.extract_fields((head + line).encode(), "text/plain")
            box = out["fields"]["shipment_released"]
            self.assertEqual((box["value"], box.get("gap")), want, line)
            self.assertEqual(out["fields"]["produced_quantity"]["value"], "1503")
            self.assertEqual(out["fields"]["inspected_quantity"]["gap"], "not_stated")
        both = document_fields.extract_fields(
            (head + "[x]  Shipment released  [ ]").encode(), "text/plain")
        self.assertEqual([c["text"] for c in both["fields"]["shipment_released"]["citations"]],
                         ["[x]  Shipment released  [ ]"])   # cites both boxes
        out = document_fields.extract_fields(
            (head + "Inspected qty: 1500 EA").encode(), "text/plain")["fields"]
        self.assertEqual((out["produced_quantity"]["value"], out["inspected_quantity"]["value"]),
                         ("1503", "1500"))

    def test_every_listed_checked_mark_reads_checked(self):
        # The profile file and the reader agree: a mark listed as checked is a value.
        head = "FINISHED GOODS INSPECTION / RELEASE SHEET\n"
        for mark in document_fields.profiles()["checkbox_marks"]["checked"]:
            box = document_fields.extract_fields((head + f"Shipment released: {mark}").encode(),
                                                 "text/plain")["fields"]["shipment_released"]
            self.assertEqual((box["value"], box.get("gap")), ("checked", None), mark)

    def test_shop_floor_table_rows_state_produced_and_filled_cited_by_their_cells(self):
        # The shop-floor sheet states quantities per activity row under QTY (EA), with
        # Total and Samples rows (CONTEXT/02 3.2-3.3): produced is the Total row's cell,
        # filled the Filling row's, each cited by its own cell; inspected, which the sheet
        # never names, stays not_stated, and a label the reader cannot read is no silence.
        head = "ASSEMBLY ORDER - SHOP FLOOR\n"
        table = ["| Task | Date | Start | Finish | QTY (EA) | People |",
                 "|---|---|---|---|---|---|",
                 "| Setup | 2026-01-07 | 16:30 | 17:20 | 0 | 2 |",
                 "| Line clearance | 2026-01-07 | 17:20 | 17:25 | 1 | 1 |",
                 "| Filling | 2026-01-07 | 17:25 | 21:45 | 1502 | 4 |",
                 "| Total | | | | 1503 | |",
                 "| Samples | | | | 1 | |"]
        text = head + "\n".join(table) + "\nRejects 0\n"
        out = document_fields.extract_fields(text.encode(), "text/plain")["fields"]
        got = {name: (out[name]["value"], out[name].get("unit"),
                      [(text[c["start"]:c["end"]], c["text"]) for c in out[name]["citations"]])
               for name in ("produced_quantity", "filled_quantity", "retained_samples")}
        self.assertEqual(got, {"produced_quantity": ("1503", "ea", [("1503", "1503")]),
                               "filled_quantity": ("1502", "ea", [("1502", "1502")]),
                               "retained_samples": ("1", "ea", [("1", "1")])})
        self.assertEqual(text.index("1503"), out["produced_quantity"]["citations"][0]["start"])
        self.assertEqual((out["inspected_quantity"]["value"], out["inspected_quantity"]["gap"],
                          out["inspected_quantity"]["citations"]), (None, "not_stated", []))
        # "Rejects 0" is a printed label with its number, no ':' (an unseparated field);
        # "Rejects none" names the field where no value is read: a reader limit, cited.
        self.assertEqual((out["rejected_quantity"]["value"], out["rejected_quantity"]["unit"],
                          [c["text"] for c in out["rejected_quantity"]["citations"]]),
                         ("0", "ea", ["0"]))
        unread = document_fields.extract_fields(
            (head + "Rejects none\n").encode(), "text/plain")["fields"]["rejected_quantity"]
        self.assertEqual((unread["gap"], [c["text"] for c in unread["citations"]]),
                         ("label_seen_unread", ["Rejects"]))
        # A labelled total beside the table is one more read: agreeing, one value with
        # both citations; disagreeing, a conflict, never a pick.
        for total, want in (("1503", ("1503", None)), ("1504", (None, "conflicting_values"))):
            both = document_fields.extract_fields(
                (text + f"Total QTY (EA): {total}\n").encode(), "text/plain")["fields"]
            self.assertEqual((both["produced_quantity"]["value"],
                              both["produced_quantity"].get("gap")), want, total)
        # OCR that drops the blank cells: the Total and Filling rows no longer line up with
        # their header, each a gap citing its row, never a cell fitted to a column.
        loose = head + "Task  Date  Start  Finish  QTY (EA)  People\n" \
            "Filling  2026-01-07  17:25  21:45  1502  4\nTotal  1503\n"
        out = document_fields.extract_fields(loose.encode(), "text/plain")
        self.assertEqual((out["fields"]["filled_quantity"]["value"],
                          out["fields"]["produced_quantity"]["value"]), ("1502", None))
        self.assertEqual([(g["field"], [c["text"] for c in g["citations"]])
                          for g in out["gaps"] if g["reason"] == "unaligned"],
                         [("produced_quantity", ["Total  1503"])])
        # Tesseract's single-spaced rows split into no cells: no value, but the Filling
        # and Total rows name both roles, so the reader limit is counted, never silence.
        from scripts import eval_document_fields_live as live
        ocr = head + "Task Date Start Finish QTY (EA) People\n" \
            "Filling 2026-01-07 08:30 14:00 2012 4\nTotal 2012\n"
        out = document_fields.extract_fields(ocr.encode(), "text/plain")["fields"]
        self.assertEqual({name: (out[name]["gap"], out[name]["unread"],
                                 [c["text"] for c in out[name]["citations"]])
                          for name in ("produced_quantity", "filled_quantity")},
                         {"produced_quantity": ("label_seen_unread", "single_spaced", ["Total"]),
                          "filled_quantity": ("label_seen_unread", "single_spaced", ["Filling"])})
        counts = live.denominators({"documents": 1, "document_class": "ao_packet", "fields": {
            name: {field.get("gap") or "stated": 1} for name, field in out.items()}})
        self.assertEqual(counts["role_outcomes"]["produced"]["label_seen_unread"], 1)
        self.assertGreater(counts["roles_unread"], 0)
        # A header naming two of a field's columns reads the first one it declares.
        two = head + "Task  QTY  QTY (EA)\nFilling  9  1502\n"
        self.assertEqual(document_fields.extract_fields(two.encode(), "text/plain")[
            "fields"]["filled_quantity"]["value"], "1502")
        # A blank row of a converted sheet keeps the table; a component list's bare Qty
        # column sums its Total row: never the produced count.
        gap = head + "| Task | QTY (EA) |\n|---|---|\n| Filling | 1502 |\n| | |\n| Total | 1503 |\n"
        self.assertEqual(document_fields.extract_fields(gap.encode(), "text/plain")[
            "fields"]["produced_quantity"]["value"], "1503")
        bom = head + "Code  Description  Qty\nP-0156  Gallon jug  1509\nTotal  all  5998.275 kg\n"
        self.assertEqual(document_fields.extract_fields(bom.encode(), "text/plain")[
            "fields"]["produced_quantity"]["value"], None)
        # A cell the reader cannot place is refused when the profile file loads: a
        # misspelt key, or a row label another field reads.
        data = json.loads(document_fields.PROFILES_PATH.read_text("utf-8"))
        fields = next(p for p in data["profiles"] if p["id"] == "ao_packet")["fields"]
        filled = next(f for f in fields if f["name"] == "filled_quantity")
        for cell, error in (({"rows": ["filling"], "column": ["qty"]}, "key_unknown"),
                            ({"rows": ["samples"], "columns": ["qty"]}, "cell_invalid")):
            filled["cell"] = cell
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "profiles.json"
                path.write_text(json.dumps(data), "utf-8")
                document_fields.profiles.cache_clear()
                try:
                    with mock.patch.object(document_fields, "PROFILES_PATH", path), \
                            self.assertRaisesRegex(ValueError, error):
                        document_fields.profiles()
                finally:
                    document_fields.profiles.cache_clear()

    def test_a_printed_shop_floor_form_reads_its_unseparated_labels_and_damage_lines(self):
        # The printed sheet's boxes carry no ':' (live gap-d1ebfd445d55): 'Total QTY (EA)'
        # and 'Samples (EA)' share one bottom line, each followed by its number; DAMAGES
        # heads lines of 'part code  qty  initial'; a task cell prints every option and
        # the worker underlines one. Values here are synthetic.
        from scripts import eval_document_fields_live as live
        head = "ASSEMBLY ORDER - SHOP FLOOR\n"
        header = "Task  Date  Start  Finish  QTY (EA)  People\n"
        options = "Filling/Setup/Tear down/Line clearance/Labeling/Other"
        damages = "DAMAGES (part code; qty; initial)\n\n  P-0901  5   AB\n  P-09022 = 3\n" \
            "Production checked\n"

        def fields(text):
            return document_fields.extract_fields(text.encode(), "text/plain")["fields"]

        def got(field):
            return (field.get("value"), field.get("unit"), field.get("gap"),
                    [c["text"] for c in field["citations"]])

        packet = head + header + "Setup  2026-02-03  08:00  09:00  0  2\n" \
            "Filling  2026-02-03  09:00  12:00  40  3\n" \
            "Total QTY (EA)  41    Samples (EA)  1    Date  2026/02/03\n" + damages
        out = fields(packet)
        self.assertEqual({n: got(out[n]) for n in ("produced_quantity", "filled_quantity",
                                                   "retained_samples")},
                         {"produced_quantity": ("41", "ea", None, ["41"]),
                          "filled_quantity": ("40", "ea", None, ["40"]),
                          "retained_samples": ("1", "ea", None, ["1"])})
        self.assertEqual([(e["key"], e["value"], e["unit"], [c["text"] for c in e["citations"]])
                          for e in out["damaged"]["entries"]],
                         [("P-0901", "5", "ea", ["P-0901  5"]),
                          ("P-09022", "3", "ea", ["P-09022 = 3"])])
        self.assertNotIn("gap", out["damaged"])
        for n in (1, 10, 60):   # no line lost or doubled, however long the list
            many = fields(head + "DAMAGES\n" + "".join(f"P-{1000 + i}  {i + 1}  AB\n"
                                                       for i in range(n)))["damaged"]
            self.assertEqual([(e["key"], e["value"]) for e in many["entries"]],
                             [(f"P-{1000 + i}", str(i + 1)) for i in range(n)], n)
        # Every role the sheet names is read: nothing is left as a reader gap.
        counts = live.denominators({"documents": 1, "document_class": "ao_packet", "fields": {
            name: {field.get("gap") or "stated": 1} for name, field in out.items()}})
        self.assertEqual(counts["roles_unread"], 0)
        # Tesseract's single-spaced line: each label's number, cut at the next label.
        line = fields(head + "Total QTY (EA) 41 Samples (EA) 2 Completed by AB Date 2026/02/03\n"
                      "DAMAGES P-0901 = 05; P-0902 = 2\nREJECTS 0\n")
        self.assertEqual([got(line[n]) for n in ("produced_quantity", "retained_samples",
                                                 "rejected_quantity")],
                         [("41", "ea", None, ["41"]), ("2", "ea", None, ["2"]),
                          ("0", "ea", None, ["0"])])
        self.assertEqual([(e["key"], e["value"]) for e in line["damaged"]["entries"]],
                         [("P-0901", "5"), ("P-0902", "2")])
        # A task cell printing every option: which row is the filling, a mark on paper, is
        # not in the text: unreadable citing the rows, never a reader gap and never a pick.
        choice = fields(head + header + options + "  2026-02-03  08:00  09:00  0  2\n" +
                        options + "  2026-02-03  09:00  12:00  40  3\n")["filled_quantity"]
        self.assertEqual((choice["value"], choice["gap"], len(choice["citations"])),
                         (None, "unreadable", 2))
        self.assertTrue(all(c["text"].startswith(options) for c in choice["citations"]))
        lone = fields(head + "Task Date QTY (EA)\n" + options + " 2026-02-03 40 3\n")
        self.assertEqual(got(lone["filled_quantity"])[2:],
                         ("unreadable", [options.split("/")[0]]))
        # Only a cell field's row label is an option pick: a plain label before a '/'
        # ('Date/Time', 'Cartons/Pallets', 'Rejects/Damages') is a label the reader
        # missed, a reader gap in every profile, never a claimed mark on paper.
        for text, name, label in (
                ("COMMERCIAL INVOICE\nInvoice No: INV-1\nDate/Time 2026-01-02\n",
                 "document_date", "Date"),
                ("COMMERCIAL INVOICE\nInvoice No: INV-1\nVendor/Supplier code\n",
                 "seller", "Vendor"),
                ("PACKING SLIP\nPacking Slip No: PS-1\nCartons/Pallets 4/1\n",
                 "packages", "Cartons"),
                (head + "Rejects/Damages 3\n", "rejected_quantity", "Rejects")):
            self.assertEqual(got(fields(text)[name])[2:], ("label_seen_unread", [label]), text)
        # Never a guess: a second Filling row with another quantity is a conflict, never a
        # sum; a second number, a contrary unit or an unclear digit stays the read's gap.
        twice = packet.replace("Total QTY", "Filling  2026-02-03  13:00  14:00  7  3\nTotal QTY")
        self.assertEqual(got(fields(twice)["filled_quantity"])[:3],
                         (None, None, "conflicting_values"))
        for tail, gap in (("41 4", "several_values"), ("41 cs", "unit_unknown"),
                          ("4?", "unreadable")):
            self.assertEqual(fields(head + f"Total QTY (EA) {tail}\n")["produced_quantity"][
                "gap"], gap, tail)
        # A key line with a second number, or a DAMAGES header with no key line under it.
        two = fields(head + "DAMAGES\nPart code  Qty  Initial\nP-0901  4  2\n")["damaged"]
        self.assertEqual([(e["key"], e["value"], e["gap"]) for e in two["entries"]],
                         [("P-0901", None, "several_values")])
        table = fields(head + "DAMAGES\n| Part code | Qty | Initial |\n| P-0901 | 4 | AB |\n"
                       "| P-0902 | 1 | AB |\n")["damaged"]
        self.assertEqual([(e["key"], e["value"], [c["text"] for c in e["citations"]])
                          for e in table["entries"]],
                         [("P-0901", "4", ["P-0901 | 4"]), ("P-0902", "1", ["P-0902 | 1"])])
        # A blank box followed by a section heading is not a column header: the component
        # list under it is no stated damage count; a single-space header naming Qty is.
        for heading in ("DAMAGES\n\nComponents\nP-1000  500  bottle\n",
                        "Damages:\nRaw materials\nP-1000  12.5 kg\n",
                        "DAMAGES\n\nComponents  Lot\nP-1000  500  bottle\n"):
            self.assertEqual(fields(head + heading)["damaged"]["entries"], [], heading)
        named = fields(head + "DAMAGES\nComponent Description Qty\nP-0901  4\n")["damaged"]
        self.assertEqual([(e["key"], e["value"]) for e in named["entries"]], [("P-0901", "4")])
        # A box is blank on the paper only when its label's line holds nothing but the
        # printed parenthetical and the next section (a label of the class) or the page
        # end closes it; any other text in it, digits or letters (a note, handwriting OCR
        # read as letters or specks), is a reader limit naming why, never a blank.
        for text in ("DAMAGES (part code; qty; initial)\nQC checked: AB\n",
                     "DAMAGES (part code; qty; initial):  ____\n"):
            self.assertEqual(got(fields(head + text)["damaged"])[2:],
                             ("value_missing", ["DAMAGES"]), text)
        for text in ("DAMAGES (part code; qty; initial)\nProduction checked\n",
                     "DAMAGES  see attached\n", "DAMAGES\nsee attached sheet\n",
                     "DAMAGES  PO-lZl seven\n", "DAMAGES\n~~ ,. ;\n",
                     "DAMAGES (part code; qty; initial)\n0121 = 07\n"):
            unread = fields(head + text)["damaged"]
            self.assertEqual((unread["gap"], unread["unread"]), ("label_seen_unread", "no_key"),
                             text)
        # A page that never names them stays silent on them.
        silent = fields(head + "Lead: AB\nProduction checked\n")
        self.assertEqual({n: silent[n]["gap"] for n in ("produced_quantity", "filled_quantity",
                                                       "retained_samples", "damaged")},
                         dict.fromkeys(("produced_quantity", "filled_quantity",
                                        "retained_samples", "damaged"), "not_stated"))
        # 'unseparated' is a quantity field's flag, true or absent: anything else is refused.
        data = json.loads(document_fields.PROFILES_PATH.read_text("utf-8"))
        specs = next(p for p in data["profiles"] if p["id"] == "ao_packet")["fields"]
        for name, flag in (("ao_lot", True), ("rejected_quantity", "yes")):
            spec = next(f for f in specs if f["name"] == name)
            saved = dict(spec)
            spec["unseparated"] = flag
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "profiles.json"
                path.write_text(json.dumps(data), "utf-8")
                document_fields.profiles.cache_clear()
                try:
                    with mock.patch.object(document_fields, "PROFILES_PATH", path), \
                            self.assertRaisesRegex(ValueError, "unseparated_invalid"):
                        document_fields.profiles()
                finally:
                    document_fields.profiles.cache_clear()
            spec.clear()
            spec.update(saved)

    def test_one_scanner_reads_a_line_items_table_and_a_cell_table_each_cell_once(self):
        # A class with both line items (a component list) and cell fields: each header
        # heads one table of one kind, so the component list's Total row is an item and
        # the shop-floor Total row the produced count; no cell is cited twice.
        data = json.loads(document_fields.PROFILES_PATH.read_text("utf-8"))
        packet = next(p for p in data["profiles"] if p["id"] == "ao_packet")
        packet["line_items"] = {"columns": [
            {"name": "component", "kind": "text", "labels": ["code"]},
            {"name": "description", "kind": "text", "labels": ["description"]},
            {"name": "quantity", "kind": "quantity", "labels": ["qty"]}]}
        text = ("ASSEMBLY ORDER - SHOP FLOOR\nCode  Description  Qty\n"
                "P-0156  Gallon jug  1509 ea\nTotal  all  5998.275 kg\n\n"
                "| Task | Date | QTY (EA) |\n|---|---|---|\n| Filling | 2026-01-07 | 1502 |\n"
                "| Total | | 1503 |\n")
        caches = [f for f in vars(document_fields).values() if hasattr(f, "cache_clear")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            path.write_text(json.dumps(data), "utf-8")
            for cached in caches:
                cached.cache_clear()
            try:
                with mock.patch.object(document_fields, "PROFILES_PATH", path):
                    out = document_fields.extract_fields(text.encode(), "text/plain")
            finally:
                for cached in caches:
                    cached.cache_clear()
        self.assertEqual([[item["cells"][c]["value"] for c in ("component", "quantity")]
                          for item in out["line_items"]],
                         [["P-0156", "1509"], ["Total", "5998.275"]])
        fields = out["fields"]
        self.assertEqual({name: (fields[name]["value"], [c["text"] for c in fields[name][
            "citations"]]) for name in ("produced_quantity", "filled_quantity")},
            {"produced_quantity": ("1503", ["1503"]), "filled_quantity": ("1502", ["1502"])})
        cited = [(c["start"], c["end"]) for field in fields.values()
                 for c in field["citations"]] + \
            [(cell["citation"]["start"], cell["citation"]["end"])
             for item in out["line_items"] for cell in item["cells"].values()]
        self.assertEqual(len(cited), len(set(cited)))

    def test_a_scanned_shop_floor_row_reads_its_cells_through_the_ocr_call(self):
        # The OCR call's word boxes (tesseract TSV, 10 px a character): the handwritten
        # date and people cells are dropped, the times and quantities written off the
        # printed columns and baseline, so no row has its header's cell count. Rebuilt by
        # position, a cell under the QTY (EA) header cell alone is that column's read.
        from integrations import document_understanding as du
        words = [("ASSEMBLY", 0, 20), ("ORDER", 90, 20), ("-", 150, 20), ("SHOP", 170, 20),
                 ("FLOOR", 220, 20), ("Task", 0, 100), ("Date", 200, 100),
                 ("Start", 400, 100), ("Finish", 550, 100), ("QTY", 700, 100),
                 ("(EA)", 740, 100), ("People", 850, 100), ("Filling", 0, 150),
                 ("08:30", 405, 152), ("14:00", 555, 158), ("1502", 695, 145),
                 ("Total", 0, 200), ("DAMAGES", 0, 300)]

        def tsv(total_left, hand=21):   # the DAMAGES box's handwriting, read at ``hand``
            rows = ["\t".join(du._TSV_HEADER)] + [
                f"5\t1\t1\t1\t1\t{n}\t{left}\t{top}\t{10 * len(text)}\t20\t{conf}\t{text}"
                for n, (text, left, top, conf) in enumerate(
                    [(*w, 91) for w in words] + [("1503", total_left, 203, 91),
                                                  ("Qx", 120, 302, hand),
                                                  ("Ol", 160, 298, hand)])]
            return "\n".join(rows) + "\n"

        def read(output, resolve=()):
            def run(command, **_):
                tool = Path(command[0]).name
                out = {"pdfinfo": b"Page    1 size: 612 x 792 pts (letter)\n" if "-f" in
                       command else b"Pages:          1\n", "pdftotext": b"\f",
                       "pdftoppm": b"", "tesseract": output.encode()}[tool]
                return du.subprocess.CompletedProcess(command, 0, out, b"")

            with mock.patch.object(du.platform, "system", return_value="Linux"), \
                    mock.patch.object(du.shutil, "which", side_effect=lambda t: f"/fake/{t}"), \
                    mock.patch.object(du.child_process, "run", side_effect=run), \
                    mock.patch.object(du, "_png_size", return_value=(2550, 3300)):
                out = document_fields.extract_fields(PDF, "application/pdf", local_pdf_ocr=True)
                return out, [document_fields.resolve_citation(
                    PDF, document_fields.full_citation(out, c), mime_type="application/pdf",
                    local_pdf_ocr=True) for c in resolve]

        PDF = b"%PDF-1.4\n%scan\n"
        out, _ = read(tsv(702))
        filled, produced = (out["fields"][n] for n in ("filled_quantity", "produced_quantity"))
        self.assertEqual([(f["value"], f["unit"], f["read_by"], [c["text"] for c in f["citations"]])
                          for f in (filled, produced)],
                         [("1502", "ea", "ocr", ["1502"]), ("1503", "ea", "ocr", ["1503"])])
        # The DAMAGES box holds words the same word boxes read below the handwriting
        # bound: handwritten part codes OCR cannot key, cited where written; read as
        # print (at the bound), the box stays no_key.
        damaged = out["fields"]["damaged"]
        self.assertEqual((damaged["entries"], damaged["gap"], damaged["unread"],
                          [c["text"] for c in damaged["citations"]]),
                         ([], "label_seen_unread", "handwritten_unkeyed", ["DAMAGES", "Qx  Ol"]))
        _, resolved = read(tsv(702), filled["citations"] + produced["citations"] +
                           damaged["citations"])
        self.assertEqual(resolved, ["1502", "1503", "DAMAGES", "Qx  Ol"])   # the OCR text
        printed = read(tsv(702, hand=du.HANDWRITING_CONFIDENCE))[0]["fields"]["damaged"]
        self.assertEqual((printed["gap"], printed["unread"]), ("label_seen_unread", "no_key"))
        # A number written under the Finish column is never the QTY (EA) cell: the row
        # stays unaligned, citing it, never a pick.
        moved = read(tsv(580))[0]["fields"]["produced_quantity"]
        self.assertEqual((moved["value"], moved["gap"], [c["text"][:5] for c in
                                                         moved["citations"]]),
                         (None, "unaligned", ["Total"]))
        # Output that is not TSV (another tesseract build, a truncated run) fails the OCR
        # read, never a partial page read as plain text.
        for output in ("ASSEMBLY ORDER - SHOP FLOOR\nTotal 1503\n",
                       tsv(702).replace("\t20\t91\t", "\t20\thigh\t", 1)):
            with self.assertRaisesRegex(ValueError, "local_pdf_ocr_failed"):
                read(output)

    def test_word_box_ocr_text_reads_ruled_cells_and_only_its_own_pages_by_column(self):
        # Pinned OCR text of a ruled shop-floor form (synthetic). Only a page rebuilt from
        # word boxes (it ends in LAYOUT_MARK) reads a cell by its columns; the printed
        # column rules OCR reads as ' | ' split cells; a task cell printing its options
        # with rules is a pick on paper; handwriting glued to a label is unreadable.
        from integrations import document_understanding as du

        def line(*cells):   # (column, text) pairs at their character columns
            out = ""
            for column, text in cells:
                out = out.ljust(column) + text
            return out

        def fields(lines, mark=True):
            page = "\n".join(lines) + ("\n" + du.LAYOUT_MARK if mark else "")
            return document_fields.extract_fields(scan(1), "application/pdf",
                                                  text=page)["fields"]

        def got(field):
            return (field.get("value"), field.get("gap"), field.get("unread"),
                    [c["text"] for c in field["citations"]])

        def fields_gaps(lines):
            return document_fields.extract_fields(
                scan(1), "application/pdf",
                text="\n".join(lines) + "\n" + du.LAYOUT_MARK)["gaps"]

        head = "ASSEMBLY ORDER - SHOP FLOOR"
        ruled = line((0, "Task"), (16, "Date"), (28, "Start | Finish | QTY (EA)"),
                     (56, "People"))
        qty = ruled.index("QTY")
        rows = [head, ruled, line((70, "by")),   # the header's second line, past its end
                line((0, "Filling"), (16, "2026-01-07"), (qty, "1502"), (56, "4")),
                line((0, "Total"), (qty, "1503"))]
        out = fields(rows)
        self.assertEqual([got(out[n]) for n in ("filled_quantity", "produced_quantity")],
                         [("1502", None, None, ["1502"]), ("1503", None, None, ["1503"])])
        # Old pinned OCR text (no mark) with a dropped cell: the row shifts left, so the
        # count rule holds: unaligned, never the People cell read as QTY.
        shifted = [head, "Task  Date  Start  Finish  QTY (EA)  People",
                   "Filling  08:30     14:00     1502      4"]
        self.assertEqual(got(fields(shifted, mark=False)["filled_quantity"])[:2],
                         (None, "unaligned"))
        self.assertIsNone(fields(rows, mark=False)["filled_quantity"]["value"])
        # A ruled option list ('Filling | Setup | Tear down', '|Setup|', a speck '|.')
        # with its continuation lines: which task, an underline, is not in the text.
        for options in ("Filling | Setup | Tear down", "Filling |Setup|Tear down",
                        "Filling |. Setup | Tear down"):
            picked = fields([head, ruled, line((0, options), (qty, "1502")),
                             line((0, "Line clearance | Labeling")), "Other:",
                             line((0, "Total"), (qty, "1503"))])
            self.assertEqual(got(picked["filled_quantity"])[:2], (None, "unreadable"),
                             options)
            self.assertEqual(picked["produced_quantity"]["value"], "1503", options)
        # The bottom line's label: its number after a ',' reads; text glued to it or
        # after a mark is the box's handwriting misread, and a glued fill line alone is
        # its box read empty: unreadable citing it, never a number out of it; a
        # plain-space word or a fill line after a gap stays unread.
        for bottom, want in (
                ("Total QTY (EA), 744", ("744", None, None, ["744"])),
                ("Total QTY (EA)lvi 264   Samples (EA)  1",
                 (None, "unreadable", None, ["lvi 264"])),
                ("Total QTY (EA)_|$ QF   Samples (EA)  1",
                 (None, "unreadable", None, ["$ QF"])),
                ("Total QTY (EA). Z0Y", (None, "unreadable", None, ["Z0Y"])),
                ("Total QTY (EA)____   Samples (EA)  1",
                 (None, "unreadable", None, ["____"])),
                ("Total QTY (EA)  ____   Samples (EA)  1",
                 (None, "label_seen_unread", "value_unplaced", ["Total QTY (EA)"])),
                ("Total QTY (EA)   Samples (EA)  1",
                 (None, "label_seen_unread", "value_unplaced", ["Total QTY (EA)"]))):
            self.assertEqual(got(fields([head, bottom])["produced_quantity"]), want, bottom)
        for glued in ("Total QTY (EA)lvi 264", "Total QTY (EA)__|"):
            self.assertEqual(got(fields([head, glued], mark=False)["produced_quantity"])[1],
                             "label_seen_unread", glued)
        self.assertEqual(got(fields([head, "Samples (EA)__|   Completed By"])[
            "retained_samples"])[:2], (None, "unreadable"))
        # A DAMAGES box beside the next section is its label line's cells left of that
        # cell; a key written across that column is never cut into a shorter part code.
        label = line((0, "DAMAGES (Part Code; QTY; Initial):"), (40, "QC checked"))
        for below, want in ((line((2, "P-0121  07")), [("P-0121", "7")]),
                            (line((36, "P-0121 07")), None)):
            damaged = fields([head, label, below])["damaged"]
            if want is None:
                self.assertEqual((damaged["entries"], damaged["gap"], damaged["unread"]),
                                 ([], "label_seen_unread", "no_key"))
            else:
                self.assertEqual([(e["key"], e["value"]) for e in damaged["entries"]], want)
        self.assertEqual(got(fields([head, label, "", "QC checked: AB"])["damaged"])[1],
                         "value_missing")
        # The mark is the page's end, never text in a blank box at the page's foot.
        for mark in (True, False):
            self.assertEqual(got(fields([head, label[:40].rstrip(), ""], mark)[
                "damaged"])[1], "value_missing", mark)
        self.assertEqual(got(fields([head, label, ""])["damaged"])[1], "value_missing")
        # A label word run into text below the box or beside it ('PO-121' is P-0121 OCR
        # misread) is box text, never the next section: the box stays unread, never blank.
        for box in (["DAMAGES", "PO-121  07"], ["DAMAGES", "Po-0121 7 AB"],
                    ["DAMAGES", "OE 0121 3"], ["DAMAGES", "Total 3 jugs"],
                    [line((0, "DAMAGES (Part Code; QTY; Initial):"), (40, "PO-121"))],
                    [line((0, "DAMAGES (Part Code; QTY; Initial):"), (40, "Size 3"))]):
            for mark in (True, False):
                self.assertEqual(got(fields([head, *box], mark)["damaged"])[1],
                                 "label_seen_unread", (box, mark))
        # Handwriting beside the label is the box itself (no section there): its keys
        # read; text naming no key beside them (garbled handwriting) is key_missing, so
        # the entries never look complete; with no key read the box stays unread.
        printed = "DAMAGES (Part Code; QTY; Initial):"
        for box, entries, gap in (
                ([line((0, printed), (40, "P-0131 = 2"))], [("P-0131", "2")], None),
                ([line((0, printed), (40, "Ab. ex)   - Bx")), "", line((80, "wr")),
                  line((40, "P-0131 - 4"))], [("P-0131", "4")], "key_missing"),
                ([line((0, printed), (40, "Ab. ex)   - Bx")), line((40, "Qx - Ol  ="))],
                 [], "label_seen_unread"),
                # A printed word ends the box: a code list below is no damage.
                ([printed, "Components", "P-0131  4"], [], "label_seen_unread")):
            damaged = fields([head, *box])["damaged"]
            self.assertEqual([(e["key"], e["value"]) for e in damaged["entries"]], entries,
                             box)
            reasons = [g["reason"] for g in fields_gaps([head, *box])
                       if g["field"] == "damaged"]
            self.assertEqual(reasons[:1], [gap] if gap else [], box)
        # A box at least 3/4 of whose text the mark line names as read below the
        # handwriting bound is handwritten_unkeyed (a stray glyph read high does not
        # undo it), citing its text; the printed line that ended it does not decide.
        # Text mostly read as print, or a mark line naming another bound, keeps it
        # no_key (never handwriting on doubt).
        page = "\n".join([head, line((0, printed), (40, "Ab. ex)   - Bx")),
                          line((40, "Qx - Ol  ="))])
        hand = [m.span() for m in re.finditer(r"\S+", page) if m.start() > page.index(":")]
        page += "\n" + line((0, "Production Checked By/Date"))
        for spans, bound, want in (
                (hand[:-1], du.HANDWRITING_CONFIDENCE, ("handwritten_unkeyed", [
                    "DAMAGES", "Ab. ex)   - Bx", "Qx - Ol  ="])),
                (hand[:4], du.HANDWRITING_CONFIDENCE, ("no_key", ["DAMAGES"])),
                (hand, du.HANDWRITING_CONFIDENCE + 1, ("no_key", ["DAMAGES"]))):
            damaged = document_fields.extract_fields(
                scan(1), "application/pdf", text=page + f"\n{du.LAYOUT_MARK} below {bound}: "
                + " ".join(f"{a}-{b}" for a, b in spans))["fields"]["damaged"]
            self.assertEqual((damaged["gap"], damaged["unread"],
                              [c["text"] for c in damaged["citations"]]),
                             ("label_seen_unread", *want), (spans, bound))

    def test_a_confirmed_document_category_is_findable_on_the_document(self):
        step = self.report["post_project"]["confirmations"]["counts"]
        self.assertEqual((step["projected"], step["superseded"], step["not_projected"],
                          step["not_sharepoint"], step["values"]), (1, 1, 1, 1, 3))
        tools = self.tools()

        def find(category, actor=READER, **extra):
            out = tools.call("plx.find", {"object_type": "SharePointFile", "filters": [
                {"property": "document_category", "op": "eq", "value": category}], **extra},
                actor)
            self.ok("plx.find", out)
            return sorted(c["object"]["object_id"] for c in out.get("candidates", ()))

        (file_id,) = find("FILL_RECORD")
        self.assertIn("01AOPACKET", file_id)
        # The latest confirmation of the bytes holds from its time, never earlier and never
        # the one it superseded; other bytes and a mail attachment never reach the file.
        self.assertEqual((find("BATCH_RECORD"), find("COA")), ([], []))
        self.assertEqual([find(c, as_known_at=self.confirmed_at[0])
                          for c in ("BATCH_RECORD", "FILL_RECORD")], [[], []])
        self.assertEqual(find("FILL_RECORD", as_known_at=self.confirmed_at[1]), [file_id])
        # Permission-first: a reader the file's permissions withhold finds nothing.
        self.assertEqual(find("FILL_RECORD", NO_FILES), [])
        out = self.ok("plx.object", tools.call("plx.object", {"object_id": file_id}, READER))
        values = {v["property"]: v for v in out["values"]}
        self.assertEqual(values["document_category"]["citation"]["lineage"], "workbench")
        self.assertEqual(values["document_category"]["citation"]["l1_handle"],
                         self.captured["l1_handle"])
        proposed = json.loads(values["proposed_portal_metadata"]["value"]["untrusted_text"])
        self.assertEqual((proposed["status"], proposed["written_to_sources"],
                          proposed["columns"]["DocumentCategory"], proposed["document_kind"]),
                         ("proposed", False, "FILL_RECORD", "ao_scan"))
        # The host's incremental build writes them again over a reused generation: the
        # same store as a cold build, generation after generation.
        from ontology.project_incremental import build_generation
        with MirrorCaptures(self.product.mirror_path) as fm, \
                SharePointCaptures(self.case.state) as files, \
                tempfile.TemporaryDirectory() as gens:
            built = [build_generation(Path(gens), load_schema(),
                                      {"portal_api": fm, "sharepoint": files},
                                      post_project=self.steps) for _ in range(2)]
        self.assertEqual([(g.mode, g.content_digest) for g in built],
                         [("cold", self.report["content_digest"]),
                          ("incremental", self.report["content_digest"])])

    def file_version(self):
        conn = sqlite3.connect(self.store)
        try:
            (row,) = conn.execute(
                "SELECT o.object_id, v.l1_handle, v.digest FROM objects o JOIN object_versions "
                "v ON v.object_id = o.object_id WHERE o.object_type = 'SharePointFile' AND "
                "o.object_id LIKE '%01AOPACKET%' AND v.superseded_at IS NULL")
        finally:
            conn.close()
        return row

    def confirmation(self, original, sha, at, category):
        """A ``plx.document_fields`` confirmation record of the packet's drive item."""
        return {"schema": kind_confirmation.SCHEMA,
                "id": "kc-rec-" + sha256((original + at).encode()).hexdigest()[:24],
                "tenant": "plx", "original_id": original, "citation": f"original://{original}",
                "original_sha256": sha, "provenance": {
                    "source": "sharepoint-drive", "drive_id": tsp.DRIVE,
                    "item_id": "01AOPACKET", "etag": self.captured["etag"]},
                "confirmed_fields": {"DocumentCategory": category, "DocumentType": "Quality"},
                "document_kind": "ao_scan", "amended_columns": [],
                "confirmed_by": "employee:00000000-0000-0000-0000-00000000000a",
                "confirmed_at": at, "written_to_sources": False}

    def test_a_superseded_later_confirmation_never_hides_a_live_earlier_one(self):
        # Decision 2026-10-08 item 1: one original confirmed the packet's bytes (live); a
        # later confirmation of the same bytes through another original was superseded
        # when that original was confirmed again with other bytes. The live one projects.
        digest = sha256(self.bytes).hexdigest()
        recorded = dt.datetime.fromisoformat(self.captured["recorded_at"].replace("Z", "+00:00"))
        t0, t1, t2 = ((recorded + dt.timedelta(milliseconds=n)).isoformat() for n in (1, 2, 3))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        kinds = Path(temp.name) / "kind-confirmations.sqlite"
        store = kind_confirmation.KindConfirmations(kinds)
        with closing(store._db()) as db:
            for record in (self.confirmation("orig-" + "7" * 32, digest, t0, "BATCH_RECORD"),
                           self.confirmation("orig-" + "8" * 32, digest, t1, "COA"),
                           self.confirmation("orig-" + "8" * 32, "0" * 64, t2, "SDS")):
                store._append(db, record)
        steps = dict(self.steps, confirmations=kind_confirmation.projection_step(kinds, "plx"))
        path = Path(temp.name) / "ontology.sqlite"
        with MirrorCaptures(self.product.mirror_path) as fm, \
                SharePointCaptures(self.case.state) as files:
            report = build_store(path, load_schema(), {"portal_api": fm, "sharepoint": files},
                                 clock=lambda: dt.datetime(2026, 10, 4, 13, 0,
                                                           tzinfo=dt.timezone.utc),
                                 post_project=steps)
        counts = report["post_project"]["confirmations"]["counts"]
        self.assertEqual((counts["projected"], counts["superseded"], counts["not_projected"]),
                         (1, 1, 1))
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        tools = ot.OntologyTools(reader, ot.ReadPolicy(tenant="plx", audiences={
            key: set(PRINCIPALS) for key in reader.audience_keys()}, markings={
            m: set(PRINCIPALS) for m in ot.store_ddl.MARKINGS}), item_gate=self.gate,
            schema=ot.default_schema(), grammar=None, contracts=CONTRACTS)
        found = {category: [c["object"]["object_id"] for c in self.ok("plx.find", tools.call(
            "plx.find", {"object_type": "SharePointFile", "filters": [
                {"property": "document_category", "op": "eq", "value": category}]},
            READER)).get("candidates", ())] for category in ("BATCH_RECORD", "COA")}
        self.assertEqual(found, {"BATCH_RECORD": [self.file_version()[0]], "COA": []})

    def test_document_types_recorded_before_stay_listed_and_only_document_fields_records(self):
        from operating.employee_permissions import EmployeePrincipal
        from operating.session_identity import (CHANNEL_AUTHOR, CHANNEL_CONSOLE,
                                                PROOF_CONFIGURED, SessionIdentity)

        oid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        employee = EmployeePrincipal("0f0f0f0f-0000-4000-8000-000000000001", oid)
        session = SessionIdentity(tenant="plx", channel=CHANNEL_CONSOLE,
                                  author_kind=CHANNEL_AUTHOR[CHANNEL_CONSOLE],
                                  proof=PROOF_CONFIGURED, employee=employee,
                                  principals=frozenset({employee.principal}),
                                  identities=frozenset({oid}))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name)
        review_store = reviews.ReviewStore(reviews.store_path(state))
        file_id, handle, digest = self.file_version()
        cite = {"object_id": file_id, "l1_handle": handle, "digest": digest}
        # A document type the review store held before kinds moved (as plx.review wrote it).
        legacy = "rev-" + "e" * 32
        with closing(sqlite3.connect(reviews.store_path(state))) as db, db:
            db.execute("INSERT INTO reviews (review_id, tenant, review_key, decision, subject, "
                       "document_kind, evidence, author, recorded_at) VALUES "
                       "(?, 'plx', ?, 'document_kind', ?, 'QUOTE', ?, ?, ?)",
                       (legacy, reviews.review_key("document_kind", file_id), file_id,
                        json.dumps([cite]), json.dumps({"principal": employee.principal,
                                                        "channel": "console",
                                                        "session": "5" * 64}),
                        self.captured["recorded_at"]))
        with self.assertRaises(reviews.ReviewError):
            review_store.record(tenant="plx", author={"principal": employee.principal},
                                decision="document_kind", subject=file_id, evidence=[cite],
                                document_kind="QUOTE")
        kinds = kind_confirmation.KindConfirmations(kind_confirmation.store_path(state))
        review = rt.ReviewTools(self.tools(), review_store, session=session,
                                gate=lambda _: READER, kinds=kinds)

        def status():
            out = self.ok("plx.review", review.call("plx.review", {"mode": "status",
                                                                   "subject": file_id},
                                                    session.actor))
            return [(r["review_id"], r.get("document_kind"), r["state"],
                     r.get("state_reason")) for r in out["reviews"]]

        # plx.document_fields is the one path that records what a document is.
        refused = review.call("plx.review", {
            "mode": "record", "decision": "document_kind", "subject": file_id,
            "document_kind": "INVOICE", "evidence": [{"l1_handle": handle, "digest": digest}]},
            session.actor)
        self.assertEqual(validate(refused, CONTRACTS["tools"]["plx.review"]["output"],
                                  root=CONTRACTS), [])
        self.assertEqual(refused["error"]["code"], "document_kind_via_document_fields")
        self.assertIn("plx.document_fields", refused["error"]["hint"])
        # The earlier one stays listed, read from the kind store it is imported into.
        self.assertEqual(status(), [(legacy, "QUOTE", "effective", None)])
        self.assertEqual([r["id"] for r in kinds.history("plx", file_id)], [legacy])
        # A later confirmation of the same bytes through plx.document_fields: the earlier
        # document type no longer holds (the build projects the later one).
        later = (dt.datetime.fromisoformat(self.captured["recorded_at"].replace("Z", "+00:00"))
                 + dt.timedelta(seconds=1)).isoformat()
        with closing(kinds._db()) as db:
            kinds._append(db, self.confirmation("orig-" + "9" * 32, sha256(self.bytes)
                                                .hexdigest(), later, "FILL_RECORD"))
        self.assertEqual(status(), [(legacy, "QUOTE", "superseded", "later_review")])

    def test_the_assembly_order_shows_its_packet_and_every_fm_disagreement(self):
        tools = self.tools()
        traverse = tt.TraverseTools(tools)
        step = {"from": AO, "path": [{"link_type": "AssemblyOrderPacket.packet_of",
                                      "direction": "reverse"}]}
        self.assertEqual(self.ok("plx.around", traverse.call("plx.around", step, READER))
                         ["objects"], [])   # a lead is never traversed by default
        found = self.ok("plx.around", traverse.call("plx.around", {**step, "tier_floor": "lead"},
                                                    READER))
        self.assertEqual([o["object_id"] for o in found["objects"]], [self.packet()])
        out = self.ok("plx.object", tools.call("plx.object", {"object_id": AO}, READER))
        conflicts = {c["subject"]: c for c in out["conflicts"]}
        P = "AssemblyOrderPacket"
        self.assertEqual(set(conflicts), {
            f"AssemblyOrderDamage.damaged_quantity vs {P}.damaged_by_component P-01274",
            f"AssemblyOrder.manufactured vs {P}.production_date",
            f"SalesOrder.required_date vs {P}.required_date",
            f"SalesOrder.customer_po vs {P}.customer_po"})
        box = conflicts[f"AssemblyOrderDamage.damaged_quantity vs {P}.damaged_by_component "
                        "P-01274"]
        self.assertEqual([(v["value"], v["quantity_role"]) for v in box["values"]],
                         [("11", "damaged"), ("20", "damaged")])
        self.assertEqual(([v.get("read_by") for v in box["values"]], box["tier"]),
                         (["ocr", None], "lead"))   # the paper's reader; FileMaker is no read
        self.assertTrue(box["values"][0]["citation"]["l1_handle"].startswith("sharepoint://"))
        self.assertTrue(box["values"][1]["citation"]["l1_handle"].startswith("api://"))
        dates = conflicts[f"AssemblyOrder.manufactured vs {P}.production_date"]["values"]
        self.assertEqual([v["value"] for v in dates], ["2026-01-07", "2026-01-05"])
        required = conflicts[f"SalesOrder.required_date vs {P}.required_date"]
        self.assertEqual([v["value"] for v in required["values"]], [None, "2026-01-01"])
        self.assertEqual(required["note"], attested_conflicts.BLANK_NOTE)
        # The same disagreements from the packet's side; none for a reader the file's
        # permissions withhold it from.
        packet = self.ok("plx.object", tools.call("plx.object", {"object_id": self.packet()},
                                                  READER))
        self.assertEqual({c["subject"] for c in packet["conflicts"]}, set(conflicts))
        hidden = self.ok("plx.object", tools.call("plx.object", {"object_id": AO}, NO_FILES))
        self.assertEqual(hidden["conflicts"], [])

    def test_process_places_the_ao_and_its_packet_with_due_dates_and_conflicts(self):
        from operating import ontology_tools_process as op
        at = dt.datetime(2026, 10, 4, 13, 0, tzinfo=dt.timezone.utc)
        process = op.ProcessTools(self.tools(family=True, clock=lambda: at))
        ao = self.ok("plx.process", process.call("plx.process", {"from": AO}, READER))
        self.assertEqual(ao["process"], "manufacturing_order")
        st = {s["stage"]: s for s in ao["stages"]}
        self.assertEqual((st["filling"]["state"], st["assembly_realized"]["state"],
                          st["sales_order"]["state"]), ("present", "present", "present"))
        self.assertTrue(st["filling"]["citations"][0]["l1_handle"].startswith("api://"))
        # FM order 351 has no shipment link anywhere: unshipped is not a fact
        self.assertEqual(st["shipment"]["missing"]["reason"], "absence_unproven")
        due = {d["stage"]: d for d in ao["due"]}
        # FM's required date is the only one recorded: cited, never chosen among others
        self.assertEqual([v["value"] for v in due["shipment"]["due_by"]], ["2026-01-01"])
        # FM says late against 2026-01-01, the paper leaves the date blank: lateness is an
        # evidence conflict naming both sources, never "overdue by N days"
        P = "AssemblyOrderPacket"
        self.assertIsNone(due["shipment"]["overdue"])
        self.assertNotIn("days_overdue", due["shipment"])
        self.assertEqual((due["shipment"]["gap"]["reason"], due["shipment"]["gap"]["subject"]),
                         ("due_date_conflict", f"SalesOrder.required_date vs {P}.required_date"))
        self.assertTrue({f"AssemblyOrder.manufactured vs {P}.production_date",
                         f"SalesOrder.required_date vs {P}.required_date"}
                        <= {c["subject"] for c in ao["conflicts"]})
        packet = self.ok("plx.process", process.call("plx.process", {"from": self.packet()},
                                                     READER))
        self.assertEqual(packet["process"], "ao_packet")
        st = {s["stage"]: s for s in packet["stages"]}
        self.assertEqual({k: v["state"] for k, v in st.items()},
                         dict.fromkeys(("ao_issued", "production_recorded", "qc_checked",
                                        "fg_released"), "present"))
        self.assertTrue(st["fg_released"]["citations"][0]["l1_handle"].startswith(
            "sharepoint://"))
        self.assertEqual((packet["current_stage"], packet["next_stage"]), (None, None))
        # released is never overdue; the blank REQUIRED DATE is not read as a date
        (fg,) = packet["due"]
        self.assertEqual((fg["stage"], fg["overdue"]), ("fg_released", False))
        self.assertNotIn("due_by", fg)
        self.assertEqual({c["subject"] for c in packet["conflicts"]},
                         {c["subject"] for c in ao["conflicts"]})

    def item(self, path):
        """An exposed item whose byte pin names the fixture scan, which stands in for the
        pinned original (isolated-fixture, never a live claim)."""
        text = path.read_text("utf-8")
        real = json.loads(text)["as_known_at"]["l1_generations"]["curated_original_bytes"]
        return json.loads(text.replace(real, sha256(self.bytes).hexdigest()))

    def host(self):
        case_host = runner.CaseHost(self.product.state_dir, self.product.audience, "plx",
                                    PRINCIPALS)
        table = bl.ontology_tool_table(self.store, tenant="plx", principals=PRINCIPALS,
                                       item_gate=self.gate)
        return bl.ToolHost(case_host, table, "plx", PRINCIPALS,
                           documents=document_fields.capture_citation_reader(
                               self.files, READER, audience()))

    def test_the_exposed_item_scores_through_the_general_path(self):
        item = self.item(ITEM)
        lookup = {(rel, str(row["id"])): row for rel, rows in FM.items() for row in rows}

        def oracle(relation, key, field_path):
            row = lookup.get((relation, str(key.get("id"))))
            return (False, None) if row is None else runner._field(row, field_path)

        with runner.MirrorPinReader(self.product.mirror_path) as reader:
            pin = runner.make_run_pin(item, reader, oracle_field=oracle)
        self.assertEqual(pin["status"], "pinned", pin)
        row = bl.run_item(item, bl.DeterministicInvestigator(), self.host(), run_pin=pin,
                          evidence_label="isolated-fixture")
        score = row["score"]
        self.assertEqual((row["outcome"], row["root"]), ("pass", "found"),
                         json.dumps({k: score[k] for k in ("codes", "values", "gaps",
                                                           "claim_checks")})[:3000])
        self.assertTrue(all(value["passed"] for value in score["values"]))
        answer = row["transcript"]["answer"]
        # FileMaker damage stays per component; the paper's box count is the open
        # conflict (a gap), and no total is stated anywhere.
        damaged = sorted(v["value"] for v in answer["values"] if v["quantity_role"] == "damaged")
        self.assertEqual(damaged, ["20", "3", "7"])
        self.assertNotIn("30", answer["text"])
        read = row["transcript"]["evidence"]
        packet_reads = [r for h, r in read.items() if h.startswith("sharepoint://")]
        self.assertTrue(packet_reads and all(r["sha256"] == sha256(self.bytes).hexdigest()
                                             for r in packet_reads))
        # The same answer citing the original's
        # shop-floor page instead of the projected fields. The gate checks a unit against the
        # one the page prints after the figure ("Filled qty: 1502 EA") and leaves it
        # unverified where none is printed ("QC retain: 1"), for the item's unit to judge.
        from evals import score as sc
        page = "original://orig-" + "a" * 32 + "#page=2"
        transcript = json.loads(json.dumps(row["transcript"]))
        transcript["evidence"][page] = {"ok": True, "sha256": sha256(self.bytes).hexdigest(),
                                        "page": 2, "text": "\n".join(PAGES[1]),
                                        "text_source": "ocr"}
        paper = {v["quantity_role"]: v for v in transcript["answer"]["values"]
                 if v["quantity_role"] in ("filled", "retained")}
        self.assertEqual(sorted((v["value"], v["unit"].upper()) for v in paper.values()),
                         [("1", "EA"), ("1502", "EA")])
        for value in paper.values():
            value["citations"] = [{"l1_handle": page, "field_path": "text"}]
        on_page = sc.score(item, transcript)
        self.assertTrue(on_page["passed"], json.dumps(on_page["values"]))

        def failing(role, **change):
            changed = json.loads(json.dumps(transcript))
            next(v for v in changed["answer"]["values"]
                 if v["quantity_role"] == role).update(change)
            result = sc.score(item, changed)
            self.assertFalse(result["passed"])
            return [v["codes"] for v in result["values"] if v["codes"]]

        # kg against the printed EA: the gate refuses it and the item's unit too.
        self.assertEqual(failing("filled", unit="kg"),
                         [["unit_mismatch", "claim_check_failed"]])
        # kg on the bare retain count: the gate leaves it unverified, the item fails it.
        self.assertEqual(failing("retained", unit="kg"), [["unit_mismatch"]])
        # A value the page does not hold.
        self.assertEqual(failing("filled", value="1503"),
                         [["value_mismatch", "claim_check_failed"]])

    def test_the_dots_p8_item_credits_packet_citations_to_its_pinned_original(self):
        # dev-F7-1002-exposed (the P8 bundle) expects the paper fields by the
        # original's byte pin: an answer citing the projected packet scores against it.
        item = self.item(ROOT / "evals/exposed/dev-F7-1002-exposed.json")
        names = {"ao_lot": "ao_lot", "bulk_lot": "bulk_lot",
                 "planned_quantity": "planned_quantity",
                 "filling_row_quantity": "filled_quantity",
                 "shop_floor_samples": "retained_samples"}
        packet = self.packet()

        def script(prompt, call):
            read = {v["property"]: v for v in call("plx.object", {"object_id": packet})["values"]}
            values = []
            for name, prop in names.items():
                value = read[prop]
                text = value["value"]["untrusted_text"] if isinstance(value["value"], dict) \
                    else value["value"]
                values.append({"name": name, "value": text, **(
                    {"unit": "EA", "quantity_role": value["quantity_role"]}
                    if value.get("quantity_role") else {}),
                    "citations": [{"l1_handle": value["citation"]["l1_handle"],
                                   "field_path": value["citation"]["field_path"]}]})
            return "```json\n" + json.dumps({"format": "plx-eval-answer/v1",
                                              "answerability": "partial", "text": "",
                                              "values": values, "gaps": []}) + "\n```"

        row = bl.run_item(item, runner.ScriptedAgent(script), self.host(), run_pin=None,
                          evidence_label="isolated-fixture")
        results = {v["name"]: v for v in row["score"]["values"]}
        self.assertEqual({name: results[name]["codes"] for name in names},
                         {name: [] for name in names})
        self.assertFalse(row["score"]["passed"])   # the rest of its 26 fields is unanswered

    def test_the_dots_bundle_scores_per_field_against_the_runtime_reader(self):
        from evals import document_fields as scoring
        # The bundle as committed: its originals are not in this keyless run, so every
        # one of its printed fields is blocked, never guessed.
        bundle = [i for i in runner.load_items(ROOT / "evals/exposed", split="dev")
                  if i["id"] in ("dev-F7-1001-exposed", "dev-F7-1002-exposed",
                                 "dev-F9-1001-exposed", "dev-F9-1002-exposed")]
        report = scoring.score_items(bundle, scoring.sharepoint_originals(self.files))
        self.assertEqual(len(report["items"]), 4)
        self.assertEqual(report["counts"], {"correct": 0, "wrong": 0, "abstain": 0,
                                            "blocked": 60})
        # The packet item with the fixture scan standing in for its pinned original: read
        # from the capture's OCR text, field by field.
        item = self.item(ROOT / "evals/exposed/dev-F7-1002-exposed.json")
        (scored,) = scoring.score_items([item], scoring.sharepoint_originals(self.files))["items"]
        outcomes = {f["name"]: f["outcome"] for f in scored["fields"]}
        correct = {"ao_lot", "bulk_lot", "planned_quantity", "filling_row_quantity",
                   "shop_floor_samples", "rejects"}
        self.assertEqual({n for n, o in outcomes.items() if o == "correct"}, correct)
        self.assertEqual({o for n, o in outcomes.items() if n not in correct}, {"abstain"})
        self.assertEqual({f["read_by"] for f in scored["fields"] if "read_by" in f}, {"ocr"})
        # A value the reader states differently is wrong, never correct.
        changed = json.loads(json.dumps(item))
        next(v for v in changed["known_outcome"]["values"]
             if v["name"] == "planned_quantity")["value"] = "1503"
        (scored,) = scoring.score_items([changed],
                                        scoring.sharepoint_originals(self.files))["items"]
        self.assertEqual([(f["outcome"], f["reason"]) for f in scored["fields"]
                          if f["name"] == "planned_quantity"], [("wrong", "value_differs")])


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "packets are PDFs: needs pypdf and reportlab")
class DocumentClassLiveCheckTests(unittest.TestCase):
    """``scripts.eval_document_fields_live`` (``operating.sharepoint_host_check
    --document-class`` plus the exposed items scored over its capture: the
    ontology.document_fields_sharepoint live check) over the in-process FakeDrive."""

    def test_the_check_counts_the_class_and_resolves_every_citation_leaking_nothing(self):
        import argparse
        from verify import live_verify as lv
        from ontology.tests.test_document_fields import pdf
        from operating import sharepoint_host_check as check
        from scripts import eval_document_fields_live as live
        drive = tsp.seeded_drive()
        packet = pdf(PAGES)
        drive.file("01AOPACKET", "AO-26005B.pdf", "01AOS", packet)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            scope_file = Path(tmp) / "scope.json"
            scope_file.write_text(json.dumps({"schema": cs.SCOPE_SCHEMA, "targets": [
                {"id": "quality", "drive_id": tsp.DRIVE, "folders": [tsp.QUALITY_FOLDER]}]}))
            audience_file = Path(tmp) / "audience.json"
            audience_file.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {"sharepoint:*": [cs.CAPTURE_PRINCIPAL]}}))
            report = check.run(argparse.Namespace(
                state_dir=str(state), scope=str(scope_file),
                live_audience=str(audience_file), principal=cs.CAPTURE_PRINCIPAL,
                document_class="ao_packet", min_documents=1),
                environ=tsp.ENV, sender=drive, sleep=lambda s: None,
                evaluate=live.evaluate)
            # Accuracy counts operator-audited items only, and only over originals it holds.
            bundle = ROOT / "evals/exposed/dev-F7-1002-exposed.json"
            oracle = json.loads(bundle.read_text("utf-8").replace(
                json.loads(bundle.read_text("utf-8"))["as_known_at"]["l1_generations"][
                    "curated_original_bytes"], sha256(packet).hexdigest()))
            audited = dict(oracle, label={"status": "human_audited", "audited_by": "operator",
                                          "audited_at": "2026-10-07"})
            store = cs.SharePointCaptureStore.open_read_only(state)
            try:
                self.assertEqual(live.accuracy(store, [oracle]),
                                 {"status": "no_operator_items", "items": 0})
                measured = live.accuracy(store, [oracle, audited])
                missing = live.accuracy(store, [json.loads(bundle.read_text("utf-8")) | {
                    "label": audited["label"]}])
            finally:
                store.close()
        self.assertTrue(report["ok"], report)
        self.assertEqual((report["evidence_label"], report["documents"],
                          report["keyed_entries"]), ("injected-transport", 1, 3))
        self.assertGreater(report["citations"], 10)
        self.assertEqual(report["citations_resolved"], report["citations"])
        self.assertEqual((report["gaps"]["value_missing"], report["gaps"]["unreadable"],
                          report["gaps"]["unchecked"]), (2, 1, 1))
        # Per field and role, every document lands in exactly one bucket: the blank PO and
        # box are blank, "150?" unreadable, the inspected count the packet never names
        # label_absent; produced and inspected stay apart.
        outcomes = report["field_outcomes"]
        for row in outcomes.values():
            self.assertEqual(sum(row[b] for b in live.BUCKETS), row["documents"])
        self.assertEqual({name: next(b for b in live.BUCKETS if outcomes[name][b]) for name in (
            "customer_po", "shipment_on_hold", "shipment_released", "produced_quantity",
            "inspected_quantity", "planned_quantity", "damaged")},
            {"customer_po": "blank", "shipment_on_hold": "blank",
             "shipment_released": "extracted", "produced_quantity": "unreadable",
             "inspected_quantity": "label_absent", "planned_quantity": "extracted",
             "damaged": "extracted"})
        self.assertEqual(outcomes["damaged"]["entries"]["extracted"], 3)
        # An ambiguous field names why: its gap reasons.
        self.assertEqual(live.denominators({"documents": 2, "document_class": "ao_packet",
                                            "fields": {"ao_lot": {"conflicting_values": 1,
                                                                  "stated": 1}}}
                                           )["field_outcomes"]["ao_lot"]["ambiguous_by"],
                         {"conflicting_values": 1})
        roles = report["role_outcomes"]
        self.assertEqual({r: next(b for b in live.BUCKETS if roles[r][b]) for r in (
            "planned", "produced", "inspected", "accepted", "damaged")},
            {"planned": "extracted", "produced": "unreadable", "inspected": "label_absent",
             "accepted": "label_absent", "damaged": "extracted"})
        # Produced reads "150?": unreadable on the paper, reported per role, never a reader
        # limit; a seen label the reader left unread is one, counted by why.
        self.assertEqual((report["role_documents"]["produced"]["unreadable"],
                          report["roles_unread"]), (1, 0))
        silent = {spec["name"]: {"not_stated": 1} for spec in next(
            p for p in document_fields.profiles()["profiles"] if p["id"] == "ao_packet")["fields"]}
        named = live.denominators({"documents": 1, "document_class": "ao_packet", "fields": {
            **silent, "inspected_quantity": {"label_seen_unread": 1,
                                             "unread_by": {"row_unaligned": 1}}}})
        self.assertEqual((named["role_outcomes"]["inspected"]["label_seen_unread"],
                          named["role_outcomes"]["inspected"]["unread_by"],
                          named["roles_unread"]), (1, {"row_unaligned": 1}, 1))
        # Per document: a role one packet read is still unread on another packet naming
        # it; a report of two packets that does not tell them apart has it unknown.
        self.assertEqual(sum(row["documents"] for row in report["document_outcomes"]),
                         report["documents"])
        each = {name: "not_stated" for name in silent}
        two = {"documents": 2, "document_class": "ao_packet", "fields": {
            **{name: {"not_stated": 2} for name in silent},
            "produced_quantity": {"stated": 1, "label_seen_unread": 1}}, "document_outcomes": [
                {"documents": 1, "fields": {**each, "produced_quantity": "stated"}},
                {"documents": 1, "fields": {**each, "produced_quantity": "label_seen_unread"}}]}
        self.assertEqual(live.denominators(two)["roles_unread"], 1)
        del two["document_outcomes"]
        self.assertIsNone(live.denominators(two)["roles_unread"])
        # No operator-interpreted item exists yet: accuracy is not claimed. The bundle's
        # originals are not in this capture: scored as blocked, counts only.
        self.assertEqual(report["accuracy"], {"status": "no_operator_items", "items": 0})
        self.assertEqual((report["exposed_items"], report["exposed_fields"]["blocked"]), (0, 60))
        self.assertEqual((measured["status"], measured["items"]), ("measured", 1))
        self.assertGreater(measured["fields"]["correct"], 0)
        self.assertEqual((missing["status"], missing["items"]), ("originals_not_captured", 0))
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        text = json.dumps(report)
        for secret in ("AO-26005B", "BC-26007E", "1502", "P-01274", tsp.DRIVE, "s3cr3t"):
            self.assertNotIn(secret, text)
        spec = json.loads((ROOT / "devloop/live-checks/ontology.document_fields_sharepoint.json")
                          .read_text(encoding="utf-8"))
        lv.validate_spec(spec, stem="ontology.document_fields_sharepoint")
        # The live expectation adds OCR (a text-layer fixture has none): everything else
        # holds; a role left unread by the reader fails it.
        failed = lv.check_expectations(spec, 0, report)
        self.assertTrue(failed and all("ocr" in str(problem) for problem in failed), failed)
        unread = lv.check_expectations(spec, 0, {**report, "roles_unread": 1})
        self.assertTrue(any("roles_unread" in str(problem) for problem in unread), unread)
        # A budget-cut capture is not a pass, whatever it measured.
        self.assertTrue(report["capture_complete"])
        cut = lv.check_expectations(spec, 0, {**report, "capture_complete": False})
        self.assertTrue(any("capture_complete" in str(problem) for problem in cut), cut)


if __name__ == "__main__":
    unittest.main()
