"""A measure read's stated exclusions (Ask plan S2; split from ``ontology_tools_sql``): for
each parameter the read leaves switched off, the rows it leaves out are read by the
measure's statement over exactly those rows (``measures.excluded_statements``: same door,
period and filter, ungrouped) and said beside the figure: how many records (n), the
measure's value over them and their own result id."""
from __future__ import annotations

from typing import Any

from operating import read_ceiling
from operating.ontology_tools_sql_check import check


def excluded(tool: Any, args: dict[str, Any], actor: Any, conn: Any, scope: Any,
             guarded: str, moment: Any) -> list[dict[str, Any]]:
    """S2: per parameter the read left switched off: {parameter, says, n, value, result}."""
    from operating import measures
    out: list[dict[str, Any]] = []
    if not isinstance(args.get("measure"), str):
        return out
    for p, made in measures.excluded_statements(
            args["measure"], args.get("days"), args.get("period"), args.get("filter"),
            args.get("parameters")):
        sql = check(made, schemas=tool._schema_names(conn),
                    allowed_schema="" if moment is not None else guarded)
        if moment is not None:
            sql = read_ceiling.bounded_sql(sql, moment, guarded)
        got = tool._query(conn, scope, sql, actor)
        cols = got.get("columns") or []
        rows = [r["cells"] for r in got.get("rows") or () if isinstance(r, dict)]
        out.append({**measures.excluded_of(p, cols, rows), "result": got.get("result")})
    return out
