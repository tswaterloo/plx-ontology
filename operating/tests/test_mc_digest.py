"""The Mission Control approvals digest (``operating.mc_digest``) over the approval autopilot's
items: each decider's pending gates grouped by class, ranked, with the history-based
recommendation, the low-risk mark and the MC task link; nothing approved or written.

SYNTHETIC fixtures only; evidence label: isolated-fixture.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
import socket
import unittest
from unittest import mock

from operating import autopilot as ap
from operating import mc_digest as md
from operating.tests.test_autopilot import OTHER, READER, everyone, opens_all
from operating.tests.test_autopilot_replay import DAY, World, at
from operating.tests.test_proactive_engine import OURS_OID
from operating.tests.test_proactive_threads import APPROVER, APPROVER_OID, OURS, people

OURS_P, THEIRS_P = "mc:live/approvals/P-OURS", "mc:live/approvals/P-THEIRS"
NOW = DAY + timedelta(hours=20)
REASONS = {          # class -> a synthetic reason stating it
    "money": "Merge the price list change",       # money wins over merge
    "permission": "Route permission change for the ops group",
    "migration": "Migration PR adds a column",
    "production": "Deploy the portal to production",
    "workflow": "Workflow file PR for the CI job",
    "merge": "Merge approval for the review fix",
    "launch": "Launch the docs lane on the agent host",
    "other": "Which week should the count happen?",
}


class Digest(World):
    def gate(self, i: int, reason: str, *, audience: str = OURS_P, hour: int = 10,
             task: str | None = None, task_id: str | None = None) -> str:
        oid = self.w.obj("MCApproval", {
            "gate_id": f"apg_{i}", "task_id": task_id or f"TASK-{1000 + i}",
            # asked by an agent no one in the directory is: an asker never decides their
            # own gate when another can (autopilot.owners_of)
            "status": "pending", "reason": reason, "requested_by": "agent@synth-tenant.example",
            "requested_at": at(hour)}, audience=audience)
        if task:
            self.w.link("MCApproval.gate_of", oid, task)
        self.events.append(("MCApproval.requested", oid, at(hour)))
        return oid

    def decided(self, audience: str, by: str, reason: str, status: str = "approved", *,
                days_before: float = 3, hours_after: float = 0) -> None:
        when = DAY + timedelta(days=-days_before, hours=9 + hours_after)
        self.w.obj("MCApproval", {"status": status, "decided_by": by, "reason": reason,
                                  "requested_at": ap._iso(when - timedelta(hours=1)),
                                  "decided_at": ap._iso(when)}, audience=audience)

    def prepare(self, ids):
        st = self.store()
        q = ap.Queue(self.dir / "q" / ap.QUEUE_FILE)
        self.addCleanup(q.close)
        with mock.patch.object(ap, "_approvers", lambda _d: [OURS_OID, APPROVER_OID]):
            registry = ap.load_registry()
            # the per-owner hourly limit defers the rest to later cycles (tested in
            # test_autopilot); here every gate is prepared in one cycle
            registry["limits"]["per_owner_per_hour"] = 1000
            ap.run_cycle(st, q, ids, people=people(), environ={}, now=NOW,
                         registry=registry, settle_items=False)
        return q

    def twenty(self):
        """20 pending gates: 17 in our project (OURS decides there), 3 in theirs."""
        for k in range(16):                       # launch history: 16 approved of 16
            self.decided(OURS_P, OURS, REASONS["launch"], days_before=1 + k)
        self.decided(OURS_P, OURS, REASONS["merge"])
        self.decided(OURS_P, OURS, REASONS["launch"], "rejected", days_before=-1)  # later
        for k in range(10):                       # their launches: too few to mark
            self.decided(THEIRS_P, APPROVER, REASONS["launch"], days_before=1 + k)
        task = self.w.obj("MCTask", {"task_id": "TASK-2000", "title": "Synth", "stage": "b"})
        ids, n = [], 0
        for cid, reason in REASONS.items():
            for hour in (12, 9):                  # two of each class, the later first
                ids.append(self.gate(n, reason, hour=hour))
                n += 1
        ids.append(self.gate(n, REASONS["merge"], hour=8, task=task, task_id="TASK-2000"))
        ids.append(self.gate(n + 1, REASONS["merge"], hour=11, task=task,
                             task_id="TASK-2000"))       # the newer: the older is superseded
        ids.append(self.gate(n + 2, REASONS["launch"], audience=THEIRS_P, task_id="x/../y"))
        ids.append(self.gate(n + 3, REASONS["launch"], audience=THEIRS_P, hour=7))
        self.assertEqual(len(ids), 20)
        return ids

    def test_twenty_gates_group_rank_route_and_link(self):
        ids = self.twenty()
        q = self.prepare(ids)
        mine = ap.visible(q, reader=READER, may_read=everyone, held=[], decide=opens_all)
        theirs = ap.visible(q, reader=OTHER, may_read=everyone, held=[], decide=opens_all)
        ours_d = md.digest(mine, NOW)
        their_d = md.digest(theirs, NOW)
        # routing: our project's 18 gates go to OURS (who decided there), theirs' 2 to the
        # approver; no gate lost or doubled
        self.assertEqual([(d["decider"], d["pending"]) for d in ours_d["deciders"]],
                         [(OURS_OID, 18)])
        self.assertEqual([(d["decider"], d["pending"]) for d in their_d["deciders"]],
                         [(APPROVER_OID, 2)])
        seen = [e["item_id"] for d in (ours_d, their_d) for x in d["deciders"]
                for g in x["groups"] for e in g["items"]]
        self.assertEqual(len(seen), 20)
        self.assertEqual(len(set(seen)), 20)
        groups = ours_d["deciders"][0]["groups"]
        # riskier classes first, the declared order among them; the low-risk class last
        self.assertEqual([g["class"] for g in groups],
                         ["money", "permission", "migration", "production", "workflow",
                          "merge", "other", "launch"])
        self.assertEqual([g["class"] for g in groups if g["low_risk"]], ["launch"])
        launch = groups[-1]["items"][0]["history"]
        # the rejection decided after the requests is not history yet
        self.assertEqual((launch["approved"], launch["decided"]), (16, 16))
        # in a class: oldest request first, a superseded gate after the rest
        merge = next(g for g in groups if g["class"] == "merge")
        self.assertEqual([e["requested_at"] for e in merge["items"]],
                         [at(9), at(11), at(12), at(8)])
        self.assertEqual([e["recommendation"] for e in merge["items"]][-1], "superseded")
        self.assertEqual(merge["recommended"]["superseded"], 1)
        money = groups[0]["items"][0]
        self.assertEqual(money["link"], f"https://mc.plxcustomer.io/tasks/{money['task_id']}")
        self.assertFalse(money["history"]["low_risk"])
        # their project: 10 launches decided, fewer than the 15 a mark needs; the
        # malformed task id gets no link
        their_launch = their_d["deciders"][0]["groups"]
        self.assertEqual([(g["class"], g["low_risk"]) for g in their_launch],
                         [("launch", False)])
        self.assertEqual(sorted(str(e["link"]) for e in their_launch[0]["items"]),
                         ["None", "https://mc.plxcustomer.io/tasks/TASK-1019"])
        self.assertEqual((ours_d["approves"], their_d["approves"]), (0, 0))
        # the daily digest carries it
        self.assertEqual(ap.digest(mine, NOW)["mc_approvals"]["pending"], 18)

    def test_a_reader_who_may_not_read_mc_sees_no_gate(self):
        q = self.prepare(self.twenty())
        none = ap.visible(q, reader=READER, may_read=lambda a: not a.startswith("mc:"),
                          held=[], decide=opens_all)
        self.assertEqual(md.digest(none, NOW)["deciders"], [])
        stranger = ap.visible(q, reader="synth-tenant:admin", may_read=everyone, held=[],
                              decide=opens_all)
        self.assertEqual(md.digest(stranger, NOW)["pending"], 0)

    def test_cardinality_one_ten_sixty_gates_none_lost_or_doubled(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.decided(OURS_P, OURS, REASONS["merge"])
                ids = [self.gate(i, REASONS["launch" if i % 2 else "money"]) for i in range(n)]
                q = self.prepare(ids)
                got = md.digest(ap.visible(q, reader=READER, may_read=everyone, held=[],
                                           decide=opens_all), NOW)
                rows = [e["item_id"] for d in got["deciders"] for g in d["groups"]
                        for e in g["items"]]
                self.assertEqual((len(rows), len(set(rows)), got["pending"]), (n, n, n))

    def test_unknown_shapes_fail_closed(self):
        self.assertEqual(md.classify(None), "other")
        self.assertEqual(md.classify("Launch the money report"), "money")
        for past in (None, {}, {"by_class": {"launch": {"approved": 99, "decided": 3}}},
                     {"by_class": {"launch": {"approved": "x", "decided": 40}}},
                     {"by_class": {"launch": {"approved": 14, "decided": 14}}},
                     {"by_class": {"launch": {"approved": 18, "decided": 20}}}):
            with self.subTest(past=past):
                self.assertFalse(md.judged("launch", past)["low_risk"])
        self.assertTrue(md.judged("launch", {"by_class": {"launch": {"approved": 19,
                                                                     "decided": 20}}})
                        ["low_risk"])
        self.assertFalse(md.judged("merge", {"by_class": {"merge": {"approved": 99,
                                                                    "decided": 99}}})
                         ["low_risk"])
        item = {"item_id": "i", "autopilot": "approval_request", "owner": "o", "mine": True,
                "body": {"happened": {"object_type": "MCApproval", "fields": {}},
                         "prepared": {"recommendation": "approve now!", "mc": "junk"}}}
        (entry,) = md.digest([item], NOW)["deciders"][0]["groups"][0]["items"]
        self.assertEqual((entry["class"], entry["recommendation"], entry["link"],
                          entry["history"]["low_risk"]), ("other", "review", None, False))
        # a mail approval ask is not an MC gate
        mail_item = dict(item, autopilot="mail_approval_request")
        self.assertEqual(md.digest([mail_item], NOW)["pending"], 0)

    def test_no_network_and_no_mc_client_on_the_path(self):
        tree = ast.parse(Path(md.__file__).read_text(encoding="utf-8"))
        imported = {(n.module or "") if isinstance(n, ast.ImportFrom) else a.name
                    for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names}
        self.assertEqual(imported - {"__future__", "datetime", "re", "typing", "autopilot"},
                         set())
        ids = self.twenty()
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network")), \
                mock.patch.object(socket, "create_connection",
                                  side_effect=AssertionError("network")):
            q = self.prepare(ids)
            got = md.digest(ap.visible(q, reader=READER, may_read=everyone, held=[],
                                       decide=opens_all), NOW)
        self.assertEqual(got["approves"], 0)
        self.assertTrue(all(e["recommendation"] in md.RECOMMENDATIONS
                            for d in got["deciders"] for g in d["groups"]
                            for e in g["items"]))


if __name__ == "__main__":
    unittest.main()
