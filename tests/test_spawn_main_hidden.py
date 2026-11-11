"""A build script without an ``if __name__ == "__main__"`` guard is never run again by
the spawn workers of a pool made inside ``ontology.store.main_not_imported``, not even
by the workers the pool starts lazily after the context has exited."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SpawnMainHiddenTests(unittest.TestCase):

    def test_an_unguarded_script_runs_once_however_late_the_workers_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            marks = Path(tmp) / "marks"
            script = Path(tmp) / "unguarded.py"
            script.write_text(textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(ROOT)!r})
                import concurrent.futures as cf, multiprocessing, os
                from ontology.store import main_not_imported
                with open({str(marks)!r}, "a") as f:
                    f.write("top\\n")
                with main_not_imported():
                    pool = cf.ProcessPoolExecutor(3, mp_context=multiprocessing.get_context("spawn"))
                # submitted after the context: the workers start now
                print("ran" if sorted(pool.map(abs, range(30))) else "")
                pool.shutdown()
            """))
            done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                                  timeout=120, cwd=tmp)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("ran", done.stdout)
            self.assertEqual(marks.read_text().splitlines(), ["top"])


if __name__ == "__main__":
    unittest.main()
