"""The hourly self-check's capture-age reader (deploy/ops/guards/self_check.sh): a fresh
store reads as its real age, a store it cannot read is an ERROR(...) naming the cause,
never a bare "?"."""
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "ops" / "guards" / "self_check.sh"


def reader_source() -> str:
    text = SCRIPT.read_text()
    return re.search(r"<<'PY'\n(.*?)\nPY\n", text, re.S).group(1)


class SelfCheckFreshness(unittest.TestCase):
    def run_reader(self, paths: dict[str, str]) -> str:
        src = reader_source()
        for old, new in paths.items():
            self.assertIn(old, src)
            src = src.replace(old, new)
        done = subprocess.run([sys.executable, "-I", "-"], input=src, text=True,
                              capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_fresh_store_reads_its_age_and_an_unreadable_one_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bc = Path(tmp) / "bc.sqlite"
            then = (datetime.now(timezone.utc) - timedelta(minutes=7)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            with sqlite3.connect(bc) as c:
                c.execute("CREATE TABLE runs (updated_at TEXT)")
                c.execute("INSERT INTO runs VALUES (?)", (then,))
            gone = str(Path(tmp) / "missing.sqlite")
            out = self.run_reader({"/data/plx-state/bc-capture/capture.sqlite": str(bc),
                                   "/data/plx-state/mail-capture/capture.db": gone})
        self.assertRegex(out, r"bc [67]m")
        self.assertIn("mail ERROR(OperationalError: ", out)
        self.assertNotIn("?(", out)


if __name__ == "__main__":
    unittest.main()
