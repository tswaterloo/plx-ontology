"""PlaybookReportStore over real evaluation reports (AO-26005B, injected-transport)."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from operating import playbook_eval as pe
from operating.playbook_reports import (
    PlaybookReportStore, ReportError, ReportIntegrityError, ReportNotFound)
from operating.tests.test_playbook_eval import AO, AoEvalCase


def rehash(report):
    """A well-formed synthetic report (valid hash) with edited content, for filter tests."""
    body = {k: v for k, v in report.items() if k != "report_sha256"}
    body["report_sha256"] = hashlib.sha256(pe._canonical(body)).hexdigest()
    return body


class StoreCase(AoEvalCase):
    audience = {"alice": {"*"}}

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "reports.sqlite3"
        self.ticks = 0
        self.store = self.make_store(lambda a, c: c in self.audience.get(a, ()) or
                                     "*" in self.audience.get(a, ()))
        self.case_id = self.investigate()["case_id"]
        self.report = self.evaluate(self.case_id)

    def make_store(self, can_read):
        def clock():
            self.ticks += 1
            return datetime(2026, 10, 1, 12, 0, self.ticks, tzinfo=timezone.utc)
        return PlaybookReportStore(self.path, can_read=can_read, clock=clock)

    def raw(self):
        return sqlite3.connect(self.path, isolation_level=None)


class RecordGetListTests(StoreCase):
    def test_record_and_get_round_trip_with_indexed_columns(self):
        out = self.store.record(self.report, evaluated_by="alice", transcript_source="none")
        self.assertEqual(out["duplicate"], False)
        got = self.store.get("alice", out["id"])
        self.assertEqual(got["report"], self.report)
        self.assertTrue(pe.verify_report_hash(got["report"]))
        self.assertEqual(got["case_id"], self.case_id)
        self.assertEqual(got["playbook_id"], AO.id)
        self.assertEqual(got["playbook_version"], str(AO.version))
        self.assertEqual(got["definition_sha256"], AO.definition_sha256)
        self.assertEqual(got["evaluator_version"], self.report["evaluator_version"])
        self.assertEqual(got["manifest_sha256"],
                         self.report["evidence_manifest"]["manifest_sha256"])
        self.assertEqual(got["case_boundary_sha256"], hashlib.sha256(
            pe._canonical(self.report["case_boundary"])).hexdigest())
        self.assertEqual(got["passed_all"], self.report["summary"]["passed_all"])
        self.assertEqual(got["passed_all_clean"], self.report["summary"]["passed_all_clean"])
        self.assertEqual((got["recorded_by"], got["transcript_source"]), ("alice", "none"))
        self.assertTrue(got["recorded_at"].startswith("2026-10-01T12:00:"))
        self.assertEqual(self.store.integrity("alice")["ok"], True)

    def test_list_is_newest_first_and_filters_by_case_and_playbook(self):
        first = self.store.record(self.report, evaluated_by="alice", transcript_source="none")
        with_transcript = self.evaluate(self.case_id, transcript=list(self.transcript))
        second = self.store.record(with_transcript, evaluated_by="alice",
                                   transcript_source="harness-transcript")
        other = rehash({**copy.deepcopy(self.report),
                        "playbook": {"id": "other_playbook", "version": "1"}})
        third = self.store.record(other, evaluated_by="alice", transcript_source="none")
        ids = [row["id"] for row in self.store.list("alice")]
        self.assertEqual(ids, [third["id"], second["id"], first["id"]])
        self.assertEqual([r["id"] for r in self.store.list("alice", playbook_id=AO.id)],
                         [second["id"], first["id"]])
        self.assertEqual(self.store.list("alice", case_id="nope"), [])
        self.assertNotIn("report", self.store.list("alice")[0])

    def test_latest_returns_the_newest_for_case_playbook_and_version(self):
        self.store.record(self.report, evaluated_by="alice", transcript_source="none")
        newer = self.store.record(self.evaluate(self.case_id, transcript=list(self.transcript)),
                                  evaluated_by="alice", transcript_source="t")
        got = self.store.latest("alice", self.case_id, AO.id, AO.version)
        self.assertEqual(got["id"], newer["id"])
        self.assertEqual(got["report"]["transcript"], "supplied")
        self.assertIsNone(self.store.latest("alice", self.case_id, AO.id, "999"))
        self.assertIsNone(self.store.latest("alice", "nope", AO.id, AO.version))

    def test_a_second_case_is_kept_separately(self):
        self.store.record(self.report, evaluated_by="alice", transcript_source="none")
        other_case = self.investigate()["case_id"]
        other = self.evaluate(other_case)
        self.store.record(other, evaluated_by="alice", transcript_source="none")
        self.assertNotEqual(other_case, self.case_id)
        self.assertEqual(len(self.store.list("alice")), 2)
        self.assertEqual(len(self.store.list("alice", case_id=other_case)), 1)

    def test_a_tampered_report_is_refused_and_nothing_is_stored(self):
        forged = copy.deepcopy(self.report)
        forged["checks"][0]["passed"] = not forged["checks"][0]["passed"]
        with self.assertRaises(ReportError):
            self.store.record(forged, evaluated_by="alice", transcript_source="none")
        self.assertEqual(self.store.list("alice"), [])

    def test_missing_provenance_is_refused(self):
        for who, source in (("", "none"), ("alice", ""), (None, "none")):
            with self.assertRaises(ReportError):
                self.store.record(self.report, evaluated_by=who, transcript_source=source)
        with self.assertRaises(ReportError):
            self.store.record(rehash({"schema": "x"}), evaluated_by="alice",
                              transcript_source="none")

    def test_recording_the_same_report_twice_returns_the_existing_id(self):
        first = self.store.record(self.report, evaluated_by="alice", transcript_source="none")
        again = self.store.record(self.report, evaluated_by="alice", transcript_source="other")
        self.assertEqual((again["id"], again["duplicate"]), (first["id"], True))
        self.assertEqual(len(self.store.list("alice")), 1)
        self.assertEqual(self.store.get("alice", first["id"])["transcript_source"], "none")


class ImmutabilityAndIntegrityTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.rid = self.store.record(self.report, evaluated_by="alice",
                                     transcript_source="none")["id"]

    def test_update_and_delete_are_refused_by_the_database(self):
        db = self.raw()
        self.addCleanup(db.close)
        for sql in ("UPDATE playbook_report SET recorded_by='mallory'",
                    "UPDATE playbook_report SET report_json='{}'",
                    "DELETE FROM playbook_report"):
            with self.assertRaises(sqlite3.DatabaseError) as ctx:
                db.execute(sql)
            self.assertIn("append_only", str(ctx.exception))
        self.assertEqual(self.store.get("alice", self.rid)["report"], self.report)

    def tamper(self, mutate):
        """Edit stored JSON the way an attacker with file access would: drop the trigger first."""
        db = self.raw()
        try:
            db.execute("DROP TRIGGER playbook_report_no_update")
            mutate(db)
        finally:
            db.close()

    def test_edited_stored_report_json_fails_integrity_and_get_refuses(self):
        def mutate(db):
            forged = copy.deepcopy(self.report)
            forged["summary"]["passed_all_clean"] = not forged["summary"]["passed_all_clean"]
            db.execute("UPDATE playbook_report SET report_json=?",
                       (json.dumps(forged, sort_keys=True, separators=(",", ":")),))
        self.tamper(mutate)
        self.assertFalse(self.store.integrity("alice")["ok"])
        with self.assertRaises(ReportIntegrityError):
            self.store.get("alice", self.rid)
        with self.assertRaises(ReportIntegrityError):
            self.store.list("alice")
        with self.assertRaises(ReportIntegrityError):
            self.store.latest("alice", self.case_id, AO.id, AO.version)

    def test_a_forged_report_with_a_recomputed_report_hash_still_fails_the_row_chain(self):
        def mutate(db):
            forged = rehash({**copy.deepcopy(self.report), "case_id": "other"})
            db.execute("UPDATE playbook_report SET report_json=?, report_sha256=?, case_id='other'",
                       (pe._canonical(forged).decode(), forged["report_sha256"]))
        self.tamper(mutate)
        self.assertFalse(self.store.integrity("alice")["ok"])
        with self.assertRaises(ReportIntegrityError):
            self.store.get("alice", self.rid)

    def test_an_edited_index_column_is_detected(self):
        self.tamper(lambda db: db.execute("UPDATE playbook_report SET passed_all_clean=1-"
                                          "passed_all_clean"))
        self.assertFalse(self.store.integrity("alice")["ok"])
        with self.assertRaises(ReportIntegrityError):
            self.store.get("alice", self.rid)

    def test_a_raw_insert_that_breaks_the_chain_is_detected(self):
        second = self.evaluate(self.case_id, transcript=list(self.transcript))
        self.store.record(second, evaluated_by="alice", transcript_source="t")
        self.assertTrue(self.store.integrity("alice")["ok"])
        db = self.raw()
        try:
            row = db.execute("SELECT report_sha256, report_json FROM playbook_report "
                             "WHERE id=1").fetchone()
            forged = rehash({**json.loads(row[1]), "evaluator_version": "forged"})
            db.execute(
                "INSERT INTO playbook_report(report_sha256,case_id,playbook_id,playbook_version,"
                "definition_sha256,evaluator_version,case_boundary_sha256,manifest_sha256,"
                "passed_all,passed_all_clean,recorded_at,recorded_by,transcript_source,"
                "report_json,prev_hash,row_hash) VALUES(?,?,?,?,?,?,?,?,1,1,?,?,?,?,?,?)",
                (forged["report_sha256"], self.case_id, AO.id, str(AO.version),
                 AO.definition_sha256, "forged", "x", "y", "2026-10-01T00:00:00+00:00",
                 "mallory", "none", pe._canonical(forged).decode(), "0" * 64, "f" * 64))
        finally:
            db.close()
        result = self.store.integrity("alice")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reports"], 2)
        with self.assertRaises(ReportIntegrityError):
            self.store.list("alice")

    def test_a_deleted_trigger_row_removal_breaks_the_chain(self):
        second = self.evaluate(self.case_id, transcript=list(self.transcript))
        self.store.record(second, evaluated_by="alice", transcript_source="t")
        db = self.raw()
        try:
            db.execute("DROP TRIGGER playbook_report_no_delete")
            db.execute("DELETE FROM playbook_report WHERE id=1")
        finally:
            db.close()
        self.assertFalse(self.store.integrity("alice")["ok"])


class AudienceTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.audience = {"alice": {"*"}, "bob": {self.case_id}, "carol": {"another-case"}}
        self.rid = self.store.record(self.report, evaluated_by="alice",
                                     transcript_source="none")["id"]
        self.other_case_report = rehash({**copy.deepcopy(self.report), "case_id": "another-case"})
        self.other_id = self.store.record(self.other_case_report, evaluated_by="alice",
                                          transcript_source="none")["id"]

    def test_default_store_denies_every_read_and_every_record(self):
        closed = PlaybookReportStore(self.path)
        with self.assertRaises(ReportNotFound):
            closed.get("alice", self.rid)
        self.assertEqual(closed.list("alice"), [])
        self.assertIsNone(closed.latest("alice", self.case_id, AO.id, AO.version))
        with self.assertRaises(PermissionError):
            closed.record(self.report, evaluated_by="alice", transcript_source="none")

    def test_no_read_without_an_actor(self):
        for actor in (None, "", "  "):
            with self.assertRaises(PermissionError):
                self.store.get(actor, self.rid)
            with self.assertRaises(PermissionError):
                self.store.list(actor)
            with self.assertRaises(PermissionError):
                self.store.latest(actor, self.case_id, AO.id, AO.version)
            with self.assertRaises(PermissionError):
                self.store.integrity(actor)

    def test_reads_are_filtered_per_case(self):
        self.assertEqual({r["id"] for r in self.store.list("alice")}, {self.rid, self.other_id})
        self.assertEqual([r["id"] for r in self.store.list("bob")], [self.rid])
        self.assertEqual([r["id"] for r in self.store.list("carol")], [self.other_id])
        self.assertEqual(self.store.list("mallory"), [])
        self.assertEqual(self.store.get("bob", self.rid)["case_id"], self.case_id)
        with self.assertRaises(ReportNotFound):
            self.store.get("bob", self.other_id)
        with self.assertRaises(ReportNotFound):
            self.store.get("mallory", self.rid)
        self.assertIsNone(self.store.latest("carol", self.case_id, AO.id, AO.version))
        self.assertIsNotNone(self.store.latest("bob", self.case_id, AO.id, AO.version))

    def test_unreadable_and_absent_are_indistinguishable(self):
        with self.assertRaises(ReportNotFound) as hidden:
            self.store.get("mallory", self.rid)
        with self.assertRaises(ReportNotFound) as absent:
            self.store.get("mallory", 9999)
        self.assertEqual(str(hidden.exception), str(absent.exception))

    def test_a_recorder_outside_the_case_audience_cannot_probe_for_duplicates(self):
        with self.assertRaises(PermissionError):
            self.store.record(self.report, evaluated_by="carol", transcript_source="none")

    def test_a_failing_audience_check_denies(self):
        def boom(actor, case_id):
            raise RuntimeError("directory down")
        broken = PlaybookReportStore(self.path, can_read=boom)
        self.assertEqual(broken.list("alice"), [])

    def test_integrity_reports_no_case_ids(self):
        result = self.store.integrity("mallory")
        self.assertEqual(result["ok"], True)
        self.assertNotIn(self.case_id, json.dumps(result))


class SourceFilterTests(StoreCase):
    ORIGINAL = "original://orig-" + "a" * 32 + "#page=2"
    SHAREPOINT = "sharepoint://drive-1/item-9"

    def setUp(self) -> None:
        super().setUp()
        report = copy.deepcopy(self.report)
        entries = report["evidence_manifest"]["entries"]
        entries.append({"handle": self.ORIGINAL, "recorded_digest": "d1", "current_digest": "d1",
                        "status": "resolved", "cited_by": ["F1"], "reread": "ok",
                        "reread_payload_sha256": None, "identity": {"freight_quote": "verified_kind"}})
        entries.append({"handle": self.SHAREPOINT, "recorded_digest": "d2", "current_digest": "d2",
                        "status": "resolved", "cited_by": ["F1"], "reread": "ok",
                        "reread_payload_sha256": None, "identity": {}})
        report["note"] = f"see {self.ORIGINAL} and {self.SHAREPOINT}"
        self.stored = rehash(report)
        self.rid = self.store.record(self.stored, evaluated_by="alice",
                                     transcript_source="none")["id"]
        self.audience["bob"] = {self.case_id}

    def test_a_reader_without_source_permission_sees_a_marker_not_the_handles(self):
        got = self.store.get("bob", self.rid)
        text = json.dumps(got["report"])
        for secret in (self.ORIGINAL, self.SHAREPOINT, "orig-" + "a" * 32, "drive-1", "verified_kind"):
            self.assertNotIn(secret, text)
        self.assertTrue(got["report"]["report_filtered"])
        withheld = [e for e in got["report"]["evidence_manifest"]["entries"] if e.get("withheld")]
        self.assertEqual(len(withheld), 2)
        self.assertTrue(all(e["handle"] is None and "identity" not in e for e in withheld))
        self.assertEqual(got["manifest_sha256"], self.stored["evidence_manifest"]["manifest_sha256"])
        latest = self.store.latest("bob", self.case_id, AO.id, AO.version)
        self.assertNotIn(self.ORIGINAL, json.dumps(latest["report"]))

    def test_the_recorder_and_a_permitted_reader_keep_the_handles(self):
        self.assertEqual(self.store.get("alice", self.rid)["report"], self.stored)
        store = PlaybookReportStore(
            self.path, can_read=lambda a, c: True,
            can_read_source=lambda a, c, h: a == "bob" and h != self.SHAREPOINT)
        got = json.dumps(store.get("bob", self.rid)["report"])
        self.assertIn(self.ORIGINAL, got)
        self.assertNotIn(self.SHAREPOINT, got)
        self.assertNotIn(self.ORIGINAL, json.dumps(store.get("carol", self.rid)["report"]))

    def test_mail_and_bc_handles_and_claims_on_withheld_sources_are_withheld(self):
        from operating.cases import Actor
        mail, bc = "graph-mail://box@x.test/msg-1@ck#body_sha256=ab", "bc://co-1/purchaseOrders(1)"
        report = copy.deepcopy(self.stored)
        report["evidence_manifest"]["entries"].append({
            "handle": mail, "status": "resolved", "cited_by": ["F9"], "reread": "ok",
            "recorded_digest": "d", "current_digest": "d", "identity": {}})
        report["note"] = f"{mail} {bc}"
        report["claim_checks"] = {"items": [
            {"item": "F9", "claims": [{"value": "11", "citation": self.ORIGINAL,
                                       "field": "text", "status": "passed"}], "leads": [
                {"value": "11"}]},
            {"item": "F1", "claims": [{"value": "7", "citation": "api://a/1", "field": "row.q",
                                       "status": "passed"}], "leads": []}],
            "failures": [{"item": "F9", "code": "claim_value_mismatch", "value": "11",
                          "detail": "x"}]}
        report = rehash(report)
        rid = self.store.record(report, evaluated_by="alice", transcript_source="none")["id"]
        text = json.dumps(self.store.get("bob", rid)["report"])
        for secret in (mail, bc, "box@x.test", '"11"', self.ORIGINAL):
            self.assertNotIn(secret, text)
        self.assertIn('"7"', text)  # an api:// claim the reader may see is kept
        # a recorder reading as an Actor (console) keeps their handles
        self.assertIn(mail, json.dumps(self.store.get("alice", rid)["report"]))
        actor = Actor("plx", ("alice-p",))
        store = PlaybookReportStore(self.path, can_read=lambda a, c: True)
        row = store.record(rehash({**report, "note": "r2"}), evaluated_by="alice-p",
                           transcript_source="none")["id"]
        self.assertIn(mail, json.dumps(store.get(actor, row)["report"]))

    def test_a_denied_api_handle_is_scrubbed_everywhere(self):
        api = self.report["evidence_manifest"]["entries"][0]["handle"]
        store = PlaybookReportStore(self.path, can_read=lambda a, c: True,
                                    can_read_source=lambda a, c, h: h != api)
        text = json.dumps(store.get("bob", self.rid)["report"])
        self.assertNotIn(api, text)

    def test_a_failing_source_check_withholds(self):
        def boom(a, c, h):
            raise RuntimeError("directory down")
        store = PlaybookReportStore(self.path, can_read=lambda a, c: True, can_read_source=boom)
        self.assertNotIn(self.ORIGINAL, json.dumps(store.get("alice", self.rid)["report"]))


if __name__ == "__main__":
    unittest.main()
