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
    counts for everyone, and on a record's page whether that record's
    document fields are pending, never another record's key."""

    DIGEST = "sha256:" + "a" * 64

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = root / "projection" / "ontology.sqlite"
        (root / "projection" / "delta").mkdir(parents=True)
        layer = root / "projection" / "delta" / "layer-1.sqlite"
        with sqlite3.connect(layer) as conn:
            conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO store_meta VALUES ('documents_pending', ?)", (json.dumps(
                {"pending": 2, "records": {"graph_mail": [["box@x.example", "msg-1"]]}}),))
        conn.close()
        (root / "projection" / "delta" / "CURRENT.json").write_text(json.dumps({
            "format": "plx-delta/v1", "base_digest": self.DIGEST, "store": str(layer),
            "layers": [{"store": str(layer)}], "documents": {
                "pending": 2, "timed_out": 1, "left_to_generation": 0,
                "held_open": {"graph_mail": 1}}}))
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PLX_DELTA_DIR", None)

    def test_counts_and_this_record(self):
        found = documents_pending(self.store, self.DIGEST)
        self.assertEqual((found.files, found.timed_out, found.records, found.this_record),
                         (2, 1, 1, None))
        mine = documents_pending(self.store, self.DIGEST,
                                 ["graph-mail://box@x.example/msg-1@v1"])
        self.assertTrue(mine.this_record)
        other = documents_pending(self.store, self.DIGEST,
                                  ["graph-mail://box@x.example/msg-2@v1"])
        self.assertFalse(other.this_record)
        for shown in (found, mine, other):   # no record key is ever served
            self.assertNotIn("msg-1", shown.model_dump_json())
            self.assertNotIn("box@", shown.model_dump_json())
        # a delta over another generation says nothing
        self.assertIsNone(documents_pending(self.store, "sha256:" + "b" * 64))


if __name__ == "__main__":
    unittest.main()
