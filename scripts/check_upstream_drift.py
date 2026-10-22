#!/usr/bin/env python3
"""Report upstream drift for files whose logic the Workbench mirrors.

USAGE
    python3 scripts/check_upstream_drift.py [--pins verify/upstream-pins.json]
        [--checkout REPO=PATH ...] [--json] [--max-files N]
    python3 scripts/check_upstream_drift.py --align-lagging [--checkout REPO=PATH ...]

Each pin in ``verify/upstream-pins.json`` names an upstream file, the commit
the Workbench read it at, and the sha256 of the file at that commit. The check
compares that sha256 with the file at the current HEAD of a local checkout.

Checkout lookup, first match wins: ``--checkout REPO=PATH``; env
``PLX_DRIFT_CHECKOUT_<REPO>`` (REPO upper-cased, non-alphanumerics become
``_``, for example ``PLX_DRIFT_CHECKOUT_PLX_CUSTOMER_PORTAL``); the pin file's
``repos.<REPO>.default_checkout`` (``/home/user/<REPO>``).

Output: one block per drifted file with a short ``git diff --shortstat`` from
the pinned commit to HEAD, the Workbench modules and the contract that cite
it. A pin whose commit or path could not be resolved when the pin was seeded
(``status: unresolved``) is listed, never compared, never guessed.

Exit codes
    0  no drift in any checked pin
    1  drift (changed or deleted upstream file); takes precedence over 2
    2  a checkout is missing or not a git repo (its pins are skipped and
       listed) and no drift was found in the pins that could be checked

Purely local git reads (``rev-parse``, ``show``, ``diff``, ``cat-file``).
No network, no writes to any checkout. Run it before building on an
upstream-mirroring module and after each upstream pull.

``--align-lagging`` checks nothing:
it moves each checkout whose HEAD lags its ``repo_pins`` commit (HEAD a strict ancestor
of it, so every pinned file would read as drift that is only the checkout's age) to that
commit, detached, with a local ``git checkout``. A checkout at, ahead of or beside its pin
(real upstream drift the check must keep seeing), with uncommitted changes, or without the
pinned commit is left as it is and named. Exit 0.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ontology.upstream import checkout_for  # noqa: E402

DEFAULT_PINS = ROOT / "verify" / "upstream-pins.json"


def git(checkout: Path, *args: str) -> Optional[bytes]:
    """Run a git command (read-only, but for ``align_lagging``'s checkout); return stdout,
    or None on any failure."""
    proc = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    return proc.stdout if proc.returncode == 0 else None


def is_git_checkout(path: Path) -> bool:
    return path.is_dir() and git(path, "rev-parse", "--git-dir") is not None


def file_sha256_at(checkout: Path, rev: str, path: str) -> Optional[str]:
    data = git(checkout, "show", f"{rev}:{path}")
    return None if data is None else sha256(data).hexdigest()


def diff_stat(checkout: Path, commit: str, path: str) -> str:
    out = git(checkout, "diff", "--shortstat", commit, "HEAD", "--", path)
    if out is None:
        return "diff stat unavailable (pinned commit not in checkout)"
    text = out.decode("utf-8", "replace").strip()
    return text or "content differs (no line stat)"


def check(pins: dict, overrides: dict[str, str]) -> dict:
    """Compare every resolved pin with its checkout HEAD. Returns a report dict."""
    report: dict = {
        "checked": 0,
        "unchanged": 0,
        "drift": [],
        "unresolved": [],
        "missing_checkouts": [],
        "heads": {},
    }
    checkouts: dict[str, Optional[Path]] = {}
    for pin in pins.get("pins", []):
        repo = pin["repo"]
        if pin.get("status") != "resolved" or not pin.get("sha256"):
            report["unresolved"].append(
                {"repo": repo, "path": pin["path"], "commit": pin["commit"],
                 "reason": pin.get("reason", "unresolved when seeded")})
            continue
        if repo not in checkouts:
            path = checkout_for(repo, overrides, pins)
            checkouts[repo] = path if is_git_checkout(path) else None
            if checkouts[repo] is None:
                report["missing_checkouts"].append({"repo": repo, "path": str(path)})
            else:
                head = git(path, "rev-parse", "HEAD")
                report["heads"][repo] = head.decode().strip() if head else None
        checkout = checkouts[repo]
        if checkout is None:
            continue
        report["checked"] += 1
        current = file_sha256_at(checkout, "HEAD", pin["path"])
        if current == pin["sha256"]:
            report["unchanged"] += 1
            continue
        report["drift"].append({
            "repo": repo,
            "path": pin["path"],
            "commit": pin["commit"],
            "state": "deleted" if current is None else "changed",
            "stat": ("file absent at HEAD" if current is None
                     else diff_stat(checkout, pin["commit"], pin["path"])),
            "workbench_modules": pin.get("workbench_modules") or [pin.get("workbench_module")],
            "contract": pin.get("contract", ""),
        })
    return report


def align_lagging(pins: dict, overrides: dict[str, str]) -> list[dict]:
    """Move every checkout that lags its repo pin to the pinned commit (module docstring);
    one ``{repo, path, action, head}`` per repo pin."""
    results = []
    for repo_pin in pins.get("repo_pins", []):
        repo, commit = repo_pin.get("repo"), repo_pin.get("commit")
        if not isinstance(commit, str):
            continue  # a pin of a blob, not of a commit
        path = checkout_for(repo, overrides, pins)
        out = {"repo": repo, "path": str(path), "head": None}
        results.append(out)
        if not is_git_checkout(path):
            out["action"] = "skipped:no_checkout"
            continue
        head = (git(path, "rev-parse", "HEAD") or b"").decode().strip()
        out["head"] = head or None
        if len(commit) != 40 or git(path, "cat-file", "-e", f"{commit}^{{commit}}") is None:
            out["action"] = "skipped:pinned_commit_absent"
        elif head == commit:
            out["action"] = "at_pin"
        elif git(path, "merge-base", "--is-ancestor", head, commit) is None:
            out["action"] = "skipped:not_behind_pin"
        elif git(path, "status", "--porcelain", "--untracked-files=no") != b"":
            out["action"] = "skipped:local_changes"
        elif git(path, "checkout", "--quiet", "--detach", commit) is None:
            out["action"] = "skipped:checkout_failed"
        else:
            out.update(action="moved_to_pin", head=commit)
    return results


def exit_code(report: dict) -> int:
    if report["drift"]:
        return 1
    return 2 if report["missing_checkouts"] else 0


def render(report: dict, max_files: int) -> str:
    lines = []
    for repo, head in sorted(report["heads"].items()):
        lines.append(f"HEAD {repo} {str(head)[:9]}")
    for miss in report["missing_checkouts"]:
        lines.append(f"SKIPPED {miss['repo']}: no git checkout at {miss['path']}")
    lines.append(
        f"checked {report['checked']} pins: {report['unchanged']} unchanged, "
        f"{len(report['drift'])} drifted; {len(report['unresolved'])} unresolved "
        f"(not compared); {len(report['missing_checkouts'])} checkout(s) missing")
    for item in report["drift"][:max_files]:
        mods = ", ".join(sorted({m for m in item["workbench_modules"] if m}))
        lines.append(f"DRIFT {item['state']} {item['repo']}:{item['path']} "
                     f"(pinned {item['commit'][:9]}): {item['stat']}")
        lines.append(f"      modules: {mods}")
        if item["contract"]:
            lines.append(f"      contract: {item['contract']}")
    if len(report["drift"]) > max_files:
        lines.append(f"... {len(report['drift']) - max_files} more drifted pins (use --json or --max-files)")
    affected = sorted({m for i in report["drift"] for m in i["workbench_modules"] if m})
    if affected:
        lines.append("affected modules: " + ", ".join(affected))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS)
    parser.add_argument("--checkout", action="append", default=[], metavar="REPO=PATH")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--max-files", type=int, default=60)
    parser.add_argument("--align-lagging", action="store_true",
                        help="move each checkout behind its repo pin to the pin; check nothing")
    args = parser.parse_args(argv)
    overrides = {}
    for item in args.checkout:
        repo, sep, path = item.partition("=")
        if not sep or not repo or not path:
            parser.error(f"--checkout needs REPO=PATH, got {item!r}")
        overrides[repo] = path
    pins = json.loads(args.pins.read_text(encoding="utf-8"))
    if args.align_lagging:
        for item in align_lagging(pins, overrides):
            print(f"ALIGN {item['repo']} {item['action']} HEAD {str(item['head'])[:9]}")
        return 0
    report = check(pins, overrides)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json
          else render(report, args.max_files))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
