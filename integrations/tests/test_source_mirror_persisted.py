"""The last complete source-mirror capture, served stale through a Portal API outage.

Decision 2026-10-04-scope-freshness. Evidence label:
``injected-transport``. The real ``SourceMirror`` behind the real
``WorkbenchReadApiClient`` runs against the source-mirror tests' ``FakeWorkbench``; the
outage is an injected HTTP 500 ``internal`` from ``GET /v1/health`` (the live symptom
since 2026-10-03 03:33Z). ``plx.find`` is the real ``OntologyTools`` over the compiled
schema, its output checked against the tool contract. The capture store is always
injected (a temporary directory); no test reads ``PLX_WORKBENCH_STATE_DIR``. Every row
is SYNTHETIC. Socket connections are refused for the whole module.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from integrations import mirror_capture as mc, source_mirror as sm
from integrations.capture_mail import CaptureError
from integrations.http import SourceHttpError, SourceUnavailable
from integrations.source_mirror import MirrorError, SourceMirror
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import identifiers, resolve
from ontology.core import Actor
from ontology.json_contract import validate
from ontology.project import MirrorCaptures, build_store
from ontology.schema import load_schema
from ontology.tests.test_resolve import CATALOG, tables
from operating import ontology_tools as ot

RELATIONS = sorted(CATALOG)
AUDIENCE = {"public.*": ["staff:sales", "staff:operations"]}
SALES = Actor("plx", frozenset({"staff:sales"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:warehouse"}), "restricted")
AS_OF = "2026-09-29T12:00:00.000000Z"
LABEL = ("source-mirror captures as of 2026-09-29T12:00:00.000000Z, live source unavailable "
         "(500, internal)")
HTTP_500 = SourceHttpError("read-only source GET failed: HTTPError", status=500,
                           error_code="internal")


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


class Outage:
    """The fake Workbench, with ``GET /v1/health`` failing while ``down`` is set and
    every route failing while ``down_all`` is (the live shape since 2026-10-03)."""

    def __init__(self, fake) -> None:
        self.fake = fake
        self.down: BaseException | None = None
        self.down_all: BaseException | None = None
        self.paths: list[str] = []

    def __call__(self, url, headers, *, max_bytes):
        path = urlsplit(url).path
        self.paths.append(path)
        if self.down_all is not None:
            raise self.down_all
        if self.down is not None and path == "/v1/health":
            raise self.down
        return self.fake(url, headers, max_bytes=max_bytes)


class PersistedTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_schema()
        cls.view = ot.SchemaView.from_compiled(cls.schema)
        cls.contracts = ot.load_contracts()

    def setUp(self) -> None:
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(CATALOG))
        fake.tables = tables()
        self.fake = fake
        self.transport = Outage(fake)
        self.clock = Clock()
        self.store = mc.MirrorCaptureStore(self.root / "state", clock=self.clock)

    def client(self, base=BASE):
        return WorkbenchReadApiClient(base, KEY, fetch=self.transport)

    def serve(self, *, relations=RELATIONS, **kwargs):
        mirror, report = mc.sync_or_persisted(
            self.store, self.client(), AUDIENCE, SALES, relations=relations,
            clock=self.clock, retry_delay_seconds=0, **kwargs)
        self.addCleanup(mirror.close)
        return mirror, report

    def persist_live(self):
        mirror, report = self.serve()
        self.assertEqual((report["served_from"], report["complete"]), ("live", True), report)
        self.assertTrue(report["persisted"]["persisted"], report)
        mirror.close()
        return report

    def running(self, name="case.sqlite", store="default"):
        """A product mirror (the case runtime's kind) with the store injected."""
        mirror = SourceMirror(self.root / name, self.client(), AUDIENCE, clock=self.clock,
                              retry_delay_seconds=0,
                              persisted_store=self.store if store == "default" else store)
        self.addCleanup(mirror.close)
        return mirror

    def tools(self, mirror, *, store_sources=None, policy=None):
        resolver = resolve.SourceResolver(
            self.view, [resolve.MirrorLookup(mirror, clock=self.clock)],
            normalizer=identifiers.normalize, workdir=self.root / "work", clock=self.clock)
        self.addCleanup(resolver.close)
        store = self.root / f"store-{len(list(self.root.glob('store-*')))}.sqlite"
        build_store(store, self.schema, store_sources or {}, normalizer=identifiers.normalize,
                    clock=self.clock)
        reader = ot.StoreReader.open(store)
        self.addCleanup(reader.conn.close)
        return ot.OntologyTools(
            reader, policy or ot.ReadPolicy("plx", audiences={
                "api:*": {"staff:sales", "staff:operations"}}),
            schema=self.view, grammar=identifiers, contracts=self.contracts,
            clock=self.clock, sources=resolver)

    def find(self, tools, arguments, actor=SALES):
        out = tools.call("plx.find", arguments, actor)
        self.assertEqual(validate(out, self.contracts["tools"]["plx.find"]["output"],
                                  root=self.contracts), [], out)
        return out

    def portal(self, out):
        (fresh,) = [f for f in out["meta"]["freshness"] if f["source"] == "portal_api"]
        return fresh

    def assert_copy(self, out, label=LABEL):
        """Served from the persisted capture: stale, labelled, as old as the capture,
        and the label is the first gap."""
        fresh = self.portal(out)
        self.assertEqual((fresh["served_from"], fresh["stale"], fresh["label"]),
                         ("persisted_capture", True, label), fresh)
        self.assertTrue(fresh["captured_at"].startswith("2026-09-29T12:00:00"), fresh)
        self.assertEqual((out["gaps"][0]["state"], out["gaps"][0]["reason"],
                          out["gaps"][0]["detail"]), ("stale", "stale_source", label),
                         out["gaps"])
        self.assertNotIn("absent_in_source", {g["state"] for g in out["gaps"]})


class FindThroughOutageTests(PersistedTestCase):
    """Acceptance of decision B over a running product mirror: a 500 from /v1/health
    with a persisted copy, a no-match over the copy, and no copy at all."""

    def test_health_500_find_returns_ranked_candidates_labelled_from_the_copy(self):
        self.persist_live()
        self.clock.advance(3600)
        mirror = self.running()
        self.transport.down = HTTP_500
        out = self.find(self.tools(mirror),
                        {"query": "CPO-DUP", "object_type": "CustomerPurchaseOrder"})
        self.assertEqual(out["status"], "ambiguous", out)
        self.assertEqual([c["rank"] for c in out["candidates"]], [1, 2])
        self.assertTrue(all(c["matched"]["raw"]["origin"]["citation"].startswith("api://")
                            for c in out["candidates"]))
        self.assert_copy(out)
        (unit,) = self.portal(out)["relations"]
        self.assertEqual((unit["relation"], unit["served_from"], unit["http_status"],
                          unit["error_code"]),
                         ("public.app_sales_order", "persisted_capture", 500, "internal"))

    def test_a_no_match_over_the_copy_is_unknown_and_labelled_never_absent(self):
        self.persist_live()
        mirror = self.running()
        self.transport.down = HTTP_500
        out = self.find(self.tools(mirror), {"query": "SO100999", "object_type": "SalesOrder"})
        self.assertEqual((out["status"], out["candidates"]), ("not_found", []), out)
        self.assert_copy(out)
        (gap,) = [g for g in out["gaps"] if g["reason"] == "no_visible_match"]
        self.assertEqual(gap["state"], "unknown")
        self.assertIn("was not refreshed", gap["detail"])  # absence_claims.traversal_problem

    def test_without_a_copy_the_unit_is_inaccessible_with_its_http_status(self):
        mirror = self.running()
        self.transport.down = HTTP_500
        out = self.find(self.tools(mirror), {"query": "SO100305", "object_type": "SalesOrder"})
        self.assertEqual((out["status"], out["candidates"]), ("not_found", []), out)
        first = out["gaps"][0]
        self.assertEqual((first["state"], first["reason"], first["subject"]),
                         ("inaccessible", "source_unavailable", "portal_api"), out["gaps"])
        self.assertIn("500, internal", first["detail"])
        fresh = self.portal(out)
        self.assertNotEqual(fresh.get("served_from"), "persisted_capture")
        self.assertNotIn("label", fresh)
        self.assertIn({"relation": "public.app_sales_order", "served_from": "none",
                       "captured_at": None, "http_status": 500, "error_code": "internal"},
                      fresh["relations"])
        self.assertNotIn("absent_in_source", {g["state"] for g in out["gaps"]})
        # Each carrier read through it is still named unreadable (callers rely on it).
        self.assertIn("source_unreadable", {g["reason"] for g in out["gaps"]})

    def test_reads_through_an_outage_ask_the_source_once_per_hold_window(self):
        # Live 2026-10-05: case.unfamiliar_order timed out at 1500 s while every Workbench
        # route answered 500, each read paying the catalog's full retry contract again.
        mirror = self.running()
        tools = self.tools(mirror)
        self.transport.down_all = HTTP_500
        for _ in range(5):
            out = self.find(tools, {"query": "SO100305", "object_type": "SalesOrder"})
            self.assertEqual(out["status"], "not_found", out)
            self.assertIn("500, internal", out["gaps"][0]["detail"])
        self.assertEqual(self.transport.paths.count("/v1/relations"), 2)  # read_attempts
        self.clock.advance(sm.CATALOG_OUTAGE_HOLD_SECONDS)
        self.find(tools, {"query": "SO100305", "object_type": "SalesOrder"})
        self.assertEqual(self.transport.paths.count("/v1/relations"), 4)
        self.transport.down_all = None
        self.clock.advance(sm.CATALOG_OUTAGE_HOLD_SECONDS)
        found = self.find(tools, {"query": "SO100305", "object_type": "SalesOrder"})
        self.assertEqual(found["status"], "found", found)

    def test_a_held_relation_whose_refresh_fails_is_the_cached_mirror_never_live(self):
        mirror = self.running()
        mirror.rows(SALES, "public.app_sales_order")  # held, captured live at 12:00
        self.clock.advance(3600)
        self.transport.down = HTTP_500
        tools = self.tools(mirror)
        for query in ("SO100305", "SO100999"):
            out = self.find(tools, {"query": query, "object_type": "SalesOrder"})
            fresh = self.portal(out)
            self.assertEqual((fresh["served_from"], fresh["stale"]),
                             ("cached_mirror", True), fresh)
            self.assertTrue(fresh["captured_at"].startswith("2026-09-29T12:00:00"), fresh)
            self.assertTrue(fresh["label"].startswith("source-mirror captures as of "
                                                      "2026-09-29T12:00:00"), fresh)
            self.assertTrue(fresh["label"].endswith("live source unavailable (500, internal)"))
            self.assertIn(("public.app_sales_order", "cached_mirror", 500, "internal"),
                          [(u["relation"], u["served_from"], u.get("http_status"),
                            u.get("error_code")) for u in fresh["relations"]])
            self.assertEqual((out["gaps"][0]["reason"], out["gaps"][0]["detail"]),
                             ("stale_source", fresh["label"]), out["gaps"])
            self.assertNotIn("absent_in_source", {g["state"] for g in out["gaps"]})
        self.assertEqual(out["status"], "not_found", out)
        found = self.find(tools, {"query": "SO100305", "object_type": "SalesOrder"})
        self.assertEqual(found["status"], "found", found)

    def test_a_live_answer_is_never_labelled_as_a_persisted_capture(self):
        self.persist_live()
        out = self.find(self.tools(self.running()),
                        {"query": "SO100305", "object_type": "SalesOrder"})
        self.assertEqual(out["status"], "found", out)
        fresh = self.portal(out)
        self.assertEqual(fresh["served_from"], "live")
        self.assertFalse({"label", "relations"} & set(fresh), fresh)
        self.assertNotIn("stale_source", {g["reason"] for g in out["gaps"]})

    def test_a_reader_the_copy_is_withheld_from_gets_nothing_and_no_relation_name(self):
        self.persist_live()
        mirror = self.running()
        self.transport.down = HTTP_500
        out = self.find(self.tools(mirror), {"query": "CPO-DUP",
                                             "object_type": "CustomerPurchaseOrder"}, OUTSIDER)
        self.assertEqual(out["candidates"], [], out)
        self.assertNotIn("public.app_sales_order", json.dumps(out))

    def test_a_json_key_index_follows_the_capture_generation_not_its_ttl(self):
        self.persist_live()
        self.clock.advance(3600)
        mirror = self.running()
        mirror.rows(SALES, "public.app_shipment")  # held live
        self.transport.down = HTTP_500
        tools = self.tools(mirror)
        stale = self.find(tools, {"query": "BCSO-77"})
        self.assertEqual(stale["status"], "found", stale)
        self.assert_copy(stale)
        self.clock.advance(60)  # inside the index TTL and the retry window: reused, labelled
        self.assert_copy(self.find(tools, {"query": "BCSO-77"}))
        # The source recovers and renumbers the order; a live refresh replaces the
        # copied rows inside the old 300 s index window.
        self.transport.down = None
        for row in self.fake.tables["public.app_sales_order"]:
            if row["id"] == "so-3":
                row["custom_fields"] = {"renumberedTo": "BCSO-88"}
        self.clock.advance(901)
        self.assertEqual(self.find(tools, {"query": "BCSO-77"})["candidates"], [])
        new = self.find(tools, {"query": "BCSO-88"})
        self.assertEqual(new["status"], "found", new)
        self.assertEqual(self.portal(new)["served_from"], "live")
        self.assertFalse(self.portal(new)["stale"])

    def test_a_projection_built_from_the_restored_copy_is_labelled(self):
        self.persist_live()
        self.transport.down = HTTP_500
        mirror, _ = self.serve()
        captures = MirrorCaptures(mirror._path)
        self.addCleanup(captures.close)
        tools = self.tools(mirror, store_sources={"portal_api": captures})
        out = self.find(tools, {"query": "SO100305", "object_type": "SalesOrder"})
        self.assertEqual(out["status"], "found", out)
        self.assert_copy(out)

    def test_a_projection_of_the_running_mirror_names_the_copied_relation(self):
        self.persist_live()
        mirror = self.running()
        mirror.rows(SALES, "public.app_sales_order")
        self.transport.down = HTTP_500
        mirror.rows(SALES, "public.app_shipment")
        captures = MirrorCaptures(mirror._path)
        self.addCleanup(captures.close)
        persisted = captures.describe()["persisted"]
        self.assertEqual(list(persisted), ["public.app_shipment"])
        self.assertEqual(persisted["public.app_shipment"]["label"], LABEL)


class MirrorFillTests(PersistedTestCase):
    """A running mirror fills only the relation it never held from the copy; what it
    holds, its citations and its client stay; it reads live again after the TTL."""

    def test_a_held_relation_and_its_citations_survive_a_fill_from_the_copy(self):
        self.serve(relations=["public.app_shipment"])[0].close()
        self.clock.advance(86400)
        mirror = self.running()
        handle = mirror.rows(SALES, "public.app_sales_order")["rows"][0]["citation"]
        self.transport.down = HTTP_500
        shipments = mirror.rows(SALES, "public.app_shipment")
        self.assertEqual(shipments["returned"], 2)
        self.assertEqual(shipments["freshness"]["freshness_label"], LABEL)
        self.assertTrue(shipments["freshness"]["stale"])
        self.assertIn("restored from the persisted source-mirror capture",
                      mirror.resolve_citation(shipments["rows"][0]["citation"],
                                              SALES)["capture"]["reason"])
        held = mirror.rows(SALES, "public.app_sales_order")
        self.assertEqual(held["returned"], 4)
        self.assertNotIn("freshness_label", held["freshness"])
        cited = mirror.resolve_citation(handle, SALES)
        self.assertTrue(cited["ok"], cited)
        self.assertNotIn("source_freshness", cited)
        out = mirror.resolve(SALES, "SO100305")
        self.assertEqual([item["relation"] for item in out.get("stale_relations", [])],
                         ["public.app_shipment"])

    def test_the_copy_is_left_once_the_source_answers_after_the_ttl(self):
        self.persist_live()
        self.clock.advance(86400)
        mirror = self.running()
        self.transport.down = HTTP_500
        self.assertEqual(mirror.rows(SALES, "public.app_shipment")["freshness"]
                         ["freshness_label"], LABEL)
        calls = len(self.transport.paths)
        self.clock.advance(60)  # inside max_staleness: no live attempt
        mirror.rows(SALES, "public.app_shipment")
        self.assertEqual(len(self.transport.paths), calls)
        self.clock.advance(900)  # past it, still out: tried, still the copy
        self.assertEqual(mirror.rows(SALES, "public.app_shipment")["freshness"]
                         ["freshness_label"], LABEL)
        self.assertGreater(len(self.transport.paths), calls)
        self.transport.down = None
        self.fake.update("public.app_shipment", "sh-1", shipment_code="SH100009")
        self.clock.advance(901)
        fresh = mirror.rows(SALES, "public.app_shipment")
        self.assertNotIn("freshness_label", fresh["freshness"])
        self.assertFalse(fresh["freshness"]["stale"])
        self.assertIn("SH100009", {item["row"]["shipment_code"] for item in fresh["rows"]})
        self.assertIsNone(mirror.source_freshness("public.app_shipment"))

    def test_a_refusal_is_never_answered_from_the_copy(self):
        self.persist_live()
        mirror = self.running()
        self.transport.down = SourceHttpError("read-only source GET failed: HTTPError",
                                              status=403, error_code="forbidden")
        with self.assertRaises(MirrorError) as refused:
            mirror.rows(SALES, "public.app_shipment")
        self.assertEqual((refused.exception.code, refused.exception.status),
                         ("source_unavailable", 403))

    def test_without_an_injected_store_a_mirror_reads_none(self):
        self.persist_live()
        self.transport.down = HTTP_500
        with self.assertRaises(MirrorError) as failed:
            self.running("plain.sqlite", store=None).rows(SALES, "public.app_shipment")
        self.assertEqual((failed.exception.code, failed.exception.status),
                         ("source_unavailable", 500))

    def test_a_copy_from_another_upstream_is_never_served(self):
        self.persist_live()
        self.transport.down = HTTP_500
        mirror = SourceMirror(self.root / "other.sqlite",
                              self.client("https://other.example.test"), AUDIENCE,
                              clock=self.clock, retry_delay_seconds=0,
                              persisted_store=self.store)
        self.addCleanup(mirror.close)
        with self.assertRaises(MirrorError) as failed:
            mirror.rows(SALES, "public.app_sales_order")
        self.assertEqual(failed.exception.code, "source_unavailable")

    def test_a_case_citation_of_a_copied_row_is_stale_not_current(self):
        from operating.case_runtime import CaseRuntime
        self.persist_live()
        audience = self.root / "audience.json"
        audience.write_text(json.dumps({"schema": "plx-source-mirror-audience/v1",
                                        "tenant": "plx", "audiences": AUDIENCE}))
        runtime = CaseRuntime(self.root / "case", audience=audience, tenant="plx",
                              client=self.client(),
                              mirror_options={"clock": self.clock, "retry_delay_seconds": 0,
                                              "persisted_store": self.store})
        self.addCleanup(runtime.close)
        self.transport.down = HTTP_500
        handle = runtime.mirror.rows(SALES, "public.app_sales_order")["rows"][0]["citation"]
        status = runtime.citation_status(handle, SALES)
        self.assertEqual((status["status"], status["content_status"],
                          status["freshness_label"]), ("stale", "current", LABEL))
        self.assertEqual(status["upstream_presence"], "not_rechecked_live_source_unavailable")


    def test_a_copied_row_never_verifies_a_proposal_effect(self):
        from operating import case_verification as cv
        from operating.case_runtime import CaseRuntime
        self.persist_live()
        audience = self.root / "audience.json"
        audience.write_text(json.dumps({"schema": "plx-source-mirror-audience/v1",
                                        "tenant": "plx", "audiences": AUDIENCE}))
        runtime = CaseRuntime(self.root / "case", audience=audience, tenant="plx",
                              client=self.client(),
                              mirror_options={"clock": self.clock, "retry_delay_seconds": 0,
                                              "persisted_store": self.store})
        self.addCleanup(runtime.close)
        self.transport.down = HTTP_500
        (row,) = [item for item in runtime.mirror.rows(SALES, "public.app_sales_order")["rows"]
                  if item["row"]["id"] == "so-2"]
        case_id = runtime.cases.open(SALES, "Set the customer PO on SO100305")
        for equals in ("CPO-DUP", "CPO-NEW"):  # held by the copy, and not held by it
            proposal = runtime.cases.propose(
                SALES, case_id, "portal.sales_order.set_po", {"po": equals},
                citations=[row["citation"]],
                expected_effect={"relation": "public.app_sales_order", "key": {"id": "so-2"},
                                 "fields": ["po_reference"], "equals": equals})
            for limit in (None, 10 * 365 * 86400):
                result = cv.verify_proposal(runtime, SALES, case_id, proposal["proposal_id"],
                                            max_staleness=limit)
                self.assertEqual((result["outcome"], result["reason"],
                                  result["observed_citation"]),
                                 ("unknown", "relation_served_from_persisted_capture", None))
        recorded = runtime.cases.get(SALES, case_id)["verifications"]
        self.assertEqual({v["outcome"] for v in recorded}, {"unknown"})
        self.assertEqual({v["detail"]["source_freshness"]["label"] for v in recorded}, {LABEL})


class StoreTests(PersistedTestCase):
    def test_a_whole_copy_is_served_offline_and_labelled_on_every_answer(self):
        self.persist_live()
        self.transport.down = HTTP_500
        self.clock.advance(3600)
        before = len(self.fake.row_requests())
        mirror, report = self.serve()
        self.assertEqual(report["served_from"], "persisted_capture")
        freshness = report["source_freshness"]
        self.assertEqual((freshness["state"], freshness["live"], freshness["http_status"],
                          freshness["error_code"], freshness["captures_as_of"],
                          freshness["label"]),
                         ("stale", False, 500, "internal", AS_OF, LABEL))
        self.assertEqual(len(self.fake.row_requests()), before)  # seen at the health check
        page = mirror.rows(SALES, "public.app_sales_order")
        self.assertEqual(page["returned"], 4)
        self.assertEqual(page["freshness"]["source_freshness"], freshness)
        self.assertEqual(mirror.catalog(SALES)["source_freshness"]["label"], LABEL)
        self.assertEqual(mirror.resolve_citation(page["rows"][0]["citation"], SALES)
                         ["source_freshness"]["live"], False)
        with self.assertRaises(MirrorError) as refused:
            mirror.rows(OUTSIDER, "public.app_sales_order")  # audiences apply unchanged
        self.assertEqual(refused.exception.code, "not_visible")
        with self.assertRaises(ValueError):  # a restored copy opens offline only
            SourceMirror(mirror._path, self.client(), AUDIENCE)

    def test_an_unreachable_source_is_labelled_without_an_http_status(self):
        self.persist_live()
        self.transport.down = SourceUnavailable("workbench API GET failed: URLError")
        _, report = self.serve()
        self.assertEqual(report["source_freshness"]["label"],
                         f"source-mirror captures as of {AS_OF}, live source unavailable "
                         "(no_http_status, source_unavailable)")

    def test_no_or_an_incomplete_copy_is_a_typed_failure_never_rows(self):
        self.transport.down = HTTP_500
        with self.assertRaises(MirrorError) as failed:
            self.serve()
        self.assertEqual(failed.exception.code, "missing_mirror")
        self.assertEqual(self.fake.row_requests(), [])
        self.transport.down = None
        self.fake.catalog["public.app_shipment"]["unavailable"] = "relation_not_ready"
        mirror, report = self.serve()
        self.assertEqual((report["complete"], report["persisted"]["reason"]),
                         (False, "incomplete"))
        mirror.close()
        self.transport.down = HTTP_500
        with self.assertRaises(MirrorError) as failed:
            self.serve()
        self.assertEqual(failed.exception.code, "missing_mirror")

    def test_a_copy_that_does_not_verify_is_refused(self):
        self.persist_live()
        (manifest_path,) = (self.store.root / "scopes").glob("*/manifest.json")
        manifest = json.loads(manifest_path.read_text())
        snapshot = manifest_path.parent / manifest["file"]
        self.transport.down = HTTP_500
        conn = sqlite3.connect(snapshot)
        conn.execute("DELETE FROM relation_state WHERE relation = 'public.app_shipment'")
        conn.commit()
        conn.close()
        manifest_path.write_text(json.dumps(dict(manifest, sha256=mc._file_sha(snapshot))))
        with self.assertRaises(MirrorError) as refused:
            self.serve()
        self.assertEqual(refused.exception.code, "persisted_incomplete")
        manifest_path.write_text(json.dumps(dict(manifest, complete=False)))
        with self.assertRaises(MirrorError) as refused:
            self.store.load(RELATIONS, tenant="plx")
        self.assertEqual(refused.exception.code, "persisted_incomplete")

    def test_a_narrower_copy_never_answers_a_wider_request(self):
        self.serve(relations=["public.app_shipment"])[0].close()
        self.transport.down = HTTP_500
        with self.assertRaises(MirrorError) as refused:
            self.serve()
        self.assertEqual(refused.exception.code, "persisted_incomplete")
        narrow, report = self.serve(relations=["public.app_shipment"])
        self.assertEqual(report["served_from"], "persisted_capture")
        with self.assertRaises(MirrorError) as missing:
            narrow.rows(SALES, "public.app_sales_order")
        self.assertEqual(missing.exception.code, "missing_mirror")

    def test_load_takes_the_newest_copy_holding_the_relation(self):
        self.persist_live()
        self.clock.advance(86400)
        self.serve(relations=["public.app_shipment"],
                   mirror_path=self.root / "second.sqlite")[0].close()
        manifest = self.store.load(["public.app_shipment"], tenant="plx")
        self.assertEqual(manifest["relations"], ["public.app_shipment"])
        self.assertTrue(manifest["captures_as_of"].startswith("2026-09-30"))

    def test_a_sync_with_a_refusal_among_outages_is_not_served_from_the_copy(self):
        self.persist_live()
        original = self.transport.fake

        def mixed(url, headers, *, max_bytes):
            path = urlsplit(url).path
            if path.endswith("/public.app_shipment/rows"):
                raise SourceHttpError("x", status=403, error_code="forbidden")
            if path.endswith("/rows"):
                raise HTTP_500
            return original(url, headers, max_bytes=max_bytes)
        self.transport.fake = mixed
        mirror, report = self.serve()
        self.assertEqual((report["served_from"], report["complete"]), ("live", False))

    def test_the_store_is_outside_git_and_only_an_explicitly_named_one(self):
        with self.assertRaises(CaptureError):
            mc.MirrorCaptureStore(Path(__file__).resolve().parent)
        self.assertEqual(mc.default_state_dir({"PLX_WORKBENCH_STATE_DIR": "/srv/plx"}),
                         Path("/srv/plx"))
        self.assertIsNone(mc.store_from_env({"XDG_STATE_HOME": "/x", "HOME": "/h"}))

    def test_only_an_outage_counts_as_live_source_unavailable(self):
        self.assertTrue(mc.is_outage("source_unavailable", 500))
        self.assertTrue(mc.is_outage("source_unavailable", None))  # unreachable
        self.assertTrue(mc.is_outage("source_throttled", 429))
        for status in (401, 403, 404):
            self.assertFalse(mc.is_outage("source_unavailable", status))
        self.assertFalse(mc.is_outage("not_visible", None))


def carrier_world(schema):
    """A FakeWorkbench serving every Portal carrier relation of the compiled schema with
    two SYNTHETIC rows each: one grammar-valid value per plain carrier column."""
    kinds = {prop.get("identifier_kind") for spec in schema.object_types.values()
             for prop in spec.get("properties", ())
             if prop.get("value_type") == "identifier"} - {None}
    columns: dict[str, dict[str, str]] = {}
    for carrier in resolve.carriers(schema, kinds):
        if carrier.source == "portal_api":
            columns.setdefault(carrier.relation, {})[carrier.path.split(".")[0]] = (
                "" if "." in carrier.path else carrier.kind)
    fake = FakeWorkbench()
    fake.catalog, fake.tables = {}, {}
    for number, (relation, cells) in enumerate(sorted(columns.items())):
        names = sorted(set(cells) | {"id", "updated_at"})
        fake.catalog[relation] = {"columns": names, "key": ["id"], "domain": "test",
                                  "since": {"column": "updated_at", "mode": "timestamp-utc"}}
        rows = []
        for index in range(2):
            row = {"id": f"r{number}-{index}", "updated_at": "2026-09-28T08:00:00Z"}
            for column, kind in cells.items():
                if column in row:
                    continue
                value = None
                for form in (f"SYNX{number:03d}{index}7", f"{number + 1:03d}{index}7"):
                    if kind and identifiers.normalize(kind, form) is not None:
                        value = form
                        break
                row[column] = value
            rows.append(row)
        fake.tables[relation] = rows
    return fake


class LiveCheckSpecTests(PersistedTestCase):
    """``verify/live-checks/mirror.persisted_fallback.json`` and its probe over an
    injected client, in the live runner's condition: a state directory that starts
    empty (each live run met a fresh one and failed ``missing_mirror``).
    No network; ``PLX_WORKBENCH_STATE_DIR`` names a temporary directory."""

    def setUp(self) -> None:
        super().setUp()
        self.transport = Outage(carrier_world(self.schema))
        self.state = self.root / "verifier-state"

    def spec(self):
        from verify import live_verify as lv
        return json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.persisted_fallback.json")
                          .read_text())

    def probe(self, state=None, branch=None):
        from verify import live_verify as lv
        from scripts import live_read_probes as probes
        spec = self.spec()
        clients = {"mirror-fallback": self.client()}
        if branch is not None:
            clients["mirror-branch"] = branch
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = probes.main(spec["command"][3:],
                               environ={"PLX_WORKBENCH_STATE_DIR": str(state or self.state)},
                               clients=clients)
        report = json.loads(out.getvalue())
        return report, lv.check_expectations(spec, code, report)

    def verdict(self, report):
        """The live check's result for this probe output: ``run_spec`` over the real
        spec, its command replaced by a stub that prints ``report``."""
        from verify import live_verify as lv
        printed = self.root / "stdout.json"
        printed.write_text(json.dumps(report))
        stub = self.root / "stub-python"
        stub.write_text(f"#!/bin/sh\ncat '{printed}'\n")
        stub.chmod(0o755)
        return lv.run_spec(self.spec(), python=str(stub), environ={
            "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY,
            "PLX_WORKBENCH_STATE_DIR": str(self.state)})

    def test_the_spec_is_valid_get_only_and_independent_of_the_health_check(self):
        from verify import live_verify as lv
        spec = self.spec()
        self.assertEqual(lv.validate_spec(spec), [])
        self.assertIn(spec["command"][2], lv.ALLOWED_MODULES)
        self.assertNotIn("requires", spec)  # it must run while portal.health_catalog fails
        # The store is the durable one the live check keeps, never the per-check {tmp}.
        self.assertIn("PLX_WORKBENCH_STATE_DIR", spec["optional_env"])
        self.assertFalse(any("{tmp}" in arg for arg in spec["command"]), spec["command"])
        # Unset, the probe runs and blocks naming it (a host misconfiguration), before any
        # request: never a skip, never a pass.
        check = lv.run_spec(spec, environ={"PLX_WORKBENCH_API_URL": BASE,
                                           "PLX_WORKBENCH_API_KEY": KEY})
        self.assertEqual((check["result"], check["detail"]["reason"],
                          check["detail"]["cause"],
                          check["detail"]["fields"]["missing_settings"]),
                         ("blocked", "upstream portal-workbench-api state_dir_unset",
                          "host_misconfiguration", ["PLX_WORKBENCH_STATE_DIR"]), check)

    def test_a_never_synced_store_is_blocked_then_the_copy_answers_a_later_outage(self):
        self.transport.down = HTTP_500
        # A copy of another scope (the host's relations) cannot answer this probe's
        # relations: it does not turn "never synced" into a failure.
        (self.state / mc.STORE_DIR).mkdir(parents=True)
        (self.state / mc.STORE_DIR / "history.json").write_text(json.dumps(
            {"schema": mc.HISTORY_SCHEMA, "persisted": 1, "persisted_scopes": ["0" * 16]}))
        report, failures = self.probe()  # no copy of these relations: the live runner's case
        self.assertEqual((report["served_from"], report["error_code"], report["blocked"],
                          report["store_runs_seen"], report["store_persists_between_runs"]),
                         ("none", "missing_mirror", "never_synced_new_store", 1, False), report)
        self.assertIn("answered != expected", failures)  # never a pass
        check = self.verdict(report)
        self.assertEqual((check["result"], check["detail"]["reason"]),
                         ("blocked", "upstream portal-workbench-api never_synced_new_store"),
                         check)
        self.assertNotIn("failures", check["detail"])

        report, _ = self.probe()  # same directory, still out, still nothing captured
        self.assertEqual((report["blocked"], report["store_runs_seen"],
                          report["store_persists_between_runs"]),
                         ("never_synced", 2, True), report)

        self.transport.down = None
        report, failures = self.probe()
        self.assertEqual(failures, [], report)
        self.assertEqual((report["served_from"], report["persisted"],
                          report["store_persisted_count"]), ("live", True, 2), report)
        self.assertNotIn("blocked", report)

        self.transport.down = HTTP_500
        rows_before = len(self.transport.fake.row_requests())
        report, failures = self.probe()
        self.assertEqual(failures, [], report)
        self.assertEqual((report["served_from"], report["http_status"],
                          report["upstream_error_code"], report["store_runs_seen"]),
                         ("persisted_capture", 500, "internal", 4), report)
        self.assertEqual(report["finds_labelled"], report["finds_with_candidates"])
        self.assertGreaterEqual(report["captures_age_hours"], 0)
        self.assertEqual(len(self.transport.fake.row_requests()), rows_before)
        self.assertEqual(self.verdict(report)["result"], "pass")
        history = json.loads((self.state / mc.STORE_DIR / "history.json").read_text())
        self.assertEqual(sorted(history), ["first_run_at", "last_persisted_at", "persisted",
                                           "persisted_scopes", "runs", "schema"])

    def test_a_store_holding_no_copy_serves_the_one_on_the_mirror_branch(self):
        """The live check's container keeps no store,
        so an outage is answered from the copy an earlier run published on the
        non-branch ref ``refs/verify/mirror`` and the live check attested on
        the results branch (a local bare repository here; an injected client never
        reads the real one)."""
        from integrations import mirror_branch_store as mbs
        from integrations.tests.test_mirror_branch_store import publish_attested

        def git(*args, **kwargs):
            return subprocess.run(["git", *args], check=True, capture_output=True,
                                  **kwargs).stdout.decode().strip()
        bare, repo = self.root / "remote.git", self.root / "repo"
        git("init", "-q", "--bare", str(bare))
        git("init", "-q", str(repo))
        git("-C", str(repo), "remote", "add", "origin", str(bare))
        branch = mbs.MirrorBranch(repo)
        command = self.spec()["command"]
        self.assertEqual(command[command.index("--mirror-ref") + 1], mbs.REF)
        report, failures = self.probe(branch=branch)  # live: complete, persisted locally
        self.assertEqual((failures, report["served_from"], report["branch_status"]),
                         ([], "live", "not_needed"), report)
        self.assertTrue(publish_attested(branch, mc.MirrorCaptureStore(self.state),
                                         {"PLX_WORKBENCH_API_KEY": KEY})["written"])

        self.transport.down = HTTP_500
        report, _ = self.probe(self.root / "run-2")  # the injected client alone
        self.assertEqual((report["branch_status"], report.get("blocked")),
                         ("not_configured", "never_synced_new_store"), report)
        report, failures = self.probe(self.root / "run-3", branch)
        self.assertEqual(failures, [], report)
        self.assertEqual((report["served_from"], report["branch_status"],
                          report["branch_source"], report["http_status"],
                          report["store_runs_seen"]),
                         ("persisted_capture", "restored", "remote", 500, 1), report)
        self.assertEqual(report["finds_labelled"], report["finds_with_candidates"])
        self.assertEqual(self.verdict(report)["result"], "pass")

        # A copy that does not verify is a failure, never "nothing yet".
        head = git("-C", str(bare), "rev-parse", mbs.REF)
        manifest = json.loads(git("-C", str(bare), "show", f"{head}:manifest.json"))
        manifest["parts"][0]["sha256"] = "0" * 64
        blob = git("-C", str(bare), "hash-object", "-w", "--stdin",
                   input=json.dumps(manifest).encode())
        listing = git("-C", str(bare), "ls-tree", head).replace(
            git("-C", str(bare), "rev-parse", f"{head}:manifest.json"), blob)
        tree = git("-C", str(bare), "mktree", input=(listing + "\n").encode())
        bad = git("-C", str(bare), "-c", "user.name=t", "-c", "user.email=t@t",
                  "commit-tree", tree, "-m", "tampered")
        git("-C", str(bare), "update-ref", mbs.REF, bad)
        report, failures = self.probe(self.root / "run-4", branch)
        self.assertEqual((report["served_from"], report["branch_status"]),
                         ("none", "refused"), report)
        self.assertNotIn("blocked", report)
        self.assertIn("answered != expected", failures)

    def test_a_copy_published_here_that_cannot_be_served_is_a_failure_not_blocked(self):
        self.probe()
        self.transport.down = HTTP_500
        for manifest in (self.state / mc.STORE_DIR / "scopes").glob("*/manifest.json"):
            manifest.unlink()
        for copy in (self.state / mc.STORE_DIR / "scopes").glob("*/mirror-*.sqlite"):
            copy.unlink()
        report, failures = self.probe()
        self.assertEqual((report["served_from"], report["error_code"],
                          report["store_persisted_count"]), ("none", "missing_mirror", 1))
        self.assertNotIn("blocked", report)
        self.assertIn("answered != expected", failures)
        self.assertEqual(self.verdict(report)["result"], "fail")


if __name__ == "__main__":
    unittest.main()
