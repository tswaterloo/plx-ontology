"""Generic ontology tools ``plx.schema``, ``plx.find`` and ``plx.object`` (L8).

The tools read the L2 projection store (``ontology/store_ddl.py``) through ``StoreReader``, a thin read interface over the frozen tables, and
answer exactly the I/O contracts in ``operating/ontology_tool_contracts.json``
(every output is validated with ``ontology/json_contract.py`` before it leaves).
Nothing here knows an object type, an identifier kind or a case: types, kinds,
bindings, display properties and link types all come from the store rows, the
compiled schema and the identifier grammar (no per-kind branch).

Permissions (store_ddl docstring): the session actor is
checked against ``objects.audience_key`` (``ReadPolicy``, default deny), the
markings of the object's version at T *and* of its latest version (a marking
added later also hides history), the markings and times of every identifier,
value and link, and the item-level ACL of mail, SharePoint and original records
(``item_gate``, re-checked through the L1 handle; default deny). Audience,
markings and time are decided in the SQL that selects candidates, before any
batch limit; the item ACL is re-checked per object before anything counts
toward a cap, rank or total. ``plx.find`` never reports how many matches were
trimmed: its output for a denied actor equals its output on a store without the
denied rows. A no-match is reported as ``absent_in_source`` only when the
reader's grant covers every unit of every searched source, it holds every marking
the schema declares on the searched types, carriers and filtered properties
(``_declared_markings``), the search was complete and current and, for a key, it
covered every identifier carrier of the type (``_carriers_searched``); otherwise it
is ``unknown``. A filter never reads a value withheld from the reader (a marking it
lacks) as missing: a readable record holding one is matched only where its readable
values already decide the filter, else it is neither matched nor excluded. A
no-match that a marking the reader lacks may explain carries a ``marking_withheld``
gap and no count (``total`` null, never zero). ``plx.object`` never
reads a property with no stored read as absent (``unknown``, ``no_value``). ``plx.object`` reports
withheld *property* counts only for an object the actor may read, as the
contract's ``withheld`` allows. Its ``identity`` lists the visible links of an identity
type (``identity_link_type``: a person's reviewed ``same_record_as`` included, a stored
key between two types never), strongest tier first; more than ``MAX_IDENTITY`` are cut
with an ``identity_truncated`` gap. Every other link is counted in ``linked_counts``.

Identity (R3): a normalized identifier finds candidates; it never merges.
Several visible matches are always ``ambiguous`` with ranked candidates (matched
key, binding, reason, tier), never a winner. An identifier a person confirmed as an
alias of a record (``plx.review``; ``ontology.resolve.ReviewedAliases``, read per call)
is one more alias row of that record, tier ``reviewed``, citing the field that states
it. Tiers of a match: an exact match of a typed identifier (the query parses as that kind) is ``verified`` unless the
indexed row's lineage is ``gold_native_staging`` (never authority until a person
confirms those rows, R1), which is a ``lead``; a literal match whose kind the
grammar did not parse and a display-text match are ``lead``. A secondary reading
of a labelled mention (``Customer PO: 4821`` also reads as the Petra PO form
``PO: 4821``; the grammar's ``label_kind_conflict``) is searched too, but its
match is only ever a ``lead`` with reason ``secondary_reading`` and ``why_not``
``label_kind_conflict``, never an exact one. The grammar's leads are searched too and are
only ever ``lead``: a declared prefix form of the whole query (``AO-26042``, a day typed
without its letter) names every record of that day in both spellings (reason
``prefix_lead``, ``why_not`` ``prefix_form``), and a typed key with a ``same_number``
rule names the rows storing the same number in another form (``PO#4821`` and a bare
Sage ``4821``: reason ``same_number_twin``, ``why_not`` ``same_number``), and a whole
query in a kind's declared marked form (identifiers.json ``marker``: ``PO#PO-6000``)
names the rows of that kind whose lead key the marked value shares (reason
``marked_form``, ``why_not`` ``marked_form``: the owning system compares the whole text,
so a marked text is never the value it marks). Exact and alias
matches decide the status: beside one, these leads are no candidates but its
``related_*`` features (``related_same_record`` when a visible identity-tier link of an
identity type, ``identity_link_type``, joins the two records, else ``related_<reason>``,
agrees null).
Without one they are the candidates, and several are ``ambiguous``, never a winner.
A kind unique only within a scope (identifiers.json ``identity_scope``: a customer PO
per customer) roots only on a record holding a readable scope value whose scope space
holds the value under that one scope among the readable records (``identity_scopes``,
``_scope_identity``); any other match of it is a ``scope_lead`` naming why (no scope
field, no scope value, a withheld one, or ``identity_scope_ambiguous``: two customers
use the number, or ``scope_status_excluded``: the Portal skips a cancelled order, so it
never makes the number ambiguous; BC scopes within its company), and only its literal
trimmed text is exact (``exact_match``).

Source text (R6 #1): every text taken from a source (display names, raw
identifiers, text/json values) is an ``untrusted_text`` envelope citing its L1
handle and field path. Typed values come only from the projector's deterministic
parsers and are re-checked against the value type here; a value that does not
fit is withheld with a typed gap instead of being guessed.

Live fallback (7f61c793591f 2a): with ``sources``
(an ``ontology.resolve.SourceResolver``), a query the projection store has no
visible match for is resolved again over the records the live readers carry for
its typed keys, read only where the policy grants the actor and projected into a
throw-away store by the same projector; the same ``plx.find`` logic then answers.
It is a fallback, never a second tool: the contract, tiers and ambiguity rule are
unchanged, and a live no-match is never ``absent_in_source``.

Links (fk-follow): with ``object_type``, a typed identifier match on a record of another
type that names one record of that type through a declared single-valued link
(``ontology.resolve.link_hops``: ``same_record``, or ``fk`` toward the "one" side) makes
that record a candidate too, reason ``linked_identifier``: ``matched`` is the linked
record's identifier, the tier is the weaker of the match's and the link's, and the
``linked_record`` feature cites the link's compared values. The link must be visible to
the reader at T (audience, markings, item ACL of its evidence, both ends). A value
naming one record directly and another through a link is ambiguous, as ever.

Corroboration:
every candidate of a binding the schema compares with a binding of another
source family carries the identity verdict ``corroboration`` (``agrees`` true =
agree, false = conflict, null = unverifiable), decided on identifiers only, with one
``corroboration_<property>`` feature per identifier comparison; the values both
records carry (dates, amounts) are reported apart as ``value_equal`` /
``value_differs`` / ``value_not_comparable`` (a lineage that is never authority, or
no shared currency or unit) and never change the verdict. Every feature cites the
compared values of both sides. The counterparts are the
records of other source families that carry the asked key (typed identifier
matches) and that this reader may see; the comparisons come from the schema only
(``ontology.resolve``: the properties both bindings source, and the identifier a
counterpart carries against the record the candidate's fk names). Permission comes
first: a counterpart, linked record or value the reader may not see is never read
or cited, so a withheld counterpart reads exactly as an absent one (``unverifiable``,
feature ``no_visible_counterpart``), never as a conflict. A verdict never changes the
status, the rank or the tier: orders, POs, AOs and invoices are never merged.
Contract note: the candidate contract has one feature list, ``why_not``; these
features ride there with their own ``agrees`` value (a proposed ``because`` list for
agreeing features is recorded in the commit).

The case host (``operating/mcp_server.py``) lists ``tool_specs`` and calls
``OntologyTools.call`` (built by ``operating/ontology_host.py``); ``register`` fills a
name -> handler table for in-process callers. ``HOST_RETIREMENTS`` records which host
tools retired into which successor, so the host stays within the contract's
``max_case_host_tools`` (tests/test_mcp_server.py).
"""

from __future__ import annotations

import base64
from contextlib import closing, nullcontext
import datetime as dt
import functools
from collections import OrderedDict
from itertools import islice
import json
import re
import secrets
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from typing import Any

from ontology import store_ddl
from ontology.json_contract import validate
from ontology.mentions import SENDER_CLASSES
from ontology.text_index import chunk_text, query_terms
from ontology.types import Actor
from operating.injection_guard import identifier_kind_of, is_identifier
from operating.ontology_tools_policy import (
    _citation, _DATE, _DECIMAL, _envelope_bytes, _FIELD_PATH, _find_text_seconds, _format_time,
    _HANDLE, _IDENTITY_TIERS, _known_at, _markings, _NAME, _PAGE_FITS, _parse_time, _PLAN_READ,
    _Scope, _TIER_RANK, _TIMESTAMP, _to_ms, DEFAULT_LIMIT, EMPTY_SCAN_MARGIN_SECONDS,
    GRAMMAR_LEAD_REASONS, LINK_BELOW_FLOOR, LINK_FILTER_TIER_FLOOR, LINK_TARGET_CAP,
    LINK_TARGETS_LEAD, LINK_TARGETS_PARTIAL, load_contracts, MARKED_REASON, MAX_BEYOND, MAX_SETS,
    NEVER_AUTHORITY, pinned_clock, PLAN_READ_SCALE, ReadPolicy, REFERENCE_REASON,
    RELATED_REFERENCE_CAP, ROOT_REASONS, run_memo_for, SCAN_CURSOR_PREFIX, SCAN_MARGIN_SECONDS,
    SCAN_ROW_BUDGET, SCOPE_EXCLUDED, SET_TTL_SECONDS, TIER_ORDER, TOOL_NAMES)
# operating/fast_answer.py (in flight tonight) still imports these through this module
from operating.ontology_tools_policy import NAMED_READ_NOTE, run_memo  # re-export
from operating.ontology_tools_identity import (
    _compare_key, _error, _gap, _Key, _LiveSets, _Match, _MatchedId, _one_source_key, _query_keys,
    _same_text, _schema_memo, _source_of, _text_tokens, bc_company, carrier_fields, compared_text,
    Grammar, LIVE_LOOKUP_HINT, PO_TEXT_FIELD, removal_gap, SchemaView, scoped_carriers,
    text_only_kind, ToolError, WithheldText)
from operating.ontology_tools_results import (
    _BAD, _chunk_match, _citation_text, _completeness, _cursor_mode, _event_bound, _filter_gaps,
    _fingerprint, _fit_entities, _freshness_first, _generation, _generation_tag, _index_row_ok,
    _ISO_DAY, _name_words, _names_identifier, _names_query, _NOT_ASCII, _not_found_subject,
    _NUMERIC_TYPES, _pinned, _read_cursor, _read_scan_cursor, _resolve_conflicts, _scan_check,
    _target_marks, _value_text_match, _write_cursor, unit_out)
from operating.ontology_tools_reader import StoreReads
from operating.ontology_tools_scope import ScopeSteps
from operating.ontology_tools_find import FindSteps
from operating.ontology_tools_object import ObjectSteps


MAX_IDENTITY = 50  # plx.object identity links; a cut says so (identity_truncated)
TEXT_SCAN_CAP = 400  # readable objects a display-text search returns at most
LEAD_SCAN_CAP = 2000  # identifier rows one prefix or same-number range reads at most
FILTER_SCAN_CAP = 5000  # readable objects a filter listing scans at most
LINK_TARGET_SEGMENTS = 20
FILTER_BATCH = 1000  # objects a filter scan reads per statement (its key-ordered range)
NARROWED_SPAN = 2000  # keys a narrowed statement passes over at most
# A filter listing whose segment matched nothing scans the next ones for this long (and
# stops this many seconds before the call's own limit), so a scarce match is listed.
EMPTY_SCAN_SECONDS = 20.0
CURSOR_MAX_CHARS = 512  # the contract's cursor bound (ontology_tool_contracts.json)
# The first window of word matches a display-text pass tests against its layouts
# (``StoreReader._layout_candidates``; later windows double), and how many candidate
# rows one query then reads.
TEXT_LAYOUT_NARROW_ROWS = 5000
TEXT_LAYOUT_CHUNK = 2000
TEXT_LAYOUT_WINDOW_MAX = 80000  # word matches one lazy layout window reads at most
# Layout cells per word match in a window past which its matches' cells are read by row
# number rather than by walking the layouts' index ranges (a random read costs about as
# much as this many sequential index entries).
TEXT_LAYOUT_RANGE_FACTOR = 8
MAX_SET_OBJECTS = 5000
# Overall ceiling on remembered live ids across every reader (each reader is also capped
# at MAX_SET_OBJECTS): four full readers' worth, so a few concurrent readers keep their
# hints while readers x 5000 cannot pile up for a whole TTL.
MAX_LIVE_IDS = 4 * MAX_SET_OBJECTS


def result_limit(limits: Mapping[str, Any]) -> int:
    """The byte bound of one result: ``limits.max_result_bytes``, or PLAN_READ_SCALE times
    it for a plan's own read (``plan_read``)."""
    base = int(limits["max_result_bytes"])
    return base * PLAN_READ_SCALE if _PLAN_READ.get() else base


class StoreReader(StoreReads):
    """Read-only access to a ``plx-ontology-store/v10`` SQLite file.

    Only the tables of ``ontology/store_ddl.py`` are read; the projector (L7)
    owns writing them. Every query here is parameterized and keyed; scope
    predicates (visible audience keys, permitted marking sets) are passed as
    JSON arrays and expanded with ``json_each`` so the actor's denied rows never
    reach Python.
    """

    # How a word match (``value_text f``) joins its value row (``object_values_numbered
    # v``); on Postgres, by the cell's key (``PgStoreReader``).
    value_word_join = "value_text f JOIN object_values_numbered v ON v.row_no = f.rowid"

    # The value rows of the word-row numbers ``:ids`` (``word_rows``): the ``FROM`` text
    # naming them ``v``, and the condition that picks them.
    numbered_values = ("object_values_numbered v",
                       "v.row_no IN (SELECT value FROM json_each(:ids))")
    # A word match's rows are read this many at a time (``word_rows``).
    WORD_ROWS_BATCH = 2000

    # The object is readable: its audience unit is granted, its version current
    # at :at carries only permitted markings, and so does its latest version (a
    # marking added later applies to every earlier version too: as_known_at never
    # reads around a reclassification).
    _VISIBLE_OBJECT_STORE = (
        "o.audience_key IN (SELECT value FROM json_each(:aud)) "
        "AND EXISTS (SELECT 1 FROM object_versions ov WHERE ov.object_id = o.object_id "
        "AND julianday(ov.recorded_at) <= julianday(:at) "
        "AND (ov.superseded_at IS NULL OR julianday(ov.superseded_at) > julianday(:at)) "
        "AND ov.markings IN (SELECT value FROM json_each(:marks))) "
        "AND (SELECT lv.markings FROM object_versions lv WHERE lv.object_id = o.object_id "
        "ORDER BY julianday(lv.recorded_at) DESC LIMIT 1) "
        "IN (SELECT value FROM json_each(:marks)) ")

    def identifiers_by_prefix(self, scope: _Scope, kinds: Sequence[str], prefix: str
                              ) -> list[sqlite3.Row]:
        """Readable identifiers of ``kinds`` whose canonical form starts with ``prefix``
        (an index range on ``normalized``; the caller applies the grammar's rule)."""
        return list(self.conn.execute(
            self._scoped_identifiers() + "AND i.normalized >= :lo AND i.normalized < :hi "
            "AND i.identifier_kind IN (SELECT value FROM json_each(:kinds)) "
            "ORDER BY i.normalized, i.object_id LIMIT :cap",
            scope.params(lo=prefix, hi=prefix + "\U0010ffff", kinds=json.dumps(list(kinds)),
                         cap=LEAD_SCAN_CAP + 1)))

    def identifiers_by_suffix(self, scope: _Scope, kinds: Sequence[str], suffix: str
                              ) -> list[sqlite3.Row]:
        """Readable identifiers of ``kinds`` whose canonical form without spaces ends with
        ``suffix`` (a scan of those kinds; a marked form's lead key is such a suffix, as a
        blocking key drops only leading text; the caller applies the grammar's rule)."""
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", suffix)
        return list(self.conn.execute(
            self._scoped_identifiers() + "AND i.identifier_kind IN (SELECT value FROM "
            "json_each(:kinds)) AND replace(i.normalized, ' ', '') LIKE :pattern ESCAPE '\\' "
            "ORDER BY i.normalized, i.object_id LIMIT :cap",
            scope.params(kinds=json.dumps(list(kinds)), pattern=pattern,
                         cap=LEAD_SCAN_CAP + 1)))

    def _word_windows(self, words: str, after: int) -> Iterator[list[int]]:
        """The value rows past ``after`` holding ``words``, in row order, in windows of
        ``TEXT_LAYOUT_NARROW_ROWS`` doubling up to ``TEXT_LAYOUT_WINDOW_MAX``: one
        statement read as far as its consumer goes (a prefix word's matches are merged
        once: 3 s for a common one, which every new statement would pay again)."""
        read = getattr(self.conn, "stream", self.conn.execute)   # Postgres: batch-wise
        cursor = read(
            "SELECT rowid FROM value_text WHERE value_text MATCH :words AND rowid > :after "
            "ORDER BY rowid", {"words": words, "after": after})
        try:
            window = max(TEXT_LAYOUT_NARROW_ROWS, 1)
            while rows := cursor.fetchmany(window):
                yield [row[0] for row in rows]
                if len(rows) < window:
                    return   # the word index's last rows
                window = max(min(window * 2, TEXT_LAYOUT_WINDOW_MAX), 1)
        finally:
            cursor.close()

    def _layout_windows(self, layouts: str, hits: list[int], matches: Iterator[list[int]]
                        ) -> Iterator[list[int]]:
        """Each window's candidates, read the cheaper way: its word matches' cells by row
        number (a random read each), unless the layouts hold fewer than
        ``TEXT_LAYOUT_RANGE_FACTOR`` cells per match between the window's first and last
        match: their index ranges there are then walked and tested against the matches
        as a set (sequential reads; a type's own few properties among a common word's
        matches). Counting those cells stops at that bound, so a window never reads more
        of the layout index than it would have probed."""
        try:
            while hits:
                bounds = {"layouts": layouts, "after": hits[0] - 1, "upto": hits[-1],
                          "hits": json.dumps(hits)}
                held = self.conn.execute(
                    "SELECT count(*) FROM (SELECT 1 FROM value_cells c INDEXED BY "
                    "value_cells_by_layout WHERE c.layout IN (SELECT value FROM "
                    "json_each(:layouts)) AND c.rowid > :after AND c.rowid <= :upto "
                    "LIMIT :most)", dict(bounds, most=len(hits) * TEXT_LAYOUT_RANGE_FACTOR)
                ).fetchone()[0]
                yield sorted(row[0] for row in self.conn.execute(
                    "SELECT c.rowid FROM json_each(:hits) h JOIN value_cells c "
                    "ON c.rowid = h.value "
                    "WHERE c.layout IN (SELECT value FROM json_each(:layouts))"
                    if held >= len(hits) * TEXT_LAYOUT_RANGE_FACTOR else
                    "SELECT c.rowid FROM value_cells c INDEXED BY value_cells_by_layout "
                    "WHERE c.layout IN (SELECT value FROM json_each(:layouts)) "
                    "AND c.rowid > :after AND c.rowid <= :upto "
                    "AND +c.rowid IN (SELECT value FROM json_each(:hits))", bounds))
                hits = next(matches, [])
        finally:
            matches.close()   # the word statement ends with the pass, read or not

    def text_matches(self, scope: _Scope, words: str,
                     typed_properties: Sequence[str] | None, after: int, batch: int, *,
                     beside: bool = False, object_type: str | None = None,
                     type_properties: Sequence[str] | None = None
                     ) -> list[sqlite3.Row]:
        """Readable values holding every word of ``words`` (an FTS5 query over
        ``value_text``) among the given ``Type.property`` pairs (``beside``: every other
        text value), or among every text value when None; keyset-paged by the value row
        number. Every permission and time predicate is in the WHERE clause, so a denied
        row never takes a slot of ``batch``.

        A word match's cell is first tested against the layouts the pass can match
        (``_text_layouts``: the declared properties, else text values) before its value,
        object and permission rows are read; among declared properties, a word matching
        many rows has its matches' layouts tested first, one window of matches at a time
        (``_layout_candidates``), so a common word costs one cell read per match the
        page needs, never a join per row that holds it nor a read of every match. ``type_properties`` (the asked type's declared properties, for
        a ``beside`` pass of one ``object_type``) narrows it the same way: a value of
        that type is one of its properties, so a name every other record repeats (our
        own company's, on every order) is no longer read row by row only to be dropped
        by type. Both only skip rows the pass's own predicates reject: the rows
        returned, their order and the page are those of the plain index scan."""
        typed = "(o.object_type || '.' || v.property) IN (SELECT value FROM json_each(:typed)) "
        where = ("AND v.value_type = 'text' " if typed_properties is None else
                 "AND v.value_type = 'text' AND NOT " + typed if beside else "AND " + typed)
        if object_type is not None:
            where += "AND o.object_type = :type "
        named = typed_properties is not None and not beside
        narrowed = bool(beside and object_type is not None and type_properties)
        layouts = self._text_layouts(
            sorted({pair.split(".", 1)[-1] for pair in typed_properties or ()})
            if named else sorted(set(type_properties or ())) if narrowed else None)
        params = scope.params(words=words, after=after, batch=batch, type=object_type,
                              typed=json.dumps(list(typed_properties or ())),
                              layouts=json.dumps(layouts))
        tail = ("JOIN object_values_numbered v ON v.row_no = f.rowid "
                "JOIN objects o ON o.object_id = v.object_id WHERE {match} f.rowid > :after AND "
                + self._VISIBLE_OBJECT + "AND " + self._at("v")
                + " AND v.markings IN (SELECT value FROM json_each(:marks)) "
                + where + "ORDER BY f.rowid LIMIT :batch")
        select = "SELECT v.*, o.object_type, o.binding_id AS object_binding, o.audience_key "
        if not layouts:
            return []   # no cell can pass the layout test (a type declaring no display text)
        candidates = self._layout_candidates(words, layouts, after) \
            if named or narrowed else None
        if candidates is None:
            return list(self.conn.execute(
                select + "FROM value_text f JOIN value_cells cell ON cell.rowid = f.rowid "
                "AND cell.layout IN (SELECT value FROM json_each(:layouts)) "
                + tail.format(match="value_text MATCH :words AND"), params))
        out: list[sqlite3.Row] = []
        with closing(candidates):   # its word statement ends here, the page full or not
            for rows in candidates:
                for start in range(0, len(rows), TEXT_LAYOUT_CHUNK):
                    out.extend(self.conn.execute(
                        select + "FROM (SELECT value AS rowid FROM json_each(:rows)) f "
                        + tail.format(match=""),
                        dict(params, rows=json.dumps(rows[start:start + TEXT_LAYOUT_CHUNK]),
                             batch=batch - len(out))))
                    if len(out) >= batch:
                        return out
        return out

    SENSITIVE_KEPT = 100_000   # version verdicts kept per reader (a handle's text never changes)

    # ``object_chunks``' columns and joins: the version current at :at, the object
    # readable, the field's markings held, the first ``:per_field`` chunks of a field.
    _CHUNK_COLUMNS = ("SELECT t.chunk_id AS row_no, t.body, fd.object_id, fd.version_handle, "
                      "fd.citation, fd.field_path, fd.sender_class, o.object_type, "
                      "o.binding_id AS object_binding, o.audience_key ")

    # Whether a narrowing is read value first (``narrowed_ids``): only a store that keeps
    # its values by property (Postgres' ``value_cells_by_field``); a SQLite generation keys
    # them by object only, and a by-property read there passes over every value it holds.
    VALUES_FIRST = False
    NARROWED_IDS_CAP = 5000   # candidates a value-first read returns at most

    # What a hop over a record's links reads of each (``links_of_many``): its ends, type,
    # tier and binding (``_beyond``'s firm links), markings and times (``_row_visible``).
    # Not the link's reason and evidence: over Postgres the links view builds those text
    # columns for every row it returns (``ontology.link_text``), 2-3x the cost of the read.
    HOP_COLUMNS = ("link_id", "link_type", "from_object", "to_object", "tier",
                   "binding_kind", "markings", "recorded_at", "superseded_at")


def default_schema() -> SchemaView | None:
    """The compiled schema from ``ontology/schema.py`` (L4) when it is
    installed and loads; None otherwise (tools then fall back to store facts)."""
    try:
        from ontology import schema as schema_loader  # type: ignore[attr-defined]
        return SchemaView.from_compiled(schema_loader.load_schema())
    except Exception:  # noqa: BLE001 - loader absent or failing: reported as a gap
        return None


@functools.lru_cache(maxsize=1)
def shared_schema() -> SchemaView | None:
    """``default_schema()`` compiled once per process (compiling costs seconds): for
    callers that only read it per call, such as the gap guard and the evaluator."""
    return default_schema()


def same_party(first: Any, second: Any, edges: Iterable[tuple[Any, Any, Any]], *,
               cut: bool = False) -> bool | None:
    """Whether two records are one party (as the scope a customer PO is unique within),
    read from the identity links ``edges`` (``(from, to, tier)``) a reader may see on
    them, followed transitively: True when links at an identity tier (``_IDENTITY_TIERS``:
    verified, reviewed; the Portal maps a BC customer to its own only through an entity-map
    row with status ``LINKED``, plx-customer-portal@40f7802
    portal/src/lib/orders/order-identity.ts:resolvePortalCustomerIdFromBc:158-175) join
    them; None when only a weaker, not ``contradicted`` link (``lead``,
    ``stored_by_text_join``, ...) does, or ``cut`` says the edge list was cut (the
    left-out link may be the one): unread, never one party; else False. plx.find
    (``related_same_record``) and the case pick (``case_workspace_live``) both decide
    through it."""
    if first == second:
        return True
    edges = list(edges)

    def joined(tiers: Callable[[Any], bool]) -> bool:
        group, grown = {first}, True
        while grown:
            grown = False
            for one, other, tier in edges:
                if tiers(tier) and (one in group) != (other in group):
                    group |= {one, other}
                    grown = True
        return second in group

    if joined(lambda tier: tier in _IDENTITY_TIERS):
        return True
    return None if cut or joined(lambda tier: tier != "contradicted") else False


def default_grammar() -> Grammar | None:
    try:
        from ontology import identifiers  # type: ignore[attr-defined]
        identifiers.schema()
        return identifiers  # the module satisfies the protocol
    except Exception:  # noqa: BLE001 - grammar absent: literal matching only
        return None


def _effective_tier(row: Mapping[str, Any]) -> str:
    """The one link-tier rule plx.around, plx.aggregate and plx.process serve (R1).

    ``row`` carries ``tier``, ``from_lineage`` and ``to_lineage`` (the lineage of each
    end's version at T, None when unknown) and, when known, ``binding_kind`` and
    ``evidence_handles``. A link touching a ``NEVER_AUTHORITY`` row is capped at ``lead``,
    except a foreign key one source stores between its own rows (``_one_source_key``):
    lineage says which copy's values win, not whether a source's own key between two of
    its rows exists. A cross-source join, a text join or a mention touching such a row
    stays a lead.

    The tier is about the link only. plx.process is stricter about the record on purpose
    (R1: a never-authority row is never evidence for an authority record's stage): it
    serves this same tier, but never counts a never-authority record reached from an
    authority one (``never_authority_record``) and makes every answer from a
    never-authority root a lead (``staging_root``)."""
    tier = row["tier"]
    if (row["from_lineage"] in NEVER_AUTHORITY or row["to_lineage"] in NEVER_AUTHORITY) \
            and _TIER_RANK[tier] < _TIER_RANK["lead"] and not _one_source_key(row):
        return "lead"
    return tier


def withheld_po_text(field_path: Any, text: Any) -> bool:
    """Whether ``text`` read from ``field_path`` is a ``po_reference`` value that is not
    PO-like (``customer_po_coverage.po_like``, the one PO shape test): it is never
    echoed, since it can be a person's name. Any field path ending in that column (a
    Portal row's ``row.po_reference``, a page's ``rows.<n>.po_reference``)."""
    from operating.customer_po_coverage import po_like
    return (isinstance(field_path, str) and text is not None
            and field_path.rsplit(".", 1)[-1] == PO_TEXT_FIELD and not po_like(text))


def _envelope(text: Any, citation: str, field_path: str | None, limit: int,
              asked: Any = None) -> dict[str, Any] | None:
    """Source text as an R6 envelope, or None when it cannot be cited. A ``po_reference``
    value that is not PO-like (``withheld_po_text``) is withheld: empty text with
    ``withheld: not_po_like``, unless it is ``asked``, the text the caller itself sent
    (plx.find's query: echoing it tells the caller nothing it did not send)."""
    if text is None or not _HANDLE.match(citation or ""):
        return None
    body = str(text)
    origin: dict[str, Any] = {"citation": citation}
    if field_path and _FIELD_PATH.match(field_path):
        origin["field_path"] = field_path
    if withheld_po_text(field_path, body) and not _same_text(body, asked):
        return WithheldText(origin, body)
    out: dict[str, Any] = {"untrusted_text": body[:limit], "origin": origin}
    if len(body) > limit:
        out["truncated"] = True
    return out


# --------------------------------------------------------------------------- sets

class SetRegistry:
    """Expiring, actor-bound set handles (``$defs/set_handle``), one per host and
    shared by every tool on it: a handle names the objects its creator could see
    (at most ``MAX_SET_OBJECTS``); a reader re-checks visibility on use."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic,
                 ttl: float = SET_TTL_SECONDS) -> None:
        self.monotonic = monotonic
        self.ttl = ttl
        # handle -> (owner, ids, expiry, live, withheld, lead): ``withheld`` marks a set
        # made from an answer this reader's grants trimmed (it is never the whole set);
        # ``lead`` one whose records were reached only through lead-tier links (possibly
        # related, never confirmed: whatever is counted or walked from it stays a lead);
        # ``links`` the link types walked to make it (empty: listed from its object types
        # alone), or None when how it was made is not known: a source read only in part
        # is then never taken as read in full for its members (``links``); ``complete``
        # whether the answer that made it held every match (no capped scan, no cut, no
        # unbuilt projection, no member left out) and ``total`` how many matched when
        # known: a count of an incomplete set is a lower bound, never exact.
        self._sets: dict[str, tuple[tuple[str, tuple[str, ...]], tuple[str, ...], float,
                                    bool, bool, bool, frozenset[str] | None, bool,
                                    int | None]] = {}
        # Object ids plx.find answered from a live lookup (not in the projection store),
        # with their expiry: traversal tools name them instead of answering "nothing".
        # Insertion order is expiry order (a refreshed id moves to the end), so the
        # oldest and the expired sit at the front: no scan on new(). ``_live_by_owner``
        # holds the same ids per reader for the per-reader cap; ``_live_refs`` counts the
        # live sets holding an id so that evicting a set drops the ids only it held.
        self._live_ids: OrderedDict[tuple[tuple, str], float] = OrderedDict()
        self._live_by_owner: dict[tuple, OrderedDict[str, None]] = {}
        self._live_refs: dict[tuple[tuple, str], int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _owner(actor: Actor) -> tuple:
        return actor.tenant, tuple(sorted(actor.principals)), getattr(actor, "clearance", None)

    def _drop_live(self, owner: tuple, oid: str) -> None:
        self._live_ids.pop((owner, oid), None)
        mine = self._live_by_owner.get(owner)
        if mine is not None:
            mine.pop(oid, None)
            if not mine:
                del self._live_by_owner[owner]

    def _drop_set(self, handle: str) -> None:
        """Forget a set and the live ids that only it held. Caller holds the lock."""
        owner, ids, _, live, *_ = self._sets.pop(handle)
        if not live:
            return
        for oid in set(ids):
            key = (owner, oid)
            left = self._live_refs.get(key, 1) - 1
            if left > 0:
                self._live_refs[key] = left
            else:
                self._live_refs.pop(key, None)
                self._drop_live(owner, oid)

    def new(self, actor: Actor, object_ids: Sequence[str], *, live: bool = False,
            withheld: bool = False, lead: bool = False,
            links: Iterable[str] | None = None, complete: bool = False,
            total: int | None = None) -> str:
        """A handle for ``object_ids``; ``live`` marks a set plx.find answered from a
        live lookup (its objects exist only in a discarded throw-away store),
        ``withheld`` one whose answer this reader's grants trimmed, ``lead`` one whose
        records only lead-tier links reach; ``links`` the link types walked to reach its
        records (``()``: listed from their object types alone; None: not known);
        ``complete`` that the answer making it held every match (the default, not said,
        is incomplete: a count of it is never exact) and ``total`` how many matched when
        known. A set cut at ``MAX_SET_OBJECTS`` is never complete."""
        handle = "set:" + secrets.token_urlsafe(18).replace("=", "")
        now = self.monotonic()
        with self._lock:
            for key in [k for k, v in self._sets.items() if v[2] <= now]:
                self._drop_set(key)
            while self._live_ids:
                (owner, oid), until = next(iter(self._live_ids.items()))
                if until > now:
                    break
                self._drop_live(owner, oid)
            while len(self._sets) >= MAX_SETS:
                self._drop_set(min(self._sets, key=lambda k: self._sets[k][2]))
            ids = tuple(object_ids[:MAX_SET_OBJECTS])  # guard-ok: cache memory bound; a cut set is never marked whole, so the incompleteness is carried truthfully
            owner = self._owner(actor)
            whole = bool(complete) and not live and len(ids) == len(object_ids) and (
                total is None or total == len(ids))
            known = total if isinstance(total, int) and total >= len(ids) else None
            self._sets[handle] = (owner, ids, now + self.ttl, live, withheld, lead,
                                  None if links is None else frozenset(links), whole,
                                  len(ids) if whole else known)
            if live:
                mine = self._live_by_owner.get(owner) or OrderedDict()
                new_ids = dict.fromkeys(ids)
                # The cap is per reader: one reader's large live find never evicts
                # another reader's ids (their hint would fall back to "not found").
                over = len(mine) + len(new_ids.keys() - mine.keys()) - MAX_SET_OBJECTS
                if over > 0:
                    for oid in list(islice((o for o in mine if o not in new_ids), over)):
                        self._drop_live(owner, oid)
                mine = self._live_by_owner.setdefault(owner, OrderedDict())
                for oid in new_ids:
                    key = (owner, oid)
                    self._live_refs[key] = self._live_refs.get(key, 0) + 1
                    self._live_ids[key] = now + self.ttl
                    self._live_ids.move_to_end(key)
                    mine[oid] = None
                    mine.move_to_end(oid)
                # Overall ceiling: the oldest ids across all readers go first.
                while len(self._live_ids) > MAX_LIVE_IDS:
                    (old_owner, old_id), _ = next(iter(self._live_ids.items()))
                    self._drop_live(old_owner, old_id)
        return handle

    def is_live(self, handle: str, actor: Actor) -> bool:
        """Whether this actor's live handle came from a live lookup."""
        with self._lock:
            entry = self._sets.get(handle)
        return (entry is not None and entry[2] > self.monotonic()
                and entry[0] == self._owner(actor) and entry[3])

    def is_withheld(self, handle: str, actor: Actor) -> bool:
        """Whether this actor's live handle came from an answer its grants trimmed."""
        with self._lock:
            entry = self._sets.get(handle)
        return (entry is not None and entry[2] > self.monotonic()
                and entry[0] == self._owner(actor) and entry[4])

    def is_lead(self, handle: str, actor: Actor) -> bool:
        """Whether this actor's live handle holds records reached only through lead-tier
        links (a ``possibly_related`` set, or one walked or counted from it)."""
        with self._lock:
            entry = self._sets.get(handle)
        return (entry is not None and entry[2] > self.monotonic()
                and entry[0] == self._owner(actor) and entry[5])

    def links(self, handle: str, actor: Actor) -> frozenset[str] | None:
        """The link types walked to make this actor's live handle (empty when its records
        were listed from their object types alone); None when that is not known, or the
        handle is unknown, expired or another reader's. A count over a set of unknown
        making never takes a source read only in part as holding none of its records."""
        with self._lock:
            entry = self._sets.get(handle)
        if entry is None or entry[2] <= self.monotonic() or entry[0] != self._owner(actor):
            return None
        return entry[6]

    def completeness(self, handle: str, actor: Actor) -> tuple[bool, int | None]:
        """(complete, total) of this actor's live handle: complete only when the answer
        that made it held every match; total how many matched when known. An unknown,
        expired or another reader's handle is (False, None): never complete."""
        with self._lock:
            entry = self._sets.get(handle)
        if entry is None or entry[2] <= self.monotonic() or entry[0] != self._owner(actor):
            return False, None
        return entry[7], entry[8]

    def mark_incomplete(self, handle: str, actor: Actor) -> None:
        """Record that this actor's handle is not every match (a later answer that
        returned it lost members): only ever from complete to incomplete."""
        with self._lock:
            entry = self._sets.get(handle)
            if entry is not None and entry[0] == self._owner(actor) and entry[7]:
                self._sets[handle] = entry[:7] + (False, None)  # guard-ok: fixed tuple shape, not content

    def live_id(self, object_id: str, actor: Actor) -> bool:
        """Whether plx.find recently answered this object id to this same actor
        (tenant, principals, clearance) from a live lookup. Owner-bound: another
        reader learns nothing about ids it never found (no existence oracle)."""
        if not isinstance(actor, Actor):
            return False
        with self._lock:
            until = self._live_ids.get((self._owner(actor), object_id))
        return until is not None and until > self.monotonic()

    def resolve(self, handle: str, actor: Actor) -> tuple[str, ...] | None:
        """Ids of a live handle this actor created; None for unknown, expired or
        another actor's handle (indistinguishable to the caller)."""
        with self._lock:
            entry = self._sets.get(handle)
        if entry is None or entry[2] <= self.monotonic():
            return None
        if entry[0] != self._owner(actor):
            return None
        return entry[1]

class OntologyTools(ScopeSteps, FindSteps, ObjectSteps):
    """``plx.schema``, ``plx.find`` and ``plx.object`` over one projection store.

    ``store``: a ``StoreReader``. ``policy``: the host's ``ReadPolicy``.
    ``schema``/``grammar``: the compiled schema and identifier grammar
    (``default_schema()``/``default_grammar()`` when the host passes nothing).
    ``clock``: returns the UTC "now" an omitted ``as_known_at`` means.
    ``item_gate(actor, l1_handle) -> bool``: the item-level ACL re-check for
    records that carry their own ACL (``graph-mail://``, ``sharepoint://``,
    ``original://``), as the store contract requires and the case host does today
    (``item_gate_from_case_runtime``). Without one, those records are denied
    (default deny); ``api://`` and ``bc://`` records have no item ACL.
    """

    @property
    def clock(self) -> Callable[[], dt.datetime]:
        """'Now' for a call naming no ``as_known_at``: the clock given, or inside
        ``snapshot_reads`` the snapshot's one moment (``pinned_clock``)."""
        return self._clock

    @clock.setter
    def clock(self, value: Callable[[], dt.datetime]) -> None:
        self._clock = pinned_clock(value)

    def __init__(self, store: StoreReader, policy: ReadPolicy, *,
                 item_gate: Callable[[Actor, str], bool] | None = None,
                 schema: SchemaView | None = None, grammar: Grammar | None = None,
                 contracts: Mapping[str, Any] | None = None,
                 clock: Callable[[], dt.datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sources: Any = None, aliases: Any = None, reviews: Any = None,
                 unconfigured: Iterable[str] = (),
                 freshness_bounds: Mapping[str, float] | None = None,
                 freshness_clock: Callable[[], dt.datetime] | None = None) -> None:
        self.store = store
        # Source family -> seconds: the host's capture bound (``operating.host.
        # freshness_bounds``, from its configured ingest cadence). A projected source
        # captured longer ago is stale, stated once per source with its age (``_freshness``).
        self.freshness_bounds = dict(freshness_bounds or {})
        # The "now" a capture's age is judged at (None: ``clock``).
        self.freshness_clock = freshness_clock
        self.sources = sources
        # Source families this host configured no reader for (a host without the Portal
        # API: ``portal_api``): their unknown or unserved capture state is not a
        # freshness problem but one ``source_not_configured`` gap (``_source_gaps``).
        self.unconfigured = frozenset(unconfigured)
        # ontology.resolve.ReviewedAliases: identifiers a person confirmed (plx.review).
        self.aliases = aliases
        # ontology.reviews.ReviewStore: values a person confirmed or corrected
        # (plx.review), shown beside the source value by plx.object.
        self.reviews = reviews
        self.policy = policy
        self.item_gate = item_gate
        # What decides ``item_gate`` (a host's capture stores), set by the host that
        # installs it: tools over one store with an equal scope decide every handle
        # alike, so their per-reader gate results may be shared within a process
        # (``ontology_tools_traverse``). None: nothing is shared.
        self.item_gate_scope: tuple | None = None
        # What else decides ``item_gate`` and can change while the host runs (the people
        # exclusion's fingerprint, ``case_tools_host``): part of every shared gate result's
        # key. None: nothing.
        self.item_gate_stamp: Callable[[], Any] | None = None
        self.schema = schema
        self.grammar = grammar
        self.contracts = dict(contracts) if contracts is not None else load_contracts()
        self.limits = self.contracts["limits"]
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))   # pinned_clock
        self.monotonic = monotonic
        # Set handles are shared with every tool on this host (plx.around /
        # plx.timeline take and return the same handles).
        self.sets = SetRegistry(monotonic)
        self.resolver: Any = None   # operating.entity_resolution.Resolver, built on first use
        # False for a delta overlay layer's tools (``ontology_host._overlaid``): a find
        # merges only the layer's candidates (``delta_overlay.merge_find``), never its
        # entities, so the layer never resolves them (each resolution is the entity
        # index's searches again, once per layer per find)
        self.resolves_entities = True
        # False when the store is an empty placeholder (a host started without a built
        # projection): a filter or listing find then enumerates nothing, so it is
        # never complete and says so (gap projection_not_built).
        self.projection_built = True
        # Candidate binding -> the bindings of other source families the schema compares
        # it with (shared with the throw-away tools of a live fallback).
        self._compared: dict[str, list[str]] = {}
        self._references = frozenset() if schema is None else frozenset(
            (tid, prop["name"]) for tid, spec in schema.object_types.items()
            for prop in spec.get("properties", ())
            if isinstance(prop, Mapping) and prop.get("identifies") == "reference")
        # Type -> identifier kinds of its binding-scoped keys (``scope: within_binding``).
        scoped: dict[str, frozenset[str]] = {} if schema is None else {
            tid: frozenset(key["identifier_kind"] for key in spec.get("identity_keys", ())
                           or () if isinstance(key, Mapping)
                           and key.get("scope") == "within_binding"
                           and key.get("identifier_kind"))
            for tid, spec in schema.object_types.items()}
        self._binding_scoped_kinds = scoped
        # Kinds two or more object types key within a binding: one value names a record
        # in each of their tables (an FM id), so it is ambiguous across tables.
        self._table_ambiguous_kinds = frozenset(
            kind for kind in {k for kinds in scoped.values() for k in kinds}
            if sum(kind in kinds for kinds in scoped.values()) >= 2)
        # Binding id -> its relation: where the identifier grammar declares a carrier's
        # identity scope (``identity_scopes``).
        self._binding_relations = {} if schema is None else {
            binding["id"]: binding.get("relation")
            for spec in schema.object_types.values()
            for binding in spec.get("bindings", ()) or ()
            if isinstance(binding, Mapping) and binding.get("id")}
        self._scoped_carriers = scoped_carriers(grammar)
        self._carrier_fields = carrier_fields(grammar)

    # ------------------------------------------------------------------ entry

    def call(self, name: str, arguments: Any, actor: Actor) -> dict[str, Any]:
        """Run one tool for ``actor``; the result always satisfies the contract."""
        pin = getattr(self.store, "one_version", None)
        with pin() if pin is not None else nullcontext():
            return self._call(name, arguments, actor)

    def _call(self, name: str, arguments: Any, actor: Actor) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            raise KeyError(name)
        contract = self.contracts["tools"][name]
        if not isinstance(arguments, dict):
            return self._checked(name, _error("invalid_input", "arguments must be an object"))
        problems = validate(arguments, contract["input"], root=self.contracts, limit=3)
        if problems:
            return self._checked(name, _error("invalid_input", "; ".join(problems)))
        handler = {"plx.schema": self._schema_tool, "plx.find": self._find,
                   "plx.object": self._object}[name]
        try:
            if name == "plx.find":
                # plx.find takes the store lock itself, only around store reads: a live
                # fallback's network reads never hold the projection store.
                result = handler(arguments, actor)
            else:
                with self.store.lock:
                    result = handler(arguments, actor)
        except ToolError as exc:
            result = _error(exc.code, exc.message, exc.hint)
            try:
                result["meta"] = self._meta(self._store_meta(), self.clock(), ())
            except ToolError:
                pass  # no schema digest anywhere: the error goes out without meta
        except sqlite3.Error as exc:
            result = _error("store_unavailable",
                            f"the projection store could not be read ({type(exc).__name__})",
                            "rebuild or reopen the projection store")
        return self._checked(name, result)

    def _checked(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"][name]["output"],
                            root=self.contracts, limit=5)
        if problems:
            # Never hand out a result that breaks the frozen contract.
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems)))
        if _envelope_bytes(result) > result_limit(self.limits):
            return _error("result_too_large", "the answer does not fit one response",
                          "narrow the request (object_type, properties, limit)")
        return result

    def _checked_filters(self, object_type: str, filters: list[dict]) -> list[dict]:
        """``filters`` as a scan reads them: every one names a declared property or a link
        of the type (else ``unknown_property``, naming both), a link takes records, a set
        or a name (``eq``, ``in``, ``prefix``), the property filters can hold together
        (``_require_co_sourced``) and are in their stored forms."""
        declared = sorted(p["name"] for p in self.schema.object_types[object_type]
                          .get("properties", ()) if isinstance(p, Mapping))
        links = self._link_filters(object_type, filters)
        timed = self.event_names(object_type)
        events = [f for f in filters if f["property"] not in declared
                  and f["property"] in timed and f not in links]
        unknown = sorted({f["property"] for f in filters} - set(declared)
                         - {f["property"] for f in links} - {f["property"] for f in events})
        if unknown:
            named = sorted(self.link_names(object_type))
            raise ToolError("unknown_property",
                            f"{object_type} declares no property {', '.join(unknown)}",
                            (property_hint(self.schema, object_type, unknown)
                             + (f"; or an event's date: {', '.join(sorted(timed))}"
                                if timed else "")
                             + (f"; or a link, naming its records, a set or a name: "
                                f"{', '.join(named)}" if named else "")))
        for f in events:
            if f.get("op") not in ("gte", "gt", "lt", "lte", "eq", "in", "prefix", "present",
                                   "absent"):
                raise ToolError("invalid_input",
                                f"a filter on the event {f['property']} takes a date (gte, "
                                "gt, lt, lte, eq, prefix) or present/absent",
                                "a date bound as YYYY-MM-DD")
        for f in links:
            if f.get("op") not in ("eq", "in", "prefix", "contains") \
                    or f.get("value") in (None, [], ""):
                raise ToolError("invalid_input",
                                f"a filter on the link {f['property']} takes eq or in (the "
                                "records it reaches: object ids or a set), prefix or "
                                "contains (their name)", "plx.find the records first, or "
                                "name them")
        rest = [f for f in filters if f not in links and f not in events]
        self._require_co_sourced(object_type, rest)
        return (self._filters_as_stored(object_type, rest) + [dict(f) for f in events]
                + [dict(f) for f in links])

    def _linked_ids(self, scope: _Scope, object_type: str,
                    links: Sequence[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
        """(the records of ``object_type`` every link filter keeps, the readable records it
        cannot decide): kept, those its link (at the default tier floor or stronger, never
        contradicted, its evidence readable) joins to one of the records the filter names
        (``_link_targets``); undecided, those only a link this reader may not read (its
        markings, or evidence it may not see) joins to them: never read as not linked,
        the caller says the filter is withheld for them (``_withheld_filter_gap``).
        Resolved once per call (its scope) or plan run (``run_memo``: one policy for its
        calls) and store state (``StoreReader.change_stamp``), for every segment of the
        scan; never kept past them, so a revoked item grant is read on the next call."""
        # bound to the store as it is (a rebuild or a delta layer makes other ids), never
        # only to the call's scope: a pinned as_known_at repeats the same scope
        key = (self.store.change_stamp(), scope.actor, scope.at, scope.audience_keys,
               scope.marking_sets, object_type,
               json.dumps(list(links), sort_keys=True, default=str))
        memo = run_memo_for(("linked_ids", id(self)))
        if memo is None:
            memo = scope.links_resolved
        if key in memo:
            kept, unsure = memo[key]
            return set(kept), set(unsure)
        names = self.link_names(object_type)
        floor = _TIER_RANK[LINK_FILTER_TIER_FLOOR]
        per_filter: list[tuple[set[str], set[str], bool]] = []
        marks: set[str] = set()
        for f in links:
            link_id, forward, target_type = names[f["property"]]
            targets, targets_unsure, set_marks = self._link_targets(scope, target_type, f)
            marks |= {mark + f["property"] for mark in set_marks}
            wanted, found, hidden, below = set(targets), set(), set(), set()

            def near_of(row: Mapping[str, Any]) -> str | None:
                near, far = ((row["from_object"], row["to_object"]) if forward
                             else (row["to_object"], row["from_object"]))
                return near if far in wanted and near.split(":", 1)[0] == object_type \
                    else None
            for start in range(0, len(targets), 500):
                chunk = targets[start:start + 500]
                rows = [row for row in self.store.links_touching(scope, chunk, [link_id])
                        if near_of(row) is not None]
                # QUERY-007: the tier as plx.around serves it (``_effective_tier``, its
                # ends' lineages read): a text join to a staging record is a lead here too
                lineage = {oid: version["lineage"] for oid, version in self._visible_versions(
                    scope, sorted({row["from_object"] for row in rows}
                                  | {row["to_object"] for row in rows})).items()}
                for row in rows:
                    near = near_of(row)
                    tier = _effective_tier({
                        "tier": row["tier"], "binding_kind": row["binding_kind"],
                        "evidence_handles": row["evidence_handles"],
                        "from_lineage": lineage.get(row["from_object"]),
                        "to_lineage": lineage.get(row["to_object"])})
                    if tier == "contradicted":
                        continue
                    if _TIER_RANK.get(tier, len(TIER_ORDER)) > floor:
                        # joined only by a lead: neither kept nor read as unlinked
                        # (plx.around's below_tier_floor, the same relation asked there)
                        below.add(near)
                        continue
                    if all(self._item_allowed(scope.actor, handle) for handle
                           in json.loads(row["evidence_handles"] or "[]")):
                        found.add(near)
                    else:
                        hidden.add(near)
                for row in self.store.withheld_links(scope, chunk, [link_id]):
                    near = near_of(row)
                    if near is not None and row["tier"] != "contradicted":
                        hidden.add(near)
            if below - found - hidden and self._visible_versions(
                    scope, sorted(below - found - hidden)):
                # only records this reader may read: an unreadable one says nothing
                marks.add(LINK_BELOW_FLOOR + f["property"])
            # a name this reader may not read: which records it names is undecided
            per_filter.append((found, hidden - found, targets_unsure))
        kept = set.intersection(*(found for found, _h, _u in per_filter)) \
            if per_filter else set()
        candidates = set().union(*(found | hidden for found, hidden, _u in per_filter))
        unsure = {oid for oid in candidates - kept
                  if all(oid in found or oid in hidden or star
                         for found, hidden, star in per_filter)}
        unsure = set(self._visible_versions(scope, sorted(unsure)))
        if any(star for _f, _h, star in per_filter):
            unsure.add("*")
        unsure |= marks   # the targets' set provenance, for the caller's gaps
        if len(memo) > 16:
            memo.clear()
        memo[key] = (frozenset(kept), frozenset(unsure))
        return set(kept), set(unsure)

    def _link_targets(self, scope: _Scope, target_type: str, f: Mapping[str, Any]
                      ) -> tuple[list[str], bool, set[str]]:
        """(the records of ``target_type`` a link filter names, readable to this reader,
        whether a name it gives matched a record whose name the reader may not read, the
        provenance marks of the sets it names: ``LINK_TARGETS_PARTIAL`` for a set that is
        not every match or that this reader's grants trimmed, ``LINK_TARGETS_LEAD`` for
        one reached through lead-tier links; QUERY-005):
        its object ids, a set's members, and the records whose display name (the type's
        ``display`` properties) equals, or with ``prefix`` begins with, a name it gives.
        More than ``LINK_TARGET_CAP`` records, or a name lookup cut short (its segments or
        the call's time), is refused (``link_targets_cut``): a filter on part of the
        records it names would answer as if exact."""
        values = f["value"] if isinstance(f["value"], list) else [f["value"]]
        ids: list[str] = []
        names: list[str] = []
        marks: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            if value.startswith("set:"):
                members = self.sets.resolve(value, scope.actor)
                if members is None:
                    # QUERY-005: an unknown, expired or another reader's handle is no
                    # empty set: never an exact zero
                    raise ToolError("handle_expired",
                                    f"the {f['property']} filter's set handle is unknown "
                                    "or expired", "re-run the call that produced it")
                ids += list(members)
                whole, _matched = self.sets.completeness(value, scope.actor) \
                    if getattr(self.sets, "completeness", None) else (False, None)
                if not whole or (getattr(self.sets, "is_withheld", None)
                                 and self.sets.is_withheld(value, scope.actor)):
                    marks.add(LINK_TARGETS_PARTIAL)
                if getattr(self.sets, "is_lead", None) and self.sets.is_lead(value,
                                                                              scope.actor):
                    marks.add(LINK_TARGETS_LEAD)
            elif value.split(":", 1)[0] == target_type and value.count(":") >= 2:
                ids.append(value)
            else:
                names.append(value)
        spec = self.schema.object_types.get(target_type) or {}
        declared = {p["name"] for p in spec.get("properties", ()) if isinstance(p, Mapping)}
        unsure = False
        for prop in [p for p in spec.get("display") or () if p in declared] if names else ():
            wanted = [{"property": prop, "op": f["op"] if f["op"] in ("prefix", "contains")
                       else "in", "value": names}]
            start, segments = "", 0
            while True:
                matches, truncated, undecided, position = self._match_filters(
                    scope, target_type, wanted, start, ids_only=True)
                ids += [m.object_id for m in matches]
                unsure = unsure or bool(undecided)
                segments += 1
                if not truncated:
                    break
                if position is None or position <= start or segments >= LINK_TARGET_SEGMENTS:
                    raise ToolError(
                        "link_targets_cut",
                        f"the {f['property']} filter's name lookup over {target_type} "
                        "stopped before its end (its scan cap or the call's time), so it "
                        "cannot say which records the link reaches",
                        f"name the {target_type} records (object ids) or a set from "
                        "plx.find, or give a longer name")
                start = position
        ids = [i for i in dict.fromkeys(ids) if i.split(":", 1)[0] == target_type]
        visible = sorted(self._visible_versions(scope, ids))
        if len(visible) > LINK_TARGET_CAP:
            raise ToolError("link_targets_cut",
                            f"the {f['property']} filter names {len(visible)} {target_type} "
                            f"records, more than the {LINK_TARGET_CAP} one call follows",
                            "name fewer records, or walk from them with plx.query")
        return visible, unsure, marks

    # ------------------------------------------------------------------ plx.find

    def _find(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        from operating.search import plug
        if arguments.get("mode") == "entities" or "text" in arguments:
            return self._find_entities(arguments, actor)
        with plug.call_state():
            query = arguments.get("query")
            object_type = arguments.get("object_type")
            if (isinstance(query, str) and plug.enabled() and "as_known_at" not in arguments
                    and _text_tokens(query, []) and not _names_identifier(query)
                    and self._may_hold_document_text(object_type)):
                # Hybrid retrieval runs before the store lock is taken; its hits are gated
                # under the lock (``_hybrid_matches``). A record type that holds no
                # document text is found by its names and codes, never ranked by it.
                plug.prefetch(self.policy, actor, self._item_allowed, query)
            with self.store.lock:
                result = self._find_store(arguments, actor)
                if isinstance(query, str) and object_type is None \
                        and "as_known_at" not in arguments and arguments.get("cursor") is None \
                        and result.get("status") in ("found", "ambiguous", "not_found") \
                        and not _names_identifier(query):
                    leads = self._structured_leads(self._scope(actor, self.clock()), query)
                    if leads:
                        result["structured"] = leads
            if isinstance(query, str) and "as_known_at" not in arguments \
                    and result.get("status") in ("found", "ambiguous", "not_found") \
                    and arguments.get("cursor") is None and self.resolves_entities:
                entities, gaps = self._entities(actor, query, object_type)
                if entities:
                    result["entities"] = entities
                result["gaps"] = list(result.get("gaps") or ()) + gaps
                _fit_entities(result, result_limit(self.limits))
            return self._find_after(arguments, actor, result)

    def _find_entities(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        """``mode: entities``: the mentions of ``text`` (or ``query``) resolved to every
        record of each thing they name, without the record search (module doc of
        ``operating.entity_resolution``)."""
        text = arguments.get("text") or arguments.get("query") or ""
        entities, gaps = self._entities(actor, text, arguments.get("object_type"))
        returned = sum(len(e["records"]) for e in entities)
        completeness = ({"complete": True, "truncated": False, "returned": returned}
                        if not gaps else {"complete": False, "truncated": False,
                                          "reason": gaps[0]["reason"], "returned": returned})
        if not entities and not gaps:
            gaps = [_gap("unknown", "no_entity_named", "text",
                         "no name or code in the text names a record this reader may "
                         "read; search the records with plx.find query or object_type")]
            completeness = {"complete": False, "truncated": False,
                            "reason": "no_entity_named", "returned": 0}
        result = {"status": "resolved" if entities else "not_found", "candidates": [],
                  "entities": entities, "gaps": gaps, "completeness": completeness,
                  "meta": self._meta_locked()}
        _fit_entities(result, result_limit(self.limits))
        return result

    def _find_sources(self, arguments: Mapping[str, Any], actor: Actor,
                      stored: dict[str, Any]) -> dict[str, Any]:
        """The same ``plx.find`` over the records the live readers hold for the
        query's typed keys (``ontology/resolve.py``), when the projection store has
        no visible match. The resolver reads only units the policy grants this actor
        and projects what it found with the one projector; this method then runs the
        unchanged store logic on that throw-away store, so identity, tiers, ranking
        and the never-a-winner rule are the same. A live read answers "now" only."""
        at = self._as_known_at(arguments)
        if "as_known_at" in arguments and at < _to_ms(self.clock()):
            return stored
        query = arguments["query"]
        keys = [(k.kind, k.normalized) for k in
                _query_keys(self.grammar, query, arguments.get("identifier_kind"))
                if k.whole and k.kind]
        if not keys:
            return stored
        resolution = self.sources.resolve(
            actor, keys, query, object_type=arguments.get("object_type"),
            may_read=functools.partial(self.policy.may_read, actor),
            units=self._pinned_units(arguments))
        try:
            if resolution.store_path is None:
                result = dict(stored, meta=dict(stored["meta"]))
            else:
                # The throw-away store's build report covers only the records read live:
                # the served projection's unparsed identifiers still go unfound by value.
                with self.store.lock:
                    unparsed = self._unparsed_carriers(
                        self._scope(actor, at), arguments.get("object_type"),
                        self._store_meta())
                reader = StoreReader.open(resolution.store_path)
                try:
                    live = OntologyTools(reader, self.policy, item_gate=self.item_gate,
                                         schema=self.schema, grammar=self.grammar,
                                         contracts=self.contracts, clock=self.clock,
                                         monotonic=self.monotonic,
                                         unconfigured=self.unconfigured,
                                         freshness_bounds=self.freshness_bounds,
                                         freshness_clock=self.freshness_clock)
                    live.sets = _LiveSets(self.sets)
                    live._compared = self._compared
                    with reader.lock:
                        result = live._find_store(arguments, actor, unparsed)
                finally:
                    reader.conn.close()
            return self._with_scope_freshness(result, resolution,
                                              names_identifier=_names_identifier(query))
        finally:
            resolution.discard()

    def _find_store(self, arguments: Mapping[str, Any], actor: Actor,
                    unparsed_elsewhere: Sequence[str] = ()) -> dict[str, Any]:
        """``unparsed_elsewhere``: identifier carriers the served projection could not
        parse, for a search of a throw-away store (``_find_sources``)."""
        at = self._as_known_at(arguments)
        cursor = arguments.get("cursor")
        offset = 0
        fingerprint = _fingerprint(arguments, actor)
        store_meta = self._store_meta()
        query = arguments.get("query")
        # A filter listing (no query) scans the type in segments of at most
        # FILTER_SCAN_CAP readable objects: its cursor carries where the scan stands
        # (``scan_after``), the matches listed by earlier segments (``prior``) and the
        # filtered properties they were undecided on, bound to this projection
        # generation (``_read_scan_cursor``).
        generation = _generation(store_meta)
        scan_after, prior, undecided_before, carry = "", 0, frozenset(), ()
        cursor_mode: str | None = None
        if cursor is not None:
            if query is None:
                offset, pinned, scan_after, prior, undecided_before, carry = \
                    _read_scan_cursor(cursor, fingerprint, generation)
            else:
                offset, pinned = _read_cursor(cursor, fingerprint)
                cursor_mode = _cursor_mode(cursor)
            if "as_known_at" not in arguments:
                at = pinned
        scope = self._scope(actor, at)
        object_type = arguments.get("object_type")
        if object_type is not None and self.schema is not None:
            spec = self.schema.object_types.get(object_type)
            if spec is None:
                raise ToolError("unknown_object_type", f"no object type {object_type}",
                                "call plx.schema for the declared object types")
            if spec.get("availability", {}).get("state") == "not_served":
                reason = spec["availability"].get("reason", "not served by any source")
                raise ToolError("not_served", f"{object_type} is not served: {reason}",
                                "record a gap; it cannot be found until a source serves it")
        filters = list(arguments.get("filters", ()))
        if object_type is not None and self.schema is not None:
            # A filter on a property the type does not declare can match nothing: an
            # error naming the valid ones, never a not_found that reads as absence.
            filters = self._checked_filters(object_type, filters)
        notes: list[dict] = []
        keys: list[_Key] = []
        undecided: set[str] = set()
        scan_position: str | None = None   # where a cut filter scan resumes
        if query is not None:
            keys = _query_keys(self.grammar, query, arguments.get("identifier_kind"))
            matches, notes, scan_truncated = self._match_query(
                scope, query, keys, arguments.get("identifier_kind"), object_type)
            notes += self._scope_identity(scope, matches, query)
            from operating.search import plug
            search_state = plug.current()
            search_mode = search_state["mode"] if search_state else "index"
            if cursor is not None:
                plug.check_cursor_mode(cursor_mode, search_mode)
            fallback = plug.fallback_gap(f"query:{' '.join(query.split())}")
            if fallback is not None:
                notes.append(fallback)
        else:
            matches, scan_truncated, undecided, scan_position = self._match_filters(
                scope, object_type, filters, scan_after)
            if not prior:
                # A segment with no match lists nothing: scan on before answering, as
                # paging through empty pages would (a status scarce among many records,
                # 24 lots of 17,500 in quarantine, was answered not_found after the
                # first segment). Bounded by EMPTY_SCAN_SECONDS and the call's limit.
                matches, scan_truncated, undecided, scan_position, scan_after = \
                    self._scan_past_empty(scope, object_type, filters, matches,
                                          scan_truncated, undecided, scan_position,
                                          scan_after)
            undecided |= undecided_before
            notes += self._scope_identity(scope, matches, None)
            if object_type is not None:
                notes += self.unsourced_filter_gaps(object_type, filters)
        # Records of other systems carrying the asked key: the counterparts a candidate
        # is corroborated against (typed identifier matches only, any type; a reference
        # included: a BC invoice naming the order is compared on the order's customer).
        counterparts = [m for m in matches if m.reason in ROOT_REASONS
                        or m.reason == REFERENCE_REASON] if query is not None else []
        if object_type is not None and query is not None and self.schema is not None:
            matches = matches + self._linked_matches(scope, matches, object_type)
        if object_type is not None:
            matches = [m for m in matches if m.object_id.split(":", 1)[0] == object_type]
        visible = self._visible_versions(scope, {m.object_id for m in matches})
        key_matched = bool(visible)
        if query is not None and filters:
            links = self._link_filters(object_type, filters) if object_type else []
            keep, undecided = self._filter_objects(scope, sorted(visible),
                                                   [f for f in filters if f not in links])
            if links:
                linked, hidden = self._linked_ids(scope, object_type, links)
                marked = _target_marks(hidden)
                hidden -= marked
                undecided = set(undecided) | marked
                if "*" in hidden or hidden & set(visible):
                    undecided = set(undecided) | {f["property"] for f in links}
                keep &= linked
            visible = {k: v for k, v in visible.items() if k in keep}
        best: dict[str, _Match] = {}
        for match in matches:
            version = visible.get(match.object_id)
            if version is None:
                continue
            # One tier rule on every path: a record whose lineage is never
            # authority (gold_native_staging, R1) is at most a lead.
            if version["lineage"] in NEVER_AUTHORITY:
                match.tier = "lead"
            held = best.get(match.object_id)
            if held is None or match.sort_key() < held.sort_key():
                best[match.object_id] = match
        ranked = sorted(best.values(), key=_Match.sort_key)
        rooted = any(m.reason in ROOT_REASONS for m in ranked)
        # roots on system record ids alone hide no business number the same text reads
        # as on another record (``_only_system_keys``, ``_business_rivals``); with no such
        # rival the roots answer, as any unique code does
        roots_found = [m for m in ranked if m.reason in ROOT_REASONS]
        unseated = rooted and not (self._only_system_keys(roots_found)
                                   and self._business_rivals(roots_found, ranked))
        # A query whose own filters pin it in a property (``_pinned``) asks for the
        # records holding it there: a reference match in a pinned property is the
        # listing's answer, a candidate (a lead, ``reference_match``, never a root and
        # never the holder of the value itself), not a gap.
        pinned = _pinned(arguments)
        references = [m for m in ranked if m.reason == REFERENCE_REASON
                      and m.property not in pinned]
        ranked = [m for m in ranked if m.reason != REFERENCE_REASON or m.property in pinned]
        holders: dict[tuple[str, str, str], set[str]] = {}
        for match in matches:
            if match.reason == REFERENCE_REASON and match.object_id in visible \
                    and match.property not in pinned:
                holders.setdefault((match.object_id.split(":", 1)[0], match.property,
                                    match.normalized), set()).add(match.object_id)
        related: list[_Match] = []
        if unseated:
            # Exact and alias matches decide the status: the grammar's leads (a prefix
            # form, a same-number twin) and references never unseat them; they ride on
            # each of them as related leads instead (``_related_features``), references
            # capped before any is built (their gaps count them all). So does a record
            # whose scope excludes it (a cancelled order: the Portal never matches it).
            leads = [m for m in ranked if m.reason in GRAMMAR_LEAD_REASONS
                     or any(f.get("feature") == SCOPE_EXCLUDED for f in m.why_not)]
            related = leads + references
            led = {m.object_id for m in leads}
            ranked = [m for m in ranked if m.object_id not in led]
        total = len(ranked)
        limit = arguments.get("limit", DEFAULT_LIMIT)
        if total and offset >= total:
            raise ToolError("invalid_cursor", "the cursor is past the end of this result",
                            "repeat the query without cursor")
        requested = offset
        if total >= 2:
            # An ambiguous page always shows two or more candidates (never a lone
            # apparent winner); a last page of one repeats its predecessor, with
            # its absolute rank, rather than shrinking below two.
            limit = max(limit, 2)
            offset = min(offset, total - 2)
        page = ranked[offset:offset + limit]
        # Ranks this reader was listed on an earlier page (the cursor's position, or an
        # earlier segment's matches): a row of such a rank is marked ``repeated``.
        listed_before = prior + max(requested, offset)
        # A resumed filter scan's segment holding fewer than two matches repeats the
        # last ones listed before it, with their absolute ranks (as a last page of one
        # repeats its predecessor): a page of a listing of several matches is never a
        # lone apparent winner, and an empty last segment never reads as no match.
        # Each is re-checked as readable now; ranks run on across segments.
        repeated: list[_Match] = []
        wanted = min(2, prior + total) - total
        if query is None and wanted > 0:
            again = self._visible_versions(scope, list(carry[-wanted:]))
            if len(carry) < wanted or any(o not in again for o in carry[-wanted:]):
                # What this reader was listed is no longer readable to it: the
                # listing cannot go on as one answer.
                raise ToolError("invalid_cursor", "the listing's earlier matches are no "
                                "longer readable to this reader",
                                "repeat the query without cursor")
            repeated = self._filter_matches(scope, object_type, filters,
                                            list(carry[-wanted:]), again)
            visible = dict(visible, **again)
        shown = repeated + page
        features = self._corroborate(scope, shown, counterparts)
        for oid, extra in self._related_features(scope, shown, related).items():
            features[oid] = list(features.get(oid, [])) + extra
        candidates = (self._candidates(scope, repeated, visible, prior - len(repeated),
                                       features, query)
                      + self._candidates(scope, page, visible, prior + offset, features,
                                         query))
        for candidate in candidates:
            if candidate["rank"] <= listed_before:
                candidate["repeated"] = True
        sources = {_source_of(m.l1_handle) for m in shown} - {None}
        if not ranked:
            sources = set(scope.sources)
        meta = self._meta(store_meta, at, sources)
        gaps = list(notes)
        withheld = False   # no match, and a marking this reader lacks may hide one
        for (holder_type, prop, value), held in sorted(
                holders.items(), key=lambda item: (-len(item[1]), item[0])):
            listing = {"object_type": holder_type,
                       "filters": [{"property": prop, "op": "eq", "value": value}]}
            gaps.append(_gap(
                "unknown", "referenced_by" if rooted else "referenced_only",
                f"{holder_type}.{prop}:{value}",
                (f"{len(held)} readable {holder_type} record(s) hold this value in {prop}, "
                 "a reference that names another record, never its holder; list them all "
                 "with plx.find " + json.dumps(listing, separators=(",", ":")))))
        if undecided:
            gaps.extend(_filter_gaps(object_type, undecided))
        if not ranked and not prior:
            # (An empty last segment of a listing whose earlier segments matched is
            # no no-match: those matches stand.)
            subject = _not_found_subject(query, object_type, filters)
            tried = [f"{k.kind}:{k.normalized}" for k in keys]
            searched: dict[str, Any] = {"queries": ([query] if query else [subject])
                                        + tried}
            if scope.sources:
                searched["sources"] = list(scope.sources)
            carried, missed = ([], []) if query is None else self._carriers_searched(
                query, keys, object_type, store_meta, scope.sources)
            if carried:
                searched["scopes"] = carried
            declared = self._declared_markings(object_type, carried, filters)
            lacking = sorted(declared - scope.held)
            withheld = bool(lacking or undecided)
            if lacking and not undecided:   # else the undecided values' gap says it
                gaps.append(_gap("unknown", "marking_withheld", subject,
                                 f"the schema marks what this search reads "
                                 f"({', '.join(lacking)}) and this reader does not hold "
                                 "that marking: records it may not read are not counted, "
                                 "so this is not absence from the source"))
            # A record whose filtered value is withheld from this reader was neither
            # matched nor excluded: absence is not established over it.
            access = self._absence_proven(scope, arguments, meta["freshness"],
                                          scan_truncated, declared) and not undecided
            if query is not None and filters and key_matched:
                # The key matched a readable record the filters excluded (or could not
                # decide on): the source holds it, so nothing here is absent.
                gaps.append(_gap("unknown", "excluded_by_filter", subject,
                                 "the query matched a record this reader can read, but the "
                                 "filters excluded it; this is not absence from the source")
                            if not undecided else
                            _gap("unknown", "undecided_by_withheld_value", subject,
                                 "the query matched a record this reader can read, but a "
                                 "filtered value of it is withheld from this reader, so the "
                                 "filters neither match nor exclude it; this is not absence "
                                 "from the source"))
            elif access and holders:
                # A readable record holds the value in a reference: the source names a
                # record by it, so its absence is not established.
                gaps.append(_gap("unknown", "no_match", subject,
                                 "a readable record holds this value in a reference that "
                                 "names another record; the named record is not "
                                 "established absent", searched if "sources" in searched
                                 else None))
            elif access and carried and not missed:
                gaps.append(_gap("absent_in_source", "no_match", subject, None, searched))
            elif access and query is not None:
                # Every unit was readable, but a key search proves absence only over
                # every identifier carrier of the type (renumbered keys included).
                gaps.append(_gap("unknown", "no_match", subject,
                                 ("the search did not cover every identifier carrier: "
                                  + ", ".join(missed) if missed else
                                  "no declared identifier carrier of a type was searched")
                                 + "; absence from the source is not established",
                                 searched if "sources" in searched else None))
            elif access and query is None and not self._filters_carried(
                    object_type, filters, scope.sources):
                # A filter-only no-match proves absence only over carriers read in full.
                gaps.append(_gap("unknown", "no_match", subject,
                                 "a filtered property was not read in full from the "
                                 "searched sources; absence from the source is not "
                                 "established"))
            elif access:
                gaps.append(_gap("absent_in_source", "no_match", subject, None, searched))
            else:
                # A permission limit, an item ACL, a past as_known_at, a capped scan
                # or an incomplete capture: absence is not established.
                gaps.append(_gap("unknown", "no_visible_match", subject,
                                 "nothing matched among the records this reader can read now; "
                                 "absence from the source is not established",
                                 searched if "sources" in searched else None))
        unparsed = sorted(set(self._unparsed_carriers(scope, object_type, store_meta))
                          | set(unparsed_elsewhere)) if query is not None and not rooted else []
        if unparsed:
            # No record holds the query as a key (no match, or only leads): one whose
            # identifier failed to parse holds no searchable key, so the search cannot
            # reach it by that value (its card says so per record).
            more = f" and {len(unparsed) - 8} more" if len(unparsed) > 8 else ""
            gaps.append(_gap("unknown", "identifier_unparsed",
                             _not_found_subject(query, object_type, filters),
                             "the projection could not parse some values of "
                             f"{', '.join(unparsed)}{more} as identifiers of their "
                             "kind; a record holding one is not found by that value"))
        def scan_cursor(position: int, after: str, before: int,
                        last: Sequence[str]) -> str | None:
            return _write_scan_cursor(position, at, fingerprint, after, before,
                                      sorted(undecided), list(last)[-2:], generation)

        # The cursor resuming the scan after this segment (None when the scan reached
        # the end of the type, or the cursor cannot carry it: the listing stays cut).
        tail = ([m.object_id for m in repeated] + [m.object_id for m in ranked])[-2:]
        resume = (scan_cursor(0, scan_position, prior + total, tail)
                  if query is None and scan_position is not None else None)
        timed_out = any(g.get("reason") == "time_budget" for g in gaps)
        if scan_truncated and not any(g.get("reason") in ("scan_cap", "time_budget")
                                      for g in gaps):
            gaps.append(_gap("unknown", "scan_cap", f"query:{' '.join((query or object_type or '').split())}",
                             "the search stopped at its scan cap; narrow it with object_type, "
                             "identifier_kind or filters" if query is not None else
                             "this page's scan stopped at its per-call cap: pass "
                             "completeness.next_cursor as cursor to scan on; the listing "
                             "is complete only on the page that says so"
                             if resume is not None else
                             "the listing's scan stopped at its per-call cap and cannot be "
                             "resumed (the cursor cannot carry where it stopped); narrow "
                             "it with filters"))
        fresh = self._freshness_gaps(meta, store_meta)
        unbuilt = query is None and not self.projection_built
        if unbuilt:
            # A filter or listing find reads only the projection; on a placeholder
            # store it enumerated nothing, whatever the sources hold.
            gaps.insert(0, _gap("not_ingested", "projection_not_built",
                                _not_found_subject(query, object_type, filters),
                                "this host has no built projection store; a filter or "
                                "listing find enumerates only the projection, so this "
                                "answer is not a listing of the sources"))
        # A resumed listing's status counts the matches of earlier segments too.
        listed = prior + total
        status = "not_found" if not listed else "found" if listed == 1 else "ambiguous"
        result: dict[str, Any] = {"status": status, "candidates": candidates,
                                  "gaps": _freshness_first(fresh, gaps),
                                  # Constant by design: plx.find never reveals how many
                                  # matches the reader's grants removed.
                                  "withheld": {"properties": None, "objects": None,
                                               "reason": "marking_and_audience"}}
        # A capped scan has no exact total: it is null and the reason says why.
        reported_total = None if scan_truncated else prior + total

        if query is None and (scan_after or prior or resume is not None):
            # A listing spanning scan segments: a set of this segment's matches would
            # read as the listing (an aggregate over it a silently short answer), and
            # no set spans the segments. None is given; the gap says why.
            gaps.insert(0, _gap("unknown", "set_spans_scan_segments",
                             _not_found_subject(query, object_type, filters),
                             "this listing spans scan segments, so no set handle is given "
                             "on any of its pages: a set would hold only one segment's "
                             "matches; page through the listing and use the candidates' "
                             "object ids"))
            result["gaps"] = _freshness_first(fresh, gaps)
        elif ranked:
            # a listing of one object type (no search text) walked no link to its records
            # Complete only when this answer holds every match: no capped scan, no
            # unbuilt projection, no filter left undecided on a withheld value.
            result["set"] = self._new_set(actor, [m.object_id for m in ranked],
                                          () if query is None and object_type else None,
                                          complete=not (scan_truncated or unbuilt
                                                        or undecided),
                                          total=reported_total)
        result["meta"] = meta
        named_gaps = self._named_not_found(query, candidates)
        if named_gaps:
            result["gaps"] = named_gaps + list(result["gaps"])

        def finish(count: int) -> dict[str, Any]:
            done = offset + max(0, count - len(repeated))
            if query is not None:
                next_cursor = (_write_cursor(done, at, fingerprint, search_mode)
                               if done < total else None)
            elif done < total:
                next_cursor = scan_cursor(done, scan_after, prior, carry)
            else:
                # The segment is listed: a cut scan resumes after where it stopped
                # (None when the cursor cannot carry it: the listing stays cut).
                next_cursor = resume
            truncated = done < total or scan_truncated
            reason = ("time_budget" if timed_out else "scan_cap") if scan_truncated \
                else "paged" if done < total else None
            completeness = _completeness(count, reported_total, truncated, reason,
                                         next_cursor)
            if unbuilt:
                completeness.update(complete=False, total=None,
                                    reason=reason or "projection_not_built")
            elif withheld and not ranked:
                # Nothing readable matched, but a marking this reader lacks may hide
                # what would: not a count of zero.
                completeness.update(complete=False, total=None,
                                    reason=reason or "marking_withheld")
            return dict(result, candidates=candidates[:count], completeness=completeness)

        # The largest page prefix that fits one response; an ambiguous answer
        # always shows at least two candidates (never a lone apparent winner).
        floor = max(2 if status == "ambiguous" else len(candidates) and 1,
                    len(repeated) + (1 if page else 0))
        count = len(candidates)
        out = finish(count)
        sent = _PAGE_FITS.get()
        while count > floor and (_envelope_bytes(out) > self.limits["max_result_bytes"]
                                 or (sent is not None and not sent(out))):
            count -= 1
            out = finish(count)
        return out

    def _lead_matches(self, scope: _Scope, query: str, keys: Sequence[_Key],
                      kind_filter: str | None, seen: set[tuple]) -> tuple[list[_Match], bool]:
        """(leads, scan capped) the grammar reads into the query, never one record: every
        record a declared prefix form of the whole query names (``prefix_leads``: a day
        prefix names every AO of that day, in both spellings of the day) and every
        same-number twin of a typed key (``same_number_prefixes`` says where to read,
        ``ontology.resolve.SameNumberIndex`` which rows hold the number: a bare Sage/FM
        ``4821`` for ``PO#4821``). Rows come from the permission-filtered SQL before any
        rank; each is tier ``lead``, never exact, so several make the answer ambiguous."""
        grammar = self.grammar
        out: list[_Match] = []
        truncated = False
        if grammar is None:
            return out, truncated

        def kinds_of(kind: str) -> tuple[str, ...]:
            try:
                return (kind,) + tuple(grammar.kind_spec(kind).get("alias_kinds", ()))
            except Exception:  # noqa: BLE001 - an unknown kind reads as itself only
                return (kind,)

        wanted = None if kind_filter is None else set(kinds_of(kind_filter))

        def take(rows: list[sqlite3.Row], keep: Callable[[sqlite3.Row], bool],
                 reason: str, feature: str) -> None:
            nonlocal truncated
            if len(rows) > LEAD_SCAN_CAP:
                truncated, rows = True, rows[:LEAD_SCAN_CAP]  # guard-ok: the truncated flag reaches completeness
            for row in rows:
                ident = (row["object_id"], row["l1_handle"], row["field_path"],
                         row["identifier_kind"])
                if ident in seen or not _index_row_ok(row) or not keep(row):
                    continue
                seen.add(ident)
                match = self._identifier_match(row, typed=True)
                match.tier, match.reason = "lead", reason
                match.why_not = ({"feature": feature, "agrees": None},)
                out.append(match)

        try:
            prefix_leads = tuple(getattr(grammar, "prefix_leads", lambda _text: ())(query))
        except Exception:  # noqa: BLE001 - a grammar refusing the text reads no lead
            prefix_leads = ()
        for lead in prefix_leads:
            kinds = kinds_of(lead.kind)
            if wanted is not None and not wanted & set(kinds):
                continue
            for prefix in lead.prefixes:
                take(self.store.identifiers_by_prefix(scope, kinds, prefix),
                     lambda row: lead.matches(row["normalized"]), "prefix_lead", "prefix_form")
        marked_value = getattr(grammar, "marked_value", None)
        lead_key = getattr(grammar, "lead_key", None)
        for kind in grammar.kinds() if marked_value and lead_key else ():
            if wanted is not None and kind not in wanted:
                continue
            try:
                value = marked_value(kind, query)
                key = lead_key(kind, value) if value else None
            except Exception:  # noqa: BLE001 - a grammar refusing the text reads no lead
                key = None
            if key:
                take(self.store.identifiers_by_suffix(scope, (kind,), key),
                     lambda row, kind=kind, key=key: lead_key(kind, row["normalized"]) == key,
                     MARKED_REASON, MARKED_REASON)
        same_number_prefixes = getattr(grammar, "same_number_prefixes", None)
        if same_number_prefixes is None:
            return out, truncated
        from ontology.resolve import SameNumberIndex

        for key in keys:
            if key.secondary or key.kind is None or (wanted is not None
                                                     and key.kind not in wanted):
                continue
            try:
                prefixes = same_number_prefixes(key.kind, key.normalized)
            except Exception:  # noqa: BLE001 - a kind without the rule has no twin
                prefixes = ()
            index, rows = SameNumberIndex(key.kind), {}
            for prefix in prefixes:
                found = self.store.identifiers_by_prefix(scope, (key.kind,), prefix)
                truncated = truncated or len(found) > LEAD_SCAN_CAP
                for row in found[:LEAD_SCAN_CAP]:  # guard-ok: declared scan bound, truncation stated in completeness
                    ref = (row["object_id"], row["l1_handle"], row["field_path"])
                    rows[ref] = row
                    index.add(ref, row["normalized"], row["property"])
            take([rows[ref] for ref in index.twins(key.normalized)], lambda _row: True,
                 "same_number_twin", "same_number")
        return out, truncated

    def _related_features(self, scope: _Scope, page: Sequence[_Match],
                          related: Sequence[_Match]) -> dict[str, list[dict[str, Any]]]:
        """One ``related_*`` feature per grammar lead on every exact/alias candidate of
        the page, citing the lead's identifier: ``related_same_record`` (agrees true) when
        ``same_party`` reads the two records as one over the visible identity links on
        either of them (the upstream stored key, e.g. a gold PO's FM id, or a person's
        review), else ``related_<reason>`` (agrees null: the same number or prefix, never
        verified). Nothing is merged or ranked by it."""
        roots = [m.object_id for m in page if m.reason in ROOT_REASONS]
        if not related or not roots:
            return {}
        types = sorted(self.schema.identity_link_types()) if self.schema is not None else []
        links = [row for row in (self.store.links_touching(
                     scope, sorted({*roots, *(m.object_id for m in related)}), types)
                     if types else ())
                 if all(self._item_allowed(scope.actor, handle)
                        for handle in json.loads(row["evidence_handles"] or "[]"))]
        # Both ends readable, as plx.object shows a card's identity list, and both held
        # by their sources at T (a removed card joins no two records).
        visible = self._visible_versions(scope, {end for row in links
                                                 for end in (row["from_object"],
                                                             row["to_object"])})
        gone = set(self.store.removals(sorted(visible), _format_time(scope.at)))
        on: dict[str, list[tuple[str, str, str]]] = {}
        for row in links:
            ends = (row["from_object"], row["to_object"])
            if all(end in visible and end not in gone for end in ends):
                for end in set(ends):
                    on.setdefault(end, []).append((*ends, row["tier"]))
        rows = self.store.values_by_citation([(m.l1_handle, m.field_path) for m in related])
        cited = {(r["l1_handle"], r["field_path"]): r for r in rows
                 if scope.holds(_markings(r["markings"]))}
        out: dict[str, list[dict[str, Any]]] = {}
        for oid in roots:
            for lead in related:
                if lead.object_id == oid:
                    continue
                same = same_party(oid, lead.object_id, dict.fromkeys(
                    [*on.get(oid, ()), *on.get(lead.object_id, ())])) is True
                feature: dict[str, Any] = {
                    "feature": "related_same_record" if same else f"related_{lead.reason}"[:64],  # guard-ok: machine feature token shape, not content
                    "agrees": True if same else None}
                value = cited.get((lead.l1_handle, lead.field_path))
                if value is not None:
                    feature["citations"] = [{"l1_handle": value["l1_handle"],
                                             "field_path": value["field_path"],
                                             "digest": value["digest"],
                                             "lineage": value["lineage"]}]
                out.setdefault(oid, []).append(feature)
        return out

    def _text_matches(self, scope: _Scope, query: str, keys: Sequence[_Key],
                      object_type: str | None) -> tuple[list[_Match], bool, int | None]:
        """Text matches over readable rows only, from the word index (store layout v8):
        the display values that name the query (``_names_query``: its words, whole and
        in order, from the value's first word; ranked first, ``exact``), then other
        display values, then the captured text of documents (``chunk_text``), then
        every other text value; an object a pass matched is not matched again by a later
        one. SQL has already applied audience, markings and time before its batch limit;
        the item ACL is then re-checked per object, and only readable objects count toward
        the cap. A query the index cannot read is a truncated search (nothing is claimed
        absent).

        An identifier of a kind no projected record carries (``text_only_kind``) is
        searched as text: its words stay in the query's words, and a match counts only
        when the grammar reads that same kind and value in the matched text."""
        # A typed find's type carrying no identifier of a mention's kind holds that
        # mention only as text ("PO#4803.pdf" of a SharePoint file: searched as the word
        # "pdf" alone, every file name holding it read, 95 s, dev 11-10).
        carried = None
        if object_type is not None and self.schema is not None:
            carried = {p.get("identifier_kind") for p in (
                self.schema.object_types.get(object_type) or {}).get("properties") or ()
                if isinstance(p, Mapping) and p.get("identifier_kind")}
        try:   # mentions the grammar reads in the query, never its whole-query readings
            text_only = {(m.kind, m.normalized) for m in self.grammar.find_identifiers(query)
                         if text_only_kind(self.grammar, m.kind)
                         or (carried is not None and m.kind not in carried)} \
                if self.grammar else set()
        except Exception:  # noqa: BLE001 - a query the grammar cannot read: words only
            text_only = set()
        tokens = _text_tokens(query, [k for k in keys if (k.kind, k.normalized)
                                      not in text_only])
        if not tokens:
            return [], False, None, False
        needle = " ".join(tokens)

        def names_all(text: Any) -> bool:
            if not text_only:
                return True
            # Each unserved kind has one written form (its shape), so text without the
            # value literally cannot name it: the grammar reads only the rows that do. A
            # kind only the asked type does not carry may be written otherwise ("PO#4803"
            # for PO4803): the grammar reads every such row.
            folded = text.casefold() if isinstance(text, str) else ""
            if not all(value.casefold() in folded for kind, value in text_only
                       if text_only_kind(self.grammar, kind)):
                return False
            try:
                read = {(m.kind, m.normalized)
                        for m in self.grammar.find_identifiers(text)}
            except Exception:  # noqa: BLE001 - text the grammar cannot read names nothing
                return False
            return text_only <= read

        deadline = self.monotonic() + _find_text_seconds()
        hybrid = self._hybrid_matches(scope, query, needle, object_type, deadline)
        if hybrid and text_only:   # a ranked near miss never stands in for the number
            hybrid = [m for m in hybrid if names_all(m.raw)]
        terms = query_terms(tokens)
        if terms is None:
            # A word the index splits or drops: nothing is claimed absent.
            return hybrid or [], True, None, False
        words = " AND ".join('"' + term.replace('"', '""') + '"*' for term in terms)
        # The query's words, each whole (the index keeps no positions, so no phrase): the
        # rows of a value that may name it (``_names_query`` decides) before any other.
        phrase = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
        asked_name = _name_words(query)
        typed = None
        if self.schema is not None:
            typed = list(_schema_memo(self.schema, ("typed", object_type), lambda: [
                f"{tid}.{prop}" for tid, props in self.schema.display().items()
                if object_type is None or tid == object_type for prop in props]))
        # One batch reads past the cap: each index query merges the words' whole row lists.
        batch = TEXT_SCAN_CAP + 1
        passes: list[tuple[str, Callable[[int], list[sqlite3.Row]]]] = [
            ("display_name", lambda after: self.store.text_matches(scope, phrase, typed, after,
                                                                   batch)),
            ("display_text", lambda after: self.store.text_matches(scope, words, typed, after,
                                                                   batch))]
        if self._may_hold_document_text(object_type):
            passes.append(("document_text", lambda after: self.store.chunk_matches(
                scope, words, object_type, after, batch)))
        if typed is not None:
            # Other text is never a declared identifier's property or carrier
            # (``_carried``): that value is found by its identifier (its grammar and scope
            # gate) or as display text, never here.
            held = typed + _schema_memo(self.schema, ("held", object_type), lambda: [
                f"{tid}.{prop['name']}" for tid, declared in self.schema.object_types.items()
                if object_type is None or tid == object_type
                for prop in declared.get("properties") or ()
                if isinstance(prop, Mapping) and prop.get("identifier_kind")])
            own = None
            if object_type is not None:
                own = list(_schema_memo(self.schema, ("own", object_type), lambda: [
                    prop["name"] for prop in (self.schema.object_types.get(object_type)
                                              or {}).get("properties") or ()
                    if isinstance(prop, Mapping) and isinstance(prop.get("name"), str)]))
            passes.append(("value_text", lambda after: self.store.text_matches(
                scope, words, held, after, batch, beside=True, object_type=object_type,
                type_properties=own or None)))
        # Hybrid hits first, so the AND-matching index only adds what search missed. A
        # hit stands as a document lead until the index matches the object's own name,
        # display text or other value: that match replaces it in its place (a task asked
        # by its id was found by a description naming the id and lost its id match).
        out: list[_Match] = list(hybrid or ())
        objects: set[str] = {m.object_id for m in out}
        ranked_only: set[str] = set(objects)
        scanned = 0
        named: set[str] = set()

        timed = False

        def cut() -> tuple[list[_Match], bool, int | None, bool]:
            # The records named by the query were all read before the cut (the name pass
            # finished): the agent is told so, never that the search was complete.
            return out, True, (len(named) if named and named_done else None), timed

        named_done = False
        try:
            for reason, fetch in passes:
                if self.monotonic() >= deadline:
                    timed = True   # what was found stands; the rest is said unread
                    return cut()
                if reason == "display_text":
                    # the name pass read its own rows: the word passes keep their budget
                    named_done, scanned = True, 0
                    if object_type is not None and named:
                        # a typed find whose words name records of the type: those are
                        # what it asks for, listed first. The word passes (any record of
                        # the type holding every word or a word's prefix) would read
                        # every row with a common prefix ("pet": our own company's name,
                        # on every record) to add leads after them: said cut instead
                        # (``scan_cap``: the named records were all read).
                        return cut()
                naming, reason = reason == "display_name", (
                    "display_text" if reason == "display_name" else reason)
                earlier, after = set(objects), 0
                while rows := fetch(after):
                    after = rows[-1]["row_no"]
                    scanned += len(rows)
                    readable = self._visible_versions(scope, {row["object_id"] for row in rows})
                    for row in rows:
                        version = readable.get(row["object_id"])
                        if reason == "value_text" and self._carried(row):
                            continue
                        if version is not None and not names_all(
                                chunk_text(row["body"]) if reason == "document_text"
                                else row["value"]):
                            continue
                        if naming and not _names_query(row["value"], asked_name):
                            continue
                        own = row["object_id"] in ranked_only and reason != "document_text"
                        match = None if version is None or (
                            row["object_id"] in earlier and not own) else (
                            _chunk_match(row, version, needle, tokens)
                            if reason == "document_text" else _value_text_match(row, reason,
                                                                                needle))
                        if match is None or (reason == "document_text"
                                             and row["object_id"] in objects):
                            continue
                        if own:
                            at = next(i for i, m in enumerate(out)
                                      if m.object_id == row["object_id"])
                            match.order = out[at].order
                            if naming:
                                match.exact = True
                                named.add(row["object_id"])
                            out[at] = match
                            ranked_only.discard(row["object_id"])
                            continue
                        if row["object_id"] not in objects and len(objects) >= TEXT_SCAN_CAP:
                            return cut()
                        if naming:
                            match.exact = True   # named by the query: ranked first
                            named.add(row["object_id"])
                        objects.add(row["object_id"])
                        out.append(match)
                    if scanned >= SCAN_ROW_BUDGET:
                        return cut()
                    if reason != "document_text" and len(rows) < batch:
                        break   # a short page is the pass's last: no statement to say so
                    if self.monotonic() >= deadline:
                        timed = True
                        return cut()
        except sqlite3.OperationalError:
            # a query the index cannot read proves no absence
            return out, True, None, False
        return out, False, None, False

    def _hybrid_matches(self, scope: _Scope, query: str, needle: str,
                        object_type: str | None, deadline: float | None = None
                        ) -> list[_Match] | None:
        """With ``PLX_SEARCH=hybrid``, the objects hybrid search ranks for ``query``
        (``operating/search``: meaning, OR-matched words, typos), as ``document_text``
        leads in its relevance order. Each passes this store's own gate at ``scope.at``
        (``_visible_versions``: audience, markings, time, item ACL), and its quoted text
        is this store's own captured text of the version shown (``object_chunks``: the
        chunk holding a query word, else its first), cited to that field: never the
        query, never the search row. An object with no stored text to quote is not a
        candidate here. None when search is off, the time asked is not now (search
        keeps no history) or search could not run: the word index alone answers."""
        from operating.search import plug
        if not plug.enabled() or not plug.is_now(scope.at, self.clock()):
            return None
        if not self._may_hold_document_text(object_type):
            # a hit is quoted from the object's stored text: a type holding none (a
            # customer, a supplier) has no hit to keep, so search is never run for it
            return None
        if _names_identifier(query):
            # a query that can name an identifier is answered by it and by the word
            # index's exact matches, never by meaning (as plx.find's prefetch and
            # plx.documents decide): its hits were only near misses, and quoting them read
            # every chunk of every hit (a PO number no record holds: 44 s)
            return None
        wait = None if deadline is None else deadline - self.monotonic()
        hits = plug.ranked(self.policy, scope.actor, self._item_allowed, query,
                           wait=wait).hits
        if not hits:
            return None
        hits = [h for h in hits if object_type is None
                or h["object_id"].split(":", 1)[0] == object_type]
        ids = [h["object_id"] for h in hits]
        readable = self._visible_versions(scope, ids)
        # the fields the hit's best-ranked search chunks were cut from (their citations):
        # only those are read, never every chunk of the object
        hints = {h["object_id"]: list(h.get("chunk_handles") or ([h["handle"]] if
                                                                 h.get("handle") else []))
                 for h in hits}
        chunks: dict[str, list[sqlite3.Row]] = {}
        for row in self.store.object_chunks(scope, {i: readable[i]["l1_handle"]
                                                    for i in ids if i in readable}, hints):
            chunks.setdefault(row["object_id"], []).append(row)
        tokens = [t.casefold() for t in _text_tokens(query, [], limit=None)]
        out: list[_Match] = []
        for rank, object_id in enumerate(ids, start=1):
            rows = chunks.get(object_id)
            if not rows or len(out) >= TEXT_SCAN_CAP:
                continue
            best, word = rows[0], None
            for row in rows:
                folded = chunk_text(row["body"]).casefold()
                word = next((t for t in tokens if t in folded), None)
                if word is not None:
                    best = row
                    break
            match = _chunk_match(best, readable[object_id], needle,
                                 [word] if word else (tokens or [""]))
            if match is not None:
                match.order = rank
                out.append(match)
        return out

    def _scan_past_empty(self, scope: _Scope, object_type: str, filters: list[dict],
                         matches: list[_Match], truncated: bool, undecided: set[str],
                         position: str | None, start: str
                         ) -> tuple[list[_Match], bool, set[str], str | None, str]:
        """The first segment of a filter listing holding a match, from ``start`` on:
        the segment already scanned when it holds one or ends the type, else the next
        ones while EMPTY_SCAN_SECONDS (and the call's own limit) allow. Returns the
        segment's matches, whether it was cut, the undecided properties of every segment
        read, where it resumes and where it started."""
        from operating import call_limits
        ends = time.monotonic() + EMPTY_SCAN_SECONDS
        while not matches and truncated and position is not None and position > start:
            left = call_limits.seconds_left()
            if time.monotonic() >= ends or (left is not None
                                            and left < EMPTY_SCAN_MARGIN_SECONDS):
                break
            start = position
            matches, truncated, more, position = self._match_filters(
                scope, object_type, filters, start)
            undecided = set(undecided) | more
        return matches, truncated, undecided, position, start

    def _scan_filters(self, scope: _Scope, object_type: str, filters: list[dict],
                      start: str, *, ids_only: bool = False
                      ) -> tuple[list[_Match], bool, set[str], str | None]:
        """``_match_filters``'s scan of one segment, never kept."""
        matched: list[str] = []
        undecided: set[str] = set()
        versions: dict[str, sqlite3.Row] = {}
        after, readable_count, scanned, truncated = start, 0, 0, False
        position = start
        links = self._link_filters(object_type, filters)
        among: list[str] | None = None
        if links:
            # a filter on a link: only the records it joins to the named ones are read; one
            # only a withheld link joins is never read as unlinked
            linked, hidden = self._linked_ids(scope, object_type, links)
            marked = _target_marks(hidden)
            undecided |= marked
            if hidden - marked:
                undecided |= {f["property"] for f in links}
            among = sorted(linked)
            filters = [f for f in filters if f not in links]
            if not among:
                return [], False, undecided, None
        timed = self.event_names(object_type)
        declared = self._declared_names(object_type)
        narrowing = _value_narrowing([f for f in filters if f.get("property") in declared])
        listed = False
        if among is None and narrowing and not start:
            # a scan's first segment only: a later one resumes a span scan (the value
            # read was not selective, or the time ran short), never reads it again
            among = self._narrowed_first(scope, object_type, narrowing)
            if among is not None and not among:
                return [], False, undecided, None
            listed = among is not None
        events = [(timed[f["property"]], f["op"], _event_bound(f))
                  for f in filters if f.get("property") in timed
                  and f.get("property") not in declared and f.get("op") != "absent"]
        from operating import call_limits
        while True:
            # A narrowed statement may pass over many records to find a batch: it reads
            # at most NARROWED_SPAN keys of the type, so the scan can stop between
            # statements when the call's time runs short and answer what it decided,
            # with where to resume, never nothing at its limit (11-06 q17: a supplier
            # name prefix over a cold store stopped at 40 s and returned nothing).
            upto = self.store.key_ahead(object_type, after, NARROWED_SPAN) \
                if (narrowing or events) and not listed else None
            rows = self.store.objects_of_type(scope, object_type, after, FILTER_BATCH,
                                              narrowing, among, events, upto, versions=True)
            left = call_limits.seconds_left()
            short = left is not None and left < SCAN_MARGIN_SECONDS
            if not rows:
                if upto is None:
                    break
                # the keys up to ``upto`` are decided, but a cursor resumes only from a
                # record this reader may read (``position``): never from a key it may not
                after = upto
                if short:
                    truncated = True
                    break
                continue
            full = len(rows) >= FILTER_BATCH
            # a statement that did not fill its batch decided every key up to its span
            after = rows[-1]["object_id"] if full or upto is None else upto
            at_end = upto is None and not full
            scanned += len(rows)
            # the rows are the versions the scan's own statement read (SQL rules applied);
            # each still passes the item gate here, as ``_visible_versions`` gates
            batch = self._gated_versions(scope, rows)
            ids = list(dict.fromkeys(r["object_id"] for r in rows if r["object_id"] in batch))
            if readable_count + len(ids) > FILTER_SCAN_CAP:
                ids = ids[:FILTER_SCAN_CAP - readable_count]
                truncated = True
                # Rows past the last readable object kept are scanned again next time.
                position = ids[-1] if ids else position
            else:
                # a span decided to its end resumes after its last readable record
                position = after if after != upto else (ids[-1] if ids else position)
            readable_count += len(ids)
            versions.update(batch)
            passing, unsure = self._filter_objects(scope, ids, filters)
            undecided |= unsure
            matched.extend(oid for oid in ids if oid in passing)
            if truncated or scanned >= SCAN_ROW_BUDGET or (short and not at_end):
                truncated = True
                break
        out = [_MatchedId(oid) for oid in matched] if ids_only else \
            self._filter_matches(scope, object_type, filters, matched, versions)
        return out, truncated, undecided, position if truncated else None

    def _identity_twins(self, scope: _Scope, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Each of ``ids``' identity twins this reader sees (a link of an identity type at
        an identity tier, live at T, its evidence and its other end readable): its
        records in other systems, each with its lineage at T."""
        types = sorted(self.schema.identity_link_types()) if self.schema is not None else []
        if not ids or not types:
            return {}
        wanted = set(ids)
        rows = [row for row in self.store.links_touching(scope, sorted(wanted), types)
                if row["from_object"] != row["to_object"]
                and all(self._item_allowed(scope.actor, handle)
                        for handle in json.loads(row["evidence_handles"] or "[]"))]
        visible = self._visible_versions(
            scope, {end for row in rows for end in (row["from_object"], row["to_object"])})
        # a twin its source no longer holds at T (a soft-deleted Portal card, a record
        # missing from its source) is no copy of the record now: never listed as one
        gone = set(self.store.removals(sorted({end for row in rows for end in (
            row["from_object"], row["to_object"]) if end not in wanted}), _format_time(scope.at)))
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            ends = (row["from_object"], row["to_object"])
            if not all(end in visible for end in ends):
                continue
            tiered = dict(row)
            tiered.update(from_lineage=visible[ends[0]]["lineage"],
                          to_lineage=visible[ends[1]]["lineage"])
            if _effective_tier(tiered) not in _IDENTITY_TIERS:
                continue
            for one, other in (ends, ends[::-1]):
                if one in wanted and other not in gone:
                    out.setdefault(one, {})[other] = visible[other]["lineage"]
        return out

    def _candidates(self, scope: _Scope, page: list[_Match], versions: Mapping[str, Any],
                    offset: int, corroboration: Mapping[str, list[dict]] | None = None,
                    asked: Any = None) -> list[dict[str, Any]]:
        displays = self._displays(scope, [m.object_id for m in page])
        limit = self.limits["max_untrusted_text_chars"]
        out = []
        for index, match in enumerate(page):
            version = versions[match.object_id]
            raw = _envelope(match.raw, match.l1_handle, match.field_path, limit,
                            asked) or {
                "untrusted_text": match.normalized[:limit],
                "origin": {"citation": version["l1_handle"]}}
            if "withheld" not in raw and withheld_po_text(match.field_path, match.raw) \
                    and not _same_text(match.raw, asked):
                raw = WithheldText(raw["origin"], str(match.raw))
            if match.sender_class in SENDER_CLASSES and "field_path" in raw["origin"]:
                raw["origin"]["sender_class"] = match.sender_class
            object_type = match.object_id.split(":", 1)[0]
            normalized, normalized_gap = identifier_out(
                match.kind, match.normalized, f"{object_type}.{match.property}",
                raw["origin"]["citation"], raw["origin"].get("field_path"), limit)
            matched = {"identifier_kind": match.kind, "normalized": normalized, "raw": raw,
                       "property": match.property, "binding": match.binding,
                       "role": match.role}
            if normalized_gap is not None:
                matched["normalized_gap"] = normalized_gap
            candidate = {"object": {"object_id": match.object_id,
                                    "object_type": object_type,
                                    "display": displays.get(match.object_id),
                                    "lineage": version["lineage"]},
                         "rank": offset + index + 1, "matched": matched,
                         "reason": match.reason, "tier": match.tier}
            why_not = [dict(feature) for feature in match.why_not]
            why_not += (corroboration or {}).get(match.object_id, [])
            if why_not:
                candidate["why_not"] = why_not
            out.append(candidate)
        return out

    def _displays(self, scope: _Scope, object_ids: list[str]) -> dict[str, dict | None]:
        """Display text of readable objects (the caller passes only those)."""
        if not object_ids:
            return {}
        display = self.schema.display() if self.schema else {}
        limit = self.limits["max_untrusted_text_chars"]
        out: dict[str, dict | None] = {}
        by_object: dict[str, dict[str, sqlite3.Row]] = {}
        # only the display properties of these objects' types are read, never every value
        shown = sorted({p for object_id in set(object_ids)
                        for p in display.get(object_id.split(":", 1)[0], ())})
        for row in self.store.readable_values(scope, object_ids, shown) if shown else ():
            if row["value"] is not None:
                by_object.setdefault(row["object_id"], {}).setdefault(row["property"], row)
        idents: dict[str, sqlite3.Row] = {}
        for row in self.store.primary_identifiers(scope, object_ids):
            idents.setdefault(row["object_id"], row)
        for object_id in object_ids:
            props = display.get(object_id.split(":", 1)[0], ())
            row = next((by_object.get(object_id, {}).get(p) for p in props
                        if p in by_object.get(object_id, {})), None)
            if row is not None:
                out[object_id] = _envelope(row["value"], row["l1_handle"], row["field_path"], limit)
            elif object_id in idents:
                ident = idents[object_id]
                out[object_id] = _envelope(ident["raw"], ident["l1_handle"],
                                           ident["field_path"], limit)
            else:
                out[object_id] = None
        return out

    # ------------------------------------------------------------------ plx.object

    def _object(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        if arguments.get("history"):   # its change log (operating.ontology_tools_history)
            from operating.ontology_tools_history import object_history
            return object_history(self, arguments, actor)
        if arguments.get("cursor") is not None:
            raise ToolError("invalid_input", "cursor pages a history (history: true)")
        at = self._as_known_at(arguments)
        scope = self._scope(actor, at)
        store_meta = self._store_meta()
        object_id = arguments["object_id"]
        versions = self._visible_versions(scope, [object_id])
        version = versions.get(object_id)
        if version is None:
            # Absent, outside the actor's audience, marked beyond the actor, or not
            # yet captured at T: one answer, so nothing about the object leaks.
            if self.sets.live_id(object_id, actor):
                # Only the reader whose plx.find answered it live is told so.
                raise ToolError("not_in_projection",
                                "the object came from a live lookup, not the projection store",
                                LIVE_LOOKUP_HINT)
            raise ToolError("object_not_found", "no such object is visible to this reader",
                            "resolve the identifier with plx.find first")
        obj = self.store.objects([object_id])[object_id]
        object_type = obj["object_type"]
        type_spec = (self.schema.object_types.get(object_type) if self.schema else None) or {}
        wanted = set(arguments.get("properties", ())) or None
        detailed = arguments.get("response_format") == "detailed"
        limit = self.limits["max_untrusted_text_chars"]

        values: list[dict] = []
        gaps: list[dict] = []
        withheld_props: set[str] = set()
        seen_props: set[str] = set()
        unreadable: set[str] = set()
        stored = self.store.scoped_currency(scope, self.store.values([object_id]))
        first_seen: dict[tuple[str, str], Any] = {}   # when each shown value was first seen
        # A marking a property's latest value carries applies to its earlier values too
        # (an as_known_at read never reads around a later marking, as for an object).
        latest: dict[str, tuple[Any, tuple[str, ...]]] = {}
        for row in stored:
            when = _parse_time(row["recorded_at"])
            held_by = latest.get(row["property"])
            if held_by is None or (when is not None and (held_by[0] is None
                                                         or when > held_by[0])):
                latest[row["property"]] = (when, _markings(row["markings"]))
        for row in stored:
            if wanted is not None and row["property"] not in wanted:
                continue
            known = _known_at(row, at)
            held = scope.holds(_markings(row["markings"])) and scope.holds(
                latest[row["property"]][1])
            if known is None:
                if held:
                    unreadable.add(row["property"])
                continue
            if not known:
                continue
            if not held:
                withheld_props.add(row["property"])
                continue
            value, problem = _value_out(row, object_type, limit)
            seen_props.add(row["property"])
            if value is None:
                gaps.append(problem)
            else:
                values.append(value)
                first_seen[(row["l1_handle"], row["field_path"])] = row["recorded_at"]
        withheld_props -= seen_props
        for name in sorted(unreadable - seen_props):
            seen_props.add(name)
            gaps.append(_gap("unknown", "unreadable_store_time", f"{object_type}.{name}",
                             "a stored value's capture time could not be read; it was left out"))

        identity, linked_counts, linked_values, link_gaps = self._links(scope, object_id,
                                                                        detailed)
        gaps.extend(link_gaps)
        beyond = self._beyond(scope, object_id)
        if len(identity) > MAX_IDENTITY:   # best tier first; the cut heads the gaps
            gaps.insert(0, _gap("unknown", "identity_truncated", object_id,
                                f"{len(identity)} identity links, the first {MAX_IDENTITY} "
                                "shown (verified and reviewed before leads); plx.identity "
                                "on this object lists them by tier"))
        gaps.extend(self._dropped_version_gaps([object_id], store_meta))
        from operating.attested_conflicts import conflicts as attested, label_reads
        label_reads(self, scope, object_id, object_type, values)   # parser or OCR (P2)
        if self.reviews is not None:   # a person's confirmation or correction, beside it
            from operating.ontology_tools_review import show_value_reviews
            show_value_reviews(self, self.reviews, scope, object_id, values)
        conflicts = _conflicts(object_type, values, linked_values, wanted)
        leads: list[dict] = []
        if wanted is None:   # what a captured document attests vs this record (P8)
            found, not_compared, leads = attested(self, scope, object_id, object_type)
            conflicts.extend(found)
            gaps.extend(not_compared)
        _resolve_conflicts(conflicts, first_seen, linked_values)

        removal = self.store.removals([object_id], _format_time(at)).get(object_id)
        if removal is not None:
            gaps.append(removal_gap(*removal, object_id))
        for prop in type_spec.get("properties", ()):
            if wanted is not None and prop["name"] not in wanted:
                continue
            state = prop.get("availability")
            if state == "not_served":
                gaps.append(_gap("inaccessible", "not_served", f"{object_type}.{prop['name']}",
                                 prop.get("availability_reason")))
            elif state == "degraded":
                gaps.append(_gap("unknown", "degraded_binding", f"{object_type}.{prop['name']}",
                                 prop.get("availability_reason")))
        unparsed = self._unparsed_value_gaps(scope, object_id, version["l1_handle"], type_spec,
                                             store_meta, wanted, seen_props | withheld_props)
        gaps.extend(unparsed[name] for name in sorted(unparsed))
        for name in sorted((wanted or set()) - seen_props - withheld_props - set(unparsed)):
            # No stored read of the property: the projection keeps no row for a field
            # it could not read (a column not mirrored, unparseable JSON or value), so
            # absence is not shown. A field read as empty is a stored null value.
            gaps.append(_gap("unknown", "no_value", f"{object_type}.{name}",
                             "the projection holds no read of this property for this "
                             "record; absence from the source is not established"))

        sources = {_source_of(version["l1_handle"], version["lineage"])}
        sources |= {_source_of(v["citation"]["l1_handle"]) for v in values}
        meta = self._meta(store_meta, at, sources - {None})
        gaps.extend(self._freshness_gaps(meta, store_meta))
        display = self._displays(scope, [object_id]).get(object_id)
        result: dict[str, Any] = {
            "object": {"object_id": object_id, "object_type": object_type, "display": display,
                       "lineage": version["lineage"]},
            "values": values, "conflicts": conflicts, "identity": identity,
            "linked_counts": linked_counts, "gaps": gaps}
        company = bc_company(version["lineage"], version["l1_handle"])
        if company is not None:
            result["object"]["company"] = company
        derived, checked = self._derived(scope, object_id, object_type, wanted)
        if checked is not None:
            # the header against its lines comes first: a later gap is cut before it
            result["gaps"] = [checked, *gaps]
        if derived:
            result["derived"] = derived
        if beyond:
            result["linked_beyond"] = beyond
        twins = self._twins_named(scope, object_id, identity)
        if twins:
            result["twins"] = twins
        if withheld_props:
            result["withheld"] = {"properties": len(withheld_props), "objects": None,
                                  "reason": "marking"}
        result["meta"] = meta
        total = len(values)
        result["completeness"] = _completeness(total, total, False, None, None)
        if len(values) > 400 or _envelope_bytes(result) > self.limits["max_result_bytes"]:
            result = self._fit_object(result, total)
        return self._with_leads(result, leads, object_id, total) if leads else result


def _rewound_cursor(cursor: Any, by: int) -> str | None:
    """A search page's offset cursor (:func:`_write_cursor`) resuming ``by`` items
    earlier, so a page a host shortened after it was cut (to the response bound) skips
    nothing; None for any other cursor (a scan cursor, malformed) or one that would
    resume before the start. Its fingerprint, time and ranking mode are kept. A filter
    listing's scan cursor is rewound within its segment (:func:`_rewound_scan_cursor`)."""
    if isinstance(cursor, str) and cursor.startswith(SCAN_CURSOR_PREFIX) and by >= 0:
        return _rewound_scan_cursor(cursor, by)
    if not isinstance(cursor, str) or cursor.startswith(SCAN_CURSOR_PREFIX) or by < 0:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict) or set(body) - {"o", "t", "f", "m"} \
            or not {"o", "t", "f"} <= set(body) or type(body["o"]) is not int \
            or body["o"] - by < 0:
        return None
    body["o"] -= by
    raw = json.dumps(body, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _rewound_scan_cursor(cursor: str, by: int) -> str | None:
    """A filter listing's scan cursor (:func:`_write_scan_cursor`) resuming ``by`` matches
    earlier in the same segment (its offset moved back; segment, earlier matches, time,
    reader and generation kept, the check recomputed), so a page a host shortened skips
    nothing; None when that would go before the segment's start (a cursor resuming at a
    new segment's start: where the last segment's page began is not in it), for a
    malformed cursor, or past the cursor bound. Only the host's own page trimming calls
    it, on the cursor the tool just wrote."""
    try:
        payload = cursor[len(SCAN_CURSOR_PREFIX):]
        inflater = zlib.decompressobj()
        raw = inflater.decompress(base64.urlsafe_b64decode(
            (payload + "=" * (-len(payload) % 4)).encode()), 4096)
        if inflater.unconsumed_tail or not inflater.eof:
            return None
        body = json.loads(raw.decode())
        if not isinstance(body, dict) or body.get("h") != _scan_check(body) \
                or type(body.get("o")) is not int or body["o"] - by < 0:
            return None
    except (ValueError, KeyError, TypeError, zlib.error):
        return None
    if by == 0:
        return cursor
    body["o"] -= by
    del body["h"]
    body["h"] = _scan_check(body)
    raw = zlib.compress(json.dumps(body, separators=(",", ":")).encode(), 9)
    out = SCAN_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return out if len(out) <= CURSOR_MAX_CHARS else None


def _write_scan_cursor(offset: int, at: dt.datetime, fingerprint: str, after: str,
                       prior: int, undecided: Sequence[str], last: Sequence[str],
                       generation: str) -> str | None:
    """A filter listing's cursor: page ``offset`` of the scan segment starting after
    object id ``after``, ``prior`` matches listed by earlier segments (``last``: the
    last one or two of them, repeated on a segment of fewer than two), the filtered
    properties they were undecided on, bound to the query and reader (``fingerprint``)
    and the projection ``generation``; deflated (object ids share long prefixes). None
    when it would still exceed the cursor bound, or when it resumes a scan (``after`` or
    ``prior``) on a store stating no generation (a resume could not tell a rebuilt
    store's object ids from these): the listing then stays cut."""
    if not generation and (after or prior):
        return None
    body: dict[str, Any] = {"o": offset, "t": _format_time(at), "f": fingerprint,
                            "a": after, "n": prior, "u": list(undecided), "k": list(last),
                            "g": _generation_tag(generation)}
    body["h"] = _scan_check(body)
    raw = zlib.compress(json.dumps(body, separators=(",", ":")).encode(), 9)
    cursor = SCAN_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return cursor if len(cursor) <= CURSOR_MAX_CHARS else None


def _valid_properties(object_type: str, names: Sequence[str], verb: str = "filter on"
                      ) -> str:
    """The hint of ``unknown_property``: the type's declared properties, as many as fit,
    in the order given (``property_hint``: the closest to the name asked first)."""
    head = f"{verb} one of {object_type}'s properties: "
    tail = "; plx.schema with object_type lists them all"
    listed: list[str] = []
    for name in names:
        if len(head) + len(", ".join(listed + [name])) + len(tail) + 16 > 600:
            return head + ", ".join(listed) + f" (+{len(names) - len(listed)} more)" + tail
        listed.append(name)
    return head + ", ".join(listed) if listed else f"{object_type} declares no properties"


def property_hint(schema: Any, object_type: str, missing: Sequence[str], *,
                  verb: str = "filter on", value_types: Collection[str] | None = None) -> str:
    """``_valid_properties`` for ``object_type`` of ``schema`` (an L8 schema view), those
    whose names are closest to the ``missing`` ones first (``vendor`` brings
    ``vendor_name``, ``total_amount`` brings ``total_cost``), so the next call can name
    one; only properties of ``value_types`` when given (a sum names a number, quantity or
    money)."""
    import difflib
    specs = [p for p in (getattr(schema, "object_types", {}) or {}).get(object_type, {})
             .get("properties", ()) if isinstance(p, Mapping) and p.get("name")]
    if value_types is not None:
        specs = [p for p in specs if p.get("value_type") in value_types]
    names = sorted(dict.fromkeys(p["name"] for p in specs))
    close: list[str] = []
    for wanted in missing:
        parts = set(wanted.split("_"))
        close += difflib.get_close_matches(wanted, names, n=6, cutoff=0.5)
        close += [n for n in names if parts & set(n.split("_")) and n not in close]
    ranked = list(dict.fromkeys(close + names))
    return _valid_properties(object_type, ranked) if verb == "filter on" \
        else _valid_properties(object_type, ranked, verb)


def _value_narrowing(filters: Sequence[Mapping[str, Any]]
                     ) -> list[tuple[str, dict[str, Any]]]:
    """Per filter whose matches a store condition can bound, (an SQL condition on one
    stored value ``x`` of the object, its parameters): every value ``_filter_holds``
    can match meets it, so an object holding no such value (and no value of the property
    the reader may not read: ``objects_of_type`` adds that) can neither match nor leave
    the filter undecided, and the scan skips it. A superset, never the verdict itself:
    ``present``; ``prefix`` (an ASCII prefix, any case); ``contains`` (the text anywhere); ``eq``/``in`` on text (a word
    of the wanted value, any case; numbers compare as numbers and are kept); ``gte``/``gt``/
    ``lt``/``lte`` on a date or time (ISO days two days wide of the bound; other kinds kept).
    Values outside ASCII are kept wherever case folding decides. Other filters (``ne``,
    ``absent``, a number bound) narrow nothing."""
    out: list[tuple[str, dict[str, Any]]] = []
    for f in filters:
        op, prop = f.get("op"), f.get("property")
        wanted = f.get("value")
        targets = wanted if isinstance(wanted, list) else [wanted]
        if not isinstance(prop, str):
            continue
        if op == "present":
            out.append(("x.value IS NOT NULL", {"property": prop}))
            continue
        if op not in ("prefix", "contains", "eq", "in", "gte", "gt", "lt", "lte") \
                or not targets or len(targets) > 20:
            continue
        if op in ("gte", "gt", "lt", "lte"):
            moment = _parse_time(targets[0]) if isinstance(targets[0], str) else None
            if moment is None:
                continue
            lower = op in ("gte", "gt")
            day = (moment.date() + dt.timedelta(days=-2 if lower else 2)).isoformat()
            out.append((f"x.value_type NOT IN ('date', 'timestamp') OR NOT {_ISO_DAY} OR "
                        f"x.value {'>=' if lower else '<'} :n_day",
                        {"property": prop, "day": day}))
            continue
        words: list[str] = []
        for target in targets:
            if isinstance(target, bool) or target is None:
                break
            text = str(target)
            if op != "prefix":
                text = max(text.split(), key=len, default="")
            if not text or not text.isascii() or "\\" in text:
                break   # Postgres' ILIKE reads a backslash as its escape
            words.append(text.lower())
        else:
            # LIKE ignores ASCII case (ILIKE on Postgres); a % or _ in a word only
            # widens what is kept
            keep = f"{_NOT_ASCII} OR " + ("" if op in ("prefix", "contains") else
                                          f"x.value_type IN {_NUMERIC_TYPES} OR ")
            likes = " OR ".join(f"x.value LIKE :n_w{i}" for i in range(len(words)))
            values = {"property": prop}
            values.update({f"w{i}": (w + "%" if op == "prefix" else "%" + w + "%")
                           for i, w in enumerate(words)})
            out.append((keep + likes, values))
    return out


def identifier_out(kind: str | None, normalized: Any, subject: str, citation: str,
                   field_path: str | None, limit: int
                   ) -> tuple[str | None, dict[str, Any] | None]:
    """A normalized identifier as a tool emits it: ``(normalized, None)`` when it is a
    typed identifier of ``kind`` (``injection_guard.is_identifier``: $defs/identifier_value
    and the kind's value pattern), else ``(None, gap)``: an ``identifier_invalid`` gap whose
    ``source_text`` quotes the value as an envelope under ``citation`` (a customer PO
    field holding a name or a sentence reaches a model only as data).

    A copy identity (a kind whose grammar declares ``copies``) is checked with its
    ``citation`` too: read from a document it is bare only when its kind is kind-specific
    (a digest), so a sender-written Message-ID is quoted source text at the tool, not only
    at the MCP boundary. Other kinds keep the boundary's source-text rule (IB-FIREWALL-4):
    a value a document-field profile typed is compared in process (attested conflicts)."""
    from ontology import identifiers
    copies = kind in identifiers.kinds() and bool(identifiers.kind_spec(kind).get("copies"))
    if withheld_po_text(field_path, normalized):
        return None, _gap("unknown", "value_withheld_not_po_like", subject,
                          "the recorded po_reference value is not PO-like; it can be a "
                          "person's name and is never echoed")
    if normalized is None or is_identifier(normalized, kind, citation if copies else None):
        return normalized, None
    gap = _gap("unknown", "identifier_invalid", subject,
               "the recorded value is not a typed identifier of its kind; its text is "
               "quoted as source text")
    quoted = _envelope(normalized, citation, field_path, limit)
    return None, (dict(gap, source_text=quoted) if quoted is not None else gap)


def _value_out(row: Mapping[str, Any], object_type: str, limit: int
               ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """One stored value as the contract's ``value``, or (None, gap) when the row
    cannot be cited or does not fit its declared type."""
    subject = f"{object_type}.{row['property']}"
    if not _NAME.match(row["property"] or ""):
        return None, _gap("unknown", "unnamed_property", subject)
    citation = _citation(row)
    if citation is None:
        return None, _gap("unknown", "uncitable_value", subject,
                          "the stored value lacks a valid L1 handle, field path or digest")
    value_type, raw = row["value_type"], row["value"]
    value: Any = raw
    value_gap = None
    if raw is not None:
        if value_type == "identifier":
            value, value_gap = identifier_out(
                identifier_kind_of(object_type, row["property"]), raw, subject,
                row["l1_handle"], row["field_path"], limit)
        elif value_type in ("text", "json"):
            value = _envelope(raw, row["l1_handle"], row["field_path"], limit)
        elif value_type in ("decimal", "money", "quantity"):
            value = raw if _DECIMAL.match(raw) else _BAD
        elif value_type == "integer":
            value = int(raw) if re.fullmatch(r"-?[0-9]{1,18}", raw) else _BAD
        elif value_type == "boolean":
            value = {"true": True, "false": False}.get(raw, _BAD)
        elif value_type == "date":
            value = raw if _DATE.match(raw) else _BAD
        elif value_type == "timestamp":
            value = raw if _TIMESTAMP.match(raw) else _BAD
        if value is _BAD:
            return None, _gap("unknown", "unparseable_value", subject,
                              f"the stored value does not fit value type {value_type}")
    out: dict[str, Any] = {"property": row["property"], "value_type": value_type,
                           "value": value, "citation": citation,
                           "authority_rank": row["authority_rank"],
                           "markings": list(_markings(row["markings"]))}
    if value_gap is not None:
        out["value_gap"] = value_gap
    if value_type == "quantity" or row["unit"] is not None:
        unit = row["unit"]
        out["unit"] = unit_out(unit)
        if unit is None:
            out["unit_gap"] = _gap("unknown", "unit_unknown", subject,
                                   "no unit is recorded for this value")
        elif out["unit"] is None:
            # A source unit cell that is not a unit token ($defs/unit): the value stays
            # readable, the unit is a gap quoting the cell as source text.
            out["unit_gap"] = dict(_gap("unknown", "unit_invalid", subject,
                                        "the recorded unit is not a unit of measure; its "
                                        "text is quoted as source text"),
                                   source_text=_envelope(unit, row["l1_handle"], None, limit))
    if row["quantity_role"] is not None:
        out["quantity_role"] = row["quantity_role"]
    if value_type == "money" or row["currency"] is not None:
        # A currency read on another record (its company's local currency) cites it; one
        # whose citation does not parse is no currency.
        text = row["currency_citation"] if "currency_citation" in row.keys() else None
        cited = _citation_text(text) if text is not None else None
        currency = row["currency"] if text is None or cited is not None else None
        out["currency"] = currency
        if currency is None:
            out["currency_gap"] = _gap("unknown", "currency_unknown", subject,
                                       "no currency is recorded for this value")
        elif cited is not None:
            out["currency_citation"] = cited
    return out, None


def _conflicts(object_type: str, values: list[dict], peer_values: list[tuple[str, Mapping]],
               wanted: set[str] | None) -> list[dict]:
    """Per-property disagreement between this object's visible values and those
    of identity peers (an identity link at verified/reviewed). Never resolved here;
    authority_rank is shown, the conflict stays."""
    by_property: dict[str, list[dict]] = {}
    for value in values:
        by_property.setdefault(value["property"], []).append(value)
    for _, row in peer_values:
        if wanted is not None and row["property"] not in wanted:
            continue
        value, _ = _value_out(row, object_type, 2000)
        if value is not None:
            by_property.setdefault(value["property"], []).append(value)
    out = []
    for prop, items in sorted(by_property.items()):
        # A withheld text (``WithheldText``) compares on its source text, so a peer holding
        # the same text is no conflict and a different one stays one; unknown without it.
        present = [v for v in items if compared_text(v["value"]) is not None]
        keys = {_compare_key(v["value_type"], compared_text(v["value"])) for v in present}
        if len(keys) >= 2:
            out.append({"kind": "value_disagreement", "subject": f"{object_type}.{prop}",
                        "values": present})
    return out


__all__ = ["OntologyTools", "ROOT_REASONS", "removal_gap", "same_party", "ReadPolicy", "SchemaView",
           "SetRegistry", "StoreReader", "TOOL_NAMES", "default_grammar", "default_schema",
           "load_contracts", "shared_schema"]
