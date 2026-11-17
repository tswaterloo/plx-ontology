"""The Spark worker (AUDIT3-24): health decides where suites and embedding go, an unknown or
unreadable host is unfit, the fallback runs here, and no credential reaches a child.
Evidence label: isolated-fixture (an injected runner; no ssh is made)."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from operating import spark_worker as sw

GOOD = {"cpus": 20, "load1": 2.0, "mem_avail_kb": 64 * 1048576, "disk_free_kb": 900 * 1048576,
        "tests_ready": True, "gpus": 1}


class Runner:
    """Answers each command by its first words; records every call and its environment."""

    def __init__(self, probes=None, codes=None):
        self.probes, self.codes, self.calls = probes or {}, codes or {}, []

    def __call__(self, args, **kw):
        self.calls.append((list(args), kw))
        if args[0] == "ssh":
            host = args[-2]
            probe = self.probes.get(host)
            if isinstance(probe, Exception):
                raise probe
            text = probe if isinstance(probe, str) else json.dumps(probe) if probe else ""
            return subprocess.CompletedProcess(args, 0 if probe else 255, text, "")
        for key, code in self.codes.items():
            if key in " ".join(map(str, args)):
                return subprocess.CompletedProcess(args, code, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def ran(self, word):
        return [a for a, _ in self.calls if word in " ".join(map(str, a))]


class Health(unittest.TestCase):
    def test_fit_busy_unreadable_and_unreachable_hosts(self):
        run = Runner({"a": GOOD, "b": {**GOOD, "load1": 200.0}, "h": {**GOOD, "load1": 76.0}, "c": "not json",
                      "d": {**GOOD, "tests_ready": False, "gpus": 0},
                      "e": subprocess.TimeoutExpired("ssh", 20),
                      "f": {**GOOD, "cpus": True}})
        found = sw.health(["a", "b", "c", "d", "e", "f", "g", "h"], "cfg", run=run)
        hosts = found["hosts"]
        # h runs a suite already (3.8 per CPU): another suite may share it, embedding not
        self.assertEqual(sw.fit_hosts(found, "tests"), ["a", "h"])
        self.assertEqual(hosts["h"]["problems"], ["busy_for_embedding"])
        self.assertEqual(sw.fit_hosts(found, "embed"), ["a"])
        self.assertEqual(hosts["b"]["problems"], ["busy"])
        self.assertEqual(hosts["d"]["problems"], ["test_sandbox_missing", "no_gpu"])
        for unfit in ("c", "e", "f", "g"):        # unreadable, timed out, a bool, no answer
            self.assertEqual((hosts[unfit]["reachable"], hosts[unfit]["tests"]), (False, False))
        self.assertEqual(hosts["a"]["gpus"], 1)
        # every probe is one batch-mode ssh with a timeout, no credential in its environment
        for args, kw in run.calls:
            self.assertIn("BatchMode=yes", args)
            self.assertEqual(kw["timeout"], sw.PROBE_SECONDS)

    def test_the_health_file_is_written_whole_and_read_only_while_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            found = sw.health(["a"], "cfg", run=Runner({"a": GOOD}))
            sw.write_health(state, found)
            text = (state / sw.HEALTH_FILE).read_text()
            self.assertNotIn("100.", text)                     # no address in it
            self.assertEqual(sw.load_health(state)["hosts"]["a"]["tests"], True)
            later = dt.datetime.now(dt.timezone.utc) + sw.MAX_AGE + dt.timedelta(minutes=1)
            self.assertIsNone(sw.load_health(state, now=later))
            (state / sw.HEALTH_FILE).write_text('{"schema": "other"}')
            self.assertIsNone(sw.load_health(state))

    def test_the_default_watches_both_sparks(self):
        # AUDIT3-24: the timer's health file named only s7d3d; with no PLX_SPARK_HOSTS the
        # probe covers both hosts, a down one reads unfit, and an override still narrows it
        self.assertEqual(sw.hosts_named(""), ("s7d3d", "sb4ec"))
        found = sw.health(sw.hosts_named(""), "cfg", run=Runner({"s7d3d": GOOD}))
        self.assertEqual(sorted(found["hosts"]), ["s7d3d", "sb4ec"])
        self.assertTrue(found["hosts"]["s7d3d"]["tests"])
        self.assertFalse(found["hosts"]["sb4ec"]["tests"])
        self.assertEqual(sw.hosts_named("sb4ec"), ("sb4ec",))

    def test_unknown_inputs_are_refused(self):
        for bad in ("a;rm -rf /", "-oProxyCommand=x", "A B"):
            with self.assertRaises(ValueError):
                sw.hosts_named(bad)
        self.assertEqual(sw.hosts_named(""), sw.DEFAULT_HOSTS)
        with self.assertRaises(ValueError):
            sw._modules(["operating.tests.x; echo"])
        with self.assertRaises(ValueError):
            sw.run_tests("HEAD~1", ["m"], hosts=[], config="c", run=Runner())
        self.assertEqual(sw.main(["test", "abc1234", "--hosts", "x y"]), 2)


class Dispatch(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"PLX_WORKBENCH_API_KEY": "k",
                                               "AZURE_CLIENT_SECRET": "s", "PLAIN": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = []

    def no_secret(self, run):
        for _args, kw in run.calls:
            env = kw.get("env") or {}
            self.assertNotIn("PLX_WORKBENCH_API_KEY", env)
            self.assertNotIn("AZURE_CLIENT_SECRET", env)
            self.assertEqual(env.get("PLAIN"), "1")

    def test_suites_go_to_the_spark_and_a_failure_there_is_the_result(self):
        run = Runner(codes={"spark_test.sh": 1})
        code = sw.run_tests("abc1234", ["operating.tests.test_x"], hosts=["s7d3d"],
                            config="cfg", run=run, log=self.log.append)
        self.assertEqual(code, 1)                  # a red module is never retried here
        self.assertFalse(run.ran("worktree"))
        (args, kw), = [c for c in run.calls if "spark_test.sh" in " ".join(c[0])]
        self.assertEqual(kw["env"]["SPARK_HOST"], "s7d3d")
        self.no_secret(run)

    def test_no_fit_host_or_a_setup_failure_runs_the_modules_here(self):
        for hosts, codes in (([], {}), (["s7d3d", "sb4ec"], {"spark_test.sh": 96})):
            run = Runner(codes=codes)
            code = sw.run_tests("abc1234", ["operating.tests.test_x"], hosts=hosts,
                                config="cfg", run=run, log=self.log.append)
            self.assertEqual(code, 0)
            local = run.ran("testslot.py")
            self.assertEqual(len(local), 1)
            self.assertEqual(local[0][-1], "operating.tests.test_x")
            # from a detached checkout of the commit, removed after
            self.assertTrue(run.ran("worktree add -q --detach"))
            self.assertTrue(run.ran("worktree remove --force"))
            self.no_secret(run)
        self.assertIn("setup_failed", self.log[-1])
        self.assertIn("no_fit_host", self.log[0])

    def test_embedding_goes_to_the_gpu_hosts_else_the_cpu_backfill(self):
        run = Runner()
        self.assertEqual(sw.run_embed("/st", hosts=["s7d3d", "sb4ec"], config="cfg",
                                      run=run, log=self.log.append), 0)
        (args,) = run.ran("operating.embed remote")
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--host"],
                         ["s7d3d", "sb4ec"])
        self.assertFalse(run.ran("backfill"))
        failed = Runner(codes={"operating.embed remote": 1})
        sw.run_embed("/st", hosts=["s7d3d"], config="cfg", run=failed, log=self.log.append)
        self.assertEqual(len(failed.ran("backfill")), 1)
        self.assertEqual(failed.ran("backfill")[0][:3], ["nice", "-n", "15"])
        refused = Runner(codes={"operating.embed remote": 2})
        self.assertEqual(sw.run_embed("/st", hosts=["s7d3d"], config="cfg", run=refused,
                                      log=self.log.append), 2)
        self.assertFalse(refused.ran("backfill"))  # a refusal is not worked around
        none = Runner()
        sw.run_embed("/st", hosts=[], config="cfg", run=none, log=self.log.append)
        self.assertEqual((len(none.ran("remote")), len(none.ran("backfill"))), (0, 1))
        for r in (run, failed, refused, none):
            self.no_secret(r)


if __name__ == "__main__":
    unittest.main()
