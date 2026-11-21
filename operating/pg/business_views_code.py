"""A view's code fingerprint: what ``refresh`` compares to rebuild a view whose own code
changed (a landed fix to its logic) even when its sources did not move (11-21: the Sage
invoice-discount fix to ``sales_invoice_lines`` stayed invisible until the next day).

Recorded in ``state.notes['code']`` by every build of the view. The fingerprint is a hash
of the syntax trees (no comments, docstrings or line numbers: a tidy-up changes nothing)
of

* the function that builds the view (a communications table: ``fill`` and its ``Spec``),
* every top-level definition of the view modules (``MODULES``) it names, followed by name,
* the declared shape (column names and kinds, reads, memos, hidden columns, ``covers``),
* for a business view, ``engine``: the shared reading and access code every view runs
  through (``Reader``, ``build_view`` ...); a change there is every view's change.

Names are followed within the view modules only (a deliberate bound: ``business_views``
reaches ~1,100 definitions of the whole repository through ``Reader``, so a closure over
all of them made every view's digest move on any landing). Code outside the view modules
(``operating.po_status`` ...) is still picked up by the daily rebuild, as before.

A view with no recorded fingerprint (built before this existed) is *adopted*: its current
fingerprint is recorded without a rebuild, so the first run after a deploy rebuilds only
what its own rules already rebuild.  ``PLX_BIZ_CODE_REFRESH=0`` turns the comparison off.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import json
import os
import threading
from typing import Any, Iterable, Mapping

MODULES = ("business_views", "business_views_supply", "business_views_access",
           "business_comms", "business_staff")
ENGINE_ROOTS = ("Reader", "Row", "Cell", "Col", "View", "columns", "build_view",
                "_resolved_rows", "_ddl", "freightcom")

# Helper modules a view's rows depend on that live outside the view modules, by view: each
# module's whole source (syntax tree, docstrings and comments left out) is part of the
# view's fingerprint, so a logic change there rebuilds the dependent views at the next
# refresh. Explicit, not followed: what those modules themselves import is not hashed.
_COMMS = ("ontology.sender_parties", "ontology.mentions", "operating.inbox_triage",
          "operating.proactive_threads", "operating.mail_envelope", "operating.my_people")
_ORDERS = ("operating.po_status", "operating.order_value", "operating.sales_order_status")
EXTERNAL: dict[str, tuple[str, ...]] = {
    "sales_orders": _ORDERS,
    "sales_order_lines": _ORDERS,
    "purchase_orders": ("operating.po_status",),
    "purchase_order_lines": ("operating.po_status",),
    "emails": _COMMS,
    "teams_messages": _COMMS,
    "meetings": _COMMS,
    "meeting_transcripts": _COMMS + ("operating.employee_permissions",),
    "sharepoint_files": ("ontology.sender_parties", "ontology.mentions"),
}
_MODTEXT: dict[str, str] = {}

_ROOT = Path(__file__).resolve().parents[2]
_LOCK = threading.Lock()
_DEFS: dict[str, list[tuple[str, str]]] | None = None   # name -> [(module, tree text)]
_REFS: dict[tuple[str, str], set[str]] = {}   # (module, name) -> the names it uses
_ENGINE: frozenset[str] | None = None


def enabled() -> bool:
    return os.environ.get("PLX_BIZ_CODE_REFRESH", "1").strip() != "0"


def _text(node: ast.AST) -> str:
    """The tree text with every docstring left out."""
    node = ast.parse(ast.unparse(node))   # a private copy: the original stays intact
    for sub in ast.walk(node):
        body = getattr(sub, "body", None)
        if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
                and isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            sub.body = body[1:] or [ast.Pass()]
    return ast.dump(node)


def _aliases(tree: ast.AST) -> set[str]:
    """The names a tree binds a view module to (``from operating.pg import business_comms
    as _comms``)."""
    out: set[str] = set()
    for sub in ast.walk(tree):
        if isinstance(sub, ast.ImportFrom) and sub.module == "operating.pg":
            out.update(a.asname or a.name for a in sub.names if a.name in MODULES)
    return out


def _names(node: ast.AST, aliases: set[str]) -> set[str]:
    """Names a definition uses: bare names, and ``alias.name`` of a view module only (a
    method's or a library's attribute is never another definition: ``x.build`` is not
    ``build``)."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
              and sub.value.id in aliases):
            out.add(sub.attr)
    return out


def _defs() -> dict[str, list[tuple[str, str]]]:
    global _DEFS
    with _LOCK:
        if _DEFS is None:
            found: dict[str, list[tuple[str, str]]] = {}
            for m in MODULES:
                tree = ast.parse((_ROOT / "operating" / "pg" / f"{m}.py")
                                 .read_text(encoding="utf-8"))
                aliases = _aliases(tree)
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        keys = [node.name]
                    elif isinstance(node, ast.Assign):
                        keys = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                        keys = [node.target.id]
                    else:
                        continue
                    text = _text(node)
                    refs = _names(node, aliases)
                    for k in keys:
                        found.setdefault(k, []).append((m, text))
                        _REFS.setdefault((m, k), set()).update(refs)
            _DEFS = found
        return _DEFS


def _closure(roots: Iterable[str], stops: Iterable[str] = (),
             skip: Iterable[str] = ()) -> set[str]:
    """The names reached from ``roots``; ``stops`` are not entered, definitions of the
    ``skip`` modules are not followed (``build_view`` names the communications builder;
    the business views do not run it)."""
    defs = _defs()
    stops, skip = set(stops), set(skip)
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen or name in stops or name not in defs:
            continue
        seen.add(name)
        for m, _text in defs[name]:
            if m not in skip:
                pending.extend(_REFS.get((m, name), ()))
    return seen


def _digest(names: Iterable[str], skip: Iterable[str] = ()) -> str:
    defs = _defs()
    h = hashlib.sha256()
    for name in sorted(names):
        for m, text in sorted(defs[name]):
            if m in skip:
                continue
            h.update(f"{m}:{name}\n{text}\n".encode("utf-8"))
    return h.hexdigest()


def engine() -> str:
    global _ENGINE
    names = _closure(ENGINE_ROOTS, skip=("business_comms",))
    _ENGINE = frozenset(names)
    return _digest(names, ("business_comms",))


def module_digest(name: str) -> str:
    """The digest of one helper module's source; an unreadable module is its own value
    (never equal to a readable one), so a missing file rebuilds rather than hides."""
    if name not in _MODTEXT:
        try:
            text = _text(ast.parse((_ROOT / (name.replace(".", "/") + ".py"))
                                  .read_text(encoding="utf-8")))
        except (OSError, SyntaxError, ValueError) as exc:
            text = f"unreadable:{type(exc).__name__}"
        _MODTEXT[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return _MODTEXT[name]


def _spec(fn: Any) -> str:
    """The ``Spec`` a communications table's ``fill`` was registered for (its closure)."""
    for cell in getattr(fn, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if hasattr(value, "__dataclass_fields__"):
            return repr(value)
    return ""


_CACHE: dict[int, str] = {}


def fingerprint(v: Any) -> str:
    """The code fingerprint of one ``business_views.View`` (module doc)."""
    key = id(v)
    if key in _CACHE:
        return _CACHE[key]
    h = hashlib.sha256()
    shape = {"name": v.name, "cols": [(c.name, c.kind) for c in v.cols],
             "reads": list(v.reads), "memos": list(v.memos),
             "internal": [list(x) for x in v.internal], "covers": v.covers,
             "listed": v.listed}
    h.update(json.dumps(shape, sort_keys=True).encode("utf-8"))
    if v.fill is not None:
        h.update(_spec(v.fill).encode("utf-8"))
        roots = [getattr(v.fill, "__name__", "")]
        if roots == ["<lambda>"]:
            roots = ["fill"]   # business_comms.fill(spec, write, table)
        h.update(_digest(_closure(roots)).encode("utf-8"))
    else:
        eng = engine()
        h.update(eng.encode("utf-8"))
        own = _closure([v.build.__name__], _ENGINE or (), ("business_comms",))
        h.update(_digest(own, ("business_comms",)).encode("utf-8"))
    for dep in EXTERNAL.get(v.name, ()):
        h.update(f"{dep}:{module_digest(dep)}\n".encode("utf-8"))
    _CACHE[key] = h.hexdigest()[:32]
    return _CACHE[key]


def recorded(notes: Any) -> str | None:
    notes = notes if isinstance(notes, dict) else json.loads(notes or "{}")
    return notes.get("code")


def code_changed(meta: Any, schema: str, views: Mapping[str, Any]) -> list[str]:
    """Built views whose recorded fingerprint differs from the code's now (none recorded:
    not changed, see ``adopt``)."""
    if not enabled():
        return []
    out = []
    for name, notes in meta.execute(f"SELECT view, notes FROM {schema}.state").fetchall():
        v = views.get(name)
        have = recorded(notes)
        if v is not None and have is not None and have != fingerprint(v):
            out.append(name)
    return out


def adopt(meta: Any, schema: str, views: Mapping[str, Any]) -> list[str]:
    """Record the current fingerprint of each built view that has none, without a rebuild."""
    if not enabled():
        return []
    done = []
    for name, notes in meta.execute(f"SELECT view, notes FROM {schema}.state").fetchall():
        v = views.get(name)
        if v is not None and recorded(notes) is None:
            meta.execute(f"UPDATE {schema}.state SET notes = COALESCE(notes, '{{}}'::jsonb) "
                         "|| jsonb_build_object('code', %s::text) WHERE view = %s",
                         (fingerprint(v), name))
            done.append(name)
    return done
