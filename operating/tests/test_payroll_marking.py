"""Payroll and HR-confidential documents are ``finance`` and ``pii`` (made-up content only)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from operating.chunking.sensitive import folders, labelled, personal_financial

PAYSTUB = """Northwind Widgets Ltd.  Statement of Earnings and Deductions
Employee: Pat Example   Employee No. 0042   Pay Period: 2026-02-01 to 2026-02-14
Pay Date: 2026-02-20
Regular Earnings 80.00 hrs 2,400.00   YTD 9,600.00
Gross Pay 2,400.00
CPP 131.00   EI 39.00   Federal Income Tax 290.00   Provincial Tax 120.00
Total Deductions 580.00
Net Pay 1,820.00"""
OFFER = """Dear Sam Sample,
We are delighted to present this offer of employment for the position of Production
Planner, reporting to the Operations Manager. Your starting salary will be $68,000 per
annum, with a start date of March 2, 2027 and a three-month probationary period. You will
be eligible for our group benefits plan. Please sign to confirm acceptance of this offer."""
TERMINATION = """Re: Termination of employment
This letter confirms the termination of your employment effective today. You will receive
your final pay, including outstanding vacation pay, and a Record of Employment will be
issued. Please return all company property."""
INVOICE = """INVOICE 10045  Invoice Date 2026-03-01  Terms: Net 30
Bill To: Contoso Foods   Ship To: Plant 2
Item 4471 Corrugated box 12x12  Qty 300  Unit 1.25  Amount 375.00
Subtotal 375.00  GST 18.75  Total 393.75  Balance due 393.75  YTD purchases 4,200.00"""
PURCHASE_ORDER = """PURCHASE ORDER PO100231  Order Date 2026-04-02
Vendor: Fabrikam Packaging  Payment terms: Net 45  Requested receipt 2026-04-20
Line 1 Stretch film 18in  Qty 40 rolls  Unit cost 22.00  Line total 880.00
Tax 114.40  Total 994.40   We are pleased to offer you a 2% discount for early payment."""


class ClassifierTests(unittest.TestCase):

    def test_a_paystub_is_payroll(self):
        self.assertEqual(personal_financial(PAYSTUB), "payroll")

    def test_an_offer_letter_and_a_termination_letter(self):
        self.assertEqual(personal_financial(OFFER), "offer_letter")
        self.assertEqual(personal_financial(TERMINATION), "termination_letter")

    def test_a_payroll_label_marks_whatever_the_text(self):
        self.assertTrue(labelled(["/sites/ConfidentialPayrollInformation/Shared Documents"]))
        self.assertTrue(labelled(["HR_Confidential"]))
        self.assertEqual(personal_financial("Q3 schedule", labels=folders(
            "/ConfidentialPayrollInformation/2026/notes.txt")), "label")
        self.assertFalse(labelled(["Confidential", "Payroll", "Operations"]))
        # the file name is never a label
        self.assertEqual(folders("/Shared/Confidential Payroll.pdf"), ["Shared"])

    def test_a_title_supports_but_never_decides(self):
        self.assertIsNone(personal_financial("Meeting moved to Tuesday", title="paystub.pdf"))
        self.assertEqual(personal_financial("Net Pay 1,820.00", title="Feb paystub.pdf"),
                         "payroll")

    def test_a_sin_near_its_name(self):
        self.assertEqual(personal_financial("Employee SIN: 130 692 544 on file"), "sin_number")
        self.assertIsNone(personal_financial("Lot 130 692 544 shipped"))

    def test_an_invoice_and_a_po_are_not_marked(self):
        self.assertIsNone(personal_financial(INVOICE))
        self.assertIsNone(personal_financial(PURCHASE_ORDER))
        self.assertIsNone(personal_financial("Can you approve the payroll run for Friday?"))


class TextIndexTests(unittest.TestCase):

    def test_the_text_index_marks_a_sensitive_documents_fields(self):
        from ontology import text_index
        field = lambda path, text: SimpleNamespace(citation="sharepoint:/d/1", field_path=path,
                                                   text=text)
        stub = SimpleNamespace(citation="sharepoint:/d/1", kind="sharepoint_file",
                               sender_class=None,
                               fields=[field("row.name", "doc.pdf"), field("row.text", PAYSTUB)])
        self.assertTrue(text_index._text_facts(stub)["sensitive"])
        stub.fields = [field("row.name", "doc.pdf"), field("row.text", INVOICE)]
        self.assertFalse(text_index._text_facts(stub)["sensitive"])


class RemarkTests(unittest.TestCase):
    """The re-mark command on a scratch database."""

    @classmethod
    def setUpClass(cls):
        try:
            import psycopg
            from operating.tests import pg_testdb
            cls.name, cls.dsn = pg_testdb.create_db()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"no scratch Postgres: {type(exc).__name__}")
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        from operating.pg.migrate import migrate
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            migrate(pg)

    def setUp(self):
        import psycopg
        self.pg = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.pg.close)
        self.pg.execute("TRUNCATE search.chunks, search.entities")
        rows = [
            ("c1", "sharepoint:/d/stub", "SharePointFile:stub", "sharepoint", PAYSTUB[:200]),
            ("c2", "sharepoint:/d/stub#page=2", "SharePointFile:stub", "sharepoint",
             PAYSTUB[200:]),
            ("c3", "sharepoint:/d/offer", "SharePointFile:offer", "sharepoint", OFFER),
            ("c4", "sharepoint:/d/inv", "SharePointFile:inv", "sharepoint", INVOICE),
            ("c5", "sharepoint:/d/po", "Attachment:po", "attachment", PURCHASE_ORDER),
            ("c6", "sharepoint:/d/lab", "SharePointFile:lab", "sharepoint", "Q3 staff notes"),
            ("c7", "api:rec", "RelSageArCmm:x", "record_text", PAYSTUB),
        ]
        from operating.chunking import content_sha
        header = "Source: SharePoint\nWhere: /Shared/2026/file.pdf"
        for cid, handle, oid, kind, body in rows:
            self.pg.execute(
                "INSERT INTO search.chunks (chunk_id, source_handle, object_id, kind, title, "
                "header, body, content_sha, audience_key, authority, updated_at) VALUES "
                "(%s, %s, %s, %s, 'file.pdf', %s, %s, %s, 'sp:Operations', 1, now())",
                (cid, handle, oid, kind, header, body, content_sha(header, body)))
        self.pg.execute("INSERT INTO search.entities (entity_id, object_id, type, name, "
                        "normalized, audience_key) VALUES ('e1', 'SharePointFile:stub', "
                        "'person', 'Pat Example', 'pat example', 'sp:Operations')")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.capture = str(Path(tmp.name) / "capture.db")
        with sqlite3.connect(self.capture) as db:
            db.execute("CREATE TABLE items (l1_handle TEXT, record TEXT)")
            db.execute("INSERT INTO items VALUES (?, ?)", ("sharepoint:/d/lab", json.dumps(
                {"original_url": "https://t.sharepoint.com/sites/ConfidentialPayrollInformation"
                                 "/Shared%20Documents/notes.docx"})))

    def marks(self):
        return {cid: list(m) for cid, m in self.pg.execute(
            "SELECT chunk_id, markings FROM search.chunks")}

    def test_dry_run_counts_then_apply_marks_once(self):
        from operating.chunking import remark
        dry = remark.run(self.dsn, capture=self.capture, apply=False, workers=2)
        self.assertEqual(dry["documents_to_mark"], 3)
        self.assertEqual(dry["chunks_to_mark"], 4)
        self.assertEqual(dry["by_reason"], {"payroll": 1, "offer_letter": 1, "label": 1})
        self.assertTrue(all(m == [] for m in self.marks().values()))   # nothing written
        done = remark.run(self.dsn, capture=self.capture, apply=True, workers=2)
        self.assertEqual(done["chunks_to_mark"], 4)
        marks = self.marks()
        for cid in ("c1", "c2", "c3", "c6"):
            self.assertEqual(marks[cid], ["finance", "pii"], cid)
        for cid in ("c4", "c5", "c7"):   # an invoice, a PO, a record's own text
            self.assertEqual(marks[cid], [], cid)
        self.assertEqual(self.pg.execute("SELECT markings FROM search.entities").fetchone()[0],
                         ["finance", "pii"])
        again = remark.run(self.dsn, capture=self.capture, apply=True, workers=2)
        self.assertEqual(again["chunks_to_mark"], 0)
        self.assertEqual(again["documents_already_marked"], 3)

    def test_documents_marked_beyond_the_reader_are_withheld(self):
        from operating.chunking import remark
        from operating.search import document_marks
        remark.run(self.dsn, capture=self.capture, apply=True, workers=1)
        ids = ["SharePointFile:stub", "SharePointFile:inv", "RelSageArCmm:x"]
        self.assertEqual(document_marks.withheld(ids, [], conn=self.pg),
                         {"SharePointFile:stub"})
        self.assertEqual(document_marks.withheld(ids, ["finance", "pii"], conn=self.pg), set())
        broken = SimpleNamespace(execute=lambda *a: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(document_marks.withheld(ids, [], conn=broken),
                         {"SharePointFile:stub", "SharePointFile:inv"})   # fail closed

    def test_a_rewrite_keeps_a_re_marked_documents_markings(self):
        from operating.chunking import Chunk, remark
        from operating.embed import store as st
        remark.run(self.dsn, capture=self.capture, apply=True, workers=1)
        row = self.pg.execute(
            "SELECT chunk_id, source_handle, object_id, kind, title, author_role, occurred_at, "
            "header, body, content_sha, linked_ids, audience_key, authority FROM search.chunks "
            "WHERE chunk_id = 'c6'").fetchone()
        names = ("chunk_id", "source_handle", "object_id", "kind", "title", "author_role",
                 "occurred_at", "header", "body", "content_sha", "linked_ids", "audience_key",
                 "authority")
        fields = {k: v for k, v in zip(names, row) if k != "content_sha"}
        import psycopg
        with psycopg.connect(self.dsn) as conn:
            st.ChunkWriter(conn).write([Chunk(**{**fields, "occurred_at": None,
                                                 "linked_ids": tuple(fields["linked_ids"]),
                                                 "markings": ()})])
        self.assertEqual(self.marks()["c6"], ["finance", "pii"])


class MarkedAttachmentReadPathTests(unittest.TestCase):
    """The review's case: an attachment whose search chunks are finance and pii, read by
    a reader without pii through every tool that can return a document's identity or
    text. It is withheld as any unreadable object; a reader holding both markings, and
    the owner (every marking), read it as before."""

    @classmethod
    def setUpClass(cls):
        from tests import test_ontology_tools_evidence as fx
        from operating.tests.search_fixture import FixtureDb
        cls.fx = fx
        cls.db = FixtureDb().__enter__()
        cls.db.add([
            dict(chunk_id="a-1#0", source_handle=fx.A1_H + "&page=1", object_id=fx.A1,
                 kind="attachment", title="packing-list.pdf",
                 header="Attachment 'packing-list.pdf'.", body="Packing list. Net Pay 1,820.00",
                 audience_key="mail:mbx-a", authority=2, markings=["finance", "pii"]),
            dict(chunk_id="m-1#0", source_handle=fx.M1_H, object_id=fx.M1, kind="mail",
                 title=fx.SUBJECT, header=f"Email '{fx.SUBJECT}'.",
                 body="The packing list is attached.", audience_key="mail:mbx-a",
                 authority=2)])

    @classmethod
    def tearDownClass(cls):
        cls.db.__exit__(None, None, None)

    def setUp(self):
        import os
        from unittest import mock
        from operating.search import document_marks
        patch = mock.patch.dict(os.environ, {"PLX_SEARCH": "hybrid", "PLX_PG_DSN": self.db.dsn,
                                             "PLX_RERANK": "off"})
        patch.start()
        self.addCleanup(patch.stop)
        document_marks.forget()
        self.addCleanup(document_marks.forget)

    def tools(self, markings):
        from operating import ontology_tools as ot
        fx = self.fx
        tools = fx.tools(reader=_MailReader(fx))
        tools.base.policy = ot.ReadPolicy(tenant="plx", audiences=dict(fx.POLICY_AUDIENCES),
                                          markings=markings)
        return tools

    def seen(self, tools, actor):
        """What each tool gives ``actor`` of the attachment."""
        fx = self.fx
        out = {}
        found = tools.base.call("plx.find", {"query": "packing-list.pdf"}, actor)
        out["find"] = fx.A1 in json.dumps(found)
        obj = tools.base.call("plx.object", {"object_id": fx.A1}, actor)
        out["object"] = "error" not in obj
        if "error" in obj:
            self.assertEqual(obj["error"]["code"], "object_not_found")
        read = tools.call("plx.read", {"citation": fx.A1_H}, actor)
        out["read"] = read.get("status") == "resolved"
        if not out["read"]:
            self.assertEqual(read.get("status"), "withheld")
            self.assertNotIn("Net Pay", json.dumps(read))
        mail = fx.ok(self, "plx.read", tools.call("plx.read", {"citation": fx.M1_H}, actor))
        out["mail_attachments"] = fx.A1_H in json.dumps(mail.get("attachments") or [])
        around = tools.traverse.call("plx.around", {"from": fx.M1, "path": [
            {"link_type": "Attachment.attached_to", "direction": "reverse"}]}, actor)
        out["around"] = fx.A1 in json.dumps(around)
        timeline = tools.traverse.call("plx.timeline", {"from": fx.A1}, actor)
        out["timeline"] = "error" not in timeline
        documents = tools.call("plx.documents", {"query": "packing list",
                                                  "sources": ["graph_mail"]}, actor)
        out["documents"] = fx.A1_H in json.dumps(documents)
        return out

    def test_a_marked_attachment_is_withheld_on_every_read_path(self):
        fx = self.fx
        tools = self.tools({"finance": {"full"}})          # FULL: finance, not pii
        for actor in (fx.OPS, fx.FULL):
            seen = self.seen(tools, actor)
            self.assertEqual({k for k, v in seen.items() if v}, set(), (actor, seen))
        # the message itself is still read in full
        mail = tools.call("plx.read", {"citation": fx.M1_H}, fx.OPS)
        self.assertEqual(mail["status"], "resolved")

    def test_a_reader_holding_the_markings_and_the_owner_see_it_everywhere(self):
        from unittest import mock
        from ontology.store_ddl import MARKINGS
        fx = self.fx
        both = self.seen(self.tools({"finance": {"full"}, "pii": {"full"}}), fx.FULL)
        self.assertTrue(all(both.values()), both)
        owner = self.tools({m: {"full"} for m in MARKINGS})
        with mock.patch(
                "operating.search.document_marks._MARKED.get",
                side_effect=AssertionError("the owner costs no read")):
            seen = self.seen(owner, fx.FULL)
        self.assertTrue(all(seen.values()), seen)

    def test_search_down_withholds_files_but_never_from_the_owner(self):
        import os
        from unittest import mock
        from ontology.store_ddl import MARKINGS
        fx = self.fx
        with mock.patch.dict(os.environ, {"PLX_PG_DSN": "postgresql:///plx_no_such_db_d"}):
            tools = self.tools({"finance": {"full"}, "pii": {"full"}})
            self.assertEqual(tools.call("plx.read", {"citation": fx.A1_H}, fx.FULL)["status"],
                             "withheld")
            owner = self.tools({m: {"full"} for m in MARKINGS})
            self.assertEqual(owner.call("plx.read", {"citation": fx.A1_H}, fx.FULL)["status"],
                             "resolved")


class _MailReader:
    """M1 with A1 listed as its attachment, and A1's text."""

    def __init__(self, fx):
        self.fx = fx

    def read(self, handle, actor, *, page=None):
        fx = self.fx
        base = {"status": "resolved", "lineage": "mail", "digest": "sha256:" + "b" * 64,
                "document": True, "gaps": [], "content_citation": handle,
                "content_field": "row.body_text"}
        if handle == fx.M1_H:
            return dict(base, fields={"row.subject": fx.SUBJECT},
                        content="The packing list is attached.",
                        attachments=[{"citation": fx.A1_H, "content_type": "application/pdf",
                                      "text_read": True, "name": {
                            "untrusted_text": "packing-list.pdf",
                            "origin": {"citation": fx.A1_H, "field_path": "row.name"}}}])
        if handle == fx.A1_H:
            return dict(base, fields={"row.name": "packing-list.pdf"},
                        content="Packing list. Net Pay 1,820.00",
                        content_field="row.content_text")
        return {"status": "unresolvable", "reason": "unknown"}

    def originals(self, actor, limit):
        return []


if __name__ == "__main__":
    unittest.main()
