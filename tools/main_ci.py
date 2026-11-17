#!/usr/bin/env python3
"""Run the whole test suite, keyless and in parallel: the full check of ``main``.

Every tracked ``test_*.py`` module (``git ls-files``, so a new test directory is
picked up without editing anything) runs as ``python3 -m unittest <module>`` in its own
process, ``--workers`` at a time (default: the runner's core count, at least 2), with the
business credential variables (``KEYLESS``) and the host's store mode (``HOST_ONLY``)
removed. This is the only place the whole suite runs.

A module listed under "Currently red" in ``tools/KNOWN-RED.md`` is reported but does
not fail the run; a listed module that passes is reported as fixed (delete its row).
A module listed under "Quarantined" in ``tools/FLAKY.md`` (load-sensitive) that fails is
re-run alone once after the others; passing alone it is reported as flaky. Any other
failure fails the run (exit 1) with the tail of its output. ``tools/land.sh`` runs its
landing tests through here (``--modules``), so both apply the same rules.

The run also does the daily allowlist audit, ``EXPIRY_AUDIT``: the gate enforces an
expired no-hardcoded-cases allowlist entry only on a candidate that touches its file, so
an expiry in an untouched file would otherwise never turn anything red. An expired entry
fails this run (on main, never a candidate's gate).

The suite runs on this host: ``deploy/ops/suite_nightly.sh`` (user timer
``deploy/user/plx-suite.timer``) calls it on a detached ``origin/main`` worktree. The GitHub
workflow that used to call it is gone (the repository's Actions allowance is spent, owner
2026-11-05).
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Mapping

KNOWN_RED = "tools/KNOWN-RED.md"
FLAKY = "tools/FLAKY.md"
MODULE_TIMEOUT_SECONDS = 900  # one stuck module must not hold the whole run
# A module that only timed out in the parallel pass runs once more, a quarter of the workers
# at a time (the suite's unit stays inside its start timeout), with this budget:
# on a loaded box (each passes in 260-830 s on a quiet run and on the Sparks) the wrapped run
# waits for its slot and runs at the lowest priority, so a slow module there says nothing
# of the code. Passing on the rerun is a pass; a second timeout or a failure is still red.
TIMED_OUT_RERUN_SECONDS = 1800
TIMED_OUT = "timed out after "
EXPIRY_AUDIT = ("scripts/check_no_hardcoded_cases.py", "--strict-expiry", "--base", "HEAD")
# Business credential names: tests never see them. A copy of
# twin/credential_names.KEYLESS (test_main_ci holds them equal).
KEYLESS = re.compile(r"^(AWS_.*|AZURE_.*|GOOGLE_.*|CLOUDSDK_.*|GH_.*|GITHUB_TOKEN|ANTHROPIC_.*"
                     r"|CLAUDE_CODE_OAUTH.*|CLAUDE_ROUTINE_FIRE_.*|MICROSOFT_GRAPH_.*|BC_.*|VMC_.*"
                     r"|MC_.*|BRAIN_.*|PLX_WORKBENCH_.*|GIT_CONFIG_COUNT|GIT_CONFIG_VALUE_.*"
                     r"|.*(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|COOKIE).*"
                     r"|(.*_)?(DATABASE|POSTGRES(QL)?|PG|DB|DIRECT)(_\w+)?_URL(_\w+)?|PGURL|PGPASSFILE"
                     r")$", re.IGNORECASE)


# The host's store mode (deploy.env, sourced by deploy/lib.sh into every ops script, the
# nightly suite's included: PLX_ONTOLOGY_PG=only since 11-13) is the served host's, never a
# test's: a test that wants Postgres sets it itself (operating/tests/pg_store_mode.py).
HOST_ONLY = ("PLX_ONTOLOGY_PG", "PLX_ONTOLOGY_PG_PARTIAL")


TEST_MARK = re.compile(r"\bunittest\b|\bTestCase\b|^\s*def test", re.MULTILINE)


def discover(tree: str) -> list[str]:
    """Dotted names of every tracked test module. A ``test_*.py`` file that defines and
    imports no test (``operating/test_records.py``: the test-record *rule*) is product code
    whose name starts with ``test_``, not a module that runs zero tests and goes red."""
    listed = subprocess.run(["git", "ls-files", "-z", "--", "*.py"], cwd=tree, text=True,
                            capture_output=True, check=True).stdout

    def is_test(path: str) -> bool:
        try:
            return bool(TEST_MARK.search(Path(tree, path).read_text(errors="replace")))
        except OSError:
            return True  # unreadable: let unittest say so rather than skip it silently
    return sorted(path[:-3].replace("/", ".") for path in listed.split("\0")
                  if path.rpartition("/")[2].startswith("test_") and is_test(path))


def known_red(text: str | None, heading: str = r"\bcurrently\s+red\b") -> set[str]:
    """Test modules listed under a "Currently red" heading of KNOWN-RED.md (or another
    ``heading`` pattern), until the next heading of the same or a higher level: dotted
    names or test file paths, in backticks, list items, table cells or prose. A class or
    test inside a module names its module; a bare test name names none. Other sections
    (fixed, triaged) never count."""
    modules: set[str] = set()
    level, heading_pattern = None, heading
    for line in (text or "").splitlines():
        heading = re.match(r"\s{0,3}(#+)\s+(.*)", line)
        if heading:
            depth = len(heading.group(1))
            if level is not None and depth <= level:
                level = None
            if re.search(heading_pattern, heading.group(2), re.IGNORECASE):
                level = depth
            continue
        if level is None:
            continue
        for token in re.findall(r"[\w./]+", line):
            token = token.strip("./")
            parts = (token[:-3] if token.endswith(".py") else token).replace("/", ".").split(".")
            index = next((i for i, part in enumerate(parts) if part.startswith("test_")), None)
            # index 0: a bare test name (a method named in a note) names no module; every
            # test module lives in a package (tests.test_x, operating/tests/test_y.py)
            if index and all(part.isidentifier() for part in parts[:index + 1]):
                modules.add(".".join(parts[:index + 1]))
    return modules


def mem_available_gb(meminfo: Path = Path("/proc/meminfo")) -> float | None:
    """MemAvailable in GB, or None when unreadable (then nothing waits on it)."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def order_by_timings(modules: list[str], timings: Mapping[str, float]) -> list[str]:
    """Longest first (last run's seconds; an unknown module counts as slow, 60 s), so the
    slow tail starts early and the shards finish together."""
    return sorted(modules, key=lambda m: (-float(timings.get(m, 60.0)), m))


def run_modules(tree: str, modules: list[str], env: Mapping[str, str],
                workers: int, *, prefix: list[str] | None = None, min_free_gb: float = 0.0,
                timeout: float | None = None,
                meminfo: Path = Path("/proc/meminfo")) -> list[tuple[str, bool, str, float]]:
    """`python3 -m unittest <module>`, one process per module, `workers` at a time:
    (module, passed, output tail, seconds) in the order given. `prefix` wraps each
    command (``tools/testslot.py`` shares the machine's test slots); with `min_free_gb`
    a module starts only while that much memory is available (the host keeps headroom
    for its services), waiting up to the module timeout."""
    limit = MODULE_TIMEOUT_SECONDS if timeout is None else timeout
    gate = threading.Lock()

    def one(module: str) -> tuple[str, bool, str, float]:
        if min_free_gb:
            with gate:  # one starter at a time re-reads the memory it leaves to the next
                waited = time.monotonic()
                while time.monotonic() - waited < limit:
                    free = mem_available_gb(meminfo)
                    if free is None or free >= min_free_gb:
                        break
                    time.sleep(2)
        started = time.monotonic()
        try:
            proc = subprocess.run([*(prefix or []), "python3", "-m", "unittest", module],
                                  cwd=tree, text=True, capture_output=True, timeout=limit,
                                  env=env)
            passed, tail = proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]
        except subprocess.TimeoutExpired:
            passed, tail = False, f"timed out after {limit:g} s"
        return module, passed, tail, round(time.monotonic() - started, 1)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, modules))


def run(tree: str, workers: int, *, modules: list[str] | None = None,
        prefix: list[str] | None = None, min_free_gb: float = 0.0,
        timings_path: str | None = None, report_path: str | None = None,
        audit: bool = True) -> tuple[int, list[str]]:
    """Run every module (or `modules`); returns (exit code, report lines). With
    `timings_path` the slowest modules start first and the file is updated with this run's
    seconds; with `report_path` every result is written there as JSON (the nightly red
    list)."""
    started = time.monotonic()
    listed_in = Path(tree, KNOWN_RED)
    listed = known_red(listed_in.read_text() if listed_in.is_file() else None)
    env = {name: value for name, value in os.environ.items()
           if not KEYLESS.match(name) and name not in HOST_ONLY}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    chosen = discover(tree) if modules is None else modules
    timings: dict[str, float] = {}
    if timings_path:
        try:
            timings = json.loads(Path(timings_path).read_text())
        except (OSError, ValueError):
            timings = {}
        chosen = order_by_timings(chosen, timings)
    runs = run_modules(tree, chosen, env, workers, prefix=prefix, min_free_gb=min_free_gb)
    flaky_in = Path(tree, FLAKY)
    quarantined = known_red(flaky_in.read_text() if flaky_in.is_file() else None, r"\bquarantined\b")
    retry = [module for module, passed, _t, _s in runs if not passed and module in quarantined]
    alone = {module: result for module, *result in
             run_modules(tree, retry, env, 1, prefix=prefix, min_free_gb=min_free_gb)}
    flaky = {module for module, (passed, _t, _s) in alone.items() if passed}
    runs = [(m, True, t, s) if m in flaky else (m, p, alone[m][1], s) if m in alone else (m, p, t, s)
            for m, p, t, s in runs]
    slow = [module for module, passed, tail, _s in runs
            if not passed and tail.startswith(TIMED_OUT)]
    again = {module: result for module, *result in
             run_modules(tree, slow, env, max(1, workers // 4), prefix=prefix,
                         min_free_gb=min_free_gb, timeout=TIMED_OUT_RERUN_SECONDS)}
    slow_ok = {module for module, (passed, _t, _s) in again.items() if passed}
    runs = [(m, again[m][0], again[m][1], again[m][2]) if m in again else (m, p, t, s)
            for m, p, t, s in runs]
    runs.sort(key=lambda item: item[0])
    if timings_path:
        timings.update({module: seconds for module, _p, _t, seconds in runs})
        Path(timings_path).parent.mkdir(parents=True, exist_ok=True)
        Path(timings_path).write_text(json.dumps(timings, indent=0, sort_keys=True))
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({
            "tree": tree, "workers": workers, "seconds": round(time.monotonic() - started),
            "head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree, text=True,
                                   capture_output=True).stdout.strip(),
            "results": [{"module": m, "passed": p, "known_red": m in listed, "seconds": sec,
                         "tail": "" if p else t} for m, p, t, sec in runs]}, indent=1))
    try:
        audit = subprocess.run(["python3", *EXPIRY_AUDIT], cwd=tree, text=True,
                               capture_output=True, timeout=MODULE_TIMEOUT_SECONDS, env=env) \
            if audit else None
    except subprocess.TimeoutExpired:
        audit = subprocess.CompletedProcess(EXPIRY_AUDIT, 1, "", "timed out")
    lines, blocking = [], []
    for module, passed, tail, seconds in runs:
        if passed:
            state = "fixed (delete its KNOWN-RED row)" if module in listed else \
                "flaky, passed alone" if module in flaky else \
                "timed out under load, passed on rerun" if module in slow_ok else "pass"
            lines.append(f"{state} {seconds:6.1f}s {module}")
        elif module in listed:
            lines.append(f"known red {seconds:6.1f}s {module}")
        else:
            lines.append(f"FAIL {seconds:6.1f}s {module}")
            blocking.append((module, tail))
    for module, tail in blocking:
        lines += [f"::error::{module} failed", tail]
    failed = sum(1 for _module, passed, _tail, _seconds in runs if not passed)
    lines.append(f"{len(runs)} modules: {len(runs) - failed} pass, {failed - len(blocking)} known "
                 f"red, {len(blocking)} FAIL; {time.monotonic() - started:.0f} s with "
                 f"{workers} workers" + (f"; {len(flaky)} flaky, passed alone" if flaky else ""))
    if audit is None:
        return (1 if blocking else 0), lines
    if audit.returncode == 0:
        lines.append("allowlist audit: pass")
    else:
        lines += ["::error::allowlist audit failed (expired or violating no-hardcoded-cases "
                  f"entries): {' '.join(EXPIRY_AUDIT)}", (audit.stdout + audit.stderr)[-1500:]]
    return (1 if blocking or audit.returncode else 0), lines


def typecheck_report(tree: str) -> list[str]:
    """tools/typecheck.py's lines, prefixed as a report: new type errors are listed but the
    suite's exit code is the tests' alone."""
    try:
        proc = subprocess.run(["python3", "tools/typecheck.py", "--repo", tree], cwd=tree,
                              text=True, capture_output=True, timeout=1500)
        out, code = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        out, code = "typecheck: timed out (not a pass)", 2
    status = {0: "pass", 1: "new errors"}.get(code, "not run")
    return [f"typecheck report (does not fail the run): {status}",
            *out.strip().splitlines()[:60]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="checkout to test (default: cwd)")
    parser.add_argument("--workers", type=int, default=max(2, os.cpu_count() or 1))
    parser.add_argument("--slots", action="store_true",
                        help="wrap each module in tools/testslot.py (shares the host's "
                             "test slots with builders and gates)")
    parser.add_argument("--min-free-gb", type=float, default=0.0,
                        help="start a module only while this much memory is available")
    parser.add_argument("--timings", help="JSON of module seconds: slowest first, updated")
    parser.add_argument("--report", help="write every result as JSON here")
    parser.add_argument("--affected", metavar="BASE",
                        help="only the modules tools/affected_tests.py selects against BASE")
    parser.add_argument("--modules", nargs="+", metavar="MODULE",
                        help="only these modules (a name starting with @ reads one per line from that file)")
    parser.add_argument("--no-audit", action="store_true", help="skip the allowlist audit (a landing's tests)")
    parser.add_argument("--typecheck", action="store_true",
                        help="also report tools/typecheck.py (mypy against its baseline); "
                             "a report only, never fails the run")
    args = parser.parse_args(argv)
    tree = os.path.abspath(args.repo)
    modules = None
    if args.affected:
        listed = subprocess.run(["python3", "tools/affected_tests.py", "--base", args.affected],
                                cwd=tree, text=True, capture_output=True, check=True).stdout
        modules = listed.split()
    if args.modules:
        modules = sorted({*(modules or ()), *(name for item in args.modules for name in (
            [line.partition("#")[0].strip() for line in Path(item[1:]).read_text().splitlines()]
            if item.startswith("@") else [item]) if name)})
    prefix = ["python3", os.path.join(tree, "tools", "testslot.py")] if args.slots else None
    code, lines = run(tree, args.workers, modules=modules, prefix=prefix,
                      min_free_gb=args.min_free_gb, timings_path=args.timings,
                      report_path=args.report, audit=not args.no_audit)
    if args.typecheck:
        lines += typecheck_report(tree)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
