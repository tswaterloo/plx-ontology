"""Source mirror ingest rules: the ingest configuration, lineage, soft-delete and
replacement rules, and the sighting SQL the mirror records rows with (split from
``integrations.source_mirror``, which re-exports every name)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from operating.declared_schema.source_mirror import _DDL, _CHANGE_DDL
from .source_mirror_values import _canonical, _sha


INGEST_SCHEMA = "plx-source-mirror-ingest/v1"
DEFAULT_INGEST_PATH = Path(__file__).with_name("source_mirror_ingest.json")
# The C0 lineage values (ontology/schema/_format.schema.json $defs.lineage) a Portal mirror row can carry. Like the C0 binding examples, a gold row with
# no exposed origin marker defaults to gold_native_staging (never authority until a
# human confirms it, Q1); its rule id (lineage_rule ``gold_no_origin_marker``)
# says the origin is unproven. A row no rule matches has lineage None.
LINEAGES = frozenset({"fm_bronze", "sage_bronze", "silver_view", "gold_etl_from_fm",
                      "gold_etl_from_sage", "gold_native_staging"})
UNCLASSIFIED = (None, "no_lineage_rule")
_LINEAGE_OPS = ("eq", "prefix", "gte", "in", "is_null", "present")
# The highest ``max_rows_per_relation`` a configuration may set. The bound stops a
# runaway traversal (one that keeps reading rows appended behind its cursor, as the
# Portal ETL's cuid() re-inserts can); it is counted over the whole traversal, across
# the runs a budget-cut traversal resumes in, and must never cut a real relation.
# Measured 2026-10-29 by key-ordered full reads (page size 5000): the largest served
# relation, public.app_manufacturing_order_line, holds 31,658,270 rows (262,608
# natural keys: the ETL's per-run copies), read in 3,608 s (8,776 rows/s). The
# configured bound (source_mirror_ingest.json) is about twice that; the cap leaves
# room to raise it again without letting one traversal stage an unbounded file.
MAX_ROWS_PER_RELATION_CAP = 100_000_000
_SYNC_KEYS = {"page_size": (1, 5000), "min_page_size": (1, 5000),
              "max_rows_per_relation": (1, MAX_ROWS_PER_RELATION_CAP),
              "throttle_attempts": (1, 20),
              "max_retry_after_seconds": (0, 3600)}
# Why a key was not held by its source over a ``row_removal`` interval; the row label
# of a soft delete says how its time was read (the source's value, or the capture).
MISSING_FROM_SOURCE = "missing_from_source"
SOFT_DELETE = "source_soft_delete"
SOFT_DELETE_OBSERVED = "source_soft_delete_observed"
# How a closed ``row_removal`` interval's end was read: the row's own update time, or
# the capture that saw the key held again.
UNTIL_SOURCE_UPDATE = "source_updated_at"
UNTIL_OBSERVED = "capture_observed"
# The label of a missing key whose successor a full capture found (module docstring).
SOURCE_REPLACEMENT = "source_replacement"
AMBIGUOUS_REPLACEMENT = "ambiguous_replacement"
# The rule :func:`replacement_key` applies; a mirror file labelled under another one
# drops, when opened, the labels the rule in force no longer infers, and its labels are
# not read before (``ontology.project.MirrorCaptures``). v2: a unique constraint on the
# cascade parent alone pairs again (v1 dropped it).
REPLACEMENT_RULE = "beyond-cascade-parent/v2"
PRISMA_FACTS_PATH = (Path(__file__).resolve().parent.parent / "ontology" / "schema" /
                     "generated" / "meta" / "portal_prisma.json")


def soft_delete_columns(rules: Sequence[Mapping[str, Any]], relation: str,
                        columns: Iterable[str]) -> list[str]:
    """The declared soft-delete columns ``relation``'s catalog ``columns`` carry."""
    have = set(columns)
    return sorted({rule["column"] for rule in rules if rule["column"] in have and
                   any(_pattern_matches(p, relation) for p in rule["relations"])})


def stale_replacement_relations(conn: sqlite3.Connection,
                                models: Mapping[str, Mapping[str, Any]],
                                relation: str | None = None) -> list[str]:
    """The relations of mirror file ``conn`` (of ``relation`` alone when given) holding a
    replacement label that :func:`replacement_key` under ``models`` no longer infers, for
    their stored columns (a relation not in ``relation_state`` has none, so no key). The
    one rule a writer clearing stale labels and a read-only reader skipping them apply."""
    return [name for name, columns in conn.execute(
                "SELECT DISTINCT rm.relation, rs.columns_json FROM row_removal rm "
                "LEFT JOIN relation_state rs ON rs.relation = rm.relation "
                "WHERE rm.replaced_basis IS NOT NULL AND (? IS NULL OR rm.relation = ?) "
                "ORDER BY rm.relation", (relation, relation))
            if replacement_key(models, name, json.loads(columns) if columns else ()) is None]


def replacement_key(models: Mapping[str, Mapping[str, Any]], relation: str,
                    columns: Iterable[str]) -> list[str] | None:
    """The columns a successor of a missing ``relation`` row must match, or None when
    no replacement is inferred: the model has no owning parent (a relation with
    ``onDelete: Cascade``), or ``columns`` (the stored ones) lack one of them. The
    natural key is a unique constraint holding an owner's columns and no id column
    (the owner's alone included: the source holds one row per value at a time), else
    every foreign-key column (parent and the rest, pairing one to one only) less an
    unserved ``onDelete: SetNull`` one, never when those are the parent's columns alone
    (an unrelated delete and add under one parent is no replacement)."""
    model = models.get(relation)
    if model is None:
        return None
    owners = [set(r["columns"]) for r in model["relations"]
              if r["on_delete"] == "Cascade" and r["columns"]]
    if not owners:
        return None
    ident = set(model["id"])
    natural = next((u for u in model["unique"] if not ident & set(u)
                    and any(owner <= set(u) for owner in owners)), None)
    if natural is None:
        # An optional link the Portal clears itself (onDelete SetNull) that the source does
        # not serve takes no part: it is unknown on both sides of every pairing, and the
        # one-to-one rule of pair_replacements still holds on the served keys.
        natural = list(dict.fromkeys(
            c for r in model["relations"] for c in r["columns"]
            if r["on_delete"] != "SetNull" or set(r["columns"]) <= set(columns)))
        if any(set(natural) <= owner for owner in owners):
            return None
    return list(natural) if not ident & set(natural) and set(natural) <= set(columns) \
        else None


def natural_text(row: Mapping[str, Any], natural: Sequence[str]) -> str | None:
    """The canonical natural-key values of ``row``, or None when one is null (a null
    never matches, as in a unique constraint)."""
    values = [row.get(column) for column in natural]
    return None if any(value is None for value in values) else _canonical(values)


def pair_replacements(vanished: Mapping[str, str], appeared: Mapping[str, str],
                      kept: Mapping[str, int]) -> dict[str, tuple[str, list[str]]]:
    """Missing key -> (basis, successor keys) for the keys of ``vanished`` (key ->
    natural text, gone since the previous capture) whose natural text a key of
    ``appeared`` (new since then) carries. ``kept``: natural text -> keys held in both
    captures. ``source_replacement`` names the one successor only when the text
    belongs to exactly one key in each capture; any other pairing is
    ``ambiguous_replacement`` with every candidate. A key with no candidate is left
    out (missing, nothing more)."""
    gone = Counter(vanished.values())
    fresh: dict[str, list[str]] = {}
    for key, text in appeared.items():
        fresh.setdefault(text, []).append(key)
    out = {}
    for key, text in vanished.items():
        found = sorted(fresh.get(text, ()))
        if found:
            one = gone[text] == 1 and len(found) == 1 and not kept.get(text)
            out[key] = (SOURCE_REPLACEMENT if one else AMBIGUOUS_REPLACEMENT, found)
    return out


def _pattern_matches(pattern: str, relation: str) -> bool:
    return relation == pattern or (pattern.endswith("*") and relation.startswith(pattern[:-1]))


def _rules_digest(rules: Sequence[Mapping[str, Any]], relation: str) -> str:
    return _sha(_canonical([[rule["id"], list(rule["relations"]),
                             [list(clause) for clause in rule["when"]], rule["lineage"]]
                            for rule in rules
                            if any(_pattern_matches(p, relation) for p in rule["relations"])]))


# Every table and index ``_DDL`` creates: a file holding them all needs no DDL at open.
_DDL_OBJECTS = frozenset(re.findall(
    r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER)\s+IF\s+NOT\s+EXISTS\s+(\w+)",
    _DDL + _CHANGE_DDL))
# The change-counter triggers (``relation_change``): a file missing one has counters that
# do not cover every write, so its writer open gives it a new ``change_epoch``.
CHANGE_TRIGGERS = frozenset(re.findall(r"CREATE\s+TRIGGER\s+IF\s+NOT\s+EXISTS\s+(\w+)",
                                        _CHANGE_DDL))
CHANGE_EPOCH = "change_epoch"
# Retries (with backoff) of the open's own write when another process holds the lock.
OPEN_WRITE_RETRIES = 3
_ROW_CURRENT_COLUMNS = ("relation, key_json, run_index, run_length, row_json, row_digest, "
                        "capture_id, first_seen_capture, first_seen_at, last_seen_capture, "
                        "last_seen_at, absent_since_capture, missing_from_source_since, "
                        "missing_basis, lineage, lineage_basis, deleted_in_source, "
                        "deleted_basis")


def _sighting_sql(r: str) -> tuple[str, str]:
    """SQL for the last sighting (capture id, time) of a ``row_current`` row ``r``.

    A full traversal writes only the rows whose content or state changed; a row it saw
    unchanged keeps its stored ``last_seen_*`` and the relation records the traversal
    once (``relation_state.seen_through_*``, set only when every row then present was
    seen). A present row's sighting is the later of the two; an absent row's (and one
    whose absence a key scan lifted after that traversal: ``sighting_floor``) is its
    own. Every write that ends a row's presence or supersedes it stores this value first,
    so the stored value is exact wherever the rule no longer applies."""
    applies = (f"s.relation = {r}.relation AND {r}.absent_since_capture IS NULL AND "
               f"s.seen_through_capture > COALESCE({r}.sighting_floor, 0)")
    return (f"MAX({r}.last_seen_capture, COALESCE((SELECT s.seen_through_capture FROM "
            f"relation_state s WHERE {applies}), 0))",
            f"MAX({r}.last_seen_at, COALESCE((SELECT s.seen_through_at FROM "
            f"relation_state s WHERE {applies}), ''))")


_SEEN_CAPTURE, _SEEN_AT = _sighting_sql("row_current")


def _seen(record: sqlite3.Row, part: str) -> Any:
    """A cited record's last sighting (``part``: ``capture`` or ``at``): a current row
    read with ``_sighting_sql`` (``seen_*``), else a version's stored value."""
    if f"seen_{part}" in record.keys():
        return record[f"seen_{part}"]
    return record[f"last_seen_{part}"]


# The replacement label of a key's open absence, for a ``row_current`` row ``{r}``.
def _successors_remapped(row: dict[str, Any], mapping: Mapping[int, int]) -> dict[str, Any]:
    """A removal row copied from a persisted capture with its successors' content captures
    (``replaced_by``: [[key, capture id], ...]) under this file's capture ids. A successor
    whose capture the copy does not bring, or a list that does not read, drops the label
    (never a citation of another capture)."""
    raw = row.get("replaced_by")
    if raw is None:
        return row
    try:
        pairs = json.loads(raw)
        moved = [[key, mapping[capture]] for key, capture in pairs]
    except (TypeError, ValueError, KeyError):
        return dict(row, replaced_by=None, replaced_basis=None)
    return dict(row, replaced_by=_canonical(moved))


_REPLACED_SQL = ("(SELECT json_array(rm.replaced_basis, rm.replaced_by) FROM row_removal rm "
                 "WHERE rm.relation = {r}.relation AND rm.key_json = {r}.key_json AND "
                 "rm.basis = 'missing_from_source' AND rm.until IS NULL AND "
                 "rm.replaced_basis IS NOT NULL) AS replaced")


# ``mirror_meta`` name prefix of a completed full re-read's identifier cleanup.
_INDEX_CLEANUP = "index-cleanup:"


def _stage_bound(bound: tuple[str | None, str, str] | None) -> tuple[str, tuple[str, ...]]:
    """The ``stage_row`` key condition of one merge batch (``_merge_batches``): ``(after,
    "<", key)`` the keys after ``after`` (None: from the first) and before ``key``, ``"<="``
    through it; None: no bound."""
    if bound is None:
        return "", ()
    after, op, key = bound
    if op not in ("<", "<="):
        raise ValueError("merge bound operator")
    if after is None:
        return f" AND key_json {op} ?", (key,)
    return f" AND key_json > ? AND key_json {op} ?", (after, key)


def _later_watermark(a: tuple[datetime | None, Any], b: tuple[datetime | None, Any]
                     ) -> tuple[datetime | None, Any]:
    """The later of two (instant, raw value) watermarks; the first on a tie."""
    if b[0] is not None and (a[0] is None or b[0] > a[0]):
        return b
    return a


def _superseded_outcome(progress: Mapping[str, Any]) -> str:
    """What a superseded refresh leaves behind, for its ``capture_superseded`` message.

    Pages it committed before it was superseded are not discarded: they stay with
    the stored traversal, which the refresh that took it over continues (or has
    completed) from them. Only its uncommitted page is dropped.
    """
    committed = progress["pages"] - progress.get("pages_at_start", 0)
    if progress.get("capture_id") is None or committed <= 0:
        return "this refresh stored nothing"
    return (f"this refresh stores no further page; the {committed} page(s) it committed "
            "stay with the traversal the other refresh holds")
