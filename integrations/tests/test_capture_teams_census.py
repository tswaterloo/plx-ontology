"""Teams census before any content read (evidence label: injected-transport).

``teams-capture`` first lists the tenant's
teams, channels and the allowlisted accounts' chats (names into the census file only,
30-day message counts from the team activity report), proposes the purchasing, QA and
R&D ones, and reads content only of what the owner's confirmation names and this
census cleared. A team, channel or chat with an excluded member is refused by the
shared people guard before its name or content is requested; without the guard, or
without a usable confirmation, nothing is read and the run is blocked on a later ask.
No getAllMessages, no subscription, no message body in a census. Over an in-process
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
from integrations.capture_teams import (CENSUS_OPERATIONS, CONFIRMATION_LATER_ASK,
                                        CONFIRMATION_PART,
                                        census_path, confirmation_block, confirmation_path,
                                        default_state_dir)
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

    def assert_census_only(self, graph) -> None:
        """Every Graph read is a census operation: listings, memberships, records and the
        activity report; never a message, getAllMessages or a subscription."""
        self.assert_no_content_read(graph)
        ops = [operation(u) for u in graph.gets()
               if urlsplit(u).hostname == "graph.microsoft.com"]
        self.assertTrue(ops)
        self.assertLessEqual(set(ops), CENSUS_OPERATIONS, ops)

    def assert_nothing_named(self, *texts: str) -> None:
        for text in texts:
            for value in (EXCLUDED, EXCLUDED_ADDRESS, SECRET_TEXT, "do-not-leak", "TOKEN"):
                self.assertNotIn(value, text)


class CensusBeforeConfirmationTests(CensusCase):
    def test_without_a_confirmation_only_names_and_counts_are_read_and_proposed(self):
        graph = self.fixture()
        graph.activity.append({"teamId": OTHER_TEAM, "details": [
            {"reportPeriod": "30", "channelMessages": 99}]})
        code, report, stdout = self.run_cli(graph)
        self.assert_census_only(graph)
        self.assertEqual((code, report["status"], report["content"]["status"]),
                         (1, "blocked", "awaiting_confirmation"))
        self.assertEqual(report["blocked_upstream"],
                         [confirmation_block("content_confirmation_absent")])
        self.assertEqual(lv.blocked_parts(report), [{
            "part": "teams.content_confirmation", "source": "owner-confirmation",
            "error_code": "content_confirmation_absent", "cause": "later_ask"}])
        self.assertEqual(report["blocked_upstream"][0]["later_ask"], CONFIRMATION_LATER_ASK)
        self.assertEqual((report["summary"]["conversations"], report["pass"]["stop"],
                          report["runs"]), (0, "blocked", []))
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
        self.assertEqual(lv.check_expectations(self.SPEC, code, report,
                                               self.SPEC["expect_blocked"]), [], report)
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

    def test_a_confirmation_reads_only_what_it_names_and_the_census_cleared(self):
        graph = self.fixture()
        self.confirm(channels=[PRIVATE, "19:gone0009@thread.tacv2"], chats=())
        _code, report, stdout = self.run_cli(graph)
        content = report["content"]
        self.assertEqual((content["status"], content["channels_due"], content["chats_due"],
                          content["confirmed_not_cleared"]), ("confirmed", 1, 0, 1))
        read = [p for p in self.paths(graph) if "/messages" in p]
        self.assertIn(f"/v1.0/teams/{TEAM}/channels/{PRIVATE}/messages/delta", read)
        self.assertEqual([p for p in read if PRIVATE not in p], [])
        self.assertEqual(report["summary"]["units_captured"], 1)
        self.assert_nothing_named(stdout)

    def test_a_refused_teams_listing_is_the_typed_consent_block_beside_the_later_ask(self):
        graph = self.fixture()
        graph.forbidden = [r"^/v1\.0/teams$"]
        code, report, _ = self.run_cli(graph)
        self.assert_census_only(graph)
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

        for where, add, kind, never, left_out in (
                ("team", team, "teams", [f"/v1.0/teams/{TEAM}", f"/v1.0/teams/{TEAM}/channels"],
                 2),
                ("channel", channel, "channels",
                 [f"/v1.0/teams/{TEAM}/channels/{PRIVATE}"], 1),
                ("chat", chat, "chats", [f"/v1.0/chats/{CHAT}"], 1)):
            with self.subTest(where):
                self.fresh()
                self.confirm()
                graph = self.fixture()
                add(graph)
                _code, report, stdout = self.run_cli(graph)
                census = report["census"]
                self.assertEqual(census[f"{kind}_refused"]["people_excluded"], 1, census)
                paths = self.paths(graph)
                for path in never:
                    self.assertNotIn(path, paths)
                refused = {"team": TEAM, "channel": PRIVATE, "chat": CHAT}[where]
                self.assertFalse([p for p in paths if refused in p and "/messages" in p])
                self.assertEqual(report["content"]["confirmed_not_cleared"], left_out)
                listing = json.dumps(self.census_file())
                self.assertNotIn(refused, listing)   # refused: counted, never listed
                self.assert_nothing_named(stdout, listing)

    def test_a_member_excluded_after_the_census_is_refused_before_its_content(self):
        graph = self.fixture()
        self.confirm()

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
                self.confirm()
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
                self.assertEqual(lv.check_expectations(self.SPEC, code, report,
                                                       self.SPEC["expect_blocked"]), [], report)
                self.assert_nothing_named(stdout)


class ConfirmationTests(CensusCase):
    def test_an_unusable_confirmation_reads_no_content_and_blocks_on_the_owner(self):
        good = {"schema": capture_teams.CONFIRMATION_SCHEMA, "confirmed_by": "owner",
                "confirmed_at": "2026-10-21",
                "channels": [{"team_id": TEAM, "channel_id": GENERAL}], "chats": [CHAT]}
        cases = (
            ("schema", {**good, "schema": "plx-teams-content-confirmation/v0"},
             "content_confirmation_invalid"),
            ("unknown_key", {**good, "approve_all": True}, "content_confirmation_invalid"),
            ("date", {**good, "confirmed_at": "21 Oct 2026"}, "content_confirmation_invalid"),
            ("no_signer", {**good, "confirmed_by": " "}, "content_confirmation_invalid"),
            ("channel_shape", {**good, "channels": [{"channel_id": GENERAL}]},
             "content_confirmation_invalid"),
            ("wildcard_chat", {**good, "chats": ["*"]}, "content_confirmation_invalid"),
            ("empty", {**good, "channels": [], "chats": []}, "content_confirmation_empty"),
            ("not_json", "{not json", "content_confirmation_unreadable"),
            ("oversize", {**good, "note": "x" * (capture_teams.MAX_CONFIRMATION_BYTES + 1)},
             "content_confirmation_unreadable"),
            ("directory", None, "content_confirmation_unreadable"),
            ("symlink_loop", "loop", "content_confirmation_unreadable"),
            ("in_git", ROOT / "integrations" / "teams_capture_scope.json",
             "content_confirmation_in_git"),
            ("only_unknown", {**good, "channels": [], "chats": [
                "19:ffff0000-0000-0000-0000-000000000009_0000ffff@unq.gbl.spaces"]},
             "content_confirmation_none_cleared"))
        for name, document, wanted in cases:
            with self.subTest(name):
                self.fresh()
                path = confirmation_path(self.state)
                path.parent.mkdir(parents=True, mode=0o700)
                if document is None:
                    path.mkdir()
                elif name == "symlink_loop":
                    os.symlink(path, path)
                elif isinstance(document, Path):
                    os.symlink(document, path)
                else:
                    path.write_text(document if isinstance(document, str)
                                    else json.dumps(document), encoding="utf-8")
                graph = self.fixture()
                code, report, _ = self.run_cli(graph)
                self.assert_census_only(graph)
                self.assertIn(confirmation_block(wanted), report["blocked_upstream"])
                self.assertEqual(report["content"]["status"],
                                 "none_cleared" if name == "only_unknown"
                                 else "confirmation_refused")
                self.assertEqual((code, report["status"], report["summary"]["conversations"]),
                                 (1, "blocked", 0))
                self.assertEqual(lv.check_expectations(self.SPEC, code, report,
                                                       self.SPEC["expect_blocked"]), [], report)
        # The tracked file the symlink pointed at is untouched.
        self.assertEqual(json.loads((ROOT / "integrations" / "teams_capture_scope.json")
                                    .read_text())["schema"], capture_teams.SCOPE_SCHEMA)


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
                self.assert_census_only(graph)
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
                gets = len(graph.gets())
                baseline = gets if baseline is None else baseline
                # Members, record, channel listing and one channel record per extra team.
                self.assertEqual(gets - baseline, 4 * extra)
                self.assertLess(elapsed, 30)
                self.assertNotIn("Purchasing", stdout)

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
                self.assert_census_only(graph)
                counts = report["census"]["message_counts"]
                self.assertEqual((counts["teams_counted"], counts["status"]),
                                 (0, "unavailable"))
                (team,) = self.census_file()["teams"]
                self.assertIsNone(team["messages_30d"])
                # Scored against the spec on disk: a typed block, never a fail.
                self.assertEqual(lv.check_expectations(self.SPEC, _code, report,
                                                       self.SPEC["expect_blocked"]), [],
                                 report)
                self.assertNotIn(OTHER_TEAM, json.dumps(self.census_file()))
                self.assertNotIn("reports.office.com", json.dumps(report))
                if reason:
                    self.assertEqual(team["count_reason"], reason)
                    self.assertIn(reason, counts["reasons"])
                    # A missing count holds nothing back: the census is complete and
                    # the run waits on the owner's confirmation only.
                    self.assertEqual(report["census"]["status"], "complete")
                    self.assertEqual([(p["part"], p["cause"])
                                      for p in lv.blocked_parts(report)],
                                     [(CONFIRMATION_PART, "later_ask")])
                else:
                    self.assertIn({"part": "teams.activity_report",
                                   "source": "microsoft-graph", "http_status": 403,
                                   "error_code": "missing_permission",
                                   "permission": "Reports.Read.All",
                                   "cause": "host_misconfiguration"},
                                  report["blocked_upstream"])

    def test_a_redirected_report_still_lets_a_confirmed_run_pass_the_spec(self):
        """The v1.0 report answers 302 to a CSV it never follows: counts stay None, the
        census completes and a consented, confirmed run passes the spec on disk."""
        graph = self.fixture()

        def sender(method, url, *rest):
            if unquote(urlsplit(url).path) == REPORT:
                graph.calls.append((method, url))
                return 302, {"Location": "https://reports.office.com/d30.csv"}, b""
            return graph(method, url, *rest)

        self.confirm()
        code, report, _ = self.run_cli(sender)
        self.assertEqual((report["census"]["status"],
                          report["census"]["message_counts"]["status"],
                          report["content"]["status"]), ("complete", "unavailable", "confirmed"))
        self.assertEqual(lv.check_expectations(self.SPEC, code, report), [], report)
        self.assertTrue(lv.completeness(self.SPEC, report)["complete"])
        self.assertNotIn("reports.office.com", json.dumps(lv._recorded(self.SPEC, report)))


if __name__ == "__main__":
    unittest.main()
