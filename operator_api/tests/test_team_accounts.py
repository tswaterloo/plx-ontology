"""Shared mailboxes and accounts with no one on record behind them are not people on Team."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from operator_api import team_accounts as ta

BARE = {"manager_state": "none", "job_title": None, "department": None}
STAFF = {"manager_state": "listed", "job_title": None, "department": "Sales"}


def state_with(shared: list[str]) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "mail-capture").mkdir()
    conn = sqlite3.connect(root / "mail-capture" / "capture.db")
    conn.execute("CREATE TABLE mailbox_purpose (mailbox_id TEXT PRIMARY KEY, purpose TEXT, "
                 "decided_at TEXT, personal_at TEXT)")
    for oid in shared:
        conn.execute("INSERT INTO mailbox_purpose VALUES (?, 'shared', 'x', NULL)", (oid,))
    conn.execute("INSERT INTO mailbox_purpose VALUES ('p-1', 'personal', 'x', 'x')")
    conn.commit()
    conn.close()
    return root


class Accounts(unittest.TestCase):
    def test_kind_shared_or_named_only_by_address_with_no_profile_is_an_account(self):
        state = state_with(["s-1"])
        rows = {"s-1": STAFF, "a-1": BARE, "t-1": BARE, "c-1": BARE, "p-1": STAFF}
        with mock.patch("integrations.capture_org.current_rows", return_value=rows):
            ta._CACHE.clear()
            self.assertTrue(ta.is_shared("s-1", {"name_from": "teams"}, state))      # the directory says shared
            self.assertTrue(ta.is_shared("a-1", {"name_from": "address"}, state))    # an address, no profile
            self.assertFalse(ta.is_shared("t-1", {"name_from": "teams"}, state))     # a colleague named them
            self.assertFalse(ta.is_shared("c-1", {"name_from": "calendar"}, state))
            self.assertFalse(ta.is_shared("p-1", {"name_from": "address"}, state))   # has a profile
            self.assertFalse(ta.is_shared("zz", {"name_from": "address"}, state))    # not in the org chart: not called bare

    def test_an_owner_confirmed_login_is_shared_even_with_a_profile_and_junk_entries_are_ignored(self):
        one, two = "b649793b-2fdf-4cc5-a7bf-1deb248b43ba", "a3d74eb5-f230-4335-a852-15fa2d7021b6"
        state = state_with([])
        with mock.patch("integrations.capture_org.current_rows", return_value={one: STAFF}), \
                mock.patch.dict("os.environ", {ta.CONFIRMED_ENV: f" {one.upper()} , not-an-id,{two}"}):
            ta._CACHE.clear()
            self.assertEqual(ta.confirmed(), {one, two})
            self.assertTrue(ta.is_shared(one, {"name_from": "teams"}, state))      # has a department and manager
            self.assertTrue(ta.is_shared(two.upper(), {"name_from": "teams"}, None))
            self.assertFalse(ta.is_shared("c-1", {"name_from": "teams"}, state))   # nobody else changes
        with mock.patch("integrations.capture_org.current_rows", return_value={one: STAFF}), \
                mock.patch.dict("os.environ", {ta.CONFIRMED_ENV: ""}):
            ta._CACHE.clear()
            self.assertFalse(ta.is_shared(one, {"name_from": "teams"}, state))     # unset: data decides

    def test_no_records_means_nobody_is_called_an_account(self):
        with mock.patch("integrations.capture_org.current_rows", side_effect=OSError("gone")):
            ta._CACHE.clear()
            self.assertFalse(ta.is_shared("a-1", {"name_from": "address"}, Path(tempfile.mkdtemp())))
            self.assertFalse(ta.is_shared("a-1", {"name_from": "address"}, None))

    def test_team_person_row_normalizes_comma_spacing(self):
        from datetime import date
        from operator_api import team_view as tv
        row = tv.person_row(
            "11111111-2222-4333-8444-555555555555",
            {"name": "Pat Mercer", "department": "Customer service,Purchasing,Receivables", "days": {}},
            until=date(2026, 11, 25),
            team=None,
            open_={},
            is_self=False,
        )
        self.assertEqual(row["team"], "Customer service, Purchasing, Receivables")

    def test_shared_ids_collects_confirmed_directory_shared_and_bare_address_accounts(self):
        one = "b649793b-2fdf-4cc5-a7bf-1deb248b43ba"
        state = state_with(["s-1"])
        org_rows = {"s-1": STAFF, "a-1": BARE, "t-1": BARE, "p-1": STAFF, one: STAFF}
        people = {
            "a-1": {"name_from": "address"},
            "t-1": {"name_from": "teams"},
            "p-1": {"name_from": "address"},
        }
        with mock.patch("integrations.capture_org.current_rows", return_value=org_rows), \
                mock.patch.dict("os.environ", {ta.CONFIRMED_ENV: one}):
            ta._CACHE.clear()
            got = ta.shared_ids(state, people)
            self.assertEqual(got, frozenset({one, "s-1", "a-1"}))
            # without people snapshot, confirmed + directory shared are still returned
            self.assertEqual(ta.shared_ids(state), frozenset({one, "s-1"}))

    def test_insights_per_person_tiles_exclude_shared_and_warehouse_accounts(self):
        from operator_api.tests import test_insights as ti
        wh_oid = "b649793b-2fdf-4cc5-a7bf-1deb248b43ba"
        state = state_with(["s-1"])
        rows = [
            ["Ana", "12", "5", "p-ana"],
            ["TorontoWH", "40", "10", wh_oid],
            ["SharedBox", "20", "0", "s-1"],
        ]
        b = ti.Backend(ti.sql_out(rows, ti.OUTPUT), everyone=True)
        b.state_dir = state
        with mock.patch("integrations.capture_org.current_rows", return_value={}), \
                mock.patch.dict("os.environ", {ta.CONFIRMED_ENV: wh_oid}):
            ta._CACHE.clear()
            got = b.insight_result(ti.person("employee:owner"), "team_output")
        self.assertEqual([r[0] for r in got.rows], ["Ana"])


if __name__ == "__main__":
    unittest.main()
