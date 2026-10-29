"""Case workspace over an ingested source mirror (operating.case_workspace_live).

Evidence label: injected-transport. The ingestion runner fills the mirror from the
in-process ``FakeWorkbench`` (synthetic AO-26005B chain rows and the synthetic PO5447
packaging rows) and the AO packet comes through the GET-only Graph reader over the
in-process ``FakeSender``; no network, no real PLX data. The workspace itself is built
offline: after ingestion the fake Workbench receives no request. Every case opens through
the generic path (``plx.find``, ``plx.object``, ``plx.around``, ``plx.investigate`` over the
projection of the state directory's captures); no case script exists.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

from integrations.source_mirror import AUDIENCE_SCHEMA, AudienceMap
from ontology.core import Actor
from operating import case_workspace_live as live_ws
from operating.case_runtime import CaseRuntime
from operating.ingestion import Ingestor, load_sources

# Skip reason when pypdf cannot import (BaseException: pyo3 PanicException).
_PYPDF_SKIP = ""
try:
    from pypdf import PdfWriter
except BaseException as _error:  # noqa: BLE001 - a broken cryptography backend panics
    if isinstance(_error, (KeyboardInterrupt, SystemExit)):
        raise
    _PYPDF_SKIP = f"pypdf import failed ({type(_error).__name__}: {_error})"

OPS, QUALITY, PURCHASING, INGEST = ("staff:operations", "staff:quality", "staff:purchasing",
                                    "svc:ingest")
AO_RELATIONS = {"public.assembly_order": [OPS], "public.order_entry": [OPS],
                "public.order_entry_item": [OPS], "public.order_entry_item_assembly": [OPS],
                # The Portal MO twin is quality's here, so an operations reader of an AO
                # case meets rows withheld from it.
                "public.app_manufacturing_order": [QUALITY], "public.packaging": [OPS],
                "silver.assembly_order_items": [QUALITY], "silver.production_tasks": [QUALITY],
                "silver.assembly_order_damage": [QUALITY]}
PO_RELATIONS = {name: [PURCHASING] for name in (
    "public.app_purchase_order", "public.app_purchase_order_line", "public.app_supplier",
    "public.app_receipt", "public.generated_purchase_order",
    "public.generated_purchase_order_item", "public.receipt_packaging")}
ROOT = Path(__file__).resolve().parents[2]
TID_FOR_TESTS = "11111111-1111-4111-8111-111111111111"
OID_FOR_TESTS = "aaaaaaaa-0000-4000-8000-000000000001"


class LiveMirrorWorkspaceTest(unittest.TestCase):
    """Ingest once (injected transport), then open and render offline per reader."""

    @classmethod
    def setUpClass(cls) -> None:
        from integrations.tests.test_source_mirror import BASE, KEY
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from operator_ui.tests import case_fixtures

        cls.temp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.temp.name)
        cls.state = cls.dir / "state"
        cls.state.mkdir()
        cls.world = case_fixtures.build_world()
        audiences = {name: sorted({*principals, INGEST}) for name, principals in
                     {**AO_RELATIONS, **PO_RELATIONS}.items()}
        cls.mirror_audience = cls.dir / "mirror-audience.json"
        cls.mirror_audience.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx", "default_classification": "internal",
            "audiences": audiences}), "utf-8")
        _mirror, cls.live_audience = case_fixtures.write_audiences(cls.dir, cls.world, "plx")
        # The ingestion runner fills the mirror (the only Portal reader).
        _tenant, ingest_actor, sources = load_sources({
            "schema": "plx-ingest-sources/v1", "tenant": "plx",
            "actor": {"principals": [INGEST], "clearance": "internal"},
            "sources": [{"id": "portal." + name.replace("_", "-").replace(".", "-"),
                         "kind": "portal_relation", "relation": name}
                        for name in sorted(audiences)]}, {})
        filler = CaseRuntime(cls.state, client=WorkbenchReadApiClient(
            BASE, KEY, fetch=cls.world.fake), audience=cls.mirror_audience, tenant="plx",
            mirror_options={"retry_delay_seconds": 0})
        ingestor = Ingestor(cls.state, sources, actor=ingest_actor, mirror=filler.mirror)
        try:
            cls.ingest_run = ingestor.run()
        finally:
            ingestor.close()
            filler.close()
        cls.portal_requests = len(cls.world.fake.paths)
        expected_path = cls.dir / "expected.json"
        cls.world.folder.expected(cls.dir).replace(expected_path)
        from operating.sharepoint_originals import folder_source
        cls.originals = folder_source(expected_path, {}, drive_id=cls.world.drive)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def runtime(self):
        from operating.tests.test_ao_originals import PAGES, extractor
        from operator_ui.tests import case_fixtures

        runtime = live_ws.offline_runtime(
            self.state, self.mirror_audience, "plx",
            live=case_fixtures.live_readers(self.world, self.live_audience),
            originals_options={"extractor": extractor(dict(PAGES))})
        self.addCleanup(runtime.close)
        return runtime

    def opener(self, runtime, principals=(OPS, QUALITY, PURCHASING)):
        return live_ws.CaseOpener(runtime, Actor("plx", frozenset(principals)), self.state,
                                  audience=AudienceMap.from_file(self.mirror_audience),
                                  originals=self.originals)

    def test_ingestion_filled_every_relation_through_the_mirror(self):
        self.assertEqual(self.ingest_run["summary"]["units"],
                         len(AO_RELATIONS) + len(PO_RELATIONS))
        self.assertEqual(self.ingest_run["summary"]["never_run"]
                         if "never_run" in self.ingest_run["summary"] else 0, 0)

    def test_a_console_session_stamps_its_cases_human_and_the_default_stays_agent(self):
        from operating.session_identity import AUTHOR_HUMAN

        runtime = self.runtime()
        investigator = Actor("plx", frozenset({OPS, QUALITY, PURCHASING}))
        human = self.opener(runtime)
        human.author_kind = AUTHOR_HUMAN
        case_id = human.open_case("ao", "AO-26005B", reinvestigate=True)["case_id"]
        self.assertEqual(runtime.cases.get(investigator, case_id)["objective_author_kind"],
                         "human")
        plain = self.opener(runtime).open_case("ao", "AO-26005B", reinvestigate=True)
        self.assertEqual(runtime.cases.get(investigator, plain["case_id"])
                         ["objective_author_kind"], "agent")
        with self.assertRaises(live_ws.OpenError):
            live_ws.CaseOpener(runtime, investigator, self.state, audience=AudienceMap.from_file(
                self.mirror_audience), author_kind="robot")

    def test_a_mirror_backed_ao_case_renders_originals_timeline_gaps_and_map(self):
        runtime = self.runtime()
        opened = self.opener(runtime).open_case("ao", "AO-26005B", reinvestigate=True)
        self.assertEqual(opened["status"], "opened", opened)
        investigator = Actor("plx", frozenset({OPS, QUALITY, PURCHASING}))
        view = live_ws.workspace(runtime, investigator, opened["case_id"])
        # Nothing reached the Portal API after ingestion: the mirror answered.
        self.assertEqual(len(self.world.fake.paths), self.portal_requests)
        self.assertFalse(runtime.client_configured)
        self.assertEqual(view["evidence"], live_ws.MIRROR_EVIDENCE)
        self.assertFalse(view["evidence"]["fixture"])
        [original] = view["originals"]
        self.assertEqual((original["filename"], original["source"]["kind"],
                          original["source"]["etag_status"]),
                         ("AO-26005B.pdf", "sharepoint-drive", "current"))
        self.assertTrue(original["integrity"]["sha256_rehashed_on_this_read"])
        # The code names the FM assembly order and its Portal MO twin (one record in two
        # systems, joined by a verified same-record link): both cards are cited, never
        # merged, and the link row one plx.around hop away is cited too.
        relations = {record["relation"] for record in view["records"]}
        self.assertTrue({"public.assembly_order", "public.app_manufacturing_order",
                         "public.order_entry_item_assembly"} <= relations, relations)
        self.assertTrue(view["records"][0]["cited_by"])
        statements = [item["statement"] for bucket in view["findings"].values()
                      for item in bucket if isinstance(item, dict) and "statement" in item]
        # The question family the root's type has (ontology/plans) answered on the case.
        chain = json.loads((ROOT / "ontology" / "plans" / "chain.json").read_text("utf-8"))
        self.assertIn(chain["title"], view["question"])
        self.assertTrue(any(text.startswith(chain["title"]) for text in statements))
        cited = [item for item in view["findings"]["supported"] if item.get("value_claims")]
        self.assertTrue(cited)
        self.assertTrue(all(claim["citation"] in {c["handle"] for c in item["citations"]}
                            for item in cited for claim in item["value_claims"]))
        dated = {item["relation"] for item in view["source_dates"]}
        self.assertTrue({"public.assembly_order", "original"} <= dated)
        kinds = {event.get("kind") for event in view["timeline"]}
        self.assertIn("case.opened", kinds)
        # What the tools could not read is a named gap, never absent.
        states = {gap["state"] for gap in view["gaps"]}
        self.assertTrue(view["gaps"])
        self.assertNotIn("absent_in_source", states)
        relations = {record["relation"] for record in view["records"]}
        self.assertTrue({"public.assembly_order", "public.app_manufacturing_order"} <= relations,
                        relations)
        self.assertTrue(view["originals"])
        self.assertNotIn("relationship_map", view)  # relationships are the ontology's
        shape = live_ws.structure(view)
        self.assertEqual((shape["status"], shape["read_only"], shape["business_system_writes"],
                          shape["originals"], shape["withheld"]["total"]),
                         ("read", True, 0, 1, 0))

    def test_a_case_missing_its_evaluation_or_proposals_is_not_a_converged_reader(self):
        runtime = self.runtime()
        opened = self.opener(runtime).open_case("ao", "AO-26005B", reinvestigate=True)
        view = live_ws.workspace(runtime, Actor("plx", frozenset({OPS, QUALITY, PURCHASING})),
                                 opened["case_id"])
        whole = live_ws.structure(view)
        self.assertTrue(whole["converged"], whole)
        partial = []
        for section in ("evaluation", "proposals", "gaps", "originals", "timeline"):
            shape = live_ws.structure({k: v for k, v in view.items() if k != section})
            self.assertFalse(shape["converged"], section)
            partial.append(shape)
        # An empty proposals section is still the panel, shown empty.
        self.assertTrue(live_ws.structure({**view, "proposals": []})["converged"])
        self.assertFalse(live_ws.structure(None).get("converged"))
        totals = live_ws.panel_totals([{"readers": [whole, *partial]},
                                       {"readers": [{"status": "not_readable"}]}])
        self.assertEqual(totals["converged_readers"], 1)
        # Every reader whose evaluation status is known, the unconverged ones included.
        self.assertEqual(totals["evaluation_by_status"], {whole["evaluation"]: len(partial)})

    def test_agreeing_values_raise_no_evidence_conflict(self):
        # The synthetic PO5447 rows: the Portal PO and its FM twin both read USD. The same
        # value is no disagreement (a PurchaseOrder.currency conflict once fired on it).
        runtime = self.runtime()
        opened = self.opener(runtime).open_case("petra_po", "PO5447", reinvestigate=True)
        self.assertEqual(opened["status"], "opened", opened)
        view = live_ws.workspace(runtime, Actor("plx", frozenset({OPS, QUALITY, PURCHASING})),
                                 opened["case_id"])
        self.assertEqual([c for c in view["conflicts"] if c["kind"] == "evidence_conflict"
                          and "PurchaseOrder.currency" in c.get("question", "")], [],
                         view["conflicts"])

    def test_a_reader_without_quality_sees_withheld_markers_not_data(self):
        runtime = self.runtime()
        opened = self.opener(runtime).open_case("ao", "AO-26005B", reinvestigate=True)
        investigator = Actor("plx", frozenset({OPS, QUALITY, PURCHASING}))
        reader = Actor("plx", frozenset({OPS}))
        audience = AudienceMap.from_file(self.mirror_audience)
        full = live_ws.workspace(runtime, investigator, opened["case_id"])
        view = live_ws.workspace(runtime, reader, opened["case_id"])
        shape = live_ws.structure(view)
        self.assertGreater(shape["withheld"]["total"], 0)
        self.assertGreater(shape["withheld"]["findings"], 0)
        self.assertEqual(view["originals"], [])
        text = json.dumps({k: v for k, v in view.items() if k != "limitations"})
        for hidden in ("app_manufacturing_order/", "AO-26005B.pdf", "sharepoint://",
                       "original://"):
            self.assertNotIn(hidden, text)
        check = live_ws.leak_check(runtime, investigator, reader, full, view, audience)
        self.assertGreater(check["withheld_rows"], 0)
        self.assertGreater(check["values_checked"], 0)
        self.assertEqual((check["row_leaks"], check["withheld_relation_refs"]), (0, 0))
        # The check is not vacuous: the investigator's view holds those values.
        leaked = live_ws.leak_check(runtime, investigator, reader, full, full, audience)
        self.assertGreater(leaked["withheld_relation_refs"], 0)
        # The case's words name the AO code, held by an operations row (the FM AO) and a
        # quality row (the Portal MO): a reader holding either may read the case, a
        # purchasing-only reader not.
        self.assertTrue(opened["audience_narrowed"])
        self.assertIsNone(live_ws.workspace(runtime, Actor("plx", frozenset({PURCHASING})),
                                            opened["case_id"]))
        # A reader who may read the packet holds the AO code from its filename: no false
        # leak, and the short-value blind spot is counted.
        both = Actor("plx", frozenset({OPS, QUALITY}))
        both_view = live_ws.workspace(runtime, both, opened["case_id"])
        self.assertEqual(len(both_view["originals"]), 1)
        both_check = live_ws.leak_check(runtime, investigator, both, full, both_view, audience)
        self.assertEqual((both_check["row_leaks"], both_check["withheld_relation_refs"]),
                         (0, 0))
        self.assertIn("values_skipped_short", check)
        # A value the reader holds from a readable original (the AO code in the packet's
        # filename) is not a leak of a withheld row that also holds it.
        purchasing = Actor("plx", frozenset({PURCHASING}))
        packet_only = {"records": [], "originals": both_view["originals"],
                       "question": "", "roots": []}
        self.assertIn("AO-26005B", json.dumps(packet_only))
        self.assertEqual(live_ws.leak_check(runtime, investigator, purchasing, full,
                                            packet_only, audience)["row_leaks"], 0)
        # A reader outside the case audience reads nothing at all (no oracle).
        self.assertIsNone(live_ws.workspace(runtime, Actor("plx", frozenset({"staff:finance"})),
                                            opened["case_id"]))

    def test_a_request_is_reused_for_the_same_investigator_and_picks_never(self):
        runtime = self.runtime()
        opener = self.opener(runtime)
        first = opener.open_case("ao", "AO-26005B")
        again = opener.open_case("ao", "ao-26005b")
        self.assertEqual(again, {"request": "ao", "status": "reused",
                                 "case_id": first["case_id"]})
        other = self.opener(runtime, principals=(OPS, QUALITY))
        self.assertEqual(other.open_case("ao", "AO-26005B")["status"], "opened")
        # Other options (no originals folder) are another request: investigated again.
        plain = live_ws.CaseOpener(runtime, Actor("plx", frozenset({OPS, QUALITY, PURCHASING})),
                                   self.state, audience=AudienceMap.from_file(self.mirror_audience))
        self.assertEqual(plain.open_case("ao", "AO-26005B")["status"], "opened")
        # A newer finished ingestion run is another request too.
        with mock.patch.object(live_ws, "_last_ingestion_run", lambda state: 10**6):
            self.assertEqual(opener.open_case("ao", "AO-26005B")["status"], "opened")
        self.assertEqual(opener.open_case("ao", "AO-26005B")["status"], "reused")
        from operating.ontology_tools_traverse import facts_path

        index = self.state / live_ws.INDEX_DIR
        kept = [index / live_ws.INDEX_FILE, opener.projection_path]
        # the kept files and the store facts kept beside each (``facts_path``) only
        allowed = {p.name for p in kept} | {facts_path(p).name for p in kept if p.exists()}
        leftovers = [p.name for p in index.iterdir() if p.name not in allowed]
        self.assertEqual(leftovers, [])  # each write goes through its own temp file

    def test_missing_data_is_named_never_a_silent_pass(self):
        runtime = self.runtime()
        failed = self.opener(runtime).open_case("ao", "AO-99999Z", reinvestigate=True)
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["missing"].startswith("missing_data:not_found"), failed)

    def test_a_portal_outage_is_blocked_upstream_never_absence(self):
        """The latest ingestion could not read the Portal (HTTP 500 on every relation):
        a request whose root is not held is blocked, a held one opens with the outage as
        an inaccessible gap, and no absence is recorded."""
        runtime = self.runtime()
        reasons = ["source_error:mirror.source_unavailable"]
        with mock.patch.object(live_ws, "portal_upstream", lambda state: reasons):
            opener = self.opener(runtime)
        self.assertEqual(opener.open_case("ao", "AO-99999Z", reinvestigate=True),
                         {"request": "ao", "status": "blocked",
                          "missing": live_ws.BLOCKED_UPSTREAM})
        opened = opener.open_case("ao", "AO-26005B", reinvestigate=True)
        self.assertEqual(opened["status"], "opened", opened)
        view = live_ws.workspace(runtime, Actor("plx", frozenset({OPS, QUALITY})),
                                 opened["case_id"])
        states = {gap["state"] for gap in view["gaps"]}
        self.assertNotIn("absent_in_source", states)
        [outage] = [gap for gap in view["gaps"] if "Portal" in gap["missing"]]
        self.assertEqual(outage["state"], "inaccessible")
        self.assertIn("mirror.source_unavailable", outage["why"])
        # The live verifier's blocked protocol: the run is blocked, never a pass.
        from devloop import live_verify as lv
        parts = lv.blocked_parts({"blocked_upstream": live_ws.upstream_parts(reasons)})
        self.assertEqual([(p["part"], p["error_code"]) for p in parts],
                         [("ingestion:portal_relation", "mirror.source_unavailable")])

    def test_an_online_runtime_is_refused(self):
        from integrations.tests.test_source_mirror import BASE, KEY
        from integrations.workbench_read_api import WorkbenchReadApiClient
        online = CaseRuntime(self.dir / "other", client=WorkbenchReadApiClient(
            BASE, KEY, fetch=self.world.fake), audience=self.mirror_audience, tenant="plx")
        self.addCleanup(online.close)
        with self.assertRaises(live_ws.OpenError) as raised:
            live_ws.CaseOpener(online, Actor("plx", frozenset({OPS})), self.dir / "other",
                               audience=AudienceMap.from_file(self.mirror_audience))
        self.assertEqual(raised.exception.code, "runtime_must_be_offline")

    @unittest.skipIf(_PYPDF_SKIP, _PYPDF_SKIP)
    def test_pypdf_noise_is_counted_by_cause_and_other_records_reach_stderr(self):
        """po-chain-unjoined-cause: the daily case.po_document_chain run left 33 stderr
        lines the verifier could only call ``stderr_unparsed``: pypdf's logging records on
        a malformed mailed PDF (they reach stderr through the last-resort handler) and
        warnings. The run counts those by typed cause in ``stderr_causes``, never a
        message. case-live-cause-honest: only pypdf's records are taken off stderr, where
        pypdf is read; any other record still reaches stderr, a traceback logged with
        ``exc_info`` whole (``devloop/live_verify.py`` reads its exception class)."""
        import logging
        import warnings

        from integrations import document_understanding as du

        writer, buffer = PdfWriter(), io.BytesIO()
        writer.add_blank_page(100, 100)
        writer.write(buffer)
        # A wrong startxref pointer: pypdf repairs it and logs warnings.
        malformed = re.sub(rb"startxref\s+\d+", b"startxref\n9", buffer.getvalue())
        structure = live_ws.structure
        readings = []

        def noisy(view):
            readings.append(du.read_pages(malformed, "pdf", max_pages=5, max_text=None))
            try:
                raise LookupError("a supplier's text")
            except LookupError:
                logging.getLogger("operating.cases").warning("resolver failed", exc_info=True)
            warnings.warn("a supplier's text", UserWarning, stacklevel=1)
            return structure(view)

        root, pypdf_log = logging.getLogger(), logging.getLogger("pypdf")
        handlers, level = list(root.handlers), pypdf_log.level
        pypdf_log.setLevel(logging.NOTSET)   # other suites set pypdf's level
        self.addCleanup(pypdf_log.setLevel, level)
        out, err = io.StringIO(), io.StringIO()
        argv = ["--state-dir", str(self.state), "--mirror-audience", str(self.mirror_audience),
                "--principal", OPS, "--principal", QUALITY, "--principal", PURCHASING,
                "--open", "ao=AO-26005B", "--min-rendered", "1"]
        # No handler of the root: a record goes through the last-resort handler.
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(root, "handlers", []), \
                mock.patch.object(live_ws, "structure", noisy):
            code = live_ws.main(argv, environ={})
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0, report)
        causes = report["stderr_causes"]
        self.assertEqual(set(causes["by_cause"]), {"log:pypdf._reader:WARNING",
                                                   "warning:UserWarning"}, causes)
        self.assertGreaterEqual(causes["by_cause"]["log:pypdf._reader:WARNING"], 1)
        self.assertEqual(causes["records"], sum(causes["by_cause"].values()))
        # The reading says pypdf repaired it; pypdf's records never reach stderr.
        self.assertIn("pypdf_log:WARNING", readings[0].warnings)
        self.assertNotIn("startxref", err.getvalue())
        # The other record reaches stderr whole: message, exception class and frame.
        self.assertIn("resolver failed", err.getvalue())
        self.assertIn("Traceback (most recent call last)", err.getvalue())
        self.assertIn("LookupError", err.getvalue())
        self.assertNotIn("supplier", out.getvalue())
        self.assertEqual(root.handlers, handlers)

    def test_stderr_causes_are_bounded_and_unnamed_causes_count_as_other(self):
        import warnings

        causes: dict = {}
        with live_ws._StderrCauses(causes):
            for index in range(live_ws.STDERR_CAUSES_MAX + 3):
                warnings.warn("x", type(f"Lib{index}Warning", (UserWarning,), {}))
            warnings.warn("again", UserWarning)
            warnings.warn("again", UserWarning)
            warnings.warn("y", type("9-not a name", (UserWarning,), {}))
        self.assertEqual(causes["records"], live_ws.STDERR_CAUSES_MAX + 6)
        self.assertEqual(len(causes["by_cause"]), live_ws.STDERR_CAUSES_MAX + 1)
        self.assertEqual(causes["by_cause"]["warning:Lib0Warning"], 1)
        self.assertEqual(causes["by_cause"]["other"], 6)

    def test_cli_prints_counts_only_and_checks_every_reader(self):
        out = io.StringIO()
        argv = ["--state-dir", str(self.state), "--mirror-audience", str(self.mirror_audience),
                "--principal", OPS, "--principal", QUALITY, "--principal", PURCHASING,
                "--open", "ao=AO-26005B", "--open", "ao=AO-99999Z", "--reinvestigate",
                "--reader", OPS, "--reader", f"{OPS},{QUALITY}",
                "--min-rendered", "1", "--output-dir", str(self.dir / "out")]
        with contextlib.redirect_stdout(out), mock.patch(
                "integrations.workbench_read_api.WorkbenchReadApiClient.__init__",
                side_effect=AssertionError("no Portal client in the workspace")):
            code = live_ws.main(argv, environ={})
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0, report)
        self.assertEqual((report["cases_requested"], report["cases_rendered"],
                          report["failed"], report["failed_unnamed"]), (2, 1, 1, 0))
        self.assertEqual((report["row_leaks"], report["withheld_relation_refs"]), (0, 0))
        self.assertEqual(report["summary_row_data_check"]["leaked"], 0)
        self.assertGreater(report["summary_row_data_check"]["values_checked"], 0)
        self.assertGreaterEqual(report["readers_with_withheld_markers"], 1)
        self.assertEqual(report["mirror"], {"workbench_client": False})
        ao = report["cases"][0]
        self.assertEqual([r["reader"] for r in ao["readers"]],
                         [sorted([OPS, QUALITY, PURCHASING]), [OPS], sorted([OPS, QUALITY])])
        # Without --live-source-audience the packet is not attached.
        self.assertEqual(ao["readers"][0]["originals"], 0)
        # Relationship counts are the projection's links as each reader's tools show
        # them: schema link types only, and a narrower reader never sees more of them
        # (operations alone cannot read the quality-held twin, so it sees fewer objects).
        from ontology.schema import load_schema
        full, ops, both = ao["readers"]
        self.assertTrue(full["relationship_edges"])
        self.assertLessEqual(set(full["relationship_edges"]), set(load_schema().link_types))
        for narrower in (ops, both):
            self.assertEqual(narrower["relationship_truncated"], [])
            for link_type, count in narrower["relationship_edges"].items():
                self.assertLessEqual(count, full["relationship_edges"].get(link_type, 0),
                                     link_type)
        self.assertLess(ops["relationship_nodes"], full["relationship_nodes"])
        self.assertEqual(report["blocked_upstream"], [])
        self.assertEqual(report["cases"][1]["status"], "failed")
        self.assertTrue(report["cases"][1]["missing"].startswith("missing_data:"))
        text = out.getvalue()
        for value in ("AO-26005B", "api://", "10718", "original://"):
            self.assertNotIn(value, text)
        self.assertIsNone(re.search(r"case-[0-9a-f]{8}", text))
        written = sorted(p.name for p in (self.dir / "out").iterdir())
        self.assertEqual(len(written), 3)

    def test_cli_ingest_step_runs_the_ingestion_runner_first(self):
        captured = {}

        def fake_main(argv, *, environ=None):
            captured["argv"] = argv
            print(json.dumps({"runs": [{"summary": {"units": 1}}], "cursor_regressions": 0,
                              "row_data_check": {"values_checked": 0, "leaked": 0},
                              "status": {"summary": {"units": 1}}}))
            return 0

        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("operating.ingestion.main", fake_main):
            live_ws.main(["--state-dir", str(self.state), "--mirror-audience",
                          str(self.mirror_audience), "--principal", OPS, "--open",
                          "ao=AO-26005B", "--ingest-sources", "S.json", "--ingest-runs", "2"],
                         environ={})
        self.assertEqual(captured["argv"][:4], ["run", "--state-dir", str(self.state),
                                                "--sources"])
        self.assertIn("--runs", captured["argv"])
        report = json.loads(out.getvalue())
        self.assertEqual(report["ingestion"]["exit_code"], 0)
        self.assertEqual(report["ingestion"]["cursor_regressions"], 0)

    def employee_console(self, role="STAFF"):
        """A live console identity: policy, Portal snapshot, secret and a human ticket
        (isolated-fixture Portal rows; operating.session_identity)."""
        from operating.session_identity import (AUTHOR_HUMAN, SESSION_TICKET_SECRET_ENV,
                                                mint_session_ticket)
        from operating.tests.test_session_identity import identity_files

        policy, snapshot = identity_files(self.dir / f"identity-{role}", {
            OPS: ("VIEW_OPERATIONS", "operations.sales", "ops"),
            QUALITY: ("VIEW_OPERATIONS", "operations.quality", "qa")}, role=role)
        secret = "s" * 48
        ticket = mint_session_ticket(uid="cmuser0000001", email="ricardo@petralabx.com",
                                     tenant_id=TID_FOR_TESTS, object_id=OID_FOR_TESTS,
                                     author=AUTHOR_HUMAN, secret=secret)
        env = {SESSION_TICKET_SECRET_ENV: secret, "CONSOLE_TICKET": ticket}
        argv = ["--employee-policy", str(policy), "--portal-access", str(snapshot)]
        return argv, env, ticket

    def test_the_console_opens_a_mirror_case_for_its_employee_and_labels_the_mirror(self):
        from operator_ui import case_console
        captured = {}

        def fake_serve(*, port, ticket_sessions, journal):
            session, workspace = ticket_sessions.authenticate(captured["ticket"])
            captured["session"], captured["journal"] = session, journal
            captured["objects"] = ticket_sessions.object_workspace(session)
            captured["listed"] = workspace.list_cases()
            captured["case"] = workspace.case(captured["listed"]["cases"][0]["case_id"])
        printed = io.StringIO()
        identity, env, captured["ticket"] = self.employee_console()
        argv = ["--state-dir", str(self.state), "--source-mirror-audience",
                str(self.mirror_audience), *identity, "--session-ticket-env", "CONSOLE_TICKET",
                "--open", "ao=AO-26005B"]
        # A free-text principal list is no identity for live (ingested) state.
        for refused in (["--principal", OPS], ["--fixture", "--principal", OPS]):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"), \
                    mock.patch.object(case_console, "serve", fake_serve):
                case_console.main(["--state-dir", str(self.state), "--source-mirror-audience",
                                   str(self.mirror_audience), *refused])
        # setUpClass's ingestion run wrote the ingestion store: the console labels it.
        with mock.patch.object(case_console, "serve", fake_serve), \
                mock.patch.dict("os.environ", env), \
                contextlib.redirect_stdout(printed), mock.patch(
                    "integrations.workbench_read_api.WorkbenchReadApiClient.__init__",
                    side_effect=AssertionError("no Portal client in the console")):
            self.assertEqual(case_console.main(argv), 0)
        self.assertEqual(captured["session"].author_kind, "human")
        self.assertEqual(captured["session"].actor.principals, {"employee:" + OID_FOR_TESTS})
        # A live console journals its sessions in the state directory.
        self.assertEqual(captured["journal"].path, self.state.resolve() / "console-journal.sqlite")
        self.assertIn('"status": "', printed.getvalue())
        self.assertNotIn("AO-26005B", printed.getvalue())
        self.assertEqual(captured["listed"]["evidence"], live_ws.MIRROR_EVIDENCE)
        self.assertIsNone(captured["objects"])  # no projection store was asked for
        view = captured["case"]
        self.assertEqual(view["evidence"]["label"], live_ws.MIRROR_EVIDENCE["label"])
        self.assertIn("AO-26005B", view["question"])
        self.assertTrue(view["timeline"] and view["gaps"] and view["records"])
        self.assertEqual(len(self.world.fake.paths), self.portal_requests)
        # A second start reuses the case; --reinvestigate investigates it again.
        for extra, status in (([], "reused"), (["--reinvestigate"], "opened")):
            printed = io.StringIO()
            with mock.patch.object(case_console, "serve", fake_serve), \
                    mock.patch.dict("os.environ", env), contextlib.redirect_stdout(printed):
                self.assertEqual(case_console.main(argv + extra), 0)
            self.assertIn(f'"status": "{status}"', printed.getvalue())
        # A journal that cannot be created (full or read-only volume) costs the journal,
        # never the console: one warning line, no path, and the console still serves.
        warned = io.StringIO()
        with mock.patch.object(case_console, "serve", fake_serve), \
                mock.patch.object(case_console.SessionJournal, "for_state_dir",
                                  side_effect=sqlite3.OperationalError("disk is full")), \
                mock.patch.dict("os.environ", env), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(warned):
            self.assertEqual(case_console.main(argv), 0)
        self.assertIsNone(captured["journal"])
        self.assertEqual(captured["session"].author_kind, "human")
        lines = warned.getvalue().splitlines()
        self.assertEqual(lines, ["console journal: unavailable (OperationalError); "
                                 "serving without a journal"], lines)
        self.assertNotIn(str(self.state), warned.getvalue())
        # The console's durable replay guard (the state directory's, shared with the MCP
        # host): a revoked ticket is refused at start-up and on every request, also
        # after a restart.
        from operating.session_identity import (SESSION_TICKET_SECRET_ENV,
                                                SessionIdentityRefused, replay_guard_for,
                                                verify_session_ticket)
        revoked = captured["ticket"]
        replay_guard_for(self.state).revoke(
            verify_session_ticket(revoked, env[SESSION_TICKET_SECRET_ENV]))
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"), \
                mock.patch.object(case_console, "serve", fake_serve), \
                mock.patch.dict("os.environ", env), contextlib.redirect_stdout(io.StringIO()):
            case_console.main(argv)
        _identity, fresh, _ticket = self.employee_console()
        with mock.patch.object(case_console, "serve", fake_serve), \
                mock.patch.dict("os.environ", fresh), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SessionIdentityRefused) as refused:
            case_console.main(argv)   # starts; the request bearing the revoked ticket fails
        self.assertEqual(refused.exception.reason, "session_ticket_revoked")

    def test_an_employee_gets_an_object_workspace_resolved_per_request(self):
        """--project-ontology on live state: each verified employee gets its own object
        workspace; its grant is the runtime's employee gate, resolved per request."""
        from operator_ui import case_console
        captured = {}

        def fake_serve(*, port, ticket_sessions, journal):
            session, _ = ticket_sessions.authenticate(captured["ticket"])
            view = ticket_sessions.object_workspace(session)
            captured.update(session=session, view=view,
                            again=ticket_sessions.object_workspace(session))
        identity, env, captured["ticket"] = self.employee_console()
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / "ontology.sqlite"
            argv = ["--state-dir", str(self.state), "--source-mirror-audience",
                    str(self.mirror_audience), *identity, "--ontology-store", str(store),
                    "--project-ontology"]
            with mock.patch.object(case_console, "serve", fake_serve), \
                    mock.patch.dict("os.environ", env), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(case_console.main(argv), 0)
                # A marking is a fixture reader's assertion, never a live employee's.
                with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                    case_console.main([*argv, "--marking", "finance"])
            view, session = captured["view"], captured["session"]
            self.assertIs(captured["again"], view)  # one object workspace per employee
            self.assertEqual(view.actor, session.actor)
            self.assertEqual(view.actor.principals, {"employee:" + OID_FOR_TESTS})
            found = view.find("AO-26005B")["find"]
            self.assertIn(found["status"], ("found", "ambiguous"), found)
            self.assertEqual(view.evidence, live_ws.MIRROR_EVIDENCE)
            # The Portal no longer grants the employee: the next request is refused
            # before any tool reads (the case host's employee gate, decided per request).
            portal = Path(identity[identity.index("--portal-access") + 1])
            snapshot = json.loads(portal.read_text("utf-8"))
            portal.write_text(json.dumps({**snapshot, "users": []}), "utf-8")
            with self.assertRaises(PermissionError):
                view.find("AO-26005B")

    def test_open_requests_are_validated(self):
        self.assertEqual(live_ws.parse_open("ao=AO-26005B"), ("ao", "AO-26005B"))
        self.assertEqual(live_ws.parse_open("pick=PurchaseOrder+ReceiptLine"),
                         ("pick", "PurchaseOrder+ReceiptLine"))
        self.assertEqual(live_ws.parse_open("find= SO100201 "), ("find", "SO100201"))
        for bad in ("ao", "unknown=1", "pick=newest-received-po", "pick=PurchaseOrder+Nope",
                    "pick=Nope", "petra_po= "):
            with self.subTest(bad=bad), self.assertRaises(live_ws.OpenError):
                live_ws.parse_open(bad)


class RelationshipCountTest(unittest.TestCase):
    """``relationships`` over a synthetic projection and tool answers: each link counted
    once per link type however many case objects hold it, every page read, a hidden or
    uncited object never counted, and the object cap named, at N = 1, 10, 60 and 61."""

    def projection(self, temp: str, objects: list[str]) -> Path:
        import sqlite3

        path = Path(temp) / "ontology.sqlite"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE object_versions (object_id TEXT, l1_handle TEXT)")
        db.executemany("INSERT INTO object_versions VALUES (?, ?)",
                       [(oid, f"api://h/{oid}@g") for oid in objects])
        db.commit()
        db.close()
        return path

    @staticmethod
    def tools(n: int, fan: int = 0, hidden: frozenset[str] = frozenset()):
        """A chain O0 -> O1 -> ... (``Doc.next``), O0 -> ``fan`` others (``Doc.many``)."""
        def edge(link_type: str, start: str, end: str) -> dict:
            return {"link_id": f"{link_type}:{start}>{end}", "link_type": link_type,
                    "from": start, "to": end, "tier": "lead"}

        def call(tool: str, args: dict) -> dict:
            if tool == "plx.object":
                oid = args["object_id"]
                if oid in hidden:
                    return {"error": {"code": "object_not_found"}}
                index = int(oid[1:])
                counts = [{"link_type": "Doc.next", "direction": "forward", "count": 1}] \
                    if index < n - 1 else []
                if index:
                    counts.append({"link_type": "Doc.next", "direction": "reverse", "count": 1})
                if index == 0 and fan:
                    counts.append({"link_type": "Doc.many", "direction": "forward",
                                   "count": fan})
                return {"object": {"object_id": oid}, "identity": [], "linked_counts": counts}
            index = int(args["from"][1:])
            hop = args["path"][0]
            if hop["link_type"] == "Doc.many":
                start = int(args.get("cursor") or 0)
                page = [edge("Doc.many", "O0", f"X{i}") for i in range(start, min(fan, start + 50))]
                more = start + 50 < fan
                return {"edges": page, "completeness": {
                    "next_cursor": str(start + 50) if more else None}}
            other = index + 1 if hop["direction"] == "forward" else index - 1
            start, end = sorted((index, other))
            # The tools never reach an end the reader cannot see.
            return {"edges": [] if f"O{other}" in hidden else [
                edge("Doc.next", f"O{start}", f"O{end}")], "completeness": {"next_cursor": None}}
        return call

    def view(self, objects: list[str]) -> dict:
        cites = [{"handle": f"api://h/{oid}@g", "status": "resolved"} for oid in objects]
        cites.append({"handle": "api://h/S0@g", "status": "withheld"})  # not this reader's
        return {"findings": {"fact": [{"citations": cites}]}}

    def test_every_link_once_and_the_object_cap_named(self):
        for n in (1, 10, 60, 61):
            with self.subTest(n=n), tempfile.TemporaryDirectory() as temp:
                objects = [f"O{i}" for i in range(n)]
                projection = self.projection(temp, objects + ["S0"])
                out = live_ws.relationships(self.tools(n), projection, self.view(objects))
                self.assertEqual(out["relationship_nodes"], min(n, live_ws.MAP_OBJECTS))
                self.assertEqual(out["relationship_edges"],
                                 {"Doc.next": n - 1} if n > 1 else {})
                self.assertEqual(out["relationship_truncated"],
                                 ["objects"] if n > live_ws.MAP_OBJECTS else [])

    def test_pages_are_read_hidden_objects_skipped_and_a_cut_read_named(self):
        with tempfile.TemporaryDirectory() as temp:
            objects = ["O0", "O1", "O2"]
            projection = self.projection(temp, objects)
            out = live_ws.relationships(self.tools(3, fan=120, hidden=frozenset({"O2"})),
                                        projection, self.view(objects))
            self.assertEqual(out, {"relationship_nodes": 2, "relationship_truncated": [],
                                   "relationship_edges": {"Doc.many": 120, "Doc.next": 1}})
            out = live_ws.relationships(self.tools(3, fan=250), projection, self.view(objects))
            self.assertEqual(out["relationship_edges"]["Doc.many"],
                             live_ws.MAP_PAGES * live_ws.MAP_PAGE)
            self.assertEqual(out["relationship_truncated"], ["links"])
            plain = self.tools(1)

            def twins(tool: str, args: dict) -> dict:   # a card listing its maximum
                card = plain(tool, args)
                card["identity"] = [{"link_id": f"t{i}", "link_type": "Doc.same_record_as"}
                                    for i in range(live_ws.CARD_IDENTITY)]
                return card
            out = live_ws.relationships(twins, projection, self.view(["O0"]))
            self.assertEqual((out["relationship_edges"], out["relationship_truncated"]),
                             ({"Doc.same_record_as": live_ws.CARD_IDENTITY}, ["card"]))
        gone = live_ws.relationships(self.tools(1), Path(temp) / "ontology.sqlite",
                                     self.view(["O0"]))
        self.assertEqual(gone, {"relationship_nodes": 0, "relationship_edges": {},
                                "relationship_truncated": ["projection_unavailable"]})
        self.assertEqual(live_ws.relationships(self.tools(1), Path(temp), None), {})


class LiveCheckSpecTest(unittest.TestCase):
    """The committed spec, run against the fixture world: its ingestion step reads the
    in-process FakeWorkbench (injected transport) and its expectations hold. The spec's
    SharePoint-original flags are dropped (the fixture packets are not the expected live
    files); the Graph reader path is covered by LiveMirrorWorkspaceTest."""

    def run_spec(self, fetch=None):
        from devloop import live_verify as lv
        from integrations import workbench_read_api
        from integrations.source_mirror import SourceMirror
        from integrations.tests.test_source_mirror import BASE, KEY
        from operator_ui.tests import case_fixtures

        spec = json.loads((ROOT / "devloop" / "live-checks" / "case.workspace_live.json")
                          .read_text("utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem="case.workspace_live"), [])
        command = list(spec["command"])
        self.assertEqual(command[:3], ["python3", "-m", "operating.case_workspace_live"])
        argv, skip = [], 0
        for arg in command[3:]:
            if skip:
                skip -= 1
                continue
            if arg in ("--live-source-audience", "--originals-expected"):
                skip = 1
                continue
            if arg == "--sharepoint-original":
                continue
            argv.append(arg)
        world = case_fixtures.build_world()
        real = workbench_read_api.WorkbenchReadApiClient
        with tempfile.TemporaryDirectory() as temp:
            argv = [str(ROOT / arg) if arg.startswith("examples/") else
                    arg.replace("{tmp}", temp) for arg in argv]
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(
                    workbench_read_api, "WorkbenchReadApiClient",
                    lambda url, key: real(BASE, KEY, fetch=fetch or world.fake)), \
                    mock.patch.dict(SourceMirror.__init__.__kwdefaults__,
                                    {"sleep": lambda _: None}):
                code = live_ws.main(argv, environ={"PLX_WORKBENCH_API_URL": BASE,
                                                   "PLX_WORKBENCH_API_KEY": "fixture-key"})
        return spec, code, json.loads(out.getvalue())

    def test_the_spec_command_meets_its_own_expectations_over_injected_sources(self):
        from devloop import live_verify as lv

        spec, code, document = self.run_spec()
        # Without the SharePoint originals no reader shows every panel: the converged
        # floor is the live run's, and here each read reader lacks only the original.
        self.assertEqual(lv.check_expectations(spec, code, document), ["converged_readers < 1"])
        self.assertEqual(document["converged_readers"], 0)
        read = [r for e in document["cases"] for r in e.get("readers", [])
                if r["status"] == "read"]
        self.assertTrue(read)
        self.assertTrue(all(r["timeline"] and r["evaluation"] is not None
                            and not r["originals"] for r in read))
        self.assertEqual(sum(document["evaluation_by_status"].values()), len(read))
        del spec["expect"]["json_min"]["converged_readers"]
        self.assertEqual(lv.check_expectations(spec, code, document), [], document)
        self.assertEqual(document["blocked_upstream"], [])
        statuses = [case["status"] for case in document["cases"]]
        self.assertEqual(statuses[:6], ["opened"] * 6)  # the six fixture AO packets
        self.assertGreater(document["ingestion"]["status_summary"]["units"], 0)
        # What the verifier would record is counts and enums only.
        recorded = json.dumps(lv._recorded(spec, document))
        for value in ("AO-26005", "api://", "PO#5447"):
            self.assertNotIn(value, recorded)

    def test_a_portal_answering_http_500_makes_the_check_blocked_never_a_pass(self):
        from devloop import live_verify as lv
        from integrations.http import SourceHttpError

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        spec, code, document = self.run_spec(portal_down)
        self.assertEqual(code, 0, document)
        self.assertTrue(lv.blocked_parts(document))
        self.assertEqual({case["status"] for case in document["cases"]}, {"blocked"})
        self.assertEqual({case["missing"] for case in document["cases"]},
                         {live_ws.BLOCKED_UPSTREAM})
        self.assertEqual((document["failed"], document["cases_rendered"]), (0, 0))
        # The full expectation is unmet (recorded as unverified); the blocked one holds.
        self.assertNotEqual(lv.check_expectations(spec, code, document), [])
        self.assertEqual(lv.check_expectations(spec, code, document, spec["expect_blocked"]),
                         [])


class LiveMirrorPurchaseOrderTest(unittest.TestCase):
    """A PO case (by number and by a run-time pick) over a mirror the ingestion runner
    filled from the synthetic order fixture (POs PO100777 / PO100888, ``order_fixture``)."""

    def setUp(self, grants: dict | None = None) -> None:
        from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from operating.tests import order_fixture as fixture

        grants = fixture.AUDIENCES if grants is None else grants
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "state"
        self.fake = FakeWorkbench()
        self.fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        self.fake.tables = fixture.tables()
        audiences = {name: sorted({*principals, INGEST})
                     for name, principals in grants.items()}
        self.audience = Path(temp.name) / "audience.json"
        self.audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                             "audiences": audiences}), "utf-8")
        _tenant, actor, sources = load_sources({
            "schema": "plx-ingest-sources/v1", "tenant": "plx",
            "actor": {"principals": [INGEST], "clearance": "internal"},
            "sources": [{"id": "portal." + name.replace("_", "-").replace(".", "-"),
                         "kind": "portal_relation", "relation": name}
                        for name in sorted(audiences)]}, {})
        filler = CaseRuntime(self.state, client=WorkbenchReadApiClient(BASE, KEY, fetch=self.fake),
                             audience=self.audience, tenant="plx",
                             mirror_options={"retry_delay_seconds": 0})
        ingestor = Ingestor(self.state, sources, actor=actor, mirror=filler.mirror)
        try:
            ingestor.run()
        finally:
            ingestor.close()
            filler.close()
        self.requests = len(self.fake.paths)
        self.runtime = live_ws.offline_runtime(self.state, self.audience, "plx")
        self.addCleanup(self.runtime.close)
        self.investigator = Actor("plx", frozenset({OPS, "staff:sales", PURCHASING}))

    def test_a_po_case_by_number_and_by_pick_renders_and_withholds_purchasing_rows(self):
        opener = live_ws.CaseOpener(self.runtime, self.investigator, self.state,
                                    audience=AudienceMap.from_file(self.audience))
        for kind, identifier in (("petra_po", "PO100777"), ("find", "PO100888"),
                                 ("pick", "PurchaseOrder+ReceiptLine")):
            with self.subTest(kind=kind):
                opened = opener.open_case(kind, identifier)
                self.assertEqual(opened["status"], "opened", opened)
                full = live_ws.workspace(self.runtime, self.investigator, opened["case_id"])
                relations = {record["relation"] for record in full["records"]}
                self.assertIn("public.app_purchase_order", relations)
                self.assertTrue(full["timeline"])
                self.assertTrue(opened["audience_narrowed"])
                # The PO number (a purchasing-only value) is the case's question and
                # root: a reader without purchasing cannot read the case at all.
                self.assertIsNone(live_ws.workspace(
                    self.runtime, Actor("plx", frozenset({OPS, "staff:sales"})),
                    opened["case_id"]))
                buyer = Actor("plx", frozenset({OPS, PURCHASING}))
                view = live_ws.workspace(self.runtime, buyer, opened["case_id"])
                check = live_ws.leak_check(self.runtime, self.investigator, buyer, full, view,
                                           AudienceMap.from_file(self.audience))
                self.assertEqual((check["row_leaks"], check["case_text_leaks"],
                                  check["withheld_relation_refs"]), (0, 0, 0))
        # PO100888's lines, receipt and supplier one link away are cited on its case.
        found = opener.open_case("find", "PO100888")
        relations = {record["relation"] for record in live_ws.workspace(
            self.runtime, self.investigator, found["case_id"])["records"]}
        self.assertTrue({"public.app_purchase_order_line", "public.app_receipt",
                         "public.app_supplier"} <= relations, relations)
        # A sales order by number and by its customer PO: the same generic path roots it,
        # its lines and shipment one link away are cited, and a reader without sales or
        # purchasing grants cannot read the case. An unknown request kind is refused.
        for kind, identifier, linked in (
                ("sales_order", "SO100201", {"public.app_sales_order_line",
                                             "public.app_shipment"}),
                ("customer_po", "CPO-7781", {"public.app_shipment"})):
            with self.subTest(kind=kind):
                opened = opener.open_case(kind, identifier)
                self.assertEqual(opened["status"], "opened", opened)
                records = live_ws.workspace(self.runtime, self.investigator,
                                            opened["case_id"])["records"]
                relations = {record["relation"] for record in records}
                self.assertTrue({"public.app_sales_order", *linked} <= relations, relations)
                self.assertIsNone(live_ws.workspace(
                    self.runtime, Actor("plx", frozenset({"staff:sales"})), opened["case_id"]))
        self.assertEqual(opener.open_case("sales_order", "SO999999")["missing"],
                         "missing_data:not_found")
        self.assertEqual(opener.open_case("bogus", "X"),
                         {"request": "bogus", "status": "failed", "missing": "unknown_kind"})
        # Two orders carrying one customer PO are refused, never one picked; a type no
        # object of which is within two links of the linked type picks nothing, and says so.
        self.assertEqual(opener.open_case("customer_po", "CPO-DUP")["missing"],
                         "ambiguous_identifier")
        self.assertEqual(opener.open_case("pick", "Supplier+AssemblyOrder")["missing"],
                         "pick_linked_unjoined")
        # Picks are never reused: a second pick investigates again.
        self.assertEqual(opener.open_case("pick", "PurchaseOrder+ReceiptLine")["status"],
                         "opened")
        self.assertEqual(len(self.fake.paths), self.requests)  # offline after ingestion

    def test_an_unjoined_pick_while_the_linked_capture_is_unfinished_names_that_capture(self):
        """po-chain-unjoined-cause: the daily run read 2 of 10 mail folders, held some
        supplier confirmations, none naming a held PO, and failed ``pick_linked_unjoined``;
        the unfinished capture of the linked type's source may hold the joined one, so the
        cause is ``pick_source_not_captured:<source>``. A pick whose link the declared edge
        reaches still opens while that capture is unfinished."""
        import sqlite3

        opener = live_ws.CaseOpener(self.runtime, self.investigator, self.state,
                                    audience=AudienceMap.from_file(self.audience))
        self.assertEqual(opener.open_case("pick", "Supplier+AssemblyOrder")["missing"],
                         "pick_linked_unjoined")
        done = {"last_full_traversal_at": "2026-10-15T00:00:00Z", "scopes": []}
        freshness = {"portal_api": {"public.app_supplier": done, "public.supplier": done,
                                    "public.app_purchase_order": done,
                                    "public.app_receipt_line": done,
                                    "public.assembly_order": {"last_full_traversal_at": None,
                                                              "scopes": []}}}
        db = sqlite3.connect(opener.projection())
        try:
            db.execute("INSERT OR REPLACE INTO store_meta(key, value) VALUES "
                       "('source_freshness', ?)", (json.dumps(freshness),))
            db.commit()
        finally:
            db.close()
        unjoined = opener.open_case("pick", "Supplier+AssemblyOrder")
        self.assertEqual(unjoined["missing"], "pick_source_not_captured:portal_api")
        # The join is still measured beside the capture's cause, counts only.
        pick = unjoined["pick"]
        self.assertEqual((pick["candidates"], pick["candidates_joined"]), (0, 0), pick)
        self.assertGreater(pick["linked_held"], 0)
        self.assertEqual(pick["linked_to_start"], 0, pick)  # none links to a supplier
        self.assertTrue(all(type(pick[key]) is int for key in (
            "linked_held", "linked_to_start", "linked_refs_unheld")), pick)
        self.assertEqual(opener.open_case("pick", "PurchaseOrder+ReceiptLine")["status"],
                         "opened")

    def test_an_answer_from_rows_a_reader_cannot_read_is_withheld_never_uncited(self):
        """Review of 18072f8f: the question families' answers (line quantities, counts,
        computed values) were written uncited, so every case reader saw them.
        With the order lines sales-only, an operations reader of the sales order's case
        sees those answers withheld."""
        from operating.tests import order_fixture as fixture

        self.doCleanups()
        self.setUp(grants={**fixture.AUDIENCES, "public.app_sales_order_line": ["staff:sales"],
                           "public.order_entry_item": ["staff:sales"]})
        audience = AudienceMap.from_file(self.audience)
        opened = live_ws.CaseOpener(self.runtime, self.investigator, self.state,
                                    audience=audience).open_case("sales_order", "SO100201")
        self.assertEqual(opened["status"], "opened", opened)
        full = self.runtime.cases.get(self.investigator, opened["case_id"])
        # The order lines hang off the order by the Portal's own foreign key: a verified
        # link, so the line quantity is recorded as the cited value itself.
        lines = [item for bucket in full["findings"].values() for item in bucket
                 if "SalesOrderLine.ordered_quantity = 100.0" in (item["statement"] or "")]
        self.assertTrue(lines)
        self.assertTrue(all(item["citations"] for item in lines))
        ops = Actor("plx", frozenset({OPS}))
        view = self.runtime.cases.get(ops, opened["case_id"])
        self.assertTrue(view["findings"]["withheld"])
        shown = json.dumps([view["findings"], view["gaps"]])
        for value in ("ordered_1", "ordered_2", "100.0", "sales_order_line_count"):
            self.assertNotIn(value, shown)
        for item in [*(i for b in view["findings"].values() for i in b), *view["gaps"]]:
            if not item.get("citations"):  # uncited text never names an object
                self.assertIsNone(re.search(r"[A-Z]\w+:[a-z0-9_]+\.[a-z0-9_.]+:\{", json.dumps(item)))
        check = live_ws.leak_check(self.runtime, self.investigator, ops,
                                   live_ws.workspace(self.runtime, self.investigator,
                                                     opened["case_id"]),
                                   live_ws.workspace(self.runtime, ops, opened["case_id"]),
                                   audience)
        self.assertEqual((check["row_leaks"], check["case_text_leaks"]), (0, 0))

    def test_revoking_the_source_grant_hides_the_case_header_on_every_surface(self):
        """Regression (review of bb48071): the question and roots of a narrowed case
        name a PO number read from purchasing relations. Once the mirror audience
        revokes those relations from purchasing, no surface (store get/list, MCP
        case_get in every view, case_list, the console list and workspace) shows the
        case to a purchasing reader; restoring the grant shows it again, and a request
        after the grant change investigates anew instead of reusing the stale case."""
        from operating.cases import CaseNotFound
        from operating.mcp_server import MCPServer, local_client
        from operator_ui.case_workspace import CaseWorkspace

        number = "PO100777"
        opened = live_ws.CaseOpener(self.runtime, self.investigator, self.state,
                                    audience=AudienceMap.from_file(self.audience)
                                    ).open_case("petra_po", number)
        self.assertEqual(opened["status"], "opened", opened)
        case_id = opened["case_id"]
        buyer = Actor("plx", frozenset({PURCHASING}))
        operations = Actor("plx", frozenset({OPS}))

        def surfaces(reader):
            """Every case surface for ``reader``: (case visible?, texts shown)."""
            call = local_client(MCPServer(case_runtime=self.runtime, actor=reader), "probe")
            texts, seen = [], []
            try:
                texts.append(self.runtime.cases.get(reader, case_id))
                seen.append("get")
            except CaseNotFound:
                pass
            listed = self.runtime.cases.list(reader)
            texts.append(listed)
            if any(item["case_id"] == case_id for item in listed):
                seen.append("list")
            for view in ({}, {"section": "header"}, {"section": "timeline"}):
                try:
                    texts.append(call("plx.case_get", {"case_id": case_id, **view}))
                    seen.append("mcp_get:" + (view.get("section") or "default"))
                except RuntimeError as error:
                    self.assertIn("case_not_found", str(error))
            mcp_list = call("plx.case_get", {})
            texts.append(mcp_list)
            if any(item["case_id"] == case_id for item in mcp_list["cases"]):
                seen.append("mcp_list")
            console = CaseWorkspace(self.runtime, reader).list_cases()
            texts.append(console)
            if any(item["case_id"] == case_id for item in console["cases"]):
                seen.append("console_list")
            view = live_ws.workspace(self.runtime, reader, case_id)
            if view is not None:
                texts.append(view)
                seen.append("workspace")
            return seen, json.dumps(texts, default=str)

        everywhere = ["get", "list", "mcp_get:default", "mcp_get:header",
                      "mcp_get:timeline", "mcp_list", "console_list", "workspace"]
        seen, text = surfaces(buyer)
        self.assertEqual(seen, everywhere)
        self.assertIn(number, text)
        self.assertEqual(surfaces(operations)[0], [])

        granted = self.audience.read_text("utf-8")
        revoked = json.loads(granted)
        for name, principals in revoked["audiences"].items():
            if PURCHASING in principals:
                revoked["audiences"][name] = sorted({OPS, INGEST})
        self.audience.write_text(json.dumps(revoked), "utf-8")

        seen, text = surfaces(buyer)
        self.assertEqual(seen, [])
        self.assertNotIn(number, text)
        # The frozen open-time audience still excludes operations from the old case.
        self.assertEqual(surfaces(operations)[0], [])
        # A request after the grant change investigates again: the fresh case is
        # narrowed to the newly granted reader, never the stale purchasing case.
        again = live_ws.CaseOpener(self.runtime, self.investigator, self.state,
                                   audience=AudienceMap.from_file(self.audience)
                                   ).open_case("petra_po", number)
        self.assertEqual(again["status"], "opened", again)
        self.assertNotEqual(again["case_id"], case_id)
        fresh = live_ws.workspace(self.runtime, operations, again["case_id"])
        self.assertIsNotNone(fresh)
        self.assertIn(number, fresh["question"])
        self.assertIsNone(live_ws.workspace(self.runtime, buyer, again["case_id"]))

        self.audience.write_text(granted, "utf-8")
        seen, text = surfaces(buyer)
        self.assertEqual(seen, everywhere)
        self.assertIn(number, text)
        self.assertEqual(self.runtime.cases.get(buyer, case_id)["roots"],
                         self.runtime.cases.get(self.investigator, case_id)["roots"])


    def test_the_leak_check_fails_a_case_whose_words_name_a_withheld_po(self):
        """Regression (review of e041d6f): opened without the narrowed audience, a PO
        number reaches an operations reader through the question, roots and uncited gaps;
        the check counts it as a row leak, never as allowed case text."""
        from operating.mcp_server import MCPServer, local_client

        number = "PO100888"
        call = local_client(MCPServer(case_runtime=self.runtime, actor=self.investigator),
                            "unnarrowed")
        [row] = self.runtime.mirror.rows(self.investigator, "public.app_purchase_order",
                                         where=[["po_number", "eq", number]])["rows"]
        case_id = call("plx.case_open", {"question": f"What happened to {number}?",
                                         "roots": [number]})["case_id"]
        call("plx.case_add_finding", {"case_id": case_id, "kind": "fact",
                                      "status": "supported", "statement": "The PO row.",
                                      "citations": [row["citation"]]})
        call("plx.case_record_gap", {"case_id": case_id, "missing": f"Receipts of {number}",
                                     "why": "not read", "who_can_answer": ["purchasing"],
                                     "state": "unknown"})
        reader = Actor("plx", frozenset({OPS}))
        full = live_ws.workspace(self.runtime, self.investigator, case_id)
        view = live_ws.workspace(self.runtime, reader, case_id)
        self.assertIsNotNone(view)  # the default audience lets operations in
        check = live_ws.leak_check(self.runtime, self.investigator, reader, full, view,
                                   AudienceMap.from_file(self.audience))
        self.assertGreaterEqual(check["case_text_leaks"], 1)
        self.assertGreaterEqual(check["row_leaks"], check["case_text_leaks"])


def _customer_drive(po: bool = True):
    """A FakeDrive standing for the PLXCUSTOMERS site: the spec's scope names the site,
    resolved here to the fixture site's drive, whose 'Customer Documents' folder holds one
    customer PO naming order OR1000888001 (synthetic); without ``po``, a bill of lading
    instead (no customer PO filed)."""
    from integrations.tests import test_capture_sharepoint as tsp
    from operating.tests.test_attested_missing import customer_po, pdf

    class CustomerDrive(tsp.FakeDrive):
        def __call__(self, method, url, headers, body, timeout, max_bytes):
            return super().__call__(method, url.replace("/sites/PLXCUSTOMERS", "/sites/AGENTS"),
                                    headers, body, timeout, max_bytes)

    drive = CustomerDrive()
    drive.folder("01CD", "Customer Documents", drive=tsp.SITE_DRIVE)
    if po:
        drive.file("01CPO", "PO 4500000001.pdf", "01CD",
                   pdf(customer_po("4500000001", "OR1000888001")), drive=tsp.SITE_DRIVE)
    else:
        from operating.tests.test_attested_missing import BOL
        drive.file("01CBL", "BL.pdf", "01CD", pdf(BOL), drive=tsp.SITE_DRIVE)
    return drive


class PickMissedReferenceTest(unittest.TestCase):
    """case-live-cause-honest: a held supplier confirmation names a PO the capture does
    not hold. The pick says ``pick_linked_unjoined`` and counts it (``linked_refs_unheld``);
    while the mail capture is unfinished that capture stays the cause
    (``pick_source_not_captured:graph_mail``: a confirmation it missed may name a held
    PO), the count still reported, and while the host recorded mail as refused the pick is
    blocked on it (``blocked_upstream:graph_mail``), never unjoined (decision
    2026-10-19-pick-cause-mail-unfinished). The count answers the reference as the link writer
    does (``mentions.mention_candidates``): a PO a person confirmed the number as an alias
    of holds it. Evidence label: injected-transport (the spec command over the fake
    Portal, BC and Graph)."""

    def test_a_confirmation_naming_an_unheld_po_is_counted_under_uncaptured_mail(self):
        from tests import test_investigate_chain_documents as chain

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        unheld = (("A1", "pi.txt", chain.CONFIRMATION.replace("PO100777", "PO100555")),)
        code, document, _text = chain.run_spec(temp.name, documents=unheld)
        [case] = document["cases"]
        self.assertEqual((code, case["status"], case["missing"]),
                         (1, "failed", "pick_linked_unjoined"), case)
        # The mail capture did not finish (as a daily run reads 2 of 10 folders).
        state = Path(temp.name) / "state"
        runtime = live_ws.offline_runtime(state, chain.MIRROR_AUDIENCE, "plx")
        self.addCleanup(runtime.close)
        opener = live_ws.CaseOpener(runtime, chain.PURCHASING, state,
                                    audience=AudienceMap.from_file(chain.MIRROR_AUDIENCE))
        self.assertEqual(opener.open_case("pick", "PurchaseOrder+SupplierConfirmation")
                         ["missing"], "pick_linked_unjoined")
        with contextlib.closing(sqlite3.connect(opener.projection())) as db, db:
            [(value,)] = db.execute(
                "SELECT value FROM store_meta WHERE key = 'source_freshness'").fetchall()
            freshness = json.loads(value)
            for facts in freshness["graph_mail"].values():
                facts["last_full_traversal_at"] = None
            db.execute("UPDATE store_meta SET value = ? WHERE key = 'source_freshness'",
                       (json.dumps(freshness),))
        unjoined = opener.open_case("pick", "PurchaseOrder+SupplierConfirmation")
        # A capture-state cause: the newest readable PO roots the case without one.
        self.assertEqual((unjoined["status"], unjoined["missing"]),
                         ("unlinked", "pick_source_not_captured:graph_mail"), unjoined)
        self.assertTrue(unjoined["case_id"], unjoined)
        for pick in (case["pick"], unjoined["pick"]):
            self.assertEqual((pick["linked_held"], pick["linked_to_start"],
                              pick["linked_refs_unheld"]), (1, 0, 1), pick)
        self.assertNotIn("PO100555", json.dumps(unjoined))
        # The host's latest mail read was refused: blocked on the grant, not unjoined.
        opener.refused = {"graph_mail": [{"part": "mail.mailbox_lookup"}]}
        refused = opener.open_case("pick", "PurchaseOrder+SupplierConfirmation")
        self.assertEqual((refused["status"], refused["missing"]),
                         ("blocked", "blocked_upstream:graph_mail"), refused)
        opener.refused = {}
        # A person confirms the confirmation's number as a held PO's alias: the writer
        # would link it, so the reference is held (the projection is not rebuilt).
        from ontology import reviews
        with contextlib.closing(sqlite3.connect(opener.projection())) as db:
            [(kind,)] = {(ref["identifier_kind"],) for (value,) in db.execute(
                "SELECT value FROM object_values WHERE property = 'evidence' AND object_id "
                "IN (SELECT object_id FROM objects WHERE object_type = 'SupplierConfirmation')")
                for ref in json.loads(value).get("references") or ()
                if ref.get("normalized") == "PO100555"}
            [(subject, handle, digest)] = db.execute(
                "SELECT o.object_id, v.l1_handle, v.digest FROM objects o JOIN "
                "object_versions v ON v.object_id = o.object_id WHERE o.object_type = "
                "'PurchaseOrder' AND v.superseded_at IS NULL ORDER BY o.object_id LIMIT 1"
            ).fetchall()
        reviews.ReviewStore(reviews.store_path(state)).record(
            tenant="plx", author={"principal": "reviewer@plx.test", "kind": "human"},
            decision="alias", subject=subject,
            alias={"identifier_kind": kind, "normalized": "PO100555", "raw": "PO100555"},
            evidence=[{"object_id": subject, "l1_handle": handle, "digest": digest,
                       "field_path": "row.id"}])
        aliased = opener.open_case("pick", "PurchaseOrder+SupplierConfirmation")
        self.assertEqual((aliased["status"], aliased["missing"],
                          aliased["pick"]["linked_refs_unheld"]),
                         ("unlinked", "pick_source_not_captured:graph_mail", 0), aliased)


class GapDateTest(unittest.TestCase):
    """da836e2f3f55 item 6: a gap's prose keeps a date its own claims state (the claim gate
    checks it) and writes every other bare date and number as "#"."""

    PO = "api://public.app_purchase_order/%7B%22id%22%3A%22po-1%22%7D@g-1"
    FM = "api://public.generated_purchase_order/%7B%22id%22%3A7%7D@g-2"

    def record(self, refuse_claims=False):
        calls = []

        def call(tool, args):
            if refuse_claims and "value_claims" in args:
                raise RuntimeError("refused")
            calls.append(args)
            return {}
        live_ws._Recorder(call, "case-x", upstream=None).conflict({
            "kind": "value_disagreement", "subject": "PurchaseOrder.expected_date",
            "note": "The Portal says 2026-01-07, FM 2026-01-09; read 2026-10-01T12:00:00Z, "
                    "3 rows.",
            "values": [{"value": value, "citation": {"l1_handle": handle, "field_path": field}}
                       for value, handle, field in (
                           ("2026-01-07", self.PO, "row.expected_date"),
                           ("2026-01-09", self.FM, "row.expected"))]})
        [recorded] = calls
        return recorded

    def test_a_gap_shows_the_dates_its_claims_cover_and_masks_the_rest(self):
        from operating import claim_checks

        recorded = self.record()
        self.assertEqual(recorded["why"], "value_disagreement: The Portal says 2026-01-07, FM "
                                          "2026-01-09; read #, # rows.")
        rows = {self.PO: {"relation": "public.app_purchase_order",
                          "row": {"expected_date": "2026-01-07T00:00:00Z"}},
                self.FM: {"relation": "public.generated_purchase_order",
                          "row": {"expected": "2026-01-09"}}}
        claims = claim_checks.parse_value_claims(recorded["value_claims"],
                                                 recorded["citations"], [])
        report = claim_checks.check_items([{
            "id": "G1", "texts": [recorded["missing"], recorded["why"]], "value_claims": claims,
            "citations": [{"handle": h, "status": "resolved"} for h in recorded["citations"]]}],
            rows=rows.get)
        self.assertTrue(report["passed"], report["failures"])
        # Recorded without its claims, the gap states no date.
        self.assertEqual(self.record(refuse_claims=True)["why"],
                         "value_disagreement: The Portal says #, FM #; read #, # rows.")


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "the customer PO is a PDF: needs pypdf and reportlab")
class CustomerPoMissingTest(unittest.TestCase):
    """``devloop/live-checks/case.customer_po_missing.json`` (REC-01) over injected
    sources: its own command, its host config copied with the in-process FakeWorkbench
    (the synthetic order fixture plus order OR1000888001 holding no PO) and a FakeDrive
    for the site; the network refused. Evidence label: injected-transport."""

    SPEC_ID = "case.customer_po_missing"
    ORDER = "OR1000888001"

    def setUp(self) -> None:
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("network access attempted in an offline test")

        for target in ("connect", "connect_ex"):
            patcher = mock.patch.object(socket.socket, target, refuse)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.spec = json.loads((ROOT / "devloop" / "live-checks" / f"{self.SPEC_ID}.json")
                               .read_text("utf-8"))

    def run_spec(self, *, served=None, fetch=None, po=True) -> tuple[int, dict, str]:
        from integrations.tests import test_capture_sharepoint as tsp
        from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
        from operating.tests import order_fixture as fixture

        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        if served is not None:   # the sales-order columns the Portal serves
            fake.catalog["public.app_sales_order"]["columns"] = served
        fake.tables = fixture.tables()
        fake.tables["public.app_sales_order"].append(
            fixture._so("so-m", self.ORDER, "cust-1", "CONFIRMED", "28"))
        config = json.loads((ROOT / "examples" / "customer_po_missing_host.live-check.json")
                            .read_text("utf-8"))
        for key in ("capture_audience", "live_audience", "mirror_audience"):
            config[key] = str(ROOT / "examples" / config[key])
        config["sharepoint"]["scope"] = str(ROOT / "examples" / config["sharepoint"]["scope"])
        (self.temp / "host.json").write_text(json.dumps(config), "utf-8")
        argv = [str(self.temp / "host.json") if arg.endswith("_host.live-check.json")
                else str(ROOT / arg) if arg.startswith("examples/")
                else arg.replace("{tmp}", str(self.temp)) for arg in self.spec["command"][3:]]
        drive = _customer_drive(po)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = live_ws.main(argv + ["--output-dir", str(self.temp / "views")], environ={
                **tsp.ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY},
                graph_sender=drive, host_options={
                    "sender": drive, "workbench_fetch": fetch or fake, "sleep": lambda _s: None})
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        return code, json.loads(out.getvalue()), out.getvalue()

    def runtime(self):
        from integrations.tests import test_capture_sharepoint as tsp
        from operating.case_runtime import build_live_readers

        live = build_live_readers(ROOT / "examples" /
                                  "customer_po_missing_live_audience.live-check.json",
                                  tsp.ENV, graph_sender=_customer_drive())
        runtime = live_ws.offline_runtime(
            self.temp / "state", ROOT / "examples" / "unfamiliar_case_audience.example.json",
            "plx", live=live)
        self.addCleanup(runtime.close)
        return runtime

    @staticmethod
    def leads(view) -> list[dict]:
        return [item for items in view["findings"].values() for item in items
                if str(item.get("statement") or "").startswith("Lead (")]

    @staticmethod
    def recorder(runtime, actor, case_id, *, refuse_findings=False):
        """A _Recorder writing onto the case store for ``actor``; with
        ``refuse_findings`` every finding is refused, as a host that cannot cite it."""
        def call(tool, args):
            args = dict(args)
            if tool == "plx.case_add_finding" and refuse_findings:
                raise RuntimeError("citation_not_recordable")
            if tool == "plx.read":   # no page: the lead cites the document itself
                raise RuntimeError("page_not_found")
            if tool == "plx.case_record_gap":
                return runtime.cases.gap(actor, args.pop("case_id"), args["missing"],
                                         args["why"], args["who_can_answer"], args["state"],
                                         citations=args["citations"])
            raise AssertionError(f"unexpected tool {tool}")
        return live_ws._Recorder(call, case_id, upstream=None)

    def test_a_document_po_on_an_order_without_one_is_one_lead_cited_to_the_page(self):
        from devloop import live_verify as lv
        from operating.case_verification import check_claims

        self.assertEqual(lv.validate_spec(self.spec, stem=self.SPEC_ID), [])
        self.assertEqual(self.spec["cadence"], "daily")
        code, document, text = self.run_spec()
        self.assertEqual(lv.check_expectations(self.spec, code, document), [], document)
        [case] = document["cases"]
        self.assertEqual((case["request"], case["status"]), ("pick", "opened"))
        investigator, reader = case["readers"]
        self.assertEqual(investigator["leads_by_kind"], {"missing_in_record": 1})
        # The reader reads the order but no document: the lead is withheld from it.
        self.assertEqual((reader["status"], reader["leads_by_kind"]), ("read", {}))
        self.assertGreater(reader["withheld"]["findings"], 0)
        self.assertIn("leads_by_kind", json.dumps(lv._recorded(self.spec, document)))
        for value in ("4500000001", self.ORDER, "so-m", "sharepoint://", "api://"):
            self.assertNotIn(value, text)
        views = {path.name: json.loads(path.read_text("utf-8"))
                 for path in (self.temp / "views").glob("*.json")}
        full = next(v for name, v in views.items() if "sales" in name)
        # The pick rests on the document-field lead: its gap cites the document, so the
        # reader who reads no document sees neither the gap nor the document's type.
        [rests] = [gap for gap in full["gaps"]
                   if str(gap.get("why")).startswith("pick_rests_on_lead")]
        self.assertTrue(rests["citations"], rests)
        self.assertTrue(all(c["handle"].startswith("sharepoint://")
                            for c in rests["citations"]), rests)
        restricted = next(v for name, v in views.items() if "sales" not in name)
        self.assertNotIn("pick_rests_on_lead", json.dumps(restricted["gaps"]))
        self.assertNotIn("CustomerPoDocument", json.dumps(restricted))
        [lead] = self.leads(full)
        # An unknown fact citing the page that states the PO and the empty order row; the
        # PO is a quote of that page, never a value of the order and never a proposal.
        self.assertEqual((lead["kind"], lead["status"]), ("fact", "unknown"))
        self.assertTrue(lead["statement"].startswith(
            "Lead (missing_in_record): SalesOrder.customer_po vs "
            "CustomerPoDocument.po_number: the cited document page states a value the cited "
            "record fields hold empty"), lead["statement"])
        page, row = [cite["handle"] for cite in lead["citations"]]
        self.assertTrue(page.startswith("sharepoint://") and page.endswith("page=1"), page)
        self.assertTrue(row.startswith("api://public.app_sales_order/"), row)
        self.assertEqual({cite["status"] for cite in lead["citations"]}, {"resolved"})
        self.assertEqual(full["proposals"], [])
        self.assertFalse([item for items in full["findings"].values() for item in items
                          if str(item.get("statement")).startswith("SalesOrder.customer_po")])
        runtime = self.runtime()
        sales = Actor("plx", frozenset({"staff:sales", OPS}), "restricted")
        report = check_claims(runtime, sales, full["case_id"])
        # No figure the case states from the document fails the claim gate's re-read.
        documents = {item["id"] for items in full["findings"].values() for item in items
                     if any(c["handle"].startswith("sharepoint://") for c in item["citations"])}
        self.assertIn(lead["id"], documents)
        self.assertFalse([f for f in report["failures"] if f["item"] in documents], report)
        # The document's figure is claimed and passes the gate's re-read of the pinned
        # bytes' documentFields row (CaseCaptures); a reader the document is withheld
        # from checks none of it.
        figures = [(claim["value"], claim["status"], claim["citation"])
                   for entry in report["items"] if entry["item"] in documents
                   for claim in entry["claims"]
                   if claim.get("field") == "row.fields.po_number.value"]
        self.assertEqual([(value, status) for value, status, _ in figures],
                         [("4500000001", "passed")], report)
        pinned = figures[0][2]
        self.assertRegex(pinned, r"^sharepoint://.*#sha256=[0-9a-f]{64}$")
        ops = Actor("plx", frozenset({OPS}), "restricted")
        self.assertLessEqual(documents, set(check_claims(runtime, ops, full["case_id"])
                                            ["unchecked_items"]))
        # A figure the bytes do not state fails the re-read: the row is read, not assumed.
        wrong = runtime.cases.finding(
            sales, full["case_id"], "CustomerPoDocument.po_number = 4500000009.", "fact",
            "unknown", citations=[pinned], value_claims=[
                {"value": "4500000009", "citation": pinned,
                 "field": "row.fields.po_number.value"}])
        self.assertEqual([f["code"] for f in check_claims(runtime, sales, full["case_id"])
                          ["failures"] if f["item"] == wrong["finding_id"]],
                         ["claim_value_mismatch"])
        # A lead the case cannot record is a gap citing the lead's own sources, so it is
        # withheld from the reader of the order who reads no document, as the lead is.
        refusing = self.recorder(runtime, sales, full["case_id"], refuse_findings=True)
        empty = {"value": None, "citation": {"l1_handle": row}}
        stated = {"value": "4500000001", "citation": {"l1_handle": pinned}}
        refusing.lead({"kind": "missing_in_record", "subject": "SalesOrder.customer_po vs "
                       "CustomerPoDocument.po_number", "values": [stated, empty]})
        refusing.lead({"kind": "value_guess", "subject": "SalesOrder.ship_to vs "
                       "CustomerPoDocument.ship_to", "values": [stated, empty]})
        self.assertEqual(refusing.gaps, 2)

        def lead_gaps(actor):
            gaps = runtime.cases.get(actor, full["case_id"])["gaps"]
            return [gap for gap in gaps
                    if str(gap.get("why") or "").startswith(("lead_not_recordable",
                                                             "lead_kind_unknown"))], gaps

        shown, _ = lead_gaps(sales)
        self.assertEqual(sorted({c["handle"] for gap in shown for c in gap["citations"]}),
                         sorted({pinned, row}))
        self.assertEqual(len(shown), 2)
        hidden, every = lead_gaps(ops)
        self.assertEqual(hidden, [])
        for named in ("CustomerPoDocument.po_number", "CustomerPoDocument.ship_to"):
            self.assertNotIn(named, json.dumps(every))
        # A withheld value gives no lead: an investigator the document is withheld from
        # opens the same order and its case records none.
        opener = live_ws.CaseOpener(runtime, Actor("plx", frozenset({OPS}), "restricted"),
                                    self.temp / "state", audience=AudienceMap.from_file(
                                        ROOT / "examples" / "unfamiliar_case_audience.example.json"))
        opened = opener.open_case("sales_order", self.ORDER)
        self.assertEqual(opened["status"], "opened", opened)
        view = live_ws.workspace(runtime, opener.actor, opened["case_id"])
        self.assertEqual((live_ws.structure(view)["leads_by_kind"], self.leads(view)), ({}, []))
        self.assertNotIn("4500000001", json.dumps(view))

    def test_a_lead_of_an_unknown_kind_or_without_a_record_side_is_never_a_finding(self):
        calls = []
        recorder = live_ws._Recorder(lambda tool, args: calls.append((tool, args)) or {},
                                     "case-x", upstream=None)
        doc, order = "sharepoint://d/i@%22e%22", "api://public.app_sales_order/k@g-1"
        document = {"value": "X", "citation": {"l1_handle": doc}}
        empty = {"value": None, "citation": {"l1_handle": order}}
        recorder.lead({"kind": "value_guess", "subject": "SalesOrder.x vs Doc.y",
                       "values": [document, empty]})
        recorder.lead({"kind": "missing_in_record", "subject": "SalesOrder.x vs Doc.y",
                       "values": [document]})
        # An unknown kind whose sources cannot be cited is not written: the store would
        # show an uncited gap naming the lead's fields to the whole case audience.
        recorder.lead({"kind": "value_guess", "subject": "SalesOrder.z vs Doc.z",
                       "values": [{"value": "X"}, "not a value"]})
        self.assertEqual([(tool, args.get("why", "").split(":")[0], args.get("citations"))
                          for tool, args in calls],
                         [("plx.case_record_gap", "lead_kind_unknown", [doc, order])])
        self.assertEqual(recorder.skipped["gaps"], 1)

    def test_a_gap_derived_from_leads_is_written_only_citing_their_documents(self):
        order_id = 'SalesOrder:portal.app_sales_order:{"id":"so-1"}'
        row, doc = "api://public.app_sales_order/k@g-1", "sharepoint://d/i@%22e%22#sha256=" + "0" * 64
        truncated = {"state": "unknown", "reason": "leads_truncated", "subject": order_id,
                     "detail": "3 missing-in-record leads, the first 1 shown"}

        def run(card, leads):
            calls = []

            def call(tool, args):
                calls.append((tool, args))
                return card if tool == "plx.object" else {}
            recorder = live_ws._Recorder(call, "case-x", upstream=None)
            recorder.tool_gaps([truncated], "plx.object", leads)
            return [args for tool, args in calls if tool == "plx.case_record_gap"], recorder

        card = {"values": [{"citation": {"l1_handle": row}}]}
        shown = [{"kind": "missing_in_record", "values": [
            {"value": "4500000001", "citation": {"l1_handle": doc}},
            {"value": None, "citation": {"l1_handle": row}}]}]
        [gap], _ = run(card, shown)
        self.assertEqual(gap["citations"], [row, doc])
        # No shown lead's document, or a record that cannot be cited: not written, never
        # the uncited "a SalesOrder record" form.
        for card_, leads in ((card, []), ({"error": {"code": "not_found"}}, shown)):
            written, recorder = run(card_, leads)
            self.assertEqual((written, recorder.skipped["gaps"]), ([], 1))

    def test_lead_page_lookups_read_each_page_once_within_one_bound(self):
        """N=1/10/60 leads: every lead is recorded once, a document's pages are read once
        for all its leads, and all page reads stay within MAX_LEAD_PAGE_READS."""
        pages = 5

        def world(n, documents):
            reads, findings = [], []

            def call(tool, args):
                if tool == "plx.read":
                    reads.append((args["citation"], args["page"]))
                    if args["page"] > pages:
                        return {"error": {"code": "page_not_found"}}
                    return {"content": {"untrusted_text": f"value {args['page']}",
                                        "origin": {"citation": f"{args['citation']}"
                                                               f"&page={args['page']}"}}}
                if tool == "plx.case_add_finding":
                    findings.append(args)
                    return {}
                raise AssertionError(tool)
            recorder = live_ws._Recorder(call, "case-x", upstream=None)
            for k in range(n):
                doc = f"sharepoint://d/i{k % documents}@%22e%22#sha256=" + "0" * 64
                recorder.lead({"kind": "missing_in_record", "subject": f"SalesOrder.f{k} vs D.g",
                               "values": [{"value": f"value {k % (pages + 2)}",
                                           "citation": {"l1_handle": doc}},
                                          {"value": None, "citation": {
                                              "l1_handle": f"api://public.so/k{k}@g-1"}}]})
            return reads, findings, recorder

        for n in (1, 10, 60):
            for documents in (1, n):
                reads, findings, recorder = world(n, documents)
                self.assertEqual(len(findings), n, (n, documents))
                self.assertEqual(len(reads), len(set(reads)), (n, documents))
                self.assertLessEqual(len(reads), live_ws.MAX_LEAD_PAGE_READS)
                self.assertEqual(recorder.page_reads, len(reads))
                if documents == 1:   # one document: its pages and the end, read once
                    self.assertLessEqual(len(reads), pages + 1)
                for args in findings:   # a page that states the value, else the document
                    cited = args["citations"][0]
                    self.assertTrue("&page=" in cited or cited.endswith("0" * 64), cited)

    def test_no_customer_po_filed_and_no_mail_captured_roots_the_order_with_a_typed_gap(self):
        """Live 2026-10-20: SharePoint captured in full, no file read as a customer PO, mail
        never configured; the pick said pick_linked_absent, naming no source, and opened
        nothing. The order is the case's root, the linked document a typed gap, and the
        run still fails naming the source never captured (no linked case was rendered)."""
        from devloop import live_verify as lv

        code, document, text = self.run_spec(po=False)
        [case] = document["cases"]
        self.assertEqual((code, case["status"], case.get("missing")),
                         (1, "unlinked", "pick_source_not_configured:graph_mail"), document)
        self.assertEqual((document["cases_rendered"], document["cases_unlinked"],
                          document["failed"], document["failed_unnamed"],
                          document["row_leaks"], document["withheld_relation_refs"]),
                         (0, 1, 1, 0, 0, 0), document)
        self.assertIn("cases_rendered < 1", " ".join(
            lv.check_expectations(self.spec, code, document)))
        self.assertIn("cases_unlinked", json.dumps(lv._recorded(self.spec, document)))
        investigator, reader = case["readers"]
        self.assertEqual((investigator["status"], reader["status"]), ("read", "read"))
        for value in (self.ORDER, "so-m", "sharepoint://", "api://"):
            self.assertNotIn(value, text)
        views = {path.name: json.loads(path.read_text("utf-8"))
                 for path in (self.temp / "views").glob("*.json")}
        full = next(v for name, v in views.items() if "sales" in name)
        [gap] = [gap for gap in full["gaps"]
                 if str(gap.get("why")).startswith("pick_linked_not_captured")]
        self.assertEqual(gap["state"], "not_ingested", gap)
        self.assertIn("pick_source_not_configured", gap["why"])
        # The gap every reader sees names neither the document type nor the source (the
        # plan's own uncited no_match gaps name the schema's link types, as on any order).
        restricted = next(v for name, v in views.items() if "sales" not in name)
        [shown] = [gap for gap in restricted["gaps"]
                   if str(gap.get("why")).startswith("pick_linked_not_captured")]
        for named in ("CustomerPoDocument", "graph_mail", "sharepoint", "mail"):
            self.assertNotIn(named, json.dumps(shown))
        self.assertNotIn("graph_mail", json.dumps(restricted))

    def test_an_unread_po_field_gives_no_lead_and_an_outage_is_blocked(self):
        from devloop import live_verify as lv
        from integrations.http import SourceHttpError
        from operating.tests import order_fixture as fixture

        columns = [c for c in fixture.CATALOG["public.app_sales_order"]["columns"]
                   if c != "po_reference"]
        code, document, _text = self.run_spec(served=columns)
        # REC-01 needs a lead: a rendered case without one is a truthful red.
        self.assertEqual(lv.check_expectations(self.spec, code, document),
                         ["missing cases[*].readers[*].leads_by_kind.missing_in_record"],
                         document)
        [case] = document["cases"]
        self.assertEqual(case["status"], "opened")
        self.assertEqual([r["leads_by_kind"] for r in case["readers"]], [{}, {}])

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        self.temp = self.temp / "outage"
        self.temp.mkdir()
        code, document, _text = self.run_spec(fetch=portal_down)
        self.assertTrue(lv.blocked_parts(document), document)
        self.assertEqual(lv.check_expectations(self.spec, code, document,
                                               self.spec["expect_blocked"]), [], document)


class ReviewedCustomerLinkTest(unittest.TestCase):
    """case.unfamiliar_order's join pick over injected sources (the synthetic order
    fixture, a BC capture of one BC-native invoice carrying its customer's PO): the Portal
    customer reaches the BC customer only as a lead, so the scope party is unread until a
    person's decision in the state directory's review store reaches the projection
    (``project_state`` runs ``ontology.reviews.link_step``). Evidence label:
    injected-transport."""

    def test_only_a_reviewed_customer_link_in_the_review_store_joins_the_pick(self):
        import sqlite3

        from ontology import reviews
        from operating.tests import test_unfamiliar_order_case as uo

        world = uo.OrderWorld(self, invoices=[uo.native("CPO-7781")], customers=uo.CARDS)
        carrier = "portal.app_sales_order.po_reference"
        # The stored-key link and the decisions on it; the company-name candidate the
        # projector also writes for the pair is a separate lead this case never decides.
        not_by_name = "binding_kind <> 'text_match'"

        def pick() -> tuple[dict, dict | None, list[str]]:
            opener = world.opener()
            opened = opener.open_case("pick", uo.JOIN)
            with contextlib.closing(sqlite3.connect(opener.projection())) as db:
                tiers = sorted(tier for (tier,) in db.execute(
                    "SELECT tier FROM links WHERE link_type = ? AND from_object = ? AND "
                    f"superseded_at IS NULL AND {not_by_name}",
                    (link["link_type"], link["from_object"])))
            return opened, opener.root, tiers

        with contextlib.closing(sqlite3.connect(world.opener().projection())) as db:
            db.row_factory = sqlite3.Row
            [link] = [dict(row) for row in db.execute(
                "SELECT * FROM links WHERE link_type = 'Customer.same_record_as' AND "
                f"from_object = ? AND {not_by_name}", (uo.PORTAL_CUSTOMER,))]
        [pair] = json.loads(link["reason"])
        evidence = [{"object_id": oid, **{key: cite[key] for key in
                                          ("l1_handle", "digest", "field_path")}}
                    for oid, cite in zip((link["from_object"], link["to_object"]),
                                         pair["citations"])]
        store = reviews.ReviewStore(reviews.store_path(world.state))

        def decide(decision: str) -> None:
            store.record(tenant="plx", author={"principal": "employee:reviewer"},
                         decision=decision, subject=link["from_object"],
                         target=link["to_object"], link_type=link["link_type"],
                         evidence=evidence)

        # A lead (the BC company is not known) never joins: the scope party is unread.
        opened, root, tiers = pick()
        self.assertEqual((link["tier"], opened.get("missing"), root,
                          opened["pick"]["carrier_outcomes"][carrier]),
                         ("lead", "pick_none_found", None, {"scope_unread": 1}), opened)
        # A person accepts it: the next projection holds it reviewed and the pick joins.
        decide("accept_link")
        opened, root, tiers = pick()
        self.assertEqual((opened["status"], root["key"], tiers,
                          opened["pick"]["carrier_outcomes"][carrier]),
                         ("opened", {"id": "so-a"}, ["lead", "reviewed"], {"joined": 1}),
                         opened)
        # The customer number now finds one customer held in both systems; asked again
        # with no decision since, that case is reused.
        customer = world.opener().open_case("find", "C0001")
        self.assertEqual(customer["status"], "opened", customer)
        self.assertEqual(world.opener().open_case("find", "C0001"),
                         {"request": "find", "status": "reused", "case_id": customer["case_id"]})
        # Another tenant's opener projects into its own file, without the plx decision
        # (read with the same review store): the plx projection keeps it.
        other = live_ws.CaseOpener(world.runtime, Actor("other", uo.INVESTIGATOR.principals),
                                   world.state, audience=AudienceMap.from_file(world.audience))
        self.assertNotEqual(other.projection(), world.opener().projection_path)
        with contextlib.closing(sqlite3.connect(other.projection())) as db:
            self.assertEqual([tier for (tier,) in db.execute(
                f"SELECT tier FROM links WHERE link_type = ? AND from_object = ? AND {not_by_name}",
                (link["link_type"], link["from_object"]))], ["lead"])
        with contextlib.closing(sqlite3.connect(world.opener().projection_path)) as db:
            self.assertIn("reviewed", [tier for (tier,) in db.execute(
                "SELECT tier FROM links WHERE link_type = ? AND from_object = ?",
                (link["link_type"], link["from_object"]))])
        # A later rejection supersedes it: contradicted beside the projector's own lead
        # (``link_step`` ends only the other steps' links), so never joined again.
        decide("reject_link")
        opened, root, tiers = pick()
        self.assertEqual((opened.get("missing"), root, tiers,
                          opened["pick"]["carrier_outcomes"][carrier]),
                         ("pick_none_found", None, ["contradicted", "lead"],
                          {"scope_unread": 1}), opened)
        # The case opened while the two were one customer is not reused after the
        # rejection: asked again, the number names two records.
        self.assertEqual(world.opener().open_case("find", "C0001"),
                         {"request": "find", "status": "failed",
                          "missing": "ambiguous_identifier"})
        # A review store that is there but unreadable fails the case, named: never a
        # projection built without the person's decisions.
        reviews.store_path(world.state).write_bytes(b"not a review store")
        for kind, identifier in (("pick", uo.JOIN), ("find", "C0001")):
            self.assertEqual(world.opener().open_case(kind, identifier),
                             {"request": kind, "status": "failed",
                              "missing": "error:ReviewStoreUnreadable"})


class JoinPathTest(unittest.TestCase):
    """case.unfamiliar_order's join pick (``pick=SalesInvoice>SalesOrder``) over the paths
    of two hops the schema declares beside the direct carriers (``join_carriers``): a BC
    invoice's orderNumber to the Portal order renumbered to it (``SalesInvoice.bills`` >
    ``SalesOrder.same_record_as``), and its shipment code to the Portal shipment and on to
    that shipment's order (``SalesInvoice.for_shipment`` > ``Shipment.ships``), as the
    Portal resolves an order's invoices (plx-customer-portal@d52c32c
    document-finance-queries.ts:47-123). Evidence label: injected-transport."""

    RENUMBERED = "portal.app_sales_order.custom_fields.renumberedTo"
    SHIPPED = "portal.app_shipment.shipment_code"

    @staticmethod
    def finds(opener, spec=None):
        """The pick's result and every (object_type, query) it asked plx.find."""
        from operating.tests import test_unfamiliar_order_case as uo

        seen = []
        real = live_ws._Session.__call__

        def spy(session, tool, arguments):
            if tool == "plx.find":
                seen.append((arguments.get("object_type"), arguments["query"]))
            return real(session, tool, arguments)
        with mock.patch.object(live_ws._Session, "__call__", spy):
            return opener.open_case("pick", spec or uo.JOIN), seen

    def test_an_invoice_joins_through_a_renumbered_order_despite_a_prefixed_reference(self):
        from operating.tests import order_fixture as fixture
        from operating.tests import test_unfamiliar_order_case as uo

        orders = [{**row, "custom_fields": {**row["custom_fields"], "renumberedTo": "S-ORD20001"}}
                  if row["id"] == "so-b" else row
                  for row in fixture.tables()["public.app_sales_order"]]
        # Posted from BC order S-ORD20001, which the Portal's so-b was renumbered to; its
        # external reference names a UAT row the served replica never holds.
        world = uo.OrderWorld(self, invoices=[uo.invoice(8, "UAT-SO100018", "29",
                                                         order="S-ORD20001")],
                              tables={"public.app_sales_order": orders})
        opener = world.opener()
        opened, seen = self.finds(opener)
        self.assertEqual((opened["status"], (opener.root or {}).get("key")),
                         ("opened", {"id": "so-b"}), opened)
        replica = {"not_in_served_replica": 1}
        self.assertEqual(opened["pick"], {
            "mode": "join", "scanned": 1, "joined": True, "root_reasons": {},
            "carrier_outcomes": {
                self.RENUMBERED: {"joined": 1},
                # The same orderNumber as the Portal order's own number: no order holds it.
                "portal.app_sales_order.order_number/order_number": {
                    "no_portal_row_for_order_number": 1},
                # Only the prefixed reference is never searched, per carrier.
                **{key: replica for key in (
                    "portal.app_sales_order.order_number",
                    "portal.app_sales_order.custom_fields.fmOeNumber",
                    "portal.app_sales_order.custom_fields.erpCode",
                    "portal.app_sales_order.po_reference", self.SHIPPED)}},
            # The newest invoice joined: the pick never turned around.
            "directions": {
                "forward": {"scanned": 1, "joined": True, "budget_spent": False},
                "reverse": {"tried": False, "start_held": 0, "linked_held": 0,
                            "linked_matched": 0, "scanned": 0, "joined": False,
                            "reasons": {}, "budget_spent": False,
                            "bc": {"reader": False, "orders": 0,
                                   "orders_unreadable": 0, "requests": 0, "lookups": 0,
                                   "lookup_states": {}, "hits": 0, "scanned": 0}}}})
        self.assertEqual(seen, [("SalesOrder", "S-ORD20001")])

    def test_an_invoice_joins_through_its_shipment_and_only_declared_paths_are_tried(self):
        import dataclasses

        from operating.tests import test_unfamiliar_order_case as uo

        sales = uo.SALES  # reads Portal orders, not shipments (order_fixture.AUDIENCES)
        # Newest first: an orderNumber no Portal order holds (only BC's own order matches
        # it), then an invoice carrying sh-2's shipment code.
        world = uo.OrderWorld(self, invoices=[uo.invoice(10, None, "29", order="S-ORD30005"),
                                              uo.invoice(9, "SH100002", "28")],
                              grants={"public.app_sales_order": [OPS, sales]})
        opener = world.opener(uo.INVESTIGATOR)
        opened, seen = self.finds(opener)
        self.assertEqual((opened["status"], (opener.root or {}).get("key")),
                         ("opened", {"id": "so-b"}), opened)
        self.assertEqual(opened["pick"]["root_reasons"], {"identity_lead_only": 1})
        self.assertEqual((opened["pick"]["carrier_outcomes"][self.RENUMBERED],
                          opened["pick"]["carrier_outcomes"][self.SHIPPED]),
                         ({"identity_lead_only": 1, "no_identifier": 1},
                          {"no_identifier": 1, "joined": 1}))
        self.assertEqual(seen, [("SalesOrder", "S-ORD30005"), ("SalesOrder", "SH100002"),
                                ("Shipment", "SH100002")])
        # The invoice and the shipment it joined through are cited beside the order.
        case = world.runtime.cases.get(uo.INVESTIGATOR, opened["case_id"])
        handles = {c["handle"] for item in uo.findings(case) for c in item["citations"]}
        self.assertTrue(any("app_shipment/" in h for h in handles), sorted(handles))
        self.assertTrue(any(h.startswith("bc://") for h in handles), sorted(handles))
        # A reader without the shipment relation: that path is never searched, never joined.
        opened, seen = self.finds(world.opener(Actor("plx", frozenset({sales}))))
        self.assertEqual((opened.get("missing"), opened["pick"]["carrier_outcomes"][self.SHIPPED]),
                         ("pick_none_found", {"no_identifier": 1,
                                              "portal_row_unreadable_for_reader": 1}), opened)
        self.assertNotIn("Shipment", {object_type for object_type, _q in seen})
        # A schema without the for_shipment link declares no such path: never tried.
        schema = dataclasses.replace(live_ws._schema(), link_types={
            name: link for name, link in live_ws._schema().link_types.items()
            if name != "SalesInvoice.for_shipment"})
        with mock.patch.object(live_ws, "_schema", lambda: schema):
            opened, seen = self.finds(world.opener())
        self.assertEqual(opened.get("missing"), "pick_none_found", opened)
        self.assertNotIn(self.SHIPPED, opened["pick"]["carrier_outcomes"])
        self.assertNotIn("Shipment", {object_type for object_type, _q in seen})



class JoinAliasCompanyTest(unittest.TestCase):
    """The join pick over a path whose hops read the number as two kinds the identifier
    registry declares aliases of one another, and BC numbers scoped by company on every
    cross-system path (``join_carriers``, ``CaseOpener._join``). A BC invoice posted from an
    order BC numbered with the Portal's own number (Manual Nos, renumberedTo unset:
    plx-customer-portal@340ef1e portal/src/lib/business-central/align-portal-so-number.ts
    sets order_number and renumberedTo together only when BC numbered differently) joins
    that order through ``SalesInvoice.bills`` > ``SalesOrder.same_record_as``
    (bc_document_no, so_number), a lead; the same number held in a second BC company names
    no one order: never joined, ``cross_scope``. Evidence label: injected-transport."""

    OWN = "portal.app_sales_order.order_number/order_number"
    RENUMBERED = JoinPathTest.RENUMBERED
    FINANCE = "staff:finance"

    def world(self, number, *, second_company=False, tables=None, restricted=False):
        """An OrderWorld whose first BC company holds an invoice posted from BC order
        ``number``; with ``second_company`` the second company holds a newer one of the
        same order number, whose records only ``FINANCE`` reads when ``restricted``."""
        from integrations.live_source_guard import LiveSourceAudience
        from integrations.tests import test_bc_capture as tb
        from operating.tests import test_unfamiliar_order_case as uo

        real = uo.bc_world

        def bc_world(invoices, customers=()):
            fake = real(invoices, customers)
            if second_company:
                fake.data[(tb.CO[1], "salesInvoices")] = [
                    {**uo.invoice(20, None, "30", order=number), "id": uo.gid(2, 12, 20)}]
            return fake

        def audience(grants, **kwargs):
            if restricted:
                grants = {f"bc:production:{tb.CO[0]}:*": grants["bc:production:*"],
                          f"bc:production:{tb.CO[1]}:*": [self.FINANCE]}
            return LiveSourceAudience(grants, **kwargs)

        with mock.patch.object(uo, "bc_world", bc_world), \
                mock.patch.object(uo, "LiveSourceAudience", audience):
            return uo.OrderWorld(self, invoices=[uo.invoice(10, None, "29", order=number)],
                                 tables=tables)

    @staticmethod
    def renumbered(number):
        from operating.tests import order_fixture as fixture

        return {"public.app_sales_order": [
            {**row, "custom_fields": {**row["custom_fields"], "renumberedTo": number}}
            if row["id"] == "so-b" else row
            for row in fixture.tables()["public.app_sales_order"]]}

    def test_an_invoice_joins_the_portal_order_bc_kept_the_number_of_as_a_lead(self):
        from operating import ontology_tools
        from operating.tests import test_unfamiliar_order_case as uo

        opener = self.world("SO100305").opener()   # so-b's own order_number
        opened = opener.open_case("pick", uo.JOIN)
        self.assertEqual((opened["status"], (opener.root or {}).get("key")),
                         ("opened", {"id": "so-b"}), opened)
        self.assertEqual(opened["pick"]["carrier_outcomes"][self.OWN], {"joined": 1})
        # The invoice is cited beside the order as a lead, never as evidence.
        case = opener.runtime.cases.get(uo.INVESTIGATOR, opened["case_id"])
        cited = [item for item in uo.findings(case)
                 if any(c["handle"].startswith("bc://") for c in item["citations"])]
        self.assertTrue(cited)
        self.assertTrue(all(item["status"] == "unknown" and "lead" in item["statement"]
                            for item in cited), cited)
        # The path comes from the registry: kinds not declared aliases of one another
        # compose no path, so the same invoice joins nothing.
        grammar = ontology_tools.default_grammar()

        class Unaliased:
            def __getattr__(self, name):
                return getattr(grammar, name)

            @staticmethod
            def kind_spec(kind):
                spec = dict(grammar.kind_spec(kind))
                if kind == "so_number":
                    spec["alias_kinds"] = []
                return spec
        with mock.patch.object(ontology_tools, "default_grammar", Unaliased):
            self.assertNotIn(self.OWN, map(live_ws.carrier_key,
                                           live_ws.join_carriers("SalesInvoice", "SalesOrder")))
            opened = self.world("SO100305").opener().open_case("pick", uo.JOIN)
        self.assertEqual((opened.get("missing"), opened["pick"]["root_reasons"]),
                         ("pick_none_found", {"identity_lead_only": 1}), opened)

    def test_a_bc_number_another_company_also_holds_joins_nothing(self):
        from operating.tests import test_unfamiliar_order_case as uo

        # The own number and a renumbered one: each joins in one company, and no invoice
        # of either company joins once a second company holds the same number.
        for number, tables, key in (("SO100305", None, self.OWN),
                                    ("S-ORD20001", self.renumbered("S-ORD20001"),
                                     self.RENUMBERED)):
            with self.subTest(key=key):
                opener = self.world(number, tables=tables).opener()
                opened = opener.open_case("pick", uo.JOIN)
                self.assertEqual((opened["status"], opened["pick"]["carrier_outcomes"][key]),
                                 ("opened", {"joined": 1}), opened)
                opener = self.world(number, tables=tables, second_company=True).opener()
                opened = opener.open_case("pick", uo.JOIN)
                self.assertEqual((opened.get("missing"), opener.root),
                                 ("pick_none_found", None), opened)
                self.assertEqual((opened["pick"]["root_reasons"],
                                  opened["pick"]["carrier_outcomes"][key]),
                                 ({"cross_scope": 2}, {"cross_scope": 2}))

    def test_a_company_the_tools_do_not_show_fails_closed(self):
        from operating.tests import test_unfamiliar_order_case as uo

        # The invoice's own company unread (no cited capture handle), or a find page that
        # may leave another company's record out: never joined.
        real = live_ws._Session.__call__

        def uncited(session, tool, arguments):
            out = real(session, tool, arguments)
            if tool == "plx.object" and out.get("object", {}).get("object_type") == \
                    "SalesInvoice":
                out = {**out, "values": [{k: v for k, v in item.items() if k != "citation"}
                                         for item in out.get("values") or []]}
            return out

        def paged(session, tool, arguments):
            out = real(session, tool, arguments)
            if tool == "plx.find":
                out = {**out, "completeness": {**out.get("completeness", {}),
                                               "complete": False}}
            return out
        for spy in (uncited, paged):
            with self.subTest(spy=spy.__name__), \
                    mock.patch.object(live_ws._Session, "__call__", spy):
                opened = self.world("SO100305").opener().open_case("pick", uo.JOIN)
                self.assertEqual((opened.get("missing"),
                                  opened["pick"]["carrier_outcomes"][self.OWN]),
                                 ("pick_none_found", {"cross_scope": 1}), opened)

    def test_a_reader_of_one_company_is_told_nothing_of_the_other(self):
        from operating.tests import test_unfamiliar_order_case as uo

        # The second company's records are finance's: the investigator neither reads its
        # invoice nor learns its order number is held there.
        opener = self.world("SO100305", second_company=True, restricted=True).opener()
        opened = opener.open_case("pick", uo.JOIN)
        self.assertEqual((opened["status"], (opener.root or {}).get("key"),
                          opened["pick"]["root_reasons"]),
                         ("opened", {"id": "so-b"}, {"root_not_readable": 1}), opened)
        self.assertNotIn("cross_scope", opened["pick"]["carrier_outcomes"][self.OWN])
        # Finance, who reads both companies' records and the Portal orders, sees both.
        reader = Actor("plx", frozenset({*uo.INVESTIGATOR.principals, self.FINANCE}))
        opened = self.world("SO100305", second_company=True,
                            restricted=True).opener(reader).open_case("pick", uo.JOIN)
        self.assertEqual(opened["pick"]["root_reasons"], {"cross_scope": 2}, opened)

class CaseProjectionConfirmationTest(unittest.TestCase):
    """``project_state`` applies the confirmations of the state directory as the host's
    build does (``build_steps.post_project``), over captured SharePoint files (the
    SharePoint capture tests' seeded drive through the in-process Graph sender), and writes
    neither decision store. Evidence label: injected-transport."""

    def test_a_confirmation_recorded_after_a_case_projection_reaches_the_next_one(self):
        from contextlib import ExitStack
        import sqlite3

        from integrations.capture_sharepoint import SCHEME
        from integrations.live_source_guard import parse_handle
        from integrations.tests import test_capture_sharepoint as tsp
        from ontology import reviews
        from operating import host, kind_confirmation

        case = tsp.CaptureCase("run")
        case.setUp()
        self.addCleanup(case.tearDown)
        case.capture().run(tsp.scope())
        state = case.state
        path = live_ws.projection_file(state, "plx")

        def values(store: Path) -> list[tuple]:
            with contextlib.closing(sqlite3.connect(store)) as db:
                return db.execute(
                    "SELECT object_id, property, value, l1_handle, recorded_at, superseded_at "
                    "FROM object_values WHERE lineage = 'workbench' AND binding_id = ? "
                    "ORDER BY 1, 2, 4", (kind_confirmation.PROJECTED_BINDING,)).fetchall()

        live_ws.project_state(state, path, tenant="plx")   # the case's projection, before
        self.assertEqual(values(path), [])
        with contextlib.closing(sqlite3.connect(path)) as db:
            files = db.execute(
                "SELECT o.object_id, v.l1_handle, v.digest, v.recorded_at, c.value FROM objects o "
                "JOIN object_versions v ON v.object_id = o.object_id JOIN object_values c ON "
                "c.object_id = v.object_id AND c.l1_handle = v.l1_handle AND c.property = "
                "'content_sha256' WHERE o.object_type = 'SharePointFile' AND "
                "v.superseded_at IS NULL ORDER BY o.object_id").fetchall()
        self.assertGreaterEqual(len(files), 2, files)
        (confirmed, handle, _, at, content), (legacy, legacy_handle, legacy_digest,
                                              legacy_at, _) = files[:2]
        # A person confirms one file through plx.document_fields ...
        (drive_id, item_id), etag, _ = parse_handle(handle, SCHEME, 2)
        record = {"schema": kind_confirmation.SCHEMA, "id": "kc-rec-" + "c" * 24,
                  "tenant": "plx", "original_id": "orig-" + "c" * 32,
                  "citation": "original://orig-" + "c" * 32, "original_sha256": content,
                  "provenance": {"source": kind_confirmation.SHAREPOINT_ORIGIN,
                                 "drive_id": drive_id, "item_id": item_id, "etag": etag},
                  "confirmed_fields": {"DocumentCategory": "COA", "DocumentType": "Quality"},
                  "document_kind": "coa", "amended_columns": [],
                  "confirmed_by": "employee:00000000-0000-0000-0000-00000000000a",
                  "confirmed_at": at, "written_to_sources": False}
        kinds = kind_confirmation.KindConfirmations(kind_confirmation.store_path(state))
        with contextlib.closing(kinds._db()) as db:
            kinds._append(db, record)
        # ... and the review store holds a document type of the other file from before
        # kinds moved, which the host's next build imports into the confirmation store.
        reviews.ReviewStore(reviews.store_path(state))
        cite = {"object_id": legacy, "l1_handle": legacy_handle, "digest": legacy_digest}
        with contextlib.closing(sqlite3.connect(reviews.store_path(state))) as db, db:
            db.execute("INSERT INTO reviews (review_id, tenant, review_key, decision, subject, "
                       "document_kind, evidence, author, recorded_at) VALUES "
                       "(?, 'plx', ?, 'document_kind', ?, 'QUOTE', ?, ?, ?)",
                       ("rev-" + "e" * 32, reviews.review_key("document_kind", legacy), legacy,
                        json.dumps([cite]), json.dumps({"principal": "employee:reviewer"}),
                        legacy_at))
        stores = [kind_confirmation.store_path(state), reviews.store_path(state)]

        def snapshot() -> list[tuple[bytes, int]]:
            # Each store's file and the size of its write-ahead log (a read-only reader of
            # a WAL store leaves an empty one).
            wal = [Path(f"{store}-wal") for store in stores]
            return [(store.read_bytes(), log.stat().st_size if log.exists() else 0)
                    for store, log in zip(stores, wal)]

        before = snapshot()
        live_ws.project_state(state, path, tenant="plx")
        self.assertEqual(snapshot(), before)   # read-only: nothing written, nothing imported
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])
        case_values = values(path)
        with ExitStack() as stack:
            host.build_projection(state, host.state_captures(state, stack), tenant="plx")
        self.assertEqual(case_values, values(host.projection_path(state)))
        categories = {(oid, json.loads(value)["columns"]["DocumentCategory"])
                      for oid, prop, value, *_ in case_values
                      if prop == "proposed_portal_metadata"}
        self.assertEqual(categories, {(confirmed, "COA"), (legacy, kind_confirmation.taxonomy
                                                           .resolve_storage_category("QUOTE"))})
        self.assertNotEqual(snapshot(), before)   # the host's build imported the review


class DisagreeingValueLiveTest(unittest.TestCase):
    """The same ingestion with the FM twin of PO5447 reading CAD: a real disagreement."""

    @classmethod
    def setUpClass(cls) -> None:
        from operator_ui.tests import case_fixtures
        real_build = case_fixtures.build_world

        def build_disagreeing():
            world = real_build()
            for row in world.fake.tables["public.generated_purchase_order"]:
                if row.get("id") == 5447:
                    row["currency"] = "CAD"
            return world
        with mock.patch.object(case_fixtures, "build_world", build_disagreeing):
            LiveMirrorWorkspaceTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    runtime = LiveMirrorWorkspaceTest.runtime
    opener = LiveMirrorWorkspaceTest.opener

    def test_a_value_disagreement_is_an_evidence_conflict_citing_both_sides(self):
        runtime = self.runtime()
        opened = self.opener(runtime).open_case("petra_po", "PO5447", reinvestigate=True)
        self.assertEqual(opened["status"], "opened", opened)
        view = live_ws.workspace(runtime, Actor("plx", frozenset({OPS, QUALITY, PURCHASING})),
                                 opened["case_id"])
        conflicts = [c for c in view["conflicts"] if c["kind"] == "evidence_conflict"]
        [currency] = [c for c in conflicts if "PurchaseOrder.currency" in c.get("question", "")]
        for conflict in conflicts:
            [side] = conflict["sides"]
            self.assertEqual(side["side"], "source records")
            self.assertGreaterEqual(len(side["citations"]), 2)  # never one side picked
            self.assertEqual(conflict["state"], "unknown")
        self.assertEqual(len(currency["sides"][0]["citations"]), 2)


if __name__ == "__main__":
    unittest.main()
