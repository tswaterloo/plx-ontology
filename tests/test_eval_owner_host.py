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
        self.assertEqual((code, errors), (0, ""))
        for value in CLAIMS.values():   # the reader's claims are never printed
            self.assertNotIn(value.lower(), printed.lower())
        config = HostConfig.from_file(self.out / "host.json")   # loads as the host loads it
        self.assertEqual(config.employee_policy, (self.out / "employee_policy.json").resolve())
        doc = json.loads((self.out / "host.json").read_text("utf-8"))
        self.assertEqual(check_marking_grants(doc["marking_grants"]),
                         {"finance": frozenset(tool.OWNER_PRINCIPALS)})
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


if __name__ == "__main__":
    unittest.main()
