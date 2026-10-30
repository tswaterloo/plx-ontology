"""HTTP-egress gate tests: positive (must flag) and negative (must pass) samples.

``scripts/check_http_egress.py`` is the `http-egress` gate check. These tests prove
it catches aliased Request, getattr/partial, opener.open(url, data), http.client
and friends, scans scripts/ and the development tooling, and that the current tree is clean.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_http_egress as gate  # noqa: E402


def rules(source: str, path: str = "integrations/sample.py") -> set[str]:
    return {rule for _, _, rule in gate.scan_source(source, path)}


# Samples live under a urllib-allowlisted path so only the shape is judged.
OK = "integrations/http.py"

# (source, path, rule): bypasses found in review 5f1391b plus later hardening.
BYPASSES = [
    ("from integrations.http import Request\nRequest(u, data=b'x')",
     "operating/x.py", "non-get-request"),
    ("import integrations.http as h\nh.Request(u, method='POST')",
     "operating/x.py", "non-get-request"),
    ("from integrations.live_source_guard import urllib_sender\n"
     "urllib_sender('PATCH', u, {}, b'x', 5, 10)", "operating/x.py", "sender-reference"),
    ("import integrations.live_source_guard as g\ng.urllib_sender('POST', u, {}, b'x', 5, 1)",
     "operating/x.py", "sender-reference"),
    ("import http\nhttp.client.HTTPSConnection('h')", "operating/x.py", "banned-import"),
    ("import urllib\nurllib.request.urlopen(u)", "operating/x.py", "urllib-import"),
    ("import ssl", "operating/x.py", "banned-import"),
    ("import asyncio\nasyncio.open_connection('h', 443)", "operating/x.py", "banned-import"),
    ("from multiprocessing.connection import Client", "operating/x.py", "banned-import"),
    ("import urllib.request as ur\nur.install_opener(ur.build_opener())",
     "integrations/http.py", "install-opener"),
    ("class C:\n    def fire(self):\n        Request(u, data=b'x', method='POST')",
     "devloop/wake_bridge.py", "non-get-request"),
    ("from urllib.request import build_opener\nclass C:\n    def __init__(s):\n"
     "        s.o = build_opener()\n    def go(s):\n        s.o.open(u, b'x')",
     "integrations/http.py", "opener-body"),
    ("def go(opener, u, d):\n    return opener.open(u, d)", "integrations/http.py",
     "opener-body"),
    ("from urllib.request import Request\nR, x = Request, 1\nR(u, data=b'x')",
     "integrations/http.py", "dynamic-access"),
    ("import urllib.request\nvars(urllib.request)['Request'](u, data=b'x')",
     "integrations/http.py", "dynamic-access"),
    ("from urllib.request import Request\nr = Request(u, method='GET')\n"
     "r.get_method = lambda: 'POST'", "integrations/http.py", "request-mutation"),
    ("import subprocess\nsubprocess.run(['python3','-c','import requests'])",
     "operating/x.py", "shell-http"),
]

POSITIVE = {
    "non-get-request": [
        "from urllib.request import Request\nRequest(u, data=b'x')",
        "from urllib.request import Request\nRequest(u, b'x')",
        "from urllib.request import Request\nRequest(u, method='PATCH')",
        "from urllib.request import Request\nRequest(u, method=verb)",
        "from urllib.request import Request\nRequest(u)",
        "from urllib.request import Request as R\nR(u, method='POST')",
        "import urllib.request as ur\nur.Request(u, method='DELETE')",
        "import urllib.request\nurllib.request.Request(u, data=d, method='GET')",
        "import urllib.request\nR = urllib.request.Request\nR(u, method='PUT')",
        "from urllib import request as rq\nrq.Request(u, **kw)",
    ],
    "opener-body": [
        "from urllib.request import build_opener\nbuild_opener().open(u, data)",
        "from urllib.request import build_opener\no = build_opener()\no.open(u, data=b'x')",
        "import urllib.request as ur\nur.urlopen(u, b'x')",
        "from urllib.request import urlopen\nurlopen(u, data=b'x')",
    ],
    "banned-import": [
        "import http.client",
        "import http.client as hc",
        "from http.client import HTTPSConnection",
        "from http import client",
        "import requests",
        "import httpx",
        "import socket",
        "import smtplib",
        "__import__('http.client')",
        "import importlib\nimportlib.import_module('requests')",
    ],
    "urllib-import": [
        "import urllib.request",
        "from urllib.request import Request",
        "from urllib import request",
    ],
    "dynamic-access": [
        "import urllib.request\ngetattr(urllib.request, 'Request')(u, method='POST')",
        "import urllib.request\ngetattr(urllib.request, name)",
        "from functools import partial\nfrom urllib.request import Request\n"
        "partial(Request, method='POST')",
        "import functools\nimport urllib.request\nfunctools.partial(urllib.request.urlopen)",
        "import importlib\nimportlib.import_module(name)",
    ],
    "request-subclass": [
        "from urllib.request import Request\nclass W(Request):\n    pass",
        "class W:\n    def get_method(self):\n        return 'POST'",
    ],
    "request-mutation": [
        "def f(r):\n    r.method = 'POST'",
        "def f(r):\n    r.data = b'x'",
        "def f(r):\n    r.add_data(b'x')",
    ],
    "shell-http": [
        "import subprocess\nsubprocess.run(['curl', '-X', 'POST', u])",
        "import os\nos.system('wget http://x')",
    ],
    "unguarded-fetch": [
        "import urllib.request as ur\nur.urlretrieve(u, p)",
    ],
}

NEGATIVE = [
    "from urllib.request import Request\nRequest(u, headers=h, method='GET')",
    "import urllib.request as ur\nur.Request(u, method='GET')",
    "from urllib.request import Request, build_opener\n"
    "build_opener().open(Request(u, method='GET'), timeout=5)",
    "from urllib.request import urlopen\nurlopen(u, timeout=5)",
    "from urllib.parse import quote, urlsplit\nurlsplit(u)",
    "from urllib.error import HTTPError",
    "from http.server import HTTPServer",
    "with open(path, 'rb') as f:\n    f.read()",
    "def f(zf):\n    return zf.open(name, 'r')",
    "import subprocess\nsubprocess.run(['git', 'status'])",
    "getattr(obj, 'name')",
    "from integrations.live_source_guard import GuardedTransport",
    "from http.client import IncompleteRead",
    "def f(request: urllib.request.Request):\n    return request",
]


AWAITING_LANE: dict[str, str] = {}

# The sender of operating/messages_planner.py (messages-api-planner, 3f924d62), verbatim.
PLANNER_SENDER = '''
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise TransportError("redirect_refused", args[2])


def post_messages(url, headers, body, timeout):
    if url != API_URL:
        raise TransportError("url_not_allowed")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        response = urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise TransportError(f"http_{status}", status) from None
    with response:
        yield from response
'''


class HttpEgressScanTests(unittest.TestCase):
    def test_positive_samples_are_flagged(self):
        for rule, samples in POSITIVE.items():
            for source in samples:
                # urllib-import is judged on a non-allowlisted path, the rest on an
                # allowlisted one so the import rule does not mask the shape rule.
                path = "integrations/sample.py" if rule == "urllib-import" else OK
                self.assertIn(rule, rules(source, path), source)

    def test_review_bypasses_are_flagged(self):
        for source, path, rule in BYPASSES:
            self.assertIn(rule, rules(source, path), source)

    def test_negative_samples_are_clean(self):
        for source in NEGATIVE:
            if "IncompleteRead" in source:
                continue
            self.assertEqual(rules(source, OK), set(), source)
        self.assertEqual(rules("from http.client import IncompleteRead",
                               "integrations/live_source_guard.py"), set())
        self.assertIn("banned-import", rules("from http.client import IncompleteRead",
                                             "operating/x.py"))
        self.assertIn("banned-import", rules("from http.client import HTTPSConnection",
                                             "integrations/live_source_guard.py"))

    def test_allowlist_is_function_scoped_and_exact(self):
        post = ("from urllib.request import Request\n"
                "def {name}():\n    return Request(u, data=b'x', method='POST')\n")
        self.assertEqual(rules(post.format(name="urllib_sender"),
                               "integrations/live_source_guard.py"), set())
        self.assertEqual(rules(post.format(name="fire"), "devloop/wake_bridge.py"), set())
        # same code elsewhere, or under another function name, is flagged
        self.assertIn("non-get-request", rules(post.format(name="other"),
                                               "integrations/live_source_guard.py"))
        self.assertIn("non-get-request", rules(post.format(name="urllib_sender"),
                                               "integrations/other.py"))
        # qualified name: a method named like an allowed function is not excused
        method = ("from urllib.request import Request\nclass C:\n    def fire(self):\n"
                  "        Request(u, data=b'x', method='POST')\n")
        self.assertIn("non-get-request", rules(method, "devloop/wake_bridge.py"))
        # the allowlist never excuses a banned import
        self.assertIn("banned-import", rules("import http.client",
                                             "integrations/live_source_guard.py"))

    def test_messages_planner_post_is_its_one_sender_and_url(self):
        planner = "operating/messages_planner.py"
        self.assertEqual(rules(PLANNER_SENDER, planner), set())
        # the same sender anywhere else, or under another name in that file, is flagged
        self.assertIn("non-get-request", rules(PLANNER_SENDER, "operating/other.py"))
        self.assertIn("non-get-request", rules(
            PLANNER_SENDER.replace("def post_messages(", "def post_other("), planner))
        # a second network call in another function of that file, even a GET
        for extra in ("def g(u):\n    return urllib.request.urlopen(u)\n",
                      "def g(u):\n    return urllib.request.urlopen("
                      "urllib.request.Request(u, method='GET'))\n",
                      "def g(u, h):\n    return urllib.request.build_opener(h)\n"):
            self.assertIn("outside-sender", rules(PLANNER_SENDER + extra, planner), extra)
        # any other URL in that file, literal, built or formatted
        for extra in ('OTHER = "https://api.anthropic.com/v1/files"\n',
                      'OTHER = "https://" + HOST\n', 'OTHER = f"https://{HOST}/v1"\n',
                      'OTHER = "http://api.anthropic.com/v1/messages"\n',
                      'OTHER = b"https://example.com/"\n'):
            self.assertIn("unlisted-url", rules(PLANNER_SENDER + extra, planner), extra)

    def test_scope_covers_scripts_and_devloop(self):
        self.assertIn("scripts", gate.SCANNED)
        self.assertIn("devloop", gate.SCANNED)
        self.assertIn(("scripts/x.py", "<module>", "banned-import"),
                      gate.scan_source("import requests", "scripts/x.py"))
        self.assertIn(("devloop/x.py", "<module>", "banned-import"),
                      gate.scan_source("import requests", "devloop/x.py"))

    def test_scope_covers_the_app_api(self):
        # operator_api (the web app's API) serves people: it sends nothing to a business
        # system, so a sender there is a finding like anywhere else.
        self.assertIn("operator_api", gate.SCANNED)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for tree in gate.REQUIRED_TREES:
                (root / tree).mkdir()
            (root / "operator_api").mkdir()
            (root / "operator_api" / "client.py").write_text("import requests\n")
            self.assertIn(("operator_api/client.py", "<module>", "banned-import"),
                          gate.scan_report(root)["findings"])

    def test_allowlisted_senders_still_exist_and_are_exercised(self):
        report = gate.scan_report()
        for path, function in gate.ALLOWED - report["excused"]:
            # only an entry allowlisted ahead of its lane may be absent, and only while absent
            self.assertIn(path, AWAITING_LANE, (path, function))
            self.assertFalse((ROOT / path).exists(), f"{path} landed: drop it from AWAITING_LANE")
        for path in gate.URLLIB_FILES - set(AWAITING_LANE):
            self.assertIn("urllib.request", (ROOT / path).read_text(encoding="utf-8"), path)

    def test_urllib_sender_itself_refuses_non_read_sends(self):
        from unittest import mock
        from urllib.parse import urlencode
        from integrations import live_source_guard as guard
        from integrations.plx_hub_guard import MC_HOSTS
        rpc = b'{"jsonrpc":"2.0","method":"tools/call"}'
        tenant = "11111111-2222-3333-4444-555555555555"
        token = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        good = urlencode({"grant_type": "client_credentials", "scope": guard.GRAPH_SCOPE,
                          "client_id": "c", "client_secret": "s"}).encode()
        refused = [
            ("PATCH", "https://graph.microsoft.com/v1.0/me", None),
            ("POST", "https://graph.microsoft.com/v1.0/me/sendMail", b"{}"),
            ("DELETE", "https://graph.microsoft.com/v1.0/me", None),
            ("GET", "https://graph.microsoft.com/v1.0/me", b"x"),
            ("GET", "http://graph.microsoft.com/v1.0/me", None),
            ("POST", token, None),
            ("POST", token, good + b"&extra=1"),
            ("POST", token, good.replace(b"client_credentials", b"password")),
            ("POST", token + "?x=1", good),
            ("POST", "https://login.microsoftonline.com/x/y", good),
            ("POST", "https://example.com/api/cursor/mcp", b'{"jsonrpc":"2.0","method":"x"}'),
            ("POST", f"https://{sorted(MC_HOSTS)[0]}/api/cursor/tasks", rpc),
            ("POST", f"https://{sorted(MC_HOSTS)[0]}/api/cursor/mcp", b"not json"),
            ("POST", "https://example.com/" + tenant + "/oauth2/v2.0/token", good),
        ]
        with mock.patch.object(guard, "build_opener") as opener:
            opener.return_value.open.side_effect = OSError("offline")
            for method, url, body in refused:
                with self.assertRaises(guard.ReadOnlyGuardError, msg=(method, url)):
                    guard.urllib_sender(method, url, {}, body, 5, 10)
            opener.assert_not_called()
            for method, url, body in (("GET", "https://graph.microsoft.com/v1.0/me", None),
                                      ("POST", token, good),
                                      ("POST", f"https://{sorted(MC_HOSTS)[0]}/api/cursor/mcp",
                                       rpc)):
                try:
                    guard.urllib_sender(method, url, {}, body, 5, 10)
                except guard.ReadOnlyGuardError:
                    self.fail(f"valid send refused: {method} {url}")
                except OSError:
                    pass  # reached the (mocked, offline) opener: not refused
            self.assertEqual(opener.call_count, 3)

    def test_messages_planner_sender_posts_to_its_one_url_only(self):
        from unittest import mock
        from operating import messages_planner as mp
        with mock.patch("urllib.request.OpenerDirector.open") as opened:
            for url in ("https://api.anthropic.com/v1/messages?x=1",
                        "http://api.anthropic.com/v1/messages",
                        "https://graph.microsoft.com/v1.0/me/sendMail",
                        "https://api.anthropic.com/v1/files"):
                with self.assertRaises(mp.TransportError, msg=url):
                    list(mp.post_messages(url, {}, b"{}", 1))
            opened.assert_not_called()

    def test_current_tree_is_clean(self):
        self.assertEqual(gate.scan_tree(), set())
        # the live check gate.http_egress holds on this tree's real --json output
        import contextlib, io, json
        from verify import live_verify
        spec = json.loads((ROOT / "devloop/live-checks/gate.http_egress.json").read_text())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gate.main(["--json"])
        document = json.loads(out.getvalue())
        self.assertEqual(live_verify.check_expectations(spec, code, document), [])
        self.assertTrue({"integrations/live_source_guard.py:urllib_sender",
                         "devloop/wake_bridge.py:fire"} <= set(document["allowlisted_senders"]))
        # one finding fails it
        failed = {**document, "status": "findings", "findings": 1,
                  "unlisted_senders": ["operating/x.py:f:non-get-request"]}
        self.assertTrue(live_verify.check_expectations(spec, 1, failed))
        self.assertNotIn("blocked_reason", document)
        # a root without a required tree is never clean: blocked in --json, exit 1 as the gate
        import tempfile
        with tempfile.TemporaryDirectory() as empty:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(gate.main(["--json", empty]), 0)
                self.assertEqual(gate.main([empty]), 1)
        blocked = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(live_verify.resolve(blocked, spec["blocked_when"]),
                         ["scan_tree_missing"])
        self.assertTrue(live_verify.check_expectations(spec, 0, blocked))

    def test_the_sharepoint_graph_writer_is_gone(self):
        for name in ("operating/sharepoint_tag_execution.py",
                     "operating/sharepoint_tag_host.py"):
            self.assertFalse((ROOT / name).exists(), name)
        for top in gate.SCANNED:
            for path in (ROOT / top).rglob("*.py"):
                if "tests" in path.relative_to(ROOT).parts or path == Path(gate.__file__):
                    continue
                text = path.read_text(encoding="utf-8")
                for marker in ("graph_patch", "sharepoint_tag_execution",
                               "SharePointTagPublisher", 'method="PATCH"', "method='PATCH'"):
                    self.assertNotIn(marker, text, f"{path.relative_to(ROOT)}: {marker}")


if __name__ == "__main__":
    unittest.main()
