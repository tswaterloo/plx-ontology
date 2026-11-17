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
            # copy the app carried before the 2026-11 sweep
            "Ask what matters about it": "significance designation",
            "What it reads now": "coy header",
            "What Ask reads": "coy header",
            "What's open, and what is prepared": "coy header",
            "How late, drawn": "coy header",
            "If something is wrong, say so. We read every note.": "aphoristic closer",
            "What I couldn't confirm": "first-person software",
            "What I checked": "first-person software",
            # 11-05/11-06 owner: "short lines like that", "slipped, vendor and 6 more"
            "Couldn't save. Try again.": "paired short sentences",
            "Nothing late. All clear.": "paired short sentences",
            "Slipped, vendor and 6 more": "truncated list",
            "Late POs, receipts, and ${n} more": "truncated list",
            "Lot L-24-0917, QC note, #qc and 1 more": "truncated list",
        }
        for copy, tic in staged.items():
            self.assertIn(tic, tics(copy), copy)
        for plain in ("Not on record", "Draft, not sent", "Mentions this record; not a "
                      "confirmation", "PO 90412 is 12 days late; the supplier has not "
                      "confirmed a date.", "Evidence (3 steps)", "Nothing is waiting on you.",
                      "Sources, last read", "Days late per order", "Ask about it",
                      "Not confirmed", "Open and prepared items", "Show 3 more",
                      "PO 90412 is late. Chase it today.", "Saved. Undo",
                      "Couldn't save that; try again", "12 of 40 POs loaded",
                      "Lot L-24-0917, QC note and #qc"):
            self.assertEqual(tics(plain), [], plain)


# a list the code cuts and ends with a count ("${names.join(", ")} and ${n} more", JSX
# "and {n} more", "· ${n} more"): built at run time, so the copy scan above never sees it whole
_CUT_LIST = re.compile(r"(?:,|;|·|\band)\s*\$?\{[^{}\n]{1,80}\}\s+more\b")


def cut_lists() -> list[str]:
    found = []
    for path in sorted((ROOT / "app" / "src").rglob("*.ts*")):
        if ".test." in path.name or path.name.endswith(".d.ts"):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _CUT_LIST.search(line):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    return found


# the same cut in the server's copy: an f-string whose text, its values as "{}", ends a list
# with a count ("{a}, {b} and {n} more", "{x}; +{n} more", "... {n} more"), or a literal
# appended to a list ("..." + " and more"). operating.prose_style.some_of states the count
# first instead ("3 of 7: A, B, C").
_PY_CUT = re.compile(r"(?:(?:\band|\+|\.\.\.)\s*\{\}\s+(?:more|others?)\b"
                     r"|[,;·]\s*\{\}\s+(?:more|others?)\s*(?:$|[.)\]]))")
_PY_AND_MORE = re.compile(r"^\W*(?:and|\+)\s*more\b")
# Text only a model reads (tool results the agent loop hands the model, never shown to a
# person as written): a model needs the count of what it was not shown, in any form.
MODEL_FACING = {Path("operating/agent_loop.py"), Path("operating/ontology_tools.py")} | {
    p.relative_to(ROOT) for p in (ROOT / "operating").glob("ontology_tools_*.py")}


def _template(node: ast.JoinedStr) -> str:
    return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)


def server_cut_lists() -> list[str]:
    found = []
    for package in ("operator_api", "operating"):
        for path in sorted((ROOT / package).rglob("*.py")):
            rel = path.relative_to(ROOT)
            if "tests" in path.parts or rel in EXEMPT or rel in MODEL_FACING:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                text = (_template(node) if isinstance(node, ast.JoinedStr) else
                        node.value if isinstance(node, ast.Constant)
                        and isinstance(node.value, str) else None)
                if text is not None and (_PY_CUT.search(text) or _PY_AND_MORE.search(text)):
                    found.append(f"{rel}:{node.lineno}: {text.strip()[:100]!r}")
    return found


class CutListTest(unittest.TestCase):
    def test_no_list_in_the_app_is_cut_with_and_n_more(self) -> None:
        found = cut_lists()
        self.assertEqual(found, [], "name every item, count them, or end the list with a "
                         "Show more control (components/ui/show-more.tsx):\n" + "\n".join(found))

    def test_the_cut_list_pattern_catches_each_form_and_passes_controls(self) -> None:
        for cut in ('{g.items.length > 6 && <li>and {g.items.length - 6} more</li>}',
                    '`${a.join(", ")} and ${n} more`', '`${h.join(" · ")} · ${n - 2} more`',
                    '.join("; ") + `; ${people.length - 3} more`'):
            self.assertRegex(cut, _CUT_LIST, cut)
        for control in ('{`Show ${left} more`}', '{rows.length - most} more in Memory',
                        '`${list.length - max} more need you`', 'Show {n} more'):
            self.assertNotRegex(control, _CUT_LIST, control)

    def test_no_server_copy_cuts_a_list_with_and_n_more(self) -> None:
        found = server_cut_lists()
        self.assertEqual(found, [], "state the count, then the names "
                         "(operating.prose_style.some_of):\n" + "\n".join(found))

    def test_the_server_cut_pattern_catches_each_form_and_passes_counts(self) -> None:
        def cut(source: str) -> bool:
            node = ast.parse(source, mode="eval").body
            text = _template(node) if isinstance(node, ast.JoinedStr) else node.value
            return bool(_PY_CUT.search(text) or _PY_AND_MORE.search(text))
        for source in ('f"{a}, {b} and {n} more"', 'f"; and {n - 3} more"', 'f" +{n} more"',
                       'f"... {n} more"', 'f", and {n} others"', '" and more"',
                       'f"{a}; {n} more."'):
            self.assertTrue(cut(source), source)
        for source in ('f"3 of {n}: {names}"', 'f"The other {n} late lines are not listed"',
                       'f"{n} further rows, not listed"', '"running the read once more"',
                       'f"{n} or more"', 'f"needs {n} more than is on hand"',
                       'f"; {n} more you can\'t open"', 'f"({n} rows shown, {m} more not shown)"'):
            self.assertFalse(cut(source), source)

    def test_some_of_names_all_or_counts_first(self) -> None:
        from operating.prose_style import some_of
        self.assertEqual(some_of(["A", "B"], 3), "A, B")
        self.assertEqual(some_of(["A", "B", "C"], 3, sep="; "), "A; B; C")
        self.assertEqual(some_of(list("ABCDEFG"), 3), "3 of 7: A, B, C")
        self.assertEqual(some_of([], 3), "")
        for n in (1, 10, 60):
            names = [f"PO {i}" for i in range(n)]
            text = some_of(names, 5)
            self.assertEqual(tics(text), [], text)
            self.assertEqual(text.count("PO "), min(n, 5))
            if n > 5:
                self.assertTrue(text.startswith(f"5 of {n}: "), text)


if __name__ == "__main__":
    unittest.main()
