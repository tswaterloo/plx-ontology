"""Mail documents in the projection and stored mention links (decision
2026-10-02-mail-and-mentions-store).

A versioned mail capture (``integrations.capture_mail``, filled over an injected
Graph transport) and synthetic Portal purchase orders are projected under the
curated schema with the ``documents`` domain; ``ontology.mentions.store_mention_links``
writes mention links into the same generation, and ``plx.object`` / ``plx.around``
reach them per reader. No network, no credentials. Evidence: injected-transport.
"""

from __future__ import annotations

import contextlib
from contextlib import closing
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock
import zipfile

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, READER_A, SCOPE,
                                                   Clock, FakeGraph, capture_audience,
                                                   file_attachment, reader_audience)
from ontology import identifiers, mentions, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import (CAPTURE_SOURCES, CapturedRecord, MailCaptures, build_store,
                              capture_source)
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2026-09-01T10:00:00.000000Z"
OUTSIDER = Actor("plx", frozenset({"staff:outsider"}), "restricted")
LINK = "Email.mentions_purchase_order"
RESTATED = "2026-10-05T10:00:00.000000Z"


def po(relation, key, row):
    native = json.dumps(key, separators=(",", ":"))
    return CapturedRecord("portal_api", relation, native, row,
                          f"api://{relation}/{list(key.values())[0]}@g-1", "row",
                          "0" * 64, T0, None, T0, f"api:{relation}")


class StaticPortal:
    source = "portal_api"

    def __init__(self, records):
        self._records = records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


def text_index(path):
    """The document-text index of a store: its fields, chunks and every word instance
    of its FTS table (``fts5vocab``)."""
    with closing(sqlite3.connect(str(path))) as db:
        db.execute("CREATE VIRTUAL TABLE temp.words USING fts5vocab(main, chunk_text, "
                   "'instance')")
        return {"fields": db.execute("SELECT * FROM text_fields ORDER BY field_id").fetchall(),
                "chunks": db.execute("SELECT * FROM text_chunks ORDER BY chunk_id").fetchall(),
                "words": db.execute("SELECT term, doc, col, offset FROM temp.words "
                                    "ORDER BY 1, 2, 3, 4").fetchall()}


def _die(*_args):
    """A document worker that dies (``ontology.project_parallel._parse`` replaced)."""
    os._exit(3)


def captured_mail_with_documents(case: unittest.TestCase) -> Path:
    """A mail capture (``<root>/capture``) of classified documents (a packing list, a
    freight quote, a proforma invoice), one of them also inside a zip (a container's
    member), over several messages; removed when ``case`` ends."""
    from ontology.tests.test_document_fields import CONFIRMATION, QUOTE, SLIP, pdf
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("slip.pdf", pdf(SLIP))
    graph = FakeGraph()
    for n, pages in enumerate((SLIP, QUOTE, CONFIRMATION, SLIP)):
        graph.put(BOX_A, f"MSG-D{n}", f"CK-D{n}", subject=f"Documents {n}",
                  body=f"Attached for PO10023{n}.",
                  attachments=[file_attachment(f"ATT-D{n}", pdf(pages), f"doc{n}.pdf"),
                               file_attachment(f"ZIP-D{n}", archive.getvalue(),
                                               f"docs{n}.zip")])
    root = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, root, True)
    capture = MailCaptureStore(root / "capture", clock=Clock())
    case.addCleanup(capture.close)
    api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
        sleep=lambda s: None, jitter=lambda: 0.0))
    run = MailCapture(capture, GraphMailReader(api, capture_audience()), CAPTURE,
                      sleep=lambda s: None).run(SCOPE)
    case.assertTrue(run["complete"], run)
    return root


class MailDocumentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "state"
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-1", "CK-1-1", body="Please confirm our PO100234 by Friday.",
                  subject="Order confirmation",
                  attachments=[file_attachment("ATT-1", name="PO100235 packing list.pdf")])
        graph.put(BOX_A, "MSG-2", "CK-2-1", body="Re: PO100236, freight booked.",
                  subject="Freight")
        graph.put(BOX_B, "MSG-B1", "CKB-1", body="Finance note on PO100234.",
                  subject="Finance")
        # A Portal sourcing request is not served by the reporting API: the brief email
        # (and a copy in another mailbox) is the only record of SR-0042 a reader can reach.
        graph.put(BOX_A, "MSG-SR", "CK-SR-1", subject="PLX sourcing brief SR-0042",
                  body="VP-0003 holds SR-0042; SR-20260115-0003")   # preview: 40 chars
        graph.put(BOX_B, "MSG-B-SR", "CKB-SR-1", subject="Sourcing cost SR-0042",
                  body="Finance view of SR-0042.")
        cls.store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph,
                                        retry=BoundedBackoff(sleep=lambda s: None,
                                                             jitter=lambda: 0.0))
        run = MailCapture(cls.store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
        cls.compiled = schema_loader.load_schema()
        portal = StaticPortal([
            po("public.app_purchase_order", {"id": "p1"}, {"id": "p1", "po_number": "PO100234"}),
            po("public.app_purchase_order", {"id": "p2"}, {"id": "p2", "po_number": "PO100235"}),
            po("public.app_purchase_order", {"id": "p3"}, {"id": "p3", "po_number": "PO100236"}),
            po("public.generated_purchase_order", {"id": 9}, {"id": 9, "ponumber": "PO100236"}),
        ])
        cls.portal = portal
        cls.path = Path(cls.tmp.name) / "ontology.sqlite"
        with MailCaptures(cls.state, internal_domains=SCOPE.internal_domains) as mail:
            cls.report = build_store(cls.path, cls.compiled,
                                     {"portal_api": portal, "graph_mail": mail},
                                     clock=lambda: NOW,
                                     post_project={"mentions": mentions.store_mention_links})
        cls.conn = sqlite3.connect(cls.path)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.store.close()
        cls.tmp.cleanup()

    def object_id(self, prefix):
        return self.conn.execute("SELECT object_id FROM objects WHERE object_id LIKE ? "
                                 "ORDER BY object_id", (prefix + "%",)).fetchone()[0]

    def email(self, message_id):
        return self.conn.execute(
            "SELECT object_id FROM objects WHERE object_type = 'Email' AND "
            "json_extract(native_key, '$.id') = ?", (message_id,)).fetchone()[0]

    def tools(self, audience=None, api="api:*", grammar=None):
        policy = ot.ReadPolicy(tenant="plx", audiences={
            api: {"staff:orders", "staff:outsider"},
            "mail:*": {"staff:orders", "staff:outsider"}},
            markings={m: {"staff:orders"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        base = ot.OntologyTools(reader, policy,
                                item_gate=self.store.item_gate(audience or reader_audience()),
                                schema=ot.SchemaView.from_compiled(self.compiled),
                                grammar=grammar, contracts=CONTRACTS, clock=lambda: NOW)
        return base, tt.TraverseTools(base)

    def ok(self, name, result):
        self.assertEqual(validate(result, CONTRACTS["tools"][name]["output"], root=CONTRACTS),
                         [], json.dumps(result)[:1500])
        self.assertNotIn("error", result, json.dumps(result)[:1500])
        return result

    def test_mail_is_registered_as_a_capture_source_and_projected_as_documents(self):
        self.assertIs(CAPTURE_SOURCES["graph_mail"], MailCaptures)
        with capture_source("graph_mail", self.state) as mail:
            self.assertEqual(mail.source, "graph_mail")
        types = dict(self.conn.execute("SELECT object_type, COUNT(*) FROM objects WHERE "
                                       "object_type IN ('Email', 'Attachment') "
                                       "GROUP BY object_type").fetchall())
        self.assertEqual(types, {"Email": 5, "Attachment": 1})
        keys = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_type = 'Email'")}
        self.assertEqual(keys, {f"mail:{BOX_A}", f"mail:{BOX_B}"})
        handles = {r[0] for r in self.conn.execute(
            "SELECT v.l1_handle FROM object_versions v JOIN objects o USING (object_id) "
            "WHERE o.object_type = 'Email'")}
        self.assertIn(graph_read.mail_citation(BOX_A, "MSG-1", "CK-1-1"), handles)
        attached = self.conn.execute("SELECT tier FROM links WHERE link_type = "
                                     "'Attachment.attached_to'").fetchall()
        self.assertEqual([r[0] for r in attached], ["verified"])

    def test_derived_relations_are_fingerprinted_from_what_they_are_read_from(self):
        """``documentFields`` and ``containerMembers`` are read from the captured bytes; the
        incremental build fingerprints them from their attachments' records, which forms
        of the bytes are on disk and the reading code, without reading a file: bytes gone
        from disk change those prints and no other."""
        relations = ["attachments", "containerMembers", "documentFields", "messages"]

        def prints():
            with MailCaptures(self.state, internal_domains=SCOPE.internal_domains) as mail:
                mail.records(relations)
                with unittest.mock.patch("ontology.document_fields.captured",
                                         side_effect=AssertionError("read a document")), \
                        unittest.mock.patch("ontology.project_graph.member_records",
                                            side_effect=AssertionError("read a container")):
                    return mail.fingerprint(relations)
        before = prints()
        capture = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        try:
            digest = capture.execute("SELECT content_sha256 FROM attachments WHERE "
                                     "content_sha256 IS NOT NULL LIMIT 1").fetchone()
        finally:
            capture.close()
        stored = [p for p in (self.state / "mail-capture" / "blobs").rglob("*")
                  if p.is_file() and digest and p.name.startswith(digest[0])]
        self.assertTrue(stored, "the attachment's bytes are on disk")
        moved = [p.rename(p.with_name(p.name + ".moved")) for p in stored]
        try:
            after = prints()
        finally:
            for p in moved:
                p.rename(p.with_name(p.name[:-len(".moved")]))
        self.assertEqual(prints(), before)
        self.assertEqual({r for r in relations if before[r] != after[r]},
                         {"containerMembers", "documentFields"})

    def test_a_refresh_reads_no_unchanged_message_and_resolves_only_what_moved(self):
        """The document steps keep what they derived from each message and its attachments
        (``ontology.mention_memo``): a refresh reads no unchanged message again, extracts
        nothing and resolves again only the message naming an identifier whose index rows
        changed (a purchase order's number moved); a message whose file reading failed
        for a reason not remembered is read again by the next build. Every generation
        equals a cold build with the same steps."""
        from ontology import project, project_incremental as inc
        from ontology.text_index import store_text_index

        def portal(number):
            return StaticPortal([
                po("public.app_purchase_order", {"id": "p1"},
                   {"id": "p1", "po_number": "PO100234"}),
                po("public.app_purchase_order", {"id": "p2"},
                   {"id": "p2", "po_number": "PO100235"}),
                po("public.app_purchase_order", {"id": "p3"}, {"id": "p3", "po_number": number}),
                po("public.generated_purchase_order", {"id": 9},
                   {"id": 9, "ponumber": "PO100236"})])

        def pinned(need):   # a world of a few rows: keep the incremental path under test
            if need["mode"] == "cold" and "incremental" in need["estimates"]:
                raise OSError("incremental_path_pinned")

        steps = {"mentions": mentions.store_mention_links, "text_index": store_text_index}
        read, extracted, resolved = [], [], []
        load, extract, resolve = (MailCaptures._message_documents, mentions.document_mentions,
                                  mentions._document_links)

        def counted_load(mail, row, attached):
            read.append(row["l1_handle"])
            return load(mail, row, attached)

        def counted_extract(document):
            extracted.append(document.citation)
            return extract(document)

        def counted_resolve(conn, declared, document, *args):
            resolved.append(document.citation)
            return resolve(conn, declared, document, *args)
        with tempfile.TemporaryDirectory() as tmp:
            def build(number, remembered=True, **options):
                read.clear(), extracted.clear(), resolved.clear()
                with MailCaptures(self.state, internal_domains=SCOPE.internal_domains) as mail, \
                        unittest.mock.patch.object(MailCaptures, "_message_documents",
                                                   counted_load), \
                        unittest.mock.patch.object(mentions, "document_mentions",
                                                   counted_extract), \
                        unittest.mock.patch.object(mentions, "_document_links",
                                                   counted_resolve), \
                        unittest.mock.patch.object(project, "readings_remembered",
                                                   return_value=remembered):
                    return inc.build_generation(
                        Path(tmp) / "projection", self.compiled,
                        {"portal_api": portal(number), "graph_mail": mail}, clock=lambda: NOW,
                        post_project=steps, **options)

            def cold(number, name):
                with MailCaptures(self.state, internal_domains=SCOPE.internal_domains) as mail:
                    return build_store(Path(tmp) / name, self.compiled,
                                       {"portal_api": portal(number), "graph_mail": mail},
                                       clock=lambda: NOW, post_project=steps)
            messages = 5   # the fixture mailbox: MSG-1, MSG-2, MSG-B1, MSG-SR, MSG-B-SR
            first = build("PO100236", remembered=False)
            self.assertTrue(extracted and resolved)
            self.assertEqual(len(read), 2 * messages)   # the two document steps
            self.assertEqual(first.content_digest, cold("PO100236", "c1.sqlite")["content_digest"])
            again = build("PO100236", space=pinned)   # no reading was kept as read: read again
            self.assertEqual((len(read), extracted), (messages, []))
            self.assertEqual(again.content_digest, first.content_digest)
            moved = build("PO100299", space=pinned)
            self.assertEqual(moved.mode, "incremental")
            self.assertEqual(moved.content_digest,
                             cold("PO100299", "c2.sqlite")["content_digest"])
            self.assertEqual((read, extracted), ([], []))
            self.assertEqual(resolved, [graph_read.mail_citation(BOX_A, "MSG-2", "CK-2-1")])
            with closing(sqlite3.connect(Path(tmp) / "c1.sqlite")) as before, \
                    closing(sqlite3.connect(moved.store)) as after:
                query = "SELECT link_id, to_object, tier FROM links WHERE binding_kind = " \
                        "'mention' ORDER BY link_id"
                self.assertNotEqual(before.execute(query).fetchall(),
                                    after.execute(query).fetchall())
                text = "SELECT citation, field_path, object_id, text_digest FROM text_fields " \
                       "ORDER BY 1, 2, 3"
                self.assertEqual(before.execute(text).fetchall(), after.execute(text).fetchall())

    def test_a_body_read_that_failed_once_is_read_again_by_the_next_refresh(self):
        """A mail body blob whose read returned nothing
        once (an OSError, altered bytes) was kept as read, so every later refresh served
        the message without its body (no text index field, no mention). The group is not
        kept: the next refresh reads it again and equals a cold build."""
        from ontology import project_incremental as inc
        from ontology.text_index import store_text_index

        def portal(number):
            return StaticPortal([
                po("public.app_purchase_order", {"id": "p1"},
                   {"id": "p1", "po_number": "PO100234"}),
                po("public.app_purchase_order", {"id": "p3"}, {"id": "p3", "po_number": number})])

        def pinned(need):
            if need["mode"] == "cold" and "incremental" in need["estimates"]:
                raise OSError("incremental_path_pinned")
        steps = {"mentions": mentions.store_mention_links, "text_index": store_text_index}
        real = MailCaptures._blob_text
        with tempfile.TemporaryDirectory() as tmp:
            def build(number, fail=False, **options):
                with MailCaptures(self.state, internal_domains=SCOPE.internal_domains) as mail, \
                        unittest.mock.patch.object(MailCaptures, "_blob_text",
                                                   (lambda _s, _d: None) if fail else real):
                    return inc.build_generation(
                        Path(tmp) / "projection", self.compiled,
                        {"portal_api": portal(number), "graph_mail": mail}, clock=lambda: NOW,
                        post_project=steps, **options)
            build("PO100236", fail=True)
            after = build("PO100299", space=pinned)
            with MailCaptures(self.state, internal_domains=SCOPE.internal_domains) as mail:
                cold = build_store(Path(tmp) / "cold.sqlite", self.compiled,
                                   {"portal_api": portal("PO100299"), "graph_mail": mail},
                                   clock=lambda: NOW, post_project=steps)
            query = "SELECT citation, field_path FROM text_fields WHERE field_path = " \
                    "'row.body_text' ORDER BY 1"
            with closing(sqlite3.connect(after.store)) as built, \
                    closing(sqlite3.connect(Path(tmp) / "cold.sqlite")) as expected:
                self.assertEqual(len(expected.execute(query).fetchall()), 5)
                self.assertEqual(built.execute(query).fetchall(),
                                 expected.execute(query).fetchall())
            self.assertEqual(after.mode, "incremental")
            self.assertEqual(after.content_digest, cold["content_digest"])

    def test_a_build_with_document_workers_equals_the_serial_one(self):
        """Documents parsed (documentFields) and their mentions extracted by the build's
        worker processes (``ontology.project_parallel.DocumentPool``): the generation, its
        report and every table equal a serial build's, and so does what the build keeps
        (the fields, readings and document memo files: same keys, same values). A worker
        that dies leaves its documents to the build's own process: still equal."""
        from integrations import captured_text
        from ontology import document_fields
        from ontology import project_incremental as inc
        from ontology import project_parallel as par
        from ontology.tests.test_project_incremental import tables
        from ontology.text_index import store_text_index

        steps = {"mentions": mentions.store_mention_links, "text_index": store_text_index}
        pools = []
        held = par.document_pool

        @contextlib.contextmanager
        def watched(count):
            with held(count) as pool:
                pools.append(pool)
                yield pool

        root = captured_mail_with_documents(self)

        def build(state, workers):
            with MailCaptures(root / "capture", internal_domains=SCOPE.internal_domains) \
                    as mail, \
                    unittest.mock.patch.object(captured_text, "_CACHE",
                                               captured_text._CACHE.empty()), \
                    unittest.mock.patch.object(captured_text, "_MEMBER_READINGS",
                                               captured_text._MEMBER_READINGS.empty()), \
                    unittest.mock.patch.object(document_fields, "_FIELDS",
                                               document_fields._FIELDS.empty()):
                return inc.build_generation(
                    state, self.compiled, {"portal_api": self.portal, "graph_mail": mail},
                    clock=lambda: NOW, post_project=steps, workers=workers)

        def kept(state):
            out = {}
            for name, query in (
                    ("fields.sqlite", "SELECT key, code, value, used FROM fields"),
                    ("readings.sqlite", "SELECT key, code, value, used FROM readings"),
                    ("member-readings.sqlite",
                     "SELECT key, code, value, used FROM members"),
                    ("mentions.sqlite", "SELECT 'm', key, code, value, used FROM mentions "
                                        "UNION ALL SELECT 't', key, code, value, used FROM texts "
                                        "UNION ALL SELECT 'g', key, '', value, used FROM groups")):
                with closing(sqlite3.connect(state / name)) as db:
                    out[name] = sorted(db.execute(query).fetchall())
            return out

        with tempfile.TemporaryDirectory() as tmp:
            serial = build(Path(tmp) / "serial", 1)
            expected = kept(Path(tmp) / "serial")
            self.assertTrue(expected["fields.sqlite"])
            self.assertTrue(expected["member-readings.sqlite"])   # the zips' members
            self.assertTrue(expected["mentions.sqlite"])
            with closing(sqlite3.connect(serial.store)) as db:   # typed documents, members too
                self.assertEqual(dict(db.execute(
                    "SELECT object_type, COUNT(*) FROM objects WHERE object_type IN "
                    "('PackingSlip', 'ForwarderQuote', 'SupplierConfirmation') "
                    "GROUP BY 1").fetchall()),
                    {"PackingSlip": 10, "ForwarderQuote": 1, "SupplierConfirmation": 1})
            small = unittest.mock.patch.multiple(par, MIN_PARSE_RECORDS=1, PARSE_CHUNK=1)
            with small, unittest.mock.patch.multiple(mentions, MENTION_AHEAD=1,
                                                     MENTION_TASK=1), \
                    unittest.mock.patch.object(par, "document_pool", watched):
                parallel = build(Path(tmp) / "parallel", 3)
                pool = pools[-1]
                self.assertGreater(pool.tasks, 1)
                self.assertEqual((pool.fallback, pool.broken), (0, False))
                # A source a worker opens again reading other bytes than the build pinned:
                # its groups are read by the build itself.
                spec = par._source_spec
                with unittest.mock.patch.object(
                        par, "_source_spec",
                        lambda *a: (*spec(*a)[:-1], "another watermark")):
                    drifted = build(Path(tmp) / "drifted", 3)
                self.assertGreater(pools[-1].fallback, 0)
                with unittest.mock.patch.object(par, "_parse", _die):
                    died = build(Path(tmp) / "died", 3)
                self.assertTrue(pools[-1].broken)
            for generation, state in ((parallel, "parallel"), (drifted, "drifted"),
                                      (died, "died")):
                self.assertEqual(generation.content_digest, serial.content_digest)
                self.assertEqual(generation.report,
                                 {**serial.report, "store": str(generation.store)})
                self.assertEqual(tables(generation.store), tables(serial.store))
                self.assertEqual(text_index(generation.store), text_index(serial.store))
                self.assertEqual(kept(Path(tmp) / state), expected, state)

    def test_a_file_reading_is_remembered_only_when_the_reading_memo_keeps_it(self):
        """``ontology.project.readings_remembered`` reads ``captured_text``'s memo keys: a
        reading that succeeded is remembered, one that failed transiently is not (a change
        of the memo's key shape breaks this, not the document memo silently)."""
        from integrations import captured_text
        from ontology.project import readings_remembered

        with MailCaptures(self.state, internal_domains=SCOPE.internal_domains) as mail:
            rows = [{**json.loads(item[0]), "content_sha256": item[1], "text_status": item[2],
                     "text_sha256": item[3]} for item in mail._conn.execute(
                "SELECT metadata, content_sha256, text_status, text_sha256 FROM attachments "
                "WHERE content_sha256 IS NOT NULL")]
            self.assertTrue(rows)
            with unittest.mock.patch.object(captured_text, "_CACHE",
                                            captured_text._CACHE.empty()), \
                    unittest.mock.patch.object(captured_text, "read_text",
                                               side_effect=OSError("transient")):
                for row in rows:
                    self.assertIsNone(captured_text.captured_text(mail.blobs, row))
                self.assertFalse(readings_remembered(mail.blobs, rows))
            with unittest.mock.patch.object(captured_text, "_CACHE",
                                            captured_text._CACHE.empty()):
                for row in rows:
                    captured_text.captured_text(mail.blobs, row)
                    captured_text.container_members(mail.blobs, row)
                self.assertTrue(readings_remembered(mail.blobs, rows))

    def test_a_captured_file_is_read_once_across_builds_under_the_same_reading_code(self):
        """Builds of one state directory keep the readings of captured files they made
        (``integrations.captured_text.KeptReadings``): the next build reads none of the
        files again and stores the same generation; a change of the reading code reads
        them again."""
        from integrations import captured_text
        from ontology import project_incremental as inc

        def build(state):
            with MailCaptures(self.state, internal_domains=SCOPE.internal_domains) as mail:
                return inc.build_generation(
                    state, self.compiled, {"portal_api": self.portal, "graph_mail": mail},
                    clock=lambda: NOW, post_project={"mentions": mentions.store_mention_links})
        read_text = captured_text.read_text
        reads = []

        def counted(*args, **kwargs):
            reads.append(1)
            return read_text(*args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "projection"
            with unittest.mock.patch.object(captured_text, "_CACHE",
                                            captured_text._CACHE.empty()), \
                    unittest.mock.patch.object(captured_text, "read_text", counted):
                first = build(state)
            self.assertTrue(reads)
            self.assertTrue((state / inc.READINGS).is_file())
            # Without the document steps' memo (``ontology.mention_memo``, which would read
            # no document at all) every document is read again, its files from the readings.
            for path in state.glob("mentions.sqlite*"):
                path.unlink()
            memo = captured_text._CACHE.empty()
            with unittest.mock.patch.object(captured_text, "_CACHE", memo), \
                    unittest.mock.patch.object(captured_text, "read_text",
                                               side_effect=AssertionError("read again")):
                again = build(state)
            self.assertEqual((again.content_digest, memo.unexpected),
                             (first.content_digest, 0))
            self.assertTrue(len(memo))   # the readings came from the kept ones
            reads.clear()

            def cold_store(name):
                with MailCaptures(self.state,
                                  internal_domains=SCOPE.internal_domains) as mail:
                    return build_store(Path(tmp) / name, self.compiled,
                                       {"portal_api": self.portal, "graph_mail": mail},
                                       clock=lambda: NOW,
                                       post_project={"mentions": mentions.store_mention_links})
            # The code that derives documentFields and containerMembers changed (the
            # projection, its steps), the reading code did not: their units are projected
            # again (never taken from the last build's fingerprints) from the kept
            # readings, no file is read again, and the generation equals a cold build.
            with unittest.mock.patch.object(captured_text, "_CACHE",
                                            captured_text._CACHE.empty()), \
                    unittest.mock.patch.object(captured_text, "read_text", counted), \
                    unittest.mock.patch("ontology.project.derivation_code",
                                        return_value="other derivation code"):
                derived = build(state)
                cold = cold_store("cold-derived.sqlite")
            self.assertEqual(reads, [])
            self.assertEqual(derived.mode, "incremental")
            self.assertGreater(derived.units_rebuilt, 0)
            self.assertEqual(derived.content_digest, cold["content_digest"])
            # The reading code changed: the files are read again.
            with unittest.mock.patch.object(captured_text, "_CACHE",
                                            captured_text._CACHE.empty()), \
                    unittest.mock.patch.object(captured_text, "read_text", counted), \
                    unittest.mock.patch.object(captured_text, "readings_code",
                                               return_value="other reading code"):
                changed = build(state)
            self.assertTrue(reads)
            self.assertEqual(changed.content_digest, cold["content_digest"])

    def test_the_readings_key_moves_with_the_reading_code_only(self):
        """The kept readings' key (``captured_text.readings_code``) names the modules
        that read files and the libraries they read with: an edit of the projection, its
        steps or the host keeps every kept reading; an edit of a reading module or another
        library version reads the files again."""
        import shutil
        from integrations import captured_text
        root = Path(captured_text.__file__).resolve().parents[1]
        others = ("ontology/document_fields.py", "operating/host.py",
                  "ontology/project_incremental.py", "ontology/mentions.py")
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp)
            for name in [m.replace(".", "/") + ".py" for m in captured_text.READING_MODULES
                         ] + list(others):
                (copy / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / name, copy / name)
            self.assertEqual(captured_text.readings_code(root=copy),
                             captured_text.readings_code())
            key = captured_text.readings_code(root=copy)
            for name in others:
                with open(copy / name, "a", encoding="utf-8") as edited:
                    edited.write("\n# edited\n")
            self.assertEqual(captured_text.readings_code(root=copy), key)
            with open(copy / "integrations/document_understanding.py", "a",
                      encoding="utf-8") as edited:
                edited.write("\n# edited\n")
            self.assertNotEqual(captured_text.readings_code(root=copy), key)
        from importlib import metadata
        real, before = metadata.version, captured_text.readings_code()
        with unittest.mock.patch.object(metadata, "version", side_effect=lambda name: (
                "999.0" if name == "pypdf" else real(name))):
            self.assertNotEqual(captured_text.readings_code(), before)

    def test_mentions_are_stored_lead_links_in_the_same_generation(self):
        step = self.report["post_project"]["mentions"]
        self.assertEqual(step["tier"], "lead")
        rows = self.conn.execute("SELECT * FROM links WHERE binding_kind = 'mention' "
                                 "ORDER BY link_id").fetchall()
        self.assertEqual(step["counts"]["links"], len(rows))
        self.assertEqual({r["tier"] for r in rows}, {"lead"})
        self.assertEqual(self.report["counts"]["links"],
                         self.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
        by_email = {}
        for r in rows:
            by_email.setdefault(r["from_object"], set()).add(r["to_object"])
        p1 = self.object_id("PurchaseOrder:portal.app_purchase_order:")
        self.assertEqual(len(by_email[self.email("MSG-1")]), 2)   # body + attachment name
        # An ambiguous number keeps every candidate as a lead in one mention group.
        # Ambiguity itself is never stored (it is decided per reader on read).
        msg2 = [r for r in rows if r["from_object"] == self.email("MSG-2")]
        self.assertEqual(len(msg2), 2)
        groups = set()
        for r in msg2:
            why = {f["feature"] for f in json.loads(r["why_not"])}
            self.assertNotIn("ambiguous", why)
            self.assertIn("external_assertion", why)   # an external sender wrote it
            groups |= {f.get("mention_group") for f in json.loads(r["reason"])
                       if f["feature"] == "identifier_mention"}
            self.assertNotIn("candidates", json.dumps(json.loads(r["why_not"])))
        self.assertEqual(len(groups), 1)
        for r in rows:
            evidence = json.loads(r["evidence_handles"])
            self.assertTrue(all(h.startswith(("graph-mail://", "api://")) for h in evidence))
        self.assertTrue(any(r["to_object"] == p1 for r in rows))

    def test_plx_object_and_around_reach_mentions_for_a_reader_who_may_see_the_mail(self):
        base, traverse = self.tools()
        p1 = self.object_id("PurchaseOrder:portal.app_purchase_order:")
        out = self.ok("plx.object", base.call("plx.object", {"object_id": p1}, READER_A))
        counts = {(c["link_type"], c["direction"]): c["count"] for c in out["linked_counts"]}
        # MSG-1 mentions it; MSG-B1 (finance mailbox) is withheld from this reader.
        self.assertEqual(counts.get((LINK, "reverse")), 1)
        around = self.ok("plx.around", traverse.call("plx.around", {
            "from": self.email("MSG-1"), "path": [{"link_type": LINK,
                                                    "direction": "forward"}],
            "tier_floor": "lead"}, READER_A))
        reached = {o["object_id"] for o in around["objects"]}
        self.assertIn(p1, reached)
        self.assertIn(self.object_id(
            "PurchaseOrder:portal.app_purchase_order:{\"id\":\"p2\"}"), reached)
        back = self.ok("plx.around", traverse.call("plx.around", {
            "from": p1, "path": [{"link_type": LINK, "direction": "reverse"}],
            "tier_floor": "lead"}, READER_A))
        self.assertEqual({o["object_id"] for o in back["objects"]}, {self.email("MSG-1")})

    def mention_edges(self, traverse, root):
        out = self.ok("plx.around", traverse.call("plx.around", {
            "from": root, "path": [{"link_type": LINK, "direction": "forward"}],
            "tier_floor": "lead", "response_format": "detailed"}, READER_A))
        return {e["to"]: e for e in out["edges"]}

    def test_ambiguity_is_decided_per_reader_from_visible_candidates_only(self):
        # MSG-2 names PO100236, held by Portal PO p3 and
        # FM generated PO 9. A reader who sees both is told the mention is ambiguous;
        # a reader denied the FM relation sees one plain lead and no hint of the other.
        p3 = self.object_id('PurchaseOrder:portal.app_purchase_order:{"id":"p3"}')
        _base, both = self.tools()
        edges = self.mention_edges(both, self.email("MSG-2"))
        self.assertEqual(len(edges), 2)
        for edge in edges.values():
            self.assertIn("ambiguous", {f["feature"] for f in edge["why_not"]})
            self.assertEqual(edge["tier"], "lead")
        _base, portal_only = self.tools(api="api:public.app_*")
        edges = self.mention_edges(portal_only, self.email("MSG-2"))
        self.assertEqual(list(edges), [p3])
        self.assertEqual(edges[p3]["tier"], "lead")
        self.assertNotIn("ambiguous", {f["feature"] for f in edges[p3]["why_not"]})
        self.assertNotIn("generated_purchase_order", json.dumps(edges))
        # A single-candidate mention is never ambiguous for anyone.
        edges = self.mention_edges(both, self.email("MSG-1"))
        self.assertTrue(edges)
        self.assertTrue(all("ambiguous" not in {f["feature"] for f in e["why_not"]}
                            for e in edges.values()))

    def test_link_tiers_count_the_mention_links(self):
        counts = self.report["counts"]
        tiers = {k: v for k, v in counts.items() if k.startswith("links.")
                 and k.split(".", 1)[1] in store_ddl.TIERS}
        self.assertEqual(sum(tiers.values()), counts["links"])
        self.assertGreaterEqual(tiers["links.lead"],
                                self.report["post_project"]["mentions"]["counts"]["links"])

    def test_a_reader_without_the_mailbox_sees_no_mail_object_and_no_mention(self):
        base, traverse = self.tools()
        p1 = self.object_id("PurchaseOrder:portal.app_purchase_order:")
        out = self.ok("plx.object", base.call("plx.object", {"object_id": p1}, OUTSIDER))
        self.assertNotIn(LINK, json.dumps(out["linked_counts"]))
        denied = base.call("plx.object", {"object_id": self.email("MSG-1")}, OUTSIDER)
        self.assertEqual(denied["error"]["code"], "object_not_found")
        back = traverse.call("plx.around", {"from": p1, "path": [
            {"link_type": LINK, "direction": "reverse"}], "tier_floor": "lead"}, OUTSIDER)
        self.assertNotIn("graph-mail://", json.dumps(back))

    def test_an_unserved_portal_number_finds_only_the_mail_the_reader_may_see(self):
        base, _traverse = self.tools(grammar=identifiers)   # the host's typed parsing

        def found(actor):
            out = base.call("plx.find", {"query": "SR-0042"}, actor)
            self.assertNotIn("error", out, json.dumps(out)[:1500])
            return ({c["object"]["object_id"] for c in out.get("candidates", ())},
                    json.dumps(out))

        ids, text = found(READER_A)
        self.assertIn(self.email("MSG-SR"), ids)
        self.assertNotIn(self.email("MSG-B-SR"), ids)   # the finance mailbox is withheld
        self.assertNotIn("Finance view", text)
        self.assertFalse(any(i.startswith("ShipRelease:") for i in ids))
        # A number only the body holds is read from the text, and only that number:
        # VP-0004 or SR-0003 (the same digits under another kind) find nothing.
        def query(q, actor=READER_A):
            out = base.call("plx.find", {"query": q}, actor)
            return {c["object"]["object_id"] for c in out.get("candidates", ())}

        self.assertEqual(query("VP-0003"), {self.email("MSG-SR")})
        self.assertEqual(query("VP-0004"), set())
        self.assertEqual(query("SR-0003"), set())
        ids, text = found(OUTSIDER)
        self.assertEqual(ids, set())
        self.assertNotIn("graph-mail://", text)
        self.assertEqual(query("VP-0003", OUTSIDER), set())

    def test_every_object_type_with_identifiers_has_a_declared_mention_link(self):
        declared = mentions.mention_link_types(self.compiled.link_types)
        for tid, spec in self.compiled.object_types.items():
            # Documents never mention-target each other: their only identifiers are copy
            # identities (a Message-ID, a digest), never read from text.
            if spec["availability"]["state"] == "not_served" or tid in (
                    "Email", "Attachment", "SharePointFile", "ContainerMember"):
                continue
            if any(p["value_type"] == "identifier" for p in spec["properties"]):
                self.assertIn(("Email", tid), declared, tid)


class MailFreshnessTests(unittest.TestCase):
    """The mail source's freshness facts (``MailCaptures.describe``): its last refresh is
    the stalest run of a folder the capture reads; a folder left with a terminal reason
    (no mailbox, not visible, forbidden) is a gap that keeps the source partial, never
    its age (2026-11-02: 34 such folder rows held the source at a three-day-old run while
    every readable folder had been read within the hour)."""

    def test_a_terminal_folder_is_a_gap_never_the_age_of_the_source(self):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            graph = FakeGraph()
            graph.put(BOX_A, "MSG-1", "CK-1-1", body="PO100234", subject="Order")
            graph.put(BOX_B, "MSG-B1", "CKB-1", body="PO100236", subject="Freight")
            with closing(MailCaptureStore(state, clock=Clock())) as store:
                api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
                    sleep=lambda s: None, jitter=lambda: 0.0))
                run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                                  sleep=lambda s: None).run(SCOPE)
                self.assertTrue(run["complete"], run)
                readable = min(r[0] for r in store.db.execute(
                    "SELECT last_run_at FROM folders"))
                for reason in ("not_visible", "forbidden,no_mailbox"):
                    store.save_folder(BOX_B, f"gone-{reason}", status="incomplete",
                                      reason=reason, last_run_at="2026-01-01T00:00:00Z")
                # A readable folder the budget left unread for long still ages the source.
                with MailCaptures(state, internal_domains=SCOPE.internal_domains) as mail:
                    facts = mail.describe()["relations"]["messages"]
                self.assertEqual(facts["last_refresh_at"], readable)
                self.assertIsNone(facts["last_full_traversal_at"])     # partial: the gaps
                store.save_folder(BOX_B, "behind", status="incomplete", reason="time_budget",
                                  last_run_at="2026-02-01T00:00:00Z")
                with MailCaptures(state, internal_domains=SCOPE.internal_domains) as mail:
                    facts = mail.describe()["relations"]["messages"]
                self.assertEqual(facts["last_refresh_at"], "2026-02-01T00:00:00Z")


class MentionCorroborationTests(unittest.TestCase):
    """ER-09 / ENG-08: a stored mail mention rises to ``corroborated`` only when two or more
    independent features agree (ontology/mentions.py MENTION_FEATURES); one rule for every
    target, a single feature stays a lead."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "state"
        graph = FakeGraph()
        day = "2026-09-20T09:00:00Z"
        for mid, sender, body in (
                ("M-CONFIRM", "sales@acme-bottles.example", "We confirm PO100301."),
                ("M-FREEMAIL", "acme.sales@gmail.com", "Status of PO100301?"),
                ("M-EARLY", "sales@acme-bottles.example", "About PO100302."),
                ("M-TWO", "sales@acme-bottles.example", "PO100301 and PO100303 ship."),
                ("M-PETRA", "buyer@example-plx.com", "Please confirm PO100304."),
                ("M-REPLY", "acme.sales@gmail.com", "Confirmed PO100304."),
                ("M-RESTATED", "sales@birch-caps.example", "Shipping PO100305.")):
            graph.put(BOX_A, mid, "CK-" + mid, body=body, subject="Order", received=day)
            graph.messages[BOX_A][mid]["from"] = {"emailAddress": {"address": sender}}
        for mid in ("M-PETRA", "M-REPLY"):
            graph.messages[BOX_A][mid]["conversationId"] = "CONV-THREAD"
        graph.messages[BOX_A]["M-PETRA"]["internetMessageHeaders"] = [
            {"name": "Authentication-Results",
             "value": "mx.example; dmarc=pass header.from=example-plx.com"}]
        cls.store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph,
                                        retry=BoundedBackoff(sleep=lambda s: None,
                                                             jitter=lambda: 0.0))
        run = MailCapture(cls.store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
        cls.compiled = compiled = schema_loader.load_schema()
        rows = [po("public.app_supplier", {"id": "s1"},
                   {"id": "s1", "name": "Acme", "website": "https://www.acme-bottles.example"}),
                # A website no URL parser reads: no domain, never a failed build.
                po("public.app_supplier", {"id": "s2"}, {"id": "s2", "website": "http://[bad"})]
        for pid, number, dated in (("p1", "PO100301", "2026-09-01"),
                                   ("p2", "PO100302", "2026-10-20"),
                                   ("p3", "PO100303", "2026-09-01"),
                                   ("p4", "PO100304", "2026-09-01")):
            rows.append(po("public.app_purchase_order", {"id": pid},
                           {"id": pid, "po_number": number, "supplier_id": "s1",
                            "date_generated": dated}))
        # A supplier a later capture recorded again (a new version from 2026-10-05, after
        # the mail, its website unchanged): its order's supplier link and its website are
        # each two stored rows that join without a gap and hold over the mention's time.
        for version, phone, since, until in (("g-1", "555-0100", T0, RESTATED),
                                             ("g-2", "555-0199", RESTATED, None)):
            rows.append(CapturedRecord(
                "portal_api", "public.app_supplier", '{"id":"s3"}',
                {"id": "s3", "name": "Birch", "website": "https://birch-caps.example",
                 "phone": phone}, f"api://public.app_supplier/s3@{version}", "row",
                version[-1] * 64, since, until, until or RESTATED, "api:public.app_supplier"))
        rows.append(po("public.app_purchase_order", {"id": "p5"},
                       {"id": "p5", "po_number": "PO100305", "supplier_id": "s3",
                        "date_generated": "2026-09-01"}))
        cls.path = Path(cls.tmp.name) / "ontology.sqlite"
        with MailCaptures(cls.state, internal_domains=SCOPE.internal_domains) as mail:
            cls.report = build_store(cls.path, compiled,
                                     {"portal_api": StaticPortal(rows), "graph_mail": mail},
                                     clock=lambda: NOW,
                                     post_project={"mentions": mentions.store_mention_links})
        cls.conn = sqlite3.connect(cls.path)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.store.close()
        cls.tmp.cleanup()

    def links(self, message_id):
        return {json.loads(r["to_object"].split(":", 2)[2])["id"]: r for r in self.conn.execute(
            "SELECT l.* FROM links l JOIN objects o ON o.object_id = l.from_object WHERE "
            "l.binding_kind = 'mention' AND l.link_type = ? AND "
            "json_extract(o.native_key, '$.id') = ?", (LINK, message_id))}

    def agreeing(self, row):
        return sorted(f["feature"] for f in json.loads(row["reason"]) if f.get("agrees"))

    def test_two_independent_features_corroborate_and_one_stays_a_lead(self):
        # The mention plus the sender at the supplier's distinctive domain.
        confirm = self.links("M-CONFIRM")["p1"]
        self.assertEqual(confirm["tier"], "corroborated")
        self.assertEqual(self.agreeing(confirm),
                         ["identifier_mention", "identity_candidate", "party_domain"])
        self.assertNotIn("uncorroborated", confirm["why_not"])
        # The corroboration cites the supplier record it read, and shows no value of it.
        self.assertIn("api://public.app_supplier/s1@g-1",
                      json.loads(confirm["evidence_handles"]))
        self.assertNotIn("acme-bottles", confirm["reason"])
        # The same number from free mail: the mention alone, a lead.
        free = self.links("M-FREEMAIL")["p1"]
        self.assertEqual(free["tier"], "lead")
        self.assertIn("uncorroborated", free["why_not"])
        # Vetoes: a message dated before the order, a message naming two orders.
        self.assertEqual(self.links("M-EARLY")["p2"]["tier"], "lead")
        self.assertEqual({k: r["tier"] for k, r in self.links("M-TWO").items()},
                         {"p1": "lead", "p3": "lead"})
        # A supplier link and website stored as two rows each that join without a gap
        # hold over the mention's time: the restated supplier's order corroborates.
        restated = self.links("M-RESTATED")["p5"]
        self.assertEqual(restated["tier"], "corroborated")
        self.assertEqual(self.agreeing(restated),
                         ["identifier_mention", "identity_candidate", "party_domain"])
        self.assertIn("api://public.app_supplier/s3@g-2",
                      json.loads(restated["evidence_handles"]))
        step = self.report["post_project"]["mentions"]
        self.assertEqual(step["link_tiers"], {"lead": step["counts"]["links"] - 3,
                                              "corroborated": 3})

    def test_a_reader_who_may_not_read_the_supplier_sees_the_lead(self):
        # plx.around and plx.object: the corroboration rests on the supplier record; a
        # reader of the order and the mail who may not read suppliers sees the lead the
        # mention is, with nothing drawn from the supplier.
        p1 = self.conn.execute("SELECT object_id FROM objects WHERE object_id LIKE "
                               "'PurchaseOrder:%\"p1\"%'").fetchone()[0]
        confirm = self.links("M-CONFIRM")["p1"]["from_object"]

        def read(api):
            base, traverse = MailDocumentsTests.tools(self, api=api)
            around = traverse.call("plx.around", {
                "from": p1, "path": [{"link_type": LINK, "direction": "reverse"}],
                "tier_floor": "lead", "response_format": "detailed"}, READER_A)
            [edge] = [e for e in around["edges"] if e["from"] == confirm]
            obj = base.call("plx.object", {"object_id": p1}, READER_A)
            [count] = [c for c in obj["linked_counts"] if c["link_type"] == LINK]
            return edge, count["tiers"]

        edge, tiers = read("api:*")
        self.assertIn("party_domain", {f["feature"] for f in edge["reason"]})
        self.assertIn("corroborated", tiers)
        edge, tiers = read("api:public.app_purchase_order")
        self.assertEqual(edge["tier"], "lead")
        self.assertNotIn("party_domain", {f["feature"] for f in edge["reason"]})
        self.assertIn("corroboration_not_visible", {f["feature"] for f in edge["why_not"]})
        self.assertNotIn("app_supplier", json.dumps(edge))
        self.assertNotIn("corroborated", tiers)

    def test_an_internal_mention_in_the_same_conversation_corroborates_the_reply(self):
        reply = self.links("M-REPLY")["p4"]
        self.assertEqual(reply["tier"], "corroborated")
        self.assertEqual(self.agreeing(reply),
                         ["identifier_mention", "identity_candidate", "petra_thread"])
        petra = self.links("M-PETRA")["p4"]
        self.assertIn(json.loads(petra["evidence_handles"])[0],
                      json.loads(reply["evidence_handles"]))
        # Petra never corroborates Petra: the internal message itself stays a lead.
        self.assertEqual(petra["tier"], "lead")


class MentionLinkRuleTests(unittest.TestCase):
    """mention-link-polymorphism (a): the per-target mention links come from one loader
    rule (ontology/schema.py MENTION_LINK_RULE), never from hand copies."""

    def documents(self):
        return schema_loader.load_documents()

    def test_no_mention_link_is_declared_by_hand(self):
        docs = dict(self.documents())
        hand = [link["id"] for doc in docs.values() for link in doc.get("link_types", [])
                if link["from"] == schema_loader.MENTION_LINK_RULE["from"]
                and any(b["kind"] == "mention" for b in link["bindings"])]
        self.assertEqual(hand, [])
        compiled = schema_loader.load_schema()
        generated = mentions.mention_link_types(compiled.link_types)
        self.assertEqual(generated[("Email", "PurchaseOrder")], LINK)
        link = compiled.link_types[LINK]
        self.assertEqual((link["reverse_name"], link["cardinality"]),
                         ("mentioned_in_email", "N:M"))
        (binding,) = link["bindings"]
        self.assertEqual((binding["kind"], binding["tier_ceiling"], binding["from_binding"]),
                         ("mention", "corroborated", "graph_mail.messages"))
        self.assertEqual([e["path"] if "path" in e else e["ref"] for e in binding["evidence"]],
                         ["ontology/mentions.py", "devloop/CONTEXT/07-ONTOLOGY-DESIGN.md#C.2"])

    def test_a_new_served_type_with_an_identifier_gets_its_link_and_a_copy_is_refused(self):
        docs = self.documents()
        extra = {"format": "plx-ontology-domain/v1", "domain": "zz_extra", "origin": "curated",
                 "description": "A synthetic domain for the mention link rule.",
                 "object_types": [{
                     "id": "ZzWidgetOrder", "description": "A synthetic order type.",
                     "availability": {"state": "served"},
                     "bindings": [{"id": "portal.zz_widget", "source": "portal_api",
                                   "relation": "public.zz_widget", "key": ["id"],
                                   "lineage": {"default": "fm_bronze"},
                                   "evidence": [{"kind": "workbench_code",
                                                 "path": "ontology/schema.py"}]}],
                     "properties": [{"name": "number", "value_type": "identifier",
                                     "identifier_kind": "so_number",
                                     "sources": [{"binding": "portal.zz_widget",
                                                  "path": "number", "parser": "trim"}]}],
                     "evidence": [{"kind": "workbench_code", "path": "ontology/schema.py"}]}]}
        compiled = schema_loader.compile_documents(docs + [("domains/zz_extra.json", extra)])
        self.assertEqual(compiled.link_types["Email.mentions_zz_widget_order"]["to"],
                         "ZzWidgetOrder")
        copy = dict(compiled.link_types[LINK])
        documents = dict(docs)["domains/documents.json"]
        hand = {**documents, "link_types": documents["link_types"] + [
            {**copy, "bindings": [{**copy["bindings"][0],
                                   "evidence": [{"kind": "ref", "id": "wb.mentions"}]}]}]}
        with self.assertRaises(schema_loader.SchemaLoadError) as raised:
            schema_loader.compile_documents([(n, hand if n == "domains/documents.json" else d)
                                             for n, d in docs])
        self.assertIn(f"link type {LINK!r} declared twice", str(raised.exception))


class MentionStepScalingTests(unittest.TestCase):
    """The step looks handles up through an index, streams
    documents and leaves no index behind in the generation."""

    def test_handle_lookup_uses_an_index_and_documents_stream(self):
        from ontology.store import StoreBuilder

        plans = []
        real = mentions._store_version

        def explained(conn, handle):
            plans.append(" ".join(str(r[-1]) for r in conn.execute(
                "EXPLAIN QUERY PLAN " + mentions.VERSION_BY_HANDLE_SQL, (handle,))))
            return real(conn, handle)

        consumed = []

        def documents():   # a generator: the step must not need a list
            for n in range(3):
                consumed.append(n)
                yield mentions.Document(f"graph-mail://x/m{n}@ck", "email", ())

        with tempfile.TemporaryDirectory() as tmp:
            builder = StoreBuilder(Path(tmp) / "s.sqlite")
            try:
                with unittest.mock.patch.object(mentions, "_store_version", explained):
                    report = mentions.store_mention_links(
                        builder, type("P", (), {"links": {}, "sources": {}})(),
                        documents=documents())
                left = builder.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (mentions.HANDLE_INDEX,)).fetchall()
            finally:
                builder.abort()
        self.assertEqual(consumed, [0, 1, 2])
        self.assertEqual(report["counts"]["documents_not_projected"], 3)
        self.assertEqual(len(plans), 3)
        for plan in plans:
            self.assertIn(mentions.HANDLE_INDEX, plan)
            self.assertNotIn("SCAN v", plan)
        self.assertEqual(left, [])   # not part of the generation's DDL


class ProbeWithMailTests(unittest.TestCase):
    """scripts.ontology_projection_probe with a mail scope (the ontology.mentions live
    check) over injected transports: Portal FakeWorkbench and FakeGraph."""

    def probe_world(self, tmp, bc, *, identity=None, **options):
        """The probe over FakeWorkbench, ``bc`` and a FakeGraph mailbox whose message names
        PO-1001 (``identity``: the mail capture's, ``mail_identity``). Returns (report,
        live-audience document, mailbox address)."""
        from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts.ontology_projection_probe import projection_probe

        from integrations.capture_mail import CaptureScope
        from integrations.live_source_guard import LiveSourceAudience
        from integrations.tests.test_capture_mail import UPN_B
        from operating.case_mail_scope import MailScope

        # Mailbox A answers as an allowlisted address: the served host reads mail of the
        # case mail scope only (operating/case_mail_scope.json).
        address = "purchase-order@petrasoap.com"
        graph = FakeGraph()
        graph.users[BOX_A].update(userPrincipalName=address, mail=address,
                                  proxyAddresses=[f"SMTP:{address}"])
        graph.put(BOX_A, "MSG-1", "CK-1-1", body="Please confirm PO-1001 today.")
        graph.put(BOX_B, "MSG-B1", "CKB-1", body="Finance copy of PO-1001.")
        api, _ = graph_read.build_graph(ENV, sender=graph,
                                        retry=BoundedBackoff(sleep=lambda s: None,
                                                             jitter=lambda: 0.0))
        scope = CaptureScope([{"address": address, "folders": ["inbox"]},
                              {"address": UPN_B, "folders": ["inbox"]}], window_days=90,
                             internal_domains=["example-plx.com"],
                             allowlist=MailScope([address, UPN_B]))
        # The capture principal may read mailbox A (and BC) only: B is never captured.
        document = {"schema": "plx-live-source-audience/v1", "tenant": "plx",
                    "audiences": {f"mail:{address}": ["staff:orders"],
                                  f"mail:{UPN_B}": ["staff:finance"],
                                  "bc:*": ["staff:orders"]},
                    "classifications": {"mail:*": "restricted"}}
        audience = LiveSourceAudience.from_json(document)
        reader_actor = READER_A
        # The spec's narrowed Portal audience, for this test's reader.
        narrowed = json.loads((ROOT / "examples/projection_probe_mirror_audience.live-check.json")
                              .read_text("utf-8").replace("svc:mail-capture", "staff:orders"))
        Path(f"{tmp}/live-audience.json").write_text(json.dumps(document), "utf-8")
        Path(f"{tmp}/mirror-audience.json").write_text(json.dumps(narrowed), "utf-8")
        client = WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench())
        result = projection_probe(
            client, f"{tmp}/mirror.sqlite", f"{tmp}/store.sqlite", claim_sample=1000,
            mail={"scope": scope, "reader": GraphMailReader(api, audience),
                  "audience": audience, "capture_actor": reader_actor, "page_budget": 4,
                  **({"identity": identity} if identity is not None else {})},
            bc=bc, live_audience=f"{tmp}/live-audience.json",
            mirror_audience=f"{tmp}/mirror-audience.json", environ=ENV, **options)
        return result, document, address

    def test_probe_captures_projects_and_counts_mention_links_without_leaks(self):
        from integrations.tests import test_bc_capture as tb

        with tempfile.TemporaryDirectory() as tmp:
            result, document, address = self.probe_world(
                tmp, {"environ": tb.ENV, "sender": tb.FakeBc(), "books": tb.BOOKS})
            # The same served state with the mailbox granted to the mail-withheld reader
            # too: its mention links are served, and counted as leaks.
            from scripts import ontology_projection_probe as probe
            document["audiences"][f"mail:{address}"].append(probe.MAIL_WITHHELD_PRINCIPAL)
            Path(f"{tmp}/live-audience.json").write_text(json.dumps(document), "utf-8")
            leaky = probe.mail_mention_checks(probe.served_host(f"{tmp}/store.sqlite", ENV),
                                              f"{tmp}/store.sqlite")
            leaky_find = probe.mail_text_find_checks(
                probe.served_host(f"{tmp}/store.sqlite", ENV), f"{tmp}/store.sqlite")
        self.assertGreater(leaky["mention_denied_leaks"], 0)
        self.assertGreater(leaky_find["text_find_denied_leaks"], 0)
        # plx.find finds sampled mail words with their citation, and serves no mail to the
        # mail-withheld reader (ontology.text_find).
        self.assertGreaterEqual(result["text_find_sampled"], 1)
        self.assertEqual((result["text_find_found"], result["text_find_missed"],
                          result["text_find_errors"], result["text_find_denied_leaks"]),
                         (result["text_find_sampled"], 0, 0, 0))
        # cold (no session projection): one build, the rebuild not asserted
        self.assertEqual(result["rebuild_digest_not_asserted"], "cold_path_single_build")
        self.assertEqual(result["email_objects"], 1)
        self.assertGreaterEqual(result["mention_links"], 1)
        self.assertGreaterEqual(result["mention_links_visible_to_reader"], 1)
        self.assertEqual(result["mention_denied_leaks"], 0)
        # ... on linked objects the mail-withheld reader is served (not a vacuous check)
        self.assertGreater(result["mention_objects_visible_to_withheld"], 0)
        self.assertEqual(result["denied_leaks"], 0)
        self.assertFalse(result["mail_capture_complete"])   # mailbox B not visible
        text = json.dumps(result)
        self.assertEqual(result["citations"]["unresolved"], 0)
        # Rows and columns the reader's value markings withhold (BC accounts, bank
        # accounts, contacts) are no claim, and the claim gate serves none of those rows.
        self.assertGreater(result["claim_values_marking_withheld"], 0)
        self.assertEqual(result["marking_withheld_citations_resolved"], 0)
        self.assertGreater(result["citations"]["by_scheme"]["graph-mail"], 0)
        for secret in ("PO-1001", "graph-mail://", BOX_A, "MSG-1", address):
            self.assertNotIn(secret, text)
        from verify import live_verify as lv
        spec = json.loads((ROOT / "devloop/live-checks/ontology.mentions.json")
                          .read_text(encoding="utf-8"))
        lv.validate_spec(spec, stem="ontology.mentions")
        # No normalizer passed (as the CLI): the probe claim-checks identifier values
        # with the build's own default normaliser.
        self.assertTrue(result["all_sampled_claims_pass"], result)
        self.assertEqual((result["blocked_upstream"], result["sources"]),
                         ([], ["bc", "graph_mail", "portal_api"]))
        # every source answered: the full expect holds but for the live evidence label
        # and the live size of the narrowed sample (this fixture withholds a few objects)
        self.assertTrue(0 < result["reader_denied_objects_probed"] < 50, result)
        self.assertEqual(lv.check_expectations(spec, 0, result),
                         ["bc_capture.evidence_label != expected",
                          "reader_denied_objects_probed < 50"])
        self.assertEqual(result["bc_capture"]["evidence_label"], "injected-transport")

    def test_a_cold_build_with_no_room_for_a_rebuild_reports_mentions_and_phase_seconds(self):
        """Live b593c374 (07:03Z): BC listed its companies without an etag and the second
        cold build could not fit, so the report held no mention table, check or phase
        timing. Now the cold path builds once (option 3): every check runs on that build, the report carries phase_seconds and
        no rebuild, and the etag gap is no unit reason: a company row without an etag
        is versioned by its content and the unit completes."""
        from urllib.parse import urlsplit

        from verify import live_verify as lv
        from integrations import bc_read
        from integrations.live_source_guard import LiveSourceAudience
        from integrations.tests import test_bc_capture as tb
        import ontology.project
        from scripts import ontology_projection_probe as probe

        class NoCompanyEtag(tb.FakeBc):   # as live BC lists its companies
            def __call__(self, method, url, *rest):
                status, headers, body = super().__call__(method, url, *rest)
                if urlsplit(url).path.endswith("/companies") and status == 200:
                    listing = json.loads(body)
                    for company in listing["value"]:
                        del company["@odata.etag"]
                    body = json.dumps(listing).encode()
                return status, headers, body

        fake, now, builds = NoCompanyEtag(), [0.0], []
        real_build = ontology.project.build_store

        def slow_build(target, *args, **kwargs):   # each build takes 1,000 s of 1,500
            builds.append(target)
            now[0] += 1000.0
            return real_build(target, *args, **kwargs)

        bc = {"environ": {**tb.ENV, bc_read.FRESH_SINCE_ENV: "2000-01-01T00:00:00Z"},
              "sender": fake, "books": tb.BOOKS}
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch("ontology.project.build_store", slow_build):
            result, _, _ = self.probe_world(tmp, bc, budget_seconds=1500.0,
                                            monotonic=lambda: now[0])
            # The capture is otherwise current: the session's next capture reads no unit
            # again (the company rows come from the listing and send no GET).
            store = f"{tmp}/store.sqlite"
            host = probe.served_host(store, ENV)
            sent = len(fake.calls)
            again = probe.capture_bc(
                bc, probe.bc_capture_path(store), None, actor=probe.reader_actor(host),
                audience=LiveSourceAudience.from_file(host.config.live_audience))
            reread = [url for _, url, _ in fake.calls[sent:] if "/companies(" in url]
        self.assertNotIn("status", result)   # not a budget stop
        self.assertEqual(len(builds), 1)   # no second build
        self.assertNotIn("rebuild_skipped", result)
        self.assertNotIn("rebuild_digest_equal", result)
        # the mention tables and every check, on the first build
        self.assertEqual(result["email_objects"], 1)
        self.assertGreaterEqual(result["mention_links"], 1)
        self.assertGreaterEqual(result["mention_links_visible_to_reader"], 1)
        self.assertEqual((result["mention_denied_leaks"], result["denied_leaks"]), (0, 0))
        self.assertTrue(result["all_sampled_claims_pass"], result)
        self.assertEqual(result["citations"]["unresolved"], 0)
        # where the budget went, phase by phase
        seconds = result["phase_seconds"]
        self.assertEqual(seconds["projection"], 1000.0)
        self.assertLessEqual({"sync", "bc_capture", "mail_capture", "projection", "publish",
                              "claims", "denied", "mentions"}, set(seconds))
        self.assertNotIn("rebuild", seconds)
        # an etag-less company row is a complete unit: no unit reason; BC is a source
        self.assertEqual(result["bc_capture"]["unit_reasons"], {})
        self.assertIsNone(result["bc_capture"]["reason"])
        self.assertEqual((result["blocked_upstream"], result["sources"]),
                         ([], ["bc", "graph_mail", "portal_api"]))
        self.assertEqual(again["units_reused"], again["units_total"] - 3)
        self.assertEqual(reread, [])
        spec = json.loads((ROOT / "devloop/live-checks/ontology.mentions.json")
                          .read_text(encoding="utf-8"))
        failures = lv.check_expectations(spec, 0, result)
        self.assertFalse([f for f in failures if "mention" in f or "rebuild" in f], failures)
        recorded = lv._recorded(spec, result)
        self.assertEqual(recorded["phase_seconds"]["projection"], 1000.0)
        self.assertEqual(recorded["bc_capture.unit_reasons"], {})
        self.assertEqual(recorded["rebuild_digest_not_asserted"], "cold_path_single_build")


if __name__ == "__main__":
    unittest.main()
