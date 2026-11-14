"""operating.file_modes.keep_private: private outside the shared-write set, owner+group in a
setgid (shared-group) directory, and a path another user owns is checked, never changed.

Evidence: isolated temporary directories; no host state."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from operating.file_modes import keep_private


class KeepPrivate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: os.system(f"rm -rf '{self.tmp}'"))

    def mode(self, p: Path) -> int:
        return os.stat(p).st_mode & 0o7777

    def test_a_plain_directory_and_file_become_owner_only(self) -> None:
        d = self.tmp / "store"; d.mkdir(mode=0o755); f = d / "x.sqlite"; f.write_text("")
        os.chmod(f, 0o644)
        keep_private(d, 0o700); keep_private(f, 0o600)
        self.assertEqual((self.mode(d), self.mode(f)), (0o700, 0o600))

    def test_in_a_shared_group_directory_the_group_keeps_read_write(self) -> None:
        d = self.tmp / "feedback"; d.mkdir(); os.chmod(d, 0o2775 | stat.S_ISVTX)
        f = d / "feedback.sqlite"; f.write_text(""); os.chmod(f, 0o644)
        keep_private(d, 0o700); keep_private(f, 0o600)
        self.assertEqual(self.mode(d), 0o2770 | stat.S_ISVTX)     # others out; setgid, sticky stay
        self.assertEqual(self.mode(f), 0o660)

    def test_another_users_path_is_checked_not_changed(self) -> None:
        d = self.tmp / "work-log"; d.mkdir(); os.chmod(d, 0o2770)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 1), \
                mock.patch("os.chmod", side_effect=AssertionError("chmod of another's path")):
            keep_private(d, 0o700)                                 # closed to others: fine
        os.chmod(d, 0o2775)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(PermissionError):
                keep_private(d, 0o700)                             # open to others: refused


if __name__ == "__main__":
    unittest.main()
