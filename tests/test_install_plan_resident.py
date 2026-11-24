"""deploy/ops/install_plan_resident.sh: the env phase takes every id-valued setting from the operator's
own env at install time and never from the repo; a missing one stops the install by name only."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "ops" / "install_plan_resident.sh"
GUID = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
IDS = ("PLX_BATCH_READER", "PLX_AUTOPILOT_BC_COMPANY_ID", "PLX_AUTOPILOT_BC_COMPANY_IDS",
       "PLX_EXCLUDED_PEOPLE_OIDS", "PLX_FULL_READERS")
# made-up values (not ids of anything real)
FAKE = {n: f"{i:08x}-0000-4000-8000-{i:012x}" for i, n in enumerate(IDS, 1)}


def functions() -> str:
    text = SCRIPT.read_text()
    return text[text.index("ID_SETTINGS="):text.index("\nwrite_env() {")]


def fill(source: str, env: dict) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "plx.env")
        src.write_text(source)
        body = "".join(f"{n}=@{n}@\n" for n in IDS) + "PGUSER=plx_api\n"
        run_env = {"PATH": os.environ["PATH"], "ID_SOURCE_FILES": str(src), "HOME": d, **env}
        return subprocess.run(["bash", "-c", functions() + "\nfill_ids"], input=body, env=run_env,
                              capture_output=True, text=True)


class EnvPhaseTest(unittest.TestCase):
    def test_no_id_is_written_in_the_repo(self):
        self.assertEqual(GUID.findall(SCRIPT.read_text()), [])

    def test_values_come_from_the_operators_env(self):
        got = fill("".join(f"{n}={v}\n" for n, v in FAKE.items()), {})
        self.assertEqual(got.returncode, 0, "env phase failed (values masked)")
        for n, v in FAKE.items():
            self.assertIn(f"{n}={v}\n", got.stdout)
        self.assertIn("PGUSER=plx_api", got.stdout)   # the non-secret settings stay as written

    def test_a_missing_value_stops_by_name_and_prints_no_value(self):
        source = "".join(f"{n}={v}\n" for n, v in FAKE.items() if n != "PLX_FULL_READERS")
        got = fill(source, {})
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("PLX_FULL_READERS", got.stderr)
        self.assertEqual(GUID.findall(got.stdout + got.stderr), [])
        self.assertNotIn("@PLX_", got.stdout)


if __name__ == "__main__":
    unittest.main()
