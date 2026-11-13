"""Mail naming an address the exclusion cannot decide is withheld from every reader.

2026-11-12: a stored message addressed to a distribution list whose members cannot be
read (hidden membership) can neither be purged (the purge decides nothing on it) nor
cleared: an excluded person may be on the list. The purge records the address
(``undecided_addresses``, with the time it was first found so), the run reports how many
(``purge_undecided``), and every read of captured mail, for a reader or for anything
derived from the store, withholds a message from, to, cc or bcc one until a purge decides
it again. Injected transport only (the fake Graph of ``test_capture_mail_all_mailboxes``);
synthetic ids and ``example-plx.com`` addresses. Evidence label: ``injected-transport``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

from integrations.capture_mail import MailCaptureStore, undecided_free_sql
from integrations.live_source_guard import LiveSourceAudience
from integrations.tests.test_capture_mail_all_mailboxes import (
    AUDIENCE, SPEC, AllMailboxesCase, DirectoryGraph, lv, oid)
from integrations.tests.test_capture_mail_exclusion_retroactive import OTHER_ENV, cc
from ontology.types import Actor

LISTED = "hidden-list@example-plx.com"
GROUP = "0a0a0a0a-0000-4000-8000-0000000000a1"


class UndecidedListTests(AllMailboxesCase):
    def setUp(self) -> None:
        super().setUp()
        self.audience = LiveSourceAudience.from_file(AUDIENCE)
        self.actor = Actor(self.audience.tenant, frozenset({"svc:mail-capture"}),
                           "restricted")
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("PLX_EXCLUDED_PEOPLE_OIDS", "PLX_EXCLUDED_PEOPLE_FILE"):
            os.environ.pop(name, None)

    def raw(self, sql: str, *params) -> list:
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def handles(self, *message_ids: str) -> list[str]:
        marks = ",".join("?" * len(message_ids))
        return [h for (h,) in self.raw(
            f"SELECT l1_handle FROM messages WHERE message_id IN ({marks}) UNION SELECT "
            f"a.l1_handle FROM attachments a JOIN messages m ON m.l1_handle=a.message_handle "
            f"WHERE m.message_id IN ({marks}) AND a.l1_handle IS NOT NULL",
            *message_ids, *message_ids)]

    def captured(self, graph: DirectoryGraph) -> None:
        """Run 1, before the list is known to hide its members: everything is stored."""
        code, report = self.main(graph, "--runs", "1", environ=OTHER_ENV)
        self.assertEqual(code, 0, report)

    def hide(self, graph: DirectoryGraph) -> None:
        graph.groups = [{"id": GROUP, "mail": LISTED, "proxyAddresses": [],
                         "groupTypes": [], "hiddenMembership": True}]

    def served(self, store: MailCaptureStore, actor: Actor,
               audience: LiveSourceAudience, handles: list[str]) -> dict:
        """What every per-reader store read gives of ``handles`` (and their mailbox)."""
        message = next(h for h in handles if "#" not in h)
        record = json.loads(self.raw("SELECT record FROM messages WHERE l1_handle=?",
                                     message)[0][0])
        moment = datetime.fromisoformat(record["received_at"].replace("Z", "+00:00"))
        attachments = [h for h in handles if "#" in h]
        return {
            "messages": [r["l1_handle"] for r in store.messages(
                actor, audience, limit=1000)["items"] if r["l1_handle"] in handles],
            "index": [i["l1_handle"] for i in store.index(actor, audience)["items"]
                      if i["l1_handle"] in handles],
            "received_at": [h for h in store.received_at(actor, audience, moment)
                            if h in handles],
            "resolved": [h for h in handles if store.resolve(h, actor, audience, text=True)],
            "item_gate": [h for h in handles if store.item_gate(audience)(actor, h)],
            "history": store.history(actor, audience, message),
            "attachments": store.attachments(message, actor, audience),
            "attachment_content": [store.attachment_content(h, actor, audience)
                                   for h in attachments if store.attachment_content(
                                       h, actor, audience) is not None],
        }

    def owner(self) -> tuple[Actor, LiveSourceAudience]:
        audience = LiveSourceAudience(
            {"mail:*": ["staff:owner"]}, tenant="plx", classifications={"mail:*": "restricted"},
            identities={"staff:owner": [oid(3)]}, source_acl_required=["mail:*"])
        return Actor("plx", frozenset({"staff:owner"}), "restricted"), audience

    def test_mail_to_an_undecided_list_is_withheld_from_every_reader_then_released(self):
        from integrations.tests.test_capture_mail import file_attachment
        graph = DirectoryGraph(members=8)   # the live check's spec holds on the report
        graph.put(oid(3), "M-list", "CK-1", attachments=[file_attachment("AL")])
        graph.messages[oid(3)]["M-list"]["toRecipients"] = [
            {"emailAddress": {"address": LISTED}}]
        cc(graph, oid(3), "M-kept", to=["box1@example-plx.com"])
        self.captured(graph)
        listed, kept = self.handles("M-list"), self.handles("M-kept")
        self.assertEqual(len(listed), 2)   # the message and its attachment
        owner, owner_audience = self.owner()
        nothing = {"messages": [], "index": [], "received_at": [], "resolved": [],
                   "item_gate": [], "history": [], "attachments": [],
                   "attachment_content": []}
        store = MailCaptureStore(self.state)
        try:   # the control: before any purge found it undecided, it is served
            before = self.served(store, self.actor, self.audience, listed)
            self.assertTrue(all(before[k] for k in nothing), before)
            # the mailbox's own reader: no other mailbox's purge moves its counts
            counts = store.counts(owner, owner_audience)
            marking = store.owner_marking(owner, owner_audience)
        finally:
            store.close()

        self.hide(graph)
        code, report = self.main(graph)   # ENV: the guard bound again; two runs
        self.assertEqual(code, 0, report)
        self.assertEqual(report["summary"]["purge_undecided"], 1)
        self.assertEqual([r["purge_undecided"] for r in report["runs"]], [1, 1])
        self.assertEqual(lv.check_expectations(SPEC, code, report), [])
        self.assertNotIn(LISTED, json.dumps(report).lower())   # counts only
        [(address, since, checked)] = self.raw("SELECT * FROM undecided_addresses")
        self.assertEqual(address, LISTED)
        self.assertTrue(len(self.raw("SELECT 1 FROM messages WHERE message_id='M-list'")))

        store = MailCaptureStore(self.state)
        try:
            for reader in ((self.actor, self.audience), (owner, owner_audience)):
                self.assertEqual(self.served(store, *reader, listed), nothing)
                # the rest of the mailbox is served as before
                self.assertEqual(self.served(store, *reader, kept)["resolved"], kept)
            after = store.counts(owner, owner_audience)
            self.assertEqual(after["messages_current"], counts["messages_current"] - 1)
            self.assertEqual(after["versions"], counts["versions"] - 1)
            self.assertEqual(after["attachments_captured"],
                             counts["attachments_captured"] - 1)
            self.assertEqual(sum(store.owner_marking(owner, owner_audience).values()),
                             sum(marking.values()) - 1)
            box = next(m for m in store.completeness(self.actor, self.audience)["mailboxes"]
                       if m["mailbox"] == oid(3))
            self.assertEqual(box["current_messages"], 2)   # its own and M-kept
        finally:
            store.close()

        # Still undecided a run later: ``since`` is when it was first found so.
        code, report = self.main(graph, "--runs", "1")
        self.assertEqual(report["summary"]["purge_undecided"], 1)
        [(_a, since_again, _c)] = self.raw("SELECT * FROM undecided_addresses")
        self.assertEqual(since_again, since)

        # The list's members can be read and nobody on it is excluded: released.
        graph.groups[0]["hiddenMembership"] = False
        graph.group_members[GROUP] = [{"@odata.type": "#microsoft.graph.user",
                                            "id": oid(1)}]
        code, report = self.main(graph, "--runs", "1")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["summary"]["purge_undecided"], 0)
        self.assertEqual(self.raw("SELECT * FROM undecided_addresses"), [])
        store = MailCaptureStore(self.state)
        try:
            self.assertEqual(self.served(store, self.actor, self.audience, listed)["resolved"],
                             listed)
        finally:
            store.close()

    def test_derived_readers_of_the_store_withhold_it_too(self):
        """The consumers that read the capture's tables directly (who corresponds with
        whom, activity, the search gate's facts, the top-up scan) see none of it."""
        from ontology import people_directory as pd
        from operating import mail_topup, team_activity
        from operating.case_tools_host import CaptureCaseHostTools
        graph = DirectoryGraph(members=4)
        cc(graph, oid(3), "M-list", to=[LISTED, "box1@example-plx.com"],
           copy=["outside.party@vendor-x.example"])
        self.captured(graph)
        [handle] = self.handles("M-list")
        path = self.state / "mail-capture" / "capture.db"

        def derived() -> dict:
            store = MailCaptureStore(self.state)
            try:
                memo: dict = {}
                CaptureCaseHostTools._mail_facts(store, [handle], memo)
                top = store.db.execute("SELECT MAX(version_id) FROM messages").fetchone()[0]
                found, _cut = mail_topup.scan(
                    store.db, pin=0, mailboxes=[oid(3)],
                    addresses=frozenset({"box1@example-plx.com"}),
                    domains=frozenset(), terms=None, since=None, until=None,
                    tenant=frozenset({"example-plx.com"}))
            finally:
                store.close()
            slice_rows = team_activity._mail_slice((str(path), 0, top + 1, "2000-01-01"))
            return {
                "census": sorted(a for (a, box) in pd.sender_counts(path, [oid(3)])
                                 if a == "outside.party@vendor-x.example"),
                "search_facts": memo[("mail_facts", str(store.path), handle)] is not None,
                "topup": [f["handle"] for f in found],
                "activity": [r for r in slice_rows
                             if "outside.party@vendor-x.example" in r[6]],
            }
        before = derived()
        self.assertEqual((before["census"], before["search_facts"], before["topup"],
                          len(before["activity"])),
                         (["outside.party@vendor-x.example"], True, [handle], 1))
        self.hide(graph)
        code, report = self.main(graph, "--runs", "1")
        self.assertEqual(report["summary"]["purge_undecided"], 1)
        self.assertEqual(derived(), {"census": [], "search_facts": False, "topup": [],
                                     "activity": []})

    def test_the_predicate_matches_every_field_and_form_and_fails_closed(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE messages (version_id INTEGER PRIMARY KEY, record TEXT)")
        self.assertEqual(undecided_free_sql(db), "1")   # no purge has decided yet
        db.execute("CREATE TABLE undecided_addresses (address TEXT PRIMARY KEY, since TEXT, "
                   "checked_at TEXT)")
        self.assertEqual(undecided_free_sql(db), "1")   # none undecided
        db.execute("INSERT INTO undecided_addresses VALUES (?, 't', 't')", (LISTED,))
        records = {
            "from": {"from": " Hidden-List@EXAMPLE-plx.com"}, "to": {"to": [LISTED]},
            "cc": {"cc": ["a@example-plx.com", LISTED.upper()]}, "bcc": {"bcc": [LISTED]},
            "to_scalar": {"to": LISTED},
            "clear": {"from": "a@example-plx.com", "to": ["b@example-plx.com"], "cc": None},
            "empty": {}}
        for name, record in records.items():
            db.execute("INSERT INTO messages (record) VALUES (?)", (json.dumps(record),))
        free = undecided_free_sql(db)
        names = list(records)
        kept = [names[v - 1] for (v,) in db.execute(
            f"SELECT version_id FROM messages WHERE {free}")]
        self.assertEqual(kept, ["clear", "empty"])
        with self.assertRaises(ValueError):
            undecided_free_sql(db, "m); DROP TABLE messages; --")
        db.execute("INSERT INTO messages (record) VALUES ('not json')")
        with self.assertRaises(sqlite3.OperationalError):   # an unreadable row: no read
            db.execute(f"SELECT version_id FROM messages WHERE {free}").fetchall()

    def test_withholding_scales_without_losing_or_doubling_a_row(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.state = Path(self.tmp.name) / f"state-{n}"
                graph = DirectoryGraph(members=2)
                for k in range(n):
                    cc(graph, oid(1), f"M-list-{k}", copy=[LISTED])
                    cc(graph, oid(1), f"M-other-{k}", to=["box2@example-plx.com"])
                self.captured(graph)
                self.hide(graph)
                started = time.monotonic()
                code, report = self.main(graph, "--runs", "1")
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(report["summary"]["purge_undecided"], 1)
                store = MailCaptureStore(self.state)
                try:
                    started = time.monotonic()
                    seen = store.messages(self.actor, self.audience, limit=1000)
                    self.assertLess(time.monotonic() - started, 2)
                    ids = [json.loads(self.raw("SELECT record FROM messages WHERE "
                                               "l1_handle=?", r["l1_handle"])[0][0])["id"]
                           for r in seen["items"]]
                finally:
                    store.close()
                self.assertEqual(seen["total"], len(ids))
                self.assertEqual(len(ids), len(set(ids)))
                self.assertFalse([i for i in ids if i.startswith("M-list-")])
                self.assertEqual(len([i for i in ids if i.startswith("M-other-")]), n)


if __name__ == "__main__":
    unittest.main()
