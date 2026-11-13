"""The rotating replies sweep (CAPTURE-009): Graph's channel delta never lists an older
root a reply was added to, and only roots active in the last ``active_days`` were listed
again, so a reply to an older root was never read. Each incremental round now lists the
replies of ``reply_sweep_roots`` more held roots in turn; a reply to any root is read
within ceil(roots / budget) rounds, and each run reports the sweep's horizon."""
from __future__ import annotations

import json
import math
import re
from urllib.parse import unquote, urlsplit

from integrations import capture_teams
from integrations.tests.test_capture_teams import GENERAL, MEMBER, CaptureCase, scope

REPLIES = re.compile(r"/v1\.0/teams/[^/]+/channels/([^/]+)/messages/(\d+)/replies")


class ReplySweepTests(CaptureCase):
    def replies_listed(self) -> list[str]:
        return [m[2] for u in self.graph.gets()
                if (m := REPLIES.fullmatch(unquote(urlsplit(u).path))) and m[1] == GENERAL]

    def general(self, run: dict) -> dict:
        unit = self.unit(GENERAL)[1]
        return [c for c in run["conversations"] if c["unit"] == unit][0]

    def late_reply_read(self, extra: int, budget: int) -> None:
        roots = ["1001", "1002"] + [str(5000 + n) for n in range(extra)]
        for root in roots[2:]:
            self.graph.post_root(GENERAL, root, "old root")
        first = self.capture.run(scope())
        self.assertEqual(self.general(first)["round"], "full")
        horizon = self.general(first)["reply_sweep"]["horizon"]
        self.assertIsNotNone(horizon)                    # a full round is a finished pass
        # a reply added to the root in the middle (months old: never "active")
        target = roots[len(roots) // 2]
        self.graph.replies[(GENERAL, target)] = [self.graph.message(
            f"{target}9", "late reply", channel=GENERAL, reply_to=target)]
        bound = math.ceil(len(roots) / budget) + 1
        listed: list[str] = []
        for n in range(1, bound + 1):
            self.graph.calls.clear()
            run = self.capture.run(scope(reply_sweep_roots=budget))
            self.assertEqual(self.general(run)["round"], "incremental")
            listed += self.replies_listed()
            if any(r["row"]["reply_to_id"] == target for r in self.visible(MEMBER, GENERAL)):
                break
        else:
            self.fail(f"the late reply was not read in {bound} rounds")
        # a pass lists each held root once: none skipped before the target, none twice
        self.assertEqual(len(listed), len(set(listed)))
        self.assertLessEqual(len(listed), len(roots))
        self.assertIn(target, listed)
        # the full pass finishes within the bound and moves the horizon on
        for _ in range(bound):
            self.graph.calls.clear()
            run = self.capture.run(scope(reply_sweep_roots=budget))
            listed += self.replies_listed()
            if self.general(run)["reply_sweep"]["horizon"] != horizon:
                break
        self.assertGreater(self.general(run)["reply_sweep"]["horizon"], horizon)
        self.assertEqual(set(listed), set(roots))        # every held root, no row lost
        self.assertEqual(run["reply_sweep"]["channels"], 2)

    def test_a_reply_to_an_old_root_is_read_within_the_bound(self):
        for extra, budget in ((0, 1), (8, 3), (58, 7)):   # 2, 10 and 60 roots
            with self.subTest(roots=extra + 2):
                self.tearDown()
                self.setUp()
                self.late_reply_read(extra, budget)

    def test_without_the_sweep_the_reply_is_never_read(self):
        self.capture.run(scope())
        self.graph.replies[(GENERAL, "1002")] = [self.graph.message(
            "10029", "late reply", channel=GENERAL, reply_to="1002")]
        for _ in range(4):
            run = self.capture.run(scope(reply_sweep_roots=0))
        self.assertFalse(any(r["row"]["reply_to_id"] == "1002"
                             for r in self.visible(MEMBER, GENERAL)))
        # the horizon stays at the full round: it never vouches for the late reply
        state = json.loads(self.store.state(capture_teams.REPLY_SWEEP + self.unit(GENERAL)[1]))
        self.assertEqual(self.general(run)["reply_sweep"]["horizon"], state["horizon"])

    def test_a_failed_replies_read_does_not_move_the_sweep(self):
        for n in range(6):
            self.graph.post_root(GENERAL, str(5000 + n), "old root")
        self.capture.run(scope())
        key = capture_teams.REPLY_SWEEP + self.unit(GENERAL)[1]
        self.capture.run(scope(reply_sweep_roots=2))
        before = self.store.state(key)
        self.assertIsNotNone(json.loads(before)["after"])
        self.graph.failing = {r"/messages/\d+/replies": 503}
        run = self.capture.run(scope(reply_sweep_roots=2))
        self.assertNotEqual(self.general(run)["status"], "complete")
        self.assertEqual(self.store.state(key), before)

    def test_a_root_gone_at_source_is_a_gap_and_the_sweep_moves_past_it(self):
        roots = [str(5000 + n) for n in range(6)]
        for root in roots:
            self.graph.post_root(GENERAL, root, "old root")
        self.capture.run(scope())
        key = capture_teams.REPLY_SWEEP + self.unit(GENERAL)[1]
        # retention removed the first old root: its replies answer 404 for good
        self.graph.failing = {rf"/messages/{roots[0]}/replies": 404}
        last = roots[-1]
        self.graph.replies[(GENERAL, last)] = [self.graph.message(
            f"{last}9", "late reply", channel=GENERAL, reply_to=last)]
        horizon = json.loads(self.store.state(key))["horizon"]
        gaps = 0
        for _ in range(8):
            run = self.capture.run(scope(reply_sweep_roots=2))
            general = self.general(run)
            if capture_teams.SWEEP_GAP in general["reasons"]:
                gaps += 1
                self.assertEqual(general["status"], "incomplete")
            if general["reply_sweep"]["horizon"] != horizon:
                break
        self.assertEqual(gaps, 1)                         # reported once per pass
        self.assertGreater(general["reply_sweep"]["horizon"], horizon)
        self.assertTrue(any(r["row"]["reply_to_id"] == last
                            for r in self.visible(MEMBER, GENERAL)))

    def test_an_unreadable_sweep_record_vouches_for_nothing(self):
        self.capture.run(scope())
        key = capture_teams.REPLY_SWEEP + self.unit(GENERAL)[1]
        for bad in ("not json", "[1, 2]", json.dumps({"horizon": 5})):
            self.store.set_state(key, bad)
            self.assertIsNone(self.capture.reply_sweep_horizon()["horizon"])
            self.assertIsNone(self.capture._sweep_state(self.unit(GENERAL)[1])["horizon"])


if __name__ == "__main__":
    import unittest
    unittest.main()
