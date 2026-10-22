"""Every route the console UI calls is served by ``operator_ui/server.py``.

A static scan of the JavaScript the console page loads (the ``<script src>`` files of
``index.html``): every string or template literal that starts with ``/api/`` is a fetch
target. Each target is requested (GET, then POST) from a real loopback server (``make_server`` with stub
workspaces, so every route family is configured); a target the server does not route
answers ``404 route_not_found``, any served route answers something else (its own
validation or the stub's answer). No network beyond 127.0.0.1, no business data.
"""

from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import re
import threading
import unittest
from urllib.parse import urlsplit

from operating import session_identity
from operator_ui import server as console

# A literal starting at /api/ up to its quote, query or whitespace; ${...} placeholders of
# a template literal stand for one path segment.
LITERAL = re.compile(r"""["'`](/api/[^"'`?\s]*)""")
PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


def served_scripts() -> dict[str, str]:
    """The scripts ``index.html`` loads, by name."""
    html = (console.HERE / "index.html").read_text("utf-8")
    names = re.findall(r'<script src="/([^"]+)"', html)
    return {name: (console.HERE / name).read_text("utf-8") for name in names}


def api_targets(js_text: str) -> set[str]:
    out = set()
    for match in LITERAL.finditer(js_text):
        path = PLACEHOLDER.sub("x", match.group(1))
        out.add(path + "x" if path.endswith("/") else path)
    return out


class _Stub:
    """Answers every workspace call with an empty view (a PDF for the original route)."""

    def __init__(self, actor):
        self.actor = actor

    def original_pdf(self, handle):
        return {"content": b"%PDF-1.4 stub"}

    def __getattr__(self, name):
        return lambda *args, **kwargs: {}


class RouteCoverageTest(unittest.TestCase):
    def setUp(self):
        session = session_identity.fixture_session(
            "plx", {"staff:operations"}, channel=session_identity.CHANNEL_CONSOLE)
        server = console.make_server(case_workspace=_Stub(session.actor),
                                     session=session, object_workspace=_Stub(session.actor))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10),
                                 server.server_close()))
        self.port, self.token = server.server_address[1], server.operator_access_token

    def not_routed(self, method, target) -> bool:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, target, body=b"{}" if method == "POST" else None,
                     headers={"Authorization": "Bearer " + self.token,
                              "Content-Type": "application/json"})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status == 404 and json.loads(body).get("error") == "route_not_found"

    def unserved(self, targets) -> list[str]:
        missing = []
        for target in sorted(targets):
            if all(self.not_routed(method, target) for method in ("GET", "POST")):
                missing.append(urlsplit(target).path)
        return missing

    def test_every_route_the_ui_calls_is_served(self):
        scripts = served_scripts()
        self.assertEqual(sorted(scripts), ["app.js", "link_confirm.js", "workspace.js",
                                           "workspace_actions.js"])
        targets = set().union(*(api_targets(text) for text in scripts.values()))
        # The scan sees the routes both views depend on.
        self.assertTrue({"/api/session", "/api/cases", "/api/objects", "/api/object",
                         "/api/object-source", "/api/investigate", "/api/object-review",
                         "/api/object-follow-up", "/api/object-actions",
                         "/api/object-action", "/api/link-confirm"} <= targets,
                        sorted(targets))
        self.assertEqual(self.unserved(targets), [])

    def test_an_unserved_route_in_the_ui_is_reported(self):
        legacy = 'api("/api/objectives"); api(`/api/transaction/${encodeURIComponent(id)}`);'
        self.assertEqual(api_targets(legacy), {"/api/objectives", "/api/transaction/x"})
        self.assertEqual(self.unserved(api_targets(legacy)),
                         ["/api/objectives", "/api/transaction/x"])


if __name__ == "__main__":
    unittest.main()
