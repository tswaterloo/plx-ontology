"""SEC-004 follow-up: an owner-declared Portal access snapshot is re-stamped only after every
row is re-checked (Entra account enabled, Portal user still active), so the owner's served
host does not lock everyone out 30 days after it was made, and a disabled person still ages
out. A real Portal export is never re-stamped."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest

from operating import employee_permissions as ep
from scripts import eval_owner_host as owner
from scripts import refresh_owner_access as refresh

OID = "11111111-2222-4333-8444-555555555555"
CLAIMS = {"uid": "portal-user-1", "email": "reader@example.test",
          "entraTenantId": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "entraObjectId": OID}
OLD = "2026-10-29T20:07:17Z"
NOW = "2026-11-14T04:00:00Z"


class RefreshOwnerAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, "portal_access.json")
        doc = owner.build(CLAIMS, read_at=OLD)["portal_access.json"]
        self.path.write_text(json.dumps(doc))
        self.original = self.path.read_text()
        self.checked: list[str] = []

    def enabled(self, oid: str) -> tuple[bool, str]:
        self.checked.append(oid)
        return (True, "") if oid == OID else (False, "account_disabled")

    def run_refresh(self, portal, **kw):
        return refresh.refresh(self.path, portal, {"basis": "mirror"},
                               kw.pop("enabled", self.enabled), now=NOW, **kw)

    def test_rechecked_rows_are_restamped_and_the_hosts_parser_accepts_them(self) -> None:
        for _ in range(2):    # a second day replaces the note, never stacks it
            out = self.run_refresh([{"id": "portal-user-1", "isActive": True}])
            self.assertTrue(out["refreshed"], out)
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc["read_at"], NOW)
        self.assertEqual(doc["source"].count(refresh.RECHECK_MARK), 1)
        self.assertEqual(self.checked, [OID, OID])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        clock = lambda: datetime(2026, 11, 20, tzinfo=timezone.utc)  # noqa: E731
        snap = ep.PortalAccessSnapshot(self.path, max_age_hours=720, clock=clock)
        self.assertEqual(snap.status()["state"], "fresh")
        stale = ep.PortalAccessSnapshot(
            self.path, max_age_hours=720,
            clock=lambda: datetime(2026, 11, 14, 4, tzinfo=timezone.utc) + timedelta(days=31))
        self.assertNotEqual(stale.status()["state"], "fresh")

    def test_a_row_that_fails_a_recheck_leaves_the_snapshot_to_age_out(self) -> None:
        cases = {
            "entra disabled": ([{"id": "portal-user-1", "isActive": True}],
                               lambda oid: (False, "account_disabled"), "row0:entra_account_disabled"),
            "portal inactive": ([{"id": "portal-user-1", "isActive": False}], None,
                                "row0:portal_inactive"),
            "portal unread": (None, None, "portal_unread"),
        }
        for name, (portal, enabled, code) in cases.items():
            with self.subTest(name):
                kw = {"enabled": enabled} if enabled else {}
                out = self.run_refresh(portal, **kw)
                self.assertFalse(out["refreshed"])
                self.assertIn(code, out["problems"])
                self.assertEqual(self.path.read_text(), self.original)

    def test_a_row_not_in_the_portal_needs_only_its_entra_account(self) -> None:
        out = self.run_refresh([{"id": "someone-else", "isActive": False}])
        self.assertTrue(out["refreshed"], out)

    def test_a_real_portal_export_is_never_restamped(self) -> None:
        doc = json.loads(self.original)
        doc["source"] = "portal-reporting-api people relations (app_user: live)"
        self.path.write_text(json.dumps(doc))
        out = self.run_refresh([{"id": "portal-user-1", "isActive": True}])
        self.assertEqual(out["problems"], ["not_owner_declared"])
        self.assertEqual(json.loads(self.path.read_text())["read_at"], OLD)

    def test_dry_run_writes_nothing(self) -> None:
        out = self.run_refresh([{"id": "portal-user-1", "isActive": True}], dry_run=True)
        self.assertTrue(out["would_refresh"])
        self.assertEqual(self.path.read_text(), self.original)


class PortalReadTest(unittest.TestCase):
    def test_a_portal_outage_falls_back_to_the_mirror(self) -> None:
        class Down:
            def health(self):
                raise OSError("portal down")
        from unittest import mock
        mirror_rows = ({"public.app_user": [{"id": "u1", "is_active": True}]},
                       {"public.app_user": {"basis": "mirror"}})
        with mock.patch.object(refresh.ra, "read_mirror", return_value=mirror_rows):
            users, basis = refresh.read_users({}, Path("mirror.sqlite"), True, Down())
        self.assertEqual([u["id"] for u in users], ["u1"])
        self.assertEqual(basis["basis"], "mirror")
        self.assertEqual(refresh.read_users({}, None, True, Down()), (None, {"basis": "unavailable"}))


if __name__ == "__main__":
    unittest.main()
