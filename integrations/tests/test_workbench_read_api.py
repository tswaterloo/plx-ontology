"""Injected contract tests for the deployed Portal Workbench API."""

import copy
import re
import threading
import unittest
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import patch

from integrations.http import SourceHttpError, SourceUnavailable
from integrations.workbench_read_api import (RelationUnavailable, WorkbenchNotReady,
                                             WorkbenchReadApiClient, failure_reason,
                                             served_entry)


KEY = "plxwb_" + "a" * 40
ORDER_COLUMNS = ["id", "idcust", "custpocode", "erpcode", "reference",
                 "datepo", "datereq", "status"]
ORDER_ROWS = [
    {"id": 11, "idcust": 7, "custpocode": "NA", "erpcode": "SO-11",
     "reference": "A", "datepo": "2026-09-01", "datereq": "2026-10-01",
     "status": "Open"},
    {"id": 12, "idcust": 8, "custpocode": "NA", "erpcode": "SO-12",
     "reference": "B", "datepo": "2026-09-02", "datereq": "2026-10-02",
     "status": "Closed"},
]


class FakeApi:
    def __init__(self):
        self.calls = []
        self.rows = copy.deepcopy(ORDER_ROWS)
        self.unhealthy_after = False

    def __call__(self, url, headers, *, max_bytes):
        self.calls.append((url, headers, max_bytes))
        assert headers == {"Authorization": "Bearer " + KEY}
        assert max_bytes <= 28_000_000
        parts = urlsplit(url)
        if parts.path == "/v1/health":
            return {"ok": not self.unhealthy_after, "recovery": True,
                    "replayLagSeconds": 3, "unavailable": [],
                    "missingColumns": [], "generatedAt": "2026-09-27T12:00:00Z"}
        if parts.path == "/v1/relations":
            return {"relations": [{"name": "public.order_entry", "schema": "public",
                "relation": "order_entry", "domain": "FM", "columns": ORDER_COLUMNS,
                "key": ["id"], "since": None, "unavailable": None},
                {"name": "sage.ar_ship_to", "schema": "sage",
                 "relation": "ar_ship_to", "domain": "customers",
                 "columns": ["_sage_pk", "audtdate"], "key": ["_sage_pk"],
                 "since": {"column": "audtdate", "mode": "yyyymmdd-text"},
                 "unavailable": None}]}
        if parts.path == "/v1/relations/public.order_entry/rows":
            query = parse_qs(parts.query)
            columns = set(query["columns"][0].split(",")) | {"id"}
            cursor = query.get("cursor")
            offset = 0 if not cursor else 1
            row = {key: value for key, value in self.rows[offset].items()
                   if key in columns}
            return {"relation": "public.order_entry", "columns": sorted(columns),
                    "rows": [row], "nextCursor": "cursor-1" if offset == 0 else None}
        raise AssertionError(url)


# Live GET /v1/health of 2026-10-06 18:00Z (shape and counts; no row values): four Sage
# relations with ungranted columns, the other 89 of 93 served.
SAGE_GAPS = {"sage.oe_invoice_detail": 155, "sage.oe_invoice_header": 182,
             "sage.oe_order_detail": 141, "sage.oe_order_header": 148}


def gap_reason(count):
    return f"{count} approved columns are missing or not granted"


class DegradedApi(FakeApi):
    """The observed degraded API: health ``ok`` false naming the four Sage relations,
    the catalog listing them unavailable beside 89 served relations, and their rows
    route answering 404 ``relation-unavailable``."""

    def __init__(self, missing_shape="reason"):
        super().__init__()
        self.sage_served = False  # True: the catalog serves the four relations again
        self.health_body = {
            "ok": False, "recovery": True, "replayLagSeconds": 0,
            "loginExpiresAt": "2026-12-27T00:00:00.000Z", "loginExpiresInDays": 81,
            "loginExpiringSoon": False, "keysExpiringSoon": [],
            "unavailable": [{"name": name, "reason": gap_reason(count)}
                            for name, count in SAGE_GAPS.items()],
            "missingColumns": [{"name": name, "reason": gap_reason(count)}
                               if missing_shape == "reason" else
                               {"name": name, "columns": [f"c{i}" for i in range(count)]}
                               for name, count in SAGE_GAPS.items()],
            "generatedAt": "2026-10-06T18:00:00.000Z"}

    def __call__(self, url, headers, *, max_bytes):
        path = urlsplit(url).path
        if path == "/v1/health":
            self.calls.append((url, headers, max_bytes))
            return copy.deepcopy(self.health_body)
        if path == "/v1/relations":
            served = super().__call__(url, headers, max_bytes=max_bytes)["relations"]
            filler = [{"name": f"public.served_{i}", "schema": "public",
                       "relation": f"served_{i}", "columns": ["id"], "key": ["id"],
                       "since": None, "unavailable": None} for i in range(87)]
            sage = [{"name": name, "schema": "sage", "relation": name.split(".")[1],
                     "columns": ["orduniq", "ponumber"],
                     "key": ["orduniq"] if self.sage_served else None, "since": None,
                     "unavailable": None if self.sage_served else gap_reason(count)}
                    for name, count in SAGE_GAPS.items()]
            return {"relations": served + filler + sage}
        if path.startswith("/v1/relations/sage."):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=404,
                                  error_code="relation-unavailable")
        return super().__call__(url, headers, max_bytes=max_bytes)


class DegradedHealthTests(unittest.TestCase):
    def test_a_catalog_beyond_200_relations_still_serves_the_rest(self):
        # Live GET /v1/relations and /v1/health of 2026-10-21 21:05Z (counts only): 246
        # catalog relations, 179 listed unavailable; at most 200 refused every read.
        fake = DegradedApi()
        gaps = {f"public.gap_{i}": gap_reason(1) for i in range(179)}
        fake.health_body["unavailable"] = [{"name": n, "reason": r} for n, r in gaps.items()]
        fake.health_body["missingColumns"] = []
        listed = DegradedApi.__call__

        def fetch(url, headers, *, max_bytes):
            value = listed(fake, url, headers, max_bytes=max_bytes)
            if urlsplit(url).path == "/v1/relations":
                value["relations"] += [
                    {"name": name, "schema": "public", "relation": name.split(".")[1],
                     "columns": ["id"], "key": None, "since": None, "unavailable": reason}
                    for name, reason in list(gaps.items())[:246 - len(value["relations"])]]
            return value
        client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fetch)
        self.assertEqual(len(client.health()["unavailable_relations"]), 179)
        catalog = client.relations()
        self.assertEqual(len(catalog), 246)
        self.assertIn("public.order_entry", catalog)

    def test_observed_degraded_body_serves_the_rest_and_names_the_gaps(self):
        for shape in ("reason", "columns"):
            with self.subTest(missing_columns_entries=shape):
                fake = DegradedApi(shape)
                client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fake)
                health = client.health()
                self.assertIs(health["ok"], False)  # as the service reported it
                self.assertIs(health["degraded"], True)
                self.assertEqual(health["unavailable_relations"],
                                 {name: gap_reason(count) for name, count in SAGE_GAPS.items()})
                catalog = client.relations()
                readable = set(catalog) - set(SAGE_GAPS)
                for name in readable:
                    served_entry(catalog, name, health["unavailable_relations"])
                self.assertEqual((len(catalog), len(readable)), (93, 89))
                self.assertEqual(client.read_relation("public.order_entry",
                                                      columns=["id"])["row_count"], 2)
                for name, count in SAGE_GAPS.items():
                    with self.assertRaises(RelationUnavailable) as caught:
                        client.read_relation(name, columns=["ponumber"])
                    self.assertIsInstance(caught.exception, SourceUnavailable)
                    self.assertEqual((caught.exception.relation, caught.exception.reason),
                                     (name, gap_reason(count)))
                    self.assertIn(name, str(caught.exception))
        # A catalog entry read before the relation went away: the rows route's 404.
        stale = {"name": "sage.oe_order_header", "columns": ["orduniq"], "key": ["orduniq"]}
        with self.assertRaises(RelationUnavailable) as caught:
            client.read_page("sage.oe_order_header", stale, columns=["orduniq"])
        self.assertEqual((caught.exception.relation, caught.exception.status,
                          caught.exception.error_code),
                         ("sage.oe_order_header", 404, "relation-unavailable"))

    def test_login_expiring_soon_is_a_warning_while_the_login_still_works(self):
        for degraded in (True, False):
            with self.subTest(degraded=degraded):
                fake = DegradedApi()
                fake.health_body.update(
                    loginExpiresAt="2026-10-11T00:00:00.000Z", loginExpiresInDays=4,
                    loginExpiringSoon=True,
                    keysExpiringSoon=[{"id": "wb-key-vince", "expiresAt": "2026-10-12",
                                       "expiresInDays": 5}])
                if not degraded:
                    fake.health_body.update(unavailable=[], missingColumns=[])
                client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fake)
                health = client.health()
                self.assertEqual((health["ok"], health["degraded"]), (False, degraded))
                self.assertEqual(health["warnings"], {
                    "login_expiring_soon": True, "login_expires_at": "2026-10-11T00:00:00Z",
                    "login_expires_in_days": 4, "keys_expiring_soon_count": 1})
                self.assertEqual(client.read_relation("public.order_entry",
                                                      columns=["id"])["row_count"], 2)

    def test_relation_that_goes_away_during_a_read_returns_no_rows(self):
        fake = DegradedApi()
        fake.health_body.update(ok=True, unavailable=[], missingColumns=[])
        original = fake.__call__

        def fetch(url, headers, *, max_bytes):
            if urlsplit(url).path == "/v1/relations/public.order_entry/rows":
                fake.health_body.update(ok=False, unavailable=[
                    {"name": "public.order_entry", "reason": "relation not found"}])
            return original(url, headers, max_bytes=max_bytes)

        client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fetch)
        with self.assertRaisesRegex(RelationUnavailable, "public.order_entry unavailable: "
                                                         "relation not found"):
            client.read_relation("public.order_entry", columns=["id"])

    def test_every_other_health_or_catalog_problem_fails_closed_with_a_reason(self):
        cases = [
            (lambda b: ["not", "an", "object"], "health: the body is not a JSON object"),
            (lambda b: {**b, "ok": None}, "health: ok is not a boolean"),
            (lambda b: {**b, "recovery": False}, "health: recovery is not true"),
            (lambda b: {**b, "replayLagSeconds": 75}, "health: replayLagSeconds 75 is not in "
                                                      "[0, 60)"),
            (lambda b: {**b, "replayLagSeconds": "3"}, "health: replayLagSeconds is not a "
                                                       "number"),
            (lambda b: {**b, "generatedAt": None}, "health: generatedAt is not a string"),
            (lambda b: {**b, "unavailable": ["sage.oe_order_header"]},
             "health: unavailable[0] names no schema.relation"),
            (lambda b: {**b, "missingColumns": [{"name": "sage.oe_order_header"}]},
             "health: missingColumns[0] sage.oe_order_header gives no reason or columns"),
            (lambda b: {**b, "unavailable": [], "missingColumns": []},
             "health: ok is false and neither a relation nor a login warning is listed"),
            (lambda b: {**b, "loginExpiresAt": "2026-10-05T23:00:00.000Z",
                        "loginExpiresInDays": -1, "loginExpiringSoon": True},
             "health: loginExpiresAt 2026-10-05T23:00:00Z is past (the login expired)"),
            (lambda b: {**b, "unavailable": [{"name": n, "reason": "relation not found"}
                                             for n in ["public.order_entry",
                                                       "sage.ar_ship_to",
                                                       *(f"public.served_{i}"
                                                         for i in range(87)),
                                                       *SAGE_GAPS]]},
             "health: none of the 93 catalog relations is served (93 listed unavailable)")]
        for mutate, reason in cases:
            with self.subTest(reason=reason):
                fake = DegradedApi()
                fake.health_body = mutate(fake.health_body)
                client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fake)
                with self.assertRaises(WorkbenchNotReady) as caught:
                    client.health()
                self.assertEqual(failure_reason(caught.exception), reason)
                self.assertNotIn(KEY, str(caught.exception))

        def broken_catalog(relations):
            def fetch(url, headers, *, max_bytes):
                if urlsplit(url).path == "/v1/relations":
                    return {"relations": relations}
                return FakeApi()(url, headers, max_bytes=max_bytes)
            return WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fetch)

        entry = {"name": "public.order_entry", "schema": "public", "relation": "order_entry",
                 "columns": ["id"], "key": ["id"]}
        for relations, reason in (
                ({}, "catalog: relations is not a list of 1 to 1000 entries"),
                ([{**entry, "key": ["idoe"]}],
                 "catalog: public.order_entry key is not distinct listed columns"),
                ([entry, entry], "catalog: public.order_entry is listed twice")):
            with self.subTest(reason=reason), self.assertRaises(WorkbenchNotReady) as caught:
                broken_catalog(relations).relations()
            self.assertEqual(caught.exception.reason, reason)

    def test_upstream_reason_text_is_published_only_as_plain_text(self):
        fake = DegradedApi()
        fake.health_body["unavailable"][0]["reason"] = (
            "see https://host/v1/health?key=" + KEY)
        client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fake)
        reason = client.health()["unavailable_relations"]["sage.oe_invoice_detail"]
        self.assertEqual(reason, "upstream reason not shown (not plain text)")
        self.assertIsNone(failure_reason(RuntimeError("https://host/v1/health?key=" + KEY)))


class WorkbenchReadApiTests(unittest.TestCase):
    def test_catalog_accepts_sage_key_from_portal_manifest(self):
        # Portal staging's approved sage.ar_ship_to includes _sage_pk.
        client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=FakeApi())
        catalog = client.relations()
        self.assertEqual(catalog["sage.ar_ship_to"]["key"], ["_sage_pk"])
        self.assertEqual(client.read_relation("public.order_entry", columns=["id"])["row_count"], 2)

    def test_the_request_count_holds_across_mirror_worker_threads(self):
        # The Portal mirror's workers share one client: every GET is counted once.
        client = WorkbenchReadApiClient("https://api.example.test", KEY,
                                        fetch=lambda url, headers, *, max_bytes: {})
        workers = [threading.Thread(target=lambda: [client._get("/v1/health")
                                                    for _ in range(2000)])
                   for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(client.requests_sent, 8 * 2000)

    def test_default_transport_waits_for_workbench_statement_window(self):
        packet = {"ok": True, "recovery": True, "replayLagSeconds": 0,
                  "unavailable": [], "missingColumns": [],
                  "generatedAt": "2026-09-27T12:00:00Z"}
        with patch("integrations.workbench_read_api.get_json", return_value=packet) as get:
            WorkbenchReadApiClient("https://api.example.test", KEY).health()
        get.assert_called_once_with("https://api.example.test/v1/health",
            {"Authorization": "Bearer " + KEY}, max_bytes=5_500_000,
            timeout_seconds=40)

    def test_full_page_is_not_an_atomic_deletion_proof(self):
        client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=FakeApi())
        result = client.read_relation("public.order_entry", columns=["id", "erpcode"],
                                      max_rows=2)
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["page_count"], 2)
        self.assertFalse(result["atomic_upstream_snapshot"])
        self.assertFalse(result["deletions_proven"])
        self.assertTrue(result["complete_within_bound"])

    def test_duplicate_key_and_page_bound_fail_closed(self):
        fake = FakeApi()
        fake.rows[1]["id"] = fake.rows[0]["id"]
        client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fake)
        with self.assertRaisesRegex(SourceUnavailable, "duplicate page key"):
            client.read_relation("public.order_entry", columns=["id"])
        with self.assertRaisesRegex(SourceUnavailable, "page bound"):
            client.read_relation("public.order_entry", columns=["id"], max_pages=1)

    def test_view_key_run_within_one_page_is_preserved(self):
        fake = FakeApi()
        original = fake.__call__
        def fetch(url, headers, *, max_bytes):
            packet = original(url, headers, max_bytes=max_bytes)
            if urlsplit(url).path == "/v1/relations/public.order_entry/rows":
                query = parse_qs(urlsplit(url).query)
                if "cursor" not in query:
                    row = dict(packet["rows"][0])
                    packet["rows"] = [row, {**row, "erpcode": "SO-11-REV"}]
            return packet
        client = WorkbenchReadApiClient("https://api.example.test", KEY,
            fetch=fetch)
        rows = client.read_relation("public.order_entry", columns=["id", "erpcode"],
            page_size=2)["rows"]
        self.assertEqual([row["id"] for row in rows], [11, 11, 12])

    def test_unapproved_column_unhealthy_replica_and_bad_key(self):
        fake = FakeApi()
        client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fake)
        with self.assertRaisesRegex(ValueError, "not_approved"):
            client.read_relation("public.order_entry", columns=["private_field"])
        fake.unhealthy_after = True
        with self.assertRaises(SourceUnavailable):
            client.read_relation("public.order_entry", columns=["id"])
        with self.assertRaises(ValueError):
            WorkbenchReadApiClient("https://api.example.test", KEY + "\n", fetch=fake)

    def test_replica_health_accepts_contract_permitted_unknown_lag(self):
        fake = FakeApi()
        original = fake.__call__

        def fetch(url, headers, *, max_bytes):
            packet = original(url, headers, max_bytes=max_bytes)
            if urlsplit(url).path == "/v1/health":
                packet["replayLagSeconds"] = None
            return packet

        client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fetch)
        self.assertIsNone(client.health()["replayLagSeconds"])
        self.assertEqual(client.read_relation("public.order_entry", columns=["id"])["row_count"], 2)


class RetiredPagedImportTests(unittest.TestCase):
    """The paged-API import into the ontology is retired (devloop/CONVERGENCE.md).

    The source mirror is the only consumer of this client. The objective runtime
    that kept paged roots advisory went with ``UnifiedPlatform`` (deletion-8).
    """

    def test_import_surfaces_are_gone(self):
        import importlib.util
        from operating.mcp_server import TOOLS
        names = {tool["name"] for tool in TOOLS}
        self.assertFalse(names & {"plx.investigate_workbench_api_named",
                                  "plx.lookup_workbench_api_named",
                                  "plx.continue_api_advisory_objective"})
        self.assertIsNone(importlib.util.find_spec("operating.unified"))


if __name__ == "__main__":
    unittest.main()
