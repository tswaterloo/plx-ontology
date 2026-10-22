"""Capture store contract (``integrations.capture_store``): registry, handles, versions.

Pure SQLite in a temp directory; no source I/O. Evidence label: ``isolated-fixture``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from integrations import capture_store
from integrations.capture_store import (BC_KIND, CaptureError, CaptureStore, CapturedRecord,
                                        SourceKind, bc_unit, is_capture_handle, make_handle,
                                        parse_handle, register_source_kind)
from integrations.live_source_guard import LiveSourceAudience
from ontology.core import Actor
from ontology.store_ddl import LINEAGES

COMPANY = "0f0e0d0c-0b0a-0908-0706-050403020100"
REC = "aaaaaaaa-0000-0000-0000-000000000001"
REC2 = "aaaaaaaa-0000-0000-0000-000000000002"
SVC = Actor("plx", frozenset({"svc"}), "restricted")
RES = [f"bc:production:{COMPANY}:items"]
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def record(rec: str, etag: str, value: str, lineage: str = "bc_manufacturing",
           entity_set: str = "items") -> CapturedRecord:
    return CapturedRecord(path=("Production", COMPANY.upper(), entity_set, rec),
                          payload={"row": {"id": rec, "unitPrice": value}, "meta": {"etag": etag}},
                          source_version=etag, lineage=lineage)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / "c.sqlite"
        self.now = T0
        self.audience = LiveSourceAudience({"bc:production:*": ["svc"]}, tenant="plx",
                                           default_classification="restricted")
        self.store = CaptureStore(self.path, self.audience, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.unit = bc_unit(COMPANY, "items")

    def capture(self, *records: CapturedRecord, state: str = "complete") -> dict:
        run = self.store.begin_run("bc", self.unit, RES)
        self.store.save_page(run["run_id"], list(records), None)
        return self.store.finish_run(run["run_id"], state)

    def test_versions_supersede_and_as_known_at_reads_history(self):
        self.capture(record(REC, 'W/"1"', "1.10"), record(REC2, 'W/"a"', "5"))
        self.now = T0 + timedelta(hours=2)
        self.capture(record(REC, 'W/"2"', "1.20"), record(REC2, 'W/"a"', "5"))
        items = self.store.records(SVC, "bc", self.unit)["items"]
        self.assertEqual([i["row"]["unitPrice"] for i in items], ["1.20", "5"])
        before = self.store.records(SVC, "bc", self.unit, as_known_at="2026-10-01T09:00:00Z")
        self.assertEqual([i["row"]["unitPrice"] for i in before["items"]], ["1.10", "5"])
        self.assertEqual(self.store.records(SVC, "bc", self.unit,
                                            as_known_at="2026-10-01T07:00:00Z")["items"], [])
        old = self.store.resolve(before["items"][0]["citation"], SVC)
        self.assertEqual((old["status"], old["capture"]["superseded_at"]),
                         ("superseded", "2026-10-01T10:00:00.000000Z"))
        self.assertEqual(old["current_citation"], items[0]["citation"])

    def test_handles_have_one_spelling_and_differ_from_live_etag_citations(self):
        self.capture(record(REC, 'W/"1"', "1"))
        handle = self.store.records(SVC, "bc", self.unit)["items"][0]["citation"]
        self.assertEqual(handle, f"bc://production/{COMPANY}/items/{REC}@"
                                 f"{self.store.generation}-1")
        kind, path, generation, capture_id = parse_handle(handle)
        self.assertEqual((kind, path[0], capture_id), (BC_KIND, "production", 1))
        for alias in (handle.replace("production", "Production"),
                      handle.replace(REC, REC.upper()), handle + "#x=1",
                      f"bc://production/{COMPANY}/items/{REC}@W%2F%221%22",
                      f"api://public.items/%7B%7D@{generation}-1"):
            self.assertFalse(is_capture_handle(alias), alias)
            self.assertEqual(self.store.resolve(alias, SVC)["error"], "invalid_handle")

    def test_resumed_run_keeps_its_cursor_and_finish_requires_an_open_run(self):
        run = self.store.begin_run("bc", self.unit, RES)
        self.store.save_page(run["run_id"], [record(REC, 'W/"1"', "1")], "https://next/1")
        again = self.store.begin_run("bc", self.unit, RES)
        self.assertEqual((again["run_id"], again["cursor"], again["resumed"]),
                         (run["run_id"], "https://next/1", True))
        self.store.finish_run(run["run_id"], "complete")
        with self.assertRaises(CaptureError):
            self.store.save_page(run["run_id"], [], None)
        self.assertFalse(self.store.begin_run("bc", self.unit, RES)["resumed"])

    def test_records_outside_the_unit_or_with_a_non_c0_lineage_are_refused(self):
        run = self.store.begin_run("bc", self.unit, RES)
        for bad in (record(REC, 'W/"1"', "1", entity_set="vendors"),
                    record(REC, 'W/"1"', "1", lineage="portal"),
                    record(REC, 'W/"1"', "1", lineage="bc_"),
                    record(REC, "", "1")):
            with self.assertRaises(CaptureError):
                self.store.save_page(run["run_id"], [bad], None)
        self.assertEqual(self.store.records(SVC, "bc", self.unit)["items"], [])
        for lineage in LINEAGES:
            self.assertTrue(capture_store.lineage_ok(lineage))

    def test_a_partial_run_marks_nothing_missing(self):
        self.capture(record(REC, 'W/"1"', "1"), record(REC2, 'W/"a"', "5"))
        self.assertEqual(self.capture(record(REC, 'W/"1"', "1"), state="partial")
                         ["marked_missing"], 0)
        self.assertEqual(len(self.store.records(SVC, "bc", self.unit)["items"]), 2)
        self.assertEqual(self.capture(record(REC, 'W/"1"', "1"))["marked_missing"], 1)
        self.assertEqual(len(self.store.records(SVC, "bc", self.unit)["items"]), 1)
        self.assertEqual(len(self.store.records(SVC, "bc", self.unit,
                                                include_missing=True)["items"]), 2)

    def test_a_unit_is_checked_against_its_declared_resources_even_when_empty(self):
        nobody = Actor("plx", frozenset({"other"}), "restricted")
        with self.assertRaises(CaptureError):  # never captured: nothing to tell
            self.store.records(SVC, "bc", self.unit)
        self.capture()  # a complete traversal that found no record
        self.assertEqual(self.store.records(SVC, "bc", self.unit)["items"], [])
        self.assertEqual(self.store.completeness(SVC, "bc", self.unit)["state"], "complete")
        for call in (lambda: self.store.records(nobody, "bc", self.unit),
                     lambda: self.store.completeness(nobody, "bc", self.unit)):
            with self.assertRaises(CaptureError) as denied:
                call()
            self.assertEqual(denied.exception.code, "not_visible")
        run = self.store.begin_run("bc", self.unit, RES)
        with self.assertRaises(CaptureError):  # one unit, one declared audience
            self.store.begin_run("bc", self.unit, [f"bc:production:{COMPANY}:vendors"])
        with self.assertRaises(CaptureError):  # a record must carry the run's resources
            self.store.save_page(run["run_id"], [record(REC, 'W/"1"', "1",
                                                        entity_set="vendors")], None)

    def test_retention_bound_drops_old_superseded_versions_and_reports_it(self):
        self.store.close()
        self.store = CaptureStore(self.path, self.audience, clock=lambda: self.now,
                                  max_versions_per_record=2)
        handles = []
        for n in range(3):
            self.now = T0 + timedelta(hours=n)
            self.capture(record(REC, f'W/"{n}"', str(n)))
            handles.append(self.store.records(SVC, "bc", self.unit)["items"][0]["citation"])
        first = self.store.resolve(handles[0], SVC)
        self.assertEqual((first["error"], first["reason"]),
                         ("capture_unknown", "pruned_by_retention"))
        self.assertEqual(self.store.resolve(handles[1], SVC)["status"], "superseded")
        self.assertEqual(self.store.resolve(handles[2], SVC)["status"], "current")
        report = self.store.operator_completeness(SVC, "bc", self.unit)
        self.assertEqual((report["versions_total"], report["retention"]),
                         (2, {"max_versions_per_record": 2, "pruned_versions": 1}))
        self.assertGreater(report["payload_bytes"], 0)

    def test_one_restrictive_audience_discloses_the_same_through_every_reader_path(self):
        # capture-bc-acl-count-metadata: an item ACL the reader fails
        # must withhold the same way from resolve, records and completeness.
        self.capture(record(REC, 'W/"1"', "1.10"), record(REC2, 'W/"a"', "5"))
        handle = self.store.records(SVC, "bc", self.unit)["items"][0]["citation"]
        strict = LiveSourceAudience({"bc:production:*": ["svc"]}, tenant="plx",
                                    default_classification="restricted",
                                    source_acl_required=RES)
        with CaptureStore(self.path, strict, clock=lambda: self.now) as store:
            self.assertEqual(store.resolve(handle, SVC)["error"], "not_visible")
            # A hidden record and a path never captured answer alike (no existence
            # oracle, never a confident absence), in this store generation or another.
            absent = handle.replace(REC, "aaaaaaaa-0000-0000-0000-0000000000ff")
            other = handle.rsplit("@", 1)[0] + "@" + "0" * 16 + "-1"
            for probe in (handle, absent, other, other.replace(REC, REC2)):
                answer = store.resolve(probe, SVC)
                self.assertEqual({k: v for k, v in answer.items() if k != "citation"},
                                 {"ok": False, "error": "not_visible",
                                  "message": "the cited record is not visible to this actor"})
            listed = store.records(SVC, "bc", self.unit)
            self.assertEqual((listed["items"], listed["withheld_count"]), ([], 2))
            seen = store.completeness(SVC, "bc", self.unit)
            self.assertEqual((seen["records_visible"], seen["withheld_count"]), (0, 2))
            self.assertFalse(seen["complete"])
            self.assertEqual((seen["state"], seen["reason"]), ("complete", "withheld"))
            for raw in ("payload_bytes", "versions_total", "retention", "records",
                        "new_versions", "pages", "withheld", "records_current", "run_id"):
                self.assertNotIn(raw, seen)
            # The operator method keeps the raw counters (capture actor reports).
            self.assertEqual(store.operator_completeness(SVC, "bc", self.unit)
                             ["versions_total"], 2)
        # A reader who sees every record gets the plain view, complete.
        seen = self.store.completeness(SVC, "bc", self.unit)
        self.assertEqual((seen["records_visible"], seen["withheld_count"], seen["complete"]),
                         (2, 0, True))
        self.assertNotIn("payload_bytes", seen)

    def test_mail_capture_is_a_registered_source_kind(self):
        # Decision 2026-10-02-mail-and-mentions-store #1: one mail store behind the
        # registry, found without importing the adapter first.
        self.assertIn("graph_mail", capture_store.source_kinds())
        kind = capture_store.source_kind("graph_mail")
        self.assertEqual((kind.scheme, kind.path_parts), ("graph-mail", 2))
        box = "12345678-aaaa-bbbb-cccc-00000000000a"
        path = kind.canonical_path([box.upper(), "AAMkMSG="])
        self.assertEqual((kind.unit(path), kind.resources(path), kind.key(path)),
                         (f"mail:{box}", (f"mail:{box}",), {"mailbox": box, "id": "AAMkMSG="}))
        # A live/captured mail citation (changeKey tail) is never a capture-store handle.
        self.assertFalse(is_capture_handle(f"graph-mail://{box}/AAMkMSG=@CQAAABYAAAD"))
        with self.assertRaises(ValueError):
            kind.canonical_path(["not-a-mailbox", "AAMkMSG="])

    def test_registry_refuses_a_conflicting_kind(self):
        self.assertIn("bc", capture_store.source_kinds())
        clash = SourceKind(name="bc", scheme="bc2", path_parts=1,
                           canonical_path=tuple, unit=str, resources=lambda p: (),
                           key=lambda p: {})
        with self.assertRaises(ValueError):
            register_source_kind(clash)
        with self.assertRaises(CaptureError):
            self.store.begin_run("nope", "x", RES)
        with self.assertRaises(ValueError):
            make_handle(BC_KIND, ("production", COMPANY, "items", REC), "zz", 1)

    def test_open_read_only_refuses_a_write_with_a_wal_file_present(self):
        self.capture(record(REC, 'W/"1"', "1.10"))
        self.store.close()  # no other connection; a clean close removes the -wal file
        self.path.with_name(self.path.name + "-wal").write_bytes(b"")
        reader = CaptureStore.open_read_only(self.path, self.audience)
        self.addCleanup(reader.close)
        self.assertEqual(reader.generation, self.store.generation)
        with self.assertRaises(sqlite3.OperationalError):
            reader._db.execute("INSERT INTO meta VALUES ('x', '1')")


if __name__ == "__main__":
    unittest.main()
