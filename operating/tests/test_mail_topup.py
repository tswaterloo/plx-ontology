"""Mail newer than the served projection (``operating/mail_topup.py``) on the case host:
messages captured after the generation was built are listed by ``plx.documents``
behind exactly the projected mail's permission checks, marked newer than the last full
refresh, cited and readable as projected mail is; and the fast path says so.

The capture is filled over the capture tests' in-process FakeGraph, the projection built
from it, then more mail captured. All mailboxes, addresses and texts are synthetic.
Evidence label: isolated-fixture / injected-transport. No network, no credentials.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
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
                                                   READER_A, SCOPE, UPN_B, Clock, FakeGraph,
                                                   capture_audience, reader_audience)
from ontology import mentions
from ontology import schema as schema_loader
from ontology.project import MailCaptures, build_store
from operating import fast_answer as fa
from operating import mail_topup
from operating.case_mail_scope import MailScope
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaptureCaseHostTools, CaseCaptures
from operating.mcp_server import MCPServer
from operating.tests.test_case_tools_host import _client

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)
SUPPLIER = "supplier@outside.example"     # FakeGraph's default sender
OLD = "2026-09-30T08:00:00Z"
NEW = "2026-10-02T09:30:00Z"


def _capture(state: Path, graph: FakeGraph) -> None:
    store = MailCaptureStore(state, clock=Clock())
    api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
        sleep=lambda s: None, jitter=lambda: 0.0))
    run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                      sleep=lambda s: None).run(SCOPE)
    assert run["complete"], run
    store.close()


class NewerMailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "mail"
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-1", "CK-1-1", body="Please confirm our PO100234 by Friday.",
                  subject="Order confirmation PO100234", received=OLD)
        graph.put(BOX_B, "MSG-B1", "CKB-1", body="Finance note on PO100234.",
                  subject="Finance PO100234", received=OLD)
        _capture(cls.state, graph)
        cls.projection = Path(cls.tmp.name) / "ontology.sqlite"
        with MailCaptures(cls.state, internal_domains=SCOPE.internal_domains) as mail:
            build_store(cls.projection, schema_loader.load_schema(), {"graph_mail": mail},
                        clock=lambda: NOW,
                        post_project={"mentions": mentions.store_mention_links})
        # Captured after the generation was built.
        graph.put(BOX_A, "MSG-2", "CK-2-1", body="PO100234 shipped today.",
                  subject="Re: Order confirmation PO100234", received=NEW)
        graph.put(BOX_B, "MSG-B2", "CKB-2", body="Private finance PO100234.",
                  subject="Finance PO100234 private", received=NEW)
        graph.put(BOX_A, "MSG-3", "CK-3-1", body="Internal PO100234 note.",
                  subject="From finance PO100234", received="2026-10-02T10:00:00Z")
        graph.messages[BOX_A]["MSG-3"]["from"] = {"emailAddress": {"address": UPN_B}}
        _capture(cls.state, graph)

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

    def server(self, actor, *, scope=None, exclusion=None) -> MCPServer:
        runtime = CaseRuntime(self.root / "state", client=None, audience=self.audience,
                              tenant="plx", live=LiveReaders(audience=reader_audience()),
                              mail_scope=scope or MailScope.unrestricted(),
                              people_exclusion=exclusion or PeopleExclusion.from_env(ENV))
        self.addCleanup(runtime.close)
        self.runtime = runtime
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=self.state)
        tools = CaptureCaseHostTools(runtime, actor, store_path=self.projection,
                                     mail_capture_dir=self.state)
        server = MCPServer(case_runtime=runtime, actor=actor, ontology=tools)
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "mail-topup-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def search(self, server, **arguments) -> dict:
        arguments.setdefault("sources", ["graph_mail"])
        result = _client(server, "plx.documents", arguments)   # contract-validated
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    @staticmethod
    def listed(answer: dict) -> list[tuple[str, bool]]:
        return [(doc["citation"].split("/")[3].split("@")[0],
                 doc.get(mail_topup.FLAG, False)) for doc in answer["documents"]]

    def test_newer_mail_is_listed_marked_cited_and_readable_for_the_reader(self):
        server = self.server(READER_A)
        for arguments in ({"participant": SUPPLIER}, {"query": "PO100234"},
                          {"participant": SUPPLIER, "query": "100234"}):
            with self.subTest(arguments=arguments):
                answer = self.search(server, **arguments)
                # newest first, only the newer ones marked; MSG-3 is from a colleague
                fresh = [("MSG-3", True)] if "participant" not in arguments else []
                self.assertEqual(self.listed(answer)[:len(fresh) + 1],
                                 fresh + [("MSG-2", True)])
                self.assertNotIn("MSG-B2", json.dumps(answer))
                self.assertIn(mail_topup.SCOPE, answer["searched"]["scopes"])
        fresh = self.search(server, participant=SUPPLIER)["documents"][0]
        self.assertEqual((fresh["direction"], fresh["matched_by"]), ("from_them", "address"))
        self.assertEqual(fresh["title"]["origin"],
                         {"citation": fresh["citation"], "field_path": "row.subject"})
        read = _client(server, "plx.read", {"citation": fresh["citation"]})
        self.assertEqual(read["structuredContent"]["status"], "resolved")
        # A projected message is never listed twice, and nothing older than the pin is new.
        handles = [d["citation"] for d in self.search(server, query="PO100234")["documents"]]
        self.assertEqual(len(handles), len(set(handles)))

    def test_a_mailbox_the_reader_is_not_granted_never_leaks_through_the_top_up(self):
        for actor in (READER_A, OUTSIDER):
            answer = self.search(self.server(actor), query="PO100234")
            self.assertNotIn("MSG-B2", json.dumps(answer))
            self.assertNotIn("MSG-B1", json.dumps(answer))
        self.assertEqual(self.listed(self.search(self.server(OUTSIDER), query="PO100234")),
                         [])

    def test_the_people_exclusion_holds_for_newer_mail_and_fails_closed(self):
        # B excluded: its mailbox is never read, and B's message in A's mailbox is withheld.
        excluded = PeopleExclusion.from_env({**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_B})
        answer = self.search(self.server(READER_A, exclusion=excluded), query="PO100234")
        self.assertIn(("MSG-2", True), self.listed(answer))
        self.assertNotIn("MSG-3", json.dumps(answer))
        self.assertNotIn("MSG-B2", json.dumps(answer))
        # A excluded: nothing of A, projected or newer.
        excluded = PeopleExclusion.from_env({**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_A})
        answer = self.search(self.server(READER_A, exclusion=excluded), query="PO100234")
        self.assertEqual(self.listed(answer), [])
        # The same for an unrestricted reader (owner-open): B's mailbox and B's message
        # in A's mailbox stay withheld, newer or projected.
        import dataclasses
        from operating import employee_permissions
        excluded = PeopleExclusion.from_env({**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_B})
        real = CaptureCaseHostTools.policy_now
        with mock.patch.object(employee_permissions, "reader_unrestricted",
                               return_value=True), \
                mock.patch.object(CaptureCaseHostTools, "policy_now",
                                  lambda host, actor: dataclasses.replace(
                                      real(host, actor), unrestricted=True)):
            server = self.server(READER_A, exclusion=excluded)
            answer = self.search(server, query="PO100234")
            self.assertTrue(server._scope.startswith("unrestricted:"))
        self.assertIn(("MSG-2", True), self.listed(answer))
        for hidden in ("MSG-3", "MSG-B1", "MSG-B2"):
            self.assertNotIn(hidden, json.dumps(answer))
        # A guard that cannot decide serves no captured message at all.
        undecided = PeopleExclusion(unavailable="excluded_people_oids_absent")
        self.assertEqual(self.listed(self.search(self.server(READER_A, exclusion=undecided),
                                                 query="PO100234")), [])

    def test_the_per_item_acl_and_case_mail_scope_hold_for_newer_mail(self):
        server = self.server(READER_A)
        with mock.patch.object(self.runtime, "_employee_acl_denial",
                               return_value="employee_acl_denied"):
            self.assertEqual(self.listed(self.search(server, query="PO100234")), [])
        outside = MailScope(["someone-else@example-plx.com"],
                            internal_domains=["example-plx.com"])
        self.assertEqual(self.listed(self.search(self.server(READER_A, scope=outside),
                                                 query="PO100234")), [])
        # Parity: every newer message listed is one the item gate admits on its own.
        tools = server.ontology.tools
        for doc in self.search(server, query="PO100234")["documents"]:
            self.assertTrue(tools._item_allowed(READER_A, doc["citation"]))

    def test_a_scan_the_time_bound_stops_is_said_never_silent(self):
        ticks = iter(range(0, 1000))
        with mock.patch.object(mail_topup, "BUDGET_SECONDS", 0.5), \
                mock.patch("operating.mail_topup.time.monotonic",
                           side_effect=lambda: float(next(ticks))):
            # top_up's defaults are bound at definition: call through the module.
            server = self.server(READER_A)
            tools = server.ontology.tools
            base = {"documents": [], "gaps": [], "searched": {"sources": ["graph_mail"]},
                    "completeness": {"returned": 0, "total": 0, "complete": True}}
            with server.ontology.request(), server.ontology.per_call():
                tools.policy = server.ontology.policy_now(READER_A)
                out = mail_topup.top_up(
                    tools, {"query": "PO100234"}, READER_A, base,
                    lambda: self.runtime.mail_reader(self.state),
                    clock=lambda: float(next(ticks)), budget=0.5, chunk=1)
        self.assertIn("newer_mail_cut", [g["reason"] for g in out["gaps"]])
        self.assertFalse(out["completeness"]["complete"])

    def test_a_participant_scan_stays_bounded_at_scale(self):
        # N=1/10/60 newer messages: none lost or duplicated in the scan, within the bound.
        for count in (1, 10, 60):
            with self.subTest(count=count):
                db = sqlite3.connect(":memory:")
                db.execute("CREATE TABLE messages (version_id INTEGER PRIMARY KEY, "
                           "l1_handle TEXT, mailbox_id TEXT, message_id TEXT, record TEXT, "
                           "superseded_at TEXT, removed_reason TEXT)")
                for n in range(count):
                    record = {"from": SUPPLIER, "to": ["a@example-plx.com"], "cc": [],
                              "subject": f"m{n}",
                              "received_at": f"2026-10-02T{n // 60:02d}:{n % 60:02d}:00Z"}
                    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,NULL,NULL)",
                               (100 + n, f"graph-mail://mbx/m{n}@ck", "mbx", f"m{n}",
                                json.dumps(record)))
                started = time.monotonic()
                found, cut = mail_topup.scan(
                    db, pin=99, mailboxes=["mbx"], addresses=frozenset({SUPPLIER}),
                    domains=frozenset(), terms=None, since=None, until=None, chunk=7)
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertFalse(cut)
                self.assertEqual(sorted(f["handle"] for f in found),
                                 sorted(f"graph-mail://mbx/m{n}@ck" for n in range(count)))
                # another mailbox's rows are never selected
                none, _ = mail_topup.scan(
                    db, pin=99, mailboxes=["other"], addresses=frozenset({SUPPLIER}),
                    domains=frozenset(), terms=None, since=None, until=None)
                self.assertEqual(none, [])

    def test_a_scan_of_a_large_delta_waits_rather_than_cutting(self):
        # The live delta past a generation's pin can be a whole history backfill (803,000
        # versions on 2026-11-02, 2.7-4.7 s to scan): a scan taking seconds finishes under
        # the default bound, so the answer keeps its newer mail and no newer_mail_cut.
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE messages (version_id INTEGER PRIMARY KEY, l1_handle TEXT, "
                   "mailbox_id TEXT, message_id TEXT, record TEXT, superseded_at TEXT, "
                   "removed_reason TEXT)")
        for n in range(6):
            record = {"from": SUPPLIER, "to": ["a@example-plx.com"], "cc": [],
                      "subject": f"m{n}", "received_at": f"2026-10-02T00:0{n}:00Z"}
            db.execute("INSERT INTO messages VALUES (?,?,?,?,?,NULL,NULL)",
                       (100 + n, f"graph-mail://mbx/m{n}@ck", "mbx", f"m{n}",
                        json.dumps(record)))
        ticks = iter(range(0, 1000))   # each chunk statement "takes" one second
        found, cut = mail_topup.scan(
            db, pin=99, mailboxes=["mbx"], addresses=frozenset({SUPPLIER}),
            domains=frozenset(), terms=None, since=None, until=None,
            clock=lambda: float(next(ticks)), chunk=1)
        self.assertFalse(cut)
        self.assertEqual(len(found), 6)


class InputsTests(unittest.TestCase):
    def test_an_unknown_or_malformed_pin_tops_up_nothing(self):
        for value in (None, "", "not json", "[]", json.dumps({"graph_mail": {}}),
                      json.dumps({"graph_mail": {"messages": {"last_capture_id": "7"}}}),
                      json.dumps({"graph_mail": {"messages": {"last_capture_id": True}}})):
            self.assertIsNone(mail_topup.pinned_capture_id(value), value)
        self.assertEqual(mail_topup.pinned_capture_id(json.dumps(
            {"graph_mail": {"messages": {"last_capture_id": 7}}})), 7)

    def test_a_paged_object_or_time_travel_search_is_left_as_it_was(self):
        result = {"documents": [], "gaps": [], "searched": {"sources": ["graph_mail"]},
                  "completeness": {}}
        for arguments in ({"query": "x", "cursor": "c"}, {"object_id": "Email:x"},
                          {"query": "x", "as_known_at": "2026-01-01"},
                          {"query": "x", "sources": ["sharepoint"]},
                          {"participant": "a@b.example"}):   # no participant resolved
            self.assertIs(mail_topup.top_up(object(), arguments, None, result,
                                            lambda: None), result)


class FastPathTests(unittest.TestCase):
    class Reads:
        def row(self, handle):
            return {"citation": handle, "row.received_at": "2026-10-02T09:30:00Z",
                    "row.subject": "Re: shipped"}

    def test_the_latest_message_says_it_is_newer_than_the_last_full_refresh(self):
        found = [{"kind": "email", "date": "2026-10-02T11:00:00+02:00",
                  "citation": "graph-mail://m/old@c", "title": "Projected"},
                 {"kind": "email", "date": "2026-10-02T09:30:00Z",
                  "citation": "graph-mail://m/new@c", "title": "Newer",
                  mail_topup.FLAG: True}]
        # 11:00+02:00 is 09:00Z: compared as instants, the newer capture wins.
        last = fa._latest_message(found)
        self.assertEqual(last["citation"], "graph-mail://m/new@c")
        draft = fa.Draft()
        fa._say_message(self.Reads(), draft, last, "The latest message was sent")
        self.assertIn("(newer than the last full refresh)", draft.sentences[0])
        draft = fa.Draft()
        fa._say_message(self.Reads(), draft, found[0], "The latest message was sent")
        self.assertNotIn("refresh", draft.sentences[0])


if __name__ == "__main__":
    unittest.main()
