"""Staff read scope: the policy a
staff reader gets from ``ontology_host.case_host_policy`` when the precomputed audience units
are given, the capture host's shared-mailbox rule and the item rule of ``CaseRuntime``."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from ontology.store import Actor
from operating import staff_scope
from operating.case_runtime import CaseRuntime, EMPLOYEE_ACL_DENIED
from operating.ontology_host import case_host_policy
from operating.staff_scope import Unit

OWN = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
EXCLUDED = "33333333-3333-4333-8333-333333333333"
SHARED = "44444444-4444-4444-8444-444444444444"
PAYROLL = "55555555-5555-4555-8555-555555555555"
ACTOR = Actor("plx", frozenset({f"employee:{OWN}"}), "restricted")


class _Audience:
    """A live/mirror audience that allows everything and counts each per-unit check."""

    def __init__(self) -> None:
        self.calls = 0

    def allows(self, *_args) -> bool:
        self.calls += 1
        return True


def _runtime(guard=None, *, unrestricted=False):
    entitlement = SimpleNamespace(
        actor=ACTOR, principal=SimpleNamespace(object_id=OWN, principal=next(iter(ACTOR.principals))),
        unrestricted=lambda: unrestricted, full_read_covers=lambda _s: False,
        full_read=None)
    gate = mock.Mock(side_effect=lambda actor: actor)
    gate.entitlement = lambda _actor: entitlement
    live, mirror = _Audience(), _Audience()
    return SimpleNamespace(
        employee_gate=gate,
        people_exclusion=PeopleExclusion([EXCLUDED]) if guard is None else guard,
        live=SimpleNamespace(audience=live),
        mirror=SimpleNamespace(_audience=lambda: mirror)), live, mirror


KEYS = ["api:public.app_sales_order", "bc:prod/items", "mc:ops/tasks",
        "sharepoint-list:site/list", "sharepoint:site-shared", "sharepoint:od-own",
        "sharepoint:od-other", f"mail:{OWN}", f"mail:{OTHER}", f"mail:{EXCLUDED}",
        "teams:chat:mine", "teams:chat:theirs", "teams:chat:with-excluded",
        "teams:channel:t1/public", "teams:channel:t1/private", "sharepoint:no-row",
        "mc:ops/with-excluded"]
UNITS = {
    "sharepoint:site-shared": Unit("shared"),
    "sharepoint:od-own": Unit("personal", frozenset({OWN})),
    "sharepoint:od-other": Unit("personal", frozenset({OTHER})),
    f"mail:{OWN}": Unit("personal", frozenset({OWN})),
    f"mail:{OTHER}": Unit("personal", frozenset({OTHER})),
    f"mail:{EXCLUDED}": Unit("personal", frozenset({EXCLUDED})),
    "teams:chat:mine": Unit("personal", frozenset({OWN, OTHER})),
    "teams:chat:theirs": Unit("personal", frozenset({OTHER})),
    "teams:chat:with-excluded": Unit("personal", frozenset({OWN, EXCLUDED})),
    "teams:channel:t1/public": Unit("shared"),
    "teams:channel:t1/private": Unit("personal", frozenset({OTHER})),
    "mc:ops/with-excluded": Unit("shared", frozenset({EXCLUDED})),
}


def _policy(runtime, keys=KEYS, units=UNITS, marking_grants=None):
    return case_host_policy(runtime, ACTOR, None, audience_keys=keys,
                            bc_units={"bc:prod/items": "prod"},
                            marking_grants=marking_grants, staff_units=units)


class StaffPolicyTests(unittest.TestCase):

    def test_staff_see_all_business_records(self):
        runtime, _live, _mirror = _runtime()
        policy = _policy(runtime)
        self.assertTrue(policy.staff)
        for key in ("api:public.app_sales_order", "api:public.app_invoice", "bc:prod/items",
                    "bc:other/vendors", "mc:ops/tasks", "sharepoint-list:site/list",
                    "sharepoint:site-shared", "teams:channel:t1/public"):
            self.assertTrue(policy.may_read(ACTOR, key), key)
        self.assertTrue(policy.covers_family(ACTOR, "api"))
        self.assertTrue(policy.covers_family(ACTOR, "bc"))

    def test_not_another_persons_mailbox_chat_or_onedrive(self):
        runtime, _live, _mirror = _runtime()
        policy = _policy(runtime)
        for key in (f"mail:{OWN}", "teams:chat:mine", "sharepoint:od-own"):
            self.assertTrue(policy.may_read(ACTOR, key), key)
        for key in (f"mail:{OTHER}", "teams:chat:theirs", "teams:channel:t1/private",
                    "sharepoint:od-other", "sharepoint:no-row", "teams:chat:unlisted",
                    "mail:not-an-oid", "meetings:calendar:" + OTHER):
            self.assertFalse(policy.may_read(ACTOR, key), key)

    def test_finance_markings_hold(self):
        runtime, _live, _mirror = _runtime()
        policy = _policy(runtime, marking_grants={"finance": frozenset({"group:finance"})})
        self.assertNotIn("finance", policy.held_markings(ACTOR))
        finance = Actor("plx", ACTOR.principals | {"group:finance"}, "restricted")
        self.assertIn("finance", policy.held_markings(finance))

    def test_finance_file_class_holds_before_the_staff_item_rule(self):
        entitlement = SimpleNamespace(actor=ACTOR, file_class_allows=lambda _r: False)
        fake = SimpleNamespace(employee_gate=SimpleNamespace(entitlement=lambda _a: entitlement),
                               _staff_item_allows=lambda *a: True)
        record = {"original_url": "https://t.sharepoint.com/sites/plx/Finance/x.xlsx",
                  "created_by": OWN, "source_acl": {"status": "read", "identities": []}}
        self.assertEqual(CaseRuntime._employee_acl_denial(fake, ACTOR, "sharepoint", record),
                         EMPLOYEE_ACL_DENIED)

    def test_excluded_people_never_appear(self):
        runtime, _live, _mirror = _runtime()
        policy = _policy(runtime)
        for key in (f"mail:{EXCLUDED}", "teams:chat:with-excluded", "mc:ops/with-excluded",
                    "meetings:calendar:" + EXCLUDED):
            self.assertFalse(policy.may_read(ACTOR, key), key)

    def test_exclusion_unavailable_grants_business_families_only(self):
        runtime, _live, _mirror = _runtime(PeopleExclusion(unavailable="excluded_people_oids_absent"))
        policy = _policy(runtime)
        self.assertTrue(policy.may_read(ACTOR, "api:public.app_sales_order"))
        for key in (f"mail:{OWN}", "teams:chat:mine", "teams:channel:t1/public",
                    "sharepoint:site-shared", "sharepoint:od-own", "mc:ops/with-excluded"):
            self.assertFalse(policy.may_read(ACTOR, key), key)

    def test_full_reader_and_no_units_are_not_staff(self):
        runtime, _live, _mirror = _runtime(unrestricted=True)
        self.assertFalse(_policy(runtime).staff)
        runtime, _live, _mirror = _runtime()
        self.assertFalse(_policy(runtime, units=None).staff)

    def test_per_unit_checks_drop_to_none(self):
        """The old model asks the live audience once per Teams/MC unit (N=1/10/60); the
        staff scope asks nothing, keeps every unit's decision and grows no slower."""
        for n in (1, 10, 60):
            keys = [f"teams:chat:c{i}" for i in range(n)] + [f"mc:ops/c{i}" for i in range(n)]
            units = {f"teams:chat:c{i}": Unit("personal", frozenset({OWN} if i % 2 else {OTHER}))
                     for i in range(n)}
            runtime, live, mirror = _runtime()
            with mock.patch("integrations.capture_teams.CHAT_KIND") as kind:
                kind.canonical_path.side_effect = lambda parts: parts
                kind.unit.side_effect = lambda path: "teams:chat:" + path[0]
                kind.resources.side_effect = lambda path: ["r"]
                case_host_policy(runtime, ACTOR, None, audience_keys=keys)
            self.assertGreaterEqual(live.calls, n, n)
            runtime, live, mirror = _runtime()
            policy = _policy(runtime, keys=keys, units=units)
            self.assertEqual(live.calls + mirror.calls, 0, n)
            granted = [k for k in keys if k.startswith("teams:") and policy.may_read(ACTOR, k)]
            self.assertEqual(len(granted), n // 2, n)
            self.assertTrue(all(policy.may_read(ACTOR, f"mc:ops/c{i}") for i in range(n)))


class SharedMailboxTests(unittest.TestCase):

    def _capture(self, kinds=True):
        """A mail capture store as the capture leaves it: mailbox rows, and (``kinds``) the
        kind its directory listing recorded for each."""
        from integrations.capture_mail import MailCaptureStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MailCaptureStore(tmp.name)
        self.addCleanup(store.close)
        rows = [(SHARED, "sales@plx.example", [], "shared"),
                (PAYROLL, "payroll@plx.example", ["payroll@plx.example"], "shared"),
                (OTHER, "person@plx.example", [], "personal"),
                (OWN, "me@plx.example", [], None)]
        for oid, upn, identities, _kind in rows:
            store.record_mailbox({"object_id": oid, "upn": upn, "identities": identities,
                                  "resources": [f"mail:{oid}"],
                                  "source_acl": {"status": "derived"}})
        if kinds:
            store.record_mailbox_kinds({oid: kind for oid, _u, _i, kind in rows if kind})
        return store.db

    def test_shared_mailboxes_open_to_staff_except_restricted_and_personal(self):
        with mock.patch.dict(os.environ, {staff_scope.RESTRICTED_SHARED_ENV:
                                          "payroll@plx.example, junk"}):
            shared = staff_scope.shared_mailboxes(self._capture())
        self.assertEqual(shared, frozenset({SHARED}))
        # No kind recorded (a capture from before, or a listing that never ran): none.
        self.assertEqual(staff_scope.shared_mailboxes(self._capture(False)), frozenset())
        self.assertEqual(staff_scope.shared_mailboxes(sqlite3.connect(":memory:")),
                         frozenset())

    def test_staff_unit_policy_opens_shared_mailboxes(self):
        runtime, _live, _mirror = _runtime()
        runtime.staff_shared_mailboxes = lambda: frozenset({SHARED, EXCLUDED})
        keys = KEYS + [f"mail:{SHARED}"]
        policy = _policy(runtime, keys=keys,
                         units={**UNITS, f"mail:{SHARED}": Unit("personal", frozenset({SHARED}))})
        self.assertTrue(policy.may_read(ACTOR, f"mail:{SHARED}"))
        self.assertFalse(policy.may_read(ACTOR, f"mail:{OTHER}"))
        self.assertFalse(policy.may_read(ACTOR, f"mail:{EXCLUDED}"))

    def test_staff_item_rule_on_mail(self):
        fake = SimpleNamespace(people_exclusion=PeopleExclusion([EXCLUDED]),
                               staff_shared_mailboxes=lambda: frozenset({SHARED, EXCLUDED}))
        entitlement = SimpleNamespace(actor=ACTOR)

        def allows(mailbox, **acl):
            acl = {"status": "derived", "kind": "mailbox_owner", "identities": [mailbox], **acl}
            return CaseRuntime._staff_item_allows(fake, entitlement, "graph-mail", acl, {})

        self.assertTrue(allows(SHARED))
        self.assertFalse(allows(OTHER))         # another person's personal mailbox
        self.assertFalse(allows(EXCLUDED))      # never an excluded person's
        self.assertFalse(allows(SHARED, status="read"))
        fake.people_exclusion = PeopleExclusion(unavailable="excluded_people_oids_absent")
        self.assertFalse(allows(SHARED))        # exclusion not usable: fail closed

    def test_staff_item_rule_on_files(self):
        """Real drive-item shape (no creator; the last modifier's address): a business
        site's file opens to staff unless its site is restricted; a restricted site, an
        unset list, a OneDrive or an unknown site keep the per-file check."""
        fake = SimpleNamespace(people_exclusion=PeopleExclusion([EXCLUDED]),
                               staff_shared_mailboxes=frozenset,
                               staff_site_baselines=lambda: {
                                   "b!driveoperations01": frozenset({"group:payroll"}),
                                   "b!driverestricted1": frozenset({"group:payroll"})})
        entitlement = SimpleNamespace(actor=ACTOR)
        acl = {"status": "read", "identities": ["group:payroll"]}

        def allows(url, modifier="pat@plx.example", drive="b!driveOperations01", **extra):
            return CaseRuntime._staff_item_allows(
                fake, entitlement, "sharepoint", acl,
                {"original_url": url, "drive_id": drive, "last_modified_by": modifier,
                 **extra})

        host = "https://t.sharepoint.com"
        ops = f"{host}/sites/Operations/Shared%20Documents/quote.xlsx"
        listed = {staff_scope.RESTRICTED_SITES_ENV:
                  "ConfidentialPayrollInformation, Executive-Finance-SV,AltonTax,Finance,"
                  "VincePersonal,InsuranceClaim,b!driveRestricted1, not a site!"}
        with mock.patch.dict(os.environ, listed):
            self.assertTrue(allows(ops))
            self.assertTrue(allows(f"{host}/teams/Sales/Shared Documents/a.pdf"))
            for site in ("ConfidentialPayrollInformation", "confidentialpayrollinformation",
                         "Executive-Finance-SV", "AltonTax", "Finance", "VincePersonal",
                         "InsuranceClaim"):
                self.assertFalse(allows(f"{host}/sites/{site}/Shared%20Documents/x.pdf"), site)
                self.assertFalse(allows(f"{host}/sites/{site}/_layouts/15/Doc.aspx?x=1"), site)
            self.assertFalse(allows(ops, drive="b!driveRestricted1"))   # by drive id
            self.assertFalse(allows(ops, drive=None))                   # no drive: closed
            self.assertTrue(allows(f"{host}/sites/Finance-Team/a.pdf"))  # not the Finance site
            self.assertFalse(allows(ops, modifier=EXCLUDED))
            self.assertFalse(allows(ops, modifier=None))             # nobody recorded
            self.assertFalse(allows(ops, created_by=EXCLUDED))
            self.assertFalse(allows(f"{host.replace('t.', 't-my.')}/personal/x_plx/Documents/a"))
            self.assertFalse(allows(f"{host}/Shared Documents/a"))    # no site segment
            self.assertFalse(allows(None))
            with mock.patch.dict(os.environ, {"PLX_STAFF_SCOPE": "off"}):
                self.assertFalse(allows(ops))
        for unset in ("", " , junk!"):
            with mock.patch.dict(os.environ, {staff_scope.RESTRICTED_SITES_ENV: unset}):
                self.assertFalse(allows(ops), repr(unset))   # unset: every site restricted
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(staff_scope.RESTRICTED_SITES_ENV, None)
            self.assertFalse(allows(ops))

    def test_restricted_site_keeps_per_file_check_through_the_item_gate(self):
        """Through ``_employee_acl_denial`` (every route's item check): a payroll file is
        read by a reader on its permission list only; a business file by every staff."""
        entitlement = SimpleNamespace(
            actor=ACTOR, file_class_allows=lambda _r: True, full_read_allows=lambda *a: False,
            item_acl_allows=lambda acl, statuses: OWN in (acl.get("identities") or ()))
        fake = SimpleNamespace(employee_gate=SimpleNamespace(entitlement=lambda _a: entitlement),
                               people_exclusion=PeopleExclusion([EXCLUDED]),
                               staff_shared_mailboxes=frozenset,
                               staff_site_baselines=lambda: {
                                   "b!drive0000001": frozenset({"group:payroll"})})
        fake._staff_item_allows = lambda *a: CaseRuntime._staff_item_allows(fake, *a)

        def denial(site, identities):
            record = {"original_url": f"https://t.sharepoint.com/sites/{site}/D/x.pdf",
                      "drive_id": "b!drive0000001", "last_modified_by": "pat@plx.example",
                      "source_acl": {"status": "read", "identities": identities}}
            return CaseRuntime._employee_acl_denial(fake, ACTOR, "sharepoint", record)

        with mock.patch.dict(os.environ, {staff_scope.RESTRICTED_SITES_ENV:
                                          "ConfidentialPayrollInformation"}):
            self.assertEqual(denial("ConfidentialPayrollInformation", ["group:payroll"]),
                             EMPLOYEE_ACL_DENIED)
            self.assertIsNone(denial("ConfidentialPayrollInformation", [OWN]))
            self.assertIsNone(denial("Operations", ["group:payroll"]))


class SiteInheritanceTests(unittest.TestCase):
    """Review 2026-11-17 (payroll leak): a business site's file opens to staff only when it
    carries its library's and site's inherited permissions or more, and never under a pay,
    HR or confidential path or a listed folder; checked through ``_employee_acl_denial``
    over a real SharePoint capture store (``CaseRuntime.staff_site_baselines``)."""

    HOST = "https://t.sharepoint.com"
    DRIVE = "b!driveOperations0001"
    # The Operations site's permission list as captured (site groups, the group's owners
    # claim, the existing-access link): what every file of the site carries.
    SITE = ["sitegroup:5", "sitegroup:operations members", "sitegroup:3",
            "sitegroup:operations owners", "sitegroup:4", "sitegroup:operations visitors",
            "c:0o.c|federateddirectoryclaimprovider|g-ops_o", "group:g-ops",
            "link-scope:existingaccess"]
    LISTED = "66666666-6666-4666-8666-666666666666"   # a payroll clerk on narrowed lists

    def _store(self, files):
        """A SharePoint capture store holding ``files``: (drive, url, identities,
        status) per current item."""
        from integrations import capture_sharepoint as cs
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = cs.SharePointCaptureStore(tmp.name)
        with store.db:
            for n, (drive, url, identities, status) in enumerate(files):
                store.db.execute(
                    "INSERT INTO items (l1_handle, drive_id, item_id, etag, root_path, path, "
                    "record, resources, source_acl, digest, content_status, text_status, "
                    "recorded_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"sharepoint://{drive}/i{n}@e", drive, f"i{n}", "e", "/", "/x",
                     json.dumps({"original_url": url}), "[]",
                     json.dumps({"status": status, "identities": identities}), "d",
                     "stored", "stored", "2026-11-17T00:00:00Z", "2026-11-17T00:00:00Z"))
        store.close()
        return tmp.name

    def _fake(self, state_dir):
        entitlement = SimpleNamespace(
            actor=ACTOR, file_class_allows=lambda _r: True,
            full_read_allows=lambda *a: False,
            item_acl_allows=lambda acl, statuses: acl.get("status") in statuses
            and self.reader in (acl.get("identities") or ()))
        fake = SimpleNamespace(
            employee_gate=SimpleNamespace(entitlement=lambda _a: entitlement),
            people_exclusion=PeopleExclusion([EXCLUDED]), staff_shared_mailboxes=frozenset,
            captures=SimpleNamespace(sharepoint_capture_dir=state_dir))
        fake.staff_site_baselines = lambda: CaseRuntime.staff_site_baselines(fake)
        fake._staff_item_allows = lambda *a: CaseRuntime._staff_item_allows(fake, *a)
        return fake

    def _opens(self, fake, path, identities, drive=None, status="read", **extra):
        record = {"original_url": f"{self.HOST}/sites/Operations/Shared%20Documents/{path}",
                  "drive_id": drive or self.DRIVE, "last_modified_by": "pat@plx.example",
                  "source_acl": {"status": status, "identities": identities}, **extra}
        return CaseRuntime._employee_acl_denial(fake, ACTOR, "sharepoint", record) is None

    def test_narrower_and_payroll_files_keep_their_check_inheriting_files_open(self):
        narrow = ["sitegroup:operations owners", self.LISTED]
        url = f"{self.HOST}/sites/Operations/Shared%20Documents/a.pdf"
        for n in (1, 10, 60):
            files = [(self.DRIVE, url, self.SITE, "read")] * (2 * n) \
                + [(self.DRIVE, url, narrow, "read")] * n
            fake = self._fake(self._store(files))
            listed = {staff_scope.RESTRICTED_SITES_ENV: "Finance,Operations/Grants/Locked"}
            self.reader = OWN   # staff, on no narrowed list
            with mock.patch.dict(os.environ, listed):
                self.assertTrue(self._opens(fake, "Quotes/q1.xlsx", self.SITE), n)
                self.assertTrue(self._opens(fake, "Quotes/q2.xlsx",     # broader: opens
                                            self.SITE + [OTHER, "link-scope:organization"]))
                # unique permissions narrower than the site's (the file's own, or a
                # folder's it inherits): kept for the people listed
                self.assertFalse(self._opens(fake, "Board/minutes.docx", narrow), n)
                self.assertFalse(self._opens(fake, "Board/x.docx", self.SITE[2:]))
                self.assertFalse(self._opens(fake, "Quotes/q3.xlsx", self.SITE,
                                             item_source_acl={"status": "read",
                                                              "identities": narrow}))
                self.assertFalse(self._opens(fake, "Quotes/q4.xlsx", self.SITE,
                                             status="partial"))
                self.assertFalse(self._opens(fake, "Quotes/q5.xlsx", self.SITE,
                                             drive="b!driveNotCaptured01"))
                # The real Operations payroll records: the site's own permission list, so
                # only the path tells them apart; a listed folder entry holds too.
                for path in ("Grants/_Restricted%20Payroll/NGen/Pay%20Statements/s.pdf",
                             "Mail/RE_%20Payroll%20stubs%20(3)/Ali%20Oct%2012%20paystub.pdf",
                             "HR/review.docx", "Staff/2026%20Salaries.xlsx",
                             "Staff/Pat%20Direct%20Deposit%20form.pdf",
                             "Hiring/Offer%20Letter%20-%20Pat.docx",
                             "Grants/Locked/plan.docx"):
                    self.assertFalse(self._opens(fake, path, self.SITE), path)
                self.assertTrue(self._opens(fake, "Grants/Unlocked/plan.docx", self.SITE))
                self.assertTrue(self._opens(fake, "Shrink%20wrap/spec.pdf", self.SITE))
            self.reader = self.LISTED   # a reader on the list still reads them
            with mock.patch.dict(os.environ, listed):
                self.assertTrue(self._opens(fake, "Board/minutes.docx", narrow))
                self.assertTrue(self._opens(
                    fake, "Grants/_Restricted%20Payroll/NGen/s.pdf", self.SITE + [self.LISTED]))

    def test_no_clear_baseline_or_no_capture_opens_nothing(self):
        url = f"{self.HOST}/sites/Operations/Shared%20Documents/a.pdf"
        self.reader = OWN
        listed = {staff_scope.RESTRICTED_SITES_ENV: "Finance"}
        # most files narrowed one way or another: no inherited set to compare with
        files = [(self.DRIVE, url, self.SITE, "read")] * 4 \
            + [(self.DRIVE, url, [f"user{i}@plx.example"], "read") for i in range(5)]
        # mostly unreadable lists: no baseline either
        files += [("b!driveOther000001", url.replace("Operations", "Other"), self.SITE,
                   "read")] + [("b!driveOther000001", url.replace("Operations", "Other"),
                                [], "unreadable")] * 3
        fake = self._fake(self._store(files))
        with mock.patch.dict(os.environ, listed):
            self.assertFalse(self._opens(fake, "Quotes/q1.xlsx", self.SITE))
            self.assertFalse(self._opens(fake, "Quotes/q1.xlsx", self.SITE,
                                         drive="b!driveOther000001"))
            for state_dir in (None, tempfile.gettempdir() + "/plx-no-capture-here"):
                fake = self._fake(state_dir)
                self.assertFalse(self._opens(fake, "Quotes/q1.xlsx", self.SITE))
        # a site whose two libraries carry different inherited sets: the smaller
        # library's files must carry the site's set as well
        other = "b!driveOperations0002"
        files = [(self.DRIVE, url, self.SITE, "read")] * 6 \
            + [(other, url, ["sitegroup:operations owners", "group:g-ops"], "read")] * 2
        fake = self._fake(self._store(files))
        with mock.patch.dict(os.environ, listed):
            self.assertTrue(self._opens(fake, "Quotes/q1.xlsx", self.SITE))
            self.assertFalse(self._opens(fake, "Quotes/q1.xlsx",
                                         ["sitegroup:operations owners", "group:g-ops"],
                                         drive=other))

    def test_baselines_are_in_the_key(self):
        from operating.ontology_tools_results import runtime_key_material
        entitlement = SimpleNamespace(fingerprint="fp", unrestricted=lambda: False)
        held = {"value": {}}
        runtime = SimpleNamespace(
            people_exclusion=PeopleExclusion([EXCLUDED]),
            employee_gate=SimpleNamespace(entitlement=lambda _a: entitlement),
            staff_shared_mailboxes=frozenset, staff_site_baselines=lambda: held["value"])
        runtime.staff_key_material = lambda: CaseRuntime.staff_key_material(runtime)
        with mock.patch.dict(os.environ, {staff_scope.RESTRICTED_SITES_ENV: "Finance"}):
            before = json.dumps(runtime_key_material(runtime, ACTOR), sort_keys=True)
            held["value"] = {"b!d": frozenset({"sitegroup:5"})}
            self.assertNotEqual(before, json.dumps(runtime_key_material(runtime, ACTOR),
                                                   sort_keys=True))


class PlannerTests(unittest.TestCase):
    """Plans are business records: every staff reader sees every plan and task (a plan's
    unit names no members), except anything naming an excluded person."""

    PLAN = "PlanAAAAAAAAAAAAAAAAAAAAAAAA"

    def test_every_plan_unit_granted_to_staff(self):
        for n in (1, 10, 60):
            plans = [f"meetings:plan:Plan{i:024d}" for i in range(n)]
            runtime, live, mirror = _runtime()
            keys = KEYS + plans + ["meetings:plan:*", "meetings:plan:bad id"]
            policy = _policy(runtime, keys=keys,
                             units={**UNITS, **{p: Unit("personal") for p in plans}})
            self.assertEqual([p for p in plans if not policy.may_read(ACTOR, p)], [], n)
            self.assertFalse(policy.may_read(ACTOR, "meetings:plan:*"))
            self.assertFalse(policy.may_read(ACTOR, "meetings:plan:bad id"))
            self.assertFalse(policy.may_read(ACTOR, "meetings:plan:Unlisted0000000000000000"))
            self.assertEqual(live.calls + mirror.calls, 0)
        runtime, _live, _mirror = _runtime()
        plan = f"meetings:plan:{self.PLAN}"
        policy = _policy(runtime, keys=KEYS + [plan],
                         units={**UNITS, plan: Unit("personal", frozenset({EXCLUDED}))})
        self.assertFalse(policy.may_read(ACTOR, plan))
        runtime, _live, _mirror = _runtime(PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assertFalse(_policy(runtime, keys=KEYS + [plan],
                                 units={**UNITS, plan: Unit("personal")}).may_read(ACTOR, plan))

    def test_task_item_opens_to_staff_not_in_the_plan(self):
        from operating.case_runtime import _EmployeeItemAcl
        fake = SimpleNamespace(people_exclusion=PeopleExclusion([EXCLUDED]),
                               staff_shared_mailboxes=frozenset)
        entitlement = SimpleNamespace(actor=ACTOR, item_acl_allows=lambda *a, **k: False,
                                      full_read_allows=lambda *a, **k: False)
        audience = SimpleNamespace(tenant="plx", allows=lambda *_a: True)
        gate = _EmployeeItemAcl(audience, entitlement, "planner-task", staff=(
            lambda acl: CaseRuntime._staff_item_allows(fake, entitlement, "planner-task",
                                                       acl, {})))
        resource = f"teams:planner:{self.PLAN}"

        def visible(*identities, status="derived"):
            return gate.item_visible(ACTOR, resource,
                                     {"status": status, "identities": list(identities)})

        self.assertTrue(visible("group:g-ops", OTHER))
        self.assertTrue(visible("group:g-ops"))
        self.assertFalse(visible("group:g-ops", EXCLUDED))      # names an excluded person
        self.assertFalse(visible("group:g-ops", status="read"))  # not the capture's shape
        self.assertFalse(gate.item_visible(ACTOR, resource, None))
        audience.allows = lambda *_a: False
        self.assertFalse(visible("group:g-ops"))                # the audience still decides
        audience.allows = lambda *_a: True
        fake.people_exclusion = PeopleExclusion(unavailable="excluded_people_oids_absent")
        self.assertFalse(visible("group:g-ops"))
        with mock.patch.dict(os.environ, {"PLX_STAFF_SCOPE": "off"}):
            fake.people_exclusion = PeopleExclusion([EXCLUDED])
            self.assertFalse(visible("group:g-ops"))


class StaffKeyMaterialTests(unittest.TestCase):
    """A recorded item decision (operating.pg.item_audiences) never outlives a change of
    the staff rule's inputs: restricted sites and shared mailboxes are in the key."""

    def test_key_changes_with_restricted_sites_and_shared_mailboxes(self):
        from operating.ontology_tools_results import runtime_key_material
        shared = {"value": frozenset()}
        entitlement = SimpleNamespace(fingerprint="fp", unrestricted=lambda: False)
        runtime = SimpleNamespace(
            people_exclusion=PeopleExclusion([EXCLUDED]),
            employee_gate=SimpleNamespace(entitlement=lambda _a: entitlement),
            staff_shared_mailboxes=lambda: shared["value"], staff_site_baselines=dict)
        runtime.staff_key_material = lambda: CaseRuntime.staff_key_material(runtime)

        def key(sites):
            with mock.patch.dict(os.environ, {staff_scope.RESTRICTED_SITES_ENV: sites}):
                return json.dumps(runtime_key_material(runtime, ACTOR), sort_keys=True)

        base = key("Finance")
        self.assertNotEqual(base, key("Finance,AltonTax"))
        self.assertNotEqual(base, key(""))
        shared["value"] = frozenset({SHARED})
        self.assertNotEqual(base, key("Finance"))
        entitlement.unrestricted = lambda: True   # a full reader's key does not move
        self.assertEqual(key("Finance"), key("AltonTax"))



class LoadUnitsKept(unittest.TestCase):
    def test_a_new_generation_with_the_same_units_keeps_the_same_object(self):
        """A search write that changes no unit (new chunks) moves the generation: the units
        read again are the very same object (a policy kept over them stays kept); a unit
        changed is a new object."""
        import threading
        state = {"generation": 1, "rows": [("mail:a", "shared", ["x@plx.example"])]}

        class _Pg:
            def execute(self, sql, *args):
                if "write_generation" in sql:
                    return SimpleNamespace(fetchone=lambda: (state["generation"], True))
                return SimpleNamespace(fetchall=lambda: list(state["rows"]))

            def rollback(self):
                pass
        reader = SimpleNamespace(lock=threading.Lock(), conn=SimpleNamespace(
            _lock=threading.Lock(), _pg=_Pg()))
        self.addCleanup(staff_scope._UNITS.pop, id(reader), None)
        first = staff_scope.load_units(reader)
        self.assertEqual(first, {"mail:a": Unit("shared", frozenset({"x@plx.example"}))})
        state["generation"] = 2
        self.assertIs(staff_scope.load_units(reader), first)
        state["generation"], state["rows"] = 3, [("mail:a", "restricted", ["x@plx.example"])]
        changed = staff_scope.load_units(reader)
        self.assertIsNot(changed, first)
        self.assertEqual(changed["mail:a"].sharing, "restricted")


class RequestGeneration(unittest.TestCase):
    """A request's Postgres check reads the search write generation with it
    (``PgStoreReader.whole_stamp``), and the staff scope keeps its units by that one: no
    second round trip per policy question inside the request, its own read outside one."""

    def _reader(self, state):
        import threading

        class _Pg:
            def execute(self, sql, *args):
                state["sql"].append(sql)
                if "write_generation" in sql:
                    return SimpleNamespace(fetchone=lambda: (state["generation"], True))
                return SimpleNamespace(fetchall=lambda: list(state["rows"]))

            def rollback(self):
                pass
        reader = SimpleNamespace(lock=threading.Lock(), conn=SimpleNamespace(
            _lock=threading.Lock(), _pg=_Pg()))
        self.addCleanup(staff_scope._UNITS.pop, id(reader), None)
        return reader

    def test_a_generation_given_is_not_read_again_and_a_new_one_reads_the_units(self):
        state = {"generation": 1, "sql": [],
                 "rows": [("mail:a", "shared", ["x@plx.example"])]}
        reader = self._reader(state)
        first = staff_scope.load_units(reader, generation=7)
        self.assertFalse([s for s in state["sql"] if "write_generation" in s])
        state["sql"].clear()
        self.assertIs(staff_scope.load_units(reader, generation=7), first)
        self.assertEqual(state["sql"], [])   # the same generation: nothing read
        state["rows"] = [("mail:a", "restricted", ["x@plx.example"])]
        changed = staff_scope.load_units(reader, generation=8)
        self.assertEqual(changed["mail:a"].sharing, "restricted")   # a write: read again
        state["sql"].clear()
        staff_scope.load_units(reader)   # none given: its own read, as before
        self.assertTrue([s for s in state["sql"] if "write_generation" in s])

    def test_the_host_passes_the_requests_generation_only_inside_the_request(self):
        from operating.ontology_host import CaseHostTools
        host = object.__new__(CaseHostTools)
        host._reader, host._depth, host._search_generation = object(), 0, None
        seen = []
        with mock.patch.object(staff_scope, "load_units",
                               lambda reader, generation=None: seen.append(generation)), \
                mock.patch.dict(os.environ, {"PLX_STAFF_SCOPE": ""}):
            host._search_generation = 5
            host._staff_units()            # outside a request: never a held generation
            host._depth = 1
            host._staff_units()
            host._search_generation = None
            host._staff_units()            # the check read none: read its own
        self.assertEqual(seen, [None, 5, None])

    def _whole_reader(self, rows, fail_first=False):
        import threading
        from operating.pg import ontology_reader as orr
        calls = []

        class _Pg:
            def execute(self, sql, *args):
                calls.append(sql)
                if fail_first and "pg_sequence_last_value" in sql:
                    raise RuntimeError("permission denied")
                row = rows["with"] if "pg_sequence_last_value" in sql else rows["plain"]
                return SimpleNamespace(fetchone=lambda: row)

            def rollback(self):
                calls.append("ROLLBACK")
        reader = object.__new__(orr.PgStoreReader)
        reader.lock = threading.Lock()
        reader.conn = SimpleNamespace(_lock=threading.Lock(), _pg=_Pg())
        self.enterContext(mock.patch.object(orr, "whole_problem", lambda row: None))
        return reader, calls

    def test_whole_stamp_reads_the_generation_in_the_same_statement(self):
        rows = {"with": ([], "{}", True, 41, True, 9), "plain": ([], "{}", True, 41)}
        reader, calls = self._whole_reader(rows)
        self.assertEqual(reader.whole_stamp(), ("postgres", 41))
        self.assertEqual(reader.search_generation, 9)
        self.assertEqual(len(calls), 1)
        rows["with"] = ([], "{}", True, 42, True, None)   # never written: 0, as before
        reader.whole_stamp()
        self.assertEqual(reader.search_generation, 0)
        rows["with"] = ([], "{}", True, 42, False, None)  # no search tables: none held
        reader.whole_stamp()
        self.assertIsNone(reader.search_generation)

    def test_whole_stamp_unable_to_read_the_generation_asks_without_it(self):
        rows = {"with": None, "plain": ([], "{}", True, 41)}
        reader, calls = self._whole_reader(rows, fail_first=True)
        self.assertEqual(reader.whole_stamp(), ("postgres", 41))
        self.assertIsNone(reader.search_generation)
        self.assertEqual(calls[1], "ROLLBACK")
        calls.clear()
        self.assertEqual(reader.whole_stamp(), ("postgres", 41))
        self.assertEqual(len(calls), 1)   # from now on only the statement without it
        self.assertNotIn("pg_sequence_last_value", calls[0])

    def test_on_postgres_the_generation_read_is_write_generations(self):
        try:
            import psycopg
            from operating.tests import pg_testdb
            name, dsn = pg_testdb.create_db()
        except (ImportError, unittest.SkipTest) as exc:
            self.skipTest(f"no Postgres: {exc}")
        self.addCleanup(pg_testdb.drop_db, name)
        from operating.pg.ontology_reader import _GENERATION_SQL
        from operating.pg.permissions import write_generation
        with psycopg.connect(dsn, autocommit=True) as pg:
            ask = "SELECT 1" + _GENERATION_SQL
            self.assertEqual(pg.execute(ask).fetchone()[1:], (False, None))  # no table
            pg.execute("CREATE SCHEMA search")
            pg.execute("CREATE SEQUENCE search.write_generation")
            self.assertEqual(pg.execute(ask).fetchone()[1:], (True, None))
            self.assertEqual(write_generation(pg), 0)
            for _ in range(3):
                pg.execute("SELECT nextval('search.write_generation')")
                self.assertEqual(pg.execute(ask).fetchone()[2], write_generation(pg))


class ReaderScopeRulesInOneRequest(unittest.TestCase):
    def test_the_watch_rules_are_read_inside_one_request(self):
        """A visit's watch-rule types are read under the session's request
        (``HostAccess.request``: its kept policy), not a policy decided outside one."""
        from contextlib import contextmanager
        from operator_api import page_parts, page_sql
        inside = []

        class _Access:
            depth = 0

            @contextmanager
            def request(self):
                self.depth += 1
                try:
                    yield self
                finally:
                    self.depth -= 1
        view = SimpleNamespace(access=_Access(), routing=lambda: {"routed": True})
        worker = SimpleNamespace(run=lambda reader, job, **kw: job(view))
        backend = SimpleNamespace(access_worker=worker)
        with mock.patch.object(page_parts, "last_visit_of", lambda v: (None, None)), \
                mock.patch.object(page_sql, "_rule_types",
                                  lambda v, seen=None: inside.append(v.access.depth) or []):
            got = page_sql._reader_scope(backend, SimpleNamespace())
        self.assertEqual(inside, [1])
        self.assertEqual(got.routing, {"routed": True})


if __name__ == "__main__":
    unittest.main()
