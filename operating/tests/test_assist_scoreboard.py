"""The scoreboard counts each person once however many roles hold them, covers an action
when any of their roles' queues did, and says what it did not measure. Synthetic results."""
from __future__ import annotations

import unittest

from operating import assist_scoreboard as sb


def role(name, members, rows, items, status="complete"):
    return {"role": name, "status": status, "member_ids": members, "items_by_person": items,
            "people_actions": rows, "work": {"actions": sum(len(v) for v in rows.values()),
                                              "covered": sum(h for v in rows.values() for _k, _t, h in v)}}


class Board(unittest.TestCase):
    def test_a_person_in_two_roles_is_counted_once_and_covered_by_either(self):
        shared = [["k1", "mail_sent:reply", 0], ["k2", "sage:ar_invoice", 0], ["k3", "teams_sent:reply", 1]]
        cs = role("customer_service", ["ann", "bo"], {"ann": shared, "bo": [["b1", "mail_sent:reply", 1]]},
                  {"ann": 3, "bo": 5})
        rec = role("receivables", ["ann"], {"ann": [["k1", "mail_sent:reply", 1], ["k2", "sage:ar_invoice", 0],
                                                   ["k3", "teams_sent:reply", 0]]}, {"ann": 9})
        out = sb.board([cs, rec])
        self.assertEqual(out["old_role_sum"]["actions"], 3 + 1 + 3)         # ann counted twice before
        self.assertEqual(out["totals"], {"people": 2, "actions": 4, "covered": 3})   # k1 by receivables, k3 by cs
        self.assertEqual(out["people"]["ann"]["primary_role"], "receivables")  # the queue with most items for her
        self.assertEqual(out["roles"]["receivables"], {"people": 1, "actions": 3, "covered": 2})
        self.assertEqual(out["roles"]["customer_service"], {"people": 1, "actions": 1, "covered": 1})
        self.assertEqual(sum(r["actions"] for r in out["roles"].values()), out["totals"]["actions"])  # additive
        self.assertIn("hours", out["not_measured"])

    def test_a_blocked_or_old_format_result_is_said_not_dropped(self):
        old = {"role": "quality", "status": "complete", "work": {"actions": 5, "covered": 1}}
        blocked = {"role": "purchasing", "status": "blocked", "reason": "people_exclusion_unavailable"}
        out = sb.board([old, blocked, role("r", ["x"], {"x": []}, {})])
        self.assertEqual({s["role"] for s in out["skipped"]}, {"quality", "purchasing"})
        self.assertEqual(out["totals"]["people"], 1)

    def test_a_tie_in_items_goes_to_the_first_role_by_name(self):
        a = role("b_role", ["z"], {"z": [["k", "t:x", 0]]}, {"z": 2})
        b = role("a_role", ["z"], {"z": [["k", "t:x", 0]]}, {"z": 2})
        self.assertEqual(sb.board([a, b])["people"]["z"]["primary_role"], "a_role")



class Nightly(unittest.TestCase):
    """The nightly run scores the US Eastern business day and says when it cannot score."""

    def test_the_day_is_the_eastern_business_day_across_a_clock_change(self):
        from datetime import date, datetime, timezone
        from operating import assist_nightly as nightly
        self.assertEqual(nightly.window(date(2026, 11, 20)),
                         (datetime(2026, 11, 20, 5, tzinfo=timezone.utc),
                          datetime(2026, 11, 21, 5, tzinfo=timezone.utc)))
        lo, hi = nightly.window(date(2026, 11, 1))     # 25 hours: the clocks went back
        self.assertEqual((hi - lo).total_seconds(), 25 * 3600)

    def test_no_live_queue_is_a_blocked_board_and_a_failed_exit(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from operating import assist_nightly as nightly
        with tempfile.TemporaryDirectory() as d:
            roles = Path(d, "roles.json")
            roles.write_text(json.dumps({"roles": {"purchasing": ["ann"]}}))
            with mock.patch.object(nightly.ap, "_store", return_value=mock.Mock()), \
                    mock.patch("operating.proactive_engine._people", return_value={}):
                code = nightly.main(["--store", "x", "--day", "2026-11-20", "--state-dir", d,
                                     "--roles-file", str(roles), "--out-dir", d])
            out = json.loads(Path(d, "2026-11-20.json").read_text())
        self.assertEqual((code, out["status"], out["reason"]), (1, "blocked", "no_live_queue"))


if __name__ == "__main__":
    unittest.main()