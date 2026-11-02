"""scripts/vendor_watch_loop.sh: the vendor-mail pass's runner. One pass (VENDOR_WATCH_ONCE)
with a stub interpreter: the pass gets its state, config and a budget inside the cap, and no
credential-shaped variable reaches it. SYNTHETIC: no store is read. Evidence label:
isolated-fixture."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vendor_watch_loop.sh"


class VendorWatchLoopTest(unittest.TestCase):

    def test_one_pass_gets_its_arguments_and_no_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            seen = tmp / "seen.txt"
            stub = tmp / "python-stub"
            stub.write_text(f"#!/bin/bash\n{{ echo \"$@\"; env; }} > {seen}\n", "utf-8")
            stub.chmod(0o755)
            envfile = tmp / "plx.env"
            envfile.write_text("PLX_EXCLUDED_PEOPLE_OIDS=synthetic-oid\n"
                               "PLX_WORKBENCH_API_KEY=synthetic-secret\n"
                               "MICROSOFT_GRAPH_CLIENT_SECRET=synthetic-secret\n", "utf-8")
            environ = {"PATH": os.environ["PATH"], "VENDOR_WATCH_ONCE": "1",
                       "VENDOR_WATCH_PYTHON": str(stub), "VENDOR_WATCH_CAP": "5400",
                       "SOME_TOKEN": "synthetic-secret"}
            done = subprocess.run(["bash", str(SCRIPT), str(tmp / "state"),
                                   str(tmp / "host.json"), str(envfile)],
                                  env=environ, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("vendor pass exit 0", done.stdout)
            text = seen.read_text("utf-8")
            self.assertIn(f"-m operating.vendor_watch --state-dir {tmp / 'state'} --config "
                          f"{tmp / 'host.json'} --budget-seconds 5100", text)
            self.assertIn("PLX_EXCLUDED_PEOPLE_OIDS=synthetic-oid", text)
            self.assertNotIn("synthetic-secret", text)


    def test_a_stop_file_ends_the_wait_between_passes_within_a_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "state").mkdir()
            stub = tmp / "python-stub"
            # the pass: drops the stop file, as an operator would during the wait
            stub.write_text(f"#!/bin/bash\ntouch {tmp / 'state' / 'vendor-watch.stop'}.later\n"
                            "exit 0\n", "utf-8")
            stub.chmod(0o755)
            environ = {"PATH": os.environ["PATH"], "VENDOR_WATCH_PYTHON": str(stub),
                       "VENDOR_WATCH_INTERVAL": "7200", "VENDOR_WATCH_STEP": "1"}
            loop = subprocess.Popen(["bash", str(SCRIPT), str(tmp / "state"),
                                     str(tmp / "host.json")], env=environ,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 30
                while not (tmp / "state" / "vendor-watch.stop.later").exists():
                    self.assertLess(time.monotonic(), deadline, "the pass never ran")
                    time.sleep(0.2)
                (tmp / "state" / "vendor-watch.stop").touch()
                out, _ = loop.communicate(timeout=20)   # well before the 2 h interval
            finally:
                loop.kill()
            self.assertEqual(loop.returncode, 0)
            self.assertIn("stopped by", out)

if __name__ == "__main__":
    unittest.main()
