"""The status line's truth-check flag: counts read from the check's status file, and none
stated once the check is older than two hours."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from operator_api.backend import TRUTH_STATUS, documents_pending, truth_flag

NOW = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)


def _write(state: Path, ran: datetime) -> None:
    path = state / TRUTH_STATUS
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"generated_at": ran.isoformat(), "late_claims": 22,
                                "late_contradicted": 22, "claims": 100, "contradicted": 50,
                                "detectors_incomplete": ["a", "b"]}))


class TruthFlag(unittest.TestCase):
    def test_fresh_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp), NOW - timedelta(minutes=20))
            flag = truth_flag(Path(tmp), NOW)
        self.assertFalse(flag.stale)
        self.assertEqual((flag.late_claims, flag.late_contradicted, flag.detectors_incomplete),
                         (22, 22, 2))

    def test_old_check_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp), NOW - timedelta(hours=3))
            self.assertTrue(truth_flag(Path(tmp), NOW).stale)

    def test_no_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(truth_flag(Path(tmp), NOW))


class DocumentsPendingFlag(unittest.TestCase):
    """The status line's pending documents:
    counts without the excluded people's mail, and on a record's page
    whether that record's document fields are pending, never another record's key."""

    DIGEST = "sha256:" + "a" * 64
    KEPT = "11111111-1111-4111-8111-111111111111"
    EXCLUDED = "22222222-2222-4222-8222-222222222222"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.store = self.state / "projection" / "ontology.sqlite"
        (self.state / "projection" / "delta").mkdir(parents=True)
        (self.state / "mail-capture").mkdir()
        conn = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        with conn:
            conn.execute("CREATE TABLE mailboxes (mailbox_id TEXT, upn TEXT, identities TEXT)")
            conn.executemany("INSERT INTO mailboxes VALUES (?, ?, '[]')",
                             [(self.KEPT, "box@x.example"), (self.EXCLUDED, "gone@x.example")])
        conn.close()
        env = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": self.EXCLUDED})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PLX_DELTA_DIR", None)
        os.environ.pop("PLX_EXCLUDED_PEOPLE_FILE", None)

    def layer(self, *mailboxes):
        self.layers = getattr(self, "layers", 0) + 1
        layer = self.state / "projection" / "delta" / f"layer-{self.layers}.sqlite"
        conn = sqlite3.connect(layer)
        with conn:
            conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO store_meta VALUES ('documents_pending', ?)", (json.dumps(
                {"pending": 2, "records": {"graph_mail": [[box, f"msg-{i}"] for i, box in
                                                          enumerate(mailboxes, 1)]}}),))
        conn.close()
        (self.state / "projection" / "delta" / "CURRENT.json").write_text(json.dumps({
            "format": "plx-delta/v1", "base_digest": self.DIGEST, "store": str(layer),
            "layers": [{"store": str(layer)}], "documents": {
                "pending": 2, "timed_out": 1, "left_to_generation": 0,
                "held_open": {"graph_mail": len(mailboxes)}}}))

    def pending(self, handles=()):
        return documents_pending(self.store, self.DIGEST, handles, state_dir=self.state)

    def test_counts_and_this_record(self):
        self.layer("box@x.example")
        found = self.pending()
        self.assertEqual((found.files, found.timed_out, found.records, found.this_record),
                         (2, 1, 1, None))
        mine = self.pending(["graph-mail://box@x.example/msg-1@v1"])
        self.assertTrue(mine.this_record)
        other = self.pending(["graph-mail://box@x.example/msg-2@v1"])
        self.assertFalse(other.this_record)
        for shown in (found, mine, other):   # no record key is ever served
            self.assertNotIn("msg-1", shown.model_dump_json())
            self.assertNotIn("box@", shown.model_dump_json())
        # a delta over another generation says nothing
        self.assertIsNone(documents_pending(self.store, "sha256:" + "b" * 64,
                                            state_dir=self.state))

    def test_postgres_alone_never_opens_the_generation_file(self):
        """``PLX_ONTOLOGY_PG=only`` (the SQLite generation retired): the served digest is
        Postgres's own and the generation file is never opened, so it can be gone; a file
        left behind holding another digest is not read. Postgres not whole: no digest,
        nothing said."""
        from operator_api import backend as be
        from operating.pg import ontology_reader
        self.layer("box@x.example", "gone@x.example")
        api = be.WorkspaceBackend(lambda s: None, data_label="t", state_dir=self.state)
        opened = []
        real = sqlite3.connect

        def connect(target, *args, **kwargs):
            opened.append(str(target))
            return real(target, *args, **kwargs)

        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only"}), \
                mock.patch.object(ontology_reader, "store_meta",
                                  return_value={"content_digest": self.DIGEST}), \
                mock.patch.object(sqlite3, "connect", connect):
            path, digest = api._served_store()
            self.assertFalse(path.exists())
            self.assertEqual(digest, self.DIGEST)
            found = documents_pending(path, digest, state_dir=self.state)
            self.store.write_bytes(b"")   # a leftover file: never read
            self.assertEqual(api._served_store()[1], self.DIGEST)
        self.assertFalse([p for p in opened if "ontology.sqlite" in p], opened)
        # the excluded person's mail stays withheld on the Postgres path too
        self.assertEqual((found.records, found.files), (1, None))
        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only"}), \
                mock.patch.object(ontology_reader, "store_meta", return_value=None):
            path, digest = api._served_store()
            self.assertIsNone(digest)
            self.assertIsNone(documents_pending(path, digest, state_dir=self.state))

    def test_the_excluded_peoples_mail_is_not_counted(self):
        self.layer("box@x.example", "gone@x.example")
        found = self.pending(["graph-mail://gone@x.example/msg-2@v1"])
        # one record counted; the file counts, which cannot leave theirs out, are omitted
        self.assertEqual((found.files, found.timed_out, found.records, found.this_record),
                         (None, None, 1, False))
        self.assertNotIn("gone@", found.model_dump_json())
        # only theirs pending: nothing is said
        self.layer("gone@x.example")
        self.assertIsNone(self.pending())
        # no exclusion list loads: undecided withholds
        self.layer("box@x.example")
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": ""}):
            self.assertIsNone(self.pending())

if __name__ == "__main__":
    unittest.main()
