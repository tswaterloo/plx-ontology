#!/usr/bin/env python3
"""The Portal's own error-code vocabulary, generated from its code at the repo pin.

``operating/portal_error_codes.json`` (format ``plx-portal-error-codes/v1``) lists every
code literal in the Portal's product source under ``portal/src`` at the
``plx-customer-portal`` commit of the upstream pin file ``repo_pins``. The owner
door (``operating.goal2_exec_owner.door_code``) and the Portal adapter's refusal
(``operating.action_adapters.owner_code``) keep a Portal error code only while it is in
this list: the employee keeps the Portal's own reason (``ORDER_NOT_EDITABLE``,
``VALIDATION``), and a word the Portal never wrote as a code (a host, a person, a PO
number, a customer) is not kept. Never edit the list by hand.

A code literal is a quoted string literal that

* is SCREAMING_SNAKE (``[A-Z][A-Z0-9]*(_[A-Z0-9]+)*``: ``VALIDATION``, ``ORDER_NOT_EDITABLE``;
  never free text, a path, a host or a lower-case identifier), and
* stands as a ``code``'s value: a ``code`` property or variable assigned, typed or
  compared (``code: "X"``, ``code?: "X" | "Y"``, ``code = "X"``, ``err.code === "X"``), or a
  member of a type alias whose name ends in ``ErrorCode``, ``FailureCode``, ``RefusalCode``
  or ``DenialCode`` (``type VoiceErrorCode = "A" | "B"``), or the literal of a self-named
  constant a ``code`` takes by name (``code: FG_FROZEN`` with ``const FG_FROZEN =
  "FG_FROZEN"``; ``X_CODE = "X"`` too) declared in the same file or in the product file it is
  imported from by name (``@/`` or relative path). A constant whose value is not its own
  name is a record key, not an error code (``where: { code: GIT_ZONE_CODE }`` with
  ``GIT_ZONE_CODE = "GIT"``), and a name re-exported through another module or declared
  nowhere is not read: neither is a code, and
* sits in a product source file (``.ts``/``.tsx``/``.js``/``.jsx``/``.mjs``/``.cjs`` under
  ``portal/src`` that ``ontology.upstream.is_product`` admits: no test, spec, fixture,
  mock or story), so a test's sample value (``ACME``) is not a code.

Lower-case Portal codes (``not_found``) are left out: the Workbench reads a refusal without
a listed code by its HTTP status (``action_adapters.STATUS_HINTS``).

Usage (local git reads only, no network):
    python3 -m scripts.portal_error_codes [--checkout PATH] [--pins PATH] [--out PATH]
    python3 -m scripts.portal_error_codes --check [--checkout PATH] [--pins PATH] [--out PATH]
``--check`` exits 0 when the committed list is what the checkout generates at the pin;
1 when it differs or was made at another commit (the pin moved: regenerate); 2 when the
checkout lacks the pinned commit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import posixpath
import re
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ontology.upstream import checkout_for, is_product  # noqa: E402
from scripts.source_registry import Tree, repo_pins  # noqa: E402

FORMAT = "plx-portal-error-codes/v1"
REPO = "plx-customer-portal"
SOURCE_ROOT = "portal/src/"
OUT_PATH = ROOT / "operating" / "portal_error_codes.json"
PINS_PATH = ROOT / "devloop" / "upstream-pins.json"

SOURCE = re.compile(r"\.(?:ts|tsx|js|jsx|mjs|cjs)$")
CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
_LITERAL = r"""(?:"[^"\\\n]*"|'[^'\\\n]*')"""
_UNION = rf"\|?\s*{_LITERAL}(?:\s*\|\s*{_LITERAL})*"
# A ``code`` property or variable (``err.code`` too, a quoted key too), then ``:`` (a
# value or a type), ``=``, ``==``/``===`` or ``!=``/``!==``, then one literal or a union.
_CODE_VALUE = re.compile(rf"""(?<![\w$])["']?code["']?\??\s*(?::|[!=]==?|=(?![=>]))\s*({_UNION})""")
_CODE_TYPE = re.compile(rf"\btype\s+\w*(?:Error|Failure|Refusal|Denial)Code\s*=\s*({_UNION})")
# A ``code`` that takes a constant by name (``code: FG_FROZEN``), the constant's declaration
# (``const FG_FROZEN = "FG_FROZEN"``, ``as const`` allowed) and a named import.
_CODE_NAME = re.compile(r"""(?<![\w$])["']?code["']?\??\s*(?::|[!=]==?|=(?![=>]))\s*"""
                        r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)(?![\w$.(\[])")
_CONST = re.compile(rf"\bconst\s+([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)\s*=\s*({_LITERAL})"
                    r"\s*(?:as\s+const\s*)?[;\n]")
_IMPORT = re.compile(r"""\bimport\s+(?:type\s+)?\{([^}]*)\}\s*from\s*["']([^"']+)["']""")
_MODULE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
                    "/index.ts", "/index.tsx", "/index.js")
RULE = ("SCREAMING_SNAKE string literals that stand as a code's value (a `code` property or "
        "variable assigned, typed or compared; a member of a type alias named *ErrorCode, "
        "*FailureCode, *RefusalCode or *DenialCode; the literal of a self-named constant "
        "(X = \"X\" or X_CODE = \"X\") a `code` takes by name, declared in the same file or "
        "the product file it is imported from) in "
        "product source under portal/src (ontology.upstream.is_product: no tests, fixtures "
        "or mocks)")


def _module(importer: str, spec: str, files: Mapping[str, str]) -> str | None:
    """The product file an import specifier names, or None (a package, or not read)."""
    if spec.startswith("@/"):
        base = SOURCE_ROOT + spec[2:]
    elif spec.startswith("."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    else:
        return None
    return next((base + suffix for suffix in ("", *_MODULE_SUFFIXES)
                 if base + suffix in files), None)


def _named_literal(name: str, path: str, files: Mapping[str, str]) -> str | None:
    """The string literal ``name`` is bound to in ``path``: its own ``const`` or the
    ``const`` of the product file it imports ``name`` from; None when neither holds it."""
    text = files[path]
    own = [literal for const, literal in _CONST.findall(text) if const == name]
    if own:
        return own[0]
    for names, spec in _IMPORT.findall(text):
        for item in names.split(","):
            parts = item.split()
            if parts and parts[0] == "type":
                parts = parts[1:]
            if not parts or parts[-1] != name:
                continue
            target = _module(path, spec, files)
            if target is None:
                return None
            source = parts[0]
            declared = [lit for const, lit in _CONST.findall(files[target]) if const == source]
            return declared[0] if declared else None
    return None


def codes(files: Mapping[str, str]) -> list[str]:
    """The code literals of ``files`` (path -> text), sorted, each once."""
    found: set[str] = set()
    for path, text in files.items():
        literals = [literal for pattern in (_CODE_VALUE, _CODE_TYPE)
                    for match in pattern.finditer(text)
                    for literal in re.findall(_LITERAL, match.group(1))]
        for name in set(_CODE_NAME.findall(text)):
            literal = _named_literal(name, path, files)
            if literal is not None and literal[1:-1] in (name, name.removesuffix("_CODE")):
                literals.append(literal)
        found.update(literal[1:-1] for literal in literals if CODE.fullmatch(literal[1:-1]))
    return sorted(found)


def build(tree: Tree) -> dict[str, Any]:
    paths = [path for path in tree.paths
             if path.startswith(SOURCE_ROOT) and SOURCE.search(path) and is_product(path)]
    return {"format": FORMAT,
            "_usage": "Generated by `python3 -m scripts.portal_error_codes`; never edit by "
                      "hand. `--check` fails when it is not what the pinned Portal commit "
                      "generates (after a repo pin moves, regenerate).",
            "repo": REPO, "commit": tree.commit, "rule": RULE,
            "codes": codes(tree.read(paths))}


def render(vocabulary: Mapping[str, Any]) -> str:
    return json.dumps(vocabulary, indent=1, ensure_ascii=False) + "\n"


def _pinned(pins_path: Path, checkout: str | None) -> tuple[str, Path]:
    pins_doc = json.loads(pins_path.read_text(encoding="utf-8"))
    commit = repo_pins(pins_doc)[REPO]["commit"]
    return commit, checkout_for(REPO, {REPO: checkout} if checkout else {}, pins_doc)


def check(out_path: Path = OUT_PATH, pins_path: Path = PINS_PATH,
          checkout: str | None = None) -> tuple[int, str]:
    """``(exit code, message)``: is ``out_path`` what the pinned commit generates?"""
    commit, path = _pinned(pins_path, checkout)
    committed = out_path.read_text(encoding="utf-8") if out_path.is_file() else None
    if committed is None:
        return 1, f"{out_path} missing: python3 -m scripts.portal_error_codes"
    recorded = json.loads(committed).get("commit")
    if recorded != commit:
        return 1, (f"{out_path} was generated at {str(recorded)[:12]}, the pin is "
                   f"{commit[:12]}: python3 -m scripts.portal_error_codes")
    if not Tree.holds(path, commit):
        return 2, f"{path} lacks {commit[:12]}: not checked"
    if render(build(Tree(REPO, path, commit))) != committed:
        return 1, (f"{out_path} differs from a generation at {commit[:12]} "
                   "(edited by hand?): python3 -m scripts.portal_error_codes")
    return 0, f"{out_path} is current at {commit[:12]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--checkout", help=f"the {REPO} checkout (default: the pin file's)")
    parser.add_argument("--pins", type=Path, default=PINS_PATH)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)
    if args.check:
        code, message = check(args.out, args.pins, args.checkout)
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    commit, path = _pinned(args.pins, args.checkout)
    if not Tree.holds(path, commit):
        print(f"{path} lacks {commit[:12]}", file=sys.stderr)
        return 2
    vocabulary = build(Tree(REPO, path, commit))
    args.out.write_text(render(vocabulary), encoding="utf-8")
    print(f"{args.out}: {len(vocabulary['codes'])} codes at {commit[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
