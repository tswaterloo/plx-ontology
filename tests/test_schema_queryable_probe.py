"""``schema.portal_queryable`` (``scripts/live_read_probes.schema_queryable``): every bound
Portal relation gets its own outcome under one total deadline, and the source registry
credits a relation only by the outcome the spec's ``registry_names`` declares for it.

Directives 45e322ffbdd8, 85b59fc54cca (1, 2): one failing relation no longer aborts the
probe or fails the rest; a relation read empty is unexercised, never queryable; the
deadline cuts the run and it reports incomplete. Decision e51ad849b488
(``devloop/DECISIONS/2026-10-07-portal-queryable-floor.md``) and directive f568d4a5e68c: the
check passes only when the run is complete and the queryable share of the bound relations
reaches the floor the spec declares (the probe reads it there), and the recorded
``not_queryable`` ids survive the verifier's detail cap.

Evidence label: injected-transport (schema-generated rows, ``ontology/generator/fixture.py``),
never live. No network I/O, no credentials.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
import inspect
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from devloop import live_verify as lv
from devloop.tests.test_live_wiring import argv_of, run_main, spec
from ontology.schema import load_schema
from scripts import live_read_probes as probes
from scripts import source_registry as sr

SPEC_ID = "schema.portal_queryable"
# Read empty live at 2026-10-07T07:51Z (gaps gap-1cda0e1f879f, gap-494946938ce8, gap-984fde40c1e2).
EMPTY_LIVE = ("public.app_manufacturing_job", "public.app_pr_line_item",
              "public.app_purchase_requisition", "public.app_receipt", "public.app_receipt_line",
              "public.app_ship_release", "public.app_shipment_line")


# Daily 2026-10-21T01:07Z (gap-0b8675835265): bound relations the catalog marked unavailable
# and ones absent from it (catalog_absent), beside the EMPTY_LIVE ones.
UNAVAILABLE_LIVE = ("public.assembly_order_status", "public.batch", "public.production",
                    "silver.assembly_order_statuses", "silver.batches",
                    "silver.production_tasks")
ABSENT_LIVE = ("public.app_approval_record", "public.app_supplier_product_tier",
               *(f"sage.{name}" for name in (
                   "ap_open_document", "ap_term", "ap_vendor", "ap_vendor_remit",
                   "gl_account", "gl_batch", "gl_je_detail", "gl_je_header")))


class SchemaWorld:
    """The Portal reporting API's catalog (the pinned snapshot) serving rows generated from
    the compiled schema's own bindings (``ontology/generator/fixture.py``), every approved
    column present; ``unavailable``/``broken`` relations answer as the client would and
    each read advances ``now`` by one second (the probe's clock)."""

    def __init__(self, schema, *, unavailable=(), broken=()):
        from ontology import schema as ontology_schema
        from ontology.generator import fixture
        snapshot = json.loads(ontology_schema.CATALOG_SNAPSHOT_PATH.read_text("utf-8"))
        self.catalog = {e["name"]: dict(e, unavailable="relation not found"
                                        if e["name"] in unavailable else e["unavailable"])
                        for e in snapshot["relations"]}
        self.broken = set(broken)
        self.now = 0.0
        source = fixture.SyntheticCaptures(schema, "portal_api", 2 * fixture.min_rows(schema))
        records = source.records(sorted(self.catalog))
        self.rows = {}
        for name, entry in self.catalog.items():
            first = {}
            for record in records[name]:
                first.setdefault(record.record_key, record.row)
            self.rows[name] = [{**{c: None for c in entry["columns"]}, **row}
                               for row in first.values()]

    def relations(self):
        return self.catalog

    def read_page(self, name, entry, *, columns, page_size, cursor=None, since=None):
        from integrations.workbench_read_api import SourceUnavailable
        self.now += 1.0
        if name in self.broken:
            raise SourceUnavailable("workbench API row page invalid")
        return {"relation": name, "columns": sorted(columns), "rows": self.rows[name][:page_size],
                "next_cursor": None}


def _live(spec_doc: dict, detail: dict, result: str) -> list[dict]:
    return [{"_from": "daily", "generated_at": "2026-10-07T00:00:00Z", "checks": [{
        "id": spec_doc["id"], "source": spec_doc["source"], "result": result,
        "detail": detail}]}]


class SchemaQueryableProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()
        cls.bound = cls.schema.portal_bindings()
        cls.spec = spec(SPEC_ID)

    def run_spec(self, world):
        """The spec's own argv over ``world``, recorded as the verifier records it
        (devloop/live_verify.py run_spec: blocked parts, the full expectation's failures as
        unverified, the fields, completeness, failures) and capped with the spec, as the
        verifier caps it, so the cap sees the real size and keeps what the spec asserts."""
        code, document = run_main(probes.main, argv_of(self.spec, "/unused"), environ={},
                                  clients={"schema-queryable": world})
        detail = {}
        failures = lv.check_expectations(self.spec, code, document)
        parts = lv.blocked_parts(document)
        if parts:
            detail["blocked"] = {"parts": parts, "cause": parts[0]["cause"]}
            if failures:
                detail["unverified"] = failures
            failures = lv.check_expectations(self.spec, code, document,
                                             self.spec["expect_blocked"])
        detail["fields"] = lv._recorded(self.spec, document)
        detail.update(lv.completeness(self.spec, document))
        if failures:
            detail["failures"] = failures
        return code, document, lv._cap(lv.scrub_tree(detail, ()), self.spec)

    def assert_every_relation_counted_once(self, document):
        self.assertEqual(sum(document[k] for k in (
            "queryable_count", "read_empty_count", "unsampled_count", "unavailable_count",
            "unreached_count", "not_queryable_count")), len(self.bound))

    def uncovered(self, detail, result="pass"):
        rows = sr.gaps(sr.load_registry(), _live(self.spec, detail, result),
                       sr.RepoFacts())["uncovered"]
        return {row["id"]: row for row in rows}

    def test_every_bound_relation_is_named_and_the_registry_credits_it(self):
        code, document, detail = self.run_spec(SchemaWorld(self.schema))
        self.assertEqual(lv.check_expectations(self.spec, code, document), [])
        self.assertEqual(lv.completeness(self.spec, document), {"complete": True})
        self.assertEqual(document["evidence_label"], "injected-transport")
        self.assertEqual(document["named_relation_count"], len(self.bound))
        self.assertEqual((document["queryable_count"], document["queryable_share"],
                          document["ok"]), (len(self.bound), 1.0, True))
        self.assertEqual((document["unavailable"], document["blocked_upstream"]), ({}, []))
        self.assert_every_relation_counted_once(document)
        self.assertGreaterEqual(document["bindings_probed"], document["queryable_count"])
        self.assertNotIn("fields_truncated", detail)
        recorded = json.dumps(detail)
        for value in ("PO1000", "api://", "text 1"):
            self.assertNotIn(value, recorded)
        rows = list(self.uncovered(detail).values())
        self.assertEqual([r["id"] for r in rows if r["system"] == "portal-reporting-api"], [])
        self.assertEqual({r["reason"] for r in rows if r["kind"] == "prisma_model"},
                         {"not_served"})
        self.assertFalse([r for r in rows if r["kind"] == "api_route" and r["reason"] in
                          ("no_live_check", "live_not_passing", "live_unexercised")])
        # The same names in a failed check (a leak, say) credit nothing as passing.
        failed = self.uncovered(detail, "fail")
        self.assertEqual(failed["portal-reporting-api.relation:public.app_sales_order"]["reason"],
                         "live_not_passing")

    def test_a_failing_relation_fails_only_itself_and_an_empty_one_is_unexercised(self):
        world = SchemaWorld(self.schema, broken={"public.receipt"})
        catalog_key = list(world.catalog["public.app_customer"]["key"])
        world.rows["public.app_customer"] = [{k: v for k, v in row.items()
                                              if k not in catalog_key}
                                             for row in world.rows["public.app_customer"]]
        world.rows["public.app_sales_order"] = []
        broken_binding = self.bound["public.app_shipment"][0][1]["id"]
        real = sr.probe_binding

        def probe_binding(tools, actor, spec_doc, bid):
            if bid == broken_binding:
                raise RuntimeError("binding read broke")
            return real(tools, actor, spec_doc, bid)

        with mock.patch.object(sr, "probe_binding", probe_binding):
            code, document, detail = self.run_spec(world)
        # Each failing relation is named with its own code; the others were still evaluated.
        self.assertEqual(document["not_queryable"], {
            "public_app": ["app_customer", "app_shipment"], "public_fm": ["receipt"]})
        self.assertEqual(document["not_queryable_codes"], {
            "probe_RuntimeError": 1, "project_KeyError": 1, "read_SourceUnavailable": 1})
        self.assertEqual(detail["fields"]["not_queryable_codes"],
                         document["not_queryable_codes"])
        self.assertEqual(document["read_empty"], {"public_app": ["app_sales_order"]})
        self.assertEqual((document["unavailable_count"], document["unreached_count"]), (0, 0))
        self.assertEqual(document["queryable_count"], len(self.bound) - 4)
        # Three failing and one empty of the bound relations: above the floor.
        self.assertEqual(document["queryable_share"],
                         int((len(self.bound) - 4) / len(self.bound) * 10000) / 10000)
        self.assertIs(document["ok"], True)
        self.assert_every_relation_counted_once(document)
        self.assertEqual(lv.check_expectations(self.spec, code, document), [])
        credited = {n for group in detail["fields"]["queryable"].values() for n in group}
        self.assertFalse(credited & {"receipt", "app_customer", "app_shipment",
                                     "app_sales_order"})
        self.assertEqual(lv.completeness(self.spec, document), {"complete": True})
        # The registry: only the failing relations are live_not_passing, the empty one is
        # unexercised (bound, nothing read back), every other served relation is evaluated.
        rows = self.uncovered(detail)
        portal = {row["name"]: row["reason"] for row in rows.values()
                  if row["system"] == "portal-reporting-api"}
        self.assertEqual(portal, {"public.app_customer": "live_not_passing",
                                  "public.app_shipment": "live_not_passing",
                                  "public.receipt": "live_not_passing",
                                  "public.app_sales_order": "live_unexercised"})
        self.assertEqual(rows["portal.prisma_model:SalesOrder"]["reason"], "live_unexercised")

    def test_below_the_floor_fails_naming_the_share_and_the_record_keeps_every_failing_id(self):
        keep = "public.app_sales_order"
        world = SchemaWorld(self.schema, broken=set(self.bound) - {keep, *EMPTY_LIVE})
        for name in EMPTY_LIVE:
            world.rows[name] = []
        code, document, detail = self.run_spec(world)
        failing = len(self.bound) - 1 - len(EMPTY_LIVE)
        # Empty relations count against the share: 1 of the bound relations.
        self.assertEqual((document["queryable_count"], document["not_queryable_count"],
                          document["read_empty_count"]), (1, failing, len(EMPTY_LIVE)))
        self.assertEqual(document["queryable_share"], int(10000 / len(self.bound)) / 10000)
        self.assertIs(document["ok"], False)
        self.assertEqual(document["queryable_floor"],
                         self.spec["expect"]["json_min"]["queryable_share"])
        failures = lv.check_expectations(self.spec, code, document)
        self.assertIn("queryable_share < 0.9", failures)
        self.assertIn("ok != expected", failures)
        # The capped record still lists every failing relation by id, and the names it credits.
        self.assertNotIn("fields_truncated", detail)
        recorded = {n for group in detail["fields"]["not_queryable"].values() for n in group}
        self.assertEqual(len(recorded), failing)
        self.assertEqual(detail["fields"]["read_empty"], {"public_app": sorted(
            n.split(".", 1)[1] for n in EMPTY_LIVE)})
        portal = {row["name"]: row["reason"] for row in self.uncovered(detail, "fail").values()
                  if row["system"] == "portal-reporting-api"}
        self.assertEqual({n for n, r in portal.items() if r == "live_unexercised"},
                         set(EMPTY_LIVE))
        self.assertEqual({n for n, r in portal.items() if r == "no_live_check"}, set())
        self.assertEqual(len([r for r in portal.values() if r == "live_not_passing"]),
                         failing + 1)   # the queryable one too: the check failed
        # The fewest queryable relations that reach the floor pass, one fewer fails.
        ranked = sorted(self.bound)
        floor = Decimal(str(self.spec["expect"]["json_min"]["queryable_share"]))
        need = int((floor * len(ranked)).to_integral_value(ROUND_CEILING))
        for broken, ok in ((len(ranked) - need, True), (len(ranked) - need + 1, False)):
            document = probes.schema_queryable(
                {}, client=SchemaWorld(self.schema, broken=ranked[:broken]))
            with self.subTest(broken=broken):
                self.assertEqual(document["queryable_count"], len(ranked) - broken)
                self.assertIs(document["ok"], ok)
                self.assertEqual(lv.check_expectations(self.spec, 0, document) == [], ok)

    def test_catalog_unavailable_relations_are_named_and_block_the_run_never_pass(self):
        # One public and one other-schema bound relation the catalog marks unavailable
        # (the shape of daily c80c4e42's unavailable 2).
        marked = [sorted(n for n in self.bound if n.startswith("public.app_"))[0],
                  sorted(n for n in self.bound if not n.startswith("public."))[0]]
        world = SchemaWorld(self.schema, unavailable=set(marked))
        code, document, detail = self.run_spec(world)
        schema_name = marked[1].split(".", 1)[0]
        named = {"public_app": [marked[0].split(".", 1)[1]], schema_name: [marked[1]]}
        self.assertEqual(document["unavailable"], named)
        self.assertEqual(detail["fields"]["unavailable"], named)
        self.assertEqual(lv.blocked_parts(document), [
            {"part": name, "source": "portal-workbench-api", "cause": "relation_unavailable",
             "reason": "relation not found"} for name in sorted(marked)])
        self.assertEqual((document["unavailable_count"], document["queryable_count"]),
                         (2, len(self.bound) - 2))
        self.assert_every_relation_counted_once(document)
        self.assertEqual((document["ok"], document["served_ok"]), (False, True))
        self.assertEqual(lv.completeness(self.spec, document)["incomplete"],
                         ["unavailable_count"])
        # As the verifier scores it: parts present, so expect_blocked decides; it is met and
        # measured (blocked in part), and the full expect stays unmet (never a pass).
        self.assertEqual(lv.check_expectations(self.spec, code, document), ["ok != expected"])
        self.assertEqual(lv.check_expectations(self.spec, code, document,
                                               self.spec["expect_blocked"]), [])
        self.assertTrue(lv._positive_minimum(self.spec["expect_blocked"]))
        portal = {row["name"]: row["reason"] for row in self.uncovered(detail, "blocked").values()
                  if row["system"] == "portal-reporting-api"}
        self.assertEqual(set(portal), set(self.bound))   # a blocked run credits nothing
        # Any other shortfall beside them (here the share under the floor) fails outright.
        rest = sorted(set(self.bound) - set(marked))
        world = SchemaWorld(self.schema, unavailable=set(marked), broken=rest[:len(rest) // 2])
        document = probes.schema_queryable({}, client=world)
        self.assertEqual((document["ok"], document["served_ok"]), (False, False))
        self.assertIn("served_ok != expected", lv.check_expectations(
            self.spec, 0, document, self.spec["expect_blocked"]))

    def test_the_capped_record_of_a_mixed_failing_run_keeps_every_name_list(self):
        # gap-0b8675835265: the daily of 2026-10-21 (6 unavailable, 10 catalog_absent, 7
        # empty) recorded 3,936 characters with read_empty dropped by the cap, so the two
        # empty MRP routes' models read no_live_check instead of live_unexercised.
        world = SchemaWorld(self.schema)
        for name in UNAVAILABLE_LIVE:
            world.catalog[name]["unavailable"] = "1 approved column is missing or not granted"
        for name in ABSENT_LIVE:
            del world.catalog[name]
        for name in EMPTY_LIVE:
            world.rows[name] = []
        code, document, detail = self.run_spec(world)
        self.assertEqual((document["unavailable_count"], document["not_queryable_count"],
                          document["read_empty_count"], document["served_ok"]),
                         (len(UNAVAILABLE_LIVE), len(ABSENT_LIVE), len(EMPTY_LIVE), False))
        self.assertIn("blocked", detail)
        self.assertLessEqual(len(json.dumps(detail, sort_keys=True)), lv.MAX_DETAIL_CHARS)
        # Every name list survives the cap, whole, and so do the failure codes and the
        # unreached count (relations never named) ahead of the counts a list carries.
        fields = detail["fields"]
        for key in ("queryable", "not_queryable", "unavailable", "read_empty",
                    "not_queryable_codes", "unreached_count"):
            with self.subTest(kept=key):
                self.assertEqual(fields.get(key), document[key])
        self.assertEqual(fields["not_queryable_codes"], {"catalog_absent": len(ABSENT_LIVE)})
        rows = self.uncovered(detail, "fail")
        portal = {row["name"]: row["reason"] for row in rows.values()
                  if row["system"] == "portal-reporting-api"}
        self.assertEqual({n for n, r in portal.items() if r == "live_unexercised"},
                         set(EMPTY_LIVE))
        self.assertEqual({n for n, r in portal.items() if r == "no_live_check"}, set())
        for route in ("portal/src/app/api/mrp/generate-po/requisition/[id]",
                      "portal/src/app/api/mrp/wms/ship-releases"):
            with self.subTest(route=route):
                self.assertEqual(rows[f"portal.api_route:{route}"]["reason"],
                                 "live_unexercised")

    def test_a_floor_breach_caused_only_by_unavailable_relations_is_blocked_not_failed(self):
        ranked = sorted(self.bound)
        marked = ranked[:len(ranked) // 5]   # over a tenth unavailable: the full share < 0.9
        document = probes.schema_queryable(
            {}, client=SchemaWorld(self.schema, unavailable=set(marked)))
        self.assertLess(document["queryable_share"], document["queryable_floor"])
        self.assertEqual((document["ok"], document["served_ok"]), (False, True))
        self.assertEqual(lv.check_expectations(self.spec, 0, document,
                                               self.spec["expect_blocked"]), [])
        self.assertEqual({p["cause"] for p in lv.blocked_parts(document)},
                         {"relation_unavailable"})
        # One relation failing beside them still fails.
        document = probes.schema_queryable({}, client=SchemaWorld(
            self.schema, unavailable=set(marked), broken=ranked[-len(ranked) // 5:]))
        self.assertFalse(document["served_ok"])

    def test_a_bound_relation_absent_from_the_catalog_is_catalog_drift_a_failure(self):
        absent = sorted(self.bound)[0]
        world = SchemaWorld(self.schema)
        del world.catalog[absent]
        document = probes.schema_queryable({}, client=world)
        self.assertEqual(document["unavailable_count"], 0)
        self.assertEqual(document["blocked_upstream"], [])
        self.assertEqual(document["not_queryable_codes"], {"catalog_absent": 1})
        self.assertEqual(sum(len(v) for v in document["not_queryable"].values()), 1)
        self.assert_every_relation_counted_once(document)

    def test_the_floor_is_declared_once_in_the_spec_and_the_probe_reads_it(self):
        self.assertIsNone(re.search(r"\b0\.9\b", inspect.getsource(probes)))
        one_broken = sorted(self.bound)[:1]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            for floor, ok in (("1", False), ("0.5", True)):
                path.write_text(json.dumps(self.spec).replace(
                    '"queryable_share": 0.9', '"queryable_share": ' + floor), "utf-8")
                with self.subTest(floor=floor), mock.patch.object(probes, "QUERYABLE_SPEC", path):
                    document = probes.schema_queryable(
                        {}, client=SchemaWorld(self.schema, broken=one_broken))
                    self.assertEqual(document["queryable_floor"], float(floor))
                    self.assertIs(document["ok"], ok)
            # A missing or malformed floor (or completeness) fails closed before any read.
            for bad in ({"json_min": {}}, {"json_min": {"queryable_share": "0.9"}},
                        {"json_min": {"queryable_share": 0}},
                        {"json_min": {"queryable_share": 1.5}}):
                path.write_text(json.dumps(dict(self.spec, expect=bad)), "utf-8")
                with self.subTest(bad=bad), mock.patch.object(probes, "QUERYABLE_SPEC", path), \
                        self.assertRaises(ValueError):
                    probes.schema_queryable({}, client=None)
            path.write_text(json.dumps({k: v for k, v in self.spec.items()
                                        if k != "complete_when"}), "utf-8")
            with mock.patch.object(probes, "QUERYABLE_SPEC", path), \
                    self.assertRaises(ValueError):
                probes.schema_queryable({}, client=None)

    def test_the_deadline_cuts_the_run_and_an_incomplete_run_never_passes(self):
        world = SchemaWorld(self.schema)   # each read takes one second of the probe's clock
        document = probes.schema_queryable({}, client=world, budget_seconds=1,
                                           clock=lambda: world.now)
        # One read fits; what it read is still projected and probed (the grace). 1 queryable
        # and 92 unreached measures the bound catalog: far under the floor, and incomplete.
        self.assertEqual((document["queryable_count"], document["unreached_count"],
                          document["named_relation_count"]), (1, len(self.bound) - 1, 1))
        self.assertEqual(document["queryable_share"], int(10000 / len(self.bound)) / 10000)
        self.assert_every_relation_counted_once(document)
        failures = lv.check_expectations(self.spec, 0, document)
        self.assertIn("queryable_share < 0.9", failures)
        self.assertIn("ok != expected", failures)
        self.assertEqual(lv.completeness(self.spec, document)["incomplete"],
                         ["unreached_count"])
        # Past the grace too, the pages read are unreached as well, never named.
        world = SchemaWorld(self.schema)
        late = probes.schema_queryable(
            {}, client=world, budget_seconds=5,
            clock=lambda: world.now + (probes.QUERYABLE_GRACE_SECONDS if world.now >= 5 else 0))
        self.assertEqual((late["unreached_count"], late["named_relation_count"]),
                         (len(self.bound), 0))
        self.assertIs(late["ok"], False)
        # Above the floor but incomplete (one unreached, or one unavailable) still fails.
        world = SchemaWorld(self.schema)
        short = probes.schema_queryable({}, client=world, budget_seconds=len(self.bound) - 1,
                                        clock=lambda: world.now)
        gone = probes.schema_queryable({}, client=SchemaWorld(
            self.schema, unavailable={sorted(self.bound)[0]}))
        for document, field in ((short, "unreached_count"), (gone, "unavailable_count")):
            with self.subTest(field=field):
                self.assertEqual(document[field], 1)
                self.assertGreaterEqual(document["queryable_share"],
                                        self.spec["expect"]["json_min"]["queryable_share"])
                self.assertIs(document["ok"], False)
                self.assertEqual(lv.check_expectations(self.spec, 0, document),
                                 ["ok != expected"])
                self.assertEqual(lv.completeness(self.spec, document)["incomplete"], [field])
        with self.assertRaises(ValueError):
            probes.schema_queryable({}, client=world, budget_seconds=0)

    def test_the_spec_deadline_covers_every_read_timing_out_and_the_timeout_covers_it(self):
        # gap-427f2d196680: with every read taking the client's whole timeout (the catalog
        # read too), the spec's own --budget-seconds still reaches every bound relation, and
        # timeout_seconds exceeds that budget plus one read in flight plus the probe's grace,
        # under the daily ceiling.
        from integrations import workbench_read_api as wra
        with mock.patch.object(wra, "get_json", return_value={}) as get_json:
            wra._get_workbench_json("https://example.invalid/r", {}, max_bytes=1)
        read_timeout = get_json.call_args.kwargs["timeout_seconds"]
        command = self.spec["command"]
        budget = float(command[command.index("--budget-seconds") + 1])
        timeout = self.spec["timeout_seconds"]
        self.assertGreaterEqual(len(self.bound), 103)
        self.assertGreater(budget, (len(self.bound) + 1) * read_timeout)
        self.assertGreater(timeout, budget + read_timeout + probes.QUERYABLE_GRACE_SECONDS)
        self.assertLessEqual(timeout, lv.MAX_TIMEOUT_BY_CADENCE[self.spec["cadence"]])
        self.assertEqual(probes.QUERYABLE_BUDGET_SECONDS, budget)   # the CLI default agrees

        class SlowWorld(SchemaWorld):
            def relations(self):
                self.now += read_timeout
                return super().relations()

            def read_page(self, *args, **kwargs):
                self.now += read_timeout - 1.0   # SchemaWorld adds the last second
                return super().read_page(*args, **kwargs)

        world = SlowWorld(self.schema)
        document = probes.schema_queryable({}, client=world, budget_seconds=budget,
                                           clock=lambda: world.now)
        self.assertEqual(document["unreached_count"], 0)
        self.assertGreaterEqual(world.now, len(self.bound) * read_timeout)
        self.assert_every_relation_counted_once(document)
        self.assertEqual(lv.completeness(self.spec, document), {"complete": True})
        self.assertEqual(lv.check_expectations(self.spec, 0, document), [])

    def test_registry_names_must_name_known_kinds_of_recorded_paths(self):
        lv.validate_spec(self.spec)
        for names in ({"pass": ["queryable"], "maybe": ["read_empty"]},
                      {"pass": ["rows_read"]}, {}, {"fail": "not_queryable"}):
            with self.subTest(names=names), self.assertRaises(lv.SpecError):
                lv.validate_spec(dict(self.spec, registry_names=names))


if __name__ == "__main__":
    unittest.main()
