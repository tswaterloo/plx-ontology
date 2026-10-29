"""Original roles are filled only by a RECORDED document kind (operating.playbook_originals).

Evidence label: ``injected-transport``. Cases run through the real ``MCPServer`` and
``CaseRuntime`` with the real GET-only ``SharePointReader`` (attach, original reads and the
listItem column read) over the in-process ``FakeSender``; employee sessions resolve through the
real ``EmployeePermissionResolver`` over Portal/Entra fixtures. Extraction text is a synthetic
stand-in passed through the store's extractor hook. No network, no credential.

Every probe of review rounds 1-5 of the parked keyword design must fail closed: with no
recorded kind it is ``role_unverifiable``; with a wrong recorded kind the text's title lines
make it ``document_kind_ambiguous``.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import json
import re
import unittest
from unittest.mock import patch

from integrations.document_understanding import ExtractedDocument, extract_document
from integrations.http import SourceUnavailable
from integrations.live_source_guard import LiveSourceAudience, NotVisible
from ontology.core import Actor
from operating import doc_metadata_taxonomy as taxonomy
from operating import kind_confirmation as kc
from operating import mcp_server
from operating import playbook_eval as pe
from operating import playbook_originals as po
from operating.case_runtime import CaseRuntime, build_live_readers
from operating.employee_permissions import EmployeePermissionPolicy, EmployeePermissionResolver
from operating.playbooks import Playbook
from operating.tests.test_case_originals import ocr_scan
from operating.tests.test_case_live_citations import (DRIVE, ENV, LIVE_AUDIENCE, SP_BASE,
                                                      LiveCitationTestCase, j)
from operating.tests.test_employee_case_runtime import CASE_POLICY, RICARDO_PERMS
from operating.tests.test_employee_permissions import OID, QA_GROUP, Calls, member, portal_user
from operating.tests.test_playbook_eval import REGISTRY, current_copy

AO = current_copy(REGISTRY.get("ao_quantity_reconciliation", 1))
EMPLOYEE_P = "employee:" + OID
EMPLOYEE = Actor("plx", frozenset({EMPLOYEE_P}))
SP_IDS = {"siteId": "site-1", "listId": "list-1", "listItemId": "7"}

# Synthetic text of the documents in the round 1-5 probes.
PROFORMA = ("PROFORMA INVOICE\nNo. PI-SWC024613\nYour Purchase Order No.: PO5447\n"
            "Total US$40,100.00\nPayment: 30% deposit")
PI_AS_QUOTATION = ("QUOTATION\nPI-25240\nTotal US$40,100.00\n30% deposit, balance against BL "
                   "copy\nFCL sea shipment, transit time approx. 30 days, B/L fee USD 50")
FORWARDER_QUOTE = ("OCEANLINK LOGISTICS\nFREIGHT QUOTATION No. OL000\nMode: AIR  Origin: NGB "
                   "Destination: IAD\nChargeable weight 412 kg\nAll-in rate USD 6.85/kg\n"
                   "Ref: PI-SWC024613")
GOODS_QUOTE_BL_FEE = ("QUOTATION No. Q-0925\nBottle 250 ml USD 0.12\nFOB Ningbo, B/L fee USD 50 "
                      "for buyer\nTHC for buyer account\nFreight quote to follow")
QUOTE_RE_PO = "QUOTATION No. Q-0925\nRe: your PURCHASE ORDER 5447\nBottle 250 ml USD 0.12"
ORDER_ACK = "ORDER ACKNOWLEDGEMENT\nCustomer Purchase Order: PO5447\nShip 25 Oct"
FINAL_INVOICE = "INVOICE No. SWC024999\nAgainst PI-SWC024613\nBalance US$28,070.00"
QC_SHEET = "FINAL INSPECTION SHEET\nAssembly Order: AO-26005B\nFilled 1502"
PORTAL_PO = ("PETRA premium care for skin and hair\nP.O. NUMBER PO5447\nPO Date: 2026-09-14\n"
             "TO: SWC Packaging\nTOTAL $40,100.00 USD")
COA = "CERTIFICATE OF ANALYSIS\nLot AO-26005B\nAppearance: conforms"


def with_roles(*roles):
    """The AO playbook with extra ``original`` evidence roles: ((role, required), ...)."""
    definition = copy.deepcopy(AO.definition)
    definition["version"] = definition["version"] + 100
    for role, required in roles:
        definition["required_evidence"].append({
            "role": role, "relation": "original", "identity": "any", "required": required,
            "key_hint": f"attached original recorded as {role}"})
    return Playbook(definition)


class TablesTest(unittest.TestCase):
    """The Portal mirror: which recorded column values name a kind."""

    def test_only_categories_that_name_the_document_establish_a_kind(self):
        reads = {value: po.column_reading("DocumentCategory", value)
                 for value in ("SIGNED_PO", "CUSTOMER_PO", "BOL", "COA", "QUOTE",
                               "ORDER_CONFIRMATION", "FILL_RECORD", "OTHER", "TECH_TRANSFER",
                               "Freight Quote", "")}
        self.assertEqual(reads["SIGNED_PO"], {"reads_as": "establishes",
                                              "kinds": ["purchase_order_pdf"]})
        self.assertEqual(reads["CUSTOMER_PO"]["kinds"], ["customer_purchase_order"])
        self.assertEqual(reads["BOL"]["kinds"], ["shipping_document"])
        self.assertEqual(reads["COA"]["kinds"], ["qc_record"])
        # a Portal "Quote" may be a goods quotation or a forwarder's: never a freight quote
        self.assertEqual(reads["QUOTE"], {"reads_as": "consistent",
                                          "kinds": ["freight_quote", "proforma_invoice"]})
        self.assertEqual(reads["ORDER_CONFIRMATION"]["reads_as"], "consistent")
        self.assertEqual(reads["FILL_RECORD"], {"reads_as": "consistent", "kinds": ["ao_scan"]})
        self.assertEqual(reads["OTHER"]["reads_as"], "no_information")
        self.assertEqual(reads[""]["reads_as"], "no_information")
        self.assertEqual(reads["TECH_TRANSFER"], {"reads_as": "another_document", "kinds": []})
        self.assertEqual(reads["Freight Quote"], {"reads_as": "unrecognised", "kinds": []})
        # legacy DocumentType: QUOTE / INVOICE are written as OTHER (tag-taxonomy DOCTYPE_TO_SP)
        self.assertEqual(po.column_reading("DocumentType", "OTHER")["reads_as"], "no_information")
        self.assertEqual(po.column_reading("DocumentType", "NDA")["reads_as"], "another_document")
        self.assertEqual(po.column_reading("ContentType", "PLX CoA")["kinds"], ["qc_record"])
        self.assertEqual(po.column_reading("ContentType", "Document")["reads_as"],
                         "no_information")

    def test_a_column_choice_reads_as_every_category_that_writes_it(self):
        """TASK-2594: the upload door writes a category's short column choice, so the choice
        may stand for any category whose document-types row writes it. It establishes a kind
        only when every such category does, and is never another document class while one
        of them may be a kind (FILL_RECORD, written as BATCH_RECORD, may be an AO scan)."""
        writers: dict[str, set[str]] = {}
        for category, row in taxonomy.DOCUMENT_TYPES.items():
            writers.setdefault(row["column"], set()).add(category)
        self.assertEqual(set(writers), set(taxonomy.DOCUMENT_CATEGORY_CHOICES))
        for choice, categories in writers.items():
            read = po.column_reading("DocumentCategory", choice)
            for category in categories:
                own = po.column_reading("DocumentCategory", category)
                with self.subTest(choice=choice, category=category):
                    if read["reads_as"] == "establishes":
                        self.assertEqual(own, read)
                    self.assertLessEqual(set(own["kinds"]), set(read["kinds"]))
        self.assertEqual(po.column_reading("DocumentCategory", "BATCH_RECORD"),
                         {"reads_as": "consistent", "kinds": ["ao_scan"]})
        self.assertEqual(po.column_reading("DocumentCategory", "ISSUED_COMMERCIAL_INVOICE"),
                         po.column_reading("DocumentCategory", "COMMERCIAL_INVOICE"))

    def test_no_portal_value_establishes_a_pi_a_freight_quote_or_an_ao_scan(self):
        """Those kinds have no Portal category: only a human confirmation fills them."""
        established = set()
        # Issued renders (ISSUED_PO, ...) are Portal values too (issued-record-categories.ts).
        for column, values in (("DocumentCategory", taxonomy.STORAGE_CATEGORIES
                                | set(taxonomy.DOCUMENT_TYPES)
                                | set(taxonomy.ISSUED_RECORD_CATEGORIES)
                                | set(taxonomy.SHIP_RELEASE_CATEGORIES)),
                               ("DocumentType", taxonomy.SP_DOCUMENT_TYPES),
                               ("ContentType", list(taxonomy.CONTENT_TYPES) + ["Document"])):
            for value in values:
                read = po.column_reading(column, value)
                self.assertNotEqual(read["reads_as"], "unrecognised", (column, value))
                if read["reads_as"] == "establishes":
                    established |= set(read["kinds"])
        self.assertTrue({"proforma_invoice", "freight_quote", "ao_scan"}.isdisjoint(established))
        self.assertEqual(established, {"purchase_order_pdf", "customer_purchase_order",
                                       "shipping_document", "qc_record"})
        self.assertEqual(po.KIND_COLUMNS, ("DocumentCategory", "DocumentType", "ContentType"))
        # A ship release's request files (Portal 8f9009d) are another document class:
        # they name no kind and fill no role.
        for category in ("CUSTOMER_EMAIL", "PICKUP_SHEET", "CASE_LABELS"):
            self.assertEqual(po.column_reading("DocumentCategory", category),
                             {"reads_as": "another_document", "kinds": []})
        # Every Portal citation names the one PORTAL_COMMIT.
        cited = {commit for sources in po.PORTAL_SOURCES.values() for source in sources
                 for commit in re.findall(r"@([0-9a-f]{7,40})\b", source)}
        self.assertEqual(cited, {po.PORTAL_COMMIT})
        self.assertTrue(all("@" in source for sources in po.PORTAL_SOURCES.values()
                            for source in sources))

    def test_roles_map_to_kinds(self):
        self.assertEqual(po.ROLE_DOCUMENTS["vendor_pi"], "proforma_invoice")
        self.assertEqual(po.ROLE_DOCUMENTS["po_evidence"], "customer_purchase_order")
        self.assertEqual(po.ROLE_DOCUMENTS["ao_original"], "ao_scan")
        self.assertEqual(po.ROLE_DOCUMENTS["shipping_documents"], "shipping_document")

    def test_title_lines_only_contradict_and_reference_lines_are_not_titles(self):
        self.assertEqual(po.contradictions("freight_quote", PI_AS_QUOTATION),
                         [{"line": "PI-25240", "kinds": ["proforma_invoice"]}])
        self.assertEqual(po.contradictions("proforma_invoice", FINAL_INVOICE),
                         [{"line": "INVOICE No. SWC024999", "kinds": ["shipping_document"]}])
        self.assertEqual([c["line"] for c in po.contradictions("ao_scan", QC_SHEET)],
                         ["FINAL INSPECTION SHEET"])
        self.assertEqual([c["line"] for c in po.contradictions("purchase_order_pdf",
                                                                QUOTE_RE_PO)],
                         ["QUOTATION No. Q-0925"])
        self.assertEqual([c["line"] for c in po.contradictions("purchase_order_pdf",
                                                                ORDER_ACK)],
                         ["ORDER ACKNOWLEDGEMENT"])
        # reference lines on the right document never contradict
        self.assertEqual(po.contradictions("proforma_invoice", PROFORMA), [])
        self.assertEqual(po.contradictions("freight_quote", FORWARDER_QUOTE), [])
        self.assertEqual(po.contradictions("purchase_order_pdf", PORTAL_PO), [])
        for label in ("Re: your PURCHASE ORDER 5447", "Customer Purchase Order: PO5447",
                      "Assembly Order: AO-26005B", "Against PI-SWC024613", "Ref: PI-25240",
                      "Sea freight to Baltimore: TBC", "B/L fee USD 50"):
            self.assertEqual(po.title_lines(label), [], label)
        # letter-spaced and zero-width OCR titles are still titles
        self.assertEqual(po.title_lines("P R O F O R M A  I N V O I C E")[0]["kinds"],
                         ["proforma_invoice"])
        self.assertEqual(po.title_lines("PRO​FORMA")[0]["kinds"], ["proforma_invoice"])


class RecordedKindTest(LiveCitationTestCase):
    """End to end: attach, record (or not) a kind, evaluate through the employee's session."""

    def setUp(self) -> None:
        super().setUp()
        self.pages: list[str] = ["ASSEMBLY ORDER\nForm #072B\nAO-26005B"]
        self.item["sharepointIds"] = dict(SP_IDS)
        self.columns: dict = {}
        self.sender.routes[f"{SP_BASE}/items/AO1/permissions"] = lambda url: j(200, RICARDO_PERMS)
        self.sender.routes[f"{SP_BASE}/items/AO1/listItem"] = lambda url: j(200, {
            "id": "7", "eTag": "\"list,1\"", "sharepointIds": dict(SP_IDS),
            "fields": dict(self.columns, FileLeafRef="AO-26005B.pdf")})
        self.runs = 0
        self.fresh()
        self.store = kc.KindConfirmations(self.root / "kind-confirmations.sqlite")

    def extract(self, content, mime_type, *, original_url, filename="", local_pdf_ocr=False):
        text, spans, start = "", [], 0
        for number, page in enumerate(self.pages, start=1):
            text += page + "\n"
            spans.append({"page": number, "start": start, "end": start + len(page)})
            start = len(text)
        return ExtractedDocument("application/pdf", "x", "extracted", "stub", text, (), (), (),
                                 tuple(spans))

    def fresh(self) -> None:
        """A new runtime and state directory (originals are content-addressed)."""
        self.runs += 1
        live = build_live_readers(
            LiveSourceAudience(LIVE_AUDIENCE, tenant="plx",
                               identities={"staff:quality": ["ricardo@petralabx.com"]},
                               source_acl_required=[f"sharepoint:{DRIVE}:*"]),
            ENV, graph_sender=self.sender, bc_sender=self.sender)
        resolver = EmployeePermissionResolver(EmployeePermissionPolicy.from_json(CASE_POLICY),
                                              Calls(portal_user("MANAGER")),
                                              Calls(member(QA_GROUP)))
        self.runtime = CaseRuntime(self.root / f"state-{self.runs}", client=None,
                                   audience=self.mirror_audience, tenant="plx",
                                   originals_options={"extractor": self.extract}, live=live,
                                   employee_permissions=resolver)
        self.addCleanup(self.runtime.close)
        self.employee = self.server(self.runtime, frozenset({EMPLOYEE_P}))
        self.case_id = self.open_case(self.employee)

    # ------------------------------------------------------------------ helpers

    def attach_sharepoint(self) -> dict:
        return self.ok(self.employee, "plx.case_attach_original",
                       {"case_id": self.case_id, "drive_id": DRIVE, "item_id": "AO1"})["original"]

    def attach_inbox(self, name: str = "doc.pdf") -> dict:
        inbox = self.runtime.originals.inbox / "staff:quality"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / name).write_bytes(b"%PDF-1.4\n% synthetic " + name.encode() + b"\n")
        return self.ok(self.employee, "plx.case_attach_original",
                       {"case_id": self.case_id, "inbox_name": name})["original"]

    def confirm(self, original: dict, kind: str, actor: Actor = EMPLOYEE) -> None:
        """The person answers the Workbench's confirmation form (operating.kind_confirmation)
        with ``kind`` as the original's document kind."""
        sent: list = []
        kc.call_tool({"citation": original["citation"], "columns": [kc.DOCUMENT_KIND],
                      "proposed_document_kind": kind}, confirmations=self.store,
                     runtime=self.runtime, actor=actor,
                     client_capabilities={"elicitation": {}}, send_request=sent.append)
        outcome = kc.handle_response(
            {"jsonrpc": "2.0", "id": sent[0]["id"], "result": {"action": "accept", "content": {
                kc.DOCUMENT_KIND: kind, "confirm": True}}},
            confirmations=self.store, runtime=self.runtime, actor=actor)
        self.assertTrue(outcome["confirmed"], outcome)

    def evaluate(self, *roles, fields="runtime", principals=None, confirmations="store"
                 ) -> dict:
        if fields == "runtime":
            fields = po.runtime_document_fields(self.runtime, EMPLOYEE)
        read = lambda name, args: self.ok(self.employee, name, args)  # noqa: E731
        if confirmations == "store":
            confirmations = kc.confirmed_kinds(self.store, EMPLOYEE)
        detail: dict = {}
        report = pe.evaluate(with_roles(*roles), read, self.case_id,
                             reader_principals=principals, document_fields=fields,
                             kind_confirmations=confirmations, originals_detail=detail)
        # the stored report keeps only the public summary; assertions read the session detail
        self.assertEqual(report["original_checks"], po.public_original_checks(detail))
        return {**report, "public_original_checks": report["original_checks"],
                "original_checks": detail}

    @staticmethod
    def gaps(report) -> list:
        return sorted((g["gap"], g.get("handle") or g.get("role"))
                      for g in report["original_checks"]["gaps"])

    def filled(self, report, role) -> list:
        return next(r["filled_by"] for r in report["original_checks"]["roles"]
                    if r["role"] == role)

    # ------------------------------------------------------------------ SharePoint column

    def test_a_correctly_typed_sharepoint_item_fills_its_role(self):
        for text, category, role in ((PORTAL_PO, "SIGNED_PO", "purchase_order_pdf"),
                                     (COA, "COA", "quality_record"),
                                     ("BILL OF LADING\nB/L No. NGB123456", "BOL",
                                      "shipping_documents")):
            with self.subTest(category=category):
                self.fresh()
                self.pages, self.columns = [text], {"DocumentCategory": category,
                                                    "DocumentType": "OTHER",
                                                    "ContentType": "Document"}
                got = self.attach_sharepoint()
                report = self.evaluate((role, True))
                checks = report["original_checks"]
                self.assertTrue(checks["ok"], checks["gaps"])
                [doc] = checks["originals"]
                self.assertEqual((doc["document"], doc["source"]),
                                 (po.ROLE_DOCUMENTS[role], "sharepoint-drive"))
                self.assertEqual(doc["kind_sources"], [{
                    "source": "sharepoint_column", "kind": po.ROLE_DOCUMENTS[role],
                    "column": "DocumentCategory", "value": category}])
                self.assertEqual(self.filled(report, role), [got["citation"]])
                self.assertEqual(doc["sharepoint_columns"]["status"], "read")
                self.assertNotIn("FileLeafRef", json.dumps(doc["sharepoint_columns"]))
        # the column read is the one GET-only listItem URL, never a write
        self.assertTrue(all(call[0] == "GET" for call in self.sender.api_calls()))
        self.assertTrue(any(call[1].split("?")[0].endswith("/items/AO1/listItem")
                            for call in self.sender.api_calls()))

    def test_a_type_column_that_contradicts_the_text_is_ambiguous(self):
        self.pages, self.columns = [PROFORMA], {"DocumentCategory": "SIGNED_PO"}
        got = self.attach_sharepoint()
        report = self.evaluate(("purchase_order_pdf", True))
        self.assertEqual(self.gaps(report), [("document_kind_ambiguous", got["citation"]),
                                             ("role_not_filled", "purchase_order_pdf")])
        [doc] = report["original_checks"]["originals"]
        self.assertEqual(doc["contradictions"][0]["line"], "PROFORMA INVOICE")
        self.assertFalse(report["summary"]["passed_all"])

    def test_columns_that_disagree_or_are_not_portal_values_are_ambiguous(self):
        for columns in ({"DocumentCategory": "SIGNED_PO", "DocumentType": "COA"},
                        {"DocumentCategory": "SIGNED_PO", "DocumentType": "NDA"},
                        {"DocumentCategory": "SIGNED_PO", "ContentType": "Freight Quote"}):
            with self.subTest(columns=columns):
                self.fresh()
                self.pages, self.columns = [PORTAL_PO], columns
                got = self.attach_sharepoint()
                report = self.evaluate(("purchase_order_pdf", True))
                self.assertIn(("document_kind_ambiguous", got["citation"]), self.gaps(report))
                self.assertEqual(self.filled(report, "purchase_order_pdf"), [])

    def test_a_quote_category_never_fills_the_freight_role(self):
        """Probe: 'B/L fee' / 'THC' / 'Freight quote to follow' on a goods quotation filed under
        the Portal's Quote category (round 5)."""
        self.pages, self.columns = [GOODS_QUOTE_BL_FEE], {"DocumentCategory": "QUOTE"}
        got = self.attach_sharepoint()
        report = self.evaluate(("freight_quote", True))
        self.assertEqual(self.gaps(report), [("role_not_filled", "freight_quote"),
                                             ("role_unverifiable", got["citation"])])
        detail = next(g["detail"] for g in report["original_checks"]["gaps"]
                      if g["gap"] == "role_unverifiable")
        self.assertIn("DocumentCategory='QUOTE'", detail)
        self.assertIn("plx.document_fields", detail)
        self.assertIn("an agent never confirms", detail)
        self.assertNotIn("Confirmed document kind:", detail)  # no sentence for an agent to copy

    def test_a_column_edit_after_attach_still_binds_by_content_tag(self):
        self.pages, self.columns = [PORTAL_PO], {"DocumentCategory": "SIGNED_PO"}
        got = self.attach_sharepoint()
        self.item["eTag"] = "\"{AO1},2\""  # the column edit moves the eTag, not the cTag
        report = self.evaluate(("purchase_order_pdf", True))
        self.assertTrue(report["original_checks"]["ok"], report["original_checks"]["gaps"])
        self.assertEqual(self.filled(report, "purchase_order_pdf"), [got["citation"]])
        # new content: the column describes other bytes than the original's
        self.item["cTag"] = "\"c:{AO1},2\""
        report = self.evaluate(("purchase_order_pdf", True))
        self.assertIn(("document_type_item_changed", got["citation"]), self.gaps(report))
        self.assertEqual(self.filled(report, "purchase_order_pdf"), [])

    def test_a_host_without_the_column_reader_fills_nothing_from_columns(self):
        self.pages, self.columns = [PORTAL_PO], {"DocumentCategory": "SIGNED_PO"}
        got = self.attach_sharepoint()
        report = self.evaluate(("purchase_order_pdf", True), fields=None)
        self.assertEqual(self.gaps(report), [("role_not_filled", "purchase_order_pdf"),
                                             ("role_unverifiable", got["citation"])])
        self.assertIn("sharepoint_document_type_not_read", report["limits"])
        self.assertEqual(report["original_checks"]["originals"][0]["sharepoint_columns"]
                         ["status"], "not_read")

    def test_an_invisible_item_is_a_gap_and_an_outage_is_incomplete(self):
        self.pages, self.columns = [PORTAL_PO], {"DocumentCategory": "SIGNED_PO"}
        got = self.attach_sharepoint()

        def hidden(drive_id, item_id):
            raise NotVisible()

        def down(drive_id, item_id):
            raise SourceUnavailable("graph 503")
        report = self.evaluate(("purchase_order_pdf", True), fields=hidden)
        self.assertIn(("document_type_not_visible", got["citation"]), self.gaps(report))
        with self.assertRaises(pe.EvaluationError) as raised:
            self.evaluate(("purchase_order_pdf", True), fields=down)
        self.assertIn("evaluation_incomplete", str(raised.exception))

    # ------------------------------------------------------------------ human confirmation

    def test_a_human_confirmation_fills_its_role(self):
        self.pages = [PROFORMA]
        got = self.attach_inbox("pi.pdf")
        self.confirm(got, "proforma_invoice")
        report = self.evaluate(("vendor_pi", True), ("freight_quote", False))
        checks = report["original_checks"]
        self.assertTrue(checks["ok"], checks["gaps"])
        [doc] = checks["originals"]
        self.assertEqual(doc["document"], "proforma_invoice")
        [source] = doc["kind_sources"]
        self.assertEqual((source["source"], source["kind"], source["confirmed_by"],
                          source["channel"]),
                         ("workbench_confirmation", "proforma_invoice", EMPLOYEE_P,
                          "mcp_elicitation"))
        self.assertEqual(report["public_original_checks"]["originals"][0]["kind_sources"],
                         ["workbench_confirmation"])
        self.assertEqual(self.filled(report, "vendor_pi"), [got["citation"]])
        self.assertEqual(self.filled(report, "freight_quote"), [])
        self.assertEqual(doc["sharepoint_columns"], {"status": "no_source_item"})
        fill, unsure = po.role_fill(checks)
        self.assertEqual((fill["freight_quote"], unsure), (set(), False))

    def test_a_human_confirmation_and_a_consistent_column_fill_together(self):
        self.pages, self.columns = [FORWARDER_QUOTE], {"DocumentCategory": "QUOTE"}
        got = self.attach_sharepoint()
        self.confirm(got, "freight_quote")
        report = self.evaluate(("freight_quote", True))
        self.assertTrue(report["original_checks"]["ok"], report["original_checks"]["gaps"])
        self.assertEqual(self.filled(report, "freight_quote"), [got["citation"]])
        # a confirmation that disagrees with a column naming another kind is ambiguous
        self.fresh()
        self.pages, self.columns = [FORWARDER_QUOTE], {"DocumentCategory": "SIGNED_PO"}
        got = self.attach_sharepoint()
        self.confirm(got, "freight_quote")
        report = self.evaluate(("freight_quote", True))
        self.assertEqual(self.gaps(report), [("document_kind_ambiguous", got["citation"]),
                                             ("role_not_filled", "freight_quote")])

    def test_a_finding_never_confirms_a_kind(self):
        """The retired copyable statement is ordinary text: neither an employee's nor an
        agent's finding fills a role, and an agent session cannot use the form either."""
        self.pages = [FORWARDER_QUOTE]
        got = self.attach_inbox("quote.pdf")
        agent = self.server(self.runtime, frozenset({"agent:claude", "staff:quality",
                                                     "staff:shared"}))
        for server in (self.employee, agent):
            self.ok(server, "plx.case_add_finding", {
                "case_id": self.case_id, "kind": "fact", "status": "supported",
                "citations": [got["citation"]],
                "statement": f"Confirmed document kind: freight_quote. Confirmed by: "
                             f"{EMPLOYEE_P}."})
        self.assertFalse(hasattr(po, "confirmation_statement"))
        report = self.evaluate(("freight_quote", True))
        self.assertEqual(self.gaps(report), [("role_not_filled", "freight_quote"),
                                             ("role_unverifiable", got["citation"])])
        detail = next(g["detail"] for g in report["original_checks"]["gaps"]
                      if g["gap"] == "role_unverifiable")
        self.assertIn("plx.document_fields", detail)
        self.assertIn("a case finding never counts", detail)
        with self.assertRaises(kc.ConfirmationRefused):
            self.confirm(got, "freight_quote",
                         actor=Actor("plx", frozenset({EMPLOYEE_P, "agent:claude"})))
        # an evaluation that does not consult confirmations says so, and fills nothing
        self.confirm(got, "freight_quote")
        report = self.evaluate(("freight_quote", True), confirmations=None)
        self.assertIn(("role_unverifiable", got["citation"]), self.gaps(report))
        detail = next(g["detail"] for g in report["original_checks"]["gaps"]
                      if g["gap"] == "role_unverifiable")
        self.assertIn("Workbench confirmations were not consulted", detail)
        self.assertEqual(self.filled(self.evaluate(("freight_quote", True)), "freight_quote"),
                         [got["citation"]])

    def test_a_confirmation_counts_only_for_its_own_original(self):
        self.pages = [FORWARDER_QUOTE]
        one = self.attach_inbox("one.pdf")
        two = self.attach_inbox("two.pdf")
        self.confirm(one, "freight_quote")
        report = self.evaluate(("freight_quote", True))
        self.assertEqual(self.filled(report, "freight_quote"), [one["citation"]])
        self.assertIn(("role_unverifiable", two["citation"]), self.gaps(report))
        # a reader returning another original's confirmation is not trusted
        record = self.store.active("plx", one["original_id"])
        report = self.evaluate(("freight_quote", True), confirmations=lambda oid: record)
        self.assertIn(("role_unverifiable", two["citation"]), self.gaps(report))
        doc = next(d for d in report["original_checks"]["originals"]
                   if d["handle"] == two["citation"])
        self.assertEqual(doc["workbench_confirmation"], {"status": "not_this_original"})

    def test_the_stored_report_shows_no_original_detail(self):
        """The report PlaybookReportStore serves to the whole case
        audience carries statuses, kinds, counts and gap codes only."""
        self.pages, self.columns = [FORWARDER_QUOTE], {"DocumentCategory": "QUOTE",
                                                       "ItemRef": "PO5447"}
        got = self.attach_sharepoint()
        self.confirm(got, "freight_quote")
        report = self.evaluate(("freight_quote", True), ("vendor_pi", True))
        public = report["public_original_checks"]
        text = json.dumps(public)
        for secret in (got["citation"], got["original_id"], got["sha256"], "AO-26005B",
                       DRIVE, "AO1", "QUOTE", OID, "OCEANLINK"):
            self.assertNotIn(secret, text)
        self.assertEqual(public["roles"], [
            {"role": "freight_quote", "required": True, "document": "freight_quote",
             "status": "filled", "filled": 1},
            {"role": "vendor_pi", "required": True, "document": "proforma_invoice",
             "status": "role_not_filled", "filled": 0}])
        self.assertEqual(public["originals"], [{"status": "ok", "document": "freight_quote",
                                                "kind_sources": ["workbench_confirmation"]}])
        self.assertEqual(public["gaps"], [{"gap": "role_not_filled", "role": "vendor_pi"}])

    # ------------------------------------------------------------------ probes (wrong source)

    def test_round_probes_with_a_wrong_recorded_kind_are_ambiguous(self):
        """Rounds 1-5: a human (or a column) records the wrong kind; the text's title lines of
        another document class make it ambiguous, never a fill."""
        for text, kind, role in (
                (PI_AS_QUOTATION, "freight_quote", "freight_quote"),       # PI titled Quotation
                (FINAL_INVOICE, "proforma_invoice", "vendor_pi"),          # final invoice + PI
                (QC_SHEET, "ao_scan", "ao_original"),                      # 'AO-26005B' QC sheet
                (QUOTE_RE_PO, "purchase_order_pdf", "purchase_order_pdf"),  # 'Re: your PO'
                (ORDER_ACK, "customer_purchase_order", "po_evidence")):
            with self.subTest(kind=kind):
                self.fresh()
                self.pages = [text]
                got = self.attach_inbox()
                self.confirm(got, kind)
                report = self.evaluate((role, True))
                self.assertEqual(self.gaps(report), [("document_kind_ambiguous", got["citation"]),
                                                     ("role_not_filled", role)])
                self.assertEqual(self.filled(report, role), [])

    def test_round_probes_without_a_recorded_kind_are_unverifiable(self):
        for text, role in ((PI_AS_QUOTATION, "freight_quote"),
                           (GOODS_QUOTE_BL_FEE, "freight_quote"),
                           (FINAL_INVOICE, "vendor_pi"), (QC_SHEET, "ao_original"),
                           (QUOTE_RE_PO, "purchase_order_pdf"), (FORWARDER_QUOTE, "freight_quote"),
                           (PROFORMA, "vendor_pi")):
            with self.subTest(role=role, text=text[:20]):
                self.fresh()
                self.pages = [text]
                got = self.attach_inbox()
                report = self.evaluate((role, True))
                self.assertEqual(self.gaps(report), [("role_not_filled", role),
                                                     ("role_unverifiable", got["citation"])])
                # text is never read to decide a kind: no page beyond the cited ones
                self.assertEqual(report["original_checks"]["page_reads"], 0)

    # ------------------------------------------------------------------ budget and agents

    def test_a_long_confirmed_original_spends_the_budget_and_is_incomplete(self):
        self.pages = [FORWARDER_QUOTE] + ["rate table"] * 148 + ["PROFORMA INVOICE"]
        got = self.attach_inbox("long.pdf")
        self.confirm(got, "freight_quote")
        report = self.evaluate(("freight_quote", True))
        self.assertEqual(self.gaps(report), [("document_text_incomplete", got["citation"]),
                                             ("role_not_filled", "freight_quote")])
        self.assertEqual(report["original_checks"]["page_reads"], po.PAGE_READ_BUDGET)

    def test_a_scan_over_the_ocr_page_cap_is_incomplete_and_one_within_it_reads_whole(self):
        """The real extractor OCRs pages 1-200 of a 300-page scan;
        those pages are never the whole text (its page 250 is a PROFORMA title), so the
        recorded kind is not checked. A 150-page scan is read whole and fills its role. The
        page budget and call rate cover 300 pages: only the cap, never a budget, stops the read.
        An extraction stored before ``skipped_by_cap`` existed (its
        warning only) is refused as whole too; the attach finding names the pages read and
        skipped; plx.read serves the capped text with a typed ``skipped_by_cap`` gap, and a
        page past the cap (230) answers with that gap, not as a missing page."""
        def page_text(number):
            return (FORWARDER_QUOTE if number == 1 else
                    "PROFORMA INVOICE" if number == 250 else f"rate table {number}")

        def extract(content, mime_type, **kw):   # OCR enabled
            return extract_document(content, mime_type, **{**kw, "local_pdf_ocr": True})

        def stored_before_the_field(content, mime_type, **kw):   # the warning only
            return dataclasses.replace(extract(content, mime_type, **kw), skipped_pages=None)
        whole_budget = functools.partial(po.check_case_originals, page_budget=400)
        for count, extractor in ((300, extract), (300, stored_before_the_field),
                                 (150, extract)):
            with self.subTest(pages=count, extractor=extractor.__name__):
                self.extract = extractor
                self.fresh()
                with ocr_scan(count, page_text):
                    got = self.attach_inbox(f"scan-{count}.pdf")
                statement = json.dumps(self.ok(self.employee, "plx.case_get",
                                               {"case_id": self.case_id})["findings"])
                self.confirm(got, "freight_quote")
                with patch.object(pe, "check_case_originals", whole_budget), \
                        patch.object(mcp_server, "CALLS_PER_MINUTE", 10 ** 6):
                    report = self.evaluate(("freight_quote", True))
                    read = self.ok(self.employee, "plx.read", {"citation": got["citation"]})
                    past = self.ok(self.employee, "plx.read",
                                   {"citation": got["citation"], "page": 230})
                    search = self.ok(self.employee, "plx.documents",
                                     {"query": "PO5447", "sources": ["original"]})
                skipped = {g["subject"]: g for g in [*read["gaps"], *past["gaps"]]
                           if g["reason"] == "skipped_by_cap"}
                unsearched = [g["subject"] for g in search["gaps"]
                              if g["reason"] == "skipped_by_cap"]
                self.assertEqual(search["documents"], [])
                [doc] = report["original_checks"]["originals"]
                self.assertEqual((read["status"], past["status"]), ("resolved", "unresolvable"))
                self.assertIn(f"rate table {min(count, 200)}", read["content"]["untrusted_text"])
                if count == 300:
                    self.assertEqual(self.gaps(report),
                                     [("document_text_incomplete", got["citation"]),
                                      ("role_not_filled", "freight_quote")])
                    self.assertEqual((doc["status"], doc["document"]),
                                     (po.TEXT_INCOMPLETE, None))
                    self.assertIn("pages 201-300", json.dumps(report["original_checks"]))
                    self.assertEqual(report["original_checks"]["page_reads"], 0)
                    self.assertIn("200 page(s) read, 100 page(s) skipped by the 200-page cap "
                                  "(pages 201-300 not read)", statement)
                    self.assertEqual(sorted(skipped), sorted({got["citation"],
                                                              got["citation"] + "#page=230"}))
                    self.assertTrue(all("pages 201-300" in gap["detail"]
                                        for gap in skipped.values()))
                    # A search with no match on the pages read is not absence.
                    self.assertEqual(unsearched, [got["citation"]])
                    self.assertFalse(search["completeness"]["complete"])
                else:
                    self.assertTrue(report["original_checks"]["ok"],
                                    report["original_checks"]["gaps"])
                    self.assertEqual((doc["document"], doc["contradictions"]),
                                     ("freight_quote", []))
                    self.assertEqual(self.filled(report, "freight_quote"), [got["citation"]])
                    self.assertEqual(report["original_checks"]["page_reads"], 150)
                    self.assertIn("150 page(s), text", statement)
                    self.assertNotIn("skipped", statement)
                    self.assertEqual((skipped, unsearched), ({}, []))

    def test_an_agent_reader_reads_no_original(self):
        self.pages = [PROFORMA]
        got = self.attach_inbox("pi.pdf")
        self.confirm(got, "proforma_invoice")
        report = self.evaluate(("vendor_pi", True), principals=["agent:workbench"])
        self.assertEqual(report["original_checks"]["status"], "not_read_agent_identity")
        self.assertIn(("original_not_visible_to_agent", got["citation"]), self.gaps(report))
        self.assertEqual(self.filled(report, "vendor_pi"), [])


if __name__ == "__main__":
    unittest.main()
