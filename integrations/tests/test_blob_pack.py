"""The mail capture's small compressed blobs packed into ``blobs/pack.sqlite``
(``integrations.blob_pack``, GROWTH-AUDIT-1111 F16).

Boundary: a captured store (the fake Graph of ``test_capture_mail``) is packed; every
reader of its text (resolve, the mention documents, the typed fields) reads the same
afterwards, for its reader and for a reader it is not for; a purge removes a packed blob's
bytes from the pack file; a purge racing the packer never leaves a row; a tampered or
unreadable pack fails closed; 1, 10 and 60 blobs across commit batches all move once; the
capture backup keeps a shard whose blobs moved into the pack. Evidence label:
injected-transport.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import zlib

from integrations import blob_pack, captured_text as ct, graph_read
from integrations.capture_mail import MailCaptureStore
from integrations.tests import test_capture_mail as tm
from integrations.tests.test_capture_mail_compact import CompactCase, OUTSIDER, READER


def loose_z(blobs: Path) -> list[Path]:
    return sorted(p for p in blobs.glob("*/*.z"))


class PackedStoreTests(CompactCase):
    def captured(self, n: int = 3) -> list[str]:
        for i in range(n):
            self.graph.put(tm.BOX_A, f"MSG-{i}", "CK-1",
                           body=f"Please confirm order {1000 + i}: 120 cases at the agreed price.")
        self.capture().run(tm.SCOPE)
        return [graph_read.mail_citation(tm.BOX_A, f"MSG-{i}", "CK-1") for i in range(n)]

    def outsider_view(self, handles):
        return [self.store.resolve(h, OUTSIDER, self.audience, text=True) for h in handles]

    def test_a_packed_store_reads_the_same_for_every_reader_and_its_files_go(self):
        handles = self.captured()
        before = self.snapshot(handles)
        hidden = self.outsider_view(handles)
        self.assertTrue(all(before[h][0] for h in handles))
        self.assertEqual(len(loose_z(self.store.blobs)), 3)
        dry = blob_pack.pack(self.store.blobs)
        self.assertEqual((dry["packed"], dry["pack_bytes"]), (3, 0))
        self.assertEqual(len(loose_z(self.store.blobs)), 3, "a dry run moves nothing")
        done = blob_pack.pack(self.store.blobs, write=True)
        self.assertEqual((done["packed"], done["unverified"], done["gone"]), (3, 0, 0))
        self.assertEqual(loose_z(self.store.blobs), [])
        self.assertEqual(self.snapshot(handles), before)
        self.assertEqual(self.outsider_view(handles), hidden)
        self.assertEqual(blob_pack.verify(self.store.blobs),
                         {"rows": 3, "bad": 0, "also_loose": 0, "loose_differs": 0})
        # the store's own reads and its size count the pack
        body = self.store.db.execute("SELECT body_sha256 FROM messages LIMIT 1").fetchone()[0]
        self.assertTrue(self.store._present(body))
        self.assertIn(b"Please confirm", self.store.blob(body))
        self.assertIn(b"Please confirm", self.store.blob_head(body))
        self.assertGreaterEqual(self.store._disk_bytes(),
                                blob_pack.pack_path(self.store.blobs).stat().st_size)
        # a capture of the same body again keeps one copy (packed), writes no loose file
        self.capture().run(tm.SCOPE)
        self.assertEqual(loose_z(self.store.blobs), [])
        # undo: every file back, byte for byte, and the readers still read the same
        self.assertEqual(blob_pack.unpack(self.store.blobs)["unpacked"], 3)
        self.assertEqual(len(loose_z(self.store.blobs)), 3)
        self.assertEqual(self.snapshot(handles), before)

    def test_a_packed_body_an_attachment_comes_to_name_is_a_file_again(self):
        self.captured(1)
        body = self.store.db.execute("SELECT body_sha256 FROM messages").fetchone()[0]
        content = self.store.blob(body)
        done = blob_pack.pack(self.store.blobs, write=True, store_db=self.store.path)
        self.assertEqual(done["packed"], 1)
        self.store.put_blob(content, body, kind="text")
        self.assertTrue(ct.blob_path(self.store.blobs, body, ct.COMPRESSED_SUFFIX).exists())
        self.assertEqual(self.store.blob(body), content)

    def test_an_attachment_text_stays_a_file_and_every_reader_reads_the_same(self):
        from integrations.doc_convert import DOCX
        from integrations.tests.office_fixtures import PURCHASE_ORDER_BODY, make_word
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", body="Order attached.", attachments=[
            {**tm.file_attachment("ATT-1", make_word(PURCHASE_ORDER_BODY), name="po.docx"),
             "contentType": DOCX}])
        with mock.patch("integrations.doc_convert.installed_backend", return_value=None):
            self.capture().run(tm.SCOPE)
        message = graph_read.mail_citation(tm.BOX_A, "MSG-1", "CK-1")
        before = self.snapshot([message])
        (text,) = [r[0] for r in self.store.db.execute("SELECT text_sha256 FROM attachments")]
        done = blob_pack.pack(self.store.blobs, write=True, store_db=self.store.path)
        self.assertEqual((done["packed"], done["attachment"]), (1, 1))
        self.assertTrue(ct.blob_path(self.store.blobs, text, ct.COMPRESSED_SUFFIX).exists())
        self.assertEqual(self.snapshot([message]), before)

    def test_a_purge_takes_a_packed_blob_out_of_the_pack_file(self):
        handles = self.captured(2)
        digest = self.store.db.execute(
            "SELECT body_sha256 FROM messages WHERE message_id='MSG-0'").fetchone()[0]
        packed = ct.blob_path(self.store.blobs, digest, ct.COMPRESSED_SUFFIX).read_bytes()
        blob_pack.pack(self.store.blobs, write=True)
        pack = blob_pack.pack_path(self.store.blobs)
        self.assertIn(packed, pack.read_bytes())
        self.store.purge_messages([(tm.BOX_A, "MSG-0")])
        self.assertFalse(blob_pack.exists(self.store.blobs, digest, ct.COMPRESSED_SUFFIX))
        self.assertIsNone(ct.read_blob(self.store.blobs, digest))
        self.assertNotIn(packed, pack.read_bytes(), "secure_delete overwrites the row")
        self.assertIsNone(self.store.resolve(handles[0], READER, self.audience, text=True))
        self.assertTrue(self.store.resolve(handles[1], READER, self.audience, text=True))

    def test_a_purge_while_a_file_is_packed_leaves_no_row(self):
        self.captured(2)
        digest = self.store.db.execute(
            "SELECT body_sha256 FROM messages WHERE message_id='MSG-0'").fetchone()[0]
        real = blob_pack._verified

        def purge_meanwhile(name, data):   # the file goes after the packer read it
            if name.startswith(digest):
                self.store.purge_messages([(tm.BOX_A, "MSG-0")])
            return real(name, data)

        with mock.patch.object(blob_pack, "_verified", side_effect=purge_meanwhile):
            done = blob_pack.pack(self.store.blobs, write=True)
        self.assertEqual((done["packed"], done["gone"]), (1, 1))
        self.assertFalse(blob_pack.exists(self.store.blobs, digest, ct.COMPRESSED_SUFFIX))
        self.assertEqual(blob_pack.verify(self.store.blobs)["rows"], 1)

    def test_a_purge_the_pack_cannot_take_now_is_never_served_and_goes_at_the_next_write(self):
        handles = self.captured(2)
        digest = self.store.db.execute(
            "SELECT body_sha256 FROM messages WHERE message_id='MSG-0'").fetchone()[0]
        packed = ct.blob_path(self.store.blobs, digest, ct.COMPRESSED_SUFFIX).read_bytes()
        blob_pack.pack(self.store.blobs, write=True)
        pack = blob_pack.pack_path(self.store.blobs)
        # the pack cannot be written (a lock held past the timeout, say)
        with mock.patch.object(blob_pack, "settle",
                               side_effect=sqlite3.OperationalError("database is locked")):
            self.store.purge_messages([(tm.BOX_A, "MSG-0")])
        self.assertIn(packed, pack.read_bytes(), "still in the file: not yet deleted")
        self.assertIsNone(ct.read_blob(self.store.blobs, digest))
        self.assertFalse(blob_pack.exists(self.store.blobs, digest, ct.COMPRESSED_SUFFIX))
        with self.assertRaises(FileNotFoundError):
            blob_pack.size(self.store.blobs, digest, ct.COMPRESSED_SUFFIX)
        self.assertIsNone(self.store.resolve(handles[0], READER, self.audience, text=True))
        # the next writer (here the packer) deletes it first
        blob_pack.pack(self.store.blobs, write=True)
        self.assertNotIn(packed, pack.read_bytes())
        self.assertEqual(blob_pack._journal_path(pack).stat().st_size, 0)
        self.assertTrue(self.store.resolve(handles[1], READER, self.audience, text=True))

    def test_the_undo_never_writes_back_a_blob_purged_meanwhile(self):
        self.captured(2)
        digest = self.store.db.execute(
            "SELECT body_sha256 FROM messages WHERE message_id='MSG-0'").fetchone()[0]
        blob_pack.pack(self.store.blobs, write=True)
        real = os.replace

        def purge_after_write(src, dst):
            real(src, dst)
            if Path(dst).name.startswith(digest):
                self.store.purge_messages([(tm.BOX_A, "MSG-0")])

        with mock.patch.object(blob_pack.os, "replace", side_effect=purge_after_write):
            done = blob_pack.unpack(self.store.blobs)
        self.assertEqual(done["unpacked"], 1)
        self.assertFalse(ct.blob_path(self.store.blobs, digest, ct.COMPRESSED_SUFFIX).exists())
        self.assertIsNone(ct.read_blob(self.store.blobs, digest))
        self.assertEqual(len(loose_z(self.store.blobs)), 1)

    def test_packing_is_refused_while_a_reader_runs_code_without_the_pack(self):
        self.captured(1)
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            old, new = proc / "old-tree", proc / "new-tree"
            (new / "integrations").mkdir(parents=True)
            (new / "integrations" / "blob_pack.py").write_text("")
            old.mkdir()
            for pid, tree in (("101", old), ("102", new)):
                (proc / pid / "fd").mkdir(parents=True)
                os.symlink(self.store.path, proc / pid / "fd" / "3")
                os.symlink(tree, proc / pid / "cwd")
            store = self.store.blobs.parent.parent
            self.assertEqual(blob_pack.stale_readers(store, proc),
                             {"stale": 1, "current": 1, "unreadable": 0, "possible": 0})
        mail = self.state / "mail-capture" / "blobs" / "sha256"
        with mock.patch.object(blob_pack, "stale_readers",
                               return_value={"stale": 1, "current": 0, "unreadable": 0}):
            self.assertEqual(blob_pack.main(["pack", "--write", "--blobs", str(mail)]), 3)
        self.assertEqual(len(loose_z(self.store.blobs)), 1, "nothing moved")
        with mock.patch.object(blob_pack, "stale_readers",
                               return_value={"stale": 0, "current": 1, "unreadable": 3,
                                             "possible": 1}):
            # another user's process that can read the store (the API's own user, through
            # the shared group) may hold it with old code nobody can see: refused
            self.assertEqual(blob_pack.main(["pack", "--write", "--blobs", str(mail)]), 3)
        self.assertEqual(len(loose_z(self.store.blobs)), 1, "nothing moved")
        with mock.patch.object(blob_pack, "stale_readers",
                               return_value={"stale": 0, "current": 1, "unreadable": 3,
                                             "possible": 0}):
            self.assertEqual(blob_pack.main(["pack", "--write", "--blobs", str(mail)]), 0)
        self.assertEqual(loose_z(self.store.blobs), [])

    def test_another_users_process_that_can_read_the_store_counts_as_a_possible_reader(self):
        self.captured(1)
        store = self.store.blobs.parent.parent
        db = os.stat(store / "capture.db")
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            for pid, uid in (("201", db.st_uid or 4242), ("202", 0)):
                (proc / pid / "fd").mkdir(parents=True)
                (proc / pid / "status").write_text(f"Name:\tpython3\nUid:\t{uid}\t{uid}\n")
            real = Path.iterdir

            def iterdir(path):
                if path.name == "fd":
                    raise PermissionError(path)
                return real(path)
            with mock.patch.object(Path, "iterdir", iterdir), \
                    mock.patch.object(blob_pack, "_could_open",
                                      side_effect=lambda uid, st: uid not in (0,)):
                got = blob_pack.stale_readers(store, proc)
        self.assertEqual((got["unreadable"], got["possible"], got["stale"]), (2, 1, 0))
        user = os.stat(store / "capture.db")
        self.assertTrue(blob_pack._could_open(user.st_uid or 4242, os.stat_result(
            (0o100640, 0, 0, 1, user.st_uid or 4242, 0, 0, 0, 0, 0))))   # its owner
        self.assertFalse(blob_pack._could_open(0, user))                    # root: system
        self.assertTrue(blob_pack._could_open(None, user))                  # unknown: closed


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.blobs = Path(self.tmp.name) / "blobs" / "sha256"
        self.blobs.mkdir(parents=True)

    def put(self, content: bytes) -> str:
        digest = sha256(content).hexdigest()
        target = ct.blob_path(self.blobs, digest, ct.COMPRESSED_SUFFIX)
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(zlib.compress(content, 6))
        return digest

    def test_one_ten_and_sixty_blobs_across_batches_move_once_each(self):
        for n in (1, 10, 60):
            with self.subTest(n=n), mock.patch.object(blob_pack, "BATCH", 7):
                digests = [self.put(f"body {n} {i}".encode()) for i in range(n)]
                done = blob_pack.pack(self.blobs, write=True)
                self.assertEqual(done["packed"], n)
                self.assertEqual(loose_z(self.blobs), [])
                self.assertEqual([ct.read_blob(self.blobs, d) for d in digests],
                                 [f"body {n} {i}".encode() for i in range(n)])
                again = blob_pack.pack(self.blobs, write=True)
                self.assertEqual(again["packed"], 0)
        self.assertEqual(blob_pack.verify(self.blobs)["rows"], 71)
        with mock.patch.object(blob_pack, "BATCH", 7):   # undo across batches
            self.assertEqual(blob_pack.unpack(self.blobs),
                             {"unpacked": 71, "kept_loose": 0, "differs": 0})
        self.assertEqual(len(loose_z(self.blobs)), 71)
        self.assertEqual(blob_pack.verify(self.blobs)["rows"], 0)

    def test_only_bodies_move_and_an_attachment_naming_a_packed_body_gets_its_file_back(self):
        db_path = Path(self.tmp.name) / "capture.db"
        db = sqlite3.connect(db_path)
        db.executescript("CREATE TABLE attachments (content_sha256 TEXT, text_sha256 TEXT);"
                         "CREATE TABLE attachment_text_versions (text_sha256 TEXT);")
        body, text, content, older = (self.put(f"{k} bytes".encode())
                                      for k in ("body", "text", "content", "older"))
        db.executemany("INSERT INTO attachments VALUES (?, ?)", [(content, text)])
        db.execute("INSERT INTO attachment_text_versions VALUES (?)", (older,))
        db.commit()
        done = blob_pack.pack(self.blobs, write=True, store_db=db_path)
        self.assertEqual((done["packed"], done["attachment"], done["restored"]), (1, 3, 0))
        for digest in (text, content, older):
            self.assertTrue(ct.blob_path(self.blobs, digest, ".z").exists())
        self.assertFalse(ct.blob_path(self.blobs, body, ".z").exists())
        # an attachment's text later equals that body: the next run writes the file back
        db.execute("INSERT INTO attachments VALUES (NULL, ?)", (body,))
        db.commit()
        db.close()
        again = blob_pack.pack(self.blobs, write=True, store_db=db_path)
        self.assertEqual(again["restored"], 1)
        self.assertTrue(ct.blob_path(self.blobs, body, ".z").exists())
        self.assertEqual(ct.read_blob(self.blobs, body), b"body bytes")

    def test_a_file_not_its_name_or_too_big_stays_loose(self):
        good = self.put(b"kept body")
        bad = sha256(b"other").hexdigest()
        wrong = ct.blob_path(self.blobs, bad, ct.COMPRESSED_SUFFIX)
        wrong.parent.mkdir(exist_ok=True)
        wrong.write_bytes(zlib.compress(b"not other"))
        big = self.put(os.urandom(blob_pack.MAX_PACKED_BYTES * 2))
        done = blob_pack.pack(self.blobs, write=True)
        self.assertEqual((done["packed"], done["unverified"], done["too_big"]), (1, 1, 1))
        self.assertTrue(wrong.exists())
        self.assertTrue(ct.blob_path(self.blobs, big, ct.COMPRESSED_SUFFIX).exists())
        self.assertEqual(ct.read_blob(self.blobs, good), b"kept body")

    def test_a_tampered_or_unreadable_pack_reads_as_nothing(self):
        digest = self.put(b"original body")
        blob_pack.pack(self.blobs, write=True)
        pack = blob_pack.pack_path(self.blobs)
        db = sqlite3.connect(pack)
        db.execute("UPDATE packed SET data = ?", (zlib.compress(b"altered body"),))
        db.commit()
        db.close()
        self.assertIsNone(ct.read_blob(self.blobs, digest))
        self.assertEqual(blob_pack.verify(self.blobs)["bad"], 1)
        blob_pack._drop_reader(self.blobs)
        pack.write_bytes(b"not a database at all" * 100)
        self.assertIsNone(ct.read_blob(self.blobs, digest))
        self.assertFalse(blob_pack.exists(self.blobs, digest, ct.COMPRESSED_SUFFIX))
        with self.assertRaises(OSError):
            blob_pack.read(self.blobs, digest, ct.COMPRESSED_SUFFIX)

    def test_only_the_compressed_form_is_ever_looked_up_in_the_pack(self):
        digest = self.put(b"a body")
        blob_pack.pack(self.blobs, write=True)
        for suffix in ("", ct.DROPPED_SUFFIX):
            self.assertFalse(blob_pack.exists(self.blobs, digest, suffix))
            with self.assertRaises(FileNotFoundError):
                blob_pack.read(self.blobs, digest, suffix)
        self.assertFalse(blob_pack.exists(self.blobs, "../" + digest[3:], ".z"))

    def test_the_cli_refuses_a_directory_that_is_not_a_blob_store(self):
        self.assertEqual(blob_pack.main(["pack", "--blobs", self.tmp.name]), 2)
        self.assertEqual(blob_pack.main(["pack", "--blobs", str(self.blobs)]), 2)
        sharepoint = Path(self.tmp.name) / "sharepoint-capture" / "blobs" / "sha256"
        sharepoint.mkdir(parents=True)
        self.assertEqual(blob_pack.main(["pack", "--blobs", str(sharepoint)]), 2)
        mail = Path(self.tmp.name) / "mail-capture" / "blobs" / "sha256"
        mail.mkdir(parents=True)
        # no capture.db: bodies cannot be told from attachments' blobs
        self.assertEqual(blob_pack.main(["pack", "--blobs", str(mail)]), 2)
        db = sqlite3.connect(mail.parent.parent / "capture.db")
        db.executescript("CREATE TABLE attachments (content_sha256 TEXT, text_sha256 TEXT);"
                         "CREATE TABLE attachment_text_versions (text_sha256 TEXT);")
        db.close()
        self.assertEqual(blob_pack.main(["pack", "--blobs", str(mail)]), 0)


class BackupTests(unittest.TestCase):
    def test_a_shard_whose_blobs_moved_into_the_pack_is_still_whole(self):
        import importlib.util
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "capture_backup_f16", root / "deploy" / "backup" / "capture_backup.py")
        cb = importlib.util.module_from_spec(spec)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            blobs = state / "mail-capture" / "blobs" / "sha256"
            blobs.mkdir(parents=True)
            digests = []
            for i in range(3):
                content = f"body {i}".encode()
                d = sha256(content).hexdigest()
                path = ct.blob_path(blobs, d, ".z")
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(zlib.compress(content))
                digests.append(d)
            db = sqlite3.connect(state / "mail-capture" / "capture.db")
            db.execute("CREATE TABLE blobs (sha256 TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO blobs VALUES (?)", [(d,) for d in digests])
            db.commit()
            db.close()
            with mock.patch.dict(os.environ, {"PLX_STATE_DIR": str(state)}):
                spec.loader.exec_module(cb)
            cb.STATE = state
            run = cb.Run.__new__(cb.Run)
            names = [f"{d[:2]}/{d}.z" for d in digests]
            run.old_names = lambda key: set(names)
            run.held_back = lambda store: False
            run.pack_copies = {}
            # a blob gone from its shard and from the pack is lost
            self.assertEqual(run.lost("blobs/mail-xx.tar.zst", [], "mail"), 3)
            blob_pack.pack(blobs, write=True, limit=2)
            loose = [n for n in names if (blobs / n).exists()]
            self.assertEqual(len(loose), 1)
            # moved into the pack but no copy of the pack this run: still lost (the
            # shard's old copy stays)
            self.assertEqual(run.lost("blobs/mail-xx.tar.zst", loose, "mail"), 2)
            snapshot = Path(tmp) / "snap.sqlite"
            src = sqlite3.connect(blob_pack.pack_path(blobs))
            dst = sqlite3.connect(snapshot)
            src.backup(dst)
            src.close()
            dst.close()
            run.pack_copies = {"mail": snapshot}
            self.assertEqual(run.lost("blobs/mail-xx.tar.zst", loose, "mail"), 0)
            # packed after this run's pack copy: in neither copy, so lost
            blob_pack.pack(blobs, write=True)
            self.assertEqual(run.lost("blobs/mail-xx.tar.zst", [], "mail"), 1)
            self.assertEqual(cb.BLOB_PACKS["mail"][1], "mail-capture/blobs/pack.sqlite")


if __name__ == "__main__":
    unittest.main()
