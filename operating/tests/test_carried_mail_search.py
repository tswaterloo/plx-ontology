"""DOC-008 in search: a mail attachment that carries a mail the people exclusion does not
clear now (an .eml or a zip stored before the capture-time drop, naming the excluded
person) is never listed, cited or read through ``plx.documents`` / ``plx.read`` on the
case host: its search hit goes through the host's item gate (``CaseRuntime`` over the
mail capture), which resolves it for nobody. A clean .eml beside it is still found.

The capture is filled over the capture tests' in-process FakeGraph, the projection built
from it. All mailboxes, addresses and texts are synthetic.
Evidence label: isolated-fixture / injected-transport. No network, no credentials.
"""

from __future__ import annotations

import datetime as dt
import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.captured_text import member_handle
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, READER_A, SCOPE,
                                                   UPN_A, UPN_B, Clock, FakeGraph,
                                                   capture_audience, file_attachment,
                                                   reader_audience)
from ontology import mentions
from ontology import schema as schema_loader
from ontology.project import MailCaptures, build_store
from operating.case_mail_scope import MailScope
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaptureCaseHostTools, CaseCaptures, page_build
from operating.mcp_server import MCPServer
from operating.tests.test_case_tools_host import _client

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)
EXCLUDING_B = {**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_B}
SECRET = (f"From: {UPN_B}\r\nTo: {UPN_A}\r\nSubject: plan\r\nContent-Type: text/plain"
          "\r\n\r\nPO100777 SECRETBODY the plan").encode()
CLEAN = (f"From: sales@acme.example\r\nTo: {UPN_A}\r\nSubject: quote\r\nContent-Type: "
         "text/plain\r\n\r\nPO100777 QUOTEBODY 42 boxes").encode()


def guard() -> PeopleExclusion:
    """B excluded, with the tenant's directory attached (the host's recorded one), so a
    carried mail's addresses are decided."""
    held = PeopleExclusion([BOX_B])
    held.attach_directory([(BOX_A, UPN_A, []), (BOX_B, UPN_B, [])])
    return held


def _zipped(**files: bytes) -> bytes:
    out = BytesIO()
    with ZipFile(out, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


class CarriedMailSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "mail"
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-1", "CK-1", subject="Fwd PO100777", body="See attached.",
                  received="2026-09-30T08:00:00Z", attachments=[
                      {**file_attachment("ATT-EML", SECRET, name="fwd.eml"),
                       "contentType": "message/rfc822"},
                      {**file_attachment("ATT-ZIP", _zipped(**{"fwd.eml": SECRET,
                                                                 "quote.txt": b"42 boxes"}),
                                         name="mails.zip"), "contentType": "application/zip"},
                      {**file_attachment("ATT-OK", CLEAN, name="quote.eml"),
                       "contentType": "message/rfc822"}])
        store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(EXCLUDING_B, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        # Stored before the capture-time drop (DOC-008): the bytes and text are held.
        with mock.patch.object(MailCapture, "_carries_withheld_mail",
                               lambda _self, _c, _r: (None, None)):
            run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                              sleep=lambda s: None).run(SCOPE)
        assert run["mailboxes"][0]["complete"], run   # B, excluded, is never read
        cls.handles = dict(store.db.execute(
            "SELECT attachment_id, l1_handle FROM attachments").fetchall())
        cls.handles["MESSAGE"] = store.db.execute(
            "SELECT DISTINCT message_handle FROM attachments").fetchone()[0]
        store.close()
        cls.projection = Path(cls.tmp.name) / "ontology.sqlite"
        with MailCaptures(cls.state, internal_domains=SCOPE.internal_domains) as mail:
            build_store(cls.projection, schema_loader.load_schema(), {"graph_mail": mail},
                        clock=lambda: NOW,
                        post_project={"mentions": mentions.store_mention_links})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def host(self, exclusion: PeopleExclusion) -> CaptureCaseHostTools:
        root = Path(self.tmp.name)
        audience = root / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {}}), encoding="utf-8")
        runtime = CaseRuntime(root / "state", client=None, audience=audience, tenant="plx",
                              live=LiveReaders(audience=reader_audience()),
                              mail_scope=MailScope.unrestricted(), people_exclusion=exclusion)
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=self.state)
        return CaptureCaseHostTools(runtime, READER_A, store_path=self.projection,
                                    mail_capture_dir=self.state)

    def decisions(self, host: CaptureCaseHostTools, *handles: str) -> dict[str, tuple]:
        """Each handle's item gate decision: the full check (a citation, a read) and the
        row-only gate a search and a page build use (``_mail_search_gate``)."""
        gate = host.tools.item_gate
        out = {}
        for handle in handles:
            full = gate(READER_A, handle)
            with page_build():
                rows = gate(READER_A, handle)
            out[handle] = (full, rows)
        return out

    def test_the_item_gate_withholds_attachments_carrying_mail_not_cleared(self):
        self.maxDiff = None
        h = self.handles
        secret_member = member_handle(h["ATT-ZIP"], "fwd.eml", sha256(SECRET).hexdigest())
        clean_member = member_handle(h["ATT-ZIP"], "quote.txt",
                                     sha256(b"42 boxes").hexdigest())
        host = self.host(guard())
        self.assertEqual(self.decisions(host, h["ATT-EML"], h["ATT-ZIP"], secret_member,
                                        h["ATT-OK"], clean_member, h["MESSAGE"]), {
            h["ATT-EML"]: (False, False), h["ATT-ZIP"]: (False, False),
            secret_member: (False, False),
            # the cleared .eml, the zip's cleared sibling and the message still pass
            h["ATT-OK"]: (True, True), clean_member: (True, True),
            h["MESSAGE"]: (True, True)})
        # A list that cannot be read: nothing captured passes (the runtime refuses).
        host = self.host(PeopleExclusion(unavailable="excluded_people_file_unreadable"))
        self.assertEqual(set(self.decisions(host, *h.values()).values()), {(False, False)})

    def test_a_search_never_lists_or_reads_the_withheld_mail(self):
        host = self.host(guard())
        server = MCPServer(case_runtime=host.runtime, actor=READER_A, ontology=host)
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "carried-mail-search-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for tool, arguments in (("plx.documents", {"query": "PO100777"}),
                                ("plx.find", {"query": "PO100777"})):
            found = _client(server, tool, arguments)
            documents = json.dumps(found["structuredContent"].get(
                "documents", found["structuredContent"].get("candidates")))
            self.assertNotIn("SECRETBODY", documents)
            self.assertNotIn("ATT-EML", documents)
            self.assertNotIn("ATT-ZIP", documents)
        for att in ("ATT-EML", "ATT-ZIP"):
            read = json.dumps(_client(server, "plx.read", {"citation": self.handles[att]}))
            self.assertNotIn("SECRETBODY", read)


if __name__ == "__main__":
    unittest.main()
