"""Portal-taxonomy metadata proposals: mirrored rules, cited evidence, no writes.

Fixtures are Portal-shaped (column names, choice values, AO/SO/PO/lot record
attributes) and isolated: no Graph, SharePoint, Portal or BC connection.
Ricardo's AO packet values are the paper transcripts in
docs/reference/cases-and-artifacts.md §3.3 (OCR-flagged here, as the
scans have no text layer).
"""

from __future__ import annotations

import ast
import copy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import unittest
from unittest import mock
from urllib.parse import unquote

from integrations.document_understanding import extract_document
from ontology.types import LEVELS, Actor, EntityRef
from operating import doc_metadata, doc_metadata_taxonomy as taxonomy
from operating.doc_metadata import build_proposals


TENANT = "isolated-fixture:doc-metadata"
AT = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
QA = Actor(TENANT, frozenset({"staff:quality"}), "restricted")
OTHER = Actor(TENANT, frozenset({"staff:finance"}), "restricted")
SITE = "https://petrasoap.sharepoint.com/sites/QCQA/Shared%20Documents/AOs/2026%20-%20AOs/"

# AO · product ERP code · OE (None = printed blank) · customer · bulk lot.
RICARDO_AOS = (
    ("AO-26005B", "DVS-5003-A", "OR1000151885", "Diversified", "BC-26007E"),
    ("AO-26005C", "DVS-5004-A", "OR1000151885", "Diversified", "BC-26005D"),
    ("AO-26005E", "MIA001", "OR100150351", "United Scents", "BC-26005F"),
    ("AO-26006C", "MIA001", "OR100150351", "United Scents", "BC-26005G"),
    ("AO-26007D", "MIA001", "OR100150351", "United Scents", "BC-26006C"),
    ("AO-26013C", "PCC001", None, "PETRA Hygienic Systems", "BC-26012H"),
)


def ao_packet(ao, erp, oe, customer, bulk):
    """Page 1, page 3 and page 11 headings of the 12-page AO packet."""
    pages = [
        f"ASSEMBLY ORDER MO\nForm #072B\nAO LOT#: {ao} BULK LOT#: {bulk}\n"
        f"ERP CODE: {erp} OE:{' ' + oe if oe else ''}\n"
        f"CUSTOMER: {customer} PO: REQUIRED DATE:\n",
        "ASSEMBLY ORDER - SHOP FLOOR\nForm #072B\nDAMAGES P-0121 07\n",
        f"FINISHED GOODS INSPECTION / RELEASE SHEET\nForm #010\nshipment released\n",
    ]
    text, rows = "", []
    for number, page in zip((1, 3, 11), pages):
        rows.append({"page": number, "start": len(text), "end": len(text) + len(page)})
        text += page + "\n"
    return text, rows


def attributes(text, title, url, *, ocr=False, rows=None):
    extracted = extract_document(text.encode(), "text/plain", original_url=url)
    attrs = extracted.evidence_attributes(url, title=title)
    if ocr:
        attrs["extraction"]["warnings"] = ["ocr_text_requires_identity_review"]
    if rows:
        attrs["extraction"]["pages"] = rows
    return attrs


def columns_snapshot(native, revision, fields):
    drive, item = native.split(":", 1)
    return {"source": "sharepoint-listitem-fields/v1", "site_id": "site-guid",
            "list_id": "list-guid", "list_item_id": "7", "drive_etag": revision,
            "source_api": f"https://graph.microsoft.com/v1.0/drives/{drive}/items/"
                          f"{item}/listItem?expand=fields", "fields": fields}


def by_column(result):
    return {row["column"]: row for row in result["columns"]}


def by_link(result):
    return {row["link"]: row for row in result["linked_records"]}


def proposals_for(fixture, actor, doc_id, *, current_columns=None, model_suggestions=()):
    """``build_proposals`` for one fixture document, linking printed numbers by exact value.

    The case host calls ``build_proposals`` with a ``plx.find``-backed lookup
    (``operating.case_context_tools.document_metadata``). This helper is the same
    call over the fixture records: records are matched among those ``actor`` can see.
    """
    source, kind, native = (unquote(part) for part in doc_id.split("::"))
    row = fixture.get(EntityRef(source, kind, native), actor)
    if row is None:
        raise KeyError(doc_id)

    def lookup(entity_type, field, value):
        found = [item for ref in sorted(fixture.records) if ref.entity_type == entity_type
                 for item in [fixture.get(ref, actor)]
                 if item is not None and item["attributes"].get(field) == value]
        return found[:10]

    # A stable digest and the original link; build_proposals echoes both.
    attrs = row["attributes"]
    return build_proposals(document_id=doc_id, revision=row["revision"],
        fingerprint=sha256(attrs["text"].encode()).hexdigest(),
        attrs=attrs, locator=attrs["original_url"],
        lookup=lookup, model_suggestions=model_suggestions, current_columns=current_columns,
        source=row["ref"]["source"], native_id=row["ref"]["native_id"])


class Fixture:
    """Current source records (one revision per record), read through tenant,
    clearance and ACL as an employee read is."""

    def __init__(self, tenant=TENANT):
        self.tenant = tenant
        self.records = {}

    def get(self, ref, actor):
        row = self.records.get(ref)
        if (row is None or row["tenant"] != actor.tenant or
                LEVELS[row["classification"]] > LEVELS[actor.clearance] or
                not ("*" in row["acl"] or set(row["acl"]) & actor.principals)):
            return None
        return copy.deepcopy(row)

    def observe(self, ref, revision, attrs, acl, provenance, at=AT):
        self.records[ref] = {"ref": ref.as_dict(), "tenant": self.tenant, "revision": revision,
                             "observed_at": at.isoformat(), "attributes": dict(attrs),
                             "acl": sorted(acl), "classification": "restricted",
                             "provenance": provenance, "authority": ref.source}
        return ref.entity_id

    def record(self, kind, native, attrs, acl=("staff:quality",), source="portal-fm-replica",
               revision="r1", at=AT):
        return self.observe(EntityRef(source, kind, native), revision, attrs, acl,
                            f"isolated-fixture:{kind}:{native}", at)

    def document(self, native, attrs, revision="etag-1"):
        attrs = {"source_container": native.split(":", 1)[0], **attrs,
                 "source_revision": revision}
        return self.observe(EntityRef("sharepoint-drive", "SourceDocument", native), revision,
                            attrs, {"staff:quality"}, attrs["original_url"])


class RicardoAoPacketTest(unittest.TestCase):
    def test_each_ao_pdf_maps_to_ao_sales_order_and_lot_with_cited_evidence(self):
        fixture = Fixture()
        records = {}
        for index, (ao, _erp, oe, customer, bulk) in enumerate(RICARDO_AOS):
            records[ao] = fixture.record("AssemblyOrder", str(10718 + index), {"mo_code": ao})
            records["lot:" + ao] = fixture.record("InventoryLot", f"lot-{index}",
                {"lotNumber": ao}, source="portal-gold")
        records["OR1000151885"] = fixture.record("SalesOrder", "351",
            {"erpCode": "OR1000151885", "poReference": "PO12172025"})
        records["OR100150351"] = fixture.record("SalesOrder", "360", {"erpCode": "OR100150351"})
        for ao, erp, oe, customer, bulk in RICARDO_AOS:
            with self.subTest(ao=ao):
                text, rows = ao_packet(ao, erp, oe, customer, bulk)
                doc_id = fixture.document(f"qcqa-drive:{ao}", attributes(
                    text, f"{ao}.pdf", SITE + ao + ".pdf", ocr=True, rows=rows))
                result = proposals_for(fixture, QA, doc_id)
                self.assertEqual(result["anchor"]["entity_type"], "ASSEMBLY_ORDER")
                self.assertTrue(result["anchor"]["candidates"][0]["source"].startswith(
                    "portal/src/app/mrp/assembly/[id]/assembly-detail.tsx@8da33e4"))
                columns, links = by_column(result), by_link(result)
                # buildAttachmentMetadata("ASSEMBLY_ORDER", category, {ref: mo_code}).
                self.assertEqual((columns["PartyType"]["value"], columns["DocStatus"]["value"],
                                  columns["Source"]["value"]),
                                 ("Internal", "Pending Review", "Staff"))
                self.assertEqual(columns["PartyType"]["basis"], "portal_rule")
                self.assertEqual(columns["ItemRef"]["value"], ao)
                self.assertEqual(columns["PartyRef"]["status"], "not_applicable")
                self.assertEqual(columns["CustomerRef"]["status"], "not_applicable")
                ref_evidence = columns["ItemRef"]["evidence"][0]
                self.assertEqual(text[ref_evidence["span"]["start"]:ref_evidence["span"]["end"]], ao)
                self.assertEqual(ref_evidence["page"], 1)
                self.assertEqual(ref_evidence["reading"], "ocr")
                self.assertTrue(columns["ItemRef"]["requires_review"])
                # The packet holds both a shop-floor fill record and a QC
                # release sheet: two Portal categories, so none is chosen.
                self.assertEqual(columns["DocumentCategory"]["status"], "conflict")
                self.assertIsNone(columns["DocumentCategory"]["value"])
                self.assertEqual({c["value"] for c in columns["DocumentCategory"]["candidates"]},
                                 {"FILL_RECORD", "QC_SIGNOFF"})
                self.assertEqual({e["page"] for c in columns["DocumentCategory"]["candidates"]
                                  for e in c["evidence"]}, {3, 11})
                self.assertEqual(links["assembly_order"]["records"][0]["entity_id"], records[ao])
                self.assertEqual(links["assembly_order"]["record_status"], "matched")
                # Portal: finished-good lot number = AO mo_code.
                self.assertEqual(links["lot"]["values"], [ao])
                self.assertIn("fp-bridge.ts@8da33e4:1029", links["lot"]["portal_rule"])
                self.assertEqual(links["lot"]["records"][0]["entity_id"], records["lot:" + ao])
                self.assertEqual(links["bulk_lot"]["values"], [bulk])
                self.assertEqual(links["bulk_lot"]["record_status"], "no_visible_record")
                self.assertEqual(links["customer"]["values"], [customer])
                self.assertEqual(links["customer_po"]["status"], "blank_in_document")
                if oe:
                    self.assertEqual(links["sales_order"]["values"], [oe])
                    self.assertEqual(links["sales_order"]["records"][0]["entity_id"], records[oe])
                    self.assertEqual(links["sales_order"]["evidence"][0]["reading"], "interpreted")
                else:
                    self.assertEqual(links["sales_order"]["status"], "blank_in_document")
                    self.assertEqual(links["sales_order"]["record_status"], "not_looked_up")
                if oe == "OR1000151885":
                    # Paper prints "PO:" blank; the FM order holds PO12172025.
                    self.assertEqual(links["customer_po"]["document_vs_record"],
                                     "document_blank_record_has_value")
                    self.assertEqual(links["customer_po"]["sales_order_record_values"],
                                     ["PO12172025"])
                self.assertEqual(result["current_columns"]["status"], "not_read")
                self.assertTrue(all(row["comparison"] == "not_read"
                                    for row in result["columns"]))
                self.assertEqual(result["business_system_writes"], 0)
                self.assertFalse(result["sharepoint_write_available"])

    def test_current_columns_report_agree_differs_missing_and_undetermined(self):
        ao, erp, oe, customer, bulk = RICARDO_AOS[0]
        text, rows = ao_packet(ao, erp, oe, customer, bulk)
        url = SITE + ao + ".pdf"
        attrs = attributes(text, f"{ao}.pdf", url, ocr=True, rows=rows)
        attrs["source_columns"] = columns_snapshot("qcqa-drive:ao", "etag-1", {
            "PartyType": "Internal", "ItemRef": "AO-26005C", "DocumentCategory": "FILL_RECORD",
            "DocumentType": "BATCH_RECORD", "ContentType": "PLX Production Order",
            "CustomerVisible": False, "FileLeafRef": "AO-26005B.pdf",
            "ProductRef": "FG-0420", "OrderRef": "CPO-77120"})
        result = build_proposals(document_id="doc", revision="etag-1", fingerprint="f",
                                 attrs=attrs, locator=url)
        columns = by_column(result)
        self.assertEqual(columns["PartyType"]["comparison"], "agree")
        self.assertEqual((columns["ItemRef"]["comparison"], columns["ItemRef"]["current_value"]),
                         ("differs", "AO-26005C"))
        self.assertEqual(columns["DocStatus"]["comparison"], "missing")
        self.assertEqual(columns["Source"]["comparison"], "missing")
        # A current value we cannot confirm is shown, never endorsed.
        self.assertEqual((columns["DocumentCategory"]["comparison"],
                          columns["DocumentCategory"]["current_value"]),
                         ("undetermined", "FILL_RECORD"))
        self.assertEqual(columns["DocumentType"]["comparison"], "current_only")
        # TASK-2215: the upload door sets Confidentiality from the party (Internal here)
        self.assertEqual((columns["Confidentiality"]["value"],
                          columns["Confidentiality"]["comparison"]), ("Internal", "missing"))
        # The Portal's customer-visibility flag is read as a boolean, never proposed.
        self.assertEqual((columns["CustomerVisible"]["status"],
                          columns["CustomerVisible"]["comparison"],
                          columns["CustomerVisible"]["current_value"]),
                         ("not_set_by_portal_rule", "current_only", False))
        # ProductRef / OrderRef come from the Portal's record links (2167c63 TASK-2222):
        # read with the current columns, never a proposal row.
        self.assertEqual({"ProductRef", "OrderRef"} & set(columns), set())
        self.assertEqual(result["content_type"]["status"], "no_portal_rule")
        self.assertEqual(result["content_type"]["current_portal_document_type"],
                         "PRODUCTION_ORDER")
        snapshot = result["current_columns"]
        self.assertEqual((snapshot["origin"], snapshot["matches_document_revision"],
                          snapshot["absent_field_meaning"]),
                         ("stored_listitem_snapshot", True, "empty_or_not_provisioned"))
        self.assertEqual(result["comparison_summary"]["missing"], 3)
        # A snapshot of another drive revision is reported as such.
        stale = build_proposals(document_id="doc", revision="etag-1", fingerprint="f",
                                attrs={**attrs, "source_revision": "etag-2"}, locator=url)
        self.assertFalse(stale["current_columns"]["matches_document_revision"])
        # ... and no column is presented as agreeing with, or missing from,
        # the current file: each comparison is marked stale.
        self.assertEqual({row["comparison"] for row in stale["columns"]}, {"stale_snapshot"})
        stale_item = by_column(stale)["ItemRef"]
        self.assertEqual((stale_item["current_value"], stale_item["comparison_at_snapshot_revision"]),
                         ("AO-26005C", "differs"))
        self.assertEqual(stale["comparison_summary"], {"stale_snapshot": len(stale["columns"])})

    def test_a_value_the_portal_states_is_never_proposed_and_a_differing_one_conflicts(self):
        ao, erp, oe, customer, bulk = RICARDO_AOS[0]
        text, rows = ao_packet(ao, erp, oe, customer, bulk)
        url = SITE + ao + ".pdf"
        attrs = attributes(text, f"{ao}.pdf", url, ocr=True, rows=rows)
        attrs["source_columns"] = columns_snapshot("qcqa-drive:ao", "etag-1", {
            "PartyType": "Internal", "ItemRef": "AO-26005C"})
        columns = by_column(build_proposals(document_id="doc", revision="etag-1",
                                            fingerprint="f", attrs=attrs, locator=url))
        # Equal to what the Portal states: not emitted.
        self.assertEqual((columns["PartyType"]["status"], columns["PartyType"]["value"],
                          columns["PartyType"]["current_value"]),
                         ("stated_by_portal", None, "Internal"))
        # Different: a conflict naming both, never a proposal over the Portal's value.
        item = columns["ItemRef"]
        self.assertEqual((item["status"], item["reason"], item["value"]),
                         ("conflict", "differs_from_portal_stated", None))
        self.assertEqual([c["value"] for c in item["candidates"]], ["AO-26005B", "AO-26005C"])
        self.assertEqual(item["candidates"][1]["basis"], "stated_by_portal")
        # A column the Portal leaves empty is still proposed.
        self.assertEqual((columns["DocStatus"]["status"], columns["DocStatus"]["comparison"]),
                         ("proposed", "missing"))
        # Columns read at another revision are not what the Portal states now.
        stale = by_column(build_proposals(document_id="doc", revision="etag-1", fingerprint="f",
                                          attrs={**attrs, "source_revision": "etag-2"},
                                          locator=url))
        self.assertEqual((stale["PartyType"]["status"], stale["ItemRef"]["status"]),
                         ("proposed", "proposed"))
        # The capture's projection serves only a snapshot bound to the version's own eTag.
        record = {"etag": "etag-1", "list_item_fields": attrs["source_columns"]}
        self.assertEqual(taxonomy.stated_metadata(record)["columns"],
                         {"PartyType": "Internal", "ItemRef": "AO-26005C"})

    def test_a_person_confirmed_value_that_differs_from_the_portal_is_a_conflict(self):
        # What the projection puts beside a confirmation (kind_confirmation projection_step):
        # the confirmed columns against SharePointFile.portal_metadata of the same version.
        snapshot = columns_snapshot("drive:item", "etag-1", {
            "PartyType": "Customer", "ItemRef": "AO-26005C", "DocumentCategory": "OTHER",
            "ExpiryDate": "2027-01-31T00:00:00Z", "DocStatus": ""})
        stated = taxonomy.stated_metadata({"etag": "etag-1", "list_item_fields": {
            **snapshot, "field_names": [c["name"] for c in taxonomy.COLUMNS]}})
        out = doc_metadata.confirmed_against_stated({
            "PartyType": "Customer", "ItemRef": "AO-26005B", "DocumentCategory": "CUSTOMER_PO",
            "ExpiryDate": "2027-01-31", "DocStatus": "Approved", "ContentType": "PLX CoA"},
            stated)
        self.assertEqual((out["status"], out["conflicts"]), ("conflict", ["ItemRef"]))
        item = out["columns"]["ItemRef"]
        self.assertEqual((item["reason"], [(c["value"], c["basis"]) for c in item["candidates"]]),
                         ("differs_from_portal_stated", [("AO-26005B", "person_confirmed"),
                                                         ("AO-26005C", "stated_by_portal")]))
        # The category agrees as the column choice the Portal writes for it (OTHER); dates by day; an empty
        # column is no conflict; a column outside the stated set was not read.
        self.assertEqual({name: row["comparison"] for name, row in out["columns"].items()},
                         {"PartyType": "agree", "ItemRef": "conflict",
                          "DocumentCategory": "agree", "ExpiryDate": "agree",
                          "DocStatus": "portal_empty", "ContentType": "not_read"})
        self.assertEqual(out["stated_citation"]["drive_etag"], "etag-1")
        # A different confirmed category conflicts; no stated read compares nothing.
        self.assertEqual(doc_metadata.confirmed_against_stated(
            {"DocumentCategory": "QUOTE"}, stated)["conflicts"], ["DocumentCategory"])
        self.assertEqual(doc_metadata.confirmed_against_stated({"ItemRef": "AO-1"}, None)
                         ["status"], "not_read")
        # A snapshot bound to another version states nothing for this one.
        self.assertIsNone(taxonomy.stated_metadata({"etag": "etag-2", "list_item_fields": {
            **snapshot, "field_names": [c["name"] for c in taxonomy.COLUMNS]}}))

    def test_propose_compares_with_the_captured_columns_of_the_attached_version(self):
        """plx.document_fields mode propose (``case_context_tools.document_metadata``) reads
        the columns the host's capture holds for the original's item, eTag and bytes, through
        the capture's gate for the session actor; a reader it refuses compares nothing."""
        from integrations import live_source_grammar as grammar
        from operating import case_context_tools
        from operating.cases import CitationUnresolvable

        text = "Packing slip\nSales Order: OR100150351\n"
        drive, item, etag = "b!drive", "01PS", '"{01PS},3"'
        snapshot = columns_snapshot(f"{drive}:{item}", etag, {"PartyType": "Internal",
                                                              "DocStatus": "Approved"})
        # as the capture stores it: the one listItem URL of the drive item
        snapshot["source_api"] = grammar.drive_list_item_fields_url(drive, item)
        asked = []

        class Captures:
            def document(self, handle, actor):
                asked.append(handle)
                if actor is OTHER:
                    raise CitationUnresolvable("capture_citation_unresolvable")
                return {"row": {"etag": etag, "list_item_fields": snapshot}}

        class Originals:
            def text_document(self, citation, actor):
                return {"view": {"filename": "ps.pdf", "sha256": "ab" * 32,
                                 "provenance": {"source": "sharepoint-drive",
                                                "drive_id": drive, "item_id": item,
                                                "etag": etag}},
                        "warnings": [], "ocr_pages": [], "text": text,
                        "pages": [{"page": 1, "start": 0, "end": len(text)}],
                        "text_sha256": "cd" * 32}

        class Runtime:
            captures, originals = Captures(), Originals()

            def employee_gate(self, actor):
                return actor

        class NoStore:   # the linked-record lookup is not under test here
            def __init__(self, *args, **kwargs):
                pass

            def call(self, *args):
                return {"error": {"code": "store_unavailable", "message": "x"}}

            def close(self):
                pass

        self.enterContext(mock.patch.object(case_context_tools, "CaseHostTools", NoStore))
        result = case_context_tools.document_metadata(Runtime(), QA, "original://orig-1")
        self.assertEqual(asked, [f"sharepoint://{drive.replace('!', '%21')}/{item}@"
                                 "%22%7B01PS%7D%2C3%22#sha256=" + "ab" * 32])
        self.assertEqual((result["current_columns"]["status"],
                          result["current_columns"]["matches_document_revision"]), ("read", True))
        columns = by_column(result)
        self.assertEqual((columns["PartyType"]["status"], columns["DocStatus"]["status"],
                          columns["Source"]["status"]),
                         ("stated_by_portal", "conflict", "proposed"))
        # A version captured before the capture recorded its request read DocumentCategory
        # only: every other column is not_read, never missing.
        snapshot = columns_snapshot(f"{drive}:{item}", etag, {"DocumentCategory": "OTHER"})
        snapshot["source_api"] = grammar.drive_list_item_fields_url(drive, item)
        legacy = by_column(case_context_tools.document_metadata(Runtime(), QA,
                                                                "original://orig-1"))
        self.assertEqual({name: (legacy[name]["status"], legacy[name]["comparison"])
                          for name in ("DocumentCategory", "PartyType", "DocStatus")},
                         {"DocumentCategory": ("stated_by_portal", "agree"),
                          "PartyType": ("proposed", "not_read"),
                          "DocStatus": ("proposed", "not_read")})
        # The capture's own snapshot names its request: an absent requested column is missing.
        snapshot["field_names"] = ["DocumentCategory", "DocStatus"]
        named = by_column(case_context_tools.document_metadata(Runtime(), QA,
                                                               "original://orig-1"))
        self.assertEqual((named["DocStatus"]["comparison"], named["PartyType"]["comparison"]),
                         ("missing", "not_read"))
        refused = case_context_tools.document_metadata(Runtime(), OTHER, "original://orig-1")
        self.assertEqual(refused["current_columns"]["status"], "not_read")
        self.assertEqual(by_column(refused)["PartyType"]["status"], "proposed")

    def test_supplied_snapshot_must_be_this_documents_listitem(self):
        fixture = Fixture()
        ao, erp, oe, customer, bulk = RICARDO_AOS[0]
        text, rows = ao_packet(ao, erp, oe, customer, bulk)
        doc_id = fixture.document("qcqa-drive:ao", attributes(
            text, f"{ao}.pdf", SITE + f"{ao}.pdf", rows=rows))
        fields = {"PartyType": "Internal", "ItemRef": ao}
        own = proposals_for(fixture, QA, doc_id, current_columns=columns_snapshot(
            "qcqa-drive:ao", "etag-1", fields))
        self.assertEqual((own["current_columns"]["status"], own["current_columns"]["origin"]),
                         ("read", "supplied_listitem_snapshot"))
        self.assertEqual(by_column(own)["ItemRef"]["comparison"], "agree")
        # Same eTag, another drive item: not this document's columns.
        other = proposals_for(fixture, QA, doc_id, current_columns=columns_snapshot(
            "other-drive:zzz", "etag-1", fields))
        self.assertEqual(other["current_columns"]["status"], "snapshot_identity_mismatch")
        self.assertEqual(other["current_columns"]["expected_source_api"],
                         columns_snapshot("qcqa-drive:ao", "etag-1", {})["source_api"])
        self.assertEqual({row["comparison"] for row in other["columns"]}, {"not_read"})
        self.assertNotIn("agree", other["comparison_summary"])
        # The right item at an older revision: shown, each column stale.
        old = proposals_for(fixture, QA, doc_id, current_columns=columns_snapshot(
            "qcqa-drive:ao", "etag-0", fields))
        self.assertFalse(old["current_columns"]["matches_document_revision"])
        self.assertEqual({row["comparison"] for row in old["columns"]}, {"stale_snapshot"})
        # A document whose identity gives no listItem URL accepts no snapshot.
        bare = build_proposals(document_id="doc", revision="etag-1", fingerprint="f",
                               attrs=attributes(text, "x.pdf", SITE + "x.pdf"), locator="x",
                               current_columns=columns_snapshot("qcqa-drive:ao", "etag-1",
                                                                fields))
        self.assertEqual(bare["current_columns"]["status"], "snapshot_identity_mismatch")

    def ao_form(self, po_line, oe="OR1000151885"):
        return ("ASSEMBLY ORDER MO\nForm #072B\nAO LOT#: AO-26005B BULK LOT#: BC-26007E\n"
                f"ERP CODE: DVS-5003-A OE: {oe}\nCUSTOMER: Diversified {po_line} "
                "REQUIRED DATE: 2026-01-01\n")

    def run_ao_form(self, text):
        fixture = Fixture()
        order = fixture.record("SalesOrder", "351", {"orderNumber": "OR1000151885",
                                                     "poReference": "PO12172025"})
        fixture.record("AssemblyOrder", "10718", {"mo_code": "AO-26005B"})
        doc_id = fixture.document("qcqa-drive:ao1", attributes(
            text, "AO-26005B.pdf", SITE + "AO-26005B.pdf"))
        result = proposals_for(fixture, QA, doc_id)
        return result, order

    def test_filled_ao_po_field_is_read_and_never_called_blank(self):
        for po_line, value, verdict in (("PO: PO12172025", "PO12172025", "agree"),
                                        ("PO#: PO12172025", "PO12172025", "agree"),
                                        ("PO: 12172025", "12172025", "differs")):
            with self.subTest(po_line=po_line):
                text = self.ao_form(po_line)
                result, order = self.run_ao_form(text)
                links = by_link(result)
                po = links["customer_po"]
                self.assertEqual(links["sales_order"]["records"][0]["entity_id"], order)
                self.assertEqual((po["status"], po["values"]), ("found_in_document", [value]))
                self.assertEqual(po["document_vs_record"], verdict)
                self.assertNotEqual(po["document_vs_record"], "document_blank_record_has_value")
                evidence = po["evidence"][0]
                self.assertEqual(evidence["quote"], value)
                self.assertEqual(text[evidence["span"]["start"]:evidence["span"]["end"]], value)
                # The bare "PO" field is read by form layout: review-grade.
                self.assertEqual(evidence["reading"], "interpreted")
                self.assertTrue(po["requires_review"])
                self.assertEqual(result["anchor"]["entity_type"], "ASSEMBLY_ORDER")

    def test_po_the_reader_does_not_find_is_not_read_not_blank(self):
        # No PO label at all (e.g. a layout the reader does not know): the
        # document is not said to be blank.
        result, _ = self.run_ao_form(self.ao_form("P.O. NUMBER PO12172025"))
        po = by_link(result)["customer_po"]
        self.assertEqual(po["status"], "absent_in_document")
        self.assertEqual(po["document_vs_record"], "document_value_not_read")
        self.assertEqual(po["sales_order_record_values"], ["PO12172025"])
        # A printed blank label still says blank.
        blank, _ = self.run_ao_form(self.ao_form("PO:"))
        self.assertEqual(by_link(blank)["customer_po"]["document_vs_record"],
                         "document_blank_record_has_value")
        self.assertEqual(by_link(blank)["customer_po"]["status"], "blank_in_document")

    def test_ao_form_customer_po_label_is_a_field_not_a_second_subject(self):
        result, _ = self.run_ao_form(self.ao_form("CUSTOMER PO: PO12172025"))
        columns, po = by_column(result), by_link(result)["customer_po"]
        self.assertEqual(result["anchor"]["status"], "decided")
        self.assertEqual(result["anchor"]["entity_type"], "ASSEMBLY_ORDER")
        self.assertEqual((columns["PartyType"]["value"], columns["ItemRef"]["value"],
                          columns["DocStatus"]["value"], columns["Source"]["value"]),
                         ("Internal", "AO-26005B", "Pending Review", "Staff"))
        # Read once by the general extractor, not twice.
        self.assertEqual((po["values"], len(po["evidence"])), (["PO12172025"], 1))
        self.assertEqual(po["document_vs_record"], "agree")

    def test_linked_record_lookup_uses_latest_revision_only(self):
        fixture = Fixture()
        moved = fixture.record("AssemblyOrder", "10718", {"mo_code": "AO-26005B"})
        fixture.record("AssemblyOrder", "10718", {"mo_code": "AO-26005Z"}, revision="r2",
                       at=AT.replace(hour=13))
        current = fixture.record("AssemblyOrder", "10719", {"mo_code": "AO-26005B"})
        doc_id = fixture.document("qcqa-drive:ao1", attributes(
            self.ao_form("PO:"), "AO-26005B.pdf", SITE + "AO-26005B.pdf"))
        result = proposals_for(fixture, QA, doc_id)
        link = by_link(result)["assembly_order"]
        self.assertEqual(([r["entity_id"] for r in link["records"]], link["record_status"]),
                         ([current], "matched"))
        self.assertNotIn(moved, [r["entity_id"] for r in link["records"]])

    def test_printed_non_value_in_the_po_field_is_not_read_not_blank(self):
        # Capital words before the next label are printed content, not a label.
        for po_line in ("PO: VERBAL", "PO: EMAIL", "PO: BLANKET", "PO: TBD", "PO: N/A"):
            with self.subTest(po_line=po_line):
                result, _ = self.run_ao_form(self.ao_form(po_line))
                po = by_link(result)["customer_po"]
                self.assertEqual((po["status"], po["values"]), ("absent_in_document", []))
                self.assertEqual(po["document_vs_record"], "document_value_not_read")
                self.assertIn("ao_po_field_not_read", result["notes"])

    def test_value_on_the_next_line_is_not_read_not_blank(self):
        # pypdf table layout: the label ends its line, the value is below it.
        text = ("ASSEMBLY ORDER MO\nForm #072B\nAO LOT#: AO-26005B\nERP CODE: DVS-5003-A\n"
                "OE:\nOR1000151885\nPO:\nPO12172025\nREQUIRED DATE:\n2026-01-01\n")
        result, _ = self.run_ao_form(text)
        links = by_link(result)
        self.assertEqual(links["customer_po"]["status"], "absent_in_document")
        self.assertEqual(links["sales_order"]["status"], "absent_in_document")
        self.assertIn("ao_po_field_not_read", result["notes"])
        self.assertIn("ao_oe_field_not_read", result["notes"])
        # Ricardo's packet shapes stay blank: "PO: REQUIRED DATE:" and
        # "OE:" ending a line whose next line starts "CUSTOMER:".
        ao, erp, _oe, customer, bulk = RICARDO_AOS[5]
        packet, _rows = ao_packet(ao, erp, None, customer, bulk)
        self.assertIn("OE:\nCUSTOMER:", packet)
        blank = build_proposals(document_id="doc", revision="r1", fingerprint="f",
                                attrs=attributes(packet, f"{ao}.pdf", SITE), locator=SITE)
        self.assertEqual({by_link(blank)[name]["status"] for name in ("customer_po", "sales_order")},
                         {"blank_in_document"})
        self.assertEqual([e["quote"] for e in by_link(blank)["sales_order"]["evidence"]], ["OE:"])
        self.assertEqual([e["quote"] for e in by_link(blank)["customer_po"]["evidence"]], ["PO:"])
        self.assertEqual(blank["notes"], [])

    def test_ambiguous_po_field_content_is_never_a_single_value(self):
        for po_line in ("PO: PO12172025, PO7777", "PO: PO1217 2025", "PO: 12/17/2025",
                        "PO: 2025-12-17"):
            with self.subTest(po_line=po_line):
                result, _ = self.run_ao_form(self.ao_form(po_line))
                po = by_link(result)["customer_po"]
                self.assertEqual((po["status"], po["values"]), ("absent_in_document", []))
                self.assertEqual(po["document_vs_record"], "document_value_not_read")
        # Two filled PO fields are a conflict, never the first one.
        result, _ = self.run_ao_form(self.ao_form("PO: PO12172025 PO: PO99"))
        po = by_link(result)["customer_po"]
        self.assertEqual((po["status"], po["values"]), ("conflict_in_document",
                                                        ["PO12172025", "PO99"]))
        self.assertEqual(po["document_vs_record"], "document_values_conflict")

    def test_petra_side_po_labels_are_not_the_customer_po(self):
        for po_line in ("PLX PO: 99123", "OUR PO: 99123", "VENDOR PO #: V12345",
                        "SUPPLIER PO: S123", "PLX PO:"):
            with self.subTest(po_line=po_line):
                result, _ = self.run_ao_form(self.ao_form(po_line))
                po = by_link(result)["customer_po"]
                self.assertEqual((po["status"], po["values"]), ("absent_in_document", []))
                self.assertNotEqual(po["document_vs_record"], "document_blank_record_has_value")

    def test_petra_po_on_an_ao_form_is_not_read_as_the_customer_po(self):
        result, _ = self.run_ao_form(self.ao_form("PETRA PO: PO5447"))
        links = by_link(result)
        self.assertEqual(links["petra_po"]["values"], ["PO5447"])
        self.assertEqual(links["customer_po"]["status"], "absent_in_document")
        self.assertEqual(links["customer_po"]["document_vs_record"], "document_value_not_read")

    def test_title_only_ao_scan_without_text_decides_nothing(self):
        url = SITE + "AO-26005B.pdf"
        attrs = attributes("", "AO-26005B.pdf", url)
        result = build_proposals(document_id="doc", revision="r1", fingerprint="f",
                                 attrs=attrs, locator=url)
        columns, links = by_column(result), by_link(result)
        self.assertEqual(result["anchor"]["status"], "unknown")
        for name in ("PartyType", "PartyRef", "DocumentCategory", "ItemRef", "DocStatus",
                     "Source"):
            self.assertEqual(columns[name]["status"], "unknown", name)
            self.assertIsNone(columns[name]["value"], name)
        self.assertEqual(links["assembly_order"]["status"], "absent_in_document")
        self.assertEqual(links["assembly_order"]["title_leads"][0]["reading"], "interpreted")
        self.assertIn("assembly_order_only_in_title", result["notes"])
        self.assertEqual(links["lot"]["status"], "absent_in_document")


class OrderDocumentTest(unittest.TestCase):
    def test_customer_po_on_sales_order_mirrors_portal_internal_party_quirk(self):
        url = "https://petrasoap.sharepoint.com/sites/PLXCUSTOMERS/Shared%20Documents/po.txt"
        text = "PURCHASE ORDER\nCustomer PO: PO12172025\nCustomer: Diversified\n"
        result = build_proposals(document_id="doc", revision="r1", fingerprint="f",
                                 attrs=attributes(text, "Customer purchase order", url),
                                 locator=url)
        columns = by_column(result)
        self.assertEqual(result["anchor"]["entity_type"], "CUSTOMER_ORDER")
        self.assertEqual(columns["DocumentCategory"]["value"], "CUSTOMER_PO")
        self.assertEqual(columns["DocumentCategory"]["allowed_values"],
                         ["CUSTOMER_PO", "QUOTE", "SHIPPING_DOC", "OTHER"])
        # attachment-metadata.ts has SALES_ORDER/ORDER/OE but not CUSTOMER_ORDER,
        # the entityType the order page sends: the Portal writes Internal.
        self.assertEqual(columns["PartyType"]["value"], "Internal")
        self.assertEqual(columns["CustomerRef"]["status"], "not_applicable")
        # The page's ref token is the order's erp_code/order_number, which a
        # customer's PO does not print: unknown, not guessed.
        self.assertEqual(columns["ItemRef"]["status"], "unknown")
        self.assertEqual(by_link(result)["customer_po"]["values"], ["PO12172025"])
        self.assertTrue(all(e["reading"] == "literal" for e in columns["DocumentCategory"]["evidence"]))
        self.assertFalse(columns["DocumentCategory"]["requires_review"])
        # TASK-2215 (e6dc559), TASK-2594 (e677b49): the upload door writes the short column
        # choice of the page category's row, that row's retention (a customer PO is kept
        # permanently) and the party's confidentiality.
        self.assertEqual((columns["DocumentCategory"]["written_as"],
                          columns["RetentionCategory"]["value"],
                          columns["Confidentiality"]["value"]), ("OTHER", "Permanent", "Internal"))
        # a file holds the page category (written before) or its canonical choice: both agree
        for current, comparison in (("OTHER", "agree"), ("CUSTOMER_PO", "agree"),
                                    ("QUOTE", "differs")):
            attrs = attributes(text, "Customer purchase order", url)
            attrs["source_columns"] = columns_snapshot("drive:po", "r1",
                                                       {"DocumentCategory": current})
            row = by_column(build_proposals(document_id="doc", revision="r1", fingerprint="f",
                                            attrs=attrs, locator=url))["DocumentCategory"]
            self.assertEqual(row["comparison"], comparison, current)

    def test_order_item_ref_follows_portal_erp_code_then_order_number(self):
        # order-detail.tsx@549f6f0:892: ref = order.erp_code || order.order_number.
        url = "https://petrasoap.sharepoint.com/sites/PLXCUSTOMERS/Shared%20Documents/ps.txt"
        text = "Packing slip\nSales Order: OR100150351\n"
        for erp, expected in (("OE-150351", "OE-150351"), ("", "OR100150351"),
                              (None, "OR100150351")):
            with self.subTest(erp=erp):
                fixture = Fixture()
                attrs = {"orderNumber": "OR100150351"}
                if erp is not None:
                    attrs["erpCode"] = erp
                order = fixture.record("SalesOrder", "150351", attrs)
                doc_id = fixture.document("cust-drive:ps", attributes(text, "ps.pdf", url))
                result = proposals_for(fixture, QA, doc_id)
                item = by_column(result)["ItemRef"]
                self.assertEqual(result["anchor"]["entity_type"], "CUSTOMER_ORDER")
                self.assertEqual((item["status"], item["value"], item["basis"]),
                                 ("proposed", expected, "portal_rule_on_linked_record"))
                cited = [e for e in item["evidence"] if e["basis"] == "linked_record"]
                self.assertEqual([e["entity_id"] for e in cited], [order])
                self.assertIn("order-detail.tsx@", cited[0]["rule"])
                printed = [e for e in item["evidence"] if e["basis"] == "document_text"]
                self.assertEqual(printed[0]["quote"], "OR100150351")
        # The printed order number alone, with no visible record, decides nothing.
        fixture = Fixture()
        fixture.record("SalesOrder", "150351", {"orderNumber": "OR100150351",
                                                "erpCode": "OE-150351"}, acl=("staff:finance",))
        doc_id = fixture.document("cust-drive:ps", attributes(text, "ps.pdf", url))
        result = proposals_for(fixture, QA, doc_id)
        item = by_column(result)["ItemRef"]
        self.assertEqual((item["status"], item["value"], item["reason"]),
                         ("unknown", None, "order_record_not_resolved"))
        self.assertNotIn("OE-150351", str(result))

    def test_vendor_confirmation_on_purchase_order_is_supplier_with_po_refs(self):
        url = "https://petrasoap.sharepoint.com/sites/AGENTS/Shared%20Documents/pi.txt"
        text = ("Order confirmation\nPetra PO: PO5447\nSupplier: Ningbo Example Co\n"
                "Purchase order received with thanks\n")
        fixture = Fixture()
        po = fixture.record("PurchaseOrder", "po-5447", {"poNumber": "PO5447"},
                            source="portal-gold")
        hidden = fixture.record("PurchaseOrder", "po-5447-finance", {"poNumber": "PO5447"},
                                acl=("staff:finance",), source="portal-gold")
        doc_id = fixture.document("agents-drive:pi", attributes(text, "PI-SWC025240.pdf", url))
        result = proposals_for(fixture, QA, doc_id)
        columns, links = by_column(result), by_link(result)
        self.assertEqual(result["anchor"]["entity_type"], "PURCHASE_ORDER")
        self.assertEqual(columns["DocumentCategory"]["value"], "ORDER_CONFIRMATION")
        self.assertEqual(columns["PartyType"]["value"], "Supplier")
        # buildAttachmentMetadata: Supplier PartyRef = ref token = po.po_number.
        self.assertEqual((columns["PartyRef"]["value"], columns["ItemRef"]["value"]),
                         ("PO5447", "PO5447"))
        self.assertEqual([r["entity_id"] for r in links["petra_po"]["records"]], [po])
        self.assertNotIn(hidden, str(result))
        self.assertEqual(links["vendor"]["values"], ["Ningbo Example Co"])

    def test_document_naming_two_record_kinds_is_a_conflict_not_a_choice(self):
        url = "https://petrasoap.sharepoint.com/sites/x/Shared%20Documents/mixed.txt"
        text = "Petra PO: PPO-77\nCustomer PO: CPO-40\n"
        result = build_proposals(document_id="doc", revision="r1", fingerprint="f",
                                 attrs=attributes(text, "mixed", url), locator=url)
        self.assertEqual(result["anchor"]["status"], "conflict")
        self.assertEqual({c["entity_type"] for c in result["anchor"]["candidates"]},
                         {"CUSTOMER_ORDER", "PURCHASE_ORDER"})
        columns = by_column(result)
        self.assertEqual({columns[n]["status"] for n in ("PartyType", "ItemRef",
                          "DocumentCategory", "DocStatus", "Source")}, {"unknown"})

    def test_conflicting_record_numbers_in_one_document_are_reported(self):
        url = "https://petrasoap.sharepoint.com/sites/x/Shared%20Documents/two.txt"
        text = "Purchase order\nPetra PO: PO5447\nPetra PO: PO5448\n"
        result = build_proposals(document_id="doc", revision="r1", fingerprint="f",
                                 attrs=attributes(text, "two", url), locator=url)
        columns, links = by_column(result), by_link(result)
        self.assertEqual(links["petra_po"]["status"], "conflict_in_document")
        self.assertEqual(links["petra_po"]["record_status"], "not_looked_up")
        self.assertEqual(columns["ItemRef"]["status"], "conflict")
        self.assertEqual(columns["PartyRef"]["status"], "conflict")
        self.assertIsNone(columns["ItemRef"]["value"])


class ModelSuggestionTest(unittest.TestCase):
    def setUp(self):
        ao, erp, oe, customer, bulk = RICARDO_AOS[0]
        self.text, rows = ao_packet(ao, erp, oe, customer, bulk)
        self.url = SITE + "AO-26005B.pdf"
        self.attrs = attributes(self.text, "AO-26005B.pdf", self.url, ocr=True, rows=rows)

    def suggestion(self, column, value, quote, model="claude-opus-5-5"):
        start = self.text.index(quote)
        return {"column": column, "value": value, "model": model,
                "span": {"start": start, "end": start + len(quote)}, "quote": quote}

    def run_with(self, *suggestions, attrs=None):
        return build_proposals(document_id="doc", revision="r1", fingerprint="f",
                               attrs=attrs or self.attrs, locator=self.url,
                               model_suggestions=list(suggestions))

    def test_model_never_resolves_a_category_conflict(self):
        result = self.run_with(self.suggestion("DocumentCategory", "QC_SIGNOFF",
                                               "FINISHED GOODS INSPECTION"))
        category = by_column(result)["DocumentCategory"]
        self.assertEqual(category["status"], "conflict")
        self.assertIsNone(category["value"])
        self.assertEqual(category["model_suggestions"][0]["reading"], "model_suggestion")

    def test_model_disagreeing_with_a_rule_becomes_a_conflict(self):
        result = self.run_with(self.suggestion("PartyType", "Customer", "CUSTOMER"))
        party = by_column(result)["PartyType"]
        self.assertEqual((party["status"], party["reason"]), ("conflict", "model_disagrees_with_rule"))
        self.assertEqual({c["basis"] for c in party["candidates"]}, {"portal_rule", "model_suggestion"})

    def test_model_fills_only_an_unset_column_and_stays_review_grade(self):
        result = self.run_with(self.suggestion("ProjectRef", "PD-26-100006", "Form #072B"))
        row = by_column(result)["ProjectRef"]
        self.assertEqual((row["status"], row["value"], row["basis"], row["requires_review"]),
                         ("model_suggestion", "PD-26-100006", "model_suggestion", True))

    def test_invalid_model_suggestions_are_rejected_with_reasons(self):
        bad_span = self.suggestion("ItemRef", "AO-26005B", "AO-26005B")
        bad_span["quote"] = "AO-99999Z"
        result = self.run_with(
            self.suggestion("DocStatus", "Signed", "Form #072B"),
            self.suggestion("DocumentCategory", "CUSTOMER_PO", "Form #072B"),
            self.suggestion("NotAColumn", "x", "Form #072B"),
            self.suggestion("CustomerVisible", "Yes", "Form #072B"),
            self.suggestion("OrderRef", "AO-26005B", "AO-26005B"),
            bad_span,
            {**self.suggestion("ItemRef", "AO-26005B", "AO-26005B"), "model": ""})
        self.assertEqual([row["reason"] for row in result["rejected_model_suggestions"]],
                         ["value_not_an_allowed_choice", "category_not_offered_for_anchor",
                          "unknown_portal_column", "column_set_from_portal_record",
                          "column_set_from_portal_record", "quote_does_not_match_current_span",
                          "model_label_required"])
        self.assertEqual(by_column(result)["ItemRef"]["status"], "proposed")


class PortalTaxonomyMirrorTest(unittest.TestCase):
    def test_attachment_rule_matches_portal_buildAttachmentMetadata(self):
        self.assertEqual(taxonomy.build_attachment_metadata("PURCHASE_ORDER", "SIGNED_PO",
            {"ref": "PO-26-0087"}), {"PartyType": "Supplier", "DocumentCategory": "OTHER",
            "DocStatus": "Pending Review", "Source": "Staff", "PartyRef": "PO-26-0087",
            "ItemRef": "PO-26-0087", "RetentionCategory": "Permanent",
            "Confidentiality": "Internal"})
        self.assertEqual(taxonomy.build_attachment_metadata("RD_PROJECT", " tech transfer ",
            {"customer": "Cloth Chronicles", "customerCode": "", "project": "PD-26-100006"}),
            {"PartyType": "Customer", "DocumentCategory": "TECH_TRANSFER",
             "DocStatus": "Pending Review", "Source": "Staff", "ProjectRef": "PD-26-100006",
             "CustomerRef": "Cloth Chronicles", "RetentionCategory": "3 years",
             "Confidentiality": "Customer Confidential"})
        # TASK-2594: a fine category keeps its own row: a fill record is written as the
        # BATCH_RECORD choice and kept as a GMP record, not OTHER for 3 years.
        fill = taxonomy.build_attachment_metadata("ASSEMBLY_ORDER", "FILL_RECORD", {})
        self.assertEqual((fill["DocumentCategory"], fill["RetentionCategory"]),
                         ("BATCH_RECORD", "7 years"))
        # resolveDocumentCategory: a category with a row is kept, a document type alias maps
        # through DOCTYPE_TO_STORAGE, anything else (or nothing) is OTHER
        resolved = [taxonomy.resolve_document_category(c) for c in
                    ("LABEL_PROOF", "Invoice", "spec sheet", "SIGNED_PO", "no such type", None, 7)]
        self.assertEqual(resolved, ["LABEL_PROOF", "COMMERCIAL_INVOICE", "SPEC_SHEET",
                                    "SIGNED_PO", "OTHER", "OTHER", "OTHER"])
        self.assertEqual([taxonomy.document_category_column(c) for c in resolved],
                         ["LABEL_PROOF", "COMMERCIAL_INVOICE", "SPEC_SHEET", "OTHER", "OTHER",
                          "OTHER", "OTHER"])
        self.assertEqual(taxonomy.retention_for_category("AGREEMENT"), "Permanent")
        # an unknown or missing category fails closed to OTHER's row
        for unknown in ("no such type", None, 7, "coa"):
            self.assertEqual((taxonomy.document_category_column(unknown),
                              taxonomy.retention_for_category(unknown)), ("OTHER", "3 years"))
        self.assertEqual(taxonomy.party_type_for_entity(" assembly_order "), "Internal")
        self.assertEqual(taxonomy.party_type_for_entity("CUSTOMER_ORDER"), "Internal")

    def test_one_document_type_row_drives_column_and_retention(self):
        # document-types.ts@45673ee (TASK-2594): every category a page routes, every issued
        # record, every storage alias target and every folder-taxonomy category has a row; a
        # row writes one of the 13 SharePoint choices and one retention choice; only OTHER is
        # named DOC. document-types.ts@f3e3a53:45-46 (bc144cd TASK-3105) gives the internal
        # REFERENCE category its own GMP row, so it is no longer OTHER for 3 years.
        rows = taxonomy.DOCUMENT_TYPES
        needed = ({c for routes in taxonomy.ENTITY_ROUTES.values() for c in routes}
                  | set(taxonomy.ISSUED_RECORD_CATEGORIES)
                  | set(taxonomy.DOCTYPE_TO_STORAGE.values())
                  | set(taxonomy.STORAGE_CATEGORIES))
        self.assertEqual(needed - set(rows), set())
        self.assertEqual((rows["REFERENCE"]["code"], taxonomy.document_category_column("REFERENCE"),
                          taxonomy.retention_for_category("REFERENCE")),
                         ("REF", "ARTWORK", "7 years"))
        self.assertTrue(all(r["column"] in taxonomy.DOCUMENT_CATEGORY_CHOICES and
                            r["retention"] in taxonomy.RETENTION_CATEGORY_CHOICES
                            for r in rows.values()))
        self.assertEqual({c for c, r in rows.items() if r["code"] == "DOC"}, {"OTHER"})
        for choice in taxonomy.DOCUMENT_CATEGORY_CHOICES:
            self.assertEqual(rows[choice]["column"], choice)

    def test_release_and_finance_categories_come_from_the_one_portal_route_table(self):
        # entity-routing.ts@549f6f0 ENTITY_ROUTES: any ship-release spelling stores as
        # SHIP_RELEASE (TASK-2399); finance and release rows share one table.
        release = ("SUPPLY_CHAIN", "Customers/{customerFolder}/{ref}")
        for spelling in ("ShipRelease", "ship_release", " SHIP_RELEASE "):
            self.assertEqual(taxonomy.canonical_entity_type(spelling), "SHIP_RELEASE")
        self.assertEqual(taxonomy.ENTITY_ROUTES["SHIP_RELEASE"]["PICKUP_SHEET"], release)
        self.assertEqual(taxonomy.SHIP_RELEASE_CATEGORIES,
                         ("CUSTOMER_EMAIL", "PICKUP_SHEET", "CASE_LABELS"))
        self.assertEqual(taxonomy.canonical_entity_type("SalesOrder"), "SalesOrder")
        # A formula sheet (TASK-2506) files with the formula's other Quality documents.
        self.assertEqual(taxonomy.ENTITY_ROUTES["FORMULA"]["FORMULA_SHEET"],
                         ("QUALITY", "Formulas/{ref}"))
        self.assertEqual(taxonomy.ENTITY_ROUTES["SUPPLIER"]["W9_TAX"],
                         ("FINANCE", "Suppliers/{supplierFolder}"))
        # entity-routing.ts@f3e3a53:256-257 (66d08fb TASK-3075): every file on a credit
        # application, OTHER included, routes to the Finance library. OTHER is every
        # entity's fallback column value, so it never classifies a file FINANCE by
        # itself: the Finance library's class does (securityClassForRoute :312-314).
        self.assertEqual(taxonomy.ENTITY_ROUTES["CREDIT_APPLICATION"]["OTHER"],
                         ("FINANCE", "Customers/{customerFolder}"))
        self.assertEqual(taxonomy.FINANCE_CATEGORIES, {
            category for routes in taxonomy.ENTITY_ROUTES.values()
            for category, (library, _) in routes.items() if library == "FINANCE"} - {"OTHER"})
        other = {"drive_id": "d-1", "path": "Customers/Acme/CA-1/note.pdf", "etag": "e1",
                 "list_item_fields": {"source": taxonomy.LIST_ITEM_FIELDS_SOURCE,
                                      "drive_etag": "e1",
                                      "fields": {"DocumentCategory": "OTHER"}}}
        self.assertEqual(taxonomy.file_security_class(other, {"d-1": "STANDARD"}),
                         ("STANDARD", None))
        self.assertEqual(taxonomy.file_security_class(other, {"d-1": "FINANCE"}),
                         ("FINANCE", None))
        # entity-routing.ts@19f1d94: requirement evidence on materials (TASK-2685), the
        # ingredient list on a formula (TASK-2745), a sample photo on one artwork version
        # (TASK-2788); the page entity rule keeps an artwork version Internal.
        self.assertEqual(taxonomy.ENTITY_ROUTES["RAW_MATERIAL"]["IFRA"],
                         ("QUALITY", "Materials/{ref}"))
        self.assertEqual(taxonomy.ENTITY_ROUTES["FORMULA"]["INGREDIENT_LIST"],
                         ("QUALITY", "Formulas/{ref}"))
        self.assertEqual(taxonomy.ENTITY_ROUTES["ARTWORK_VERSION"],
                         {"ARTWORK_SAMPLE": ("PRODUCT_DEVELOPMENT",
                                             "Customers/{customerFolder}/{project}")})
        self.assertEqual(taxonomy.build_attachment_metadata("ARTWORK_VERSION", "ARTWORK_SAMPLE",
                                                            {"project": "PRJ-7"}),
            {"PartyType": "Internal", "DocumentCategory": "ARTWORK", "DocStatus": "Pending Review",
             "Source": "Staff", "RetentionCategory": "7 years", "Confidentiality": "Internal",
             "ProjectRef": "PRJ-7"})
        self.assertEqual(taxonomy.FINANCE_CATEGORIES, {"W9_TAX", "BANK_REFERENCE", "TAX_FORM",
                                                       "BUSINESS_REGISTRATION", "FINANCIAL_STMT"})
        self.assertEqual(taxonomy.build_attachment_metadata("ShipRelease", "CUSTOMER_EMAIL",
            {"customer": "Cloth Chronicles", "customerCode": "CC01", "ref": "SR-100042"}),
            {"PartyType": "Customer", "DocumentCategory": "OTHER",
             "DocStatus": "Pending Review", "Source": "Staff", "PartyRef": "CC01",
             "ItemRef": "SR-100042", "CustomerRef": "Cloth Chronicles",
             # a release email is a shipping record: GMP 7 years since TASK-2594
             "RetentionCategory": "7 years", "Confidentiality": "Customer Confidential"})

    def test_project_outlook_pin_files_as_a_product_development_project_email(self):
        # document-types.ts@b4118a3:41 and entity-routing.ts@b4118a3:236-237 (TASK-3207): a
        # pinned Outlook email on a project is an EML project row and files in the Product
        # Development customer -> project folder, never the Finance library.
        self.assertEqual(taxonomy.DOCUMENT_TYPES["OUTLOOK_PIN"],
                         {"code": "EML", "column": "OTHER", "retention": "3 years"})
        self.assertEqual(taxonomy.ENTITY_ROUTES["RD_PROJECT"]["OUTLOOK_PIN"],
                         ("PRODUCT_DEVELOPMENT", "Customers/{customerFolder}/{project}"))
        self.assertEqual(taxonomy.build_attachment_metadata("RD_PROJECT", "OUTLOOK_PIN",
            {"customer": "Cloth Chronicles", "customerCode": "", "project": "PD-26-100006"}),
            {"PartyType": "Customer", "DocumentCategory": "OTHER",
             "DocStatus": "Pending Review", "Source": "Staff", "ProjectRef": "PD-26-100006",
             "CustomerRef": "Cloth Chronicles", "RetentionCategory": "3 years",
             "Confidentiality": "Customer Confidential"})
        # OUTLOOK_PIN is routed on RD_PROJECT only; the Finance set does not move.
        self.assertEqual([e for e, routes in taxonomy.ENTITY_ROUTES.items()
                          if "OUTLOOK_PIN" in routes], ["RD_PROJECT"])
        self.assertEqual(taxonomy.FINANCE_CATEGORIES, {"W9_TAX", "BANK_REFERENCE", "TAX_FORM",
                                                       "BUSINESS_REGISTRATION", "FINANCIAL_STMT"})

    def test_batch_waiver_files_as_a_quality_batch_document(self):
        # document-types.ts@1d3eaf2:93 and entity-routing.ts@1d3eaf2:177 (e2728afe6 TASK-1929):
        # a waiver is a 7-year GMP WVR row with no column choice of its own, filed with the
        # batch's Quality documents next to its CoA, never the Finance library.
        self.assertEqual(taxonomy.DOCUMENT_TYPES["WAIVER"],
                         {"code": "WVR", "column": "OTHER", "retention": "7 years"})
        self.assertEqual(taxonomy.ENTITY_ROUTES["BATCH"]["WAIVER"],
                         ("QUALITY", "Batches/{year}/{ref}"))
        self.assertEqual(taxonomy.ENTITY_ROUTES["BATCH"]["WAIVER"],
                         taxonomy.ENTITY_ROUTES["BATCH"]["COA"])
        self.assertEqual(taxonomy.build_attachment_metadata("BATCH", "WAIVER",
            {"ref": "B-26-0001", "year": "2026"}),
            {"PartyType": "Internal", "DocumentCategory": "OTHER",
             "DocStatus": "Pending Review", "Source": "Staff", "ItemRef": "B-26-0001",
             "RetentionCategory": "7 years", "Confidentiality": "Internal"})
        # WAIVER is routed on BATCH only; the Finance set does not move.
        self.assertEqual([e for e, routes in taxonomy.ENTITY_ROUTES.items()
                          if "WAIVER" in routes], ["BATCH"])
        self.assertEqual(taxonomy.FINANCE_CATEGORIES, {"W9_TAX", "BANK_REFERENCE", "TAX_FORM",
                                                       "BUSINESS_REGISTRATION", "FINANCIAL_STMT"})

    def test_document_type_and_category_maps(self):
        self.assertEqual(taxonomy.resolve_doc_type("spec sheet"), "SPECIFICATION")
        self.assertEqual(taxonomy.resolve_doc_type("party:vendor"), "OTHER")
        self.assertEqual(taxonomy.resolve_doc_type("Mystery"), "OTHER")
        self.assertEqual(taxonomy.sharepoint_document_type("SPECIFICATION"), "OTHER")
        self.assertEqual(taxonomy.resolve_storage_category("Invoice"), "COMMERCIAL_INVOICE")
        self.assertEqual(taxonomy.party_type_for_category("QC_SIGNOFF"), "Internal")
        self.assertEqual(taxonomy.party_type_for_category("ORDER_CONFIRMATION"), "Supplier")
        self.assertEqual(taxonomy.party_type_for_category("UNSEEN"), "Customer")
        # folder-taxonomy.ts@19f1d94: ARTWORK files on LABEL_PROOF's customer -> project tree
        # (e8a89d5 TASK-1379), with its sample photos and templates (TASK-2788, TASK-2787);
        # board references and requirement evidence (TASK-2684) stay Internal.
        for category, party in (("ARTWORK", "Customer"), ("ARTWORK_SAMPLE", "Customer"),
                                ("ARTWORK_TEMPLATE", "Customer"),
                                ("ARTWORK_REFERENCE", "Internal"), ("IFRA", "Internal")):
            self.assertEqual(taxonomy.party_type_for_category(category), party, category)
        self.assertEqual(set(taxonomy.PRISMA_DOCUMENT_TYPES), set(taxonomy.DOCTYPE_TO_SP))
        for context in taxonomy.ENTITY_CONTEXTS.values():
            self.assertTrue(set(context["categories"]) <= taxonomy.STORAGE_CATEGORIES)
        self.assertEqual([c["name"] for c in taxonomy.COLUMNS],
            ["DocumentType", "ReviewStatus", "ExpiryDate", "CustomerRef", "ProjectRef",
             "RetentionCategory", "PartyType", "PartyRef", "DocumentCategory", "ItemRef",
             "DocStatus", "Source", "Confidentiality", "CustomerVisible", "ProductRef",
             "OrderRef"])


class NoWritePathTest(unittest.TestCase):
    def test_modules_import_no_http_or_network_code(self):
        root = Path(__file__).resolve().parents[1] / "operating"
        for name in ("doc_metadata.py", "doc_metadata_taxonomy.py"):
            tree = ast.parse((root / name).read_text())
            imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                        if isinstance(node, ast.Import) for alias in node.names}
            imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree)
                         if isinstance(node, ast.ImportFrom)}
            self.assertFalse(imported & {"urllib", "http", "socket", "ssl", "requests",
                                         "subprocess"}, name)
            self.assertNotIn("integrations.graph", (root / name).read_text())

    def test_proposals_read_without_writing(self):
        fixture = Fixture()
        ao, erp, oe, customer, bulk = RICARDO_AOS[0]
        text, rows = ao_packet(ao, erp, oe, customer, bulk)
        doc_id = fixture.document("qcqa-drive:ao", attributes(text, "AO-26005B.pdf",
                                                              SITE + "AO-26005B.pdf", rows=rows))
        before = copy.deepcopy(fixture.records)
        result = proposals_for(fixture, QA, doc_id)
        self.assertEqual(result["schema"], doc_metadata.SCHEMA)
        self.assertEqual(before, fixture.records)


if __name__ == "__main__":
    unittest.main()
