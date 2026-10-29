"""Playbook evaluation (component F) against cases driven through the real MCP tools.

Evidence label: ``injected-transport``. The AO-26005B case is investigated through
``MCPServer`` over the in-process ``FakeWorkbench`` (the setup of ``test_ao_26005b_case``,
without the OCR part); the customer-PO case uses the fixtures of ``test_customer_po_coverage``.
The evaluator mechanics below run on the AO playbook **v1** over the frozen v1-shaped
investigation (``operating/tests/ao_v1_investigation.py``: v1 binds M3 adoption ledgers and
is the only AO version with a repeat sum); ``AoV2Tests`` scores the reference investigation
(``operating/tests/ao_reference_investigation.py``) with v2. Mutated cases are built by
driving MCP tools (a
wrapped caller that drops or adds one call), never by editing the case store.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import unittest
from typing import Any

from operating import mcp_server
from operating.mcp_server import HOST_READS
from operating import playbook_eval as pe
from operating import playbook_originals as po
from operating.tests import ao_reference_investigation as ao_reference
from operating.tests.ao_v1_investigation import investigate
from operating.playbooks import Playbook, PlaybookIntegrityError, PlaybookRegistry
from operating.tests import test_ao_26005b_case as ao_tests
from operating.tests import test_customer_po_coverage as cpo_tests
from operating import customer_po_coverage as cpo
from operating import source_reads

# The shipped directory plus the published v1/v2 AO definitions, which name a real case in
# their key hints and so live as test fixtures (byte-identical, hash-pinned below).
RETIRED_DIR = Path(__file__).with_name("fixtures") / "playbooks"
REGISTRY = PlaybookRegistry().load_directory().load_directory(RETIRED_DIR)
# The read tools the published v1/v2 allowed retired into these (wire-host); those files
# stay unchanged (retired versions, never scored) and v3 ships the new names. plx.source_citation
# and plx.case_verify_proposal merged into plx.read and plx.case_propose (host-budget: AO v4,
# CPO v3).
_SUCCESSORS = {"plx.source_resolve": ["plx.find"], "plx.source_rows": ["plx.find", "plx.object"],
               "plx.source_citation": ["plx.read"],
               "plx.case_verify_proposal": ["plx.case_propose"]}


# What the published absence gaps name, structured as a current version must (property).
_ABSENCE_PROPERTY = {"header_quantities_empty": ["AssemblyOrder.final_quantity",
                                                 "AssemblyOrder.ordered_quantity"]}


def current_copy(playbook):
    """A retired version's definition with its retired tools renamed to their successors
    and its absence gaps structured, in memory only: the same checks scored on today's
    host."""
    definition = copy.deepcopy(dict(playbook.definition))
    for item in definition["expected_gaps"]:
        if item["kind"] == "absent_in_source":
            item["property"] = _ABSENCE_PROPERTY[item["id"]]
            item["match"].pop("terms", None)
    for key in ("allowed_tools", "writes_case_state"):
        names = []
        for name in definition[key]:
            for successor in _SUCCESSORS.get(name, [name]):
                if successor not in names:
                    names.append(successor)
        definition[key] = names
    return Playbook(json.loads(json.dumps(definition)))


AO = current_copy(REGISTRY.get("ao_quantity_reconciliation", 1))
AO_V2 = current_copy(REGISTRY.get("ao_quantity_reconciliation", 2))
CPO = REGISTRY.get("customer_po_enrichment")
OPS_QUALITY = ao_tests.OPS_QUALITY



def _first_calculations(result):
    """The calculations list a plx.case_get response starts with: the full view's, or
    the calculations section page at offset 0 (a case that no longer fits one response
    once every string leaves as an untrusted_text envelope); else None."""
    if result.get("view") == "full":
        return result["calculations"]
    if (result.get("view"), result.get("section"), result.get("offset")) == (
            "section", "calculations", 0) and result.get("items"):
        return result["items"]
    return None

class Recorder:
    """Wraps a caller and records every tool name called through it."""

    def __init__(self, call):
        self._call, self.names = call, []

    def __call__(self, name, arguments):
        self.names.append(name)
        return self._call(name, arguments)


class AoEvalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = ao_tests.Ao26005bCaseTest("setUp")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.server = self.h.server(OPS_QUALITY, self.h.runtime())
        self.call = self.h.caller(self.server)
        self.transcript = []  # the tool names the investigation actually called

    def investigate(self, drop=None, extra=None):
        """Drive the real investigation; ``drop(name, args)`` swallows a call (a mutated
        investigation), ``extra(call, out)`` adds calls through the same MCP tools."""
        def wrapped(name, arguments):
            if drop and drop(name, arguments):
                return {"gap": {}, "calculation": {"result": "0"}}
            self.transcript.append(name)
            return self.call(name, arguments)

        def extra_call(name, arguments):
            self.transcript.append(name)
            return self.call(name, arguments)
        out = investigate(wrapped)
        if extra:
            extra(extra_call, out)
        return out

    def evaluate(self, case_id, playbook=AO, **kwargs):
        return pe.evaluate(playbook, self.call, case_id, **kwargs)

    def check(self, report, check_id):
        return next(item for item in report["checks"] if item["id"] == check_id)

    def failed(self, report):
        return [item["id"] for item in report["checks"] if not item["passed"]]


def _with_original_roles(base, *roles):
    """``base`` with extra ``original`` evidence roles: ((role, required), ...)."""
    definition = copy.deepcopy(base.definition)
    definition["version"] = definition["version"] + 100
    for role, required in roles:
        definition["required_evidence"].append({
            "role": role, "relation": "original", "identity": "any", "required": required,
            "key_hint": f"attached original recorded as {role}"})
    return Playbook(definition)


FORWARDER_QUOTE = ("OCEANLINK LOGISTICS\nFREIGHT QUOTATION No. OL000\nMode: AIR  Origin: NGB "
                   "Destination: IAD\nChargeable weight 412 kg  Valid until 2026-10-15")


class _Extracted:
    """A stand-in extraction (the store's extractor hook): one entry per page."""

    def __init__(self, pages):
        pages = [pages] if isinstance(pages, str) else list(pages)
        self.text, spans, start = "", [], 0
        for number, page in enumerate(pages, start=1):
            self.text += page + "\n"
            spans.append({"page": number, "start": start, "end": start + len(page)})
            start = len(self.text)
        self.pages, self.status, self.method = spans, "extracted", "fake"
        self.ocr_pages, self.warnings = [], []


def _with_detail(playbook, call, case_id, **kwargs):
    """``pe.evaluate`` with the session-only per-original detail in ``original_checks`` (the
    stored report carries ``public_original_checks`` of it, asserted here)."""
    detail: dict = {}
    report = pe.evaluate(playbook, call, case_id, originals_detail=detail, **kwargs)
    assert report["original_checks"] == po.public_original_checks(detail)
    return {**report, "original_checks": detail}


class OriginalChecksTests(AoEvalCase):
    """Original integrity and role binding read through the evaluator's own MCP session
    (injected-transport; extraction text is a synthetic stand-in). Recorded kinds (SharePoint
    column, human confirmation) are covered end to end in test_playbook_originals; here the
    attached originals have no recorded kind, as an operator inbox file does not."""

    def setUp(self) -> None:
        super().setUp()
        texts: dict[bytes, Any] = {}
        self.texts = texts
        runtime = ao_tests.CaseRuntime(
            self.h.state, client=ao_tests.WorkbenchReadApiClient(
                ao_tests.BASE, ao_tests.KEY, fetch=self.h.fake),
            audience=self.h.audience_path, tenant="plx",
            mirror_options={"clock": self.h.clock, "retry_delay_seconds": 0},
            originals_options={"extractor": lambda content, *a, **k: _Extracted(
                texts.get(bytes(content), ""))})
        self.addCleanup(runtime.close)
        self.runtime = runtime
        self.server = self.h.server(OPS_QUALITY, runtime)
        self.call = self.h.caller(self.server)

    def attach(self, case_id, name, pages, principal="staff:operations"):
        body = b"%PDF-1.4\n% synthetic " + name.encode() + b"\n"
        self.texts[body] = pages
        inbox = self.runtime.originals.inbox / principal
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / name).write_bytes(body)
        return self.call("plx.case_attach_original",
                         {"case_id": case_id, "inbox_name": name})["original"]

    def evaluate(self, case_id, playbook=AO, **kwargs):
        return _with_detail(playbook, self.call, case_id, **kwargs)

    def gaps(self, report):
        return sorted((g["gap"], g.get("handle") or g.get("role"))
                      for g in report["original_checks"]["gaps"])

    def quote_case(self):
        case_id = self.investigate()["case_id"]
        return case_id, self.attach(case_id, "OL000-Quotation-NGB-IAD-AIR.pdf", FORWARDER_QUOTE)

    def test_text_alone_never_fills_a_role_and_absence_stays_unknown(self):
        case_id, got = self.quote_case()
        report = self.evaluate(case_id, playbook=_with_original_roles(AO, ("freight_quote", True)))
        self.assertEqual(self.gaps(report), [("role_not_filled", "freight_quote"),
                                             ("role_unverifiable", got["citation"])])
        self.assertEqual(report["original_checks"]["page_reads"], 0)
        self.assertEqual(report["evaluator_version"], "plx-playbook-evaluator/14")
        self.assertFalse(report["summary"]["passed_all"])
        fill, unsure = po.role_fill(report["original_checks"])
        self.assertEqual((fill["freight_quote"], unsure), (set(), True))
        # no original role: integrity only, the cited handle re-read, no page read
        plain = self.evaluate(case_id)["original_checks"]
        self.assertEqual((plain["status"], plain["ok"], plain["page_reads"]),
                         ("integrity_only", True, 0))
        self.assertIsNone(plain["originals"][0]["document"])

    def test_a_refused_read_makes_the_evaluation_incomplete_not_failed(self):
        case_id, _got = self.quote_case()
        playbook = _with_original_roles(AO, ("freight_quote", True))

        def raw(name, arguments):  # the MCP session itself, rate limit included
            self.h.request_id += 1
            response = self.server.handle({"jsonrpc": "2.0", "id": self.h.request_id,
                                           "method": "tools/call",
                                           "params": {"name": name, "arguments": arguments}},
                                          host_code=True)  # the evaluator is host code
            result = response["result"]
            if result.get("isError"):
                raise RuntimeError(result["content"][0]["text"])
            return result["structuredContent"]
        now = mcp_server.monotonic()
        self.server.call_times.clear()
        self.server.call_times.extend([now] * (mcp_server.CALLS_PER_MINUTE - 4))
        with self.assertRaises(pe.EvaluationError) as raised:
            pe.evaluate(playbook, raw, case_id)
        self.assertIn("evaluation_incomplete", str(raised.exception))
        self.assertIn("rate_limit_exceeded", str(raised.exception))
        self.server.call_times.clear()
        self.assertIn("original_checks", pe.evaluate(playbook, raw, case_id))

    def test_a_required_original_role_with_nothing_cited_is_a_gap(self):
        case_id = self.investigate()["case_id"]
        report = self.evaluate(case_id, playbook=_with_original_roles(AO, ("vendor_pi", True)))
        self.assertEqual(self.gaps(report), [("role_not_filled", "vendor_pi")])
        self.assertFalse(report["original_checks"]["ok"])
        self.assertFalse(report["summary"]["passed_all"])
        plain = self.evaluate(case_id)
        self.assertEqual((plain["original_checks"]["status"], plain["original_checks"]["ok"]),
                         ("none_expected", True))
        self.assertTrue(plain["summary"]["passed_all"])

    def test_reads_use_the_evaluators_session_and_the_recorded_digest(self):
        case_id, got = self.quote_case()
        playbook = _with_original_roles(AO, ("freight_quote", True))

        def refused(name, arguments):
            if name == "plx.source_citation" and arguments["citation"].startswith("original://"):
                return {"ok": False, "error": "original_not_visible",
                        "citation": arguments["citation"]}
            return self.call(name, arguments)

        def renamed(name, arguments):
            result = self.call(name, arguments)
            if name == "plx.source_citation" and result.get("filename"):
                result = {**result, "filename": "Other.pdf"}  # not the view the case digested
            return result
        for caller, gap in ((refused, "original_not_visible"),
                            (renamed, "original_changed_since_cited")):
            with self.subTest(gap=gap):
                report = _with_detail(playbook, caller, case_id)
                self.assertEqual(self.gaps(report), sorted(
                    [(gap, got["citation"]), ("role_not_filled", "freight_quote")]))
                self.assertFalse(report["summary"]["passed_all"])
                self.assertIsNone(report["original_checks"]["originals"][0]["filename"])

    def test_a_reader_outside_the_audience_learns_nothing_of_the_original(self):
        case_id = self.investigate()["case_id"]
        got = self.attach(case_id, "OL000-Quotation-NGB-IAD-AIR.pdf", FORWARDER_QUOTE,
                          principal="staff:quality")
        ops = self.h.caller(self.h.server(ao_tests.OPS, self.runtime))
        report = _with_detail(_with_original_roles(AO, ("freight_quote", True)), ops, case_id)
        self.assertEqual(self.gaps(report), [("cited_evidence_withheld_from_reader", None),
                                             ("role_not_filled", "freight_quote")])
        self.assertEqual(report["original_checks"]["status"], "checked")
        text = json.dumps(report)
        self.assertNotIn(got["original_id"], text)
        self.assertNotIn(got["sha256"], text)
        self.assertNotIn("OL000", text)

    def test_an_agent_reader_reads_no_original_and_says_so(self):
        case_id, got = self.quote_case()
        seen = Recorder(self.call)
        report = _with_detail(_with_original_roles(AO, ("freight_quote", True)), seen, case_id,
                             reader_principals=["agent:workbench"])
        self.assertEqual(report["original_checks"]["status"], "not_read_agent_identity")
        self.assertEqual(self.gaps(report), [("original_not_visible_to_agent", got["citation"]),
                                             ("role_not_filled", "freight_quote")])
        self.assertFalse(report["summary"]["passed_all"])

    def test_compare_counts_a_new_original_gap_as_a_regression(self):
        case_id, _got = self.quote_case()
        # the stored (public) reports: compare() never needs an original's handle
        old = pe.evaluate(_with_original_roles(AO, ("freight_quote", True)), self.call, case_id)
        new = pe.evaluate(_with_original_roles(AO, ("freight_quote", True), ("vendor_pi", True)),
                          self.call, case_id)
        result = pe.compare(old, new, allow_changed_case=True)
        self.assertEqual(result["original_gaps_new"], ["role_not_filled vendor_pi"])
        self.assertEqual(result["newly_failing"], [])
        self.assertTrue(result["regression"])
        back = pe.compare(new, old, allow_changed_case=True)
        self.assertEqual(back["original_gaps_resolved"], ["role_not_filled vendor_pi"])
        self.assertFalse(back["regression"])


class AoPositiveTests(AoEvalCase):
    def test_the_real_investigation_passes_every_check(self):
        case_id = self.investigate()["case_id"]
        report = self.evaluate(case_id)
        self.assertEqual(self.failed(report), [], report["checks"])
        self.assertEqual(report["summary"]["checks"], 15)
        self.assertTrue(report["summary"]["passed_all"])
        self.assertEqual(report["authority_violations"], 0)
        self.assertEqual(report["definition_sha256"], AO.definition_sha256)
        self.assertTrue(pe.verify_report_hash(report))
        # Every evidence role is backed by cited ids, arithmetic check names its calculation.
        self.assertEqual(self.check(report, "damage_cited")["evidence"], ["C3"])
        self.assertIn("C1", self.check(report, "planned_arith")["evidence"])
        self.assertIn("= 6 EA", self.check(report, "planned_arith")["detail"])
        self.assertIn("= 417 EA", self.check(report, "ordered_arith")["detail"])

    def test_expected_gaps_hold_and_are_attributed_to_the_recorded_gaps(self):
        report = self.evaluate(self.investigate()["case_id"])
        gaps = {row["id"]: row for row in report["expected_gaps"]}
        self.assertEqual({key: (row["condition_held"], row["recorded"], row["status"])
                          for key, row in gaps.items()},
                         {"chain_unproven": (False, False, "ok"),
                          "dates_unreadable": (False, False, "ok"),
                          "late_reason_unknown": (True, True, "ok"),
                          "header_quantities_empty": (True, True, "ok"),
                          "original_scan_not_ingested": (True, True, "ok")})
        self.assertEqual(gaps["late_reason_unknown"]["recorded_gap_ids"], ["G1"])
        self.assertEqual(gaps["header_quantities_empty"]["recorded_gap_ids"], ["G2"])
        self.assertEqual(report["ambiguous_gap_ids"], [])

    def test_the_paged_summary_view_gives_the_same_report(self):
        case_id = self.investigate()["case_id"]
        full = self.evaluate(case_id)
        # 14 000, as in test_ao_26005b_case: the client reads case_get through the output
        # boundary, where every untyped string is an envelope naming its origin (the
        # Workbench's own text: workbench://plx.case_get), so the summary needs more than
        # the 10 000 it fitted bare.
        for envelope in (16_000, 14_000):
            with self.subTest(envelope=envelope), \
                    ao_tests.patch("operating.mcp_server.MAX_RESULT_BYTES", envelope):
                self.assertEqual(self.h.ok(self.server, "plx.case_get",
                                           {"case_id": case_id})["view"], "summary")
                self.assertEqual(self.evaluate(case_id), full)

    def test_the_report_hash_is_reproducible_and_tamper_evident(self):
        case_id = self.investigate()["case_id"]
        first, second = self.evaluate(case_id), self.evaluate(case_id)
        self.assertEqual(first, second)
        self.assertEqual(first["report_sha256"], second["report_sha256"])
        forged = copy.deepcopy(first)
        forged["checks"][0]["passed"] = False
        self.assertFalse(pe.verify_report_hash(forged))

    def test_a_same_case_in_a_fresh_runtime_gives_the_same_report(self):
        case_id = self.investigate()["case_id"]
        first = self.evaluate(case_id)
        later = self.h.server(OPS_QUALITY, self.h.runtime())
        again = pe.evaluate(AO, self.h.caller(later), case_id)
        self.assertEqual(again["report_sha256"], first["report_sha256"])

    def test_the_evaluator_calls_only_read_tools(self):
        case_id = self.investigate()["case_id"]
        recorder = Recorder(self.call)
        report = pe.evaluate(AO, recorder, case_id)
        self.assertTrue(report["summary"]["passed_all"])
        self.assertTrue(recorder.names)
        self.assertLessEqual(set(recorder.names), {"plx.case_get", "plx.source_citation",
                                                     "mirror.rows"})
        before = self.h.case_view(self.server, case_id)
        self.evaluate(case_id)
        self.assertEqual(self.h.case_view(self.server, case_id), before)  # case unchanged

    def test_a_write_tool_is_refused_before_it_reaches_the_server(self):
        case_id = self.investigate()["case_id"]
        recorder = Recorder(self.call)
        guarded = pe._ReadOnly(recorder)
        with self.assertRaises(pe.EvaluationError):
            guarded("plx.case_record_gap", {"case_id": case_id})
        self.assertEqual(recorder.names, [])


class AoV2Tests(AoEvalCase):
    """Playbook v2 over the reference investigation (``ao_reference_investigation``):
    finished
    goods derived from the unit-ratio component, damage per component, lateness and paper
    disagreements as recorded conflicts."""

    def investigate_v2(self, drop=None, extra=None):
        def wrapped(name, arguments):
            if drop and drop(name, arguments):
                return {"gap": {}, "calculation": {"result": "0"}}
            self.transcript.append(name)
            return self.call(name, arguments)
        out = ao_reference.investigate(wrapped, ao_code="AO-26005B")
        if extra:
            extra(self.call, out)
        return out

    def evaluate_v2(self, case_id, **kwargs):
        return pe.evaluate(AO_V2, self.call, case_id, **kwargs)

    def test_published_versions_are_unchanged_retired_and_v4_is_current(self):
        v1 = REGISTRY.get("ao_quantity_reconciliation", 1)
        v2 = REGISTRY.get("ao_quantity_reconciliation", 2)
        v3 = REGISTRY.get("ao_quantity_reconciliation", 3)
        v4 = REGISTRY.get("ao_quantity_reconciliation", 4)
        # M3 adoption ledgers bind the v1 definition; it is never edited in place.
        self.assertEqual(v1.definition_sha256,
                         "a3515167ad9709598880b29371f8f7686ad0c8ff72206a63fffe55ab61343797")
        self.assertEqual((v1.retired_tools, v2.retired_tools),
                         (["plx.source_citation", "plx.source_resolve", "plx.source_rows"],) * 2)
        self.assertEqual(v3.retired_tools, ["plx.source_citation"])
        self.assertFalse(v4.retired)
        self.assertEqual(REGISTRY.get("ao_quantity_reconciliation"), v4)
        # v4 is v3 with plx.read in place of plx.source_citation (host-budget).
        renamed = json.loads(json.dumps(dict(v3.definition)))
        renamed["version"] = 4
        renamed["allowed_tools"] = ["plx.read" if name == "plx.source_citation" else name
                                    for name in renamed["allowed_tools"]]
        self.assertEqual(renamed, json.loads(json.dumps(dict(v4.definition))))
        # v3 (and so v4) is v2 with the successor names, its key hints without case
        # examples and its absence gap structured (property, matched without wording).
        same = json.loads(json.dumps(dict(AO_V2.definition)))
        same["version"] = 4
        for item in same["required_evidence"]:
            item["key_hint"] = re.sub(r" \(e\.g\. [^)]*\)", "", item["key_hint"])
        self.assertEqual(same, json.loads(json.dumps(dict(v4.definition))))
        with self.assertRaisesRegex(pe.EvaluationError,
                                    "playbook_retired:plx.source_citation,plx.source_resolve"):
            pe.evaluate(v2, self.call, "case-x")
        cpo_v1 = REGISTRY.get("customer_po_enrichment", 1)
        self.assertTrue(cpo_v1.retired)
        self.assertEqual((CPO.version, CPO.retired), (3, False))
        self.assertEqual(REGISTRY.get("customer_po_enrichment", 2).retired_tools,
                         ["plx.case_verify_proposal", "plx.source_citation"])

    def test_the_product_investigation_passes_every_v2_check(self):
        out = self.investigate_v2()
        report = self.evaluate_v2(out["case_id"], transcript=self.transcript)
        self.assertEqual(self.failed(report), [], report["checks"])
        self.assertEqual(report["summary"]["checks"], 17)
        self.assertTrue(report["summary"]["passed_all"])
        self.assertEqual(report["authority_violations"], 0)
        self.assertEqual(report["definition_sha256"], AO_V2.definition_sha256)
        self.assertIn("= 6 EA", self.check(report, "planned_arith")["detail"])
        self.assertIn("= 1 EA", self.check(report, "filled_arith")["detail"])
        self.assertIn("= 417 EA", self.check(report, "ordered_arith")["detail"])
        # One component row is cited for the finished goods; damage rows stay per component.
        # (G1 is the unread Portal FG columns gap, which cites the AO row only.)
        self.assertEqual(self.check(report, "item_cited")["evidence"], ["C1", "C2", "C6", "F2",
                                                                       "F3", "F4", "G2"])
        self.assertEqual(len(self.check(report, "damage_cited")["evidence"]), 5)
        gaps = {row["id"]: (row["condition_held"], row["recorded"], row["status"])
                for row in report["expected_gaps"]}
        self.assertEqual(gaps, {"chain_unproven": (False, False, "ok"),
                                "dates_unreadable": (False, False, "ok"),
                                "lateness_conflict": (True, True, "ok"),
                                "header_quantities_empty": (True, True, "ok"),
                                "original_scan_not_ingested": (True, True, "ok")})
        self.assertEqual(report["ambiguous_gap_ids"], [])

    def test_v1_cannot_score_the_corrected_case(self):
        report = self.evaluate(self.investigate_v2()["case_id"])  # v1
        # (and v1 has no Line clearance role, so that cited task row is unbound for v1)
        self.assertEqual(self.failed(report), ["filling_cited", "planned_arith", "filled_arith",
                                               "damaged_arith", "ordered_arith"])

    def test_dropping_the_lateness_conflict_fails_the_lateness_check(self):
        out = self.investigate_v2(drop=lambda name, args: name == "plx.case_record_gap"
                                  and "governing required date" in args["missing"])
        report = self.evaluate_v2(out["case_id"])
        self.assertEqual(self.failed(report), ["lateness_gap"])

    def test_on_time_fm_dates_owe_no_lateness_conflict(self):
        self.h.fake.tables["public.order_entry"][0]["datereq"] = "2026-01-10"
        report = self.evaluate_v2(self.investigate_v2()["case_id"])
        gap = next(row for row in report["expected_gaps"] if row["id"] == "lateness_conflict")
        self.assertFalse(gap["condition_held"])
        self.assertEqual(self.failed(report), [])

    def test_a_paper_figure_written_into_the_case_fails_the_number_check(self):
        """Why the paper leads stay in the report until the original is attached."""
        def extra(call, out):
            call("plx.case_record_gap", {
                "case_id": out["case_id"], "state": "unknown",
                "missing": "Box damage per the paper sheet",
                "why": "The pack's OCR reads 11 boxes damaged on paper.",
                "who_can_answer": ["Ricardo (Quality)"], "citations": out["cite"]["damage"]})
        report = self.evaluate_v2(self.investigate_v2(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), ["no_uncited_numbers"])
        self.assertIn("11", self.check(report, "no_uncited_numbers")["detail"])

    def test_skipping_the_per_unit_division_beside_the_good_calculation_fails(self):
        def extra(call, out):
            item = out["cite"]["item"]
            call("plx.case_compute", {
                "case_id": out["case_id"], "unit": "EA", "expression": "planned - produced",
                "relations": [{"left": "planned", "op": "-", "right": "actual",
                               "as": "planned_not_produced"}],
                "inputs": {"planned": {"citation": item, "field": "row.quantity", "unit": "EA"},
                           "produced": {"citation": item, "field": "row.actual_amount",
                                        "unit": "EA"}}})
        report = self.evaluate_v2(self.investigate_v2(extra=extra)["case_id"])
        # It touches the produced input every derived computation shares: all three fail.
        self.assertEqual(self.failed(report), ["planned_arith", "filled_arith", "ordered_arith"])
        self.assertIn("not an exact binding", self.check(report, "planned_arith")["detail"])

    def test_citing_the_box_row_as_the_basis_fails(self):
        """Review 47646d9: the box row (qty_per_batch 0.25, planned 378 rounded up) as the
        basis gives planned - produced = 9 EA; v2 must refuse it."""
        def box_basis(rows, ao_id):
            return next(row for row in rows if row["row"]["id"] == 58386), ""
        with ao_tests.patch(f"{ao_reference.__name__}.fg_basis", box_basis):
            out = self.investigate_v2()
        self.assertEqual(out["results"]["planned_minus_produced"], "9")
        report = self.evaluate_v2(out["case_id"])
        self.assertEqual(self.failed(report), ["item_cited", "fg_basis", "planned_arith",
                                               "filled_arith", "ordered_arith"])
        self.assertIn("no resolved citation", self.check(report, "item_cited")["detail"])
        self.assertFalse(report["summary"]["passed_all"])

    def test_a_basis_that_another_unit_ratio_row_disagrees_with_fails(self):
        # The label row 58385 (qty_per_batch 1) says 1499 were produced; the cited jug row
        # says 1503. The script refuses such a basis; a client that cites one anyway fails.
        self.h.fake.tables["silver.assembly_order_items"][3]["actual_amount"] = "1499.0"
        real = ao_reference.fg_basis

        def ignoring_the_label(rows, ao_id):
            basis, _why = real([row for row in rows if row["row"]["id"] != 58385], ao_id)
            return basis, ""
        # A client that also hides the conflict (never cites the label row): only the
        # evaluator's own enumeration of the qty_per_batch 1 source rows can catch it.
        with ao_tests.patch(f"{ao_reference.__name__}.fg_basis", ignoring_the_label):
            out = self.investigate_v2(drop=lambda name, args: name == "plx.case_record_gap"
                                      and "every component" in args["missing"])
        report = self.evaluate_v2(out["case_id"])
        self.assertEqual(self.failed(report), ["fg_basis"])
        detail = self.check(report, "fg_basis")["detail"]
        self.assertIn("disagree with the cited basis", detail)
        self.assertIn("%3A58385", detail)
        # Recorded honestly, the conflict gap cites the label row: the basis is then
        # ambiguous for every derived computation too.
        self.setUp()
        self.h.fake.tables["silver.assembly_order_items"][3]["actual_amount"] = "1499.0"
        with ao_tests.patch(f"{ao_reference.__name__}.fg_basis", ignoring_the_label):
            out = self.investigate_v2()
        self.assertEqual(self.failed(self.evaluate_v2(out["case_id"])),
                         ["fg_basis", "planned_arith", "filled_arith", "ordered_arith"])

    def test_a_cited_second_unit_ratio_row_makes_the_basis_ambiguous(self):
        def extra(call, out):
            [cap] = call("mirror.rows", {"relation": "silver.assembly_order_items",
                                             "where": [["id", "eq", 58384]]})["rows"]
            call("plx.case_add_finding", {
                "case_id": out["case_id"], "kind": "fact", "status": "supported",
                "statement": "The cap row is also consumed one per finished unit.",
                "citations": [cap["citation"]]})
        report = self.evaluate_v2(self.investigate_v2(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), ["fg_basis", "planned_arith", "filled_arith",
                                               "ordered_arith"])
        self.assertIn("ambiguous", self.check(report, "planned_arith")["detail"])

    def test_the_basis_and_damage_checks_enumerate_the_source(self):
        report = self.evaluate_v2(self.investigate_v2()["case_id"])
        basis = self.check(report, "fg_basis")
        self.assertEqual((basis["row_completeness"], basis["rows_enumerated"]),
                         ("enumerated", 3))  # jug, cap, label: the qty_per_batch 1 rows
        damage = self.check(report, "damage_per_component")
        self.assertEqual((damage["passed"], damage["row_completeness"],
                          damage["rows_enumerated"]), (True, "enumerated", 3))
        self.assertIn("3 row.assembly_item_id group(s)", damage["detail"])

    def test_a_mixed_component_pack_total_fails_the_damage_check(self):
        def extra(call, out):
            call("plx.case_compute", {
                "case_id": out["case_id"], "unit": "pack", "expression": "d0 + d1 + d2",
                "inputs": {f"d{i}": {"citation": handle, "field": "row.qty_damaged",
                                     "unit": "pack"}
                           for i, handle in enumerate(out["cite"]["damage"])}})
        report = self.evaluate_v2(self.investigate_v2(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), ["damage_per_component"])
        self.assertIn("sums rows of 3 different", self.check(
            report, "damage_per_component")["detail"])

    def test_a_missing_or_doubled_component_group_fails_the_damage_check(self):
        out = self.investigate_v2(drop=lambda name, args: name == "plx.case_compute"
                                  and args["unit"] == "item58386")
        report = self.evaluate_v2(out["case_id"])
        # The swallowed call's placeholder result also leaves an uncited number in the
        # damage finding.
        self.assertEqual(self.failed(report), ["no_uncited_numbers", "damage_per_component"])
        self.assertIn("never counted", self.check(report, "damage_per_component")["detail"])
        self.setUp()

        def extra(call, out):
            handle = out["cite"]["damage"][0]
            call("plx.case_compute", {
                "case_id": out["case_id"], "unit": "item58386x", "expression": "d0 * 2",
                "inputs": {"d0": {"citation": handle, "field": "row.qty_damaged",
                                  "unit": "item58386x"}}})
        report = self.evaluate_v2(self.investigate_v2(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), ["damage_per_component"])
        self.assertIn("already summed", self.check(report, "damage_per_component")["detail"])


class AoMutationTests(AoEvalCase):
    def test_a_case_missing_the_late_production_gap_fails_the_gap_expectation(self):
        out = self.investigate(drop=lambda name, args: name == "plx.case_record_gap"
                               and args["missing"].startswith("Why AO-26005B"))
        report = self.evaluate(out["case_id"])
        gap = next(row for row in report["expected_gaps"] if row["id"] == "late_reason_unknown")
        self.assertEqual((gap["condition_held"], gap["recorded"], gap["status"]),
                         (True, False, "missing"))
        self.assertEqual(report["summary"]["gaps_missing"], 1)
        # The other expected gaps are still recorded and their checks still pass.
        self.assertEqual(self.failed(report), [])
        self.assertFalse(report["summary"]["passed_all"])  # an expected gap is missing

    def test_a_missing_required_gap_check_fails(self):
        for missing, check_id in (("The original AO-26005B production scan", "scan_gap"),
                                  ("AO-26005B header quantities", "header_gap")):
            with self.subTest(check=check_id):
                self.setUp()
                out = self.investigate(drop=lambda name, args, m=missing:
                                       name == "plx.case_record_gap"
                                       and args["missing"].startswith(m))
                report = self.evaluate(out["case_id"])
                self.assertEqual(self.failed(report), [check_id])
                self.assertIn("no", self.check(report, check_id)["detail"])

    def test_a_finding_with_an_uncited_number_fails(self):
        def extra(call, out):
            call("plx.case_add_finding", {
                "case_id": out["case_id"], "kind": "fact", "status": "supported",
                "statement": "AO-26005B filled 1502 EA of which 77 EA passed final inspection.",
                "citations": [out["cite"]["filling"]]})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), ["no_uncited_numbers"])
        detail = self.check(report, "no_uncited_numbers")["detail"]
        self.assertIn("77", detail)
        self.assertNotIn("1502", detail)  # 1502 is in the cited filling row
        self.assertTrue(self.check(report, "facts_cite")["passed"])

    def test_an_uncited_date_fails(self):
        def extra(call, out):
            call("plx.case_add_finding", {
                "case_id": out["case_id"], "kind": "fact", "status": "supported",
                "statement": "The customer agreed a new date of 2026-02-14.",
                "citations": [out["cite"]["order"]]})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), ["no_uncited_numbers"])
        self.assertIn("2026-02-14", self.check(report, "no_uncited_numbers")["detail"])

    def test_a_calculation_that_disagrees_with_the_cited_arithmetic_fails(self):
        def extra(call, out):
            item = out["cite"]["item"]
            call("plx.case_compute", {
                "case_id": out["case_id"], "unit": "EA", "expression": "planned + manufactured",
                # The roles differ, so compute needs the relation declared; the evaluator
                # must still catch the arithmetic.
                "relations": [{"left": "planned", "op": "+", "right": "actual", "as": "planned_plus_actual"}],
                "inputs": {"planned": {"citation": item, "field": "row.quantity", "unit": "EA"},
                           "manufactured": {"citation": item, "field": "row.actual_amount",
                                            "unit": "EA"}}})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), ["planned_arith"])
        detail = self.check(report, "planned_arith")["detail"]
        self.assertIn("C5", detail)
        self.assertIn("gives 6", detail)

    def test_a_missing_calculation_fails_its_check(self):
        # A case that never computed the ordered-vs-manufactured figure: drive the same
        # investigation with that compute call swallowed.
        out = investigate(lambda name, args: {"calculation": {"result": "0"}}
                          if name == "plx.case_compute" and args["expression"] == "ordered - manufactured"
                          else self.call(name, args))
        report = self.evaluate(out["case_id"])
        self.assertEqual(self.failed(report), ["ordered_arith"])
        self.assertIn("no recorded calculation", self.check(report, "ordered_arith")["detail"])

    def test_an_item_of_another_ao_leaves_roles_and_calculations_unproven(self):
        self.h.fake.tables["silver.assembly_order_items"][1]["assembly_order_id"] = 10719
        report = self.evaluate(self.investigate()["case_id"])
        # The script records the chain gap and computes only the damage total; the filling
        # row is never cited and the three chain-dependent calculations are absent.
        gap = next(row for row in report["expected_gaps"] if row["id"] == "chain_unproven")
        self.assertEqual((gap["condition_held"], gap["recorded"]), (True, True))
        # item_cited fails too since identity binding: the cited item belongs to another AO.
        self.assertEqual(self.failed(report), ["item_cited", "filling_cited", "planned_arith",
                                               "filled_arith", "ordered_arith"])

    def test_a_contradicted_chain_makes_the_chain_gap_expected(self):
        tables = self.h.fake.tables
        tables["public.order_entry_item"].append(
            {"id": 9999, "idoe": 352, "idfp": 1534, "idbulk": 1091, "qty": "1920.0",
             "status_c": "Completed"})
        tables["public.order_entry_item_assembly"][0]["idoeitem"] = 9999
        report = self.evaluate(self.investigate()["case_id"])
        gaps = {row["id"]: row for row in report["expected_gaps"]}
        self.assertTrue(gaps["chain_unproven"]["condition_held"])
        self.assertTrue(gaps["chain_unproven"]["recorded"])
        self.assertFalse(gaps["late_reason_unknown"]["condition_held"])
        self.assertEqual(gaps["chain_unproven"]["status"], "ok")
        self.assertEqual(report["ambiguous_gap_ids"], [])

    def test_on_time_production_expects_no_late_gap(self):
        self.h.fake.tables["public.order_entry"][0]["datereq"] = "2026-01-10"
        report = self.evaluate(self.investigate()["case_id"])
        gaps = {row["id"]: row for row in report["expected_gaps"]}
        self.assertFalse(gaps["late_reason_unknown"]["condition_held"])
        self.assertEqual(self.failed(report), [])

    def test_unreadable_dates_expect_the_dates_gap(self):
        self.h.fake.tables["public.order_entry"][0]["datereq"] = "01/01/2026"
        report = self.evaluate(self.investigate()["case_id"])
        gaps = {row["id"]: row for row in report["expected_gaps"]}
        self.assertEqual((gaps["dates_unreadable"]["condition_held"],
                          gaps["dates_unreadable"]["recorded"]), (True, True))
        self.assertFalse(gaps["late_reason_unknown"]["condition_held"])
        self.assertEqual(report["ambiguous_gap_ids"], [])


class AuthorityTests(AoEvalCase):
    def test_a_transcript_within_allowed_tools_has_no_violation(self):
        case_id = self.investigate()["case_id"]
        transcript = [{"tool": name} for name in AO.definition["allowed_tools"]] + ["plx.case_get"]
        report = self.evaluate(case_id, transcript=transcript)
        self.assertEqual(report["authority_violations"], 0)
        self.assertTrue(report["summary"]["transcript_evaluated"])

    def test_the_actual_tool_calls_of_the_investigation_are_within_allowed_tools(self):
        case_id = self.investigate()["case_id"]
        self.assertIn("plx.case_compute", self.transcript)
        # Direct mirror reads and the host-code citation read are not host tools
        # (operating.source_reads, mcp_server.HOST_READS).
        self.assertLessEqual(set(self.transcript) - source_reads.READS - HOST_READS,
                             set(AO.definition["allowed_tools"]))
        report = self.evaluate(case_id, transcript=self.transcript)
        self.assertEqual(report["authority_violations"], 0)
        self.assertTrue(report["summary"]["passed_all"])

    def test_a_tool_outside_allowed_tools_is_an_authority_violation(self):
        case_id = self.investigate()["case_id"]
        report = self.evaluate(case_id, transcript=[
            {"tool": "plx.case_open"}, {"tool": "plx.case_propose"}, "plx.case_close",
            {"name": "plx.case_get"}])
        self.assertEqual(report["authority_violations"], 2)
        self.assertEqual([item["detail"] for item in report["authority_violation_details"]],
                         ["plx.case_propose", "plx.case_close"])
        self.assertFalse(report["summary"]["passed_all"])
        self.assertEqual(self.failed(report), [])  # the checks themselves still pass

    def test_a_transcript_entry_with_a_business_write_is_a_violation(self):
        case_id = self.investigate()["case_id"]
        report = self.evaluate(case_id, transcript=[
            {"tool": "plx.case_compute", "business_system_writes": 1}])
        self.assertEqual(report["authority_violations"], 1)
        self.assertEqual(report["authority_violation_details"][0]["kind"], "business_system_write")

    def test_a_malformed_transcript_entry_is_a_violation(self):
        report = self.evaluate(self.investigate()["case_id"], transcript=[42])
        self.assertEqual(report["authority_violations"], 1)


class CompareTests(AoEvalCase):
    def test_a_stricter_version_that_now_fails_is_a_regression(self):
        case_id = self.investigate()["case_id"]
        old = self.evaluate(case_id)
        stricter = copy.deepcopy(AO.definition)
        stricter["version"] = 2
        stricter["expected_gaps"].append({
            "id": "tariff_unknown", "kind": "unknown", "about": "Tariff treatment of the goods",
            "when": "always", "match": {"terms": ["tariff"]}})
        stricter["success_checks"].append({"id": "tariff_gap", "type": "gap_recorded",
                                           "params": {"gap": "tariff_unknown"}})
        new = self.evaluate(case_id, playbook=pe.Playbook(stricter))
        result = pe.compare(old, new)
        self.assertEqual(result["newly_failing"], [])  # new check, not a changed check
        self.assertEqual(result["added_checks"], ["tariff_gap"])
        self.assertEqual(self.check(new, "tariff_gap")["passed"], False)
        self.assertEqual(new["summary"]["failed_ids"], ["tariff_gap"])
        # no changed check fails, but the case no longer passes as a whole
        self.assertEqual((result["passed_all"], result["regression"]),
                         ({"old": True, "new": False}, True))

    def test_a_check_that_passed_and_now_fails_is_newly_failing(self):
        case_id = self.investigate()["case_id"]
        old = self.evaluate(case_id)
        changed = copy.deepcopy(AO.definition)
        changed["version"] = 2
        for item in changed["required_computations"]:
            if item["name"] == "planned_minus_manufactured":
                item["expression"] = "planned + manufactured"
        new = self.evaluate(case_id, playbook=pe.Playbook(changed))
        result = pe.compare(old, new)
        self.assertEqual(result["newly_failing"], ["planned_arith"])
        self.assertTrue(result["regression"])
        self.assertEqual(result["from"]["version"], 1)
        self.assertEqual(result["to"]["version"], 2)
        self.assertNotEqual(old["definition_sha256"], new["definition_sha256"])

    def test_identical_reports_are_not_a_regression_and_a_fix_is_newly_passing(self):
        case_id = self.investigate()["case_id"]
        old = self.evaluate(case_id)
        self.assertFalse(pe.compare(old, old)["regression"])
        broken = copy.deepcopy(old)
        broken["checks"][0]["passed"] = False
        fixed = pe.compare(broken, old)
        self.assertEqual((fixed["newly_passing"], fixed["regression"]), ([old["checks"][0]["id"]], False))

    def test_dropping_a_passing_check_is_a_regression(self):
        case_id = self.investigate()["case_id"]
        old = self.evaluate(case_id)
        weaker = copy.deepcopy(AO.definition)
        weaker["version"] = 2
        weaker["success_checks"] = [c for c in weaker["success_checks"] if c["id"] != "facts_cite"]
        new = self.evaluate(case_id, playbook=pe.Playbook(weaker))
        result = pe.compare(old, new)
        self.assertEqual(result["removed_passing_checks"], ["facts_cite"])
        self.assertTrue(result["regression"])

    def test_reports_of_different_cases_are_refused(self):
        first = self.evaluate(self.investigate()["case_id"])
        self.setUp()
        second = self.evaluate(self.investigate()["case_id"])
        with self.assertRaises(pe.EvaluationError):
            pe.compare(first, second)


class CustomerPoEvalBase:
    """Shared fixture for the customer-PO evaluator tests; deliberately has no test_ methods,
    so subclasses do not re-run another class's tests."""

    def setUp(self) -> None:
        self.h = cpo_tests.CoverageTestCase("setUp")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.server = self.h.server(cpo_tests.OPS_FINANCE, self.h.runtime())
        self.call = lambda name, arguments: self.h.ok(self.server, name, arguments)
        self.actual = Recorder(self.call)  # what build() really calls

    def coverage(self, **filters):
        """Orders without a customer PO, read for the session actor the way the
        ``case.customer_po_coverage`` check reads them (``customer_po_coverage.coverage``)."""
        return cpo.coverage(self.server.case_runtime.mirror, self.server.actor, **filters)

    def record_po_gaps(self, case_id, orders, call=None):
        """The gaps an investigator records with plx.case_record_gap for orders whose every
        exposed PO field is empty: one per order citing it, one not-ingested gap citing all."""
        call = call or self.actual
        for order in orders:
            call("plx.case_record_gap", {
                "case_id": case_id, "state": "absent_in_source",
                "missing": f"Customer PO of sales order {order['order_number']}",
                "why": "Every exposed PO field of the cited order is empty; the PO may be "
                       "held elsewhere.",
                "who_can_answer": ["PLX order entry"], "citations": [order["citation"]],
                "property": "SalesOrder.customer_po"})
        call("plx.case_record_gap", {
            "case_id": case_id, "state": "not_ingested",
            "missing": "Customer PO documents for sales orders "
                       + ", ".join(order["order_number"] for order in orders),
            "why": "Mail, SharePoint and BC are not ingested; Portal attachments are unknown.",
            "who_can_answer": ["PLX order entry"],
            "citations": [order["citation"] for order in orders]})

    def build(self, propose=True, gaps=True):
        [order] = self.coverage(origins=["sage_import"],
                                statuses=["absent_in_this_source"])["orders"]
        evidence = self.actual("mirror.rows", {
            "relation": "sage.oe_order_header", "where": [["ordnumber", "eq", "S3"]]})
        case_id = self.actual("plx.case_open", {
            "question": "Missing customer POs", "roots": [order["order_number"]]})["case_id"]
        if propose:
            self.actual("plx.case_propose", {
                "case_id": case_id, "capability": "portal.sales_order.set_customer_po",
                "payload": {"proposed_customer_po": "PO-7788",
                            "evidence_citation": evidence["rows"][0]["citation"],
                            "evidence_field": "ponumber"},
                "preconditions": ["a person at PLX confirms the evidence belongs to this order"],
                "citations": [evidence["rows"][0]["citation"], order["citation"]],
                # The Portal write sets po_reference (one value per field).
                "expected_effect": {"relation": cpo.RELATION, "key": order["key"],
                                    "values": {"po_reference": "PO-7788"}},
                "evidence": {"statement": "The cited Sage order header holds PO-7788 in "
                                          "ponumber; it is proposed as the customer PO of "
                                          f"sales order {order['order_number']}."}})
        if gaps:
            self.record_po_gaps(case_id, [order])
        return case_id

    def failed(self, report):
        return [item["id"] for item in report["checks"] if not item["passed"]]


class CustomerPoEvalTests(CustomerPoEvalBase, unittest.TestCase):
    def test_evidence_accepted_only_by_an_any_role_is_never_clean(self):
        """Review 424873f: a foreign Sage header passed clean, labelled 'bound'."""
        case_id = self.build()
        other = self.call("mirror.rows", {"relation": "sage.oe_order_header",
                                              "where": [["ordnumber", "eq", "S4"]]})
        self.call("plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": "Sage order S4 is on file.",
            "citations": [other["rows"][0]["citation"]]})
        report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
        unchecked = report["summary"]["evidence_rows_identity_unchecked"]
        self.assertIn(other["rows"][0]["citation"], unchecked)
        self.assertIn("identity_not_checked", report["summary"]["limits"])
        self.assertFalse(report["summary"]["passed_all_clean"])
        entry = next(e for e in report["evidence_manifest"]["entries"]
                     if e["handle"] == other["rows"][0]["citation"])
        self.assertEqual(entry["identity"], {"po_evidence_row": "any"})

    def test_a_proposal_with_evidence_and_gaps_passes(self):
        case_id = self.build()
        # The transcript is the tool calls build() actually made, not a hand-written list.
        self.assertIn("mirror.rows", self.actual.names)  # a direct read, not a host tool
        self.assertLessEqual(set(self.actual.names) - source_reads.READS,
                             set(CPO.definition["allowed_tools"]))
        report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
        self.assertEqual(self.failed(report), [], report["checks"])
        self.assertTrue(report["summary"]["passed_all"])
        gaps = {row["id"]: row for row in report["expected_gaps"]}
        self.assertEqual({key: (row["condition_held"], row["recorded"])
                          for key, row in gaps.items()},
                         {"po_absent_in_source": (True, True),
                          "po_text_not_po": (False, False),
                          "po_documents_not_ingested": (True, True)})
        support = next(c for c in report["checks"] if c["id"] == "proposals_supported")
        self.assertEqual(support["evidence"], ["F1", "P1"])
        self.assertEqual(report["definition_sha256"], CPO.definition_sha256)
        self.assertEqual(pe.evaluate(CPO, self.call, case_id)["report_sha256"],
                         pe.evaluate(CPO, self.call, case_id)["report_sha256"])

    def around_the_tool_gap(self, case_id, order, missing, prop=None):
        """An absence recorded straight into the case store (as a stale writer or an old
        record would), so only the evaluator stands between it and a pass."""
        runtime = self.server.case_runtime
        return runtime.cases.gap(self.server.actor, case_id, missing,
                                 "The PO field is empty.", ["PLX order entry"],
                                 "absent_in_source", [order["citation"]],
                                 about_property=prop)

    def order(self, test):
        return next(item for item in self.call("mirror.rows", {
            "relation": cpo.RELATION, "limit": 50})["rows"] if test(item["row"]))

    def test_a_false_absence_never_passes_v2_whatever_its_wording(self):
        """Reviews wh-2 and wh-3 (material): absent_in_source on an order holding PO-5501
        (po_reference) or PO12172025 (custom_fields.custPoCode only) passed v2 clean when
        worded off the expected term. The evaluator now checks the structured claim on
        the re-read row: unstructured or contradicted, it is a violation."""
        holding = self.order(lambda row: row.get("po_reference") == "PO-5501")
        fm_held = self.order(lambda row: row.get("po_reference") is None
                             and "PO12172025" in str(row.get("custom_fields")))
        for order, prop, kind in (
                (holding, None, "absence_gap_unstructured"),
                (holding, "SalesOrder.customer_po", "absence_gap_contradicted"),
                (fm_held, None, "absence_gap_unstructured"),
                (fm_held, "SalesOrder.customer_po", "absence_gap_contradicted")):
            with self.subTest(order=order["row"]["order_number"], prop=prop):
                number = order["row"]["order_number"]
                case_id = self.actual("plx.case_open", {"question": "Missing customer POs",
                                                        "roots": [number]})["case_id"]
                self.around_the_tool_gap(case_id, order, f"No PO on file for {number}", prop)
                report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
                self.assertIn(kind, [v["kind"] for v in report["authority_violation_details"]])
                self.assertFalse(report["summary"]["passed_all"])
                self.assertFalse(report["summary"]["passed_all_clean"])

    def test_a_true_absence_is_attributed_by_structure_not_wording(self):
        empty = self.order(lambda row: "po_reference" in row and row["po_reference"] is None
                           and row.get("custom_fields") is None)
        number = empty["row"]["order_number"]
        case_id = self.actual("plx.case_open", {"question": "Missing customer POs",
                                                "roots": [number]})["case_id"]
        self.actual("plx.case_record_gap", {
            "case_id": case_id, "state": "absent_in_source",
            "missing": f"Nothing in the PO fields of {number}", "why": "Both are empty.",
            "who_can_answer": ["PLX order entry"], "citations": [empty["citation"]],
            "property": "SalesOrder.customer_po"})
        report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
        gaps = {row["id"]: row for row in report["expected_gaps"]}
        self.assertEqual((gaps["po_absent_in_source"]["condition_held"],
                          gaps["po_absent_in_source"]["status"]), (True, "ok"))
        self.assertNotIn("absence_gap_unattributed", report["summary"]["limits"])
        self.assertFalse([v for v in report["authority_violation_details"]
                          if v["kind"].startswith("absence_gap")])

    def test_a_po_field_holding_the_orders_own_number_passes_v2_as_absent(self):
        """Review wh-4 (material): an order whose PO fields hold only its own number
        (po_reference, or custPoCode equal to its fmOeNumber) could pass v2 neither way.
        Condition, gap guard and evaluator now read it as one placeholder: absent."""
        self.h.fake.tables[cpo.RELATION] += [
            cpo_tests._order(41, po_reference="OR0041"),
            cpo_tests._order(43, number="OR0043",
                             custom_fields={"fmOeNumber": "OR0042", "custPoCode": "OR0042"})]
        for number in ("OR0041", "OR0043"):
            with self.subTest(order=number):
                order = self.order(lambda row: row["order_number"] == number)
                case_id = self.actual("plx.case_open", {"question": "Missing customer POs",
                                                        "roots": [number]})["case_id"]
                recorded = self.actual("plx.case_record_gap", {
                    "case_id": case_id, "state": "absent_in_source",
                    "missing": f"Customer PO of sales order {number}",
                    "why": "The PO field holds the order's own number.",
                    "who_can_answer": ["PLX order entry"], "citations": [order["citation"]],
                    "property": "SalesOrder.customer_po"})
                self.assertTrue(recorded["gap_guard"]["checked"][0]["self_references"])
                report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
                gaps = {row["id"]: row for row in report["expected_gaps"]}
                self.assertEqual((gaps["po_absent_in_source"]["condition_held"],
                                  gaps["po_absent_in_source"]["status"]), (True, "ok"))
                self.assertFalse(gaps["po_text_not_po"]["condition_held"])
                self.assertFalse([v for v in report["authority_violation_details"]
                                  if v["kind"].startswith("absence_gap")])

    def test_missing_gap_records_fail_the_gap_checks(self):
        report = pe.evaluate(CPO, self.call, self.build(gaps=False))
        self.assertEqual(self.failed(report), ["absent_gap", "not_ingested_gap"])

    def test_the_playbook_does_not_allow_a_dispatch_tool_so_one_is_a_violation(self):
        report = pe.evaluate(CPO, self.call, self.build(),
                             transcript=["plx.case_get", "portal.sales_order.set_customer_po"])
        self.assertEqual(report["authority_violations"], 1)


def blind(call, *relations):
    """A reader that cannot re-read rows of the given relations (a withheld/failed citation)."""
    def wrapped(name, arguments):
        if name == "plx.source_citation" and any(
                f"api://{relation}/" in arguments["citation"] for relation in relations):
            return {"ok": False, "error": "withheld"}
        return call(name, arguments)
    return wrapped


class FailClosedMatrix(AoEvalCase):
    """One adversarial case per check type: each check passes only on positive, complete
    evidence, so every case below must fail with a reason (and the clean case passes)."""

    def compute(self, damage_rows):
        def extra(call, out):
            handles = out["cite"]["damage"]
            chosen = [handles[index] for index in damage_rows]
            call("plx.case_compute", {
                "case_id": out["case_id"], "unit": "pack",
                "expression": " + ".join(f"d{i}" for i in range(len(chosen))),
                "inputs": {f"d{i}": {"citation": h, "field": "row.qty_damaged", "unit": "pack"}
                           for i, h in enumerate(chosen)}})
        return extra

    def only_bad_sum(self, damage_rows):
        """The investigation with its correct damage total swallowed and a wrong one added."""
        out = self.investigate(
            drop=lambda name, args: name == "plx.case_compute" and args["unit"] == "pack",
            extra=self.compute(damage_rows))
        return out["case_id"]

    def test_arithmetic_binds_each_cited_row_exactly_once(self):
        for label, rows in (("same row twice (40, true 30)", [0, 0]),
                            ("one of three rows (20)", [0]),
                            ("two of three rows", [0, 1]),
                            ("a row repeated beside the others", [0, 0, 1, 2])):
            for mode in ("bad sum alone", "bad sum beside the good one"):
                with self.subTest(f"{label}: {mode}"):
                    self.setUp()
                    case_id = (self.only_bad_sum(rows) if mode == "bad sum alone" else
                               self.investigate(extra=self.compute(rows))["case_id"])
                    report = self.evaluate(case_id)
                    self.assertEqual(self.failed(report), ["damaged_arith"])
                    self.assertFalse(report["summary"]["passed_all"])

    def test_arithmetic_is_recomputed_from_the_evaluators_own_reread(self):
        self.setUp()
        report = self.evaluate(self.investigate()["case_id"])  # clean: all three rows, once
        self.assertTrue(self.check(report, "damaged_arith")["passed"])
        self.assertIn("= 30 pack", self.check(report, "damaged_arith")["detail"])

    def test_an_unreadable_cited_row_fails_arithmetic_evidence_and_statements(self):
        case_id = self.investigate()["case_id"]
        report = pe.evaluate(AO, blind(self.call, "silver.assembly_order_damage"), case_id)
        self.assertLessEqual({"damage_cited", "damaged_arith"}, set(self.failed(report)))
        self.assertIn("cannot re-read", self.check(report, "damage_cited")["detail"])
        blind_items = pe.evaluate(AO, blind(self.call, "silver.assembly_order_items"), case_id)
        self.assertIn("item_cited", self.failed(blind_items))
        self.assertIn("planned_arith", self.failed(blind_items))

    def test_an_unknown_condition_fails_the_gap_check_instead_of_not_applicable(self):
        case_id = self.investigate()["case_id"]
        report = pe.evaluate(AO, blind(self.call, "public.assembly_order"), case_id)
        gap = next(row for row in report["expected_gaps"] if row["id"] == "header_quantities_empty")
        self.assertEqual((gap["condition_state"], gap["condition_held"], gap["status"]),
                         ("unknown", False, "unknown"))
        header = self.check(report, "header_gap")
        self.assertFalse(header["passed"])
        self.assertTrue(header["detail"].startswith("condition_unknown"))
        self.assertFalse(report["summary"]["passed_all"])
        self.assertGreaterEqual(report["summary"]["gaps_unknown"], 1)
        # A condition that does not hold on readable rows is still "not applicable".
        self.setUp()
        self.h.fake.tables["public.order_entry"][0]["datereq"] = "2026-01-10"
        ok = self.evaluate(self.investigate()["case_id"])
        self.assertEqual(self.failed(ok), [])

    def test_an_unread_statement_citation_fails_even_without_numbers(self):
        def extra(call, out):
            call("plx.case_add_finding", {
                "case_id": out["case_id"], "kind": "fact", "status": "supported",
                "statement": "The filling row was reviewed.",
                "citations": [out["cite"]["filling"]]})
        case_id = self.investigate(extra=extra)["case_id"]
        report = pe.evaluate(AO, blind(self.call, "silver.production_tasks"), case_id)
        self.assertIn("no_uncited_numbers", self.failed(report))
        self.assertIn("facts_cite", self.failed(report))

    def test_number_tokens_of_statements_are_found(self):
        stray = {"-777": "-777", "1503/99999": "99999", "4242-pack": "4242",
                 "(1,920)": "1920", "0.5": "0.5", ".75": "0.75", "+12": "12",
                 "10-20": "20", "seventy-seven": "77", "two hundred": "200"}
        for text, expected in stray.items():
            with self.subTest(text):
                self.assertIn(expected, pe._numbers_in(f"value {text} here"))
        self.assertIn("-777", pe._numbers_in("shortfall -777"))
        self.assertNotIn("777", pe._numbers_in("shortfall -777"))
        self.assertEqual(pe._numbers_in("1,920 EA and 1.50"), {"1920", "1.5"})
        for text in ("AO-26005B", "OR1000151885", "PO12172025", "DVS-5003-3.78L-A",
                     "see api://public.app_sales_order/351 on 2026-01-05"):
            with self.subTest(text):
                self.assertEqual(pe._numbers_in(text), set())
        self.assertEqual(pe._numbers_in("AO-77"), {"77"})  # not an identifier: a number
        self.assertEqual(pe._numbers_in("a one row chain"), set())  # small words are prose

    def test_uncited_number_spellings_in_a_finding_fail_the_check(self):
        for statement, number in (("Shortfall of -777 EA on the filling row.", "-777"),
                                  ("Ratio 1503/99999 on the filling row.", "99999"),
                                  ("A 4242-pack lot on the filling row.", "4242"),
                                  ("Seventy-seven packs on the filling row.", "77")):
            with self.subTest(statement):
                self.setUp()

                def extra(call, out, text=statement):
                    call("plx.case_add_finding", {
                        "case_id": out["case_id"], "kind": "fact", "status": "supported",
                        "statement": text, "citations": [out["cite"]["filling"]]})
                report = self.evaluate(self.investigate(extra=extra)["case_id"])
                self.assertEqual(self.failed(report), ["no_uncited_numbers"])
                self.assertIn(number, self.check(report, "no_uncited_numbers")["detail"])

    def gap(self, **kwargs):
        def extra(call, out):
            citations = [out["cite"][role] for role in kwargs.pop("roles", ["ao"])]
            call("plx.case_record_gap", {
                "case_id": out["case_id"], "state": kwargs["state"], "missing": kwargs["missing"],
                "why": kwargs["why"], "who_can_answer": ["x"], "citations": citations})
        return extra

    def test_an_unrelated_gap_of_the_same_kind_does_not_satisfy_an_expected_gap(self):
        unrelated = dict(state="not_ingested", missing="Supplier freight invoices",
                         why="Mail is not ingested.")
        out = self.investigate(
            drop=lambda name, args: name == "plx.case_record_gap"
            and args["state"] == "not_ingested", extra=self.gap(**unrelated))
        report = self.evaluate(out["case_id"])
        self.assertEqual(self.failed(report), ["scan_gap"])
        self.assertIn("no matching", self.check(report, "scan_gap")["detail"])
        # Right text but citing nothing of the required role is not a match either.
        self.setUp()
        out = self.investigate(
            drop=lambda name, args: name == "plx.case_record_gap"
            and args["state"] == "not_ingested",
            extra=self.gap(state="not_ingested", missing="The original AO-26005B production scan",
                           why="Paper.", roles=["order"]))
        self.assertEqual(self.failed(self.evaluate(out["case_id"])), ["scan_gap"])

    def test_a_gap_satisfying_two_expected_gaps_is_ambiguous_and_matches_none(self):
        both = dict(state="unknown", roles=["ao", "link", "order", "filling"],
                    missing="Which sales order AO-26005B was produced for, by the required date",
                    why="Also why it was produced after the required date.")
        out = self.investigate(
            drop=lambda name, args: name == "plx.case_record_gap"
            and args["missing"].startswith("Why AO-26005B"), extra=self.gap(**both))
        report = self.evaluate(out["case_id"])
        gaps = {row["id"]: row for row in report["expected_gaps"]}
        self.assertEqual(gaps["late_reason_unknown"]["recorded_gap_ids"], [])
        self.assertEqual(gaps["late_reason_unknown"]["status"], "missing")
        self.assertEqual(report["ambiguous_gap_ids"], ["G3"])
        self.assertFalse(report["summary"]["passed_all"])

    def test_a_withheld_calculation_never_passes(self):
        case_id = self.investigate()["case_id"]

        def hide(name, arguments):
            result = self.call(name, arguments)
            if name == "plx.case_get" and _first_calculations(result) is not None:
                result = copy.deepcopy(result)
                calculations = _first_calculations(result)
                calculations[0] = {"id": calculations[0]["id"], "content": "withheld"}
            return result
        report = pe.evaluate(AO, hide, case_id)
        self.assertIn("planned_arith", self.failed(report))


class CustomerPoFailClosed(CustomerPoEvalBase, unittest.TestCase):
    def test_an_unreadable_order_row_fails_the_gap_checks_as_condition_unknown(self):
        case_id = self.build()
        report = pe.evaluate(CPO, blind(self.call, "public.app_sales_order"), case_id)
        for check_id in ("absent_gap", "not_ingested_gap"):
            check = next(c for c in report["checks"] if c["id"] == check_id)
            self.assertFalse(check["passed"], check_id)
            self.assertTrue(check["detail"].startswith("condition_unknown"), check["detail"])
        self.assertIn("order_cited", self.failed(report))

    def test_an_unrelated_not_ingested_gap_does_not_satisfy_the_documents_gap(self):
        case_id = self.build(gaps=False)
        order = self.coverage(origins=["sage_import"],
                              statuses=["absent_in_this_source"])["orders"][0]
        self.call("plx.case_record_gap", {
            "case_id": case_id, "state": "not_ingested", "missing": "Freight invoices",
            "why": "Not ingested.", "who_can_answer": ["x"], "citations": [order["citation"]]})
        report = pe.evaluate(CPO, self.call, case_id)
        self.assertEqual(self.failed(report), ["absent_gap", "not_ingested_gap"])


FOREIGN_DAMAGE = {"id": 9001, "assembly_order_id": 10719, "assembly_item_id": 1,
                  "raw_or_pack": "Pack", "damage_type": "Pack", "status": "Generated",
                  "qty_damaged": "5.0", "qty_total": "1.0", "date_entered": "2026-01-08"}


class IdentityBindingTests(AoEvalCase):
    """Finding 1: a cited row of the right relation is not evidence unless it is the case's own."""

    def foreign_damage(self):
        self.h.fake.tables["silver.assembly_order_damage"].append(dict(FOREIGN_DAMAGE))
        rows = self.call("mirror.rows", {
            "relation": "silver.assembly_order_damage",
            "where": [["assembly_order_id", "eq", 10719]]})["rows"]
        return rows[0]["citation"]

    def test_a_foreign_damage_row_beside_the_right_ones_fails_evidence_cited(self):
        self.setUp()
        foreign = self.foreign_damage()

        def extra(call, out):
            call("plx.case_add_finding", {
                "case_id": out["case_id"], "kind": "fact", "status": "supported",
                "statement": "A damage row was reviewed.", "citations": [foreign]})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertIn("damage_cited", self.failed(report))
        self.assertIn("not bound to the case subject", self.check(report, "damage_cited")["detail"])
        self.assertIn(foreign, self.check(report, "damage_cited")["detail"])
        self.assertFalse(report["summary"]["passed_all"])
        self.assertEqual(report["summary"]["unbound_evidence_rows"], 1)

    def test_a_computation_over_a_foreign_row_instead_of_one_of_the_right_rows_fails(self):
        self.setUp()
        foreign = self.foreign_damage()

        def extra(call, out):
            handles = out["cite"]["damage"][:2] + [foreign]
            call("plx.case_compute", {
                "case_id": out["case_id"], "unit": "pack", "expression": "d0 + d1 + d2",
                "inputs": {f"d{i}": {"citation": h, "field": "row.qty_damaged", "unit": "pack"}
                           for i, h in enumerate(handles)}})
        out = self.investigate(
            drop=lambda name, args: name == "plx.case_compute" and args["unit"] == "pack",
            extra=extra)
        report = self.evaluate(out["case_id"])
        self.assertIn("damaged_arith", self.failed(report))
        self.assertIn("not among the cited rows", self.check(report, "damaged_arith")["detail"])

    def test_an_unrelated_ao_order_row_fails_order_evidence(self):
        self.setUp()
        self.h.fake.tables["public.order_entry"].append(
            {**self.h.fake.tables["public.order_entry"][0], "id": 352, "erpcode": "OR1000999999"})
        case_id = self.investigate()["case_id"]
        other = self.call("mirror.rows", {
            "relation": "public.order_entry", "where": [["id", "eq", 352]]})["rows"][0]
        self.call("plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": "Another order was looked at.", "citations": [other["citation"]]})
        report = self.evaluate(case_id)
        self.assertIn("order_cited", self.failed(report))
        self.assertIn(other["citation"], self.check(report, "order_cited")["detail"])

    def test_customer_po_order_cited_fails_for_an_unrelated_order(self):
        """The advisory probe: a case rooted at one order, the cited row is another order."""
        h = CustomerPoEvalTests("test_a_proposal_with_evidence_and_gaps_passes")
        h.setUp()
        self.addCleanup(h.doCleanups)
        orders = h.coverage()["orders"]
        target = next(item for item in orders if item["order_number"] == "OR0003")
        other = next(item for item in orders if item["order_number"] == "OR0005")
        rooted = h.actual("plx.case_open", {"question": "Missing customer POs",
                                            "roots": ["OR0003"]})["case_id"]
        h.record_po_gaps(rooted, [other])
        wrong = pe.evaluate(CPO, h.call, rooted)
        check = next(c for c in wrong["checks"] if c["id"] == "order_cited")
        self.assertFalse(check["passed"], check)
        self.assertIn("not bound to the case subject", check["detail"])
        right = h.actual("plx.case_open", {"question": "Missing customer POs",
                                           "roots": ["OR0003"]})["case_id"]
        h.record_po_gaps(right, [target])
        check = next(c for c in pe.evaluate(CPO, h.call, right)["checks"]
                     if c["id"] == "order_cited")
        self.assertTrue(check["passed"], check)
        # Both cited: the extra unrelated order is still refused, not averaged away.
        both = h.actual("plx.case_open", {"question": "Missing customer POs",
                                          "roots": ["OR0003"]})["case_id"]
        h.record_po_gaps(both, [target, other])
        check = next(c for c in pe.evaluate(CPO, h.call, both)["checks"]
                     if c["id"] == "order_cited")
        self.assertFalse(check["passed"], check)
        # A case with no roots has no subject to bind to and fails closed.
        bare = h.actual("plx.case_open", {"question": "Missing customer POs"})["case_id"]
        h.record_po_gaps(bare, [target])
        check = next(c for c in pe.evaluate(CPO, h.call, bare)["checks"]
                     if c["id"] == "order_cited")
        self.assertFalse(check["passed"])
        self.assertIn("no roots", check["detail"])


class NonExactCalculationTests(AoEvalCase):
    """Round-2 finding: a calculation with a literal or foreign input is never skipped."""

    def damage_calc(self, expression, extra_inputs):
        def extra(call, out):
            handles = out["cite"]["damage"]
            inputs = {f"d{i}": {"citation": h, "field": "row.qty_damaged", "unit": "pack"}
                      for i, h in enumerate(handles)}
            inputs.update(extra_inputs(out))
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "pack",
                                      "expression": expression, "inputs": inputs})
        return extra

    def test_a_literal_fudge_beside_the_good_calculation_fails(self):
        # A role-less literal with a unit is a quantity, not a scale, so the compute tool
        # refuses it beside roled inputs; an adversarial caller labels it with their role.
        refused = self.h.call(self.server, "plx.case_compute", {
            "case_id": self.investigate()["case_id"], "unit": "pack", "expression": "d + k",
            "inputs": {"d": {"literal": "1", "unit": "pack", "role": "damaged"},
                       "k": {"literal": "5", "unit": "pack"}}})
        self.assertIn("quantity_role_mismatch:damaged+unroled", refused["content"][0]["text"])
        self.setUp()
        extra = self.damage_calc("d0 + d1 + d2 + k", lambda out: {"k": {
            "literal": "5", "unit": "pack", "role": "damaged"}})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), ["damaged_arith"])
        self.assertIn("not an exact binding", self.check(report, "damaged_arith")["detail"])
        self.assertFalse(report["summary"]["passed_all"])

    def test_a_foreign_cited_input_beside_the_good_calculation_fails(self):
        # The Filling task's qty is now pinned "filled" by a conditional source contract,
        # so the compute tool refuses it mislabelled as damaged...
        out = self.investigate()
        refused = self.h.call(self.server, "plx.case_compute", {
            "case_id": out["case_id"], "unit": "pack", "expression": "f",
            "inputs": {"f": {"citation": out["cite"]["filling"], "field": "row.qty",
                             "unit": "pack", "role": "damaged"}}})
        self.assertIn("role_contradicts_source_contract", refused["content"][0]["text"])
        self.setUp()
        # ...and the foreign row is mislabelled through an uncontracted column of it
        # (hours_total); the evaluator still catches the foreign binding. (Before the
        # contract this used the qty column, which also failed filled_arith.)
        extra = self.damage_calc("d0 + d1 + d2 + f", lambda out: {"f": {
            "citation": out["cite"]["filling"], "field": "row.hours_total", "unit": "pack",
            "role": "damaged"}})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), ["damaged_arith"])
        self.assertIn("not an exact binding", self.check(report, "damaged_arith")["detail"])

    def test_round3_partial_literal_beside_the_good_calculation_fails(self):
        """Round-3 finding: planned (cited) - manufactured (literal 1510) beside the correct
        planned - manufactured was skipped as 'another computation'."""
        def extra(call, out):
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "EA",
                                      "expression": "planned - manufactured", "inputs": {
                                          "planned": {"citation": out["cite"]["item"],
                                                      "field": "row.quantity", "unit": "EA"},
                                          # Labelled planned: an unroled literal
                                          # quantity is refused by the compute tool.
                                          "manufactured": {"literal": "1510", "unit": "EA",
                                                           "role": "planned"}}})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertIn("planned_arith", self.failed(report))
        self.assertIn("not an exact binding", self.check(report, "planned_arith")["detail"])
        self.assertFalse(report["summary"]["passed_all"])

    def test_round4_all_literal_calculation_beside_the_good_one_fails(self):
        def extra(call, out):
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "EA",
                                      "expression": "planned - manufactured", "inputs": {
                                          "planned": {"literal": "1509", "unit": "EA"},
                                          "manufactured": {"literal": "1510", "unit": "EA"}}})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertIn("planned_arith", self.failed(report))
        self.assertFalse(report["summary"]["passed_all"])

    def test_round5_undeclared_cited_field_plus_literal_in_a_declared_unit_fails(self):
        def extra_ea(call, out):
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "EA",
                                      "expression": "planned - manufactured", "inputs": {
                                          "planned": {"citation": out["cite"]["order"],
                                                      "field": "row.id", "unit": "EA"},
                                          "manufactured": {"literal": "1510", "unit": "EA"}}})
        report = self.evaluate(self.investigate(extra=extra_ea)["case_id"])
        self.assertIn("planned_arith", self.failed(report))
        self.assertFalse(report["summary"]["passed_all"])

        def extra_pack(call, out):
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "pack",
                                      "expression": "k + a", "inputs": {
                                          "k": {"literal": "5", "unit": "pack"},
                                          "a": {"citation": out["cite"]["ao"], "field": "row.id",
                                                "unit": "pack"}}})
        report = self.evaluate(self.investigate(extra=extra_pack)["case_id"])
        self.assertEqual(self.failed(report), ["damaged_arith"])

    def test_round3_foreign_row_in_place_of_an_input_fails(self):
        def extra(call, out):
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "EA",
                                      "expression": "planned - manufactured", "inputs": {
                                          "planned": {"citation": out["cite"]["item"],
                                                      "field": "row.quantity", "unit": "EA"},
                                          # mislabelled with the actual role: compute
                                          # accepts the declaration on an uncontracted
                                          # column (the Filling task's qty is now pinned
                                          # "filled"), the evaluator must not
                                          "manufactured": {"citation": out["cite"]["filling"],
                                                           "field": "row.hours_total",
                                                           "unit": "EA", "role": "actual"}},
                                      "relations": [{"left": "planned", "op": "-", "right": "actual", "as": "planned_not_produced"}]})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertIn("planned_arith", self.failed(report))
        self.assertFalse(report["summary"]["passed_all"])

    def test_the_clean_case_still_passes_and_a_calculation_of_another_computation_is_skipped(self):
        report = self.evaluate(self.investigate()["case_id"])
        self.assertEqual(self.failed(report), [])


    def test_hardcoded_expression_on_exact_binding_fails(self):
        """Minor finding 1: exact inputs, but the recorded method is not the playbook's."""
        def extra(call, out):
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "EA",
                                      "expression": "planned*6/1509 + manufactured*0",
                                      "relations": [{"left": "planned", "op": "+", "right": "actual", "as": "fudged"}], "inputs": {
                                          "planned": {"citation": out["cite"]["item"],
                                                      "field": "row.quantity", "unit": "EA"},
                                          "manufactured": {"citation": out["cite"]["item"],
                                                           "field": "row.actual_amount",
                                                           "unit": "EA"}}})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertIn("planned_arith", self.failed(report))
        self.assertIn("up to renaming", self.check(report, "planned_arith")["detail"])
        self.assertFalse(report["summary"]["passed_all"])

    def test_two_fields_of_one_row_are_perturbed_independently(self):
        """Review ec3a804: values keyed by row made planned - manufactured 0 every round, so
        this hardcoded expression (exact on the real rows) passed."""
        def extra(call, out):
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "EA",
                                      "expression": "(planned - manufactured)*(1 + planned/manufactured - 1509/1503)",
                                      "relations": [{"left": "planned", "op": "-", "right": "actual", "as": "gap"},
                                                    {"left": "planned", "op": "/", "right": "actual", "as": "ratio"},
                                                    {"left": "gap", "op": "*", "right": "ratio", "as": "fudged"}],
                                      "inputs": {
                                          "planned": {"citation": out["cite"]["item"],
                                                      "field": "row.quantity", "unit": "EA"},
                                          "manufactured": {"citation": out["cite"]["item"],
                                                           "field": "row.actual_amount",
                                                           "unit": "EA"}}})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertIn("planned_arith", self.failed(report))
        self.assertFalse(report["summary"]["passed_all"])

    def test_a_reweighted_repeat_sum_fails(self):
        def extra(call, out):
            handles = out["cite"]["damage"]
            self.assertEqual(len(handles), 3)
            inputs = {f"d{i}": {"citation": h, "field": "row.qty_damaged", "unit": "pack"}
                      for i, h in enumerate(handles)}
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "pack",
                                      "expression": "2*d0 - d1 + 2*d2", "inputs": inputs})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertIn("damaged_arith", self.failed(report))

    def test_renamed_inputs_with_the_declared_expression_pass(self):
        def extra(call, out):
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "EA",
                                      "expression": "p - m",
                                      "relations": [{"left": "planned", "op": "-", "right": "actual", "as": "planned_not_produced"}], "inputs": {
                                          "p": {"citation": out["cite"]["item"],
                                                "field": "row.quantity", "unit": "EA"},
                                          "m": {"citation": out["cite"]["item"],
                                                "field": "row.actual_amount", "unit": "EA"}}})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertEqual(self.failed(report), [])

    def test_swapped_operands_fail(self):
        def extra(call, out):
            call("plx.case_compute", {"case_id": out["case_id"], "unit": "EA",
                                      "expression": "p - m",
                                      "relations": [{"left": "actual", "op": "-", "right": "planned", "as": "swapped"}], "inputs": {
                                          "m": {"citation": out["cite"]["item"],
                                                "field": "row.quantity", "unit": "EA"},
                                          "p": {"citation": out["cite"]["item"],
                                                "field": "row.actual_amount", "unit": "EA"}}})
        report = self.evaluate(self.investigate(extra=extra)["case_id"])
        self.assertIn("planned_arith", self.failed(report))


class LimitsAndUnitsTests(AoEvalCase):
    def source_rows_hook(self, change):
        """A caller that lets ``change(result, arguments)`` rewrite mirror.rows answers."""
        def hooked(name, arguments):
            result = self.call(name, arguments)
            if name == "mirror.rows" and arguments["relation"] == "silver.assembly_order_damage":
                result = copy.deepcopy(result)
                change(result, arguments)
            return result
        return hooked

    def test_repeat_arithmetic_enumerates_the_source_rows_and_carries_no_limit(self):
        case_id = self.investigate()["case_id"]
        recorder = Recorder(self.call)
        report = pe.evaluate(AO, recorder, case_id, transcript=self.transcript)
        damaged = self.check(report, "damaged_arith")
        self.assertTrue(damaged["passed"], damaged)
        self.assertNotIn("limits", damaged)
        self.assertEqual(damaged["row_completeness"], "enumerated")
        self.assertEqual(damaged["rows_enumerated"], 3)
        self.assertEqual(damaged["row_completeness_basis"], "non_atomic_mirror_replica_read")
        self.assertNotIn("limits", self.check(report, "planned_arith"))
        self.assertEqual(report["limits"], [])
        self.assertTrue(report["summary"]["passed_all"])
        self.assertTrue(report["summary"]["passed_all_clean"], report["summary"])
        self.assertIn("mirror.rows", recorder.names)
        # The enumeration is in the manifest as enumerated, never as a cited entry.
        enumerated = report["evidence_manifest"]["enumerated"]
        self.assertEqual([(e["role"], e["status"], len(e["rows"])) for e in enumerated],
                         [("damage", "enumerated", 3)])
        cited = {entry["handle"] for entry in report["evidence_manifest"]["entries"]}
        self.assertTrue(set(enumerated[0]["rows"]) & cited)  # same rows, different capture view
        self.assertTrue(pe.verify_report_hash(report))

    def test_incomplete_enumeration_keeps_the_row_completeness_limit(self):
        case_id = self.investigate()["case_id"]
        for label, change in (
                ("truncated, more rows matched than returned",
                 lambda r, a: r.update(total_matched=r["total_matched"] + 1)),
                ("next_offset that does not advance",
                 lambda r, a: r.update(next_offset=0)),
                ("tool says complete false", lambda r, a: r.update(complete=False)),
                ("stale mirror", lambda r, a: r["freshness"].update(stale=True)),
                ("refresh error", lambda r, a: r["freshness"].update(refresh_error="x")),
                ("a row dropped", lambda r, a: r.update(rows=r["rows"][:-1])),
                ("a row of another AO", lambda r, a: r["rows"][0]["row"].update(
                    assembly_order_id=-1)),
                ("no freshness", lambda r, a: r.pop("freshness"))):
            with self.subTest(label):
                report = pe.evaluate(AO, self.source_rows_hook(change), case_id,
                                     transcript=self.transcript)
                damaged = self.check(report, "damaged_arith")
                self.assertTrue(damaged["passed"], damaged)
                self.assertEqual(damaged["limits"], ["row_completeness_not_enumerated"])
                self.assertEqual(damaged["row_completeness"], "not_enumerated")
                self.assertTrue(damaged["row_completeness_reason"])
                self.assertFalse(report["summary"]["passed_all_clean"])

    def test_a_mirror_refresh_between_pages_refuses_enumerated(self):
        case_id = self.investigate()["case_id"]
        for label, change in (
                ("capture id moves on a later page",
                 lambda r, a: r["freshness"].update(latest_capture_id="moved")
                 if a["offset"] >= 1 else None),
                ("no capture marker", lambda r, a: r["freshness"].pop("latest_capture_id"))):
            with self.subTest(label):
                hooked = self.source_rows_hook(change)

                def small_pages(name, arguments, hooked=hooked):
                    if name == "mirror.rows":
                        arguments = {**arguments, "limit": 1}
                    return hooked(name, arguments)
                report = pe.evaluate(AO, small_pages, case_id, transcript=self.transcript)
                damaged = self.check(report, "damaged_arith")
                self.assertTrue(damaged["passed"], damaged)
                self.assertEqual(damaged["row_completeness"], "not_enumerated")
                self.assertEqual(damaged["limits"], ["row_completeness_not_enumerated"])
                self.assertTrue(("refreshed between pages" in damaged["row_completeness_reason"])
                                or "no capture marker" in damaged["row_completeness_reason"])
                self.assertFalse(report["summary"]["passed_all_clean"])
        # An unchanged marker across pages is still enumerated.
        report = pe.evaluate(AO, lambda n, a: self.call(
            n, {**a, "limit": 1} if n == "mirror.rows" else a), case_id,
            transcript=self.transcript)
        self.assertEqual(self.check(report, "damaged_arith")["row_completeness"], "enumerated")

    def test_a_source_row_missing_from_the_sum_fails_the_check(self):
        case_id = self.investigate()["case_id"]

        def extra_source_row(result, arguments):
            # the source gained a fourth damage row after the case cited three
            row = copy.deepcopy(result["rows"][0])
            row["row"]["id"] = 5447
            head, _key, tail = row["citation"].rpartition("/")[0], None, \
                row["citation"].rsplit("@", 1)[1]
            row["citation"] = f"{head}/5447@{tail}"
            result["rows"].append(row)
            result["total_matched"] += 1
        report = pe.evaluate(AO, self.source_rows_hook(extra_source_row), case_id,
                             transcript=self.transcript)
        damaged = self.check(report, "damaged_arith")
        self.assertFalse(damaged["passed"], damaged)
        self.assertEqual(damaged["row_completeness"], "not_proven_rows_missing")
        self.assertIn("missing from the sum", damaged["detail"])
        self.assertIn("5447", damaged["detail"])
        self.assertEqual(damaged["limits"], ["row_completeness_not_enumerated"])
        self.assertIn("damaged_arith", self.failed(report))
        self.assertFalse(report["summary"]["passed_all"])
        self.assertFalse(report["summary"]["passed_all_clean"])

    def with_fixed_damage_type(self, value):
        d = copy.deepcopy(AO.definition)
        d["version"] = 2
        for role in d["required_evidence"]:
            if role["role"] == "damage":
                role["identity"]["fixed"] = {"damage_type": value}
        return Playbook(d)

    def test_fixed_columns_match_exactly_in_binding_and_enumeration(self):
        case_id = self.investigate()["case_id"]
        exact = self.evaluate(case_id, playbook=self.with_fixed_damage_type("Pack"),
                              transcript=self.transcript)
        damaged = self.check(exact, "damaged_arith")
        self.assertTrue(damaged["passed"], damaged)
        self.assertEqual(damaged["row_completeness"], "enumerated")
        self.assertEqual(damaged["rows_enumerated"], 3)
        self.assertTrue(self.check(exact, "damage_cited")["passed"])
        # A case-variant of the stored value is a different value in both places: the cited
        # rows do not bind, and the enumeration (same rule) does not call anything enumerated.
        for variant in ("pack", "PACK", " Pack"):
            with self.subTest(variant):
                report = self.evaluate(case_id, playbook=self.with_fixed_damage_type(variant),
                                       transcript=self.transcript)
                self.assertFalse(self.check(report, "damage_cited")["passed"])
                self.assertIn("not bound to the case subject",
                              self.check(report, "damage_cited")["detail"])
                self.assertNotEqual(self.check(report, "damaged_arith").get("row_completeness"),
                                    "enumerated")
                self.assertFalse(report["summary"]["passed_all_clean"])

    def test_a_failing_enumeration_call_keeps_the_limit(self):
        case_id = self.investigate()["case_id"]

        def broken(name, arguments):
            if name == "mirror.rows" and arguments["relation"] == "silver.assembly_order_damage":
                raise RuntimeError("boom")
            return self.call(name, arguments)
        report = pe.evaluate(AO, broken, case_id)
        self.assertEqual(self.check(report, "damaged_arith")["limits"],
                         ["row_completeness_not_enumerated"])

    def test_paging_reads_to_the_end(self):
        case_id = self.investigate()["case_id"]
        seen = []

        def small_pages(name, arguments):
            if name == "mirror.rows" and arguments["relation"] == "silver.assembly_order_damage":
                arguments = {**arguments, "limit": 1}
                seen.append(arguments["offset"])
            return self.call(name, arguments)
        report = pe.evaluate(AO, small_pages, case_id, transcript=self.transcript)
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(self.check(report, "damaged_arith")["rows_enumerated"], 3)
        self.assertTrue(report["summary"]["passed_all_clean"])

    def test_the_evaluator_still_refuses_every_write_tool(self):
        guarded = pe._ReadOnly(Recorder(self.call))
        for tool in ("plx.case_add_finding", "plx.case_compute", "plx.case_record_gap",
                     "plx.case_open", "plx.case_attach_original"):
            with self.assertRaises(pe.EvaluationError):
                guarded(tool, {})
        self.assertEqual(guarded.names, [])

    def test_caller_declared_units_are_counted_as_unverified_not_failed(self):
        report = self.evaluate(self.investigate()["case_id"])
        units = report["unit_semantics"]
        self.assertTrue(units)
        self.assertEqual({item["unit_basis"] for item in units}, {"caller_declared"})
        self.assertEqual(report["summary"]["unverified_unit_inputs"], len(units))
        self.assertEqual(report["summary"]["unit_inputs"], len(units))
        self.assertEqual(self.failed(report), [])
        self.assertEqual(self.check(report, "planned_arith")["unit_basis"],
                         {"caller_declared": 2})

    def tampered(self, case_id, **changes):
        def wrapped(name, arguments):
            result = self.call(name, arguments)
            if name == "plx.case_get" and _first_calculations(result) is not None:
                result = copy.deepcopy(result)
                for spec in _first_calculations(result)[0]["inputs"].values():
                    spec.update(changes)
            return result
        return pe.evaluate(AO, wrapped, case_id)

    def test_a_missing_or_unknown_unit_basis_fails_the_arithmetic(self):
        case_id = self.investigate()["case_id"]
        report = self.tampered(case_id, unit_basis="vibes")
        self.assertEqual(self.failed(report), ["planned_arith"])
        self.assertIn("unit_basis", self.check(report, "planned_arith")["detail"])
        self.assertEqual({u["unit_basis"] for u in report["unit_semantics"]
                          if u["calculation"] == "C1"}, {"missing"})

    def test_a_claimed_source_contract_without_a_contract_fails_and_a_real_one_passes(self):
        case_id = self.investigate()["case_id"]
        report = self.tampered(case_id, unit_basis="source_contract")
        self.assertEqual(self.failed(report), ["planned_arith"])
        self.assertIn("records none", self.check(report, "planned_arith")["detail"])
        report = self.tampered(case_id, unit_basis="source_contract", unit_contract="c1",
                               unit_contract_digest="0" * 64)
        self.assertEqual(self.failed(report), [])
        self.assertEqual(report["summary"]["unverified_unit_inputs"],
                         report["summary"]["unit_inputs"] - 2)

    def test_a_source_contract_unit_that_contradicts_the_playbook_fails(self):
        case_id = self.investigate()["case_id"]
        report = self.tampered(case_id, unit_basis="source_contract", unit_contract="c1",
                               unit_contract_digest="0" * 64, unit="pack")
        self.assertEqual(self.failed(report), ["planned_arith"])
        self.assertIn("unit pack != EA", self.check(report, "planned_arith")["detail"])


class FrozenContractTests(AoEvalCase):
    """Finding 3: the report pins the case boundary, the evidence and the evaluator."""

    def test_the_report_records_boundary_manifest_evaluator_and_transcript_state(self):
        report = self.evaluate(self.investigate()["case_id"])
        boundary = report["case_boundary"]
        view = self.h.case_view(self.server, report["case_id"])
        self.assertEqual(boundary["events"], view["integrity"]["events"])
        self.assertTrue(boundary["chain_ok"])
        self.assertEqual(boundary["timeline_entries"], len(view["timeline"]))
        self.assertEqual(len(boundary["timeline_sha256"]), 64)
        self.assertEqual(boundary["last_event"]["seq"], boundary["events"])
        self.assertEqual(report["evaluator_version"], pe.EVALUATOR_VERSION)
        manifest = report["evidence_manifest"]
        self.assertEqual(len(manifest["manifest_sha256"]), 64)
        handles = [entry["handle"] for entry in manifest["entries"]]
        self.assertEqual(handles, sorted(handles))
        self.assertGreaterEqual(len(handles), 9)
        for entry in manifest["entries"]:
            self.assertEqual(entry["status"], "resolved")
            self.assertEqual(entry["reread"], "ok")
            self.assertTrue(entry["recorded_digest"])
            self.assertTrue(entry["cited_by"])
            self.assertEqual(set(entry["identity"].values()), {"bound"})
        self.assertTrue(pe.verify_report_hash(report))

    def test_a_changed_case_changes_the_boundary_and_manifest(self):
        case_id = self.investigate()["case_id"]
        first = self.evaluate(case_id)
        again = self.evaluate(case_id)
        self.assertEqual(first["case_boundary"], again["case_boundary"])
        self.call("plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": "The filling row was reviewed.",
            "citations": [self.h.case_view(self.server, case_id)["findings"]["supported"][0][
                "citations"][0]["handle"]]})
        later = self.evaluate(case_id)
        self.assertEqual(later["case_boundary"]["events"], first["case_boundary"]["events"] + 1)
        self.assertNotEqual(later["case_boundary"]["timeline_sha256"],
                            first["case_boundary"]["timeline_sha256"])

    def test_compare_refuses_reports_of_a_case_that_changed_and_says_what(self):
        case_id = self.investigate()["case_id"]
        old = self.evaluate(case_id)
        item = self.h.case_view(self.server, case_id)["calculations"][0]["inputs"]
        handle = next(iter(item.values()))["citation"]["handle"]
        self.call("plx.case_record_gap", {
            "case_id": case_id, "state": "unknown", "missing": "Something else",
            "why": "Not read.", "who_can_answer": ["x"], "citations": [handle]})
        new = self.evaluate(case_id)
        with self.assertRaises(pe.EvaluationError) as raised:
            pe.compare(old, new)
        message = str(raised.exception)
        self.assertIn("case_changed_between_reports", message)
        self.assertIn("events", message)
        self.assertIn("timeline_sha256", message)
        allowed = pe.compare(old, new, allow_changed_case=True)
        self.assertFalse(allowed["same_case_content"])
        self.assertTrue(any("events" in change for change in allowed["case_changes"]))
        self.assertFalse(pe.compare(old, old)["case_changes"])
        self.assertTrue(pe.compare(old, old)["same_case_content"])

    def test_compare_refuses_a_changed_evidence_manifest_even_at_the_same_boundary(self):
        old = self.evaluate(self.investigate()["case_id"])
        new = copy.deepcopy(old)
        new["evidence_manifest"]["entries"][0]["recorded_digest"] = "f" * 64
        with self.assertRaises(pe.EvaluationError) as raised:
            pe.compare(old, new)
        self.assertIn("evidence changed", str(raised.exception))
        self.assertIn(old["evidence_manifest"]["entries"][0]["handle"], str(raised.exception))
        self.assertTrue(pe.compare(old, new, allow_changed_case=True)["case_changes"])

    def test_compare_refuses_a_report_without_a_frozen_boundary_and_other_evaluators(self):
        old = self.evaluate(self.investigate()["case_id"])
        legacy = {key: value for key, value in old.items() if key != "case_boundary"}
        with self.assertRaises(pe.EvaluationError):
            pe.compare(old, legacy)
        other = {**old, "evaluator_version": "older"}
        with self.assertRaises(pe.EvaluationError) as raised:
            pe.compare(old, other)
        self.assertIn("different_evaluator_versions", str(raised.exception))
        self.assertEqual(pe.compare(old, other, allow_changed_evaluator=True)
                         ["evaluator_versions"], {"old": pe.EVALUATOR_VERSION, "new": "older"})

    def test_no_transcript_means_authority_not_checked_and_a_qualified_pass(self):
        case_id = self.investigate()["case_id"]
        report = self.evaluate(case_id)
        self.assertEqual(report["transcript"], "none")
        self.assertFalse(report["summary"]["authority_checked"])
        self.assertEqual(report["authority_violations"], 0)  # not checked, which is not clean
        self.assertTrue(report["summary"]["passed_all"])
        self.assertEqual(report["summary"]["passed_all_qualification"], "authority_not_checked")
        self.assertFalse(report["summary"]["passed_all_clean"])
        checked = self.evaluate(case_id, transcript=self.transcript)
        self.assertEqual(checked["transcript"], "supplied")
        self.assertTrue(checked["summary"]["authority_checked"])
        self.assertIsNone(checked["summary"]["passed_all_qualification"])
        self.assertNotEqual(checked["report_sha256"], report["report_sha256"])

    def test_the_evaluator_refuses_a_definition_changed_after_load(self):
        case_id = self.investigate()["case_id"]
        playbook = pe.Playbook(copy.deepcopy(AO.definition))
        dict.__setitem__(playbook._definition["success_checks"][0], "type", "evidence_cited")
        with self.assertRaises(PlaybookIntegrityError):
            pe.evaluate(playbook, self.call, case_id)


class PerSubjectGapTests(CustomerPoEvalBase, unittest.TestCase):
    """Review finding 1: with two bound orders each expected gap is owed once per order."""

    def two_orders(self):
        coverage = self.coverage(origins=["portal_entered"],
                                 statuses=["absent_in_this_source"])
        return coverage["orders"][:2]

    def build_two(self, record):
        """``record``: the orders (indexes) each gap is recorded for, via plx.case_record_gap."""
        orders = self.two_orders()
        self.assertEqual(len(orders), 2)
        case_id = self.call("plx.case_open", {
            "question": "Two orders without POs",
            "roots": [order["order_number"] for order in orders]})["case_id"]
        for index in record:
            self.record_po_gaps(case_id, [orders[index]], call=self.call)
        # Both orders are cited as evidence regardless of which gaps were recorded.
        for order in orders:
            self.call("plx.case_add_finding", {
                "case_id": case_id, "statement": "Order row read.", "kind": "fact",
                "status": "supported", "citations": [order["citation"]]})
        return case_id, orders

    def rows(self, report):
        return {row["id"]: row for row in report["expected_gaps"]}

    def test_gaps_for_both_orders_pass_with_per_subject_rows(self):
        case_id, orders = self.build_two([0, 1])
        report = pe.evaluate(CPO, self.call, case_id)
        self.assertEqual(self.failed(report), [], report["checks"])
        row = self.rows(report)["po_absent_in_source"]
        self.assertEqual(sorted(item["subject"] for item in row["subjects"]),
                         sorted(order["citation"] for order in orders))
        self.assertTrue(all(item["condition_held"] and item["recorded"]
                            for item in row["subjects"]))
        self.assertEqual(row["status"], "ok")
        self.assertEqual(report["summary"]["gaps_missing"], 0)

    def test_omitting_either_orders_gap_fails_naming_that_order(self):
        for kept in (0, 1):
            case_id, orders = self.build_two([kept])
            report = pe.evaluate(CPO, self.call, case_id)
            for check_id in ("absent_gap", "not_ingested_gap"):
                check = next(c for c in report["checks"] if c["id"] == check_id)
                self.assertFalse(check["passed"], (kept, check_id))
                self.assertIn(orders[1 - kept]["citation"], check["detail"])
                self.assertNotIn(orders[kept]["citation"], check["detail"])
            self.assertFalse(report["summary"]["passed_all"])
            self.assertEqual(self.rows(report)["po_absent_in_source"]["status"], "missing")

    def test_a_single_order_case_keeps_one_subject(self):
        report = pe.evaluate(CPO, self.call, self.build())
        for row in report["expected_gaps"]:
            self.assertEqual(len(row["subjects"]), 1, row["id"])
        self.assertEqual(self.failed(report), [])


class RereadPayloadPinTests(CustomerPoEvalBase, unittest.TestCase):
    """Review finding 2: the evaluator scores only the payload the case recorded."""

    @staticmethod
    def drifting(call, field, value):
        """A source whose stored row differs at evaluation time from citation time."""
        def wrapped(name, arguments):
            result = call(name, arguments)
            if name == "plx.source_citation" and result.get("ok") \
                    and result.get("relation") == "public.app_sales_order":
                result = {**result, "row": {**result["row"], field: value}}
            return result
        return wrapped

    def test_manifest_pins_the_reread_payload_to_the_recorded_digest(self):
        report = pe.evaluate(CPO, self.call, self.build())
        entries = report["evidence_manifest"]["entries"]
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(entry["reread"], "ok", entry)
            if entry["handle"].startswith("api://"):
                self.assertEqual(entry["reread_payload_sha256"], entry["recorded_digest"])
        self.assertEqual(report["summary"]["changed_rereads"], [])

    def test_a_source_change_between_recording_and_evaluation_invalidates_it(self):
        case_id = self.build()
        clean = pe.evaluate(CPO, self.call, case_id)
        self.assertTrue(clean["summary"]["passed_all"])
        # Same empty-PO conclusion, but the row is no longer what the case cited.
        drift = pe.evaluate(CPO, self.drifting(self.call, "total_amount", "99.00"), case_id)
        changed = [e for e in drift["evidence_manifest"]["entries"]
                   if e["handle"].startswith("api://public.app_sales_order")]
        self.assertTrue(changed)
        self.assertTrue(all(e["reread"] == "changed" for e in changed))
        self.assertNotEqual(changed[0]["reread_payload_sha256"], changed[0]["recorded_digest"])
        self.assertFalse(drift["summary"]["passed_all"])
        self.assertEqual(drift["summary"]["changed_rereads"],
                         sorted(e["handle"] for e in drift["evidence_manifest"]["entries"]
                                if e["reread"] == "changed"))
        for check_id in ("order_cited", "absent_gap", "not_ingested_gap"):
            self.assertIn(check_id, self.failed(drift))

    def test_compare_treats_a_payload_digest_difference_as_a_case_change(self):
        case_id = self.build()
        clean = pe.evaluate(CPO, self.call, case_id)
        drift = pe.evaluate(CPO, self.drifting(self.call, "total_amount", "99.00"), case_id)
        self.assertTrue(pe.case_changes(clean, drift))
        with self.assertRaises(pe.EvaluationError):
            pe.compare(clean, drift)
        self.assertFalse(pe.compare(clean, drift, allow_changed_case=True)["same_case_content"])

class OriginalPageClaimEvaluationTests(unittest.TestCase):
    """A claim on an original page (field "text") is scored on the page the evaluator re-read
    and digest-checked, not marked claim_uncited (review of 927033f, AO_V2 with an original)."""

    def setUp(self) -> None:
        from operating.tests import test_ao_originals as orig_tests
        harness = orig_tests.AoOriginalsInvestigationTest("setUp")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        server = harness.server(orig_tests.OPS_QUALITY, harness.runtime())
        self.call = harness.caller(server)
        self.case_id = orig_tests.investigate(
            self.call, orig_tests.PAPER, harness.originals())["case_id"]
        self.playbook = AO_V2

    def test_a_page_claim_is_not_uncited(self):
        report = pe.evaluate(self.playbook, self.call, self.case_id)
        self.assertEqual(report["claim_checks"]["failures"], [])
        self.assertEqual(report["summary"]["claim_failure_codes"], [])
        passed = [claim for item in report["claim_checks"]["items"]
                  for claim in item["claims"] if claim.get("text_source")]
        self.assertTrue(passed)

    def test_an_agent_only_reader_is_not_given_the_page(self):
        report = pe.evaluate(self.playbook, self.call, self.case_id,
                             reader_principals=["agent:x"])
        self.assertIn("claim_uncited", report["summary"]["claim_failure_codes"])


if __name__ == "__main__":
    unittest.main()
