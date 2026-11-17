"""The API's app data keeps the shared state group's read/write (small-fixes-1115).

Files the API creates with os.open(..., 0o600) ignore the umask's group bits, so in the
setgid app-data directory they came out 0600 (known-readers.json is rewritten about once a
minute), and the host's api_user_setup.sh --check failed and was re-applied every minute.
Each writer now gives a file made in a setgid directory group read/write; outside one it
stays owner-only, and others never get anything.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import unittest
from types import SimpleNamespace

from integrations.page_counts import PageCounts
from operator_api import backend as be
from operator_api import team_view
from operator_api.page_store import PageStore


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


class AppDataSharedModesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="app-data-modes-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shared = self.root / "shared"
        self.shared.mkdir()
        os.chmod(self.shared, 0o2770)   # api_user_setup.sh's directory: setgid, group rwx
        self.plain = self.root / "plain"
        self.plain.mkdir(mode=0o700)

    def _remember(self, data_dir: Path) -> be.WorkspaceBackend:
        backend = be.WorkspaceBackend(lambda s: None, data_label="test", data_dir=data_dir)
        oid = "29e05178-0bd1-49b9-be6e-b01be1975f70"
        session = SimpleNamespace(principals=frozenset({f"employee:{oid}"}),
                                  employee=SimpleNamespace(object_id=oid), email=None,
                                  session_ref="a" * 64, expires_at=int(time.time()) + 3600)
        backend.remember_reader(be.Reader(session, "dev", "X"))
        return backend

    def test_known_readers_and_its_lock_are_group_read_write(self) -> None:
        for _ in range(2):   # the rewrite every minute, not only the first write
            self._remember(self.shared)
            self.assertEqual(mode(self.shared / be.KNOWN_READERS_FILE), 0o660)
            self.assertEqual(mode(self.shared / (be.KNOWN_READERS_FILE + ".lock")), 0o660)
            os.utime(self.shared / be.KNOWN_READERS_FILE, (0, 0))

    def test_known_readers_stay_owner_only_outside_the_shared_group_directory(self) -> None:
        self._remember(self.plain)
        self.assertEqual(mode(self.plain / be.KNOWN_READERS_FILE), 0o600)

    def test_learned_owner_ids_group_read_write(self) -> None:
        path = self.shared / team_view.OWNER_IDS_FILE
        team_view._learn_owner(path, ["29e05178-0bd1-49b9-be6e-b01be1975f70"])
        self.assertEqual(mode(path), 0o660)
        plain = self.plain / team_view.OWNER_IDS_FILE
        team_view._learn_owner(plain, ["29e05178-0bd1-49b9-be6e-b01be1975f70"])
        self.assertEqual(mode(plain), 0o600)

    def test_page_stores_and_their_wal_group_read_write(self) -> None:
        (self.shared / "page-cache").mkdir(mode=0o2770)
        os.chmod(self.shared / "page-cache", 0o2770)
        store = PageStore(self.shared / "page-cache")
        store.put("k", {"v": 1}, 60)
        self.assertEqual(mode(store.path), 0o660)
        counts = PageCounts(self.shared / "page-counts.sqlite")
        counts._conn()
        for suffix in ("", "-wal", "-shm"):
            p = Path(f"{counts.path}{suffix}")
            if p.exists():
                self.assertEqual(mode(p) & 0o077, 0o060, p.name)

    def test_other_api_writers_keep_the_group_grant(self) -> None:
        """view-as audit, the feedback digest and the learning ledger (a directory that
        keeps its setgid bit and group, its file and WAL group read/write)."""
        from operating.learning import Ledger
        from operator_api import feedback, view_as
        (self.shared / "view-as").mkdir(mode=0o2770)
        os.chmod(self.shared / "view-as", 0o2770)
        view_as.Audit(self.shared).record(viewer="a", key="b", route="/x", status=200)
        self.assertEqual(mode(self.shared / "view-as" / "audit.jsonl"), 0o660)
        out = feedback._write_json(self.shared / "digest.json", {"a": 1})
        self.assertEqual(mode(out), 0o660)
        ledger_dir = self.shared / "learning"
        ledger_dir.mkdir(mode=0o2770)
        os.chmod(ledger_dir, 0o2770)
        ledger = Ledger(ledger_dir)
        self.addCleanup(ledger.close)
        self.assertEqual(mode(ledger_dir), 0o2770)
        self.assertEqual(mode(ledger.path), 0o660)
        # outside the shared group nothing widens: others never get anything
        plain = self.plain / "digest.json"
        feedback._write_json(plain, {"a": 1})
        self.assertEqual(mode(plain), 0o600)
        solo = Ledger(self.plain / "learning")
        self.addCleanup(solo.close)
        self.assertEqual((mode(solo.directory), mode(solo.path)), (0o700, 0o600))


if __name__ == "__main__":
    unittest.main()
