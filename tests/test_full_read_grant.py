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
        # Finance and price markings: the named reader's principal only.
        grants = ep.full_read_marking_grants({"finance": frozenset({"staff:x"})}, vince)
        self.assertEqual(grants["finance"], frozenset({"staff:x", "employee:" + VINCE}))
        self.assertIn("employee:" + VINCE, grants["purchasing_prices"])
        self.assertNotIn("pii", grants)
        self.assertEqual(ep.full_read_marking_grants({"finance": frozenset({"staff:x"})}, other),
                         {"finance": frozenset({"staff:x"})})
        self.assertEqual(vince.full_read_code(),
                         "full_read:finance+mail+sharepoint+teams:mail_all")
        self.assertIsNone(other.full_read_code())
        self.assertNotEqual(vince.fingerprint, other.fingerprint)

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
        from operator_ui.session_journal import SessionJournal
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
        resolver = self.resolver(access=access)
        vince = resolver.resolve(ep.EmployeePrincipal(TID, VINCE))
        self.assertIsNotNone(vince.full_read)
        self.assertFalse(vince.qc_document_reader)
        self.assertEqual(self.denial(resolver, VINCE, "sharepoint", QC_RECORD),
                         EMPLOYEE_ACL_DENIED)
        self.assertIsNone(self.denial(resolver, VINCE, "sharepoint", SP_RECORD))
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
