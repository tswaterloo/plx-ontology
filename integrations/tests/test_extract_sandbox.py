"""The extraction process's kernel boundary (INT-015, ``integrations.extract_sandbox``):
a process that applied it reads no file outside its allowed directories (the home
directory's credential files, ``/proc`` of another process), opens no socket (TCP or the
PostgreSQL UNIX socket) and still runs its tools. Evidence label: isolated-fixture (this
host's kernel)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from integrations import extract_sandbox

ROOT = Path(__file__).resolve().parents[2]

_PROBE = r"""
import json, os, socket, subprocess, sys
sys.path.insert(0, sys.argv[1])
from integrations import extract_sandbox
state = extract_sandbox.apply(sys.argv[1], mode="required")
out = {"state": state["state"]}
def probe(name, action):
    try:
        action()
        out[name] = "allowed"
    except OSError:
        out[name] = "denied"
probe("secret", lambda: open(sys.argv[2]).read())
probe("proc", lambda: open(f"/proc/{os.getppid()}/environ").read())
probe("tcp", lambda: socket.socket(socket.AF_INET).close())
probe("unix", lambda: socket.socket(socket.AF_UNIX).close())
probe("tmp", lambda: open(os.path.join(sys.argv[3], "w.txt"), "w").write("x"))
probe("repo", lambda: open(os.path.join(sys.argv[1], "integrations", "extract_worker.py")).read())
out["tool"] = subprocess.run(["sh", "-c", "echo ok"], capture_output=True, text=True).stdout
print(json.dumps(out))
"""


@unittest.skipUnless(extract_sandbox.abi() >= extract_sandbox.MIN_ABI,
                     "needs a Linux kernel with Landlock ABI 4+")
class BoundaryTests(unittest.TestCase):
    def test_a_sandboxed_process_reads_no_secret_and_opens_no_socket(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as home, \
                tempfile.TemporaryDirectory() as tmp:
            secret = Path(home) / "plx.env"
            secret.write_text("SECRET=x\n")
            found = subprocess.run([sys.executable, "-c", _PROBE, str(ROOT), str(secret), tmp],
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(found.returncode, 0, found.stderr[-2000:])
            out = json.loads(found.stdout.strip().splitlines()[-1])
        self.assertEqual(out, {"state": "enforced", "secret": "denied", "proc": "denied",
                               "tcp": "denied", "unix": "denied", "tmp": "allowed",
                               "repo": "allowed", "tool": "ok\n"})

    def test_the_mail_capture_extraction_process_runs_inside_the_boundary(self):
        from integrations import capture_mail
        text = b"Order confirmation PO 4711 for 120 cases.\n"
        attachment = {"name": "note.txt", "contentType": "text/plain",
                      "content_type": "text/plain", "size": len(text)}
        with mock.patch.dict(os.environ, {"PLX_EXTRACT_SANDBOX": "required"}):
            extractors = capture_mail._Extractors(1, 120)
        try:
            self.assertEqual(extractors.read(text, attachment, defer_ocr=True,
                                             defer_all=False)[0], "extracted")
            (pid,) = extractors.pids()
            status = Path(f"/proc/{pid}/status").read_text()
        finally:
            extractors.close()
        self.assertIn("NoNewPrivs:\t1", status)
        self.assertIn("Seccomp:\t2", status)


class ModeTests(unittest.TestCase):
    def test_an_unknown_mode_is_required_and_required_without_landlock_refuses(self):
        self.assertEqual(extract_sandbox.mode_from({}), "best_effort")
        self.assertEqual(extract_sandbox.mode_from({"PLX_EXTRACT_SANDBOX": "of"}), "required")
        self.assertEqual(extract_sandbox.mode_from({"PLX_EXTRACT_SANDBOX": "OFF"}), "off")
        with mock.patch.object(extract_sandbox, "abi", lambda: 0):
            with self.assertRaises(RuntimeError):
                extract_sandbox.apply(ROOT, mode="required", memory_bytes=None)
            self.assertEqual(extract_sandbox.apply(ROOT, mode="best_effort",
                                                   memory_bytes=None)["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
