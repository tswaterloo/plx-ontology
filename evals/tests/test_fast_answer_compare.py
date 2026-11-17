"""IN-11 / AUD-02 comparison harness (``evals.fast_answer_compare``) and the off switch the
off arm runs with (``operator_api.backend.fast_off``). Evidence: isolated-fixture."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from evals import fast_answer_compare as fc


def write_run(root: Path, name: str, rows):
    run = root / "runs" / name
    run.mkdir(parents=True)
    (run / "verdicts.jsonl").write_text("".join(
        json.dumps({"item_id": i, "outcome": o}) + "\n" for i, o, _s, _st in rows))
    (run / "log.jsonl").write_text("".join(
        json.dumps({"id": i, "seconds": s, "status": st}) + "\n" for i, _o, s, st in rows))
    return run


class Compare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def runs(self, on, off):
        self.n = getattr(self, "n", 0) + 1
        return fc.compare(fc.arm(write_run(self.root, f"on{self.n}", on)),
                          fc.arm(write_run(self.root, f"off{self.n}", off)))

    def test_off_matching_on_is_remove_and_the_table_counts_each_outcome(self):
        on = [(f"G-{i}", "correct" if i < 15 else "wrong", 100.0 + i, "ok") for i in range(20)]
        off = [(f"G-{i}", "correct" if i < 16 else "no_answer", 80.0, "ok") for i in range(20)]
        got = self.runs(on, off)
        self.assertEqual(got["decision"], "remove", got["why"])
        self.assertEqual((got["on"]["correct"], got["on"]["wrong"]), (15, 5))
        self.assertEqual((got["off"]["correct"], got["off"]["no_answer"]), (16, 4))
        self.assertEqual(got["only_off_correct"], ["G-15"])
        self.assertEqual(got["off"]["median_seconds"], 80.0)
        self.assertIn("| phrase-rule OFF | 16/20 |", fc.table(got))

    def test_off_behind_or_more_wrong_is_keep(self):
        # the 11-13 result: on 30/40, off 26/40
        on = [(f"G-{i}", "correct" if i < 30 else "wrong", 113.0, "ok") for i in range(40)]
        off = [(f"G-{i}", "correct" if i < 26 else "wrong", 108.0, "ok") for i in range(40)]
        self.assertEqual(self.runs(on, off)["decision"], "keep")
        # as many right but more wrong: keep
        on = [(f"G-{i}", "correct" if i < 15 else "no_answer", 1.0, "ok") for i in range(20)]
        off = [(f"G-{i}", "correct" if i < 15 else "wrong", 1.0, "ok") for i in range(20)]
        self.assertEqual(self.runs(on, off)["decision"], "keep")

    def test_unpaired_or_too_few_items_are_incomparable(self):
        on = [(f"G-{i}", "correct", 1.0, "ok") for i in range(20)]
        off = [(f"G-{i}", "correct", 1.0, "ok") for i in range(19)]
        got = self.runs(on, off)
        self.assertEqual((got["decision"], got["unpaired"]), ("incomparable", ["G-19"]))
        small = self.runs(on[:5], on[:5])
        self.assertEqual(small["decision"], "incomparable")

    def test_timeouts_and_missing_runs(self):
        on = [(f"G-{i}", "wrong" if i == 0 else "correct", 300.0, "timeout" if i == 0 else "ok")
              for i in range(16)]
        got = self.runs(on, on)
        self.assertEqual(got["on"]["timeouts"], 1)
        self.assertEqual(fc.arm(self.root / "runs" / "absent"), {})

    def test_report_cli_exit_codes_and_no_text(self):
        rows = [(f"G-{i}", "correct", 2.0, "ok") for i in range(16)]
        write_run(self.root, "a", rows)
        write_run(self.root, "b", rows)
        out = self.root / "r.json"
        with mock.patch("sys.stdout"):
            self.assertEqual(fc.main(["report", "--root", str(self.root), "--on", "a",
                                      "--off", "b", "--json", str(out)]), 0)
            self.assertEqual(fc.main(["report", "--root", str(self.root), "--on", "a",
                                      "--off", "nope"]), 3)
        self.assertEqual(json.loads(out.read_text())["decision"], "remove")


class PickAndPlan(unittest.TestCase):
    def test_pick_takes_only_served_unspent_items_seeded(self):
        items = [{"id": f"G-{i}", "question": "fast" if i % 2 else "slow"} for i in range(30)]
        serves = lambda q: q == "fast"
        a = fc.pick(items, 5, 7, exclude=["G-1"], serves=serves)
        self.assertEqual(a, fc.pick(items, 5, 7, exclude=["G-1"], serves=serves))
        self.assertEqual(len(a), 5)
        self.assertTrue(all(int(i[2:]) % 2 and i != "G-1" for i in a))
        self.assertEqual(len(fc.pick(items, 99, 7, serves=serves)), 15)

    def test_plan_runs_the_same_ids_one_arm_at_a_time_under_the_lock(self):
        on, off = fc.plan(Path("/r"), "held", ["G-1", "G-2"], "in11", "--dev x")
        for line in (on, off):
            self.assertTrue(line.startswith(f"flock {fc.EVAL_LOCK} env "))
            self.assertIn("--ids G-1,G-2", line)
        self.assertNotIn("PLX_FAST_ANSWER", on)
        self.assertIn("PLX_FAST_ANSWER=off ", off)
        self.assertIn("--name in11-off", off)


class OffSwitch(unittest.TestCase):
    def test_off_sends_every_question_past_the_phrase_rule_path(self):
        from operator_api import backend as be
        self.assertTrue(be.fast_off({"PLX_FAST_ANSWER": " OFF "}))
        for env in ({}, {"PLX_FAST_ANSWER": "on"}, {"PLX_FAST_ANSWER": "0"}):
            self.assertFalse(be.fast_off(env))
        host = object.__new__(be.WorkspaceBackend)
        with mock.patch.dict("os.environ", {"PLX_FAST_ANSWER": "off"}), \
                mock.patch("operating.fast_answer.serves", side_effect=AssertionError):
            gen = host._fast_ask(None, type("R", (), {"question": "PO 4512 status"})(), "c",
                                 lambda: 0)
            with self.assertRaises(StopIteration) as done:
                next(gen)
        self.assertIsNone(done.exception.value)


class Families(unittest.TestCase):
    """CAP-L180: the router goes only when no family's general path is worse."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_draw_is_seeded_per_family_capped_and_skips_spent_and_unrouted(self):
        items = [{"id": f"L-{i}", "question": "late"} for i in range(9)] + \
            [{"id": f"P-{i}", "question": "profit"} for i in range(3)] + \
            [{"id": "X-0", "question": "open"}]
        fam = {"late": "late_list", "profit": "customer_profit"}.get
        got = fc.pick_families(items, 4, 1114, exclude=["L-0"], family_of=fam)
        self.assertEqual(sorted(set(got.values())), ["customer_profit", "late_list"])
        self.assertEqual(sum(v == "late_list" for v in got.values()), 4)
        self.assertEqual(sum(v == "customer_profit" for v in got.values()), 3)
        self.assertNotIn("L-0", got)
        self.assertNotIn("X-0", got)
        self.assertEqual(got, fc.pick_families(items, 4, 1114, exclude=["L-0"], family_of=fam))

    def test_one_worse_family_keeps_the_router_though_the_whole_set_would_remove_it(self):
        on = [(f"A-{i}", "correct", 1.0, "ok") for i in range(15)] + \
            [(f"B-{i}", "correct", 1.0, "ok") for i in range(5)]
        off = [(f"A-{i}", "correct", 1.0, "ok") for i in range(15)] + \
            [(f"B-{i}", "correct" if i < 4 else "wrong", 1.0, "ok") for i in range(5)]
        on += [(f"A-{i}", "wrong", 1.0, "ok") for i in range(15, 20)]
        off += [(f"A-{i}", "correct", 1.0, "ok") for i in range(15, 20)]
        families = {**{f"A-{i}": "late_list" for i in range(20)},
                    **{f"B-{i}": "customer_profit" for i in range(5)}}
        a = fc.arm(write_run(self.root, "on", on))
        b = fc.arm(write_run(self.root, "off", off))
        self.assertEqual(fc.compare(a, b)["decision"], "remove")
        got = fc.compare_families(a, b, families)
        self.assertEqual(got["families"]["late_list"]["decision"], "remove")
        self.assertEqual(got["families"]["customer_profit"]["decision"], "keep")
        self.assertEqual(got["decision"], "keep")
        self.assertIn("customer_profit=keep", got["why"])
        self.assertIn("| customer_profit |", fc.table(got))
        # a family under FAMILY_MIN_ITEMS is incomparable, and so is the whole
        fixed = fc.arm(write_run(self.root, "off2", [r for r in off if r[0].startswith("A")] +
                                 [(f"B-{i}", "correct", 1.0, "ok") for i in range(5)]))
        tiny = {**{f"A-{i}": "late_list" for i in range(20)}, "B-0": "tiny"}
        got = fc.compare_families({k: v for k, v in a.items() if k in tiny},
                                  {k: v for k, v in fixed.items() if k in tiny}, tiny)
        self.assertEqual(got["families"]["tiny"]["decision"], "incomparable")
        self.assertEqual(got["decision"], "incomparable")


if __name__ == "__main__":
    unittest.main()
