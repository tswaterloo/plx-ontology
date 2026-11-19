"""``plx.business``: one read over a curated business view (``biz.<view>``, built by
``operating.pg.business_views``): customers, what they owe, sales orders and invoices,
suppliers, purchase orders, products and stock, each real thing once, every money
column beside its currency.

A request names a view and plain columns: ``where``, ``group_by`` (a date column by
day/week/month/quarter/year), ``measures`` (count, count_distinct, sum, avg, min, max),
``select``, ``order_by``, ``limit``; ``{"describe": true}`` returns the data dictionary.
It is compiled to one bound, read-only statement. A money measure is always split by its
currency column (never added across currencies).

Permissions: a row is read only when the reader holds every audience key and every
marking set it and every other record a value of it was read from carry (``audience_keys``,
``rec_marks``; a ``blocked`` class: nobody), and none of those records is a document the
reader may not open. A row the reader may read whose USED column's cell carries a marking
the reader lacks, or whose USED column was read from a record the reader may not read
(``col_classes.needs``, ``lineage.col_objects``: a supplier's mailed promise date), is
left out and counted as withheld (the answer is partial), as ``plx.table`` does.

The answer has ``plx.table``'s shape (groups with members_cited and per-measure inputs
and inputs_citations; rows with cell citations; completeness, partial, withheld, gaps),
so the answer gate binds its counts and sums the same way."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from operating.ontology_tools_identity import ToolError
from operating.pg import business_views as bv

TOOL_NAME = "plx.business"
TOOL_NAMES = (TOOL_NAME,)
OPS = ("=", "!=", "<", "<=", ">", ">=", "in", "contains", "starts_with", "is_null",
       "not_null", "between")
FNS = ("count", "count_distinct", "sum", "avg", "min", "max")
BUCKETS = ("day", "week", "month", "quarter", "year")
NUMERIC = frozenset({"money", "numeric", "integer"})
MAX_WHERE, MAX_GROUP, MAX_MEASURES, MAX_SELECT, MAX_ORDER = 12, 3, 4, 16, 3
MAX_IN, MAX_TEXT = 200, 200
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
MAX_CITED = 50
STATEMENT_SECONDS = 20.0
# views held as per-item permissioned captures: asked of plx.table
ITEM_VIEWS = {"emails": "Email", "teams_messages": "TeamsMessage",
              "meetings": "CalendarEvent",
              "meeting_transcripts": "MeetingTranscript"}
PILOT = "pilot"
OF_RECORD = "of_record"
# rows a view holds outside the system of record are left out unless the request filters
# or groups on the column: (column, the value kept, gap reason, gap note)
DEFAULT_FILTERS = (
    (PILOT, False, "pilot_rows_left_out",
     "BC pilot rows (pilot = true) are left out: Sage is the system of record; filter or "
     "group on pilot to read them"),
    (OF_RECORD, True, "not_of_record_rows_left_out",
     "rows no Sage record carries (of_record = false: held only in FileMaker, the Portal or "
     "BC) are left out: Sage is the system of record; filter or group on of_record to read "
     "them"),
)

_CONTRACT = json.loads((Path(__file__).with_name("ontology_tool_contracts.json"))
                       .read_text(encoding="utf-8"))["tools"][TOOL_NAME]
DESCRIPTION: str = _CONTRACT["description"]
INPUT_SCHEMA: dict[str, Any] = _CONTRACT["input"]
TOOL: dict[str, Any] = {"name": TOOL_NAME, "description": DESCRIPTION,
                        "inputSchema": INPUT_SCHEMA, "annotations": {"readOnlyHint": True}}


def _bad(message: str, hint: str | None = None) -> ToolError:
    return ToolError("invalid_request", message, hint)


def _gap(kind: str, code: str, subject: str, detail: str) -> dict[str, Any]:
    from operating.ontology_tools_identity import _gap as gap
    return gap(kind, code, subject, detail)


class _Sql:
    def __init__(self) -> None:
        self.params: dict[str, Any] = {}

    def bind(self, value: Any) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f"%({name})s"


def _q(name: str) -> str:
    return '"' + name + '"'


class BusinessTool:
    def __init__(self, base: Any) -> None:
        self.base = base

    def call(self, name: str, arguments: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        if name != TOOL_NAME:
            return {"error": {"code": "unknown_tool", "message": f"{name} is not plx.business"}}
        try:
            return self._call(dict(arguments or {}), actor)
        except ToolError as exc:
            error = {"code": exc.code, "message": exc.message[:600]}
            if exc.hint:
                error["hint"] = exc.hint[:600]
            return {"error": error}

    # ------------------------------------------------------------------ plan

    def _call(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        unknown = sorted(set(args) - set(INPUT_SCHEMA["properties"]))
        if unknown:
            raise _bad(f"unknown request keys: {', '.join(unknown)}",
                       "use view, where, group_by, measures, select, order_by, limit, describe")
        if args.get("describe"):
            return {"views": bv.dictionary()}
        if args.get("overview") is not None:
            return self._overview(args, actor)
        name = args.get("view")
        if isinstance(name, str) and name in ITEM_VIEWS:
            raise ToolError("use_plx_table", f"{name} are read per item under the mail "
                            "rules", f"call plx.table with from: {ITEM_VIEWS[name]}")
        if not isinstance(name, str) or name not in bv.listed():
            raise ToolError("unknown_view", f"no business view {str(name)[:64]!r}",
                            "views: " + ", ".join(sorted(bv.listed())) +
                            " (describe: true lists their columns)")
        conn, scope, docs = self._reader(actor)
        view = bv.VIEWS[name]
        plan = _plan(view, args)
        sql, params = _compile(view, plan, scope, docs)
        got = self._run(conn, sql, params)
        return _answer(view, plan, got)

    def _reader(self, actor: Any) -> tuple[Any, Any, dict[str, list[str]]]:
        conn = getattr(getattr(self.base, "store", None), "conn", None)
        if getattr(conn, "_pg", None) is None or not getattr(conn, "postgres", False):
            raise ToolError("not_served", "plx.business reads the Postgres store only",
                            "use plx.table")
        scope = self.base._scope(actor, self.base.clock())
        from operating.search import document_marks
        docs = {k: list(json.loads(v)) for k, v in document_marks.params(scope.held).items()
                if k != "dm_people"}   # a full reader's tag, not a list
        return conn, scope, docs

    def _overview(self, args: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        """One customer's or chain's account in one call (``overview_requests``): each
        section one reader-scoped read of a view, answered as that request would be."""
        if set(args) != {"overview"}:
            raise _bad("overview stands alone: {\"overview\": {\"customer\": text}}")
        got = args["overview"]
        text = got.get("customer") if isinstance(got, Mapping) else None
        if not isinstance(got, Mapping) or set(got) != {"customer"} \
                or not isinstance(text, str) or not 2 <= len(text.strip()) <= MAX_TEXT:
            raise _bad("overview is {\"customer\": the customer's or chain's name}")
        conn, scope, docs = self._reader(actor)
        today = bv.today()
        sections, gaps, partial = [], [], False
        for section, request in overview_requests(text.strip(), today):
            view = bv.VIEWS[request["view"]]
            plan = _plan(view, request, internal=True)
            sql, params = _compile(view, plan, scope, docs)
            answer = _answer(view, plan, self._run(conn, sql, params))
            partial = partial or bool(answer.get("partial"))
            sections.append({"section": section, "request": request, "answer": answer})
        gaps.append(_gap("unknown", "mail_not_in_views", text.strip()[:120],
                         "mail, Teams and meetings with the party are not in the business "
                         "views: search them with plx.documents or plx.find"))
        return {"overview": {"customer": text.strip(), "as_of": today.isoformat(),
                             "matched": "every account whose name or chain contains the "
                                        "text, case-insensitive"},
                "sections": sections, "partial": partial, "gaps": gaps}

    def _run(self, conn: Any, sql: str, params: Mapping[str, Any]) -> dict[str, Any]:
        from operating import call_limits
        import psycopg
        left = call_limits.seconds_left()
        if left is not None and left <= 0:
            raise ToolError("timeout", "no time is left in this call for plx.business")
        seconds = STATEMENT_SECONDS if left is None else max(0.5, min(STATEMENT_SECONDS,
                                                                       left - 0.5))
        pg = conn._pg
        with conn._lock:
            try:
                with pg.transaction():
                    pg.execute("SET TRANSACTION READ ONLY")
                    pg.execute(f"SET LOCAL statement_timeout = {int(seconds * 1000)}")
                    (got,) = pg.execute(sql, params, prepare=False).fetchone()
                    return json.loads(got, parse_float=Decimal)
            except psycopg.errors.UndefinedTable:
                raise ToolError("view_not_built", "this business view has not been built "
                                "yet", "use plx.table") from None
            except psycopg.errors.QueryCanceled:
                raise ToolError("timeout", f"the query ran past {seconds:.0f} s",
                                "narrow it with where") from None
            except psycopg.Error as exc:
                raise ToolError("query_failed", f"the store refused the query "
                                f"({type(exc).__name__})") from None


# An overview's party: the text in the customer's name or its chain (store accounts roll up)
PARTY = "party"
PARTY_PAIR = {"name": "chain", "customer": "customer_chain"}


def overview_requests(text: str, today: Any) -> list[tuple[str, dict[str, Any]]]:
    """The overview's sections as plx.business requests (``party``: the text in the
    customer's name or chain): accounts by chain, open orders and their value, what they
    owe by aging bucket, invoiced in the last 90 days and the same days a year earlier,
    the latest invoice and order."""
    import datetime as dt
    start = today - dt.timedelta(days=89)
    try:
        ly_start, ly_end = start.replace(year=start.year - 1), today.replace(year=today.year - 1)
    except ValueError:   # 29 February
        ly_start, ly_end = start - dt.timedelta(days=365), today - dt.timedelta(days=365)

    def party(field: str = "customer") -> dict[str, Any]:
        return {"field": field, "op": PARTY, "value": text}
    return [
        ("accounts", {"view": "customers", "where": [party("name")], "group_by": ["chain"],
                      "measures": [{"fn": "count"}]}),
        ("open_orders", {"view": "sales_orders", "where": [
            party(), {"field": "status", "op": "=", "value": "open"}],
            "measures": [{"fn": "count"}, {"fn": "sum", "field": "value"}]}),
        ("late_open_orders", {"view": "sales_orders", "where": [
            party(), {"field": "status", "op": "=", "value": "open"},
            {"field": "days_late", "op": ">", "value": 0}], "measures": [{"fn": "count"}]}),
        ("owed_by_aging", {"view": "customer_balances", "where": [party()],
                           "group_by": ["aging_bucket"],
                           "measures": [{"fn": "sum", "field": "amount_due"},
                                        {"fn": "count"}]}),
        ("invoiced_last_90_days", {"view": "sales_invoices", "where": [
            party(), {"field": "invoice_date", "op": "between",
                      "value": [start.isoformat(), today.isoformat()]}],
            "measures": [{"fn": "sum", "field": "subtotal"}, {"fn": "count"}]}),
        ("invoiced_same_days_last_year", {"view": "sales_invoices", "where": [
            party(), {"field": "invoice_date", "op": "between",
                      "value": [ly_start.isoformat(), ly_end.isoformat()]}],
            "measures": [{"fn": "sum", "field": "subtotal"}, {"fn": "count"}]}),
        ("latest_invoice", {"view": "sales_invoices", "where": [party()],
                            "measures": [{"fn": "max", "field": "invoice_date"},
                                         {"fn": "count"}]}),
        ("latest_order", {"view": "sales_orders", "where": [party()],
                          "measures": [{"fn": "max", "field": "order_date"},
                                       {"fn": "count"}]}),
    ]


def _list(value: Any, what: str, most: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _bad(f"{what} must be a list")
    if len(value) > most:
        raise _bad(f"at most {most} {what}")
    return value


def _col(view: bv.View, name: Any) -> bv.Col:
    col = view.col(name) if isinstance(name, str) else None
    if col is None:
        raise ToolError("unknown_column", f"{view.name} has no column {str(name)[:64]!r}",
                        "columns: " + ", ".join(c.name for c in view.cols))
    return col


def _plan(view: bv.View, args: Mapping[str, Any], *, internal: bool = False
          ) -> dict[str, Any]:
    where = []
    extra_used = set()
    ops = (*OPS, PARTY) if internal else OPS   # party: the overview's own
    for item in _list(args.get("where"), "where", MAX_WHERE):
        if not isinstance(item, Mapping) or item.get("op") not in ops:
            raise _bad("a where item is {field, op, value}", "ops: " + ", ".join(OPS))
        col = _col(view, item.get("field"))
        op, value = item["op"], item.get("value")
        if op == PARTY:
            if col.name not in PARTY_PAIR or view.col(PARTY_PAIR[col.name]) is None:
                raise _bad(f"{view.name} has no party columns")
            extra_used.add(PARTY_PAIR[col.name])
            where.append((col, op, _scalar(col, value)))
            continue
        if op in ("is_null", "not_null"):
            value = None
        elif op == "in":
            if not isinstance(value, list) or not value or len(value) > MAX_IN:
                raise _bad(f"in takes a list of 1..{MAX_IN} values")
            value = [_scalar(col, v) for v in value]
        elif op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise _bad("between takes [low, high]")
            value = [_scalar(col, v) for v in value]
        else:
            value = _scalar(col, value)
            if op in ("contains", "starts_with") and col.kind not in ("text", "currency"):
                raise _bad(f"{op} applies to text columns")
        where.append((col, op, value))
    # rows outside the system of record (BC pilot data, orders no Sage record carries) are
    # left out unless the request asks about them
    grouped_on = {item if isinstance(item, str) else (item or {}).get("field")
                  for item in args.get("group_by") or () if isinstance(item, (str, Mapping))}
    left_out = []
    for name, kept, _reason, _note in DEFAULT_FILTERS:
        col = view.col(name)
        if col is not None and all(c.name != name for c, _, _ in where) \
                and name not in grouped_on:
            where.append((col, "=", kept))
            left_out.append(name)
    groups = []
    for item in _list(args.get("group_by"), "group_by", MAX_GROUP):
        name, bucket = (item, None) if isinstance(item, str) else (
            item.get("field"), item.get("bucket")) if isinstance(item, Mapping) else (None, None)
        col = _col(view, name)
        if bucket is not None and (bucket not in BUCKETS or col.kind != "date"):
            raise _bad("bucket applies to date columns: " + ", ".join(BUCKETS))
        groups.append((col, bucket))
    measures = []
    for item in _list(args.get("measures"), "measures", MAX_MEASURES):
        if not isinstance(item, Mapping) or item.get("fn") not in FNS:
            raise _bad("a measure is {fn, field}", "fns: " + ", ".join(FNS))
        fn = item["fn"]
        col = None if item.get("field") is None else _col(view, item["field"])
        if fn in ("sum", "avg") and (col is None or col.kind not in NUMERIC):
            raise _bad(f"{fn} needs a number or money column")
        if fn in ("count_distinct", "min", "max") and col is None:
            raise _bad(f"{fn} needs a column")
        measures.append((fn, col))
    select = [_col(view, n) for n in _list(args.get("select"), "select", MAX_SELECT)]
    if measures and select:
        raise _bad("ask for measures or select, not both")
    if not measures and not select:
        select = [c for c in view.cols][:MAX_SELECT]
    # every money measure is split by its currency: its currency column is a group key
    grouped = {c.name for c, b in groups if b is None}
    # and a quantity by its unit column (``<col>_unit``): never added across units
    for fn, col in measures:
        if col is None or fn in ("count", "count_distinct"):
            continue
        split = f"{col.name}_currency" if col.kind == "money" else f"{col.name}_unit"
        if view.col(split) is not None and split not in grouped:
            if len(groups) >= MAX_GROUP + 1:
                raise _bad("too many group keys with the currency or unit split")
            groups.append((view.col(split), None))
            grouped.add(split)
    order = []
    for item in _list(args.get("order_by"), "order_by", MAX_ORDER):
        if not isinstance(item, Mapping) or not isinstance(item.get("by"), str):
            raise _bad("an order_by item is {by, dir}")
        by, desc = item["by"], item.get("dir") == "desc"
        if measures:
            if by.startswith("m") and by[1:].isdigit() and int(by[1:]) < len(measures):
                order.append((f"m{int(by[1:])}", desc))
                continue
            idx = next((i for i, (c, _) in enumerate(groups) if c.name == by), None)
            if idx is None:
                raise _bad("order a grouped read by m0, m1.. or a group key")
            order.append((f"g{idx}", desc))
        else:
            order.append((_col(view, by).name, desc))
    limit = args.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise _bad(f"limit is 1..{MAX_LIMIT}")
    used = extra_used | {c.name for c, _, _ in where} | {c.name for c, _ in groups} | \
        {c.name for _, c in measures if c is not None} | {c.name for c in select} | \
        {n for n, _ in order if view.col(n) is not None}
    return {"where": where, "groups": groups, "measures": measures, "select": select,
            "order": order, "limit": limit, "used": sorted(used),
            "left_out": left_out}


def _scalar(col: bv.Col, value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        if col.kind == "boolean" and isinstance(value, bool):
            return value
        raise _bad(f"{col.name}: a string or number")
    text = str(value)
    if len(text) > MAX_TEXT:
        raise _bad(f"{col.name}: value too long")
    if col.kind == "boolean":   # true or false only: anything else fails, never a guess
        word = text.strip().lower()
        if word not in ("true", "false"):
            raise _bad(f"{col.name} is true or false: {text[:40]!r} is not")
        return word == "true"
    if col.kind in NUMERIC:
        try:
            return str(Decimal(text))
        except ArithmeticError:
            raise _bad(f"{col.name} is a number: {text[:40]!r} is not") from None
    if col.kind == "date":
        if bv.to_date(text) is None or len(text) < 10:
            raise _bad(f"{col.name} is a date: give YYYY-MM-DD")
        return text[:10]
    return text


_CAST = {"money": "::numeric", "numeric": "::numeric", "integer": "::numeric",
         "date": "::date", "boolean": "::boolean"}


def _condition(col: bv.Col, op: str, value: Any, q: _Sql) -> str:
    c, cast = _q(col.name), _CAST.get(col.kind, "::text")
    if op == "is_null":
        return f"{c} IS NULL"
    if op == "not_null":
        return f"{c} IS NOT NULL"
    if op == "in":
        return f"{c} = ANY({q.bind(value)}{cast}[])"
    if op == "between":
        return f"{c} BETWEEN {q.bind(value[0])}{cast} AND {q.bind(value[1])}{cast}"
    if op == PARTY:
        pattern = q.bind("%" + _like(value) + "%")
        return f"({c} ILIKE {pattern} OR {_q(PARTY_PAIR[col.name])} ILIKE {pattern})"
    if op == "contains":
        return f"{c} ILIKE {q.bind('%' + _like(value) + '%')}"
    if op == "starts_with":
        return f"{c} ILIKE {q.bind(_like(value) + '%')}"
    if col.kind in ("text", "currency") and op in ("=", "!="):
        return f"lower({c}) {'=' if op == '=' else '<>'} lower({q.bind(value)})"
    return f"{c} {'<>' if op == '!=' else op} {q.bind(value)}{cast}"


def _column_ok(name: str, marks: str) -> str:
    """Whether the reader may read column ``name`` (a bound parameter) of a row: its cell's
    marking set is one it holds, and everything only that column was read from (its
    ``needs``: audience keys, marking sets, not blocked; its ``col_objects``: no document
    the reader may not open) is held too. Unknown or NULL: not readable."""
    need = f"_col_needs->{name}"
    return (f"((col_marks->>{name} IS NULL OR col_marks->>{name} = ANY({marks}::text[])) "
            f"AND ({need} IS NULL OR (COALESCE(({need}->>'blocked')::boolean, true) = false "
            f"AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE({need}->"
            f"'audience_keys', '[]')) _ca(k) WHERE _ca.k NOT IN (SELECT k FROM _aud)) "
            f"AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE({need}->"
            f"'rec_marks', '[]')) _cm(k) WHERE _cm.k NOT IN (SELECT k FROM _mk)))) "
            f"AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(_col_objects->"
            f"{name}, '[]')) _co(k) WHERE NOT (_co.k NOT IN (SELECT k FROM _dm))))")


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _compile(view: bv.View, plan: Mapping[str, Any], scope: Any,
             docs: Mapping[str, list[str]]) -> tuple[str, dict[str, Any]]:
    q = _Sql()
    marks = q.bind(list(scope.marking_sets))
    # The reader's sets as hashed lookups: array containment (<@, &&) compares every
    # element with every element, and a full reader holds ~9.5k audience keys (76 s over
    # the 77k sales orders; 0.2 s this way). A row with a NULL key or marking, or any
    # key or marking outside the reader's sets, is not readable (as <@ was: fail closed).
    sets = (f"_aud AS MATERIALIZED (SELECT k FROM unnest({q.bind(list(scope.audience_keys))}"
            f"::text[]) k WHERE k IS NOT NULL), "
            f"_mk AS MATERIALIZED (SELECT k FROM unnest({marks}::text[]) k "
            f"WHERE k IS NOT NULL), "
            f"_dm AS MATERIALIZED (SELECT k FROM unnest({q.bind(list(docs.get('dm_ids') or []))}"
            f"::text[] || {q.bind(list(docs.get('dm_own') or []))}::text[]) k "
            f"WHERE k IS NOT NULL), ")
    readable = ("NOT _blocked AND "
                "NOT EXISTS (SELECT 1 FROM unnest(audience_keys) _a(k) WHERE _a.k IS NULL "
                "OR _a.k NOT IN (SELECT k FROM _aud)) "
                "AND NOT EXISTS (SELECT 1 FROM unnest(rec_marks) _r(k) WHERE _r.k IS NULL "
                "OR _r.k NOT IN (SELECT k FROM _mk)) "
                # NOT (.. NOT IN ..): a hashed subplan built once; IN became a semi join
                # rebuilding the hash of every withheld id for each row
                "AND NOT EXISTS (SELECT 1 FROM unnest(object_ids || _read_ids) _o(k) "
                "WHERE NOT (_o.k NOT IN (SELECT k FROM _dm)))")
    cols_ok = " AND ".join(_column_ok(q.bind(n), marks) for n in plan["used"]) or "true"
    where = " AND ".join(_condition(c, op, v, q) for c, op, v in plan["where"]) or "true"
    # door v2 tables: the row's audience/marking pattern, column marks and lineage are
    # joined back from their side tables (a row whose class or lineage is missing: out);
    # a row naming Freightcom (ai_ok false) never reaches a model
    v2 = (f"(SELECT t.*, a.audience_keys, a.rec_marks, a.blocked AS _blocked, "
          f"c.marks AS col_marks, c.needs AS _col_needs, l.cites, l.object_ids, "
          f"l.read_ids AS _read_ids, "
          f"l.col_objects AS _col_objects, l.systems, "
          f"l.copies FROM {bv.SCHEMA}.{view.name} t "
          f"JOIN {bv.SCHEMA}.access_classes a ON a.id = t.access_class "
          f"JOIN {bv.SCHEMA}.col_classes c ON c.id = t.col_class "
          f"JOIN {bv.SCHEMA}.lineage l ON l.view = {q.bind(view.name)} "
          f"AND l.handle = t.handle WHERE t.ai_ok) src")
    base = (sets + f"base AS MATERIALIZED (SELECT *, ({cols_ok}) AS _ok FROM {v2} "
            f"WHERE {readable}), "
            f"rows AS (SELECT * FROM base WHERE _ok AND {where})")
    withheld = "(SELECT count(*) FROM base WHERE NOT _ok)"
    limit = int(plan["limit"])
    if plan["measures"]:
        keys = []
        for i, (col, bucket) in enumerate(plan["groups"]):
            expr = _q(col.name) if bucket is None else \
                f"date_trunc({q.bind(bucket)}, {_q(col.name)})::date"
            keys.append((f"g{i}", expr, col.name))
        sel = [f"{e} AS {k}" for k, e, _ in keys]
        sel += [f"(array_agg(cites->{q.bind(n)}) FILTER (WHERE cites ? {q.bind(n)}))[1] "
                f"AS {k}_c" for k, _, n in keys]
        sel += ["count(*) AS _members",
                f"(array_agg(handle ORDER BY handle))[1:{MAX_CITED}] AS _cited"]
        for i, (fn, col) in enumerate(plan["measures"]):
            m = f"m{i}"
            if fn == "count":
                sel.append(f"count(*) AS {m}")
                continue
            c = _q(col.name)
            agg = {"count_distinct": f"count(DISTINCT {c})", "sum": f"sum({c})",
                   "avg": f"avg({c})", "min": f"min({c})", "max": f"max({c})"}[fn]
            sel.append(f"{agg} AS {m}")
            sel.append(f"count({c}) AS {m}_in")
            name = q.bind(col.name)
            sel.append(f"(array_agg(cites->{name} ORDER BY handle) FILTER (WHERE {c} IS NOT NULL "
                       f"AND cites ? {name}))[1:{MAX_CITED}] AS {m}_c")
            if fn == "sum":
                sel.append(f"count(*) FILTER (WHERE {c} < 0) AS {m}_neg")
            if col.kind == "money":
                cur = _q(col.name + "_currency")
                sel.append(f"CASE WHEN count(DISTINCT {cur}) = 1 THEN min({cur}) END AS {m}_cur")
        group = (" GROUP BY " + ", ".join(k for k, _, _ in keys)) if keys else ""
        order = ", ".join(f"{k} {'DESC' if d else 'ASC'} NULLS LAST" for k, d in plan["order"])
        order = order or ", ".join(k for k, _, _ in keys) or "1"
        sql = (f"WITH {base}, g AS (SELECT {', '.join(sel)} FROM rows{group}) "
               f"SELECT json_build_object('rows', (SELECT coalesce(json_agg(t), '[]') FROM "
               f"(SELECT * FROM g ORDER BY {order} LIMIT {limit}) t), "
               f"'total', (SELECT count(*) FROM g), 'withheld', {withheld})::text")
        return sql, q.params
    names = [c.name for c in plan["select"]]
    picks = ", ".join(_q(n) for n in dict.fromkeys(
        names + [n + "_currency" for n in names if view.col(n).kind == "money"]))
    order = ", ".join(f"{_q(n)} {'DESC' if d else 'ASC'} NULLS LAST" for n, d in plan["order"])
    order = (order + ", " if order else "") + "handle"
    sql = (f"WITH {base} SELECT json_build_object('rows', (SELECT coalesce(json_agg(t), '[]') "
           f"FROM (SELECT handle AS _h, object_ids AS _oids, systems AS _sys, copies AS _copies, "
           f"cites AS _cites, {picks} FROM rows ORDER BY {order} LIMIT {limit}) t), "
           f"'total', (SELECT count(*) FROM rows), 'withheld', {withheld})::text")
    return sql, q.params


def _num(value: Any) -> str | None:
    if value is None:
        return None
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


UNIT_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9._/%-]{0,11}")


def _shown(col: bv.Col, value: Any, cite: Mapping[str, str] | None = None) -> Any:
    """A cell as the tools state it: a number as its Decimal text; a date or a value the
    build computed (no source cell cited) bare; source text as an ``untrusted_text``
    envelope citing its record and field."""
    if value is None:
        return None
    if col.kind in NUMERIC:
        return _num(value)
    if isinstance(value, bool) or col.kind in ("date", "boolean", "currency") or not cite \
            or (col.name.endswith("_unit") and UNIT_TOKEN.fullmatch(str(value))):
        # a unit of measure (EA, KG, L) is a code like a currency: bare, so sums by unit
        # key on it; anything longer or odder stays source text in an envelope
        return value if isinstance(value, (bool, str)) else str(value)
    from operating.ontology_tools import _envelope
    return _envelope(str(value), cite["l1_handle"], cite["field_path"], 2000)


def _cite(raw: Any) -> dict[str, str] | None:
    if isinstance(raw, list) and len(raw) == 2 and all(isinstance(x, str) for x in raw):
        return {"l1_handle": raw[0], "field_path": raw[1]}
    return None


def _answer(view: bv.View, plan: Mapping[str, Any], got: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"view": view.name}
    rows = [r for r in got.get("rows") or () if isinstance(r, Mapping)]
    gaps: list[dict[str, Any]] = []
    capped = False
    if plan["measures"]:
        groups = []
        for rec in rows:
            key, key_cites = {}, {}
            for i, (col, bucket) in enumerate(plan["groups"]):
                name = f"{col.name}@{bucket}" if bucket else col.name
                cite = _cite(rec.get(f"g{i}_c"))
                key[name] = _shown(col, rec.get(f"g{i}"), cite if bucket is None else None)
                if cite and bucket is None:
                    key_cites[name] = cite
            members = int(rec.get("_members") or 0)
            group: dict[str, Any] = {"key": key, "members": members,
                                     "members_cited": [h for h in rec.get("_cited") or ()
                                                       if isinstance(h, str)][:MAX_CITED]}
            if key_cites:
                group["key_citations"] = key_cites
            capped = capped or members > MAX_CITED
            measures = []
            for i, (fn, col) in enumerate(plan["measures"]):
                m: dict[str, Any] = {"fn": fn, "field": col.name if col else None}
                value = rec.get(f"m{i}")
                if fn in ("count", "count_distinct"):
                    m["value"] = str(int(value or 0))
                else:
                    first = next(iter(map(_cite, rec.get(f"m{i}_c") or ())), None)
                    m["value"] = _shown(col, value, first)
                if col is not None:
                    m["inputs"] = int(rec.get(f"m{i}_in") or 0)
                    m["inputs_citations"] = [c for c in map(_cite, rec.get(f"m{i}_c") or ())
                                             if c][:MAX_CITED]
                    capped = capped or m["inputs"] > MAX_CITED
                    if fn == "sum":
                        m["negative_inputs"] = int(rec.get(f"m{i}_neg") or 0)
                    if col.kind == "money" and fn != "count_distinct":
                        cur = rec.get(f"m{i}_cur")
                        if isinstance(cur, str) and len(cur) == 3:
                            m["currency"] = cur
                        else:
                            m["currency_basis"] = "not_stated"
                            m["value"] = None if m["inputs"] > 1 else m["value"]
                measures.append(m)
            group["measures"] = measures
            groups.append(group)
        out["groups"] = groups
    else:
        listed = []
        for rec in rows:
            cites = rec.get("_cites") if isinstance(rec.get("_cites"), Mapping) else {}
            values = {}
            for col in plan["select"]:
                cite = _cite(cites.get(col.name))
                cell: dict[str, Any] = {"value": _shown(col, rec.get(col.name), cite),
                                        "value_type": col.kind}
                if cite and cell["value"] is not None:
                    cell["citation"] = cite
                if col.kind == "money" and isinstance(rec.get(col.name + "_currency"), str):
                    cell["currency"] = rec[col.name + "_currency"]
                values[col.name] = cell
            listed.append({"handle": rec.get("_h"), "object_ids": rec.get("_oids") or [],
                           "systems": rec.get("_sys") or [], "copies": rec.get("_copies") or 0,
                           "values": values})
        out["rows"] = listed
    total = int(got.get("total") or 0)
    out["total_groups" if plan["measures"] else "total_rows"] = total
    out["completeness"] = ({"complete": True, "truncated": False, "returned": len(rows),
                            "total": total} if len(rows) >= total else
                           {"complete": False, "truncated": True,
                            "reason": "group_page" if plan["measures"] else "page_cap",
                            "returned": len(rows), "total": total})
    for name, _kept, reason, note in DEFAULT_FILTERS:
        if name in plan.get("left_out", ()):
            gaps.append(_gap("excluded", reason, view.name, note))
    if capped:
        gaps.append(_gap("unknown", "citations_capped", view.name,
                         f"a group has more than {MAX_CITED} inputs: the first {MAX_CITED} "
                         "are cited, every one is counted"))
    withheld = int(got.get("withheld") or 0)
    if withheld:
        out["withheld"] = {"properties": None, "objects": withheld,
                           "reason": "marking_and_audience"}
    out["partial"] = bool(withheld)
    out["gaps"] = gaps
    return out


def register(table: dict[str, dict[str, Any]], base: Any) -> BusinessTool:
    tool = BusinessTool(base)
    if TOOL_NAME in table:
        raise ValueError(f"tool already registered: {TOOL_NAME}")
    table[TOOL_NAME] = dict(TOOL, handler=lambda arguments, actor: tool.call(
        TOOL_NAME, arguments, actor))
    return tool


__all__ = ["TOOL", "TOOL_NAME", "TOOL_NAMES", "BusinessTool", "register"]
