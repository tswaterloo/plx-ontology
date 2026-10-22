"""The console as an employee starts it: ``python3 -m operator_ui.case_console``.

Starts the real command, through the fixture world's entry point
(``python3 -m operator_ui.tests.case_fixtures``), on a new state directory with the fixture seed (``--fixture
--seed-fixtures --principal staff:operations --port 0``), reads the URL it prints and
uses the landing page over HTTP from 127.0.0.1 only: the page, the session, the objective
box's search, opening an object and running a question family on it. Evidence label:
isolated-fixture / injected-transport; credential-shaped variables are removed from the
child's environment and nothing leaves the machine.
"""

from __future__ import annotations

from http.client import HTTPConnection
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL", re.I)


class CaseConsoleSmokeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="case-console-smoke-")
        self.addCleanup(temp.cleanup)
        env = {name: value for name, value in os.environ.items() if not CREDENTIAL.search(name)}
        env["PYTHONPATH"] = str(ROOT)
        # CI sets no PYTHONUNBUFFERED: the URL must arrive through a block-buffered pipe.
        env.pop("PYTHONUNBUFFERED", None)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "operator_ui.tests.case_fixtures", "--state-dir",
             str(Path(temp.name) / "state"), "--fixture", "--seed-fixtures",
             "--principal", "staff:operations", "--port", "0"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.stop)
        lines: Queue = Queue()
        threading.Thread(target=lambda: [lines.put(line) for line in self.proc.stdout],
                         daemon=True).start()
        printed = []
        while True:
            try:
                line = lines.get(timeout=300)
            except Empty:
                self.fail("the console printed no URL: " + "".join(printed)[-2000:])
            printed.append(line)
            found = re.search(r"Operator console: http://127\.0\.0\.1:(\d+)/#access=(\S+)", line)
            if found:
                self.port, self.token = int(found[1]), found[2]
                return
            if self.proc.poll() is not None and lines.empty():
                self.fail("the console exited: " + "".join(printed)[-2000:])

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=20)
        self.proc.stdout.close()

    def get(self, path, *, auth=True):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=120)
        conn.request("GET", path, headers={"Authorization": "Bearer " + self.token}
                     if auth else {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, response.getheader("Content-Type", ""), body

    def api(self, path):
        status, kind, body = self.get(path)
        self.assertEqual((status, kind.split(";")[0]), (200, "application/json"), body[:300])
        return json.loads(body)

    def test_the_landing_page_finds_opens_and_investigates_for_the_session_reader(self):
        status, _, page = self.get("/", auth=False)
        self.assertEqual(status, 200)
        page = page.decode("utf-8")
        self.assertIn('data-view="objects"', page)
        self.assertIn('<script src="/workspace.js" defer></script>', page)
        for script in ("/app.js", "/workspace.js"):
            self.assertEqual(self.get(script, auth=False)[0], 200)
        self.assertEqual(self.get("/api/session", auth=False)[0], 401)
        session = self.api("/api/session")
        self.assertTrue(session["case_workspace"])
        self.assertTrue(session["identity"]["fixture"])
        found = self.api("/api/objects?q=" + quote("Is PO5447 fully received?", safe=""))
        self.assertTrue(found["evidence"]["fixture"])
        self.assertEqual(found["actor"]["principals"], ["staff:operations"])
        po = next(c["object"]["object_id"] for c in found["find"]["candidates"]
                  if c["object"]["object_type"] == "PurchaseOrder")
        self.assertTrue(found["plans"]["PurchaseOrder"])
        opened = self.api("/api/object?id=" + quote(po, safe=""))
        self.assertEqual((opened["status"], opened["object_id"]), ("read", po))
        self.assertEqual(opened["documents"]["status"], "read")
        family = found["plans"]["PurchaseOrder"][0]["family"]
        answer = self.api(f"/api/investigate?family={family}&root=" + quote(po, safe=""))
        self.assertEqual(answer["investigation"]["root"]["object_id"], po)
        self.assertEqual(answer["business_system_writes"], 0)


if __name__ == "__main__":
    unittest.main()
