"""``plx.table``: one structured read over the business records, computed in Postgres.

A request names one record type (``from``), filters (``where``), groups (``group_by``,
dates bucketed by day/week/month/quarter/year), measures (count, count_distinct, sum,
avg, min, max) and an order and limit, or, with no measure, the fields to list
(``select``). A field is one of the type's own properties, ``system`` / ``binding`` (the
record's source) or ``object_id``, or ``<link>.<property>``: one declared forward link's
target record's property (``billed_to.name``), the target read under the same rules.

The request is compiled here into ONE read-only Postgres statement: never text a model
wrote, every value bound as a parameter, every property and link checked against the
compiled schema first. The reader's permissions are applied inside the statement, before
anything is filtered, grouped or counted:

* a record counts only when its audience key is one the reader may read, its current
  version carries only marking sets the reader holds, it is no document marked beyond
  the reader (``operating.search.document_marks``), and its citation scheme is a business
  system's (``api``/``bc``/``mc``). Captured mail (``Email``) is counted too, under the
  mail item gate's own rules applied inside the statement
  (``operating.ontology_tools_table_items``: the reader's mailboxes, the people exclusion
  over sender and To/Cc, Bcc looked up in the capture, Freightcom withheld; what SQL
  cannot decide is decided by the gate's functions and bound for a second pass, anything
  left undecided withheld). Captured Teams chat messages (``TeamsChatMessage``) are counted
  under the item gate's own decision: only messages of the chats whose membership admits
  the reader are read, each matching one decided by the gate's function in batches
  (``CaseRuntime.capture_resolve_many``) and bound for a second pass, Freightcom withheld,
  those not decided within the call's cap or time withheld. Attachments, Teams channels,
  SharePoint, calendar, Planner and original captures carry per-item permissions this
  statement does not hold: their types are refused up front;
* a value counts only when the cell the schema's precedence ranks first carries markings
  the reader holds; a record with a used field the reader may not see, or a linked
  record it may not read, is left out of every group and counted as ``withheld``, and the
  answer is ``partial`` (never presented as complete);
* a link is followed only at a solid tier (verified/reviewed/corroborated), when its own
  markings are held, every evidence handle it carries is a business-system handle (or the
  record's own), no end is never-authority staging, and the target passes the same record
  checks. With ``leads`` a record with no such solid link follows its lead-tier links
  (a guess, e.g. a sender matched by domain only): its group counts it in
  ``lead_members``, its row says ``lead``, and a ``lead_basis`` gap makes the answer gate
  bind those counts as leads; without it a ``possibly_related`` gap names the link types
  read that hold lead links. A record linked to several targets (one party's copies in
  several systems) reads a property's value when every target gives that one value, the
  most authoritative cited; targets giving different values, or an object id of several,
  give no key (an ``ambiguous_link`` gap states how many). A null test on a link's own
  fields (object_id/system/binding) asks only whether the record links at all.

One record held in several systems (Sage, Portal, BC copies of one order) is one member of
a group (``merge_twins``, default on): records joined by an identity link type of the type
(``identity_link_type``) at an identity tier (verified/reviewed), live, its markings held,
its evidence business-system handles, neither end never-authority (R1: such a link is a
lead, as ``_effective_tier`` serves it) and both ends readable by this reader, over any
number of hops, are one cluster; per group the copy holding the first measured field's
value from the most authoritative source stands for it and the others are counted in the
group's ``twins``. A reader who may not read a copy is never merged through it.

A sum, average, minimum or maximum of money is always split by the cell's currency (a
quantity by its unit): never one total across currencies. Amounts whose cells state no
currency are never combined: such a measure over more than one record has no value and a
``currency_unknown`` gap (one record's own amount stands, ``currency_basis`` not_stated). Every group cites up to 50 of
its member records and, per measure, up to 50 of the cells it read (a
``citations_capped`` gap past that) and states how many inputs it read, so the answer
gate binds counts and sums to it (``claim_checks_values.returned_evidence``).

Served only over a Postgres store (``PgStoreReader``): any other store is refused
(``not_served``), never answered from a partial read."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from operating.ontology_tools_identity import _gap, ToolError
from operating.ontology_tools_policy import _format_time

TOOL_NAME = "plx.table"
TOOL_NAMES = (TOOL_NAME,)

# Citation schemes of the business systems whose records carry no per-item permission.
TABLE_SCHEMES = ("api", "bc", "mc")
SOLID_TIERS = ("verified", "reviewed", "corroborated")
# the tiers at which an identity link makes two records one (ontology_tools_policy._IDENTITY_TIERS,
# the rule plx.aggregate's twins follow)
IDENTITY_TIERS = ("verified", "reviewed")
NEVER_AUTHORITY_LINEAGES = ("gold_native_staging",)
OPS = ("=", "!=", "<", "<=", ">", ">=", "in", "contains", "starts_with", "is_null",
       "not_null", "between")
FNS = ("count", "count_distinct", "sum", "avg", "min", "max")
BUCKETS = ("day", "week", "month", "quarter", "year")
NUMERIC = frozenset({"integer", "decimal", "money", "quantity"})
DATED = frozenset({"date", "timestamp"})
# The company's time zone: a timestamp's day, week, month (filters and buckets) is its
# calendar day here, never the UTC day it is stored in.
BUSINESS_TZ = "America/New_York"
PSEUDO = {"system": "text", "binding": "text", "object_id": "text"}
MAX_WHERE, MAX_GROUP, MAX_MEASURES, MAX_SELECT, MAX_ORDER = 12, 3, 4, 12, 3
MAX_IN, MAX_TEXT = 200, 200
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
MAX_CITED = 50
STATEMENT_SECONDS = 20.0
# seconds kept for the mail rules' second pass beyond the first pass's own time
SECOND_PASS_MARGIN = 1.0
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# Record types read through per-item permissions or held as files: refused here.
_ITEM_SOURCES = frozenset({"graph_mail", "sharepoint", "original", "teams", "meetings",
                           "planner", "calendar"})

_CONTRACT = json.loads((Path(__file__).with_name("ontology_tool_contracts.json"))
                       .read_text(encoding="utf-8"))["tools"][TOOL_NAME]
DESCRIPTION: str = _CONTRACT["description"]
INPUT_SCHEMA: dict[str, Any] = _CONTRACT["input"]

TOOL: dict[str, Any] = {"name": TOOL_NAME, "description": DESCRIPTION,
                        "inputSchema": INPUT_SCHEMA, "annotations": {"readOnlyHint": True}}


# --------------------------------------------------------------------------- request

@dataclass
class _Field:
    """One requested field: its request name, the link it goes through (None: the
    record's own), the property or pseudo name and its declared value type."""
    name: str
    link: str | None
    prop: str
    value_type: str
    pseudo: bool = False
    spec: Mapping[str, Any] = dc_field(default_factory=dict)


def _bad(message: str, hint: str | None = None) -> ToolError:
    return ToolError("invalid_input", message, hint)


class TableTool:
    """``plx.table`` over a host's ``OntologyTools`` (``base``): its store must be a
    Postgres reader; scope, schema and clock are the base's, so every call reads under
    the policy the host set on it for that call."""

    def __init__(self, base: Any) -> None:
        self.base = base

    # ------------------------------------------------------------------ entry

    def call(self, name: str, arguments: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        if name != TOOL_NAME:
            return {"error": {"code": "unknown_tool", "message": f"{name} is not plx.table"}}
        try:
            return self._call(dict(arguments or {}), actor)
        except ToolError as exc:
            error = {"code": exc.code, "message": exc.message[:600]}
            if exc.hint:
                error["hint"] = exc.hint[:600]
            return {"error": error}

    def _call(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        unknown = sorted(set(args) - set(INPUT_SCHEMA["properties"]))
        if unknown:
            raise _bad(f"unknown request keys: {', '.join(unknown)}",
                       "use from, where, group_by, measures, select, order_by, limit")
        plan = self._plan(args)
        conn = getattr(getattr(self.base, "store", None), "conn", None)
        if getattr(conn, "_pg", None) is None or not getattr(conn, "postgres", False):
            raise ToolError("not_served", "plx.table reads the Postgres store only, and "
                            "this host serves another", "use plx.aggregate")
        at = self.base.clock()
        scope = self.base._scope(actor, at)
        docs = self._documents(scope)
        if _driver(plan) is not None:
            plan = dict(plan, unheld_date_kinds=self._unheld_date_kinds(conn, scope))
        rules = None
        if plan["family"] is not None:
            from operating.ontology_tools_table_items import item_rules
            rules = item_rules(plan["family"], self.base, actor)
        sql, params = _compile(plan, scope, docs, rules)
        started = time.monotonic()
        got = self._run(conn, sql, params)
        if rules is not None and rules.needs_second(got):
            # what SQL could not decide, decided by the gate's own functions, then bound;
            # the lookups leave the second pass the first one's time (and a margin)
            from operating import call_limits
            left = call_limits.seconds_left()
            first = time.monotonic() - started
            rules.learn(got, None if left is None else
                        time.monotonic() + left - 1.25 * first - SECOND_PASS_MARGIN)
            sql, params = _compile(plan, scope, docs, rules)
            got = self._run(conn, sql, params)
        out = self._answer(plan, got, scope, at)
        if rules is not None:
            out["gaps"] = list(out["gaps"]) + [
                _gap("unknown", "item_rules_capped", plan["type"], text)
                for text in rules.gaps()]
            if got.get("withheld_items"):
                out["gaps"].append(_gap(
                    "unknown", f"{plan['family']}_withheld", plan["type"],
                    f"{int(got['withheld_items'])} of the withheld messages are left out by "
                    f"the {plan['family']} rules: {rules.withheld_note}"))
        return out

    def _unheld_date_kinds(self, conn: Any, scope: Any) -> list[int]:
        """The value kinds of dates whose markings this reader does not hold: a date
        filter's index read (``_driver``) keeps every record holding such a cell, so a
        value the reader may not see is withheld as before, never sorted by its value."""
        pg = conn._pg
        with conn._lock:
            with pg.transaction():
                pg.execute("SET TRANSACTION READ ONLY")
                rows = pg.execute("SELECT kind FROM value_kinds WHERE value_type = ANY("
                                  "%(t)s::text[]) AND NOT (markings = ANY(%(m)s::text[]))",
                                  {"t": sorted(DATED), "m": list(scope.marking_sets)},
                                  prepare=False).fetchall()
        return sorted(int(k) for (k,) in rows)

    def _documents(self, scope: Any) -> dict[str, list[str]]:
        from operating.search import document_marks
        got = document_marks.params(scope.held)
        # only the id/type lists are JSON; ``dm_people`` is a plain tag the item gates read
        return {k: list(json.loads(got[k])) for k in ("dm_ids", "dm_types", "dm_own") if k in got}

    # ------------------------------------------------------------------ plan

    def _plan(self, args: Mapping[str, Any]) -> dict[str, Any]:
        schema = getattr(self.base, "schema", None)
        if schema is None:
            raise ToolError("schema_unavailable", "the compiled ontology schema is not "
                            "loaded, so fields cannot be checked")
        otype = args.get("from")
        if not isinstance(otype, str) or otype not in schema.object_types:
            raise ToolError("unknown_type", f"no record type {str(otype)[:64]!r}",
                            "call plx.schema for the record types")
        spec = schema.object_types[otype]
        from operating.search.document_marks import FILE_TYPES
        sources = {b.get("source") for b in spec.get("bindings") or ()
                   if isinstance(b, Mapping)}
        from operating.ontology_tools_table_items import item_family
        family = item_family(otype)
        if family is None and (otype in FILE_TYPES or "Document" in (
                spec.get("interfaces") or ()) or (sources and sources <= _ITEM_SOURCES)):
            raise ToolError("not_served", f"{otype} records carry per-item permissions "
                            "plx.table does not apply", "use plx.documents or plx.find")
        fields: dict[str, _Field] = {}

        def field(name: Any) -> _Field:
            if not isinstance(name, str) or not name:
                raise _bad("a field must be a non-empty string")
            if name not in fields:
                fields[name] = self._field(schema, otype, name)
            return fields[name]

        where = []
        for item in _list(args.get("where"), "where", MAX_WHERE):
            if not isinstance(item, Mapping):
                raise _bad("each where entry is an object {field, op, value}")
            if set(item) - {"field", "op", "value"}:
                raise _bad("a where entry takes field, op and value only")
            f, op = field(item.get("field")), item.get("op")
            if op not in OPS:
                raise _bad(f"unknown op {str(op)[:20]!r}", f"use one of {', '.join(OPS)}")
            where.append((f, op, _local_bound(f, _operand(f, op, item.get("value"),
                                                          "value" in item))))
        groups = []
        for item in _list(args.get("group_by"), "group_by", MAX_GROUP):
            bucket = None
            if isinstance(item, Mapping):
                if set(item) - {"field", "bucket"}:
                    raise _bad("a group_by entry takes field and bucket only")
                bucket = item.get("bucket")
                item = item.get("field")
            f = field(item)
            if bucket is not None:
                if bucket not in BUCKETS:
                    raise _bad(f"unknown bucket {str(bucket)[:20]!r}",
                               f"use one of {', '.join(BUCKETS)}")
                if f.value_type not in DATED:
                    raise _bad(f"{f.name} is not a date; only dates take a bucket")
            groups.append((f, bucket))
        measures = []
        for item in _list(args.get("measures"), "measures", MAX_MEASURES):
            if not isinstance(item, Mapping) or set(item) - {"fn", "field"}:
                raise _bad("a measure is {fn, field}")
            fn = item.get("fn")
            if fn not in FNS:
                raise _bad(f"unknown measure {str(fn)[:20]!r}", f"use one of {', '.join(FNS)}")
            f = field(item["field"]) if item.get("field") is not None else None
            if f is None and fn != "count":
                raise _bad(f"{fn} needs a field")
            if fn in ("sum", "avg") and (f is None or f.value_type not in NUMERIC):
                raise _bad(f"{fn} needs a number, money or quantity field; "
                           f"{f.name if f else 'none'} is {f.value_type if f else 'none'}")
            if fn in ("sum", "avg") and f is not None and _unit_price(f):
                raise _bad(f"{f.name} is a unit price: a {fn} of prices is no total",
                           "sum the line amount instead")
            measures.append((fn, f))
        select = [field(name) for name in _list(args.get("select"), "select", MAX_SELECT)]
        if measures and select:
            raise _bad("select lists records; measures compute over groups: send one")
        if groups and not measures:
            measures = [("count", None)]
        if not measures and not select:
            raise _bad("name measures (to count/sum/rank) or select (to list records)")
        limit = args.get("limit", DEFAULT_LIMIT)
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise _bad(f"limit is an integer from 1 to {MAX_LIMIT}")
        order = []
        names = {f"m{i}" for i in range(len(measures))} if measures else set()
        keyed = {f.name for f, _ in groups} if measures else {f.name for f in select}
        for item in _list(args.get("order_by"), "order_by", MAX_ORDER):
            if not isinstance(item, Mapping) or set(item) - {"by", "dir"}:
                raise _bad("an order_by entry is {by, dir}")
            by, direction = item.get("by"), item.get("dir", "desc" if measures else "asc")
            if by not in names | keyed:
                raise _bad(f"order_by {str(by)[:60]!r} is neither a measure (m0..) nor a "
                           "grouped or selected field")
            if direction not in ("asc", "desc"):
                raise _bad("dir is asc or desc")
            order.append((by, direction))
        merge = args.get("merge_twins", True)
        if type(merge) is not bool:
            raise _bad("merge_twins is true or false")
        leads = args.get("leads", False)
        if type(leads) is not bool:
            raise _bad("leads is true or false")
        # the identity link types joining two records of this type (one thing held in two
        # systems): merged in groups unless the request asks for every copy
        twins = sorted(lid for lid in schema.identity_link_types()
                       if schema.link_types[lid].get("from") == otype
                       and schema.link_types[lid].get("to") == otype) \
            if merge and measures else []
        types = {otype} | {schema.link_types[f.link]["to"] for f in fields.values() if f.link}
        sources = sorted({b.get("source") for t in types
                          for b in schema.object_types[t].get("bindings") or ()
                          if isinstance(b, Mapping) and isinstance(b.get("source"), str)
                          and (b.get("source") not in _ITEM_SOURCES
                               or family is not None and t == otype)})
        return {"type": otype, "family": family, "fields": fields, "sources": sources,
                "types": sorted(types),
                # the links whose ends' values are read (a twin link joins two records
                # of ``type`` itself, whose relations ``types`` already names)
                "links": sorted({f.link for f in fields.values() if f.link}),
                "where": where, "groups": groups,
                "measures": measures, "select": select, "order": order, "limit": limit,
                "twins": twins, "leads": leads, "args": dict(args)}

    def _field(self, schema: Any, otype: str, name: str) -> _Field:
        if name in PSEUDO:
            return _Field(name, None, name, PSEUDO[name], pseudo=True)
        link, _, prop = name.rpartition(".")
        if not link:
            spec = _declared(schema, otype).get(name)
            if spec is None or not _NAME.match(name):
                raise ToolError("unknown_property", f"{otype} declares no property "
                                f"{name[:80]!r}", f"call plx.schema with object_type {otype}")
            return _Field(name, None, name, spec.get("value_type") or "text", spec=spec)
        if "." in link or not _NAME.match(link):
            raise _bad(f"{name[:80]!r}: one link hop only (<link>.<property>)")
        link_id = f"{otype}.{link}"
        declared = schema.link_types.get(link_id)
        if declared is None or declared.get("from") != otype:
            raise ToolError("undeclared_link", f"{otype} declares no link {link!r}",
                            f"call plx.schema with object_type {otype} for its links")
        if link_id in schema.identity_link_types():
            raise _bad(f"{link} joins one record's copies; it is no field to read through")
        target = declared.get("to")
        if prop in PSEUDO:
            return _Field(name, link_id, prop, PSEUDO[prop], pseudo=True)
        spec = _declared(schema, target).get(prop)
        if spec is None or not _NAME.match(prop):
            raise ToolError("unknown_property", f"{target} (through {link}) declares no "
                            f"property {prop[:80]!r}", f"call plx.schema with object_type "
                            f"{target}")
        return _Field(name, link_id, prop, spec.get("value_type") or "text", spec=spec)

    # ------------------------------------------------------------------ run

    def _run(self, conn: Any, sql: str, params: Mapping[str, Any]) -> dict[str, Any]:
        from operating import call_limits
        left = call_limits.seconds_left()
        if left is not None and left <= 0:
            raise ToolError("timeout", "no time is left in this call for plx.table")
        seconds = STATEMENT_SECONDS if left is None else max(0.5, min(STATEMENT_SECONDS,
                                                                       left - 0.5))
        # native SQL, never the SQLite dialect the connection translates: the person-link
        # guard (``PgCompatConnection._guarded``) hides only same-person identity links,
        # which no field is read through (``_field`` refuses identity links)
        text = sql
        import psycopg
        pg = conn._pg
        with conn._lock:
            try:
                with pg.transaction():
                    pg.execute("SET TRANSACTION READ ONLY")
                    pg.execute(f"SET LOCAL statement_timeout = {int(seconds * 1000)}")
                    (got,) = pg.execute(text, params, prepare=False).fetchone()
                    return json.loads(got, parse_float=Decimal)
            except psycopg.errors.QueryCanceled:
                raise ToolError("timeout", f"the query ran past {seconds:.0f} s",
                                "narrow it with where (a date range, a status)") from None
            except psycopg.Error as exc:
                raise ToolError("query_failed", f"the store refused the query "
                                f"({type(exc).__name__})") from None

    # ------------------------------------------------------------------ answer

    def _answer(self, plan: Mapping[str, Any], got: Mapping[str, Any], scope: Any,
                at: dt.datetime) -> dict[str, Any]:
        out: dict[str, Any] = {"type": plan["type"]}
        gaps: list[dict[str, Any]] = []
        capped = False
        rows = [r for r in got.get("rows") or () if isinstance(r, Mapping)]
        if plan["select"] and not plan["measures"]:
            out["rows"] = [_record(plan, rec) for rec in rows]
        else:
            groups = []
            for rec in rows:
                group, cut = _group(plan, rec)
                capped = capped or cut
                groups.append(group)
            out["groups"] = groups
        returned = len(rows)
        total = int(got.get("total") or 0)
        out["total_groups" if plan["measures"] else "total_rows"] = total
        out["completeness"] = ({"complete": True, "truncated": False, "returned": returned,
                                "total": total} if returned >= total else
                               {"complete": False, "truncated": True, "reason": "group_page"
                                if plan["measures"] else "page_cap",
                                "returned": returned, "total": total})
        if capped:
            gaps.append(_gap("unknown", "citations_capped", plan["type"],
                             f"a group has more than {MAX_CITED} inputs: the first "
                             f"{MAX_CITED} are cited, every one is counted"))
        unstated = sum(1 for g in out.get("groups") or () for m in g["measures"]
                       if m.get("currency_basis") == "not_stated" and m["value"] is None
                       and m.get("inputs", 0) > 1)
        if unstated:
            gaps.append(_gap("unknown", "currency_unknown", plan["type"],
                             f"{unstated} group measures combine amounts whose records state "
                             "no currency: no value is given for them (amounts in several "
                             "currencies are never combined); list the records instead"))
        leads = int(got.get("leads") or 0)
        if leads:
            gaps.append(_gap("unknown", "lead_basis", plan["type"],
                             f"{leads} records are joined to a linked record only by a lead "
                             "link (a guess, e.g. by the sender's domain, never confirmed): "
                             "each group states how many (lead_members), each row lead"))
        unfollowed = [t for t in got.get("lead_types") or () if isinstance(t, str)]
        if unfollowed:
            gaps.append(_gap("unknown", "possibly_related", plan["type"],
                             f"{', '.join(unfollowed)[:300]} also has lead-tier links (a "
                             "guess, e.g. by the sender's domain) this answer did not "
                             "follow; ask with leads: true to count them, labelled"))
        ambiguous = int(got.get("ambiguous") or 0)
        if ambiguous:
            gaps.append(_gap("unknown", "ambiguous_link", plan["type"],
                             f"{ambiguous} records link to more than one record through a "
                             "field read: they are in the group with no key there"))
        withheld = int(got.get("withheld") or 0)
        if withheld:
            # records whose used values or linked records this reader may not see: left
            # out of every group (the answer partial), counted here
            out["withheld"] = {"properties": None, "objects": withheld,
                               "reason": "marking_and_audience"}
        unwalked = sum(int(r.get("_unres") or 0) for r in rows)
        if unwalked:
            gaps.append(_gap("unknown", "twins_unresolved", plan["type"],
                             f"{unwalked} members have a copy in another system this read "
                             "could not join them with (a copy not readable or no longer "
                             "held): one record may be counted more than once"))
        out["partial"] = bool(withheld or unwalked)
        out["gaps"] = gaps + self._freshness_gaps(plan, at, out)
        return out

    def _freshness_gaps(self, plan: Mapping[str, Any], at: dt.datetime,
                        out: dict[str, Any]) -> list[dict[str, Any]]:
        """``meta`` (schema digest, as known at, the freshness of the sources the read
        types are bound to) and those sources' freshness gaps, as plx.query states them
        (``_freshness_gaps`` with the types read and the links walked): a source read only
        in part in relations that hold none of these types' records nor these links'
        says ``partly_read_elsewhere`` (nothing of this answer is missing), any other
        part read keeps ``source_partly_read`` (its counts and sums are lower bounds).
        None read: the sources' own gaps, unrefined (fail closed)."""
        try:
            store_meta = self.base._store_meta()
            meta = self.base._meta(store_meta, at, plan["sources"])
        except ToolError:
            return []
        out["meta"] = meta
        try:
            return list(self.base._freshness_gaps(meta, store_meta, plan.get("types") or (),
                                                  plan.get("links") or ()))
        except Exception:  # noqa: BLE001 - refined gaps unread: the sources' own
            try:
                return list(self.base._source_gaps(meta["freshness"]))
            except Exception:  # noqa: BLE001 - no freshness gaps read: none rather than fail
                return []


# --------------------------------------------------------------------------- helpers

def _list(value: Any, what: str, most: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > most:
        raise _bad(f"{what} is a list of at most {most}")
    return value


def _declared(schema: Any, otype: Any) -> dict[str, Mapping[str, Any]]:
    spec = schema.object_types.get(otype) or {}
    return {p["name"]: p for p in spec.get("properties") or () if isinstance(p, Mapping)}


def _unit_price(f: _Field) -> bool:
    from ontology.units import is_unit_price
    return is_unit_price(f.spec)


def _scalar(f: _Field, value: Any) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and f.value_type in NUMERIC:
        return Decimal(str(value))
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and len(value) <= MAX_TEXT:
        if f.value_type in NUMERIC:
            try:
                return Decimal(value.strip().replace(",", ""))
            except ArithmeticError:
                raise _bad(f"{f.name} is a number: {value[:40]!r} is not") from None
        return value
    raise _bad(f"a value for {f.name} is a string (at most {MAX_TEXT} characters), "
               "number or boolean")


def _operand(f: _Field, op: str, value: Any, given: bool) -> Any:
    if op in ("is_null", "not_null"):
        if given and value is not None:
            raise _bad(f"{op} takes no value")
        return None
    if not given or value is None:
        raise _bad(f"{op} on {f.name} needs a value")
    if op == "in":
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN:
            raise _bad(f"in takes a list of 1 to {MAX_IN} values")
        return [_scalar(f, v) for v in value]
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise _bad("between takes [low, high]")
        return [_scalar(f, v) for v in value]
    if op in ("contains", "starts_with"):
        if not isinstance(value, str) or not value.strip():
            raise _bad(f"{op} takes a non-empty string")
        if len(value) > MAX_TEXT:
            raise _bad(f"a value is at most {MAX_TEXT} characters")
        return value
    return _scalar(f, value)


_NUMBER = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
TEXT_LIMIT = 2000


def _source_text(value: Any, handle: Any, path: Any) -> Any:
    """A value read from a source as the tools state source text: an ``untrusted_text``
    envelope citing its record and field (a ``po_reference`` that is not PO-like
    withheld, ``ontology_tools._envelope``); a number as its Decimal text; None as None
    (and None when it cannot be cited)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, Decimal)):
        return _text(value)
    if _NUMBER.match(str(value)):
        return str(value)
    if not isinstance(handle, str):
        return None
    from operating.ontology_tools import _envelope
    return _envelope(str(value), handle, path if isinstance(path, str) else None, TEXT_LIMIT)


def _record(plan: Mapping[str, Any], rec: Mapping[str, Any]) -> dict[str, Any]:
    values = {}
    for i, f in enumerate(plan["select"]):
        values[f.name] = _cell(f, rec.get(f"s{i}_v"), rec.get(f"s{i}_n"),
                               rec.get(f"s{i}_h"), rec.get(f"s{i}_fp"),
                               rec.get(f"s{i}_cur"), rec.get(f"s{i}_u"))
    out = {"handle": rec["_h"], "object_id": rec["_oid"], "values": values}
    if plan.get("leads") and rec.get("_lead"):
        out["lead"] = True
    return out


def _cell(f: _Field, value: Any, num: Any, handle: Any, path: Any, currency: Any = None,
          unit: Any = None) -> dict[str, Any]:
    if f.value_type in NUMERIC and num is not None:
        shown: Any = _text(num)
    else:
        shown = _source_text(value, handle, path)
    out: dict[str, Any] = {"value": shown, "value_type": f.value_type}
    if handle and path:
        out["citation"] = {"l1_handle": handle, "field_path": path}
    if f.value_type == "money" and isinstance(currency, str) and _CURRENCY.match(currency):
        out["currency"] = currency
    if f.value_type == "quantity" and isinstance(unit, str) and unit:
        out["unit"] = unit
    return out


def _text(number: Any) -> str | None:
    if number is None:
        return None
    value = Decimal(number)
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _group(plan: Mapping[str, Any], rec: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    key: dict[str, Any] = {}
    cites: dict[str, Any] = {}
    handles = [h for h in rec.get("_cited") or () if isinstance(h, str)]
    for i, (f, bucket) in enumerate(plan["groups"]):
        name = f"{f.name}@{bucket}" if bucket else f.name
        value = rec.get(f"g{i}")
        cite = rec.get(f"g{i}_c")
        cited = isinstance(cite, list) and len(cite) == 2 and isinstance(cite[0], str) \
            and isinstance(cite[1], str)
        if f.value_type in NUMERIC and value is not None:
            try:
                value = _text(value)
            except ArithmeticError:
                pass   # a source text the number column did not parse: as written
        # a key is source text: enveloped, citing the cell it was read from (a pseudo
        # field's, the record's own handle)
        shown = _source_text(value, cite[0] if cited else (handles[0] if handles else None),
                             cite[1] if cited else None)
        key[name] = shown
        if cited and shown is not None and not (isinstance(shown, Mapping)
                                                and shown.get("withheld")):
            cites[name] = {"l1_handle": cite[0], "field_path": cite[1]}
    members = int(rec.get("_members") or 0)
    group: dict[str, Any] = {"key": key, "members": members}
    if rec.get("_twins"):
        group["twins"] = int(rec["_twins"])
    if plan.get("leads") and rec.get("_lead"):
        group["lead_members"] = int(rec["_lead"])
    if cites:
        group["key_citations"] = cites
    group["members_cited"] = handles[:MAX_CITED]
    capped = members > MAX_CITED
    out_measures = []
    for i, (fn, f) in enumerate(plan["measures"]):
        m: dict[str, Any] = {"fn": fn, "field": f.name if f else None}
        value = rec.get(f"m{i}")
        if fn in ("count", "count_distinct"):
            m["value"] = str(int(value or 0))
        elif f is not None and f.value_type in NUMERIC:
            m["value"] = _text(value)
        else:   # min/max of text or dates: the value of one cited cell
            first = (rec.get(f"m{i}_c") or [[None, None]])[0]
            m["value"] = _source_text(value, first[0], first[1])
        if f is not None:
            m["inputs"] = int(rec.get(f"m{i}_in") or 0)
            cells = [{"l1_handle": c[0], "field_path": c[1]}
                     for c in rec.get(f"m{i}_c") or () if isinstance(c, list) and len(c) == 2
                     and isinstance(c[0], str) and isinstance(c[1], str)]
            m["inputs_citations"] = cells[:MAX_CITED]
            if fn == "sum" and f.value_type in NUMERIC:
                m["negative_inputs"] = int(rec.get(f"m{i}_neg") or 0)
            capped = capped or m["inputs"] > MAX_CITED
            if f.value_type == "money" and fn in ("sum", "avg", "min", "max"):
                currency = rec.get(f"m{i}_cur")
                if isinstance(currency, str) and _CURRENCY.match(currency):
                    m["currency"] = currency
                else:
                    m["currency_basis"] = "not_stated"
                    if m["inputs"] > 1:
                        # amounts whose records state no currency may be in several
                        # (a BC aged row's): never added, averaged or ranked together
                        # (plx.aggregate refuses the same); one record's own amount stands
                        m["value"] = None
            if f.value_type == "quantity" and fn in ("sum", "avg", "min", "max") \
                    and isinstance(rec.get(f"m{i}_u"), str) and rec.get(f"m{i}_u"):
                m["unit"] = rec[f"m{i}_u"]
        out_measures.append(m)
    group["measures"] = out_measures
    return group, capped


# --------------------------------------------------------------------------- SQL

class _Sql:
    """Parameters bound by name (``%(pN)s``): every value the request carries."""

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}

    def bind(self, value: Any) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f"%({name})s"


def _visible(alias: str, ver: str, q: _Sql, scope: Any, docs: Mapping[str, list[str]],
             rules: Any = None) -> str:
    """The record checks of one object ``alias`` and its current version ``ver``: the
    reader's audiences and markings, a business system's handle, no document beyond the
    reader, and held by its source at the scope's time (``_held``). With ``rules``
    (``ontology_tools_table_items.MailRules``: the record read itself, never a linked one),
    a captured message is checked too: its audience key one of the mailboxes the mail gate
    serves this reader; the per-message rules are the caller's."""
    schemes, item = list(TABLE_SCHEMES), ""
    if rules is not None:
        schemes.append(rules.scheme)
        item = (f"AND (split_part({ver}.l1_handle, '://', 1) <> {q.bind(rules.scheme)} OR "
                f"{rules.audience_sql(alias, q)}) ")
    return (_held(alias, q, scope) + " AND "
            f"{alias}.audience_key = ANY({q.bind(list(scope.audience_keys))}::text[]) "
            f"AND {ver}.markings = ANY({q.bind(list(scope.marking_sets))}::text[]) "
            f"AND split_part({ver}.l1_handle, '://', 1) = ANY({q.bind(schemes)}"
            "::text[]) " + item + 
            f"AND NOT ({alias}.object_id = ANY({q.bind(docs.get('dm_ids') or [])}::text[])) "
            f"AND NOT ({alias}.object_id = ANY({q.bind(docs.get('dm_own') or [])}::text[])) "
            f"AND NOT ({alias}.object_type = ANY({q.bind(docs.get('dm_types') or [])}"
            "::text[])) ")


def _held(alias: str, q: _Sql, scope: Any) -> str:
    """True when the source of object ``alias`` held it at the scope's time: exactly the
    records ``ontology.store.removed_at_sql`` (the removal test ``StoreReader.removals``
    and plx.aggregate apply) does not hold for, in native Postgres. Its missing mark
    counts without a removal row; each removal row starts at its own time, else at the
    missing mark. A start or end time that does not parse is a removal of unknown time:
    in force at every time and never ended (fail closed: never read as held)."""
    at = f"ontology.sq_julianday({q.bind(_format_time(scope.at))})"
    since = f"COALESCE(rm.since, {alias}.missing_from_source_since)"
    return (f"NOT (({alias}.missing_from_source_since IS NOT NULL AND "
            f"COALESCE(ontology.sq_julianday({alias}.missing_from_source_since) <= {at}, "
            "true) AND NOT EXISTS (SELECT 1 FROM object_removals rm WHERE rm.object_id = "
            f"{alias}.object_id)) OR EXISTS (SELECT 1 FROM object_removals rm WHERE "
            f"rm.object_id = {alias}.object_id AND {since} IS NOT NULL AND "
            f"COALESCE(ontology.sq_julianday({since}) <= {at}, true) AND (rm.until IS NULL "
            f"OR COALESCE(ontology.sq_julianday(rm.until) > {at}, true))))")


def _pick(alias: str, ver: str, prop: str, q: _Sql, scope: Any) -> str:
    """A lateral join ``alias``: of version ``ver``'s live cells of ``prop``, the one the
    schema's precedence ranks first (authority rank, then field), and whether the reader
    holds its markings (``ok``). Read per record through the version and cell keys, after
    the filters before it have cut the records."""
    return (f"LEFT JOIN LATERAL (SELECT c.value AS v, c.value_num AS n, c.currency AS cur, "
            "k.unit AS u, f.field_path AS fp, k.authority_rank AS rk, "
            f"(k.markings = ANY({q.bind(list(scope.marking_sets))}::text[])) AS ok "
            "FROM value_sources s JOIN value_cells c ON c.src = s.src "
            "JOIN value_fields f ON f.field = c.field JOIN value_kinds k ON k.kind = c.kind "
            f"WHERE s.ver = {ver} AND s.superseded_at IS NULL AND f.property = {q.bind(prop)} "
            f"ORDER BY k.authority_rank NULLS LAST, c.field, c.value LIMIT 1) {alias} ON true")


def _link(alias: str, link: str, q: _Sql, scope: Any, docs: Mapping[str, list[str]],
          leads: bool = False) -> str:
    """A lateral join ``alias``: the record's live links of type ``link`` whose target the
    reader may read (``seen``: link markings held, every evidence handle a business
    system's or the record's own, no never-authority end, the target passing the record
    checks), at a solid tier, or, with ``leads`` and no solid one seen, at the lead tier
    (``lead``): the targets used (``tvers``/``ths``: versions and handles, in object id
    order), the first (``t``, ``tver``, ``th``, ``tb``), how many distinct targets
    (``n``) and bindings (``nb``) and how many links of a tier that would count are not
    seen (``hidden``)."""
    tiers = list(SOLID_TIERS) + (["lead"] if leads else [])
    use = "x.seen AND (x.solid OR NOT x.anysolid)"
    return (
        f"LEFT JOIN LATERAL (SELECT min(x.to_object) FILTER (WHERE {use}) AS t, "
        f"(array_agg(x.tv_ver ORDER BY x.to_object) FILTER (WHERE {use}))[1] AS tver, "
        f"(array_agg(x.th ORDER BY x.to_object) FILTER (WHERE {use}))[1] AS th, "
        f"(array_agg(x.tb ORDER BY x.to_object) FILTER (WHERE {use}))[1] AS tb, "
        f"array_agg(x.tv_ver ORDER BY x.to_object) FILTER (WHERE {use}) AS tvers, "
        f"array_agg(x.th ORDER BY x.to_object) FILTER (WHERE {use}) AS ths, "
        f"count(DISTINCT x.to_object) FILTER (WHERE {use}) AS n, "
        f"count(DISTINCT x.tb) FILTER (WHERE {use}) AS nb, "
        f"COALESCE(bool_or({use} AND NOT x.solid), false) AS lead, "
        "count(*) FILTER (WHERE NOT x.seen AND (x.solid OR NOT x.anysolid)) AS hidden "
        "FROM (SELECT y.*, bool_or(y.seen AND y.solid) OVER () AS anysolid FROM ("
        "SELECT l.to_object, tv.ver AS tv_ver, tv.l1_handle AS th, tob.binding_id AS tb, "
        f"l.tier = ANY({q.bind(list(SOLID_TIERS))}::text[]) AS solid, "
        "(tob.object_id IS NOT NULL AND tv.ver IS NOT NULL AND "
        f"l.markings = ANY({q.bind(list(scope.marking_sets))}::text[]) AND "
        "NOT (COALESCE(tv.lineage, '') = ANY("
        f"{q.bind(list(NEVER_AUTHORITY_LINEAGES))}::text[])) AND "
        "NOT EXISTS (SELECT 1 FROM json_array_elements_text(CASE WHEN "
        "l.evidence_handles IS NULL OR l.evidence_handles = '' THEN '[]'::json ELSE "
        "l.evidence_handles::json END) h(x) WHERE split_part(h.x, '://', 1) <> ALL("
        f"{q.bind(list(TABLE_SCHEMES))}::text[]) AND h.x <> v.l1_handle) AND "
        + _visible("tob", "tv", q, scope, docs) +
        ") AS seen FROM links l LEFT JOIN objects tob ON tob.object_id = l.to_object "
        "LEFT JOIN object_versions tv ON tv.object_id = l.to_object AND "
        "tv.superseded_at IS NULL "
        f"WHERE l.from_object = o.object_id AND l.link_type = {q.bind(link)} AND "
        # a link to a record its source no longer holds is no link now: neither a
        # target nor one withheld (the record is in the group with no key there)
        f"(tob.object_id IS NULL OR {_held('tob', q, scope)}) AND "
        f"l.superseded_at IS NULL AND l.tier = ANY({q.bind(tiers)}::text[]) "
        "AND NOT (COALESCE(v.lineage, '') = ANY("
        f"{q.bind(list(NEVER_AUTHORITY_LINEAGES))}::text[]))) y) x) {alias} ON true")


def _pick_linked(alias: str, lk: str, prop: str, q: _Sql, scope: Any) -> str:
    """A lateral join ``alias``: ``prop`` read on every target link ``lk`` uses, each
    target's first cell as ``_pick`` ranks it: the value of the most authoritative one
    (with its handle ``h``), whether the reader holds every one's markings (``ok``) and
    how many distinct values they give (``nv``): several targets giving one value (one
    party's copies in several systems) give that value, never an ambiguity."""
    one = ("SELECT c.value AS v, c.value_num AS n, c.currency AS cur, k.unit AS u, "
           "f.field_path AS fp, k.authority_rank AS rk, "
           f"(k.markings = ANY({q.bind(list(scope.marking_sets))}::text[])) AS ok "
           "FROM value_sources s JOIN value_cells c ON c.src = s.src "
           "JOIN value_fields f ON f.field = c.field JOIN value_kinds k ON k.kind = c.kind "
           f"WHERE s.ver = t.ver AND s.superseded_at IS NULL AND f.property = {q.bind(prop)} "
           "ORDER BY k.authority_rank NULLS LAST, c.field, c.value LIMIT 1")
    first = "ORDER BY p.rk NULLS LAST, t.h"
    return (f"LEFT JOIN LATERAL (SELECT "
            + ", ".join(f"(array_agg(p.{c} {first}))[1] AS {c}"
                        for c in ("v", "n", "cur", "u", "fp", "rk"))
            + f", (array_agg(t.h {first}))[1] AS h, bool_and(COALESCE(p.ok, true)) AS ok, "
            "count(DISTINCT json_build_array(p.v, p.n, p.cur, p.u)::text) AS nv "
            f"FROM unnest({lk}.tvers, {lk}.ths) t(ver, h) LEFT JOIN LATERAL ({one}) p "
            f"ON true) {alias} ON true")


def _compile(plan: Mapping[str, Any], scope: Any, docs: Mapping[str, list[str]],
             rules: Any = None) -> tuple[str, dict[str, Any]]:
    """The one statement: per visible record of the type, each field's value read per
    record (the filters' fields first, so the later reads run only on the records the
    filters keep), the records kept that match or whose match a withheld value hides
    (``kept``), then the page of groups or rows and the counts beside it."""
    q = _Sql()
    fields: dict[str, _Field] = plan["fields"]
    # the filters' fields first: Postgres applies a filter as soon as its field is read
    order = list(dict.fromkeys([f.name for f, _, _ in plan["where"]] + list(fields)))
    joins: list[str] = []
    picked: dict[tuple[str | None, str], str] = {}
    linked: dict[str, str] = {}
    cols: dict[str, dict[str, str]] = {}
    for name in order:
        f = fields[name]
        if f.link is not None and f.link not in linked:
            linked[f.link] = f"lk{len(linked)}"
            joins.append(_link(linked[f.link], f.link, q, scope, docs, plan.get("leads", False)))
        if not f.pseudo and (f.link, f.prop) not in picked:
            alias = picked[(f.link, f.prop)] = f"x{len(picked)}"
            joins.append(_pick(alias, "v.ver", f.prop, q, scope) if f.link is None else
                         _pick_linked(alias, linked[f.link], f.prop, q, scope))
        if f.link is None:
            if f.pseudo:
                expr = {"system": "split_part(o.binding_id, '.', 1)",
                        "binding": "o.binding_id", "object_id": "o.object_id"}[f.prop]
                cols[name] = {"v": expr, "n": "NULL::numeric", "h": "v.l1_handle",
                              "fp": "NULL::text", "cur": "NULL::text", "u": "NULL::text",
                              "rk": "NULL::integer", "ok": "true", "amb": "false",
                              "ln": "NULL::bigint"}
            else:
                x = picked[(None, f.prop)]
                cols[name] = {"v": f"{x}.v", "n": f"{x}.n", "h": "v.l1_handle",
                              "fp": f"{x}.fp", "cur": f"{x}.cur", "u": f"{x}.u",
                              "rk": f"{x}.rk", "ok": f"COALESCE({x}.ok, true)", "amb": "false",
                              "ln": "NULL::bigint"}
            continue
        # a link field: one value when the link has one target, or when its targets (one
        # party's copies in several systems) give one value (a binding, the property);
        # an object id only from one target. ``ln``: how many targets (existence tests)
        lk = linked[f.link]
        okl = f"COALESCE({lk}.hidden, 0) = 0"
        if f.pseudo:
            single = f"COALESCE({lk}.n, 0) <= 1" + (
                "" if f.prop == "object_id" else f" OR COALESCE({lk}.nb, 0) <= 1")
            expr = {"system": f"split_part({lk}.tb, '.', 1)", "binding": f"{lk}.tb",
                    "object_id": f"{lk}.t"}[f.prop]
            cols[name] = {"v": f"CASE WHEN {single} THEN {expr} END", "n": "NULL::numeric",
                          "h": f"CASE WHEN {single} THEN {lk}.th END", "fp": "NULL::text",
                          "cur": "NULL::text", "u": "NULL::text", "rk": "NULL::integer",
                          "ok": okl,
                          "amb": f"NOT ({single})", "ln": f"COALESCE({lk}.n, 0)"}
        else:
            x = picked[(f.link, f.prop)]
            single = f"COALESCE({lk}.n, 0) <= 1 OR COALESCE({x}.nv, 0) <= 1"
            cols[name] = {k: f"CASE WHEN {single} THEN {x}.{k} END"
                          for k in ("v", "n", "fp", "cur", "u", "rk")}
            cols[name].update(h=f"CASE WHEN {single} THEN {x}.h END",
                              ok=f"({okl} AND COALESCE({x}.ok, true))",
                              amb=f"NOT ({single})", ln=f"COALESCE({lk}.n, 0)")
    index = {name: k for k, name in enumerate(fields)}

    def raw(f: _Field, part: str) -> str:
        return cols[f.name][part]

    def col(f: _Field, part: str) -> str:
        return f"r.f{index[f.name]}_{part}"

    select = ["o.object_id AS _oid", "v.l1_handle AS _h"]
    for name, k in index.items():
        c = cols[name]
        select += [f"{c['v']} AS f{k}_v", f"{c['n']} AS f{k}_n", f"{c['h']} AS f{k}_h",
                   f"{c['fp']} AS f{k}_fp", f"{c['cur']} AS f{k}_cur", f"{c['u']} AS f{k}_u",
                   f"{c['rk']} AS f{k}_rk",
                   f"{c['ok']} AS f{k}_ok", f"{c['amb']} AS f{k}_amb", f"{c['ln']} AS f{k}_ln"]
    oks = " AND ".join(f"({c['ok']})" for c in cols.values()) or "true"
    item_cols, item_where = "", ""
    if rules is not None:
        # the mail rules (``ontology_tools_table_items``): a message the gate serves to
        # nobody is not there; one not decided, or naming Freightcom, is withheld
        joins.append(rules.join(q))
        item_cols = rules.columns
        item_where = " AND NOT " + rules.deny_sql(q)
    exists_only = {f.name for f, op, _ in plan["where"] if _exists_test(f, op)}
    valued = ({f.name for f, _ in plan["groups"]} | {f.name for _, f in plan["measures"] if f}
              | {f.name for f in plan["select"]}
              | {f.name for f, op, _ in plan["where"] if not _exists_test(f, op)})
    ambs = " OR ".join(f"({cols[n]['amb']})" for n in cols
                       if n in valued or n not in exists_only) or "false"
    # a record joined to a linked record only by a lead link (``leads``)
    lead = " OR ".join(f"{lk}.lead" for lk in linked.values()) or "false"
    # kept: every record that matches, or whose match a value it may not see hides
    maybe = [f"(({_condition(f, op, value, raw, q)}) OR NOT ({raw(f, 'ok')}))"
             for f, op, value in plan["where"]]
    matched = " AND ".join(_condition(f, op, value, col, q)
                           for f, op, value in plan["where"]) or "true"
    drive = _driver_sql(plan, q)
    source = ("objects o JOIN object_versions v ON v.object_id = o.object_id AND "
              "v.superseded_at IS NULL" if drive is None else
              "drv JOIN object_versions v ON v.ver = drv.ver AND v.superseded_at IS NULL "
              "JOIN objects o ON o.object_id = v.object_id")
    kept = ((f"drv AS MATERIALIZED ({drive}), " if drive is not None else "")
            + "kept0 AS MATERIALIZED (SELECT " + ", ".join(select)
            + f", ({oks}) AS _fok, ({ambs}) AS _amb, ({lead}) AS _lead{item_cols} "
            f"FROM {source} {' '.join(joins)} "
            f"WHERE o.object_type = {q.bind(plan['type'])} AND "
            + _visible("o", "v", q, scope, docs, rules)
            + (" AND " + " AND ".join(maybe) if maybe else "") + item_where + "), "
            "kept AS (SELECT r.*, (r._fok"
            + (f" AND {rules.ok_sql(q)}" if rules is not None else "")
            + ") AS _ok FROM kept0 r)")
    if plan["select"] and not plan["measures"]:
        body, total = _list_sql(plan, col, matched, q)
    else:
        body, total = _group_sql(plan, col, matched, q,
                                 _twins_sql(plan, q, scope, docs) if plan["twins"] else None,
                                 _held("xo", q, scope))
    # one row: the page (in its order, numbers as JSON text the caller reads as Decimal)
    # and the counts the answer states beside it
    sql = ("WITH " + kept + f", out AS ({body}) "
           "SELECT json_build_object('rows', COALESCE((SELECT json_agg(o ORDER BY o._ord) "
           "FROM out o), '[]'::json), "
           "'withheld', (SELECT count(*) FROM kept r WHERE NOT r._ok), "
           f"'ambiguous', (SELECT count(*) FROM kept r WHERE r._ok AND r._amb AND ({matched})), "
           f"'total', ({total})"
           + (f", 'leads', (SELECT count(*) FROM kept r WHERE r._ok AND r._lead AND ({matched}))"
              if plan.get("leads") else _lead_note(linked, q))
           + (rules.extras_sql(matched, q) if rules is not None else "") + ")::text")
    return sql, q.params


# a date cell the ``value_cells_dates`` index holds (its predicate, written as the index's)
_DATED_CELL = "c.value ~ '^[12][0-9]{3}-[01][0-9]-[0-3][0-9]'"
_DRIVER_OPS = ("between", "=", ">=", ">", "<=", "<")


def _driver(plan: Mapping[str, Any]) -> tuple[_Field, str | None, str | None] | None:
    """(field, low, high) of the first filter on the record's own date that bounds it:
    the records holding a cell of it with ``low <= value < high`` (text order: the
    database orders text by code point, ``C.UTF-8``) are exactly those whose date
    ``_condition`` may match, so the statement starts from them through the date index
    instead of reading every record of the type."""
    def succ(text: str) -> str | None:
        return text[:-1] + chr(ord(text[-1]) + 1) if text else None
    for f, op, value in plan["where"]:
        if f.link is not None or f.pseudo or f.value_type not in DATED or op not in _DRIVER_OPS:
            continue
        if op == "between":
            low, high = str(value[0]), succ(str(value[1]))
        else:
            text = str(value)
            low, high = {"=": (text, succ(text)), ">=": (text, None), ">": (succ(text), None),
                         "<=": (None, succ(text)), "<": (None, text)}[op]
        if f.value_type == "timestamp":
            low, high = _widen(low, -1), _widen(high, 1)
        if (low is None and high is None) or (low == "" or high == ""):
            continue
        return f, low, high
    return None


def _driver_sql(plan: Mapping[str, Any], q: _Sql) -> str | None:
    """The versions ``_driver`` starts from: a cell of the date in range, or any cell of
    it in a kind whose markings the reader does not hold (``unheld_date_kinds``: such a
    record is kept, and withheld, whatever its hidden value)."""
    found = _driver(plan)
    if found is None:
        return None
    f, low, high = found
    fields = f"c.field IN (SELECT field FROM value_fields WHERE property = {q.bind(f.prop)})"
    bounds = "".join([f" AND c.value >= {q.bind(low)}" if low is not None else "",
                      f" AND c.value < {q.bind(high)}" if high is not None else ""])
    sql = ("SELECT s.ver FROM value_cells c JOIN value_sources s ON s.src = c.src WHERE "
           f"s.superseded_at IS NULL AND {fields} AND {_DATED_CELL}{bounds}")
    unheld = plan.get("unheld_date_kinds") or []
    if unheld:
        sql += (" UNION SELECT s.ver FROM value_cells c JOIN value_sources s ON s.src = c.src "
                f"WHERE s.superseded_at IS NULL AND {fields} AND c.kind = ANY("
                f"{q.bind(list(unheld))}::int[])")
    return f"SELECT DISTINCT ver FROM ({sql}) d"


def _exists_test(f: _Field, op: str) -> bool:
    """A null test on a link's own fields (object_id, system, binding): whether the
    record links to any target, whatever their number."""
    return f.link is not None and f.pseudo and op in ("is_null", "not_null")


def _lead_note(linked: Mapping[str, str], q: _Sql) -> str:
    """Without ``leads``: whether a link type read has any live lead-tier link at all
    (one index probe), so the answer says it followed none of them."""
    if not linked:
        return ""
    return (", 'lead_types', (SELECT COALESCE(json_agg(t.link_type ORDER BY t.link_type), "
            "'[]'::json) FROM unnest("
            f"{q.bind(sorted(linked))}::text[]) t(link_type) WHERE EXISTS (SELECT 1 FROM "
            "links l WHERE l.link_type = t.link_type AND l.tier = 'lead' AND "
            "l.superseded_at IS NULL))")


def _condition(f: _Field, op: str, value: Any, col: Any, q: _Sql) -> str:
    v, n = col(f, "v"), col(f, "n")
    if _exists_test(f, op):
        return f"{col(f, 'ln')} {'=' if op == 'is_null' else '>'} 0"
    if op == "is_null":
        return f"{v} IS NULL AND {n} IS NULL"
    if op == "not_null":
        return f"({v} IS NOT NULL OR {n} IS NOT NULL)"
    numeric = f.value_type in NUMERIC
    if op in ("contains", "starts_with"):
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%" if op == "contains" else f"{escaped}%"
        return f"{v} ILIKE {q.bind(pattern)}"
    if numeric:
        target = f"COALESCE({n}, CASE WHEN {v} ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN {v}::numeric END)"
        if op == "in":
            return f"{target} = ANY({q.bind(list(value))}::numeric[])"
        if op == "between":
            return (f"{target} BETWEEN {q.bind(value[0])}::numeric AND "
                    f"{q.bind(value[1])}::numeric")
        sql_op = "<>" if op == "!=" else op
        return f"{target} {sql_op} {q.bind(value)}::numeric"
    if f.value_type in DATED:
        if f.value_type == "timestamp":
            v = _local(v)

        def cut(p: str) -> str:
            return f"left({v}, length({p}))"
        if op == "in":
            return f"left({v}, 10) = ANY({q.bind([str(x) for x in value])}::text[])"
        if op == "between":
            low, high = q.bind(str(value[0])), q.bind(str(value[1]))
            return f"{cut(low)} >= {low} AND {cut(high)} <= {high}"
        p = q.bind(str(value))
        return f"{cut(p)} {'<>' if op == '!=' else op} {p}"
    if op == "in":
        return f"lower({v}) = ANY({q.bind([str(x).lower() for x in value])}::text[])"
    if op == "between":
        return f"{v} BETWEEN {q.bind(str(value[0]))} AND {q.bind(str(value[1]))}"
    if op in ("=", "!="):
        return f"lower({v}) {'=' if op == '=' else '<>'} lower({q.bind(str(value))})"
    return f"{v} {op} {q.bind(str(value))}"


# a timestamp stated with its zone (``Z`` or an offset), as the ISO parsers store them
_ZONED = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)$")
_ZONED_SQL = ("'^[0-9]{4}-[01][0-9]-[0-3][0-9][T ][0-2][0-9]:[0-5][0-9](:[0-5][0-9]"
              "([.][0-9]+)?)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'")


def _local(expr: str) -> str:
    """``expr`` (a timestamp's stored text) as its wall-clock time in ``BUSINESS_TZ``
    (``YYYY-MM-DDTHH:MM:SS``), so a date compared with it or a bucket of it is the
    company's calendar day: 2026-11-01T02:00Z is the evening of October 31. A value
    stated without a zone has no instant to convert and stays as stored."""
    return (f"CASE WHEN {expr} ~ {_ZONED_SQL} THEN to_char(({expr})::timestamptz AT TIME "
            f"ZONE '{BUSINESS_TZ}', 'YYYY-MM-DD\"T\"HH24:MI:SS') ELSE {expr} END")


def _local_bound(f: _Field, value: Any) -> Any:
    """A timestamp filter's bound stated with its zone, as the same wall-clock text
    ``_local`` compares it with; a date or a zone-less time is already the company's."""
    if f.value_type != "timestamp":
        return value
    if isinstance(value, list):
        return [_local_bound(f, v) for v in value]
    if not isinstance(value, str) or not _ZONED.match(value.strip()):
        return value
    from zoneinfo import ZoneInfo
    try:
        at = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _bad(f"{f.name}: {value[:40]!r} is not a time") from None
    return at.astimezone(ZoneInfo(BUSINESS_TZ)).strftime("%Y-%m-%dT%H:%M:%S")


def _widen(bound: str | None, days: int) -> str | None:
    """A driver bound on a stored timestamp moved ``days`` whole days out: a value's
    stored text and its ``BUSINESS_TZ`` wall clock differ by less than a day whatever
    zone it was stored in, so the widened range holds every record the exact filter may
    match. A bound that does not start with a date bounds nothing."""
    if bound is None:
        return None
    try:
        day = dt.date.fromisoformat(bound[:10])
    except ValueError:
        return None
    return (day + dt.timedelta(days=days)).isoformat()


def _bucket(expr: str, bucket: str | None) -> str:
    if bucket is None:
        return expr
    # a real calendar date only (2024-13-45, 2023-02-29: no key, the row in the group
    # with none), so no bucket's cast to a date can fail the whole query; the day is
    # checked against its month's length once the month is known to be valid
    day = f"left({expr}, 10)"
    dated = (f"CASE WHEN {expr} ~ '^[0-9]{{4}}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])' "
             f"AND left({expr}, 4) <> '0000' THEN CASE WHEN substr({expr}, 9, 2)::int <= extract(day FROM "
             f"(left({expr}, 7) || '-01')::date + interval '1 month' - interval '1 day') "
             f"THEN {day} END END")
    if bucket == "day":
        return dated
    if bucket == "month":
        return f"left({dated}, 7)"
    if bucket == "year":
        return f"left({dated}, 4)"
    if bucket == "quarter":
        return (f"left({dated}, 4) || '-Q' || ((substr({dated}, 6, 2)::int - 1) / 3 + 1)"
                "::text")
    return f"to_char(date_trunc('week', ({dated})::date), 'YYYY-MM-DD')"


def _dims(plan: Mapping[str, Any], col: Any) -> list[tuple[str, str]]:
    """(alias, expression): the currency of each money measure's field and the unit of
    each quantity measure's, grouped by beside the keys (never a total across them)."""
    out = []
    for i, (fn, f) in enumerate(plan["measures"]):
        if f is None or fn not in ("sum", "avg", "min", "max"):
            continue
        if f.value_type == "money":
            out.append((f"m{i}_cur", col(f, "cur")))
        elif f.value_type == "quantity":
            out.append((f"m{i}_u", col(f, "u")))
    return out


def _twins_sql(plan: Mapping[str, Any], q: _Sql, scope: Any, docs: Mapping[str, list[str]]
               ) -> str:
    """CTEs ``idl`` (the identity links between two records of the type this reader may
    read: live, at an identity tier, markings held, every evidence handle a business
    system's, no never-authority end), ``reach`` (every record each such record reaches over
    them, any number of hops) and ``cl`` (each linked record's cluster: the least object id
    it reaches), so one record held in several systems is one member."""
    never = q.bind(list(NEVER_AUTHORITY_LINEAGES))
    return (
        "idl AS MATERIALIZED (SELECT l.from_object AS a, l.to_object AS b FROM links l "
        "JOIN objects oa ON oa.object_id = l.from_object JOIN object_versions va ON "
        "va.object_id = oa.object_id AND va.superseded_at IS NULL "
        "JOIN objects ob ON ob.object_id = l.to_object JOIN object_versions vb ON "
        "vb.object_id = ob.object_id AND vb.superseded_at IS NULL "
        f"WHERE l.link_type = ANY({q.bind(list(plan['twins']))}::text[]) "
        f"AND l.superseded_at IS NULL AND l.tier = ANY({q.bind(list(IDENTITY_TIERS))}::text[]) "
        f"AND l.from_object <> l.to_object "
        f"AND l.markings = ANY({q.bind(list(scope.marking_sets))}::text[]) "
        "AND NOT EXISTS (SELECT 1 FROM json_array_elements_text(CASE WHEN "
        "l.evidence_handles IS NULL OR l.evidence_handles = '' THEN '[]'::json ELSE "
        "l.evidence_handles::json END) h(x) WHERE split_part(h.x, '://', 1) <> ALL("
        f"{q.bind(list(TABLE_SCHEMES))}::text[])) "
        f"AND oa.object_type = {q.bind(plan['type'])} AND ob.object_type = oa.object_type "
        f"AND NOT (COALESCE(va.lineage, '') = ANY({never}::text[])) "
        f"AND NOT (COALESCE(vb.lineage, '') = ANY({never}::text[])) AND "
        + _visible("oa", "va", q, scope, docs) + " AND " + _visible("ob", "vb", q, scope, docs)
        + "), reach(s, t) AS (SELECT a, b FROM idl UNION SELECT b, a FROM idl UNION "
        "SELECT r.s, CASE WHEN i.a = r.t THEN i.b ELSE i.a END FROM reach r JOIN idl i "
        "ON r.t IN (i.a, i.b)), "
        "cl AS (SELECT s AS oid, LEAST(s, min(t)) AS cid FROM reach GROUP BY s)")


def _group_sql(plan: Mapping[str, Any], col: Any, matched: str, q: _Sql,
               twins: str | None = None, held_other: str = "true") -> tuple[str, str]:
    """(the page's SELECT, the SQL counting every group). With ``twins`` (``_twins_sql``),
    the records of one cluster in one group are one member: its copy holding the first
    measured field's value from the most authoritative source, the others counted in
    ``_twins``."""
    keys, sel, exprs = [], [], {}
    for i, (f, bucket) in enumerate(plan["groups"]):
        expr = col(f, "v")
        if f.value_type in NUMERIC:
            expr = f"COALESCE({col(f, 'n')}::text, {col(f, 'v')})"
        elif f.value_type == "timestamp" and bucket is not None:
            expr = _local(expr)
        expr = _bucket(expr, bucket)
        keys.append(expr)
        exprs[f.name] = expr
        sel.append(f"{expr} AS g{i}")
        sel.append(f"(array_agg(json_build_array({col(f, 'h')}, {col(f, 'fp')}) ORDER BY "
                   f"r._h) FILTER (WHERE {col(f, 'fp')} IS NOT NULL))[1] AS g{i}_c")
    dims = _dims(plan, col)
    for alias, expr in dims:
        keys.append(expr)
        sel.append(f"{expr} AS {alias}")
    for i, (fn, f) in enumerate(plan["measures"]):
        if f is None:
            exprs[f"m{i}"] = "count(*)"
            sel.append(f"count(*) AS m{i}")
            continue
        number = f.value_type in NUMERIC
        val = col(f, "n") if number else col(f, "v")
        present = f"({col(f, 'n')} IS NOT NULL OR {col(f, 'v')} IS NOT NULL)"
        agg = {"count": f"count(*) FILTER (WHERE {present})",
               "count_distinct": f"count(DISTINCT {val})",
               "sum": f"sum({val})", "avg": f"avg({val})",
               "min": f"min({val})", "max": f"max({val})"}[fn]
        exprs[f"m{i}"] = agg
        # numbers leave as text: JSON numbers would reach Python as floats
        sel.append(f"({agg}){'::text' if number and fn not in ('count', 'count_distinct') else ''}"
                   f" AS m{i}")
        sel.append(f"count(*) FILTER (WHERE {present}) AS m{i}_in")
        if fn == "sum" and number:
            # inputs below zero (a credit): a sum over a source read in part is then no
            # lower bound of the source's total (``claim_checks_counts``: no floor)
            sel.append(f"count(*) FILTER (WHERE {val} < 0) AS m{i}_neg")
        sel.append(f"(array_agg(json_build_array({col(f, 'h')}, {col(f, 'fp')}) ORDER BY "
                   f"r._h) FILTER (WHERE {present} AND {col(f, 'fp')} IS NOT NULL))"
                   f"[1:{MAX_CITED}] AS m{i}_c")
    sel.append("count(*) AS _members")
    sel.append("count(*) FILTER (WHERE r._lead) AS _lead")
    sel.append(f"(array_agg(r._h ORDER BY r._h))[1:{MAX_CITED}] AS _cited")
    order = [f"{exprs[by]} {direction.upper()} NULLS LAST"
             for by, direction in plan["order"] or [("m0", "desc")]]
    order += [f"{k} ASC NULLS LAST" for k in keys]
    sel.append(f"row_number() OVER (ORDER BY {', '.join(order)}) AS _ord")
    where = f"FROM kept r WHERE r._ok AND ({matched})"
    group = (" GROUP BY " + ", ".join(keys)) if keys else ""
    total = (f"SELECT count(*) FROM (SELECT 1 {where}{group}) t" if keys else "1")
    page = f"{group} ORDER BY {', '.join(order)} LIMIT {q.bind(plan['limit'])}"
    if twins is None:
        return f"SELECT {', '.join(sel)} {where}{page}", total
    sel.append("COALESCE(sum(r._n - 1), 0) AS _twins")
    sel.append("count(*) FILTER (WHERE r._unres) AS _unres")
    part = ", ".join([*keys, "r._cid"])
    cid = "COALESCE(cl.cid, r._oid)"
    pick = ["r._h ASC"]
    first = next((f for _, f in plan["measures"] if f is not None), None)
    if first is not None:
        pick[:0] = [f"({col(first, 'n')} IS NOT NULL OR {col(first, 'v')} IS NOT NULL) DESC",
                    f"{col(first, 'rk')} ASC NULLS LAST"]
    over = ", ".join([*keys, "r._cid"])
    # a member with an identity link the clusters do not walk (a copy this reader may
    # not read, a link not every evidence handle of a business system backs, or a copy
    # its source no longer holds that links on to another record): its copies may be
    # counted apart (a chain broken in the middle), so the member's group is no exact
    # count (``_unres``: the answer partial). A removed copy linked to this member alone
    # is no record now and splits nothing.
    types, tiers = q.bind(list(plan["twins"])), q.bind(list(IDENTITY_TIERS))
    live = (f"link_type = ANY({types}::text[]) AND {{0}}.superseded_at IS NULL AND "
            f"{{0}}.tier = ANY({tiers}::text[]) AND {{0}}.from_object <> {{0}}.to_object")
    unwalked = (
        "EXISTS (SELECT 1 FROM links ul JOIN objects xo ON xo.object_id = CASE WHEN "
        "ul.from_object = r._oid THEN ul.to_object ELSE ul.from_object END WHERE ul."
        + live.format("ul") + " AND (ul.from_object = r._oid OR ul.to_object = r._oid) "
        "AND NOT EXISTS (SELECT 1 FROM idl WHERE idl.a = ul.from_object AND "
        f"idl.b = ul.to_object) AND (({held_other}) OR EXISTS (SELECT 1 FROM links ux "
        "WHERE ux." + live.format("ux") + " AND xo.object_id IN (ux.from_object, "
        "ux.to_object) AND r._oid NOT IN (ux.from_object, ux.to_object))))")
    return (f"WITH RECURSIVE {twins}, "
            f"m0 AS (SELECT r.*, {cid} AS _cid, {unwalked} AS _u "
            f"FROM kept r LEFT JOIN cl ON cl.oid = r._oid WHERE r._ok AND ({matched})), "
            f"m AS (SELECT r.*, count(*) OVER (PARTITION BY {over}) AS _n, "
            f"bool_or(r._u) OVER (PARTITION BY {over}) AS _unres FROM m0 r), "
            f"one AS (SELECT DISTINCT ON ({part}) r.* FROM m r "
            f"ORDER BY {part}, {', '.join(pick)}) "
            f"SELECT {', '.join(sel)} FROM one r{page}", total)


def _list_sql(plan: Mapping[str, Any], col: Any, matched: str, q: _Sql
              ) -> tuple[str, str]:
    """(the page's SELECT, the SQL counting every matching record)."""
    sel = ["r._oid", "r._h", "r._lead"]
    for i, f in enumerate(plan["select"]):
        for part in ("v", "n", "h", "fp", "cur", "u"):
            sel.append(f"{col(f, part)}{'::text' if part == 'n' else ''} AS s{i}_{part}")
    names = [f.name for f in plan["select"]]
    order = []
    for by, direction in plan["order"]:
        f = plan["select"][names.index(by)]
        expr = col(f, "n") if f.value_type in NUMERIC else col(f, "v")
        order.append(f"{expr} {direction.upper()} NULLS LAST")
    order.append("r._h ASC")
    sel.append(f"row_number() OVER (ORDER BY {', '.join(order)}) AS _ord")
    where = f"FROM kept r WHERE r._ok AND ({matched})"
    return (f"SELECT {', '.join(sel)} {where} ORDER BY {', '.join(order)} "
            f"LIMIT {q.bind(plan['limit'])}", f"SELECT count(*) {where}")


def register(table: dict[str, dict[str, Any]], base: Any) -> TableTool:
    """Serve ``plx.table`` in a host tool table (``ontology_host.tool_table``)."""
    tool = TableTool(base)
    if TOOL_NAME in table:
        raise ValueError(f"tool already registered: {TOOL_NAME}")
    table[TOOL_NAME] = dict(TOOL, handler=lambda arguments, actor: tool.call(
        TOOL_NAME, arguments, actor))
    return tool


__all__ = ["TOOL", "TOOL_NAME", "TOOL_NAMES", "TableTool", "register"]
