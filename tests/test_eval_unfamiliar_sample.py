"""Unfamiliar-transaction find eval (eval.unfamiliar_sample): sampling, scoring, report.

Evidence label: ``injected-transport``. The oracle side reads the synthetic world in
``evals/oracle/fixture_world.py`` through the real ``WorkbenchReadApiClient`` (fixture
fetch) and the fixture BC/document ports; the product side is either an injected tool call
or the real ``plx.find``/``plx.object`` over a projection built from the same fixture world
through the real source mirror and projector. Nothing here is live;
live check ``eval.unfamiliar_sample`` stays live-unverified until the live
verifier records a pass.
"""

from __future__ import annotations

import contextlib
import gc
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import warnings
from urllib.parse import quote

from verify import live_verify
from evals import measure
from evals.oracle import PortalReads, Sources
from evals.oracle.cross_sample import (DOCUMENT_KINDS, KINDS, THREE_WAY, Item, names_exactly,
                                       public_row, sample, system_of)
from evals.oracle.fixture_world import build_world
from evals.oracle.generator import BC_INVOICE_SETS
from evals.oracle import keys as K
from evals.oracle.source import field_value, key_text
from evals.score import canonical
from scripts import eval_oracle_dev as dev
from scripts import eval_unfamiliar_sample as ev

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "devloop" / "live-checks" / "eval.unfamiliar_sample.json"


def fixture_sample(run_id: str = "seed-a", count: int = 28):
    sources, documents, _, world = dev.build_fixture()
    return sample(sources, run_id=run_id, count=count, documents=documents), sources, world


class Unnamed:
    """A document port whose every search names nothing: no hit, the sources of
    ``listing(kind)``, its withheld count once per kind (the first query's result of the
    kind's first search). The documents are searched one way,
    ``generator.document_candidates``."""

    def listing(self, kind):
        raise NotImplementedError

    def search(self, kind, queries, *, hits, seed=""):
        listed, given = self.listing(kind), vars(self).setdefault("given", set())
        withheld = 0 if kind in given else listed.get("withheld", 0)
        given.add(kind)
        return {"records": [], "source": listed["source"],
                "results": [{"query": q, "handles": [], "complete": True,
                             "withheld": 0 if n else withheld}
                            for n, q in enumerate(queries)]}


def root_handle(item: Item) -> str:
    key = dict(item.key)
    if item.relation.startswith("bc:"):
        return (f"bc://production/{key['company_id'].upper()}/{item.relation[3:]}/"
                f"{key['id']}@W/\"etag\"")
    return f"api://{item.relation}/{quote(canonical(key), safe='')}@0123456789abcdef-1"


def sibling(item: Item) -> str:
    """Another record of the root's own relation."""
    return f"api://{item.relation}/{quote(canonical({'id': 'other'}), safe='')}@{'b' * 16}-2"


def candidate(handle: str, n: int, text: str = "x", reason: str = "exact_identifier",
              object_id: str | None = None) -> dict:
    return {"object": {"object_id": object_id or f"Thing:fixture:{n}", "object_type": "Thing",
                       "display": None, "lineage": "gold_etl_from_fm"},
            "rank": n, "reason": reason, "tier": "verified",
            "matched": {"identifier_kind": "x", "normalized": "x", "property": "p",
                        "binding": "b.c", "role": "primary",
                        "raw": {"untrusted_text": text,
                                "origin": {"citation": handle, "field_path": "row.x"}}}}


def object_of(handle: str) -> str:
    """The object id an injected plx.find gives the candidate of ``handle``."""
    return "Thing:fixture:" + hashlib.sha256(handle.encode()).hexdigest()[:16]


class ScriptedFind:
    """An injected product: per identifier, the candidate handles plx.find returns, each
    matched by its literal text with reason exact_identifier unless ``leads`` names the
    handle (then a ``display_text`` lead). ``processes``: root handle -> the plx.process
    answer from that candidate (its object id is derived from the handle)."""

    def __init__(self, answers: dict[str, list[str]], leads: frozenset = frozenset(),
                 processes: dict[str, dict] | None = None) -> None:
        self.answers = answers
        self.leads = leads
        self.processes = {object_of(h): r for h, r in (processes or {}).items()}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, dict(arguments)))
        if name == "plx.process" and arguments.get("from") in self.processes:
            return self.processes[arguments["from"]]
        if name != "plx.find":
            return {"error": {"code": "not_scripted"}}
        handles = self.answers.get(arguments["query"], [])
        status = "not_found" if not handles else ("found" if len(handles) == 1 else
                                                  "ambiguous")
        return {"status": status, "candidates": [
                    candidate(h, i + 1, arguments["query"],
                              "display_text" if h in self.leads else "exact_identifier",
                              object_of(h))
                    for i, h in enumerate(handles)],
                "gaps": [], "completeness": {"complete": True, "next_cursor": None}}


def process_answer(reading: dict, flip: tuple[str, ...] = (), undecided: tuple[str, ...] = ()
                   ) -> dict:
    """A plx.process answer (contract shape) that says what the oracle's ``reading`` says,
    except the aspects in ``flip`` (the opposite decided answer) and ``undecided`` (a typed
    gap / ``agree: null``)."""
    stages, checks = [], []
    for aspect, entry in reading["aspects"].items():
        kind, _, name = aspect.partition(":")
        value = entry["value"]
        if aspect in undecided:
            value = None
        elif aspect in flip:
            value = {"present": "absent", "absent": "present", True: False,
                     False: True}[value]
        if kind == "stage":
            row = {"stage": name, "state": "present" if value == "present" else "missing",
                   "events": []}
            if value != "present":
                row["missing"] = {"state": "absent_in_source" if value == "absent"
                                  else "unknown", "reason": "fixture", "subject": name}
            stages.append(row)
        else:
            checks.append({"roles": name.split("_"), "values": [], "agree": value})
    return {"process": "purchase_to_invoice", "stages": stages, "quantity_checks": checks,
            "gaps": [], "completeness": {"complete": True}, "meta": {}}


class SamplingTest(unittest.TestCase):
    def test_same_run_id_same_sample_and_another_run_id_another(self):
        first, _, _ = fixture_sample("seed-a")
        again, _, _ = fixture_sample("seed-a")
        other, _, _ = fixture_sample("seed-b")
        self.assertEqual(first.items, again.items)
        self.assertEqual(first.kinds, again.kinds)
        self.assertGreaterEqual(len(first.items), 25)
        self.assertNotEqual([i.hash16 for i in first.items], [i.hash16 for i in other.items])

    def test_every_kind_is_sampled_and_spread(self):
        drawn, _, _ = fixture_sample()
        self.assertEqual(len(drawn.items), 28)
        self.assertEqual(set(drawn.kinds), set(KINDS))
        for kind in KINDS:
            self.assertGreaterEqual(drawn.kinds[kind]["items"], 1, kind)
            self.assertEqual(drawn.kinds[kind]["items"],
                             sum(1 for i in drawn.items if i.kind == kind))

    def test_identifier_comes_from_another_systems_record_and_names_exactly_the_root(self):
        drawn, sources, world = fixture_sample()
        edges = {e.name: e for e in K.EDGES}
        texts = {kind: [t for d in world.docs.get(kind, []) for t in d["texts"]]
                 for kind in ("mail", "sharepoint")}
        foreign_text = 0
        for item in drawn.items:
            self.assertNotEqual(item.reference_system, system_of(item.relation), item)
            if item.kind in ("mail", "sharepoint"):
                self.assertTrue(any(item.identifier in t for t in texts[item.kind]))
                continue
            edge = edges[item.reference]
            other, field = ((edge.target, edge.target_field) if edge.source == item.relation
                            else (edge.source, edge.source_field))
            own = edge.source_field if edge.source == item.relation else edge.target_field
            self.assertEqual(system_of(other), item.reference_system)
            # The identifier is a value the other system's record holds in that field.
            if other.startswith("bc:"):
                held = {key_text(r["fields"].get(field)) for r in world.bc[other[3:]]}
            else:
                held = {key_text(field_value(r, field)) for r in world.relations[other]}
            self.assertIn(item.identifier, held, item.reference)
            # ...and the oracle's own lookup on the root's joined field finds only the root.
            root = next(r for r in self._roots(sources, item) if r.key == item.key)
            self.assertTrue(names_exactly(sources, root, own, item.identifier))
            foreign_text += item.identifier != root.identifier
        # Some identifiers are not the root's own number at all (an FM PO number, a
        # vendor invoice No.): the question cannot be answered by echoing the root's key.
        self.assertGreater(foreign_text, 0)

    @staticmethod
    def _roots(sources, item):
        from evals.oracle.cross_sample import bc_roots, portal_roots
        return bc_roots(sources, 50) if item.root_kind == "bc_invoice" \
            else portal_roots(sources, item.root_kind)

    def test_staging_rows_are_never_roots_and_unconfigured_sources_spill(self):
        world = build_world()
        sources = Sources(PortalReads(world.read_relation), None)
        drawn = sample(sources, run_id="seed-a", count=28, documents=None)
        self.assertEqual(drawn.kinds["bc_invoice"]["status"], "not_configured")
        self.assertEqual(drawn.kinds["mail"]["status"], "not_configured")
        self.assertTrue(all(i.kind not in ("bc_invoice", "mail", "sharepoint")
                            for i in drawn.items))
        self.assertTrue(all(i.reference_system != "bc" for i in drawn.items))
        self.assertGreater(len(drawn.items), 0)
        portal = sources.portal
        for item in drawn.items:
            row = portal.lookup(item.relation, "id", dict(item.key)["id"])[0]
            from evals.oracle.generator import lineage
            self.assertNotEqual(lineage(item.relation, row, portal), "gold_native_staging")


# Every source a three-way reading names (Portal relations and the BC purchase invoices).
THREE_WAY_SOURCES = ("public.app_purchase_order", "public.app_purchase_order_line",
                     "public.app_receipt", "public.app_receipt_line")


def holding(call, items, *, but: tuple[str, ...] = (), extra: tuple[str, ...] = ()
            ) -> ev.Product:
    """An injected product whose projection holds every root relation except ``but``, and
    the audience keys in ``extra``; it stands for the session projection the spec's run
    opens (the spec pins ``eval.product.source``)."""
    keys = set(extra)
    for item in items:
        if item.relation in but:
            continue
        if item.relation.startswith("bc:"):
            keys.add(f"bc:{dict(item.key)['company_id']}/{item.relation[3:]}")
        else:
            keys.add(f"api:{item.relation}")
    return ev.Product(call, {"source": "session_projection", "objects": 10},
                      audience_keys=keys)


def three_way_holding(call, items) -> ev.Product:
    """``holding`` plus every source a three-way reading names (BC in one company)."""
    return holding(call, items, extra=tuple(f"api:{r}" for r in THREE_WAY_SOURCES)
                   + ("bc:c1/purchaseInvoices",))


def answered(drawn, *, unanswered: tuple[str, ...] = ()) -> dict:
    """A report from an injected product that finds every root exactly, and answers each
    three-way item's plx.process as the oracle reads it, except the items of the
    ``unanswered`` kinds (plx.find returns nothing for them)."""
    answers = {i.identifier: [root_handle(i)] for i in drawn.items
               if i.kind not in unanswered}
    for item in drawn.items:   # a token a document shares with an unanswered kind's root
        if item.kind in unanswered:
            answers.pop(item.identifier, None)
    processes = {root_handle(i): process_answer(drawn.readings[i.hash16])
                 for i in drawn.items if i.kind == THREE_WAY}
    return ev.run_eval(drawn, three_way_holding(ScriptedFind(answers, processes=processes),
                                                drawn.items),
                       run_id="seed-a", evidence_label="injected-transport", configured={})


def recorded_whole(report: dict, failures: list[str]) -> bool:
    """Whether the verifier keeps a failing run's recorded fields and failures uncut."""
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    detail = live_verify._cap({"fields": live_verify._recorded(spec, report),
                               "failures": failures})
    return "fields_truncated" not in detail and detail["failures"] == failures


def answered_kinds(drawn) -> dict:
    """The kinds typed blocked in ``eval.kinds_typed_blocked`` of ``drawn`` answered fully."""
    return answered(drawn)["eval"]["kinds_typed_blocked"]


def spec_failures(report: dict, exit_code: int | None = None) -> list[str]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    document = dict(report, evidence_label="live-read")  # a fixture run differs only here
    code = ev.verdict(report) if exit_code is None else exit_code
    return live_verify.check_expectations(spec, code, document)


def passing(kind: str) -> str:
    """The outcome that passes ``kind``."""
    return "three_way_correct" if kind == THREE_WAY else "found_exact"


def outcomes(kind: str) -> tuple[str, ...]:
    return ev.THREE_WAY_OUTCOMES if kind == THREE_WAY else ev.OUTCOMES


class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.drawn, _, _ = fixture_sample()

    def scripted(self):
        answers: dict[str, list[str]] = {}
        expected = {}
        for item in self.drawn.items:
            handle = root_handle(item)
            if item.kind in ("sales_order", "assembly_order"):
                answers[item.identifier] = [handle]
                expected[item.hash16] = "found_exact"
            elif item.kind == "purchase_order":
                answers[item.identifier] = [handle, sibling(item)]
                expected[item.hash16] = "ambiguous"
            elif item.kind == "receipt":
                answers[item.identifier] = [sibling(item)]
                expected[item.hash16] = "wrong"
        for item in self.drawn.items:  # no exact root, so plx.process is never asked
            if item.kind == THREE_WAY:
                expected[item.hash16] = "root_not_resolved"
        for item in self.drawn.items:  # documents and BC invoices get no answer
            if item.hash16 not in expected and item.identifier not in answers:
                expected[item.hash16] = "not_found"
        product = holding(ScriptedFind(answers), self.drawn.items)
        report = ev.run_eval(self.drawn, product, run_id="seed-a",
                             evidence_label="injected-transport", configured={})
        return report, expected

    def test_outcomes_and_rates_by_kind_and_answering_path(self):
        report, expected = self.scripted()
        rows = report["items"]
        # (an identifier a document shares with a sampled record gets that record's answer)
        self.assertEqual({r["hash16"]: r["outcome"] for r in rows if r["hash16"] in expected},
                         expected)
        self.assertGreaterEqual(len(expected), 20)
        block = report["eval"]
        self.assertEqual(set(block["by_kind"]), set(KINDS))
        for kind in KINDS:
            group = [r for r in rows if r["kind"] == kind]
            passed = sum(r["outcome"] == "found_exact" for r in group)
            self.assertEqual(block["by_kind"][kind]["pass"], f"{passed}/{len(group)}")
            for outcome in outcomes(kind):   # a zero count is left out
                self.assertEqual(block["by_kind"][kind].get(outcome, 0),
                                 sum(r["outcome"] == outcome for r in group))
        n = self.drawn.kinds["sales_order"]["items"]
        self.assertEqual(block["by_kind"]["sales_order"]["pass"], f"{n}/{n}")
        self.assertTrue(block["by_kind"]["receipt"]["pass"].startswith("0/"))
        # Every item was answered by plx.find: untemplated; no plan or script answered.
        self.assertEqual({r["bucket"] for r in rows}, {"untemplated"})
        self.assertEqual({r["path"] for r in rows}, {"find"})
        self.assertEqual(block["by_bucket"],
                         {"scripted": "0/0", "plan-family": "0/0",
                          "untemplated": f"{report['counts']['found_exact']}/{len(rows)}"})
        self.assertEqual(block["pass"], f"{report['counts']['found_exact']}/{len(rows)}")
        self.assertEqual(block["scored"], len(rows))
        self.assertTrue(block["complete"])
        # ENG-11 diagnostics from each item's own tool calls: a found or wrong root is an
        # answer, a miss is an unanswered question, an ambiguous root a person's choice;
        # sampled items never pair with a manual baseline.
        resolution = {"found_exact": "answered", "wrong": "answered", "ambiguous": "unanswered",
                      "not_found": "unanswered", "root_not_resolved": "unanswered"}
        for row in rows:
            self.assertEqual(row["measure"]["resolution"], resolution[row["outcome"]], row)
            self.assertEqual(row["measure"]["tool_calls"] > 0, True)
        spent = block["measure"]  # zero counts left out
        self.assertEqual((spent["runs"], spent["answer_ms"]["measured"]), (len(rows), len(rows)))
        self.assertEqual(spent["answered"], sum(r["outcome"] in ("found_exact", "wrong")
                                                for r in rows))
        self.assertEqual(spent["identity_reviews"],
                         sum(r["outcome"] == "ambiguous" or r["reason"] == "find_ambiguous"
                             for r in rows))
        self.assertEqual(spent["first_cited_evidence_ms"]["measured"],
                         sum(r["candidates"] > r["uncited_candidates"] for r in rows))
        self.assertNotIn("unsupported_assertions", spent)  # every candidate was cited
        self.assertNotIn("vs_manual", block)

    def test_aggregates_other_system_candidates_and_sampling_exclusions(self):
        items = [i for i in self.drawn.items if i.kind == "sales_order"]
        twin = "bc://production/C1/salesOrders/abc@W/\"1\""
        answers = {i.identifier: [root_handle(i), twin] for i in items}
        report = ev.run_eval(self.drawn, holding(ScriptedFind(answers), self.drawn.items),
                             run_id="seed-a", evidence_label="injected-transport",
                             configured={})
        block = report["eval"]
        rows = report["items"]
        self.assertEqual(block["other_system_candidates"],
                         {"items_with": sum(1 for r in rows if r["other_system_candidates"]),
                          "total": sum(r["other_system_candidates"] for r in rows)})
        by_hash = {r["hash16"]: r for r in rows}
        for item in items:  # each sales order root came back with its one BC twin
            self.assertEqual(by_hash[item.hash16]["other_system_candidates"], 1)
        self.assertGreaterEqual(block["other_system_candidates"]["items_with"], len(items))
        for name in ev.SAMPLING_EXCLUSIONS:
            self.assertEqual(block["sampling_exclusions"].get(name, 0),
                             sum(k["skipped"].get(name, 0) for k in self.drawn.kinds.values()))
        self.assertNotIn(0, block["sampling_exclusions"].values())   # zero counts left out
        self.assertGreater(block["sampling_exclusions"]["no_reference"], 0)

    def test_roots_the_product_never_captured_are_excluded_and_counted(self):
        answers = {i.identifier: [root_handle(i)] for i in self.drawn.items}
        product = holding(ScriptedFind(answers), self.drawn.items,
                          but=("public.app_receipt",))
        report = ev.run_eval(self.drawn, product, run_id="seed-a",
                             evidence_label="injected-transport", configured={})
        receipts = [i for i in self.drawn.items if i.relation == "public.app_receipt"]
        self.assertGreater(len(receipts), 0)
        block = report["eval"]
        self.assertEqual(block["excluded"], {"product_not_holding_root": len(receipts)})
        self.assertEqual(block["scored"], len(self.drawn.items) - len(receipts))
        self.assertEqual(int(block["pass"].split("/")[1]), block["scored"])
        self.assertEqual(block["by_kind"]["receipt"]["excluded"],
                         sum(1 for i in receipts if i.kind == "receipt"))
        excluded = [r for r in report["items"] if r["outcome"] == "excluded"]
        self.assertEqual({r["reason"] for r in excluded}, {"product_not_holding_root"})
        self.assertTrue(all(r["bucket"] is None for r in excluded))

    def test_only_the_identifier_goes_to_find(self):
        finder = ScriptedFind({})
        ev.run_eval(self.drawn, holding(finder, self.drawn.items), run_id="seed-a",
                    evidence_label="injected-transport", configured={})
        finds = [a for n, a in finder.calls if n == "plx.find"]
        self.assertEqual([a["query"] for a in finds], [i.identifier for i in self.drawn.items])
        self.assertTrue(all(set(a) == {"query", "limit"} for a in finds))

    def test_other_system_candidates_do_not_block_an_exact_find(self):
        item = next(i for i in self.drawn.items if i.kind == "sales_order")
        bc_twin = "bc://production/C1/salesOrders/abc@W/\"1\""
        product = holding(ScriptedFind({item.identifier: [root_handle(item), bc_twin]}),
                          [item])
        scored = ev.score_item(product, item)
        self.assertEqual((scored["outcome"], scored["other_system_candidates"]),
                         ("found_exact", 1))

    def test_a_root_found_only_as_a_lead_is_found_lead_never_exact(self):
        """find_root's gate: a display-text match, or a typed match whose stored text is
        not the asked text (normalized only), is a lead (eval-unfamiliar-sample-scoring)."""
        item = next(i for i in self.drawn.items if i.kind == "sales_order")
        handle = root_handle(item)
        lead = ev.score_item(holding(ScriptedFind({item.identifier: [handle]},
                                                  leads=frozenset({handle})), [item]), item)
        self.assertEqual((lead["outcome"], lead["root_rank"]), ("found_lead", 1))

        class Normalized(ScriptedFind):
            def __call__(self, name, arguments):
                result = super().__call__(name, arguments)
                for c in result.get("candidates", []):
                    c["matched"]["raw"]["untrusted_text"] = arguments["query"].lower()
                return result

        product = holding(Normalized({item.identifier: [handle]}), [item])
        product.grammar = None  # trimmed, case-sensitive: a case-only match is no root
        self.assertEqual(ev.score_item(product, item)["outcome"], "found_lead")
        # A lead sibling beside the exact root does not make it ambiguous.
        mixed = ScriptedFind({item.identifier: [sibling(item), handle]},
                             leads=frozenset({sibling(item)}))
        scored = ev.score_item(holding(mixed, [item]), item)
        self.assertEqual((scored["outcome"], scored["root_rank"]), ("found_exact", 2))

    def test_success_at_1_mrr_and_rates_over_sampled_and_scored(self):
        items = self.drawn.items
        orders = [i for i in items if i.kind == "sales_order"]
        pos = [i for i in items if i.kind == "purchase_order"]
        receipts = [i for i in items if i.relation == "public.app_receipt"]
        self.assertTrue(orders and pos and receipts)
        answers, leads = {}, set()
        for item in orders:          # rank 1, exact
            answers[item.identifier] = [root_handle(item)]
        for item in pos:             # rank 2 behind a lead sibling, exact
            answers[item.identifier] = [sibling(item), root_handle(item)]
            leads.add(sibling(item))
        product = holding(ScriptedFind(answers, frozenset(leads)), items,
                          but=("public.app_receipt",))
        report = ev.run_eval(self.drawn, product, run_id="seed-a",
                             evidence_label="injected-transport", configured={})
        every = report["items"]
        # Three-way rows are scored on plx.process: never in the identity figures.
        rows = [r for r in every if r["kind"] != THREE_WAY]
        self.assertLess(len(rows), len(every))
        scored = [r for r in rows if r["outcome"] != "excluded"]
        self.assertEqual(len(rows) - len(scored), len(receipts))
        exact = sum(1 for r in rows if r["outcome"] == "found_exact")
        first = sum(1 for r in scored if r["root_rank"] == 1)
        mrr = sum(1 / r["root_rank"] for r in scored if r["root_rank"]) / len(scored)
        block = report["eval"]
        ranking = block["ranking"]
        all_scored = [r for r in every if r["outcome"] != "excluded"]
        self.assertEqual((block["items"], block["scored"]), (len(every), len(all_scored)))
        self.assertEqual(block["pass"], f"{exact}/{len(all_scored)}")
        # The identity rates have their own denominators.
        self.assertEqual((ranking["items"], ranking["scored"]), (len(rows), len(scored)))
        self.assertEqual((ranking["found_exact"], ranking["found_lead"]), (exact, 0))
        self.assertEqual(ranking["success_at_1"], first)
        self.assertEqual(ranking["mrr"], round(mrr, 4))
        # Hand count: every order root at rank 1, every PO root at rank 2, others absent.
        hit = {r["hash16"]: r["root_rank"] for r in scored}
        for item in orders:
            self.assertEqual(hit[item.hash16], 1)
        for item in pos:
            if item.hash16 in hit:
                self.assertEqual(hit[item.hash16], 2)
        self.assertGreater(first, 0)
        self.assertLess(ranking["mrr"], 1)

    def test_no_product_excludes_every_item_and_fails(self):
        report = ev.run_eval(self.drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="seed-a", evidence_label="injected-transport",
                             configured={})
        self.assertEqual(report["counts"]["items"], 28)
        self.assertEqual(report["counts"]["excluded"], 28)
        self.assertEqual(report["eval"]["excluded"], {"product_path_unavailable": 28})
        self.assertEqual(report["eval"]["pass"], "0/0")
        self.assertFalse(report["eval"]["product"]["available"])
        self.assertEqual(ev.verdict(report), 3)

    def test_an_empty_projection_fails_even_with_items(self):
        product = ev.Product(ScriptedFind({}), {"objects": 0})
        report = ev.run_eval(self.drawn, product, run_id="seed-a",
                             evidence_label="injected-transport", configured={})
        self.assertEqual(report["eval"]["excluded"], {"product_not_holding_root": 28})
        self.assertEqual(ev.verdict(report), 3)

    def test_an_expired_budget_stops_finding_and_labels_the_run_incomplete(self):
        finder = ScriptedFind({})
        deadline = ev.Deadline(0)
        report = ev.run_eval(self.drawn, holding(finder, self.drawn.items), run_id="seed-a",
                             evidence_label="injected-transport", configured={},
                             deadline=deadline)
        self.assertEqual(finder.calls, [])
        self.assertEqual(report["eval"]["excluded"], {"run_budget_exhausted": 28})
        self.assertFalse(report["eval"]["complete"])
        self.assertIn("run_budget_exhausted", report["eval"]["incomplete"])

    def test_tool_errors_are_not_found_with_a_reason_code(self):
        item = self.drawn.items[0]
        product = holding(lambda name, args: {"error": {"code": "store_unavailable"}}, [item])
        scored = ev.score_item(product, item)
        self.assertEqual((scored["outcome"], scored["reason"]),
                         ("not_found", "tool_error_store_unavailable"))

    def test_handle_identities(self):
        self.assertEqual(ev.handle_identity("bc://production/AB-C/purchaseInvoices/X1@W/\"2\""),
                         ("bc:purchaseInvoices", canonical({"company_id": "ab-c", "id": "x1"})))
        key = quote(canonical({"id": 7}), safe="")
        self.assertEqual(ev.handle_identity(f"api://public.app_receipt/{key}@{'c' * 16}-3"),
                         ("public.app_receipt", canonical({"id": "7"})))
        self.assertIsNone(ev.handle_identity("graph-mail://m/1"))


def one_bucket_each(test: unittest.TestCase, block: dict) -> dict:
    """Every sampled item is in exactly one bucket and every scored one in one class (a
    count left out is zero); returns the block with every count."""
    full = {key: block.get(key, 0) for key in ev.ANSWER_COUNTS}
    test.assertEqual(set(block) - set(full), {"causes"} if "causes" in block else set())
    test.assertEqual(full["sampled"], full["blocked_upstream"] + full["not_held"]
                     + full["unscored_other"] + full["product_error"] + full["scored"])
    test.assertEqual(full["scored"], full["correct"] + full["wrong"] + full["abstain_ok"]
                     + full["missed"])
    test.assertEqual(sum(block.get("causes", {}).values()),
                     full["sampled"] - full["scored"] - full["product_error"])
    return full


class AnswerCorrectnessTest(unittest.TestCase):
    """Answer correctness (three-way) beside root lookup, every
    item accounted for, unscored items with their cause, and a wrong-answer bound."""

    def setUp(self):
        self.drawn, _, _ = fixture_sample()

    def test_three_way_answers_and_root_lookup_are_reported_apart(self):
        report, _ = ScoringTest.scripted(self)
        block = report["eval"]
        three_way = [r for r in report["items"] if r["kind"] == THREE_WAY]
        self.assertEqual(block["answer"]["sampled"], len(three_way))
        self.assertEqual(block["answer"]["missed"], len(three_way))   # root never resolved
        roots = one_bucket_each(self, block["find_root"])
        found = [r["outcome"] for r in report["items"] if r["kind"] != THREE_WAY]
        self.assertEqual(roots["sampled"], len(found))
        # found_exact is correct, a wrong root wrong; an ambiguous answer (the oracle names
        # one root), a lead or nothing is a miss, never an abstention
        self.assertTrue(all(found.count(o) for o in ("found_exact", "wrong", "ambiguous")))
        self.assertEqual((roots["correct"], roots["wrong"], roots["abstain_ok"],
                          roots["missed"]),
                         (found.count("found_exact"), found.count("wrong"), 0,
                          found.count("ambiguous") + found.count("not_found")
                          + found.count("found_lead")))
        one_bucket_each(self, block["answer"])

    def test_unscored_items_carry_their_cause(self):
        outage = ev.run_eval(self.drawn, ev.Product(None, {
            "status": "source_unavailable", "error_code": "source_unavailable",
            "http_status": 503}), run_id="r", evidence_label="injected-transport",
            configured={})["eval"]
        for name in ("answer", "find_root"):
            block = one_bucket_each(self, outage[name])
            self.assertEqual((block["scored"], block["blocked_upstream"]),
                             (0, block["sampled"]), name)
            self.assertEqual(outage[name]["causes"], {"upstream_outage": block["sampled"]})
        answers = {i.identifier: [root_handle(i)] for i in self.drawn.items}
        unbound = ev.run_eval(self.drawn, holding(ScriptedFind(answers), self.drawn.items,
                                                  but=("public.app_receipt",)),
                              run_id="r", evidence_label="injected-transport",
                              configured={})["eval"]["find_root"]
        receipts = sum(1 for i in self.drawn.items if i.relation == "public.app_receipt")
        self.assertEqual(unbound["not_held"], receipts)
        self.assertEqual(unbound["causes"], {"missing_bindings": receipts})
        never_built = ev.run_eval(self.drawn, ev.Product(None, {"status": "not_built"}),
                                  run_id="r", evidence_label="injected-transport",
                                  configured={})["eval"]["find_root"]
        self.assertEqual(set(never_built["causes"]), {"projection_timeout"})
        for block in (unbound, never_built):
            one_bucket_each(self, block)

    @staticmethod
    def own_bc_capture(budget_seconds: float) -> dict:
        """The product's own BC capture (``capture_bc``) of the fixture world's BC under a
        budget of ``budget_seconds`` (one second per request)."""
        from functools import partial
        from unittest import mock
        from integrations import bc_read
        from integrations.tests.test_live_source_adapters import ENV

        fake, books = fixture_bc_sender(build_world())
        probe = partial(bc_read.capture_probe, monotonic=lambda: float(len(fake.calls)))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(bc_read, "capture_probe", probe):
            return ev.run_bc_capture(lambda db, seconds: ev.capture_bc(
                ENV, db, seconds, sender=fake, books=books), Path(tmp) / "bc.sqlite",
                budget_seconds)

    def test_a_bc_root_not_held_takes_its_cause_from_the_bc_capture_not_the_portal(self):
        """Live 2026-10-07: BC roots unheld because the BC capture ran out of budget were
        labelled by the Portal mirror's state; the cause follows the BC capture's, and
        within it the root set's own unit in the root's company: a partial capture that
        read every company's root sets whole (the token, the listing, 2 companies x 2 root
        sets, one more request) leaves a binding gap, never a timeout; one cut inside the
        roots is a timeout, except for the roots of the one unit it read whole (the first
        company's sales invoices; review minor 4eb0f8535a28: the root sets' states were
        merged over the companies, so those read projection_timeout too)."""
        from evals.oracle.fixture_world import COMPANIES

        answers = {i.identifier: [root_handle(i)] for i in self.drawn.items}
        unheld = tuple({i.relation for i in self.drawn.items if i.relation.startswith("bc:")})
        self.assertTrue(unheld)
        roots_whole, roots_cut = self.own_bc_capture(7), self.own_bc_capture(3)
        for capture in (roots_whole, roots_cut):
            self.assertEqual((capture["status"], capture["stopped_reason"]),
                             ("partial", "budget"))
        first, second = (ev.company_key(company) for company, _ in COMPANIES)
        self.assertEqual(roots_whole["root_sets"], {
            s: {"*": "other", first: "complete", second: "complete"} for s in BC_INVOICE_SETS})
        self.assertEqual(roots_cut["root_sets"], {
            "salesInvoices": {"*": "other", first: "complete", second: "budget"},
            "purchaseInvoices": {"*": "other", first: "budget", second: "budget"}})
        whole_in_cut = {i.hash16 for i in self.drawn.items if i.relation == "bc:salesInvoices"
                        and dict(i.key)["company_id"] == COMPANIES[0][0]}
        self.assertTrue(0 < len(whole_in_cut) < sum(i.relation.startswith("bc:")
                                                     for i in self.drawn.items))
        for mirror_complete, bc, cause in (
                (True, roots_whole, "missing_bindings"),
                (False, roots_cut, "projection_timeout"),
                (True, dict(roots_whole, root_sets={s: {"*": "upstream"}
                                                    for s in BC_INVOICE_SETS}),
                 "upstream_outage"),
                (False, {"status": "captured", "units_reached": 6, "units_complete": 6,
                         "seconds": 9.5}, "missing_bindings"),
                (True, {"status": "partial", "units_reached": 6, "units_complete": 2,
                        "seconds": 99.0, "stopped_reason": "budget"}, "projection_timeout"),
                (True, {"status": "no_budget"}, "projection_timeout"),
                (True, {"status": "failed", "error": "SourceHttpError"}, "upstream_outage"),
                (True, {"status": "unexpected"}, "host_misconfiguration")):
            with self.subTest(bc=bc["status"], roots=bc.get("root_sets")):
                product = holding(ScriptedFind(answers), self.drawn.items, but=unheld)
                product.info.update(mirror_complete=mirror_complete, bc_capture=bc)
                report = ev.run_eval(self.drawn, product, run_id="r",
                                     evidence_label="injected-transport", configured={})
                rows = [r for r in report["items"] if r["root_kind"] == "bc_invoice"]
                self.assertTrue(rows)
                expected = {r["hash16"]: "missing_bindings" if bc is roots_cut and
                            r["hash16"] in whole_in_cut else cause for r in rows}
                self.assertEqual({r["hash16"]: (r["reason"], r["cause"]) for r in rows},
                                 {h: ("product_not_holding_root", c)
                                  for h, c in expected.items()})
                # eval.product says how far the BC capture got (sparse: no stop, no key)
                lifted = report["eval"]["product"]
                self.assertEqual(lifted["bc_capture"], bc["status"])
                self.assertEqual({k: lifted.get("bc_" + k) for k in ev.BC_PRODUCT_FIELDS},
                                 {k: bc.get(k) for k in ev.BC_PRODUCT_FIELDS})

    def test_mostly_wrong_answers_fail_even_when_every_kind_passes(self):
        """The dishonest shape the rule used to pass: one exact item per kind, the rest
        confidently wrong."""
        answers, first, wrong = {}, set(), []
        for item in self.drawn.items:   # one identifier may be asked by two kinds
            if item.kind == THREE_WAY or item.kind not in first:
                handles = answers.setdefault(item.identifier, [])
                if root_handle(item) not in handles:   # two kinds asking one root
                    handles.append(root_handle(item))
                first.add(item.kind)
            else:
                wrong.append(item)
        for item in wrong:
            answers.setdefault(item.identifier, [sibling(item)])
        processes = {root_handle(i): process_answer(self.drawn.readings[i.hash16])
                     for i in self.drawn.items if i.kind == THREE_WAY}
        report = ev.run_eval(self.drawn, three_way_holding(
            ScriptedFind(answers, processes=processes), self.drawn.items), run_id="seed-a",
            evidence_label="injected-transport", configured={})
        block = report["eval"]
        self.assertEqual(block["kinds_missing"], [])
        self.assertGreaterEqual(block["scored"], 20)
        wrong = block["answer"].get("wrong", 0) + block["find_root"].get("wrong", 0)
        self.assertGreater(wrong * 10, block["answer"]["scored"] + block["find_root"]["scored"])
        self.assertIs(block["wrong_within_bound"], False)
        self.assertEqual(ev.verdict(report), 3)
        self.assertEqual(block["fail_reasons"], ["wrong_above_bound"])
        self.assertEqual(spec_failures(report), ["exit_code 3 != 0",
                                                 "eval.wrong_within_bound != expected",
                                                 "eval.fail_reasons not empty"])
        honest = answered(self.drawn)
        self.assertIs(honest["eval"]["wrong_within_bound"], True)
        self.assertEqual(ev.verdict(honest), 0)


    def test_a_product_tool_error_is_product_error_never_an_abstention_and_fails(self):
        answers = {i.identifier: [root_handle(i)] for i in self.drawn.items}
        processes = {root_handle(i): process_answer(self.drawn.readings[i.hash16])
                     for i in self.drawn.items if i.kind == THREE_WAY}
        broken = next(h for h in processes)
        processes[broken] = {"error": {"code": "internal"}}
        report = ev.run_eval(self.drawn, three_way_holding(
            ScriptedFind(answers, processes=processes), self.drawn.items), run_id="seed-a",
            evidence_label="injected-transport", configured={})
        answer = one_bucket_each(self, report["eval"]["answer"])
        self.assertEqual((answer["product_error"], answer["abstain_ok"]), (1, 0))
        self.assertEqual(ev.verdict(report), 3)
        self.assertEqual(ev.verdict(answered(self.drawn)), 0)


class BudgetAndCoverageTest(unittest.TestCase):
    def test_mirrored_relations_are_derived_from_the_schema(self):
        from ontology.schema import load_schema
        schema = load_schema()
        roots = ev.root_relations()
        self.assertEqual(roots, sorted({r for _, relations in
                                        __import__("evals.oracle.generator", fromlist=["x"])
                                        .PORTAL_STRATA.values() for r, _ in relations}))
        self.assertIn("public.receipt_raw_material", roots)
        wanted = ev.required_relations(schema, roots)
        self.assertTrue(set(roots) <= set(wanted))
        types = {tid for tid, t in schema.object_types.items()
                 for b in t["bindings"] if b.get("relation") in roots}
        bound = {b["relation"] for tid in types for b in schema.object_types[tid]["bindings"]
                 if b.get("source") == "portal_api"}
        self.assertTrue(bound <= set(wanted))
        every = {b["relation"] for t in schema.object_types.values() for b in t["bindings"]
                 if b.get("source") == "portal_api"}
        self.assertLess(len(wanted), len(every))  # a subset, not the whole catalog
        self.assertEqual(ev.required_relations(schema, []), [])

    def test_a_starved_projection_is_empty_and_the_check_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            from integrations.workbench_read_api import WorkbenchReadApiClient
            client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                            "plxwb_" + "f" * 40,
                                            fetch=ev._fixture_fetch(build_world()))
            from ontology.schema import load_schema
            relations = ev.required_relations(load_schema(), ev.root_relations())
            store, info, complete = ev.build_projection(client, Path(tmp) / "p", relations,
                                                        budget_seconds=1e-6)
            self.assertIsNotNone(store)
            self.assertFalse(info["mirror_complete"])
            self.assertEqual(info["objects"], 0)
            self.assertEqual(complete, frozenset())
            product = ev.open_product(store, info=info, complete_relations=complete)
            self.addCleanup(product.close)
            self.assertEqual(product.object_count, 0)
            drawn, _, _ = fixture_sample()
            report = ev.run_eval(drawn, product, run_id="seed-a",
                                 evidence_label="injected-transport", configured={})
        self.assertEqual(report["eval"]["product"]["object_count"], 0)
        self.assertFalse(report["eval"]["complete"])
        self.assertIn("mirror_incomplete", report["eval"]["incomplete"])
        self.assertEqual(report["eval"]["pass"], "0/0")
        self.assertEqual(ev.verdict(report), 3)

    def _scored_report(self, scored: int) -> dict:
        """A report from an injected product that holds only ``scored`` roots, taken one
        kind at a time so every kind is scored once ``scored`` >= 8."""
        drawn, _, _ = fixture_sample()
        answers = {i.identifier: [root_handle(i)] for i in drawn.items}
        processes = {root_handle(i): process_answer(drawn.readings[i.hash16])
                     for i in drawn.items if i.kind == THREE_WAY}
        product = three_way_holding(ScriptedFind(answers, processes=processes), drawn.items)
        queues = {k: [i for i in drawn.items if i.kind == k] for k in KINDS}
        order = [q[n] for n in range(28) for q in queues.values() if n < len(q)]
        ids = {i.hash16 for i in order[:scored]}
        product.holds = lambda item: item.hash16 in ids  # the rest: never captured
        return ev.run_eval(drawn, product, run_id="seed-a",
                           evidence_label="injected-transport", configured={})

    def test_too_few_scored_items_fail_however_good_the_rate(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        for scored in (0, 1, 19):
            report = self._scored_report(scored)
            self.assertEqual(report["eval"]["scored"], scored)
            self.assertEqual(report["eval"]["pass"].split("/")[1], str(scored))
            self.assertTrue(report["eval"]["complete"])
            self.assertEqual(ev.verdict(report), 3, scored)
            document = dict(report, evidence_label="live-read")
            self.assertIn("too_few_scored", report["eval"]["fail_reasons"], scored)
            self.assertIn("eval.fail_reasons not empty",
                          live_verify.check_expectations(spec, 0, document), scored)
        report = self._scored_report(20)
        self.assertEqual(ev.verdict(report), 0)
        self.assertEqual(ev.verdict(report, min_scored=21), 3)

    def test_a_budget_cut_run_fails_even_with_enough_scored_items(self):
        report = self._scored_report(24)
        self.assertEqual(ev.verdict(report), 0)
        report["eval"]["incomplete"].append("run_budget_exhausted")
        report["eval"]["complete"] = False
        self.assertEqual(ev.verdict(report), 3)

    def test_a_short_budget_that_cuts_the_finds_fails_the_command(self):
        # The run deadline passes after 5 finds (an injected clock), not after a race
        # between a 1 s wall-time budget and how fast this machine builds and finds.
        from unittest import mock

        now, finds, find_root = [0.0], [0], ev.find_root

        def find_then_tick(product, item, deadline=None):
            finds[0] += 1
            if finds[0] > 5:
                now[0] = 1e9
            return find_root(product, item, deadline)

        real = ev.Deadline
        clocked = lambda seconds: real(seconds, monotonic=lambda: now[0])  # noqa: E731
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), mock.patch.object(ev, "Deadline", clocked), \
                mock.patch.object(ev, "find_root", find_then_tick):
            code = ev.main(["fixture", "--total-budget-seconds", "600",
                            "--projection-reserve-seconds", "0"], environ={})
        report = json.loads(buffer.getvalue())
        self.assertIn("run_budget_exhausted", report["eval"]["incomplete"])
        self.assertEqual(code, 3)
        self.assertFalse(report["eval"]["complete"])
        self.assertLess(report["eval"]["scored"], 20)

    def test_a_spent_total_budget_fails_the_command_cleanly(self):
        # 0, not a tiny positive budget: that races the fixture setup's wall time
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ev.main(["fixture", "--total-budget-seconds", "0",
                            "--projection-reserve-seconds", "0"], environ={})
        self.assertEqual(code, 3)
        report = json.loads(buffer.getvalue())
        self.assertFalse(report["eval"]["product"]["available"])
        self.assertFalse(report["eval"]["complete"])
        self.assertEqual(report["counts"]["items"], 28)

    def test_a_deadline_passed_before_any_find_is_reported_as_budget_exhausted(self):
        # The build overran the run deadline and nothing was sampled: no find saw it.
        now = [0.0]
        deadline = ev.Deadline(5, monotonic=lambda: now[0])
        drawn = sample(Sources(PortalReads(dev.portal_port(type("C", (), {
            "read_relation": staticmethod(lambda *a, **k: build_world().read_relation(
                a[0], k["columns"]))})(), ev.Deadline(0).guard)), None),
            run_id="seed-a", count=28)
        now[0] = 6.0
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={},
                             deadline=deadline)
        self.assertIn("run_budget_exhausted", report["eval"]["incomplete"])
        self.assertFalse(report["eval"]["complete"])
        self.assertEqual(ev.verdict(report), 3)

    def test_the_budget_guard_stops_oracle_reads_as_failures_not_absence(self):
        world = build_world()
        deadline = ev.Deadline(0)
        port = dev.portal_port(type("C", (), {"read_relation": staticmethod(
            lambda *a, **k: world.read_relation(a[0], k["columns"]))})(), deadline.guard)
        drawn = sample(Sources(PortalReads(port), None), run_id="seed-a", count=28)
        self.assertEqual(drawn.items, [])
        self.assertTrue(drawn.failures)
        self.assertEqual(set(drawn.failures.values()), {"BudgetExhausted"})
        self.assertEqual(drawn.kinds["sales_order"]["status"], "unavailable")

    def test_a_document_search_failure_gets_the_label_a_relation_read_gets(self):
        from evals.oracle.source import ReadFailed

        class Coded(Exception):
            error_code = "rate_limited"

        class GatewayTimeout(Exception):
            status = 504

        for exc, label in ((Coded(), "error_rate_limited"),
                           (GatewayTimeout(), "http_504")):
            failed = ReadFailed.of("t", exc)
            self.assertEqual(failed.label(), label)
            drawn, _, _ = fixture_sample()
            kind = DOCUMENT_KINDS[0]
            drawn.kinds[kind] = {"status": "unavailable", "reason": failed.reason,
                                 **failed.fields()}
            self.assertEqual(ev.read_failure_causes(drawn)[kind], {label: 1})

    def test_read_failures_in_the_report_are_bounded_with_a_total(self):
        drawn = sample(Sources(PortalReads(dev.portal_port(type("C", (), {
            "read_relation": staticmethod(lambda *a, **k: build_world().read_relation(
                a[0], k["columns"]))})(), ev.Deadline(0).guard)), None),
            run_id="seed-a", count=28)
        drawn.failures.update({f"public.rel_{i:02d}": "HTTPError" for i in range(25)})
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={})
        self.assertEqual(len(report["read_failures"]), ev.MAX_READ_FAILURES)
        self.assertEqual(report["read_failures_total"], len(drawn.failures))
        self.assertGreater(report["read_failures_total"], ev.MAX_READ_FAILURES)

    def test_failed_reads_name_their_typed_cause_per_source_and_the_document_funnel(self):
        class Forbidden(Exception):
            status = 403

        world = build_world()
        first, second = sorted(K.RELATIONS)[:2]
        failing = {first: Forbidden(), second: TimeoutError()}

        def read(relation, columns):
            if relation in failing:
                raise failing[relation]
            return world.read_relation(relation, columns)

        reads = PortalReads(dev.portal_port(type("C", (), {"read_relation": staticmethod(
            lambda *a, **k: read(a[0], k["columns"]))})()))
        drawn = sample(Sources(reads, None), run_id="seed-a", count=28)
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={})
        causes = report["read_failure_causes"]
        self.assertEqual(causes[ev.public_source(first)], {"http_403": 1})
        self.assertEqual(causes[ev.public_source(second)], {"timeout": 1})
        self.assertIn("source_read_failures", report["eval"]["incomplete"])

        fixture, _, _ = fixture_sample()
        ok = ev.run_eval(fixture, ev.Product(None, {"status": "not_configured"}),
                         run_id="r", evidence_label="fixture", configured={})
        self.assertNotIn("read_failure_causes", ok)
        self.assertEqual(set(ok["document_funnel"]), set(DOCUMENT_KINDS))
        for kind, funnel in ok["document_funnel"].items():
            self.assertEqual(set(funnel), {"listed", "read", "classified", "candidates"})
            self.assertEqual(funnel["listed"], fixture.kinds[kind]["search"]["documents"])
            self.assertEqual(funnel["candidates"], fixture.kinds[kind]["candidates"])
            self.assertGreater(funnel["listed"], 0)

    def test_min_scored_above_count_is_rejected_before_any_work(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                ev.main(["fixture", "--count", "10"], environ={})
        self.assertEqual(raised.exception.code, 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIn(ev.main(["fixture", "--count", "10", "--min-scored", "5"],
                                  environ={}), (0, 3))


class CrossKindUniquenessTest(unittest.TestCase):
    """What the oracle accepts as one record must not depend on what the product holds."""

    def collision_world(self):
        from evals.oracle.generator import PORTAL_STRATA
        world = build_world()
        (ao_rel, ao_col), = PORTAL_STRATA["assembly_order"][1]
        (po_rel, po_col), = PORTAL_STRATA["purchase_order"][1]
        token = key_text(world.relations[ao_rel][0][ao_col])
        world.relations[po_rel][0][po_col] = token   # one token names an AO and a PO
        world.docs["mail"] = [{"handle": "graph-mail://fx-box/collide@c1", "texts": [token]}]
        world.docs["sharepoint"] = []
        return world, token

    def mail_items(self, record_kinds):
        world, token = self.collision_world()
        sources = Sources(PortalReads(world.read_relation), None)
        drawn = sample(sources, run_id="seed-a", count=28, documents=world.documents(),
                       record_kinds=record_kinds)
        return drawn, token

    def test_a_token_naming_a_held_and_an_unheld_kind_is_not_unique_either_way(self):
        everything, token = self.mail_items(None)
        held_only, _ = self.mail_items(["assembly_order"])
        for drawn in (everything, held_only):
            self.assertEqual([i for i in drawn.items if i.kind == "mail"], [])
            self.assertEqual(drawn.kinds["mail"]["skipped"].get("not_unique"), 1)

    def test_undrawn_kinds_are_indexed_but_never_become_items(self):
        drawn, _ = self.mail_items(["assembly_order"])
        self.assertEqual(drawn.kinds["purchase_order"]["status"], "not_sampled")
        self.assertGreater(drawn.kinds["purchase_order"]["roots"], 0)
        self.assertEqual({i.root_kind for i in drawn.items} - {"assembly_order"}, set())


class BuildShareAndCoverageTest(unittest.TestCase):
    """The product build keeps its share of the run budget; sampling follows the product."""

    def test_the_build_gets_its_share_whatever_the_run_has_left(self):
        seen = []

        def fake_build(client, directory, relations, *, budget_seconds, unstable=(),
                       state_dir=None, bc_capture=None, projection_reserve=0.0,
                       run_left=None):
            seen.append((budget_seconds, projection_reserve))
            return None, {"status": "not_built"}, frozenset()

        now = [0.0]
        deadline = ev.Deadline(600, monotonic=lambda: now[0])
        original, ev.build_projection = ev.build_projection, fake_build
        try:
            ev.product_from_build(None, Path("x"), deadline, 120.0, total_seconds=600,
                                  share=0.5, projection_reserve=100.0)
            now[0] = 400.0  # a slow run: less than the share is left after the reserve
            ev.product_from_build(None, Path("x"), deadline, 120.0, total_seconds=600,
                                  share=0.5, projection_reserve=100.0)
        finally:
            ev.build_projection = original
        # the projection's reserve is inside the build's budget, never on top of it
        self.assertEqual(seen, [(300.0, 100.0), (80.0, 100.0)])

    def test_bc_keeps_its_floor_after_a_portal_overrun_but_never_passes_the_deadline(self):
        """Live 2026-10-07: the Portal sync overran its share and BC got ~69 s of 396 s
        (no root held in 3 of 4 runs). BC keeps ``BC_CAPTURE_SHARE`` of the build budget
        whatever the sync took, and gets all that is left after a fast one; the floor
        yields to the run's deadline (fake clock): BC gets only what the deadline leaves
        it (less the projection's reserve), and none when it leaves nothing (a coded
        ``no_budget``, never an overrun). One rule (``bc_budget``) for both builds: this
        eval's projection and the dev baseline's product."""
        from types import SimpleNamespace
        from unittest import mock

        from integrations.workbench_read_api import WorkbenchReadApiClient
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as baseline

        relations = ev.required_relations(load_schema(), ev.root_relations())
        world_fetch = ev._fixture_fetch(build_world())
        floor = 400.0 * ev.BC_CAPTURE_SHARE
        builders = {   # module, reserve kept for the projection, build -> info
            "unfamiliar": (ev, 20.0, lambda client, tmp, bc, left: ev.build_projection(
                client, tmp / "p", relations, budget_seconds=420.0, bc_capture=bc,
                projection_reserve=20.0, run_left=left)[1]),
            "dev_baseline": (baseline, 0.0, lambda client, tmp, bc, left: baseline.build_product(
                client, tmp, relations, budget_seconds=400.0, bc_capture=bc,
                run_left=left).info)}
        for name, (module, reserve, build) in builders.items():
            for sync_seconds, run_seconds, expected in (
                    (1000.0, None, floor), (0.0, None, 400.0),
                    (1000.0, 1050.0 + reserve, 50.0), (1000.0, 950.0 + reserve, None)):
                with self.subTest(build=name, sync=sync_seconds, run=run_seconds):
                    now, granted = [0.0], []

                    def fetch(url, headers, **kwargs):
                        if not now[0]:
                            now[0] += sync_seconds   # the Portal sync's wall time
                        return world_fetch(url, headers, **kwargs)

                    def bc_capture(db, seconds):
                        granted.append(seconds)
                        raise RuntimeError("no BC here")   # reported failed, never raised

                    client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                                    "plxwb_" + "f" * 40, fetch=fetch)
                    deadline = None if run_seconds is None else \
                        ev.Deadline(run_seconds, monotonic=lambda: now[0])
                    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                            module, "time", SimpleNamespace(monotonic=lambda: now[0])):
                        info = build(client, Path(tmp), bc_capture,
                                     deadline and deadline.remaining)
                    self.assertEqual(granted, [] if expected is None else [expected])
                    self.assertEqual(info["bc_capture"],
                                     {"status": "no_budget"} if expected is None else
                                     {"status": "failed", "error": "RuntimeError"})

    def test_a_binding_bc_floor_keeps_the_finds_and_projection_reserves(self):
        """Review 4eb0f8535a28 (a): the BC floor yielded to the run's deadline but not to
        the finds' reserve, so a slow Portal sync let BC run into the finds' time. A run of
        600 s keeping 60 s for the finds and 20 s of the build for the projection: the sync
        takes 500 s, BC's floor (70 s) binds and gets only the 20 s before both reserves."""
        from types import SimpleNamespace
        from unittest import mock

        from integrations.workbench_read_api import WorkbenchReadApiClient

        world_fetch = ev._fixture_fetch(build_world())
        now, granted = [0.0], []

        def fetch(url, headers, **kwargs):
            if not now[0]:
                now[0] = 500.0   # the Portal sync's wall time
            return world_fetch(url, headers, **kwargs)

        def bc_capture(db, seconds):
            granted.append(seconds)
            raise RuntimeError("no BC here")   # reported failed, never raised

        client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                        "plxwb_" + "f" * 40, fetch=fetch)
        deadline = ev.Deadline(600, monotonic=lambda: now[0])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                ev, "time", SimpleNamespace(monotonic=lambda: now[0])):
            ev.product_from_build(client, Path(tmp), deadline, 60.0, total_seconds=600,
                                  share=0.5, bc_capture=bc_capture,
                                  projection_reserve=20.0).close()
        self.assertEqual(granted, [600 - 500 - 60 - 20])

    def test_a_state_dir_makes_the_next_build_incremental_and_survives_a_portal_outage(self):
        from integrations.http import SourceHttpError
        from unittest import mock

        from integrations import workbench_read_api
        from integrations.tests import test_bc_capture as tb
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from ontology.schema import load_schema
        from ontology.tests.test_project_bc import CAPTURE_ENTITY_SETS, purchase_world

        schema = load_schema()
        relations = sorted(set(ev.required_relations(schema, ev.root_relations()))
                           | set(ev.process_relations(schema)))

        def client(fetch):
            return WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                          "plxwb_" + "f" * 40, fetch=fetch)

        def portal_500(url, headers, **_):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=500)

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            (state / "bc-capture").mkdir(parents=True)   # the host's BC capture
            tb.Harness(self, purchase_world(), db=state / "bc-capture" / "capture.sqlite"
                       ).run(entity_sets=CAPTURE_ENTITY_SETS)
            healthy = client(ev._fixture_fetch(build_world()))
            runs = [ev.build_projection(healthy, Path(tmp) / "unused", relations,
                                        budget_seconds=None, state_dir=state)
                    for _ in range(2)]
            for store, info, complete in runs:
                self.assertEqual(info["product_state"], "persistent")
                self.assertEqual(info["captures"], ["bc", "portal_api"])
                self.assertTrue(complete)
            self.assertEqual(runs[0][1]["build"]["mode"], "cold")
            second = runs[1][1]["build"]
            self.assertEqual(second["mode"], "incremental", second)
            self.assertLess(second["units_rebuilt"], second["units"])
            self.assertFalse((Path(tmp) / "unused").exists())

            # Portal down: the product is still built (BC held), the Portal is never held
            # from what the mirror kept, and every Portal relation is blocked upstream.
            with mock.patch.object(workbench_read_api, "get_json", portal_500):
                store, info, complete = ev.build_projection(
                    WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                           "plxwb_" + "f" * 40),
                    Path(tmp) / "unused", relations, budget_seconds=None, state_dir=state)
            self.assertEqual(info["portal"], {"status": "source_unavailable",
                                              "error_code": "source_unavailable",
                                              "http_status": 500})
            self.assertEqual(complete, frozenset())
            product = ev.open_product(store, info=info, complete_relations=complete,
                                      upstream_failed=info["upstream_failed"])
            self.addCleanup(product.close)
            self.assertTrue(product.available)
            self.assertTrue(any(k.startswith("bc:") for k in product.audience_keys))
            self.assertFalse(any(product.holds_relation(r) for r in relations))
            self.assertEqual(product.upstream_block([relations[0]]),
                             {"blocked": "upstream", "error_code": "source_unavailable",
                              "http_status": 500})

        # Without a state dir the build is for this run alone, and says so.
        with tempfile.TemporaryDirectory() as tmp:
            _, info, _ = ev.build_projection(healthy, Path(tmp) / "p", relations,
                                             budget_seconds=None)
        self.assertEqual((info["product_state"], info["build"]["mode"]), ("per_run", "cold"))

    def test_a_build_that_never_ran_is_not_built_not_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            # the projection's reserve leaves the sync nothing: the build never runs
            store, info, complete = ev.build_projection(None, Path(tmp) / "p", ["a.b"],
                                                        budget_seconds=100,
                                                        projection_reserve=120)
        self.assertIsNone(store)
        self.assertEqual((info["status"], info["reason"]), ("not_built", "no_budget_for_build"))
        drawn, _, _ = fixture_sample()
        deadline = ev.Deadline(600)
        report = ev.run_eval(drawn, ev.Product(None, info), run_id="seed-a",
                             evidence_label="injected-transport", configured={},
                             deadline=deadline)
        incomplete = report["eval"]["incomplete"]
        self.assertIn("product_not_built", incomplete)
        self.assertNotIn("run_budget_exhausted", incomplete)
        self.assertNotIn("mirror_incomplete", incomplete)  # no sync ran
        self.assertEqual(report["eval"]["product"]["status"], "not_built")
        self.assertEqual(ev.verdict(report), 3)

    def test_roots_of_a_kind_the_product_does_not_hold_are_not_scored_excluded(self):
        sources, documents, _, world = dev.build_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            held = ev.fixture_product(world, Path(tmp) / "p")
            self.addCleanup(held.close)
            (po, _), = ev.PORTAL_STRATA["purchase_order"][1]
            self.assertIn(po, held.complete_relations)
            product = ev.Product(held.call, held.info, audience_keys=held.audience_keys,
                                 complete_relations=held.complete_relations - {po})
            kinds = product.held_record_kinds()
            self.assertNotIn("purchase_order", kinds)
            self.assertNotIn("bc_invoice", kinds)  # a Portal-only build holds no BC unit
            drawn = sample(sources, run_id="seed-a", count=28, documents=documents,
                           record_kinds=kinds)
            report = ev.run_eval(drawn, product, run_id="seed-a",
                                 evidence_label="injected-transport", configured={})
        block = report["eval"]
        # The three-way kind's roots are Portal purchase orders: not held either.
        self.assertNotIn(THREE_WAY, kinds)
        self.assertEqual(block["kinds_not_held"], ["bc_invoice", THREE_WAY, "purchase_order"])
        self.assertEqual(report["kinds"]["purchase_order"]["status"], "not_sampled")
        self.assertNotIn("product_not_holding_root", block["excluded"])
        self.assertFalse([i for i in report["items"] if i["kind"] == "purchase_order"])
        self.assertEqual(report["counts"]["items"], 28)  # its quota went to the other kinds
        self.assertEqual(block["scored"], report["counts"]["items"] - report["counts"]["excluded"])

    def test_an_unreadable_document_kind_publishes_what_it_tried_never_empty(self):
        sources, _, _, world = dev.build_fixture()

        class Withheld(Unnamed):
            def listing(self, kind):
                return {"withheld": 2, "source": [f"fixture:{kind}"]}

        with tempfile.TemporaryDirectory() as tmp:
            held = ev.fixture_product(world, Path(tmp) / "p")
            self.addCleanup(held.close)
            drawn = sample(sources, run_id="seed-a", count=28, documents=Withheld())
            report = ev.run_eval(drawn, held, run_id="seed-a",
                                 evidence_label="injected-transport", configured={})
        for kind in ("mail", "sharepoint"):
            entry = report["kinds"][kind]
            self.assertEqual((entry["status"], entry["reason"], entry["withheld"],
                              entry["tried"]),
                             ("unreadable", "withheld", 2, [f"fixture:{kind}"]))
        self.assertNotIn("tried", report["kinds"]["sales_order"])  # a read kind: counts only

    def test_sampling_stops_at_the_reserve_so_the_finds_keep_their_share(self):
        now = [0.0]
        deadline = ev.Deadline(600, monotonic=lambda: now[0])
        guard = ev.sampling_guard(deadline, 120.0)
        slow_reads = 0
        with self.assertRaises(ev.BudgetExhausted):
            while True:          # a slow sampler: each oracle read takes 50 s
                guard()
                now[0] += 50.0
                slow_reads += 1
        self.assertEqual(slow_reads, 10)
        self.assertEqual(deadline.remaining(), 100.0)   # about the finds' reserve is left
        self.assertFalse(deadline.expired())

    def test_a_kind_the_product_does_not_hold_fails_as_not_held_never_skipped(self):
        drawn, _, _ = fixture_sample()
        answers = {i.identifier: [root_handle(i)] for i in drawn.items}
        product = holding(ScriptedFind(answers), drawn.items)
        keep = drawn.items[0].kind
        product.holds = lambda item: item.kind == keep
        report = ev.run_eval(drawn, product, run_id="seed-a",
                             evidence_label="injected-transport", configured={})
        self.assertEqual(report["eval"]["scored_kinds"], 1)
        self.assertEqual(report["eval"]["kinds_missing"], [k for k in KINDS if k != keep])
        for kind in report["eval"]["kinds_missing"]:
            entry = report["eval"]["by_kind"][kind]
            self.assertEqual((entry["status"], entry["reason"], entry["scored"]),
                             ("unscored", "product_not_holding_root", 0), kind)
        self.assertEqual(ev.verdict(report, min_scored=1), 3)

    def test_a_product_with_bc_units_keeps_bc_invoice_and_no_product_means_no_filter(self):
        self.assertIsNone(ev.Product(None, {}).held_record_kinds())
        product = ev.Product(lambda *a: {}, {}, audience_keys={"bc:c/salesinvoices"})
        self.assertEqual(product.held_record_kinds(), ["bc_invoice"])

    def test_the_bc_probe_budget_is_shared_across_kinds(self):
        sources, documents, _, _ = dev.build_fixture()
        drawn = sample(sources, run_id="seed-a", count=28, documents=documents,
                       bc_finds_total=6)
        used = {k: v["bc_finds"] for k, v in drawn.kinds.items() if "bc_finds" in v}
        self.assertLessEqual(sum(used.values()), 6)
        self.assertGreater(sum(1 for n in used.values() if n), 1)  # not one kind's alone
        first = next(v for v in drawn.kinds.values() if "bc_finds_allotted" in v)
        self.assertLess(first["bc_finds_allotted"], 6)
        self.assertGreater(sum(v.get("skipped", {}).get("bc_probe_budget", 0)
                               for v in drawn.kinds.values()), 0)


class EveryKindRuleTest(unittest.TestCase):
    """The check passes only when each kind (eight, with the
    three-way kind) has a scored passing item; a kind without one fails, named, never
    silently."""

    def setUp(self):
        self.drawn, _, _ = fixture_sample()

    def test_every_kind_passing_passes_the_command_and_the_spec(self):
        self.assertEqual({i.kind for i in self.drawn.items}, set(KINDS))
        report = answered(self.drawn)
        block = report["eval"]
        self.assertEqual(block["kinds_missing"], [])
        self.assertEqual(len(KINDS), 8)
        self.assertEqual((block["kinds_found_exact"], block["kinds_required"]), (8, 8))
        for kind in KINDS:
            entry = block["by_kind"][kind]
            self.assertEqual(entry["status"], "passed", kind)
            self.assertGreaterEqual(entry[passing(kind)], 1, kind)
            self.assertEqual(entry["items"], self.drawn.kinds[kind]["items"])
            self.assertEqual(entry["candidates"], self.drawn.kinds[kind]["candidates"])
            self.assertGreater(entry["candidates"], 0)
            self.assertEqual(entry["scored"], entry["items"] - entry.get("excluded", 0))
            self.assertEqual(entry["scored"], sum(entry.get(o, 0) for o in outcomes(kind)))
        self.assertEqual(ev.verdict(report), 0)
        self.assertEqual(spec_failures(report), [])

    def test_one_kind_short_fails_the_command_and_the_spec_naming_the_missing_kind(self):
        for missing in KINDS:
            report = answered(self.drawn, unanswered=(missing,))
            block = report["eval"]
            self.assertEqual(block["kinds_missing"], [missing])
            self.assertEqual(block["kinds_found_exact"], 7)
            entry = block["by_kind"][missing]
            if missing == THREE_WAY:   # plx.find cannot resolve the root: no process asked
                self.assertEqual((entry["status"], entry.get("three_way_correct", 0)),
                                 ("no_correct_answer", 0))
                self.assertEqual(entry["root_not_resolved"], entry["scored"])
            else:
                self.assertEqual((entry["status"], entry.get("found_exact", 0)),
                                 ("no_found_exact", 0))
                self.assertEqual(entry["not_found"], entry["scored"])
            self.assertGreaterEqual(block["scored"], 20)
            self.assertEqual(ev.verdict(report), 3, missing)
            self.assertEqual(block["fail_reasons"], ["kinds_failing"])
            self.assertEqual(spec_failures(report),
                             ["exit_code 3 != 0", "eval.fail_reasons not empty"])

    def test_a_kind_whose_searches_name_nothing_names_its_source_and_fails(self):
        sources, _, _, world = dev.build_fixture()

        class NoDocuments(Unnamed):
            def listing(self, kind):
                return {"source": [f"fixture:{kind}-folder"]}

        drawn = sample(sources, run_id="seed-a", count=28, documents=NoDocuments())
        report = answered(drawn)
        block = report["eval"]
        for kind in ("mail", "sharepoint"):
            entry = block["by_kind"][kind]
            self.assertEqual((entry["status"], entry["candidates"], entry["items"],
                              entry["reason"], block["sources_tried"][kind]),
                             ("no_candidate_in_sample", 0, 0, "no_document_names_root",
                              [f"fixture:{kind}-folder"]))
            # no items: why and what it read, never zero outcome counts or a 0/0 rate
            self.assertFalse({"found_exact", "scored", "pass"} & set(entry))
        self.assertEqual(block["kinds_missing"], ["mail", "sharepoint"])
        self.assertGreaterEqual(block["scored"], 20)   # the other kinds took the quota
        self.assertEqual(ev.verdict(report), 3)
        self.assertEqual(block["fail_reasons"], ["kinds_failing"])
        self.assertIn("eval.fail_reasons not empty", spec_failures(report))

    def test_a_record_kind_with_no_rows_is_empty_with_the_relations_it_tried(self):
        from evals.oracle.generator import PORTAL_STRATA, record_tried
        world = build_world()
        for relation, _ in PORTAL_STRATA["assembly_order"][1]:
            world.relations[relation] = []
        from evals.oracle.source import BcReads
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        report = answered(sample(sources, run_id="seed-a", count=28,
                                 documents=world.documents()))
        entry = report["eval"]["by_kind"]["assembly_order"]
        self.assertEqual((entry["status"], entry["candidates"], entry["reason"],
                          report["eval"]["sources_tried"]["assembly_order"]),
                         ("empty", 0, "no_roots", record_tried("assembly_order")))
        self.assertIn("assembly_order", report["eval"]["kinds_missing"])
        self.assertEqual(ev.verdict(report), 3)

    def test_portal_non_2xx_blocks_the_portal_kinds_upstream_and_fails_without_a_crash(self):
        from evals.oracle.generator import PORTAL_STRATA
        from integrations import workbench_read_api
        from integrations.http import SourceHttpError

        def portal_500(url, headers, **_):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=500)

        original = workbench_read_api.get_json
        workbench_read_api.get_json = portal_500
        out, err = io.StringIO(), io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = ev.main(["live", "--no-bc", "--build-store", str(Path(tmp) / "p"),
                                "--run-id", "seed-a"],
                               environ={"PLX_WORKBENCH_API_URL": "https://portal.invalid",
                                        "PLX_WORKBENCH_API_KEY": "plxwb_" + "f" * 40})
        finally:
            workbench_read_api.get_json = original
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(code, 3)
        report = json.loads(out.getvalue())   # still a valid report
        block = report["eval"]
        self.assertEqual(report["product"]["status"], "source_unavailable")
        self.assertFalse(block["product"]["available"])
        self.assertIn("product_source_unavailable", block["incomplete"])
        portal_kinds = sorted(PORTAL_STRATA)
        self.assertEqual(len(portal_kinds), 4)
        for kind in portal_kinds + [THREE_WAY]:
            entry = block["by_kind"][kind]
            # blocked, with its share of the sample counted below (never 0, never dropped);
            # the Portal's failure is named here and in eval.product, not per source
            self.assertEqual((entry["status"], entry["blocked"], entry["http_status"]),
                             ("blocked", "upstream", 500), kind)
            self.assertNotIn(kind, block.get("sources_tried", {}))
            self.assertIn(kind, block["kinds_missing"])
        self.assertEqual(block["blocked"]["kinds_quota"], 15)
        self.assertEqual((block["find_root"]["blocked_upstream"],
                          block["answer"]["blocked_upstream"]), (12, 3))
        self.assertEqual(block["find_root"]["causes"], {"upstream_outage": 12})
        self.assertEqual(block["kinds_found_exact"], 0)
        self.assertEqual(report["row_data_check"]["leaked"], 0)
        # Nothing else was captured: measured under the outage rule, and it fails.
        self.assertEqual(block["pass_rule"], "portal_outage")
        self.assertEqual(block["fail_reasons"], ["product_unavailable", "no_bc_rooted_scored"])
        failures = spec_failures(report, code)
        self.assertIn("exit_code 3 != 0", failures)
        self.assertIn("eval.fail_reasons not empty", failures)
        self.assertTrue(recorded_whole(report, failures))

    def test_a_budget_stop_is_blocked_budget_not_upstream(self):
        world = build_world()
        port = dev.portal_port(type("C", (), {"read_relation": staticmethod(
            lambda *a, **k: world.read_relation(a[0], k["columns"]))})(),
            ev.Deadline(0).guard)
        drawn = sample(Sources(PortalReads(port), None), run_id="seed-a", count=28)
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="injected-transport", configured={})
        entry = report["eval"]["by_kind"]["sales_order"]
        self.assertEqual((entry["status"], entry["blocked"]), ("blocked", "budget"))


def fixture_bc_sender(world):
    """The BC API v2.0 fake of ``integrations/tests/test_bc_capture.py`` serving the
    fixture world's BC companies, documents and lines, so the product's own BC capture
    reads the records the oracle samples (injected transport, GET only)."""
    from evals.oracle.fixture_world import COMPANIES
    from integrations.bc_read import CAPTURE_ENTITY_SETS
    from integrations.live_source_grammar import BC_DOCUMENT_LINES
    from integrations.tests.test_bc_capture import FakeBc

    fake = FakeBc({s: 0 for s in CAPTURE_ENTITY_SETS}, companies=[c for c, _ in COMPANIES])
    for entity_set, records in world.bc.items():
        for record in records:
            fake.data[(record["company_id"], entity_set)].append({
                "@odata.etag": f'W/"{record["id"][-6:]}"',
                "lastModifiedDateTime": "2026-09-30T10:00:00Z", **record["fields"]})
            if entity_set in BC_DOCUMENT_LINES:
                fake.lines[(record["company_id"], entity_set, record["id"])] = [
                    {"@odata.etag": f'W/"{line["id"][-6:]}"', **line["fields"]}
                    for line in world.bc_lines.get(record["id"], [])]
    books = {"production": {COMPANIES[0][0]: "fx_one", COMPANIES[1][0]: "fx_two"}}
    return fake, books


class PortalOutageTest(unittest.TestCase):
    """With the Portal down (HTTP 500 on every route) the
    product captures BC itself, projects it by generation and answers BC-rooted items
    through the case host's tools; the Portal strata are blocked upstream with their
    counts. Green means every kind passed (rule every_kind): a run that meets the outage
    rule (BC-rooted items scored, no asked kind failing) reads blocked upstream_outage,
    never pass, and one that does not fails (gap-51ebb6c78e90)."""

    def assert_reads_blocked_by_the_outage(self, report: dict) -> None:
        block = report["eval"]
        self.assertEqual(block["pass_rule"], "portal_outage")
        self.assertEqual((block["fail_reasons"], block["blocked_cause"]), ([], "upstream_outage"))
        self.assertEqual(ev.verdict(report), 0)
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        self.assertEqual(live_verify.resolve(report, spec["blocked_when"]), ["upstream_outage"])
        self.assertEqual(spec["blocked_causes"]["upstream_outage"], "upstream_outage")
        # without its cause the spec still refuses it: green is pinned to every_kind and
        # seven kinds found exactly, never the outage rule's one scored BC-rooted item
        unblocked = json.loads(json.dumps(report))
        del unblocked["eval"]["blocked_cause"]
        failures = spec_failures(unblocked)
        self.assertIn("eval.pass_rule != expected", failures)
        self.assertIn("eval.kinds_found_exact < 7", failures)

    def outage_report(self, error_code: str | None = None, wrong: bool = False,
                      naming_documents: bool = True) -> dict:
        """One run with the Portal answering 500 on every route (``error_code``: the coded
        ``{error: {code}}`` form) and the product capturing BC itself; ``wrong``: plx.find's
        candidates never carry the root (every scored find is wrong); without
        ``naming_documents`` mail and SharePoint name no BC invoice."""
        from integrations.http import SourceHttpError
        from integrations.tests.test_live_source_adapters import ENV
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from evals.oracle import BcReads

        def portal_500(url, headers, **_):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=500,
                                  error_code=error_code)

        world = build_world()
        fake, books = fixture_bc_sender(world)
        portal = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                        "plxwb_" + "f" * 40, fetch=portal_500)
        # Mail and SharePoint name some sampled BC invoices (the Portal side is unreadable,
        # so a document is the other system an identifier comes from).
        numbers = [r["fields"]["number"] for s in ("salesInvoices", "purchaseInvoices")
                   for r in world.bc[s][:5]]

        class Documents:
            def sample(self, kind, limit, **_):
                return {"records": [{"handle": f"fixture:{kind}/{n}",
                                     "texts": [f"Invoice {number} attached"]}
                                    for n, number in enumerate(numbers)
                                    if naming_documents and (n % 2 == 0) == (kind == "mail")],
                        "complete": True, "source": [f"fixture:{kind}"]}

        with tempfile.TemporaryDirectory() as tmp:
            product = ev.product_from_build(
                portal, Path(tmp) / "product", ev.Deadline(600), 0, total_seconds=600,
                bc_capture=lambda db, seconds: ev.capture_bc(ENV, db, seconds, sender=fake,
                                                             books=books))
            self.addCleanup(product.close)
            # the spec's run answers from the session projection, built the same way
            # (SessionProjectionTest); the spec pins its source
            product.info["source"] = "session_projection"
            self.assertTrue(product.available)
            self.assertEqual(product.info["captures"], ["bc", "portal_api"])
            self.assertEqual(product.info["bc_capture"]["status"], "captured")
            self.assertTrue(fake.bc_gets("/salesInvoices"))   # the fake refuses non-GETs
            if wrong:
                product.identities = lambda candidate: {("bc:salesInvoices", "elsewhere")}
            drawn = sample(Sources(PortalReads(dev.portal_port(portal)),
                                   BcReads(world.bc_port())),
                           run_id="seed-a", count=28, documents=Documents(),
                           record_kinds=product.held_record_kinds())
            return ev.run_eval(drawn, product, run_id="seed-a",
                               evidence_label="injected-transport", configured={}, count=28)

    def test_portal_500_scores_bc_rooted_items_from_the_products_own_bc_capture(self):
        report = self.outage_report()
        block = report["eval"]
        self.assertEqual(block["pass_rule"], "portal_outage")
        for kind in ("sales_order", "purchase_order", "assembly_order", "receipt", THREE_WAY):
            entry = block["by_kind"][kind]
            self.assertEqual((entry["status"], entry["blocked"], entry["http_status"]),
                             ("blocked", "upstream", 500), kind)
        self.assertEqual(block["blocked"]["kinds_quota"], 15)
        self.assertEqual((block["find_root"]["blocked_upstream"],
                          block["answer"]["blocked_upstream"]), (12, 3))
        # BC-rooted items were asked through plx.find on the projection of the capture
        bc_rows = [r for r in report["items"] if r["root_kind"] == "bc_invoice"]
        self.assertTrue(bc_rows)
        self.assertTrue(all(r["path"] == "find" for r in bc_rows))
        rooted = block["bc_rooted"]
        self.assertEqual(rooted["sampled"], len(bc_rows))
        self.assertGreaterEqual(rooted.get("correct", 0), 1)
        self.assertEqual(rooted["scored"], sum(rooted.get(k, 0) for k in (
            "correct", "wrong", "abstain_ok", "missed")))
        self.assertIsInstance(block["product"]["projection_seconds"], float)
        self.assertEqual(block["product"]["bc_capture"], "captured")
        product = block["product"]
        self.assertEqual(product["bc_units_reached"], product["bc_units_complete"])
        self.assertGreater(product["bc_units_reached"], 0)
        self.assertIsInstance(product["bc_seconds"], float)
        self.assertNotIn("bc_stopped_reason", product)
        self.assertEqual(report["row_data_check"]["leaked"], 0)
        self.assertEqual(ev.verdict(report, min_items=25, min_scored=20), 0)
        self.assertTrue(recorded_whole(report, []))
        self.assert_reads_blocked_by_the_outage(report)
        # one scored BC-rooted item alone is no more than blocked either
        one = json.loads(json.dumps(report))
        one["eval"]["bc_rooted"].update(sampled=1, scored=1, correct=1)
        self.assertEqual(ev.fail_reasons(one), [])
        self.assertEqual(ev.blocked_cause(one["eval"]), "upstream_outage")

    def test_portal_500_draws_bc_invoice_references_from_the_invoices_own_carriers(self):
        """Live 4f2ed1ed/67d8d678: BC answered, the Portal was down and no document named a
        sampled invoice, so every BC invoice reference (all edges join Portal records) was
        unreadable and bc_invoice was empty no_reference. The invoice's own copy of another
        system's identifier (the vendor's invoice number) is now the reference; numbers
        that name an order first (externalDocumentNumber, orderNumber) are not."""
        report = self.outage_report(naming_documents=False)
        block = report["eval"]
        entry = block["by_kind"]["bc_invoice"]
        self.assertEqual(entry["status"], "passed")
        self.assertGreaterEqual(entry["references"], 1)
        bc_rows = [r for r in report["items"] if r["root_kind"] == "bc_invoice"]
        self.assertEqual({(r["kind"], r["reference"], r["reference_system"]) for r in bc_rows},
                         {("bc_invoice", "receipt.bc_purchase_invoice.carried", "portal")})
        self.assertGreaterEqual(block["bc_rooted"]["scored"], 1)
        self.assertEqual(block["bc_rooted"].get("wrong", 0), 0)
        self.assertGreaterEqual(report["row_data_check"]["values_checked"], 1)
        self.assertEqual(report["row_data_check"]["leaked"], 0)
        self.assert_reads_blocked_by_the_outage(report)

    def test_a_failing_coded_outage_run_keeps_its_record_whole(self):
        """The largest outage record: a coded Portal error on every blocked kind, wrong
        answers and failing kinds; the verifier still keeps every recorded field."""
        report = self.outage_report(error_code="internal", wrong=True)
        block = report["eval"]
        self.assertEqual(block["by_kind"]["sales_order"]["error_code"], "internal")
        self.assertEqual(block["fail_reasons"], ["kinds_failing", "wrong_above_bound"])
        self.assertNotIn("blocked_cause", block)   # a failing outage run fails
        self.assertEqual(ev.verdict(report), 3)
        failures = spec_failures(report)
        self.assertIn("eval.fail_reasons not empty", failures)
        self.assertTrue(recorded_whole(report, failures))
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        kept = live_verify._cap({"fields": live_verify._recorded(
            spec, dict(report, evidence_label="live-read")), "failures": failures})
        self.assertEqual(kept["fields"]["eval"]["fail_reasons"], block["fail_reasons"])
        self.assertEqual(kept["fields"]["eval"]["bc_rooted"], block["bc_rooted"])


class UpstreamBlockTest(unittest.TestCase):
    """A Portal failure during the run: every touched item and kind is blocked upstream,
    with its HTTP status and error code, never scored wrong and never dropped."""

    def test_a_relation_the_portal_fails_mid_sync_blocks_its_items_never_scores_them(self):
        from integrations.http import SourceHttpError
        from integrations.workbench_read_api import WorkbenchReadApiClient

        world = build_world()
        fetch = ev._fixture_fetch(world)
        failing = f"/v1/relations/{quote(K.MO, safe='')}/rows"

        def mid_sync(url, headers, **kwargs):
            if failing in url or f"/v1/relations/{K.MO}/rows" in url:
                raise SourceHttpError("read-only source GET failed: HTTPError", status=403,
                                      error_code="relation_forbidden")
            return fetch(url, headers, **kwargs)

        client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                        "plxwb_" + "f" * 40, fetch=mid_sync)
        with tempfile.TemporaryDirectory() as tmp:
            product = ev.product_from_build(client, Path(tmp) / "product", ev.Deadline(600),
                                            0.0, total_seconds=600)
            self.addCleanup(product.close)
            self.assertTrue(product.available)   # the other relations were synced
            self.assertEqual(product.upstream_failed[K.MO],
                             {"http_status": 403, "error_code": "relation_forbidden"})
            # The oracle's own reads succeed (the outage hit the sync only); the kind is
            # still sampled, so its items are reported, not silently not_held.
            self.assertIn("assembly_order", product.held_record_kinds())
            sources = Sources(PortalReads(world.read_relation), None)
            drawn = sample(sources, run_id="seed-a", count=28, documents=world.documents(),
                           record_kinds=product.held_record_kinds())
            report = ev.run_eval(drawn, product, run_id="seed-a",
                                 evidence_label="injected-transport", configured={})
        rows = report["items"]
        touched = [r for r, i in zip(rows, drawn.items) if i.relation == K.MO]
        self.assertTrue(touched)
        for row in touched:
            self.assertEqual((row["outcome"], row["blocked"], row["http_status"],
                              row["error_code"]),
                             ("blocked", "upstream", 403, "relation_forbidden"))
        self.assertFalse([r for r, i in zip(rows, drawn.items)
                          if i.relation != K.MO and r["outcome"] == "blocked"])
        block = report["eval"]
        entry = block["by_kind"]["assembly_order"]
        self.assertEqual((entry["status"], entry["blocked"], entry["http_status"],
                          entry["error_code"], entry["scored"], entry.get("wrong", 0),
                          entry.get("not_found", 0), entry["blocked_items"]),
                         ("blocked", "upstream", 403, "relation_forbidden", 0, 0, 0,
                          entry["items"]))
        self.assertEqual(report["counts"]["blocked"], len(touched))
        self.assertEqual(block["blocked"]["items"], len(touched))
        # Items and kinds are tallied apart (review em-1): never one mixed count.
        self.assertEqual(block["blocked"]["items_http_status"], {"403": len(touched)})
        self.assertEqual(block["blocked"]["kinds_http_status"], {"403": 1})
        self.assertEqual(block["blocked"]["items_by_cause"], {"upstream": len(touched)})
        self.assertEqual(block["blocked"]["kinds"], 1)
        self.assertNotIn("http_status", block["blocked"])
        self.assertNotIn("product_sync_failed", block["excluded"])   # apart from exclusions
        self.assertEqual(block["scored"], len(rows) - report["counts"]["excluded"]
                         - len(touched))
        self.assertIn("items_blocked_upstream", block["incomplete"])
        self.assertEqual(block["product"]["relations_upstream_failed"], 1)
        self.assertIn("assembly_order", block["kinds_missing"])   # blocked never passes
        self.assertEqual(report["row_data_check"]["leaked"], 0)

    def test_a_failed_sync_blocks_every_item_with_the_portal_status(self):
        drawn, _, _ = fixture_sample()
        product = ev.Product(None, {"status": "source_unavailable",
                                    "error_code": "source_unavailable", "http_status": 503})
        report = ev.run_eval(drawn, product, run_id="r", evidence_label="injected-transport",
                             configured={})
        self.assertEqual({(r["outcome"], r["blocked"], r["http_status"])
                          for r in report["items"] if r["kind"] != THREE_WAY},
                         {("blocked", "upstream", 503)})
        for kind in KINDS:
            entry = report["eval"]["by_kind"][kind]
            if entry["items"] and kind != THREE_WAY:
                self.assertEqual((entry["status"], entry["blocked"], entry["http_status"]),
                                 ("blocked", "upstream", 503), kind)
        self.assertEqual(report["eval"]["scored"], 0)
        self.assertEqual(ev.verdict(report), 3)

    def test_source_names_survive_the_verifier_and_a_mailbox_is_never_published(self):
        sources, _, _, _ = dev.build_fixture()

        class Unread(Unnamed):
            def listing(self, kind):
                return {"source": ["mail:someone@example.invalid/inbox"] if kind == "mail" else
                        ["sharepoint:/Documents", "sharepoint:/Customer Documents"]}

        report = dict(answered(sample(sources, run_id="seed-a", count=28,
                                      documents=Unread())), evidence_label="live-read")
        tried = report["eval"]["sources_tried"]
        self.assertEqual(tried["mail"], ["mail"])
        self.assertEqual(tried["sharepoint"], ["sharepoint:/Documents", "sharepoint"])
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        failures = spec_failures(report)
        detail = live_verify._cap({"fields": live_verify._recorded(spec, report),
                                   "failures": failures})
        check = {"id": spec["id"], "result": "fail", "detail": detail}
        lifted = live_verify.build_report([], [check], root=ROOT, main_sha="abc",
                                          previous_main_sha=None)["eval"][spec["id"]]
        self.assertEqual(lifted["sources_tried"], tried)
        self.assertNotIn("example.invalid", json.dumps(report["eval"]))


class ProductPathTest(unittest.TestCase):
    """The real plx.find/plx.object over a projection of the fixture world."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.product = ev.fixture_product(build_world(), Path(cls.tmp.name) / "product")
        cls.drawn, _, cls.world = fixture_sample()
        cls.report = ev.run_eval(cls.drawn, cls.product, run_id="seed-a",
                                 evidence_label="injected-transport", configured={})

    @classmethod
    def tearDownClass(cls):
        cls.product.close()
        cls.tmp.cleanup()

    def test_the_case_host_tool_table_serves_find(self):
        self.assertTrue(self.product.available)
        self.assertIn("plx.find", self.product.info["tools"])
        self.assertTrue(self.product.info["mirror_complete"])

    def test_found_roots_are_found_and_uncaptured_roots_are_excluded_not_skipped(self):
        counts = self.report["counts"]
        self.assertEqual(counts["items"], 28)
        self.assertEqual(sum(counts[o] for o in ev.OUTCOMES + ev.THREE_WAY_OUTCOMES)
                         + counts["excluded"], 28)
        self.assertGreater(counts["found_exact"], 0)
        # The fixture projection holds no BC captures: BC-invoice roots are excluded with
        # a reason and a count, and the rate covers the scored items only.
        bc = self.report["eval"]["by_kind"]["bc_invoice"]
        self.assertEqual(bc["pass"], "0/0")
        self.assertEqual(bc["excluded"], self.drawn.kinds["bc_invoice"]["items"])
        block = self.report["eval"]
        self.assertEqual(block["excluded"], {"product_not_holding_root": bc["excluded"]})
        self.assertEqual(block["scored"], 28 - bc["excluded"])
        product = block["product"]
        self.assertTrue(product["available"])
        self.assertGreater(product["object_count"], 0)
        self.assertTrue(product["mirror_complete"])
        # completeness = complete_bound_relation_count/bound_total over the required
        # relations; an unserved required relation is counted, never complete.
        complete, total = map(int, product["completeness"].split("/"))
        info = self.report["product"]["completeness"]
        self.assertEqual((complete, total), (info["complete_bound_relation_count"],
                                             info["bound_total"]))
        self.assertEqual(total, self.report["product"]["relations_required"])
        self.assertEqual(total, complete + product["relations_unserved"])
        self.assertEqual(product["unstable_excluded"], 0)
        # Measured, not asserted: the Portal-only projection holds no BC invoice, so that
        # kind is unscored and the every-kind rule fails the check.
        self.assertEqual((bc["status"], bc["reason"]), ("unscored", "product_not_holding_root"))
        self.assertIn("bc_invoice", block["kinds_missing"])
        self.assertEqual(block["kinds_found_exact"], len(KINDS) - len(block["kinds_missing"]))
        self.assertEqual(ev.verdict(self.report), 3)

    def test_report_carries_no_row_data(self):
        text = json.dumps(self.report)
        self.assertEqual(self.report["row_data_check"]["leaked"], 0)
        self.assertGreaterEqual(self.report["row_data_check"]["values_checked"], 25)
        for item in self.drawn.items:
            self.assertNotIn(item.identifier, text)
            for _, value in item.key:
                if len(str(value)) >= 4:
                    self.assertNotIn(str(value), text)
        values = {v for rows in self.world.relations.values() for r in rows
                  for v in r.values() if isinstance(v, str) and len(v) >= 6}
        values |= {v for rows in self.world.bc.values() for r in rows
                   for v in r["fields"].values() if isinstance(v, str) and len(v) >= 6}
        leaked = sorted(v for v in values if v in text)
        self.assertEqual(leaked, [])
        self.assertEqual(set(self.report["items"][0]),
                         {"n", "kind", "root_kind", "reference", "reference_system",
                          "hash16", "bucket", "outcome", "reason", "path", "candidates",
                          "other_system_candidates", "root_rank", "uncited_candidates",
                          "measure"})

    def test_row_data_check_flags_a_leaked_identifier(self):
        item = self.drawn.items[0]
        text = json.dumps({"note": f"root {item.identifier} found"})
        self.assertEqual(ev.row_data_check(text, [item.identifier])["leaked"], 1)
        self.assertEqual(ev.row_data_check(json.dumps({"n": 1}), [item.identifier])["leaked"],
                         0)


class SessionProjectionTest(unittest.TestCase):
    """The daily build took 736 s of the 1500 s run (live
    2026-10-14 13:22Z) and cut sales_order and sharepoint by the budget. With the verifier
    session's projection committed in this session, the spec's own argv neither syncs,
    captures nor projects and sampling gets the build's share; without one the run builds
    as before (injected transport over the fixture world, the session produced as
    ``ontology.session_projection`` produces it)."""

    def run_spec(self, tmp: str, environ: dict) -> tuple[int, dict, int, list]:
        from unittest import mock

        from integrations import workbench_read_api
        from ontology import project

        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        argv = [arg.replace("{tmp}", tmp).replace("{session_state}", tmp + "/session")
                for arg in spec["command"][3:] if "{daily_report}" not in arg]
        argv[argv.index("--run-id") + 1] = "seed-a"   # both runs draw one sample
        builds, lefts = [], []
        real_build, real_clock = ev.build_projection, ev.sampling_clock

        def build(*args, **kwargs):
            builds.append(1)
            return real_build(*args, **kwargs)

        def clock(deadline, reserve):
            made = real_clock(deadline, reserve)
            start = made.start

            def first(kinds_left, slices=None):   # the seconds sampling starts with
                lefts.append(deadline.remaining() - reserve)
                made.start = start
                return start(kinds_left, slices)
            made.start = first
            return made

        projected = []
        real_project = project.Projector.project_into
        fetch = ev._fixture_fetch(build_world())
        out = io.StringIO()
        with mock.patch.object(workbench_read_api, "get_json",
                               lambda url, headers, **kw: fetch(url, headers)), \
                mock.patch.object(ev, "build_projection", build), \
                mock.patch.object(ev, "sampling_clock", clock), \
                mock.patch.object(project.Projector, "project_into",
                                  lambda p, b: projected.append(1) or real_project(p, b)), \
                contextlib.redirect_stdout(out):
            code = ev.main(argv, environ={
                "PLX_WORKBENCH_API_URL": "https://workbench-fixture.invalid",
                "PLX_WORKBENCH_API_KEY": "plxwb_" + "f" * 40, **environ})
        return code, json.loads(out.getvalue()), len(builds) + len(projected), lefts

    def test_a_committed_session_projection_is_reused_and_sampling_gets_the_build_share(self):
        from datetime import datetime, timezone

        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts import ontology_projection_probe as producer
        from scripts.mirror_measure import ordered_full_coverage

        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        command = spec["command"]
        total = float(command[command.index("--total-budget-seconds") + 1])
        reserve = float(command[command.index("--find-reserve-seconds") + 1])
        share = float(command[command.index("--build-share") + 1])
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with tempfile.TemporaryDirectory() as tmp:
            session = tmp + "/session"
            ordered_full_coverage(
                WorkbenchReadApiClient("https://workbench-fixture.invalid", "plxwb_" + "f" * 40,
                                       fetch=ev._fixture_fetch(build_world())),
                session + "/mirror.sqlite", resolve_sample=0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(producer.main(
                    ["--produce-session", "--session-projection", session + "/projection",
                     "--db", session + "/mirror.sqlite"], environ={}), 0)
            fresh = {live_verify.CAPTURE_FRESH_SINCE_ENV: start}
            code, reused, built, lefts = self.run_spec(tmp, fresh)
            # outside a verifier session (no session start) the run builds as before
            _, cold, cold_built, _ = self.run_spec(tmp, {})
        block = reused["eval"]
        self.assertIn(code, (0, 3))
        self.assertEqual((block["product"]["source"], block["product"]["available"]),
                         ("session_projection", True))
        self.assertGreater(block["product"]["object_count"], 0)
        self.assertEqual(block["product"]["completeness"], cold["eval"]["product"]["completeness"])
        self.assertEqual(built, 0)   # no sync, BC capture or projection of its own
        # a build spending its share would leave sampling total * (1 - share) - reserve
        self.assertGreater(lefts[0], total - reserve - 30)
        self.assertGreater(lefts[0] - (total * (1 - share) - reserve), total * share - 30)
        # the session product answers every kind as a build of its own does (no BC here)
        self.assertEqual(block["by_kind"]["purchase_order"]["status"], "passed")
        self.assertEqual(block["by_kind"], cold["eval"]["by_kind"])
        self.assertEqual(reused["row_data_check"]["leaked"], 0)
        self.assertEqual(cold["eval"]["product"]["source"], "projection_build")
        self.assertGreaterEqual(cold_built, 2)   # its own build and projection
        # the fallback never passes unnoticed: its own fail reason, and the spec's pinned
        # source refuses it; the session run carries neither
        self.assertNotIn("session", block["product"])
        self.assertNotIn("session_not_committed", block["fail_reasons"])
        self.assertEqual(cold["eval"]["product"]["session"], "not_committed")
        self.assertIn("session_not_committed", cold["eval"]["fail_reasons"])
        self.assertEqual(spec["expect"]["json_equals"]["eval.product.source"],
                         "session_projection")
        self.assertIn("eval.product.source != expected", spec_failures(cold, 3))
        self.assertNotIn("eval.product.source != expected", spec_failures(reused, code))


class CommandAndVerifierTest(unittest.TestCase):
    def test_fixture_command_is_reproducible_and_fails_on_the_kinds_it_misses(self):
        outputs = []
        for _ in range(2):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter("always", ResourceWarning)
                code = ev.main(["fixture"], environ={})
                gc.collect()
            # The command closes the product's store: no connection is left to the
            # collector's unclosed-database warning, which lands on a later run's stderr.
            self.assertEqual([str(w.message) for w in seen
                              if issubclass(w.category, ResourceWarning)], [])
            outputs.append(json.loads(buffer.getvalue()))
            # The staging rows lineage keeps out of the benchmark (QA smoke, demo, BC
            # renumbered) never reach the scored output.
            for marker in ("FXBC", "fx-po-qa", "fx-po-demo", "fx-po-bc", "PO#99901"):
                self.assertNotIn(marker, buffer.getvalue())
            # The fixture product holds Portal captures only: bc_invoice is not held.
            block = outputs[-1]["eval"]
            self.assertEqual(block["by_kind"]["bc_invoice"]["status"], "not_held")
            # nothing was read for a kind not held, so it names no source
            self.assertNotIn("bc_invoice", block.get("sources_tried", {}))
            self.assertEqual(code, 3)
            self.assertGreaterEqual(block["scored"], 20)
        for output in outputs:  # timings and memory are the only run-to-run difference
            for name in ("build_seconds", "projection_seconds", "peak_rss_mib"):
                output["eval"]["product"].pop(name)
        strip = [measure.without_wall_clock({k: v for k, v in o.items() if k != "product"})
                 for o in outputs]
        self.assertEqual(strip[0], strip[1])
        self.assertEqual(outputs[0]["evidence_label"], "injected-transport")
        self.assertEqual(outputs[0]["adapter_status"], "live-unverified")

    def test_spec_validates_and_dry_run_passes(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        live_verify.validate_spec(spec, stem=SPEC.stem)
        self.assertIn("scripts.eval_unfamiliar_sample", live_verify.ALLOWED_MODULES)
        done = subprocess.run([sys.executable, "devloop/live_verify.py", "--dry-run", "--only",
                               "eval.unfamiliar_sample"], cwd=ROOT, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout[-500:])
        self.assertEqual(json.loads(done.stdout)["summary"]["valid"], 1)

    def test_the_fixture_command_fails_the_spec_only_on_its_missing_kinds(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ev.main(["fixture"], environ={})
        document = json.loads(buffer.getvalue())
        missing = document["eval"]["kinds_missing"]
        self.assertTrue(missing)
        self.assertEqual(document["eval"]["fail_reasons"],
                         ["kinds_failing", "no_bc_rooted_scored"])
        failures = spec_failures(document, code)
        # the fixture product holds no BC capture, so no BC-rooted item is scored either
        # and it builds its own product, which the spec's pinned source refuses
        self.assertEqual(failures, ["exit_code 3 != 0", "eval.product.source != expected",
                                    "eval.bc_rooted.scored < 1", "eval.kinds_found_exact < 7",
                                    "eval.fail_reasons not empty"])
        self.assertTrue(recorded_whole(document, failures))

    def test_an_every_kind_report_meets_the_spec_and_the_verifier_lifts_the_eval_block(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        drawn, _, _ = fixture_sample()
        document = dict(answered(drawn), evidence_label="live-read")
        self.assertEqual(live_verify.check_expectations(spec, ev.verdict(document), document),
                         [])
        detail = live_verify._cap({"fields": live_verify._recorded(spec, document)})
        self.assertNotIn("fields_truncated", detail)
        # Room left for live data (more skip codes, read failures, longer edge lists).
        self.assertLess(len(json.dumps(detail, sort_keys=True)),
                        live_verify.MAX_DETAIL_CHARS - 1000)
        for path in spec["record"]:
            self.assertIn(path, detail["fields"])
        self.assertNotIn("items", " ".join(p for p in spec["record"] if p.startswith("items[")))
        check = {"id": spec["id"], "result": "pass", "detail": detail}
        other = {"id": "case.x", "result": "pass", "detail": {"fields": {"eval": {"a": 1}}}}
        report = live_verify.build_report([], [check, other], root=ROOT, main_sha="abc",
                                          previous_main_sha=None)
        lifted = report["eval"]
        self.assertEqual(set(lifted), {"eval.unfamiliar_sample"})
        self.assertEqual(lifted["eval.unfamiliar_sample"]["pass"], document["eval"]["pass"])
        self.assertEqual(lifted["eval.unfamiliar_sample"]["by_kind"],
                         document["eval"]["by_kind"])
        self.assertEqual(lifted["eval.unfamiliar_sample"]["result"], "pass")

    def test_the_largest_fixture_records_keep_headroom_under_the_verifier_cap(self):
        """The verifier keeps 4000 characters of a check's detail; the largest records the
        fixtures make (every kind unscored, a failing Portal outage with the product's own
        BC capture, a kind that tried many sources) stay <= 3600 so live data has room.
        Zero outcome counts are left out of eval.by_kind and a kind names at most
        MAX_SOURCES_TRIED sources (their count apart)."""
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        sources, _, _, _ = dev.build_fixture()
        paths = [f"sharepoint:/Library{n}" for n in range(9)]

        class ManyPaths(Unnamed):
            def listing(self, kind):
                return {"source": paths if kind == "sharepoint" else ["mail:x/inbox"]}

        many = answered(sample(sources, run_id="seed-a", count=28, documents=ManyPaths()))
        self.assertEqual(many["eval"]["sources_tried"]["sharepoint"],
                         paths[:ev.MAX_SOURCES_TRIED])
        self.assertEqual(many["eval"]["sources_tried_total"], {"sharepoint": len(paths)})
        # every three-way item undecided, each open aspect with its own long product reason
        drawn, _, _ = fixture_sample()
        processes = {}
        for n, item in enumerate(i for i in drawn.items if i.kind == THREE_WAY):
            answer = process_answer(drawn.readings[item.hash16], undecided=ev.ASPECTS)
            for stage in answer["stages"]:
                stage["missing"]["reason"] = f"{stage['stage']}_cause_{n}".ljust(40, "x")
            answer["gaps"] = [{"state": "unknown", "reason": f"{role}_cause_{n}".ljust(40, "x"),
                               "subject": role} for role in ("ordered", "received", "invoiced")]
            processes[root_handle(item)] = answer
        undecided = ev.run_eval(drawn, three_way_holding(ScriptedFind(
            {i.identifier: [root_handle(i)] for i in drawn.items}, processes=processes),
            drawn.items), run_id="seed-a", evidence_label="injected-transport", configured={})
        open_aspects = [a for r in undecided["items"] if r["outcome"] == "three_way_undecided"
                        for a in r["undecided_aspects"].items()]
        self.assertIn(("stage:vendor_invoice", "vendor_invoice_cause_0".ljust(40, "x")),
                      open_aspects)
        self.assertGreater(len(set(open_aspects)), ev.MAX_UNDECIDED_REASONS)
        kind = undecided["eval"]["by_kind"][THREE_WAY]
        counts = {k: n for k, n in kind.items() if k.startswith("undecided.")}
        self.assertEqual(len(counts), ev.MAX_UNDECIDED_REASONS + 1)
        self.assertEqual(sum(counts.values()), len(open_aspects))
        names = {a.partition(":")[2] for a in ev.ASPECTS} | {"other"}
        self.assertTrue(all(k.split(".")[1] in names for k in counts), counts)
        reports = {"many_sources": many, "undecided": undecided,
                   "none_scored": BudgetAndCoverageTest._scored_report(self, 0),
                   "outage_wrong": PortalOutageTest.outage_report(
                       self, error_code="InternalError", wrong=True, naming_documents=False)}
        sizes = {}
        for name, report in reports.items():
            document = dict(report, evidence_label="live-read")
            detail = {"fields": live_verify._recorded(spec, document),
                      "failures": spec_failures(report)}
            sizes[name] = len(json.dumps(detail, sort_keys=True))
            self.assertNotIn("fields_truncated", live_verify._cap(detail), name)
        recorded = live_verify._recorded(spec, dict(undecided, evidence_label="live-read"))
        self.assertEqual({k: n for k, n in recorded["eval"]["by_kind"][THREE_WAY].items()
                          if k.startswith("undecided.")}, counts)
        self.assertLessEqual(max(sizes.values()), 3600, sizes)

    def test_live_without_credentials_fails_with_an_error_class_only(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = ev.main(["live", "--no-bc"], environ={})
        self.assertEqual(code, 1)
        self.assertEqual(set(json.loads(err.getvalue())), {"error"})

    def test_sharepoint_scope_is_the_drive_and_folders_the_kind_reads(self):
        """The QCQA '/AOs/2026 - AOs' capture scope points the SharePoint kind at that
        folder's files; with the drive alone the kind keeps no_candidate_in_sample."""
        from evals.oracle.generator import PORTAL_STRATA
        scope = ROOT / "examples" / "sharepoint_capture_scope.qcqa_aos.live-check.json"
        target = json.loads(scope.read_text())["targets"][0]
        (ao_rel, ao_col), = PORTAL_STRATA["assembly_order"][1]
        seen = []

        def build_live(env, args, guard=None):
            world = build_world()
            seen.append((env.get("SHAREPOINT_DRIVE_ID"), list(args.sharepoint_path)))
            read = seen[-1] == (target["drive_id"], target["folders"])
            world.docs["sharepoint"] = [{
                "handle": "sharepoint://fx-drive/AOs/ao-packet",
                "texts": [f"AO {key_text(world.relations[ao_rel][0][ao_col])} packet"]}] \
                if read else []
            sources, documents, configured, _ = dev.build_fixture(world)
            return sources, documents, configured

        def run(*extra):
            out = io.StringIO()
            original, dev.build_live = dev.build_live, build_live
            try:
                with contextlib.redirect_stdout(out):
                    code = ev.main(["live", "--no-bc", "--run-id", "seed-a", *extra],
                                   environ={"SHAREPOINT_DRIVE_ID": "b!other"})
            finally:
                dev.build_live = original
            return code, json.loads(out.getvalue())["eval"]["by_kind"]["sharepoint"]

        _, drive_alone = run("--sharepoint-path", "/Documents")
        self.assertEqual(drive_alone["status"], "no_candidate_in_sample")
        self.assertEqual(seen[-1], ("b!other", ["/Documents"]))
        _, scoped = run("--sharepoint-scope", str(scope))
        self.assertEqual(seen[-1], (target["drive_id"], target["folders"]))
        self.assertNotEqual(scoped["status"], "no_candidate_in_sample")
        self.assertGreaterEqual(scoped["candidates"], 1)
        for bad in ("{}", json.dumps({"schema": "plx-sharepoint-capture-scope/v1",
                                      "targets": [target, target]})):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "scope.json"
                path.write_text(bad)
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as raised:
                    ev.main(["live", "--sharepoint-scope", str(path)], environ={})
                self.assertEqual(raised.exception.code, 2)

    def test_sharepoint_scope_is_parsed_and_bounded_as_capture_reads_it(self):
        """The scope file is read by the capture module's own parser
        (``CaptureScope.from_file``), so its walk bounds apply to the SharePoint kind:
        ``max_depth`` 1 lists the scope folder and its children, never a grandchild;
        ``max_folders`` caps the folders listed; ``max_content_bytes`` caps each text
        read. A scope the capture refuses is refused here too."""
        from unittest import mock
        from integrations import graph_read

        target = json.loads((ROOT / "examples" / "sharepoint_capture_scope.qcqa_aos"
                             ".live-check.json").read_text())["targets"][0]
        root = target["folders"][0]
        listed, asked = [], []

        class Drive:   # a tree three levels deep: four folders below each folder
            def __init__(self, *args, **kwargs):
                pass

            def list_folder(self, actor, path, **kwargs):
                listed.append(path)
                below = [{"kind": "folder", "path": f"{path}/f{n}"} for n in range(4)] \
                    if path.count("/") - root.count("/") < 3 else []
                return {"items": below + [{"kind": "file", "id": path, "name": "ao.txt",
                                           "size": 10, "citation": f"sharepoint://d{path}@e"}],
                        "completeness": {"truncated": False}}

            def get_content(self, actor, item_id, *, max_bytes):
                asked.append(max_bytes)
                return {"content": b"AO", "content_status": "read"}

        def documents(scope: dict):
            built = []

            def build_live(env, args, guard=None):
                built.append(original(env, args, guard=guard)[1])
                raise RuntimeError("stop after the ports")
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "scope.json"
                path.write_text(json.dumps(scope))
                original = dev.build_live
                with mock.patch.object(dev, "build_live", build_live), \
                        mock.patch.object(graph_read, "build_graph",
                                          lambda env: (None, None)), \
                        mock.patch.object(graph_read, "SharePointReader", Drive), \
                        contextlib.redirect_stderr(io.StringIO()):
                    code = ev.main(["live", "--no-bc", "--sharepoint-scope", str(path)],
                                   environ={n: "x" for n in ("MICROSOFT_GRAPH_TENANT_ID",
                                                             "MICROSOFT_GRAPH_CLIENT_ID",
                                                             "MICROSOFT_GRAPH_CLIENT_SECRET")})
            self.assertEqual((code, len(built)), (1, 1))
            listed.clear()
            asked.clear()
            built[0].sample("sharepoint", 200)
            return [p.count("/") - root.count("/") for p in listed]

        scope = {"schema": "plx-sharepoint-capture-scope/v1",
                 "targets": [dict(target, max_depth=1, max_folders=200)]}
        self.assertEqual(documents(scope), [0, 1, 1, 1, 1])   # never a depth-2 folder
        self.assertEqual(set(asked), {dev.MAX_TEXT_BYTES})
        scope["targets"] = [dict(target, max_depth=3, max_folders=3)]
        scope["max_content_bytes"] = 1000
        self.assertEqual(documents(scope), [0, 1, 1])
        self.assertEqual(set(asked), {1000})
        # the eval's own walk bounds still hold where the scope's are wider
        scope["targets"] = [dict(target, max_depth=20, max_folders=5000)]
        self.assertLessEqual(max(documents(scope)), dev.MAX_DRIVE_DEPTH)
        for bad in ({"schema": "plx-sharepoint-capture-scope/v1",
                     "targets": [dict(target, max_depth=-1)]},
                    {"schema": "plx-sharepoint-capture-scope/v1",
                     "targets": [dict(target, unknown=1)]},
                    {"schema": "plx-sharepoint-capture-scope/v1", "targets": [
                        {k: v for k, v in target.items() if k != "drive_id"} | {
                            "site": "QualityControl"}]}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "scope.json"
                path.write_text(json.dumps(bad))
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as raised:
                    ev.main(["live", "--sharepoint-scope", str(path)], environ={})
                self.assertEqual(raised.exception.code, 2)

    def test_sampler_is_under_the_oracle_import_ban(self):
        done = subprocess.run([sys.executable, "scripts/check_oracle_imports.py", str(ROOT)],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout[-800:])
        self.assertTrue((ROOT / "evals" / "oracle" / "cross_sample.py").is_file())

    def test_public_rows_hold_enums_and_hashes_only(self):
        drawn, _, _ = fixture_sample()
        for item in drawn.items:
            row = public_row(item)
            self.assertNotIn(item.identifier, json.dumps(row))
            self.assertRegex(row["hash16"], r"^[0-9a-f]{16}$")


# BC API v2.0 entity properties of the sets live 1303d122 listed in read_failures (names
# from the API v2.0 resource reference; live bc.case_entities of the same run read every
# filter field of integrations.live_source_grammar.BC_QUERY_SPECS with
# filter_fields_absent empty, and bc.purchase_reader filtered purchaseReceipts and
# purchaseInvoices by orderNumber). A filter on any other property answers 400, as BC does.
_ADDRESS = ("AddressLine1 AddressLine2 City Country State PostCode")
V2_PROPERTIES = {
    "purchaseOrders": "id number orderDate postingDate vendorId vendorNumber vendorName "
                      "payToName payToVendorId payToVendorNumber shipToName shipToContact "
                      "currencyId currencyCode pricesIncludeTax paymentTermsId "
                      "shipmentMethodId purchaser requestedReceiptDate discountAmount "
                      "discountAppliedBeforeTax totalAmountExcludingTax totalTaxAmount "
                      "totalAmountIncludingTax fullyReceived status lastModifiedDateTime",
    "purchaseReceipts": "id number invoiceDate postingDate dueDate vendorNumber vendorName "
                        "payToName payToContact payToVendorNumber shipToName shipToContact "
                        "currencyCode orderNumber lastModifiedDateTime",
    "purchaseInvoices": "id number invoiceDate postingDate dueDate vendorInvoiceNumber "
                        "vendorId vendorNumber vendorName payToName payToContact "
                        "payToVendorId payToVendorNumber shipToName shipToContact "
                        "currencyId currencyCode orderId orderNumber pricesIncludeTax "
                        "discountAmount discountAppliedBeforeTax totalAmountExcludingTax "
                        "totalTaxAmount totalAmountIncludingTax status lastModifiedDateTime",
    "salesInvoices": "id number externalDocumentNumber invoiceDate postingDate dueDate "
                     "customerPurchaseOrderReference customerId customerNumber customerName "
                     "billToName billToCustomerId billToCustomerNumber shipToName "
                     "shipToContact currencyId currencyCode orderId orderNumber "
                     "paymentTermsId shipmentMethodId salesperson pricesIncludeTax "
                     "remainingAmount discountAmount discountAppliedBeforeTax "
                     "totalAmountExcludingTax totalTaxAmount totalAmountIncludingTax status "
                     "lastModifiedDateTime phoneNumber email",
}
# The reads live 1303d122 reported failed, by (entity set, field); None: the sample.
LIVE_FAILED_READS = (("purchaseInvoices", "orderNumber"), ("purchaseOrders", "number"),
                     ("purchaseReceipts", "orderNumber"),
                     ("salesInvoices", "externalDocumentNumber"), ("salesInvoices", None))


def v2_bc_sender(fake, properties, sent):
    """``fake`` (``fixture_bc_sender``) with BC's exact-equality ``$filter`` on the entity's
    v2.0 properties; a filter on a property the entity does not expose answers 400.
    ``sent`` receives (method, url) of every request."""
    from urllib.parse import parse_qs, unquote, unquote_plus, urlencode, urlsplit, urlunsplit

    def send(method, url, headers, body, timeout, max_bytes):
        sent.append((method, unquote_plus(url)))
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        condition = query.get("$filter", [""])[0]
        if " eq " not in condition:
            return fake(method, url, headers, body, timeout, max_bytes)
        name, _, literal = condition.partition(" eq ")
        entity_set = unquote(parts.path).rsplit("/", 1)[1]
        if name not in properties[entity_set].split():
            return 400, {}, b'{"error":{"code":"BadRequest"}}'
        rest = {k: v for k, v in query.items() if k != "$filter"}
        status, out_headers, raw = fake(
            method, urlunsplit(parts._replace(query=urlencode(rest, doseq=True))),
            {**headers, "Prefer": "odata.maxpagesize=1000"}, body, timeout, max_bytes)
        if status != 200:
            return status, out_headers, raw
        page = json.loads(raw)
        value = literal[1:-1].replace("''", "'")
        page["value"] = [r for r in page["value"]
                         if str(r.get(name, "")).casefold() == value.casefold()]
        return 200, out_headers, json.dumps(page).encode()
    return send


def live_bc_reads(world, properties=V2_PROPERTIES, guard=None):
    """The oracle's BC reads as ``scripts.eval_oracle_dev.build_live`` wires them (the real
    GET-only ``BusinessCentralReader`` and ``LiveBcPort``) over the v2.0 fake."""
    from evals.oracle import BcReads
    from integrations.bc_read import BusinessCentralReader, build_bc
    from integrations.live_source_guard import LiveSourceAudience
    from integrations.tests.test_live_source_adapters import ENV
    from ontology.types import Actor

    fake, _ = fixture_bc_sender(world)
    sent: list = []
    api, _, tenant, environment = build_bc(ENV, sender=v2_bc_sender(fake, properties, sent))
    audience = LiveSourceAudience({f"bc:{environment.lower()}:*": [dev.EVAL_PRINCIPAL]},
                                  tenant="eval")
    reader = BusinessCentralReader(api, audience, tenant_id=tenant, environment=environment)
    actor = Actor("eval", frozenset({dev.EVAL_PRINCIPAL}), "restricted")
    port = dev.LiveBcPort(reader, actor, **({"guard": guard} if guard else {}))
    return BcReads(port), sent


def world_with_bc_orders():
    """The fixture world plus BC purchase orders and posted receipts (its own sets hold
    none), so every read of ``LIVE_FAILED_READS`` has a record to find."""
    from evals.oracle.fixture_world import bc_record

    world = build_world()
    for n in range(1, 4):
        world.bc["purchaseOrders"].append(bc_record(
            "purchaseOrders", n % 2, 1000 + n, number=f"FXPO{710000 + n}", status="Open",
            fullyReceived=True, vendorNumber="FXV001"))
        world.bc["purchaseReceipts"].append(bc_record(
            "purchaseReceipts", n % 2, 1100 + n, number=f"FXPR{810000 + n}",
            orderNumber=f"FXPO{710000 + n}", vendorNumber="FXV001"))
    return world


class BcFieldReadTest(unittest.TestCase):
    """Live 1303d122 listed bc:purchaseInvoices.orderNumber, bc:purchaseOrders.number,
    bc:purchaseReceipts.orderNumber, bc:salesInvoices.externalDocumentNumber and
    bc:salesInvoices.sample as read_failures with no cause; the same run's bc.case_entities
    read those fields. The cause was the run budget (the sampling guard): each failure now
    says its cause, and a field BC does not expose is a typed not_exposed gap."""

    def test_the_failed_reads_succeed_against_the_v2_metadata_shape(self):
        world = world_with_bc_orders()
        bc, sent = live_bc_reads(world)
        for entity_set, field in LIVE_FAILED_READS:
            if field is None:
                self.assertTrue(bc.sample(entity_set, 50), entity_set)
                continue
            value = next(r["fields"][field] for r in world.bc[entity_set]
                         if r["fields"].get(field))
            records, complete = bc.find(entity_set, field, value)
            self.assertTrue(records and complete, (entity_set, field))
            self.assertTrue(all(r["fields"][field] == value for r in records))
        self.assertEqual(bc.failures(), {})
        self.assertTrue(any("$filter=orderNumber eq " in url for _, url in sent))
        self.assertEqual({m for m, url in sent if "businesscentral" in url}, {"GET"})

    def test_a_field_bc_does_not_expose_is_a_typed_not_exposed_gap(self):
        from evals.oracle.families import Context
        from evals.oracle.source import ReadFailed

        world = world_with_bc_orders()
        properties = dict(V2_PROPERTIES, purchaseReceipts=V2_PROPERTIES[
            "purchaseReceipts"].replace(" orderNumber", ""))
        bc, _ = live_bc_reads(world, properties)
        with self.assertRaises(ReadFailed) as caught:
            bc.find("purchaseReceipts", "orderNumber", "FXPO710001")
        self.assertEqual((caught.exception.cause, caught.exception.http_status),
                         ("not_exposed", 400))
        ctx = Context(Sources(PortalReads(dev.portal_port(type("C", (), {
            "read_relation": staticmethod(lambda *a, **k: world.read_relation(
                a[0], k["columns"]))})())), bc), {})
        self.assertEqual(ctx.lookup("bc:purchaseReceipts", "orderNumber", "FXPO710002"), [])
        self.assertEqual([g["reason"] for g in ctx.gaps], ["read_failed_not_exposed"])
        # a value the BC grammar cannot send is not queryable: no request, not a failure
        # of the source
        with self.assertRaises(ReadFailed) as caught:
            bc.find("purchaseOrders", "number", "PO (copy) 'x'")
        self.assertEqual(caught.exception.cause, "not_queryable")
        self.assertEqual(bc.causes(), {"bc:purchaseReceipts.orderNumber": "not_exposed",
                                       "bc:purchaseOrders.number": "not_queryable"})

    def test_the_report_names_each_failed_reads_cause(self):
        drawn, _, _ = fixture_sample()
        drawn.failures.update({"bc:purchaseReceipts.orderNumber": "HTTPError",
                               "bc:salesInvoices.sample": "BudgetExhausted",
                               "bc:purchaseOrders.number": "ValueError"})
        drawn.causes.update({"bc:purchaseReceipts.orderNumber": "not_exposed",
                             "bc:salesInvoices.sample": "budget",
                             "bc:purchaseOrders.number": "not_queryable"})
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={})
        self.assertEqual(report["read_failures"], {
            "bc:purchaseReceipts.orderNumber": "not_exposed",
            "bc:purchaseOrders.number": "not_queryable",
            "bc:salesInvoices.sample": "budget"})
        self.assertIn("source_read_failures", report["eval"]["incomplete"])
        self.assertIn("sampling_budget_cut", report["eval"]["incomplete"])
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        kept = live_verify._recorded(spec, dict(report, evidence_label="live-read"))
        self.assertEqual(kept["read_failures"], report["read_failures"])
        # a budget stop alone is not a source failure
        del drawn.failures["bc:purchaseReceipts.orderNumber"]
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={})
        self.assertNotIn("source_read_failures", report["eval"]["incomplete"])


class KindSliceTest(unittest.TestCase):
    """Live 1303d122: the build left sampling 63 s, the sales-order BC probes spent it and
    bc_invoice, mail and SharePoint were blocked by the budget, never read. Each kind now
    draws in its own slice of the sampling time."""

    @staticmethod
    def slow_run(count=28, seconds=160.0, slow=("salesOrders", "salesInvoices"),
                 per_find=40.0, slow_sample=0.0, slow_documents=0.0, greedy=False,
                 world=None, bc_finds_total=150):
        """A sample where every BC find on ``slow`` sets takes ``per_find`` seconds (the BC
        invoice listing ``slow_sample``, each document search ``slow_documents``) of a
        ``seconds`` sampling budget, through the runner's own guard and clock.
        ``greedy``: a document search reads page after page (a second each) until the
        guard stops it."""
        world = world or build_world()
        now = [0.0]
        deadline = ev.Deadline(seconds, monotonic=lambda: now[0])
        clock = ev.sampling_clock(deadline, 0.0)
        guard = ev.sampling_guard(deadline, 0.0, clock)
        inner, docs = world.bc_port(), world.documents()

        class SlowBc:
            def find(self, entity_set, field, value):
                guard()
                if entity_set in slow:
                    now[0] += per_find
                return inner.find(entity_set, field, value)

            def lines(self, record):
                guard()
                return inner.lines(record)

            def sample(self, entity_set, limit):
                guard()
                now[0] += slow_sample
                return inner.sample(entity_set, limit)

        class Docs:
            def search(self, kind, queries, **kwargs):
                guard()
                now[0] += slow_documents
                while greedy:
                    guard()
                    now[0] += 1.0
                return docs.search(kind, queries, **kwargs)

        from evals.oracle import BcReads
        portal = dev.portal_port(type("C", (), {"read_relation": staticmethod(
            lambda *a, **k: world.read_relation(a[0], k["columns"]))})())
        return sample(Sources(PortalReads(portal), BcReads(SlowBc())), run_id="seed-a",
                      count=count, documents=Docs(), clock=clock,
                      bc_finds_total=bc_finds_total)

    def test_one_slow_kind_stops_at_its_slice_and_the_others_still_draw(self):
        drawn = self.slow_run()
        slow = drawn.kinds["sales_order"]
        self.assertTrue(slow.get("budget_cut"), slow)
        self.assertGreater(slow["skipped"]["kind_budget"], 0)
        for kind in KINDS:
            entry = drawn.kinds[kind]
            if kind == "sales_order":
                continue
            self.assertNotIn(entry["status"], ("unavailable", "unreadable"), (kind, entry))
            self.assertGreaterEqual(entry["references"], 1, kind)
            self.assertGreaterEqual(entry["items"], 1, kind)
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={})
        self.assertTrue(report["kinds"]["sales_order"]["budget_cut"])
        self.assertIn("sampling_budget_cut", report["eval"]["incomplete"])
        self.assertGreater(report["eval"]["sampling_exclusions"]["kind_budget"], 0)

    def test_a_kind_whose_slice_was_spent_resumes_its_reference_reads_on_what_is_left(self):
        """gap-bf9911970a51 (live sales_order blocked: budget on its BC reference read): a
        kind's reference reads keep a share of their own. The BC invoice listing spends the
        kind's whole slice (12 s of 80 s / 8); once every kind has drawn, it resumes its
        reference reads in what the others left, never blocked by the budget."""
        drawn = self.slow_run(seconds=80.0, slow=(), slow_sample=6.0)
        entry = drawn.kinds["bc_invoice"]
        self.assertEqual(entry["status"], "read")
        self.assertNotIn("budget_cut", entry)
        self.assertNotIn("kind_budget", entry["skipped"])
        for relation in (f"bc:{s}" for s in BC_INVOICE_SETS):   # every root counted once
            counts = entry["funnel"][relation]
            self.assertEqual(counts["roots"], sum(counts[s] for s in (
                "no_reference_edge", "no_reference", "references", "untried")), relation)
        self.assertGreaterEqual(entry["references"], 1)
        self.assertGreaterEqual(entry["items"], 1)
        for kind in KINDS:
            self.assertGreaterEqual(drawn.kinds[kind]["items"], 1, kind)

    def test_a_kind_its_slice_leaves_without_a_reference_is_blocked_budget_never_empty(self):
        # its listing spent its slice and the documents spent the rest: no time to resume
        drawn = self.slow_run(seconds=80.0, slow=(), slow_sample=6.0, slow_documents=100.0)
        entry = drawn.kinds["bc_invoice"]
        self.assertEqual((entry["status"], entry.get("blocked")), ("unavailable", "budget"))
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={})
        by_kind = report["eval"]["by_kind"]["bc_invoice"]
        self.assertEqual((by_kind["status"], by_kind["blocked"]), ("blocked", "budget"))

    def test_a_kind_cut_early_keeps_its_share_when_later_kinds_would_take_every_second(self):
        """gap-bf9911970a51 / gap-51ebb6c78e90 (live 37e539e: sales_order, mail and
        SharePoint blocked: budget). The BC invoice listing spends bc_invoice's slice
        (12 s of 80 s / 8) and each document kind's search would read until stopped;
        a later kind's first slice is its fair share, never what the earlier ones left, so
        the cut kind's second slice still finds its references, every record kind (receipt
        with its reference) is sampled and the documents are blocked by their own reads."""
        drawn = self.slow_run(seconds=80.0, slow=(), slow_sample=6.0, greedy=True)
        for kind in KINDS:
            entry = drawn.kinds[kind]
            if kind in DOCUMENT_KINDS:   # their own search was cut: blocked, never empty
                self.assertEqual((entry["status"], entry.get("blocked"), entry["reason"]),
                                 ("unavailable", "budget", "BudgetExhausted"), kind)
                continue
            self.assertEqual(entry["status"], "read", (kind, entry))
            self.assertGreaterEqual(entry["references"], 1, kind)
            self.assertGreaterEqual(entry["items"], 1, kind)
        self.assertNotIn("budget_cut", drawn.kinds["bc_invoice"])
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={})
        blocked = {k for k, v in report["eval"]["by_kind"].items() if v.get("blocked")}
        self.assertEqual(blocked, set(DOCUMENT_KINDS))

    def test_a_read_overrunning_the_whole_budget_leaves_reads_free_kinds_sampled(self):
        """One BC find takes 40 s, the whole sampling budget: the slice bounds a kind's
        reads, not its checks over the rows read up front, so every record kind whose
        references need no further read is still sampled; the documents, whose search is a
        read, are blocked by the budget."""
        drawn = self.slow_run(seconds=40.0, slow=("purchaseInvoices",), per_find=40.0)
        for kind in KINDS:
            entry = drawn.kinds[kind]
            if kind in DOCUMENT_KINDS:
                self.assertEqual((entry["status"], entry.get("blocked")),
                                 ("unavailable", "budget"), kind)
            else:
                self.assertGreaterEqual(entry["items"], 1, (kind, entry))

    def test_a_kind_cut_before_any_reference_names_what_its_tried_roots_lacked(self):
        """The live sales-order shape: BC holds no copy of the Portal orders and each BC
        probe is slow, so the slices cut the kind with no reference: blocked by the budget,
        and it names what the roots it did try lacked and the sources holding no copy."""
        world = build_world()
        world.bc["salesOrders"], world.bc["salesInvoices"] = [], []
        drawn = self.slow_run(seconds=80.0, per_find=15.0, world=world)
        entry = drawn.kinds["sales_order"]
        self.assertEqual((entry["status"], entry.get("blocked"), entry.get("within_read")),
                         ("unavailable", "budget", "no_reference"))
        self.assertEqual(entry["missing_reference"], ["bc:salesInvoices.externalDocumentNumber",
                                                      "bc:salesOrders.externalDocumentNumber",
                                                      "bc:salesOrders.number"])
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={})
        by_kind = report["eval"]["by_kind"]["sales_order"]
        self.assertEqual((by_kind["blocked"], by_kind["within_read"]), ("budget", "no_reference"))
        # its BC probes completed and found no copy before the slices cut it
        unnamed = set(entry.get("unnamed_reference") or [])
        self.assertEqual(report["eval"]["missing_reference"]["sales_order"],
                         {"cause": "bc_source_mismatch", "roots": entry["skipped"]["no_reference"],
                          "bc_finds": entry["bc_finds"],
                          **{n: "no_naming_record" if n in unnamed else "no_record"
                             for n in entry["missing_reference"]}})

    def test_a_source_holding_records_naming_no_root_is_no_naming_record(self):
        from evals.oracle import cross_sample as cs
        looked: dict = {}
        cs._looked(looked, "bc:salesOrders", "number", False, True)    # records, none named
        cs._looked(looked, "bc:salesInvoices", "number", False)        # no record at all
        cs._looked(looked, "bc:salesInvoices", "number", False)
        entry = {"missing_reference": cs.missing_reference(looked),
                 "unnamed_reference": cs.unnamed_reference(looked), "skipped": {}}
        cause = ev.missing_reference_cause(entry)
        self.assertEqual(cause["cause"], "bc_source_mismatch")
        self.assertEqual(cause["bc:salesOrders.number"], "no_naming_record")
        self.assertEqual(cause["bc:salesInvoices.number"], "no_record")

    def test_sales_orders_bc_holds_no_copy_of_are_a_source_mismatch_not_a_budget(self):
        """gap-b01a86b2f438: BC holds none of the sampled Portal sales-order numbers. With
        every probe made (or the kind stopped on consecutive misses) the cause is
        bc_source_mismatch (counts and source names only,
        kept by the verifier); with any probe of the kind refused by the shared BC probe
        budget (the live daily: 927 refused, 25 made), budget_exhausted, never a mismatch
        it did not measure."""
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        causes = {}
        for probes in (10_000, 150, 10, 0):
            world = build_world()
            world.bc["salesOrders"], world.bc["salesInvoices"] = [], []
            drawn = self.slow_run(seconds=1e6, slow=(), world=world, bc_finds_total=probes)
            entry = drawn.kinds["sales_order"]
            self.assertEqual((entry["status"], entry["references"]), ("empty", 0), probes)
            report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                                 run_id="r", evidence_label="fixture", configured={})
            cause = report["eval"]["missing_reference"]["sales_order"]
            recorded = live_verify._recorded(spec, dict(report, evidence_label="live-read"))
            self.assertEqual(recorded["eval"]["missing_reference"]["sales_order"], cause)
            causes[probes] = cause
        # every probe made until the kind stopped on BC_MISS_STOP consecutive misses:
        # the mismatch it measured, with the probes it no longer made
        # counted bc_miss_stop, the same with the 150 shared probes as with any budget
        from evals.oracle.cross_sample import BC_MISS_STOP
        for probes in (10_000, 150):
            self.assertEqual({k: v for k, v in causes[probes].items() if k != "bc_miss_stop"}, {
                "cause": "bc_source_mismatch", "roots": drawn.kinds["sales_order"]["roots"],
                "bc_finds": BC_MISS_STOP,
                "bc:salesInvoices.externalDocumentNumber": "no_record",
                "bc:salesOrders.externalDocumentNumber": "no_record",
                "bc:salesOrders.number": "no_record"}, probes)
            self.assertGreater(causes[probes]["bc_miss_stop"], 0, probes)
        # a share below the stop: some probes made and some refused, which measured nothing
        self.assertEqual(causes[10]["cause"], "budget_exhausted")
        self.assertGreater(causes[10]["bc_finds"], 0)
        self.assertGreater(causes[10]["bc_probe_budget"], 0)
        self.assertEqual(causes[0], {"cause": "budget_exhausted",
                                     "roots": drawn.kinds["sales_order"]["roots"],
                                     "bc_probe_budget": causes[0]["bc_probe_budget"]})
        self.assertGreater(causes[0]["bc_probe_budget"], 0)

    def test_a_receipt_without_a_reference_names_the_source_holding_no_copy(self):
        """Live receipts are FM raw receipts (lab ids) and app_receipt is empty (the
        live-shaped world): the kind is empty for no_reference and names the reference
        source its roots were looked up in, in the sampler and in the published block."""
        from evals.oracle.fixture_world import build_live_shaped_world
        drawn = self.slow_run(seconds=1e6, slow=(), world=build_live_shaped_world())
        entry = drawn.kinds["receipt"]
        self.assertEqual((entry["status"], entry["reason"]), ("empty", "no_reference"))
        self.assertEqual(entry["missing_reference"], [f"{K.RECEIPT}.receipt_number"])
        report = ev.run_eval(drawn, ev.Product(None, {"status": "not_configured"}),
                             run_id="r", evidence_label="fixture", configured={})
        self.assertEqual(report["eval"]["missing_reference"],
                         {"receipt": {"cause": "no_reference",
                                      "roots": entry["skipped"]["no_reference"],
                                      f"{K.RECEIPT}.receipt_number": "no_record"}})
        for kind, v in drawn.kinds.items():   # a kind with references names no missing one
            if v["references"]:
                self.assertNotIn("missing_reference", v, kind)

    def test_item_counts_hold_at_one_ten_and_sixty(self):
        for count in (1, 10, 60):
            drawn = self.slow_run(count=count, seconds=1e6, slow=())
            hashes = [i.hash16 for i in drawn.items]
            self.assertEqual(len(hashes), len(set(hashes)), count)
            self.assertEqual(len(drawn.items), count)
            self.assertEqual(sum(v["items"] for v in drawn.kinds.values()), count)


class EveryKindRegressionTest(unittest.TestCase):
    """With the Portal up and the product capturing BC itself (fixture transport), no kind
    whose source holds records comes back empty, and BC-rooted items are scored; a run
    with none fails as no_bc_rooted_scored under either rule."""

    def test_no_kind_is_empty_while_its_source_has_records_and_bc_rooted_is_scored(self):
        from integrations.tests.test_live_source_adapters import ENV
        from integrations.workbench_read_api import WorkbenchReadApiClient

        world = build_world()
        fake, books = fixture_bc_sender(world)
        client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                        "plxwb_" + "f" * 40, fetch=ev._fixture_fetch(world))
        sources, documents, _, _ = dev.build_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            product = ev.product_from_build(
                client, Path(tmp) / "product", ev.Deadline(600), 0, total_seconds=600,
                bc_capture=lambda db, seconds: ev.capture_bc(ENV, db, seconds, sender=fake,
                                                             books=books))
            self.addCleanup(product.close)
            drawn = sample(sources, run_id="seed-a", count=28, documents=documents,
                           record_kinds=product.held_record_kinds())
            report = ev.run_eval(drawn, product, run_id="seed-a",
                                 evidence_label="injected-transport", configured={}, count=28)
        block = report["eval"]
        for kind in KINDS:
            entry = block["by_kind"][kind]
            self.assertFalse(entry["status"] == "empty" and entry.get("candidates"),
                             (kind, entry))
            self.assertGreaterEqual(entry["items"], 1, kind)
        self.assertGreaterEqual(block["bc_rooted"]["scored"], 1)
        self.assertNotIn("no_bc_rooted_scored", block["fail_reasons"])
        # the same report with no BC-rooted item scored fails, under either rule
        for rule in ("every_kind", "portal_outage"):
            broken = json.loads(json.dumps(report))
            broken["eval"]["pass_rule"] = rule
            broken["eval"]["bc_rooted"] = {"sampled": 0, "scored": 0}
            self.assertIn("no_bc_rooted_scored", ev.fail_reasons(broken), rule)


class MailNamesServedOrdersTest(unittest.TestCase):
    """gap-f713876847b6 / gap-04f9b49ebae9 (daily c80c4e42: mail 0 candidates,
    no_document_names_root): the purchasing mail names orders the Portal does not hold
    (case.po_mail_links 2026-10-13: its unmatched PO values all sit above every Portal PO
    row read), which BC serves, and Portal POs the Portal drained to BC under their own
    number. A mail naming a BC-served order is a mail item rooted in BC, and one naming a
    drained PO is rooted at the PO (its BC twin is the same transaction, never a second
    record), each scored through the product's own BC capture; no reference is lost or
    duplicated at N = 1/10/60 named BC orders (injected transport)."""

    def run_named(self, n: int) -> tuple[dict, dict, str]:
        from evals.oracle.fixture_world import (bc_record, drain_purchase_orders_to_bc,
                                                po_number)
        from integrations.tests.test_live_source_adapters import ENV
        from integrations.workbench_read_api import WorkbenchReadApiClient

        world = build_world()
        drained = world.relations["public.app_purchase_order"][1]
        drain_purchase_orders_to_bc(world, [drained["id"]])
        # BC orders numbered in the Portal's PO series past every Portal PO row
        served = [bc_record("purchaseOrders", k % 2, 960_000 + k, number=po_number(900 + k),
                            vendorNumber="FXV001", orderDate="2098-12-01")
                  for k in range(n)]
        world.bc["purchaseOrders"] += served
        world.docs["mail"] = [
            {"handle": "graph-mail://fx-box/drained@x",
             "texts": [f"RE: {drained['po_number']} goods received", "Thanks."]},
            *({"handle": f"graph-mail://fx-box/served-{k}@x",
               "texts": [f"Order {r['fields']['number']} confirmed", "Ships Friday."]}
              for k, r in enumerate(served))]
        fake, books = fixture_bc_sender(world)
        client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                        "plxwb_" + "f" * 40, fetch=ev._fixture_fetch(world))
        sources, documents, _, _ = dev.build_fixture(world)
        with tempfile.TemporaryDirectory() as tmp:
            product = ev.product_from_build(
                client, Path(tmp) / "product", ev.Deadline(600), 0, total_seconds=600,
                bc_capture=lambda db, seconds: ev.capture_bc(ENV, db, seconds, sender=fake,
                                                             books=books))
            self.addCleanup(product.close)
            drawn = sample(sources, run_id="seed-a", count=28, documents=documents,
                           record_kinds=product.held_record_kinds(), bc_sample_limit=200)
            report = ev.run_eval(drawn, product, run_id="seed-a",
                                 evidence_label="injected-transport", configured={}, count=28)
        return drawn.kinds["mail"], report, drained["po_number"]

    def test_a_mail_naming_a_served_order_is_a_scored_mail_item(self):
        for n in (1, 10, 60):
            mail, report, _ = self.run_named(n)
            with self.subTest(n=n):
                # one reference per order named: the BC orders and the drained PO
                self.assertEqual((mail["candidates"], mail["references"]), (n + 1, n + 1))
                self.assertEqual(mail["skipped"].get("not_unique", 0), 0)
        mail, report, number = self.run_named(1)
        rows = [r for r in report["items"] if r["kind"] == "mail"]
        self.assertEqual(sorted((r["root_kind"], r["outcome"]) for r in rows),
                         [("bc_document", "found_exact"), ("purchase_order", "found_exact")])
        self.assertEqual(report["eval"]["by_kind"]["mail"]["status"], "passed")
        self.assertGreaterEqual(report["eval"]["bc_rooted"]["scored"], 1)
        self.assertNotIn(number, json.dumps(report))
        self.assertEqual(report["row_data_check"]["leaked"], 0)


class DocumentNamesBcSalesOrderTest(unittest.TestCase):
    """gap-b01a86b2f438 / ER-08 gap-aec0bc8b9809 (task unfamiliar-roots): BC holds sales
    orders the Portal's staging replica has no row for (live case.unfamiliar_order: 47/50
    cited BC roots had no Portal row). A mail or SharePoint file naming such an order by
    its BC number is a document item rooted at the BC sales order itself (a Portal row is
    optional) and the product resolves that root exactly through its own BC capture. A
    number BC does not serve roots nothing. No reference is lost or duplicated at
    N = 1/10/60 named orders (injected transport)."""

    def run_named(self, source: str, n: int) -> tuple[dict, dict, list]:
        from evals.oracle.fixture_world import bc_record
        from integrations.tests.test_live_source_adapters import ENV
        from integrations.workbench_read_api import WorkbenchReadApiClient

        world = build_world()
        portal_numbers = {r["order_number"] for r in world.relations[K.SO]}
        served = [bc_record("salesOrders", k % 2, 970_000 + k, number=f"FXSO9{10000 + k}",
                            customerNumber="FXC001", orderDate="2098-12-01",
                            fullyShipped=True)
                  for k in range(n)]
        self.assertFalse(portal_numbers & {r["fields"]["number"] for r in served})
        world.bc["salesOrders"] += served
        scheme = "graph-mail://fx-box" if source == "mail" else "sharepoint://fx-drive"
        world.docs[source] = [
            *({"handle": f"{scheme}/so-{k}@x",
               "texts": [f"Order {r['fields']['number']} shipped", "Thanks."]}
              for k, r in enumerate(served)),
            # a number BC does not serve: no root, never a reference
            {"handle": f"{scheme}/unserved@x", "texts": ["Order FXSO899999 shipped"]}]
        fake, books = fixture_bc_sender(world)
        client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                        "plxwb_" + "f" * 40, fetch=ev._fixture_fetch(world))
        sources, documents, _, _ = dev.build_fixture(world)
        with tempfile.TemporaryDirectory() as tmp:
            product = ev.product_from_build(
                client, Path(tmp) / "product", ev.Deadline(600), 0, total_seconds=600,
                bc_capture=lambda db, seconds: ev.capture_bc(ENV, db, seconds, sender=fake,
                                                             books=books))
            self.addCleanup(product.close)
            drawn = sample(sources, run_id="seed-a", count=28, documents=documents,
                           record_kinds=product.held_record_kinds(), bc_sample_limit=200)
            report = ev.run_eval(drawn, product, run_id="seed-a",
                                 evidence_label="injected-transport", configured={}, count=28)
        return drawn, report, [r["fields"]["number"] for r in served]

    def test_a_document_naming_a_bc_only_sales_order_roots_at_that_order(self):
        for source in DOCUMENT_KINDS:
            for n in (1, 10, 60):
                drawn, report, numbers = self.run_named(source, n)
                entry = drawn.kinds[source]
                with self.subTest(source=source, n=n):
                    # one reference per order named, none for the unserved number
                    self.assertEqual((entry["candidates"], entry["references"]), (n, n))
                    self.assertEqual(entry["skipped"].get("not_unique", 0), 0)
                    items = [i for i in drawn.items if i.kind == source]
                    self.assertTrue(items)
                    self.assertEqual({i.relation for i in items}, {"bc:salesOrders"})
                    rows = [r for r in report["items"] if r["kind"] == source]
                    self.assertEqual({(r["root_kind"], r["outcome"]) for r in rows},
                                     {("bc_document", "found_exact")})
                    self.assertEqual(report["eval"]["by_kind"][source]["status"], "passed")
                    self.assertGreaterEqual(report["eval"]["bc_rooted"]["scored"], len(rows))
                    text = json.dumps(report)
                    self.assertFalse([x for x in numbers if x in text])
                    self.assertEqual(report["row_data_check"]["leaked"], 0)



class BcNativeSalesOrderRootTest(unittest.TestCase):
    """Decision 2026-10-18-unfamiliar-roots-bc-native-sales-order-roots (option 2, ER-08
    gap-aec0bc8b9809): the sales_order kind also roots at BC sales orders. A BC order with
    no Portal row is a sales_order root (counted, never dropped) that only a document can
    name: no Portal record gives it a reference, and its customer PO
    (``externalDocumentNumber``, ``keys.SHARED_REFERENCE_FIELDS``) gives none. Twin
    rule: the BC copy the Portal drained under a Portal order's number is that Portal
    root, one transaction: not a root of its own, it gives the Portal root its reference
    with no BC probe, and a mail naming the number names one root. N = 1/10/60 BC-only
    orders: none lost or duplicated (fixture)."""

    def drawn(self, n: int, twin: bool):
        from evals.oracle.cross_sample import portal_roots
        from evals.oracle.fixture_world import bc_record

        world = build_world()
        sources, _, _, _ = dev.build_fixture(world)
        invoiced = {key_text(r["fields"].get("externalDocumentNumber"))
                    for r in world.bc["salesInvoices"]}
        number = next(r.identifier for r in sorted(portal_roots(sources, "sales_order"),
                                                   key=lambda r: r.identifier)
                      if r.identifier not in invoiced)
        only = [bc_record("salesOrders", k % 2, 980_000 + k, number=f"FXSO8{10000 + k}",
                          customerNumber="FXC001", externalDocumentNumber=f"CPO-{k}",
                          orderDate="2098-12-01") for k in range(n)]
        world.bc["salesOrders"] += only + ([bc_record(
            "salesOrders", 0, 990_000, number=number, customerNumber="FXC001",
            orderDate="2098-12-01")] if twin else [])
        world.docs["mail"] = [{"handle": f"graph-mail://fx-box/so-{k}@x",
                               "texts": [f"Order {text} shipped", "Thanks."]}
                              for k, text in enumerate([number] + [
                                  r["fields"]["number"] for r in only])]
        world.docs["sharepoint"] = []
        sources, documents, _, _ = dev.build_fixture(world)
        return sample(sources, run_id="seed-a", count=400, documents=documents,
                      bc_sample_limit=200), number

    def test_a_bc_only_sales_order_is_a_root_and_its_portal_twin_is_one_root(self):
        for n in (1, 10, 60):
            base, _ = self.drawn(n, twin=False)
            drawn, number = self.drawn(n, twin=True)
            entry, before = drawn.kinds["sales_order"], base.kinds["sales_order"]
            with self.subTest(n=n):
                self.assertIn("bc:salesOrders", entry["tried"])
                bc = entry["funnel"]["bc:salesOrders"]
                self.assertEqual((bc["rows"], bc["portal_twin"], bc["roots"], bc["no_reference"]),
                                 (n + 1, 1, n, n), bc)
                self.assertEqual(entry["skipped"]["portal_twin"], 1)
                self.assertEqual(before["funnel"]["bc:salesOrders"]["portal_twin"], 0)
                # the BC-only orders: no record kind item; the twin's Portal root: a
                # reference read from the sampled BC copy, no probe
                items = [i for i in drawn.items if i.kind == "sales_order"]
                self.assertFalse([i for i in items if i.relation.startswith("bc:")])
                portal = entry["funnel"][K.SO]
                self.assertEqual((portal["references"], portal["from_sampled_bc"]),
                                 (before["funnel"][K.SO]["references"] + 1,
                                  before["funnel"][K.SO]["from_sampled_bc"] + 1))
                self.assertEqual([(i.relation, i.reference) for i in items
                                  if i.identifier == number], [(K.SO, "so.bc_sales_order")])
                self.assertLessEqual(entry["bc_finds"], before["bc_finds"])
                # the mail: one root per number named, the twin's being the Portal record
                mail = drawn.kinds["mail"]
                self.assertEqual((mail["references"], mail["skipped"].get("not_unique", 0)),
                                 (n + 1, 0), mail)
                named = {i.identifier: i.relation for i in drawn.items if i.kind == "mail"}
                self.assertEqual(named.pop(number), K.SO)
                self.assertEqual(len(named), n)
                self.assertEqual(set(named.values()), {"bc:salesOrders"})
                self.assertEqual({i.root_kind for i in drawn.items if i.kind == "mail"
                                  and i.relation.startswith("bc:")}, {"bc_document"})


class TypedBlockedKindTest(unittest.TestCase):
    """gap-ca4afaf72198: a kind left without items for a declared cause is
    typed blocked, per kind with its block code (mail: search refused while the directory
    consent is a later_ask; receipt: no reference while the served app_receipt reads 0
    rows). It never counts as resolved. Only receipt may stay typed blocked and pass (7 of 8
    kinds passed); while mail is typed blocked the run reads blocked with its cause, never
    pass, and an untyped failure still fails."""

    @staticmethod
    def drawn(*, refuse_mail=True, receipts_served=False, refused_by="tenant", replica=None,
              bc_finds_total=150, sales_orders=0):
        """``replica``: ``disjoint`` (BC holds only orders of another database, UAT, and no
        copy of a Portal order: gap-b01a86b2f438), ``twin`` (disjoint but one BC order is
        the Portal twin of a sampled Portal order) or ``shared`` (disjoint, but the UAT
        orders carry customer POs the synthetic Portal orders share: gap-51ebb6c78e90).
        ``sales_orders``: the Portal sales-order rows read, synthetic ones added to the
        world's up to that many."""
        world = build_world()
        orders = world.relations["public.app_sales_order"]
        for n in range(len(orders), sales_orders):
            orders.append(dict(orders[n % 5], id=f"fx-so-syn-{n}", order_number=f"FXSO5{n:05d}",
                               po_reference=f"FXCPO-SH{n % 5}" if replica == "shared"
                               else f"FXCPO-S{n:05d}", custom_fields={"fmId": 60_000 + n}))
        if replica:
            from evals.oracle.cross_sample import portal_roots
            from evals.oracle.fixture_world import bc_record

            sources, _, _, _ = dev.build_fixture(world)
            number = min(r.identifier for r in portal_roots(sources, "sales_order"))
            for n, record in enumerate(world.bc["salesInvoices"]):
                record["fields"]["externalDocumentNumber"] = f"UATIN{n:05d}"
            # shared: the UAT orders carry customer POs that Portal orders also carry
            shared = ({"externalDocumentNumber": f"FXCPO-SH{k}"} if replica == "shared"
                      else {} for k in range(5))
            world.bc["salesOrders"] += [bc_record(
                "salesOrders", k % 2, 970_000 + k, number=f"UATSO{k:05d}",
                customerNumber="FXC001", orderDate="2098-12-01", **extra)
                for k, extra in enumerate(shared)]
            if replica == "twin":
                world.bc["salesOrders"].append(bc_record(
                    "salesOrders", 0, 990_000, number=number, customerNumber="FXC001",
                    orderDate="2098-12-01"))
        rel = world.relations
        # the live shape: receipts held only as FM raw receipts, app_receipt served empty
        for n, row in enumerate(rel["public.app_receipt"]):
            rel["public.receipt_raw_material"].append({
                "id": 27000 + n, "idlabid": row["receipt_number"], "idpogenitem": None,
                "status_c": "Released", "audittimestampmodified": row["updated_at"]})
        # served: app_receipt rows are read, none carries a receipt number
        rel["public.app_receipt"] = [dict(r, receipt_number=None)
                                     for r in rel["public.app_receipt"]] \
            if receipts_served else []
        if not receipts_served:
            rel["public.app_receipt_line"] = []
        sources, _, _, _ = dev.build_fixture(world)
        docs = world.documents()

        class Refusing:   # the tenant refuses every mail search (Graph 400)
            def search(self, kind, queries, *, hits, seed=""):
                if kind == "mail" and refuse_mail:
                    return {"records": [], "source": ["fixture:mail"],
                            "results": [{"query": q, "refused": "guard" if (
                                refused_by == "mixed" and n == 0) else (
                                "tenant" if refused_by == "mixed" else refused_by)}
                                        for n, q in enumerate(queries)]}
                return docs.search(kind, queries, hits=hits, seed=seed)

            def __getattr__(self, name):
                return getattr(docs, name)

        return sample(sources, run_id="seed-a", count=28, documents=Refusing(),
                      bc_finds_total=bc_finds_total)

    def test_sales_orders_on_a_replica_disjoint_from_bc_read_blocked_by_the_replica_ask(self):
        """gap-b01a86b2f438: the reporting API's replica (staging) shares no order with BC
        (UAT). Roots read, no Portal twin, every BC probe made and none found: sales_order
        is typed blocked by the replica later_ask and the run reads blocked, never pass; a
        refused probe is budget_exhausted and fails; one Portal twin passes the kind."""
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        code = "later_ask:workbench-api-replica-target"
        report = answered(self.drawn(refuse_mail=False, replica="disjoint",
                                     bc_finds_total=10_000))
        block = report["eval"]
        self.assertEqual(block["missing_reference"]["sales_order"]["cause"],
                         "bc_source_mismatch")
        self.assertEqual(block["kinds_typed_blocked"],
                         {"receipt": "unserved:public.app_receipt", "sales_order": code})
        self.assertEqual((block["fail_reasons"], block["blocked_cause"], ev.verdict(report)),
                         ([], code, 0))
        self.assertNotIn("blocked_also", block)
        self.assertEqual(live_verify.resolve(report, spec["blocked_when"]), [code])
        self.assertEqual(spec["blocked_causes"][code], "later_ask")
        # refused probes: the cause is the budget, no replica block, the run fails
        report = answered(self.drawn(refuse_mail=False, replica="disjoint", bc_finds_total=10))
        block = report["eval"]
        self.assertEqual(block["missing_reference"]["sales_order"]["cause"], "budget_exhausted")
        self.assertGreater(block["missing_reference"]["sales_order"]["bc_probe_budget"], 0)
        self.assertNotIn("typed_block", block["by_kind"]["sales_order"])
        self.assertEqual(ev.kinds_failing(block), ["sales_order"])
        self.assertNotIn("blocked_cause", block)
        self.assertEqual(ev.verdict(report), 3)
        self.assertIn("eval.fail_reasons not empty", spec_failures(report))
        # one BC order is a Portal order's twin: its reference passes the kind
        report = answered(self.drawn(refuse_mail=False, replica="twin", bc_finds_total=10_000))
        block = report["eval"]
        self.assertEqual(block["by_kind"]["sales_order"]["status"], "passed")
        self.assertEqual((block["fail_reasons"], ev.verdict(report), spec_failures(report)),
                         ([], 0, []))
        self.assertNotIn("blocked_cause", block)

    def test_shared_customer_pos_on_uat_orders_are_misses_not_finds(self):
        """gap-51ebb6c78e90: the UAT orders in BC carry customer POs (externalDocumentNumber)
        that Portal orders share. A probe that finds such an order but yields no reference
        naming the root is a miss, not a find: the kind stops on BC_MISS_STOP misses
        (bc_miss_stop counted, no bc_probe_budget refusal), the BC source is reported missing
        (bc_source_mismatch) and sales_order is typed blocked by the replica later_ask."""
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        code = "later_ask:workbench-api-replica-target"
        drawn = self.drawn(refuse_mail=False, replica="shared", sales_orders=452)
        orders = drawn.kinds["sales_order"]
        self.assertNotIn("bc_probe_budget", orders["skipped"])
        self.assertGreater(orders["skipped"]["bc_miss_stop"], 0)
        report = answered(drawn)
        block = report["eval"]
        cause = block["missing_reference"]["sales_order"]
        self.assertEqual(cause["cause"], "bc_source_mismatch")
        self.assertGreater(cause["bc_miss_stop"], 0)
        self.assertNotIn("bc_probe_budget", cause)
        self.assertEqual(block["kinds_typed_blocked"]["sales_order"], code)
        self.assertEqual((block["fail_reasons"], block["blocked_cause"], ev.verdict(report)),
                         ([], code, 0))
        self.assertEqual(live_verify.resolve(report, spec["blocked_when"]), [code])

    def test_444_sales_orders_without_a_bc_copy_stop_on_misses_and_read_blocked(self):
        """Decision 89aed11571b0 (gap-5a015ad95402): 444 Portal sales orders, none with a BC
        copy, and 150 shared BC probes. The kind stops after BC_MISS_STOP consecutive misses
        (the rest counted bc_miss_stop, not refused by the budget), its unspent share goes to
        the later kinds, every kind keeps at least its reserved share, and the run reads
        blocked by the replica later_ask, not budget_exhausted."""
        from evals.oracle.cross_sample import BC_MISS_STOP
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        code = "later_ask:workbench-api-replica-target"
        # 452 rows read, 8 of the world's own are staging rows: 444 roots
        drawn = self.drawn(refuse_mail=False, replica="disjoint", sales_orders=452)
        orders = drawn.kinds["sales_order"]
        self.assertEqual(orders["funnel"]["public.app_sales_order"]["roots"], 444)
        self.assertEqual(orders["bc_finds"], BC_MISS_STOP)
        self.assertNotIn("bc_probe_budget", orders["skipped"])
        self.assertGreater(orders["skipped"]["bc_miss_stop"], 0)
        probers = ("bc_invoice", "sales_order", "purchase_order", "assembly_order", "receipt",
                   THREE_WAY)
        for kind in probers:
            self.assertGreaterEqual(drawn.kinds[kind]["bc_finds_allotted"],
                                    150 // len(probers), kind)
        # the share sales_order left unspent went to the kinds after it
        self.assertGreater(drawn.kinds[THREE_WAY]["bc_finds_allotted"], 150 // len(probers))
        # gap-f713876847b6: 444 sales-order roots do not crowd the mail stratum out of the
        # roots its search reads: it still draws a candidate
        self.assertEqual(drawn.kinds["mail"]["status"], "read")
        self.assertGreater(drawn.kinds["mail"]["candidates"], 0)
        report = answered(drawn)
        block = report["eval"]
        cause = block["missing_reference"]["sales_order"]
        self.assertEqual((cause["cause"], cause["bc_finds"], cause["bc_miss_stop"]),
                         ("bc_source_mismatch", BC_MISS_STOP,
                          orders["skipped"]["bc_miss_stop"]))
        self.assertNotIn("bc_probe_budget", cause)
        recorded = live_verify._recorded(spec, dict(report, evidence_label="live-read"))
        self.assertEqual(recorded["eval"]["missing_reference"]["sales_order"], cause)
        self.assertEqual(block["kinds_typed_blocked"]["sales_order"], code)
        self.assertEqual((block["fail_reasons"], block["blocked_cause"], ev.verdict(report)),
                         ([], code, 0))
        self.assertEqual(live_verify.resolve(report, spec["blocked_when"]), [code])

    def test_a_partly_failing_bc_never_reads_replica_disjoint(self):
        """gap-b01a86b2f438: a reference lookup that failed or a BC root set that could not
        be read measured no mismatch: sales_order is not typed blocked and the run fails."""
        for skip in ("reference_read_gap", "bc_read_failed"):
            drawn = self.drawn(refuse_mail=False, replica="disjoint", bc_finds_total=10_000)
            self.assertEqual(answered_kinds(drawn).get("sales_order"),
                             "later_ask:workbench-api-replica-target")
            drawn.kinds["sales_order"]["skipped"][skip] = 1
            report = answered(drawn)
            self.assertNotIn("sales_order", report["eval"]["kinds_typed_blocked"], skip)
            self.assertIn("sales_order", ev.kinds_failing(report["eval"]), skip)
            self.assertEqual(ev.verdict(report), 3, skip)

    def test_two_typed_blocks_read_blocked_by_one_enum_code_the_verifier_keeps(self):
        """Mail and sales_order typed blocked together: the verifier reads one enum-shaped
        code (no comma, at most 64 characters), the other listed beside it, both kept."""
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        report = answered(self.drawn(replica="disjoint", bc_finds_total=10_000))
        block = report["eval"]
        mail, replica = ("later_ask:graph-mail-directory-consent",
                         "later_ask:workbench-api-replica-target")
        self.assertEqual({k: block["kinds_typed_blocked"][k] for k in ("mail", "sales_order")},
                         {"mail": mail, "sales_order": replica})
        self.assertEqual((block["fail_reasons"], ev.verdict(report)), ([], 0))
        self.assertEqual((block["blocked_cause"], block["blocked_also"]), (mail, [replica]))
        blocked = live_verify.resolve(report, spec["blocked_when"])
        self.assertEqual(blocked, [mail])
        self.assertTrue(live_verify.ENUM_STRING.fullmatch(blocked[0]))
        kept = live_verify._cap({"fields": live_verify._recorded(
            spec, dict(report, evidence_label="live-read"))})
        self.assertNotIn("fields_truncated", kept)
        self.assertEqual((kept["fields"]["eval"]["blocked_cause"],
                          kept["fields"]["eval"]["blocked_also"]), (mail, [replica]))

    def test_seven_resolved_and_receipt_typed_blocked_pass_the_command_and_the_spec(self):
        report = answered(self.drawn(refuse_mail=False))
        block = report["eval"]
        self.assertEqual(block["kinds_typed_blocked"], {"receipt": "unserved:public.app_receipt"})
        self.assertEqual((block["kinds_found_exact"], block["fail_reasons"]), (7, []))
        self.assertNotIn("blocked_cause", block)
        self.assertEqual(ev.verdict(report), 0)
        self.assertEqual(spec_failures(report), [])

    def test_mail_typed_blocked_reads_blocked_with_its_cause_never_pass(self):
        report = answered(self.drawn())
        block = report["eval"]
        codes = {"mail": "later_ask:graph-mail-directory-consent",
                 "receipt": "unserved:public.app_receipt"}
        self.assertEqual(block["kinds_typed_blocked"], codes)
        # never resolved: still missing, not counted as found
        self.assertEqual(sorted(block["kinds_missing"]), ["mail", "receipt"])
        self.assertEqual(block["kinds_found_exact"], 6)
        self.assertEqual((block["by_kind"]["mail"]["status"],
                          block["by_kind"]["mail"]["reason"]), ("blocked", "search_refused"))
        self.assertEqual((block["by_kind"]["receipt"]["status"],
                          block["by_kind"]["receipt"]["reason"]), ("empty", "no_reference"))
        for kind in KINDS:
            self.assertEqual(block["by_kind"][kind].get("typed_block"), codes.get(kind), kind)
            if kind not in codes:
                self.assertEqual(block["by_kind"][kind]["status"], "passed", kind)
        # 6 passed + receipt typed + mail typed: the diagnosis stays, the verdict is blocked
        # (exit 0 and a cause the live check's blocked_when reads), never a pass
        self.assertEqual(block["blocked_cause"], "later_ask:graph-mail-directory-consent")
        self.assertEqual(ev.kinds_failing(block), ["mail"])
        self.assertEqual(block["fail_reasons"], [])
        self.assertEqual(ev.verdict(report), 0)
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        self.assertEqual(spec["blocked_when"], "eval.blocked_cause")
        self.assertEqual(live_verify.resolve(report, spec["blocked_when"]),
                         ["later_ask:graph-mail-directory-consent"])
        # the verifier keeps each kind's block code in the recorded eval block, uncut
        detail = live_verify._cap({"fields": live_verify._recorded(
            spec, dict(report, evidence_label="live-read"))})
        self.assertNotIn("fields_truncated", detail)
        kept = detail["fields"]["eval"]
        self.assertEqual({k: kept["by_kind"][k].get("typed_block") for k in codes}, codes)

    def test_one_rule_blocks_mail_here_and_in_oracle_dev(self):
        """Both commands read mail blocked by the generator's one rule (the tenant refused
        every query, nothing answered): all refused is blocked in each, a majority refused
        with documents found, or one answered, is blocked in neither."""
        from evals.oracle.fixture_world import build_live_shaped_world

        def refusing(docs, answered):
            class Port:
                max_roots = getattr(docs, "max_roots", None)

                def search(self, kind, queries, **kwargs):
                    answer = docs.search(kind, queries, **kwargs)
                    if kind == "mail":
                        answer["results"] = [
                            r if n < answered else {"query": r["query"], "refused": "tenant"}
                            for n, r in enumerate(answer["results"])]
                        if not answered:
                            answer["records"] = []
                    return answer

                def __getattr__(self, name):
                    return getattr(docs, name)
            return Port()

        for answered, blocked in ((0, True), (3, False), (1, False)):
            world = build_live_shaped_world()
            sources, _, configured, _ = dev.build_fixture(world)
            report, _ = dev.execute(sources, refusing(world.documents(), answered),
                                    run_id="live-2099-01-02T03", now=dev.FIXTURE_NOW,
                                    count=30, audit=10, id_suffix="live", evidence_note=None,
                                    evidence_label="injected-transport", configured=configured)
            in_dev = report["strata"]["mail"].get("blocked") == "upstream"
            drawn = self.drawn()   # all refused
            if answered:
                world = build_world()
                for n, row in enumerate(world.relations["public.app_receipt"]):
                    world.relations["public.receipt_raw_material"].append({
                        "id": 27000 + n, "idlabid": row["receipt_number"],
                        "idpogenitem": None, "status_c": "Released",
                        "audittimestampmodified": row["updated_at"]})
                world.relations["public.app_receipt"] = []
                world.relations["public.app_receipt_line"] = []
                sources, _, _, _ = dev.build_fixture(world)
                drawn = sample(sources, run_id="seed-a", count=28,
                               documents=refusing(world.documents(), answered))
            here = "mail" in answered_kinds(drawn)
            self.assertEqual((in_dev, here), (blocked, blocked), answered)

    def test_an_untyped_unresolved_kind_still_fails(self):
        # sharepoint unanswered beside the two typed kinds: its failure has no declared cause
        report = answered(self.drawn(), unanswered=("sharepoint",))
        block = report["eval"]
        self.assertEqual(block["kinds_found_exact"], 5)
        self.assertEqual(block["fail_reasons"], ["kinds_failing"])
        self.assertIn("sharepoint", ev.kinds_failing(block))
        self.assertNotIn("typed_block", block["by_kind"]["sharepoint"])
        # mail answered, receipt typed: 7 resolved, sharepoint untyped still fails
        report = answered(self.drawn(refuse_mail=False), unanswered=("sharepoint",))
        block = report["eval"]
        self.assertEqual(block["kinds_typed_blocked"],
                         {"receipt": "unserved:public.app_receipt"})
        self.assertEqual(block["kinds_found_exact"], 6)
        self.assertEqual(ev.kinds_failing(block), ["sharepoint"])
        self.assertEqual(ev.verdict(report), 3)
        self.assertEqual(spec_failures(report), ["exit_code 3 != 0",
                                                 "eval.kinds_found_exact < 7",
                                                 "eval.fail_reasons not empty"])

    def test_mail_refused_by_our_own_guard_is_untyped_and_fails(self):
        """A refusal by our own grammar or read-only guard is a break in our search code,
        never the directory consent ask; one guard refusal beside tenant ones also is."""
        for refused_by in ("guard", "other"):
            report = answered(self.drawn(refused_by=refused_by))
            block = report["eval"]
            self.assertEqual((block["by_kind"]["mail"]["status"],
                              block["by_kind"]["mail"]["reason"]),
                             ("blocked", "search_refused"), refused_by)
            self.assertEqual(report["kinds"]["mail"]["search_refused"],
                             {refused_by: report["kinds"]["mail"]["search_refused"][
                                 refused_by]})
            self.assertNotIn("typed_block", block["by_kind"]["mail"])
            self.assertEqual(block["kinds_typed_blocked"],
                             {"receipt": "unserved:public.app_receipt"})
            self.assertEqual(ev.kinds_failing(block), ["mail"])
            self.assertNotIn("blocked_cause", block)
            self.assertEqual(block["fail_reasons"], ["kinds_failing"])
            self.assertEqual(ev.verdict(report), 3)
            self.assertIn("eval.fail_reasons not empty", spec_failures(report))
        # one guard refusal beside tenant ones is no tenant consent ask either
        block = answered(self.drawn(refused_by="mixed"))["eval"]
        self.assertNotIn("typed_block", block["by_kind"]["mail"])
        self.assertIn("mail", ev.kinds_failing(block))

    def test_fewer_than_seven_with_mail_resolved_fails_with_the_typed_kinds(self):
        report = answered(self.drawn(), unanswered=("sales_order",))
        block = report["eval"]
        self.assertEqual(block["kinds_found_exact"], 5)
        self.assertEqual(sorted(ev.kinds_failing(block)), ["mail", "sales_order"])
        self.assertNotIn("blocked_cause", block)
        self.assertEqual(ev.verdict(report), 3)

    def test_a_receipt_without_a_reference_once_app_receipt_is_served_is_untyped(self):
        drawn = self.drawn(receipts_served=True)
        self.assertGreater(drawn.kinds["receipt"]["funnel"][K.RECEIPT]["rows"], 0)
        report = answered(drawn)
        block = report["eval"]
        self.assertEqual(block["by_kind"]["receipt"]["status"], "empty")
        self.assertNotIn("typed_block", block["by_kind"]["receipt"])
        self.assertEqual(block["kinds_typed_blocked"],
                         {"mail": "later_ask:graph-mail-directory-consent"})
        self.assertEqual(sorted(ev.kinds_failing(block)), ["mail", "receipt"])
        self.assertNotIn("blocked_cause", block)   # an untyped failure is never blocked
        self.assertEqual(ev.verdict(report), 3)

class MailScopeTest(unittest.TestCase):
    """gap-f713876847b6: mail reads the mailboxes a capture scope file names (--mail-scope,
    parsed by integrations/capture_mail.CaptureScope.from_file as --sharepoint-scope is),
    searched for the roots' numbers and then listed. Only mailboxes inside the grant are
    read; one outside it, or a read the people-exclusion guard cannot clear, is a typed,
    later_ask-backed block; a malformed or widening scope fails closed."""

    UNITS = [("mailbox-1", "first@fixture.invalid", "inbox"),
             ("mailbox-2", "second@fixture.invalid", "inbox")]

    class Mailbox:
        def __init__(self, listed, refuse=None):
            self.listed, self.refuse, self.asked = listed, refuse, []

        @contextlib.contextmanager
        def directory_cache(self):
            yield

        def list_messages(self, actor, mailbox, *, folder=None, search=None,
                          max_items=100, **kwargs):
            self.asked.append((mailbox, folder, search))
            if self.refuse is not None:
                raise self.refuse
            found = [] if search else self.listed.get(mailbox, [])
            return {"items": found[:max_items], "completeness": {
                "truncated": len(found) > max_items, "withheld_count": 0}}

    def drawn(self, box, *, units=UNITS, blocked=()):
        from evals.oracle.cross_sample import portal_roots
        from ontology.types import Actor
        world = build_world()
        sources, _, _, _ = dev.build_fixture(world)
        docs = world.documents()
        live = dev.LiveDocuments(Actor("eval", frozenset({dev.EVAL_PRINCIPAL}), "restricted"),
                                 mail_reader=box, mail_units=units, mail_blocked=blocked)
        if box.listed == {}:
            numbers = sorted(r.identifier for r in portal_roots(sources, "purchase_order"))
            box.listed["second@fixture.invalid"] = [
                {"citation": f"graph-mail://second/m-{n}@c", "body_preview": "",
                 "parent_folder_id": "fid-inbox", "subject": f"PO {number} received"}
                for n, number in enumerate(numbers[:6])]

        class Routed:   # mail from the scoped mailboxes, the rest from the fixture world
            def search(self, kind, queries, *, hits, seed=""):
                port = live if kind == "mail" else docs
                return port.search(kind, queries, hits=hits, seed=seed)

            def list_documents(self, kind, limit, *, seed=""):
                if kind == "mail":
                    return live.list_documents(kind, limit, seed=seed)
                listing = getattr(docs, "list_documents", None)
                return listing(kind, limit, seed=seed) if listing else None

            def __getattr__(self, name):
                return getattr(docs, name)

        return sample(sources, run_id="seed-a", count=28, documents=Routed())

    def test_mail_passes_from_the_listing_of_a_second_scoped_mailbox(self):
        box = self.Mailbox({})
        drawn = self.drawn(box)
        self.assertEqual({m for m, _, search in box.asked if search},
                         {"first@fixture.invalid", "second@fixture.invalid"})
        mail = drawn.kinds["mail"]
        self.assertEqual(mail["tried"], ["mail:mailbox-1/inbox", "mail:mailbox-2/inbox"])
        report = answered(drawn)
        self.assertEqual(report["eval"]["by_kind"]["mail"]["status"], "passed")
        self.assertNotIn("mail", report["eval"]["kinds_typed_blocked"])
        self.assertNotIn("fixture.invalid", json.dumps(report))
        self.assertEqual(ev.public_source("mail:mailbox-2/inbox"), "mail:mailbox-2/inbox")
        self.assertEqual(ev.public_source("mail:first@fixture.invalid/inbox"), "mail")

    def test_an_exclusion_guard_that_cannot_decide_is_a_typed_block(self):
        from integrations.people_exclusion import ExclusionUnavailable
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        code = "later_ask:excluded-people-oids"
        drawn = self.drawn(self.Mailbox(
            {"x": []}, refuse=ExclusionUnavailable("excluded_people_oids_absent")))
        report = answered(drawn)
        block = report["eval"]
        self.assertEqual(block["by_kind"]["mail"]["status"], "blocked")
        self.assertEqual(block["kinds_typed_blocked"]["mail"], code)
        self.assertEqual(spec["blocked_causes"][code], "later_ask")
        # a typed block other than TYPED_PASSES reads the run blocked by its later_ask
        self.assertEqual(ev.blocked_causes(block), [code])
        self.assertEqual(block["kinds_missing"], ["mail"])

    def test_a_mailbox_outside_the_grant_is_a_typed_block_and_never_read(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        code = "later_ask:mail-scope-grant"
        box = self.Mailbox({"x": []})
        report = answered(self.drawn(box, units=[], blocked=[("mailbox-1", "mail-scope-grant")]))
        self.assertEqual(box.asked, [])
        self.assertEqual(report["eval"]["kinds_typed_blocked"]["mail"], code)
        self.assertEqual(spec["blocked_causes"][code], "later_ask")

    def test_a_unit_or_code_outside_the_scope_vocabulary_fails_closed(self):
        from ontology.types import Actor
        actor = Actor("eval", frozenset({dev.EVAL_PRINCIPAL}), "restricted")
        box = self.Mailbox({})
        for kwargs in ({"mail_units": [("first@fixture.invalid", "x", "inbox")]},
                       {"mail_units": [("mailbox-0", "first@fixture.invalid", "inbox")]},
                       {"mail_blocked": [("mailbox-1", "graph-mail-directory-consent")]},
                       {"mail_units": self.UNITS, "mailbox": "first@fixture.invalid"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                dev.LiveDocuments(actor, mail_reader=box, **kwargs)
        # an unknown or address-keyed scope block is published as unknown or dropped, and
        # never typed blocked
        self.assertIsNone(ev.scope_block({"mailbox-1": "something-else"}))
        self.assertIsNone(ev.scope_block({}))
        self.assertEqual(ev.public_scope_blocked({"first@fixture.invalid": "mail-scope-grant",
                                                  "mailbox-2": "other"}),
                         {"mailbox-2": "unknown"})

    def test_a_blocked_mailbox_beside_a_read_one_is_an_incomplete_read_never_typed(self):
        box = self.Mailbox({})
        drawn = self.drawn(box, units=[self.UNITS[1]],
                           blocked=[("mailbox-1", "mail-scope-grant")])
        self.assertNotIn("first@fixture.invalid", {m for m, _, _ in box.asked})
        mail = drawn.kinds["mail"]
        self.assertNotIn("blocked", mail)
        # the blocked unit is counted apart, never as a refused search of the query another
        # unit answered (searches are not counted once per unit)
        self.assertGreater(mail["search"]["search_unit_scope"], 0)
        self.assertNotIn("search_refused_scope", mail["search"])
        report = answered(drawn)
        self.assertEqual(report["eval"]["by_kind"]["mail"]["status"], "passed")
        self.assertNotIn("mail", report["eval"]["kinds_typed_blocked"])
        self.assertGreater(report["kinds"]["mail"]["search_unit_unanswered"]["scope"], 0)
        self.assertNotIn("search_refused", report["kinds"]["mail"])

    def test_one_message_found_in_two_mailbox_units_is_one_hit(self):
        from ontology.types import Actor
        message = {"citation": "graph-mail://shared/m-1@c", "id": "m-1", "subject": "PO 7",
                   "body_preview": "", "parent_folder_id": "fid-inbox", "change_key": "c"}

        class Both(self.Mailbox):
            def list_messages(self, actor, mailbox, *, search=None, **kwargs):
                self.asked.append((mailbox, kwargs.get("folder"), search))
                return {"items": [dict(message)], "completeness": {"truncated": False,
                                                                   "withheld_count": 1}}

            def get_message(self, actor, mailbox, message_id, **kwargs):
                return {"change_key": "c", "body_text": "PO 7"}

        for n in (1, 10, 60):
            with self.subTest(units=n):
                box = Both({})
                units = [(f"mailbox-{i}", f"box{i}@fixture.invalid", "inbox")
                         for i in range(1, n + 1)]
                live = dev.LiveDocuments(Actor("eval", frozenset({dev.EVAL_PRINCIPAL}),
                                               "restricted"), mail_reader=box, mail_units=units)
                got = live.search("mail", ["FXCPO-1007", "FXCPO-1008"], hits=5, seed="s")
                self.assertEqual([r["handles"] for r in got["results"]],
                                 [[message["citation"]]] * 2)
                self.assertEqual([r["withheld"] for r in got["results"]], [n, n])
                self.assertEqual([r["handle"] for r in got["records"]], [message["citation"]])
                self.assertEqual(len(got["source"]), n)
                self.assertEqual(len(box.asked), 2 * n)

    def write(self, directory, document):
        path = Path(directory) / "scope.json"
        path.write_text(document if isinstance(document, str) else json.dumps(document),
                        encoding="utf-8")
        return path

    def test_the_scope_file_names_mailboxes_by_scope_id_and_sets_aside_the_ungranted(self):
        from operating.case_mail_scope import MailScope
        granted = sorted(MailScope.builtin().addresses)[0]
        with tempfile.TemporaryDirectory() as tmp:
            units, blocked = ev.mail_scope_target(self.write(tmp, {
                "schema": "plx-mail-capture-scope/v1", "mailboxes": [
                    {"address": granted, "folders": ["inbox", "sentitems"]},
                    {"address": "nobody@fixture.invalid"}]}))
        self.assertEqual(units, [("mailbox-1", granted, "inbox"),
                                 ("mailbox-1", granted, "sentitems")])
        self.assertEqual(blocked, [("mailbox-2", "mail-scope-grant")])

    def test_a_malformed_or_widening_scope_fails_closed(self):
        from operating.case_mail_scope import MailScope
        granted = sorted(MailScope.builtin().addresses)[0]
        bad = ["{not json", {"schema": "plx-mail-capture-scope/v2", "mailboxes": []},
               {"schema": "plx-mail-capture-scope/v1", "mailboxes": [{"address": "x"}]},
               {"schema": "plx-mail-capture-scope/v1", "allowlisted_mailboxes": {}},
               {"schema": "plx-mail-capture-scope/v1",
                "mailboxes": [{"address": granted}], "all_mailboxes": {"enabled": True}}]
        for document in bad:
            with tempfile.TemporaryDirectory() as tmp, self.subTest(document=document):
                path = self.write(tmp, document)
                with self.assertRaises(ValueError):
                    ev.mail_scope_target(path)
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as stop:
                    ev.main(["live", "--mail-scope", str(path)], environ={})
                self.assertEqual(stop.exception.code, 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, {"schema": "plx-mail-capture-scope/v1",
                                    "mailboxes": [{"address": granted}]})
            with contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as stop:
                ev.main(["live", "--mail-scope", str(path), "--mailbox", granted], environ={})
            self.assertEqual(stop.exception.code, 2)

    def test_an_excluded_mailbox_is_dropped_and_an_unmatched_exclusion_refuses(self):
        from operating.case_mail_scope import MailScope
        granted = sorted(MailScope.builtin().addresses)[0]
        base = {"schema": "plx-mail-capture-scope/v1", "mailboxes": [
            {"address": granted}, {"address": "other@fixture.invalid"}]}

        def exclude(*addresses):
            return {**base, "all_mailboxes": {"enabled": False, "exclude": [
                {"address": a, "reason": "owner"} for a in addresses]}}
        with tempfile.TemporaryDirectory() as tmp:
            units, blocked = ev.mail_scope_target(self.write(tmp, exclude(
                "OTHER@fixture.invalid")))
            self.assertEqual(([u[0] for u in units], blocked), (["mailbox-1"], []))
            self.assertEqual([u[1] for u in units], [granted])
            for document in (exclude("unknown@fixture.invalid"),   # matches no mailbox
                             exclude(granted, "other@fixture.invalid")):   # nothing left
                path = self.write(tmp, document)
                with self.assertRaises(ValueError):
                    ev.mail_scope_target(path)
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as stop:
                    ev.main(["live", "--mail-scope", str(path)], environ={})
                self.assertEqual(stop.exception.code, 2)

    def test_the_live_check_reads_its_mail_scope_inside_the_grant(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        args = spec["command"] if isinstance(spec["command"], list) else spec["command"].split()
        self.assertNotIn("--mailbox", args)
        path = ROOT / args[args.index("--mail-scope") + 1]
        units, blocked = ev.mail_scope_target(path)
        self.assertTrue(units)
        self.assertEqual(blocked, [])


if __name__ == "__main__":
    unittest.main()
