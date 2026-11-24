"""Views that read a table a swap replaces (any schema, and views on top of those).

A comms table's swap renames the live table, drops the old one and renames the new one in:
a view over it (the Assist list's ``assist.matter_items`` reads ``biz.emails`` and
``biz.teams_messages``; found 24 Nov on the Teams train copy) blocks the DROP. ``save`` reads
every such view (its definition, reloptions such as security_barrier, owner, grants and
comments) in dependency order, ``drop`` removes them top-down, ``restore`` makes them again
bottom-up from the saved text and puts owner, grants and comments back. The guarded views
``sql_views`` makes itself are dropped by it before ``save``; nothing here knows them."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Saved:
    schema: str
    name: str
    definition: str
    options: list[str]
    owner: str
    grants: list[tuple[str, str, bool]]          # (grantee or PUBLIC, privilege, grantable)
    comment: str | None
    column_comments: list[tuple[str, str]] = field(default_factory=list)
    depth: int = 1

    @property
    def ref(self) -> str:
        return f"{_q(self.schema)}.{_q(self.name)}"


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


_CLOSURE = """
WITH RECURSIVE dep(oid, depth) AS (
    SELECT r.ev_class, 1 FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
    WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = to_regclass(%s)
      AND r.ev_class <> d.refobjid
  UNION
    SELECT r.ev_class, dep.depth + 1 FROM dep JOIN pg_depend d ON d.refobjid = dep.oid
    JOIN pg_rewrite r ON r.oid = d.objid
    WHERE d.classid = 'pg_rewrite'::regclass AND r.ev_class <> dep.oid AND dep.depth < 20)
SELECT c.oid, n.nspname, c.relname, c.relkind, max(dep.depth)
FROM dep JOIN pg_class c ON c.oid = dep.oid JOIN pg_namespace n ON n.oid = c.relnamespace
GROUP BY c.oid, n.nspname, c.relname, c.relkind ORDER BY max(dep.depth), n.nspname, c.relname"""


def save(write: Any, table: str) -> list[Saved]:
    """Every view over ``table`` (``schema.name``), nearest first. A dependent that is not a
    plain view (a materialized view) refuses: it cannot be remade from its text here."""
    out: list[Saved] = []
    for oid, schema, name, kind, depth in write.execute(_CLOSURE, (table,)).fetchall():
        if kind != "v":
            raise RuntimeError(f"{schema}.{name} (relkind {kind}) reads {table}: the swap can "
                               "re-create plain views only; drop or move it first")
        definition, options, owner, comment = write.execute(
            "SELECT pg_get_viewdef(%s, true), c.reloptions, pg_get_userbyid(c.relowner), "
            "obj_description(c.oid, 'pg_class') FROM pg_class c WHERE c.oid = %s",
            (oid, oid)).fetchone()
        grants = [(g or "PUBLIC", p, bool(opt)) for g, p, opt in write.execute(
            "SELECT CASE WHEN a.grantee = 0 THEN NULL ELSE pg_get_userbyid(a.grantee) END, "
            "a.privilege_type, a.is_grantable FROM pg_class c, aclexplode(c.relacl) a "
            "WHERE c.oid = %s AND a.grantee <> c.relowner ORDER BY 1, 2", (oid,)).fetchall()]
        columns = [(c, t) for c, t in write.execute(
            "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM pg_attribute a "
            "WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped "
            "AND col_description(a.attrelid, a.attnum) IS NOT NULL ORDER BY a.attnum",
            (oid,)).fetchall()]
        out.append(Saved(schema, name, definition.rstrip().rstrip(";"), list(options or []),
                         owner, grants, comment, columns, depth))
    return out


def drop(write: Any, saved: list[Saved]) -> None:
    for v in reversed(saved):   # the views on top first
        write.execute(f"DROP VIEW {v.ref}")


def restore(write: Any, saved: list[Saved]) -> None:
    for v in saved:   # the views underneath first
        opts = f" WITH ({', '.join(v.options)})" if v.options else ""
        write.execute(f"CREATE VIEW {v.ref}{opts} AS {v.definition}")
        if v.owner != write.execute("SELECT current_user").fetchone()[0]:
            write.execute(f"ALTER VIEW {v.ref} OWNER TO {_q(v.owner)}")
        for who, priv, grantable in v.grants:
            to = "PUBLIC" if who == "PUBLIC" else _q(who)
            write.execute(f"GRANT {priv} ON {v.ref} TO {to}"
                          + (" WITH GRANT OPTION" if grantable else ""))
        if v.comment is not None:
            write.execute(f"COMMENT ON VIEW {v.ref} IS {_lit(v.comment)}")
        for col, text in v.column_comments:
            write.execute(f"COMMENT ON COLUMN {v.ref}.{_q(col)} IS {_lit(text)}")
