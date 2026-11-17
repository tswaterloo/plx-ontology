"""Source mirror queries: the where/order checks, row matching and SQL prefilters a read
runs, and the scan helpers (split from ``integrations.source_mirror``, which re-exports
every name)."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping

from .source_mirror_values import _NUMERIC_TEXT, _norm_identifier, _number, _text


def _capture_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"capture_id": row["capture_id"], "relation": row["relation"],
            "mode": row["mode"], "reason": row["reason"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "since_sent": row["since_sent"], "catalog_revision": row["catalog_revision"],
            "page_count": row["page_count"], "rows_received": row["rows_received"],
            "state": (row["capture_state"] or "complete")
                     if "capture_state" in row.keys() else "complete",
            "health_before": json.loads(row["health_before_json"]),
            "health_after": json.loads(row["health_after_json"]),
            "atomic_upstream_snapshot": False, "deletions_proven": False}


def _matches(row: Mapping[str, Any], column: str, op: str, value: Any) -> bool:
    cell = row.get(column)
    if op == "is_null":
        return (cell is None) is value
    if cell is None:
        return False
    if op == "eq":
        return _text(cell) == _text(value)
    if op == "in":
        return _text(cell) in {_text(item) for item in value}
    if op == "prefix":
        text = _text(cell)
        return text is not None and text.startswith(value)
    left, right = _number(cell), _number(value)
    if left is None or right is None:
        if not isinstance(cell, str) or not isinstance(value, str):
            return False
        left, right = cell, value
    return left >= right if op == "gte" else left <= right


_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")


def _index_forms(op: str, value: Any) -> list[str] | None:
    """The identifier-index values (``_norm_identifier``) a cell equal to ``value`` (``eq``)
    or to one of its items (``in``) is indexed under, or None when some match might not
    be indexed: only a text or integer cell is (a float, boolean, object or array cell is
    not), so an asked value whose text a float's ``repr``, a boolean or a JSON value could
    also have, or one the index never holds (empty, control characters, past 256
    characters), reads the relation as before."""
    if op not in ("eq", "in"):
        return None
    items = value if op == "in" else [value]
    if not isinstance(items, (list, tuple)) or not items:
        return None
    forms: set[str] = set()
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            return None
        text = str(item)
        if text in ("true", "false") or text.startswith(("{", "[", '"')) \
                or text != text.strip() \
                or (_NUMERIC_TEXT.fullmatch(text) and not _INTEGER_TEXT.fullmatch(text)) \
                or text.casefold().lstrip("+-") in ("inf", "nan", "infinity"):
            return None
        norm = _norm_identifier(text)
        if norm is None:
            return None
        forms.add(norm)
    return sorted(forms)


def _sql_prefilter(column: str, op: str, value: Any) -> tuple[str, list[Any]]:
    """A SQLite condition over ``row_json`` that every exact match satisfies.

    It narrows the candidates without changing the result: ``_matches``
    decides every candidate. Text and integer cells are compared in SQL;
    other JSON types pass through to the exact check when they could match.
    """
    path = f'$."{column}"'
    kind = "json_type(row_json, ?)"
    if op == "is_null":
        if value:
            return f"({kind} IS NULL OR {kind} = 'null')", [path, path]
        return f"{kind} <> 'null'", [path]
    if op in ("gte", "lte"):
        return f"{kind} <> 'null'", [path]
    if op == "prefix":
        return (f"(({kind} = 'text' AND substr(json_extract(row_json, ?), 1, ?) = ?) OR "
                f"{kind} IN ('integer', 'real', 'true', 'false', 'object', 'array'))",
                [path, path, len(value), value, path])
    texts, integers, others = set(), set(), set()
    for item in (value if op == "in" else [value]):
        text = _text(item)
        texts.add(text)
        if isinstance(item, int) or (isinstance(item, str) and _INTEGER_TEXT.fullmatch(item)):
            number = int(item)
            if -2 ** 63 <= number < 2 ** 63:
                integers.add(number)
            else:  # SQLite cannot bind it; let every integer cell through
                others.add("integer")
        if isinstance(item, float) or (isinstance(item, str) and
                                       _NUMERIC_TEXT.fullmatch(item.strip())):
            others.add("real")
        if text in ("true", "false"):
            others.update(("true", "false"))
        if text.startswith(("{", "[")):
            others.update(("object", "array"))
    parts = [f"({kind} = 'text' AND json_extract(row_json, ?) IN "
             f"({','.join('?' for _ in texts)}))"]
    params: list[Any] = [path, path, *sorted(texts)]
    if integers:
        parts.append(f"({kind} = 'integer' AND json_extract(row_json, ?) IN "
                     f"({','.join('?' for _ in integers)}))")
        params += [path, path, *sorted(integers)]
    if others:
        parts.append(f"{kind} IN ({','.join('?' for _ in others)})")
        params += [path, *sorted(others)]
    return "(" + " OR ".join(parts) + ")", params


def mirror_options(config: Mapping[str, Any]) -> dict[str, Any]:
    """``SourceMirror`` keyword options from a loaded ingest configuration."""
    sync = config.get("sync", {})
    names = {"page_size": "page_size", "min_page_size": "min_page_size",
             "max_rows_per_relation": "max_rows_per_relation",
             "throttle_attempts": "throttle_attempts",
             "max_retry_after_seconds": "max_retry_after_seconds"}
    options: dict[str, Any] = {target: sync[name] for name, target in names.items()
                               if name in sync}
    if config.get("projections"):
        options["projections"] = config["projections"]
    options["lineage_rules"] = config["lineage_rules"]
    options["soft_delete_rules"] = config["soft_delete_rules"]
    return options


PROBE_PRINCIPAL = "service:mirror-full-coverage-probe"


def soft_delete_counts(coverage: Mapping[str, Any]) -> dict[str, Any]:
    """Counts only: rows the source soft-deleted, in total and per relation whose
    catalog carries a declared soft-delete column (relation names, never values)."""
    declared = {item["relation"]: item["rows_deleted_in_source"]
                for item in coverage["relations"] if item["soft_delete_columns"]}
    return {"rows_deleted_in_source_total": coverage["rows_deleted_in_source_total"],
            "soft_delete_relation_count": len(declared),
            "soft_deleted_rows": dict(sorted(declared.items()))}


_SCAN_STALENESS_MAX = 86_400.0 * 365


def scan_freshness(freshness: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a scan's reader must say about its rows (INT-013): whether the capture they
    come from is stale, as of when, and the refresh that failed (the rows are then the
    last capture held, never a live read)."""
    if not freshness:
        return None
    return {"stale": bool(freshness.get("stale")), "as_of": freshness.get("last_refresh_at"),
            "refresh_error": freshness.get("refresh_error"),
            "refresh_error_code": freshness.get("refresh_error_code"),
            "refresh_http_status": freshness.get("refresh_http_status"),
            "latest_capture_id": freshness.get("latest_capture_id")}


def scan_label(scan: Mapping[str, Any]) -> dict[str, Any]:
    """The freshness fields a consumer of a scan carries beside its counts: ``stale``
    (also true when the refresh failed) and ``as_of``, plus ``refresh_error`` when one
    failed; a scan with no freshness (not read) carries none."""
    found = scan.get("freshness")
    if not found:
        return {}
    out = {"stale": bool(found["stale"] or found["refresh_error"]), "as_of": found["as_of"]}
    if found["refresh_error"]:
        out["refresh_error"] = found["refresh_error_code"] or found["refresh_error"]
    return out
