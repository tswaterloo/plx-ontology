"""The Labour page for the owner-open reader, and for anyone else without a grant."""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from operating.employee_permissions import FULL_READ_SCOPES
from operator_api import briefs

OWNER = "full_read:" + "+".join(sorted(FULL_READ_SCOPES)) + ":mail_all"


def reader(access: object, principals=("employee:someone",)):
    return SimpleNamespace(session=SimpleNamespace(access=access, principals=principals))


class LabourOwner(unittest.TestCase):
    def page(self, who):
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict("os.environ", {"PLX_LABOUR_GRANTS_FILE": ""}):
            return briefs.labour_page_of(state, who)

    def test_owner_open_reader_is_not_refused(self):
        self.assertNotEqual(self.page(reader(OWNER))["state"], "not_granted")

    def test_others_without_a_grant_still_see_nothing(self):
        for access in (None, "", "full_read:bc:mail_all", OWNER[:-1], OWNER + "x", ["x"]):
            with self.subTest(access=access):
                page = self.page(reader(access))
                self.assertEqual(page["state"], "not_granted")
                self.assertEqual((page["weeks"], page["trend"], page["alerts"]), ([], [], []))

    def test_one_owner_test_decides_every_owner_route(self):
        """S-16: the backend's owner test (view-as, autopilot, ops, the owner workload) is the
        same rule as the Labour and feedback pages', so they can never disagree."""
        from operator_api.feedback import owner_open
        for access in (OWNER, None, "", "full_read:bc:mail_all", OWNER[:-1], OWNER + "x",
                       OWNER.replace("mail_all", "mail_shared_only"), ["x"]):
            with self.subTest(access=access):
                who = reader(access)
                self.assertEqual(briefs.BriefsMixin._is_owner(object(), who),
                                 owner_open(who.session))
                self.assertEqual(owner_open(who.session), access == OWNER)
        self.assertFalse(briefs.BriefsMixin._is_owner(object(), SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
