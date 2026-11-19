"""tools/run_cache.py and deploy/spark/spark_test.sh's use of it (isolated fixture: a temporary
repository, and two stand-in Spark homes reached through a stand-in ssh/scp that runs the real
remote script and the real tools/main_ci.py locally). A result is reused only on the identical
tree: a changed dependency runs again, a FAIL is never reused, and a report that is not a
complete main_ci run records nothing."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import run_cache  # noqa: E402

SPARK_TEST = ROOT / "deploy" / "spark" / "spark_test.sh"
TREE = "a" * 40
# ssh -F cfg [-o x]... <host> <command>: the command runs in that host's own home
FAKE_SSH = """#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -F|-o|-p) shift 2 ;; -*) shift ;; *) break ;; esac; done
h=$1; shift; export HOME="$FAKE_SPARKS/$h"; echo "$h $*" >> "$FAKE_SPARKS/ssh.log"
# FAKE_PROBE stands in for the host's "cores load1 free-GB user-scope" answer
case "$*" in *loadavg*) if [ -n "${FAKE_PROBE+x}" ]; then echo "$FAKE_PROBE"; exit 0; fi ;; esac
cd "$HOME" && exec bash -c "$*"
"""
# systemd-run --user --scope [-q] [-p X]... [--] <command>: runs the command as is
FAKE_SYSTEMD_RUN = """#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in --) shift; break ;; -p) shift 2 ;; -*) shift ;; *) break ;; esac; done
exec "$@"
"""
FAKE_SCP = """#!/bin/bash
args=(); while [ $# -gt 0 ]; do case "$1" in -F) shift 2 ;; -*) shift ;; *) args+=("$1"); shift ;; esac; done
dst=${args[-1]}; cp "${args[0]}" "$FAKE_SPARKS/${dst%%:*}/${dst#*:}"
"""
STUB_OK = "#!/bin/sh\nexit 0\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           *args], check=True, text=True, capture_output=True).stdout.strip()


def report(*lines: str) -> str:
    """A main_ci report: result lines, then the summary."""
    fails = sum(1 for line in lines if line.startswith("FAIL"))
    return "\n".join([*lines, f"{len(lines)} modules: {len(lines) - fails} pass, 0 known red, "
                      f"{fails} FAIL; 3 s with 2 workers"]) + "\n"


class SparkRunCacheTests(unittest.TestCase):
    """Spark_test.sh -> stand-in hosts -> spark_test_remote.sh -> main_ci."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        files = {
            "tools/__init__.py": "", "pkg/__init__.py": "", "pkg/tests/__init__.py": "",
            "tools/smoke-tests.txt": "pkg.tests.test_smoke\n",
            "pkg/dep.py": "VALUE = 1\n",
            "pkg/tests/test_a.py": "import unittest\nfrom pkg import dep\n\n\nclass A(unittest.TestCase):\n"
                                   "    def test_value(self):\n        self.assertEqual(dep.VALUE, 1)\n",
            "pkg/tests/test_b.py": "import unittest\n\n\nclass B(unittest.TestCase):\n"
                                   "    def test_red(self):\n        self.fail('red')\n",
            "pkg/tests/test_smoke.py": "import unittest\n\n\nclass S(unittest.TestCase):\n"
                                       "    def test_ok(self):\n        pass\n",
        }
        for name, text in files.items():
            Path(self.repo, name).parent.mkdir(parents=True, exist_ok=True)
            Path(self.repo, name).write_text(text)
        shutil.copy(ROOT / "tools" / "main_ci.py", self.repo / "tools" / "main_ci.py")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "one")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, text in {"ssh": FAKE_SSH, "scp": FAKE_SCP, "systemd-run": FAKE_SYSTEMD_RUN}.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        for host in ("s7d3d", "sb4ec"):
            tests = self.tmp / "sparks" / host / "plx-tests"
            (tests / "pg" / "bin").mkdir(parents=True)
            (tests / "venv" / "bin").mkdir(parents=True)
            for stub in ("pg_ctl", "psql"):
                (tests / "pg" / "bin" / stub).write_text(STUB_OK)
                (tests / "pg" / "bin" / stub).chmod(0o755)
            os.symlink(sys.executable, tests / "venv" / "bin" / "python3")
            subprocess.run(["git", "init", "-q", "--bare", str(tests / "repo.git")], check=True)
        self.cache = self.tmp / "cache"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("SPARK_", "PLX_", "LAND_"))}
        self.env.update({"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_SPARKS": str(self.tmp / "sparks"),
                         "LAND_REPO": str(self.repo), "SPARK_LOGS": str(self.tmp / "logs"),
                         "SPARK_SLOT_DIR": str(self.tmp / "slots"), "PLX_RUN_CACHE": str(self.cache),
                         "PLX_SPARK_SSH_CONFIG": str(self.tmp / "ssh_config"), "SPARK_JOBS": "2"})
        (self.tmp / "slots").mkdir()

    def spark(self, sha: str, *modules: str) -> tuple[int, str, str]:
        args = [a for m in modules for a in ("-t", m)]
        run = subprocess.run(["bash", str(SPARK_TEST), sha, *args], env=self.env, text=True,
                             capture_output=True, timeout=300)
        log = run.stdout.rsplit("log ", 1)[-1].strip()
        return run.returncode, run.stdout, Path(log).read_text() if Path(log).is_file() else ""

    def test_a_tree_passes_once_and_a_changed_dependency_runs_again(self) -> None:
        one = git(self.repo, "rev-parse", "HEAD")
        code, out, log = self.spark(one, "pkg.tests.test_a", "pkg.tests.test_b")
        self.assertEqual(code, 1, out + log)
        self.assertIn("2 modules: 1 pass, 0 known red, 1 FAIL", out)
        self.assertNotIn("test_smoke", log)
        self.assertNotIn("cached pass", log)

        code, out, log = self.spark(one, "pkg.tests.test_a", "pkg.tests.test_b")
        self.assertEqual(code, 1, out + log)          # the FAIL ran again and still fails
        self.assertIn("2 modules: 1 pass, 0 known red, 1 FAIL", out)
        self.assertIn("1 run, 1 cached", out)
        self.assertRegex(log, r"(?m)^cached pass +0\.0s pkg\.tests\.test_a$")
        self.assertRegex(log, r"(?m)^FAIL +[0-9.]+s pkg\.tests\.test_b$")

        # the dependency of test_a changes: a new tree, so test_a runs (and now fails)
        Path(self.repo, "pkg", "dep.py").write_text("VALUE = 2\n")
        git(self.repo, "commit", "-qam", "two")
        two = git(self.repo, "rev-parse", "HEAD")
        code, out, log = self.spark(two, "pkg.tests.test_a")
        self.assertEqual(code, 1, out + log)
        self.assertNotIn("cached pass", log)
        self.assertRegex(log, r"(?m)^FAIL +[0-9.]+s pkg\.tests\.test_a$")

        known = subprocess.run([sys.executable, str(ROOT / "tools" / "run_cache.py"), "--cache", str(self.cache),
                                "base-known", "--repo", str(self.repo), "--base", one,
                                "pkg.tests.test_a", "pkg.tests.test_b", "pkg.tests.test_c"],
                               text=True, capture_output=True, check=True).stdout.splitlines()
        self.assertTrue(known[0].startswith("pkg.tests.test_a: green on base"), known)
        self.assertTrue(known[1].startswith("pkg.tests.test_b: red on base"), known)
        self.assertTrue(known[2].startswith("pkg.tests.test_c: not run on base"), known)

        self.env["SPARK_CACHE"] = "0"                 # asked to run everything: nothing is skipped
        code, out, log = self.spark(one, "pkg.tests.test_a")
        self.assertEqual(code, 0, out + log)
        self.assertNotIn("cached pass", log)


    def remote_run(self, sha: str, *, lander: bool = False, **env: str) -> str:
        """The command the host was given to run the tests (``spark_test_remote.sh``)."""
        ssh_log = self.tmp / "sparks" / "ssh.log"
        ssh_log.unlink(missing_ok=True)
        ids = self.tmp / "ids.txt"
        ids.write_text("pkg.tests.test_a\n")
        args = ["--ids", str(ids)] if lander else ["-t", "pkg.tests.test_a"]
        run = subprocess.run(["bash", str(SPARK_TEST), sha, *args], text=True, capture_output=True,
                             timeout=300, env={**self.env, "SPARK_HOST": "s7d3d", "SPARK_CACHE": "0", **env})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        got = [line for line in ssh_log.read_text().splitlines() if f"spark_test_remote.sh {sha} " in line]
        self.assertEqual(len(got), 1, got)
        return got[0]

    def test_the_lander_outweighs_a_lane_and_no_run_is_niced_pinned_or_capped(self) -> None:
        """11-18 owner: no nice, no CPU pins or caps; the lander's priority is its CPU weight;
        workers come from the host's free cores, bounded only by its free memory."""
        del self.env["SPARK_JOBS"]
        sha = git(self.repo, "rev-parse", "HEAD")
        busy_host = "20 3.4 60 1"                      # 20 cores, ~3 busy, 60 GB free, user scopes
        lane = self.remote_run(sha, FAKE_PROBE=busy_host)
        lander = self.remote_run(sha, lander=True, FAKE_PROBE=busy_host)
        for cmd in (lane, lander):
            self.assertNotRegex(cmd, r"\bnice\b|taskset", cmd)
        self.assertIn("systemd-run --user --scope -q -p CPUWeight=100 -p MemoryMax=44G -- bash", lane)
        self.assertIn(f"spark_test_remote.sh {sha} 17 ", lane)        # the idle cores
        self.assertIn("-p CPUWeight=1000 -p MemoryMax=44G -- bash", lander)
        self.assertIn(f"spark_test_remote.sh {sha} 20 ", lander)      # every core
        # little free memory bounds both (the OOM guard): (24 - 16 reserve) / 2 GB a worker
        self.assertIn("MemoryMax=8G -- bash ~/plx-tests/", self.remote_run(sha, lander=True, FAKE_PROBE="20 0.0 24 1"))
        self.assertIn(f"{sha} 4 ", self.remote_run(sha, lander=True, FAKE_PROBE="20 0.0 24 1"))

    def test_lanes_running_together_share_the_free_memory(self) -> None:
        """Each takes
        its share, so together they never claim it several
        times; the lander keeps the whole of it."""
        import fcntl
        del self.env["SPARK_JOBS"]
        sha = git(self.repo, "rev-parse", "HEAD")
        slots = self.tmp / "slots"
        slots.mkdir(exist_ok=True)
        held = []
        for n in (1, 2):                               # two other modules hold slots 1 and 2
            f = open(slots / f"spark-slot.{n}.lock", "w")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(f)
        try:
            lane = self.remote_run(sha, FAKE_PROBE="20 1.0 76 1")
            lander = self.remote_run(sha, lander=True, FAKE_PROBE="20 1.0 76 1")
        finally:
            for f in held:
                f.close()
        self.assertIn("-p CPUWeight=100 -p MemoryMax=20G -- bash", lane)
        self.assertIn(f"spark_test_remote.sh {sha} 10 ", lane)        # 20 GB / 2 GB a worker
        self.assertIn("-p CPUWeight=1000 -p MemoryMax=60G -- bash", lander)
        self.assertIn(f"spark_test_remote.sh {sha} 20 ", lander)

    def test_an_unreadable_host_or_bad_setting_fails_closed_to_a_small_unscoped_run(self) -> None:
        del self.env["SPARK_JOBS"]
        sha = git(self.repo, "rev-parse", "HEAD")
        for probe in ("", "garbage", "20 x 60 1", "0 1.0 60 1", "20 1.0 60 yes"):
            with self.subTest(probe=probe):
                cmd = self.remote_run(sha, lander=True, FAKE_PROBE=probe)
                self.assertNotIn("systemd-run", cmd)
                self.assertIn(f"spark_test_remote.sh {sha} 4 ", cmd)  # never "every core"
        # no user scope on the host: the run goes unscoped, still unthrottled
        cmd = self.remote_run(sha, FAKE_PROBE="20 1.0 60 0")
        self.assertNotIn("systemd-run", cmd)
        self.assertIn(f"spark_test_remote.sh {sha} 19 ", cmd)
        # nonsense settings fall back to their defaults instead of breaking the arithmetic
        cmd = self.remote_run(sha, lander=True, FAKE_PROBE="20 0.0 60 1", SPARK_WORKER_GB="0",
                              SPARK_MEM_RESERVE_GB="x", SPARK_JOBS="many", SPARK_LANDER_WEIGHT="-5")
        self.assertIn("-p CPUWeight=1000 -p MemoryMax=44G -- bash", cmd)
        self.assertIn(f"spark_test_remote.sh {sha} 20 ", cmd)


class ResultRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        self.log = self.cache / "host.log"

    def test_only_a_complete_report_records_and_failure_tails_are_never_read(self) -> None:
        self.log.write_text("pass    1.0s pkg.tests.test_a\n")            # no summary: cut short
        self.assertEqual(run_cache.record(self.cache, TREE, "spark", [self.log]), 0)
        self.log.write_text(report("pass    1.0s pkg.tests.test_a").replace("1 modules", "2 modules"))
        self.assertEqual(run_cache.record(self.cache, TREE, "spark", [self.log]), 0)   # count mismatch
        forged = report("FAIL    1.0s pkg.tests.test_b").replace(
            "1 modules", "::error::pkg.tests.test_b failed\npass    1.0s pkg.tests.test_c\n1 modules")
        self.log.write_text(forged)                   # a test printing a result line in its tail
        self.assertEqual(run_cache.record(self.cache, TREE, "spark", [self.log]), 1)
        self.assertEqual(run_cache.passed(self.cache, TREE, "spark"), set())

    def test_a_pass_is_reused_only_on_its_runner_kind_unmixed_and_fresh(self) -> None:
        self.log.write_text(report("pass    1.0s pkg.tests.test_a", "pass    1.0s pkg.tests.test_f.Slow",
                                   "flaky, passed alone    2.0s pkg.tests.test_g"))
        run_cache.record(self.cache, TREE, "spark", [self.log])
        self.assertEqual(run_cache.passed(self.cache, TREE, "local:box"), set())
        self.assertEqual(run_cache.passed(self.cache, TREE, "spark", now=time.time() + 25 * 3600), set())
        done = run_cache.passed(self.cache, TREE, "spark")
        self.assertEqual(done, {"pkg.tests.test_a", "pkg.tests.test_f.Slow", "pkg.tests.test_g"})
        # a module covers its classes; one class never covers its module
        self.assertTrue(run_cache.covered("pkg.tests.test_a.SomeTests", done))
        self.assertFalse(run_cache.covered("pkg.tests.test_f", done))
        self.log.write_text(report("FAIL    1.0s pkg.tests.test_g"))  # the same tree failed once too
        run_cache.record(self.cache, TREE, "spark", [self.log])
        self.assertNotIn("pkg.tests.test_g", run_cache.passed(self.cache, TREE, "spark"))

    def test_concurrent_writers_lose_and_tear_no_line(self) -> None:
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from tools import run_cache\n"
                "for k in range(100): run_cache.append(Path(sys.argv[2]), sys.argv[3], "
                "[{'id': f'm{sys.argv[4]}_{k}', 'ok': True, 'at': 1, 'env': 'spark', 'pad': 'x' * 3000}])\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(self.cache), TREE, str(n)])
                 for n in range(8)]
        self.assertEqual([p.wait() for p in procs], [0] * 8)
        lines = run_cache._results_file(self.cache, TREE).read_text().splitlines()
        self.assertEqual(len(lines), 800)
        self.assertEqual(len({json.loads(line)["id"] for line in lines}), 800)

    def test_split_balances_by_time_and_reports_cached_ids_once(self) -> None:
        ids = ["m.a", "m.b", "m.c", "m.d.Slow", "m.d.Fast"]
        timings = {"m.a": 10, "m.b": 10, "m.c": 1, "m.d": 4}
        shares = [run_cache.split(ids, {"m.c", "m.d"}, (i, 2), timings) for i in range(2)]
        self.assertEqual(sorted(shares[0][0] + shares[1][0]), ["m.a", "m.b"])
        self.assertEqual((sorted(shares[0][1]), shares[1][1]), (["m.c", "m.d.Fast", "m.d.Slow"], []))
        # a class without its own seconds never weighs its whole module's
        timings = {"m.e": 900, "m.a": 100, "m.b": 100}
        shares = [run_cache.split(["m.e.Quick", "m.a", "m.b"], set(), (i, 2), timings)[0] for i in range(2)]
        self.assertIn(["m.a", "m.e.Quick"], [sorted(s) for s in shares])

class AffectedCacheTests(unittest.TestCase):
    """A stand-in selector counts its runs and prints what the tree holds."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        (self.repo / "tools").mkdir(parents=True)
        (self.repo / "tools" / "affected_tests.py").write_text(
            "import os, pathlib, sys, time\n"
            "with open(os.environ['COUNT'], 'a') as f: f.write('x')\n"
            "time.sleep(0.5)\n"
            "root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1])\n"
            "print((root / 'mods.txt').read_text().strip())\n")
        (self.repo / "mods.txt").write_text("pkg.tests.test_a\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "one")
        self.count = self.tmp / "count"
        self.count.write_text("")
        self.cache = self.tmp / "cache"

    def select(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(ROOT / "tools" / "run_cache.py"), "--cache", str(self.cache),
                                 "affected", "--base", "HEAD", *args], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env={**os.environ, "COUNT": str(self.count)})

    def test_one_computation_per_key_and_never_for_an_uncommitted_tree(self) -> None:
        callers = [self.select("--repo", str(self.repo), "--sha", "HEAD") for _ in range(4)]
        callers.append(self.select("--root", str(self.repo)))
        outs = [c.communicate()[0] for c in callers]
        self.assertEqual(outs, ["pkg.tests.test_a\n"] * 5)
        self.assertEqual(self.count.read_text(), "x")          # computed once for all five
        (self.repo / "mods.txt").write_text("pkg.tests.test_b\n")   # uncommitted: selected fresh
        self.assertEqual(self.select("--root", str(self.repo)).communicate()[0], "pkg.tests.test_b\n")
        git(self.repo, "commit", "-qam", "two")                 # a new tree: a new key
        self.assertEqual(self.select("--root", str(self.repo)).communicate()[0], "pkg.tests.test_b\n")
        self.assertEqual(self.count.read_text(), "xxx")
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0)   # no checkout left behind


class SlotStatusTests(unittest.TestCase):
    def test_held_free_and_waiting_slots_are_shown_and_dead_records_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "spark-runs").mkdir()
            held = os.open(d / "spark-slot.2.lock", os.O_RDWR | os.O_CREAT)
            fcntl.flock(held, fcntl.LOCK_EX)
            self.addCleanup(os.close, held)
            now = time.time()
            me, dead = os.getpid(), 2 ** 22 + 7
            for pid, state, slot in ((me, "running", "2"), (os.getppid(), "waiting", ""), (dead, "waiting", "")):
                (d / "spark-runs" / f"{pid}.json").write_text(json.dumps(
                    {"pid": pid, "sha": "c" * 40, "ids": 12, "state": state, "slot": slot,
                     "since": now - 600, "who": "lane-x"}))
            lines = run_cache.slots(d, 3, now=now)
            self.assertEqual(lines[0], "slot 1: free")
            self.assertEqual(lines[1], "slot 2: running cccccccccc (12 ids, lane-x) for 10 min")
            self.assertEqual(lines[2], "slot 3: free")
            self.assertEqual(lines[3], "waiting 1: cccccccccc (lane-x) for 10 min")
            self.assertEqual(lines[-1], "2 of 3 slots free, 1 waiting")
            self.assertFalse((d / "spark-runs" / f"{dead}.json").exists())


if __name__ == "__main__":
    unittest.main()
