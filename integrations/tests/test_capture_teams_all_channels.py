"""Teams everything: every channel the census clears (evidence label: injected-transport).

Owner directive "Teams everything" (2026-10-21): the capture widens from the teams a
member joined to every team the census enumerates and the people guard clears. Owner
decision 2026-10-26: that set is read with no human content confirmation; a
``content-confirmation.json`` beside the census is neither required nor consulted.
A team, channel or chat with an excluded member is refused before any request naming
it, and the client's request log proves no request did; chats without
``Chat.Read.All`` stay their own blocked part. The live-check spec
``graph.teams_all_channels`` runs its own argv here over an in-process fake Graph:
no network, no credentials.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

from devloop import live_verify as lv
from integrations import capture_teams, graph_read
from integrations.capture_teams import (CONFIRMATION_PART, CONFIRMATION_SCHEMA, TeamsCapture,
                                        TeamsCaptureError, TeamsScope, census_path,
                                        confirmation_path, default_state_dir)
from integrations.tests.test_capture_teams import (CHAT, COS, ENV, GENERAL, PRIVATE,
                                                   SECRET_TEXT, TEAM, U_COS, j)
from integrations.tests.test_capture_teams_census import (EXCLUDED, EXCLUDED_ADDRESS,
                                                          ManyTeamsGraph)

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / lv.SPECS_DIR_NAME / "graph.teams_all_channels.json").read_text())
SHIPPED = (capture_teams.DEFAULT_SCOPE,
           ROOT / "examples" / "teams_capture_scope.all_channels.live-check.json")
# A team the tenant lists whose members hold the excluded person, and a one-on-one chat
# of cos@ with that person (its id embeds both object ids).
EXCLUDED_TEAM = "7eaa0000-0000-0000-0002-0000000000e1"
EXCLUDED_CHAT = f"19:{U_COS}_{EXCLUDED}@unq.gbl.spaces"
DIRECTIVE = "5e1f0c2a9b7d"


class AllTeamsGraph(ManyTeamsGraph):
    """The census fake (one joined team, ``extra`` teams nobody in the scope joined, each
    with one standard channel holding one message) plus a team and a one-on-one chat of
    the excluded person. Every request is recorded before it is answered."""

    def __init__(self, extra: int) -> None:
        super().__init__(extra)
        self.team_names[EXCLUDED_TEAM] = "Payroll"
        for team, channel in self.extra.items():
            root = self.message(f"9{len(self.roots):04d}", SECRET_TEXT)
            del root["chatId"]
            root["channelIdentity"] = {"teamId": team, "channelId": channel}
            self.roots[channel] = {root["id"]: root}

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        path = unquote(urlsplit(url).path)
        if any(re.search(p, path) for p in [*self.forbidden, *self.failing]):
            return super().__call__(method, url, headers, body, timeout, max_bytes)
        if path == f"/v1.0/teams/{EXCLUDED_TEAM}/members":
            self.calls.append((method, url))
            return j(200, {"value": [{"userId": U_COS, "email": COS, "roles": []},
                                     {"userId": EXCLUDED, "email": EXCLUDED_ADDRESS,
                                      "roles": []}]})
        if path == f"/v1.0/users/{U_COS}/chats":
            self.calls.append((method, url))
            return j(200, {"value": [{"id": CHAT, "chatType": "oneOnOne"},
                                     {"id": EXCLUDED_CHAT, "chatType": "oneOnOne"}]})
        found = re.fullmatch(r"/v1\.0/teams/([0-9a-f-]{36})/channels/([^/]+)/messages"
                             r"(/delta|/\d+/replies)", path)
        if found and found[1] in self.extra and self.extra[found[1]] == found[2]:
            self.calls.append((method, url))
            base = f"https://graph.microsoft.com{urlsplit(url).path}"
            if found[3] == "/delta":
                return j(200, {"value": list(self.roots[found[2]].values()),
                               "@odata.deltaLink": f"{base}?$deltatoken={self.seq}"})
            return j(200, {"value": []})
        return super().__call__(method, url, headers, body, timeout, max_bytes)


class AllChannelsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**ENV, "XDG_STATE_HOME": self.tmp.name}
        self.state = default_state_dir(self.env, injected=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fresh(self) -> None:
        self.tmp.cleanup()
        self.setUp()

    def fixture(self, extra: int = 3) -> AllTeamsGraph:
        graph = AllTeamsGraph(extra)
        graph.post_root(GENERAL, "1001", replies=1)
        graph.post_root(PRIVATE, "2001")
        graph.post_chat("3001")
        return graph

    def confirm_all(self, **fields) -> Path:
        """An old all-form confirmation as Tanush once wrote it beside the census (a
        fixture shaped as human-authored; ``fields`` replace or, set to None, drop its
        keys). Since 2026-10-26 the run neither needs nor reads it."""
        document = {"schema": CONFIRMATION_SCHEMA, "scope": "all_census_cleared",
                    "confirmed_by": "Tanush Shah", "confirmed_at": "2026-10-21",
                    "directive": DIRECTIVE, "note": "Teams everything"}
        document.update(fields)
        path = confirmation_path(self.state)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({k: v for k, v in document.items() if v is not None}),
                        encoding="utf-8")
        return path

    def run_spec(self, graph) -> tuple[int, dict, str]:
        argv = [str(ROOT / a) if a.startswith("examples/") else a for a in SPEC["command"][3:]]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = graph_read.main(argv, environ=self.env, sender=graph)
        return code, json.loads(out.getvalue()), out.getvalue()

    @staticmethod
    def urls(graph) -> list[str]:
        return [unquote(u).lower() for u in graph.gets()
                if urlsplit(u).hostname == "graph.microsoft.com"]

    def cleared(self) -> tuple[set[tuple[str, str]], set[str]]:
        listing = json.loads(census_path(self.state).read_text(encoding="utf-8"))
        return ({(t["team_id"], c["channel_id"]) for t in listing["teams"]
                 for c in t["channels"]}, {c["chat_id"] for c in listing["chats"]})

    def read(self, graph) -> tuple[set[tuple[str, str]], set[str]]:
        """The channels whose message delta and the chats whose messages were requested."""
        channels, chats = set(), set()
        for url in graph.gets():
            path = unquote(urlsplit(url).path)
            found = re.fullmatch(r"/v1\.0/teams/([^/]+)/channels/([^/]+)/messages/delta", path)
            if found:
                channels.add((found[1], found[2]))
            found = re.fullmatch(r"/v1\.0/chats/([^/]+)/messages", path)
            if found:
                chats.add(found[1])
        return channels, chats

    def assert_nothing_named(self, text: str) -> None:
        for value in (EXCLUDED, EXCLUDED_ADDRESS, EXCLUDED_TEAM, EXCLUDED_CHAT, TEAM, GENERAL,
                      CHAT, COS, SECRET_TEXT, "Payroll", "Operations", "do-not-leak", "TOKEN"):
            self.assertNotIn(value, text)


class AllFormTests(AllChannelsCase):
    def test_the_all_form_reads_exactly_the_cleared_set_wider_than_member_joined(self):
        graph = self.fixture(extra=3)
        code, report, stdout = self.run_spec(graph)
        self.assertEqual(lv.check_expectations(SPEC, code, report), [], report)
        self.assertEqual((report["evidence_label"], lv.blocked_parts(report)),
                         ("injected-transport", []))
        channels, chats = self.cleared()
        # The census cleared the joined team's 2 channels and the 3 nobody joined; the
        # excluded person's team and one-on-one chat are refused, never cleared.
        self.assertEqual(len(channels), 5)
        self.assertEqual(chats, {CHAT})
        self.assertEqual(self.read(graph), (channels, chats))
        content, coverage = report["content"], report["coverage"]
        self.assertEqual((content["form"], content["status"], content["confirmation"],
                          content["confirmed_channels"], content["confirmed_chats"],
                          content["confirmed_not_cleared"], content["channels_due"],
                          content["chats_due"]),
                         ("all_census_cleared", "census_cleared", "not_required",
                          5, 1, 0, 5, 1))
        self.assertFalse(confirmation_path(self.state).exists())   # code never writes it
        self.assertEqual({k: coverage[k] for k in (
            "channels_enumerated", "channels_member_joined", "wider_than_member_joined",
            "channels_captured", "chats_captured", "excluded_requested", "blocked_unnamed")},
            {"channels_enumerated": 5, "channels_member_joined": 2,
             "wider_than_member_joined": True, "channels_captured": 5, "chats_captured": 1,
             "excluded_requested": 0, "blocked_unnamed": 0})
        self.assertEqual(report["census"]["member_joined"],
                         {"accounts": 2, "accounts_excluded": 0, "teams": 1, "channels": 2})
        self.assert_nothing_named(stdout)
        # What the verifier records: counts, booleans and enums only.
        recorded = lv._recorded(SPEC, report)
        self.assertEqual(recorded["coverage"], {k: coverage[k] for k in coverage})
        self.assertEqual(recorded["content.form"], "all_census_cleared")
        self.assert_nothing_named(json.dumps(recorded))
        # A tenant whose teams the members all joined is no wider: the check fails.
        self.fresh()
        graph = self.fixture(extra=0)
        code, report, _ = self.run_spec(graph)
        self.assertIs(report["coverage"]["wider_than_member_joined"], False)
        self.assertIn("coverage.wider_than_member_joined != expected",
                      lv.check_expectations(SPEC, code, report))

    def test_no_confirmation_old_or_malformed_changes_what_is_read(self):
        """Absent, an old all form, or one malformed or code-written: none blocks the run
        and none widens it. Exactly the census-cleared set is read, the excluded person's
        team and chat never are, and the file is left as it was."""
        cases = [
            ("absent", None),
            ("old_all_form", {}),
            ("channels_beside_all", {"channels": [{"team_id": TEAM, "channel_id": GENERAL}]}),
            ("other_scope", {"scope": "all_teams"}),
            ("not_a_date", {"confirmed_at": "2026-13-01"}),
            ("an_agent", {"confirmed_by": "Claude Opus"}),
            ("an_address", {"confirmed_by": COS}),
        ]
        for name, fields in cases:
            with self.subTest(name):
                self.fresh()
                path = self.confirm_all(**fields) if fields is not None else None
                before = path.read_bytes() if path else None
                graph = self.fixture()
                code, report, stdout = self.run_spec(graph)
                self.assertEqual(lv.check_expectations(SPEC, code, report), [], report)
                self.assertEqual((code, report["status"], report["blocked_upstream"]),
                                 (0, "complete", []), report)
                self.assertNotIn(CONFIRMATION_PART, json.dumps(report))
                self.assertEqual((report["content"]["status"],
                                  report["content"]["confirmation"],
                                  report["content"]["confirmed_not_cleared"]),
                                 ("census_cleared", "not_required", 0))
                channels, chats = self.cleared()
                self.assertEqual((len(channels), chats), (5, {CHAT}))
                self.assertEqual(self.read(graph), (channels, chats))
                urls = self.urls(graph)
                for value in (EXCLUDED, EXCLUDED_ADDRESS, EXCLUDED_CHAT.lower()):
                    self.assertFalse([u for u in urls if value in u], value)
                self.assertFalse([u for u in urls if EXCLUDED_TEAM in u and "/members" not in u])
                self.assertEqual(report["coverage"]["excluded_requested"], 0)
                self.assert_nothing_named(stdout)
                if path is None:   # code never writes or defaults a confirmation
                    self.assertFalse(confirmation_path(self.state).exists())
                else:              # nor rewrites or removes one left over
                    self.assertEqual(path.read_bytes(), before)
        # A scope naming the cleared set by another word fails closed before any request.
        document = json.loads(SHIPPED[1].read_text(encoding="utf-8"))
        for teams in ("all", "all_member_joined", True):
            with self.assertRaises(TeamsCaptureError):
                TeamsScope({**document, "teams": teams})
        for path in SHIPPED:
            self.assertTrue(TeamsScope.from_file(path).all_census_cleared, path)


class ExcludedPeopleTests(AllChannelsCase):
    def test_excluded_members_never_reach_a_request_url(self):
        graph = self.fixture()
        code, report, stdout = self.run_spec(graph)
        self.assertEqual(code, 0, report)
        urls = self.urls(graph)
        for value in (EXCLUDED, EXCLUDED_ADDRESS, EXCLUDED_CHAT.lower()):
            self.assertFalse([u for u in urls if value in u], value)
        # The team is refused on the members read that cleared it: nothing more of it.
        self.assertEqual([u for u in urls if EXCLUDED_TEAM in u],
                         [f"https://graph.microsoft.com/v1.0/teams/{EXCLUDED_TEAM}/members"])
        census = report["census"]
        self.assertEqual((census["teams_refused"]["people_excluded"],
                          census["chats_refused"]["people_excluded"]), (1, 1))
        coverage = report["coverage"]
        self.assertEqual((coverage["excluded_requested"], coverage["requests_logged"]),
                         (0, report["gets_sent"]))
        self.assertEqual(coverage["requests_logged"], len(graph.gets()))
        self.assert_nothing_named(stdout)

    def test_the_request_log_catches_a_guard_that_lets_an_excluded_chat_through(self):
        # The log is not vacuous: with the chat-id check disabled, the one-on-one chat is
        # requested (its members read refuses it after), and the run is not ok.
        graph = self.fixture()
        with mock.patch.object(TeamsCapture, "_id_excluded", return_value=False):
            code, report, _ = self.run_spec(graph)
        self.assertTrue([u for u in self.urls(graph) if EXCLUDED_CHAT.lower() in u])
        self.assertGreaterEqual(report["coverage"]["excluded_requested"], 1)
        self.assertEqual((code, report["ok"]), (1, False))
        self.assertIn("coverage.excluded_requested != expected",
                      lv.check_expectations(SPEC, code, report))


class ChatPermissionTests(AllChannelsCase):
    def test_chats_without_permission_stay_their_own_blocked_part(self):
        for forbidden, part, permission in (
                (r"/chats/[^/]+/messages$", "teams.chat_messages", "Chat.Read.All"),
                (r"/users/[^/]+/chats$", "teams.chats", "Chat.ReadBasic.All")):
            with self.subTest(part):
                self.fresh()
                graph = self.fixture()
                graph.forbidden = [forbidden]
                code, report, stdout = self.run_spec(graph)
                self.assertEqual((code, report["status"]), (1, "blocked"))
                self.assertEqual({(p["part"], p["error_code"], p.get("permission"))
                                  for p in report["blocked_upstream"]},
                                 {(part, "missing_permission", permission)})
                coverage = report["coverage"]
                self.assertEqual((coverage["channels_captured"], coverage["chats_captured"]),
                                 (5, 0))
                self.assertEqual(lv.check_expectations(SPEC, code, report,
                                                       SPEC["expect_blocked"]), [], report)
                self.assert_nothing_named(stdout)
        # A block naming no permission (a refused token, 401) is not one the check accepts.
        self.fresh()
        graph = self.fixture()
        graph.failing = {r"/chats/[^/]+/messages$": 401}
        code, report, _ = self.run_spec(graph)
        self.assertEqual(report["coverage"]["blocked_unnamed"], 1)
        self.assertIn("coverage.blocked_unnamed != expected",
                      lv.check_expectations(SPEC, code, report, SPEC["expect_blocked"]))


class CardinalityTests(AllChannelsCase):
    def test_every_cleared_channel_is_read_once_at_any_cardinality(self):
        for extra in (1, 10, 60):
            with self.subTest(extra=extra):
                self.fresh()
                graph = self.fixture(extra)
                started = time.monotonic()
                code, report, _ = self.run_spec(graph)
                elapsed = time.monotonic() - started
                self.assertEqual(lv.check_expectations(SPEC, code, report), [], report)
                channels, _chats = self.cleared()
                self.assertEqual(len(channels), 2 + extra)
                self.assertEqual(self.read(graph)[0], channels)
                deltas = [u for u in graph.gets() if urlsplit(u).path.endswith("/delta")]
                # One delta read per channel and run that took it: never twice in a run.
                self.assertEqual(len(deltas), len(set(deltas)))
                coverage = report["coverage"]
                self.assertEqual((coverage["channels_enumerated"],
                                  coverage["channels_captured"]), (2 + extra, 2 + extra))
                # 2 roots and 1 reply in the joined team, one root in each other channel,
                # one chat message: no row lost or duplicated.
                self.assertEqual(report["relations"],
                                 {"channelMessages": 3 + extra, "chatMessages": 1})
                self.assertLess(elapsed, 60, extra)


if __name__ == "__main__":
    unittest.main()
