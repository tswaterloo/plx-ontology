"""The generic tables' side of the canonical classes contract (migration 0041,
``operating.pg.business_views_access``): ``generic_records`` builds ``records`` and
``record_links`` inside Postgres, so their row patterns are given ids in the SAME
``<schema>.access_classes`` table the curated tables use, set-based (``assign``), and the
reader's door (``operating.pg.door.reader_scope``) holds them by the one rule every
reader already has: not ``blocked``, every audience key and every marking set held.

``FULL_KEY`` is an audience key no scope holds: ``plx.sql`` adds it to a full reader's keys
only (``people_excluded.full_reader``: the reader the people-exclusion set is built for).
A class carrying it is a full reader's alone: records of systems that are not business
systems (SharePoint lists, Planner, records read out of mail), which plx.table shows to no
staff reader, and links learned from such evidence."""
from __future__ import annotations

from typing import Any

FULL_KEY = "plx:full-reader"
UNMARKED = "[]"
CLASS_MAX = 2**31 - 1   # integer ids, as business_views.CLASS_MAX


def ensure(write: Any, schema: str) -> None:
    """The canonical class tables (``door.ensure``: migration 0041's DDL for a scratch
    schema; the served one must have them)."""
    from operating.pg import door
    write.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    door.ensure(write, schema)


def assign_cols(write: Any, schema: str, patterns: str) -> int:
    """Give every column pattern of the query ``patterns`` (rows of one ``marks jsonb``:
    column -> marking set) not in ``col_classes`` yet the next ids (no ``needs``), id 0
    being nothing marked; the caller's transaction, under the lock
    ``business_views_access.col_class_ids`` takes too. Returns how many were added."""
    write.execute(f"LOCK TABLE {schema}.col_classes IN SHARE ROW EXCLUSIVE MODE")
    write.execute(f"INSERT INTO {schema}.col_classes (id, marks, needs) SELECT 0, '{{}}', "
                  f"'{{}}' WHERE NOT EXISTS (SELECT 1 FROM {schema}.col_classes WHERE id = 0)")
    cur = write.execute(
        f"INSERT INTO {schema}.col_classes (id, marks, needs) SELECT (SELECT COALESCE(max(id), "
        f"0) FROM {schema}.col_classes) + row_number() OVER (ORDER BY p.marks::text), p.marks, "
        f"'{{}}'::jsonb FROM (SELECT DISTINCT q.marks FROM ({patterns}) q(marks)) p WHERE NOT "
        f"EXISTS (SELECT 1 FROM {schema}.col_classes c WHERE c.marks = p.marks AND "
        "c.needs = '{}'::jsonb)")
    top = write.execute(f"SELECT max(id) FROM {schema}.col_classes").fetchone()[0]
    if top is not None and top > CLASS_MAX:
        raise RuntimeError(f"{schema}.col_classes: more than {CLASS_MAX} patterns")
    return cur.rowcount


def assign(write: Any, schema: str, patterns: str) -> int:
    """Give every pattern of the query ``patterns`` (rows of ``keys text[], marks text[]``,
    each sorted and distinct) not in ``access_classes`` yet the next ids (never ``blocked``,
    no document marks: a generic record is checked by its own key and markings), under the
    table lock ``business_views_access.access_ids`` takes too; the caller's transaction.
    Returns how many were added."""
    write.execute(f"LOCK TABLE {schema}.access_classes IN SHARE ROW EXCLUSIVE MODE")
    cur = write.execute(
        f"INSERT INTO {schema}.access_classes (id, audience_keys, rec_marks, doc_marks, "
        f"blocked) SELECT (SELECT COALESCE(max(id), 0) FROM {schema}.access_classes) "
        "+ row_number() OVER (ORDER BY p.keys, p.marks), p.keys, p.marks, '{}'::text[], false "
        f"FROM (SELECT DISTINCT keys, marks FROM ({patterns}) q(keys, marks)) p "
        f"WHERE NOT EXISTS (SELECT 1 FROM {schema}.access_classes c WHERE "
        "c.audience_keys = p.keys AND c.rec_marks = p.marks AND c.doc_marks = '{}' "
        "AND NOT c.blocked)")
    top = write.execute(f"SELECT max(id) FROM {schema}.access_classes").fetchone()[0]
    if top is not None and top > CLASS_MAX:
        raise RuntimeError(f"{schema}.access_classes: more than {CLASS_MAX} patterns")
    return cur.rowcount
