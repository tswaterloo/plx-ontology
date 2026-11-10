"""The declared full-read grant (employee policy ``full_read``) at the item gates.

A named employee with the grant opens a mailbox they do not own, a SharePoint item whose
permission list does not name them and a Finance-class file, and holds the finance and
price markings; another employee of the same host does not. The people exclusion always
wins and must be loaded; ``mail_scope`` ``shared_only`` narrows mail to the declared shared
mailboxes; no entry, no grant; a wildcard or malformed entry is refused. Synthetic ids."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

from integrations.people_exclusion import PeopleExclusion
from operating import employee_permissions as ep
from operating.case_runtime import EMPLOYEE_ACL_DENIED, CaseRuntime, _EmployeeItemAcl
from ontology.core import Actor
from scripts import eval_owner_host as tool

TID = "11111111-2222-4333-8444-555555555555"
VINCE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
BOX = "cccccccc-bbbb-4ccc-8ddd-000000000001"       # someone else's mailbox
SHARED = "cccccccc-bbbb-4ccc-8ddd-000000000002"    # a shared/functional mailbox
EXCLUDED = "dddddddd-bbbb-4ccc-8ddd-000000000003"  # an excluded person
CLAIMS = {"uid": VINCE, "email": "vince@example.com", "entraTenantId": TID,
          "entraObjectId": VINCE}


def mail_acl(oid: str, address: str) -> dict:
    return {"status": "derived", "kind": "mailbox_owner", "identities": [oid, address]}


SP_RECORD = {"source_acl": {"status": "read", "identities": ["someone@example.com"]},
             "path": "/Shared Documents/Purchasing/quote.pdf"}
QC_RECORD = {"source_acl": {"status": "read", "identities": ["someone@example.com"]},
             "path": "/Shared Documents/Batches/B-100/coa.pdf"}


class FullReadGrantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        out = self.tmp / "host"
        with contextlib.redirect_stdout(io.StringIO()):
            code = tool.main(["--out", str(out), "--reader-env", "PLX_EVAL_READER_VINCE",
                              "--full-read"],
                             environ={"PLX_EVAL_READER_VINCE": json.dumps(CLAIMS)})
        self.assertEqual(code, 0)
        self.policy_doc = json.loads((out / "employee_policy.json").read_text("utf-8"))
        access = json.loads((out / "portal_access.json").read_text("utf-8"))
        other = dict(access["users"][0], id="other", email="other@example.com",
                     entraObjectId=OTHER)
        access["users"].append(other)   # same role and scopes, no grant
        self.access = self.tmp / "access.json"
        self.access.write_text(json.dumps(access), "utf-8")

    def resolver(self, doc=None, exclusion=None, access=None) -> ep.EmployeePermissionResolver:
        policy = ep.EmployeePermissionPolicy.from_json(doc or self.policy_doc)
        if access is not None:
            path = self.tmp / "access-alt.json"
            path.write_text(json.dumps(access), "utf-8")
            return ep.EmployeePermissionResolver(
                policy, ep.PortalAccessSnapshot(path),
                people_exclusion=PeopleExclusion([EXCLUDED]) if exclusion is None else exclusion)
        guard = PeopleExclusion([EXCLUDED]) if exclusion is None else exclusion
        return ep.EmployeePermissionResolver(policy, ep.PortalAccessSnapshot(self.access),
                                             people_exclusion=guard)

    def denial(self, resolver, oid, scheme, record):
        runtime = SimpleNamespace(employee_gate=ep.EmployeeActorGate(resolver, "plx"))
        return CaseRuntime._employee_acl_denial(
            runtime, Actor("plx", frozenset({"employee:" + oid}), "restricted"), scheme, record)

    def test_grant_opens_mail_sharepoint_and_finance_for_the_named_reader_only(self):
        resolver = self.resolver()
        box = {"source_acl": mail_acl(BOX, "box@example.com")}
        finance_file = dict(SP_RECORD, security_class="FINANCE")
        self.assertIsNone(self.denial(resolver, VINCE, "graph-mail", box))
        self.assertIsNone(self.denial(resolver, VINCE, "sharepoint", SP_RECORD))
        self.assertEqual(self.denial(resolver, OTHER, "graph-mail", box),
                         ep.MAILBOX_ACCESS_UNDETERMINED)
        self.assertEqual(self.denial(resolver, OTHER, "sharepoint", SP_RECORD),
                         EMPLOYEE_ACL_DENIED)
        vince = resolver.resolve(ep.EmployeePrincipal(TID, VINCE))
        other = resolver.resolve(ep.EmployeePrincipal(TID, OTHER))
        # An unclassified file may be Finance: opened under the grant's finance scope only.
        self.assertTrue(vince.file_class_allows(finance_file))
        self.assertFalse(other.file_class_allows(finance_file))
        # Teams through the capture store's per-record audience.
        audience = SimpleNamespace(tenant="plx", allows=lambda actor, resource: True)
        chat = {"status": "read", "identities": [BOX]}
        self.assertTrue(_EmployeeItemAcl(audience, vince, "teams-chat")
                        .item_visible(vince.actor, "teams:chat:x", chat))
        self.assertFalse(_EmployeeItemAcl(audience, other, "teams-chat")
                         .item_visible(other.actor, "teams:chat:x", chat))
        # Every marking for the owner grant (every source, every mailbox: unrestricted);
        # the named reader's principal only.
        self.assertTrue(vince.unrestricted())
        self.assertFalse(other.unrestricted())
        grants = ep.full_read_marking_grants({"finance": frozenset({"staff:x"})}, vince)
        self.assertEqual(grants["finance"], frozenset({"staff:x", "employee:" + VINCE}))
        from ontology.store_ddl import MARKINGS
        self.assertEqual({m for m, held in grants.items() if "employee:" + VINCE in held},
                         set(MARKINGS))
        # A grant short of a source holds the finance and price markings only.
        doc = json.loads(json.dumps(self.policy_doc))
        doc["full_read"][0]["scopes"] = ["finance", "mail", "sharepoint"]
        narrow = self.resolver(doc).resolve(ep.EmployeePrincipal(TID, VINCE))
        self.assertFalse(narrow.unrestricted())
        grants = ep.full_read_marking_grants({"finance": frozenset({"staff:x"})}, narrow)
        self.assertIn("employee:" + VINCE, grants["purchasing_prices"])
        self.assertNotIn("pii", grants)
        self.assertEqual(ep.full_read_marking_grants({"finance": frozenset({"staff:x"})}, other),
                         {"finance": frozenset({"staff:x"})})
        self.assertEqual(vince.full_read_code(),
                         "full_read:finance+mail+sharepoint+teams:mail_all")
        self.assertIsNone(other.full_read_code())
        self.assertNotEqual(vince.fingerprint, other.fingerprint)

    def test_grant_opens_meetings_items_by_their_derived_acl_and_never_an_excluded_one(self):
        vince = self.resolver().resolve(ep.EmployeePrincipal(TID, VINCE))
        other = self.resolver().resolve(ep.EmployeePrincipal(TID, OTHER))
        for scheme, kind in (("calendar-event", "mailbox_owner"),
                             ("planner-task", "plan_people"),
                             ("meeting-transcript", "meeting_participants")):
            with self.subTest(scheme):
                acl = {"status": "derived", "kind": kind, "identities": [BOX]}
                self.assertTrue(vince.full_read_allows(scheme, acl))
                self.assertFalse(other.full_read_allows(scheme, acl))
                self.assertFalse(vince.full_read_allows(scheme, dict(acl, identities=[
                    BOX, EXCLUDED])))
        # each scheme only with its own ACL kind
        self.assertFalse(vince.full_read_allows("planner-task", {
            "status": "derived", "kind": "meeting_participants", "identities": [BOX]}))

    def meetings_seen(self, readers, keys, guard=None):
        """{reader oid: the ``meetings:`` units ``case_host_policy`` grants it}."""
        from integrations.live_source_guard import LiveSourceAudience
        from operating import ontology_host
        guard = PeopleExclusion([EXCLUDED]) if guard is None else guard
        live = LiveSourceAudience.from_file(self.tmp / "host" / "live_audience.json")
        runtime = SimpleNamespace(
            employee_gate=ep.EmployeeActorGate(self.resolver(exclusion=guard), "plx"),
            people_exclusion=guard, live=SimpleNamespace(audience=live),
            mirror=SimpleNamespace(_audience=lambda: SimpleNamespace(
                allows=lambda actor, relation: True)))
        seen = {}
        for oid in readers:
            actor = Actor("plx", frozenset({"employee:" + oid}), "restricted")
            policy = ontology_host.case_host_policy(runtime, actor, None, audience_keys=keys)
            seen[oid] = {k for k in keys if policy.may_read(actor, k)}
        return seen

    def test_meetings_units_open_to_their_own_reader_and_the_full_read_owner_only(self):
        """Calendar, organizer and plan units are decided, never left to default deny: a
        reader sees their own calendar only, the full-read owner every calendar and plan,
        nobody an excluded person's calendar or organizer unit, and
        nobody any calendar when the exclusion is not loaded (fail closed); a malformed
        meetings unit never."""
        keys = [f"meetings:calendar:{VINCE}", f"meetings:calendar:{OTHER}",
                f"meetings:calendar:{BOX}", f"meetings:calendar:{EXCLUDED}",
                f"meetings:organizer:{BOX}", f"meetings:organizer:{EXCLUDED}",
                "meetings:plan:PlanOne", "meetings:other:x", "meetings:calendar:not-an-oid",
                "meetings:plan:*", "meetings:plan:Plan One"]
        excluded = {f"meetings:calendar:{EXCLUDED}", f"meetings:organizer:{EXCLUDED}"}
        seen = self.meetings_seen((VINCE, OTHER), keys)
        # the owner (full read over every mailbox): every calendar, organizer and plan
        # (a unit id outside its grammar, ``meetings:plan:*`` included, is never granted:
        # never a pattern over every plan)
        self.assertEqual(seen[VINCE], set(keys) - excluded - {
            "meetings:other:x", "meetings:calendar:not-an-oid", "meetings:plan:*",
            "meetings:plan:Plan One"})
        # another employee: their own calendar and the plan (its tasks per record) only
        self.assertEqual(seen[OTHER], {f"meetings:calendar:{OTHER}", "meetings:plan:PlanOne"})
        # an excluded person's calendar and transcripts: nobody
        for oid in (VINCE, OTHER):
            self.assertFalse(seen[oid] & excluded, oid)
        # no loaded exclusion: no calendar or organizer unit for anyone
        for guard in (PeopleExclusion(unavailable="excluded_people_unresolved"),):
            unloaded = self.meetings_seen((VINCE, OTHER), keys, guard)
            for oid in (VINCE, OTHER):
                self.assertFalse({k for k in unloaded[oid]
                                  if not k.startswith("meetings:plan:")}, oid)

    def test_planner_tasks_stay_decided_per_record_inside_a_granted_plan(self):
        """A plan unit granted on the audience opens no task by itself: each task is
        decided by its own captured people (``plan_people``): a reader the task names
        reads it, another does not, the full-read owner does unless an excluded person is
        among its people, and a task with no derived ACL is read by nobody."""
        resolver = self.resolver()
        vince = resolver.resolve(ep.EmployeePrincipal(TID, VINCE))
        other = resolver.resolve(ep.EmployeePrincipal(TID, OTHER))
        audience = SimpleNamespace(tenant="plx", allows=lambda actor, resource: True)
        unit = "meetings:plan:PlanOne"

        def visible(who, identities, status="derived"):
            acl = {"status": status, "kind": "plan_people", "identities": identities}
            return _EmployeeItemAcl(audience, who, "planner-task").item_visible(
                who.actor, unit, acl)
        self.assertFalse(visible(other, [BOX]))
        self.assertTrue(visible(other, [OTHER]))
        self.assertTrue(visible(vince, [BOX]))
        self.assertFalse(visible(vince, [BOX, EXCLUDED]))
        self.assertFalse(visible(vince, [BOX], status="unreadable"))
        self.assertFalse(visible(other, [OTHER], status="unreadable"))

    def test_excluded_person_stays_hidden_and_an_unloaded_exclusion_grants_nothing(self):
        resolver = self.resolver()
        excluded_box = {"source_acl": mail_acl(EXCLUDED, "gone@example.com")}
        self.assertEqual(self.denial(resolver, VINCE, "graph-mail", excluded_box),
                         ep.MAILBOX_ACCESS_UNDETERMINED)
        chat = {"status": "read", "identities": [EXCLUDED, BOX]}
        vince = resolver.resolve(ep.EmployeePrincipal(TID, VINCE))
        self.assertFalse(vince.full_read_allows("teams-chat", chat))
        # A mail ACL without the mailbox's object id cannot be decided: denied.
        self.assertFalse(vince.full_read_allows(
            "graph-mail", {"status": "derived", "kind": "mailbox_owner",
                           "identities": ["box@example.com"]}))
        for guard in (PeopleExclusion([]), PeopleExclusion(unavailable="excluded_people_unresolved")):
            unloaded = self.resolver(exclusion=guard)
            held = unloaded.resolve(ep.EmployeePrincipal(TID, VINCE))
            self.assertIsNone(held.full_read)
            self.assertIn("full_read:exclusion_unavailable", held.notes)
            self.assertEqual(self.denial(unloaded, VINCE, "graph-mail",
                                         {"source_acl": mail_acl(BOX, "box@example.com")}),
                             ep.MAILBOX_ACCESS_UNDETERMINED)
        # The grantee excluded: no grant.
        held = self.resolver(exclusion=PeopleExclusion([VINCE])).resolve(
            ep.EmployeePrincipal(TID, VINCE))
        self.assertIsNone(held.full_read)

    def test_the_unrestricted_owner_never_reads_an_excluded_persons_mailbox_or_files(self):
        """Owner-open keeps ``PLX_EXCLUDED_PEOPLE_OIDS`` fail-closed: the unrestricted
        reader opens no mailbox of an excluded person, no file an excluded person created
        (even under a read permission list that does not name them), no chat they are in;
        an exclusion that cannot be decided leaves nobody unrestricted."""
        resolver = self.resolver()
        vince = resolver.resolve(ep.EmployeePrincipal(TID, VINCE))
        self.assertTrue(vince.unrestricted())
        self.assertEqual(self.denial(resolver, VINCE, "graph-mail",
                                     {"source_acl": mail_acl(EXCLUDED, "gone@example.com")}),
                         ep.MAILBOX_ACCESS_UNDETERMINED)
        theirs = dict(SP_RECORD, created_by=EXCLUDED, last_modified_by=BOX)
        self.assertEqual(self.denial(resolver, VINCE, "sharepoint", theirs),
                         EMPLOYEE_ACL_DENIED)
        self.assertIsNone(self.denial(resolver, VINCE, "sharepoint",
                                      dict(theirs, created_by=BOX)))
        self.assertFalse(vince.full_read_allows(
            "teams-chat", {"status": "read", "identities": [BOX, EXCLUDED]}))
        for guard in (PeopleExclusion([]),
                      PeopleExclusion(unavailable="excluded_people_unresolved")):
            held = self.resolver(exclusion=guard).resolve(ep.EmployeePrincipal(TID, VINCE))
            self.assertFalse(held.unrestricted())
            gate = ep.EmployeeActorGate(self.resolver(exclusion=guard), "plx")
            self.assertFalse(ep.reader_unrestricted(
                gate, Actor("plx", frozenset({"employee:" + VINCE}), "restricted")))
        # Any failure deciding the flag is no flag.
        self.assertFalse(ep.reader_unrestricted(
            SimpleNamespace(entitlement=lambda actor: 1 / 0), object()))

    def test_mail_scope_shared_only_reads_only_declared_shared_mailboxes(self):
        doc = json.loads(json.dumps(self.policy_doc))
        doc["full_read"][0]["mail_scope"] = "shared_only"
        doc["shared_mailboxes"] = [SHARED]
        resolver = self.resolver(doc)
        self.assertIsNone(self.denial(resolver, VINCE, "graph-mail",
                                      {"source_acl": mail_acl(SHARED, "orders@example.com")}))
        self.assertEqual(self.denial(resolver, VINCE, "graph-mail",
                                     {"source_acl": mail_acl(BOX, "box@example.com")}),
                         ep.MAILBOX_ACCESS_UNDETERMINED)
        doc.pop("shared_mailboxes")   # none declared: no mailbox
        self.assertEqual(self.denial(self.resolver(doc), VINCE, "graph-mail",
                                     {"source_acl": mail_acl(SHARED, "orders@example.com")}),
                         ep.MAILBOX_ACCESS_UNDETERMINED)

    def test_no_grant_without_an_explicit_entry_and_malformed_entries_refused(self):
        doc = {k: v for k, v in self.policy_doc.items() if k != "full_read"}
        held = self.resolver(doc).resolve(ep.EmployeePrincipal(TID, VINCE))
        self.assertIsNone(held.full_read)
        self.assertEqual(self.denial(self.resolver(doc), VINCE, "sharepoint", SP_RECORD),
                         EMPLOYEE_ACL_DENIED)
        grant = self.policy_doc["full_read"][0]
        bad = [dict(grant, object_id="*"), dict(grant, scopes=["*"]), dict(grant, scopes=[]),
               dict(grant, scopes=["mail", "mail"]), dict(grant, mail_scope="some"),
               {k: v for k, v in grant.items() if k != "reason"},
               dict(grant, granted_at="yesterday"), dict(grant, extra=1),
               dict(grant, granted_by="*")]
        for entry in bad:
            with self.assertRaises(ValueError, msg=entry):
                ep.EmployeePermissionPolicy.from_json(dict(self.policy_doc, full_read=[entry]))
        with self.assertRaises(ValueError):   # one grant per employee
            ep.EmployeePermissionPolicy.from_json(dict(self.policy_doc, full_read=[grant, grant]))
        with self.assertRaises(ValueError):   # never for a service principal
            ep.EmployeePermissionPolicy.from_json(dict(
                self.policy_doc, service_principal_object_ids=[VINCE]))

    def test_scope_switches_narrow_the_grant(self):
        doc = json.loads(json.dumps(self.policy_doc))
        doc["full_read"][0]["scopes"] = ["sharepoint"]
        resolver = self.resolver(doc)
        self.assertEqual(self.denial(resolver, VINCE, "graph-mail",
                                     {"source_acl": mail_acl(BOX, "box@example.com")}),
                         ep.MAILBOX_ACCESS_UNDETERMINED)
        vince = resolver.resolve(ep.EmployeePrincipal(TID, VINCE))
        # Without the finance scope a file that may be Finance stays closed; its ACL opens.
        self.assertEqual(self.denial(resolver, VINCE, "sharepoint", SP_RECORD),
                         EMPLOYEE_ACL_DENIED)
        self.assertTrue(vince.full_read_allows("sharepoint", SP_RECORD["source_acl"]))
        self.assertFalse(vince.full_read_allows("teams-chat", {"identities": [BOX]}))
        self.assertNotIn("employee:" + VINCE,
                         ep.full_read_marking_grants({}, vince).get("finance", frozenset()))

    def test_label_and_journal_entry_for_what_is_served_under_the_grant(self):
        from operator_api import auth, backend
        from operator_api.workspace.session_journal import SessionJournal
        resolver = self.resolver()
        vince = auth.configured_employee(resolver, VINCE)()
        other = auth.configured_employee(resolver, OTHER)()
        self.assertEqual(backend.access_label(vince), "Viewing with full-read access")
        self.assertIsNone(backend.access_label(other))
        # The grant is re-checked on each resolve: an unloaded exclusion drops the label.
        unloaded = self.resolver(exclusion=PeopleExclusion([]))
        self.assertIsNone(backend.access_label(auth.configured_employee(unloaded, VINCE)()))
        app = backend.WorkspaceBackend(lambda session: None, data_label="test",
                                       state_dir=self.tmp)
        reader = auth.Reader(session=vince, mode="dev", display_name="Vince")
        app.journal_full_read(reader, "/api/ask")
        SessionJournal.for_state_dir(self.tmp).record(method="GET", path="/api/source",
                                                      status=200, session=other)
        rows = SessionJournal.for_state_dir(self.tmp)
        import sqlite3
        with sqlite3.connect(rows.path) as db:
            got = db.execute("SELECT route, employee, access FROM request ORDER BY id").fetchall()
        self.assertEqual(got, [("/api/ask", "employee:" + VINCE,
                                "full_read:finance+mail+sharepoint+teams:mail_all"),
                               ("/api/source", "employee:" + OTHER, None)])
        self.assertEqual(app.me(reader).access_label, "Viewing with full-read access")

    def test_quality_is_not_granted_a_non_qc_grantee_opens_no_batches_qc_document(self):
        access = json.loads(self.access.read_text("utf-8"))
        access["users"][0].update(role="STAFF", accessLevel="L2")   # Vince, not admin/QC
        # A grant short of a source (no Teams): not the owner's, so quality is not granted.
        doc = json.loads(json.dumps(self.policy_doc))
        doc["full_read"][0]["scopes"] = ["finance", "mail", "sharepoint"]
        path = self.tmp / "access-alt.json"
        path.write_text(json.dumps(access), "utf-8")
        resolver = ep.EmployeePermissionResolver(
            ep.EmployeePermissionPolicy.from_json(doc), ep.PortalAccessSnapshot(path),
            people_exclusion=PeopleExclusion([EXCLUDED]))
        vince = resolver.resolve(ep.EmployeePrincipal(TID, VINCE))
        self.assertIsNotNone(vince.full_read)
        self.assertFalse(vince.qc_document_reader)
        self.assertEqual(self.denial(resolver, VINCE, "sharepoint", QC_RECORD),
                         EMPLOYEE_ACL_DENIED)
        self.assertIsNone(self.denial(resolver, VINCE, "sharepoint", SP_RECORD))
        # The owner grant (every source: unrestricted) opens it without a QC role.
        owner = self.resolver(access=access)
        self.assertFalse(owner.resolve(ep.EmployeePrincipal(TID, VINCE)).qc_document_reader)
        self.assertIsNone(self.denial(owner, VINCE, "sharepoint", QC_RECORD))
        # The admin grantee (a QC document reader) opens it.
        self.assertIsNone(self.denial(self.resolver(), VINCE, "sharepoint", QC_RECORD))

    def test_unread_sharepoint_permissions_open_only_with_cleared_creator_and_modifier(self):
        resolver = self.resolver()
        unread = dict(SP_RECORD, source_acl={"status": "unreadable"})
        self.assertEqual(self.denial(resolver, VINCE, "sharepoint", unread), EMPLOYEE_ACL_DENIED)
        named = dict(unread, created_by=BOX, last_modified_by=SHARED)
        self.assertIsNone(self.denial(resolver, VINCE, "sharepoint", named))
        for record in (dict(unread, last_modified_by=SHARED),          # creator unknown
                       dict(unread, created_by=BOX, last_modified_by=EXCLUDED),
                       # an address an unbound guard cannot decide: withheld, not blind
                       dict(unread, created_by=BOX, last_modified_by="x@example.com")):
            self.assertEqual(self.denial(resolver, VINCE, "sharepoint", record),
                             EMPLOYEE_ACL_DENIED)

    def test_a_changed_exclusion_list_applies_without_a_restart(self):
        from integrations.people_exclusion import ReloadingExclusion
        listing = self.tmp / "excluded.json"
        listing.write_text(json.dumps({"schema": "plx-people-exclusion/v1",
                                       "object_ids": [EXCLUDED]}), "utf-8")
        now = [0.0]
        guard = ReloadingExclusion({"PLX_EXCLUDED_PEOPLE_FILE": str(listing)},
                                   config=self.tmp / "none.json", clock=lambda: now[0])
        resolver = self.resolver(exclusion=guard)
        vince = resolver.resolve(ep.EmployeePrincipal(TID, VINCE))
        box = {"source_acl": mail_acl(BOX, "box@example.com")}
        self.assertIsNone(self.denial(resolver, VINCE, "graph-mail", box))
        listing.write_text(json.dumps({"schema": "plx-people-exclusion/v1",
                                       "object_ids": [EXCLUDED, BOX]}), "utf-8")
        self.assertFalse(vince.full_read_allows("graph-mail", box["source_acl"]))
        listing.write_text("not json", "utf-8")   # unreadable now: the grant opens nothing
        now[0] += 301
        self.assertFalse(vince.full_read.usable())

    def test_the_app_s_case_host_keeps_the_grant_s_finance_markings(self):
        """The app and console read through ``host_access``'s case host: the finance and
        price markings the grant holds (and the host config's marking grants) stay in its
        policy, with the console's own markings added, never substituted (the reader once
        saw every item ledger entry withheld under a finance grant)."""
        from unittest import mock
        from operating import ontology_host
        from operating.case_tools_host import CaptureCaseHostTools
        from operator_api.workspace.object_workspace import host_access
        runtime = SimpleNamespace(
            employee_gate=ep.EmployeeActorGate(self.resolver(), "plx"),
            mirror=SimpleNamespace(_audience=lambda: SimpleNamespace(
                allows=lambda actor, relation: True)))

        def base_policy(host, reader):
            return ontology_host.case_host_policy(
                runtime, reader, None, marking_grants={"finance": frozenset({"staff:x"})})
        with mock.patch.object(CaptureCaseHostTools, "__init__", lambda *a, **k: None), \
                mock.patch.object(CaptureCaseHostTools, "policy_now", base_policy):
            held = {}
            for oid in (VINCE, OTHER):
                actor = Actor("plx", frozenset({"employee:" + oid}), "restricted")
                host = host_access(runtime, None, actor, markings=("l5",)).host
                held[oid] = set(host.policy_now(actor).held_markings(actor))
        from ontology.store_ddl import MARKINGS
        self.assertEqual(held[VINCE], set(MARKINGS))   # unrestricted: every marking
        self.assertEqual(held[OTHER], {"l5"})

    def test_no_access_caveat_for_a_reader_whose_grant_opens_every_item(self):
        """Under a full-read grant of mail (all mailboxes), SharePoint and Teams no item
        permission withholds anything from the reader: the app's "may be in places you
        can't open" caveats are not shown unless a count says something was withheld; a
        reader without it, or with a narrower grant, keeps them."""
        from operator_api import backend, trail
        resolver = self.resolver()

        def view(oid, res=resolver):
            gate = ep.EmployeeActorGate(res, "plx")
            return SimpleNamespace(access=SimpleNamespace(
                runtime=SimpleNamespace(employee_gate=gate),
                actor=Actor("plx", frozenset({"employee:" + oid}), "restricted")))
        self.assertTrue(trail.reads_every_item(view(VINCE)))
        self.assertFalse(trail.reads_every_item(view(OTHER)))
        doc = json.loads(json.dumps(self.policy_doc))
        doc["full_read"][0]["mail_scope"] = "shared_only"
        self.assertFalse(trail.reads_every_item(view(VINCE, self.resolver(doc))))
        self.assertFalse(trail.reads_every_item(SimpleNamespace()))
        reply = ("I found no confirmation. Some mail and files are restricted from your "
                 "view, so one could still exist. Some mail and files were withheld from "
                 "this reader, so that isn't proof. The PO is Open.")
        self.assertEqual(backend.without_access_caveats(reply),
                         "I found no confirmation. The PO is Open.")

    def test_source_and_object_pages_are_labelled_and_journalled_under_the_grant(self):
        from operator_api import auth, backend, models as m
        resolver = self.resolver()
        app = backend.WorkspaceBackend(lambda session: None, data_label="test",
                                       state_dir=self.tmp)
        vince = auth.Reader(session=auth.configured_employee(resolver, VINCE)(), mode="entra",
                            display_name="V")
        other = auth.Reader(session=auth.configured_employee(resolver, OTHER)(), mode="entra",
                            display_name="O")
        view = m.SourceView(handle="h", system="Email", label="l", kind="text")
        self.assertEqual(app._labelled(vince, view, "/api/source").access_label,
                         "Viewing with full-read access")
        self.assertIsNone(view.access_label)          # the cached payload is not changed
        self.assertIsNone(app._labelled(other, view, "/api/object").access_label)
        app._labelled(vince, view, "/api/object")
        import sqlite3
        with sqlite3.connect(self.tmp / "console-journal.sqlite") as db:
            got = db.execute("SELECT route FROM request WHERE access IS NOT NULL").fetchall()
        self.assertEqual(got, [("/api/source",), ("/api/object",)])


if __name__ == "__main__":
    unittest.main()
