"""The delta's mail scan counts from when the generation read the mail capture, not from
when it was built: an attachment recovered (deferred bytes downloaded) after the read and
before the build is in neither the generation nor any layer otherwise (2026-11-05:
28,042 captured attachments never projected). A state counted from the build time scans
once again from the read."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ontology import delta

BUILT = "2026-11-05T14:32:11Z"
READ = "2026-11-05T06:46:40Z"
FROM = "2026-11-04T06:46:40Z"   # READ less the margin


class _NoOne:
    def is_excluded(self, mailbox):
        return mailbox == "excluded-box"


def _capture(path: Path, events):
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE messages (version_id INTEGER PRIMARY KEY, mailbox_id TEXT, "
        "message_id TEXT, l1_handle TEXT);"
        "CREATE TABLE attachments (l1_handle TEXT, message_handle TEXT);"
        "CREATE TABLE attachment_text_versions (recorded_at TEXT, l1_handle TEXT);"
        "CREATE TABLE message_events (l1_handle TEXT NOT NULL, event TEXT NOT NULL, "
        "at TEXT NOT NULL);")
    db.execute("INSERT INTO messages VALUES (10, 'box', 'm0', 'graph-mail://box/m0@1')")
    db.executemany("INSERT INTO message_events VALUES (?, 'attachment_recovered', ?)", events)
    db.commit()
    db.close()


class MailReadSince(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.base = SimpleNamespace(
            built_at=BUILT, mark=lambda source, relation: 10,
            freshness={"graph_mail": {"messages": {"last_refresh_at": READ},
                                      "attachments": {"last_refresh_at": READ}}})

    def changed(self, state):
        return {k for (k,) in state.conn.execute(
            "SELECT key FROM changed WHERE source = 'graph_mail'")}

    def test_an_attachment_recovered_between_read_and_build_is_followed(self):
        _capture(self.dir / "capture.db", [
            ("graph-mail://box/before-margin@1", "2026-11-04T05:00:00Z"),
            ("graph-mail://box/in-margin@1", "2026-11-05T05:00:00Z"),
            ("graph-mail://box/between@1", "2026-11-05T09:00:00Z"),
            ("graph-mail://box/after-build@1", "2026-11-05T15:00:00Z"),
            ("graph-mail://excluded-box/between@1", "2026-11-05T09:30:00Z")])
        state = delta.State(self.dir / "state.sqlite")
        self.addCleanup(state.conn.close)
        delta.mail_changes(self.dir / "capture.db", self.base, state, _NoOne())
        keys = self.changed(state)
        self.assertEqual(sorted(keys), ['["box", "after-build"]', '["box", "between"]',
                                       '["box", "in-margin"]'])
        self.assertEqual(state.get("mail_event_basis"), delta.MAIL_EVENT_BASIS)

    def test_a_state_counted_from_the_build_scans_once_again_from_the_read(self):
        _capture(self.dir / "capture.db", [
            ("graph-mail://box/between@1", "2026-11-05T09:00:00Z"),
            ("graph-mail://box/after-build@1", "2026-11-05T15:00:00Z")])
        state = delta.State(self.dir / "state.sqlite")
        self.addCleanup(state.conn.close)
        state.put("mail_event", 3)            # past both events: the old basis
        state.put("mail_text_since", "2026-11-06T00:00:00Z")
        delta.mail_changes(self.dir / "capture.db", self.base, state, _NoOne())
        self.assertIn('["box", "between"]', self.changed(state))
        state.conn.execute("DELETE FROM changed")
        delta.mail_changes(self.dir / "capture.db", self.base, state, _NoOne())
        self.assertEqual(self.changed(state), set())   # once only: positions kept after

    def test_read_since_never_starts_after_the_build_and_reads_unknown_shapes(self):
        self.assertEqual(delta.read_since(self.base, "graph_mail"), FROM)
        late = SimpleNamespace(built_at=BUILT, freshness={
            "graph_mail": {"messages": {"last_refresh_at": "2026-11-06T00:00:00Z"}}})
        self.assertEqual(delta.read_since(late, "graph_mail"), "2026-11-04T14:32:11Z")
        odd = SimpleNamespace(built_at=BUILT, freshness={"graph_mail": {"messages": 7}})
        self.assertEqual(delta.read_since(odd, "graph_mail"), "2026-11-04T14:32:11Z")
        bad = SimpleNamespace(built_at="not a time", freshness={})
        self.assertEqual(delta.read_since(bad, "graph_mail"), "")
        self.assertEqual(delta.read_since(SimpleNamespace(built_at=None, freshness=None),
                                          "graph_mail"), "")


if __name__ == "__main__":
    unittest.main()
