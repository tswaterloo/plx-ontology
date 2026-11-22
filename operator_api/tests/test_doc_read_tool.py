"""The Ask's whole-document read (``plx.read_document``) and the tables a made file holds
(``plx.make_document`` ``tables``), through the real backend mixins on the mock backend.

Read: the person's own file and a SharePoint file their host opens are read to the end
(pages, find, a sheet's rows counted and totalled); another reader's file, a SharePoint
file the host does not open for them, an unknown argument and no exclusion list answer
nothing of the text; Freightcom or an excluded person named far past the first page
withholds every kind of read, and so does a person excluded after the reading was kept.

Tables: every row of the statement as the person reads it, past plx.sql's page (N=1/10/60
and 450 rows, pages of 37: none lost or doubled, the statement's own order kept), another
reader's rows never; a count that moves while read, a table past a kind's bound and a
deck's long table are refused, never a short file. SYNTHETIC people and rows; no network.
Evidence label: isolated-fixture."""
from __future__ import annotations

import io
from pathlib import Path
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from integrations.people_exclusion import PeopleExclusion
from operating import doc_read
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api.auth import Reader
from operator_api.mock import MockBackend

OWNER = ("ana@plx.example", "33333333-3333-4333-8333-333333333333")
STAFF = ("sam@plx.example", "55555555-5555-4555-8555-555555555555")
HIDDEN = ("hidden@plx.example", "99999999-9999-4999-8999-999999999999")
SP = "sharepoint://b%21drive/01ITEM@%22%7BE1%7D%2C1%22"


def reader_of(who) -> Reader:
    return Reader(session=fixture_session("plx", [f"staff:{who[0].split('@')[0]}"],
                                          channel=CHANNEL_CONSOLE, subject_email=who[0],
                                          subject_oid=who[1]),
                  mode="mock", display_name=who[0], email=who[0])


def csv_bytes(n: int, tail: str = "") -> bytes:
    lines = ["Item,Price,Note"] + [f"IT-{i:05d},{i}.25,ok" for i in range(n)]
    return ("\n".join(lines) + ("\n" + tail if tail else "") + "\n").encode()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = self.b = MockBackend()
        self.guard = PeopleExclusion([HIDDEN[1]])
        self.guard.attach_directory([(OWNER[1], OWNER[0], []), (STAFF[1], STAFF[0], []),
                                     (HIDDEN[1], HIDDEN[0], [])])
        b.people_guard = lambda: self.guard
        b.instruct_uploads = None
        b.state_dir = Path(tmp.name)
        self.ana, self.sam = reader_of(OWNER), reader_of(STAFF)

    def tools(self, reader):
        return {t.spec["name"]: t for t in self.b.assistant_tools(reader)}

    def own_file(self, reader, data: bytes, name="list.csv") -> str:
        row = self.b._uploads().prepare(reader.key, name=name, kind="csv", data=data,
                                        target=None, people=[])
        return f"file:{row['file_id']}"


class ReadDocument(Base):
    def test_own_and_opened_files_are_read_to_the_end(self):
        read = self.tools(self.ana)["plx.read_document"]
        cite = self.own_file(self.ana, csv_bytes(4000))
        first = read.call({"citation": cite})
        self.assertNotIn("error", first)
        text, offset = first["content"]["untrusted_text"], first["next_offset"]
        while offset is not None:
            page = read.call({"citation": cite, "offset": offset})
            text += page["content"]["untrusted_text"]
            offset = page["next_offset"]
        rows = re.findall(r"^r\d+ \| (IT-\d{5})", text, re.M)
        self.assertEqual(rows, [f"IT-{i:05d}" for i in range(4000)])
        found = read.call({"citation": cite, "find": "IT-03999"})
        self.assertEqual(len(found["matches"]), 1)
        counted = read.call({"citation": cite, "where": [{"column": "Price", "op": ">=",
                                                          "value": 3000}], "sum": ["Price"]})
        self.assertEqual(counted["matched"], 1000)
        self.assertEqual(counted["sums"]["Price"], sum(i + 0.25 for i in range(3000, 4000)))
        # a SharePoint file: only as the reader's host opens it
        opened = {self.ana.key}
        self.b.prepared_doc_read = lambda r, c: ("Plan.csv", csv_bytes(30), '"e,1"') \
            if r.key in opened and c == SP else None
        self.assertEqual(read.call({"citation": SP, "find": "IT-00029"})["matches"][0]["in"],
                         "rows")
        theirs = self.tools(self.sam)["plx.read_document"].call({"citation": SP})
        self.assertEqual(theirs["error"]["code"], "not_readable")
        self.assertNotIn("content", theirs)

    def test_a_dropped_original_is_read_from_its_captured_text_for_those_the_host_opens(self):
        """11-22 (HARD20 h07): 29,473 of 70,954 SharePoint PDFs have no original any more but
        the text the capture extracted; plx.read_document answered not_readable for all of
        them. It now reads that text through the same host gate and says so; a reader the
        host does not open it for still gets nothing; text withheld by the egress check
        (Freightcom) is still withheld."""
        read = self.tools(self.ana)["plx.read_document"]
        coa = "Certificate of Analysis\nLot Number:\n26233A\nBulk Lot Number: BC-26181D\n"
        opened = {self.ana.key}
        self.b.prepared_doc_read = lambda r, c: None               # the original is gone
        self.b.prepared_doc_text = lambda r, c: ("COA K18.pdf", coa, "v1") \
            if r.key in opened and c == SP else None
        got = read.call({"citation": SP})
        self.assertNotIn("error", got, got)
        self.assertEqual(got["kind"], "captured_text")
        self.assertEqual(got["name"], "COA K18.pdf")
        self.assertIn("26233A", got["content"]["untrusted_text"])
        self.assertIn("not held here", got["captured_text_note"])
        found = read.call({"citation": SP, "find": "BC-26181D"})
        self.assertEqual(len(found["matches"]), 1)
        theirs = self.tools(self.sam)["plx.read_document"].call({"citation": SP})
        self.assertEqual(theirs["error"]["code"], "not_readable")
        self.assertNotIn("content", theirs)
        # no text kept either: the plain refusal, never an empty read
        self.b.prepared_doc_text = lambda r, c: None
        self.assertEqual(read.call({"citation": SP})["error"]["code"], "not_readable")
        # the same egress check as any read
        self.b.prepared_doc_text = lambda r, c: ("COA.pdf", coa + "Freightcom freight bill", "v1")
        self.assertEqual(read.call({"citation": SP})["error"]["code"], "egress_withheld")

    def test_a_mail_attachment_is_read_whole_or_from_its_captured_text(self):
        """A byte-pinned mail-attachment citation reads like a file the host opens: its
        original when held, else the text extracted from it; nothing for a reader the host
        does not open it for, nor for a citation without its pin."""
        read = self.tools(self.ana)["plx.read_document"]
        att = "graph-mail://mbx/msg@ck#attachment=AAA&sha256=" + "a" * 64
        opened = {self.ana.key}
        self.b.prepared_mail_attachment = lambda r, c: ("Plan.csv", csv_bytes(30), None) \
            if r.key in opened and c == att else None
        got = read.call({"citation": att, "find": "IT-00029"})
        self.assertEqual(got["matches"][0]["in"], "rows")
        self.b.prepared_mail_attachment = lambda r, c: ("Spec.pdf", None, "Lot 26233A passes") \
            if r.key in opened and c == att else None
        got = read.call({"citation": att})
        self.assertEqual(got["kind"], "captured_text")
        self.assertEqual(got["name"], "Spec.pdf")
        self.assertIn("26233A", got["content"]["untrusted_text"])
        theirs = self.tools(self.sam)["plx.read_document"].call({"citation": att})
        self.assertEqual(theirs["error"]["code"], "not_readable")
        self.assertEqual(read.call({"citation": "graph-mail://mbx/msg@ck"})["error"]["code"],
                         "unsupported")

    def test_nobody_reads_what_they_may_not(self):
        read = self.tools(self.ana)["plx.read_document"]
        mine = self.own_file(self.ana, csv_bytes(50))
        for who, citation, code in ((self.sam, mine, "not_found"),
                                    (self.ana, "file:f_" + "0" * 24, "not_found"),
                                    (self.ana, "original://x/1", "unsupported")):
            with self.subTest(citation=citation):
                got = self.tools(who)["plx.read_document"].call({"citation": citation})
                self.assertEqual(got["error"]["code"], code)
                self.assertNotIn("content", got)
        for bad in ({"citation": mine, "max_chars": 5}, {"citation": mine, "offset": 0,
                                                         "find": "x"},
                    {"citation": mine, "offset": -1}, {"citation": mine, "sum": "Price"}):
            with self.subTest(bad=bad):
                self.assertEqual(read.call(bad)["error"]["code"], "invalid_input")
        # Freightcom or an excluded person named far past the first page: nothing at all
        for tail in ("IT-9,1,Freightcom pickup", f"IT-9,1,ask {HIDDEN[0]}"):
            cite = self.own_file(self.ana, csv_bytes(6000, tail))
            for args in ({}, {"offset": 0}, {"find": "IT-00001"}, {"where": []}):
                with self.subTest(tail=tail[:12], args=args):
                    got = read.call({"citation": cite, **args})
                    self.assertEqual(got["error"]["code"], "egress_withheld")
                    self.assertNotIn("IT-00001", str(got))
        # a person excluded after a reading was kept: the next call answers nothing
        named = self.own_file(self.ana, csv_bytes(10, f"IT-9,1,ask {STAFF[0]}"))
        self.assertNotIn("error", read.call({"citation": named}))
        later = PeopleExclusion([STAFF[1], HIDDEN[1]])
        later.attach_directory([(OWNER[1], OWNER[0], []), (STAFF[1], STAFF[0], []),
                                (HIDDEN[1], HIDDEN[0], [])])
        self.guard = later
        self.assertEqual(read.call({"citation": named})["error"]["code"], "egress_withheld")
        self.guard = PeopleExclusion(unavailable="excluded_people_oids_absent")
        self.assertEqual(read.call({"citation": mine})["error"]["code"], "not_ready")


class Attachments(Base):
    def test_a_cut_attachment_is_kept_and_read_to_its_end(self):
        """A file whose text goes past what the question holds says so first, with the
        citation of the person's own copy; that copy reads to its last row; Freightcom
        named only past the cut withholds the whole file (the screens see all of it)."""
        import base64
        import json
        import types
        from operating import ask_attachments
        from operator_api import models as m
        from operator_api.backend_ask import AskMixin
        b = self.b
        for name in ("_attached", "_keep_attached"):
            setattr(b, name, types.MethodType(getattr(AskMixin, name), b))

        def ask(*files):
            return b._attached(m.AskRequest(question="check it", attachments=[
                {"name": n, "data": base64.b64encode(d).decode()} for n, d in files]),
                self.ana)
        big, late = csv_bytes(4000), csv_bytes(4000, "IT-9,1,Freightcom pickup")
        kept, withheld = ask(("orders.csv", big), ("late.csv", late)).files
        self.assertTrue(kept.cut)
        self.assertIsNone(kept.data)
        cite = re.search(r'"citation": "(file:f_[0-9a-f]+)"', kept.text.split("\n", 1)[0])
        self.assertTrue(cite, kept.text[:300])
        self.assertIsNone(kept.kept)   # a CSV is read on, not edited
        self.assertEqual(withheld.unread, ask_attachments.WITHHELD)
        self.assertEqual(withheld.text, "")
        read = self.tools(self.ana)["plx.read_document"]
        last = read.call({"citation": cite.group(1), "find": "IT-03999"})
        self.assertEqual(len(last["matches"]), 1)
        self.assertNotIn("f_", json.dumps(kept.meta))
        self.assertEqual(self.tools(self.sam)["plx.read_document"].call(
            {"citation": cite.group(1)})["error"]["code"], "not_found")


def scan_pdf(pages: int) -> bytes:
    """A PDF of ``pages`` scanned pages (images, no text layer), each reading
    "PAGE <n> TOTAL <n>00.50"."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
    images = []
    for n in range(1, pages + 1):
        image = Image.new("L", (1500, 300), 255)
        ImageDraw.Draw(image).text((60, 100), f"PAGE {n} TOTAL {n}00.50", fill=0, font=font)
        images.append(image)
    out = io.BytesIO()
    images[0].save(out, "PDF", resolution=200, save_all=True, append_images=images[1:])
    return out.getvalue()


@unittest.skipUnless(shutil.which("tesseract") and shutil.which("pdftoppm")
                     and Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists(),
                     "local OCR (poppler + tesseract + a font) not on this host")
class LongScans(Base):
    """OCR inside a request is capped at 10 pages; the rest is read in the background into
    the person's kept file (one run per file, a timeout, killed when the file is deleted)."""

    def test_a_scan_past_the_cap_says_what_was_read_and_a_later_read_has_the_rest(self):
        from operator_api import ocr_background
        data = scan_pdf(12)
        direct = doc_read.read("s.pdf", data, ocr_cap=10)
        self.assertEqual((direct.ocr_pages, direct.ocr_pending, direct.ocr_total),
                         (10, [11, 12], 12))
        self.assertIn("PAGE 10", direct.text)
        self.assertNotIn("PAGE 11", direct.text)
        self.assertTrue(any("11-12" in u and "10 of the 12" in u for u in direct.unread))
        again = doc_read.read("s.pdf", data, ocr_cap=10, ocr_known={11: "PAGE 11 X", 12: "PAGE 12 X"})
        self.assertEqual((again.ocr_pending, again.ocr_pages), ([], 2))
        self.assertIn("PAGE 12 X", again.text)
        cite = self.own_file(self.ana, data, "s.pdf")
        file_id = cite[len("file:"):]
        desk = self.b._uploads()
        read = self.tools(self.ana)["plx.read_document"]
        first = read.call({"citation": cite})
        self.assertEqual(first["ocr_pending_pages"], 2)
        self.assertIn("10 of 12", first["ocr_note_more"])
        self.assertIn("still being read", first["ocr_note_more"])
        self.assertNotIn("PAGE 11", first["content"]["untrusted_text"])
        self.assertIn(ocr_background.start(desk, file_id), ("running", "done"))  # one run per file
        proc = ocr_background._RUNNING.get(file_id)
        if proc is not None:
            proc.wait(timeout=180)
        for _ in range(100):
            if not ocr_background.running(file_id):
                break
            time.sleep(0.1)
        got = ocr_background.known(desk, file_id)
        self.assertEqual(sorted(got), list(range(1, 13)))
        self.assertIn("PAGE 12", got[12])
        later = read.call({"citation": cite, "find": "TOTAL 1200.50"})
        self.assertNotIn("ocr_pending_pages", later)
        self.assertEqual(len(later["matches"]), 1)
        self.assertEqual(self.tools(self.sam)["plx.read_document"].call(
            {"citation": cite})["error"]["code"], "not_found")

    def test_a_scan_attachment_says_how_many_pages_were_read(self):
        import base64
        import types
        from operator_api import models as m, ocr_background
        from operator_api.backend_ask import AskMixin
        b = self.b
        for name in ("_attached", "_keep_attached", "_ocr_rest"):
            setattr(b, name, types.MethodType(getattr(AskMixin, name), b))
        got = b._attached(m.AskRequest(question="total?", attachments=[
            {"name": "scan.pdf", "data": base64.b64encode(scan_pdf(11)).decode()}]),
            self.ana).files[0]
        self.assertTrue(got.text.startswith("[Scanned pages: 10 of 11 were read by OCR"), got.text[:200])
        self.assertIn("still being read in the background", got.text[:300])
        self.assertIn("PAGE 10", got.text)
        self.assertNotIn("PAGE 11 TOTAL", got.text)
        cite = re.search(r'"citation": "file:(f_[0-9a-f]+)"', got.text)
        proc = ocr_background._RUNNING.get(cite.group(1)) if cite else None
        if proc is not None:
            proc.wait(timeout=180)
            time.sleep(1.5)


    def test_sidecar_pages_are_screened_and_only_the_files_own_digest_counts(self):
        """Excluded or Freightcom text only in sidecar pages is withheld on the next read;
        a sidecar written for other bytes is not used (a fresh OCR cap applies)."""
        import hashlib
        import json
        from operator_api import ocr_background
        data = scan_pdf(12)
        cite = self.own_file(self.ana, data, "s.pdf")
        file_id = cite[len("file:"):]
        desk = self.b._uploads()
        sidecar = Path(desk.dir) / "files" / (file_id + ".ocr.json")
        read = self.tools(self.ana)["plx.read_document"]

        def write(pages, digest=None):
            sidecar.write_text(json.dumps({"digest": digest or hashlib.sha256(data).hexdigest(),
                                           "pages": {str(n): t for n, t in pages.items()}}))
        clean = {n: f"PAGE {n} TOTAL {n}00.50" for n in range(1, 13)}
        write(clean)
        self.assertEqual(sorted(ocr_background.known(desk, file_id)), list(range(1, 13)))
        self.assertNotIn("ocr_pending_pages", read.call({"citation": cite}))
        for late in (f"ask {HIDDEN[0]}", "Freightcom pickup at 9"):
            with self.subTest(late=late[:12]):
                write({**clean, 12: late})
                got = read.call({"citation": cite})
                self.assertEqual(got["error"]["code"], "egress_withheld")
                self.assertNotIn("content", got)
                self.assertEqual(read.call({"citation": cite, "find": "ask"})["error"]["code"],
                                 "egress_withheld")
        write(clean, digest="0" * 64)               # another file's pages: never used
        self.assertIsNone(ocr_background.known(desk, file_id))
        self.assertEqual(read.call({"citation": cite})["ocr_pending_pages"], 2)


class BackgroundBounds(Base):
    def setUp(self):
        super().setUp()
        from operator_api import ocr_background
        self.bg = ocr_background
        self.addCleanup(ocr_background._TRIED.clear)

    def keep(self):
        row = self.b._uploads().prepare(self.ana.key, name="s.pdf", kind="pdf", data=b"%PDF-1.4 x",
                                        target=None, people=[])
        return row["file_id"], Path(self.b._uploads().dir) / "files" / row["file_id"]

    def test_one_run_per_file_killed_when_the_file_is_deleted_or_late(self):
        file_id, path = self.keep()
        desk = self.b._uploads()
        with mock.patch.object(self.bg, "_command", lambda s, o: ["sleep", "300"]), \
                mock.patch.object(self.bg, "POLL", 0.05):
            self.assertEqual(self.bg.start(desk, file_id), "started")
            proc = self.bg._RUNNING[file_id]
            self.assertEqual(self.bg.start(desk, file_id), "running")   # not a second one
            path.unlink()                                              # the file is deleted
            proc.wait(timeout=10)
            for _ in range(100):
                if not self.bg.running(file_id) and file_id not in self.bg._RUNNING:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(proc.returncode)
            self.assertNotIn(file_id, self.bg._RUNNING)
        file_id, path = self.keep()
        self.bg._TRIED.clear()
        # a run past its time is killed, and the file is not tried again at once
        row = self.b._uploads().prepare(self.ana.key, name="t.pdf", kind="pdf", data=b"%PDF-1.4 yy",
                                        target=None, people=[])
        late = row["file_id"]
        with mock.patch.object(self.bg, "_command", lambda s, o: ["sleep", "300"]), \
                mock.patch.object(self.bg, "POLL", 0.05):
            self.assertEqual(self.bg.start(desk, late, seconds=0.3), "started")
            proc = self.bg._RUNNING[late]
            proc.wait(timeout=10)
            for _ in range(100):
                if late not in self.bg._RUNNING:
                    break
                time.sleep(0.05)
            self.assertEqual(self.bg.start(desk, late), "later")
        self.assertEqual(self.bg.start(desk, "f_" + "1" * 24), "none")   # no such file
        self.assertEqual(self.bg.start(desk, "../x"), "none")            # not a file id
        for bad in ("../x", "f_" + "1" * 23, "f_" + "G" * 24, "f_" + "1" * 24 + "/../y", ""):
            self.assertIsNone(self.bg._paths(desk, bad), bad)
        self.assertIsNotNone(self.bg._paths(desk, "f_" + "1" * 24))

    def test_at_most_two_children_on_the_host_and_one_per_file_across_processes(self):
        desk = self.b._uploads()
        ids = []
        for n in range(4):
            row = desk.prepare(self.ana.key, name=f"s{n}.pdf", kind="pdf",
                               data=b"%PDF-1.4 " + bytes([65 + n]) * 5, target=None, people=[])
            ids.append(row["file_id"])
        with mock.patch.object(self.bg, "_command", lambda s, o: ["sleep", "300"]), \
                mock.patch.object(self.bg, "POLL", 0.05):
            got = [self.bg.start(desk, i) for i in ids]
            self.assertEqual(got, ["started", "started", "later", "later"])
            # another API process (no memory of these): the file's lock is still held
            running = dict(self.bg._RUNNING)
            self.bg._RUNNING.clear()
            self.assertEqual(self.bg.start(desk, ids[0]), "running")
            self.bg._RUNNING.update(running)
            for proc in running.values():
                self.bg._kill(proc)
            for proc in running.values():
                proc.wait(timeout=10)
            for _ in range(100):
                if not self.bg._RUNNING:
                    break
                time.sleep(0.05)
            self.bg._TRIED.clear()
            self.assertEqual(self.bg.start(desk, ids[2]), "started")   # a slot is free again
            for proc in list(self.bg._RUNNING.values()):
                self.bg._kill(proc)


class LegacyAttachments(Base):
    def test_a_cut_old_xls_is_kept_as_theirs_and_read_to_the_end(self):
        """Review minor 11-21: a cut .xls got no file: pointer and could not be read whole."""
        import base64
        import types
        from integrations.tests.test_partial_and_legacy_extraction import xls
        from operator_api import models as m
        from operator_api.backend_ask import AskMixin
        b = self.b
        for name in ("_attached", "_keep_attached"):
            setattr(b, name, types.MethodType(getattr(AskMixin, name), b))
        got = b._attached(m.AskRequest(question="check", attachments=[
            {"name": "old.xls", "data": base64.b64encode(xls(sheet_rows=20000)).decode()}]),
            self.ana).files[0]
        self.assertTrue(got.cut, got.unread)
        cite = re.search(r'"citation": "(file:f_[0-9a-f]+)"', got.text.split("\n", 1)[0])
        self.assertTrue(cite, got.text[:300])
        read = self.tools(self.ana)["plx.read_document"]
        first = read.call({"citation": cite.group(1)})
        self.assertNotIn("error", first)
        self.assertEqual(first["kind"], "spreadsheet")
        self.assertEqual(self.tools(self.sam)["plx.read_document"].call(
            {"citation": cite.group(1)})["error"]["code"], "not_found")
        own = b._pw_file(self.ana, cite.group(1)[len("file:"):])
        self.assertEqual(own["content_type"], "application/vnd.ms-excel")


class FakeReads:
    """``insights.read_sql`` as each reader: their own rows only, plx.sql's page (200 rows)
    and a byte cap (``page`` rows) whatever the statement asks; ``drift`` adds a row after
    the count, as a writer would."""

    def __init__(self, rows_by_reader, page=37):
        self.rows, self.page, self.drift, self.calls = rows_by_reader, page, False, 0

    def __call__(self, backend, reader, sql, *a, **k):
        self.calls += 1
        rows = list(self.rows[reader.key])
        columns = ["customer", "order_number", "value"]
        if "count(*) AS plx_rows" in sql:
            if self.drift:
                self.rows[reader.key].append(["Late Ltd", "OR-LATE", 1])
            return {"columns": ["plx_rows"], "rows": [{"cells": [len(rows)]}],
                    "truncated": False}
        paged = re.search(r"ORDER BY (.*) LIMIT 200 OFFSET (\d+)$", sql)
        if paged:
            keys = [k.strip() for k in paged.group(1).split(",")]
            for key in reversed(keys):
                if key == "plx_t::text":
                    rows.sort(key=lambda r: str(r))
                else:
                    pos, *direction = key.split()
                    rows.sort(key=lambda r: r[int(pos) - 1], reverse=direction[:1] == ["desc"])
            start = int(paged.group(2))
            got = rows[start:start + min(200, self.page)]
            return {"columns": columns, "rows": [{"cells": r} for r in got],
                    "truncated": start + len(got) < len(rows)}
        if sql.endswith("ORDER BY value DESC"):   # the statement as plx.sql runs it
            rows.sort(key=lambda r: r[2], reverse=True)
        return {"columns": columns, "rows": [{"cells": r} for r in rows[:self.page]],
                "truncated": len(rows) > self.page}


class Tables(Base):
    SQL = ("SELECT customer, order_number, value FROM sales_orders WHERE status = 'open' "
           "ORDER BY value DESC")

    def make(self, reader, kind="xlsx", content="# Open orders\n[table 1]", sql=SQL):
        return self.tools(reader)["plx.make_document"].call(
            {"name": "Open orders", "kind": kind, "content": content,
             "tables": [{"sql": sql, "title": "Open orders"}]})

    def file_rows(self, reader, made):
        data = self.b._uploads().file(reader.key, made["file_id"])
        return re.findall(r"(OR-\d+)", doc_read.read("f", data, ocr=False).text)

    def test_every_row_as_the_person_reads_it(self):
        rows = {self.ana.key: [], self.sam.key: [["Beta", "OR-90001", 5]]}
        fake = FakeReads(rows)
        with mock.patch("operator_api.insights.read_sql", fake):
            for n in (1, 10, 60, 450):
                with self.subTest(rows=n):
                    rows[self.ana.key][:] = [[f"Cust {i % 7}", f"OR-{i:05d}", i % 113]
                                             for i in range(n)]
                    made = self.make(self.ana)
                    self.assertNotIn("refused", made)
                    self.assertEqual(made["tables"][0]["rows"], n)
                    got = self.file_rows(self.ana, made)
                    self.assertEqual(sorted(got), sorted(f"OR-{i:05d}" for i in range(n)))
                    # the statement's own order (value DESC) is the file's
                    values = [r[2] for r in rows[self.ana.key]]
                    by_code = {r[1]: r[2] for r in rows[self.ana.key]}
                    self.assertEqual([by_code[c] for c in got], sorted(values, reverse=True))
            theirs = self.make(self.sam)
            self.assertEqual(self.file_rows(self.sam, theirs), ["OR-90001"])
            # a CSV of one table needs only the placement
            csv = self.make(self.ana, kind="csv", content="[table 1]")
            self.assertEqual(len(self.file_rows(self.ana, csv)), 450)

    def test_a_table_that_cannot_be_held_whole_is_refused(self):
        rows = {self.ana.key: [[f"C{i}", f"OR-{i:05d}", i] for i in range(120)]}
        fake = FakeReads(rows)
        with mock.patch("operator_api.insights.read_sql", fake):
            fake.drift = True
            self.assertIn("changed while", self.make(self.ana)["refused"])
            fake.drift = False
            self.assertIn("refused", self.make(self.ana, kind="pptx"))   # 121 rows on a slide
            with mock.patch("operating.instruct_files.MAX_TABLE_ROWS",
                            {"xlsx": 100, "csv": 100, "docx": 100, "pdf": 100, "pptx": 30}):
                self.assertIn("more than the 100", self.make(self.ana)["refused"])
            self.assertIn("refused", self.tools(self.ana)["plx.make_document"].call(
                {"name": "x", "kind": "xlsx", "content": "[table 1]",
                 "tables": [{"sql": self.SQL, "where": "1=1"}]}))
        self.assertEqual(len(self.b._uploads()._rows()), 0)   # nothing made for a refusal

    def test_cut_cells_and_an_oversized_row_are_said(self):
        long = {"untrusted_text": "x" * 400, "truncated": True}
        rows = {self.ana.key: [["C", "OR-00001", long]]}
        with mock.patch("operator_api.insights.read_sql", FakeReads(rows)):
            made = self.make(self.ana)
            self.assertIn("400 characters", made["tables"][0]["cells_cut"])
        from operator_api import doc_tables
        pages = iter([{"columns": ["a"], "rows": [], "truncated": True},
                      {"columns": ["plx_rows"], "rows": [{"cells": [3]}]},
                      {"columns": ["a"], "rows": [], "truncated": True}])
        with self.assertRaises(doc_tables.TableRefused) as why:
            doc_tables.read_all(lambda q: next(pages), "SELECT a FROM t")
        self.assertIn("larger than one read", str(why.exception))

    def test_model_rows_past_a_sheet_are_refused_not_cut(self):
        content = "\n".join(f"OR-{i} | {i}" for i in range(2500))
        got = self.tools(self.ana)["plx.make_document"].call(
            {"name": "x", "kind": "xlsx", "content": content})
        self.assertIn("tables", got["refused"])


    def test_the_row_limit_counts_data_rows_only(self):
        """Review minor 11-21: [table N] and divider lines are no rows (the file maker's own
        count): 1,990 rows with table pointers and dividers are no refusal, 2,001 data rows
        are, and the same lines through files.data_rows agree."""
        from operating import instruct_files as files
        make = self.tools(self.ana)["plx.make_document"]
        rows = [f"OR-{i} | {i}" for i in range(1990)]
        padded = "\n".join(rows + ["[table 1]", "[table 2]", "|---|---|", "---", ""] * 3)
        self.assertEqual(files.data_rows(padded), 1990)
        self.assertNotIn("refused", make.call({"name": "x", "kind": "xlsx", "content": padded}))
        over = "\n".join(f"OR-{i} | {i}" for i in range(2001))
        self.assertIn("tables", make.call({"name": "x", "kind": "xlsx", "content": over})["refused"])


class OrderKeys(unittest.TestCase):
    def test_the_statements_own_order_is_kept_when_it_names_columns(self):
        from operator_api.doc_tables import order_keys
        cols = ["customer", "order_number", "value"]
        self.assertEqual(order_keys("SELECT * FROM t ORDER BY value DESC NULLS LAST, 1", cols),
                         ["3 desc nulls last", "1 asc"])
        self.assertEqual(order_keys('SELECT * FROM t o ORDER BY o.customer LIMIT 900', cols),
                         ["1 asc"])
        self.assertEqual(order_keys("SELECT customer, (SELECT max(x) FROM y ORDER BY x) v "
                                    "FROM t", cols), [])
        self.assertEqual(order_keys("SELECT row_number() OVER (ORDER BY value) FROM t", cols),
                         [])
        self.assertIsNone(order_keys("SELECT * FROM t ORDER BY lower(customer)", cols))
        self.assertIsNone(order_keys("SELECT * FROM t ORDER BY value * 2", cols))


if __name__ == "__main__":
    unittest.main()
