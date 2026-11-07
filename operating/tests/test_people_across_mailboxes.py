"""People seen across mailboxes (``ontology.people_directory``): a Person is visible to a
reader of any captured mailbox the person corresponds in (from, to or cc), and each
reader sees only the messages and links of the mailboxes it may read; a reader of no
mailbox sees no one, a reader of every mailbox sees everyone, and the people exclusion
(``PLX_EXCLUDED_PEOPLE_OIDS``) still keeps an excluded person's mailbox out of every
grant. Evidence label: ``isolated-fixture`` / ``injected-transport`` (the capture tests'
in-process FakeGraph). Every id and address is made up; no network, no credentials."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, OUTSIDER,
                                                   READER_A, SCOPE, UPN_A, UPN_B, Clock,
                                                   FakeGraph, capture_audience,
                                                   reader_audience)
from ontology import mentions
from ontology import people_directory as pd
from ontology import schema as schema_loader
from ontology.people_links import people_links_step
from ontology.project import MailCaptures, build_store
from ontology.types import Actor
from operating.case_mail_scope import MailScope
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaptureCaseHostTools, CaseCaptures
from operating.mcp_server import MCPServer
from operating.tests.test_case_tools_host import NOW, _client

PII_HOLDERS = frozenset({"staff:orders", "staff:finance", "staff:outsider"})
READER_B = Actor("plx", frozenset({"staff:finance"}), "restricted")
BOTH = Actor("plx", frozenset({"staff:orders", "staff:finance"}), "restricted")
SHARED = "supplier@outside.example"     # writes to both mailboxes (FakeGraph's sender)
ONLY_A = "carol@client-one.example"     # writes to mailbox A only
ONLY_B = "dave@client-two.example"      # writes to mailbox B only
REPLIED = "erin@client-three.example"   # writes to B; A writes to her (a recipient in A)


def _from(graph: FakeGraph, user: str, mid: str, sender: str, to: str | None = None) -> None:
    message = graph.messages[user][mid]
    message["from"] = {"emailAddress": {"address": sender}}
    if to is not None:
        message["toRecipients"] = [{"emailAddress": {"address": to}}]


class PeopleAcrossMailboxesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "mail"
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-A1", "CK-A1", subject="Shared supplier to A")
        graph.put(BOX_A, "MSG-A2", "CK-A2", subject="Carol to A")
        _from(graph, BOX_A, "MSG-A2", ONLY_A)
        graph.put(BOX_A, "MSG-A3", "CK-A3", subject="A writes to Erin")
        _from(graph, BOX_A, "MSG-A3", UPN_A, to=REPLIED)
        graph.put(BOX_B, "MSG-B1", "CK-B1", subject="Shared supplier to B")
        graph.put(BOX_B, "MSG-B2", "CK-B2", subject="Dave to B")
        _from(graph, BOX_B, "MSG-B2", ONLY_B)
        graph.put(BOX_B, "MSG-B3", "CK-B3", subject="Erin to B")
        _from(graph, BOX_B, "MSG-B3", REPLIED)
        store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
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

    def server(self, actor, exclusion=None) -> MCPServer:
        runtime = CaseRuntime(self.root / "state", client=None, audience=self.audience,
                              tenant="plx", live=LiveReaders(audience=reader_audience()),
                              mail_scope=MailScope.unrestricted(),
                              people_exclusion=exclusion or PeopleExclusion.from_env(ENV))
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=self.state)
        tools = CaptureCaseHostTools(runtime, actor, store_path=self.projection,
                                     mail_capture_dir=self.state,
                                     marking_grants={"pii": PII_HOLDERS})
        server = MCPServer(case_runtime=runtime, actor=actor, ontology=tools)
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "people-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def visible(self, actor, exclusion=None) -> set[str]:
        """The addresses (or staff mailboxes) of the Persons ``actor`` may open."""
        server = self.server(actor, exclusion)
        out = set()
        for name, object_id in PEOPLE.items():
            result = _client(server, "plx.object", {"object_id": object_id})
            content = result["structuredContent"]
            if "error" in content:
                self.assertEqual(content["error"]["code"], "object_not_found", content)
                continue
            self.assertEqual(content["object"]["object_id"], object_id)
            out.add(name)
        return out

    def around(self, actor, exclusion=None) -> tuple[int, str]:
        """(how many messages of the shared supplier ``actor`` is shown, the answer)."""
        server = self.server(actor, exclusion)
        result = _client(server, "plx.around", {
            "from": PEOPLE[SHARED],
            "path": [{"link_type": "Email.sent_by_person", "direction": "reverse"}],
            "response_format": "detailed"})
        self.assertFalse(result["isError"], result)
        text = json.dumps(result["structuredContent"])
        counts = _client(server, "plx.object", {"object_id": PEOPLE[SHARED]}
                         )["structuredContent"].get("linked_counts", [])
        sent = sum(c["count"] for c in counts if c["link_type"] == "Email.sent_by_person")
        return sent, text

    def test_each_reader_sees_the_people_its_own_mailbox_corresponds_with(self):
        # Erin writes to B and A only writes to her: B's readers see her, A's do not
        # (that she is a contact at all is a fact of B's mail); Carol and Dave only
        # through their own mailbox; each staff person through their own mailbox.
        self.assertEqual(self.visible(READER_A), {SHARED, ONLY_A, BOX_A})
        self.assertEqual(self.visible(READER_B), {SHARED, ONLY_B, REPLIED, BOX_B})

    def test_each_reader_sees_only_its_own_messages_of_a_shared_person(self):
        sent_a, text_a = self.around(READER_A)
        sent_b, text_b = self.around(READER_B)
        self.assertEqual((sent_a, sent_b), (1, 1))
        self.assertIn("MSG-A1", text_a)
        for hidden in ("MSG-B", "to B"):          # no message, subject or count of B's
            self.assertNotIn(hidden, text_a)
        self.assertIn("MSG-B1", text_b)
        for hidden in ("MSG-A", "to A"):
            self.assertNotIn(hidden, text_b)
        sent_both, text_both = self.around(BOTH)
        self.assertEqual(sent_both, 2)
        self.assertIn("MSG-A1", text_both)
        self.assertIn("MSG-B1", text_both)

    def test_a_reader_of_no_mailbox_sees_no_one(self):
        self.assertEqual(self.visible(OUTSIDER), set())
        server = self.server(OUTSIDER)
        granted = server.ontology.policy_now(OUTSIDER).audiences
        self.assertFalse(any(principals for key, principals in granted.items()
                             if key.startswith(("mail:", pd.COMPOSITE))))
        sent, text = self.around(OUTSIDER)
        self.assertEqual(sent, 0)
        self.assertNotIn("MSG-", text)

    def test_a_reader_of_every_mailbox_sees_everyone(self):
        self.assertEqual(self.visible(BOTH), set(PEOPLE))

    def test_a_person_citation_never_reads_live_mail(self):
        # A person citation the capture does not serve this reader is withheld or
        # unresolvable from the capture alone: never a live mail read.
        server = self.server(READER_B)
        handle = pd.person_handle(BOX_A, pd.address_key(ONLY_A))
        with mock.patch("operating.ontology_tools_evidence.RuntimeCitations._read_live",
                        side_effect=AssertionError("live read")):
            read = _client(server, "plx.read", {"citation": handle})["structuredContent"]
        self.assertEqual(read.get("status"), "withheld")
        self.assertNotIn("client-one", json.dumps(read))

    def test_a_handle_naming_another_mailbox_reveals_nothing(self):
        # Anyone can build a person handle naming any mailbox (the key is the sha256 of
        # an address). A reader of B gets the same answer whatever mailbox it names, and
        # that answer is decided on B's mail alone: Carol, who wrote only to A, and Erin,
        # whom only A wrote to, are withheld from A's reader either way.
        def answers(actor, address):
            server = self.server(actor)
            key = pd.address_key(address)
            out = set()
            for named in (BOX_A, BOX_B, pd.PEOPLE_HOST):
                read = _client(server, "plx.read", {
                    "citation": f"graph-mail://{named}/person/{key}"})["structuredContent"]
                out.add(read.get("status"))
            return out
        self.assertEqual(answers(READER_B, ONLY_A), {"withheld"})
        self.assertEqual(answers(READER_A, REPLIED), {"withheld"})
        self.assertEqual(answers(READER_A, ONLY_B), {"withheld"})
        self.assertEqual(len(answers(READER_B, SHARED)), 1)
        self.assertNotIn("withheld", answers(READER_B, SHARED))
        self.assertNotIn("withheld", answers(READER_B, REPLIED))
        # a staff handle is only its own: one naming another mailbox is nobody
        server = self.server(BOTH)
        read = _client(server, "plx.read", {
            "citation": f"graph-mail://{BOX_A}/person/{BOX_B}"})["structuredContent"]
        self.assertEqual(read.get("status"), "withheld")

    def test_an_outside_contact_is_cited_by_no_mailbox(self):
        # A mailbox id is an employee's Entra id: an outside contact's citations and
        # every value origin name none, for every reader.
        for actor in (READER_A, READER_B, BOTH):
            server = self.server(actor)
            for name in (SHARED, ONLY_A, ONLY_B, REPLIED):
                content = _client(server, "plx.object", {"object_id": PEOPLE[name]}
                                  )["structuredContent"]
                if "error" in content:
                    continue
                text = json.dumps(content)
                self.assertIn(f"graph-mail://{pd.PEOPLE_HOST}/person/", text)
                for box in (BOX_A, BOX_B):
                    self.assertNotIn(box, text, (actor, name))
        import sqlite3
        conn = sqlite3.connect(f"file:{self.projection}?mode=ro", uri=True)
        try:
            handles = [r[0] for r in conn.execute(
                "SELECT DISTINCT l1_handle FROM object_values WHERE object_id LIKE "
                "'Person:graph_mail.people:%'")]
        finally:
            conn.close()
        self.assertEqual(len(handles), len(PEOPLE))
        for handle in handles:
            mailbox, key = pd.parse_person_handle(handle)
            self.assertEqual(mailbox, key if mailbox is not None else None, handle)

    def test_a_person_is_decided_on_the_readers_mailboxes_alone(self):
        # Resolving a person never reads a mailbox the reader may not see, nor one the
        # exclusion withholds, nor the mailbox a handle names.
        from integrations.tests.test_capture_mail import reader_audience as audience_of
        excluded = PeopleExclusion.from_env({**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_B})
        store = MailCaptureStore(self.state, clock=Clock())
        self.addCleanup(store.close)
        seen: list[str] = []
        store.db.set_trace_callback(seen.append)
        cases = [(READER_A, None, ONLY_B), (READER_A, None, SHARED),
                 (BOTH, excluded, SHARED), (BOTH, excluded, REPLIED)]
        for actor, guard, address in cases:
            for named in (BOX_A, BOX_B, pd.PEOPLE_HOST):
                store.resolve(f"graph-mail://{named}/person/{pd.address_key(address)}",
                              actor, audience_of(), people=guard)
            store.resolve(f"graph-mail://{BOX_B}/person/{BOX_B}", actor, audience_of(),
                          people=guard)
        self.assertTrue([q for q in seen if "messages" in q and BOX_A in q])  # traced
        reads = [q for q in seen if "messages" in q and BOX_B in q]
        self.assertEqual(reads, [])

    def test_the_people_exclusion_still_holds(self):
        # B's mailbox excluded now: its owner and the contacts known only from mail there
        # vanish for every reader (Erin wrote only to B: what A sent her is no evidence
        # that she is a contact once B's mail is out); the supplier who wrote to A too
        # stays visible through A alone.
        excluded = PeopleExclusion.from_env({**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_B})
        self.assertEqual(self.visible(READER_B, excluded), set())
        self.assertEqual(self.visible(READER_A, excluded), {SHARED, ONLY_A, BOX_A})
        self.assertEqual(self.visible(BOTH, excluded), {SHARED, ONLY_A, BOX_A})
        sent, text = self.around(BOTH, excluded)
        self.assertEqual(sent, 1)
        self.assertNotIn("MSG-B", text)
        server = self.server(READER_B, excluded)
        granted = server.ontology.policy_now(READER_B).audiences
        self.assertFalse(any(principals for key, principals in granted.items()
                             if key.startswith(("mail:", pd.COMPOSITE))))
        # no usable exclusion list: nobody is seen at all
        undecided = PeopleExclusion(unavailable="excluded_people_oids_absent")
        self.assertEqual(self.visible(BOTH, undecided), set())


PEOPLE: dict[str, str] = {}   # address (or staff mailbox) -> Person object id


def _person_id(key: str) -> str:
    return 'Person:graph_mail.people:{"person_key":"%s"}' % key


PEOPLE.update({SHARED: _person_id(pd.address_key(SHARED)),
               ONLY_A: _person_id(pd.address_key(ONLY_A)),
               ONLY_B: _person_id(pd.address_key(ONLY_B)),
               REPLIED: _person_id(pd.address_key(REPLIED)),
               BOX_A: _person_id(BOX_A), BOX_B: _person_id(BOX_B)})


class CompositeAudienceTests(unittest.TestCase):
    """The ``mail-any:`` key: short at any mailbox count, decoded only against the same
    mailbox list, and refused (fail closed) on every other input."""

    @staticmethod
    def ids(n: int) -> list[str]:
        return [f"00000000-0000-4000-8000-{i:012x}" for i in range(n)]

    def test_keys_name_exactly_their_mailboxes_at_any_count(self):
        for n in (1, 10, 60, 166):
            every = self.ids(n)
            with self.subTest(n=n):
                key = pd.audience_key(every, every)
                if n == 1:
                    self.assertEqual(key, f"mail:{every[0]}")
                    continue
                self.assertLessEqual(len(key), 128)      # a Postgres index entry
                self.assertEqual(pd.decode_audience(key, every), tuple(sorted(every)))
                some = every[1::3]
                if len(some) > 1:
                    self.assertEqual(pd.decode_audience(pd.audience_key(some, every), every),
                                     tuple(sorted(some)))

    def test_a_key_that_no_longer_decodes_opens_nothing(self):
        every = self.ids(10)
        key = pd.audience_key(every[2:5], every)
        # a mailbox added before them in the sorted list shifts every bit: the check fails
        moved = sorted(every + ["00000000-0000-0000-0000-000000000000"])
        refused = [key.replace(".", "0."),                      # another bitmap
                   key[:-1] + ("0" if key[-1] != "0" else "1"),  # another check
                   key + "0", "mail-any:", "mail-any:*", "mail-any:ff.", None, 7,
                   f"mail:{every[0]}"]
        self.assertIsNone(pd.decode_audience(key, moved))
        # one added after them leaves their bits: the same mailboxes
        self.assertEqual(pd.decode_audience(key, every + ["z"]), tuple(every[2:5]))
        for bad in refused:
            with self.subTest(key=bad):
                self.assertIsNone(pd.decode_audience(bad, every))
        self.assertIsNone(pd.decode_audience(key, every[:3]))   # a bit past the list
        with self.assertRaises(ValueError):
            pd.audience_key([], every)
        with self.assertRaises(ValueError):
            pd.audience_key(["not-held", every[0]], every)

    def test_the_search_scope_grants_a_composite_only_as_the_policy_decides(self):
        from operating.ontology_tools import ReadPolicy
        from operating.pg.permissions import scope_for
        every = self.ids(3)
        key = pd.audience_key(every[:2], every)
        policy = ReadPolicy("plx", {key: frozenset({"staff:orders"}),
                                    "mail-any:zz.0000000000000000": frozenset({"staff:orders"})})
        exclusion = PeopleExclusion([every[2]])
        scope = scope_for(policy, READER_A, [key, "mail-any:zz.0000000000000000"],
                          exclusion=exclusion)
        self.assertEqual(scope.allowed_audience_keys, (key,))   # off-grammar: dropped
        self.assertEqual(scope_for(policy, OUTSIDER, [key], exclusion=exclusion)
                         .allowed_audience_keys, ())
        unavailable = PeopleExclusion(unavailable="excluded_people_oids_absent")
        self.assertEqual(scope_for(policy, READER_A, [key], exclusion=unavailable)
                         .allowed_audience_keys, ())


if __name__ == "__main__":
    unittest.main()


class SenderLookupAtScaleTests(unittest.TestCase):
    """An outside contact's page is decided by an index probe per reader mailbox, never a
    scan of the reader's mail: 166 mailboxes of synthetic mail, every decision under
    50 ms, the same answers before the stored rows are keyed (read the slow way) and
    after, and a decision kept per reader only while the store is unchanged."""
    BOXES, PER_BOX = 166, 1200

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.guard = PeopleExclusion.from_env(ENV)
        cls.store = MailCaptureStore(Path(cls.tmp.name) / "mail", clock=Clock(),
                                     people=cls.guard)
        cls.ids = [f"12345678-aaaa-bbbb-cccc-{i:012x}" for i in range(0x100, 0x100 + cls.BOXES)]
        cls.upns = [f"box{i}@example-plx.com" for i in range(cls.BOXES)]
        for oid, upn in zip(cls.ids, cls.upns):
            cls.store.record_mailbox({"object_id": oid, "upn": upn, "identities": [upn],
                                      "resources": [f"mail:{upn}"],
                                      "source_acl": {"status": "not_required"}})
        cls.only_last = "late@far-supplier.example"     # writes to the last mailbox only
        cls.only_7 = "seven@client.example"             # writes to mailbox 7 only
        cls.recipient = "cc-only@client.example"        # only ever written to (mailbox 3)
        rows = []
        for b, oid in enumerate(cls.ids):
            for n in range(cls.PER_BOX):
                sender = f"noise{n % 300}-{b}@bulk{n % 7}.example"
                to = [cls.upns[b]]
                if b == cls.BOXES - 1 and n == cls.PER_BOX - 1:
                    sender = cls.only_last
                elif b == 7 and n == 5:
                    sender = cls.only_7
                elif b == 3 and n == 9:
                    to = [cls.recipient]
                record = json.dumps({"from": sender, "to": to, "subject": "s"})
                rows.append((f"graph-mail://{oid}/message/M{n}@CK", oid, f"M{n}", "CK",
                             "F", record, "d", "complete", "2026-10-01T00:00:00Z",
                             "2026-10-01T00:00:00Z"))
        with cls.store.db:   # as a store captured before the sender key: rows unkeyed
            cls.store.db.executemany(
                "INSERT INTO messages (l1_handle, mailbox_id, message_id, change_key, "
                "folder_id, record, digest, body_status, recorded_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        cls.owner = Actor("plx", frozenset({"staff:owner"}), "restricted")
        cls.reader7 = Actor("plx", frozenset({"staff:seven"}), "restricted")
        cls.reader8 = Actor("plx", frozenset({"staff:eight"}), "restricted")
        grants = {f"mail:{upn}": ["staff:owner"] for upn in cls.upns}
        grants[f"mail:{cls.upns[7]}"].append("staff:seven")
        grants[f"mail:{cls.upns[8]}"].append("staff:eight")
        cls.audience = reader_audience_of(grants)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def handle(self, address):
        return pd.person_handle("people", pd.address_key(address))

    def decide(self, actor, address):
        self.store._person_cache = None   # a fresh decision, never a kept one
        started = time.perf_counter()
        found = self.store.resolve(self.handle(address), actor, self.audience)
        return found, time.perf_counter() - started

    def answers(self):
        return {(name, address): self.decide(actor, address)[0] is not None
                for name, actor in (("owner", self.owner), ("r7", self.reader7),
                                    ("r8", self.reader8))
                for address in (self.only_last, self.only_7, self.recipient,
                                "nobody@never.example")}

    def test_lookup_is_an_index_probe_with_the_same_answers(self):
        before = self.answers()
        expected = {("owner", self.only_last): True, ("owner", self.only_7): True,
                    ("r7", self.only_7): True}
        self.assertEqual({k for k, v in before.items() if v}, set(expected))
        self.assertEqual(self.store.fill_sender_keys(batch=7000)["filled"],
                         self.BOXES * self.PER_BOX)
        self.assertEqual(self.store.fill_sender_keys()["filled"], 0)
        self.assertEqual(self.answers(), before)
        plan = " ".join(r[3] for r in self.store.db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM messages INDEXED BY messages_by_sender "
            "WHERE mailbox_id = ? AND sender_key = ? AND superseded_at IS NULL", ("a", "b")))
        self.assertIn("messages_by_sender", plan)
        slowest = 0.0
        for actor in (self.owner, self.reader7, self.reader8):
            for address in (self.only_last, "nobody@never.example", self.recipient):
                for _ in range(3):
                    slowest = max(slowest, self.decide(actor, address)[1])
        self.assertLess(slowest, 0.05, f"slowest person decision {slowest * 1000:.1f} ms")
        print(f"\nsender lookup, {self.BOXES} mailboxes x {self.PER_BOX} messages: "
              f"slowest decision {slowest * 1000:.1f} ms")

    def test_kept_decision_follows_reader_and_store_changes(self):
        late = "later@new-supplier.example"
        self.store._person_cache = None
        handle = self.handle(late)
        self.assertIsNone(self.store.resolve(handle, self.owner, self.audience))
        self.assertIsNone(self.store.resolve(handle, self.owner, self.audience))  # kept
        self.store.add_version({"mailbox": self.ids[8], "id": "LATE", "change_key": "CK",
                                "citation": f"graph-mail://{self.ids[8]}/message/LATE@CK",
                                "content_sha256": "0" * 64, "from": late,
                                "to": [self.upns[8]]},
                               folder_id="F", body_status="complete", attachments=[])
        self.assertIsNotNone(self.store.resolve(handle, self.owner, self.audience))
        self.assertIsNotNone(self.store.resolve(handle, self.reader8, self.audience))
        self.assertIsNone(self.store.resolve(handle, self.reader7, self.audience))


def reader_audience_of(grants):
    from integrations.live_source_guard import LiveSourceAudience
    return LiveSourceAudience(grants, tenant="plx", classifications={"mail:*": "restricted"})
