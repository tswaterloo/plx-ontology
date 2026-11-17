"""The capture writers keep the shared state group's grant (deploy-diag-1114, 11-14).

The API's own user reads the live WAL capture stores through group plx-state
(deploy/release/api_user_setup.sh: store directory g+rwxs, store file g+rw). The mail
and SharePoint writers reset their directory to 0700 on every open, so SQLite in the
API refused the stores (SQLITE_READONLY_DIRECTORY) and the API exited at start. A
writer may make a new store private; it never narrows an existing group grant, and it
never leaves anything to others.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest

from integrations.capture_mail import MailCaptureStore, private_mode
from integrations.capture_sharepoint import SharePointCaptureStore
from integrations import capture_teams
from integrations.live_source_guard import LiveSourceAudience

AUDIENCE = LiveSourceAudience({"teams:*": ["svc:teams-capture"]}, tenant="plx",
                              classifications={"teams:*": "internal"}, identities={})


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class PrivateModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="plx-private-mode-")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir(mode=0o700)

    def test_helper_keeps_a_shared_group_grant_only(self) -> None:
        d = self.state / "d"
        d.mkdir()
        f = d / "f"
        f.write_text("x")
        os.chmod(d, 0o777)   # no setgid: group bits are a umask's, not a grant
        os.chmod(f, 0o666)
        self.assertEqual(private_mode(d, 0o700), 0o700)
        self.assertEqual(private_mode(f, 0o600), 0o600)
        os.chmod(d, 0o2777)  # the shared state group's directory
        self.assertEqual(private_mode(d, 0o700), 0o2770)
        self.assertEqual(private_mode(f, 0o600), 0o660)
        os.chmod(d, 0o3777)  # setgid + sticky, as api_user_setup.sh applies: sticky stays
        self.assertEqual(private_mode(d, 0o700), 0o3770)
        # the grant is given, not only kept (11-15): a store an older writer reset to 0600,
        # or one only group-readable, is group read-write again
        for had in (0o644, 0o640, 0o600):
            os.chmod(f, had)
            self.assertEqual(private_mode(f, 0o600), 0o660)
        os.chmod(d, 0o2700)  # a store directory made under umask 077 in the shared state
        self.assertEqual(private_mode(d, 0o700), 0o2770)
        self.assertEqual(private_mode(d / "missing", 0o600), 0o600)

    def test_new_stores_stay_private(self) -> None:
        MailCaptureStore(self.state).db.close()
        root = self.state / "mail-capture"
        self.assertEqual(_mode(root), 0o700)
        self.assertEqual(_mode(root / "capture.db"), 0o600)

    def test_reopen_keeps_a_group_grant(self) -> None:
        MailCaptureStore(self.state).db.close()
        SharePointCaptureStore(self.state).db.close()
        capture_teams.open_store(self.state, AUDIENCE).close()
        stores = [self.state / "mail-capture", self.state / "sharepoint-capture",
                  self.state / capture_teams.STORE_DIR]
        for root in stores:   # what api_user_setup.sh applies
            os.chmod(root, 0o3770)
            os.chmod(root / "capture.db", 0o660)
        MailCaptureStore(self.state).db.close()
        SharePointCaptureStore(self.state).db.close()
        capture_teams.open_store(self.state, AUDIENCE).close()
        for root in stores:
            with self.subTest(store=root.name):
                self.assertEqual(_mode(root), 0o3770)
                self.assertEqual(_mode(root / "capture.db"), 0o660)


class SharedStoreModesTest(unittest.TestCase):
    """A store in the shared state (setgid + sticky directory, as api_user_setup.sh makes
    it) is opened and written under the host's UMask 0077: the store and its -wal/-shm
    are 0660 whether the store is new, kept, or was reset to 0600 by another writer."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="plx-shared-mode-")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        os.chmod(self.state, 0o3770)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)

    def open_all(self) -> list[Path]:
        MailCaptureStore(self.state).db.close()
        SharePointCaptureStore(self.state).db.close()
        capture_teams.open_store(self.state, AUDIENCE).close()
        return [self.state / "mail-capture" / "capture.db",
                self.state / "sharepoint-capture" / "capture.db",
                self.state / capture_teams.STORE_DIR / capture_teams.DB_FILE]

    def assert_shared(self, db: Path) -> None:
        self.assertEqual(_mode(db), 0o660)
        self.assertTrue(_mode(db.parent) & 0o2070 == 0o2070, oct(_mode(db.parent)))
        self.assertEqual(_mode(db.parent) & 0o007, 0)
        for suffix in ("-wal", "-shm"):
            side = Path(f"{db}{suffix}")
            if side.exists():
                self.assertEqual(_mode(side), 0o660, side.name)

    def test_new_stores_in_the_shared_state_are_group_read_write(self) -> None:
        for db in self.open_all():
            with self.subTest(store=db.parent.name):
                self.assert_shared(db)

    def test_a_store_reset_to_0600_is_set_right_with_its_wal_and_shm_on_the_next_open(self) -> None:
        import sqlite3
        stores = self.open_all()
        readers = []   # a reader (the API) holds each store: its -wal/-shm stay on disk
        try:
            for db in stores:
                reader = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
                reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
                readers.append(reader)
                for side in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
                    self.assertTrue(side.exists(), side.name)
                    os.chmod(side, 0o600)   # what the older Teams writer left (5:59 AM ET)
            self.open_all()
            for db in stores:
                with self.subTest(store=db.parent.name):
                    for side in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
                        self.assertEqual(_mode(side), 0o660, side.name)
        finally:
            for reader in readers:
                reader.close()

    def test_a_side_file_removed_between_its_stat_and_its_chmod_is_skipped(self) -> None:
        """The last connection closing removes the -wal/-shm: one that goes after
        keep_store_modes saw it and before it chmods it leaves the open going on."""
        import sqlite3
        from unittest import mock
        from integrations import capture_store
        db = self.open_all()[0]
        reader = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        try:
            reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
            wal, shm = Path(f"{db}-wal"), Path(f"{db}-shm")
            for side in (db, wal, shm):
                self.assertTrue(side.exists(), side.name)
                os.chmod(side, 0o600)
            real = capture_store.private_mode

            def wal_goes(path, base):   # runs between keep_store_modes' stat and its chmod
                want = real(path, base)
                if str(path) == str(wal):
                    self.assertEqual(want, 0o660)   # so a chmod of the vanished file follows
                    os.unlink(wal)
                return want
            with mock.patch.object(capture_store, "private_mode", side_effect=wal_goes):
                capture_store.keep_store_modes(db)
            self.assertFalse(wal.exists())
            self.assertEqual(_mode(db), 0o660)
            self.assertEqual(_mode(shm), 0o660)   # the side file after it is still set
            os.chmod(db, 0o600)
            with mock.patch.object(capture_store.os, "chmod", side_effect=PermissionError("no")):
                with self.assertRaises(PermissionError):   # only a vanished file is skipped
                    capture_store.keep_store_modes(db)
        finally:
            reader.close()
        MailCaptureStore(self.state).db.close()   # the store still opens and is set right
        self.assert_shared(db)

    def test_outside_the_shared_state_stores_stay_owner_only(self) -> None:
        os.chmod(self.state, 0o700)
        for db in self.open_all():
            with self.subTest(store=db.parent.name):
                self.assertEqual(_mode(db), 0o600)
                for suffix in ("-wal", "-shm"):
                    side = Path(f"{db}{suffix}")
                    if side.exists():
                        self.assertEqual(_mode(side) & 0o077, 0, side.name)


if __name__ == "__main__":
    unittest.main()
