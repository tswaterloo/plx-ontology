"""Keys of a generation build (``ontology/build_keys.py``).

What must hold: the code that writes a unit's rows cannot change without a new
``PROJECTION_ROWS_VERSION`` (else an incremental build reuses rows the new code would
write otherwise); a code digest follows what a definition reaches and nothing else, so
an edit elsewhere (a docstring, an unrelated function) keeps it.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock

from ontology import build_keys

class RowsCodeVersionTests(unittest.TestCase):
    def test_the_rows_code_is_the_one_its_version_names(self):
        digest = build_keys.rows_code_digest()
        self.assertIsNotNone(digest)
        self.assertIn(
            digest, build_keys.ROWS_CODE_DIGESTS.get(build_keys.PROJECTION_ROWS_VERSION, ()),
            "the code that writes a unit's rows changed (ontology.build_keys "
            "ROWS_CODE_ROOTS): bump PROJECTION_ROWS_VERSION when it writes other rows, "
            "else add its digest to the version's in ROWS_CODE_DIGESTS")
        versions = list(build_keys.ROWS_CODE_DIGESTS.values())
        self.assertEqual(sum(map(len, versions)), len(frozenset().union(*versions)))

    def test_a_reader_of_captured_files_or_a_step_is_not_rows_code(self):
        # What the derived relations hold is in their fingerprints, and the steps run
        # in every build: an edit to them must not make the next build cold.
        reached = []
        real = build_keys._Module.refs

        def refs(module, nodes):
            found = real(module, nodes)
            reached.extend(found)
            return found
        with mock.patch.object(build_keys._Module, "refs", refs):
            build_keys.rows_code_digest()
        names = {(m, n) for m, n in reached}
        for outside in (("ontology.project", "derivation_code"),
                        ("ontology.project", "MirrorCaptures"),
                        ("ontology.project", "finish_generation")):
            self.assertNotIn(outside, names)
        self.assertFalse({m for m, _ in names} & {"ontology.document_fields",
                                                  "integrations.captured_text",
                                                  "integrations.source_mirror",
                                                  "ontology.mentions"})


class ReaderCodeTests(unittest.TestCase):
    def test_a_readers_code_reaches_what_shapes_its_records(self):
        # The mirror's citations, a BC source's stand-in scopes: in no captured byte.
        from ontology.project import MirrorCaptures
        from ontology.project_bc import BcCaptures
        reached = []
        real = build_keys._Module.refs

        def refs(module, nodes):
            found = real(module, nodes)
            reached.extend(found)
            return found
        with mock.patch.object(build_keys._Module, "refs", refs):
            for cls in (MirrorCaptures, BcCaptures):
                self.assertIsNotNone(build_keys.reader_digest(cls.__new__(cls)))
        self.assertIn(("integrations.source_mirror", "make_citation"), reached)
        self.assertNotEqual(build_keys.reader_digest(MirrorCaptures.__new__(MirrorCaptures)),
                            build_keys.reader_digest(BcCaptures.__new__(BcCaptures)))


class ScriptReaderTests(unittest.TestCase):
    def test_a_reader_outside_the_packages_is_digested_by_its_file_alike_each_build(self):
        # A source class of a script (``scripts`` has no package file): a token per
        # build would make every unit of its relations dirty on every build.
        from scripts.eval_order_margin import FIXTURE_BOOKS, MemoryCaptures
        one, two = MemoryCaptures([], FIXTURE_BOOKS), MemoryCaptures([], FIXTURE_BOOKS)
        digest = build_keys.reader_digest(one)
        self.assertIsNotNone(digest)
        self.assertEqual(digest, build_keys.reader_digest(two))
        self.assertIsNone(build_keys.reader_digest(one, build_keys._ROOT))  # never adopted


class CodeDigestTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "__init__.py").write_text("")
        self.write("b", '''
            LIMIT = 3

            def helper(x):
                """Its docstring."""
                return x + LIMIT

            def unrelated():
                return 1
            ''')
        self.write("c", '''
            def lazy():
                return 2

            def other():
                return 3
            ''')
        self.write("a", '''
            from pkg.b import helper
            import pkg.c as c_mod

            def root(x):
                # a comment
                from pkg import c
                return helper(x) + c.lazy() + c_mod.other()
            ''')
        patch = mock.patch.object(build_keys, "_ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name, text):
        path = self.root / "pkg" / f"{name}.py"
        path.write_text(textwrap.dedent(text))
        build_keys._MODULES.clear()

    def digest(self):
        build_keys._MODULES.clear()
        return build_keys.code_digest([("pkg.a", "root")])

    def test_a_digest_follows_what_a_definition_reaches_and_nothing_else(self):
        first = self.digest()
        self.assertIsNotNone(first)
        # docstrings, comments and unreached definitions leave it
        self.write("b", '''
            LIMIT = 3

            def helper(x):
                """Another docstring."""
                return x + LIMIT  # a note

            def unrelated():
                return 99
            ''')
        self.assertEqual(self.digest(), first)
        # a reached constant, a lazily imported function, a module.attr function do not
        for name, text in (
                ("b", "LIMIT = 4\ndef helper(x):\n    return x + LIMIT\n"
                      "def unrelated():\n    return 1\n"),
                ("c", "def lazy():\n    return 5\ndef other():\n    return 3\n"),
                ("c", "def lazy():\n    return 2\ndef other():\n    return 6\n")):
            with self.subTest(name=name, text=text):
                saved = (self.root / "pkg" / f"{name}.py").read_text()
                self.write(name, text)
                self.assertNotEqual(self.digest(), first)
                self.write(name, saved)
                self.assertEqual(self.digest(), first)

    def test_an_unreadable_module_gives_no_digest(self):
        self.write("b", "def helper(x:\n")
        self.assertIsNone(self.digest())
        (self.root / "pkg" / "b.py").unlink()
        self.assertIsNone(self.digest())


if __name__ == "__main__":
    unittest.main()
