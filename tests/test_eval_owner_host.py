"""scripts.eval_owner_host: the case-card scoreboard's host config for the named reader.

The generated directory must load as the product host loads it (host config, employee
policy, Portal access snapshot, audiences), resolve exactly the named reader to the owner
principals, resolve nobody else, keep every holder the committed scope already names, and
never print a claim. Synthetic identities only."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from operating import employee_permissions as ep
from operating.host import HostConfig
from operating.ontology_host import check_marking_grants
from scripts import eval_owner_host as tool

TID = "11111111-2222-4333-8444-555555555555"
OID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
CLAIMS = {"uid": "cmreader0001", "email": "reader@example.com",
          "entraTenantId": TID.upper(), "entraObjectId": OID}
ENV = {"PLX_EVAL_READER": json.dumps(CLAIMS)}


def generate(out: Path, environ=ENV) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = tool.main(["--out", str(out)], environ=environ)
    return code, stdout.getvalue(), stderr.getvalue()


class OwnerEvalHostTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.out = self.tmp / "owner"

    def test_the_generated_host_resolves_the_named_reader_as_owner_and_nobody_else(self):
        code, printed, errors = generate(self.out)
        # No error; without --full-read the host reads the directory, and the missing Graph
        # credentials are said up front (names only).
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(errors)["warning"], "entra_directory_needs_graph_credentials")
        for value in CLAIMS.values():   # the reader's claims are never printed
            self.assertNotIn(value.lower(), printed.lower())
            self.assertNotIn(value.lower(), errors.lower())
        config = HostConfig.from_file(self.out / "host.json")   # loads as the host loads it
        self.assertEqual(config.employee_policy, (self.out / "employee_policy.json").resolve())
        doc = json.loads((self.out / "host.json").read_text("utf-8"))
        # The owner holds every marking the live-ops key grades it as holding: none fewer
        # (a fact the key calls visible would be hidden), none more.
        from evals.live_ops import READERS
        self.assertEqual(check_marking_grants(doc["marking_grants"]),
                         {m: frozenset(tool.OWNER_PRINCIPALS)
                          for m in READERS["vince"]["markings"]})
        policy = ep.EmployeePermissionPolicy.from_file(self.out / "employee_policy.json")
        resolver = ep.EmployeePermissionResolver(
            policy, ep.PortalAccessSnapshot(self.out / "portal_access.json"))
        held = resolver.resolve(ep.EmployeePrincipal(TID, OID))
        self.assertIsNone(held.denied_reason)
        self.assertEqual(held.actor.principals,
                         frozenset({*tool.OWNER_PRINCIPALS, "employee:" + OID}))
        self.assertEqual(held.actor.clearance, "restricted")
        # Another employee of the same tenant, or the reader in another tenant: nothing.
        self.assertEqual(resolver.resolve(ep.EmployeePrincipal(TID, OTHER)).denied_reason,
                         "portal_access_unavailable")
        self.assertEqual(resolver.resolve(ep.EmployeePrincipal(
            "11111111-2222-4333-8444-666666666666", OID)).denied_reason, "tenant_mismatch")

    def test_only_the_owner_principals_are_added_to_the_committed_scope(self):
        self.assertEqual(generate(self.out)[0], 0)
        base = json.loads(tool.BASE_CONFIG.read_text("utf-8"))
        for name, key in (("mirror_audience.json", "mirror_audience"),
                          ("live_audience.json", "live_audience")):
            committed = json.loads((tool.EXAMPLES / base[key]).read_text("utf-8"))
            made = json.loads((self.out / name).read_text("utf-8"))
            for unit, holders in committed["audiences"].items():
                self.assertEqual(set(made["audiences"][unit]) - set(holders),
                                 set(tool.OWNER_PRINCIPALS), unit)
            for unit, holders in made["audiences"].items():
                self.assertTrue(set(holders) <= set(tool.OWNER_PRINCIPALS)
                                | set(committed["audiences"].get(unit, [])), unit)
            self.assertEqual(made["tenant"], committed["tenant"])
        live = json.loads((self.out / "live_audience.json").read_text("utf-8"))
        self.assertEqual(live["classifications"]["mail:*"], "restricted")
        # No wildcard principal, and the source scope (what is captured) is the committed one.
        self.assertNotIn("*", json.dumps([h for hs in live["audiences"].values() for h in hs]))
        made = json.loads((self.out / "host.json").read_text("utf-8"))
        for source in ("portal", "bc", "mail", "sharepoint"):
            self.assertEqual(made[source], base[source])

    def test_a_role_reader_holds_its_principals_and_its_marking_never_finance(self):
        for role, (principals, marking) in tool.ROLES.items():
            out = self.tmp / role
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(tool.main(["--out", str(out), "--role", role],
                                           environ=ENV), 0)
            doc = json.loads((out / "host.json").read_text("utf-8"))
            self.assertEqual(check_marking_grants(doc["marking_grants"]),
                             {marking: frozenset(principals)} if marking else {})
            policy = ep.EmployeePermissionPolicy.from_file(out / "employee_policy.json")
            self.assertNotIn("full_read", json.loads(
                (out / "employee_policy.json").read_text("utf-8")))
            held = ep.EmployeePermissionResolver(
                policy, ep.PortalAccessSnapshot(out / "portal_access.json")).resolve(
                ep.EmployeePrincipal(TID, OID))
            self.assertEqual(held.actor.principals, frozenset({*principals, "employee:" + OID}))
            self.assertEqual(held.portal_role, "SUPER_ADMIN")
        with self.assertRaises(ValueError):
            tool.build(CLAIMS, role="purchasing", full_read=True)

    def test_a_full_read_eval_host_reads_no_directory_and_resolves_the_same_grants(self):
        """A scoreboard host (--full-read) needs no Entra directory read, so it runs without
        Graph credentials; the directory changes none of a generated reader's principals,
        clearance, full-read grant or markings (its policy gates no principal by Entra), so
        switching it off changes nothing the reader may read. A reader without full read
        keeps it (its item ACLs match the groups it reads) and is told what it needs."""
        from operating.agent_session import GRAPH_ENV

        def directory(_principal):
            return ep.EntraDirectoryRead(
                "read", OID, user_principal_name=CLAIMS["email"], mail=CLAIMS["email"],
                department="Quality", group_ids=frozenset({OTHER}), source="graph://users",
                job_title="President", read_at="2026-11-02T00:00:00Z")
        for kwargs in ({"full_read": True}, {}, {"role": "quality"}, {"role": "purchasing"}):
            with self.subTest(**kwargs):
                files = tool.build(CLAIMS, **kwargs)
                self.assertEqual(files["host.json"]["entra_directory"],
                                 not kwargs.get("full_read"))
                out = self.tmp / ("r-" + "-".join(map(str, kwargs.values())))
                tool.write(out, files)
                policy = ep.EmployeePermissionPolicy.from_file(out / "employee_policy.json")
                snapshot = ep.PortalAccessSnapshot(out / "portal_access.json")
                off = ep.EmployeePermissionResolver(policy, snapshot).resolve(
                    ep.EmployeePrincipal(TID, OID))
                on = ep.EmployeePermissionResolver(policy, snapshot, directory=directory
                                                   ).resolve(ep.EmployeePrincipal(TID, OID))
                self.assertIsNone(off.denied_reason)
                self.assertIsNone(on.denied_reason)
                self.assertEqual((off.actor.principals, off.actor.clearance,
                                  off.full_read_code(), off.file_clearance,
                                  off.qc_document_reader),
                                 (on.actor.principals, on.actor.clearance,
                                  on.full_read_code(), on.file_clearance,
                                  on.qc_document_reader))
        with contextlib.redirect_stdout(io.StringIO()) as printed, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(tool.main(["--out", str(self.tmp / "fr"), "--full-read"],
                                       environ=ENV), 0)
        self.assertFalse(json.loads(printed.getvalue())["entra_directory"])
        self.assertEqual(errors.getvalue(), "")
        fake = {**ENV, **{name: "x" for name in GRAPH_ENV}}   # present: nothing to say
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(tool.main(["--out", str(self.tmp / "on")], environ=fake), 0)
        self.assertEqual(errors.getvalue(), "")

    def test_a_department_member_is_routed_by_its_department_never_as_an_admin(self):
        from operating.briefing import Routing
        for role, (principals, marking) in tool.ROLES.items():
            out = self.tmp / f"member-{role}"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tool.main(["--out", str(out), "--role", role,
                                            "--department-member"], environ=ENV), 0)
            doc = json.loads((out / "host.json").read_text("utf-8"))
            self.assertFalse(doc["entra_directory"])
            held = ep.EmployeePermissionResolver(
                ep.EmployeePermissionPolicy.from_file(out / "employee_policy.json"),
                ep.PortalAccessSnapshot(out / "portal_access.json")).resolve(
                ep.EmployeePrincipal(TID, OID))
            self.assertEqual(held.actor.principals, frozenset({*principals, "employee:" + OID}))
            self.assertNotIn("finance", json.dumps(doc["marking_grants"]))
            routing = Routing.of(held)
            self.assertFalse(routing.all_types)
            self.assertEqual(routing.departments, {tool.ROLE_DEPARTMENTS[role]})
        with self.assertRaises(ValueError):
            tool.build(CLAIMS, department_member=True)

    def test_the_owner_reader_is_granted_exactly_the_repositories_the_registry_names(self):
        from integrations.live_source_guard import Actor, LiveSourceAudience

        self.assertEqual(generate(self.out)[0], 0)
        made = json.loads((self.out / "live_audience.json").read_text("utf-8"))
        registry = json.loads(tool.REGISTRY.read_text("utf-8"))
        named = tool.registry_repositories(registry)
        self.assertIn("repo:tswaterloo/OntologyProject", named)   # PG-01's map
        self.assertEqual(sorted(k for k in made["audiences"] if k.startswith("repo:")), named)
        for key in named:
            self.assertEqual(made["audiences"][key], sorted(tool.OWNER_PRINCIPALS))
        audience = LiveSourceAudience.from_json(made)
        owner = Actor(made["tenant"], frozenset(tool.OWNER_PRINCIPALS), "restricted")
        other = Actor(made["tenant"], frozenset({"staff:sales"}), "restricted")
        self.assertTrue(audience.allows(owner, named[0]))
        self.assertFalse(audience.allows(other, named[0]))
        self.assertFalse(audience.allows(owner, "repo:tswaterloo/another-repo"))
        # a malformed reference names no repository
        self.assertEqual(tool.registry_repositories({"rows": [{"source": {
            "repository_ref": "https://github.com/x/y/tree/main"}}]}), [])

    def test_the_files_are_private_to_the_operator(self):
        self.assertEqual(generate(self.out)[0], 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
        for path in self.out.iterdir():
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path.name)

    def test_a_missing_or_invalid_reader_writes_nothing(self):
        for environ in ({}, {"PLX_EVAL_READER": "not json"},
                        {"PLX_EVAL_READER": json.dumps({**CLAIMS, "uid": ""})}):
            code, printed, errors = generate(self.out, environ)
            self.assertEqual((code, printed), (2, ""))
            self.assertEqual(json.loads(errors), {"error": "ValueError"})
            self.assertFalse(self.out.exists())


class LiveHostConfigTest(unittest.TestCase):
    """``from_live``: an eval run reads with exactly the reader's own live host config."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.live = self.tmp / "live"
        with contextlib.redirect_stdout(io.StringIO()):
            tool.main(["--out", str(self.live), "--full-read"], environ=ENV)
        doc = json.loads((self.live / "host.json").read_text("utf-8"))
        owners = list(tool.OWNER_PRINCIPALS)
        doc["marking_grants"] = {m: owners for m in ("finance", "purchasing_prices",
                                                     "quality_controlled", "l5", "pii")}
        doc["entra_directory"] = True   # the live owner host reads its Entra directory
        (self.live / "host.json").write_text(json.dumps(doc), "utf-8")
        self.doc = doc

    def test_the_run_config_is_the_live_one_its_files_read_in_place(self):
        out = self.tmp / "run"
        path = tool.from_live(self.live / "host.json", out, CLAIMS, offline_directory=True)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["host.json"])  # no copies
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        doc = json.loads(path.read_text("utf-8"))
        self.assertEqual(doc["marking_grants"], self.doc["marking_grants"])   # every grant
        self.assertFalse(doc["entra_directory"])
        config, live = HostConfig.from_file(path), HostConfig.from_file(self.live / "host.json")
        for name in ("sources", "marking_grants", "tenant", *tool_paths()):  # as the live host
            self.assertEqual(getattr(config, name, None), getattr(live, name, None), name)
        self.assertEqual(config.employee_policy, (self.live / "employee_policy.json").resolve())
        self.assertEqual(set(config.marking_grants), set(self.doc["marking_grants"]))
        kept = tool.from_live(self.live / "host.json", self.tmp / "run2", CLAIMS)
        self.assertTrue(json.loads(kept.read_text("utf-8"))["entra_directory"])

    def test_an_offline_run_is_refused_when_the_directory_gates_a_principal(self):
        """Switching the directory off would drop a principal gated by an Entra group:
        the run would not read with the reader's own grants, so it is refused."""
        policy_path = self.live / "employee_policy.json"
        policy = json.loads(policy_path.read_text("utf-8"))
        policy["principals"]["staff:quality"]["entra_groups"] = [OTHER]
        policy_path.write_text(json.dumps(policy), "utf-8")
        with self.assertRaises(ValueError) as caught:
            tool.from_live(self.live / "host.json", self.tmp / "gated", CLAIMS,
                           offline_directory=True)
        self.assertEqual(str(caught.exception), "offline_directory_changes_principals")
        self.assertFalse((self.tmp / "gated").exists())
        kept = tool.from_live(self.live / "host.json", self.tmp / "kept", CLAIMS)
        self.assertTrue(json.loads(kept.read_text("utf-8"))["entra_directory"])

    def test_another_reader_or_a_broken_config_is_refused_and_nothing_written(self):
        for claims, config, code in (
                ({**CLAIMS, "entraObjectId": OTHER}, self.live / "host.json",
                 "reader_not_the_live_host_config_reader"),
                ({**CLAIMS, "entraTenantId": OTHER}, self.live / "host.json",
                 "reader_not_the_live_host_config_reader"),
                (CLAIMS, self.tmp / "absent.json", "live_host_config_unreadable")):
            with self.subTest(code=code), self.assertRaises(ValueError) as caught:
                tool.from_live(config, self.tmp / "refused", claims)
            self.assertEqual(str(caught.exception), code)
            self.assertFalse((self.tmp / "refused").exists())


def tool_paths():
    from operating.host import _PATHS
    return _PATHS


if __name__ == "__main__":
    unittest.main()
