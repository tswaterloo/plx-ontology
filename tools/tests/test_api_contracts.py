"""tools/api_contracts.py: generated API contracts regenerated at landing (AUD-17).

Fixture: a temporary git repo whose generators are stand-ins with the real entry points
(``python3 -m operator_api openapi --write``, ``python3 tools/gen_models.py``, ``node
scripts/gen-api.mjs`` through a fake ``node`` on PATH), so the merge and conflict logic is
tested without the app: a conflict only in generated files is resolved by regenerating, any
other conflict or a failing generator aborts the merge, and the regenerate commit holds only
generated files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools import api_contracts as ac

# openapi.json is the sorted lines of operator_api/spec.txt; the stand-in also records
# whether it saw a credential variable
OPENAPI_MAIN = '''import json, os, sys
from pathlib import Path
here = Path(__file__).parent
if os.environ.get("STANDIN_FAIL"):
    sys.exit("generator broke")
lines = sorted(l for l in (here / "spec.txt").read_text().splitlines() if l)
creds = sorted(k for k in os.environ if "SECRET" in k or "TOKEN" in k)
(here / "openapi.json").write_text(json.dumps({"paths": lines}, indent=1) + "\\n")
(here.parent / "creds-seen.txt").write_text(" ".join(creds))
'''
GEN_MODELS = '''from pathlib import Path
root = Path(__file__).resolve().parents[1]
names = (root / "schema.txt").read_text().split()
for rel in ("operating/typed_graph/models.py", "operating/app_tables/rows.py"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"class {n}: pass\\n" for n in sorted(names)))
'''
# a fake node: "node scripts/gen-api.mjs" in app/ writes src/lib/openapi.d.ts from
# ../operator_api/openapi.json, only when app/node_modules resolves; logs each run
FAKE_NODE = '''#!/bin/sh
echo "run $*" >> "${NODE_LOG:-/dev/null}"
[ -d node_modules ] || { echo "no node_modules" >&2; exit 1; }
want=$(mktemp); { echo "// GENERATED"; cat ../operator_api/openapi.json; } > "$want"
case " $* " in
  *" --check "*) cmp -s "$want" src/lib/openapi.d.ts; rc=$?; rm -f "$want"; exit $rc ;;
esac
mkdir -p src/lib; mv "$want" src/lib/openapi.d.ts
'''


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


class ApiContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Lander")
        git(self.repo, "config", "user.email", "lander@example.invalid")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "root")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "node").write_text(FAKE_NODE)
        (bin_dir / "node").chmod(0o755)
        self.modules = self.tmp / "node_modules"
        self.modules.mkdir()
        self.node_log = self.tmp / "node.log"
        os.environ["NODE_LOG"] = str(self.node_log)
        self.addCleanup(os.environ.pop, "NODE_LOG", None)
        self.tools = ac.Tools(python=sys.executable, node_bin=str(bin_dir),
                              node_modules=str(self.modules), timeout=120)
        self.write({"operator_api/__init__.py": "", "operator_api/__main__.py": OPENAPI_MAIN,
                    "operator_api/spec.txt": "a\n1\n2\n3\n4\nz\n", "tools/gen_models.py": GEN_MODELS,
                    "schema.txt": "Order\n", "app/scripts/gen-api.mjs": "// stand-in\n",
                    "app/package.json": "{}\n", "app/.gitignore": "node_modules/\n",
                    ".gitignore": "creds-seen.txt\n", "notes.txt": "base\n"})
        self.regen_and_commit("base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.node_log.unlink(missing_ok=True)

    def write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def regen_and_commit(self, message: str) -> str:
        ac.regenerate(self.repo, self.tools)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def lane(self, name: str, files: dict[str, str], regenerate: bool = True) -> str:
        git(self.repo, "checkout", "-q", "-B", name, self.base)
        self.write(files)
        if regenerate:
            sha = self.regen_and_commit(name)
        else:
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", name)
            sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "main")
        return sha

    def spec(self, *inserted: tuple[int, str]) -> str:
        lines = ["a", "1", "2", "3", "4", "z"]
        for at, text in sorted(inserted, reverse=True):
            lines.insert(at, text)
        return "\n".join(lines) + "\n"

    def openapi(self) -> list[str]:
        return json.loads((self.repo / ac.OPENAPI).read_text())["paths"]

    def assert_clean(self) -> None:
        self.assertEqual(git(self.repo, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

    def test_a_conflict_only_in_generated_files_is_resolved_by_regenerating(self) -> None:
        one = self.lane("lane/one", {"operator_api/spec.txt": self.spec((1, "b"))})
        two = self.lane("lane/two", {"operator_api/spec.txt": self.spec((5, "y"))})
        self.assertTrue(ac.merge(self.repo, one, "Batch: merge one", self.tools, self.base).ok)
        result = ac.merge(self.repo, two, "Batch: merge two", self.tools, self.base)
        self.assertTrue(result.ok, result.line())
        self.assertEqual(sorted(result.regenerated), sorted([ac.OPENAPI, ac.OPENAPI_TS]))
        self.assertEqual(self.openapi(), sorted(["a", "1", "2", "3", "4", "z", "b", "y"]))
        ts = (self.repo / ac.OPENAPI_TS).read_text()
        self.assertIn('"b"', ts)
        self.assertIn('"y"', ts)
        self.assertNotIn("<<<<<<<", (self.repo / ac.OPENAPI).read_text() + ts)
        self.assertEqual(len(git(self.repo, "log", "-1", "--format=%P").split()), 2)
        self.assertIn("Batch: merge two", git(self.repo, "log", "-1", "--format=%B"))
        self.assert_clean()
        self.assertFalse((self.repo / "app" / "node_modules").exists(), "the link is removed")
        self.assertEqual(ac.regenerate(self.repo, self.tools, self.base), [], "already current")

    def test_a_conflict_in_any_other_file_aborts_the_merge_and_names_it(self) -> None:
        one = self.lane("lane/one", {"operator_api/spec.txt": self.spec((1, "b")), "notes.txt": "one\n"})
        two = self.lane("lane/two", {"operator_api/spec.txt": self.spec((5, "y")), "notes.txt": "two\n"})
        ac.merge(self.repo, one, "m1", self.tools, self.base)
        head = git(self.repo, "rev-parse", "HEAD")
        result = ac.merge(self.repo, two, "m2", self.tools, self.base)
        self.assertFalse(result.ok)
        self.assertIn("notes.txt", result.conflicts)
        self.assertTrue(result.line().startswith("conflict: notes.txt"), result.line())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assert_clean()

    def test_a_failing_generator_aborts_the_merge_fail_closed(self) -> None:
        one = self.lane("lane/one", {"operator_api/spec.txt": self.spec((1, "b"))})
        two = self.lane("lane/two", {"operator_api/spec.txt": self.spec((5, "y"))})
        ac.merge(self.repo, one, "m1", self.tools, self.base)
        head = git(self.repo, "rev-parse", "HEAD")
        os.environ["STANDIN_FAIL"] = "1"
        try:
            result = ac.merge(self.repo, two, "m2", self.tools, self.base)
        finally:
            os.environ.pop("STANDIN_FAIL")
        self.assertFalse(result.ok)
        self.assertEqual(sorted(result.conflicts), sorted([ac.OPENAPI, ac.OPENAPI_TS]))
        self.assertIn("regenerating them failed", result.reason)
        self.assertIn("generator broke", result.reason)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assert_clean()

    def test_an_unknown_commit_is_not_clean_and_leaves_the_tree(self) -> None:
        result = ac.merge(self.repo, "0" * 40, "m", self.tools, self.base)
        self.assertFalse(result.ok)
        self.assertEqual(result.conflicts, [])
        self.assert_clean()

    def test_a_lane_that_did_not_regenerate_gets_one_contracts_commit(self) -> None:
        sha = self.lane("lane/x", {"operator_api/spec.txt": self.spec((2, "q")),
                                   "schema.txt": "Order\nInvoice\n"}, regenerate=False)
        self.assertTrue(ac.merge(self.repo, sha, "Batch: merge x", self.tools, self.base).ok)
        os.environ["PLX_TEST_SECRET"] = "not-for-generators"
        try:
            new, changed = ac.commit_regenerated(self.repo, self.tools, self.base)
        finally:
            os.environ.pop("PLX_TEST_SECRET")
        self.assertIsNotNone(new)
        self.assertEqual(sorted(changed), sorted(ac.GENERATED))
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), ac.REGEN_MESSAGE)
        self.assertEqual(sorted(git(self.repo, "show", "--name-only", "--format=", "HEAD").split()),
                         sorted(ac.GENERATED), "the commit holds only generated files")
        self.assertIn("q", self.openapi())
        self.assertIn("class Invoice", (self.repo / ac.TYPED_MODELS).read_text())
        self.assertEqual((self.repo / "creds-seen.txt").read_text(), "", "generators run keyless")
        self.assert_clean()
        self.assertEqual(ac.commit_regenerated(self.repo, self.tools, self.base), (None, []))

    def test_the_typescript_step_runs_only_when_its_inputs_moved_and_needs_node_modules(self) -> None:
        sha = self.lane("lane/x", {"schema.txt": "Order\nShipment\n"}, regenerate=False)
        ac.merge(self.repo, sha, "m", self.tools, self.base)
        ac.commit_regenerated(self.repo, self.tools, self.base)
        self.assertFalse(self.node_log.exists(), "openapi.json did not move: node not run")
        sha = self.lane("lane/y", {"operator_api/spec.txt": self.spec((3, "w"))}, regenerate=False)
        ac.merge(self.repo, sha, "m", self.tools, self.base)
        broken = ac.Tools(self.tools.python, self.tools.node_bin, str(self.tmp / "missing"), 120)
        with self.assertRaises(ac.RegenError):
            ac.commit_regenerated(self.repo, broken, self.base)
        git(self.repo, "checkout", "-q", "--", ".")
        new, changed = ac.commit_regenerated(self.repo, self.tools, self.base)
        self.assertIn(ac.OPENAPI_TS, changed)
        self.assertTrue(self.node_log.exists())

    def test_the_command_line_says_one_line_and_exits_by_outcome(self) -> None:
        one = self.lane("lane/one", {"operator_api/spec.txt": self.spec((1, "b"))})
        args = ["--tree", str(self.repo), "--base", self.base, "--node-bin", self.tools.node_bin,
                "--node-modules", str(self.modules)]
        proc = subprocess.run([sys.executable, str(Path(ac.__file__)), "merge", *args, "--sha", one,
                               "-m", "Batch: merge one"], capture_output=True, text=True,
                              env={**os.environ, "NODE_LOG": str(self.node_log)})
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "merged"), proc.stderr)
        proc = subprocess.run([sys.executable, str(Path(ac.__file__)), "regen", *args, "--commit"],
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "unchanged"), proc.stderr)


if __name__ == "__main__":
    unittest.main()
