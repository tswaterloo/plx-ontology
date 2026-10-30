"""A small fixture tree for the import graph (temporary directories, no repo reads)."""
from __future__ import annotations

from pathlib import Path


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


TREE = {
    "pkg/__init__.py": '"""Pkg package."""\n',
    "pkg/core.py": (
        '"""Core of the fixture.\n\nLonger text that is not the summary."""\n'
        "import os\nimport pkg.util.deep.leaf\nfrom pkg import util, helper\nfrom pkg.cli import run\n"
        "LIMIT = 3\n_hidden = 1\n\nclass Thing:\n    pass\n\ndef make():\n    pass\n"),
    "pkg/util/__init__.py": "",
    "pkg/util/deep.py": "from .. import helper\nfrom . import __init__ as init\n",
    "pkg/helper.py": "def assist():\n    pass\n",
    "pkg/cli.py": '"""Command line\nwrapped over two lines."""\nfrom .util.deep import *\n\n\ndef run():\n    pass\n',
    "pkg/lazy.py": "def go():\n    from pkg.helper import assist\n    return assist\n",
    "pkg/tests/test_core.py": "from pkg import core\n",
    "pkg/tests/test_lazy.py": "from pkg.lazy import go\n",
    "scripts/check_x.py": "from pkg.core import make\n",
    "tests/test_check_x.py": "import sys\nsys.path.insert(0, 'scripts')\nimport check_x\n",
    "tests/test_cli_process.py": 'CMD = ["python3", "-m", "pkg.cli"]\nPATH = "scripts/check_x.py"\n',
    "tests/test_prose.py": 'X = "pkg.core is mentioned in prose, not a reference"\nY = "pkg"\nZ = "__init__.py"\nW = "pkg/__init__.py"\n',
    ".hidden/skipped.py": "",
    "EVIDENCE/skipped.py": "",
    "__pycache__/skipped.py": "",
    "nested/.git": "gitdir: elsewhere\n",
    "nested/skipped.py": "",
}
