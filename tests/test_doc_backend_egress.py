"""The document backends open no connection of their own (hard rule, ``http-egress`` gate).

markitdown -> magika -> onnxruntime (1.31) posts OneDS telemetry to
``mobile.events.data.microsoft.com`` a few seconds after its first inference
session, from native code (libcurl), unless ``ORT_DISABLE_TELEMETRY=1``. So:

1. ``integrations/doc_convert.py`` is the one module that imports that stack;
2. it turns the telemetry off before the import, over a caller's ``0``;
3. a real conversion, where the package is installed, opens no connection: the
   child's proxy variables point at a local listener (libcurl honours them), so
   a native upload is caught there and never leaves the machine.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from integrations import doc_convert


ROOT = Path(__file__).resolve().parents[1]
STACK = ("markitdown", "magika", "onnxruntime")
IMPORT_SITE = "integrations/doc_convert.py"
# onnxruntime 1.31 uploads ~8.5 s after the first session; wait past that.
UPLOAD_WINDOW_SECONDS = 15
PROXY_VARIABLES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                   "ALL_PROXY", "all_proxy")

# Stands in for onnxruntime 1.31: telemetry unless the variable is "1" when the stack
# is imported and when a session runs.
FAKE_MARKITDOWN = '''
import os, socket
SEEN = os.environ.get("ORT_DISABLE_TELEMETRY")
def _telemetry():
    if os.environ.get("ORT_DISABLE_TELEMETRY") != "1":
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("192.0.2.1", 443))
_telemetry()
class _Result:
    markdown = "Petra PO: PPO-200"
class MarkItDown:
    def __init__(self, enable_plugins):
        pass
    def convert_local(self, path):
        _telemetry()
        return _Result()
'''

CHILD = r'''
import socket, sys, time
attempts = []
def refuse(self, address, *args):
    attempts.append(repr(address))
    raise OSError("network access attempted by a document backend")
socket.socket.connect = refuse
socket.socket.connect_ex = refuse
from integrations import doc_convert
from integrations.tests.office_fixtures import PURCHASE_ORDER_BODY, make_word
backend = doc_convert.installed_backend()
if backend is None:
    print("no-backend")
    sys.exit(3)
text = doc_convert.convert(make_word(PURCHASE_ORDER_BODY), "docx", backend=backend).text
time.sleep(float(sys.argv[1]))
print("PO\\_12345" in text, attempts)
'''


def _env() -> dict[str, str]:
    # Never hand credentials to a child process (no business-system calls here).
    return {key: value for key, value in os.environ.items()
            if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_CONFIG_COUNT")) or
            not any(mark in key for mark in ("KEY", "SECRET", "TOKEN", "PASSWORD"))}


def _python_files():
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if not any(part.startswith(".") or part in {"node_modules", "__pycache__"}
                   for part in relative.parts):
            yield relative, path


def _stack_imports(source: str, filename: str) -> list[str]:
    """Static ``import``/``from`` of the stack, and calls of an importer by name."""
    def named(module: object) -> bool:
        return isinstance(module, str) and module.split(".")[0] in STACK
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module] if not node.level else []
        elif isinstance(node, ast.Call) and node.args:
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else "")
            first = node.args[0]
            modules = ([first.value] if "import" in callee.lower() and
                       isinstance(first, ast.Constant) else [])
        else:
            continue
        found += [f"{filename}:{node.lineno}:{module}" for module in modules if named(module)]
    return found


class DocumentBackendEgressTest(unittest.TestCase):
    def test_only_the_import_site_imports_the_backend_stack(self):
        offenders = []
        for relative, path in _python_files():
            if relative.as_posix() != IMPORT_SITE:
                offenders += _stack_imports(path.read_text(encoding="utf-8"),
                                            relative.as_posix())
        self.assertEqual(offenders, [])
        site = (ROOT / IMPORT_SITE).read_text(encoding="utf-8")
        self.assertEqual([hit.split(":")[-1] for hit in _stack_imports(site, IMPORT_SITE)],
                         ["markitdown"])
        # The rule sees the shapes a bypass would take.
        self.assertEqual(len(_stack_imports(
            "import onnxruntime as rt\nfrom magika.types import X\n"
            "importlib.import_module('markitdown')\n__import__('magika')\n"
            "_importable('onnxruntime.capi')\nfrom . import markitdown\n", "x.py")), 5)

    def test_backend_import_turns_telemetry_off_over_the_callers_setting(self):
        attempts = []

        def refuse(self, address, *args):
            attempts.append(address)
            raise OSError("network access attempted by a document backend")

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "markitdown.py").write_text(FAKE_MARKITDOWN, encoding="utf-8")
            doc_convert._import_backend.cache_clear()
            try:
                with patch.dict(os.environ, {"ORT_DISABLE_TELEMETRY": "0"}), \
                        patch.object(doc_convert, "_REQUIRES", {}), \
                        patch.dict(sys.modules), patch.object(sys, "path", [directory, *sys.path]), \
                        patch.multiple(socket.socket, connect=refuse, connect_ex=refuse):
                    sys.modules.pop("markitdown", None)
                    backend = doc_convert.installed_backend()
                    self.assertIsNotNone(backend)
                    self.assertEqual(sys.modules["markitdown"].SEEN, "1")
                    converted = doc_convert.convert(b"PK fake", "docx", backend=backend)
            finally:
                doc_convert._import_backend.cache_clear()
        self.assertEqual((converted.text, attempts), ("Petra PO: PPO-200", []))

    @unittest.skipUnless(importlib.util.find_spec("markitdown"),
                         "markitdown is not installed (optional)")
    def test_a_real_conversion_opens_no_connection(self):
        if not doc_convert.available("docx"):
            self.skipTest("markitdown's docx extra is not installed: "
                          + "+".join(doc_convert.missing_extras("docx")))
        listener = socket.create_server(("127.0.0.1", 0))
        listener.settimeout(0.2)
        proxy = "http://127.0.0.1:%d" % listener.getsockname()[1]
        caught, done = [], threading.Event()

        def accept():
            while not done.is_set():
                try:
                    connection, _ = listener.accept()
                except OSError:
                    continue
                with connection:
                    connection.settimeout(2)
                    try:
                        caught.append(connection.recv(200))
                    except OSError as error:
                        caught.append(repr(error))

        env = {key: value for key, value in _env().items() if "proxy" not in key.lower()}
        env.update({name: proxy for name in PROXY_VARIABLES},
                   ORT_DISABLE_TELEMETRY="0")
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        try:
            child = subprocess.run([sys.executable, "-c", CHILD, str(UPLOAD_WINDOW_SECONDS)],
                                   cwd=ROOT, env=env, capture_output=True, text=True,
                                   timeout=UPLOAD_WINDOW_SECONDS + 120)
        finally:
            done.set()
            thread.join()
            listener.close()
        if child.returncode == 3 and child.stdout.strip() == "no-backend":
            self.skipTest("markitdown is installed but does not import here")
        self.assertEqual(child.returncode, 0, child.stderr[-2000:])
        self.assertEqual((child.stdout.strip(), caught), ("True []", []))


if __name__ == "__main__":
    unittest.main()
