"""Container member readings kept between builds (``captured_text.KeptMembers``) and the
primer that fills them ahead of a build (``ontology.member_primer``)."""

from __future__ import annotations

from contextlib import closing
import datetime as dt
import io
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock
import zipfile

from integrations import captured_text as ct
from integrations import kept_members as km
from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import (BOX_A, CAPTURE, ENV, SCOPE, Clock, FakeGraph,
                                                   capture_audience, file_attachment)
from ontology import member_primer
from ontology.project import MailCaptures

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)


def _zip(**files: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as packed:
        for name, data in files.items():
            packed.writestr(name, data)
    return out.getvalue()


def _fresh_memo():
    return unittest.mock.patch.object(ct, "_MEMBER_READINGS", ct._MEMBER_READINGS.empty())


class KeptMembersTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.blobs = self.root / "blobs"
        self.store = self.root / "member-readings.sqlite"

    def put(self, data: bytes, name: str) -> dict:
        digest = ct.sha256(data).hexdigest()
        path = ct.blob_path(self.blobs, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"content_sha256": digest, "name": name, "content_type": "application/zip"}

    def test_a_kept_member_reading_is_served_to_the_next_build_only_as_a_read_would_give_it(self):
        row = self.put(_zip(**{"a.txt": b"PO100235 confirmed", "b.csv": b"x,y\n1,2\n"}),
                       "docs.zip")
        with _fresh_memo(), km.kept_members(self.store, [self.blobs]):
            first = ct.container_members(self.blobs, row)
        self.assertEqual([m.name for m in first.members], ["a.txt", "b.csv"])
        self.assertEqual(oct(os.stat(self.store).st_mode & 0o777), "0o600")
        reader = unittest.mock.Mock(side_effect=AssertionError("read again"))
        never = unittest.mock.patch.object(ct, "read_container", reader)
        with _fresh_memo(), never, km.kept_members(self.store, [self.blobs]) as kept:   # the next build
            self.assertEqual(ct.container_members(self.blobs, row), first)
            self.assertEqual(kept.hits, 1)
        # Another reading code (an extractor or container walk changed): read again.
        with _fresh_memo(), unittest.mock.patch.object(km, "members_code", lambda: "other"), \
                km.kept_members(self.store, [self.blobs]) as kept:
            self.assertEqual(ct.container_members(self.blobs, row), first)
            self.assertEqual((kept.hits, kept.kept), (0, 1))
        # Bytes gone: no members, as the read itself finds, kept or not.
        ct.blob_path(self.blobs, row["content_sha256"]).unlink()
        with _fresh_memo(), km.kept_members(self.store, [self.blobs]):
            self.assertIsNone(ct.container_members(self.blobs, row))
        self.assertEqual(reader.call_count, 0)

    def test_a_member_read_that_failed_transiently_is_never_kept(self):
        row = self.put(_zip(**{"a.txt": b"text"}), "one.zip")
        real = ct.read_text
        with _fresh_memo(), km.kept_members(self.store, [self.blobs]) as kept, \
                unittest.mock.patch.object(ct, "read_text", side_effect=OSError("busy")):
            found = ct.container_members(self.blobs, row)
            self.assertTrue(found.retry)
            self.assertIsNone(found.members[0].reading)
            self.assertEqual(kept.kept, 0)
        with _fresh_memo(), km.kept_members(self.store, [self.blobs]) as kept, \
                unittest.mock.patch.object(ct, "read_text", side_effect=real):
            found = ct.container_members(self.blobs, row)
            self.assertEqual((kept.hits, kept.kept), (0, 1))
            self.assertEqual(found.members[0].reading.text, "text")

    def test_a_reading_made_out_of_memory_is_never_kept(self):
        """Running out of memory is the process's, never the file's: a mail whose parser
        ran out (as a 135 MB message can) or a PDF member whose reader's native code did
        (``std::bad_alloc``, caught by the reader, which reads the page as unreadable)
        is not kept as unreadable or empty; the next read with memory keeps it whole."""
        from email.message import EmailMessage
        import sys
        from integrations import document_understanding as du
        from operating import use_case_parts

        message = EmailMessage()
        message["From"], message["Subject"] = "a@example.com", "Docs"
        message.set_content("See attached.")
        for n in range(3):
            message.add_attachment(f"note {n}".encode(), maintype="text", subtype="plain",
                                   filename=f"note{n}.txt")
        mail = {**self.put(message.as_bytes(), "fwd.eml"), "content_type": "message/rfc822"}

        class Starved:
            def __init__(self, **_):
                pass

            def parsebytes(self, _data):
                raise MemoryError()
        with _fresh_memo(), km.kept_members(self.store, [self.blobs]) as kept, \
                unittest.mock.patch.object(use_case_parts, "BytesParser", Starved):
            self.assertIsNone(ct.container_members(self.blobs, mail))
            self.assertEqual(kept.kept, 0)

        packed = self.put(_zip(**{"a.pdf": b"%PDF-1.4 not really", "b.txt": b"text"}),
                          "pdfs.zip")
        import pypdf
        native = unittest.mock.patch.object(
            pypdf, "PdfReader", side_effect=RuntimeError("onnxruntime: std::bad_alloc"))
        no_poppler = unittest.mock.patch.object(du, "local_pdf_text_layer", return_value=None)
        with _fresh_memo(), km.kept_members(self.store, [self.blobs]) as kept, native, \
                no_poppler:
            found = ct.container_members(self.blobs, packed)
            self.assertIsNotNone(found)   # read on, the page unreadable: not the file's
            if hasattr(sys, "monitoring"):
                self.assertEqual(kept.kept, 0)
        with _fresh_memo(), km.kept_members(self.store, [self.blobs]) as kept:
            self.assertEqual(len(ct.container_members(self.blobs, mail).members), 3)
            self.assertIsNotNone(ct.container_members(self.blobs, packed))
            self.assertEqual((kept.hits, kept.kept), (0, 2))

    def test_the_store_has_one_writer_a_build_reads_without_it_while_a_primer_holds_it(self):
        row = self.put(_zip(**{"a.txt": b"text"}), "one.zip")
        with km.kept_members(self.store, [self.blobs], required=True):
            with _fresh_memo(), km.kept_members(self.store, [self.blobs]) as kept:
                self.assertIsNone(kept)
                self.assertIsNotNone(ct.container_members(self.blobs, row))
                self.assertIsNone(ct._MEMBER_READINGS.backing)
            with self.assertRaises(km.MembersBusy):
                with km.kept_members(self.store, [self.blobs], required=True):
                    pass
            with self.assertRaises(km.MembersBusy):
                member_primer.prime(self.root, self.root, workers=1)
        with km.kept_members(self.store, [self.blobs]) as kept:   # released
            self.assertIsNotNone(kept)


class PrimerTests(unittest.TestCase):
    def test_a_primed_store_is_what_the_build_reads_members_from(self):
        """The primer reads each container the build reads members of (mail attachments,
        once per key), under the build's key and code: a build after it reads no
        container again and derives the generation a build without it derives. Run
        again, it reads nothing (resumable)."""
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        from ontology import document_fields, schema as schema_loader
        from ontology import project_incremental as inc

        graph = FakeGraph()
        inner = _zip(**{"note.txt": b"Inner PO100231 note"})
        for n in range(3):   # the same zip twice (one key), and a plain file
            graph.put(BOX_A, f"MSG-{n}", f"CK-{n}", subject=f"Docs {n}",
                      body=f"For PO10023{n}.",
                      attachments=[file_attachment(f"ZIP-{n}", _zip(**{
                          "a.txt": f"Packing list PO10023{n % 2}".encode(),
                          "inner.zip": inner}), f"docs{n % 2}.zip"),
                          file_attachment(f"TXT-{n}", b"plain", f"plain{n}.txt")])
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        state = root / "state"
        capture = MailCaptureStore(state, clock=Clock())
        self.addCleanup(capture.close)
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(capture, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        self.assertTrue(run["complete"], run)

        primed = root / "primed"
        primed.mkdir()
        counts = member_primer.prime(state, primed, workers=2)
        self.assertEqual((counts["files"], counts["distinct"], counts["containers"],
                          counts["read"], counts["newly_kept"]), (6, 5, 2, 2, 2), counts)
        again = member_primer.prime(state, primed, workers=1)
        self.assertEqual((again["kept"], again["containers"], again["read"]), (2, 0, 0))

        def builds(projection):   # a primer is not a build: it is never counted
            with closing(sqlite3.connect(projection / "member-readings.sqlite")) as db:
                return int(db.execute("SELECT value FROM meta WHERE key = 'builds'"
                                      ).fetchone()[0])
        self.assertEqual(builds(primed), 0)

        compiled = schema_loader.load_schema()

        def build(projection, patches=()):
            with MailCaptures(state, internal_domains=SCOPE.internal_domains) as mail, \
                    unittest.mock.patch.object(ct, "_CACHE", ct._CACHE.empty()), \
                    _fresh_memo(), \
                    unittest.mock.patch.object(document_fields, "_FIELDS",
                                               document_fields._FIELDS.empty()):
                for patch in patches:
                    patch.start()
                try:
                    return inc.build_generation(projection, compiled, {"graph_mail": mail},
                                                clock=lambda: NOW, workers=1)
                finally:
                    for patch in patches:
                        patch.stop()
        unprimed = build(root / "unprimed")
        reader = unittest.mock.Mock(side_effect=AssertionError("read again"))
        after = build(primed, [unittest.mock.patch.object(ct, "read_container", reader)])
        self.assertEqual(reader.call_count, 0)
        self.assertEqual(after.content_digest, unprimed.content_digest)
        self.assertEqual(builds(primed), 1)
        with closing(sqlite3.connect(primed / inc.MEMBERS)) as db, \
                closing(sqlite3.connect(root / "unprimed" / inc.MEMBERS)) as other:
            query = "SELECT key, code, value FROM members ORDER BY key"
            self.assertEqual(db.execute(query).fetchall(), other.execute(query).fetchall())
            self.assertEqual(db.execute("SELECT DISTINCT code FROM members").fetchall(),
                             [(km.members_code(),)])


if __name__ == "__main__":
    unittest.main()
