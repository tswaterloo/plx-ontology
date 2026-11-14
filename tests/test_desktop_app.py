"""The desktop shell's rules (desktop/src/policy.js): which address the window may show, where it
may navigate, what a page may put in a notification. Runs the node:test file when a Node is on the
host; the Electron window itself is not started here (desktop/README.md)."""

from __future__ import annotations

import shutil
import subprocess
import unittest
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE_DIRS = ("/data/tools/node-v22.23.4-linux-x64/bin", "/data/tools/node/bin")


def _node() -> str | None:
    """The host's pinned Node first (PATH's is the distro's Node 18 under the lander)."""
    dirs = ((f"{os.environ['PLX_NODE']}/bin",) if os.environ.get("PLX_NODE") else ()) + NODE_DIRS
    found = next((str(Path(d) / "node") for d in dirs if (Path(d) / "node").exists()), None)
    return found or shutil.which("node")


class DesktopPolicy(unittest.TestCase):
    def test_policy_rules_hold(self) -> None:
        node = _node()
        if node is None:
            self.skipTest("no node on this host")
        run = subprocess.run([node, "--test", "desktop/test/policy.test.js"], cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout[-2000:] + run.stderr[-1000:])

    def test_builds_stay_unsigned_until_a_certificate_exists(self) -> None:
        text = (ROOT / "desktop" / "electron-builder.yml").read_text()
        # an unsigned config must say so, so nobody mistakes an installer for a signed one
        self.assertIn("UNSIGNED", text)
        self.assertIn("identity: null", text)


if __name__ == "__main__":
    unittest.main()
