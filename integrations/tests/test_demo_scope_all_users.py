"""Mail-to-case and Teams chats cover every directory user
except the excluded people; no human content confirmation for Teams. Fakes only."""

from __future__ import annotations

import tempfile
import unittest

from integrations import capture_teams, graph_read
from integrations.capture_teams import TeamsCapture, TeamsScope, open_store
from integrations.people_exclusion import PeopleExclusion
from integrations.tests.test_capture_teams import (CAPTURE, COS, ENV, GENERAL, FakeTeamsGraph,
                                                   audience, chat_listings, scope)
from operating.case_mail_scope import MailScope, MailScopeError

EXCLUDED_ID = "0e0e0e0e-0000-4000-8000-00000000e0e1"
EXCLUDED_MAIL = "private.person@petrasoap.com"
NEWCOMER, NEWCOMER_ID = "newcomer@petrasoap.com", "0000cccc-0000-0000-0000-000000000009"
LISTED = ("receiving@petrasoap.com",)


def mail_scope(enabled: bool = True) -> MailScope:
    return MailScope(LISTED, all_directory=enabled)


class CaseMailScopeTests(unittest.TestCase):
    def test_a_directory_user_off_the_list_is_now_in_scope(self):
        guarded = mail_scope()
        guarded.attach_people(PeopleExclusion([EXCLUDED_ID]))
        self.assertTrue(guarded.allows([NEWCOMER_ID, NEWCOMER]))
        self.assertFalse(mail_scope(False).allows([NEWCOMER_ID, NEWCOMER]))   # the old mode

    def test_an_excluded_oid_is_never_in_scope(self):
        guard = PeopleExclusion([EXCLUDED_ID])
        guarded = mail_scope()
        guarded.attach_people(guard)
        self.assertFalse(guarded.allows([EXCLUDED_ID, EXCLUDED_MAIL]))
        self.assertFalse(guarded.allows([EXCLUDED_ID.upper()]))

    def test_a_missing_or_unusable_exclusion_list_fails_closed(self):
        for people in (None, PeopleExclusion([]), PeopleExclusion(unavailable=
                                                                  "excluded_people_file_unreadable")):
            guarded = mail_scope()
            guarded.attach_people(people)
            self.assertFalse(guarded.allows([NEWCOMER_ID, NEWCOMER]), people)

    def test_an_identity_set_without_an_object_id_is_not_decidable(self):
        guarded = mail_scope()
        guarded.attach_people(PeopleExclusion([EXCLUDED_ID]))
        self.assertFalse(guarded.allows([NEWCOMER]))

    def test_the_shipped_file_turns_it_on_and_a_bad_switch_is_refused(self):
        self.assertTrue(MailScope.builtin().all_directory)
        document = {"schema": "plx-case-mail-scope/v1",
                    "mailboxes": [{"address": LISTED[0], "status": "reported_allowlisted"}],
                    "all_directory": {"enabled": "yes"}}
        with self.assertRaises(MailScopeError):
            MailScope.from_json(document)


class TeamsFollowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph = FakeTeamsGraph()
        self.graph.users[NEWCOMER] = NEWCOMER_ID
        self.graph.users[EXCLUDED_MAIL] = EXCLUDED_ID
        self.graph.post_root(GENERAL, "1001")
        self.graph.post_chat("3001")

    def capture(self, env=ENV) -> TeamsCapture:
        store = open_store(self.tmp.name, audience())
        self.addCleanup(store.close)
        api, _transport = graph_read.build_graph(env, sender=self.graph)
        return TeamsCapture(store, api, audience(), CAPTURE)

    def widened(self) -> TeamsScope:
        return TeamsScope({"schema": capture_teams.SCOPE_SCHEMA,
                           "members": [{"address": COS, "joined_teams": True}],
                           "member_chats": "mail_allowlist"},
                          allowlist=MailScope(("cos@petrasoap.com", "vince@petrasoap.com"),
                                              all_directory=True))

    def test_chats_follow_the_widened_set_and_never_an_excluded_person(self):
        self.capture().run(self.widened())
        listed = chat_listings(self.graph)
        self.assertIn(NEWCOMER_ID, listed)                      # not on the list, now read
        self.assertFalse({EXCLUDED_MAIL, EXCLUDED_ID} & listed)
        self.assertFalse([u for _m, u in self.graph.calls
                          if EXCLUDED_ID in u or "private.person" in u.lower()])

    def test_the_old_allowlist_mode_does_not_read_the_newcomer(self):
        self.capture().run(scope())
        self.assertNotIn(NEWCOMER_ID, chat_listings(self.graph))

    def test_without_an_exclusion_list_no_chat_is_requested(self):
        env = {k: v for k, v in ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
        run = self.capture(env).run(self.widened())
        self.assertEqual(chat_listings(self.graph), set())
        self.assertFalse(run["complete"])


class NoConfirmationTests(unittest.TestCase):
    def test_content_is_read_without_a_confirmation_file(self):
        graph = FakeTeamsGraph()
        graph.post_root(GENERAL, "1001")
        graph.post_chat("3001")
        with tempfile.TemporaryDirectory() as host:
            store = open_store(host, audience())
            self.addCleanup(store.close)
            api, _t = graph_read.build_graph(ENV, sender=graph)
            capture = TeamsCapture(store, api, audience(), CAPTURE)
            blocked: list = []
            _census, private = capture.census(scope(), blocked, lambda: False)
            content, left_out = capture_teams.confirmed_scope(
                scope(), private, {"form": capture_teams.ALL_CENSUS_CLEARED})
            self.assertEqual(left_out, 0)
            self.assertIsNotNone(content)
            self.assertTrue(content.teams or content.chats)
            self.assertNotIn("teams.content_confirmation", [p.get("part") for p in blocked])


if __name__ == "__main__":
    unittest.main()
