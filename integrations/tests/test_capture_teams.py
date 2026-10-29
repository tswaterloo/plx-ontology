"""Teams capture over an injected Graph transport (evidence label: injected-transport).

No network and no credentials: every request goes to an in-process fake Graph
(directory, joined teams, chats, channels, membership, channel message delta with
skip/delta tokens, replies, chat message listing). Covers the rules the capture
must hold: GET-only through the closed grammar, paging/caps/resume/delta with
honest completeness, membership trimming for a reader outside a channel, a 403
reported as ``blocked: missing_permission`` (a chat read under its own part), whose
chats are read (every account of the mail allowlist, derived from it), the live-check
spec against the fixture run, its state carried across daily runs in the host state
directory and its time budget.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from devloop import live_verify as lv
from integrations import capture_teams, graph_read
from integrations import live_source_grammar as grammar
from integrations.capture_teams import Conversation, TeamsCapture, TeamsScope, open_store
from integrations.live_source_guard import (BoundedBackoff, LiveSourceAudience,
                                            ReadOnlyGuardError, RequestDeadline,
                                            check_request)
from ontology.project_graph import TeamsCaptures
from ontology.types import Actor
from operating.case_mail_scope import MailScope

ROOT = Path(__file__).resolve().parents[2]
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": "11111111-2222-3333-4444-555555555555",
       "MICROSOFT_GRAPH_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
       "MICROSOFT_GRAPH_CLIENT_SECRET": "s3cr3t~CLIENT~value~do-not-leak"}
COS, VINCE, OTHER = "cos@petrasoap.com", "vince@petrasoap.com", "guest@example-plx.com"
U_COS = "0000aaaa-0000-0000-0000-000000000001"
U_VINCE = "0000aaaa-0000-0000-0000-000000000002"
U_OTHER = "0000aaaa-0000-0000-0000-000000000003"
TEAM = "7eaa0000-0000-0000-0000-00000000000a"
GENERAL = "19:general0001@thread.tacv2"
PRIVATE = "19:private0002@thread.tacv2"
CHAT = "19:0000aaaa-0000-0000-0000-000000000001_0000aaaa@unq.gbl.spaces"
SECRET_TEXT = "Freight quote for PO12172025 is 4,200 USD"
FULL_HISTORY = "0001-01-01T00:00:00Z"   # Graph's visibleHistoryStartDateTime: all history

CAPTURE = Actor("plx", frozenset({"svc:teams-capture"}), "restricted")
MEMBER = Actor("plx", frozenset({"staff:teams-member"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:teams-outsider"}), "restricted")
COS_ONLY = Actor("plx", frozenset({"staff:cos"}), "restricted")
VINCE_ONLY = Actor("plx", frozenset({"staff:vince"}), "restricted")


def j(status, value):
    return status, {}, json.dumps(value).encode()


class FakeTeamsGraph:
    """Graph for one team (a standard and a private channel) and one chat."""

    def __init__(self) -> None:
        # Every account of the built-in allowlist resolves: the shipped scopes read their chats.
        self.users = {address: f"0000bbbb-0000-0000-0000-{n:012d}" for n, address
                      in enumerate(sorted(MailScope.builtin().addresses))}
        self.users.update({COS: U_COS, VINCE: U_VINCE})
        self.team_members = [(U_COS, COS), (U_VINCE, VINCE)]
        self.channel_members = {PRIVATE: [(U_VINCE, VINCE)]}
        self.chat_members = [(U_COS, COS), (U_OTHER, OTHER)]
        # A member's visibleHistoryStartDateTime (default: the whole history; None: absent).
        self.history: dict[str, str | None] = {}
        self.seq = 0
        self.roots: dict[str, dict[str, dict]] = {GENERAL: {}, PRIVATE: {}}
        self.changed: dict[str, dict[str, int]] = {GENERAL: {}, PRIVATE: {}}
        self.replies: dict[tuple[str, str], list[dict]] = {}
        self.chat: dict[str, dict] = {}
        self.forbidden: list[str] = []
        self.failing: dict[str, int] = {}       # path pattern -> HTTP status answered
        self.expired: set[str] = set()          # continuation URLs answering 410
        self.channel_types = {GENERAL: "standard", PRIVATE: "private"}
        # The tenant hosting CHAT (ENV's own by default; a cross-tenant chat otherwise).
        self.chat_tenant = ENV["MICROSOFT_GRAPH_TENANT_ID"]
        # Channels whose delta, while they hold no message, answers an empty page and a
        # nextLink Graph then refuses with 400 (live 2026-10-27).
        self.refused_empty_delta: set[str] = set()
        # The census's reads: the tenant's teams, names, and the 30-day activity report.
        self.team_names = {TEAM: "Operations"}
        self.channel_names = {GENERAL: "General", PRIVATE: "QA hand-offs"}
        self.chat_topics = {CHAT: "R&D Bi-weekly Meeting"}
        self.activity: list[dict] = [{"teamId": TEAM, "details": [
            {"reportPeriod": "30", "channelMessages": 12}]}]
        self.calls: list[tuple[str, str]] = []

    def _stamp(self) -> str:
        self.seq += 1
        moment = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=10 * self.seq)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def message(self, message_id: str, text: str, *, channel: str | None = None,
                reply_to: str | None = None) -> dict:
        stamp = self._stamp()
        where = ({"channelIdentity": {"teamId": TEAM, "channelId": channel}} if channel
                 else {"chatId": CHAT})
        return {"id": message_id, "replyToId": reply_to, "etag": stamp, "messageType": "message",
                "createdDateTime": stamp, "lastModifiedDateTime": stamp, **where,
                "from": {"user": {"id": U_COS, "displayName": "Cos Agent"}},
                "body": {"contentType": "html", "content": f"<p>{text}</p>"},
                "attachments": [{"id": "att-1", "contentType": "reference", "name": "quote.pdf",
                                 "contentUrl": "https://petrasoap.sharepoint.com/q.pdf"}],
                "mentions": [{"id": 0, "mentionText": "Vince",
                              "mentioned": {"user": {"id": U_VINCE, "displayName": "Vince"}}}],
                "webUrl": "https://teams.microsoft.com/l/message/x"}

    def post_root(self, channel: str, message_id: str, text: str = SECRET_TEXT,
                  replies: int = 0) -> None:
        self.roots[channel][message_id] = self.message(message_id, text, channel=channel)
        self.changed[channel][message_id] = self.seq
        self.replies[(channel, message_id)] = [
            self.message(f"{message_id}{n}", f"reply {n}", channel=channel, reply_to=message_id)
            for n in range(1, replies + 1)]

    def post_chat(self, message_id: str, text: str = SECRET_TEXT) -> None:
        self.chat[message_id] = self.message(message_id, text)

    def _page(self, base: str, items: list, query: dict, *, end: dict) -> tuple:
        top = int(query.get("$top", ["50"])[0])
        start = int(query.get("$skiptoken", ["0"])[0])
        page = {"value": items[start:start + top]}
        if start + top < len(items):
            rest = {k: v[0] for k, v in query.items() if k != "$skiptoken"}
            rest["$skiptoken"] = str(start + top)
            page["@odata.nextLink"] = base + "?" + urlencode(rest)
        else:
            page.update(end)
        return j(200, page)

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": "eyJ.TOKEN-do-not-leak.sig", "expires_in": 3599})
        assert method == "GET", method
        path, query = unquote(parts.path), parse_qs(parts.query)
        base = f"https://graph.microsoft.com{parts.path}"
        if any(re.search(p, path) for p in self.forbidden):
            return j(403, {"error": {"code": "Forbidden"}})
        for pattern, status in self.failing.items():
            if re.search(pattern, path):
                return j(status, {"error": {"code": "serviceNotAvailable"}})
        if url in self.expired:
            return j(410, {"error": {"code": "resyncRequired"}})
        if path == "/v1.0/users":
            # The people guard's one directory listing (every user, one page).
            return j(200, {"value": [{"id": u, "userPrincipalName": a, "mail": a,
                                      "proxyAddresses": [f"SMTP:{a}"]}
                                     for a, u in sorted(self.users.items())]})
        found = re.fullmatch(r"/v1\.0/users/([^/]+)", path)
        if found:
            user = self.users.get(found[1].lower())
            return (j(200, {"id": user, "userPrincipalName": found[1].lower(),
                            "mail": found[1].lower()}) if user
                    else j(404, {"error": {"code": "Request_ResourceNotFound"}}))
        if re.fullmatch(r"/v1\.0/users/[^/]+/joinedTeams", path):
            return j(200, {"value": [{"id": TEAM}]})
        if path == f"/v1.0/users/{U_COS}/chats":
            return j(200, {"value": [{"id": CHAT, "chatType": "oneOnOne"}]})
        if re.fullmatch(r"/v1\.0/users/[^/]+/chats", path):
            return j(200, {"value": []})
        if path == "/v1.0/teams":
            return j(200, {"value": [{"id": team} for team in self.team_names]})
        if path == f"/v1.0/teams/{TEAM}":
            return j(200, {"id": TEAM, "displayName": self.team_names[TEAM]})
        found = re.fullmatch(rf"/v1\.0/teams/{TEAM}/channels/([^/]+)", path)
        if found and found[1] in self.channel_types:
            return j(200, {"id": found[1], "membershipType": self.channel_types[found[1]],
                           "displayName": self.channel_names.get(found[1], "Thread")})
        if path == f"/v1.0/chats/{CHAT}":
            return j(200, {"id": CHAT, "chatType": "oneOnOne", "tenantId": self.chat_tenant,
                           "topic": self.chat_topics.get(CHAT)})
        if path == "/v1.0/reports/getTeamsTeamActivityDetail(period='D30')":
            return j(200, {"value": self.activity})
        if path == f"/v1.0/teams/{TEAM}/channels":
            return j(200, {"value": [{"id": c, **({"membershipType": t} if t else {})}
                                     for c, t in self.channel_types.items()]})
        members = None
        if path == f"/v1.0/teams/{TEAM}/members":
            members = self.team_members
        if path == f"/v1.0/chats/{CHAT}/members":
            members = self.chat_members
        found = re.fullmatch(rf"/v1\.0/teams/{TEAM}/channels/([^/]+)/members", path)
        if found:
            members = self.channel_members[found[1]]
        if members is not None:
            return j(200, {"value": [
                {"userId": u, "email": e, "roles": [], "displayName": e.split("@")[0].title(),
                 **({} if self.history.get(u, FULL_HISTORY) is None else
                    {"visibleHistoryStartDateTime": self.history.get(u, FULL_HISTORY)})}
                for u, e in members]})
        found = re.fullmatch(rf"/v1\.0/teams/{TEAM}/channels/([^/]+)/messages", path)
        if found:   # the plain listing: newest roots first
            items = sorted(self.roots[found[1]].values(),
                           key=lambda m: m["lastModifiedDateTime"], reverse=True)
            return self._page(base, items, query, end={})
        found = re.fullmatch(rf"/v1\.0/teams/{TEAM}/channels/([^/]+)/messages/delta", path)
        if found and found[1] in self.refused_empty_delta and not self.roots[found[1]]:
            if "$skiptoken" in query:
                return j(400, {"error": {"code": "BadRequest"}})
            return j(200, {"value": [], "@odata.nextLink": f"{base}?$skiptoken=empty"})
        if found:
            channel = found[1]
            since = int(query.get("$deltatoken", ["0"])[0])
            changed = sorted(m for m, seq in self.changed[channel].items() if seq > since)
            items = [self.roots[channel][m] for m in changed]
            return self._page(base, items, query,
                              end={"@odata.deltaLink": f"{base}?$deltatoken={self.seq}"})
        found = re.fullmatch(rf"/v1\.0/teams/{TEAM}/channels/([^/]+)/messages/(\d+)/replies",
                             path)
        if found:
            return self._page(base, self.replies.get((found[1], found[2]), []), query, end={})
        if path == f"/v1.0/chats/{CHAT}/messages":
            items = sorted(self.chat.values(), key=lambda m: m["lastModifiedDateTime"],
                           reverse=True)
            wanted = query.get("$filter", [""])[0]
            if wanted:
                since = wanted.split(" gt ", 1)[1]
                items = [m for m in items if m["lastModifiedDateTime"] > since]
            return self._page(base, items, query, end={})
        return j(404, {"error": {"code": "NotFound"}})

    def gets(self) -> list[str]:
        return [url for method, url in self.calls if method == "GET"]


def audience() -> LiveSourceAudience:
    return LiveSourceAudience(
        {"teams:*": ["svc:teams-capture", "staff:teams-member", "staff:teams-outsider",
                     "staff:cos", "staff:vince"]},
        tenant="plx", classifications={"teams:*": "restricted"},
        identities={"staff:teams-member": [COS, VINCE], "staff:cos": [U_COS],
                    "staff:vince": [U_VINCE],
                    "staff:teams-outsider": ["outsider@petrasoap.com"]})


def scope(allowlist: tuple[str, ...] = (COS, VINCE), **caps) -> TeamsScope:
    return TeamsScope({"schema": capture_teams.SCOPE_SCHEMA,
                       "members": [{"address": COS, "joined_teams": True}],
                       "member_chats": "mail_allowlist", **caps},
                      allowlist=MailScope(allowlist))


def confirm(state_home: str | dict, channels: list[str], chats: list[str] = (CHAT,)) -> Path:
    """Write the human content confirmation (as a person would, beside the census) into
    the injected check state of ``state_home`` ($XDG_STATE_HOME, or the whole environment):
    ``channels`` of TEAM and ``chats``. The directories it makes are 0700, as the CLI's."""
    env = state_home if isinstance(state_home, dict) else {**ENV, "XDG_STATE_HOME": state_home}
    path = capture_teams.confirmation_path(capture_teams.default_state_dir(env, injected=True))
    for parent in reversed(path.parents):
        if not parent.exists():
            parent.mkdir(mode=0o700)
    path.write_text(json.dumps({
        "schema": capture_teams.CONFIRMATION_SCHEMA, "confirmed_by": "owner",
        "confirmed_at": "2026-10-21",
        "channels": [{"team_id": TEAM, "channel_id": c} for c in channels],
        "chats": list(chats)}), encoding="utf-8")
    return path


def chat_listings(graph: FakeTeamsGraph) -> set[str]:
    """The user ids whose chats were listed."""
    return {m[1] for u in graph.gets()
            if (m := re.fullmatch(r"/v1\.0/users/([^/]+)/chats", unquote(urlsplit(u).path)))}


class CaptureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.graph = FakeTeamsGraph()
        self.graph.post_root(GENERAL, "1001", replies=2)
        self.graph.post_root(GENERAL, "1002")
        self.graph.post_root(PRIVATE, "2001", "Board-only pricing")
        self.graph.post_chat("3001")
        self.graph.post_chat("3002", "second")
        self.audience = audience()
        self.store = open_store(self.tmp.name, self.audience)
        api, self.transport = graph_read.build_graph(ENV, sender=self.graph)
        self.capture = TeamsCapture(self.store, api, self.audience, CAPTURE)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def unit(self, thread: str) -> tuple[str, str]:
        if thread == CHAT:
            return "teams_chat", Conversation("chat", CHAT).unit
        return "teams_channel", Conversation("channel", thread, TEAM).unit

    def visible(self, actor: Actor, thread: str) -> list[dict]:
        """The messages of a conversation the actor sees (not its own record)."""
        return [r for r in self.store.records(actor, *self.unit(thread))["items"]
                if f"/{capture_teams.CONVERSATION}@" not in r["citation"]]

    def conversation(self, actor: Actor, thread: str) -> list[dict]:
        """The conversation's own record as the actor sees it (none, or one)."""
        return [r for r in self.store.records(actor, *self.unit(thread))["items"]
                if f"/{capture_teams.CONVERSATION}@" in r["citation"]]

    def by_kind(self, run: dict) -> dict[str, dict]:
        return {f"{c['kind']}:{c['membership']}": c for c in run["conversations"]}


class GetOnlyTests(CaptureCase):
    def test_every_request_is_a_grammar_get_and_writes_are_refused(self):
        run = self.capture.run(scope())
        self.assertTrue(run["complete"], run)
        methods = {m for m, _u in self.graph.calls}
        self.assertEqual(methods, {"GET", "POST"})
        self.assertTrue(all(urlsplit(u).hostname == "login.microsoftonline.com"
                            for m, u in self.graph.calls if m == "POST"))
        ops = {grammar.match("graph.microsoft.com", urlsplit(u).path) for u in self.graph.gets()}
        self.assertNotIn(None, ops)
        self.assertLessEqual({"graph.channel_messages_delta", "graph.channel_message_replies",
                              "graph.chat_messages", "graph.team_members",
                              "graph.channel_members", "graph.chat_members"}, ops)
        messages = Conversation("channel", GENERAL, TEAM).url("graph.channel_messages_delta")
        for method in ("POST", "PATCH", "DELETE"):
            with self.assertRaises(ReadOnlyGuardError):
                check_request(method, messages.replace("/delta", ""), environ={})
        for path in (f"/v1.0/teams/{TEAM}/channels/{GENERAL}/messages/1001/replies/post",
                     f"/v1.0/chats/{CHAT}/sendActivityNotification"):
            with self.assertRaises(ReadOnlyGuardError, msg=path):
                check_request("GET", "https://graph.microsoft.com" + path, environ={})


class ChatScopeTests(CaptureCase):
    def test_chats_are_read_for_exactly_the_allowlisted_accounts(self):
        # The shipped scopes name no chat account: they derive them from the allowlist.
        builtin = MailScope.builtin()
        for path in (capture_teams.DEFAULT_SCOPE,
                     ROOT / "examples" / "teams_capture_scope.live-check.json"):
            chats = {a for a, _joined, with_chats in TeamsScope.from_file(path).members
                     if with_chats}
            self.assertEqual(chats, set(builtin.addresses), path)
            self.assertFalse(chats & builtin.excluded, path)
        # The same scope document follows the allowlist it is given, on the wire too.
        self.capture.run(scope())
        self.assertEqual(chat_listings(self.graph), {U_COS, U_VINCE})
        self.graph.calls.clear()
        self.capture.run(scope(allowlist=(COS,)))
        self.assertEqual(chat_listings(self.graph), {U_COS})
        # Channels and chats share the conversation cap in turn.
        run = self.capture.run(scope(max_conversations=2))
        self.assertEqual(sorted(c["kind"] for c in run["conversations"]), ["channel", "chat"])
        self.assertIn("cap:conversation_cap", run["discovery"]["reasons"])

    def test_an_unknown_or_unbounded_chat_scope_fails_closed(self):
        base = {"schema": capture_teams.SCOPE_SCHEMA, "members": [{"address": COS}]}
        for document, allowlist, code in (
                ({**base, "member_chats": "everyone"}, MailScope([COS]),
                 "scope_member_chats_invalid"),
                ({**base, "members": [{"address": COS, "chats": True}]}, MailScope([COS]),
                 "scope_member_invalid"),          # no second, hand-kept list of chat accounts
                ({**base, "member_chats": "mail_allowlist"}, MailScope.unrestricted(),
                 "scope_member_chats_unbounded")):
            with self.subTest(code), self.assertRaises(capture_teams.TeamsCaptureError) as err:
                TeamsScope(document, allowlist=allowlist)
            self.assertEqual(err.exception.code, code)


class RotationTests(CaptureCase):
    """Over ``max_conversations`` the runs rotate on a stored cursor: every conversation
    found is read, discovery is complete once a pass has read them all."""

    def channels(self, threads: list[str]) -> None:
        self.graph.channel_types = dict.fromkeys(threads, "standard")
        for thread in threads:
            self.graph.roots.setdefault(thread, {})
            self.graph.changed.setdefault(thread, {})

    def listed(self) -> list[str]:
        """The conversations whose messages the last run listed."""
        return [m[1] for u in self.graph.gets() if (m := re.fullmatch(
            r"/v1\.0/(?:teams/[^/]+/channels|chats)/([^/]+)/messages(?:/delta)?",
            unquote(urlsplit(u).path)))]

    def run_until_complete(self, cap: int, total: int, change=None) -> list[list[str]]:
        reads: list[list[str]] = []
        for _ in range(total + 2):
            self.graph.calls.clear()
            run = self.capture.run(scope(max_conversations=cap))
            reads.append(self.listed())
            found = sorted(self.graph.channel_types) + [CHAT]
            discovery = run["discovery"]
            self.assertEqual(discovery["discovered_total"], len(found))
            # Only the conversations due are read, each once.
            self.assertEqual(len(reads[-1]), min(cap, len(found)))
            self.assertEqual(len(set(reads[-1])), len(reads[-1]))
            self.assertEqual([self.unit(t)[1] for t in reads[-1]],
                             [c["unit"] for c in run["conversations"]])
            unread = set(found) - {t for r in reads for t in r}
            self.assertEqual(discovery["unvisited"], len(unread))
            self.assertEqual(discovery["status"], "incomplete" if unread else "complete")
            self.assertEqual(run["complete"], not unread)
            if not unread:
                return reads
            if change:
                change(len(reads))
        self.fail(f"discovery never complete in {len(reads)} runs")

    def test_repeated_capped_runs_read_every_conversation_at_any_cardinality(self):
        for total in (1, 10, 60):
            with self.subTest(total=total):
                self.tearDown()
                self.setUp()
                self.channels([f"19:rot{n:03d}@thread.tacv2" for n in range(total - 1)])
                reads = self.run_until_complete(4, total)
                channels = {t for r in reads for t in r if t != CHAT}
                self.assertEqual(channels, set(self.graph.channel_types))
                # Channels and chats in turn: the one chat takes a place in every run.
                self.assertEqual(len(reads), 1 if total <= 4 else -(-(total - 1) // 3))
                # The next pass starts where this one stopped, never over the same window.
                self.graph.calls.clear()
                run = self.capture.run(scope(max_conversations=4))
                if total > 4:
                    self.assertIn("cap:conversation_cap", run["discovery"]["reasons"])
                    self.assertNotEqual(self.listed(), reads[-1])

    def test_rotation_keys_on_ids_so_added_or_removed_conversations_skip_nothing(self):
        threads = [f"19:rot{n:03d}@thread.tacv2" for n in range(1, 10)]
        self.channels(threads)
        # An unreadable stored cursor starts the rotation over: nothing counts as read.
        self.store.set_state(capture_teams.ROTATION, "{not json")
        added, removed = "19:rot000@thread.tacv2", "19:rot009@thread.tacv2"

        def change(runs: int) -> None:
            if runs == 1:   # behind the cursor, and one not read yet
                self.channels([added] + [t for t in threads if t != removed])
        reads = self.run_until_complete(4, len(threads) + 2, change)
        # Every channel listed is read once in the pass, the one added behind the cursor
        # too; the one removed before its turn is not waited for.
        channels = [t for r in reads for t in r if t != CHAT]
        self.assertEqual(sorted(channels), sorted(self.graph.channel_types))
        self.assertIn(added, channels)
        self.assertEqual(len(reads), 3)

    def test_an_old_layout_rotation_restarts_the_pass_and_never_reports_it_complete(self):
        threads = [f"19:rot{n:03d}@thread.tacv2" for n in range(1, 10)]
        self.channels(threads)
        # Unversioned (old layout): it claims every conversation visited. Read as such,
        # the first run would report the pass complete after reading only 4 of 10.
        every = [self.unit(t)[1] for t in [*threads, CHAT]]
        self.store.set_state(capture_teams.ROTATION, json.dumps(
            {"channel": every[2], "next": "channel", "visited": every}))
        reads = self.run_until_complete(4, len(every))   # nothing counts as visited
        self.assertEqual(len(reads), 3)
        self.assertEqual({t for r in reads for t in r}, {*threads, CHAT})

    def test_a_conversation_cut_short_keeps_the_pass_open_until_its_read_completes(self):
        threads = [f"19:rot{n:03d}@thread.tacv2" for n in range(8)]
        self.channels(threads)
        cut = threads[5]
        self.graph.post_root(cut, "5001", replies=2)
        cut_unit = self.unit(cut)[1]

        def runs(count: int, **caps) -> list[tuple[bool, int, dict | None]]:
            out = []
            for _ in range(count):
                run = self.capture.run(scope(max_conversations=4, **caps))
                read = {c["unit"]: c for c in run["conversations"]}.get(cut_unit)
                discovery = run["discovery"]
                self.assertEqual(run["complete"], discovery["status"] == "complete")
                self.assertEqual("cap:conversation_cap" in discovery["reasons"],
                                 discovery["unvisited"] > 0)
                out.append((run["complete"], discovery["unvisited"], read))
            return out

        # Run 2 cuts the channel (reply cap); run 3 reads the rest of the pass but the cut
        # channel is still incomplete, so no run reports the pass complete.
        capped = runs(5, max_replies_per_message=1)
        self.assertEqual([read is not None for _, _, read in capped],
                         [False, True, False, False, True])
        self.assertEqual(capped[1][2]["reasons"], ["cap:reply_cap"])
        self.assertEqual([(done, left) for done, left, _ in capped[2:]],
                         [(False, 1), (False, 1), (False, 1)])
        # Once the cap fits, the pass completes in the run that reads the channel complete.
        fits = runs(3)
        self.assertEqual([(done, left, read is not None) for done, left, read in fits],
                         [(False, 1, False), (False, 1, False), (True, 0, True)])
        self.assertEqual(fits[-1][2]["status"], "complete")
        self.assertEqual(len(self.visible(MEMBER, cut)), 3)   # the root and both replies


class PagingTests(CaptureCase):
    def test_caps_report_incomplete_and_the_next_runs_resume_then_read_deltas(self):
        tight = scope(page_size=1, max_pages_per_run=1)
        first = self.by_kind(self.capture.run(tight))
        general = first["channel:standard"]
        self.assertEqual((general["status"], general["reasons"], general["round"]),
                         ("incomplete", ["cap:page_cap"], "full"))
        self.assertEqual(general["counts"]["pages"], 1)
        runs = [self.capture.run(tight) for _ in range(3)]
        self.assertEqual(self.by_kind(runs[0])["channel:standard"]["resumed_from"], "cursor")
        last = self.by_kind(runs[-1])
        self.assertTrue(all(c["status"] == "complete" for c in last.values()), last)
        self.assertEqual(len(self.visible(MEMBER, GENERAL)), 4)   # 2 roots + 2 replies
        self.assertEqual(len(self.visible(MEMBER, CHAT)), 2)
        full_chat = next(u for u in self.graph.gets() if f"/chats/{CHAT}/messages" in unquote(u))
        self.assertIn("$orderby=lastModifiedDateTime desc", unquote(full_chat).replace("+", " "))
        # New messages: the channel reads only the change since its delta link and the
        # chat only messages modified since its high-water.
        self.graph.post_root(GENERAL, "1003")
        self.graph.post_chat("3003")
        delta = self.by_kind(self.capture.run(scope()))
        for key in ("channel:standard", "chat:oneOnOne"):
            self.assertEqual((delta[key]["status"], delta[key]["round"],
                              delta[key]["counts"]["versions_new"]),
                             ("complete", "incremental", 1), key)
        self.assertEqual(delta["channel:standard"]["counts"]["messages_read"], 1)
        # The chat re-reads its overlap (the newest captured message), not its history.
        self.assertEqual(delta["chat:oneOnOne"]["counts"]["messages_read"], 2)

    def test_a_capped_full_round_whose_cursor_expired_restarts_in_full(self):
        tight = scope(page_size=1, max_pages_per_run=1)
        self.capture.run(tight)
        cursor = self.store.run(*self.unit(GENERAL))["cursor"]
        self.assertIn("skiptoken", cursor)
        self.graph.expired.add(cursor)
        general = self.by_kind(self.capture.run(scope()))["channel:standard"]
        self.assertEqual((general["status"], general["round"],
                          general["counts"]["round_restarts"]), ("complete", "full", 1), general)
        self.assertEqual(len(self.visible(MEMBER, GENERAL)), 4)

    def test_a_reply_cap_keeps_the_channel_incomplete_until_the_cap_fits(self):
        run = self.by_kind(self.capture.run(scope(max_replies_per_message=1)))
        self.assertEqual(run["channel:standard"]["reasons"], ["cap:reply_cap"])
        again = self.by_kind(self.capture.run(scope(max_replies_per_message=1)))
        self.assertEqual(again["channel:standard"]["status"], "incomplete")
        fits = self.by_kind(self.capture.run(scope()))
        self.assertEqual(fits["channel:standard"]["status"], "complete")
        self.assertEqual(len(self.visible(MEMBER, GENERAL)), 4)


class AudienceTests(CaptureCase):
    def test_a_reader_outside_the_channel_sees_and_resolves_nothing(self):
        self.capture.run(scope())
        rows = self.visible(MEMBER, GENERAL)
        root = next(r for r in rows if r["key"]["id"] == "1001")
        self.assertEqual(root["row"]["body_text"], SECRET_TEXT)
        self.assertEqual(root["row"]["attachments"][0]["captured"], "reference_only")
        self.assertEqual(root["row"]["mentions"][0]["target_id"], U_VINCE)
        self.assertEqual(root["lineage"], "mail")
        private = self.visible(MEMBER, PRIVATE)          # vince is in the private channel
        self.assertEqual(len(private), 1)
        # cos is a team member but not in the private channel: granted the resource,
        # outside its membership -> no rows, no resolve, no complete view.
        self.assertEqual(self.visible(COS_ONLY, PRIVATE), [])
        self.assertEqual(len(self.visible(COS_ONLY, GENERAL)), 4)
        self.assertEqual(self.store.resolve(private[0]["citation"], COS_ONLY)["error"],
                         "not_visible")
        for thread in (GENERAL, PRIVATE, CHAT):
            self.assertEqual(self.visible(OUTSIDER, thread), [])
        self.assertFalse(self.store.completeness(OUTSIDER, *self.unit(GENERAL))["complete"])
        # The grant map alone never widens a conversation: no source_acl_required needed.
        self.assertTrue(self.audience.source_acl_required("teams:chat:" + CHAT))

    def test_a_chat_is_trimmed_to_its_members_though_every_allowlisted_account_is_read(self):
        self.capture.run(scope())
        self.assertIn(U_VINCE, chat_listings(self.graph))
        # cos's chat with a guest outside the allowlist: cos sees it, vince (allowlisted,
        # granted every chat, his own chats read) is not a member and sees nothing.
        rows = self.visible(COS_ONLY, CHAT)
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.visible(VINCE_ONLY, CHAT), [])
        self.assertEqual(self.store.resolve(rows[0]["citation"], VINCE_ONLY)["error"],
                         "not_visible")
        self.assertIsNone(self.store.claim_rows(VINCE_ONLY)(rows[0]["citation"]))

    def test_a_chat_member_sees_no_message_from_before_his_visible_history(self):
        # Vince is added to the chat after its messages (3001 at 01:00:00.000, 3002 at
        # 01:10): his current visibleHistoryStartDateTime decides which he may read.
        self.graph.chat_members.append((U_VINCE, VINCE))
        cases = (("2030-01-01T00:00:00Z", set()),   # added without history
                 (None, set()),                       # start not reported: fails closed
                 ("2026-10-01T01:05:00Z", {"3002"}),  # history from between the two
                 (FULL_HISTORY, {"3001", "3002"}),    # the whole history
                 ("2026-10-01T01:00:00.000Z", {"3001", "3002"}),  # from 3001's instant on
                 ("2026-10-01T01:00:00.5Z", {"3002"}),            # same second, after 3001
                 ("2026-10-01T01:00:00.0000001Z", {"3002"}),      # 100 ns after it
                 ("2026-10-01T01:00:00.5xZ", set()),   # unreadable start: fails closed
                 ("2030-01-01T00:00:00Z", set()))     # re-added without history
        minted: set[str] = set()
        known = None
        for start, seen in cases:
            self.graph.history[U_VINCE] = start
            self.capture.run(scope())
            rows = {r["key"]["id"]: r["citation"] for r in self.visible(COS_ONLY, CHAT)}
            minted.update(rows.values())
            known = self.store.now() if start == FULL_HISTORY else known
            self.assertEqual(set(rows), {"3001", "3002"}, start)
            self.assertEqual({r["key"]["id"] for r in self.visible(VINCE_ONLY, CHAT)}, seen,
                             start)
            for message, handle in rows.items():
                self.assertEqual(self.store.resolve(handle, VINCE_ONLY)["ok"],
                                 message in seen, (start, message))
                self.assertEqual(self.store.claim_rows(VINCE_ONLY)(handle) is not None,
                                 message in seen, (start, message))
        # Re-added without history, he reads none of it through any handle minted while
        # his history covered it, nor in a past view: his current start decides.
        self.assertGreater(len(minted), 2)
        for handle in minted:
            self.assertEqual(self.store.resolve(handle, VINCE_ONLY)["error"], "not_visible")
            self.assertIsNone(self.store.claim_rows(VINCE_ONLY)(handle))
            self.assertTrue(self.store.resolve(handle, COS_ONLY)["ok"])
        past = {a: self.store.records(a, *self.unit(CHAT), as_known_at=known)["items"]
                for a in (VINCE_ONLY, COS_ONLY)}
        # Nor the chat's own record (its latest message is outside his history).
        self.assertEqual((len(past[VINCE_ONLY]), len(past[COS_ONLY])), (0, 3))
        self.assertEqual(sum(f"/{capture_teams.CONVERSATION}@" in r["citation"]
                             for r in past[COS_ONLY]), 1)

    def gap(self, relation: str) -> str | None:
        with TeamsCaptures(self.store.path) as captured:
            return captured.capture_gap(relation)

    def test_a_chat_whose_members_read_is_refused_is_excluded_and_never_complete(self):
        # A definitive answer (403 refused, 404 not a member any more or gone) drops the
        # membership read before; a transient one keeps it (TransientMembersReadTests).
        self.capture.run(scope())
        handles = [r["citation"] for r in self.visible(COS_ONLY, CHAT)]
        self.assertEqual(len(handles), 2)
        for status, state in ((404, "incomplete"), (403, "blocked")):
            with self.subTest(status):
                self.graph.failing = {rf"/chats/{re.escape(CHAT)}/members": status}
                run = self.capture.run(scope())
                chat = self.by_kind(run)["chat:oneOnOne"]
                self.assertEqual(chat["status"], state)
                self.assertIn("members_unread", chat["reasons"])
                self.assertFalse(run["complete"])
                self.assertFalse(self.store.operator_completeness(
                    CAPTURE, *self.unit(CHAT))["complete"])
                # The membership read before is not kept: no stale member reads the chat
                # (the only chat: nothing of its relation is projected).
                self.assertEqual(self.visible(COS_ONLY, CHAT), [])
                for handle in handles:
                    self.assertEqual(self.store.resolve(handle, COS_ONLY)["error"],
                                     "not_visible")
                self.assertEqual(self.gap("chatMessages"), "source_not_captured")
        self.graph.failing = {}
        chat = self.by_kind(self.capture.run(scope()))["chat:oneOnOne"]
        self.assertEqual((chat["status"], chat["round"]), ("complete", "full"))
        self.assertEqual(len(self.visible(COS_ONLY, CHAT)), 2)
        self.assertIsNone(self.gap("chatMessages"))

    def test_a_chat_hosted_in_another_tenant_is_not_readable_never_a_blocked_part(self):
        """Live 2026-10-27: Graph refuses (403) the messages of exactly the chats hosted
        in another tenant, though the app holds Chat.Read.All. Such a chat is settled as
        ``not_readable:other_tenant`` (nothing claimed captured, no blocked part); a 403
        on a chat of this tenant, or with the home tenant unknown, stays blocked."""
        api, _transport = graph_read.build_graph(ENV, sender=self.graph)
        home = TeamsCapture(self.store, api, self.audience, CAPTURE,
                            home_tenant=ENV["MICROSOFT_GRAPH_TENANT_ID"].upper())
        self.graph.forbidden = [rf"/chats/{re.escape(CHAT)}/messages$"]
        self.graph.chat_tenant = "99999999-8888-7777-6666-555555555555"
        run = home.run(scope())
        chat = self.by_kind(run)["chat:oneOnOne"]
        self.assertEqual((chat["status"], chat["reasons"]),
                         ("not_readable", ["not_readable:other_tenant"]))
        self.assertEqual(run["blocked_upstream"], [])
        self.assertTrue(run["complete"], run)   # the channels read, the chat settled
        self.assertEqual(self.visible(COS_ONLY, CHAT), [])   # nothing claimed captured
        self.assertFalse(self.store.operator_completeness(
            CAPTURE, *self.unit(CHAT))["complete"])
        # Fail closed: the same refusal of a chat this tenant hosts, or with no home
        # tenant known, or with the chat's own record unread, is a blocked part.
        for case, capture, tenant, record in (
                ("hosted here", home, ENV["MICROSOFT_GRAPH_TENANT_ID"], True),
                ("home unknown", self.capture, "99999999-8888-7777-6666-555555555555", True),
                ("record unread", home, "99999999-8888-7777-6666-555555555555", False)):
            with self.subTest(case):
                self.graph.chat_tenant = tenant
                self.graph.failing = {} if record else {rf"/chats/{re.escape(CHAT)}$": 503}
                run = capture.run(scope())
                chat = self.by_kind(run)["chat:oneOnOne"]
                self.assertEqual((chat["status"], chat["reasons"]),
                                 ("blocked", ["blocked:missing_permission:Chat.Read.All"]))
                self.assertFalse(run["complete"])
                self.assertEqual({b["part"] for b in run["blocked_upstream"]},
                                 {"teams.chat_messages"})
        self.graph.failing = {}

    def test_an_empty_channel_whose_delta_link_graph_refuses_is_read_complete(self):
        """Live 2026-10-27: a channel with no message answers its delta with an empty
        page and a nextLink Graph refuses (HTTP 400). One page of its plain listing
        ($top=1) proves it empty: the round ends complete; a channel that does hold a
        message keeps the 400 as a source error."""
        empty = "19:empty0003@thread.tacv2"
        self.graph.channel_types[empty] = "standard"
        self.graph.roots[empty], self.graph.changed[empty] = {}, {}
        self.graph.refused_empty_delta.add(empty)
        for _ in range(2):   # and again: no delta link kept, a full round each run
            run = self.capture.run(scope())
            channel = next(c for c in run["conversations"]
                           if c["unit"] == Conversation("channel", empty, TEAM).unit)
            self.assertEqual((channel["status"], channel["reasons"], channel["round"]),
                             ("complete", [], "full"))
            self.assertTrue(run["complete"], run)
        listings = [u for u in self.graph.gets()
                    if grammar.match("graph.microsoft.com", urlsplit(u).path)
                    == "graph.channel_messages"]
        self.assertEqual(len(listings), 2)
        self.assertTrue(all(parse_qs(urlsplit(u).query) == {"$top": ["1"]} for u in listings))
        # A refused delta link of a channel the listing shows a message in: not empty.
        self.graph.roots[empty]["9001"] = self.graph.message("9001", "x", channel=empty)
        self.graph.refused_empty_delta.discard(empty)
        delta = Conversation("channel", empty, TEAM).url("graph.channel_messages_delta")
        self.graph.failing = {re.escape(urlsplit(delta).path) + "$": 400}
        run = self.capture.run(scope())
        channel = next(c for c in run["conversations"]
                       if c["unit"] == Conversation("channel", empty, TEAM).unit)
        self.assertEqual(channel["status"], "incomplete")
        self.assertTrue(any(r.startswith("source_error:") for r in channel["reasons"]))
        self.assertFalse(run["complete"])
        self.graph.failing = {}

    def test_a_channel_whose_members_read_is_refused_loses_its_membership(self):
        private_members = rf"/channels/{re.escape(PRIVATE)}/members"
        # Never read: the channel beside a settled one keeps its relation incomplete.
        self.graph.failing = {private_members: 503}
        run = self.by_kind(self.capture.run(scope()))
        self.assertEqual(run["channel:standard"]["status"], "complete")
        self.assertEqual(run["channel:private"]["status"], "incomplete")
        self.assertEqual(self.gap("channelMessages"), "source_capture_incomplete")
        self.graph.failing = {}
        self.capture.run(scope())
        handles = [r["citation"] for r in self.visible(VINCE_ONLY, PRIVATE)]
        self.assertEqual(len(handles), 1)
        # Read before, then gone (404): the former member loses it through the item gate.
        self.graph.failing = {private_members: 404}
        run = self.capture.run(scope())
        private = self.by_kind(run)["channel:private"]
        self.assertEqual((private["status"], private["reasons"][0]),
                         ("incomplete", "members_unread"))
        self.assertFalse(run["complete"])
        self.assertEqual(self.visible(VINCE_ONLY, PRIVATE), [])
        for handle in handles:
            self.assertEqual(self.store.resolve(handle, VINCE_ONLY)["error"], "not_visible")
            self.assertIsNone(self.store.claim_rows(VINCE_ONLY)(handle))
        self.assertEqual(self.gap("channelMessages"), "source_capture_incomplete")

    def test_a_capped_members_read_keeps_its_cap_reason_and_its_membership(self):
        run = self.by_kind(self.capture.run(scope(max_members=1)))
        for key in ("channel:standard", "chat:oneOnOne"):
            self.assertEqual((run[key]["status"], run[key]["reasons"]),
                             ("incomplete", ["cap:member_item_cap"]), key)
        self.assertEqual(len(self.visible(COS_ONLY, GENERAL)), 4)
        # A chat is cleared by the people-exclusion guard only on a whole member list.
        with self.assertRaises(capture_teams.CaptureError):   # never read, no membership
            self.visible(COS_ONLY, CHAT)

    def test_a_member_removed_from_the_team_stops_seeing_its_messages(self):
        self.capture.run(scope())
        self.assertEqual(len(self.visible(COS_ONLY, GENERAL)), 4)
        before = self.store.now()
        cited = [r["citation"] for r in self.visible(COS_ONLY, GENERAL)]
        self.graph.team_members = [(U_VINCE, VINCE)]
        run = self.by_kind(self.capture.run(scope()))
        self.assertEqual(run["channel:standard"]["counts"]["acl_refreshed"], 4)
        self.assertEqual(self.visible(COS_ONLY, GENERAL), [])
        self.assertEqual(len(self.visible(MEMBER, GENERAL)), 4)
        # History goes too: handles minted before the removal and past views.
        for handle in cited:
            self.assertEqual(self.store.resolve(handle, COS_ONLY)["error"], "not_visible")
            self.assertIsNone(self.store.claim_rows(COS_ONLY)(handle))
            self.assertTrue(self.store.resolve(handle, MEMBER)["ok"])
        self.assertEqual(self.store.records(COS_ONLY, *self.unit(GENERAL),
                                            as_known_at=before)["items"], [])

    def test_a_channel_of_unknown_membership_type_is_gated_by_its_own_members(self):
        self.graph.channel_types[PRIVATE] = None
        self.capture.run(scope())
        self.assertEqual(self.visible(COS_ONLY, PRIVATE), [])
        self.assertEqual(len(self.visible(MEMBER, PRIVATE)), 1)


class TransientMembersReadTests(CaptureCase):
    """A members read that fails transiently (429 after the bounded Retry-After retries,
    5xx, a timeout, a request deadline) keeps the membership recorded before and its
    recorded_at, leaves the run open (members_unread) and is read again next run; a
    definitive answer (403/404, an excluded person, a cut list, a changed membership)
    still drops or replaces it. A kept membership is served only within the store's
    capture-age bound (``capture_store.UNIT_ACL_MAX_AGE``)."""

    CHAT_MEMBERS = rf"/chats/{re.escape(CHAT)}/members"

    def recorded(self, thread: str) -> tuple | None:
        kind, unit = self.unit(thread)
        conn = sqlite3.connect(f"file:{self.store.path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT acl, recorded_at FROM unit_acl WHERE kind = ? "
                                "AND unit = ?", (kind, unit)).fetchone()
        finally:
            conn.close()

    def capture_with(self, sender, **kwargs) -> TeamsCapture:
        api, _transport = graph_read.build_graph(ENV, sender=sender, **kwargs)
        return TeamsCapture(self.store, api, self.audience, CAPTURE)

    def assert_kept(self, chat: dict, before: tuple, handles: list[str]) -> None:
        self.assertEqual(chat["status"], "incomplete")
        self.assertIn("members_unread", chat["reasons"])
        self.assertEqual(self.recorded(CHAT), before)       # the ACL and its recorded_at
        self.assertEqual(len(self.visible(COS_ONLY, CHAT)), 2)
        for handle in handles:
            self.assertTrue(self.store.resolve(handle, COS_ONLY)["ok"])
            self.assertEqual(self.store.resolve(handle, VINCE_ONLY)["error"], "not_visible")
        self.assertEqual(self.visible(VINCE_ONLY, CHAT), [])
        self.assertFalse(self.store.operator_completeness(
            CAPTURE, *self.unit(CHAT))["complete"])
        with TeamsCaptures(self.store.path) as captured:   # projected, never complete
            self.assertEqual(captured.capture_gap("chatMessages"), "source_capture_incomplete")

    def test_a_transient_members_failure_keeps_the_membership_and_the_run_open(self):
        self.capture.run(scope())
        before = self.recorded(CHAT)
        handles = [r["citation"] for r in self.visible(COS_ONLY, CHAT)]
        self.assertEqual(len(handles), 2)

        def timing_out(method, url, *rest):
            if re.search(self.CHAT_MEMBERS, unquote(urlsplit(url).path)):
                raise TimeoutError("timed out")
            return self.graph(method, url, *rest)

        for case in ("429", "500", "502", "503", "504", "timeout"):
            with self.subTest(case):
                self.graph.failing = ({} if case == "timeout"
                                      else {self.CHAT_MEMBERS: int(case)})
                capture = self.capture_with(timing_out) if case == "timeout" else self.capture
                run = capture.run(scope())
                chat = self.by_kind(run)["chat:oneOnOne"]
                self.assert_kept(chat, before, handles)
                self.assertFalse(run["complete"])
                self.assertEqual(run["blocked_upstream"], [])
                # Read again next run: its run left open, unvisited by the pass.
                self.assertEqual(self.store.run(*self.unit(CHAT))["state"], "in_progress")
        # A request deadline that refuses the read before any I/O is transient too.
        self.graph.failing = {}
        deadline = RequestDeadline(0)
        with deadline.active():
            chat = self.capture.capture(Conversation("chat", CHAT, membership="oneOnOne"),
                                        scope(), [])
        self.assertEqual(deadline.refused, 1)
        self.assert_kept(chat, before, handles)
        # The next successful read records it again and completes the chat.
        chat = self.by_kind(self.capture.run(scope()))["chat:oneOnOne"]
        self.assertEqual(chat["status"], "complete")
        self.assertGreater(self.recorded(CHAT)[1], before[1])

    def test_a_throttled_members_read_is_retried_after_retry_after_within_the_budget(self):
        self.capture.run(scope())
        before = self.recorded(CHAT)
        throttled = {"left": 1}
        slept: list[float] = []

        def throttling(method, url, *rest):
            if re.search(self.CHAT_MEMBERS, unquote(urlsplit(url).path)) and throttled["left"]:
                throttled["left"] -= 1
                return 429, {"Retry-After": "3"}, b'{"error": {"code": "TooManyRequests"}}'
            return self.graph(method, url, *rest)

        retry = BoundedBackoff(sleep=slept.append, jitter=lambda: 0.0, budget_seconds=60)
        chat = self.by_kind(self.capture_with(throttling, retry=retry).run(scope()))[
            "chat:oneOnOne"]
        self.assertEqual(chat["status"], "complete")
        self.assertEqual(slept, [3.0])                   # the source's own wait, honoured
        self.assertGreater(self.recorded(CHAT)[1], before[1])
        # Throttled past every retry: transient, the membership kept, the run open.
        throttled["left"], slept[:] = 99, []
        before = self.recorded(CHAT)
        chat = self.by_kind(self.capture_with(throttling, retry=BoundedBackoff(
            sleep=slept.append, jitter=lambda: 0.0, budget_seconds=60)).run(scope()))[
            "chat:oneOnOne"]
        self.assertEqual(len(slept), 2)                  # MAX_RETRIES, then given up
        self.assertEqual((chat["status"], self.recorded(CHAT)), ("incomplete", before))
        self.assertIn("members_unread", chat["reasons"])

    def test_definitive_answers_still_drop_or_replace_the_membership(self):
        self.capture.run(scope())
        handles = [r["citation"] for r in self.visible(COS_ONLY, CHAT)]
        for status in (403, 404, 400, 401):
            with self.subTest(status):
                self.graph.failing = {self.CHAT_MEMBERS: status}
                chat = self.by_kind(self.capture.run(scope()))["chat:oneOnOne"]
                self.assertIn("members_unread", chat["reasons"])
                self.assertIsNone(self.recorded(CHAT))
                for handle in handles:
                    self.assertEqual(self.store.resolve(handle, COS_ONLY)["error"],
                                     "not_visible")
                self.graph.failing = {}
                self.capture.run(scope())
                self.assertIsNotNone(self.recorded(CHAT))
        # A cut member list may hide an excluded person: dropped, never kept.
        chat = self.by_kind(self.capture.run(scope(max_members=1)))["chat:oneOnOne"]
        self.assertEqual(chat["reasons"], ["cap:member_item_cap"])
        self.assertIsNone(self.recorded(CHAT))
        self.assertEqual(self.store.resolve(handles[0], COS_ONLY)["error"], "not_visible")

    def test_a_reader_removed_from_a_chat_loses_it_once_a_read_records_the_change(self):
        self.graph.chat_members.append((U_VINCE, VINCE))
        self.capture.run(scope())
        handles = [r["citation"] for r in self.visible(VINCE_ONLY, CHAT)]
        self.assertEqual(len(handles), 2)
        # Removed upstream, but the read fails transiently: the membership read before
        # still decides (within its bound) until a read records the change.
        self.graph.chat_members.remove((U_VINCE, VINCE))
        self.graph.failing = {self.CHAT_MEMBERS: 503}
        self.capture.run(scope())
        self.assertEqual(len(self.visible(VINCE_ONLY, CHAT)), 2)
        self.graph.failing = {}
        chat = self.by_kind(self.capture.run(scope()))["chat:oneOnOne"]
        self.assertEqual(chat["status"], "complete")
        self.assertEqual(self.visible(VINCE_ONLY, CHAT), [])
        for handle in handles:
            self.assertEqual(self.store.resolve(handle, VINCE_ONLY)["error"], "not_visible")
            self.assertIsNone(self.store.claim_rows(VINCE_ONLY)(handle))
            self.assertTrue(self.store.resolve(handle, COS_ONLY)["ok"])

    def test_a_kept_membership_older_than_the_serving_bound_is_not_served(self):
        from integrations.capture_store import UNIT_ACL_MAX_AGE, CaptureStore
        self.assertEqual(UNIT_ACL_MAX_AGE, timedelta(hours=48))
        self.capture.run(scope())
        self.graph.failing = {self.CHAT_MEMBERS: 429}
        self.capture.run(scope())                        # kept, not refreshed
        recorded = datetime.fromisoformat(self.recorded(CHAT)[1].replace("Z", "+00:00"))
        handles = [r["citation"] for r in self.visible(COS_ONLY, CHAT)]
        self.assertEqual(len(handles), 2)
        channel = [r["citation"] for r in self.visible(COS_ONLY, GENERAL)]
        for offset, served in ((timedelta(hours=47), True),
                               (UNIT_ACL_MAX_AGE, True),
                               (UNIT_ACL_MAX_AGE + timedelta(seconds=1), False),
                               (timedelta(days=30), False),
                               (-timedelta(hours=1), False)):   # dated after now
            with self.subTest(offset), CaptureStore(
                    self.store.path, self.audience,
                    clock=lambda o=offset: recorded + o) as later:
                rows = [r for r in later.records(COS_ONLY, *self.unit(CHAT))["items"]
                        if f"/{capture_teams.CONVERSATION}@" not in r["citation"]]
                self.assertEqual(len(rows), 2 if served else 0)
                for handle in handles:
                    self.assertEqual(later.resolve(handle, COS_ONLY).get("ok", False), served)
                    self.assertEqual(later.claim_rows(COS_ONLY)(handle) is not None, served)
                # A channel's membership is bounded alike.
                self.assertEqual(len([h for h in channel
                                      if later.resolve(h, COS_ONLY).get("ok")]),
                                 4 if served else 0)


class ProbeTests(unittest.TestCase):
    """``python3 -m integrations.graph_read teams-capture`` with the live-check spec's
    own argv over the fake Graph: its expectations hold on the fixture output."""

    SPEC = json.loads((ROOT / lv.SPECS_DIR_NAME / "graph.teams_capture.json").read_text())

    def run_spec(self, graph, state: str | None = None, *, confirmed: bool = True,
                 **flags: str) -> tuple[int, dict]:
        """The spec's argv (``flags`` replace its values) over ``graph``, its host state
        directory under ``state`` ($XDG_STATE_HOME; a fresh one when None). ``confirmed``:
        a person confirmed every channel and chat of ``graph`` (the content read)."""
        with tempfile.TemporaryDirectory() as tmp:
            if confirmed:
                confirm(state or tmp, list(graph.channel_types))
            argv = self.argv(tmp, **flags)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = graph_read.main(argv, environ={**ENV, "XDG_STATE_HOME": state or tmp},
                                       sender=graph)
        return code, json.loads(out.getvalue())

    def argv(self, tmp: str, **flags: str) -> list[str]:
        argv = [a.replace("{tmp}", tmp) for a in self.SPEC["command"][3:]]
        argv = [str(ROOT / a) if a.startswith("examples/") else a for a in argv]
        for flag, value in flags.items():
            argv[argv.index("--" + flag.replace("_", "-")) + 1] = value
        return argv

    def many(self, graph: FakeTeamsGraph, threads: list[str]) -> None:
        for thread in threads:
            graph.channel_types[thread] = "standard"
            graph.roots[thread], graph.changed[thread] = {}, {}

    def fixture(self) -> FakeTeamsGraph:
        graph = FakeTeamsGraph()
        graph.post_root(GENERAL, "1001", replies=2)
        graph.post_root(PRIVATE, "2001")
        graph.post_chat("3001")
        return graph

    def test_the_spec_passes_on_the_fixture_and_prints_no_content(self):
        graph = self.fixture()
        code, report = self.run_spec(graph)
        self.assertEqual(lv.check_expectations(self.SPEC, code, report), [], report)
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual(lv.blocked_parts(report), [])
        self.assertEqual(lv.completeness(self.SPEC, report)["complete"], True)
        self.assertGreater(report["leak_check"]["outsider_actor_withheld"], 0)
        # The verifier records the ontology relations the run holds (2 roots, 2 replies;
        # 1 chat message), so a pass names them for the source registry.
        self.assertEqual(lv._recorded(self.SPEC, report)["relations"],
                         {"channelMessages": 4, "chatMessages": 1})
        self.assertEqual(lv._recorded(self.SPEC, report)["summary"]["chats"], 1)
        # The Graph reads the run was answered for: the registry credits their endpoints.
        # The census's reads, then the confirmed channels' and chat's content reads (each
        # confirmed channel's own record, no joined-teams discovery).
        self.assertEqual(lv._recorded(self.SPEC, report)["operations_read"], [
            "graph.channel_members", "graph.channel_message_replies",
            "graph.channel_messages_delta", "graph.chat", "graph.chat_members",
            "graph.chat_messages", "graph.team", "graph.team_channel", "graph.team_channels",
            "graph.team_members", "graph.teams", "graph.teams_activity_report", "graph.user",
            "graph.user_chats", "graph.users"])
        self.assertEqual({k: report["census"][k] for k in (
            "status", "teams", "teams_named", "channels", "channels_named", "chats_named")},
            {"status": "complete", "teams": 1, "teams_named": 1, "channels": 2,
             "channels_named": 2, "chats_named": 1})
        self.assertEqual(report["census"]["message_counts"]["teams_counted"], 1)
        self.assertEqual((report["content"]["status"], report["content"]["channels_due"],
                          report["content"]["chats_due"]), ("census_cleared", 2, 1))
        # The live-check scope lists the chats of every allowlisted account, no other.
        self.assertEqual(chat_listings(graph), {graph.users[a] for a in
                                                MailScope.builtin().addresses})
        text = json.dumps(report)
        for value in (SECRET_TEXT, "PO12172025", COS, U_COS, TEAM, GENERAL, CHAT, "1001",
                      "do-not-leak", "TOKEN", "Operations", "QA hand-offs", "Bi-weekly"):
            self.assertNotIn(value, text)

    def test_past_the_cap_the_spec_runs_until_one_pass_is_read_and_fails_short_of_it(self):
        # A run bound below --runs or past MAX_RUNS, or a budget not a positive finite
        # number of seconds, is refused before any request.
        for flag, bad in (("max_runs", "1"), ("max_runs", "13"), ("budget_seconds", "0"),
                          ("budget_seconds", "nan"), ("budget_seconds", "inf")):
            graph = self.fixture()
            with tempfile.TemporaryDirectory() as tmp, \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(graph_read.main(self.argv(tmp, **{flag: bad}),
                                                 environ={**ENV, "XDG_STATE_HOME": tmp},
                                                 sender=graph), 1, bad)
            self.assertEqual((graph.calls, out.getvalue()), ([], ""), bad)
        threads = [f"19:many{n:03d}@thread.tacv2" for n in range(40)]
        for cut in (False, True):
            with self.subTest(cut=cut):
                graph = self.fixture()
                self.many(graph, threads)
                graph.post_root(threads[7], "4001")
                if cut:   # over max_replies_per_message 50 every run: never read complete
                    graph.post_root(threads[9], "4002", replies=51)
                code, report = self.run_spec(graph)
                found = [r["discovery"]["discovered_total"] for r in report["runs"]]
                self.assertEqual(set(found), {43})
                self.assertEqual(report["runs"][0]["discovery"]["reasons"],
                                 ["cap:conversation_cap"])
                self.assertTrue(report["pass"]["unvisited_falling"], report["pass"])
                if cut:
                    self.assertEqual(report["pass"]["runs"], 12)
                    self.assertEqual(report["pass"]["unvisited"][-1], 1)
                    self.assertFalse(report["pass"]["last_run_complete"])
                    self.assertEqual(report["status"], "incomplete")
                    self.assertIn("status", "".join(lv.check_expectations(
                        self.SPEC, code, report)))
                    self.assertFalse(lv.completeness(self.SPEC, report)["complete"])
                else:
                    # 16 a run, the one chat among them every run.
                    self.assertEqual(report["pass"]["unvisited"], [27, 12, 0])
                    self.assertEqual(lv.check_expectations(self.SPEC, code, report), [],
                                     report)
                    self.assertTrue(lv.completeness(self.SPEC, report)["complete"])
                    self.assertEqual(report["summary"]["units_captured"], 43)

    def test_a_conversation_created_mid_pass_is_not_read_before_a_census_clears_it(self):
        # Content is read only of what this invocation's census cleared: 20 channels
        # created after it (even ones in scope ahead) wait for the next census.
        graph = self.fixture()
        self.many(graph, [f"19:many{n:03d}@thread.tacv2" for n in range(40)])
        new = [f"19:new{n:03d}@thread.tacv2" for n in range(20)]
        reads = []

        def sender(method, url, *rest):
            if unquote(urlsplit(url).path).endswith("/messages/delta"):
                reads.append(url)
                if len(reads) == 1:   # the first content read: the census is done
                    self.many(graph, new)
            return graph(method, url, *rest)
        sender.channel_types = graph.channel_types   # confirmed: all 43, the new 20 too

        with tempfile.TemporaryDirectory() as host:
            confirm(host, [*graph.channel_types, *new])
            code, report = self.run_spec(sender, host, confirmed=False)
        unvisited = report["pass"]["unvisited"]
        self.assertEqual(unvisited, [27, 12, 0])                       # never rose
        self.assertEqual(report["pass"]["unvisited_first_run"], unvisited)
        self.assertEqual(report["content"]["confirmed_not_cleared"], 0)   # no confirmation
        self.assertFalse([u for _m, u in graph.calls if "new0" in unquote(u)])
        self.assertEqual(lv.check_expectations(self.SPEC, code, report), [], report)
        self.assertEqual(report["summary"]["units_captured"], 43)

    def test_the_day_a_carried_pass_completes_passes_and_starts_no_new_pass(self):
        graph = self.fixture()
        self.many(graph, [f"19:many{n:03d}@thread.tacv2" for n in range(40)])
        with tempfile.TemporaryDirectory() as host:
            day1 = self.run_spec(graph, host, runs="1", max_runs="2")[1]   # 32 of 43 read
            code, day2 = self.run_spec(graph, host)
        self.assertEqual((day1["status"], day1["pass"]["unvisited"]), ("incomplete", [27, 12]))
        # Day 2's first run completes the carried pass: its forced --runs 2 never starts
        # the next pass (whose unvisited would climb back to 27).
        self.assertEqual((day2["storage"]["carried"], day2["pass"]["runs"],
                          day2["pass"]["stop"], day2["pass"]["unvisited"]),
                         (True, 1, "complete", [0]))
        self.assertEqual(lv.check_expectations(self.SPEC, code, day2), [], day2)
        self.assertTrue(lv.completeness(self.SPEC, day2)["complete"])

    def test_the_host_state_carries_a_pass_across_daily_runs(self):
        # A 300-message chat over a cap of 200 a run: one run per daily invocation, the
        # store kept in the host state directory, so the second invocation completes it.
        graph = FakeTeamsGraph()
        for n in range(300):
            graph.post_chat(f"{5000 + n}", f"message {n}")
        with tempfile.TemporaryDirectory() as host, tempfile.TemporaryDirectory() as tmp:
            scope_file = Path(tmp) / "scope.json"
            scope_file.write_text(json.dumps({
                "schema": capture_teams.SCOPE_SCHEMA, "member_chats": "mail_allowlist",
                "members": [{"address": COS, "joined_teams": False}], "page_size": 50,
                "max_pages_per_run": 20, "max_items_per_run": 200}))
            days = [self.run_spec(graph, host, scope=str(scope_file), runs="1",
                                  max_runs="1")[1] for _ in range(2)]
            store = (Path(host) / "plx-workbench" / "teams-capture-check"
                     / "injected-transport" / "teams-capture" / "capture.db")
            self.assertTrue(store.is_file())
            self.assertEqual({oct(d.stat().st_mode & 0o777) for d in store.parents[:4]},
                             {"0o700"})   # every directory it made below the base
            # No host state directory named (or a relative one): refused before any request.
            # A relative PLX_WORKBENCH_STATE_DIR is refused, never passed over for XDG.
            for env in (ENV, {**ENV, "XDG_STATE_HOME": "relative/state"},
                        {**ENV, "XDG_STATE_HOME": host,
                         "PLX_WORKBENCH_STATE_DIR": "relative/state"}):
                graph.calls.clear()
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(graph_read.main(self.argv(tmp), environ=env,
                                                     sender=graph), 1)
                self.assertEqual(graph.calls, [])
        chats = [[c for c in day["runs"][0]["conversations"] if c["kind"] == "chat"][0]
                 for day in days]
        self.assertEqual([(d["status"], d["storage"]["carried"], d["storage"]["host_state"])
                          for d in days],
                         [("incomplete", False, True), ("complete", True, True)])
        self.assertEqual([(c["status"], c["reasons"], c["resumed_from"]) for c in chats],
                         [("incomplete", ["cap:item_cap"], None), ("complete", [], "cursor")])
        self.assertEqual(days[1]["summary"]["messages_current"], 300)
        self.assertEqual(days[1]["pass"]["stop"], "complete")
        self.assertEqual({d["storage"]["state_base"] for d in days}, {"xdg_state_home"})

    def test_a_set_workbench_state_dir_holds_the_store_ahead_of_xdg(self):
        graph = self.fixture()
        with tempfile.TemporaryDirectory() as workbench, \
                tempfile.TemporaryDirectory() as xdg, tempfile.TemporaryDirectory() as tmp:
            reports = []
            confirm({**ENV, "XDG_STATE_HOME": xdg, "PLX_WORKBENCH_STATE_DIR": workbench},
                    list(graph.channel_types))
            for _ in range(2):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = graph_read.main(self.argv(tmp), sender=graph, environ={
                        **ENV, "XDG_STATE_HOME": xdg, "PLX_WORKBENCH_STATE_DIR": workbench})
                self.assertEqual(code, 0)
                reports.append(json.loads(out.getvalue()))
            store = (Path(workbench) / "teams-capture-check" / "injected-transport"
                     / "teams-capture" / "capture.db")
            self.assertTrue(store.is_file())
            self.assertEqual({oct(d.stat().st_mode & 0o777) for d in store.parents[:3]},
                             {"0o700"})
            self.assertEqual(list(Path(xdg).iterdir()), [])   # XDG untouched
        self.assertEqual([(r["storage"]["state_base"], r["storage"]["carried"],
                           r["storage"]["host_state"]) for r in reports],
                         [("workbench_state_dir", False, True),
                          ("workbench_state_dir", True, True)])

    def test_a_spent_time_budget_reports_incomplete_and_the_next_run_carries_on(self):
        # One conversation at a time: the deadline stops the very next read.
        self.spent_budget_case(workers=1, overshoot=0)

    def test_concurrent_conversations_overshoot_a_spent_budget_by_at_most_one_read_each(self):
        # Eight at a time: a conversation that started before the deadline was seen may
        # send the one read it had begun (its members read); none starts after it.
        self.spent_budget_case(workers=capture_teams.WORKERS,
                               overshoot=capture_teams.WORKERS - 1)

    def spent_budget_case(self, *, workers: int, overshoot: int) -> None:
        graph = self.fixture()
        self.many(graph, [f"19:many{n:03d}@thread.tacv2" for n in range(40)])
        parser = argparse.ArgumentParser()
        capture_teams.add_cli_arguments(parser)
        with tempfile.TemporaryDirectory() as host:
            env = {**ENV, "XDG_STATE_HOME": host}
            confirm(host, list(graph.channel_types))
            args = parser.parse_args(self.argv(host)[1:] + ["--workers", str(workers)])
            # Each conversation read (members or messages) after the census's last read
            # (the activity report) takes 315 s: the 6300 s budget is spent after 20 (a
            # conversation's members read, before its messages), inside run 1.
            def reads() -> list[str]:
                urls = [u for _m, u in graph.calls]
                report = [i for i, u in enumerate(urls) if "/reports/" in urlsplit(u).path]
                return [u for u in urls[report[0] + 1:] if re.search(
                    r"/(messages|members)", urlsplit(u).path)] if report else []
            spent = capture_teams.run_cli(args, environ=env, sender=graph,
                                          clock=lambda: 315.0 * len(reads()))
            captured = reads()
            after = capture_teams.run_cli(args, environ=env, sender=graph, clock=lambda: 0.0)
        self.assertEqual((spent["status"], spent["pass"]["stop"], spent["budget"]),
                         ("incomplete", "budget", {"seconds": 6300.0, "exhausted": True}))
        self.assertEqual(spent["pass"]["runs"], 1)   # no run starts after it, --runs or not
        self.assertGreaterEqual(len(captured), 20)   # no page read past the deadline
        self.assertLessEqual(len(captured), 20 + overshoot)
        # The read under way pauses on its cursor; the conversations due after it wait.
        run = spent["runs"][0]
        self.assertIn(capture_teams.TIME_BUDGET, run["discovery"]["reasons"])
        cut = [c for c in run["conversations"] if c["reasons"] == [capture_teams.TIME_BUDGET]]
        if workers == 1:   # the last one taken is the one the deadline paused
            self.assertEqual(run["conversations"][-1]["reasons"], [capture_teams.TIME_BUDGET])
        self.assertTrue(cut)
        self.assertLess(len(run["conversations"]), run["discovery"]["conversations"])
        self.assertTrue(spent["ok"], spent)          # incomplete, not failed: no leak, no error
        self.assertNotEqual(lv.check_expectations(self.SPEC, 0, spent), [])
        self.assertFalse(lv.completeness(self.SPEC, spent)["complete"])
        # The next run starts with the conversations the budget left and completes the pass.
        self.assertEqual((after["status"], after["storage"]["carried"], after["pass"]["stop"]),
                         ("complete", True, "complete"))
        self.assertEqual(lv.check_expectations(self.SPEC, 0, after), [], after)

    def test_a_missing_teams_permission_is_blocked_not_failed(self):
        graph = self.fixture()
        graph.forbidden = [r"/messages", r"/chats$"]
        code, report = self.run_spec(graph)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual({p["error_code"] for p in report["blocked_upstream"]},
                         {"missing_permission"})
        # The one Graph refusal shape (mail and SharePoint lists build the same part).
        self.assertIn({"part": "teams.chats", "source": "microsoft-graph", "http_status": 403,
                       "error_code": "missing_permission", "permission": "Chat.ReadBasic.All",
                       "cause": "host_misconfiguration"}, report["blocked_upstream"])
        parts = lv.blocked_parts(report)
        self.assertEqual({(p["http_status"], p["cause"]) for p in parts},
                         {(403, "host_misconfiguration")})
        self.assertEqual(lv.check_expectations(self.SPEC, code, report,
                                               self.SPEC["expect_blocked"]), [], report)
        self.assertNotEqual(lv.check_expectations(self.SPEC, code, report), [])

    def test_a_refused_chat_read_is_its_own_blocked_part_beside_the_members_one(self):
        chat_members = rf"/chats/{re.escape(CHAT)}/members"
        private_members = rf"/channels/{re.escape(PRIVATE)}/members"
        for forbidden, chat_part in (([private_members, chat_members], "teams.chat_members"),
                                     ([private_members, r"/chats$"], "teams.chats")):
            with self.subTest(chat_part):
                graph = self.fixture()
                graph.forbidden = forbidden
                code, report = self.run_spec(graph)
                self.assertEqual(lv.check_expectations(self.SPEC, code, report,
                                                       self.SPEC["expect_blocked"]), [], report)
                # The verdict keeps part names, not permissions: they must differ.
                self.assertEqual({p["part"] for p in lv.blocked_parts(report)},
                                 {"teams.members", chat_part})


if __name__ == "__main__":
    unittest.main()
