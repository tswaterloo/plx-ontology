"""PRIV-1114-1: a captured message is served only when every person on it is cleared.

The mail item gate (``RuntimeCitations.mail_capture``, which the host's item gate, the claim
gate and ``plx.read`` all read through) and the search gate
(``CaptureCaseHostTools._mail_search_gate``) withhold a message unless its sender and every
To/Cc/Bcc recipient is a cleared person of ours or an external counterpart
(``operating.excluded_people.participants_withheld`` over ``people_graph.PeopleDirectory``,
the rule of the people graph and vendor terms engines). Every tool that reaches mail
through a gate inherits it: plx.find, plx.object, plx.read, plx.around, plx.aggregate,
plx.query, plx.documents, and the plans of plx.investigate built on them.

Fixture: three messages to the orders mailbox from an outside supplier, one addressed to
the orders mailbox only (cleared), one copied to a tenant address no captured mailbox and
no directory can decide (undecided), one copied to the finance mailbox whose owner is
excluded after the capture ran (excluded). Every answer shows the cleared one and nothing
of the other two: no subject, no message id, no count.

Evidence label: injected-transport (the mail capture over the capture tests' in-process
FakeGraph, behind the real GET-only client; no network, made-up ids and addresses).
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, READER_A, SCOPE,
                                                   UPN_A, UPN_B, Clock, FakeGraph,
                                                   capture_audience, reader_audience)
from ontology import mentions
from ontology.people_directory import address_key
from ontology import schema as schema_loader
from ontology.people_links import people_links_step
from ontology.project import MailCaptures, build_store
from operating.case_mail_scope import MailScope
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaptureCaseHostTools, CaseCaptures
from operating.cases import CitationUnresolvable
from operating.mcp_server import MCPServer
from operating.ontology_tools_results import item_gate_from_case_runtime
from operating.tests.test_case_tools_host import NOW, _client

SUPPLIER = "supplier@outside.example"
UNDECIDED = "pat.unlisted@example-plx.com"   # a tenant domain; no mailbox, no directory
SUBJECTS = {"cleared": "Cleared order note", "undecided": "Undecided copy note",
            "excluded": "Excluded copy note"}
MESSAGES = {"cleared": "MSG-OK", "undecided": "MSG-UND", "excluded": "MSG-EXC"}
# The finance mailbox's owner, excluded after the capture ran.
EXCLUDED_B = {**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_B}
HIDDEN = ("undecided", "excluded")
EMAIL_LIST = {"object_type": "Email", "limit": 50,
              "filters": [{"property": "subject", "op": "present"}]}


class MailParticipantsClearedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "mail"
        graph = FakeGraph()
        for name, mid in MESSAGES.items():
            graph.put(BOX_A, mid, "CK-" + mid, subject=SUBJECTS[name])
        graph.messages[BOX_A]["MSG-UND"]["ccRecipients"] = [
            {"emailAddress": {"address": UNDECIDED}}]
        graph.messages[BOX_A]["MSG-EXC"]["ccRecipients"] = [
            {"emailAddress": {"address": UPN_B}}]
        store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
        items = store.index(READER_A, reader_audience())["items"]
        cls.handles = {name: next(i["l1_handle"] for i in items if i["message_id"] == mid)
                       for name, mid in MESSAGES.items()}
        # The capture's bound guard recorded the tenant's uncleared addresses beside its
        # listing; the class runs without that record (as a store recorded before it, or
        # one whose record aged out): an address no mailbox and no listing holds is then
        # undecided. ``TenantRecordTests`` below covers the record itself.
        cls.record = store.db.execute("SELECT value FROM meta WHERE key='tenant_withheld_at'"
                                      ).fetchone()
        with store.db:
            store.db.execute("DELETE FROM meta WHERE key='tenant_withheld_at'")
        store.close()
        cls.projection = Path(cls.tmp.name) / "ontology.sqlite"
        with MailCaptures(cls.state, internal_domains=SCOPE.internal_domains,
                          exclusion=PeopleExclusion.from_env(ENV)) as mail:
            build_store(cls.projection, schema_loader.load_schema(), {"graph_mail": mail},
                        clock=lambda: NOW,
                        post_project={"mentions": mentions.store_mention_links,
                                      "with_people": people_links_step})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.audience = self.root / "audience.json"
        self.audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                             "audiences": {}}), encoding="utf-8")

    def runtime(self, environ=EXCLUDED_B) -> CaseRuntime:
        runtime = CaseRuntime(self.root / "state", client=None, audience=self.audience,
                              tenant="plx", live=LiveReaders(audience=reader_audience()),
                              mail_scope=MailScope([UPN_A, UPN_B]),
                              people_exclusion=PeopleExclusion.from_env(environ))
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=self.state)
        return runtime

    def server(self, environ=EXCLUDED_B) -> MCPServer:
        runtime = self.runtime(environ)
        tools = CaptureCaseHostTools(runtime, READER_A, store_path=self.projection,
                                     mail_capture_dir=self.state,
                                     marking_grants={"pii": READER_A.principals})
        server = MCPServer(case_runtime=runtime, actor=READER_A, ontology=tools)
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "participants-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def call(self, server, name, arguments) -> dict:
        result = _client(server, name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def assert_only_cleared(self, answer, *, cleared_shown=True):
        text = json.dumps(answer)
        for name in HIDDEN:
            self.assertNotIn(SUBJECTS[name], text, name)
            self.assertNotIn(MESSAGES[name], text, name)
        self.assertNotIn(UNDECIDED, text)
        if cleared_shown:
            self.assertIn(MESSAGES["cleared"], text)

    def emails(self, server) -> dict[str, str]:
        """Every Email object ``server``'s reader finds, with its candidate as text."""
        found = self.call(server, "plx.find", EMAIL_LIST)
        return {c["object"]["object_id"]: json.dumps(c) for c in found["candidates"]}

    # ------------------------------------------------------------------ the gate

    def test_the_item_gate_serves_a_message_only_when_every_participant_is_cleared(self):
        # Fails on the old gate: it decided the mailbox and the sender only, so both
        # copies were served.
        gate = item_gate_from_case_runtime(self.runtime())
        self.assertEqual({name: gate(READER_A, handle)
                          for name, handle in self.handles.items()},
                         {"cleared": True, "undecided": False, "excluded": False})
        # Before the finance owner's exclusion only the undecided copy is withheld.
        gate = item_gate_from_case_runtime(self.runtime(ENV))
        self.assertEqual({name: gate(READER_A, handle)
                          for name, handle in self.handles.items()},
                         {"cleared": True, "undecided": False, "excluded": True})
        # No usable list: nothing is served at all.
        gate = item_gate_from_case_runtime(self.runtime({}))
        self.assertFalse(any(gate(READER_A, h) for h in self.handles.values()))

    def test_the_participant_rule_fails_closed_on_what_it_cannot_decide(self):
        from operating.excluded_people import participants_withheld
        from operating.people_graph import PeopleDirectory
        runtime = self.runtime(ENV)
        store = MailCaptureStore(self.state, clock=Clock())
        self.addCleanup(store.close)
        directory = PeopleDirectory.from_store(runtime, store)
        rule = lambda *named, **kw: participants_withheld(directory, list(named), **kw)
        self.assertFalse(rule(SUPPLIER, UPN_A, BOX_A))
        for bad in (UNDECIDED, "not-an-address@", "a@b@c.example"):
            with self.subTest(bad=bad):
                self.assertTrue(rule(SUPPLIER, bad))
        self.assertTrue(rule(UPN_A, sender=[], sender_required=True))   # no sender
        # ... unless the mailbox it was read from stands for its sender: a cleared owner
        # clears it, an excluded or malformed one does not
        self.assertFalse(rule(UPN_A, sender=[], sender_required=True, mailbox=BOX_A))
        self.assertTrue(rule(UPN_A, sender=[], sender_required=True, mailbox="not-an-id"))
        excluded = PeopleDirectory.from_store(self.runtime(), store)
        self.assertTrue(participants_withheld(excluded, [UPN_A], sender=[],
                                              sender_required=True, mailbox=BOX_B))
        self.assertTrue(rule(SUPPLIER, sender={"name": "no address"}, sender_required=True))
        # a sender that is there but names no identity (a display name) may be anyone's: the
        # mailbox never stands in for it (only a message with no sender recorded at all)
        self.assertTrue(rule(UPN_A, sender=["Accounts Payable"], sender_required=True,
                             mailbox=BOX_A))
        self.assertTrue(participants_withheld(None, [SUPPLIER]))
        unusable = PeopleDirectory(PeopleExclusion(unavailable="excluded_people_file_unreadable"))
        self.assertTrue(participants_withheld(unusable, [SUPPLIER]))

    def test_a_withheld_message_is_never_read_live_in_its_place(self):
        # Fails on the old code twice over: the capture served the copies, and a message
        # the capture refused fell through to the live reader (another guard's decision).
        from unittest import mock

        from operating.ontology_tools_evidence import RuntimeCitations
        runtime = self.runtime()
        runtime.live.mail = mock.Mock()
        reader = RuntimeCitations(runtime, mail_capture_dir=self.state)
        with mock.patch.object(RuntimeCitations, "_read_live",
                               side_effect=AssertionError("read live")):
            for name in HIDDEN:
                read = reader.read(self.handles[name], READER_A)
                self.assertEqual((read["status"], read["reason"]),
                                 ("withheld", "participants_not_cleared"), name)
                self.assertEqual([g["reason"] for g in read["gaps"]],
                                 ["participants_not_cleared"])
                self.assert_only_cleared(read, cleared_shown=False)
                with self.assertRaises(CitationUnresolvable):
                    runtime.resolve_citation(self.handles[name], READER_A)
            self.assertEqual(reader.read(self.handles["cleared"], READER_A)["status"],
                             "resolved")
        self.assertEqual(runtime.live.mail.method_calls, [])

    # ------------------------------------------------------------------ every tool

    def test_find_object_and_read_show_nothing_of_an_uncleared_message(self):
        # Fails on the old code: find listed all three, object opened them, read served
        # their subjects.
        every = self.emails(self.server(ENV))   # the finance owner not yet excluded
        server = self.server()
        found = self.call(server, "plx.find", EMAIL_LIST)
        self.assertEqual(len(found["candidates"]), 1, found)
        self.assert_only_cleared(found)
        hidden = [oid for oid, text in every.items()
                  if SUBJECTS["excluded"] in text or MESSAGES["excluded"] in text]
        for object_id in hidden:
            result = _client(server, "plx.object", {"object_id": object_id})
            self.assertEqual(result["structuredContent"]["error"]["code"], "object_not_found")
            self.assert_only_cleared(result["structuredContent"], cleared_shown=False)
        for name in HIDDEN:
            read = dict(_client(server, "plx.read", {"citation": self.handles[name]}
                                )["structuredContent"])
            self.assertNotEqual(read.get("status"), "resolved", name)
            self.assertNotIn("content", read, name)
            read.pop("citation", None)   # the handle the caller asked about, echoed back
            self.assert_only_cleared(read, cleared_shown=False)
        read = self.call(server, "plx.read", {"citation": self.handles["cleared"]})
        self.assertEqual(read["status"], "resolved")
        # free text search over the captured messages (the search gate)
        text = self.call(server, "plx.find", {"query": "copy note"})
        self.assert_only_cleared(text, cleared_shown=False)
        # the people tools: a person known only from a withheld copy is no one
        for address in (UNDECIDED, UPN_B):
            person = 'Person:graph_mail.people:{"person_key":"%s"}' % address_key(address)
            found = _client(server, "plx.object", {"object_id": person})["structuredContent"]
            self.assertEqual(found["error"]["code"], "object_not_found", address)

    def test_around_aggregate_and_query_count_only_cleared_mail(self):
        # Fails on the old code: the supplier's sent mail counted and showed three.
        server = self.server()
        people = ['Person:graph_mail.people:{"person_key":"%s"}' % address_key(SUPPLIER)]
        around = self.call(server, "plx.around", {
            "from": people[0], "response_format": "detailed",
            "path": [{"link_type": "Email.sent_by_person", "direction": "reverse"}]})
        self.assert_only_cleared(around)
        counts = self.call(server, "plx.object", {"object_id": people[0]}).get(
            "linked_counts", [])
        self.assertEqual(sum(c["count"] for c in counts
                             if c["link_type"] == "Email.sent_by_person"), 1, counts)
        aggregate = self.call(server, "plx.aggregate", {
            "object_type": "Email", "measure": {"op": "count"}})
        self.assert_only_cleared(aggregate, cleared_shown=False)
        self.assertIn(1, _numbers(aggregate), aggregate)
        self.assertNotIn(3, _numbers(aggregate), aggregate)
        # plx.query composes find, around and aggregate (the host's own call: the agent
        # surface does not list it)
        query = server.ontology.call("plx.query", {"from": {"object_type": "Email"},
                                                   "measures": [{"op": "count"}]}, READER_A)
        self.assertNotIn("error", query, query)
        self.assert_only_cleared(query, cleared_shown=False)
        self.assertIn(1, _numbers(query), query)
        self.assertNotIn(3, _numbers(query), query)

    def test_a_person_excluded_since_withholds_at_once_whatever_was_decided_before(self):
        # The traversal shares gate results between the hosts of one process for minutes
        # (``ontology_tools_traverse``'s slice gate): keyed by the exclusion too, the
        # copy to the finance owner leaves plx.around as soon as the owner is excluded.
        supplier = 'Person:graph_mail.people:{"person_key":"%s"}' % address_key(SUPPLIER)
        args = {"from": supplier, "response_format": "detailed",
                "path": [{"link_type": "Email.sent_by_person", "direction": "reverse"}]}
        before = json.dumps(self.call(self.server(ENV), "plx.around", args))
        self.assertIn(MESSAGES["excluded"], before)
        self.assertNotIn(MESSAGES["undecided"], before)
        self.assert_only_cleared(self.call(self.server(), "plx.around", args))

    def test_one_to_sixty_messages_lose_no_cleared_row_and_keep_no_uncleared_one(self):
        # Cardinality probe of the per-message decision: of n cleared and n uncleared
        # messages a reader is served exactly the n cleared ones.
        runtime = self.runtime(ENV)
        from operating.ontology_tools_evidence import RuntimeCitations
        reader = RuntimeCitations(runtime, mail_capture_dir=self.state)
        store = MailCaptureStore(self.state, clock=Clock())
        self.addCleanup(store.close)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                cleared = [reader.participants_withheld(store, [SUPPLIER], [UPN_A, f"x{i}@o.example"])
                           for i in range(n)]
                uncleared = [reader.participants_withheld(store, [SUPPLIER],
                                                          [UPN_A, f"p{i}@example-plx.com"])
                             for i in range(n)]
                self.assertEqual((cleared.count(False), uncleared.count(True)), (n, n))


def _numbers(value) -> list:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _numbers(v)]
    if isinstance(value, list):
        return [n for v in value for n in _numbers(v)]
    return []


class TenantRecordTests(unittest.TestCase):
    """The tenant's uncleared addresses (``people_exclusion.tenant_withheld_rows``): with a
    recent record an address the tenant does not hold at all (a former employee's) is no
    current person of ours and withholds nothing; one the record holds stays withheld; no
    record, a stale one or one of another listing decides nothing (fail closed)."""

    PARTNER, GONE = "11111111-1111-4111-8111-111111111111", "gone.person@example-plx.com"

    def guard(self, *, groups=True):
        from datetime import datetime, timezone
        guard = PeopleExclusion([self.PARTNER])
        guard._bound = True
        guard._listing = [(self.PARTNER, "p@example-plx.com", ["p@example-plx.com",
                                                                "alias.p@example-plx.com"]),
                          ("22222222-2222-4222-8222-222222222222", "a@example-plx.com",
                           ["a@example-plx.com", "shared@example-plx.com"]),
                          ("33333333-3333-4333-8333-333333333333", "b@example-plx.com",
                           ["b@example-plx.com", "shared@example-plx.com"])]
        guard._addresses = frozenset({"p@example-plx.com", "alias.p@example-plx.com"})
        guard._listing_at = datetime.now(timezone.utc)
        guard._groups = {"team@example-plx.com": False, "all@example-plx.com": True} \
            if groups else None
        return guard

    def test_the_record_holds_every_uncleared_address_and_no_cleared_one(self):
        from integrations.people_exclusion import address_digest, tenant_withheld_rows
        self.assertIsNone(tenant_withheld_rows(self.guard(groups=False)))
        self.assertIsNone(tenant_withheld_rows(PeopleExclusion([self.PARTNER])))  # unbound
        _at, rows = tenant_withheld_rows(self.guard())
        kinds = dict(rows)
        self.assertEqual(kinds[address_digest("p@example-plx.com")], "excluded")
        self.assertEqual(kinds[address_digest("alias.p@example-plx.com")], "excluded")
        for address in ("shared@example-plx.com", "team@example-plx.com",
                        "all@example-plx.com"):
            self.assertEqual(kinds[address_digest(address)], "uncleared", address)
        for address in ("a@example-plx.com", "b@example-plx.com", self.GONE):
            self.assertNotIn(address_digest(address), kinds, address)
        self.assertNotIn("p@example-plx.com", json.dumps(rows))   # digests only

    def test_the_directory_decides_by_a_recent_record_of_this_listing_only(self):
        from datetime import datetime, timedelta, timezone
        from integrations.people_exclusion import mail_capture_tenant_withheld
        from operating.excluded_people import participants_withheld
        from operating.people_graph import PERSON, UNDECIDED, WITHHELD, PeopleDirectory
        with tempfile.TemporaryDirectory() as tmp:
            store = MailCaptureStore(Path(tmp), clock=Clock())
            self.addCleanup(store.close)
            self.assertGreater(store.record_tenant_listing(self.guard()), 0)
            # the excluded person's own mailbox is not captured: their alias history is
            # not known, so an address no listing holds decides nothing yet
            self.assertIsNone(mail_capture_tenant_withheld(tmp))
            # captured, with a sent message from an old alias no listing names any more
            with store.db:
                store.db.execute("INSERT INTO mailboxes VALUES (?, ?, '[]', '[]', '{}', ?, ?)",
                                 (self.PARTNER, "p@example-plx.com", "2026-11-01T00:00:00Z",
                                  "2026-11-01T00:00:00Z"))
                store.db.execute("INSERT INTO folders (mailbox_id, folder_key, folder_id, "
                                 "status) VALUES (?, 'sentitems', 'F-SENT', 'complete')",
                                 (self.PARTNER,))
                store.db.execute(
                    "INSERT INTO messages (l1_handle, mailbox_id, message_id, change_key, "
                    "folder_id, record, digest, body_status, recorded_at, last_seen_at) VALUES "
                    "('h-old', ?, 'M-OLD', 'CK', 'F-SENT', ?, 'd', 'none', 'x', 'x')",
                    (self.PARTNER, json.dumps({"from": "old.p@example-plx.com"})))
            self.assertGreater(store.record_tenant_listing(self.guard()), 0)
            recorded = mail_capture_tenant_withheld(tmp)
            self.assertIsNotNone(recorded)
            later = datetime.now(timezone.utc) + timedelta(days=2)
            self.assertIsNone(mail_capture_tenant_withheld(tmp, now=later))   # stale
            offline = PeopleExclusion([self.PARTNER])

            def state(address, record):
                return PeopleDirectory(offline, internal_domains=["example-plx.com"],
                                       tenant_withheld=record).address(address).state
            self.assertEqual(state(self.GONE, recorded), PERSON)
            self.assertEqual(state(self.GONE, None), UNDECIDED)
            self.assertEqual(state("all@example-plx.com", recorded), UNDECIDED)
            self.assertEqual(state("shared@example-plx.com", recorded), UNDECIDED)
            self.assertEqual(state("alias.p@example-plx.com", recorded), WITHHELD)
            # an alias they sent from once is theirs, not an address nobody holds
            self.assertEqual(state("old.p@example-plx.com", recorded), WITHHELD)
            directory = PeopleDirectory(offline, internal_domains=["example-plx.com"],
                                        tenant_withheld=recorded)
            self.assertFalse(participants_withheld(directory, [self.GONE],
                                                   sender=["x@outside.example"]))
            self.assertTrue(participants_withheld(directory, [self.GONE, "p@example-plx.com"],
                                                  sender=["x@outside.example"]))
            # a later listing recorded without a groups listing: the old record no longer
            # matches the listing's time and decides nothing
            with store.db:
                store.db.execute("UPDATE meta SET value='2000-01-01T00:00:00Z' "
                                 "WHERE key='tenant_listing_at'")
            self.assertIsNone(mail_capture_tenant_withheld(tmp))

    def test_the_gate_serves_an_unheld_address_with_the_record_and_never_the_excluded(self):
        case = MailParticipantsClearedTests
        case.setUpClass()
        self.addCleanup(case.tearDownClass)
        self.assertIsNotNone(case.record)   # the capture's bound guard recorded it
        db = MailCaptureStore(case.state, clock=Clock())
        self.addCleanup(db.close)
        with db.db:
            db.db.execute("INSERT INTO meta VALUES ('tenant_withheld_at', ?)", case.record)
            # as a store whose excluded people's own mailboxes are captured (their alias
            # history complete: ``MailCaptureStore._alias_history``)
            db.db.execute("INSERT INTO meta VALUES ('tenant_alias_history', 'complete') ON "
                          "CONFLICT(key) DO UPDATE SET value = excluded.value")
        probe = case("test_the_item_gate_serves_a_message_only_when_every_participant_is_"
                     "cleared")
        probe.setUp()
        self.addCleanup(probe.doCleanups)
        gate = item_gate_from_case_runtime(probe.runtime())
        self.assertEqual({name: gate(READER_A, handle) for name, handle in case.handles.items()},
                         {"cleared": True, "undecided": True, "excluded": False})


if __name__ == "__main__":
    unittest.main()
