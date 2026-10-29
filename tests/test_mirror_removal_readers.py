"""Every reader of a removed record goes through the one removal test (mirror-soft-deletes
review minors).

A record its source soft-deleted (``deleted_in_source``) reads as removed wherever a
missing one does: ``plx.object`` labels it with a removal gap, ``plx.aggregate`` leaves
it out of counts and sums and counts it in that gap (One whose
absence is not established stays counted), ``plx.read`` of its mirror citation carries
the gap, ``plx.process`` holds it out (an evidence record leaves its stage unknown, a
line is never summed) and a case-root pick never roots it; each through
``ontology.store.removals_at`` / ``removed_at_sql`` (or the mirror's own label) and never
past the reader's grant. The mirror's label and removal
interval agree on a re-delete; an undelete ends the interval at the row's own update time
when the source serves one, labelled; soft-delete and missing counts are distinct keys;
capture-store removal stretches go with the versions retention drops; a served projection
of an older store format answers a typed ``rebuild_required``. Fake Workbench transport,
hand-filled stores and temp files; every identifier is synthetic. Evidence:
isolated-fixture.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from integrations.capture_store import (REMOVALS_COLUMN, CaptureStore, CapturedRecord, bc_unit,
                                        removals_of)
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AUDIENCE_SCHEMA, AudienceMap
from integrations.source_mirror import SourceMirror
from integrations.tests.test_source_mirror import ALL, BASE, KEY, OPS, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import identifiers, store_ddl
from ontology import schema as schema_loader
from ontology.core import Actor
from ontology.json_contract import validate
from ontology.project import MirrorCaptures, build_store
from operating import ontology_tools as ot
from operating import ontology_tools_process as op
from operating.ontology_tools_aggregate import AggregateTools
from tests import test_ontology_tools_process as pbase
from tests import test_ontology_tools_review as tr
from tests import test_process_three_way as three

A = tr.employee_session(tr.OID_A).actor
STRANGER = tr.employee_session("cccccccc-cccc-4ccc-8ccc-cccccccccccc").actor  # no grant
EACH = "each"


CUSTOMERS = "public.app_customer"


def setUpModule():
    pbase.setUpModule()


def customer(key, deleted_at=None, updated_at="2026-09-29T10:00:00"):
    return {"id": key, "code": f"K-{key}", "deleted_at": deleted_at, "updated_at": updated_at}


def workbench(rows):
    """A Portal relation carrying the declared soft-delete column and a timestamp since."""
    fake = FakeWorkbench()
    fake.catalog = {CUSTOMERS: {"columns": ["id", "code", "deleted_at", "updated_at"],
                                "key": ["id"], "domain": "customers",
                                "since": {"column": "updated_at", "mode": "timestamp-utc"}}}
    fake.tables = {CUSTOMERS: rows}
    return fake


def open_mirror(test, path, fake, clock):
    mirror = SourceMirror(path, WorkbenchReadApiClient(BASE, KEY, fetch=fake), ALL, clock=clock,
                          retry_delay_seconds=0)
    test.addCleanup(mirror.close)
    return mirror


def customer_id(key):
    return f'Customer:portal.app_customer:{{"id":"{key}"}}'


def checked(test, name, result):
    test.assertEqual(validate(result, tr.CONTRACTS["tools"][name]["output"],
                              root=tr.CONTRACTS), [], result)
    return result


def removal_gaps(result):
    return {g["reason"]: g for g in result.get("gaps", ())
            if g["reason"] in ("deleted_in_source", "missing_from_source")}


class MirrorRemovalReaderTests(unittest.TestCase):
    """Soft deletes and absences from a real mirror, projected, read by the tools."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def project(self, name, count):
        """``count`` customers; every third soft-deleted, c01 missing from a later full
        traversal. The OntologyTools over their projection, and the mirror."""
        rows = [customer(f"c{i:02d}", deleted_at="2026-09-28T08:00:00" if i % 3 == 0 else None)
                for i in range(count)]
        fake, clock = workbench(rows), Clock()
        mirror = self.mirror = open_mirror(self, self.root / f"{name}.mirror", fake, clock)
        mirror.rows(OPS, CUSTOMERS)                                  # 09-29 12:00
        if count > 1:
            fake.tables[CUSTOMERS] = [r for r in fake.tables[CUSTOMERS] if r["id"] != "c01"]
            clock.advance(3600)
            mirror.refresh(OPS, CUSTOMERS, full=True)                # c01 missing at 13:00
        clock.advance(3600)
        store = self.root / f"{name}.sqlite"
        build_store(store, schema_loader.load_schema(),
                    {"portal_api": MirrorCaptures(self.root / f"{name}.mirror")},
                    normalizer=identifiers.normalize, clock=lambda: clock.now)
        base = ot.OntologyTools(ot.StoreReader.open(store), tr.policy(), item_gate=tr.item_gate,
                                grammar=identifiers, schema=ot.SchemaView.from_compiled(
                                    schema_loader.load_schema()),
                                contracts=tr.CONTRACTS, clock=lambda: clock.now)
        self.addCleanup(base.store.conn.close)
        return base, [customer_id(r["id"]) for r in rows]

    def test_plx_object_labels_a_soft_deleted_record_and_a_missing_one(self):
        base, ids = self.project("object", 3)
        gaps = removal_gaps(checked(self, "plx.object", base.call(
            "plx.object", {"object_id": ids[0]}, A)))
        self.assertEqual(list(gaps), ["deleted_in_source"])
        self.assertEqual(gaps["deleted_in_source"]["state"], "absent_in_source")
        self.assertIn("2026-09-28T08:00:00", gaps["deleted_in_source"]["detail"])
        self.assertEqual(list(removal_gaps(base.call("plx.object", {"object_id": ids[1]}, A))),
                         ["missing_from_source"])
        self.assertEqual(removal_gaps(base.call("plx.object", {"object_id": ids[2]}, A)), {})
        # Captured at 09-29 12:00, the deletion read then: an earlier as_known_at knows no
        # record; a reader without the grant sees no record, never its removal.
        early = base.call("plx.object", {"object_id": ids[0],
                                         "as_known_at": "2026-09-29T11:00:00Z"}, A)
        self.assertEqual(early["error"]["code"], "object_not_found")
        denied = base.call("plx.object", {"object_id": ids[0]}, STRANGER)
        self.assertEqual(denied["error"]["code"], "object_not_found")

    def test_plx_aggregate_leaves_deleted_members_out_and_counts_them_at_1_10_60(self):
        for count in (1, 10, 60):
            with self.subTest(members=count):
                base, ids = self.project(f"agg{count}", count)
                tools = AggregateTools(base)
                started = time.monotonic()
                out = checked(self, "plx.aggregate", tools.call("plx.aggregate", {
                    "set": base.sets.new(A, ids), "measure": {"op": "count"}}, A))
                self.assertLess(time.monotonic() - started, 5)
                deleted = len(range(0, count, 3))
                # The source asserts the deleted ones gone: left out, never a member, and
                # the answer as known now is complete without them.
                self.assertEqual(out["groups"][0]["value"], str(count - deleted))
                self.assertTrue(out["completeness"]["complete"], out)
                gaps = removal_gaps(out)
                self.assertEqual(gaps["deleted_in_source"]["state"], "absent_in_source")
                self.assertTrue(gaps["deleted_in_source"]["detail"].startswith(
                    f"{deleted} affected"), gaps)
                if count > 1:
                    # c01's absence is not established here: it stays counted, unknown.
                    self.assertEqual(gaps["missing_from_source"]["state"], "unknown")
                    self.assertTrue(gaps["missing_from_source"]["detail"].startswith(
                        "1 affected"), gaps)
                else:
                    self.assertNotIn("missing_from_source", gaps)
                # A reader without the grant counts nothing it cannot see, nor its removal.
                hidden = tools.call("plx.aggregate", {"set": base.sets.new(STRANGER, ids),
                                                      "measure": {"op": "count"}}, STRANGER)
                self.assertEqual(removal_gaps(hidden), {})
                self.assertNotIn("c00", json.dumps(hidden))
                left = tools.call("plx.compare", {"left": ids[0], "right": ids[-1]}, A)
                self.assertIn(f"left: {ids[0]}", {g["subject"] for g in left["gaps"]
                                                  if g["reason"] == "deleted_in_source"})

    def test_plx_read_of_a_deleted_mirror_row_carries_its_gap(self):
        from operating.ontology_tools_evidence import EvidenceTools, RuntimeCitations
        base, _ids = self.project("read", 3)

        class Runtime:   # the one member RuntimeCitations reads an api:// handle through
            mirror = self.mirror
        rows = {r["key"]["id"]: r["citation"] for r in self.mirror.rows(
            OPS, CUSTOMERS, include_missing=True)["rows"]}
        tools = EvidenceTools(base, reader=RuntimeCitations(Runtime()))
        out = checked(self, "plx.read", tools.call("plx.read", {"citation": rows["c00"]}, OPS))
        self.assertEqual(out["status"], "resolved")
        gap = removal_gaps(out)["deleted_in_source"]
        self.assertEqual((gap["state"], gap["subject"]), ("absent_in_source", rows["c00"]))
        self.assertIn("2026-09-28T08:00:00", gap["detail"])
        self.assertEqual(removal_gaps(tools.call("plx.read", {"citation": rows["c02"]}, OPS)),
                         {})
        # A reader without the relation reads nothing of the row, nor its deletion.
        denied = tools.call("plx.read", {"citation": rows["c00"]}, STRANGER)
        self.assertEqual(denied["status"], "withheld")
        self.assertNotIn("deleted_in_source", json.dumps(denied))


class ProcessRemovalTests(three._Base):
    """plx.process holds a soft-deleted record out like a missing one, with its gap."""

    def world(self, victim):
        s = pbase.Store()
        ids = three.gold_sales(s, "so-del", ordered=[("a", "10", EACH, "5")],
                               shipments=[("SHIPPED", [("a", "10", EACH)])])
        gone = ids[victim][0]
        s.conn.execute("INSERT INTO object_removals (object_id, basis, since) "
                       "VALUES (?, 'source_soft_delete', ?)", (gone, pbase.T0))
        return s, ids, gone

    def test_a_deleted_stage_record_leaves_the_stage_unknown_with_its_gap(self):
        s, ids, gone = self.world("shipments")
        answer, st, checks = self.run_po(s, ids["so"])
        missing = st["shipment"]["missing"]
        self.assertEqual((st["shipment"]["state"], missing["state"], missing["reason"]),
                         ("missing", "unknown", "deleted_in_source"))
        self.assertIn(gone, missing["detail"])
        self.assertIsNone(checks[("ordered", "shipped")]["agree"])

    def test_a_deleted_line_is_cited_never_summed_and_hidden_from_a_reader_without_it(self):
        s, ids, gone = self.world("ship_lines")
        answer, _, checks = self.run_po(s, ids["so"])
        self.assertIsNone(checks[("ordered", "shipped")]["agree"])  # never "shipped 10"
        [gap] = [g for g in answer.result["gaps"] if g["reason"] == "deleted_in_source"]
        self.assertEqual((gap["state"], gap["subject"]), ("unknown", "shipped"))
        self.assertIn(gone, gap["detail"])
        # The line marked beyond the sales reader: its removal is never named to it.
        s.conn.execute("UPDATE object_versions SET markings = '[\"l5\"]' WHERE object_id = ?",
                       (gone,))
        hidden = self.answer(op.ProcessTools(pbase.tools(s).base, pbase.GRAPHS),
                             {"from": ids["so"]}, pbase.SALES)
        self.assertNotIn("deleted_in_source", json.dumps(hidden.result))
        self.assertNotIn(gone, json.dumps(hidden.result))


class CaseRootPickTests(unittest.TestCase):
    """A run-time pick never roots a record its source soft-deleted."""

    def test_a_pick_skips_soft_deleted_records(self):
        from operating import case_workspace_live as live_ws
        from operating.case_runtime import CaseRuntime
        from operating.ingestion import Ingestor, load_sources
        from operating.tests import order_fixture as fixture

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name) / "state"
        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        fake.catalog["public.app_purchase_order"]["columns"].append("deleted_at")
        fake.tables = fixture.tables()
        kept = "po-a"   # the oldest Portal PO: every newer one is deleted in the source
        for row in fake.tables["public.app_purchase_order"]:
            row["deleted_at"] = None if row["id"] == kept else "2026-09-29T09:00:00Z"
        ingest = "svc:ingest"
        audiences = {name: sorted({*principals, ingest})
                     for name, principals in fixture.AUDIENCES.items()}
        audience = Path(temp.name) / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": audiences}), "utf-8")
        _tenant, actor, sources = load_sources({
            "schema": "plx-ingest-sources/v1", "tenant": "plx",
            "actor": {"principals": [ingest], "clearance": "internal"},
            "sources": [{"id": "portal." + name.replace("_", "-").replace(".", "-"),
                         "kind": "portal_relation", "relation": name}
                        for name in sorted(audiences)]}, {})
        filler = CaseRuntime(state, client=WorkbenchReadApiClient(BASE, KEY, fetch=fake),
                             audience=audience, tenant="plx",
                             mirror_options={"retry_delay_seconds": 0})
        ingestor = Ingestor(state, sources, actor=actor, mirror=filler.mirror)
        try:
            ingestor.run()
        finally:
            ingestor.close()
            filler.close()
        runtime = live_ws.offline_runtime(state, audience, "plx")
        self.addCleanup(runtime.close)
        investigator = Actor("plx", frozenset({"staff:operations", "staff:purchasing"}))
        opener = live_ws.CaseOpener(runtime, investigator, state,
                                    audience=AudienceMap.from_file(audience))
        opened = opener.open_case("pick", "PurchaseOrder")
        self.assertEqual(opened["status"], "opened", opened)
        root = opener.root["object_id"]
        # The projection holds the deleted POs (newer than the kept one: a pick reading
        # only the missing column roots one of them).
        db = sqlite3.connect(opener.projection().resolve().as_uri() + "?mode=ro", uri=True)
        self.addCleanup(db.close)
        deleted = {row[0] for row in db.execute(
            "SELECT object_id FROM object_removals WHERE basis = 'source_soft_delete' "
            "AND until IS NULL")}
        self.assertEqual(len(deleted), len(fake.tables["public.app_purchase_order"]) - 1)
        self.assertNotIn(root, deleted)


class MirrorLabelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def state(self, mirror):
        label = mirror.rows(OPS, CUSTOMERS, include_missing=True)["rows"][0]
        intervals = [tuple(r) for r in mirror._db.execute(
            "SELECT since, until, until_basis FROM row_removal WHERE basis = "
            "'source_soft_delete' ORDER BY since")]
        return label.get("deleted_in_source"), intervals

    def test_redelete_keeps_one_time_and_an_undelete_ends_at_the_source_update_time(self):
        fake, clock = workbench([customer("c1")]), Clock()
        mirror = open_mirror(self, self.root / "m.sqlite", fake, clock)
        mirror.rows(OPS, CUSTOMERS)                                        # 09-29 12:00

        def step(day, hour, **changes):
            fake.update(CUSTOMERS, "c1", **changes)
            clock.now = dt.datetime(2026, 10, day, hour, tzinfo=dt.timezone.utc)
            mirror.refresh(OPS, CUSTOMERS, full=True)
            return self.state(mirror)

        first = "2026-10-01T00:00:00.000000Z"
        self.assertEqual(step(1, 6, deleted_at="2026-10-01T00:00:00",
                              updated_at="2026-10-01T00:00:00"), (first, [(first, None, None)]))
        # Deleted again while deleted (never seen held in between): one time for both.
        self.assertEqual(step(2, 6, deleted_at="2026-10-02T00:00:00",
                              updated_at="2026-10-02T00:00:00"), (first, [(first, None, None)]))
        # Undeleted at 10-03 01:00 by the source's update time, read at 06:00.
        self.assertEqual(step(3, 6, deleted_at=None, updated_at="2026-10-03T01:00:00"),
                         (None, [(first, "2026-10-03T01:00:00.000000Z", "source_updated_at")]))
        # An update time outside the interval (not bumped): the capture's time, labelled.
        step(4, 6, deleted_at="2026-10-04T00:00:00", updated_at="2026-10-04T00:00:00")
        self.assertEqual(step(5, 6, deleted_at=None, updated_at="2026-09-01T00:00:00")[1][-1],
                         ("2026-10-04T00:00:00.000000Z", "2026-10-05T06:00:00.000000Z",
                          "capture_observed"))

    def test_a_restore_that_keeps_the_update_time_ends_at_the_capture(self):
        # Deleted 10-01 (the deletion bumps updated_at); restored by raw SQL that leaves
        # updated_at alone, read 10-05: never closed at the deletion's own update time.
        fake, clock = workbench([customer("c1")]), Clock()
        mirror = open_mirror(self, self.root / "raw.sqlite", fake, clock)
        mirror.rows(OPS, CUSTOMERS)
        for day, changes in ((1, {"deleted_at": "2026-10-01T00:00:00",
                                  "updated_at": "2026-10-01T00:00:00.002"}),
                             (5, {"deleted_at": None})):
            fake.update(CUSTOMERS, "c1", **changes)
            clock.now = dt.datetime(2026, 10, day, 6, tzinfo=dt.timezone.utc)
            mirror.refresh(OPS, CUSTOMERS, full=True)
        self.assertEqual(self.state(mirror), (None, [(
            "2026-10-01T00:00:00.000000Z", "2026-10-05T06:00:00.000000Z", "capture_observed")]))

    def test_soft_delete_and_missing_counts_are_distinct_keys(self):
        # Two duplicate-key runs: d1 (two rows, deleted), d2 (two rows, then missing).
        rows = [customer("d1", deleted_at="2026-09-28T08:00:00"),
                customer("d1", deleted_at="2026-09-28T08:00:00"),
                customer("d2"), customer("d2"), customer("d3")]
        fake, clock = workbench(rows), Clock()
        mirror = open_mirror(self, self.root / "dup.sqlite", fake, clock)
        mirror.rows(OPS, CUSTOMERS)
        fake.tables[CUSTOMERS] = [r for r in fake.tables[CUSTOMERS] if r["id"] != "d2"]
        clock.advance(3600)
        mirror.refresh(OPS, CUSTOMERS, full=True)
        [entry] = [r for r in mirror.coverage(OPS)["relations"] if r["relation"] == CUSTOMERS]
        self.assertEqual((entry["rows_deleted_in_source"], entry["rows_missing_from_source"]),
                         (1, 1))


class CaptureRetentionTests(unittest.TestCase):
    def test_removal_stretches_go_with_the_versions_retention_drops(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        company = "0f0e0d0c-0b0a-0908-0706-050403020100"
        now = [dt.datetime(2026, 10, 1, 8, tzinfo=dt.timezone.utc)]
        audience = LiveSourceAudience({"bc:production:*": ["svc"]}, tenant="plx",
                                      default_classification="restricted")
        capture = CaptureStore(Path(tmp.name) / "bc.sqlite", audience, clock=lambda: now[0],
                               max_versions_per_record=2)
        self.addCleanup(capture.close)
        unit = bc_unit(company, "items")
        rec = "aaaaaaaa-0000-0000-0000-000000000001"

        def item(content):
            return CapturedRecord(path=("Production", company.upper(), "items", rec),
                                  payload={"row": {"id": rec, "n": content}, "meta": {}},
                                  source_version=f'W/"{content}"', lineage="bc_manufacturing")
        # Missing 09-10 and 11-12 (gone with v1 and v2), 14-15 (outlives no version).
        for hour, records in ((8, [item(1)]), (9, []), (10, [item(1)]), (11, []),
                              (12, [item(2)]), (13, [item(3)]), (14, []), (15, [item(3)]),
                              (16, [item(4)])):
            now[0] = now[0].replace(hour=hour)
            run = capture.begin_run("bc", unit, [f"bc:production:{company}:items"])
            capture.save_page(run["run_id"], records, None)
            capture.finish_run(run["run_id"], "complete")
        [row] = capture._db.execute(f"SELECT *, {REMOVALS_COLUMN} FROM versions "
                                    "WHERE superseded_at IS NULL").fetchall()
        self.assertEqual(capture._db.execute("SELECT COUNT(*) FROM versions").fetchone()[0], 2)
        self.assertEqual(removals_of(row), (("2026-10-01T14:00:00.000000Z",
                                             "2026-10-01T15:00:00.000000Z",
                                             "missing_from_source"),))


class StaleStoreTests(unittest.TestCase):
    """A host serving a projection of an older store format answers rebuild_required."""

    def test_an_old_format_store_answers_rebuild_required_on_every_tool(self):
        from operating.case_runtime import CaseRuntime
        from operating.ontology_host import CaseHostTools
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        audience = root / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": ALL}))
        store = root / "projection.sqlite"
        build_store(store, ot.default_schema(), {}, normalizer=identifiers.normalize)
        runtime = CaseRuntime(root / "state", client=None, audience=audience, tenant="plx")
        self.addCleanup(runtime.close)
        calls = (("plx.find", {"query": "SO-1"}),
                 ("plx.object", {"object_id": customer_id("c1")}),
                 ("plx.process", {"from": customer_id("c1")}),
                 ("plx.timeline", {"from": customer_id("c1")}),
                 ("plx.identity", {"object_ids": [customer_id("c1")]}))
        host = CaseHostTools(runtime, OPS, store_path=store, bc=False)
        self.addCleanup(host.close)
        self.assertNotIn("rebuild_required", json.dumps(host.call("plx.find", calls[0][1], OPS)))
        with sqlite3.connect(store) as db:   # the v1 file: no removals, an older format
            db.execute("UPDATE store_meta SET value = 'plx-ontology-store/v1' "
                       "WHERE key = 'format'")
            db.execute("DROP TABLE object_removals")
        stale = CaseHostTools(runtime, OPS, store_path=store, bc=False)
        self.addCleanup(stale.close)
        stale.register_tool("plx.registered", lambda arguments, actor: {"read": True})
        calls += (("plx.registered", {}),)   # a host-registered tool (plx.review, plx.act)
        for name, arguments in calls:
            with self.subTest(tool=name):
                out = stale.call(name, arguments, OPS)
                self.assertEqual(out["error"]["code"], "rebuild_required", out)
                self.assertIn(store_ddl.FORMAT, out["error"]["message"])
                if name in tr.CONTRACTS["tools"]:
                    checked(self, name, out)


if __name__ == "__main__":
    unittest.main()
