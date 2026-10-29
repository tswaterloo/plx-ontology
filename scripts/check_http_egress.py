"""Static HTTP-egress gate (`http-egress` in devloop/gate-checks.json).

Hard rule: Graph, SharePoint, BC, Portal, MC and mail HTTP is read-only. This AST
scan is a tripwire, not a sandbox: it fails when non-test code under the scanned
trees could send a non-GET request or reach the network outside the reviewed
GET-only clients. It resolves import aliases and simple ``X = Request`` rebinding.

Findings (rule codes):
  banned-import         http.client, requests, httpx, aiohttp, urllib3, socket,
                        smtplib, ftplib, ... imported at all (also via
                        ``__import__``/``importlib.import_module`` with a literal)
  urllib-import         ``urllib.request`` imported by a file not in URLLIB_FILES
  non-get-request       ``Request`` (any alias) without a literal ``method="GET"``
                        or with ``data``/a body argument
  opener-body           ``urlopen(url, data)`` or ``opener.open(url, data)``
                        where ``opener`` came from ``build_opener``/OpenerDirector
  request-subclass      a subclass of ``Request`` or ``get_method`` override
  request-mutation      ``.add_data(...)`` call or ``.method``/``.data`` assignment
  dynamic-access        ``getattr``/``partial`` reaching Request/urlopen/opener
                        objects, or ``getattr(<net module>, <non-literal>)``
  shell-http            subprocess/os call whose arguments mention curl/wget
  unguarded-fetch       ``urlretrieve``/``URLopener`` legacy fetchers
  sender-reference      any import/reference of ``urllib_sender`` outside
                        live_source_guard.py and plx_hub_guard.py
  install-opener        ``install_opener`` (global opener swap)
  outside-sender        in a SENDER_URLS file, any Request/urlopen/opener call (a GET
                        too) outside its allowlisted function
  unlisted-url          in a SENDER_URLS file, any string holding ``://`` other than
                        its listed URLs (literal, concatenated or formatted)

A bare ``Request`` name or ``.Request`` attribute call is checked by name too, so a
re-export (``from integrations.http import Request``) cannot hide a non-GET call.

ALLOWED lists (path, qualified enclosing function) pairs verified by reading the code:
  integrations/live_source_guard.py urllib_sender  the one guarded sender: runs
      after check_request (GET, or the OAuth client-credentials token POST).
  devloop/wake_bridge.py fire  controller tooling: one POST to the Claude routine
      fire endpoint (FIRE_URL allowlist, redirects refused). It reaches no
      business system; the controller owner may retire it.
  operating/messages_planner.py post_messages  the opt-in Messages API planner
      (decision devloop/DECISIONS/messages-api-planner-q7.md): one POST to
      https://api.anthropic.com/v1/messages (API_URL; any other url raises before the
      request), redirects refused (a redirect handler that raises). It reaches no
      business system; the body carries the reader's own trimmed tool results.
SENDER_URLS pins a sender-only file to its URLs and its one sending function: no other
URL string and no other network call (outside-sender, unlisted-url).
ALLOWED_IMPORTS: ``http.client.IncompleteRead`` (an exception class) in the guard.
URLLIB_FILES lists the files that import ``urllib.request``; each builds
``Request(..., method="GET")`` with no redirect following (verified), except the
allowlisted senders above.

Usage: python3 scripts/check_http_egress.py [--json] [ROOT]   (exit 0 clean, 1 findings)
``--json`` prints one object instead (live check ``gate.http_egress``): findings and the
unlisted senders (path:function:rule), and the allowlisted senders the scan excused
(path:function); names and counts only, never source text. A REQUIRED_TREES directory
absent under ROOT is never clean: text mode exits 1; ``--json`` prints only
``blocked_reason`` ``scan_tree_missing`` and the missing names and exits 0, so the live
check reads blocked (its ``blocked_when`` holds only at the expected exit code).
The gate runs main's copy of this script against the candidate tree (ROOT), so a
candidate cannot widen its own allowlist; see devloop/gate-checks.json. The gate
command still falls back to the candidate's copy when origin/main has no such file
(first introduction or a shallow clone without the ref); that fallback is kept on
purpose, because dropping it would fail the gate where the ref is missing.

Known limitations (a tripwire sees syntax, not data flow): a curl/wget command or tool
name built outside the call (``cmd = ["curl", url]; subprocess.run(cmd)``, or
``TOOL = "curl"`` at module level; a literal ``subprocess.run(["curl", url])`` is
flagged), and a ``lambda`` or code directly in an allowlisted function's body (the
allowlist matches path plus enclosing function; a nested ``def`` is flagged).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("ontology", "integrations", "operating", "operator_ui", "ops_plane",
           "reasoning", "engineering", "scripts", "devloop", "twin")
# Trees that must exist under ROOT: they hold the reviewed clients and senders. One absent
# means the scan read the wrong root, never a clean tree (the others may be retired).
REQUIRED_TREES = ("integrations", "operating", "scripts", "devloop")

SENDER_FILES = {"integrations/live_source_guard.py", "integrations/plx_hub_guard.py"}
ALLOWED = {
    ("integrations/live_source_guard.py", "urllib_sender"),
    ("devloop/wake_bridge.py", "fire"),
    ("operating/messages_planner.py", "post_messages"),
}
SENDER_URLS = {"operating/messages_planner.py": {"https://api.anthropic.com/v1/messages"}}
# Exception classes only (no connection can be opened through them).
ALLOWED_IMPORTS = {("integrations/live_source_guard.py", "http.client.IncompleteRead")}
URLLIB_FILES = {
    "integrations/http.py",
    "integrations/live_source_guard.py",
    "devloop/wake_bridge.py",
    "operating/messages_planner.py",
}
BANNED_MODULES = {
    "http.client", "httplib", "requests", "httpx", "aiohttp", "urllib3", "socket",
    "smtplib", "ftplib", "telnetlib", "imaplib", "poplib", "nntplib", "websockets",
    "websocket", "xmlrpc.client", "pycurl", "paramiko", "ssl",
    "multiprocessing.connection",
}
NET_MODULES = {"urllib", "urllib.request", "http", "http.client"}
REQUEST = "urllib.request.Request"
OPENER_BUILDERS = {"urllib.request.build_opener", "urllib.request.OpenerDirector"}
URLOPEN = "urllib.request.urlopen"
LEGACY = {"urllib.request.urlretrieve", "urllib.request.URLopener",
          "urllib.request.FancyURLopener"}
NET_NAMES = {"Request", "urlopen", "build_opener", "OpenerDirector", "urlretrieve"}
SHELL_CALLS = {"subprocess.run", "subprocess.Popen", "subprocess.call",
               "subprocess.check_call", "subprocess.check_output", "os.system",
               "os.popen", "os.execv", "os.execvp", "os.execl", "os.execlp",
               "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"}
ASYNC_NET = {"asyncio.open_connection", "asyncio.start_server",
             "asyncio.open_unix_connection"}


def _banned(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in BANNED_MODULES)


class _Scanner(ast.NodeVisitor):
    def __init__(self, relative: str):
        self.relative = relative
        self.aliases: dict[str, str] = {}
        self.openers: set[str] = set()
        self.stack: list[str] = []
        self.call_funcs: set[int] = set()
        self.exempt: set[int] = set()
        self.findings: set[tuple[str, str, str]] = set()
        self.excused: set[tuple[str, str]] = set()
        self.urls = SENDER_URLS.get(relative)

    # ---- helpers
    def _flag(self, rule: str, *, write: bool = False) -> None:
        function = ".".join(self.stack) if self.stack else "<module>"
        if write and (self.relative, function) in ALLOWED:
            self.excused.add((self.relative, function))
            return
        self.findings.add((self.relative, function, rule))

    def _dotted(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def _check_import(self, module: str) -> None:
        if _banned(module):
            self._flag("banned-import")
        if module == "urllib.request" and self.relative not in URLLIB_FILES:
            self._flag("urllib-import")

    # ---- imports / aliases
    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self._check_import(item.name)
            if item.name.split(".")[-1] == "urllib_sender" and self.relative not in SENDER_FILES:
                self._flag("sender-reference")
            if item.asname:
                self.aliases[item.asname] = item.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if not all((self.relative, f"{module}.{i.name}") in ALLOWED_IMPORTS
                   for i in node.names):
            self._check_import(module)
        for item in node.names:
            full = f"{module}.{item.name}" if module else item.name
            if (self.relative, full) not in ALLOWED_IMPORTS:
                self._check_import(full)
            if item.name == "urllib_sender" and self.relative not in SENDER_FILES:
                self._flag("sender-reference")
            self.aliases[item.asname or item.name] = full
        self.generic_visit(node)

    def _value_reference(self, node: ast.AST, dotted: str | None) -> None:
        """Request/urlopen/opener builders used as values (passed, stored, looked up)."""
        if (dotted in {REQUEST, URLOPEN} | OPENER_BUILDERS | LEGACY
                and id(node) not in self.call_funcs and id(node) not in self.exempt):
            self._flag("dynamic-access", write=True)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
        if (self.urls is not None and isinstance(value, str) and "://" in value
                and value not in self.urls):
            self._flag("unlisted-url")

    def visit_arg(self, node: ast.arg) -> None:
        return None  # annotations are not references

    def visit_Attribute(self, node: ast.Attribute) -> None:
        dotted = self._dotted(node)
        self._value_reference(node, dotted)
        if dotted and (_banned(dotted) or dotted == "urllib.request"):
            self._check_import(dotted)
        if node.attr == "urllib_sender" and self.relative not in SENDER_FILES:
            self._flag("sender-reference")
        if node.attr == "__dict__" and self._dotted(node.value) in NET_MODULES:
            self._flag("dynamic-access", write=True)
        if node.attr in ("install_opener",):
            self._flag("install-opener", write=True)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._value_reference(node, self.aliases.get(node.id))
        if node.id == "urllib_sender" and self.relative not in SENDER_FILES:
            self._flag("sender-reference")
        if node.id == "install_opener":
            self._flag("install-opener", write=True)

    # ---- scopes
    def _scope(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == "get_method":
            self._flag("request-subclass", write=True)
        self._scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if any(self._dotted(base) == REQUEST for base in node.bases):
            self._flag("request-subclass", write=True)
        self._scope(node, node.name)

    # ---- assignments (aliasing, opener tracking, mutation)
    def visit_Assign(self, node: ast.Assign) -> None:
        value_name = self._dotted(node.value) if isinstance(
            node.value, (ast.Name, ast.Attribute)) else None
        builder = (self._dotted(node.value.func)
                   if isinstance(node.value, ast.Call) else None)
        if value_name in {REQUEST, URLOPEN} | OPENER_BUILDERS and all(
                isinstance(t, ast.Name) for t in node.targets):
            self.exempt.add(id(node.value))  # plain alias; calls through it are checked
        for target in node.targets:
            if isinstance(target, ast.Name):
                if value_name and (value_name in {REQUEST, URLOPEN} | OPENER_BUILDERS
                                   or value_name in NET_MODULES):
                    self.aliases[target.id] = value_name
                if builder in OPENER_BUILDERS:
                    self.openers.add(target.id)
            if builder in OPENER_BUILDERS and isinstance(target, ast.Attribute):
                self.openers.add(ast.unparse(target))
            if isinstance(target, ast.Attribute) and target.attr in (
                    "method", "data", "get_method"):
                self._flag("request-mutation", write=True)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit_Assign(ast.Assign(targets=[node.target], value=node.value,
                                         lineno=node.lineno))
        else:
            self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Attribute) and node.target.attr in ("method", "data"):
            self._flag("request-mutation", write=True)
        self.generic_visit(node)

    # ---- calls
    def visit_Call(self, node: ast.Call) -> None:
        self.call_funcs.add(id(node.func))
        target = self._dotted(node.func)
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        has_star = any(isinstance(a, ast.Starred) for a in node.args) or any(
            k.arg is None for k in node.keywords)
        by_name = (isinstance(node.func, ast.Name) and node.func.id == "Request") or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "Request")
        if target == REQUEST or by_name:
            method = keywords.get("method")
            body = len(node.args) >= 2 or "data" in keywords
            literal_get = isinstance(method, ast.Constant) and method.value == "GET"
            if body or has_star or not literal_get:
                self._flag("non-get-request", write=True)
        elif target == URLOPEN:
            if len(node.args) >= 2 or "data" in keywords or has_star:
                self._flag("opener-body", write=True)
        elif target in LEGACY:
            self._flag("unguarded-fetch", write=True)
        if isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            named = ast.unparse(receiver)
            is_opener = (named in self.openers or "opener" in named.lower()) or (
                isinstance(receiver, ast.Call)
                and self._dotted(receiver.func) in OPENER_BUILDERS)
            if node.func.attr == "open" and is_opener and (
                    len(node.args) >= 2 or "data" in keywords or has_star):
                self._flag("opener-body", write=True)
            if self.urls is not None and node.func.attr == "open" and is_opener:
                self._flag("outside-sender", write=True)
            if node.func.attr == "add_data":
                self._flag("request-mutation", write=True)
        if self.urls is not None and (
                by_name or target in {REQUEST, URLOPEN} | OPENER_BUILDERS | LEGACY):
            self._flag("outside-sender", write=True)
        if target == "setattr" and len(node.args) >= 2 and isinstance(
                node.args[1], ast.Constant) and node.args[1].value in (
                "method", "data", "get_method"):
            self._flag("request-mutation", write=True)
        if target == "vars" and node.args and self._dotted(node.args[0]) in NET_MODULES:
            self._flag("dynamic-access", write=True)
        if target in ASYNC_NET:
            self._flag("banned-import")
        if target == "getattr" and node.args:
            obj = self._dotted(node.args[0])
            attr = node.args[1] if len(node.args) > 1 else None
            literal = attr.value if isinstance(attr, ast.Constant) else None
            if obj in NET_MODULES and not (isinstance(literal, str) and literal.isidentifier()
                                           and literal not in NET_NAMES
                                           and literal not in ("client", "request")):
                self._flag("dynamic-access", write=True)
            elif isinstance(literal, str) and literal in NET_NAMES:
                self._flag("dynamic-access", write=True)
        if target in ("functools.partial", "partial", "functools.partialmethod") and node.args:
            first = self._dotted(node.args[0]) or ""
            if first in {REQUEST, URLOPEN} | OPENER_BUILDERS | LEGACY or first in self.openers \
                    or first.rsplit(".", 1)[-1] in NET_NAMES or first.endswith(".open") \
                    and first.split(".")[0] in self.openers:
                self._flag("dynamic-access", write=True)
        if target in ("__import__", "importlib.import_module") and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self._check_import(first.value)
            else:
                self._flag("dynamic-access")
        if target in SHELL_CALLS:
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Constant) and isinstance(inner.value, str)
                        and ({"curl", "wget", "nc", "ncat"}
                             & set(inner.value.lower().replace("/", " ").split())
                             or any(m in inner.value for m in (
                                 "fetch(", "requests.", "import requests", "urllib", "http.client",
                                 "urlopen", "XMLHttpRequest")))):
                    self._flag("shell-http")
                    break
        self.generic_visit(node)


def _scan(source: str, relative: str) -> _Scanner:
    scanner = _Scanner(relative)
    scanner.visit(ast.parse(source, relative))
    return scanner


def scan_source(source: str, relative: str) -> set[tuple[str, str, str]]:
    """Findings (path, enclosing function, rule) for one module's source text."""
    return _scan(source, relative).findings


def scan_report(root: Path = ROOT) -> dict:
    """The scan of every non-test module under SCANNED: findings and the excused
    (path, function) pairs of ALLOWED, i.e. the allowlisted senders present."""
    found: set[tuple[str, str, str]] = set()
    excused: set[tuple[str, str]] = set()
    files = 0
    for top in SCANNED:
        for path in sorted((root / top).rglob("*.py")):
            relative = path.relative_to(root)
            if "tests" in relative.parts or "__pycache__" in relative.parts:
                continue
            scanner = _scan(path.read_text(encoding="utf-8"), relative.as_posix())
            found |= scanner.findings
            excused |= scanner.excused
            files += 1
    return {"findings": found, "excused": excused, "files": files}


def scan_tree(root: Path = ROOT) -> set[tuple[str, str, str]]:
    return scan_report(root)["findings"]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if len(argv) > 1 or any(a.startswith("-") for a in argv):
        print("usage: check_http_egress.py [--json] [ROOT]", file=sys.stderr)
        return 2
    root = Path(argv[0]).resolve() if argv else ROOT
    missing = [top for top in REQUIRED_TREES if not (root / top).is_dir()]
    if missing:
        if as_json:
            print(json.dumps({"status": "blocked", "blocked_reason": "scan_tree_missing",
                              "missing_trees": missing}, sort_keys=True))
            return 0
        print(f"http-egress: scan tree missing: {', '.join(missing)}")
        return 1
    report = scan_report(root)
    found = report["findings"]
    if as_json:
        senders = sorted(f"{path}:{function}" for path, function in report["excused"])
        print(json.dumps({
            "status": "findings" if found else "clean", "files_scanned": report["files"],
            "findings": len(found),
            "unlisted_senders": sorted(":".join(item) for item in found),
            "allowlisted_senders": senders, "allowlisted_count": len(senders),
            "allowlisted_absent": sorted(f"{p}:{f}" for p, f in ALLOWED - report["excused"]),
        }, sort_keys=True))
        return 1 if found else 0
    for path, function, rule in sorted(found):
        print(f"http-egress: {path}:{function}: {rule}")
    if found:
        print(f"http-egress: {len(found)} finding(s); GET-only reviewed clients only")
        return 1
    print("http-egress: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
