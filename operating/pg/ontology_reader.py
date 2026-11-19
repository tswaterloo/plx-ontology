"""The read tools' store on Postgres: ``StoreReader`` over the ``ontology`` schema.

``PLX_ONTOLOGY_PG=1`` makes a case host (``operating.ontology_host.CaseHostTools``) read
``plx.find``, ``plx.object``, ``plx.around``, ``plx.timeline``, ``plx.read``'s store reads,
``plx.query`` (which composes those), the rest of the tool table and the proactive
engine's store reads (``operating.proactive_engine._connect``) from Postgres
(``PLX_PG_DSN``) instead of the served SQLite generation and its row-delta overlay: the
Postgres ontology takes every delta layer in place (``operating.pg.ontology_delta``), so
it is the current store and no overlay applies.

``PLX_ONTOLOGY_PG=only`` makes Postgres the source of truth: no SQLite generation is
read (none need be served), every reader above reads Postgres, and the word index is
Postgres's (migration 0010: values and document words). Postgres must hold an ontology
whole for that: every table of ``ontology_load.ORDER`` recorded loaded, the word index
among them, none deferred (``whole``). Otherwise a reader does not open and a request on an
open one raises :class:`PostgresNotWhole` (``check_whole``, asked on the reader's own
connection at each request): an error, never an empty or partial answer. A load or
migration running in place does not make it so (a delta layer and a migration apply in one
transaction; only ``ontology_load --replace`` empties tables).

:class:`PgStoreReader` is ``StoreReader`` with its connection a
``sqlite_compat.PgCompatConnection``: every query, with the reader's permission
predicates (audience keys, marking sets, value markings, the as-known-at time, the item
gate) in it, runs on Postgres as written. The values' word index (SQLite FTS5
``value_text``) is read from Postgres (migration 0010: a GIN index over the value cells)
once it is built: the same queries, with ``MATCH`` read as a ``tsquery``
(``sqlite_compat.translate``), and the value-cell spans that narrow them. The document
words (``chunk_text``) are read from the SQLite generation named by ``text_store`` (the
last one, kept read-only; ``search.*`` holds 14 % of its fields, ``docs/pg-cutover.md``),
as the value words are until their index is built; with no generation none match.
"""

from __future__ import annotations

import bisect
from contextlib import contextmanager
import itertools
import json
import os
import re
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Collection, Iterable, Iterator, Mapping, Sequence

from ontology.store_ddl import VALUE_COLUMNS
from operating.ontology_tools import StoreReader
from operating.ontology_tools_policy import _readable_text
from operating.pg.sqlite_compat import PgCompatConnection, Row

ENV = "PLX_ONTOLOGY_PG"


# ``PLX_ONTOLOGY_PG``'s value for Postgres as the only store (module docstring).
ONLY = "only"


class PostgresNotWhole(RuntimeError):
    """Postgres is the only store (``PLX_ONTOLOGY_PG=only``) and holds no whole ontology:
    the read is refused (never answered from a partial or empty store)."""


def enabled(env: Any = None) -> bool:
    return (os.environ if env is None else env).get(ENV) in ("1", ONLY)


def pg_only(env: Any = None) -> bool:
    """Whether Postgres is the only store read (no SQLite generation is served)."""
    return (os.environ if env is None else env).get(ENV) == ONLY


# Whether Postgres holds an ontology whole for serving it alone: (tables recorded loaded,
# the generation record, whether both word indexes are built and valid).
_WHOLE_SQL = (
    "SELECT ARRAY(SELECT substr(name, 7) FROM ontology.load_state WHERE name LIKE 'table:%%'), "
    "(SELECT value FROM ontology.load_state WHERE name = 'generation'), "
    "(SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'ontology' "
    "AND c.relname IN ('value_cells_words', 'chunk_words_words') AND i.indisvalid) = 2")


def whole_problem(row: Sequence[Any] | None) -> str | None:
    """Why Postgres cannot be served alone, from a ``_WHOLE_SQL`` row; None: it can."""
    from operating.pg.ontology_load import ORDER
    if row is None or row[1] is None:
        return "no_generation_loaded"
    generation = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    if generation.get("deferred"):
        return "tables_deferred:" + ",".join(sorted(generation["deferred"]))
    missing = sorted(set(ORDER) - set(row[0] or ()))
    if missing:
        return "tables_not_loaded:" + ",".join(missing)
    if not row[2]:
        return "word_index_not_built"
    return None


def whole(target: str | None = None) -> str | None:
    """``whole_problem`` of the database at ``target`` (default ``PLX_PG_DSN``); an
    unreachable one is ``unreachable``."""
    try:
        import psycopg
        with psycopg.connect(target or dsn(), connect_timeout=5) as pg:
            row = pg.execute(_WHOLE_SQL).fetchone()
    except Exception as error:  # noqa: BLE001 - not whole: refused, never empty
        return f"unreachable:{type(error).__name__}"
    return whole_problem(row)


def dsn() -> str:
    from operating.pg import config
    return config.dsn()


def served_digest(store_path: str | Path) -> str | None:
    """The content digest of the SQLite generation served at ``store_path``."""
    try:
        conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM store_meta WHERE key = 'content_digest'"
                               ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def store_meta(keys: Sequence[str], target: str | None = None) -> dict[str, str] | None:
    """``store_meta`` rows of the Postgres ontology (a delta layer keeps its freshness
    keys current), when it is whole (``whole``); else None, as an unreadable SQLite
    generation's are."""
    try:
        import psycopg
        with psycopg.connect(target or dsn(), connect_timeout=5) as pg:
            if whole_problem(pg.execute(_WHOLE_SQL).fetchone()) is not None:
                return None
            return dict(pg.execute("SELECT key, value FROM ontology.store_meta WHERE key = "
                                   "ANY(%s)", (list(keys),)).fetchall())
    except Exception:  # noqa: BLE001 - unreadable: no meta
        return None


def change_stamp(target: str | None = None) -> str | None:
    """What Postgres holds now, as one text that changes with every applied delta layer,
    migration or load: its generation's content digest and its data version
    (``ontology.sq_data_version``). When Postgres is the only store this stands where a
    generation's content digest stood (the gap inbox's runs, ``served_behind``); None when
    Postgres cannot say."""
    import psycopg

    try:
        with psycopg.connect(target or dsn(), connect_timeout=5) as pg:
            row = pg.execute(
                "SELECT (SELECT value FROM ontology.store_meta WHERE key = 'content_digest'), "
                "ontology.sq_data_version()").fetchone()
    except Exception:  # noqa: BLE001 - unreachable: no stamp
        return None
    return None if row is None or row[0] is None else f"{row[0]}@{row[1]}"


def serves(store_path: str | Path | None, target: str | None = None) -> bool:
    """Whether a host serving the generation at ``store_path`` reads Postgres: the switch
    is on and Postgres holds that very generation whole (``ready``). Between a new
    generation's publish and its swap into Postgres (``ontology_swap``) the host reads
    the SQLite generation and its overlay, so no reader sees an older generation than
    the one served. With Postgres the only store (``pg_only``) always: whatever file is
    (or is not) at ``store_path``, and the reader refuses to open when it is not whole."""
    if pg_only():
        return True
    if not enabled() or store_path is None:
        return False
    digest = served_digest(store_path)
    return digest is not None and ready(target, digest=digest)


def ready(target: str | None = None, *, digest: str | None = None) -> bool:
    """Whether the Postgres ontology holds a whole generation (every table of
    ``ontology_load.ORDER`` recorded loaded): a load in progress (``--replace`` empties
    the tables first) or an unreachable database is not read; the host serves the SQLite
    generation instead."""
    if os.environ.get("PLX_ONTOLOGY_PG_PARTIAL") == "1":
        return True   # diagnostics only: read a partial load
    try:
        import psycopg
        from operating.pg.ontology_load import OPTIONAL, ORDER
        with psycopg.connect(target or dsn(), connect_timeout=5) as pg:
            done = {row[0] for row in pg.execute(
                "SELECT substr(name, 7) FROM ontology.load_state WHERE name LIKE 'table:%%'")}
            generation = pg.execute("SELECT value FROM ontology.load_state WHERE name = "
                                    "'generation'").fetchone()
        if generation is None:
            return False
        if digest is not None and (generation[0] or {}).get("content_digest") != digest:
            return False
        deferred = set((generation[0] or {}).get("deferred") or ())
        return set(ORDER) - set(OPTIONAL) - deferred <= done
    except Exception:  # noqa: BLE001 - not ready: the SQLite generation serves
        return False


# How often a reader asks again whether the values' word index is built (a load
# finishing it, a ``--replace`` emptying it).
WORDS_RECHECK_SECONDS = 60.0
# Whether each word index is read here (the values', the documents'): built, valid, and
# its step recorded.
_WORD_INDEX_SQL = (
    "EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'ontology' "
    "AND c.relname = '{index}' AND i.indisvalid) "
    "AND EXISTS (SELECT 1 FROM ontology.load_state WHERE name = 'table:{step}')")
_WORDS_SQL = ("SELECT " + _WORD_INDEX_SQL.format(index="value_cells_words", step="value_words")
              + ", " + _WORD_INDEX_SQL.format(index="chunk_words_words", step="chunk_words"))
# ``TraverseTools.cell_span`` on Postgres: the word rows' numbers ((src << 20) | field,
# migration 0010) of the family's value sources, every version, as at most ``CELL_RUNS``
# runs. A family's sources are not one block: each delta layer appends its sources after
# every other family's, so after a day of layers the span from the family's first source
# to its last held most of the store (SharePoint, 11-18: 2% of the sources in a span over
# 56% of them, every word search of it reading mostly mail cells and dropping them one
# item gate at a time: plx.search past its 40 s limit). The runs split that span at its
# ``CELL_RUNS - 1`` widest gaps (the gaps between the family's own consecutive sources):
# every source of the family lies in a run; a run may hold another family's sources too
# (the search's audience term drops those, as before).
# ``PgStoreReader.family_cell_span``: the exact runs' time limit, and the span of every
# value cell (a superset of any family's) read by the key index past it
CELL_SPAN_EXACT_MS = 8000
CELL_RUNS = 32
WHOLE_SPAN_SQL = ("SELECT (SELECT MIN(src) FROM ontology.value_sources) << 20, "
                  "((SELECT MAX(src) FROM ontology.value_sources) << 20) | 1048575")


def cell_runs_sql(schema: str = "", *, owned: bool = False) -> str:
    """The family's sources split at their widest gaps (``CELL_RUNS``): each row a run's
    first source, the gap before it (NULL: the family's first source) and the family's last
    source (``cell_runs``). Named parameters family, low, high, runs (and owned: only these
    objects' sources); ``schema``: the tables' schema prefix (``ontology.``) or none."""
    mine = f"o.object_id = ANY(%(owned)s) AND " if owned else ""
    return (
        f"WITH fam AS (SELECT DISTINCT s.src FROM {schema}objects o "
        f"JOIN {schema}object_versions v ON v.object_id = o.object_id "
        f"JOIN {schema}value_sources s ON s.ver = v.ver WHERE {mine}"
        "((o.audience_key >= %(low)s AND o.audience_key < %(high)s) "
        "OR o.audience_key = %(family)s)), "
        "g AS (SELECT src, src - lag(src) OVER (ORDER BY src) AS gap FROM fam) "
        "SELECT src, gap, (SELECT MAX(src) FROM fam) FROM g WHERE gap IS NULL OR gap > 1 "
        "ORDER BY gap DESC NULLS FIRST, src LIMIT %(runs)s")


CELL_SPAN_SQL = cell_runs_sql()


def chunk_runs_sql(schema: str = "", *, owned: bool = False) -> str:
    """``cell_runs_sql`` over the document-text index: the family's chunk ids (every
    version's fields) split at their ``runs - 1`` widest gaps, rows as ``cell_runs`` reads
    them (``shift`` 0). The same named parameters. Each delta layer's chunks are numbered
    after every other family's, so a family's chunks are many blocks (SharePoint, 11-18:
    868,728 chunks from id 16.3M to 33.4M of 33.4M); its runs, not its span, narrow a
    word search of it."""
    mine = f"o.object_id = ANY(%(owned)s) AND " if owned else ""
    return (
        f"WITH fam AS (SELECT t.chunk_id FROM {schema}objects o "
        f"JOIN {schema}text_fields f ON f.object_id = o.object_id "
        f"JOIN {schema}text_chunks t ON t.field_id = f.field_id WHERE {mine}"
        "((o.audience_key >= %(low)s AND o.audience_key < %(high)s) "
        "OR o.audience_key = %(family)s)), "
        "g AS (SELECT chunk_id, chunk_id - lag(chunk_id) OVER (ORDER BY chunk_id) AS gap "
        "FROM fam) "
        "SELECT chunk_id, gap, (SELECT MAX(chunk_id) FROM fam) FROM g "
        "WHERE gap IS NULL OR gap > 1 ORDER BY gap DESC NULLS FIRST, chunk_id LIMIT %(runs)s")


CHUNK_SPAN_SQL = chunk_runs_sql()
# ``PgStoreReader.family_chunk_span``: the exact chunk runs' time limit (11-18 live, read
# only: SharePoint 3.9 s, Teams 2.2 s; mail past 40 s, read as not known)
CHUNK_SPAN_EXACT_MS = 8000
_NOT_KNOWN = object()


def cell_runs(rows: Sequence[Sequence[Any]], shift: int = 20
              ) -> tuple[tuple[int, int], ...] | None:
    """``cell_runs_sql``'s rows as row-number runs ((first source << 20, (last source << 20)
    | 1048575), ascending; ``shift`` 0: ``chunk_runs_sql``'s, first to last chunk id);
    None when the family holds no source."""
    if not rows:
        return None
    last = int(rows[0][2])
    starts = sorted((int(src), None if gap is None else int(gap)) for src, gap, _ in rows)
    out = []
    for i, (src, _gap) in enumerate(starts):
        end = last if i + 1 == len(starts) else starts[i + 1][0] - starts[i + 1][1]
        out.append((src << shift, (end << shift) | ((1 << shift) - 1)))
    return tuple(out)


def merge_runs(runs: Iterable[Sequence[int]], most: int | None = CELL_RUNS
               ) -> tuple[tuple[int, int], ...]:
    """Inclusive runs as their union, ascending, overlapping or touching ones joined; past
    ``most`` runs, the runs either side of the narrowest gaps joined (so a run only ever
    widens: every number in a given run stays in one)."""
    out: list[list[int]] = []
    for low, high in sorted((int(a), int(b)) for a, b in runs):
        if out and low <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], high)
        else:
            out.append([low, high])
    while most is not None and len(out) > max(most, 1):
        at = min(range(len(out) - 1), key=lambda i: (out[i + 1][0] - out[i][1], i))
        out[at][1] = out[at + 1][1]
        del out[at + 1]
    return tuple((a, b) for a, b in out)


# ``TraverseTools._slices("link")`` on Postgres without composing every link's evidence
# text: the generic read formats 25M packed texts (``ontology.link_text``: a skeleton and
# its slot texts) to find the few gated handles in them, minutes of a fact read. A packed
# evidence text whose skeleton is plain (no backslash, every placeholder a whole JSON
# string ``"%s"``, no other ``%`` than doubled ones, a JSON array once each placeholder is
# an empty string, as many placeholders as slots) and whose slot texts are plain (no
# quote, backslash or control character) is a JSON array whose elements are the
# skeleton's literals and the slot texts themselves: its gated handles are the skeleton's
# gated literals and its gated slot texts, read by join. Every other row (a plain
# ``evidence_handles``, an odd skeleton, an odd slot: ``%(odd)s::text[]``) is read as before. Rows
# are (name, markings, gated JSON list or NULL, end) like the generic read's; the folding
# keeps the latest end per marking and per handle, so the same facts.
_LINK_SKELETONS = (
    "SELECT id, holes, ok, CASE WHEN ok THEN ontology.sq_l9_gated(%(schemes)s::text[], lit) END AS lit "
    "FROM (SELECT id, holes, lit, CASE WHEN bare AND pg_input_is_valid(lit, 'jsonb') "
    "THEN jsonb_typeof(lit::jsonb) = 'array' ELSE false END AS ok "
    "FROM (SELECT id, (length(text) - length(replace(text, '\"%%s\"', ''))) / 4 AS holes, "
    "replace(replace(text, '\"%%s\"', '\"\"'), '%%%%', '%%') AS lit, "
    "strpos(replace(replace(text, '\"%%s\"', '\"\"'), '%%%%', ''), '%%') = 0 "
    "AND strpos(text, chr(92)) = 0 AS bare FROM ontology.link_skeletons) s) x")
_LINK_END = ("MAX(CASE WHEN l.superseded_at IS NULL THEN '~' "
             "WHEN ontology.sq_l9_time(l.superseded_at) IS NULL THEN '~' "
             "ELSE ontology.sq_l9_time(l.superseded_at) END)")
_ODD_TEXT = r"""text ~ '["\\\x01-\x1f]'"""
# each part's latest end per (name, markings, gated) read by the workers that read it
# each part's latest end per (name, markings, gated) read by the workers that read it
def link_slice_sql(ranged: bool = False, owned: bool = False, typed: bool = False,
                   c: str = ' COLLATE "C"') -> str:
    """``LINK_SLICE_SQL``; ``ranged``: of the links whose id lies in
    [``%(lo)s``, ``%(hi)s``) alone (either end NULL: open), byte order (``c``: the
    collation that is, ``ontology.pg_byte_order.key_collate``; by the key index), so a
    full read splits into parts read side by
    side (``ontology_tools_traverse._read_pg_facts_parallel``); ``owned``: of the links
    from or to the objects ``%(owned)s`` alone, by their indexes (a delta layer's
    objects, ``ontology_tools_traverse.catch_up_pg_facts``)."""
    part = ("" if not ranged else
            f" AND (%(lo)s::text IS NULL OR l.link_id{c} >= %(lo)s) "
            f"AND (%(hi)s::text IS NULL OR l.link_id{c} < %(hi)s)")
    rows, lead = "ontology.link_rows", ""
    if typed:
        # one link type's rows by its index (``link_rows_by_type``): a host reading the
        # slice of the types a call needs before the whole one is kept
        rows = "lr"
        lead = ("WITH lr AS MATERIALIZED (SELECT * FROM ontology.link_rows "
                "WHERE link_type = %(name)s) ")
    elif owned:
        # the objects' links read once by the from and the to index (left to the planner,
        # an OR over thousands of ids scanned all 25M rows, four times)
        rows = "lr"
        lead = ("WITH lr AS MATERIALIZED (SELECT * FROM ontology.link_rows "
                "WHERE from_object = ANY(%(owned)s::text[]) UNION ALL "
                "SELECT * FROM ontology.link_rows WHERE to_object = ANY(%(owned)s::text[]) "
                "AND NOT from_object = ANY(%(owned)s::text[])) ")
    return lead + (
        "SELECT name, markings, gated, MAX(until) FROM ("
        # every row's markings
        f"SELECT l.link_type AS name, l.markings, NULL AS gated, {_LINK_END} AS until "
        f"FROM {rows} l WHERE TRUE{part} GROUP BY 1, 2 "
        "UNION ALL "
        # the gated handles of a row not read by join, as before
        "SELECT l.link_type, l.markings, ontology.sq_l9_gated(%(schemes)s::text[], "
        "coalesce(l.evidence_handles, ontology.link_text(l.evidence_id, l.slots, 1, l.ek))), "
        f"{_LINK_END} FROM {rows} l "
        f"LEFT JOIN ({_LINK_SKELETONS}) k ON k.id = l.evidence_id "
        "WHERE (l.evidence_id IS NULL OR k.ok IS NOT TRUE "
        f"OR k.holes <> cardinality(l.slots[1:l.ek]) OR l.link_id = ANY(%(odd)s::text[])){part} "
        "GROUP BY 1, 2, 3 "
        "UNION ALL "
        # a plain packed row's gated slot texts
        f"SELECT l.link_type, l.markings, to_json(ARRAY[t.text])::text, {_LINK_END} "
        f"FROM {rows} l JOIN ({_LINK_SKELETONS}) k ON k.id = l.evidence_id "
        "AND k.ok AND k.holes = cardinality(l.slots[1:l.ek]) "
        "CROSS JOIN LATERAL unnest(l.slots[1:l.ek]) u(id) "
        "JOIN ontology.link_texts t ON t.id = u.id AND strpos(t.text, '://') > 0 "
        "AND split_part(t.text, '://', 1) = ANY(%(schemes)s::text[]) "
        f"WHERE NOT l.link_id = ANY(%(odd)s::text[]){part} GROUP BY 1, 2, 3 "
        "UNION ALL "
        # a plain packed row's skeleton's gated literals
        f"SELECT l.link_type, l.markings, k.lit, {_LINK_END} "
        f"FROM {rows} l JOIN ({_LINK_SKELETONS}) k ON k.id = l.evidence_id "
        "AND k.ok AND k.holes = cardinality(l.slots[1:l.ek]) AND k.lit IS NOT NULL "
        f"WHERE NOT l.link_id = ANY(%(odd)s::text[]){part} GROUP BY 1, 2, 3) r GROUP BY 1, 2, 3")


# each part's latest end per (name, markings, gated) read by the workers that read it
LINK_SLICE_SQL = link_slice_sql()
# The link slice kept in Postgres (migration 0028, CC-P-07: ``ontology.link_marks``), in
# ``link_slice_sql``'s row shape: per (type, markings) the latest end of every row (handle
# '') and of each gated handle. Read only when ``ontology.link_marks_vouched`` holds for
# these gated schemes (kept at the data version, generation and link_rows change counter
# Postgres has now); otherwise the slice is read from link_rows as before.
LINK_MARKS_KEPT_SQL = "SELECT ontology.link_marks_vouched(%(schemes)s::text[])"
LINK_MARKS_SQL = (
    "SELECT link_type, markings, CASE WHEN handle = '' THEN NULL "
    "ELSE to_json(ARRAY[handle])::text END, until FROM ontology.link_marks "
    "WHERE %(name)s::text IS NULL OR link_type = %(name)s")
# The links one of whose evidence slots is an odd text (read as before); more odd texts
# than this and the slice is read the generic way.
LINK_ODD_SQL = (
    f"SELECT array_agg(id) FROM (SELECT id FROM ontology.link_texts WHERE {_ODD_TEXT} "
    "LIMIT %(cap)s) t")
LINK_ODD_ROWS_SQL = (
    "SELECT coalesce(array_agg(link_id), '{}') FROM ontology.link_rows "
    "WHERE evidence_id IS NOT NULL AND slots[1:ek] && %(texts)s::bigint[]")
LINK_ODD_CAP = 1000
# ``LINK_ODD_ROWS_SQL`` among the links from or to the objects ``%(owned)s`` alone, by their
# indexes and the slot texts' key (the whole-table pattern scan took minutes per layer).
LINK_ODD_OWNED_SQL = (
    "SELECT coalesce(array_agg(DISTINCT l.link_id), '{}') FROM ontology.link_rows l "
    "CROSS JOIN LATERAL unnest(l.slots[1:l.ek]) u(id) "
    f"JOIN ontology.link_texts t ON t.id = u.id AND t.{_ODD_TEXT} "
    "WHERE l.evidence_id IS NOT NULL AND (l.from_object = ANY(%(owned)s::text[]) "
    "OR l.to_object = ANY(%(owned)s::text[]))")
# A word match's value row: SQLite joins the cell rowid; here the cell's key, so the value
# row is one index probe (``row_no`` is computed).
VALUE_WORD_JOIN = ("value_text f JOIN object_values_numbered v "
                   "ON v.src = f.src AND v.field = f.field")
_NUMBERED = ", ".join(f"v.{c}" for c in ("row_no", *VALUE_COLUMNS))
# A window's candidates are joined as many at a time as rows are still wanted, and twice
# as many again each time fewer passed, up to this many (in order: a page stops early).
CANDIDATES = 2000
# The value rows of a window's candidate cells (``:cells``: ``(src << 20) | field``, in
# their row numbers' order): each one probe of the cell's key; ``row_no`` is the cell's key
# until ``PgStoreReader.text_matches`` puts its row number in its place.
_CANDIDATE_VALUES = (
    "SELECT k.id AS row_no, " + ", ".join(f"v.{c}" for c in VALUE_COLUMNS)
    + ", o.object_type, o.binding_id AS object_binding, o.audience_key "
    "FROM (SELECT key AS pos, value AS id FROM json_each(:cells)) k "
    "JOIN object_values_typed v ON v.src = (k.id >> 20) AND v.field = (k.id & 1048575) "
    "JOIN objects o ON o.object_id = v.object_id WHERE ")
# The chunks of a window's candidate chunk ids (``:rows``): ``StoreReader.chunk_matches``.
_CANDIDATE_CHUNKS = (
    "SELECT t.chunk_id AS row_no, t.body, fd.object_id, fd.version_handle, "
    "fd.citation, fd.field_path, fd.sender_class, o.object_type, "
    "o.binding_id AS object_binding, o.audience_key "
    "FROM (SELECT value AS chunk_id FROM json_each(:rows)) f "
    "JOIN text_chunks t ON t.chunk_id = f.chunk_id "
    "JOIN text_fields fd ON fd.field_id = t.field_id "
    "JOIN objects o ON o.object_id = fd.object_id "
    "JOIN object_versions v ON v.l1_handle = fd.version_handle "
    "AND v.object_id = fd.object_id WHERE ")


# ---------------------------------------------------------------- word matches in order
# A word match is read in the order SQLite's FTS5 reads it (its rowid: a value row's
# number, ``(src << 20) | its field's place``, migration 0011; a chunk's id), a window of
# that order at a time, so a page stops early instead of reading every match of a common
# word (1.0 s a page median on the 444 sample queries, 13.5 s the worst, before). Each
# window is read one of two ways, chosen from the words' statistics (``pg_stats``: the
# value cells' word index and ``chunk_words.words``, 10,000 frequent words each):
# * ``index``: the GIN index's match intersected with the window's key range (a
#   BitmapAnd: the word's postings and the range's keys, then only the window's rows);
#   a rare word's window is the whole store (one read);
# * ``walk``: the window's rows in key order, each tested (a common word: its first page
#   is a few thousand rows; a value is first tested by a substring check of its plain
#   terms, 0.6 us a row, before its words are cut, 9 us).
# A window that holds too few matches doubles the next one.
# A window's key range is a bitmap of the value cells' key index (``value_cells_pkey``,
# ~10 cells a source), so a value window costs its range's cells, read again with every
# window, beside the word's postings: 7.5-35 s a window on the served store (2026-11-13,
# a 24,000-match customer name: 5 windows, 101 s), while the word's whole match read 0.2 s
# (20,000 matches), 0.4-0.6 s (50,000-100,000), 2.4-7.4 s (450,000-540,000). So a value
# word estimated to match up to ``WHOLE_VALUE_MATCHES`` is read in one window; and a read
# of named properties' cells only (``fields``: a name pass), where few matches pass and
# a page reads every window anyway, is read in one window unless it is walked.
WINDOW_MATCHES = 600       # the matches a window is sized to hold
WHOLE_MATCHES = 20_000     # a word estimated to match no more is read in one window
WHOLE_VALUE_MATCHES = 100_000   # the same for the value cells (above)
KEPT_MATCHES = 20_000      # a window of no more matches is kept for the next page
KEPT_WINDOWS = 8           # windows read kept per data version (the next page's)
# a window is walked when walking it costs less than the index's postings (per row,
# relative to one posting decoded): ``matches ** 2 > batch * rows * WALK_COST``
WALK_COST = {"value_text": 60.0, "value_text_plain": 4.0, "chunk_text": 8.0}
_TERM = re.compile(r"'((?:[^']|'')*)'(:\*)?|([&|!()])")
_PLAIN_TERM = re.compile(r"[a-z0-9]+")
_FACTS_SQL = {
    "value_text": ("SELECT min(src), max(src), (SELECT reltuples FROM pg_class "
                   "WHERE oid = 'ontology.value_cells_words'::regclass) FROM ontology.value_cells"),
    "chunk_text": ("SELECT min(chunk_id), max(chunk_id), (SELECT reltuples FROM pg_class "
                   "WHERE oid = 'ontology.chunk_words'::regclass) FROM ontology.chunk_words"),
}
_STATS_SQL = {
    "value_text": "tablename = 'value_cells_words'",
    "chunk_text": "tablename = 'chunk_words' AND attname = 'words'",
}


def _parse_tsquery(text: str) -> Any:
    """The tree of a ``words.tsquery`` text: ('term', word, prefix) | ('&' | '|', a, b) |
    ('!', a)."""
    tokens = [(m.group(3), m.group(1), bool(m.group(2))) for m in _TERM.finditer(text)]
    at = 0

    def primary() -> Any:
        nonlocal at
        op, word, prefix = tokens[at]
        at += 1
        if op == "(":
            out = alternatives()
            at += 1   # ')'
            return out
        if op == "!":
            return ("!", primary())
        return ("term", word.replace("''", "'").replace("\\\\", "\\"), prefix)

    def conjunction() -> Any:
        nonlocal at
        out = primary()
        while at < len(tokens) and tokens[at][0] == "&":
            at += 1
            out = ("&", out, primary())
        return out

    def alternatives() -> Any:
        nonlocal at
        out = conjunction()
        while at < len(tokens) and tokens[at][0] == "|":
            at += 1
            out = ("|", out, conjunction())
        return out
    return alternatives()


def _plain_terms(tree: Any) -> list[str] | None:
    """The terms every match holds as a substring of its lower-cased text when the text is
    ASCII (a conjunction of ASCII terms), else None."""
    if tree[0] == "term":
        return [tree[1]] if _PLAIN_TERM.fullmatch(tree[1]) else None
    if tree[0] == "&":
        left, right = _plain_terms(tree[1]), _plain_terms(tree[2])
        return None if left is None or right is None else left + right
    return None


def _candidates(windows: Iterator[list[tuple[int, int]]],
                read: Any, batch: int) -> list[Any]:
    """Up to ``batch`` rows ``read(candidates, wanted)`` returns over the windows'
    candidates, in their order: a slice as long as the rows still wanted, doubled after a
    slice that passed fewer (``CANDIDATES`` at most)."""
    out: list[Any] = []
    size = batch
    for window in windows:
        start = 0
        while start < len(window):
            part = window[start:start + size]
            start += len(part)
            got = read(part, batch - len(out))
            out.extend(got)
            if len(out) >= batch:
                return out
            if len(got) < len(part):
                size = min(size * 2, CANDIDATES)
    return out


class WordWindows:
    """The ordered word matches of one Postgres reader (module comment above): its facts
    (field order, kinds, key ranges, word statistics) kept per data version."""

    def __init__(self, conn: PgCompatConnection) -> None:
        self.conn = conn
        self.version: Any = None
        self.facts: dict[str, Any] = {}

    def _pg(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with self.conn._lock, self.conn._pg.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def refresh(self) -> dict[str, Any]:
        """The facts of the data version Postgres holds now (read again when it moved)."""
        version = self._pg("SELECT ontology.sq_data_version()")[0][0]
        if version == self.version:
            return self.facts
        fields = self._pg("SELECT field, property, field_path FROM ontology.value_fields")
        if self._pg("SELECT to_regclass('ontology.value_field_order') IS NOT NULL")[0][0]:
            # migration 0011's order (``value_field_order``: bytewise, as SQLite's BINARY)
            ordered = sorted(fields, key=lambda r: (r[1].encode(), r[2].encode(), r[0]))
            rank = {field: at for at, (field, _p, _f) in enumerate(ordered, 1)}
        else:   # before it, the views number a cell by its field
            rank = {field: field for field, _p, _f in fields}
        by_property: dict[str, list[int]] = {}
        for field, prop, _path in fields:
            by_property.setdefault(prop, []).append(field)
        kinds = dict(self._pg("SELECT kind, value_type FROM ontology.value_kinds"))
        facts: dict[str, Any] = {
            "rank": rank, "field_of": {at: field for field, at in rank.items()},
            "by_property": by_property,
            "indexed": frozenset(k for k, t in kinds.items()
                                 if t in ("text", "identifier", "enum")),
            "text": frozenset(k for k, t in kinds.items() if t == "text"),
            "estimates": {}, "kept": {}}
        for table, sql in _FACTS_SQL.items():
            low, high, rows = self._pg(sql)[0]
            facts[table] = (low, high, max(float(rows or 0), 1.0))
            stats = self._pg("SELECT most_common_elems::text::text[], most_common_elem_freqs "
                             "FROM pg_stats WHERE schemaname = 'ontology' AND "
                             + _STATS_SQL[table])
            words, freqs = (stats[0] if stats and stats[0][0] else ([], []))
            floor = float(freqs[len(words)]) if len(freqs) > len(words) else 0.0
            facts[f"stats:{table}"] = (dict(zip(words, map(float, freqs))), sorted(words),
                                       floor)
        self.version, self.facts = version, facts
        return facts

    def estimate(self, table: str, query: str) -> float:
        """The rows of ``table``'s word index ``query`` (a tsquery text) is estimated to
        match, from the words' statistics (a word not among them: half the least frequent
        one's share, as Postgres estimates it)."""
        facts = self.facts
        held = facts["estimates"].get((table, query))
        if held is not None:
            return held
        freq, ordered, floor = facts[f"stats:{table}"]
        rows = facts[table][2]

        def share(tree: Any) -> float:
            if tree[0] == "term":
                if not tree[2]:
                    return freq.get(tree[1], floor / 2)
                start = bisect.bisect_left(ordered, tree[1])
                total = floor / 2
                for word in itertools.islice(ordered, start, None):
                    if not word.startswith(tree[1]):
                        break
                    total += freq[word]
                return min(total, 1.0)
            if tree[0] == "&":
                return min(share(tree[1]), share(tree[2]))
            if tree[0] == "|":
                return min(share(tree[1]) + share(tree[2]), 1.0)
            return 1.0 - share(tree[1])
        try:
            out = share(_parse_tsquery(query)) * rows
        except (IndexError, TypeError):
            out = rows
        facts["estimates"][(table, query)] = out
        return out

    def windows(self, table: str, query: str, after: int,
                spans: Sequence[tuple[int, int]] | None = None, *, want: int = WINDOW_MATCHES,
                fields: Collection[int] | None = None,
                kinds: Collection[int] | None = None) -> Iterator[list[tuple[int, int]]]:
        """Each window's matches of the tsquery ``query`` past the row number ``after``
        (within ``spans``, inclusive row number ranges, when given), ascending:
        (row number, key) pairs, the key a value cell's ``(src << 20) | field`` (only
        cells of ``fields`` and ``kinds`` when given) or a chunk id. A generator: it reads
        a window when the caller asks for the next."""
        facts = self.refresh()
        low, high, rows = facts[table]
        if low is None:
            return
        value = table == "value_text"
        shift = 20 if value else 0
        ranges = sorted((max(low, a >> shift), min(high, b >> shift)) for a, b in
                        (spans if spans is not None else [(low << shift, high << shift)]))
        ranges = [(a, b) for a, b in ranges if b >= a]
        start = after >> shift if after > 0 else low
        span = sum(b - a + 1 for a, b in ranges)
        whole = high - low + 1
        if not span or start > ranges[-1][1]:
            return
        matches = max(self.estimate(table, query) * span / whole, 1.0)
        tree = _parse_tsquery(query)
        plain = _plain_terms(tree) if value else None
        cost = WALK_COST["value_text_plain" if plain else table]
        walk = matches * matches > want * rows * span / whole * cost
        # the windows' grid is fixed by the estimate (a window twice the one before), so
        # the next page reads the same window again (kept: ``self.kept``) and skips those
        # before its ``after`` unread
        one = not walk and (matches <= WHOLE_MATCHES or value and (
            matches <= WHOLE_VALUE_MATCHES or fields is not None))
        width = span if one else max(int(span * want / matches), 1)
        rank, indexed = facts["rank"], facts["indexed"]
        allowed = None if kinds is None else frozenset(kinds)
        field_list = None if fields is None else sorted(fields)
        for a, b in ranges:
            lo = a
            while lo <= b:
                hi = min(b, lo + width - 1)
                width *= 2
                if hi < start:
                    lo = hi + 1
                    continue
                key = (table, query, lo, hi, walk, None if field_list is None
                       else tuple(field_list), allowed)
                got = facts["kept"].get(key)
                if got is None:
                    got = self._read(table, query, lo, hi, low, high, walk, plain, field_list)
                    if value:
                        got = sorted(((src << 20) | rank[field], (src << 20) | field)
                                     for src, field, kind in got
                                     if kind in indexed and (allowed is None or kind in allowed))
                    else:
                        got = sorted((chunk, chunk) for (chunk,) in got)
                    kept = facts["kept"]
                    if len(got) <= KEPT_MATCHES:
                        try:
                            while len(kept) >= KEPT_WINDOWS:
                                kept.pop(next(iter(kept)))
                        except (KeyError, RuntimeError, StopIteration):
                            kept.clear()   # another thread's: start again
                        kept[key] = got
                lo = hi + 1
                yield [pair for pair in got if pair[0] > after] if after > 0 else got

    def _read(self, table: str, query: str, lo: int, hi: int, low: int, high: int,
              walk: bool, plain: list[str] | None, fields: list[int] | None) -> list[tuple]:
        """One window's matching rows (``windows``): the GIN index's match (with the
        window's key range as a second bitmap unless it is the whole store), or the
        window's rows walked in key order."""
        value = table == "value_text"
        key = "c.src" if value else "chunk_id"
        bounded = walk or lo > low or hi < high
        if value:
            test = "ontology.sq_words(c.value) @@ %s::tsquery"
            params: list[Any] = [query]
            if plain and walk:
                test = ("(c.value ~ '[^\\x01-\\x7f]' OR ("
                        + " AND ".join(["strpos(lower(c.value), %s) > 0"] * len(plain))
                        + ")) AND " + test)
                params = [*plain, query]
            sql = ("SELECT c.src, c.field, c.kind FROM ontology.value_cells c WHERE " + test
                   + " AND c.value_num IS NULL AND c.value IS NOT NULL "
                   "AND NOT EXISTS (SELECT 1 FROM ontology.packed_sources p WHERE p.src = c.src)")
            if fields is not None:
                # a filter on the matched rows, never an index condition: ``field`` is the
                # key's second column, so a bitmap of it scanned the whole key (0.5 s)
                sql += " AND (c.field + 0) = ANY(%s)"
                params.append(fields)
        else:
            sql = "SELECT chunk_id FROM ontology.chunk_words WHERE words @@ %s::tsquery"
            params = [query]
        if bounded:
            sql += f" AND {key} >= %s AND {key} <= %s"
            params += [lo, hi]
        off = "enable_bitmapscan" if walk else "enable_indexscan"
        with self.conn._lock, self.conn._pg.cursor() as cur:
            cur.execute(f"SELECT set_config('{off}', 'off', false), "
                        "set_config('enable_seqscan', 'off', false)")
            try:
                cur.execute(sql, params)
                return cur.fetchall()
            finally:
                cur.execute(f"RESET {off}; RESET enable_seqscan")

class PgStoreReader(StoreReader):
    """``StoreReader`` over Postgres (module docstring)."""

    VALUES_FIRST = True   # values kept by property (``value_cells_by_field``)

    def narrowed_ids(self, object_type: str, narrowing: Sequence[tuple[str, dict[str, Any]]],
                     marks: str) -> list[str] | None:
        """``StoreReader.narrowed_ids`` bounded by the thread's SQL deadline
        (``call_limits.sql_deadline``), which Postgres statements do not otherwise see:
        the statement's own ``statement_timeout`` for this read only, cancelled at the
        deadline (``sqlite3.OperationalError``, as a SQLite interrupt)."""
        from operating import call_limits
        left = call_limits.seconds_left()
        if left is None:
            return super().narrowed_ids(object_type, narrowing, marks)
        if left <= 0:
            raise sqlite3.OperationalError("interrupted")
        pg = self.conn._pg
        with self.conn._lock:
            (before,) = pg.execute("SHOW statement_timeout").fetchone()
            pg.execute(f"SET statement_timeout = {max(1, int(left * 1000))}")
            try:
                return super().narrowed_ids(object_type, narrowing, marks)
            finally:
                pg.execute(f"SET statement_timeout = '{before}'")

    def __init__(self, conn: PgCompatConnection, text_reader: StoreReader | None = None,
                 *, only: bool = False) -> None:
        super().__init__(conn)   # type: ignore[arg-type]
        self.text_reader = None if only else text_reader
        # Postgres is the only store (``pg_only``): never a SQLite generation, and a store
        # that is not whole refuses every request (``check_whole``)
        self.only = only
        if self.text_reader is not None:
            conn.mirror = self.text_reader.conn
        # which word index this store reads, known before its first fact is read: learnt
        # later, it reset the facts read so far (a new store state) part way through
        # ``prime``, so only its last fact was ever kept
        self._word_state()

    @classmethod
    def open(cls, dsn: str | Path | None = None, *, text_store: str | Path | None = None,
             only: bool | None = None,
             statement_timeout_ms: int | None = None,
             temp_limit: str | None = None) -> "PgStoreReader":
        """The reader over Postgres; ``text_store``: the SQLite generation word queries
        read until the word index is built here. ``only`` (default ``pg_only()``): no
        SQLite generation is read and a store that is not whole raises
        :class:`PostgresNotWhole` here."""
        from operating.pg import config
        only = pg_only() if only is None else only
        text = None
        if not only and text_store is not None and Path(text_store).is_file():
            text = StoreReader.open(text_store)
        limit: dict[str, Any] = {} if statement_timeout_ms is None else {
            "statement_timeout_ms": statement_timeout_ms}
        if temp_limit is not None:
            limit["temp_limit"] = temp_limit   # each statement's temp files (a facts read)
        # CC-C-04: with the switch on, ``links`` is ``ontology_seen.links``, without the
        # mention repeats ``operating.pg.mention_repeats`` marked (off: ``ontology.links``);
        # row 14: ``PLX_PG_BARE_MENTIONS=1`` adds the bare-token mention leads
        # (``operating.pg.bare_mentions``)
        from operating.pg import bare_mentions
        limit["search_path"] = bare_mentions.search_path()
        # a reader never sees the same-person links ``person_link_guard`` hides
        reader = cls(PgCompatConnection(str(dsn) if dsn else config.dsn(), link_guard=True,
                                        **limit), text,
                     only=only)
        if only:
            try:
                reader.check_whole()
            except BaseException:
                reader.close()
                raise
        try:
            row = reader.conn.execute("SELECT value FROM load_state WHERE name = "
                                      "'generation'").fetchone()
            reader.deferred = frozenset(json.loads(row[0]).get("deferred") or ()) \
                if row and isinstance(row[0], str) else frozenset(
                    (row[0] or {}).get("deferred") or ()) if row else frozenset()
        except (sqlite3.Error, ValueError, AttributeError):
            reader.deferred = frozenset()
        return reader

    deferred: frozenset = frozenset()

    @property
    def windows(self) -> WordWindows:
        """This reader's ordered word matches (``WordWindows``), made on first use."""
        held = self.__dict__.get("_windows")
        if held is None or held.conn is not self.conn:
            held = self.__dict__["_windows"] = WordWindows(self.conn)
        return held

    def sensitive_versions(self, pairs: Sequence[tuple[str, str]]) -> set[str]:
        """``StoreReader.sensitive_versions`` where the chunks are: the SQLite
        generation's while the load deferred ``text_chunks``, else Postgres."""
        if "text_chunks" in self.deferred and self.text_reader is not None:
            with self.text_reader.lock:
                return self.text_reader.sensitive_versions(pairs)
        return super().sensitive_versions(pairs)

    def object_chunks(self, scope: Any, versions: Mapping[str, str],
                      hints: Mapping[str, Sequence[str]] | None = None) -> list[Any]:
        """``StoreReader.object_chunks`` (the hinted fields' first chunks, else an
        object's first fields; never every chunk): from the SQLite generation while the
        load deferred ``text_chunks`` (``ontology_load.DEFERRABLE``), else Postgres."""
        if "text_chunks" in self.deferred and self.text_reader is not None:
            with self.text_reader.lock:
                rows = self.text_reader.object_chunks(scope, versions, hints)
            return self.permitted_here(scope, rows)
        return super().object_chunks(scope, versions, hints)

    def check_whole(self) -> None:
        """Raise :class:`PostgresNotWhole` unless Postgres holds an ontology whole
        (``whole_problem``), read on this reader's connection: one small query, asked
        by the host at every request when Postgres is the only store."""
        with self.lock, self.conn._lock:
            try:
                row = self.conn._pg.execute(_WHOLE_SQL.replace("%%", "%")).fetchone()
            except Exception as error:  # noqa: BLE001 - unreadable: refused
                raise PostgresNotWhole(f"unreachable:{type(error).__name__}") from error
        problem = whole_problem(row)
        if problem is not None:
            raise PostgresNotWhole(problem)

    def state_stamp(self) -> Any:
        """The Postgres ontology's data version (new with every applied delta layer,
        migration or load): what a host keys what it decided over the store by."""
        with self.lock:
            return ("postgres", self.conn.execute("PRAGMA data_version").fetchone()[0])

    def _word_state(self) -> tuple[bool, bool]:
        """(value words, document words) read on Postgres (``_WORDS_SQL``), asked again
        every ``WORDS_RECHECK_SECONDS``; a database that cannot say has neither."""
        held = getattr(self, "_words", None)
        now = time.monotonic()
        if held is None or now - held[0] > WORDS_RECHECK_SECONDS:
            with self.lock, self.conn._lock:
                try:
                    row = self.conn._pg.execute(_WORDS_SQL).fetchone()
                    loaded = (bool(row[0]), bool(row[1]))
                except Exception:  # noqa: BLE001 - not built: the SQLite generation's
                    loaded = (False, False)
            tables = tuple(t for t, on in zip(("value_text", "chunk_text"), loaded) if on)
            if tables != self.conn.words:
                # the facts kept per store state (the cell spans among them) were read
                # from the other word index: a new state
                self.conn.total_changes += 1
            held = self._words = (now, loaded)
            self.conn.words = tables
        return held[1]

    def words_loaded(self) -> bool:
        """Whether value word queries read Postgres (``_word_state``)."""
        return self._word_state()[0]

    def chunk_words_loaded(self) -> bool:
        """Whether document word queries read Postgres (``_word_state``)."""
        return self._word_state()[1]

    def _no_words(self, which: str) -> None:
        """A word query with no index to read: Postgres alone refuses it."""
        if self.only:
            raise PostgresNotWhole(f"word_index_not_built:{which}")

    def permitted_here(self, scope: Any, rows: Sequence[Any]) -> list[Any]:
        """``rows`` (document chunks the SQLite generation's word index found, already
        checked there) that Postgres also permits: Postgres holds the field with markings
        the reader holds and its object under an audience the reader sees. A migration in
        place re-marks and re-keys Postgres only (``ontology_migrate``), so a generation's
        older markings never decide alone; a field Postgres does not hold is withheld."""
        if not rows:
            return []
        ids = sorted({row["object_id"] for row in rows})
        with self.lock:
            held = {(r[0], r[1], r[2], r[3]): (r[4], r[5]) for r in self.conn.execute(
                "SELECT f.object_id, f.version_handle, f.citation, f.field_path, f.markings, "
                "o.audience_key FROM text_fields f JOIN objects o ON o.object_id = f.object_id "
                "WHERE f.object_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))}
        marks, audiences = set(scope.marking_sets), set(scope.audience_keys)
        out = []
        for row in rows:
            got = held.get((row["object_id"], row["version_handle"], row["citation"],
                            row["field_path"]))
            if got is not None and got[0] in marks and got[1] in audiences:
                out.append(row)
        return out

    @property
    def word_conn(self) -> Any:
        """The connection value word queries run on (``value_text`` and the value-cell
        spans that narrow them): this one once the values' word index is built; before,
        the SQLite generation's, with every function registered on this reader's
        connection; this one when there is none (its word queries then raise)."""
        if self.words_loaded() or self.text_reader is None:
            if not self.words_loaded():
                self._no_words("value_text")
            return self.conn
        return self.text_reader.conn

    @property
    def chunk_conn(self) -> Any:
        """The connection document word queries (``chunk_text``) run on: this one once the
        document word index is built; before, the SQLite generation's; this one when there
        is none (they raise: a cut search; Postgres alone: ``PostgresNotWhole``)."""
        if self.chunk_words_loaded() or self.text_reader is None:
            if not self.chunk_words_loaded():
                self._no_words("chunk_text")
            return self.conn
        return self.text_reader.conn

    @property
    def value_word_join(self) -> str:
        """``StoreReader.value_word_join`` on the connection ``word_conn`` reads."""
        return VALUE_WORD_JOIN if self.words_loaded() else StoreReader.value_word_join

    @property
    def cell_span_sql(self) -> str | None:
        """``TraverseTools.cell_span``'s query when the word index is read here."""
        return CELL_SPAN_SQL if self.words_loaded() else None

    def family_cell_span(self, family: str) -> tuple[tuple[int, int], ...] | None:
        """``TraverseTools.cell_span`` of ``family`` on Postgres: its runs
        (``CELL_SPAN_SQL``, ``cell_runs``) within ``CELL_SPAN_EXACT_MS`` (or the thread's
        SQL deadline, when sooner); past it, every value cell's span (``WHOLE_SPAN_SQL``,
        two key index probes) as one run. Runs only narrow a word search that applies the
        reader's audiences itself, so a wider one reads more rows and never fewer: the
        bulk family (mail: every version of millions of objects joined) ran past 5 minutes
        under load on 2026-11-13, 110 s of one Ask. None when the family holds no cell."""
        params = {"family": family, "low": family + ":", "high": family + chr(ord(":") + 1),
                  "runs": CELL_RUNS}

        def whole(pg: Any) -> tuple[tuple[int, int], ...] | None:
            row = pg.execute(WHOLE_SPAN_SQL).fetchone()
            return None if row is None or row[0] is None else ((int(row[0]), int(row[1])),)
        return self._bounded_runs(CELL_SPAN_SQL, params, CELL_SPAN_EXACT_MS, 20, whole)

    def family_chunk_span(self, family: str) -> tuple[tuple[int, int], ...] | None:
        """``TraverseTools.chunk_span`` of ``family`` on Postgres: its chunk id runs
        (``CHUNK_SPAN_SQL``, ``cell_runs`` with shift 0), ``()`` when it holds no chunk;
        None (not known: a search of it reads every match) past ``CHUNK_SPAN_EXACT_MS``
        or the thread's SQL deadline (the bulk family, mail: past 40 s live), or while
        the document word index is not read here (the SQLite generation's chunk ids are
        not these: ``ontology.chunk_families`` narrows those)."""
        if not self.chunk_words_loaded():
            return None
        params = {"family": family, "low": family + ":", "high": family + chr(ord(":") + 1),
                  "runs": CELL_RUNS}
        runs = self._bounded_runs(CHUNK_SPAN_SQL, params, CHUNK_SPAN_EXACT_MS, 0,
                                  lambda pg: _NOT_KNOWN)
        return None if runs is _NOT_KNOWN else (runs or ())

    def _bounded_runs(self, sql: str, params: Mapping[str, Any], limit_ms: int, shift: int,
                      past: Callable[[Any], Any]) -> Any:
        """``cell_runs`` of ``sql`` (``shift``) read within ``limit_ms`` (or the thread's SQL
        deadline, when sooner); past it, ``past(pg)`` read in the same snapshot."""
        from operating import call_limits
        limit = limit_ms / 1000
        left = call_limits.seconds_left()
        if left is not None:
            limit = min(limit, left)
        import psycopg
        with self.lock, self.conn._lock:
            pg = self.conn._pg
            (before,) = pg.execute("SHOW statement_timeout").fetchone()
            pg.execute(f"SET statement_timeout = {max(1, int(limit * 1000))}")
            # Inside a transaction (the facts read holds one snapshot) a cancelled
            # statement aborts it: the exact read is guarded by a savepoint, so the
            # fallback runs in the same snapshot (11-13: every facts read under load
            # failed "InFailedSqlTransaction" here, and the shared facts went stale).
            guarded = pg.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
            if guarded:
                pg.execute("SAVEPOINT plx_cell_span")
            try:
                runs = cell_runs(pg.execute(sql, params).fetchall(), shift)
            except psycopg.errors.QueryCanceled:
                if guarded:
                    pg.execute("ROLLBACK TO SAVEPOINT plx_cell_span")
                elif pg.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
                    pg.execute("ROLLBACK")
                pg.execute("SET statement_timeout = 0")
                runs = past(pg)
            else:
                if guarded:
                    pg.execute("RELEASE SAVEPOINT plx_cell_span")
            finally:
                try:
                    pg.execute(f"SET statement_timeout = '{before}'")
                except Exception:  # noqa: BLE001 - a lost session is opened anew with its own
                    pass
        return runs

    def link_odd(self) -> list[str] | None:
        """The links one of whose evidence slots is an odd text (``LINK_ODD_SQL``), read
        the generic way inside the native link slice; None: more than ``LINK_ODD_CAP``,
        the slice is read the generic way."""
        with self.conn._lock, self.conn._pg.cursor() as cur:
            cur.execute(LINK_ODD_SQL, {"cap": LINK_ODD_CAP + 1})
            texts = cur.fetchone()[0] or []
            if len(texts) > LINK_ODD_CAP:
                return None
            odd: list[str] = []
            if texts:
                cur.execute(LINK_ODD_ROWS_SQL, {"texts": texts})
                odd = list(cur.fetchone()[0] or [])
                if len(odd) > LINK_ODD_CAP:
                    return None
            return odd

    link_odd_cap = LINK_ODD_CAP

    def link_marks(self, schemes: Sequence[str], name: str | None = None
                   ) -> list[tuple] | None:
        """The link slice's rows (``link_slice_sql``'s shape) from the kept
        ``ontology.link_marks`` (migration 0028), of every type or of ``name`` alone, in
        this reader's snapshot; None when they are not kept for exactly these gated
        schemes at the store as it is now (``LINK_MARKS_KEPT_SQL``), or the store has
        none (an older schema): the slice is then read from link_rows."""
        pg = self.conn._pg
        with self.conn._lock, pg.cursor() as cur:
            from operating.pg import link_marks
            if not link_marks.present(cur):
                return None
            cur.execute(LINK_MARKS_KEPT_SQL, {"schemes": sorted(schemes)})
            kept = cur.fetchone()
            if not kept or kept[0] is not True:
                return None
            cur.execute(LINK_MARKS_SQL, {"name": name})
            return cur.fetchall()

    def link_odd_of(self, objects: Sequence[str]) -> list[str] | None:
        """``link_odd`` among the links from or to ``objects`` alone (``LINK_ODD_OWNED_SQL``,
        by their indexes); None: more than ``LINK_ODD_CAP``."""
        with self.conn._lock, self.conn._pg.cursor() as cur:
            cur.execute(LINK_ODD_OWNED_SQL, {"owned": list(objects)})
            odd = list(cur.fetchone()[0] or [])
        return None if len(odd) > LINK_ODD_CAP else odd

    def slice_rows(self, kind: str, schemes: Sequence[str], *,
                   key_range: tuple[str | None, str | None] | None = None,
                   odd: Sequence[str] | None = None,
                   objects: Sequence[str] | None = None,
                   name: str | None = None) -> list[tuple] | None:
        """``TraverseTools._slices``' rows of ``kind`` read natively (``LINK_SLICE_SQL``:
        links only); None: read them the generic way. ``key_range``: (lo, hi) of the link
        ids read (``link_slice_sql``); ``odd``: ``link_odd`` read once for every part;
        ``objects``: of the links from or to these objects alone; ``name``: of the links
        of this type alone."""
        if kind != "link":
            return None
        if objects is None and key_range is None:
            # the whole slice, or one type's: kept in Postgres when it is current
            kept = self.link_marks(schemes, name)
            if kept is not None:
                return kept
        if odd is None and objects is not None:
            odd = self.link_odd_of(objects)
            if odd is None:
                return None
        if odd is None:
            odd = self.link_odd()
            if odd is None:
                return None
        with self.conn._lock, self.conn._pg.cursor() as cur:
            # each part aggregated by the workers that read it (the count lets them), and
            # the gated slot texts joined by hash: estimated a few thousand rows, the
            # planner probed millions of them one at a time
            with self.conn._pg.transaction():
                if objects is None and name is None:
                    cur.execute("SET LOCAL enable_nestloop = off")
                    # few groups (type, markings, gated texts): hashed in memory, where
                    # a sort of each part's millions of rows spilled to temp files
                    cur.execute("SET LOCAL enable_sort = off")
                elif objects is not None:   # a few objects' links: by index, nested
                    cur.execute("SET LOCAL enable_seqscan = off")
                params: dict[str, Any] = {"schemes": sorted(schemes), "odd": list(odd)}
                if key_range is not None:
                    params.update(lo=key_range[0], hi=key_range[1])
                if objects is not None:
                    params["owned"] = list(objects)
                if name is not None:
                    params["name"] = name
                from ontology.pg_byte_order import key_collate
                cur.execute(link_slice_sql(key_range is not None, objects is not None,
                                           name is not None and objects is None,
                                           key_collate(cur) if key_range is not None
                                           else ' COLLATE "C"'), params)
                return cur.fetchall()

    def text_matches(self, scope: Any, words: str, typed_properties: Sequence[str] | None,
                     after: int, batch: int, *, beside: bool = False,
                     object_type: str | None = None,
                     type_properties: Sequence[str] | None = None) -> list[Any]:
        """``StoreReader.text_matches`` on the values' word index: the same predicates and
        order; the layout narrowing becomes its own condition (a named pass's properties
        are already in its predicate; a ``beside`` pass of one type reads that type's
        properties only, as the layouts it narrows to hold)."""
        if not self.words_loaded():
            if self.text_reader is None:
                self._no_words("value_text")
                return []
            with self.text_reader.lock:
                return self.text_reader.text_matches(
                    scope, words, typed_properties, after, batch, beside=beside,
                    object_type=object_type, type_properties=type_properties)
        from operating.pg.words import tsquery
        query = tsquery(words)
        typed = "(o.object_type || '.' || v.property) IN (SELECT value FROM json_each(:typed)) "
        where = ("AND v.value_type = 'text' " if typed_properties is None else
                 "AND v.value_type = 'text' AND NOT " + typed if beside else "AND " + typed)
        if object_type is not None:
            where += "AND o.object_type = :type "
        narrowed = bool(beside and object_type is not None and type_properties)
        if narrowed:
            where += "AND v.property IN (SELECT value FROM json_each(:own)) "
        params = scope.params(words=words, after=after, batch=batch, type=object_type,
                              typed=json.dumps(list(typed_properties or ())),
                              own=json.dumps(sorted(set(type_properties or ()))))
        # the cells the pass's own predicates can hold (``StoreReader._text_layouts``): the
        # named properties' fields, a narrowed pass's type's, and text kinds unless named
        facts = self.windows.refresh()
        named = typed_properties is not None and not beside
        props = ({pair.split(".", 1)[-1] for pair in typed_properties or ()} if named
                 else set(type_properties or ()) if narrowed else None)
        fields = None if props is None else \
            [f for prop in sorted(props) for f in facts["by_property"].get(prop, ())]
        kinds = None if named else facts["text"]
        sql = (_CANDIDATE_VALUES + self._VISIBLE_OBJECT + "AND " + self._at("v")
               + " AND v.markings IN (SELECT value FROM json_each(:marks)) "
               + where + "ORDER BY CAST(k.pos AS INTEGER) LIMIT :batch")

        def read(part: list[tuple[int, int]], want: int) -> list[Any]:
            number = {cell: row for row, cell in part}
            rows = self.conn.execute(sql, dict(params, batch=want, cells=json.dumps(
                [cell for _row, cell in part]))).fetchall()
            return [Row(row.keys(), (number[row[0]], *tuple(row)[1:])) for row in rows]
        return _candidates(self.windows.windows(
            "value_text", query, after, fields=fields, kinds=kinds,
            want=max(WINDOW_MATCHES, 3 * batch)), read, batch)

    def chunk_matches(self, scope: Any, words: str, object_type: str | None, after: int,
                      batch: int) -> list[Any]:
        """``StoreReader.chunk_matches`` on the document word index (``chunk_words``) once
        it is built; before, the SQLite generation's."""
        if self.chunk_words_loaded():
            from operating.pg.words import tsquery
            query = tsquery(words)
            where = "AND o.object_type = :type " if object_type is not None else ""
            sql = (_CANDIDATE_CHUNKS + self._VISIBLE_OBJECT + "AND " + self._at("v")
                   + " AND fd.markings IN (SELECT value FROM json_each(:marks)) "
                   + where + "ORDER BY f.chunk_id LIMIT :batch")

            def read(part: list[tuple[int, int]], want: int) -> list[Any]:
                return self.conn.execute(sql, scope.params(
                    words=words, after=after, batch=want, type=object_type,
                    rows=json.dumps([row for row, _id in part]))).fetchall()
            return _readable_text(self, scope, _candidates(self.windows.windows(
                "chunk_text", query, after, want=max(WINDOW_MATCHES, 3 * batch)), read, batch))
        if self.text_reader is None:
            self._no_words("chunk_text")
            return []
        with self.text_reader.lock:
            rows = self.text_reader.chunk_matches(scope, words, object_type, after, batch)
        return self.permitted_here(scope, rows)

    @property
    def numbered_values(self) -> tuple[str, str]:
        """``StoreReader.numbered_values``: a row number's value row by its cell's key (the
        number is computed here, never indexed)."""
        if not self.words_loaded():
            return StoreReader.numbered_values
        return ("(SELECT value AS row_no FROM json_each(:ids)) n JOIN object_values_numbered v "
                "ON v.src = (n.row_no >> 20) AND v.row_no = n.row_no", "1 = 1")

    @contextmanager
    def word_rows(self, table: str, words: str,
                  spans: Sequence[tuple[int, int]] | None = None, *,
                  keep: Any = None) -> Iterator[Iterator[int]]:
        """``StoreReader.word_rows`` read a window at a time (``WordWindows``) once the
        word index is read here; before, on the connection that holds it."""
        loaded = self.chunk_words_loaded() if table == "chunk_text" else self.words_loaded()
        if not loaded:
            with super().word_rows(table, words, spans, keep=keep) as rows:
                yield rows
            return
        from operating.pg.words import tsquery
        query = tsquery(words)

        def ids() -> Iterator[int]:
            for window in self.windows.windows(table, query, 0, spans):
                for row, _key in window:
                    if keep is None or keep(row):
                        yield row
        yield ids()

    def holds_text_of(self, schemes: Sequence[str]) -> bool:
        try:
            with self.lock:
                for scheme in schemes:
                    if self.conn.execute(
                            "SELECT 1 FROM text_fields WHERE citation >= ? AND citation < ? "
                            "LIMIT 1", (f"{scheme}://", f"{scheme}:/0")).fetchone():
                        return True
            return False
        except sqlite3.OperationalError:
            return True

    def close(self) -> None:
        self.conn.close()
        if self.text_reader is not None:
            self.text_reader.conn.close()


__all__ = ["ENV", "ONLY", "PgStoreReader", "PostgresNotWhole", "change_stamp", "enabled",
           "pg_only", "serves", "whole"]
