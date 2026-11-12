"""Value markings of raw source rows: what a reader may see of a row a source read serves
(``plx.read``, the page source view ``/api/source``), by the rules ``plx.object`` applies
to the properties projected from it.

A raw row is the source's whole record: every column, not only those a curated type
reads. So a column's markings (``relation_markings``) are the union of

* every property that reads it, in every object type bound to the relation: curated and
  generated, including a generated raw type a curated binding shadows (it leaves the
  compiled schema, ``CompiledSchema.shadowed``, yet its columns are still in the raw row:
  the Portal PO's ``subtotal`` is marked there and read by no curated property);
* for a column no type reads, the name rules of ``schema/overrides/markings.json``
  (money-like names, the scoped ``purchasing_prices`` replacement and the declared
  exceptions), as the generator marks a generated column, and the declared money
  columns (``money.json``);
* ``relation_markings`` columns of ``markings.json``.

Money by what a column holds, decided per column, never per cell. A finance-sensitive
relation (a Portal relation of the purchasing, orders or customers domain of the reporting
catalog: purchasing, sales, AP, AR and GL; a BC entity set that carries a money marking)
keeps its figures behind ``finance`` whatever their names. There a column no curated
property reads, that no marking covers and that is no key is finance when it holds
amounts: on the Portal, when the relation's value profile (``ontology.money_profile``:
the committed ``raw_money_profile.json`` and the one every projection build writes into
its state directory, read by a reader of that state, ``state_dirs``; per relation the
newer decides) finds it mostly
numeric with fractional figures (at least 0.5 % of them: Sage's ``2210.400`` makes its
``2225`` money too; FileMaker's ``1.0`` is whole, and so is an invoice number column with
a stray ``12.5``); in BC, when $metadata types it decimal. A column the profile finds
whole (an identifier, a compact date, a flag, a count), a code (an encoded reference such
as FileMaker container data, figures of 1e12 and more), text, or a measure that is not
money (``NOT_MONEY``: a quantity, weight, rate, percent, factor, lead time...) stays
visible, on every row alike; so does a column a curated property reads unmarked, as
``plx.object`` shows it. A column the profile does not list or found empty is finance
unless its declared type holds no figure (a text, date, identifier or JSON column). A
scoped marking replaces ``finance`` on its listed columns, as it does for the name rules.
The decision is cached per relation (``relation_markings``).

Two guards follow the data between profiles. A column profiled whole that serves a
fractional figure, or profiled code or text that serves a figure written as an amount, is
withheld for that read and the profile is stale (``stale``; the next build profiles
again; ``money_profile.check`` fails on an old profile; the truth check counts both). A Sage
optional field (``value``, ``defval``) is withheld on each row whose own ``decimals``
declare an amount (a free-shipping minimum in dollars, ``SHPMINDOL``).

Fail closed. A relation that carries a money marking (``finance`` or
``purchasing_prices``) on its row or any column, or is finance-sensitive, is a money
relation: there a column whose status is unknown (no type reads it and no source catalog
names it: a field the source added after the schema was generated) carries ``finance``.
A row whose relation carries a type marking (every type bound to it is marked) is
withheld whole from a reader lacking it.

Twins. At read time (``mark_row``), a column the reader may see whose value, or any
numeric value inside it (a JSON column), equals a withheld column's figure on the same
row (a fractional amount, or a whole one of 100 or more: ``_figure``) is withheld too:
the same figure under an unmarked name (BC ``totalAmountExcludingTax`` beside the marked
``totalAmountIncludingTax`` when tax is 0, a total copied into ``custom_fields``) never
shows what the marking withholds. Key columns are never twins (the citation names them
already), nor is a column a marking the reader holds covers (shown by right).

Known limits. A twin of a whole figure under 100 is not matched, nor is a figure written
inside free text (``total 48213.77`` in a note); an unmarked column equal to a figure by
chance (a quantity equal to a price) is withheld with it. ``plx.read``'s digest of a row
covers its withheld cells (the case digest a claim and a case record are checked
against); reproducing it needs every withheld cell guessed at once. Document text (a PDF,
a mail body) is gated per document, not per value.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

# Source families of a raw read: the Portal mirror (``api://``) and Business Central
# (``bc://``, live or captured), as the schema's bindings name them.
PORTAL = "portal_api"
BC = "bc"
MONEY_MARKINGS = frozenset({"finance", "purchasing_prices"})
# The marking a column of unknown status on a money relation carries: the strictest
# money marking (every finance holder holds purchasing_prices, store_ddl.MARKING_HELD_WITH).
UNKNOWN_MONEY = frozenset({"finance"})
# BC $metadata types a money name rule can read (store_ddl value types).
_EDM_TYPES = {"Edm.Decimal": "decimal", "Edm.Double": "decimal", "Edm.Single": "decimal",
              "Edm.Int16": "integer", "Edm.Int32": "integer", "Edm.Int64": "integer",
              "Edm.Byte": "integer", "Edm.String": "text"}
# Portal reporting-catalog domains whose relations are finance-sensitive (module doc): the
# catalog files Sage's GL with AP under purchasing and AR under customers.
SENSITIVE_DOMAINS = frozenset({"purchasing", "orders", "customers"})
# A measure that is not money, by the column's snake_case name: a decimal column so named
# stays visible on a finance-sensitive relation. Anything else decimal there is money.
NOT_MONEY = re.compile(r"qty|quantit|weight|wght|uom|conv|factor|percent|pct|rate|ratio"
                       r"|decml|precision|days|count|cnt|seq|level|latitude|longitude"
                       r"|width|height|length|depth|volume|cube|inventory|period|year"
                       r"|lead_?time|cnv|unitcon|ordered|received|shipped|backord")
PROFILE = Path(__file__).with_name("raw_money_profile.json")
_NUMBER = re.compile(r"\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\s*")
_MAX_LEAVES = 400   # numeric values read inside one JSON column (bounded work per row)


def snake(name: str) -> str:
    """A column name as the name rules read it (``ontology.generator.build.snake``)."""
    from ontology.generator.build import snake as generator_snake
    return generator_snake(name)


@dataclass(frozen=True)
class RelationMarkings:
    """The markings of one relation's raw rows. ``row``: a reader lacking any of them
    gets no row. ``columns``: every column whose status is known, with its markings
    (empty: unmarked). ``unknown``: the markings of a column not in ``columns``.
    ``keys``: the relation's key columns (never twins). ``sensitive``: a
    finance-sensitive relation (module doc). ``tripwire``: column -> the kind the profile
    found it (whole, code, text): a cell that looks like money there is withheld for that
    read and the profile is stale (``stale``, ``_trips``). ``typed_by_row``: a value column -> the
    column of its row declaring its decimals (Sage optional fields): withheld where that
    declares decimals."""

    row: frozenset[str]
    columns: Mapping[str, frozenset[str]]
    unknown: frozenset[str]
    keys: frozenset[str] = field(default_factory=frozenset)
    sensitive: bool = False
    tripwire: Mapping[str, str] = field(default_factory=dict)
    typed_by_row: Mapping[str, str] = field(default_factory=dict)

    def of(self, column: str) -> frozenset[str]:
        got = self.columns.get(column)
        return self.unknown if got is None else got

    @property
    def money(self) -> bool:
        return self.sensitive or bool(
            (self.row | self.unknown | frozenset().union(*self.columns.values()))
            & MONEY_MARKINGS)


# --------------------------------------------------------------------------- the rules


@functools.lru_cache(maxsize=1)
def _overrides() -> Any:
    from ontology.generator.overrides import load_overrides
    return load_overrides()


def _rule_markings(source: str, relation: str, column: str, value_type: str | None
                   ) -> frozenset[str]:
    """The markings the generator gives a raw column by its name (``markings.json``
    name rules, scoped replacements and exceptions; ``money.json`` declared money
    columns), for a column no type describes. ``value_type`` None: unknown, every rule
    reads it (fail closed)."""
    overrides = _overrides()
    family = "portal" if source == PORTAL else source
    if column in overrides.money_of(family).get(relation, {}):
        return frozenset(overrides.raw_scoped_marking(m, source, relation, column)
                         for m in overrides.money_markings)
    out: set[str] = set()
    for rule in overrides.name_rules:
        if value_type is not None and value_type not in rule.value_types:
            continue
        if not rule.reads(snake(column)) or (relation, column) in rule.exceptions:
            continue
        out.add(overrides.raw_scoped_marking(rule.marking, source, relation, column))
    return frozenset(out)


def _relation_extra(source: str, relation: str) -> tuple[frozenset[str],
                                                         dict[str, frozenset[str]]]:
    row, columns = _overrides().relation_markings.get((source, relation), ((), {}))
    return frozenset(row), {c: frozenset(m) for c, m in columns.items()}


@functools.lru_cache(maxsize=4)
def _generated_types(directory: str) -> tuple[Mapping[str, Any], ...]:
    """Every generated raw type as written (``schema/generated/*.json``), shadowed ones
    included."""
    out = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.extend(t for t in doc.get("object_types") or () if isinstance(t, Mapping))
    return tuple(out)


@functools.lru_cache(maxsize=4)
def _catalog_domains(meta: str) -> Mapping[str, str]:
    """Portal relation -> its reporting-catalog domain (``portal_catalog.json``)."""
    try:
        portal = json.loads((Path(meta) / "portal_catalog.json").read_text(encoding="utf-8"))
        return {str(r["name"]): str(r.get("domain") or "") for r in portal.get("relations") or ()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


PROFILE_KINDS = ("money", "whole", "code", "ident", "text", "empty")
# A state directory's own profile (``STATE_PROFILE`` in it), written by every projection
# build (``operating.host.build_projection``). A reader passes its own state directories
# (``state_dirs``: its case runtime's) to every call; none: the committed profile only.
STATE_PROFILE = "raw-money-profile.json"


def _dirs(state_dirs: Iterable[str | Path] | None) -> tuple[str, ...]:
    return tuple(sorted({str(Path(d)) for d in state_dirs or () if d}))


def state_dirs_of(runtime: Any) -> tuple[str, ...]:
    """The state directory a case runtime reads (``CaseRuntime.state_dir``), as the
    ``state_dirs`` of the marking calls made for it; none when it names none."""
    state_dir = getattr(runtime, "state_dir", None)
    return _dirs([state_dir]) if isinstance(state_dir, (str, Path)) else ()


@functools.lru_cache(maxsize=8)
def _profile_file(path: str, stamp: tuple) -> tuple[str, Mapping[str, Mapping[str, str]]]:
    """(its ``profiled_at``, relation -> {column: kind}) of one profile file; empty when
    missing or unreadable. ``stamp``: its (mtime, size), so a rewritten file is read anew."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", {}
    out: dict[str, dict[str, str]] = {}
    for relation, kinds in (doc.get("relations") or {}).items():
        if isinstance(kinds, Mapping):
            out[str(relation)] = {str(c): kind for kind in PROFILE_KINDS
                                  for c in kinds.get(kind) or ()}
    return str(doc.get("profiled_at") or ""), out


def _stamp(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return ()


def _files(state_dirs: tuple[str, ...]) -> list[Path]:
    return [PROFILE] + [Path(d) / STATE_PROFILE for d in state_dirs]


def _profiles(state_dirs: tuple[str, ...]) -> list[tuple[str, Mapping[str, Mapping[str, str]]]]:
    return [_profile_file(str(f), _stamp(f)) for f in _files(state_dirs) if _stamp(f)]


def profile_stamp(state_dirs: Iterable[str | Path] | None = None) -> tuple:
    """What the profiles a reader of ``state_dirs`` reads now are (paths and stamps): part
    of the cache key."""
    return tuple((str(f), _stamp(f)) for f in _files(_dirs(state_dirs)))


def _profile(relation: str, state_dirs: tuple[str, ...] = ()) -> Mapping[str, str]:
    """{column: kind} of ``relation`` in the newest profile that profiles it, of the
    committed one and those of ``state_dirs``."""
    best: tuple[str, Mapping[str, str]] | None = None
    for profiled_at, relations in _profiles(state_dirs):
        if relation in relations and (best is None or profiled_at > best[0]):
            best = (profiled_at, relations[relation])
    return best[1] if best else {}


def _not_money(column: str) -> bool:
    return bool(NOT_MONEY.search(snake(column)))


@functools.lru_cache(maxsize=4)
def _catalog_columns(meta: str) -> Mapping[tuple[str, str], Mapping[str, str | None]]:
    """(source, relation) -> {column: value type or None} the pinned source catalogs name:
    the Portal reporting catalog and BC's $metadata."""
    out: dict[tuple[str, str], dict[str, str | None]] = {}
    try:
        portal = json.loads((Path(meta) / "portal_catalog.json").read_text(encoding="utf-8"))
        for rel in portal.get("relations") or ():
            out[(PORTAL, rel["name"])] = {str(c): None for c in rel.get("columns") or ()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        bc = json.loads((Path(meta) / "bc_metadata.json").read_text(encoding="utf-8"))
        for name, spec in (bc.get("entity_sets") or {}).items():
            out[(BC, name)] = {str(p["name"]): _EDM_TYPES.get(str(p.get("type")), "other")
                               for p in spec.get("properties") or ()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return out


def _types_bound(compiled: Any, source: str, relation: str, generated: Iterable[Mapping[str, Any]]
                 ) -> list[tuple[Mapping[str, Any], set[str]]]:
    """(type, the ids of its bindings to the relation) of every type bound to it: the
    compiled ones and the generated raw ones (shadowed included)."""
    seen: set[str] = set()
    out = []
    for type_def in list(compiled.object_types.values()) + list(generated):
        bound = {b.get("id") for b in type_def.get("bindings") or ()
                 if b.get("source") == source and b.get("relation") == relation}
        if bound and type_def.get("id") not in seen:
            seen.add(type_def.get("id"))
            out.append((type_def, bound))
    return out


@functools.lru_cache(maxsize=4096)
def _markings_for(digest: str, profiles: tuple, source: str, relation: str,
                  state_dirs: tuple[str, ...]) -> RelationMarkings:
    from ontology import schema as ontology_schema
    compiled = ontology_schema.load_schema()
    return _compute(compiled, source, relation, str(ontology_schema.GENERATED_DIR),
                    str(ontology_schema.META_DIR), state_dirs)


def _compute(compiled: Any, source: str, relation: str, generated_dir: str, meta_dir: str,
             state_dirs: tuple[str, ...] = ()) -> RelationMarkings:
    row: set[str] = set()
    described: dict[str, set[str]] = {}
    curated: set[str] = set()          # columns a curated property reads
    types: dict[str, set[str]] = {}    # column -> the value types properties read it as
    keys: set[str] = set()
    origin = getattr(compiled, "origin_of_type", {}) or {}
    for type_def, bound in _types_bound(compiled, source, relation,
                                        _generated_types(generated_dir)):
        row |= set(type_def.get("markings") or ())
        generated = origin.get(type_def.get("id"), "generated") == "generated"
        for binding in type_def.get("bindings") or ():
            if binding.get("id") in bound:
                keys |= {str(k) for k in binding.get("key") or () if isinstance(k, str)}
        for prop in type_def.get("properties") or ():
            marks = set(prop.get("markings") or ())
            for src in prop.get("sources") or ():
                if src.get("binding") in bound and isinstance(src.get("path"), str):
                    column = src["path"].split(".", 1)[0]
                    described.setdefault(column, set()).update(marks)
                    if not generated:
                        curated.add(column)
                    elif "." not in src["path"] and prop.get("value_type"):
                        types.setdefault(column, set()).add(str(prop["value_type"]))
    extra_row, extra_columns = _relation_extra(source, relation)
    row |= extra_row
    columns = {c: frozenset(m) for c, m in described.items()}
    catalog = _catalog_columns(meta_dir).get((source, relation), {})
    for column, value_type in catalog.items():
        if column not in columns:
            columns[column] = _rule_markings(source, relation, column, value_type)
        if value_type is not None:
            types.setdefault(column, set()).add(value_type)
    for column, marks in extra_columns.items():
        columns[column] = columns.get(column, frozenset()) | marks
    money = bool((row | frozenset().union(*columns.values())) & MONEY_MARKINGS)
    sensitive = (_catalog_domains(meta_dir).get(relation) in SENSITIVE_DOMAINS
                 if source == PORTAL else money)
    tripwire: dict[str, str] = {}
    typed_by_row: dict[str, str] = {}
    if sensitive:
        overrides = _overrides()
        # Sage optional fields: a row declares its own value's type (``decimals``): a value
        # with decimals is an amount (a free-shipping minimum in dollars), else a code or
        # a count. The value column is withheld on the rows whose type has decimals.
        decimals = next((c for c in set(columns) | set(catalog) if c == "decimals"), None)
        if decimals is not None:
            typed_by_row = {c: decimals for c in ("value", "defval")
                            if c in set(columns) | set(catalog) and not columns.get(c)}
        profiled = _profile(relation, state_dirs) if source == PORTAL else {}
        for column in sorted(set(columns) | set(catalog) | set(profiled)):
            if columns.get(column) or column in curated or column in keys \
                    or _not_money(column):
                continue
            if source == PORTAL:
                kind = profiled.get(column)
                if kind in ("whole", "code", "ident", "text"):
                    tripwire[column] = kind   # visible: unless a cell looks like money
                    continue   # an identifier, a date, a flag, a count, a code, a word
                if kind != "money" and types.get(column) and not (
                        types[column] & {"decimal", "money", "integer"}):
                    continue   # never filled when profiled: its declared type holds no figure
            elif "decimal" not in types.get(column, set()):
                continue       # BC: $metadata types its amounts decimal
            columns[column] = frozenset({overrides.raw_scoped_marking(
                "finance", source, relation, column)})
    return RelationMarkings(frozenset(row), columns,
                            UNKNOWN_MONEY if money or sensitive else frozenset(),
                            frozenset(keys), sensitive,
                            {c: k for c, k in tripwire.items() if c not in keys},
                            dict(typed_by_row))


# The committed schema's digest, re-read at most this often: ``load_schema`` re-reads and
# hashes every schema file on each call (tens of milliseconds), which a read of a wide row
# must not pay per column. Schema files change with a deploy, which restarts the reader.
_DIGEST_SECONDS = 60.0
_digest_seen: list[Any] = [None, 0.0]


# state_dirs -> (``profile_stamp``, when read): re-read at most every ``_DIGEST_SECONDS``.
_profile_seen_at: dict[tuple[str, ...], tuple[tuple, float]] = {}


def _profile_seen(state_dirs: tuple[str, ...]) -> tuple:
    import time
    now = time.monotonic()
    seen = _profile_seen_at.get(state_dirs)
    if seen is None or now - seen[1] > _DIGEST_SECONDS:
        seen = (profile_stamp(state_dirs), now)
        _profile_seen_at[state_dirs] = seen
    return seen[0]


def _committed_digest() -> str:
    import time
    from ontology import schema as ontology_schema
    now = time.monotonic()
    if _digest_seen[0] is None or now - _digest_seen[1] > _DIGEST_SECONDS:
        _digest_seen[:] = [ontology_schema.load_schema().digest, now]
    return _digest_seen[0]


def relation_markings(source: str, relation: str, schema: Any = None,
                      state_dirs: Iterable[str | Path] | None = None) -> RelationMarkings:
    """The markings of ``relation``'s raw rows in ``source`` (module doc). ``schema``: a
    compiled schema (default the committed one, cached per schema digest and profiles).
    ``state_dirs``: the reader's state directories, whose own profiles count too."""
    dirs = _dirs(state_dirs)
    if schema is None:
        return _markings_for(_committed_digest(), _profile_seen(dirs), source, relation, dirs)
    from ontology import schema as ontology_schema
    return _compute(schema, source, relation, str(ontology_schema.GENERATED_DIR),
                    str(ontology_schema.META_DIR), dirs)


def _column_of(found: RelationMarkings, source: str, relation: str, column: str
               ) -> frozenset[str]:
    if column in found.columns:
        return found.columns[column]
    return _undescribed(source, relation, column) | found.unknown


@functools.lru_cache(maxsize=8192)
def _undescribed(source: str, relation: str, column: str) -> frozenset[str]:
    return _rule_markings(source, relation, column, None)


def column_markings(source: str, relation: str, column: str, schema: Any = None,
                    state_dirs: Iterable[str | Path] | None = None) -> frozenset[str]:
    """One column's markings, an undescribed column read by the name rules first (its
    value type unknown) and then the relation's fail-closed default."""
    return _column_of(relation_markings(source, relation, schema, state_dirs), source,
                      relation, column)


# --------------------------------------------------------------------------- a row


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        try:
            return Decimal(repr(value))
        except InvalidOperation:
            return None
    if isinstance(value, str) and len(value) <= 64 and _NUMBER.fullmatch(value):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None


def _figure(number: Decimal) -> bool:
    """Whether a withheld number is a figure a twin could repeat: a small whole number
    (an exchange rate of 1, a 5 % discount, a count) recurs by chance in unrelated
    columns, so only a fractional amount or a whole one of 100 or more is matched."""
    try:
        return number != number.to_integral_value() or abs(number) >= 100
    except ArithmeticError:   # an exponent past the context's range: no small count
        return True


def numbers(value: Any) -> set[Decimal]:
    """Every non-zero numeric value in ``value``: itself, a numeric string, and the
    numbers inside a JSON object, array or JSON text (bounded: ``numbers_read``)."""
    return numbers_read(value)[0]


def numbers_read(value: Any) -> tuple[set[Decimal], bool]:
    """``numbers`` and whether every value in ``value`` was read: False once the bound
    (``_MAX_LEAVES`` values, a JSON text over 200,000 characters) left some unread, so a
    number past it is not known either way (DOC-005)."""
    out: set[Decimal] = set()
    complete = True
    stack, seen = [value], 0
    while stack and seen < _MAX_LEAVES:
        item = stack.pop()
        seen += 1
        if isinstance(item, Mapping):
            stack.extend(item.values())
            continue
        if isinstance(item, (list, tuple)):
            stack.extend(item)
            continue
        if isinstance(item, str) and item[:1] in ("{", "["):
            if len(item) > 200_000:
                complete = False   # too long to read: what it holds is unknown
                continue
            try:
                stack.append(json.loads(item))
                continue
            except ValueError:
                pass
        number = _number(item)
        if number is not None and number.is_finite() and number != 0:
            out.add(number)   # equal values hash alike (5120.000 is 5120); never normalised:
            # a huge exponent overflows the context
    return out, complete and not stack


def _positive(value: Any) -> bool:
    number = _number(value)
    return number is not None and number > 0


def _trips(kind: str, value: Any) -> bool:
    """Whether a cell of a column the profile found ``kind`` looks like money: a whole
    column's any fraction; a code's or a text's a fractional figure written as an amount
    (``money_profile.money_shaped``: under 1e12, at most six places), an identifier
    column's such a figure with two places or more; never encodings or words."""
    if kind == "whole":
        return _fractional(value)
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        return False
    from ontology.money_profile import decimals, money_shaped
    number = _number(value)
    if number is None or not number.is_finite() or not money_shaped(number):
        return False
    # An identifier column's "4501.2" is a PO number; "12.50" is written as an amount.
    return kind != "ident" or decimals(number) >= 2


def _fractional(value: Any) -> bool:
    """A figure that is not whole (``12.5``; FileMaker's ``1.0`` and ``2225`` are whole)."""
    number = _number(value)
    if number is None or not number.is_finite():
        return False
    try:
        return number != number.to_integral_value()
    except ArithmeticError:
        return False


# (relation, column) a tripwire fired on in this process: its profile is stale until the
# next build profiles the mirror again (``check`` reports it; the read withheld the cell).
STALE: set[tuple[str, str]] = set()


def _mark_stale(relation: str, column: str) -> None:
    if (relation, column) not in STALE:
        STALE.add((relation, column))
        import sys
        print(f"raw_markings: profile stale: {relation}.{column} profiled whole served a "
              "fractional figure (withheld)", file=sys.stderr)


def stale() -> list[tuple[str, str]]:
    """The (relation, column) whose profile a read found stale in this process."""
    return sorted(STALE)


def withheld_columns(source: str, relation: str, row: Mapping[str, Any],
                     held: Iterable[str], schema: Any = None,
                     state_dirs: Iterable[str | Path] | None = None
                     ) -> frozenset[str] | None:
    """The columns of ``row`` a reader holding ``held`` may not see (marked ones and
    their twins), or None when the reader may not see the row at all. ``state_dirs``: the
    reader's (``relation_markings``)."""
    held = frozenset(held)
    found = relation_markings(source, relation, schema, state_dirs)
    if not found.row <= held:
        return None
    hidden = {c for c in row if not _column_of(found, source, relation, str(c)) <= held}
    if not UNKNOWN_MONEY <= held:
        for column, declares in found.typed_by_row.items():
            if column in row and _positive(row.get(declares)):
                hidden.add(column)
        for column, kind in found.tripwire.items():
            if column in row and column not in hidden and _trips(kind, row[column]):
                hidden.add(column)
                _mark_stale(relation, column)
    if not hidden:
        return frozenset()
    secret: set[Decimal] = set()
    # whether every withheld value was read: past the bound a withheld figure is unknown,
    # so any figure elsewhere may repeat it (DOC-005)
    whole = True
    for column in hidden:
        found_numbers, complete = numbers_read(row[column])
        secret |= {n for n in found_numbers if _figure(n)}
        whole = whole and complete
    if secret or not whole:
        for column, value in row.items():
            # A column a marking covers that the reader holds is shown by right, even
            # when it repeats a figure another marking withholds (a purchasing reader's
            # PO line amount equal to its tax-inclusive finance twin); only an unmarked
            # column is a twin.
            marks = _column_of(found, source, relation, str(column))
            if column in hidden or column in found.keys or (marks and marks <= held):
                continue
            shown, complete = numbers_read(value)
            # a column read only in part may hold a withheld figure past the bound
            if shown & secret or not complete or (
                    not whole and any(_figure(n) for n in shown)):
                hidden.add(column)
    return frozenset(hidden)


def mark_row(source: str, relation: str, row: Mapping[str, Any], held: Iterable[str],
             schema: Any = None, state_dirs: Iterable[str | Path] | None = None
             ) -> tuple[dict[str, Any] | None, frozenset[str]]:
    """(``row`` as a reader holding ``held`` may see it, the columns withheld); (None,
    empty) when a row marking is lacked."""
    hidden = withheld_columns(source, relation, row, held, schema, state_dirs)
    if hidden is None:
        return None, frozenset()
    return {c: v for c, v in row.items() if c not in hidden}, hidden


def mark_view(handle: str, view: Any, held: Iterable[str],
              state_dirs: Iterable[str | Path] | None = None) -> Any:
    """A cited record's view as the case runtime gives it (``CaseRuntime.resolve_citation``
    and ``claim_row``: a mirror or BC capture ``row`` or ``rows``, a live BC ``record``
    whose BC columns are its ``fields``, a moved revision under ``revision``) as a reader
    holding ``held`` may see it: each withheld column (``withheld_columns``) left out and
    named in ``withheld_fields`` (``row.<column>``, ``rows.<n>.<column>``,
    ``record.fields.<column>``), a live record's ``content_sha256`` dropped once one is.
    None when a row marking is lacked, or the handle is a row citation whose relation it
    does not name (fail closed). Any other citation's view is returned as it is."""
    if not isinstance(handle, str) or not handle.startswith(("api://", "bc://")) \
            or not isinstance(view, Mapping):
        return view
    found = handle_relation(handle)
    if found is None:
        return None
    source, relation = found
    held = frozenset(held)
    if not relation_markings(source, relation, None, state_dirs).row <= held:
        return None
    out = dict(view)
    hidden: list[str] = []
    if isinstance(view.get("revision"), Mapping):
        revision = mark_view(handle, view["revision"], held, state_dirs)
        if revision is None:
            return None
        out["revision"] = revision
    for key, rows in (("row", [view.get("row")]), ("rows", view.get("rows"))):
        if key not in view or not isinstance(rows, list):
            continue
        shown = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                shown.append(row)
                continue
            got, withheld = mark_row(source, relation, row, held, None, state_dirs)
            if got is None:
                return None
            prefix = "row" if key == "row" else f"rows.{index}"
            hidden += [f"{prefix}.{column}" for column in sorted(withheld)]
            shown.append(got)
        out[key] = shown[0] if key == "row" else shown
    record = view.get("record")
    if source == BC and isinstance(record, Mapping) and isinstance(record.get("fields"),
                                                                    Mapping):
        got, withheld = mark_row(source, relation, record["fields"], held, None, state_dirs)
        if got is None:
            return None
        if withheld:
            out["record"] = {k: v for k, v in {**record, "fields": got}.items()
                             if k != "content_sha256"}
            hidden += [f"record.fields.{column}" for column in sorted(withheld)]
    if hidden:
        out["withheld_fields"] = sorted(set(view.get("withheld_fields") or ()) | set(hidden))
    return out


def handle_relation(handle: str) -> tuple[str, str] | None:
    """(source, relation) a raw row citation reads: ``api://<relation>/...`` (the Portal
    mirror) or ``bc://<env>/<company>/<entitySet>/<id>...`` (live or captured BC); None
    for any other citation or a malformed one."""
    if not isinstance(handle, str):
        return None
    if handle.startswith("api://"):
        relation = handle[len("api://"):].split("/", 1)[0]
        return (PORTAL, relation) if relation else None
    if handle.startswith("bc://"):
        from integrations.live_source_guard import parse_handle
        try:
            (_env, _company, entity_set, _record), _version, _extra = parse_handle(
                handle, "bc", 4)
        except ValueError:
            return None
        return (BC, entity_set) if entity_set else None
    return None


__all__ = ["BC", "MONEY_MARKINGS", "NOT_MONEY", "PORTAL", "RelationMarkings",
           "SENSITIVE_DOMAINS", "column_markings", "handle_relation", "mark_row",
           "mark_view", "numbers", "relation_markings",
           "withheld_columns"]
