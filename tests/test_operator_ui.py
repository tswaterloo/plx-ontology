from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from operator_ui.server import make_server  # noqa: E402


# deletion-8 retired the UnifiedPlatform capability routes; deletion-18 the injected-service
# views and the approve/execute actions (approval lives in Mission Control, never here).
RETIRED_GET = ("/api/engineering", "/api/episodes", "/api/objectives", "/api/campaigns",
               "/api/work-inbox", "/api/autonomy-runs", "/api/business-watches",
               "/api/streams", "/api/reconciliation-inbox", "/api/transactions?q=SO-1",
               "/api/objective/objective-1", "/api/business-workspace/x",
               "/api/search?q=progress", "/api/replay", "/api/overview", "/api/findings",
               "/api/recover", "/api/entity/item-1", "/api/workflow/f-1")
RETIRED_POST = ("/api/investigate", "/api/objectives", "/api/campaigns",
                "/api/autonomy-runs", "/api/business-watches", "/api/goal-intake",
                "/api/streams/poll-due", "/api/replica/named", "/api/approve/p-1",
                "/api/execute/p-1")


class ConsoleHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, method: str, path: str, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        result = (response.status, response.getheader("Content-Type"), payload)
        conn.close()
        return result

    def test_static_ui_loads(self):
        status, content_type, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"Cases", data)
        self.assertNotIn(b"Coordinated campaigns", data)

    def test_loopback_only(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            make_server(host="0.0.0.0")

    def test_retired_routes_are_not_served(self):
        session = json.loads(self.request("GET", "/api/session")[2])
        self.assertEqual(session["mode"], "local-isolated")
        self.assertFalse(session["case_workspace"])
        for path in RETIRED_GET:
            status, _, data = self.request("GET", path)
            self.assertEqual((status, json.loads(data)), (404, {"error": "route_not_found"}), path)
        headers = {"Content-Type": "application/json", "X-Operator-Token": session["csrf_token"]}
        for path in RETIRED_POST:
            body = json.dumps({"actor": "a", "evidence": {"reference": "r"}})
            status, _, data = self.request("POST", path, body, headers)
            self.assertEqual((status, json.loads(data)), (404, {"error": "route_not_found"}), path)


if __name__ == "__main__":
    unittest.main()
