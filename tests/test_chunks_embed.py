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

import dataclasses
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
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

from operating.pg import scratch_db

scratch_db.use_test_cluster()   # the test cluster when one runs (bare runs too)

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = "0e0e0e0e-0000-4000-8000-00000000e0e1"
STAFF = "0f0f0f0f-0000-4000-8000-0000000000a1"


def exclusion(*, excluded=(EXCLUDED,), directory=None, drives=None, chats=None,
              domains=("example-plx.com",)) -> Exclusion:
    return Exclusion(PeopleExclusion(list(excluded)), directory=directory or {},
                     internal_domains=domains, drive_owners=drives or {},
                     chat_members=chats or {})


# --------------------------------------------------------------------------- real store

class MeetingsUnitTests(unittest.TestCase):
    def test_meetings_units_are_keyed_and_an_excluded_owner_is_withheld(self):
        from operating.chunking.permissions import unit_row
        excl = exclusion()
        for unit, clear in ((f"meetings:calendar:{STAFF}", True),
                            (f"meetings:organizer:{STAFF}", True),
                            (f"meetings:calendar:{EXCLUDED}", False),
                            (f"meetings:organizer:{EXCLUDED}", False),
                            ("meetings:plan:PlanOne", True), ("meetings:other:x", False)):
            with self.subTest(unit):
                self.assertTrue(valid_key(unit))
                self.assertEqual(excl.unit_clear(unit), clear)
        self.assertEqual(unit_row(f"meetings:calendar:{STAFF}", excl)["people"], [STAFF])
        self.assertFalse(exclusion(excluded=()).unit_clear(f"meetings:calendar:{STAFF}"))


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
        self.assertIn("Ids: PO100234", packing.header)
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
              role TEXT, object_id TEXT, markings TEXT, superseded_at TEXT,
              property TEXT);
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
        for k, v, role, m, *prop in idents:
            self.conn.execute("INSERT INTO identifiers VALUES (?,?,?,?,?,?,NULL,?)",
                              (k, v, v, role, oid, m, prop[0] if prop else "code"))
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

    def test_program_source_and_code_text_are_not_chunked_as_documents(self):
        code = "#include <vector>\nnamespace rs {\n" + "  int frame_count = 0;\n" * 14 + "}\n"
        for n, (name, text) in enumerate((
                ("types.hpp", "Plain words in a header file name it as source code."),
                ("notes.txt", code),
                ("spec.pdf", "Carton spec: 350 gsm board, matte varnish, and the code\n"
                             "int x = 0;\nis only quoted once in this document.\n" * 3),
                ("export.txt", "sku;qty;price;\n" + "SOAP-1;12;3.40;\n" * 14))):
            handle = f"sharepoint://drive-site/c{n}@1"
            self.fx.obj(f"SharePointFile:c{n}", "SharePointFile", "sharepoint:drive-site",
                        kind="sharepoint_file", handle=handle,
                        text={"row.content_text": (handle, text)},
                        values={"name": (name, "[]")})
        chunks, counts = self.run_chunker(exclusion(drives={"drive-site": ""}))
        self.assertEqual({c.object_id for c in chunks},
                         {"SharePointFile:c2", "SharePointFile:c3"})
        self.assertEqual((counts.skipped["not_a_document:program_file"],
                          counts.skipped["not_a_document:code_text"]), (1, 1))

    def test_a_card_or_bank_statement_is_marked_finance_and_pii_by_its_content(self):
        texts = {
            "s1": "Statement period Oct 1 - Oct 31. Previous balance 812.40. New balance "
                  "1,020.15. Minimum payment 25.00 due Nov 21. Credit limit 5,000.",
            "s2": "Please charge the deposit to Visa card 4532 0151 1283 0366, exp 09/28.",
            "s3": "Invoice 1000111346: balance due 4,512.00, payment terms net 30, order "
                  "4532015112830366, lot 22018837449911.",
        }
        for key, text in texts.items():
            handle = f"sharepoint://drive-site/{key}@1"
            self.fx.obj(f"SharePointFile:{key}", "SharePointFile", "sharepoint:drive-site",
                        kind="sharepoint_file", handle=handle,
                        text={"row.content_text": (handle, text)},
                        values={"name": (f"{key}.pdf", "[]")})
        chunks, counts = self.run_chunker(exclusion(drives={"drive-site": ""}))
        marks = {c.object_id: tuple(c.markings) for c in chunks}
        self.assertEqual(marks, {"SharePointFile:s1": ("finance", "pii"),
                                 "SharePointFile:s2": ("finance", "pii"),
                                 "SharePointFile:s3": ()})
        self.assertEqual((counts.documents["marked_finance_pii:statement"],
                          counts.documents["marked_finance_pii:card_number"]), (1, 1))

    def test_a_meeting_is_chunked_unless_a_person_on_it_is_excluded(self):
        staff = "staff.one@example-plx.com"
        box = "0e0e0e0e-0000-4000-8000-0000000000b1"
        for n, attendee in ((1, "buyer@customer-example.com"), (2, "gone@example-plx.com")):
            oid = f"Meeting:m{n}"
            handle = f"calendar-event://{box}/m{n}@1"
            self.fx.obj(oid, "Meeting", f"meetings:calendar:{box}", handle=handle,
                        kind="calendar_event",
                        text={"row.body_preview": (handle, f"Agenda {n}: review the carton "
                                                           "artwork proof and the ship date.")},
                        values={"subject": ("Carton artwork review", "[]"),
                                "start": ("2026-11-04T15:00:00Z", "[]"),
                                "organizer": (json.dumps({"address": staff, "name": "S"}), "[]"),
                                "attendees": (json.dumps([{"address": attendee,
                                                           "name": "A"}]), "[]")})
        handle = f"calendar-event://{box}/m3@1"     # no organiser address: no role
        self.fx.obj("Meeting:m3", "Meeting", f"meetings:calendar:{box}", handle=handle,
                    kind="calendar_event",
                    text={"row.body_preview": (handle, "Agenda: carton artwork sign-off.")},
                    values={"subject": ("Sign-off", "[]"),
                            "organizer": (json.dumps({"name": "Room"}), "[]"),
                            "attendees": (json.dumps([{"address": "buyer@customer-example.com",
                                                       "name": "A"}]), "[]")})
        excl = exclusion(directory={staff: "0e0e0e0e-0000-4000-8000-0000000000aa",
                                    "gone@example-plx.com": EXCLUDED})
        chunks, counts = self.run_chunker(excl)
        m3 = [c for c in chunks if c.object_id == "Meeting:m3"]
        self.assertNotIn("organised by", m3[0].header)
        chunks = [c for c in chunks if c.object_id != "Meeting:m3"]
        self.assertEqual([c.object_id for c in chunks], ["Meeting:m1"])
        self.assertEqual(chunks[0].kind, "meeting")
        self.assertEqual(chunks[0].occurred_at, "2026-11-04T15:00:00Z")
        self.assertIn("Title: Carton artwork review", chunks[0].header)
        self.assertNotIn("gone@", chunks[0].header + chunks[0].body)
        self.assertEqual(counts.skipped["people_exclusion:meeting_person"], 1)

    def test_shards_partition_the_documents_and_teams_stay_in_shard_zero(self):
        for n in range(6):
            self.fx.obj(f"MCTask:T-{n}", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
                        handle=f"mc://live/tasks/T-{n}@1",
                        text={"row.description": (f"mc://live/tasks/T-{n}@1",
                                                  f"Task {n} about the lavender soap order.")})
        chat = "19:c@thread.v2"
        self.teams_message(chat, "t1", "2026-10-01T10:00:00Z", "Teams note about cartons",
                           STAFF)
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        excl = exclusion(chats={chat: (STAFF,)})
        whole = {c.chunk_id for c in ProjectionChunker(conn, excl, Counts()).chunks()}
        parts = [{c.chunk_id for c in ProjectionChunker(conn, excl, Counts()).chunks(
            shard=(i, 3))} for i in range(3)]
        self.assertEqual(len(whole), 7)
        self.assertEqual(set().union(*parts), whole)
        self.assertEqual(sum(len(p) for p in parts), len(whole))
        self.assertTrue(any(c.startswith("teams:") for c in parts[0]))
        self.assertFalse(any(c.startswith("teams:") for p in parts[1:] for c in p))
        # A mail window never leaves out a Teams message or a task, however old.
        counts = Counts()
        windowed = {c.chunk_id for c in ProjectionChunker(conn, excl, counts).chunks(
            since="2026-06-01")}
        self.assertEqual(windowed, whole)
        self.assertEqual(counts.skipped["before_window"], 0)

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
        self.assertIn("Ids: AO-26197B", task.header)
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

    def test_a_layer_slice_reads_only_its_own_objects_and_chunks_them_as_a_whole_read(self):
        # CC-P-02: a delta layer is indexed a slice at a time; each slice read every text
        # field and every record of the layer (35 full reads of a 700k-object layer for
        # 0 chunks). A slice reads its own objects only, and chunks them as a whole read.
        for n in range(300):
            h = f"mc://live/tasks/T-{n}@1"
            self.fx.obj(f"MCTask:T-{n}", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
                        handle=h, text={"row.description": (
                            h, f"Task {n} waits on the carton supplier for the label.")})
            self.fx.obj(f"RelSageArCmm:{n}", "RelSageArCmm", "api:sage.ar_cmm",
                        values={"textcmnt1": (f"Customer {n} asked to hold shipments until "
                                              "the credit note is issued.", "[]")})
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        mine = frozenset({"MCTask:T-7", "MCTask:T-250", "RelSageArCmm:7", "RelSageArCmm:299"})

        class Counting:
            """The connection, counting the rows its reads hand to Python."""
            rows = 0

            def execute(self, *args):
                got = conn.execute(*args).fetchall()
                Counting.rows += len(got)
                return Rows(got)

        class Rows(list):
            def fetchall(self):
                return list(self)

        def chunks(source, objects):
            return sorted((c.object_id, c.chunk_id, c.body) for c in [
                *ProjectionChunker(source, exclusion(), Counts()).chunks(
                    ["mc_task"], objects=objects),
                *RecordChunker(source, exclusion(), Counts()).record_chunks(objects=objects)])

        whole = [c for c in chunks(conn, None) if c[0] in mine]
        sliced = chunks(Counting(), mine)
        self.assertEqual(sliced, whole)
        self.assertEqual({c[0] for c in sliced}, mine)
        # O(slice), not O(layer): 600 objects and 600 values are never handed to Python
        self.assertLess(Counting.rows, 60, Counting.rows)

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
                         ("record_words", "api:sage.ar_cmm", ("finance",)))   # raw rows: words only
        self.assertIn("Customer C0012", chunk.header)
        self.assertEqual(chunk.authority, 4)

    def test_mail_and_teams_participants_become_people_and_the_exclusion_holds(self):
        from operating.chunking.entities import participants
        box = "12345678-aaaa-bbbb-cccc-0000000000ee"
        staff = "staff@example-plx.com"
        self.fx.obj("Customer:9", "Customer", "api:public.app_customer",
                    values={"name": ("Maple Suds Inc", "[]"),
                            "bc_email": ("orders@maplesuds.example", "[]")})
        for oid, sender, to, when in (
                ("Email:1", "jane.roe@maplesuds.example",
                 f"['{staff}', 'no-reply@maplesuds.example', 'info2@maplesuds.example']",
                 "2026-10-15"),
                ("Email:2", staff, "['jane.roe@maplesuds.example', 'gone@example-plx.com']",
                 "2026-10-28"),
                ("Email:3", "gone@example-plx.com", "['kim@maplesuds.example']",
                 "2026-10-29")):
            self.fx.obj(oid, "Email", f"mail:{box}", kind="email",
                        values={"sender": (sender, "[]"), "to_addresses": (to, "[]"),
                                "received_at": (when + "T10:00:00Z", "[]")})
        chat = "19:c@thread.v2"
        self.teams_message(chat, "t1", "2026-10-28T09:00:00Z", "the INCI is different", STAFF)
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        excl = exclusion(directory={staff: STAFF, "gone@example-plx.com": EXCLUDED},
                         chats={chat: (STAFF,)})
        found = {(e.audience_key, e.alias[0] if e.alias else e.name): e
                 for e in participants(conn, Counts(), excl)}
        jane = found[(f"mail:{box}", "jane.roe@maplesuds.example")]
        self.assertEqual(jane.type, "person")
        self.assertEqual(jane.name, "Jane Roe")
        self.assertIn("Maple Suds Inc", jane.alias)
        self.assertEqual(jane.object_id, "Email:2")        # its newest message there
        self.assertEqual(jane.markings, ("pii",))
        self.assertIn((f"mail:{box}", staff), found)
        self.assertNotIn("no-reply@", json.dumps([e.alias for e in found.values()]))
        self.assertNotIn("info2@", json.dumps([e.alias for e in found.values()]))
        text = json.dumps([e.__dict__ for e in found.values()])
        self.assertNotIn("gone@", text)                     # never the excluded person
        self.assertNotIn("kim@", text)                   # nor what their mail names
        self.assertNotIn("Email:3", text)
        teams = [e for e in found.values() if e.audience_key == f"teams:chat:{chat}"]
        self.assertEqual([e.object_id for e in teams], [f"TeamsChatMessage:{chat}/t1"])
        self.assertEqual(list(participants(conn, Counts(), None)), [])   # no guard: none

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
        self.fx.obj("Contact:company", "Contact", "bc:co/contacts",
                    values={"display_name": ("Maple Suds Inc.", '["pii"]'),
                            "contact_type": ("Company", '["pii"]'),
                            "email": ("buyer@customer.example", '["pii"]')})
        self.fx.obj("Contact:noaddress", "Contact", "bc:co/contacts",
                    values={"display_name": ("No Address", '["pii"]')})
        self.fx.obj("PurchaseOrder:1", "PurchaseOrder", "api:public.app_purchase_order",
                    idents=[("petra_po", "PO5280", "primary", "[]")])
        self.fx.obj("Customer:bad", "Customer", "api:*", values={"name": ("Nope", "[]")})
        # a parent's code each location carries names the parent, never the location; a
        # first name typed in a number field is no code
        self.fx.obj("Customer:2", "Customer", "api:public.app_customer",
                    values={"name": ("Savon Quebec - Laval", "[]")},
                    idents=[("customer_erp_code", "SAVL02", "alias", "[]", "erp_code"),
                            ("customer_erp_code", "SAVQ01", "alias", "[]",
                             "bill_to_erp_code")])
        self.fx.obj("CustomerPurchaseOrder:1", "CustomerPurchaseOrder", "api:sage.oe",
                    idents=[("customer_po", "MARCEL", "alias", "[]", "po_number")])
        # a document asked for by number: its number value is its code, nothing else
        self.fx.obj("SalesInvoice:1", "SalesInvoice", "api:sage.oe",
                    values={"invoice_number": ("XN1000777 ", "[]"),
                            "customer_number": ("SAVQ01", "[]")},
                    idents=[("customer_erp_code", "SAVQ01", "alias", "[]",
                             "customer_number")])
        self.fx.obj("Receipt:1", "Receipt", "api:fm.receipt",
                    values={"sage_receipt_number": ("n/a", "[]")})
        self.fx.obj("CustomerPurchaseOrder:2", "CustomerPurchaseOrder", "api:sage.oe",
                    idents=[("customer_po", "PO-4410", "alias", "[]", "po_number")])
        self.fx.obj("CustomerLocation:1", "CustomerLocation", "api:public.app_customer_location",
                    values={"label": ("Savon Quebec - Montreal plant", "[]"),
                            "country": ("CA", "[]"), "city": ("PRIVATECITY", '["pii"]')})
        self.fx.obj("Product:1", "Product", "bc:co/items",
                    values={"name": ("Oat soap bar 100g", "[]"),
                            "item_category": ("SOAP", "[]")})
        # IN-13: a channel, chat, task or file is named by its own title; one without a
        # title is no entity, never named by its technical id; a chat whose members the
        # exclusion cannot clear is withheld
        self.fx.obj("TeamsChannel:1", "TeamsChannel", "teams:channel:t1/c1",
                    values={"display_name": ("Babassu launch", "[]")},
                    idents=[("teams_channel_id", "c1", "primary", "[]", "channel_id")])
        self.fx.obj("TeamsChannel:2", "TeamsChannel", "teams:channel:t1/c2",
                    idents=[("teams_channel_id", "c2", "primary", "[]", "channel_id")])
        self.fx.obj("TeamsChat:1", "TeamsChat", "teams:chat:19:a",
                    values={"topic": ("Babassu launch sync", "[]")})
        self.fx.obj("MCTask:1", "MCTask", "mc:qa", values={"title": ("Recall drill", "[]")})
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        counts = Counts()
        excl = exclusion(directory={"gone@example-plx.com": EXCLUDED})
        found = {e.object_id: e for e in entities(conn, counts, exclusion=excl)}
        self.assertEqual((found["TeamsChannel:1"].type, found["TeamsChannel:1"].name),
                         ("channel", "Babassu launch"))
        self.assertNotIn("TeamsChannel:2", found)
        self.assertNotIn("TeamsChat:1", found)
        self.assertEqual((found["MCTask:1"].type, found["MCTask:1"].name), ("task", "Recall drill"))
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
        self.assertEqual(found["Contact:1"].type, "person")
        self.assertEqual(found["Contact:company"].type, "contact")   # a company's card
        self.assertEqual(found["Contact:company"].markings, ("pii",))
        self.assertEqual(found["PurchaseOrder:1"].name, "PO5280")
        self.assertNotIn("Customer:bad", found)
        self.assertEqual(fold("G2  Beauty & Co."), "g2 beauty and co")
        # Attribute words from the record's own values; a pii value never an alias.
        place = found["CustomerLocation:1"]
        self.assertIn("canada canadian", place.alias)
        self.assertNotIn("PRIVATECITY", " ".join(place.alias))
        self.assertEqual(place.markings, ())
        self.assertIn("SOAP", found["Product:1"].alias)
        self.assertIn("SAVL02", found["Customer:2"].alias)
        self.assertNotIn("SAVQ01", found["Customer:2"].alias)
        self.assertNotIn("CustomerPurchaseOrder:1", found)     # no code: no entity
        invoice = found["SalesInvoice:1"]
        self.assertEqual((invoice.type, invoice.name), ("sales_invoice", "Invoice XN1000777"))
        self.assertIn("XN1000777", invoice.alias)
        self.assertNotIn("SAVQ01", invoice.alias)               # its customer's code
        self.assertNotIn("Receipt:1", found)
        self.assertIn("PO-4410", found["CustomerPurchaseOrder:2"].alias + (
            found["CustomerPurchaseOrder:2"].name,))

    def email(self, oid, box, text, *, sender="supplier@outside.example", marks="[]"):
        handle = f"graph-mail://{box}/{oid}@1"
        values = {"subject": ("Order status", "[]")}
        if sender is not None:
            values["sender"] = (sender, "[]")
        self.fx.obj(oid, "Email", f"mail:{box}", handle=handle, kind="email", marks=marks,
                    text={"row.body_text": (handle, text)}, values=values)
        return oid

    def test_a_mail_window_reads_only_the_mail_recorded_inside_it(self):
        box = "12345678-aaaa-bbbb-cccc-0000000000dd"
        self.email("Email:old", box, "An old message about the lavender soap order.")
        self.email("Email:new", box, "A new message about the castile soap order.")
        self.fx.conn.execute("UPDATE object_versions SET recorded_at = '2026-11-02T00:00:00Z' "
                             "WHERE object_id = 'Email:new'")
        # captured in the same recent sweep, but sent in May: left out by its date, and
        # its attachment with it
        self.email("Email:may", box, "A May message about the glycerin soap order.")
        self.fx.conn.execute("UPDATE object_versions SET recorded_at = '2026-11-02T00:00:00Z' "
                             "WHERE object_id = 'Email:may'")
        self.fx.conn.execute("INSERT INTO object_values VALUES ('Email:may', 'received_at', "
                             "'2026-05-04T10:00:00Z', '[]', NULL, 'h', 'row.received_at', "
                             "'text')")
        handle = f"graph-mail://{box}/Email:may@1#attachment=A9"
        self.fx.obj("Attachment:A9", "Attachment", f"mail:{box}", handle=handle,
                    kind="attachment", text={"row.content_text": (
                        handle + "&page=1", "Attachment text of the May message, long.")},
                    values={"name": ("may.pdf", "[]")})
        self.fx.conn.execute("UPDATE object_versions SET recorded_at = '2026-11-02T00:00:00Z' "
                             "WHERE object_id = 'Attachment:A9'")
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        counts = Counts()
        chunks = list(ProjectionChunker(conn, exclusion(), counts).chunks(since="2026-10-06"))
        self.assertEqual({c.object_id for c in chunks}, {"Email:new"})
        self.assertEqual(counts.skipped["before_window"], 3)

    def test_a_history_window_reads_the_older_mail_and_leaves_the_newer(self):
        box = "12345678-aaaa-bbbb-cccc-0000000000de"
        for oid, sent in (("Email:jun", "2026-06-04"), ("Email:nov", "2026-11-03")):
            self.email(oid, box, f"A message of {sent} about the soap order and cartons.")
            self.fx.conn.execute("UPDATE object_versions SET recorded_at = "
                                 "'2026-11-04T00:00:00Z' WHERE object_id = ?", (oid,))
            self.fx.conn.execute("INSERT INTO object_values VALUES (?, 'received_at', ?, "
                                 "'[]', NULL, 'h', 'row.received_at', 'text')",
                                 (oid, sent + "T10:00:00Z"))
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        counts = Counts()
        chunks = list(ProjectionChunker(conn, exclusion(), counts).chunks(
            since="2026-05-01", until="2026-10-06"))
        self.assertEqual({c.object_id for c in chunks}, {"Email:jun"})
        self.assertEqual(counts.skipped["after_window"], 1)

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
                             "'primary','Email:2','[]',NULL,'code')")
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
        self.assertIn("PO5555", first.header)
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

    def test_withheld_senders_scale_linearly_and_two_disagreeing_senders_withhold(self):
        from operating.chunking.projection import withheld_messages
        box = "12345678-aaaa-bbbb-cccc-0000000000dd"
        directory = {}
        for n in range(60):
            sender = f"p{n}@example-plx.com"
            directory[sender] = EXCLUDED if n % 2 else STAFF
            self.email(f"Email:m{n}", box, f"Message {n} about carton deliveries this week.",
                       sender=sender)
            handle = f"graph-mail://{box}/Email:m{n}@1#attachment=A{n}"
            self.fx.obj(f"Attachment:A{n}", "Attachment", f"mail:{box}", handle=handle,
                        kind="attachment", values={"name": ("x.pdf", "[]")})
            self.fx.link(f"Attachment:A{n}", f"Email:m{n}", "Attachment.attached_to")
        # a second current sender value on a cleared message: not every value clears
        self.fx.conn.execute("INSERT INTO object_values VALUES (?,?,?,?,NULL,?,?,?)",
                             ("Email:m0", "sender", "p1@example-plx.com", "[]", "h",
                              "row.sender", "text"))
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        got = withheld_messages(conn, exclusion(directory=directory))
        odd = {n for n in range(60) if n % 2} | {0}
        self.assertEqual(got, {f"Email:m{n}" for n in odd} | {f"Attachment:A{n}" for n in odd})

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


class DeltaRunningTests(unittest.TestCase):
    """PERF-16: the follower's embedding waits while a delta cycle holds its run lock,
    told from the kernel's lock table (never by taking the lock: a delta starting then
    would skip its cycle)."""

    def test_the_delta_run_lock_is_read_without_taking_it(self):
        import fcntl
        from operating.embed import follow as fl
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertFalse(fl.delta_running(state))          # no lock file: not running
            lock = state / "projection" / "delta" / ".run.lock"
            lock.parent.mkdir(parents=True)
            lock.touch()
            self.assertFalse(fl.delta_running(state))
            with open(lock, "a") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(fl.delta_running(state))
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            self.assertFalse(fl.delta_running(state))
            # reading it never took it: a delta can still take it at once
            with open(lock, "a") as again:
                fcntl.flock(again.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(fl.delta_running(state, locks=state / "no-such-table"))


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

    def test_rows_a_parallel_shard_wrote_are_kept_by_the_full_pass(self):
        chunks = [self.chunk(i, f"Distinct body {i} about lavender soap.") for i in range(6)]
        self.st.ChunkWriter(self.conn).write(chunks)
        other = self.st.connect(f"postgresql:///{self.db}")
        self.addCleanup(other.close)
        self.st.ChunkWriter(other).write(chunks[3:])     # a shard on its own connection
        writer = self.st.ChunkWriter(self.conn)
        writer.write(chunks[:2])
        writer.mark_seen([c.chunk_id for c in chunks[3:]])
        self.assertEqual(writer.delete_stale(["mail"]), {"mail": {"deleted": 1}})

    def test_window_keeps_old_mail_unwritten_but_never_deletes_it(self):
        from operating.embed.shards import _Tally
        old, new = self.chunk(1, "An old message about soap."), self.chunk(2, "A new one.")
        old = old.__class__(**{**old.__dict__, "source_handle": "graph-mail://box-a/1@1",
                               "occurred_at": "2026-01-02T00:00:00Z"})
        new = new.__class__(**{**new.__dict__, "source_handle": "graph-mail://box-a/2@1",
                               "occurred_at": "2026-11-02T00:00:00Z"})
        doc = self.chunk(3, "A SharePoint file of January.", kind="sharepoint")
        doc = doc.__class__(**{**doc.__dict__, "occurred_at": "2026-01-02T00:00:00Z"})
        self.st.ChunkWriter(self.conn).write([old])          # stored by an earlier run
        writer = self.st.ChunkWriter(self.conn)
        tally = _Tally("2026-10-01")
        tally.take([old, new, doc], writer)
        writer.mark_seen(tally.outside)
        self.assertEqual(tally.outside, [old.chunk_id])
        self.assertEqual((writer.inserted, writer.unchanged), (2, 0))
        writer.delete_stale(["mail", "sharepoint"])
        stored = {r[0] for r in self.conn.execute("SELECT chunk_id FROM search.chunks")}
        self.assertEqual(stored, {old.chunk_id, new.chunk_id, doc.chunk_id})
        # A windowed pass that never read the old message (recorded before the window)
        # keeps its row, and still deletes a message of the window that is gone.
        writer = self.st.ChunkWriter(self.conn)
        writer.write([doc])
        self.assertEqual(writer.delete_stale(["mail", "sharepoint"], allow_shrink=True,
                                             keep_before="2026-10-01"),
                         {"mail": {"deleted": 1}, "sharepoint": {"deleted": 0}})
        stored = {r[0] for r in self.conn.execute("SELECT chunk_id FROM search.chunks")}
        self.assertEqual(stored, {old.chunk_id, doc.chunk_id})

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

    def test_follow_indexes_a_generation_once_and_each_delta_layer_once(self):
        from operating.chunking import make_chunk
        from operating.embed import follow as fl
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        projection = state / "projection"
        (projection / "generations" / "g1").mkdir(parents=True)
        (projection / "generations" / "g1" / "store.sqlite").write_bytes(b"")
        (projection / "CURRENT").write_text(json.dumps(
            {"generation": "g1", "content_digest": "sha256:aaa"}))
        layer = projection / "delta" / "delta-1.sqlite"
        layer.parent.mkdir()
        fx = Fixture(layer)
        for n in (1, 2):
            fx.obj(f"MCTask:T-{n}", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
                   handle=f"mc://live/tasks/T-{n}@2",
                   text={"row.description": (f"mc://live/tasks/T-{n}@2",
                                             f"Task {n} now waits on the carton supplier.")})
        fx.conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
        fx.conn.execute("INSERT INTO delta_owned VALUES ('MCTask:T-1')")   # T-2: context
        fx.done()
        (projection / "delta" / "CURRENT.json").write_text(json.dumps(
            {"format": "plx-delta/v1", "base_digest": "sha256:aaa", "store": str(layer),
             "layers": [{"store": str(layer), "owned": 1}]}))
        earlier = make_chunk(kind="mc_task", parts=("mc:live/tasks/PRJ-X", "old", "0"),
                             source_handle="mc://live/tasks/T-1@1", object_id="MCTask:T-1",
                             title="T-1", author_role=None, occurred_at=None,
                             header="Source: x", body="Task 1 was on time.", linked_ids=[],
                             audience_key="mc:live/tasks/PRJ-X", markings=())
        self.st.ChunkWriter(self.conn).write([earlier])
        passes = []
        args = fl.parser().parse_args(["--state", str(state), "--embed-seconds", "0"])
        with mock.patch.object(fl, "_exclusion", return_value=exclusion()), \
                mock.patch.object(fl.config, "settings",
                                  return_value={"PLX_PG_DSN": f"postgresql:///{self.db}"}), \
                mock.patch.object(fl, "generation_pass",
                                  side_effect=lambda a, store: passes.append(store) or {}):
            first = fl.follow(args)
            rows = self.conn.execute("SELECT object_id, body FROM search.chunks").fetchall()
            self.assertEqual(rows, [("MCTask:T-1", "Task 1 now waits on the carton supplier.")])
            self.assertEqual(first["layers"]["delta-1.sqlite"]["replaced_deleted"], 1)
            self.assertEqual(len(passes), 1)
            # nothing new: no pass, no layer read again
            self.assertEqual(fl.follow(args)["layers"], {})
            self.assertEqual(len(passes), 1)
            cursor = json.loads((state / "search" / "follow.json").read_text())
            self.assertEqual((cursor["digest"], cursor["layers"]), ("sha256:aaa", [str(layer)]))
            # one run at a time
            with fl.held(state / "search" / "follow.json.lock"):
                self.assertEqual(fl.follow(args)["skipped"], "another_follow_running")
            # an hourly fold lists the same change again: the version indexed is not re-read
            fold = projection / "delta" / "delta-2.sqlite"
            shutil.copy(layer, fold)
            (projection / "delta" / "CURRENT.json").write_text(json.dumps(
                {"format": "plx-delta/v1", "base_digest": "sha256:aaa", "store": str(fold),
                 "layers": [{"store": str(fold), "owned": 1}]}))
            again = fl.follow(args)["layers"]["delta-2.sqlite"]
            self.assertEqual((again["already_indexed"], again["chunks"]), (1, 0))
            # the SQLite generation retired (Postgres the only store): a pass that would
            # be due (newer chunk code) is skipped, never read from a missing file, and
            # the layers still follow
            (projection / "generations" / "g1" / "store.sqlite").unlink()
            with mock.patch.object(fl, "chunk_code", return_value="newer-chunk-code"):
                retired = fl.follow(args)
            self.assertEqual(retired["generation_pass"], {"skipped": "generation_retired"})
            self.assertEqual((len(passes), "layers" in retired), (1, True))

    def test_follow_embeds_with_one_model_in_its_own_process(self):
        # GA-F15: the follower's embedding loaded bge-m3 once per child process (1.9 GB
        # each); it now runs one model in its own process, all its threads, no children
        import concurrent.futures
        from operating.embed import follow as fl, models as md
        self.st.ChunkWriter(self.conn).write(
            [self.chunk(n, f"Body {n} about the carton supplier.") for n in range(5)])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        (state / "projection").mkdir()
        (state / "projection" / "CURRENT").write_text(json.dumps(
            {"generation": "g1", "content_digest": "sha256:aaa"}))
        dim = self.st.column_dim(self.conn)
        loads = []

        def load(settings, **_):
            loads.append(settings.get("PLX_EMBED_THREADS"))
            return md.Embedder("fake", dim, lambda texts: np.ones((len(texts), dim)))

        def no_children(*_, **__):
            raise AssertionError("a model process per child")
        args = fl.parser().parse_args(["--state", str(state), "--mode", "layers",
                                       "--embed-seconds", "60", "--workers", "3"])
        with mock.patch.object(fl, "_exclusion", return_value=exclusion()), \
                mock.patch.object(fl.config, "settings",
                                  return_value={"PLX_PG_DSN": f"postgresql:///{self.db}"}), \
                mock.patch.object(md, "load", load), \
                mock.patch.object(concurrent.futures, "ProcessPoolExecutor", no_children), \
                mock.patch("os.cpu_count", return_value=16):
            report = fl.follow(args)
        self.assertEqual(loads, ["8"])                  # one model, half the cores' threads
        self.assertEqual((report["embed"]["texts_embedded"], report["embed"]["workers"]),
                         (5, 1))
        self.assertEqual(self.st.pending(self.conn, "fake"), [])

    def test_backfill_embeds_the_recent_and_the_undated_first_and_takes_turns(self):
        from operating.embed import backfill as bf
        now = dt.datetime.now(dt.timezone.utc)
        rows = []
        for n, (days, kind) in enumerate([(400, "mail"), (5, "mail"), (None, "record_text"),
                                          (30, "teams"), (200, "sharepoint")]):
            c = self.chunk(n, f"Distinct body {n} about cartons.", kind=kind)
            at = None if days is None else (now - dt.timedelta(days=days)).isoformat()
            rows.append(c.__class__(**{**c.__dict__, "occurred_at": at}))
        self.st.ChunkWriter(self.conn).write(rows)
        order = self.st.pending(self.conn, "m", order="backfill", recent_days=90)
        sha = {r.content_sha: r.kind + str(i) for i, r in enumerate(rows)}
        self.assertEqual([sha[s] for s in order],
                         ["mail1", "teams3", "record_text2", "sharepoint4", "mail0"])
        recent = self.st.pending(self.conn, "m", order="backfill", recent_days=90,
                                 recent_only=True)
        self.assertEqual([sha[s] for s in recent], ["mail1", "teams3", "record_text2"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        self.assertFalse(bf.running(state))
        with bf.waited(state / bf.BACKFILL_LOCK, 0) as mine:
            self.assertTrue(mine)
            self.assertTrue(bf.running(state))          # the follower leaves embedding
            with bf.waited(state / bf.BACKFILL_LOCK, 0.2, poll=0.05) as second:
                self.assertFalse(second)                # one backfill at a time
        self.assertFalse(bf.running(state))

    def test_a_pass_keeps_the_rows_another_run_wrote_while_it_ran(self):
        writer = self.st.ChunkWriter(self.conn)
        writer.write([self.chunk(1, "Kept: this pass produced it."),
                      self.chunk(2, "Gone: its source left before the pass.")])
        started = self.conn.execute("SELECT now()").fetchone()[0]
        self.conn.commit()
        other = self.st.ChunkWriter(self.conn)
        other.write([self.chunk(3, "A delta layer wrote this during the pass.")])
        self.conn.execute("UPDATE search.chunks SET updated_at = now() + interval '1 second' "
                          "WHERE object_id = 'O3'")
        self.conn.commit()
        writer = self.st.ChunkWriter(self.conn)
        writer.write([self.chunk(1, "Kept: this pass produced it.")])
        writer.delete_stale(["mail"], allow_shrink=True, written_before=started)
        left = sorted(r[0] for r in self.conn.execute("SELECT object_id FROM search.chunks"))
        self.assertEqual(left, ["O1", "O3"])

    def test_backfill_slices_store_what_they_encoded_and_stop_on_time(self):
        import time as _time
        from concurrent.futures import ThreadPoolExecutor
        from operating.embed import backfill as bf, models as md
        rows = [self.chunk(n, f"Body {n} " + "x" * (n * 7)) for n in range(5)]
        self.st.ChunkWriter(self.conn).write(rows)
        dim = self.st.column_dim(self.conn)
        shas = self.st.pending(self.conn, "fake", order="backfill")
        with mock.patch.object(md, "_WORKER", md.Embedder(
                "fake", dim, lambda texts: np.ones((len(texts), dim)))), \
                ThreadPoolExecutor(2) as pool:
            done, _ = bf.encode_sorted(self.conn, pool, "fake", dim, shas, call=2,
                                       inflight_max=2, deadline=_time.monotonic() - 1)
            self.assertEqual(done, set())                    # past its time: nothing
            done, stored = bf.encode_sorted(self.conn, pool, "fake", dim, shas, call=2,
                                            inflight_max=2, deadline=_time.monotonic() + 60)
        self.assertEqual((done, stored), (set(shas), 5))
        self.assertEqual(self.st.pending(self.conn, "fake"), [])
        # entity names: short texts, embedded in their own phase
        from operating.chunking.entities import Entity
        self.st.ChunkWriter(self.conn).write_entities([Entity(
            entity_id=f"e{n}", object_id=f"O{n}", type="supplier", name=f"Carton Co {n}",
            alias=(), normalized=f"carton co {n}", audience_key="mail:box-a", markings=())
            for n in range(3)])
        name_dim = self.st.column_dim(self.conn, "entities", "name_embedding")
        with mock.patch.object(md, "_WORKER", md.Embedder(
                "fake", name_dim, lambda texts: np.ones((len(texts), name_dim)))), \
                ThreadPoolExecutor(2) as pool:
            got = bf.embed_names(self.conn, pool, "fake", name_dim,
                                 deadline=_time.monotonic() + 60, inflight_max=2)
        self.assertEqual(got, (3, 3))
        self.assertEqual(self.st.pending_names(self.conn, "fake", 10), [])

    def mc_version(self, n, version, body):
        from operating.chunking import make_chunk
        return make_chunk(kind="mc_task", parts=("mc:live/tasks/PRJ-X", f"T-{n}@{version}", "0"),
                          source_handle=f"mc://live/tasks/T-{n}@{version}",
                          object_id=f"MCTask:T-{n}", title=f"T-{n}", author_role=None,
                          occurred_at=None, header="Source: x", body=body, linked_ids=[],
                          audience_key="mc:live/tasks/PRJ-X", markings=())

    def handles(self):
        return sorted(r[0] for r in self.conn.execute(
            "SELECT source_handle FROM search.chunks WHERE object_id = 'MCTask:T-1'"))

    def test_a_layer_that_fails_to_read_stays_behind_and_is_read_again(self):
        # SEARCH-005: a locked or unreadable layer is no "not a delta layer": it never
        # enters the cursor, no later layer is indexed past it, the run says so (exit 1)
        import contextlib
        import io
        from operating.embed import follow as fl
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        projection = state / "projection"
        (projection / "generations" / "g1").mkdir(parents=True)
        (projection / "generations" / "g1" / "store.sqlite").write_bytes(b"")
        (projection / "CURRENT").write_text(json.dumps(
            {"generation": "g1", "content_digest": "sha256:aaa"}))
        (projection / "delta").mkdir()
        stores = []
        for n in (1, 2):
            layer = projection / "delta" / f"delta-{n}.sqlite"
            fx = Fixture(layer)
            h = f"mc://live/tasks/T-{n}@2"
            fx.obj(f"MCTask:T-{n}", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task", handle=h,
                   text={"row.description": (h, f"Task {n} waits on the carton supplier.")})
            fx.conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
            fx.conn.execute("INSERT INTO delta_owned VALUES (?)", (f"MCTask:T-{n}",))
            fx.done()
            stores.append(str(layer))
        not_layer = projection / "delta" / "delta-0.sqlite"
        sqlite3.connect(not_layer).close()       # no layer tables: skipped for good
        (projection / "delta" / "CURRENT.json").write_text(json.dumps(
            {"format": "plx-delta/v1", "base_digest": "sha256:aaa", "store": stores[-1],
             "layers": [{"store": s, "owned": 1} for s in reversed([str(not_layer)] + stores)]}))
        args = fl.parser().parse_args(["--state", str(state), "--embed-seconds", "0",
                                       "--mode", "layers"])
        real = fl.open_ro

        def locked(path):
            if Path(path).name == "delta-1.sqlite":
                raise sqlite3.OperationalError("database is locked")
            return real(path)
        cursor_path = state / "search" / "follow.json"
        with mock.patch.object(fl, "_exclusion", return_value=exclusion()), \
                mock.patch.object(fl.config, "settings",
                                  return_value={"PLX_PG_DSN": f"postgresql:///{self.db}"}):
            with mock.patch.object(fl, "open_ro", locked):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(fl.main(["--state", str(state), "--embed-seconds", "0",
                                              "--mode", "layers"]), 1)
                report = json.loads(out.getvalue())
            self.assertEqual(report["layers"]["delta-0.sqlite"], {"skipped": "not_a_delta_layer"})
            self.assertEqual(report["layers"]["delta-1.sqlite"]["failed"], "layer_unreadable")
            self.assertEqual(report["layer_failed"], "delta-1.sqlite")
            self.assertNotIn("delta-2.sqlite", report["layers"])
            self.assertEqual(report["layers_behind"], 2)
            self.assertEqual(json.loads(cursor_path.read_text())["layers"], [str(not_layer)])
            self.assertEqual(self.conn.execute("SELECT count(*) FROM search.chunks")
                             .fetchone()[0], 0)
            # readable again: both layers indexed in order, nothing behind
            again = fl.follow(args)
            self.assertEqual(sorted(again["layers"]), ["delta-1.sqlite", "delta-2.sqlite"])
            self.assertEqual((again["layers_behind"], "layer_failed" in again), (0, False))
            self.assertEqual(sorted(r[0] for r in self.conn.execute(
                "SELECT DISTINCT object_id FROM search.chunks")), ["MCTask:T-1", "MCTask:T-2"])

    def test_a_layer_during_a_generation_pass_leaves_one_version_the_newest(self):
        import time as _time
        self.st.ChunkWriter(self.conn).write([self.mc_version(1, 1, "Task 1 is open.")])
        started = self.conn.execute("SELECT now()").fetchone()[0]   # the pass starts
        self.conn.commit()
        _time.sleep(0.02)
        layer = self.st.ChunkWriter(self.conn)                      # a layer: v3
        layer.write([self.mc_version(1, 3, "Task 1 is cancelled.")])
        self.st.delete_replaced(self.conn, [("MCTask:T-1", "mc_task")],
                                [self.mc_version(1, 3, "x").chunk_id])
        _time.sleep(0.02)
        gen = self.st.ChunkWriter(self.conn)                        # the pass: its v2
        gen.write([self.mc_version(1, 2, "Task 1 waits on cartons.")])
        self.assertEqual(self.st.drop_overtaken(gen.conn, started), 1)
        gen.delete_stale(["mc_task"], allow_shrink=True, written_before=started)
        self.assertEqual(self.handles(), ["mc://live/tasks/T-1@3"])

    def test_a_layer_run_after_a_pass_restores_the_newer_version_it_deleted(self):
        from operating.embed import follow as fl
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        layer = Path(tmp.name) / "delta-1.sqlite"
        fx = Fixture(layer)
        fx.obj("MCTask:T-1", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
               handle="mc://live/tasks/T-1@3",
               text={"row.description": ("mc://live/tasks/T-1@3", "Task 1 is cancelled now.")})
        fx.conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
        fx.conn.execute("INSERT INTO delta_owned VALUES ('MCTask:T-1')")
        fx.done()
        # the pass left the generation's v2 alone (its stale deletion took a layer's v3)
        self.st.ChunkWriter(self.conn).write([self.mc_version(1, 2, "Task 1 waits.")])
        fl.layer_pass(self.conn, Path(tmp.name), layer, exclusion())
        self.assertEqual(self.handles(), ["mc://live/tasks/T-1@3"])
        # a later fold lists v3 again (held, skipped) while a v2 row is back: it goes
        self.st.ChunkWriter(self.conn).write([self.mc_version(1, 2, "Task 1 waits.")])
        again = fl.layer_pass(self.conn, Path(tmp.name), layer, exclusion())
        self.assertEqual((again["already_indexed"], again["other_versions_deleted"]), (1, 1))
        self.assertEqual(self.handles(), ["mc://live/tasks/T-1@3"])

    def test_a_large_layer_stops_at_its_deadline_and_resumes_after_its_last_slice(self):
        from operating.embed import follow as fl
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        layer = Path(tmp.name) / "delta-1.sqlite"
        fx = Fixture(layer)
        fx.conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
        for n in (1, 2, 3):
            h = f"mc://live/tasks/T-{n}@1"
            fx.obj(f"MCTask:T-{n}", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task", handle=h,
                   text={"row.description": (h, f"Task {n} waits on the carton supplier.")})
            fx.conn.execute("INSERT INTO delta_owned VALUES (?)", (f"MCTask:T-{n}",))
        fx.done()
        saved = []
        ticks = iter([0.0, 0.5, 5.0])   # slice 1 starts in time, slice 2 after the deadline
        with mock.patch.object(fl, "SLICE", 2), \
                mock.patch.object(fl, "_clock", lambda: next(ticks)):
            first = fl.layer_pass(self.conn, Path(tmp.name), layer, exclusion(), deadline=1.0,
                                  save=lambda p: saved.append(dict(p)))
        self.assertEqual(first["partial"], {"slices_done": 1, "slices": 2})
        self.assertNotIn("entities", first)
        self.assertEqual(saved[-1], {"done": 1})
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT DISTINCT object_id FROM search.chunks")), ["MCTask:T-1", "MCTask:T-2"])
        # the next run resumes after slice 1: T-3 only, nothing chunked twice
        with mock.patch.object(fl, "SLICE", 2):
            second = fl.layer_pass(self.conn, Path(tmp.name), layer, exclusion(),
                                   progress=dict(saved[-1]))
        self.assertNotIn("partial", second)
        self.assertEqual((second["chunks"], second["already_indexed"]), (1, 0))
        self.assertEqual(self.conn.execute(
            "SELECT count(DISTINCT object_id) FROM search.chunks").fetchone()[0], 3)
        # a run killed inside slice 2 left T-3 in part: that slice is not skipped as held
        with mock.patch.object(fl, "SLICE", 2):
            third = fl.layer_pass(self.conn, Path(tmp.name), layer, exclusion(),
                                  progress={"done": 1, "started": 1})
        self.assertEqual((third["chunks"], third["already_indexed"]), (1, 0))
        # CC-P-02: a slice is not started when the last one's time would carry it past
        # the deadline (a run killed in a slice redoes it): slice 1 took 6 of 10 s
        for path in ("T-1", "T-2", "T-3"):
            self.conn.execute("DELETE FROM search.chunks WHERE object_id = %s",
                              (f"MCTask:{path}",))
        self.conn.commit()
        ticks = iter([0.0, 6.0, 6.0])
        with mock.patch.object(fl, "SLICE", 2), \
                mock.patch.object(fl, "_clock", lambda: next(ticks)):
            fitted = fl.layer_pass(self.conn, Path(tmp.name), layer, exclusion(),
                                   deadline=10.0)
        self.assertEqual(fitted["partial"], {"slices_done": 1, "slices": 2})

    def list_store(self, n, *, extra=()):
        """A store of ``n`` SharePoint list items on a site list (and ``extra`` rows)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.sqlite"
        fx = Fixture(path)
        unit = "sharepoint-list:plx.sharepoint.com,site-1,web-1/list-1"
        for i in range(n):
            fields = {"Title": f"Lavender soap complaint number {i}",
                      "Status_x0020_Note": "waiting on the supplier to answer the claim"}
            fx.obj(f"SharePointListItem:i{i:03}", "SharePointListItem", unit,
                   values={"fields": (json.dumps(fields), "[]")})
        for oid, audience, marks in extra:
            fx.obj(oid, "SharePointListItem", audience, marks=marks,
                   values={"fields": (json.dumps({"Title": "Payroll correction for the "
                                                  "night shift", "Note": "send to finance"}),
                                      "[]")})
        fx.done()
        store = open_ro(path)
        self.addCleanup(store.close)
        return store

    def test_a_records_pass_indexes_each_unchunked_record_once_at_any_count(self):
        """DI-07: records no generation pass or layer chunked become searchable; one already
        holding record text is left alone; a re-run writes nothing; slices lose no row."""
        from operating.embed import record_pass as rp
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.conn.execute("TRUNCATE search.chunks")
                self.conn.commit()
                store = self.list_store(n)
                held = self.chunk(0, "an older indexed row of the first item",
                                  audience="sharepoint-list:plx.sharepoint.com,site-1,web-1/list-1",
                                  kind="list_item")
                self.st.ChunkWriter(self.conn).write([dataclasses.replace(
                    held, object_id="SharePointListItem:i000")])
                with mock.patch.object(rp, "SLICE", 7):
                    got = rp.record_pass(store, self.conn, exclusion(), ["SharePointListItem"])
                done = got["types"]["SharePointListItem"]
                self.assertEqual((done["objects"], done["already_indexed"],
                                  done["chunked_objects"]), (n, 1, n - 1))
                rows = self.conn.execute(
                    "SELECT object_id, kind, count(*) FROM search.chunks GROUP BY 1, 2 "
                    "ORDER BY 1").fetchall()
                self.assertEqual([r[0] for r in rows],
                                 [f"SharePointListItem:i{i:03}" for i in range(n)])
                self.assertEqual({(r[1], r[2]) for r in rows}, {("list_item", 1)})
                # the held item kept its own row, unrewritten
                self.assertEqual(self.conn.execute(
                    "SELECT body FROM search.chunks WHERE object_id = %s",
                    ("SharePointListItem:i000",)).fetchone()[0],
                    "an older indexed row of the first item")
                again = rp.record_pass(store, self.conn, exclusion(), ["SharePointListItem"])
                self.assertEqual(again["types"]["SharePointListItem"]["chunks"], 0)

    def test_a_records_pass_keeps_markings_and_never_writes_a_withheld_unit(self):
        from operating.embed import record_pass as rp
        mine = "sharepoint-list:plx-my.sharepoint.com,personal-1,web-1/list-9"
        store = self.list_store(0, extra=(
            ("SharePointListItem:pay", "sharepoint-list:plx.sharepoint.com,s,w/l",
             '["finance", "pii"]'),
            ("SharePointListItem:own", mine, "[]")))
        got = rp.record_pass(store, self.conn, exclusion(), ["SharePointListItem"])
        self.assertEqual(got["skipped"].get("people_exclusion:record"), 1)
        rows = self.conn.execute("SELECT object_id, audience_key, markings FROM search.chunks"
                                 ).fetchall()
        self.assertEqual([(r[0], r[1], sorted(r[2])) for r in rows],
                         [("SharePointListItem:pay", "sharepoint-list:plx.sharepoint.com,s,w/l",
                           ["finance", "pii"])])

    def test_a_records_pass_refuses_and_writes_nothing_when_it_cannot_decide(self):
        import contextlib
        from operating.embed import record_pass as rp
        store = self.list_store(3)
        down = types.SimpleNamespace(unavailable="excluded_people_unresolved")
        cases = {"people_exclusion_unavailable:excluded_people_unresolved":
                 dict(exclusion=down, types=["SharePointListItem"]),
                 "type_not_indexed:NoSuchType":
                 dict(exclusion=exclusion(), types=["SharePointListItem", "NoSuchType"]),
                 "type_not_indexed:<none>": dict(exclusion=exclusion(), types=[]),
                 "another_follow_running":
                 dict(exclusion=exclusion(), types=["SharePointListItem"],
                      lock=lambda: contextlib.nullcontext(False))}
        for refused, kw in cases.items():
            with self.subTest(refused=refused):
                got = rp.record_pass(store, self.conn, kw.pop("exclusion"), kw.pop("types"),
                                     **kw)
                self.assertEqual(got["refused"], refused)
                self.assertEqual(self.conn.execute(
                    "SELECT count(*) FROM search.chunks").fetchone()[0], 0)

    def test_sender_purge_removes_an_excluded_senders_rows_in_any_unit(self):
        writer = self.st.ChunkWriter(self.conn)
        writer.write([self.chunk(1, "Kept message about lavender soap orders."),
                      self.chunk(2, "Excluded sender message in a colleague's mailbox.")])
        self.assertEqual(self.st.purge_objects(self.conn, {"O2", "O-not-stored"}), 1)
        left = [r[0] for r in self.conn.execute("SELECT object_id FROM search.chunks")]
        self.assertEqual(left, ["O1"])

    def test_card_purge_dry_run_counts_only_and_apply_masks_words_and_embeddings(self):
        import contextlib
        import dataclasses
        import io
        from operating.embed import card_purge
        dim = self.st.column_dim(self.conn)
        cards = ("4111 1111 1111 1111", "5500-0000-0000-0004", "378282246310005")
        rows = []
        for i in range(60):   # N=60: every card row purged once, every other row untouched
            card = cards[i % 3] if i % 4 == 0 else None
            chunk = self.chunk(i, f"Order {i} for lavender soap, PO 1045{i:02d}, lot "
                                  f"2026110912{i:03d}, call 416-555-0199"
                                  + (f", paid by Visa {card} exp 09/27." if card else "."))
            if card:   # an index written before chunks were masked
                chunk = dataclasses.replace(chunk, body=chunk.body.replace("[card]", card),
                                            title=f"Card {card}" if i == 0 else chunk.title)
                self.assertIn(card, chunk.body)
            rows.append(chunk)
        self.st.ChunkWriter(self.conn).write(rows)
        self.conn.execute("UPDATE search.chunks SET embedding = %s::halfvec, embed_model = 'm'",
                          (self.st.vector_text(np.ones(4, dtype=np.float32), dim),))
        self.conn.execute(
            "INSERT INTO search.entities (entity_id, object_id, type, name, alias, normalized, "
            "audience_key, markings) VALUES ('E1', 'O1', 'contact', 'Card holder', "
            "ARRAY['4111111111111111', 'jane'], 'card holder', 'mail:box-a', '{}'), "
            "('E2', 'O2', 'item', 'Soap 4006381333931', ARRAY['4006381333931'], 'soap', "
            "'mail:box-a', '{}')")
        self.conn.commit()
        dsn = f"postgresql:///{self.db}"
        before = self.conn.execute("SELECT md5(string_agg(chunk_id || body || content_sha, '' "
                                   "ORDER BY chunk_id)) FROM search.chunks").fetchone()[0]

        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(card_purge.main(["--dsn", dsn, *args]), 0)
            text = out.getvalue()
            for card in cards + ("4111111111111111",):
                self.assertNotIn(card, text)
            self.assertNotIn("O1", text)
            return json.loads(text)
        dry = run()
        self.assertEqual((dry["mode"], dry["chunks"]["with_card"], dry["chunks"]["by_column"],
                          dry["chunks"]["embeddings_to_drop"], dry["entities"]["with_card"]),
                         ("dry_run", 15, {"body": 15, "title": 1}, 15, 1))
        self.conn.rollback()
        self.assertEqual(self.conn.execute(
            "SELECT md5(string_agg(chunk_id || body || content_sha, '' ORDER BY chunk_id)) "
            "FROM search.chunks").fetchone()[0], before)          # a dry run writes nothing
        done = run("--apply")
        self.assertEqual((done["chunks"]["with_card"], done["chunks"]["embeddings_dropped"],
                          done["entities"]["by_column"]), (15, 15, {"alias": 1}))
        self.conn.rollback()
        stored = {r[0]: r[1:] for r in self.conn.execute(
            "SELECT chunk_id, title, body, content_sha, embedding IS NULL FROM search.chunks")}
        self.assertEqual(len(stored), 60)
        for chunk in rows:
            title, body, sha, no_vector = stored[chunk.chunk_id]
            self.assertFalse(any(c in body or c in (title or "") for c in cards))
            masked = dataclasses.replace(chunk, body=chunk.body.replace(
                next((c for c in cards if c in chunk.body), "\0"), "[card]"))
            self.assertEqual((body, sha), (masked.body, masked.content_sha))
            self.assertEqual(no_vector, "[card]" in body)        # only purged rows re-embed
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM search.chunks WHERE tsv @@ to_tsquery('simple', '4111') "
            "OR tsv @@ to_tsquery('simple', '378282246310005') OR body LIKE '%0000-0004%'"
        ).fetchone()[0], 0)                                       # no word of a card left
        self.assertEqual(self.conn.execute(
            "SELECT alias, name FROM search.entities ORDER BY entity_id").fetchall(),
            [(["[card]", "jane"], "Card holder"),
             (["4006381333931"], "Soap 4006381333931")])          # a barcode stays
        self.conn.rollback()
        again = run()
        self.assertEqual((again["chunks"]["with_card"], again["entities"]["with_card"]), (0, 0))

    def test_vectors_are_padded_to_the_column_never_truncated(self):
        self.assertEqual(self.st.column_dim(self.conn) is not None, True)
        dim = self.st.column_dim(self.conn)
        text = self.st.vector_text(np.ones(2, dtype=np.float32), dim)
        self.assertEqual(text.count(",") + 1, dim)
        with self.assertRaises(ValueError):
            self.st.vector_text(np.ones(dim + 1, dtype=np.float32), dim)


if __name__ == "__main__":
    unittest.main()
