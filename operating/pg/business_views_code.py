"""A view's print: what ``refresh`` compares to rebuild a view whose rows' definition changed
(a landed fix to its logic) even when its sources did not move.

Recorded in ``state.notes['code']`` by every build of the view. Since speed specs P4 the
print (``fingerprint``, prefix ``v1:``) is the view's DECLARED ``version`` (``View.version``,
bumped by hand when its output can change; ``tests/test_view_versions.py`` refuses an
output change without a bump) plus its name, columns and kinds, hidden columns, reads, the
common door columns and the door's contract version. Never source text: a comment, a
docstring, a refactor or an ops guard moves nothing (on 11-23 a disk-guard edit moved every
comms print and the box tried to rebuild 3.3M mails).

A stored print of the old style (source text, ``_fingerprint_v0``, kept for one release) is
*adopted* (re-stamped without a rebuild) only when it equals the old-style print of the code
now deployed, i.e. it is proven to have been built by this code; else the view counts as
changed: a business view is rebuilt in place, a comms view never is on the box
(``needs_train``: its rows keep serving until a Spark train swaps in a table built by this
code). A view with no stored print is adopted as before. ``PLX_BIZ_CODE_REFRESH=0`` turns
the comparison off.

``python3 -m operating.pg.business_views_code prints`` prints every view's print.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import json
import os
import threading
from typing import Any, Iterable, Mapping

MODULES = ("business_views", "business_views_supply", "business_views_ar",
           "business_views_shipments", "business_views_chain", "business_views_chain_extra",
           "business_views_access", "business_comms", "business_staff")
ENGINE_ROOTS = ("Reader", "Row", "Cell", "Col", "View", "columns", "build_view",
                "_resolved_rows", "_ddl")

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
    "shipments": ("operating.sales_order_status",),
    "purchase_orders": ("operating.po_status",),
    "purchase_order_lines": ("operating.po_status",),
    "emails": _COMMS + ("operating.pg.mail_said", "operating.pg.mail_owners"),
    "suppliers": ("operating.pg.party_keys",),
    "customers": ("operating.pg.party_keys",),
    "supplier_balances": ("operating.pg.payee_class",),
    "supplier_invoices": ("operating.pg.payee_class",),
    "teams_messages": _COMMS,
    "meetings": _COMMS,
    "meeting_transcripts": _COMMS + ("operating.employee_permissions",),
    "sharepoint_files": ("ontology.sender_parties", "ontology.mentions"),
    "attachments": ("ontology.sender_parties", "ontology.mentions"),
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


_CACHE: dict[int, str] = {}   # the old-style prints, by view object


def _fingerprint_v0(v: Any) -> str:
    """The old-style print (source text of the view's code, module doc): kept for one
    release, only to prove a stored state was built by the code now deployed (``adopt``)."""
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


STYLE = "v1:"
# The door's contract version (DOOR v2: access_class / col_class / ai_ok / lineage, migration
# 0041): a change of how every row carries its door is every view's change.
DOOR_VERSION = 2


def fingerprint(v: Any) -> str:
    """The print of one ``business_views.View`` (speed specs P4): its declared ``version``
    (bumped by hand when its rows can change: ``tests/test_view_versions.py`` refuses an
    output change without a bump), its name, columns and kinds, hidden columns, reads, the
    common door columns and the door's contract version. Never source text: a comment, a
    docstring, a refactor or an ops guard moves nothing."""
    from operating.pg import business_views as bv
    shape = {"name": v.name, "version": int(getattr(v, "version", 1)),
             "cols": [(c.name, c.kind) for c in v.cols], "internal": [list(x) for x in v.internal],
             "reads": list(v.reads), "listed": v.listed, "covers": v.covers,
             "common": [list(x) for x in bv.COMMON], "door": DOOR_VERSION}
    return STYLE + hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8")
                                  ).hexdigest()[:29]


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
    """Record the current print of each built view that has none, or whose stored print is
    old-style and PROVEN made by the code now deployed: equal to the old-style print of
    this code (``_fingerprint_v0``). A stored old-style print that differs is left as it is
    (the view is then ``code_changed``: a business view is rebuilt in place, a comms view
    waits for its train, ``needs_train``). Never adopted blind."""
    if not enabled():
        return []
    done = []
    for name, notes in meta.execute(f"SELECT view, notes FROM {schema}.state").fetchall():
        v = views.get(name)
        have = recorded(notes)
        if v is None or (have is not None and have.startswith(STYLE)):
            continue
        if have is None or have == _fingerprint_v0(v):
            meta.execute(f"UPDATE {schema}.state SET notes = COALESCE(notes, '{{}}'::jsonb) "
                         "|| jsonb_build_object('code', %s::text) WHERE view = %s",
                         (fingerprint(v), name))
            done.append(name)
    return done


def needs_train(meta: Any, schema: str, views: Mapping[str, Any]) -> list[str]:
    """The comms views (``fill``) whose stored print is not the code's: never rebuilt on
    the box (a whole comms build is a Spark train); their old rows keep serving."""
    return [n for n in code_changed(meta, schema, views)
            if getattr(views.get(n), "fill", None) is not None]


def main(argv: list[str] | None = None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["prints"]:
        print("usage: python3 -m operating.pg.business_views_code prints", file=sys.stderr)
        return 2
    from operating.pg import business_views as bv
    print(json.dumps({n: fingerprint(v) for n, v in sorted(bv.VIEWS.items())}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
