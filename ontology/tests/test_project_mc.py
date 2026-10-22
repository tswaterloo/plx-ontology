"""Mission Control captures projected as typed ontology objects (ontology/project_mc.py).

An MC capture (integrations/mc_sources.py over an injected fake MC) and synthetic Portal
purchase orders are projected under the curated schema; mention links reach the orders
MC text names, and the tools trim MC objects per reader and per MC project. Evidence:
injected-transport.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from integrations.capture_store import CaptureStore
from integrations.mc_sources import CaptureSpec, McSourcesCapture
from integrations.tests.test_mc_sources import (INTENT, Clock, FakeMc, add_hr_project,
                                                audience, mc_client)
from ontology import mentions, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import build_store, capture_source
from ontology.project_mc import INCOMPLETE, NOT_CAPTURED, McCaptures
from ontology.tests.test_mail_documents import StaticPortal, po
from ontology.types import Actor
from operating import ontology_tools as ot
from operating.ontology_host import case_host_policy

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
OPS = Actor("plx", frozenset({"staff:ops"}), "restricted")
ORDERS = Actor("plx", frozenset({"staff:orders"}), "restricted")


class ProjectMcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        fake = FakeMc()
        add_hr_project(fake)
        capture = Path(cls.tmp.name) / "mc.sqlite"
        with CaptureStore(capture, audience(), clock=Clock()) as store:
            McSourcesCapture(store, mc_client(fake)).capture(
                CaptureSpec("ops", ("landed cost",)))
        cls.compiled = schema_loader.load_schema()
        portal = StaticPortal([po("public.app_purchase_order", {"id": "p1"},
                                  {"id": "p1", "po_number": "PO100234"})])
        cls.path = Path(cls.tmp.name) / "ontology.sqlite"
        with capture_source("mission_control", capture) as source:
            assert isinstance(source, McCaptures)
            cls.gap = source.capture_gap("knowledge")
            cls.report = build_store(cls.path, cls.compiled,
                                     {"portal_api": portal, "mission_control": source},
                                     clock=lambda: NOW,
                                     post_project={"mentions": mentions.store_mention_links})
        cls.conn = sqlite3.connect(cls.path)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def object_id(self, object_type, native):
        return self.conn.execute("SELECT object_id FROM objects WHERE object_type = ? AND "
                                 "coalesce(json_extract(native_key, '$.id'), json_extract(native_key, '$.event_id')) = ?",
                                 (object_type, native)).fetchone()[0]

    def test_mc_records_are_typed_cited_objects_in_their_audience(self):
        types = dict(self.conn.execute(
            "SELECT object_type, COUNT(*) FROM objects WHERE object_type LIKE 'MC%' "
            "OR object_type = 'KnowledgeEntry' GROUP BY object_type").fetchall())
        self.assertEqual(types, {"MCTask": 4, "MCApproval": 2, "MCBucket": 2,
                                 "MCTaskEvent": 27, "KnowledgeEntry": 1})
        rows = self.conn.execute(
            "SELECT o.audience_key, v.l1_handle, v.lineage FROM objects o "
            "JOIN object_versions v USING (object_id) WHERE o.object_type = 'MCApproval' "
            "ORDER BY o.audience_key").fetchall()
        self.assertEqual([r["audience_key"] for r in rows],
                         ["mc:ops/approvals/PRJ-HR", "mc:ops/approvals/PRJ-OPS"])
        rows = rows[1:]
        self.assertTrue(rows[0]["l1_handle"].startswith("mc://ops/approvals/apg_one@"))
        self.assertEqual(rows[0]["lineage"], "mission_control")
        intent = self.conn.execute(
            "SELECT value, field_path, l1_handle FROM object_values WHERE property = "
            "'workbench_intent_id' AND value IS NOT NULL").fetchone()
        self.assertEqual((intent["value"], intent["field_path"]),
                         (INTENT, "row.workbench_intent_id"))
        self.assertEqual(intent["l1_handle"], rows[0]["l1_handle"])
        markings = self.conn.execute("SELECT markings FROM object_values WHERE property = "
                                     "'requested_by'").fetchone()[0]
        self.assertEqual(json.loads(markings), ["pii"])
        lineages = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT v.lineage FROM objects o JOIN object_versions v USING (object_id) "
            "WHERE o.object_type = 'KnowledgeEntry'")}
        # mc_search_knowledge hits only: brain items are knowledge_source's (GOV-13).
        self.assertEqual(lineages, {"mission_control"})
        # A ranked search is never an enumeration: reported, never complete.
        self.assertEqual(self.gap, INCOMPLETE)

    def test_a_retired_brain_items_unit_in_an_older_capture_is_neither_described_nor_projected(self):
        # A store captured before GOV-13 retired brain_items still holds its runs and
        # versions; knowledge_source is the only brain-item path now.
        old = Path(self.tmp.name) / "old-mc.sqlite"
        shutil.copy(Path(self.tmp.name) / "mc.sqlite", old)
        with sqlite3.connect(old) as conn:
            for table in ("runs", "versions"):  # the primary key is the store's own
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if not r[5]]
                picked = ", ".join(
                    "replace(unit, '/knowledge', '/brain_items')" if c == "unit" else
                    "replace(path, '/knowledge/', '/brain_items/')" if c == "path" else c
                    for c in cols)
                conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {picked} "
                             f"FROM {table} WHERE unit = 'mc:ops/knowledge'")
            self.assertTrue(conn.execute("SELECT COUNT(*) FROM versions WHERE unit = "
                                         "'mc:ops/brain_items'").fetchone()[0])
        with McCaptures(old) as source:
            self.assertNotIn("brain_items", source.describe()["relations"])
            self.assertEqual(source.capture_gap("brain_items"), NOT_CAPTURED)
            self.assertEqual(list(source.records(["brain_items"])["brain_items"]), [])
            self.assertEqual({d.kind for d in source.documents()},
                             {"mc_task", "mc_approval", "knowledge_entry"})
            self.assertEqual(sum(d.kind == "knowledge_entry" for d in source.documents()), 1)

    def test_gates_link_to_their_task_and_mc_text_links_the_orders_it_names(self):
        gate = self.conn.execute("SELECT * FROM links WHERE link_type = 'MCApproval.gate_of' "
                                 "AND from_object = ?",
                                 (self.object_id("MCApproval", "apg_one"),)).fetchone()
        self.assertEqual((gate["tier"], gate["to_object"]),
                         ("verified", self.object_id("MCTask", "TASK-2")))
        po_object = self.conn.execute("SELECT object_id FROM objects WHERE object_type = "
                                      "'PurchaseOrder'").fetchone()[0]
        mentions_po = self.conn.execute(
            "SELECT link_type, from_object, tier, evidence_handles FROM links "
            "WHERE binding_kind = 'mention' AND to_object = ? ORDER BY link_type",
            (po_object,)).fetchall()
        self.assertEqual({r["link_type"] for r in mentions_po},
                         {"MCTask.mentions_purchase_order", "MCApproval.mentions_purchase_order",
                          "KnowledgeEntry.mentions_purchase_order"})
        self.assertEqual({r["tier"] for r in mentions_po}, {"lead"})
        task_links = [r for r in mentions_po if r["link_type"].startswith("MCTask")]
        self.assertEqual({r["from_object"] for r in task_links},
                         {self.object_id("MCTask", "TASK-1"), self.object_id("MCTask", "TASK-4")})
        for r in mentions_po:
            self.assertTrue(any(h.startswith("mc://") for h in json.loads(r["evidence_handles"])))

    def test_tools_show_mc_objects_only_to_readers_their_audience_grants(self):
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:ops", "staff:orders"}, "mc:ops/*": {"staff:ops"}},
            markings={m: {"staff:ops", "staff:orders"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        tools = ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(self.compiled),
                                 grammar=None, contracts=CONTRACTS, clock=lambda: NOW)
        task = self.object_id("MCTask", "TASK-1")
        seen = tools.call("plx.object", {"object_id": task}, OPS)
        self.assertEqual(validate(seen, CONTRACTS["tools"]["plx.object"]["output"],
                                  root=CONTRACTS), [], json.dumps(seen)[:1500])
        self.assertNotIn("error", seen)
        hidden = tools.call("plx.object", {"object_id": task}, ORDERS)
        self.assertIn("error", hidden)
        po_object = self.conn.execute("SELECT object_id FROM objects WHERE object_type = "
                                      "'PurchaseOrder'").fetchone()[0]

        def mc_links(actor):
            out = tools.call("plx.object", {"object_id": po_object}, actor)
            return {c["link_type"] for c in out["linked_counts"]
                    if c["link_type"].startswith(("MC", "KnowledgeEntry"))}
        self.assertIn("MCTask.mentions_purchase_order", mc_links(OPS))
        self.assertEqual(mc_links(ORDERS), set())

    def test_the_case_host_grants_mc_units_only_through_the_live_audience(self):
        keys = ot.StoreReader.open(self.path)
        self.addCleanup(keys.conn.close)
        held = [k for k in keys.audience_keys() if k.startswith("mc:")]
        self.assertIn("mc:ops/approvals/PRJ-OPS", held)
        runtime = SimpleNamespace(
            mirror=SimpleNamespace(_audience=lambda: SimpleNamespace(allows=lambda a, r: False)),
            live=SimpleNamespace(audience=audience(), bc=None), employee_gate=lambda a: a)

        def granted(actor, keys=held):
            policy = case_host_policy(runtime, actor, None, audience_keys=keys)
            return {k for k, principals in policy.audiences.items() if principals}

        self.assertEqual(granted(OPS), set(held))
        planner = Actor("plx", frozenset({"staff:planner"}), "restricted")
        self.assertEqual(granted(planner), {"mc:ops/tasks/PRJ-OPS"})
        self.assertEqual(granted(ORDERS), set())
        # A project-scoped key without its project (a store built before per-project keys)
        # is granted to nobody.
        self.assertEqual(granted(OPS, ["mc:ops/tasks", "mc:ops/knowledge"]), {"mc:ops/knowledge"})
        no_live = SimpleNamespace(**{**vars(runtime), "live": None})
        self.assertEqual(case_host_policy(no_live, OPS, None, audience_keys=held).audiences, {})


    def test_find_and_links_hide_another_mc_projects_tasks_from_a_tasks_reader(self):
        # The planner holds mc:ops:tasks and PRJ-OPS, not PRJ-HR (test_mc_sources.audience).
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        runtime = SimpleNamespace(
            mirror=SimpleNamespace(_audience=lambda: SimpleNamespace(allows=lambda a, r: True)),
            live=SimpleNamespace(audience=audience(), bc=None), employee_gate=lambda a: a)
        tools = ot.OntologyTools(reader, ot.ReadPolicy("plx", {}),
                                 schema=ot.SchemaView.from_compiled(self.compiled),
                                 grammar=None, contracts=CONTRACTS, clock=lambda: NOW)
        planner = Actor("plx", frozenset({"staff:planner"}), "restricted")
        po_object = self.conn.execute("SELECT object_id FROM objects WHERE object_type = "
                                      "'PurchaseOrder'").fetchone()[0]

        def read(actor):
            tools.policy = case_host_policy(runtime, actor, None,
                                            audience_keys=reader.audience_keys())
            found = tools.call("plx.find", {"object_type": "MCTask", "filters": [
                {"property": "task_id", "op": "prefix", "value": "TASK-"}], "limit": 50}, actor)
            linked = tools.call("plx.object", {"object_id": po_object}, actor)["linked_counts"]
            return ({c["object"]["object_id"] for c in found["candidates"]},
                    {c["link_type"]: c["count"] for c in linked
                     if c["link_type"] == "MCTask.mentions_purchase_order"})

        tasks = {n: self.object_id("MCTask", n) for n in ("TASK-1", "TASK-2", "TASK-3", "TASK-4")}
        mentions = dict(self.conn.execute(
            "SELECT from_object = ?, COUNT(*) FROM links WHERE link_type = "
            "'MCTask.mentions_purchase_order' GROUP BY 1", (tasks["TASK-4"],)).fetchall())
        self.assertEqual(read(OPS), (set(tasks.values()), {
            "MCTask.mentions_purchase_order": mentions[0] + mentions[1]}))
        self.assertEqual(read(planner), ({tasks[n] for n in ("TASK-1", "TASK-2", "TASK-3")},
                                         {"MCTask.mentions_purchase_order": mentions[0]}))
        self.assertEqual(read(ORDERS), (set(), {}))


if __name__ == "__main__":
    unittest.main()
