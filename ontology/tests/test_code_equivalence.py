"""Code proven equivalent to the commit that built generation 8 (``ontology/code_equivalence.py``).

What must hold, recomputed from that commit's own tree (``git archive``): the derivation
code's value is that commit's while the files it covers differ from that commit's only
as listed, each listed file only in the definitions this test names (none of which
derives a record); the mail and SharePoint readers differ only in the listed definitions;
any other difference gives the plain digest and no reader equivalence.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ontology import build_adopt, build_keys, code_equivalence as ce, project

ROOT = build_keys._ROOT

# path -> the top-level definitions that differ from BASE_COMMIT's, and why none of them
# decides a documentFields or containerMembers record.
DERIVATION_DEFINITIONS = {
    # the digest itself (narrowed, build-speed5; this table), the generation's finish;
    # the projector's object-by-object writes and object report parts:
    # rows of records, no derived record
    "ontology/project.py": {"_DERIVATION_DATA", "_IMPORT", "_NOT_DERIVATION", "__all__",
                            "_derivation_digest", "derivation_code", "derivation_files",
                            "document_code",
                            "derivation_modules", "environment_digest",
                            "finish_generation", "projected_counts",
                            "Projector", "_Report", "object_unit"},
    # the mention step's per-document failures (a step, not a derivation)
    "ontology/mentions.py": {"_memo_code", "_store_links", "read_ahead"},
    "ontology/document_failures.py": None,   # new: imported by the mention step only
    # scheduling, spills, merge, kept member views; _derive and _parse as they were
    "ontology/project_parallel.py": {"DERIVED", "Result", "SPILL_BATCH", "_KEPT", "_Spill",
                                     "_Spilled", "_kept_views", "_merge_rows", "_run",
                                     "_spilled", "_with_spills", "merge_part", "pieces",
                                     "project", "tasks"},
    # the builder's resume checkpoint
    "ontology/store.py": {"CHECKPOINT_META", "StoreBuilder", "VOLATILE_META"},
    # out of memory re-raised (READER_CHANGES)
    "operating/use_case_parts.py": {"_RESOURCE_WORDS", "_carried", "_is_package", "_mail",
                                    "_member", "_mime_node", "_parser_failure", "_resource",
                                    "_subject_name", "_zip"},
}


def _base_tree(test: unittest.TestCase) -> Path:
    scratch = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, scratch, True)
    try:
        return build_adopt.materialize(ce.BASE_COMMIT, scratch / "tree")
    except build_adopt.NotAdopted as why:   # a clone without that commit
        test.skipTest(f"{ce.BASE_COMMIT} not readable: {why}")


def _definitions(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    build_keys._strip_docstrings(tree)
    module = build_keys._Module.__new__(build_keys._Module)
    module.defs, module.imports, module.root, module.name = {}, {}, ROOT, ""
    module.package = ""
    module._top(tree.body)
    return {name: "\n".join(build_keys._dump(n) for n in nodes)
            for name, nodes in module.defs.items()}


class DerivationEquivalenceTests(unittest.TestCase):
    def test_the_derivation_code_is_the_base_commits_while_only_listed_files_differ(self):
        tree = _base_tree(self)
        files = project.derivation_files(ROOT)
        diff = ce.derivation_differences(files)
        active = all(ce.DERIVATION_CHANGES.get(p) == pair for p, pair in diff.items())
        if not active:   # a listed file moved on: the plain digest, no equivalence
            self.assertEqual(project.derivation_code(), project.environment_digest(files))
            self.skipTest("a derivation file differs from the listed versions")
        # That commit's own derivation_code, run from its tree with this Python.
        env = {k: v for k, v in os.environ.items()
               if not any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        done = subprocess.run(
            [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
             "from ontology.project import derivation_code; print(derivation_code())",
             str(tree)], cwd=str(tree), capture_output=True, text=True, timeout=300,
            env=env, check=False)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(project.derivation_code(), done.stdout.strip())
        self.assertNotEqual(project.derivation_code(), project.environment_digest(files))
        # Each differing file differs in the named definitions only.
        self.assertEqual(sorted(diff), sorted(DERIVATION_DEFINITIONS))
        for path, names in DERIVATION_DEFINITIONS.items():
            then, now = tree / path, ROOT / path
            if names is None:
                self.assertFalse(then.exists(), path)
                continue
            old, new = _definitions(then), _definitions(now)
            differing = {n for n in set(old) | set(new) if old.get(n) != new.get(n)}
            self.assertEqual(differing, names, path)
        for name in ("_derive", "_parse", "_parse_tasks", "_init_parse", "DocumentPool"):
            self.assertEqual(_definitions(tree / "ontology/project_parallel.py")[name],
                             _definitions(ROOT / "ontology/project_parallel.py")[name], name)

    def test_any_other_difference_gives_the_plain_digest(self):
        files = project.derivation_files(ROOT)
        listed = next(iter(ce.DERIVATION_CHANGES))
        for name, edited in (
                ("a listed file in another version",
                 [(p, "0" * 64 if p == listed else s) for p, s in files]),
                ("an unlisted file changed",
                 [(p, "1" * 64 if p == "ontology/document_fields.py" else s)
                  for p, s in files]),
                ("a module added", files + [("ontology/new_reader.py", "2" * 64)])):
            with self.subTest(name):
                self.assertIsNone(ce.equivalent_derivation_code(edited))


# The step memo codes this test computes in a child, from a tree, with that tree's schema.
_MEMO_SCRIPT = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from ontology.schema import load_schema\n"
    "from ontology import mentions, project, text_index\n"
    "declared = mentions.mention_link_types(load_schema().link_types)\n"
    "json.dump({'mention_memo': mentions._memo_code(declared),\n"
    "           'text_index_memo': text_index._memo_code(),\n"
    "           'document_code': project.document_code()}, sys.stdout)\n")


def _memo_codes(tree: Path) -> dict[str, str]:
    import json
    env = {k: v for k, v in os.environ.items()
           if not any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-I", "-c", _MEMO_SCRIPT, str(tree)],
                          cwd=str(tree), capture_output=True, text=True, timeout=600,
                          env=env, check=False)
    if done.returncode != 0:
        raise AssertionError(done.stderr[-2000:])
    return json.loads(done.stdout)


class StepMemoEquivalenceTests(unittest.TestCase):
    def test_the_step_memos_keep_the_base_commits_keys_while_only_listed_code_differs(self):
        """The mention memo, the text index memo and the document group keys give the
        base commit's values (what its build kept them under) for this code, and the
        files they digest differ from that commit's only in the named definitions."""
        tree = _base_tree(self)
        then, now = _memo_codes(tree), _memo_codes(ROOT)
        with mock.patch.object(ce, "earlier_value", lambda kind, value: value):
            plain = _memo_codes_here()
        for kind in ce.VALUE_EQUIVALENTS:
            with self.subTest(kind):
                if plain[kind] in ce.VALUE_EQUIVALENTS[kind]:
                    self.assertEqual(now[kind], then[kind])
                    self.assertNotEqual(plain[kind], then[kind])
                else:   # code moved on (or another Python/libraries): its own value
                    self.assertEqual(now[kind], plain[kind])
        old = _definitions(tree / "ontology/text_index.py")
        new = _definitions(ROOT / "ontology/text_index.py")
        self.assertEqual({n for n in set(old) | set(new) if old.get(n) != new.get(n)},
                         {"_base_index", "_memo_code", "store_text_index"})
        for path in ("ontology/mention_memo.py", "ontology/reviews.py",
                     "ontology/identifiers.py", "ontology/project_graph.py",
                     "operating/doc_metadata_taxonomy.py", "integrations/captured_pages.py",
                     "integrations/captured_text.py"):
            self.assertEqual((tree / path).read_bytes(), (ROOT / path).read_bytes(), path)
        store_then = _definitions(tree / "ontology/store.py")
        store_now = _definitions(ROOT / "ontology/store.py")
        self.assertEqual(store_then["held_stretches"], store_now["held_stretches"])

    def test_any_other_input_gives_the_codes_own_value(self):
        from ontology import mentions, text_index
        declared = {("Email", "Lot"): "Email.mentions_other"}
        self.assertEqual(mentions._memo_code(declared), _plain(mentions._memo_code, declared))
        self.assertNotIn(mentions._memo_code(declared),
                         set(ce.VALUE_EQUIVALENTS["mention_memo"].values()))
        with mock.patch.object(project, "derivation_code", lambda: "other derivation"):
            code = project.document_code()
        self.assertNotIn(code, set(ce.VALUE_EQUIVALENTS["document_code"].values()))
        with mock.patch.object(text_index, "__file__", str(ROOT / "ontology" / "store.py")):
            code = text_index._memo_code()
        self.assertNotIn(code, set(ce.VALUE_EQUIVALENTS["text_index_memo"].values()))


def _plain(function, *args):
    with mock.patch.object(ce, "earlier_value", lambda kind, value: value):
        return function(*args)


def _memo_codes_here() -> dict[str, str]:
    from ontology import mentions, text_index
    from ontology.schema import load_schema
    declared = mentions.mention_link_types(load_schema().link_types)
    return {"mention_memo": mentions._memo_code(declared),
            "text_index_memo": text_index._memo_code(),
            "document_code": project.document_code()}


class ReaderEquivalenceTests(unittest.TestCase):
    def test_mail_and_sharepoint_readers_differ_only_as_listed(self):
        tree = _base_tree(self)
        seen = set()
        for cls in (project.MailCaptures, project.SharePointCaptures):
            source = object.__new__(cls)
            diff = ce.reader_differences(build_keys.reader_parts(source, tree),
                                         build_keys.reader_parts(source))
            self.assertTrue(diff, cls.__name__)   # they do differ: the table is needed
            self.assertNotEqual(build_keys.reader_digest(source, tree),
                                build_keys.reader_digest(source))
            for key, pair in diff.items():
                self.assertEqual(ce.READER_CHANGES.get(key), pair, key)
            seen |= set(diff)
            self.assertTrue(ce.reader_equivalent(source, tree), cls.__name__)
        self.assertEqual(seen, set(ce.READER_CHANGES))   # no stale entry

        # Any other change to what a reader reaches: no equivalence.
        edited = tree / "operating" / "use_case_parts.py"
        edited.write_text(edited.read_text(encoding="utf-8") + "\nMAX_NAME_CHARS = 1\n",
                          encoding="utf-8")
        build_keys._MODULES.clear()
        for cls in (project.MailCaptures, project.SharePointCaptures):
            self.assertFalse(ce.reader_equivalent(object.__new__(cls), tree), cls.__name__)


if __name__ == "__main__":
    unittest.main()
