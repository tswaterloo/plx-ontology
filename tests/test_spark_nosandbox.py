"""Spark runs where bwrap cannot start (deploy/spark/pathmap.c + the no-sandbox mode of
spark_run_remote.py), and the resident plan runners spread over several Sparks."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy" / "spark"))
import spark_run_remote as remote  # noqa: E402


@unittest.skipUnless(shutil.which("gcc"), "needs gcc (the Spark builds the map the same way)")
class PathMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.so = cls.tmp / "pathmap.so"
        subprocess.run(["gcc", "-O2", "-fPIC", "-shared", "-o", str(cls.so),
                        str(ROOT / "deploy/spark/pathmap.c"), "-ldl"], check=True)
        cls.ro, cls.rw = cls.tmp / "ro", cls.tmp / "rw"
        (cls.ro / "bin").mkdir(parents=True)
        cls.rw.mkdir()
        (cls.ro / "hello.txt").write_text("from the spark")
        (cls.ro / "bin" / "tool").write_text("#!/bin/sh\necho tool-ran\n")
        (cls.ro / "bin" / "tool").chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_py(self, code: str, mapped: bool = True) -> subprocess.CompletedProcess:
        env = {"PATH": "/data/fake/bin:/usr/bin:/bin", "LD_PRELOAD": str(self.so)}
        if mapped:
            env["PLX_PATHMAP"] = (f"/data/fake={self.ro}=ro:/data/out={self.rw}=rw")
        return subprocess.run(["python3", "-c", code], capture_output=True, text=True,
                              env=env, timeout=60)

    def test_reads_follow_the_map_writes_obey_it(self):
        got = self.run_py(
            "import os, subprocess\n"
            "print(open('/data/fake/hello.txt').read())\n"
            "print(os.path.exists('/data/fake/hello.txt'), os.path.exists('/data/nothing'))\n"
            "open('/data/out/x', 'w').write('1'); print(os.path.exists('/data/out/x'))\n"
            "try: open('/data/fake/new', 'w')\n"
            "except OSError as e: print('refused', e.errno)\n"
            "try: os.remove('/data/fake/hello.txt')\n"
            "except OSError as e: print('refused', e.errno)\n"
            "print(subprocess.run(['tool'], capture_output=True, text=True).stdout.strip())\n"
            "print(subprocess.run(['/data/fake/bin/tool'], capture_output=True, text=True).stdout.strip())\n")
        self.assertEqual(got.stderr.strip(), "")
        self.assertEqual(got.stdout.split("\n")[:-1],
                         ["from the spark", "True False", "True", "refused 30", "refused 30",
                          "tool-ran", "tool-ran"])
        self.assertTrue((self.rw / "x").exists())
        self.assertTrue((self.ro / "hello.txt").exists())

    def test_a_child_with_a_rebuilt_environment_keeps_the_map(self):
        got = self.run_py(
            "import subprocess\n"
            "r = subprocess.run(['/usr/bin/env', 'sh', '-c', 'echo ${PLX_PATHMAP:+map} ${LD_PRELOAD:+pre}; cat /data/fake/hello.txt'],"
            " env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)\n"
            "print(r.stdout.strip())\n")
        self.assertEqual(got.stdout.strip().split("\n"), ["map pre", "from the spark"])

    def test_a_runner_can_make_its_folders_under_a_parent_that_exists_only_in_the_map(self):
        # plan_resident starts with os.makedirs(out, exist_ok=True) for /data/plx-plan/out, a
        # --spark-out folder: its parents (/data, /data/plx-plan) exist in the sandbox, not on a
        # Spark without them (11-24: PermissionError '/data' on sb4ec)
        out, queue = self.tmp / "so" / "out", self.tmp / "so" / "queue"
        out.mkdir(parents=True, exist_ok=True)
        queue.mkdir(parents=True, exist_ok=True)
        binds = [(str(out), "/data/plx-plan/out", False), (str(queue), "/data/plx-plan/queue", False),
                 ("/usr/bin/env", "/usr/local/bin/claude", True)]
        skel = remote.skeleton(binds, self.tmp / "skel")
        made = {d for _s, d, _r in skel}
        self.assertIn("/data/plx-plan", made)
        self.assertFalse(made & {"/usr", "/usr/local", "/usr/local/bin"})      # they exist on a Spark: left alone
        env = {"PATH": "/usr/bin:/bin", "LD_PRELOAD": str(self.so),
               "PLX_PATHMAP": remote.pathmap(binds + skel)}
        code = ("import os\nos.makedirs('/data/plx-plan/out', exist_ok=True)\n"
                "os.makedirs('/data/plx-plan/queue', exist_ok=True)\n"
                "open('/data/plx-plan/queue/owner-w1', 'w').write('n')\n"
                "print(os.path.isdir('/data/plx-plan'), os.path.exists('/data/plx-plan/other'))\n"
                "try: os.mkdir('/data/plx-plan/new')\nexcept OSError as e: print('refused', e.errno)\n")
        got = subprocess.run(["python3", "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(got.stderr, "")
        self.assertEqual(got.stdout.split("\n")[:2], ["True False", "refused 30"])
        self.assertEqual((queue / "owner-w1").read_text(), "n")

    def test_a_path_that_is_not_mapped_is_left_alone(self):
        got = self.run_py("print(open('/etc/hostname').read() == open('/etc/hostname').read())")
        self.assertEqual(got.stdout.strip(), "True")

    def test_a_prefix_is_not_a_path_component(self):
        # /data/fakeish is not under /data/fake
        got = self.run_py("import os; print(os.path.exists('/data/fakeish/hello.txt'))")
        self.assertEqual(got.stdout.strip(), "False")

    def test_without_a_map_it_is_a_pass_through(self):
        got = self.run_py("print(open('/etc/hostname').read() != '')", mapped=False)
        self.assertEqual(got.stdout.strip(), "True")

    def test_the_sudo_stand_in_gives_a_program_outside_the_map_the_spark_paths(self):
        shim = self.tmp / "sudo"
        shim.write_text(remote.SUDO_SHIM_PRELOAD)
        shim.chmod(0o755)
        env = {"PATH": "/usr/bin:/bin", "PLX_PATHMAP": f"/var/lib/home={self.rw}=rw",
               "LD_PRELOAD": str(self.so)}
        got = subprocess.run([str(shim), "-n", "-u", "x", "-g", "x", "env", "-i", "HOME=/var/lib/home",
                              "sh", "-c", "echo $HOME; echo ${LD_PRELOAD:+preload}"],
                             capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(got.stdout.split(), [str(self.rw), "preload"])

    def test_the_mode_is_built_from_the_same_binds_as_the_sandbox(self):
        binds = [("/s/a", "/data/a", True), ("/s/b", "/data/b", False)]
        self.assertEqual(remote.pathmap(binds), "/data/a=/s/a=ro:/data/b=/s/b=rw")


class PlanRunnerHosts(unittest.TestCase):
    def setUp(self):
        from operating.pg import plan_sections_pull as pp
        self.pp = pp
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def env(self, text):
        p = os.path.join(self.dir, "plan.env")
        Path(p).write_text(text)
        return p

    def test_hosts_come_from_the_settings_file_default_s7d3d(self):
        self.assertEqual(self.pp.runner_hosts(os.path.join(self.dir, "none")), ["s7d3d"])
        self.assertEqual(self.pp.runner_hosts(self.env("PGUSER=x\n")), ["s7d3d"])
        self.assertEqual(self.pp.runner_hosts(self.env("PLX_PLAN_HOSTS=s7d3d sb4ec\n")),
                         ["s7d3d", "sb4ec"])

    def test_a_bad_host_name_fails_loudly(self):
        with self.assertRaises(ValueError):
            self.pp.runner_hosts(self.env("PLX_PLAN_HOSTS=s7d3d;rm\n"))

    def test_requests_reach_every_host_and_stay_in_the_spool_when_one_is_away(self):
        got = []

        class T:
            def __init__(self, host, down=False):
                self.host, self.down = host, down

            def put_ids(self, ids, name):
                if self.down:
                    raise RuntimeError("ssh down")
                got.append((self.host, list(ids)))

        self.pp.FanOut([T("a"), T("b")]).put_ids(["r1"], "q.ids")
        self.assertEqual(got, [("a", ["r1"]), ("b", ["r1"])])
        got.clear()
        with self.assertRaises(RuntimeError) as cm:
            self.pp.FanOut([T("a"), T("b", down=True)]).put_ids(["r2"], "q.ids")
        self.assertIn("b: ssh down", str(cm.exception))
        self.assertEqual(got, [("a", ["r2"])])        # the other host still got them

    def written_env(self, hosts):
        """The settings file as install_plan_resident.sh's env phase writes it."""
        script = (ROOT / "deploy/ops/install_plan_resident.sh").read_text()
        fn = script[script.index("write_env() {"):script.index("for phase in")]
        got = subprocess.run(["bash", "-c", fn + '\nwrite_env | sed "s#@HOME@#/h#g"'],
                             capture_output=True, text=True, env={"PATH": "/usr/bin:/bin",
                                                                  "PLX_PLAN_HOSTS": hosts})
        self.assertEqual(got.returncode, 0, got.stderr)
        return self.env(got.stdout)

    def test_the_written_settings_file_splits_the_shards_four_and_four(self):
        path = self.written_env("s7d3d sb4ec")
        script = (ROOT / "deploy/spark/plan_resident.sh").read_text()
        snippet = script[script.index('if [ -z "$host" ]'):script.index("unset PLX_WORKBENCH")]
        out = []
        for i in range(8):
            r = subprocess.run(["bash", "-c", f'set -a; . "{path}"; set +a; shard={i}/8 host=""\n'
                                f'{snippet}\necho $host'], capture_output=True, text=True)
            self.assertEqual(r.stderr, "")
            out.append(r.stdout.strip())
        self.assertEqual(out.count("s7d3d"), 4)
        self.assertEqual(out.count("sb4ec"), 4)
        self.assertEqual(out, ["s7d3d", "sb4ec"] * 4)
        # the puller reads the same file the same way, and a request fans out to both hosts
        self.assertEqual(self.pp.runner_hosts(path), ["s7d3d", "sb4ec"])
        self.assertEqual(self.pp.runner_hosts(self.written_env("s7d3d")), ["s7d3d"])
        self.assertEqual(self.pp.runner_hosts(self.env('PLX_PLAN_HOSTS="s7d3d sb4ec"\n')),
                         ["s7d3d", "sb4ec"])

    def test_the_runner_script_gives_shard_i_the_host_at_i_mod_count(self):
        script = (ROOT / "deploy/spark/plan_resident.sh").read_text()
        snippet = script[script.index('if [ -z "$host" ]'):script.index("unset PLX_WORKBENCH")]
        out = []
        for i in range(8):
            r = subprocess.run(["bash", "-c", f'shard={i}/8 host=""; PLX_PLAN_HOSTS="s7d3d sb4ec"\n'
                                f'{snippet}\necho $host'], capture_output=True, text=True)
            out.append(r.stdout.strip())
        self.assertEqual(out, ["s7d3d", "sb4ec"] * 4)




class SandboxProbe(unittest.TestCase):
    def fake(self, *outcomes):
        seq = list(outcomes)

        def run(*a, **k):
            o = seq.pop(0)
            if isinstance(o, BaseException):
                raise o
            return subprocess.CompletedProcess(a[0], o[0], "", o[1])
        return run

    def test_only_a_refused_namespace_switches_modes(self):
        refused = (1, "bwrap: No permissions to create new namespace, likely because the kernel does not allow non-privileged user namespaces")
        self.assertFalse(remote.sandbox_available(self.fake(refused)))
        self.assertTrue(remote.sandbox_available(self.fake((0, ""))))

    def test_a_timeout_retries_once_then_fails_loudly(self):
        t = subprocess.TimeoutExpired("bwrap", 60)
        self.assertTrue(remote.sandbox_available(self.fake(t, (0, ""))))
        with self.assertRaises(SystemExit):
            remote.sandbox_available(self.fake(t, t))

    def test_another_failure_never_drops_the_sandbox(self):
        with self.assertRaises(SystemExit):
            remote.sandbox_available(self.fake((1, "bwrap: execvp true: No such file or directory")))
        with self.assertRaises(SystemExit):
            remote.sandbox_available(self.fake(FileNotFoundError("bwrap")))


if __name__ == "__main__":
    unittest.main()
