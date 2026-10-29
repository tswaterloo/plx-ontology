"""Captured case originals served per reader through their source item's gate.

A
file ``scripts.case_originals read`` holds in ``<state>/case-originals`` reads through
``plx.documents``, ``plx.read`` and the claim gate's re-read for a reader who may open the
SharePoint item (or Teams chat message) it was read from, and for nobody else. A file
with no known source ACL is served to nobody, and the people exclusion holds.

The real user path: MCP ``tools/call`` on ``MCPServer`` over ``CaptureCaseHostTools``
with a versioned SharePoint capture (the capture tests' in-process FakeDrive) and a
fixture capture store of case originals (receipts in the reader's own format).

Evidence label: ``injected-transport`` / ``isolated-fixture``. No network, no credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import time
import unittest
from unittest import mock
from urllib.parse import quote

from evals import runner
from evals import score as sc
from evals.tests.test_runner import ABSTAIN, card_item
from evals.tests import fixtures as fx

from integrations.live_source_guard import LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AudienceMap
from integrations.tests import test_capture_teams as tct
from integrations.tests.test_capture_sharepoint import (DRIVE, EMPLOYEE, OUTSIDER, QUALITY,
                                                        QUALITY_FOLDER, RICARDO, SALES,
                                                        TEXT_SECRET, reader_audience)
from operating import captured_originals as co
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaptureCaseHostTools, CaseCaptures
from operating.cases import CitationUnresolvable
from operating.mcp_server import MCPServer
from operating.tests.test_case_host_sharepoint import SOON, SharePointHostCase
from operating.tests.test_case_tools_host import _client

EXCLUDED_OID = "0e0e0e0e-0000-4000-8000-00000000e0e1"
EXCLUDED_UPN = "excluded.person@example-plx.com"
VERSION = "f" * 64
BRIEF, CHAT, LINK_ONLY, MEMBER, DUPLICATE, EXCLUDED, TERSE = (
    "co-aaaaaaaaaaa1", "co-bbbbbbbbbbb2", "co-ccccccccccc3", "co-ddddddddddd4",
    "nr-eeeeeeeeeee5", "co-fffffffffff6", "co-aaaaaaaaaaa7")
# A brief whose file name holds none of the words a reader asks for it by.
TERSE_NAME = "AT_v3.docx"
TERSE_FACT = "Stephen's brief: treat the bottle filler as a capital asset from April."
BRIEF_NAME = "Asset tracker brief (capitalize vs expense).md"
PAGE_TWO_FACT = "The bottle filler on line 3 replaces the 2019 unit and is capitalised."
CHAT_URL = ("https://teams.microsoft.com/l/message/19%3Aabc123def%40thread.v2/1791234134091"
            "?context=%7b%22contextType%22%3a%22chat%22%7d&tenantId=t")


class _Directory:
    """The one complete ``GET /users`` listing the people guard binds from."""

    def __init__(self, oid=EXCLUDED_OID, upn=EXCLUDED_UPN):
        self.oid, self.upn = oid, upn

    def get_pages(self, url, **bounds):
        return ([{"id": self.oid, "userPrincipalName": self.upn}], {"complete": True})


def bound_guard(oid=EXCLUDED_OID, upn=EXCLUDED_UPN) -> PeopleExclusion:
    guard = PeopleExclusion([oid])
    guard.bind(_Directory(oid, upn))
    return guard


def write_original(store, entry_id, name, content, *, drive=None, item=None, teams=None,
                   url=None, parent=None, status="registered", repository=None):
    """One file as ``scripts.case_originals read`` stores it: bytes by SHA-256, the
    extracted text and a receipt naming its canonical source."""
    digest = hashlib.sha256(content).hexdigest()
    (store / "extractions").mkdir(parents=True, exist_ok=True)
    (store / digest).write_bytes(content)
    (store / "extractions" / f"{digest}.txt").write_text(content.decode("utf-8", "replace"),
                                                         encoding="utf-8")
    folder = store / "receipts" / ("discovered" if status == "discovered" else VERSION)
    folder.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema": "plx-case-original-receipt/v1", "entry_id": entry_id, "status": status,
        "body_status": "read", "match": None if status == "discovered" else True,
        "sha256": digest, "bytes": len(content), "stored": f"case-originals/{digest}",
        "name": name, "parent": parent, "detected_media_type": "text/plain",
        "read_at": "2026-10-27T12:00:00+00:00",
        "canonical_source": {"drive_id": drive, "item_id": item, "teams_message_url": teams,
                             "url": url, "drive_path": None, "member_path": None,
                             "repository_ref": repository, "attachment_name": None},
        "extraction": {"status": "complete", "methods": ["utf8-text"],
                       "parts": {"extracted": 1, "skipped_by_cap": 0, "unread": 0},
                       "text_ref": f"case-originals/extractions/{digest}.txt",
                       "unread_reasons": []}}
    (folder / f"{entry_id}.json").write_text(json.dumps(receipt), encoding="utf-8")


class CapturedOriginalsCase(SharePointHostCase):
    scanned = False

    def setUp(self):
        super().setUp()
        self.case_state = self.root / "case-state"
        self.store = self.case_state / "case-originals"
        brief = ("# Asset tracker brief\n\n" + "Context line about the tracker.\n" * 300
                 + PAGE_TWO_FACT + "\n")
        self.add(BRIEF, BRIEF_NAME, brief.encode(), drive=DRIVE, item="01FILEC")
        self.add(CHAT, "Grants brief.md", b"# Grants\nchat attachment\n", teams=CHAT_URL)
        self.add(LINK_ONLY, "Linked tracker.xlsx", b"PK linked only",
                 url="https://contoso-plx.sharepoint.com/sites/Ops/tracker.xlsx")
        self.add(MEMBER, "freight quote.txt", b"Freight quote for the filler\n", parent=BRIEF)
        # The SharePoint capture already serves these exact bytes as 01FILEB.
        self.add(DUPLICATE, "release.txt", TEXT_SECRET.encode(), drive=DRIVE, item="01FILEB",
                 status="discovered")
        self.add(TERSE, TERSE_NAME, ("# v3\n" + "Background line.\n" * 600 + TERSE_FACT
                                     + "\n").encode(), drive=DRIVE, item="01FILEC")
        self.add(EXCLUDED, "excluded owner notes.txt", b"notes\n", drive=DRIVE, item="01FILEB",
                 url=("https://contoso-my.sharepoint.com/personal/excluded_person_example-plx"
                      "_com/Documents/Microsoft Teams Chat Files/notes.txt"))

    def add(self, entry_id, name, content, **source):
        write_original(self.store, entry_id, name, content, **source)

    def runtime_server(self, actor, *, guard=None, audience=None):
        runtime = CaseRuntime(self.case_state, client=None,
                              audience=self.mirror_audience(True), tenant="plx",
                              live=LiveReaders(audience=audience or reader_audience()),
                              people_exclusion=guard or bound_guard())
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, sharepoint_capture_dir=self.state,
                                        store_path=self.projection, clock=lambda: SOON)
        tools = CaptureCaseHostTools(runtime, actor, store_path=self.projection,
                                     sharepoint_capture_dir=self.state, clock=lambda: SOON)
        server = MCPServer(case_runtime=runtime, actor=actor, ontology=tools)
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "captured-originals-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return runtime, server

    def documents(self, server, query):
        answer = self.ok(server, "plx.documents", {"query": query, "sources": ["original"]})
        return answer, [d["citation"] for d in answer["documents"]]

    def read(self, server, citation):
        return self.ok(server, "plx.read", {"citation": citation})


class SourceReaderTests(CapturedOriginalsCase):
    def test_a_reader_of_the_source_item_finds_the_brief_and_cites_its_page(self):
        runtime, server = self.runtime_server(QUALITY)
        _answer, found = self.documents(server, "asset tracker brief")
        self.assertIn(f"original://{BRIEF}", found)
        whole = self.read(server, f"original://{BRIEF}")
        self.assertEqual(whole["status"], "resolved", whole)
        page = f"original://{BRIEF}#page=2"
        cited = self.read(server, page)
        self.assertEqual(cited["status"], "resolved", cited)
        self.assertIn(PAGE_TWO_FACT, json.dumps(cited))
        self.assertNotIn(PAGE_TWO_FACT, json.dumps(self.read(server,
                                                             f"original://{BRIEF}#page=1")))
        # The claim gate re-reads the same page through the runtime's one resolver.
        view = runtime.resolve_citation(page, QUALITY)
        self.assertIn(PAGE_TWO_FACT, view["text"])
        self.assertEqual(view["text_source"], "original_text")
        # A member of the brief's container is gated by the container's item.
        self.assertEqual(self.read(server, f"original://{MEMBER}")["status"], "resolved")

    def test_bytes_changed_since_capture_never_read_as_the_cited_file(self):
        runtime, server = self.runtime_server(QUALITY)
        receipt = json.loads((self.store / "receipts" / VERSION / f"{BRIEF}.json").read_text())
        (self.case_state / receipt["stored"]).write_bytes(b"tampered")
        self.assertEqual(self.read(server, f"original://{BRIEF}#page=2")["status"],
                         "unresolvable")
        with self.assertRaises(CitationUnresolvable):
            runtime.resolve_citation(f"original://{BRIEF}", QUALITY)

    def test_bytes_changed_after_their_hash_was_remembered_are_never_read(self):
        # A hash is remembered only for the same unchanged file: rewritten in place to the
        # same size with its modification time put back, it is hashed again (the change
        # time moved) and refused.
        runtime, server = self.runtime_server(QUALITY)
        receipt = json.loads((self.store / "receipts" / VERSION / f"{BRIEF}.json").read_text())
        stored = self.case_state / receipt["stored"]
        with mock.patch.object(co, "SETTLED_NS", -1):
            for _ in range(2):
                self.assertEqual(self.read(server, f"original://{BRIEF}#page=2")["status"],
                                 "resolved")
            self.assertTrue(runtime.captured_originals._hashed)
            before = stored.stat()
            time.sleep(0.05)
            content = stored.read_bytes()
            stored.write_bytes(content.replace(b"capitalised", b"expensed!!!"))
            os.utime(stored, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(stored.stat().st_size, before.st_size)
            self.assertEqual(self.read(server, f"original://{BRIEF}#page=2")["status"],
                             "unresolvable")
            with self.assertRaises(CitationUnresolvable):
                runtime.resolve_citation(f"original://{BRIEF}", QUALITY)

    def test_page_windows_count_served_bytes_as_json_does(self):
        # The page split counts each line's bytes served (JSON string, then that string
        # as JSON again) without building either; every character class counts alike.
        from operating.case_originals import _served_bytes

        def served(text):
            once = json.dumps(text, ensure_ascii=False)
            return len(once.encode()) + len(json.dumps(once, ensure_ascii=False).encode())

        alphabet = [chr(c) for c in range(0x80)] + list("é€😀\u2028\xa0\x85\ufeff") + ["a"] * 40
        pick = random.Random(5)
        for _ in range(20000):
            text = "".join(pick.choice(alphabet) for _ in range(pick.randint(0, 12)))
            text += "\n" if pick.random() < 0.5 else ""
            self.assertEqual(_served_bytes(text), served(text), repr(text))

    def test_bytes_the_sharepoint_capture_serves_are_not_listed_twice_but_still_read(self):
        _runtime, server = self.runtime_server(EMPLOYEE)
        _answer, found = self.documents(server, "release")
        self.assertNotIn(f"original://{DUPLICATE}", found)
        self.assertEqual(self.read(server, f"original://{DUPLICATE}")["status"], "resolved")


class WithheldTests(CapturedOriginalsCase):
    def assert_withheld(self, server, runtime, actor, entry_id, query):
        """Nothing of the original reaches the reader: no listing, no title, the same
        answer as an id that never existed."""
        answer, found = self.documents(server, query)
        self.assertNotIn(f"original://{entry_id}", found)
        receipt = next(self.store.glob(f"receipts/*/{entry_id}.json"))
        name = json.loads(receipt.read_text())["name"]
        self.assertNotIn(name, json.dumps(answer))
        for handle in (f"original://{entry_id}", f"original://{entry_id}#page=1"):
            read = self.read(server, handle)
            missing = self.read(server, handle.replace(entry_id, "co-999999999999"))
            self.assertEqual((read["status"], read.get("reason")),
                             (missing["status"], missing.get("reason")))
            self.assertNotIn(name, json.dumps(read))
            with self.assertRaises(CitationUnresolvable):
                runtime.resolve_citation(handle, actor)

    def test_a_reader_without_access_to_the_source_item_gets_nothing(self):
        for actor in (EMPLOYEE, OUTSIDER):   # sales may not open 01FILEC; outsider nothing
            with self.subTest(actor=actor):
                runtime, server = self.runtime_server(actor)
                self.assert_withheld(server, runtime, actor, BRIEF, "asset tracker brief")
                self.assert_withheld(server, runtime, actor, MEMBER, "freight quote")

    def test_an_original_with_no_known_source_acl_is_served_to_nobody(self):
        runtime, server = self.runtime_server(QUALITY)
        self.assert_withheld(server, runtime, QUALITY, CHAT, "grants brief")
        self.assert_withheld(server, runtime, QUALITY, LINK_ONLY, "linked tracker")
        states = runtime.captured_originals.coverage()["states"]
        self.assertEqual(states[CHAT], "unknown_acl:source_not_captured:teams_chat")
        self.assertEqual(states[LINK_ONLY], "unknown_acl:source_item_unidentified")
        self.assertEqual(states[BRIEF], "servable:sharepoint")

    def test_the_people_exclusion_holds_over_the_source_gate(self):
        # 01FILEB is open to quality and sales, but this copy sits in an excluded
        # person's OneDrive.
        runtime, server = self.runtime_server(QUALITY)
        self.assert_withheld(server, runtime, QUALITY, EXCLUDED, "excluded owner notes")
        # A guard that cannot decide withholds every captured original.
        runtime, server = self.runtime_server(
            QUALITY, guard=PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assert_withheld(server, runtime, QUALITY, BRIEF, "asset tracker brief")


class ContentSearchTests(CapturedOriginalsCase):
    """An original is found by the words of its stored extracted text, not only its name;
    one the reader may not open is counted by gate kind, never named."""

    def not_openable(self, answer):
        return {gap["subject"]: gap for gap in answer["gaps"]
                if gap["reason"] == "original_not_openable"}

    def test_a_reader_of_the_item_finds_a_brief_by_the_words_of_its_text(self):
        _runtime, server = self.runtime_server(QUALITY)
        answer, found = self.documents(server, "Stephen's capital asset brief")
        self.assertEqual(found, [f"original://{TERSE}"])
        snippet = answer["documents"][0]["snippet"]
        self.assertIn("Stephen", snippet["untrusted_text"])
        # The snippet is cited to the page holding the words, and that page reads them.
        self.assertRegex(snippet["origin"]["citation"], rf"^original://{TERSE}#page=[2-9]$")
        self.assertIn(TERSE_FACT, json.dumps(self.read(server, snippet["origin"]["citation"])))
        self.assertEqual(self.not_openable(answer), {})
        # A word in neither the name nor the text: no match, nothing withheld named.
        _answer, found = self.documents(server, "Stephen zeppelin")
        self.assertEqual(found, [])

    def test_a_reader_the_source_gate_refuses_learns_only_how_many_names_match(self):
        runtime, server = self.runtime_server(EMPLOYEE)   # sales may not open 01FILEC
        answer, found = self.documents(server, "asset tracker brief")
        self.assertEqual(found, [])
        dumped = json.dumps(answer)
        for secret in (BRIEF, BRIEF_NAME, PAGE_TWO_FACT):
            self.assertNotIn(secret, dumped)
        gap = self.not_openable(answer)["source:original:source_gate_denied"]
        self.assertEqual(gap["state"], "inaccessible")
        self.assertTrue(gap["detail"].startswith("1 captured original(s) match"), gap)
        self.assertFalse(answer["completeness"]["complete"])
        searched = runtime.captured_originals.search(EMPLOYEE, 50,
                                                     words=["asset", "tracker", "brief"])
        self.assertEqual((searched["found"], searched["withheld"]),
                         ([], {"source_gate_denied": 1}))

    def test_a_text_only_match_on_an_unopenable_original_counts_nothing(self):
        # The words are in TERSE's text, not its name: a reader who may not open it must
        # not learn, word by word, what it says (decision 2026-10-27 name only).
        runtime, server = self.runtime_server(EMPLOYEE)
        answer, found = self.documents(server, "Stephen's capital asset brief")
        self.assertEqual((found, self.not_openable(answer)), ([], {}))
        for secret in (TERSE, TERSE_NAME, "Stephen's brief", "bottle filler"):
            self.assertNotIn(secret, json.dumps(answer))
        self.assertEqual(runtime.captured_originals.search(
            EMPLOYEE, 50, words=["stephen", "capital"])["withheld"], {})
        # The same holds for an original with no known ACL: its text ("linked only")
        # counts nothing, its name ("Linked tracker.xlsx") does.
        self.assertEqual(runtime.captured_originals.search(
            EMPLOYEE, 50, words=["linked", "only"])["withheld"], {})
        self.assertEqual(runtime.captured_originals.search(
            EMPLOYEE, 50, words=["linked", "tracker"])["withheld"], {"source_acl_unknown": 1})
        # The reader who may open TERSE still finds it by its text.
        _runtime, server = self.runtime_server(QUALITY)
        self.assertEqual(self.documents(server, "Stephen's capital asset brief")[1],
                         [f"original://{TERSE}"])

    def test_an_original_with_no_known_acl_is_counted_for_every_reader_never_named(self):
        for actor in (QUALITY, EMPLOYEE):
            with self.subTest(actor=actor):
                _runtime, server = self.runtime_server(actor)
                answer, found = self.documents(server, "linked tracker")
                self.assertEqual(found, [])
                self.assertNotIn("Linked tracker", json.dumps(answer))
                gap = self.not_openable(answer)["source:original:source_acl_unknown"]
                self.assertTrue(gap["detail"].startswith("1 captured original(s)"), gap)

    def test_a_teams_attachment_whose_chat_is_not_captured_counts_nothing(self):
        # Nobody can tell who is in the chat, so the exclusion cannot clear it: no count.
        runtime, server = self.runtime_server(EMPLOYEE)
        answer, found = self.documents(server, "grants brief")
        self.assertEqual((found, self.not_openable(answer)), ([], {}))
        self.assertNotIn("Grants brief", json.dumps(answer))
        self.assertEqual(runtime.captured_originals.search(
            EMPLOYEE, 50, words=["grants", "brief"])["withheld"], {})

    def test_the_people_exclusion_is_never_counted(self):
        # 01FILEB admits quality, but this copy sits in an excluded person's OneDrive.
        _runtime, server = self.runtime_server(QUALITY)
        answer, _found = self.documents(server, "excluded owner notes")
        self.assertEqual(self.not_openable(answer), {})
        # A guard that cannot decide counts nothing at all, of any kind.
        runtime, server = self.runtime_server(
            EMPLOYEE, guard=PeopleExclusion(unavailable="excluded_people_oids_absent"))
        for query in ("asset tracker brief", "grants brief", "linked tracker"):
            answer, found = self.documents(server, query)
            self.assertEqual((found, self.not_openable(answer)), ([], {}))
        self.assertIsNone(runtime.captured_originals.search(
            EMPLOYEE, 50, words=["grants"])["withheld"])

    def test_the_exclusion_helper_clears_only_what_the_guard_decides(self):
        runtime, _server = self.runtime_server(QUALITY)
        clear = runtime._captured_original_exclusion_clear
        self.assertTrue(clear(None))
        self.assertTrue(clear("someone_else_example-plx_com"))
        self.assertFalse(clear("excluded_person_example-plx_com"))
        self.assertFalse(clear("EXCLUDED_PERSON_EXAMPLE-PLX_COM"))
        # A guard that cannot decide clears nothing, not even a file in no OneDrive.
        runtime, _server = self.runtime_server(
            QUALITY, guard=PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assertFalse(runtime._captured_original_exclusion_clear(None))
        # A guard with a list but no directory binding cannot decide an owner offline,
        # nor a chat's people (addresses need the bound guard).
        runtime, _server = self.runtime_server(QUALITY, guard=PeopleExclusion([EXCLUDED_OID]))
        self.assertFalse(runtime._captured_original_exclusion_clear(
            "someone_else_example-plx_com"))
        self.assertFalse(runtime._captured_original_exclusion_clear(
            None, [{"identities": ["someone@example-plx.com"]}]))
        # A chat's people: cleared only when none of them is excluded, by id or address.
        self.assertTrue(clear(None, [{"identities": ["someone@example-plx.com"]},
                                     {"user": {"id": "0000aaaa-0000-0000-0000-000000000001"}}]))
        self.assertFalse(clear(None, [{"user": {"id": EXCLUDED_OID}}]))
        self.assertFalse(clear(None, [{"identities": [EXCLUDED_UPN.upper()]}]))

    def test_text_hits_of_unopenable_originals_never_use_the_gate_checks(self):
        # Sales may open none of the "z" notes (01FILEC); their text, not their name,
        # holds the word. Sales may open "0 release" (no match, first by name) and "zz release" (a text
        # hit), both 01FILEB.
        for n in range(3):
            self.add(f"co-00000000000{n}", f"z{n} notes.txt",
                     b"the zebra clause\n", drive=DRIVE, item="01FILEC")
        self.add("co-0000000000fe", "0 release.txt", b"plain\n", drive=DRIVE, item="01FILEB")
        self.add("co-0000000000ff", "zz release.txt", b"zebra release\n", drive=DRIVE,
                 item="01FILEB")
        runtime, _server = self.runtime_server(EMPLOYEE)
        # The gate order is by name match, then name: the unopenable text hits do not
        # take the one gate check ahead of the openable file that sorts first.
        first = runtime.captured_originals.search(EMPLOYEE, 50, words=["zebra"],
                                                  max_checks=1)
        self.assertEqual([r["original_id"] for r in first["found"]], ["co-0000000000fe"])
        self.assertTrue(first["cut"])
        scanned = runtime.captured_originals.search(EMPLOYEE, 50, words=["zebra"])
        self.assertEqual([(r["original_id"], r["match"]) for r in scanned["found"]][:1],
                         [("co-0000000000ff", "text")])
        self.assertEqual(scanned["withheld"], {})

    def test_a_refusing_store_is_a_named_gap_not_silence(self):
        runtime, server = self.runtime_server(QUALITY)

        def refuse(*_args, **_kwargs):
            raise PermissionError("refused")
        runtime.captured_originals.search = refuse
        answer, found = self.documents(server, "Stephen brief")
        self.assertEqual(found, [])
        self.assertIn("originals_store_refused", [g["reason"] for g in answer["gaps"]])
        self.assertFalse(answer["completeness"]["complete"])


def host_read(server, citation):
    """``plx.source_citation`` as the harness reads it (host code, the tool's own result)."""
    server.request_id = getattr(server, "request_id", 1) + 1
    response = server.handle({"jsonrpc": "2.0", "id": server.request_id,
                              "method": "tools/call", "params": {
                                  "name": "plx.source_citation",
                                  "arguments": {"citation": citation}}}, host_code=True)
    return response["result"]["structuredContent"]


class _ScoringReader:
    """The scoring reader ``evals.runner.CaseHost`` is, over this test's own server and
    runtime as ``actor``: the harness read and the gate's refusal kind."""

    def __init__(self, server, runtime, actor):
        self.server, self.runtime, self.actor = server, runtime, actor

    def read(self, name, arguments):
        return host_read(self.server, arguments["citation"])

    def original_refusal(self, citation):
        return self.runtime.original_refusal_kind(citation, self.actor)


class RefusalKindTests(CapturedOriginalsCase):
    """Inside the gate a decided refusal and a failure answer every reader alike
    (``original_not_visible``); only the scorer's ``refusal_kind`` tells them apart, and
    only a decided refusal makes a pinned original not held (``evals.runner``)."""

    NEVER = "co-999999999999"

    def sha(self, entry_id):
        receipt = next(self.store.glob(f"receipts/*/{entry_id}.json"))
        return json.loads(receipt.read_text())["sha256"]

    def assert_reads_as_absent(self, server, entry_id):
        """The tool answers, to the agent and to host code, are exactly those of an id
        that never existed: the kind leaves no trace in them."""
        for handle in (f"original://{entry_id}", f"original://{entry_id}#page=1"):
            missing = handle.replace(entry_id, self.NEVER)
            self.assertEqual(host_read(server, handle),
                             {"ok": False, "error": "original_not_visible", "citation": handle})
            self.assertEqual(self.answer(server, handle),
                             self.answer(server, missing).replace(self.NEVER, entry_id))

    def answer(self, server, handle):
        """``plx.read``'s answer as the agent gets it, its read time aside."""
        read = self.read(server, handle)
        read["meta"].pop("as_known_at")
        return json.dumps(read, sort_keys=True)

    def scored(self, server, runtime, actor, entry_id):
        """``originals_held`` and the outcome of an abstention over the entry's bytes."""
        item = card_item(oracle={"id": "oracle:proposed-comprehension@x",
                                 "interface": "plx-eval-oracle/v1",
                                 "derived_from": [f"original://sha256/{self.sha(entry_id)}"],
                                 "computed_at": "2026-10-27T00:00:00Z"})
        reader = _ScoringReader(server, runtime, actor)
        held = runner.originals_held(item, self.case_state, reader=reader)
        host = fx.FakeHost()
        host.state_dir = self.case_state
        host.read = reader.read
        host.original_refusal = reader.original_refusal
        transcript = runner.record(item, runner.ScriptedAgent(
            lambda prompt, call: fx.final_text(ABSTAIN)), host)
        return held, sc.outcome_of(sc.score(item, transcript))

    def assert_refused(self, server, runtime, actor, entry_id):
        self.assertEqual(runtime.original_refusal_kind(f"original://{entry_id}", actor),
                         co.REFUSED)
        self.assert_reads_as_absent(server, entry_id)
        self.assertEqual(self.scored(server, runtime, actor, entry_id),
                         ({"pinned": 1, "missing": 1, "not_openable": 1}, "blocked"))

    def assert_failed(self, server, runtime, actor, entry_id):
        self.assertEqual(runtime.original_refusal_kind(f"original://{entry_id}", actor),
                         co.FAILED)
        self.assert_reads_as_absent(server, entry_id)
        self.assertEqual(self.scored(server, runtime, actor, entry_id),
                         ({"pinned": 1, "missing": 0, "unreadable": ["reader_check_failed"]},
                          "wrong"))

    def test_a_reader_who_opens_the_original_has_no_refusal_and_it_is_held(self):
        runtime, server = self.runtime_server(QUALITY)
        self.assertIsNone(runtime.original_refusal_kind(f"original://{BRIEF}", QUALITY))
        self.assertEqual(self.scored(server, runtime, QUALITY, BRIEF),
                         ({"pinned": 1, "missing": 0}, "wrong"))

    def test_each_decided_refusal_makes_the_original_not_held(self):
        self.add("co-0000000000a1", "uncaptured.txt", b"not in the capture\n", drive=DRIVE,
                 item="01NOTCAPTURED")
        cases = [
            # the source ACL denies this reader (sales, outsider; and a member of it)
            (EMPLOYEE, BRIEF), (OUTSIDER, BRIEF), (EMPLOYEE, MEMBER),
            # the people exclusion decided: the copy sits in an excluded person's OneDrive
            (QUALITY, EXCLUDED),
            # no known ACL: an item the configured SharePoint capture does not hold
            (QUALITY, "co-0000000000a1")]
        for actor, entry_id in cases:
            with self.subTest(actor=sorted(actor.principals), entry=entry_id):
                runtime, server = self.runtime_server(actor)
                self.assert_refused(server, runtime, actor, entry_id)
        # No known source ACL at all (a link only): served to nobody by policy. Its bytes
        # are never held for a reader, so the pin is missing before any reader check.
        runtime, server = self.runtime_server(QUALITY)
        self.assertEqual(runtime.original_refusal_kind(f"original://{LINK_ONLY}", QUALITY),
                         co.REFUSED)
        self.assert_reads_as_absent(server, LINK_ONLY)

    def test_each_gate_failure_stays_unknown_and_the_abstention_stays_wrong(self):
        # The people exclusion cannot decide: no usable list, or an owner it cannot
        # decide offline (a list with no directory binding).
        for guard, entry_id in ((PeopleExclusion(unavailable="excluded_people_oids_absent"),
                                 BRIEF), (PeopleExclusion([EXCLUDED_OID]), EXCLUDED)):
            with self.subTest(guard=guard.unavailable, entry=entry_id):
                runtime, server = self.runtime_server(QUALITY, guard=guard)
                self.assert_failed(server, runtime, QUALITY, entry_id)
        # A Teams chat attachment on a host with no Teams capture: not configured.
        runtime, server = self.runtime_server(QUALITY)
        self.assertEqual(runtime.original_refusal_kind(f"original://{CHAT}", QUALITY),
                         co.FAILED)
        self.assert_reads_as_absent(server, CHAT)

        class Locked(CaseCaptures):
            def _sharepoint(self, handle, actor, why=None):
                raise sqlite3.OperationalError("database is locked")

        def raising(error):
            def captures(handle, actor):
                raise error
            return captures

        for name, captures in (
                ("not configured", None), ("locked store", None),
                ("exception", raising(RuntimeError("gate bug"))),
                ("timeout", raising(TimeoutError("re-read timed out")))):
            with self.subTest(name):
                runtime, server = self.runtime_server(QUALITY)
                runtime.captures = (Locked(runtime, sharepoint_capture_dir=self.state)
                                    if name == "locked store" else captures)
                self.assert_failed(server, runtime, QUALITY, BRIEF)

    def test_an_item_the_capture_recorded_deleted_is_a_failure_not_a_refusal(self):
        self.drive.delete("01FILEC")
        self.capture.run(self.capture_scope())
        lookup = co.SourceLookup(self.case_state, sharepoint_dir=self.state)
        self.assertEqual([lookup.sharepoint_deleted(DRIVE, item) for item in (
            "01FILEC", "01FILEB", "01NOTCAPTURED")], [True, False, False])
        for actor in (QUALITY, EMPLOYEE):   # one the ACL admitted, one it refused
            with self.subTest(actor=sorted(actor.principals)):
                runtime, server = self.runtime_server(actor)
                self.assert_failed(server, runtime, actor, BRIEF)

    def test_an_unknown_handle_or_a_refused_check_is_a_failure(self):
        # A member whose container receipt is gone: nothing says what ACL it had.
        self.add("co-0000000000b2", "orphan.txt", b"orphan\n", parent="co-777777777777")
        runtime, _server = self.runtime_server(QUALITY)
        self.assertEqual(runtime.original_refusal_kind("original://co-0000000000b2", QUALITY),
                         co.FAILED)
        for handle in (f"original://{self.NEVER}", "original://nope", None, 7):
            self.assertEqual(runtime.captured_originals.refusal_kind(handle, QUALITY),
                             co.FAILED)

        def broken(*_args):
            raise OSError("unreadable store")
        runtime.captured_originals._lookup = broken
        self.assertEqual(runtime.original_refusal_kind(f"original://{BRIEF}", QUALITY),
                         co.FAILED)


REPO_ORIGINAL = "co-abcabcabc001"
REPO_NAME = "Packaging procure-to-GL map.md"
REPO_EXCLUDED = "co-abcabcabc004"
EXCLUDED_ONEDRIVE = ("https://contoso-my.sharepoint.com/personal/excluded_person_example-plx"
                     "_com/Documents/Microsoft Teams Chat Files/procure.md")
REPO_REF = ("https://github.com/acme-plx/case-maps/blob/" + "e" * 40
            + "/context/procure-map.json")


def repository_audience(*, grants=("staff:quality",), repository="repo:acme-plx/case-maps"):
    """``reader_audience`` plus one exact repository grant (default deny)."""
    return LiveSourceAudience(
        {f"sharepoint:{DRIVE}:{QUALITY_FOLDER}": ["staff:quality", "staff:sales"],
         f"sharepoint:{DRIVE}:{QUALITY_FOLDER}/*": ["staff:quality", "staff:sales"],
         f"sharepoint:{DRIVE}:/Restricted": ["staff:quality"],
         f"sharepoint:{DRIVE}:/Restricted/*": ["staff:quality"],
         repository: list(grants)},
        tenant="plx", identities={"staff:quality": [RICARDO], "staff:sales": [SALES]},
        source_acl_required=[f"sharepoint:{DRIVE}:*"])


class RepositoryOriginalTests(CapturedOriginalsCase):
    """A repository-sourced original reads only for a reader the live audience grants its
    repository; every other reader's answers are those an unknown-ACL original gave."""

    NEVER = "co-999999999999"
    assert_reads_as_absent = RefusalKindTests.assert_reads_as_absent
    answer = RefusalKindTests.answer

    def setUp(self):
        super().setUp()
        self.add(REPO_ORIGINAL, REPO_NAME, b"# Procure to GL\nPO approval then receipt.\n",
                 repository=REPO_REF)

    def test_a_granted_reader_reads_the_repository_original_and_no_one_else_does(self):
        runtime, server = self.runtime_server(QUALITY, audience=repository_audience())
        _answer, found = self.documents(server, "procure GL map")
        self.assertEqual(found, [f"original://{REPO_ORIGINAL}"])
        read = self.read(server, f"original://{REPO_ORIGINAL}")
        self.assertEqual(read["status"], "resolved")
        self.assertIsNone(runtime.original_refusal_kind(f"original://{REPO_ORIGINAL}",
                                                        QUALITY))
        self.assertEqual(runtime.captured_originals.coverage()["states"][REPO_ORIGINAL],
                         "servable:repository")
        # Not granted: sales and an outsider under the same audience, quality under one
        # that grants another repository, or under one granting no repository at all.
        refused = [(EMPLOYEE, repository_audience()), (OUTSIDER, repository_audience()),
                   (QUALITY, repository_audience(repository="repo:acme-plx/case-maps-old")),
                   (QUALITY, reader_audience())]
        for actor, audience in refused:
            with self.subTest(actor=sorted(actor.principals)):
                runtime, server = self.runtime_server(actor, audience=audience)
                self.assertEqual(runtime.original_refusal_kind(
                    f"original://{REPO_ORIGINAL}", actor), co.REFUSED)
                self.assert_reads_as_absent(server, REPO_ORIGINAL)
                answer, found = self.documents(server, "procure GL map")
                self.assertEqual(found, [])
                self.assertNotIn(REPO_NAME, json.dumps(answer))
                self.assertEqual(runtime.captured_originals.search(
                    actor, 50, words=["procure", "gl"])["withheld"],
                    {"source_acl_unknown": 1})
        # The people exclusion still holds first for a granted reader.
        runtime, server = self.runtime_server(
            QUALITY, audience=repository_audience(),
            guard=PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assertEqual(runtime.original_refusal_kind(f"original://{REPO_ORIGINAL}",
                                                       QUALITY), co.FAILED)
        self.assertNotEqual(self.read(server, f"original://{REPO_ORIGINAL}")["status"],
                            "resolved")

    def add_repository_variants(self):
        """Repository files whose sources also name something else: a non-Teams message
        link, a SharePoint site link, a copy in an excluded person's OneDrive."""
        body = b"# Procure to GL\nPO approval then receipt.\n"
        self.add("co-abcabcabc002", "procure map thread.md", body + b"2",
                 repository=REPO_REF, teams="https://example.com/thread/1")
        self.add("co-abcabcabc003", "procure map site copy.md", body + b"3",
                 repository=REPO_REF,
                 url="https://contoso-plx.sharepoint.com/sites/Ops/procure.md")
        self.add(REPO_EXCLUDED, "procure map excluded copy.md", body + b"4",
                 repository=REPO_REF, url=EXCLUDED_ONEDRIVE)

    def test_an_unauthorised_reader_sees_what_it_saw_before_the_grant_existed(self):
        # Base behaviour (before decision 2026-10-29): ``source_unit`` gave every
        # repository file no unit (``repository_file``). Patching exactly that back in
        # gives base's answers; a refused reader's answers now are the same.
        self.add_repository_variants()
        real = co.source_unit

        def base_source_unit(receipt):
            unit, reason = real(receipt)
            return (None, co.REPOSITORY) if unit and unit["kind"] == "repository" \
                else (unit, reason)

        handles = [f"original://{e}" for e in (REPO_ORIGINAL, "co-abcabcabc002",
                                                "co-abcabcabc003", REPO_EXCLUDED)]

        def answers(actor, audience):
            runtime, server = self.runtime_server(actor, audience=audience)
            store = runtime.captured_originals
            documents = self.documents(server, "procure map")[0]
            documents["meta"].pop("as_known_at")
            return (json.dumps(documents, sort_keys=True),
                    [store.search(actor, 50, words=w)
                     for w in (["procure"], ["procure", "map"], [], ["po"], ["thread"])],
                    store.list_visible(actor, 50),
                    [runtime.original_refusal_kind(h, actor) for h in handles],
                    [self.answer(server, h) for h in handles])

        for actor, audience in ((EMPLOYEE, repository_audience()),
                                (OUTSIDER, repository_audience()),
                                (QUALITY, reader_audience())):
            with self.subTest(actor=sorted(actor.principals)):
                now = answers(actor, audience)
                with mock.patch.object(co, "source_unit", base_source_unit):
                    base = answers(actor, audience)
                    runtime, _ = self.runtime_server(actor, audience=audience)
                    self.assertEqual(runtime.captured_originals.coverage()["states"][
                        REPO_ORIGINAL], "unknown_acl:repository_file")
                self.assertEqual(now, base)
                # base counted the plain and site-linked files only, never the one with
                # a message link or the excluded person's copy
                self.assertEqual(now[1][0]["withheld"], {"source_acl_unknown": 2})

    def test_an_excluded_onedrive_copy_stays_withheld_from_a_granted_reader(self):
        self.add_repository_variants()
        runtime, server = self.runtime_server(QUALITY, audience=repository_audience())
        self.assertEqual(runtime.original_refusal_kind(f"original://{REPO_EXCLUDED}",
                                                       QUALITY), co.REFUSED)
        self.assertNotEqual(self.read(server, f"original://{REPO_EXCLUDED}")["status"],
                            "resolved")
        answer, found = self.documents(server, "procure map excluded copy")
        self.assertEqual(found, [])
        for secret in ("excluded copy.md", REPO_EXCLUDED, "not_openable"):
            self.assertNotIn(secret, json.dumps(answer))
        self.assertEqual(runtime.captured_originals.search(
            QUALITY, 50, words=["excluded", "copy"])["withheld"], {})
        # the granted reader still reads the other repository files
        self.assertEqual(self.read(server, "original://co-abcabcabc003")["status"],
                         "resolved")

    def test_every_repository_original_is_listed_or_counted_once_at_1_10_and_60(self):
        added = {REPO_ORIGINAL}
        for count in (1, 10, 60):
            with self.subTest(count=count):
                for n in range(count):
                    entry_id = f"co-{count:04d}{n:08x}"
                    self.add(entry_id, f"repo map {entry_id}.md", entry_id.encode(),
                             repository=REPO_REF)
                    added.add(entry_id)
                runtime, _server = self.runtime_server(QUALITY, audience=repository_audience())
                found = runtime.captured_originals.search(QUALITY, 500, words=["map"])
                ids = [row["original_id"] for row in found["found"]]
                self.assertFalse(found["cut"])
                self.assertEqual((len(ids), set(ids) & added), (len(set(ids)), added))
                runtime, _server = self.runtime_server(EMPLOYEE,
                                                       audience=repository_audience())
                refused = runtime.captured_originals.search(EMPLOYEE, 500, words=["map"])
                self.assertEqual((refused["found"], refused["cut"], refused["withheld"]),
                                 ([], False, {"source_acl_unknown": len(added)}))

    def test_a_repository_grant_is_exact_never_a_wildcard(self):
        for pattern in ("repo:*", "repo:acme-plx/*", "repo:acme-plx", "repo:acme-plx/x/y",
                        "repo:../case-maps", "repo:acme-plx/.."):
            with self.subTest(pattern=pattern), \
                    self.assertRaisesRegex(ValueError, "pattern_invalid"):
                LiveSourceAudience({pattern: ["staff:quality"]}, tenant="plx")
        audience = repository_audience()
        self.assertTrue(audience.allows(QUALITY, "repo:ACME-plx/Case-Maps"))
        # validated as given, before lowercasing: a Kelvin sign is not a "k"
        kelvin = repository_audience(repository="repo:acme-plx/kit")
        self.assertTrue(kelvin.allows(QUALITY, "repo:acme-plx/kit"))
        for resource in ("repo:acme-plx/\u212ait", "repo:acme-plx/k\u0131t"):
            with self.subTest(resource=resource):
                self.assertFalse(kelvin.allows(QUALITY, resource))
                self.assertFalse(kelvin.allows_all(QUALITY, [resource]))
                self.assertEqual(kelvin.principals_for(resource), frozenset())
        for resource in ("repo:acme-plx/case-maps-old", "repo:acme-plx/case-map",
                         "repo:acme-plx/case-maps/x", "repo:other/case-maps"):
            with self.subTest(resource=resource):
                self.assertFalse(audience.allows(QUALITY, resource))


class CardinalityTests(CapturedOriginalsCase):
    def test_every_visible_member_is_listed_once_at_1_10_and_60(self):
        added: set[str] = set()
        for count in (1, 10, 60):
            with self.subTest(count=count):
                ids = [f"co-{count:04d}{n:08x}" for n in range(count)]
                for entry_id in ids:
                    self.add(entry_id, f"member {entry_id}.txt", entry_id.encode(),
                             parent=BRIEF)
                runtime, _server = self.runtime_server(QUALITY)
                listed, cut = runtime.captured_originals.list_visible(QUALITY, 500)
                found = [row["original_id"] for row in listed]
                self.assertFalse(cut)
                self.assertEqual(len(found), len(set(found)))
                self.assertTrue(set(ids) <= set(found))
                denied, _ = runtime.captured_originals.list_visible(EMPLOYEE, 500)
                self.assertFalse({row["original_id"] for row in denied} & set(ids))
                # Every matching member a reader is refused is counted once, none named.
                added.update(ids)
                counted = runtime.captured_originals.search(EMPLOYEE, 500, words=["member"])
                self.assertEqual(counted["found"], [])
                self.assertEqual(counted["withheld"], {"source_gate_denied": len(added)})


class NamedItemsFirstTests(CapturedOriginalsCase):
    """A cold SharePoint capture (no walk reached the brief's item) serves the brief to
    nobody; once the capture's named-items pass captured the items the case originals
    name (``capture_sharepoint`` Named items first), the item gate decides as after a
    full walk: the item's reader reads it, a reader without item access does not."""

    def test_the_named_pass_lets_the_item_gate_decide_on_a_cold_capture(self):
        from integrations import capture_sharepoint as cs
        from integrations.graph_read import SharePointReader
        from integrations.tests.test_capture_sharepoint import (CAPTURE, Clock,
                                                                capture_audience, scope)
        from integrations.tests.test_capture_sharepoint_named import Cut, _Reader

        named = co.sharepoint_items(self.case_state)
        self.assertEqual(named, [(DRIVE, "01FILEB"), (DRIVE, "01FILEC")])
        self.state = self.root / "sharepoint-cold"   # the runtime's capture: a cold one
        cold = cs.SharePointCaptureStore(self.state, clock=Clock())
        self.addCleanup(cold.close)
        runtime, server = self.runtime_server(QUALITY)
        self.assertNotEqual(self.read(server, f"original://{BRIEF}")["status"], "resolved")
        cut = Cut(after=len(named))   # the named pass, then every root deferred
        capture = cs.SharePointCapture(cold, lambda d: _Reader(SharePointReader(
            self.api, capture_audience(), d, transport=self.transport, read_permissions=True,
            max_acl_reads=cs.MAX_ACL_READS), cut), CAPTURE, budget_seconds=10_000,
            monotonic=cut)
        capture.named_items = named
        run = capture.run(scope())
        self.assertEqual(run["named_items"]["outcomes"]["captured"], 2, run["named_items"])
        self.assertEqual(cold.db.execute("SELECT COUNT(*) FROM walk_positions").fetchone()[0], 0)
        self.assertFalse(run["complete"])   # the walk itself has not run
        runtime, server = self.runtime_server(QUALITY)
        for entry_id in (BRIEF, MEMBER):
            self.assertEqual(self.read(server, f"original://{entry_id}")["status"], "resolved")
        for actor in (EMPLOYEE, OUTSIDER):   # no item access to 01FILEC
            with self.subTest(actor=actor):
                runtime, server = self.runtime_server(actor)
                self.assertEqual(runtime.original_refusal_kind(f"original://{BRIEF}", actor),
                                 co.REFUSED)
                for entry_id in (BRIEF, MEMBER):
                    self.assertNotEqual(self.read(server, f"original://{entry_id}")["status"],
                                        "resolved")


class TeamsChatAttachmentTests(tct.CaptureCase):
    """A file attached in a captured Teams chat reads for the chat's members only."""

    def test_a_chat_member_reads_the_attachment_and_an_outsider_does_not(self):
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        context = quote(json.dumps({"contextType": "chat"}))
        link = (f"https://teams.microsoft.com/l/message/{quote(tct.CHAT, safe='')}/3001"
                f"?context={context}")
        store = Path(self.tmp.name) / "case-originals"
        write_original(store, CHAT, "Grants brief.md", b"# Grants brief\nthe ask\n", teams=link)
        write_original(store, MEMBER, "budget.txt", b"budget\n", parent=CHAT)
        runtime = CaseRuntime(self.tmp.name, client=None, audience=AudienceMap({}, tenant="plx"),
                              tenant="plx", live=LiveReaders(self.audience),
                              people_exclusion=PeopleExclusion.from_env(tct.ENV))
        self.addCleanup(runtime.close)
        for entry_id in (CHAT, MEMBER):
            view = runtime.resolve_citation(f"original://{entry_id}#page=1", tct.MEMBER)
            self.assertTrue(view["text"])
            for actor in (tct.OUTSIDER, OUTSIDER):
                with self.assertRaises(CitationUnresolvable):
                    runtime.resolve_citation(f"original://{entry_id}", actor)
        listed, _cut = runtime.captured_originals.list_visible(tct.MEMBER, 50)
        self.assertEqual(sorted(r["original_id"] for r in listed), sorted([CHAT, MEMBER]))
        self.assertEqual(runtime.captured_originals.list_visible(tct.OUTSIDER, 50), ([], False))

    def attachment_runtime(self, guard):
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        context = quote(json.dumps({"contextType": "chat"}))
        link = (f"https://teams.microsoft.com/l/message/{quote(tct.CHAT, safe='')}/3001"
                f"?context={context}")
        store = Path(self.tmp.name) / "case-originals"
        write_original(store, CHAT, "Grants brief.md", b"# Grants brief\nthe ask\n", teams=link)
        runtime = CaseRuntime(self.tmp.name, client=None, audience=AudienceMap({}, tenant="plx"),
                              tenant="plx", live=LiveReaders(self.audience),
                              people_exclusion=guard)
        self.addCleanup(runtime.close)
        return runtime

    def test_a_refused_reader_is_told_the_count_only_when_the_chat_people_clear(self):
        runtime = self.attachment_runtime(bound_guard())   # nobody of the chat excluded
        self.assertEqual(runtime.captured_originals.search(
            tct.OUTSIDER, 50, words=["grants"])["withheld"], {"source_gate_denied": 1})
        self.assertEqual(runtime.captured_originals.search(
            tct.MEMBER, 50, words=["grants"])["withheld"], {})

    def test_an_excluded_chat_participant_hides_the_attachment_and_its_count(self):
        # The chat's guest is excluded: the member no longer opens the file, and neither
        # the member nor an outsider learns that a matching original exists.
        runtime = self.attachment_runtime(bound_guard(tct.U_OTHER, tct.OTHER))
        with self.assertRaises(CitationUnresolvable):
            runtime.resolve_citation(f"original://{CHAT}", tct.MEMBER)
        for actor in (tct.MEMBER, tct.OUTSIDER, OUTSIDER):
            with self.subTest(actor=actor):
                found = runtime.captured_originals.search(actor, 50, words=["grants"])
                self.assertEqual((found["found"], found["withheld"]), ([], {}))

    def test_teams_refusals_are_decided_and_teams_failures_are_not(self):
        runtime = self.attachment_runtime(bound_guard())
        handle = f"original://{CHAT}"
        self.assertIsNone(runtime.original_refusal_kind(handle, tct.MEMBER))
        # not a member of the chat: the capture's gate refused this reader
        self.assertEqual(runtime.original_refusal_kind(handle, tct.OUTSIDER), co.REFUSED)
        # the chat's guest is excluded: decided by a usable guard
        excluded = self.attachment_runtime(bound_guard(tct.U_OTHER, tct.OTHER))
        self.assertEqual(excluded.original_refusal_kind(handle, tct.MEMBER), co.REFUSED)
        # a guard that cannot decide refuses every captured message: a failure
        undecided = self.attachment_runtime(
            PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assertEqual(undecided.original_refusal_kind(handle, tct.MEMBER), co.FAILED)
        # an unreadable Teams store: a failure, never a refusal
        from integrations.capture_teams import DB_FILE, STORE_DIR
        (Path(self.tmp.name) / STORE_DIR / DB_FILE).write_bytes(b"not a database")
        for actor in (tct.MEMBER, tct.OUTSIDER):
            self.assertEqual(runtime.original_refusal_kind(handle, actor), co.FAILED)

    def test_a_chat_whose_people_cannot_be_decided_counts_nothing(self):
        # An unbound guard decides ids only, not the chat's addresses: fail closed.
        runtime = self.attachment_runtime(PeopleExclusion.from_env(tct.ENV))
        self.assertEqual(runtime.captured_originals.search(
            tct.OUTSIDER, 50, words=["grants"])["withheld"], {})


class CardReportTests(unittest.TestCase):
    def test_cards_on_advisory_only_maps_are_named_apart_from_ungated_originals(self):
        from tempfile import TemporaryDirectory
        from scripts.eval_case_originals_coverage import card_dependencies

        def card_set(root, case, cards):
            (root / case).mkdir()
            (root / case / "proposed_items.json").write_text(json.dumps(
                {"real_example": {"cards": cards}}), encoding="utf-8")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            card_set(root, "maps", [{"id": "M1", "cites": ["advisory:abc"]}])
            card_set(root, "briefs", [{"id": "B1", "originals": [CHAT]},
                                      {"id": "B2", "originals": [BRIEF, CHAT]},
                                      {"id": "B3", "originals": ["co-000000000000"]}])
            card_set(root, "watched", [{"id": "W1", "cites": ["advisory:def"]}])
            report = card_dependencies(root, {
                CHAT: "unknown_acl:source_not_captured:teams_chat",
                BRIEF: "servable:sharepoint"}, watched=["x/watched/proposed_items.json"])
        self.assertEqual(report["maps"], {"advisory_only": {"cards": 1, "ids": ["M1"]}})
        self.assertEqual({s: v["ids"] for s, v in report["briefs"].items()},
                         {"unknown_acl": ["B1"], "servable": ["B2"], "not_held": ["B3"]})
        self.assertEqual(list(report["watched"]), ["in_watched_source"])


if __name__ == "__main__":
    unittest.main()
