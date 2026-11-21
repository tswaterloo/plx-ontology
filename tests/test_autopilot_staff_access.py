"""Autopilot lookups for opted-in staff (scripts/autopilot_staff_access.py) and the daily re-check
that drops a leaver instead of locking everyone out (scripts/refresh_owner_access.py), at the
files the Autopilot host and its Lookup read: the snapshot (the hosts' own parser) and the
readers file (``autopilot_context.readers``)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest

from operating import autopilot_write as aw
from operating import employee_permissions as ep
from operating.card_actions import Journal
from operator_api import autopilot_context as ac
from scripts import autopilot_staff_access as sa
from scripts import eval_owner_host as owner
from scripts import refresh_owner_access as refresh

TENANT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OWNER = "11111111-2222-4333-8444-555555555555"
STAFF = "22222222-3333-4444-8555-666666666666"
OTHER = "33333333-4444-4555-8666-777777777777"
NOW = datetime(2026, 11, 14, 4, tzinfo=timezone.utc)


def portal_row(uid: str, email: str, active: bool = True) -> dict:
    return {"id": uid, "role": "USER", "accessLevel": "L2", "isActive": active,
            "mrpWarehouse": False, "mrpProduction": False, "mrpRnD": False,
            "departmentIds": ["d1"], "accessScopes": [{"scopeType": "DATA",
                                                        "scopeKey": "operations.sales",
                                                        "effect": "ALLOW"}],
            "policyDenies": [], "_served_email": email}


class Guard:
    def __init__(self, excluded=()):
        self.excluded = {e.lower() for e in excluded}

    def decides(self, value):
        return True

    def is_excluded(self, value):
        return str(value).lower() in self.excluded


class StaffAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.snap = self.dir / "portal_access.json"
        claims = {"uid": "owner-1", "email": "owner@example.test", "entraTenantId": TENANT,
                  "entraObjectId": OWNER}
        doc = owner.build(claims, read_at="2026-11-13T00:00:00Z")["portal_access.json"]
        self.snap.write_text(json.dumps(doc))
        self.readers = self.dir / "readers.json"
        self.readers.write_text(json.dumps({OWNER: claims}))
        self.journal = self.dir / "audit.jsonl"
        self.portal = [portal_row("p-staff", "staff@example.test"),
                       portal_row("p-other", "other@example.test")]
        self.directory = {"staff@example.test": STAFF, "other@example.test": OTHER}
        self.disabled: set[str] = set()
        self.guard = Guard()
        self.graph_calls = 0
        self.org = lambda: {STAFF: "staff@example.test"}

    def switch(self, person: str, on: bool) -> None:
        aw.set_drafts(Journal(self.journal), person, "ar_collections", on, person, NOW)

    def run_it(self, **kw):
        def lookup():
            def look(email):
                self.graph_calls += 1
                return self.directory.get(email)
            return look
        return sa.run(self.snap, self.readers, self.journal,
                      portal_reader=kw.pop("portal_reader", lambda: (
                          self.portal, {sa.ra.GRANTS: {"basis": "mirror"},
                                        sa.ra.SCOPES: {"basis": "mirror"}})),
                      lookup=lookup,
                      enabled=lambda: (lambda oid: (oid not in self.disabled,
                                                    "account_disabled")),
                      guard=lambda: self.guard, **kw)

    def rows(self) -> dict:
        snap = ep.PortalAccessSnapshot(self.snap, max_age_hours=1e9)
        snap.status()
        doc = json.loads(self.snap.read_text())
        return {u["entraObjectId"]: u for u in doc["users"]}

    def test_an_opted_in_person_gets_their_own_portal_row_and_a_session(self) -> None:
        self.switch(STAFF, True)
        out = self.run_it()
        self.assertTrue(out["changed"], out)
        self.assertEqual(out["added"], 1)
        row = self.rows()[STAFF]
        # their own Portal row as the Portal holds it, never a grant made up here
        self.assertEqual((row["id"], row["role"], row["accessLevel"], row["email"]),
                         ("p-staff", "USER", "L2", "staff@example.test"))
        self.assertNotIn("_served_email", row)
        self.assertIn(OWNER, self.rows())
        self.assertEqual(set(ac.readers(self.readers)), {OWNER, STAFF})
        self.assertEqual(ac.readers(self.readers)[STAFF]["uid"], "p-staff")
        for p in (self.snap, self.readers, self.dir / sa.ADDED_FILE):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        # a second run with nothing new reads nothing from Graph and changes nothing
        calls, before = self.graph_calls, self.snap.read_text()
        self.assertFalse(self.run_it()["changed"])
        self.assertEqual((self.graph_calls, self.snap.read_text()), (calls, before))

    def test_nobody_is_added_unless_every_check_passes(self) -> None:
        both = lambda: (self.portal, {sa.ra.GRANTS: {"basis": "live"},  # noqa: E731
                                      sa.ra.SCOPES: {"basis": "live"}})
        cases = {
            "excluded person": dict(guard=Guard(["staff@example.test"])),
            "entra disabled": dict(disabled={STAFF}),
            "portal user inactive": dict(portal=[portal_row("p-staff", "staff@example.test",
                                                            active=False)]),
            "no portal user": dict(portal=[portal_row("p-other", "other@example.test")]),
            "address resolves to another person": dict(directory={"staff@example.test": OTHER}),
            "two portal rows for the person": dict(portal=[
                portal_row("p-a", "staff@example.test"), portal_row("p-b", "staff@example.test")]),
            "portal unread": dict(reader=lambda: (None, {})),
            "portal denies unread": dict(reader=lambda: (self.portal, {
                sa.ra.GRANTS: {"basis": "unavailable"},
                sa.ra.SCOPES: {"basis": "live"}})),
        }
        original = (self.snap.read_text(), self.readers.read_text())
        self.switch(STAFF, True)
        for name, case in cases.items():
            with self.subTest(name):
                self.guard = case.get("guard", Guard())
                self.disabled = set(case.get("disabled", ()))
                self.portal = case.get("portal", [portal_row("p-staff", "staff@example.test")])
                self.directory = case.get("directory", {"staff@example.test": STAFF})
                out = self.run_it(portal_reader=case.get("reader", both))
                self.assertEqual(out["added"], 0, out)
                self.assertEqual((self.snap.read_text(), self.readers.read_text()), original)
                self.assertEqual(set(ac.readers(self.readers)), {OWNER})

    def test_an_unreadable_journal_or_switch_off_never_grows_access(self) -> None:
        self.journal.write_text("{not json\n")
        original = self.snap.read_text()
        out = self.run_it()
        self.assertEqual(out["problems"], ["journal_unreadable"])
        self.assertEqual(self.snap.read_text(), original)
        self.journal.unlink()
        self.switch(STAFF, True)
        self.switch(STAFF, False)
        self.assertEqual(self.run_it()["added"], 0)
        self.assertEqual(set(self.rows()), {OWNER})

    def test_switching_off_removes_only_the_rows_this_added(self) -> None:
        self.switch(STAFF, True)
        self.assertEqual(self.run_it()["added"], 1)
        self.switch(STAFF, False)
        out = self.run_it()
        self.assertEqual(out["removed"], 1, out)
        # the owner's declared row is not opted in through the journal and stays
        self.assertEqual(set(self.rows()), {OWNER})
        self.assertEqual(set(ac.readers(self.readers)), {OWNER})

    def test_ten_and_sixty_people_switching_on_are_each_added_once(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                ids = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(n)]
                self.portal = [portal_row(f"p{i}", f"s{i}@example.test") for i in range(n)]
                self.directory = {f"s{i}@example.test": ids[i] for i in range(n)}
                for oid in ids:
                    self.switch(oid, True)
                out = self.run_it()
                self.assertEqual(out["added"], n, out)
                rows = json.loads(self.snap.read_text())["users"]
                self.assertEqual(len(rows), n + 1)
                self.assertEqual(len({r["entraObjectId"] for r in rows}), n + 1)
                self.assertEqual(set(ac.readers(self.readers)), {OWNER, *ids})

    def test_all_staff_adds_every_active_staff_member_but_the_excluded(self) -> None:
        self.portal.append(portal_row("p-gone", "gone@example.test", active=False))
        self.portal.append(portal_row("p-excl", "excl@example.test"))
        self.directory.update({"gone@example.test": "44444444-0000-4000-8000-000000000000",
                               "excl@example.test": "55555555-0000-4000-8000-000000000000"})
        self.guard = Guard(["excl@example.test"])
        out = self.run_it(all_staff=True, directory=self.org)  # nobody opted in
        self.assertEqual((out["added"], out["staff"]), (2, 2), out)
        self.assertEqual(set(ac.readers(self.readers)), {OWNER, STAFF, OTHER})
        self.assertEqual(set(self.rows()), {OWNER, STAFF, OTHER})
        # without --all-staff (the opt-in run) the same files would take them out again;
        # with it, a second run changes nothing
        self.assertFalse(self.run_it(all_staff=True, directory=self.org)["changed"])
        # a person who leaves (Portal inactive) is taken out; the owner's row stays
        self.portal = [portal_row("p-staff", "staff@example.test")]
        out = self.run_it(all_staff=True, directory=self.org)
        self.assertEqual(out["removed"], 1, out)
        self.assertEqual(set(ac.readers(self.readers)), {OWNER, STAFF})
        # later excluded: taken out at the next run
        self.guard = Guard([STAFF])
        self.assertEqual(self.run_it(all_staff=True, directory=self.org)["removed"], 1)
        self.assertEqual(set(ac.readers(self.readers)), {OWNER})

    def test_all_staff_gives_staff_without_a_portal_user_the_plain_row(self) -> None:
        plain = "66666666-0000-4000-8000-000000000000"
        excl = "77777777-0000-4000-8000-000000000000"
        inactive = "88888888-0000-4000-8000-000000000000"
        self.portal.append(portal_row("p-in", "inactive@example.test", active=False))
        self.guard = Guard([excl])
        org = {plain: {"upn": "Plain@Example.test", "account_enabled": True},
               excl: {"upn": "excl@example.test", "account_enabled": True},
               inactive: {"upn": "inactive@example.test", "account_enabled": True},
               "99999999-0000-4000-8000-000000000000": {"upn": "off@example.test",
                                                        "account_enabled": False}}
        out = self.run_it(all_staff=True, directory=lambda: sa.directory_staff(org))
        self.assertEqual(out["added"], 3, out)       # staff, other (Portal) + plain
        row = self.rows()[plain]
        self.assertEqual((row["role"], row["accessLevel"], row["accessScopes"],
                          row["departmentIds"], row["email"]),
                         ("STAFF", "L2", [], [], "plain@example.test"))
        # the hosts' own resolver takes it as an employee (a known role, a level)
        from operating import employee_permissions_portal as portal
        self.assertIn(row["role"], portal.EMPLOYEE_ROLES)
        self.assertEqual(row["accessLevel"], portal.LEGACY_ROLE_BOOTSTRAP[row["role"]])
        self.assertEqual(ac.readers(self.readers)[plain]["uid"], plain)
        # excluded: no entry; Portal user inactive: no plain row either; disabled: none
        self.assertEqual(set(ac.readers(self.readers)), {OWNER, STAFF, OTHER, plain})
        # the org chart unread: nothing changes (never shrinks to the Portal staff)
        before = self.readers.read_text()
        out = self.run_it(all_staff=True, directory=dict)
        self.assertEqual(out["problems"], ["staff_unread"])
        self.assertEqual(self.readers.read_text(), before)
        # gone from the org chart: taken out
        org.pop(plain)
        out = self.run_it(all_staff=True, directory=lambda: sa.directory_staff(org))
        self.assertEqual(out["removed"], 1, out)
        self.assertNotIn(plain, ac.readers(self.readers))

    def test_all_staff_never_removes_on_an_outage(self) -> None:
        self.assertEqual(self.run_it(all_staff=True, directory=self.org)["added"], 2)
        before = (self.snap.read_text(), self.readers.read_text())

        def broken():
            def look(email):
                raise OSError("graph down")
            return look
        for name, kw in {"portal unread": dict(portal_reader=lambda: (None, {})),
                         "graph raises": dict(lookup=broken)}.items():
            with self.subTest(name):
                out = sa.run(self.snap, self.readers, self.journal,
                             portal_reader=kw.get("portal_reader", lambda: (
                                 self.portal, {sa.ra.GRANTS: {"basis": "mirror"},
                                               sa.ra.SCOPES: {"basis": "mirror"}})),
                             lookup=kw.get("lookup", lambda: self.directory.get),
                             enabled=lambda: (lambda oid: (True, "")),
                             guard=lambda: self.guard, all_staff=True,
                             directory=self.org)
                self.assertTrue(out.get("problems"), out)
                self.assertEqual(out.get("removed", 0), 0, out)
                self.assertEqual((self.snap.read_text(), self.readers.read_text()), before)


class LeaverDropTest(unittest.TestCase):
    """The daily re-check drops a person who left and re-stamps the rest; an undecidable
    check removes nobody."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, "portal_access.json")
        claims = {"uid": "owner-1", "email": "owner@example.test", "entraTenantId": TENANT,
                  "entraObjectId": OWNER}
        doc = owner.build(claims, read_at="2026-10-29T20:07:17Z")["portal_access.json"]
        staff = {k: v for k, v in portal_row("p-staff", "staff@example.test").items()
                 if not k.startswith("_")}
        staff.update(email="staff@example.test", entraTenantId=TENANT, entraObjectId=STAFF)
        doc["users"].append(staff)
        self.path.write_text(json.dumps(doc))
        self.original = self.path.read_text()

    def test_a_leaver_is_dropped_and_the_others_restamped(self) -> None:
        out = refresh.refresh(self.path, [{"id": "p-staff", "isActive": True}], {"basis": "mirror"},
                              lambda oid: (oid == OWNER, "account_disabled"),
                              now="2026-11-14T04:00:00Z")
        self.assertTrue(out["refreshed"], out)
        self.assertEqual(out["dropped"], 1)
        doc = json.loads(self.path.read_text())
        self.assertEqual([u["entraObjectId"] for u in doc["users"]], [OWNER])
        self.assertEqual(doc["read_at"], "2026-11-14T04:00:00Z")
        ep.PortalAccessSnapshot(self.path, max_age_hours=720,
                                clock=lambda: NOW).status()

    def test_an_undecidable_check_removes_and_stamps_nothing(self) -> None:
        for name, enabled in {
                "graph unreadable": lambda oid: (oid == OWNER, "unreadable:timeout"),
                "graph raises": lambda oid: (_ for _ in ()).throw(OSError("down")),
                "mismatch": lambda oid: (oid == OWNER, "user_mismatch")}.items():
            with self.subTest(name):
                out = refresh.refresh(self.path, [], {"basis": "mirror"}, enabled,
                                      now="2026-11-14T04:00:00Z")
                self.assertFalse(out["refreshed"], out)
                self.assertEqual(self.path.read_text(), self.original)


if __name__ == "__main__":
    unittest.main()
