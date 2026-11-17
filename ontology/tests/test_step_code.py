"""CAP-L180 option 1: a step's code digest is its definitions, not its files' bytes.

A v2 table (``ontology/step_code.json`` with ``roots``) digests the definitions each step's
roots reach in its code files (docstrings and comments left out), each listed module the
roots reach nothing in whole, each listed JSON file canonically. A v1 table keeps the byte
digests its build recorded. The first plan from a v1 served commit to a v2 tree of the same
definitions runs nothing and keeps the served byte digest as ``steps_from`` (else the
apply and the delta updater refuse ``other_steps``).
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ontology import step_code
from ontology import step_migrate as sm

A = '''"""Module a."""
import json

LIMIT = 3


def helper(x):
    """Add the limit."""
    return x + LIMIT


def unused():
    return 1


def step_a(builder, projector):
    return helper(1)
'''

B = '''from pkg.a import helper


def step_b(builder, projector):
    return helper(2) * 2


def other():
    return 0
'''

C = '''def lone():
    return 5
'''

FILES = {"a": ["pkg/a.py"], "b": ["pkg/b.py", "pkg/a.py", "pkg/c.py", "pkg/rules.json"]}
ROOTS = {"a": [["pkg.a", "step_a"]], "b": [["pkg.b", "step_b"]]}


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q")
        for path, text in (("pkg/__init__.py", ""), ("pkg/a.py", A), ("pkg/b.py", B),
                           ("pkg/c.py", C), ("pkg/rules.json", '{"x": 1, "y": [1, 2]}')):
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text)
        self.table(roots=True)
        self.base = self.commit("base")

    def table(self, roots: bool) -> None:
        (self.repo / "ontology").mkdir(exist_ok=True)
        table = {"format": step_code.FORMAT_V2 if roots else step_code.FORMAT_V1,
                 "steps": FILES}
        if roots:
            table["roots"] = ROOTS
        (self.repo / step_code.CODE_FILE).write_text(json.dumps(table))

    def commit(self, message: str) -> str:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def edit(self, path: str, old: str, new: str) -> str:
        file = self.repo / path
        text = file.read_text()
        self.assertIn(old, text)
        file.write_text(text.replace(old, new))
        return self.commit(f"edit {path}")

    def code(self, ref):
        return step_code.code_at(ref, self.repo)

    def moved(self, before: str, after: str) -> set[str]:
        old, new = self.code(before), self.code(after)
        return {k for k in old if old[k] != new[k]}


class DefinitionDigestTests(Fixture):
    def test_docstrings_comments_and_blank_lines_move_nothing_but_move_the_bytes(self):
        self.edit("pkg/a.py", '"""Add the limit."""', '"""Add the limit (a new word)."""')
        self.edit("pkg/a.py", '"""Module a."""', '"""Module a, documented again."""')
        after = self.edit("pkg/a.py", "    return x + LIMIT\n",
                          "    # the limit, again\n\n    return x + LIMIT  # added\n")
        self.assertEqual(self.moved(self.base, after), set())
        old = step_code.code_digests(self.base, self.repo, FILES)
        new = step_code.code_digests(after, self.repo, FILES)
        self.assertEqual({k for k in old if old[k] != new[k]}, {"a", "b"})   # v1 would run both
        # the tree read in place and the commit read from git agree
        self.assertEqual(step_code.code_at(None, self.repo), self.code(after))

    def test_the_tree_digests_are_kept_until_a_file_they_can_read_changes(self):
        # a build records them (``build_store``) and resolve and overlay ask again: seconds
        # each under v2, so a process derives them once per tree content
        first = step_code.code_at(None, self.repo)
        with mock.patch.object(step_code, "_definition_digests",
                               side_effect=AssertionError("derived again")):
            self.assertEqual(step_code.code_at(None, self.repo), first)
        self.edit("pkg/a.py", "LIMIT = 3", "LIMIT = 4")   # same size: by bytes, not stat
        again = step_code.code_at(None, self.repo)
        self.assertEqual({k for k in first if first[k] != again[k]}, {"a", "b"})
        (self.repo / "pkg" / "d.py").write_text("X = 1\n")   # an added module counts too
        with mock.patch.object(step_code, "_definition_digests",
                               wraps=step_code._definition_digests) as derived:
            self.assertEqual(step_code.code_at(None, self.repo), again)
        self.assertEqual(derived.call_count, 1)

    def test_a_new_process_reads_the_tree_digests_another_kept(self):
        # each new host derives them on its first find (a live store's build): ~7 s cold
        cache = Path(self.tmp.name) / "cache"
        with mock.patch.dict(os.environ, {"PLX_STEP_CODE_CACHE": str(cache)}):
            first = step_code.code_at(None, self.repo)
            step_code._TREE_DIGESTS.clear()   # as a new process
            with mock.patch.object(step_code, "_definition_digests",
                                   side_effect=AssertionError("derived again")):
                self.assertEqual(step_code.code_at(None, self.repo), first)
            (kept,) = cache.glob("*.json")
            kept.write_text(json.dumps({k: "0" * 64 for k in first}))
            kept.chmod(0o664)   # a file another may write is never read
            step_code._TREE_DIGESTS.clear()
            self.assertEqual(step_code.code_at(None, self.repo), first)

    def test_an_unreached_definition_removed_moves_nothing(self):
        after = self.edit("pkg/a.py", "def unused():\n    return 1\n\n\n", "")
        self.assertEqual(self.moved(self.base, after), set())
        after2 = self.edit("pkg/b.py", "\n\ndef other():\n    return 0\n", "\n")
        self.assertEqual(self.moved(after, after2), set())

    def test_a_reached_definition_moves_only_the_steps_reaching_it(self):
        only_a = self.edit("pkg/a.py", "return helper(1)", "return helper(1) + 1")
        self.assertEqual(self.moved(self.base, only_a), {"a"})
        both = self.edit("pkg/a.py", "LIMIT = 3", "LIMIT = 4")
        self.assertEqual(self.moved(only_a, both), {"a", "b"})
        only_b = self.edit("pkg/b.py", "* 2", "* 3")
        self.assertEqual(self.moved(both, only_b), {"b"})

    def test_a_listed_module_the_roots_never_reach_is_digested_whole(self):
        after = self.edit("pkg/c.py", "return 5", "return 6")
        self.assertEqual(self.moved(self.base, after), {"b"})

    def test_a_statement_run_on_import_moves_its_module(self):
        after = self.edit("pkg/a.py", "LIMIT = 3\n", "LIMIT = 3\nprint(LIMIT)\n")
        self.assertEqual(self.moved(self.base, after), {"a", "b"})

    def test_json_is_canonical_and_a_value_change_moves_it(self):
        same = self.edit("pkg/rules.json", '{"x": 1, "y": [1, 2]}',
                         '{\n "x": 1,\n "y": [ 1,2 ]\n}\n')
        self.assertEqual(self.moved(self.base, same), set())
        value = self.edit("pkg/rules.json", '"x": 1', '"x": 2')
        self.assertEqual(self.moved(same, value), {"b"})

    def test_a_reordered_json_object_moves_it(self):
        # a table "in schema order" (identifiers.json kinds) is ordered data
        after = self.edit("pkg/rules.json", '{"x": 1, "y": [1, 2]}', '{"y": [1, 2], "x": 1}')
        self.assertEqual(self.moved(self.base, after), {"b"})

    def test_an_import_swapped_to_another_unlisted_target_moves_the_module(self):
        for path in ("pkg/d.py", "pkg/e.py"):   # the same text, neither listed
            (self.repo / path).write_text("def helper(x):\n    return x\n")
        self.commit("d and e")
        on_d = self.edit("pkg/b.py", "from pkg.a import helper", "from pkg.d import helper")
        on_e = self.edit("pkg/b.py", "from pkg.d import helper", "from pkg.e import helper")
        self.assertEqual(self.moved(on_d, on_e), {"b"})
        # a standard-library import rebound under the same name
        stdlib = self.edit("pkg/a.py", "import json\n", "import pickle as json\n")
        self.assertEqual(self.moved(on_e, stdlib), {"a", "b"})

    def test_a_top_level_patch_of_another_module_moves_its_module(self):
        patched = self.edit("pkg/a.py", "LIMIT = 3\n", "LIMIT = 3\njson.dumps = repr\n")
        self.assertEqual(self.moved(self.base, patched), {"a", "b"})
        other = self.edit("pkg/a.py", "json.dumps = repr", "json.dumps = str")
        self.assertEqual(self.moved(patched, other), {"a", "b"})
        item = self.edit("pkg/a.py", "json.dumps = str\n", "json.dumps = str\nREG = {}\n"
                         "REG['k'] = helper\n")
        self.assertEqual(self.moved(other, item), {"a", "b"})

    def test_a_module_dispatching_through_globals_or_sys_modules_is_taken_whole(self):
        for dispatch, imports in (('globals()["un" + "used"]()', ""),
                                  ('getattr(sys.modules[__name__], "un" + "used")()',
                                   "import sys\n")):
            with self.subTest(dispatch=dispatch):
                (self.repo / "pkg" / "a.py").write_text(
                    imports + A.replace("return helper(1)", f"return helper({dispatch})"))
                dyn = self.commit("dispatch")
                # the reached definition no longer names it: only the whole module holds it
                after = self.edit("pkg/a.py", "    return 1\n", "    return 2\n")
                self.assertEqual(self.moved(dyn, after), {"a", "b"})

    def test_an_absent_file_is_absent_and_an_unreadable_one_is_refused(self):
        (self.repo / "pkg" / "c.py").unlink()
        gone = self.commit("drop c")
        self.assertEqual(self.moved(self.base, gone), {"b"})
        (self.repo / "pkg" / "a.py").write_text("def step_a(:\n")
        broken = self.commit("broken")
        with self.assertRaisesRegex(ValueError, "step_code_unreadable"):
            self.code(broken)

    def test_a_v1_table_is_its_byte_digests_and_has_no_roots(self):
        self.table(roots=False)
        v1 = self.commit("v1")
        self.assertIsNone(step_code.step_roots(v1, self.repo))
        self.assertEqual(self.code(v1), step_code.code_digests(v1, self.repo, FILES))
        self.assertNotEqual(self.code(v1), self.code(self.base))

    def test_a_malformed_roots_entry_is_refused(self):
        for bad in ({"a": "pkg.a"}, {"a": [["pkg.a"]]}, {"a": [["", "x"]]}, ["a"]):
            table = {"format": step_code.FORMAT_V2, "steps": FILES, "roots": bad}
            (self.repo / step_code.CODE_FILE).write_text(json.dumps(table))
            ref = self.commit("bad roots")
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "steps_unreadable"):
                step_code.step_roots(ref, self.repo)
        (self.repo / step_code.CODE_FILE).write_text(json.dumps(
            {"format": step_code.FORMAT_V1, "steps": FILES, "roots": ROOTS}))
        with self.assertRaisesRegex(ValueError, "steps_unreadable"):
            step_code.step_roots(self.commit("v1 with roots"), self.repo)


class ServedFixture(Fixture):
    """A served commit under the byte scheme (v1 table) and this tree's v2 table over it."""

    def setUp(self):
        super().setUp()
        self.table(roots=False)
        self.served = self.commit("served under the byte scheme")
        self.table(roots=True)
        self.tree = self.commit("this tree: the v2 table")
        self.specs = {name: sm.Spec((f"{name}:",), tuple(paths)) for name, paths in FILES.items()}


class TransitionPlanTests(ServedFixture):
    """The first plan from a v1 served commit: one record that runs nothing."""

    SCHEMA = {"object_types": {}, "link_types": {}}

    def plan(self, **kw):
        units = {"id": "u0", "units": {"affected": []}, "refused": [], "scope": []}
        rezone = lambda served, new: {"served": served, "values": [], "events": []}
        with mock.patch.object(sm, "ROOTS", step_code.step_roots(None, self.repo)), \
                mock.patch("ontology.schema_migrate.schema_at", return_value=self.SCHEMA), \
                mock.patch("ontology.schema_migrate.diff", return_value=units), \
                mock.patch.object(sm, "rezone_carriers", rezone):
            return sm.plan(self.served, new=self.SCHEMA, repo=self.repo, specs=self.specs,
                           present=sorted(self.specs), **kw)

    def test_same_definitions_run_nothing_and_move_the_held_digest_from_the_served_bytes(self):
        got = self.plan()
        served_bytes = step_code.code_digests(self.served, self.repo, FILES)
        self.assertEqual(got["steps"]["run"], {})
        self.assertEqual(got["steps"]["refused"], [])
        self.assertEqual(got["steps_from"], step_code.steps_digest_of(served_bytes))
        self.assertEqual(got["steps_to"],
                         step_code.steps_digest_of(step_code.code_at(None, self.repo)))
        self.assertNotEqual(got["steps_from"], got["steps_to"])   # one zero-run record
        self.assertEqual(got["id"], sm._sha(["u0", got["steps_from"], got["steps_to"], {}])[:16])

    def test_a_reached_change_in_the_tree_runs_that_step_only(self):
        (self.repo / "pkg" / "b.py").write_text(B.replace("* 2", "* 5"))
        self.commit("b changes")
        got = self.plan()
        self.assertEqual((got["steps"]["run"], got["steps"]["why"]), ({"b": None}, {"b": "code"}))
        self.assertEqual(got["steps_from"], step_code.steps_digest_of(
            step_code.code_digests(self.served, self.repo, FILES)))

    def test_a_pinned_served_code_over_a_v1_commit_needs_its_compare_pin(self):
        pin = step_code.code_digests(self.served, self.repo, FILES)
        with self.assertRaisesRegex(ValueError, "served_compare_code_needed"):
            self.plan(old_code=pin)
        with mock.patch.object(sm, "ROOTS", step_code.step_roots(None, self.repo)):
            compare = sm.compare_code(self.served, self.repo, self.specs)
        got = self.plan(old_code=pin, compare=compare)
        self.assertEqual(got["steps"]["run"], {})
        self.assertEqual(got["steps_from"], step_code.steps_digest_of(pin))

    def test_seeding_from_a_v1_commit_holds_what_its_build_recorded(self):
        self.assertEqual(sm.recorded_steps_digest(self.served, self.repo),
                         self.plan()["steps_from"])


class ComparePinTests(ServedFixture):
    """A build-only served tree's ``--served-compare-code`` pin: made by ``compare-pin``
    and bound to the ``--served-code`` pin it was made beside."""

    def test_the_pin_of_the_served_tree_is_its_compare_code_and_is_bound_to_its_code(self):
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        tree = Path(held.name)
        archive = subprocess.run(["git", "-C", str(self.repo), "archive", self.served],
                                 check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
        with mock.patch.object(sm, "ROOTS", step_code.step_roots(None, self.repo)):
            pin = sm.compare_pin(tree, self.specs)
            want = sm.compare_code(self.served, self.repo, self.specs)
        served = step_code.code_digests(self.served, self.repo, FILES)
        self.assertEqual(pin["code"], want)
        self.assertEqual(pin["of"], step_code.steps_digest_of(served))
        path = Path(held.name) / "pin.json"
        path.write_text(json.dumps(pin))
        self.assertEqual(sm.bound_compare_code(path, served), want)
        other = dict(served, a="0" * 64)   # a pin made beside another served code
        with self.assertRaisesRegex(ValueError, "served_compare_code_unbound"):
            sm.bound_compare_code(path, other)
        path.write_text(json.dumps(dict(pin, of=None)))
        with self.assertRaisesRegex(ValueError, "served_compare_code_unbound"):
            sm.bound_compare_code(path, served)
        path.write_text(json.dumps(dict(pin, steps_digest="steps:" + "0" * 32)))
        with self.assertRaisesRegex(ValueError, "served_code_unverified"):
            sm.bound_compare_code(path, served)


class ThisTreeTests(unittest.TestCase):
    """The real table: every step rooted at the definitions its build runs."""

    def test_the_table_is_v2_and_each_root_is_a_definition_in_a_listed_module(self):
        files, roots = step_code.step_files(), step_code.step_roots()
        self.assertIsNotNone(roots)
        self.assertEqual(set(roots), set(files))
        self.assertEqual(set(files), set(sm.present_steps()))
        for step, pairs in roots.items():
            self.assertTrue(pairs, step)
            for module, name in pairs:
                path = module.replace(".", "/") + ".py"
                self.assertIn(path, files[step], (step, module))
                tree = ast.parse((Path(step_code.ROOT) / path).read_text())
                defined = {n.name for n in tree.body if isinstance(
                    n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                self.assertIn(name, defined, (step, module, name))
        self.assertEqual(step_code.tree_steps_digest(), sm.steps_digest())

    def test_each_root_is_what_the_build_registry_runs(self):
        # operating.build_steps.post_project: each step unwrapped (``_named``,
        # ``_documents_cleaned``) is its root, a closure its root returns, or a closure of
        # post_project calling its root by the global name that root is bound to
        import importlib
        import inspect
        from operating import build_steps
        roots = step_code.step_roots()
        with tempfile.TemporaryDirectory() as state:
            steps = build_steps.post_project(state, "tenant")
        self.assertEqual(set(steps), set(roots))
        for name, step in steps.items():
            fn = inspect.unwrap(step)
            pairs = {(m, n) for m, n in roots[name]}
            targets = [getattr(importlib.import_module(m), n) for m, n in pairs]
            qual = fn.__qualname__
            if "<locals>" not in qual:
                self.assertTrue(any(fn is t for t in targets), (name, qual))
                continue
            factory = (fn.__module__, qual.split(".<locals>")[0])
            if factory in pairs:
                continue
            self.assertEqual(factory, ("operating.build_steps", "post_project"), (name, qual))
            called = [fn.__globals__.get(n) for n in fn.__code__.co_names]
            self.assertTrue(any(c is t for c in called for t in targets), (name, qual))

    def test_the_entry_points_the_delta_and_in_place_runs_import_are_reached(self):
        # ontology.delta / pg_step_inplace / operating.pg.text_remark call these directly
        from ontology.build_keys import code_parts
        wanted = {"mentions": [("ontology.mentions", n) for n in (
                      "candidate_keys", "document_mentions", "cited_page", "MENTION_FEATURES",
                      "source_document_groups")],
                  "text_index": [("ontology.text_index", n) for n in (
                      "_fields", "_sensitive", "FIELD_CAP")]}
        roots = step_code.step_roots()
        for step, parts in wanted.items():
            got = code_parts(roots[step])
            self.assertIsNotNone(got, step)
            for part in parts:
                self.assertIn(part, got, (step, part))


if __name__ == "__main__":
    unittest.main()
