"""Structure-aware chunks keyed as their source's gate, and incremental embedding.

Boundaries tested (all identifiers and text synthetic):

* Keys equal the gate's. A real projection (mail capture -> ``build_store`` with the word
  index) is chunked; for several reader policies the chunks the contract's SQL prefilter
  admits (``audience_key`` granted, ``markings`` held) are exactly the objects ``plx.find``
  admits by audience and markings, so a chunk from a mailbox the reader cannot read is
  hidden; markings on the text are carried into the chunk.
* The people exclusion: an excluded mailbox, OneDrive, chat member or sender is never
  chunked; an unavailable guard refuses the run; undecidable units are withheld.
* Each chunker: mail (quoted history removed, sender role, ids in the header), attachment
  pages, Teams windows, MC tasks, spreadsheet rows with repeated headers, document
  sections and slides, record free text, entities; unknown keys are not written.
* Postgres (a throwaway ``plx_test_b_<pid>`` database, skipped without one): an unchanged
  re-run rewrites nothing and keeps every vector; a changed chunk alone is re-embedded;
  equal contents are embedded once; duplicate clusters; withheld units deleted; a shrink
  past the guard is refused.
* Embedding providers: remote refused without ``PLX_EMBED_ALLOW_REMOTE=1``; batches and
  429 retries; the sender reaches its one URL only and the egress gate pins it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np

from integrations.people_exclusion import PeopleExclusion
from ontology import store_ddl
from ontology.types import Actor
from operating.chunking import dedup as dd
from operating.chunking import split
from operating.chunking.entities import entities, fold
from operating.chunking.normalise import normalise
from operating.chunking.permissions import Exclusion, valid_key
from operating.chunking.projection import Counts, ProjectionChunker, open_ro
from operating.chunking.records import RecordChunker

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = "0e0e0e0e-0000-4000-8000-00000000e0e1"
STAFF = "0f0f0f0f-0000-4000-8000-0000000000a1"


def exclusion(*, excluded=(EXCLUDED,), directory=None, drives=None, chats=None,
              domains=("example-plx.com",)) -> Exclusion:
    return Exclusion(PeopleExclusion(list(excluded)), directory=directory or {},
                     internal_domains=domains, drive_owners=drives or {},
                     chat_members=chats or {})


# --------------------------------------------------------------------------- real store

class MailStoreTests(unittest.TestCase):
    """A real projection built from a mail capture (``ontology/tests/test_text_index``)."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        from integrations import graph_read
        from integrations.capture_mail import MailCapture, MailCaptureStore
        from integrations.graph_read import GraphMailReader
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, SCOPE,
                                                           Clock, FakeGraph, capture_audience,
                                                           file_attachment)
        from ontology import schema as schema_loader
        from ontology.project import MailCaptures, build_store
        from ontology.tests.test_document_fields import pdf
        from ontology.tests.test_mail_documents import StaticPortal, po
        from ontology.text_index import store_text_index
        cls.BOX_A, cls.BOX_B = BOX_A, BOX_B
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "state"
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-1", "CK-1-1", subject="Packing for PO100234",
                  body="Packing list attached for PO100234, 500kgs on two pallets.",
                  attachments=[file_attachment("ATT-1", pdf([["Page one: cartons"],
                                                             ["Lanolin reconciliation pending"]]),
                                               name="packing.pdf")])
        graph.put(BOX_A, "MSG-2", "CK-2-1", subject="Re: Marigold freight",
                  body="Marigold freight is booked for Tuesday morning at the dock.\r\n\r\n"
                       "On Mon, Sep 28, 2026 at 9:00 AM Someone wrote:\r\n"
                       "> QUOTEDSECRET old pricing for the freight lane\r\n")
        graph.put(BOX_B, "MSG-B1", "CKB-1", subject="Finance",
                  body="Lanolin reconciliation pending audit (finance copy) for PO100234.")
        cls.capture = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(cls.capture, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
        cls.compiled = schema_loader.load_schema()
        cls.path = Path(cls.tmp.name) / "ontology.sqlite"
        portal = StaticPortal([po("public.app_purchase_order", {"id": "p1"},
                                  {"id": "p1", "po_number": "PO100234"})])
        now = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)
        cls.now = now
        with MailCaptures(cls.state, internal_domains=SCOPE.internal_domains) as mail:
            build_store(cls.path, cls.compiled, {"portal_api": portal, "graph_mail": mail},
                        clock=lambda: now, post_project={"text_index": store_text_index})

    @classmethod
    def tearDownClass(cls):
        cls.capture.close()
        cls.tmp.cleanup()

    def chunks(self, excl=None, path=None):
        conn = open_ro(path or self.path)
        self.addCleanup(conn.close)
        counts = Counts()
        out = list(ProjectionChunker(conn, excl or exclusion(), counts).chunks())
        return out, counts

    def find_objects(self, policy, actor, query):
        """Objects ``plx.find`` admits by audience and markings (item gate open)."""
        from operating import ontology_tools as ot
        contracts = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text())
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        tools = ot.OntologyTools(reader, policy, item_gate=lambda a, h: True,
                                 schema=ot.SchemaView.from_compiled(self.compiled),
                                 grammar=None, contracts=contracts, clock=lambda: self.now)
        result = tools.call("plx.find", {"query": query}, actor)
        self.assertNotIn("error", result)
        return {c["object"]["object_id"] for c in result["candidates"]}

    @staticmethod
    def prefilter(chunks, policy, actor):
        """The contract's SQL rule: audience_key granted AND markings <@ held."""
        held = policy.held_markings(actor)
        return [c for c in chunks if policy.may_read(actor, c.audience_key)
                and set(c.markings) <= held]

    def test_chunk_keys_are_the_projection_keys_of_their_objects(self):
        chunks, counts = self.chunks()
        self.assertTrue(chunks)
        self.assertEqual(counts.skipped.get("audience_key_unknown", 0), 0)
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        for chunk in chunks:
            key = conn.execute("SELECT audience_key FROM objects WHERE object_id = ?",
                               (chunk.object_id,)).fetchone()[0]
            self.assertEqual(chunk.audience_key, key)
            self.assertTrue(valid_key(chunk.audience_key))
            self.assertTrue(chunk.source_handle.startswith("graph-mail://"))
        self.assertEqual({c.audience_key for c in chunks},
                         {f"mail:{self.BOX_A}", f"mail:{self.BOX_B}"})

    def test_a_reader_who_cannot_read_a_mailbox_sees_none_of_its_chunks(self):
        from operating import ontology_tools as ot
        chunks, _ = self.chunks()
        reader = Actor("plx", frozenset({"staff:orders"}), "restricted")
        policies = {
            "both": ot.ReadPolicy(tenant="plx", audiences={"mail:*": {"staff:orders"},
                                                           "api:*": {"staff:orders"}}),
            "only_a": ot.ReadPolicy(tenant="plx", audiences={
                f"mail:{self.BOX_A}": {"staff:orders"}, "api:*": {"staff:orders"}}),
            "family_minus_b": ot.ReadPolicy(tenant="plx", audiences={
                "mail:*": {"staff:orders"}, f"mail:{self.BOX_B}": set(),
                "api:*": {"staff:orders"}}),
            "none": ot.ReadPolicy(tenant="plx", audiences={"api:*": {"staff:orders"}}),
        }
        for name, policy in policies.items():
            with self.subTest(policy=name):
                visible = self.prefilter(chunks, policy, reader)
                found = self.find_objects(policy, reader, "lanolin reconciliation")
                with_phrase = {c.object_id for c in visible if "Lanolin reconciliation"
                               in c.body}
                self.assertEqual(with_phrase, found)
                if name != "both":
                    self.assertFalse([c for c in visible if self.BOX_B in c.audience_key])
                    self.assertFalse([c for c in visible if "finance copy" in c.body])

    def test_markings_on_the_text_are_carried_and_hide_the_chunk(self):
        from operating import ontology_tools as ot
        marked = Path(self.tmp.name) / "marked.sqlite"
        shutil.copy(self.path, marked)
        conn = sqlite3.connect(marked)
        conn.execute("UPDATE text_fields SET markings = '[\"finance\"]' WHERE object_id IN "
                     "(SELECT object_id FROM objects WHERE audience_key = ?)",
                     (f"mail:{self.BOX_B}",))
        conn.commit()
        conn.close()
        chunks, _ = self.chunks(path=marked)
        b = [c for c in chunks if c.audience_key == f"mail:{self.BOX_B}"]
        self.assertTrue(b)
        self.assertTrue(all("finance" in c.markings for c in b))
        policy = ot.ReadPolicy(tenant="plx", audiences={"mail:*": {"staff:orders"}},
                               markings={"finance": {"staff:finance"}})
        reader = Actor("plx", frozenset({"staff:orders"}), "restricted")
        self.assertFalse([c for c in self.prefilter(chunks, policy, reader)
                          if c.audience_key == f"mail:{self.BOX_B}"])

    def test_an_excluded_mailbox_is_never_chunked_and_its_unit_is_withheld(self):
        chunks, counts = self.chunks(excl=exclusion(excluded=(self.BOX_B,)))
        self.assertTrue(chunks)
        self.assertFalse([c for c in chunks if self.BOX_B in c.audience_key])
        self.assertIn(f"mail:{self.BOX_B}", counts.withheld)
        self.assertGreater(counts.skipped["people_exclusion:mail"], 0)
        # A guard that cannot decide chunks no mail at all.
        chunks, counts = self.chunks(excl=Exclusion(PeopleExclusion([])))
        self.assertEqual(chunks, [])
        self.assertGreater(counts.skipped["people_exclusion:mail"], 0)

    def test_mail_chunk_drops_quoted_history_and_states_its_context(self):
        chunks, _ = self.chunks()
        freight = [c for c in chunks if c.kind == "mail" and "Marigold" in c.body]
        self.assertEqual(len(freight), 1)
        chunk = freight[0]
        self.assertNotIn("QUOTEDSECRET", chunk.body)
        self.assertIn("Title: Re: Marigold freight", chunk.header)
        self.assertIn("Author: external (outside.example)", chunk.header)
        self.assertNotIn("supplier@", chunk.header)          # a role, never the address
        self.assertIn("Date: 2026-", chunk.header)
        packing = [c for c in chunks if c.kind == "mail" and "Packing list" in c.body][0]
        self.assertIn("PO PO100234", packing.header)
        self.assertIn("petra_po:PO100234", packing.linked_ids)
        self.assertIn("500 kg", packing.body)                # normalised spelling
        pages = [c for c in chunks if c.kind == "attachment"]
        self.assertTrue(pages)
        self.assertTrue(all("#attachment=" in c.source_handle and "&page=" in c.source_handle
                            for c in pages))
        page2 = [c for c in pages if "Lanolin" in c.body][0]
        self.assertTrue(page2.source_handle.endswith("&page=2"))
        self.assertIn("attached to email 'Packing for PO100234'", page2.header)


# --------------------------------------------------------------------------- fixture store

class Fixture:
    """The projection tables the chunkers read, with ``object_values`` as a table."""

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE objects (object_id TEXT PRIMARY KEY, object_type TEXT,
              binding_id TEXT, native_key TEXT, audience_key TEXT, first_recorded_at TEXT,
              missing_from_source_since TEXT);
            CREATE TABLE object_versions (object_id TEXT, l1_handle TEXT, digest TEXT,
              lineage TEXT, markings TEXT, recorded_at TEXT, superseded_at TEXT);
            CREATE TABLE text_fields (field_id INTEGER PRIMARY KEY, object_id TEXT,
              version_handle TEXT, citation TEXT, field_path TEXT, document_kind TEXT,
              sender_class TEXT, markings TEXT, text_digest TEXT);
            CREATE TABLE text_chunks (chunk_id INTEGER PRIMARY KEY, field_id INTEGER,
              start INTEGER, body BLOB);
            CREATE TABLE object_values (object_id TEXT, property TEXT, value TEXT,
              markings TEXT, superseded_at TEXT, l1_handle TEXT, field_path TEXT,
              value_type TEXT);
            CREATE TABLE link_rows (link_id TEXT, link_type TEXT, from_object TEXT,
              to_object TEXT, tier TEXT, binding_kind TEXT);
            CREATE TABLE identifiers (identifier_kind TEXT, normalized TEXT, raw TEXT,
              role TEXT, object_id TEXT, markings TEXT, superseded_at TEXT);
        """)
        self.n = 0

    def obj(self, oid, otype, audience, *, handle=None, marks="[]", values=None, text=None,
            kind=None, missing=None, idents=()):
        handle = handle or f"fx://{oid}@1"
        self.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                          (oid, otype, "b", "{}", audience, "2026-01-01", missing))
        self.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,NULL)",
                          (oid, handle, "d", "mail", marks, "2026-01-01T00:00:00Z"))
        for prop, (value, vmarks) in (values or {}).items():
            self.conn.execute("INSERT INTO object_values VALUES (?,?,?,?,NULL,?,?,?)",
                              (oid, prop, value, vmarks, handle, f"row.{prop}", "text"))
        for path, (citation, body) in (text or {}).items():
            self.n += 1
            self.conn.execute("INSERT INTO text_fields VALUES (?,?,?,?,?,?,?,?,?)",
                              (self.n, oid, handle, citation, path, kind, "unknown", marks, "x"))
            self.conn.execute("INSERT INTO text_chunks (field_id, start, body) VALUES (?,?,?)",
                              (self.n, 0, zlib.compress(body.encode())))
        for k, v, role, m in idents:
            self.conn.execute("INSERT INTO identifiers VALUES (?,?,?,?,?,?,NULL)",
                              (k, v, v, role, oid, m))
        return handle

    def link(self, a, b, link_type, binding="fk"):
        self.conn.execute("INSERT INTO link_rows VALUES (?,?,?,?,?,?)",
                          (f"{a}>{b}", link_type, a, b, "verified", binding))

    def done(self):
        self.conn.commit()
        self.conn.close()


class FixtureChunkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fx.sqlite"
        self.fx = Fixture(self.path)

    def run_chunker(self, excl=None, kinds=None, records=False):
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        counts = Counts()
        if records:
            return list(RecordChunker(conn, excl or exclusion(), counts).record_chunks()), counts
        return list(ProjectionChunker(conn, excl or exclusion(), counts).chunks(kinds)), counts

    def teams_message(self, chat, mid, created, text, who, reply_to=None):
        oid = f"TeamsChatMessage:{chat}/{mid}"
        frm = json.dumps({"display_name": "Name Hidden", "id": who, "kind": "user"})
        handle = f"teams-chat://{chat}/{mid}@1"
        self.fx.obj(oid, "TeamsChatMessage", f"teams:chat:{chat}", handle=handle,
                    kind="teams_message", text={"row.body_text": (handle, text)},
                    values={"from": (frm, "[]"), "created_at": (created, "[]"),
                            "chat_id": (chat, "[]"), "reply_to_id": (reply_to, "[]"),
                            "id": (mid, "[]")})
        self.fx.link(oid, f"TeamsChat:{chat}", "TeamsChatMessage.in_chat")
        return oid

    def test_teams_message_carries_a_window_of_the_next_messages_and_drops_excluded(self):
        chat = "19:chat-one@thread.v2"
        self.fx.obj(f"TeamsChat:{chat}", "TeamsChat", f"teams:chat:{chat}",
                    values={"topic": ("Screen print delays", "[]")})
        self.teams_message(chat, "1", "2026-10-01T09:00:00Z",
                           "Is the screen print order for PO5280 still late?", STAFF)
        self.teams_message(chat, "2", "2026-10-01T09:05:00Z",
                           "Yes, supplier says Friday now for the cartons.", STAFF)
        self.teams_message(chat, "3", "2026-10-01T09:06:00Z",
                           "EXCLUDEDPERSONTEXT should never be chunked anywhere.", EXCLUDED)
        other = "19:chat-unknown@thread.v2"
        self.teams_message(other, "9", "2026-10-01T10:00:00Z",
                           "A chat whose members the capture never recorded.", STAFF)
        excl = exclusion(chats={chat: (STAFF,)})
        chunks, counts = self.run_chunker(excl)
        self.assertEqual(len(chunks), 2)
        first = [c for c in chunks if "PO5280" in c.body][0]
        self.assertIn("Reply (Teams user): Yes, supplier says Friday", first.body)
        self.assertIn("Title: Screen print delays", first.header)
        for chunk in chunks:
            self.assertNotIn("EXCLUDEDPERSONTEXT", chunk.body)
            self.assertNotIn("Name Hidden", chunk.header + chunk.body)
            self.assertEqual(chunk.audience_key, f"teams:chat:{chat}")
            self.assertEqual(chunk.authority, 1)
        self.assertEqual(counts.skipped["people_exclusion:sender"], 1)
        self.assertIn(f"teams:chat:{other}", counts.withheld)

    def test_mc_task_and_unknown_or_missing_keys(self):
        self.fx.obj("MCTask:T-1", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
                    handle="mc://live/tasks/T-1@1",
                    text={"row.description": ("mc://live/tasks/T-1@1",
                                              "Chase the supplier about AO-26197B labels.")},
                    values={"title": ("Label chase", "[]"), "stage": ("doing", "[]"),
                            "assignee": ("Somebody Named", '["pii"]')})
        self.fx.obj("MCTask:T-2", "MCTask", "mc:*", kind="mc_task",
                    text={"row.description": ("mc://t2", "A wildcard key is never a unit.")})
        self.fx.obj("MCTask:T-3", "MCTask", "nosuch:unit", kind="mc_task",
                    text={"row.description": ("mc://t3", "An unknown family is no key either.")})
        self.fx.obj("MCTask:T-4", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
                    marks='["secret_marking"]',
                    text={"row.description": ("mc://t4", "A marking the store does not know.")})
        self.fx.obj("MCTask:T-5", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
                    missing="2026-02-01",
                    text={"row.description": ("mc://t5", "Gone from the source, not searchable.")})
        chunks, counts = self.run_chunker()
        self.assertEqual(len(chunks), 1)
        task = chunks[0]
        self.assertEqual((task.kind, task.audience_key), ("mc_task", "mc:live/tasks/PRJ-X"))
        self.assertIn("AO AO-26197B", task.header)
        self.assertIn("stage doing", task.header)
        self.assertNotIn("Somebody Named", task.header)      # a pii value never in a header
        self.assertEqual(counts.skipped["audience_key_unknown"], 2)
        self.assertEqual(counts.skipped["markings_unknown"], 1)
        self.assertEqual(counts.skipped["missing_from_source"], 1)

    def test_sharepoint_sheet_rows_repeat_headers_and_onedrive_owner_is_decided(self):
        sheet = ("[Sheet: Pricing]\nA1=SKU | B1=Description | C1=Price CAD\n"
                 + "\n".join(f"A{i}=SKU-{i} | B{i}=Lavender soap bar {i} | C{i}={i}.50"
                             for i in range(2, 80)))
        self.fx.obj("SharePointFile:f1", "SharePointFile", "sharepoint:drive-site",
                    kind="sharepoint_file", handle="sharepoint://drive-site/f1@1",
                    text={"row.content_text": ("sharepoint://drive-site/f1@1", sheet)},
                    values={"name": ("pricing.xlsx", "[]")})
        self.fx.obj("SharePointFile:f2", "SharePointFile", "sharepoint:drive-mine",
                    kind="sharepoint_file", handle="sharepoint://drive-mine/f2@1",
                    text={"row.content_text": ("sharepoint://drive-mine/f2@1",
                                               "# Notes\n\nA private OneDrive note.")})
        self.fx.obj("SharePointFile:f3", "SharePointFile", "sharepoint:drive-unknown",
                    kind="sharepoint_file",
                    text={"row.content_text": ("sharepoint://u/f3@1", "# Title\n\nUnknown drive")})
        excl = exclusion(directory={"excluded.person@example-plx.com": EXCLUDED},
                         drives={"drive-site": "", "drive-mine": "excluded_person_example-plx_com"})
        chunks, counts = self.run_chunker(excl)
        self.assertTrue(chunks)
        self.assertEqual({c.kind for c in chunks}, {"sheet_row"})
        for chunk in chunks:
            self.assertTrue(chunk.body.startswith("Columns: SKU | Description | Price CAD"))
            self.assertIn("Sheet 'Pricing' rows", chunk.header)
            self.assertEqual(chunk.authority, 3)
        rows = "\n".join(c.body for c in chunks)
        for i in range(2, 80):
            self.assertEqual(rows.count(f"SKU: SKU-{i};"), 1)      # no row lost or doubled
        self.assertEqual(counts.withheld, {"sharepoint:drive-mine", "sharepoint:drive-unknown"})

    def test_document_sections_and_slides(self):
        doc = ("# Receiving SOP\n\nIntro.\n\n## Stage 1: Inspect\n\nCheck the cartons for damage "
               "and count the pallets against the packing slip.\n\n## Stage 2: Record\n\n"
               "Enter the receipt in BC the same day with the lot number and expiry.")
        deck = ("<!-- Slide number: 1 -->\n# Q4 plan\nLaunch the oat soap line in November "
                "with the new cartons.\n\n<!-- Slide number: 2 -->\n# Risks\nScreen print "
                "supplier capacity is short for the holiday run.")
        self.fx.obj("SharePointFile:sop", "SharePointFile", "sharepoint:drive-site",
                    kind="sharepoint_file",
                    text={"row.content_text": ("sharepoint://s/sop@1", doc)},
                    values={"name": ("Receiving process map.docx", "[]")})
        self.fx.obj("SharePointFile:deck", "SharePointFile", "sharepoint:drive-site",
                    kind="sharepoint_file",
                    text={"row.content_text": ("sharepoint://s/deck@1", deck)},
                    values={"name": ("plan.pptx", "[]")})
        chunks, _ = self.run_chunker(exclusion(drives={"drive-site": ""}))
        stages = [c for c in chunks if c.kind == "map_stage"]
        self.assertEqual(len(stages), 2)       # the one-line intro joins stage 1
        self.assertTrue(any("Receiving SOP > Stage 2: Record" in c.header for c in stages))
        self.assertTrue(any("Intro." in c.body and "Stage 1" in c.body for c in stages))
        slides = [c for c in chunks if c.kind == "sharepoint"]
        self.assertEqual(len(slides), 2)
        self.assertTrue(any("Slide 2" in c.header and "capacity" in c.body for c in slides))

    def test_record_free_text_is_keyed_by_value_and_version_markings(self):
        self.fx.obj("RelSageArCmm:1", "RelSageArCmm", "api:sage.ar_cmm", marks='["finance"]',
                    values={"textcmnt1": ("Customer asked to hold shipments until the credit "
                                          "note is issued.", "[]"),
                            "textcmnt2": ("12345 67890", "[]")},
                    idents=[("customer_erp_code", "C0012", "primary", "[]")])
        chunks, counts = self.run_chunker(records=True)
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual((chunk.kind, chunk.audience_key, chunk.markings),
                         ("record_text", "api:sage.ar_cmm", ("finance",)))
        self.assertIn("Customer C0012", chunk.header)
        self.assertEqual(chunk.authority, 4)

    def test_entities_name_alias_fold_and_marked_identifiers_left_out(self):
        self.fx.obj("Customer:1", "Customer", "api:public.app_customer",
                    values={"name": ("Savon Québec Inc.", "[]")},
                    idents=[("customer_erp_code", "SAVQ01", "primary", "[]"),
                            ("customer_erp_code", "FIN-77", "alias", '["finance"]')])
        self.fx.obj("Contact:1", "Contact", "bc:co/contacts",
                    values={"display_name": ("Person Name", '["pii"]'),
                            "email": ("buyer@customer.example", '["pii"]')})
        self.fx.obj("Contact:excluded", "Contact", "bc:co/contacts",
                    values={"display_name": ("Excluded Person", '["pii"]'),
                            "email": ("gone@example-plx.com", '["pii"]')})
        self.fx.obj("Contact:noaddress", "Contact", "bc:co/contacts",
                    values={"display_name": ("No Address", '["pii"]')})
        self.fx.obj("PurchaseOrder:1", "PurchaseOrder", "api:public.app_purchase_order",
                    idents=[("petra_po", "PO5280", "primary", "[]")])
        self.fx.obj("Customer:bad", "Customer", "api:*", values={"name": ("Nope", "[]")})
        self.fx.obj("CustomerLocation:1", "CustomerLocation", "api:public.app_customer_location",
                    values={"label": ("Savon Quebec - Montreal plant", "[]"),
                            "country": ("CA", "[]"), "city": ("PRIVATECITY", '["pii"]')})
        self.fx.obj("Product:1", "Product", "bc:co/items",
                    values={"name": ("Oat soap bar 100g", "[]"),
                            "item_category": ("SOAP", "[]")})
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        counts = Counts()
        excl = exclusion(directory={"gone@example-plx.com": EXCLUDED})
        found = {e.object_id: e for e in entities(conn, counts, exclusion=excl)}
        self.assertNotIn("Contact:excluded", found)
        self.assertNotIn("Contact:noaddress", found)          # undecidable: withheld
        self.assertEqual(counts.skipped["people_exclusion:person"], 1)
        self.assertNotIn("Contact:1", {e.object_id for e in entities(conn, Counts())})
        customer = found["Customer:1"]
        self.assertEqual(customer.normalized, "savon quebec inc")
        self.assertIn("savon quebec", customer.alias)
        self.assertIn("SAVQ01", customer.alias)
        self.assertNotIn("FIN-77", " ".join(customer.alias))
        self.assertEqual(customer.markings, ())
        self.assertEqual(found["Contact:1"].markings, ("pii",))
        self.assertEqual(found["PurchaseOrder:1"].name, "PO PO5280")
        self.assertNotIn("Customer:bad", found)
        self.assertEqual(fold("G2  Beauty & Co."), "g2 beauty and co")
        # Attribute words from the record's own values; a pii value never an alias.
        place = found["CustomerLocation:1"]
        self.assertIn("canada canadian", place.alias)
        self.assertNotIn("PRIVATECITY", " ".join(place.alias))
        self.assertEqual(place.markings, ())
        self.assertIn("SOAP", found["Product:1"].alias)

    def email(self, oid, box, text, *, sender="supplier@outside.example", marks="[]"):
        handle = f"graph-mail://{box}/{oid}@1"
        values = {"subject": ("Order status", "[]")}
        if sender is not None:
            values["sender"] = (sender, "[]")
        self.fx.obj(oid, "Email", f"mail:{box}", handle=handle, kind="email", marks=marks,
                    text={"row.body_text": (handle, text)}, values=values)
        return oid

    def test_cross_unit_links_state_only_ids_the_text_names_and_never_labels(self):
        box = "12345678-aaaa-bbbb-cccc-0000000000aa"
        self.email("Email:1", box, "Where is SO OR1000154203? The client is waiting.")
        # Another unit (Sage): an invoice the mail is linked to, with a customer code and a
        # name the text never states.
        self.fx.obj("SalesInvoice:9", "SalesInvoice", "api:sage.oe_invoice_header",
                    values={"name": ("Hidden Customer Ltd", "[]")},
                    idents=[("customer_erp_code", "A1TEXT11", "primary", "[]"),
                            ("so_number", "OR1000154203", "alias", "[]")])
        self.fx.link("Email:1", "SalesInvoice:9", "Email.mentions_sales_invoice", "mention")
        # Same unit: a linked mail whose version carries a marking the chunk does not.
        self.email("Email:2", box, "Marked twin message body that is long enough.",
                   marks='["finance"]')
        self.fx.conn.execute("INSERT INTO identifiers VALUES ('petra_po','PO7777','PO7777',"
                             "'primary','Email:2','[]',NULL)")
        self.fx.link("Email:1", "Email:2", "Email.same_thread")
        # Same unit, readable: its id and name may be stated.
        self.fx.obj("Thread:3", "Thread", f"mail:{box}", values={"name": ("Cartons thread", "[]")},
                    idents=[("petra_po", "PO5555", "primary", "[]")])
        self.fx.link("Email:1", "Thread:3", "Email.in_thread")
        chunks, _ = self.run_chunker(exclusion())
        first = [c for c in chunks if c.object_id == "Email:1"][0]
        self.assertIn("SO OR1000154203", first.header)
        self.assertNotIn("A1TEXT11", first.header + " ".join(first.linked_ids))
        self.assertNotIn("Hidden Customer", first.header)
        self.assertNotIn("PO7777", first.header + " ".join(first.linked_ids))
        self.assertIn("PO PO5555", first.header)
        self.assertIn("Cartons thread", first.header)

    def test_ids_match_on_token_boundaries_and_attachments_follow_their_sender(self):
        from operating.chunking import headers as hd
        self.assertTrue(hd.label_allowed("PO12", "see po12, thanks", False))
        self.assertFalse(hd.label_allowed("PO12", "see PO123 and XPO12", False))
        box = "12345678-aaaa-bbbb-cccc-0000000000cc"
        self.email("Email:ex", box, "Excluded sender mail body, never chunked here.",
                   sender="gone@example-plx.com")
        handle = f"graph-mail://{box}/Email:ex@1#attachment=A1"
        self.fx.obj("Attachment:A1", "Attachment", f"mail:{box}", handle=handle,
                    kind="attachment", text={"row.content_text": (handle + "&page=1",
                                             "Attachment text of an excluded sender's mail.")},
                    values={"name": ("x.pdf", "[]")})
        self.fx.link("Attachment:A1", "Email:ex", "Attachment.attached_to")
        excl = exclusion(directory={"gone@example-plx.com": EXCLUDED})
        chunks, counts = self.run_chunker(excl)
        self.assertEqual(chunks, [])
        self.assertEqual(counts.skipped["people_exclusion:sender"], 1)
        self.assertEqual(counts.skipped["people_exclusion:attachment_sender"], 1)
        from operating.chunking.projection import withheld_messages
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(withheld_messages(conn, excl), {"Email:ex", "Attachment:A1"})

    def test_latest_version_tie_takes_every_tied_marking_and_sender_fails_closed(self):
        box = "12345678-aaaa-bbbb-cccc-0000000000bb"
        self.email("Email:t", box, "A message whose versions tie on their recorded time.")
        self.fx.conn.execute("INSERT INTO object_versions VALUES ('Email:t','graph-mail://x@2',"
                             "'d','mail','[\"finance\"]','2026-01-01T00:00:00Z',"
                             "'2026-01-01T00:00:00Z')")
        self.email("Email:nosender", box, "A message with no sender stated anywhere at all.",
                   sender=None)
        chunks, counts = self.run_chunker(exclusion())
        self.assertEqual([c.object_id for c in chunks], ["Email:t"])
        self.assertEqual(chunks[0].markings, ("finance",))
        self.assertEqual(counts.skipped["people_exclusion:sender_undecided"], 1)


class TextUnitTests(unittest.TestCase):
    def test_normalise_spells_units_currencies_dates_without_changing_values(self):
        out = normalise("Need 500kgs and 20 Lbs. at C$1,200 = 12 pct by 25/10/2026 not "
                        "10/02/2026; 12 Oct 2026.")
        self.assertIn("500 kg", out)
        self.assertIn("20 lb.", out)
        self.assertIn("CAD 1,200", out)
        self.assertIn("12%", out)
        self.assertIn("25/10/2026 (2026-10-25)", out)
        self.assertIn("10/02/2026;", out)                 # ambiguous: left as written
        self.assertIn("12 Oct 2026 (2026-10-12)", out)

    def test_control_characters_never_reach_a_row(self):
        from operating.chunking import make_chunk
        chunk = make_chunk(kind="mail", parts=("k",), source_handle="fx://h", object_id=None,
                           title="T\x00itle", author_role=None, occurred_at=None,
                           header="Source: x\x01", body="Body\x00 with NUL\tand tab\nline",
                           linked_ids=(), audience_key="mail:x", markings=())
        self.assertEqual(chunk.body, "Body with NUL\tand tab\nline")
        self.assertEqual((chunk.title, chunk.header), ("Title", "Source: x"))

    def test_pack_never_loses_text_and_keeps_table_headers(self):
        table = "| SKU | Qty |\n| --- | --- |\n" + "\n".join(f"| S{i} | {i} |" for i in range(400))
        pieces = split.pack(table, 300)
        self.assertGreater(len(pieces), 5)
        self.assertTrue(all(p.startswith("| SKU | Qty |") and len(p) <= 300 for p in pieces))
        joined = "\n".join(pieces)
        self.assertTrue(all(f"| S{i} | {i} |" in joined for i in range(400)))
        prose = " ".join(f"Sentence number {i} is here." for i in range(500))
        pieces = split.pack(prose, 400)
        self.assertTrue(all(len(p) <= 400 for p in pieces))
        self.assertEqual("".join(" ".join(pieces).split()), "".join(prose.split()))

    def test_mail_forward_keeps_the_forwarded_message(self):
        own, forwarded = split.mail_own_text(
            "FYI\r\n\r\n---------- Forwarded message ---------\r\nFrom: a@b.example\r\n"
            "The carton supplier confirms shipment on Friday.")
        self.assertTrue(forwarded)
        self.assertIn("carton supplier confirms", own)
        own, forwarded = split.mail_own_text(
            "FYI\r\n\r\n---------- Forwarded message ---------\r\nFrom: a@b.example\r\n"
            "Subject: Cartons\r\n\r\nThe carton supplier confirms shipment on Friday."
            "\r\n\r\nOn Sun, Sep 27, 2026 at 9:00 AM Old wrote:\r\n> OLDHISTORY pricing\r\n")
        self.assertTrue(forwarded)
        self.assertIn("Subject: Cartons", own)
        self.assertNotIn("OLDHISTORY", own)
        self.assertNotIn("a@b.example", own)

    def test_duplicates_exact_and_near_cluster_without_merging_rows(self):
        base = ("The shipment of PO 5280 for the screen printed soap boxes is delayed because "
                "the supplier missed the ink delivery and will ship on Friday by truck. ") * 3
        rows = [("a", dd.body_sha(base), base), ("b", dd.body_sha(base), base),
                ("c", dd.body_sha(base + " Thanks."), base + " Thanks."),
                ("d", dd.body_sha("unrelated " * 40), "unrelated text about lavender " * 10)]
        groups = dd.groups(rows)
        self.assertEqual(groups["a"], groups["b"])
        self.assertEqual(groups["a"], groups["c"])
        self.assertNotIn("d", groups)
        self.assertGreater(dd.near(base, base + " Thanks."), 0.85)


# --------------------------------------------------------------------------- providers

class ProviderTests(unittest.TestCase):
    def test_remote_is_refused_without_the_switch_or_key(self):
        from operating.embed import models as md
        for values in ({"PLX_EMBED_MODEL": "openai:text-embedding-3-large"},
                       {"PLX_EMBED_MODEL": "openai:text-embedding-3-large",
                        "PLX_EMBED_ALLOW_REMOTE": "1"},
                       {"PLX_EMBED_MODEL": "openai:text-embedding-3-large",
                        "PLX_EMBED_ALLOW_REMOTE": "yes", "OPENAI_API_KEY": "k"}):
            with self.assertRaises(md.RemoteRefused):
                md.load(values, post=lambda *a: self.fail("sent"))
        with self.assertRaises(ValueError):
            md.load({"PLX_EMBED_MODEL": "someone/else"})

    def test_openai_batches_retries_and_shortens_to_the_column(self):
        from operating import embed_sender
        from operating.embed import models as md
        calls, slept = [], []

        def post(url, headers, body, timeout):
            payload = json.loads(body)
            calls.append(payload)
            if len(calls) == 1:
                raise embed_sender.TransportError("http_429", 429, 1.5)
            data = [{"index": i, "embedding": [1.0] + [0.0] * (payload["dimensions"] - 1)}
                    for i in range(len(payload["input"]))]
            return json.dumps({"data": data}).encode()

        values = {"PLX_EMBED_MODEL": "openai:text-embedding-3-large",
                  "PLX_EMBED_ALLOW_REMOTE": "1", "OPENAI_API_KEY": "test-key"}
        with mock.patch("time.sleep", lambda s: slept.append(s)):
            embedder = md.load(values, column_dim=1024, post=post)
            vectors = embedder.encode([f"text {i}" for i in range(md.OPENAI_MAX_INPUTS + 5)])
        self.assertEqual(vectors.shape, (md.OPENAI_MAX_INPUTS + 5, 1024))
        self.assertEqual(slept, [1.5])
        sizes = [len(c["input"]) for c in calls[1:]]
        self.assertEqual(sizes, [md.OPENAI_MAX_INPUTS, 5])
        self.assertTrue(all(c["dimensions"] == 1024 for c in calls))
        self.assertEqual(embedder.name, "openai:text-embedding-3-large@1024")
        self.assertNotIn("test-key", repr(embedder))

    def test_sender_reaches_its_one_url_and_the_egress_gate_pins_it(self):
        from operating import embed_sender
        with mock.patch("urllib.request.OpenerDirector.open") as opened:
            for url in ("https://api.openai.com/v1/embeddings?x=1",
                        "http://api.openai.com/v1/embeddings",
                        "https://api.openai.com/v1/files",
                        "https://graph.microsoft.com/v1.0/me/sendMail"):
                with self.assertRaises(embed_sender.TransportError, msg=url):
                    embed_sender.post_embeddings(url, {}, b"{}", 1)
            opened.assert_not_called()
        from scripts import check_http_egress as egress
        source = (ROOT / "operating/embed_sender.py").read_text()
        rules = lambda src: {r for _, _, r in egress.scan_source(  # noqa: E731
            src, "operating/embed_sender.py")}
        self.assertEqual(rules(source), set())
        self.assertIn("non-get-request", rules(source.replace("def post_embeddings(",
                                                              "def post_other(")))
        self.assertIn("unlisted-url", rules(source.replace(
            "https://api.openai.com/v1/embeddings", "https://api.openai.com/v1/files")))


# --------------------------------------------------------------------------- postgres

def _pg_available() -> bool:
    return shutil.which("createdb") is not None and shutil.which("psql") is not None


CONTRACT_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA search;
CREATE TABLE search.chunks (chunk_id text PRIMARY KEY, source_handle text NOT NULL,
  object_id text, kind text NOT NULL, title text, author_role text, occurred_at timestamptz,
  header text NOT NULL, body text NOT NULL, content_sha text NOT NULL,
  linked_ids text[] NOT NULL DEFAULT '{}', audience_key text NOT NULL,
  markings text[] NOT NULL DEFAULT '{}', dup_group text, authority smallint NOT NULL,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', header || ' ' || body)) STORED,
  embedding halfvec(8), embed_model text, updated_at timestamptz NOT NULL);
CREATE TABLE search.entities (entity_id text PRIMARY KEY, object_id text, type text,
  name text, alias text[], normalized text, audience_key text, markings text[],
  name_embedding halfvec(8));
"""


@unittest.skipUnless(_pg_available(), "no local Postgres")
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = f"plx_test_b_{os.getpid()}"
        made = subprocess.run(["createdb", cls.db], capture_output=True, text=True)
        if made.returncode != 0:
            raise unittest.SkipTest("createdb failed")
        migrations = sorted((ROOT / "operating/pg/migrations").glob("*.sql")) \
            if (ROOT / "operating/pg/migrations").is_dir() else []
        if migrations and (ROOT / "operating/pg/__main__.py").is_file():
            env = {**os.environ, "PLX_PG_DSN": f"postgresql:///{cls.db}"}
            done = subprocess.run([sys.executable, "-m", "operating.pg", "migrate"], cwd=ROOT,
                                  env=env, capture_output=True, text=True)
            if done.returncode != 0:
                raise unittest.SkipTest("Lane A migration failed")
        else:
            subprocess.run(["psql", "-q", "-d", cls.db, "-c", CONTRACT_DDL], check=True,
                           capture_output=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["dropdb", "--if-exists", cls.db], capture_output=True)

    def setUp(self):
        from operating.embed import store as st
        self.st = st
        self.conn = st.connect(f"postgresql:///{self.db}")
        self.addCleanup(self.conn.close)
        self.conn.execute("TRUNCATE search.chunks, search.entities")
        if self.conn.execute("SELECT to_regclass('search.audience_units')").fetchone()[0]:
            self.conn.execute("TRUNCATE search.audience_units")
        self.conn.commit()

    def chunk(self, n, body, audience="mail:box-a", kind="mail"):
        from operating.chunking import make_chunk
        return make_chunk(kind=kind, parts=(audience, f"h{n}", "0"), source_handle=f"fx://h{n}",
                          object_id=f"O{n}", title=f"T{n}", author_role=None,
                          occurred_at="2026-10-01T00:00:00Z", header=f"Source: x\nTitle: T{n}",
                          body=body, linked_ids=[], audience_key=audience, markings=())

    def embedder(self):
        from operating.embed.models import Embedder
        seen = []

        def encode(texts):
            seen.extend(texts)
            return np.ones((len(texts), 4))
        return Embedder("fake-4", 4, encode), seen

    def test_incremental_reembeds_only_changed_and_equal_contents_once(self):
        from operating.embed.__main__ import embed
        chunks = [self.chunk(i, f"Body number {i} about soap orders.") for i in range(10)]
        chunks.append(self.chunk(99, "Body number 0 about soap orders.", audience="mail:box-b"))
        chunks[-1] = chunks[-1].__class__(**{**chunks[-1].__dict__,
                                             "header": chunks[0].header})
        writer = self.st.ChunkWriter(self.conn)
        writer.write(chunks)
        self.assertEqual(writer.inserted, 11)
        fake, seen = self.embedder()
        report = embed(self.conn, {}, kinds=None, limit=None, embedder=fake)
        self.assertEqual(report["texts_embedded"], 10)          # equal content once
        self.assertEqual(report["rows_updated"], 11)
        # Unchanged re-run: nothing rewritten, every vector kept, nothing re-embedded.
        writer = self.st.ChunkWriter(self.conn)
        writer.write(chunks)
        self.assertEqual((writer.inserted, writer.updated, writer.unchanged), (0, 0, 11))
        seen.clear()
        report = embed(self.conn, {}, kinds=None, limit=None, embedder=fake)
        self.assertEqual((report["texts_embedded"], seen), (0, []))
        # One body changes: that row alone loses its vector and is re-embedded.
        chunks[3] = self.chunk(3, "Body number 3 changed: the order shipped.")
        writer = self.st.ChunkWriter(self.conn)
        writer.write(chunks)
        self.assertEqual((writer.updated, writer.unchanged), (1, 10))
        report = embed(self.conn, {}, kinds=None, limit=None, embedder=fake)
        self.assertEqual(report["texts_embedded"], 1)
        self.assertEqual(len(seen), 1)
        self.assertIn("order shipped", seen[0])
        dims = self.conn.execute("SELECT count(*) FROM search.chunks WHERE embedding IS NULL"
                                 ).fetchone()[0]
        self.assertEqual(dims, 0)
        # A key change alone rewrites the row but keeps its vector.
        chunks[4] = chunks[4].__class__(**{**chunks[4].__dict__, "markings": ("finance",)})
        writer = self.st.ChunkWriter(self.conn)
        writer.write(chunks)
        self.assertEqual(writer.updated, 1)
        row = self.conn.execute("SELECT markings, embedding IS NOT NULL FROM search.chunks "
                                "WHERE chunk_id = %s", (chunks[4].chunk_id,)).fetchone()
        self.assertEqual(row, (["finance"], True))

    def test_stale_rows_withheld_units_shrink_guard_and_dedup(self):
        from operating.embed.__main__ import dedup
        body = "The cartons for PO 5280 ship Friday by truck from the Toronto plant. " * 4
        chunks = [self.chunk(i, f"Distinct body {i} about lavender soap.") for i in range(6)]
        chunks += [self.chunk(50, body, audience="mail:box-b"), self.chunk(51, body)]
        writer = self.st.ChunkWriter(self.conn)
        writer.write(chunks)
        report = dedup(self.conn, None)
        self.assertEqual((report["chunks_in_groups"], report["groups"]), (2, 1))
        groups = self.conn.execute("SELECT count(DISTINCT chunk_id) FROM search.chunks WHERE "
                                   "dup_group IS NOT NULL").fetchone()[0]
        self.assertEqual(groups, 2)
        self.assertEqual(writer.delete_units({"mail:box-b"}), 1)
        writer = self.st.ChunkWriter(self.conn)
        writer.write(chunks[:2])
        refused = writer.delete_stale(["mail"])
        self.assertEqual(refused["mail"]["refused"], "shrink_guard")
        writer = self.st.ChunkWriter(self.conn)
        writer.write(chunks[:6])
        self.assertEqual(writer.delete_stale(["mail"]), {"mail": {"deleted": 1}})

    def test_units_record_who_a_unit_belongs_to_before_its_chunks(self):
        from operating.chunking.permissions import unit_row
        excl = exclusion(directory={"a@example-plx.com": STAFF},
                         drives={"d-site": "", "d-mine": "a_example-plx_com"},
                         chats={"19:c@thread.v2": ("a@example-plx.com", EXCLUDED)})
        excl.drive_sites = {"d-site": "Purchasing"}
        keys = ["mail:" + STAFF, "teams:chat:19:c@thread.v2", "sharepoint:d-site",
                "sharepoint:d-mine", "api:public.app_customer", "bc:co-1/vendors",
                "mc:live/tasks/PRJ-X", "teams:channel:t/c", "repo:o/n"]
        rows = {k: unit_row(k, excl) for k in keys}
        self.assertEqual(rows[keys[0]]["people"], [STAFF])
        self.assertEqual(rows[keys[1]]["people"], sorted([STAFF, EXCLUDED]))
        self.assertEqual((rows[keys[2]]["sharing"], rows[keys[2]]["label"]),
                         ("shared", "Purchasing"))
        self.assertEqual((rows[keys[3]]["sharing"], rows[keys[3]]["people"]),
                         ("personal", [STAFF]))
        for key in keys[4:8]:
            self.assertEqual(rows[key]["sharing"], "shared", key)
            self.assertTrue(rows[key]["label"])
        for key in keys[:2] + [keys[3], keys[8]]:
            self.assertEqual((rows[key]["sharing"], rows[key]["label"]), ("personal", None))
        # The unit's row is in the database before (and with) its first chunk.
        writer = self.st.ChunkWriter(self.conn, unit_of=lambda k: unit_row(k, excl))
        writer.write([self.chunk(1, "A chat message about the late cartons order.",
                                 audience="teams:chat:19:c@thread.v2")])
        row = self.conn.execute("SELECT sharing, people FROM search.audience_units WHERE "
                                "audience_key = %s", ("teams:chat:19:c@thread.v2",)).fetchone()
        self.assertEqual(row, ("personal", sorted([STAFF, EXCLUDED])))
        self.assertEqual(writer.units_recorded, 1)
        writer.write([self.chunk(2, "Another message in that chat about pallets.",
                                 audience="teams:chat:19:c@thread.v2")])
        self.assertEqual(writer.units_recorded, 1)

    def test_purge_priority_order_and_bulk_index_rebuild(self):
        from operating.embed.__main__ import embed
        from operating.chunking.permissions import unit_row
        from operating.pg import bulk
        excl = exclusion(chats={"19:x@thread.v2": (STAFF, EXCLUDED)}, drives={"d": ""})
        writer = self.st.ChunkWriter(self.conn, unit_of=lambda k: unit_row(k, excl))
        writer.write([self.chunk(1, "Mail of an excluded person about pallets.",
                                 audience=f"mail:{EXCLUDED}"),
                      self.chunk(2, "A chat with an excluded member about cartons.",
                                 audience="teams:chat:19:x@thread.v2", kind="teams"),
                      self.chunk(3, "A staff mailbox message about lavender soap.",
                                 audience=f"mail:{STAFF}"),
                      self.chunk(4, "A stage of the receiving process map document.",
                                 audience="sharepoint:d", kind="map_stage")])
        report = self.st.purge_excluded(self.conn, excl)
        self.assertEqual(report, {"units": 2, "rows": 2})
        left = {r[0] for r in self.conn.execute("SELECT audience_key FROM search.chunks")}
        self.assertEqual(left, {f"mail:{STAFF}", "sharepoint:d"})
        order = self.st.pending(self.conn, "fake-4", order="priority")
        kinds = [self.conn.execute("SELECT kind FROM search.chunks WHERE content_sha = %s",
                                   (sha,)).fetchone()[0] for sha in order]
        self.assertEqual(kinds, ["map_stage", "mail"])
        fake, _ = self.embedder()
        result = embed(self.conn, {}, kinds=None, limit=None, embedder=fake,
                       order="priority", bulk=True)
        self.assertEqual(result["texts_embedded"], 2)
        self.assertIsNotNone(result["bulk_index_rebuild_seconds"])
        self.assertTrue(bulk.vector_index_present(self.conn))

    def test_bulk_drop_holds_no_lock_and_a_failed_load_still_rebuilds(self):
        import psycopg
        from operating.embed.__main__ import embed
        from operating.embed.models import Embedder
        from operating.pg import bulk
        writer = self.st.ChunkWriter(self.conn)
        writer.write([self.chunk(i, f"Body {i} about the carton order and pallets.")
                      for i in range(3)])
        self.st.pending(self.conn, "x")           # leaves a transaction open, as embed() did
        bulk.drop_vector_index(self.conn)
        with psycopg.connect(f"postgresql:///{self.db}") as other:
            other.execute("SET lock_timeout = '1s'")
            self.assertEqual(other.execute("SELECT count(*) FROM search.chunks").fetchone()[0], 3)
        bulk.rebuild_vector_index(self.conn)

        def broken(texts):
            raise RuntimeError("encoder_failed")
        with self.assertRaises(RuntimeError) as caught:
            embed(self.conn, {}, kinds=None, limit=None, embedder=Embedder("fake-4", 4, broken),
                  bulk=True)
        self.assertEqual(str(caught.exception), "encoder_failed")
        self.conn.rollback()
        self.assertTrue(bulk.vector_index_present(self.conn))

    def test_sender_purge_removes_an_excluded_senders_rows_in_any_unit(self):
        writer = self.st.ChunkWriter(self.conn)
        writer.write([self.chunk(1, "Kept message about lavender soap orders."),
                      self.chunk(2, "Excluded sender message in a colleague's mailbox.")])
        self.assertEqual(self.st.purge_objects(self.conn, {"O2", "O-not-stored"}), 1)
        left = [r[0] for r in self.conn.execute("SELECT object_id FROM search.chunks")]
        self.assertEqual(left, ["O1"])

    def test_vectors_are_padded_to_the_column_never_truncated(self):
        self.assertEqual(self.st.column_dim(self.conn) is not None, True)
        dim = self.st.column_dim(self.conn)
        text = self.st.vector_text(np.ones(2, dtype=np.float32), dim)
        self.assertEqual(text.count(",") + 1, dim)
        with self.assertRaises(ValueError):
            self.st.vector_text(np.ones(dim + 1, dtype=np.float32), dim)


if __name__ == "__main__":
    unittest.main()
