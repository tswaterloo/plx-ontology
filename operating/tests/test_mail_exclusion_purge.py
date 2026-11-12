"""INT-018: mail an excluded person was only Bcc'd on, captured before Graph reads selected
``bccRecipients``, is found and purged from the capture store and every derived copy.

A fake Graph that answers only the selected fields (as Graph does) holds a message the
mailbox owner sent with the excluded person in Bcc, its received copy in a second
mailbox and a control message. Captured with the old ``$select`` (the bug), projected
into a SQLite ontology store and loaded into a scratch Postgres; ``scan`` re-reads the
candidates through the GET-only client and records only the refused ids, ``purge``
without ``--apply`` changes nothing, with it the message, its copy, their attachment and
every row derived from them are gone and the control message stays everywhere.
Injected transport.
"""

from __future__ import annotations

import contextlib
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import unittest
from unittest import mock

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.live_source_guard import LIVE_AUDIENCE_SCHEMA, BoundedBackoff
from integrations.tests import test_capture_mail as tm
from ontology.project import object_id_of
from operating import host
from operating import mail_exclusion_purge as mp
from operating.tests import pg_testdb

EXCLUDED = tm.ENV["PLX_EXCLUDED_PEOPLE_OIDS"]
EXCLUDED_MAIL = "quiet.watcher@example-plx.com"
OLD_SELECT = graph_read._MAIL_SELECT.replace("bccRecipients,", "")


def bcc_graph() -> tm.FakeGraph:
    graph = tm.FakeGraph()
    graph.strict_select = True
    graph.users[EXCLUDED] = {"id": EXCLUDED, "userPrincipalName": EXCLUDED_MAIL,
                             "mail": EXCLUDED_MAIL, "proxyAddresses": [f"SMTP:{EXCLUDED_MAIL}"]}
    # Sent by mailbox A to B, the excluded person only in Bcc; B holds the received copy.
    graph.put(tm.BOX_A, "M-SENT", "CK-1", subject="Quarter plan", body="Plan attached.",
              attachments=[tm.file_attachment("ATT-S", b"plan bytes\n" * 4, "plan.txt")])
    sent = graph.messages[tm.BOX_A]["M-SENT"]
    sent["from"] = {"emailAddress": {"address": tm.UPN_A}}
    sent["toRecipients"] = [{"emailAddress": {"address": tm.UPN_B}}]
    sent["bccRecipients"] = [{"emailAddress": {"address": EXCLUDED_MAIL}}]
    graph.put(tm.BOX_B, "M-COPY", "CK-1", subject="Quarter plan", body="Plan attached.")
    copy = graph.messages[tm.BOX_B]["M-COPY"]
    copy["from"] = {"emailAddress": {"address": tm.UPN_A}}
    copy["internetMessageId"] = sent["internetMessageId"]
    graph.put(tm.BOX_A, "M-KEEP", "CK-1", subject="Freight", body="Truck at nine.")
    # sent by A with a colleague (not excluded) in Bcc: kept by every check
    graph.put(tm.BOX_A, "M-BCC-OK", "CK-1", subject="Pallets", body="Two pallets.")
    graph.messages[tm.BOX_A]["M-BCC-OK"].update(
        {"from": {"emailAddress": {"address": tm.UPN_A}},
         "bccRecipients": [{"emailAddress": {"address": tm.UPN_B}}]})
    return graph


def capture(state: Path, graph: tm.FakeGraph, env: dict = tm.ENV) -> dict:
    store = MailCaptureStore(state, clock=tm.Clock())
    try:
        api, _ = graph_read.build_graph(env, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        reader = graph_read.GraphMailReader(api, tm.capture_audience())
        run = MailCapture(store, reader, tm.CAPTURE, sleep=lambda s: None).run(tm.SCOPE)
        assert run["complete"], run
        return run
    finally:
        store.close()


class BccPurgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        import psycopg
        from operating.pg import migrate
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            migrate.migrate(pg, values={"embed_model": "m", "embed_dim": 8})

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.graph = bcc_graph()
        with mock.patch.object(graph_read, "_MAIL_SELECT", OLD_SELECT):
            capture(self.state, self.graph)   # before the fix: Bcc never read
        with ExitStack() as stack:
            found = host.state_captures(self.state, stack,
                                        internal_domains=tm.SCOPE.internal_domains)
            host.build_projection(self.state, found)
        self.store_path = host.projection_path(self.state)
        from operating.pg import ontology_load
        ontology_load.load(self.store_path, self.dsn, workers=1, replace=True,
                           log=io.StringIO())
        self.handles = self.capture_handles()
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            pg.execute("DELETE FROM search.chunks")
            for n, handle in enumerate(sorted(h for hs in self.handles.values() for h in hs)):
                pg.execute(
                    "INSERT INTO search.chunks (chunk_id, source_handle, object_id, kind, "
                    "header, body, content_sha, audience_key, authority, updated_at) VALUES "
                    "(%s, %s, NULL, 'mail', 'h', 'b', %s, 'mail:x', 1, now())",
                    (f"c{n}", handle, f"{n:064x}"))

    def capture_handles(self) -> dict[str, set[str]]:
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        try:
            found: dict[str, set[str]] = {}
            for mid, handle in db.execute("SELECT message_id, l1_handle FROM messages"):
                found.setdefault(mid, set()).add(handle)
            for mid, handle in db.execute(
                    "SELECT m.message_id, a.l1_handle FROM attachments a JOIN messages m ON "
                    "m.l1_handle = a.message_handle WHERE a.l1_handle IS NOT NULL"):
                found[mid].add(handle)
            return found
        finally:
            db.close()

    def cli(self, *argv: str, environ: dict = tm.ENV) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mp.main(list(argv), environ=environ, sender=self.graph)
        return code, json.loads(out.getvalue())

    def held(self) -> dict[str, Any]:
        """What each copy holds of each message: capture rows, SQLite and Postgres
        objects citing its handles, search chunks citing them."""
        import psycopg
        result = {}
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        store = sqlite3.connect(self.store_path)
        try:
            with psycopg.connect(self.dsn) as pg:
                for mid, handles in self.handles.items():
                    hs = sorted(handles)
                    marks = ",".join("?" * len(hs))
                    result[mid] = (
                        db.execute("SELECT count(*) FROM messages WHERE message_id=?",
                                   (mid,)).fetchone()[0],
                        store.execute("SELECT count(DISTINCT object_id) FROM object_versions "
                                      f"WHERE l1_handle IN ({marks})", hs).fetchone()[0],
                        pg.execute("SELECT count(DISTINCT object_id) FROM "
                                   "ontology.object_versions WHERE l1_handle = ANY(%s)",
                                   (hs,)).fetchone()[0],
                        pg.execute("SELECT count(*) FROM search.chunks WHERE source_handle "
                                   "= ANY(%s)", (hs,)).fetchone()[0])
        finally:
            db.close()
            store.close()
        return result

    def test_bcc_only_mail_is_found_and_purged_from_every_copy(self):
        before = self.held()
        for mid in ("M-SENT", "M-COPY", "M-KEEP"):   # the bug: all were captured
            self.assertTrue(all(before[mid]), (mid, before[mid]))

        out = self.state / "mail-capture" / mp.SCAN_FILE
        audience = self.state.parent / "audience.json"
        audience.write_text(json.dumps({
            "schema": LIVE_AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {f"mail:{tm.UPN_A}": ["svc:mail-capture"],
                          f"mail:{tm.UPN_B}": ["svc:mail-capture"]},
            "classifications": {"mail:*": "restricted"}}))
        code, report = self.cli("scan", "--state-dir", str(self.state), "--live-audience",
                                str(audience))
        self.assertEqual(code, 0, report)
        self.assertEqual((report["matched"], report["undecided"], report["complete"]),
                         (1, 0, True), report)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        text = out.read_text().lower() + json.dumps(report).lower()
        self.assertNotIn(EXCLUDED_MAIL, text)   # ids only, never the excluded identity
        self.assertNotIn(EXCLUDED, text)
        self.assertEqual(json.loads(out.read_text())["matches"], [[tm.BOX_A, "M-SENT"]])
        # every Graph read of the scan was a GET of the exclusion fields only
        reads = [u for _m, u, _h in self.graph.calls if "bccRecipients" in u]
        self.assertTrue(reads)
        self.assertFalse([u for u in reads if "body" in u.split("$select=")[-1].split("&")[0]])

        # a generation's print index (object ids only), as the live generation has one
        prints = self.store_path.parent / "generations" / "00000001" / "objects.sqlite"
        email = {m: object_id_of("Email", "graph_mail.messages", {"id": m, "mailbox": b})
                 for b, m in ((tm.BOX_A, "M-SENT"), (tm.BOX_A, "M-KEEP"))}
        with sqlite3.connect(prints) as db:
            db.execute("CREATE TABLE bindings (binding TEXT PRIMARY KEY, key TEXT, "
                       "basis TEXT, objects INTEGER)")
            db.execute("CREATE TABLE objects (binding TEXT, object_id TEXT, print BLOB, "
                       "part TEXT, PRIMARY KEY (binding, object_id)) WITHOUT ROWID")
            db.executemany("INSERT INTO objects VALUES ('graph_mail.messages', ?, x'00', "
                           "NULL)", [(o,) for o in email.values()])
        self.assertEqual(mp.default_stores(self.state),
                         [self.store_path.parent / "generations" / "00000001" / n
                          for n in ("store.sqlite", "objects.sqlite")])
        purge = ("purge", "--state-dir", str(self.state), "--dsn", self.dsn)
        # fail closed: no exclusion list reads nothing; a file of another shape purges nothing
        calls = len(self.graph.calls)
        code, refused = self.cli("scan", "--state-dir", str(self.state), "--live-audience",
                                 str(audience), "--out", str(self.state / "other.json"),
                                 environ={k: v for k, v in tm.ENV.items()
                                          if not k.startswith("PLX_EXCLUDED")})
        self.assertEqual((code, refused["error"]), (2, "people_exclusion_unavailable"))
        self.assertFalse([u for _m, u, _h in self.graph.calls[calls:] if "/messages" in u])
        (self.state / "bad.json").write_text(json.dumps({"matches": [[tm.BOX_A, "M-KEEP"]]}))
        code, bad = self.cli(*purge, "--matches", str(self.state / "bad.json"), "--apply")
        self.assertEqual((code, bad["error"]), (2, "matches_file_invalid"))
        code, dry = self.cli(*purge)
        self.assertEqual(code, 0, dry)
        self.assertEqual(dry["messages_with_copies"], 2, dry)   # the sent copy and B's copy
        self.assertGreater(dry["postgres"]["ontology.objects"], 0, dry)
        self.assertGreater(dry["sqlite"][0]["objects"], 0, dry)
        self.assertEqual(self.held(), before)   # a dry run writes nothing

        code, applied = self.cli(*purge, "--apply")
        self.assertEqual(code, 0, applied)
        self.assertEqual(applied["capture"]["messages"], 2, applied)
        after = self.held()
        self.assertEqual(after["M-SENT"], (0, 0, 0, 0))
        self.assertEqual(after["M-COPY"], (0, 0, 0, 0))
        self.assertEqual(after["M-KEEP"], before["M-KEEP"])
        self.assertEqual(after["M-BCC-OK"], before["M-BCC-OK"])
        with sqlite3.connect(prints) as db:
            self.assertEqual([r[0] for r in db.execute("SELECT object_id FROM objects")],
                             [email["M-KEEP"]])
        import psycopg
        with psycopg.connect(self.dsn) as pg:   # recorded, so served caches refresh
            self.assertEqual(pg.execute("SELECT count(*) FROM ontology.load_state WHERE "
                                        "name LIKE 'purge:mail-exclusion:%'").fetchone()[0], 1)
        # run again: nothing left to remove, nothing else touched
        code, again = self.cli(*purge, "--apply")
        self.assertEqual((code, again["handles"]), (0, 0), again)
        self.assertEqual(self.held()["M-KEEP"], before["M-KEEP"])
        # the purge remembered the message: B's folders read afresh (A's are not, so no
        # read of the sender's copy shows the Bcc again) never store B's copy, which
        # carries no Bcc
        self.assertEqual(applied["withheld_keys_new"], 1, applied)
        with sqlite3.connect(self.state / "mail-capture" / "capture.db") as db:
            db.execute("DELETE FROM folders WHERE mailbox_id = ?", (tm.BOX_B,))
        capture(self.state, self.graph)
        self.assertEqual(stored(self.state), {"M-KEEP", "M-BCC-OK"})

    def test_copies_a_capture_purged_are_removed_from_every_derived_store(self):
        """A message the capture learns is withheld after a copy of it was stored and
        projected (B's copy, read before A's copy showed the excluded person in Bcc): the
        next run deletes the stored copies and journals their handles; the purge removes
        the derived objects (no scan needed), a dry run changes nothing, and the journal is
        emptied only once they are gone."""
        before = self.held()
        imid = self.graph.messages[tm.BOX_A]["M-SENT"]["internetMessageId"]
        store = MailCaptureStore(self.state, clock=tm.Clock())
        try:
            store.remember_withheld([graph_read.withheld_message_key(imid)])
        finally:
            store.close()
        run = capture(self.state, self.graph)   # its start purges both stored copies
        self.assertEqual(run["purged"]["messages"], 2, run["purged"])
        journal = self.journal()
        self.assertEqual(journal, self.handles["M-SENT"] | self.handles["M-COPY"])
        derived = self.held()
        for mid in ("M-SENT", "M-COPY"):   # gone from the capture store only
            self.assertEqual(derived[mid][0], 0)
            self.assertEqual(derived[mid][1:], before[mid][1:])
        purge = ("purge", "--state-dir", str(self.state), "--dsn", self.dsn)
        code, dry = self.cli(*purge)       # no scan file: the journal alone
        self.assertEqual(code, 0, dry)
        self.assertEqual((dry["scan"], dry["journalled_handles"], dry["handles"]),
                         ("absent", len(journal), len(journal)), dry)
        self.assertGreater(dry["postgres"]["ontology.objects"], 0, dry)
        self.assertEqual(self.held(), derived)
        self.assertEqual(self.journal(), journal)
        code, applied = self.cli(*purge, "--apply")
        self.assertEqual(code, 0, applied)
        self.assertEqual(applied["journal_forgotten"], len(journal), applied)
        after = self.held()
        self.assertEqual(after["M-SENT"], (0, 0, 0, 0))
        self.assertEqual(after["M-COPY"], (0, 0, 0, 0))
        self.assertEqual(after["M-KEEP"], before["M-KEEP"])
        self.assertEqual(after["M-BCC-OK"], before["M-BCC-OK"])
        self.assertEqual(self.journal(), set())
        # an explicit matches file that does not exist is refused, not read as empty
        code, missing = self.cli(*purge, "--matches", str(self.state / "nope.json"))
        self.assertEqual((code, missing["error"]), (2, "matches_missing"))

    def journal(self) -> set[str]:
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        try:
            return {h for (h,) in db.execute("SELECT l1_handle FROM purged_handles")}
        finally:
            db.close()


def stored(state: Path) -> set[str]:
    db = sqlite3.connect(state / "mail-capture" / "capture.db")
    try:
        return {m for (m,) in db.execute("SELECT message_id FROM messages")}
    finally:
        db.close()


class CaptureBccTests(unittest.TestCase):
    """The capture itself: Bcc is selected, checked and recorded."""

    def setUp(self):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"

    def test_a_message_with_the_excluded_person_only_in_bcc_is_never_captured(self):
        """An excluded person's mail is stored in no mailbox. B's copy
        carries no Bcc (Graph never says who was Bcc'd on a received copy), so the
        sender's copy is remembered by the SHA-256 of its internetMessageId only: a copy
        not yet stored is never stored, one stored before is purged by the next run, and
        no later run captures it again."""
        graph = bcc_graph()
        for _ in range(3):
            capture(self.state, graph)
            self.assertNotIn("M-SENT", stored(self.state))
        self.assertEqual(stored(self.state), {"M-KEEP", "M-BCC-OK"})
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        self.addCleanup(db.close)
        imid = graph.messages[tm.BOX_A]["M-SENT"]["internetMessageId"]
        self.assertEqual(db.execute("SELECT message_key FROM people_withheld").fetchall(),
                         [(graph_read.withheld_message_key(imid),)])
        self.assertNotIn(imid.encode(), (self.state / "mail-capture" / "capture.db")
                         .read_bytes())

    def test_a_listing_without_bcc_and_a_failed_full_read_hold_the_message(self):
        """A delta cursor stored before Bcc was selected lists messages without it; when
        the full read that would check it fails, the message is held undecided, not
        stored from the listing; once a full read succeeds it is dropped and remembered."""
        graph = bcc_graph()
        graph.fail_get = {r".*/messages/M-SENT": 10**6}
        store = MailCaptureStore(self.state, clock=tm.Clock())
        try:
            api, _ = graph_read.build_graph(tm.ENV, sender=graph, retry=BoundedBackoff(
                sleep=lambda s: None, jitter=lambda: 0.0))
            reader = graph_read.GraphMailReader(api, tm.capture_audience())
            with mock.patch.object(graph_read, "_MAIL_SELECT", OLD_SELECT):
                run = MailCapture(store, reader, tm.CAPTURE, sleep=lambda s: None).run(tm.SCOPE)
        finally:
            store.close()
        self.assertNotIn("M-SENT", stored(self.state))
        self.assertGreaterEqual(sum(f["counts"]["people_undecided"]
                                    for b in run["mailboxes"] for f in b["folders"]), 1, run)
        graph.fail_get = {}
        for _ in range(2):
            capture(self.state, graph)
        self.assertEqual(stored(self.state), {"M-KEEP", "M-BCC-OK"})

    def test_a_later_exclusion_purges_a_stored_bcc_and_every_copy(self):
        earlier = {**tm.ENV, "PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e9"}
        graph = bcc_graph()
        capture(self.state, graph, earlier)
        self.assertEqual(stored(self.state), {"M-SENT", "M-COPY", "M-KEEP", "M-BCC-OK"})
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT json_extract(record, '$.bcc') FROM messages "
                                    "WHERE message_id = 'M-SENT'").fetchone()[0],
                         json.dumps([EXCLUDED_MAIL]).replace(" ", ""))
        run = capture(self.state, graph)   # the person is excluded now
        self.assertEqual(run["purged"]["messages"], 2, run["purged"])
        self.assertEqual(stored(self.state), {"M-KEEP", "M-BCC-OK"})


if __name__ == "__main__":
    unittest.main()
