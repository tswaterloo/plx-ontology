#!/usr/bin/env python3
"""
Four files are generated from sources and committed (AUD-17):

* ``operator_api/openapi.json``      ``python3 -m operator_api openapi --write`` (the models)
* ``operating/typed_graph/models.py`` and ``operating/app_tables/rows.py``
                                     ``python3 tools/gen_models.py`` (the ontology schema)
* ``app/src/lib/openapi.d.ts``       ``node scripts/gen-api.mjs`` in ``app/`` (openapi.json)

The lander does it:

When the only conflicted paths are generated files,
            the conflict is resolved by regenerating them from the merged sources (never by
            taking a side); a conflict in any other file, or a generator that fails, aborts
            the merge and reports the conflict as before (fail closed).
``regen``   regenerates every generated file in the merged tree and, with ``--commit``,
            commits the ones that changed as "Batch: regenerate API contracts".

The landing's ``openapi --check`` and ``gen-api --check`` stay as the verify after this.
A generator whose source is not in the tree (a test repo, an old tree) is skipped. The
TypeScript file is regenerated only when its inputs (openapi.json, the generator, the app's
package files) or the file itself differ from ``--base`` (main: consistent by the same
check), or always without ``--base``; it needs node and an ``app/node_modules`` (hard-linked
from ``--node-modules`` for the run, removed after). Generators run without credential
variables.

    python3 tools/api_contracts.py merge --tree DIR --sha SHA -m MESSAGE [--base REF] ...
    python3 tools/api_contracts.py regen --tree DIR [--commit] [--base REF] ...

Exit 0: merged / regenerated (one line says what); 1: conflict or generator failure (one
line says which files or which generator); 2: usage.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Sequence

OPENAPI = "operator_api/openapi.json"
TYPED_MODELS = "operating/typed_graph/models.py"
APP_ROWS = "operating/app_tables/rows.py"
OPENAPI_TS = "app/src/lib/openapi.d.ts"
GENERATED = (OPENAPI, TYPED_MODELS, APP_ROWS, OPENAPI_TS)
# what the TypeScript file is a function of (besides itself)
TS_INPUTS = (OPENAPI, "app/scripts/gen-api.mjs", "app/package.json", "app/package-lock.json")
REGEN_MESSAGE = "Batch: regenerate API contracts"
CREDENTIAL = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|DSN|CREDENTIAL")


class RegenError(RuntimeError):
    """A generator could not run or failed: the tree's contracts are not known good."""


@dataclasses.dataclass
class Tools:
    python: str = sys.executable
    node_bin: str | None = None        # a directory put first on PATH for node
    node_modules: str | None = None    # an app/node_modules to link for gen-api.mjs
    timeout: float = 600.0


@dataclasses.dataclass
class Merged:
    ok: bool
    conflicts: list[str] = dataclasses.field(default_factory=list)
    regenerated: list[str] = dataclasses.field(default_factory=list)  # resolved by regenerating
    reason: str = ""

    def line(self) -> str:
        if self.ok:
            return ("merged" if not self.regenerated else
                    f"merged (conflict in generated files resolved by regenerating: "
                    f"{' '.join(self.regenerated)})")
        return f"conflict: {' '.join(self.conflicts[:5]) or 'not clean'}" + \
            (f" ({self.reason})" if self.reason else "")


def _git(tree: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True,
                          timeout=600)
    if check and proc.returncode:
        raise RegenError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[-200:]}")
    return proc


def keyless_env(tools: Tools) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not CREDENTIAL.search(k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if tools.node_bin:
        env["PATH"] = f"{tools.node_bin}:{env.get('PATH', '')}"
    return env


def _run(label: str, cmd: Sequence[str], cwd: Path, tools: Tools) -> None:
    try:
        proc = subprocess.run(list(cmd), cwd=cwd, env=keyless_env(tools), capture_output=True,
                              text=True, timeout=tools.timeout)
    except subprocess.TimeoutExpired:
        raise RegenError(f"{label} timed out after {tools.timeout:.0f} s") from None
    except OSError as error:
        raise RegenError(f"{label} could not start: {type(error).__name__}: {error}") from None
    if proc.returncode:
        raise RegenError(f"{label} failed (exit {proc.returncode}): "
                         f"{(proc.stdout + proc.stderr).strip()[-300:]}")


def _differs_from(tree: Path, base: str | None, paths: Sequence[str]) -> bool:
    """Whether any of ``paths`` in the working tree differs from ``base`` (True when there
    is no base or git cannot say)."""
    if not base:
        return True
    proc = _git(tree, "diff", "--name-only", base, "--", *paths)
    return proc.returncode != 0 or bool(proc.stdout.strip())


def _node_step(tree: Path, tools: Tools) -> None:
    app = tree / "app"
    target = app / "node_modules"
    made = False
    if not target.exists():
        source = Path(tools.node_modules) if tools.node_modules else None
        if source is None or not source.is_dir():
            raise RegenError(f"gen-api.mjs needs app/node_modules (none at {tools.node_modules or 'unset'})")
        # a hard-linked copy, like the landers' contracts check (node resolves an ES module's
        # imports from its real path, so a symlinked node_modules does not resolve)
        _run("cp -al node_modules", ["cp", "-al", str(source.resolve()), str(target.resolve())], app, tools)
        made = True
    try:
        _run("node scripts/gen-api.mjs", ["node", "scripts/gen-api.mjs"], app, tools)
    finally:
        if made:
            shutil.rmtree(target, ignore_errors=True)


def regenerate(tree: Path, tools: Tools, base: str | None = None) -> list[str]:
    """Run every generator the tree has; the generated paths that now differ from HEAD."""
    tree = Path(tree).resolve()
    if (tree / "operator_api" / "__main__.py").is_file():
        _run("python3 -m operator_api openapi --write",
             [tools.python, "-m", "operator_api", "openapi", "--write"], tree, tools)
    if (tree / "tools" / "gen_models.py").is_file():
        _run("python3 tools/gen_models.py", [tools.python, "tools/gen_models.py"], tree, tools)
    if (tree / "app" / "scripts" / "gen-api.mjs").is_file() and \
            _differs_from(tree, base, (*TS_INPUTS, OPENAPI_TS)):
        _node_step(tree, tools)
    proc = _git(tree, "diff", "--name-only", "HEAD", "--", *GENERATED)
    if proc.returncode:
        # mid-merge with unmerged entries HEAD-diff still works; anything else is unknown
        raise RegenError(f"git diff failed: {proc.stderr.strip()[-200:]}")
    return [p for p in proc.stdout.split() if p in GENERATED]


def unmerged(tree: Path) -> list[str]:
    return [p for p in _git(tree, "diff", "--name-only", "--diff-filter=U").stdout.split() if p]


def merge(tree: Path, sha: str, message: str, tools: Tools, base: str | None = None) -> Merged:
    """Merge ``sha`` into ``tree`` (``--no-ff``, ``message``). A conflict only in generated
    files is resolved by regenerating them; any other conflict, or a failing generator,
    aborts the merge."""
    tree = Path(tree)
    done = _git(tree, "merge", "-q", "--no-ff", "-m", message, sha)
    if done.returncode == 0:
        return Merged(True)
    files = unmerged(tree)
    other = [f for f in files if f not in GENERATED]
    if not files or other:
        _git(tree, "merge", "--abort")
        return Merged(False, other + [f for f in files if f in GENERATED] if files else [],
                      reason="" if files else (done.stderr or done.stdout).strip()[-200:])
    try:
        regenerate(tree, tools, base)
        _git(tree, "add", "--", *[p for p in GENERATED if (tree / p).exists() or p in files],
             check=True)
        left = unmerged(tree)
        if left:
            raise RegenError(f"still unmerged after regenerating: {' '.join(left)}")
        _git(tree, "commit", "-q", "--no-verify", "-m",
             f"{message}\n\nGenerated API contracts regenerated from the merged sources "
             f"(the conflict was only in: {' '.join(files)}).", check=True)
    except RegenError as error:
        _git(tree, "merge", "--abort")
        return Merged(False, files, reason=f"regenerating them failed: {error}")
    return Merged(True, regenerated=files)


def commit_regenerated(tree: Path, tools: Tools, base: str | None = None,
                       message: str = REGEN_MESSAGE) -> tuple[str | None, list[str]]:
    """Regenerate; commit the generated files that changed. (new HEAD or None, changed)."""
    tree = Path(tree)
    changed = regenerate(tree, tools, base)
    if not changed:
        return None, []
    _git(tree, "add", "--", *changed, check=True)
    _git(tree, "commit", "-q", "--no-verify", "-m", message, "--", *changed, check=True)
    return _git(tree, "rev-parse", "HEAD", check=True).stdout.strip(), changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("merge", "regen"):
        p = sub.add_parser(name)
        p.add_argument("--tree", type=Path, required=True)
        p.add_argument("--base", help="the ref the tree was built on (main)")
        p.add_argument("--python", default=sys.executable)
        p.add_argument("--node-bin")
        p.add_argument("--node-modules")
        p.add_argument("--timeout", type=float, default=600.0)
        if name == "merge":
            p.add_argument("--sha", required=True)
            p.add_argument("-m", "--message", required=True)
        else:
            p.add_argument("--commit", action="store_true")
            p.add_argument("--message", default=REGEN_MESSAGE)
    args = parser.parse_args(argv)
    tools = Tools(args.python, args.node_bin, args.node_modules, args.timeout)
    if args.cmd == "merge":
        result = merge(args.tree, args.sha, args.message, tools, args.base)
        print(result.line())
        return 0 if result.ok else 1
    try:
        if args.commit:
            sha, changed = commit_regenerated(args.tree, tools, args.base, args.message)
            print(f"committed {sha[:10]}: {' '.join(changed)}" if sha else "unchanged")
        else:
            changed = regenerate(args.tree, tools, args.base)
            print(f"regenerated: {' '.join(changed)}" if changed else "unchanged")
    except RegenError as error:
        print(f"regenerate failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
