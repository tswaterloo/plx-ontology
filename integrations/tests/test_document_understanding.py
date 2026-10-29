"""Real format document extraction and evidence contract tests (isolated data)."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from hashlib import sha256
import importlib
import io
import logging
import os
from pathlib import Path
import quopri
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

from integrations.captured_text import read_container
from integrations.document_understanding import (extract_document, line_item_text_witness,
                                                  local_image_text)
from integrations.tests.test_doc_convert import make_msg, msg_tree
from ontology.core import ValidationError
from ontology.tests.test_container_members import zipped
from operating.use_case_parts import members


OCR_TOOLS = all(shutil.which(tool) for tool in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract"))
POPPLER = all(shutil.which(tool) for tool in ("pdfinfo", "pdftotext"))
# Whole and with a content stream, so a tool refusal is pdf_unreadable, not a pdf_stub.
WHOLE_PDF = b"%PDF-1.4\n1 0 obj\n<< /Length 0 >>\nstream\nendstream\nendobj\n%%EOF\n"


URL = "https://tenant.example.com/sites/purchasing/po-200.pdf"


def read_text_of(content: bytes, name: str) -> str:
    return extract_document(content, "application/octet-stream", original_url=URL,
                            filename=name).text
CONTENT = ("Petra PO: PPO-200\nSupplier: Acme Packaging\n"
           "Item: BTL-100 | Qty: 100,000 bottles | Unit Price: USD 0.12\n"
           "Requested delivery: 2026-11-15\nTotal Amount: USD 12000\n")


class DocumentUnderstandingTest(unittest.TestCase):
    def test_text_claims_have_exact_spans_and_preserve_unknowns(self):
        document = extract_document(CONTENT.encode(), "text/plain", original_url=URL)
        self.assertEqual(document.status, "extracted")
        facts = document.evidence_attributes(URL, title="Petra order")
        self.assertEqual(facts["identifiers"], [
            {"type": "petra_po", "value": "PPO-200",
             "provenance": URL + "#documentText=10-17"},
            {"type": "product_code", "value": "BTL-100",
             "provenance": URL + "#documentText=49-56"}])
        self.assertEqual(facts["quantities"][0]["value"], 100000)
        self.assertEqual(facts["quantities"][0]["unit"], "bottles")
        self.assertEqual(facts["costs"][0]["amount"], 0.12)
        self.assertEqual(facts["extraction"]["line_items"][0]["confidence"], "review")
        self.assertEqual(next(c for c in document.claims if c["kind"] == "date")["value"], "2026-11-15")
        for claim in document.claims:
            span = claim["span"]
            self.assertTrue(document.text[span["start"]:span["end"]])
        # An amount without currency is not a priced operational fact.
        bare = extract_document(b"Price: 100\nQty: 500\n", "text/plain", original_url=URL)
        self.assertEqual(bare.evidence_attributes(URL)["costs"], [])
        self.assertEqual(bare.evidence_attributes(URL)["quantities"], [])
        self.assertEqual(len(bare.claims), 2)

    def test_a_csv_file_is_read_as_text_whatever_type_the_host_declares(self):
        """A ``.csv`` file is text, as a container's ``.csv`` member is: SharePoint
        declares it ``application/vnd.ms-excel`` (or ``text/csv``); read as UTF-8 text,
        never ``unsupported``. Another file of the spreadsheet type is still unread."""
        rows = b"PO Number,Qty\r\n4500000002,500\r\n"
        for mime in ("application/vnd.ms-excel", "text/csv", "application/octet-stream"):
            with self.subTest(mime=mime):
                document = extract_document(rows, mime, original_url=URL, filename="po.CSV")
                self.assertEqual((document.status, document.method, document.text),
                                 ("extracted", "utf8-text", rows.decode()))
        self.assertEqual(extract_document(rows, "application/vnd.ms-excel", original_url=URL,
                                          filename="po.xls").status, "unsupported")

    def test_a_markdown_file_is_read_as_text(self):
        """A process map saved as Markdown (SharePoint declares ``text/markdown``) is
        text as written, never ``unsupported``; one captured before this was read (its
        stored status ``unsupported``) reads from its pinned bytes the same way."""
        import tempfile
        from hashlib import sha256
        from pathlib import Path
        from integrations.captured_text import captured_text

        body = "# Replenishment Map\n\n| Step | Owner |\n|---|---|\n| Ship | Ops |\n"
        for mime, name in (("text/markdown", "Map.md"), ("text/x-markdown", "map.markdown"),
                           ("application/octet-stream", "Map.MD")):
            with self.subTest(mime=mime):
                document = extract_document(body.encode(), mime, original_url=URL,
                                            filename=name)
                self.assertEqual((document.status, document.text), ("extracted", body))
        # Declared HTML, it is read as HTML (its visible text: no tags here, so the same).
        as_html = extract_document(body.encode(), "text/html", original_url=URL,
                                   filename="map.html")
        self.assertEqual((as_html.status, as_html.method), ("extracted", "html-text"))
        undecodable = extract_document(b"\xff\xfe\x00bad", "text/markdown", original_url=URL)
        self.assertEqual(undecodable.status, "unsupported")
        with tempfile.TemporaryDirectory() as tmp:
            digest = sha256(body.encode()).hexdigest()
            blob = Path(tmp) / digest[:2] / digest
            blob.parent.mkdir(parents=True)
            blob.write_bytes(body.encode())
            found = captured_text(tmp, {"content_sha256": digest, "mime_type": "text/markdown",
                                        "name": "Map.md", "text_status": "unsupported"})
        self.assertEqual(found.text, body)

    def test_html_and_json_files_and_members_are_read_as_text(self):
        """The process maps were captured ``unsupported`` (an HTML map standalone, an HTML
        map and its JSON spec in a zip), so a read of the right file saw nothing. HTML is
        its visible text (no script or style), JSON its text as written (never parsed
        and re-written: ``1.10`` stays ``1.10``); a file captured before this reads from
        its pinned bytes, whole even past the plain-text bound."""
        import tempfile

        from integrations import document_understanding as du
        from integrations.captured_text import capture_text, captured_text

        page = ("<html><head><style>.x{color:red}</style><script>var hidden='SCRIPTONLY'"
                "</script></head><body><h1>Expired Material Map</h1><p>A 30-day extension "
                "applies only if all six conditions hold &amp; QA signs.</p><table><tr>"
                "<td>Look-ahead</td><td>14 days</td></tr></table></body></html>")
        spec = '{"rule": "look-ahead window", "rate": 1.10, "name": "Caf\u00e9 \u2013 map"}'
        archive = zipped([("Map/Map.html", page.encode()),
                          ("Map/data/map-spec.json", spec.encode()),
                          ("Map/label.bin", b"\x00\x01\x02")])
        found = read_container(archive, "Map.zip", "application/zip")
        by_path = {m.path: m for m in found.members}
        html = by_path["Map/Map.html"]
        self.assertEqual((html.mime_type, html.text_status, html.reading.method),
                         ("text/html", "extracted", "html-text"))
        self.assertIn("A 30-day extension applies only if all six conditions hold & QA signs.",
                      html.reading.text)
        self.assertIn("Look-ahead\t14 days", html.reading.text)
        self.assertNotIn("SCRIPTONLY", html.reading.text)
        self.assertNotIn("color:red", html.reading.text)
        member = by_path["Map/data/map-spec.json"]
        self.assertEqual((member.mime_type, member.text_status, member.reading.text),
                         ("application/json", "extracted", spec))
        self.assertEqual(by_path["Map/label.bin"].text_status, "unsupported")
        # Standalone, captured before HTML was read (stored ``unsupported``): read now.
        long_page = ("<html><body>" + "<p>Worked example C: good bottles returned.</p>"
                     * (du.MAX_TEXT // 40) + "</body></html>").encode()
        with tempfile.TemporaryDirectory() as tmp:
            for content, mime, name in ((page.encode(), "text/html", "Map.html"),
                                        (long_page, "application/octet-stream", "Long.HTM"),
                                        (spec.encode(), "application/octet-stream",
                                         "map-spec.json")):
                digest = sha256(content).hexdigest()
                blob = Path(tmp) / digest[:2] / digest
                blob.parent.mkdir(parents=True, exist_ok=True)
                blob.write_bytes(content)
                with self.subTest(name=name):
                    read = captured_text(tmp, {"content_sha256": digest, "mime_type": mime,
                                               "name": name, "text_status": "unsupported"})
                    self.assertEqual(read.status, "extracted")
                    self.assertEqual(read.text, du.extract_document(
                        content, mime, original_url=URL, filename=name).text)
                    # A new capture stores the same text.
                    self.assertEqual(capture_text(content, {"mime_type": mime, "name": name}),
                                     ("extracted", read.text))
        self.assertGreater(len(read_text_of(long_page, "Long.HTM")), du.MAX_TEXT)
        # Over the HTML byte bound, or not UTF-8: unsupported with its reason, never parsed.
        over = b"<p>" + b"x" * du.MAX_HTML_BYTES + b"</p>"
        big = extract_document(over, "text/html", original_url=URL)
        self.assertEqual((big.status, big.text, big.warnings),
                         ("unsupported", "", (f"html_exceeds_bound:{du.MAX_HTML_BYTES}",)))
        latin = extract_document("<p>Caf\u00e9</p>".encode("latin-1"), "text/html",
                                 original_url=URL)
        self.assertEqual((latin.status, latin.warnings),
                         ("unsupported", ("text_encoding_unsupported",)))
        blank = extract_document(b"<html><script>x()</script></html>", "text/html",
                                 original_url=URL)
        self.assertEqual((blank.status, blank.text), ("no_text", ""))

    def test_html_sibling_elements_never_glue_words_and_inline_runs_stay_one_word(self):
        """Reproduced (2026-10-29): process maps style <span>/<b> as blocks, so adjacent
        elements read "still to approve.Owner", "methodBusiness Central", "2026Left out"
        and the answer gate refused correct verbatim quotes. Sibling elements with no
        text between are separated; a word split across inline elements is not."""
        from integrations.document_understanding import html_text

        def read(page):
            found = extract_document(page.encode(), "text/html", original_url=URL,
                                     filename="map.html")
            self.assertEqual(found.text, html_text(page))
            return found.text

        glued = read("<div><b>A screening clearing account still to approve.</b><span>Owner, "
                     "14 Oct 2026: every cost.</span></div><p><span>Costing method</span>"
                     "<span>Business Central items stay Average.</span></p><dl><dt>Updated "
                     "14 Oct 2026</dt><dd>Left out: freight</dd></dl><span>Step 4</span><b>"
                     "Release</b><span>A</span><span>B</span>")
        for quote in ("still to approve. Owner, 14 Oct", "Costing method Business Central",
                      "14 Oct 2026\n\nLeft out", "Step 4 Release", "A B"):
            self.assertIn(quote, glued)
        for joined in ("approve.Owner", "methodBusiness", "2026Left", "4Release"):
            self.assertNotIn(joined, glued)
        # A word split across inline elements stays one word: text after an end tag,
        # a lowercase continuation, a digit run, and punctuation after an element.
        kept = read("<p><b>Re</b>lease the <b>Re</b><i>lease</i> lot <span>12</span><span>"
                    "34</span> on <b>Mon</b><span>, then</span> <a>QA</a>-signed "
                    "<span>PLX</span><span>s</span>.</p>")
        self.assertEqual(kept, "Release the Release lot 1234 on Mon, then QA-signed PLXs.")
        # Whitespace already between elements is kept as written, nothing doubled.
        self.assertEqual(read("<p><span>one</span> <span>two</span>\n<span>three</span></p>"),
                         "one two\nthree")
        # Script and style content between elements never decides a separator.
        self.assertEqual(read("<p><b>end.</b><script>x()</script><span>Next</span></p>"),
                         "end. Next")

    def test_amounts_read_their_decimal_mark_only_when_unambiguous(self):
        """Owner-found bug: "USD 0.04125" read as 0 and "EUR 0,85" as 85. A decimal
        comma is read only when unambiguous; otherwise the amount has no value."""
        def prices(text):
            document = extract_document(text.encode(), "text/plain", original_url=URL)
            return [(c["value"], c["currency"], c.get("unreadable"))
                    for c in document.claims if c["kind"] == "price"]

        self.assertEqual(prices("Unit Price: USD 0.04125\n"), [(0.04125, "USD", None)])
        self.assertEqual(prices("Unit Price: EUR 0,85\n"), [(0.85, "EUR", None)])
        self.assertEqual(prices("Total Amount: EUR 1.234,50\n"), [(1234.5, "EUR", None)])
        # "1,234" alone is thousands or 1.234: no value, and never a priced fact.
        self.assertEqual(prices("Total Amount: USD 1,234\n"),
                         [(None, "USD", "decimal_separator_ambiguous")])
        bare = extract_document(b"Total Amount: USD 1,234\n", "text/plain", original_url=URL)
        self.assertEqual(bare.evidence_attributes(URL)["costs"], [])
        # Another number of the document fixes the convention ("0.25": "." is decimal).
        self.assertEqual(prices("Unit Price: USD 0.25\nTotal Amount: USD 1,234\n"),
                         [(0.25, "USD", None), (1234, "USD", None)])
        self.assertEqual(prices("Total Amount: USD 1,2,3\n"),
                         [(None, "USD", "number_unreadable")])

    def test_email_mime_body_and_headers_are_its_own_text(self):
        message = EmailMessage()
        message["Subject"] = "Supplier confirmation VC-300 for Petra PO: PPO-200"
        message["From"] = "supplier@example.test"
        message["To"] = "buyer@example.test"
        message["Date"] = "Thu, 24 Sep 2026 12:00:00 +0000"
        message.set_content("Vendor confirmation: VC-300\nQty: 100,000 bottles\n")
        message.add_attachment(CONTENT.encode(), maintype="text", subtype="plain",
                               filename="purchase-order.txt")
        document = extract_document(message.as_bytes(), "message/rfc822",
                                    original_url="https://outlook.example.test/mail/m1")
        self.assertEqual(document.status, "extracted")
        self.assertEqual(document.method, "rfc822-mime")
        self.assertIn("supplier@example.test", document.text)
        self.assertIn("VC-300", [c["value"] for c in document.claims])
        self.assertIn("PPO-200", [c["value"] for c in document.claims])   # the subject's
        self.assertIn("supplier@example.test", [c["value"] for c in document.claims
                                             if c["kind"] == "party"])
        self.assertTrue(any(c["field"] == "message date" for c in document.claims))
        # The attachment is a container member, read on its own (next test).
        self.assertNotIn("BTL-100", document.text)
        self.assertEqual(document.line_items, ())

    def test_a_mail_is_read_once_and_nested_mail_is_one_rule_in_both_formats(self):
        """Direction finding 11e322b9: an attachment was read into its mail's text and
        again as a container member, one identifier under two handles, and the two
        walks disagreed on nested mail. Now a mail's text is its own; every attachment
        is a member read once; a nested mail is the same in .eml and .msg, attached as a
        file or carried (a forward-as-attachment message/rfc822 entity, an embedded .msg
        item; gap-0d37a104a3db): a member with its own text, its attachments its members
        (a carried message with no file name is shown as <Subject>.eml, here "Fwd.eml").
        A carried message whose bytes cannot be had stays a visible gap."""
        def eml(body, attachments, html=False):
            message = EmailMessage()
            message["From"], message["To"], message["Subject"] = "a@x.test", "b@x.test", "Fwd"
            message.set_content(body)
            if html:
                message.add_alternative(f"<p>{body}</p>", subtype="html")
            for name, content in attachments:
                if isinstance(content, EmailMessage):
                    message.add_attachment(content)
                else:
                    maintype, subtype = mimetypes[name.rsplit(".", 1)[1]].split("/")
                    message.add_attachment(content, maintype=maintype, subtype=subtype,
                                           filename=name)
            return message

        def carried_msg(*extra):
            return msg_tree([("carried.txt", "text/plain", b"Petra PO: PPO-206")],
                            body="Petra PO: PPO-205", header=24) | dict(extra)

        mimetypes = {"txt": "text/plain", "eml": "message/rfc822",
                     "msg": "application/vnd.ms-outlook"}
        own, conf = "Petra PO: PPO-201", ("conf.txt", b"Petra PO: PPO-202")
        nested_msg = make_msg([("deep.txt", "text/plain", b"Petra PO: PPO-203")],
                              body="Petra PO: PPO-204")
        nested_eml = eml("Petra PO: PPO-204", [("deep.txt", b"Petra PO: PPO-203")]).as_bytes()
        forwarded = eml("Petra PO: PPO-205", [("carried.txt", b"Petra PO: PPO-206")])
        cases = {
            "outer.eml": (eml(own, [conf, ("inner.msg", nested_msg), ("carried", forwarded)],
                              html=True).as_bytes(), "message/rfc822", "inner.msg", "Fwd.eml"),
            "outer.msg": (make_msg([("conf.txt", "text/plain", conf[1]),
                                    ("inner.eml", "message/rfc822", nested_eml),
                                    ("carried", "", carried_msg())], body=own),
                          "application/vnd.ms-outlook", "inner.eml", "carried")}
        for name, (content, mime, nested, carried) in cases.items():
            with self.subTest(name):
                document = extract_document(content, mime, original_url=URL, filename=name)
                self.assertEqual((document.status, document.warnings), ("extracted", ()))
                found = read_container(content, name, mime)
                texts = {"(container)": document.text,
                         **{m.name or m.path: m.reading.text for m in found.members}}
                cited = {n: [k for k, text in texts.items() if f"PPO-20{n}" in (text or "")]
                         for n in range(1, 7)}
                self.assertEqual(cited, {1: ["(container)"], 2: ["conf.txt"], 3: ["deep.txt"],
                                         4: [nested], 5: [carried], 6: ["carried.txt"]})
                paths = {m.name or m.path: m.path for m in found.members}
                self.assertTrue(paths["deep.txt"].startswith(paths[nested] + "!/"))
                self.assertTrue(paths["carried.txt"].startswith(paths[carried] + "!/"))
                self.assertEqual([(m.container, m.text_status) for m in found.members
                                  if (m.name or m.path) in (nested, carried)],
                                 [(True, "extracted")] * 2)
                self.assertEqual(found.gaps, ())
        # A forwarded message's member bytes are the source's own bytes in the container.
        content = cases["outer.eml"][0]
        [data] = [f["data"] for f in members(content, "outer.eml", "message/rfc822")["files"]
                  if f["path"] == "4"]
        self.assertEqual(data, forwarded.as_bytes())
        self.assertIn(data, content)
        # An embedded item that cannot be saved (a stream past the part bound) is the gap;
        # its attachment is still a member.
        content = make_msg([("carried", "", carried_msg(("__substg1.0_1035001F", b"<id>")))])
        entry = content.find("__substg1.0_1035001F".encode("utf-16-le"))
        content = content[:entry + 0x78] + (2 ** 31).to_bytes(4, "little") + \
            content[entry + 0x7C:]
        found = read_container(content, "broken.msg", "application/vnd.ms-outlook")
        self.assertEqual([g["reason"] for g in found.gaps if "reason" in g],
                         ["carried_message_not_read"])
        self.assertEqual([m.reading.text for m in found.members], ["Petra PO: PPO-206"])

    def test_a_transfer_encoded_forwarded_message_is_decoded_before_its_headers(self):
        """Directive 5a8d03900711 (1, 5): Python's parser parses a message/rfc822 part
        without decoding it, so a forward written base64 or quoted-printable was read
        as its own encoding (no headers, unsupported ``email_headers_missing``, and its
        own attachments were not members). The stated encoding is now removed from the
        source's raw body before any of it is parsed (a quoted-printable soft line break
        in a 77-char header made the rest body text), and a carried message with no file
        name is shown as ``<Subject>.eml``: a subject ending ``.msg`` never picks the
        Outlook reader. A stated base64 body that holds no base64 keeps the visible
        gap, never an empty member."""
        inner = (b"From: a@x.test\r\nTo: b@x.test\r\nSubject: Quote Q-77 caf\xc3\xa9.msg\r\n"
                 b"References: <" + b"a" * 27 + b"@mail.example.com> <b" + b"c" * 8
                 + b"@x.test>\r\n"  # 77 chars: quoted-printable soft-wraps it
                 b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=C\r\n\r\n"
                 b"--C\r\nContent-Type: text/plain\r\n\r\nPetra PO: PPO-500\r\n--C\r\n"
                 b"Content-Type: text/plain\r\nContent-Disposition: attachment; "
                 b"filename=q.txt\r\n\r\nPetra PO: PPO-501\r\n--C--\r\n")
        bodies = {"7bit": inner,  # the control: an unencoded forward reads the same
                  "base64": base64.encodebytes(inner).replace(b"\n", b"\r\n"),
                  "quoted-printable": quopri.encodestring(  # soft-wraps at 76
                      inner.replace(b"\r\n", b"\n")).replace(b"\n", b"\r\n")}

        def forward(encoding, body):
            return (b"From: c@x.test\r\nTo: d@x.test\r\nSubject: FW\r\nMIME-Version: 1.0\r\n"
                    b"Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\n"
                    b"Content-Type: text/plain\r\n\r\nsee below\r\n--B\r\n"
                    b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: "
                    + encoding.encode() + b"\r\n\r\n" + body + b"\r\n--B--\r\n")

        for encoding, body in bodies.items():
            with self.subTest(encoding):
                found = read_container(forward(encoding, body), "fw.eml", "message/rfc822")
                self.assertEqual(found.gaps, ())
                self.assertEqual([(m.path, m.name, m.mime_type, m.container, m.text_status)
                                  for m in found.members],
                                 [("2", "Quote Q-77 café.msg.eml", "message/rfc822", True,
                                   "extracted"),
                                  ("2!/2", "q.txt", "text/plain", False, "extracted")])
                carried, attachment = (m.reading.text for m in found.members)
                for line in ("Subject: Quote Q-77 café.msg", "From: a@x.test", "To: b@x.test",
                             "Petra PO: PPO-500"):
                    self.assertIn(line, carried)
                self.assertEqual(attachment, "Petra PO: PPO-501")
        found = read_container(forward("base64", b"@@@@"), "fw.eml", "message/rfc822")
        self.assertEqual([g["reason"] for g in found.gaps if "reason" in g],
                         ["carried_message_not_read"])
        self.assertEqual(found.members, ())

    def test_a_bounce_report_and_an_unsplit_multipart_are_read_without_false_gaps(self):
        """gap-3804403b13bf: a delivery-failure report's status blocks were carried
        messages (spurious carried_message_not_read gaps), and a mail whose multipart
        body has no boundary had no text. A status block is report text, a member; the
        returned message is a carried message; an unsplit body is the mail's own text
        where it decodes, else a visible gap."""
        bounce = (b"From: postmaster@x.test\r\nTo: a@x.test\r\nSubject: Undeliverable\r\n"
                  b"MIME-Version: 1.0\r\nContent-Type: multipart/report; "
                  b"report-type=delivery-status; boundary=\"B\"\r\n\r\n"
                  b"--B\r\nContent-Type: text/plain\r\n\r\nDelivery has failed.\r\n"
                  b"--B\r\nContent-Type: message/delivery-status\r\n\r\n"
                  b"Reporting-MTA: dns; mx.x.test\r\n\r\nFinal-Recipient: rfc822; c@y.test\r\n"
                  b"Action: failed\r\nStatus: 5.1.1\r\n\r\n"
                  b"--B\r\nContent-Type: message/rfc822\r\n\r\nFrom: a@x.test\r\n"
                  b"To: c@y.test\r\nSubject: PO\r\n\r\nPetra PO: PPO-300\r\n--B--\r\n")
        found = read_container(bounce, "bounce.eml", "message/rfc822")
        self.assertEqual(found.gaps, ())
        self.assertEqual([(m.path, m.mime_type, m.container) for m in found.members],
                         [("2", "text/plain", False), ("3", "message/rfc822", True)])
        status, returned = (m.reading.text for m in found.members)
        self.assertIn("Final-Recipient: rfc822; c@y.test", status)
        self.assertIn("PPO-300", returned)
        self.assertIn("Delivery has failed.", extract_document(
            bounce, "message/rfc822", original_url=URL).text)
        head = (b"From: a@x.test\r\nTo: b@x.test\r\nSubject: S\r\n"
                b"Content-Type: multipart/mixed\r\n\r\n")
        for body, text, gaps in ((b"Petra PO: PPO-400\r\n", "Petra PO: PPO-400", []),
                                 (b"Petra PO: \xff\xfe\r\n", None, ["text_encoding_unsupported"])):
            document = extract_document(head + body, "message/rfc822", original_url=URL)
            self.assertEqual("PPO-400" in document.text, text is not None)
            self.assertEqual(document.warnings, () if gaps == [] else tuple(gaps))
            found = read_container(head + body, "unsplit.eml", "message/rfc822")
            self.assertEqual((found.members, [g["reason"] for g in found.gaps]), ((), gaps))

    def test_a_converted_office_attachment_member_keeps_its_provenance(self):
        """A DOCX or XLSX mailed in an .eml or .msg is a member read on its own bytes:
        its reading is the converted text whose record names those bytes, and a
        workbook keeps its sheet spans."""
        from integrations.tests.test_doc_convert import (DOCX, XLSX, make_docx, make_xlsx,
                                                         stored_provenance_valid)
        word = make_docx(["Vendor confirmation: VC-300", "Customer PO: CPO_42"])
        sheet = make_xlsx()
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = "a@x.test", "b@x.test", "Docs"
        message.set_content("Attached.")
        for content, name, mime in ((word, "po.docx", DOCX), (sheet, "quote.xlsx", XLSX)):
            message.add_attachment(content, maintype="application", filename=name,
                                   subtype=mime.split("/", 1)[1])
        cases = {"docs.eml": (message.as_bytes(), "message/rfc822"),
                 "docs.msg": (make_msg([("po.docx", "", word), ("quote.xlsx", XLSX, sheet)]),
                              "application/vnd.ms-outlook")}
        for name, (content, mime) in cases.items():
            with self.subTest(name), unittest.mock.patch(
                    "integrations.doc_convert.installed_backend", return_value=None):
                found = {m.name: m for m in read_container(content, name, mime).members}
                for member, original in ((found["po.docx"], word), (found["quote.xlsx"], sheet)):
                    self.assertEqual(member.sha256, sha256(original).hexdigest())
                    document = extract_document(original, member.mime_type, original_url=URL,
                                                filename=member.name)
                    self.assertEqual((member.reading.method, member.reading.text),
                                     (document.method, document.text))
                po = extract_document(word, found["po.docx"].mime_type, original_url=URL,
                                      filename="po.docx")
                self.assertEqual(po.conversion["source_sha256"], found["po.docx"].sha256)
                self.assertTrue(stored_provenance_valid(po.evidence_attributes(URL)))
                quote = extract_document(sheet, found["quote.xlsx"].mime_type,
                                         original_url=URL, filename="quote.xlsx")
                self.assertEqual([s["sheet"] for s in quote.sheets], ["Quote"])
                self.assertEqual(next(c for c in quote.claims
                                      if c["field"] == "quote_number")["sheet"], "Quote")

    def test_multiline_items_and_shipping_specs_are_source_cited(self):
        content = ("Item: BTL-200\nDescription: Clear PET bottle\nQty: 5000 units\n"
                   "Unit Price: USD 0.33\n\nItem: CAP-200\nQty: 5000 units\n"
                   "Unit Price: USD 0.07\nShipping mode: air\nIncoterm: FOB\n")
        doc = extract_document(content.encode(), "text/plain", original_url=URL)
        self.assertEqual([item["item_code"] for item in doc.line_items], ["BTL-200", "CAP-200"])
        self.assertEqual([item["unit_price"] for item in doc.line_items], [0.33, 0.07])
        self.assertEqual({c["value"] for c in doc.claims if c["kind"] == "shipping"},
                         {"air", "FOB"})

    def test_scanned_form_labels_do_not_consume_the_next_line_as_a_code(self):
        content = ("AO LOT#: AO-26005B\nCUSTOMER: Diversified PO: REQUIRED DATE:\n"
                   "PRODUCT CODE:\nLINE NUMBER: 01\n"
                   "ITEM CODE: CAP-100\nPRODUCT CODE: BTL-100\n")
        document = extract_document(content.encode(), "text/plain", original_url=URL)
        self.assertEqual([claim["value"] for claim in document.claims
                          if claim["field"] == "product_code"], ["CAP-100", "BTL-100"])
        self.assertIn("AO-26005B", [claim["value"] for claim in document.claims
                                     if claim["field"] == "assembly_order"])
        self.assertEqual([claim["value"] for claim in document.claims
                          if claim["field"] == "customer"], ["Diversified"])

    def test_a_value_cut_at_an_ocr_column_gap_is_never_read_short(self):
        # tesseract keeps interword spaces (preserve_interword_spaces=1, PDF and image
        # alike): a unit or the rest of an identifier past a wide gap is the next cell.
        gap = ("Total Qty: 2012     cases\nCustomer PO: 4500     123\n"
               "Item: BTL-1\nQty: 50     ea\nUnit Price: USD 2\n")
        document = extract_document(gap.encode(), "text/plain", original_url=URL)
        self.assertEqual([c["unit"] for c in document.claims if c["kind"] == "quantity"],
                         ["unknown", "unknown"])
        self.assertEqual([item["unit"] for item in document.line_items], ["unknown"])
        self.assertTrue(line_item_text_witness(document.text, document.line_items[0]))
        po, = (c for c in document.claims if c["field"] == "customer_po")
        self.assertEqual((po["value"], po["confidence"], po["uncertain"]),
                         ("4500", "review", "split_at_gap"))
        facts = document.evidence_attributes(URL)
        self.assertEqual(facts["quantities"], [])
        self.assertEqual(facts["identifiers"][0]["confidence"], "review")
        # A unit on the number's own cell still reads; a labelled next cell is no unit.
        same = extract_document(b"Total Qty: 2012 cases\nQty: 7     Pallets: 2\n"
                                b"Customer PO: 4500     Date: 2026-01-02\n",
                                "text/plain", original_url=URL)
        self.assertEqual([(c["value"], c["unit"]) for c in same.claims
                          if c["kind"] == "quantity"], [(2012, "cases"), (7, "unspecified")])
        self.assertEqual(same.evidence_attributes(URL)["quantities"][0]["unit"], "cases")
        self.assertNotIn("uncertain", next(c for c in same.claims
                                           if c["field"] == "customer_po"))
        # An image is OCR-read with the same interword-space profile as a PDF page.
        done = subprocess.CompletedProcess([], 0, stdout=b"Total Qty: 2012     cases")
        with unittest.mock.patch("shutil.which", return_value="/usr/bin/tesseract"), \
                unittest.mock.patch("integrations.child_process.run",
                                    return_value=done) as run:
            self.assertEqual(local_image_text(b"png"), "Total Qty: 2012     cases")
        self.assertIn("preserve_interword_spaces=1", run.call_args.args[0])

    def test_ao_form_erp_and_oe_identifiers_are_cited_review_leads(self):
        # These adjacent fields are printed on Ricardo's AO-26005B page 1.
        # The transcript is isolated; the scanned PDF remains outside fixtures.
        content = ("AO LOT#: AO-26005B\n"
                   "ERP CODE: DVS-5003-A OE: OR1000151885\n"
                   "CUSTOMER: Diversified PO: REQUIRED DATE:\n")
        document = extract_document(content.encode(), "text/plain", original_url=URL)
        claims = {(row["field"], row["value"]): row for row in document.claims
                  if row["kind"] == "identifier"}
        self.assertEqual({("product_code", "DVS-5003-A"),
                          ("sales_order", "OR1000151885"),
                          ("assembly_order", "AO-26005B")}, set(claims))
        for key in (("product_code", "DVS-5003-A"),
                    ("sales_order", "OR1000151885")):
            row = claims[key]
            self.assertEqual(row["confidence"], "review")
            self.assertTrue(row["interpretation"])
            span = row["span"]
            self.assertEqual(content[span["start"]:span["end"]], key[1])
        attrs = document.evidence_attributes(URL, title="AO-26005B.pdf")
        self.assertEqual({row["type"] for row in attrs["identifiers"]},
                         {"assembly_order", "product_code", "sales_order"})
        self.assertTrue(all(row["confidence"] == "review" for row in
                            attrs["identifiers"] if row["type"] != "assembly_order"))
        blank = extract_document(b"ERP CODE: OE: OR1000151885\nOE:\nOR1000151885\n",
                                 "text/plain", original_url=URL)
        self.assertEqual([(row["field"], row["value"]) for row in blank.claims
                          if row["kind"] == "identifier"],
                         [("sales_order", "OR1000151885")])

    def test_ambiguous_dollar_is_never_a_literal_usd_cost(self):
        def prices(text):
            document = extract_document(text.encode(), "text/plain", original_url=URL)
            return document, [(c["currency"], c["confidence"], c["interpretation"])
                              for c in document.claims if c["kind"] == "price"]
        def costs(document):
            return [(c["amount"], c["currency"]) for c in
                    document.evidence_attributes(URL)["costs"]]
        literal, unresolved = ("literal", False), ("unknown", "literal", False)
        # A bare "$" names no currency, and a declaration never lets it
        # become a priced cost: the cited amount span does not state it.
        for text, expected in (
                ("Unit price: $12.00\n", unresolved),
                ("Currency: CAD\nUnit price: $12.00\n", ("CAD", "review", True)),
                ("All prices in USD\nUnit price: $12.00\n", ("USD", "review", True)),
                ("Currency: CAD\nCurrency: USD\nUnit price: $12.00\n", unresolved),
                ("Currency: USD/CAD\nUnit price: $12.00\n", unresolved),
                ("Currency: EUR\nUnit price: $12.00\n", unresolved),
                ("Currency: CAD\nUnit price: €12.00\n", unresolved),
                ("Unit price: 12.00\n", unresolved)):
            with self.subTest(text=text):
                document, found = prices(text)
                self.assertEqual(found, [expected])
                self.assertEqual(costs(document), [])
        # Explicit codes and single-currency symbols stay literal priced facts.
        for text, currency in (("Unit price: CAD 12.00\n", "CAD"),
                               ("Unit price: USD $12.00\n", "USD"),
                               ("Currency: CAD\nUnit price: USD 12.00\n", "USD"),
                               ("Unit price: €12.00\n", "EUR"),
                               ("Currency: GBP\nUnit price: £12.00\n", "GBP")):
            with self.subTest(text=text):
                document, found = prices(text)
                self.assertEqual(found, [(currency, *literal)])
                self.assertEqual(costs(document), [(12, currency)])

    def test_a_malformed_nested_multipart_loses_neither_body_nor_attachment(self):
        # A multipart part without its boundary (a truncated mail) has a str payload;
        # walking into it raised, and the body and every member were lost.
        raw = (b'From: a@x.test\nTo: b@x.test\nSubject: Fwd\nMIME-Version: 1.0\n'
               b'Content-Type: multipart/mixed; boundary="OUT"\n\n--OUT\n'
               b'Content-Type: multipart/alternative; boundary="ALT"\n\n--ALT\n'
               b'Content-Type: text/plain\n\nPetra PO: PPO-201\n--ALT\n'
               b'Content-Type: multipart/related\n\nno boundary here\n--ALT--\n--OUT\n'
               b'Content-Type: multipart/mixed; boundary="GONE"\n\ntruncated\n--OUT\n'
               b'Content-Type: text/plain\nContent-Disposition: attachment; filename="conf.txt"'
               b'\n\nPetra PO: PPO-202\n--OUT--\n')
        document = extract_document(raw, "message/rfc822", original_url=URL)
        self.assertEqual(document.status, "extracted")
        self.assertIn("PPO-201", document.text)
        self.assertNotIn("PPO-202", document.text)
        found = read_container(raw, "m.eml", "message/rfc822")
        self.assertEqual([(m.name, m.reading.text) for m in found.members
                          if m.name == "conf.txt"], [("conf.txt", "Petra PO: PPO-202")])
        self.assertNotIn("container_unreadable", [g.get("reason") for g in found.gaps])

    def test_currency_declaration_is_scoped_to_its_own_document_and_line_items(self):
        message = EmailMessage()
        message["Subject"] = "Quote"
        message["From"] = "supplier@example.test"
        message.set_content("Currency: CAD\nFreight quote: $500\n")
        # A name cannot widen the mail's declaration: an attachment is its own document.
        message.add_attachment(b"Item: BTL-100 | Qty: 100 bottles | Unit Price: $0.12\n",
                               maintype="text", subtype="plain", filename="quote[1].txt")
        message.add_attachment(b"Currency: USD\nItem: CAP-200 | Qty: 50 units | Unit Price: $0.07\n",
                               maintype="text", subtype="plain", filename="caps.txt")
        document = extract_document(message.as_bytes(), "message/rfc822",
                                    original_url="https://outlook.example.test/mail/m2")
        self.assertEqual([(c["field"], c["currency"], c["confidence"]) for c in document.claims
                          if c["kind"] == "price"], [("freight quote", "CAD", "review")])
        self.assertEqual(document.evidence_attributes(
            "https://outlook.example.test/mail/m2")["costs"], [])
        quote, caps = (extract_document(f["data"], f["hint"], original_url=URL)
                       for f in members(message.as_bytes(), "m2.eml")["files"])
        self.assertEqual([(c["field"], c["currency"], c["confidence"]) for c in
                          (*quote.claims, *caps.claims) if c["kind"] == "price"],
                         [("unit price", "unknown", "literal"), ("unit price", "USD", "review")])
        self.assertEqual([(row["item_code"], row["currency"])
                          for row in (*quote.line_items, *caps.line_items)],
                         [("BTL-100", "unknown"), ("CAP-200", "USD")])
        # The stored line is re-witnessed against its whole document, so a later
        # reparse agrees; a stored line claiming USD for a bare "$" in an
        # undeclared document (the old extractor's output) does not.
        for document in (quote, caps):
            for row in document.line_items:
                self.assertTrue(line_item_text_witness(document.text, row))
        stale = {**quote.line_items[0], "currency": "USD"}
        self.assertFalse(line_item_text_witness(quote.text, stale))

    def test_currency_declaration_scan_is_linear_and_line_bounded(self):
        for text in ("Currency" + " " * 40_000 + "\nUnit price: $1\n",
                     "All prices" + "\t" * 40_000 + "x\n",
                     "Currency:" + " " * 40_000 + "CADX\n"):
            started = time.monotonic()
            extract_document(text.encode(), "text/plain", original_url=URL)
            self.assertLess(time.monotonic() - started, 2.0)
        # The code must end the line: lists, alternatives and asides are not
        # a single declaration.
        for declaration in ("Currency: CAD; USD", "Currency: CAD - USD", "Currency: CAD\tUSD",
                            "Currency: CAD (converted from USD)", "Currency: USD/CAD"):
            with self.subTest(declaration=declaration):
                document = extract_document(f"{declaration}\nUnit price: $12.00\n".encode(),
                                            "text/plain", original_url=URL)
                self.assertEqual([c["currency"] for c in document.claims
                                  if c["kind"] == "price"], ["unknown"])
        document = extract_document(b"Currency:   CAD  \nUnit price: $12.00\n", "text/plain",
                                    original_url=URL)
        self.assertEqual([c["currency"] for c in document.claims if c["kind"] == "price"],
                         ["CAD"])

    def test_extraction_is_linear_on_adversarial_whitespace(self):
        # Untrusted mail, OCR and spreadsheet text reaches every pattern. Most
        # inputs below took tens of seconds at ~95k characters when adjacent
        # optional whitespace runs overlapped.
        for prefix, filler in (("Unit price:", " \n"), ("price:", " "),
                               ("", " \n"), ("", "\r\n"), ("AO LOT #", "\t"),
                               ("Currency", " "), ("material", "\n"), ("AO", " "),
                               ("quote", "\n"), ("invoice number", " "), ("SKU", "\t"),
                               ("project", " "), ("customer PO", " "), ("ERP CODE", " ")):
            text = prefix + filler * ((95_000 - len(prefix)) // len(filler))
            with self.subTest(prefix=prefix, filler=filler):
                started = time.monotonic()
                extract_document(text.encode(), "text/plain", original_url=URL)
                self.assertLess(time.monotonic() - started, 2.0)

    def test_party_values_are_linear_and_never_blank(self):
        for label, filler in (("supplier:", " "), ("Ship to:", "\t")):
            text = label + filler * 90_000 + "a" * 300 + "\n"
            started = time.monotonic()
            extract_document(text.encode(), "text/plain", original_url=URL)
            self.assertLess(time.monotonic() - started, 2.0)
        # A party value starts at its first non-blank character, so a label
        # with nothing (or one character) after it yields no party claim.
        for text in ("Supplier:   \n", "Supplier: x\n", "Vendor:\t\n"):
            document = extract_document(text.encode(), "text/plain", original_url=URL)
            self.assertEqual([c for c in document.claims if c["kind"] == "party"], [], text)
        document = extract_document(b"Supplier:   Acme Packaging   PO: 1\n", "text/plain",
                                    original_url=URL)
        self.assertEqual([c["value"] for c in document.claims if c["kind"] == "party"],
                         ["Acme Packaging"])

    def test_crlf_currency_declaration(self):
        # Windows-exported text and raw mail bodies keep CRLF line ends.
        document = extract_document(b"Currency: CAD\r\nFreight quote: $500\r\n",
                                    "text/plain", original_url=URL)
        self.assertIn("\r\n", document.text)
        # A lone CR at the very end of the text also ends the line.
        tail = extract_document(b"Freight quote: $5\nCurrency: CAD\r", "text/plain",
                                original_url=URL)
        self.assertEqual([c["currency"] for c in tail.claims if c["kind"] == "price"], ["CAD"])
        self.assertEqual([(c["currency"], c["confidence"]) for c in document.claims
                          if c["kind"] == "price"], [("CAD", "review")])

    def test_unsupported_bytes_and_url_are_explicit(self):
        unsupported = extract_document(b"\x00\x01", "image/png", original_url=URL)
        self.assertEqual(unsupported.status, "unsupported")
        self.assertEqual(unsupported.claims, ())
        with self.assertRaises(ValidationError):
            extract_document(b"text", "text/plain", original_url="http://example.test/unsafe")


def _importable(name: str) -> bool:
    """True only if the module really imports.

    `find_spec` is truthy for an installed-but-broken package; a pypdf whose
    native dependency panics on import (a pyo3 PanicException, which is a
    BaseException) must skip the PDF tests rather than fail them.
    """
    try:
        importlib.import_module(name)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return False
    return True


class HtmlTableTest(unittest.TestCase):
    def test_table_cells_are_tab_separated_and_rows_are_lines(self):
        html = (b"<table><tr><th>Item</th><th>Qty</th></tr>"
                b"<tr><td>BTL-100</td><td>10</td></tr></table>")
        message = EmailMessage()
        message["Subject"] = "Table"
        message["From"] = "a@example.test"
        message["To"] = "b@example.test"
        message.set_content(html.decode(), subtype="html")
        document = extract_document(message.as_bytes(), "message/rfc822",
                                    original_url="https://outlook.example.test/mail/m2")
        lines = [line for line in document.text.splitlines() if line.strip()][-2:]
        self.assertEqual([line.strip("\t ").split("\t") for line in lines],
                         [["Item", "Qty"], ["BTL-100", "10"]])
        self.assertNotIn("Qty10", document.text)


@unittest.skipUnless(_importable("pypdf") and _importable("reportlab"),
                     "PDF format test requires importable pypdf and reportlab runtime")
class PdfDocumentUnderstandingTest(unittest.TestCase):
    @staticmethod
    def pdf(lines):
        from reportlab.pdfgen import canvas
        buffer = io.BytesIO()
        page = canvas.Canvas(buffer)
        y = 750
        for line in lines:
            page.drawString(72, y, line)
            y -= 20
        page.save()
        return buffer.getvalue()

    def test_generated_pdf_with_real_text_layer(self):
        content = self.pdf(["Petra PO: PPO-200", "Supplier: Acme Packaging",
            "Item: BTL-100 | Qty: 100,000 bottles | Unit Price: USD 0.12",
            "Requested delivery: 2026-11-15"])
        result = extract_document(content, "application/pdf", original_url=URL)
        self.assertEqual(result.status, "extracted")
        self.assertEqual(result.pages[0]["page"], 1)
        self.assertTrue(any(c["kind"] == "identifier" and c["value"] == "PPO-200" for c in result.claims))
        self.assertEqual(result.line_items[0]["quantity"], 100000)
        self.assertEqual(result.line_items[0]["unit_price"], 0.12)

    def test_blank_pdf_abstains_from_ocr(self):
        from reportlab.pdfgen import canvas
        buffer = io.BytesIO()
        canvas.Canvas(buffer).save()
        result = extract_document(buffer.getvalue(), "application/pdf", original_url=URL)
        self.assertEqual(result.status, "no_text")
        self.assertIn("image_only_or_empty_pdf_requires_ocr", result.warnings)
        self.assertEqual(result.claims, ())

    def test_an_unreadable_pdf_reports_method_none_and_a_typed_failure(self):
        """Directive 440211923d3f (4): nothing read is method ``none`` with the reason in
        ``failure``, also when pypdf opened the PDF but could not read a page."""
        from unittest.mock import patch
        from pypdf import PageObject

        with patch("integrations.doc_convert.installed_backend", return_value=None), \
                patch.object(logging.getLogger("pypdf"), "disabled", True):
            malformed = extract_document(b"%PDF-1.4\nnot a pdf\n", "application/pdf",
                                         original_url=URL)
            with patch.object(PageObject, "extract_text", side_effect=ValueError("bad")):
                page = extract_document(self.pdf(["Petra PO: PPO-200"]), "application/pdf",
                                        original_url=URL)
        for document, failure in ((malformed, "pdf_parse_error"), (page, "pdf_parse_error")):
            self.assertEqual((document.status, document.method, document.failure, document.text),
                             ("unsupported", "none", failure, ""))
        self.assertIn("pdf_parse_error:ValueError", page.warnings)
        self.assertIsNone(extract_document(self.pdf(["x"]), "application/pdf",
                                           original_url=URL).failure)

    @unittest.skipUnless(OCR_TOOLS, "poppler and tesseract not installed")
    def test_pdf_unreadable_typed_by_bytes(self):
        """A PDF poppler refuses (pdfinfo exit 1) and pypdf cannot read either is a typed
        OCR failure named by the bytes' shape, never one undifferentiated cause."""
        from integrations import document_understanding as du
        good = self.pdf(["Petra PO: PPO-200"])
        cases = (
            ("not_a_pdf", b""),
            ("pdf_truncated", b"%PDF-1.4\n"),
            ("not_a_pdf", b"<html><body>" + b"login " * 40 + b"</body></html>"),
            ("not_a_pdf", b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2),
            ("pdf_truncated", good[:len(good) // 2]),
            # A header and an end but no stream anywhere: no page can hold a mark.
            ("pdf_stub", b"%PDF-1.4\n" + b"1 0 obj\n<<>>\nendobj\n" * 8 + b"%%EOF\n"),
            ("pdf_stub", b"%PDF-1.4\n%%EOF\n"),
            ("pdf_stub", b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
                         b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n%%EOF\n"),
            # A stream is content that a repair might read: never a stub.
            ("pdf_unreadable", b"%PDF-1.4\n" + b"1 0 obj\n<<>>\nendobj\n" * 8 +
             b"9 0 obj\n<< /Length 9 >>\nstream\nBT (x) Tj\nendstream\nendobj\n%%EOF\n"),
        )
        for cause, content in cases:
            with self.subTest(cause=cause, size=len(content)):
                with self.assertRaises(du.OcrFailed) as raised:
                    du.read_pages(content, "pdf", ocr=True)
                self.assertEqual(raised.exception.cause, cause)
                from integrations.captured_text import capture_text
                status, text = capture_text(content, {"name": "po.pdf",
                                                      "mime_type": "application/pdf"})
                self.assertEqual((status, text), (f"ocr_failed:{cause}", None))

    @unittest.skipUnless(OCR_TOOLS, "poppler and tesseract not installed")
    def test_pypdf_fallback_when_pdfinfo_refuses(self):
        """poppler's pdfinfo refuses (exit 1) a PDF that pypdf reads: the text layer is
        read by pypdf (its method kept), not a failed OCR run."""
        from integrations import document_understanding as du
        content = self.pdf(["Petra PO: PPO-200", "Supplier: Acme Packaging"])
        real = du.child_process.run

        def refuse_pdfinfo(command, **kwargs):
            if os.path.basename(command[0]) == "pdfinfo":
                return subprocess.CompletedProcess(command, 1, b"",
                                                   b"Syntax Error: Couldn't find trailer\n")
            return real(command, **kwargs)

        with unittest.mock.patch.object(du.child_process, "run", refuse_pdfinfo):
            reading = du.read_pages(content, "pdf", ocr=True)
            found = extract_document(content, "application/pdf", original_url=URL,
                                     local_pdf_ocr=True)
        for got in (reading, found):
            self.assertEqual(got.method, "pypdf-text")
            self.assertIn("PPO-200", got.text)
        self.assertEqual(found.status, "extracted")
        self.assertIn("pdf_read_by_pypdf_after_poppler_refused", reading.warnings)

    @unittest.skipUnless(POPPLER, "poppler not installed")
    def test_an_owner_password_never_blocks_a_read_and_a_user_password_does(self):
        """DECIDED (architect-20261008T1122Z): an owner password restricts editing and
        printing, not reading. Every reader reads such a PDF, poppler's text layer
        included; only a PDF that needs its user password is ``encrypted``."""
        import sys
        from unittest.mock import patch
        from pypdf import PdfReader, PdfWriter
        from integrations import document_understanding as du

        def encrypted(user_password):
            writer = PdfWriter(clone_from=PdfReader(io.BytesIO(self.pdf(["Petra PO: PPO-200"]))))
            writer.encrypt(user_password=user_password, owner_password="owner",
                           algorithm="RC4-128")
            buffer = io.BytesIO()
            writer.write(buffer)
            return buffer.getvalue()

        owner, user = encrypted(""), encrypted("user")
        self.assertIn("PPO-200", du.local_pdf_text_layer(owner)[0])
        self.assertIsNone(du.local_pdf_text_layer(user))
        with patch("integrations.doc_convert.installed_backend", return_value=None):
            readers = [extract_document(owner, "application/pdf", original_url=URL)]
            with patch.dict(sys.modules, {"pypdf": None}):
                readers.append(extract_document(owner, "application/pdf", original_url=URL))
                refused = extract_document(user, "application/pdf", original_url=URL)
        self.assertEqual([(d.status, d.method) for d in readers],
                         [("extracted", "pypdf-text"), ("extracted", "pdftotext-text")])
        self.assertEqual((refused.status, refused.method, refused.failure),
                         ("unsupported", "none", "encrypted"))



class BrokenPdfParserTest(unittest.TestCase):
    def test_skip_guard_treats_panicking_import_as_unavailable(self):
        from unittest.mock import patch

        class PanicException(BaseException):
            pass

        with patch("importlib.import_module",
                   side_effect=PanicException("Python API call failed")):
            self.assertFalse(_importable("pypdf"))
        with patch("importlib.import_module", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _importable("pypdf")
        self.assertTrue(_importable("json"))

    def test_panicking_pdf_parser_reads_the_text_layer_else_degrades_to_unsupported(self):
        # Reproduces the cloud failure: `import pypdf` raised a pyo3
        # PanicException (a BaseException), which escaped `except ImportError`.
        # The one reader then reads poppler's text layer; only without it is the
        # PDF unsupported.
        import sys
        from unittest.mock import patch
        from integrations.tests.test_doc_convert import make_pdf

        panic = type("PanicException", (BaseException,), {"__module__": "pyo3_runtime"})
        pypdf = type(sys)("pypdf")
        pypdf.__getattr__ = lambda name: (_ for _ in ()).throw(panic("Python API call failed"))
        pdf = make_pdf([["Petra PO: PPO-200"], ["Quote Number: Q-5"]])
        with patch.dict(sys.modules, {"pypdf": pypdf}), \
                patch("integrations.doc_convert.installed_backend", return_value=None):
            with patch("integrations.document_understanding.shutil.which", return_value=None):
                missing = extract_document(pdf, "application/pdf", original_url=URL)
            read = (extract_document(pdf, "application/pdf", original_url=URL)
                    if POPPLER else None)
        self.assertEqual((missing.text, missing.pages, missing.status, missing.method,
                          missing.failure), ("", (), "unsupported", "none", "parser_panicked"))
        self.assertEqual(missing.warnings, ("pdf_parser_broken_reinstall_pypdf",))
        if read is not None:
            self.assertEqual((read.status, read.method, [p["page"] for p in read.pages]),
                             ("extracted", "pdftotext-text", [1, 2]))
            quote = next(c for c in read.claims if c["field"] == "quote_number")
            self.assertEqual(quote["page"], 2)
            # The method names the reader that read it, and why pypdf did not stays
            # visible (directive 440211923d3f (4)); the same without pypdf installed.
            self.assertIn("pdf_parser_broken_reinstall_pypdf", read.warnings)
            with patch.dict(sys.modules, {"pypdf": None}), \
                    patch("integrations.doc_convert.installed_backend", return_value=None):
                uninstalled = extract_document(pdf, "application/pdf", original_url=URL)
            self.assertEqual((uninstalled.status, uninstalled.method, uninstalled.failure),
                             ("extracted", "pdftotext-text", None))
            self.assertIn("pdf_parser_unavailable_install_pypdf", uninstalled.warnings)

    def test_poppler_user_password_refusal_is_keyed_on_exit_status_and_word(self):
        """Directive f6515fd2bede (3)(4): poppler's refusal to open a PDF without its
        user password is known by exit status 1 and the word "password" in any case, not
        one release's message; the capture's OCR path then says ``unsupported`` (OCR
        never ran), and only an OCR run that failed ``ocr_failed``."""
        from subprocess import CompletedProcess
        from unittest.mock import patch
        from integrations import document_understanding as du
        from integrations.captured_text import capture_text

        def poppler(opened):
            def run(command, **_):
                self.assertEqual(Path(command[0]).name, "pdfinfo", command)  # nothing read
                return opened
            return run

        cases = (
            (CompletedProcess([], 1, b"", b"Command Line Error: incorrect password\n"),
             None, "unsupported"),
            (CompletedProcess([], 1, b"", b"Error: INCORRECT PASSWORD\n"), None, "unsupported"),
            (CompletedProcess([], 1, b"", b"Syntax Error: Couldn't find trailer dictionary\n"),
             "local_pdf_ocr_failed", "ocr_failed:pdf_unreadable"),
            (CompletedProcess([], 3, b"", b"Permission Error: password\n"),
             "local_pdf_ocr_failed", "ocr_failed:pdfinfo_exit"),
            # The text layer refuses a PDF over the page bound; OCR reads its first pages
            # (OcrPageCapTest), which this pdfinfo stub cannot list: no capture status.
            (CompletedProcess([], 0, b"Encrypted:      no\nPages:          201\n", b""),
             "PDF page count exceeds bound", None))
        record = {"name": "scan.pdf", "mime_type": "application/pdf"}
        with patch.object(du.platform, "system", return_value="Linux"), \
                patch.object(du.shutil, "which", side_effect=lambda tool: f"/usr/bin/{tool}"):
            for opened, layer, captured in cases:
                with self.subTest(opened.stderr or opened.stdout), \
                        patch.object(du.child_process, "run", side_effect=poppler(opened)):
                    if layer is None:
                        self.assertIsNone(du.local_pdf_text_layer(b"%PDF-1.4\n"))
                    else:
                        with self.assertRaisesRegex(ValidationError, f"^{layer}$"):
                            du.local_pdf_text_layer(b"%PDF-1.4\n")
                    if captured is not None:
                        self.assertEqual(capture_text(WHOLE_PDF, record), (captured, None))


class OnePdfReaderTest(unittest.TestCase):
    """gap-105e75f678ed: one function chooses how a PDF/DOCX/PPTX is read."""

    # The calls that read a PDF or Office file, and the functions allowed to make them.
    READ_CALLS = {"PdfReader", "extract_text", "local_pdf_text_layer", "doc_convert.convert",
                  "convert_stdlib", "_portable_pdf_text", "_macos_pdf_text", "MarkItDown"}
    READERS = {"_pypdf_open", "_pypdf_pages", "_pdftotext_pages", "_ocr_reading",
               "_pdf_reading", "_office_reading", "local_pdf_text_layer"}

    @classmethod
    def references(cls, source):
        """(scope, name) of every reference to a call in ``READ_CALLS``, however it is
        made: a call, an alias (``import ... as``, an assignment, a bound method) or
        ``getattr`` with the name; ``scope`` is the enclosing function."""
        import ast

        tree = ast.parse(source)
        modules = {"doc_convert"}  # the names integrations.doc_convert is bound to
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules |= {alias.asname or alias.name for alias in node.names
                            if alias.name.rsplit(".", 1)[-1] == "doc_convert"}

        def module(node):
            return ((isinstance(node, ast.Name) and node.id in modules) or
                    (isinstance(node, ast.Attribute) and node.attr == "doc_convert"))

        def named(name, holder=None):
            if name == "convert":
                return "doc_convert.convert" if holder else None
            return name if name in cls.READ_CALLS else None

        def found(node):
            if isinstance(node, ast.Name):
                return [named(node.id)]
            if isinstance(node, ast.Attribute):
                return [named(node.attr, module(node.value))]
            if isinstance(node, ast.ImportFrom):
                source = (node.module or "").rsplit(".", 1)[-1] == "doc_convert"
                return [named(alias.name, source) for alias in node.names]
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
                    node.func.id == "getattr" and len(node.args) >= 2 and
                    isinstance(node.args[1], ast.Constant) and
                    isinstance(node.args[1].value, str)):
                return [named(node.args[1].value, module(node.args[0]))]
            return []

        into = set()

        def walk(node, scope):
            for child in ast.iter_child_nodes(node):
                inner = (child.name if isinstance(child, (ast.FunctionDef,
                                                          ast.AsyncFunctionDef)) else scope)
                into.update((inner, name) for name in found(child) if name)
                walk(child, inner)

        walk(tree, "<module>")
        return into

    def test_read_pages_is_the_only_place_that_chooses_the_reader(self):
        from pathlib import Path

        # An alias or getattr does not hide a reader (directive 440211923d3f (2)).
        self.assertEqual(self.references(
            "import integrations.doc_convert as dc\n"
            "from pypdf import PdfReader as Reader\n"
            "def sneaky(data, du, page):\n"
            "    read, text = getattr(du, 'local_pdf_text_layer'), page.extract_text\n"
            "    run = getattr(dc, 'convert')\n"
            "    return read(data), dc.convert(data), Reader(data), run(data), text()\n"),
            {("<module>", "PdfReader"), ("sneaky", "local_pdf_text_layer"),
             ("sneaky", "extract_text"), ("sneaky", "doc_convert.convert")})
        root = Path(__file__).resolve().parents[2]
        readers = set()
        for path in sorted(root.glob("**/*.py")):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in f"/{rel}" or rel.startswith(("evals/", ".")):
                continue  # tests and eval fixtures generate PDFs; they choose no reader
            readers |= {(rel, scope, name)
                        for scope, name in self.references(path.read_text(encoding="utf-8"))}
        # doc_convert is the converter adapter: it imports and calls MarkItDown, never
        # chooses it. Its convert() falls back to its own stdlib converter, with the
        # reason recorded, for a format whose MarkItDown extra is not installed.
        allowed = {("integrations/doc_convert.py", "convert", "convert_stdlib"),
                   ("integrations/doc_convert.py", "convert", "MarkItDown"),
                   ("integrations/doc_convert.py", "_import_backend", "MarkItDown")}
        outside = {entry for entry in readers - allowed
                   if entry[0] != "integrations/document_understanding.py" or
                   entry[1] not in self.READERS}
        self.assertEqual(outside, set())
        for name in ("PdfReader", "extract_text", "doc_convert.convert", "local_pdf_text_layer"):
            self.assertTrue(any(entry[2] == name for entry in readers), name)

    def test_callers_go_through_read_pages(self):
        from unittest.mock import patch
        from integrations import document_understanding as du
        from operating.use_case_parts import account

        stub = du._paged("0" * 64, "pypdf-text", [du.Page(1, "Petra PO: PPO-1", "pypdf-text")])
        with patch.object(du, "read_pages", return_value=stub) as reader:
            document = extract_document(b"%PDF-1.4", "application/pdf", original_url=URL)
            parts = account(b"%PDF-1.4\n", "po.pdf")["parts"]
        self.assertEqual([call.args[1] for call in reader.call_args_list], ["pdf", "pdf"])
        self.assertEqual((document.method, document.text), ("pypdf-text", "Petra PO: PPO-1"))
        self.assertEqual([(p["path"], p["method"], p["source_sha256"]) for p in parts
                          if p["state"] == "extracted"],
                         [("po.pdf!/page/1", "pypdf-text", "0" * 64)])
        with self.assertRaises(ValueError):  # a kind it does not read fails closed
            du.read_pages(b"PK", "xlsx")

    @unittest.skipUnless(_importable("pypdf"), "the page bound is pypdf's page count")
    def test_first_pages_reads_a_bounded_prefix_and_fails_closed_otherwise(self):
        """Directive 23c4347efa25 (1): ``first_pages`` reads a PDF over the page bound on
        its first pages and names the rest; without it such a PDF is still refused
        whole, and a reading it cannot bound fails closed."""
        from unittest.mock import patch
        from integrations import document_understanding as du
        from integrations.tests.test_doc_convert import make_pdf

        pdf = make_pdf([[f"Page marker M{n}"] for n in range(1, 4)])
        with patch("integrations.doc_convert.installed_backend", return_value=None):
            reading = du.read_pages(pdf, "pdf", max_pages=2, first_pages=True)
            with self.assertRaisesRegex(ValidationError, "^PDF page count exceeds bound$"):
                du.read_pages(pdf, "pdf", max_pages=2)
        self.assertEqual(([page.number for page in reading.pages], reading.skipped_pages,
                          reading.method), ([1, 2], (3, 3), "pypdf-text"))
        self.assertNotIn("M3", reading.text)
        for unbounded in ({"max_pages": None}, {"max_pages": 0}):
            with self.subTest(unbounded), self.assertRaises(ValueError):
                du.read_pages(pdf, "pdf", first_pages=True, **unbounded)


class OcrPageCapTest(unittest.TestCase):
    """Directive 5ba82b6040ec: the OCR path reads pages 1-``MAX_PAGES`` of a longer PDF
    and records the rest skipped by the page cap, the rule the use-case intake keeps;
    directive d82b7c38fc7b: so do the path without OCR and the capture's text."""

    @unittest.skipUnless(_importable("pypdf") or POPPLER, "no PDF text reader installed")
    def test_without_ocr_a_long_pdf_is_read_on_its_first_pages_everywhere(self):
        """No outright refusal: ``extract_document`` without OCR reads pages 1-200 and
        records 201-250 skipped by the page cap; the capture stores those 200 pages'
        text, and every reading of it (the pinned bytes, the pinned OCR text, a member
        of a container) carries the skipped range. A pinned text of 200 pages whose
        PDF's page count cannot be known fails closed."""
        from unittest.mock import patch
        from zipfile import ZipFile
        from integrations import captured_text as ct
        from integrations import document_understanding as du
        from integrations.tests.test_doc_convert import make_pdf
        from ontology.mentions import text_pages

        pdf = make_pdf([[f"Page marker M{n:03d}"] for n in range(1, 251)])
        gap = {"first_page": 201, "last_page": 250, "cap": "pages", "limit": du.MAX_PAGES}
        box = io.BytesIO()
        with ZipFile(box, "w") as archive:
            archive.writestr("big.pdf", pdf)
        record = {"name": "big.pdf", "mime_type": "application/pdf"}
        with patch("integrations.doc_convert.installed_backend", return_value=None), \
                patch.object(du, "local_ocr_available", return_value=False):
            document = extract_document(pdf, "application/pdf", original_url=URL)
            status, text = ct.capture_text(pdf, record)
            reading = ct.read_text(pdf, "application/pdf", filename="big.pdf")
            pinned = ct.read_text(pdf, "application/pdf", filename="big.pdf", text=text)
            container = read_container(box.getvalue(), "box.zip")
            with patch.object(du, "pdf_page_count", return_value=None), \
                    self.assertRaisesRegex(ValidationError, "^PDF page count unknown$"):
                ct.read_text(pdf, "application/pdf", filename="big.pdf", text=text)
        self.assertEqual((document.status, document.skipped_pages,
                          [page["page"] for page in document.pages]),
                         ("extracted", (201, 250), list(range(1, 201))))
        self.assertIn("pdf_pages_skipped_by_cap:201-250", document.warnings)
        self.assertEqual(document.evidence_attributes(URL)["extraction"]["skipped_by_cap"], gap)
        self.assertEqual((status, len(text_pages(text))), ("extracted", 200))
        self.assertIn("M200", text_pages(text)[-1])
        self.assertNotIn("M201", text)
        for found in (reading, pinned):
            self.assertEqual((found.status, len(found.pages), found.skipped_by_cap),
                             ("extracted", 200, gap))
            self.assertEqual(found.page(200), text_pages(text)[-1])
        (member,) = container.members
        self.assertEqual((member.path, member.text_status, member.reading.skipped_by_cap),
                         ("big.pdf", "extracted", gap))
        self.assertIn({"path": "big.pdf!/page/201-250", "kind": "pdf_page",
                       "state": "skipped_by_cap", "cap": "pages", "limit": du.MAX_PAGES},
                      container.gaps)
        # Within the bound nothing is skipped, and a pinned text needs no page count.
        small = make_pdf([["Petra PO: PPO-200"]])
        with patch.object(du, "pdf_page_count", side_effect=AssertionError("counted")):
            self.assertIsNone(ct.read_text(small, "application/pdf", filename="po.pdf",
                                           text="Petra PO: PPO-200").skipped_by_cap)
        self.assertIsNone(ct.read_text(small, "application/pdf").skipped_by_cap)

    @unittest.skipUnless(all(shutil.which(tool) for tool in
                             ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")),
                         "local poppler + tesseract OCR not installed")
    def test_portable_ocr_reads_the_first_pages_and_records_the_rest(self):
        from integrations import document_understanding as du
        from integrations.captured_text import capture_text, read_text
        from integrations.tests.test_doc_convert import make_pdf
        from ontology.mentions import text_pages

        pdf = make_pdf([[f"Page marker M{n:03d}"] for n in range(1, du.MAX_PAGES + 7)])
        with unittest.mock.patch.object(du.platform, "system", return_value="Linux"):
            document = extract_document(pdf, "application/pdf", original_url=URL,
                                        local_pdf_ocr=True)
            # The capture stores the OCR text of pages 1-200; its readings name the rest.
            captured = capture_text(pdf, {"name": "big.pdf", "mime_type": "application/pdf"})
            reread = read_text(pdf, "application/pdf", filename="big.pdf", local_pdf_ocr=True)
        self.assertEqual((document.status, document.method, document.skipped_pages),
                         ("extracted", "poppler-tesseract", (201, 206)))
        self.assertEqual([page["page"] for page in document.pages], list(range(1, 201)))
        last = document.pages[-1]
        self.assertIn("M200", document.text[last["start"]:last["end"]])
        self.assertNotIn("M201", document.text)
        self.assertIn("pdf_pages_skipped_by_cap:201-206", document.warnings)
        self.assertEqual(document.evidence_attributes(URL)["extraction"]["skipped_by_cap"],
                         {"first_page": 201, "last_page": 206, "cap": "pages",
                          "limit": du.MAX_PAGES})
        self.assertEqual((captured[0], len(text_pages(captured[1]))), ("extracted", 200))
        self.assertEqual((len(reread.pages), reread.skipped_by_cap["first_page"],
                          reread.skipped_by_cap["last_page"]), (200, 201, 206))
        # Within the bound nothing is skipped.
        within = extract_document(make_pdf([["Petra PO: PPO-200"]]), "application/pdf",
                                  original_url=URL, local_pdf_ocr=True)
        self.assertEqual((within.skipped_pages, "skipped_by_cap" in
                          within.evidence_attributes(URL)["extraction"]), (None, False))

    def test_macos_helper_is_given_the_page_bound_and_its_count_is_checked(self):
        """The Swift helper reads at most the bound it is given and reports the page
        count: past it the pages are skipped with OCR's first pages, an inconsistent
        report fails closed (Python side; the helper itself needs a Mac run)."""
        import json
        from subprocess import CompletedProcess
        from integrations import document_understanding as du

        commands = []

        def helper(rows, count):
            def run(command, **_):
                commands.append(command)
                payload = {"pages": [{"page": n, "text": f"M{n}", "method": "pdfkit-text"}
                                     for n in range(1, rows + 1)], "page_count": count}
                return CompletedProcess(command, 0, json.dumps(payload).encode(), b"")
            return run

        def read(rows, count, **options):
            with unittest.mock.patch.object(du.child_process, "run",
                                            side_effect=helper(rows, count)):
                return du.read_pages(b"%PDF-1.4\n", "pdf", ocr=True, **options)

        with unittest.mock.patch.object(du.platform, "system", return_value="Darwin"), \
                unittest.mock.patch.object(du, "_local_ocr_backend", return_value="macos"), \
                unittest.mock.patch.object(du.Path, "is_file", return_value=True):
            reading = read(200, 250, first_pages=True)
            self.assertEqual(commands[-1][-1], "200")
            self.assertEqual((len(reading.pages), reading.skipped_pages,
                              reading.method), (200, (201, 250), "macos-pdfkit-vision"))
            self.assertIsNone(read(3, 3, first_pages=True).skipped_pages)
            self.assertEqual(read(2, 9, max_pages=2, first_pages=True).skipped_pages, (3, 9))
            self.assertEqual(commands[-1][-1], "2")
            with self.assertRaisesRegex(ValidationError, "^PDF page count exceeds bound$"):
                read(200, 250)
            for rows, count in ((199, 250), (5, 4), (3, None), (0, 0)):
                with self.subTest(rows=rows, count=count), \
                        self.assertRaisesRegex(ValidationError, "local_pdf_ocr_pages_invalid"):
                    read(rows, count, first_pages=True)


def process_gone(pid: int, wait: float = 10.0) -> bool:
    """Whether ``pid`` is gone (or a zombie no reaper collected yet) within ``wait`` s."""
    end = time.monotonic() + wait
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        stat = Path(f"/proc/{pid}/stat")
        try:
            if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except OSError:
            pass
        if time.monotonic() > end:
            return False
        time.sleep(0.05)


def wait_for_pid(path: Path, wait: float = 20.0) -> int:
    end = time.monotonic() + wait
    while not (path.is_file() and path.read_text().strip()):
        if time.monotonic() > end:
            raise AssertionError(f"no pid written to {path}")
        time.sleep(0.02)
    return int(path.read_text())


class AbandonedOcrTest(unittest.TestCase):
    """Directive ccc4760bc112 item 2: an OCR read its caller abandons (the oracle's
    240 s text budget) leaves no OCR child running and no ``plx-pdf-ocr-*`` folder of
    customer page images, whether it is abandoned while this process runs or the
    process exits with the read still on a daemon thread."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.pidfile = self.root / "tool.pid"
        # Every poppler/tesseract tool: records its pid and hangs, as a long OCR does.
        self.tool = self.root / "tool"
        self.tool.write_text(f"#!/bin/sh\necho $$ > '{self.pidfile}'\nexec sleep 60\n")
        self.tool.chmod(0o700)
        self.addCleanup(self._kill_left_tool)

    def _kill_left_tool(self) -> None:
        if self.pidfile.is_file() and self.pidfile.read_text().strip():
            try:
                os.kill(int(self.pidfile.read_text()), 9)
            except ProcessLookupError:
                pass

    def test_a_read_abandoned_at_its_budget_stops_its_child_and_removes_its_folder(self):
        from integrations import child_process, document_understanding as du
        from scripts import eval_oracle_dev as dev
        with unittest.mock.patch.object(du.shutil, "which", return_value=str(self.tool)), \
                unittest.mock.patch.object(tempfile, "tempdir", str(self.tmp)):
            self.assertIsNone(dev._within(2.0, du._portable_pdf_text, b"%PDF-1.4\n"))
            self.assertTrue(process_gone(wait_for_pid(self.pidfile, wait=0)))
            self.assertEqual(list(self.tmp.glob("plx-pdf-ocr-*")), [])
            # Only the abandoned thread is refused: this one still runs its children.
            self.assertEqual(child_process.run([sys.executable, "-c", "print(1)"],
                                               timeout=30).stdout.strip(), b"1")

    def test_the_watchdog_stops_ocr_before_its_hard_exit(self):
        """``os._exit`` runs no atexit handler: the eval watchdog abandons first."""
        import argparse
        from contextlib import redirect_stdout
        from integrations import child_process, document_understanding as du
        from scripts import eval_dev_baseline as baseline
        seen, exited = [], threading.Event()

        def read() -> None:
            try:
                du._portable_pdf_text(b"%PDF-1.4\n")
            except Exception:  # noqa: BLE001 - the abandoned read's failure is not asserted
                pass

        def hard_exit(code: int) -> None:
            seen.append((code, process_gone(pid, wait=0), list(self.tmp.glob("plx-pdf-ocr-*"))))
            exited.set()

        # The injected hard_exit returns: the module's closed state is restored after.
        with unittest.mock.patch.object(du.shutil, "which", return_value=str(self.tool)), \
                unittest.mock.patch.object(tempfile, "tempdir", str(self.tmp)), \
                unittest.mock.patch.object(child_process, "_closed", False), \
                redirect_stdout(io.StringIO()):
            threading.Thread(target=read, daemon=True).start()
            pid = wait_for_pid(self.pidfile)
            baseline.watchdog(baseline.Progress(clock=lambda: 1.0), argparse.Namespace(),
                              0.0, hard_exit, poll=0.01)
            self.assertTrue(exited.wait(30))
        self.assertEqual(seen, [(3, True, [])])

    def test_exit_with_a_read_still_running_leaves_no_child_or_folder(self):
        root = Path(__file__).resolve().parents[2]
        # The read's own failure (its child killed at exit) is swallowed, as capture_text
        # does: a traceback printed during interpreter shutdown can abort it.
        script = (
            "import sys, threading, time; sys.path.insert(0, {root!r})\n"
            "from integrations import document_understanding as du\n"
            "du.shutil.which = lambda tool: {tool!r}\n"
            "def read():\n"
            "    try:\n"
            "        du._portable_pdf_text(b'%PDF-1.4\\n')\n"
            "    except BaseException:\n"
            "        pass\n"
            "threading.Thread(target=read, daemon=True).start()\n"
            "from pathlib import Path; p = Path({pid!r})\n"
            "while not p.is_file() or not p.read_text().strip(): time.sleep(0.02)\n"
            ).format(root=str(root), tool=str(self.tool), pid=str(self.pidfile))
        done = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True,
                              timeout=60, env={"PATH": os.environ.get("PATH", ""),
                                               "TMPDIR": str(self.tmp)})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(process_gone(int(self.pidfile.read_text())))
        self.assertEqual(list(self.tmp.glob("plx-pdf-ocr-*")), [])


class OcrFailureCauseTest(unittest.TestCase):
    """QA-06: a failed OCR run says why, as a typed cause the capture keeps in its text
    status (``ocr_failed:<cause>``), and a scan turned upside down or on its side reads."""

    def test_each_ocr_fault_is_a_typed_cause_the_capture_keeps(self):
        from subprocess import CompletedProcess, TimeoutExpired
        from integrations import document_understanding as du
        from integrations.captured_text import capture_text

        def tools(*, pages=b"Pages:          1\n", size=b"612 x 792", fails=None,
                  timeout=None, render=None, tsv=None, code=1, signal=None):
            def run(command, **_):
                tool = Path(command[0]).name
                if tool == timeout:
                    raise TimeoutExpired(command, 1)
                if tool == signal:   # killed (a negative code)
                    return CompletedProcess(command, -9, b"", b"")
                if tool == fails:
                    return CompletedProcess(command, code, b"", b"Syntax Error: broken\n")
                if tool == "pdfinfo":
                    out = b"Page    1 size: " + size + b" pts\n" if "-f" in command else pages
                elif tool == "pdftotext":
                    out = b"\f"   # one page, no text layer: a scan
                elif tool == "pdftoppm":
                    dpi = int(command[command.index("-r") + 1])   # a letter page at dpi
                    width, height = render or (round(612 * dpi / 72), round(792 * dpi / 72))
                    Path(command[-1] + ".png").write_bytes(
                        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") +
                        height.to_bytes(4, "big"))
                    out = b""
                elif command[-1] == "0":   # orientation: too little text is no answer
                    return CompletedProcess(command, 1, b"", b"Too few characters\n")
                else:
                    out = "\t".join(du._TSV_HEADER).encode() + b"\n" if tsv is None else tsv
                return CompletedProcess(command, 0, out, b"")
            return run

        cases = (("deadline", tools(timeout="tesseract"), 1),
                 ("render_exit", tools(fails="pdftoppm"), 1),
                 ("recognize_exit", tools(fails="tesseract"), 1),
                 ("tool_signal", tools(signal="pdfinfo"), 1),
                 ("pdf_unreadable", tools(fails="pdfinfo"), 1),
                 ("pdfinfo_exit", tools(fails="pdfinfo", code=3), 1),

                 ("page_count", tools(pages=b"Pages:          0\n"), 1),
                 ("page_bound", tools(size=b"20500 x 600"), 1),
                 ("image_page_bound", tools(), 0),   # one scanned page over a bound of 0
                 ("render_mismatch", tools(render=(1, 1)), 1),
                 ("output_bound", tools(tsv=b"x" * 1_000_001), 1),
                 ("ocr_output_invalid", tools(tsv=b"level\tbroken\n"), 1))
        # ``text_layer_exit`` (a text layer alone, or one OCR cannot read around) and the
        # macOS helper's ``tool_exit`` are reached through other entry points:
        # test_document_understanding_ocr_exit; the byte-shape causes of a PDF poppler
        # refuses through test_pdf_unreadable_typed_by_bytes.
        self.assertEqual({cause for cause, _, _ in cases} | {
            "text_layer_exit", "tool_exit", "not_a_pdf", "pdf_truncated", "pdf_stub"},
                         set(du.OCR_FAILURE_CAUSES))
        record = {"name": "scan.pdf", "mime_type": "application/pdf"}
        with unittest.mock.patch.object(du.platform, "system", return_value="Linux"), \
                unittest.mock.patch.object(du.shutil, "which",
                                           side_effect=lambda tool: f"/usr/bin/{tool}"):
            # Read through, the same fakes give a scan with no text, not a failure.
            with unittest.mock.patch.object(du.child_process, "run", side_effect=tools()):
                self.assertEqual(capture_text(b"%PDF-1.4\n", record), ("ocr_no_text", None))
            for cause, run, image_pages in cases:
                with self.subTest(cause), \
                        unittest.mock.patch.object(du.child_process, "run", side_effect=run), \
                        unittest.mock.patch.object(du, "MAX_OCR_PAGES", image_pages):
                    with self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$") as raised:
                        du._portable_pdf_text(b"%PDF-1.4\n")
                    self.assertEqual(raised.exception.cause, cause)
                    self.assertEqual(capture_text(WHOLE_PDF, record),
                                     (f"ocr_failed:{cause}", None))
        with self.assertRaisesRegex(ValueError, "^ocr_failure_cause_unknown$"):
            du.OcrFailed("slow")   # no fault leaves untyped

    @unittest.skipUnless(all(shutil.which(tool) for tool in
                             ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")) and
                         _importable("pypdf"), "local poppler + tesseract OCR not installed")
    def test_a_scan_turned_upside_down_or_on_its_side_reads(self):
        from pypdf import PdfReader, PdfWriter
        fixture = Path(__file__).with_name("fixtures") / "scanned-order-fixture.pdf"
        for degrees in (90, 180, 270):
            writer = PdfWriter()
            page = PdfReader(fixture).pages[0]
            page.rotate(degrees)   # rendered turned: what a scan fed in turned looks like
            writer.add_page(page)
            turned = io.BytesIO()
            writer.write(turned)
            with self.subTest(degrees), \
                    unittest.mock.patch("integrations.document_understanding.platform.system",
                                        return_value="Linux"):
                document = extract_document(turned.getvalue(), "application/pdf",
                                            original_url=URL, local_pdf_ocr=True)
                self.assertEqual({(claim["field"], claim["value"]) for claim in
                                  document.claims if claim["kind"] == "identifier"},
                                 {("sales_order", "SO-OCR-123"), ("customer_po", "CPO-456")})
                self.assertEqual(document.method, "poppler-tesseract")
                # Read upright: its lines run across the page, not down it.
                self.assertIn("Sales order: SO-OCR-123", document.text)
        # An orientation answer is checked, never trusted: an upright page OSD calls
        # upside down reads as rendered, its turned reading being the less confident.
        from integrations import child_process
        real = child_process.run

        def wrong_osd(command, *args, **kwargs):
            if command[-2:] == ["--psm", "0"]:
                return subprocess.CompletedProcess(command, 0, b"Rotate: 180\n", b"")
            return real(command, *args, **kwargs)
        with unittest.mock.patch("integrations.document_understanding.platform.system",
                                 return_value="Linux"), \
                unittest.mock.patch.object(child_process, "run", side_effect=wrong_osd):
            document = extract_document(fixture.read_bytes(), "application/pdf",
                                        original_url=URL, local_pdf_ocr=True)
        self.assertIn("Sales order: SO-OCR-123", document.text)


class RaisedReadNotRememberedTest(unittest.TestCase):
    """A read that raised (a transient ``child_process.Abandoned``, ``OSError`` or
    timeout) is None to its caller and read again by the next one, never remembered as
    unreadable for the life of the process; a real reading without text is remembered
    (``captured_text`` and ``container_members``)."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.blobs = Path(temp.name)

    def store(self, content: bytes, mime: str, name: str) -> dict:
        digest = sha256(content).hexdigest()
        (self.blobs / digest[:2]).mkdir(exist_ok=True)
        (self.blobs / digest[:2] / digest).write_bytes(content)
        return {"content_sha256": digest, "content_type": mime, "name": name}

    def test_captured_text_reads_again_after_a_raise_and_remembers_no_text(self):
        from integrations import captured_text as ct, child_process
        tag = os.urandom(8).hex()   # bytes no other test (or earlier run) has read
        text = f"PO Number: {tag}\n"
        row = self.store(text.encode(), "text/plain", f"po-{tag}.txt")
        for failure in (child_process.Abandoned("child_process_abandoned"),
                        OSError("transient"), TimeoutError("timed out")):
            with unittest.mock.patch.object(ct, "read_text", side_effect=failure):
                self.assertIsNone(ct.captured_text(self.blobs, row))
        self.assertEqual(ct.captured_text(self.blobs, row).text, text)

        blank = self.store(f"   \n{' ' * 7}\n".encode(), "text/plain", f"blank-{tag}.txt")
        first = ct.captured_text(self.blobs, blank)
        self.assertIsNotNone(first)
        self.assertIsNone(first.text)
        with unittest.mock.patch.object(ct, "read_text",
                                        side_effect=AssertionError("read again")):
            self.assertIs(ct.captured_text(self.blobs, blank), first)

    def test_a_read_over_a_bound_is_remembered_once_and_a_defect_is_logged_not_kept(self):
        from integrations import captured_text as ct
        tag = os.urandom(8).hex()
        over = self.store(f"over {tag}".encode(), "text/plain", f"over-{tag}.txt")
        with unittest.mock.patch.object(
                ct, "read_text",
                side_effect=ValidationError("extracted document text exceeds bound")) as read:
            self.assertIsNone(ct.captured_text(self.blobs, over))
            self.assertIsNone(ct.captured_text(self.blobs, over))
        self.assertEqual(read.call_count, 1)   # the same bytes exceed it on every read
        # Each of the workbook extractor's bound errors is a limit too.
        import re
        source = Path(ct.__file__).with_name("xlsx_extract.py").read_text(encoding="utf-8")
        bounds = re.findall(r'ValidationError\("([^"]*exceeds? bound)"\)', source)
        self.assertGreaterEqual(len(bounds), 6)
        for message in bounds:
            self.assertEqual(ct.read_failure(ValidationError(message)), "limit", message)
        # An OCR run that failed (a timeout is wrapped so) or an unknown page count
        # (pdfinfo timed out) is the extractor's error too, and is read again.
        for message in ("local_pdf_ocr_failed", "PDF page count unknown"):
            row = self.store(f"{message} {tag}".encode(), "text/plain", f"t-{tag}.txt")
            with unittest.mock.patch.object(ct, "read_text",
                                            side_effect=ValidationError(message)) as read:
                ct.captured_text(self.blobs, row)
                ct.captured_text(self.blobs, row)
            self.assertEqual(read.call_count, 2, message)

        text = f"Customer PO {tag}\n"
        row = self.store(text.encode(), "text/plain", f"secret-name-{tag}.txt")
        before = ct._CACHE.unexpected
        with unittest.mock.patch.object(ct, "read_text", side_effect=KeyError(text)), \
                self.assertLogs("integrations.captured_text", "WARNING") as logged:
            self.assertIsNone(ct.captured_text(self.blobs, row))
        self.assertEqual(ct._CACHE.unexpected, before + 1)
        self.assertIn("KeyError", logged.output[0])
        self.assertNotIn(tag, "".join(logged.output))   # neither its text nor its name
        self.assertEqual(ct.captured_text(self.blobs, row).text, text)

    def test_a_container_whose_member_read_failed_transiently_is_read_again(self):
        import zipfile
        from integrations import captured_text as ct, child_process
        tag = os.urandom(8).hex()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.txt", f"PO Number: {tag}\n")
            archive.writestr("b.txt", f"Packing slip {tag}\n")
        row = self.store(buffer.getvalue(), "application/zip", f"pos-{tag}.zip")
        real, calls = ct.read_text, []

        def first_member_abandoned(content, *args, **kwargs):
            calls.append(content)
            if len(calls) == 1:
                raise child_process.Abandoned("child_process_abandoned")
            return real(content, *args, **kwargs)
        with unittest.mock.patch.object(ct, "read_text", side_effect=first_member_abandoned):
            first = ct.container_members(self.blobs, row)
        self.assertEqual([m.text_status for m in first.members], ["failed", "extracted"])
        again = ct.container_members(self.blobs, row)
        self.assertEqual([m.reading.text for m in again.members],
                         [f"PO Number: {tag}\n", f"Packing slip {tag}\n"])

        # A member over a bound fails the same way every read: the container is kept.
        bound = io.BytesIO()
        with zipfile.ZipFile(bound, "w") as archive:
            archive.writestr("big.txt", f"big {tag}")
        big = self.store(bound.getvalue(), "application/zip", f"big-{tag}.zip")
        with unittest.mock.patch.object(
                ct, "read_text", side_effect=ValidationError("extracted document text exceeds bound")):
            kept = ct.container_members(self.blobs, big)
        self.assertEqual([m.text_status for m in kept.members], ["failed"])
        with unittest.mock.patch.object(ct, "read_container",
                                        side_effect=AssertionError("read again")) as read:
            self.assertEqual(ct.container_members(self.blobs, big), kept)
            read.assert_not_called()

    def test_a_read_in_flight_when_the_memo_is_replaced_never_writes_into_the_new_one(self):
        # A thread still reading (an earlier test's, abandoned at its budget) when a
        # check run swaps in a fresh memo remembers into the memo it began with.
        from integrations import captured_text as ct
        tag = os.urandom(8).hex()
        row = self.store(f"PO Number: {tag}\n".encode(), "text/plain", f"po-{tag}.txt")
        real, started, release, got = ct.read_text, threading.Event(), threading.Event(), []

        def slow(*args, **kwargs):
            started.set()
            release.wait(10)
            return real(*args, **kwargs)
        old = ct._CACHE
        with unittest.mock.patch.object(ct, "read_text", side_effect=slow):
            reader = threading.Thread(target=lambda: got.append(ct.captured_text(self.blobs, row)))
            reader.start()
            self.assertTrue(started.wait(10))
            with unittest.mock.patch.object(ct, "_CACHE", old.empty()) as fresh:
                release.set()
                reader.join(10)
                self.assertFalse(reader.is_alive())
                self.assertEqual(len(fresh), 0)
        self.assertEqual(got[0].text, f"PO Number: {tag}\n")
        self.assertIn((row["content_sha256"], "text/plain", row["name"], None), old)

    def test_container_members_reads_again_after_a_raise_and_remembers_a_result(self):
        import zipfile
        from integrations import captured_text as ct, child_process
        tag = os.urandom(8).hex()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("po.txt", f"PO Number: {tag}\n")
        row = self.store(buffer.getvalue(), "application/zip", f"pos-{tag}.zip")
        for failure in (child_process.Abandoned("child_process_abandoned"),
                        OSError("transient")):
            with unittest.mock.patch.object(ct, "read_container", side_effect=failure):
                self.assertIsNone(ct.container_members(self.blobs, row))
        found = ct.container_members(self.blobs, row)
        self.assertEqual([m.reading.text for m in found.members], [f"PO Number: {tag}\n"])

        empty = io.BytesIO()
        with zipfile.ZipFile(empty, "w") as archive:
            archive.comment = tag.encode()
        none = self.store(empty.getvalue(), "application/zip", f"empty-{tag}.zip")
        first = ct.container_members(self.blobs, none)
        with unittest.mock.patch.object(ct, "read_container",
                                        side_effect=AssertionError("read again")) as again:
            self.assertEqual(ct.container_members(self.blobs, none), first)
            again.assert_not_called()


if __name__ == "__main__":
    unittest.main()
