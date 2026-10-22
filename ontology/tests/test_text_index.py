"""plx.find over the word index of store layout v8 (``ontology/text_index.py``).

A phrase held only by a captured attachment's page is found and cited to that page; a
mail body is found with its sender class; a withheld mailbox's copy of the same phrase
never reaches a reader the item ACL or markings deny; an incremental index equals a cold
one and forgets the words it no longer holds; a text match over 100k values stays under
200 ms (the LIKE scan it replaces read every value); a query the index cannot read is a
truncated search, never an absence; plx.around does the same work with the index
populated or empty. All identifiers and text are synthetic.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, READER_A, SCOPE,
                                                   Clock, FakeGraph, capture_audience,
                                                   file_attachment, reader_audience)
from ontology import schema as schema_loader
from ontology import store_ddl
from ontology.json_contract import validate
from ontology.mentions import Document, TextField
from ontology.project import MailCaptures, build_store
from ontology.store import StoreBuilder
from ontology.tests.test_document_fields import pdf
from ontology.tests.test_mail_documents import StaticPortal, po
from ontology.text_index import store_text_index
from ontology.types import Actor
from operating import ontology_tools as ot
from tests import test_ontology_around_latency as lat
from tests import test_ontology_tools_traverse as fx

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)
OUTSIDER = Actor("plx", frozenset({"staff:outsider"}), "restricted")
PHRASE = "Lanolin reconciliation pending audit"


def find(tools, query, actor, **args):
    tools = getattr(tools, "base", tools)   # a TraverseTools serves its base's plx.find
    result = tools.call("plx.find", {"query": query, **args}, actor)
    problems = validate(result, CONTRACTS["tools"]["plx.find"]["output"], root=CONTRACTS)
    assert problems == [] and "error" not in result, (problems, json.dumps(result)[:1500])
    return result


class MailTextTests(unittest.TestCase):
    """A mail capture through ``build_store`` with the ``text_index`` step."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "state"
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-1", "CK-1-1", body="Packing list attached.", subject="Packing",
                  attachments=[file_attachment("ATT-1", pdf([["Page one: cartons"],
                                                             [PHRASE + " by Friday"]]),
                                               name="packing.pdf")])
        graph.put(BOX_A, "MSG-2", "CK-2-1", body="Marigold freight booked for Tuesday.",
                  subject="Freight")
        graph.put(BOX_B, "MSG-B1", "CKB-1", body=PHRASE + " (finance copy).",
                  subject="Finance",
                  attachments=[file_attachment("ATT-B", pdf([[PHRASE]]), name="ledger.pdf")])
        cls.store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph,
                                        retry=BoundedBackoff(sleep=lambda s: None,
                                                             jitter=lambda: 0.0))
        run = MailCapture(cls.store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
        cls.compiled = schema_loader.load_schema()
        cls.path = Path(cls.tmp.name) / "ontology.sqlite"
        portal = StaticPortal([po("public.app_purchase_order", {"id": "p1"},
                                  {"id": "p1", "po_number": "PO100234"})])
        with MailCaptures(cls.state, internal_domains=SCOPE.internal_domains) as mail:
            cls.report = build_store(cls.path, cls.compiled,
                                     {"portal_api": portal, "graph_mail": mail},
                                     clock=lambda: NOW,
                                     post_project={"text_index": store_text_index})
            cls.documents = list(mail.documents())

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def tools(self, policy_readers=("staff:orders", "staff:outsider")):
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": set(policy_readers), "mail:*": set(policy_readers)},
            markings={m: {"staff:orders"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        return ot.OntologyTools(reader, policy, item_gate=self.store.item_gate(reader_audience()),
                                schema=ot.SchemaView.from_compiled(self.compiled),
                                grammar=None, contracts=CONTRACTS, clock=lambda: NOW)

    def test_a_phrase_only_in_an_attachment_is_found_cited_to_its_page(self):
        report = self.report["post_project"]["text_index"]
        self.assertEqual(report["vector"]["state"], "unavailable")
        result = find(self.tools(), "lanolin reconciliation", READER_A)
        self.assertEqual(len(result["candidates"]), 1, json.dumps(result)[:1500])
        cand = result["candidates"][0]
        self.assertEqual((cand["tier"], cand["reason"]), ("lead", "document_text"))
        self.assertIn('"attachment_id":"ATT-1"', cand["object"]["object_id"])
        origin = cand["matched"]["raw"]["origin"]
        self.assertRegex(origin["citation"],
                         f"^graph-mail://{BOX_A}/MSG-1@CK-1-1#attachment=ATT-1&sha256=[0-9a-f]{{64}}"
                         "&page=2$")
        self.assertEqual(origin["field_path"], "row.content_text")
        self.assertIn(PHRASE, cand["matched"]["raw"]["untrusted_text"])
        # A mail body (held by no value) is found with its sender class.
        body = find(self.tools(), "marigold freight", READER_A)["candidates"]
        self.assertEqual(len(body), 1)
        self.assertIn('"id":"MSG-2"', body[0]["object"]["object_id"])
        self.assertEqual(body[0]["matched"]["raw"]["origin"],
                         {"citation": f"graph-mail://{BOX_A}/MSG-2@CK-2-1",
                          "field_path": "row.body_text", "sender_class": "external"})

    def test_a_withheld_record_never_appears(self):
        # Mailbox B holds the phrase in a body and an attachment: READER_A's item ACL
        # denies it, OUTSIDER is denied both mailboxes; SQL admits all of them.
        for actor, seen in ((READER_A, 1), (OUTSIDER, 0)):
            result = find(self.tools(), "lanolin reconciliation", actor)
            text = json.dumps(result)
            self.assertEqual(len(result["candidates"]), seen, text[:1500])
            self.assertNotIn(BOX_B, text)
            self.assertNotIn("MSG-B1", text)
            self.assertNotIn("finance copy", text)
        # A reader without the store's markings sees no marked field: mark the fields.
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("UPDATE text_fields SET markings = '[\"finance\"]'")
            conn.commit()
            policy = ot.ReadPolicy(tenant="plx", audiences={"mail:*": {"staff:orders"}},
                                   markings={"finance": {"staff:finance"}})
            reader = ot.StoreReader.open(self.path)
            self.addCleanup(reader.conn.close)
            tools = ot.OntologyTools(reader, policy,
                                     item_gate=self.store.item_gate(reader_audience()),
                                     schema=ot.SchemaView.from_compiled(self.compiled),
                                     grammar=None, contracts=CONTRACTS, clock=lambda: NOW)
            self.assertEqual(find(tools, "lanolin reconciliation", READER_A)["candidates"], [])
        finally:
            conn.execute("UPDATE text_fields SET markings = '[]'")
            conn.commit()
            conn.close()

    def test_an_incremental_index_equals_a_cold_one_and_forgets_removed_words(self):
        # The next capture: page 2 rewritten, mailbox B gone, everything else unchanged.
        changed = []
        for document in self.documents:
            if BOX_B in document.citation:
                continue
            fields = tuple(TextField(f.citation, f.field_path,
                                     f.text.replace(PHRASE, "Beeswax pallets relabelled"))
                           for f in document.fields)
            changed.append(Document(document.citation, document.kind, fields,
                                    document.sender_class))
        projector = types.SimpleNamespace(_marks_for_path=lambda binding, path: set())

        def index(cold):
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "next.sqlite"
                builder = StoreBuilder(target, seed=self.path)
                try:
                    conn = builder.connection
                    if cold:
                        conn.execute("INSERT INTO chunk_text (chunk_text) VALUES ('delete-all')")
                        conn.execute("DELETE FROM text_chunks")
                        conn.execute("DELETE FROM text_fields")
                    report = store_text_index(builder, projector, documents=changed)
                    rows = sorted(conn.execute(
                        "SELECT f.citation, f.field_path, f.object_id, f.version_handle, "
                        "f.markings, f.sender_class, f.text_digest, t.start, t.body FROM "
                        "text_fields f JOIN text_chunks t ON t.field_id = f.field_id"))
                    hits = {word: sorted(conn.execute(
                        "SELECT f.citation FROM chunk_text c JOIN text_chunks t ON "
                        "t.chunk_id = c.rowid JOIN text_fields f ON f.field_id = t.field_id "
                        "WHERE chunk_text MATCH ?", (word,)))
                        for word in ("lanolin", "beeswax", "marigold", "finance")}
                finally:
                    builder.abort()
            return report, rows, hits

        warm, cold = index(False), index(True)
        self.assertEqual(warm, cold)
        report, rows, hits = warm
        self.assertEqual((hits["lanolin"], hits["finance"]), ([], []))   # no stale words
        self.assertEqual(len(hits["beeswax"]), 1)
        self.assertEqual(len(hits["marigold"]), 1)
        self.assertEqual(report["fields"], len(rows))


# --------------------------------------------------------------------------- fixture store

SIZE = 100_000
RARE = "Zephyrine Holdings"


def index_documents(store, documents, marks=lambda binding, path: set()):
    """Run the ``text_index`` step over ``documents`` on a hand-filled fixture store."""
    return store_text_index(types.SimpleNamespace(connection=store.conn),
                            types.SimpleNamespace(_marks_for_path=marks), documents=documents)


def doc(handle, text, path="row.content_text"):
    return Document(handle, "sharepoint_file", (TextField(handle, path, text),))


def with_parties(n, rare_at):
    """The traverse fixture plus ``n`` parties named ``Filler company <i>`` (one named
    ``RARE``), each a value row the old LIKE scan read on every text query."""
    def fill(s):
        objects, versions, values = [], [], []
        for i in range(n):
            oid = f"Party:portal.party:x-{i:06d}"
            handle = fx.api("party", f"x{i}")
            name = RARE if i == rare_at else f"Filler company {i}"
            objects.append((oid, "Party", "portal.party", json.dumps([f"x-{i:06d}"]),
                            "api:public.party", fx.T0, None))
            versions.append((oid, handle, fx._h(handle), "fm_bronze", "[]", fx.T0, None))
            values.append((oid, "name", "text", name, None, None, None, None, None, handle,
                           "row.name", fx._h(handle + name), "fm_bronze", "portal.party",
                           "as_is", 0, "[]", fx.T0, None))
        s.conn.executemany("INSERT INTO objects VALUES (?,?,?,?,?,?,?)", objects)
        s.conn.executemany("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)", versions)
        s.conn.executemany("INSERT INTO object_values VALUES "
                           "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
    return fx.build(extra=fill)


class FixtureTextTests(unittest.TestCase):
    def test_text_match_on_100k_rows_is_under_200_ms(self):
        steps = {}
        for size in (SIZE // 10, SIZE):
            store = with_parties(size, rare_at=size // 2)
            self.addCleanup(store.conn.close)
            tools, _ = fx.tools(store)
            for query in (RARE.lower(), "unheard of words"):
                find(tools, query, fx.FULL)
                steps[size, query] = sum(step for sql, step in _statement_steps(
                    store.conn, lambda: find(tools, query, fx.FULL))
                    # The reader's scope lists the store's audience units on every call,
                    # whatever it asks (not a text read; a residual of its own).
                    if not sql.startswith("SELECT DISTINCT audience_key FROM objects"))
        # A rare word or a miss reads the index, not every value: 10x the values, about
        # the same SQLite work (the LIKE scan it replaces did 10x).
        for query in (RARE.lower(), "unheard of words"):
            with self.subTest(query=query):
                self.assertLess(steps[SIZE, query], 1.5 * steps[SIZE // 10, query], steps)
        self.assertGreaterEqual(store.conn.execute(
            "SELECT COUNT(*) FROM object_values WHERE value_type = 'text'").fetchall()[0][0], SIZE)
        tools, _ = fx.tools(store)
        cases = ((RARE.lower(), 1), ("unheard of words", 0), ("filler company", None))
        for query, found in cases:
            result = find(tools, query, fx.FULL, limit=50)   # warm: the first call per store
            best = min(_timed(tools, query) for _ in range(3))
            with self.subTest(query=query):
                self.assertLess(best, 0.2)
                if found is not None:
                    self.assertEqual(len(result["candidates"]), found)
                else:   # 100k matches: the scan stops at its cap, and says so
                    self.assertTrue(result["candidates"])
                    self.assertEqual((result["completeness"]["complete"],
                                      result["completeness"]["reason"]), (False, "scan_cap"))

    def test_document_text_is_trimmed_per_reader_and_counted_once_per_object(self):
        for n in (1, 10, 60):
            store = fx.build()
            self.addCleanup(store.conn.close)
            handles = []
            for i in range(n):
                oid, handle = f"Document:sharepoint.docs:t-{i:03d}", f"sharepoint://drive-1/t-{i}@e1"
                store.obj(oid, "sharepoint:drive-1", handle, "sharepoint")
                handles.append(handle)
            # Each document says it in two chunks (one candidate each), plus a marked one,
            # one the item gate denies to OPS and one whose version is superseded.
            text = "orchid shipment " + "x " * 3000 + "orchid shipment again"
            index_documents(store, [doc(h, text) for h in handles]
                            + [doc(fx.D1_H, text, "row.secret"), doc(fx.D2_H, text)],
                            marks=lambda binding, path: {"finance"} if path == "secret" else set())
            store.conn.execute("UPDATE object_versions SET superseded_at = "
                               "'2099-01-02T00:00:00Z' WHERE l1_handle = ?", (fx.D1_H,))
            store.obj(fx.D1, "sharepoint:drive-1", "sharepoint://drive-1/item-1@etag2",
                      "sharepoint", recorded="2099-01-02T00:00:00Z", add_object=False)
            tools, _ = fx.tools(store)
            for actor, extra in ((fx.OPS, set()), (fx.FULL, {fx.D2})):
                with self.subTest(n=n, actor=sorted(actor.principals)):
                    result = find(tools, "Orchid shipment", actor, limit=50)
                    # Every readable document once, whatever its chunks; read every page.
                    self.assertEqual(result["completeness"]["total"], len(want := {
                        f"Document:sharepoint.docs:t-{i:03d}" for i in range(n)} | extra))
                    got, page = set(), result
                    while True:
                        ids = [c["object"]["object_id"] for c in page["candidates"]]
                        self.assertEqual(len(ids), len(set(ids)))
                        got |= set(ids)   # an ambiguous last page repeats one: never alone
                        cursor = page["completeness"].get("next_cursor")
                        if not cursor:
                            break
                        page = find(tools, "Orchid shipment", actor, limit=50, cursor=cursor)
                    self.assertEqual(got, want)
                    for cand in result["candidates"]:
                        self.assertEqual(cand["reason"], "document_text")
                        self.assertEqual(cand["matched"]["raw"]["origin"]["field_path"],
                                         "row.content_text")

    def test_a_query_the_index_cannot_read_is_truncated_not_absent(self):
        store = fx.build()
        self.addCleanup(store.conn.close)
        tools, _ = fx.tools(store)
        # U+19B0 is a word character to Python and a separator to the index's tokenizer:
        # the index splits (or, alone, drops) the word, so it cannot prove it absent.
        for query in ("spec abcᦰdef", "spec ᦰᦱᦲ"):
            with self.subTest(query=ascii(query)):
                result = find(tools, query, fx.FULL)
                self.assertEqual(result["candidates"], [])
                self.assertFalse(result["completeness"]["complete"])
                self.assertIn(("unknown", "scan_cap"),
                              {(g["state"], g["reason"]) for g in result["gaps"]})
        self.assertEqual(find(tools, "spec sheet", fx.FULL)["candidates"][0]["object"][
            "object_id"], fx.D1)

    def test_around_work_is_the_same_with_the_index_populated_or_empty(self):
        populated = lat.work(lat.sized(lat.SIZE))
        store = lat.sized(lat.SIZE)
        self.assertGreater(store.conn.execute(
            "SELECT COUNT(*) FROM value_text WHERE value_text MATCH 'filler'").fetchall()[0][0], 0)
        store.conn.execute("INSERT INTO value_text (value_text) VALUES ('delete-all')")
        store.conn.commit()
        self.assertEqual(lat.work(store), populated)


def _statement_steps(conn, call):
    """(statement, SQLite VM steps) of each statement ``call`` runs on ``conn``."""
    out: list[list] = []

    def step():
        if out:
            out[-1][1] += 1
        return 0
    conn.set_trace_callback(lambda sql: out.append([sql, 0]))
    conn.set_progress_handler(step, 1)
    try:
        call()
    finally:
        conn.set_trace_callback(None)
        conn.set_progress_handler(None, 1)
    return out


def _timed(tools, query):
    tools = getattr(tools, "base", tools)
    start = time.perf_counter()
    tools.call("plx.find", {"query": query, "limit": 50}, fx.FULL)
    return time.perf_counter() - start


if __name__ == "__main__":
    unittest.main()
