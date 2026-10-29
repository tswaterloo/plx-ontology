"""The people-exclusion guard (access record 64fca27acc88) over an injected transport.

Mail, Teams chats and OneDrives of excluded people are refused before any request,
keyed by Entra object ID; a message an excluded person sent is dropped before it is
stored or indexed; with no ID list every gated read fails closed. Every ID and
address here is synthetic. Evidence label: ``injected-transport``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from integrations import graph_read, people_exclusion
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.people_exclusion import (ExclusionUnavailable, PeopleExcluded,
                                           PeopleExclusion)
from integrations.live_source_guard import BoundedBackoff, NotVisible
from integrations.tests import test_capture_mail as tm
from integrations.tests import test_capture_sharepoint_estate as te
from integrations.tests import test_capture_teams as tc
from integrations.tests import test_live_source_adapters as ta
from integrations.capture_store import CaptureError
from integrations.capture_teams import CONVERSATION, Conversation, TeamsCapture, open_store

EXCLUDED = tm.BOX_B                         # the synthetic excluded person's mailbox
EXCLUDED_UPN = tm.UPN_B
ENV_EXCLUDING_B = {**tm.ENV, people_exclusion.ENV_IDS: EXCLUDED}
ENV_ABSENT = {k: v for k, v in tm.ENV.items() if k != people_exclusion.ENV_IDS}


class StubApi:
    """``get_pages`` only: the directory listing ``bind`` reads."""

    def __init__(self, users=(), complete=True, fail=None):
        self.users, self.complete, self.fail, self.calls = list(users), complete, fail, 0

    def get_pages(self, url, **_kwargs):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.users, {"complete": self.complete}


class GuardConfigTests(unittest.TestCase):
    def test_absent_empty_or_invalid_lists_fail_closed(self):
        cases = {"absent": ({}, "excluded_people_oids_absent"),
                 "empty": ({people_exclusion.ENV_IDS: " , "}, "excluded_people_oids_absent"),
                 "invalid": ({people_exclusion.ENV_IDS: f"{EXCLUDED},not-an-id"},
                             "excluded_people_oids_invalid")}
        for name, (environ, code) in cases.items():
            with self.subTest(name):
                guard = PeopleExclusion.from_env(environ)
                self.assertEqual(guard.unavailable, code)
                with self.assertRaises(ExclusionUnavailable) as raised:
                    guard.is_excluded(tm.BOX_A)
                self.assertEqual(raised.exception.code, code)
                self.assertIsInstance(raised.exception, NotVisible)

    def test_the_committed_config_names_nobody(self):
        config = json.loads(people_exclusion.CONFIG.read_text())
        self.assertEqual(config["schema"], people_exclusion.SCHEMA)
        self.assertEqual(config["object_ids"], [])

    def test_id_file_must_be_absolute_readable_and_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "ids.json"
            outside.write_text(json.dumps({"schema": people_exclusion.SCHEMA,
                                           "object_ids": [EXCLUDED]}))
            guard = PeopleExclusion.from_env({people_exclusion.ENV_FILE: str(outside)})
            self.assertIsNone(guard.unavailable)
            self.assertTrue(guard.is_excluded(EXCLUDED.upper()))
            self.assertFalse(guard.is_excluded(tm.BOX_A))
            self.assertEqual(PeopleExclusion.from_env({people_exclusion.ENV_FILE: "ids.json"})
                             .unavailable, "excluded_people_file_unreadable")
            outside.write_text("{not json")
            self.assertEqual(PeopleExclusion.from_env({people_exclusion.ENV_FILE: str(outside)})
                             .unavailable, "excluded_people_file_unreadable")
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            inside = repo / "ids.json"
            inside.write_text(json.dumps({"schema": people_exclusion.SCHEMA,
                                          "object_ids": [EXCLUDED]}))
            self.assertEqual(PeopleExclusion.from_env({people_exclusion.ENV_FILE: str(inside)})
                             .unavailable, "excluded_people_file_in_git")

    def test_addresses_need_a_complete_directory_listing(self):
        guard = PeopleExclusion([EXCLUDED])
        with self.assertRaises(ExclusionUnavailable):
            guard.is_excluded(EXCLUDED_UPN)          # unbound: an address is undecided
        for api in (StubApi(complete=False), StubApi(fail=RuntimeError("boom"))):
            with self.subTest(api.complete):
                guard = PeopleExclusion([EXCLUDED])
                with self.assertRaises(ExclusionUnavailable):
                    guard.bind(api)
                self.assertEqual(guard.unavailable, "excluded_people_unresolved")
                with self.assertRaises(ExclusionUnavailable):
                    guard.is_excluded(tm.BOX_A)      # one failed bind closes everything
        guard = PeopleExclusion([EXCLUDED])
        guard.bind(StubApi([{"id": EXCLUDED, "userPrincipalName": "B.Person@Example-plx.com",
                             "mail": EXCLUDED_UPN, "proxyAddresses": ["smtp:alias@x.example"]},
                            {"id": tm.BOX_A, "userPrincipalName": tm.UPN_A}]))
        for value in (EXCLUDED_UPN, "b.person@example-plx.com", "ALIAS@x.example", EXCLUDED):
            self.assertTrue(guard.is_excluded(value), value)
        self.assertFalse(guard.is_excluded(tm.UPN_A))
        with self.assertRaises(PeopleExcluded) as raised:
            guard.refuse_onedrive_path("b_person_example-plx_com")
        self.assertEqual(raised.exception.kind, "onedrive_owner")

    def test_nothing_names_an_excluded_person(self):
        guard = PeopleExclusion([EXCLUDED])
        guard.bind(StubApi([{"id": EXCLUDED, "userPrincipalName": EXCLUDED_UPN}]))
        try:
            guard.refuse([EXCLUDED_UPN], "sender")
        except PeopleExcluded as exc:
            refused = exc
        text = " ".join([repr(guard), str(refused), repr(refused),
                         json.dumps(guard.block_part("mail.people_exclusion")),
                         json.dumps(PeopleExclusion().block_part("x"))])
        for secret in (EXCLUDED, EXCLUDED.lower(), EXCLUDED_UPN):
            self.assertNotIn(secret, text)


    def test_a_block_part_carries_a_cause_live_verify_knows(self):
        import devloop.live_verify as live_verify
        absent = PeopleExclusion.from_env({}).block_part("mail.people_exclusion")
        self.assertEqual(live_verify.blocked_cause(declared=absent["cause"]), "later_ask")
        unresolved = PeopleExclusion([EXCLUDED])
        unresolved._bind_failed = True
        self.assertIn(unresolved.block_part("mail.people_exclusion")["cause"],
                      live_verify.CAUSES)


class MailGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.graph = tm.FakeGraph()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MailCaptureStore(Path(self.tmp.name) / "state", clock=tm.Clock())

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def reader(self, environ) -> GraphMailReader:
        api, _transport = graph_read.build_graph(
            environ, sender=self.graph,
            retry=BoundedBackoff(sleep=lambda _s: None, jitter=lambda: 0.0))
        return GraphMailReader(api, tm.capture_audience())

    def capture(self, environ) -> MailCapture:
        return MailCapture(self.store, self.reader(environ), tm.CAPTURE, page_size=2,
                           sleep=lambda _s: None)

    def naming(self, *values: str) -> list[str]:
        return [u for _m, u, _h in self.graph.calls
                if any(v.lower() in unquote(u).lower() for v in values)]

    def test_an_excluded_object_id_is_refused_with_zero_requests(self):
        reader = self.reader(ENV_EXCLUDING_B)
        for call in (lambda: reader.list_messages(tm.CAPTURE, EXCLUDED),
                     lambda: reader.get_message(tm.CAPTURE, EXCLUDED, "MSG-B1"),
                     lambda: reader.open_mailbox(tm.CAPTURE, EXCLUDED.upper())):
            with self.assertRaises(PeopleExcluded) as raised:
                call()
            self.assertEqual(raised.exception.kind, "mailbox")
        self.assertEqual(self.graph.calls, [])          # not even a token request

    def test_an_excluded_address_is_refused_before_any_request_naming_it(self):
        reader = self.reader(ENV_EXCLUDING_B)
        with self.assertRaises(PeopleExcluded):
            reader.list_messages(tm.CAPTURE, EXCLUDED_UPN)
        self.assertEqual(self.naming(EXCLUDED, EXCLUDED_UPN), [])
        self.assertEqual(reader.open_mailbox(tm.CAPTURE, tm.UPN_A)["object_id"],
                         tm.BOX_A)                      # the others still open

    def test_an_absent_list_refuses_every_mail_read_with_zero_requests(self):
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1")
        for environ in (ENV_ABSENT, {**tm.ENV, people_exclusion.ENV_IDS: ""}):
            reader = self.reader(environ)
            for call in (lambda: reader.list_messages(tm.CAPTURE, tm.UPN_A),
                         lambda: reader.list_messages(tm.CAPTURE, tm.BOX_A),
                         lambda: reader.get_message(tm.CAPTURE, tm.UPN_A, "MSG-1"),
                         lambda: reader.mailbox_owner_acl(tm.CAPTURE, tm.UPN_A)):
                with self.assertRaises(ExclusionUnavailable) as raised:
                    call()
                self.assertEqual(raised.exception.code, "excluded_people_oids_absent")
            run = self.capture(environ).run(tm.SCOPE)
            self.assertEqual(run["mailboxes"], [])
            self.assertFalse(run["complete"])
            self.assertEqual(run["people_blocked"]["error_code"], "excluded_people_oids_absent")
            self.assertEqual(run["people_blocked"]["later_ask"], "excluded-people-oids")
        self.assertEqual(self.graph.calls, [])
        self.assertEqual(self.store.messages(tm.CAPTURE, tm.capture_audience())["total"], 0)

    def test_a_named_excluded_mailbox_is_a_typed_refusal_in_the_run(self):
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1")
        self.graph.put(tm.BOX_B, "MSG-B1", "CKB-1")
        run = self.capture(ENV_EXCLUDING_B).run(tm.SCOPE)
        self.assertEqual(run["people_excluded"], 1)
        self.assertEqual([m.get("status") for m in run["mailboxes"]],
                         ["complete", "people_excluded"])
        self.assertFalse(run["complete"])
        self.assertEqual(self.naming(EXCLUDED, EXCLUDED_UPN), [])
        self.assertNotIn(EXCLUDED, json.dumps(run))
        self.assertNotIn(EXCLUDED_UPN, json.dumps(run))

    def test_a_message_from_an_excluded_sender_is_never_stored_or_indexed(self):
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1")
        self.graph.put(tm.BOX_A, "MSG-2", "CK-2", subject="Sent by the excluded person")
        self.graph.messages[tm.BOX_A]["MSG-2"]["from"] = {
            "emailAddress": {"address": EXCLUDED_UPN.upper()}}
        scope = tm.CaptureScope([{"address": tm.UPN_A, "folders": ["inbox"]}], window_days=90,
                                internal_domains=["example-plx.com"], allowlist=tm.SYNTHETIC)
        run = self.capture(ENV_EXCLUDING_B).run(scope)
        folder = run["mailboxes"][0]["folders"][0]
        self.assertEqual(folder["counts"]["people_dropped"], 1)
        self.assertEqual(folder["counts"]["versions_new"], 1)
        audience = tm.capture_audience()
        self.assertEqual([m["message_id"] for m in
                          self.store.index(tm.CAPTURE, audience)["items"]], ["MSG-1"])
        self.assertEqual(self.store.messages(tm.CAPTURE, audience)["total"], 1)
        self.assertEqual(self.naming("MSG-2"), [])       # no body or attachment read
        stored = b"".join(p.read_bytes() for p in Path(self.tmp.name).rglob("*") if p.is_file())
        self.assertNotIn(b"Sent by the excluded person", stored)
        # Read directly, the same message is refused, never returned.
        with self.assertRaises(PeopleExcluded) as raised:
            self.reader(ENV_EXCLUDING_B).get_message(tm.CAPTURE, tm.UPN_A, "MSG-2")
        self.assertEqual(raised.exception.kind, "sender")

    def test_a_message_sent_on_behalf_by_an_excluded_person_is_dropped(self):
        # Graph returns only the $select'ed fields, so every message read must select
        # ``sender`` as well as ``from`` or a send-on-behalf goes unseen live.
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1")
        self.graph.put(tm.BOX_A, "MSG-2", "CK-2", subject="Sent on behalf")
        self.graph.messages[tm.BOX_A]["MSG-2"]["sender"] = {
            "emailAddress": {"address": EXCLUDED_UPN}}
        scope = tm.CaptureScope([{"address": tm.UPN_A, "folders": ["inbox"]}], window_days=90,
                                internal_domains=["example-plx.com"], allowlist=tm.SYNTHETIC)
        run = self.capture(ENV_EXCLUDING_B).run(scope)
        self.assertEqual(run["mailboxes"][0]["folders"][0]["counts"]["people_dropped"], 1)
        message_reads = [unquote(u) for _m, u, _h in self.graph.calls if "/messages" in u]
        self.assertTrue(message_reads)
        for url in message_reads:
            selected = parse_qs(urlsplit(url).query).get("$select", [""])[0].split(",")
            self.assertIn("from", selected, url)
            self.assertIn("sender", selected, url)

    def test_a_listing_drops_the_excluded_senders_messages(self):
        sent = ta.message("m2", "ck2")
        sent["from"] = {"emailAddress": {"address": ta.CEO_UPN.upper()}}
        sender = ta.FakeSender({ta.MSG_PATH: ta.j(200, {"value": [ta.message("m1", "ck1"),
                                                                   sent]})})
        api, _transport = ta.graph_api(sender, {**ta.ENV, people_exclusion.ENV_IDS: ta.CEO_ID})
        listed = GraphMailReader(api, ta.audience()).list_messages(ta.ACTOR, ta.MAILBOX)
        self.assertEqual([m["id"] for m in listed["items"]], ["m1"])
        self.assertEqual(listed["completeness"]["people_dropped"], 1)
        self.assertNotIn(ta.CEO_UPN, json.dumps(listed).lower())


class TeamsGuardTests(unittest.TestCase):
    """capture_teams uses the same guard. Chats are read over the fake only: no Teams
    chat is ever read live by this module."""

    SENDER = "0000aaaa-0000-0000-0000-000000000009"   # an excluded sender, no member

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph = tc.FakeTeamsGraph()
        self.graph.post_root(tc.GENERAL, "1001", replies=2)
        self.graph.post_root(tc.GENERAL, "1002")
        self.graph.post_root(tc.PRIVATE, "2001", "Board-only pricing")
        self.graph.post_chat("3001")
        self.audience = tc.audience()
        self.store = open_store(self.tmp.name, self.audience)
        self.addCleanup(self.store.close)

    def capture(self, excluded=None, *, absent=False):
        env = dict(tc.ENV)
        if absent:
            env.pop(people_exclusion.ENV_IDS)
        elif excluded:
            env[people_exclusion.ENV_IDS] = excluded
        api, _transport = graph_read.build_graph(env, sender=self.graph)
        return TeamsCapture(self.store, api, self.audience, tc.CAPTURE)

    def paths(self, thread):
        kind, unit = ("teams_chat", Conversation("chat", thread).unit) if thread == tc.CHAT \
            else ("teams_channel", Conversation("channel", thread, tc.TEAM).unit)
        # Message ids: the conversation's own record (CONVERSATION) is no
        # message (as the CLI counts it apart).
        return {r.path[-1] for r in self.store.current_records(kind, unit)
                if r.path[-1] != CONVERSATION}

    def test_an_absent_or_empty_list_refuses_every_teams_read_with_zero_requests(self):
        for name, kwargs in {"absent": {"absent": True}, "empty": {"excluded": " , "}}.items():
            with self.subTest(name):
                self.graph.calls.clear()
                run = self.capture(**kwargs).run(tc.scope())
                self.assertEqual(self.graph.calls, [])          # not even the token
                self.assertFalse(run["complete"])
                self.assertEqual(run["discovery"]["status"], "blocked")
                [part] = run["blocked_upstream"]
                self.assertEqual((part["source"], part["error_code"], part["later_ask"]),
                                 ("people-exclusion", "excluded_people_oids_absent",
                                  "excluded-people-oids"))
        self.assertEqual(self.paths(tc.GENERAL) | self.paths(tc.CHAT), set())

    def test_an_unresolved_directory_reads_nothing_else(self):
        self.graph.failing = {r"^/v1\.0/users$": 503}
        run = self.capture(tc.U_VINCE).run(tc.scope())
        self.assertFalse(run["complete"])
        self.assertEqual({unquote(u).split("?")[0] for u in self.graph.gets()},
                         {"https://graph.microsoft.com/v1.0/users"})
        self.assertEqual(run["conversations"], [])

    def test_an_excluded_member_is_refused_before_any_request_naming_them(self):
        run = self.capture(tc.U_VINCE).run(tc.scope())
        self.assertEqual(run["discovery"]["people_excluded"], 1)
        for url in self.graph.gets():
            self.assertNotIn(tc.U_VINCE, unquote(url))
            self.assertNotIn(tc.VINCE, unquote(url).lower())
        self.assertNotIn(tc.U_VINCE, json.dumps(run, default=sorted))
        self.assertNotIn(tc.VINCE, json.dumps(run, default=sorted))

    def test_a_chat_with_an_excluded_member_is_never_read_and_is_withheld(self):
        # Captured before the person was excluded: the store holds the chat.
        self.capture().run(tc.scope())
        self.assertEqual(self.paths(tc.CHAT), {"3001"})
        unit = Conversation("chat", tc.CHAT).unit
        self.assertTrue(self.store.records(tc.COS_ONLY, "teams_chat", unit)["items"])
        self.graph.calls.clear()
        self.graph.post_chat("3002", "after the exclusion")
        run = self.capture(tc.U_OTHER).run(tc.scope())
        chat = [c for c in run["conversations"] if c["kind"] == "chat"]
        self.assertEqual([c["status"] for c in chat], ["people_excluded"])
        self.assertEqual(run["people_excluded"], 1)
        self.assertFalse([u for u in self.graph.gets() if f"/chats/{tc.CHAT}/messages"
                          in unquote(u)])
        self.assertNotIn("3002", self.paths(tc.CHAT))
        # What the earlier capture held loses its membership: nobody sees it.
        try:
            items = self.store.records(tc.COS_ONLY, "teams_chat", unit)["items"]
        except CaptureError:
            items = []
        self.assertEqual(items, [])
        # Withheld every run is settled, not a gap: the channels still complete the pass.
        self.assertTrue(run["complete"], run)

    def test_a_chat_whose_member_list_is_truncated_is_never_read(self):
        # The unlisted members may include an excluded person: the guard cannot decide,
        # so the chat's messages are not requested (fail closed), its run is a gap.
        run = self.capture(self.SENDER).run(tc.scope(max_members=1))
        chat = [c for c in run["conversations"] if c["kind"] == "chat"]
        self.assertEqual([(c["status"], c["reasons"]) for c in chat],
                         [("incomplete", ["cap:member_item_cap"])])
        self.assertFalse([u for u in self.graph.gets() if f"/chats/{tc.CHAT}/messages"
                          in unquote(u)])
        self.assertEqual(self.paths(tc.CHAT), set())
        self.assertFalse(run["complete"])

    def test_messages_an_excluded_person_sent_are_dropped_before_storage(self):
        sender = {"user": {"id": self.SENDER, "displayName": "Excluded"}}
        self.graph.roots[tc.GENERAL]["1002"]["from"] = sender
        self.graph.replies[(tc.GENERAL, "1001")][0]["from"] = sender
        self.graph.chat["3001"]["from"] = sender
        run = self.capture(self.SENDER).run(tc.scope())
        self.assertEqual(run["people_dropped"], 3)
        self.assertEqual(self.paths(tc.GENERAL), {"1001", "10012"})
        self.assertEqual(self.paths(tc.CHAT), set())
        self.assertTrue(run["complete"], run)   # dropped by policy, not a provenance gap
        self.assertNotIn(self.SENDER, json.dumps(run, default=sorted))


class OneDriveGuardTests(te.EstateCase):
    """Estate discovery's personal sites (OneDrives) through the shared guard."""

    EXCLUDED_SITE = (f"{te.MY_HOST},99999999-0000-0000-0000-000000000001,"
                     "99999999-0000-0000-0000-000000000002")

    def setUp(self) -> None:
        super().setUp()
        te.SITES[self.EXCLUDED_SITE] = f"https://{te.MY_HOST}/personal/{te.EXCLUDED_PATH}"
        self.addCleanup(te.SITES.pop, self.EXCLUDED_SITE)
        self.drive.sites.append(self.EXCLUDED_SITE)
        self.drive.libraries = {**te.LIBRARIES,
                                self.EXCLUDED_SITE: [("b!excluded-Drive_77", "Documents")]}

    def capture_with(self, env) -> te.SharePointCapture:
        api, transport = graph_read.build_graph(
            env, sender=self.drive,
            retry=BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0))

        def reader(drive_id):
            return te.SharePointReader(api, te.ts.capture_audience(), drive_id,
                                       transport=transport, read_permissions=True,
                                       max_acl_reads=te.cs.MAX_ACL_READS)

        return te.SharePointCapture(self.store, reader, te.ts.CAPTURE,
                                    estate=graph_read.SharePointSites(api, te.ts.SP_HOST))

    def requests_naming(self, site: str) -> list[str]:
        return [u for u in self.urls() if site in u or "excluded-Drive_77" in u]

    def test_an_excluded_owners_onedrive_is_refused_before_its_libraries_are_listed(self):
        run = self.capture_with(te.ts.ENV).run(te.estate_scope())
        counts = run["discovery"]["counts"]
        self.assertEqual(self.requests_naming(self.EXCLUDED_SITE), [])
        self.assertEqual((counts["sites_personal"], counts["sites_people_excluded"],
                          counts["sites_people_withheld"]), (2, 1, 0))
        self.assertNotIn("b!excluded-Drive_77", self.roots(run))
        self.assertIn(te.PERSONAL_DRIVE, self.roots(run))   # another OneDrive still read
        self.assertEqual(run["discovery"]["gaps"], ["library_listing:http_403"])
        text = json.dumps(run)
        for secret in (te.EXCLUDED_OID, te.EXCLUDED_UPN, te.EXCLUDED_PATH):
            self.assertNotIn(secret, text)

    def test_an_absent_or_empty_list_withholds_every_onedrive_with_a_typed_gap(self):
        for value in (None, "", " , "):
            env = {k: v for k, v in te.ts.ENV.items() if k != people_exclusion.ENV_IDS}
            if value is not None:
                env[people_exclusion.ENV_IDS] = value
            start = len(self.drive.calls)
            run = self.capture_with(env).run(te.estate_scope())
            urls = self.urls(start)
            counts = run["discovery"]["counts"]
            self.assertEqual([u for u in urls if te.MY_HOST in u.split("?")[0]
                              or "/v1.0/users" in u], [])
            self.assertEqual((counts["sites_people_withheld"],
                              counts["libraries_personal_planned"]), (2, 0))
            self.assertIn("people_exclusion:excluded_people_oids_absent",
                          run["discovery"]["gaps"])
            self.assertFalse(run["discovery"]["complete"])
            self.assertIn(te.ts.DRIVE, self.roots(run))   # site libraries unaffected

    def test_an_unresolved_directory_withholds_every_onedrive(self):
        self.drive.users_status = 503
        run = self.capture_with(te.ts.ENV).run(te.estate_scope())
        counts = run["discovery"]["counts"]
        self.assertEqual([u for u in self.urls() if te.MY_HOST in u.split("?")[0]], [])
        self.assertEqual(counts["sites_people_withheld"], 2)
        self.assertIn("people_exclusion:excluded_people_unresolved", run["discovery"]["gaps"])

    def test_a_personal_site_whose_path_names_no_owner_is_withheld(self):
        te.SITES[self.EXCLUDED_SITE] = f"https://{te.MY_HOST}/"
        run = self.capture_with(te.ts.ENV).run(te.estate_scope())
        self.assertEqual(self.requests_naming(self.EXCLUDED_SITE), [])
        self.assertIn("people_exclusion:onedrive_owner_unresolved", run["discovery"]["gaps"])

    def test_an_estate_without_personal_sites_never_lists_the_directory(self):
        self.drive.sites = [s for s in self.drive.sites if not s.startswith(te.MY_HOST)]
        self.capture_with(te.ts.ENV).run(te.estate_scope())
        self.assertEqual([u for u in self.urls() if "/v1.0/users" in u], [])


if __name__ == "__main__":
    unittest.main()
