"""Tests for operating.assist_score (Fable ASSIST-SPECS §8.5 F10)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest

from operating import assist_score as sc


def _ts(hours: int = 0) -> datetime:
    return datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc) + timedelta(hours=hours)


class AssistScoreTests(unittest.TestCase):
    def test_per_kind_and_per_person_scoring(self):
        matters = [
            {
                "matter_id": "m1",
                "owner_person": "alice@example.com",
                "kind": "conversation",
                "subject_key": "t1",
                "sources": {"records": []},
                "basis_at": _ts(0),
                "next_act": "reply",
                "counterpart": "cust1@ext.com",
                "plan": {"first_act": "reply", "recipients": ["cust1@ext.com"]},
            },
            {
                "matter_id": "m2",
                "owner_person": "alice@example.com",
                "kind": "conversation",
                "subject_key": "t2",
                "sources": {"records": []},
                "basis_at": _ts(0),
                "next_act": "forward",
                "counterpart": "ops@example.com",
                "plan": {"first_act": "forward", "recipients": ["ops@example.com"]},
            },
            {
                "matter_id": "m3",
                "owner_person": "bob@example.com",
                "kind": "record",
                "subject_key": "rec:100",
                "sources": {"records": ["rec:100"]},
                "basis_at": _ts(0),
                "next_act": "system_write",
                "plan": {"first_act": "system_write"},
            },
            {
                "matter_id": "m4",
                "owner_person": "bob@example.com",
                "kind": "task",
                "subject_key": "task:200",
                "sources": {"records": ["task:200"]},
                "basis_at": _ts(0),
                "next_act": "task",
                "plan": {"first_act": "task"},
            },
            {
                "matter_id": "m5",
                "owner_person": "carol@example.com",
                "kind": "approval",
                "subject_key": "app:300",
                "sources": {"records": ["app:300"]},
                "basis_at": _ts(0),
                "next_act": "decide",
                "plan": {"first_act": "decide"},
            },
        ]
        actions = [
            {
                "action_key": "a1",
                "person": "alice@example.com",
                "at": _ts(1),
                "system": "mail",
                "kind": "mail_sent",
                "thread_key": "t1",
                "recipients": ["cust1@ext.com"],
                "not_human": "",
            },
            {
                "action_key": "a2",
                "person": "alice@example.com",
                "at": _ts(2),
                "system": "mail",
                "kind": "mail_sent",  # maps to reply, not forward
                "thread_key": "t2",
                "recipients": ["someone_else@example.com"],
                "not_human": "",
            },
            {
                "action_key": "a3",
                "person": "bob@example.com",
                "at": _ts(1),
                "system": "sage",
                "kind": "sage_ar_invoice",
                "target_handle": "rec:100",
                "recipients": [],
                "not_human": "",
            },
            {
                "action_key": "a4",
                "person": "bob@example.com",
                "at": _ts(2),
                "system": "planner",
                "kind": "planner_created",
                "target_handle": "task:200",
                "recipients": [],
                "not_human": "",
            },
            {
                "action_key": "a5",
                "person": "carol@example.com",
                "at": _ts(1),
                "system": "mission_control",
                "kind": "approval",
                "target_handle": "app:300",
                "recipients": [],
                "not_human": "",
            },
            {
                "action_key": "a_extra",
                "person": "carol@example.com",
                "at": _ts(3),
                "system": "mail",
                "kind": "mail_sent",
                "thread_key": "t_unrelated",
                "recipients": ["other@ext.com"],
                "not_human": "",
            },
        ]

        res = sc.score_matters(matters, actions, window_days=7, as_of=_ts(5))
        self.assertEqual(res["totals"]["kind_score"], "4 of 5")
        self.assertEqual(res["totals"]["recipient_score"], "1 of 2")
        self.assertEqual(res["by_kind"]["reply"]["kind_score"], "1 of 1")
        self.assertEqual(res["by_kind"]["forward"]["kind_score"], "0 of 1")
        self.assertEqual(res["by_kind"]["system_write"]["kind_score"], "1 of 1")
        self.assertEqual(res["by_kind"]["task"]["kind_score"], "1 of 1")
        self.assertEqual(res["by_kind"]["decide"]["kind_score"], "1 of 1")
        self.assertEqual(res["by_person"]["alice@example.com"]["kind_score"], "1 of 2")
        self.assertEqual(res["by_person"]["bob@example.com"]["kind_score"], "2 of 2")
        self.assertEqual(res["unmatched_real_acts"], 1)
        self.assertEqual(res["no_real_act_yet"], 0)

    def test_no_real_act_yet_and_excluded_person_omission(self):
        matters = [
            {
                "matter_id": "m_wait",
                "owner_person": "alice@example.com",
                "kind": "conversation",
                "subject_key": "t_wait",
                "sources": {"records": []},
                "basis_at": _ts(0),
                "next_act": "reply",
            },
            {
                "matter_id": "m_excl",
                "owner_person": "secret@example.com",
                "kind": "conversation",
                "subject_key": "t_excl",
                "sources": {"records": []},
                "basis_at": _ts(0),
                "next_act": "reply",
            },
        ]
        res = sc.score_matters(matters, [], excluded_people={"secret@example.com"})
        self.assertEqual(res["totals"]["kind_score"], "0 of 0")
        self.assertEqual(res["no_real_act_yet"], 1)
        self.assertNotIn("secret@example.com", res["by_person"])
        self.assertIn("excluded_people_omitted:1", res["not_measured"])

    def test_empty_db_and_atomic_0600_snapshot(self):
        res = sc.score_matters([], [], window_days=7)
        self.assertEqual(res["totals"]["kind_score"], "0 of 0")
        self.assertEqual(res["by_kind"], {})
        self.assertEqual(res["by_person"], {})

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "assist" / "nightly-score.json"
            sc.write_snapshot(out, res)
            self.assertTrue(out.exists())
            self.assertEqual(stat.S_IMODE(out.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            loaded = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(loaded["schema"], "assist.nightly_score.v1")

    def test_exclusion_guard_fails_closed_when_unavailable_or_undecidable(self):
        from integrations.people_exclusion import PeopleExclusion

        matters = [{
            "matter_id": "m1",
            "owner_person": "alice@example.com",
            "kind": "conversation",
            "subject_key": "t1",
            "sources": {"records": []},
            "basis_at": _ts(0),
            "next_act": "reply",
        }]
        unavail = PeopleExclusion(unavailable="excluded_people_file_unreadable")
        res_u = sc.score_matters(matters, [], exclusion_guard=unavail)
        self.assertEqual(res_u["by_person"], {})
        self.assertTrue(any("people_exclusion_unavailable" in r for r in res_u["not_measured"]))

        # Unbound guard cannot decide email addresses -> must fail closed and omit
        unbound = PeopleExclusion(["11111111-1111-1111-1111-111111111111"])
        res_b = sc.score_matters(matters, [], exclusion_guard=unbound)
        self.assertEqual(res_b["by_person"], {})
        self.assertIn("excluded_people_omitted:1", res_b["not_measured"])


if __name__ == "__main__":
    unittest.main()
