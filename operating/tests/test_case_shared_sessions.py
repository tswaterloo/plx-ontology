"""Shared case sessions (the acceptance bar) on the real case store: sessions working one case at
once record a finding once, keep their distinct findings, lose no write in a real
multi-process race, and a session opening the case reads the journal tail and what the
others wrote since its last open. Synthetic fixtures only."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import time
import unittest

from devloop import live_verify
from ontology.core import Actor, ValidationError
from operating.cases import CaseNotFound, CaseStore, finding_key, supersede_refusal
from operating.tests.test_cases import AO, ENG, ENG_OPS, PACK, CaseTestBase

BOTH = ["s0", "s1", "s2", "s3"]


def _writer(args: tuple[str, str, str, int, float]) -> list[bool]:
    path, case_id, session, count, start = args
    store = CaseStore(path)
    actor = Actor("plx", frozenset({"eng", "ops"}))
    while time.time() < start:
        pass
    merged = []
    for n in range(count):
        merged.append(store.finding(actor, case_id, f"shared {n}", "interpretation",
                                    "unknown")["merged"])
        store.finding(actor, case_id, f"own {session} {n}", "interpretation", "unknown")
    return merged


class SharedCaseSessionsTest(CaseTestBase):
    def test_two_sessions_merge_the_same_finding_and_a_third_reads_both(self):
        case_id = self.open_case()
        first = self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported",
                                   [AO, PACK])
        second = self.store.finding(ENG_OPS, case_id, "  ordered 1920  ea ", "fact",
                                    "supported", [PACK, AO])
        self.assertEqual((first["merged"], second["merged"]), (False, True))
        self.assertEqual((second["finding_id"], second["key"]), (first["finding_id"], first["key"]))
        # A different verdict, citation set or kind is a different finding and is kept.
        self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "contradicted", [AO, PACK])
        self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        self.store.finding(ENG_OPS, case_id, "Packs damaged?", "interpretation", "unknown")
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual(sum(len(b) for b in view["findings"].values()), 4)
        opened = self.store.read_tail(ENG_OPS, case_id, "third")
        self.assertEqual([f["id"] for f in opened["tail"]], ["F1", "F2", "F3", "F4"])
        self.assertEqual(opened["unread_total"], 4)
        self.assertFalse(opened["resumed"])
        other = self.open_case()
        self.assertNotEqual(self.store.finding(ENG_OPS, other, "Ordered 1920 EA", "fact",
                                               "supported", [AO, PACK])["key"], first["key"])

    def test_a_finding_never_merges_across_author_kinds(self):
        def write(_who, kind):
            return self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported",
                                      [AO], author_kind=kind)

        case_id = self.open_case()
        agent_first = write("a", "agent")
        person_repeat = write("p", "human")
        self.assertEqual((agent_first["merged"], person_repeat["merged"]), (False, False))
        self.assertNotEqual(agent_first["finding_id"], person_repeat["finding_id"])
        self.assertTrue(write("a2", "agent")["merged"])      # same author kind still merges
        self.assertEqual(write("h2", "human")["finding_id"], person_repeat["finding_id"])
        # The agent cannot erase the person's assertion.
        found = {f["id"]: f for f in self.store.get(ENG_OPS, case_id, authors=True)
                 ["findings"]["supported"]}
        self.assertEqual(supersede_refusal(found[person_repeat["finding_id"]], ENG_OPS, "agent"),
                         "supersede_needs_human_author")
        with self.assertRaises(ValidationError):
            self.store.supersede(ENG_OPS, case_id, person_repeat["finding_id"], "same",
                                 author_kind="agent")
        # Person first, then agent: two findings as well.
        other = self.open_case()
        first = self.store.finding(ENG_OPS, other, "Packs damaged", "interpretation", "unknown",
                                   author_kind="human")
        second = self.store.finding(ENG_OPS, other, "Packs damaged", "interpretation", "unknown",
                                    author_kind="agent")
        self.assertNotEqual(first["finding_id"], second["finding_id"])
        # Workbench-composed text and caller text stay apart.
        composed = self.store.finding(ENG_OPS, other, "Packs damaged", "interpretation",
                                      "unknown", author_kind="agent", workbench_statement=True)
        self.assertFalse(composed["merged"])

    def test_dedupe_covers_one_recording_actor_only(self):
        case_id = self.open_case()
        text = ("Ordered 1920 EA", "fact", "supported", [AO])
        a1 = self.store.finding(ENG_OPS, case_id, *text, author_kind="agent")
        b1 = self.store.finding(ENG, case_id, *text, author_kind="agent")
        self.assertEqual((a1["merged"], b1["merged"]), (False, False))
        self.assertNotEqual(a1["finding_id"], b1["finding_id"])
        # Same actor repeats still merge.
        self.assertTrue(self.store.finding(ENG_OPS, case_id, *text, author_kind="agent")["merged"])
        self.assertEqual(self.store.finding(ENG, case_id, *text, author_kind="agent")["finding_id"],
                         b1["finding_id"])
        # A's agent may not supersede B's finding (as on base); each corrects its own.
        with self.assertRaises(ValidationError) as refused:
            self.store.supersede(ENG_OPS, case_id, b1["finding_id"], "x", author_kind="agent")
        self.assertIn("supersede_needs_human_author", str(refused.exception))
        self.store.supersede(ENG_OPS, case_id, a1["finding_id"], "own fix", author_kind="agent")
        self.store.supersede(ENG, case_id, b1["finding_id"], "own fix", author_kind="agent")

    def test_a_recheck_on_changed_row_content_is_a_new_finding(self):
        case_id = self.open_case()
        first = self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        same = self.store.finding(ENG_OPS, case_id, "ordered 1920 ea", "fact", "supported", [AO])
        self.assertEqual((same["merged"], same["finding_id"]), (True, first["finding_id"]))
        self.resolver.rows[AO][0]["row"]["manufactured"] = "1510"
        again = self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        self.assertFalse(again["merged"])
        self.assertNotEqual(again["finding_id"], first["finding_id"])
        view = self.store.get(ENG_OPS, case_id)["findings"]
        self.assertEqual([f["id"] for f in view["supported"]], [again["finding_id"]])
        self.assertEqual([f["id"] for f in view["citation_content_changed"]], [first["finding_id"]])
        # The re-checked finding is itself the merge target for the new content.
        self.assertTrue(self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact",
                                           "supported", [AO])["merged"])

    def test_a_superseded_finding_is_not_a_merge_target(self):
        case_id = self.open_case()
        old = self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        new = self.store.finding(ENG_OPS, case_id, "Ordered 1900 EA", "fact", "supported", [AO])
        self.store.supersede(ENG_OPS, case_id, old["finding_id"], "typo",
                             replaced_by=new["finding_id"])
        again = self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        self.assertFalse(again["merged"])
        self.assertFalse(self.store.finding(ENG_OPS, case_id, "Ordered 1900 EA", "fact",
                                            "supported", [AO], merge=False)["merged"])

    def test_resumed_session_reads_only_what_came_after_its_last_open(self):
        case_id = self.open_case()
        self.store.finding(ENG_OPS, case_id, "one", "interpretation", "unknown")
        self.assertEqual(self.store.read_tail(ENG_OPS, case_id, "s1")["unread_total"], 1)
        self.store.finding(ENG_OPS, case_id, "two", "interpretation", "unknown")
        resumed = self.store.read_tail(ENG_OPS, case_id, "s1")
        self.assertEqual([f["statement"] for f in resumed["unread"]], ["two"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(self.store.read_tail(ENG_OPS, case_id, "s1")["unread"], [])
        self.assertEqual(self.store.read_tail(ENG_OPS, case_id, "s2")["unread_total"], 2)

    def test_tail_is_trimmed_to_the_reader(self):
        case_id = self.open_case()
        self.store.finding(ENG_OPS, case_id, "Packs damaged", "fact", "supported", [PACK])
        self.assertEqual(self.store.read_tail(ENG_OPS, case_id, "s1")["tail"][0]["statement"],
                         "Packs damaged")
        self.resolver.rows[PACK][1].clear()          # nobody may read the cited row now
        withheld = self.store.read_tail(ENG_OPS, case_id, "s2")["tail"][0]
        self.assertIsNone(withheld.get("statement"))
        self.assertTrue(withheld["content"].startswith("withheld"))
        with self.assertRaises(CaseNotFound):
            self.store.read_tail(Actor("plx", frozenset({"finance"})), case_id, "s3")
        with self.assertRaises(CaseNotFound):
            self.store.read_tail(ENG_OPS, "case-missing", "s3")

    def test_live_check_argv_passes_its_expectations_on_its_real_output(self):
        spec = json.loads(Path("devloop/live-checks/session.shared_case.json").read_text("utf-8"))
        done = subprocess.run(spec["command"], capture_output=True, text=True, timeout=120)
        document = json.loads(done.stdout)
        self.assertEqual(live_verify.check_expectations(spec, done.returncode, document), [])
        self.assertNotIn("Quantity", done.stdout)

    def test_concurrent_processes_record_each_finding_once(self):
        case_id = self.open_case()
        count = 12
        start = time.time() + 1.0
        with ProcessPoolExecutor(max_workers=len(BOTH)) as pool:
            results = list(pool.map(_writer, [(str(self.path), case_id, s, count, start)
                                              for s in BOTH]))
        statements = [f["statement"] for bucket in self.store.get(ENG_OPS, case_id)
                      ["findings"].values() for f in bucket]
        self.assertEqual(len(statements), count + len(BOTH) * count)
        self.assertEqual(len(set(statements)), len(statements))
        self.assertEqual(sum(not m for r in results for m in r), count)   # one creator each
        with sqlite3.connect(self.path) as db:
            seqs = [n for (n,) in db.execute(
                "SELECT case_seq FROM case_event WHERE case_id=? ORDER BY case_seq", (case_id,))]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertTrue(self.store.get(ENG_OPS, case_id)["integrity"]["chain_ok"])

    def test_key_is_deterministic(self):
        self.assertEqual(finding_key("c", "fact", "supported", "A  b", ["x", "y", "x"]),
                         finding_key("c", "fact", "supported", "a B", ["y", "x"]))


if __name__ == "__main__":
    unittest.main()
