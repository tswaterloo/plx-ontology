"""Data-shape profile for keyless development: shapes, rates and cardinalities, never values.

The live-check runner runs this through the ``verify/live-checks/profile.*.json``
specs (``python3 verify/live_verify.py``); each spec's stdout is re-checked by
``sanitize_profile`` and published in the run result (``checks[*].profile``). That profile is read to write bindings and
normalizers against real shapes without ever seeing business data::

    python3 -m verify.data_profile portal --relations 'public.app_*' --join A.c=B.d
    python3 -m verify.data_profile bc --line-documents 5 --join purchaseOrders.vendorNumber=vendors.number
    python3 -m verify.data_profile mail --mailbox <upn> --folder inbox --days 90 --label ops-inbox
    python3 -m verify.data_profile drive --drive-id-env SHAREPOINT_DRIVE_ID --path / --label customers

Reads go only through the existing GET-only clients:
``integrations.workbench_read_api.WorkbenchReadApiClient`` (Portal mirror relations,
bounded keyset pages via ``read_page``), ``integrations.bc_read.BusinessCentralReader`` (every company
by index, the case entity sets and lines of the first documents) and
``integrations.graph_read`` (``GraphMailReader`` message metadata,
``SharePointReader`` drive-item metadata; no bodies, no content). The profile is of
the reader output code is written against (BC numbers are decimal text there).

What a profile may contain (``plx-data-profile/v1``): target names and the *declared*
column names (the Portal catalog, the BC ``$select`` allowlist, the reader record keys;
other row keys are only counted), row counts, completeness flags, JSON type counts,
null and absence rates, distinct-count buckets, string classes from a fixed list,
value-length buckets, digit/letter shape masks (``AAA-9999``: every letter becomes
``A``/``a``, every digit ``9``) only for single-token code-like values (no
whitespace, at least one digit, never an ``@``) and only when at least ``k`` (default
5) rows share the mask, declared key uniqueness and declared join resolution rates. Nested JSON key names
are published only when reviewed (``verify/profile-nested-keys.json``) or declared by
the source (``source_mirror.DEFAULT_IDENTIFIERS`` paths); every other nested key is
data until proven otherwise and appears only as a counted shape mask. Never a value,
name, email, amount, subject, file name, id or company name: companies are numbered,
targets are labelled by the spec. ``sanitize_profile`` rebuilds a profile from this
closed shape and drops every string that is not a schema-shaped name, a fixed enum, a
permitted nested key or a mask over the mask alphabet. A mail read Graph refuses for a
missing grant (``integrations.graph_read.mail_block``) is no error but a top-level
``blocked_upstream`` part (part name, source, status, fixed codes and the permission's
name only), which the live check reads as blocked.

Known limitation: a mask keeps the length, letter case and punctuation layout of a
code (``AA-99999A``). It never keeps a letter or digit, and it needs >= k rows, but a
reader who already knows a short list of codes can tell which layouts occur.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import fnmatch
import json
import os
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "plx-data-profile/v1"
K_MIN = 5
MAX_MASK_CHARS = 32
MAX_MASK_SPACES = 2
MAX_MASKS = 12
MAX_NESTED_KEYS = 40
MAX_COLUMNS = 400
MAX_TARGETS = 300
MAX_JOINS = 100
MAX_PROFILE_CHARS = 600_000

SOURCE_KINDS = ("portal", "bc", "mail", "drive")
EVIDENCE_LABELS = ("live-read", "injected-transport")
TYPES = ("null", "boolean", "integer", "number", "string", "object", "array")
STRING_CLASSES = ("empty", "blank", "integer_text", "decimal_text", "grouped_number_text",
                  "iso_date", "iso_datetime", "uuid", "email_shape", "url",
                  "json_object_text", "json_array_text", "code_like", "word", "token_like",
                  "text")
COUNT_BUCKETS = ("0", "1", "2-4", "5-9", "10-99", "100-999", "1000-9999", "10000+")
LENGTH_BUCKETS = ("0", "1-3", "4-8", "9-16", "17-32", "33-64", "65-256", "257+")
JOIN_BASIS = ("exact", "lower_bound")
JOIN_STATUS = ("ok", "target_absent", "column_absent")
SKIP_REASONS = ("unpaged", "unavailable", "not_in_catalog")
# A ``blocked_upstream`` part: ``capture_mail.graph_block``'s shape, fixed values only.
BLOCKED_SOURCES = ("microsoft-graph",)
BLOCKED_ERROR_CODES = ("missing_permission",)
BLOCKED_CAUSES = ("host_misconfiguration",)
PERMISSION = re.compile(r"[A-Z][A-Za-z]{1,31}(?:\.[A-Z][A-Za-z]{1,31}){1,3}")

# Names are schema identifiers (relations, entity sets, columns, labels); masks use
# only A/a/9, space and ASCII punctuation, so they cannot spell a value.
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{0,127}")
NESTED_KEY = re.compile(r"[a-z][A-Za-z0-9_]{0,47}")  # camelCase/snake keys, not proper names
LABEL = re.compile(r"[a-z0-9][a-z0-9_.\-]{0,47}")
ERROR_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_:]{0,63}")
_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ ")
MASK = re.compile("[Aa9" + re.escape("".join(sorted(_PUNCT))) + "]{1,%d}" % MAX_MASK_CHARS)

_INTEGER_TEXT = re.compile(r"[+-]?\d+")
_DECIMAL_TEXT = re.compile(r"[+-]?\d*\.\d+")
_GROUPED = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                           r"(?:Z|[+-]\d{2}:?\d{2})?")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


# ------------------------------------------------------------------ primitives

def count_bucket(n: int) -> str:
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    for upper, label in ((4, "2-4"), (9, "5-9"), (99, "10-99"), (999, "100-999"),
                         (9999, "1000-9999")):
        if n <= upper:
            return label
    return "10000+"


def length_bucket(n: int) -> str:
    if n == 0:
        return "0"
    for upper, label in ((3, "1-3"), (8, "4-8"), (16, "9-16"), (32, "17-32"), (64, "33-64"),
                         (256, "65-256")):
        if n <= upper:
            return label
    return "257+"


def shape_mask(value: str) -> str | None:
    """``AAA-9999``-style shape of a short value, or None (too long / too wordy).

    Letters become ``A`` (upper) or ``a`` (lower, and caseless scripts), digits ``9``,
    ASCII punctuation and space stay, anything else becomes ``?``. No literal letter
    or digit of the value survives."""
    if not value or len(value) > MAX_MASK_CHARS or value.count(" ") > MAX_MASK_SPACES:
        return None
    out = []
    for ch in value:
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        elif ch in _PUNCT:
            out.append(ch)
        elif ch.isspace():
            return None  # tabs/newlines: free text, not a code
        else:
            out.append("?")
    return "".join(out)


def string_class(value: str) -> str:
    if value == "":
        return "empty"
    stripped = value.strip()
    if not stripped:
        return "blank"
    if _INTEGER_TEXT.fullmatch(stripped):
        return "integer_text"
    if _DECIMAL_TEXT.fullmatch(stripped):
        return "decimal_text"
    if _GROUPED.fullmatch(stripped):
        return "grouped_number_text"
    if _ISO_DATE.fullmatch(stripped):
        return "iso_date"
    if _ISO_DATETIME.fullmatch(stripped):
        return "iso_datetime"
    if _UUID.fullmatch(stripped):
        return "uuid"
    if _EMAIL.fullmatch(stripped):
        return "email_shape"
    if _URL.fullmatch(stripped):
        return "url"
    if stripped[0] in "{[" and _json_container(stripped) is not None:
        return "json_object_text" if stripped[0] == "{" else "json_array_text"
    if not any(c.isspace() for c in stripped):
        if len(stripped) > MAX_MASK_CHARS:
            return "token_like"
        if any(c.isdigit() for c in stripped) or any(c in _PUNCT for c in stripped):
            return "code_like"
        return "word"
    return "text"


def _json_container(text: str) -> Any:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, (dict, list)) else None


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return "string"  # Decimal and other scalars render as text in reader output


def canonical_text(value: Any) -> str | None:
    """Text used to compare key/join values (never published)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, default=str)


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


# ------------------------------------------------------------------ declared names

# The reader record shapes (integrations.graph_read GraphMailReader._record and
# SharePointReader._record, minus source_acl; ``depth`` is added by drive_profile).
MAIL_RECORD_KEYS = ("source", "kind", "mailbox", "mailbox_upn", "id", "id_type", "change_key",
                    "parent_folder_id", "last_modified", "received_at", "sent_at", "subject",
                    "from", "to", "cc", "conversation_id", "internet_message_id",
                    "has_attachments", "original_url", "body_preview", "content_sha256",
                    "citation")
DRIVE_RECORD_KEYS = ("source", "kind", "drive_id", "id", "etag", "ctag", "path", "name", "size",
                     "mime_type", "source_hashes", "created_at", "last_modified",
                     "last_modified_by", "original_url", "content_sha256_bytes",
                     "content_sha256", "citation", "depth")
NESTED_KEYS_FILE = "verify/profile-nested-keys.json"
_ALLOWLIST: list[tuple[str, str, frozenset[str]]] | None = None


def _load_allowlist() -> list[tuple[str, str, frozenset[str]]]:
    """(target glob, column, key names) from the reviewed file plus the nested identifier
    paths the source mirror declares (``source_mirror.DEFAULT_IDENTIFIERS``)."""
    global _ALLOWLIST
    if _ALLOWLIST is not None:
        return _ALLOWLIST
    entries: list[tuple[str, str, frozenset[str]]] = []
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        NESTED_KEYS_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    for target, columns in (data.get("keys") or {}).items():
        for column, keys in (columns or {}).items():
            entries.append((target, column, frozenset(k for k in keys
                                                      if isinstance(k, str)
                                                      and NESTED_KEY.fullmatch(k))))
    try:
        from integrations.source_mirror import DEFAULT_IDENTIFIERS
    except ImportError:
        DEFAULT_IDENTIFIERS = {}
    for relation, identifiers in DEFAULT_IDENTIFIERS.items():
        for ident in identifiers:
            column, dot, key = ident[0].partition(".")
            if dot and NESTED_KEY.fullmatch(key):
                entries.append((relation, column, frozenset({key})))
    _ALLOWLIST = entries
    return entries


def allowed_nested_keys(target: str, column: str) -> frozenset[str]:
    """JSON key names of ``target.column`` that a profile may name (reviewed or
    source-declared); every other nested key is published only as a counted shape."""
    found: set[str] = set()
    for pattern, col, keys in _load_allowlist():
        if col == column and fnmatch.fnmatchcase(target, pattern):
            found |= keys
    return frozenset(found)


def _maskable(value: str, cls: str) -> bool:
    """Masks only for single-token codes: string class ``code_like`` (no whitespace,
    <= 32 characters), at least one digit, and no ``@``. Punctuated names ("O'Brien",
    "Smith-Jones", "Alton, Vince"), words, emails and name+email strings would only
    publish word lengths, so they get classes and length buckets instead."""
    return cls == "code_like" and "@" not in value and any(c.isdigit() for c in value)


def _ranked(counts: Counter) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


# ------------------------------------------------------------------ columns

class _Column:
    """Accumulates one column's statistics; ``values`` stay in memory, never output."""

    def __init__(self, name: str, *, nested_allow: frozenset[str] = frozenset(),
                 nested: bool = True) -> None:
        self.name = name
        self.nested = nested
        self.nested_allow = nested_allow
        self.absent = 0
        self.types: Counter = Counter()
        self.distinct: set[str] = set()
        self.classes: Counter = Counter()
        self.lengths: Counter = Counter()
        self.masks: Counter = Counter()
        self.unmasked = 0
        self.padded = 0
        self.multiline = 0
        self.negative = self.zero = self.fractional = 0
        self.true_rows = 0
        self.array_lengths: Counter = Counter()
        self.key_rows: Counter = Counter()
        self.key_values: dict[str, list[Any]] = {}

    def add_absent(self) -> None:
        self.absent += 1

    def add(self, value: Any) -> None:
        kind = json_type(value)
        if kind == "string" and not isinstance(value, str):
            value = str(value)
        self.types[kind] += 1
        if kind == "null":
            return
        text = canonical_text(value)
        if text is not None:
            self.distinct.add(text)
        if kind == "string":
            self._add_string(value)
        elif kind in ("integer", "number"):
            if value < 0:
                self.negative += 1
            elif value == 0:
                self.zero += 1
            if kind == "number" and not float(value).is_integer():
                self.fractional += 1
        elif kind == "boolean":
            self.true_rows += int(value)
        elif kind == "array":
            self.array_lengths[count_bucket(len(value))] += 1
        elif kind == "object":
            self._add_object(value)

    def _add_string(self, value: str) -> None:
        cls = string_class(value)
        self.classes[cls] += 1
        self.lengths[length_bucket(len(value))] += 1
        if value != value.strip():
            self.padded += 1
        if "\n" in value or "\r" in value:
            self.multiline += 1
        mask = shape_mask(value) if _maskable(value, cls) else None
        if mask is None:
            self.unmasked += 1
        else:
            self.masks[mask] += 1
        if cls == "json_object_text":
            container = _json_container(value.strip())
            if isinstance(container, dict):
                self._add_object(container)

    def _add_object(self, value: Mapping[str, Any]) -> None:
        if not self.nested:
            return
        for key, inner in value.items():
            if not isinstance(key, str):
                continue
            self.key_rows[key] += 1
            if key in self.nested_allow:  # values only of reviewed/declared keys
                self.key_values.setdefault(key, []).append(inner)

    def result(self, rows: int, k: int) -> dict[str, Any]:
        present = rows - self.absent
        nulls = self.types.get("null", 0)
        non_null = present - nulls
        out: dict[str, Any] = {
            "name": self.name, "absent_pct": _pct(self.absent, rows),
            "null_pct": _pct(nulls, present), "types": dict(sorted(self.types.items())),
            "distinct_bucket": count_bucket(len(self.distinct)),
            "all_distinct": bool(non_null) and len(self.distinct) == non_null,
            "single_value": len(self.distinct) == 1}
        strings = self.types.get("string", 0)
        if strings:
            out["string_classes"] = dict(sorted(self.classes.items()))
        if strings >= k:
            out["length_buckets"] = {b: self.lengths[b] for b in LENGTH_BUCKETS if self.lengths[b]}
            out["padded_rows"] = self.padded
            out["multiline_rows"] = self.multiline
            shown = [(m, n) for m, n in _ranked(self.masks) if n >= k][:MAX_MASKS]
            out["masks"] = [{"mask": m, "rows": n} for m, n in shown]
            out["masks_below_k_rows"] = sum(self.masks.values()) - sum(n for _, n in shown)
            out["unmasked_rows"] = self.unmasked
        numbers = self.types.get("integer", 0) + self.types.get("number", 0)
        if numbers >= k:
            out["numeric"] = {"negative_rows": self.negative, "zero_rows": self.zero,
                              "fractional_rows": self.fractional}
        if self.types.get("boolean", 0) >= k:
            out["true_rows"] = self.true_rows
        if self.types.get("array", 0) >= k:
            out["array_lengths"] = {b: self.array_lengths[b] for b in COUNT_BUCKETS
                                    if self.array_lengths[b]}
        if self.key_rows:
            named = sorted(key for key in self.key_rows if key in self.nested_allow)
            out["nested_keys"] = [{"name": key, "rows": self.key_rows[key]}
                                  for key in named][:MAX_NESTED_KEYS]
            # Any other key may be data (a login, a customer slug, a code): never named,
            # only its shape, with the same code-like and >= k rules as values.
            shapes: Counter = Counter()
            for key, n in self.key_rows.items():
                if key not in self.nested_allow:
                    mask = shape_mask(key) if _maskable(key, string_class(key)) else None
                    shapes[mask or ""] += n
            out["nested_other_keys"] = len(self.key_rows) - len(named)
            out["nested_key_masks"] = [{"mask": m, "rows": n} for m, n in _ranked(shapes)
                                       if m and n >= k][:MAX_MASKS]
            nested = []
            for key in named[:MAX_NESTED_KEYS]:
                column = _Column(f"{self.name}.{key}", nested=False)
                values = self.key_values.get(key, [])
                for value in values:
                    column.add(value)
                column.absent = rows - len(values)
                nested.append(column.result(rows, k))
            if nested:
                out["nested"] = nested
        return out


def profile_rows(name: str, rows: Sequence[Mapping[str, Any]], *, columns: Sequence[str],
                 key: Sequence[str] = (), complete: bool, k: int = K_MIN,
                 extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Profile one target (relation, entity set, mailbox folder, drive tree).

    ``columns`` is the declared shape (the Portal catalog, the BC ``$select`` allowlist,
    the reader record keys); only those are named. Row keys outside it are counted
    (``undeclared_columns``, ``undeclared_column_rows``), never named."""
    order = [c for c in dict.fromkeys(columns) if isinstance(c, str) and NAME.fullmatch(c)]
    order = order[:MAX_COLUMNS]
    declared = set(order)
    stats = {column: _Column(column, nested_allow=allowed_nested_keys(name, column))
             for column in order}
    undeclared: set[str] = set()
    undeclared_rows = 0
    for row in rows:
        extra_keys = [c for c in row if c not in declared]
        if extra_keys:
            undeclared.update(map(str, extra_keys))
            undeclared_rows += 1
        for column, acc in stats.items():
            if column in row:
                acc.add(row[column])
            else:
                acc.add_absent()
    target: dict[str, Any] = {"name": name, **(extra or {}), "row_count": len(rows),
                              "complete": bool(complete),
                              "undeclared_columns": len(undeclared),
                              "undeclared_column_rows": undeclared_rows,
                              "columns": [stats[c].result(len(rows), k) for c in order]}
    if key:
        target["key"] = list(key)
        keyed = [tuple(canonical_text(row.get(c)) for c in key) for row in rows
                 if all(row.get(c) is not None for c in key)]
        counts = Counter(keyed)
        duplicates = sum(n for n in counts.values() if n > 1)
        target["key_null_rows"] = len(rows) - len(keyed)
        target["key_duplicate_rows"] = duplicates
        target["key_unique_pct"] = _pct(len(keyed) - duplicates, len(keyed))
    return target


# ------------------------------------------------------------------ joins

def parse_join(text: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """``FROM_TARGET.COLUMN=TO_TARGET.COLUMN`` (targets may contain dots)."""
    left, sep, right = text.partition("=")
    parts = []
    for side in (left, right):
        target, dot, column = side.strip().rpartition(".")
        if not sep or not dot or not NAME.fullmatch(target) or not NAME.fullmatch(column):
            raise ValueError(f"join must be TARGET.COLUMN=TARGET.COLUMN: {text!r}")
        parts.append((target, column))
    return parts[0], parts[1]


def join_stats(source: tuple[str, str], target: tuple[str, str],
               rows: Mapping[str, tuple[Sequence[Mapping[str, Any]], bool]],
               **extra: Any) -> dict[str, Any]:
    """How many source rows' value of the declared column finds a target row.

    ``rows`` maps target name -> (rows, complete). ``basis`` is ``exact`` only when the
    target was read completely; otherwise the rate is a lower bound."""
    result: dict[str, Any] = {"from": ".".join(source), "to": ".".join(target), **extra}
    if source[0] not in rows or target[0] not in rows:
        result["status"] = "target_absent"
        return result
    from_rows, from_complete = rows[source[0]]
    to_rows, to_complete = rows[target[0]]
    if (from_rows and not any(source[1] in r for r in from_rows)) or \
            (to_rows and not any(target[1] in r for r in to_rows)):
        result["status"] = "column_absent"
        return result
    index = Counter(canonical_text(r.get(target[1])) for r in to_rows
                    if r.get(target[1]) is not None)
    loose = Counter(t.strip().casefold() for t in index.elements())
    values = [canonical_text(r.get(source[1])) for r in from_rows
              if r.get(source[1]) is not None]
    matches = Counter("none" if not index[v] else "one" if index[v] == 1 else "many"
                      for v in values)
    loose_hits = sum(1 for v in values if loose[v.strip().casefold()])
    result.update({"status": "ok", "from_rows": len(from_rows), "from_complete": from_complete,
                   "from_rows_with_value": len(values),
                   "resolved_pct": _pct(len(values) - matches["none"], len(values)),
                   "normalized_resolved_pct": _pct(loose_hits, len(values)),
                   "matches": {m: matches[m] for m in ("none", "one", "many")},
                   "basis": "exact" if to_complete else "lower_bound"})
    return result


# ------------------------------------------------------------------ sanitizer

class _Dropped:
    def __init__(self) -> None:
        self.count = 0


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _enum_counts(value: Any, allowed: Sequence[str], dropped: _Dropped) -> dict[str, int]:
    out = {}
    if isinstance(value, Mapping):
        for key, n in value.items():
            if key in allowed and isinstance(n, int) and not isinstance(n, bool):
                out[key] = n
            else:
                dropped.count += 1
    return out


def _code_mask(mask: str) -> bool:
    """The producer's codes-only rule, re-checked: one token, a digit, no ``@``."""
    return " " not in mask and "@" not in mask and "9" in mask


def _clean_masks(value: Any, dropped: _Dropped) -> list[dict[str, Any]]:
    masks = []
    for item in value if isinstance(value, list) else []:
        if (isinstance(item, Mapping) and isinstance(item.get("mask"), str)
                and MASK.fullmatch(item["mask"]) and _code_mask(item["mask"])
                and isinstance(item.get("rows"), int) and item["rows"] >= K_MIN):
            masks.append({"mask": item["mask"], "rows": item["rows"]})
        else:
            dropped.count += 1
    return masks[:MAX_MASKS]


def _clean_column(column: Any, dropped: _Dropped, target: str,
                  depth: int = 0) -> dict[str, Any] | None:
    if not isinstance(column, Mapping) or not isinstance(column.get("name"), str) \
            or not NAME.fullmatch(column["name"]):
        dropped.count += 1
        return None
    out: dict[str, Any] = {"name": column["name"]}
    for field in ("absent_pct", "null_pct"):
        if column.get(field) is None or _num(column.get(field)):
            out[field] = column.get(field)
    out["types"] = _enum_counts(column.get("types"), TYPES, dropped)
    if column.get("distinct_bucket") in COUNT_BUCKETS:
        out["distinct_bucket"] = column["distinct_bucket"]
    for field in ("all_distinct", "single_value"):
        if isinstance(column.get(field), bool):
            out[field] = column[field]
    if "string_classes" in column:
        out["string_classes"] = _enum_counts(column["string_classes"], STRING_CLASSES, dropped)
    if "length_buckets" in column:
        out["length_buckets"] = _enum_counts(column["length_buckets"], LENGTH_BUCKETS, dropped)
    if "array_lengths" in column:
        out["array_lengths"] = _enum_counts(column["array_lengths"], COUNT_BUCKETS, dropped)
    for field in ("padded_rows", "multiline_rows", "masks_below_k_rows", "unmasked_rows",
                  "true_rows", "nested_other_keys"):
        if isinstance(column.get(field), int) and not isinstance(column[field], bool):
            out[field] = column[field]
    if isinstance(column.get("numeric"), Mapping):
        out["numeric"] = {f: column["numeric"][f] for f in
                          ("negative_rows", "zero_rows", "fractional_rows")
                          if isinstance(column["numeric"].get(f), int)}
    if "masks" in column:
        out["masks"] = _clean_masks(column["masks"], dropped)
    if "nested_key_masks" in column and depth == 0:
        out["nested_key_masks"] = _clean_masks(column["nested_key_masks"], dropped)
    if "nested_keys" in column and depth == 0:
        # Only reviewed/source-declared key names of this target column survive.
        permitted = allowed_nested_keys(target, column["name"])
        keys = []
        for item in column["nested_keys"] if isinstance(column["nested_keys"], list) else []:
            if (isinstance(item, Mapping) and isinstance(item.get("name"), str)
                    and item["name"] in permitted and isinstance(item.get("rows"), int)
                    and not isinstance(item["rows"], bool)):
                keys.append({"name": item["name"], "rows": item["rows"]})
            else:
                dropped.count += 1
        out["nested_keys"] = keys[:MAX_NESTED_KEYS]
    if "nested" in column and depth == 0 and isinstance(column["nested"], list):
        allowed = {f"{column['name']}.{k['name']}" for k in out.get("nested_keys", [])}
        nested = []
        for inner in column["nested"][:MAX_NESTED_KEYS]:
            clean = _clean_column(inner, dropped, target, depth + 1)
            if clean is not None and clean["name"] in allowed:
                nested.append(clean)
            elif clean is not None:
                dropped.count += 1
        out["nested"] = nested
    return out


def _clean_names(value: Any, dropped: _Dropped) -> list[str]:
    out = []
    for name in value if isinstance(value, list) else []:
        if isinstance(name, str) and NAME.fullmatch(name):
            out.append(name)
        else:
            dropped.count += 1
    return out


def _clean_target(target: Any, dropped: _Dropped) -> dict[str, Any] | None:
    if not isinstance(target, Mapping) or not isinstance(target.get("name"), str) \
            or not NAME.fullmatch(target["name"]):
        dropped.count += 1
        return None
    out: dict[str, Any] = {"name": target["name"]}
    for field in ("company_index", "row_count", "key_null_rows", "key_duplicate_rows",
                  "documents_read", "undeclared_columns", "undeclared_column_rows"):
        if isinstance(target.get(field), int) and not isinstance(target[field], bool):
            out[field] = target[field]
    if isinstance(target.get("complete"), bool):
        out["complete"] = target["complete"]
    if target.get("key_unique_pct") is None or _num(target.get("key_unique_pct")):
        if "key_unique_pct" in target:
            out["key_unique_pct"] = target["key_unique_pct"]
    if "key" in target:
        out["key"] = _clean_names(target["key"], dropped)
    columns = [c for c in (_clean_column(c, dropped, target["name"]) for c in
                           (target.get("columns") or [])[:MAX_COLUMNS]) if c is not None]
    out["columns"] = columns
    return out


def _clean_join(join: Any, dropped: _Dropped) -> dict[str, Any] | None:
    if not isinstance(join, Mapping) or join.get("status") not in JOIN_STATUS:
        dropped.count += 1
        return None
    out: dict[str, Any] = {"status": join["status"]}
    for field in ("from", "to"):
        if not isinstance(join.get(field), str) or not NAME.fullmatch(join[field]):
            dropped.count += 1
            return None
        out[field] = join[field]
    for field in ("company_index", "from_rows", "from_rows_with_value"):
        if isinstance(join.get(field), int) and not isinstance(join[field], bool):
            out[field] = join[field]
    for field in ("resolved_pct", "normalized_resolved_pct"):
        if field in join and (join[field] is None or _num(join[field])):
            out[field] = join[field]
    if isinstance(join.get("from_complete"), bool):
        out["from_complete"] = join["from_complete"]
    if join.get("basis") in JOIN_BASIS:
        out["basis"] = join["basis"]
    if "matches" in join:
        out["matches"] = _enum_counts(join["matches"], ("none", "one", "many"), dropped)
    return out


def _clean_blocked(part: Any, dropped: _Dropped) -> dict[str, Any] | None:
    if (isinstance(part, Mapping) and isinstance(part.get("part"), str)
            and NAME.fullmatch(part["part"]) and part.get("source") in BLOCKED_SOURCES
            and http_status_ok(part.get("http_status"))
            and part.get("error_code") in BLOCKED_ERROR_CODES
            and part.get("cause") in BLOCKED_CAUSES
            and (isinstance(part.get("permission"), str)
                 and PERMISSION.fullmatch(part["permission"]))):
        return {key: part[key] for key in ("part", "source", "http_status", "error_code",
                                           "cause", "permission")}
    if _people_block(part):
        return {key: part[key] for key in ("part", "source", "http_status", "error_code",
                                           "cause", "later_ask")}
    dropped.count += 1
    return None


def _people_block(part: Any) -> bool:
    """Whether ``part`` is ``PeopleExclusion.block_part``'s shape: the people guard
    refused the read before any request (no HTTP status), with one of its codes and the
    cause it gives that code (later_ask ``excluded-people-oids``)."""
    from integrations import people_exclusion

    code = part.get("error_code") if isinstance(part, Mapping) else None
    return (isinstance(part, Mapping) and isinstance(part.get("part"), str)
            and NAME.fullmatch(part["part"]) is not None
            and part.get("source") == "people-exclusion" and part.get("http_status") is None
            and code in people_exclusion.CODES
            and part.get("cause") == ("upstream_outage" if code == "excluded_people_unresolved"
                                      else "later_ask")
            and part.get("later_ask") == people_exclusion.LATER_ASK)


def sanitize_profile(document: Any, *, max_chars: int = MAX_PROFILE_CHARS) -> dict[str, Any]:
    """Rebuild a profile from the closed ``plx-data-profile/v1`` shape.

    Unknown keys are dropped; every string must be a schema-shaped name, a fixed enum
    or a mask over the mask alphabet (``sanitizer_dropped`` counts what was removed).
    Targets beyond ``max_chars`` of JSON are cut and counted (``targets_truncated``)."""
    dropped = _Dropped()
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "valid": False, "sanitizer_dropped": 1}
    out: dict[str, Any] = {"schema": SCHEMA, "valid": True}
    if document.get("source_kind") in SOURCE_KINDS:
        out["source_kind"] = document["source_kind"]
    if document.get("evidence_label") in EVIDENCE_LABELS:
        out["evidence_label"] = document["evidence_label"]
    for field in ("k", "target_count", "errors_count", "requests_sent", "companies_count"):
        if isinstance(document.get(field), int) and not isinstance(document[field], bool):
            out[field] = document[field]
    if isinstance(out.get("k"), int) and out["k"] < K_MIN:
        out["k"] = K_MIN  # masks below K_MIN are dropped by _clean_column anyway
    skipped = []
    for item in (document.get("skipped") or [])[:MAX_TARGETS]:
        if (isinstance(item, Mapping) and isinstance(item.get("name"), str)
                and NAME.fullmatch(item["name"]) and item.get("reason") in SKIP_REASONS):
            skipped.append({"name": item["name"], "reason": item["reason"]})
        else:
            dropped.count += 1
    errors = []
    for item in (document.get("errors") or [])[:MAX_TARGETS]:
        if (isinstance(item, Mapping) and isinstance(item.get("name"), str)
                and NAME.fullmatch(item["name"]) and isinstance(item.get("error"), str)
                and ERROR_CODE.fullmatch(item["error"])):
            clean = {"name": item["name"], "error": item["error"]}
            if http_status_ok(item.get("http_status")):
                clean["http_status"] = item["http_status"]
            if http_error_code_ok(item.get("error_code")):
                clean["error_code"] = item["error_code"]
            if isinstance(item.get("company_index"), int):
                clean["company_index"] = item["company_index"]
            errors.append(clean)
        else:
            dropped.count += 1
    joins = [j for j in (_clean_join(j, dropped) for j in
                         (document.get("joins") or [])[:MAX_JOINS]) if j is not None]
    raw_blocked = document.get("blocked_upstream") or []
    if not isinstance(raw_blocked, list):  # not the shape: dropped and counted
        raw_blocked = [None]
    blocked = [b for b in (_clean_blocked(b, dropped) for b in raw_blocked[:MAX_TARGETS])
               if b is not None]
    if blocked:
        out["blocked_upstream"] = blocked
    out.update(skipped=skipped, errors=errors, joins=joins, targets=[])
    budget = max_chars - len(json.dumps(out))
    truncated = 0
    for target in (document.get("targets") or [])[:MAX_TARGETS]:
        clean = _clean_target(target, dropped)
        if clean is None:
            continue
        size = len(json.dumps(clean)) + 2
        if size > budget:
            truncated += 1
            continue
        budget -= size
        out["targets"].append(clean)
    # A profile sanitized twice (CLI, then live_verify) keeps the first pass's counts.
    prior = {f: document.get(f) for f in ("targets_truncated", "sanitizer_dropped")}
    prior = {f: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0
             for f, v in prior.items()}
    out["targets_truncated"] = truncated + prior["targets_truncated"]
    out["sanitizer_dropped"] = dropped.count + prior["sanitizer_dropped"]
    return out


# ------------------------------------------------------------------ sources

def _envelope(kind: str, label: str, targets: list, joins: list, errors: list,
              skipped: list, requests_sent: int | None, k: int, **extra: Any) -> dict[str, Any]:
    doc = {"schema": SCHEMA, "source_kind": kind, "evidence_label": label, "k": k,
           "target_count": len(targets), "errors_count": len(errors), **extra,
           "targets": targets, "joins": joins, "errors": errors, "skipped": skipped}
    if requests_sent is not None:
        doc["requests_sent"] = requests_sent
    return doc


def _error_code(exc: BaseException) -> str:
    return type(exc).__name__


_HTTP_ERROR_CODE = re.compile(r"[a-z][a-z_-]{0,63}")  # no digits: no PO or order numbers


def http_status_ok(value: Any) -> bool:
    return type(value) is int and 100 <= value <= 599


def http_error_code_ok(value: Any) -> bool:
    return isinstance(value, str) and _HTTP_ERROR_CODE.fullmatch(value) is not None


def http_diagnostics(exc: BaseException) -> dict[str, Any]:
    """``http_status`` and ``error_code`` of a SourceHttpError in the cause chain.

    Follows both ``__cause__`` and ``__context__`` of ``exc`` and of each link, at most
    four links deep and once per object. Only a status in 100..599 and a short
    lowercase code pass: never a message, URL or body."""
    from integrations.http import SourceHttpError

    seen: set[int] = set()
    level: list[BaseException] = [exc]
    for _ in range(4):
        following: list[BaseException] = []
        for link in level:
            if id(link) in seen:
                continue
            seen.add(id(link))
            if isinstance(link, SourceHttpError):
                out: dict[str, Any] = {}
                if http_status_ok(link.status):
                    out["http_status"] = link.status
                if http_error_code_ok(link.error_code):
                    out["error_code"] = link.error_code
                return out
            following += [e for e in (link.__cause__, link.__context__) if e is not None]
        level = following
    return {}


def _matches(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _sample_relation(client: Any, name: str, entry: Mapping[str, Any], *, page_size: int,
                     max_pages: int) -> tuple[list[dict[str, Any]], bool]:
    """Up to ``max_pages`` keyset pages; (rows, complete). A repeated cursor fails."""
    from integrations.http import SourceUnavailable

    rows: list[dict[str, Any]] = []
    cursor, seen = None, set()
    for _ in range(max_pages):
        page = client.read_page(name, entry, columns=list(entry["columns"]), cursor=cursor,
                                page_size=page_size)
        rows.extend(page["rows"])
        cursor = page["next_cursor"]
        if cursor is None:
            return rows, True
        if cursor in seen:
            raise SourceUnavailable("workbench API cursor did not progress")
        seen.add(cursor)
    return rows, False


def portal_profile(client: Any, *, include: Sequence[str] = ("*",), exclude: Sequence[str] = (),
                   page_size: int = 1000, max_pages: int = 3, full: Sequence[str] = (),
                   full_max_pages: int = 50, joins: Sequence[str] = (), k: int = K_MIN,
                   evidence_label: str = "live-read") -> dict[str, Any]:
    """Portal mirror relations (the Workbench API catalog) through bounded sample reads.

    Pages come from ``WorkbenchReadApiClient.read_page`` (one validated GET per keyset
    page with the catalog entry held once); a relation is ``complete`` only when its
    last page has no next cursor within the page bound. Readiness is checked once before
    and once after (``health``); ``read_relation`` keeps its fail-closed full-read
    semantics for the mirror and is not used here. A degraded API is profiled: the
    relations its health or catalog names unavailable are skipped as ``unavailable``."""
    from integrations.http import SourceUnavailable

    parsed = [parse_join(j) for j in joins]
    join_targets = {side[0] for pair in parsed for side in pair}
    # Raises SourceUnavailable unless the replica is ready; degraded names relations.
    listed = client.health().get("unavailable_relations") or {}
    catalog = client.relations()
    targets, errors, skipped = [], [], []
    kept: dict[str, tuple[list, bool]] = {}
    names = sorted(n for n in catalog if _matches(n, include) and not _matches(n, exclude))
    for name in sorted(set(full) | join_targets):
        if _matches(name, include) and not _matches(name, exclude) and name not in catalog:
            skipped.append({"name": name, "reason": "not_in_catalog"})
    for name in names:
        entry = catalog[name]
        if entry.get("unavailable") or name in listed:
            skipped.append({"name": name, "reason": "unavailable"})
            continue
        if not entry.get("key"):
            skipped.append({"name": name, "reason": "unpaged"})
            continue
        pages = full_max_pages if name in full else max_pages
        try:
            rows, complete = _sample_relation(client, name, entry, page_size=page_size,
                                              max_pages=pages)
        except (SourceUnavailable, ValueError) as exc:
            errors.append({"name": name, "error": _error_code(exc),
                           **http_diagnostics(exc)})
            continue
        targets.append(profile_rows(name, rows, key=entry["key"], complete=complete,
                                    columns=entry["columns"], k=k))  # catalog columns only
        if name in join_targets:
            kept[name] = (rows, complete)
    join_results = [join_stats(a, b, kept) for a, b in parsed]
    client.health()
    return _envelope("portal", evidence_label, targets, join_results, errors, skipped,
                     None, k)


def bc_profile(reader: Any, actor: Any, *, entity_sets: Sequence[str], page_size: int = 200,
               max_pages: int = 1, line_documents: int = 5, joins: Sequence[str] = (),
               k: int = K_MIN, requests: Any = None) -> dict[str, Any]:
    """Every company the reader sees (by index), each entity set and the lines of the
    first ``line_documents`` documents of each document set."""
    from integrations import live_source_grammar as grammar
    from integrations.live_source_guard import NotVisible, SourceUnavailable

    parsed = [parse_join(j) for j in joins]
    companies = reader.companies(actor)["items"]
    targets, errors, join_results = [], [], []
    for index, company in enumerate(companies):
        company_id = company.get("id")
        if not isinstance(company_id, str):
            errors.append({"name": "companies", "error": "company_id_missing",
                           "company_index": index})
            continue
        kept: dict[str, tuple[list, bool]] = {}
        for entity_set in entity_sets:
            try:
                listing = reader.list_entities(actor, company_id, entity_set,
                                               page_size=page_size, max_pages=max_pages,
                                               max_items=page_size * max_pages)
            except (SourceUnavailable, NotVisible, ValueError) as exc:
                errors.append({"name": entity_set, "error": _error_code(exc),
                               **http_diagnostics(exc), "company_index": index})
                continue
            items = listing["items"]
            complete = not listing["completeness"].get("truncated", True)
            rows = [item["fields"] for item in items]
            targets.append(profile_rows(entity_set, rows, key=("id",), complete=complete, k=k,
                                        columns=_bc_columns(grammar, entity_set),
                                        extra={"company_index": index}))
            kept[entity_set] = (rows, complete)
            line_set = grammar.BC_DOCUMENT_LINES.get(entity_set)
            if not line_set or line_documents <= 0:
                continue
            line_rows: list = []
            lines_complete = complete and len(items) <= line_documents
            read = 0
            for item in items[:line_documents]:
                try:
                    lines = reader.list_lines(actor, company_id, entity_set, item["id"],
                                              page_size=100, max_pages=2, max_items=200)
                except (SourceUnavailable, NotVisible, ValueError) as exc:
                    errors.append({"name": line_set, "error": _error_code(exc),
                                   **http_diagnostics(exc), "company_index": index})
                    lines_complete = False
                    continue
                read += 1
                lines_complete = lines_complete and not lines["completeness"].get("truncated")
                line_rows.extend(line["fields"] for line in lines["items"])
            targets.append(profile_rows(line_set, line_rows, key=("id",),
                                        columns=_bc_columns(grammar, line_set),
                                        complete=lines_complete, k=k,
                                        extra={"company_index": index,
                                               "documents_read": read}))
            kept[line_set] = (line_rows, lines_complete)
        join_results.extend(join_stats(a, b, kept, company_index=index) for a, b in parsed)
    return _envelope("bc", _label(reader), targets, join_results, errors, [],
                     getattr(requests, "requests_sent", None), k,
                     companies_count=len(companies))


def _bc_columns(grammar: Any, entity_set: str) -> list[str]:
    """The declared BC shape: the entity set's ``$select`` allowlist (always-selected
    identity fields included). Other BC properties are counted, not named; their names
    are published by the reviewed ``bc.case_entities`` probe."""
    spec = grammar.BC_QUERY_SPECS.get(entity_set)
    return sorted(spec.select) if spec is not None else ["id"]


def _label(reader: Any) -> str:
    label = getattr(reader, "evidence_label", None)
    return label if label in EVIDENCE_LABELS else "injected-transport"


_RECORD_EXCLUDE = frozenset({"source_acl"})


def mail_profile(reader: Any, actor: Any, *, mailbox: str, label: str, folder: str | None,
                 days: int, page_size: int = 50, max_pages: int = 10, k: int = K_MIN,
                 transport: Any = None, now: datetime | None = None) -> dict[str, Any]:
    """Message metadata of one mailbox folder over ``days`` (no bodies). A read Graph
    refuses for a missing grant (``graph_read.mail_block`` over ``transport``) is a
    ``blocked_upstream`` part, not an error."""
    from integrations.graph_read import mail_block
    from integrations.live_source_guard import NotVisible, SourceUnavailable

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    name = "mail." + label
    targets, errors, blocked = [], [], []
    try:
        listing = reader.list_messages(actor, mailbox, folder=folder, received_since=since,
                                       page_size=page_size, max_pages=max_pages,
                                       max_items=page_size * max_pages)
    except (SourceUnavailable, NotVisible, ValueError) as exc:
        found = mail_block(exc, transport) if transport is not None else None
        if found is not None:
            blocked.append(found)
        else:
            errors.append({"name": name, "error": _error_code(exc),
                           **http_diagnostics(exc)})
    else:
        rows = [{c: v for c, v in item.items() if c not in _RECORD_EXCLUDE}
                for item in listing["items"]]
        targets.append(profile_rows(name, rows, key=("id",), columns=MAIL_RECORD_KEYS,
                                    complete=not listing["completeness"].get("truncated", True),
                                    k=k))
    return _envelope("mail", _transport_label(transport), targets, [], errors, [],
                     getattr(transport, "requests_sent", None), k,
                     **({"blocked_upstream": blocked} if blocked else {}))


def _transport_label(transport: Any) -> str:
    return "live-read" if getattr(transport, "sender_injected", True) is False \
        else "injected-transport"


def drive_profile(reader: Any, actor: Any, *, path: str, label: str, max_depth: int = 1,
                  max_folders: int = 20, page_size: int = 200, k: int = K_MIN,
                  transport: Any = None) -> dict[str, Any]:
    """Drive-item metadata of one folder tree (breadth first, bounded; no content)."""
    from integrations.live_source_guard import NotVisible, SourceUnavailable

    name = "drive." + label
    rows: list[dict[str, Any]] = []
    errors: list = []
    complete = True
    queue = [(path, 0)]
    folders = 0
    while queue:
        folder, depth = queue.pop(0)
        if folders >= max_folders:
            complete = False
            break
        folders += 1
        try:
            listing = reader.list_folder(actor, folder, page_size=page_size, max_pages=5,
                                         max_items=page_size * 5)
        except (SourceUnavailable, NotVisible, ValueError) as exc:
            errors.append({"name": name, "error": _error_code(exc),
                           **http_diagnostics(exc)})
            complete = False
            continue
        if listing["completeness"].get("truncated") or listing["completeness"].get(
                "withheld_count"):
            complete = False
        for item in listing["items"]:
            row = {c: v for c, v in item.items() if c not in _RECORD_EXCLUDE}
            row["depth"] = depth
            rows.append(row)
            if item.get("kind") == "folder":
                if depth + 1 < max_depth:
                    queue.append((item["path"], depth + 1))
                else:
                    complete = False
    targets = [profile_rows(name, rows, key=("id",), complete=complete, k=k,
                            columns=DRIVE_RECORD_KEYS)]
    return _envelope("drive", _transport_label(transport), targets, [], errors, [],
                     getattr(transport, "requests_sent", None), k)


# ------------------------------------------------------------------ CLI

def _probe_actor() -> Any:
    from ontology.core import Actor
    return Actor("probe", frozenset({"probe:operator"}), "restricted")


def run(args: argparse.Namespace, environ: Mapping[str, str], *, sender: Any = None,
        portal_client: Any = None) -> dict[str, Any]:
    if args.kind == "portal":
        label = "injected-transport" if portal_client is not None else "live-read"
        client = portal_client
        if client is None:
            url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
            if not url or not key:
                raise LookupError("missing_env")
            from integrations.workbench_read_api import WorkbenchReadApiClient
            client = WorkbenchReadApiClient(url, key)
        return portal_profile(client, include=args.relations or ["*"], exclude=args.exclude,
                              page_size=args.page_size, max_pages=args.max_pages,
                              full=args.full, full_max_pages=args.full_max_pages,
                              joins=args.join, k=args.k, evidence_label=label)
    from integrations.live_source_guard import LiveSourceAudience
    actor = _probe_actor()
    if args.kind == "bc":
        from integrations.bc_read import CASE_ENTITY_SETS, BusinessCentralReader, build_bc
        api, transport, tenant_id, environment = build_bc(environ, sender=sender)
        audience = LiveSourceAudience({f"bc:{environment.lower()}:*": ["probe:operator"]},
                                      tenant="probe")
        reader = BusinessCentralReader(api, audience, tenant_id=tenant_id,
                                       environment=environment)
        return bc_profile(reader, actor, entity_sets=args.entity_sets or list(CASE_ENTITY_SETS),
                          page_size=args.page_size, max_pages=args.max_pages,
                          line_documents=args.line_documents, joins=args.join, k=args.k,
                          requests=transport)
    from integrations.graph_read import GraphMailReader, SharePointReader, build_graph
    api, transport = build_graph(environ, sender=sender)
    if args.kind == "mail":
        audience = LiveSourceAudience({"mail:*": ["probe:operator"]}, tenant="probe")
        return mail_profile(GraphMailReader(api, audience), actor, mailbox=args.mailbox,
                            label=args.label, folder=args.folder, days=args.days,
                            page_size=min(args.page_size, 50), max_pages=args.max_pages,
                            k=args.k, transport=transport)
    drive = args.drive_id or environ.get(args.drive_id_env or "", "")
    if not drive:
        raise LookupError("missing_env")
    base = args.path.rstrip("/") or ""
    audience = LiveSourceAudience({f"sharepoint:{drive}:{base or '/'}": ["probe:operator"],
                                   f"sharepoint:{drive}:{base}/*": ["probe:operator"]},
                                  tenant="probe")
    reader = SharePointReader(api, audience, drive, transport=transport)
    return drive_profile(reader, actor, path=args.path, label=args.label,
                         max_depth=args.max_depth, max_folders=args.max_folders,
                         page_size=min(args.page_size, 200), k=args.k, transport=transport)


def _label_arg(value: str) -> str:
    if not LABEL.fullmatch(value):
        raise argparse.ArgumentTypeError("label must match " + LABEL.pattern)
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m verify.data_profile",
                                description=__doc__.splitlines()[0])
    p.add_argument("kind", choices=SOURCE_KINDS)
    p.add_argument("--k", type=int, default=K_MIN, help=f"mask/key threshold (>= {K_MIN})")
    p.add_argument("--page-size", type=int, default=200)
    p.add_argument("--max-pages", type=int, default=1)
    p.add_argument("--join", action="append", default=[], metavar="T.C=T.C")
    p.add_argument("--relations", action="append", default=[], metavar="GLOB",
                   help="portal: relation globs (default every catalog relation)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    p.add_argument("--full", action="append", default=[], metavar="RELATION",
                   help="portal: read this relation up to --full-max-pages")
    p.add_argument("--full-max-pages", type=int, default=50)
    p.add_argument("--entity-sets", action="append", default=[],
                   help="bc: entity sets (default bc_read.CASE_ENTITY_SETS)")
    p.add_argument("--line-documents", type=int, default=5)
    p.add_argument("--mailbox")
    p.add_argument("--folder")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--drive-id")
    p.add_argument("--drive-id-env")
    p.add_argument("--path", default="/")
    p.add_argument("--max-depth", type=int, default=1)
    p.add_argument("--max-folders", type=int, default=20)
    p.add_argument("--label", type=_label_arg, default="target")
    return p


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None, portal_client: Any = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if args.k < K_MIN:
        p.error(f"--k must be >= {K_MIN}")
    if not (1 <= args.page_size <= 5000 and 1 <= args.max_pages <= 100
            and 1 <= args.full_max_pages <= 500 and 0 <= args.line_documents <= 50
            and 1 <= args.days <= 3650 and 1 <= args.max_depth <= 6
            and 1 <= args.max_folders <= 500):
        p.error("bound out of range")
    if args.kind == "mail" and not args.mailbox:
        p.error("mail needs --mailbox")
    if args.kind == "drive" and not (args.drive_id or args.drive_id_env):
        p.error("drive needs --drive-id or --drive-id-env")
    for join in args.join:
        try:
            parse_join(join)
        except ValueError:
            p.error("invalid --join")
    env = os.environ if environ is None else environ
    try:
        document = run(args, env, sender=sender, portal_client=portal_client)
    except Exception as exc:  # the class only: messages can carry URLs or values
        from integrations.live_source_guard import ReadOnlyGuardError
        code = "read_only_guard" if isinstance(exc, ReadOnlyGuardError) else _error_code(exc)
        print(json.dumps({"error": code, "kind": args.kind, **http_diagnostics(exc)}),
              file=sys.stderr)
        return 2 if code == "read_only_guard" else 1
    print(json.dumps(sanitize_profile(document), indent=1, sort_keys=True))
    return 0


__all__ = ["SCHEMA", "K_MIN", "bc_profile", "drive_profile", "join_stats", "mail_profile",
           "main", "parse_join", "portal_profile", "profile_rows", "sanitize_profile",
           "shape_mask", "string_class"]


if __name__ == "__main__":
    raise SystemExit(main())
