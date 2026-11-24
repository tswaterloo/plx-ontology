#!/usr/bin/env python3
"""Machine check of the owner's guardrails (11-23): five ratchets over the product code.

    python3 tools/guardrails.py                 # report: count per rule, per file against the baseline
    python3 tools/guardrails.py --write-baseline   # rewrite tools/guardrails-baseline.json (today's counts)
    python3 tools/guardrails.py --lower            # lower baseline counts that fell; never raises one
    python3 tools/guardrails.py --list RULE        # every finding of a rule with file:line

``tests/test_guardrails.py`` fails when a file's count of a rule is above its baseline (a file
that is not in the baseline has 0). A count may go down, never up: fix a finding, run ``--lower``.
A finding that is right as written carries ``# guard-ok: <reason>`` (``//`` in TypeScript) on its
line or the line above; the reason is required and is read by the reviewer. Rules (the numbers are
the owner's guardrails):

  silent-except  (3) broad/bare ``except`` whose body only passes/returns/assigns empty, None, 0,
                 False, [] or {} with no raise and no logging call. Surface a typed status.
  silent-cut     (1) ``x[:N]`` (N >= 3), ``.slice(0, N)``, ``.substring(0, N)``, ``line-clamp-N`` and
                 SQL ``LIMIT N`` (N > 1) without a total/next page in the same function, in operating/,
                 operator_api/ and app/src. Use ``operating.limits.cut`` / ``app/src/lib/cut.ts`` /
                 ``ShowAll``, which say what was left out.
  shape-meaning  (2) module-level *_WORDS / *_KEYWORDS / *_PATTERNS / NOREPLY* / BULK* collections
                 and regexes alternating 3+ plain words, in operating/ and operator_api/. Meaning
                 comes from data, not from word lists.
  duplicate-def  (4) one constant name with different values in 2+ modules, or one status vocabulary
                 (same members) defined in 2+ modules. Define once, import.
  pause-expiry   (5) a systemd drop-in/unit/flag under deploy/ that disables or holds something
                 without an ``expires:`` or ``resume:`` comment.

Stdlib only; the whole scan is a few seconds.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "guardrails-baseline.json"
PY_DIRS = ("operating", "operator_api")
TS_DIR = "app/src"
RULES = {
    "silent-except": "no silent failure: a broad except must log and surface a status, not return/assign empty",
    "silent-cut": "no silent limit: say where it cuts (operating.limits.cut / app/src/lib/cut.ts / ShowAll) and let the rest be reached",
    "shape-meaning": "no guessing meaning from shapes: no word/name lists or word-alternation regexes deciding what something is",
    "duplicate-def": "one definition: a figure or status vocabulary is defined once and imported",
    "pause-expiry": "pauses expire: a hold/disable under deploy/ carries an 'expires:' or 'resume:' comment",
}
# Files that implement the stated cut themselves.
CUT_HELPERS = {"operating/limits.py", "app/src/lib/cut.ts", "app/src/components/ShowAll.tsx"}
# Generic constant names that legitimately differ per module (name -> reason).
CONSTANT_ALLOW = {
    "ROOT": "a module's own path anchor", "LOGGER": "per-module logger", "LOG": "per-module logger",
    "TIMEOUT": "a per-call timeout named alike in unrelated callers", "SCHEMA": "per-module schema name",
    "DEFAULT": "per-module default", "COLUMNS": "per-table column list", "FIELDS": "per-record field list",
    "HEADERS": "per-call HTTP headers", "SQL": "per-module statement",
}
PRAGMA = re.compile(r"(?:#|//)\s*guard-ok:\s*(\S.{2,})")
LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "print_exc", "format_exc"}
LOG_NAMES = {"print", "log", "warn", "warning", "logger", "_log", "_warn", "note", "record_failure"}


class Finding(NamedTuple):
    rule: str
    path: str
    line: int
    text: str


# ---------------------------------------------------------------------------- file discovery
def _is_test(rel: str) -> bool:
    p = Path(rel)
    return ("tests" in p.parts or "__tests__" in p.parts or p.name.startswith("test_")
            or p.name == "conftest.py" or ".test." in p.name or p.name.endswith(".spec.ts"))


def py_files(root: Path = ROOT) -> list[str]:
    out = []
    for d in PY_DIRS:
        for p in sorted((root / d).rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if not _is_test(rel) and "__pycache__" not in rel:
                out.append(rel)
    return out


def ts_files(root: Path = ROOT) -> list[str]:
    out = []
    for ext in ("*.ts", "*.tsx"):
        for p in sorted((root / TS_DIR).rglob(ext)):
            rel = p.relative_to(root).as_posix()
            if not _is_test(rel) and "node_modules" not in rel:
                out.append(rel)
    return out


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8", errors="replace")


def _pragma(lines: list[str], line: int) -> bool:
    """A ``guard-ok: reason`` on the finding's line or the line above."""
    for n in (line, line - 1):
        if 1 <= n <= len(lines) and PRAGMA.search(lines[n - 1]):
            return True
    return False


# ---------------------------------------------------------------------------- python helpers
def _empty(node: ast.AST | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return node.value in (None, 0, False, "", b"", 0.0) and not (isinstance(node.value, str) and node.value.strip())
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_empty(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Call) and not node.args and not node.keywords:
        return isinstance(node.func, ast.Name) and node.func.id in {"list", "dict", "set", "tuple", "frozenset"}
    return False


def _logs(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Raise):
            return True
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and (f.attr in LOG_ATTRS or "log" in f.attr.lower() or "warn" in f.attr.lower()):
                return True
            if isinstance(f, ast.Name) and (f.id in LOG_NAMES or "log" in f.id.lower() or "warn" in f.id.lower()):
                return True
    return False


def _broad(h: ast.ExceptHandler) -> bool:
    t = h.type
    if t is None:
        return True
    names = [t] if not isinstance(t, ast.Tuple) else list(t.elts)
    return any((isinstance(n, ast.Name) and n.id in {"Exception", "BaseException"})
               or (isinstance(n, ast.Attribute) and n.attr in {"Exception", "BaseException"}) for n in names)


def _silent_stmt(s: ast.stmt) -> bool:
    if isinstance(s, (ast.Pass, ast.Continue, ast.Break)):
        return True
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
        return True  # a docstring or a bare literal
    if isinstance(s, ast.Return):
        return _empty(s.value)
    if isinstance(s, (ast.Assign, ast.AnnAssign)):
        return _empty(s.value)
    return False


def _tree(src: str, rel: str) -> ast.AST | None:
    try:
        return ast.parse(src, filename=rel)
    except SyntaxError:
        return None


def _silent_excepts(tree: ast.AST) -> Iterable[tuple[int, str]]:
    for n in ast.walk(tree):
        if isinstance(n, ast.ExceptHandler) and _broad(n):
            if all(_silent_stmt(s) for s in n.body) and not _logs(n):
                what = "bare except" if n.type is None else "broad except"
                yield n.lineno, f"{what} returns/assigns empty with no logging or raise"


HASHY = re.compile(r"sha|hash|digest|uuid|checksum|hexdigest|etag", re.I)  # an id prefix is not a cut text
DATEY = re.compile(r"date|iso|stamp|_at\b|\bat\b|time|when|\bts\b|sent|day|month|due|created|updated", re.I)
DATE_WIDTHS = {4, 7, 10, 13, 16, 19}  # YYYY, YYYY-MM, YYYY-MM-DD, ...T hh, hh:mm, hh:mm:ss


def _slice_cuts(tree: ast.AST) -> Iterable[tuple[int, str]]:
    for n in ast.walk(tree):
        if not isinstance(n, ast.Subscript) or not isinstance(n.slice, ast.Slice):
            continue
        sl = n.slice
        if sl.lower is not None or sl.step is not None or not isinstance(sl.upper, ast.Constant):
            continue
        up = sl.upper.value
        if not isinstance(up, int) or isinstance(up, bool) or up < 3:
            continue
        label = ast.unparse(n.value)
        if HASHY.search(label) or (up in DATE_WIDTHS and DATEY.search(label)):
            continue
        yield n.lineno, f"slice [:{up}] cuts without saying so"


LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.I)
PAGED = re.compile(r"offset|cursor|total|has_more|next_page|next_cursor|truncat|more_", re.I)


def _sql_limits(tree: ast.AST, src: str) -> Iterable[tuple[int, str]]:
    funcs = [(f.lineno, getattr(f, "end_lineno", f.lineno), f) for f in ast.walk(tree)
             if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
    lines = src.splitlines()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            continue
        m = next((m for m in LIMIT_RE.finditer(n.value) if int(m.group(1)) > 1), None)
        if not m or "SELECT" not in n.value.upper():
            continue
        enclosing = [f for f in funcs if f[0] <= n.lineno <= f[1]]
        if enclosing:
            f = min(enclosing, key=lambda t: t[1] - t[0])
            body = "\n".join(lines[f[0] - 1:f[1]])
        else:
            body = n.value
        if PAGED.search(body):
            continue
        yield n.lineno, f"SQL LIMIT {m.group(1)} without a total or next page"


SHAPE_NAME = re.compile(r"(?:WORDS|KEYWORDS|PATTERNS?)$|NOREPLY|BULK")
ALT_WORDS = re.compile(r"(?<![A-Za-z])[A-Za-z]{3,}(?:\|[A-Za-z]{3,}){2,}(?![A-Za-z])")
RE_CALLS = {"compile", "search", "match", "fullmatch", "sub", "subn", "findall", "finditer", "split"}


def _target_names(s: ast.stmt) -> list[str]:
    ts = s.targets if isinstance(s, ast.Assign) else [s.target] if isinstance(s, ast.AnnAssign) else []
    return [t.id for t in ts if isinstance(t, ast.Name)]


def _shape_meaning(tree: ast.Module) -> Iterable[tuple[int, str]]:
    for s in tree.body:
        for name in _target_names(s):
            val = getattr(s, "value", None)
            if SHAPE_NAME.search(name.upper()) and isinstance(
                    val, (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Call, ast.Constant)):
                yield s.lineno, f"{name} is a word/pattern list used to guess meaning"
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args:
            f = n.func
            fname = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            a = n.args[0]
            if fname in RE_CALLS and isinstance(a, ast.Constant) and isinstance(a.value, str) and ALT_WORDS.search(a.value):
                yield n.lineno, "regex alternates 3+ plain words (a word list in disguise)"


# ---------------------------------------------------------------------------- duplicate definitions
BUSINESS_NAME = re.compile(r"SETTLED|^LATE|^OPEN|FLOOR|MIN_FREE|THRESHOLD|DISK|STATUS", re.I)
LIMIT_NAME = re.compile(r"^(?:MAX|MIN)_\w{4,}|\w{4,}_(?:MAX|MIN|LIMIT|CAP|CHARS|TERMS)$|^LIMIT_|^CAP_")
STATUS_NAME = re.compile(r"STATUS|STATES?\b|SETTLED|TERMINAL|VOCAB|OUTCOMES?|_OPEN\b|^OPEN|CLOSED|FINAL|ACTIVE|PENDING", re.I)


def _num(node: ast.AST):
    """A number from a constant or constant arithmetic; None otherwise."""
    try:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, (ast.BinOp, ast.UnaryOp)):
            v = eval(compile(ast.Expression(node), "<c>", "eval"), {"__builtins__": {}}, {})  # constants only
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
    except Exception:  # guard-ok: not a constant expression means not a number; the caller treats None as "skip"
        return None
    return None


def _members(node: ast.AST) -> frozenset | None:
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords and isinstance(node.func, ast.Name) \
            and node.func.id in {"frozenset", "set", "tuple", "list"}:
        node = node.args[0]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and node.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, (str, int, float)) for e in node.elts):
        return frozenset(e.value for e in node.elts)  # type: ignore[attr-defined]
    return None


def _module_constants(tree: ast.Module) -> list[tuple[str, int, object]]:
    out = []
    for s in tree.body:
        val = getattr(s, "value", None)
        if val is None:
            continue
        for name in _target_names(s):
            key = name.lstrip("_")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", key) or key in CONSTANT_ALLOW:
                continue
            if not (LIMIT_NAME.search(key) or BUSINESS_NAME.search(key) or STATUS_NAME.search(key)):
                continue  # only limits, floors, thresholds and status sets: other names are per-module vocabulary
            num = _num(val)
            mem = _members(val)
            if num is not None:
                out.append((key, s.lineno, ("n", num)))
            elif mem is not None and len(mem) >= 2 and STATUS_NAME.search(key):
                out.append((key, s.lineno, ("s", mem)))
    return out


def _related(a: str, b: str, imports: dict[str, set[str]]) -> bool:
    """Two modules that can disagree about one figure: one imports the other, or they are siblings
    of one family (same directory, same first word of the file name: ``autopilot_drafter``/``autopilot_jobs``)."""
    def dotted(r: str) -> str:
        return r[:-3].replace("/", ".").removesuffix(".__init__")
    if dotted(b) in imports.get(a, ()) or dotted(a) in imports.get(b, ()):
        return True
    pa, pb = Path(a), Path(b)
    return pa.parent == pb.parent and pa.stem.split("_")[0] == pb.stem.split("_")[0]


def duplicate_findings(consts: dict[str, list[tuple[str, int, object]]],
                       imports: dict[str, set[str]] | None = None) -> list[Finding]:
    """``consts``: per module, its module-level constants without a ``guard-ok`` (``_module_constants``)."""
    by_name: dict[str, list[tuple[str, int, object]]] = defaultdict(list)
    vocab: dict[frozenset, list[tuple[str, int, str]]] = defaultdict(list)
    for rel, items in consts.items():
        for key, line, val in items:
            by_name[key].append((rel, line, val))
            if val[0] == "s" and STATUS_NAME.search(key) and len(val[1]) >= 3 and all(isinstance(m, str) for m in val[1]):
                vocab[val[1]].append((rel, line, key))
    out: list[Finding] = []
    for key, defs in sorted(by_name.items()):
        anywhere = bool(BUSINESS_NAME.search(key))  # a floor / settled set / status is one figure wherever it is used
        defs = [d for d in defs if any(o[0] != d[0] and repr(o[2]) != repr(d[2]) and (anywhere or _related(d[0], o[0], imports or {}))
                                       for o in defs)]  # a limit counts only against a related module's
        files = {d[0] for d in defs}
        if len(files) >= 2:
            where = ", ".join(f"{d[0]}={_show(d[2])}" for d in sorted(defs)[:4])
            out += [Finding("duplicate-def", d[0], d[1], f"{key} has different values in {len(files)} modules ({where})") for d in defs]
    for mem, defs in vocab.items():
        if len({d[0] for d in defs}) >= 2:  # the same members under any name in 2+ modules is a copy
            names = ", ".join(sorted({f"{d[2]}@{d[0]}" for d in defs})[:4])
            out += [Finding("duplicate-def", d[0], d[1], f"status vocabulary {sorted(mem)[:4]}... defined in {len({x[0] for x in defs})} modules ({names})") for d in defs]
    return out


def _show(v) -> str:
    return str(v[1]) if v[0] == "n" else "{" + ",".join(sorted(map(str, v[1]))[:3]) + ",...}"


# ---------------------------------------------------------------------------- typescript
TS_CUTS = [
    (re.compile(r"\.(?:slice|substring|substr)\(\s*0\s*,\s*(\d+)\s*\)"), lambda m: int(m.group(1)) >= 3,
     "text/list cut with a literal and no note (.slice/.substring)"),
    (re.compile(r"line-clamp-\d+|[Ll]ineClamp"), lambda m: True, "line-clamp hides the rest of a text"),
]


def ts_findings(rel: str, src: str) -> list[Finding]:
    out = []
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        code = line.split("//", 1)[0] if line.lstrip().startswith("//") else line
        if line.lstrip().startswith(("//", "*", "/*")):
            continue
        for rx, ok, msg in TS_CUTS:
            m = rx.search(code)
            if m and ok(m) and not _pragma(lines, i):
                out.append(Finding("silent-cut", rel, i, msg))
    return out


# ---------------------------------------------------------------------------- pauses
PAUSE_LINES = [
    re.compile(r"^\s*(?:ExecStart|ExecCondition|ExecStartPre)\s*=\s*-?(?:/usr)?/bin/(?:true|false)\b"),
    re.compile(r"^\s*ConditionPathExists\s*=\s*!"),
    re.compile(r"^\s*Condition\w*\s*=\s*\S*(?:hold|pause|disabled)", re.I),
    re.compile(r"^\s*Environment\s*=.*\b[A-Z0-9_]*(?:_OFF|DISABLED?|PAUSED?|_HOLD|_SKIP)[A-Z0-9_]*=(?:1|true|yes|on)\b", re.I),
    re.compile(r"^\s*(?:Refuse|Mask)\w*\s*="),
]
PAUSE_ALLOW = {
    "NEXT_TELEMETRY_DISABLED": "Next.js opts out of telemetry; not a hold of ours",
    "DISABLE_AUTOUPDATER": "the Claude CLI's own updater; not a hold of ours",
}
EXPIRES = re.compile(r"^\s*[#;].*\b(?:expires|resume)\s*:", re.I | re.M)
FLAG_NAME = re.compile(r"\.(?:hold|pause|paused|disabled|off|flag)$|^(?:hold|pause|paused|disabled)$", re.I)


def pause_findings(root: Path = ROOT) -> list[Finding]:
    out = []
    base = root / "deploy"
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if "__pycache__" in p.parts or p.is_dir():
            continue
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            if str(p.readlink()) == "/dev/null":
                out.append(Finding("pause-expiry", rel, 1, "unit masked to /dev/null (no expires:/resume: note possible in a link)"))
            continue
        if p.suffix not in {".conf", ".service", ".timer", ".path", ".socket", ".target"} and not FLAG_NAME.search(p.name):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        has_note = bool(EXPIRES.search(text))
        if FLAG_NAME.search(p.name) and not has_note:
            out.append(Finding("pause-expiry", rel, 1, "a hold/flag file without an 'expires:' or 'resume:' comment"))
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(("#", ";")):
                continue
            if any(k in line for k in PAUSE_ALLOW):
                continue
            if any(rx.search(line) for rx in PAUSE_LINES) and not has_note:
                out.append(Finding("pause-expiry", rel, i, "disables/holds something without an 'expires:' or 'resume:' comment"))
    return out


# ---------------------------------------------------------------------------- the scan
def _scan_py(args: tuple[str, str]) -> tuple[list[Finding], list[tuple[str, int, object]], set[str]]:
    """One module: its findings of rules 1-3 and its module-level constants (for rule 4)."""
    root, rel = args
    src = _read(Path(root), rel)
    tree = _tree(src, rel)
    if tree is None:
        return [], [], set()
    lines = src.splitlines()
    found: list[Finding] = []
    found += [Finding("silent-except", rel, ln, m) for ln, m in _silent_excepts(tree)]
    if rel not in CUT_HELPERS:
        found += [Finding("silent-cut", rel, ln, m) for ln, m in _slice_cuts(tree)]
        found += [Finding("silent-cut", rel, ln, m) for ln, m in _sql_limits(tree, src)]
    found += [Finding("shape-meaning", rel, ln, m) for ln, m in _shape_meaning(tree)]  # type: ignore[arg-type]
    out, seen = [], set()
    for f in found:
        if _pragma(lines, f.line) or (f.rule, f.line) in seen:
            continue
        seen.add((f.rule, f.line))
        out.append(f)
    imported: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            imported.add(n.module)
            imported.update(f"{n.module}.{a.name}" for a in n.names)
        elif isinstance(n, ast.Import):
            imported.update(a.name for a in n.names)
    return out, [c for c in _module_constants(tree) if not _pragma(lines, c[1])], imported  # type: ignore[arg-type]


def scan(root: Path = ROOT) -> list[Finding]:
    files = py_files(root)
    jobs = [(str(root), rel) for rel in files]
    if len(jobs) > 40:
        import os
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as pool:  # parent-watch: short-lived scan pool, closed by the with block
            results = list(pool.map(_scan_py, jobs, chunksize=8))
    else:
        results = [_scan_py(j) for j in jobs]
    findings: list[Finding] = []
    consts: dict[str, list] = {}
    imports: dict[str, set[str]] = {}
    for rel, (found, cs, imp) in zip(files, results):
        findings += found
        consts[rel] = cs
        imports[rel] = imp
    findings += duplicate_findings(consts, imports)
    for rel in ts_files(root):
        if rel not in CUT_HELPERS:
            findings += ts_findings(rel, _read(root, rel))
    findings += pause_findings(root)
    first: dict[tuple[str, str, int], Finding] = {}
    for f in sorted(set(findings)):
        first.setdefault((f.rule, f.path, f.line), f)  # one finding per line
    return sorted(first.values())


def counts(findings: Iterable[Finding]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {r: {} for r in RULES}
    for f in findings:
        out[f.rule][f.path] = out[f.rule].get(f.path, 0) + 1
    return {r: dict(sorted(v.items())) for r, v in out.items()}


def load_baseline() -> dict[str, dict[str, int]]:
    return json.loads(BASELINE.read_text())["rules"] if BASELINE.exists() else {r: {} for r in RULES}


def write_baseline(c: dict[str, dict[str, int]]) -> None:
    lines = ['{', '"comment": "Ratchet baseline of tools/guardrails.py: findings per rule per file today. A count may go down, never up. Rewrite with --write-baseline, or only lower with --lower.",', '"rules": {']
    rule_blocks = []
    for r, files in c.items():
        body = ",\n".join(f'  {json.dumps(p)}: {n}' for p, n in files.items())
        rule_blocks.append(f' {json.dumps(r)}: {{\n{body}\n }}' if files else f' {json.dumps(r)}: {{}}')
    lines.append(",\n".join(rule_blocks))
    lines += ['}', '}']
    BASELINE.write_text("\n".join(lines) + "\n")
    json.loads(BASELINE.read_text())


def over_baseline(findings: list[Finding], base: dict[str, dict[str, int]]) -> list[tuple[str, str, int, int]]:
    """(rule, path, now, allowed) for every file whose count is above its baseline."""
    now = counts(findings)
    return [(r, p, n, base.get(r, {}).get(p, 0)) for r, files in now.items() for p, n in files.items()
            if n > base.get(r, {}).get(p, 0)]


def added_lines(path: str, base_ref: str) -> set[int] | None:
    """Line numbers of ``path`` added since ``base_ref`` (None when git cannot say)."""
    try:
        d = subprocess.run(["git", "diff", "-U0", base_ref, "--", path], cwd=ROOT, capture_output=True, text=True, timeout=30)
        if d.returncode:
            return None
        lines: set[int] = set()
        for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", d.stdout, re.M):
            start, n = int(m.group(1)), int(m.group(2) or 1)
            lines.update(range(start, start + n))
        return lines
    except Exception as exc:
        print(f"guardrails: git diff unavailable ({exc}); listing every finding of the file", file=sys.stderr)
        return None


def explain(findings: list[Finding], over: list[tuple[str, str, int, int]], base_ref: str = "origin/main") -> str:
    msgs = []
    for rule, path, n, allowed in over:
        mine = [f for f in findings if f.rule == rule and f.path == path]
        added = added_lines(path, base_ref)
        new = [f for f in mine if added is not None and f.line in added] or mine
        msgs.append(f"[{rule}] {path}: {n} findings, baseline {allowed}. Rule: {RULES[rule]}")
        msgs += [f"    {f.path}:{f.line}: {f.text}" for f in new]
    msgs.append("Fix it, or say why it is right with '# guard-ok: <reason>' (// in TypeScript). "
                "Never raise tools/guardrails-baseline.json.")
    return "\n".join(msgs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--lower", action="store_true")
    ap.add_argument("--list", metavar="RULE")
    a = ap.parse_args(argv)
    findings = scan()
    now = counts(findings)
    if a.write_baseline:
        write_baseline(now)
    elif a.lower:
        base = load_baseline()
        lowered = {r: {p: min(n, now.get(r, {}).get(p, 0)) for p, n in files.items() if now.get(r, {}).get(p, 0) > 0}
                   for r, files in base.items()}
        write_baseline(lowered)
    if a.list:
        for f in findings:
            if f.rule == a.list:
                print(f"{f.path}:{f.line}: {f.text}")
    base = load_baseline()
    print(f"{'rule':<15}{'findings':>9}{'files':>7}{'baseline':>10}")
    for r in RULES:
        print(f"{r:<15}{sum(now[r].values()):>9}{len(now[r]):>7}{sum(base.get(r, {}).values()):>10}")
    over = over_baseline(findings, base)
    if over:
        print(explain(findings, over))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
