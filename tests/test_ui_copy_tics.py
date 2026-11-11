"""The static copy a Workbench reader sees carries none of the writing tics in
``operating.prose_style``: the app's strings and JSX text (``app/src``, tests excepted) and
the string literals of the read API and the operating modules that word briefs, alerts,
autopilot items and errors (tests excepted)."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from operating.prose_style import tics

ROOT = Path(__file__).resolve().parent.parent
# The style module quotes the tics it bans.
EXEMPT = {Path("operating/prose_style.py")}

_TS_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|`((?:[^`\\]|\\.)*)`')
_JSX_TEXT = re.compile(r">([^<>{}]*[A-Za-z][^<>{}]*)<")
_JSX_HEADING = re.compile(r"<(h[1-6]|CardTitle|SectionTitle)\b[^>]*>([^<{}]+)<", re.S)


def _ts_copy(text: str) -> list[str]:
    out = [next(g for g in m.groups() if g is not None) for m in _TS_STRING.finditer(text)]
    out += [" ".join(m.group(1).split()) for m in _JSX_TEXT.finditer(text)]
    out += [" ".join(m.group(2).split()) for m in _JSX_HEADING.finditer(text)]
    return [s for s in out if re.search(r"[A-Za-z]{2,}\s+[A-Za-z]", s)]


def _py_copy(text: str) -> list[str]:
    tree = ast.parse(text)
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
            and re.search(r"[A-Za-z]{2,}\s+[A-Za-z]", n.value)]


def _files():
    for path in sorted((ROOT / "app" / "src").rglob("*.ts*")):
        if ".test." not in path.name and path.suffix in (".ts", ".tsx"):
            yield path, _ts_copy
    for package in ("operator_api", "operating"):
        for path in sorted((ROOT / package).rglob("*.py")):
            if "tests" not in path.parts and path.relative_to(ROOT) not in EXEMPT:
                yield path, _py_copy


def scan() -> list[str]:
    found = []
    for path, extract in _files():
        for copy in extract(path.read_text(encoding="utf-8")):
            for tic in tics(copy):
                found.append(f"{path.relative_to(ROOT)}: {tic}: {copy[:120]!r}")
    return found


class StaticCopyTicsTest(unittest.TestCase):
    def test_static_copy_has_no_writing_tics(self) -> None:
        found = scan()
        self.assertEqual(found, [], "rewrite each as a plain statement:\n" + "\n".join(found))

    def test_lint_catches_each_listed_tic_and_passes_plain_copy(self) -> None:
        staged = {
            "It is not a delay. It is a cancellation.": "negation-first reveal",
            "This is the part that matters for the buyer.": "significance designation",
            "The table shows it: three POs slipped.": "abstraction agency",
            "Five rows are one supplier. The sixth is not.": "deferred noun",
            "What this means": "coy header",
            "Late is late, and that's the whole point.": "aphoristic closer",
            "You might think the PO shipped, but it did not.": "straw-man knockdown",
            "Late. Unconfirmed. Unpaid. Escalate.": "fragment cadence",
            "The confirmation is the load-bearing record.": "dressed metaphor",
            "Bottom line: PO 90412 is 12 days late.": "label-first framing",
        }
        for copy, tic in staged.items():
            self.assertIn(tic, tics(copy), copy)
        for plain in ("Not on record", "Draft, not sent", "Mentions this record; not a "
                      "confirmation", "PO 90412 is 12 days late; the supplier has not "
                      "confirmed a date.", "Evidence (3 steps)", "Nothing is waiting on you."):
            self.assertEqual(tics(plain), [], plain)


if __name__ == "__main__":
    unittest.main()
