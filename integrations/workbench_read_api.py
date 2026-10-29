"""Consumer for Portal's deployed read-only Workbench API.

The API pages approved FM/Sage relations; it has no exact-record filter and
does not provide a transaction-wide snapshot. This module therefore exposes
bounded read evidence, not a live named replica attestation or business-action
authority. Its one product consumer is ``integrations.source_mirror``.

Partial availability: the service names the relations it cannot serve (``unavailable``
and ``missingColumns`` of ``GET /v1/health``, ``unavailable`` on a catalog entry).
:meth:`WorkbenchReadApiClient.health` then answers degraded and names them, every other
relation reads normally, and a read of a named one raises :class:`RelationUnavailable`
with the relation and the upstream reason, so its rows are never fabricated or dropped
unseen. Anything else the client cannot trust fails closed with
:class:`WorkbenchNotReady` and a sanitized reason.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import threading
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlencode

from .http import SourceHttpError, SourceUnavailable, check_https_base, get_json


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED = re.compile(r"^([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")
_MAX_RESPONSE = 5_500_000
# Shape bound on the catalog and health relation lists, not a budget: the Portal listed
# 246 relations on 2026-10-21, and a cap at the catalog size made every read unavailable.
_MAX_RELATIONS = 1000
# A reason is plain text: words, numbers and punctuation without URL, query, quote or
# address characters, and never a key-shaped or long opaque token.
_REASON = re.compile(r"[A-Za-z0-9 _.,:;=<>()\[\]+\-]{1,160}")
_SECRET_SHAPE = re.compile(r"plxwb_|[A-Za-z0-9_\-]{33,}", re.IGNORECASE)
_WITHHELD_REASON = "upstream reason not shown (not plain text)"


def _plain_reason(value: Any) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text if _REASON.fullmatch(text) and not _SECRET_SHAPE.search(text) else None


def _upstream_reason(value: Any, default: str) -> str:
    """An upstream reason as plain text; ``default`` when there is none."""
    if not isinstance(value, str) or not value.strip():
        return default
    return _plain_reason(value) or _WITHHELD_REASON


class WorkbenchNotReady(SourceUnavailable):
    """``/v1/health`` or ``/v1/relations`` broke the client's readiness contract, so
    nothing is read. ``reason`` names what failed with field names, relation names and
    counts only, never a key, a URL or a row value."""

    def __init__(self, reason: str) -> None:
        super().__init__("workbench API not ready: " + reason)
        self.reason = reason


class RelationUnavailable(SourceUnavailable):
    """One relation cannot be read while the rest of the API may be: the service lists
    it unavailable (health or catalog), its catalog entry has no pagination key, or its
    rows route answered ``relation-unavailable``. ``relation`` and ``reason`` (the
    upstream reason as plain text, else a fixed description) are safe to report;
    ``status`` is the HTTP status when the rows route refused it, ``error_code`` the
    service's code for the condition."""

    def __init__(self, relation: str, reason: str, *, status: int | None = None) -> None:
        super().__init__(f"workbench API relation {relation} unavailable: {reason}")
        self.relation = relation
        self.reason = reason
        self.status = status
        self.error_code = "relation-unavailable"


def failure_reason(exc: BaseException) -> str | None:
    """The sanitized reason of a readiness or relation failure; None for any other
    exception, whose message may carry a URL."""
    if isinstance(exc, (WorkbenchNotReady, RelationUnavailable)):
        return _plain_reason(exc.reason)
    return None


def _entry_gap(entry: Mapping[str, Any]) -> str | None:
    """Why the API does not serve a catalog entry; None when it does."""
    if entry.get("unavailable"):
        return _upstream_reason(entry["unavailable"], "the catalog marks it unavailable")
    if not entry.get("key"):
        return "its catalog entry has no pagination key"
    return None


def served_entry(catalog: Mapping[str, Mapping[str, Any]], name: str,
                 unavailable_relations: Mapping[str, str] | None = None
                 ) -> Mapping[str, Any]:
    """``name``'s catalog entry when the API serves it, else RelationUnavailable naming
    it: listed by health (``unavailable_relations`` of
    :meth:`WorkbenchReadApiClient.health`), absent from the catalog, marked unavailable
    there or unpaged."""
    if unavailable_relations and name in unavailable_relations:
        raise RelationUnavailable(name, unavailable_relations[name])
    entry = catalog.get(name)
    if entry is None:
        raise RelationUnavailable(name, "not in the API catalog")
    gap = _entry_gap(entry)
    if gap is not None:
        raise RelationUnavailable(name, gap)
    return entry


def _instant(value: Any) -> datetime | None:
    """An ISO 8601 instant with a zone (``Z`` or an offset); None for anything else
    (``"infinity"``, null, a naive time)."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    text = value.strip()
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    if re.search(r"[+-]\d\d$", text):  # Postgres text offsets: +00
        text += ":00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None


def _warnings(value: Mapping[str, Any]) -> dict[str, Any]:
    """Early warnings of a health body that never stop a read: the database login
    expiring soon (its expiry instant and days left) and how many API keys expire soon
    (a count, never their ids)."""
    out: dict[str, Any] = {}
    if value.get("loginExpiringSoon") is True:
        expires = _instant(value.get("loginExpiresAt"))
        days = value.get("loginExpiresInDays")
        out.update(login_expiring_soon=True,
                   login_expires_at=(expires.strftime("%Y-%m-%dT%H:%M:%SZ")
                                     if expires is not None else None),
                   login_expires_in_days=(days if isinstance(days, int)
                                          and not isinstance(days, bool) else None))
    keys = value.get("keysExpiringSoon")
    if isinstance(keys, list) and keys:
        out["keys_expiring_soon_count"] = len(keys)
    return out


def _listed_gaps(value: Mapping[str, Any], field: str) -> dict[str, str]:
    """{relation: reason} of a health list: entries ``{name, reason}`` or ``{name,
    columns}`` (the missing column names, reported as their count)."""
    items = value.get(field)
    if not isinstance(items, list) or len(items) > _MAX_RELATIONS:
        raise WorkbenchNotReady(f"health: {field} is not a list of at most {_MAX_RELATIONS} entries")
    out: dict[str, str] = {}
    for index, item in enumerate(items):
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or _QUALIFIED.fullmatch(name) is None:
            raise WorkbenchNotReady(f"health: {field}[{index}] names no schema.relation")
        columns = item.get("columns")
        if isinstance(item.get("reason"), str) and item["reason"].strip():
            reason = _upstream_reason(item["reason"], _WITHHELD_REASON)
        elif (isinstance(columns, list) and columns and
              all(isinstance(col, str) for col in columns)):
            reason = (f"{len(columns)} approved column"
                      f"{'s are' if len(columns) != 1 else ' is'} missing or not granted")
        else:
            raise WorkbenchNotReady(f"health: {field}[{index}] {name} gives no reason "
                                    "or columns")
        out.setdefault(name, reason)
    return out


class PagingDefect(SourceUnavailable):
    """The service's keyset broke its own contract: a key recurred on a later page,
    the cursor did not move, or the cursor names another key than the page's last row.
    Re-reading the same cursor cannot repair it, so callers never retry it."""


def _cursor_key(cursor: str, name: str, key: Sequence[str]) -> list[Any] | None:
    """The key values a cursor in the service's documented shape carries, else None.

    petralabx/plx-customer-portal services/workbench-api/src/core/query.mjs@c3ffda3
    ``serializeCursor``: base64url JSON ``{"r": relation, "k": key columns, "v": the
    page's last key values}``. The cursor stays opaque to callers; a cursor in any
    other shape is accepted unchecked."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, binascii.Error):
        return None
    if (not isinstance(payload, dict) or payload.get("r") != name or
            payload.get("k") != list(key) or not isinstance(payload.get("v"), list)):
        return None
    return payload["v"]


def _get_workbench_json(url: str, headers: Mapping[str, str], *, max_bytes: int) -> Any:
    # The service allows 30 seconds for a statement and 45 seconds for Lambda.
    return get_json(url, headers, max_bytes=max_bytes, timeout_seconds=40)


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class WorkbenchReadApiClient:
    """Strict GET client for TASK-2014's three routes and keyset pages.

    ``before_page``, when given, runs before every GET (each page of a relation read
    included) and before the GET is sent; what it raises propagates as raised, never as
    SourceUnavailable (a caller's deadline stops a long read at its next page)."""

    def __init__(self, base_url: str, api_key: str, *,
                 fetch: Callable[..., Any] = _get_workbench_json,
                 before_page: Callable[[], None] | None = None) -> None:
        check_https_base(base_url)
        if (not isinstance(api_key, str) or not api_key.startswith("plxwb_")
                or len(api_key) < 20 or any(ord(char) < 33 or ord(char) > 126
                                            for char in api_key)):
            raise ValueError("workbench_api_personal_key_invalid")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._fetch = fetch
        self._before_page = before_page
        # GETs this client sent (each attempt, answered or not).
        self.requests_sent = 0
        # Mirror workers share one client: the count is taken under this lock.
        self._sent_lock = threading.Lock()
        # The listed relations whose catalog was last seen to serve another relation.
        self._served_beyond: frozenset[str] | None = None

    def _get(self, path: str, query: Mapping[str, str | int] | None = None) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        if self._before_page is not None:
            self._before_page()
        with self._sent_lock:
            self.requests_sent += 1
        try:
            return self._fetch(url, {"Authorization": "Bearer " + self._api_key},
                               max_bytes=_MAX_RESPONSE)
        except SourceUnavailable:
            raise
        except Exception as exc:
            raise SourceUnavailable("workbench API GET failed: " +
                                    type(exc).__name__) from None

    def health(self) -> dict[str, Any]:
        """``GET /v1/health``: the service's body plus ``degraded``,
        ``unavailable_relations`` ({relation: reason}) and ``warnings``; ``ok`` stays as
        reported.

        Degraded: the service lists relations in ``unavailable`` or ``missingColumns``.
        The others read normally; a read of a listed one raises RelationUnavailable.
        While degraded, ``GET /v1/relations`` must still serve another relation (checked
        once per set of listed relations). ``loginExpiringSoon`` and
        ``keysExpiringSoon`` are early warnings while the login still works: they are
        reported in ``warnings`` and never stop a read. Fails closed with
        WorkbenchNotReady and its reason on a malformed body or list entry, a replica not
        in recovery, a replay lag outside [0, 60) s (null is the contract's unknown lag),
        no ``generatedAt`` text, a ``loginExpiresAt`` instant already past (at the
        service's ``generatedAt``, else now), ``ok`` false with neither a listed relation
        nor a login warning, or no relation served at all."""
        value = self._get("/v1/health")
        if not isinstance(value, dict):
            raise WorkbenchNotReady("health: the body is not a JSON object")
        if not isinstance(value.get("ok"), bool):
            raise WorkbenchNotReady("health: ok is not a boolean")
        if value.get("recovery") is not True:
            raise WorkbenchNotReady("health: recovery is not true")
        lag = value.get("replayLagSeconds")
        if lag is not None and (isinstance(lag, bool) or not isinstance(lag, (int, float))):
            raise WorkbenchNotReady("health: replayLagSeconds is not a number")
        if lag is not None and not 0 <= lag < 60:
            raise WorkbenchNotReady(f"health: replayLagSeconds {lag:g} is not in [0, 60)")
        if not isinstance(value.get("generatedAt"), str):
            raise WorkbenchNotReady("health: generatedAt is not a string")
        expires = _instant(value.get("loginExpiresAt"))
        now = _instant(value["generatedAt"]) or datetime.now(timezone.utc)
        if expires is not None and expires <= now:
            raise WorkbenchNotReady(f"health: loginExpiresAt {expires:%Y-%m-%dT%H:%M:%SZ} "
                                    "is past (the login expired)")
        gaps = _listed_gaps(value, "unavailable")
        for name, reason in _listed_gaps(value, "missingColumns").items():
            gaps.setdefault(name, reason)
        warnings = _warnings(value)
        if value["ok"] is False and not gaps and not warnings.get("login_expiring_soon"):
            raise WorkbenchNotReady("health: ok is false and neither a relation nor a login "
                                    "warning is listed")
        if gaps:
            self._check_served_beyond(gaps)
        return {**value, "degraded": bool(gaps),
                "unavailable_relations": dict(sorted(gaps.items())), "warnings": warnings}

    def _check_served_beyond(self, gaps: Mapping[str, str]) -> None:
        """WorkbenchNotReady unless the catalog serves a relation health does not list."""
        listed = frozenset(gaps)
        if self._served_beyond == listed:
            return
        catalog = self.relations()
        if not any(name not in listed and _entry_gap(entry) is None
                   for name, entry in catalog.items()):
            raise WorkbenchNotReady(f"health: none of the {len(catalog)} catalog relations "
                                    f"is served ({len(listed)} listed unavailable)")
        self._served_beyond = listed

    def relations(self) -> dict[str, dict[str, Any]]:
        """``GET /v1/relations`` by name. An entry the service does not serve stays
        listed with its ``unavailable`` reason (:func:`served_entry` refuses it); a
        broken contract is WorkbenchNotReady with the entry and the problem."""
        value = self._get("/v1/relations")
        rows = value.get("relations") if isinstance(value, dict) else None
        if not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_RELATIONS:
            raise WorkbenchNotReady(f"catalog: relations is not a list of 1 to {_MAX_RELATIONS} entries")
        result: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(rows):
            name = item.get("name") if isinstance(item, dict) else None
            match = _QUALIFIED.fullmatch(name) if isinstance(name, str) else None
            if match is None:
                raise WorkbenchNotReady(f"catalog: entry {index} names no schema.relation")
            columns = item.get("columns")
            key = item.get("key")
            if item.get("schema") != match[1] or item.get("relation") != match[2]:
                problem = "schema or relation does not match its name"
            elif (not isinstance(columns, list) or not columns or
                    any(not isinstance(col, str) or _NAME.fullmatch(col) is None
                        for col in columns) or
                    len(columns) != len(set(columns))):
                problem = "columns are not distinct column names"
            elif key is not None and (not isinstance(key, list) or not key or
                    any(not isinstance(col, str) for col in key) or
                    len(key) != len(set(key)) or
                    any(col not in columns for col in key)):
                problem = "key is not distinct listed columns"
            elif name in result:
                problem = "is listed twice"
            else:
                result[name] = item
                continue
            raise WorkbenchNotReady(f"catalog: {name} {problem}")
        return result

    def read_page(self, name: str, entry: Mapping[str, Any], *, columns: Sequence[str],
                  cursor: str | None = None, since: str | None = None,
                  page_size: int = 1000) -> dict[str, Any]:
        """One keyset page of ``name`` (``GET /v1/relations/<name>/rows``).

        ``entry`` is the relation's catalog entry from :meth:`relations`, which
        the caller already holds, so a page costs exactly one GET. Mirrors the
        service contract (petralabx/plx-customer-portal
        services/workbench-api/src/core/query.mjs): ``columns`` must be
        approved, the key columns are always returned, ``since`` is ``>=`` on
        the catalog since column, the page holds at most ``page_size`` rows and
        never splits an equal-key run, and ``nextCursor`` is null at the end.
        """
        if (_QUALIFIED.fullmatch(name) is None or not isinstance(entry, Mapping) or
                entry.get("name", name) != name or
                isinstance(page_size, bool) or not isinstance(page_size, int) or
                not 1 <= page_size <= 5000 or
                isinstance(columns, (str, bytes)) or not columns):
            raise ValueError("workbench_api_relation_read_invalid")
        gap = _entry_gap(entry)
        if gap is not None:
            raise RelationUnavailable(name, gap)
        requested = list(columns)
        if (any(not isinstance(col, str) for col in requested) or
                len(requested) != len(set(requested)) or
                any(col not in entry["columns"] for col in requested)):
            raise ValueError("workbench_api_columns_not_approved")
        if since is not None and not entry.get("since"):
            raise ValueError("workbench_api_since_not_supported")
        if cursor is not None and (not isinstance(cursor, str) or
                                   not 1 <= len(cursor) <= 4096):
            raise ValueError("workbench_api_cursor_invalid")
        expected = set(requested) | set(entry["key"])
        query: dict[str, str | int] = {"limit": page_size, "columns": ",".join(requested)}
        if cursor is not None:
            query["cursor"] = cursor
        if since is not None:
            query["since"] = since
        try:
            page = self._get("/v1/relations/" + quote(name, safe=".") + "/rows", query)
        except SourceHttpError as exc:
            if exc.status == 404 and exc.error_code == "relation-unavailable":
                raise RelationUnavailable(name, "its rows route answered relation-unavailable",
                                          status=404) from exc
            raise
        if (not isinstance(page, dict) or page.get("relation") != name or
                not isinstance(page.get("columns"), list) or
                any(not isinstance(col, str) for col in page["columns"]) or
                set(page["columns"]) != expected or
                not isinstance(page.get("rows"), list) or
                len(page["rows"]) > page_size or
                "nextCursor" not in page):
            raise SourceUnavailable("workbench API row page invalid")
        for row in page["rows"]:
            if not isinstance(row, dict) or set(row) != expected:
                raise SourceUnavailable("workbench API row columns invalid")
        next_cursor = page["nextCursor"]
        if next_cursor is not None and (not isinstance(next_cursor, str) or
                                        not 1 <= len(next_cursor) <= 4096):
            raise SourceUnavailable("workbench API row page invalid")
        if next_cursor is not None and (not page["rows"] or next_cursor == cursor):
            raise PagingDefect("workbench API cursor did not progress")
        if next_cursor is not None:
            carried = _cursor_key(next_cursor, name, entry["key"])
            if carried is not None and carried != [page["rows"][-1][col]
                                                   for col in entry["key"]]:
                # The next page would start after another key: rows between the two
                # are skipped (or re-read) silently.
                raise PagingDefect("workbench API cursor does not name the page's last key")
        return {"relation": name, "columns": sorted(expected), "rows": page["rows"],
                "next_cursor": next_cursor}

    def read_relation(self, name: str, *, columns: Sequence[str],
                      max_rows: int = 100_000, max_pages: int = 250,
                      page_size: int = 1000, since: str | None = None
                      ) -> dict[str, Any]:
        """Read every bounded page, with explicit non-atomic/deletion caveats."""
        if (_QUALIFIED.fullmatch(name) is None or
                not 1 <= max_rows <= 1_000_000 or
                not 1 <= max_pages <= 1000 or
                not 1 <= page_size <= 5000 or
                isinstance(columns, (str, bytes)) or not columns):
            raise ValueError("workbench_api_relation_read_invalid")
        before = self.health()
        catalog = self.relations()
        entry = served_entry(catalog, name, before.get("unavailable_relations"))
        cursor = None
        seen_cursors = set()
        seen_keys = set()
        rows: list[dict[str, Any]] = []
        pages = 0
        expected: set[str] = set()
        while True:
            if pages >= max_pages:
                raise SourceUnavailable("workbench API page bound reached")
            page = self.read_page(name, entry, columns=columns, cursor=cursor, since=since,
                                  page_size=page_size)
            expected = set(page["columns"])
            pages += 1
            page_keys = set()
            for row in page["rows"]:
                key = tuple(json.dumps(row[col], sort_keys=True, default=str)
                            for col in entry["key"])
                if key in seen_keys:
                    raise PagingDefect("workbench API duplicate page key")
                # Silver views may contain several rows with one `id`. The
                # service keeps a whole equal-key run on one page so its
                # cursor cannot skip part of that run. Reject only keys that
                # recur on a later page, which signals a broken page boundary
                # or a changing upstream read.
                page_keys.add(key)
                rows.append(row)
                if len(rows) > max_rows:
                    raise SourceUnavailable("workbench API relation row bound reached")
            seen_keys.update(page_keys)
            next_cursor = page["next_cursor"]
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise PagingDefect("workbench API cursor did not progress")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        after = self.health()
        listed = after.get("unavailable_relations") or {}
        if name in listed:
            # It became unavailable during the read: the rows are not a complete read.
            raise RelationUnavailable(name, listed[name])
        return {"schema": "plx-workbench-api-relation-read/v1", "relation": name,
            "columns": sorted(expected), "key": entry["key"], "rows": rows,
            "row_count": len(rows), "page_count": pages,
            "catalog_revision": catalog_revision(entry),
            "health_before": {"generated_at": before["generatedAt"],
                "replay_lag_seconds": before["replayLagSeconds"]},
            "health_after": {"generated_at": after["generatedAt"],
                "replay_lag_seconds": after["replayLagSeconds"]},
            "complete_within_bound": True,
            "atomic_upstream_snapshot": False,
            # Full traversal is still not an atomic snapshot: rows can move
            # across page boundaries while the API is being queried.
            "deletions_proven": False,
            "mode": "full_paged_relation" if since is None else
                "incremental_paged_relation"}


def catalog_revision(entry: Mapping[str, Any]) -> str:
    """Digest of one catalog entry: a change means the relation's shape changed."""
    return _digest(entry)


__all__ = ["PagingDefect", "RelationUnavailable", "WorkbenchNotReady",
           "WorkbenchReadApiClient", "catalog_revision", "failure_reason", "served_entry"]
