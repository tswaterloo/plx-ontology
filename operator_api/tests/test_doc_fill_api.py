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
from operator_api.tests.test_instruct import ANA, FILE_H, MAIL, World

PO = "PurchaseOrder:90412"
PO_H = "bc://Production/c/purchaseOrders/90412@W%2F%22p1%22"
FORM = files.make("xlsx", "Supplier questionnaire\nPO Number:|\nSupplier Name:|\n"
                          "Contact email:|\nIncoterms:|\nSignature:|\nShoe size:|")


CARD_FORM = files.make("xlsx", "Card authorisation\nIncoterms:|\nCustomer account:|")
# a network's published test number, never a real card
CARD_DIGITS = "4111111111111111"


class FillWorld(World):
    excluded_value = False
    card_thread = False     # the thread holds an earlier filled card authorisation

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


    def form_sources(self, reader, fields, *, message, words, named):
        """Ana opens the email the form came in: its thread says the terms (and,
        when ``excluded_value``, names a person kept out)."""
        if message != MAIL or reader.session.subject_oid != ANA[1]:
            return [], [], None
        said = ti.EXCLUDED if self.excluded_value else "CIF"
        facts = [df.Fact("Said.field:incoterms", said, df.Citation(MAIL, "Incoterms", MAIL),
                         tier=1)]
        if self.card_thread:
            facts.append(df.Fact("Said.field:customer_number", "4111 1111 1111 1111",
                                 df.Citation(MAIL, "Customer account", MAIL), tier=1))
        return facts, [], None


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

    def test_with_no_record_named_the_forms_own_thread_answers_cited(self):
        out = self.ask(self.ana, "fill this in", {"kind": "email", "handle": MAIL})
        self.assertEqual(out.status_code, 200, out.text)
        fill = out.json()["fills"][0]
        got = {f["label"]: f for f in fill["fields"]}
        self.assertEqual((got["Incoterms"]["status"], got["Incoterms"]["after"],
                          got["Incoterms"]["citation"]["source"]),
                         ("filled", "CIF", MAIL))
        self.assertEqual(got["PO Number"]["status"], "no_fact")
        self.assertEqual(fill["record"]["type"], "Request")
        self.assertIn("its thread", out.json()["answer"]["direct_answer"])
        # Ben cannot read that email: nothing to fill from, so he is asked for the record
        self.assertNotEqual(self.ask(self.ben, "fill this in",
                                     {"kind": "email", "handle": MAIL}).status_code, 200)
        # a thread value naming a person kept out never enters the file
        self.backend.excluded_value = True
        out = self.ask(self.ana, "fill this in", {"kind": "email", "handle": MAIL})
        self.assertNotEqual(out.status_code, 200)

    def test_a_card_number_it_read_never_enters_a_filled_file(self):
        """An earlier filled card authorisation in the form's thread: the terms are
        filled, the account blank its card number would take is left for the reader to
        write (``doc_fill_api._card_fact``), in the file served raw as in its preview."""
        from unittest import mock

        def fill():
            out = self.ask(self.ana, "fill this in", {"kind": "email", "handle": MAIL},
                           form=CARD_FORM, name="Card form.xlsx")
            self.assertEqual(out.status_code, 200, out.text)
            got = out.json()["fills"][0]
            data = self.ana.get(got["download_url"]).content
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                body = b"".join(z.read(n) for n in z.namelist()).decode("utf-8", "replace")
            return {f["label"]: f["after"] for f in got["fields"]}, body
        self.backend.card_thread = True
        # the check is not vacuous: without the rule the thread's card fills the file
        with mock.patch("operator_api.doc_fill_api._card_fact", return_value=False):
            after, body = fill()
        self.assertIn(CARD_DIGITS, "".join(ch for ch in body if ch.isdigit()))
        after, body = fill()
        self.assertEqual(after["Incoterms"], "CIF")
        self.assertEqual(after["Customer account"], "")
        self.assertNotIn(CARD_DIGITS, "".join(ch for ch in body if ch.isdigit()))

    def test_card_fields_named_by_earlier_copies_and_threads_are_never_filled(self):
        """Keys prefixed by an earlier copy (``Copy.f3|cvv``) or a mail thread
        (``Said.field:cvv``) are card fields too; a plain field still fills."""
        from operator_api import doc_fill_api as api
        for key in ("Copy.f3|cvv", "Said.field:cvv", "Copy.f3|expiry".replace("expiry", "card_exp"),
                    "Said.field:security code", "Thread/cvv2"):
            self.assertTrue(api._card_fact(df.Fact(key, "123", None)), key)
        self.assertFalse(api._card_fact(df.Fact("Copy.f3|incoterms", "CIF", None)))
        self.assertFalse(api._card_fact(df.Fact("Said.field:incoterms", "CIF", None)))

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

    def fill_from_mail(self, client=None):
        r = (client or self.ana).post("/api/doc-fill", json={
            "name": "Questionnaire.xlsx", "data": base64.b64encode(FORM).decode(),
            "record": "PO 90412", "reply_to": MAIL})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def live_sender(self):
        """A real ``ActionSender`` with sending on for Ana, its post an injected fake Graph
        that runs the guard's own check of every request first (AUDIT4-06)."""
        import json as _json
        from integrations import graph_send
        from integrations import live_source_guard as guard
        env = {"PLX_EXCLUDED_PEOPLE_OIDS": ti.EXCLUDED, "PLX_ACTIONS_LIVE": "1",
               "PLX_ACTIONS_SENDERS": ti.ANA[0]}
        claims = base64.urlsafe_b64encode(_json.dumps({"roles": ["Mail.Send"]}).encode())
        posted = []

        def post(url, _token, body):
            guard.check_action_request("POST", url, body, environ=env)
            posted.append((url, _json.loads(body)))
            return 202, {}, b""
        self.backend.actions_environ = env
        self.backend.action_sender = graph_send.ActionSender(
            env, bearer=lambda: "h." + claims.decode().rstrip("=") + ".s", post=post)
        self.backend._action_desk = None
        return posted

    def test_from_an_email_the_reply_carries_the_filled_file_and_goes_only_on_confirm(self):
        import hashlib
        posted = self.live_sender()
        fill = self.fill_from_mail()
        reply = fill["reply"]
        self.assertEqual((reply["to"], reply["status"], reply["attachment"], reply["missing"]),
                         (["pat@maple.example"], "ready", "Questionnaire (filled).xlsx", []))
        self.assertEqual([o["kind"] for o in reply["actions"]],
                         ["send_email", "reply_in_thread"])
        data = self.ana.get(fill["download_url"]).content
        card = {"card_id": reply["card_id"], "kind": "reply_in_thread"}
        shown = self.ana.post("/api/actions/preview", json=card)
        self.assertEqual(shown.status_code, 200, shown.text)
        shown = shown.json()
        self.assertEqual(shown["problems"], [])
        self.assertEqual(shown["payload"]["attachments"], [{
            "upload_id": reply["card_id"][len("fill:"):],
            "name": "Questionnaire (filled).xlsx",
            "content_type": files.MEDIA["xlsx"], "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}])
        # the preview's request names the file and its size, never its bytes
        attached = shown["request"]["body"]["message"]["attachments"]
        self.assertEqual([(a["name"], a.get("size"), "contentBytes" in a) for a in attached],
                         [("Questionnaire (filled).xlsx", len(data), False)])
        self.assertEqual(posted, [])                               # a preview sends nothing
        done = self.ana.post("/api/actions/confirm", json={
            **card, "payload": {"attachments": shown["payload"]["attachments"]},
            "confirm": True, "idempotency_key": "fill-reply-0001-key",
            "payload_hash": shown["payload_hash"]})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["status"], "sent")
        [(url, body)] = posted
        self.assertTrue(url.endswith("/reply"), url)
        [wire] = body["message"]["attachments"]
        self.assertEqual(base64.b64decode(wire["contentBytes"]), data)   # the exact file
        self.assertEqual(wire["@odata.type"], "#microsoft.graph.fileAttachment")
        self.assertNotIn("contentBytes", str(done.json().get("request")))
        # the journal keeps the file's name, size and hash, never its bytes
        held = (self.tmp / "actions" / "journal.jsonl").read_text()
        self.assertIn(hashlib.sha256(data).hexdigest(), held)
        self.assertNotIn(wire["contentBytes"][:200], held)
        # a second confirm with the same key replays; the message is never sent twice
        again = self.ana.post("/api/actions/confirm", json={
            **card, "payload": {"attachments": shown["payload"]["attachments"]},
            "confirm": True, "idempotency_key": "fill-reply-0001-key",
            "payload_hash": shown["payload_hash"]})
        self.assertTrue(again.json()["replayed"])
        self.assertEqual(len(posted), 1)

    def test_a_filled_reply_is_refused_when_the_file_is_not_the_one_previewed(self):
        posted = self.live_sender()
        fill = self.fill_from_mail()
        card = {"card_id": fill["reply"]["card_id"], "kind": "reply_in_thread"}
        shown = self.ana.post("/api/actions/preview", json=card).json()
        # the kept file changes on disk after the preview: the confirm refuses, nothing sent
        file_id = fill["download_url"].rsplit("/", 1)[1]
        path = self.tmp / "instruct" / "files" / file_id
        path.write_bytes(path.read_bytes() + b"x")
        done = self.ana.post("/api/actions/confirm", json={
            **card, "confirm": True, "idempotency_key": "fill-reply-0002-key",
            "payload_hash": shown["payload_hash"]})
        self.assertIn(done.status_code, (409, 422), done.text)
        self.assertIn("changed", done.text)
        self.assertEqual(posted, [])

    def test_a_filled_reply_is_only_its_reader_s_and_adds_no_other_file(self):
        posted = self.live_sender()
        fill = self.fill_from_mail()
        card = {"card_id": fill["reply"]["card_id"], "kind": "reply_in_thread"}
        # another reader cannot open Ana's filled reply, nor guess one
        self.assertEqual(self.ben.post("/api/actions/preview", json=card).status_code, 404)
        self.assertEqual(self.ana.post("/api/actions/preview", json={
            **card, "card_id": "fill:up_000000000000000000000000"}).status_code, 404)
        # an edit may take the file off, never add another upload
        r = self.ana.post("/api/actions/preview", json={
            **card, "payload": {"attachments": [{"upload_id": "up_000000000000000000000000"}]}})
        self.assertIn("Only the file prepared", r.text)
        off = self.ana.post("/api/actions/preview", json={**card, "payload": {
            "attachments": []}}).json()
        self.assertIsNone(off["payload"]["attachments"])
        self.assertNotIn("attachments", off["request"]["body"]["message"])
        # a forward carries no file
        r = self.ana.post("/api/actions/preview", json={
            "card_id": card["card_id"], "kind": "forward",
            "payload": {"attachments": [{"upload_id": card["card_id"][5:]}]}})
        self.assertNotEqual(r.status_code, 200)
        # the people exclusion unavailable: nothing with the file can go (fail closed)
        self.backend.actions_environ = {**self.backend.actions_environ,
                                        "PLX_EXCLUDED_PEOPLE_OIDS": ""}
        r = self.ana.post("/api/actions/preview", json=card)
        self.assertTrue(r.status_code != 200 or r.json()["problems"], r.text)
        self.assertEqual(posted, [])
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
