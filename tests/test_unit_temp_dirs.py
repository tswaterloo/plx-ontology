"""The services that build or fold SQLite stores (the ingest build, the delta cycle and its
fold) keep SQLite's and Python's temp files on /data: on 2026-11-06 a build's SQLite temp
files filled the root disk. Reads the committed unit files only."""

from __future__ import annotations

from pathlib import Path
import unittest

UNITS = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
BUILDERS = ("plx-ingest", "plx-delta", "plx-delta-compact")


class UnitTempDirTests(unittest.TestCase):
    def test_every_store_builder_keeps_temp_files_on_data(self):
        for name in BUILDERS:
            env = {}
            for line in (UNITS / f"{name}.service").read_text(encoding="utf-8").splitlines():
                if line.startswith("Environment="):
                    key, _, value = line[len("Environment="):].partition("=")
                    env[key] = value
            with self.subTest(unit=name):
                for key in ("SQLITE_TMPDIR", "TMPDIR"):
                    self.assertTrue(env.get(key, "").startswith("/data/"), key)


if __name__ == "__main__":
    unittest.main()
