"""The plan request spool (KNOWN-GAPS O4): the folder the app creates must be readable, listable
and removable by the puller of the state's group, whatever the process umask."""
import os
import stat
import tempfile
import unittest

from operating import plan_requests as pr


class Spool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.where = os.path.join(self.tmp.name, "plan-requests")
        self.old = os.environ.get(pr.ENV)
        os.environ[pr.ENV] = self.where
        self.umask = os.umask(0o077)       # the strictest the service could run under
        self.addCleanup(os.umask, self.umask)
        self.addCleanup(lambda: os.environ.pop(pr.ENV, None) if self.old is None
                        else os.environ.__setitem__(pr.ENV, self.old))

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_a_new_folder_and_its_requests_are_group_readable_and_writable_setgid(self):
        for n in (1, 10, 60):
            for i in range(n):
                self.assertTrue(pr.request(f"PurchaseOrder:x:{n}-{i}"))
            self.assertEqual(self.mode(self.where), 0o2770)
            names = [f for f in os.listdir(self.where)]
            self.assertTrue(all(self.mode(os.path.join(self.where, f)) == 0o660 for f in names))
            self.assertEqual(len(pr.waiting()), len(names))   # none lost, none duplicated
            pr.done([f for f, _ in pr.waiting()])
        self.assertEqual(os.listdir(self.where), [])

    def test_a_folder_made_with_the_old_owner_only_mode_is_repaired(self):
        os.makedirs(self.where, mode=0o700)
        os.chmod(self.where, 0o2700)
        self.assertTrue(pr.request("PurchaseOrder:x:1"))
        self.assertEqual(self.mode(self.where), 0o2770)

    def test_a_folder_that_cannot_be_made_right_is_said_and_no_request_is_reported_made(self):
        os.makedirs(self.where, mode=0o700)
        os.chmod(self.where, 0o700)
        from unittest import mock
        with mock.patch("os.chmod", side_effect=PermissionError("not the owner")), \
                self.assertLogs("operating.plan_requests", "WARNING") as seen:
            self.assertFalse(pr.request("PurchaseOrder:x:2"))
        self.assertIn("not written", seen.output[0])


if __name__ == "__main__":
    unittest.main()
