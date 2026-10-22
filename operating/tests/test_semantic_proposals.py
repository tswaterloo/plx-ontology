"""The one adoption path for proposed playbook versions (RSN-08, ENG-13).

Evidence label: ``isolated-fixture`` (a temporary copy of the shipped playbook directory as the
read-only layer and a temporary state directory's overlay as the adoption target, scored
by the registered ``playbook_eval.case_runner`` with ``playbook_eval.evaluate`` replaced by
synthetic reports, recorded in a real ``PlaybookReportStore`` and compared by the real
``playbook_eval.compare``) and ``injected-transport`` (``RoundTripTests``: the AO case
investigated through ``MCPServer`` over the in-process ``FakeWorkbench``, scored unreplaced).
Every adoption below is made by a test playing the human steward; no path adopts for one.
Crashes are injected at ``os.replace`` (the move).
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from ontology.core import Actor
from operating import playbook_eval as pe, playbooks
from operating import semantic_proposals as sp
from operating.playbook_reports import PlaybookReportStore, ReportIntegrityError
from operating.playbooks import PLAYBOOK_DIR, PlaybookRegistry, overlay_dir
from operating.tests.test_playbook_eval import AO, AoEvalCase

AGENT = Actor("plx", frozenset({"agent:claude"}))
EVALUATOR = Actor("plx", frozenset({"service:playbook-evaluator"}))
STEWARD = Actor("plx", frozenset({"employee:steward-1"}))
OTHER_STEWARD = Actor("plx", frozenset({"employee:steward-2"}))
ITEMS = ["case-0001", "case-0002", "case-0003"]
RUNNER = pe.PROPOSAL_RUNNER
DIGEST = "ab" * 32
NEW_FILE = "customer_po_enrichment_v4.json"
CHECKS = ("c1", "c2")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="plx-sp-test-"))
        self.addCleanup(shutil.rmtree, tmp)
        self.playbooks = tmp / "playbooks"
        shutil.copytree(PLAYBOOK_DIR, self.playbooks)
        self.reports = PlaybookReportStore(tmp / "state" / "playbook-reports.sqlite",
                                           can_read=lambda reader, case: True)
        self.overlay = overlay_dir(tmp / "state")
        # per side: "pass", "fail" (c1 fails), {"failed": [...], "violations": n, "added": [...],
        # "unread": True} ("added": further checks, all failing; "unread": an item withheld from
        # the reader), an exception to raise, or a callable of the case id giving one of those
        self.result = {"before": "pass", "after": "pass"}
        self.seen = []
        patch = mock.patch.object(pe, "evaluate", self.report)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = self.make_store()
        self.current = json.loads((self.playbooks / "customer_po_enrichment_v3.json").read_text())
        self.edit = copy.deepcopy(self.current)
        self.edit["version"] = 4
        self.edit["question_pattern"] += " (reworded)"

    def report(self, playbook, call, case_id, transcript=None, **kwargs):
        """A synthetic frozen report of ``case_id``: ``after`` when scored by the new version."""
        edited = playbook.version == self.edit["version"]
        self.seen.append((edited, case_id))
        spec = self.result["after" if edited else "before"]
        spec = spec(case_id) if callable(spec) else spec
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, str):
            spec = {"pass": {}, "fail": {"failed": ["c1"]}}[spec]
        failed = [*spec.get("failed", []), *spec.get("added", [])]
        violations = spec.get("violations", 0)
        report = {"schema": pe.REPORT_SCHEMA, "case_id": case_id,
                  "playbook": {"id": playbook.id, "version": playbook.version},
                  "definition_sha256": playbook.definition_sha256,
                  "evaluator_version": pe.EVALUATOR_VERSION,
                  "case_boundary": {"events": 4, "chain_ok": True},
                  "evidence_manifest": {"manifest_sha256": "cd" * 32, "entries": [],
                                        "unresolved": [{"cited_by": "F1", "index": 0}]
                                        if spec.get("unread") else []},
                  "checks": [{"id": c, "passed": c not in failed}
                             for c in (*CHECKS, *spec.get("added", []))],
                  "expected_gaps": [], "authority_violations": violations,
                  "summary": {"passed_all": not failed and not violations,
                              "passed_all_clean": False}}
        report["report_sha256"] = hashlib.sha256(json.dumps(
            report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return report

    def make_store(self, items=ITEMS, runners=None, overlay=True):
        stewards = ("employee:steward-1", "employee:steward-2")
        overlay = self.overlay if overlay is True else overlay
        if runners is None:
            return pe.proposal_store(self.reports, frozen_cases=list(items), stewards=stewards,
                                     shipped=self.playbooks, overlay=overlay)
        return sp.ProposalStore(self.reports, frozen_items={"playbook": items}, runners=runners,
                                stewards=stewards, shipped={"playbook": self.playbooks},
                                overlays={"playbook": overlay})

    def propose(self, actor=AGENT, **kwargs):
        args = {"kind": "playbook", "file": NEW_FILE, "content": self.edit,
                "rationale": "the question reads clearer",
                "evidence": ["api://portal/public.app_sales_order/7#po_reference"]}
        return self.store.propose(actor, **{**args, **kwargs})

    def evaluate(self, pid, actor=EVALUATOR, runner=RUNNER, **runner_args):
        return self.store.evaluate(pid, actor, runner, **{"call": None, **runner_args})

    def records(self, schema):
        with self.reports.changes() as (events, _append):
            return [e for e in events if e["schema"] == schema]

    def assert_resolves(self, digests, reports, reader="service:playbook-evaluator"):
        """Every digest names a report stored as the evaluator's, its hash intact."""
        for digest in digests:
            row = self.reports.get_verifiable(reader, reports[digest])
            self.assertEqual((row["recorded_by"], row["report"]["report_sha256"]),
                             (reader, digest))
            self.assertTrue(pe.verify_report_hash(row["report"]))

    def refused(self, code, fn, *args, **kwargs):
        with self.assertRaises(sp.ProposalRefused) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def assert_unchanged(self):
        self.assertFalse((self.overlay / NEW_FILE).exists())
        self.assertFalse((self.playbooks / NEW_FILE).exists())

    def layers(self):
        return PlaybookRegistry().load_layers(self.playbooks, self.overlay)


class AdoptionTests(Base):
    def test_a_steward_adopts_a_non_regressing_version_and_it_takes_effect_at_next_load(self):
        shipped = {p.name: p.read_bytes() for p in self.playbooks.iterdir()}
        loaded = self.layers()
        pid = self.propose()
        self.assertEqual(self.propose(), pid)  # idempotent
        comparison = self.evaluate(pid)
        self.assertEqual(sorted(self.seen),
                         sorted([(False, i) for i in ITEMS] + [(True, i) for i in ITEMS]))
        self.assertEqual((comparison["item_ids"], comparison["runner"], comparison["regression"]),
                         (ITEMS, RUNNER, False))
        self.assertEqual({i: c["regression"] for i, c in comparison["item_comparisons"].items()},
                         dict.fromkeys(ITEMS, False))
        self.assert_resolves([d for i in ITEMS for d in comparison["evidence"][i].values()],
                             comparison["reports"])
        self.assert_unchanged()  # evaluating ran on staged copies only
        event = self.store.adopt(pid, STEWARD)
        self.assertEqual((event["steward"], self.store.status(pid)),
                         ("employee:steward-1", "adopted"))
        # what was already loaded keeps the old version; the next load serves the new one from
        # the overlay, and the shipped directory is byte-identical
        self.assertEqual(loaded.get("customer_po_enrichment").version, 3)
        self.assertEqual(self.layers().get("customer_po_enrichment").version, 4)
        self.assertEqual({p.name: p.read_bytes() for p in self.playbooks.iterdir()}, shipped)
        self.assertEqual(sorted(p.name for p in self.overlay.iterdir()), [NEW_FILE])
        self.refused("already_adopted", self.store.adopt, pid, OTHER_STEWARD)
        self.assertEqual(self.reports.integrity("anyone")["changes"], 4)  # + decision, move

    def test_without_an_overlay_nothing_is_adopted_and_the_overlay_is_never_shipped_code(self):
        for code_dir in (self.playbooks, PLAYBOOK_DIR, PLAYBOOK_DIR / "sub", PLAYBOOK_DIR.parent):
            with self.assertRaises(ValueError):
                self.make_store(overlay=code_dir)
        self.store = self.make_store(overlay=None)
        pid = self.propose()
        self.evaluate(pid)   # proposing and evaluating need no state directory
        self.refused("adoption_directory_not_configured", self.store.check_adoption, pid, STEWARD)
        self.refused("adoption_directory_not_configured", self.store.adopt, pid, STEWARD)
        self.assertEqual(self.store.status(pid), "evaluated")
        self.assertFalse(self.overlay.exists())
        self.assert_unchanged()

    def test_agents_proposers_and_evaluators_never_adopt(self):
        pid = self.propose()
        self.refused("evaluator_is_proposer", self.evaluate, pid, AGENT)
        human_evaluator = Actor("plx", frozenset({"employee:steward-2"}))
        self.evaluate(pid, human_evaluator)
        for actor, code in (
                (AGENT, "steward_must_be_one_human_employee"),
                (EVALUATOR, "steward_must_be_one_human_employee"),
                (Actor("plx", frozenset({"employee:steward-1", "agent:claude"})),
                 "steward_must_be_one_human_employee"),
                (Actor("plx", frozenset({"employee:someone"})), "not_a_steward"),
                (human_evaluator, "steward_is_evaluator")):
            self.refused(code, self.store.adopt, pid, actor)
        human_pid = self.propose(actor=STEWARD, rationale="the steward's own edit")
        self.evaluate(human_pid)
        self.refused("steward_is_proposer", self.store.adopt, human_pid, STEWARD)
        self.assert_unchanged()
        self.assertEqual(self.store.status(pid), "evaluated")

    def test_only_a_registered_runner_whose_results_bind_recorded_evidence_reaches_evaluated(self):
        pid = self.propose()
        self.refused("unregistered_runner", self.evaluate, pid, runner="another-runner")
        self.refused("unregistered_runner", self.evaluate, pid, runner=pe.case_runner)
        side = {"outcome": "pass", "evidence_sha256": DIGEST}
        bound = {"regression": False, "from": {"report_sha256": DIGEST},
                 "to": {"report_sha256": DIGEST}}
        for result, code in (({"before": {"outcome": "pass"}, "after": side, "comparison": bound},
                              "bad_item_result"),
                             ({"before": side, "after": {**side, "outcome": "skipped"},
                               "comparison": bound}, "bad_item_result"),
                             ({"before": side, "after": side, "comparison": bound},
                              "evidence_not_recorded")):
            self.store = self.make_store(runners={"playbook": {
                RUNNER: lambda proposal, record, **_: lambda *args: result}})
            self.refused(code, self.evaluate, pid)
        def broken(*args):
            raise TimeoutError("item run timed out")
        self.store = self.make_store(runners={"playbook": {RUNNER: lambda *_, **__: broken}})
        with self.assertRaises(TimeoutError):
            self.evaluate(pid)
        self.assertEqual(self.store.status(pid), "proposed")
        self.refused("no_comparison", self.store.adopt, pid, STEWARD)
        # a kind without a runner: neither proposable nor evaluable
        for kind in ("generator_override", "ontology_plan", "schema"):
            self.refused("no_runner_for_kind", self.propose, kind=kind, file="markings.json")
        self.store = self.make_store(runners={})
        self.refused("no_runner_for_kind", self.propose, rationale="another edit")
        self.refused("no_runner_for_kind", self.evaluate, pid)
        self.assertEqual(self.store.status(pid), "proposed")
        # a comparison by a runner no longer registered is not adoptable
        self.store = self.make_store()
        self.evaluate(pid)
        self.store = self.make_store(runners={"playbook": {"newer-runner": lambda *_, **__: None}})
        self.refused("unregistered_runner", self.store.adopt, pid, STEWARD)
        self.assert_unchanged()

    def competing(self):
        """Two evaluated proposals of the same new file with different content."""
        other = copy.deepcopy(self.edit)
        other["question_pattern"] += " (another wording)"
        pids = [self.propose(), self.propose(content=other, rationale="another wording")]
        for pid in pids:
            self.evaluate(pid)
        return pids, {pids[0]: self.edit, pids[1]: other}

    def held(self):
        return json.loads((self.overlay / NEW_FILE).read_text())

    def test_concurrent_adoptions_of_one_target_adopt_exactly_one(self):
        pids, contents = self.competing()
        # each thread's first write transaction is its decision, the second its move: no move
        # starts before both stewards have decided, so the second decision sees the first one
        # recorded but not yet moved, which only the pending-adoption rule refuses
        real_changes, calls = self.reports.changes, Counter()
        decided = {pid: threading.Event() for pid in pids}

        @contextmanager
        def changes():
            me = threading.current_thread().name
            calls[me] += 1
            deciding = calls[me] == 1
            if not deciding:
                for event in decided.values():
                    event.wait(10)
            try:
                with real_changes() as opened:
                    yield opened
            finally:
                if deciding:
                    decided[me].set()
        start, outcomes = threading.Barrier(2), {}
        def adopt(pid, steward):
            start.wait()
            try:
                outcomes[pid] = self.store.adopt(pid, steward)["proposal_id"]
            except sp.ProposalRefused as refused:
                outcomes[pid] = refused.code
        with mock.patch.object(self.reports, "changes", changes):
            threads = [threading.Thread(target=adopt, args=(pid, steward), name=pid)
                       for pid, steward in zip(pids, (STEWARD, OTHER_STEWARD))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        winner = [pid for pid in pids if outcomes.get(pid) == pid]
        self.assertEqual(len(winner), 1, outcomes)
        loser = next(pid for pid in pids if pid != winner[0])
        self.assertEqual(outcomes[loser], "adoption_pending")
        self.assertEqual([self.store.status(pid) for pid in (winner[0], loser)],
                         ["adopted", "evaluated"])
        self.assertEqual(self.held(), contents[winner[0]])
        self.assertEqual(sorted(p.name for p in self.overlay.iterdir() if p.name.startswith(".")),
                         [])  # no temp file left behind

    def test_a_crash_between_recording_and_moving_recovers_on_retry(self):
        pids, contents = self.competing()
        real_replace = os.replace
        def crash_before(src, dst):
            raise OSError("crash before the move")
        def crash_after(src, dst):
            real_replace(src, dst)
            raise OSError("crash after the move, before its confirmation")
        for crash in (crash_before, crash_after):
            with mock.patch.object(sp.os, "replace", crash), self.assertRaises(OSError):
                self.store.adopt(pids[0], STEWARD)
            self.assertEqual(self.store.status(pids[0]), "adoption_pending")
            # the competing proposal is never recorded adopted while the first one is pending
            self.refused("adoption_pending", self.store.adopt, pids[1], OTHER_STEWARD)
        self.assertEqual(self.held(), contents[pids[0]])  # moved, not yet confirmed
        event = self.store.adopt(pids[0], OTHER_STEWARD)  # any steward completes it
        self.assertEqual((event["steward"], self.store.status(pids[0])),
                         ("employee:steward-1", "adopted"))
        # the confirmation names what the file held before the first move, not after it
        self.assertEqual([(a["applied_sha256"], a["replaced_sha256"])
                          for a in self.records(sp.APPLIED)], [(event["adopted_sha256"], None)])
        self.refused("already_adopted", self.store.adopt, pids[0], STEWARD)
        # an edit of the adopted file is never overwritten by a re-adoption
        (self.overlay / NEW_FILE).write_bytes(b"{}")
        self.refused("target_changed_since_adoption", self.store.adopt, pids[0], STEWARD)
        self.assertEqual((self.overlay / NEW_FILE).read_bytes(), b"{}")
        self.refused("target_changed_since_proposal", self.store.adopt, pids[1], OTHER_STEWARD)
        self.assertEqual(self.store.status(pids[1]), "evaluated")

    def test_adoption_needs_a_comparison_on_the_same_frozen_items_and_target(self):
        pid = self.propose()
        self.evaluate(pid)
        # the frozen set moved on (a case dropped): the comparison no longer covers it
        self.store = self.make_store(items=ITEMS[:2])
        self.refused("item_set_differs", self.store.adopt, pid, STEWARD)
        self.store = self.make_store()
        (self.playbooks / "customer_po_enrichment_v3.json").write_bytes(
            (self.playbooks / "customer_po_enrichment_v3.json").read_bytes() + b"\n")
        self.refused("target_changed_since_proposal", self.store.adopt, pid, STEWARD)
        self.assert_unchanged()

    def test_any_regressing_comparison_of_the_edit_refuses_adoption(self):
        """compare's rule, not the case outcome: a case failing on both sides whose after side
        adds a newly failing check or an authority violation regresses, and no later passing
        comparison, with other runner arguments or under another proposal, runs it away."""
        for n, after in enumerate(({"failed": ["c1", "c2"]}, {"failed": ["c2"], "violations": 1})):
            edit = copy.deepcopy(self.edit)
            edit["question_pattern"] += f" ({n})"
            pid = self.propose(content=edit)
            self.result.update(before={"failed": ["c2"]}, after=after)
            regressing = self.evaluate(pid)
            self.assertEqual((regressing["before"], regressing["after"]),
                             (dict.fromkeys(ITEMS, "fail"), dict.fromkeys(ITEMS, "fail")))
            self.assertEqual((regressing["regressed_items"], regressing["regression"]),
                             (ITEMS, True))
            self.refused("comparison_regresses", self.store.adopt, pid, STEWARD)
            self.result.update(before="pass", after="pass")
            later = self.evaluate(pid, transcripts={ITEMS[0]: ["plx.case_get"]})
            self.assertFalse(later["regression"])
            self.assertNotEqual(later["runner_args"], regressing["runner_args"])
            self.refused("comparison_regresses", self.store.adopt, pid, STEWARD)
            again = self.propose(content=edit, rationale="the same edit, proposed again")
            self.evaluate(again)
            self.refused("comparison_regresses", self.store.adopt, again, STEWARD)
        self.assert_unchanged()

    def test_a_regression_blocks_the_same_rule_whatever_the_folder_file_wording_and_version(self):
        """The same edit is its content or its rule: an unrelated file added to the directory,
        another file name, other runner arguments, reworded descriptive fields or a later
        version number never re-propose a regressing edit into adoption, while a changed rule
        in the same directory is adopted."""
        regressing = self.propose()
        self.result.update(after="fail")
        self.assertTrue(self.evaluate(regressing)["regression"])
        self.result.update(after="pass")
        unrelated = json.loads((self.playbooks / "ao_quantity_reconciliation_v4.json").read_text())
        unrelated["version"] = 5
        (self.playbooks / "ao_quantity_reconciliation_v5.json").write_text(json.dumps(unrelated))
        for file in (NEW_FILE, "customer_po_enrichment_v4_again.json"):
            again = self.propose(file=file, rationale="the same edit, another folder state")
            self.assertNotEqual(again, regressing)
            later = self.evaluate(again, transcripts={ITEMS[0]: ["plx.case_get"]})
            self.assertFalse(later["regression"])
            self.assertNotEqual(later["tree_sha256"], self.records(sp.PROPOSAL)[0]["tree_sha256"])
            self.refused("comparison_regresses", self.store.adopt, again, STEWARD)
        # nor does a byte change no evaluation reads: wording, defaults spelled out, a version
        reworded, bumped = copy.deepcopy(self.edit), copy.deepcopy(self.edit)
        reworded["expected_gaps"][0]["about"] += " (reworded)"
        reworded["required_evidence"][0]["key_hint"] += " (reworded)"
        reworded["question_pattern"] += " (again)"
        reworded["expected_gaps"][0]["match"]["cites"] = reworded["expected_gaps"][0][
            "match"].get("cites", [])
        bumped["version"] = 5
        for n, cosmetic in enumerate((reworded, bumped)):
            again = self.propose(content=cosmetic, file=f"customer_po_enrichment_c{n}.json")
            self.assertFalse(self.evaluate(again)["regression"])
            self.refused("comparison_regresses", self.store.adopt, again, STEWARD)
        other = copy.deepcopy(self.edit)
        other["success_checks"].append(
            {"id": "po_evidence_cited", "type": "evidence_cited", "params": {"role": "po_evidence"}})
        different = self.propose(content=other, rationale="a different rule")
        self.evaluate(different)
        self.store.adopt(different, STEWARD)
        self.assertEqual(self.held(), other)

    def test_a_legacy_proposal_without_rule_sha256_matches_by_its_computed_rule(self):
        """A proposal recorded before ``rule_sha256`` existed is the same edit as a reworded one
        of the same rule, in both directions: its regression blocks the reworded version, and a
        legacy reworded version is blocked by a recorded regression."""
        changes = self.reports.changes

        @contextmanager
        def legacy():
            with changes() as (events, append):
                yield events, lambda event: append(
                    {k: v for k, v in event.items() if k != "rule_sha256"})
        with mock.patch.object(self.reports, "changes", legacy):
            regressing = self.propose()
        self.assertNotIn("rule_sha256", self.records(sp.PROPOSAL)[0])
        # its computed rule is pinned to the fields dropped when the rule was introduced
        record = self.records(sp.PROPOSAL)[0]
        rule = sp._rule_sha256(record)
        with mock.patch.object(playbooks, "DESCRIPTIVE_FIELDS",
                               playbooks.DESCRIPTIVE_FIELDS + ("success_checks",)):
            self.assertEqual(sp._rule_sha256(record), rule)
            self.assertNotEqual(sp._rule(record["content"]), rule)
        self.result.update(after="fail")
        self.assertTrue(self.evaluate(regressing)["regression"])
        self.result.update(after="pass")
        reworded = copy.deepcopy(self.edit)
        reworded["question_pattern"] += " (again)"
        again = self.propose(content=reworded, file="customer_po_enrichment_c0.json")
        self.assertFalse(self.evaluate(again)["regression"])
        self.refused("comparison_regresses", self.store.adopt, again, STEWARD)
        reworded["required_evidence"][0]["key_hint"] += " (again)"
        with mock.patch.object(self.reports, "changes", legacy):
            legacy_again = self.propose(content=reworded, file="customer_po_enrichment_c1.json")
        self.assertFalse(self.evaluate(legacy_again)["regression"])
        self.refused("comparison_regresses", self.store.adopt, legacy_again, STEWARD)
        self.assert_unchanged()

    def test_a_case_no_reader_can_read_is_left_out_and_only_no_scorable_case_refuses(self):
        """A case read in part whose host reasons say no reader can read it (the runner's
        ``unscorable`` probe) is unscorable on both sides and out of the comparison; with no
        host reason it stays unscored and blocks; a reason this module does not know fails
        closed; adoption refuses only when every frozen case is unscorable."""
        first_unread = {"before": lambda case_id: {"unread": case_id == ITEMS[0]},
                        "after": lambda case_id: {"unread": case_id == ITEMS[0]}}
        hidden = {"before": {"unread": True}, "after": {"unread": True}}
        rebuilt = lambda case_id: ["mirror_rebuilt"]   # noqa: E731
        pid = self.propose()
        self.result = hidden
        self.evaluate(pid, unscorable=rebuilt)
        self.refused("no_scorable_case", self.store.adopt, pid, STEWARD)
        self.result = first_unread
        self.refused("bad_item_result", self.evaluate, pid, unscorable=lambda case_id: ["gone"])
        self.evaluate(pid, unscorable=lambda case_id: [])
        self.refused("comparison_unscored", self.store.adopt, pid, STEWARD)
        comparison = self.evaluate(pid, unscorable=rebuilt)
        self.assertEqual(([comparison[side][ITEMS[0]] for side in ("before", "after")],
                          comparison["unscorable"], comparison["unscored_items"]),
                         (["unscorable"] * 2, {ITEMS[0]: ["mirror_rebuilt"]}, []))
        self.store.adopt(pid, STEWARD)
        self.assertEqual(self.store.status(pid), "adopted")

    def test_a_mirror_rebuild_never_shrinks_the_regression_set_below_half(self):
        """Once any frozen case is unscorable, adoption needs at least half of the frozen cases
        scored (``MIN_SCORED_SHARE``): 1 scored of 10 with 9 ``mirror_rebuilt`` is refused with
        a typed reason, 5 of 10 adopts; a fully scored set adopts whatever its size."""
        items = [f"case-{n:04d}" for n in range(10)]
        rebuilt = lambda case_id: ["mirror_rebuilt"]   # noqa: E731
        for scored, refusal in ((1, "too_few_scored_cases"), (4, "too_few_scored_cases"),
                                (5, None), (10, None)):
            with self.subTest(scored=scored):
                self.reports = PlaybookReportStore(self.overlay.parent / f"reports-{scored}.sqlite",
                                                   can_read=lambda reader, case: True)
                self.store = self.make_store(items=items)
                pid = self.propose()
                unread = lambda case_id: {"unread": case_id not in items[:scored]}  # noqa: E731
                self.result = {"before": unread, "after": unread}
                comparison = self.evaluate(pid, unscorable=rebuilt)
                self.assertEqual((len(comparison["unscorable"]), comparison["unscored_items"]),
                                 (10 - scored, []))
                if refusal:
                    with self.assertRaises(sp.ProposalRefused) as caught:
                        self.store.adopt(pid, STEWARD)
                    self.assertEqual((caught.exception.code, str(caught.exception)),
                                     (refusal, f"proposal_refused:{refusal}: {scored} of 10 "
                                               f"frozen cases scored, {10 - scored} unscorable"))
                    self.assert_unchanged()
                    continue
                self.store.adopt(pid, STEWARD)
                self.assertEqual(self.store.status(pid), "adopted")
                (self.overlay / NEW_FILE).unlink()

    def test_compare_regresses_when_the_case_no_longer_passes_as_a_whole(self):
        """compare's passed_all clause: a stricter version adding a check the case fails
        regresses with no shared check newly failing; a case failing on both sides, or newly
        passing, does not regress by it."""
        for before, after, regression in (("pass", {"added": ["c3"]}, True),
                                          ("fail", {"failed": ["c1"], "added": ["c3"]}, False),
                                          ("fail", "pass", False)):
            edit = copy.deepcopy(self.edit)
            edit["question_pattern"] += f" ({before}, {after})"
            pid = self.propose(content=edit)
            self.result.update(before=before, after=after)
            comparison = self.evaluate(pid)
            item = comparison["item_comparisons"][ITEMS[0]]
            self.assertEqual((item["newly_failing"], item["removed_passing_checks"],
                              item["authority_violations"]["new"]), ([], [], 0))
            self.assertEqual((item["passed_all"]["old"], item["passed_all"]["new"],
                              comparison["regression"]),
                             (before == "pass", after == "pass", regression))
            if regression:
                self.assertEqual(item["added_checks"], ["c3"])
                self.refused("comparison_regresses", self.store.adopt, pid, STEWARD)
        self.assert_unchanged()

    def test_a_failed_evaluation_keeps_its_reports_runner_args_and_regression(self):
        pid = self.propose()
        self.result["after"] = lambda i: {ITEMS[0]: "fail", ITEMS[1]: pe.EvaluationError(
            "evaluation_incomplete")}.get(i, "pass")
        with self.assertRaises(pe.EvaluationError):
            self.evaluate(pid, transcripts={ITEMS[1]: ["plx.case_get"]})
        [failed] = self.records(sp.EVALUATION_FAILED)
        self.assertEqual((failed["error"], failed["regressed_items"], failed["regression"],
                          sorted(failed["runner_args"])),
                         ("EvaluationError", [ITEMS[0]], True, ["call", "transcripts"]))
        # the first case on both sides and the second case's before side were recorded
        self.assertEqual(len(failed["reports"]), 3)
        self.assert_resolves(failed["reports"], failed["reports"])
        self.assertEqual(self.store.status(pid), "proposed")
        self.result["after"] = "pass"
        self.assertFalse(self.evaluate(pid)["regression"])
        self.refused("comparison_regresses", self.store.adopt, pid, STEWARD)
        self.assert_unchanged()

    def test_a_playbook_version_that_weakens_checks_is_refused_whatever_the_items_say(self):
        self.refused("published_playbook_version_is_immutable", self.propose,
                     file="customer_po_enrichment_v3.json", content=self.current)
        weaker = copy.deepcopy(self.edit)
        weaker["success_checks"] = [c for c in weaker["success_checks"] if c["id"] != "facts_cite"]
        pid = self.propose(content=weaker)
        comparison = self.evaluate(pid)
        self.assertEqual((comparison["regressed_items"], comparison["playbook_reasons"]),
                         ([], ["check_removed:facts_cite"]))
        self.refused("comparison_regresses", self.store.adopt, pid, STEWARD)
        self.assert_unchanged()

    def test_unknown_or_invalid_input_and_a_broken_record_chain_fail_closed(self):
        self.refused("bad_target_file", self.propose, file="../customer_po_enrichment_v4.json")
        self.refused("evidence_required", self.propose, evidence=[])
        bad = copy.deepcopy(self.edit)
        bad["success_checks"][0]["type"] = "no_such_check"
        self.refused("content_invalid", self.propose, content=bad)
        # a rule that cannot be computed is the same typed refusal, never a raw ValueError
        with mock.patch.object(sp, "behavioural_body", side_effect=ValueError("no rule")):
            self.refused("content_invalid", self.propose)
        self.assertEqual(self.reports.integrity("anyone")["changes"], 0)
        pid = self.propose()
        self.result["before"] = "fail"
        self.evaluate(pid)
        db = sqlite3.connect(self.reports.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):  # append-only
                db.execute("UPDATE playbook_change SET record_json='{}'")
            db.execute("DROP TRIGGER playbook_change_no_update")
            db.execute("UPDATE playbook_change SET record_json=replace(record_json,"
                       "'\"after\":{','\"after\":{\"x\":\"pass\",') WHERE id=2")
            db.commit()
        finally:
            db.close()
        self.assertFalse(self.reports.integrity("anyone")["ok"])
        with self.assertRaises(ReportIntegrityError):
            self.store.adopt(pid, STEWARD)
        self.assert_unchanged()


class RoundTripTests(AoEvalCase):
    def test_the_case_runner_scores_two_versions_and_only_the_non_regressing_one_is_adopted(self):
        """ENG-13 through the registered runner: the evaluator scores the frozen case under the
        current and each proposed version; a sum-for-difference version regresses and is
        refused, a reworded one passes and a steward adopts it."""
        case_id = self.investigate()["case_id"]
        broken = copy.deepcopy(AO.definition)
        broken["version"] = 2
        for item in broken["required_computations"]:
            if item["name"] == "planned_minus_manufactured":
                item["expression"] = "planned + manufactured"
        reworded = copy.deepcopy(AO.definition)
        reworded["version"] = 2
        reworded["question_pattern"] += " (reworded)"
        steward = Actor("plx", frozenset({"employee:steward"}))
        evaluator = Actor("plx", frozenset({"service:evaluator"}))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "shipped"
            folder.mkdir()
            (folder / "ao_v1.json").write_text(json.dumps(AO.definition))
            reports = PlaybookReportStore(
                Path(tmp) / "reports.sqlite",
                can_read=lambda reader, case: reader == "service:evaluator" and case == case_id)
            store = pe.proposal_store(reports, frozen_cases=[case_id],
                                      stewards=["employee:steward"], shipped=folder,
                                      overlay=overlay_dir(tmp))
            pids = [store.propose(AGENT, kind="playbook", file="ao_v2.json", content=content,
                                  rationale=why, evidence=["a case finding"])
                    for content, why in ((broken, "sum, not difference"), (reworded, "clearer"))]
            # an evaluator outside the case audience cannot store its reports: no comparison
            with self.assertRaises(PermissionError):
                store.evaluate(pids[0], Actor("plx", frozenset({"service:outsider"})),
                               pe.PROPOSAL_RUNNER, call=self.call)
            self.assertEqual(store.status(pids[0]), "proposed")
            outcomes = []
            for pid in pids:
                comparison = store.evaluate(pid, evaluator, pe.PROPOSAL_RUNNER, call=self.call)
                self.assertEqual(comparison["runner"], pe.PROPOSAL_RUNNER)
                self.assertEqual(sorted(comparison["evidence"][case_id]), ["after", "before"])
                outcomes.append((comparison["before"][case_id], comparison["after"][case_id]))
                # every digest resolves to a report stored as the evaluator's, hash intact
                for side, digest in comparison["evidence"][case_id].items():
                    row = reports.get_verifiable("service:evaluator",
                                                 comparison["reports"][digest])
                    self.assertEqual((row["recorded_by"], row["report"]["report_sha256"],
                                      row["report"]["playbook"]["version"]),
                                     ("service:evaluator", digest, 1 if side == "before" else 2))
            self.assertEqual(outcomes, [("pass", "fail"), ("pass", "pass")])
            with self.assertRaises(sp.ProposalRefused) as caught:
                store.adopt(pids[0], steward)
            self.assertEqual(caught.exception.code, "comparison_regresses")
            self.assertFalse(overlay_dir(tmp).exists())
            store.adopt(pids[1], steward)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["ao_v1.json"])
            adopted = PlaybookRegistry().load_layers(folder, overlay_dir(tmp)).get(AO.id)
            self.assertEqual((adopted.version, adopted.definition["question_pattern"]),
                             (2, reworded["question_pattern"]))


if __name__ == "__main__":
    unittest.main()
