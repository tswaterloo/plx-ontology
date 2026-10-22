"""Mirror completeness: full sync of every served relation, resumable
paging, Retry-After, key-only absence scans, batch citation resolve, SQL-side
filters, lineage and per-relation completeness counts.

Every test runs the real ``WorkbenchReadApiClient`` over the injected fake
Workbench transport of ``test_source_mirror`` (no network); the fake mirrors
Portal's keyset paging (petralabx/plx-customer-portal
services/workbench-api/src/core/query.mjs).
"""

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from integrations.http import SourceHttpError, SourceUnavailable, get_json, retry_after_seconds
from integrations.source_mirror import (LINEAGES, MirrorError, SourceMirror,
    _matches, _sql_prefilter, full_coverage_probe, load_ingest_config, make_citation)
from integrations.tests.test_source_mirror import (ALL, BASE, KEY, OPS, OUTSIDER, FakeWorkbench,
    MirrorTestCase)
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor


def generated_relation(fake, name, count, *, since=True, start=0):
    """Add a relation with ``count`` rows (id, code, qty, updated_at) to the fake."""
    columns = ["id", "code", "qty", "updated_at"]
    fake.catalog[name] = {"columns": columns, "key": ["id"], "domain": "orders",
                          "since": ({"column": "updated_at", "mode": "timestamptz"}
                                    if since else None)}
    fake.tables[name] = [{"id": start + index, "code": f"C-{start + index}",
                          "qty": str(index % 7), "updated_at": "2026-09-28T00:00:00Z"}
                         for index in range(count)]


class ScriptedFake(FakeWorkbench):
    """The fake Workbench plus scripted row-request failures and a request log."""

    def __init__(self):
        super().__init__()
        self.row_errors = []      # exceptions raised by the next row requests, in order
        self.fail_after = {}      # relation -> row requests allowed before failing
        self.timeout_above = None  # statement-timeout for limit above this
        self.reject_cursors = set()
        self.unhealthy_after = {}  # relation -> row requests after which health fails
        self.queries = []

    def __call__(self, url, headers, *, max_bytes):
        parts = urlsplit(url)
        if parts.path.endswith("/rows"):
            query = parse_qs(parts.query)
            name = parts.path.split("/")[3]
            self.queries.append((name, query))
            if self.row_errors:
                raise self.row_errors.pop(0)
            if name in self.fail_after:
                if self.fail_after[name] <= 0:
                    raise RuntimeError("connection reset")
                self.fail_after[name] -= 1
            if (self.timeout_above is not None and
                    int(query["limit"][0]) > self.timeout_above):
                raise SourceHttpError("read-only source GET failed: HTTPError", status=503,
                                      error_code="statement-timeout")
            if query.get("cursor", [None])[0] in self.reject_cursors:
                # Rejected once (e.g. a key change upstream), accepted when re-read.
                self.reject_cursors.discard(query["cursor"][0])
                raise SourceHttpError("read-only source GET failed: HTTPError", status=400,
                                      error_code="invalid-cursor")
            result = super().__call__(url, headers, max_bytes=max_bytes)
            if name in self.unhealthy_after:
                self.unhealthy_after[name] -= 1
                if self.unhealthy_after[name] <= 0:
                    del self.unhealthy_after[name]
                    self.healthy = False
            return result
        return super().__call__(url, headers, max_bytes=max_bytes)

    def queries_for(self, name):
        return [query for relation, query in self.queries if relation == name]


class SyncTestCase(MirrorTestCase):
    def setUp(self):
        super().setUp()
        self.fake = ScriptedFake()
        self.sleeps = []

    def mirror(self, audience=None, **options):
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        options.setdefault("sleep", self.sleeps.append)
        options.setdefault("retry_delay_seconds", 0)
        mirror = SourceMirror(self.path, client, ALL if audience is None else audience,
                              clock=self.clock, **options)
        self.addCleanup(mirror.close)
        return mirror

    def covered(self, mirror, name):
        return {item["relation"]: item for item in mirror.coverage(OPS)["relations"]}[name]

    def db(self):
        connection = sqlite3.connect(str(self.path))
        self.addCleanup(connection.close)
        return connection


class FullSyncTests(SyncTestCase):
    def test_degraded_api_syncs_the_rest_and_names_the_relation_it_stopped_serving(self):
        """Live 2026-10-06: health ``ok`` false listing relations the API no longer serves.
        The others sync; a listed relation the catalog still shows fails as
        ``relation_unavailable`` on its first 404, never retried and never an outage."""
        generated_relation(self.fake, "public.gen_ok", 3)
        generated_relation(self.fake, "public.gen_gone", 3)
        gone_reads = []

        def fetch(url, headers, *, max_bytes):
            path = urlsplit(url).path
            if path == "/v1/relations/public.gen_gone/rows":
                gone_reads.append(url)
                raise SourceHttpError("read-only source GET failed: HTTPError", status=404,
                                      error_code="relation-unavailable")
            packet = self.fake(url, headers, max_bytes=max_bytes)
            if path == "/v1/health":
                packet.update(ok=False, unavailable=[{"name": "public.gen_gone",
                                                      "reason": "relation not found"}])
            return packet

        mirror = SourceMirror(self.path, WorkbenchReadApiClient(BASE, KEY, fetch=fetch), ALL,
                              clock=self.clock, sleep=self.sleeps.append,
                              retry_delay_seconds=0)
        self.addCleanup(mirror.close)
        runs = {run["relation"]: run for run in mirror.sync(
            OPS, relations=["public.gen_ok", "public.gen_gone"])["runs"]}
        self.assertEqual(runs["public.gen_ok"]["outcome"], "captured")
        self.assertEqual(runs["public.gen_gone"]["outcome"], "failed")
        self.assertTrue(runs["public.gen_gone"]["error"].startswith(
            "relation_unavailable: workbench API relation public.gen_gone unavailable"),
            runs["public.gen_gone"]["error"])
        self.assertEqual((len(gone_reads), self.sleeps), (1, []))

    def test_sync_captures_every_served_relation_with_counts(self):
        for index in range(40):
            generated_relation(self.fake, f"public.gen_rel_{index:02d}", index * 3 + 1)
        self.fake.catalog["public.not_served"] = {"columns": ["id"], "key": None,
                                                  "domain": "orders", "since": None}
        self.fake.tables["public.not_served"] = []
        mirror = self.mirror(page_size=4)
        report = mirror.sync(OPS)
        coverage = report["coverage"]
        served = [name for name, entry in self.fake.catalog.items() if entry["key"]]
        self.assertEqual(coverage["catalog_relation_count"], len(self.fake.catalog))
        self.assertEqual(coverage["served_relation_count"], len(served))
        self.assertEqual(coverage["unavailable_relation_count"], 1)
        self.assertEqual(coverage["complete_relation_count"], len(served))
        self.assertTrue(coverage["all_served_complete"])
        by_name = {item["relation"]: item for item in coverage["relations"]}
        for name in served:
            self.assertEqual(by_name[name]["rows_current"], len(self.fake.tables[name]), name)
            self.assertTrue(by_name[name]["complete"], name)
            self.assertIsNone(by_name[name]["sync_in_progress"])
        # Paging: 40 rows at 4 per page is 10 pages for gen_rel_13.
        self.assertEqual(by_name["public.gen_rel_13"]["latest_capture"]["pages"], 10)
        self.assertEqual(len(self.fake.queries_for("public.gen_rel_13")), 10)
        self.assertEqual({run["outcome"] for run in report["runs"]},
                         {"captured", "unavailable"})
        self.assertFalse(by_name["public.not_served"]["served"])
        self.assertFalse(coverage["atomic_upstream_snapshot"])
        self.assertFalse(coverage["deletions_proven"])
        # A second sync refreshes incrementally where since allows.
        self.clock.advance(60)
        modes = {run["relation"]: run["mode"] for run in mirror.sync(OPS)["runs"]
                 if run["outcome"] == "captured"}
        self.assertEqual(modes["public.gen_rel_05"], "incremental")
        self.assertEqual(modes["public.customer"], "full_traversal")
        # full=True forces full traversals everywhere.
        modes = {run["mode"] for run in mirror.sync(OPS, full=True)["runs"]
                 if run["outcome"] == "captured"}
        self.assertEqual(modes, {"full_traversal"})

    def test_sync_resumes_from_the_last_committed_page_after_a_failure(self):
        generated_relation(self.fake, "public.big", 10)
        mirror = self.mirror(page_size=3)
        self.fake.fail_after["public.big"] = 2
        first = {run["relation"]: run for run in
                 mirror.sync(OPS, relations=["public.big", "public.customer"])["runs"]}
        self.assertEqual(first["public.big"]["outcome"], "failed")
        self.assertTrue(first["public.big"]["error"].startswith("source_unavailable"))
        self.assertEqual(first["public.customer"]["outcome"], "captured")
        coverage = {item["relation"]: item for item in mirror.coverage(OPS)["relations"]}
        progress = coverage["public.big"]["sync_in_progress"]
        self.assertEqual((progress["mode"], progress["pages"], progress["rows_received"]),
                         ("lazy_fill", 2, 6))
        self.assertFalse(coverage["public.big"]["complete"])
        self.assertFalse(coverage["public.big"]["mirrored"])
        # A relation never completed is not served from its partial pages.
        with self.assertRaises(MirrorError):
            mirror.rows(OPS, "public.big")
        stored_cursor = self.db().execute(
            "SELECT cursor FROM sync_progress WHERE relation = 'public.big'").fetchone()[0]
        del self.fake.fail_after["public.big"]
        before = len(self.fake.queries_for("public.big"))
        second = {run["relation"]: run for run in
                  mirror.sync(OPS, relations=["public.big"])["runs"]}
        self.assertEqual(second["public.big"]["outcome"], "captured")
        self.assertTrue(second["public.big"]["resumed"])
        self.assertEqual(second["public.big"]["pages"], 4)
        resumed = self.fake.queries_for("public.big")[before:]
        self.assertEqual(len(resumed), 2)
        self.assertEqual(resumed[0]["cursor"], [stored_cursor])
        result = mirror.rows(OPS, "public.big", max_staleness=3600)
        self.assertEqual(result["total_matched"], 10)
        # One capture (the first one, begun before public.customer's) holds the
        # whole resumed traversal.
        self.assertEqual({item["capture_id"] for item in result["rows"]}, {1})
        self.assertTrue(self.covered(mirror, "public.big")["complete"])
        states = dict(self.db().execute("SELECT capture_id, state FROM capture_state"))
        self.assertEqual(states[1], "complete")

    def test_a_failure_after_the_last_page_resumes_into_finish(self):
        """Health failing after the last committed page must not
        wedge the relation; the next sync (also full=True) finishes without re-reading."""
        generated_relation(self.fake, "public.big", 6)
        mirror = self.mirror(page_size=2)
        self.fake.unhealthy_after["public.big"] = 3
        run = mirror.sync(OPS, relations=["public.big"])["runs"][0]
        self.assertEqual(run["outcome"], "failed")
        self.assertIn("health", run["error"])
        self.assertEqual(self.covered(mirror, "public.big")["sync_in_progress"]["pages"], 3)
        reads = len(self.fake.queries_for("public.big"))
        self.fake.healthy = True
        run = mirror.sync(OPS, relations=["public.big"], full=True)["runs"][0]
        self.assertEqual((run["outcome"], run["resumed"], run["pages"]),
                         ("captured", True, 3))
        self.assertEqual(len(self.fake.queries_for("public.big")), reads)
        self.assertTrue(self.covered(mirror, "public.big")["complete"])
        self.assertEqual(mirror.rows(OPS, "public.big", max_staleness=3600)["total_matched"], 6)
        # And a scan interrupted the same way also finishes on the next call.
        self.fake.tables["public.big"].pop()
        self.clock.advance(5)
        self.fake.unhealthy_after["public.big"] = 3
        with self.assertRaises(MirrorError):
            mirror.scan_absence(OPS, "public.big")
        self.fake.healthy = True
        reads = len(self.fake.queries_for("public.big"))
        scan = mirror.scan_absence(OPS, "public.big")
        self.assertEqual((scan["resumed"], scan["keys_missing"]), (1, 1))
        self.assertEqual(len(self.fake.queries_for("public.big")), reads)

    def test_an_interrupted_retraversal_is_never_served(self):
        """Pages of an unfinished re-read are staged, not served."""
        generated_relation(self.fake, "public.big", 6, since=False)
        mirror = self.mirror(page_size=2)
        before = self.by_id(mirror.rows(OPS, "public.big"))
        self.fake.update("public.big", 0, code="NEW-0")
        self.fake.fail_after["public.big"] = 1
        self.clock.advance(1000)
        stale = mirror.rows(OPS, "public.big")
        self.assertTrue(stale["freshness"]["refresh_error"])
        self.assertEqual(stale["freshness"]["sync_in_progress"]["pages"], 1)
        self.assertIn("staged, not served", " ".join(stale["freshness"]["caveats"]))
        rows = self.by_id(stale)
        self.assertEqual(rows[0]["row"]["code"], "C-0")
        self.assertEqual({item["capture_id"] for item in rows.values()}, {1})
        current = mirror.resolve_citation(before[0]["citation"], OPS)
        self.assertEqual((current["status"], current["row"]["code"]), ("current", "C-0"))
        self.assertEqual(mirror.resolve(OPS, "NEW-0")["candidate_count"], 0)
        del self.fake.fail_after["public.big"]
        rows = self.by_id(mirror.rows(OPS, "public.big", max_staleness=0))
        self.assertEqual(rows[0]["row"]["code"], "NEW-0")
        self.assertEqual(mirror.resolve_citation(before[0]["citation"], OPS)["status"],
                         "superseded")

    def test_a_column_drop_is_not_served_half_applied(self):
        generated_relation(self.fake, "public.big", 6, since=False)
        mirror = self.mirror(page_size=2)
        mirror.rows(OPS, "public.big")
        self.fake.catalog["public.big"]["columns"].remove("qty")
        for row in self.fake.tables["public.big"]:
            del row["qty"]
        self.fake.fail_after["public.big"] = 1
        self.clock.advance(1000)
        stale = mirror.rows(OPS, "public.big", where=[["qty", "is_null", True]])
        self.assertIn("qty", stale["columns"])
        self.assertEqual(stale["total_matched"], 0)
        del self.fake.fail_after["public.big"]
        fresh = mirror.rows(OPS, "public.big", max_staleness=0)
        self.assertNotIn("qty", fresh["columns"])
        self.assertEqual(fresh["total_matched"], 6)

    def test_budget_stops_between_pages_and_the_next_call_continues(self):
        generated_relation(self.fake, "public.big", 9)
        ticks = iter(range(1000))
        mirror = self.mirror(page_size=3, monotonic=lambda: next(ticks))
        # Deadline = start + 3 ticks: two page reads, then the budget ends.
        report = mirror.sync(OPS, relations=["public.big", "public.customer"],
                             budget_seconds=3)
        outcomes = {run["relation"]: run["outcome"] for run in report["runs"]}
        self.assertEqual(outcomes, {"public.big": "budget_exhausted",
                                    "public.customer": "budget_exhausted"})
        progress = {item["relation"]: item for item in
                    report["coverage"]["relations"]}["public.big"]["sync_in_progress"]
        self.assertGreaterEqual(progress["pages"], 1)
        report = mirror.sync(OPS, relations=["public.big", "public.customer"])
        self.assertEqual({run["outcome"] for run in report["runs"]}, {"captured"})
        self.assertTrue(self.covered(mirror, "public.big")["complete"])
        self.assertEqual(self.covered(mirror, "public.big")["rows_current"], 9)
        self.assertEqual(report["coverage"]["rows_current_total"], 11)

    def test_retry_after_is_honoured_on_429_and_503(self):
        mirror = self.mirror(retry_delay_seconds=0.5)
        self.fake.row_errors = [
            SourceHttpError("x", status=429, retry_after_seconds=7.0),
            SourceHttpError("x", status=503, error_code="too-many-connections"),
            SourceHttpError("x", status=503, error_code="too-many-connections")]
        result = mirror.rows(OPS, "public.customer")
        self.assertEqual(result["total_matched"], 2)
        # Retry-After when sent; otherwise backoff from retry_delay_seconds.
        self.assertEqual(self.sleeps, [7.0, 1.0, 2.0])
        self.assertEqual(mirror.throttle_waits, [7.0, 1.0, 2.0])

    def test_throttling_beyond_policy_fails_without_sleeping(self):
        mirror = self.mirror(max_retry_after_seconds=30)
        self.fake.row_errors = [SourceHttpError("x", status=429, retry_after_seconds=600.0)]
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OPS, "public.customer")
        self.assertEqual(caught.exception.code, "source_throttled")
        self.assertIn("Retry-After 600 s", str(caught.exception))
        self.assertEqual(self.sleeps, [])
        mirror = self.mirror(throttle_attempts=2)
        self.fake.row_errors = [SourceHttpError("x", status=503)] * 3
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OPS, "public.customer")
        self.assertEqual(caught.exception.code, "source_throttled")
        self.assertEqual(len(self.sleeps), 1)

    def test_other_client_errors_are_final(self):
        mirror = self.mirror()
        self.fake.row_errors = [SourceHttpError("read-only source GET failed: HTTPError",
                                                status=401, error_code="unauthorized")]
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OPS, "public.customer")
        self.assertEqual(caught.exception.code, "source_unavailable")
        self.assertIn("HTTP 401 unauthorized", str(caught.exception))
        self.assertEqual(len(self.fake.queries_for("public.customer")), 1)
        # A 503 naming a database login failure is not throttling: one request, no wait
        # (WORKBENCH-API.md@12db891 "Database errors").
        mirror = self.mirror()
        self.fake.row_errors = [SourceHttpError("x", status=503,
                                                error_code="database-login-failed")] * 3
        with self.assertRaises(MirrorError) as caught:
            mirror.rows(OPS, "public.customer")
        self.assertEqual(caught.exception.code, "source_unavailable")
        self.assertIn("HTTP 503 database-login-failed", str(caught.exception))
        self.assertEqual(len(self.fake.queries_for("public.customer")), 2)
        self.assertEqual(self.sleeps, [])

    def test_statement_timeout_halves_the_page_until_it_fits(self):
        generated_relation(self.fake, "public.big", 7)
        self.fake.timeout_above = 2
        mirror = self.mirror(page_size=8, min_page_size=1)
        mirror.sync(OPS, relations=["public.big"])
        limits = [query["limit"][0] for query in self.fake.queries_for("public.big")]
        self.assertEqual(limits, ["8", "4", "2", "2", "2", "2"])
        self.assertEqual(self.covered(mirror, "public.big")["rows_current"], 7)
        self.fake.timeout_above = 0
        mirror = self.mirror(page_size=2, min_page_size=2)
        result = mirror.refresh(OPS, "public.big", full=True)
        self.assertTrue(result["refresh_error"].startswith("statement_timeout"))
        self.assertEqual(result["row_count"], 7)  # stored rows are still served, stale

    def test_http_500_halves_the_page_until_it_fits(self):
        """An HTTP 500 on a page is split like a statement
        timeout (same cursor, half the page, down to the floor)."""
        generated_relation(self.fake, "public.big", 7)
        server_error = SourceHttpError("read-only source GET failed: HTTPError", status=500,
                                       error_code="internal")
        self.fake.row_errors = [server_error, server_error]
        mirror = self.mirror(page_size=8, min_page_size=1)
        run = mirror.sync(OPS, relations=["public.big"])["runs"][0]
        self.assertEqual(run["outcome"], "captured", run)
        queries = self.fake.queries_for("public.big")
        self.assertEqual([query["limit"][0] for query in queries], ["8", "4", "2", "2", "2", "2"])
        self.assertEqual([query.get("cursor") for query in queries[:3]], [None, None, None])
        self.assertEqual(self.covered(mirror, "public.big")["rows_current"], 7)
        self.assertEqual(self.sleeps, [])  # a split is immediate, not a backoff

    def test_http_500_at_the_floor_fails_with_its_status_recorded(self):
        generated_relation(self.fake, "public.big", 7)
        server_error = SourceHttpError("read-only source GET failed: HTTPError", status=500,
                                       error_code="internal")
        self.fake.row_errors = [server_error] * 10
        mirror = self.mirror(page_size=4, min_page_size=2, read_attempts=2)
        run = mirror.sync(OPS, relations=["public.big"])["runs"][0]
        self.assertEqual(run["outcome"], "failed")
        self.assertEqual(run["http_status"], 500)
        self.assertTrue(run["error"].startswith("source_unavailable"), run)
        self.assertIn("HTTP 500 internal", run["error"])
        # 4 -> 2 (one split), then the ordinary retry policy at the floor: bounded.
        self.assertEqual([query["limit"][0] for query in self.fake.queries_for("public.big")],
                         ["4", "2", "2"])
        stored = self.db().execute(
            "SELECT error FROM refresh_failure WHERE relation = 'public.big'").fetchone()[0]
        self.assertIn("HTTP 500", stored)

    def test_sync_captures_ordered_relations_first_inside_one_budget(self):
        for name in ("public.a_rel", "public.m_rel", "public.z_rel"):
            generated_relation(self.fake, name, 3)
        mirror = self.mirror()
        runs = mirror.sync(OPS, order=["public.z_rel", "public.not_in_catalog",
                                       "public.m_rel", "public.z_rel"])["runs"]
        names = [run["relation"] for run in runs]
        rest = sorted(set(self.fake.catalog) - {"public.z_rel", "public.m_rel"})
        self.assertEqual(names, ["public.z_rel", "public.m_rel", *rest])
        self.assertEqual({run["outcome"] for run in runs[:2]}, {"captured"})
        # order composes with relations and never adds an unselected relation.
        runs = mirror.sync(OPS, relations=["public.a_rel", "public.m_rel"],
                           order=["public.m_rel", "public.z_rel"])["runs"]
        self.assertEqual([run["relation"] for run in runs], ["public.m_rel", "public.a_rel"])
        # A budget that ends after the ordered relations leaves the rest, not them.
        self.fake.queries.clear()
        mirror = self.mirror(monotonic=lambda: 100.0 if self.fake.queries else 0.0)
        runs = mirror.sync(OPS, order=["public.z_rel"], budget_seconds=15)["runs"]
        self.assertEqual(runs[0]["relation"], "public.z_rel")
        self.assertNotEqual(runs[0]["outcome"], "budget_exhausted")
        self.assertEqual({run["outcome"] for run in runs[1:]}, {"budget_exhausted"})
        with self.assertRaises(MirrorError) as raised:
            mirror.sync(OPS, order="public.z_rel")
        self.assertEqual(raised.exception.code, "invalid_relation")

    def test_a_rejected_cursor_restarts_the_traversal(self):
        generated_relation(self.fake, "public.big", 6)
        mirror = self.mirror(page_size=2)
        self.fake.fail_after["public.big"] = 1
        mirror.sync(OPS, relations=["public.big"])
        stored = self.db().execute(
            "SELECT cursor, capture_id FROM sync_progress").fetchone()
        del self.fake.fail_after["public.big"]
        self.fake.reject_cursors.add(stored[0])
        mirror.sync(OPS, relations=["public.big"])
        self.assertTrue(self.covered(mirror, "public.big")["complete"])
        states = dict(self.db().execute("SELECT capture_id, state FROM capture_state"))
        self.assertEqual(states[stored[1]], "abandoned")
        self.assertEqual(sorted(states.values()), ["abandoned", "complete"])
        reason = self.db().execute("SELECT reason FROM capture WHERE capture_id = ?",
                                   (max(states),)).fetchone()[0]
        self.assertIn("rejected the stored cursor", reason)

    def test_catalog_change_abandons_an_interrupted_traversal(self):
        generated_relation(self.fake, "public.big", 6)
        mirror = self.mirror(page_size=2)
        self.fake.fail_after["public.big"] = 1
        mirror.sync(OPS, relations=["public.big"])
        del self.fake.fail_after["public.big"]
        self.fake.catalog["public.big"]["columns"].append("note")
        for row in self.fake.tables["public.big"]:
            row["note"] = None
        report = mirror.sync(OPS, relations=["public.big"])
        self.assertFalse(report["runs"][0]["resumed"])
        self.assertTrue(self.covered(mirror, "public.big")["complete"])
        self.assertIn("note", mirror.rows(OPS, "public.big")["columns"])

    def test_outsider_syncs_scans_and_counts_nothing(self):
        mirror = self.mirror()
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        requests = len(self.fake.row_requests())
        catalog_reads = self.fake.paths.count("/v1/relations")
        report = mirror.sync(OUTSIDER)
        self.assertEqual(self.fake.paths.count("/v1/relations"), catalog_reads)
        self.assertEqual(report["runs"], [])
        self.assertEqual(report["coverage"]["catalog_relation_count"], 0)
        self.assertEqual(report["coverage"]["relations"], [])
        self.assertEqual(mirror.coverage(OUTSIDER)["rows_current_total"], 0)
        with self.assertRaises(MirrorError) as caught:
            mirror.scan_absence(OUTSIDER, "public.customer")
        self.assertEqual(caught.exception.code, "not_visible")
        batch = mirror.resolve_citations([citation], OUTSIDER)
        self.assertEqual(batch["results"][0]["error"], "not_visible")
        self.assertNotIn("row", batch["results"][0])
        self.assertEqual(len(self.fake.row_requests()), requests)


class AbsenceTests(SyncTestCase):
    def test_missing_row_is_labelled_stays_citable_and_returns(self):
        mirror = self.mirror()
        citation = self.by_id(mirror.rows(OPS, "public.customer"))[8]["citation"]
        removed = [row for row in self.fake.tables["public.customer"] if row["id"] == 8]
        self.fake.tables["public.customer"] = [
            row for row in self.fake.tables["public.customer"] if row["id"] != 8]
        mirror.refresh(OPS, "public.customer", full=True)
        self.assertEqual(sorted(self.by_id(mirror.rows(OPS, "public.customer"))), [7])
        labelled = self.by_id(mirror.rows(OPS, "public.customer", include_missing=True))
        self.assertEqual(sorted(labelled), [7, 8])
        self.assertEqual(labelled[8]["missing_from_source_since"], "2026-09-29T12:00:00.000000Z")
        self.assertNotIn("missing_from_source_since", labelled[7])
        resolved = mirror.resolve_citation(citation, OPS)
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["row"]["code"], "C8")
        self.assertEqual(resolved["missing_from_source_since"], "2026-09-29T12:00:00.000000Z")
        self.assertEqual(resolved["missing_basis"], "full_traversal:2")
        self.assertFalse(resolved["deletions_proven"])
        self.assertEqual(mirror.coverage(OPS)["rows_missing_total"], 1)
        # It reappears unchanged: unmarked, served again, same citation.
        self.fake.tables["public.customer"] += removed
        self.clock.advance(10)
        mirror.refresh(OPS, "public.customer", full=True)
        rows = self.by_id(mirror.rows(OPS, "public.customer"))
        self.assertEqual(rows[8]["citation"], citation)
        self.assertNotIn("missing_from_source_since", rows[8])
        again = mirror.resolve_citation(citation, OPS)
        self.assertNotIn("missing_from_source_since", again)
        self.assertEqual(again["upstream_presence"], "observed_in_latest_full_traversal")
        self.assertEqual(mirror.coverage(OPS)["rows_missing_total"], 0)

    def test_key_only_scan_marks_what_since_cannot_see(self):
        mirror = self.mirror()
        mirror.rows(OPS, "public.assembly_order")
        self.fake.tables["public.assembly_order"] = [
            row for row in self.fake.tables["public.assembly_order"] if row["id"] != 10719]
        self.fake.tables["public.assembly_order"].append(
            {"id": 10999, "nameamo_c": "AO-NEW", "lotno": None, "qtytotal": "1",
             "audittimestampmodified": "2026-09-20T00:00:00"})
        self.clock.advance(1000)
        refreshed = mirror.rows(OPS, "public.assembly_order")
        self.assertEqual(refreshed["freshness"]["refreshed_now"], "incremental")
        self.assertEqual(refreshed["total_matched"], 4)  # deletion invisible to since
        before = len(self.fake.queries_for("public.assembly_order"))
        self.clock.advance(5)
        scan = mirror.scan_absence(OPS, "public.assembly_order")
        scan_queries = self.fake.queries_for("public.assembly_order")[before:]
        self.assertEqual({query["columns"][0] for query in scan_queries}, {"id"})
        self.assertEqual((scan["state"], scan["keys_seen"], scan["keys_missing"],
                          scan["keys_returned"], scan["keys_unmirrored"]),
                         ("complete", 4, 1, 0, 1))
        rows = self.by_id(mirror.rows(OPS, "public.assembly_order", max_staleness=3600))
        self.assertNotIn(10719, rows)
        self.assertNotIn(10999, rows)  # a key scan reads no content
        missing = self.by_id(mirror.rows(OPS, "public.assembly_order", max_staleness=3600,
                                         include_missing=True))[10719]
        self.assertEqual(missing["row"]["nameamo_c"], "AO-26006")
        self.assertEqual(missing["missing_from_source_since"], "2026-09-29T12:16:45.000000Z")
        self.assertEqual(mirror.resolve(OPS, "AO-26006")["candidate_count"], 0)
        resolved = mirror.resolve_citation(missing["citation"], OPS)
        self.assertEqual((resolved["missing_basis"], resolved["upstream_presence"]),
                         ("key_scan:1", "absent_from_latest_key_scan"))
        # Back upstream: the next scan unmarks it.
        self.fake.tables["public.assembly_order"].append(
            {"id": 10719, "nameamo_c": "AO-26006", "lotno": None, "qtytotal": "1509",
             "audittimestampmodified": "2026-09-29T11:00:00.5"})
        self.clock.advance(5)
        scan = mirror.scan_absence(OPS, "public.assembly_order")
        self.assertEqual((scan["keys_missing"], scan["keys_returned"]), (0, 1))
        self.assertIn(10719, self.by_id(mirror.rows(OPS, "public.assembly_order",
                                                    max_staleness=3600)))
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM absence_seen").fetchone()[0], 0)

    def test_sync_with_absence_scan_scans_after_incremental_refreshes(self):
        mirror = self.mirror()
        mirror.sync(OPS, relations=["public.assembly_order", "public.customer"])
        self.fake.tables["public.assembly_order"].pop()
        self.clock.advance(60)
        runs = {run["relation"]: run for run in mirror.sync(
            OPS, relations=["public.assembly_order", "public.customer"],
            absence_scan=True)["runs"]}
        self.assertEqual(runs["public.assembly_order"]["mode"], "incremental")
        self.assertEqual(runs["public.assembly_order"]["absence_scan"]["keys_missing"], 1)
        # A full traversal already sees absence; no scan is added after it.
        self.assertNotIn("absence_scan", runs["public.customer"])

    def test_interrupted_scan_resumes_and_marks_nothing_until_complete(self):
        generated_relation(self.fake, "public.big", 9)
        mirror = self.mirror(page_size=3)
        mirror.sync(OPS, relations=["public.big"])
        self.fake.tables["public.big"] = self.fake.tables["public.big"][1:]
        self.clock.advance(5)
        self.fake.fail_after["public.big"] = 1
        with self.assertRaises(MirrorError):
            mirror.scan_absence(OPS, "public.big")
        self.assertEqual(mirror.coverage(OPS)["rows_missing_total"], 0)
        del self.fake.fail_after["public.big"]
        scan = mirror.scan_absence(OPS, "public.big")
        self.assertEqual((scan["resumed"], scan["pages"], scan["keys_missing"]), (1, 3, 1))

    def test_scan_requires_a_completed_capture(self):
        mirror = self.mirror()
        with self.assertRaises(MirrorError) as caught:
            mirror.scan_absence(OPS, "public.customer")
        self.assertEqual(caught.exception.code, "not_mirrored")


class BatchResolveTests(SyncTestCase):
    def test_batch_equals_single_resolve_for_every_outcome(self):
        mirror = self.mirror()
        rows = self.by_id(mirror.rows(OPS, "public.assembly_order"))
        old = rows[10718]["citation"]
        self.fake.update("public.assembly_order", 10718, lotno="L-9",
                         audittimestampmodified="2026-09-29T11:59:00")
        self.clock.advance(1000)
        mirror.rows(OPS, "public.assembly_order")
        customer = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        shipments = mirror.rows(OPS, "silver.shipments")["rows"][0]["citation"]
        handles = [old, rows[10719]["citation"], customer, shipments, "not a handle",
                   make_citation("public.assembly_order", {"id": 1}, 1, mirror.generation),
                   make_citation("public.assembly_order", {"id": 10719}, 1, "0" * 16),
                   make_citation("public.assembly_order", {"id": 10719}, 99,
                                 mirror.generation),
                   make_citation("public.app_quote", {"id": 1}, 1, mirror.generation)]
        audience = dict(ALL)
        audience["silver.*"] = []
        mirror.set_audience_map(audience)
        batch = mirror.resolve_citations(handles, OPS)
        singles = [mirror.resolve_citation(handle, OPS) for handle in handles]
        self.assertEqual(batch["results"], singles)
        self.assertEqual([item.get("status") or item["error"] for item in batch["results"]],
                         ["superseded", "current", "current", "not_visible", "invalid_handle",
                          "not_found", "capture_unknown", "capture_unknown", "not_found"])
        self.assertEqual(batch["ok_count"], 3)
        self.assertEqual(batch["results"][0]["row"]["lotno"], "L-1")
        with self.assertRaises(MirrorError):
            mirror.resolve_citations("api://x", OPS)

    def test_query_count_does_not_grow_with_the_number_of_handles(self):
        generated_relation(self.fake, "public.big", 400)
        mirror = self.mirror()
        mirror.sync(OPS, relations=["public.big"])
        handles = [item["citation"] for item in
                   mirror.rows(OPS, "public.big", limit=500, max_staleness=3600)["rows"]]
        statements = []
        mirror._db.set_trace_callback(statements.append)
        mirror.resolve_citations(handles[:5], OPS)
        small = len([s for s in statements if s.lstrip().upper().startswith("SELECT")])
        statements.clear()
        mirror.resolve_citations(handles, OPS)
        large = len([s for s in statements if s.lstrip().upper().startswith("SELECT")])
        mirror._db.set_trace_callback(None)
        self.assertEqual(small, large)

    def test_five_thousand_handles_resolve_in_under_three_seconds(self):
        generated_relation(self.fake, "public.big", 5200)
        mirror = self.mirror()
        mirror.sync(OPS, relations=["public.big"])
        handles = [make_citation("public.big", {"id": index}, 1, mirror.generation)
                   for index in range(5000)]
        started = time.perf_counter()
        batch = mirror.resolve_citations(handles, OPS)
        elapsed = time.perf_counter() - started
        self.assertEqual(batch["ok_count"], 5000)
        self.assertEqual(batch["results"][4321]["row"]["code"], "C-4321")
        self.assertEqual(batch["results"][4321]["lineage"], "fm_bronze")
        self.assertLess(elapsed, 3.0)


class SqlFilterTests(SyncTestCase):
    CELLS = [None, 0, 5, -5, 12, 2 ** 62, 5.0, 5.5, "5", "05", "+5", "5.0", "abc", "Abc",
             "abcdef", "", "true", True, False, {"a": 1}, ["x"], "2026-09-29", "9" * 25,
             int("9" * 19)]

    def test_sql_prefilter_never_drops_an_exact_match(self):
        values = [0, 5, -5, 5.0, 5.5, "5", "05", "+5", "5.0", "abc", "ab", "true", "false",
                  '{"a":1}', '["x"]', "", "9" * 25, "9" * 19, int("9" * 19), 2 ** 70]
        predicates = ([(op, value) for op in ("eq", "gte", "lte") for value in values] +
                      [("prefix", value) for value in values
                       if isinstance(value, str) and value] +
                      [("in", [5, "abc", 5.5]), ("in", ["true", "05"]), ("is_null", True),
                       ("is_null", False)])
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE t (n INTEGER, row_json TEXT)")
        rows = [{"c": cell} for cell in self.CELLS] + [{}]
        db.executemany("INSERT INTO t VALUES (?, ?)",
                       [(index, json.dumps(row)) for index, row in enumerate(rows)])
        for op, value in predicates:
            clause, params = _sql_prefilter("c", op, value)
            kept = {n for (n,) in db.execute(f"SELECT n FROM t WHERE {clause}", params)}
            exact = {index for index, row in enumerate(rows) if _matches(row, "c", op, value)}
            self.assertLessEqual(exact, kept, (op, value))

    def test_rows_filter_in_sql_and_return_exactly_the_full_scan_result(self):
        generated_relation(self.fake, "public.big", 60)
        mirror = self.mirror()
        mirror.sync(OPS, relations=["public.big"])
        statements = []
        mirror._db.set_trace_callback(statements.append)
        result = mirror.rows(OPS, "public.big", where=[["code", "eq", "C-17"]],
                             max_staleness=3600)
        mirror._db.set_trace_callback(None)
        self.assertEqual([item["key"]["id"] for item in result["rows"]], [17])
        self.assertTrue(any("json_extract(row_json" in s and "FROM row_current" in s
                            for s in statements))
        for where in ([["qty", "gte", 5]], [["code", "prefix", "C-1"]],
                      [["qty", "in", ["1", 2]]], [["id", "eq", "17"]],
                      [["code", "is_null", False], ["qty", "lte", "2"]]):
            filtered = mirror.rows(OPS, "public.big", where=where, limit=500,
                                   max_staleness=3600)
            everything = mirror.rows(OPS, "public.big", limit=500, max_staleness=3600)
            expected = [item["key"]["id"] for item in everything["rows"]
                        if all(_matches(self.fake_row(item), *clause) for clause in where)]
            self.assertEqual([item["key"]["id"] for item in filtered["rows"]], expected, where)

    @staticmethod
    def fake_row(item):
        return item["row"]


class LineageTests(SyncTestCase):
    def test_bundled_ingest_config_is_valid(self):
        config = load_ingest_config()
        self.assertEqual(config["sync"]["page_size"], 5000)
        self.assertTrue({rule["lineage"] for rule in config["lineage_rules"]} <= LINEAGES)
        for bad in ({"schema": "x"}, {"schema": "plx-source-mirror-ingest/v1",
                                       "sync": {"page_size": 9000}},
                    {"schema": "plx-source-mirror-ingest/v1",
                     "lineage_rules": [{"id": "a", "relations": ["public.*"],
                                        "lineage": "production"}]}):
            with self.assertRaises(ValueError):
                load_ingest_config(bad)

    def test_rows_citations_and_coverage_carry_declared_lineage(self):
        self.fake.tables["public.app_sales_order"] += [
            {"id": 3, "order_number": "OR3", "po_reference": "PO-3", "total_amount": "1",
             "custom_fields": {"sageOrdUniq": " 70123 "}, "updated_at": "2026-09-29T09:00:00"},
            {"id": 4, "order_number": "OR4", "po_reference": "PO-4", "total_amount": "1",
             "custom_fields": {"source": "gold-so-bridge", "fmId": "9"},
             "updated_at": "2026-09-29T09:00:00"},
            {"id": 5, "order_number": "OR5", "po_reference": "PO-5", "total_amount": "1",
             "custom_fields": {"sageOrdUniq": "  "}, "updated_at": "2026-09-29T09:00:00"}]
        mirror = self.mirror()
        mirror.sync(OPS)
        orders = self.by_id(mirror.rows(OPS, "public.app_sales_order", max_staleness=3600))
        self.assertEqual({key: item["lineage"] for key, item in orders.items()},
                         {1: "gold_native_staging", 2: "gold_etl_from_fm",
                          3: "gold_etl_from_sage", 4: "gold_native_staging",
                          5: "gold_native_staging"})
        resolved = mirror.resolve_citation(orders[4]["citation"], OPS)
        self.assertEqual((resolved["lineage"], resolved["lineage_rule"]),
                         ("gold_native_staging", "sales_order_portal_bridge"))
        coverage = {item["relation"]: item["lineage_counts"]
                    for item in mirror.coverage(OPS)["relations"]}
        self.assertEqual(coverage["sage.po_header"], {"sage_bronze": 3})
        self.assertEqual(coverage["silver.shipments"], {"silver_view": 3})
        self.assertEqual(coverage["public.customer"], {"fm_bronze": 2})
        candidates = mirror.resolve(OPS, "OR4")["candidates"]
        self.assertEqual([item["lineage"] for item in candidates], ["gold_native_staging"])

    def test_a_rule_change_reclassifies_stored_rows_and_versions(self):
        mirror = self.mirror()
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        mirror.close()
        rules = [{"id": "customers_native", "relations": ["public.customer"],
                  "when": [["code", "prefix", "C"]], "lineage": "gold_native_staging"}]
        reopened = self.mirror(lineage_rules=rules)
        self.assertEqual(reopened.resolve_citation(citation, OPS)["lineage"],
                         "gold_native_staging")
        reopened.close()
        # No matching rule at all: lineage None (unknown), the row is never dropped.
        bare = self.mirror(lineage_rules=[])
        self.assertIsNone(bare.rows(OPS, "public.customer")["rows"][0]["lineage"])

    def test_a_file_from_before_lineage_and_absence_columns_is_migrated(self):
        mirror = self.mirror()
        citation = mirror.rows(OPS, "public.customer")["rows"][0]["citation"]
        mirror.close()
        with contextlib.closing(sqlite3.connect(str(self.path))) as db:
            for table, columns in (("row_current", ("missing_from_source_since",
                                                    "missing_basis", "lineage",
                                                    "lineage_basis")),
                                   ("row_version", ("lineage", "lineage_basis"))):
                for column in columns:
                    db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            db.execute("DROP TABLE relation_lineage")
            db.commit()
        reopened = self.mirror()
        resolved = reopened.resolve_citation(citation, OPS)
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["lineage"], "fm_bronze")


class ProbeTests(SyncTestCase):
    def build_catalog(self, count):
        self.fake.catalog, self.fake.tables = {}, {}
        for index in range(count):
            schema = ("public", "silver", "sage")[index % 3]
            generated_relation(self.fake, f"{schema}.probe_rel_{index:02d}", 60,
                               since=index % 2 == 0, start=index * 1000)
            for row in self.fake.tables[f"{schema}.probe_rel_{index:02d}"]:
                row["code"] = "SECRET-VALUE-" + str(row["id"])

    def test_probe_syncs_93_relations_and_reports_counts_only(self):
        self.build_catalog(93)
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        report = full_coverage_probe(client, self.path, resolve_sample=5000,
                                     retry_delay_seconds=0, sleep=self.sleeps.append)
        self.assertEqual(report["relation_count"], 93)
        self.assertEqual(report["catalog_relation_count"], 93)
        self.assertEqual(report["complete_relation_count"], 93)
        self.assertTrue(report["all_served_complete"])
        self.assertTrue(report["complete_equals_catalog"])
        self.assertEqual(report["rows_current_total"], 93 * 60)
        self.assertEqual(report["handles_resolved"], 5000)
        self.assertTrue(report["resolve_all_ok"])
        self.assertTrue(report["batch_resolve_under_3s"])
        self.assertEqual(report["lineage_totals"],
                         {"fm_bronze": 31 * 60, "silver_view": 31 * 60, "sage_bronze": 31 * 60})
        self.assertEqual(report["relations"]["public.probe_rel_00"],
                         {"complete": True, "rows_current": 60, "rows_missing": 0,
                          "rows_deleted": 0, "pages": 1})
        text = json.dumps(report)
        self.assertNotIn("SECRET-VALUE", text)
        self.assertNotIn("api://", text)

    def test_probe_captures_ordered_relations_first_and_reports_their_outcomes(self):
        self.build_catalog(6)
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        first = ["silver.probe_rel_04", "public.probe_rel_03", "public.not_served_here"]
        report = full_coverage_probe(client, self.path, resolve_sample=10, order=first,
                                     retry_delay_seconds=0, sleep=self.sleeps.append)
        rows_paths = [path for path in self.fake.paths if path.endswith("/rows")]
        self.assertEqual(rows_paths[:2], ["/v1/relations/silver.probe_rel_04/rows",
                                          "/v1/relations/public.probe_rel_03/rows"])
        self.assertEqual(report["capture_order"], "ordered_first")
        self.assertEqual(report["ordered_in_catalog_count"], 2)
        self.assertEqual(report["ordered_run_outcomes"], {"captured": 2})
        self.assertEqual(report["complete_relation_count"], 6)
        plain = full_coverage_probe(client, self.path.with_name("plain.sqlite"),
                                    resolve_sample=0, retry_delay_seconds=0)
        self.assertNotIn("capture_order", plain)

    def test_probe_cli_through_live_read_probes(self):
        from scripts import live_read_probes as probes
        self.build_catalog(4)
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = probes.main(["mirror-full-coverage", "--db", str(self.path),
                                "--resolve-sample", "10"], environ={},
                               clients={"mirror-full-coverage": client})
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["complete_relation_count"], report["handles_resolved"]),
                         (4, 10))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probes.main(["mirror-full-coverage"], environ={}), 1)
        self.assertEqual(json.loads(err.getvalue())["error"], "LookupError")

    def test_spec_is_valid_get_only_and_counts_only(self):
        from verify import live_verify as lv
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.full_coverage.json")
                          .read_text())
        self.assertEqual(lv.validate_spec(spec), [])
        self.assertEqual(spec["command"][2], "scripts.live_read_probes")
        self.assertIn(spec["command"][2], lv.ALLOWED_MODULES)
        self.assertEqual(spec["expect"]["json_min"]["catalog_relation_count"], 93)
        self.assertEqual(spec["expect"]["json_min"]["handles_resolved"], 5000)
        self.assertTrue(spec["expect"]["json_equals"]["batch_resolve_under_3s"])


class CliTests(SyncTestCase):
    def test_coverage_and_cite_batch_run_offline_without_credentials(self):
        from integrations.source_mirror import main
        audience = Path(self.directory.name) / "audience.json"
        audience.write_text(json.dumps({"schema": "plx-source-mirror-audience/v1",
                                        "tenant": "plx", "audiences": ALL}))
        mirror = self.mirror(audience)
        handles = [item["citation"] for item in mirror.rows(OPS, "public.customer")["rows"]]
        mirror.close()
        handles_file = Path(self.directory.name) / "handles.txt"
        handles_file.write_text("\n".join(handles + ["bad"]) + "\n")
        base = ["--db", str(self.path), "--audience", str(audience),
                "--principal", "staff:operations"]
        for command, check in (
                (["coverage"], lambda out: out["complete_relation_count"] == 1),
                (["cite-batch", str(handles_file)],
                 lambda out: (out["count"], out["ok_count"]) == (3, 2))):
            output = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output):
                self.assertEqual(main(base + command), 0)
            self.assertTrue(check(json.loads(output.getvalue())), command)
        errors = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stderr(errors):
            self.assertEqual(main(base + ["sync"]), 2)
        self.assertEqual(json.loads(errors.getvalue())["error"], "source_unavailable")


class TransportTests(unittest.TestCase):
    def test_http_error_keeps_status_retry_after_and_error_code_only(self):
        body = io.BytesIO(b'{"error":{"code":"too-many-connections","message":"secret"}}')
        error = HTTPError("https://api.example.test/v1/x", 503, "busy",
                          {"Retry-After": "12"}, body)

        class Opener:
            def open(self, request, timeout):
                raise error

        with patch("integrations.http.build_opener", return_value=Opener()):
            with self.assertRaises(SourceHttpError) as caught:
                get_json("https://api.example.test/v1/x", {})
        exc = caught.exception
        self.assertIsInstance(exc, SourceUnavailable)
        self.assertEqual((exc.status, exc.retry_after_seconds, exc.error_code),
                         (503, 12.0, "too-many-connections"))
        self.assertEqual(str(exc), "read-only source GET failed: HTTPError")

    def test_retry_after_forms(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("30"), 30.0)
        self.assertEqual(retry_after_seconds("Thu, 01 Oct 2026 12:00:45 GMT", now=now), 45.0)
        self.assertEqual(retry_after_seconds("Thu, 01 Oct 2026 11:00:00 GMT", now=now), 0.0)
        for bad in (None, "", "soon", "-5", "1.5"):
            self.assertIsNone(retry_after_seconds(bad))


if __name__ == "__main__":
    unittest.main()
