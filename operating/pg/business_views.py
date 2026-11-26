"""The curated business views (schema ``biz``): one row per real-world thing, built in
Python over the served ontology and read by ``plx.business``.

Each view is a plain table ``biz.<view>`` rebuilt whole: the rows are computed here (the
ontology's own Python rules decide statuses, values and lateness), copied into
``biz.<view>_next`` and swapped in with one transaction (live renamed away, _next renamed
live, the old one dropped), so a reader sees the old table or the new one, never a part.

What every build holds to:
- Only records their source still holds: each type's records are listed with the store's
  own removal predicate (``ontology.store.removed_ids_sql``, the one
  ``proactive_engine._Store`` reads through: removed, source-deleted or missing from the
  source), PLX's own documents held as supplier documents left out
  (``own_issued.hidden_ids``); a link counts only when both of its ends are held.
- One row per real thing: records of one type joined by an identity link
  (``identity_link_type``) at an identity tier, every evidence handle a business system's,
  neither end of a never-authority lineage, are one cluster; its representative copy is
  the most authoritative system's (BC, Portal app, Sage, FileMaker); each column is the
  representative's value, else the first copy (in that order) holding one.
- Money is never mixed: every money column has its currency column beside it.
- Door v2 (ARCH-FINAL section 14): a view table holds its business columns, ``handle``, its
  keys (``customer_handle``, ...) and three permission columns decided once here:
  ``access_class`` (the id in ``<schema>.access_classes`` of the row's audience keys, every
  copy's and every linked record's, and record markings, of every version a value was read
  from and every link followed; ids are append-only, shared by every view),
  ``col_class`` (the id in ``<schema>.col_classes`` of the row's per-column markings and
  needs: what a column read from a record the row is not, a mailed promise date, requires;
  ``business_views_access`` states the classes contract) and ``ai_ok`` (always true now: the owner dropped the Freightcom rule 11-23; a text column or
  the handle, ``image_ocr_gpu.FREIGHTCOM``: no Freightcom row reaches a
  model). Lineage (object ids, per-column cites [handle, field_path], systems, copies) is
  in ``<schema>.lineage`` by (view, handle). A row made from an object of
  ``ontology.people_excluded_objects`` (an excluded person's own data): the owner's alone.

Build: ``python3 -m operating.pg.business_views build [--view NAME]...`` (niced; read-only
reads, one write transaction per view); ``refresh`` rebuilds only the views whose input
types the change feed shows moved (else the store ``stamp``) or whose day changed (``plx-business-views.timer`` after the deltas); ``status``
prints biz.state."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dc_field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from operating.pg import party_keys

SERVED_SCHEMA = "biz"   # migration 0041's schema, the one Ask reads when nothing else is set


def _schema_from_env() -> str:
    """``PLX_BIZ_SCHEMA`` names another schema to build into and read from (a scratch copy
    for a benchmark beside the served one); unset = ``biz``. A name that is not a plain
    lower-case identifier fails closed (it is spliced into SQL)."""
    name = os.environ.get("PLX_BIZ_SCHEMA", "").strip() or SERVED_SCHEMA
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) or name in ("public", "pg_catalog",
                                                                     "information_schema"):
        raise ValueError(f"PLX_BIZ_SCHEMA must be a plain lower-case schema name, not {name!r}")
    return name


SCHEMA = _schema_from_env()
IDENTITY_TIERS = ("verified", "reviewed")
SOLID_TIERS = ("verified", "reviewed", "corroborated")
BUSINESS_SCHEMES = ("api", "bc", "mc")
NEVER_AUTHORITY_LINEAGES = ("gold_native_staging",)
BUSINESS_TZ = "America/New_York"
# the common columns every view carries after its own
COMMON = (("handle", "text"), ("access_class", "integer"), ("col_class", "integer"),
          ("ai_ok", "boolean"))
LINEAGE_COLS = ("view", "handle", "object_ids", "read_ids", "col_objects", "cites", "systems",
                "copies")
# indexed columns besides handle, the keys (*_handle) and the dates: what pages filter on
STATUS_COLS = ("status", "order_status", "po_status", "verdict", "aging_bucket")
NAME_COLS = ("name", "customer", "supplier", "item_name", "customer_chain", "chain")
# numbers and codes, indexed bytewise (``text_pattern_ops``): an equality or a prefix range
# on one reaches the index through the guarded views (leakproof operators), as the search
# box's number lookups do (``page_reads._find_sql``)
NUMBER_COLS = ("order_number", "customer_po", "po_number", "invoice_number",
               "document_number", "batch_code", "lot_number", "item_code")
CLASS_MAX = 2**31 - 1   # integer ids (the canonical classes contract, migration 0041)
EXCLUDED_TABLE = "ontology.people_excluded_objects"
STREAM_BATCH = 20_000   # rows per server-cursor batch of a whole-type read
UNRESOLVED = "*unresolved*"   # a read whose record is not known: never readable
SQL_TYPES = {"text": "text", "money": "numeric", "numeric": "numeric", "date": "date",
             "integer": "integer", "boolean": "boolean", "currency": "text",
             "timestamp": "timestamptz", "list": "text[]"}
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
NATIONAL = "Customer.national_account"
NATIONAL_TYPE = "RelSageArNat"
NATIONAL_TIERS = (*SOLID_TIERS, "stored_by_text_join")   # Sage's own key, stored
BILL_TO = "Customer.billed_to"
CHAIN_COL = ("customer_chain", "text", "the customer's chain or parent account (its Sage "
             "national account, else the customer it is billed to, else the chain its "
             "store-account name states): a chain's store accounts share it")


# A customer or supplier is held as several copies (Sage, Portal, FileMaker, each BC company);
# settled identity links join few of them. A party type also merges by a declared, general
# rule (never a name list): a stored-key link (stored_by_text_join, corroborated); a lead
# identity link whose two records state the same code and the same name; and two parties of
# the same name whose lead links reach only each other (the name names one party both ways).
# Two system-of-record accounts (two Sage customers) are never merged by these rules.
PARTY_KEY_TIERS = ("stored_by_text_join", "corroborated")
LEAD_TIER = "lead"


@dataclass(frozen=True)
class PartyMerge:
    code_props: tuple[str, ...]
    name_prop: str = "name"
    record_system: str = "sage"


PARTY_MERGE = {"Customer": PartyMerge(("erp_code", "code")),
               "Supplier": PartyMerge(("code",))}
MERGE_RULES_DOC = ("which rules joined its copies: identity_link (a settled identity link), "
                   "stored_key (a stored-key identity link), same_code_and_name (a lead "
                   "identity link between copies stating the same code and name), same_name "
                   "(a lead identity link between two parties of the same name whose lead "
                   "links reach only each other); empty: one copy. Two Sage accounts never "
                   "merge by these rules")


def _norm_name(raw: Any) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(raw or "").casefold()).split())


def _first_text(rec: "Rec", props: Sequence[str]) -> str:
    for prop in props:
        cell = rec.cell(prop)
        text = _text(cell.value) if cell is not None else None
        if text:
            return text.upper()
    return ""


def party_merge(spec: PartyMerge, recs: Mapping[str, "Rec"],
                held: Mapping[str, list[tuple[str, str, str]]],
                union: Callable[[str, str, bool], bool], find: Callable[[str], str],
                link_marks: dict[str, set[str]], rule_of: dict[str, set[str]]) -> None:
    """Merge a party type's copies by ``PARTY_MERGE``'s rule (see above) into ``union``."""
    for tier in PARTY_KEY_TIERS:
        for a, b, marks in held.get(tier, ()):
            if union(a, b, True):
                link_marks[a].add(marks)
                rule_of[a].add("stored_key")
    name = {oid: _norm_name(r.cell(spec.name_prop).value if r.cell(spec.name_prop) else None)
            for oid, r in recs.items()}
    code = {oid: _first_text(r, spec.code_props) for oid, r in recs.items()}
    named: list[tuple[str, str, str]] = []
    for a, b, marks in sorted(held.get(LEAD_TIER, ())):
        if not name[a] or name[a] != name[b]:
            continue
        if code[a] and code[a] == code[b]:
            if union(a, b, True):
                link_marks[a].add(marks)
                rule_of[a].add("same_code_and_name")
        else:
            named.append((a, b, marks))
    # same name, different (or no) code: only when each side's same-name lead links reach
    # the other side alone (a party lead-linked to two same-named parties joins neither)
    reach: dict[str, set[str]] = defaultdict(set)
    for a, b, _m in named:
        ra, rb = find(a), find(b)
        if ra != rb:
            reach[ra].add(rb)
            reach[rb].add(ra)
    for a, b, marks in named:
        ra, rb = find(a), find(b)
        if ra == rb or reach.get(ra) != {rb} or reach.get(rb) != {ra}:
            continue
        if union(a, b, True):
            link_marks[a].add(marks)
            rule_of[a].add("same_name")


def handle_col(party: str, how: str) -> tuple[str, str, str]:
    """The ``<party>_handle`` column: the key of the party's own view row."""
    return (f"{party}_handle", "text",
            f"the {party}s row it belongs to (join {party}s on {party}s.handle): the one "
            f"real {party} reached through {how}; empty when there is no such link or the "
            f"links reach two different {party}s")


# --------------------------------------------------------------------------- records

@dataclass(slots=True)
class Cell:
    value: Any
    num: Any
    currency: str | None
    field_path: str
    rank: int
    marks: str
    handle: str
    unit: str | None = None
    tax_basis: str | None = None
    lineage: str | None = None

    def row(self, rec: "Rec", prop: str) -> dict[str, Any]:
        """The cell as the ontology's value rows state it (``order_value`` reads these)."""
        return {"value": self.value, "currency": self.currency, "unit": self.unit,
                "tax_basis": self.tax_basis, "lineage": self.lineage, "property": prop,
                "object_id": rec.object_id, "l1_handle": self.handle,
                "field_path": self.field_path, "markings": self.marks}


@dataclass(slots=True)
class Rec:
    object_id: str
    object_type: str
    binding: str
    audience_key: str
    handles: dict[str, str] = dc_field(default_factory=dict)   # handle -> version markings
    lineages: set[str] = dc_field(default_factory=set)
    cells: dict[str, list[Cell]] = dc_field(default_factory=dict)

    def cell(self, prop: str) -> Cell | None:
        got = self.cells.get(prop)
        return got[0] if got else None

    def rows(self, props: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Every held cell of ``props`` as value rows, by property."""
        return {p: [c.row(self, p) for c in self.cells[p]] for p in props if p in self.cells}


def binding_rank(binding: str) -> int:
    """Which copy of a thing speaks for it: BC, the Portal app, Sage, FileMaker, others."""
    b = binding or ""
    if b.startswith("bc."):
        return 0
    if b.startswith("portal.app_"):
        return 1
    if "sage" in b:
        return 2
    if "fm_" in b or b.startswith("portal.fm"):
        return 3
    return 4


def system_of(binding: str) -> str:
    from operating.order_value import record_system   # the one system rule
    return record_system(binding)


def _marks_size(marks: str) -> int:
    try:
        return len(json.loads(marks or "[]"))
    except ValueError:
        return 99


def _marks(prefix: str, values: Sequence[str]) -> str:
    """Named placeholders for a short list (a JSON-list parameter would make the read run
    whole client side instead of streaming from a server cursor)."""
    return ", ".join(f":{prefix}{i}" for i in range(len(values))) or "NULL"


def _binds(prefix: str, values: Sequence[str]) -> dict[str, str]:
    return {f"{prefix}{i}": v for i, v in enumerate(values)}


class Reader:
    """Read-only reads of the served ontology for a build, over a ``PgCompatConnection``
    (the store's own SQL dialect: the removal predicate is the store's, as written)."""

    def __init__(self, conn: Any, schema: Any) -> None:
        self.conn = conn
        self.schema = schema
        self._removed: dict[str, frozenset[str]] = {}
        self._hidden: frozenset[str] | None = None
        self._memo: dict[str, Any] = {}

    def memo(self, key: str, make: Callable[[], Any]) -> Any:
        """What two views of one build share (read once per build)."""
        if key not in self._memo:
            self._memo[key] = make()
        return self._memo[key]

    def removed(self, object_type: str) -> frozenset[str]:
        if object_type not in self._removed:
            from ontology import store as ostore
            sql = ostore.removed_ids_sql(object_type_sql=":t")
            self._removed[object_type] = frozenset(
                row[0] for row in self.conn.execute(sql, {"t": object_type}))
        return self._removed[object_type]

    def hidden(self) -> frozenset[str]:
        if self._hidden is None:
            try:
                from operating.pg import own_issued
                self._hidden = frozenset(own_issued.hidden_ids())
            except Exception:   # noqa: BLE001 - no set built: nothing to hide by it
                self._hidden = frozenset()
        return self._hidden

    def records(self, object_type: str, props: Sequence[str]) -> dict[str, Rec]:
        """The held records of a type with their current cells of ``props`` (each record's
        cells per property, authority first; of two versions holding one, the less marked
        version's cell first)."""
        gone = self.removed(object_type) | self.hidden()
        out: dict[str, Rec] = {}
        # one copy of each repeated text (a handle on each of its cells, field paths,
        # markings, lineages, units): the largest type is ~2.2M cells
        pool: dict[Any, Any] = {}
        same = pool.setdefault
        rows = self._rows(
            "SELECT o.object_id, o.binding_id, o.audience_key, v.l1_handle, v.markings AS vm, "
            "COALESCE(s.lineage, v.lineage) AS lineage, f.property, f.field_path, c.value, "
            "c.value_num, c.currency, k.authority_rank, k.markings AS km, k.unit, k.tax_basis "
            "FROM objects o JOIN object_versions v ON v.object_id = o.object_id "
            "AND v.superseded_at IS NULL "
            "JOIN value_sources s ON s.ver = v.ver AND s.superseded_at IS NULL "
            # per source its own cells by the (src, field) key: a plain join let the
            # planner hash-join a whole-table scan of value_cells (30 GB served; 14+ min
            # for 122k Sage AR documents on a loaded box, 23 s this way)
            "CROSS JOIN LATERAL (SELECT c.value, c.value_num, c.currency, c.field, c.kind "
            "FROM value_cells c WHERE c.src = s.src OFFSET 0) c "
            "JOIN value_fields f ON f.field = c.field "
            "JOIN value_kinds k ON k.kind = c.kind "
            f"WHERE o.object_type = :t AND f.property IN ({_marks('p', props)})",
            {"t": object_type, **_binds("p", props)})
        for row in rows:
            oid = row["object_id"]
            if oid in gone:
                continue
            handle = row["l1_handle"]
            if handle is None or handle.split("://", 1)[0] not in BUSINESS_SCHEMES:
                continue
            rec = out.get(oid)
            if rec is None:
                rec = out[oid] = Rec(oid, object_type, row["binding_id"] or "",
                                     row["audience_key"] or "")
            handle = same(handle, handle)
            rec.handles[handle] = same(row["vm"] or "[]", row["vm"] or "[]")
            lineage = row["lineage"]
            if lineage:
                lineage = same(lineage, lineage)
                rec.lineages.add(lineage)
            rec.cells.setdefault(same(row["property"], row["property"]), []).append(Cell(
                row["value"], row["value_num"], same(row["currency"], row["currency"]),
                same(row["field_path"] or "", row["field_path"] or ""),
                row["authority_rank"] if row["authority_rank"] is not None else 1 << 30,
                same(row["km"] or "[]", row["km"] or "[]"), handle,
                same(row["unit"], row["unit"]), same(row["tax_basis"], row["tax_basis"]),
                lineage))
        for rec in out.values():
            for cells in rec.cells.values():
                cells.sort(key=lambda c: (c.rank, _marks_size(rec.handles.get(c.handle, "")),
                                          c.field_path, c.handle, str(c.value)))
        return out

    def _rows(self, sql: str, params: Any) -> Iterable[Any]:
        """A whole-type read taken from a server-side cursor a batch at a time (bounded
        memory: never the driver's full row list beside the records made from it); a
        connection without ``stream`` (SQLite in tests) runs it whole."""
        stream = getattr(self.conn, "stream", None)
        if stream is None:
            return self.conn.execute(sql, params)
        return stream(sql, params, batch=STREAM_BATCH, read_all=True)

    def ids(self, object_type: str) -> dict[str, str]:
        """The held records of a type (no values read): id -> audience key."""
        gone = self.removed(object_type) | self.hidden()
        return {row[0]: row[1] or "" for row in self.conn.execute(
            "SELECT object_id, audience_key FROM objects WHERE object_type = :t",
            {"t": object_type}) if row[0] not in gone}

    def link_rows(self, link_types: Sequence[str], tiers: Sequence[str] | None
                  ) -> list[tuple[str, str, str, str, str]]:
        """(from, to, link markings, link type, tier) of the live links of these types at
        these tiers (None: any tier) whose evidence is all business-system handles."""
        if not link_types:
            return []
        rows = self._rows(
            "SELECT l.from_object, l.to_object, l.markings, l.link_type, l.evidence_handles, "
            f"l.tier FROM links l WHERE l.link_type IN ({_marks('lt', link_types)}) "
            "AND l.superseded_at IS NULL "
            + ("" if tiers is None else f"AND l.tier IN ({_marks('ti', tiers)}) ")
            + "AND l.from_object <> l.to_object",
            {**_binds("lt", link_types), **_binds("ti", tiers or ())})
        out = []
        for row in rows:
            try:
                ev = json.loads(row["evidence_handles"] or "[]")
            except ValueError:
                continue
            if any(not isinstance(h, str) or h.split("://", 1)[0] not in BUSINESS_SCHEMES
                   for h in ev or ()):
                continue
            out.append((row["from_object"], row["to_object"], row["markings"] or "[]",
                        row["link_type"], row["tier"]))
        return out

    def identity_types(self, object_type: str) -> list[str]:
        from operating.ontology_tools_identity import identity_link_type
        return sorted(lid for lid, link in self.schema.link_types.items()
                      if link.get("from") == object_type and link.get("to") == object_type
                      and identity_link_type(link))

    def clusters(self, object_type: str, recs: Mapping[str, Rec]) -> list["Thing"]:
        """The records as real things: identity-linked held records are one. A party type
        (``PARTY_MERGE``) also merges by its declared rule (``party_merge``)."""
        parent: dict[str, str] = {oid: oid for oid in recs}
        link_marks: dict[str, set[str]] = defaultdict(set)
        rule_of: dict[str, set[str]] = defaultdict(set)
        spec = PARTY_MERGE.get(object_type)
        accounts: dict[str, set[str]] = {
            oid: ({oid} if spec and system_of(r.binding) == spec.record_system else set())
            for oid, r in recs.items()}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str, guarded: bool) -> bool:
            ra, rb = find(a), find(b)
            if ra == rb:
                return True
            if guarded and accounts[ra] and accounts[rb]:
                return False   # two system-of-record accounts: two parties
            parent[ra] = rb
            accounts[rb] |= accounts.pop(ra)
            return True
        never = set(NEVER_AUTHORITY_LINEAGES)
        tiers = IDENTITY_TIERS + (PARTY_KEY_TIERS + (LEAD_TIER,) if spec else ())
        held: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for a, b, marks, _, tier in self.link_rows(self.identity_types(object_type), tiers):
            if a not in recs or b not in recs:
                continue   # an end removed, missing or of another type: no twin
            if recs[a].lineages & never or recs[b].lineages & never:
                continue
            held[tier].append((a, b, marks))
        for tier in IDENTITY_TIERS:
            for a, b, marks in held[tier]:
                union(a, b, False)
                link_marks[a].add(marks)
                rule_of[a].add("identity_link")
        if spec is not None:
            party_merge(spec, recs, held, union, find, link_marks, rule_of)
        groups: dict[str, list[Rec]] = defaultdict(list)
        for oid in sorted(recs):
            groups[find(oid)].append(recs[oid])
        things = []
        for members in groups.values():
            members.sort(key=lambda r: (binding_rank(r.binding), r.object_id))
            marks = set().union(*(link_marks.get(r.object_id, set()) for r in members))
            rules = set().union(*(rule_of.get(r.object_id, set()) for r in members))
            things.append(Thing(members, marks, rules))
        things.sort(key=lambda t: t.rep.object_id)
        return things

    def linked(self, link_type: str, ends: Mapping[str, Any], targets: Mapping[str, Any],
               tiers: Sequence[str] = SOLID_TIERS) -> dict[str, list[tuple[str, str]]]:
        """from id -> [(to id, link markings)] of a link type at a solid tier (``tiers``),
        both ends held (in ``ends`` and ``targets``)."""
        out: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for a, b, marks, _, _t in self.link_rows([link_type], tiers):
            if a in ends and b in targets:
                out[a].append((b, marks))
        for v in out.values():
            v.sort()
        return out


@dataclass
class Thing:
    members: list[Rec]
    link_marks: set[str] = dc_field(default_factory=set)
    merged_by: set[str] = dc_field(default_factory=set)   # the merge rules that joined it

    @property
    def rep(self) -> Rec:
        return self.members[0]

    def cell(self, prop: str) -> Cell | None:
        for rec in self.members:
            got = rec.cell(prop)
            if got is not None and got.value not in (None, ""):
                return got
        return None


# --------------------------------------------------------------------------- rows

class Row:
    """One view row being made: column values and where each came from."""

    def __init__(self, thing: Thing | None = None) -> None:
        self.values: dict[str, Any] = {}
        self.cites: dict[str, list[str]] = {}
        self.col_marks: dict[str, str] = {}
        self.audience: set[str] = set()
        self.marks: set[str] = set()
        self.objects: list[str] = []
        self.systems: list[str] = []
        self.handle: str | None = None
        self.copies = 0
        self.fx: Any = None   # an invoice head's stated exchange rate (Rate), lines use it
        # every other record a value was read from: object id -> the handles read ("*":
        # every current version's markings; none: its audience key only, the cells'
        # markings already read); per column, records only that column was read from
        self.reads: dict[str, set[str]] = defaultdict(set)
        self.col_reads: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.col_link_marks: dict[str, set[str]] = defaultdict(set)
        # per column, records the cell NAMES without a value read from them (the org
        # chart's manager): no permission of theirs joins the column; the cell is emptied
        # when one is an excluded person's (build and ``door.exclude``, ``col_objects``)
        self.col_names: dict[str, set[str]] = {}
        if thing is not None:
            self.add_thing(thing, primary=True)

    def add_thing(self, thing: Thing, *, primary: bool = False) -> None:
        """Every copy's audience key and version markings; the primary thing's ids too."""
        for rec in thing.members:
            self.audience.add(rec.audience_key)
            if not primary:
                self.reads[rec.object_id]   # noqa: B018 (its audience key and id)
        self.marks |= thing.link_marks
        if primary:
            self.objects = [r.object_id for r in thing.members]
            self.systems = sorted({system_of(r.binding) for r in thing.members})
            self.copies = len(thing.members)
            self.handle = _rep_handle(thing.rep)

    def read(self, cell: Cell | None, column: str, value: Any = None, *,
             rec: Rec | None = None) -> None:
        """Column ``column`` from ``cell`` (``value``: the cell's value made typed)."""
        if cell is None:
            self.values.setdefault(column, None)
            return
        self.values[column] = cell.value if value is None else value
        self.cites[column] = [cell.handle, cell.field_path]
        if cell.marks not in ("", "[]"):
            self.col_marks[column] = cell.marks
        if rec is not None:
            self.marks.add(rec.handles.get(cell.handle, "[]"))

    def read_record(self, oid: Any, handle: str = "*", *, column: str | None = None) -> None:
        """A value of this row (of ``column`` only, when given) was read from record ``oid``
        (its ``handle``): its audience key, markings and id join the row's (the column's)
        class at build. Not a record id: the row (column) is blocked, never readable."""
        oid = oid if isinstance(oid, str) and oid else UNRESOLVED
        target = self.reads if column is None else self.col_reads[column]
        target[oid].add(handle) if handle else target[oid]

    def inherit(self, head: "Row", columns: Iterable[tuple[str, str]] = ()) -> None:
        """A row read from ``head`` (a line from its order): everything ``head`` was read
        from joins this row; each (column, head column) copied with its cites, marks and
        the records only that column was read from."""
        self.audience |= head.audience
        self.marks |= head.marks
        for oid in head.objects:
            self.reads[oid]   # noqa: B018
        for oid, handles in head.reads.items():
            self.reads[oid] |= handles
        for col, src in columns:
            self.values[col] = head.values.get(src)
            if src in head.cites:
                self.cites[col] = head.cites[src]
            if src in head.col_marks:
                self.col_marks[col] = head.col_marks[src]
            self.derive(col, head, src)

    def derive(self, column: str, src_row: "Row", src: str) -> None:
        """``column`` is computed from ``src_row``'s column ``src``: its column marks and
        the records only it was read from carry over."""
        if src in src_row.col_marks and column not in self.col_marks:
            self.col_marks[column] = src_row.col_marks[src]
        for oid, handles in src_row.col_reads.get(src, {}).items():
            self.col_reads[column][oid] |= handles
        self.col_link_marks[column] |= src_row.col_link_marks.get(src, set())

    def used(self, rec: Rec, cell: Cell | None) -> None:
        if cell is not None:
            self.marks.add(rec.handles.get(cell.handle, "[]"))


def _rep_handle(rec: Rec) -> str:
    # the least-marked version's handle speaks for the record
    return sorted(rec.handles, key=lambda h: (_marks_size(rec.handles[h]), h))[0]


def pick(thing: Thing, prop: str) -> tuple[Cell | None, Rec | None]:
    """The first copy stating ``prop``: a blank (BC's encoded blank ``_x0020_`` too) is
    stated by none, so the next copy's value is read."""
    for rec in thing.members:
        got = rec.cell(prop)
        if got is not None and got.value not in (None, "") and (
                not isinstance(got.value, str) or _text(got.value) is not None):
            return got, rec
    return None, None


def fill(row: Row, thing: Thing, column: str, prop: str,
         conv: Callable[[Any], Any] | None = None) -> Any:
    cell, rec = pick(thing, prop)
    value = None
    if cell is not None:
        value = conv(cell.value) if conv else _text(cell.value)
        if value is None:
            cell = None
    row.read(cell, column, value, rec=rec)
    return value


CATEGORY_DOC = ("the item's category, read from Sage's item master first (its codes are "
                "product lines such as PLABEL, LIQUID, VCARE; no Sage category table is "
                "captured, so the code stands), "
                "else from BC's item category (PACK, FG, DIST, RAW: a different, coarser list)")


def fill_category(row: Row, thing: Thing, column: str = "category") -> str | None:
    """``column`` from the item's category: the Sage copy's when it states one, else the
    first copy that does (``CATEGORY_DOC``)."""
    sage = [r for r in thing.members if "sage" in (r.binding or "")]
    for rec in sage:
        cell = rec.cell("item_category")
        if cell is not None and _text(cell.value) is not None:
            row.read(cell, column, _text(cell.value), rec=rec)
            return row.values[column]
    return fill(row, thing, column, "item_category")


# Business Central's OData escapes a character an XML name cannot hold as ``_xHHHH_``
# (a blank option value is ``_x0020_``, "Sales_x0020_Shipment"): decoded, never shown
_XML_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _text(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw)
    if "_x" in text:
        text = _XML_ESCAPE.sub(lambda m: chr(int(m[1], 16)), text)
    text = text.strip()
    return text or None


def to_decimal(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def cents(amount: Decimal) -> Decimal:
    """A derived amount (a sum of quantity x price lines) to the cent, half up."""
    from decimal import ROUND_HALF_UP
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


_YMD = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def to_date(raw: Any) -> dt.date | None:
    """An ISO date or timestamp (its US Eastern day when zoned), or a Sage YYYYMMDD."""
    text = _text(raw)
    if not text:
        return None
    m = _YMD.match(text) or None
    try:
        if m:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        if _ISO.match(text):
            if len(text) > 10 and (text.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", text)):
                from zoneinfo import ZoneInfo
                stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
                return stamp.astimezone(ZoneInfo(BUSINESS_TZ)).date()
            m2 = _ISO.match(text)
            return dt.date(int(m2[1]), int(m2[2]), int(m2[3]))
    except ValueError:
        return None
    return None


_CUR = re.compile(r"^[A-Z]{3}$")


def to_currency(raw: Any) -> str | None:
    text = (_text(raw) or "").upper()
    return text if _CUR.match(text) else None


def money(row: Row, column: str, cell: Cell | None, rec: Rec | None,
          currency: tuple[Cell | None, Rec | None] | None = None) -> None:
    """A money column and its ``<column>_currency``: the cell's own currency, else the
    record's currency cell; no currency known leaves the amount out (never mixed)."""
    amount = to_decimal(cell.num if cell is not None and cell.num is not None
                        else (cell.value if cell is not None else None))
    code = to_currency(cell.currency) if cell is not None else None
    cur_cell, cur_rec = currency if currency is not None else (None, None)
    if code is None and cur_cell is not None:
        code = to_currency(cur_cell.value)
    if amount is None or code is None:
        row.read(None, column)
        row.read(None, column + "_currency")
        return
    row.read(cell, column, amount, rec=rec)
    if cell.currency is None and cur_cell is not None:
        row.read(cur_cell, column + "_currency", code, rec=cur_rec)
    else:
        row.read(cell, column + "_currency", code, rec=rec)


# --------------------------------------------------------------------------- views

@dataclass
class Col:
    name: str
    kind: str   # text | money | numeric | date | integer | boolean | currency
    doc: str


@dataclass
class View:
    name: str
    doc: str
    cols: list[Col]
    build: Callable[["Reader", dt.date], Iterable[Row]]
    reads: tuple[str, ...]
    memos: tuple[str, ...] = ()
    # a table built set-based in Postgres with its own read rule (``business_comms``):
    # fill(write, table, as_of) writes the made, empty table, returns the marks it read to
    fill: Callable[[Any, str, dt.date], Mapping[str, Any]] | None = None
    internal: tuple[tuple[str, str], ...] = ()   # its hidden columns (name, SQL type)
    # one row per record of its first read type (``generic_records.covered_types``: that
    # type is then left out of ``records``); False for a view of a few of them (``staff``:
    # 165 of 45,882 Person records)
    covers: bool = True
    # False: a table only a search reads (``business_comms.FILES``: its rows are each
    # re-gated by the item gate after the statement), never offered to a reader's queries:
    # left out of the dictionary, ``describe`` and the catalog (``listed``)
    listed: bool = True
    # the declared version of what the view's rows are (speed specs P4): bumped by hand when
    # its output can change; the print hashes it, never the code's text
    version: int = 1

    def col(self, name: str) -> Col | None:
        return next((c for c in self.cols if c.name == name), None)


VIEWS: dict[str, View] = {}


# every curated view's last column: which systems hold a copy of the row (``Row.systems``,
# the same list the row's lineage keeps), so "in BC", "in FileMaker" is a filter
SYSTEMS_COL = "source_systems"
SYSTEMS_DOC = ("the systems holding a copy of this row, sorted and comma separated: bc "
               "(Business Central), filemaker, portal (the Portal app), sage; e.g. "
               "'bc,filemaker,portal' (one system's rows: source_systems LIKE '%bc%'); the "
               "row's other columns are the joined thing's. 'bc,portal' is held in Business "
               "Central and the Portal")


def view(name: str, doc: str, cols: Sequence[tuple[str, str, str]], reads: Sequence[str],
         memos: Sequence[str] = (), covers: bool = True,
         version: int = 1) -> Callable[[Callable], Callable]:
    def wrap(fn: Callable) -> Callable:
        made: list[Col] = []
        for cname, kind, cdoc in cols:
            made.append(Col(cname, kind, cdoc))
            if kind == "money":
                made.append(Col(cname + "_currency", "currency",
                                f"ISO currency of {cname}; sums never mix currencies"))
        made.append(Col(SYSTEMS_COL, "text", SYSTEMS_DOC))
        VIEWS[name] = View(name, doc, made, fn, tuple(reads), tuple(memos), covers=covers,
                           version=version)
        return fn
    return wrap


def aging_bucket(due: dt.date | None, as_of: dt.date) -> tuple[int | None, str | None]:
    if due is None:
        return None, None
    days = (as_of - due).days
    if days <= 0:
        return days, "current"
    if days <= 30:
        return days, "1-30"
    if days <= 60:
        return days, "31-60"
    if days <= 90:
        return days, "61-90"
    return days, "over 90"


CUSTOMER_PROPS = ("name", "erp_code", "code", "currency", "email", "bc_email",
                  "bc_phone_number", "contact_person", "salesperson_code",
                  "sage_salesperson_code", "business_line", "bc_credit_limit", "bc_blocked")


@view("customers", "Every customer once: its Portal, Sage, BC (each company) and FileMaker "
      "copies joined by identity links and the party merge rule (merged_by says which); "
      "a chain's store accounts stay separate customers under one chain.",
      [("name", "text", "customer name"),
       ("customer_code", "text", "the customer's ERP code: its Sage account's code when it "
        "has one (erp_code, else code), else another copy's"),
       ("codes", "text", "every copy's code as 'system:code' (Customer erp_code, else "
        "code, per copy), comma separated: any system's code finds the customer"),
       ("merged_by", "text", MERGE_RULES_DOC),
       ("currency", "text", "the customer's billing currency: its Sage account's "
        "(Customer.currency of the Sage copy, the account its invoices and balances are "
        "booked on), else the first other copy's"),
       ("email", "text", "main email"), ("phone", "text", "main phone"),
       ("contact_person", "text", "main contact"),
       ("salesperson", "text", "salesperson code"),
       ("business_line", "text", "business line"),
       ("credit_limit", "money", "credit limit (BC bc_credit_limit, in that BC copy's "
        "own currency)"),
       ("blocked", "text", "BC blocked flag, when set"),
       ("chain", "text", "the chain or parent account (Sage national account, else the "
        "bill-to customer): one chain's store accounts share it"),
       ("chain_code", "text", "the chain's code"),
       ("chain_basis", "text", "national_account, bill_to or name_prefix (store accounts "
        "named '<chain> - <store>', at least 3 of them)"),
       ("party_handles", "list", party_keys.HANDLES_DOC.format(kind="customer"))],
      reads=("Customer", NATIONAL_TYPE), memos=("customers", "chains"))
def _customers(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    chains = customer_chains(rd)
    _r, things = customer_things(rd)
    rows = []
    for thing in unique_things(things):
        row = customer_row(thing)
        add_chain(row, thing, chains, "chain")
        chain = chains.get(thing.rep.object_id)
        row.values["chain_code"] = chain.code if chain and chain.name else None
        row.values["chain_basis"] = chain.basis if chain and chain.name else None
        rows.append(row)
    party_keys.fill(rows)
    yield from rows


def unique_things(things: Mapping[str, Thing]) -> list[Thing]:
    """Each real thing of a record id -> thing map once, by its representative id."""
    return sorted({id(t): t for t in things.values()}.values(), key=lambda t: t.rep.object_id)


def of_record_first(thing: Thing, system: str) -> Thing:
    """The thing with its system-of-record copies first (whose code speaks for it)."""
    return Thing(sorted(thing.members, key=lambda r: system_of(r.binding) != system),
                 thing.link_marks, thing.merged_by)


def party_codes(row: Row, thing: Thing, column: str, props: Sequence[str]) -> None:
    """``column``: every copy's code as 'system:code' ('sage:AQUIS12, bc:AQUIS12, ...'),
    so a code of any system finds the party."""
    seen: list[str] = []
    for rec in thing.members:
        for prop in props:
            cell = rec.cell(prop)
            text = _text(cell.value) if cell is not None else None
            if text:
                got = f"{system_of(rec.binding)}:{text}"
                if got not in seen:
                    seen.append(got)
                    row.used(rec, cell)
                break
    row.values[column] = ", ".join(sorted(seen)) or None


def customer_row(thing: Thing) -> Row:
    row = Row(thing)
    fill(row, thing, "name", "name")
    first = of_record_first(thing, PARTY_MERGE["Customer"].record_system)
    code = fill(row, first, "customer_code", "erp_code")
    if code is None:
        fill(row, first, "customer_code", "code")
    party_codes(row, thing, "codes", PARTY_MERGE["Customer"].code_props)
    row.values["merged_by"] = ", ".join(sorted(thing.merged_by)) or None
    # Sage is AR's system of record: its account's currency (the one its invoices and
    # balances are in) speaks for the customer, never a Portal or FileMaker default
    fill(row, first, "currency", "currency", to_currency)
    if fill(row, thing, "email", "email") is None:
        fill(row, thing, "email", "bc_email")
    fill(row, thing, "phone", "bc_phone_number")
    fill(row, thing, "contact_person", "contact_person")
    if fill(row, thing, "salesperson", "salesperson_code") is None:
        fill(row, thing, "salesperson", "sage_salesperson_code")
    fill(row, thing, "business_line", "business_line")
    money_of(row, thing, "credit_limit", "bc_credit_limit")
    fill(row, thing, "blocked", "bc_blocked")
    return row


AR_OPEN = "RelSageArOpenDocument"
AR_PROPS = ("idinvc", "idcust", "trxtypetxt", "trxtypeid", "dateinvc", "datedue",
            "amtduetc", "amtinvctc", "codecurn", "swpaid", "idorder", "idcustpo")
# Sage AR document types (trxtypetxt): 1 invoice, 2 debit note, 3 credit note, 4 interest,
# 5 unapplied cash, 10 prepayment, 11 receipt
SAGE_AR_DOC = {"1": "invoice", "2": "debit note", "3": "credit note", "4": "interest",
               "5": "unapplied cash", "10": "prepayment", "11": "receipt"}


def doc_type_doc(kinds: Mapping[str, str], money: str) -> str:
    """The ``document_type`` column's description: every kind the source's own type field
    (Sage ``txttrxtype``) names, and how a count of bills reads it."""
    return (f"the document's kind by Sage's own type (txttrxtype): {', '.join(kinds.values())}"
            f"; other: a type Sage names that is none of these. A count or total of invoices "
            f"(bills) is document_type = 'invoice'; {money} are not invoices")


@view("customer_balances", "What customers owe now: every open (unpaid) Sage AR document "
      "with its remaining balance, aged by due date, in its own currency. Business "
      "Central's aged receivables are not in this view.",
      [("customer", "text", "customer name (the linked customer)"),
       ("customer_code", "text", "Sage customer code"),
       ("document_number", "text", "invoice / credit note / receipt number"),
       ("document_type", "text", doc_type_doc(SAGE_AR_DOC, "receipts, prepayments, "
                                              "unapplied cash and credit notes")),
       ("document_date", "date", "document date"),
       ("due_date", "date", "due date"),
       ("amount_due", "money", "remaining balance, Sage amtduetc in codecurn (negative for "
        "credits and receipts); only documents Sage marks unpaid (swpaid 0)"),
       ("original_amount", "money", "document amount (Sage amtinvctc)"),
       ("days_overdue", "integer", "as_of - due_date in days (Sage datedue); 0 or less: "
        "not yet due. Overdue = owed and past due: amount_due > 0 and days_overdue > 0; a "
        "credit, receipt or prepayment (amount_due < 0) is never overdue whatever its date "
        "(net it in only when a net balance is asked for)"),
       ("aging_bucket", "text", "days_overdue in buckets: current (0 or less), 1-30, "
        "31-60, 61-90, over 90"),
       ("order_number", "text", "the sales order it bills, when stated"),
       ("customer_po", "text", "customer PO, when stated"),
       ("as_of", "date", "the day the aging was computed"), CHAIN_COL,
       handle_col("customer", f"the document's {AR_OPEN}.customer link")],
      reads=(AR_OPEN, "Customer"), memos=("customers", "chains"),
      covers=False)   # the open documents only: every AR document stays in records
def _customer_balances(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    docs = rd.records(AR_OPEN, AR_PROPS)
    customers, things = customer_things(rd)
    chains = customer_chains(rd)
    owner = rd.linked(f"{AR_OPEN}.customer", docs, customers)
    for oid in sorted(docs):
        rec = docs[oid]
        paid = rec.cell("swpaid")
        if paid is None or (_text(paid.value) or "") != "0":
            continue   # paid (or no paid flag stated): not open
        thing = Thing([rec])
        row = Row(thing)
        row.used(rec, paid)
        fill(row, thing, "customer_code", "idcust")
        fill(row, thing, "document_number", "idinvc")
        kind, krec = pick(thing, "trxtypetxt")
        row.read(kind, "document_type", SAGE_AR_DOC.get((_text(kind.value) or ""), "other")
                 if kind else None, rec=krec)
        fill(row, thing, "document_date", "dateinvc", to_date)
        due = fill(row, thing, "due_date", "datedue", to_date)
        cur = pick(thing, "codecurn")
        cell, crec = pick(thing, "amtduetc")
        if cell is None:
            cell, crec = rec.cell("amtduetc"), rec   # a zero balance is a value
        money(row, "amount_due", cell, crec, cur)
        money(row, "original_amount", *pick(thing, "amtinvctc"), cur)
        days, bucket = aging_bucket(due, as_of)
        row.values["days_overdue"], row.values["aging_bucket"] = days, bucket
        for col in ("days_overdue", "aging_bucket"):
            row.derive(col, row, "due_date")
        row.values["as_of"] = as_of
        fill(row, thing, "order_number", "idorder")
        fill(row, thing, "customer_po", "idcustpo")
        add_chain(row, customer_link(row, thing, owner, things, ORDER_RECORD_SYSTEM), chains)
        yield row


def name_linked(row: Row, ends: Sequence[tuple[str, str]], things: Mapping[str, Thing],
                column: str, prop: str = "name",
                of_record: Sequence[tuple[str, str]] = ()) -> Thing | None:
    """``column`` from the one real thing ``ends`` (``(id, link markings)``) link to: its
    copies' audience keys and the links' markings are read into the row. Two different
    things: the one the system-of-record copies' links (``of_record``, a subset of
    ``ends``) reach, when they reach exactly one (the system of record speaks for the
    record: a Portal copy linked to an unmerged FileMaker customer does not unstate the
    Sage order's customer); else, or none, unstated."""
    held = [(c, marks) for c, marks in ends if c in things]
    if len({things[c].rep.object_id for c, _ in held}) > 1:
        held = [(c, marks) for c, marks in of_record if c in things]
    if len({things[c].rep.object_id for c, _ in held}) != 1:
        row.values[column] = None
        return None
    for _c, marks in held:
        row.marks.add(marks)
    thing = things[held[0][0]]
    row.add_thing(thing)
    cell, rec = pick(thing, prop)
    row.read(cell, column, _text(cell.value) if cell else None, rec=rec)
    return thing


def party_handle(row: Row, party: str, thing: Thing | None) -> Thing | None:
    """``<party>_handle``: the view row key of ``thing`` (the one real party a link
    reached, ``name_linked``'s result), the same key the party's own view writes."""
    row.values[f"{party}_handle"] = _rep_handle(thing.rep) if thing is not None else None
    return thing


def code_from_party(row: Row, column: str, thing: Thing | None,
                    props: Sequence[str], system: str) -> None:
    """``column`` from the linked party's own code (its system-of-record copy first) when
    the record itself states none (Sage orders and invoices carry no customer code)."""
    if thing is None or row.values.get(column) is not None:
        return
    first = of_record_first(thing, system)
    for prop in props:
        if fill(row, first, column, prop) is not None:
            return


def customer_link(row: Row, thing: Thing, links: Mapping[str, Sequence[tuple[str, str]]],
                  things: Mapping[str, Thing], system: str) -> Thing | None:
    """The row's customer (name, handle) from the customer links (``links``: record id ->
    ends) of the record's copies; on a conflict, its ``system`` (of record) copies' links."""
    ends = [e for r in thing.members for e in links.get(r.object_id, ())]
    mine = [e for r in thing.members if system_of(r.binding) == system
            for e in links.get(r.object_id, ())]
    return party_handle(row, "customer", name_linked(row, ends, things, "customer",
                                                     of_record=mine))


def customer_things(rd: Reader) -> tuple[dict[str, Rec], dict[str, Thing]]:
    """The held customers and, per customer record, its real customer (memoised; the
    customers view reads the same things, so a row's customer_handle is its handle)."""
    def make() -> tuple[dict[str, Rec], dict[str, Thing]]:
        recs = rd.records("Customer", CUSTOMER_PROPS)
        return recs, {r.object_id: t for t in rd.clusters("Customer", recs) for r in t.members}
    return rd.memo("customers", make)


# A customer's chain: the Sage national account its accounts are grouped under (store
# accounts of one chain: "TAKE 5 -Waverly", "TAKE 5 -Summerville"), else the customer its
# account is billed to; a bill-to customer heads its own chain. Links only, never a name.


@dataclass
class Chain:
    name: str | None
    code: str | None
    basis: str
    cell: Cell | None
    rec: Rec | None
    audience: set[str]
    marks: set[str]
    ids: set[str] = dc_field(default_factory=set)   # every record its name was read from


def customer_chains(rd: Reader) -> dict[str, Chain]:
    """Customer cluster representative id -> its chain (memoised)."""
    return rd.memo("chains", lambda: _customer_chains(rd))


def _customer_chains(rd: Reader) -> dict[str, Chain]:
    recs, things = customer_things(rd)
    nats = rd.records(NATIONAL_TYPE, ("nameacct", "idnatacct"))
    national = rd.linked(NATIONAL, recs, nats, NATIONAL_TIERS)
    bill_to = rd.linked(BILL_TO, recs, recs)
    tops = {t.rep.object_id: t for t in things.values()}
    heads: set[str] = set()
    out: dict[str, Chain] = {}
    for top, thing in tops.items():
        ids = [r.object_id for r in thing.members]
        ends = [e for i in ids for e in national.get(i, ())]
        if len({n for n, _ in ends}) == 1:
            nat = nats[ends[0][0]]
            cell, rec = pick(Thing([nat]), "nameacct")
            code = nat.cell("idnatacct")
            name = _text(cell.value) if cell else None
            out[top] = Chain(name or (_text(code.value) if code else None),
                             _text(code.value) if code else None, "national_account",
                             cell if name else code, nat, {nat.audience_key},
                             {m for _, m in ends} | {nat.handles.get(
                                 (cell or code).handle, "[]") if (cell or code) else "[]"},
                             {nat.object_id})
            continue
        parents = {things[b].rep.object_id: m for i in ids for b, m in bill_to.get(i, ())
                   if b in things and things[b].rep.object_id != top}
        if len(parents) == 1:
            (parent, link_marks), = parents.items()
            heads.add(parent)
            pthing = tops[parent]
            cell, rec = pick(pthing, "name")
            code, _ = pick(pthing, "erp_code")
            out[top] = Chain(_text(cell.value) if cell else None,
                             _text(code.value) if code else None, "bill_to", cell, rec,
                             {r.audience_key for r in pthing.members},
                             {link_marks} | ({rec.handles.get(cell.handle, "[]")}
                                             if cell and rec else set()),
                             {r.object_id for r in pthing.members})
    for head in heads - set(out):   # a bill-to customer heads its own chain
        thing = tops[head]
        cell, rec = pick(thing, "name")
        code, _ = pick(thing, "erp_code")
        out[head] = Chain(_text(cell.value) if cell else None,
                          _text(code.value) if code else None, "bill_to", cell, rec,
                          set(), set())
    # else a store account named "<chain> - <store>" shared by CHAIN_MIN customers
    by_prefix: dict[str, list[tuple[str, str, Cell, Rec]]] = defaultdict(list)
    for top, thing in tops.items():
        if top in out:
            continue
        cell, rec = pick(thing, "name")
        got = chain_prefix(_text(cell.value) if cell else None)
        if got is not None:
            by_prefix[got[0]].append((top, got[1], cell, rec))
    for key, held in by_prefix.items():
        if len(held) < CHAIN_MIN:
            continue
        counts: dict[str, int] = defaultdict(int)
        for _top, text, _c, _r in held:
            counts[text] += 1
        name = sorted(counts, key=lambda t: (-counts[t], t))[0]
        # the name is the most common spelling of every store's: each store is read
        every = {r.object_id for t, *_ in held for r in tops[t].members}
        marks = {r.handles.get(c.handle, "[]") for _t, _x, c, r in held}
        for top, _text_, cell, rec in held:
            out[top] = Chain(name, None, "name_prefix", cell, rec, set(), set(marks), every)
    return out


CHAIN_MIN = 3
_SPLIT = re.compile(r"\s+-|-\s+|\s#|\(")


def chain_prefix(name: str | None) -> tuple[str, str] | None:
    """(key, text) of the chain a store account's name states ("TAKE 5 -Waverly",
    "Take 5 - Colorado Springs": "TAKE 5"), or None when the name has no store part or
    the part before it is too short to name a chain."""
    if not name:
        return None
    parts = _SPLIT.split(name, maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None
    text = " ".join(parts[0].split()).strip(" .,-")
    key = re.sub(r"[^0-9a-z]+", " ", text.casefold()).strip()
    if len(key) < 3 or not re.search(r"[a-z]", key):
        return None
    return key, text


def add_chain(row: Row, customer: Thing | None, chains: Mapping[str, Chain],
              column: str = "customer_chain") -> None:
    """``column``: the chain of the row's customer (its national account or bill-to);
    what it was read from joins the row's audience and markings."""
    chain = chains.get(customer.rep.object_id) if customer is not None else None
    if chain is None or chain.name is None:
        row.values[column] = None
        return
    row.values[column] = chain.name
    row.audience |= chain.audience
    row.marks |= chain.marks
    for oid in chain.ids:
        row.read_record(oid, "")
    if chain.cell is not None:
        row.cites[column] = [chain.cell.handle, chain.cell.field_path]
        if chain.cell.marks not in ("", "[]"):
            row.col_marks[column] = chain.cell.marks




def money_of(row: Row, thing: Thing, column: str, prop: str,
             currency_prop: str = "currency") -> None:
    """Money column ``column`` from the first copy stating ``prop``, in that copy's own
    currency (the cell's, else that record's ``currency_prop``): never another copy's."""
    cell, rec = pick(thing, prop)
    cur = (rec.cell(currency_prop), rec) if rec is not None else None
    money(row, column, cell, rec, cur)


def pilot_binding(binding: str) -> bool:
    """A BC record of a kind BC holds as pilot data (Sage is the system of record)."""
    return (binding or "").startswith(("bc.", "portal.bc_"))


PILOT_DOC = "true: a Business Central record (its rows are in this view; filter on pilot)"
INVOICE_PROPS = ("invoice_number", "bc_number", "order_number", "customer_number",
                 "customer_po", "status", "invoice_date", "due_date", "subtotal", "total_tax",
                 "total_amount", "remaining_amount", "currency", "exchange_rate",
                 "home_currency", "invoice_discount")


@view("sales_invoices", "Every customer invoice once, Sage first; Business Central invoices "
      "carry pilot = true. "
      "Each amount in the invoice's own currency; total includes tax, subtotal is before tax.",
      [("invoice_number", "text", "invoice number"),
       ("customer", "text", "customer name (the billed customer)"),
       ("customer_code", "text", "customer code on the invoice (customer_number), else the "
        "billed customer's own code (its Sage account's first)"),
       ("invoice_date", "date", "invoice date"),
       ("due_date", "date", "due date"),
       ("order_number", "text", "the sales order it bills"),
       ("customer_po", "text", "customer PO"),
       ("status", "text", "BC status (Open, Paid, Canceled); Sage invoices state none"),
       ("subtotal", "money", "amount before tax (subtotal)"),
       ("tax", "money", "tax amount (total_tax)"),
       ("total", "money", "total including tax (total_amount)"),
       ("remaining", "money", "still to pay, BC pilot invoices only (what customers owe "
        "now: customer_balances)"),
       ("exchange_rate", "numeric", "Sage: home currency (CAD) per one unit of the invoice's "
        "currency, as the invoice states it (1 for a CAD invoice); BC states none"),
       ("pilot", "boolean", PILOT_DOC), CHAIN_COL,
       handle_col("customer", "the invoice's SalesInvoice.billed_to link")],
      reads=("SalesInvoice", "Customer"), memos=("customers", "chains", "invoices"))
def _sales_invoices(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    seen: set[int] = set()
    for row in invoice_heads(rd).values():
        if id(row) not in seen:
            seen.add(id(row))
            yield row


def invoice_heads(rd: Reader) -> dict[str, Row]:
    """Invoice record id -> its invoice's row (one Row per real invoice, every copy's
    record id mapped to it; memoised: the invoice lines carry their invoice's columns)."""
    return rd.memo("invoices", lambda: _invoice_heads(rd))


def _invoice_heads(rd: Reader) -> dict[str, Row]:
    recs = rd.records("SalesInvoice", INVOICE_PROPS)
    customers, things = customer_things(rd)
    billed = rd.linked("SalesInvoice.billed_to", recs, customers)
    chains = customer_chains(rd)
    out: dict[str, Row] = {}
    for thing in rd.clusters("SalesInvoice", recs):
        row = Row(thing)
        if fill(row, thing, "invoice_number", "invoice_number") is None:
            fill(row, thing, "invoice_number", "bc_number")
        customer = customer_link(row, thing, billed, things, ORDER_RECORD_SYSTEM)
        add_chain(row, customer, chains)
        fill(row, thing, "customer_code", "customer_number")
        code_from_party(row, "customer_code", customer, PARTY_MERGE["Customer"].code_props,
                        PARTY_MERGE["Customer"].record_system)
        fill(row, thing, "invoice_date", "invoice_date", to_date)
        fill(row, thing, "due_date", "due_date", to_date)
        fill(row, thing, "order_number", "order_number")
        fill(row, thing, "customer_po", "customer_po")
        fill(row, thing, "status", "status")
        for column, prop in (("subtotal", "subtotal"), ("tax", "total_tax"),
                             ("total", "total_amount"), ("remaining", "remaining_amount")):
            money_of(row, thing, column, prop)
        row.values["pilot"] = all(pilot_binding(r.binding) for r in thing.members)
        row.fx = invoice_rate(row, thing)
        row.discount = header_money(thing, "invoice_discount")
        row.subtotal_stated = header_money(thing, "subtotal")
        for r in thing.members:
            out[r.object_id] = row
    return out


def header_money(thing: Thing, prop: str) -> tuple[Decimal, str, tuple[Cell, Rec]] | None:
    """An invoice header's own money cell ``prop`` (Sage ``invnetnotx`` subtotal,
    ``invdiscamt`` discount) from the first copy stating it with a currency; None when
    none states one."""
    for rec in thing.members:
        cell = rec.cell(prop)
        amount = _amount(cell)
        if amount is not None and cell is not None and to_currency(cell.currency):
            return amount, to_currency(cell.currency), (cell, rec)
    return None


def over_subtotal(head: Any, revenues: Sequence[Any]) -> bool:
    """Whether an invoice's lines (their ``line_economics`` revenue) must be reconciled to
    its header (``reconcile_to_subtotal``): they sum to more than the header's subtotal in
    its currency, or they cannot be summed in it (a line unpriced or in another currency)
    while the header states a discount."""
    sub = getattr(head, "subtotal_stated", None)
    if sub is None or not revenues:
        return False
    if any(r is None or r[1] != sub[1] for r in revenues):
        disc = getattr(head, "discount", None)
        return disc is not None and disc[0] != 0
    return sum((cents(r[0]) for r in revenues), Decimal(0)) > cents(sub[0])


def reconcile_to_subtotal(head: Any, lines: Sequence[dict[str, Any]]) -> None:
    """An invoice's line revenue made to add up to its header's subtotal, the figure its
    source states before tax.

    Sage's header subtotal (``invnetnotx``) is the lines' extended amounts less every
    discount the invoice takes, and its tax is reckoned on it. A line states only its own
    discount (``invdisc``): on 94 of 81,735 Sage invoices (11-21) the header took more
    (``invdiscamt``) than its lines carry, so the lines summed above the subtotal (by
    30.68 on IN1000180001, 95.47 on 1000105025, whose lines carry 100.51 of their own and
    the header 95.47 more). What the lines hold above the subtotal is taken off them in
    proportion to their revenue, to the cent (largest remainder), so they sum to it
    exactly; lines at or below the subtotal stay as stated (nothing is added to a line).
    Lines that cannot be summed in the subtotal's currency (one unpriced or in another
    currency) under a header that states a discount are left without revenue
    (``revenue_unknown``): the gross is never stated as the net. The header's subtotal
    (and discount) cells join each changed line's revenue and margin citations and
    markings."""
    sub = getattr(head, "subtotal_stated", None)
    if sub is None or not lines:
        return
    amount, currency, sub_cell = sub
    revs = [x["got"]["revenue"] for x in lines]
    if any(r is None or r[1] != currency for r in revs):
        disc = getattr(head, "discount", None)
        if disc is not None and disc[0] != 0:
            for line in lines:
                _unstate_revenue(line["got"])
        return
    base = sum((cents(r[0]) for r in revs), Decimal(0))
    excess = base - cents(amount)
    if excess <= 0 or base <= 0:
        return
    cited = [sub_cell]
    disc = getattr(head, "discount", None)
    if disc is not None and disc[0] != 0:
        cited.append(disc[2])
    raw = [excess * cents(r[0]) / base for r in revs]
    parts = [q.quantize(Decimal("0.01"), rounding="ROUND_FLOOR") for q in raw]
    left = int((excess - sum(parts, Decimal(0))) / Decimal("0.01"))
    for i in sorted(range(len(raw)), key=lambda k: (parts[k] - raw[k], k))[:left]:
        parts[i] += Decimal("0.01")
    for line, part in zip(lines, parts):
        economics = line["got"]
        rev = economics["revenue"]
        economics["revenue"] = (cents(rev[0]) - part, rev[1], rev[2] + cited)
        margin = economics["margin"]
        if margin is not None:
            economics["margin"] = (margin[0] - part, margin[1], margin[2] + cited)


def _unstate_revenue(economics: dict[str, Any]) -> None:
    economics["revenue"] = economics["margin"] = None
    economics["basis"] = "revenue_unknown"


@dataclass(frozen=True)
class Rate:
    """An invoice's stated exchange rate: ``home`` currency per one unit of ``source``."""
    rate: Decimal
    source: str
    home: str
    cells: tuple[tuple[Cell, Rec], ...]


def invoice_rate(row: Row, thing: Thing) -> Rate | None:
    """The invoice header's own exchange rate (Sage inrate with insourcurr/inhomecurr: home
    = source x rate), read from ONE copy that states all three; anything missing,
    unreadable or not positive gives None (no conversion, the margin stays unstated)."""
    for rec in thing.members:
        rate, src, home = (rec.cell(p) for p in ("exchange_rate", "currency", "home_currency"))
        if rate is None or src is None or home is None:
            continue
        value, s_code, h_code = _amount(rate), to_currency(src.value), to_currency(home.value)
        if value is None or value <= 0 or s_code is None or h_code is None:
            break
        row.read(rate, "exchange_rate", value, rec=rec)
        return Rate(value, s_code, h_code, ((rate, rec), (src, rec), (home, rec)))
    row.read(None, "exchange_rate")
    return None


INVOICE_LINE_PROPS = ("invoiced_quantity", "unit_price", "amount_excluding_tax",
                      "extended_amount", "invoice_discount_share", "unit_cost",
                      "unit_conversion", "cost_unit_conversion", "sage_item_code",
                      "description", "line_type")
# Sage OE invoice detail line types (oe_invoice_detail.linetype): 1 an item, 2 a
# miscellaneous charge; BC states its own word (Item, ...)
SAGE_LINE_TYPES = {"1": "item", "2": "misc"}
INVOICE_LINE_LINK = "SalesInvoiceLine.line_of"
INVOICE_ITEM_LINK = "SalesInvoiceLine.for_item"


def _amount(cell: Cell | None) -> Decimal | None:
    if cell is None:
        return None
    return to_decimal(cell.num if cell.num is not None else cell.value)


def _money_cell(row: Row, column: str, amount: Decimal | None, currency: str | None,
                cells: Sequence[tuple[Cell, Rec]]) -> None:
    """A derived money column (``amount`` in ``currency``), cited to the first of the
    cells it was computed from; every cell's marking joins the column's (the strictest
    reader rule: a reader lacking any of them is withheld the column)."""
    if amount is None or currency is None or not cells:
        row.values[column] = row.values[column + "_currency"] = None
        return
    row.values[column], row.values[column + "_currency"] = cents(amount), currency
    first = cells[0][0]
    row.cites[column] = row.cites[column + "_currency"] = [first.handle, first.field_path]
    marks = sorted({m for c, _r in cells for m in json.loads(c.marks or "[]")})
    if marks:
        row.col_marks[column] = row.col_marks[column + "_currency"] = json.dumps(marks)
    for c, r in cells:
        row.used(r, c)


def line_economics(rec: Rec, fx: Rate | None = None) -> dict[str, Any]:
    """One invoice line's revenue, cost and margin as its source states them, never mixing
    currencies: revenue = the amount before tax (BC amount_excluding_tax; Sage the
    extended amount less its share of the invoice discount); cost = quantity x unit cost
    converted from the selling unit to the costing unit (Sage: quantity x unit_conversion
    / cost_unit_conversion x unit cost; a stated unit cost of 0 costs 0); margin =
    revenue - cost in the revenue's currency. Sage states cost in the home currency: a line
    sold in another currency has its cost converted at its OWN invoice's stated rate
    (``fx``: cost / rate, only when the revenue is in the invoice's currency and the cost in
    its home currency; basis cost_converted_at_invoice_rate), else the margin is unstated."""
    out: dict[str, Any] = {"revenue": None, "cost": None, "margin": None,
                           "basis": "cost_unknown", "cells": {}}
    qty = rec.cell("invoiced_quantity")
    excl = rec.cell("amount_excluding_tax")
    ext = rec.cell("extended_amount")
    disc = rec.cell("invoice_discount_share")
    if _amount(excl) is not None and to_currency(excl.currency):
        out["revenue"] = (_amount(excl), to_currency(excl.currency), [(excl, rec)])
    elif _amount(ext) is not None and to_currency(ext.currency):
        amount, cur, used = _amount(ext), to_currency(ext.currency), [(ext, rec)]
        if _amount(disc) is not None and _amount(disc) != 0:
            if to_currency(disc.currency) != cur:
                amount = None   # a discount in another currency: revenue unknown
            else:
                amount -= _amount(disc)
                used.append((disc, rec))
        if amount is not None:
            out["revenue"] = (amount, cur, used)
    ucost = rec.cell("unit_cost")
    cost_amount, q = _amount(ucost), _amount(qty)
    cost_cur = to_currency(ucost.currency) if ucost is not None else None
    if cost_amount is not None and cost_cur is not None:
        if cost_amount == 0:
            out["cost"] = (Decimal(0), cost_cur, [(ucost, rec)])
        elif q is not None:
            conv, cconv = rec.cell("unit_conversion"), rec.cell("cost_unit_conversion")
            c1, c2 = _amount(conv), _amount(cconv)
            if conv is None and cconv is None:
                out["cost"] = (q * cost_amount, cost_cur, [(ucost, rec), (qty, rec)])
            elif c1 is not None and c2 is not None and c1 > 0 and c2 > 0:
                out["cost"] = (q * c1 / c2 * cost_amount, cost_cur,
                               [(ucost, rec), (qty, rec), (conv, rec), (cconv, rec)])
    rev, cost = out["revenue"], out["cost"]
    if rev is not None and cost is not None:
        if rev[1] == cost[1]:
            out["margin"] = (cents(rev[0]) - cents(cost[0]), rev[1], rev[2] + cost[2])
            out["basis"] = "same_currency"
        elif fx is not None and fx.source == rev[1] and fx.home == cost[1]:
            out["margin"] = (cents(rev[0]) - cents(cost[0] / fx.rate), rev[1],
                             rev[2] + cost[2] + list(fx.cells))
            out["basis"] = "cost_converted_at_invoice_rate"
        else:
            out["basis"] = "cost_in_other_currency"
    elif rev is None:
        out["basis"] = "revenue_unknown"
    return out


@view("sales_invoice_lines", "Every line of every customer invoice (sales_invoices: Sage "
      "first; Business Central lines carry pilot = true): product, quantity and its unit, revenue "
      "(before tax, after the invoice discount), cost and margin per line, each in its own "
      "currency. Sage states unit cost in the home currency (CAD): a line sold in another "
      "currency has its cost converted at its own invoice's exchange rate for the margin "
      "(margin_basis = cost_converted_at_invoice_rate; cost stays as stated, in CAD).",
      [("invoice_number", "text", "the invoice's number"),
       ("invoice_date", "date", "the invoice's date"),
       ("customer", "text", "the billed customer"), CHAIN_COL,
       ("item_code", "text", "the product's item code"),
       ("item_name", "text", "the product's name"),
       ("category", "text", "the product's category: " + CATEGORY_DOC),
       ("description", "text", "line description"),
       ("line_type", "text", "item or misc (a miscellaneous charge); BC states its own"),
       ("quantity", "numeric", "quantity invoiced"),
       ("quantity_unit", "text", "unit of quantity (sums only within one unit)"),
       ("unit_price", "money", "price per unit"),
       ("revenue", "money", "line revenue before tax, after its share of the invoice discount"),
       ("unit_cost", "money", "cost per costing unit as the source states it"),
       ("cost", "money", "line cost: quantity x unit cost (Sage: converted from the selling "
        "unit to the costing unit)"),
       ("margin", "money", "revenue - cost in the revenue's currency (a CAD cost converted "
        "at the invoice's own exchange rate when sold in another currency)"),
       ("margin_pct", "numeric", "margin / revenue x 100 (2 decimals)"),
       ("margin_basis", "text", "same_currency or cost_converted_at_invoice_rate (margin "
        "stated); cost_in_other_currency (no usable invoice rate), cost_unknown or "
        "revenue_unknown (margin unstated)"),
       ("pilot", "boolean", PILOT_DOC),
       ("invoice_handle", "text", "the sales_invoices row it is a line of (join "
        "sales_invoices on handle), through its SalesInvoiceLine.line_of link"),
       ("customer_code", "text", "the invoice's customer_code (sales_invoices)"),
       handle_col("customer", "its invoice's SalesInvoice.billed_to link (the invoice's "
                  "customer_handle)"),
       handle_col("product", "its line's item link: the key to join products on (item_code "
                  "is the code the line states, which the product row may carry differently)")],
      reads=("SalesInvoiceLine", "SalesInvoice", "Customer", "Product"),
      memos=("customers", "chains", "invoices", "products"))
def _sales_invoice_lines(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    heads = invoice_heads(rd)
    recs = rd.records("SalesInvoiceLine", INVOICE_LINE_PROPS)
    of = rd.linked(INVOICE_LINE_LINK, recs, heads)
    products, pthings = product_things(rd)
    item_of = rd.linked(INVOICE_ITEM_LINK, recs, products)
    things = rd.clusters("SalesInvoiceLine", recs)

    def head_of(thing: Thing) -> tuple[Any, list]:
        ends = [e for r in thing.members for e in of.get(r.object_id, ())]
        invoices = {id(heads[i]): heads[i] for i, _m in ends}
        # its invoice not held (removed), or two invoices: not this line's
        return (next(iter(invoices.values())) if len(invoices) == 1 else None), ends

    # first the invoices whose lines must be reconciled to their subtotal: their lines
    # are held until every line of the invoice is read, the rest go out as read
    revenues: dict[int, tuple[Any, list]] = {}
    for thing in things:
        head, _ends = head_of(thing)
        if head is not None:
            revenues.setdefault(id(head), (head, []))[1].append(
                line_economics(thing.rep, getattr(head, "fx", None))["revenue"])
    over = {key for key, (head, revs) in revenues.items() if over_subtotal(head, revs)}
    del revenues
    held: dict[int, tuple[Row, list[dict[str, Any]]]] = {}
    for thing in things:
        head, ends = head_of(thing)
        if head is None:
            continue
        row = Row(thing)
        row.inherit(head, ((c, c) for c in ("invoice_number", "invoice_date", "customer",
                                             "customer_chain", "pilot", "customer_handle",
                                             "customer_code")))
        row.marks |= {m for _i, m in ends}
        row.values["invoice_handle"] = head.handle
        product = name_linked(row, [e for r in thing.members for e in item_of.get(r.object_id, ())],
                              pthings, "item_name")
        party_handle(row, "product", product)
        if fill(row, thing, "item_code", "sage_item_code") is None and product is not None:
            if fill(row, product, "item_code", "code") is None:
                fill(row, product, "item_code", "erp_code")
        if product is not None:
            fill_category(row, product)
        else:
            row.values["category"] = None
        fill(row, thing, "description", "description")
        kind = fill(row, thing, "line_type", "line_type")
        if kind is not None:
            row.values["line_type"] = SAGE_LINE_TYPES.get(kind, kind.lower())
        rec = thing.rep
        qty = rec.cell("invoiced_quantity")
        q = _amount(qty)
        row.read(qty if q is not None else None, "quantity", q, rec=rec)
        unit = _text(qty.unit) if q is not None else None
        row.read(qty if unit else None, "quantity_unit", unit.upper() if unit else None, rec=rec)
        for column, prop in (("unit_price", "unit_price"), ("unit_cost", "unit_cost")):
            cell = rec.cell(prop)
            money(row, column, cell, rec if cell is not None else None)
        got = line_economics(rec, getattr(head, "fx", None))
        if id(head) in over:
            held.setdefault(id(head), (head, []))[1].append({"row": row, "rec": rec,
                                                             "got": got})
            continue
        yield _line_money(row, got)
    for head, lines in held.values():
        reconcile_to_subtotal(head, lines)
        for line in lines:
            yield _line_money(line["row"], line["got"])


def _line_money(row: Row, got: dict[str, Any]) -> Row:
    """A line's revenue, cost and margin columns from its economics (``line_economics``;
    reconciled to its invoice's subtotal: ``reconcile_to_subtotal``)."""
    for column in ("revenue", "cost", "margin"):
        part = got[column]
        if part is None:
            _money_cell(row, column, None, None, ())
        else:
            _money_cell(row, column, part[0], part[1], part[2])
    rev, margin = got["revenue"], got["margin"]
    row.values["margin_pct"] = (cents(margin[0] / cents(rev[0]) * 100)
                                if margin is not None and cents(rev[0]) != 0 else None)
    row.values["margin_basis"] = got["basis"]
    # what the margin was read from guards its share and basis too (a finance-marked
    # unit cost never shows through margin_pct)
    guard = row.col_marks.get("margin") or row.col_marks.get("cost")
    for column in ("margin_pct", "margin_basis"):
        if guard:
            row.col_marks[column] = guard
        if "margin" in row.cites:
            row.cites[column] = row.cites["margin"]
    return row


# --------------------------------------------------------------------------- sales orders

SO_EXTRA = ("order_number", "customer_po", "fm_erp_code", "erp_code", "order_date",
            "salesperson_code", "sage_salesperson_code", "sage_order_type")
# Sage OE order types (oe_order_header.type, read from the data: 73.8k type 1, 1.5k type 4
# whose complete code is 3, the quotes' "completed"); anything else is "other"
SAGE_ORDER_TYPES = {"1": "order", "4": "quote"}
SO_LINE_PROPS = ("shipped_quantity", "uom", "sage_item_code", "description", "line_type",
                 "outstanding_quantity", "shipment_date")
ORDER_LINK = "SalesOrder.ordered_by"
# the system of record for customer orders: an order no copy of which it holds (FileMaker
# order entry, a Portal-only order, a BC pilot order) is left out of plx.business reads by
# default (on the served data a third of those are unlinked copies of Sage orders: same
# customer and PO)
ORDER_RECORD_SYSTEM = "sage"
OF_RECORD_DOC = ("true: a Sage order header carries the order; false: held only in "
                 "FileMaker order entry, the Portal or BC")
ITEM_LINK = "SalesOrderLine.for_item"
# The systems whose order copies state the order's currency (``order_value``: the one
# rule of an order's currency of record, every surface's)
from operating.order_value import CURRENCY_STATED_BY  # noqa: E402


@dataclass
class Order:
    """One real customer order as the one sales-order rule reads it."""
    thing: Thing
    status: str
    reason: str | None
    wanted: Any                      # sales_order_status.Requested
    value: Any                       # order_value.OrderValue (the copy that has one)
    lines: list[str]                 # the line-carrying copy's held lines
    carrier: Rec | None              # that copy
    reads: dict[str, set[str]]       # other records its status and value read (Row.reads)
    link_marks: set[str]             # markings of the links its status and value followed
    basis: str = "rule"              # sage: Sage's own complete code decided the status
    order_type: str | None = None    # Sage's order type (order, quote, other)
    currency: str | None = None      # the order's currency of record (``order_currency``)
    currency_basis: str | None = None   # invoice, sage_order, bc_order, customer or why none
    currency_from: tuple[str, str, str] | None = None   # (record id, handle, field path)
    outstanding: Decimal | None = None    # sum of the lines' stated quantity still to ship
    ordered_on: dt.date | None = None     # the earliest order date any copy states
    last_invoice: dt.date | None = None   # the newest date of an invoice billing it


def order_currency(group: Sequence[Rec], invoices: Sequence[Rec], customer: Thing | None
                   ) -> Any:
    """The order's currency of record (``order_value.currency_of_record``, the one rule
    ``plx.object`` and the engine's cards use too) from its copies, its settled billing
    invoices and its one customer (None: no one customer)."""
    from operating import order_value as ov

    def rows(recs: Iterable[Rec], props: Sequence[str]) -> list[dict[str, Any]]:
        return [r for rec in recs for got in rec.rows(props).values() for r in got]
    return ov.currency_of_record(rows(group, ov.COPY_CURRENCY_PROPS),
                                 rows(invoices, ov.INVOICE_CURRENCY_PROPS),
                                 rows(customer.members if customer else (),
                                      ov.CUSTOMER_CURRENCY_PROPS))


def _cite_of(row: Mapping[str, Any] | None) -> tuple[str, str, str] | None:
    if not row:
        return None
    return (str(row.get("object_id")), str(row.get("l1_handle")), str(row.get("field_path")))


def sales_orders_model(rd: Reader) -> dict[str, Any]:
    return rd.memo("sales_orders", lambda: _sales_orders_model(rd))


def _sales_orders_model(rd: Reader) -> dict[str, Any]:
    """Every customer order once (settled identity clusters), each judged by the one
    status rule (``sales_order_status``, as the proactive engine reads it: closed when any
    copy is; a lead shipment or invoice, a closed lead twin, an unknown copy or no line on
    any copy leaves it unknown, never open) and valued by ``order_value.judge``."""
    from operating import order_value as ov
    from operating import po_status
    from operating import sales_order_status as so

    header = (*so.READ_PROPS, so.DATE_PROP, *so.OWN_DAYS, ov.TOTAL, ov.CURRENCY, ov.SUBTOTAL,
              *SO_EXTRA)
    found = rd.records(so.TYPE, tuple(dict.fromkeys(header)))
    things = rd.clusters(so.TYPE, found)
    root = {r.object_id: t.rep.object_id for t in things for r in t.members}
    lines = rd.records(ov.LINE_TYPE, tuple(dict.fromkeys(
        (*ov.LINE_PROPS, *ov.LINE_TEXT, *ov.CHECK_PROPS, *SO_LINE_PROPS))))
    line_ids = rd.ids(ov.LINE_TYPE)
    invoices = rd.records(ov.INVOICE_TYPE, (*ov.INVOICE_PROPS, "invoice_date"))
    invoice_ids = rd.ids(ov.INVOICE_TYPE)
    held_from = {"Shipment": rd.ids("Shipment"), ov.INVOICE_TYPE: invoice_ids}
    settled = set(ov.SETTLED)
    # fulfilment: a settled shipment or invoice (True), else a lead one (False)
    fulfilled: dict[str, bool] = {}
    fulfil_read: dict[str, set[str]] = defaultdict(set)
    followed: dict[str, set[str]] = defaultdict(set)   # order record -> link markings
    for a, b, m, lt, tier in rd.link_rows(so.FULFILLED_LINKS, None):
        if b in found and a in held_from[lt.split(".", 1)[0]]:
            fulfilled[b] = fulfilled.get(b, False) or tier in settled
            fulfil_read[b].add(a)
            followed[b].add(m or "[]")
    lines_of: dict[str, set[str]] = defaultdict(set)
    for a, b, m, _lt, tier in rd.link_rows([so.LINE_OF], None):
        if b in found and a in line_ids and tier in settled:
            lines_of[b].add(a)
            followed[b].add(m or "[]")
    # a line two orders hold is neither's (never valued or listed twice)
    shared = {ln for ln, n in Counter(ln for got in lines_of.values() for ln in got).items()
              if n > 1}
    for got in lines_of.values():
        got -= shared
    lined = {b for b, got in lines_of.items() if got}
    billed: dict[str, set[str]] = defaultdict(set)
    for a, b, m, _lt, tier in rd.link_rows(list(ov.BILLS), ov.SETTLED):
        if a in invoice_ids and b in found:
            billed[a].add(b)
            followed[b].add(m or "[]")
    invoices_of: dict[str, set[str]] = defaultdict(set)
    for inv, orders in billed.items():
        for o in orders:
            invoices_of[o].add(inv)
    lead_twins: dict[str, set[str]] = defaultdict(set)
    for a, b, m, _lt, tier in rd.link_rows(so.twin_links(), None):
        if a in found and b in found and root[a] != root[b]:
            lead_twins[root[a]].add(b)
            lead_twins[root[b]].add(a)
            followed[a].add(m or "[]")
            followed[b].add(m or "[]")
    binding = {oid: po_status.binding_of(oid) or "" for oid in found}
    props = {oid: rec.rows(header) for oid, rec in found.items()}
    state = {oid: so.record_status(props[oid], binding=binding[oid],
                                   fulfilled=fulfilled.get(oid, False)) for oid in found}
    grouped: dict[str, list] = defaultdict(list)
    for oid in found:
        own = [r.get("value") for n in so.OWN_DAYS for r in props[oid].get(n, ())]
        for r in props[oid].get(so.DATE_PROP, ()):
            grouped[binding[oid]].append((so.day_of(r), own))
    defaults = so.date_defaults(grouped)

    # the order's currency of record (``order_currency``): its customer is the one its
    # copies link (the system of record's copies' links first), one real customer only
    customers, cthings = customer_things(rd)
    ordered_by = rd.linked(ORDER_LINK, found, customers)

    def customer_of(group: Sequence[Rec]) -> Thing | None:
        for recs in ([r for r in group if system_of(r.binding) == ORDER_RECORD_SYSTEM], group):
            reps = {cthings[c].rep.object_id: cthings[c] for r in recs
                    for c, _m in ordered_by.get(r.object_id, ()) if c in cthings}
            if reps:
                return next(iter(reps.values())) if len(reps) == 1 else None
        return None

    def value_of(oid: str, of_record: Any) -> Any:
        own = props[oid]
        closed = so.record_status(own, binding=binding[oid]).cls == so.CLOSED
        got_lines = [lines[ln].rows(ov.LINE_PROPS + ov.LINE_TEXT + ov.CHECK_PROPS)
                     if ln in lines else {} for ln in sorted(lines_of.get(oid, ()))]
        got_inv = [(invoices[i].rows(ov.INVOICE_PROPS) if i in invoices else {},
                    billed[i] == {oid}) for i in sorted(invoices_of.get(oid, ()))]
        return ov.judge_order(own, object_id=oid, closed=closed, lines=got_lines,
                              invoices=got_inv, of_record=of_record)

    orders: dict[str, Order] = {}
    for thing in things:
        group = [r.object_id for r in thing.members]
        top = thing.rep.object_id
        order = so.order_status(state[m] for m in group)
        status, reason = order.cls, order.reason
        if order.cls != so.CLOSED:
            if any(fulfilled.get(m) is False for m in group):
                status, reason = so.UNKNOWN, "fulfilment_unsettled"
            elif any(state[t].cls == so.CLOSED or t in fulfilled
                     for t in lead_twins.get(top, ())):
                status, reason = so.UNKNOWN, "closed_in_unsettled_twin"
            elif order.cls == so.UNKNOWN:
                pass
            elif order.cls == so.DRAFT:
                status, reason = so.DRAFT, None
            elif not any(m in lined for m in group):
                status, reason = so.UNKNOWN, so.NO_LINES
        # Sage is the system of record for its orders: an order a Sage OE header carries
        # takes Sage's own complete code (1/2 still to ship, 3/4/5 complete) and its order
        # type (a quote is never an open order); the rule above stands for the rest
        basis, order_kind = "rule", None
        sage = next((m for m in group if any(so.word(r.get("value"))
                                             for r in props[m].get(so.SAGE_CODE, ()))), None)
        if sage is not None:
            typ = next((so.word(r.get("value")) for r in props[sage].get("sage_order_type", ())
                        if so.word(r.get("value"))), "")
            order_kind = SAGE_ORDER_TYPES.get(typ, "other") if typ else None
            sage_code = next(so.word(r.get("value")) for r in props[sage].get(so.SAGE_CODE, ())
                             if so.word(r.get("value")))
            if order_kind not in (None, "order"):
                status, reason, basis = so.CLOSED if order_kind == "quote" else so.UNKNOWN, \
                    f"sage_order_type_{typ}", "sage"
            elif sage_code in so.SAGE_WORDS:
                status, reason, basis = so.SAGE_WORDS[sage_code], None, "sage"
        wanted = so.requested_date([
            (props[m].get(so.DATE_PROP, ()),
             [r.get("value") for n in so.OWN_DAYS for r in props[m].get(n, ())],
             defaults.get(binding[m], ())) for m in group])
        of_record = order_currency(
            thing.members, [invoices[i] for m in group for i in sorted(invoices_of.get(m, ()))
                            if i in invoices], customer_of(thing.members))
        # valued from its copy of record (Sage's when it holds one: ``value_copies``)
        value = None
        for m in ov.value_copies(group):
            got = value_of(m, of_record)
            if value is None or (value.amount is None and got.amount is not None):
                value = got
            if got.amount is not None:
                break
        carrier = next((m for m in group if lines_of.get(m)), None)
        # everything the status and value were read from: value rows' records (their
        # versions), fulfilment records, lead twins and their lines and invoices
        read: dict[str, set[str]] = defaultdict(set)
        for obj in ([*value.rows, *value.check_rows] if value else ()):
            if obj.get("object_id") not in set(group):
                read[obj.get("object_id")].add(obj.get("l1_handle") or "*")
        for m in group:
            for a in fulfil_read.get(m, ()):
                read[a].add("*")
            for ln in lines_of.get(m, ()):
                read[ln]   # noqa: B018 (lines decide the status: their keys and ids)
            for inv in invoices_of.get(m, ()):
                read[inv]   # noqa: B018
        for t in lead_twins.get(top, ()):
            read[t].add("*")
        marks = {mk for m in group for mk in followed.get(m, ())}
        orders[top] = Order(thing, status, reason, wanted, value,
                            sorted(ln for ln in lines_of.get(carrier, ()) if ln in lines)
                            if carrier else [], found.get(carrier) if carrier else None, read,
                            marks, basis, order_kind, of_record.code, of_record.basis,
                            _cite_of(of_record.row))
        _activity(orders[top], group, props, lines, invoices, invoices_of)
    return {"orders": orders, "lines": lines, "settle_days": _settle_days(orders.values())}


def _activity(order: "Order", group: Sequence[str], props: Mapping[str, Any],
              lines: Mapping[str, Rec], invoices: Mapping[str, Rec],
              invoices_of: Mapping[str, set[str]]) -> None:
    """What the records state of an order's progress, for ``open_state``: the quantity its
    lines still owe (only when EVERY line states one), the day it was ordered and the newest
    invoice billing it."""
    stated = [lines[ln].cell("outstanding_quantity") for ln in order.lines if ln in lines]
    owed = [to_decimal(c.value) if c is not None else None for c in stated]
    order.outstanding = sum(owed, Decimal(0)) if owed and None not in owed else None
    days = [to_date(r.get("value")) for m in group for r in props[m].get("order_date", ())]
    order.ordered_on = min((d for d in days if d), default=None)
    billed = [to_date(invoices[i].cell("invoice_date").value) for m in group
              for i in invoices_of.get(m, ()) if i in invoices
              and invoices[i].cell("invoice_date") is not None]
    order.last_invoice = max((d for d in billed if d), default=None)


SETTLE_MIN_ORDERS = 200      # closed orders needed before their pace says anything
SETTLE_SHARE = 0.99          # the share of closed orders that were fully invoiced within it
SETTLE_FLOOR_DAYS = 30


def _settle_days(orders: Iterable["Order"]) -> int | None:
    """How long closed orders take: the days from order to last invoice that
    ``SETTLE_SHARE`` of the closed orders that state both were done within (measured on the
    served data 11-23: 121 days for 72.8k closed Sage orders). None under
    ``SETTLE_MIN_ORDERS`` such orders: no pace, so no order is called stale."""
    spans = sorted((o.last_invoice - o.ordered_on).days for o in orders
                   if o.status == "closed" and o.last_invoice and o.ordered_on
                   and o.last_invoice >= o.ordered_on)
    if len(spans) < SETTLE_MIN_ORDERS:
        return None
    return max(SETTLE_FLOOR_DAYS, spans[min(len(spans) - 1, int(len(spans) * SETTLE_SHARE))])


def open_state(order: "Order", days_late: int | None, as_of: dt.date,
               settle_days: int | None) -> str | None:
    """Whether an open order is still going: ``nothing_outstanding`` (every line states
    nothing left to ship: the header is open, the goods are not owed), ``stale`` (past its
    required date by more than closed orders take to finish and nothing ordered or invoiced
    within that time: nobody is working it) or ``live``. Not open: None."""
    if order.status != "open":
        return None
    if order.outstanding is not None and order.outstanding <= 0:
        return "nothing_outstanding"
    if settle_days is not None and days_late is not None and days_late > settle_days:
        last = max((d for d in (order.ordered_on, order.last_invoice) if d), default=None)
        if last is None or (as_of - last).days > settle_days:
            return "stale"
    return "live"


def _order_row(rd: Reader, order: Order, things: Mapping[str, Thing],
               ordered_by: Mapping[str, list]) -> Row:
    thing = order.thing
    row = Row(thing)
    for oid, handles in order.reads.items():
        for h in handles or {""}:
            row.read_record(oid, h)
    row.marks |= order.link_marks
    fill(row, thing, "order_number", "order_number")
    customer = customer_link(row, thing, ordered_by, things, ORDER_RECORD_SYSTEM)
    add_chain(row, customer, customer_chains(rd))
    if fill(row, thing, "customer_code", "erp_code") is None:
        fill(row, thing, "customer_code", "fm_erp_code")
    code_from_party(row, "customer_code", customer, PARTY_MERGE["Customer"].code_props,
                    PARTY_MERGE["Customer"].record_system)
    row.values["of_record"] = any(system_of(r.binding) == ORDER_RECORD_SYSTEM
                                  for r in thing.members)
    return row


# the dates an order without an entered one of its own takes from the one submission that
# covers it: none stated, a source default, or the order's own day (an entry default)
CARRIED_FROM_COVER = ("no_required_date", "required_date_default")


def _order_covers(orders: Mapping[str, Order], things: Mapping[str, Thing],
                  ordered_by: Mapping[str, Sequence[tuple[str, str]]]) -> dict[str, list[str]]:
    """Covering order -> the orders its PO-range number covers (``operating.order_cover``:
    same one customer by settled links, the covered order's customer PO in the range)."""
    from operating import order_cover

    def value(thing: Thing, prop: str) -> Any:
        cell, _ = pick(thing, prop)
        return cell.value if cell is not None else None

    def customer(thing: Thing) -> str | None:
        ends = {things[e].rep.object_id for r in thing.members
                for e, _m in ordered_by.get(r.object_id, ()) if e in things}
        return ends.pop() if len(ends) == 1 else None
    return order_cover.covers((top, value(o.thing, "order_number"),
                               value(o.thing, "customer_po"), customer(o.thing))
                              for top, o in orders.items())


@view("sales_orders", "Every customer order once (Portal, FileMaker, Sage and BC copies of "
      "one order joined by settled identity links), its status by the one sales-order rule "
      "and its value by the one order-value rule (Sage zeroes invoiced headers, so a value "
      "may be derived from the invoices that bill it or its lines: value_basis says which).",
      [("order_number", "text", "order number"),
       ("customer", "text", "customer name (the ordering customer)"),
       ("customer_code", "text", "customer's ERP code on the order (erp_code, else "
        "fm_erp_code), else the ordering customer's own code (its Sage account's first)"),
       ("customer_po", "text", "customer PO"),
       ("order_date", "date", "order date"),
       ("required_date", "date", "the date the customer asked for: SalesOrder.required_date "
        "of the copy whose source ranks first, empty when that date is no request (see "
        "required_date_note); when no copy holds an entered date, the one covering "
        "submission's (required_date_basis covered_by)"),
       ("required_date_note", "text", "why required_date is empty: required_date_own_day "
        "(the source's date equals the order's own day, order_date or the day the record "
        "was written: an entry default, so the order has no promised date and is never "
        "late), required_date_default (a day the source stamps on many orders at once), "
        "no_required_date, required_date_unreadable or required_date_disputed (two "
        "top-ranked copies state different days)"),
       ("required_date_basis", "text", "own: the order's own copies state required_date; "
        "covered_by: none of them holds an entered date, so it is the entered date of the "
        "one submission that covers the order (covered_by); empty when there is no date"),
       ("covers", "text", "for a submission numbered by a PO range (PO8735-43, 8811-8812, "
        "PO-008672/73): the order numbers of the same customer's orders whose customer PO "
        "is in the range (separate orders, never copies of it)"),
       ("covered_by", "text", "the order numbers of the submissions whose PO range covers "
        "this order's customer PO (same customer)"),
       ("covered_by_handle", "text", "the sales_orders row of the covering submission (join "
        "sales_orders on handle); empty when none or two cover it"),
       ("status", "text", "open, closed, draft or unknown (never guessed open)"),
       ("status_reason", "text", "why the status is unknown (or sage_order_type_<code>: "
        "a Sage quote or other non-order document)"),
       ("status_basis", "text", "sage: Sage's own order-complete code (1/2 open, 3/4/5 "
        "complete) for a Sage order; rule: the cross-system "
        "sales-order rule (Portal, FileMaker and BC copies)"),
       ("order_type", "text", "Sage order type: order, quote or other (empty: not a Sage "
        "order)"),
       ("days_late", "integer", "as_of - required_date in days, for status open orders "
        "with a required_date; above 0 is late, 0 or less not yet due; empty when the "
        "order is not open or has no required_date"),
       ("open_state", "text", "for an open order: live (counted late when days_late is above "
        "0), nothing_outstanding (every line states nothing left to ship: the header is open, "
        "nothing is owed), stale (past its required date by more than settle_days and "
        "nothing ordered or invoiced within settle_days: nobody is working it) or "
        "copy_of_record (held only outside Sage and the very order of record of the same "
        "customer and customer PO, one of each: the order of record is the one counted). "
        "Empty when the order is not open. Only live orders are counted late"),
       ("settle_days", "integer", "the days within which 99% of closed orders were fully "
        "invoiced after their order date (the build's own measure of all closed orders that "
        "state both; empty when too few do): the line between late and stale"),
       ("last_activity", "date", "the newest of the order date and the date of an invoice "
        "billing the order"),
       ("value", "money", "the order's value (see value_basis)"),
       ("value_basis", "text", "source_total (its own total), invoices (sum of the invoices "
        "billing it, before tax) or order_lines (sum of its lines)"),
       ("value_note", "text", "why the order has no value"),
       ("salesperson", "text", "salesperson code"),
       ("line_count", "integer", "order lines (of the copy that holds them)"),
       ("as_of", "date", "the day days_late was computed"),
       ("of_record", "boolean", OF_RECORD_DOC), CHAIN_COL,
       handle_col("customer", f"the order's {ORDER_LINK} link")],
      reads=("SalesOrder", "SalesOrderLine", "SalesInvoice", "Shipment", "Customer"),
      memos=("customers", "sales_orders", "chains"))
def _sales_orders(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    rows = list(_sales_order_rows(rd, as_of))
    mark_copies(rows)
    yield from rows


def mark_copies(rows: Sequence[Row]) -> None:
    """An order only FileMaker, the Portal or BC hold that is the SAME order as an order of
    record is its copy: same customer, same customer PO, and that pair held by exactly one
    order of record and exactly one copy-only order (a generic PO such as "NA" or one held
    twice proves nothing). The copy's ``open_state`` is ``copy_of_record`` and the order of
    record's row is cited (its records join the column's class); the order of record is the
    one counted. Measured 11-23: 21 of 100 open FileMaker-only and 31 of 73 BC-only orders."""
    def key(row: Row) -> tuple[str, str] | None:
        po = _norm_name(row.values.get("customer_po"))
        owner = row.values.get("customer_handle")
        return (owner, po) if po and owner else None
    held: dict[tuple[str, str], tuple[list[Row], list[Row]]] = defaultdict(lambda: ([], []))
    for row in rows:
        k = key(row)
        if k is not None:
            held[k][0 if row.values.get("of_record") else 1].append(row)
    for record, copies in held.values():
        if len(record) != 1 or len(copies) != 1:
            continue
        copy = copies[0]
        if copy.values.get("status") == "open" and copy.values.get("open_state") is not None:
            copy.values["open_state"] = "copy_of_record"
            for oid in record[0].objects:
                copy.read_record(oid, "*", column="open_state")
            copy.cites.pop("open_state", None)


def _sales_order_rows(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    from operating import order_cover
    from operating import sales_order_status as so
    model = sales_orders_model(rd)
    customers, things = customer_things(rd)
    found = {r.object_id: r for o in model["orders"].values() for r in o.thing.members}
    ordered_by = rd.linked(ORDER_LINK, found, customers)
    cover = _order_covers(model["orders"], things, ordered_by)
    covering = order_cover.covered_by(cover)

    def number(top: str) -> str:
        cell, _ = pick(model["orders"][top].thing, "order_number")
        return str(cell.value) if cell is not None else ""

    def read_from(row: Row, top: str, column: str) -> None:
        for rec in model["orders"][top].thing.members:
            row.read_record(rec.object_id, "*", column=column)
    for top in sorted(model["orders"]):
        order = model["orders"][top]
        thing = order.thing
        row = _order_row(rd, order, things, ordered_by)
        # the covers link both ways: never an identity, each order its own row
        for column, them in (("covers", cover.get(top, ())),
                             ("covered_by", covering.get(top, ()))):
            row.values[column] = ", ".join(number(t) for t in them) or None
            for t in them:
                read_from(row, t, column)
        subs = covering.get(top, ())
        row.values["covered_by_handle"] = (_rep_handle(model["orders"][subs[0]].thing.rep)
                                           if len(subs) == 1 else None)
        if row.values["covered_by_handle"]:
            read_from(row, subs[0], "covered_by_handle")
        fill(row, thing, "customer_po", "customer_po")
        fill(row, thing, "order_date", "order_date", to_date)
        day = order.wanted.day if order.wanted is not None else None
        note = order.wanted.reason if order.wanted else None
        if note == so.OWN_DAY:
            # the date-defaults rule: a required date equal to the order's own day is the
            # entry form's default (Sage OE's expected-ship), no date the customer asked
            # for: the order has no promised date and is never late by it
            day = None
        wanted, basis = order.wanted, "own"
        if day is None and len(subs) == 1 and (note in CARRIED_FROM_COVER or note == so.OWN_DAY):
            # no entered date of its own: the one covering submission's entered date (a
            # real request, not its own day nor a default) is the date the customer asked
            # for; two covering submissions, or none with a real date, carry nothing
            theirs = model["orders"][subs[0]].wanted
            if theirs is not None and theirs.day is not None and theirs.reason is None:
                wanted, basis, day, note = theirs, "covered_by", theirs.day, None
                read_from(row, subs[0], "required_date")
                read_from(row, subs[0], "required_date_basis")
        row.values["required_date"] = day
        row.values["required_date_note"] = note
        row.values["required_date_basis"] = basis if day is not None else None
        if day is not None and wanted.governing:
            first = wanted.governing[0]
            row.cites["required_date"] = [first["l1_handle"], first["field_path"]]
            if first.get("markings") not in (None, "", "[]"):
                row.col_marks["required_date"] = first["markings"]
        row.values["status"], row.values["status_reason"] = order.status, order.reason
        row.values["status_basis"], row.values["order_type"] = order.basis, order.order_type
        row.values["days_late"] = (as_of - day).days if day and order.status == "open" else None
        row.derive("days_late", row, "required_date")
        row.values["open_state"] = open_state(order, row.values["days_late"], as_of,
                                              model["settle_days"])
        row.values["settle_days"] = model["settle_days"]
        row.values["last_activity"] = max((d for d in (order.ordered_on, order.last_invoice)
                                           if d), default=None)
        value = order.value
        if value is not None and value.amount is not None:
            row.values["value"] = cents(value.amount)
            row.values["value_currency"] = value.currency
            first = value.rows[0] if value.rows else None
            if first is not None:
                row.cites["value"] = [first["l1_handle"], first["field_path"]]
            from operating.order_value import markings
            marks = markings(value.rows)
            if marks:
                row.col_marks["value"] = row.col_marks["value_currency"] = json.dumps(marks)
            row.values["value_basis"] = value.basis
            row.derive("value_basis", row, "value")
            if order.currency_from is not None and value.currency == order.currency and (
                    first is None or first.get("object_id") != order.currency_from[0]):
                # the currency of record was read from another record (an invoice, the
                # Sage copy, the customer): cited, and that record joins the column's class
                oid, handle, path = order.currency_from
                row.cites["value_currency"] = [handle, path]
                row.read_record(oid, handle, column="value_currency")
        else:
            row.values["value_note"] = value.reason if value is not None else "no_value"
        if fill(row, thing, "salesperson", "salesperson_code") is None:
            fill(row, thing, "salesperson", "sage_salesperson_code")
        row.values["line_count"] = len(order.lines)
        row.values["as_of"] = as_of
        yield row


def _money_of_record(row: Row, column: str, cell: Cell | None, rec: Rec | None,
                     order: Order) -> None:
    """``money`` for a cell of a copy stating no currency: its amount in the order's
    currency of record (``Order.currency``, cited to the record it was read from); none
    known leaves the amount out."""
    amount = to_decimal(cell.num if cell is not None and cell.num is not None
                        else (cell.value if cell is not None else None))
    if amount is None or order.currency is None:
        row.read(None, column)
        row.read(None, column + "_currency")
        return
    row.read(cell, column, amount, rec=rec)
    row.values[column + "_currency"] = order.currency
    if order.currency_from is not None:
        oid, handle, path = order.currency_from
        row.cites[column + "_currency"] = [handle, path]
        row.read_record(oid, handle, column=column + "_currency")


@view("sales_order_lines", "The lines of every customer order once (the lines of the one "
      "copy of the order that holds them), with the order's number, customer and status.",
      [("order_number", "text", "order number"),
       ("customer", "text", "customer name"),
       ("order_status", "text", "the order's status (sales_orders.status)"),
       ("order_date", "date", "order date"),
       ("item_code", "text", "item code"),
       ("item_name", "text", "item name (the linked product)"),
       ("description", "text", "line description"),
       ("ordered_quantity", "numeric", "quantity ordered"),
       ("shipped_quantity", "numeric", "quantity shipped"),
       ("outstanding_quantity", "numeric", "quantity still to ship as the source states it "
        "(not computed)"),
       ("uom", "text", "unit of measure"),
       ("unit_price", "money", "unit price"),
       ("line_amount", "money", "line amount: the line's own stated total, else ordered_quantity "
        "x unit_price less the line discount (order_value line rule)"),
       ("line_type", "text", "source line type"),
       ("of_record", "boolean", "the order's of_record (sales_orders.of_record)"), CHAIN_COL,
       ("order_handle", "text", "the sales_orders row it is a line of (join sales_orders "
        "on handle)"),
       ("customer_code", "text", "the order's customer_code (sales_orders)"),
       handle_col("customer", "its order's SalesOrder.ordered_by link (the order's "
                  "customer_handle)"),
       handle_col("product", f"the line's {ITEM_LINK} link: the key to join products on. "
                  "item_code is the code the Sage line states and often is not the code the "
                  "product row carries (Sage 'BR900', the product's own 'PT-BR900'): 64,485 "
                  "of 219,175 lines (29%) on 11-23 named no product row by code; join on this")],
      reads=("SalesOrderLine", "SalesOrder", "Product", "Customer"),
      memos=("customers", "sales_orders", "products", "chains"))
def _sales_order_lines(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    from operating import order_value as ov
    model = sales_orders_model(rd)
    customers, things = customer_things(rd)
    found = {r.object_id: r for o in model["orders"].values() for r in o.thing.members}
    ordered_by = rd.linked(ORDER_LINK, found, customers)
    lines = model["lines"]
    products, pthings = product_things(rd)
    item_of = rd.linked(ITEM_LINK, lines, products)
    held = Counter(ln for o in model["orders"].values() for ln in o.lines)
    for top in sorted(model["orders"]):
        order = model["orders"][top]
        if not order.lines:
            continue
        head = _order_row(rd, order, things, ordered_by)
        odate = pick(order.thing, "order_date")
        carrier = order.carrier
        ocur = (carrier.cell("currency"), carrier) if carrier is not None else (None, None)
        code = to_currency(ocur[0].value) if ocur[0] is not None else None
        # lines of a copy stating no currency (CURRENCY_STATED_BY) are in the order's
        # currency of record, never in the one their own cells say
        stated = carrier is not None and system_of(carrier.binding) in CURRENCY_STATED_BY
        if not stated:
            code = order.currency
        for ln in order.lines:
            if held[ln] != 1:
                continue   # a line two orders hold: not one order's line (never twice)
            rec = lines[ln]
            lthing = Thing([rec])
            row = Row(lthing)
            row.inherit(head, ((c, c) for c in ("order_number", "customer", "customer_chain",
                                                "of_record", "customer_handle",
                                                "customer_code")))
            row.values["order_handle"] = head.handle
            row.values["order_status"] = order.status
            row.read(odate[0], "order_date", to_date(odate[0].value) if odate[0] else None,
                     rec=odate[1])
            product = None
            if fill(row, lthing, "item_code", "sage_item_code") is None:
                product = name_linked(row, item_of.get(ln, []), pthings, "item_code", "code")
                if row.values.get("item_code") is None and product is not None:
                    cell, prec = pick(product, "erp_code")
                    row.read(cell, "item_code", _text(cell.value) if cell else None, rec=prec)
            else:
                ends = item_of.get(ln, [])
                if len({pthings[c].rep.object_id for c, _ in ends if c in pthings}) == 1:
                    product = pthings[next(c for c, _ in ends if c in pthings)]
                    for _c, marks in ends:
                        row.marks.add(marks)
                    row.add_thing(product)
            party_handle(row, "product", product)
            if product is not None:
                cell, prec = pick(product, "name")
                row.read(cell, "item_name", _text(cell.value) if cell else None, rec=prec)
            else:
                row.values["item_name"] = None
            fill(row, lthing, "description", "description")
            for column in ("ordered_quantity", "shipped_quantity", "outstanding_quantity"):
                fill(row, lthing, column, column, to_decimal)
            fill(row, lthing, "uom", "uom")
            cell, crec = pick(lthing, "unit_price")
            if stated:
                money(row, "unit_price", cell, crec, ocur)
            else:
                _money_of_record(row, "unit_price", cell, crec, order)
            # priced by the one order-value rule; the cell's currency, else its order's
            got = ov._line_amount(rec.rows(ov.LINE_PROPS),
                                  (lambda r: to_currency(r.get("currency")) or code)
                                  if stated else (lambda r: code))
            if isinstance(got, tuple) and got[1] is not None:
                amount, cur, used = got
                row.values["line_amount"], row.values["line_amount_currency"] = cents(amount), cur
                row.cites["line_amount"] = [used[0]["l1_handle"], used[0]["field_path"]]
                if not stated and order.currency_from is not None:
                    oid, handle, path = order.currency_from
                    row.cites["line_amount_currency"] = [handle, path]
                    row.read_record(oid, handle, column="line_amount_currency")
                marks = sorted({m for u in used for m in json.loads(u["markings"] or "[]")})
                if marks:
                    row.col_marks["line_amount"] = json.dumps(marks)
            fill(row, lthing, "line_type", "line_type")
            yield row


import sys as _sys
if __name__ == "__main__":
    _sys.modules.setdefault("operating.pg.business_views", _sys.modules[__name__])

_registering = False


def _register_submodules() -> None:
    """Import every business-view submodule once its prerequisites have finished loading so
    importing any submodule or running ``python3 -m operating.pg.business_views`` in any
    order never fails with a circular-import error."""
    global _registering, _supply, _ar, _shipments, _chain, _chain_extra, _staff, _comms, _parties
    if _registering:
        return
    _registering = True
    try:
        # The products, batches, stock and purchasing views (their own module, registered here
        # in this order after the sales views); their names stay readable as this module's.
        from operating.pg import business_views_supply as _supply  # noqa: E402,F401
        if not hasattr(_supply, "PO_PROPS"):
            return
        # every Sage AR document with its paid date (receivables of a past day)
        from operating.pg import business_views_ar as _ar  # noqa: E402,F401
        if "receivable_documents" not in VIEWS:
            return
        # typed customer shipments with linked sales order required_date and on_time verdict
        from operating.pg import business_views_shipments as _shipments  # noqa: E402,F401
        if "shipments" not in VIEWS:
            return
        # typed supply-chain views: receipts, assembly_orders, lots (PLAN-A18 Part 2)
        from operating.pg import business_views_chain as _chain  # noqa: E402,F401
        if not hasattr(_chain, "_single_linked"):
            return
        # typed chain views Part 3: receipt_lines, quotes, freight_bills (PLAN-A18 Part 3)
        from operating.pg import business_views_chain_extra as _chain_extra  # noqa: E402,F401
        if not hasattr(_chain_extra, "_freight_bills"):
            return
        # the org chart (staff), read from the org capture's store beside the staff Persons
        from operating.pg import business_staff as _staff  # noqa: E402,F401
        # registered last, after the business views of their rank (stale_views orders them so):
        # the communications tables (emails, Teams, meetings, documents) register into VIEWS too
        from operating.pg import business_comms as _comms  # noqa: E402,F401
        # the company (party) and chain columns of the customer views (contract C7)
        from operating.pg import parties as _parties  # noqa: E402
        _parties.declare_columns(VIEWS)
    finally:
        _registering = False


_register_submodules()


def product_things(rd: Reader) -> tuple[dict[str, Rec], dict[str, Thing]]:
    return _supply.product_things(rd)


def __getattr__(name: str) -> Any:
    return getattr(_supply, name)


# --------------------------------------------------------------------------- build

def columns(v: View) -> list[tuple[str, str]]:
    return [(c.name, SQL_TYPES[c.kind]) for c in v.cols] + list(COMMON)


def _ddl(v: View, table: str) -> str:
    cols = ", ".join(f'"{n}" {t}' for n, t in columns(v))
    return f"CREATE TABLE {SCHEMA}.{table} ({cols})"


from operating.pg.business_views_access import (  # noqa: E402,F401  (split out for size)
    _indexes, access_ids, access_pattern, build_view, col_class_ids, excluded_objects,
    feed_head, stale_views, sweep_leftovers)


def _guarded_views() -> Any:
    """plx.sql's guarded-view module (``operating.pg.sql_views``: drop/create the
    security-barrier view over one built table, for whichever schema is built), or None
    where it is not installed in this tree."""
    import importlib.util
    if importlib.util.find_spec("operating.pg.sql_views") is None:
        return None
    from operating.pg import sql_views
    return sql_views


def dependent_views(write: Any, name: str) -> list[str]:
    """Views (any schema) whose definition reads ``<SCHEMA>.<name>``."""
    got = write.execute(
        "SELECT DISTINCT vn.nspname || '.' || vc.relname FROM pg_depend d "
        "JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class vc ON vc.oid = r.ev_class "
        "JOIN pg_namespace vn ON vn.oid = vc.relnamespace "
        "WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = to_regclass(%s) "
        "AND vc.oid <> d.refobjid ORDER BY 1", (f"{SCHEMA}.{name}",)).fetchall()
    return [r[0] for r in got]


def today() -> dt.date:
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo(BUSINESS_TZ)).date()


STAMP_SQL = (
    "SELECT (SELECT max(v.ver) FROM object_versions v), (SELECT max(l.rowid) FROM link_rows l), "
    "(SELECT count(*) FROM object_removals), "
    "(SELECT count(*) FROM objects WHERE missing_from_source_since IS NOT NULL)")
REFRESH_LOCK = 41041   # pg advisory lock key: one refresh at a time
# A build is a batch job reading whole record types: a read may take minutes on a loaded
# box (the served API's 120 s per statement would stop it); still bounded.
BUILD_STATEMENT_MS = 30 * 60 * 1000


def stamp(conn: Any) -> str:
    """What the views were built from: the newest version and link and the removals the
    store holds (a delta that adds, changes or removes records moves it)."""
    row = conn.execute(STAMP_SQL).fetchone()
    return "data:" + "/".join(str(x) for x in row)


def refresh(dsn: str, *, schema: Any = None, as_of: dt.date | None = None,
            force: bool = False, heartbeat: Any = None) -> dict[str, Any]:
    """Rebuild the views whose inputs moved since they were built (``stale_views``: the
    change feed's rows of the types a view reads, else the store ``stamp``) or that are
    missing or were built on another day (aging and lateness are by day); else nothing.
    One refresh at a time (an advisory lock); before migration 0041 it does
    nothing (``skipped``).

    Each view on its own (``view_state``, migration 0060; speed specs P2 + P6): a view that
    fails keeps its old table and is recorded with its reason and class; the other views are
    built all the same and the run returns normally, listing it under ``failed``
    (``all_failed`` when every view it tried failed: the caller exits 1). The same failure
    twice holds the view (``quarantined``). Without migration 0060 there is nowhere to
    record a failure, so the run raises after every view was tried. ``heartbeat``
    (``view_state.Heartbeat``) is told each step."""
    import psycopg
    from operating.pg.sqlite_compat import PgCompatConnection
    from operating.pg import view_state as vs
    when = as_of or today()
    with psycopg.connect(dsn, autocommit=True) as meta:
        if meta.execute(f"SELECT to_regclass('{SCHEMA}.state')").fetchone()[0] is None:
            return {"skipped": "migration 0041_business_views not applied"}
        if not meta.execute("SELECT pg_try_advisory_lock(%s)", (REFRESH_LOCK,)).fetchone()[0]:
            return {"skipped": "another refresh is running"}
        try:
            if heartbeat is not None:
                heartbeat.start()   # READY once the lock is held (speed specs P2)
            return _refresh_locked(dsn, meta, schema, when, force, heartbeat, vs,
                                   PgCompatConnection, psycopg)
        finally:
            meta.execute("SELECT pg_advisory_unlock(%s)", (REFRESH_LOCK,))


def _refresh_locked(dsn: str, meta: Any, schema: Any, when: dt.date, force: bool,
                    heartbeat: Any, vs: Any, PgCompatConnection: Any, psycopg: Any
                    ) -> dict[str, Any]:
    """``refresh`` under its lock (its doc)."""
    conn = PgCompatConnection(dsn)
    try:
        now = stamp(conn)
    finally:
        conn.close()
    swept = sweep_leftovers(meta)
    from operating.pg import business_views_code as code
    adopted = code.adopt(meta, SCHEMA, VIEWS)   # built before fingerprints: no rebuild
    recoded = code.code_changed(meta, SCHEMA, VIEWS)
    stale = stale_views(meta, VIEWS, now, when, force)
    from operating.pg import parties
    party_found = parties.lookup(meta)   # {} until the parties are built (migration 0066)
    party_digest = parties.digest(party_found) if party_found else None
    if party_found and not force:   # a view built under other parties is built again
        stale = parties.stale_with_parties(meta, stale)
    # a comms view whose print is not the code's is never rebuilt whole here (speed specs P4:
    # that is a Spark train); its old rows keep serving and keep their in-place updates
    train = [] if force else code.needs_train(meta, SCHEMA, VIEWS)
    tracked = vs.ready(meta, SCHEMA)
    prints = {n: code.fingerprint(v) for n, v in VIEWS.items()} if tracked else {}
    out: dict[str, Any] = {}
    failures: dict[str, str] = {}
    attempted: list[str] = []
    remade: list[str] = []   # views whose rows were made again: their health is measured
    if tracked and not force:
        from operating import disk_budget
        stale, held = vs.runnable(meta, SCHEMA, stale, prints,
                                  available_gb=lambda: disk_budget.status()["available_gb"])
        if held:
            out["quarantined"] = held
    else:
        held = {}
    built_views = {r[0] for r in meta.execute(f"SELECT view FROM {SCHEMA}.state")}
    checked_at, head = dt.datetime.now(dt.timezone.utc), feed_head(meta)
    if tracked:   # nothing they read moved: current as of this check
        vs.ok(meta, SCHEMA, [n for n in VIEWS if n not in stale and n not in held
                             and n in built_views], watermark=head, at=checked_at)

    def step(phase: str, views: Sequence[str]) -> None:
        attempted.extend(v for v in views if v in VIEWS)
        if heartbeat is not None:
            heartbeat.step(phase, views)

    def done(names: Sequence[str], at: dt.datetime, mark: Any) -> None:
        good = [n for n in names if n not in failures]
        remade.extend(good)
        if heartbeat is not None:
            heartbeat.moved()
        if tracked:
            took = dict(meta.execute(f"SELECT view, seconds FROM {SCHEMA}.state "
                                     "WHERE view = ANY(%s)", (good,)).fetchall()) if good else {}
            vs.ok(meta, SCHEMA, good, watermark=mark, versions=prints, seconds=took, at=at)
        parties.record(meta, good, party_digest)

    # a comms table built before moves by its changed records only, in place
    marks = {row[0]: (row[1] if isinstance(row[1], dict) else json.loads(row[1] or
                                                                       "{}")).get("marks")
             for row in meta.execute(f"SELECT view, notes FROM {SCHEMA}.state")}
    changed = [n for n in stale if not force and VIEWS[n].fill and marks.get(n)]
    stale = [n for n in stale if n not in changed and n not in train]
    if changed:
        from operating.pg import business_comms
        out["updated"] = []
        with psycopg.connect(dsn, autocommit=True) as write:
            write.execute(f"SET statement_timeout = {BUILD_STATEMENT_MS}")
            for n in changed:
                at, mark = dt.datetime.now(dt.timezone.utc), feed_head(meta)
                step(f"update {n}", [n])
                try:
                    out["updated"].append(business_comms.refresh_changed(
                        write, n, marks[n], digest=now))
                except Exception as exc:  # noqa: BLE001 - recorded per view, the rest go on
                    failures[n] = f"{type(exc).__name__}: {str(exc)}"
                done([n], at, mark)
    if swept:
        out["swept"] = swept
    if adopted:
        out["code_recorded"] = adopted
    if recoded:
        out["code_changed"] = recoded
    if train:   # said in the run, in view_state and as an alarm until the train swaps
        out["needs_train"] = train
    if stale:
        # side by side (``business_views_parallel``): 29 min one after the other
        from operating.pg import business_views_parallel as parallel
        seconds = dict(meta.execute(f"SELECT view, seconds FROM {SCHEMA}.state"))
        # the order views first, and the precomputed order pages made from them at
        # once (the door serves a page only while it is as new as these views: made
        # at the end of the whole refresh they were stale most of the time); the
        # other views then; a view that fails never stops another (``view_state``)
        from operating.pg import record_pages
        first = [n for n in stale if n in record_pages.SOURCES]
        rest = [n for n in stale if n not in first]
        built: list[Any] = []
        for group in (first, rest):
            if not group:
                continue
            at, mark = dt.datetime.now(dt.timezone.utc), feed_head(meta)
            step(f"build {', '.join(group)}", group)
            try:
                got, errors = parallel.build(dsn, group, schema=schema, as_of=when,
                                             digest=now, seconds=seconds)
            except Exception as exc:  # noqa: BLE001 - the whole call failed: each view did
                got, errors = [], {n: f"{type(exc).__name__}: {str(exc)}"
                                   for n in group}
            built += got
            failures.update(errors)
            for g in got:   # a build that waited for its turn is not current
                if isinstance(g, dict) and g.get("paused") and g.get("view") in group:
                    failures.setdefault(g["view"], f"BuildPaused: {g['paused']}")
            done(group, at, mark)
            # the record pages of the families whose views were built now
            if any(n in record_pages.SOURCES and n not in failures for n in group):
                pages = _follow(meta, vs, tracked, failures, "record_page_items", step,
                                lambda: record_pages.follow(dsn))
                if pages:
                    out["record_pages_early" if group is first else "record_pages"] = pages
        out["built"] = built
    hard = {n: r for n, r in failures.items() if not r.startswith("BuildPaused: ")}
    if hard and not tracked:   # nowhere to record them: the run fails, after every view
        from operating.pg.view_state import ViewsFailed
        raise ViewsFailed(hard)
    # the number columns' indexes, on every refresh (also when nothing was stale:
    # a table in place before them gets them so; never in ``build``/``build_view``,
    # whose code the views' fingerprints hash: a change there rebuilds every table)
    # the search's number indexes only speed it up: a failure here never stops the
    # refresh (the generic records still follow), it is reported and retried next run
    try:
        with psycopg.connect(dsn, autocommit=True) as write:
            write.execute(f"SET statement_timeout = {BUILD_STATEMENT_MS}")
            made = _number_indexes(write)
    except psycopg.Error as exc:
        made = []
        out["number_indexes_failed"] = f"{type(exc).__name__}: {exc}"
    if made:
        out["number_indexes"] = made
    from operating.pg import generic_records   # records/record_links, once built
    generic = _follow(meta, vs, tracked, failures, "records", step,
                      lambda: generic_records.follow(dsn, when))
    if generic:
        out["generic"] = generic
    if "record_pages" not in out and "record_pages_early" not in out:
        from operating.pg import record_pages   # the precomputed pages, once built
        pages = _follow(meta, vs, tracked, failures, "record_page_items", step,
                        lambda: record_pages.follow(dsn))
        if pages:
            out["record_pages"] = pages
    # the companies (parties) over the customer and supplier rows made now: the views that
    # carry them are rebuilt at the next refresh when what they carry changed
    if {"customers", "suppliers"} & set(remade) or _parties_unbuilt(meta):
        try:
            out["parties"] = {k: v for k, v in parties.build(dsn).items()
                              if not k.startswith("_")}
        except Exception as exc:  # noqa: BLE001 - said; the customer views carry NULL until built
            out["parties_failed"] = f"{type(exc).__name__}: {exc}"
            print(json.dumps({"parties_failed": out["parties_failed"]}), file=sys.stderr,
                  flush=True)
    if tracked:
        vs.seen(meta, SCHEMA, vs.newest_source(meta, {n: v.reads for n, v in VIEWS.items()}))
        from operating.pg import column_health
        unmeasured = {r[0] for r in meta.execute(
            f"SELECT s.view FROM {SCHEMA}.state s WHERE NOT EXISTS (SELECT 1 FROM "
            f"{SCHEMA}.column_health h WHERE h.view = s.view)")} & set(VIEWS)
        todo = [n for n in VIEWS if n in set(remade) | unmeasured]
        step("column health", [])
        try:   # a health reading never stops the refresh: said in its output and the journal
            out["column_health"] = column_health.measure(
                meta, SCHEMA, todo, [n for n in todo if VIEWS[n].fill is not None])
        except Exception as exc:  # noqa: BLE001 - reported, the views are built
            out["column_health_failed"] = f"{type(exc).__name__}: {exc}"
            print(json.dumps({"column_health_failed": out["column_health_failed"]}),
                  file=sys.stderr, flush=True)
    if train and tracked:   # after their in-place updates (``ok``) so the mark stays
        for n in train:
            vs.needs_train(meta, SCHEMA, n, version=prints.get(n))
    try:   # every view the measure registry and the Ask catalog name must exist
        from operating.pg import named_views
        gone = named_views.report(meta, SCHEMA)
        if gone:
            out["views_named_missing"] = gone
    except Exception as exc:  # noqa: BLE001 - said in the output; the ops alarm also checks
        out["views_named_check_failed"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps({"views_named_check_failed": out["views_named_check_failed"]}),
              file=sys.stderr, flush=True)
    if remade:   # the measures over the views just made again (contract C3)
        _measure_values(dsn, meta, sorted(set(remade)), out)
    # daily AR snapshot (PLAN-P2-A5, migration 0087): lightweight upsert after customer_balances
    # refreshes or when today's snapshot has not been recorded yet
    if meta.execute(f"SELECT to_regclass('{SCHEMA}.ar_daily_snapshots')").fetchone()[0] is not None:
        if "customer_balances" in remade or not meta.execute(
                f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.ar_daily_snapshots "
                "WHERE snapshot_date = %s)", (when,)).fetchone()[0]:
            try:
                from operating.pg import ar_daily_snapshots
                out["ar_daily_snapshots"] = ar_daily_snapshots.run(meta, schema=SCHEMA)
            except Exception as exc:  # noqa: BLE001 - reported; never fails the refresh
                out["ar_daily_snapshots_failed"] = f"{type(exc).__name__}: {exc}"
                print(json.dumps({"ar_daily_snapshots_failed": out["ar_daily_snapshots_failed"]}),
                      file=sys.stderr, flush=True)
    if failures and tracked:
        out["failed"] = {n: {"reason": r, **vs.failed(meta, SCHEMA, n, r, version=prints.get(n))}
                         for n, r in failures.items() if not r.startswith("BuildPaused: ")}
        tried = set(attempted)
        out["all_failed"] = bool(tried) and tried <= set(failures)
        print(json.dumps({"views_failed": out["failed"]}, default=str), file=sys.stderr,
              flush=True)
    return out or {"fresh": now}


def _parties_unbuilt(meta: Any) -> bool:
    """Whether the party tables exist (migration 0066) and hold nothing yet."""
    if meta.execute("SELECT to_regclass('biz.party_members')").fetchone()[0] is None:
        return False
    return meta.execute("SELECT NOT EXISTS (SELECT 1 FROM biz.party_members)").fetchone()[0]


def _measure_values(dsn: str, meta: Any, views: Sequence[str], out: dict[str, Any]) -> None:
    """``measure_values.build`` of the measures whose view was made again this run, once
    migration 0061 made the table; its failure is said in the run's output and the journal
    and never stops the refresh (the old rows keep their as_of)."""
    if meta.execute(f"SELECT to_regclass('{SCHEMA}.measure_values')").fetchone()[0] is None:
        return
    from operating import measures
    names = [n for n, m in measures.measures().items() if m["view"] in set(views)]
    if not names:
        return
    from operating.pg import measure_values
    try:
        got = measure_values.build(dsn, dsn, names)
    except Exception as exc:  # noqa: BLE001 - said; the views are built
        out["measure_values_failed"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps({"measure_values_failed": out["measure_values_failed"]}),
              file=sys.stderr, flush=True)
        return
    out["measure_values"] = {n: g.get("rows", g.get("served", g.get("error")))
                             for n, g in got.items()}
    bad = sorted(n for n, g in got.items() if "check" in g and not g["check"]["equal"])
    if bad:   # stored and statement differ in one snapshot: said, an alarm reads it
        out["measure_values_unequal"] = bad
        print(json.dumps({"measure_values_unequal": bad}), file=sys.stderr, flush=True)


def _follow(meta: Any, vs: Any, tracked: bool, failures: dict[str, str], name: str,
            step: Any, run: Any) -> Any:
    """One follower of the views (generic records, the record pages): its failure is
    recorded under ``name`` like a view's, and never stops the refresh."""
    at = dt.datetime.now(dt.timezone.utc)
    step(f"follow {name}", [])
    try:
        got = run()
    except Exception as exc:  # noqa: BLE001 - recorded under its name, the refresh goes on
        failures[name] = f"{type(exc).__name__}: {str(exc)}"
        return None
    if tracked:
        vs.ok(meta, SCHEMA, [name], watermark=None, at=at)
    return got


MIGRATION = os.path.join(os.path.dirname(__file__), "migrations", "0041_business_views.sql")


def ensure_scratch_schema(write: Any) -> None:
    """A scratch schema (``PLX_BIZ_SCHEMA`` other than ``biz``) gets migration 0041's DDL
    with its own name: the same state table and reader grants; the served ``biz`` schema
    is only ever made by the migration."""
    if SCHEMA == SERVED_SCHEMA:
        raise ValueError("the served schema biz is made by migration 0041, not here")
    with open(MIGRATION, encoding="utf-8") as fh:
        ddl = fh.read()
    # one at a time (side-by-side builds each ensure it: their GRANTs on the one schema
    # failed "tuple concurrently updated"), the lock released with the transaction
    with write.transaction():
        write.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"ensure {SCHEMA}",))
        write.execute(re.sub(r"\bbiz\b", SCHEMA, ddl))


def drop_scratch_schema(dsn: str) -> str:
    """Drop the scratch schema (after a benchmark); never the served one."""
    import psycopg
    if SCHEMA == SERVED_SCHEMA:
        raise ValueError("refusing to drop the served schema biz (PLX_BIZ_SCHEMA is unset)")
    with psycopg.connect(dsn, autocommit=True) as write:
        write.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    return SCHEMA


def build(dsn: str, names: Sequence[str] | None = None, *, schema: Any = None,
          as_of: dt.date | None = None, digest: str | None = None) -> list[dict[str, Any]]:
    import psycopg
    from operating.pg.sqlite_compat import PgCompatConnection
    if schema is None:
        from ontology.schema import load_schema
        schema = load_schema()
    unknown = sorted(set(names or ()) - set(VIEWS))
    if unknown:
        raise ValueError(f"unknown views: {', '.join(unknown)}")
    conn = PgCompatConnection(dsn, statement_timeout_ms=BUILD_STATEMENT_MS)
    try:
        reader = Reader(conn, schema)
        if digest is None:
            digest = stamp(conn)
        with psycopg.connect(dsn, autocommit=True) as write:
            # the writes (a comms table's chunks, flags, indexes) are capped as a refresh's
            # are: one stuck statement fails its view, it never holds the build for hours
            write.execute(f"SET statement_timeout = {BUILD_STATEMENT_MS}")
            if SCHEMA != SERVED_SCHEMA:
                ensure_scratch_schema(write)
            if write.execute(f"SELECT to_regclass('{SCHEMA}.state')").fetchone()[0] is None:
                raise RuntimeError("schema biz is missing: apply migration "
                                   "0041_business_views first (python3 -m operating.pg.migrate)")
            todo = list(names or list(VIEWS))
            out, failed = [], []
            seen = feed_head(write)   # before the reads: a change during the build rebuilds
            for i, n in enumerate(todo):
                try:   # one view failing (old table kept) never stops the views after it
                    out.append(build_view(n, reader, write, as_of=as_of or today(),
                                          digest=digest))
                    write.execute(f"UPDATE {SCHEMA}.state SET notes = notes || %s::jsonb "
                                  "WHERE view = %s", (json.dumps({"feed": seen}), n))
                except Exception as exc:  # noqa: BLE001 - reported below, the build fails
                    from operating.pg.build_pause import BuildPaused   # a quiet retry
                    if isinstance(exc, BuildPaused):
                        out.append({"view": n, "paused": str(exc)[:200]})
                    else:
                        failed.append(f"{n}: {type(exc).__name__}: {str(exc)[:200]}")
                # what no later view shares is let go (bounded memory over a whole build)
                later = {k for m in todo[i + 1:] for k in VIEWS[m].memos}
                for key in [k for k in reader._memo if k not in later]:
                    del reader._memo[key]
            if failed:
                raise RuntimeError("views not built (old tables kept): " + "; ".join(failed))
            return out
    finally:
        conn.close()


def _number_indexes(write: Any) -> list[str]:
    """The number columns' bytewise indexes on the tables in place (``number_indexes``)."""
    from operating.pg import number_indexes
    return number_indexes.ensure(write, SCHEMA, VIEWS, NUMBER_COLS)


LINEAGE_DOC = ("every row's source records (object ids, systems, citations) are in "
               "lineage (join on view and handle); the raw records stay reachable, so a "
               "derived column's definition can be checked or replaced")


def dictionary() -> dict[str, Any]:
    """The data dictionary, generated from the view specs: every view, the source types it
    is built from, its columns and what they mean (a derived column says how it is
    computed and from which raw fields)."""
    return {name: {"about": v.doc, "sources": list(v.reads), "lineage": LINEAGE_DOC,
                   "columns": {c.name: f"{c.kind}: {c.doc}" for c in v.cols}}
            for name, v in listed().items()}


def listed() -> dict[str, View]:
    """The views a reader's queries may name (``View.listed``)."""
    return {name: v for name, v in VIEWS.items() if v.listed}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="business_views")
    ap.add_argument("command", choices=("build", "refresh", "status", "dictionary",
                                           "drop-scratch"))
    ap.add_argument("--view", action="append")
    ap.add_argument("--dsn")
    ap.add_argument("--force", action="store_true", help="refresh: rebuild every view")
    args = ap.parse_args(argv)
    if args.command == "dictionary":
        print(json.dumps(dictionary(), indent=1))
        return 0
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    if args.command == "drop-scratch":
        print(json.dumps({"dropped": drop_scratch_schema(dsn)}))
        return 0
    if args.command == "status":
        import psycopg
        with psycopg.connect(dsn) as c:
            for row in c.execute(f"SELECT view, built_at, rows, seconds FROM {SCHEMA}.state "
                                 "ORDER BY view"):
                print(*row)
        return 0
    # full speed (No nice on a build); the unit bounds memory, the build its disk
    _stop_on_term()
    if args.command == "refresh":
        from operating.pg import view_state
        heartbeat = _heartbeat(dsn)
        try:
            got = refresh(dsn, force=args.force, heartbeat=heartbeat)
        finally:
            heartbeat.stop()
        print(json.dumps(got, default=str))
        return 1 if got.get("all_failed") else 0
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as meta:   # one build at a time, as refresh
        if not meta.execute("SELECT pg_try_advisory_lock(%s)", (REFRESH_LOCK,)).fetchone()[0]:
            print(json.dumps({"skipped": "another refresh is running"}))
            return 0
        try:
            sweep_leftovers(meta)
            for got in build(dsn, args.view):
                print(json.dumps(got))
        finally:
            meta.execute("SELECT pg_advisory_unlock(%s)", (REFRESH_LOCK,))
    return 0


def _heartbeat(dsn: str) -> Any:
    """The refresh's ``view_state.Heartbeat``: progress = the newest statement start or end
    of the run's own sessions (they carry the unit's ``PGAPPNAME``); each beat stamps the
    current step's views. Its own connection (the run's connections are busy)."""
    import psycopg
    from operating.pg import view_state
    app = os.environ.get("PGAPPNAME") or "business_views"
    state: dict[str, Any] = {}

    def conn() -> Any:
        if state.get("c") is None or state["c"].closed:
            state["c"] = psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                                         application_name=f"{app}-heartbeat")
        return state["c"]

    def progress() -> Any:
        return conn().execute(view_state.PROGRESS_SQL, (app,)).fetchone()[0]

    def on_beat(views: Sequence[str], phase: str) -> None:
        if view_state.ready(conn(), SCHEMA):
            view_state.beat(conn(), SCHEMA, [v for v in views if v in VIEWS], phase)
    return view_state.Heartbeat(progress, on_beat)


def _stop_on_term() -> None:
    """SIGTERM (the unit's time limit, a stop) unwinds like an error: the view being built
    drops its ``_next`` tables and the views already swapped in stay (each its own
    commit); the next run starts with what is still missing."""
    import signal

    def stop(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)


if __name__ == "__main__":
    # run as the package module, so the supply views register in the one VIEWS
    from operating.pg import business_views as _package
    sys.exit(_package.main())

