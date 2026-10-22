"""One text path for captured files (DOC-01, ``integrations.captured_text``).

A scanned PDF mailed as an attachment is OCR-read at capture (or by a later round once
the host has OCR) and its identifiers become page-cited mention links, as a scanned
SharePoint file's do; one build reads each captured file once, mail and SharePoint
alike, for its mention document, its typed fields and resolve. The SharePoint list-item
capture is a capture store of its own: a state directory holding it without the drive
capture still projects its list items, gated per item. Projected through the host's entry
point over injected Graph transports: no network, no credentials.
"""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock
import uuid

from integrations import captured_text as ct
from integrations import document_understanding as du
from integrations.tests import test_capture_mail as tm
from integrations.capture_sharepoint import SharePointCaptureStore
from integrations.tests import test_capture_sharepoint as tsp
from integrations.tests import test_capture_sharepoint_lists as tsl
from integrations.source_mirror import AudienceMap
from ontology.tests import test_graph_projection as tg
from operating import host
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.ontology_host import CaseHostTools

SP_LINK = "SharePointFile.mentions_purchase_order"


class CapturedTextTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.case = tsp.CaptureCase("run")
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.state = self.case.state

    def build(self, mail):
        with ExitStack() as stack:
            built = host.build_projection(self.state, tg.sources(stack, self.state))
        conn = sqlite3.connect(host.projection_path(self.state))
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        self.assertEqual(built["post_project"]["mentions"]["counts"]["undeclared_target"], 0)
        return conn

    @staticmethod
    def links(conn, link_type, object_type, key, value):
        return conn.execute(
            "SELECT l.to_object, l.reason FROM links l JOIN objects o ON o.object_id = "
            "l.from_object WHERE l.link_type = ? AND o.object_type = ? AND "
            f"json_extract(o.native_key, '$.{key}') = ?", (link_type, object_type, value)
        ).fetchall()

    @staticmethod
    def po(conn, key):
        return conn.execute("SELECT object_id FROM objects WHERE object_type = 'PurchaseOrder' "
                            "AND json_extract(native_key, '$.id') = ?", (key,)).fetchone()[0]

    def test_a_scanned_pdf_mail_attachment_yields_page_cited_mention_links(self):
        scan = tsp._scanned_or_skip(self)   # page 2: "Purchase order PO100235"
        self.case.capture().run(tsp.scope())
        graph = tm.FakeGraph()
        graph.put(tm.BOX_A, "MSG-1", "CK-1-1", subject="Packet", body="Scan attached.",
                  attachments=[tg.attachment("ATT-S", "packet.pdf", scan, "application/pdf")])
        # Captured on a host without OCR: never silently textless, and nothing to link.
        with mock.patch("integrations.document_understanding.local_ocr_available",
                        return_value=False):
            mail = tg.capture_mail(self.state, graph)
        self.addCleanup(mail.close)
        status = "SELECT text_status FROM attachments WHERE attachment_id = 'ATT-S'"
        self.assertEqual(mail.db.execute(status).fetchone()[0], ct.OCR_UNAVAILABLE)
        conn = self.build(mail)
        self.assertEqual(self.links(conn, tg.ATT_LINK, "Attachment", "attachment_id", "ATT-S"),
                         [])
        conn.close()
        # A later round on a host with OCR reads the stored bytes (no source read).
        downloads = len([c for c in graph.calls if "/attachments/ATT-S" in c[1]])
        tg.capture_mail(self.state, graph, store=mail)
        self.assertEqual(len([c for c in graph.calls if "/attachments/ATT-S" in c[1]]),
                         downloads)
        self.assertEqual(mail.db.execute(status).fetchone()[0], ct.TEXT_OCR)
        conn = self.build(mail)
        rows = self.links(conn, tg.ATT_LINK, "Attachment", "attachment_id", "ATT-S")
        self.assertEqual({r["to_object"] for r in rows}, {self.po(conn, "p2"),
                                                          self.po(conn, "b2")})
        for row in rows:
            [cited] = [f for f in json.loads(row["reason"])
                       if f["feature"] == "identifier_mention"]
            [mention] = cited["citations"]
            self.assertTrue(mention["l1_handle"].endswith("&page=2"), mention)
            resolved = mail.resolve(mention["l1_handle"], tm.READER_A, tm.reader_audience(),
                                    text=True)
            start, end = cited["span"]
            self.assertEqual(resolved["row"]["content_text"][start:end], "PO100235")
            self.assertIsNone(mail.resolve(mention["l1_handle"], tg.OUTSIDER,
                                           tm.reader_audience(), text=True))

    def test_one_build_reads_each_captured_file_once_for_every_reader(self):
        run = uuid.uuid4().hex   # bytes no earlier reading in this process has seen
        sp_text = f"Release for PO100236 ({run})".encode()
        self.case.drive.file("01UNIQUE", "release-note.txt", "01AOS", sp_text,
                             mime="text/plain", acl=(tsp.RICARDO, tsp.SALES))
        self.case.capture().run(tsp.scope())
        graph = tm.FakeGraph()
        notes = {}
        for m in range(6):   # 60 attachments: no row lost or duplicated
            items = []
            for i in range(10):
                content = f"Re PO100236 batch {m} note {i} ({run})".encode()
                notes[f"ATT-{m}-{i}"] = content
                items.append(tg.attachment(f"ATT-{m}-{i}", f"note-{m}-{i}.txt", content,
                                           "text/plain"))
            graph.put(tm.BOX_A, f"MSG-{m}", f"CK-{m}", subject=f"Batch {m}", body="Notes.",
                      attachments=items)
        mail = tg.capture_mail(self.state, graph)
        self.addCleanup(mail.close)
        reads = []
        real = du.extract_document

        def counting(content, mime_type, **options):
            reads.append(sha256(content).hexdigest())
            return real(content, mime_type, **options)

        with mock.patch.object(du, "extract_document", counting):
            conn = self.build(mail)
        counted = Counter(reads)
        # Mentions, typed fields (several bindings) and resolve share one reading.
        self.assertTrue(all(n == 1 for n in counted.values()), counted.most_common(3))
        mine = {sha256(c).hexdigest() for c in notes.values()} | {sha256(sp_text).hexdigest()}
        self.assertLessEqual(mine, set(counted))
        p3 = self.po(conn, "p3")
        for aid in notes:
            self.assertEqual([r["to_object"] for r in self.links(
                conn, tg.ATT_LINK, "Attachment", "attachment_id", aid)], [p3], aid)
        self.assertEqual([r["to_object"] for r in self.links(
            conn, SP_LINK, "SharePointFile", "id", "01UNIQUE")], [p3])
        with mock.patch.object(du, "extract_document", counting):
            reads.clear()
            for aid, content in notes.items():
                m = aid.split("-")[1]
                handle = (f"{tm.graph_read.mail_citation(tm.BOX_A, f'MSG-{m}', f'CK-{m}')}"
                          f"#attachment={aid}&sha256={sha256(content).hexdigest()}")
                resolved = mail.resolve(handle, tm.READER_A, tm.reader_audience(), text=True)
                self.assertEqual(resolved["row"]["content_text"], content.decode())
            resolved = self.case.store.resolve(self.case.handle("01UNIQUE"), tsp.QUALITY,
                                               tsp.reader_audience())
            self.assertEqual(resolved["row"]["content_text"], sp_text.decode())
        self.assertEqual(reads, [])


class ListCaptureStandaloneTests(tsl.CaptureCase):
    """The list-item capture opened as its own store, with or without the drive capture."""

    def build(self) -> tuple[list[tuple], dict]:
        """The projected list items (key, audience, handle) and the item count each
        reader finds through the ontology host."""
        state = Path(self.tmp.name)
        with ExitStack() as stack:
            sources = host.state_captures(state, stack)
            self.assertEqual(set(sources), {"sharepoint"})
            built = host.build_projection(state, sources)
        self.unheld = {g["where"] for g in built["gaps"] if g["code"] == "source_not_captured"}
        path = host.projection_path(state)
        conn = sqlite3.connect(path)
        try:
            items = conn.execute(
                "SELECT o.native_key, o.audience_key, v.l1_handle FROM objects o JOIN "
                "object_versions v USING (object_id) WHERE o.object_type = "
                "'SharePointListItem' ORDER BY v.l1_handle").fetchall()
        finally:
            conn.close()
        runtime = CaseRuntime(self.tmp.name, client=None,
                              audience=AudienceMap({}, tenant="plx"), tenant="plx",
                              live=LiveReaders(self.audience))
        self.addCleanup(runtime.close)
        find = {"object_type": "SharePointListItem", "limit": 50,
                "filters": [{"property": "id", "op": "present"}]}
        found = {}
        for actor in (tsl.READER, tsl.OUTSIDER, tsl.WITHHELD):
            tools = CaseHostTools(runtime, actor, store_path=path)
            self.addCleanup(tools.close)
            out = tools.call("plx.find", find, actor)
            found[actor] = len(out["candidates"])
            if actor is not tsl.READER:
                self.assertNotIn(tsl.VENDOR, json.dumps(out), actor)
                self.assertNotIn("sharepoint-list://", json.dumps(out), actor)
        return items, found

    def test_list_items_project_without_the_drive_capture_and_the_same_beside_it(self):
        self.graph.perms["2"] = []   # shared with nobody the audience names
        self.assertTrue(self.capture.run(tsl.scope())["complete"])
        alone, found = self.build()
        self.assertEqual(len(alone), 5)
        self.assertTrue(all(a.startswith("sharepoint-list:") and h.startswith(
            "sharepoint-list://") for _, a, h in alone), alone)
        self.assertEqual(found, {tsl.READER: 4, tsl.OUTSIDER: 0, tsl.WITHHELD: 0})
        # The absent drive capture is reported not captured, never read as empty.
        self.assertIn("sharepoint.drive_items", self.unheld)
        SharePointCaptureStore(self.tmp.name).db.close()   # a drive capture beside it
        self.assertEqual(self.build(), (alone, found))
        self.assertNotIn("sharepoint.drive_items", self.unheld)


if __name__ == "__main__":
    unittest.main()
