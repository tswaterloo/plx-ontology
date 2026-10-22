"""Injected contract tests for the permission-enforcing Workbench source mirror.

A fake transport emulates the deployed Workbench API (health, catalog and keyset
row pages with the upstream ``since`` comparison for each mode) behind the real
``WorkbenchReadApiClient``; nothing touches the network.
"""

import base64
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from integrations.source_mirror import (AudienceMap, MirrorError, SourceMirror,
    citation_generation, main, make_citation, parse_citation, parse_decimal)
from integrations.http import SourceHttpError
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor


KEY = "plxwb_" + "m" * 40
BASE = "https://api.example.test"
SINCE_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")

CATALOG = {
    "public.assembly_order": {
        "columns": ["id", "nameamo_c", "lotno", "qtytotal", "audittimestampmodified"],
        "key": ["id"], "domain": "manufacturing",
        "since": {"column": "audittimestampmodified", "mode": "timestamp-utc"}},
    "public.app_purchase_order": {
        "columns": ["id", "po_number", "tracking_number", "total_cost", "status",
                    "shipped_at", "updated_at"],
        "key": ["id"], "domain": "purchasing",
        "since": {"column": "updated_at", "mode": "timestamptz"}},
    "public.app_sales_order": {
        "columns": ["id", "order_number", "po_reference", "total_amount", "custom_fields",
                    "updated_at"],
        "key": ["id"], "domain": "orders",
        "since": {"column": "updated_at", "mode": "timestamp-utc"}},
    "sage.po_header": {
        "columns": ["_sage_pk", "ponumber", "rqnnumber", "doctotal", "audtdate"],
        "key": ["_sage_pk"], "domain": "purchasing",
        "since": {"column": "audtdate", "mode": "yyyymmdd-text"}},
    "public.customer": {
        "columns": ["id", "code", "name", "status"], "key": ["id"],
        "domain": "customers", "since": None},
    "silver.shipments": {
        "columns": ["id", "shipment_code", "status"], "key": ["id"],
        "domain": "orders", "since": None},
}

TABLES = {
    "public.assembly_order": [
        {"id": 10718, "nameamo_c": "AO-26005B", "lotno": "L-1", "qtytotal": None,
         "audittimestampmodified": "2026-09-29T10:00:00"},
        {"id": 10719, "nameamo_c": "AO-26006", "lotno": None, "qtytotal": "1509",
         "audittimestampmodified": "2026-09-29T11:00:00.5"},
        {"id": 10720, "nameamo_c": "AO-26007", "lotno": None, "qtytotal": "12",
         "audittimestampmodified": None},
        {"id": 10721, "nameamo_c": "AO-26008", "lotno": None, "qtytotal": "3",
         "audittimestampmodified": "2026-09-29T09:00:00"},
    ],
    "public.app_purchase_order": [
        {"id": "po-a", "po_number": "PO-1001", "tracking_number": "1Z999",
         "total_cost": "35.099999999999994", "status": "OPEN",
         "shipped_at": "2026-09-20T00:00:00Z", "updated_at": "2026-09-29T11:00:00.000000Z"},
        {"id": "po-b", "po_number": "PO-1002", "tracking_number": None,
         "total_cost": "120.00", "status": "RECEIVED", "shipped_at": None,
         "updated_at": "2026-09-28T08:00:00Z"},
        {"id": "po-c", "po_number": "PX-7", "tracking_number": None,
         "total_cost": "9.5", "status": "OPEN", "shipped_at": "2026-09-25T00:00:00Z",
         "updated_at": "2026-09-27T08:00:00Z"},
    ],
    "public.app_sales_order": [
        {"id": 1, "order_number": "OR1000151885", "po_reference": "po-1001",
         "total_amount": "10.00", "custom_fields": None,
         "updated_at": "2026-09-29T09:00:00"},
        {"id": 2, "order_number": "SO-FM-12", "po_reference": "Jane Buyer",
         "total_amount": "20.00",
         "custom_fields": {"erpCode": "OR2000000001", "custPoCode": "CUST-77"},
         "updated_at": "2026-09-29T09:30:00"},
    ],
    "sage.po_header": [
        {"_sage_pk": "1", "ponumber": "PO-1001", "rqnnumber": "RQ-1",
         "doctotal": "35.1", "audtdate": "20260927"},
        {"_sage_pk": "2", "ponumber": "PO-2002", "rqnnumber": None,
         "doctotal": "10.0", "audtdate": "20260928"},
        {"_sage_pk": "3", "ponumber": "PO-3003", "rqnnumber": None,
         "doctotal": "1.0", "audtdate": "20261231"},
    ],
    "public.customer": [
        {"id": 7, "code": "C7", "name": "Customer Seven", "status": "Active"},
        {"id": 8, "code": "C8", "name": "Customer Eight", "status": "Active"},
    ],
    "silver.shipments": [
        {"id": 5, "shipment_code": "S-5A", "status": "Shipped"},
        {"id": 5, "shipment_code": "S-5B", "status": "Shipped"},
        {"id": 6, "shipment_code": "S-6", "status": "Open"},
    ],
}

ALL = {"public.*": ["staff:operations"], "sage.*": ["staff:operations", "staff:finance"],
       "silver.*": ["staff:operations"]}
OPS = Actor("plx", frozenset({"staff:operations"}))
OUTSIDER = Actor("plx", frozenset({"staff:marketing"}))


def _instant(text):
    """ISO parse that, like Postgres, accepts 1-9 fraction digits (3.10 does not)."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", text)
    stamp = match[1] + "." + (match[2] or "0")[:6].ljust(6, "0")
    zone = match[3]
    return datetime.fromisoformat(stamp + ("+00:00" if zone == "Z" else zone or ""))


class FakeWorkbench:
    """Emulates Portal's Workbench API routes and their since semantics."""

    def __init__(self):
        self.catalog = copy.deepcopy(CATALOG)
        self.tables = copy.deepcopy(TABLES)
        self.paths = []
        self.since_log = []
        self.healthy = True
        self.fail_row_requests = 0
        self.generated_at = "2026-09-29T12:00:00Z"

    def __call__(self, url, headers, *, max_bytes):
        assert headers == {"Authorization": "Bearer " + KEY}
        parts = urlsplit(url)
        self.paths.append(parts.path)
        if parts.path == "/v1/health":
            return {"ok": self.healthy, "recovery": True, "replayLagSeconds": 1,
                    "unavailable": [], "missingColumns": [],
                    "generatedAt": self.generated_at}
        if parts.path == "/v1/relations":
            return {"relations": [{"name": name, "schema": name.split(".")[0],
                                   "relation": name.split(".")[1], "unavailable": None,
                                   **copy.deepcopy(entry)}
                                  for name, entry in self.catalog.items()]}
        match = re.fullmatch(r"/v1/relations/([a-z0-9_]+\.[a-z0-9_]+)/rows", parts.path)
        assert match, parts.path
        if self.fail_row_requests:
            self.fail_row_requests -= 1
            raise RuntimeError("transient")
        return self._rows(match[1], parse_qs(parts.query))

    def _rows(self, name, query):
        entry = self.catalog[name]
        key = entry["key"]
        requested = query["columns"][0].split(",")
        assert set(requested) <= set(entry["columns"]), requested
        selected = [c for c in entry["columns"] if c in set(requested) | set(key)]
        rows = list(self.tables[name])
        since = query.get("since", [None])[0]
        if since is not None:
            assert entry["since"] and SINCE_FORMAT.fullmatch(since), since
            self.since_log.append((name, since))
            rows = [row for row in rows if self._since_ok(entry["since"], row, since)]
        rows.sort(key=lambda row: tuple(row[c] for c in key))
        cursor = query.get("cursor", [None])[0]
        if cursor is not None:
            after = tuple(json.loads(cursor))
            rows = [row for row in rows if tuple(row[c] for c in key) > after]
        limit = int(query["limit"][0])
        page = rows[:limit]
        more = len(rows) > limit
        if more:
            following = tuple(rows[limit][c] for c in key)
            while page and tuple(page[-1][c] for c in key) == following:
                page.pop()
            assert page, "key run longer than page"
        return {"relation": name, "columns": selected,
                "rows": [{c: row.get(c) for c in selected} for row in page],
                "nextCursor": json.dumps([page[-1][c] for c in key]) if more else None}

    @staticmethod
    def _since_ok(since, row, value):
        cell = row.get(since["column"])
        if cell is None:
            return False
        boundary = _instant(value).astimezone(timezone.utc)
        if since["mode"] == "timestamptz":
            return _instant(cell) >= boundary
        if since["mode"] == "timestamp-utc":
            return _instant(cell) >= boundary.replace(tzinfo=None)
        return cell >= boundary.strftime("%Y%m%d")

    def row_requests(self, name=None):
        return [p for p in self.paths if p.endswith("/rows") and
                (name is None or p == f"/v1/relations/{name}/rows")]

    def update(self, name, key_value, **changes):
        key = self.catalog[name]["key"][0]
        for row in self.tables[name]:
            if row[key] == key_value:
                row.update(changes)
                return
        raise KeyError(key_value)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class MirrorTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fake = FakeWorkbench()
        self.clock = Clock()
        self.path = Path(self.directory.name) / "mirror" / "mirror.sqlite"

    def mirror(self, audience=None, **options):
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        mirror = SourceMirror(self.path, client, ALL if audience is None else audience,
                              clock=self.clock, retry_delay_seconds=0, **options)
        self.addCleanup(mirror.close)
        return mirror

    def by_id(self, result):
        return {item["key"][next(iter(item["key"]))]: item for item in result["rows"]}


class FillAndRefreshTests(MirrorTestCase):
    def test_lazy_fill_pages_once_then_serves_from_mirror(self):
        mirror = self.mirror(page_size=2)
        self.assertEqual(self.fake.row_requests(), [])
        result = mirror.rows(OPS, "public.assembly_order")
        self.assertEqual(result["freshness"]["refreshed_now"], "lazy_fill")
        self.assertEqual(len(self.fake.row_requests("public.assembly_order")), 2)
        self.assertEqual(result["total_matched"], 4)
        self.assertEqual([item["key"]["id"] for item in result["rows"]],
                         [10718, 10719, 10720, 10721])
        first = result["rows"][0]
        self.assertRegex(mirror.generation, r"^[0-9a-f]{16}$")
        self.assertEqual(first["citation"],
                         "api://public.assembly_order/%7B%22id%22%3A10718%7D@"
                         f"{mirror.generation}-1")
        freshness = result["freshness"]
        self.assertFalse(freshness["atomic_upstream_snapshot"])
        self.assertFalse(freshness["deletions_proven"])
        self.assertEqual(freshness["refresh_strategy"], "incremental_since")
        self.assertEqual(freshness["source_watermark"], "2026-09-29T11:00:00.500000Z")
        self.assertEqual(freshness["source_watermark_raw"], "2026-09-29T11:00:00.5")
        self.assertEqual(freshness["health_before"]["generated_at"], "2026-09-29T12:00:00Z")
        # The row without a since value is counted as an incremental blind spot.
        self.assertIn("1 row(s) had no usable value", " ".join(freshness["caveats"]))
        again = mirror.rows(OPS, "public.assembly_order", columns=["nameamo_c"])
        self.assertEqual(again["freshness"]["refreshed_now"], "none")
        self.assertEqual(len(self.fake.row_requests()), 2)
        self.assertEqual(again["rows"][0]["row"], {"nameamo_c": "AO-26005B"})

    def test_incremental_timestamp_utc_overlap_catches_boundary_rows_and_upserts(self):
        mirror = self.mirror()
        before = self.by_id(mirror.rows(OPS, "public.assembly_order"))
        # A late commit stamped before the watermark but inside the overlap,
        # an unchanged row inside the window, a new row and an old-stamped change.
        self.fake.update("public.assembly_order", 10718, lotno="L-2",
                         audittimestampmodified="2026-09-29T10:50:00")
        self.fake.tables["public.assembly_order"].append(
            {"id": 10722, "nameamo_c": "AO-26009", "lotno": None, "qtytotal": "1",
             "audittimestampmodified": "2026-09-29T11:30:00"})
        self.fake.update("public.assembly_order", 10721, qtytotal="99",
                         audittimestampmodified="2026-09-29T09:05:00")
        self.clock.advance(1000)
        after = mirror.rows(OPS, "public.assembly_order")
        freshness = after["freshness"]
        self.assertEqual(freshness["refreshed_now"], "incremental")
        self.assertEqual(self.fake.since_log[-1],
                         ("public.assembly_order", "2026-09-29T10:45:00.500000Z"))
        rows = self.by_id(after)
        self.assertEqual(rows[10718]["row"]["lotno"], "L-2")
        self.assertEqual(rows[10718]["capture_id"], 2)
        self.assertEqual(rows[10719]["capture_id"], before[10719]["capture_id"])
        self.assertEqual(rows[10719]["citation"], before[10719]["citation"])
        self.assertIn(10722, rows)
        self.assertEqual(rows[10720]["row"]["qtytotal"], "12")
        # Stamped before the overlap window: invisible to since, as documented.
        self.assertEqual(rows[10721]["row"]["qtytotal"], "3")
        self.assertEqual(freshness["source_watermark"], "2026-09-29T11:30:00.000000Z")
        self.clock.advance(1000)
        mirror.refresh(OPS, "public.assembly_order", full=True)
        rows = self.by_id(mirror.rows(OPS, "public.assembly_order"))
        self.assertEqual(rows[10721]["row"]["qtytotal"], "99")

    def test_incremental_timestamptz_mode(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.app_purchase_order")
        self.fake.update("public.app_purchase_order", "po-b", status="CLOSED",
                         updated_at="2026-09-29T11:59:00.123456Z")
        self.clock.advance(901)
        result = mirror.rows(OPS, "public.app_purchase_order")
        self.assertEqual(result["freshness"]["refreshed_now"], "incremental")
        self.assertEqual(self.fake.since_log[-1],
                         ("public.app_purchase_order", "2026-09-29T10:45:00.000000Z"))
        rows = self.by_id(result)
        self.assertEqual(rows["po-b"]["row"]["status"], "CLOSED")
        self.assertEqual(result["freshness"]["source_watermark"],
                         "2026-09-29T11:59:00.123456Z")

    def test_incremental_yyyymmdd_text_mode_is_day_granular_and_caps_future_dates(self):
        mirror = self.mirror()
        first = mirror.rows(OPS, "sage.po_header")
        freshness = first["freshness"]
        # 20261231 is later than the replica's generatedAt, so it is excluded.
        self.assertEqual(freshness["source_watermark_raw"], "20260928")
        self.assertIn("1 row(s) in the latest capture had a since value later",
                      " ".join(freshness["caveats"]))
        self.fake.update("sage.po_header", "1", doctotal="36.1", audtdate="20260929")
        self.clock.advance(901)
        result = mirror.rows(OPS, "sage.po_header")
        self.assertEqual(result["freshness"]["refreshed_now"], "incremental")
        self.assertEqual(self.fake.since_log[-1],
                         ("sage.po_header", "2026-09-27T00:00:00.000000Z"))
        rows = self.by_id(result)
        self.assertEqual(rows["1"]["row"]["doctotal"], "36.1")
        self.assertEqual(rows["2"]["capture_id"], 1)
        self.assertEqual(result["freshness"]["source_watermark_raw"], "20260929")

    def test_relation_without_since_is_refreshed_by_full_traversal(self):
        mirror = self.mirror()
        first = mirror.rows(OPS, "public.customer")
        self.assertEqual(first["freshness"]["refresh_strategy"], "full_traversal_only")
        self.assertIn("No since column", " ".join(first["freshness"]["caveats"]))
        citation = self.by_id(first)[8]["citation"]
        self.fake.tables["public.customer"] = [
            row for row in self.fake.tables["public.customer"] if row["id"] != 8]
        self.fake.tables["public.customer"].append(
            {"id": 9, "code": "C9", "name": "Customer Nine", "status": "Active"})
        self.clock.advance(901)
        result = mirror.rows(OPS, "public.customer")
        self.assertEqual(result["freshness"]["refreshed_now"], "full_traversal")
        self.assertEqual(result["freshness"]["refresh_reason"], "relation has no since column")
        self.assertEqual(self.fake.since_log, [])
        self.assertEqual(sorted(self.by_id(result)), [7, 9])
        self.assertEqual(result["freshness"]["rows_absent_from_latest_full_traversal"], 1)
        resolved = mirror.resolve_citation(citation, OPS)
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["upstream_presence"], "absent_from_latest_full_traversal")
        self.assertFalse(resolved["deletions_proven"])

    def test_non_unique_view_key_is_kept_as_a_run(self):
        mirror = self.mirror()
        result = mirror.rows(OPS, "silver.shipments")
        self.assertFalse(result["freshness"]["key_unique"])
        self.assertEqual(result["total_matched"], 3)
        runs = [item for item in result["rows"] if item["key"] == {"id": 5}]
        self.assertEqual([item["key_run"]["length"] for item in runs], [2, 2])
        self.assertEqual(runs[0]["citation"], runs[1]["citation"])
        resolved = mirror.resolve_citation(runs[0]["citation"], OPS)
        self.assertEqual(sorted(row["shipment_code"] for row in resolved["rows"]),
                         ["S-5A", "S-5B"])
        self.assertFalse(resolved["key_unique"])

    def test_catalog_change_forces_full_traversal_and_supersedes_citations(self):
        mirror = self.mirror()
        citation = mirror.rows(OPS, "public.assembly_order")["rows"][0]["citation"]
        self.fake.catalog["public.assembly_order"]["columns"].append("finalqty")
        for row in self.fake.tables["public.assembly_order"]:
            row["finalqty"] = None
        self.clock.advance(901)
        result = mirror.rows(OPS, "public.assembly_order")
        self.assertEqual(result["freshness"]["refreshed_now"], "full_traversal")
        self.assertEqual(result["freshness"]["refresh_reason"], "catalog entry changed")
        self.assertIn("finalqty", result["columns"])
        old = mirror.resolve_citation(citation, OPS)
        self.assertEqual(old["status"], "superseded")
        self.assertNotIn("finalqty", old["row"])

    def test_refresh_failure_reports_stale_rows_and_first_fill_failure_raises(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.assembly_order")
        self.fake.healthy = False
        self.clock.advance(5000)
        result = mirror.rows(OPS, "public.assembly_order")
        self.assertTrue(result["freshness"]["stale"])
        self.assertEqual(result["freshness"]["refreshed_now"], "none")
        self.assertTrue(result["freshness"]["refresh_error"].startswith("source_unavailable"))
        self.assertEqual(result["total_matched"], 4)
        self.assertIn("older than max_staleness", " ".join(result["freshness"]["caveats"]))
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OPS, "public.customer")
        self.assertEqual(caught.exception.code, "source_unavailable")
        self.fake.healthy = True
        recovered = mirror.rows(OPS, "public.assembly_order")
        self.assertFalse(recovered["freshness"]["stale"])
        self.assertIsNone(recovered["freshness"]["last_refresh_failure"])

    def test_rows_offset_pages_past_the_row_limit_in_order(self):
        mirror = self.mirror()
        first = mirror.rows(OPS, "public.assembly_order", limit=3)
        self.assertEqual((first["offset"], first["returned"], first["next_offset"],
                          first["truncated"]), (0, 3, 3, True))
        rest = mirror.rows(OPS, "public.assembly_order", limit=3, offset=3)
        self.assertEqual((rest["offset"], rest["returned"], rest["next_offset"],
                          rest["total_matched"]), (3, 1, None, 4))
        self.assertEqual([item["key"]["id"] for item in first["rows"] + rest["rows"]],
                         [10718, 10719, 10720, 10721])
        self.assertEqual(rest["freshness"]["latest_capture_id"],
                         first["freshness"]["latest_capture_id"])
        whole = mirror.rows(OPS, "public.assembly_order")
        self.assertEqual((whole["next_offset"], whole["truncated"]), (None, False))
        self.assertEqual(mirror.rows(OPS, "public.assembly_order", offset=10)["rows"], [])
        for bad in (-1, 1_000_001, True, "1"):
            with self.assertRaises(MirrorError) as caught:
                mirror.rows(OPS, "public.assembly_order", offset=bad)
            self.assertEqual(caught.exception.code, "invalid_offset")
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OUTSIDER, "public.assembly_order", offset=1)
        self.assertEqual(caught.exception.code, "not_visible")

    def test_transient_read_failure_is_retried_once(self):
        mirror = self.mirror()
        self.fake.fail_row_requests = 1
        result = mirror.rows(OPS, "public.customer")
        self.assertEqual(result["total_matched"], 2)
        self.fake.fail_row_requests = 2
        self.clock.advance(901)
        stale = mirror.rows(OPS, "public.customer")
        self.assertEqual(self.fake.fail_row_requests, 0)
        self.assertEqual(stale["freshness"]["refresh_error"],
                         "source_unavailable: workbench API GET failed: RuntimeError")

    def test_capture_ids_are_durable_and_monotonic_across_reopen(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.assembly_order")
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        mirror.close()
        reopened = self.mirror()
        self.assertTrue(reopened.resolve_citation(citation, OPS)["ok"])
        ids = [reopened.refresh(OPS, "public.customer")["latest_capture_id"]
               for _ in range(2)]
        self.assertEqual(ids, [3, 4])
        self.assertEqual(parse_citation(citation)[3], 2)

    def test_projection_bounds_mirrored_columns(self):
        mirror = self.mirror(projections={"sage.po_header": ["ponumber"]})
        result = mirror.rows(OPS, "sage.po_header")
        self.assertEqual(result["columns"], ["_sage_pk", "ponumber", "audtdate"])
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OPS, "sage.po_header", columns=["doctotal"])
        self.assertEqual(caught.exception.code, "unknown_column")


class EntitlementTests(MirrorTestCase):
    def test_outside_principal_gets_nothing_and_triggers_no_reads(self):
        mirror = self.mirror()
        citation = mirror.rows(OPS, "public.assembly_order")["rows"][0]["citation"]
        requests = len(self.fake.row_requests())
        catalog = mirror.catalog(OUTSIDER)
        self.assertEqual(catalog["relation_count"], 0)
        self.assertEqual(catalog["relations"], [])
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OUTSIDER, "public.assembly_order")
        self.assertEqual(caught.exception.code, "not_visible")
        with self.assertRaises(MirrorError):
            mirror.rows(OUTSIDER, "public.customer")
        resolved = mirror.resolve(OUTSIDER, "AO-26005B", fill_missing=True)
        self.assertEqual(resolved["candidate_count"], 0)
        self.assertEqual(resolved["coverage"], {"searched": [], "not_mirrored": [],
                                                "not_in_upstream_catalog": [], "failed": []})
        self.assertEqual(mirror.resolve_citation(citation, OUTSIDER)["error"], "not_visible")
        missing = make_citation("public.assembly_order", {"id": 1}, 1)
        self.assertEqual(mirror.resolve_citation(missing, OUTSIDER)["error"], "not_visible")
        with self.assertRaises(MirrorError):
            mirror.refresh(OUTSIDER, "public.assembly_order")
        self.assertEqual(len(self.fake.row_requests()), requests)

    def test_default_deny_most_specific_pattern_and_explicit_empty_deny(self):
        audience = {"public.assembly_order": ["staff:operations"],
                    "sage.*": ["staff:finance"], "sage.po_header": [],
                    "public.*": ["staff:all"], "public.app_*": ["staff:portal"]}
        mirror = self.mirror(audience)
        finance = Actor("plx", frozenset({"staff:finance"}))
        portal = Actor("plx", frozenset({"staff:portal"}))
        everyone = Actor("plx", frozenset({"staff:all"}))
        self.assertEqual([r["name"] for r in mirror.catalog(OPS)["relations"]],
                         ["public.assembly_order"])
        self.assertEqual([r["name"] for r in mirror.catalog(finance)["relations"]], [])
        self.assertEqual([r["name"] for r in mirror.catalog(portal)["relations"]],
                         ["public.app_purchase_order", "public.app_sales_order"])
        self.assertEqual([r["name"] for r in mirror.catalog(everyone)["relations"]],
                         ["public.customer"])
        with self.assertRaises(MirrorError):
            mirror.rows(finance, "sage.po_header")

    def test_tenant_and_classification_are_enforced(self):
        audience = AudienceMap({"sage.*": ["staff:finance"]}, tenant="plx",
                               classifications={"sage.*": "confidential"})
        mirror = self.mirror(audience)
        internal = Actor("plx", frozenset({"staff:finance"}))
        cleared = Actor("plx", frozenset({"staff:finance"}), "confidential")
        other_tenant = Actor("other", frozenset({"staff:finance"}), "restricted")
        self.assertEqual(mirror.catalog(internal)["relation_count"], 0)
        self.assertEqual(mirror.catalog(other_tenant)["relation_count"], 0)
        self.assertEqual([r["name"] for r in mirror.catalog(cleared)["relations"]],
                         ["sage.po_header"])

    def test_audience_configuration_is_validated(self):
        for bad in ({"*": ["a"]}, {"*.x": ["a"]}, {"public": ["a"]},
                    {"public.x": ["*"]}, {"public.x": "staff:a"}, {"public.x": [" a"]}):
            with self.assertRaises(ValueError):
                AudienceMap(bad, tenant="plx")
        with self.assertRaises(ValueError):
            AudienceMap.from_json({"schema": "plx-source-mirror-audience/v1",
                                   "audiences": {}, "extra": 1, "tenant": "plx"})
        example = Path(__file__).resolve().parents[2] / "examples" / \
            "source_mirror_audience.example.json"
        shipped = AudienceMap.from_file(example)
        finance = Actor("plx", frozenset({"staff:finance"}), "confidential")
        self.assertTrue(shipped.allows(finance, "sage.po_header"))
        self.assertFalse(shipped.allows(finance, "sage.ar_customer"))
        self.assertFalse(shipped.allows(Actor("plx", frozenset({"staff:finance"})),
                                        "sage.po_header"))
        self.assertFalse(shipped.allows(OPS, "public.customer"))

    def test_file_revocation_hides_rows_and_citations_immediately(self):
        path = Path(self.directory.name) / "audience.json"

        def write(audiences):
            path.write_text(json.dumps({"schema": "plx-source-mirror-audience/v1",
                                        "tenant": "plx", "audiences": audiences}))

        write({"public.assembly_order": ["staff:operations"]})
        mirror = self.mirror(path)
        citation = mirror.rows(OPS, "public.assembly_order")["rows"][0]["citation"]
        self.assertTrue(mirror.resolve_citation(citation, OPS)["ok"])
        self.assertEqual(mirror.resolve(OPS, "AO-26005B")["candidate_count"], 1)
        write({"public.assembly_order": []})
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OPS, "public.assembly_order")
        self.assertEqual(caught.exception.code, "not_visible")
        self.assertEqual(mirror.resolve_citation(citation, OPS)["error"], "not_visible")
        self.assertEqual(mirror.resolve(OPS, "AO-26005B")["candidate_count"], 0)
        self.assertEqual(mirror.catalog(OPS)["relation_count"], 0)
        path.write_text("{not json")
        self.assertEqual(mirror.resolve_citation(citation, OPS)["error"], "not_visible")
        self.assertIn("audience file invalid", mirror.catalog(OPS)["audience_error"])
        write({"public.assembly_order": ["staff:operations"]})
        requests = len(self.fake.row_requests())
        self.assertTrue(mirror.resolve_citation(citation, OPS)["ok"])
        self.assertEqual(len(self.fake.row_requests()), requests)
        mirror.set_audience_map({})
        self.assertEqual(mirror.resolve_citation(citation, OPS)["error"], "not_visible")


class CitationTests(MirrorTestCase):
    def test_citation_change_detection_and_retention(self):
        mirror = self.mirror()
        original = self.by_id(mirror.rows(OPS, "public.app_purchase_order"))["po-a"]
        current = mirror.resolve_citation(original["citation"], OPS)
        self.assertEqual(current["status"], "current")
        self.assertFalse(current["changed_since"])
        self.assertEqual(current["upstream_presence"], "observed_in_latest_full_traversal")
        self.assertEqual(current["capture"]["mode"], "lazy_fill")
        self.assertFalse(current["capture"]["atomic_upstream_snapshot"])
        self.fake.update("public.app_purchase_order", "po-a", status="CLOSED",
                         updated_at="2026-09-29T11:58:00Z")
        self.clock.advance(901)
        changed = self.by_id(mirror.rows(OPS, "public.app_purchase_order"))["po-a"]
        self.assertNotEqual(changed["citation"], original["citation"])
        old = mirror.resolve_citation(original["citation"], OPS)
        self.assertEqual(old["status"], "superseded")
        self.assertTrue(old["changed_since"])
        self.assertEqual(old["row"]["status"], "OPEN")
        self.assertEqual(old["current_citation"], changed["citation"])
        self.assertFalse(old["content_equal_to_current"])
        # Reverting to the old content creates a new capture, not an old revision.
        self.fake.update("public.app_purchase_order", "po-a", status="OPEN",
                         updated_at="2026-09-29T11:00:00.000000Z")
        mirror.refresh(OPS, "public.app_purchase_order", full=True)
        old = mirror.resolve_citation(original["citation"], OPS)
        self.assertEqual(old["status"], "superseded")
        self.assertTrue(old["content_equal_to_current"])
        self.assertNotEqual(old["current_citation"], original["citation"])

    def test_invalid_unknown_and_missing_citations(self):
        mirror = self.mirror()
        item = mirror.rows(OPS, "public.customer")["rows"][0]
        for handle in ("", "api://public.customer/7@1", "http://x",
                       "api://public.customer/%7B%7D@1",
                       "api://public.customer/%7B%22id%22%3A7%7D@0", None):
            self.assertEqual(mirror.resolve_citation(handle, OPS)["error"], "invalid_handle")
        self.assertEqual(mirror.resolve_citation(
            make_citation("public.customer", {"id": 99}, 1), OPS)["error"], "not_found")
        self.assertEqual(mirror.resolve_citation(
            make_citation("public.customer", {"id": 7}, 999), OPS)["error"], "capture_unknown")
        self.assertEqual(mirror.resolve_citation(
            make_citation("public.customer", {"id": "7"}, 1), OPS)["error"], "not_found")
        self.assertTrue(mirror.resolve_citation(item["citation"], OPS)["ok"])

    def test_citation_round_trip_escapes_key_text(self):
        handle = make_citation("sage.po_header", {"_sage_pk": "a/b@c d"}, 12)
        self.assertNotIn(" ", handle)
        relation, key_json, key, capture = parse_citation(handle)
        self.assertEqual((relation, key, capture),
                         ("sage.po_header", {"_sage_pk": "a/b@c d"}, 12))
        self.assertEqual(key_json, '{"_sage_pk":"a/b@c d"}')
        self.assertIsNone(citation_generation(handle))  # legacy form still parses
        current = make_citation("sage.po_header", {"_sage_pk": "a/b@c d"}, 12,
                                "0123456789abcdef")
        self.assertTrue(current.endswith("@0123456789abcdef-12"), current)
        self.assertEqual(parse_citation(current), parse_citation(handle))
        self.assertEqual(citation_generation(current), "0123456789abcdef")
        with self.assertRaises(ValueError):
            make_citation("sage.po_header", {"_sage_pk": "x"}, 1, "not-hex")

    def remove_mirror_files(self):
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                candidate.unlink()

    def test_rebuilt_mirror_never_resolves_old_citations_to_new_content(self):
        # CR-003 regression: capture ids are AUTOINCREMENT per file, so a rebuilt
        # mirror reuses capture 1 for different content under the same key.
        mirror = self.mirror()
        old = self.by_id(mirror.rows(OPS, "public.customer"))[7]
        old_generation = mirror.generation
        mirror.close()
        self.remove_mirror_files()
        self.fake.update("public.customer", 7, status="Suspended")
        rebuilt = self.mirror()
        self.assertNotEqual(rebuilt.generation, old_generation)
        new = self.by_id(rebuilt.rows(OPS, "public.customer"))[7]
        self.assertEqual((old["capture_id"], new["capture_id"]), (1, 1))  # ids are reused
        self.assertNotEqual(new["citation"], old["citation"])  # handles are not
        answer = rebuilt.resolve_citation(old["citation"], OPS)
        self.assertEqual((answer["ok"], answer["error"], answer["reason"]),
                         (False, "capture_unknown", "mirror_generation_mismatch"))
        self.assertNotIn("row", answer)
        self.assertNotIn("Suspended", json.dumps(answer))
        self.assertEqual(rebuilt.resolve_citation(new["citation"], OPS)["row"]["status"],
                         "Suspended")
        # A key the new mirror has never seen is still simply not found.
        self.assertEqual(rebuilt.resolve_citation(
            make_citation("public.customer", {"id": 99}, 1, old_generation), OPS)["error"],
            "not_found")
        # Within one generation an unknown capture is capture_unknown with no reason.
        same = rebuilt.resolve_citation(
            make_citation("public.customer", {"id": 7}, 999, rebuilt.generation), OPS)
        self.assertEqual(same["error"], "capture_unknown")
        self.assertNotIn("reason", same)

    def test_legacy_handles_resolve_only_in_the_file_that_held_them(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.customer")
        mirror.close()
        # Simulate a mirror file filled before generations existed.
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM mirror_meta WHERE name = 'generation'")
            db.commit()
        upgraded = self.mirror()
        legacy = make_citation("public.customer", {"id": 7}, 1)
        answer = upgraded.resolve_citation(legacy, OPS)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["row"]["code"], "C7")
        self.assertEqual(answer["current_citation"],
                         make_citation("public.customer", {"id": 7}, 1, upgraded.generation))
        # Captures made after the upgrade are only reachable with a generation.
        self.clock.advance(901)
        self.fake.update("public.customer", 7, status="Changed")
        upgraded.refresh(OPS, "public.customer")
        self.assertEqual(upgraded.resolve_citation(
            make_citation("public.customer", {"id": 7}, 2), OPS)["reason"],
            "mirror_generation_mismatch")
        upgraded.close()
        self.remove_mirror_files()
        fresh = self.mirror()
        fresh.rows(OPS, "public.customer")
        self.assertEqual(fresh.resolve_citation(legacy, OPS)["reason"],
                         "mirror_generation_mismatch")


class BindingTests(MirrorTestCase):
    """CR-005: a mirror file is pinned to the tenant and upstream that filled it."""

    def test_reopening_for_another_tenant_or_upstream_is_refused(self):
        mirror = self.mirror()
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        mirror.close()
        other_tenant = SourceMirror(self.path, None, ALL, tenant="acme", clock=self.clock)
        self.addCleanup(other_tenant.close)
        acme = Actor("acme", frozenset({"staff:operations"}))
        for call in (lambda: other_tenant.rows(acme, "public.customer"),
                     lambda: other_tenant.catalog(acme),
                     lambda: other_tenant.resolve(acme, "C7"),
                     lambda: other_tenant.refresh(acme, "public.customer")):
            with self.assertRaises(MirrorError) as caught:
                call()
            self.assertEqual(caught.exception.code, "binding_mismatch")
        self.assertEqual(other_tenant.resolve_citation(citation, acme)["error"],
                         "binding_mismatch")
        other_tenant.close()
        client = WorkbenchReadApiClient("https://other.example.test", KEY, fetch=self.fake)
        other_source = SourceMirror(self.path, client, ALL, clock=self.clock,
                                    retry_delay_seconds=0)
        self.addCleanup(other_source.close)
        with self.assertRaises(MirrorError) as caught:
            other_source.rows(OPS, "public.customer")
        self.assertEqual(caught.exception.code, "binding_mismatch")
        self.assertNotIn("example.test", str(caught.exception))
        other_source.close()
        # Same tenant offline, or the same upstream again, still serves.
        offline = SourceMirror(self.path, None, ALL, clock=self.clock)
        self.addCleanup(offline.close)
        self.assertTrue(offline.resolve_citation(citation, OPS)["ok"])
        offline.close()
        self.assertEqual(self.mirror().rows(OPS, "public.customer")["returned"], 2)


    def test_binding_follows_the_audience_in_force_not_the_constructor(self):
        # Editing the audience file's tenant plx -> acme used to
        # serve plx rows to acme actors because only the constructor tenant was
        # compared.
        path = Path(self.directory.name) / "audience.json"

        def write(tenant):
            path.write_text(json.dumps({"schema": "plx-source-mirror-audience/v1",
                                        "tenant": tenant, "audiences": ALL}))

        write("plx")
        mirror = self.mirror(path)
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        write("acme")
        acme = Actor("acme", frozenset({"staff:operations"}))
        for call in (lambda: mirror.rows(acme, "public.customer"),
                     lambda: mirror.catalog(acme),
                     lambda: mirror.resolve(acme, "C7"),
                     lambda: mirror.refresh(acme, "public.customer")):
            with self.assertRaises(MirrorError) as caught:
                call()
            self.assertEqual(caught.exception.code, "binding_mismatch")
        self.assertEqual(mirror.resolve_citation(citation, acme)["error"],
                         "binding_mismatch")
        # The same holds for an in-memory audience swapped to another tenant.
        write("plx")
        mirror.set_audience_map(ALL, tenant="acme")
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(acme, "public.customer")
        self.assertEqual(caught.exception.code, "binding_mismatch")
        mirror.set_audience_map(ALL, tenant="plx")
        self.assertEqual(mirror.rows(OPS, "public.customer")["returned"], 2)

    def test_file_filled_before_pins_is_pinned_when_reopened(self):
        mirror = self.mirror()
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        mirror.close()
        with contextlib.closing(sqlite3.connect(str(self.path))) as db:
            db.execute("DELETE FROM mirror_meta WHERE name IN ('tenant', 'source_digest')")
            db.commit()
        # Reopened for its own tenant, offline: pinned at open, before any capture.
        SourceMirror(self.path, None, ALL, clock=self.clock).close()
        with contextlib.closing(sqlite3.connect(str(self.path))) as db:
            pins = dict(db.execute("SELECT name, value FROM mirror_meta WHERE name "
                                   "IN ('tenant', 'source_digest')").fetchall())
        self.assertEqual(pins, {"tenant": "plx"})
        other_tenant = SourceMirror(self.path, None, ALL, tenant="acme", clock=self.clock)
        self.addCleanup(other_tenant.close)
        acme = Actor("acme", frozenset({"staff:operations"}))
        self.assertEqual(other_tenant.resolve_citation(citation, acme)["error"],
                         "binding_mismatch")
        other_tenant.close()
        # The upstream pin is added by the first reopen that knows the upstream.
        self.mirror().close()
        client = WorkbenchReadApiClient("https://other.example.test", KEY, fetch=self.fake)
        other_source = SourceMirror(self.path, client, ALL, clock=self.clock)
        self.addCleanup(other_source.close)
        self.assertEqual(other_source.resolve_citation(citation, OPS)["error"],
                         "binding_mismatch")

    def test_legacy_file_opened_with_an_unusable_audience_pins_the_host_tenant(self):
        mirror = self.mirror()
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        mirror.close()
        with contextlib.closing(sqlite3.connect(str(self.path))) as db:
            db.execute("DELETE FROM mirror_meta WHERE name IN ('tenant', 'source_digest')")
            db.commit()
        path = Path(self.directory.name) / "audience.json"
        path.write_text("{not json")
        reopened = SourceMirror(self.path, None, path, tenant="plx", clock=self.clock)
        self.addCleanup(reopened.close)
        path.write_text(json.dumps({"schema": "plx-source-mirror-audience/v1",
                                    "tenant": "acme", "audiences": ALL}))
        acme = Actor("acme", frozenset({"staff:operations"}))
        with self.assertRaises(MirrorError) as caught:
            reopened.rows(acme, "public.customer")
        self.assertEqual(caught.exception.code, "binding_mismatch")
        self.assertEqual(reopened.resolve_citation(citation, acme)["error"], "binding_mismatch")

    def test_allow_and_binding_checks_judge_one_audience_read(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.customer")
        acme = Actor("acme", frozenset({"staff:operations"}))
        plx_map, acme_map = AudienceMap(ALL, tenant="plx"), AudienceMap(ALL, tenant="acme")
        # An edit landing between two reads of the audience within one call
        # must not pair plx's binding check with acme's allow check.
        with patch.object(mirror, "_audience", side_effect=[plx_map] + [acme_map] * 10):
            self.assertEqual(mirror.catalog(acme)["relations"], [])
        with patch.object(mirror, "_audience", side_effect=[plx_map] + [acme_map] * 10):
            self.assertEqual(mirror.resolve(acme, "C7")["candidate_count"], 0)

    def mirror_with_fetch_hook(self, hook, audience=None):
        def fetch(*args, **kwargs):
            hook()
            return self.fake(*args, **kwargs)

        client = WorkbenchReadApiClient(BASE, KEY, fetch=fetch)
        mirror = SourceMirror(self.path, client, ALL if audience is None else audience,
                              clock=self.clock, retry_delay_seconds=0)
        self.addCleanup(mirror.close)
        return mirror

    def test_binding_mismatch_during_a_capture_is_not_served_as_stale_rows(self):
        armed = []

        def repin():  # another process pins the file to acme mid-read
            if armed:
                with contextlib.closing(sqlite3.connect(str(self.path))) as db:
                    db.execute("UPDATE mirror_meta SET value='acme' WHERE name='tenant'")
                    db.commit()

        mirror = self.mirror_with_fetch_hook(repin)
        mirror.rows(OPS, "public.customer")
        armed.append(True)
        with self.assertRaises(MirrorError) as caught:
            mirror.refresh(OPS, "public.customer")
        self.assertEqual(caught.exception.code, "binding_mismatch")
        armed.clear()
        with contextlib.closing(sqlite3.connect(str(self.path))) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM capture").fetchone()[0], 1)
            db.execute("UPDATE mirror_meta SET value='plx' WHERE name='tenant'")
            db.commit()
        # A citation refreshed through the same race is refused as a typed
        # result, as callers such as customer-PO coverage expect a dict.
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        armed.append(True)
        result = mirror.resolve_citation(citation, OPS, max_staleness=0)
        self.assertEqual((result["ok"], result["error"]), (False, "binding_mismatch"))
        self.assertNotIn("row", result)

    def test_capturing_operations_read_the_audience_once(self):
        path = Path(self.directory.name) / "audience.json"

        def write(tenant):
            path.write_text(json.dumps({"schema": "plx-source-mirror-audience/v1",
                                        "tenant": tenant, "audiences": ALL}))

        # First fill for acme while the file is edited to plx during the read:
        # the capture pins the tenant the call checked, never the edited one.
        write("acme")
        edits = []
        mirror = self.mirror_with_fetch_hook(lambda: edits and write(edits.pop()), path)
        acme = Actor("acme", frozenset({"staff:operations"}))
        edits.append("plx")
        self.assertEqual(mirror.rows(acme, "public.customer")["returned"], 2)
        with contextlib.closing(sqlite3.connect(str(self.path))) as db:
            self.assertEqual(db.execute("SELECT value FROM mirror_meta WHERE "
                                        "name='tenant'").fetchone()[0], "acme")
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OPS, "public.customer")
        self.assertEqual(caught.exception.code, "binding_mismatch")
        write("acme")
        reads = []
        original = mirror._audience

        def counted():
            reads.append(1)
            return original()

        with patch.object(mirror, "_audience", side_effect=counted):
            for call in (lambda: mirror.refresh(acme, "public.customer"),
                         lambda: mirror.rows(acme, "public.customer", max_staleness=0),
                         lambda: mirror.resolve(acme, "C7", max_staleness=0)):
                reads.clear()
                call()
                self.assertEqual(len(reads), 1)

    def test_empty_file_is_not_pinned_at_open(self):
        SourceMirror(self.path, None, ALL, tenant="acme", clock=self.clock).close()
        self.assertEqual(self.mirror().rows(OPS, "public.customer")["returned"], 2)

    def test_client_without_an_upstream_identity_is_refused(self):
        class Anonymous:
            def get_json(self, *args, **kwargs):
                raise AssertionError("must not be called")

        for client in (Anonymous(), type("Blank", (), {"base_url": "  "})()):
            with self.assertRaises(ValueError) as caught:
                SourceMirror(self.path, client, ALL, clock=self.clock)
            self.assertEqual(str(caught.exception), "source_mirror_client_base_url_required")

class QueryTests(MirrorTestCase):
    def rows(self, mirror, **options):
        return sorted(self.by_id(mirror.rows(OPS, "public.app_purchase_order", **options)))

    def test_where_operations(self):
        mirror = self.mirror()
        self.assertEqual(self.rows(mirror, where=[("po_number", "eq", "PO-1001")]), ["po-a"])
        self.assertEqual(self.rows(mirror, where=[["po_number", "prefix", "PO-"]]),
                         ["po-a", "po-b"])
        self.assertEqual(self.rows(mirror, where=[("po_number", "in", ["PX-7", "PO-1002"])]),
                         ["po-b", "po-c"])
        # Numeric strings compare as decimals, not text ("9.5" < "100").
        self.assertEqual(self.rows(mirror, where=[("total_cost", "gte", "100")]), ["po-b"])
        self.assertEqual(self.rows(mirror, where=[("total_cost", "lte", 35.1)]),
                         ["po-a", "po-c"])
        self.assertEqual(self.rows(mirror, where=[("shipped_at", "gte", "2026-09-21")]),
                         ["po-c"])
        self.assertEqual(self.rows(mirror, where=[("tracking_number", "is_null", True)]),
                         ["po-b", "po-c"])
        self.assertEqual(self.rows(mirror, where=[("tracking_number", "is_null", False)]),
                         ["po-a"])
        self.assertEqual(self.rows(mirror, where=[("status", "eq", "OPEN"),
                                                  ("total_cost", "gte", 10)]), ["po-a"])
        ao = mirror.rows(OPS, "public.assembly_order", where=[("id", "eq", "10718")])
        self.assertEqual([item["key"]["id"] for item in ao["rows"]], [10718])
        for where, code in (([("status", "like", "x")], "invalid_where"),
                            ([("status", "eq")], "invalid_where"),
                            ([("status", "in", [])], "invalid_where"),
                            ([("status", "eq", None)], "invalid_where"),
                            ([("nope", "eq", 1)], "unknown_column")):
            with self.assertRaises(MirrorError) as caught:
                mirror.rows(OPS, "public.app_purchase_order", where=where)
            self.assertEqual(caught.exception.code, code)

    def test_limit_bounds_order_and_truncation(self):
        mirror = self.mirror()
        for limit in (0, 501, True, "5"):
            with self.assertRaises(MirrorError) as caught:
                mirror.rows(OPS, "public.app_purchase_order", limit=limit)
            self.assertEqual(caught.exception.code, "invalid_limit")
        result = mirror.rows(OPS, "public.app_purchase_order", limit=2,
                             order=[("total_cost", "desc")], columns=["total_cost"])
        self.assertEqual(result["total_matched"], 3)
        self.assertEqual(result["returned"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual([item["row"]["total_cost"] for item in result["rows"]],
                         ["120.00", "35.099999999999994"])
        nulls = mirror.rows(OPS, "public.app_purchase_order", order=[("shipped_at", "desc")])
        self.assertEqual([item["key"]["id"] for item in nulls["rows"]], ["po-c", "po-a", "po-b"])
        self.assertEqual(len(mirror.rows(OPS, "public.app_purchase_order", limit=500)["rows"]), 3)
        with self.assertRaises(MirrorError):
            mirror.rows(OPS, "public.app_purchase_order", max_staleness=-1)

    def test_unknown_column_is_refused_before_lazy_fill(self):
        mirror = self.mirror()
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OPS, "public.assembly_order", columns=["secret"])
        self.assertEqual(caught.exception.code, "unknown_column")
        self.assertEqual(self.fake.row_requests(), [])


class ResolveTests(MirrorTestCase):
    def test_resolve_lists_every_exact_match_without_selecting(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.app_purchase_order")
        partial = mirror.resolve(OPS, "  po-1001 ")
        self.assertEqual([c["relation"] for c in partial["candidates"]],
                         ["public.app_purchase_order"])
        self.assertIn("sage.po_header", partial["coverage"]["not_mirrored"])
        self.assertIn("public.assembly_order", partial["coverage"]["not_mirrored"])
        result = mirror.resolve(OPS, "  po-1001 ", fill_missing=True)
        self.assertTrue(result["selection_required"])
        self.assertEqual(result["candidate_count"], 3)
        found = {(c["relation"], c["column"]): c for c in result["candidates"]}
        self.assertEqual(sorted(found), [("public.app_purchase_order", "po_number"),
                                         ("public.app_sales_order", "po_reference"),
                                         ("sage.po_header", "ponumber")])
        # A Portal-entered row (no import marker): Portal resolves po_reference as an
        # order identity (order-identity.ts resolveOrderByNumber), so it is a typed
        # external reference, not free text.
        typed = found[("public.app_sales_order", "po_reference")]
        self.assertEqual((typed["identifier_strength"], typed["strength_basis"]),
                         ("external_reference", "portal_order_entry"))
        self.assertNotIn("note", typed)
        legacy = mirror.resolve(OPS, "jane buyer")["candidates"]
        self.assertEqual([(c["identifier_strength"], c["strength_basis"]) for c in legacy],
                         [("free_text", "legacy_fm_migration_reference_text")])
        self.assertIn("not a PO identity", legacy[0]["note"])
        self.assertEqual(found[("sage.po_header", "ponumber")]["kind"],
                         "sage_purchase_order_number")
        for candidate in result["candidates"]:
            self.assertTrue(mirror.resolve_citation(candidate["citation"], OPS)["ok"])
            self.assertNotIn("winner", candidate)
        self.assertEqual(result["coverage"]["not_mirrored"], [])
        self.assertEqual(result["coverage"]["failed"], [])
        self.assertIn("public.app_receipt", result["coverage"]["not_in_upstream_catalog"])

    def test_resolve_counts_only_visible_relations(self):
        mirror = self.mirror({"public.app_purchase_order": ["staff:purchasing"],
                              "sage.*": ["staff:finance"],
                              "public.app_sales_order": ["staff:operations"]})
        purchasing = Actor("plx", frozenset({"staff:purchasing"}))
        result = mirror.resolve(purchasing, "PO-1001", fill_missing=True)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual([item["relation"] for item in result["coverage"]["searched"]],
                         ["public.app_purchase_order"])
        self.assertEqual(self.fake.row_requests("sage.po_header"), [])

    def test_resolve_assembly_code_and_order_number(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.assembly_order")
        mirror.rows(OPS, "public.app_sales_order")
        ao = mirror.resolve(OPS, "ao-26005b")
        self.assertEqual([(c["relation"], c["column"], c["kind"], c["key"])
                          for c in ao["candidates"]],
                         [("public.assembly_order", "nameamo_c", "fm_assembly_order_code",
                           {"id": 10718})])
        order = mirror.resolve(OPS, "OR1000151885")
        self.assertEqual([(c["relation"], c["column"]) for c in order["candidates"]],
                         [("public.app_sales_order", "order_number")])
        # One key inside a JSON-object column (Portal SO bridged from FM).
        bridged = mirror.resolve(OPS, "or2000000001")["candidates"]
        self.assertEqual([(c["column"], c["kind"], c["value"], c["key"]) for c in bridged],
                         [("custom_fields.erpCode", "fm_order_erp_code", "OR2000000001",
                           {"id": 2})])
        self.assertEqual(mirror.resolve(OPS, "nothing-here")["candidate_count"], 0)
        for literal in ("", "   ", "x" * 300, "bad\x00"):
            with self.assertRaises(MirrorError):
                mirror.resolve(OPS, literal)

    def test_resolve_hides_rows_absent_from_latest_full_traversal(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.assembly_order")
        self.assertEqual(mirror.resolve(OPS, "AO-26006")["candidate_count"], 1)
        self.fake.tables["public.assembly_order"] = [
            row for row in self.fake.tables["public.assembly_order"] if row["id"] != 10719]
        mirror.refresh(OPS, "public.assembly_order", full=True)
        self.assertEqual(mirror.resolve(OPS, "AO-26006")["candidate_count"], 0)

    def test_identifier_config_change_rebuilds_index(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.customer")
        self.assertEqual(mirror.resolve(OPS, "C7")["candidate_count"], 0)
        mirror.close()
        custom = self.mirror(identifiers={"public.customer": [("code", "customer_code",
                                                                "external_reference")]})
        self.assertEqual(custom.resolve(OPS, "c7")["candidate_count"], 1)


class DecimalTests(unittest.TestCase):
    def test_float_artifact_keeps_exact_and_flags_normalization(self):
        reading = parse_decimal("35.099999999999994")
        self.assertEqual(reading["exact"], "35.099999999999994")
        self.assertEqual(reading["normalized"], "35.1")
        self.assertTrue(reading["changed_by_normalization"])
        self.assertEqual(reading["rule"], "binary64-artifact-rounded-to-15-significant-digits")
        self.assertEqual(reading["raw"], "35.099999999999994")
        self.assertEqual(parse_decimal(0.1 + 0.2)["normalized"], "0.3")
        self.assertEqual(parse_decimal(35.099999999999994)["exact"], "35.099999999999994")

    def test_exact_values_are_not_rounded(self):
        for raw, exact in (("12.50", "12.50"), (" 1509 ", "1509"), (1502, "1502"),
                           ("-0.0001", "-0.0001"), ("1E+3", "1000"),
                           ("9223372036854775807", "9223372036854775807"),
                           ("12345678901234.5", "12345678901234.5")):
            reading = parse_decimal(raw)
            self.assertEqual((reading["exact"], reading["normalized"]), (exact, exact), raw)
            self.assertEqual(reading["rule"], "exact-decimal")
            self.assertFalse(reading["changed_by_normalization"])
        # 16+ significant digits with a fraction exceed binary64; exact is kept.
        wide = parse_decimal("123456789012345.6")
        self.assertEqual((wide["exact"], wide["normalized"]),
                         ("123456789012345.6", "123456789012346"))
        self.assertTrue(wide["changed_by_normalization"])

    def test_non_numeric_values(self):
        for raw, rule in ((None, "null"), ("", "not_numeric"), ("N/A", "not_numeric"),
                          ("1,234.50", "not_numeric"), (True, "not_numeric"),
                          ("NaN", "non_finite"), ("-Infinity", "non_finite"),
                          (float("inf"), "non_finite"), ({"a": 1}, "not_numeric")):
            reading = parse_decimal(raw)
            self.assertEqual(reading["rule"], rule, raw)
            self.assertIsNone(reading["normalized"])


class CatalogAndCliTests(MirrorTestCase):
    def test_catalog_reports_columns_identifiers_and_freshness(self):
        mirror = self.mirror()
        before = {r["name"]: r for r in mirror.catalog(OPS)["relations"]}
        self.assertEqual(len(before), len(CATALOG))
        self.assertFalse(before["public.assembly_order"]["mirrored"])
        self.assertIsNone(before["public.assembly_order"]["freshness"])
        self.assertEqual([c["column"] for c in
                          before["public.assembly_order"]["identifier_columns"]],
                         ["nameamo_c", "lotno"])
        mirror.rows(OPS, "public.assembly_order")
        after = {r["name"]: r for r in mirror.catalog(OPS)["relations"]}
        freshness = after["public.assembly_order"]["freshness"]
        self.assertEqual(freshness["row_count"], 4)
        self.assertEqual(freshness["since"], {"column": "audittimestampmodified",
                                              "mode": "timestamp-utc"})
        self.assertEqual(self.fake.row_requests(), ["/v1/relations/public.assembly_order/rows"])

    def test_cli_resolves_citation_offline_without_credentials(self):
        audience = Path(self.directory.name) / "audience.json"
        audience.write_text(json.dumps({"schema": "plx-source-mirror-audience/v1",
                                        "tenant": "plx",
                                        "audiences": {"public.customer": ["staff:operations"]}}))
        mirror = self.mirror(audience)
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        mirror.close()
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output):
            code = main(["--db", str(self.path), "--audience", str(audience),
                         "--principal", "staff:operations", "cite", citation])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["row"]["code"], "C7")
        errors = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stderr(errors):
            code = main(["--db", str(self.path), "--audience", str(audience),
                         "--principal", "staff:operations", "rows", "public.app_quote"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(errors.getvalue())["error"], "not_visible")


class PoReferenceStrengthTests(MirrorTestCase):
    """po_reference strength follows the Portal writer of each row
    (petralabx/plx-customer-portal staging@8da33e4)."""

    def test_strength_depends_on_the_row_origin_not_the_column(self):
        rows = [
            # Sage ETL: po_reference = COALESCE(oe_order_header.ponumber, po_reference).
            {"id": "s1", "order_number": "DSO100001", "po_reference": "4500123",
             "custom_fields": {"sageOrdUniq": 70123.0, "fmId": "9"}},
            {"id": "s2", "order_number": "DSO100002", "po_reference": "4500123",
             "custom_fields": json.dumps({"source": "sage.oe_order_header"})},
            # FM-ensure: poReference = order_entry.custpocode.
            {"id": "f1", "order_number": "SO100003", "po_reference": "4500123",
             "custom_fields": {"fmId": "351", "source": "fm-oe-ensure"}},
            # Portal order entry, bridged to FM afterwards.
            {"id": "g1", "order_number": "SO100004", "po_reference": "4500123",
             "custom_fields": {"fmId": "900004", "source": "gold-so-bridge"}},
            # fmId without source and no legacy marker (classify_origin lineage name).
            {"id": "l1", "order_number": "SO100007", "po_reference": "4500123",
             "custom_fields": {"fmId": 353}},
            # Legacy migrate-fm-to-portal.sql, also after a BC renumber.
            {"id": "fm-so-12", "order_number": "SO-FM-12", "po_reference": "4500123",
             "custom_fields": {"fmId": 12, "custPoCode": ""}},
            {"id": "x13", "order_number": "101235", "po_reference": "4500123",
             "custom_fields": {"fmOeNumber": "SO-FM-13", "renumberedTo": "101235"}},
            # Unreadable or unrecognised origin stays conservative.
            {"id": "u1", "order_number": "SO100005", "po_reference": "4500123",
             "custom_fields": "not json"},
            {"id": "u2", "order_number": "SO100006", "po_reference": "4500123",
             "custom_fields": {"source": "other.system"}}]
        self.fake.tables["public.app_sales_order"] = [
            {"total_amount": "1.00", "updated_at": "2026-09-29T09:00:00", **row}
            for row in rows]
        mirror = self.mirror()
        mirror.rows(OPS, "public.app_sales_order")
        found = {c["key"]["id"]: (c["identifier_strength"], c["strength_basis"])
                 for c in mirror.resolve(OPS, "4500123")["candidates"]}
        self.assertEqual(found, {
            "s1": ("external_reference", "sage_etl_ponumber"),
            "s2": ("external_reference", "sage_etl_ponumber"),
            "f1": ("external_reference", "fm_oe_ensure_custpocode"),
            "g1": ("external_reference", "portal_order_entry"),
            "l1": ("external_reference", "fm_linked_without_source"),
            "fm-so-12": ("free_text", "legacy_fm_migration_reference_text"),
            "x13": ("free_text", "legacy_fm_migration_reference_text"),
            "u1": ("free_text", "origin_unknown"),
            "u2": ("free_text", "origin_unknown")})
        catalog = {item["name"]: item for item in mirror.catalog(OPS)["relations"]}
        columns = {item["column"]: item for item in
                   catalog["public.app_sales_order"]["identifier_columns"]}
        self.assertIn("legacy", columns["po_reference"]["strength_depends_on_row"])
        self.assertNotIn("strength_depends_on_row", columns["order_number"])
        # The pre-renumber number stays resolvable (align-portal-so-number.ts).
        previous = mirror.resolve(OPS, "SO-FM-13")["candidates"]
        self.assertEqual([(c["key"]["id"], c["column"]) for c in previous],
                         [("x13", "custom_fields.fmOeNumber")])

    def test_a_customer_po_match_names_one_identity_per_customer(self):
        # Portal TASK-2401 (order-identity.ts@40f7802 resolveOrderByNumber): a PO is
        # unique per customer, matched on customer plus the normalised PO.
        self.fake.catalog["public.app_sales_order"]["columns"].append("customer_id")
        rows = [("a1", "cust-A", "PO-77"), ("a2", "cust-A", " po-77 "),
                ("b1", "cust-B", "PO-77"), ("n1", None, "PO-77"), ("w1", " ", "PO-77")]
        self.fake.tables["public.app_sales_order"] = [
            {"id": key, "order_number": f"SO-{key}", "po_reference": po,
             "customer_id": customer, "total_amount": "1.00",
             "custom_fields": {"source": "gold-so-bridge"},
             "updated_at": "2026-09-29T09:00:00"} for key, customer, po in rows]
        mirror = self.mirror()
        mirror.rows(OPS, "public.app_sales_order")
        found = {c["key"]["id"]: c["identity"]
                 for c in mirror.resolve(OPS, "PO-77")["candidates"]}
        self.assertEqual(sorted(found), ["a1", "a2", "b1", "n1", "w1"])
        self.assertEqual({(i["kind"], i["scope_field"]) for i in found.values()},
                         {("customer_po", "customer_id")})
        self.assertEqual(found["a1"]["key"], found["a2"]["key"])
        self.assertIsNotNone(found["a1"]["key"])
        self.assertNotEqual(found["a1"]["key"], found["b1"]["key"])
        # No customer: no identity, as the Portal skips the PO match.
        for key in ("n1", "w1"):
            self.assertEqual((found[key]["key"], found[key]["reason"]),
                             (None, "no_scope_value"))
        # Only scoped kinds carry an identity: an order number match does not.
        order = mirror.resolve(OPS, "SO-a1")["candidates"]
        self.assertEqual([("identity" in c, c["column"]) for c in order],
                         [(False, "order_number")])

    def test_portal_trim_text_renders_like_javascript_string(self):
        from integrations.source_mirror import portal_trim_text
        for value, text in ((70123.0, "70123"), (7, "7"), (-0.0, "0"), (False, "false"),
                            (" \ufeffS1 \n", "S1"), ({"a": 1}, "[object Object]"),
                            ([None, 2], ",2"), (1.5, "1.5")):
            self.assertEqual(portal_trim_text(value), text, value)
        for value in (None, "", " \t", [], [None]):
            self.assertIsNone(portal_trim_text(value), value)

    def test_every_answer_says_the_rows_are_the_staging_primary_not_uat(self):
        result = self.mirror().rows(OPS, "public.app_sales_order")
        caveats = " ".join(result["freshness"]["caveats"])
        self.assertIn("staging primary", caveats)
        self.assertIn("not from the UAT database", caveats)
        self.assertIn("not what staff entered or see in the staging Portal", caveats)


class ConcurrentRefreshTests(MirrorTestCase):
    """A delayed page or finish of a refresh that another
    refresh took over must never change a capture behind an issued citation."""

    REL = "public.concurrent"

    def setUp(self):
        super().setUp()
        self.fake.catalog[self.REL] = {"columns": ["id", "label"], "key": ["id"],
                                       "domain": "orders", "since": None}
        self.fake.tables[self.REL] = [{"id": i, "label": f"ORIGINAL-{i}"} for i in (1, 2, 3, 4)]
        self.during = None  # (predicate(url), callback) run once inside a writer's request

    def hooked(self, url, headers, *, max_bytes):
        response = self.fake(url, headers, max_bytes=max_bytes)
        if self.during is not None and self.during[0](url):
            callback, self.during = self.during[1], None
            callback()
        return response

    def writer(self, *, hooked=False, **options):
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.hooked if hooked else self.fake)
        options = {"retry_delay_seconds": 0, "page_size": 2, "read_attempts": 1, **options}
        mirror = SourceMirror(self.path, client, ALL, clock=self.clock, **options)
        self.addCleanup(mirror.close)
        return mirror

    def db(self):
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    def interrupt_after_first_page(self, mirror):
        """Leave an in-progress traversal with one committed page (a crashed writer)."""
        def crash():
            raise RuntimeError("connection reset")
        self.during = (lambda url: "/rows" in url and "cursor=" in url, crash)
        with self.assertRaises(MirrorError):
            mirror.refresh(OPS, self.REL)

    def test_a_delayed_page_of_a_superseded_refresh_never_rewrites_a_cited_capture(self):
        a, b = self.writer(hooked=True), self.writer()
        a.refresh(OPS, self.REL)
        self.fake.update(self.REL, 3, label="INTERMEDIATE-3")
        issued = {}

        def writer_b():  # A's final page (INTERMEDIATE-3) is in flight meanwhile
            self.fake.update(self.REL, 3, label="LATEST-3")
            issued["refresh"] = b.refresh(OPS, self.REL, full=True)
            issued["row"] = b.rows(OPS, self.REL, where=[["id", "eq", 3]])["rows"][0]

        self.during = (lambda url: "/rows" in url and "cursor=" in url, writer_b)
        late = a.refresh(OPS, self.REL, full=True)
        self.assertIsNone(issued["refresh"]["refresh_error"])
        cited = issued["row"]
        self.assertEqual(cited["row"]["label"], "LATEST-3")
        capture_id = issued["refresh"]["latest_capture_id"]
        # The late writer fails loudly; it never reports success.
        self.assertEqual(late["refreshed_now"], "none")
        self.assertTrue(late["refresh_error"].startswith("capture_superseded"), late)
        self.assertEqual(late["latest_capture_id"], capture_id)
        for mirror in (a, b):
            resolved = mirror.resolve_citation(cited["citation"], OPS)
            self.assertTrue(resolved["ok"])
            self.assertEqual(resolved["status"], "current")
            self.assertFalse(resolved["changed_since"])
            self.assertEqual(resolved["row"]["label"], "LATEST-3")
            now = mirror.rows(OPS, self.REL, where=[["id", "eq", 3]])["rows"][0]
            self.assertEqual((now["citation"], now["row"]["label"]),
                             (cited["citation"], "LATEST-3"))
        db = self.db()
        self.assertEqual(db.execute("SELECT state FROM capture_state WHERE capture_id = ?",
                                    (capture_id,)).fetchone()[0], "complete")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM stage_row").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_progress").fetchone()[0], 0)
        self.assertTrue(db.execute("SELECT error FROM refresh_failure WHERE relation = ?",
                                   (self.REL,)).fetchone()[0].startswith("capture_superseded"))

    def test_a_delayed_finish_of_a_superseded_refresh_leaves_the_capture_unchanged(self):
        a, b = self.writer(hooked=True), self.writer()
        a.refresh(OPS, self.REL)
        self.fake.update(self.REL, 4, label="LATEST-4")
        seen = {}

        def writer_b():  # A has committed its last page; its finish is delayed
            seen["refresh"] = b.refresh(OPS, self.REL, full=True)
            seen["capture"] = dict(self.db().execute(
                "SELECT * FROM capture WHERE capture_id = ?",
                (seen["refresh"]["latest_capture_id"],)).fetchone())

        rows_done = {"last": False}

        def after_last_page(url):
            if "/rows" in url and "cursor=" in url:
                rows_done["last"] = True
                return False
            return rows_done["last"] and url.endswith("/v1/health")

        self.during = (after_last_page, writer_b)
        late = a.refresh(OPS, self.REL, full=True)
        self.assertIsNone(seen["refresh"]["refresh_error"])
        self.assertEqual(seen["refresh"]["refreshed_now"], "full_traversal")
        self.assertTrue(late["refresh_error"].startswith("capture_superseded"), late)
        capture_id = seen["refresh"]["latest_capture_id"]
        db = self.db()
        self.assertEqual(dict(db.execute("SELECT * FROM capture WHERE capture_id = ?",
                                         (capture_id,)).fetchone()), seen["capture"])
        self.assertEqual(db.execute("SELECT state FROM capture_state WHERE capture_id = ?",
                                    (capture_id,)).fetchone()[0], "complete")
        self.assertEqual(b.rows(OPS, self.REL, where=[["id", "eq", 4]])["rows"][0]["row"]
                         ["label"], "LATEST-4")

    def test_resume_takes_over_by_compare_and_swap_on_the_owner(self):
        a, b = self.writer(hooked=True), self.writer(hooked=True)
        self.interrupt_after_first_page(a)
        db = self.db()
        crashed = dict(db.execute("SELECT * FROM sync_progress").fetchone())
        self.assertRegex(crashed["owner"], r"^[0-9a-f]{16}$")
        rival = self.writer(hooked=True)
        original_health = b._health
        calls = {"n": 0}

        def health_then_rival_takes_over():
            calls["n"] += 1
            if calls["n"] == 1:  # between B reading the progress and its swap
                self.interrupt_after_first_page(rival)
            return original_health()

        with patch.object(b, "_health", side_effect=health_then_rival_takes_over):
            with self.assertRaises(MirrorError) as raised:
                b.refresh(OPS, self.REL)
        self.assertEqual(raised.exception.code, "capture_superseded")
        held = dict(db.execute("SELECT * FROM sync_progress").fetchone())
        self.assertEqual((held["capture_id"], held["pages"], held["resumed"]),
                         (crashed["capture_id"], crashed["pages"], crashed["resumed"] + 1))
        self.assertNotEqual(held["owner"], crashed["owner"])
        self.assertEqual(db.execute("SELECT state FROM capture_state WHERE capture_id = ?",
                                    (crashed["capture_id"],)).fetchone()[0], "in_progress")
        # The rival's crashed traversal is still resumable by the next refresh.
        result = self.writer().refresh(OPS, self.REL)
        self.assertIsNone(result["refresh_error"])
        self.assertEqual(result["latest_capture_id"], crashed["capture_id"])

    def test_a_crashed_writers_traversal_is_resumed_by_the_next_refresh(self):
        a = self.writer(hooked=True)
        self.interrupt_after_first_page(a)
        a.close()
        before = len(self.fake.row_requests(self.REL))
        nxt = self.writer()
        result = nxt.refresh(OPS, self.REL)
        self.assertIsNone(result["refresh_error"])
        self.assertEqual(len(self.fake.row_requests(self.REL)) - before, 1)
        rows = nxt.rows(OPS, self.REL)["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual({item["capture_id"] for item in rows}, {result["latest_capture_id"]})

    def test_a_pre_owner_file_is_migrated_and_its_traversal_resumed(self):
        a = self.writer(hooked=True)
        self.interrupt_after_first_page(a)
        a.close()
        with contextlib.closing(sqlite3.connect(str(self.path))) as db:
            db.execute("ALTER TABLE sync_progress DROP COLUMN owner")
            db.commit()
        reopened = self.writer()
        columns = {row["name"] for row in self.db().execute("PRAGMA table_info(sync_progress)")}
        self.assertIn("owner", columns)
        self.assertIsNone(self.db().execute("SELECT owner FROM sync_progress").fetchone()[0])
        result = reopened.refresh(OPS, self.REL)
        self.assertIsNone(result["refresh_error"])
        self.assertEqual(len(reopened.rows(OPS, self.REL)["rows"]), 4)

    def test_a_delayed_first_page_never_replaces_a_newer_completed_capture(self):
        """No traversal row exists after B started and completed
        a newer capture, yet A's late first page must not become current."""
        a, b = self.writer(hooked=True), self.writer()
        a.refresh(OPS, self.REL)
        self.fake.update(self.REL, 1, label="INTERMEDIATE-1")
        issued = {}

        def writer_b():  # A's first page (INTERMEDIATE-1) is in flight meanwhile
            self.fake.update(self.REL, 1, label="LATEST-1")
            issued["refresh"] = b.refresh(OPS, self.REL, full=True)
            issued["row"] = b.rows(OPS, self.REL, where=[["id", "eq", 1]])["rows"][0]

        self.during = (lambda url: "/rows" in url and "cursor=" not in url, writer_b)
        late = a.refresh(OPS, self.REL, full=True)
        self.assertIsNone(issued["refresh"]["refresh_error"])
        self.assertEqual(issued["row"]["row"]["label"], "LATEST-1")
        self.assertTrue(late["refresh_error"].startswith(
            "capture_superseded: another refresh completed a newer capture"), late)
        capture_id = issued["refresh"]["latest_capture_id"]
        self.assertEqual(late["latest_capture_id"], capture_id)
        resolved = a.resolve_citation(issued["row"]["citation"], OPS)
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["status"], "current")
        self.assertFalse(resolved["changed_since"])
        self.assertEqual(resolved["row"]["label"], "LATEST-1")
        now = b.rows(OPS, self.REL, where=[["id", "eq", 1]])["rows"][0]
        self.assertEqual((now["citation"], now["row"]["label"]),
                         (issued["row"]["citation"], "LATEST-1"))
        db = self.db()
        self.assertEqual(db.execute("SELECT MAX(capture_id) FROM capture").fetchone()[0],
                         capture_id)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_progress").fetchone()[0], 0)

    def test_a_delayed_first_page_of_a_first_fill_fails_loudly(self):
        a, b = self.writer(hooked=True), self.writer()
        filled = {}

        def writer_b():
            filled["refresh"] = b.refresh(OPS, self.REL)

        self.during = (lambda url: "/rows" in url and "cursor=" not in url, writer_b)
        with self.assertRaises(MirrorError) as raised:
            a.refresh(OPS, self.REL)
        self.assertEqual(raised.exception.code, "capture_superseded")
        self.assertIn("completed a newer capture", str(raised.exception))
        self.assertIsNone(filled["refresh"]["refresh_error"])
        self.assertEqual(len(b.rows(OPS, self.REL)["rows"]), 4)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM capture").fetchone()[0], 1)

    def test_a_superseded_writer_never_abandons_the_completed_capture(self):
        """A's cursor is rejected after B resumed and completed
        A's traversal; A's abandon must not mark that complete capture abandoned."""
        a, b = self.writer(hooked=True), self.writer()
        a.refresh(OPS, self.REL)
        self.fake.update(self.REL, 3, label="LATEST-3")
        done = {}

        def writer_b_then_reject():
            done["refresh"] = b.refresh(OPS, self.REL, full=True)
            raise SourceHttpError("read-only source GET failed: HTTPError", status=400,
                                  error_code="invalid-cursor")

        self.during = (lambda url: "/rows" in url and "cursor=" in url, writer_b_then_reject)
        late = a.refresh(OPS, self.REL, full=True)
        self.assertIsNone(done["refresh"]["refresh_error"])
        self.assertTrue(late["refresh_error"].startswith(
            "capture_superseded: another refresh completed this traversal"), late)
        capture_id = done["refresh"]["latest_capture_id"]
        db = self.db()
        self.assertEqual(db.execute("SELECT state, note FROM capture_state WHERE capture_id = ?",
                                    (capture_id,)).fetchone()[:], ("complete", None))
        self.assertEqual(db.execute("SELECT MAX(capture_id) FROM capture").fetchone()[0],
                         capture_id)
        self.assertEqual(b.rows(OPS, self.REL, where=[["id", "eq", 3]])["rows"][0]["row"]
                         ["label"], "LATEST-3")

    # -- each owner check on its own (review mo-2) -----------------------------

    def resume_with_rival(self, rival_action, *, page_size=2, on_request=1):
        """An interrupted traversal (one committed page) resumed by ``resumer``;
        ``rival_action`` runs while the resumer's ``on_request``-th row request
        is in flight. Returns (resumer's MirrorError, stored progress before)."""
        self.interrupt_after_first_page(self.writer(hooked=True, page_size=page_size))
        crashed = dict(self.db().execute("SELECT * FROM sync_progress").fetchone())
        resumer = self.writer(hooked=True, page_size=page_size)
        seen = {"n": 0}

        def nth_row_request(url):
            if "/rows" not in url:
                return False
            seen["n"] += 1
            return seen["n"] == on_request

        self.during = (nth_row_request, rival_action)
        with self.assertRaises(MirrorError) as raised:
            resumer.refresh(OPS, self.REL)
        return raised.exception, crashed

    def rival_takes_over(self):
        def crash():
            raise RuntimeError("connection reset")
        self.during = (lambda url: "/rows" in url, crash)  # after its swap, before a page
        with self.assertRaises(MirrorError):
            self.writer(hooked=True).refresh(OPS, self.REL)

    def test_a_first_page_after_a_rivals_first_page_is_refused_by_the_row_check(self):
        a, b = self.writer(hooked=True), self.writer(hooked=True)
        self.during = (lambda url: "/rows" in url and "cursor=" not in url,
                       lambda: self.interrupt_after_first_page(b))
        with self.assertRaises(MirrorError) as raised:
            a.refresh(OPS, self.REL)
        self.assertEqual(raised.exception.code, "capture_superseded")
        self.assertEqual(str(raised.exception),
                         "another refresh started a traversal of this relation first; "
                         "this refresh stored nothing")
        db = self.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM capture").fetchone()[0], 1)
        held = dict(db.execute("SELECT * FROM sync_progress").fetchone())
        self.assertEqual((held["pages"], held["resumed"]), (1, 0))
        result = self.writer().refresh(OPS, self.REL)
        self.assertIsNone(result["refresh_error"])
        self.assertEqual(result["latest_capture_id"], held["capture_id"])

    def test_a_primary_key_clash_on_the_first_page_is_a_supersession_not_a_sqlite_error(self):
        """Without the row check, two concurrent first pages collide on the
        sync_progress key; the IntegrityError must not escape ``except MirrorError``."""
        a, b = self.writer(hooked=True), self.writer(hooked=True)
        self.during = (lambda url: "/rows" in url and "cursor=" not in url,
                       lambda: self.interrupt_after_first_page(b))
        with patch.object(a, "_check_owner", lambda progress: None):
            report = a.sync(OPS, relations=[self.REL])
        run = report["runs"][0]
        self.assertEqual(run["outcome"], "failed")
        self.assertTrue(run["error"].startswith("capture_superseded: "), run)
        self.assertIn("stored nothing", run["error"])
        db = self.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM capture").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT pages FROM sync_progress").fetchone()[0], 1)

    def test_only_the_primary_key_clash_is_a_supersession(self):
        import sqlite3
        w = self.writer()
        progress = {"mode": "full", "reason": "first", "since_sent": None,
                    "catalog_revision": "r", "columns_json": "[]", "page_size": 10,
                    "started_at": "t", "resumed": 0, "health_before_json": "{}",
                    "owner": "o"}
        page = {"next_cursor": None}
        w._db.execute("BEGIN")
        try:
            w._insert_progress("rel", 1, progress, page, 1, 1, 1, "t")
            with self.assertRaises(MirrorError) as raised:
                w._insert_progress("rel", 2, progress, page, 1, 1, 1, "t")
            self.assertEqual(raised.exception.code, "capture_superseded")
            with self.assertRaises(sqlite3.IntegrityError):  # NOT NULL, not the key
                w._insert_progress("other", 3, dict(progress, mode=None), page, 1, 1, 1, "t")
        finally:
            w._db.execute("ROLLBACK")

    def test_a_resumed_writer_whose_traversal_was_taken_over_stops(self):
        error, crashed = self.resume_with_rival(self.rival_takes_over)
        self.assertEqual(error.code, "capture_superseded")
        self.assertEqual(str(error), "another refresh took this traversal over; "
                                     "this refresh stored nothing")
        held = dict(self.db().execute("SELECT * FROM sync_progress").fetchone())
        self.assertEqual((held["capture_id"], held["pages"], held["resumed"]),
                         (crashed["capture_id"], 1, 2))
        result = self.writer().refresh(OPS, self.REL)
        self.assertIsNone(result["refresh_error"])
        self.assertEqual(len(self.writer().rows(OPS, self.REL)["rows"]), 4)

    def test_a_superseded_writer_says_its_committed_pages_stay_with_the_traversal(self):
        error, crashed = self.resume_with_rival(self.rival_takes_over, page_size=1,
                                                on_request=2)
        self.assertEqual(str(error), (
            "another refresh took this traversal over; this refresh stores no further page; "
            "the 1 page(s) it committed stay with the traversal the other refresh holds"))
        held = dict(self.db().execute("SELECT * FROM sync_progress").fetchone())
        self.assertEqual(held["pages"], crashed["pages"] + 1)
        before = len(self.fake.row_requests(self.REL))
        result = self.writer(page_size=1).refresh(OPS, self.REL)
        self.assertIsNone(result["refresh_error"])
        # The committed pages are reused: only the two remaining pages are read.
        self.assertEqual(len(self.fake.row_requests(self.REL)) - before, 2)
        self.assertEqual(len(self.writer().rows(OPS, self.REL)["rows"]), 4)

    def test_a_page_count_that_moved_under_the_writer_stops_it(self):
        def bump_pages():
            with contextlib.closing(sqlite3.connect(str(self.path))) as db:
                db.execute("UPDATE sync_progress SET pages = pages + 1")
                db.commit()
        error, _ = self.resume_with_rival(bump_pages)
        self.assertEqual(error.code, "capture_superseded")
        self.assertTrue(str(error).startswith("another refresh took this traversal over"))
        self.assertEqual(self.db().execute(
            "SELECT state FROM capture_state").fetchone()[0], "in_progress")

    def test_a_capture_no_longer_in_progress_is_never_written_again(self):
        def complete_capture():
            with contextlib.closing(sqlite3.connect(str(self.path))) as db:
                db.execute("UPDATE capture_state SET state = 'complete'")
                db.commit()
        error, crashed = self.resume_with_rival(complete_capture)
        self.assertEqual(error.code, "capture_superseded")
        self.assertTrue(str(error).startswith("another refresh completed this traversal"))
        db = self.db()
        self.assertEqual(db.execute("SELECT pages FROM sync_progress").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM stage_row").fetchone()[0], 2)
        self.assertIsNone(db.execute("SELECT * FROM relation_state").fetchone())

    def test_a_failed_resume_swap_reads_no_page(self):
        a, b = self.writer(hooked=True), self.writer(hooked=True)
        self.interrupt_after_first_page(a)
        rival = self.writer(hooked=True)
        original_health = b._health
        requests = {}

        def health_then_rival_takes_over():
            if not requests:
                self.interrupt_after_first_page(rival)
                requests["after_rival"] = len(self.fake.row_requests(self.REL))
            return original_health()

        with patch.object(b, "_health", side_effect=health_then_rival_takes_over):
            with self.assertRaises(MirrorError) as raised:
                b.refresh(OPS, self.REL)
        self.assertEqual(str(raised.exception),
                         "another refresh took over or completed the interrupted traversal "
                         "first; this refresh stored nothing")
        self.assertEqual(len(self.fake.row_requests(self.REL)), requests["after_rival"])



def _portal_cursor(name, key, values):
    """Portal's cursor: base64url JSON of relation, key columns and the last row's key
    (petralabx/plx-customer-portal services/workbench-api/src/core/query.mjs@c3ffda3
    serializeCursor)."""
    payload = json.dumps({"r": name, "k": key, "v": values}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


class RunawayFake(FakeWorkbench):
    """The fake Workbench plus the failure shapes seen live on 2026-10-01/02.

    ``repeat_keyset``: the server ignores the cursor (the keyset restarts), with the
    same or a fresh cursor each time. ``reinsert``: every row request first appends
    rows under fresh, later-sorting ids (Portal's etl-silver-to-gold.ts inserts
    ``cuid()`` per run, so ``ON CONFLICT (id)`` never fires). ``server_errors``: an
    HTTP 500 with Portal's error code while ``limit`` is below a threshold (None:
    always). ``timeout_from``: a 503 ``statement-timeout`` while ``limit`` is at or
    above a threshold. ``portal_cursor``: cursors in Portal's base64url shape;
    ``skip_cursor``: such a cursor naming a later key than the page's last row.
    ``request_cap``: past that many row requests every one is a final HTTP 400, so a
    pager that never stops fails its test instead of hanging it.
    """

    def __init__(self):
        super().__init__()
        self.repeat_keyset = {}
        self.reinsert = {}
        self.server_errors = {}
        self.timeout_from = {}
        self.request_cap = 300
        self.portal_cursor = set()
        self.skip_cursor = set()
        self.requests = []
        self.inflight = 0
        self.max_inflight = 0
        self.delay = 0.0
        self._lock = threading.Lock()

    def __call__(self, url, headers, *, max_bytes):
        parts = urlsplit(url)
        if not parts.path.endswith("/rows"):
            return super().__call__(url, headers, max_bytes=max_bytes)
        with self._lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay:
                time.sleep(self.delay)
            return self._row_page(url, headers, max_bytes)
        finally:
            with self._lock:
                self.inflight -= 1

    def _row_page(self, url, headers, max_bytes):
        parts = urlsplit(url)
        name = parts.path.split("/")[3]
        query = parse_qs(parts.query)
        limit = int(query["limit"][0])
        cursor = query.get("cursor", [None])[0]
        with self._lock:
            self.requests.append((name, limit, cursor))
            capped = len(self.requests) > self.request_cap
        if capped:
            raise SourceHttpError("read-only source GET failed: HTTPError", status=400,
                                  error_code="request-cap")
        if name in self.timeout_from and limit >= self.timeout_from[name]:
            raise SourceHttpError("read-only source GET failed: HTTPError", status=503,
                                  error_code="statement-timeout")
        if name in self.server_errors:
            code, below = self.server_errors[name]
            if below is None or limit < below:
                raise SourceHttpError("read-only source GET failed: HTTPError", status=500,
                                      error_code=code)
        if name in self.reinsert:
            table = self.tables[name]
            top = max(row["id"] for row in table)
            table.extend({**table[-1], "id": top + 1 + index}
                         for index in range(self.reinsert[name]))
        key = self.catalog[name]["key"]
        if cursor is not None and name in self.portal_cursor:
            values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))["v"]
            cursor = json.dumps(values)
        if name in self.repeat_keyset:
            cursor = None
        rebuilt = {k: v for k, v in query.items() if k != "cursor"}
        if cursor is not None:
            rebuilt["cursor"] = [cursor]
        page = self._rows(name, rebuilt)
        if page["nextCursor"] is not None:
            last = json.loads(page["nextCursor"])
            if name in self.repeat_keyset:
                self.repeat_keyset[name] += 1
                if self.repeat_keyset[name] > 1:   # a fresh cursor every time
                    page["nextCursor"] = json.dumps(last + [self.repeat_keyset[name]])
            if name in self.portal_cursor:
                if name in self.skip_cursor:
                    ordered = sorted(self.tables[name], key=lambda row: [row[c] for c in key])
                    index = [[row[c] for c in key] for row in ordered].index(last)
                    last = [ordered[index + 1][c] for c in key]
                page["nextCursor"] = _portal_cursor(name, key, last)
        return page

    def limits(self, name):
        return [limit for relation, limit, _ in self.requests if relation == name]


def _numbered(fake, name, count, *, since=None):
    fake.catalog[name] = {"columns": ["id", "code", "updated_at"], "key": ["id"],
                          "domain": "manufacturing", "since": since}
    fake.tables[name] = [{"id": index, "code": f"C-{index}",
                          "updated_at": "2026-09-28T00:00:00Z"} for index in range(count)]


class PagingRunawayTests(MirrorTestCase):
    """Five gold relations staged 1,000,000 distinct keys and
    committed 0, and one Sage relation returned HTTP 500. A paging defect must stop a
    relation after the page that proves it, a row-bound relation must not stay wedged,
    and one failing relation never blocks the others."""

    REL = "public.app_runaway"

    def setUp(self):
        super().setUp()
        self.fake = RunawayFake()
        self.sleeps = []

    def mirror(self, audience=None, **options):
        options.setdefault("sleep", self.sleeps.append)
        options.setdefault("retry_delay_seconds", 0)
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        mirror = SourceMirror(self.path, client, ALL if audience is None else audience,
                              clock=self.clock, **options)
        self.addCleanup(mirror.close)
        return mirror

    def run_of(self, report, name):
        return {run["relation"]: run for run in report["runs"]}[name]

    def db(self):
        connection = sqlite3.connect(str(self.path))
        self.addCleanup(connection.close)
        return connection

    def assert_abandoned(self, name):
        db = self.db()
        self.assertIsNone(db.execute("SELECT 1 FROM sync_progress WHERE relation = ?",
                                     (name,)).fetchone())
        self.assertEqual(db.execute("SELECT COUNT(*) FROM stage_row WHERE relation = ?",
                                    (name,)).fetchone()[0], 0)

    def test_a_restarting_keyset_is_a_paging_defect_after_the_page_that_proves_it(self):
        for fresh in (False, True):
            with self.subTest(fresh_cursor=fresh):
                self.fake = RunawayFake()
                _numbered(self.fake, self.REL, 9)
                self.fake.repeat_keyset[self.REL] = 1 if fresh else -10**9
                mirror = self.mirror(page_size=3)
                report = mirror.sync(OPS, relations=[self.REL, "public.customer"])
                run = self.run_of(report, self.REL)
                self.assertEqual(run["outcome"], "failed")
                self.assertEqual(run["failure_class"], "paging_defect", run)
                self.assertTrue(run["error"].startswith("paging_defect"), run)
                # Two pages prove it; no retry re-reads the broken keyset.
                self.assertEqual(len(self.fake.limits(self.REL)), 2)
                self.assert_abandoned(self.REL)
                self.assertEqual(self.run_of(report, "public.customer")["outcome"],
                                 "captured")
                mirror.close()
                self.path.unlink()

    def test_reinserted_ids_hit_the_row_bound_without_wedging_the_relation(self):
        _numbered(self.fake, self.REL, 3)
        self.fake.reinsert[self.REL] = 2   # every page appends two fresh, later ids
        mirror = self.mirror(page_size=2, max_rows_per_relation=6)
        report = mirror.sync(OPS, relations=[self.REL, "public.customer"])
        run = self.run_of(report, self.REL)
        self.assertEqual((run["outcome"], run["failure_class"]), ("failed", "row_bound"), run)
        self.assertTrue(run["error"].startswith("row_bound"), run)
        self.assertEqual(run["rows_received"], 6)
        self.assertEqual(run["pages"], 3)
        self.assert_abandoned(self.REL)  # nothing staged survives a doomed traversal
        self.assertEqual(self.run_of(report, "public.customer")["outcome"], "captured")
        # Upstream repairs the ETL: the next run starts fresh instead of resuming a
        # traversal that is already at the bound.
        del self.fake.reinsert[self.REL]
        self.fake.tables[self.REL] = self.fake.tables[self.REL][:5]
        report = mirror.sync(OPS, relations=[self.REL])
        run = self.run_of(report, self.REL)
        self.assertEqual(run["outcome"], "captured", run)
        self.assertFalse(run["resumed"])
        self.assertEqual(run["rows_received"], 5)

    def test_a_cursor_that_skips_past_the_last_row_is_a_paging_defect(self):
        _numbered(self.fake, self.REL, 7)
        self.fake.portal_cursor.add(self.REL)
        mirror = self.mirror(page_size=3)
        run = self.run_of(mirror.sync(OPS, relations=[self.REL]), self.REL)
        self.assertEqual(run["outcome"], "captured", run)  # a well-formed cursor pages on
        self.assertEqual(run["rows_received"], 7)
        self.fake.skip_cursor.add(self.REL)
        run = self.run_of(mirror.sync(OPS, relations=[self.REL], full=True), self.REL)
        self.assertEqual((run["outcome"], run["failure_class"]), ("failed", "paging_defect"))
        self.assertEqual(run["pages"], 0)
        self.assert_abandoned(self.REL)

    def test_a_non_unique_since_column_pages_by_key_and_misses_nothing(self):
        since = {"column": "updated_at", "mode": "timestamptz"}
        _numbered(self.fake, self.REL, 7, since=since)   # one updated_at for every row
        mirror = self.mirror(page_size=2)
        self.assertEqual(self.run_of(mirror.sync(OPS, relations=[self.REL]),
                                     self.REL)["rows_received"], 7)
        self.clock.advance(3600)
        run = self.run_of(mirror.sync(OPS, relations=[self.REL]), self.REL)
        self.assertEqual(run["mode"], "incremental")
        self.assertEqual(run["rows_received"], 7)   # the overlap re-reads all, keyed
        self.assertEqual(run["pages"], 4)
        self.assertEqual(self.db().execute(
            "SELECT COUNT(*) FROM row_current WHERE relation = ?", (self.REL,)).fetchone()[0], 7)

    def test_key_run_and_null_key_500s_grow_the_page_instead_of_halving_it(self):
        for code in ("key-run-too-long", "null-key"):
            with self.subTest(code=code):
                self.fake = RunawayFake()
                _numbered(self.fake, self.REL, 9)
                self.fake.server_errors[self.REL] = (code, 8)
                mirror = self.mirror(page_size=2, min_page_size=1)
                run = self.run_of(mirror.sync(OPS, relations=[self.REL]), self.REL)
                self.assertEqual(run["outcome"], "captured", run)
                self.assertEqual(self.fake.limits(self.REL), [2, 4, 8, 8])
                self.assertEqual(run["page_size_final"], 8)
                self.assertEqual(self.sleeps, [])
                mirror.close()
                self.path.unlink()

    def test_a_key_run_longer_than_the_largest_page_is_a_source_defect(self):
        _numbered(self.fake, self.REL, 9)
        self.fake.server_errors[self.REL] = ("key-run-too-long", None)
        mirror = self.mirror(page_size=1000)
        run = self.run_of(mirror.sync(OPS, relations=[self.REL]), self.REL)
        self.assertEqual((run["outcome"], run["failure_class"]), ("failed", "source_defect"))
        self.assertEqual((run["http_status"], run["upstream_error_code"]),
                         (500, "key-run-too-long"))
        self.assertEqual(self.fake.limits(self.REL), [1000, 2000, 4000, 5000])

    def test_a_row_too_large_500_fails_at_once_without_halving(self):
        _numbered(self.fake, self.REL, 9)
        self.fake.server_errors[self.REL] = ("row-too-large", None)
        mirror = self.mirror(page_size=8, min_page_size=1)
        run = self.run_of(mirror.sync(OPS, relations=[self.REL]), self.REL)
        self.assertEqual((run["failure_class"], run["upstream_error_code"]),
                         ("source_defect", "row-too-large"))
        self.assertEqual(self.fake.limits(self.REL), [8])

    def oscillating(self):
        """A relation whose pages time out at 8 rows and more but end inside an
        equal-key run below 8: no page size reads its first cursor."""
        _numbered(self.fake, self.REL, 9)
        self.fake.timeout_from[self.REL] = 8
        self.fake.server_errors[self.REL] = ("key-run-too-long", 8)

    def test_timeouts_and_key_runs_on_one_cursor_end_as_a_source_defect_without_a_deadline(
            self):
        self.oscillating()
        mirror = self.mirror(page_size=8, min_page_size=1)
        with self.assertRaises(MirrorError) as caught:
            mirror.refresh(OPS, self.REL)   # a lazy fill: no deadline bounds it
        self.assertEqual(caught.exception.code, "source_defect")
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(caught.exception.upstream_code, "key-run-too-long")
        self.assertIn("statement-timeout", str(caught.exception))
        # Halve once, then bisect between the two errors; never 8, 4, 8, 4, ...
        self.assertEqual(self.fake.limits(self.REL), [8, 4, 6, 7])
        self.assertEqual(self.sleeps, [])
        # The failed fill keeps nothing staged that a later run could stitch onto.
        self.assertEqual(self.db().execute(
            "SELECT COUNT(*) FROM row_current WHERE relation = ?", (self.REL,)).fetchone()[0], 0)

    def test_an_oscillating_relation_in_a_sync_is_a_source_defect_not_the_budget(self):
        self.oscillating()
        ticks = iter(range(10_000))
        mirror = self.mirror(page_size=8, min_page_size=1, monotonic=lambda: next(ticks))
        report = mirror.sync(OPS, budget_seconds=200)
        run = self.run_of(report, self.REL)
        self.assertEqual((run["outcome"], run["failure_class"]), ("failed", "source_defect"))
        self.assertEqual((run["http_status"], run["upstream_error_code"]),
                         (500, "key-run-too-long"))
        self.assertEqual(self.fake.limits(self.REL), [8, 4, 6, 7])
        others = [r for r in report["runs"] if r["relation"] != self.REL]
        self.assertEqual({r["outcome"] for r in others}, {"captured"})

    def test_a_window_that_stays_open_still_captures_at_a_size_between_the_errors(self):
        _numbered(self.fake, self.REL, 9)
        self.fake.timeout_from[self.REL] = 8
        self.fake.server_errors[self.REL] = ("key-run-too-long", 6)
        mirror = self.mirror(page_size=8, min_page_size=1)
        mirror.refresh(OPS, self.REL)
        # 8 timed out, 4 ended in the key run; 6 reads this page and the next.
        self.assertEqual(self.fake.limits(self.REL), [8, 4, 6, 6])
        self.assertEqual(self.db().execute(
            "SELECT COUNT(*) FROM row_current WHERE relation = ?", (self.REL,)).fetchone()[0], 9)

    def test_an_unexpected_exception_fails_one_relation_and_keeps_the_others(self):
        original = SourceMirror._apply_page

        def explode(mirror, relation, *args, **kwargs):
            if relation == "sage.po_header":
                raise KeyError("C-secret-row-value")
            return original(mirror, relation, *args, **kwargs)

        for workers in (1, 3):
            with self.subTest(workers=workers):
                self.fake = RunawayFake()
                with patch.object(SourceMirror, "_apply_page", explode):
                    mirror = self.mirror(page_size=4)
                    report = mirror.sync(OPS, workers=workers)
                run = self.run_of(report, "sage.po_header")
                self.assertEqual((run["outcome"], run["failure_class"], run["exception_type"]),
                                 ("failed", "internal_error", "KeyError"))
                self.assertNotIn("secret", json.dumps(report))
                others = [r for r in report["runs"] if r["relation"] != "sage.po_header"]
                self.assertEqual({r["outcome"] for r in others}, {"captured"})
                # Surfaced, not swallowed: the relation is failed and coverage incomplete.
                self.assertFalse(report["coverage"]["all_served_complete"])
                self.assertEqual(report["coverage"]["complete_relation_count"],
                                 len(self.fake.catalog) - 1)
                mirror.close()
                self.path.unlink()

    def test_a_relation_that_keeps_failing_with_500_never_blocks_the_others(self):
        for workers in (1, 3):
            with self.subTest(workers=workers):
                self.fake = RunawayFake()
                self.sleeps.clear()
                self.fake.server_errors["sage.po_header"] = ("internal", None)
                mirror = self.mirror(page_size=4, min_page_size=1, read_attempts=3,
                                     retry_delay_seconds=1)
                report = mirror.sync(OPS, workers=workers)
                run = self.run_of(report, "sage.po_header")
                self.assertEqual(run["outcome"], "failed")
                self.assertEqual(run["failure_class"], "source_defect")
                self.assertEqual((run["http_status"], run["upstream_error_code"]),
                                 (500, "internal"))
                # Smaller pages first (4 -> 2 -> 1), then backoff retries at the floor.
                self.assertEqual(self.fake.limits("sage.po_header"), [4, 2, 1, 1, 1])
                self.assertEqual(self.sleeps, [1, 2])
                others = [r for r in report["runs"] if r["relation"] != "sage.po_header"]
                self.assertEqual({r["outcome"] for r in others}, {"captured"})
                self.assertEqual(report["coverage"]["complete_relation_count"],
                                 len(self.fake.catalog) - 1)
                mirror.close()
                self.path.unlink()

    # Review mc-r2 and its follow-up: every failure of one relation in a sync is
    # classified and recorded for that relation; the lock wait and a closed page-size
    # window never cost a relation its place.

    def test_an_exception_outside_the_refresh_fails_one_relation_not_the_sync(self):
        original = SourceMirror._audience
        lock = threading.Lock()
        armed = [1]

        def audience(mirror):
            # The first per-relation audience re-read (in _sync_one) fails once.
            if sys._getframe(1).f_code.co_name == "_sync_one":
                with lock:
                    fire = bool(armed) and armed.pop()
                if fire:
                    raise ValueError("C-secret-audience-value")
            return original(mirror)

        for workers in (1, 3):
            with self.subTest(workers=workers):
                self.fake = RunawayFake()
                armed[:] = [1]
                with patch.object(SourceMirror, "_audience", audience):
                    mirror = self.mirror(page_size=4)
                    report = mirror.sync(OPS, workers=workers)
                failed = [r for r in report["runs"] if r["outcome"] != "captured"]
                self.assertEqual(len(failed), 1)
                self.assertEqual((failed[0]["outcome"], failed[0]["failure_class"],
                                  failed[0]["exception_type"]),
                                 ("failed", "internal_error", "ValueError"))
                self.assertNotIn("secret", json.dumps(report))
                self.assertEqual(report["coverage"]["complete_relation_count"],
                                 len(self.fake.catalog) - 1)
                mirror.close()
                self.path.unlink()

    def test_an_exception_in_the_absence_scan_keeps_the_capture_and_the_report(self):
        mirror = self.mirror(page_size=4)
        mirror.sync(OPS)
        with patch.object(SourceMirror, "_scan_absence",
                          side_effect=KeyError("C-secret-row-value")):
            report = mirror.sync(OPS, absence_scan=True, workers=3)
        scanned = [r for r in report["runs"] if "absence_scan" in r]
        self.assertTrue(scanned)
        for run in scanned:
            self.assertEqual(run["outcome"], "captured")
            self.assertEqual(run["absence_scan"], {"error": "internal_error: KeyError",
                                                   "exception_type": "KeyError"})
        self.assertNotIn("secret", json.dumps(report))

    def test_an_internal_error_is_a_refresh_failure_of_that_relation(self):
        original = SourceMirror._apply_page

        def explode(mirror, relation, *args, **kwargs):
            if relation == "sage.po_header":
                raise KeyError("C-secret-row-value")
            return original(mirror, relation, *args, **kwargs)

        with patch.object(SourceMirror, "_apply_page", explode):
            report = self.mirror(page_size=4).sync(OPS, workers=2)
        coverage = report["coverage"]
        items = {item["relation"]: item for item in coverage["relations"]}
        self.assertEqual(items["sage.po_header"]["last_refresh_failure"]["error"],
                         "internal_error: KeyError")
        self.assertFalse(items["sage.po_header"]["complete"])
        self.assertEqual(coverage["failed_count"], 1)
        self.assertEqual(self.db().execute("SELECT error FROM refresh_failure").fetchall(),
                         [("internal_error: KeyError",)])
        # The others ran on and completed.
        self.assertEqual(coverage["complete_relation_count"], len(self.fake.catalog) - 1)
        self.assertNotIn("secret", json.dumps(report))

    def test_waiting_for_the_write_lock_does_not_spend_the_relation_budget(self):
        _numbered(self.fake, self.REL, 9)
        fetch = self.fake
        holder = []

        def fetch_and_lock(url, headers, *, max_bytes):
            page = fetch(url, headers, max_bytes=max_bytes)
            if url.split("?")[0].endswith(f"/{self.REL}/rows") and not holder:
                # Another writer takes the mirror file for one second after page 1.
                other = sqlite3.connect(str(self.path), isolation_level=None,
                                        check_same_thread=False)
                other.execute("BEGIN IMMEDIATE")
                holder.append(other)
                release = threading.Timer(1.0, lambda: (other.rollback(), other.close()))
                release.start()
                self.addCleanup(release.join)
            return page

        client = WorkbenchReadApiClient(BASE, KEY, fetch=fetch_and_lock)
        mirror = SourceMirror(self.path, client, ALL, clock=self.clock, page_size=3,
                              retry_delay_seconds=0)
        self.addCleanup(mirror.close)
        run = mirror.sync(OPS, relations=[self.REL], budget_seconds=0.5)["runs"][0]
        self.assertEqual(run["outcome"], "captured")
        self.assertEqual((run["pages"], run["rows_received"]), (3, 9))
        self.assertGreaterEqual(run["lock_wait_seconds"], 0.9)

    def test_a_closed_window_defers_the_relation_and_keeps_its_traversal(self):
        _numbered(self.fake, self.REL, 9)
        fake = self.fake
        original = fake._row_page

        def row_page(url, headers, max_bytes):
            if "cursor=" in url and self.REL in url:
                # Past the first page no size reads the cursor: 4+ rows time out,
                # fewer end inside an equal-key run.
                fake.timeout_from[self.REL] = 4
                fake.server_errors[self.REL] = ("key-run-too-long", 4)
            return original(url, headers, max_bytes)

        fake._row_page = row_page
        mirror = self.mirror(page_size=4, min_page_size=1)
        run = self.run_of(mirror.sync(OPS, relations=[self.REL]), self.REL)
        self.assertEqual((run["outcome"], run["failure_class"]), ("failed", "source_defect"))
        self.assertIs(run["defer_next_run"], True)
        self.assertEqual(self.fake.limits(self.REL), [4, 4, 2, 3])
        # Not doomed: the committed page stays for the next run to resume.
        self.assertEqual(self.db().execute(
            "SELECT pages FROM sync_progress WHERE relation = ?", (self.REL,)).fetchone(), (1,))
        mirror.close()
        fake._row_page = original
        del fake.timeout_from[self.REL], fake.server_errors[self.REL]
        run = self.run_of(self.mirror(page_size=4).sync(OPS, relations=[self.REL]), self.REL)
        self.assertEqual((run["outcome"], run["resumed"], run["rows_received"]),
                         ("captured", True, 9))

    def test_other_failures_are_not_deferred(self):
        _numbered(self.fake, self.REL, 9)
        self.fake.server_errors[self.REL] = ("internal", None)
        run = self.run_of(self.mirror(page_size=4, min_page_size=1, read_attempts=1)
                          .sync(OPS, relations=[self.REL]), self.REL)
        self.assertEqual(run["failure_class"], "source_defect")
        self.assertIs(run["defer_next_run"], False)

    # Every path that fails a relation in a
    # sync leaves its refresh_failure, a record that cannot be written is reported, and
    # the wait for the write lock is bounded.

    def failure_of(self, report, name):
        return {item["relation"]: item
                for item in report["coverage"]["relations"]}[name]["last_refresh_failure"]

    def test_an_audience_error_fails_a_mirrored_relation_in_coverage(self):
        original = SourceMirror._audience
        armed = []

        def audience(mirror):
            if sys._getframe(1).f_code.co_name == "_sync_one" and armed:
                armed.pop()
                raise MirrorError("source_contract", "audience re-read failed")
            return original(mirror)

        mirror = self.mirror(page_size=4)
        first = mirror.sync(OPS)
        self.assertTrue(first["coverage"]["all_served_complete"])
        armed.append(1)
        with patch.object(SourceMirror, "_audience", audience):
            report = mirror.sync(OPS)
        failed = [r for r in report["runs"] if r["outcome"] != "captured"]
        self.assertEqual(len(failed), 1)
        self.assertIs(failed[0]["failure_recorded"], True)
        # The stored capture of the earlier run no longer counts as complete.
        self.assertEqual(self.failure_of(report, failed[0]["relation"])["error"],
                         "source_contract: audience re-read failed")
        self.assertEqual(report["coverage"]["complete_relation_count"],
                         len(self.fake.catalog) - 1)
        self.assertEqual(report["coverage"]["failed_count"], 1)

    def test_a_store_busy_relation_fails_in_coverage(self):
        original = SourceMirror._apply_page
        busy = []

        def locked(mirror, relation, *args, **kwargs):
            if relation == "sage.po_header" and busy:
                raise sqlite3.OperationalError("database is locked")
            return original(mirror, relation, *args, **kwargs)

        for workers in (1, 3):
            with self.subTest(workers=workers):
                self.fake = RunawayFake()
                busy[:] = []
                with patch.object(SourceMirror, "_apply_page", locked):
                    mirror = self.mirror(page_size=4)
                    mirror.sync(OPS, workers=workers)
                    busy.append(1)
                    report = mirror.sync(OPS, workers=workers)
                run = self.run_of(report, "sage.po_header")
                self.assertEqual((run["outcome"], run["failure_class"],
                                  run["failure_recorded"]), ("failed", "store_busy", True))
                self.assertTrue(self.failure_of(report, "sage.po_header")["error"]
                                .startswith("mirror_store_busy: "))
                self.assertEqual(report["coverage"]["complete_relation_count"],
                                 len(self.fake.catalog) - 1)
                mirror.close()
                self.path.unlink()

    def test_a_failure_record_that_cannot_be_written_is_reported(self):
        original = SourceMirror._apply_page

        def explode(mirror, relation, *args, **kwargs):
            if relation == "sage.po_header":
                raise KeyError("C-secret-row-value")
            return original(mirror, relation, *args, **kwargs)

        mirror = self.mirror(page_size=4)
        self.db().execute("CREATE TRIGGER no_failure BEFORE INSERT ON refresh_failure "
                          "BEGIN SELECT RAISE(ABORT, 'refused'); END")
        with patch.object(SourceMirror, "_apply_page", explode):
            report = mirror.sync(OPS, workers=2)
        run = self.run_of(report, "sage.po_header")
        self.assertEqual((run["outcome"], run["failure_class"]), ("failed", "internal_error"))
        self.assertIs(run["failure_recorded"], False)
        self.assertEqual(run["record_error"], "IntegrityError")
        captured = [r for r in report["runs"] if r["relation"] != "sage.po_header"]
        self.assertTrue(all(r["outcome"] == "captured" for r in captured))
        self.assertNotIn("failure_recorded", captured[0])

    def locking_mirror(self, hold_seconds, **options):
        """A mirror (with ``options``) whose second page of REL is followed by another
        writer holding the mirror file for ``hold_seconds``; returns it and the release
        timer."""
        _numbered(self.fake, self.REL, 9)
        fetch = self.fake
        holder = []
        release = []

        def fetch_and_lock(url, headers, *, max_bytes):
            page = fetch(url, headers, max_bytes=max_bytes)
            if (url.split("?")[0].endswith(f"/{self.REL}/rows") and "cursor=" in url
                    and not holder):
                other = sqlite3.connect(str(self.path), isolation_level=None,
                                        check_same_thread=False)
                other.execute("BEGIN IMMEDIATE")
                holder.append(other)
                timer = threading.Timer(hold_seconds,
                                        lambda: (other.rollback(), other.close()))
                timer.start()
                release.append(timer)
                self.addCleanup(timer.join)
            return page

        client = WorkbenchReadApiClient(BASE, KEY, fetch=fetch_and_lock)
        mirror = SourceMirror(self.path, client, ALL, clock=self.clock, page_size=3,
                              retry_delay_seconds=0, **options)
        self.addCleanup(mirror.close)
        return mirror, release

    def test_a_lock_held_past_the_allowance_fails_the_relation_as_store_busy(self):
        mirror, release = self.locking_mirror(1.5)
        with patch("integrations.source_mirror.LOCK_WAIT_ALLOWANCE_SECONDS", 0.3,
                   create=True):
            report = mirror.sync(OPS, relations=[self.REL])
        run = report["runs"][0]
        self.assertEqual((run["outcome"], run["failure_class"]), ("failed", "store_busy"))
        # It stopped at its allowance instead of waiting the holder out.
        self.assertGreaterEqual(run["lock_wait_seconds"], 0.25)
        self.assertLess(run["lock_wait_seconds"], 1.2)
        self.assertTrue(self.failure_of(report, self.REL)["error"]
                        .startswith("mirror_store_busy: "))
        self.assertEqual(self.db().execute(
            "SELECT pages FROM sync_progress WHERE relation = ?", (self.REL,)).fetchone(), (1,))
        release[0].join()
        run = mirror.sync(OPS, relations=[self.REL])["runs"][0]
        self.assertEqual((run["outcome"], run["resumed"], run["rows_received"]),
                         ("captured", True, 9))

    def test_past_the_run_credit_a_lock_wait_counts_against_the_budget(self):
        mirror, _ = self.locking_mirror(1.5)
        with patch("integrations.source_mirror.LOCK_WAIT_RUN_CAP_SECONDS", 0.0,
                   create=True):
            run = mirror.sync(OPS, relations=[self.REL], budget_seconds=0.5)["runs"][0]
        self.assertEqual((run["outcome"], run["failure_class"]),
                         ("budget_exhausted", "budget"))
        # The charged wait ended with the budget, not with the holder.
        self.assertLess(run["lock_wait_seconds"], 1.2)
        self.assertEqual(self.db().execute(
            "SELECT pages FROM sync_progress WHERE relation = ?", (self.REL,)).fetchone(), (1,))

    # Review follow-up (task mirror-followup): a failure record that cannot be written
    # still fails the relation in coverage, waits only seconds for the write lock, keeps
    # the refresh's own error, and concurrent lock waits never share one run credit.

    def test_an_unwritten_failure_record_still_fails_the_relation_in_coverage(self):
        _numbered(self.fake, self.REL, 9)
        first = self.mirror(page_size=3)
        self.assertTrue(self.covered(first.sync(OPS, relations=[self.REL]))["complete"])
        first.close()
        self.db().execute("CREATE TRIGGER no_failure BEFORE INSERT ON refresh_failure "
                          "BEGIN SELECT RAISE(ABORT, 'refused'); END")
        report = self.mirror(page_size=3, max_rows_per_relation=5).sync(
            OPS, relations=[self.REL], full=True)
        run = report["runs"][0]
        # The refresh's own failure class, not the record's.
        self.assertEqual((run["outcome"], run["failure_class"]), ("failed", "row_bound"))
        self.assertEqual((run["failure_recorded"], run["record_error"]),
                         (False, "IntegrityError"))
        item = self.covered(report)
        # The stored capture stands and nothing is staged, yet the run failed.
        self.assertIsNone(item["sync_in_progress"])
        self.assertIsNone(item["last_refresh_failure"])
        self.assertEqual(item["unrecorded_refresh_failure"], "IntegrityError")
        self.assertIs(item["complete"], False)
        self.assertEqual(report["coverage"]["failed_count"], 1)

    def covered(self, report):
        return {item["relation"]: item
                for item in report["coverage"]["relations"]}[self.REL]

    def test_a_failure_record_waits_only_seconds_for_a_held_write_lock(self):
        # The retry after the workers waits the mirror's own (here short) busy timeout.
        mirror, release = self.locking_mirror(3.0, _busy_timeout_ms=300)
        started = time.monotonic()
        with patch("integrations.source_mirror.LOCK_WAIT_ALLOWANCE_SECONDS", 0.3), \
                patch("integrations.source_mirror.FAILURE_RECORD_WAIT_SECONDS", 0.2,
                      create=True):
            report = mirror.sync(OPS, relations=[self.REL])
        elapsed = time.monotonic() - started
        run = report["runs"][0]
        self.assertEqual((run["outcome"], run["failure_class"]), ("failed", "store_busy"))
        self.assertEqual((run["failure_recorded"], run["record_error"]),
                         (False, "OperationalError"))
        # Allowance, the record's bound and the retry, not until the holder lets go.
        self.assertLess(elapsed, 1.5)
        self.assertTrue(release[0].is_alive())
        self.assertIs(self.covered(report)["complete"], False)
        self.assertEqual(self.covered(report)["unrecorded_refresh_failure"],
                         "OperationalError")

    def test_a_failure_record_blocked_during_the_run_is_written_after_it(self):
        mirror, release = self.locking_mirror(1.0)
        with patch("integrations.source_mirror.LOCK_WAIT_ALLOWANCE_SECONDS", 0.3), \
                patch("integrations.source_mirror.FAILURE_RECORD_WAIT_SECONDS", 0.1,
                      create=True):
            report = mirror.sync(OPS, relations=[self.REL])
        run = report["runs"][0]
        self.assertEqual((run["failure_class"], run["failure_recorded"]),
                         ("store_busy", True))
        self.assertNotIn("record_error", run)
        self.assertIsNone(self.covered(report)["unrecorded_refresh_failure"])
        # Written, so another process sees it too.
        other = self.mirror(page_size=3)
        item = {i["relation"]: i for i in other.coverage(OPS)["relations"]}[self.REL]
        self.assertTrue(item["last_refresh_failure"]["error"]
                        .startswith("mirror_store_busy: "))

    def test_an_unwritten_failure_stays_failed_in_later_syncs_and_coverage(self):
        # A later sync of another relation and coverage() on the
        # same mirror must not count the failed relation complete.
        other_rel = "public.app_other"
        _numbered(self.fake, self.REL, 9)
        _numbered(self.fake, other_rel, 3)
        first = self.mirror(page_size=3)
        first.sync(OPS, relations=[self.REL, other_rel])
        first.close()
        self.db().execute("CREATE TRIGGER no_failure BEFORE INSERT ON refresh_failure "
                          "BEGIN SELECT RAISE(ABORT, 'refused'); END")
        mirror = self.mirror(page_size=3, max_rows_per_relation=5)
        for workers in (1, 2):
            with self.subTest(workers=workers):
                run = self.run_of(mirror.sync(OPS, relations=[self.REL, other_rel],
                                              full=True, workers=workers), self.REL)
                self.assertEqual((run["failure_class"], run["record_error"]),
                                 ("row_bound", "IntegrityError"))
                later = mirror.sync(OPS, relations=[other_rel])
                self.assertEqual(later["runs"][0]["outcome"], "captured")
                for coverage in (later["coverage"], mirror.coverage(OPS)):
                    item = {i["relation"]: i for i in coverage["relations"]}[self.REL]
                    self.assertIs(item["complete"], False)
                    self.assertEqual(item["unrecorded_refresh_failure"], "IntegrityError")
                    self.assertEqual(coverage["failed_count"], 1)
        # A successful refresh clears it.
        self.db().execute("DROP TRIGGER no_failure")
        mirror.close()
        mirror = self.mirror(page_size=3)
        mirror._unrecorded[self.REL] = "IntegrityError"
        self.assertEqual(mirror.sync(OPS, relations=[self.REL], workers=2)["runs"][0]
                         ["outcome"], "captured")
        item = {i["relation"]: i for i in mirror.coverage(OPS)["relations"]}[self.REL]
        self.assertIs(item["complete"], True)

    def test_a_blocked_failure_record_keeps_the_refresh_error_and_its_status(self):
        _numbered(self.fake, self.REL, 3)
        self.fake.server_errors[self.REL] = ("internal", None)
        fetch = self.fake

        def lock_then_fetch(url, headers, *, max_bytes):
            if url.split("?")[0].endswith(f"/{self.REL}/rows"):
                other = sqlite3.connect(str(self.path), isolation_level=None,
                                        check_same_thread=False)
                other.execute("BEGIN IMMEDIATE")
                timer = threading.Timer(3.0, lambda: (other.rollback(), other.close()))
                timer.start()
                self.addCleanup(timer.join)
            return fetch(url, headers, max_bytes=max_bytes)

        client = WorkbenchReadApiClient(BASE, KEY, fetch=lock_then_fetch)
        mirror = SourceMirror(self.path, client, ALL, clock=self.clock, page_size=3,
                              min_page_size=3, read_attempts=1, retry_delay_seconds=0,
                              _busy_timeout_ms=500)
        self.addCleanup(mirror.close)
        with patch("integrations.source_mirror.FAILURE_RECORD_WAIT_SECONDS", 0.2,
                   create=True), \
                self.assertRaises(MirrorError) as raised:
            mirror.refresh(OPS, self.REL)
        self.assertEqual((raised.exception.code, raised.exception.status,
                          raised.exception.upstream_code),
                         ("source_unavailable", 500, "internal"))
        self.assertEqual(raised.exception.failure_record, "OperationalError")

    def test_concurrent_lock_waits_never_spend_the_same_run_credit(self):
        names = [f"public.app_credit_{index}" for index in range(2)]
        for name in names:
            _numbered(self.fake, name, 6)
        fetch = self.fake
        barrier = threading.Barrier(2, timeout=10)
        guard = threading.Lock()
        holder = []

        def fetch_and_lock(url, headers, *, max_bytes):
            page = fetch(url, headers, max_bytes=max_bytes)
            if url.split("?")[0].endswith("/rows") and "cursor=" not in url:
                # Both workers have read page 1; another writer then holds the file
                # while both wait for it at the same time.
                barrier.wait()
                with guard:
                    if not holder:
                        other = sqlite3.connect(str(self.path), isolation_level=None,
                                                check_same_thread=False)
                        other.execute("BEGIN IMMEDIATE")
                        holder.append(other)
                        timer = threading.Timer(3.0,
                                                lambda: (other.rollback(), other.close()))
                        timer.start()
                        self.addCleanup(timer.join)
                barrier.wait()
            return page

        client = WorkbenchReadApiClient(BASE, KEY, fetch=fetch_and_lock)
        mirror = SourceMirror(self.path, client, ALL, clock=self.clock, page_size=3,
                              retry_delay_seconds=0)
        self.addCleanup(mirror.close)
        with patch("integrations.source_mirror.LOCK_WAIT_RUN_CAP_SECONDS", 1.5), \
                patch("integrations.source_mirror.FAILURE_RECORD_WAIT_SECONDS", 0.1,
                      create=True):
            report = mirror.sync(OPS, relations=names, budget_seconds=0.6, workers=2)
        self.assertEqual([run["outcome"] for run in report["runs"]],
                         ["budget_exhausted", "budget_exhausted"])
        short, long = sorted(run["lock_wait_seconds"] for run in report["runs"])
        # One worker had the whole 1.5 s credit on top of the budget; the other had
        # none left, so its wait ended with the 0.6 s budget.
        self.assertGreaterEqual(long, 1.4)
        self.assertLess(short, 1.0)


class ResumeAcrossRunsTests(MirrorTestCase):
    """A budget-cut traversal continues in the next run (a new process on the same
    state dir) from its last committed page; nothing already read is read again."""

    REL = "public.app_resume"

    def setUp(self):
        super().setUp()
        self.fake = RunawayFake()

    def test_the_next_run_continues_from_the_committed_cursor(self):
        _numbered(self.fake, self.REL, 9)
        ticks = iter(range(10_000))
        first = self.mirror(page_size=3, monotonic=lambda: next(ticks))
        report = first.sync(OPS, relations=[self.REL], budget_seconds=3)
        self.assertEqual(report["runs"][0]["outcome"], "budget_exhausted")
        read_before = len(self.fake.limits(self.REL))
        self.assertGreaterEqual(read_before, 1)
        first.close()
        second = self.mirror(page_size=3)
        run = second.sync(OPS, relations=[self.REL])["runs"][0]
        self.assertEqual(run["outcome"], "captured")
        self.assertTrue(run["resumed"])
        resumed = self.fake.requests[read_before:]
        self.assertIsNotNone(resumed[0][2])        # starts at a stored cursor, not page 1
        self.assertEqual(read_before + len(resumed), 3)
        self.assertEqual(run["rows_received"], 9)


class ParallelSyncTests(MirrorTestCase):
    """Relations are read in parallel inside the API's connection limit (wb_api: 5);
    each relation still pages in order on its own fenced traversal."""

    def setUp(self):
        super().setUp()
        self.fake = RunawayFake()
        for index in range(6):
            _numbered(self.fake, f"public.app_par_{index}", 7)

    def test_workers_capture_the_same_mirror_concurrently_and_bounded(self):
        self.fake.delay = 0.02
        mirror = self.mirror(page_size=3)
        report = mirror.sync(OPS, workers=3)
        self.assertEqual({run["outcome"] for run in report["runs"]}, {"captured"})
        self.assertEqual([run["relation"] for run in report["runs"]],
                         sorted(self.fake.catalog))
        self.assertTrue(report["coverage"]["all_served_complete"])
        self.assertLessEqual(self.fake.max_inflight, 3)
        self.assertGreaterEqual(self.fake.max_inflight, 2)
        self.assertEqual(report["workers"], 3)
        sequential_path = Path(self.directory.name) / "sequential.sqlite"
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        with SourceMirror(sequential_path, client, ALL, clock=self.clock,
                          page_size=3) as sequential:
            expected = sequential.sync(OPS)["coverage"]
        for field in ("rows_current_total", "complete_relation_count"):
            self.assertEqual(report["coverage"][field], expected[field])

    def test_workers_are_bounded_by_the_service_connection_limit(self):
        mirror = self.mirror()
        for bad in (0, 5, True, 2.0):
            with self.assertRaises(MirrorError) as raised:
                mirror.sync(OPS, workers=bad)
            self.assertEqual(raised.exception.code, "invalid_workers")


if __name__ == "__main__":
    unittest.main()
