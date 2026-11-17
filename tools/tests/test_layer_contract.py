"""The layer contract (tools/layer_contract.py): the tree has no upward import beyond the
frozen allowlist, and a new one, in any form an import takes, fails the check."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import textwrap
import unittest

from tools import layer_contract as lc

ROOT = Path(__file__).resolve().parents[2]


def tree(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="layer-contract-"))
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(textwrap.dedent(text), encoding="utf-8")
    return root


BASE = {
    "ontology/__init__.py": "",
    "ontology/schema.py": "X = 1\n",
    "integrations/__init__.py": "",
    "integrations/mail.py": "from ontology import schema\n",          # down: allowed
    "operating/__init__.py": "",
    "operating/tools.py": "import integrations.mail\n",
    "operator_api/__init__.py": "",
    "operator_api/app.py": "from operating.tools import X\n",
    "evals/__init__.py": "",
    "evals/score.py": "import operator_api.app\n",                    # outside: unconstrained
    "tools/layer-contract-allowlist.json": "",
}


class LayerContractTest(unittest.TestCase):
    def run_check(self, root: Path, allow: list) -> tuple[int, str]:
        (root / "tools" / "layer-contract-allowlist.json").write_text(
            json.dumps({"format": lc.FORMAT, "edges": allow}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = lc.main(["--root", str(root)])
        return code, out.getvalue()

    def test_the_repo_keeps_its_contract(self):
        new, _gone = lc.check(ROOT, lc.ALLOWLIST)
        self.assertEqual(new, [], "new upward imports; move the code down instead")

    def test_downward_imports_pass(self):
        code, out = self.run_check(tree(BASE), [])
        self.assertEqual(code, 0, out)

    def test_every_form_of_a_new_upward_import_fails(self):
        forms = {
            "absolute": "import operating.tools\n",
            "from": "from operating import tools\n",
            "function-local": "def f():\n    from operating.tools import X\n    return X\n",
            "relative": "from ...operating import tools\n",
            "importlib": "import importlib\nM = importlib.import_module('operating.tools')\n",
            "outside the layers": "from evals import score\n",
        }
        for label, text in forms.items():
            with self.subTest(label):
                # ontology.sub.mod: ``...`` is the top level
                files = {**BASE, "ontology/sub/__init__.py": "", "ontology/sub/mod.py": text}
                code, out = self.run_check(tree(files), [])
                self.assertEqual(code, 1, out)
                self.assertIn("NEW upward import", out)

    def test_an_allowlisted_edge_passes_and_a_vanished_one_is_reported(self):
        files = {**BASE, "ontology/mod.py": "import operating.tools\n"}
        code, out = self.run_check(tree(files), [["ontology.mod", "operating.tools"],
                                                 ["ontology.gone", "operating.tools"]])
        self.assertEqual(code, 0, out)
        self.assertIn("1 allowlisted edge(s) no longer exist", out)

    def test_tests_are_not_held_to_the_contract(self):
        files = {**BASE, "ontology/tests/__init__.py": "",
                 "ontology/tests/test_x.py": "import operator_api.app\n",
                 "ontology/test_y.py": "import operator_api.app\n"}
        code, out = self.run_check(tree(files), [])
        self.assertEqual(code, 0, out)

    def test_a_broken_allowlist_fails_closed(self):
        root = tree(BASE)
        (root / "tools" / "layer-contract-allowlist.json").write_text('{"edges": []}')
        with self.assertRaises(ValueError):
            lc.check(root, root / "tools" / "layer-contract-allowlist.json")


if __name__ == "__main__":
    unittest.main()
