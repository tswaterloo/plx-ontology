"""An OCR tool exit is a typed per-file result, never a run failure, and a re-extraction
is a new immutable version that keeps the old citation (owner directive 4b2734959760,
core gap C: 11 of 37 customer-PO SharePoint files read ``ocr_failed:tool_exit``).

Injected-transport: the poppler/tesseract processes are faked at ``child_process.run``
(evidence label ``injected-transport``; no network, no credentials, no real document).
Whether the host's poppler really exits on the live files is measured by the
``customer_po`` live check, not here.
"""

from __future__ import annotations

import sqlite3
import unittest
import unittest.mock
from hashlib import sha256
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

from integrations import document_understanding as du
from integrations import graph_read
from integrations.captured_text import capture_text, ocr_failure_cause, text_status_kind
from integrations.tests import test_capture_mail as base
from integrations.tests.test_capture_mail import (ATTACHMENT_BYTES, BOX_A, READER_A, SCOPE,
                                                  file_attachment, reader_audience)
from ontology.core import ValidationError
from ontology.types import Actor

RECORD = {"name": "po.pdf", "mime_type": "application/pdf"}
# Whole and with a content stream, so a tool refusal is pdf_unreadable, not a pdf_stub.
WHOLE_PDF = b"%PDF-1.4\n1 0 obj\n<< /Length 0 >>\nstream\nendstream\nendobj\n%%EOF\n"
WORD = ("5\t1\t1\t1\t1\t1\t10\t10\t80\t20\t96\tPO-4711\n")


def tsv(*rows: str) -> bytes:
    return ("\t".join(du._TSV_HEADER) + "\n" + "".join(rows)).encode()


class Poppler:
    """Faked pdfinfo/pdftotext/pdftoppm/tesseract of one scanned page. ``behave`` maps a
    tool (``pdfinfo-open``, ``pdfinfo-list``, ``pdftotext``, ``pdftoppm``, ``osd``,
    ``tesseract``) to ``(code, stderr)`` or a function of the call number returning it
    (None: the tool works). ``seen`` keeps every call as ``(role, command)``."""

    def __init__(self, behave=None, *, layer: bytes = b"\f", words: bytes | None = None):
        self.behave, self.layer = dict(behave or {}), layer
        self.words = tsv(WORD) if words is None else words
        self.seen: list[tuple[str, list[str]]] = []

    def role(self, command) -> str:
        tool = Path(command[0]).name
        if tool == "pdfinfo":
            return "pdfinfo-list" if "-f" in command else "pdfinfo-open"
        if tool == "tesseract":
            return "osd" if command[-1] == "0" else "tesseract"
        return tool

    def __call__(self, command, **_):
        role = self.role(command)
        self.seen.append((role, list(command)))
        calls = sum(1 for seen, _ in self.seen if seen == role)
        rule = self.behave.get(role)
        rule = rule(calls) if callable(rule) else rule
        if isinstance(rule, BaseException):
            raise rule
        if rule is not None:
            return CompletedProcess(command, rule[0], b"", rule[1])
        out = b""
        if role == "pdfinfo-open":
            out = b"Pages:          1\n"
        elif role == "pdfinfo-list":
            out = b"Page    1 size: 612 x 792 pts\n"
        elif role == "pdftotext":
            out = self.layer
        elif role == "pdftoppm":
            dpi = int(command[command.index("-r") + 1])
            width, height = round(612 * dpi / 72), round(792 * dpi / 72)
            Path(command[-1] + ".png").write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") +
                height.to_bytes(4, "big"))
        elif role == "osd":
            return CompletedProcess(command, 1, b"", b"Too few characters\n")
        elif role == "tesseract":
            out = self.words
        return CompletedProcess(command, 0, out, b"")

    def count(self, role: str) -> int:
        return sum(1 for seen, _ in self.seen if seen == role)


def hosted(poppler):
    """A Linux host with the four tools, running ``poppler`` for every call."""
    stack = unittest.mock.patch.object(du.platform, "system", return_value="Linux")
    which = unittest.mock.patch.object(du.shutil, "which",
                                       side_effect=lambda tool: f"/usr/bin/{tool}")
    run = unittest.mock.patch.object(du.child_process, "run", side_effect=poppler)
    return _All(stack, which, run)


class _All:
    def __init__(self, *managers) -> None:
        self.managers = managers

    def __enter__(self):
        for manager in self.managers:
            manager.__enter__()
        return self

    def __exit__(self, *exc):
        for manager in reversed(self.managers):
            manager.__exit__(*exc)


class ToolExitIsATypedResultTests(unittest.TestCase):
    def status(self, poppler) -> str:
        """What the capture keeps for one scanned file: a status, never an exception."""
        with hosted(poppler):
            status, text = capture_text(WHOLE_PDF, RECORD)
        self.assertIsNone(text)
        return status

    def test_each_tool_exit_is_its_own_cause_in_the_files_text_status(self):
        cases = (
            # A damaged or truncated file: pdfinfo cannot open it (exit 1, no password).
            ("pdf_unreadable", {"pdfinfo-open": (1, b"Syntax Error: Couldn't find trailer\n")}),
            # A stub PDF with no page (live 2026-10-21: 5 Customer Documents files of
            # 125-156 bytes, the size Graph states): pdfinfo opens it and exits 99.
            ("pdf_unreadable", {"pdfinfo-open": (99, b"Syntax Error: Invalid page count 0\n")}),
            ("pdfinfo_exit", {"pdfinfo-list": (99, b"Syntax Error: broken\n")}),
            ("pdfinfo_exit", {"pdfinfo-open": (3, b"Permission Error\n")}),
            ("pdfinfo_exit", {"pdfinfo-list": (1, b"Syntax Error: broken\n")}),
            ("render_exit", {"pdftoppm": (1, b"Syntax Error: broken page\n")}),
            ("recognize_exit", {"tesseract": (1, b"Error in pixReadStream\n")}),
            ("tool_signal", {"pdftoppm": (-9, b"")}),            # killed (out of memory)
            ("tool_signal", {"tesseract": (-11, b"")}),
            ("tool_signal", {"pdfinfo-open": (-9, b"")}),
            ("deadline", {"tesseract": TimeoutExpired(["tesseract"], 1)}),
        )
        for cause, behave in cases:
            with self.subTest(cause=cause, behave=list(behave)):
                status = self.status(Poppler(behave))
                self.assertEqual(status, f"ocr_failed:{cause}")
                self.assertEqual(ocr_failure_cause(status), cause)
                self.assertEqual(text_status_kind(status), "ocr_failed")

    def test_a_password_pdf_is_still_unsupported_not_a_failure(self):
        status = self.status(Poppler({"pdfinfo-open": (1, b"Command Line Error: Incorrect password\n")}))
        self.assertEqual(status, "unsupported")

    def test_a_tool_exit_raises_the_typed_error_never_a_bare_one(self):
        with hosted(Poppler({"pdftoppm": (1, b"x")})), \
                self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$") as raised:
            du._portable_pdf_text(b"%PDF-1.4\n")
        self.assertEqual(raised.exception.cause, "render_exit")

    def test_every_new_cause_is_listed_and_an_unknown_one_still_fails_closed(self):
        for cause in ("pdf_unreadable", "pdfinfo_exit", "text_layer_exit", "render_exit",
                      "recognize_exit", "tool_signal"):
            self.assertIn(cause, du.OCR_FAILURE_CAUSES)
            self.assertEqual(du.OcrFailed(cause).cause, cause)
        for bad in ("slow", "", None, "tool_exit_3"):
            with self.assertRaisesRegex(ValueError, "^ocr_failure_cause_unknown$"):
                du.OcrFailed(bad)
        # A stored status with a cause nobody typed is counted as unknown, not dropped.
        self.assertEqual(ocr_failure_cause("ocr_failed:gremlin"), "unknown")

    def test_a_failure_of_one_file_among_many_leaves_the_others_read(self):
        # N=1/10/60 scanned files, every fifth one broken: every file gets a status and
        # the broken ones are typed, the rest read; none lost or doubled.
        for n in (1, 10, 60):
            with self.subTest(n=n):
                statuses = []
                for index in range(n):
                    broken = index % 5 == 4
                    poppler = Poppler({"pdftoppm": (1, b"x")} if broken else {})
                    statuses.append((broken, self.status_or_text(poppler)))
                self.assertEqual(len(statuses), n)
                for broken, status in statuses:
                    self.assertEqual(status, "ocr_failed:render_exit" if broken
                                     else "extracted_ocr")

    def status_or_text(self, poppler) -> str:
        with hosted(poppler):
            return capture_text(WHOLE_PDF, RECORD)[0]


class FallbackTests(unittest.TestCase):
    def test_a_failing_text_layer_falls_through_to_ocr_and_stays_review_grade(self):
        # pdftotext exit 3 is poppler's "copying text is not allowed" (and any other text
        # layer fault): the page is rendered and read like a scan, and its range is an
        # OCR range, so identifiers from it stay review leads.
        poppler = Poppler({"pdftotext": (3, b"Copying of text from this document is not allowed.\n")})
        with hosted(poppler):
            text, pages, status, warnings, ocr_pages, count = du._portable_pdf_text(b"%PDF-1.4\n")
        self.assertIn("PO-4711", text)
        self.assertEqual((status, ocr_pages, count), ("extracted", frozenset({1}), 1))
        self.assertIn("ocr_text_requires_identity_review", warnings)
        self.assertGreaterEqual(poppler.count("pdftoppm"), 1)
        with hosted(Poppler({"pdftotext": (3, b"x")})):
            self.assertEqual(capture_text(WHOLE_PDF, RECORD)[0], "extracted_ocr")

    def test_the_text_layer_pass_alone_cannot_fall_through_and_says_so(self):
        # Without OCR there is nothing to fall through to: a typed failure.
        with hosted(Poppler({"pdftotext": (3, b"x")})), \
                self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$") as raised:
            du._portable_pdf_text(b"%PDF-1.4\n", ocr=False)
        self.assertEqual(raised.exception.cause, "text_layer_exit")

    def test_a_text_layer_that_dies_on_a_signal_is_not_papered_over(self):
        with hosted(Poppler({"pdftotext": (-9, b"")})), \
                self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$") as raised:
            du._portable_pdf_text(b"%PDF-1.4\n")
        self.assertEqual(raised.exception.cause, "tool_signal")

    def test_a_page_that_fails_at_full_resolution_is_read_once_at_a_lower_one(self):
        for role in ("pdftoppm", "tesseract"):
            with self.subTest(role=role):
                poppler = Poppler({role: lambda call: (1, b"oom") if call == 1 else None})
                with hosted(poppler):
                    text, _, _, _, ocr_pages, _ = du._portable_pdf_text(b"%PDF-1.4\n")
                self.assertIn("PO-4711", text)
                self.assertEqual(ocr_pages, frozenset({1}))
                dpis = [int(c[c.index("-r") + 1]) for r, c in poppler.seen if r == "pdftoppm"]
                self.assertEqual(dpis[0], 300)
                self.assertLess(dpis[-1], dpis[0])

    def test_a_page_that_fails_at_every_resolution_is_one_typed_failure(self):
        poppler = Poppler({"pdftoppm": (1, b"oom")})
        with hosted(poppler), self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$"):
            du._portable_pdf_text(b"%PDF-1.4\n")
        self.assertEqual(poppler.count("pdftoppm"), 2)   # once more, not forever

    def test_a_deadline_is_never_retried(self):
        poppler = Poppler({"tesseract": TimeoutExpired(["tesseract"], 1)})
        with hosted(poppler), self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$"):
            du._portable_pdf_text(b"%PDF-1.4\n")
        self.assertEqual(poppler.count("pdftoppm"), 1)

    def test_orientation_failure_still_reads_the_page_as_rendered(self):
        poppler = Poppler({"osd": (1, b"Too few characters\n")})
        with hosted(poppler):
            self.assertEqual(capture_text(WHOLE_PDF, RECORD)[0], "extracted_ocr")


class MacosHelperExitTests(unittest.TestCase):
    """The Swift helper exits 2 with a code on stderr; each code is its own cause."""

    def read(self, code: int, stderr: bytes):
        def run(command, **_):
            return CompletedProcess(command, code, b"", stderr)

        with unittest.mock.patch.object(du.platform, "system", return_value="Darwin"), \
                unittest.mock.patch.object(du.Path, "is_file", return_value=True), \
                unittest.mock.patch.object(du.child_process, "run", side_effect=run):
            return du._macos_pdf_text(b"%PDF-1.4\n")

    def test_helper_codes_map_to_typed_causes(self):
        cases = ((b"ocr_page_limit_exceeded\n", 2, "image_page_bound"),
                 (b"pdf_page_dimensions_invalid\n", 2, "page_bound"),
                 (b"pdf_page_render_failed\n", 2, "render_exit"),
                 (b"vision_ocr_failed\n", 2, "recognize_exit"),
                 (b"pdf_unavailable_or_encrypted\n", 2, "pdf_unreadable"),
                 (b"pdf_page_count_out_of_bounds\n", 2, "page_count"),
                 (b"something new\n", 2, "tool_exit"),      # an unknown code stays typed
                 (b"", -9, "tool_signal"))
        for stderr, code, cause in cases:
            with self.subTest(stderr=stderr, code=code), \
                    self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$") as raised:
                self.read(code, stderr)
            self.assertEqual(raised.exception.cause, cause)

    def test_a_text_over_the_bound_is_the_bound_error_not_an_ocr_failure(self):
        with self.assertRaisesRegex(ValidationError, "^extracted PDF text exceeds bound$"):
            self.read(2, b"pdf_text_limit_exceeded\n")


class MailReextractionIsANewVersionTests(base.CaptureCase):
    """The mail store reads attachment text again from its stored bytes (OCR now
    available): the new reading is a new immutable version, the old one stays
    resolvable, and OCR text is review-grade (``extracted_ocr``) until a person
    confirms it."""

    def setUp(self):
        super().setUp()
        self.graph.put(BOX_A, "MSG-1", "CK-1-1", attachments=[file_attachment("ATT-1")])
        self.audience = reader_audience()
        self.handle = (graph_read.mail_citation(BOX_A, "MSG-1", "CK-1-1") +
                       f"#attachment=ATT-1&sha256={sha256(ATTACHMENT_BYTES).hexdigest()}")

    def run_with(self, results):
        """One capture run whose attachment reading is ``results`` (status, text)."""
        from integrations import capture_mail
        capture = getattr(self, "_capture", None) or self.capture()
        self._capture = capture
        with unittest.mock.patch.object(capture_mail, "capture_text", return_value=results), \
                unittest.mock.patch.object(du, "local_ocr_available", return_value=True):
            return capture.run(SCOPE)

    def versions(self):
        return self.store.text_chain(self.handle)

    def test_a_reading_after_ocr_arrives_is_version_two_and_version_one_still_resolves(self):
        self.run_with(("ocr_unavailable", None))
        self.assertEqual([(v["version"], v["text_status"]) for v in self.versions()],
                         [(1, "ocr_unavailable")])
        run = self.run_with(("extracted_ocr", "PO-4711 total 1,234.50"))
        self.assertEqual(self.folder(run)["counts"]["texts_reextracted"], 1)
        versions = self.versions()
        self.assertEqual([(v["version"], v["text_status"]) for v in versions],
                         [(1, "ocr_unavailable"), (2, "extracted_ocr")])
        self.assertIsNone(versions[0]["text_sha256"])
        # The current citation reads the new text, review-grade.
        now = self.store.resolve(self.handle, READER_A, self.audience, text=True)
        self.assertEqual(now["row"]["content_text"], "PO-4711 total 1,234.50")
        self.assertEqual((now["row"]["text_version"], now["row"]["text_status"]),
                         (2, "extracted_ocr"))
        # The old citation still resolves, to the old reading (no text), not the new one.
        old = self.store.resolve(self.handle, READER_A, self.audience, text=True,
                                 text_version=1)
        self.assertIsNotNone(old)
        self.assertEqual((old["row"]["text_version"], old["row"]["text_status"],
                          old["row"]["content_text"]), (1, "ocr_unavailable", None))

    def test_a_later_reading_never_overwrites_an_earlier_one(self):
        self.run_with(("ocr_unavailable", None))
        first = self.store.put_text("first reading")
        second = self.store.put_text("second reading")
        self.store.fill_text(self.handle, "extracted_ocr", first)
        self.store.fill_text(self.handle, "extracted_ocr", second)
        self.assertEqual([v["version"] for v in self.versions()], [1, 2, 3])
        for number, text in ((2, "first reading"), (3, "second reading")):
            view = self.store.resolve(self.handle, READER_A, self.audience, text=True,
                                      text_version=number)
            self.assertEqual(view["row"]["content_text"], text)
        # The current pointer is the newest, and the store itself refuses a rewrite.
        self.assertEqual(self.store.resolve(self.handle, READER_A, self.audience,
                                            text=True)["row"]["content_text"],
                         "second reading")
        for sql in ("UPDATE attachment_text_versions SET text_status='x'",
                    "DELETE FROM attachment_text_versions"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self.store.db.execute(sql)

    def test_the_same_reading_twice_is_not_a_new_version(self):
        self.run_with(("ocr_unavailable", None))
        digest = self.store.put_text("same")
        self.store.fill_text(self.handle, "extracted_ocr", digest)
        self.store.fill_text(self.handle, "extracted_ocr", digest)
        self.assertEqual([v["version"] for v in self.versions()], [1, 2])

    def test_an_earlier_version_of_a_page_citation_reads_that_pages_old_text(self):
        from integrations.captured_text import PAGE_BREAK

        self.run_with(("extracted_ocr", f"page one{PAGE_BREAK}page two"))
        self.store.fill_text(self.handle, "extracted_ocr",
                             self.store.put_text(f"PAGE ONE{PAGE_BREAK}PAGE TWO"))
        for version, first in ((None, "PAGE ONE"), (2, "PAGE ONE"), (1, "page one")):
            with self.subTest(version=version):
                view = self.store.resolve(f"{self.handle}&page=1", READER_A, self.audience,
                                          text=True, text_version=version)
                self.assertEqual(view["row"]["content_text"], first)
        # A page the reading does not have is None, not another page or another version.
        self.assertIsNone(self.store.resolve(f"{self.handle}&page=3", READER_A,
                                             self.audience, text=True, text_version=1))

    def test_a_version_on_a_handle_that_is_not_an_attachments_resolves_to_nothing(self):
        self.run_with(("extracted_ocr", "x"))
        message = graph_read.mail_citation(BOX_A, "MSG-1", "CK-1-1")
        self.assertIsNotNone(self.store.resolve(message, READER_A, self.audience, text=True))
        self.assertIsNone(self.store.resolve(message, READER_A, self.audience, text=True,
                                             text_version=1))
        # And a version asked without text (nothing to version) is refused, not ignored.
        self.assertIsNone(self.store.resolve(self.handle, READER_A, self.audience,
                                             text_version=1))

    def test_an_unknown_or_forged_version_resolves_to_nothing(self):
        self.run_with(("ocr_unavailable", None))
        for number in (0, 2, -1, "1", True, 1.0, [1]):
            with self.subTest(number=number):
                self.assertIsNone(self.store.resolve(self.handle, READER_A, self.audience,
                                                     text=True, text_version=number))

    def test_a_reader_who_may_not_see_the_mailbox_sees_no_version(self):
        self.run_with(("ocr_unavailable", None))
        self.run_with(("extracted_ocr", "secret terms"))
        blind = Actor("plx", frozenset({"staff:finance"}), "restricted")
        for number in (None, 1, 2):
            with self.subTest(number=number):
                self.assertIsNone(self.store.resolve(self.handle, blind, self.audience,
                                                     text=True, text_version=number))
        self.assertEqual(self.store.text_versions(self.handle, blind, self.audience), [])
        self.assertEqual(len(self.store.text_versions(self.handle, READER_A, self.audience)), 2)

    def test_a_file_whose_ocr_failed_with_the_old_untyped_cause_is_read_again_once(self):
        self.run_with(("ocr_unavailable", None))
        # The store as an earlier release left it: the failure untyped, never re-read.
        self.store.db.execute("UPDATE attachments SET text_status='ocr_failed:tool_exit'")
        self.store.db.execute("DELETE FROM message_events WHERE event='text_reextracted'")
        self.store.db.commit()
        run = self.run_with(("ocr_failed:render_exit", None))      # now typed by tool
        self.assertEqual(self.folder(run)["counts"]["texts_reextracted"], 1)
        self.assertEqual(self.versions()[-1]["text_status"], "ocr_failed:render_exit")
        self.assertEqual(self.versions()[0]["text_status"], "ocr_failed:tool_exit")
        run = self.run_with(("ocr_failed:render_exit", None))      # not retried forever
        self.assertEqual(self.folder(run)["counts"]["texts_reextracted"], 0)
        self.assertEqual(len(self.versions()), 2)
        # A typed failure stays: it is not among the statuses a later round re-reads.
        self.assertEqual(self.store.text_unread(BOX_A, "inbox", 10), [])

    def test_many_attachments_each_get_their_own_chain(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                handles = []
                with base.tempfile.TemporaryDirectory() as tmp:
                    store = base.MailCaptureStore(Path(tmp) / "s", clock=self.clock)
                    self.addCleanup(store.close)
                    for index in range(n):
                        handle = f"h{index}"
                        store.db.execute(
                            "INSERT INTO attachments (message_handle, attachment_id, l1_handle,"
                            " mailbox_id, metadata, content_sha256, content_status, "
                            "recorded_at, text_status) VALUES (?,?,?,?,?,?,?,?,?)",
                            (f"m{index}", f"a{index}", handle, BOX_A, "{}", "0" * 64,
                             "captured", "t", "ocr_unavailable"))
                        store.db.commit()
                        store.fill_text(handle, "extracted_ocr", store.put_text(f"t{index}"))
                        handles.append(handle)
                    counts = [len(store.text_chain(h)) for h in handles]
                self.assertEqual(counts, [2] * n)


if __name__ == "__main__":
    unittest.main()
