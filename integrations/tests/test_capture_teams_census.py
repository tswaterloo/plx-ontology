"""Teams census before any content read (evidence label: injected-transport).

``Teams-capture`` first lists the tenant's
teams, channels and the allowlisted accounts' chats (names into the census file only,
30-day message counts from the team activity report), proposes the purchasing, QA and
R&D ones, and then reads content of everything this census cleared (a ``content-confirmation.json`` is neither
required nor consulted). A team, channel or chat with an excluded member is refused by
the shared people guard before its name or content is requested, and is never named in
the census file; without the guard nothing is read and the run is blocked. No
getAllMessages, no subscription, no message body in a census. Over an in-process
fake Graph only: no network, no credentials.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import unittest
from urllib.parse import unquote, urlsplit

from verify import live_verify as lv
from integrations import capture_teams, graph_read
from integrations import live_source_grammar as grammar
from integrations.capture_teams import (CENSUS_OPERATIONS, CONFIRMATION_PART,
                                        census_path, confirmation_path, default_state_dir)
from integrations.tests import test_capture_teams as base
from integrations.tests.test_capture_teams import (CHAT, COS, ENV, GENERAL, PRIVATE,
                                                   SECRET_TEXT, TEAM, U_COS, FakeTeamsGraph,
                                                   j)

ROOT = Path(__file__).resolve().parents[2]
EXCLUDED = ENV["PLX_EXCLUDED_PEOPLE_OIDS"]
EXCLUDED_ADDRESS = "excluded.person@petrasoap.com"
OTHER_TEAM = "7eaa0000-0000-0000-0000-0000000000ff"   # in the report, not in the tenant list
NAMES = ("Operations", "General", "QA hand-offs", "R&D Bi-weekly Meeting")
REPORT = "/v1.0/reports/getTeamsTeamActivityDetail(period='D30')"


def operation(url: str) -> str | None:
    parts = urlsplit(url)
    return grammar.match(parts.hostname, parts.path)


class CensusCase(unittest.TestCase):
    SPEC = base.ProbeTests.SPEC
    argv = base.ProbeTests.argv

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.env = {**ENV, "XDG_STATE_HOME": self.home}
        self.state = default_state_dir(self.env, injected=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fresh(self) -> None:
        """A new state home for the next subtest."""
        self.tmp.cleanup()
        self.setUp()

    def fixture(self) -> FakeTeamsGraph:
        graph = FakeTeamsGraph()
        graph.users[EXCLUDED_ADDRESS] = EXCLUDED   # the guard learns the address
        graph.post_root(GENERAL, "1001", replies=1)
        graph.post_root(PRIVATE, "2001")
        graph.post_chat("3001")
        return graph

    def confirm(self, channels=(GENERAL, PRIVATE), chats=(CHAT,)) -> Path:
        return base.confirm(self.env, list(channels), list(chats))

    def run_cli(self, graph, env: dict | None = None, **flags: str) -> tuple[int, dict, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = graph_read.main(self.argv(self.home, **flags),
                                   environ=self.env if env is None else env, sender=graph)
        return code, json.loads(out.getvalue()), out.getvalue()

    def census_file(self) -> dict:
        return json.loads(census_path(self.state).read_text(encoding="utf-8"))

    def paths(self, graph) -> list[str]:
        return [unquote(urlsplit(u).path) for u in graph.gets()
                if urlsplit(u).hostname == "graph.microsoft.com"]

    def assert_no_content_read(self, graph) -> None:
        for url in graph.gets():
            self.assertNotIn("/messages", unquote(url))
            self.assertNotIn("getAllMessages", url)
            self.assertNotIn("subscriptions", url)

    def census_gets(self, graph) -> list[str]:
        """The census's Graph reads: every read up to its last one, the activity report
        (asked once, when a team was listed), else up to the first message read."""
        urls = [u for u in graph.gets() if urlsplit(u).hostname == "graph.microsoft.com"]
        report = [n for n, u in enumerate(urls) if unquote(urlsplit(u).path) == REPORT]
        self.assertLessEqual(len(report), 1, urls)
        if report:
            return urls[:report[0] + 1]
        return urls[:next((n for n, u in enumerate(urls) if "/messages" in unquote(u)),
                          len(urls))]

    def assert_census_first(self, graph) -> None:
        """The census runs before any content: every Graph read up to and with the
        activity report is a census operation (listings, memberships, records), and no
        read at all is getAllMessages or a subscription."""
        census = self.census_gets(graph)
        ops = [operation(u) for u in census]
        self.assertLessEqual(set(ops), CENSUS_OPERATIONS, ops)
        self.assertFalse([u for u in census if "/messages" in unquote(u)])
        for url in graph.gets():
            self.assertNotIn("getAllMessages", url)
            self.assertNotIn("subscriptions", url)

    def read(self, graph) -> tuple[set[str], set[str]]:
        """The channels whose message delta and the chats whose messages were requested."""
        channels, chats = set(), set()
        for path in self.paths(graph):
            found = re.fullmatch(r"/v1\.0/teams/[^/]+/channels/([^/]+)/messages/delta", path)
            if found:
                channels.add(found[1])
            found = re.fullmatch(r"/v1\.0/chats/([^/]+)/messages", path)
            if found:
                chats.add(found[1])
        return channels, chats

    def cleared(self) -> tuple[set[str], set[str]]:
        """The channels and chats the census file lists (what the people guard cleared)."""
        listing = self.census_file()
        return ({c["channel_id"] for t in listing["teams"] for c in t["channels"]},
                {c["chat_id"] for c in listing["chats"]})

    def assert_no_confirmation_step(self, report) -> None:
        """No content confirmation is asked for or written."""
        self.assertNotIn(CONFIRMATION_PART, {p["part"] for p in report["blocked_upstream"]})
        self.assertEqual((report["content"]["form"], report["content"]["confirmation"],
                          report["content"]["confirmed_not_cleared"]),
                         ("all_census_cleared", "not_required", 0))
        self.assertFalse(confirmation_path(self.state).exists())   # code never writes it

    def assert_nothing_named(self, *texts: str) -> None:
        for text in texts:
            for value in (EXCLUDED, EXCLUDED_ADDRESS, SECRET_TEXT, "do-not-leak", "TOKEN"):
                self.assertNotIn(value, text)


class CensusThenContentTests(CensusCase):
    def test_the_census_names_and_counts_then_everything_it_cleared_is_read(self):
        graph = self.fixture()
        graph.activity.append({"teamId": OTHER_TEAM, "details": [
            {"reportPeriod": "30", "channelMessages": 99}]})
        code, report, stdout = self.run_cli(graph)
        self.assert_census_first(graph)
        self.assertEqual((code, report["status"], report["content"]["status"]),
                         (0, "complete", "census_cleared"))
        self.assertEqual((report["blocked_upstream"], lv.blocked_parts(report)), ([], []))
        self.assert_no_confirmation_step(report)
        # Content of exactly what the census cleared: no confirmation file needed.
        self.assertEqual(self.read(graph), self.cleared())
        self.assertEqual(self.read(graph), ({GENERAL, PRIVATE}, {CHAT}))
        self.assertEqual((report["content"]["channels_due"], report["content"]["chats_due"]),
                         (2, 1))
        self.assertEqual(lv.check_expectations(self.SPEC, code, report), [], report)
        census = report["census"]
        self.assertEqual({k: census[k] for k in ("status", "teams", "teams_cleared",
                                                 "channels", "channels_cleared", "chats",
                                                 "chats_cleared", "proposals")},
                         {"status": "complete", "teams": 1, "teams_cleared": 1,
                          "channels": 2, "channels_cleared": 2, "chats": 1,
                          "chats_cleared": 1,
                          "proposals": {"purchasing": 0, "qa": 1, "rnd": 1}})
        self.assertEqual((census["message_counts"]["status"],
                          census["message_counts"]["teams_counted"]), ("counted", 1))
        # Names, ids and counts go to the census file (0600, outside git), never stdout.
        for value in (*NAMES, TEAM, GENERAL, PRIVATE, CHAT, OTHER_TEAM, COS, U_COS):
            self.assertNotIn(value, stdout)
        self.assert_nothing_named(stdout)
        self.assertTrue(census["file"]["written"])
        self.assertEqual(stat.S_IMODE(census_path(self.state).stat().st_mode), 0o600)
        listing = self.census_file()
        text = json.dumps(listing)
        self.assertNotIn(OTHER_TEAM, text)   # another team's report row is never kept
        self.assert_nothing_named(text)
        (team,) = listing["teams"]
        self.assertEqual((team["team_id"], team["name"], team["messages_30d"]),
                         (TEAM, "Operations", 12))
        self.assertEqual({c["channel_id"]: (c["name"], c["messages_30d"], c["proposed"])
                          for c in team["channels"]},
                         {GENERAL: ("General", None, []),
                          PRIVATE: ("QA hand-offs", None, ["qa"])})
        self.assertEqual([(c["chat_id"], c["topic"], c["proposed"]) for c in listing["chats"]],
                         [(CHAT, "R&D Bi-weekly Meeting", ["rnd"])])
        self.assertEqual(listing["proposals"], {
            "purchasing": [], "qa": [{"kind": "channel", "team_id": TEAM,
                                      "channel_id": PRIVATE}],
            "rnd": [{"kind": "chat", "chat_id": CHAT}]})

    def test_a_left_over_confirmation_file_is_neither_required_nor_consulted(self):
        """An old confirmation beside the census, narrow or unusable, changes nothing: the
        run reads what the census cleared, and the file is left as it was."""
        for name in ("narrow", "not_json", "directory", "symlink_loop", "in_git"):
            with self.subTest(name):
                self.fresh()
                path = confirmation_path(self.state)
                if name == "narrow":
                    self.confirm(channels=[PRIVATE, "19:gone0009@thread.tacv2"], chats=())
                else:
                    path.parent.mkdir(parents=True, mode=0o700)
                    if name == "not_json":
                        path.write_text("{not json", encoding="utf-8")
                    elif name == "directory":
                        path.mkdir()
                    elif name == "symlink_loop":
                        os.symlink(path, path)
                    else:
                        os.symlink(ROOT / "integrations" / "teams_capture_scope.json", path)
                before = os.readlink(path) if path.is_symlink() else (
                    None if path.is_dir() else path.read_bytes())
                graph = self.fixture()
                code, report, stdout = self.run_cli(graph)
                self.assert_census_first(graph)
                self.assertEqual((code, report["status"], report["content"]["status"],
                                  report["blocked_upstream"]),
                                 (0, "complete", "census_cleared", []))
                self.assertNotIn(CONFIRMATION_PART,
                                 {p["part"] for p in report["blocked_upstream"]})
                self.assertEqual(self.read(graph), ({GENERAL, PRIVATE}, {CHAT}))
                self.assertEqual(report["content"]["confirmed_not_cleared"], 0)
                self.assertEqual(report["summary"]["units_captured"], 3)
                self.assertEqual(os.readlink(path) if path.is_symlink() else (
                    None if path.is_dir() else path.read_bytes()), before)
                self.assert_nothing_named(stdout)
        # The tracked file the symlink pointed at is untouched.
        self.assertEqual(json.loads((ROOT / "integrations" / "teams_capture_scope.json")
                                    .read_text())["schema"], capture_teams.SCOPE_SCHEMA)

    def test_a_refused_teams_listing_is_the_typed_consent_block_and_no_channel_is_read(self):
        graph = self.fixture()
        graph.forbidden = [r"^/v1\.0/teams$"]
        code, report, _ = self.run_cli(graph)
        self.assert_census_first(graph)
        self.assert_no_confirmation_step(report)
        # No team listed: no channel read; the chat the census cleared still is.
        self.assertEqual(self.read(graph), (set(), {CHAT}))
        self.assertIn({"part": "teams.teams", "source": "microsoft-graph", "http_status": 403,
                       "error_code": "missing_permission", "permission": "Team.ReadBasic.All",
                       "cause": "host_misconfiguration"}, report["blocked_upstream"])
        self.assertEqual((report["census"]["teams"], report["census"]["channels"]), (0, 0))
        self.assertEqual(lv.check_expectations(self.SPEC, code, report,
                                               self.SPEC["expect_blocked"]), [], report)


class ExcludedMemberTests(CensusCase):
    def test_a_team_channel_or_chat_with_an_excluded_member_is_refused_before_its_name(self):
        def team(graph):
            graph.team_members.append((EXCLUDED, EXCLUDED_ADDRESS))

        def channel(graph):
            graph.channel_members[PRIVATE].append((EXCLUDED, EXCLUDED_ADDRESS))

        def chat(graph):
            graph.chat_members.append((EXCLUDED, EXCLUDED_ADDRESS))

        for where, add, kind, never, members, still_read in (
                ("team", team, "teams", [f"/v1.0/teams/{TEAM}", f"/v1.0/teams/{TEAM}/channels"],
                 f"/v1.0/teams/{TEAM}/members", (set(), {CHAT})),
                ("channel", channel, "channels",
                 [f"/v1.0/teams/{TEAM}/channels/{PRIVATE}"],
                 f"/v1.0/teams/{TEAM}/channels/{PRIVATE}/members", ({GENERAL}, {CHAT})),
                ("chat", chat, "chats", [f"/v1.0/chats/{CHAT}"],
                 f"/v1.0/chats/{CHAT}/members", ({GENERAL, PRIVATE}, set()))):
            with self.subTest(where):
                self.fresh()
                graph = self.fixture()
                add(graph)
                code, report, stdout = self.run_cli(graph)
                census = report["census"]
                self.assertEqual(census[f"{kind}_refused"]["people_excluded"], 1, census)
                paths = self.paths(graph)
                for path in never:
                    self.assertNotIn(path, paths)
                refused = {"team": TEAM, "channel": PRIVATE, "chat": CHAT}[where]
                # Refused on the members read that would clear it: no request names it
                # again, not its record (its name) and not its messages, in any run.
                self.assertEqual({p for p in paths if refused in p}, {members})
                self.assertFalse([p for p in paths if refused in p and "/messages" in p])
                # Without a confirmation step the rest of the census is still read, and
                # only that: the refused conversation is never due.
                self.assert_no_confirmation_step(report)
                self.assertEqual(self.read(graph), self.cleared())
                self.assertEqual(self.read(graph), still_read)
                self.assertEqual((report["content"]["channels_due"],
                                  report["content"]["chats_due"]),
                                 (len(still_read[0]), len(still_read[1])))
                self.assertEqual(code, 0, report)
                for url in graph.gets():   # the excluded person is never named in a request
                    self.assertNotIn(EXCLUDED, unquote(url))
                    self.assertNotIn(EXCLUDED_ADDRESS, unquote(url).lower())
                listing = json.dumps(self.census_file())
                self.assertNotIn(refused, listing)   # refused: counted, never listed
                self.assert_nothing_named(stdout, listing)

    def test_a_member_excluded_after_the_census_is_refused_before_its_content(self):
        graph = self.fixture()

        def sender(method, url, *rest):
            reply = graph(method, url, *rest)
            if unquote(urlsplit(url).path) == REPORT:
                graph.team_members.append((EXCLUDED, EXCLUDED_ADDRESS))
            return reply
        sender.channel_types = graph.channel_types
        _code, report, stdout = self.run_cli(sender)
        paths = self.paths(graph)
        self.assertNotIn(f"/v1.0/teams/{TEAM}/channels/{GENERAL}/messages/delta", paths)
        self.assertIn(f"/v1.0/teams/{TEAM}/channels/{PRIVATE}/messages/delta", paths)
        statuses = [c["status"] for r in report["runs"] for c in r["conversations"]
                    if c["kind"] == "channel" and c["membership"] == "standard"]
        self.assertTrue(statuses)
        self.assertEqual(set(statuses), {"people_excluded"})
        self.assert_nothing_named(stdout)

    def test_without_the_guard_nothing_is_requested_and_no_listing_is_kept(self):
        stale = census_path(self.state)
        for name, env, forbidden, part in (
                ("ids_absent", {k: v for k, v in self.env.items()
                                if k != "PLX_EXCLUDED_PEOPLE_OIDS"}, [], None),
                ("directory_refused", self.env, [r"^/v1\.0/users$"], "graph.users")):
            with self.subTest(name):
                stale.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                stale.write_text("{}", encoding="utf-8")
                graph = self.fixture()
                graph.forbidden = forbidden
                code, report, stdout = self.run_cli(graph, env=env)
                ops = [operation(u) for u in graph.gets()
                       if urlsplit(u).hostname == "graph.microsoft.com"]
                self.assertEqual(ops, [part] if part else [])
                self.assertEqual(report["status"], "blocked")
                self.assertIn("teams.people_exclusion",
                              {p["part"] for p in report["blocked_upstream"]})
                self.assertFalse(stale.exists())   # a stale listing is never left behind
                self.assertFalse(report["census"]["file"]["written"])
                self.assertEqual(report["summary"]["conversations"], 0)
                self.assert_no_content_read(graph)
                self.assertEqual(lv.check_expectations(self.SPEC, code, report,
                                                       self.SPEC["expect_blocked"]), [], report)
                self.assert_nothing_named(stdout)


class ManyTeamsGraph(FakeTeamsGraph):
    """The fake tenant plus ``extra`` teams, each with one member (cos@), one standard
    channel and a report row."""

    def __init__(self, extra: int) -> None:
        super().__init__()
        self.users[EXCLUDED_ADDRESS] = EXCLUDED
        self.extra = {f"7eaa0000-0000-0000-0001-{n:012d}": f"19:extra{n:04d}@thread.tacv2"
                      for n in range(extra)}
        for n, team in enumerate(self.extra):
            self.team_names[team] = f"Purchasing {n}"
            self.activity.append({"teamId": team, "details": [
                {"reportPeriod": "30", "channelMessages": n}]})

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        path = unquote(urlsplit(url).path)
        found = re.fullmatch(r"/v1\.0/teams/([0-9a-f-]{36})(/.*)?", path)
        if found and found[1] in self.extra:
            self.calls.append((method, url))
            team, rest, channel = found[1], found[2] or "", self.extra[found[1]]
            if rest == "":
                return j(200, {"id": team, "displayName": self.team_names[team]})
            if rest == "/members":
                return j(200, {"value": [{"userId": U_COS, "email": COS, "roles": []}]})
            if rest == "/channels":
                return j(200, {"value": [{"id": channel, "membershipType": "standard"}]})
            if rest == f"/channels/{channel}":
                return j(200, {"id": channel, "displayName": "Supplier orders",
                               "membershipType": "standard"})
            return j(404, {"error": {"code": "NotFound"}})
        return super().__call__(method, url, headers, body, timeout, max_bytes)


class ManyChatsGraph(FakeTeamsGraph):
    """The fake tenant plus ``extra`` group chats of cos@, each with cos@ as its member and
    one message."""

    def __init__(self, extra: int) -> None:
        super().__init__()
        self.users[EXCLUDED_ADDRESS] = EXCLUDED
        self.extra = [f"19:{n:032x}@thread.v2" for n in range(extra)]

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        path = unquote(urlsplit(url).path)
        if path == f"/v1.0/users/{U_COS}/chats":
            self.calls.append((method, url))
            return j(200, {"value": [{"id": c, "chatType": "group"}
                                     for c in [CHAT, *self.extra]]})
        found = re.fullmatch(r"/v1\.0/chats/([^/]+)(/.*)?", path)
        if found and found[1] in self.extra:
            self.calls.append((method, url))
            chat, rest = found[1], found[2] or ""
            if rest == "":
                return j(200, {"id": chat, "chatType": "group", "topic": "Grants",
                               "tenantId": self.chat_tenant})
            if rest == "/members":
                return j(200, {"value": [{"userId": U_COS, "email": COS, "roles": []}]})
            if rest == "/messages":
                return j(200, {"value": [self.message("9001", "brief")]})
            return j(404, {"error": {"code": "NotFound"}})
        return super().__call__(method, url, headers, body, timeout, max_bytes)


class ChatCensusRotationTests(CensusCase):
    def test_past_max_chats_each_census_starts_after_the_last_and_every_chat_is_reached(self):
        """A chat past ``max_chats`` was never listed while the census always read the
        same first ids (2026-10-27: 4,627 chats against a cap of 200). Each census the
        cap cuts starts after the chat the last one reached, so every chat is cleared and
        its content read within ceil(chats / max_chats) runs, none twice in one census."""
        scope = json.loads((ROOT / "examples" / "teams_capture_scope.live-check.json")
                           .read_text(encoding="utf-8"))
        scope["census"]["max_chats"] = 2
        path = Path(self.home) / "scope.json"
        path.write_text(json.dumps(scope), encoding="utf-8")
        every = None
        cleared: list[str] = []
        content: set[str] = set()
        for _run in range(3):
            graph = ManyChatsGraph(4)
            every = {CHAT, *graph.extra}
            _code, report, stdout = self.run_cli(graph, scope=str(path))
            census = report["census"]
            self.assertEqual(census["chats"], 5)
            self.assertIn("cap:chat_cap", census["reasons"])
            listed = [c["chat_id"] for c in self.census_file()["chats"]]
            self.assertEqual(len(listed), 2)
            self.assertEqual(len(set(listed)), 2)
            cleared += listed
            content |= self.read(graph)[1]
            self.assertNotIn("Grants", stdout)
        self.assertEqual(set(cleared), every)            # none lost
        self.assertEqual(content, every)                 # each one's content read
        self.assertEqual(len(cleared), 6)                # 5 chats, the sixth wraps round
        self.assertEqual(cleared[5], cleared[0])


class CardinalityTests(CensusCase):
    def test_every_team_is_listed_once_with_its_count_at_any_cardinality(self):
        baseline = None
        for extra in (0, 1, 10, 60):
            with self.subTest(extra=extra):
                self.fresh()
                graph = ManyTeamsGraph(extra)
                started = time.monotonic()
                _code, report, stdout = self.run_cli(graph)
                elapsed = time.monotonic() - started
                self.assert_census_first(graph)
                census = report["census"]
                self.assertEqual((census["teams"], census["teams_cleared"],
                                  census["channels"], census["channels_cleared"],
                                  census["status"]),
                                 (1 + extra, 1 + extra, 2 + extra, 2 + extra, "complete"))
                self.assertEqual((census["message_counts"]["teams_counted"],
                                  census["proposals"]["purchasing"]), (1 + extra, extra))
                listing = self.census_file()
                ids = [t["team_id"] for t in listing["teams"]]
                self.assertEqual(sorted(ids), sorted({TEAM, *graph.extra}))   # none lost, none twice
                channels = [c["channel_id"] for t in listing["teams"] for c in t["channels"]]
                self.assertEqual(sorted(channels),
                                 sorted({GENERAL, PRIVATE, *graph.extra.values()}))
                self.assertEqual({t["team_id"]: t["messages_30d"] for t in listing["teams"]
                                  if t["team_id"] in graph.extra},
                                 {t: n for n, t in enumerate(graph.extra)})
                gets = len(self.census_gets(graph))
                baseline = gets if baseline is None else baseline
                # Census reads: members, record, channel listing and one channel record
                # per extra team.
                self.assertEqual(gets - baseline, 4 * extra)
                self.assertLess(elapsed, 30)
                self.assertNotIn("Purchasing", stdout)
                # Every channel the census cleared is due for content: none lost.
                self.assertEqual(report["content"]["channels_due"], 2 + extra)
                self.assertEqual(self.read(graph)[0], set(channels))

    def test_concurrent_reads_list_and_capture_exactly_what_one_at_a_time_does(self):
        """The census and the capture on eight threads (the CLI's default) and on one:
        the same listing, the same conversations read, the same counts; an excluded
        person's team refused before its name either way, and never named in a request."""
        outcomes = {}
        for workers in ("1", str(capture_teams.WORKERS)):
            self.fresh()
            graph = ManyTeamsGraph(30)
            graph.team_members.append((EXCLUDED, EXCLUDED_ADDRESS))   # the fixture's team
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = graph_read.main(self.argv(self.home) + ["--workers", workers],
                                       environ=self.env, sender=graph)
            report = json.loads(out.getvalue())
            for url in graph.gets():
                self.assertNotIn(EXCLUDED, unquote(url))
                self.assertNotIn(EXCLUDED_ADDRESS, unquote(url).lower())
            self.assertFalse([p for p in self.paths(graph) if p.startswith(f"/v1.0/teams/{TEAM}")
                              and p != f"/v1.0/teams/{TEAM}/members"])
            census = report["census"]
            outcomes[workers] = (
                self.census_file(), self.read(graph),
                {k: census[k] for k in ("teams", "teams_cleared", "teams_refused",
                                        "channels", "channels_cleared", "chats",
                                        "chats_cleared", "status")},
                report["status"], code, sorted(self.paths(graph)))
        one, many = outcomes.values()
        self.assertEqual(one, many)
        self.assertEqual(one[2]["teams_refused"]["people_excluded"], 1)
        self.assertEqual(len(one[1][0]), 30)   # every extra team's channel read

    def test_past_max_teams_the_census_stops_reading_and_reports_the_cap(self):
        scope = json.loads((ROOT / "examples" / "teams_capture_scope.live-check.json")
                           .read_text(encoding="utf-8"))
        scope["census"]["max_teams"] = 5
        path = Path(self.home) / "scope.json"
        path.write_text(json.dumps(scope), encoding="utf-8")
        graph = ManyTeamsGraph(10)
        _code, report, _ = self.run_cli(graph, scope=str(path))
        census = report["census"]
        self.assertEqual((census["teams"], census["teams_cleared"], census["status"]),
                         (11, 5, "incomplete"))
        self.assertIn("cap:team_cap", census["reasons"])
        self.assertEqual(len(self.census_file()["teams"]), 5)
        self.assertNotEqual(report["status"], "complete")


class ActivityReportTests(CensusCase):
    def test_a_report_refused_redirected_or_unmatched_leaves_counts_unset_not_zero(self):
        def refused(graph):
            graph.forbidden = [r"/reports/"]
            return graph

        def redirected(graph):
            def sender(method, url, *rest):
                if unquote(urlsplit(url).path) == REPORT:
                    graph.calls.append((method, url))
                    return 302, {"Location": "https://reports.office.com/d30.csv"}, b""
                return graph(method, url, *rest)
            return sender

        def unmatched(graph):
            graph.activity[:] = [{"teamId": OTHER_TEAM, "details": [
                {"reportPeriod": "30", "channelMessages": 7}]}]
            return graph

        for name, change, reason in (("refused", refused, None),
                                     ("redirect", redirected, "report_redirect"),
                                     ("unmatched", unmatched, "report_unmatched")):
            with self.subTest(name):
                self.fresh()
                graph = self.fixture()
                _code, report, _ = self.run_cli(change(graph))
                self.assert_census_first(graph)
                self.assert_no_confirmation_step(report)
                counts = report["census"]["message_counts"]
                self.assertEqual((counts["teams_counted"], counts["status"]),
                                 (0, "unavailable"))
                (team,) = self.census_file()["teams"]
                self.assertIsNone(team["messages_30d"])
                # Scored against the spec on disk: a typed block or a pass, never a fail.
                self.assertEqual(lv.check_expectations(
                    self.SPEC, _code, report,
                    *([] if reason else [self.SPEC["expect_blocked"]])), [], report)
                self.assertNotIn(OTHER_TEAM, json.dumps(self.census_file()))
                self.assertNotIn("reports.office.com", json.dumps(report))
                if reason:
                    self.assertEqual(team["count_reason"], reason)
                    self.assertIn(reason, counts["reasons"])
                    # A missing count holds nothing back: the census is complete, no
                    # part is blocked and everything it cleared is read.
                    self.assertEqual(report["census"]["status"], "complete")
                    self.assertEqual(lv.blocked_parts(report), [])
                    self.assertEqual(self.read(graph), ({GENERAL, PRIVATE}, {CHAT}))
                else:
                    self.assertIn({"part": "teams.activity_report",
                                   "source": "microsoft-graph", "http_status": 403,
                                   "error_code": "missing_permission",
                                   "permission": "Reports.Read.All",
                                   "cause": "host_misconfiguration"},
                                  report["blocked_upstream"])

    def test_a_redirected_report_still_lets_a_census_cleared_run_pass_the_spec(self):
        """The v1.0 report answers 302 to a CSV it never follows: counts stay None, the
        census completes and a consented run of what it cleared passes the spec on disk."""
        graph = self.fixture()

        def sender(method, url, *rest):
            if unquote(urlsplit(url).path) == REPORT:
                graph.calls.append((method, url))
                return 302, {"Location": "https://reports.office.com/d30.csv"}, b""
            return graph(method, url, *rest)

        code, report, _ = self.run_cli(sender)
        self.assertEqual((report["census"]["status"],
                          report["census"]["message_counts"]["status"],
                          report["content"]["status"]),
                         ("complete", "unavailable", "census_cleared"))
        self.assert_no_confirmation_step(report)
        self.assertEqual(lv.check_expectations(self.SPEC, code, report), [], report)
        self.assertTrue(lv.completeness(self.SPEC, report)["complete"])
        self.assertNotIn("reports.office.com", json.dumps(lv._recorded(self.SPEC, report)))


if __name__ == "__main__":
    unittest.main()
