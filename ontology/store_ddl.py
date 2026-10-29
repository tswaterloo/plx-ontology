"""DDL of the L2 ontology projection store (C0 contract, frozen; DDL only).

The projection is disposable and rebuilt from L1 captures:
a builder writes a new SQLite file and swaps it in atomically, so it never
contends with the ingest worker. It copies no uncited data. Every object, value,
link, identifier and event row carries the L1 handle it was read from
(``api://…@<generation>-<capture>``, ``bc://…``, ``graph-mail://…``,
``sharepoint://…``, ``original://…``, ``brain://…``) plus the field path inside that record, a
digest of the cited content and the record's lineage (R1). A finding cites the
same handle and field path, so ``operating/claim_checks.py`` checks it unchanged.

Time (R8): one store holds every captured version. ``objects`` is the identity
(one row per source record); ``object_versions`` holds one row per captured
version of it (L1 handle, digest, lineage, markings). ``recorded_at`` is when L1
first captured that version and ``superseded_at`` when a later capture replaced
it; ``as_known_at = T`` reads rows with ``recorded_at <= T`` and (``superseded_at``
null or ``> T``) in object_versions, object_values, links, identifiers and
events alike. At most one version per object is current. Values cite the
version they were read from (composite foreign key). Valid (business) time
exists only on events. ``missing_from_source_since`` mirrors the L1 key-only
scan state: missing from the source, not deleted. ``object_removals`` keeps every
stretch ``[since, until)`` the source did not hold the record, by ``basis``: missing
from the source (closed when it returned; the open one equals
``missing_from_source_since``) or deleted by the source's own soft-delete column
(``source_soft_delete``, from its value). A missing record a full capture found replaced
by a fresh id carries ``replaced_basis`` (``source_replacement``: ``replaced_by`` names
its one successor; ``ambiguous_replacement``: every candidate, none chosen) and
``replaced_by`` (JSON list of object ids); v3: never a pairing on the owning parent's
foreign keys alone, so a v2 store (which may name a wrong successor) is rebuilt. A review or link built on the record does not
hold inside a removal (``ontology.reviews``), so ``as_known_at`` in a past removal
reads it withdrawn and after the return reads it again.

Permissions: ``objects.audience_key`` names the L1 audience unit the record
belongs to (``api:<schema.relation>``, ``bc:<company>/<entity set>``,
``mail:<mailbox>``, ``sharepoint:<drive>``, ``original:<id>``, ``brain:<namespace>``). Tools check the
actor against it before fill, rank and count; item-level ACLs (a mail message,
a SharePoint item) are re-gated on read through the L1 handle. ``markings`` are
canonical JSON arrays (sorted, no spaces) of the format's markings, on every
row a tool can return, identifiers included.

Foreign keys: SQLite enforces REFERENCES only with ``PRAGMA foreign_keys = ON``,
which is per connection. ``create`` turns it on for its connection; every
connection that writes the store (``ontology/store.py``) must turn it on too.

Layout (v6): ``object_values`` and ``links`` are views with the v5 columns, so every
reader and its SQL are unchanged. A value is a cell naming its provenance row
(``value_sources``: object, version, digest, lineage, binding, times), an interned field
and an interned kind; a link row keeps each reason and evidence text once in
``link_texts``, by reference from a skeleton. INSTEAD OF triggers keep direct writes,
updates and deletes through the views working with the v5 constraints; the builder
writes the physical tables directly. ``object_values_numbered`` exposes the cell rowid
(the v5 value rowid) for keyset paging. A view has no ``rowid`` (it reads NULL) and SQLite
counts no change an INSTEAD OF trigger makes (a write through a view reports
``rowcount`` 0): a writer verifies its outcome by reading the rows back.

Layout v7:
the v6 tables, with ``object_versions``' unique key reordered to
``(l1_handle, object_id)`` so a lookup by handle is a search, not a scan. Same rows, same
disk; a v6 store names another format and is rebuilt cold (``format_problem``).

Layout v8 (same decision): the v7 tables plus a word index of
captured text for ``plx.find`` (``ontology/text_index.py``). ``value_text`` holds the words
of every text, identifier and enum value under its cell's rowid (the value's ``row_no``),
kept by triggers on ``value_cells``; ``text_fields`` / ``text_chunks`` / ``chunk_text`` hold
the scanned document text no value carries (mail bodies, file and attachment text, by
page), written by the ``text_index`` build step. Both FTS5 tables are contentless and
keep no positions (``detail=none``): a search is an AND of word prefixes, and the text
it quotes is the value's or the chunk's own. A v7 store is rebuilt cold.

Layout v9: the v8 tables with the
marking ``purchasing_prices`` added to ``MARKINGS``, so every ``markings`` CHECK admits its
sets. A v8 store's CHECK refuses them, so it is rebuilt cold.

Layout v10 (measured on the live store, generation 00000015: 74.6M
value cells took 4.9 GB of its 9.5 GB, 68M of them from 14 generated Sage AP relations). A
value row is a cell or a packed record: a binding declared ``value_storage: packed`` (in
``ontology/schema/overrides/storage.json``, with its evidence) stores the currency-free values
of each source record as one JSON array under a shared layout, and those values are not
word-indexed (they are read by object, property, identifier and link, never by a word
search). ``value_sources`` names its version by rowid instead of repeating its object, handle,
digest and lineage. ``object_values`` reads the same rows as v9. A v9 store is rebuilt cold.

Enumerations here must equal those in ``ontology/schema/_format.schema.json``
(test: ``ontology/tests/test_contracts.py``). The projector (L7) owns
``ontology/store.py``; it executes this DDL and changes it only through a new
``FORMAT`` version.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from ontology.units import TAX_BASES

FORMAT = "plx-ontology-store/v10"

LINEAGES = ("fm_bronze", "sage_bronze", "silver_view", "gold_etl_from_fm", "gold_etl_from_sage",
            "gold_native_staging", "mail", "sharepoint", "original", "workbench",
            "mission_control", "brain")
BC_LINEAGE_GLOB = "bc_[a-z0-9]*"
TIERS = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead", "contradicted")
LINK_KINDS = ("fk", "json_path", "same_record", "text_match", "mention", "reviewed")
VALUE_TYPES = ("text", "identifier", "integer", "decimal", "money", "quantity", "boolean", "date",
               "timestamp", "enum", "json")
QUANTITY_ROLES = ("accepted", "actual", "committed", "damaged", "delivered", "filled", "inspected",
                  "invoiced", "manufactured", "ordered", "planned", "produced", "received",
                  "rejected", "retained", "returned", "shipped")
TIME_BASES = ("source_recorded", "source_business_date", "planned", "capture_observed",
              "document_stated", "document_ocr")
IDENTIFIER_ROLES = ("primary", "alias")
REMOVAL_BASES = ("missing_from_source", "source_soft_delete")
REPLACEMENT_BASES = ("source_replacement", "ambiguous_replacement")
MARKINGS = ("finance", "l5", "pii", "purchasing_prices", "quality_controlled")
# A marking every holder of another marking holds too (``held_markings`` of every read
# policy): purchasing_prices marks the purchase-order prices purchasing staff may read
# (``ontology/schema/overrides/markings.json``
# ``scoped_markings``); a finance reader reads them as before.
MARKING_HELD_WITH = {"purchasing_prices": ("finance",)}


def with_implied(held: Iterable[str]) -> frozenset[str]:
    """``held`` and every marking it implies (``MARKING_HELD_WITH``)."""
    held = frozenset(held)
    return held | {m for m, by in MARKING_HELD_WITH.items() if held & set(by)}


def _marking_sets() -> tuple[str, ...]:
    sets = []
    for mask in range(1 << len(MARKINGS)):
        chosen = sorted(m for i, m in enumerate(MARKINGS) if mask >> i & 1)
        sets.append("[" + ",".join(f'"{m}"' for m in chosen) + "]")
    return tuple(sorted(sets))


MARKING_SETS = _marking_sets()


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# bc_<company>: same grammar as the format's ^bc_[a-z0-9][a-z0-9_]{0,47}$.
_LINEAGE = (f"({_in('lineage', LINEAGES)} OR (lineage GLOB '{BC_LINEAGE_GLOB}' "
            "AND substr(lineage, 4) NOT GLOB '*[^a-z0-9_]*' AND length(lineage) <= 51))")
# Same field-path grammar as operating/claim_checks.py _FIELD (row./key./rows.N. prefix).
_FIELD_PATH = ("(field_path GLOB 'row.?*' OR field_path GLOB 'key.?*' "
               "OR field_path GLOB 'rows.[0-9]*.?*')")
_MARKINGS = f"markings TEXT NOT NULL DEFAULT '[]' CHECK ({_in('markings', MARKING_SETS)})"
_TIMES = ("recorded_at TEXT NOT NULL, superseded_at TEXT, "
          "CHECK (superseded_at IS NULL OR superseded_at >= recorded_at)")

# v6: the logical value and link tables are views over compact physical tables (the
# comments at value_sources and link_rows); readers see the v5 rows and columns.
VALUE_COLUMNS = ("object_id", "property", "value_type", "value", "unit", "quantity_role",
                 "currency", "tax_basis", "currency_citation", "l1_handle", "field_path",
                 "digest", "lineage", "binding_id", "parser", "authority_rank", "markings",
                 "recorded_at", "superseded_at")
# v10: a value source names its version by rowid (``ver``); its object, handle, digest and
# lineage are the version's (a digest or lineage of its own is kept where it differs).
SOURCE_COLUMNS = ("object_id", "l1_handle", "digest", "lineage", "binding_id", "recorded_at",
                  "superseded_at")
SOURCE_STORED = ("ver", "binding_id", "recorded_at", "superseded_at", "digest", "lineage")
FIELD_COLUMNS = ("property", "field_path")
KIND_COLUMNS = ("value_type", "unit", "quantity_role", "tax_basis", "parser", "authority_rank",
                "markings")
CELL_COLUMNS = ("value", "currency", "currency_citation")
# A packed record holds at most this many values (its layout's positions).
PACK_MAX_FIELDS = 4096
# How a binding's values are stored (schema ``value_storage``): ``cells``, one row per
# value, word-indexed when text; ``packed``, one row per source record holding every value
# that carries no currency, never word-indexed (read by object, property, identifier, link).
VALUE_STORAGES = ("cells", "packed")
_VALUE_FROM = {"object_id": "v.object_id", "l1_handle": "v.l1_handle",
               "digest": "coalesce(s.digest, v.digest)",
               "lineage": "coalesce(s.lineage, v.lineage)",
               "binding_id": "s.binding_id", "recorded_at": "s.recorded_at",
               "superseded_at": "s.superseded_at",
               **{c: "f." + c for c in FIELD_COLUMNS}, **{c: "k." + c for c in KIND_COLUMNS},
               "value": "CASE WHEN c.field IS NULL THEN json_extract(c.value, lf.path) "
                        "ELSE c.value END",
               "currency": "c.currency", "currency_citation": "c.currency_citation"}
_VALUE_JOIN = ("FROM value_cells c JOIN value_sources s ON s.src = c.src "
               "JOIN object_versions v ON v.rowid = s.ver "
               "JOIN value_layout_fields lf ON lf.layout = c.layout "
               "JOIN value_fields f ON f.field = lf.field JOIN value_kinds k ON k.kind = lf.kind")
LINK_COLUMNS = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
                "link_contract", "reason", "why_not", "evidence_handles", "reviewed_by",
                "reviewed_at", "review_action_id", "markings", "recorded_at", "superseded_at")
LINK_SLOTS = 24
LINK_PACKED = {"reason": "reason_id", "evidence_handles": "evidence_id"}


def skeleton_of(text: str) -> str:
    """A link text as a skeleton without slots: every '%' doubled (the view formats it)."""
    return text.replace("%", "%%")


def singleton_fields(field: int, kind: int) -> str:
    """``value_layouts.fields`` of the layout of one value (a cell): ``[[field,kind]]``."""
    return f"[[{int(field)},{int(kind)}]]"


def _new(column: str) -> str:
    # A view has no column defaults: an omitted markings/why_not reads as the table's.
    return f"coalesce(NEW.{column}, '[]')" if column in ("markings", "why_not") else f"NEW.{column}"


def _match(columns: tuple[str, ...], row: str = "NEW") -> str:
    return " AND ".join(f"{c} IS {_new(c).replace('NEW.', row + '.')}" for c in columns)


def _value_triggers() -> tuple[str, ...]:
    """Direct writes through ``object_values`` (tests, small stores): a written value is a
    cell. A delete or update of a packed value first unpacks its record into cells (the
    unpacked text values are then word-indexed like any cell)."""
    def ver(row: str) -> str:
        return (f"(SELECT rowid FROM object_versions WHERE object_id = {row}.object_id "
                f"AND l1_handle = {row}.l1_handle)")

    def own(column: str, row: str) -> str:   # NULL when it is the version's own
        return (f"CASE WHEN {row}.{column} IS (SELECT {column} FROM object_versions "
                f"WHERE rowid = {ver(row)}) THEN NULL ELSE {row}.{column} END")

    def source_match(row: str) -> str:
        return (f"ver IS {ver(row)} AND binding_id IS {row}.binding_id "
                f"AND recorded_at IS {row}.recorded_at AND superseded_at IS {row}.superseded_at "
                f"AND digest IS {own('digest', row)} AND lineage IS {own('lineage', row)}")

    def field_of(row: str) -> str:
        return (f"(SELECT field FROM value_fields WHERE property = {row}.property "
                f"AND field_path = {row}.field_path)")

    def srcs_of(row: str) -> str:
        return (f"(SELECT s.src FROM value_sources s WHERE s.ver = {ver(row)})")

    kind_new = f"(SELECT kind FROM value_kinds WHERE {_match(KIND_COLUMNS)} ORDER BY kind LIMIT 1)"
    single_new = f"'[[' || {field_of('NEW')} || ',' || {kind_new} || ']]'"
    exists_key = ("SELECT 1 FROM object_values w WHERE w.object_id = {0}.object_id "
                  "AND w.l1_handle = {0}.l1_handle AND w.property = {0}.property "
                  "AND w.field_path = {0}.field_path")
    # A value's digest and lineage are stored only where they differ from its version's:
    # one not given is refused as the v9 NOT NULL columns refused it, never read as the
    # version's.
    not_null = " ".join(
        f"SELECT RAISE(ABORT, 'NOT NULL constraint failed: value_sources.{c}') "
        f"WHERE {{0}}.{c} IS NULL;" for c in ("digest", "lineage"))
    raise_dup = ("SELECT RAISE(ABORT, 'UNIQUE constraint failed: object_values.object_id, "
                 "object_values.property, object_values.l1_handle, object_values.field_path')")
    interns = (
        f"INSERT INTO value_sources ({', '.join(SOURCE_STORED)}) SELECT {ver('NEW')}, "
        f"NEW.binding_id, NEW.recorded_at, NEW.superseded_at, {own('digest', 'NEW')}, "
        f"{own('lineage', 'NEW')} WHERE NOT EXISTS (SELECT 1 FROM value_sources WHERE "
        f"{source_match('NEW')});",
        f"INSERT INTO value_fields (property, field_path) SELECT NEW.property, NEW.field_path "
        f"WHERE NOT EXISTS (SELECT 1 FROM value_fields WHERE {_match(FIELD_COLUMNS)});",
        f"INSERT INTO value_kinds ({', '.join(KIND_COLUMNS)}) SELECT "
        f"{', '.join(_new(c) for c in KIND_COLUMNS)} WHERE NOT EXISTS "
        f"(SELECT 1 FROM value_kinds WHERE {_match(KIND_COLUMNS)});",
        f"INSERT INTO value_layouts (fields) SELECT {single_new} WHERE NOT EXISTS "
        f"(SELECT 1 FROM value_layouts WHERE fields = {single_new});",
        f"INSERT INTO value_layout_fields (layout, pos, field, kind, path) SELECT "
        f"(SELECT layout FROM value_layouts WHERE fields = {single_new}), 0, "
        f"{field_of('NEW')}, {kind_new}, NULL WHERE NOT EXISTS (SELECT 1 FROM "
        f"value_layout_fields WHERE layout = (SELECT layout FROM value_layouts WHERE "
        f"fields = {single_new}));")
    new_src = f"(SELECT src FROM value_sources WHERE {source_match('NEW')} ORDER BY src LIMIT 1)"
    new_layout = f"(SELECT layout FROM value_layouts WHERE fields = {single_new})"

    def unpack(row: str) -> str:
        """The packed records of the value's version holding its field, as cells."""
        packs = (f"c.field IS NULL AND c.src IN {srcs_of(row)} AND EXISTS (SELECT 1 FROM "
                 f"value_layout_fields l2 WHERE l2.layout = c.layout AND l2.field = "
                 f"{field_of(row)})")
        return (
            "INSERT INTO value_cells (src, field, layout, value) SELECT c.src, lf.field, "
            "(SELECT layout FROM value_layouts WHERE fields = '[[' || lf.field || ',' || "
            "lf.kind || ']]'), json_extract(c.value, lf.path) FROM value_cells c "
            f"JOIN value_layout_fields lf ON lf.layout = c.layout WHERE {packs} "
            "ORDER BY c.src, lf.pos;"
            f" DELETE FROM value_cells WHERE rowid IN (SELECT c.rowid FROM value_cells c "
            f"WHERE {packs});")

    old_cell = (f"SELECT c.rowid FROM value_cells c WHERE c.field = {field_of('OLD')} "
                f"AND c.src IN {srcs_of('OLD')}")
    drop_empty = ("DELETE FROM value_sources WHERE ver IS " + ver("OLD") + " AND NOT EXISTS "
                  "(SELECT 1 FROM value_cells c WHERE c.src = value_sources.src);")
    return (
        f"""CREATE TRIGGER IF NOT EXISTS object_values_insert INSTEAD OF INSERT ON object_values
        BEGIN
          {not_null.format('NEW')}
          {raise_dup} WHERE EXISTS ({exists_key.format('NEW')});
          {' '.join(interns)}
          INSERT INTO value_cells (src, field, layout, {', '.join(CELL_COLUMNS)}) VALUES (
            {new_src}, {field_of('NEW')}, {new_layout}, {', '.join(_new(c) for c in CELL_COLUMNS)});
        END""",
        f"""CREATE TRIGGER IF NOT EXISTS object_values_delete INSTEAD OF DELETE ON object_values
        BEGIN
          {unpack('OLD')}
          DELETE FROM value_cells WHERE rowid IN ({old_cell});
          {drop_empty}
        END""",
        # In place: the cell keeps its rowid (row_no), as the v5 row did.
        f"""CREATE TRIGGER IF NOT EXISTS object_values_update INSTEAD OF UPDATE ON object_values
        BEGIN
          {raise_dup} WHERE EXISTS ({exists_key.format('NEW')} AND NOT (
            NEW.object_id IS OLD.object_id AND NEW.l1_handle IS OLD.l1_handle
            AND NEW.property IS OLD.property AND NEW.field_path IS OLD.field_path));
          {not_null.format('NEW')}
          {unpack('OLD')}
          {' '.join(interns)}
          UPDATE value_cells SET src = {new_src}, field = {field_of('NEW')},
            layout = {new_layout}, {', '.join(f'{c} = {_new(c)}' for c in CELL_COLUMNS)}
          WHERE rowid = ({old_cell});
          {drop_empty}
        END""",
    )


def _link_column(column: str) -> str:
    """A view column: the stored column, or a packed text formatted with its slot texts.
    The evidence takes the first ``ek`` slots; the reason skips them ('%.0s' reads an
    argument and writes nothing). One format() pass, no replace() per slot; the tiers
    keep the slot lookups to about the slots the row uses."""
    if column not in LINK_PACKED:
        return "l." + column
    skeleton = f"(SELECT text FROM link_skeletons WHERE id = l.{LINK_PACKED[column]})"
    if column == "reason":
        skeleton = f"substr('{'%.0s' * LINK_SLOTS}', 1, 4 * l.ek) || {skeleton}"
        tiers = [(f"l.s{n} IS NULL", n) for n in (8, 16)]
    else:
        tiers = [(f"l.ek <= {n}", n) for n in (0, 2, 8)]

    def formatted(slots: int) -> str:
        return f"format({skeleton}" + "".join(
            f", (SELECT text FROM link_texts WHERE id = l.s{k})" for k in range(slots)) + ")"
    whens = " ".join(f"WHEN {test} THEN {formatted(n)}" for test, n in tiers)
    return f"CASE {whens} ELSE {formatted(LINK_SLOTS)} END"


def _link_triggers() -> tuple[str, ...]:
    # A direct write stores its texts as written (no slots); the builder packs them.
    plain = [c for c in LINK_COLUMNS if c not in LINK_PACKED]
    stored = {c: f"replace(NEW.{c}, '%', '%%')" for c in LINK_PACKED}   # skeleton_of
    skeletons = " ".join(
        f"INSERT INTO link_skeletons (text) SELECT {stored[c]} WHERE NOT EXISTS "
        f"(SELECT 1 FROM link_skeletons WHERE text = {stored[c]});" for c in LINK_PACKED)
    ids = {column: f"(SELECT id FROM link_skeletons WHERE text = {stored[column]})"
           for column in LINK_PACKED}
    return (
        f"""CREATE TRIGGER IF NOT EXISTS links_insert INSTEAD OF INSERT ON links
        BEGIN
          {skeletons}
          INSERT INTO link_rows ({', '.join(plain)}, {', '.join(LINK_PACKED.values())})
          VALUES ({', '.join(_new(c) for c in plain)}, {', '.join(ids.values())});
        END""",
        """CREATE TRIGGER IF NOT EXISTS links_delete INSTEAD OF DELETE ON links
        BEGIN
          DELETE FROM link_rows WHERE link_id = OLD.link_id;
        END""",
        f"""CREATE TRIGGER IF NOT EXISTS links_update INSTEAD OF UPDATE ON links
        BEGIN
          {skeletons}
          UPDATE link_rows SET {', '.join(f'{c} = {_new(c)}' for c in plain)},
            {', '.join(f'{LINK_PACKED[c]} = {ids[c]}' for c in LINK_PACKED)}, ek = 0,
            {', '.join(f's{k} = NULL' for k in range(LINK_SLOTS))}
          WHERE link_id = OLD.link_id;
        END""",
    )


_VALUE_TRIGGERS = _value_triggers()
# v8 word index (the module docstring): unicode61 words, case and diacritics folded; no
# positions (an AND of word prefixes needs none) and no sizes, to keep it small.
TEXT_TOKENIZER = "unicode61 remove_diacritics 2"
TEXT_FTS = ("USING fts5({}, content='', detail='none', columnsize=0, "
            f"tokenize='{TEXT_TOKENIZER}')")
# The value types whose words are indexed (a value of another type is a number, a date,
# a flag or JSON; its text is not searched as words).
TEXT_VALUE_TYPES = ("text", "identifier", "enum")
_INDEXED = ("{0}.field IS NOT NULL AND {0}.value IS NOT NULL AND (SELECT k.value_type "
            "FROM value_layout_fields lf JOIN value_kinds k ON k.kind = lf.kind "
            f"WHERE lf.layout = {{0}}.layout) IN ({', '.join(repr(t) for t in TEXT_VALUE_TYPES)})")
# A contentless table forgets a row only when told its words: the delete names the
# value it was indexed with (a value kind is never deleted or changed in place). A packed
# record (``field`` NULL) is never indexed.
_TEXT_TRIGGERS = (
    f"""CREATE TRIGGER IF NOT EXISTS value_text_insert AFTER INSERT ON value_cells
    WHEN {_INDEXED.format('NEW')}
    BEGIN
      INSERT INTO value_text (rowid, value) VALUES (NEW.rowid, NEW.value);
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS value_text_delete AFTER DELETE ON value_cells
    WHEN {_INDEXED.format('OLD')}
    BEGIN
      INSERT INTO value_text (value_text, rowid, value) VALUES ('delete', OLD.rowid, OLD.value);
    END""",
    # One trigger, old words out before new words in: a rowid's words are added and
    # removed as deltas, so the other order would drop a word both values hold.
    f"""CREATE TRIGGER IF NOT EXISTS value_text_update AFTER UPDATE OF value, layout, field ON value_cells
    BEGIN
      INSERT INTO value_text (value_text, rowid, value)
        SELECT 'delete', OLD.rowid, OLD.value WHERE {_INDEXED.format('OLD')};
      INSERT INTO value_text (rowid, value) SELECT NEW.rowid, NEW.value
        WHERE {_INDEXED.format('NEW')};
    END""",
)
_LINK_TRIGGERS = _link_triggers()

STATEMENTS: tuple[str, ...] = (
    # Build facts: format, schema_digest, catalog_hash, built_at, as_known_at,
    # l1_generations (JSON), source freshness (JSON). Read by every tool's meta.
    """CREATE TABLE IF NOT EXISTS store_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL)""",
    # Identity: one row per projected source record. object_id = '<Type>:<binding id>:<native key>'.
    """CREATE TABLE IF NOT EXISTS objects (
        object_id TEXT PRIMARY KEY,
        object_type TEXT NOT NULL,
        binding_id TEXT NOT NULL,
        native_key TEXT NOT NULL CHECK (json_valid(native_key)),
        audience_key TEXT NOT NULL,
        first_recorded_at TEXT NOT NULL,
        missing_from_source_since TEXT,
        UNIQUE (object_type, binding_id, native_key))""",
    # Stretches the source did not hold the record (module docstring); until NULL = open.
    f"""CREATE TABLE IF NOT EXISTS object_removals (
        object_id TEXT NOT NULL REFERENCES objects(object_id),
        basis TEXT NOT NULL CHECK ({_in('basis', REMOVAL_BASES)}),
        since TEXT NOT NULL,
        until TEXT,
        replaced_basis TEXT CHECK (replaced_basis IS NULL OR
                                   {_in('replaced_basis', REPLACEMENT_BASES)}),
        replaced_by TEXT,
        CHECK ((replaced_basis IS NULL) = (replaced_by IS NULL)),
        CHECK (until IS NULL OR until > since),
        PRIMARY KEY (object_id, since, basis))""",
    # Versions: one row per captured version of a record (its own L1 handle). v7: the
    # unique key leads with the handle, so its index (no extra one) finds the objects
    # of an L1 handle: a citation, a mention or a review names a record by handle only,
    # and keyed by object first that read scanned every version (plx.around's page
    # citations on the 1.6M-object live store). Lookups by object use the primary key.
    f"""CREATE TABLE IF NOT EXISTS object_versions (
        object_id TEXT NOT NULL REFERENCES objects(object_id),
        l1_handle TEXT NOT NULL,
        digest TEXT NOT NULL,
        lineage TEXT NOT NULL CHECK {_LINEAGE},
        {_MARKINGS},
        {_TIMES},
        PRIMARY KEY (object_id, recorded_at),
        UNIQUE (l1_handle, object_id))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS object_versions_current ON object_versions(object_id) "
    "WHERE superseded_at IS NULL",
    # Typed values, per source, never merged (R11): the logical table is the view
    # ``object_values`` (below). value is text: Decimal text,
    # ISO date/timestamp, normalized identifier, 'true'/'false'; NULL = source null.
    # tax_basis (v4): a money value's basis on its own record (the source's tax_basis, per
    # lineage, or read per record through tax_basis_from; ontology/units.py); NULL
    # for any other value, and on a money row written without one.
    # currency_citation (v5): the record the currency was read on when that is not the
    # value's own (a blank currency code read as the local currency of its company:
    # binding local_currency): JSON {object_id, l1_handle, field_path, digest, lineage}
    # of the version in force at the value's time. A reader who may not read that object
    # reads the currency as unknown (ontology.store.withhold_currency).
    # authority_rank = position in the property's precedence (0 strongest), NULL if
    # no rule matched; disagreement between ranks is a conflict, never resolved here.
    #
    # v10 layout (rows are the same, stored once per fact): value_sources holds the
    # provenance a value shares with every other value read from the same version: the
    # version by rowid (``ver``: its object, L1 handle, digest and lineage, the last two
    # kept here only where a value's differ), binding and recorded/superseded; value_fields
    # the (property, field_path) pair; value_kinds the type, unit, role, tax basis, parser,
    # authority rank and markings; value_layouts the (field, kind) of each position of a
    # value row; value_cells one row per value (a cell) or per packed record (every
    # currency-free value of one source of a ``value_storage: packed`` binding). The
    # logical key (object_id, property, l1_handle, field_path) is one field per source
    # (a cell's UNIQUE (src, field), a record's layout, and the builder's check that the
    # two never share a field and that two sources of one version never share one:
    # ontology.store.StoreBuilder.check); the INSTEAD OF triggers enforce it on a direct
    # write. A version's rowid is never renumbered: a store is never VACUUMed.
    f"""CREATE TABLE IF NOT EXISTS value_sources (
        src INTEGER PRIMARY KEY,
        ver INTEGER NOT NULL,
        binding_id TEXT NOT NULL,
        digest TEXT,
        lineage TEXT CHECK (lineage IS NULL OR {_LINEAGE}),
        {_TIMES})""",
    "CREATE INDEX IF NOT EXISTS value_sources_by_version ON value_sources(ver)",
    f"""CREATE TABLE IF NOT EXISTS value_fields (
        field INTEGER PRIMARY KEY,
        property TEXT NOT NULL,
        field_path TEXT NOT NULL CHECK {_FIELD_PATH},
        UNIQUE (property, field_path))""",
    f"""CREATE TABLE IF NOT EXISTS value_kinds (
        kind INTEGER PRIMARY KEY,
        value_type TEXT NOT NULL CHECK ({_in('value_type', VALUE_TYPES)}),
        unit TEXT,
        quantity_role TEXT CHECK (quantity_role IS NULL OR {_in('quantity_role', QUANTITY_ROLES)}),
        tax_basis TEXT CHECK (tax_basis IS NULL OR
                              (value_type = 'money' AND {_in('tax_basis', TAX_BASES)})),
        parser TEXT NOT NULL,
        authority_rank INTEGER CHECK (authority_rank IS NULL OR authority_rank >= 0),
        {_MARKINGS},
        CHECK (value_type <> 'quantity' OR quantity_role IS NOT NULL))""",
    "CREATE INDEX IF NOT EXISTS value_kinds_by_type ON value_kinds(value_type, parser)",
    # v10 layouts: the (field, kind) of each position of a value row; a cell's has one.
    """CREATE TABLE IF NOT EXISTS value_layouts (
        layout INTEGER PRIMARY KEY,
        fields TEXT NOT NULL UNIQUE CHECK (json_valid(fields) AND json_type(fields) = 'array'
                                           AND json_array_length(fields) > 0))""",
    f"""CREATE TABLE IF NOT EXISTS value_layout_fields (
        layout INTEGER NOT NULL REFERENCES value_layouts(layout),
        pos INTEGER NOT NULL CHECK (pos >= 0 AND pos < {PACK_MAX_FIELDS}),
        field INTEGER NOT NULL REFERENCES value_fields(field),
        kind INTEGER NOT NULL REFERENCES value_kinds(kind),
        path TEXT CHECK (path IS NULL OR path = '$[' || pos || ']'),
        PRIMARY KEY (layout, pos),
        UNIQUE (layout, field)) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS value_layout_fields_by_field ON value_layout_fields(field)",
    # A value row: a cell (``field`` set; its layout is that field's alone; ``value`` the
    # value) or a packed record (``field`` NULL; ``value`` a JSON array of every value
    # of its layout, text or null, in position order; no currency).
    """CREATE TABLE IF NOT EXISTS value_cells (
        src INTEGER NOT NULL REFERENCES value_sources(src),
        field INTEGER REFERENCES value_fields(field),
        layout INTEGER NOT NULL REFERENCES value_layouts(layout),
        value TEXT,
        currency TEXT CHECK (currency IS NULL OR (length(currency) = 3 AND currency = upper(currency))),
        currency_citation TEXT CHECK (currency_citation IS NULL OR
                                      (currency IS NOT NULL AND json_valid(currency_citation)
                                       AND json_type(currency_citation) = 'object')),
        CHECK (field IS NOT NULL OR (value IS NOT NULL AND currency IS NULL
                                     AND json_valid(value) AND json_type(value) = 'array')),
        UNIQUE (src, field))""",
    "CREATE INDEX IF NOT EXISTS value_cells_by_layout ON value_cells(layout)",
    f"""CREATE VIEW IF NOT EXISTS object_values AS SELECT
        {', '.join(_VALUE_FROM[c] + ' AS ' + c for c in VALUE_COLUMNS)}
        {_VALUE_JOIN}""",
    # A view has no rowid: row_no is the cell's (the word index's rowid), for keyset
    # paging; a packed record's values have none (they are not word-indexed).
    f"""CREATE VIEW IF NOT EXISTS object_values_numbered AS SELECT c.rowid AS row_no,
        {', '.join(_VALUE_FROM[c] + ' AS ' + c for c in VALUE_COLUMNS)}
        {_VALUE_JOIN} WHERE c.field IS NOT NULL""",
    *_VALUE_TRIGGERS,
    # v8 word index of values (``_TEXT_TRIGGERS``): rowid = value_cells.rowid.
    f"CREATE VIRTUAL TABLE IF NOT EXISTS value_text {TEXT_FTS.format('value')}",
    *_TEXT_TRIGGERS,
    # v8 document text no value carries (``ontology/text_index.py``): one row per scanned
    # field of a projected document version (its citation: the L1 handle, ``#page=<n>``
    # for a page; the version it reads; the markings a reader must hold: the version's
    # and its field's), its text in chunks (zlib, UTF-8; ``start`` = the chunk's first
    # character in the field) and their words (``chunk_text``, rowid = chunk_id). No
    # foreign keys: the step rewrites what it no longer finds, and a reader joins the
    # version and re-checks it.
    f"""CREATE TABLE IF NOT EXISTS text_fields (
        field_id INTEGER PRIMARY KEY,
        object_id TEXT NOT NULL,
        version_handle TEXT NOT NULL,
        citation TEXT NOT NULL,
        field_path TEXT NOT NULL CHECK {_FIELD_PATH},
        document_kind TEXT NOT NULL,
        sender_class TEXT NOT NULL,
        {_MARKINGS},
        text_digest TEXT NOT NULL,
        UNIQUE (citation, field_path, object_id))""",
    """CREATE TABLE IF NOT EXISTS text_chunks (
        chunk_id INTEGER PRIMARY KEY,
        field_id INTEGER NOT NULL,
        start INTEGER NOT NULL CHECK (start >= 0),
        body BLOB NOT NULL,
        UNIQUE (field_id, start))""",
    f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_text {TEXT_FTS.format('body')}",
    # Typed, tiered links (the logical table is the view ``links``). reason = JSON
    # feature vector of why the link exists (pairs
    # compared with their cited values, or features with fixed log-odds weights);
    # why_not = JSON list of features that failed or near-miss candidates (R3,
    # Senzing-style why/why-not). Explanations are feature vectors, never prose.
    # v6: link_rows names reason and evidence_handles by their skeleton (link_skeletons: a
    # format() pattern, the text with each slotted JSON string's content as %s and every
    # other '%' as %%) and the slotted strings, in text order, by id in s<slot>
    # (link_texts, JSON escaped; the evidence's first, ek of them). The LINK_SLOTS
    # longest strings that are not object keys take the slots; the rest stay in the
    # skeleton. Handles, digests, field names and values are stored once; links of one
    # type share a skeleton; the view puts the text back byte for byte. The checks the
    # v5 columns had hold on the skeleton and on each string.
    """CREATE TABLE IF NOT EXISTS link_texts (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL UNIQUE CHECK (json_valid('"' || text || '"')))""",
    """CREATE TABLE IF NOT EXISTS link_skeletons (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL UNIQUE CHECK (json_valid(text) AND json_type(text) = 'array'
                                         AND json_array_length(text) > 0))""",
    f"""CREATE TABLE IF NOT EXISTS link_rows (
        link_id TEXT PRIMARY KEY,
        link_type TEXT NOT NULL,
        from_object TEXT NOT NULL REFERENCES objects(object_id),
        to_object TEXT NOT NULL REFERENCES objects(object_id),
        tier TEXT NOT NULL CHECK ({_in('tier', TIERS)}),
        binding_kind TEXT NOT NULL CHECK ({_in('binding_kind', LINK_KINDS)}),
        link_contract TEXT,
        reason_id INTEGER NOT NULL REFERENCES link_skeletons(id),
        why_not TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(why_not) AND json_type(why_not) = 'array'),
        evidence_id INTEGER NOT NULL REFERENCES link_skeletons(id),
        reviewed_by TEXT,
        reviewed_at TEXT,
        review_action_id TEXT,
        ek INTEGER NOT NULL DEFAULT 0 CHECK (ek BETWEEN 0 AND {LINK_SLOTS}),
        {', '.join(f's{k} INTEGER REFERENCES link_texts(id)' for k in range(LINK_SLOTS))},
        {_MARKINGS},
        {_TIMES},
        CHECK ({' AND '.join(f's{k + 1} IS NULL OR s{k} IS NOT NULL' for k in range(LINK_SLOTS - 1))}),
        CHECK (from_object <> to_object),
        CHECK (tier <> 'reviewed' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL
                                      AND review_action_id IS NOT NULL)),
        CHECK (binding_kind <> 'text_match' OR tier IN ('stored_by_text_join', 'lead', 'contradicted')),
        CHECK (binding_kind <> 'mention' OR tier IN ('corroborated', 'lead', 'contradicted')))""",
    "CREATE INDEX IF NOT EXISTS links_from ON link_rows(from_object, link_type)",
    "CREATE INDEX IF NOT EXISTS links_to ON link_rows(to_object, link_type)",
    f"""CREATE VIEW IF NOT EXISTS links AS SELECT
        {', '.join(_link_column(c) + ' AS ' + c for c in LINK_COLUMNS)}
        FROM link_rows l""",
    *_LINK_TRIGGERS,
    # Identifier index behind plx.find: every normalized identifier (kinds from
    # ontology/schema/identifiers.json) and alias (renumberedTo, fmPoNumber, BC
    # number, externalDocumentNumber ...) with the cited field it came from.
    f"""CREATE TABLE IF NOT EXISTS identifiers (
        identifier_kind TEXT NOT NULL,
        normalized TEXT NOT NULL,
        raw TEXT NOT NULL,
        role TEXT NOT NULL CHECK ({_in('role', IDENTIFIER_ROLES)}),
        object_id TEXT NOT NULL REFERENCES objects(object_id),
        property TEXT NOT NULL,
        l1_handle TEXT NOT NULL,
        field_path TEXT NOT NULL CHECK {_FIELD_PATH},
        lineage TEXT NOT NULL CHECK {_LINEAGE},
        {_MARKINGS},
        {_TIMES},
        PRIMARY KEY (identifier_kind, normalized, object_id, l1_handle, field_path))""",
    "CREATE INDEX IF NOT EXISTS identifiers_by_value ON identifiers(normalized)",
    "CREATE INDEX IF NOT EXISTS identifiers_by_object ON identifiers(object_id)",
    # Business events from declared date columns (valid time) or capture windows.
    f"""CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        time_basis TEXT NOT NULL CHECK ({_in('time_basis', TIME_BASES)}),
        time TEXT,
        window_start TEXT,
        window_end TEXT,
        l1_handle TEXT NOT NULL,
        field_path TEXT NOT NULL CHECK {_FIELD_PATH},
        digest TEXT NOT NULL,
        lineage TEXT NOT NULL CHECK {_LINEAGE},
        {_MARKINGS},
        {_TIMES},
        CHECK ((time_basis = 'capture_observed' AND time IS NULL AND window_start IS NOT NULL
                AND window_end IS NOT NULL AND window_end >= window_start)
               OR (time_basis <> 'capture_observed' AND time IS NOT NULL)))""",
    # Event-to-object relations with OCEL 2.0 qualifiers.
    """CREATE TABLE IF NOT EXISTS event_objects (
        event_id TEXT NOT NULL REFERENCES events(event_id),
        object_id TEXT NOT NULL REFERENCES objects(object_id),
        qualifier TEXT NOT NULL,
        PRIMARY KEY (event_id, object_id, qualifier))""",
    "CREATE INDEX IF NOT EXISTS event_objects_by_object ON event_objects(object_id)",
)


# Fixed planner statistics (``sqlite_stat1``, never measured): a value row is reached by its
# source (a few rows per source), never by its layout (every object holding a field) when
# the source is known. Without them the planner read a version's values through the
# layout index, every object's cell of that field each time: the events phase of a build
# took 220 s instead of 16 s on a third of the live store (2026-10-27).
PLANNER_STATS = (("value_cells", "value_cells_by_layout", "10000000 200000"),
                 ("value_cells", "sqlite_autoindex_value_cells_1", "10000000 6 1"))


def create(conn: sqlite3.Connection) -> None:
    """Create every table and index (idempotent), enable foreign keys on ``conn``
    and stamp the store format."""
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        # One transaction: a connection in autocommit mode (the builder's) would
        # otherwise commit, and sync to disk, after every statement.
        if not conn.in_transaction:
            conn.execute("BEGIN")
        for statement in STATEMENTS:
            conn.execute(statement)
        conn.execute("INSERT INTO store_meta(key, value) VALUES ('format', ?) "
                     "ON CONFLICT(key) DO NOTHING", (FORMAT,))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'"
                        ).fetchone() is None:
            conn.execute("ANALYZE store_meta")   # creates sqlite_stat1 (a one-row table)
        conn.execute("DELETE FROM sqlite_stat1")
        conn.executemany("INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES (?, ?, ?)",
                         PLANNER_STATS)
    conn.execute("ANALYZE sqlite_master")   # this connection plans with them too
