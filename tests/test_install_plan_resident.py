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


def rounds_function() -> str:
    text = SCRIPT.read_text()
    return text[text.index("run_rounds() {"):text.index("\nfor phase in")]


class FillClosedRounds(unittest.TestCase):
    """fill-closed spreads its rounds over PLX_PLAN_HOSTS and loads complete files of an earlier run."""

    def setup(self, d: Path, n_rounds: int):
        parts = d / "parts"
        parts.mkdir()
        for k in range(n_rounds):
            (parts / f"r{k:02d}").write_text(f"id{k}a\nid{k}b\n")
        log = d / "log"
        batch = d / "batch.sh"       # stands in for plan_batch.sh: records its host, writes whole files
        batch.write_text(
            '#!/bin/bash\nwhile [ $# -gt 0 ]; do case $1 in --out) o=$2;; --host) h=$2;; --ids) i=$2;; esac; shift; done\n'
            f'echo "batch $(basename $i) $h" >> {log}\nsleep 0.3\nmkdir -p "$o"\n'
            'python3 - "$o" "$i" <<"PY"\nimport json,sys\no,i=sys.argv[1:]\n'
            'ids=open(i).read().split()\nfor s in range(8):\n'
            ' with open(f"{o}/shard-{s}.jsonl","w") as f:\n'
            '  f.write(json.dumps({"stamps":{},"shard":f"{s}/8"})+"\\n")\n'
            '  for r in ids[s%len(ids)::8] if s<len(ids) else []:\n'
            '   for fam in ("late_po","people_on_record","landed_cost","po_trail"):\n'
            '    f.write(json.dumps({"record_id":r,"family":fam})+"\\n")\nPY\n')
        batch.chmod(0o755)
        return parts, log, batch

    def run_it(self, d: Path, parts: Path, batch: Path, hosts: str):
        script = ("set -u; repo=%s; envf=/dev/null; sha=x; shards_per_round=8; " % SCRIPT.parents[2]
                  + rounds_function() + '\nrun_rounds "$1" "$2"')
        env = {"PATH": os.environ["PATH"], "HOME": str(d), "PLX_PLAN_HOSTS": hosts,
               "PLAN_BATCH_CMD": str(batch), "PLAN_UPSERT_CMD": "true"}
        return subprocess.run(["bash", "-c", script, "x", str(parts), str(d / "out")], env=env,
                              capture_output=True, text=True)

    def test_rounds_spread_over_both_hosts_and_a_complete_round_is_not_recomputed(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            parts, log, batch = self.setup(d, 4)
            got = self.run_it(d, parts, batch, "s7d3d sb4ec")
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            calls = sorted(log.read_text().split("\n")[:-1])
            self.assertEqual(calls, ["batch r00 s7d3d", "batch r01 sb4ec", "batch r02 s7d3d", "batch r03 sb4ec"])
            log.unlink()
            again = self.run_it(d, parts, batch, "s7d3d sb4ec")   # all four complete now
            self.assertEqual(again.returncode, 0)
            self.assertFalse(log.exists())
            self.assertEqual(again.stdout.count("not computed again"), 4)

    def test_a_cut_round_is_computed_again_and_a_failed_batch_fails_the_phase(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            parts, log, batch = self.setup(d, 2)
            self.run_it(d, parts, batch, "s7d3d")
            shard = d / "out" / "closed-r01" / "shard-0.jsonl"
            shard.write_text(shard.read_text()[:-20])        # killed mid-line
            self.run_it(d, parts, batch, "s7d3d")
            self.assertEqual(log.read_text().count("batch r01"), 2)
            self.assertEqual(log.read_text().count("batch r00"), 1)
            batch.write_text("#!/bin/bash\nexit 1\n")
            (d / "parts" / "r02").write_text("zz\n")
            bad = self.run_it(d, parts, batch, "s7d3d")
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("r02", bad.stdout)


if __name__ == "__main__":
    unittest.main()
