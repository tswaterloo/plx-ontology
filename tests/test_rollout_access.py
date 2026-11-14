"""scripts/rollout_access.py: the per-person access preview for the rollout (O-02).

Evidence label: ``isolated-fixture``. Portal-shaped rows, a fake GET-only client and a
mirror-shaped SQLite file; no network, no credential, nobody signed in.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from integrations.people_exclusion import PeopleExclusion
from integrations.workbench_read_api import RelationUnavailable
from operating import employee_permissions as ep
from scripts import rollout_access as ra

TENANT = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"
EXCLUDED = "33333333-3333-4333-8333-333333333333"
FIN_ID, OPS_ID = "dept-fin", "dept-ops"


def oid(n: int) -> str:
    return f"44444444-4444-4444-8444-{n:012d}"


def tables(n_staff: int = 2) -> dict:
    users = [{"id": "u-owner", "role": "SUPER_ADMIN", "access_level": "L5", "is_active": 1},
             {"id": "u-excl", "role": "MANAGER", "access_level": "L3", "is_active": True},
             {"id": "u-cust", "role": "CUSTOMER", "access_level": "L1", "is_active": True},
             {"id": "u-gone", "role": "STAFF", "access_level": "L2", "is_active": False},
             {"id": "u-fin", "role": "MANAGER", "access_level": "L3", "is_active": True}]
    users += [{"id": f"u-s{i}", "role": "STAFF", "access_level": "L2", "is_active": True}
              for i in range(n_staff)]
    return {
        ra.USERS: users,
        ra.DEPARTMENTS: [{"id": FIN_ID, "code": "FIN", "is_active": True},
                         {"id": OPS_ID, "code": "OPS", "is_active": True}],
        ra.MEMBERS: [{"id": "m1", "user_id": "u-fin", "department_id": FIN_ID,
                      "provenance": "PORTAL"},
                     {"id": "m2", "user_id": "u-fin", "department_id": "dept-unknown",
                      "provenance": "PORTAL"}],
        ra.SCOPES: [{"id": "s1", "user_id": "u-fin", "scope_type": "COMPANY",
                     "scope_key": "petra-hygienic-plx", "effect": "ALLOW"}],
        ra.GRANTS: [{"id": "g1", "subject_id": "u-fin", "effect": "DENY",
                     "capability": "VIEW_CUSTOMERS", "state": "ACTIVE", "revoked_at": None},
                    {"id": "g2", "subject_id": "u-fin", "effect": "ALLOW",
                     "capability": "VIEW_OPERATIONS", "state": "ACTIVE"}],
    }


def identities(n_staff: int = 2) -> dict:
    out = {"u-owner": {"email": "owner@plx.example", "entraObjectId": OWNER},
           "u-excl": {"email": "excl@plx.example", "entraObjectId": EXCLUDED},
           "u-fin": {"email": "fin@plx.example", "entraObjectId": oid(1)}}
    out.update({f"u-s{i}": {"email": f"s{i}@plx.example", "entraObjectId": oid(100 + i)}
                for i in range(n_staff)})
    return out


def policy(full_read: list[str] = (OWNER,)) -> ep.EmployeePermissionPolicy:
    return ep.EmployeePermissionPolicy.from_json({
        "schema": ep.POLICY_SCHEMA, "tenant": "plx", "entra_tenant_id": TENANT,
        "clearance_by_access_level": {"L1": "internal", "L2": "internal",
                                      "L3": "confidential", "L4": "confidential",
                                      "L5": "restricted"},
        "principals": {"staff:finance": {"portal_roles": ["MANAGER", "ADMIN", "SUPER_ADMIN"]},
                       "staff:floor": {"portal_roles": ["STAFF"]}},
        "full_read": [{"object_id": o, "granted_by": "owner", "granted_at": "2026-11-01T00:00:00Z",
                       "reason": "owner", "scopes": ["finance", "mail", "sharepoint", "teams"]}
                      for o in full_read]})


class FakeClient:
    """The reporting service's three routes; ``app_user`` listed unavailable."""

    def __init__(self, data: dict, unavailable: dict) -> None:
        self.data, self.unavailable, self.gets = data, unavailable, []

    def health(self):
        return {"unavailable_relations": dict(self.unavailable)}

    def relations(self):
        return {name: {"name": name, "columns": sorted({k for r in rows for k in r}) or ["id"],
                       "key": ["id"]} for name, rows in self.data.items()}

    def read_relation(self, name, *, columns):
        self.gets.append(name)
        if name in self.unavailable:
            raise RelationUnavailable(name, self.unavailable[name])
        return {"rows": self.data[name]}


def mirror_file(path: Path, data: dict, captured: str = "2026-11-13T10:00:00Z") -> None:
    conn = sqlite3.connect(path)
    conn.execute("create table relation_state (relation text primary key, columns_json text, "
                 "last_full_traversal_at text, last_refresh_at text)")
    conn.execute("create table row_current (relation text, key_json text, run_index int, "
                 "row_json text, missing_from_source_since text, deleted_in_source text)")
    for name, rows in data.items():
        conn.execute("insert into relation_state values (?,?,?,?)",
                     (name, json.dumps(sorted({k for r in rows for k in r})), captured, captured))
        for i, row in enumerate(rows):
            conn.execute("insert into row_current values (?,?,?,?,?,?)",
                         (name, json.dumps([row["id"]]), 0, json.dumps(row), None, None))
    conn.execute("insert into row_current values (?,?,?,?,?,?)",
                 (ra.USERS, '["u-deleted"]', 0, json.dumps(
                     {"id": "u-deleted", "role": "ADMIN", "access_level": "L4",
                      "is_active": True}), "2026-11-12T00:00:00Z", None))
    conn.commit()
    conn.close()


class SnapshotTest(unittest.TestCase):
    def test_live_first_mirror_fills_and_only_resolved_identities_are_written(self):
        data = tables()
        client = FakeClient(data, {ra.USERS: "1 approved column is missing or not granted"})
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp) / "mirror.sqlite"
            mirror_file(mirror, {ra.USERS: data[ra.USERS]})
            live = ra.read_live(client)
            got, basis = ra.merge(live, ra.read_mirror(mirror, ra.PEOPLE_RELATIONS))
        self.assertEqual(basis[ra.USERS]["basis"], "mirror")
        self.assertEqual(basis[ra.USERS]["live"], "1 approved column is missing or not granted")
        self.assertEqual(basis[ra.SCOPES]["basis"], "live")
        self.assertNotIn("u-deleted", {u["id"] for u in got[ra.USERS]})   # gone upstream
        rows = ra.join_identities(ra.portal_rows(got), {"u-fin": identities()["u-fin"]}, TENANT)
        fin = next(r for r in rows if r["id"] == "u-fin")
        self.assertEqual(fin["departmentIds"], [FIN_ID, "dept-unknown"])
        self.assertEqual(ra.department_codes(fin), ["FIN"])        # unknown dept confirms nothing
        self.assertEqual([d["id"] for d in fin["policyDenies"]], ["g1"])   # ALLOW is no deny
        doc = ra.snapshot_document(rows, basis, "test")
        self.assertEqual([u["id"] for u in doc["users"]], ["u-fin"])
        self.assertEqual(doc["read_at"], "2026-11-13T10:00:00Z")   # the oldest input
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps(doc))
            read = ep.PortalAccessSnapshot(path)(ep.EmployeePrincipal(TENANT, oid(1)))
        self.assertEqual((read.role, read.clearance_departments), ("MANAGER", frozenset({"FIN"})))

    def test_no_snapshot_without_the_portals_denies_and_scopes(self):
        for gone in ((ra.GRANTS,), (ra.SCOPES,), (ra.GRANTS, ra.SCOPES)):
            with self.subTest(gone):
                client = FakeClient(tables(), {r: "relation not granted" for r in gone})
                got, basis = ra.merge(ra.read_live(client), None)
                rows = ra.join_identities(ra.portal_rows(got), identities(), TENANT)
                with self.assertRaises(ra.RolloutError) as refused:
                    ra.snapshot_document(rows, basis, "test")
                self.assertEqual(str(refused.exception), "portal_denies_unavailable")

    def test_served_email_conflicting_with_the_export_is_never_joined(self):
        rows = ra.portal_rows({ra.USERS: [{"id": "u1", "role": "STAFF", "access_level": "L2",
                                           "is_active": True, "email": "A@plx.example"}]})
        joined = ra.join_identities(rows, {"u1": {"email": "b@plx.example",
                                                  "entraObjectId": oid(1)}}, TENANT)
        self.assertEqual(joined[0]["_identity"], "conflict")
        self.assertFalse(ra.resolved(joined[0]))
        looked = ra.join_identities(rows, {}, TENANT, lookup=lambda e: oid(9) if
                                    e == "a@plx.example" else None)
        self.assertEqual((looked[0]["entraObjectId"], looked[0]["_entra"]), (oid(9), "graph_upn"))

    def test_an_uninterpretable_flag_or_missing_users_refuses(self):
        with self.assertRaises(ra.RolloutError):
            ra.portal_rows({ra.USERS: [{"id": "u1", "role": "STAFF", "is_active": "yes"}]})
        with self.assertRaises(ra.RolloutError):
            ra.portal_rows({ra.SCOPES: []})
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "ids.json"
            bad.write_text(json.dumps({"schema": ra.IDENTITY_SCHEMA, "users": [
                {"portalUserId": "u1", "email": "x@plx.example"},
                {"portalUserId": "u1", "email": "y@plx.example"}]}))
            with self.assertRaises(ra.RolloutError):
                ra.load_identities(bad)

    def test_main_refuses_without_a_usable_exclusion_and_drops_an_excluded_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            host = write_host(tmp / "host", policy_doc())
            ids = tmp / "ids.json"
            ids.write_text(json.dumps({"schema": ra.IDENTITY_SCHEMA, "users": [
                {"portalUserId": k, **v} for k, v in identities().items()]}))
            argv = ["snapshot", "--out", str(tmp / "out"), "--identities", str(ids),
                    "--tenant-config", str(host)]
            client = FakeClient(tables(), {})
            err = io.StringIO()
            with redirect_stderr(err):
                code = ra.main(argv, client=client, environ={"PLX_EXCLUSION_ENV_FILE": ""})
            self.assertEqual(code, 2)
            self.assertIn("people_exclusion_unavailable", err.getvalue())
            self.assertFalse((tmp / "out" / "portal_access.json").exists())
            out = io.StringIO()
            with redirect_stdout(out):
                code = ra.main(argv, client=client, environ={
                    "PLX_EXCLUSION_ENV_FILE": "", "PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED})
            self.assertEqual(code, 0)
            written = (tmp / "out" / "portal_access.json").read_text()
            rows_file = (tmp / "out" / "rows.json").read_text()
            for text in (written, rows_file, out.getvalue()):
                self.assertNotIn(EXCLUDED, text)
                self.assertNotIn("excl@", text)
            self.assertEqual(json.loads(out.getvalue())["dropped_by_exclusion"], 1)
            self.assertEqual(os.stat(tmp / "out" / "portal_access.json").st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(tmp / "out").st_mode & 0o777, 0o700)

    def test_output_never_lands_in_the_repository_or_the_served_config(self):
        for path in (ra.ROOT / "scratch-o02", ra.SERVED_CONFIG_ROOT / "workbench-host-vince"):
            with self.assertRaises(ra.RolloutError):
                ra.out_dir(path)


def policy_doc(full_read: list[str] = (OWNER,)) -> dict:
    doc = {"schema": ep.POLICY_SCHEMA, "tenant": "plx", "entra_tenant_id": TENANT,
           "clearance_by_access_level": {"L1": "internal", "L2": "internal",
                                         "L3": "confidential", "L4": "confidential",
                                         "L5": "restricted"},
           "principals": {"staff:finance": {"portal_roles": ["MANAGER", "ADMIN", "SUPER_ADMIN"]},
                          "staff:floor": {"portal_roles": ["STAFF"]}},
           "full_read": [{"object_id": o, "granted_by": "owner",
                          "granted_at": "2026-11-01T00:00:00Z", "reason": "owner",
                          "scopes": ["finance", "mail", "sharepoint", "teams"]}
                         for o in full_read]}
    return doc


def write_host(base: Path, pol: dict, grants: dict | None = None) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "policy.json").write_text(json.dumps(pol))
    (base / "audience.json").write_text(json.dumps({
        "schema": "plx-source-mirror-audience/v1", "tenant": "plx",
        "audiences": {"public.*": ["staff:finance", "staff:floor"]}}))
    (base / "host.json").write_text(json.dumps({
        "employee_policy": "policy.json", "mirror_audience": "audience.json",
        "marking_grants": grants or {}}))
    return base / "host.json"


class PreviewTest(unittest.TestCase):
    def rows(self, n_staff: int = 2):
        rows = ra.join_identities(ra.portal_rows(tables(n_staff)), identities(n_staff), TENANT)
        return ra.drop_excluded(rows, PeopleExclusion([EXCLUDED]))[0]

    def run_preview(self, rows, pol, grants=None, **kw):
        from integrations.source_mirror import AudienceMap
        audience = AudienceMap({"public.*": ["staff:finance", "staff:floor"]}, tenant="plx")
        return ra.preview(rows, policy=pol, tenant_id=TENANT, marking_grants=grants,
                          mirror_audience=audience,
                          relations=["public.app_user", "public.purchase_order"],
                          guard=PeopleExclusion([EXCLUDED]), owners=[OWNER], **kw)

    def test_over_broad_grants_are_flagged_and_the_owner_is_not(self):
        grants = {"finance": ["staff:finance", "staff:floor"], "pii": ["staff:floor"]}
        entries = self.run_preview(self.rows(), policy(full_read=[OWNER, oid(1)]), grants)
        by = {e["email"]: e for e in entries}
        self.assertEqual(set(by), {"owner@plx.example", "fin@plx.example",
                                   "s0@plx.example", "s1@plx.example"})   # active employees only
        owner = by["owner@plx.example"]
        self.assertTrue(owner["unrestricted"] and owner["payroll_hr_documents"])
        self.assertEqual(owner["flags"], [])
        fin = by["fin@plx.example"]
        self.assertIn("full-read grant held by someone other than the two owners", fin["flags"])
        staff = by["s0@plx.example"]
        self.assertTrue(staff["payroll_hr_documents"])
        self.assertIn("sees payroll and HR documents (holds the finance and pii markings)",
                      staff["flags"])
        self.assertIn("holds the finance marking without a Finance or Executive department",
                      staff["flags"])
        self.assertTrue(any(f.startswith("reads the Portal's people/access tables")
                            for f in staff["flags"]))
        self.assertEqual(staff["mail"],
                         "own mailbox only (shared and other people's mailboxes withheld)")
        self.assertEqual(staff["portal_relations"], "2 of 2")
        self.assertEqual(staff["autopilot"], "off (in no autopilot role; opt-in only)")

    def test_unresolved_rows_are_decided_but_only_counted(self):
        rows = ra.join_identities(ra.portal_rows(tables()), {"u-owner": identities()["u-owner"]},
                                  TENANT)
        rows = ra.drop_excluded(rows, PeopleExclusion([EXCLUDED]))[0]
        entries = self.run_preview(rows, policy())
        text = ra.table(entries, "t")
        self.assertIn("owner@plx.example", text)
        self.assertNotIn("u-s0", text)
        agg = ra.summary(entries)["unresolved_by_attributes"]
        self.assertEqual(sum(a["count"] for a in agg), 4)      # excl, fin, s0, s1
        self.assertEqual({a["principals"] for a in agg if a["role"] == "STAFF"}, {"floor"})

    def test_cardinality_no_row_lost_or_duplicated(self):
        for n in (1, 10, 60):
            rows = self.rows(n)
            entries = self.run_preview(rows, policy())
            self.assertEqual(len(entries), n + 2)      # owner, fin and the n staff
            self.assertEqual(len({e["portal_user"] for e in entries}), n + 2)


class RenderTest(unittest.TestCase):
    def test_policy_departments_and_host_people_tables(self):
        example = json.loads((ra.ROOT / "examples" /
                              "employee_permission_policy.example.json").read_text())
        depts = [{"id": f"id-{c}", "code": c, "is_active": True}
                 for c in ("OPS", "PROC", "WH", "QUALITY", "FIN", "SALES")]
        doc = ra.render_policy(example, policy_doc(), depts)
        self.assertEqual(doc["principals"]["staff:finance"]["department_ids"], ["id-FIN"])
        self.assertNotIn("entra_groups", doc["principals"]["staff:quality"])
        self.assertEqual(doc["entra_tenant_id"], TENANT)
        self.assertEqual([g["object_id"] for g in doc["full_read"]], [OWNER])
        with self.assertRaises(ra.RolloutError):
            ra.render_policy(example, policy_doc(), depts[:-1])
        host, audience = ra.render_host(
            {"employee_policy": "p.json", "live_audience": "live.json"}, Path("/srv/host"),
            {"schema": "plx-source-mirror-audience/v1", "tenant": "plx",
             "audiences": {"public.*": ["staff:finance"]}}, [OWNER], ["public.app_user"])
        self.assertEqual(host["live_audience"], "/srv/host/live.json")
        self.assertEqual(host["marking_grants"], ra.ROLLOUT_MARKING_GRANTS)
        self.assertNotIn("pii", host["marking_grants"])
        self.assertEqual(audience["audiences"]["public.app_user"], ["employee:" + OWNER])


if __name__ == "__main__":
    unittest.main()
