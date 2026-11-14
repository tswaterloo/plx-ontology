"""Pool workers end with the process that started them (``ontology.parent_watch``)."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]

# A parent that starts a two-worker pool through ``pool_init`` (an initializer of its own
# chained after the watch), writes the workers' pids, then waits to be killed.
PARENT = textwrap.dedent("""
    import multiprocessing as mp, os, sys, time
    from concurrent.futures import ProcessPoolExecutor
    from ontology import parent_watch

    def mark(path):
        with open(path, "a") as f:
            f.write(f"init {os.getpid()}\\n")

    def pid(_):
        time.sleep(0.3)
        return os.getpid()

    if __name__ == "__main__":
        out, kind = sys.argv[1], sys.argv[2]
        if kind == "futures":
            pool = ProcessPoolExecutor(2, mp_context=mp.get_context("spawn"),
                                       **parent_watch.pool_init(mark, (out + ".init",)))
            pids = set(pool.map(pid, range(4)))
        else:
            pool = mp.get_context("spawn").Pool(2, **parent_watch.pool_init(
                mark, (out + ".init",)))
            pids = set(pool.map(pid, range(4)))
        with open(out, "w") as f:
            f.write(" ".join(map(str, sorted(pids))))
        time.sleep(600)
""")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie of ours is gone for this purpose
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


class PoolWorkersEndWithParent(unittest.TestCase):
    def _run(self, kind: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script, out = Path(tmp) / "parent.py", Path(tmp) / "pids"
            script.write_text(PARENT)
            env = dict(os.environ, PYTHONPATH=str(ROOT))
            parent = subprocess.Popen([sys.executable, str(script), str(out), kind],
                                      cwd=tmp, env=env)
            pids: list[int] = []
            try:
                deadline = time.time() + 60
                while time.time() < deadline and not (out.exists() and out.read_text()):
                    time.sleep(0.1)
                pids = [int(p) for p in out.read_text().split()]
                self.assertTrue(pids)
                self.assertTrue(all(_alive(p) for p in pids))
                # the earlier initializer still ran in every worker
                inits = {int(l.split()[1]) for l in Path(str(out) + ".init").read_text()
                         .splitlines()}
                self.assertTrue(set(pids) <= inits)
                parent.send_signal(signal.SIGKILL)   # no exit handler runs
                parent.wait(10)
                deadline = time.time() + 10
                while time.time() < deadline and any(_alive(p) for p in pids):
                    time.sleep(0.2)
                self.assertEqual([p for p in pids if _alive(p)], [])
            finally:
                if parent.poll() is None:
                    parent.kill()
                for p in pids:
                    try:
                        os.kill(p, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_process_pool_executor_workers_end(self) -> None:
        self._run("futures")

    def test_multiprocessing_pool_workers_end(self) -> None:
        self._run("pool")

    def test_old_name_still_imports(self) -> None:
        from operating import parent_watch as old
        from ontology import parent_watch as new
        self.assertIs(old.die_with, new.die_with)
        self.assertIs(old.pool_init, new.pool_init)


if __name__ == "__main__":
    unittest.main()
