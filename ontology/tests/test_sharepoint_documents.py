"""SharePoint files in the projection with stored mention links (owner decision
93047ba19524).

A versioned SharePoint capture (``integrations.capture_sharepoint``, filled over an
injected Graph transport) and synthetic Portal purchase orders are projected under the
curated schema; ``ontology.mentions.store_mention_links`` writes the files' mention
links into the same generation, and ``plx.find`` / ``plx.object`` / ``plx.documents`` /
``plx.around`` serve ``SharePointFile`` objects with resolvable ``sharepoint://``
citations, per reader. No network, no credentials. Evidence: injected-transport.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from integrations.live_source_guard import q
from integrations.tests.test_capture_sharepoint import (CAPTURE, DRIVE, EMPLOYEE, NAME_SECRET,
                                                        OUTSIDER, QUALITY, CaptureCase,
                                                        capture_audience, reader_audience,
                                                        scope)
from ontology import mentions, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import (CAPTURE_SOURCES, SharePointCaptures, build_store,
                              capture_source)
from ontology.tests.test_mail_documents import StaticPortal, po
from operating import ontology_tools as ot
from operating import ontology_tools_evidence as oe
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
LINK = "SharePointFile.mentions_purchase_order"


class SharePointDocumentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.case = CaptureCase("run")
        cls.case.setUp()
        cls.store = cls.case.store
        run = cls.case.capture().run(scope())
        assert run["complete"], run
        cls.compiled = schema_loader.load_schema()
        portal = StaticPortal([
            po("public.app_purchase_order", {"id": "p1"}, {"id": "p1", "po_number": "PO100234"}),
            po("public.app_purchase_order", {"id": "p2"}, {"id": "p2", "po_number": "PO100235"}),
        ])
        cls.path = Path(cls.case.tmp.name) / "ontology.sqlite"
        with SharePointCaptures(cls.case.state) as files:
            cls.report = build_store(cls.path, cls.compiled,
                                     {"portal_api": portal, "sharepoint": files},
                                     clock=lambda: NOW,
                                     post_project={"mentions": mentions.store_mention_links})
        cls.conn = sqlite3.connect(cls.path)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.case.tearDown()

    def file_object(self, item_id):
        return self.conn.execute(
            "SELECT object_id FROM objects WHERE object_type = 'SharePointFile' AND "
            "json_extract(native_key, '$.id') = ?", (item_id,)).fetchone()[0]

    def po_object(self, key):
        return self.conn.execute("SELECT object_id FROM objects WHERE object_id LIKE ?",
                                 (f'PurchaseOrder:portal.app_purchase_order:{{"id":"{key}"}}',)
                                 ).fetchone()[0]

    def tools(self, audience=None):
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:quality", "staff:sales", "staff:outsider"},
            "sharepoint:*": {"staff:quality", "staff:sales", "staff:outsider"}},
            markings={m: {"staff:quality", "staff:sales"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        base = ot.OntologyTools(reader, policy,
                                item_gate=self.store.item_gate(audience or reader_audience()),
                                schema=ot.SchemaView.from_compiled(self.compiled),
                                grammar=None, contracts=CONTRACTS, clock=lambda: NOW)
        return base, tt.TraverseTools(base), oe.EvidenceTools(base)

    def ok(self, name, result):
        self.assertEqual(validate(result, CONTRACTS["tools"][name]["output"], root=CONTRACTS),
                         [], json.dumps(result)[:1500])
        self.assertNotIn("error", result, json.dumps(result)[:1500])
        return result

    def test_sharepoint_is_a_registered_capture_source_projected_as_files(self):
        self.assertIs(CAPTURE_SOURCES["sharepoint"], SharePointCaptures)
        with capture_source("sharepoint", self.case.state) as files:
            described = files.describe()
        self.assertTrue(described["relations"]["driveItems"]["last_full_traversal_at"])
        rows = self.conn.execute(
            "SELECT o.audience_key, v.l1_handle, v.lineage FROM objects o JOIN object_versions "
            "v USING (object_id) WHERE o.object_type = 'SharePointFile'").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["audience_key"] for r in rows}, {f"sharepoint:{DRIVE}"})
        self.assertEqual({r["lineage"] for r in rows}, {"sharepoint"})
        for row in rows:   # every version cites a handle the capture resolves offline
            self.assertTrue(row["l1_handle"].startswith(f"sharepoint://{q(DRIVE)}/"))
            self.assertIsNotNone(self.store.resolve(row["l1_handle"], CAPTURE,
                                                    capture_audience()))
        # The files binding is captured; list items are not (no list-item capture here).
        self.assertEqual([g["where"] for g in self.report.get("gaps", [])
                          if g["code"] == "source_not_captured" and "sharepoint" in g["where"]],
                         ["gen.sharepoint.list_items"])

    def test_mentions_are_lead_links_written_in_the_same_generation(self):
        step = self.report["post_project"]["mentions"]
        rows = self.conn.execute("SELECT * FROM links WHERE link_type = ?", (LINK,)).fetchall()
        self.assertEqual(step["counts"]["links"], len(rows))
        pairs = {(r["from_object"], r["to_object"]) for r in rows}
        # The AO packet names PO100234 in its file name; release.txt in its extracted text.
        self.assertEqual(pairs, {(self.file_object("01FILEA"), self.po_object("p1")),
                                 (self.file_object("01FILEB"), self.po_object("p1"))})
        for row in rows:
            self.assertEqual((row["tier"], row["binding_kind"]), ("lead", "mention"))
            why = {f["feature"] for f in json.loads(row["why_not"])}
            self.assertIn("external_assertion", why)   # the author of a file is unknown
            reason = json.loads(row["reason"])
            paths = {c["field_path"] for f in reason for c in f.get("citations", [])}
            self.assertTrue(paths & {"row.name", "row.content_text"}, paths)
        # Written into the generation the build swapped in (one content digest).
        meta = dict(self.conn.execute("SELECT key, value FROM store_meta").fetchall())
        self.assertIn("mentions", json.loads(meta["build_report"])["post_project"])

    def test_plx_find_returns_the_file_with_a_resolvable_citation(self):
        base, _traverse, _evidence = self.tools()
        out = self.ok("plx.find", base.call("plx.find", {
            "query": "release", "object_type": "SharePointFile"}, QUALITY))
        self.assertEqual(out["status"], "found", json.dumps(out)[:800])
        (candidate,) = out["candidates"]
        self.assertEqual(candidate["object"]["object_id"], self.file_object("01FILEB"))
        cited = json.dumps(candidate)
        handle = self.conn.execute(
            "SELECT l1_handle FROM object_versions WHERE object_id = ?",
            (self.file_object("01FILEB"),)).fetchone()[0]
        self.assertIn(handle, cited)
        self.assertEqual(self.store.resolve(handle, QUALITY, reader_audience())["row"]["name"],
                         "release.txt")

    def test_plx_object_documents_and_around_reach_the_file_per_reader(self):
        base, traverse, evidence = self.tools()
        p1 = self.po_object("p1")
        out = self.ok("plx.object", base.call("plx.object", {"object_id": p1}, QUALITY))
        counts = {(c["link_type"], c["direction"]): c["count"] for c in out["linked_counts"]}
        self.assertEqual(counts.get((LINK, "reverse")), 2)
        docs = self.ok("plx.documents", evidence.call("plx.documents", {
            "object_id": p1, "sources": ["sharepoint"]}, QUALITY))
        cited = json.dumps(docs)
        self.assertIn("sharepoint://", cited)
        self.assertGreaterEqual(len(docs["documents"]), 2, cited[:800])
        around = self.ok("plx.around", traverse.call("plx.around", {
            "from": p1, "path": [{"link_type": LINK, "direction": "reverse"}],
            "tier_floor": "lead"}, QUALITY))
        self.assertEqual({o["object_id"] for o in around["objects"]},
                         {self.file_object("01FILEA"), self.file_object("01FILEB")})

    def test_an_employee_without_access_sees_neither_the_file_nor_its_mention(self):
        base, traverse, evidence = self.tools()
        packet = self.file_object("01FILEA")      # item permissions: Ricardo only
        denied = base.call("plx.object", {"object_id": packet}, EMPLOYEE)
        self.assertEqual(denied["error"]["code"], "object_not_found")
        p1 = self.po_object("p1")
        back = self.ok("plx.around", traverse.call("plx.around", {
            "from": p1, "path": [{"link_type": LINK, "direction": "reverse"}],
            "tier_floor": "lead"}, EMPLOYEE))
        self.assertEqual({o["object_id"] for o in back["objects"]},
                         {self.file_object("01FILEB")})
        docs = evidence.call("plx.documents", {"object_id": p1, "sources": ["sharepoint"]},
                             EMPLOYEE)
        self.assertNotIn(packet, json.dumps(docs))
        found = base.call("plx.find", {"query": "pricing", "object_type": "SharePointFile"},
                          EMPLOYEE)
        self.assertNotIn(NAME_SECRET, json.dumps(found))
        self.assertEqual(found["candidates"], [])
        outsider = base.call("plx.find", {"query": "release", "object_type": "SharePointFile"},
                             OUTSIDER)
        self.assertEqual(outsider["candidates"], [])

    def test_every_object_type_with_identifiers_has_a_sharepoint_mention_link(self):
        declared = mentions.mention_link_types(self.compiled.link_types)
        for tid, spec in self.compiled.object_types.items():
            # Documents never mention-target each other: their only identifiers are copy
            # identities (a Message-ID, a digest), never read from text.
            if spec["availability"]["state"] == "not_served" or tid in (
                    "Email", "Attachment", "SharePointFile", "ContainerMember"):
                continue
            if any(p["value_type"] == "identifier" for p in spec["properties"]):
                self.assertIn(("SharePointFile", tid), declared, tid)
        link = self.compiled.link_types[LINK]
        self.assertEqual((link["reverse_name"], link["bindings"][0]["from_binding"]),
                         ("mentioned_in_sharepoint_file", "sharepoint.drive_items"))
        self.assertEqual(self.compiled.object_types["SharePointFile"]["availability"]["state"],
                         "served")


if __name__ == "__main__":
    unittest.main()
