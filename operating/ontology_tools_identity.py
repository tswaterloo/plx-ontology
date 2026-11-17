"""Schema views and identifier identity for the ontology tools: the compiled
``SchemaView``, the identifier ``Grammar`` and its exact/scoped matching, query keys,
source and BC-company lineage, withheld text, gaps, errors and the ``_Match`` record."""

from __future__ import annotations

from dataclasses import dataclass, field
import functools
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from ontology.types import Actor
from operating.ontology_tools_policy import (_citation, _HEX64, _markings, _OTHER_TEXT_REASONS,
                                             _REASON_RANK, _SCHEME_SOURCE, _TIER_RANK,
                                             LINKED_REASON, ROOT_REASONS, SCOPE_EXCLUDED,
                                             SCOPE_FEATURE, SCOPE_FEATURES)

if TYPE_CHECKING:
    from operating.ontology_tools import SetRegistry


# --------------------------------------------------------------------------- schema

# What a find derives from a schema alone (its display properties, identifier carriers,
# the bindings a candidate is compared with), kept per schema: a SchemaView is frozen, every
# layer of a delta overlay shares its host's, and every session of one process loads its
# own view of the same compiled schema. Kept by the schema's content digest (a SHA-256 over
# its whole canonical form, ``ontology.schema.schema_digest``) so each new session reuses
# what an earlier one derived (measured 2026-11-07: a new session's first find spent 3 to
# 5 s deriving binding comparisons again); a schema without such a digest is kept per
# object.
_SCHEMA_MEMO: dict[Any, tuple[Any, dict[Any, Any]]] = {}
_SCHEMA_MEMO_LOCK = threading.Lock()
_SCHEMA_MEMO_KEPT = 32
_CONTENT_DIGEST = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


# Reader-scoped answers kept per store state on its reader (``OntologyTools._store_kept``).
_STORE_KEPT_LOCK = threading.Lock()
_STORE_KEPT_MAX = 4096


def _schema_key(schema: Any) -> tuple[Any, Any]:
    """(memo key, what a held entry must be): the content digest and itself, or the
    object's id and the object."""
    digest = getattr(schema, "digest", None)
    if isinstance(digest, str) and _CONTENT_DIGEST.match(digest):
        return ("digest", digest), digest
    return ("id", id(schema)), schema


def _same_owner(held: Any, owner: Any) -> bool:
    """A content digest matches by value (each session unpickles its own copy); an object
    kept by id matches only itself (an id can be reused after the object is freed)."""
    return held == owner if isinstance(owner, str) else held is owner


def _schema_memo(schema: Any, key: Any, build: Callable[[], Any]) -> Any:
    """``build()`` for ``schema`` and ``key``, built once per schema content (the caller
    never changes the value)."""
    memo_key, owner = _schema_key(schema)
    with _SCHEMA_MEMO_LOCK:
        held = _SCHEMA_MEMO.get(memo_key)
        if held is not None and _same_owner(held[0], owner) and key in held[1]:
            return held[1][key]
    value = build()
    with _SCHEMA_MEMO_LOCK:
        held = _SCHEMA_MEMO.get(memo_key)
        if held is None or not _same_owner(held[0], owner):
            if len(_SCHEMA_MEMO) >= _SCHEMA_MEMO_KEPT:
                _SCHEMA_MEMO.clear()
            held = _SCHEMA_MEMO[memo_key] = (owner, {})
        return held[1].setdefault(key, value)


@dataclass(frozen=True)
class SchemaView:
    """The parts of the compiled schema the tools read (``ontology/schema.py``
    ``CompiledSchema`` has the same attributes; ``from_compiled`` adapts it)."""

    object_types: Mapping[str, Mapping[str, Any]]
    link_types: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    event_types: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    action_types: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    digest: str = ""

    @classmethod
    def from_compiled(cls, compiled: Any) -> "SchemaView":
        return cls(dict(compiled.object_types), dict(compiled.link_types),
                   dict(compiled.event_types), dict(compiled.action_types),
                   str(compiled.digest))

    @classmethod
    def from_documents(cls, documents: Iterable[Mapping[str, Any]]) -> "SchemaView":
        """Index domain documents by id (no shadowing/validation: that is the
        loader's job); the digest is a SHA-256 of their canonical JSON."""
        docs = list(documents)
        view: dict[str, dict[str, Any]] = {"object_types": {}, "link_types": {},
                                           "event_types": {}, "action_types": {}}
        for doc in docs:
            for section in view:
                for item in doc.get(section, ()):
                    view[section][item["id"]] = item
        body = json.dumps(sorted((json.dumps(d, sort_keys=True) for d in docs)))
        return cls(digest=hashlib.sha256(body.encode()).hexdigest(), **view)

    @property
    def digest_hex(self) -> str | None:
        found = _HEX64.match(self.digest or "")
        return found.group(1) if found else None

    def display(self) -> dict[str, tuple[str, ...]]:
        return {tid: tuple(t.get("display", ())) for tid, t in self.object_types.items()}

    def identity_link_types(self) -> frozenset[str]:
        """The declared link types that are identities (``identity_link_type``)."""
        return frozenset(lid for lid, link in self.link_types.items()
                         if identity_link_type(link))


def identity_link_type(declared: Mapping[str, Any]) -> bool:
    """Whether a declared link type joins records of one thing held in two systems, the
    one identity rule of every tool (ER-06): its two ends are one object type and it
    declares a ``same_record`` binding. A stored key between two types (a ledger entry's
    receipt, ``ItemLedgerEntry.posted_by_receipt``) is a reference, whatever its binding;
    a stored link of an identity type is one whatever its binding (a person's review, a
    text match), its tier saying how far it is established."""
    end = declared.get("from")
    return end is not None and end == declared.get("to") and any(
        isinstance(b, Mapping) and b.get("kind") == "same_record"
        for b in declared.get("bindings") or ())


# --------------------------------------------------------------------------- grammar

class Grammar(Protocol):
    """The identifier grammar interface (``ontology/identifiers.py``)."""

    def kinds(self) -> tuple[str, ...]: ...
    def normalize(self, kind: str, value: Any) -> str | None: ...
    def find_identifiers(self, text: Any) -> list[Any]: ...
    def kind_spec(self, kind: str) -> Mapping[str, Any]: ...
    def exact_compare(self, kind: str | None, stored: Any, asked: Any) -> bool: ...


def exact_match(kind: str | None, matched_text: Any, asked: Any,
                grammar: Grammar | None, scoped: bool = False) -> bool:
    """Whether ``matched_text`` (a candidate's ``matched.raw`` text) is ``asked`` by the
    literal comparison the owning system makes for ``kind`` (identifiers.json
    ``exact_compare``, ``ontology.identifiers.exact_compare``: SO numbers trim and case;
    PO numbers trim, any case, no wildcard). plx.find ranks by it and ``find_root`` / the
    find eval / the case path root on it (``roots_exactly``): one rule. A grammar without
    ``exact_compare`` (or none) makes only trimmed, case-sensitive equality exact.

    A kind whose grammar declares ``identity_scope`` (a customer PO, unique per customer)
    is never exact by its text alone: the Portal matches it only with its scope
    (``resolveOrderByNumber``: no customer, no PO match; plx-customer-portal@40f7802
    order-identity.ts:125-149). It is exact only when ``scoped`` (plx.find established the
    matched record's one scope: ``identity_scopes``) and then by the literal trimmed text,
    case-sensitive: the owning system's normalised comparison needs the asker's scope,
    which a query does not carry, so a case or spacing variant stays a lead."""
    compare = getattr(grammar, "exact_compare", None)
    if scoped_kind(grammar, kind):
        if not scoped:
            return False
        kind = None  # the grammar compares an undeclared kind by its literal trimmed text
    if compare is None:
        return (isinstance(matched_text, str) and isinstance(asked, str)
                and bool(asked.strip()) and matched_text.strip() == asked.strip())
    return bool(compare(kind, matched_text, asked))


def scoped_kind(grammar: Grammar | None, kind: Any) -> bool:
    """Whether the grammar declares ``kind`` unique only within a scope
    (identifiers.json ``identity_scope``)."""
    if grammar is None or not isinstance(kind, str):
        return False
    try:
        return bool(grammar.kind_spec(kind).get("identity_scope"))
    except Exception:  # noqa: BLE001 - a kind the grammar does not declare is unscoped
        return False


def text_only_kind(grammar: Grammar | None, kind: Any) -> bool:
    """Whether every registry carrier of ``kind`` is unserved (identifiers.json
    ``carriers`` all ``served: false``, e.g. a Portal sourcing request number): no
    projected record holds it, so its value is only ever read from the text citing it."""
    if grammar is None or not isinstance(kind, str):
        return False
    try:
        carriers = [c for c in grammar.kind_spec(kind).get("carriers", ()) or ()
                    if isinstance(c, Mapping)]
    except Exception:  # noqa: BLE001 - a kind the grammar does not declare
        return False
    return bool(carriers) and all(c.get("served") is False for c in carriers)


def carrier_fields(grammar: Grammar | None) -> frozenset[tuple[str, str]]:
    """Every (relation, field) the registry declares a carrier of any kind
    (identifiers.json ``carriers``)."""
    try:
        kinds = sorted(grammar.kinds()) if grammar is not None else ()
    except Exception:  # noqa: BLE001 - a grammar without kinds declares no carrier
        return frozenset()
    return frozenset((carrier["relation"], carrier["field"]) for kind in kinds
                     for carrier in grammar.kind_spec(kind).get("carriers", ()) or ()
                     if isinstance(carrier, Mapping) and isinstance(carrier.get("relation"), str)
                     and isinstance(carrier.get("field"), str))


def scoped_carriers(grammar: Grammar | None) -> dict[tuple[str, str], str]:
    """(relation, field) -> the scoped kind whose registry carriers declare that field
    (identifiers.json ``carriers`` of a kind with ``identity_scope``). A field two scoped
    kinds declare maps to the first by name: either one applies the scope gate."""
    out: dict[tuple[str, str], str] = {}
    try:
        kinds = sorted(grammar.kinds()) if grammar is not None else ()
    except Exception:  # noqa: BLE001 - a grammar without kinds declares no carrier
        return out
    for kind in kinds:
        if not scoped_kind(grammar, kind):
            continue
        for carrier in grammar.kind_spec(kind).get("carriers", ()) or ():
            if (isinstance(carrier, Mapping) and isinstance(carrier.get("relation"), str)
                    and isinstance(carrier.get("field"), str)):
                out.setdefault((carrier["relation"], carrier["field"]), kind)
    return out


def roots_exactly(candidate: Mapping[str, Any], asked: Any, grammar: Grammar | None) -> bool:
    """The one root gate on one plx.find candidate (find_root, the find eval, the case
    path and plx.investigate, the last two through ``scoped_root_lead``): a match reason
    that may make a root (``ROOT_REASONS``) whose matched text is ``asked`` by
    ``exact_match``; a scoped kind only with the ``identity_scope`` feature plx.find gave
    the candidate. One explicit allowance: a ``linked_identifier`` candidate (fk-follow,
    asked with an ``object_type``) roots as the match it was reached from, by that
    match's text and scope feature (plx.find copies both), so asking for the order
    rather than the PO roots the same record or none. Anything else (``display_text``,
    ``filter_match``, any lead reason, an unknown one) is a lead."""
    matched = candidate.get("matched") if isinstance(candidate.get("matched"), Mapping) else {}
    raw = matched.get("raw") if isinstance(matched.get("raw"), Mapping) else {}
    scoped = any(isinstance(f, Mapping) and f.get("feature") == SCOPE_FEATURE
                 and f.get("agrees") is True for f in candidate.get("why_not") or ())
    return (candidate.get("reason") in (ROOT_REASONS | {LINKED_REASON})
            and exact_match(matched.get("identifier_kind"), raw.get("untrusted_text"),
                            asked, grammar, scoped))


def scoped_root_lead(candidate: Mapping[str, Any], asked: Any,
                     grammar: Grammar | None) -> str | None:
    """Why one plx.find candidate matched on a scoped kind (a customer PO) may not root a
    case or a plan on ``asked``, or None when it may (or its kind is not scoped). Derived
    from ``roots_exactly``, the one root gate: None exactly when it passes; else ``why``
    names the first failure: the failing scope feature (``no_scope_value``,
    ``identity_scope_ambiguous``, ...), ``normalized_only`` (a case or spacing variant),
    the reason plx.find gave a match the gate never roots (``display_text`` or
    ``filter_match`` on a carrier field, ``marked_form``: ``PO#PO-6000``), else
    ``no_scope_value``. The case path and plx.investigate refuse such a root as
    ``root_lead_only:<why>``: every entry point reaches this one gate through the kind
    plx.find resolved."""
    matched = candidate.get("matched") if isinstance(candidate.get("matched"), Mapping) else {}
    kind = matched.get("identifier_kind")
    if not scoped_kind(grammar, kind) or roots_exactly(candidate, asked, grammar):
        return None
    features = [f for f in candidate.get("why_not") or () if isinstance(f, Mapping)
                and f.get("feature") in SCOPE_FEATURES]
    failed = [f.get("feature") for f in features
              if not (f.get("feature") == SCOPE_FEATURE and f.get("agrees") is True)]
    raw = matched.get("raw") if isinstance(matched.get("raw"), Mapping) else {}
    reason = candidate.get("reason")
    if failed:
        return failed[0]
    if features and not exact_match(kind, raw.get("untrusted_text"), asked, grammar, True):
        return "normalized_only"
    if isinstance(reason, str) and reason not in ROOT_REASONS and reason != LINKED_REASON:
        return reason
    return "no_scope_value"


def identity_scopes(store: "StoreReader", grammar: Grammar | None,
                    relations: Mapping[str, Any], held: Iterable[str],
                    rows: Iterable[tuple[str, str, str, str, str]]
                    ) -> dict[tuple[str, str, str], tuple[tuple[str, ...] | None, str,
                                                           dict[str, Any] | None]]:
    """(object_id, l1_handle, field_path) -> (identity key, feature, scope citation) of
    each identifier row ``(object_id, l1_handle, field_path, kind, raw)`` of a kind the
    grammar scopes; other rows are left out. The scope is the carrier's ``scope_field``
    (identifiers.json, per relation and field: the registry, never a column named here)
    read from the same captured record, as the projection stores it for this reader
    (``held`` markings), qualified as the carrier declares (``identifiers.scope_rules``:
    ``within`` ``company`` prefixes the record's BC company from its capture handle, as
    two companies' customers can share a number). ``feature``: ``identity_scope`` (the
    key, ``identifiers.identity_key``: scope space, scope value, normalised value),
    ``no_scope_field`` (the carrier declares none), ``no_scope_value`` (the record holds
    none: the Portal skips the PO match without a customer), ``scope_value_withheld`` (a
    marking this reader lacks) or ``scope_status_excluded`` (the record's ``excludes``
    field holds an excluded value, read from the same captured cell: the Portal skips a
    cancelled order; an unreadable status excludes nothing); each of the last four names
    no identity."""
    held = frozenset(held)
    scope_of = getattr(grammar, "identity_scope", None)
    key_of = getattr(grammar, "identity_key", None)
    rules_of = getattr(grammar, "scope_rules", None)
    out: dict[tuple[str, str, str], tuple[tuple[str, ...] | None, str,
                                          dict[str, Any] | None]] = {}
    wanted: dict[tuple[str, str, str], tuple[str, str, str, str, Mapping[str, Any]]] = {}
    for object_id, handle, path, kind, raw in rows:
        if not scoped_kind(grammar, kind):
            continue
        ref = (object_id, handle, path)
        parts = object_id.split(":", 2)
        root, _, field_name = path.partition(".")
        relation = relations.get(parts[1]) if len(parts) == 3 else None
        found = None if scope_of is None or key_of is None or len(parts) < 3 else scope_of(
            kind, relation, field_name)
        if found is None:
            out[ref] = (None, "no_scope_field", None)
        else:
            rules = (rules_of(kind, relation, field_name) if rules_of else None) or {}
            wanted[ref] = (kind, raw, found[0], f"{root}.{found[1]}", rules)
    pairs = {(ref[1], want[3]) for ref, want in wanted.items()}
    excluded_paths = {ref: f"{ref[2].partition('.')[0]}.{want[4]['excludes'][0]}"
                      for ref, want in wanted.items() if want[4].get("excludes")}
    pairs |= {(ref[1], path) for ref, path in excluded_paths.items()}
    values: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    cells: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in store.values_by_citation(sorted(pairs)):
        values.setdefault((row["object_id"], row["l1_handle"], row["field_path"]), row)
        cells.setdefault((row["l1_handle"], row["field_path"]), []).append(row)
    for ref, (kind, raw, space, scope_path, rules) in wanted.items():
        row = values.get((ref[0], ref[1], scope_path))
        if ref in excluded_paths and any(
                cell["value"] is not None and set(_markings(cell["markings"])) <= held
                and str(cell["value"]).strip() in rules["excludes"][1]
                for cell in cells.get((ref[1], excluded_paths[ref]), ())):
            out[ref] = (None, SCOPE_EXCLUDED, None)
        elif row is None or row["value"] is None:
            out[ref] = (None, "no_scope_value", None)
        elif not set(_markings(row["markings"])) <= held:
            out[ref] = (None, "scope_value_withheld", None)
        else:
            value = str(row["value"]).strip()
            if rules.get("within") == "company":
                company = _handle_company(ref[1])
                value = f"{company}/{value}" if company and value else ""
            key = key_of(kind, raw, (space, value))
            out[ref] = ((tuple(key), SCOPE_FEATURE, _citation(row)) if key
                        else (None, "no_scope_value", None))
    return out


def _handle_company(handle: str) -> str | None:
    """``<environment>/<company-id>`` of a BC capture or live handle
    (``bc://<environment>/<company-id>/<entitySet>/<id>@...``, ontology/project_bc.py), or
    None for any other handle: BC numbers are unique only within a company."""
    scheme, sep, rest = handle.partition("://")
    parts = rest.rpartition("@")[0].split("/") if sep and scheme == "bc" else []
    return "/".join(parts[:2]) if len(parts) >= 4 and all(parts[:2]) else None


@dataclass(frozen=True)
class _Key:
    kind: str | None
    normalized: str
    whole: bool  # the whole query is this identifier (not a mention inside text)
    span: tuple[int, int] | None = None  # where a mention sits in the query (label included)
    # Another kind's reading of a labelled mention's value (the grammar's
    # ``secondary``, ``label_kind_conflict``): searched, but only ever a lead.
    secondary: bool = False


def _query_keys(grammar: Grammar | None, query: str, kind_filter: str | None) -> list[_Key]:
    """Typed keys in the query: the whole query normalized as each kind, then
    every identifier mention the grammar finds inside it. Kind order is the
    grammar's; nothing here names a kind."""
    keys: list[_Key] = []
    seen: set[tuple[str | None, str]] = set()
    if grammar is None:
        return keys
    wanted: tuple[str, ...] | None = None
    if kind_filter is not None:
        aliases: tuple[str, ...] = ()
        try:
            aliases = tuple(grammar.kind_spec(kind_filter).get("alias_kinds", ()))
        except Exception:  # noqa: BLE001 - unknown kind: only that kind is tried
            aliases = ()
        wanted = (kind_filter,) + aliases

    def add(kind: str, normalized: Any, whole: bool,
            span: tuple[int, int] | None = None, secondary: bool = False) -> None:
        if not isinstance(normalized, str) or not normalized:
            return
        if wanted is not None and kind not in wanted:
            return
        if (kind, normalized) not in seen:
            seen.add((kind, normalized))
            keys.append(_Key(kind, normalized, whole, span, secondary))
        elif not secondary:
            # A key read as a primary reading anywhere is never only a secondary one.
            for index, key in enumerate(keys):
                if (key.kind, key.normalized) == (kind, normalized) and key.secondary:
                    keys[index] = _Key(kind, normalized, whole, span)

    for kind in grammar.kinds():
        try:
            add(kind, grammar.normalize(kind, query), True)
        except Exception:  # noqa: BLE001 - a normalizer refusing a form is no match
            continue
    try:
        mentions = grammar.find_identifiers(query)
    except Exception:  # noqa: BLE001
        mentions = []
    for mention in mentions:
        start = getattr(mention, "label_start", getattr(mention, "start", None))
        end = getattr(mention, "end", None)
        span = (start, end) if isinstance(start, int) and isinstance(end, int) else None
        kind = getattr(mention, "kind", "")
        add(kind, getattr(mention, "normalized", None), False, span)
        for other in getattr(mention, "secondary", ()) or ():
            if getattr(other, "label_kind_conflict", False):
                # A reading of a kind the label's kind aliases ("Business Central
                # document PO100192" read as a Petra PO) agrees with the label.
                add(getattr(other, "kind", ""), getattr(other, "normalized", None), False,
                    span, secondary=getattr(other, "kind", "") not in _alias_related(
                        grammar, kind))
    return keys


def _alias_related(grammar: Grammar, kind: str) -> set[str]:
    """The kinds ``kind`` aliases and the kinds that alias it (``search_keys``' set)."""
    related: set[str] = set()
    for other in grammar.kinds():
        try:
            if other == kind:
                related.update(grammar.kind_spec(kind).get("alias_kinds", ()))
            elif kind in grammar.kind_spec(other).get("alias_kinds", ()):
                related.add(other)
        except Exception:  # noqa: BLE001 - a kind the grammar cannot describe
            continue
    return related


def _text_tokens(query: str, keys: Sequence[_Key], *, limit: int | None = 6) -> list[str]:
    """Words of the query outside identifier mentions (3+ letters or digits each,
    at most ``limit``, None: all), all of which a display value must contain."""
    chars = list(query)
    for key in keys:
        if key.span:
            for index in range(max(0, key.span[0]), min(len(chars), key.span[1])):
                chars[index] = " "
    words = re.findall(r"[^\W_]{3,}", "".join(chars))
    return words if limit is None else words[:limit]


# plx.find's structured leads (``OntologyTools._structured_leads``): the sources whose
# records are a system of record's own rows (the Portal's reporting API carries the
# Portal, FileMaker and Sage; Business Central; Mission Control), never documents; how
# many leads one find returns; the shortest query word compared.
RECORD_SOURCES = frozenset({"portal_api", "bc", "mission_control"})
STRUCTURED_LEADS = 5
STRUCTURED_WORD_MIN = 4
# A value list a property description names: "(Released, Quarantine, Rejected)" or
# "INTAKE/RELEASED/QUARANTINE".
_VALUE_GROUP = re.compile(r"\(([^()]{3,300})\)")
_SLASH_VALUES = re.compile(r"\b[A-Za-z][A-Za-z ]{0,23}(?:/[A-Za-z][A-Za-z ]{0,23}){2,}\b")


def _word_stem(word: str) -> str:
    """``word`` folded with one common inflection cut ("quarantined" and "quarantine",
    "rejected" and "rejecting" compare equal), never below four letters."""
    folded = word.casefold()
    for suffix in ("ing", "ed", "es", "s", "e"):
        if folded.endswith(suffix) and len(folded) - len(suffix) >= 4:
            return folded[:-len(suffix)]
    return folded


def _declared_values(prop: Mapping[str, Any]) -> list[str]:
    """The values a property declares: its ``enum_values``, else each value list its
    description names (three or more short items, comma or slash separated)."""
    declared = prop.get("enum_values")
    if isinstance(declared, list):
        return [str(v) for v in declared if isinstance(v, (str, int))
                and not isinstance(v, bool)]
    text = str(prop.get("description") or "")
    out: list[str] = []
    for group in _VALUE_GROUP.findall(text):
        parts = [part.strip() for part in group.split(",")]
        if len(parts) >= 3 and all(0 < len(part) <= 24 and len(part.split()) <= 2
                                   for part in parts):
            out += parts
    for group in _SLASH_VALUES.findall(text):
        out += [part.strip() for part in group.split("/") if part.strip()]
    return out


def _literal_forms(query: str) -> list[str]:
    stripped = query.strip()
    collapsed = re.sub(r"\s+", "", stripped)
    out: list[str] = []
    for form in (stripped, stripped.upper(), collapsed, collapsed.upper()):
        if form and form not in out:
            out.append(form)
    return out


# --------------------------------------------------------------------------- helpers

_BC_HANDLE = re.compile(r"bc://([^/]+)/([0-9A-Fa-f-]{36})/")


@functools.lru_cache(maxsize=1)
def _bc_company_pins() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    from integrations.bc_read import load_company_books, load_company_scope
    try:
        return load_company_books(), load_company_scope()[1]
    except (OSError, ValueError):
        return {}, {}


def bc_company(lineage: Any, handle: Any) -> dict[str, str] | None:
    """The Business Central company a BC record is in: its version's lineage
    (``bc_<book>``) agreeing with the company its handle names by the pinned books
    (``integrations/bc_company_books.json``), with the name BC lists. Numbers recur across
    companies (live: 1,751 distribution customers repeat manufacturing numbers), so a BC
    record is told apart by it. None for another source, or when the two disagree or the
    company is not pinned (never guessed)."""
    if not isinstance(lineage, str) or not lineage.startswith("bc_"):
        return None
    found = _BC_HANDLE.match(handle) if isinstance(handle, str) else None
    if found is None:
        return None
    books, names = _bc_company_pins()
    environment, company = found.group(1).lower(), found.group(2).lower()
    book = books.get(environment, {}).get(company)
    if book is None or f"bc_{book}" != lineage:
        return None
    return {"book": book, "name": names.get(environment, {}).get(company) or book,
            "l1_handle": handle}


def _source_of(handle: str | None, lineage: str | None = None) -> str | None:
    if lineage == "workbench":
        return "workbench"
    if not isinstance(handle, str) or "://" not in handle:
        return None
    return _SCHEME_SOURCE.get(handle.split("://", 1)[0])


def origin_rank(lineage: Any) -> int:
    """How near a record is to the system its business object was created in, by its
    lineage: a source system's own record (``*_bronze``: FileMaker, Sage) 0, the
    Portal's (``gold_*``: its ETL copies and its own records) 1, a BC posting
    (``bc_*``) 2, anything else 3. Lower is the origin."""
    text = str(lineage or "")
    if text.endswith("_bronze"):
        return 0
    if text.startswith("gold_"):
        return 1
    if text.startswith("bc_"):
        return 2
    return 3


def _one_source_key(row: Mapping[str, Any]) -> bool:
    """Whether a link row is a stored foreign key (binding ``fk``) whose evidence is all
    read from one known source. A row that does not carry its binding and evidence, a
    join through another source, a text join, a mention, no evidence or unparseable
    evidence is not (fail closed)."""
    keys = row.keys() if hasattr(row, "keys") else ()
    if "binding_kind" not in keys or "evidence_handles" not in keys \
            or row["binding_kind"] != "fk":
        return False
    try:
        handles = json.loads(row["evidence_handles"])
    except (TypeError, ValueError):
        return False
    if not isinstance(handles, list):
        return False
    sources = {_source_of(h) for h in handles}
    return len(sources) == 1 and None not in sources


# A source field that can hold a person's name instead of a PO (REC-07): its value is served only when PO-like.
PO_TEXT_FIELD = "po_reference"
PO_WITHHELD = "not_po_like"


class WithheldText(dict):
    """A withheld ``untrusted_text`` envelope (``withheld: not_po_like``, empty text) that
    keeps the source text for in-process comparison only (``compared_text``: identity-peer
    conflicts and attested document comparisons decide on the source's real text, as they
    would unwithheld). The text is in a slot, never an item: JSON, ``dict(...)`` and the
    MCP firewall see the items only, so it never leaves the process; a plain copy loses it
    and then compares as unknown."""
    __slots__ = ("source_text",)

    def __init__(self, origin: Mapping[str, Any], source_text: str) -> None:
        super().__init__(untrusted_text="", origin=origin, withheld=PO_WITHHELD)
        self.source_text = source_text


def compared_text(value: Any) -> Any:
    """The text a comparison reads from a value: an envelope's text, a withheld
    envelope's source text (``WithheldText``), None for a withheld envelope without it
    (unknown: neither a match nor a conflict); any other value as it is."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        if "withheld" in value:
            return getattr(value, "source_text", None)
        return value["untrusted_text"]
    return value


def _same_text(first: Any, second: Any) -> bool:
    """The same words, whatever the case and spacing (the caller's own text)."""
    return (isinstance(first, str) and isinstance(second, str)
            and " ".join(first.split()).casefold() == " ".join(second.split()).casefold())


def _predicate_text(predicate: Any) -> str:
    """A schema record test (``ontology/schema/_format.schema.json`` predicate) as text."""
    if not isinstance(predicate, Mapping):
        return "?"
    text = f"{predicate.get('path')} {predicate.get('op')}"
    return text + (f" {json.dumps(predicate['value'])}" if "value" in predicate else "")


def _tested_marks(prop: Mapping[str, Any], binding: str,
                  type_spec: Mapping[str, Any]) -> set[str] | None:
    """The markings a reader must hold to learn whether a record passes the ``when``
    tests of ``prop``'s sources on ``binding``: those of every property of the type
    stated by a field the tests read (same top-level column). None when a test reads
    across a link, reads a column no property of the type states on ``binding`` (its
    markings are unknown) or is malformed (no reader learns the outcome per record)."""
    columns: set[str] = set()
    for source in prop.get("sources", ()):
        if not isinstance(source, Mapping) or source.get("binding") != binding:
            continue
        for test in source.get("when") or ():
            if not isinstance(test, Mapping) or "via_link" in test \
                    or not isinstance(test.get("path"), str):
                return None
            columns.add(test["path"].split(".", 1)[0])
    marks: set[str] = set()
    stated: set[str] = set()
    for other in type_spec.get("properties", ()):
        if not isinstance(other, Mapping):
            continue
        read = {str(src.get("path", "")).split(".", 1)[0] for src in other.get("sources", ())
                if isinstance(src, Mapping) and src.get("binding") == binding} & columns
        if read:
            stated |= read
            marks.update(other.get("markings", ()))
    return marks if stated == columns else None


def _gap(state: str, reason: str, subject: str, detail: str | None = None,
         searched: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"state": state, "reason": reason, "subject": subject[:700]}
    if detail:
        out["detail"] = detail[:600]
    if searched:
        out["searched"] = dict(searched)
    return out


# The gap of a record its source did not hold at T, by removal basis (store_ddl).
REMOVAL_REASONS = {"missing_from_source": "missing_from_source",
                   "source_soft_delete": "deleted_in_source"}


def removal_gap(basis: str, since: str | None, subject: str) -> dict[str, Any]:
    """The gap of a record removed at T (``StoreReader.removals``): deleted by the
    source's own soft-delete column, else missing from it (a key scan; never proof of a
    deletion)."""
    if basis == "source_soft_delete":
        return _gap("absent_in_source", REMOVAL_REASONS[basis], subject,
                    f"the source marks this record deleted (its own soft-delete column) "
                    f"since {since or 'an unknown time'}; it stays cited, it is not live")
    return _gap("absent_in_source", REMOVAL_REASONS["missing_from_source"], subject,
                "the source no longer returns this record (key-only scan); "
                "not proof it was deleted")


def _error(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "message": message[:600]}
    if hint:
        out["hint"] = hint[:600]
    return {"error": out}


def _compare_key(value_type: str, value: Any) -> Any:
    if value_type in ("decimal", "money", "quantity"):
        try:
            from ontology.measure import exact_normal
            return ("d", exact_normal(Decimal(str(value))))
        except InvalidOperation:
            return ("s", str(value))
    if value_type in ("text", "json"):
        return ("t", " ".join(str(value).split()).casefold())
    return ("s", str(value))


class ToolError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


class _MatchedId(NamedTuple):
    """A filter match read for its record alone (``_match_filters(ids_only=True)``)."""
    object_id: str


@dataclass
class _Match:
    object_id: str
    tier: str
    reason: str
    role: str
    kind: str
    normalized: str
    raw: str
    property: str
    binding: str
    l1_handle: str
    field_path: str
    why_not: tuple[Mapping[str, Any], ...] = ()
    # The matched text is the asked text by the owning system's literal comparison
    # (``exact_match``): ranks before a normalised-only match of the same strength.
    exact: bool = False
    # A captured document's text: its sender class (``untrusted_text`` origin).
    sender_class: str | None = None
    # Relevance order of a hybrid search hit (``operating/search``, PLX_SEARCH=hybrid):
    # 1 is the best; 0 for every other match, which keeps the object-id order.
    order: int = 0

    def sort_key(self) -> tuple:
        # Other text (a document's words, a value that is not display text) ranks with
        # the alias matches, so a record's identifier match (a linked one included) is
        # the one it is shown and gated by.
        primary = self.role == "primary" and self.reason not in _OTHER_TEXT_REASONS
        return (_TIER_RANK[self.tier], 0 if primary else 1,
                _REASON_RANK[self.reason], 0 if self.exact else 1, self.order,
                self.object_id)


LIVE_LOOKUP_HINT = ("plx.find answered it from a live source lookup; its objects are not "
                    "in the projection store yet: read the cited row with "
                    "plx.read, or build or refresh the projection "
                    "(ontology/project.py)")


class _LiveSets:
    """The shared registry as a live-fallback answer sees it: every handle it makes is
    marked live, so plx.around / plx.timeline on it say ``not_in_projection``."""

    def __init__(self, registry: SetRegistry) -> None:
        self.registry = registry

    def new(self, actor: Actor, object_ids: Sequence[str],
            links: Iterable[str] | None = None, complete: bool = False,
            total: int | None = None) -> str:
        # a live set is never walked or counted (``not_in_projection``): no links kept,
        # and never complete (the registry records none live as complete)
        return self.registry.new(actor, object_ids, live=True)

    def completeness(self, handle: str, actor: Actor) -> tuple[bool, int | None]:
        return self.registry.completeness(handle, actor)

    def resolve(self, handle: str, actor: Actor) -> tuple[str, ...] | None:
        return self.registry.resolve(handle, actor)


# --------------------------------------------------------------------------- tools


def schema_offers_type(types: Mapping[str, Any], tid: Any) -> bool:
    """Whether an unfiltered ``plx.schema`` listing offers the object type: a type no
    source serves (``not_served``: no record of it can be read) is left out, so the map
    lists only types a read can reach; naming it (``object_type``) still describes it and
    why it is not served. A link, event or action is offered when every type it names is."""
    spec = types.get(tid) if isinstance(tid, str) else None
    return spec is None or (spec.get("availability") or {}).get(
        "state", "served") != "not_served"
