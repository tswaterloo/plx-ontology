"""The capture's exclusion purge reads the whole store only when it must (mail-fast-1113).

Every mail run used to start with ``MailCaptureStore.purge_withheld`` re-reading every
stored record (live 11-13: about 84 GB of reads in a run's first two minutes, new mail
only every 60+ minutes). Now the records are read whole only when the exclusion differs
from the one the last purge ran under, and whenever the recorded state cannot be trusted
(fail closed); otherwise only the versions stored since. These tests hold the privacy
side: a newly excluded person's old mail still goes, a corrupt or missing state reads the
whole store, a key remembered later still finds an old copy, an address held undecided
that becomes excluded reads the whole store, and a record rewritten under the purge drops
the state. Injected transport only (the fake Graph of ``test_capture_mail_all_mailboxes``);
synthetic ids and ``example-plx.com`` addresses. Evidence label: ``injected-transport``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from unittest import mock

from integrations import capture_mail
from integrations.capture_mail import MailCaptureStore
from integrations.graph_read import withheld_message_key
from integrations.people_exclusion import PeopleExclusion
from integrations.tests.test_capture_mail_all_mailboxes import (
    EXCLUDED, EXCLUDED_ALIAS, EXCLUDED_UPN, AllMailboxesCase, DirectoryGraph, oid)
from integrations.tests.test_capture_mail_exclusion_retroactive import OTHER_ENV, cc

OTHER = OTHER_ENV["PLX_EXCLUDED_PEOPLE_OIDS"]
ZERO = dict.fromkeys(capture_mail.PURGE_COUNTS, 0)


class _Api:
    """``get_pages`` over the fake directory's ``/users`` listing (complete)."""

    def __init__(self, graph: DirectoryGraph) -> None:
        self.users = [{"id": u["_listed_id"], "mail": u["mail"],
                       "userPrincipalName": u["userPrincipalName"],
                       "proxyAddresses": u["proxyAddresses"]} for u in graph.users.values()]

    def get_pages(self, url, **_kw):
        return list(self.users), {"complete": True}


class IncrementalPurgeTests(AllMailboxesCase):
    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("PLX_EXCLUDED_PEOPLE_OIDS", "PLX_EXCLUDED_PEOPLE_FILE"):
            os.environ.pop(name, None)
        self.graph = DirectoryGraph(members=8)
        cc(self.graph, oid(1), "M-cc-old", copy=[EXCLUDED_ALIAS])
        cc(self.graph, oid(2), "M-plain", to=["box1@example-plx.com"])
        code, report = self.main(self.graph, "--runs", "1", environ=OTHER_ENV)
        self.assertEqual(code, 0, report)

    # ------------------------------------------------------------------ helpers

    def guard(self, *ids: str) -> PeopleExclusion:
        people = PeopleExclusion(ids)
        people.bind(_Api(self.graph))
        return people

    def raw(self, sql: str, *params) -> list:
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        try:
            with db:
                return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def purge(self, people: PeopleExclusion) -> tuple[dict, list[str]]:
        """One purge; the statements it ran that read message records with no
        ``version_id`` bound (a read of every record)."""
        store = MailCaptureStore(self.state)
        seen: list[str] = []
        store.db.set_trace_callback(seen.append)
        try:
            counts = store.purge_withheld(people)
        finally:
            store.db.set_trace_callback(None)
            store.close()
        whole = [s for s in seen if "FROM messages" in s and "record" in s
                 and "version_id >" not in s and "version_id=" not in s
                 and "l1_handle" not in s and "temp.copy_pairs" not in s]
        return counts, whole

    def plant(self, message_id: str, address: str) -> None:
        """Rewrite a stored record below the purge's mark to name ``address`` (by SQL, as
        nothing in the capture does): what an incremental purge must not look at."""
        [(record,)] = self.raw("SELECT record FROM messages WHERE message_id=?", message_id)
        value = json.loads(record)
        value["cc"] = [*value.get("cc", []), address]
        self.raw("UPDATE messages SET record=? WHERE message_id=?", json.dumps(value),
                 message_id)

    def stored(self, message_id: str) -> int:
        return self.raw("SELECT COUNT(*) FROM messages WHERE message_id=?", message_id)[0][0]

    def state_value(self):
        row = self.raw("SELECT value FROM meta WHERE key=?", capture_mail._PURGE_STATE)
        return json.loads(row[0][0]) if row else None

    # ------------------------------------------------------------------ tests

    def test_a_newly_excluded_persons_old_mail_is_purged_by_a_whole_store_read(self):
        # The first run purged under another list and recorded its state.
        before = self.state_value()
        self.assertIsNotNone(before)
        self.assertEqual(self.stored("M-cc-old"), 1)
        self.purge(self.guard(OTHER))   # this guard's state (no groups listing read)
        _, whole = self.purge(self.guard(OTHER))   # the same list: nothing read whole
        self.assertEqual(whole, [])
        counts, whole = self.purge(self.guard(EXCLUDED))   # the person added
        self.assertNotEqual(whole, [])
        self.assertEqual(self.stored("M-cc-old"), 0)
        self.assertGreaterEqual(counts["messages"], 2)   # their own mailbox and the cc
        self.assertNotEqual(self.state_value()["digest"], before["digest"])
        text = " ".join(r for (r,) in self.raw("SELECT record FROM messages")).lower()
        for token in (EXCLUDED_UPN, EXCLUDED_ALIAS):
            self.assertNotIn(token, text)

    def test_an_unchanged_list_reads_only_new_mail(self):
        people = self.guard(EXCLUDED)
        self.purge(people)
        through = self.state_value()["through"]
        # Old mail rewritten behind the purge's back is not re-read: the proof the
        # whole-store read was skipped (the capture never rewrites who a record names).
        self.plant("M-plain", EXCLUDED_UPN)
        counts, whole = self.purge(self.guard(EXCLUDED))
        self.assertEqual((counts, whole), (ZERO, []))
        self.assertEqual(self.stored("M-plain"), 1)
        # New mail naming the person, stored after the mark, is read and purged (the
        # capture refuses such mail when it reads it: planted as a later version).
        [(row,)] = self.raw("SELECT version_id FROM messages WHERE message_id='M-plain'")
        self.raw("INSERT INTO messages (l1_handle, mailbox_id, message_id, change_key, "
                 "folder_id, record, digest, body_status, recorded_at, last_seen_at) "
                 "SELECT l1_handle || '-new', mailbox_id, 'M-planted-new', change_key, "
                 "folder_id, record, digest, body_status, recorded_at, last_seen_at FROM "
                 "messages WHERE version_id=?", row)
        counts, whole = self.purge(self.guard(EXCLUDED))
        self.assertEqual(whole, [])
        self.assertEqual(self.stored("M-planted-new"), 0)
        self.assertEqual(self.stored("M-plain"), 1)   # older than the mark: not re-read
        self.assertGreater(self.state_value()["through"], through)

    def test_a_corrupt_missing_or_foreign_state_reads_the_whole_store(self):
        self.purge(self.guard(EXCLUDED))
        targets = [m for (m,) in self.raw(
            "SELECT DISTINCT message_id FROM messages WHERE message_id LIKE 'M-%-1' "
            "ORDER BY message_id")]
        self.assertGreaterEqual(len(targets), 7)
        for label, change in (
                ("corrupt", lambda: self.raw("UPDATE meta SET value='{not json' WHERE key=?",
                                             capture_mail._PURGE_STATE)),
                ("missing", lambda: self.raw("DELETE FROM meta WHERE key=?",
                                             capture_mail._PURGE_STATE)),
                ("ahead", lambda: self.raw(
                    "UPDATE meta SET value=json_set(value, '$.through', 10000000) WHERE key=?",
                    capture_mail._PURGE_STATE)),
                ("wrong type", lambda: self.raw(
                    "UPDATE meta SET value=json_set(value, '$.through', '5') WHERE key=?",
                    capture_mail._PURGE_STATE)),
                ("keys lost", lambda: self.raw("DROP TABLE message_keys")),
                ("undecided lost", lambda: self.raw(
                    "UPDATE meta SET value=json_set(value, '$.undecided', 3) WHERE key=?",
                    capture_mail._PURGE_STATE)),
                ("other version", lambda: self.raw(
                    "UPDATE meta SET value=json_set(value, '$.v', 2) WHERE key=?",
                    capture_mail._PURGE_STATE))):
            with self.subTest(label):
                target = targets.pop()
                self.plant(target, EXCLUDED_UPN)
                change()
                counts, whole = self.purge(self.guard(EXCLUDED))
                self.assertNotEqual(whole, [])
                self.assertEqual(self.stored(target), 0)
                self.assertEqual(counts["messages"], 1)
                self.assertIsNotNone(self.state_value())   # a valid state again
                _, whole = self.purge(self.guard(EXCLUDED))
                self.assertEqual(whole, [])

    def test_purging_the_newest_versions_keeps_the_next_purge_incremental(self):
        self.purge(self.guard(EXCLUDED))
        [(newest,)] = self.raw("SELECT MAX(version_id) FROM messages")
        [(target,)] = self.raw("SELECT message_id FROM messages WHERE version_id=?", newest)
        self.plant(target, EXCLUDED_UPN)
        self.raw("DELETE FROM meta WHERE key=?", capture_mail._PURGE_STATE)
        self.purge(self.guard(EXCLUDED))   # whole: deletes the newest version
        self.assertEqual(self.stored(target), 0)
        _, whole = self.purge(self.guard(EXCLUDED))
        self.assertEqual(whole, [])

    def test_a_rebuild_cut_short_leaves_no_state_to_trust(self):
        self.purge(self.guard(EXCLUDED))
        [(record,)] = self.raw("SELECT record FROM messages WHERE message_id='M-plain'")
        key = withheld_message_key(json.loads(record)["internet_message_id"])
        self.raw("DELETE FROM message_keys WHERE version_id > 0 AND version_id IN "
                 "(SELECT MAX(version_id) FROM message_keys)")   # keys ahead: no state used
        store = MailCaptureStore(self.state)
        try:
            store.remember_withheld([key])
            with mock.patch.object(capture_mail, "withheld_message_key",
                                   side_effect=MemoryError):   # killed mid-rebuild
                with self.assertRaises(MemoryError):
                    store._key_messages(None, store._version_mark())
        finally:
            store.close()
        self.assertIsNone(self.state_value())
        counts, whole = self.purge(self.guard(EXCLUDED))
        self.assertNotEqual(whole, [])
        self.assertEqual(self.stored("M-plain"), 0)

    def test_the_key_table_never_counts_toward_the_store_bound(self):
        store = MailCaptureStore(self.state)
        try:
            store.db.execute("DROP TABLE IF EXISTS message_keys")
            store._keys_bytes = None
            before = store.store_bytes()
            store._key_messages(None, store._version_mark())
            self.assertGreater(store.db.execute("SELECT COUNT(*) FROM message_keys"
                                                ).fetchone()[0], 0)
            self.assertLessEqual(store.store_bytes(), before)
        finally:
            store.close()

    def test_a_guard_without_a_digest_always_reads_the_whole_store(self):
        people = self.guard(EXCLUDED)
        self.purge(people)
        self.plant("M-plain", EXCLUDED_UPN)
        guard = self.guard(EXCLUDED)
        del guard._addresses   # a guard this purge cannot read: no digest
        self.assertIsNone(capture_mail._purge_digest(guard))
        with mock.patch.object(capture_mail, "_purge_digest", return_value=None):
            counts, whole = self.purge(self.guard(EXCLUDED))
        self.assertNotEqual(whole, [])
        self.assertEqual(self.stored("M-plain"), 0)
        self.assertIsNone(self.state_value())

    def test_a_key_remembered_later_finds_an_old_copy_without_a_whole_read(self):
        self.purge(self.guard(EXCLUDED))
        [(record,)] = self.raw("SELECT record FROM messages WHERE message_id='M-plain'")
        key = withheld_message_key(json.loads(record)["internet_message_id"])
        self.assertTrue(key)
        store = MailCaptureStore(self.state)
        try:
            store.remember_withheld([key])
        finally:
            store.close()
        counts, whole = self.purge(self.guard(EXCLUDED))
        self.assertEqual(whole, [])
        self.assertEqual(self.stored("M-plain"), 0)
        self.assertEqual(counts["messages"], 1)

    def test_a_record_rewritten_with_other_recipients_drops_the_state(self):
        self.purge(self.guard(EXCLUDED))
        [(handle, record, digest)] = self.raw("SELECT l1_handle, record, digest FROM "
                                              "messages WHERE message_id='M-plain'")
        value = json.loads(record)
        store = MailCaptureStore(self.state)
        try:   # the same recipients: the state holds
            store.fill_body(handle, {**value, "content_sha256": digest}, "complete")
            self.assertIsNotNone(self.state_value())
            value["cc"] = [EXCLUDED_UPN]
            store.fill_body(handle, {**value, "content_sha256": digest}, "complete")
        finally:
            store.close()
        self.assertIsNone(self.state_value())
        counts, whole = self.purge(self.guard(EXCLUDED))
        self.assertNotEqual(whole, [])
        self.assertEqual(self.stored("M-plain"), 0)

    def test_an_address_held_undecided_that_becomes_excluded_reads_the_whole_store(self):
        self.purge(self.guard(EXCLUDED))
        address = "list.one@example-plx.com"
        self.plant("M-plain", address)
        self.raw("CREATE TABLE IF NOT EXISTS undecided_addresses (address TEXT PRIMARY KEY, "
                 "since TEXT NOT NULL, checked_at TEXT NOT NULL)")
        self.raw("INSERT INTO undecided_addresses VALUES (?, 'x', 'x')", address)
        people = self.guard(EXCLUDED)
        real = people.is_excluded
        with mock.patch.object(people, "is_excluded",
                               side_effect=lambda a: a == address or real(a)):
            counts, whole = self.purge(people)
        self.assertNotEqual(whole, [])
        self.assertEqual(self.stored("M-plain"), 0)

    def test_a_list_whose_members_cannot_be_confirmed_changes_the_digest(self):
        # An address decided earlier (a cleared list) that a later listing cannot confirm
        # must be re-decided everywhere (held undecided, withheld from every reader): the
        # digest moves, so the next purge reads the whole store.
        people = self.guard(EXCLUDED)
        before = capture_mail._purge_digest(people)
        people._groups = {"list.one@example-plx.com": False}
        cleared = capture_mail._purge_digest(people)
        people._groups = {"list.one@example-plx.com": None}
        unsure = capture_mail._purge_digest(people)
        people._groups = {"list.one@example-plx.com": True}
        excluded = capture_mail._purge_digest(people)
        self.assertEqual(len({before, cleared, unsure, excluded}), 4)
        self.assertEqual(capture_mail._purge_digest(self.guard(EXCLUDED)), before)   # stable
        self.assertNotEqual(capture_mail._purge_digest(self.guard(OTHER)), before)
        for value in (before, cleared, unsure, excluded):
            for token in (EXCLUDED, EXCLUDED_UPN, EXCLUDED_ALIAS):
                self.assertNotIn(token, value)

    def test_n_1_10_60_new_messages_each_purged_once_in_the_window(self):
        self.purge(self.guard(EXCLUDED))
        [(row,)] = self.raw("SELECT version_id FROM messages WHERE message_id='M-plain'")
        self.plant("M-plain", EXCLUDED_UPN)   # old: outside the window, kept
        total = 0
        for n in (1, 10, 60):
            with self.subTest(n=n):
                for k in range(n):
                    self.raw(
                        "INSERT INTO messages (l1_handle, mailbox_id, message_id, "
                        "change_key, folder_id, record, digest, body_status, recorded_at, "
                        "last_seen_at) SELECT l1_handle || ?, mailbox_id, ?, change_key, "
                        "folder_id, record, digest, body_status, recorded_at, last_seen_at "
                        "FROM messages WHERE version_id=?", f"-{n}-{k}", f"M-new-{n}-{k}",
                        row)
                started = time.monotonic()
                counts, whole = self.purge(self.guard(EXCLUDED))
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(whole, [])
                self.assertEqual(counts["messages"], n)
                self.assertEqual(self.raw("SELECT COUNT(*) FROM messages WHERE message_id "
                                          "LIKE 'M-new-%'"), [(0,)])
                total += n
        self.assertEqual(total, 71)
        self.assertEqual(self.stored("M-plain"), 1)


if __name__ == "__main__":
    unittest.main()
