"""Candidate business measure definitions captured from real usage and promoted by owner review.

Saved dashboard tiles, accepted Ask answers with ad-hoc or derived SQL, and direct owner
proposals are captured here, deduplicated by canonical SQL signature with usage frequency
and provenance, sanitized through the people exclusion (fail closed), and queued for owner
review.

Approving a candidate promotes it into the live governed definitions overlay
(``governed_overlay.json`` beside ``candidates.json``), which ``operating.measures`` reads
alongside git-tracked ``measures.json`` so Ask and Insights cite the definition immediately
without modifying the release tree.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from operating.file_modes import keep_private, keep_shared
from operating import measures as _measures_mod
from operating.ontology_tools_identity import ToolError
from operating.ontology_tools_sql_check import _tokens, check as check_sql, check_where

LOCK_WAIT = 10.0

log = logging.getLogger(__name__)

ENV = "PLX_MEASURE_CANDIDATES_DIR"
CANDIDATES_FORMAT = "plx-measure-candidates/v1"
OVERLAY_FORMAT = "plx-governed-measures/v1"
CANDIDATE_STATES = ("pending", "approved", "dismissed")
CANDIDATE_SOURCES = ("accepted_answer", "saved_tile", "manual")
MEASURE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,50}$")
COL_NAME_RE = _measures_mod._NAME
AGG_EXPR_RE = _measures_mod._VALUE

# Secret-like tokens, long hex/base64 strings, and identity patterns scrubbed before storage.
_TOKEN_A_RE = re.compile(r"\b(?:sk|pk|rk|ghp|gho)[A-Za-z0-9_\-.]{16,}")
_TOKEN_B_RE = re.compile(r"\b(?:xox[abp]|eyJ)[A-Za-z0-9_\-.]{16,}")
_LONG_OPAQUE_RE = re.compile(r"\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9+/]{48,}={0,2}")
_KV_SECRET_RE = re.compile(r"\b(?:api[_-]?key|secret)\s*[:=]\s*\S+", re.IGNORECASE)
_KV_TOKEN_RE = re.compile(r"\b(?:token|password)\s*[:=]\s*\S+", re.IGNORECASE)
_KV_PWD_RE = re.compile(r"\b(?:passwd|pwd)\s*[:=]\s*\S+", re.IGNORECASE)
_OID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_MAIL_RE = re.compile(r"\b[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_ANGLED_MAIL_RE = re.compile(r"<\s*([^<>\s]+@[^<>\s]+)\s*>")
REDACTED = "[withheld]"

# Process-level registered state directory and in-memory overlay (for mock / tests).
_STATE_LOCK = threading.Lock()
_REGISTERED_DIR: Optional[Path] = None
_MEM_OVERLAY: dict[str, dict[str, Any]] = {}
_MEM_REV: int = 0


class CandidateError(ValueError):
    """A validation or state refusal with a plain-English message and HTTP status hint."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _iso_now(now: Optional[dt.datetime] = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _today_iso(now: Optional[dt.datetime] = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # guard-ok: hosts without tzdata fall back to UTC calendar day
        return moment.astimezone(dt.timezone.utc).date().isoformat()


def register(state_dir: Optional[Path]) -> None:
    """Register the server's state directory so ``operating.measures`` reads its overlay."""
    with _STATE_LOCK:
        global _REGISTERED_DIR
        _REGISTERED_DIR = (Path(state_dir) / "measure-candidates") if state_dir else None


def clear_memory_overlay() -> None:
    """Reset the in-memory governed overlay (for isolated tests)."""
    with _STATE_LOCK:
        global _MEM_REV
        _MEM_OVERLAY.clear()
        _MEM_REV += 1


def overlay_dir() -> Optional[Path]:
    """The configured directory for candidate and governed overlay files, or None."""
    override = os.environ.get(ENV)
    if override:
        return Path(override)
    with _STATE_LOCK:
        return _REGISTERED_DIR


def overlay_file() -> Optional[Path]:
    d = overlay_dir()
    return (d / "governed_overlay.json") if d is not None else None


def overlay_stamp() -> tuple[str, int, int]:
    """Cheap cache key for ``operating.measures``: changes immediately when the overlay moves."""
    path = overlay_file()
    if path is not None:
        try:
            st = path.stat()
            return (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            return (str(path), 0, 0)
    with _STATE_LOCK:
        return ("mem", _MEM_REV, len(_MEM_OVERLAY))


def governed_overlay() -> list[dict[str, Any]]:
    """Every owner-approved measure definition from the live governed overlay."""
    path = overlay_file()
    if path is None:
        with _STATE_LOCK:
            return [dict(v) for v in _MEM_OVERLAY.values()]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.error("governed measure overlay unreadable (%s): %s", path, exc)
        return []
    items = raw.get("measures") if isinstance(raw, Mapping) else None
    if not isinstance(items, list):
        return []
    return [dict(m) for m in items if isinstance(m, Mapping)]


# ----------------------------------------------------------------- exclusion and masking


def _default_guard() -> Any:
    try:
        from operating.pg.permissions import load_exclusion
        return load_exclusion()
    except Exception as exc:  # noqa: BLE001
        log.warning("people exclusion unavailable for measure candidates: %s", type(exc).__name__)
        return None


def _mail_forms(address: str) -> list[str]:
    plain = str(address).strip().lower()
    angled = _ANGLED_MAIL_RE.search(plain)
    plain = angled.group(1) if angled else plain
    local, at, domain = plain.rpartition("@")
    forms = [plain]
    if at and "+" in local.lstrip("+"):
        forms.append(local.split("+", 1)[0] + "@" + domain)
    return forms


def _oid_excluded(guard: Any, oid: str) -> bool:
    try:
        return guard is None or bool(guard.unavailable) or bool(guard.is_excluded(oid))
    except Exception as exc:  # noqa: BLE001
        log.warning("exclusion check failed for id: %s", type(exc).__name__)
        return True


def _one_mail_excluded(guard: Any, address: str) -> bool:
    try:
        if guard is None or guard.unavailable:
            return True
        if guard.decides(address):
            return bool(guard.is_excluded(address))
        domains = set(guard.known_domains())
        return not domains or address.rsplit("@", 1)[1].lower() in domains
    except Exception as exc:  # noqa: BLE001
        log.warning("exclusion check failed for address: %s", type(exc).__name__)
        return True


def _mail_excluded(guard: Any, address: str) -> bool:
    return any(_one_mail_excluded(guard, form) for form in _mail_forms(address))


def _scrub_secrets(text: str) -> str:
    out = _TOKEN_A_RE.sub("[removed]", text)
    out = _TOKEN_B_RE.sub("[removed]", out)
    out = _LONG_OPAQUE_RE.sub("[removed]", out)
    out = _KV_SECRET_RE.sub("[removed]", out)
    out = _KV_TOKEN_RE.sub("[removed]", out)
    return _KV_PWD_RE.sub("[removed]", out)


def _has_secret(text: str) -> bool:
    return bool(
        _TOKEN_A_RE.search(text)
        or _TOKEN_B_RE.search(text)
        or _LONG_OPAQUE_RE.search(text)
        or _KV_SECRET_RE.search(text)
        or _KV_TOKEN_RE.search(text)
        or _KV_PWD_RE.search(text)
    )


def clean_text(text: Any, guard: Any) -> str:
    """Scrub secret-like strings and excluded/undecidable person ids and emails (fail closed)."""
    if not isinstance(text, str):
        return ""
    out = _scrub_secrets(text)
    out = _OID_RE.sub(lambda m: REDACTED if _oid_excluded(guard, m.group(0).lower()) else m.group(0), out)
    out = _MAIL_RE.sub(lambda m: REDACTED if _mail_excluded(guard, m.group(0).lower()) else m.group(0), out)
    return out.strip()


def sql_names_excluded(sql: str, guard: Any) -> bool:
    """True when ``sql`` contains a credential or an excluded/undecidable person id or email."""
    if _has_secret(sql):
        return True
    if any(_oid_excluded(guard, oid.lower()) for oid in _OID_RE.findall(sql)):
        return True
    if any(_mail_excluded(guard, addr.lower()) for addr in _MAIL_RE.findall(sql)):
        return True
    return False


# ----------------------------------------------------------------- SQL normalization & extraction


def normalize_sql(sql: str) -> str:
    """Canonical representation of a checked SELECT statement for deduplication."""
    checked = check_sql(sql, bare_views=True)
    toks = _tokens(checked)
    parts: list[str] = []
    for kind, text in toks:
        if kind == "word":
            parts.append(text.lower())
        elif kind == "qident":
            parts.append(f'"{text}"')
        elif kind == "string":
            parts.append("'" + text.replace("'", "''") + "'")
        else:
            parts.append(text)
    return " ".join(parts)


def statement_signature(sql: str) -> str:
    """Stable deduplication key for a SQL statement (invariant to whitespace and keyword case)."""
    norm = normalize_sql(sql)
    return "sig-" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _view_dictionary() -> dict[str, dict[str, Any]]:
    try:
        from operating.ontology_tools_sql import _dictionary
        return dict(_dictionary())
    except Exception as exc:  # noqa: BLE001
        log.warning("view dictionary unavailable for candidate extraction: %s", type(exc).__name__)
        return {}


def extract_views(sql: str, dictionary: Optional[Mapping[str, Any]] = None) -> list[str]:
    """Ordered unique business view names referenced after FROM or JOIN in ``sql``."""
    checked = check_sql(sql, bare_views=True)
    toks = _tokens(checked)
    known = set(dictionary if dictionary is not None else _view_dictionary())
    views: list[str] = []
    for i, (kind, tok) in enumerate(toks):
        if kind == "word" and tok.lower() in ("from", "join") and i + 1 < len(toks):
            nk, nt = toks[i + 1]
            if nk in ("word", "qident"):
                low = nt.lower()
                if low in known and low not in views:
                    views.append(low)
    return views


def _top_level_clauses(toks: Sequence[tuple[str, str]]) -> dict[str, list[tuple[str, str]]]:
    """Split a single SELECT statement's top-level tokens into clause slices (depth 0)."""
    clauses: dict[str, list[tuple[str, str]]] = {}
    depth = 0
    current: Optional[str] = None
    i = 0
    n = len(toks)
    while i < n:
        kind, tok = toks[i]
        if kind == "punct" and tok == "(":
            depth += 1
            if current is not None:
                clauses[current].append((kind, tok))
            i += 1
            continue
        if kind == "punct" and tok == ")":
            depth = max(0, depth - 1)
            if current is not None:
                clauses[current].append((kind, tok))
            i += 1
            continue
        if depth == 0 and kind == "word":
            low = tok.lower()
            nxt = toks[i + 1][1].lower() if i + 1 < n and toks[i + 1][0] == "word" else ""
            if low in ("select", "from", "where", "having", "limit", "offset"):
                current = low
                clauses.setdefault(current, [])
                i += 1
                continue
            if low in ("group", "order") and nxt == "by":
                current = f"{low}_by"
                clauses.setdefault(current, [])
                i += 2
                continue
        if current is not None:
            clauses[current].append((kind, tok))
        i += 1
    return clauses


def _split_commas(toks: Sequence[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    parts: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    depth = 0
    for kind, tok in toks:
        if kind == "punct" and tok == "(":
            depth += 1
            cur.append((kind, tok))
        elif kind == "punct" and tok == ")":
            depth = max(0, depth - 1)
            cur.append((kind, tok))
        elif depth == 0 and kind == "punct" and tok == ",":
            if cur:
                parts.append(cur)
                cur = []
        else:
            cur.append((kind, tok))
    if cur:
        parts.append(cur)
    return parts


def _strip_alias(expr: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    depth = 0
    for idx, (kind, tok) in enumerate(expr):
        if kind == "punct" and tok == "(":
            depth += 1
        elif kind == "punct" and tok == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and kind == "word" and tok.lower() == "as":
            return list(expr[:idx])
    if len(expr) >= 2 and expr[-1][0] in ("word", "qident") and expr[-2] == ("punct", ")"):
        return list(expr[:-1])
    return list(expr)


def _extract_plain_agg(expr: Sequence[tuple[str, str]]) -> Optional[str]:
    core = _strip_alias(expr)
    if len(core) == 4 and core[0][0] == "word" and core[1] == ("punct", "(") and core[3] == ("punct", ")"):
        fn = core[0][1].lower()
        arg = core[2][1].lower() if core[2][0] == "word" else core[2][1]
        cand = f"{fn}({arg})"
        if AGG_EXPR_RE.match(cand):
            return cand
    if (
        len(core) == 5
        and core[0][0] == "word"
        and core[1] == ("punct", "(")
        and core[2][0] == "word"
        and core[2][1].lower() == "distinct"
        and core[3][0] == "word"
        and core[4] == ("punct", ")")
    ):
        cand = f"{core[0][1].lower()}(DISTINCT {core[3][1].lower()})"
        if AGG_EXPR_RE.match(cand):
            return cand
    return None


def _tokens_to_sql(toks: Sequence[tuple[str, str]]) -> str:
    out: list[str] = []
    for kind, tok in toks:
        if kind == "string":
            piece = "'" + tok.replace("'", "''") + "'"
        elif kind == "qident":
            piece = f'"{tok}"'
        else:
            piece = tok
        if out and piece not in (",", ")", ".", "::") and out[-1] not in ("(", ".", "::"):
            out.append(" ")
        out.append(piece)
    return "".join(out).strip()


def _slug_id(text: str, fallback: str = "custom_measure") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"m_{cleaned}" if cleaned else fallback
    if len(cleaned) < 2:
        cleaned = fallback
    if len(cleaned) > 48:
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:6]
        parts: list[str] = []
        total = 0
        for word in cleaned.split("_"):
            step = len(word) + (1 if parts else 0)
            if total + step > 40:
                break
            parts.append(word)
            total += step
        head = "_".join(parts) if parts else "m"
        cleaned = f"{head}_{digest}"
    return cleaned


def _humanize_name(name: str) -> str:
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Custom measure"


def extract_proposal(
    sql: str,
    *,
    title: str = "",
    question: str = "",
    dictionary: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Extract proposed measure metadata from a checked SQL statement."""
    checked = check_sql(sql, bare_views=True)
    toks = _tokens(checked)
    views_dict = dict(dictionary) if dictionary is not None else _view_dictionary()
    views = extract_views(checked, views_dict)

    has_complex_top = any(
        kind == "word" and tok.lower() in ("with", "join", "union", "intersect", "except", "over", "having")
        for kind, tok in toks
    )
    clauses = _top_level_clauses(toks)
    select_items = _split_commas(clauses.get("select", []))
    group_items = _split_commas(clauses.get("group_by", []))
    where_toks = clauses.get("where", [])

    proposed_view = views[0] if len(views) == 1 else ""
    view_cols: dict[str, str] = {}
    if proposed_view and proposed_view in views_dict:
        view_cols = dict((views_dict[proposed_view] or {}).get("columns") or {})

    aggs: list[str] = []
    non_aggs: list[list[tuple[str, str]]] = []
    for item in select_items:
        agg = _extract_plain_agg(item)
        if agg is not None:
            aggs.append(agg)
        else:
            non_aggs.append(_strip_alias(item))

    proposed_value = ""
    if len(aggs) == 1 and proposed_view:
        m = AGG_EXPR_RE.match(aggs[0])
        if m and (m.group(3) == "*" or m.group(3) in view_cols):
            proposed_value = aggs[0]

    # Identify date columns in the view and see if the query uses one.
    date_cols = [
        c for c, desc in view_cols.items()
        if str(desc).split(":", 1)[0].strip().lower().startswith(("date", "timestamp"))
    ]
    referenced_words = {tok.lower() for kind, tok in toks if kind == "word"}
    proposed_date = next((c for c in date_cols if c in referenced_words), "")

    # Identify currency/unit split column matching the aggregated column.
    proposed_per = ""
    if proposed_value and proposed_view:
        val_col = AGG_EXPR_RE.match(proposed_value).group(3)
        for suffix in ("_currency", "_unit"):
            cand_per = f"{val_col}{suffix}"
            if cand_per in view_cols:
                proposed_per = cand_per
                break
        if not proposed_per:
            for col in view_cols:
                if col in referenced_words and (col.endswith("_currency") or col.endswith("_unit")):
                    proposed_per = col
                    break

    # Extract dimensions from GROUP BY or non-aggregated SELECT items that are columns of the view.
    proposed_dimensions: list[str] = []
    for items in (group_items, non_aggs):
        for expr in items:
            if len(expr) == 1 and expr[0][0] == "word":
                col = expr[0][1].lower()
                if col in view_cols and col != proposed_date and col != proposed_per and col not in proposed_dimensions:
                    proposed_dimensions.append(col)

    # Extract WHERE only when it is simple (no subqueries, no date window literals/now()).
    where_has_subquery = any(k == "word" and t.lower() == "select" for k, t in where_toks)
    where_has_time_filter = any(
        (k == "word" and t.lower() in ("now", "interval", "current_date", "current_timestamp"))
        or (k == "string" and bool(re.match(r"^\d{4}-\d{2}-\d{2}", t)))
        for k, t in where_toks
    )
    where_sql = _tokens_to_sql(where_toks) if where_toks else ""

    # Decide if this query can run as a full structured single-view measure without losing SQL semantics.
    can_be_structured = (
        bool(proposed_view)
        and bool(proposed_value)
        and not has_complex_top
        and not where_has_subquery
        and not where_has_time_filter
        and len(aggs) == 1
        and all(
            (len(e) == 1 and e[0][0] == "word" and e[0][1].lower() in view_cols)
            or any(k == "word" and t.lower() == "date_trunc" for k, t in e)
            for e in non_aggs
        )
    )

    proposed_where = where_sql if can_be_structured else ""
    sql_mode = "structured" if can_be_structured else "custom_sql"

    if proposed_per.endswith("_currency"):
        proposed_unit = "money"
    elif proposed_per.endswith("_unit"):
        proposed_unit = "quantity"
    elif proposed_value.startswith("count("):
        proposed_unit = "count"
    else:
        proposed_unit = "number"

    proposed_grain = "record" if "DISTINCT" in proposed_value else "row"

    raw_title = title.strip() or question.strip()
    if raw_title:
        proposed_name = raw_title.rstrip("?").strip()
    elif proposed_value and proposed_view:
        proposed_name = f"{_humanize_name(proposed_value)} on {proposed_view.replace('_', ' ')}"
    elif views:
        proposed_name = f"Measure over {', '.join(v.replace('_', ' ') for v in views)}"
    else:
        proposed_name = "Proposed measure"

    proposed_id = _slug_id(proposed_name)
    if question.strip():
        proposed_description = f"Defined from usage: {question.strip()}"
    elif title.strip():
        proposed_description = f"Defined from pinned chart: {title.strip()}"
    elif views:
        proposed_description = f"Calculated over {', '.join(views)}."
    else:
        proposed_description = "Proposed shared business definition."

    return {
        "sql": checked,
        "views": views,
        "sql_mode": sql_mode,
        "proposed_id": proposed_id,
        "proposed_name": proposed_name,
        "proposed_description": proposed_description,
        "proposed_owner": "Company",
        "proposed_unit": proposed_unit,
        "proposed_grain": proposed_grain,
        "proposed_view": proposed_view,
        "proposed_value": proposed_value,
        "proposed_per": proposed_per,
        "proposed_date": proposed_date,
        "proposed_where": proposed_where,
        "proposed_dimensions": proposed_dimensions,
    }


def _atomic_write_json(path: Path, body: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    keep_private(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    keep_shared(tmp, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def _governed_entry_from_candidate(
    cand: Mapping[str, Any],
    edits: Mapping[str, Any],
    now: Optional[dt.datetime] = None,
    dictionary: Optional[Mapping[str, Any]] = None,
    guard: Any = None,
) -> dict[str, Any]:
    """Build and validate a governed measure entry ready for ``operating.measures``."""
    from operating import measures as ms

    active_guard = _default_guard() if guard is None else guard
    views_dict = dict(dictionary) if dictionary is not None else _view_dictionary()
    mid = str(edits.get("id") or cand.get("proposed_id") or "").strip().lower()
    if not MEASURE_ID_RE.match(mid):
        raise CandidateError(
            "invalid_measure_id",
            "Measure id must be 2 to 51 lowercase letters, digits, or underscores starting with a letter.",
        )

    base_ids = set(ms.base_measures()) | set(ms.reports())
    if mid in base_ids:
        raise CandidateError(
            "measure_id_exists",
            f"A built-in measure or report named '{mid}' already exists; choose a distinct id.",
            status=409,
        )

    name = str(edits.get("name") or cand.get("proposed_name") or "").strip()
    description = str(edits.get("description") or cand.get("proposed_description") or "").strip()
    owner = str(edits.get("owner") or cand.get("proposed_owner") or "Company").strip()
    unit = str(edits.get("unit") or cand.get("proposed_unit") or "number").strip()
    grain = str(edits.get("grain") or cand.get("proposed_grain") or "row").strip()
    if not name or not description or not owner:
        raise CandidateError("invalid_measure", "Name, description, and owner are required.")

    try:
        sql = check_sql(str(cand.get("sql") or ""), bare_views=True)
    except ToolError as exc:
        raise CandidateError("invalid_sql", exc.message, status=422) from None
    view = str(edits.get("view") if edits.get("view") is not None else cand.get("proposed_view") or "").strip()
    value = str(edits.get("value") if edits.get("value") is not None else cand.get("proposed_value") or "").strip()
    per = str(edits.get("per") if edits.get("per") is not None else cand.get("proposed_per") or "").strip()
    date_col = str(edits.get("date") if edits.get("date") is not None else cand.get("proposed_date") or "").strip()
    where = str(edits.get("where") if edits.get("where") is not None else cand.get("proposed_where") or "").strip()
    dims_raw = edits.get("dimensions") if edits.get("dimensions") is not None else cand.get("proposed_dimensions") or []
    dimensions = [str(d).strip() for d in dims_raw if str(d).strip()]

    if where:
        if sql_names_excluded(where, active_guard):
            raise CandidateError(
                "excluded_content",
                "This condition names a person or value that cannot be stored as a shared definition.",
                status=422,
            )
        safe_view = view if view in views_dict and COL_NAME_RE.match(view) else "t"
        try:
            where = check_where(where, safe_view)
        except ToolError as exc:
            raise CandidateError("invalid_sql", exc.message, status=422) from None

    structured_requested = any(edits.get(k) is not None for k in ("view", "value", "per", "date", "where"))
    use_structured = (cand.get("sql_mode") == "structured" or structured_requested) and bool(view and value)

    if use_structured:
        if view not in views_dict:
            raise CandidateError("invalid_measure", f"Unknown business view '{view}'.")
        vcols = set((views_dict[view] or {}).get("columns") or ())
        if not AGG_EXPR_RE.match(value):
            raise CandidateError("invalid_measure", f"Value '{value}' is not a plain aggregate expression.")
        agg_col = AGG_EXPR_RE.match(value).group(3)
        if agg_col != "*" and agg_col not in vcols:
            raise CandidateError("invalid_measure", f"Column '{agg_col}' is not on view '{view}'.")
        for field_name, col_val in (("per", per), ("date", date_col)):
            if col_val and (not COL_NAME_RE.match(col_val) or col_val not in vcols):
                raise CandidateError("invalid_measure", f"{field_name} '{col_val}' is not a column on '{view}'.")
        for d in dimensions:
            if not COL_NAME_RE.match(d) or d not in vcols:
                raise CandidateError("invalid_measure", f"Dimension '{d}' is not a column on '{view}'.")
        # Honor the catalogue's pilot / of_record conventions when the view carries those columns.
        parameters: list[dict[str, Any]] = []
        if "pilot" in vcols and "pilot" not in where:
            parameters.append({
                "id": "bc_pilot",
                "default": False,
                "excludes_where": "pilot",
                "says": "records held only in Business Central",
            })
        if "of_record" in vcols and "of_record" not in where:
            where = f"({where}) AND of_record" if where else "of_record"
        entry: dict[str, Any] = {
            "id": mid,
            "name": name,
            "owner": owner,
            "unit": unit,
            "definition": description,
            "view": view,
            "value": value,
            "per": per,
            "date": date_col,
            "where": where,
            "dimensions": dimensions,
            "days": 30,
            "grain": grain,
            "parameters": parameters,
            "governed_source": "approved_candidate",
            "candidate_id": cand.get("id"),
        }
    else:
        primary_view = view or ((cand.get("views") or [""])[0])
        entry = {
            "id": mid,
            "name": name,
            "owner": owner,
            "unit": unit,
            "definition": description,
            "view": primary_view,
            "value": "",
            "per": "",
            "date": "",
            "where": "",
            "dimensions": [d for d in dimensions if COL_NAME_RE.match(d)],
            "grain": grain,
            "sql": sql,
            "governed_source": "approved_candidate",
            "candidate_id": cand.get("id"),
        }

    entry["version"] = ms.version(entry)
    entry["defined_on"] = _today_iso(now)
    return entry


# ----------------------------------------------------------------- candidate store


class _StoreLock:
    """Thread-safe (RLock) and cross-process lock."""

    def __init__(self, directory: Optional[Path]) -> None:
        self.directory = directory
        self._thread = threading.RLock()
        self._depth = 0
        self._fd: Optional[int] = None

    @contextlib.contextmanager
    def acquire(self, *, write: bool = True) -> Iterator[None]:
        deadline = time.monotonic() + LOCK_WAIT
        if not self._thread.acquire(timeout=LOCK_WAIT):
            raise CandidateError(
                "busy",
                "Candidate definitions are busy right now: try again in a moment.",
                status=503,
            )
        try:
            if self._depth == 0 and self.directory is not None:
                fd: Optional[int] = None
                if write:
                    self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    keep_private(self.directory, 0o700)
                    lock_path = self.directory / "candidates.lock"
                    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                    keep_shared(lock_path, 0o600)
                elif self.directory.exists():
                    lock_path = self.directory / "candidates.lock"
                    try:
                        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                        keep_shared(lock_path, 0o600)
                    except OSError as exc:
                        log.debug("candidate store reading without lock (%s): %s", self.directory, type(exc).__name__)
                        fd = None
                if fd is not None:
                    try:
                        while True:
                            try:
                                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                if time.monotonic() >= deadline:
                                    raise CandidateError(
                                        "busy",
                                        "Candidate definitions are busy right now: try again in a moment.",
                                        status=503,
                                    ) from None
                                time.sleep(0.05)
                    except BaseException:
                        os.close(fd)
                        raise
                    self._fd = fd
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    fd, self._fd = self._fd, None
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
        finally:
            self._thread.release()

    def __enter__(self) -> "_StoreLock":
        self._cm = self.acquire(write=True)
        self._cm.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._cm.__exit__(*exc)


class CandidateStore:
    """Thread- and process-safe candidate store and governed overlay writer.

    The
    directory is created lazily on write so read-only directories do not fail startup.
    When ``directory`` is None, stores candidates in memory and publishes approved measures
    to the process in-memory overlay.
    """

    def __init__(self, directory: Optional[Path]) -> None:
        self.directory = Path(directory) if directory is not None else None
        self._lock = _StoreLock(self.directory)
        self._memory: dict[str, dict[str, Any]] = {}

    @classmethod
    def for_backend(cls, backend: Any) -> "CandidateStore":
        override = os.environ.get(ENV)
        state = getattr(backend, "state_dir", None)
        data = getattr(backend, "data_dir", None)
        directory = (
            Path(override) if override
            else Path(state) / "measure-candidates" if state
            else Path(data) / "measure-candidates" if data
            else None
        )
        return cls(directory)

    def _candidates_path(self) -> Optional[Path]:
        return (self.directory / "candidates.json") if self.directory is not None else None

    def _overlay_path(self) -> Optional[Path]:
        return (self.directory / "governed_overlay.json") if self.directory is not None else None

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        path = self._candidates_path()
        if path is None:
            return {k: dict(v) for k, v in self._memory.items()}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.error("candidate store unreadable (%s): %s", path, exc)
            return {}
        rows = raw.get("candidates") if isinstance(raw, Mapping) else None
        if not isinstance(rows, list):
            return {}
        return {str(r["id"]): dict(r) for r in rows if isinstance(r, Mapping) and r.get("id")}

    def _save_locked(self, items: Mapping[str, Mapping[str, Any]]) -> None:
        ordered = sorted(
            (dict(v) for v in items.values()),
            key=lambda c: (-int(c.get("usage_count") or 0), str(c.get("last_seen_at") or "")),
        )
        path = self._candidates_path()
        if path is None:
            self._memory = {c["id"]: c for c in ordered}
            return
        _atomic_write_json(path, {"format": CANDIDATES_FORMAT, "candidates": ordered})

    def _publish_overlay_locked(self, entry: Mapping[str, Any]) -> None:
        path = self._overlay_path()
        if path is None:
            with _STATE_LOCK:
                global _MEM_REV
                _MEM_OVERLAY[str(entry["id"])] = dict(entry)
                _MEM_REV += 1
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            existing = [m for m in (raw.get("measures") or []) if isinstance(m, Mapping)]
        except FileNotFoundError:
            existing = []
        except (OSError, ValueError) as exc:
            log.error("governed overlay unreadable before update (%s): %s", path, exc)
            existing = []
        by_id = {str(m["id"]): dict(m) for m in existing if m.get("id")}
        by_id[str(entry["id"])] = dict(entry)
        ordered = [by_id[k] for k in sorted(by_id)]
        _atomic_write_json(path, {"format": OVERLAY_FORMAT, "measures": ordered})

    def capture(
        self,
        *,
        sql: str,
        source_kind: str = "manual",
        title: str = "",
        question: str = "",
        source_ref: str = "",
        name: str = "",
        description: str = "",
        owner: str = "",
        proposer_key: str = "",
        proposer_name: Optional[str] = None,
        proposer_hidden: bool = False,
        markings: Optional[Sequence[str]] = None,
        guard: Any = None,
        now: Optional[dt.datetime] = None,
        dictionary: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Capture or increment a candidate measure definition, sanitized through ``guard``."""
        if source_kind not in CANDIDATE_SOURCES:
            raise CandidateError("invalid_source", f"Unknown candidate source '{source_kind}'.")
        active_guard = _default_guard() if guard is None else guard
        try:
            checked_sql = check_sql(sql, bare_views=True)
        except ToolError as exc:
            raise CandidateError("invalid_sql", exc.message, status=422) from None

        if sql_names_excluded(checked_sql, active_guard):
            raise CandidateError(
                "excluded_content",
                "This statement names a person or value that cannot be stored as a shared definition.",
                status=422,
            )

        clean_title = clean_text(title, active_guard)
        clean_question = clean_text(question, active_guard)
        clean_name = clean_text(name, active_guard)
        clean_desc = clean_text(description, active_guard)
        clean_owner = clean_text(owner, active_guard)
        clean_ref = clean_text(source_ref, active_guard)
        stored_proposer = None if proposer_hidden else (clean_text(proposer_name, active_guard) or None)

        extracted = extract_proposal(
            checked_sql,
            title=clean_name or clean_title,
            question=clean_question,
            dictionary=dictionary,
        )
        sig = statement_signature(checked_sql)
        at = _iso_now(now)
        norm_markings = None if markings is None else sorted({str(m) for m in markings})

        with self._lock:
            items = self._load_locked()
            existing = next((c for c in items.values() if c.get("signature") == sig), None)
            if existing is not None:
                existing["usage_count"] = int(existing.get("usage_count") or 1) + 1
                existing["last_seen_at"] = at
                same_proposer = proposer_key == existing.get("proposer_key")
                if same_proposer:
                    if clean_question and not existing.get("question"):
                        existing["question"] = clean_question
                    if clean_title and not existing.get("tile_title"):
                        existing["tile_title"] = clean_title
                    if clean_name and existing.get("status") == "pending":
                        existing["proposed_name"] = clean_name
                        existing["proposed_id"] = _slug_id(clean_name)
                    if clean_desc and existing.get("status") == "pending":
                        existing["proposed_description"] = clean_desc
                    if clean_owner and existing.get("status") == "pending":
                        existing["proposed_owner"] = clean_owner
                # Merge sensitive markings upward (never downgrade sensitivity on dedup).
                prev_m = existing.get("markings")
                if prev_m is None or norm_markings is None:
                    existing["markings"] = None
                else:
                    existing["markings"] = sorted(set(prev_m) | set(norm_markings))
                items[existing["id"]] = existing
                self._save_locked(items)
                return dict(existing)

            cid = "mc-" + secrets.token_hex(6)
            proposed_name = clean_name or extracted["proposed_name"]
            proposed_desc = clean_desc or extracted["proposed_description"]
            proposed_owner = clean_owner or extracted["proposed_owner"]
            record: dict[str, Any] = {
                "id": cid,
                "signature": sig,
                "status": "pending",
                "source_kind": source_kind,
                "source_ref": clean_ref or None,
                "tile_title": clean_title or None,
                "question": clean_question or None,
                "sql": extracted["sql"],
                "views": list(extracted["views"]),
                "sql_mode": extracted["sql_mode"],
                "usage_count": 1,
                "first_seen_at": at,
                "last_seen_at": at,
                "proposed_id": _slug_id(proposed_name),
                "proposed_name": proposed_name,
                "proposed_description": proposed_desc,
                "proposed_owner": proposed_owner,
                "proposed_unit": extracted["proposed_unit"],
                "proposed_grain": extracted["proposed_grain"],
                "proposed_view": extracted["proposed_view"],
                "proposed_value": extracted["proposed_value"],
                "proposed_per": extracted["proposed_per"],
                "proposed_date": extracted["proposed_date"],
                "proposed_where": extracted["proposed_where"],
                "proposed_dimensions": list(extracted["proposed_dimensions"]),
                "proposer_key": proposer_key,
                "proposer_name": stored_proposer,
                "proposer_hidden": bool(proposer_hidden),
                "markings": norm_markings,
                "reviewed_by": None,
                "reviewed_at": None,
                "dismiss_reason": None,
                "approved_measure_id": None,
            }
            items[cid] = record
            self._save_locked(items)
            return dict(record)

    def get(self, candidate_id: str) -> Optional[dict[str, Any]]:
        with self._lock.acquire(write=False):
            items = self._load_locked()
            found = items.get(candidate_id)
            return dict(found) if found is not None else None

    def list(self, *, status: Optional[str] = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Return every matching candidate (no silent cap), ordered by usage then recency,
        plus counts per status across all candidates."""
        if status is not None and status not in CANDIDATE_STATES:
            raise CandidateError("invalid_status", f"Unknown candidate status '{status}'.")
        with self._lock.acquire(write=False):
            items = list(self._load_locked().values())
        counts = {s: 0 for s in CANDIDATE_STATES}
        for c in items:
            st = str(c.get("status") or "pending")
            if st in counts:
                counts[st] += 1
        filtered = [dict(c) for c in items if status is None or c.get("status") == status]
        filtered.sort(
            key=lambda c: (
                0 if c.get("status") == "pending" else 1,
                -int(c.get("usage_count") or 0),
                -(dt.datetime.fromisoformat(str(c["last_seen_at"]).replace("Z", "+00:00")).timestamp()
                  if c.get("last_seen_at") else 0.0),
                str(c.get("id") or ""),
            )
        )
        return filtered, counts

    def approve(
        self,
        candidate_id: str,
        *,
        reviewer_name: str,
        edits: Optional[Mapping[str, Any]] = None,
        guard: Any = None,
        now: Optional[dt.datetime] = None,
        dictionary: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Approve a candidate and promote it to the live governed overlay."""
        active_guard = _default_guard() if guard is None else guard
        clean_edits: dict[str, Any] = {}
        for k, v in (edits or {}).items():
            if v is None:
                continue
            if isinstance(v, str) and k in ("name", "description", "owner", "unit", "grain"):
                clean_edits[k] = clean_text(v, active_guard)
            else:
                clean_edits[k] = v

        with self._lock:
            items = self._load_locked()
            cand = items.get(candidate_id)
            if cand is None:
                raise CandidateError("not_found", "No such candidate definition.", status=404)
            if sql_names_excluded(str(cand.get("sql") or ""), active_guard):
                raise CandidateError(
                    "excluded_content",
                    "This statement names a person or value that cannot be promoted.",
                    status=422,
                )
            entry = _governed_entry_from_candidate(
                cand, clean_edits, now=now, dictionary=dictionary, guard=active_guard)
            self._publish_overlay_locked(entry)
            cand["status"] = "approved"
            cand["reviewed_by"] = clean_text(reviewer_name, active_guard) or None
            cand["reviewed_at"] = _iso_now(now)
            cand["approved_measure_id"] = entry["id"]
            cand["proposed_id"] = entry["id"]
            cand["proposed_name"] = entry["name"]
            cand["proposed_description"] = entry["definition"]
            cand["proposed_owner"] = entry["owner"]
            cand["proposed_unit"] = entry["unit"]
            cand["proposed_grain"] = entry.get("grain") or cand.get("proposed_grain") or "row"
            cand["proposed_dimensions"] = list(entry.get("dimensions") or [])
            items[candidate_id] = cand
            self._save_locked(items)
            return dict(cand)

    def dismiss(
        self,
        candidate_id: str,
        *,
        reviewer_name: str,
        reason: Optional[str] = None,
        guard: Any = None,
        now: Optional[dt.datetime] = None,
    ) -> dict[str, Any]:
        """Dismiss a candidate so it no longer waits in the review queue."""
        active_guard = _default_guard() if guard is None else guard
        clean_reason = clean_text(reason, active_guard) if reason else None
        with self._lock:
            items = self._load_locked()
            cand = items.get(candidate_id)
            if cand is None:
                raise CandidateError("not_found", "No such candidate definition.", status=404)
            cand["status"] = "dismissed"
            cand["reviewed_by"] = clean_text(reviewer_name, active_guard) or None
            cand["reviewed_at"] = _iso_now(now)
            cand["dismiss_reason"] = clean_reason or None
            items[candidate_id] = cand
            self._save_locked(items)
            return dict(cand)


def reclean_candidate(cand: Mapping[str, Any], guard: Any) -> dict[str, Any]:
    """Re-apply current people exclusion to every displayed text field of a candidate."""
    out = dict(cand)
    for field in (
        "tile_title",
        "question",
        "proposed_name",
        "proposed_description",
        "proposed_owner",
        "proposer_name",
        "reviewed_by",
        "dismiss_reason",
        "source_ref",
    ):
        val = out.get(field)
        if isinstance(val, str):
            out[field] = clean_text(val, guard) or None
    if isinstance(out.get("sql"), str) and sql_names_excluded(out["sql"], guard):
        out["sql"] = None
    if isinstance(out.get("proposed_where"), str) and out["proposed_where"] and sql_names_excluded(out["proposed_where"], guard):
        out["proposed_where"] = ""
    return out
