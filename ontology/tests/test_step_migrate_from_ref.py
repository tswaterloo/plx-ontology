"""A step migration starts from the step code digest the served store holds.

The served build recorded ``steps_digest`` over its own tree's ``STEPS`` (its step names and
code files). When this tree adds a step or a code file to one, the plan's ``steps_from``
must still be that recorded digest, or the apply refuses (``other_steps``) a migration
whose plan showed no refusal. A ``STEPS`` the planner cannot read is refused, never guessed.
"""

from __future__ import annotations

import json
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


def _defs_at(ref: str, path: str):
    """(top-level definitions by name, imported names -> (module tail, name)) of ``path`` at
    ``ref`` (git show)."""
    import ast
    module = ast.parse(subprocess.run(["git", "-C", str(sm.ROOT), "show",
                                       f"{ref}:{path}"], capture_output=True,
                                      check=True).stdout)
    defs, imported = {}, {}
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                defs.update({x.id: node for x in ast.walk(t) if isinstance(x, ast.Name)})
        else:
            defs[f"<{node.lineno}>"] = node
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module:
            for al in node.names:
                imported[al.asname or al.name] = (node.module.split(".")[-1], al.name)
        elif isinstance(node, (ast.ImportFrom, ast.Import)):
            for al in node.names:
                imported[al.asname or al.name.split(".")[-1]] = (
                    al.name.split(".")[-1], None)
    return defs, imported


def _reached(b, mods, entry):
    """(module, name) pairs ``entry`` reaches over the trees ``b`` (:func:`_defs_at`) of
    ``mods``: names, attributes of imported modules, and an attribute of an object of
    unknown origin as that name in any module."""
    import ast
    seen, todo = set(), [entry]
    while todo:
        m, n = todo.pop()
        if (m, n) in seen:
            continue
        seen.add((m, n))
        defs, imported = b[m]
        if n in imported:
            mod, name = imported[n]
            if mod in mods and name:
                todo.append((mod, name))
            continue
        node = defs.get(n)
        if node is None:
            continue
        for x in ast.walk(node):
            if isinstance(x, ast.Name):
                todo.append((m, x.id))
            elif isinstance(x, ast.Attribute):
                base = x.value.id if isinstance(x.value, ast.Name) else None
                target = imported.get(base, (None,))[0] if base else None
                if target in mods:
                    todo.append((target, x.attr))
                elif base not in defs or isinstance(
                        getattr(defs[base], "value", None), (ast.Name, ast.Attribute)):
                    # an object of unknown origin (or a bare alias): the name in
                    # any module. A module-level name this module builds itself
                    # (PROFILES_PATH.read_text) is followed through its own
                    # definition (its Name above).
                    todo += [(mm, x.attr) for mm in mods]
    return seen


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
        # what the build stored: a commit of no table records its files' bytes (v1)
        held = "steps:" + sm._sha(sm.byte_digests(self.served, self.repo, recorded))[:32]
        # this tree: the firm step reads one more file, and a new step exists
        now = {"firm": sm.Spec(("crb-mid:",), ("ontology/firm.py", "ontology/resolve.py")),
               "mentions": sm.Spec(("mention:",), ("ontology/mentions.py",)),
               "pegging": sm.Spec(("peg:",), ("ontology/pegging.py",))}
        self.assertNotEqual("steps:" + sm._sha(sm.byte_digests(self.served, self.repo, now))[:32],
                            held)
        old = sm.code_at(self.served, self.repo)
        self.assertEqual("steps:" + sm._sha(old)[:32], held)
        # the changed and the new step still run again: their code differs from the served
        new = sm.byte_digests(self.served, self.repo, now)
        self.assertNotEqual(old["firm"], new["firm"])
        self.assertNotIn("pegging", old)
        self.assertEqual(old["mentions"], new["mentions"])

    def test_an_edit_to_the_reading_code_runs_document_fields_again(self):
        # document_fields parses what integrations.captured_text reads, and
        # integrations.document_understanding decides that text (hidden HTML, messages):
        # an edit to either changes the step's output, so its code digest must move
        # (the real STEPS table), and steps that read neither keep theirs.
        (self.repo / "integrations").mkdir()
        for name in ("captured_text.py", "document_understanding.py"):
            (self.repo / "integrations" / name).write_text("X = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "readers")
        before = git(self.repo, "rev-parse", "HEAD")
        for name in ("captured_text.py", "document_understanding.py"):
            (self.repo / "integrations" / name).write_text("X = 2\n")
            git(self.repo, "commit", "-qam", name)
            after = git(self.repo, "rev-parse", "HEAD")
            old = sm.code_digests(before, self.repo)
            new = sm.code_digests(after, self.repo)
            self.assertNotEqual(old["document_fields"], new["document_fields"], name)
            moved = {step for step in old if old[step] != new[step]}
            self.assertEqual(moved, {step for step, spec in sm.STEPS.items()
                                     if f"integrations/{name}" in spec.code}, name)
            before = after

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



    def test_a_ref_with_the_code_table_is_read_from_it_and_this_tree_records_its_own(self):
        """From the table (``ontology/step_code.json``) on, a commit's step code is read from
        it, whatever its ``STEPS`` look like; a malformed table is refused. This tree's
        build records exactly ``steps_digest()`` (``project.build_store`` reads the table,
        ``STEPS`` take their code from it)."""
        from ontology import step_code
        (self.repo / "ontology" / "step_migrate.py").write_text(
            "STEPS = {name: Spec((), CODE[name]) for name in CODE}\n")
        (self.repo / "ontology" / "step_code.json").write_text(json.dumps(
            {"steps": {"firm": ["ontology/firm.py", "ontology/resolve.py"],
                       "mentions": ["ontology/mentions.py"]}}))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "tabled")
        now = git(self.repo, "rev-parse", "HEAD")
        files = {"firm": sm.Spec((), ("ontology/firm.py", "ontology/resolve.py")),
                 "mentions": sm.Spec((), ("ontology/mentions.py",))}
        # a v1 table (no roots): that commit's build recorded bytes
        self.assertEqual(sm.code_at(now, self.repo), sm.byte_digests(now, self.repo, files))
        self.assertNotEqual(sm.code_at(now, self.repo), sm.code_at(self.served, self.repo))
        for bad in ({"steps": ["firm"]}, {"steps": {"firm": [""]}}, {"other": {}}):
            (self.repo / "ontology" / "step_code.json").write_text(json.dumps(bad))
            git(self.repo, "commit", "-qam", "bad table")
            with self.assertRaisesRegex(ValueError, "steps_unreadable"):
                sm.code_at(git(self.repo, "rev-parse", "HEAD"), self.repo)
        self.assertEqual(step_code.tree_steps_digest(), sm.steps_digest())
        self.assertEqual({name: spec.code for name, spec in sm.STEPS.items()},
                         step_code.step_files())


def _served_code(ref):
    """A commit's step code as its own table lists it (``step_migrate.code_at``, what a plan
    from that served commit reads), else its files under this tree's table: a file this tree
    adds to a step (``ontology/mention_occurrences.py``) moves no pinned digest of before."""
    return sm.code_at(ref) or sm.byte_digests(ref)


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
        # a7f9a137da: the tree with c7035f86f1's DDL move (nothing else in their files).
        # Pinned over document_fields' code files of then (its reading modules joined the
        # list later: a pair over the old list never matches a digest over the new one).
        import dataclasses
        then = dict(sm.STEPS, document_fields=dataclasses.replace(
            sm.STEPS["document_fields"], code=("ontology/document_fields.py",
                                               "ontology/schema/document_fields.json")))
        try:
            old, new = (sm.byte_digests("fb77cd38eb", specs=then),
                        sm.byte_digests("a7f9a137da", specs=then))
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if old == sm.byte_digests("no-such-ref-xyz", specs=then):
            self.skipTest("commits unavailable")
        for name in ("document_fields", "confirmations"):
            self.assertIn((old[name], new[name]), sm.SAME_ROWS[name])
        self.assertEqual(len(sm.SAME_ROWS["confirmations"]), 1)
        self.assertEqual(set(sm.SAME_ROWS), {"document_fields", "confirmations", "with_owners",
                                             "twins", "your_reference", "with_people",
                                             "sender_parties", "unique_mentions",
                                             "supply_pegging", "text_index",
                                             "sender_corroboration", "task_names"})

    def test_the_pool_initializer_pins_go_from_the_4b_served_steps(self):
        # a725d74f07: the step code train 4b serves; 8a3ac9ab84: perm-box-1114-steps' pool
        # initializer change, nothing else in these two steps' files
        try:
            served, after = _served_code("a725d74f07"), _served_code("8a3ac9ab84")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if after == sm.byte_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        for name in ("text_index", "sender_corroboration"):
            self.assertEqual(sm.SAME_ROWS[name], frozenset({(served[name], after[name])}))

    def test_the_batched_reads_pins_are_only_nw13_over_the_served_steps(self):
        # f5c2561ca0: the step code Postgres serves (11-13); 40fc2e1917: the train's part 1
        # before NW-13 (these four steps' code as served); 0f1f7b75ca: NW-13's batched reads
        # (7edac7f84d) on it, nothing else. The pin goes from the served digest to NW-13's.
        try:
            served, before, after = (_served_code("f5c2561ca0"),
                                     _served_code("40fc2e1917"),
                                     _served_code("0f1f7b75ca"))
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if after == sm.byte_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        for name in ("sender_parties", "unique_mentions", "supply_pegging"):
            self.assertEqual(served[name], before[name])
            self.assertIn((served[name], after[name]), sm.SAME_ROWS[name])
        self.assertEqual(len(sm.SAME_ROWS["sender_parties"]), 1)
        self.assertEqual(len(sm.SAME_ROWS["supply_pegging"]), 1)
        self.assertIn((served["with_people"], after["with_people"]), sm.SAME_ROWS["with_people"])
        self.assertEqual({k for k in before if before[k] != after[k]},
                         {"sender_parties", "unique_mentions", "supply_pegging",
                          "with_people", "with_owners"})

    def test_the_one_record_window_pin_is_only_mention_firm_over_the_served_steps(self):
        # a725d74f07: the step code Postgres serves after the 11-14 train (unique_mentions as
        # 66a7a60c45..c63ee71f36); 88fdc30dff: NW-13's window same_record read on it, nothing else
        try:
            served, after = _served_code("a725d74f07"), _served_code("88fdc30dff")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if after == sm.byte_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        self.assertEqual({k for k in served if served[k] != after[k]}, {"unique_mentions"})
        self.assertIn((served["unique_mentions"], after["unique_mentions"]),
                      sm.SAME_ROWS["unique_mentions"])
        self.assertEqual(len(sm.SAME_ROWS["unique_mentions"]), 2)

    def test_the_zstd_held_forms_pins_go_from_train_3s_steps(self):
        # c387ba7453: train 3's PIN; e6640ecb53: blob-zstd merged onto it with the held
        # tests through blob_pack and the code-equivalence tables. Of the steps' files only
        # document_fields.py and captured_text.py moved: document_fields and with_people
        try:
            served, after = sm.byte_digests("c387ba7453"), sm.byte_digests("e6640ecb53")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if after == sm.byte_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        self.assertEqual({k for k in served if served[k] != after[k]},
                         {"document_fields", "with_people"})
        for name in ("document_fields", "with_people"):
            self.assertIn((served[name], after[name]), sm.SAME_ROWS[name])
        # two byte pins and the zstd pin again in the v2 scheme (test below)
        self.assertEqual(len(sm.SAME_ROWS["document_fields"]), 3)

    def test_the_v2_repins_are_train_4s_byte_pins_in_this_trees_scheme(self):
        # c387ba7453: train 3's PIN (served, v1); f9bdbbbcb3: the PIN_4 landing, whose byte
        # pins from c387ba7453 are COLLAPSED unique_mentions/task_ids and SAME_ROWS
        # document_fields/task_names. A v2 tree compares both under its own scheme
        # (compare_code), so each such change is pinned again as that pair; with_people's
        # zstd change reaches no definition its roots reach and moves no v2 digest.
        if sm.code_at("f9bdbbbcb3") is None or sm.code_at("c387ba7453") is None:
            self.skipTest("commits unavailable")
        served, pin4 = sm.compare_code("c387ba7453"), sm.compare_code("f9bdbbbcb3")
        moved = {k for k in pin4 if served.get(k) != pin4[k]}
        self.assertEqual(moved, {"document_fields", "task_names", "unique_mentions", "task_ids"})
        for name in ("unique_mentions", "task_ids"):
            self.assertIn((served[name], pin4[name]), sm.COLLAPSED[name])
            self.assertEqual(len(sm.COLLAPSED[name]), 2)   # the byte pin and this one
        for name in ("document_fields", "task_names"):
            self.assertIn((served[name], pin4[name]), sm.SAME_ROWS[name])
        self.assertEqual(len(sm.SAME_ROWS["task_names"]), 2)

    def test_the_parted_alias_pin_is_with_people_alone(self):
        # TRAIN2-B1 (95656b3e63, onto M 988f5f55fa): alias_pairs filtered by object part.
        # From train 4's PIN only with_people's v2 digest moves; from train 3's and 4's
        # alike its pair is pinned
        if sm.code_at("95656b3e63") is None or sm.code_at("f9bdbbbcb3") is None \
                or sm.code_at("c387ba7453") is None:
            self.skipTest("commits unavailable")
        after, pin4 = sm.compare_code("95656b3e63"), sm.compare_code("f9bdbbbcb3")
        self.assertEqual({k for k in after if pin4.get(k) != after[k]}, {"with_people"})
        for served in (sm.compare_code("c387ba7453"), pin4):
            self.assertIn((served["with_people"], after["with_people"]),
                          sm.SAME_ROWS["with_people"])

    def test_the_zstd_with_people_pin_is_code_the_step_never_reaches(self):
        # every name blob-zstd changed in document_fields and captured_text (c387ba7453 ->
        # e6640ecb53) is out of reach of the with_people entry (people_links_step)
        mods = {"people_links": "ontology/people_links.py",
                "sender_parties": "ontology/sender_parties.py",
                "mentions": "ontology/mentions.py",
                "document_fields": "ontology/document_fields.py",
                "captured_text": "integrations/captured_text.py",
                "document_understanding": "integrations/document_understanding.py",
                "blob_pack": "integrations/blob_pack.py"}
        import ast
        try:
            a = {m: _defs_at("c387ba7453", p) for m, p in mods.items()}
            b = {m: _defs_at("e6640ecb53", p) for m, p in mods.items()}
        except subprocess.CalledProcessError as exc:
            self.skipTest(f"commits unavailable: {exc}")
        changed = {(m, n) for m in mods for n in set(a[m][0]) | set(b[m][0])
                   if n not in a[m][0] or n not in b[m][0]
                   or ast.dump(a[m][0][n]) != ast.dump(b[m][0][n])}
        self.assertEqual({m for m, _n in changed},
                         {"document_fields", "captured_text", "blob_pack"})
        names = {n for _m, n in changed if not n.startswith("<")}
        self.assertTrue({"_bytes_held", "blob_held", "reading_kept", "read_blob",
                         "readings_code", "container_members"} <= names, names)
        self.assertIn(("document_fields", "_bytes_held"),
                      _reached(b, mods, ("document_fields", "_documents")))   # the probe sees it
        step = _reached(b, mods, ("people_links", "people_links_step"))
        self.assertIn(("document_fields", "profiles"), step)
        self.assertEqual(changed & step, set())

    def test_the_owner_pin_is_the_list_membership_change_from_the_served_steps(self):
        # be37cefd36: the step code the in-place run of 11-11 serves; ed679e9772: the last
        # distribution-list change of people_exclusion (nothing else in with_owners' files)
        try:
            old, new = _served_code("be37cefd36"), _served_code("ed679e9772")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if old == sm.byte_digests("no-such-ref-xyz"):
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
            old, new = _served_code("0bfbe020a8"), _served_code("defb027118")
            first = _served_code("be37cefd36"), _served_code("ed679e9772")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if old == sm.byte_digests("no-such-ref-xyz"):
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

    def test_the_twin_pins_are_a_party_judge_change_neither_step_reaches(self):
        # 4029b28005: main with the served step code; 2de675a044: C-07 (the two_companies
        # outcome of _PartyTwin.judge) merged with identity-1 (_PartyTwin.partners: stored-key
        # links only). twins and your_reference read identity_scoring.py only for code
        # twins_parties and key_joins run; a change they reach is no pin.
        import ast
        import subprocess
        try:
            old, new = _served_code("4029b28005"), _served_code("2de675a044")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if old == sm.byte_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        for name in ("twins", "your_reference"):
            self.assertEqual(sm.SAME_ROWS[name], frozenset({(old[name], new[name])}))
        self.assertEqual({k for k in old if old[k] != new[k]},
                         {"twins", "twins_parties", "your_reference", "key_joins", "mentions",
                          "with_owners", "with_people"})

        def tops(ref):
            src = subprocess.run(["git", "-C", str(sm.ROOT), "show",
                                  f"{ref}:ontology/identity_scoring.py"],
                                 capture_output=True, check=True).stdout
            out = {}
            for node in ast.parse(src).body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    out[node.name] = node
                elif isinstance(node, ast.Assign):
                    out.update({t.id: node for t in node.targets if isinstance(t, ast.Name)})
                else:
                    out[f"<{node.lineno}>"] = node
            return out
        a, b = tops("4029b28005"), tops("2de675a044")
        changed = {n for n in set(a) | set(b)
                   if n not in a or n not in b or ast.dump(a[n]) != ast.dump(b[n])}
        self.assertEqual(changed, {"_PARTY_OUTCOMES", "_PartyTwin"})

        def reached(entry):   # every name and attribute name read, classes whole
            seen, todo = set(), [entry]
            while todo:
                n = todo.pop()
                if n in seen or n not in b:
                    continue
                seen.add(n)
                todo.extend({x.id for x in ast.walk(b[n]) if isinstance(x, ast.Name)} |
                            {x.attr for x in ast.walk(b[n]) if isinstance(x, ast.Attribute)})
            return seen
        self.assertEqual(changed & reached("party_twin_step"), changed)   # the probe sees it
        for entry in ("twin_step", "your_reference_step"):
            self.assertEqual(changed & reached(entry), set(), entry)

    def test_the_with_people_pin_is_a_parse_change_the_step_never_reaches(self):
        # 062ecdc47b: the served step code (FROM of the combined migration); 7513ed0816: its
        # part 1 tree (DOC-002's _parse compound unit; the alias of people_links left for
        # part 2, its _excluder kept for owner_links). Names resolve by module: a module's
        # own definitions, else what it imports from another of the step's modules.
        import ast
        import subprocess
        try:
            old, new = _served_code("062ecdc47b"), _served_code("7513ed0816")
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if old == sm.byte_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        self.assertIn((old["with_people"], new["with_people"]), sm.SAME_ROWS["with_people"])
        mods = {"people_links": "ontology/people_links.py",
                "sender_parties": "ontology/sender_parties.py",
                "mentions": "ontology/mentions.py",
                "document_fields": "ontology/document_fields.py"}
        self.assertEqual(set(sm.STEPS["with_people"].code) - set(mods.values()),
                         {"ontology/schema/document_fields.json"})

        def parsed(ref, path):
            return ast.parse(subprocess.run(["git", "-C", str(sm.ROOT), "show",
                                             f"{ref}:{path}"], capture_output=True,
                                            check=True).stdout)

        def tops(module):
            out, imported = {}, {}
            for node in module.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    out[node.name] = node
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                        out.update({x.id: node for x in ast.walk(t) if isinstance(x, ast.Name)})
                else:
                    out[f"<{node.lineno}>"] = node
            for node in ast.walk(module):   # imports anywhere (a function's own too)
                if isinstance(node, ast.ImportFrom) and node.module:
                    for al in node.names:
                        imported[al.asname or al.name] = (node.module.split(".")[-1], al.name)
                elif isinstance(node, ast.ImportFrom) or isinstance(node, ast.Import):
                    for al in node.names:
                        name = al.name.split(".")[-1]
                        imported[al.asname or name] = (name, None)
            return out, imported
        a = {m: tops(parsed("062ecdc47b", p)) for m, p in mods.items()}
        b = {m: tops(parsed("7513ed0816", p)) for m, p in mods.items()}
        changed = {(m, n) for m in mods for n in set(a[m][0]) | set(b[m][0])
                   if n not in a[m][0] or n not in b[m][0]
                   or ast.dump(a[m][0][n]) != ast.dump(b[m][0][n])}
        self.assertEqual(changed, {("document_fields", "_parse"),
                                   ("document_fields", "_COMPOUND_UNIT"),
                                   ("people_links", "_excluder")})

        def reached(entry):   # (module, name) pairs; an attribute of a module is its name
            seen, todo = set(), [entry]
            while todo:
                m, n = todo.pop()
                if (m, n) in seen:
                    continue
                seen.add((m, n))
                defs, imported = b[m]
                if n in imported:
                    mod, name = imported[n]
                    if mod in mods and name:
                        todo.append((mod, name))
                    continue
                node = defs.get(n)
                if node is None:
                    continue
                for x in ast.walk(node):
                    if isinstance(x, ast.Name):
                        todo.append((m, x.id))
                    elif isinstance(x, ast.Attribute):
                        base = x.value.id if isinstance(x.value, ast.Name) else None
                        target = imported.get(base, (None,))[0] if base else None
                        if target in mods:
                            todo.append((target, x.attr))
                        else:   # a method or an unknown object's: every module's name
                            todo += [(mm, x.attr) for mm in mods]
            return seen
        self.assertEqual(changed & reached(("document_fields", "_fields_read")),
                         {("document_fields", "_parse"),
                          ("document_fields", "_COMPOUND_UNIT")})   # the probe sees it
        step = reached(("people_links", "people_links_step"))
        self.assertIn(("document_fields", "profiles"), step)   # it crosses modules
        self.assertEqual(changed & step, set())

    def test_the_with_people_pin_for_the_certificate_lot_link_is_code_the_step_never_reaches(self):
        # 062ecdc47b: the served step code; 7513ed0816: the part 1 tree before CT-D-06;
        # 5bb94a934a: the part 1 tree with CertificateOfAnalysis.for_lot (all inside
        # document_fields) and DOC-001/003/004/006 (document_fields and the reading modules
        # it imports). Every name they gained or changed is out of reach of the with_people
        # entry (people_links_step), which crosses into document_fields only through
        # ``profiles``; no other module of the step moved.
        import ast
        import subprocess
        try:
            served, before, after = (sm.byte_digests(r) for r in
                                     ("062ecdc47b", "7513ed0816", "5bb94a934a"))
        except Exception as exc:   # noqa: BLE001 - a clone without those commits
            self.skipTest(f"commits unavailable: {exc}")
        if served == sm.byte_digests("no-such-ref-xyz"):
            self.skipTest("commits unavailable")
        self.assertEqual(sm.SAME_ROWS["with_people"] & {(served["with_people"],
                                                         after["with_people"])},
                         {(served["with_people"], after["with_people"])})
        mods = {"people_links": "ontology/people_links.py",
                "sender_parties": "ontology/sender_parties.py",
                "mentions": "ontology/mentions.py",
                "document_fields": "ontology/document_fields.py",
                "captured_text": "integrations/captured_text.py",
                "document_understanding": "integrations/document_understanding.py"}

        a = {m: _defs_at("7513ed0816", p) for m, p in mods.items()}
        b = {m: _defs_at("5bb94a934a", p) for m, p in mods.items()}

        def reached(entry):
            return _reached(b, mods, entry)
        changed = {(m, n) for m in mods for n in set(a[m][0]) | set(b[m][0])
                   if n not in a[m][0] or n not in b[m][0]
                   or ast.dump(a[m][0][n]) != ast.dump(b[m][0][n])}
        self.assertEqual({m for m, _n in changed},
                         {"document_fields", "captured_text", "document_understanding"})
        names = {n for _m, n in changed if not n.startswith("<")}
        self.assertTrue({"store_document_links", "_lot_links", "_holder_candidates",
                         "_product_via", "date_order", "carrier_promises", "_fields_of",
                         "reading_kept", "_HTMLText"} <= names, names)

        self.assertIn(("document_fields", "store_document_links"),
                      reached(("document_fields", "store_document_links")))   # the probe sees it
        # and follows a reading into the reading modules (the document_fields step does)
        self.assertIn(("document_understanding", "_HTMLText"),
                      reached(("document_fields", "_documents")))
        step = reached(("people_links", "people_links_step"))
        self.assertIn(("document_fields", "profiles"), step)
        self.assertEqual(changed & step, set())
        # the digest differs only by these files: the other steps' code did not move
        self.assertEqual({k for k in served if served[k] != after[k]} - {"document_fields"},
                         {k for k in served if served[k] != before[k]} - {"document_fields"}
                         | {"with_people"})

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


class GrammarIsMentionsCodeTests(unittest.TestCase):
    """The identifier grammar is the mentions step's extractor: a pattern change alone
    re-derives its links (live 2026-11-12: the narrower petra_po pattern left 58,086
    served mention links the old grammar made, while the step's own files were unchanged)."""

    def test_a_grammar_pattern_change_runs_the_mentions_step_again(self):
        import shutil
        mentions = {"mentions": sm.STEPS["mentions"]}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for path in mentions["mentions"].code:
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(Path(sm.ROOT) / path, repo / path)
            before = sm.code_digests(None, repo, mentions)
            grammar = repo / "ontology/schema/identifiers.json"
            grammar.write_text(grammar.read_text().replace("\\\\d{3,9}(?:-FM", "\\\\d{1,9}(?:-FM"))
            after = sm.code_digests(None, repo, mentions)
        self.assertNotEqual(before["mentions"], after["mentions"])
        schema = {"object_types": {}, "link_types": {}}
        plan = sm.plan_steps(schema, schema, before, after, {"mentions"}, mentions, inputs={})
        self.assertEqual((plan["run"], plan["why"]), ({"mentions": None}, {"mentions": "code"}))


class GrammarMovesIdentifierRowsTests(unittest.TestCase):
    """A kind's parse decides the identifier rows a projection unit writes for the
    properties carrying it, and no schema slice holds the grammar: a looser Message-ID
    pattern (CT-D-09) left the served Email rows without the identifiers the new parse
    gives, under a plan that showed no unit to rewrite."""

    def test_only_a_rule_change_moves_a_kind_and_an_unknown_section_is_named(self):
        kind = {"value_pattern": "<[^<>]+>", "normalizer": "message_id",
                "description": "old words", "forms": [{"form": "a", "example": "<x@y>"}]}
        served = {"kinds": {"internet_message_id": kind, "gone": {"value_pattern": "x"}},
                  "mention_profiles": {"p": 1}, "tiers": {"a": 1}, "description": "d"}
        prose = {**served, "description": "other words", "kinds": {
            **served["kinds"], "internet_message_id": {
                **kind, "description": "new words",
                "forms": [{"form": "a", "example": "<z@y>"}]}}}
        self.assertEqual(sm.grammar_changes(served, prose),
                         {"kinds": [], "sections": [], "widened": []})
        moved = {**served, "mention_profiles": {"p": 2}, "tiers": {"a": 2}, "kinds": {
            "internet_message_id": {**kind, "value_pattern": "[!-~]{1,998}"},
            "added": {"value_pattern": "y"}}}
        # only its pattern moved: widened (a set-based re-parse may complete it in place)
        self.assertEqual(sm.grammar_changes(served, moved),
                         {"kinds": ["added", "gone", "internet_message_id"],
                          "sections": ["tiers"], "widened": ["internet_message_id"]})
        # what a mention of the kind is raised to (firm_mention: unique_mentions' only)
        # moves no kind; it re-runs that step
        firm = {**served, "kinds": {**served["kinds"], "internet_message_id": {
            **kind, "firm_mention": {"types": ["Email"], "why": "w", "upstream": "u"}}}}
        self.assertEqual(sm.grammar_changes(served, firm), {
            "kinds": [], "sections": [], "widened": [], "steps": ["unique_mentions"]})
        renormalized = {**served, "kinds": {**served["kinds"], "internet_message_id": {
            **kind, "value_pattern": "[!-~]{1,998}", "normalizer": "trim"}}}
        self.assertEqual(sm.grammar_changes(served, renormalized)["widened"], [])

    def test_the_units_holding_a_changed_kind_are_rewritten_and_a_section_is_refused(self):
        from ontology.schema import load_schema
        schema = load_schema()
        self.assertIn("b:graph_mail.messages",
                      sm._identifier_units(schema, ["internet_message_id"]))
        self.assertEqual(sm._identifier_units(schema, []), [])
        code = sm.code_digests(None)
        present = sorted(sm.STEPS)
        same = sm.plan_schemas(schema, schema, code, code, present)
        self.assertEqual(same["units"]["units"]["affected"], [])
        moved = sm.plan_schemas(schema, schema, code, code, present, grammar={
            "kinds": ["internet_message_id"], "sections": ["identity_rules"]})
        self.assertIn("b:graph_mail.messages", moved["units"]["units"]["affected"])
        self.assertIn("identifier_grammar_changed:identity_rules", moved["units"]["refused"])
        self.assertNotEqual(moved["id"], same["id"])
        firm = sm.plan_schemas(schema, schema, code, code, present, grammar={
            "kinds": [], "sections": [], "widened": [], "steps": ["unique_mentions"]})
        self.assertEqual(firm["units"]["units"]["affected"], [])
        self.assertEqual((firm["steps"]["run"], firm["steps"]["why"]),
                         ({"unique_mentions": None}, {"unique_mentions": "identifier_grammar"}))
        # widened only: the Email versions are completed in place, set-based
        # (operating.pg.identifier_reparse), not re-projected; their type still re-runs the
        # steps reading it, and a served-schema delta of them is projected again
        widened = sm.plan_schemas(schema, schema, code, code, present, grammar={
            "kinds": ["internet_message_id"], "sections": [],
            "widened": ["internet_message_id"]})
        units = widened["units"]
        self.assertNotIn("b:graph_mail.messages", units["units"]["affected"])
        self.assertEqual([(c["kind"], c["binding"]) for c in units["identifier_reparse"]],
                         [("internet_message_id", "graph_mail.messages")])
        self.assertIn("Email", units["touched_types"])
        self.assertIn("Email:graph_mail.messages:", units["scope"])


class CollapsedTests(unittest.TestCase):
    """CC-C-04: a code change pinned as collapsed in place (``COLLAPSED``) runs neither the
    step nor its readers; the plan names it under ``collapsed`` (the migration folds the
    store instead); an unpinned change of the same step still runs it and its readers."""

    def test_a_collapsed_pair_names_the_step_and_runs_no_reader(self):
        schema = {"object_types": {}, "link_types": {}}
        specs = {"mentions": sm.Spec(("mention:",), ("m.py",)),
                 "unique_mentions": sm.Spec(("crb-mid:",), ("u.py",), reads=("mentions",)),
                 "task_ids": sm.Spec(("crb-mcid:",), ("t.py",), reads=("mentions",))}
        old = {"mentions": "m", "unique_mentions": "u", "task_ids": "t"}
        new = {**old, "unique_mentions": "u2", "task_ids": "t2"}
        pins = {"unique_mentions": frozenset({("u", "u2")}), "task_ids": frozenset({("t", "t2")})}
        plan = sm.plan_steps(schema, schema, old, new, set(old), specs, inputs={},
                             collapsed=pins)
        self.assertEqual((plan["run"], plan["collapsed"]), ({}, ["task_ids", "unique_mentions"]))
        loose = sm.plan_steps(schema, schema, old, {**new, "task_ids": "t3"}, set(old), specs,
                              inputs={}, collapsed=pins)
        self.assertEqual((loose["run"], loose["collapsed"]), ({"task_ids": None},
                                                              ["unique_mentions"]))
        self.assertNotIn("collapsed", sm.plan_steps(schema, schema, old, old, set(old), specs,
                                                    inputs={}, collapsed=pins))
