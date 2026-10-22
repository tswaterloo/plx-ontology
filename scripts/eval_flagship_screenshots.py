"""Regenerate the flagship-walkthrough screenshots from the isolated fixture console.

    python3 scripts/eval_flagship_screenshots.py [--out docs/flagship-walkthrough]

Starts ``python3 -m operator_ui.tests.case_fixtures --fixture --seed-fixtures --principal
staff:operations --port 0`` on a new temporary state directory (synthetic data, no network),
then runs ``scripts/flagship_screenshots.js`` under Node Playwright / the preinstalled
Chromium (PLAYWRIGHT_BROWSERS_PATH, default /opt/pw-browsers; nothing is installed). The
access URL goes to the browser by environment only; credential-shaped variables are removed
from both children. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL", re.I)
BROWSERS = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/opt/pw-browsers"
NODE_PATHS = [p for p in (os.environ.get("NODE_PATH", "").split(os.pathsep)
                          + ["/opt/node-tools/node_modules"]) if p]


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not CREDENTIAL.search(k)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "flagship-walkthrough")
    parser.add_argument("--explore", action="store_true", help="also print the page text")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flagship-shots-") as temp:
        env = {**clean_env(), "PYTHONPATH": str(ROOT)}
        env.pop("PYTHONUNBUFFERED", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "operator_ui.tests.case_fixtures", "--state-dir",
             str(Path(temp) / "state"), "--fixture", "--seed-fixtures",
             "--principal", "staff:operations", "--port", "0"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines: Queue = Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()
        try:
            url = None
            while url is None:
                try:
                    line = lines.get(timeout=300)
                except Empty:
                    raise SystemExit("the fixture console printed no URL") from None
                found = re.search(r"Operator console: (http://127\.0\.0\.1:\d+/#access=\S+)", line)
                if found:
                    url = found[1]
                elif proc.poll() is not None and lines.empty():
                    raise SystemExit("the fixture console exited before serving")
            run = subprocess.run(
                ["node", str(ROOT / "scripts" / "flagship_screenshots.js")],
                capture_output=True, text=True, timeout=900,
                env={**clean_env(), "NODE_PATH": os.pathsep.join(NODE_PATHS),
                     "PLAYWRIGHT_BROWSERS_PATH": BROWSERS, "CONSOLE_URL": url,
                     "OUT_DIR": str(args.out), "EXPLORE": "1" if args.explore else "0"})
            token = url.split("#access=", 1)[1]
            sys.stdout.write(run.stdout.replace(token, "<token>"))
            if run.returncode:
                sys.stderr.write(run.stderr.replace(token, "<token>")[-3000:])
                return run.returncode
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc.stdout.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
