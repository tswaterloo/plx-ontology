"""Classes split into mixins resolve every method as they did when it was one class.

The module split moved members of large classes into mixin bases kept in sibling modules
(``<module>_<part>.py``). Python then looks members up along the MRO, so a move is safe only
when it keeps three rules. Together they mean every attribute and every zero-arg ``super()``
resolves to the same definition as before the split:

* a zero-arg ``super().x`` in a method of the class or of one of its split mixins never
  resolves to the class itself or to one of its split mixins (it used to skip the whole class;
  ``CaptureCaseHostTools._captured_call``'s ``super().call()`` once reached the moved ``call``
  and recursed forever);
* no two parts (the class and its split mixins) define the same name, so none shadows another;
* no mixin method uses a name-mangled ``__attr``: it would mangle with the mixin's name.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("integrations", "operating", "operator_api", "verify")


def split_modules() -> list[str]:
    """Modules ``pkg/x.py`` with at least one sibling ``pkg/x_*.py``."""
    found = []
    for package in PACKAGES:
        files = {p.stem for p in (ROOT / package).glob("*.py")}
        found += [f"{package}.{stem}" for stem in sorted(files)
                  if stem != "__init__" and any(o.startswith(stem + "_") for o in files)]
    return found


def function_of(value: Any) -> Any:
    if isinstance(value, (staticmethod, classmethod)):
        value = value.__func__
    if isinstance(value, property):
        value = value.fget
    return value if inspect.isfunction(value) else None


def super_names(function: Any) -> list[str]:
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError, SyntaxError):
        return []
    return [node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "super"
            and not node.value.args]


def split_classes() -> list[tuple[type, list[type]]]:
    """(class, its parts: the class and its bases defined in its module's siblings)."""
    out = []
    for name in split_modules():
        module = importlib.import_module(name)
        for value in vars(module).values():
            if not inspect.isclass(value) or value.__module__ != name:
                continue
            parts = [k for k in value.__mro__
                     if k is value or k.__module__.startswith(name + "_")]
            if len(parts) > 1:
                out.append((value, parts))
    return out


class SplitMroTests(unittest.TestCase):
    def test_split_classes_resolve_as_one_class(self) -> None:
        classes = split_classes()
        self.assertGreater(len(classes), 5, "no split classes found: discovery is broken")
        problems = []
        for cls, parts in classes:
            mro = cls.__mro__
            owners: dict[str, type] = {}
            for part in parts:
                for attr, value in part.__dict__.items():
                    if attr.startswith("__") and attr.endswith("__") and attr not in (
                            "__init__", "__call__", "__enter__", "__exit__"):
                        continue
                    if attr in owners:
                        problems.append(f"{cls.__qualname__}.{attr}: defined on both "
                                        f"{owners[attr].__qualname__} and {part.__qualname__}")
                    owners[attr] = part
                    function = function_of(value)
                    if function is None:
                        continue
                    for target in super_names(function):
                        hit = next((k for k in mro[mro.index(part) + 1:]
                                    if target in k.__dict__), None)
                        if hit in parts:
                            problems.append(f"{part.__qualname__}.{attr}: super().{target} "
                                            f"resolves to the split part {hit.__qualname__}")
                    if part is not cls:
                        prefix = f"_{part.__name__.lstrip('_')}__"
                        problems += [f"{part.__qualname__}.{attr}: mangled name {n}"
                                     for n in function.__code__.co_names
                                     if n.startswith(prefix)]
        self.assertEqual(problems, [])

    def test_the_rule_catches_a_super_call_reaching_a_moved_method(self) -> None:
        class Base:
            def call(self) -> str:
                return "base"

        class Moved:
            def call(self) -> str:
                return "moved"

        class Split(Moved, Base):
            def wrapped(self) -> str:
                return super().call()

        target = super_names(Split.wrapped)
        hit = next(k for k in Split.__mro__[1:] if target[0] in k.__dict__)
        self.assertEqual(target, ["call"])
        self.assertIs(hit, Moved)


if __name__ == "__main__":
    unittest.main()
