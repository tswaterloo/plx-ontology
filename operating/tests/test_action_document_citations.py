"""A customer-PO proposal whose evidence is a byte-pinned document field, on the action path.

Directive 10c0a3786831: the missing-in-record draft (operating/attested_conflicts.py) cites
the PO number a captured customer PO states (``fields.po_number.value``) in a SharePoint
file version (``sharepoint://...#sha256=``) or a mail attachment
(``graph-mail://...#attachment=..&sha256=``) next to the Portal order that holds no PO.
``plx.act`` propose (the catalog hook's ``cited_field`` / ``cited_value`` traces) and the
playbook evaluator (its proposal guard re-check) read that field through the one
documentFields reader, ``capability_guards.current_reader`` for the session actor (pinned
bytes through the case runtime's capture gate, re-extracted by
``ontology.document_fields.pinned_document_row``): the proposal registers and scores clean
for a reader of the document and for nobody else, and bytes other than the pinned ones, or
a read of them the capture gate now refuses, refuse it. The approver's gate reason names the
file. Decision ``devloop/DECISIONS/2026-10-10-act-document-field-provenance.md``.

Evidence label: injected-transport (Workbench API: ``FakeWorkbench``; Graph mail and
SharePoint captures over the capture tests' in-process fakes) / isolated-fixture. No
network, no credentials, never live.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import unittest
from unittest import mock

from ontology import document_fields
from ontology.types import Actor
from operating import action_path as ap
from operating import capability_guards as cg
from operating import customer_po_coverage as cpo
from operating import mcp_server
from operating import playbook_eval as pe
from operating import playbook_tools as pt
from operating.cases import CitationUnresolvable
from operating.mcp_server import MCPServer
from operating.playbooks import PLAYBOOK_DIR
from operating.session_identity import CHANNEL_CONSOLE, CHANNEL_MCP, fixture_session
from operating.tests import test_customer_po_coverage as cpo_tests

CAPABILITY = "portal.sales_order.set_customer_po"
PO = "4500012345"
CONFIRMS = "a person at PLX confirms the evidence belongs to this order"
# Portal rows (public.*), mailbox A (staff:orders) and the Quality folder (staff:sales).
READER = frozenset({"staff:operations", "staff:orders", "staff:sales"})
NO_DOCUMENTS = frozenset({"staff:operations"})
EMPLOYEE = "orders.lead@example-plx.com"


def actor(principals):
    return Actor("plx", principals, "restricted")   # mail is classified restricted


def session(principals, channel=CHANNEL_MCP):
    return fixture_session("plx", principals, channel=channel, clearance="restricted",
                           subject_email=EMPLOYEE)


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "the cited documents are PDFs: needs pypdf and reportlab")
class DocumentCitedProposalTests(unittest.TestCase):
    def setUp(self):
        from integrations import capture_sharepoint as cs, graph_read
        from integrations.capture_mail import MailCapture, MailCaptureStore
        from integrations.captured_text import byte_pinned
        from integrations.graph_read import GraphMailReader, SharePointReader
        from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
        from integrations.people_exclusion import PeopleExclusion
        from integrations.tests import test_capture_mail as tm
        from integrations.tests import test_capture_sharepoint as sp
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from operating.case_mail_scope import MailScope
        from operating.case_runtime import CaseRuntime, LiveReaders
        from operating.case_tools_host import CaseCaptures
        from operating.tests import test_attested_missing as am

        self.h = cpo_tests.CoverageTestCase("setUp")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        root = self.h.root
        self.pdf = am.pdf(am.customer_po(PO, "OR0003"))
        digest = hashlib.sha256(self.pdf).hexdigest()
        no_sleep = BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0)

        self.drive = sp.seeded_drive()
        self.drive.file("01CPO", "customer-po.pdf", "01AOS", self.pdf, acl=(sp.SALES,))
        self.sharepoint = cs.SharePointCaptureStore(root / "sharepoint", clock=sp.Clock())
        self.addCleanup(self.sharepoint.close)
        api, transport = graph_read.build_graph(sp.ENV, sender=self.drive, retry=no_sleep)
        self.sp_capture = cs.SharePointCapture(self.sharepoint, lambda d: SharePointReader(
            api, sp.capture_audience(), d, transport=transport, read_permissions=True,
            max_acl_reads=cs.MAX_ACL_READS), sp.CAPTURE)
        self.assertTrue(self.sp_capture.run(sp.scope())["complete"])
        self.sp_scope = sp.scope()
        self.sp_handle = byte_pinned(cs.version_handle(sp.DRIVE, "01CPO", '"{01CPO},1"'),
                                     digest)

        graph = tm.FakeGraph()
        graph.put(tm.BOX_A, "MSG-1", "CK-1", subject="Order", body="See PDF.",
                  attachments=[tm.file_attachment("ATT-1", self.pdf, "po.pdf")])
        mail = MailCaptureStore(root / "mail", clock=tm.Clock())
        self.addCleanup(mail.close)
        api, _ = graph_read.build_graph(tm.ENV, sender=graph, retry=no_sleep)
        self.assertTrue(MailCapture(mail, GraphMailReader(api, tm.capture_audience()),
                                    tm.CAPTURE, sleep=lambda s: None).run(tm.SCOPE)["complete"])
        (self.mail_handle,) = [row[0] for row in mail.db.execute(
            "SELECT l1_handle FROM attachments")]

        audience = LiveSourceAudience(
            {f"mail:{tm.UPN_A}": ["staff:orders"],
             f"sharepoint:{sp.DRIVE}:{sp.QUALITY_FOLDER}": ["staff:sales"],
             f"sharepoint:{sp.DRIVE}:{sp.QUALITY_FOLDER}/*": ["staff:sales"]},
            tenant="plx", identities={"staff:sales": [sp.SALES]},
            classifications={"mail:*": "restricted"},
            source_acl_required=[f"sharepoint:{sp.DRIVE}:*"])
        soon = dt.datetime(2026, 10, 1, 13, 0, tzinfo=dt.timezone.utc)
        self.runtime = CaseRuntime(
            self.h.state, client=WorkbenchReadApiClient(cpo_tests.BASE, cpo_tests.KEY,
                                                         fetch=self.h.fake),
            audience=self.h.audience_path, tenant="plx",
            mirror_options={"clock": self.h.clock, "retry_delay_seconds": 0},
            live=LiveReaders(audience=audience), mail_scope=MailScope.unrestricted(),
            people_exclusion=PeopleExclusion.from_env(tm.ENV))
        self.addCleanup(self.runtime.close)
        self.runtime.captures = CaseCaptures(self.runtime, mail_capture_dir=root / "mail",
                                             sharepoint_capture_dir=root / "sharepoint",
                                             clock=lambda: soon)
        self.path = ap.ActionPath(root / "actions.db", self.runtime.cases)
        self.blob = {self.sp_handle: self.sharepoint.blobs / digest[:2] / digest,
                     self.mail_handle: mail.blobs / digest[:2] / digest}
        # The file name each capture records for the version, and the pin's first digits.
        self.file = {self.sp_handle: f'file "customer-po.pdf" sha256 {digest[:12]}',
                     self.mail_handle: f'file "po.pdf" sha256 {digest[:12]}'}

    def server(self, principals):
        server = MCPServer(case_runtime=self.runtime, actor=actor(principals),
                           action_path=self.path, session=session(principals))
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "doc-act-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def propose(self, document):
        """A human-opened case on order 3 (no PO) and the missing-in-record proposal citing
        the PO the ``document`` citation states; recorded by the document's reader."""
        server = self.server(READER)
        (order,) = self.h.ok(server, "mirror.rows", {
            "relation": cpo.RELATION, "where": [["id", "eq", 3]]})["rows"]
        case_id = self.path.open_case(
            actor(READER), "Enter the customer PO on order OR0003?",
            objective_author_kind="human", session=session(READER, CHANNEL_CONSOLE))
        proposal = self.h.ok(server, "plx.case_propose", {
            "case_id": case_id, "capability": CAPABILITY,
            "payload": {"proposed_customer_po": PO, "evidence_citation": document,
                        "evidence_field": "fields.po_number.value"},
            "preconditions": [{"check": "field_empty", "citation": order["citation"],
                               "field": "po_reference"}, CONFIRMS],
            "citations": [document, order["citation"]],
            "expected_effect": {"relation": cpo.RELATION, "key": {"id": 3},
                                "values": {"po_reference": PO}}})["proposal"]
        return case_id, proposal["proposal_id"]

    def act(self, principals, case_id, proposal_id):
        return self.h.call(self.server(principals), "plx.act", {
            "mode": "propose", "case_id": case_id, "proposal_id": proposal_id})

    def act_status(self, principals, case_id):
        return self.h.call(self.server(principals), "plx.act",
                           {"mode": "status", "case_id": case_id})

    def claim(self, case_id, document):
        """A supported finding claiming the PO number ``document``'s row states."""
        return self.h.ok(self.server(READER), "plx.case_add_finding", {
            "case_id": case_id, "statement": f"The customer's PO states PO {PO}.",
            "kind": "fact", "status": "supported", "citations": [document],
            "value_claims": [{"value": PO, "citation": document,
                              "field": "row.fields.po_number.value",
                              "role": "identifier"}]})["finding"]["finding_id"]

    def findings(self, principals, case_id):
        """(finding id, bucket, citation statuses) the case shows ``principals``, and the
        claim failure codes their claim check reports."""
        server = self.server(principals)
        buckets = self.h.ok(server, "plx.case_get", {"case_id": case_id})["findings"]
        shown = sorted((item["id"], bucket, [cite["status"] for cite in item["citations"]])
                       for bucket, items in buckets.items() for item in items)
        checked = self.h.ok(server, "plx.case_get", {"case_id": case_id, "section": "claims"})
        return shown, [failure["code"] for failure in checked["failures"]]

    @contextlib.contextmanager
    def reextracted(self, field, value):
        """The pinned bytes re-extract (a new extractor, same bytes) with ``field`` now
        stating ``value``; the host's per-extraction memo starts empty on both sides."""
        real = document_fields.pinned_document_row

        def changed(*args, **kwargs):
            found = real(*args, **kwargs)
            if found.get("ok"):
                row = json.loads(json.dumps(found["row"]))
                row["fields"][field] = {**row["fields"].get(field, {}), "value": value}
                found = {**found, "row": row}
            return found

        self.runtime.captures._rows.clear()
        try:
            with mock.patch.object(document_fields, "pinned_document_row", changed):
                yield
        finally:
            self.runtime.captures._rows.clear()

    def test_a_re_extraction_moves_only_the_claims_on_the_field_it_changed(self):
        """Decision 2026-10-18-pinned-citation-digest-bytes: the pinned version's digest
        binds its bytes; the row they are re-extracted to rides in the view as derived. A
        re-extraction changing another field keeps the finding and plx.act; one changing
        the claimed PO fails that claim claim_value_mismatch, never content_changed."""
        case_id, proposal_id = self.propose(self.sp_handle)
        finding = self.claim(case_id, self.sp_handle)
        held = ([(finding, "supported", ["resolved"])], [])
        self.assertEqual(self.findings(READER, case_id), held)
        (other,) = [name for name in document_fields.pinned_document_row(
            self.sp_handle, self.sp_handle.split("#")[0], self.pdf, {"name": "po.pdf",
            "content_type": "application/pdf"})["row"]["fields"] if name != "po_number"][:1]
        with self.reextracted(other, "re-read differently"):
            self.assertEqual(self.findings(READER, case_id), held)
            registered = self.act(READER, case_id, proposal_id)
            self.assertFalse(registered["isError"], registered)
        case_id, proposal_id = self.propose(self.sp_handle)
        finding = self.claim(case_id, self.sp_handle)
        with self.reextracted("po_number", "4500099999"):
            self.assertEqual(self.findings(READER, case_id),
                             ([(finding, "supported", ["resolved"])],
                              ["claim_value_mismatch"]))
            self.assertIn("capability_guard_failed:value_in_cited_evidence",
                          self.refusal(self.act(READER, case_id, proposal_id)))

    def refusal(self, result):
        """The refusal text of a tool error (bare, or inside the error envelope)."""
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    def test_a_document_cited_proposal_registers_for_its_reader_only(self):
        for document, relation in ((self.sp_handle, "sharepoint.documentFields"),
                                   (self.mail_handle, "graph_mail.documentFields")):
            with self.subTest(document=relation):
                case_id, proposal_id = self.propose(document)
                # Who may not read the document is shown no proposal to register.
                self.assertIn("action_refused:proposal_withheld",
                              self.refusal(self.act(NO_DOCUMENTS, case_id, proposal_id)))
                # A field its profile proposes through no type supplies no value.
                with mock.patch.object(document_fields, "comparisons", return_value=[]):
                    self.assertIn("action_refused:parameter_untraced:proposed_customer_po:"
                                  "value_not_in_cited_field",
                                  self.refusal(self.act(READER, case_id, proposal_id)))
                registered = self.act(READER, case_id, proposal_id)
                self.assertFalse(registered["isError"], registered)
                intent = registered["structuredContent"]["intent"]
                self.assertEqual((intent["capability"], intent["state"],
                                  intent["expected_effect"]["values"]),
                                 (CAPABILITY, "proposed", {"po_reference": PO}))
                # The approver reads where the value came from: the document's field, and
                # which file it is (its recorded name and pinned digest).
                self.assertIn(f"value source: cited {relation} row.fields.po_number.value "
                              f"as identifier:customer_po; {self.file[document]}",
                              intent["gate_reason"])
                # Who may not read the document sees the intent's state only: no value,
                # no gate reason, no file.
                listed = self.act_status(NO_DOCUMENTS, case_id)
                self.assertFalse(listed["isError"], listed)
                self.assertEqual([(item["withheld"], item["state"]) for item in
                                  listed["structuredContent"]["intents"]], [(True, "proposed")])
                for leak in (PO, self.file[document].split('"')[1], intent["intent_id"]):
                    self.assertNotIn(leak, json.dumps(listed, ensure_ascii=False))

    def test_other_bytes_or_a_revoked_read_of_the_pinned_ones_refuse(self):
        from integrations.capture_sharepoint import SharePointCaptureStore

        @contextlib.contextmanager
        def changed_bytes(document):
            self.blob[document].write_bytes(self.pdf + b"\n")
            try:
                yield
            finally:
                self.blob[document].write_bytes(self.pdf)

        def unreadable(document):
            return mock.patch.object(SharePointCaptureStore, "blob", side_effect=OSError)

        def revoked(document):
            # The capture gate now refuses this reader the version the case still shows:
            # the read plx.act propose makes through capability_guards.current_reader.
            return mock.patch.object(self.runtime.captures, "document", side_effect=(
                CitationUnresolvable("capture_citation_unresolvable")))

        reader = actor(READER)
        for document, spoil in ((self.sp_handle, changed_bytes), (self.sp_handle, unreadable),
                                (self.sp_handle, revoked), (self.mail_handle, changed_bytes),
                                (self.mail_handle, revoked)):
            with self.subTest(document=document.split(":", 1)[0], read=spoil.__name__):
                case_id, proposal_id = self.propose(document)
                finding = self.claim(case_id, document)
                with spoil(document):
                    with self.assertRaises(ap.ActionPathError) as caught:
                        self.path.register(reader, case_id, proposal_id,
                                           session=session(READER),
                                           read=cg.current_reader(self.runtime, reader))
                    # A pinned SharePoint version whose bytes the gate cannot serve (other
                    # bytes, an unreadable blob) is an unavailable citation: the finding
                    # and the proposal are withheld. A mail attachment's view is the
                    # message row, and a revoked document read leaves the case's view
                    # resolved: those fail the parameter trace. plx.act refuses all.
                    unavailable = (document == self.sp_handle and spoil is not revoked)
                    self.assertEqual(
                        caught.exception.code,
                        "proposal_withheld" if unavailable else
                        "parameter_untraced:proposed_customer_po:citation_changed")
                    self.assertIn("capability_guard_failed:value_in_cited_evidence"
                                  if not unavailable else "action_refused:proposal_withheld",
                                  self.refusal(self.act(READER, case_id, proposal_id)))
                    if unavailable:
                        self.assertEqual(self.findings(READER, case_id)[0], [
                            (finding, "withheld", ["citation_unavailable"])])
                self.assertEqual(self.path.list_for_case(reader, case_id)["total"], 0)

    def test_a_mail_attachment_whose_original_was_dropped_still_reads_as_cited(self):
        """Compact mail storage (``integrations.capture_mail``): the capture dropped the
        attachment original once its text was read and kept the reading it made of the
        bytes. The claim and the proposal read the same field from that reading, for its
        reader only (``plx.act`` reads it through ``capability_guards.current_reader``);
        an altered record is refused like other bytes, never guessed."""
        from integrations import capture_mail, captured_text as ct
        from integrations.capture_mail import MailCaptureStore

        digest = self.mail_handle.rsplit("sha256=", 1)[1]
        store = MailCaptureStore(self.h.root / "mail")
        try:
            with mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 1):
                self.assertEqual(store.settle_original(digest), "dropped")
        finally:
            store.close()
        self.assertFalse(self.blob[self.mail_handle].exists())
        def fresh():
            """The readings made again from the store (no process memo)."""
            self.runtime.captures._rows.clear()
            stack = contextlib.ExitStack()
            stack.enter_context(mock.patch.object(ct, "_CACHE", ct._CACHE.empty()))
            stack.enter_context(mock.patch.object(document_fields, "_FIELDS",
                                                  document_fields._FIELDS.empty()))
            return stack

        with fresh():
            case_id, proposal_id = self.propose(self.mail_handle)
            self.assertIn("action_refused:proposal_withheld",
                          self.refusal(self.act(NO_DOCUMENTS, case_id, proposal_id)))
            registered = self.act(READER, case_id, proposal_id)
            self.assertFalse(registered["isError"], registered)
            self.assertIn(self.file[self.mail_handle],
                          registered["structuredContent"]["intent"]["gate_reason"])
        with fresh():
            case_id, proposal_id = self.propose(self.mail_handle)
        record = self.blob[self.mail_handle].with_name(digest + ct.DROPPED_SUFFIX)
        record.write_bytes(record.read_bytes()[:-4])
        with fresh():
            self.assertIn("capability_guard_failed:value_in_cited_evidence",
                          self.refusal(self.act(READER, case_id, proposal_id)))

    def test_a_follow_up_citing_the_document_registers_for_its_reader_only(self):
        for document in (self.sp_handle, self.mail_handle):
            with self.subTest(document=document.split(":", 1)[0]):
                case_id, _ = self.propose(document)
                server = self.server(READER)
                missing = "Who confirms the customer PO the document states for OR0003?"
                why = "The PO number is read from the customer's document only."
                gap = self.h.ok(server, "plx.act", {
                    "mode": "propose", "case_id": case_id,
                    "action": "workbench.follow_up.record",
                    "parameters": {"missing": missing, "why": why, "who_can_answer": ["Sales"],
                                   "state": "unknown", "citations": [document]}})
                follow_up = self.h.ok(server, "plx.case_propose", {
                    "case_id": case_id, "capability": ap.ESCALATION_CAPABILITY,
                    "payload": {"gap_id": gap["execution"]["receipt"]["gap_id"],
                                "title": missing, "description": why, "priority": "high"},
                    "citations": [document]})["proposal"]["proposal_id"]
                self.assertIn("action_refused:proposal_withheld",
                              self.refusal(self.act(NO_DOCUMENTS, case_id, follow_up)))
                registered = self.act(READER, case_id, follow_up)
                self.assertFalse(registered["isError"], registered)
                intent = registered["structuredContent"]["intent"]
                self.assertEqual((intent["capability"], intent["state"]),
                                 (ap.ESCALATION_CAPABILITY, "proposed"))

    def test_the_evaluator_scores_the_same_proposal_without_a_flag(self):
        playbook = pe.PlaybookRegistry().load_directory().get("customer_po_enrichment")
        for document in (self.sp_handle, self.mail_handle):
            with self.subTest(document=document.split(":", 1)[0]):
                case_id, proposal_id = self.propose(document)
                server = self.server(READER)

                def flags(**options):
                    report = pe.evaluate(playbook, lambda name, arguments: self.h.ok(
                        server, name, arguments), case_id, **options)
                    return [item["kind"] for item in report["authority_violation_details"]
                            if item["id"] == proposal_id]

                self.assertEqual(flags(document_rows=cg.current_reader(
                    self.runtime, actor(READER))), [])
                # No document read for the evaluator: its evidence is unread, never passed.
                self.assertEqual(flags(), ["capability_guard_failed"])

    def test_plx_playbook_evaluate_reads_the_document_as_plx_act_does(self):
        """The MCP evaluator path (``MCPServer._playbook_call`` -> ``plx.playbook_evaluate``):
        the host passes the evaluator's own ``document_rows``, so the stored reports of a
        frozen case with a document-cited proposal flag no guard failure."""
        from operator_ui.case_console import REPORTS_FILE, report_store_for
        case_id, proposal_id = self.propose(self.mail_handle)
        state = self.h.state
        config = self.h.root / "playbook-proposals.json"
        config.write_text(json.dumps({"schema": pt.CONFIG_SCHEMA,
                                      "stewards": ["employee:steward-1"],
                                      "frozen_cases": [case_id]}))
        config = pt.load_config(config)
        # Proposer and evaluator share no principal; the evaluator reads the document.
        proposer = actor({"service:proposer", "staff:finance"})
        evaluator = actor({"service:playbook-evaluator", *READER})

        def tool(who, name, arguments):
            server = MCPServer(case_runtime=self.runtime, actor=who, agent="plx.agent.playbook",
                               playbook_proposals=mcp_server.playbook_session_tools(
                                   self.runtime, who, state, config))
            server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "doc-eval-test", "version": "1"}}})
            server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return self.h.ok(server, name, arguments)

        evidence = self.path.open_case(
            proposer, "Does the order carry a customer PO?", objective_author_kind="human",
            session=fixture_session("plx", proposer.principals, channel=CHANNEL_CONSOLE,
                                    clearance="restricted", subject_email=EMPLOYEE))
        edit = json.loads((PLAYBOOK_DIR / "customer_po_enrichment_v3.json").read_text())
        edit.update(version=4, question_pattern=edit["question_pattern"] + " (reworded)")
        proposed = tool(proposer, pt.PROPOSE, {
            "file": "customer_po_enrichment_v4.json", "content": edit,
            "rationale": "reads clearer", "evidence": [evidence]})["proposal_id"]
        evaluated = tool(evaluator, pt.EVALUATE, {"proposal_id": proposed})
        self.assertEqual([(item["case_id"], item["before_outcome"] != "unscored",
                           item["after_outcome"] != "unscored") for item in evaluated["cases"]],
                         [(case_id, True, True)])
        reports = report_store_for(self.runtime, state / REPORTS_FILE, recorders=[evaluator])
        listed = reports.list(evaluator, case_id)
        self.assertEqual(len(listed), 2)   # before and after
        for summary in listed:
            details = reports.get(evaluator, summary["id"])["report"][
                "authority_violation_details"]
            self.assertEqual([item["kind"] for item in details if item["id"] == proposal_id],
                             [])


if __name__ == "__main__":
    unittest.main()
