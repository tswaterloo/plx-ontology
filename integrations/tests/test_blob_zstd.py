"""The mail capture's attachment originals held as ``<sha256>.zst``
(``integrations.blob_zstd``, DISK-CLEAN-1114).

Boundary: a captured store (the fake Graph of ``test_capture_mail``) has its originals
compressed; every reader (resolve, every page, the mention documents, the typed fields, the
store's own blob read and head, the carried-mail head, the stored-form sizes the projection
keys on, the typed-field steps' held tests) reads the same afterwards and after the undo; a
purge removes the ``.zst``; an original deleted while it is compressed never comes back; a
damaged frame reads as nothing; compressing is refused while a tree without the reader
runs; the capture backup reads a ``.zst`` member as its blob. Evidence label:
injected-transport.
"""
from __future__ import annotations

from hashlib import sha256
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from deploy.backup import capture_backup
from integrations import blob_pack, blob_zstd, carried_mail, captured_text as ct
from integrations.tests import test_capture_mail as tm
from integrations.tests.test_capture_mail_compact import CompactCase, attachment, pdf
from ontology import document_fields

SHEET = b"sku,qty,price\n" + b"".join(b"FG-%04d,%d,%d.50\n" % (n, n, n % 97) for n in range(4000))


def raws(blobs: Path) -> list[Path]:
    return sorted(p for p in blobs.glob("*/*") if len(p.name) == 64)


def zsts(blobs: Path) -> list[Path]:
    return sorted(blobs.glob("*/*.zst"))


class CodecTests(unittest.TestCase):
    def test_a_frame_reads_back_its_bytes_size_and_head(self):
        frame = blob_zstd.compress(SHEET, 10)
        self.assertLess(len(frame), len(SHEET) // 3)
        self.assertEqual(blob_zstd.decompress(frame), SHEET)
        self.assertEqual(blob_zstd.content_size(frame[:18]), len(SHEET))
        self.assertEqual(blob_zstd.head(io.BytesIO(frame), 4096), SHEET[:4096])
        self.assertEqual(blob_zstd.head(io.BytesIO(frame), 10 ** 7), SHEET)
        self.assertEqual(blob_zstd.decompress(blob_zstd.compress(b"")), b"")

    def test_anything_but_one_whole_frame_within_the_limit_is_refused(self):
        frame = blob_zstd.compress(SHEET)
        for bad in (frame + b"x", frame[:-3], b"not a frame", frame + frame):
            with self.assertRaises(ValueError):
                blob_zstd.decompress(bad)
        with self.assertRaises(ValueError):
            blob_zstd.decompress(frame, limit=len(SHEET) - 1)


class CompressedStoreTests(CompactCase):
    def captured(self):
        po = pdf([["Purchase order 4500012345", "120 cases"], ["Page two of the order"]])
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", body="Order and stock attached.",
                       attachments=[attachment("ATT-1", po, "po.pdf", "application/pdf"),
                                    attachment("ATT-2", SHEET, "stock.csv", "text/csv")])
        with mock.patch("integrations.doc_convert.installed_backend", return_value=None):
            self.assertTrue(self.capture().run(tm.SCOPE)["complete"])
        handles = [self.handle("MSG-1", "CK-1", a, c) for a, c in (("ATT-1", po),
                                                                  ("ATT-2", SHEET))]
        return handles, {sha256(c).hexdigest(): c for c in (po, SHEET)}

    def reads(self, handles, originals):
        blobs = self.store.blobs
        with mock.patch("integrations.doc_convert.installed_backend", return_value=None):
            snap = self.snapshot(handles)
        return snap, {d: (ct.read_blob(blobs, d), self.store.blob(d),
                          self.store.blob_head(d), carried_mail._blob_head(str(blobs), d),
                          ct.blob_form_size(blobs, d), blob_pack.exists(blobs, d),
                          document_fields._bytes_held(blobs, d),
                          document_fields._bytes_kept(blobs, d, None))
                      for d in originals}

    def test_every_reader_reads_the_same_after_compressing_and_after_the_undo(self):
        handles, originals = self.captured()
        blobs = self.store.blobs
        before = self.reads(handles, originals)
        self.assertTrue(all(v[0] == originals[d] for d, v in before[1].items()))
        dry = blob_zstd.compress_store(blobs, min_bytes=0)
        self.assertGreaterEqual(dry["files"], 1)
        self.assertEqual(zsts(blobs), [], "a dry run writes nothing")
        done = blob_zstd.compress_store(blobs, write=True, min_bytes=0, min_saving=0.0)
        self.assertGreaterEqual(done["files"], 1)
        self.assertGreater(done["bytes_saved"], 0)
        packed = {p.name[:-4] for p in zsts(blobs)}
        self.assertIn(sha256(SHEET).hexdigest(), packed)
        self.assertTrue(packed.isdisjoint(p.name for p in raws(blobs)),
                        "a compressed original's bytes file is gone")
        self.assertEqual(self.reads(handles, originals), before)
        self.assertEqual(blob_zstd.verify_store(blobs),
                         {"zst": len(packed), "bad": 0, "also_raw": 0})
        self.assertEqual(blob_zstd.expand_store(blobs), {"expanded": len(packed), "bad": 0})
        self.assertEqual(zsts(blobs), [])
        for digest in packed:
            self.assertEqual(sha256(ct.blob_path(blobs, digest).read_bytes()).hexdigest(),
                             digest)
        self.assertEqual(self.reads(handles, originals), before)

    def test_a_purge_removes_the_compressed_original(self):
        handles, originals = self.captured()
        blobs = self.store.blobs
        blob_zstd.compress_store(blobs, write=True, min_bytes=0, min_saving=0.0)
        self.assertTrue(zsts(blobs))
        frames = [p.read_bytes() for p in zsts(blobs)]
        self.store.purge_messages([(tm.BOX_A, "MSG-1")])
        self.assertEqual(zsts(blobs), [])
        for digest in originals:
            self.assertIsNone(ct.read_blob(blobs, digest))
            self.assertFalse(blob_pack.exists(blobs, digest))
        left = b"".join(p.read_bytes() for p in blobs.glob("*/*") if p.is_file())
        self.assertFalse(any(f in left for f in frames))

    def test_an_original_deleted_while_it_is_compressed_never_comes_back(self):
        self.captured()
        blobs = self.store.blobs
        raw = ct.blob_path(blobs, sha256(SHEET).hexdigest())
        real = blob_zstd._write

        def write_then_purge(target, data):
            real(target, data)
            raw.unlink()          # a purge or a drop between the write and the unlink
        with mock.patch.object(blob_zstd, "_write", write_then_purge):
            outcome = blob_zstd.compress_one(raw, level=3, min_bytes=0, min_saving=0.0,
                                             write=True)
        self.assertEqual(outcome, ("gone", 0))
        self.assertFalse(raw.with_name(raw.name + ".zst").exists())
        self.assertIsNone(ct.read_blob(blobs, raw.name))

    def test_a_damaged_frame_reads_as_nothing_and_fails_verify(self):
        self.captured()
        blobs = self.store.blobs
        digest = sha256(SHEET).hexdigest()
        blob_zstd.compress_store(blobs, write=True, min_bytes=0)
        zst = ct.blob_path(blobs, digest, ".zst")
        zst.write_bytes(blob_zstd.compress(SHEET.replace(b"FG-0001", b"FG-9999")))
        self.assertIsNone(ct.read_blob(blobs, digest))
        with self.assertRaises(Exception):
            self.store.blob(digest)
        self.assertEqual(blob_zstd.verify_store(blobs)["bad"], 1)
        zst.write_bytes(b"\x28\xb5\x2f\xfd garbage")
        self.assertIsNone(ct.read_blob(blobs, digest))
        zst.write_bytes(b"not a zstd frame")   # size reads the header only: no magic, no size
        self.assertIsNone(ct.read_blob(blobs, digest))
        with self.assertRaises(OSError):
            blob_pack.size(blobs, digest)
        self.assertEqual(blob_zstd.expand_store(blobs)["bad"], 1)
        self.assertTrue(zst.exists(), "the undo leaves a bad frame as it is")

    def test_compressing_is_refused_while_a_tree_without_the_reader_runs(self):
        self.captured()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            old, new, other = proc / "old-tree", proc / "new-tree", proc / "not-a-tree"
            for tree in (old, new):
                (tree / "integrations").mkdir(parents=True)
                (tree / "integrations" / "captured_text.py").write_text("")
            (new / "integrations" / "blob_zstd.py").write_text("")
            other.mkdir()
            for pid, tree in (("101", old), ("102", new), ("103", other)):
                (proc / pid).mkdir()
                os.symlink(tree, proc / pid / "cwd")
            self.assertEqual(blob_zstd.stale_trees(proc),
                             {"stale": 1, "current": 1, "possible": 0})
        mail = self.state / "mail-capture" / "blobs" / "sha256"
        before = raws(self.store.blobs)
        with mock.patch.object(blob_zstd, "stale_trees",
                               return_value={"stale": 1, "current": 0, "possible": 0}):
            self.assertEqual(blob_zstd.main(["compress", "--write", "--min-bytes", "0",
                                             "--blobs", str(mail)]), 3)
        with mock.patch.object(blob_zstd, "stale_trees",
                               return_value={"stale": 0, "current": 1, "possible": 1}):
            self.assertEqual(blob_zstd.main(["compress", "--write", "--min-bytes", "0",
                                             "--blobs", str(mail)]), 3)
        self.assertEqual(raws(self.store.blobs), before, "nothing moved")
        with mock.patch.object(blob_zstd, "stale_trees",
                               return_value={"stale": 0, "current": 1, "possible": 0}):
            self.assertEqual(blob_zstd.main(["compress", "--write", "--min-bytes", "0",
                                             "--blobs", str(mail)]), 0)
        self.assertTrue(zsts(self.store.blobs))
        self.assertEqual(blob_zstd.main(["verify", "--blobs", str(self.store.blobs.parent)]), 2)


class ReaderGuardTests(unittest.TestCase):
    def test_only_this_users_non_python_helpers_are_left_out_of_the_unseen(self):
        me = os.getuid()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            for pid, name, uid in (("1", "gpg-agent", me), ("2", "sudo", me),
                                   ("3", "python3", me), ("4", "gpg-agent", me + 1)):
                (proc / pid).mkdir()
                (proc / pid / "status").write_text(f"Name:\t{name}\nUid:\t{uid}\t{uid}\n")
            self.assertEqual([blob_pack.unseen_reader(proc / p) for p in "1234"],
                             [0, 0, 1, 1])
            self.assertEqual(blob_pack.unseen_reader(proc / "missing"), 1)


class BackupTests(unittest.TestCase):
    def test_a_zst_member_is_its_blob(self):
        digest = "ab" * 32
        for name in (digest, digest + ".zst", digest + ".z", digest + ".dropped"):
            self.assertEqual(capture_backup._digest(f"ab/{name}"), digest)


if __name__ == "__main__":
    unittest.main()
