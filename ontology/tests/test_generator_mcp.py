"""MCP tool roles (ontology/generator/mcp.py): every plx-mc, plx-brain and vmc-context tool
is bound, action-typed or excluded with a reason; the pin never guesses a schema; action
types never make a write callable without a per-call approval.

Evidence label: isolated-fixture (the committed registry, schema and pin; a temporary git
repository for the TypeScript reader; injected MC transport). No network I/O.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from integrations.live_source_guard import ReadOnlyGuardError
from integrations.plx_hub_guard import MC_APPROVABLE_WRITE_TOOLS, MC_WRITE_ARGUMENTS_NOT_SENT
from integrations.tests.test_mc_write_rules import FakeSender, authorize, client
from ontology.generator import mcp
from scripts import live_read_probes, source_registry as sr


class RolesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = mcp._inputs()

    def test_every_tool_has_exactly_one_role_and_the_committed_outputs_reproduce(self):
        registry, compiled, scope, policy, pin, domain, decision = self.inputs
        self.assertEqual(mcp.check(*self.inputs), [])
        self.assertEqual(len(mcp.tool_entries(registry)), 64)
        # A tool upstream adds has no role: refused until it gets one, never silently kept.
        grown = copy.deepcopy(registry)
        grown["entries"].append({"id": "plx-mc.mcp_tool:mc_new_tool", "system": "plx-mc",
                                 "kind": "mcp_tool", "name": "mc_new_tool",
                                 "evidence": ["PLX_MC:src/x.ts@eeb7db796e8c:1"]})
        problems = mcp.check(grown, compiled, scope, policy, pin, domain, decision)
        self.assertIn("plx-mc.mcp_tool:mc_new_tool: roles [] (exactly one role)", problems)
        self.assertTrue(any(p.startswith("pin is stale") for p in problems))
        # Excluding a bound read gives it two roles; an exclusion needs its reason.
        both = dict(scope, **{"plx-mc.mcp_tool:mc_get_task": mcp.DECISION})
        self.assertTrue(any(p.startswith("plx-mc.mcp_tool:mc_get_task: roles")
                            for p in mcp.check(registry, compiled, both, policy, pin, domain,
                                               decision)))
        self.assertIn("plx-mc.mcp_tool:mc_self_check: excluded without its reason in "
                      + mcp.DECISION, mcp.check(registry, compiled, scope, policy, pin, domain,
                                                decision.replace("`mc_self_check`", "")))
        # A changed upstream input shape changes the generated action type.
        moved = copy.deepcopy(pin)
        moved["servers"]["plx-mc"]["tools"]["mc_request_approval"]["input"]["urgent"] = {
            "type": "boolean", "required": False}
        self.assertTrue(any("action_types differ" in p for p in mcp.check(
            registry, compiled, scope, policy, moved, domain, decision)))

    def test_a_tools_list_serving_mc_upload_evidence_is_pinned_and_never_sent(self):
        """MC e857f8c serves mc_upload_evidence (gap-38ff527f6339): the pin holds its input
        shape, the policy never sends it and the guard refuses it before I/O. A tool MC adds
        that no one classified stays unpinned, so the live check still fails on it."""
        registry, compiled, scope, policy, pin, domain, decision = self.inputs
        # The tools/list entry as MC serves it (evidence-actions.ts:54-64@e857f8c).
        upload = {"name": "mc_upload_evidence", "inputSchema": {"type": "object", "properties": {
            n: {} for n in ("checkoutId", "kind", "fileName", "contentType", "caption",
                            "contentBase64", "size", "sha256", "uploadedName")},
            "required": ["caption", "checkoutId", "contentType", "fileName", "kind"]}}
        diff = live_read_probes.mc_schema_diff([upload])
        self.assertEqual((diff["schema_unpinned"], diff["schema_mismatches"], diff["schema_matched"]),
                         ([], [], 1))
        self.assertIn("mc_upload_evidence", policy["never_sent"])
        self.assertEqual(mcp.roles(registry, compiled, scope)["plx-mc.mcp_tool:mc_upload_evidence"],
                         [("excluded", mcp.DECISION)])
        sender = FakeSender(fn=lambda *a: self.fail("a never-sent tool reached the transport"))
        with self.assertRaises(ValueError):                    # never opted in or approved
            client(sender, write_tools=["mc_upload_evidence"])
        with self.assertRaises(ValueError):
            authorize("mc_upload_evidence", {"checkoutId": "c"})
        mc, _ = client(sender, write_tools=sorted(MC_APPROVABLE_WRITE_TOOLS))
        with self.assertRaises(ReadOnlyGuardError):
            mc.call_tool("mc_upload_evidence", {"checkoutId": "c"})
        self.assertEqual(sender.calls, [])
        new = {"name": "mc_new_tool", "inputSchema": {"type": "object", "properties": {}}}
        self.assertEqual(live_read_probes.mc_schema_diff([upload, new])["schema_unpinned"],
                         ["mc_new_tool"])

    def test_mc_complete_task_pins_the_outcome_input_mc_serves_and_the_door_never_sends_it(self):
        """MC 5bf29d2 TASK-3362 adds the optional ``outcome`` (completed | blocked | failed)
        to mc_complete_task (actions.ts completeTaskInputShape:658-659@5bf29d2): the pin holds
        it, so the tools/list MC now serves matches and the pre-TASK-3362 shape does not.
        The Workbench hands in an action gate, never UAT code work, so it never sends it (MC
        records an omitted outcome as completed); the door refuses it before any I/O."""
        pinned = json.loads(mcp.PIN_PATH.read_text(encoding="utf-8"))["servers"]["plx-mc"]["tools"]
        self.assertEqual(pinned["mc_complete_task"]["input"]["outcome"],
                         {"type": "enum", "required": False})
        required = ["checkoutId", "rollback", "summary", "verificationCommands"]
        served = ("checkoutId", "summary", "commitSha", "prUrl", "outcome",
                  "verificationCommands", "filesChanged", "rollback", "testRun", "shots")
        def listed(names):
            return {"name": "mc_complete_task", "inputSchema": {
                "type": "object", "properties": {n: {} for n in names}, "required": required}}
        diff = live_read_probes.mc_schema_diff([listed(served)])
        self.assertEqual((diff["schema_unpinned"], diff["schema_mismatches"], diff["schema_matched"]),
                         ([], [], 1))
        before = [n for n in served if n != "outcome"]
        self.assertEqual(live_read_probes.mc_schema_diff([listed(before)])["schema_mismatches"],
                         ["mc_complete_task"])
        self.assertIn("outcome", MC_WRITE_ARGUMENTS_NOT_SENT["mc_complete_task"])
        self.assertNotIn("outcome", MC_APPROVABLE_WRITE_TOOLS["mc_complete_task"])
        args = {"checkoutId": "dsp_0123456789abcdef", "summary": "s", "rollback": "r",
                "verificationCommands": ["python3 -m unittest"], "outcome": "blocked"}
        sender = FakeSender(fn=lambda *a: self.fail("a never-sent input reached the transport"))
        mc, _ = client(sender, write_tools=["mc_complete_task"])
        with self.assertRaises((ReadOnlyGuardError, ValueError)):
            mc.call_write_tool("mc_complete_task", args,
                               authorization=authorize("mc_complete_task", args))
        self.assertEqual(sender.calls, [])

    def test_registry_gaps_keep_no_unbound_or_unexcluded_mcp_tool(self):
        registry = self.inputs[0]
        facts = sr.RepoFacts()
        doc = sr.gaps(registry, None, facts)
        rows = [r for r in doc["uncovered"] if r["system"] in mcp.SYSTEMS]
        # What is left waits on the registry, never on a binding: reads stop before
        # evaluated until a live check names them.
        self.assertEqual({(r["system"], r["status"], r["reason"]) for r in rows}, {
            ("plx-mc", "fixture_queryable", "no_live_results"),
            ("plx-brain", "fixture_queryable", "no_live_results")})
        # An action-typed write is terminal bound_action: verified per call by read-back,
        # never captured (gap-109c7c494f8a, DECISIONS/2026-10-08-gen-mc-registry-mcp-coverage).
        self.assertEqual({r["name"] for r in sr.coverage(registry, None, facts)
                          if r["system"] == "plx-mc" and r["status"] == "bound_action"},
                         {"mc_checkout_task", "mc_complete_task", "mc_create_task",
                          "mc_report_progress", "mc_request_approval"})
        by_system = doc["counts"]["by_system"]
        self.assertEqual(by_system["vmc-context"]["out_of_scope"], 11)
        self.assertEqual((by_system["plx-mc"]["out_of_scope"], by_system["plx-brain"]["out_of_scope"]),
                         (32, 8))

    def test_action_typed_tools_are_sent_only_with_an_approval_naming_that_call(self):
        compiled = self.inputs[1]
        tools = {i.split(":", 1)[1] for a in compiled.action_types.values()
                 for i in a.get("registry_entries", ()) if i.startswith("plx-mc.")}
        self.assertEqual(tools, set(MC_APPROVABLE_WRITE_TOOLS))
        sender = FakeSender(fn=lambda *a: self.fail("no I/O without a matching approval"))
        mc, _ = client(sender, write_tools=sorted(tools))
        args = {"taskId": "TASK-9", "reason": "r"}
        for tool in sorted(tools):
            other = next(t for t in sorted(tools) if t != tool)
            with self.assertRaises(ReadOnlyGuardError, msg=tool):
                mc.call_tool(tool, args)                       # the read door
            with self.assertRaises(ReadOnlyGuardError, msg=tool):
                mc.call_write_tool(tool, args, authorization=authorize(other, args))
        self.assertEqual(sender.calls, [])

    def test_approvable_write_arguments_are_the_pinned_tool_inputs(self):
        """The guard's per-tool argument allowlist plus the inputs it never sends is the
        upstream input shape the pin read: an argument MC added or dropped fails here, not
        at the first approved call; a never-sent input is no action parameter."""
        pinned = json.loads(mcp.PIN_PATH.read_text(encoding="utf-8"))["servers"]["plx-mc"]["tools"]
        self.assertEqual({tool: set(args) | MC_WRITE_ARGUMENTS_NOT_SENT.get(tool, set())
                          for tool, args in MC_APPROVABLE_WRITE_TOOLS.items()},
                         {tool: set(pinned.get(tool, {}).get("input", ()))
                          for tool in MC_APPROVABLE_WRITE_TOOLS})
        params = {a["registry_entries"][0].split(":", 1)[1]: set(a["params"])
                  for a in self.inputs[5]["action_types"]}
        for tool, never in MC_WRITE_ARGUMENTS_NOT_SENT.items():
            self.assertFalse(params[tool] & {mcp.snake(n) for n in never}, tool)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _scripts_imports(source: str) -> list[int]:
    """Lines importing ``scripts`` anywhere in ``source``: module level or in a function,
    ``import``/``from`` or ``importlib.import_module``/``__import__`` of a literal name."""
    def names(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Import):
            return [a.name for a in node.names]
        if isinstance(node, ast.ImportFrom):
            return [node.module or ""] if not node.level else []
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str) and (
                    getattr(node.func, "attr", None) == "import_module"
                    or getattr(node.func, "id", None) in ("__import__", "import_module")):
            return [node.args[0].value]
        return []
    return [node.lineno for node in ast.walk(ast.parse(source))
            if any(n == "scripts" or n.startswith("scripts.") for n in names(node))]


class LayeringTests(unittest.TestCase):
    def test_no_non_test_ontology_module_imports_scripts(self):
        """Scripts build on the ontology, never the reverse; ontology tests may import
        scripts."""
        probe = ("import os\ndef f():\n    from scripts.source_registry import is_product\n"
                 "    import scripts.x, json\n    importlib.import_module('scripts.y')\n"
                 "    __import__('scripts')\n    from .scripts import z\n")
        self.assertEqual(_scripts_imports(probe), [3, 4, 5, 6])
        package = Path(mcp.__file__).resolve().parents[1]
        modules = [p for p in sorted(package.rglob("*.py")) if "tests" not in
                   p.relative_to(package).parts]
        self.assertIn(package / "generator" / "mcp.py", modules)
        found = {str(p.relative_to(package.parent)): lines for p in modules
                 if (lines := _scripts_imports(p.read_text(encoding="utf-8")))}
        self.assertEqual(found, {})


class PinReaderTests(unittest.TestCase):
    def test_shapes_are_read_as_written_and_never_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "schemas.ts").write_text(
                'export const nonBlank = z.string().trim().min(1);\n'
                'export const gateSchema = z.object({ taskId: z.string(), '
                'reason: nonBlank.max(500), note: nonBlank.optional() });\n')
            (repo / "src" / "server.ts").write_text(
                'server.tool("t_literal", "Desc, with a comma.", {\n'
                '  // a comment: with a colon\n'
                '  ids: z.array(z.string().optional()).min(1),\n'
                '  n: z.number().int().default(3),\n'
                '  mode: z.enum(["a", "b"]).optional(),\n'
                '}, async (a) => a);\n'
                'server.tool("t_ref", "Gate.", gateSchema.shape, async (a) => a);\n'
                'server.tool("t_none", "No input.", async () => 1);\n'
                'server.tool("t_spread", "Spread.", { ...base, x: z.string() }, async () => 1);\n'
                'server.registerTool("t_out", { description: "d", inputSchema: z.object({ '
                'q: z.string() }).strict(), outputSchema: { r: z.string() } }, async () => 1);\n')
            _git(repo, "init", "-q")
            _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", ".")
            _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x")
            src = mcp._Source(repo, _git(repo, "rev-parse", "HEAD"))
            read = {n: mcp.read_registration(src, "src/server.ts", line, n) for n, line in
                    (("t_literal", 1), ("t_ref", 7), ("t_none", 8), ("t_spread", 9), ("t_out", 10))}
        self.assertEqual(read["t_literal"]["input"], {
            "ids": {"type": "array", "required": True},     # an optional item, not an optional field
            "mode": {"type": "enum", "required": False},
            "n": {"type": "integer", "required": False}})
        self.assertEqual(read["t_ref"]["input"], {          # resolved across files, chains kept
            "note": {"type": "string", "required": False},
            "reason": {"type": "string", "required": True},
            "taskId": {"type": "string", "required": True}})
        self.assertEqual(read["t_none"]["input"], {})
        self.assertIsNone(read["t_spread"]["input"])        # unresolvable: stated, not guessed
        self.assertIn("spreads", read["t_spread"]["input_unresolved"])
        self.assertEqual((read["t_out"]["input"], read["t_out"]["output_schema"]),
                         ({"q": {"type": "string", "required": True}}, "declared"))
        self.assertEqual(read["t_literal"]["output_schema"], "not_declared")

    def test_exported_schema_identifiers_are_exact_including_dollar_signs(self):
        names = ("gateSchema", "$gateSchema", "gateSchema$")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "schemas.ts").write_text("\n".join(
                f"export const {name} = z.object({{ value{i}: z.string() }});"
                for i, name in enumerate(names)))
            (repo / "src" / "decoys.ts").write_text("\n".join(
                f"export const {name}Suffix = z.object({{ decoy: z.number() }});"
                for name in names))
            (repo / "src" / "server.ts").write_text("\n".join(
                f'server.tool("tool{i}", "Schema.", {name}.shape, async (a) => a);'
                for i, name in enumerate(names)))
            _git(repo, "init", "-q")
            _git(repo, "add", ".")
            _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x")
            src = mcp._Source(repo, _git(repo, "rev-parse", "HEAD"))
            for i, name in enumerate(names):
                with self.subTest(identifier=name):
                    read = mcp.read_registration(src, "src/server.ts", i + 1, f"tool{i}")
                    self.assertEqual(read["input"], {
                        f"value{i}": {"type": "string", "required": True}})

    def test_repin_digest_carries_only_an_unmoved_pin_and_fails_closed_otherwise(self):
        """Without an upstream checkout, the pin follows a registry change only when the
        tool entries and upstream commits it was built from are unchanged."""
        def stamped(registry):
            registry["entries_digest"] = sr.entries_digest(registry["entries"])
            return registry

        current = json.loads(mcp.REGISTRY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(mcp._entries_digest(current["entries"]), sr.entries_digest(current["entries"]))
        pin = json.loads(mcp.PIN_PATH.read_text(encoding="utf-8"))
        previous = copy.deepcopy(current)   # the registry the pin was built from ...
        previous["entries"] = [e for e in previous["entries"] if e["kind"] == mcp.KIND] + [
            e for e in previous["entries"] if e["kind"] != mcp.KIND][:3]   # ... other rows moved
        stamped(previous)
        built_from = dict(pin, registry_entries_digest=previous["entries_digest"])
        carried = mcp.carry_pin_digest(built_from, previous, current)
        self.assertEqual(carried, dict(pin, registry_entries_digest=current["entries_digest"]))
        moved_tool = copy.deepcopy(current)
        tool_id = mcp.tool_entries(current)[0]["id"]
        next(e for e in moved_tool["entries"] if e["id"] == tool_id)["evidence"] = [
            "PLX_MC:x@eeb7db796e8c:9"]
        moved_commit = copy.deepcopy(current)
        moved_commit["upstream"]["PLX_MC"]["commit"] = "0" * 40
        forged = dict(copy.deepcopy(previous), entries_digest=current["entries_digest"])
        for label, args, why in (
                ("tool", (built_from, previous, stamped(moved_tool)), "MCP tool entries changed"),
                ("commit", (built_from, previous, moved_commit), "upstream commits changed"),
                ("other source", (pin, previous, current), "not the one the pin was built from"),
                ("forged digest", (dict(pin, registry_entries_digest=current["entries_digest"]),
                                   forged, current), "differ from its entries_digest")):
            with self.subTest(label):
                with self.assertRaises(mcp.McpGenError) as caught:
                    mcp.carry_pin_digest(*args)
                self.assertIn(why, str(caught.exception))

    def test_committed_pin_reproduces_from_the_pinned_upstream_commits(self):
        registry = json.loads(mcp.REGISTRY_PATH.read_text(encoding="utf-8"))
        checkouts = mcp.checkouts_for(set(mcp.SYSTEMS.values()), {})
        if not all(sr.Tree.holds(checkouts[r], registry["upstream"][r]["commit"])
                   for r in checkouts):
            self.skipTest("an upstream checkout does not hold its pinned commit")
        self.assertEqual(mcp.build_pin(registry, checkouts),
                         json.loads(mcp.PIN_PATH.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
