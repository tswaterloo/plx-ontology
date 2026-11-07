"""Mission Control tasks linked to the records they name (``ontology.mc_task_links``).

Evidence label: ``isolated-fixture`` (made-up Portal rows and MC tasks, the real projector,
the compiled schema and the two steps; no source read).

* A name a task writes is a lead to the party whose name it is, or is with a qualifier;
  never to a party whose name goes on with other words, never for a word the tasks write
  in lower case, never for a colleague's name, never for a pair a person rejected.
* An identifier lead is raised to a firm link beside it only when the task wrote the
  identifier exactly as a whole token and it names one record.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ontology import identifiers, schema
from ontology import mc_task_links as mc
from ontology.project import CapturedRecord, build_store

T = "2026-10-01T00:00:00Z"


class Source:
    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        if self.source == "mission_control":
            key, handle = f"live/{row['id']}", f"mc://live/tasks/{row['id']}@0123456789abcdef-1"
            audience = "mc:live/tasks/PRJ-X"
        else:
            key = json.dumps({"id": row["id"]})
            handle = f"api://{rel}/{quote(key, safe='')}@0123456789abcdef-1"
            audience = f"api:{rel}"
        digest = hashlib.sha256(f"{rel}/{key}".encode()).hexdigest()
        return CapturedRecord(self.source, rel, key, row, handle, "row", digest, T, None, T,
                              audience)


PORTAL = {
    "public.app_supplier": [
        {"id": "s1", "code": "ZORB01", "name": "Zorblax (Packaging)"},
        {"id": "s2", "code": "ZORB02", "name": "Zorblax Logistics"},
        {"id": "s3", "code": "QUIN01", "name": "Quinn Marlowe"},
        {"id": "s4", "code": "VELD01", "name": "Veldtrix Inc."}],
    "public.customer": [{"id": 1, "name": "Brightwash"}, {"id": 2, "name": "Test"}],
    "public.app_purchase_order": [
        {"id": "p1", "po_number": "PO900123"}, {"id": "p2", "po_number": "PO900124"},
        {"id": "p3", "po_number": "PO900125"}, {"id": "p4", "po_number": "PO900125"}],
}


def task(i, title, description=None, reporter="quinn@petrasoap.com"):
    return {"id": f"TASK-{i}", "scope": "live", "title": title, "description": description,
            "reporter": reporter, "stage": "backlog"}


MC = {"tasks": [
    task(1, "Chase Zorblax about the late drums"),
    task(2, "Quote for Brightwash next week", "test the quote flow; test again"),
    task(3, "Run the Test importer", "a test run, then another test, test data"),
    task(4, "Requested by: Quinn Marlowe"),
    task(5, "Price sheet from Veldtrix"),
    task(6, "Check PO900123 today", "Series PO900124+ starts here; PO900125 twice"),
]}


def oid(type_id, binding, key):
    return f"{type_id}:{binding}:{json.dumps(key, separators=(',', ':'))}"


def task_oid(i):
    return oid("MCTask", "mission_control.tasks", {"id": f"TASK-{i}", "scope": "live"})


SUP = {k: oid("Supplier", "portal.app_supplier", {"id": k}) for k in ("s1", "s2", "s3", "s4")}
BRIGHT = oid("Customer", "portal.fm_customer", {"id": 1})
PO = {k: oid("PurchaseOrder", "portal.app_purchase_order", {"id": k})
      for k in ("p1", "p2", "p3", "p4")}
LEAD_PO = "MCTask.mentions_purchase_order"


def _lead(n, task_no, target, field, span, normalized):
    reason = [{"feature": "identifier_mention", "agrees": True, "identifier_kind": "petra_po",
               "normalized": normalized, "mention_group": f"g{n}", "span": span,
               "citations": [{"l1_handle": f"mc://live/tasks/TASK-{task_no}@0123456789abcdef-1",
                              "field_path": f"row.{field}", "digest": "d",
                              "lineage": "mission_control"}]}]
    return {"link_id": f"mention:{n}{target[-6:]}", "link_type": LEAD_PO,
            "from_object": task_oid(task_no), "to_object": target, "tier": "lead",
            "binding_kind": "mention", "link_contract": None, "reason": json.dumps(reason),
            "why_not": "[]",
            "evidence_handles": json.dumps([reason[0]["citations"][0]["l1_handle"]]),
            "reviewed_by": None,
            "reviewed_at": None, "review_action_id": None, "markings": "[]",
            "recorded_at": T, "superseded_at": None}


def seed(builder, projector):
    """The mentions step's identifier leads of task 6 and a person's rejection of
    task 5 -> Veldtrix."""
    text = MC["tasks"][5]["description"]
    builder.add("links", [
        _lead(1, 6, PO["p1"], "title", [6, 14], "PO900123"),
        _lead(2, 6, PO["p2"], "description", [7, 15], "PO900124"),
        _lead(3, 6, PO["p3"], "description", [text.index("PO900125"),
                                              text.index("PO900125") + 8], "PO900125"),
        _lead(3, 6, PO["p4"], "description", [text.index("PO900125"),
                                              text.index("PO900125") + 8], "PO900125"),
        {"link_id": "review:x", "link_type": "MCTask.mentions_supplier",
         "from_object": task_oid(5), "to_object": SUP["s4"], "tier": "contradicted",
         "binding_kind": "reviewed", "link_contract": None,
         "reason": '[{"agrees":false,"feature":"review"}]', "why_not": "[]",
         "evidence_handles": '["review:a1"]', "reviewed_by": "tester", "reviewed_at": T,
         "review_action_id": "a1", "markings": "[]", "recorded_at": T,
         "superseded_at": None}])
    return {}


class McTaskLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        cls.report = build_store(
            path, schema.load_schema(),
            {"portal_api": Source("portal_api", PORTAL),
             "mission_control": Source("mission_control", MC)},
            normalizer=identifiers.normalize,
            clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
            post_project={"reviews": seed, "task_ids": mc.task_ids_step,
                          "task_names": mc.task_names_step})
        cls.conn = sqlite3.connect(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def links(self, prefix):
        return sorted(self.conn.execute(
            "SELECT from_object, to_object, link_type, tier FROM links WHERE link_id >= ? AND "
            "link_id < ?", (prefix, mc._end(prefix))).fetchall())

    def test_names_link_the_party_named_and_nothing_else(self):
        self.assertEqual(self.links(mc.NAME_PREFIX), sorted([
            (task_oid(1), SUP["s1"], "MCTask.mentions_supplier", "lead"),
            (task_oid(2), BRIGHT, "MCTask.mentions_customer", "lead")]))

    def test_an_exact_unique_identifier_is_firm_beside_its_lead(self):
        self.assertEqual(self.links(mc.FIRM_PREFIX),
                         [(task_oid(6), PO["p1"], LEAD_PO, "corroborated")])
        counts = self.report["post_project"]["task_ids"]["counts"]
        self.assertEqual((counts["firm"], counts["not_exact"], counts["several_records"]),
                         (1, 1, 1))

    def test_a_rerun_removes_its_own_rows_first(self):
        class Builder:
            connection = self.conn

            def add(self, table, rows):
                raise AssertionError("nothing to add")
        before = self.links(mc.FIRM_PREFIX)
        try:
            mc._write(Builder(), mc.FIRM_PREFIX, [])
            self.assertEqual(self.links(mc.FIRM_PREFIX), [])
        finally:
            self.conn.rollback()
        self.assertEqual(self.links(mc.FIRM_PREFIX), before)


if __name__ == "__main__":
    unittest.main()
