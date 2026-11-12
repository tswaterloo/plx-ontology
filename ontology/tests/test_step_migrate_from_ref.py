"""A step migration starts from the step code digest the served store holds.

The served build recorded ``steps_digest`` over its own tree's ``STEPS`` (its step names and
code files). When this tree adds a step or a code file to one, the plan's ``steps_from``
must still be that recorded digest, or the apply refuses (``other_steps``) a migration
whose plan showed no refusal. A ``STEPS`` the planner cannot read is refused, never guessed.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ontology import step_migrate as sm

OLD_STEPS = '''
STEPS = {
    "firm": Spec(("crb-mid:",), ("ontology/firm.py",), reads=("mentions",)),
    "mentions": Spec(("mention:",), ("ontology/mentions.py",)),
}
'''


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class FromRefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q")
        (self.repo / "ontology").mkdir()
        for name, text in (("firm.py", "A = 1\n"), ("mentions.py", "B = 1\n"),
                           ("resolve.py", "C = 1\n"), ("step_migrate.py", OLD_STEPS)):
            (self.repo / "ontology" / name).write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "served")
        self.served = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def test_steps_from_is_the_served_builds_digest_after_this_tree_adds_files_and_steps(self):
        recorded = {"firm": sm.Spec(("crb-mid:",), ("ontology/firm.py",)),
                    "mentions": sm.Spec(("mention:",), ("ontology/mentions.py",))}
        held = sm.steps_digest(self.served, self.repo, recorded)   # what the build stored
        # this tree: the firm step reads one more file, and a new step exists
        now = {"firm": sm.Spec(("crb-mid:",), ("ontology/firm.py", "ontology/resolve.py")),
               "mentions": sm.Spec(("mention:",), ("ontology/mentions.py",)),
               "pegging": sm.Spec(("peg:",), ("ontology/pegging.py",))}
        self.assertNotEqual(sm.steps_digest(self.served, self.repo, now), held)
        old = sm.code_at(self.served, self.repo)
        self.assertEqual("steps:" + sm._sha(old)[:32], held)
        # the changed and the new step still run again: their code differs from the served
        new = sm.code_digests(self.served, self.repo, now)
        self.assertNotEqual(old["firm"], new["firm"])
        self.assertNotIn("pegging", old)
        self.assertEqual(old["mentions"], new["mentions"])

    def test_a_steps_table_it_cannot_read_is_refused_and_a_tree_without_one_is_none(self):
        (self.repo / "ontology" / "step_migrate.py").write_text(
            "STEPS = {name: Spec((), files) for name, files in TABLE}\n")
        git(self.repo, "commit", "-qam", "computed")
        with self.assertRaisesRegex(ValueError, "steps_unreadable"):
            sm.code_at(git(self.repo, "rev-parse", "HEAD"), self.repo)
        (self.repo / "ontology" / "step_migrate.py").write_text(
            'STEPS = {"firm": Spec(("x:",), FILES)}\n')
        git(self.repo, "commit", "-qam", "named files")
        with self.assertRaises(ValueError):
            sm.code_at(git(self.repo, "rev-parse", "HEAD"), self.repo)
        git(self.repo, "rm", "-q", "ontology/step_migrate.py")
        git(self.repo, "commit", "-qm", "before step migrations")
        self.assertIsNone(sm.code_at(git(self.repo, "rev-parse", "HEAD"), self.repo))



class SameRowsTests(unittest.TestCase):
    """A step code change pinned as writing the same rows (``SAME_ROWS``) does not run the
    step again; any other change of its code does, and the steps digest still moves."""

    def test_a_pinned_pair_is_no_reason_and_any_other_is(self):
        schema = {"object_types": {}, "link_types": {}}
        old = {"document_fields": "a", "confirmations": "c"}
        pinned = {"document_fields": frozenset({("a", "b")})}
        plan = sm.plan_steps(schema, schema, old, {**old, "document_fields": "b"},
                             set(old), same_rows=pinned, inputs={})
        self.assertEqual(plan["run"], {})
        other = sm.plan_steps(schema, schema, old, {**old, "document_fields": "x"},
                              set(old), same_rows=pinned, inputs={})
        self.assertEqual((other["run"], other["why"]), ({"document_fields": None},
                                                        {"document_fields": "code"}))
        # a pin of one step is none for another step with the same digests
        crossed = sm.plan_steps(schema, schema, {"confirmations": "a"},
                                {"confirmations": "b"}, {"confirmations"},
                                same_rows=pinned, inputs={})
        self.assertEqual(crossed["run"], {"confirmations": None})

    def test_the_pins_are_the_declared_schema_move_from_the_served_build(self):
        # fb77cd38eb: the step code the 11-10 build serves for these two steps;
        # a7f9a137da: the tree with c7035f86f1's DDL move (nothing else in their files)
        try:
            old, new = sm.code_digests("fb77cd38eb"), sm.code_digests("a7f9a137da")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if old == sm.code_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        for name in ("document_fields", "confirmations"):
            self.assertEqual(sm.SAME_ROWS[name], frozenset({(old[name], new[name])}))
        self.assertEqual(set(sm.SAME_ROWS), {"document_fields", "confirmations", "with_owners"})

    def test_the_owner_pin_is_the_list_membership_change_from_the_served_steps(self):
        # be37cefd36: the step code the in-place run of 11-11 serves; ed679e9772: the last
        # distribution-list change of people_exclusion (nothing else in with_owners' files)
        try:
            old, new = sm.code_digests("be37cefd36"), sm.code_digests("ed679e9772")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if old == sm.code_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        self.assertIn((old["with_owners"], new["with_owners"]), sm.SAME_ROWS["with_owners"])
        self.assertEqual({k for k in old if old[k] != new[k]}, {"with_owners"})

    def test_the_second_owner_pin_is_only_the_added_guard_fingerprint(self):
        # 0bfbe020a8: the step code Postgres serves after the first owner pin; defb027118:
        # PeopleExclusion.fingerprint added (the commitments cursor), nothing else in the
        # step's files. The two pins are the whole with_owners entry.
        import ast
        import subprocess
        try:
            old, new = sm.code_digests("0bfbe020a8"), sm.code_digests("defb027118")
            first = sm.code_digests("be37cefd36"), sm.code_digests("ed679e9772")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if old == sm.code_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        self.assertEqual({k for k in old if old[k] != new[k]}, {"with_owners"})
        self.assertEqual(sm.SAME_ROWS["with_owners"], frozenset({
            (first[0]["with_owners"], first[1]["with_owners"]),
            (old["with_owners"], new["with_owners"])}))

        def tree(ref, path, drop=False):
            done = subprocess.run(["git", "-C", str(sm.ROOT), "show", f"{ref}:{path}"],
                                  capture_output=True, check=True)
            module = ast.parse(done.stdout)
            if drop:   # the one added method, and nothing else, comes out
                for node in module.body:
                    if isinstance(node, ast.ClassDef) and node.name == "PeopleExclusion":
                        kept = [n for n in node.body if not (
                            isinstance(n, ast.FunctionDef) and n.name == "fingerprint")]
                        self.assertEqual(len(node.body) - len(kept), 1)
                        node.body = kept
            return ast.dump(module)
        for path in sm.STEPS["with_owners"].code:
            drop = path == "integrations/people_exclusion.py"
            self.assertEqual(tree("0bfbe020a8", path), tree("defb027118", path, drop), path)
            if path.startswith("ontology/"):   # the step never reaches the new method
                self.assertNotIn("fingerprint", tree("defb027118", path))

    def test_the_step_guard_decides_the_same_under_the_pinned_change(self):
        # with_owners' guard (from_env: never bound, nothing attached) decides object ids
        # alone, as at be37cefd36; an address stays undecided (the step skips it)
        import subprocess
        import types
        from integrations import people_exclusion as new
        done = subprocess.run(["git", "-C", str(sm.ROOT), "show",
                               "be37cefd36:integrations/people_exclusion.py"],
                              capture_output=True)
        if done.returncode:
            self.skipTest("commit unavailable")
        old = types.ModuleType("integrations.people_exclusion")
        old.__file__, old.__package__ = new.__file__, "integrations"
        exec(compile(done.stdout, new.__file__, "exec"), old.__dict__)
        barred, other = "22222222-2222-2222-2222-222222222222", \
            "33333333-3333-3333-3333-333333333333"
        values = [barred, other, barred.upper(), "omar@plx.test", "list@plx.test", "", None,
                  "not-an-id", 7]
        for environ in ({"PLX_EXCLUDED_PEOPLE_OIDS": barred, "PLX_EXCLUSION_ENV_FILE": ""},
                        {"PLX_EXCLUSION_ENV_FILE": ""}):
            def judged(mod):
                guard = mod.PeopleExclusion.from_env(mod.environ_with_exclusion(environ))
                return guard.unavailable, [(guard.decides(v), guard.decides(v)
                                            and guard.is_excluded(v)) for v in values]
            self.assertEqual(judged(old), judged(new))
            if "PLX_EXCLUDED_PEOPLE_OIDS" in environ:   # barred by id; addresses undecided
                self.assertEqual(judged(new)[1][:4], [(True, True), (True, False),
                                                      (True, True), (False, False)])
            else:   # no list: fail closed, nothing decided
                self.assertEqual(judged(new)[0], "excluded_people_oids_absent")


class DeclaredInputsTests(unittest.TestCase):
    """A step's declared inputs hold every type its rules read, so a migration touching one
    runs it again (a type it reads but does not declare would keep stale links)."""

    def test_pegging_and_owner_steps_declare_every_type_their_rules_read(self):
        import json
        from ontology import owner_links
        from ontology.schema import load_schema

        schema = load_schema()
        rules = json.loads((Path(sm.ROOT) / "ontology/schema/pegging.json").read_text("utf-8"))
        named: set[str] = set()

        def walk(value):
            if isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)
            elif isinstance(value, str) and value.split(".")[0] in schema.object_types:
                named.add(value.split(".")[0])
        walk(rules)
        self.assertIn("AssemblyOrderAllocation", named)
        self.assertLessEqual(named, sm.STEPS["supply_pegging"].declared(schema))
        self.assertLessEqual({"Person", *owner_links.OWNERS},
                             sm.STEPS["with_owners"].declared(schema))
        for step in ("supply_pegging", "with_owners"):   # both write through mentions' rules
            self.assertIn("ontology/mentions.py", sm.STEPS[step].code)



class ServedCodePinTests(unittest.TestCase):
    """``--served-code``: the served step code of a build whose tree no commit holds. Taken
    only when it hashes to its own ``steps_digest``; anything else fails closed."""

    def pin(self, payload):
        import json
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / "pin.json"
        path.write_text(json.dumps(payload))
        return path

    def test_a_pin_that_hashes_to_its_digest_is_the_served_code(self):
        code = sm.code_digests()
        got = sm.served_code(self.pin({"code": code,
                                       "steps_digest": "steps:" + sm._sha(code)[:32]}))
        self.assertEqual(got, code)
        # the plan's steps_from is then that digest, and this tree against it runs nothing
        the_plan = sm.plan("HEAD", old_code=got)
        self.assertEqual(the_plan["steps_from"], "steps:" + sm._sha(code)[:32])
        self.assertEqual(the_plan["steps"]["run"], {})

    def test_an_edited_unknown_or_empty_pin_is_refused(self):
        code = sm.code_digests()
        digest = "steps:" + sm._sha(code)[:32]
        edited = {**code, "text_index": "0" * 64}
        for payload in ({"code": edited, "steps_digest": digest},
                        {"code": code, "steps_digest": "steps:" + "0" * 32},
                        {"code": code},
                        {"code": {}, "steps_digest": "steps:" + sm._sha({})[:32]},
                        {"code": {"text_index": 1}, "steps_digest": digest},
                        ["not", "a", "pin"]):
            with self.subTest(payload=str(payload)[:60]):
                with self.assertRaisesRegex(ValueError, "served_code_unverified"):
                    sm.served_code(self.pin(payload))


class PartsInOrderJobTests(unittest.TestCase):
    """``_parts_in_order`` names a ``__main__`` job by this module's import name (a
    static table, never a computed lookup): the three jobs resolve, any other name is
    refused before a worker starts."""

    def run_with(self, name):
        from concurrent import futures
        from unittest import mock

        def job(path, *args):
            return path
        job.__module__, job.__name__ = "__main__", name
        submitted = []

        class Pool:
            def __init__(self, *a, **k):
                pass

            def submit(self, fn, *a):
                submitted.append(fn)
                done = futures.Future()
                done.set_result(a[0])
                return done

            def shutdown(self, **k):
                pass
        with mock.patch.object(futures, "ProcessPoolExecutor", Pool):
            return list(sm._parts_in_order(job, [("x",)], 2, Path("/tmp"))), submitted

    def test_the_three_jobs_resolve_to_this_modules_own(self):
        for name in ("_served_value_part", "_layer_value_part", "_layer_link_part"):
            parts, submitted = self.run_with(name)
            self.assertIs(submitted[0], getattr(sm, name))
            self.assertEqual(len(parts), 1)

    def test_an_unknown_main_job_is_refused_before_any_worker(self):
        with self.assertRaises(KeyError):
            self.run_with("os_system")


if __name__ == "__main__":
    unittest.main()
