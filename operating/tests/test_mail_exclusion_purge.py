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


def carried_zip(sender: str, to: str) -> tuple[bytes, bytes]:
    """(a zip carrying one mail from ``sender`` to ``to`` and a text file, that mail)."""
    from zipfile import ZipFile
    mail = (f"From: {sender}\r\nTo: {to}\r\nSubject: quote\r\n\r\nPrice 12.50 a box."
            ).encode()
    out = io.BytesIO()
    with ZipFile(out, "w") as archive:
        archive.writestr("fwd.eml", mail)
        archive.writestr("note.txt", b"Forty-two boxes on the dock.")
    return out.getvalue(), mail


class CarriedPurgeTests(unittest.TestCase):
    """DOC-008: containers stored before the capture-time drop that carry an excluded
    person's mail (or one that cannot be cleared) are found offline and withheld, with
    every derived copy; the message and every cleared container stay."""

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
        graph = self.graph = tm.FakeGraph()
        graph.users[EXCLUDED] = {"id": EXCLUDED, "userPrincipalName": EXCLUDED_MAIL,
                                 "mail": EXCLUDED_MAIL,
                                 "proxyAddresses": [f"SMTP:{EXCLUDED_MAIL}"]}
        self.zips = {"ATT-BAD": carried_zip(EXCLUDED_MAIL, tm.UPN_A),
                     "ATT-UNSURE": carried_zip("who@example-plx.com", tm.UPN_A),
                     "ATT-OK": carried_zip(tm.UPN_B, tm.UPN_A)}
        graph.put(tm.BOX_A, "M-1", "CK-1", subject="Quotes", body="See attached.",
                  attachments=[tm.file_attachment(a, z, f"{a.lower()}.zip")
                               for a, (z, _m) in self.zips.items()])
        graph.put(tm.BOX_A, "M-2", "CK-1", subject="Freight", body="Truck at nine.")
        with mock.patch.object(MailCapture, "_carries_withheld_mail",
                               lambda _self, _c, _r: (None, None)):   # before the rule
            capture(self.state, graph)
        with ExitStack() as stack:
            found = host.state_captures(self.state, stack,
                                        internal_domains=tm.SCOPE.internal_domains)
            host.build_projection(self.state, found)
        self.store_path = host.projection_path(self.state)
        from operating.pg import ontology_load
        ontology_load.load(self.store_path, self.dsn, workers=1, replace=True,
                           log=io.StringIO())
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        try:
            self.handles = dict(db.execute("SELECT attachment_id, l1_handle FROM attachments"))
            self.message = db.execute("SELECT l1_handle FROM messages WHERE message_id='M-1'"
                                      ).fetchone()[0]
        finally:
            db.close()
        # the directory the capture recorded, as the offline guard reads it
        self.directory = mock.patch(
            "integrations.people_exclusion.mail_capture_directory",
            lambda _state, now=None: [(tm.BOX_A, tm.UPN_A, []), (tm.BOX_B, tm.UPN_B, []),
                                      (EXCLUDED, EXCLUDED_MAIL, [])])
        self.directory.start()
        self.addCleanup(self.directory.stop)

    def cli(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mp.main(list(argv), environ=tm.ENV, sender=self.graph)
        return code, json.loads(out.getvalue())

    def test_a_bound_guard_decides_the_tenant_address_the_directory_does_not_hold(self):
        """Offline, a tenant address the recorded directory does not hold withholds its
        container; bound through Graph (directory reads only) it is decided: only the
        excluded person's mail is withheld."""
        audience = self.state.parent / "audience.json"
        audience.write_text(json.dumps({
            "schema": LIVE_AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {f"mail:{tm.UPN_A}": ["svc:mail-capture"],
                          f"mail:{tm.UPN_B}": ["svc:mail-capture"]},
            "classifications": {"mail:*": "restricted"}}))
        out = self.state.parent / "bound.json"
        code, scan = self.cli("carried", "--state-dir", str(self.state), "--live-audience",
                              str(audience), "--out", str(out), "--workers", "1")
        self.assertEqual(code, 0, scan)
        self.assertEqual((scan["excluded"], scan["undecided"], scan["clear"]), (1, 0, 2), scan)
        self.assertEqual(json.loads(out.read_text())["withheld"],
                         {self.handles["ATT-BAD"]: "excluded:people"})
        self.assertTrue(all(m == "GET" for m, _u, *_ in self.graph.calls
                            if "login" not in _u), self.graph.calls)

    def held(self) -> dict[str, tuple[int, int, int]]:
        """Per attachment: SQLite objects and Postgres objects citing it or a member of
        it, and whether its capture row still holds bytes."""
        import psycopg
        result = {}
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        store = sqlite3.connect(self.store_path)
        try:
            with psycopg.connect(self.dsn) as pg:
                for att, handle in self.handles.items():
                    like = handle.replace("%", "\\%").replace("_", "\\_") + "%"
                    result[att] = (
                        store.execute("SELECT count(DISTINCT object_id) FROM object_versions "
                                      "WHERE l1_handle LIKE ? ESCAPE '\\'", (like,)
                                      ).fetchone()[0],
                        pg.execute("SELECT count(DISTINCT object_id) FROM "
                                   "ontology.object_versions WHERE l1_handle LIKE %s",
                                   (like,)).fetchone()[0],
                        db.execute("SELECT count(*) FROM attachments WHERE attachment_id=? "
                                   "AND content_sha256 IS NOT NULL", (att,)).fetchone()[0])
                result["M-1"] = (store.execute(
                    "SELECT count(*) FROM object_versions WHERE l1_handle=?",
                    (self.message,)).fetchone()[0], pg.execute(
                    "SELECT count(*) FROM ontology.object_versions WHERE l1_handle=%s",
                    (self.message,)).fetchone()[0], db.execute(
                    "SELECT count(*) FROM messages WHERE message_id='M-1'").fetchone()[0])
        finally:
            db.close()
            store.close()
        return result

    def test_stored_containers_carrying_unclear_mail_are_withheld_with_every_copy(self):
        before = self.held()
        for att in self.handles:   # attachment and members derived, bytes stored
            self.assertGreaterEqual(before[att][0], 2, (att, before))
            self.assertEqual(before[att][2], 1)
        state = str(self.state)
        code, scan = self.cli("carried", "--state-dir", state, "--workers", "2")
        self.assertEqual(code, 0, scan)
        self.assertEqual((scan["excluded"], scan["undecided"], scan["withheld_attachments"],
                          scan["carrying_mail"]), (1, 1, 2, 3), scan)
        out = json.loads((self.state / "mail-capture" / mp.CARRIED_FILE).read_text())
        self.assertEqual(out["withheld"], {self.handles["ATT-BAD"]: "excluded:people",
                                           self.handles["ATT-UNSURE"]:
                                           "withheld:carried_mail_undecided"})
        for address in (EXCLUDED_MAIL, "who@", tm.UPN_B):   # handles and counts only
            self.assertNotIn(address, json.dumps(out))
        self.assertEqual(self.held(), before)            # a scan without --apply reads only
        code, dry = self.cli("purge", "--state-dir", state, "--dsn", self.dsn)
        self.assertEqual((code, dry["carried_attachments"]), (0, 2), dry)
        self.assertEqual(self.held(), before)
        code, applied = self.cli("purge", "--state-dir", state, "--dsn", self.dsn, "--apply")
        self.assertEqual(code, 0, applied)
        self.assertEqual(applied["carried_capture"]["attachments"], 2, applied)
        after = self.held()
        self.assertEqual(after["ATT-BAD"], (0, 0, 0))
        self.assertEqual(after["ATT-UNSURE"], (0, 0, 0))
        self.assertEqual(after["ATT-OK"], before["ATT-OK"])
        self.assertEqual(after["M-1"], before["M-1"])      # the message itself stays
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        try:
            rows = dict(db.execute("SELECT attachment_id, content_status || '|' || "
                                   "ifnull(l1_handle, '-') FROM attachments"))
            self.assertEqual(rows["ATT-BAD"], "excluded:people|-")
            self.assertEqual(rows["ATT-UNSURE"], "withheld:carried_mail_undecided|-")
            self.assertEqual(db.execute("SELECT count(*) FROM purged_handles").fetchone()[0], 0)
        finally:
            db.close()
        from hashlib import sha256
        blobs = {p.name.split(".")[0] for p in (self.state / "mail-capture").rglob("*")
                 if p.is_file()}
        self.assertNotIn(sha256(self.zips["ATT-BAD"][0]).hexdigest(), blobs)
        self.assertIn(sha256(self.zips["ATT-OK"][0]).hexdigest(), blobs)
        # with --apply the walks are kept (cycle lock held) and read back next time
        code, kept = self.cli("carried", "--state-dir", state, "--apply")
        self.assertEqual((code, kept["withheld_attachments"], kept["kept_new"]), (0, 0, 1),
                         kept)
        code, again = self.cli("carried", "--state-dir", state)
        self.assertEqual((again["kept_walks"], again["walked"]), (1, 0), again)

    def test_an_unavailable_exclusion_decides_nothing_and_writes_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mp.main(["carried", "--state-dir", str(self.state)],
                           environ={k: v for k, v in tm.ENV.items()
                                    if k != "PLX_EXCLUDED_PEOPLE_OIDS"})
        self.assertEqual(code, 2, out.getvalue())
        self.assertFalse((self.state / "mail-capture" / mp.CARRIED_FILE).exists())


class CarriedWalkTests(unittest.TestCase):
    def test_a_dropped_original_carries_nothing_and_a_missing_one_is_not_cleared(self):
        """The store drops an original only when it is no container: walked as carrying
        nothing; bytes gone with no dropped form are no walk (withheld: not cleared)."""
        from hashlib import sha256
        from integrations.captured_text import DROPPED_SUFFIX, blob_path
        with tempfile.TemporaryDirectory() as tmp:
            content, _mail = carried_zip(EXCLUDED_MAIL, tm.UPN_A)
            digest = sha256(content).hexdigest()
            self.assertEqual(mp._walk_one(tmp, digest, "a.zip", None), (digest, None))
            path = blob_path(tmp, digest, DROPPED_SUFFIX)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"{}")
            self.assertEqual(mp._walk_one(tmp, digest, "a.zip", None), (digest, "null"))
            blob_path(tmp, digest).write_bytes(content)   # the bytes themselves: walked
            _d, walked = mp._walk_one(tmp, digest, "a.zip", None)
            self.assertIn(EXCLUDED_MAIL, walked)
            # an Office package is told by its central directory, never walked
            from zipfile import ZipFile
            buf = io.BytesIO()
            with ZipFile(buf, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("fwd.eml", f"From: {EXCLUDED_MAIL}\r\n\r\nx")
            package = buf.getvalue()
            pdigest = sha256(package).hexdigest()
            blob_path(tmp, pdigest).parent.mkdir(parents=True, exist_ok=True)
            blob_path(tmp, pdigest).write_bytes(package)
            self.assertEqual(mp._walk_one(tmp, pdigest, "a.docx", None), (pdigest, "null"))


if __name__ == "__main__":
    unittest.main()
