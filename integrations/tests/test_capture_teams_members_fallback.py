"""Teams capture when Graph refuses ``/teams/{id}/members`` (evidence: injected-transport).

A team id is its Microsoft 365 group id, so a
standard channel whose team members read answers 403 is captured under its group
(``acl_source`` ``group_fallback``, identity ``group:<team-id>``); a private or shared
channel has no fallback and stays blocked naming ``ChannelMember.Read.All``; the
members endpoint is used whenever it answers; a reader without the group identity,
outside the grant or withheld sees nothing through any consumer of the store.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from verify import live_verify as lv
from integrations import graph_read
from integrations.capture_store import CaptureError
from integrations.capture_teams import TeamsCapture, open_store
from integrations.live_source_guard import LiveSourceAudience
from integrations.tests import test_capture_teams as base
from integrations.tests.test_capture_teams import (CAPTURE, COS, COS_ONLY, ENV, GENERAL,
                                                   MEMBER, OUTSIDER, PRIVATE, SECRET_TEXT,
                                                   TEAM, U_COS, FakeTeamsGraph, j, scope)
from ontology.types import Actor

TEAM_MEMBERS = rf"^/v1\.0/teams/{TEAM}/members$"
CHANNEL_MEMBERS = r"/channels/[^/]+/members$"
GROUP_READER = Actor("plx", frozenset({"staff:group-member"}), "restricted")
WITHHELD = Actor("plx", frozenset({"staff:teams-withheld"}), "restricted")


class DirectoryGraph(FakeTeamsGraph):
    """The fake Graph plus the directory reads of ``graph_directory`` (enabled users,
    transitive membership refused for consent, groups from joinedTeams) and per-path
    refusals with any status (``refuse``: path regex -> HTTP status)."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse: dict[str, int] = {r"/transitiveMemberOf/": 403}

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        path = unquote(urlsplit(url).path)
        for pattern, status in self.refuse.items():
            if re.search(pattern, path):
                self.calls.append((method, url))
                return j(status, {"error": {"code": "Refused"}})
        status, head, payload = super().__call__(method, url, headers, body, timeout,
                                                 max_bytes)
        if status == 200 and re.fullmatch(r"/v1\.0/users/[^/]+", path):
            payload = json.dumps({**json.loads(payload), "accountEnabled": True}).encode()
        return status, head, payload


class FallbackCase(base.CaptureCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.graph = DirectoryGraph()
        self.graph.post_root(GENERAL, "1001", replies=2)
        self.graph.post_root(GENERAL, "1002")
        self.graph.post_root(PRIVATE, "2001", "Board-only pricing")
        self.audience = LiveSourceAudience(
            {"teams:*": ["svc:teams-capture", "staff:teams-member", "staff:teams-outsider",
                         "staff:cos", "staff:group-member"]},
            tenant="plx", classifications={"teams:*": "restricted"},
            identities={"staff:teams-member": [COS], "staff:cos": [U_COS],
                        "staff:teams-outsider": ["outsider@petrasoap.com"],
                        "staff:group-member": [f"group:{TEAM}"]})
        self.store = open_store(self.tmp.name, self.audience)
        api, self.transport = graph_read.build_graph(ENV, sender=self.graph)
        self.capture = TeamsCapture(self.store, api, self.audience, CAPTURE)

    def visible(self, actor: Actor, thread: str) -> list[dict]:
        try:
            return super().visible(actor, thread)
        except CaptureError:            # a unit withheld from the actor or never captured
            return []

    def member_reads(self) -> int:
        return sum(1 for u in self.graph.gets()
                   if re.search(TEAM_MEMBERS, unquote(urlsplit(u).path)))

    def assert_sees_nothing(self, actor: Actor, handles: list[str]) -> None:
        for thread in (GENERAL, PRIVATE):
            self.assertEqual(self.visible(actor, thread), [], actor)
        rows = self.store.claim_rows(actor)
        for handle in handles:   # what the host's item gate reads (CaseRuntime)
            self.assertFalse(self.store.resolve(handle, actor)["ok"])
            self.assertIsNone(rows(handle))


class MembersFallbackTests(FallbackCase):
    def test_a_refused_team_members_read_captures_the_standard_channel_under_its_group(self):
        self.graph.refuse[TEAM_MEMBERS] = 403
        run = self.capture.run(scope())
        general = self.by_kind(run)["channel:standard"]
        self.assertEqual((general["status"], general["reasons"], general["acl_source"]),
                         ("complete", [], "group_fallback"), general)
        self.assertNotIn("TeamMember.Read.All",
                         {p.get("permission") for p in run["blocked_upstream"]})
        rows = self.visible(GROUP_READER, GENERAL)
        self.assertEqual(len(rows), 4)                         # 2 roots + 2 replies
        self.assertIn(SECRET_TEXT, {r["row"]["body_text"] for r in rows})
        handles = [r["citation"] for r in rows]
        self.assertTrue(all(self.store.resolve(h, GROUP_READER)["ok"] for h in handles))
        acl = {"status": "derived", "basis": "group_fallback", "identities": [f"group:{TEAM}"]}
        record = self.store.current_records("teams_channel", self.unit(GENERAL)[1])[0]
        self.assertEqual(record.payload["meta"]["source_acl"], acl)
        # The group grants only its identity: a team member known by address or object id
        # alone, a reader granted the resource but in no group, and a withheld principal
        # see, resolve and claim nothing.
        for actor in (MEMBER, COS_ONLY, OUTSIDER, WITHHELD):
            self.assert_sees_nothing(actor, handles)

    def test_a_private_or_shared_channel_stays_blocked_naming_its_permission(self):
        for membership in ("private", "shared"):
            with self.subTest(membership):
                self.graph.channel_types[PRIVATE] = membership
                self.graph.refuse.update({TEAM_MEMBERS: 403, CHANNEL_MEMBERS: 403})
                run = self.by_kind(self.capture.run(scope()))
                self.assertEqual(run["channel:standard"]["acl_source"], "group_fallback")
                closed = run[f"channel:{membership}"]
                self.assertEqual((closed["status"], closed["reasons"], closed["acl_source"]),
                                 ("blocked",
                                  ["blocked:missing_permission:ChannelMember.Read.All",
                                   "members_unread"], None))
                self.assertEqual(self.visible(GROUP_READER, PRIVATE), [])
                self.assertEqual(self.visible(MEMBER, PRIVATE), [])
        self.assertFalse([u for u in self.graph.gets()
                          if f"/channels/{PRIVATE}/messages" in unquote(u)])

    def test_only_a_refusal_falls_back_and_the_members_endpoint_wins_when_it_answers(self):
        # Not a 403 (unknown, unauthorized, failing): no fallback, nothing captured.
        for status, reason in ((404, "source_error:SourceNotFound"),
                               (401, "blocked:unauthorized")):
            with self.subTest(status):
                self.graph.refuse[TEAM_MEMBERS] = status
                general = self.by_kind(self.capture.run(scope()))["channel:standard"]
                self.assertEqual((general["acl_source"], general["round"]), (None, None))
                self.assertIn("members_unread", general["reasons"])
                self.assertTrue(any(r.startswith(reason) for r in general["reasons"]), general)
                self.assertEqual(self.visible(GROUP_READER, GENERAL), [])
        self.graph.refuse[TEAM_MEMBERS] = 403
        self.capture.run(scope())
        handles = [r["citation"] for r in self.visible(GROUP_READER, GENERAL)]
        self.assertEqual(len(handles), 4)
        # The endpoint answers again: it is read, used, and every message moves to the
        # read membership, so the group-only reader loses them and the members gain them.
        del self.graph.refuse[TEAM_MEMBERS]
        reads = self.member_reads()
        general = self.by_kind(self.capture.run(scope()))["channel:standard"]
        self.assertEqual(self.member_reads(), reads + 1)
        self.assertEqual((general["acl_source"], general["counts"]["members"],
                          general["counts"]["acl_refreshed"]), ("team_members", 2, 4))
        self.assertEqual(len(self.visible(MEMBER, GENERAL)), 4)
        self.assertEqual(len(self.visible(COS_ONLY, GENERAL)), 4)
        self.assert_sees_nothing(GROUP_READER, handles)
        self.assert_sees_nothing(OUTSIDER, handles)


class FallbackSpecTests(unittest.TestCase):
    """The live-check spec's own argv. Its census clears a team only on its members
    (The people guard decides before any other request of it),
    so a refused team members read is no longer captured under the group on this path:
    the team is refused before its name or channels are requested, none of its channels
    is due for content (none was cleared), and the run is blocked naming
    TeamMember.Read.All. (``TeamsCapture.run`` keeps the group fallback: above.)"""

    SPEC, run_spec, argv = base.ProbeTests.SPEC, base.ProbeTests.run_spec, base.ProbeTests.argv

    def test_a_refused_team_members_read_blocks_the_team_before_any_other_read_of_it(self):
        graph = DirectoryGraph()
        graph.post_root(GENERAL, "1001", replies=2)
        graph.post_root(PRIVATE, "2001")
        graph.post_chat("3001")
        graph.refuse[TEAM_MEMBERS] = 403
        code, report = self.run_spec(graph)
        self.assertEqual(lv.check_expectations(self.SPEC, code, report,
                                               self.SPEC["expect_blocked"]), [], report)
        self.assertEqual({(p["part"], p["permission"]) for p in report["blocked_upstream"]},
                         {("teams.members", "TeamMember.Read.All")})
        census = report["census"]
        self.assertEqual((census["teams"], census["teams_refused"]["members_unread"],
                          census["teams_cleared"], census["channels"]), (1, 1, 0, 0))
        paths = [unquote(urlsplit(u).path) for u in graph.gets()]
        self.assertFalse([p for p in paths if p.startswith(f"/v1.0/teams/{TEAM}")
                          and p != f"/v1.0/teams/{TEAM}/members"], paths)
        # Content is due of what the census cleared, with no
        # human confirmation (the stale one run_spec writes is not consulted). The refused
        # team clears no channel, so none of its channels is due or read.
        content = report["content"]
        self.assertEqual((content["status"], content["confirmed_channels"],
                          content["channels_due"], content["confirmed_not_cleared"]),
                         ("census_cleared", 0, 0, 0), content)
        self.assertEqual(report["summary"]["conversations_group_fallback"], 0)
        self.assertEqual({c["kind"] for c in report["runs"][-1]["conversations"]}, {"chat"})


if __name__ == "__main__":
    unittest.main()
