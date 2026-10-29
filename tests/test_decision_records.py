"""Decision records read from documents (O2).

An approval, release, sign-off or ruling document mailed or filed in SharePoint is a
``DecisionRecord`` (profile ``decision_record``, ``ontology/schema/document_fields.json``):
what was decided, who decided, when and on what basis, each as written, and a blank field
its typed gap. Its ``po_reference`` / ``quote_reference`` / ``batch_reference`` mention the
record it is about (``decision_records``, lead tier), and ``plx.investigate``'s decision
plan (``ontology/plans/decision_evidence.json``) lists a document's decision next to the MC
approval gates and task events: its time re-read from the pinned bytes through the case
runtime's capture gate (``ontology_tools_investigate.citation_rows``), never from the
projection store. Crafted PDFs (reportlab) captured over the capture tests' injected Graph
transports; synthetic Portal purchase orders. No network, no credentials.
Evidence: injected-transport / isolated-fixture.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.tests import test_capture_mail as tm
from integrations.tests import test_capture_sharepoint as tsp
from ontology import document_fields, mentions, store_ddl
from ontology import schema as schema_loader
from ontology.project import MailCaptures, SharePointCaptures, build_store
from ontology.tests.test_document_fields import CONTRACTS, gold_po, pdf
from ontology.tests.test_mail_documents import StaticPortal
from ontology.types import Actor
from operating import decision_evidence as de
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt

NOW = dt.datetime(2026, 10, 1, 13, 0, tzinfo=dt.timezone.utc)
READER = Actor("plx", frozenset({"staff:orders", "staff:quality"}), "restricted")
MAIL_ONLY = Actor("plx", frozenset({"staff:orders"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:outsider"}), "restricted")

# Signed, dated and reasoned: a decision. Mailed.
SIGNED = [["BATCH RELEASE RECORD", "Release No: BRR-2026-031",
           "Subject: Release of batch B240915", "Batch: B240915", "PO Number: PO100234",
           "Decision: Released", "Released by: J. Okafor", "Signature: J. Okafor",
           "Release date: 2026-09-18", "Reason: CoA within spec; retained samples logged"]]
# A printed date and every approval field left blank: no decision, each blank its gap.
# Mailed.
BLANK = [["QC RELEASE FORM", "Form No: QRF-77", "Subject: Release of batch B240916",
          "PO Number: PO100234", "Date: 2026-09-19", "Decision:", "Approved by:",
          "Signature:", "Release date:", "Reason:"]]
# A ticked box, a named approver and no signature or reason. Filed in SharePoint.
TICKED = [["QUOTE APPROVAL", "Approval No: QA-12", "PO Number: PO100235",
           "Approved by: Dana Reyes", "Approval date: 2026-09-03", "[x] Approved",
           "[ ] Rejected"]]
# Printed date, pending status, no approver or signature: no decision. Mailed.
PENDING = [["QC RELEASE FORM", "Approval No: QRF-78", "Subject: Release of batch B240917",
            "PO Number: PO100237", "Date: 2026-09-20", "Status: Pending approval",
            "Approved by:", "Signature:", "Reason:"]]
# Both outcome boxes ticked: a decision whose outcome is a conflict. Mailed.
CONFLICT = [["QUOTE APPROVAL", "Approval No: QA-13", "PO Number: PO100236",
             "Rejected by: Dana Reyes", "Approval date: 2026-09-05", "[x] Approved",
             "[x] Rejected"]]
# Documents of other classes with an approval or release heading and a form number.
HEADED = [["PURCHASE ORDER", "PO Number: 4500000002", "Approval", "Form No: F-12",
           "Approved by: K. Lee"],
          ["BILL OF LADING", "B/L No: BL-778", "Release", "Ref No: R-55", "Release No: 9"],
          ["DELIVERY RECEIPT", "Authorization", "Form No: DR-3", "Received by: M. Ortiz"]]


def read(pages):
    return document_fields.extract_fields(pdf(pages), "application/pdf", filename="d.pdf")


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "decision records are read from PDFs: needs pypdf and reportlab")
class DecisionRecordFieldsTests(unittest.TestCase):
    def test_what_was_decided_who_when_and_why_are_read_as_written(self):
        found = read(SIGNED)
        self.assertEqual((found["status"], found["profile"]), ("classified", "decision_record"))
        values = {name: field["value"] for name, field in found["fields"].items()}
        self.assertEqual({k: values[k] for k in (
            "document_number", "subject", "outcome", "decided_by", "signature", "decided_on",
            "basis", "po_reference", "batch_reference")}, {
            "document_number": "BRR-2026-031", "subject": "Release of batch B240915",
            "outcome": "Released", "decided_by": "J. Okafor", "signature": "J. Okafor",
            "decided_on": "2026-09-18", "basis": "CoA within spec; retained samples logged",
            "po_reference": "PO100234", "batch_reference": "B240915"})
        # "Released by:" is the decider's label, never the "released" box.
        self.assertEqual(found["fields"]["released"]["gap"], "not_stated")

    def test_a_blank_approval_field_is_its_gap_never_a_decision(self):
        found = read(BLANK)
        self.assertEqual(found["profile"], "decision_record")
        for name in ("outcome", "decided_by", "signature", "decided_on", "basis"):
            with self.subTest(field=name):
                self.assertIsNone(found["fields"][name]["value"])
                self.assertEqual(found["fields"][name]["gap"], "value_missing")

    def test_a_printed_date_is_no_decision_time_and_a_rejecter_is_the_decider(self):
        fields = read(PENDING)["fields"]
        self.assertIsNone(fields["decided_on"]["value"])
        self.assertEqual(fields["outcome"]["value"], "Pending approval")
        self.assertEqual(read(CONFLICT)["fields"]["decided_by"]["value"], "Dana Reyes")

    def test_an_approval_heading_on_another_class_keeps_that_class(self):
        for page in HEADED:
            with self.subTest(title=page[0]):
                found = read([page])
                self.assertEqual(found["status"], "classified", found.get("gaps"))
                self.assertNotEqual(found["profile"], "decision_record")

    def test_a_ticked_box_is_the_outcome_and_an_approver_label_is_not_a_box(self):
        fields = read(TICKED)["fields"]
        self.assertEqual((fields["approved"]["value"], fields["rejected"]["gap"],
                          fields["decided_by"]["value"], fields["decided_on"]["value"]),
                         ("checked", "unchecked", "Dana Reyes", "2026-09-03"))
        self.assertEqual(fields["signature"]["gap"], "not_stated")

    def test_the_type_links_to_what_it_is_about_at_lead_tier_only(self):
        compiled = schema_loader.load_schema()
        links = {lid: link for lid, link in compiled.link_types.items()
                 if lid.startswith("DecisionRecord.")}
        self.assertEqual({link["to"] for link in links.values()},
                         {"PurchaseOrder", "SalesOrder", "Quote", "AssemblyOrder", "Batch",
                          "PurchaseRequisition"})
        for lid, link in links.items():
            with self.subTest(link=lid):
                self.assertEqual(link["reverse_name"], "decision_records")
                self.assertEqual([(b["kind"], b["tier_ceiling"]) for b in link["bindings"]],
                                 [("mention", "lead")])


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "decision records are read from PDFs: needs pypdf and reportlab")
class DecisionDocumentsInvestigatedTests(unittest.TestCase):
    """Two mailed decision records naming PO100234 (signed, blank), a pending one naming
    PO100237, an approval with both boxes ticked naming PO100236 and a ticked quote approval
    filed in SharePoint naming PO100235; the decision plan over each PO."""

    @classmethod
    def setUpClass(cls):
        from operating.case_mail_scope import MailScope
        from operating.case_runtime import CaseRuntime, LiveReaders
        from operating.case_tools_host import CaseCaptures
        from integrations.source_mirror import AudienceMap

        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        no_sleep = BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0)
        graph = tm.FakeGraph()
        graph.put(tm.BOX_A, "MSG-1", "CK-1", subject="Release", body="Records attached.",
                  attachments=[tm.file_attachment("ATT-1", pdf(SIGNED), "release.pdf"),
                               tm.file_attachment("ATT-2", pdf(BLANK), "qc form.pdf"),
                               tm.file_attachment("ATT-3", pdf(PENDING), "pending.pdf"),
                               tm.file_attachment("ATT-4", pdf(CONFLICT), "approval.pdf")])
        cls.mail = MailCaptureStore(root / "mail", clock=tm.Clock())
        api, _ = graph_read.build_graph(tm.ENV, sender=graph, retry=no_sleep)
        assert MailCapture(cls.mail, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
                           sleep=lambda s: None).run(tm.SCOPE)["complete"]
        cls.case = tsp.CaptureCase("run")
        cls.case.setUp()
        cls.case.drive.file("01QAPP", "quote approval.pdf", "01AOS", pdf(TICKED))
        assert cls.case.capture().run(tsp.scope())["complete"]
        cls.compiled = schema_loader.load_schema()
        cls.path = root / "ontology.sqlite"
        portal = StaticPortal([gold_po("p1", "PO100234"), gold_po("p2", "PO100235"),
                               gold_po("p3", "PO100236"), gold_po("p4", "PO100237")])
        with MailCaptures(root / "mail", internal_domains=tm.SCOPE.internal_domains) as mail, \
                SharePointCaptures(cls.case.state) as files:
            build_store(cls.path, cls.compiled, {"portal_api": portal, "graph_mail": mail,
                                                 "sharepoint": files}, clock=lambda: NOW,
                        post_project={"mentions": mentions.store_mention_links,
                                      "document_fields": document_fields.store_document_links})
        audience = LiveSourceAudience(
            {f"mail:{tm.UPN_A}": ["staff:orders"],
             f"sharepoint:{tsp.DRIVE}:{tsp.QUALITY_FOLDER}": ["staff:quality"],
             f"sharepoint:{tsp.DRIVE}:{tsp.QUALITY_FOLDER}/*": ["staff:quality"]},
            tenant="plx", identities={"staff:quality": [tsp.RICARDO]},
            classifications={"mail:*": "restricted"},
            source_acl_required=[f"sharepoint:{tsp.DRIVE}:*"])
        cls.runtime = CaseRuntime(root / "state", client=None,
                                  audience=AudienceMap({}, tenant="plx"), tenant="plx",
                                  live=LiveReaders(audience), mail_scope=MailScope.unrestricted(),
                                  people_exclusion=PeopleExclusion.from_env(tm.ENV))
        cls.runtime.captures = CaseCaptures(cls.runtime, mail_capture_dir=root / "mail",
                                            sharepoint_capture_dir=cls.case.state,
                                            clock=lambda: NOW)

    @classmethod
    def tearDownClass(cls):
        cls.runtime.close()
        cls.mail.close()
        cls.case.tearDown()
        cls.tmp.cleanup()

    def po(self, key):
        return f'PurchaseOrder:portal.app_purchase_order:{{"id":"{key}"}}'

    def table(self):
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:orders", "staff:quality", "staff:outsider"},
            "mail:*": {"staff:orders"}, "sharepoint:*": {"staff:quality"}},
            markings={m: {"staff:orders", "staff:quality"} for m in store_ddl.MARKINGS})
        mail_gate = self.mail.item_gate(tm.reader_audience())
        file_gate = self.case.store.item_gate(tsp.reader_audience())

        def gate(actor, handle):
            return (mail_gate if handle.startswith("graph-mail://") else file_gate)(actor,
                                                                                     handle)
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        base = ot.OntologyTools(reader, policy, item_gate=gate,
                                schema=ot.SchemaView.from_compiled(self.compiled), grammar=None,
                                contracts=CONTRACTS, clock=lambda: NOW)
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(table, rows=oi.citation_rows(self.runtime),
                                               scope=oi.read_scope(base), clock=lambda: NOW))
        return table

    def investigate(self, root, actor):
        out = self.table()["plx.investigate"]["handler"]({"family": "F11", "root": root}, actor)
        return out.get("structuredContent", out)

    def decisions(self, out):
        return [e for e in out["answer"] if e["name"].startswith("decisions_")
                and not e["name"].startswith("decisions_context_")]

    def test_a_signed_record_is_a_decision_and_a_blank_one_is_its_gap(self):
        out = self.investigate(self.po("p1"), READER)
        (decision,) = self.decisions(out)
        self.assertEqual(decision["value"], "2026-09-18")
        self.assertEqual(decision["inputs"], ["document_decided_on_1", "document_decided_by_1",
                                              "document_outcome_1", "document_basis_1"])
        self.assertTrue(decision["basis"][0].startswith("DecisionRecord:graph_mail."))
        gaps = {(g["reason"], g["state"]) for g in out["gaps"]}
        # The blank form, printed date and all, records no decision: its gap, the record
        # answering that nothing was decided there.
        self.assertIn(("decision_not_made_on_record", "absent_in_source"), gaps)
        self.assertNotIn("no_recorded_decision", {r for r, _ in gaps})
        # Re-read from the pinned bytes: no time is left unverified, no claim uncited.
        self.assertNotIn("decision_time_unverified", {g["reason"] for g in out["gaps"]})
        self.assertEqual(out["claim_checks"]["failures"], [])
        steps = {s["step"]: s for s in out["trace"]}
        self.assertEqual((steps["decision_docs"]["status"], steps["decision_docs"]["objects"]),
                         ("ok", 2))

    def test_a_ticked_box_names_the_outcome_and_an_unsigned_record_states_its_approver(self):
        out = self.investigate(self.po("p2"), READER)
        (decision,) = self.decisions(out)
        self.assertEqual(decision["value"], "2026-09-03")
        self.assertEqual(decision["inputs"], ["document_decided_on", "document_decided_by",
                                              "document_approved"])
        self.assertIn("decision_basis_not_recorded", {g["reason"] for g in out["gaps"]})
        self.assertNotIn("decision_time_unverified", {g["reason"] for g in out["gaps"]})
        self.assertEqual(out["claim_checks"]["failures"], [])

    def test_a_dated_pending_form_is_no_decision(self):
        out = self.investigate(self.po("p4"), READER)
        self.assertEqual(self.decisions(out), [])
        (made,) = [g for g in out["gaps"] if g["reason"] == "decision_not_made_on_record"]
        self.assertIn("outcome is open", made["detail"])
        self.assertIn("no_recorded_decision", {g["reason"] for g in out["gaps"]})

    def test_two_ticked_outcomes_are_a_conflict_never_one_picked(self):
        out = self.investigate(self.po("p3"), READER)
        (decision,) = self.decisions(out)
        self.assertEqual(decision["inputs"], ["document_decided_on", "document_decided_by",
                                              "document_approved", "document_rejected"])
        (conflict,) = [g for g in out["gaps"] if g["reason"] == "decision_outcome_conflict"]
        self.assertIn("document_approved", conflict["detail"])
        self.assertIn("document_rejected", conflict["detail"])

    def test_a_dated_blank_form_alone_is_no_recorded_decision(self):
        evidence = {"t": [{"name": "t", "object_id": "D:1", "value": "2026-09-20"}],
                    "a": [{"name": "a", "object_id": "D:1", "value": None}],
                    "o": [{"name": "o", "object_id": "D:1", "value": "Pending approval"}]}
        args = {"records": [{"label": "doc", "time": ["t"], "actor": ["a"], "outcome": ["o"],
                             "decided_by_any": ["actor", "outcome"],
                             "open_outcomes": ["pending"]}]}
        out = de.plan_run(args, evidence, [], items={}, name="decisions", as_of="2026-10-01",
                          resolver=None, actor=READER, complete=True, covers=lambda _: True)
        self.assertEqual([e for e in out["entries"] if e["name"] != "decisions"], [])
        self.assertEqual([g["reason"] for g in out["gaps"]],
                         ["decision_not_made_on_record", "no_recorded_decision"])

    def test_an_open_word_inside_a_decided_outcome_keeps_the_decision(self):
        record = {"open_outcomes": ["pending", "open", "draft"]}
        self.assertTrue(de._open("Pending approval", record))
        self.assertTrue(de._open({"untrusted_text": " Draft"}, record))
        self.assertFalse(de._open("Approved with open points", record))
        self.assertFalse(de._open("Approved - draft 2 artwork", record))

    def test_a_reader_of_neither_source_finds_no_document_decision(self):
        for root in (self.po("p1"), self.po("p2")):
            with self.subTest(root=root):
                out = self.investigate(root, OUTSIDER)
                self.assertEqual(self.decisions(out), [])
                (none,) = [g for g in out["gaps"] if g["reason"] == "no_recorded_decision"]
                self.assertIn("decision_document", none["searched"]["sources"])
        # The mail reader reads the mailed record, never the filed one.
        self.assertEqual(len(self.decisions(self.investigate(self.po("p1"), MAIL_ONLY))), 1)
        self.assertEqual(self.decisions(self.investigate(self.po("p2"), MAIL_ONLY)), [])

    def test_a_document_time_is_re_read_from_its_pinned_bytes(self):
        rows = oi.citation_rows(self.runtime)
        (pinned,) = [r[0] for r in self.mail.db.execute(
            "SELECT l1_handle FROM attachments WHERE l1_handle LIKE '%attachment=ATT-1&%'")]
        view = rows(pinned, READER)
        self.assertEqual(view["row"]["fields"]["decided_on"]["value"], "2026-09-18")
        self.assertIsNone(rows(pinned, OUTSIDER))
        entry = {"name": "t", "kind": "property", "value": "2026-09-18", "object_id": "D:1",
                 "citation": {"l1_handle": pinned,
                              "field_path": "row.fields.decided_on.value"}}
        run = de._Run({}, rows, READER, None)
        self.assertIsNotNone(run.reread(entry))
        self.assertIsNone(run.reread(dict(entry, value="2026-09-19")))
        self.assertIsNone(de._Run({}, rows, OUTSIDER, None).reread(entry))


if __name__ == "__main__":
    unittest.main()
