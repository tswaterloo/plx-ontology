"""Fill this form for PO 90412 (the Ask with the form attached, and ``POST /api/doc-fill``):
the filled copy and every blank with its source come back for review; nothing is saved,
uploaded or sent; the record, the file and its source stay the reader's own. Evidence:
isolated-fixture (the Ask's instruct harness)."""

from __future__ import annotations

import base64
import io
import zipfile

from operating import doc_fill as df
from operating import instruct_files as files
from operator_api.tests import test_instruct as ti
from operator_api.tests.test_instruct import ANA, BEN, FILE_H, MAIL, World

PO = "PurchaseOrder:90412"
PO_H = "bc://Production/c/purchaseOrders/90412@W%2F%22p1%22"
FORM = files.make("xlsx", "Supplier questionnaire\nPO Number:|\nSupplier Name:|\n"
                          "Contact email:|\nIncoterms:|\nSignature:|\nShoe size:|")


class FillWorld(World):
    excluded_value = False

    def record_facts(self, reader, object_type, object_id):
        if object_id != PO or reader.session.subject_oid != ANA[1] or not self.po_open:
            raise KeyError("object_not_found")
        rows = {"PurchaseOrder.po_number": "90412", "PurchaseOrder.vendor_name": "Maple Leaf",
                "PurchaseOrder.incoterms": "FCA"}
        facts = [df.Fact(k, v, df.Citation(PO, k.split(".")[1], PO_H)) for k, v in rows.items()]
        if self.excluded_value:   # a value naming a person kept out of every answer
            facts.append(df.Fact("Supplier.name", ti.EXCLUDED, df.Citation(
                "Supplier:V1", "name", "bc://Production/c/vendors/1@a")))
        from operator_api import models as m
        return facts, [], m.ObjectRef(object_id=PO, type="PurchaseOrder", label="PO 90412",
                                      open_url="/object/PurchaseOrder/PurchaseOrder:90412")


class DocFillApi(ti.InstructApi):
    def setUp(self):
        super().setUp()
        self.backend.__class__ = FillWorld      # the harness's world, with the PO's facts
        self.model = self.backend.instruct_call = ti.Model()   # never called for a fill

    def ask(self, client, text, context=None, form=FORM, name="Questionnaire.xlsx"):
        return self.instruct(client, text, context, attachments=[
            {"name": name, "data": base64.b64encode(form).decode()}])

    def test_a_filled_form_comes_back_for_review_with_every_source_and_nothing_saved(self):
        out = self.ask(self.ana, "fill this form for PO 90412")
        self.assertEqual(out.status_code, 200, out.text)
        fill = out.json()["fills"][0]
        got = {f["label"]: (f["status"], f["after"]) for f in fill["fields"]}
        self.assertEqual(got, {"PO Number": ("filled", "90412"),
                               "Supplier Name": ("filled", "Maple Leaf"),
                               "Contact email": ("unknown_field", ""),
                               "Incoterms": ("filled", "FCA"),
                               "Signature": ("person_only", ""),
                               "Shoe size": ("unknown_field", "")})
        for f in fill["fields"]:
            if f["status"] == "filled":
                self.assertEqual((f["citation"]["record"], f["citation"]["source"]), (PO, PO_H))
                self.assertIn("/source?handle=", f["citation"]["open_url"])
        self.assertEqual((fill["name"], fill["kind"], fill["record"]["object_id"]),
                         ("Questionnaire (filled).xlsx", "xlsx", PO))
        # every filled value is an answer fact cited by its source, as any answer's is
        self.assertEqual(len(out.json()["answer"]["facts"]), 3)
        self.assertEqual(self.model.prompts, [])
        # no folder named: nothing to upload; the copy is Ana's alone to download
        self.assertIsNone(fill["upload_id"])
        mine = self.ana.get(fill["download_url"])
        self.assertEqual(mine.status_code, 200)
        self.assertNotEqual(mine.content, FORM)
        self.assertEqual(self.ben.get(fill["download_url"]).status_code, 404)
        again = {f.label: f.current for f in df.open_document(mine.content,
                                                              df.load_terms()).fields()}
        self.assertEqual(again["PO Number"], "90412")
        self.assertEqual(again["Shoe size"], "")

    def test_a_record_or_folder_the_reader_cannot_open_fills_nothing(self):
        # Ben: PO 90412 is not his to read; nothing is filled or kept for him
        out = self.ask(self.ben, "fill this form for PO 90412")
        self.assertNotEqual(out.status_code, 200)
        self.assertEqual(self.backend._uploads()._rows(), [])
        # Ana with no record named, or a page record she lost: nothing filled
        self.assertNotEqual(self.ask(self.ana, "fill this form").status_code, 200)
        self.backend.po_open = False
        self.assertNotEqual(self.ask(self.ana, "fill this form for PO 90412").status_code, 200)
        self.backend.po_open = True
        # the SharePoint file it should go beside is Ben's: refused, not filled
        r = self.ana.post("/api/doc-fill", json={
            "name": "Q.xlsx", "data": base64.b64encode(FORM).decode(), "record": "PO 90412",
            "source": "sharepoint://drive-other/item-x@e1"})
        self.assertEqual(r.status_code, 404, r.text)
        # a value naming an excluded person never enters the file
        self.backend.excluded_value = True
        fill = self.ask(self.ana, "fill this form for PO 90412").json()["fills"][0]
        name = next(f for f in fill["fields"] if f["label"] == "Supplier Name")
        self.assertEqual(name["after"], "Maple Leaf")
        data = self.ana.get(fill["download_url"]).content
        self.assertNotIn(ti.EXCLUDED.encode(), b"".join(
            zipfile.ZipFile(io.BytesIO(data)).read(n) for n in
            zipfile.ZipFile(io.BytesIO(data)).namelist()))
        # an attachment that is no form: the Ask goes on as an instruction (the model)
        self.ask(self.ana, "fill this form for PO 90412", form=b"plain words", name="a.txt")
        self.assertEqual(len(self.model.prompts), 1)

    def test_beside_its_sharepoint_original_it_is_an_upload_approved_as_a_dry_run(self):
        """The copy goes beside the SharePoint file it came from as a new file the reader
        approves through the gated write path; with SharePoint not opened by the owner the
        approval is recorded and nothing is uploaded."""
        from operating.tests.test_autopilot_write import Transport
        from operating import autopilot_write_adapters as wa
        b, fake = self.backend, Transport()
        b.autopilot_adapters = {"sharepoint_write": wa.SharePointAdapter(fake, environ={})}
        source = FillWorld.source

        def with_parent(me, reader, handle, offset):
            view = source(me, reader, handle, offset)
            if handle == FILE_H:
                view.fields = {**view.fields, "row.parent_id": "01FOLDER"}
            return view
        FillWorld.source = with_parent
        self.addCleanup(setattr, FillWorld, "source", source)
        out = self.ask(self.ana, "fill this in for PO 90412", {"kind": "email", "handle": FILE_H})
        fill = out.json()["fills"][0]
        self.assertEqual(fill["folder"], "/Purchasing/Quotes")
        up = f"/api/instruct/uploads/{fill['upload_id']}"
        shown = self.ana.get(up).json()
        self.assertEqual(shown["name"], "Questionnaire (filled).xlsx")
        self.assertTrue(shown["missing"])                         # not opened: a dry run
        done = self.ana.post(up + "/approve", json={"confirm": True,
                                                    "preview_hash": shown["preview_hash"]})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["status"], "would_upload")
        self.assertEqual(fake.calls, [])

    def test_from_an_email_the_reply_with_the_file_is_prepared_never_sent(self):
        r = self.ana.post("/api/doc-fill", json={
            "name": "Questionnaire.xlsx", "data": base64.b64encode(FORM).decode(),
            "record": "PO 90412", "reply_to": MAIL})
        self.assertEqual(r.status_code, 200, r.text)
        reply = r.json()["reply"]
        self.assertEqual((reply["to"], reply["status"], reply["attachment"]),
                         (["pat@maple.example"], "not_available",
                          "Questionnaire (filled).xlsx"))
        self.assertEqual(self.sends, [])
        # another reader's email is not theirs to reply to
        r = self.ben.post("/api/doc-fill", json={
            "name": "Q.xlsx", "data": base64.b64encode(FORM).decode(), "record": "PO 90412",
            "reply_to": MAIL})
        self.assertNotEqual(r.status_code, 200)


def load_tests(loader, tests, pattern):
    """This module's own tests (the instruct harness's run in its module)."""
    import unittest
    return unittest.TestSuite(DocFillApi(name) for name in loader.getTestCaseNames(DocFillApi)
                              if name in DocFillApi.__dict__)
