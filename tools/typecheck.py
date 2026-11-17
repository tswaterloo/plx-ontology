#!/usr/bin/env python3
"""Type check operating/, ontology/ and operator_api/ against a baseline.

Runs mypy with ``mypy.ini`` and compares its errors to ``tools/typecheck-baseline.txt``,
one error per line as ``path: [code] message`` (no line number, so moving code does not
churn the baseline; a repeated error is listed as often as it occurs). An error not in the
baseline is new and fails (exit 1); a baseline line no longer reported is fixed and is
listed so its line can be deleted. ``--update`` rewrites the baseline from this run.

mypy is a development tool (``requirements-dev.txt``), not a runtime dependency: it is
taken from ``PLX_MYPY`` (a mypy executable), else ``python3 -m mypy`` when importable.
Without it the check reports ``unavailable`` and exits 2, never a pass. ``main_ci``
calls this as a report that does not fail the suite.
"""
from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "tools/typecheck-baseline.txt"
CONFIG = "mypy.ini"
TIMEOUT_SECONDS = 1200
ERROR = re.compile(r"^(?P<path>[^:\s]+\.pyi?):\d+(?::\d+)?: error: (?P<msg>.*?)"
                   r"(?:\s+\[(?P<code>[a-z0-9-]+)\])?$")


def normalise(output: str) -> Counter[str]:
    """mypy output -> multiset of ``path: [code] message`` (notes and summaries dropped)."""
    found: Counter[str] = Counter()
    for line in output.splitlines():
        match = ERROR.match(line.strip())
        if match:
            found[f"{match['path']}: [{match['code'] or 'misc'}] {match['msg']}"] += 1
    return found


def read_baseline(path: Path) -> Counter[str]:
    if not path.is_file():
        return Counter()
    return Counter(line for line in path.read_text("utf-8").splitlines()
                   if line and not line.startswith("#"))


def mypy_command() -> list[str] | None:
    explicit = os.environ.get("PLX_MYPY")
    if explicit:
        return [explicit]
    if importlib.util.find_spec("mypy") is not None:
        return [sys.executable, "-m", "mypy"]
    return None


def compare(found: Counter[str], baseline: Counter[str]) -> tuple[list[str], list[str]]:
    """(new errors, fixed baseline lines), each listed once per surplus occurrence."""
    new = sorted((found - baseline).elements())
    fixed = sorted((baseline - found).elements())
    return new, fixed


def run(tree: Path = ROOT, *, update: bool = False) -> tuple[int, list[str]]:
    command = mypy_command()
    if command is None:
        return 2, ["typecheck: unavailable (mypy not installed; see requirements-dev.txt "
                   "or set PLX_MYPY)"]
    try:
        proc = subprocess.run([*command, "--config-file", CONFIG], cwd=tree, text=True,
                              capture_output=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return 2, [f"typecheck: timed out after {TIMEOUT_SECONDS} s (not a pass)"]
    if proc.returncode not in (0, 1):  # 2: mypy itself failed (config, crash)
        return 2, ["typecheck: mypy failed", (proc.stdout + proc.stderr)[-1500:]]
    found = normalise(proc.stdout)
    baseline_path = tree / BASELINE
    if update:
        baseline_path.write_text(
            "# mypy errors accepted on the baseline (tools/typecheck.py); delete fixed lines.\n"
            + "".join(f"{line}\n" for line in sorted(found.elements())), "utf-8")
        return 0, [f"typecheck: baseline written, {sum(found.values())} errors"]
    new, fixed = compare(found, read_baseline(baseline_path))
    lines = [f"typecheck: {sum(found.values())} errors, {len(new)} new, {len(fixed)} fixed "
             f"(baseline {BASELINE})"]
    lines += [f"  new: {line}" for line in new[:50]]
    if len(new) > 50:
        lines.append(f"  ... {len(new) - 50} more new")
    lines += [f"  fixed (delete from the baseline): {line}" for line in fixed[:20]]
    return (1 if new else 0), lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--update", action="store_true", help="rewrite the baseline")
    args = parser.parse_args(argv)
    code, lines = run(Path(args.repo), update=args.update)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
