"""The claim gate sees a mirror (``api://``) revision the source moved past.

Evidence label: ``injected-transport``. The real ``WorkbenchReadApiClient`` reads the
in-process ``FakeWorkbench``; the agent loop's planner is scripted and plx.investigate's
primitives are canned (only the cited row is read, through the case runtime). No
network, no credentials, no live data.

A value cited on a mirror row, then changed upstream and refreshed: the agent loop and
plx.investigate (now) report ``claim_changed_since`` and never ``check: passed``; an
``as_known_at`` before the supersession still passes; the digest a case records
(``CaseRuntime.resolve_citation``) does not move.
"""

from __future__ import annotations

from pathlib import Path
import re

from ontology.core import Actor
from operating import agent_loop, case_verification
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating.cases import _digest
from operating.mcp_server import MCPServer
from operating.ontology_host import CaseHostTools
from operating.tests.test_mcp_case_tools import OPS, OUTSIDER, CaseToolsTestCase

RELATION = "public.assembly_order"
ROOT = 'AssemblyOrder:portal.fm_assembly_order:{"id":10718}'
BEFORE, AFTER = "2026-09-29T12:30:00+00:00", "2026-09-29T13:30:00+00:00"


class MirrorChangedSinceTests(CaseToolsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.rt = self.runtime()
        self.actor = Actor("plx", OPS)
        rows = self.rt.mirror.rows(self.actor, RELATION)["rows"]  # first capture, 12:00
        self.handle = next(r["citation"] for r in rows if r["row"]["id"] == 10718)

    def supersede(self) -> str:
        """Change the cited row upstream and refresh at 13:00; the new current citation."""
        self.fake.update(RELATION, 10718, lotno="L-2",
                         audittimestampmodified="2026-09-29T12:45:00")
        self.clock.advance(3600)
        self.rt.mirror.refresh(self.actor, RELATION)
        return self.rt.mirror.resolve_citation(self.handle, self.actor)["current_citation"]

    def loop(self, actor: Actor) -> dict:
        """agent_loop.run end to end: plx.read the cited row, then claim its lot."""
        server = MCPServer(case_runtime=self.rt, actor=actor,
                           ontology=CaseHostTools(self.rt, actor))
        self.addCleanup(server.close)

        def planner(request):
            if not request["transcript"]:
                return {"action": "call", "calls": [
                    {"tool": "plx.read", "arguments": {"citation": self.handle}}]}, None
            return {"action": "answer", "unknowns": [], "claims": [
                {"text": "The assembly order's lot is L-1.", "citations": [self.handle],
                 "value_claims": [{"value": "L-1", "citation": self.handle,
                                   "field": "row.lotno"}]}]}, None
        return agent_loop.run("What lot is the assembly order?", server, planner)

    def investigate(self, actor: Actor, **extra) -> dict:
        """plx.investigate over a canned plx.object whose lot cites the row; the claim gate
        reads it through ``citation_rows`` on the case runtime."""
        def obj(arguments, _actor):
            return {"object": {"object_id": ROOT, "object_type": "AssemblyOrder",
                               "display": None, "lineage": "gold_etl_from_fm"},
                    "values": [{"property": "lot_number", "value_type": "text",
                                "value": {"untrusted_text": "L-1", "origin": {
                                    "citation": self.handle, "field_path": "row.lotno"}},
                                "markings": [],
                                "citation": {"l1_handle": self.handle,
                                             "field_path": "row.lotno",
                                             "digest": "b" * 64, "lineage": "fm_bronze"}}],
                    "conflicts": [], "identity": [], "linked_counts": [], "gaps": [],
                    "completeness": {"complete": True, "truncated": False, "returned": 1},
                    "meta": {"schema_digest": "a" * 64, "as_known_at": AFTER,
                             "freshness": []}}
        doc = {"$schema": "_plan.schema.json", "format": oi.PLAN_FORMAT, "id": "ao_lot",
               "families": ["F9"], "title": "t", "description": "d",
               "root": {"object_types": ["AssemblyOrder"]}, "step_budget": 4,
               "steps": [{"id": "read", "tool": "plx.object", "from": "root",
                          "bind": "object_id", "required": True}],
               "evidence": [{"name": "lot", "kind": "property",
                             "object_type": "AssemblyOrder", "properties": ["lot_number"]}]}
        self.assertEqual(oi.check_plan(doc, ot.default_schema(), ot.load_contracts()), [])
        tools = oi.InvestigateTools({"plx.object": {"handler": obj}},
                                    plans={"ao_lot": oi.Plan(doc, "procedure:ao_lot@" + "0" * 16)},
                                    schema=ot.default_schema(),
                                    rows=oi.citation_rows(self.rt))
        result = tools.call("plx.investigate", {"family": "F9", "root": ROOT, **extra}, actor)
        self.assertNotIn("error", result, result)
        return result

    def lot(self, result: dict) -> tuple:
        entry = next(e for e in result["answer"] if e["name"] == "lot")
        return entry["check"], entry.get("code")

    def test_a_superseded_mirror_value_is_changed_since_for_the_loop_and_investigate(self):
        # Current: both gates pass the cited value.
        answered = self.loop(self.actor)
        self.assertEqual(answered["outcome"], "answered", answered)
        self.assertEqual([c["check"]["status"] for c in answered["claims"]], ["passed"])
        self.assertEqual(self.lot(self.investigate(self.actor)), ("passed", None))
        case_id = self.rt.cases.open(self.actor, "What lot is AO-26005B?")
        self.rt.cases.finding(self.actor, case_id, "The lot of the assembly order is L-1.",
                              "fact", "supported", citations=[self.handle])
        recorded = self.rt.cases.get(self.actor, case_id)["findings"]["supported"][0][
            "citations"][0]["digest"]
        claims_before = case_verification.check_claims(self.rt, self.actor, case_id)

        current = self.supersede()
        self.assertNotEqual(current, self.handle)
        # The agent loop: the claim is unsupported with the gate's code, never passed.
        moved = self.loop(self.actor)
        self.assertEqual((moved["outcome"], moved["reason"], moved["claims"]),
                         ("abstained", "no_checked_claims", []))
        self.assertEqual([(u["reason"], [f["code"] for f in u["failures"]])
                          for u in moved["unsupported_claims"]],
                         [("claim_check_failed", ["claim_changed_since"])])
        # plx.investigate now: failed, listed, never check: passed.
        now = self.investigate(self.actor)
        self.assertEqual(self.lot(now), ("failed", "claim_changed_since"))
        self.assertEqual(now["claim_checks"]["failures"],
                         [{"name": "lot", "code": "claim_changed_since"}])
        self.assertIn("claim_check_failed", now["answerability"]["reasons"])
        # As known before the supersession, the value was current: checked as usual.
        self.assertEqual(self.lot(self.investigate(self.actor, as_known_at=BEFORE)),
                         ("passed", None))
        self.assertEqual(self.lot(self.investigate(self.actor, as_known_at=AFTER)),
                         ("failed", "claim_changed_since"))
        # The digested view is unchanged: the case's recorded digest still matches, the
        # case claim check reads the same, and the status rides only on the gate's row.
        view = self.rt.resolve_citation(self.handle, self.actor)
        self.assertEqual(_digest(view), recorded)
        self.assertNotIn("changed_since", view)
        self.assertEqual(case_verification.check_claims(self.rt, self.actor, case_id),
                         claims_before)
        row = oi.citation_rows(self.rt)(self.handle, self.actor)
        self.assertEqual((row["changed_since"], row["superseded_at"], row["current_citation"],
                          row["row"]["lotno"]), (True, "2026-09-29T13:00:00.000000Z", current, "L-1"))
        # The current citation is no changed-since view; an outsider reads no row at all.
        self.assertNotIn("changed_since", oi.citation_rows(self.rt)(current, self.actor))
        self.assertIsNone(oi.citation_rows(self.rt)(self.handle, Actor("plx", OUTSIDER)))
        self.assertEqual(self.lot(self.investigate(Actor("plx", OUTSIDER))),
                         ("failed", "claim_uncited"))

    def test_an_unreadable_supersession_time_fails_closed(self):
        self.supersede()
        real = self.rt.mirror.resolve_citation

        def unknown(handle, actor, **kw):
            result = real(handle, actor, **kw)
            return {**result, "superseded_at": None} if result.get("ok") else result
        self.rt.mirror.resolve_citation = unknown
        self.assertEqual(self.lot(self.investigate(self.actor, as_known_at=BEFORE)),
                         ("failed", "claim_changed_since"))
        # A status the runtime does not know is changed, never current.
        self.rt.mirror.resolve_citation = lambda h, a, **kw: {**real(h, a, **kw),
                                                              "changed_since": "unknown"}
        self.assertTrue(oi.citation_rows(self.rt)(self.handle, self.actor)["changed_since"])

    def test_agent_loop_has_no_row_reader_or_contracts_of_its_own(self):
        """The loop's final answer goes through the one answer gate, which reads rows
        through ``gate_rows(citation_rows(...))``; neither keeps a row reader or contracts
        of its own."""
        from operating import answer_gate
        loop = Path(agent_loop.__file__).read_text(encoding="utf-8")
        gate = Path(answer_gate.__file__).read_text(encoding="utf-8")
        for source in (loop, gate):
            self.assertIsNone(re.search(r"\.(resolve_citation|claim_row)\(", source))
            self.assertNotIn("SourceUnitContracts", source)
        self.assertIn("answer_gate.gate_answer(", loop)
        self.assertIn("gate_rows(citation_rows(", gate)
