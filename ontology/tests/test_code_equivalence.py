"""The code-equivalence tables (``ontology/code_equivalence.py``): the zstd held forms keep
train 3's derivation, readings and document codes.

What must hold, recomputed from the commits' own trees (``git archive``): the tree that
made the change (e6640ecb53) digests as BASE_COMMIT's tree for the derivation code and the
readings code, and any other difference gives the plain digest."""

from __future__ import annotations

import ast
from collections import deque
from functools import lru_cache
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from integrations import captured_text
from ontology import code_equivalence as ce, project

ROOT = Path(project.__file__).resolve().parents[1]
CHANGED = "e6640ecb53"   # blob-zstd onto c387ba7453, with these tables


def _tree(test: unittest.TestCase, ref: str, into: Path) -> Path:
    into.mkdir()
    try:
        data = subprocess.run(["git", "-C", str(ROOT), "archive", ref], check=True,
                              capture_output=True, timeout=300).stdout
    except (OSError, subprocess.SubprocessError) as why:
        test.skipTest(f"{ref} not readable: {why}")
    import io
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        try:
            tar.extractall(into, filter="data")
        except TypeError:   # a Python without extraction filters (our own commit's tree)
            tar.extractall(into)
    return into


class TreesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dir = tempfile.TemporaryDirectory()
        cls.scratch = Path(cls._dir.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._dir.cleanup()

    def trees(self) -> tuple[Path, Path]:
        base, now = self.scratch / "base", self.scratch / "now"
        if not base.exists():
            _tree(self, ce.BASE_COMMIT, base)
        if not now.exists():
            _tree(self, CHANGED, now)
        return base, now

    def test_the_derivation_code_is_the_base_commits_with_the_listed_changes(self):
        base, now = self.trees()
        files, then = project.derivation_files(now), project.derivation_files(base)
        self.assertNotEqual(files, then)
        diff = {p: (dict(then).get(p), s) for p, s in files if dict(then).get(p) != s}
        self.assertEqual({p for p in diff if p != "ontology/code_equivalence.py"},
                         set(ce._ZSTD))
        for path, pair in ce._ZSTD.items():
            self.assertEqual(diff[path], pair)
        self.assertEqual(ce.earlier_files("derivation", files), then)
        self.assertEqual(project._derivation_digest(now), project.environment_digest(then))
        self.assertEqual(project._derivation_digest(now), project._derivation_digest(base))

    def test_the_readings_code_is_the_base_commits(self):
        base, now = self.trees()
        self.assertEqual(captured_text.readings_code(root=now),
                         captured_text.readings_code(root=base))
        # only captured_text of the reading modules moved
        moved = [m for m in captured_text.READING_MODULES if
                 (now.joinpath(*m.split(".")).with_suffix(".py")).read_bytes() !=
                 (base.joinpath(*m.split(".")).with_suffix(".py")).read_bytes()]
        self.assertEqual(moved, ["integrations.captured_text"])

    def test_document_codes_other_files_are_the_base_commits(self):
        # document_code digests derivation_code, readings_code and these six whole files
        base, now = self.trees()
        for module in ("ontology/project.py", "ontology/mentions.py", "ontology/identifiers.py",
                       "ontology/project_graph.py", "operating/doc_metadata_taxonomy.py",
                       "integrations/captured_pages.py"):
            self.assertEqual((now / module).read_bytes(), (base / module).read_bytes(), module)


class PlainDigestTest(unittest.TestCase):
    def test_the_acl_state_method_is_added_alone_and_no_derivation_module_calls_it(self):
        # capture-fix-1119e: the group's file is main's (6720961dc7, the served key's) plus CaptureStore.acl_state
        import difflib, hashlib
        (path, (then, now)), = ce._ACL_STATE.items()
        try:
            old = subprocess.run(["git", "-C", str(ROOT), "show", f"6720961dc7:{path}"],
                                 check=True, capture_output=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError) as why:
            self.skipTest(f"commit not readable: {why}")
        self.assertEqual(hashlib.sha256(old).hexdigest(), then)
        new = (ROOT / path).read_bytes()
        self.assertEqual(hashlib.sha256(new).hexdigest(), now)
        diff = [line for line in difflib.unified_diff(
            old.decode().splitlines(), new.decode().splitlines(), lineterm="", n=0)
            if line[:1] in "+-" and line[:3] not in ("+++", "---")]
        self.assertEqual([line for line in diff if line.startswith("-")], [])
        added = "\n".join(line[1:] for line in diff)
        self.assertIn("def acl_state(self, kind_name", added)
        self.assertEqual(added.count("def "), 1)
        from ontology.project import derivation_files
        for name, _ in derivation_files(ROOT):
            if name not in (path, "ontology/code_equivalence.py") and name.endswith(".py"):
                self.assertNotIn("acl_state", (ROOT / name).read_text(), name)

    def test_each_group_is_undone_alone_and_the_groups_are_disjoint(self):
        groups = ce.CHANGES["derivation"]
        names = [name for group in groups for name in group]
        self.assertEqual(len(names), len(set(names)))
        then = {p: old for g in groups for p, (old, _) in g.items()}
        base = [("integrations/a.py", "1" * 64), *((p, old) for p, old in sorted(then.items())
                                                    if old is not None)]
        zstd = [(p, ce._ZSTD[p][1] if p in ce._ZSTD else s) for p, s in base] + \
            [(p, now) for p, (old, now) in ce._ZSTD.items() if old is None]
        zstd.sort()
        both = [(p, ce._ACL_STATE[p][1] if p in ce._ACL_STATE else s) for p, s in zstd]
        clock = [(p, ce._ACL_STATE[p][1] if p in ce._ACL_STATE else s) for p, s in base]
        for files in (zstd, both, clock):
            self.assertEqual(sorted(ce.earlier_files("derivation", files)), sorted(base))

    def test_any_other_difference_gives_the_plain_digest(self):
        listed = ce._ZSTD
        files = [("integrations/a.py", "1" * 64), ("ontology/code_equivalence.py", "2" * 64),
                 *((p, now) for p, (_, now) in sorted(listed.items()))]
        undone = ce.earlier_files("derivation", files)
        self.assertNotIn("ontology/code_equivalence.py", dict(undone))
        self.assertNotIn("integrations/blob_zstd.py", dict(undone))
        self.assertEqual(dict(undone)["integrations/captured_text.py"],
                         listed["integrations/captured_text.py"][0])
        self.assertEqual(dict(undone)["integrations/a.py"], "1" * 64)
        self.assertIsNotNone(ce.equivalent_derivation_code(files))
        # one listed file at another version: nothing undone, the plain digest
        other = [(p, "3" * 64 if p == "integrations/captured_text.py" else s) for p, s in files]
        self.assertEqual(ce.earlier_files("derivation", other),
                         [f for f in other if f[0] != "ontology/code_equivalence.py"])
        self.assertIsNone(ce.equivalent_derivation_code(other))
        # one listed file missing: likewise
        short = [f for f in files if f[0] != "integrations/blob_pack.py"]
        self.assertIsNone(ce.equivalent_derivation_code(short))
        # an unknown kind is never undone
        self.assertEqual(ce.earlier_files("unknown", files),
                         [f for f in files if f[0] != "ontology/code_equivalence.py"])

    def test_the_readings_code_moves_with_another_version_of_captured_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for module in captured_text.READING_MODULES:
                path = root.joinpath(*module.split(".")).with_suffix(".py")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT.joinpath(*module.split(".")).with_suffix(".py"))
                                 .read_bytes())
            same = captured_text.readings_code(root=root)
            target = root / "integrations" / "captured_text.py"
            target.write_bytes(target.read_bytes() + b"\n# edited\n")
            self.assertNotEqual(captured_text.readings_code(root=root), same)

    def test_memo_codes_are_kept_as_given(self):
        for kind in ("mention_memo", "text_index_memo", "document_code", "unknown"):
            self.assertEqual(ce.earlier_value(kind, "abc"), "abc")

    def test_this_trees_derivation_code_is_its_plain_digest_or_the_undone_one(self):
        files = project.derivation_files(ROOT)
        undone = ce.earlier_files("derivation", files)
        expect = project.environment_digest(
            undone if undone != [f for f in files if f[0] != "ontology/code_equivalence.py"]
            else files)
        self.assertEqual(project.derivation_code(), expect)


# The image OCR reach guard: what the derivation, reading, member,
# projection, delta and mention code can run, as a call graph over the repository's own
# modules by name. A node per function, method, class body and module body (an
# ``if __name__ == "__main__"`` block never runs on import); an edge for every name a body
# uses that resolves through the module's imports, a module attribute or ``self.``/``cls.``,
# and for a string naming an OCR reader (``getattr``). A class reached reaches every
# method it has (its bases with it): a method called on an untyped object is a method of
# a class some reached code named, or of an object a caller outside the entry points
# made. Annotations never run and are skipped.
IMAGE_OCR_READERS = frozenset({"integrations.document_understanding.local_image_text",
                               "operating.use_case_parts.local_image_ocr"})
# capture_text reads an image by OCR only when its caller passes ``image_ocr`` (anything
# but False or None, or ``**kwargs``): such a call, or capture_text handed on as a value,
# reaches the OCR; its own reference to local_image_text is that guarded one.
GUARDED_OCR = {"integrations.captured_text.capture_text":
               "integrations.document_understanding.local_image_text"}


def repository_sources(root: Path) -> dict[str, bytes]:
    """module -> source of every non-test module of the repository's packages."""
    out = {}
    for package in sorted(p for p in root.iterdir() if (p / "__init__.py").is_file()):
        for path in sorted(package.rglob("*.py")):
            rel = path.relative_to(root).with_suffix("")
            if "tests" not in rel.parts:
                out[".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)] = \
                    path.read_bytes()
    return out


def entry_modules() -> set[str]:
    from integrations import kept_members
    root_modules = {p.relative_to(ROOT / "ontology").stem for p in (ROOT / "ontology").glob(
        "project*.py")}
    return (set(project.derivation_modules(ROOT)) | set(captured_text.READING_MODULES)
            | set(kept_members.MEMBER_MODULES) | {f"ontology.{m}" for m in root_modules}
            | {"ontology.delta", "integrations.kept_members", "ontology.mentions"})


@lru_cache(maxsize=None)
def _parsed(source: bytes, module: str) -> ast.Module:
    return ast.parse(source, module)


_WALKED: dict[int, list] = {}   # id of a node of a cached tree -> its nodes


def _nodes(node) -> list:
    if id(node) not in _WALKED:
        _WALKED[id(node)] = list(_walk(node))
    return _WALKED[id(node)]


def _walk(node):
    pending = [node]
    while pending:
        node = pending.pop()
        yield node
        for field, value in ast.iter_fields(node):
            if field not in ("annotation", "returns"):
                pending.extend(v for v in (value if isinstance(value, list) else [value])
                               if isinstance(v, ast.AST))


class TransportOutTest(unittest.TestCase):
    """The guarded transport, its grammar and write shapes are not derivation code
    (``code_equivalence._TRANSPORT_OUT``): a new read or write path moves no key."""

    GONE = ("integrations.live_source_guard", "integrations.live_source_grammar",
            "integrations.plx_hub_guard", "integrations.people_exclusion", "integrations.http")

    def test_the_derivation_reaches_no_transport_and_keeps_the_served_files(self):
        reached = project.derivation_modules(ROOT)
        self.assertEqual([m for m in self.GONE if m in reached], [])
        files = project.derivation_files(ROOT)
        undone = ce.earlier_files("derivation", files)
        held = dict(undone)
        for path, (then, now) in ce._TRANSPORT_OUT.items():
            self.assertEqual(held.get(path), then, path)
        # the restored list is in the derivation's own order: modules by path, then data
        data = [f for f in undone if f[0] in project._DERIVATION_DATA]
        modules = [f for f in undone if f[0] not in project._DERIVATION_DATA]
        self.assertEqual(undone[-len(data):], data)
        self.assertEqual(modules, sorted(modules, key=lambda f: Path(f[0]).parts))

    def test_the_group_holds_only_while_project_is_its_version_and_the_six_are_out(self):
        files = project.derivation_files(ROOT)
        moved = [(n, "0" * 64 if n == "ontology/project.py" else s) for n, s in files]
        self.assertEqual([n for n, _ in ce.earlier_files("derivation", moved)
                          if n in ce._TRANSPORT_OUT and ce._TRANSPORT_OUT[n][1] == ce.ABSENT],
                         [])
        # a transport file reached again (an import added back): nothing of the group undone
        back = files + [("integrations/http.py", "1" * 64)]
        self.assertEqual(dict(ce.earlier_files("derivation", back))["ontology/project.py"],
                         dict(files)["ontology/project.py"])

    def test_the_document_code_is_pinned_only_by_its_exact_value(self):
        (now, then), = ce.VALUES["document_code"].items()
        self.assertEqual(ce.earlier_value("document_code", now), then)
        self.assertEqual(ce.earlier_value("document_code", "f" * 64), "f" * 64)
        self.assertEqual(ce.earlier_value("mention_memo", now), now)


class CallGraph:
    def __init__(self, sources: dict[str, bytes]):
        self.sources = sources
        self.packages = {m.rpartition(".")[0] for m in sources}
        self.roots = {m.split(".")[0] for m in sources}
        self.bodies: dict[str, list] = {}
        self.owner: dict[str, tuple[str, str | None]] = {}
        self.members: dict[str, set[str]] = {}
        self.aliases: dict[str, dict[str, str]] = {}

    def _load(self, module: str) -> None:
        if module in self.aliases or module not in self.sources:
            return
        tree = _parsed(self.sources[module], module)
        defs = (ast.FunctionDef, ast.AsyncFunctionDef)
        self._add(f"{module}:<module>", [n for n in tree.body if not isinstance(
            n, (*defs, ast.ClassDef)) and not (isinstance(n, ast.If) and "__name__" in {
                x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)})], module, None)
        for node in tree.body:
            if isinstance(node, defs):
                self._add(f"{module}.{node.name}", [node], module, None)
            elif isinstance(node, ast.ClassDef):
                cls = f"{module}.{node.name}"
                self._add(cls, [*node.bases, *node.keywords, *node.decorator_list,
                                *(n for n in node.body if not isinstance(n, defs))], module, cls)
                self.members[cls] = set()
                for item in node.body:
                    if isinstance(item, defs):
                        self._add(f"{cls}.{item.name}", [item], module, cls)
                        self.members[cls].add(f"{cls}.{item.name}")
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    aliases[local] = alias.name if alias.asname else local
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = module.split(".")[:None if module in self.packages else -1]
                    parts = parts[:len(parts) - node.level + 1]
                    base = ".".join(parts + ([base] if base else []))
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
        self.aliases[module] = aliases

    def _add(self, name, body, module, cls):
        self.bodies[name] = body
        self.owner[name] = (module, cls)

    def _module_of(self, name: str) -> str | None:
        while name and name not in self.sources:
            name = name.rpartition(".")[0]
        return name or None

    def _symbol(self, qualified: str, depth: int = 0) -> set[str]:
        """The nodes a dotted name names: a module's body, a def, a class with its
        methods, or what a module re-exports under that name."""
        module = self._module_of(qualified)
        if module is None or depth > 8:
            return set()
        self._load(module)
        if qualified == module:
            return {f"{module}:<module>"}
        if qualified in self.bodies:
            return {qualified, *self.members.get(qualified, ())}
        head, _, name = qualified.rpartition(".")
        if head == module and name in self.aliases[module]:
            return self._symbol(self.aliases[module][name], depth + 1)
        return {f"{f}.{name}" for f in self._symbol(head, depth + 1)
                if f"{f}.{name}" in self.bodies}   # a method of a re-exported class

    def _resolve(self, node, module: str, cls: str | None) -> set[str]:
        chain: list[str] = []
        while isinstance(node, ast.Attribute):
            chain.insert(0, node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return set()
        if node.id in ("self", "cls") and cls and chain:
            return self._symbol(f"{cls}.{chain[0]}")
        if f"{module}.{node.id}" in self.bodies:
            start = f"{module}.{node.id}"
        elif node.id in self.aliases[module]:
            start = self.aliases[module][node.id]
            if start.split(".")[0] not in self.roots:
                return set()   # an installed library: none of ours runs there
        else:
            return set()   # a local or builtin: what it holds was named where it was made
        return self._symbol(".".join([start, *chain])) or self._symbol(start)

    def edges(self, name: str) -> set[str]:
        module, cls = self.owner[name]
        out = {f"{module}:<module>"}
        for body in self.bodies[name]:
            nodes = _nodes(body)
            plain = {id(n.func) for n in nodes if isinstance(n, ast.Call) and not any(
                k.arg is None or k.arg == "image_ocr" and not (
                    isinstance(k.value, ast.Constant) and k.value.value in (False, None))
                for k in n.keywords)}
            for node in nodes:
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and \
                        node.value.replace(".", "").isidentifier():   # getattr, importlib
                    out |= {r for r in IMAGE_OCR_READERS
                            if node.value.rpartition(".")[2] == r.rpartition(".")[2]}
                elif isinstance(node, (ast.Name, ast.Attribute)) and \
                        isinstance(getattr(node, "ctx", None), ast.Load):
                    found = self._resolve(node, module, cls)
                    out |= found
                    if id(node) not in plain:
                        out |= {GUARDED_OCR[g] for g in found & GUARDED_OCR.keys()}
        return out - {name, GUARDED_OCR.get(name)}

    def reach(self, modules: set[str]) -> dict[str, list[str]]:
        """OCR reader -> one path to it from an entry point of ``modules`` (every node of
        theirs but the readers and capture_text themselves)."""
        for module in modules:
            self._load(module)
        entries = [n for n, (m, _) in self.owner.items() if m in modules
                   and n not in IMAGE_OCR_READERS and n not in GUARDED_OCR]
        parent: dict[str, str | None] = dict.fromkeys(entries)
        queue, found = deque(entries), {}
        while queue:
            node = queue.popleft()
            if node in IMAGE_OCR_READERS:
                path = [node]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])
                found.setdefault(node, path[::-1])
                continue
            for nxt in self.edges(node):
                if nxt in self.bodies and nxt not in parent:
                    parent[nxt] = node
                    queue.append(nxt)
        return found


class ImageOcrPinTest(unittest.TestCase):
    """The image OCR group: document_understanding moved only in the
    image OCR reading, and nothing that derives or reads a kept record runs it."""

    IMAGE_OCR = {"local_image_text", "_image_for_ocr", "_plausible_dpi", "_png",
                 "_reading_score", "_image_retries", "IMAGE_OCR_LONGEST", "IMAGE_OCR_SMALL",
                 "IMAGE_OCR_MAX_ENLARGE", "_SURE_WORD", "_SURE_WORDS_MIN", "_ocr_frames",
                 "_flattened", "_LIGHT_GLYPHS", "_downscaled", "IMAGE_OCR_MAX_SECONDS"}

    def test_document_understanding_moved_only_in_the_image_reading(self):
        import ast
        import hashlib
        path = "integrations/document_understanding.py"
        (then, now), = ce._IMAGE_OCR.values()
        try:
            base = subprocess.run(["git", "-C", str(ROOT), "show", f"{ce.BASE_COMMIT}:{path}"],
                                  check=True, capture_output=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError) as why:
            self.skipTest(f"base not readable: {why}")
        self.assertEqual(hashlib.sha256(base).hexdigest(), then)
        current = (ROOT / path).read_bytes()
        if hashlib.sha256(current).hexdigest() != now:
            self.skipTest("document_understanding moved past the pin: the plain digest holds")

        def top(source: bytes) -> dict[str, str]:
            out = {}
            for node in ast.parse(source).body:
                names = ([node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                         else [t.id for t in getattr(node, "targets", [getattr(node, "target",
                                                                                None)])
                               if isinstance(t, ast.Name)])
                for name in names or [ast.dump(node)]:
                    out[name] = ast.dump(node)
            return out
        before, after = top(base), top(current)
        moved = {n for n in before.keys() | after.keys() if before.get(n) != after.get(n)}
        self.assertTrue(moved)
        self.assertEqual(moved - self.IMAGE_OCR, set())

    def test_nothing_that_derives_reads_or_projects_reaches_the_image_reading(self):
        # Every function, method and module body of the derivation, reading, member,
        # projection, delta and mention modules is an entry point; nothing any of them
        # can run, directly or through any repository module, may reach the image OCR.
        graph = CallGraph(repository_sources(ROOT))
        self.assertEqual(graph.reach(entry_modules()), {})

    def test_the_reach_guard_fails_on_an_injected_indirect_call(self):
        sources = repository_sources(ROOT)
        injected = {
            # the delta -> a new helper module -> a capture function that opts in
            "a capture helper two modules away": {
                "ontology.delta": "\ndef _injected(c):\n    from integrations import zz_helper"
                                  "\n    return zz_helper.pass_images(c)\n",
                "integrations.zz_helper": "from integrations.capture_mail import zz_later\n"
                                          "def pass_images(c):\n    return zz_later(c)\n",
                "integrations.capture_mail": "\ndef zz_later(c):\n    return capture_text("
                                             "c, {}, image_ocr=True)\n"},
            # kept_members -> a method of a new class -> the OCR by a module alias
            "a method through an import alias": {
                "integrations.kept_members": "\nclass _Injected:\n    def read(self, c):\n"
                                             "        import integrations.document_understanding"
                                             " as d\n        return d.local_image_text(c)\n"},
            # a reading module hands capture_text on: whoever calls it may opt in
            "capture_text handed on": {
                "integrations.captured_text": "\ndef _injected(c, read=None):\n"
                                              "    return (read or capture_text)(c, {})\n"},
            # the case intake's OCR reader, named by string
            "the case reader by getattr": {
                "ontology.mentions": "\ndef _injected():\n    import operating.use_case_parts"
                                     " as p\n    return getattr(p, 'local_image_ocr')()\n"},
        }
        for case, patch in injected.items():
            with self.subTest(case=case):
                changed = dict(sources)
                for module, tail in patch.items():
                    changed[module] = changed.get(module, b"") + tail.encode()
                reach = CallGraph(changed).reach(entry_modules())
                self.assertTrue(reach, case)
        # and the guard's one exception holds: capture_text without image OCR
        changed = dict(sources)
        changed["ontology.delta"] += (b"\ndef _plain(c):\n    from integrations.captured_text"
                                      b" import capture_text\n    return capture_text(c, {},"
                                      b" image_ocr=False)\n")
        self.assertEqual(CallGraph(changed).reach(entry_modules()), {})

    def test_this_trees_reading_codes_undo_the_image_reading(self):
        files = project.derivation_files(ROOT)
        path = "integrations/document_understanding.py"
        (then, now), = ce._IMAGE_OCR.values()
        if dict(files).get(path) != now:
            self.skipTest("document_understanding moved past the pin")
        self.assertEqual(dict(ce.earlier_files("derivation", files))[path], then)
        module = "integrations.document_understanding"
        self.assertEqual(dict(ce.earlier_files("readings_code", [(module, now)]))[module], then)


if __name__ == "__main__":
    unittest.main()
