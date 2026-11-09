"""Measured system-of-record precedence (``operating.source_precedence``): agreement with
downstream evidence measured per type x field on a store, read-only, and the read-time
choice among disagreeing copies (``operating.answer_conflicts``) it drives.

SYNTHETIC fixtures only (``World`` stores, synthetic handles); evidence label:
isolated-fixture.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from operating import answer_conflicts as ac
from operating import source_precedence as sp
from operating.tests.test_proactive_engine import World

NOW = dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc)
RULE = "SalesOrder.total_amount"
PORTAL, BC, FM = "gold_native_staging", "bc_synth", "fm_bronze"


def _set_lineage(w: World, oid: str, lineage: str) -> None:
    for table in ("object_versions", "object_values"):
        for row in w.rows[table]:
            if row["object_id"] == oid:
                row["lineage"] = lineage


def _twin(w: World, a: str, b: str) -> None:
    w.link("SalesOrder.same_record_as", a, b)
    w.rows["links"][-1]["binding_kind"] = "same_record"


def orders(n: int, *, portal_right: int, invoice: bool = True) -> World:
    """``n`` orders, each a Portal copy and a BC copy that disagree on the total; the
    first ``portal_right`` have invoices matching the Portal copy, the rest the BC copy."""
    w = World()
    for i in range(n):
        portal = w.obj("SalesOrder", {"total_amount": f"{100 + i}.00"}, binding="portal.so")
        bc = w.obj("SalesOrder", {"total_amount": f"{200 + i}.00"}, binding="bc.so")
        _set_lineage(w, portal, PORTAL)
        _set_lineage(w, bc, BC)
        _twin(w, portal, bc)
        if invoice:
            billed = f"{100 + i}.00" if i < portal_right else f"{200 + i}"
            inv = w.obj("SalesInvoice", {"total_amount": billed}, binding="bc.inv")
            _set_lineage(w, inv, BC)
            w.link("SalesInvoice.bills", inv, bc)      # linked to one copy only
    return w


def measured(w: World, d: str, **kw) -> dict:
    path = w.build(Path(d) / "s.sqlite")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        spec = [r for r in sp.load_spec() if r["subject"] == RULE]
        return sp.measure(conn, spec, store="fixture", now=NOW, **kw)
    finally:
        conn.close()


def reading(value: str, lineage: str, n: int, *, days_ago: int = 3) -> ac.Reading:
    return ac.Reading(value=value, handle=f"api://synth.t/{n}", field_path="row.total",
                      authority_rank=0 if lineage == PORTAL else 1, rank_scope="record",
                      as_of=NOW - dt.timedelta(days=days_ago), lineage=lineage)


def table(shares: dict[str, tuple[int, int]]) -> dict:
    return {"schema": sp.TABLE_SCHEMA, "measured_at": "2026-11-09T00:00:00Z", "rules": {
        RULE: {"evidence_label": "the posted invoices", "sources": {
            sp.source_of(lin): {"compared": n, "agreed": k, "disputed_compared": n,
                                "disputed_agreed": k} for lin, (k, n) in shares.items()}}}}


class Measure(unittest.TestCase):
    def test_agreement_per_source_one_ten_sixty_no_record_lost_or_doubled(self):
        for n in (1, 10, 60):
            right = n * 3 // 4
            with self.subTest(n=n), tempfile.TemporaryDirectory() as d:
                rule = measured(orders(n, portal_right=right), d)["rules"][RULE]
                self.assertEqual((rule["with_evidence"], rule["disputed"]), (n, n))
                self.assertEqual(rule["sources"]["portal"],
                                 {"compared": n, "agreed": right, "disputed_compared": n,
                                  "disputed_agreed": right})
                self.assertEqual(rule["sources"]["bc"]["agreed"], n - right)

    def test_a_value_read_from_the_evidence_record_is_not_compared(self):
        with tempfile.TemporaryDirectory() as d:
            w = orders(2, portal_right=2)
            # the BC order copy read off the invoice itself: the same record
            inv = [r for r in w.rows["objects"] if r["object_type"] == "SalesInvoice"][0]
            bc = [r for r in w.rows["objects"] if r["binding_id"] == "bc.so"][0]
            src = [v for v in w.rows["object_versions"] if v["object_id"] == inv["object_id"]]
            for table_ in ("object_versions", "object_values"):
                for row in w.rows[table_]:
                    if row["object_id"] == bc["object_id"]:
                        row["l1_handle"] = src[0]["l1_handle"]
            rule = measured(w, d)["rules"][RULE]
            self.assertEqual(rule["sources"]["bc"]["compared"], 1)
            self.assertEqual(rule["sources"]["portal"]["compared"], 2)

    def test_a_sample_keeps_whole_clusters(self):
        with tempfile.TemporaryDirectory() as d:
            rule = measured(orders(60, portal_right=30), d, sample=3)["rules"][RULE]
            self.assertLess(rule["with_evidence"], 60)
            self.assertGreater(rule["with_evidence"], 0)
            # every kept order has both copies: none is half a cluster
            self.assertEqual(rule["sources"]["portal"]["compared"],
                             rule["sources"]["bc"]["compared"])

    def test_the_store_is_opened_read_only_and_the_table_written_only_to_out(self):
        with tempfile.TemporaryDirectory() as d:
            path = orders(3, portal_right=1).build(Path(d) / "s.sqlite")
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            out = Path(d) / "t" / "measured.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = sp.main(["--store", str(path), "--out", str(out), "--only", RULE])
            self.assertEqual(code, 0)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            self.assertEqual(json.loads(out.read_text())["rules"][RULE]["disputed"], 3)

    def test_an_unknown_rule_key_or_aggregate_refuses_the_spec(self):
        raw = json.loads(sp.SPEC_FILE.read_text())
        with tempfile.TemporaryDirectory() as d:
            for bad in ({"aggregate": "avg"}, {"order": ["portal"]},
                        {"path": [{"links": ["X.y"], "dir": "sideways"}]},
                        {"tolerance_days": -1}):
                body = json.loads(json.dumps(raw))
                body["rules"][0].update(bad)
                path = Path(d) / "spec.json"
                path.write_text(json.dumps(body))
                with self.subTest(bad=bad), self.assertRaises(sp.SpecInvalid):
                    sp.load_spec(path)


class Decide(unittest.TestCase):
    def conflict(self, measured_table, readings):
        from unittest import mock
        with mock.patch.object(sp, "load_table", return_value=measured_table):
            return ac.resolve("total amount", readings, now=NOW, measured_as=RULE)

    def test_the_highest_agreement_wins_over_the_declared_order_and_says_why(self):
        found = self.conflict(table({PORTAL: (20, 100), BC: (90, 100)}),
                              [reading("100.00", PORTAL, 1), reading("200.00", BC, 2)])
        self.assertEqual(found["rule"], "measured_agreement")
        trusted = [v for v in found["values"] if v["trusted"]]
        self.assertEqual([v["value"] for v in trusted], ["200.00"])
        self.assertEqual(len(found["values"]), 2)                 # both values shown
        self.assertIn("BC 90%", trusted[0]["why"])
        self.assertIn("the Portal 20%", trusted[0]["why"])
        self.assertIn("the posted invoices", trusted[0]["why"])
        other = [v for v in found["values"] if not v["trusted"]][0]
        self.assertIn("Disagrees with", other["why"])
        self.assertNotIn("100 ", trusted[0]["why"])               # shares, never counts

    def test_a_tie_goes_to_the_most_recently_updated_copy(self):
        found = self.conflict(table({PORTAL: (80, 100), BC: (81, 100)}),
                              [reading("100.00", PORTAL, 1, days_ago=1),
                               reading("200.00", BC, 2, days_ago=9)])
        self.assertEqual(found["rule"], "measured_agreement")
        self.assertEqual([v["value"] for v in found["values"] if v["trusted"]], ["100.00"])
        self.assertIn("most recently updated", found["values"][0]["why"])
        # equally recent copies in a tie: no measured decision (the declared order applies)
        found = self.conflict(table({PORTAL: (80, 100), BC: (81, 100)}),
                              [reading("100.00", PORTAL, 1), reading("200.00", BC, 2)])
        self.assertEqual(found["rule"], "system_of_record")

    def test_no_decision_without_enough_measurement_or_a_known_source(self):
        cases = {"too few": table({PORTAL: (2, 5), BC: (5, 5)}),
                 "unmeasured source": table({PORTAL: (20, 100)}),
                 "foreign table": {"schema": "other", "rules": {}},
                 "no table": None}
        for name, measured_table in cases.items():
            with self.subTest(name):
                found = self.conflict(measured_table or {},
                                      [reading("100.00", PORTAL, 1), reading("200.00", BC, 2)])
                self.assertEqual(found["rule"], "system_of_record")   # declared rank 0
        no_lineage = [reading("100.00", PORTAL, 1),
                      ac.Reading(value="200.00", handle="bc://synth/2", as_of=NOW)]
        found = self.conflict(table({PORTAL: (20, 100), BC: (90, 100)}), no_lineage)
        self.assertNotEqual(found["rule"], "measured_agreement")

    def test_an_unreadable_table_file_decides_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.json"
            path.write_text("{not json")
            self.assertIsNone(sp.load_table(path))
            self.assertIsNone(sp.load_table(Path(d) / "missing.json"))

    def test_a_restricted_reader_sees_no_conflict_from_a_side_it_may_not_open(self):
        from unittest import mock
        conflict = {"kind": "value_disagreement", "subject": RULE, "values": [
            {"value": "100.00", "citation": {"l1_handle": "api://synth.t/1",
                                             "field_path": "row.total", "lineage": PORTAL}},
            {"value": "200.00", "citation": {"l1_handle": "bc://synth/2",
                                             "field_path": "row.total", "lineage": BC}}]}
        measured_table = table({PORTAL: (20, 100), BC: (90, 100)})
        with mock.patch.object(sp, "load_table", return_value=measured_table):
            both = ac.tool_conflicts([conflict], visible=lambda hs: set(), now=NOW)
            self.assertEqual(both, [])
            seen = ac.tool_conflicts([conflict], visible=lambda hs: set(hs), now=NOW)
            self.assertEqual(seen[0]["rule"], "measured_agreement")
            one = ac.tool_conflicts([conflict], now=NOW,
                                    visible=lambda hs: {h for h in hs if h.startswith("api")})
            self.assertEqual(one, [])                 # never half a conflict
            summary = ac.resolution(conflict, now=NOW)
            self.assertEqual((summary["rule"], summary["trusted"]), ("measured_agreement", [1]))


class CommittedTable(unittest.TestCase):
    def test_the_committed_table_is_readable_and_keyed_by_the_spec(self):
        found = sp.load_table(sp.TABLE_FILE)
        self.assertIsNotNone(found)
        self.assertLessEqual(set(found["rules"]), {r["subject"] for r in sp.load_spec()})


if __name__ == "__main__":
    unittest.main()
