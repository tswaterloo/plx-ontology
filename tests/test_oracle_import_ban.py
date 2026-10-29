"""The oracle cannot run Workbench code (09 R7 c, 10-DECISION 8): gate ``oracle-import-ban``.

Two layers. The static allowlist scan (``scripts/check_oracle_imports.py``) is clean on
this tree and flags every planted bypass, including the ones a deny list missed (path
loading of ``ontology/json_contract.py`` through importlib/runpy/SourceFileLoader/exec,
aliased ``import_module``, ``getattr(importlib, ...)``, ``builtins.__import__``,
``pkgutil.resolve_name``, ``from scripts import ...``). At run time the whole package is
imported and the fixture generator run end to end under an audit hook that records every
repository file opened, imported or executed outside ``evals/``: there must be none.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_oracle_imports.py"

BYPASSES = {
    "a.py": "import ontology\n",
    "b.py": "from operating.case_runtime import x\n",
    "c.py": "from integrations import workbench_read_api\n",
    "d.py": "import integrations.bc_read as r\n",
    "e.py": "from ...ontology import core\n",
    "f.py": "import importlib\nimportlib.import_module('ontology.core')\n",
    "g.py": "name = 'operating'\n__import__(name)\n",
    "sub/h.py": "from .... import ontology\n",
    "i.py": ("import importlib.util\n"
             "spec = importlib.util.spec_from_file_location('jc', 'ontology/json_contract.py')\n"
             "m = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\n"),
    "j.py": "import runpy\nrunpy.run_path('ontology/json_contract.py')\n",
    "k.py": "exec(open('ontology/json_contract.py').read())\n",
    "l.py": "from importlib import import_module as im\nim('ontology.core')\n",
    "m.py": "import importlib\ngetattr(importlib, 'import_module')('ontology.core')\n",
    "n.py": "import builtins\nbuiltins.__dict__['__import__']('ontology')\n",
    "o.py": "import pkgutil\npkgutil.resolve_name('ontology.core:Actor')\n",
    "p.py": ("from importlib.machinery import SourceFileLoader\n"
             "SourceFileLoader('x', 'ontology/json_contract.py').load_module()\n"),
    "q.py": "from scripts import eval_oracle_dev\n",
    "r.py": "import json\njson.__loader__\n",
    "s.py": "x = ().__class__.__mro__\n",
    "t.py": "f = eval\n",
    "u.py": "code = compile('1', 'f', 'eval')\n",
    "v.py": "from pathlib import Path\nPath('operating/case_runtime.py').read_text()\n",
    "w.py": "import sys\nsys.modules\n",
    "x.py": "from evals import runner\n",
    "y.data": "not python\n",
    # Attribute chains from allowed modules (re-review b5fc325).
    "z1.py": "import typing\ntyping.sys.modules['builtins'].exec('1')\n",
    "z2.py": "import pathlib\npathlib.os.system('true')\n",
    "z3.py": "import dataclasses\nm = dataclasses.sys.modules\n",
    "z4.py": "from typing import get_type_hints\n",
    "z5.py": "import typing\nx = typing.ForwardRef('1')\n",
    "z6.py": "import unittest\n",  # unittest only inside the package's tests
    "tests/z7.py": "from unittest import mock\n",
    "tests/z8.py": "import unittest.mock\n",
    "tests/z9.py": "import unittest\nunittest.mock.patch('x.y')\n",
    "tests/z10.py": "import unittest\nunittest.defaultTestLoader.loadTestsFromName('x.y')\n",
    "z11.py": "s = \"x='ontology'\"\n",
    "z12.py": "from re import *\n",
}
CLEAN = ("from .keys import EDGES\nimport json\nimport re\nfrom evals.oracle import keys\n"
         "from urllib.parse import quote\nP = re.compile('x')\n")
CLEAN_TEST = "import unittest\nfrom evals.oracle import keys\n"


def _scan(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), str(root)], capture_output=True,
                          text=True, timeout=60)


class OracleImportBanTest(unittest.TestCase):
    def test_repository_oracle_is_clean(self):
        done = _scan(ROOT)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("clean", done.stdout)

    def test_every_known_bypass_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "evals" / "oracle"
            for name, text in {**BYPASSES, "ok.py": CLEAN, "tests/ok.py": CLEAN_TEST}.items():
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            done = _scan(Path(tmp))
        self.assertEqual(done.returncode, 1)
        flagged = {line.split(":")[0].split("evals/oracle/", 1)[1]
                   for line in done.stdout.splitlines() if line.startswith("evals/")}
        self.assertEqual(flagged, set(BYPASSES))

    def test_missing_package_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_scan(Path(tmp)).returncode, 1)

    def test_runtime_touches_no_repository_code_outside_evals(self):
        code = """
import json, os, sys
ROOT = os.path.realpath(sys.argv[1])
EVALS = os.path.join(ROOT, "evals") + os.sep
seen = []
def hook(event, args):
    path = None
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = os.fsdecode(args[0])
    elif event == "exec" and hasattr(args[0], "co_filename"):
        path = args[0].co_filename
    elif event == "import" and len(args) > 1 and isinstance(args[1], str):
        path = args[1]
    if path and not path.startswith("<"):  # "<frozen ...>", "<string>": not files
        real = os.path.realpath(path)
        if real.startswith(ROOT + os.sep) and not real.startswith(EVALS):
            seen.append([event, os.path.relpath(real, ROOT)])
    if event == "import" and str(args[0]).split(".")[0] in (
            "ontology", "operating", "integrations", "scripts"):
        seen.append([event, str(args[0])])
sys.addaudithook(hook)
import importlib, pkgutil, evals.oracle
for m in pkgutil.walk_packages(evals.oracle.__path__, "evals.oracle."):
    importlib.import_module(m.name)
from evals.oracle import BcReads, PortalReads, Sources
from evals.oracle.fixture_world import build_world
from evals.oracle.generator import generate
w = build_world()
g = generate(Sources(PortalReads(w.read_relation), BcReads(w.bc_port())), run_id="audit",
             now="2099-01-02T04:00:00Z", documents=w.documents())
print(json.dumps({"items": len(g.items), "seen": seen}))
"""
        done = subprocess.run([sys.executable, "-c", code, str(ROOT)], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(report["items"], 30)
        self.assertEqual(report["seen"], [])

    def test_runtime_hook_would_see_a_path_load(self):
        """The audit hook is not vacuous: a path load of an ontology file is reported."""
        code = """
import os, sys
ROOT = os.path.realpath(sys.argv[1])
seen = []
sys.addaudithook(lambda e, a: seen.append(e) if e == "open" and isinstance(a[0], str)
                 and os.path.realpath(a[0]).startswith(os.path.join(ROOT, "ontology")) else None)
import importlib.util
spec = importlib.util.spec_from_file_location("jc", os.path.join(ROOT, "ontology", "json_contract.py"))
spec.loader.exec_module(importlib.util.module_from_spec(spec))
print(len(seen))
"""
        done = subprocess.run([sys.executable, "-c", code, str(ROOT)], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertGreaterEqual(int(done.stdout.strip()), 1)


if __name__ == "__main__":
    unittest.main()
