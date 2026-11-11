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
        self.assertEqual(set(sm.SAME_ROWS), {"document_fields", "confirmations"})


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


if __name__ == "__main__":
    unittest.main()
