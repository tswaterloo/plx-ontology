#!/usr/bin/env python3
"""Print a code map: one compact line per Python module, grouped by package.

USAGE
    python3 scripts/gen_codemap.py [--section PREFIX ...] [--out FILE] [--root DIR]

Each line gives the module's purpose (docstring), main public names, first-party imports
and the test modules that import it, from the AST import graph of the
development tooling. Modules that
the upstream pin file lists as mirroring upstream files also name those pins.
The map is built on demand and never committed. It goes to stdout, or to ``--out``;
each ``--section`` keeps only a package (``operating``) or the modules under a path
prefix (``operating/case_``), plus the header.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from devloop import import_graph  # noqa: E402

PINS = "devloop/upstream-pins.json"
DOC_CHARS = 110
CAPS = {"names": 8, "imports": 12, "tests": 6, "pins": 3}

HEADER = """\
# Code map (generated)

Built from the AST on demand by `python3 scripts/gen_codemap.py [--section PREFIX ...]
[--out FILE]`; it is not committed, so it is never stale.

How to use
- Read your package's section before touching code instead of grepping around:
  `--section operating` for a package, `--section operating/case_` for modules by prefix.
- Tests for a change: `python3 devloop/affected_tests.py --base origin/main` (tests of the
  changed modules and of everything that imports them, transitively; `ALL` means the
  whole gate set). Run them with `python3 -m unittest <modules>`.
- Line: path: purpose | names (public top-level names, those other modules import first)
  | imports (first-party) | tests (test modules importing it) | pins (upstream files it
  mirrors, from `devloop/upstream-pins.json`). Lists are capped, `+N` is the rest.
- Edges are static imports plus string references to a module name or file name
  (`devloop/import_graph.py`). Dependents are not listed; ask `affected_tests.py`.
"""


def render(root: Path, sections: Sequence[str] = ()) -> str:
    """The whole map, or only what ``sections`` select. A section is a package name (all of
    it) or a path prefix (the modules and test directories under it); one that selects
    nothing raises ``LookupError``."""
    graph = import_graph.build(root)
    uses: dict[tuple[str, str], int] = defaultdict(int)
    importers: dict[str, list[str]] = defaultdict(list)
    for module in graph.values():
        for pair in module.from_names:
            uses[pair] += 1
        for target in module.imports:
            importers[target].append(module.name)
    pins = _pins(root)
    packages: dict[str, list[import_graph.Module]] = defaultdict(list)
    for module in graph.values():
        packages[module.path.partition("/")[0] if "/" in module.path else "(root)"].append(module)
    matched: set[str] = set()
    out = [HEADER]
    for package in sorted(packages):
        modules = sorted(packages[package], key=lambda m: m.path)
        code = [m for m in modules if not m.is_test]
        tests = [m for m in modules if import_graph.is_unittest_module(m.name)]
        entries = []
        for m in code:
            names = sorted(m.public, key=lambda n: -uses[(m.name, n)])
            direct = sorted(i for i in importers[m.name] if import_graph.is_unittest_module(i))
            fields = [("names", names), ("imports", list(m.imports)), ("tests", direct),
                      ("pins", pins.get(m.path, []))]
            parts = [f"{key}: {_capped(items, CAPS[key])}" for key, items in fields if items]
            head = f"{m.path}: {_clip(m.doc, DOC_CHARS)}" if m.doc else m.path
            entries.append((m.path, f"- {' | '.join([head] + parts)}\n"))
        for directory, group in _by_directory(tests):
            entries.append((f"{directory}/", f"- tests in {directory}/: "
                            f"{', '.join(t.name.rpartition('.')[2] for t in group)}\n"))
        note = f"{len(code)} modules, {len(tests)} test modules"
        if sections:
            whole = {p for p in sections if p.rstrip("/") == package}
            kept = [(key, line) for key, line in entries if whole or any(key.startswith(p) for p in sections)]
            matched |= whole | {p for p in sections if any(key.startswith(p) for key, _ in entries)}
            if not kept:
                continue
            entries = kept
            note = note if whole else f"{note}; matching lines only"
        out.append(f"\n## {package} ({note})\n")
        out.extend(line for _, line in entries)
    if missing := [p for p in sections if p not in matched]:
        raise LookupError(f"no package or module path starts with: {', '.join(missing)}")
    return "".join(out)


def _by_directory(tests: list[import_graph.Module]) -> list[tuple[str, list[import_graph.Module]]]:
    groups: dict[str, list[import_graph.Module]] = defaultdict(list)
    for test in tests:
        groups[test.path.rpartition("/")[0]].append(test)
    return sorted(groups.items())


def _capped(items: list[str], cap: int) -> str:
    return ", ".join(items[:cap]) + (f" (+{len(items) - cap})" if len(items) > cap else "")


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def _pins(root: Path) -> dict[str, list[str]]:
    path = root / PINS
    if not path.is_file():
        return {}
    found: dict[str, set[str]] = defaultdict(set)
    for pin in json.loads(path.read_text(encoding="utf-8")).get("pins", []):
        for module in pin.get("workbench_modules") or [pin.get("workbench_module")]:
            if module and module.endswith(".py"):
                found[module].add(f"{pin['repo']}:{pin['path']}")
    return {module: sorted(pins) for module, pins in found.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--section", nargs="+", action="extend", default=[], metavar="PREFIX",
                        help="only this package or modules under this path prefix (repeatable)")
    parser.add_argument("--out", type=Path, help="write the map to this file instead of stdout")
    parser.add_argument("--root", type=Path, default=ROOT, help="repo root (default: this checkout)")
    args = parser.parse_args(argv)
    try:
        text = render(args.root, args.section)
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"{args.out}: {len(text.encode('utf-8')):,} bytes")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
